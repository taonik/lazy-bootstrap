"""Fil-C: memory-safe C/C++ on top of clang 20.1.8.

Two release artefacts exist and they are *not* interchangeable (D-13):

    filc-<v>-linux-x86_64.tar.xz     "pizfix" - musl based, self-contained, no root
    optfil-<v>-linux-x86_64.tar.xz   "/opt/fil" - glibc 2.40 based, wants root

`variant = auto` picks by the libc of the target. Note that Alpine plus
`libc6-compat` is still musl - gcompat is a shim, not glibc - so an Alpine image
gets the pizfix build even when libc6-compat is installed.

Fil-C tracks its own version numbers, not LLVM's: 0.681 is clang 20.1.8.
"""

from __future__ import annotations

from ..orchestration.executors.base import Executor
from ..orchestration.logs import get
from ..net import DownloadError
from .base import Install, Toolchain, ToolchainError, unpack_tarball

log = get("tc.filc")

RELEASE_BASE = "https://github.com/pizlonator/fil-c/releases/download"
INSTALL_ROOT = "/opt/lazy-bootstrap/toolchains"
DEFAULT_VERSION = "0.681"

PIZFIX = "pizfix"     # musl, self-contained
OPTFIL = "optfil"     # glibc, /opt/fil


class FilcToolchain(Toolchain):
    kind = "filc"
    sysdeps_component = "filc"
    # The release tarballs ship no LTO plugin, so the distro default
    # `-flto=auto -ffat-lto-objects` makes every link fail (D-22).
    default_drop_flags = ["-flto", "-flto=*", "-ffat-lto-objects",
                          "-fno-fat-lto-objects", "-ffat-lto-objects=*"]
    # nolto for the same reason; nostrip because debugedit/dh_dwz do not
    # understand Fil-C's DWARF either (D-23).
    default_deb_build_options = ["nolto", "nostrip"]

    def provision(self, executor: Executor, distro_id: str, facts: dict[str, str],
                  fetcher, unit: str = "", sysdeps=None) -> Install:
        notes = self.ensure_sysdeps(sysdeps)
        errors: list[str] = []
        for strategy in self.config.provision:
            try:
                if strategy == "binary":
                    install = self._from_release(executor, distro_id, facts, fetcher, unit)
                elif strategy == "preinstalled":
                    install = self._preinstalled(executor, unit)
                elif strategy == "distro":
                    # No distro ships Fil-C today; try anyway so a private repo
                    # or a prepared CI image can satisfy it.
                    install = self._preinstalled(executor, unit)
                elif strategy == "source":
                    raise ToolchainError(
                        "building Fil-C from source means building LLVM; use provision=binary "
                        "or bake it into a CI image (ci/images/)"
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

    # -- strategies ---------------------------------------------------------

    def _from_release(self, executor: Executor, distro_id: str, facts: dict[str, str],
                      fetcher, unit: str) -> Install:
        if facts.get("arch", "x86_64") not in ("x86_64", "amd64"):
            raise ToolchainError(f"Fil-C is x86_64-only (target is {facts.get('arch')})")

        version = self.config.version or DEFAULT_VERSION
        variant = self.select_variant(facts.get("libc", "unknown"))
        asset = f"{variant}-{version}-linux-x86_64.tar.xz"
        url = self.config.url or f"{RELEASE_BASE}/v{version}/{asset}"

        download = fetcher.fetch(url)
        if variant == PIZFIX:
            # Self-contained: unpack the whole thing wherever we like.
            prefix = f"{INSTALL_ROOT}/{self.id}"
            unpack_tarball(executor, download.path, prefix, strip=1, unit=unit)
            return self._setup_pizfix(executor, prefix, version, variant, unit)
        return self._setup_optfil(executor, download.path, fetcher, version, variant, unit)

    def _setup_pizfix(self, executor: Executor, prefix: str, version: str,
                      variant: str, unit: str) -> Install:
        """Run the upstream setup.sh, or an equivalent that needs no patchelf.

        setup.sh does two things: rewrite the rpath of a few shared objects, and
        symlink the kernel headers into pizfix/os-include. Only the first needs
        patchelf, and LD_LIBRARY_PATH achieves the same result, so a target
        without patchelf is still fully usable.
        """
        notes: list[str] = []
        script = f"""
set -e
cd {prefix}
if command -v patchelf >/dev/null 2>&1; then
    sh setup.sh >/tmp/lb-filc-setup.log 2>&1 && echo "LB_SETUP=upstream" || echo "LB_SETUP=failed"
else
    echo "LB_SETUP=manual"
fi
# Kernel headers: needed whichever path we took above (setup.sh does this too,
# and re-doing it is harmless because the links are created only if missing).
mkdir -p pizfix/os-include
cd pizfix/os-include
[ -e linux ] || ln -sf /usr/include/linux linux 2>/dev/null || true
if [ -d /usr/include/x86_64-linux-gnu/asm ]; then
    [ -e asm ] || ln -sf /usr/include/x86_64-linux-gnu/asm asm
elif [ -d /usr/include/asm ]; then
    [ -e asm ] || ln -sf /usr/include/asm asm
fi
[ -e asm-generic ] || ln -sf /usr/include/asm-generic asm-generic 2>/dev/null || true
ls -l
"""
        result = executor.run(script, title="fil-c setup", timeout=600,
                              unit=unit, step_prefix="toolchain")
        mode = _grab(result.stdout, "LB_SETUP")
        if mode != "upstream":
            notes.append("setup.sh skipped (no patchelf): using LD_LIBRARY_PATH instead")
        if "/usr/include/linux" not in result.stdout and "linux ->" not in result.stdout:
            notes.append("kernel headers (/usr/include/linux) missing in the target: "
                         "install linux-headers / linux-libc-dev for full coverage")

        return Install(
            toolchain_id=self.id, kind=self.kind, source="binary",
            cc=f"{prefix}/build/bin/clang", cxx=f"{prefix}/build/bin/clang++",
            version=f"{version} ({variant}, musl, clang 20.1.8)", prefix=prefix,
            env={"LD_LIBRARY_PATH": f"{prefix}/pizfix/lib64:{prefix}/pizfix/lib",
                 "FILC_ROOT": prefix},
            runtime_libdirs=[f"{prefix}/pizfix/lib", f"{prefix}/pizfix/lib64"],
            notes=notes,
        )

    def _setup_optfil(self, executor: Executor, tarball, fetcher, version: str,
                      variant: str, unit: str) -> Install:
        """Install the /opt/fil distribution.

        The download is a wrapper: docs, a setup.sh and the real payload in
        `fil.tar.xz`. Upstream's setup.sh extracts that payload to /opt/fil and
        then offers to configure sshd. We only want the extraction, and doing it
        ourselves means the target needs neither `getopt` nor `xz` - both absent
        from slim images. setup.sh is still used when the target can run it, so
        upstream stays the source of truth wherever possible.
        """
        import subprocess
        from pathlib import Path

        from ..orchestration import util

        # Step 1 (host side): pull the inner payload out of the wrapper, once.
        staging = util.ensure_dir(Path(fetcher.cache_dir).parent / "filc" / f"optfil-{version}")
        inner_xz = staging / "fil.tar.xz"
        if not inner_xz.exists():
            log.info("extracting fil.tar.xz from the /opt/fil archive")
            subprocess.run(
                ["tar", "-xf", str(tarball), "-C", str(staging), "--strip-components=1"],
                check=True)
        setup_sh = staging / "setup.sh"

        # Step 2: choose the payload format the target can actually read.
        payload = inner_xz
        if not executor.which("xz"):
            plain = staging / "fil.tar"
            if not plain.exists():
                log.warning("target has no xz (declared in ci/system-deps/*/filc.txt); "
                            "decompressing fil.tar.xz on the host instead")
                with open(plain, "wb") as out:
                    subprocess.run(["xz", "-dc", str(inner_xz)], stdout=out, check=True)
            payload = plain

        notes: list[str] = []
        # Step 3: prefer upstream's installer when its own prerequisites are met.
        can_run_setup = (setup_sh.exists() and executor.which("getopt")
                         and executor.which("xz") and not executor.exists("/opt/fil"))
        if can_run_setup:
            executor.upload(setup_sh, "/tmp/optfil-setup.sh")
            executor.upload(inner_xz, "/tmp/fil.tar.xz")
            result = executor.run(
                "set -e\ncd /tmp\nsh optfil-setup.sh --unattended\nrm -f /tmp/fil.tar.xz",
                title="fil-c /opt/fil setup.sh", timeout=1800, unit=unit,
                step_prefix="toolchain")
            if not result.ok:
                notes.append("upstream setup.sh failed; falling back to a plain extraction")
                can_run_setup = False

        if not can_run_setup:
            executor.mkdir("/opt/fil")
            executor.upload(payload, f"/tmp/{payload.name}")
            flag = "-xJf" if payload.suffix == ".xz" else "-xf"
            executor.run(
                f"set -e\ncd /opt/fil\ntar {flag} /tmp/{payload.name} --strip-components=1\n"
                f"rm -f /tmp/{payload.name}\nls bin | head -n 5",
                title="extract /opt/fil", timeout=1800, unit=unit,
                step_prefix="toolchain").check("extract /opt/fil")
            notes.append("installed by extracting fil.tar.xz directly (setup.sh not used)")

        cc = executor.run(
            "for c in /opt/fil/bin/filcc /opt/fil/bin/clang; do "
            "[ -x \"$c\" ] && echo \"$c\" && break; done",
            title="locate /opt/fil driver", unit=unit, step_prefix="toolchain").stdout.strip()
        if not cc:
            raise ToolchainError("/opt/fil does not contain a filcc/clang driver")
        cxx = "/opt/fil/bin/fil++" if executor.exists("/opt/fil/bin/fil++") else cc
        return Install(
            toolchain_id=self.id, kind=self.kind, source="binary",
            cc=cc, cxx=cxx, version=f"{version} ({variant}, glibc 2.40, clang 20.1.8)",
            prefix="/opt/fil",
            env={"FILC_ROOT": "/opt/fil"},
            runtime_libdirs=["/opt/fil/lib", "/opt/fil/lib64"], notes=notes,
        )

    def _preinstalled(self, executor: Executor, unit: str) -> Install | None:
        result = executor.run(
            "for c in /opt/fil/bin/filcc $(command -v filcc 2>/dev/null); do "
            "[ -x \"$c\" ] && echo \"$c\" && break; done",
            title="locate fil-c", unit=unit, step_prefix="toolchain")
        cc = result.stdout.strip()
        if not cc:
            return None
        return Install(toolchain_id=self.id, kind=self.kind, source="preinstalled",
                       cc=cc, cxx=cc.replace("filcc", "fil++"),
                       version=self.config.version or "unknown")

    # -- variant selection --------------------------------------------------

    def select_variant(self, libc: str) -> str:
        """auto | pizfix | optfil, resolved against the target's libc."""
        requested = (self.config.variant or "auto").lower()
        if requested in (PIZFIX, "musl", "filc"):
            return PIZFIX
        if requested in (OPTFIL, "glibc", "opt"):
            return OPTFIL
        if requested != "auto":
            raise ToolchainError(f"unknown fil-c variant {requested!r}")
        if libc == "musl":
            return PIZFIX
        if libc == "glibc":
            return OPTFIL
        # Unknown libc: pizfix is self-contained, so it is the safe guess.
        log.warning("libc of the target is unknown; defaulting to the %s build", PIZFIX)
        return PIZFIX


def _grab(text: str, key: str) -> str:
    for line in text.splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip()
    return ""
