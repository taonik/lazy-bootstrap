"""Distro registry and auto-detection."""

from __future__ import annotations

from ..orchestration.executors.base import Executor
from .alpine import AlpineDistro
from .base import BuildContext, Distro, SourceTree
from .debian import DebianDistro

DISTROS: dict[str, type[Distro]] = {
    "debian": DebianDistro,
    "ubuntu": DebianDistro,     # same family, same driver (D-08)
    "alpine": AlpineDistro,
}


def get(name: str) -> Distro:
    try:
        return DISTROS[name]()
    except KeyError:
        raise ValueError(
            f"unknown distro {name!r}; known: {', '.join(sorted(DISTROS))}"
        ) from None


def detect(executor: Executor) -> Distro:
    """Ask each driver whether it recognises the environment. Alpine is probed
    first because a Debian image never contains /sbin/apk, while some apk-based
    images do carry a dpkg shim."""
    for cls in (AlpineDistro, DebianDistro):
        if cls.detect(executor):
            return cls()
    raise ValueError("could not detect the distribution (no dpkg, no apk)")


__all__ = ["BuildContext", "DISTROS", "Distro", "SourceTree", "detect", "get"]
