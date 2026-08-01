"""What a caller asks for, and what the orchestrator answers (docs/SPECS.md D-27).

The whole point of this module is that a caller describes the environment it
*needs* and never how to obtain it. "Is the image there? do I have to pull it?
should I build it?" are the orchestrator's questions, answered under a policy
the caller chooses but does not implement.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# --- acquisition policy -----------------------------------------------------
#
# What the orchestrator is allowed to do when the environment is not already
# available locally. The caller picks one; the orchestrator enforces it.

REQUIRE = "require"    # must already be here; never fetch, never build
DOWNLOAD = "download"  # may pull from a registry
BUILD = "build"        # may build locally (worker recipe / Containerfile)
AUTO = "auto"          # download if missing; do not build
POLICIES = (AUTO, REQUIRE, DOWNLOAD, BUILD)

# --- what the orchestrator will have to do to satisfy a request -------------

NONE = "none"                  # already available
PULL = "pull"                  # fetch an image from a registry
UNPACK = "unpack"              # unpack an image into a rootfs
MATERIALISE = "materialise"    # build a rootfs from the host or a prepare command
BUILD_ACTION = "build"         # build the environment from a recipe
UNAVAILABLE = "unavailable"    # cannot be satisfied at all


@dataclass
class EnvironmentRequest:
    """A description of the environment a caller needs.

    Everything here is *intent*. There is deliberately no "pull this image" or
    "run this build" field: those are consequences the orchestrator derives.
    """

    backend: str = "oci"            # host | chroot | bwrap | firejail | oci | vm
    engine: str = "podman"          # oci: podman | docker ; vm: qemu | libvirt
    image: str = ""                 # base or prebuilt worker image
    rootfs: str = ""                # image[:REF] | hostfs[:MODE] | dir:PATH | none
    rootfs_path: str = ""           # "" = cache, "temp" = throwaway
    rootfs_prepare: str = ""        # command that populates a dir: rootfs
    workdir: str = ""               # "" = the backend default, or "temp"
    network: bool = True
    binds: dict[str, str] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)
    name: str = "env"
    privileged: bool = False
    acquire: str = AUTO
    #: caching proxy for the package manager; "" = straight to the archive
    package_cache: str = ""
    #: free-form, carried through to the handle: flavour, toolchain, run id...
    labels: dict[str, str] = field(default_factory=dict)
    #: registry -> mirror host, applied to every image reference
    registry_mirrors: dict[str, str] = field(default_factory=dict)

    def describe(self) -> str:
        if self.backend in ("oci", "podman", "docker"):
            return f"{self.backend}:{self.image or '?'}"
        if self.backend in ("vm", "qemu"):
            return f"vm/{self.engine if self.engine in ('qemu', 'libvirt') else 'qemu'}:" \
                   f"{self.image or self.rootfs or '?'}"
        return f"{self.backend}:{self.rootfs or 'image'}"


@dataclass
class Availability:
    """The orchestrator's answer to "can you give me this, and at what cost?"."""

    satisfied: bool            # already available, nothing to do
    action: str                # what would have to happen: none/pull/unpack/...
    where: str = ""            # human description of the location, if any
    detail: str = ""           # why not, or what exactly would be done
    allowed: bool = True       # whether `action` is permitted by the policy

    @property
    def ok(self) -> bool:
        """True when a call to ensure() would succeed."""
        return self.satisfied or (self.allowed and self.action != UNAVAILABLE)

    def summary(self) -> str:
        if self.satisfied:
            return f"available ({self.where})" if self.where else "available"
        if not self.allowed:
            return f"missing, and the policy forbids '{self.action}': {self.detail}"
        if self.action == UNAVAILABLE:
            return f"unavailable: {self.detail}"
        return f"missing, would {self.action}: {self.detail or self.where}"


@dataclass
class EnvironmentHandle:
    """An open environment. Close it through the orchestrator, not by hand."""

    request: EnvironmentRequest
    executor: object                    # executors.base.Executor
    rootfs_path: str = ""
    image: str = ""
    _rootfs: object | None = None       # rootfs.Rootfs, released on close

    @property
    def backend(self) -> str:
        return self.request.backend

    def describe(self) -> dict[str, str]:
        return {
            "backend": self.request.backend,
            "engine": self.request.engine if self.request.backend in
                      ("oci", "podman", "docker", "vm") else "",
            "image": self.image,
            "rootfs": self.rootfs_path,
            "workdir": getattr(self.executor, "spec", None) and self.executor.spec.workdir or "",
            **{f"label.{k}": v for k, v in self.request.labels.items()},
        }


class OrchestrationError(RuntimeError):
    """Raised when a request cannot be satisfied under the chosen policy."""
