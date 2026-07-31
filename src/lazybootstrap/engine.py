"""The orchestrator: inventory -> preflight -> per-unit rebuild -> report.

Reading order of a run:

  1. open an environment on the image and detect the distro
  2. read the inventory and plan the build units
  3. *preflight*: prove the default toolchain works before spending hours on a
     comparison matrix (this is a hard gate unless --no-preflight)
  4. for each unit: provision the toolchain, prepare the builder, build each
     source package, optionally retry failures with the fallback toolchain
  5. write the canonical JSON report
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from . import distros, sysdeps as sysdeps_mod, toolchains
from .orchestration import rootfs as rootfs_mod, util
from .orchestration import EnvironmentRequest, Orchestrator
from .config import RunConfig, ToolchainConfig
from .distros.base import BuildContext, Distro
from .orchestration.executors import NEEDS_ROOTFS, ExecutorSpec, create
from .orchestration.executors.base import Executor
from .orchestration.images import ImageStore
from .orchestration.logs import get
from .model import Attempt, PackageResult, RunReport, Status, StepLog, ToolchainReport
from .net import DownloadError, Fetcher
from .planner import BuildUnit, Plan, plan as make_plan
from .toolchains.base import Install, ToolchainError
from .orchestration.trace import Tracer

log = get("engine")

WORKDIR = "/build"


class PreflightError(RuntimeError):
    """The default toolchain does not work: refuse to run the matrix."""


@dataclass
class Environment:
    """An open build environment plus what we know about it."""

    executor: Executor
    distro: Distro
    facts: dict[str, str]
    sysdeps: sysdeps_mod.SysDeps | None = None
    handle: object | None = None
    orchestrator: object | None = None

    def close(self) -> None:
        """Hand the environment back; the orchestrator owns its teardown."""
        if self.orchestrator is not None and self.handle is not None:
            self.orchestrator.close(self.handle)
        else:
            self.executor.close()


class Engine:
    def __init__(self, config: RunConfig, tracer: Tracer | None = None) -> None:
        self.config = config
        self.tracer = tracer or Tracer()
        self.fetcher = Fetcher(config.cache_dir, self.tracer)
        self.images = ImageStore(config.cache_dir, config.engine,
                                 config.registry_mirrors, self.tracer)
        # Everything about *where* a command runs goes through the orchestrator;
        # this module only knows what it needs (docs/SPECS.md D-27).
        self.orchestrator = Orchestrator(config.cache_dir, self.tracer)
        # Host workdir policy: default, explicit, or throwaway (D-25).
        self.workdir, self._workdir_temp = rootfs_mod.resolve_workdir(config.workdir, WORKDIR)
        self._open_rootfs: list[rootfs_mod.Rootfs] = []

    # -- environment --------------------------------------------------------

    def request(self, name: str, network: bool = True) -> EnvironmentRequest:
        """Describe the environment this run needs. No acquisition logic here."""
        return EnvironmentRequest(
            backend=self.config.backend,
            engine=self.config.engine,
            image=self.config.image,
            rootfs=self.config.rootfs,
            rootfs_path=self.config.rootfs_path,
            rootfs_prepare=self.config.rootfs_prepare,
            workdir=self.workdir,
            network=network and self.config.network != "disabled",
            name=name,
            acquire=self.config.acquire,
            registry_mirrors=dict(self.config.registry_mirrors),
            env={**{f"LB_VM_{k.upper()}": v for k, v in self.config.vm_options.items()},
                 **({"LB_DISTFILES_MIRROR": self.config.distfiles_mirror}
                    if self.config.distfiles_mirror else {})},
            labels={"tool": "lazy-bootstrap"},
        )

    def open_environment(self, name: str, network: bool = True) -> Environment:
        """Ask the orchestrator for an environment, then identify what came back."""
        request = self.request(name, network)
        handle = self.orchestrator.open(request)
        executor = handle.executor

        # Optional one-shot mutation of the image (the "+libc6-compat" case).
        for command in self.config.image_setup:
            executor.run(command, title="image setup", timeout=900,
                         unit=name, step_prefix="setup").check("image setup")

        distro = (distros.get(self.config.distro) if self.config.distro != "auto"
                  else distros.detect(executor))
        facts = distro.describe(executor)
        log.info("environment %s: %s %s (%s, %s)", name, facts.get("id"),
                 facts.get("version"), facts.get("arch"), facts.get("libc"))

        # Declared system dependencies, subject to the run's policy (D-24).
        deps = sysdeps_mod.SysDeps(
            executor=executor, family=distro.id, backend=self.config.backend,
            policy=self.config.system_deps, unit=name)
        if not deps.allowed:
            log.info("system dependencies will not be installed (%s)", deps.why_not())
        return Environment(executor=executor, distro=distro, facts=facts, sysdeps=deps,
                           handle=handle, orchestrator=self.orchestrator)

    # -- inventory ----------------------------------------------------------

    def inventory(self) -> tuple[list, dict[str, str], str]:
        env = self.open_environment("lb-inventory")
        try:
            packages = env.distro.inventory(env.executor)
            log.info("%d installed packages, %d source packages",
                     len(packages), len({p.source for p in packages}))
            return packages, env.facts, env.distro.id
        finally:
            env.close()

    # -- preflight ----------------------------------------------------------

    def preflight(self, toolchain_id: str) -> ToolchainReport:
        """Prove the default toolchain provisions, compiles and links (D-12).

        Optionally build one real package too (`preflight_package`), which
        catches "the compiler works but the distro's build machinery does not"
        far more cheaply than discovering it on package 200.
        """
        config = self.config.toolchain(toolchain_id)
        env = self.open_environment("lb-preflight")
        try:
            driver = toolchains.get(config)
            log.info("preflight: provisioning %s", toolchain_id)
            install = driver.provision(env.executor, env.distro.id, env.facts,
                                       self.fetcher, unit="preflight", sysdeps=env.sysdeps)
            ok, detail = driver.probe(env.executor, install, unit="preflight", workdir=self.workdir)
            report = driver.report(install, ok, detail)
            if not ok:
                raise PreflightError(
                    f"default toolchain {toolchain_id} cannot compile a hello world:\n{detail}")
            log.info("preflight: %s ok (%s)", toolchain_id, report.version or "version unknown")

            if self.config.preflight_package:
                self._preflight_build(env, driver, install, report)
            return report
        finally:
            env.close()

    def _preflight_build(self, env: Environment, driver, install: Install,
                         report: ToolchainReport) -> None:
        source = self.config.preflight_package
        log.info("preflight: rebuilding canary package %s", source)
        ctx = self._context(env, driver.environment(install), unit="preflight")
        env.distro.prepare_builder(env.executor, ctx)
        result = self._build_source(env, source, ctx)
        if not result.status.is_success:
            raise PreflightError(
                f"canary package {source} does not rebuild with the default toolchain "
                f"({result.status.value}): {util.tail(result.error, 1200)}")
        report.detail = (report.detail + f"\ncanary {source}: ok").strip()

    # -- the main run -------------------------------------------------------

    def run(self, toolchain_id: str, packages: list, label: str = "") -> RunReport:
        """Rebuild the planned packages with one toolchain."""
        started = time.monotonic()
        run_id = f"{util.slugify(self.config.image)}-{util.slugify(toolchain_id)}-{int(time.time())}"
        report = RunReport(
            run_id=run_id,
            started_at=util.now_iso(),
            image=self.images.resolve(self.config.image),
            backend=self.config.backend,
            toolchain=toolchain_id,
            fallback=self.config.fallback_toolchain,
            grouping=self.config.grouping,
            label=label or toolchain_id,
            notes=list(self.config.notes),
        )

        plan = make_plan(packages, self.config)
        log.info("plan: %d source packages in %d unit(s) [%s]",
                 plan.source_count, len(plan.units), plan.grouping)

        for unit in plan.units:
            self._run_unit(unit, toolchain_id, report)

        # Packages filtered out are still listed, so a report always describes
        # the whole image rather than only the interesting slice.
        for package in plan.skipped:
            report.results.append(PackageResult(package=package, status=Status.SKIPPED,
                                                toolchain=toolchain_id, unit="-"))

        report.finished_at = util.now_iso()
        report.environment.update({
            "engine": self.config.engine if self.config.backend in
                      ("oci", "podman", "docker") else "",
            "grouping": self.config.grouping,
            "jobs": str(self.config.jobs),
            "system_deps": self.config.system_deps,
            "rootfs": self.config.rootfs or ("image" if self.config.backend in NEEDS_ROOTFS else ""),
            "workdir": self.workdir,
            "acquire": self.config.acquire,
            "wall_seconds": round(time.monotonic() - started, 2),
            "source_mirror": (self.config.source_mirrors.get(report.distro or "", "")
                              or self.config.source_mirrors.get(
                                  report.environment.get("driver", ""), "")),
            "registry_mirrors": ", ".join(f"{k}->{v}" for k, v in self.config.registry_mirrors.items()),
        })
        report.recompute_stats()
        return report

    def _run_unit(self, unit: BuildUnit, toolchain_id: str, report: RunReport) -> None:
        log.info("unit %s: %d source package(s)", unit.name, len(unit.sources))
        env = self.open_environment(f"lb-{util.slugify(toolchain_id)}-{unit.name}")
        try:
            # `distro` is what the image says it is (ubuntu), `driver` is the
            # family that knows how to build it (debian). Both matter in a report.
            report.distro = env.facts.get("id") or env.distro.id
            report.environment.setdefault("driver", env.distro.id)
            report.environment.setdefault("distro_version", env.facts.get("version", ""))
            report.environment.setdefault("arch", env.facts.get("arch", ""))
            report.environment.setdefault("libc", env.facts.get("libc", ""))

            installs = self._provision(env, toolchain_id, unit, report)
            if toolchain_id not in installs:
                self._mark_unit(unit, report, Status.BLOCKED, toolchain_id,
                                "toolchain could not be provisioned")
                return

            driver, install = installs[toolchain_id]
            ctx = self._context(env, driver.environment(install), unit=unit.name)
            prepare_steps = env.distro.prepare_builder(env.executor, ctx)
            failed = [s for s in prepare_steps if s.rc != 0 and s.fatal]
            if failed:
                self._mark_unit(unit, report, Status.BLOCKED, toolchain_id,
                                f"builder preparation failed at {failed[0].name}:\n"
                                f"{util.tail(failed[0].output, 1500)}")
                return

            for source in unit.sources:
                result = self._build_source(env, source, ctx)
                if result.status.is_failure and self.config.fallback_toolchain:
                    result = self._retry_with_fallback(env, source, unit, report, result)
                for package in unit.packages[source]:
                    report.results.append(PackageResult(
                        package=package,
                        status=result.status,
                        toolchain=result.toolchain,
                        attempts=result.attempts,
                        seconds=result.seconds,
                        artifacts=result.artifacts,
                        unit=unit.name,
                    ))
                if result.status.is_failure and not self.config.keep_going:
                    log.error("stopping after %s (keep_going disabled)", source)
                    break
        finally:
            if env.sysdeps is not None:
                for note in env.sysdeps.skipped:
                    if note not in report.notes:
                        report.notes.append(note)
            env.close()

    # -- provisioning -------------------------------------------------------

    def _provision(self, env: Environment, toolchain_id: str, unit: BuildUnit,
                   report: RunReport) -> dict[str, tuple]:
        """Provision the unit's toolchain (and the fallback, if configured)."""
        wanted = [toolchain_id]
        if self.config.fallback_toolchain and self.config.fallback_toolchain != toolchain_id:
            wanted.append(self.config.fallback_toolchain)

        installs: dict[str, tuple] = {}
        for entry in wanted:
            config = self.config.toolchain(entry)
            driver = toolchains.get(config)
            try:
                install = driver.provision(env.executor, env.distro.id, env.facts,
                                           self.fetcher, unit=unit.name, sysdeps=env.sysdeps)
            except (ToolchainError, DownloadError) as exc:
                log.error("toolchain %s unavailable: %s", entry, exc)
                report.toolchains.append(ToolchainReport(
                    id=entry, kind=config.kind, ok=False, detail=str(exc)))
                continue
            ok, detail = driver.probe(env.executor, install, unit=unit.name, workdir=self.workdir)
            report.toolchains.append(driver.report(install, ok, detail))
            if ok:
                installs[entry] = (driver, install)
            else:
                log.error("toolchain %s failed its probe: %s", entry, detail[:400])
        return installs

    def _context(self, env: Environment, toolchain_env: dict[str, str], unit: str) -> BuildContext:
        # A mirror may be keyed by the concrete distro (ubuntu) or by the
        # driver family (debian); the concrete one wins.
        mirror = (self.config.source_mirrors.get(env.facts.get("id", ""))
                  or self.config.source_mirrors.get(env.distro.id, ""))
        return BuildContext(
            workdir=self.workdir,
            env=toolchain_env,
            jobs=self.config.build_jobs,
            timeout=self.config.timeout,
            unit=unit,
            source_mirror=mirror,
            sysdeps=env.sysdeps,
        )

    # -- one source package -------------------------------------------------

    def _build_source(self, env: Environment, source: str, ctx: BuildContext) -> PackageResult:
        """fetch -> build-dep -> build -> collect, recorded step by step."""
        from .model import PackageRef

        started = time.monotonic()
        steps: list[StepLog] = []
        status = Status.FAILED
        error = ""
        artifacts: list[str] = []

        tree, fetch_result = env.distro.fetch_source(env.executor, source, ctx)
        steps.append(env.distro.step("fetch-source", fetch_result))
        if tree is None:
            status = _classify_fetch_failure(fetch_result.output)
            error = util.tail(fetch_result.output, 2000)
        else:
            deps_result = env.distro.install_build_deps(env.executor, source, ctx)
            steps.append(env.distro.step("build-deps", deps_result))
            if not deps_result.ok:
                status = _classify_fetch_failure(deps_result.output)
                error = util.tail(deps_result.output, 2000)
            else:
                build_result = env.distro.build(env.executor, tree, ctx)
                steps.append(env.distro.step("build", build_result, limit=8000))
                if build_result.ok:
                    status = Status.OK
                    artifacts = env.distro.collect_artifacts(env.executor, tree, ctx)
                elif build_result.rc == 124:
                    status = Status.TIMEOUT
                    error = f"exceeded {ctx.timeout}s"
                else:
                    status = Status.FAILED
                    error = util.tail(build_result.output, 4000)

        seconds = time.monotonic() - started
        attempt = Attempt(toolchain=ctx.env.get("LB_TOOLCHAIN", "?"), status=status,
                          seconds=round(seconds, 2), steps=steps, error=error)
        return PackageResult(
            package=PackageRef(name=source, source_name=source),
            status=status, toolchain=attempt.toolchain, attempts=[attempt],
            seconds=round(seconds, 2), artifacts=artifacts, unit=ctx.unit,
        )

    def _retry_with_fallback(self, env: Environment, source: str, unit: BuildUnit,
                             report: RunReport, first: PackageResult) -> PackageResult:
        """Rebuild with the fallback toolchain, keeping both attempts on record."""
        fallback = self.config.fallback_toolchain
        installs = {tc.id: tc for tc in report.toolchains}
        if fallback not in installs or not installs[fallback].ok:
            return first

        config = self.config.toolchain(fallback)
        driver = toolchains.get(config)
        # The fallback was provisioned in _provision; re-deriving the install
        # from the report keeps this cheap (no second download or unpack).
        install = Install(toolchain_id=fallback, kind=config.kind,
                          source=installs[fallback].source,
                          cc=installs[fallback].cc, cxx=installs[fallback].cxx,
                          version=installs[fallback].version)
        driver.install_shim(env.executor, install, unit=unit.name)
        ctx = self._context(env, driver.environment(install), unit=unit.name)

        log.info("retrying %s with fallback toolchain %s", source, fallback)
        retry = self._build_source(env, source, ctx)
        merged = PackageResult(
            package=first.package,
            status=Status.FALLBACK if retry.status.is_success else retry.status,
            toolchain=fallback,
            attempts=[*first.attempts, *retry.attempts],
            seconds=round(first.seconds + retry.seconds, 2),
            artifacts=retry.artifacts,
            unit=unit.name,
        )
        return merged

    # -- bookkeeping --------------------------------------------------------

    def _mark_unit(self, unit: BuildUnit, report: RunReport, status: Status,
                   toolchain_id: str, error: str) -> None:
        """Record a whole unit as blocked/failed - used when the environment
        itself could not be prepared, so no package ever got a chance."""
        log.error("unit %s: %s", unit.name, error.splitlines()[0] if error else status.value)
        for source, packages in unit.packages.items():
            attempt = Attempt(toolchain=toolchain_id, status=status, seconds=0.0, error=error)
            for package in packages:
                report.results.append(PackageResult(
                    package=package, status=status, toolchain=toolchain_id,
                    attempts=[attempt], unit=unit.name))


# --- classification ---------------------------------------------------------

from .toolchains.base import NETWORK_MARKERS as _NETWORK_MARKERS

_NOSOURCE_MARKERS = (
    "Unable to find a source package",
    "no APKBUILD for",
    "E: Unable to find a source package for",
    "no source tree unpacked",
)


def _classify_fetch_failure(output: str) -> Status:
    """Distinguish "the network would not let me" from "this package cannot build".

    Conflating the two would poison a toolchain comparison, which is the whole
    point of the tool (D-20).
    """
    if any(marker in output for marker in _NOSOURCE_MARKERS):
        return Status.NOSOURCE
    if any(marker in output for marker in _NETWORK_MARKERS):
        return Status.BLOCKED
    return Status.FAILED


def write_report(report: RunReport, out_dir: str | Path) -> Path:
    path = Path(out_dir) / report.run_id / "report.json"
    util.write_json_atomic(path, report.to_dict())
    log.info("report written: %s", path)
    return path


def load_report(path: str | Path) -> RunReport:
    import json

    candidate = Path(path)
    if candidate.is_dir():
        candidate = candidate / "report.json"
    return RunReport.from_dict(json.loads(candidate.read_text(encoding="utf-8")))
