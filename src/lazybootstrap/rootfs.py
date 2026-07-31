"""Where a build environment's filesystem comes from (docs/SPECS.md D-25).

This is deliberately orthogonal to *how* commands run (the Executor backends).
A rootfs directory can be produced in four ways, and any of them can be paired
with chroot, bubblewrap or firejail:

    image    unpack an image from the local engine pool (podman/docker/skopeo)
    hostfs   derive it from the running host's filesystem
    dir      use a directory somebody else prepared - the escape hatch that
             lets an external tool (debootstrap, mmdebstrap, another engine,
             a CI artefact) own that step
    none     no rootfs at all: run straight on the host

`hostfs` has three modes, in increasing order of cost and isolation:

    bind     read-only recursive bind of the host tree - cheapest, but the
             build cannot write anywhere outside the workdir
    overlay  copy-on-write overlay on top of the host tree - writes land in an
             upper layer, the host is untouched. The default when available.
    copy     a real copy of the selected top-level directories - slow and
             large, but works without overlayfs and survives a reboot

Paths follow the same policy everywhere: a default under the cache, an explicit
path, or a throwaway temporary directory removed at the end of the run.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import util
from .images import ImageStore
from .logs import get
from .trace import Tracer

log = get("rootfs")

IMAGE = "image"
HOSTFS = "hostfs"
DIR = "dir"
NONE = "none"

SOURCES = (IMAGE, HOSTFS, DIR, NONE)
HOSTFS_MODES = ("auto", "overlay", "bind", "copy")

#: Top-level directories taken from the host for a `hostfs` rootfs. /home, /root
#: and /mnt are deliberately absent: a build has no business seeing them.
HOST_TREES = ["usr", "lib", "lib64", "lib32", "bin", "sbin", "etc", "var", "opt", "srv"]

#: `temp` as a path means "make me a throwaway directory".
TEMP = "temp"


class RootfsError(RuntimeError):
    pass


@dataclass
class RootfsSpec:
    """How to obtain the filesystem for one environment."""

    source: str = IMAGE
    ref: str = ""              # image reference, when source=image
    path: str = ""             # target dir; "" = under the cache, "temp" = throwaway
    mode: str = "auto"         # hostfs: auto | overlay | bind | copy
    prepare: str = ""          # shell command that populates the directory itself
    trees: list[str] = field(default_factory=lambda: list(HOST_TREES))
    reuse: bool = True         # reuse an already materialised rootfs

    @classmethod
    def parse(cls, value: str, image: str = "", **kwargs: object) -> "RootfsSpec":
        """Accept the compact CLI spellings.

            image            -> the run's --image, unpacked from the local pool
            image:debian:13  -> that image instead
            hostfs           -> derived from this machine
            hostfs:overlay   -> ...with an explicit mode
            dir:/srv/rootfs  -> an existing directory
            none             -> no rootfs (host backend)
        """
        head, _, tail = (value or IMAGE).partition(":")
        head = head.strip().lower()
        if head not in SOURCES:
            raise RootfsError(f"unknown rootfs source {head!r}; known: {', '.join(SOURCES)}")
        spec = cls(source=head, **kwargs)  # type: ignore[arg-type]
        if head == IMAGE:
            spec.ref = tail or image
        elif head == HOSTFS:
            if tail:
                spec.mode = tail
        elif head == DIR:
            if not tail:
                raise RootfsError("dir: needs a path, e.g. dir:/srv/rootfs")
            spec.path = tail
        if spec.mode not in HOSTFS_MODES:
            raise RootfsError(f"unknown hostfs mode {spec.mode!r}; known: {', '.join(HOSTFS_MODES)}")
        return spec

    def describe(self) -> str:
        if self.source == IMAGE:
            return f"image:{self.ref}"
        if self.source == HOSTFS:
            return f"hostfs:{self.mode}"
        if self.source == DIR:
            return f"dir:{self.path}"
        return NONE


class Rootfs:
    """A materialised rootfs, plus whatever must be undone at the end."""

    def __init__(self, path: Path, spec: RootfsSpec, cleanup: list[list[str]] | None = None,
                 remove: bool = False) -> None:
        self.path = path
        self.spec = spec
        self._cleanup = cleanup or []
        self._remove = remove

    def release(self) -> None:
        """Unmount in reverse order, then drop the directory if it was temporary."""
        for argv in reversed(self._cleanup):
            subprocess.run(argv, capture_output=True, text=True)
        self._cleanup.clear()
        if self._remove and self.path.exists():
            log.debug("removing temporary rootfs %s", self.path)
            shutil.rmtree(self.path, ignore_errors=True)

    def __enter__(self) -> "Rootfs":
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


class RootfsProvider:
    """Turns a RootfsSpec into a directory on disk."""

    def __init__(self, cache_dir: str | Path, images: ImageStore | None = None,
                 tracer: Tracer | None = None) -> None:
        self.cache_dir = util.ensure_dir(cache_dir)
        self.images = images
        self.tracer = tracer or Tracer()

    # -- entry point --------------------------------------------------------

    def materialise(self, spec: RootfsSpec, name: str = "env") -> Rootfs:
        if spec.source == NONE:
            return Rootfs(Path("/"), spec)
        if spec.source == DIR:
            return self._from_dir(spec)
        target, temporary = self._target_path(spec, name)
        if spec.source == IMAGE:
            return self._from_image(spec, target, temporary)
        if spec.source == HOSTFS:
            return self._from_hostfs(spec, target, temporary)
        raise RootfsError(f"unsupported rootfs source: {spec.source}")

    # -- path policy --------------------------------------------------------

    def _target_path(self, spec: RootfsSpec, name: str) -> tuple[Path, bool]:
        """Default under the cache, explicit path, or throwaway (D-25)."""
        if spec.path == TEMP:
            path = Path(tempfile.mkdtemp(prefix=f"lazy-bootstrap-{util.slugify(name)}-"))
            log.info("temporary rootfs at %s", path)
            return path, True
        if spec.path:
            return util.ensure_dir(spec.path), False
        key = util.slugify(spec.ref or f"{spec.source}-{spec.mode}")
        return util.ensure_dir(self.cache_dir / "rootfs" / key), False

    # -- sources ------------------------------------------------------------

    def _from_dir(self, spec: RootfsSpec) -> Rootfs:
        path = Path(spec.path)
        if spec.prepare:
            # Delegation: an external tool (debootstrap, another engine, a CI
            # artefact) owns the population step; we only run and check it.
            util.ensure_dir(path)
            self._run(["/bin/sh", "-c", spec.prepare], f"prepare rootfs at {path}",
                      env={"LB_ROOTFS": str(path)})
        if not path.is_dir():
            raise RootfsError(f"rootfs directory does not exist: {path}")
        if not (path / "bin").exists() and not (path / "usr" / "bin").exists():
            raise RootfsError(f"{path} does not look like a rootfs (no bin/ or usr/bin/)")
        log.info("using prepared rootfs %s", path)
        return Rootfs(path, spec)

    def _from_image(self, spec: RootfsSpec, target: Path, temporary: bool) -> Rootfs:
        if self.images is None:
            raise RootfsError("no image store configured for rootfs source 'image'")
        if not spec.ref:
            raise RootfsError("rootfs source 'image' needs an image reference")
        # ImageStore owns its own cache layout; honour an explicit target by
        # copying into it, so --rootfs-path always means what it says.
        unpacked = self.images.rootfs(spec.ref, force=not spec.reuse)
        if spec.path:
            if not any(target.iterdir()) or not spec.reuse:
                self._run(["cp", "-a", f"{unpacked}/.", str(target)],
                          f"copy rootfs to {target}")
            return Rootfs(target, spec, remove=temporary)
        return Rootfs(unpacked, spec)

    def _from_hostfs(self, spec: RootfsSpec, target: Path, temporary: bool) -> Rootfs:
        mode = spec.mode
        if mode == "auto":
            mode = "overlay" if _overlay_available() else "copy"
            log.debug("hostfs mode auto -> %s", mode)
        builder = {"overlay": self._hostfs_overlay,
                   "bind": self._hostfs_bind,
                   "copy": self._hostfs_copy}[mode]
        cleanup: list[list[str]] = []
        builder(spec, target, cleanup)
        log.info("hostfs rootfs ready at %s (%s)", target, mode)
        return Rootfs(target, spec, cleanup=cleanup, remove=temporary)

    # -- hostfs modes -------------------------------------------------------

    def _hostfs_overlay(self, spec: RootfsSpec, target: Path,
                        cleanup: list[list[str]]) -> None:
        """Copy-on-write view of the host: writes go to an upper layer."""
        state = target.parent / f"{target.name}.overlay"
        upper = util.ensure_dir(state / "upper")
        work = util.ensure_dir(state / "work")
        util.ensure_dir(target)
        argv = ["mount", "-t", "overlay", "lazy-bootstrap-overlay",
                "-o", f"lowerdir=/,upperdir={upper},workdir={work}", str(target)]
        result = self._run(argv, "overlay host filesystem", check=False)
        if result.returncode != 0:
            raise RootfsError(
                "overlay mount failed (need root and overlayfs); use "
                f"--rootfs hostfs:copy instead\n{result.stderr.strip()}")
        cleanup.append(["umount", "-l", str(target)])

    def _hostfs_bind(self, spec: RootfsSpec, target: Path,
                     cleanup: list[list[str]]) -> None:
        """Read-only recursive bind: cheapest, and the build cannot alter the host."""
        util.ensure_dir(target)
        for tree in spec.trees:
            source = Path("/") / tree
            if not source.exists():
                continue
            mountpoint = util.ensure_dir(target / tree)
            self._run(["mount", "--rbind", str(source), str(mountpoint)],
                      f"bind /{tree}").check_returncode()
            self._run(["mount", "-o", "remount,ro,bind", str(mountpoint)],
                      f"remount /{tree} read-only", check=False)
            cleanup.append(["umount", "-l", str(mountpoint)])
        for writable in ("tmp", "run", "build"):
            util.ensure_dir(target / writable)

    def _hostfs_copy(self, spec: RootfsSpec, target: Path,
                     cleanup: list[list[str]]) -> None:
        """A real copy: slow, but needs no mount privileges and outlives a reboot."""
        util.ensure_dir(target)
        for tree in spec.trees:
            source = Path("/") / tree
            destination = target / tree
            if not source.exists() or destination.exists():
                continue
            log.info("copying /%s into the rootfs (this is the slow mode)", tree)
            self._run(["cp", "-a", str(source), str(destination)],
                      f"copy /{tree}").check_returncode()
        for extra in ("proc", "sys", "dev", "tmp", "run", "build"):
            util.ensure_dir(target / extra)

    # -- plumbing -----------------------------------------------------------

    def _run(self, argv: list[str], title: str, check: bool = True,
             env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        step_id = self.tracer.next_id("rootfs")
        self.tracer.step(step_id, title, wrapper=argv, env=env or {})
        import os

        merged = {**os.environ, **(env or {})}
        result = subprocess.run(argv, capture_output=True, text=True, env=merged)
        self.tracer.result(step_id, result.returncode, 0.0, result.stdout + result.stderr)
        if check and result.returncode != 0:
            raise RootfsError(f"{title} failed:\n{util.tail(result.stderr, 1200)}")
        return result


def _overlay_available() -> bool:
    try:
        filesystems = Path("/proc/filesystems").read_text(encoding="utf-8")
    except OSError:
        return False
    import os

    return "overlay" in filesystems and os.geteuid() == 0


# --- workdir policy ---------------------------------------------------------


def resolve_workdir(value: str, default: str = "/build") -> tuple[str, bool]:
    """Default path, explicit path, or a throwaway one (D-25).

    Returns (path, temporary). Only meaningful for the host backend: inside a
    container or a chroot the workdir is created wherever it is asked for.
    """
    if not value:
        return default, False
    if value == TEMP:
        return tempfile.mkdtemp(prefix="lazy-bootstrap-work-"), True
    return value, False
