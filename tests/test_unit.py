"""Unit tests: everything that can be checked without building anything.

    python3 tests/test_unit.py          (no dependencies, no network, no root)

The end-to-end coverage lives in tests/matrix.sh, which exercises the real
execution classes.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lazybootstrap import planner, sysdeps  # noqa: E402
from lazybootstrap.orchestration import images, rootfs, util  # noqa: E402
from lazybootstrap.orchestration import spec as envspec  # noqa: E402
from lazybootstrap.orchestration.orchestrator import Orchestrator  # noqa: E402
from lazybootstrap.config import RunConfig, ToolchainConfig, from_mapping  # noqa: E402
from lazybootstrap.model import (Attempt, PackageRef, PackageResult,  # noqa: E402
                                 RunReport, Status, StepLog)
from lazybootstrap.report import compare as compare_mod  # noqa: E402
from lazybootstrap.report import render, render_comparison  # noqa: E402
from lazybootstrap.toolchains import filc, llvm  # noqa: E402
from lazybootstrap.toolchains.base import Install, Toolchain  # noqa: E402


# --- images -----------------------------------------------------------------


class TestImageReferences(unittest.TestCase):
    def test_normalise(self):
        self.assertEqual(images.normalise("alpine"), "docker.io/library/alpine:latest")
        self.assertEqual(images.normalise("debian:13-slim"), "docker.io/library/debian:13-slim")
        self.assertEqual(images.normalise("ghcr.io/o/r:v1"), "ghcr.io/o/r:v1")
        self.assertEqual(images.normalise("myorg/img"), "docker.io/myorg/img:latest")

    def test_mirror_rewrites_only_the_registry(self):
        mirrors = {"docker.io": "mirror.gcr.io"}
        self.assertEqual(images.apply_mirrors("debian:13-slim", mirrors),
                         "mirror.gcr.io/library/debian:13-slim")
        # A different registry is left alone.
        self.assertEqual(images.apply_mirrors("ghcr.io/o/r:v1", mirrors), "ghcr.io/o/r:v1")

    def test_no_mirror_is_a_no_op_but_still_normalises(self):
        self.assertEqual(images.apply_mirrors("alpine", {}), "alpine")


# --- rootfs -----------------------------------------------------------------


class TestRootfsSpec(unittest.TestCase):
    def test_parse_forms(self):
        spec = rootfs.RootfsSpec.parse("image", image="debian:13-slim")
        self.assertEqual((spec.source, spec.ref), (rootfs.IMAGE, "debian:13-slim"))

        spec = rootfs.RootfsSpec.parse("image:alpine", image="ignored")
        self.assertEqual(spec.ref, "alpine")

        spec = rootfs.RootfsSpec.parse("hostfs:overlay")
        self.assertEqual((spec.source, spec.mode), (rootfs.HOSTFS, "overlay"))

        spec = rootfs.RootfsSpec.parse("dir:/srv/rootfs")
        self.assertEqual((spec.source, spec.path), (rootfs.DIR, "/srv/rootfs"))

        self.assertEqual(rootfs.RootfsSpec.parse("none").source, rootfs.NONE)

    def test_rejects_nonsense(self):
        with self.assertRaises(rootfs.RootfsError):
            rootfs.RootfsSpec.parse("wat")
        with self.assertRaises(rootfs.RootfsError):
            rootfs.RootfsSpec.parse("hostfs:teleport")
        with self.assertRaises(rootfs.RootfsError):
            rootfs.RootfsSpec.parse("dir:")

    def test_workdir_policy(self):
        self.assertEqual(rootfs.resolve_workdir("", "/build"), ("/build", False))
        self.assertEqual(rootfs.resolve_workdir("/w"), ("/w", False))
        path, temporary = rootfs.resolve_workdir("temp")
        self.assertTrue(temporary and Path(path).is_dir())
        Path(path).rmdir()


# --- toolchain version logic ------------------------------------------------


class TestLlvmVersions(unittest.TestCase):
    def test_exact_version_is_not_satisfied_by_a_neighbour(self):
        # The whole point of D-12: a comparison must not silently use 20.1.2
        # when it was told 20.1.8.
        self.assertFalse(llvm.version_satisfies("20.1.8", "20.1.2"))
        self.assertTrue(llvm.version_satisfies("20.1.8", "20.1.8"))
        self.assertTrue(llvm.version_satisfies("20", "20.1.2"))
        self.assertTrue(llvm.version_satisfies("20.1", "20.1.2"))
        self.assertTrue(llvm.version_satisfies("", "anything"))

    def test_nearest_versions_tries_the_exact_one_first(self):
        candidates = llvm.nearest_versions("20.1.8", window=3)
        self.assertEqual(candidates[0], "20.1.8")
        self.assertIn("20.1.7", candidates)
        self.assertIn("20.1.9", candidates)

    def test_asset_naming_changed_at_llvm_20(self):
        self.assertEqual(llvm._asset_names("20.1.8", "X64"),
                         ["LLVM-20.1.8-Linux-X64.tar.xz"])
        self.assertTrue(llvm._asset_names("18.1.8", "X64")[0].startswith("clang+llvm-18.1.8"))


class TestFilcVariant(unittest.TestCase):
    def _driver(self, variant="auto"):
        return filc.FilcToolchain(ToolchainConfig(id="filc-0.681", variant=variant))

    def test_libc_decides(self):
        self.assertEqual(self._driver().select_variant("musl"), filc.PIZFIX)
        self.assertEqual(self._driver().select_variant("glibc"), filc.OPTFIL)

    def test_alpine_with_libc6_compat_is_still_musl(self):
        # gcompat is a shim, not glibc: the target reports musl, so pizfix (D-13).
        self.assertEqual(self._driver().select_variant("musl"), filc.PIZFIX)

    def test_explicit_override_wins(self):
        self.assertEqual(self._driver("optfil").select_variant("musl"), filc.OPTFIL)
        self.assertEqual(self._driver("pizfix").select_variant("glibc"), filc.PIZFIX)

    def test_unknown_libc_falls_back_to_the_self_contained_build(self):
        self.assertEqual(self._driver().select_variant("unknown"), filc.PIZFIX)

    def test_asset_name_is_not_the_variant_name(self):
        # Upstream publishes the musl build as filc-*, not pizfix-*; assuming
        # otherwise 404s every musl provisioning (found on alpine).
        self.assertEqual(filc.ASSET_PREFIX[filc.PIZFIX], "filc")
        self.assertEqual(filc.ASSET_PREFIX[filc.OPTFIL], "optfil")


# --- the compiler shim ------------------------------------------------------


class _FakeToolchain(Toolchain):
    kind = "fake"

    def provision(self, *a, **kw):  # pragma: no cover - not used here
        raise NotImplementedError


class TestShim(unittest.TestCase):
    def test_drop_flags_land_in_the_shim(self):
        from lazybootstrap.toolchains.base import _shim_body

        body = _shim_body("/opt/cc", "-O2", "", ["-flto", "-ffat-lto-objects"])
        self.assertIn("exec /opt/cc -O2", body)
        self.assertIn("-flto | -ffat-lto-objects)", body)
        self.assertIn("set -- \"$@\" \"$arg\"", body)   # the filtering rotation

    def test_no_drop_flags_means_no_filter_block(self):
        from lazybootstrap.toolchains.base import _shim_body

        body = _shim_body("/usr/bin/gcc", "", "", [])
        self.assertNotIn("count=$#", body)

    def test_explicit_dash_disables_the_defaults(self):
        driver = _FakeToolchain(ToolchainConfig(id="fake", drop_flags=["-"]))
        driver.default_drop_flags = ["-flto"]
        self.assertEqual(driver.drop_flags(Install("fake", "fake", "distro")), [])

    def test_shlibdeps_shim_is_only_written_for_own_runtimes(self):
        from lazybootstrap.toolchains.base import _shlibdeps_body

        body = _shlibdeps_body(["/opt/fil/lib"])
        self.assertIn("-l/opt/fil/lib", body)
        self.assertIn("--ignore-missing-info", body)

    def test_missing_libc_headers_are_not_a_broken_toolchain(self):
        # A freshly unpacked clang on a runtime-only image compiles nothing
        # because there are no headers, not because it is broken (D-34).
        hint = Toolchain._probe_hint("probe.c:2:10: fatal error: 'stdio.h' file not found")
        self.assertIn("no C library headers", hint)
        self.assertIn("libc6-dev", hint)
        self.assertEqual(Toolchain._probe_hint("error: too few arguments"), "")

    def test_environment_puts_the_shim_first_on_path(self):
        driver = _FakeToolchain(ToolchainConfig(id="fake"))
        install = Install("fake", "fake", "distro", cc="/x/cc", cxx="/x/c++", shimmed=True)
        env = driver.environment(install)
        self.assertTrue(env["PATH"].startswith("/opt/lazy-bootstrap/shim/bin:"))

    def test_cc_is_not_exported_next_to_the_shim(self):
        # D-29: an exported CC beats the compiler autoconf derives from --host,
        # so gzip's --host=i686-w64-mingw32 sub-build dies under plain gcc.
        driver = _FakeToolchain(ToolchainConfig(id="fake"))
        install = Install("fake", "fake", "distro", cc="/x/cc", cxx="/x/c++", shimmed=True)
        env = driver.environment(install)
        self.assertNotIn("CC", env)
        self.assertNotIn("CXX", env)
        # ...but the probe still has a name to call.
        self.assertEqual(env["LB_CC"], "/opt/lazy-bootstrap/shim/bin/cc")

    def test_cc_is_exported_when_there_is_no_shim(self):
        driver = _FakeToolchain(ToolchainConfig(id="fake"))
        install = Install("fake", "fake", "distro", cc="/x/cc", cxx="/x/c++", shimmed=False)
        self.assertEqual(driver.environment(install)["CC"], "/x/cc")

    def test_export_cc_can_be_turned_back_on(self):
        driver = _FakeToolchain(ToolchainConfig(id="fake", export_cc=True))
        install = Install("fake", "fake", "distro", cc="/x/cc", cxx="/x/c++", shimmed=True)
        self.assertEqual(driver.environment(install)["CC"], "/opt/lazy-bootstrap/shim/bin/cc")


# --- planner ----------------------------------------------------------------


def _packages():
    return [
        PackageRef(name="libc6", version="2.41", source_name="glibc"),
        PackageRef(name="libc-bin", version="2.41", source_name="glibc"),
        PackageRef(name="bash", version="5.2", source_name="bash"),
        PackageRef(name="coreutils", version="9.5", source_name="coreutils"),
        PackageRef(name="perl-base", version="5.40", source_name="perl"),
    ]


class TestPlanner(unittest.TestCase):
    def test_binaries_are_grouped_by_source(self):
        plan = planner.plan(_packages(), RunConfig(grouping="all"))
        self.assertEqual(len(plan.units), 1)
        self.assertEqual(plan.source_count, 4)          # glibc counted once
        self.assertEqual(plan.units[0].binary_count, 5)

    def test_grouping_package_is_one_unit_per_source(self):
        plan = planner.plan(_packages(), RunConfig(grouping="package"))
        self.assertEqual(len(plan.units), 4)

    def test_grouping_group_respects_the_size(self):
        plan = planner.plan(_packages(), RunConfig(grouping="group", group_size=2))
        self.assertEqual([len(u.sources) for u in plan.units], [2, 2])

    def test_limit_counts_sources_not_binaries(self):
        # limit=1 means one *build*, so all binaries of that source come along:
        # glibc is first in inventory order and produces two of them.
        plan = planner.plan(_packages(), RunConfig(limit=1))
        self.assertEqual(plan.source_count, 1)
        self.assertEqual({p.name for p in plan.selected}, {"libc6", "libc-bin"})

    def test_exclude_and_include(self):
        plan = planner.plan(_packages(), RunConfig(exclude=["lib*"]))
        self.assertNotIn("libc6", [p.name for p in plan.selected])
        plan = planner.plan(_packages(), RunConfig(include=["lib*"]))
        self.assertEqual({p.name for p in plan.selected}, {"libc6", "libc-bin"})

    def test_explicit_package_matches_binary_or_source(self):
        plan = planner.plan(_packages(), RunConfig(packages=["glibc"]))
        self.assertEqual({p.name for p in plan.selected}, {"libc6", "libc-bin"})


# --- configuration ----------------------------------------------------------


class TestConfig(unittest.TestCase):
    def test_provision_defaults_depend_on_the_kind(self):
        self.assertEqual(ToolchainConfig(id="gcc").provision, ["preinstalled", "distro"])
        self.assertEqual(ToolchainConfig(id="llvm-20.1.8").provision, ["distro", "binary"])
        self.assertEqual(ToolchainConfig(id="filc-0.681").provision, ["binary"])

    def test_kind_and_version_are_inferred_from_the_id(self):
        entry = ToolchainConfig(id="clang-19")
        self.assertEqual((entry.kind, entry.version), ("llvm", "19"))
        self.assertEqual(ToolchainConfig(id="gcc").version, "")

    def test_profile_round_trip(self):
        raw = {
            "image": "debian:13-slim",
            "run": {"backend": "podman", "grouping": "group", "group_size": 5},
            "exclude": ["linux-*"],
            "toolchains": [{"id": "gcc"}, {"id": "filc-0.681", "provision": ["binary"]}],
        }
        cfg = from_mapping(raw)
        self.assertEqual(cfg.image, "debian:13-slim")
        self.assertEqual(cfg.group_size, 5)
        self.assertEqual(cfg.toolchain_ids, ["gcc", "filc-0.681"])
        self.assertEqual(cfg.exclude, ["linux-*"])

    def test_unknown_keys_are_rejected_loudly(self):
        with self.assertRaises(ValueError):
            from_mapping({"totally_not_a_key": 1})

    def test_cli_overrides_beat_the_profile(self):
        cfg = RunConfig(backend="oci", limit=3).with_overrides(backend="host", limit=None)
        self.assertEqual(cfg.backend, "host")
        self.assertEqual(cfg.limit, 3)      # None means "not specified"


# --- system deps ------------------------------------------------------------


class TestSysDeps(unittest.TestCase):
    def test_declared_lists_are_read_and_commented_lines_ignored(self):
        packages = sysdeps.load("debian", "filc")
        self.assertIn("xz-utils", packages)
        self.assertIn("patchelf", packages)
        self.assertTrue(all(not p.startswith("#") for p in packages))

    def test_ubuntu_maps_to_the_debian_family(self):
        self.assertEqual(sysdeps.load("ubuntu", "common"), sysdeps.load("debian", "common"))

    def test_missing_component_is_empty_not_an_error(self):
        self.assertEqual(sysdeps.load("debian", "no-such-component"), [])

    def test_host_backend_requires_an_explicit_opt_in(self):
        deps = sysdeps.SysDeps(executor=None, family="debian", backend="host", policy="target")
        self.assertFalse(deps.allowed)
        self.assertIn("host", deps.why_not())
        deps.policy = "host"
        self.assertTrue(deps.allowed)

    def test_container_backends_install_by_default(self):
        deps = sysdeps.SysDeps(executor=None, family="debian", backend="oci", policy="target")
        self.assertTrue(deps.allowed)
        deps.policy = "off"
        self.assertFalse(deps.allowed)


# --- report model and renderers ---------------------------------------------


def _report(label: str, outcomes: dict[str, Status]) -> RunReport:
    report = RunReport(run_id=f"run-{label}", started_at="2026-01-01T00:00:00+00:00",
                       label=label, toolchain=label, image="debian:13-slim", distro="debian")
    for name, status in outcomes.items():
        report.results.append(PackageResult(
            package=PackageRef(name=name, version="1.0", source_name=name),
            status=status, toolchain=label, seconds=1.5,
            attempts=[Attempt(toolchain=label, status=status, seconds=1.5,
                              steps=[StepLog(name="build", rc=0 if status.is_success else 2,
                                             seconds=1.5, output="boom")])],
        ))
    report.recompute_stats()
    return report


class TestReport(unittest.TestCase):
    def test_stats(self):
        report = _report("gcc", {"a": Status.OK, "b": Status.FAILED, "c": Status.SKIPPED})
        self.assertEqual(report.stats.total, 3)
        self.assertEqual(report.stats.success, 1)
        self.assertAlmostEqual(report.stats.success_rate, 100 / 3, places=3)

    def test_fallback_counts_as_success(self):
        report = _report("x", {"a": Status.FALLBACK})
        self.assertEqual(report.stats.success, 1)

    def test_json_round_trip_is_lossless(self):
        report = _report("gcc", {"a": Status.OK, "b": Status.BLOCKED})
        restored = RunReport.from_dict(json.loads(json.dumps(report.to_dict())))
        self.assertEqual(restored.run_id, report.run_id)
        self.assertEqual([r.status for r in restored.results],
                         [r.status for r in report.results])
        self.assertEqual(restored.stats.by_status, report.stats.by_status)

    def test_every_format_renders(self):
        report = _report("gcc", {"a": Status.OK, "b": Status.FAILED})
        for fmt in ("text", "md", "json", "html"):
            out = render(report, fmt)
            self.assertTrue(out.strip(), f"{fmt} rendered nothing")
        self.assertIn("<!doctype html>", render(report, "html").lower())

    def test_html_is_self_contained(self):
        html = render(_report("gcc", {"a": Status.OK}), "html")
        for forbidden in ("http://", "https://", "src=\"//"):
            self.assertNotIn(forbidden, html.replace(
                "https://github.com/taonik/lazy-bootstrap", ""))


class TestComparison(unittest.TestCase):
    def setUp(self):
        self.base = _report("gcc", {"a": Status.OK, "b": Status.OK, "c": Status.FAILED})
        self.other = _report("filc", {"a": Status.OK, "b": Status.FAILED, "c": Status.OK})

    def test_transitions(self):
        comparison = compare_mod.compare([self.base, self.other], baseline="gcc")
        transitions = comparison.transitions("filc")
        self.assertEqual(transitions[compare_mod.REGRESSION], ["b"])
        self.assertEqual(transitions[compare_mod.FIX], ["c"])
        self.assertEqual(transitions[compare_mod.STABLE_OK], ["a"])

    def test_regressions_shortcut(self):
        comparison = compare_mod.compare([self.base, self.other], baseline="gcc")
        self.assertEqual(comparison.regressions("filc"), ["b"])

    def test_missing_package_is_gone_not_a_regression(self):
        partial = _report("clang", {"a": Status.OK})
        comparison = compare_mod.compare([self.base, partial], baseline="gcc")
        self.assertEqual(comparison.transitions("clang")[compare_mod.GONE], ["b", "c"])

    def test_all_comparison_formats_render(self):
        comparison = compare_mod.compare([self.base, self.other], baseline="gcc")
        for fmt in ("text", "md", "json", "html"):
            self.assertTrue(render_comparison(comparison, fmt).strip())

    def test_duplicate_labels_are_disambiguated(self):
        comparison = compare_mod.compare([_report("gcc", {"a": Status.OK}),
                                          _report("gcc", {"a": Status.FAILED})])
        self.assertEqual(len(set(comparison.labels)), 2)


# --- failure classification -------------------------------------------------


class TestClassification(unittest.TestCase):
    def test_network_failures_are_blocked_not_failed(self):
        from lazybootstrap.engine import _classify_fetch_failure

        self.assertEqual(
            _classify_fetch_failure("E: Failed to fetch http://deb.debian.org 403  Forbidden"),
            Status.BLOCKED)
        self.assertEqual(
            _classify_fetch_failure("Host not in allowlist: deb.debian.org"), Status.BLOCKED)

    def test_missing_source_is_its_own_status(self):
        from lazybootstrap.engine import _classify_fetch_failure

        self.assertEqual(
            _classify_fetch_failure("E: Unable to find a source package for wat"),
            Status.NOSOURCE)
        self.assertEqual(_classify_fetch_failure("no APKBUILD for wat in aports"),
                         Status.NOSOURCE)

    def test_anything_else_is_a_real_failure(self):
        from lazybootstrap.engine import _classify_fetch_failure

        self.assertEqual(_classify_fetch_failure("error: too few arguments"), Status.FAILED)


# --- flavours ---------------------------------------------------------------


class TestFlavours(unittest.TestCase):
    def test_ci_flavours_parse_and_cover_both_distros(self):
        from lazybootstrap import worker

        flavours = worker.load_flavours()
        self.assertTrue(flavours)
        self.assertEqual({f.distro for f in flavours}, {"debian", "alpine"})
        # Exactly one default per distro: the baseline a comparison hangs off.
        for distro in ("debian", "alpine"):
            defaults = [f for f in flavours if f.distro == distro and f.default]
            self.assertEqual(len(defaults), 1, f"{distro} needs exactly one default")

    def test_save_targets(self):
        from lazybootstrap.worker import SaveTarget, WorkerError

        self.assertEqual(SaveTarget.parse("image:x:y").kind, "image")
        self.assertEqual(SaveTarget.parse("dir:/tmp/x").value, "/tmp/x")
        self.assertEqual(SaveTarget.parse("").kind, "")
        with self.assertRaises(WorkerError):
            SaveTarget.parse("floppy:/dev/fd0")


# --- the orchestration seam -------------------------------------------------


class TestOrchestrationIsIndependent(unittest.TestCase):
    """The package must stay liftable into a repository of its own (D-27)."""

    def test_it_never_imports_the_domain(self):
        root = Path(__file__).resolve().parents[1] / "src" / "lazybootstrap"
        offenders = []
        domain = {"engine", "distros", "toolchains", "planner", "report", "worker",
                  "model", "config", "sysdeps", "net"}
        for path in (root / "orchestration").rglob("*.py"):
            for line in path.read_text().splitlines():
                line = line.strip()
                if not line.startswith(("import ", "from ")):
                    continue
                for name in domain:
                    if f"from ..{name}" in line or f"import ..{name}" in line \
                            or f"lazybootstrap.{name}" in line:
                        offenders.append(f"{path.name}: {line}")
        self.assertEqual(offenders, [], "orchestration must not depend on the domain")

    def test_public_surface_is_stable(self):
        from lazybootstrap import orchestration

        for name in ("Orchestrator", "EnvironmentRequest", "EnvironmentHandle",
                     "Availability", "OrchestrationError", "AUTO", "REQUIRE"):
            self.assertTrue(hasattr(orchestration, name), f"missing {name}")


class TestAcquisitionPolicy(unittest.TestCase):
    def _request(self, **kw):
        return envspec.EnvironmentRequest(**kw)

    def test_host_is_always_satisfied(self):
        state = Orchestrator().available(self._request(backend="host"))
        self.assertTrue(state.satisfied)
        self.assertEqual(state.action, envspec.NONE)

    def test_require_forbids_pulling(self):
        request = self._request(backend="podman", image="example.invalid/nope:1",
                                acquire=envspec.REQUIRE)
        state = Orchestrator().available(request)
        self.assertFalse(state.satisfied)
        self.assertFalse(state.allowed)
        self.assertFalse(state.ok)
        self.assertIn("forbids", state.summary())

    def test_auto_allows_pulling(self):
        request = self._request(backend="podman", image="example.invalid/nope:1",
                                acquire=envspec.AUTO)
        state = Orchestrator().available(request)
        self.assertTrue(state.allowed)
        self.assertTrue(state.ok)
        self.assertEqual(state.action, envspec.PULL)

    def test_unknown_backend_is_unavailable_not_a_crash(self):
        state = Orchestrator().available(self._request(backend="teleporter"))
        self.assertEqual(state.action, envspec.UNAVAILABLE)
        self.assertFalse(state.ok)

    def test_dir_rootfs_without_prepare_is_unavailable(self):
        request = self._request(backend="chroot", rootfs="dir:/nonexistent-lb")
        self.assertEqual(Orchestrator().available(request).action, envspec.UNAVAILABLE)

    def test_dir_rootfs_with_prepare_can_be_materialised(self):
        request = self._request(backend="chroot", rootfs="dir:/nonexistent-lb",
                                rootfs_prepare="true")
        state = Orchestrator().available(request)
        self.assertEqual(state.action, envspec.MATERIALISE)
        self.assertTrue(state.allowed)

    def test_availability_is_side_effect_free(self):
        target = Path("/nonexistent-lazy-bootstrap-should-not-appear")
        request = self._request(backend="chroot", rootfs="hostfs:copy",
                                rootfs_path=str(target))
        Orchestrator().available(request)
        self.assertFalse(target.exists())


# --- the VM class -----------------------------------------------------------


class TestVmDrivers(unittest.TestCase):
    def setUp(self):
        from lazybootstrap.orchestration.executors import vm

        self.vm = vm

    def test_engine_falls_back_to_the_default_driver(self):
        # --engine is shared with the container backends, where it means podman.
        self.assertEqual(self.vm.resolve_driver_name("podman"), "qemu")
        self.assertEqual(self.vm.resolve_driver_name(""), "qemu")
        self.assertEqual(self.vm.resolve_driver_name("qemu"), "qemu")

    def test_unimplemented_drivers_say_so_distinctly(self):
        from lazybootstrap.orchestration.executors.base import ExecutorError

        with self.assertRaises(ExecutorError) as caught:
            self.vm.get_driver("libvirt")
        self.assertIn("not implemented", str(caught.exception))

        with self.assertRaises(ExecutorError) as caught:
            self.vm.get_driver("hyperv")
        self.assertIn("unknown VM driver", str(caught.exception))

    def test_probe_reports_every_known_driver(self):
        report = self.vm.probe()
        self.assertEqual(set(report), set(self.vm.KNOWN_DRIVERS))
        for info in report.values():
            self.assertIn(info["available"], ("yes", "no"))

    def test_accel_choice(self):
        driver = self.vm.QemuDriver()
        self.assertEqual(driver.accel_flag(self.vm.VmSpec(accel="tcg")), ["-accel", "tcg"])
        self.assertEqual(driver.accel_flag(self.vm.VmSpec(accel="kvm")), ["-accel", "kvm"])
        # auto follows the host: exactly one of the two, never a crash.
        self.assertIn(driver.accel_flag(self.vm.VmSpec())[1], ("kvm", "tcg"))

    def test_disk_format_is_guessed_when_qemu_img_cannot_tell(self):
        self.assertEqual(self.vm._disk_format(self.vm.VmSpec(image="/x/d.raw")), "raw")
        self.assertEqual(self.vm._disk_format(self.vm.VmSpec(image="/x/d.qcow2")), "qcow2")
        self.assertEqual(
            self.vm._disk_format(self.vm.VmSpec(image="/x/d.qcow2", disk_format="raw")), "raw")

    # These two must hold whether or not this machine has qemu: without it the
    # orchestrator reports the missing hypervisor, which is the first problem
    # it meets and an equally correct answer. Asserting the later message made
    # the test pass only on machines like the one it was written on.
    def _vm_unavailable(self, request):
        state = Orchestrator().available(request)
        self.assertFalse(state.satisfied)
        self.assertEqual(state.action, envspec.UNAVAILABLE)
        self.assertTrue(state.detail.strip(), "an unavailable request must say why")
        return state

    def test_a_container_reference_is_not_a_bootable_disk(self):
        state = self._vm_unavailable(
            envspec.EnvironmentRequest(backend="vm", image="debian:13-slim"))
        if self.vm.QemuDriver().available()[0]:
            self.assertIn("not a file on this machine", state.detail)

    def test_a_vm_without_anything_bootable_is_unavailable(self):
        state = self._vm_unavailable(envspec.EnvironmentRequest(backend="vm"))
        if self.vm.QemuDriver().available()[0]:
            self.assertIn("disk image", state.detail)


# --- utilities --------------------------------------------------------------


class TestUtil(unittest.TestCase):
    def test_slugify(self):
        self.assertEqual(util.slugify("docker.io/library/debian:13-slim"),
                         "docker.io-library-debian-13-slim")
        self.assertEqual(util.slugify("!!!"), "x")

    def test_tail_keeps_the_end(self):
        text = "\n".join(str(i) for i in range(1000))
        self.assertTrue(util.tail(text, 100).endswith("999"))

    def test_atomic_write_leaves_no_partial_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a" / "b.json"
            util.write_json_atomic(path, {"x": 1})
            self.assertEqual(json.loads(path.read_text())["x"], 1)
            self.assertEqual(list(path.parent.glob(".*tmp")), [])

    def test_human_seconds(self):
        self.assertEqual(util.human_seconds(5), "5.0s")
        self.assertEqual(util.human_seconds(125), "2m05s")
        self.assertEqual(util.human_seconds(7325), "2h02m")


if __name__ == "__main__":
    unittest.main(verbosity=2)
