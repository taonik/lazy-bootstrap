"""OCI backend: podman or docker, engine-agnostic.

One long-lived container per build unit (D-06): the container is started with
`sleep infinity` and every step is an `exec` into it, so build-dependencies and
unpacked sources survive between steps. Engine differences are limited to the
binary name plus a couple of flags, so both are supported by the same class.
"""

from __future__ import annotations

import itertools
import os
import subprocess
from pathlib import Path
from typing import Mapping

from .. import util
from ..logs import get
from .. import proxy as proxy_mod
from .base import Executor, ExecutorError, env_prefix, require

log = get("oci")

_COUNTER = itertools.count(1)


class OciExecutor(Executor):
    kind = "oci"

    def __init__(self, spec, tracer=None) -> None:  # type: ignore[no-untyped-def]
        super().__init__(spec, tracer)
        self.engine = spec.engine or "podman"
        # Names carry a per-process suffix: an interrupted run can leave a
        # container in "Stopping" for a while, and a fresh run must not collide
        # with it. `podman ps` still shows what belongs to which run.
        base = spec.name or f"lb-{util.slugify(spec.image)[:32]}"
        self.container = f"{base}-{os.getpid()}-{next(_COUNTER)}"
        self.label = f"{self.engine}:{self.container}"

    # -- lifecycle ----------------------------------------------------------

    def _start(self) -> None:
        require(self.engine, f"install {self.engine}")
        if not self.spec.image:
            raise ExecutorError("oci backend needs an image reference (spec.image)")
        self._remove_stale()

        # No --workdir here: the directory does not exist yet in a pristine
        # image and both podman and docker refuse to start in that case.
        argv = [self.engine, "run", "--detach", "--name", self.container]
        if self.engine == "podman":
            argv.append("--replace")        # tolerate a container left by a killed run
            argv += ["--stop-timeout", "0"]  # SIGKILL straight away on removal
        settings = proxy_mod.detect()
        if settings.active and self.spec.network:
            if settings.points_at_loopback():
                # A loopback-bound proxy is unreachable from a bridged
                # container: the name resolves to the gateway, but the proxy is
                # not listening there. Sharing the host's network namespace is
                # what makes the sanctioned egress path usable at all (D-33).
                argv += ["--network", "host"]
                log.info("egress proxy on loopback: using the host network namespace")
            for key, value in settings.env.items():
                argv += ["--env", f"{key}={value}"]
        self._proxy = settings
        if not self.spec.network:
            argv += ["--network", "none"]
        if self.spec.privileged:
            argv += ["--privileged"]
        for host_path, target in self.spec.binds.items():
            util.ensure_dir(host_path)
            # :z relabels for SELinux hosts and is a no-op elsewhere.
            argv += ["--volume", f"{Path(host_path).resolve()}:{target}:z"]
        for key, value in self.spec.env.items():
            argv += ["--env", f"{key}={value}"]
        argv += [self.spec.image, "sleep", "infinity"]

        step_id = self.tracer.next_id("session")
        self.tracer.step(step_id, f"start container {self.container}",
                         backend=self.label, wrapper=argv, unit=self.container)
        proc = subprocess.run(argv, capture_output=True, text=True)
        self.tracer.result(step_id, proc.returncode, 0.0, proc.stdout + proc.stderr)
        if proc.returncode != 0:
            raise ExecutorError(f"could not start container:\n{proc.stderr.strip()}")
        # `sleep` must exist in the image; busybox and coreutils both provide it.
        self.run("true", title="container ready", step_prefix="session").check("container start")

    def _close(self) -> None:
        self._remove_stale()

    def _remove_stale(self) -> None:
        # `-t 0` skips the 10s SIGTERM grace period: `sleep infinity` is not
        # going to shut down gracefully and we do not want to wait for it.
        argv = [self.engine, "rm", "-f"]
        if self.engine == "podman":
            argv += ["-t", "0"]
        subprocess.run([*argv, self.container], capture_output=True, text=True, timeout=120)

    # -- primitives ---------------------------------------------------------

    def _wrap(self, script: str, cwd: str | None, env: Mapping[str, str]) -> list[str]:
        argv = [self.engine, "exec"]
        for key, value in env.items():
            argv += ["--env", f"{key}={value}"]
        # `cd` in the script rather than --workdir: the directory may not exist
        # yet, and it keeps the traced command pasteable as-is.
        prologue = f"cd {util.shell_join([cwd])} 2>/dev/null || true\n" if cwd else ""
        argv += [self.container, "/bin/sh", "-c", prologue + script]
        return argv

    def _wrap_stdin(self, script: str, env: Mapping[str, str]) -> list[str]:
        """`podman exec` leaves stdin closed unless asked: without -i the piped
        archive never reaches tar, which then reports the far more confusing
        "This does not look like a tar archive"."""
        argv = self._wrap(script, None, env)
        return [argv[0], argv[1], "-i", *argv[2:]]

    def upload(self, host_path: str | Path, target_path: str) -> None:
        self._cp(str(Path(host_path)), f"{self.container}:{target_path}")

    def download(self, target_path: str, host_path: str | Path) -> None:
        util.ensure_dir(Path(host_path).parent)
        self._cp(f"{self.container}:{target_path}", str(Path(host_path)))

    def _cp(self, src: str, dst: str) -> None:
        argv = [self.engine, "cp", src, dst]
        step_id = self.tracer.next_id("copy")
        self.tracer.step(step_id, f"cp {src} -> {dst}", backend=self.label,
                         wrapper=argv, unit=self.container)
        proc = subprocess.run(argv, capture_output=True, text=True)
        self.tracer.result(step_id, proc.returncode, 0.0, proc.stdout + proc.stderr)
        if proc.returncode != 0:
            raise ExecutorError(f"{self.engine} cp failed: {proc.stderr.strip()}")

    # -- extras -------------------------------------------------------------

    def commit(self, image_ref: str) -> None:
        """Freeze the current container state into an image - handy to cache a
        prepared builder (toolchain + build-dep) between runs."""
        subprocess.run([self.engine, "commit", self.container, image_ref],
                       capture_output=True, text=True, check=True)
