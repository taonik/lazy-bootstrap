"""chroot backend: the classic, no sandbox tooling required.

Weaker isolation than bubblewrap (no namespaces: the build shares the host's
PID table, network and users) but it needs nothing beyond root and `chroot(8)`,
which makes it the fallback that works on the oldest and most locked-down
machines. The API filesystems are mounted on entry and unmounted on exit, so a
build that expects /proc behaves normally.

The rootfs itself comes from rootfs.RootfsProvider, so a chroot can be an
unpacked OCI image, a copy-on-write view of the host, or a directory somebody
else prepared (docs/SPECS.md D-25).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Mapping

from .. import util
from ..logs import get
from .base import (Executor, ExecutorError, ensure_resolver, env_prefix,
                   is_root, require)

log = get("chroot")

#: (source, target, type, options) - mounted inside the rootfs before use.
API_MOUNTS = [
    ("proc", "proc", "proc", ""),
    ("sysfs", "sys", "sysfs", "ro"),
    ("/dev", "dev", "", "rbind"),
    # 1777: apt drops privileges to `_apt` and must be able to write to /tmp.
    ("tmpfs", "tmp", "tmpfs", "mode=1777"),
]


class ChrootExecutor(Executor):
    kind = "chroot"

    def _start(self) -> None:
        require("chroot", "coreutils")
        if not is_root():
            raise ExecutorError("the chroot backend needs root; use bwrap for an unprivileged run")
        if not self.spec.rootfs:
            raise ExecutorError("chroot backend needs a rootfs directory (spec.rootfs)")
        root = Path(self.spec.rootfs)
        if not root.is_dir():
            raise ExecutorError(f"rootfs not found: {root}")

        self._mounted: list[str] = []
        self._undo: list[list[str]] = []
        for source, target, fstype, options in API_MOUNTS:
            mountpoint = util.ensure_dir(root / target)
            argv = ["mount"]
            if options == "rbind":
                argv += ["--rbind", source, str(mountpoint)]
            else:
                argv += ["-t", fstype, source, str(mountpoint)]
                if options:
                    argv += ["-o", options]
            result = subprocess.run(argv, capture_output=True, text=True)
            if result.returncode == 0:
                self._mounted.append(str(mountpoint))
            else:
                log.debug("could not mount %s: %s", target, result.stderr.strip())

        util.ensure_dir(root / self.spec.workdir.lstrip("/"))
        for host_path, target in self.spec.binds.items():
            mountpoint = util.ensure_dir(root / target.lstrip("/"))
            util.ensure_dir(host_path)
            subprocess.run(["mount", "--bind", str(Path(host_path).resolve()), str(mountpoint)],
                           capture_output=True, text=True)
            self._mounted.append(str(mountpoint))

        # An image's /etc/resolv.conf is a placeholder the engine would replace;
        # in a chroot nobody does, so DNS is dead until we bind the host's.
        for undo in ensure_resolver(root):
            self._undo.append(undo)

    def _close(self) -> None:
        for argv in reversed(getattr(self, "_undo", [])):
            subprocess.run(argv, capture_output=True, text=True)
        self._undo = []
        # Lazy unmount, deepest first: something in the build may still hold a
        # reference, and a hung unmount is worse than a deferred one.
        for mountpoint in sorted(getattr(self, "_mounted", []), key=len, reverse=True):
            subprocess.run(["umount", "-l", mountpoint], capture_output=True, text=True)
        self._mounted = []

    def _wrap(self, script: str, cwd: str | None, env: Mapping[str, str]) -> list[str]:
        root = str(Path(self.spec.rootfs).resolve())
        prologue = f"cd {util.shell_join([cwd or self.spec.workdir])} 2>/dev/null || true\n"
        return ["chroot", root, *env_prefix(env), "/bin/sh", "-c", prologue + script]

    # -- file transfer: the rootfs is a directory on this machine -----------

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
