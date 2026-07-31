"""Host backend: run straight on the machine, no isolation.

Fast and transparent, which makes it the right choice for `doctor`, for probing
a toolchain and for debugging. It is *not* the default for rebuilds: installing
build-dependencies for hundreds of packages would rewrite the host (D-05).
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Mapping

from .. import util
from .base import Executor, env_prefix


class HostExecutor(Executor):
    kind = "host"

    def _start(self) -> None:
        util.ensure_dir(self.spec.workdir)

    def _wrap(self, script: str, cwd: str | None, env: Mapping[str, str]) -> list[str]:
        # `cd` inside the script rather than via subprocess(cwd=) so the wrapper
        # printed by --debug is a complete, pasteable command.
        prologue = f"cd {util.shell_join([cwd])} 2>/dev/null || true\n" if cwd else ""
        return [*env_prefix(env), "/bin/sh", "-c", prologue + script]

    def upload(self, host_path: str | Path, target_path: str) -> None:
        _copy(Path(host_path), Path(target_path))

    def download(self, target_path: str, host_path: str | Path) -> None:
        _copy(Path(target_path), Path(host_path))


def _copy(src: Path, dst: Path) -> None:
    util.ensure_dir(dst.parent)
    if src.is_dir():
        shutil.copytree(src, dst, dirs_exist_ok=True, symlinks=True)
    else:
        shutil.copy2(src, dst)
