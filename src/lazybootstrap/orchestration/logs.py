"""Logging setup: one human-readable stream on stderr plus an optional
per-run file. Kept deliberately small - the reports are the real output."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

_COLORS = {
    "DEBUG": "\033[2;37m",
    "INFO": "\033[0;36m",
    "WARNING": "\033[0;33m",
    "ERROR": "\033[0;31m",
    "CRITICAL": "\033[1;31m",
}
_RESET = "\033[0m"


class _Formatter(logging.Formatter):
    def __init__(self, color: bool) -> None:
        super().__init__("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S")
        self.color = color

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        if not self.color:
            return text
        return f"{_COLORS.get(record.levelname, '')}{text}{_RESET}"


def setup(verbosity: int = 0, quiet: bool = False, logfile: str | Path | None = None) -> None:
    level = logging.WARNING if quiet else (logging.DEBUG if verbosity > 0 else logging.INFO)
    root = logging.getLogger("lazybootstrap")
    root.setLevel(logging.DEBUG)
    root.handlers.clear()

    stream = logging.StreamHandler(sys.stderr)
    stream.setLevel(level)
    color = sys.stderr.isatty() and os.environ.get("NO_COLOR") is None
    stream.setFormatter(_Formatter(color))
    root.addHandler(stream)

    # The file handler always records DEBUG so a failed run can be diagnosed
    # without re-running it with -v.
    if logfile:
        Path(logfile).parent.mkdir(parents=True, exist_ok=True)
        fileh = logging.FileHandler(logfile, encoding="utf-8")
        fileh.setLevel(logging.DEBUG)
        fileh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s %(message)s"))
        root.addHandler(fileh)


def get(name: str) -> logging.Logger:
    return logging.getLogger(f"lazybootstrap.{name}")
