"""Toolchain interface and the compiler shim (docs/SPECS.md D-11, D-12).

A toolchain is provisioned into an environment and then exposes an environment
dict. The important part is that the dict does *not* rely on build systems
honouring `CC`: it puts a directory of wrapper scripts first in `PATH`, so even
a hand-written Makefile calling `gcc` ends up in the requested compiler.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from ..config import ToolchainConfig
from ..orchestration.executors.base import Executor
from ..orchestration.logs import get
from ..model import ToolchainReport

log = get("toolchain")

SHIM_DIR = "/opt/lazy-bootstrap/shim"

#: Compiler names a Debian/Alpine build may invoke. Every one of them is
#: shimmed, including the GNU triplet forms that autotools like to use.
C_ALIASES = ["cc", "gcc", "clang", "x86_64-linux-gnu-gcc", "x86_64-alpine-linux-musl-gcc"]
CXX_ALIASES = ["c++", "g++", "clang++", "x86_64-linux-gnu-g++", "x86_64-alpine-linux-musl-g++"]


class ToolchainError(RuntimeError):
    pass


#: Substrings that mean "the network refused", not "this does not exist".
#: Shared with engine._classify_fetch_failure so both halves agree (D-20).
NETWORK_MARKERS = (
    "Host not in allowlist",
    "Temporary failure resolving",
    "Could not resolve host",
    "Connection refused",
    "403  Forbidden",
    "Failed to fetch",
    "Unable to connect",
    "network is unreachable",
    "Could not connect",
    "ERROR: unable to select packages",
    "is not signed",
)


def looks_blocked(output: str) -> bool:
    return any(marker in output for marker in NETWORK_MARKERS)


def provisioning_hint(output: str, distro_id: str) -> str:
    """Turn a package-manager failure into something a human can act on."""
    if not looks_blocked(output):
        return ""
    archive = ("dl-cdn.alpinelinux.org" if distro_id == "alpine"
               else "deb.debian.org / the image's apt archive")
    return (f"the package archive is unreachable ({archive}). This is a network "
            "problem, not a missing package: allow the host, or point the run at a "
            "reachable one with --source-mirror / --registry-mirror")


@dataclass
class Install:
    """Result of provisioning: where the compiler is and how to use it."""

    toolchain_id: str
    kind: str
    source: str                 # distro | binary | source | preinstalled
    cc: str = ""                # absolute path inside the environment
    cxx: str = ""
    version: str = ""
    prefix: str = ""            # installation root, if any
    env: dict[str, str] = field(default_factory=dict)
    cflags: list[str] = field(default_factory=list)
    cxxflags: list[str] = field(default_factory=list)
    ldflags: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    #: extra flags to drop, discovered while provisioning (e.g. a missing plugin)
    notes_drop: list[str] = field(default_factory=list)
    #: directories holding a runtime the distro does not package (Fil-C's libpizlo,
    #: an upstream libc++, ...). Drives the dpkg-shlibdeps shim (D-23).
    runtime_libdirs: list[str] = field(default_factory=list)
    #: DEB_BUILD_OPTIONS keywords this toolchain needs, e.g. "nostrip" because
    #: dh_dwz cannot read clang's DWARF (D-23).
    deb_build_options: list[str] = field(default_factory=list)
    shimmed: bool = False


class Toolchain(ABC):
    kind: str = "abstract"

    def __init__(self, config: ToolchainConfig) -> None:
        self.config = config

    @property
    def id(self) -> str:
        return self.config.id

    # -- the driver contract ------------------------------------------------

    #: component name in ci/system-deps/<family>/<component>.txt
    sysdeps_component: str = ""

    @abstractmethod
    def provision(self, executor: Executor, distro_id: str, facts: dict[str, str],
                  fetcher, unit: str = "", sysdeps=None) -> Install:
        """Make the compiler available inside `executor`. Raise ToolchainError
        when no configured strategy works."""

    def ensure_sysdeps(self, sysdeps) -> list[str]:
        """Install this toolchain's declared system dependencies (D-24).

        Declared once in ci/system-deps/, so the on-the-fly environment and the
        CI image flavour never drift apart."""
        if sysdeps is None:
            return []
        ok, detail = sysdeps.ensure(self.sysdeps_component or self.kind)
        return [] if ok and not detail else [detail]

    # -- shared behaviour ---------------------------------------------------

    #: Flags this toolchain cannot honour, dropped by the shim unless the run
    #: overrides `drop_flags`. Overridden per driver.
    default_drop_flags: list[str] = []

    #: DEB_BUILD_OPTIONS this toolchain needs on the Debian family, because a
    #: packaging step - not a compiler step - cannot cope with its output.
    default_deb_build_options: list[str] = []

    def drop_flags(self, install: Install) -> list[str]:
        configured = self.config.drop_flags
        if configured == ["-"]:          # explicit "drop nothing"
            return []
        return configured or [*self.default_drop_flags, *install.notes_drop]

    def install_shim(self, executor: Executor, install: Install, unit: str = "") -> None:
        """Write wrapper scripts so PATH-based compiler lookups are captured."""
        extra_c = " ".join(install.cflags + self.config.cflags)
        extra_cxx = " ".join(install.cxxflags + self.config.cxxflags)
        extra_ld = " ".join(install.ldflags + self.config.ldflags)
        dropped = self.drop_flags(install)

        executor.mkdir(f"{SHIM_DIR}/bin")
        for name in C_ALIASES:
            executor.write_text(f"{SHIM_DIR}/bin/{name}",
                                _shim_body(install.cc, extra_c, extra_ld, dropped), mode="0755")
        for name in CXX_ALIASES:
            executor.write_text(f"{SHIM_DIR}/bin/{name}",
                                _shim_body(install.cxx or install.cc, extra_cxx, extra_ld, dropped),
                                mode="0755")
        if install.runtime_libdirs:
            executor.write_text(f"{SHIM_DIR}/bin/dpkg-shlibdeps",
                                _shlibdeps_body(install.runtime_libdirs), mode="0755")
            install.notes.append(
                "dpkg-shlibdeps shimmed with -l " + " -l ".join(install.runtime_libdirs)
                + " --ignore-missing-info (this toolchain ships its own runtime, D-23)")

        install.shimmed = True
        if dropped:
            install.notes.append("shim drops: " + " ".join(dropped))
        log.debug("shim installed for %s -> %s (dropping %s)", self.id, install.cc,
                  " ".join(dropped) or "nothing")

    def environment(self, install: Install) -> dict[str, str]:
        """Environment handed to every build step of this toolchain."""
        env = {
            "CC": f"{SHIM_DIR}/bin/cc" if install.shimmed else install.cc,
            "CXX": f"{SHIM_DIR}/bin/c++" if install.shimmed else (install.cxx or install.cc),
            "LB_TOOLCHAIN": self.id,
        }
        if install.shimmed:
            # The shim must win over /usr/bin, hence the prefix.
            env["PATH"] = f"{SHIM_DIR}/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
        cflags = " ".join(install.cflags + self.config.cflags)
        cxxflags = " ".join(install.cxxflags + self.config.cxxflags)
        ldflags = " ".join(install.ldflags + self.config.ldflags)
        if cflags:
            # DEB_*_APPEND is belt-and-braces next to the shim: harmless on
            # Alpine, and it reaches packages that use dpkg-buildflags directly.
            env["DEB_CFLAGS_APPEND"] = cflags
        if cxxflags:
            env["DEB_CXXFLAGS_APPEND"] = cxxflags
        if ldflags:
            env["DEB_LDFLAGS_APPEND"] = ldflags
        options = [*install.deb_build_options, *self.default_deb_build_options]
        if options:
            existing = install.env.get("DEB_BUILD_OPTIONS", "").split()
            env["DEB_BUILD_OPTIONS"] = " ".join(dict.fromkeys([*existing, *options]))
        env.update({k: v for k, v in install.env.items() if k != "DEB_BUILD_OPTIONS"})
        env.update(self.config.env)
        return env

    def probe(self, executor: Executor, install: Install, unit: str = "",
              workdir: str = "/build") -> tuple[bool, str]:
        """Compile and run a hello-world with this toolchain.

        This is the gate that decides whether a toolchain is usable at all,
        before hundreds of packages are attempted with it.
        """
        env = self.environment(install)
        # Under the workdir, not /tmp: the workdir is the one location every
        # backend guarantees to be writable and persistent across steps.
        scratch = f"{workdir.rstrip('/')}/.lazy-bootstrap"
        source = f"{scratch}/probe.c"
        binary = f"{scratch}/probe"
        executor.write_text(source, _PROBE_SOURCE)
        result = executor.run(
            f'set -e\n"$CC" -O1 -o {binary} {source}\n{binary}\n',
            title=f"probe {self.id}", env=env, timeout=600, unit=unit, step_prefix="probe",
        )
        detail = result.output.strip()
        if not result.ok:
            return False, detail[-1500:]
        version = executor.run('"$CC" --version 2>&1 | head -n 2', title="compiler version",
                               env=env, unit=unit, step_prefix="probe").stdout.strip()
        return True, version or detail

    def report(self, install: Install, ok: bool, detail: str) -> ToolchainReport:
        return ToolchainReport(
            id=self.id,
            kind=self.kind,
            version=install.version,
            source=install.source,
            cc=install.cc,
            cxx=install.cxx,
            ok=ok,
            detail="\n".join([*install.notes, detail]).strip(),
        )


# --- shim body --------------------------------------------------------------


def unpack_tarball(executor: Executor, host_tarball, prefix: str, strip: int = 1,
                   unit: str = "") -> None:
    """Install a .tar.xz into `prefix` inside the environment.

    `xz` is a *declared* dependency of the llvm and filc flavours
    (ci/system-deps/<family>/{llvm,filc}.txt), so normally it is already there.
    The host-side path below is a fallback for the cases where it cannot be:
    `--system-deps off`, a target with no package archive reachable, or a
    prebuilt image that dropped it. It is logged, never silent (D-24).
    """
    from pathlib import Path
    import subprocess

    host_tarball = Path(host_tarball)
    executor.mkdir(prefix)

    if executor.which("xz"):
        remote = f"/tmp/{host_tarball.name}"
        executor.upload(host_tarball, remote)
        result = executor.run(
            f"set -e\ncd {prefix}\ntar -xJf {remote} --strip-components={strip}\nrm -f {remote}\nls",
            title=f"unpack {host_tarball.name}", timeout=3600, unit=unit, step_prefix="toolchain")
        result.check(f"unpack {host_tarball.name}")
        return

    plain = host_tarball.with_suffix("")           # foo.tar.xz -> foo.tar
    if not plain.exists():
        log.warning("target has no xz although it is a declared system dependency; "
                    "decompressing %s on the host instead", host_tarball.name)
        with open(plain, "wb") as out:
            subprocess.run(["xz", "-dc", str(host_tarball)], stdout=out, check=True)
    remote = f"/tmp/{plain.name}"
    executor.upload(plain, remote)
    result = executor.run(
        f"set -e\ncd {prefix}\ntar -xf {remote} --strip-components={strip}\nrm -f {remote}\nls",
        title=f"unpack {plain.name}", timeout=3600, unit=unit, step_prefix="toolchain")
    result.check(f"unpack {plain.name}")


def _shim_body(compiler: str, extra_flags: str, extra_ldflags: str,
               drop: list[str] | None = None) -> str:
    """A wrapper that is readable when someone cats it during a debug session.

    LB_SHIM_TRACE=1 makes every compiler invocation echo itself (and every
    dropped flag), which is the in-target half of the tracing story (D-17).
    """
    drop = drop or []
    if not drop:
        filter_block = ""
    else:
        patterns = " | ".join(drop)
        # Rotate the positional parameters, keeping only the ones we want: the
        # portable way to filter "$@" in a shell without arrays.
        filter_block = f"""
