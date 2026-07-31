"""Clang/LLVM: distro package, upstream release tarball, or source build.

Version policy (D-12): if the run asks for an exact version (`llvm-20.1.8`) the
distro strategy only succeeds when the distro really has that version - a
"close enough" package is a silent lie in a toolchain comparison. When it does
not, the upstream release tarball is downloaded, and if that exact release has
no Linux binary the *nearest* one in the same series is used and recorded.
"""

from __future__ import annotations

import re

from ..executors.base import Executor
from ..logs import get
from ..net import DownloadError
from .base import Install, Toolchain, ToolchainError, unpack_tarball
from .gcc import _installer, _resolve, _version_of

log = get("tc.llvm")

RELEASE_BASE = "https://github.com/llvm/llvm-project/releases/download"
INSTALL_ROOT = "/opt/lazy-bootstrap/toolchains"

#: Asset naming changed at LLVM 20: before that, releases shipped
#: clang+llvm-<v>-x86_64-linux-gnu-ubuntu-<rel>.tar.xz; from 20 on, LLVM-<v>-Linux-X64.tar.xz.
_MODERN_ASSET = "LLVM-{version}-Linux-{arch}.tar.xz"
_LEGACY_ASSETS = [
    "clang+llvm-{version}-x86_64-linux-gnu-ubuntu-22.04.tar.xz",
    "clang+llvm-{version}-x86_64-linux-gnu-ubuntu-18.04.tar.xz",
    "clang+llvm-{version}-x86_64-linux-gnu-ubuntu-20.04.tar.xz",
]


class LlvmToolchain(Toolchain):
    kind = "llvm"
    sysdeps_component = "llvm"

    def provision(self, executor: Executor, distro_id: str, facts: dict[str, str],
                  fetcher, unit: str = "", sysdeps=None) -> Install:
        notes = self.ensure_sysdeps(sysdeps)
        errors: list[str] = []
        for strategy in self.config.provision:
            try:
                if strategy == "distro":
                    install = self._from_distro(executor, distro_id, unit)
                elif strategy == "binary":
                    install = self._from_release(executor, facts, fetcher, unit)
                elif strategy == "preinstalled":
                    install = self._preinstalled(executor, unit)
                elif strategy == "source":
                    raise ToolchainError(
                        "building LLVM from source takes hours; bake it into a CI image "
                        "(ci/images/) instead of doing it inside a run"
                    )
                else:
                    continue
            except (ToolchainError, DownloadError) as exc:
                errors.append(f"{strategy}: {exc}")
                install = None
            if install:
                install.notes.extend(notes)
                self.install_shim(executor, install, unit)
                return install
            errors.append(f"{strategy}: not available")
        raise ToolchainError(f"could not provision {self.id}\n  " + "\n  ".join(errors))

    # -- strategy: distro ---------------------------------------------------

    def _from_distro(self, executor: Executor, distro_id: str, unit: str) -> Install | None:
        want = self.config.version
        major = want.split(".")[0] if want else ""
        packages = self.config.packages or _clang_packages(distro_id, major)
        result = executor.run(_installer(distro_id, packages),
                              title=f"install {self.id} from distro",
                              env={"DEBIAN_FRONTEND": "noninteractive"},
                              timeout=1800, unit=unit, step_prefix="toolchain")
        if not result.ok:
            log.debug("distro clang install failed: %s", result.output.strip()[-300:])
            return None
        cc = _resolve(executor, [f"clang-{major}"] if major else [], "clang", unit=unit)
        if not cc:
            return None
        cxx = _resolve(executor, [f"clang++-{major}"] if major else [], "clang++", unit=unit)
        got = _version_of(executor, cc, unit)
        if want and not version_satisfies(want, got):
            raise ToolchainError(
                f"distro provides clang {got}, run asked for {want} "
                "(refusing to substitute; falling through to the release tarball)"
            )
        return Install(toolchain_id=self.id, kind=self.kind, source="distro",
                       cc=cc, cxx=cxx or cc, version=got)

    def _preinstalled(self, executor: Executor, unit: str) -> Install | None:
        major = self.config.version.split(".")[0] if self.config.version else ""
        cc = _resolve(executor, [f"clang-{major}"] if major else [], "clang", unit=unit)
        if not cc:
            return None
        cxx = _resolve(executor, [f"clang++-{major}"] if major else [], "clang++", unit=unit)
        got = _version_of(executor, cc, unit)
        if self.config.version and not version_satisfies(self.config.version, got):
            return None
        return Install(toolchain_id=self.id, kind=self.kind, source="preinstalled",
                       cc=cc, cxx=cxx or cc, version=got)

    # -- strategy: upstream release tarball ---------------------------------

    def _from_release(self, executor: Executor, facts: dict[str, str],
                      fetcher, unit: str) -> Install | None:
        version = self.config.version
        if not version:
            raise ToolchainError("provision=binary needs an explicit version (e.g. llvm-20.1.8)")

        url, resolved = self._resolve_asset(version, facts.get("arch", "x86_64"), fetcher)
        notes = []
        if resolved != version:
            notes.append(f"exact release {version} has no Linux binary; using {resolved}")
            log.warning("%s: %s", self.id, notes[-1])

        download = fetcher.fetch(url)
        prefix = f"{INSTALL_ROOT}/{self.id}"
        unpack_tarball(executor, download.path, prefix, strip=1, unit=unit)

        cc, cxx = f"{prefix}/bin/clang", f"{prefix}/bin/clang++"
        # Upstream binaries are glibc builds: on musl they simply will not run,
        # and saying so here is far clearer than a link error 200 packages later.
        if facts.get("libc") == "musl":
            notes.append("upstream LLVM binaries are glibc-linked; on musl they need gcompat "
                         "and may still fail - prefer the distro package on Alpine")
        return Install(toolchain_id=self.id, kind=self.kind, source="binary",
                       cc=cc, cxx=cxx, version=resolved, prefix=prefix,
                       env={"LD_LIBRARY_PATH": f"{prefix}/lib"},
                       runtime_libdirs=[f"{prefix}/lib"], notes=notes)

    def _resolve_asset(self, version: str, arch: str, fetcher) -> tuple[str, str]:
        """Find a downloadable asset for `version`, or the nearest one."""
        arch_tag = "X64" if arch in ("x86_64", "amd64") else "ARM64"
        for candidate in nearest_versions(version):
            for name in _asset_names(candidate, arch_tag):
                url = f"{RELEASE_BASE}/llvmorg-{candidate}/{name}"
                ok, code = fetcher.reachable(url)
                log.debug("probe %s -> %s", url, code)
                if ok:
                    return url, candidate
        raise ToolchainError(
            f"no LLVM Linux binary found for {version} or any nearby release "
            f"(checked {', '.join(nearest_versions(version))})"
        )


