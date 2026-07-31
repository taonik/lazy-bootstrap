"""The orchestrator: the only door between a job and its execution environment.

Three verbs, and callers never need more:

    probe()               what can this machine do at all?
    available(request)    could you give me this, and what would it cost?
    open(request)         give it to me (acquiring it if the policy allows)

`available()` has no side effects, so a job can ask before committing - that is
the "query the orchestrator" half of docs/SPECS.md D-27. `open()` performs
whatever `available()` predicted, subject to the request's acquisition policy,
so a job never has to decide *how* to obtain an image or a rootfs.

This package is written to be liftable into a repository of its own: nothing in
it imports the rebuild logic, and its public surface is spec.py plus these three
verbs.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from . import rootfs as rootfs_mod
from . import util
from .executors import NEEDS_ROOTFS, ExecutorSpec, create
from .executors import probe as probe_backends
from .images import ImageStore, apply_mirrors
from .logs import get
from .spec import (AUTO, BUILD, BUILD_ACTION, DOWNLOAD, MATERIALISE, NONE, PULL,
                   REQUIRE, UNAVAILABLE, UNPACK, Availability, EnvironmentHandle,
                   EnvironmentRequest, OrchestrationError)
from .trace import Tracer

log = get("orchestrator")


class Orchestrator:
    def __init__(self, cache_dir: str | Path = "/var/cache/lazy-bootstrap",
                 tracer: Tracer | None = None) -> None:
        self.cache_dir = util.ensure_dir(cache_dir)
        self.tracer = tracer or Tracer()
        self._open: list[EnvironmentHandle] = []

    # -- what this machine can do -------------------------------------------

    def probe(self) -> dict[str, dict[str, str]]:
        return probe_backends()

    def supports(self, backend: str) -> bool:
        info = self.probe().get(backend if backend != "oci" else "podman", {})
        return info.get("available") == "yes"

    # -- can you give me this? ----------------------------------------------

    def available(self, request: EnvironmentRequest) -> Availability:
        """Answer without changing anything on disk."""
        backend = request.backend
        if backend == "host":
            return Availability(satisfied=True, action=NONE, where="this machine")

        if backend in ("oci", "podman", "docker"):
            return self._image_availability(request)

        if backend in NEEDS_ROOTFS or backend == "vm":
            return self._rootfs_availability(request)

        return Availability(satisfied=False, action=UNAVAILABLE,
                            detail=f"unknown backend {backend!r}")

    def _image_availability(self, request: EnvironmentRequest) -> Availability:
        engine = request.engine or "podman"
        if not shutil.which(engine):
            return Availability(satisfied=False, action=UNAVAILABLE,
                                detail=f"{engine} is not installed")
        if not request.image:
            return Availability(satisfied=False, action=UNAVAILABLE,
                                detail="no image reference in the request")
        ref = apply_mirrors(request.image, request.registry_mirrors)
        if self._image_present(engine, ref):
            return Availability(satisfied=True, action=NONE, where=f"{engine} image {ref}")
        return Availability(
            satisfied=False, action=PULL, where=ref,
            detail=f"{ref} is not in the local {engine} store",
            allowed=request.acquire in (AUTO, DOWNLOAD, BUILD),
        )

    def _rootfs_availability(self, request: EnvironmentRequest) -> Availability:
        spec = self._rootfs_spec(request)
        if spec.source == rootfs_mod.NONE:
            return Availability(satisfied=True, action=NONE, where="/")

        if spec.source == rootfs_mod.DIR:
            path = Path(spec.path)
            if path.is_dir() and (path / "bin").exists() or (path / "usr" / "bin").exists():
                return Availability(satisfied=True, action=NONE, where=str(path))
            if spec.prepare:
                return Availability(satisfied=False, action=MATERIALISE, where=str(path),
                                    detail="would run the prepare command",
                                    allowed=request.acquire in (AUTO, DOWNLOAD, BUILD))
            return Availability(satisfied=False, action=UNAVAILABLE,
                                detail=f"{path} is not a rootfs and no --rootfs-prepare was given")

        if spec.source == rootfs_mod.HOSTFS:
            return Availability(satisfied=False, action=MATERIALISE,
                                where=spec.path or "cache",
                                detail=f"would build a {spec.mode} view of this machine",
                                allowed=request.acquire in (AUTO, DOWNLOAD, BUILD))

        # source == image: is it unpacked already, and if not, is it even local?
        cached = self.cache_dir / "rootfs" / util.slugify(
            apply_mirrors(spec.ref, request.registry_mirrors))
        if cached.with_suffix(".stamp").exists():
            return Availability(satisfied=True, action=NONE, where=str(cached))
        engine = request.engine or "podman"
        ref = apply_mirrors(spec.ref, request.registry_mirrors)
        if shutil.which(engine) and self._image_present(engine, ref):
            return Availability(satisfied=False, action=UNPACK, where=ref,
                                detail=f"{ref} is local but not yet unpacked",
                                allowed=request.acquire != REQUIRE)
        return Availability(satisfied=False, action=PULL, where=ref,
                            detail=f"{ref} must be pulled, then unpacked",
                            allowed=request.acquire in (AUTO, DOWNLOAD, BUILD))

    # -- give it to me ------------------------------------------------------

    def open(self, request: EnvironmentRequest) -> EnvironmentHandle:
        """Acquire (if permitted) and start an environment."""
        state = self.available(request)
        if not state.satisfied:
            if not state.allowed or state.action == UNAVAILABLE:
                raise OrchestrationError(
                    f"cannot provide {request.describe()}: {state.summary()}"
                    + (f"\nrelax it with --acquire download (currently '{request.acquire}')"
                       if not state.allowed else ""))
            log.info("%s: %s", request.name, state.summary())

        spec = ExecutorSpec(
            kind=request.backend,
            engine=request.engine,
            workdir=request.workdir or "/build",
            network=request.network,
            name=request.name,
            binds=dict(request.binds),
            env=dict(request.env),
            privileged=request.privileged,
        )
        handle = EnvironmentHandle(request=request, executor=None)  # type: ignore[arg-type]

        if request.backend in ("oci", "podman", "docker"):
            store = self._store(request)
            spec.image = store.pull(request.image)
            handle.image = spec.image
        elif request.backend in NEEDS_ROOTFS:
            materialised = self._provider(request).materialise(
                self._rootfs_spec(request), request.name)
            spec.rootfs = str(materialised.path)
            handle.rootfs_path = spec.rootfs
            handle._rootfs = materialised

        spec.kind = request.backend
        handle.executor = create(spec, self.tracer)
        handle.executor.start()
        handle.executor.mkdir(spec.workdir)
        self._open.append(handle)
        return handle

    def close(self, handle: EnvironmentHandle) -> None:
        try:
            handle.executor.close()
        finally:
            if handle._rootfs is not None:
                handle._rootfs.release()
            if handle in self._open:
                self._open.remove(handle)

    def close_all(self) -> None:
        for handle in list(self._open):
            self.close(handle)

    # -- helpers ------------------------------------------------------------

    def _store(self, request: EnvironmentRequest) -> ImageStore:
        return ImageStore(self.cache_dir, request.engine or "podman",
                          request.registry_mirrors, self.tracer)

    def _provider(self, request: EnvironmentRequest) -> rootfs_mod.RootfsProvider:
        return rootfs_mod.RootfsProvider(self.cache_dir, self._store(request), self.tracer)

    def _rootfs_spec(self, request: EnvironmentRequest) -> rootfs_mod.RootfsSpec:
        return rootfs_mod.RootfsSpec.parse(
            request.rootfs or rootfs_mod.IMAGE,
            image=request.image,
            path=request.rootfs_path,
            prepare=request.rootfs_prepare,
        )

    @staticmethod
    def _image_present(engine: str, ref: str) -> bool:
        proc = subprocess.run([engine, "image", "exists", ref], capture_output=True, text=True)
        if engine == "podman" and proc.returncode in (0, 1):
            return proc.returncode == 0
        return subprocess.run([engine, "image", "inspect", ref],
                              capture_output=True, text=True).returncode == 0


__all__ = ["Orchestrator", "AUTO", "BUILD", "DOWNLOAD", "REQUIRE"]
_ = (BUILD_ACTION,)   # re-exported for callers that classify actions
