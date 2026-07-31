"""OCI image handling: reference normalisation, mirrors, pull and unpack.

Two jobs, both boring on purpose:

* turn a user-written reference ("debian:13-slim", "docker.io/library/alpine")
  into something an engine will accept, applying registry mirrors (D-20);
* materialise a rootfs directory from an image, for the bwrap/firejail backends.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from . import util
from .logs import get
from .trace import Tracer

log = get("images")

DEFAULT_REGISTRY = "docker.io"
DEFAULT_NAMESPACE = "library"


def normalise(ref: str) -> str:
    """`alpine` -> `docker.io/library/alpine:latest`.

    Applies the same rules as the Docker CLI so that mirror matching below can
    work on a canonical string.
    """
    if not ref:
        return ref
    remainder = ref
    registry = ""
    head, sep, tail = ref.partition("/")
    looks_like_host = sep and ("." in head or ":" in head or head == "localhost")
    if looks_like_host:
        registry, remainder = head, tail
    else:
        registry = DEFAULT_REGISTRY
    if "/" not in remainder and registry == DEFAULT_REGISTRY:
        remainder = f"{DEFAULT_NAMESPACE}/{remainder}"
    name, _, tag = remainder.partition(":")
    if "@" in remainder:              # digest reference: leave it alone
        return f"{registry}/{remainder}"
    return f"{registry}/{name}:{tag or 'latest'}"


def apply_mirrors(ref: str, mirrors: dict[str, str]) -> str:
    """Rewrite the registry host of `ref` if a mirror is configured for it.

    `{"docker.io": "mirror.gcr.io"}` turns `docker.io/library/debian:13-slim`
    into `mirror.gcr.io/library/debian:13-slim`.
    """
    if not mirrors:
        return ref
    canonical = normalise(ref)
    registry, _, remainder = canonical.partition("/")
    mirror = mirrors.get(registry) or mirrors.get("*")
    if not mirror:
        return canonical
    return f"{mirror.rstrip('/')}/{remainder}"


class ImageStore:
    """Pull images and unpack rootfs directories, with a host-side cache."""

    def __init__(
        self,
        cache_dir: str | Path,
        engine: str = "podman",
        mirrors: dict[str, str] | None = None,
        tracer: Tracer | None = None,
    ) -> None:
        self.cache_dir = util.ensure_dir(cache_dir)
        self.engine = engine
        self.mirrors = mirrors or {}
        self.tracer = tracer or Tracer()

    # -- pull ---------------------------------------------------------------

    def resolve(self, ref: str) -> str:
        return apply_mirrors(ref, self.mirrors)

    def pull(self, ref: str) -> str:
        """Make `ref` available locally; returns the reference actually used."""
        target = self.resolve(ref)
        if self._present(target):
            log.debug("image already local: %s", target)
            return target
        log.info("pulling %s", target)
        self._run([self.engine, "pull", target], f"pull {target}")
        return target

    def _present(self, ref: str) -> bool:
        proc = subprocess.run([self.engine, "image", "exists", ref],
                              capture_output=True, text=True)
        if proc.returncode in (0, 1) and self.engine == "podman":
            return proc.returncode == 0
        proc = subprocess.run([self.engine, "image", "inspect", ref],
                              capture_output=True, text=True)
        return proc.returncode == 0

    # -- unpack -------------------------------------------------------------

    def rootfs(self, ref: str, force: bool = False) -> Path:
        """Export `ref` into a directory usable by bwrap/firejail.

        Order of preference (D-07): engine export, then skopeo+umoci.
        """
        target = self.resolve(ref)
        dest = self.cache_dir / "rootfs" / util.slugify(target)
        stamp = dest.with_suffix(".stamp")
        if stamp.exists() and not force:
            log.debug("rootfs cached: %s", dest)
            return dest
        if dest.exists():
            # An interrupted run can leave bind mounts inside; releasing them
            # first turns "Device or resource busy" into a non-event.
            released = util.unmount_below(dest)
            if released:
                log.info("released %d stale mount(s) under %s", len(released), dest)
            self._run(["rm", "-rf", str(dest)], "clean rootfs")
        util.ensure_dir(dest)

        if util.which(self.engine):
            self.pull(target)
            self._export_via_engine(target, dest)
        elif util.which("skopeo") and util.which("umoci"):
            self._export_via_skopeo(target, dest)
        else:
            raise RuntimeError(
                "no way to unpack an image: install podman/docker, or skopeo+umoci"
            )
        stamp.write_text(target + "\n", encoding="utf-8")
        log.info("rootfs ready: %s (%.0f MiB)", dest, util.dir_size(dest) / (1 << 20))
        return dest

    def _export_via_engine(self, ref: str, dest: Path) -> None:
        name = f"lb-export-{util.slugify(ref)[:32]}"
        subprocess.run([self.engine, "rm", "-f", name], capture_output=True, text=True)
        self._run([self.engine, "create", "--name", name, ref, "true"], f"create {name}")
        try:
            # export | tar -x keeps the whole thing streaming: no intermediate tarball.
            step_id = self.tracer.next_id("image")
            self.tracer.step(step_id, f"export {ref} -> {dest}",
                             wrapper=[self.engine, "export", name, "|", "tar", "-x", "-C", str(dest)])
            export = subprocess.Popen([self.engine, "export", name], stdout=subprocess.PIPE)
            untar = subprocess.Popen(["tar", "-x", "-C", str(dest)], stdin=export.stdout)
            if export.stdout:
                export.stdout.close()
            rc = untar.wait()
            export.wait()
            self.tracer.result(step_id, rc, 0.0)
            if rc != 0:
                raise RuntimeError(f"export of {ref} failed (rc={rc})")
        finally:
            subprocess.run([self.engine, "rm", "-f", name], capture_output=True, text=True)

    def _export_via_skopeo(self, ref: str, dest: Path) -> None:
        oci_dir = self.cache_dir / "oci" / util.slugify(ref)
        if oci_dir.exists():
            self._run(["rm", "-rf", str(oci_dir)], "clean oci layout")
        self._run(["skopeo", "copy", f"docker://{ref}", f"oci:{oci_dir}:img"], f"skopeo copy {ref}")
        bundle = self.cache_dir / "bundle" / util.slugify(ref)
        if bundle.exists():
            self._run(["rm", "-rf", str(bundle)], "clean bundle")
        self._run(["umoci", "unpack", "--rootless", "--image", f"{oci_dir}:img", str(bundle)],
                  f"umoci unpack {ref}")
        self._run(["cp", "-a", f"{bundle}/rootfs/.", str(dest)], "move rootfs")

    # -- plumbing -----------------------------------------------------------

    def _run(self, argv: list[str], title: str) -> None:
        step_id = self.tracer.next_id("image")
        self.tracer.step(step_id, title, wrapper=argv)
        proc = subprocess.run(argv, capture_output=True, text=True)
        self.tracer.result(step_id, proc.returncode, 0.0, proc.stdout + proc.stderr)
        if proc.returncode != 0:
            raise RuntimeError(f"{title} failed:\n{util.tail(proc.stderr, 1500)}")
