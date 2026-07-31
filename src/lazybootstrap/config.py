"""Run configuration.

A run is fully described by a `RunConfig`. It can come from a TOML profile
(see ./profiles), from CLI flags, or from both - CLI flags always win, so a
profile is a set of defaults rather than a straitjacket.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

DEFAULT_CACHE = Path(os.environ.get("LAZY_BOOTSTRAP_CACHE", "/var/cache/lazy-bootstrap"))
DEFAULT_OUT = Path(os.environ.get("LAZY_BOOTSTRAP_OUT", "runs"))

# Registry mirrors let a locked-down network still pull public base images:
# `--registry-mirror mirror.gcr.io` rewrites docker.io/... to mirror.gcr.io/...
DEFAULT_REGISTRY_MIRRORS: dict[str, str] = {}


@dataclass
class ToolchainConfig:
    """One entry of the toolchain matrix.

    `id` is the display name and the report column ("gcc", "llvm-20.1.8", ...).
    `kind` selects the driver (gcc / llvm / filc).
    `provision` is an ordered list of strategies tried in turn:
    distro (package manager), binary (upstream release tarball), source (build it).
    """

    id: str
    kind: str = ""
    version: str = ""
    #: empty means "the driver default for this kind", filled in below
    provision: list[str] = field(default_factory=list)
    variant: str = "auto"          # fil-c: auto | pizfix (musl) | optfil (glibc)
    cflags: list[str] = field(default_factory=list)
    cxxflags: list[str] = field(default_factory=list)
    ldflags: list[str] = field(default_factory=list)
    #: Flags the shim removes from the command line before calling the compiler.
    #: Distro build flags are tuned for the distro's own gcc; an alternative
    #: toolchain often cannot honour all of them (D-22). Empty means "use the
    #: driver's defaults"; ["-"] (a lone dash) means "drop nothing".
    drop_flags: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    url: str = ""                  # explicit tarball override for `binary`
    packages: list[str] = field(default_factory=list)  # distro packages for `distro`

    def __post_init__(self) -> None:
        if not self.kind:
            self.kind = _infer_kind(self.id)
        if not self.version:
            self.version = _infer_version(self.id)
        if not self.provision:
            self.provision = list(DEFAULT_PROVISION.get(self.kind, ["distro", "binary"]))


#: Sensible provisioning order per toolchain kind (D-12).
#: gcc looks at what is already installed first - it is the distro's own
#: compiler, so reinstalling it is pointless and, on the host backend, rude.
#: No distro packages Fil-C, so it goes straight to the release tarball.
DEFAULT_PROVISION = {
    "gcc": ["preinstalled", "distro"],
    "llvm": ["distro", "binary"],
    "filc": ["binary"],
}


def _infer_kind(toolchain_id: str) -> str:
    head = toolchain_id.split("-", 1)[0].lower()
    return {"clang": "llvm", "llvm": "llvm", "gcc": "gcc", "filc": "filc", "fil": "filc"}.get(head, head)


def _infer_version(toolchain_id: str) -> str:
    _, _, rest = toolchain_id.partition("-")
    return rest if rest and rest[0].isdigit() else ""


@dataclass
class RunConfig:
    # --- what to rebuild ---------------------------------------------------
    image: str = ""
    distro: str = "auto"                    # auto | debian | alpine
    packages: list[str] = field(default_factory=list)      # explicit allow-list
    exclude: list[str] = field(default_factory=list)       # glob patterns
    include: list[str] = field(default_factory=list)       # glob patterns
    limit: int = 0                          # 0 = no limit
    essential_only: bool = False            # debian: only Essential/Important
    image_setup: list[str] = field(default_factory=list)   # shell run once on the image

    # --- how to run it -----------------------------------------------------
    backend: str = "oci"                    # host | chroot | bwrap | firejail | oci
    #: where the environment's filesystem comes from (docs/SPECS.md D-25):
    #:   image[:ref] | hostfs[:overlay|bind|copy] | dir:<path> | none
    rootfs: str = ""                        # "" = image for the isolating backends
    rootfs_path: str = ""                   # "" = under the cache, "temp" = throwaway
    rootfs_prepare: str = ""                # shell command that populates a dir: rootfs
    #: build directory: "" = the backend default, a path, or "temp"
    workdir: str = ""
    #: what the orchestrator may do to obtain the environment (D-27):
    #: auto (pull if missing) | require (never fetch) | download | build
    acquire: str = "auto"
    engine: str = "podman"                  # oci backend: podman | docker
    grouping: str = "all"                   # all | package | group
    group_size: int = 20                    # grouping=group
    jobs: int = 1                           # parallel build units
    build_jobs: int = 0                     # make -j inside a unit; 0 = nproc
    timeout: int = 1800                     # seconds per package
    keep_going: bool = True
    network: str = "enabled"                # enabled | disabled (inside build env)
    #: off | target | host - see sysdeps.py and docs/SPECS.md D-24.
    #: "host" is required before anything is installed on the host backend.
    system_deps: str = "target"

    # --- toolchains --------------------------------------------------------
    toolchains: list[ToolchainConfig] = field(default_factory=list)
    default_toolchain: str = "gcc"
    fallback_toolchain: str = ""            # "" = no fallback
    preflight: bool = True                  # gate the matrix on the default toolchain
    preflight_package: str = ""             # optional canary package

    # --- plumbing ----------------------------------------------------------
    registry_mirrors: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_REGISTRY_MIRRORS))
    source_mirrors: dict[str, str] = field(default_factory=dict)  # distro -> archive URL
    cache_dir: Path = DEFAULT_CACHE
    out_dir: Path = DEFAULT_OUT
    label: str = ""
    notes: list[str] = field(default_factory=list)

    # -- derived ------------------------------------------------------------

    def toolchain(self, toolchain_id: str) -> ToolchainConfig:
        for entry in self.toolchains:
            if entry.id == toolchain_id:
                return entry
        # Unknown ids are still usable: "clang-19" describes itself well enough.
        return ToolchainConfig(id=toolchain_id)

    @property
    def toolchain_ids(self) -> list[str]:
        return [tc.id for tc in self.toolchains]

    def with_overrides(self, **overrides: Any) -> "RunConfig":
        known = {f.name for f in fields(self)}
        data = {f.name: getattr(self, f.name) for f in fields(self)}
        for key, value in overrides.items():
            if value is None or key not in known:
                continue
            data[key] = value
        return RunConfig(**data)


# --- loading ----------------------------------------------------------------


def load_profile(path: str | Path) -> RunConfig:
    """Read a TOML profile into a RunConfig."""
    raw = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    return from_mapping(raw, base_dir=Path(path).parent)


def from_mapping(raw: dict[str, Any], base_dir: Path | None = None) -> RunConfig:
    data: dict[str, Any] = {}

    # Flat sections are merged into the top level so profiles stay readable:
    # [image] / [run] / [toolchains] instead of one giant table.
    for section in ("target", "run", "plumbing"):
        data.update(raw.get(section, {}) or {})
    data.update({k: v for k, v in raw.items() if not isinstance(v, (dict, list))})
    for key in ("packages", "exclude", "include", "notes", "image_setup"):
        if key in raw:
            data[key] = raw[key]

    toolchains = []
    for entry in raw.get("toolchains", []) or []:
        if isinstance(entry, str):
            toolchains.append(ToolchainConfig(id=entry))
        else:
            toolchains.append(ToolchainConfig(**entry))
    if toolchains:
        data["toolchains"] = toolchains

    for key in ("cache_dir", "out_dir"):
        if key in data:
            path = Path(str(data[key])).expanduser()
            if base_dir and not path.is_absolute():
                path = (base_dir / path).resolve()
            data[key] = path

    known = {f.name for f in fields(RunConfig)}
    unknown = sorted(set(data) - known)
    if unknown:
        raise ValueError(f"unknown configuration keys: {', '.join(unknown)}")
    return RunConfig(**data)


def default_toolchain_matrix() -> list[ToolchainConfig]:
    """The matrix used when a run does not name its own toolchains."""
    return [
        ToolchainConfig(id="gcc", kind="gcc", provision=["distro"]),
        ToolchainConfig(id="llvm", kind="llvm", provision=["distro", "binary"]),
        ToolchainConfig(id="llvm-20.1.8", kind="llvm", version="20.1.8", provision=["distro", "binary"]),
        ToolchainConfig(id="filc-0.681", kind="filc", version="0.681", provision=["binary"]),
    ]
