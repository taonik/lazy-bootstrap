"""Toolchain registry."""

from __future__ import annotations

from ..config import ToolchainConfig
from .base import Install, Toolchain, ToolchainError
from .filc import FilcToolchain
from .gcc import GccToolchain
from .llvm import LlvmToolchain

KINDS: dict[str, type[Toolchain]] = {
    "gcc": GccToolchain,
    "llvm": LlvmToolchain,
    "clang": LlvmToolchain,
    "filc": FilcToolchain,
}


def get(config: ToolchainConfig) -> Toolchain:
    try:
        return KINDS[config.kind]( config)
    except KeyError:
        raise ToolchainError(
            f"unknown toolchain kind {config.kind!r} for {config.id!r}; "
            f"known: {', '.join(sorted(KINDS))}"
        ) from None


__all__ = ["Install", "KINDS", "Toolchain", "ToolchainError", "get"]
