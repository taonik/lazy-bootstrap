"""Command line interface.

    lazy-bootstrap doctor                     what this machine can do
    lazy-bootstrap inventory IMAGE            packages installed in an image
    lazy-bootstrap rebuild  [--profile P]     the main command
    lazy-bootstrap report   RUN --format html re-render a finished run
    lazy-bootstrap compare  RUN...            diff several runs
    lazy-bootstrap toolchains                 known toolchains / flavours
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import config as config_mod
from . import engine as engine_mod
from . import report
from .orchestration import logs, util
from .config import RunConfig, ToolchainConfig, default_toolchain_matrix
from .orchestration.executors import probe as probe_backends
from .model import RunReport, Status
from .net import Fetcher
from .planner import plan as make_plan
from .report.compare import compare as compare_runs
from .orchestration.trace import Tracer, level_from_env

log = logs.get("cli")


# --- argument parsing -------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lazy-bootstrap",
        description="Rebuild every package of a distro image, with a toolchain of your choice.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("-v", "--verbose", action="count", default=0)
    parser.add_argument("-q", "--quiet", action="store_true")
    parser.add_argument("--debug", action="count", default=None,
                        help="per-step tracing; repeat for payloads (-dd) and full output (-ddd)")
    sub = parser.add_subparsers(dest="command", required=True)

    # -- doctor ------------------------------------------------------------
    doctor = sub.add_parser("doctor", help="report backends, tools and network reachability")
    doctor.add_argument("--json", action="store_true")
    doctor.add_argument("--no-network", action="store_true", help="skip the reachability probes")

    # -- inventory ---------------------------------------------------------
    inventory = sub.add_parser("inventory", help="list the packages installed in an image")
    _add_target_args(inventory)
    inventory.add_argument("--json", action="store_true")
    inventory.add_argument("--sources", action="store_true", help="list source packages only")

    # -- rebuild -----------------------------------------------------------
    rebuild = sub.add_parser("rebuild", help="rebuild packages with one or more toolchains")
    _add_target_args(rebuild)
    _add_selection_args(rebuild)
    rebuild.add_argument("-t", "--toolchain", action="append", dest="toolchains_cli",
                         help="toolchain id; repeat for a matrix (default: gcc)")
    rebuild.add_argument("--default-toolchain", help="baseline toolchain (default: gcc)")
    rebuild.add_argument("--fallback-toolchain",
                         help="retry failures with this toolchain")
    rebuild.add_argument("--no-preflight", action="store_true",
                         help="do not verify the default toolchain before the matrix")
    rebuild.add_argument("--preflight-package", help="also rebuild this package during preflight")
    rebuild.add_argument("--grouping", choices=["all", "group", "package"])
    rebuild.add_argument("--group-size", type=int)
    rebuild.add_argument("--timeout", type=int, help="per-package timeout in seconds")
    rebuild.add_argument("--build-jobs", type=int, help="make -j inside a build (0 = nproc)")
    rebuild.add_argument("--stop-on-failure", action="store_true")
    rebuild.add_argument("--format", action="append", dest="formats",
                         choices=list(report.FORMATS), help="also print this format to stdout")
    rebuild.add_argument("--dry-run", action="store_true",
                         help="plan and print the units, build nothing")

    # -- toolchain ---------------------------------------------------------
    tc = sub.add_parser("toolchain",
                        help="provision a toolchain into a target and prove it works")
    tc.add_argument("action", choices=["check"],
                    help="check: provision the toolchain and compile+run a program, "
                         "without preparing the distro's build machinery")
    _add_target_args(tc)
    tc.add_argument("-t", "--toolchain", dest="toolchains_cli", action="append",
                    metavar="ID", help="toolchain to check (repeatable)")
    tc.add_argument("--json", action="store_true")

    # -- env ---------------------------------------------------------------
    env = sub.add_parser("env", help="query the execution-environment orchestrator")
    # `action` first: the optional image positional would otherwise swallow it.
    env.add_argument("action", choices=["probe", "available", "ensure"],
                     help="probe: what this machine can do; available: could I get "
                          "this environment, and at what cost (no side effects); "
                          "ensure: get it now and release it")
    _add_target_args(env)
    env.add_argument("--json", action="store_true")

    # -- build-worker ------------------------------------------------------
    worker = sub.add_parser("build-worker",
                            help="build a worker environment (build machinery + toolchain) "
                                 "using any execution class")
    _add_target_args(worker)
    worker.add_argument("-f", "--flavour", help="flavour from ci/flavours.toml")
    worker.add_argument("-t", "--toolchain", dest="toolchains_cli", action="append",
                        help="toolchain id, when not using a flavour")
    worker.add_argument("--save", metavar="TARGET",
                        help="image:TAG | dir:PATH | tar:FILE (default: build and discard)")
    worker.add_argument("--no-probe", action="store_true",
                        help="skip the hello-world check on the toolchain")
    worker.add_argument("--list", action="store_true", help="list the known flavours")

    # -- report ------------------------------------------------------------
    render = sub.add_parser("report", help="re-render a finished run")
    render.add_argument("run", help="run directory or report.json")
    render.add_argument("--format", default="text", choices=list(report.FORMATS))
    render.add_argument("-o", "--output", help="write to a file instead of stdout")

    # -- compare -----------------------------------------------------------
    comparison = sub.add_parser("compare", help="compare several runs")
    comparison.add_argument("runs", nargs="+", help="run directories or report.json files")
    comparison.add_argument("--format", default="text", choices=list(report.FORMATS))
    comparison.add_argument("--baseline", default="", help="label to compare against")
    comparison.add_argument("-o", "--output")

    # -- toolchains --------------------------------------------------------
    toolchains = sub.add_parser("toolchains", help="list known toolchains and CI flavours")
    toolchains.add_argument("--json", action="store_true")
    return parser


def _add_target_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("image", nargs="?", help="image reference, e.g. debian:13-slim")
    parser.add_argument("-p", "--profile", help="TOML profile (see profiles/)")
    parser.add_argument("--distro", help="debian | alpine | auto")
    parser.add_argument("-b", "--backend", choices=["host", "chroot", "bwrap", "firejail",
                                                    "oci", "podman", "docker", "vm", "qemu"])
    parser.add_argument("--rootfs", metavar="SPEC",
                        help="filesystem for chroot/bwrap/firejail: image[:REF] | "
                             "hostfs[:overlay|bind|copy] | dir:PATH | none "
                             "(default: image)")
    parser.add_argument("--rootfs-path", metavar="PATH",
                        help="where to materialise the rootfs; 'temp' for a throwaway one")
    parser.add_argument("--rootfs-prepare", metavar="CMD",
                        help="shell command that populates a dir: rootfs ($LB_ROOTFS is set)")
    parser.add_argument("--workdir", metavar="PATH",
                        help="build directory inside the environment; 'temp' for a throwaway one")
    parser.add_argument("--engine", choices=["podman", "docker", "qemu", "libvirt"],
                        help="container engine, or VM driver when --backend vm")
    parser.add_argument("--registry-mirror", action="append", default=None,
                        metavar="REGISTRY=MIRROR",
                        help="e.g. docker.io=mirror.gcr.io (repeatable)")
    parser.add_argument("--source-mirror", action="append", default=None,
                        metavar="DISTRO=SPEC",
                        help="apt: 'debian=http://host/debian trixie main'; apk: 'alpine=<aports branch>'")
    parser.add_argument("--image-setup", action="append", default=None, metavar="CMD",
                        help="shell command run once on the image before anything else")
    parser.add_argument("--distfiles-mirror", metavar="URL",
                        help="Alpine: mirror of upstream tarballs "
                             "(e.g. https://distfiles.alpinelinux.org/distfiles/)")
    parser.add_argument("--check", choices=["on", "off"], default=None,
                        help="run each package's own test suite (default: on). "
                             "A package that builds but fails its tests has not "
                             "been shown to rebuild correctly.")
    parser.add_argument("--resource", action="append", default=None, metavar="KEY=VALUE",
                        help="resource limit, optionally per phase: cpus=4, "
                             "memory=8G, time=600, jobs=2, device=/dev/dri, or "
                             "build.memory=16G / test.time=600 / download.jobs=2. "
                             "Repeatable.")
    parser.add_argument("--package-cache", metavar="URL",
                        help="caching proxy for build dependencies, e.g. "
                             "http://127.0.0.1:3142 (see ci/cache/compose.yml)")
    parser.add_argument("--cache-budget", metavar="SIZE",
                        help="disk the prefetch may use, e.g. 4G (default: 25%% "
                             "of the free space on the cache filesystem)")
    parser.add_argument("--env-budget", metavar="SIZE",
                        help="space a single build may need before it is declined "
                             "(default: measured free space in the environment)")
    parser.add_argument("--env-store", metavar="PATH",
                        help="keep prepared build environments here for reuse "
                             "(pbuilder-style); default <cache-dir>/env")
    parser.add_argument("--env-reuse", choices=["off", "strict", "relaxed"],
                        default=None,
                        help="reuse saved environments: strict (index must match), "
                             "relaxed (refresh first), off. Default: strict")
    parser.add_argument("--env-slice", choices=["exact", "minor", "major", "any"],
                        default=None,
                        help="how coarsely to key saved environments by package "
                             "version. Default: exact")
    parser.add_argument("--vm-option", action="append", default=None, metavar="KEY=VALUE",
                        help="VM driver setting: SSH_KEY, SSH_USER, SEED, MEMORY, CPUS, "
                             "ACCEL, BOOT_TIMEOUT (repeatable)")
    parser.add_argument("--acquire", choices=["auto", "require", "download", "build"],
                        help="what the orchestrator may do to obtain the environment: "
                             "auto (pull if missing, the default), require (never fetch), "
                             "download, build")
    parser.add_argument("--download-image", dest="acquire", action="store_const",
                        const="download", help="alias for --acquire download")
    parser.add_argument("--no-download-image", dest="acquire", action="store_const",
                        const="require", help="alias for --acquire require")
    parser.add_argument("--system-deps", choices=["off", "target", "host"],
                        help="install the packages declared in ci/system-deps/ "
                             "(default: target; 'host' is required before anything "
                             "is installed on the host backend)")
    parser.add_argument("--cache-dir")
    parser.add_argument("--out-dir")


def _add_selection_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--package", action="append", dest="packages_cli",
                        help="only this package/source (repeatable)")
    parser.add_argument("--include", action="append", help="glob of packages to keep")
    parser.add_argument("--exclude", action="append", help="glob of packages to drop")
    parser.add_argument("--limit", type=int, help="build at most N source packages")


# --- configuration assembly -------------------------------------------------


def resolve_config(args: argparse.Namespace) -> RunConfig:
    """Profile first, CLI flags on top (D-03)."""
    cfg = config_mod.load_profile(args.profile) if getattr(args, "profile", None) else RunConfig()

    overrides: dict[str, object] = {}
    for name in ("image", "distro", "backend", "engine", "grouping", "group_size",
                 "timeout", "build_jobs", "limit", "include", "exclude",
                 "default_toolchain", "fallback_toolchain", "preflight_package",
                 "system_deps", "rootfs", "rootfs_path", "rootfs_prepare", "workdir",
                 "acquire", "distfiles_mirror", "package_cache", "check", "resource",
                 "cache_budget", "env_budget", "env_store", "env_reuse",
                 "env_slice"):
        value = getattr(args, name, None)
        if value not in (None, [], ""):
            overrides[name] = value
    if getattr(args, "packages_cli", None):
        overrides["packages"] = args.packages_cli
    if getattr(args, "toolchains_cli", None):
        overrides["toolchains"] = [ToolchainConfig(id=t) for t in args.toolchains_cli]
    if getattr(args, "image_setup", None):
        overrides["image_setup"] = list(cfg.image_setup) + list(args.image_setup)
    if getattr(args, "no_preflight", False):
        overrides["preflight"] = False
    if getattr(args, "stop_on_failure", False):
        overrides["keep_going"] = False
    if getattr(args, "cache_dir", None):
        overrides["cache_dir"] = Path(args.cache_dir)
    if getattr(args, "out_dir", None):
        overrides["out_dir"] = Path(args.out_dir)
    if getattr(args, "registry_mirror", None):
        mirrors = dict(cfg.registry_mirrors)
        mirrors.update(_pairs(args.registry_mirror))
        overrides["registry_mirrors"] = mirrors
    if getattr(args, "vm_option", None):
        overrides["vm_options"] = {**cfg.vm_options, **_pairs(args.vm_option)}
    if getattr(args, "source_mirror", None):
        mirrors = dict(cfg.source_mirrors)
        mirrors.update(_pairs(args.source_mirror))
        overrides["source_mirrors"] = mirrors

    cfg = cfg.with_overrides(**overrides)
    if not cfg.toolchains:
        cfg.toolchains = [ToolchainConfig(id=cfg.default_toolchain)]
    return cfg


def _require_image(cfg: RunConfig) -> None:
    """An image is only needed when the rootfs actually comes from one (D-25)."""
    if cfg.image or cfg.backend == "host":
        return
    source = (cfg.rootfs or "image").split(":", 1)[0]
    if source == "image":
        raise SystemExit(
            "an image is required (positional argument or profile), or pick a rootfs "
            "that does not need one: --rootfs hostfs / --rootfs dir:PATH")


def _pairs(values: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in values:
        key, _, value = item.partition("=")
        if not value:
            raise SystemExit(f"expected KEY=VALUE, got {item!r}")
        out[key.strip()] = value.strip()
    return out


def make_tracer(args: argparse.Namespace, run_dir: Path | None = None) -> Tracer:
    level = args.debug if args.debug is not None else level_from_env()
    replay = util.ensure_dir(run_dir / "replay") if run_dir else None
    return Tracer(level=level, replay_dir=replay)


# --- commands ---------------------------------------------------------------


def cmd_doctor(args: argparse.Namespace) -> int:
    facts = {"backends": probe_backends()}

    if not args.no_network:
        fetcher = Fetcher(config_mod.DEFAULT_CACHE)
        probes = {
            "github.com": "https://github.com/",
            "github releases": "https://github.com/llvm/llvm-project/releases/latest",
            "docker.io": "https://registry-1.docker.io/v2/",
            "mirror.gcr.io": "https://mirror.gcr.io/v2/",
            "deb.debian.org": "https://deb.debian.org/debian/dists/",
            "dl-cdn.alpinelinux.org": "https://dl-cdn.alpinelinux.org/alpine/",
            "archive.ubuntu.com": "http://archive.ubuntu.com/ubuntu/dists/",
        }
        facts["network"] = {}
        for name, url in probes.items():
            ok, detail = fetcher.reachable(url)
            facts["network"][name] = {"reachable": "yes" if ok else "no", "detail": detail}

    if args.json:
        print(json.dumps(facts, indent=2))
        return 0

    print("Backends and helpers")
    for name, info in facts["backends"].items():
        mark = "ok " if info.get("available") == "yes" else "-- "
        print(f"  [{mark}] {name:<12} {info.get('detail', '')}")
    if "network" in facts:
        print("\nNetwork reachability")
        for name, info in facts["network"].items():
            mark = "ok " if info["reachable"] == "yes" else "-- "
            print(f"  [{mark}] {name:<24} {info['detail']}")
        blocked = [n for n, i in facts["network"].items() if i["reachable"] == "no"]
        if blocked:
            print("\n  Blocked hosts limit what can be rebuilt. Add them to the network")
            print("  allowlist, or point --source-mirror / --registry-mirror elsewhere:")
            for name in blocked:
                print(f"    - {name}")
    return 0


def cmd_inventory(args: argparse.Namespace) -> int:
    cfg = resolve_config(args)
    _require_image(cfg)
    engine = engine_mod.Engine(cfg, make_tracer(args))
    packages, facts, distro_id = engine.inventory()

    if args.sources:
        sources = sorted({p.source for p in packages})
        print("\n".join(sources) if not args.json else json.dumps(sources, indent=2))
        return 0
    if args.json:
        print(json.dumps({
            "image": engine.images.resolve(cfg.image),
            "distro": distro_id,
            "facts": facts,
            "packages": [p.__dict__ for p in packages],
        }, indent=2))
        return 0

    origin = engine.images.resolve(cfg.image) or f"{cfg.backend}:{cfg.rootfs or 'hostfs'}"
    print(f"{origin}  ({facts.get('id', distro_id)} {facts.get('version', '')}, "
          f"{facts.get('arch', '?')}, {facts.get('libc', '?')})")
    print(f"{len(packages)} binary packages from {len({p.source for p in packages})} sources\n")
    for package in sorted(packages, key=lambda p: p.name):
        origin = f"  <- {package.source}" if package.source != package.name else ""
        print(f"  {package.name:<34} {package.version:<26}{origin}")
    return 0


def cmd_rebuild(args: argparse.Namespace) -> int:
    cfg = resolve_config(args)
    _require_image(cfg)

    util.ensure_dir(cfg.out_dir)
    tracer = make_tracer(args)
    engine = engine_mod.Engine(cfg, tracer)

    # Step 1: inventory once and reuse it for every toolchain, so all runs in
    # the matrix are comparing exactly the same package set.
    packages, facts, distro_id = engine.inventory()
    plan = make_plan(packages, cfg)
    log.info("selected %d source packages (%d binaries) in %d unit(s)",
             plan.source_count, len(plan.selected), len(plan.units))

    if args.dry_run:
        print(f"image   : {engine.images.resolve(cfg.image) or '(none)'}")
        print(f"rootfs  : {cfg.rootfs or 'image'}")
        print(f"distro  : {distro_id} {facts.get('version', '')} "
              f"({facts.get('arch')}, {facts.get('libc')})")
        print(f"backend : {cfg.backend}/{cfg.engine}")
        print(f"matrix  : {', '.join(cfg.toolchain_ids)}")
        for unit in plan.units:
            print(f"\nunit {unit.name}: {len(unit.sources)} sources, "
                  f"{unit.binary_count} binaries")
            for source in unit.sources[:40]:
                print(f"  - {source}")
            if len(unit.sources) > 40:
                print(f"  ... and {len(unit.sources) - 40} more")
        return 0

    # Step 2: the default toolchain must work before the matrix is worth running.
    if cfg.preflight:
        try:
            engine.preflight(cfg.default_toolchain)
        except (engine_mod.PreflightError, Exception) as exc:  # noqa: BLE001
            log.error("preflight failed: %s", exc)
            print(f"\npreflight failed with the default toolchain "
                  f"({cfg.default_toolchain}):\n{exc}\n\n"
                  "Fix that first, or pass --no-preflight to run anyway.", file=sys.stderr)
            return 3

    # Step 3: one run per toolchain.
    written: list[Path] = []
    reports: list[RunReport] = []
    for toolchain_id in cfg.toolchain_ids:
        log.info("=== toolchain %s ===", toolchain_id)
        run_report = engine.run(toolchain_id, packages)
        run_report.distro = run_report.distro or distro_id
        run_dir = Path(cfg.out_dir) / run_report.run_id
        tracer.replay_dir = util.ensure_dir(run_dir / "replay")
        files = report.write_all(run_report, run_dir)
        written.append(files["json"])
        reports.append(run_report)
        print(report.render(run_report, "text"))
        for fmt in args.formats or []:
            if fmt != "text":
                print(report.render(run_report, fmt))

    # Step 4: if the run was a matrix, the comparison is the actual deliverable.
    if len(reports) > 1:
        comparison = compare_runs(reports, baseline=cfg.default_toolchain)
        compare_dir = util.ensure_dir(Path(cfg.out_dir) / "comparisons")
        stamp = util.slugify(f"{cfg.image}-{reports[0].run_id[-10:]}")
        for fmt, suffix in (("json", "json"), ("md", "md"), ("html", "html"), ("text", "txt")):
            util.write_text_atomic(compare_dir / f"{stamp}.{suffix}",
                                   report.render_comparison(comparison, fmt))
        print(report.render_comparison(comparison, "text"))
        print(f"comparison written to {compare_dir}/{stamp}.{{json,md,html,txt}}")

    for path in written:
        print(f"report: {path}")

    # Exit code reflects the *default* toolchain only: an experimental toolchain
    # failing is data, not a broken run.
    baseline = next((r for r in reports if r.toolchain == cfg.default_toolchain), None)
    if baseline and any(r.status.is_failure for r in baseline.results):
        return 1
    return 0


def cmd_toolchain(args: argparse.Namespace) -> int:
    """Answer "does this toolchain work on this target?" on its own.

    Deliberately separate from `rebuild`: a toolchain that ships its own clang
    and runtime needs nothing from the distro's archives, so it can be verified
    even where those archives are unreachable. Conflating the two would report
    a network limit as a broken toolchain (docs/SPECS.md D-20, D-31).
    """
    from . import toolchains as toolchains_mod

    cfg = resolve_config(args)
    tracer = make_tracer(args)
    eng = engine_mod.Engine(cfg, tracer)
    results = []
    rc = 0
    env = eng.open_environment("lb-toolchain-check")
    try:
        for entry in cfg.toolchains:
            driver = toolchains_mod.get(entry)
            row = {"id": entry.id, "kind": driver.kind, "ok": False,
                   "source": "", "version": "", "detail": ""}
            try:
                install = driver.provision(env.executor, env.distro.id, env.facts,
                                           eng.fetcher, unit="check", sysdeps=env.sysdeps)
                row["source"], row["version"] = install.source, install.version
                ok, detail = driver.probe(env.executor, install, unit="check",
                                          workdir=eng.workdir)
                row["ok"], row["detail"] = ok, detail.strip()
            except Exception as exc:  # noqa: BLE001 - reported per toolchain
                row["detail"] = str(exc).strip()
            results.append(row)
            rc = rc or (0 if row["ok"] else 1)
    finally:
        env.close()

    if args.json:
        print(json.dumps({"image": cfg.image, "distro": env.facts.get("id", ""),
                          "libc": env.facts.get("libc", ""), "toolchains": results}, indent=2))
        return rc
    print(f"target    : {cfg.image or '(host)'}  "
          f"[{env.facts.get('id','?')} {env.facts.get('version','')}, "
          f"{env.facts.get('arch','?')}, {env.facts.get('libc','?')}]")
    for row in results:
        mark = "ok    " if row["ok"] else "FAILED"
        print(f"  {mark} {row['id']:<14} {row['version'] or '-':<40} "
              f"{('from ' + row['source']) if row['source'] else ''}")
        for line in [l for l in row["detail"].splitlines() if l.strip()][:6]:
            print(f"         {line.strip()[:160]}")
    return rc


def cmd_env(args: argparse.Namespace) -> int:
    """The orchestrator, exposed on its own: query it, or pre-warm an environment.

    Pre-warming is an optimisation, never a prerequisite - `rebuild` acquires
    what it needs by itself, under the same policy (docs/SPECS.md D-27).
    """
    from .orchestration import Orchestrator

    cfg = resolve_config(args)
    orchestrator = Orchestrator(cfg.cache_dir, make_tracer(args))

    if args.action == "probe":
        facts = orchestrator.probe()
        if args.json:
            print(json.dumps(facts, indent=2))
            return 0
        for name, info in facts.items():
            mark = "ok " if info.get("available") == "yes" else "-- "
            print(f"  [{mark}] {name:<12} {info.get('detail', '')}")
        return 0

    engine = engine_mod.Engine(cfg, make_tracer(args))
    request = engine.request("lb-env")
    state = orchestrator.available(request)

    if args.action == "available":
        if args.json:
            print(json.dumps({"request": request.describe(), "satisfied": state.satisfied,
                              "action": state.action, "allowed": state.allowed,
                              "where": state.where, "detail": state.detail,
                              "acquire": request.acquire}, indent=2))
        else:
            print(f"request : {request.describe()}")
            print(f"policy  : --acquire {request.acquire}")
            print(f"status  : {state.summary()}")
        return 0 if state.ok else 1

    # ensure: acquire it now, prove it starts, hand it straight back.
    handle = orchestrator.open(request)
    try:
        facts = handle.describe()
        if args.json:
            print(json.dumps(facts, indent=2))
        else:
            for key, value in facts.items():
                if value:
                    print(f"  {key:<12} {value}")
        return 0
    finally:
        orchestrator.close(handle)


def cmd_build_worker(args: argparse.Namespace) -> int:
    from . import worker as worker_mod

    if args.list:
        for flavour in worker_mod.load_flavours():
            mark = " (default)" if flavour.default else (" (mixed)" if flavour.mixed else "")
            print(f"  {flavour.name:<42} {flavour.distro:<8} {flavour.toolchain:<14}{mark}")
            if flavour.description:
                print(f"      {flavour.description}")
        return 0

    cfg = resolve_config(args)
    if args.flavour:
        flavour = worker_mod.find_flavour(args.flavour)
        # The flavour supplies the base image and toolchain; CLI flags still win.
        cfg = cfg.with_overrides(image=cfg.image or flavour.base)
    else:
        toolchain = (args.toolchains_cli or [cfg.default_toolchain])[0]
        flavour = worker_mod.Flavour(name=f"adhoc-{util.slugify(toolchain)}",
                                     base=cfg.image, toolchain=toolchain)
    _require_image(cfg)

    save = worker_mod.SaveTarget.parse(args.save or "")
    engine = engine_mod.Engine(cfg, make_tracer(args))
    result = worker_mod.WorkerBuilder(engine).build(flavour, save, probe=not args.no_probe)

    print(f"flavour   : {result.flavour}")
    print(f"built on  : {result.backend}")
    print(f"toolchain : {result.toolchain} {result.toolchain_version} "
          f"(from {result.toolchain_source})")
    print(f"probe     : {'ok' if result.ok else 'skipped/failed'}")
    if result.saved_as:
        print(f"saved as  : {result.saved_as}")
    else:
        print("saved as  : (nothing; pass --save image:TAG / dir:PATH / tar:FILE)")
    return 0 if result.ok else 1


def cmd_report(args: argparse.Namespace) -> int:
    run_report = engine_mod.load_report(args.run)
    text = report.render(run_report, args.format)
    _emit(text, args.output)
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    reports = [engine_mod.load_report(path) for path in args.runs]
    comparison = compare_runs(reports, baseline=args.baseline)
    _emit(report.render_comparison(comparison, args.format), args.output)
    return 0


def cmd_toolchains(args: argparse.Namespace) -> int:
    entries = [
        {"id": tc.id, "kind": tc.kind, "version": tc.version,
         "provision": tc.provision, "variant": tc.variant}
        for tc in default_toolchain_matrix()
    ]
    flavours = _load_flavours()
    if args.json:
        print(json.dumps({"toolchains": entries, "ci_flavours": flavours}, indent=2))
        return 0
    print("Toolchains")
    for entry in entries:
        print(f"  {entry['id']:<16} kind={entry['kind']:<6} "
              f"version={entry['version'] or '-':<10} provision={','.join(entry['provision'])}")
    if flavours:
        print("\nCI image flavours (ci/flavours.toml)")
        for flavour in flavours:
            print(f"  {flavour.get('name', '?'):<28} {flavour.get('distro', '?'):<8} "
                  f"{flavour.get('toolchain', '?')}")
    return 0


def _load_flavours() -> list[dict]:
    import tomllib

    path = Path(__file__).resolve().parents[2] / "ci" / "flavours.toml"
    if not path.exists():
        return []
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    return data.get("flavour", [])


def _emit(text: str, output: str | None) -> None:
    if output:
        util.write_text_atomic(output, text)
        print(f"written: {output}", file=sys.stderr)
    else:
        print(text)


# --- entry point ------------------------------------------------------------

COMMANDS = {
    "doctor": cmd_doctor,
    "toolchain": lambda args: cmd_toolchain(args),
    "env": lambda args: cmd_env(args),
    "build-worker": lambda args: cmd_build_worker(args),
    "inventory": cmd_inventory,
    "rebuild": cmd_rebuild,
    "report": cmd_report,
    "compare": cmd_compare,
    "toolchains": cmd_toolchains,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logs.setup(verbosity=args.verbose, quiet=args.quiet)
    try:
        return COMMANDS[args.command](args)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - top level: report, do not traceback
        if args.verbose:
            raise
        print(f"error: {exc}", file=sys.stderr)
        return 2


_ = Status  # re-exported for consumers of this module
