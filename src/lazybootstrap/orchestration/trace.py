"""Step tracing and manual replay (see docs/SPECS.md D-17/D-18).

Two jobs:

1. Print what every step does, at a level chosen by the user.
2. Write a *runnable* shell script per build unit, so the whole pipeline can be
   replayed by hand outside the tool.

Debug levels (``--debug`` repeated, or ``LB_DEBUG``):

    0  quiet          only the normal INFO log
    1  steps          step id, backend, rc, duration
    2  + payload      the shell body and the environment delta
    3  + full output  stdout/stderr of every step, untruncated
"""

from __future__ import annotations

import os
import shlex
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import util

_LOCK = threading.Lock()


def level_from_env(default: int = 0) -> int:
    raw = os.environ.get("LB_DEBUG", "")
    if raw.isdigit():
        return int(raw)
    return default


@dataclass
class Tracer:
    """Per-run tracer. Cheap when disabled: every method returns immediately."""

    level: int = 0
    replay_dir: Path | None = None
    stream: Any = sys.stderr
    _counter: int = field(default=0, repr=False)
    _open_scripts: set[str] = field(default_factory=set, repr=False)

    # -- lifecycle ----------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self.level > 0

    def next_id(self, prefix: str) -> str:
        with _LOCK:
            self._counter += 1
            return f"{prefix}#{self._counter:04d}"

    # -- step reporting -----------------------------------------------------

    def step(
        self,
        step_id: str,
        title: str,
        *,
        backend: str = "",
        wrapper: Sequence[str] | None = None,
        script: str = "",
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        unit: str = "",
    ) -> None:
        """Announce a step *before* it runs, and record it for replay."""
        if self.replay_dir is not None:
            self._append_replay(unit or "run", step_id, title, wrapper, script, cwd, env)
        if self.level < 1:
            return
        with _LOCK:
            self._write(f"\n\033[1;35m::\033[0m \033[1m{step_id}\033[0m {title}"
                        f"{f'  [{backend}]' if backend else ''}")
            if self.level >= 2:
                if cwd:
                    self._write(f"   cwd: {cwd}")
                if env:
                    for key in sorted(env):
                        self._write(f"   env: {key}={_short(env[key])}")
                if wrapper:
                    self._write(f"   run: {util.shell_join(list(wrapper))}")
                if script:
                    self._write("   script:")
                    self._write(util.indent(script.rstrip(), "     | "))

    def result(self, step_id: str, rc: int, seconds: float, output: str = "") -> None:
        """Announce the outcome of a step."""
        if self.level < 1:
            return
        with _LOCK:
            mark = "\033[0;32mok\033[0m" if rc == 0 else f"\033[0;31mrc={rc}\033[0m"
            self._write(f"   -> {mark} in {util.human_seconds(seconds)}   [{step_id}]")
            if output and (self.level >= 3 or (rc != 0 and self.level >= 1)):
                body = output if self.level >= 3 else util.tail(output, 2000)
                self._write(util.indent(body.rstrip(), "     > "))

    def note(self, message: str) -> None:
        if self.level < 1:
            return
        with _LOCK:
            self._write(f"   \033[2m# {message}\033[0m")

    # -- replay script ------------------------------------------------------

    def _append_replay(
        self,
        unit: str,
        step_id: str,
        title: str,
        wrapper: Sequence[str] | None,
        script: str,
        cwd: str | None,
        env: Mapping[str, str] | None,
    ) -> None:
        assert self.replay_dir is not None
        path = self.replay_dir / f"{util.slugify(unit)}.sh"
        with _LOCK:
            first = str(path) not in self._open_scripts
            util.ensure_dir(path.parent)
            with open(path, "a", encoding="utf-8") as fh:
                if first:
                    self._open_scripts.add(str(path))
                    fh.write(_REPLAY_HEADER.format(unit=unit))
                fh.write(f"\n# ---- {step_id}  {title} " + "-" * max(0, 56 - len(title)) + "\n")
                if cwd:
                    fh.write(f"#   cwd: {cwd}\n")
                for key in sorted(env or {}):
                    fh.write(f"#   env: {key}={env[key]}\n")
                if wrapper:
                    # The wrapper is what you paste in your own terminal.
                    fh.write("step " + util.shell_join(list(wrapper)) + "\n")
                if script:
                    fh.write("#   payload:\n")
                    fh.write(util.indent(script.rstrip(), "#     ") + "\n")
            if first:
                os.chmod(path, 0o755)

    # -- plumbing -----------------------------------------------------------

    def _write(self, text: str) -> None:
        print(text, file=self.stream, flush=True)


_REPLAY_HEADER = """#!/bin/sh
# Replay script for build unit: {unit}
#
# Generated by lazy-bootstrap. Every `step` line below is the exact command the
# tool ran, wrapper included, in order. Run this file to reproduce the unit, or
# copy single lines into your terminal to inspect one stage.
#
#   sh replay/<unit>.sh            run everything
#   LB_REPLAY_DRY=1 sh <file>      print the commands without running them
set -u

step() {{
    printf '\\n\\033[1;35m::\\033[0m %s\\n' "$*"
    if [ "${{LB_REPLAY_DRY:-0}}" = 1 ]; then return 0; fi
    "$@"
}}
"""


def _short(value: str, limit: int = 160) -> str:
    return value if len(value) <= limit else value[: limit - 3] + "..."


def quote(value: str) -> str:
    return shlex.quote(value)