# --- version helpers --------------------------------------------------------


def _asset_names(version: str, arch_tag: str) -> list[str]:
    major = int(version.split(".")[0])
    if major >= 20:
        return [_MODERN_ASSET.format(version=version, arch=arch_tag)]
    return [name.format(version=version) for name in _LEGACY_ASSETS]


def nearest_versions(version: str, window: int = 12) -> list[str]:
    """Exact version first, then the same x.y series walking the patch level
    down and then up. Bounded on purpose: an unbounded search would hammer the
    release server for a version that simply does not exist."""
    match = re.match(r"^(\d+)(?:\.(\d+))?(?:\.(\d+))?$", version)
    if not match:
        return [version]
    major = int(match.group(1))
    minor = int(match.group(2) or 0)
    patch = match.group(3)
    if patch is None:
        # "20" or "20.1": ask for the series, newest patch first.
        return [f"{major}.{minor}.{p}" for p in range(window, -1, -1)]
    patch_num = int(patch)
    candidates = [version]
    for delta in range(1, window + 1):
        if patch_num - delta >= 0:
            candidates.append(f"{major}.{minor}.{patch_num - delta}")
        candidates.append(f"{major}.{minor}.{patch_num + delta}")
    return candidates


def version_satisfies(requested: str, actual: str) -> bool:
    """`20` matches 20.1.2; `20.1` matches 20.1.2; `20.1.8` matches only 20.1.8."""
    if not requested:
        return True
    want = [p for p in re.split(r"[.\-]", requested) if p.isdigit()]
    got = [p for p in re.split(r"[.\-]", actual) if p.isdigit()]
    if not want or not got:
        return False
    return got[: len(want)] == want


def _clang_packages(distro_id: str, major: str) -> list[str]:
    if distro_id == "alpine":
        return ["clang", "llvm", "lld", "compiler-rt", "musl-dev"]
    if major:
        return [f"clang-{major}", f"lld-{major}", f"llvm-{major}"]
    return ["clang", "lld", "llvm"]
