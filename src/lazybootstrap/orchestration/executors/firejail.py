"""Firejail backend: same rootfs model as bubblewrap, different jailer.

Firejail is more opinionated than bwrap (profiles, seccomp presets) and its
`--chroot` mode is pickier - notably it refuses to run as root unless
`force-nonewprivs`/`root` are permitted by /etc/firejail/firejail.config. It is
kept as a first-class backend because it is what many desktops already have, but
`lazy-bootstrap doctor` is the authority on whether it actually works here.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Mapping

from .. import util
from .base import Executor, ExecutorError, ensure_resolver, env_prefix, require


class FirejailExecutor(Executor):
    kind = "firejail"

    def _start(self) -> None:
        require("firejail", "install firejail")
        if not self.spec.rootfs:
            raise ExecutorError("firejail backend needs a rootfs directory (spec.rootfs)")
        root = Path(self.spec.rootfs)
        if not root.is_dir():
            raise ExecutorError(f"rootfs not found: {root}")
        util.ensure_dir(root / self.spec.workdir.lstrip("/"))
        for target in self.spec.binds.values():
            util.ensure_dir(root / target.lstrip("/"))
        self._undo = ensure_resolver(root)

        # Fail here, with the fix, rather than on every build step.
        probe = subprocess.run(["firejail", "--quiet", "--noprofile", f"--chroot={root}", "true"],
                               capture_output=True, text=True)
        if probe.returncode != 0 and "chroot feature is disabled" in probe.stderr:
            raise ExecutorError(
                "firejail --chroot is disabled on this machine. Add a line reading\n"
                "    chroot yes\n"
                "to /etc/firejail/firejail.config, or use --backend bwrap / --backend chroot.")

    def _close(self) -> None:
        for argv in reversed(getattr(self, "_undo", [])):
            subprocess.run(argv, capture_output=True, text=True)
        self._undo = []

    def _wrap(self, script: str, cwd: str | None, env: Mapping[str, str]) -> list[str]:
        root = str(Path(self.spec.rootfs).resolve())
        argv = [
            "firejail",
            "--quiet",
            "--noprofile",
            f"--chroot={root}",
        ]
        # Note what is deliberately *not* here: --caps.drop=all and --nogroups.
        # A build installs packages, and apt drops privileges to `_apt` via
        # setgroups(2); without CAP_SETGID that call fails and every install
        # dies on "Permission denied" in /var/lib/apt/lists/partial. The chroot
        # plus the caller's own isolation is the boundary we rely on here; pass
        # extra flags through spec.env["LB_FIREJAIL_ARGS"] to tighten it.
        extra = self.spec.env.get("LB_FIREJAIL_ARGS", "")
        if extra:
            argv += extra.split()
        if not self.spec.network:
            argv += ["--net=none"]
        # firejail has no per-path bind in --chroot mode for arbitrary sources,
        # so binds are materialised as bind mounts before the jail starts.
        for host_path, target in self.spec.binds.items():
            argv += [f"--bind={Path(host_path).resolve()},{root.rstrip('/')}{target}"]
        prologue = f"cd {util.shell_join([cwd or self.spec.workdir])} 2>/dev/null || true\n"
        argv += [*env_prefix(env), "/bin/sh", "-c", prologue + script]
        return argv

    def _host_path(self, target_path: str) -> Path:
        for host_path, target in self.spec.binds.items():
            if target_path == target or target_path.startswith(target.rstrip("/") + "/"):
                return Path(host_path) / target_path[len(target):].lstrip("/")
        return Path(self.spec.rootfs) / target_path.lstrip("/")

    def upload(self, host_path: str | Path, target_path: str) -> None:
        _copy(Path(host_path), self._host_path(target_path))

    def download(self, target_path: str, host_path: str | Path) -> None:
        _copy(self._host_path(target_path), Path(host_path))


def _copy(src: Path, dst: Path) -> None:
    util.ensure_dir(dst.parent)
    if src.is_dir():
        shutil.copytree(src, dst, dirs_exist_ok=True, symlinks=True)
    else:
        shutil.copy2(src, dst)