# Flags this toolchain cannot honour (see docs/SPECS.md D-22).
count=$#
while [ "$count" -gt 0 ]; do
    arg="$1"; shift; count=$((count - 1))
    case "$arg" in
        {patterns})
            [ "${{LB_SHIM_TRACE:-0}}" != 0 ] && printf '[shim] dropped %s\\n' "$arg" >&2
            continue
            ;;
    esac
    set -- "$@" "$arg"
done
"""
    return f"""#!/bin/sh
# lazy-bootstrap compiler shim -> {compiler}
# Extra CFLAGS : {extra_flags or '(none)'}
# Extra LDFLAGS: {extra_ldflags or '(none)'}
# Dropped flags: {' '.join(drop) or '(none)'}
if [ "${{LB_SHIM_TRACE:-0}}" != 0 ]; then
    printf '[shim] %s %s\\n' "$0" "$*" >&2
fi
{filter_block}
exec {compiler} {extra_flags} {extra_ldflags} "$@"
"""


def _shlibdeps_body(libdirs: list[str]) -> str:
    """Teach Debian's dependency scanner about a runtime it does not package.

    A toolchain like Fil-C links against libraries (libpizlo, its own libc) that
    belong to no .deb, so `dpkg-shlibdeps` cannot resolve them and the build dies
    *after* a successful compile and link. Pointing it at the runtime directory
    and accepting missing dependency info keeps the failure signal where it
    belongs: on compilation, not on packaging metadata (D-23).
    """
    flags = " ".join(f"-l{d}" for d in libdirs)
    return f"""#!/bin/sh
# lazy-bootstrap dpkg-shlibdeps shim: this toolchain ships its own runtime.
real=/usr/bin/dpkg-shlibdeps
[ -x "$real" ] || real=$(command -v -- dpkg-shlibdeps 2>/dev/null | grep -v lazy-bootstrap | head -n 1)
if [ "${{LB_SHIM_TRACE:-0}}" != 0 ]; then
    printf '[shim] dpkg-shlibdeps {flags} --ignore-missing-info %s\\n' "$*" >&2
fi
exec "$real" {flags} --ignore-missing-info "$@"
"""


_PROBE_SOURCE = """/* lazy-bootstrap toolchain probe: compile, link and run. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

int main(void) {
    char *buffer = malloc(32);
    if (!buffer) return 1;
    snprintf(buffer, 32, "lazy-bootstrap");
    if (strcmp(buffer, "lazy-bootstrap") != 0) return 2;
    printf("probe ok: %s\\n", buffer);
    free(buffer);
    return 0;
}
"""
