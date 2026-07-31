"""GCC: the distro default, and the reference every other toolchain is compared to.

Provisioning is almost always `distro` - a distro's own gcc is the one its
packages are known to build with, which is exactly what makes it the baseline.
Building GCC from source is supported in principle but deliberately not wired to
a download here: it is a multi-hour job that belongs in a prepared CI image.
"""

from __future__ import annotations

from ..executors.base import Executor
from ..logs import get
from .base import Install, Toolchain, ToolchainError

log = get("tc.gcc")


class GccToolchain(Toolchain):
    kind = "gcc"
    sysdeps_component = "gcc"

    def provision(self, executor: Executor, distro_id: str, facts: dict[str, str],
                  fetcher, unit: str = "", sysdeps=None) -> Install:
        notes = self.ensure_sysdeps(sysdeps)
        version = self.config.version
        for strategy in self.config.provision:
            if strategy == "distro":
                install = self._from_distro(executor, distro_id, version, unit)
            elif strategy == "preinstalled":
                install = self._preinstalled(executor, version, unit)
            elif strategy == "source":
                raise ToolchainError(
                    "building gcc from source is not wired up; use a prepared CI image "
                    "(see ci/images/) or provision=distro"
                )
            else:
                continue
            if install:
                install.notes.extend(notes)
                self.install_shim(executor, install, unit)
                return install
        raise ToolchainError(
            f"could not provision {self.id} (tried: {', '.join(self.config.provision)})"
        )

    # -- strategies ---------------------------------------------------------

    def _from_distro(self, executor: Executor, distro_id: str, version: str,
                     unit: str) -> Install | None:
        packages = self.config.packages or _default_packages(distro_id, version)
        install_cmd = _installer(distro_id, packages)
        result = executor.run(install_cmd, title=f"install {self.id} from distro",
                              env={"DEBIAN_FRONTEND": "noninteractive"},
                              timeout=1800, unit=unit, step_prefix="toolchain")
        if not result.ok:
            log.warning("distro install of %s failed: %s", self.id,
                        result.output.strip()[-400:])
            return None
        cc = _resolve(executor, [f"gcc-{version}"] if version else [], "gcc", "cc", unit=unit)
        cxx = _resolve(executor, [f"g++-{version}"] if version else [], "g++", "c++", unit=unit)
        if not cc:
            return None
        return Install(
            toolchain_id=self.id, kind=self.kind, source="distro",
            cc=cc, cxx=cxx or cc, version=_version_of(executor, cc, unit),
        )

    def _preinstalled(self, executor: Executor, version: str, unit: str) -> Install | None:
        cc = _resolve(executor, [f"gcc-{version}"] if version else [], "gcc", "cc", unit=unit)
        if not cc:
            return None
        cxx = _resolve(executor, [f"g++-{version}"] if version else [], "g++", "c++", unit=unit)
        return Install(toolchain_id=self.id, kind=self.kind, source="preinstalled",
                       cc=cc, cxx=cxx or cc, version=_version_of(executor, cc, unit))


# --- helpers shared with the llvm driver ------------------------------------


def _default_packages(distro_id: str, version: str) -> list[str]:
    if distro_id == "alpine":
        return ["gcc", "g++", "musl-dev", "make"]
    if version:
        return [f"gcc-{version}", f"g++-{version}"]
    return ["gcc", "g++"]


def _installer(distro_id: str, packages: list[str]) -> str:
    joined = " ".join(packages)
    if distro_id == "alpine":
        return f"apk add --no-cache {joined}"
    # `|| true`: a single unreachable third-party repository must not stop us
    # installing from the ones that do work. If the packages really are missing,
    # the install below fails and that is the error worth reporting.
    return ("apt-get update -o Acquire::Retries=3 || echo '[lazy-bootstrap] apt-get update reported errors' >&2\n"
            f"apt-get install -y --no-install-recommends {joined}")


def _resolve(executor: Executor, preferred: list[str], *fallbacks: str, unit: str = "") -> str:
    """First name on PATH wins; the versioned name is tried before the generic one."""
    candidates = [c for c in [*preferred, *fallbacks] if c]
    probe = " ".join(f"command -v {c} 2>/dev/null;" for c in candidates)
    result = executor.run(f"{{ {probe} }} | head -n 1", title="resolve compiler",
                          unit=unit, step_prefix="toolchain")
    return result.stdout.strip().splitlines()[0].strip() if result.stdout.strip() else ""


def _version_of(executor: Executor, compiler: str, unit: str = "") -> str:
    result = executor.run(f"{compiler} -dumpversion 2>/dev/null || {compiler} --version | head -n 1",
                          title="compiler version", unit=unit, step_prefix="toolchain")
    return result.stdout.strip().splitlines()[0].strip() if result.stdout.strip() else ""
