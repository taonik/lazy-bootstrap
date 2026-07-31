"""Bubblewrap backend: run inside a rootfs directory using user namespaces.

No daemon, no image store, no root strictly required - the lightest way to get a
real distro userland. The rootfs is a plain directory (see images.materialise),
so uploads/downloads are ordinary file copies, which makes this backend the
easiest one to reason about when something goes wrong.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Mapping

from .. import util
from .base import Executor, ExecutorError, ensure_resolver, env_prefix, require


class BubblewrapExecutor(Executor):
    kind = "bwrap"

    def _start(self) -> None:
        require("bwrap", "install bubblewrap")
        if not self.spec.rootfs:
            raise ExecutorError("bwrap backend needs a rootfs directory (spec.rootfs)")
        root = Path(self.spec.rootfs)
        if not root.is_dir():
            raise ExecutorError(f"rootfs not found: {root}")
        # Directories that must exist inside the rootfs for the binds to land.
        for mount in ("proc", "sys", "dev", self.spec.workdir.lstrip("/")):
            util.ensure_dir(root / mount)
        # /tmp gets the usual 1777 because apt drops privileges to `_apt` and
        # writes there. /run is deliberately left alone: firejail refuses to
        # chroot into a tree whose /run is world-writable, and the same rootfs
        # cache is shared between backends.
        util.ensure_dir(root / "tmp").chmod(0o1777)
        util.ensure_dir(root / "run").chmod(0o755)
        self._undo = ensure_resolver(root)
        for target in self.spec.binds.values():
            util.ensure_dir(root / target.lstrip("/"))

    def _close(self) -> None:
        import subprocess

        for argv in reversed(getattr(self, "_undo", [])):
            subprocess.run(argv, capture_output=True, text=True)
        self._undo = []

    def _wrap(self, script: str, cwd: str | None, env: Mapping[str, str]) -> list[str]:
        root = str(Path(self.spec.rootfs).resolve())
        argv = [
            "bwrap",
            "--bind", root, "/",
            "--proc", "/proc",
            "--dev", "/dev",
            # No --tmpfs for /tmp or /run: each run() is a separate bwrap
            # process, so a tmpfs would be empty again every step. The rootfs's
            # own directories are the session state, exactly as in a container.
            # Their permissions are set in _start (apt needs a writable /tmp).
            "--die-with-parent",
            "--unshare-pid",
            "--unshare-ipc",
            "--unshare-uts",
            "--hostname", self.label or "lazy-bootstrap",
        ]
        if not self.spec.network:
            argv += ["--unshare-net"]
        for host_path, target in self.spec.binds.items():
            argv += ["--bind", str(Path(host_path).resolve()), target]
        argv += ["--chdir", cwd or self.spec.workdir]
        argv += [*env_prefix(env), "/bin/sh", "-c", script]
        return argv

    # -- file transfer: the rootfs is just a directory ----------------------

    def _host_path(self, target_path: str) -> Path:
        # Honour binds first: a bound host directory is visible from both sides.
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
