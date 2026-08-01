"""Executor: the only thing the rest of the tool knows about "where a command runs".

Deliberately tiny (docs/SPECS.md D-04): run a POSIX shell script, move files in
and out. Everything else is expressed in terms of those, which is what keeps the
four backends honest.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence

from .. import util
from ..logs import get
from .. import proxy as proxy_mod
from ..trace import Tracer

log = get("exec")


@dataclass
class CommandResult:
    argv: list[str]
    rc: int
    stdout: str
    stderr: str
    seconds: float
    step_id: str = ""

    @property
    def ok(self) -> bool:
        return self.rc == 0

    @property
    def output(self) -> str:
        """stdout and stderr interleaved is what a human wants in a log."""
        parts = [p for p in (self.stdout, self.stderr) if p]
        return "\n".join(parts)

    def check(self, what: str = "command") -> "CommandResult":
        if not self.ok:
            raise ExecutorError(f"{what} failed (rc={self.rc})\n{util.tail(self.output, 2000)}")
        return self


class ExecutorError(RuntimeError):
    pass


@dataclass
class ExecutorSpec:
    """Everything a backend needs to build a session, in one place."""

    kind: str = "host"
    image: str = ""                 # OCI reference (oci backend, rootfs source)
    rootfs: str = ""                # existing rootfs directory (bwrap/firejail)
    engine: str = "podman"          # oci: podman | docker
    workdir: str = "/build"         # working directory inside the environment
    binds: dict[str, str] = field(default_factory=dict)  # host path -> target path
    env: dict[str, str] = field(default_factory=dict)
    network: bool = True
    name: str = ""                  # container / session name
    privileged: bool = False
    #: Backend-level resource caps, already rendered as engine flags by
    #: lazybootstrap.limits. Kept as flags rather than as numbers so a backend
    #: that cannot enforce them holds an empty list instead of silently
    #: dropping a value it was handed.
    resource_args: list[str] = field(default_factory=list)


class Executor(ABC):
    """A place where shell scripts can be executed."""

    kind: str = "abstract"

    def __init__(self, spec: ExecutorSpec, tracer: Tracer | None = None) -> None:
        self.spec = spec
        self.tracer = tracer or Tracer()
        self.label = spec.name or self.kind
        self._started = False
        self._starting = False

    # -- lifecycle ----------------------------------------------------------

    def __enter__(self) -> "Executor":
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def start(self) -> None:
        # `_starting` breaks the cycle: a backend's _start() may itself call
        # run(), which lazily calls start() again.
        if self._started or self._starting:
            return
        self._starting = True
        try:
            self._start()
            self._started = True
        finally:
            self._starting = False

    def close(self) -> None:
        if self._started:
            self._close()
            self._started = False

    def _start(self) -> None:  # pragma: no cover - default is "nothing to do"
        return None

    def _close(self) -> None:  # pragma: no cover
        return None

    # -- the two primitives -------------------------------------------------

    @abstractmethod
    def _wrap(self, script: str, cwd: str | None, env: Mapping[str, str]) -> list[str]:
        """Return the host-side argv that runs `script` inside this environment."""

    def _wrap_stdin(self, script: str, env: Mapping[str, str]) -> list[str]:
        """argv for a command that must receive stdin. Most backends exec the
        script directly and inherit it; container engines need to be told."""
        return self._wrap(script, None, env)

    def feed(self, script: str, producer: list[str], title: str = "",
             unit: str = "", step_prefix: str = "run", timeout: int | None = None):
        """Run `producer` on the host and pipe its stdout into `script` inside.

        The point is to never materialise the intermediate: decompressing a
        2 GiB archive to an 8 GiB tar on the host and then copying that tar in
        costs ~25 GiB of disk for an 8 GiB install, and fails on any machine
        with a normal amount of free space.
        """
        import subprocess
        import time

        argv = self._wrap_stdin(script, dict(self.spec.env))
        step_id = self.tracer.next_id(step_prefix)
        self.tracer.step(step_id, title or "feed", backend=self.label,
                         wrapper=[*producer, "|", *argv], unit=unit, script=script)
        started = time.monotonic()
        source = subprocess.Popen(producer, stdout=subprocess.PIPE)
        try:
            proc = subprocess.run(argv, stdin=source.stdout, capture_output=True,
                                  text=True, timeout=timeout)
        finally:
            if source.stdout:
                source.stdout.close()
            source.wait()
        seconds = time.monotonic() - started
        output = (proc.stdout or "") + (proc.stderr or "")
        rc = proc.returncode or (source.returncode or 0)
        self.tracer.result(step_id, rc, seconds, output)
        return CommandResult(rc=rc, stdout=proc.stdout or "", stderr=proc.stderr or "",
                             seconds=seconds, argv=argv)

    @abstractmethod
    def upload(self, host_path: str | Path, target_path: str) -> None:
        """Copy a file or directory from the host into the environment."""

    @abstractmethod
    def download(self, target_path: str, host_path: str | Path) -> None:
        """Copy a file or directory out of the environment onto the host."""

    # -- derived operations -------------------------------------------------

    def run(
        self,
        script: str,
        *,
        title: str = "",
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        timeout: int | None = None,
        check: bool = False,
        unit: str = "",
        step_prefix: str = "step",
    ) -> CommandResult:
        """Run a POSIX shell script inside the environment."""
        self.start()
        merged = {**self.spec.env, **(env or {})}
        argv = self._wrap(script, cwd or self.spec.workdir, merged)
        step_id = self.tracer.next_id(step_prefix)

        self.tracer.step(
            step_id,
            title or script.strip().splitlines()[0][:70],
            backend=self.label,
            wrapper=argv,
            script=script,
            cwd=cwd or self.spec.workdir,
            env=merged,
            unit=unit or self.label,
        )

        started = time.monotonic()
        try:
            proc = subprocess.run(
                argv, capture_output=True, text=True, timeout=timeout, errors="replace"
            )
            rc, out, err = proc.returncode, proc.stdout, proc.stderr
        except subprocess.TimeoutExpired as exc:
            rc = 124
            out = _decode(exc.stdout)
            err = _decode(exc.stderr) + f"\n[lazy-bootstrap] timeout after {timeout}s"
        seconds = time.monotonic() - started

        result = CommandResult(argv=argv, rc=rc, stdout=out, stderr=err,
                               seconds=seconds, step_id=step_id)
        self.tracer.result(step_id, rc, seconds, result.output)
        log.debug("%s %s rc=%s in %s", self.label, step_id, rc, util.human_seconds(seconds))
        if check:
            result.check(title or "command")
        return result

    def which(self, program: str) -> str | None:
        result = self.run(f"command -v {util.shell_join([program])} 2>/dev/null || true",
                          title=f"which {program}", step_prefix="probe")
        path = result.stdout.strip().splitlines()
        return path[0] if path and path[0] else None

    def exists(self, path: str) -> bool:
        return self.run(f"test -e {util.shell_join([path])}",
                        title=f"exists {path}", step_prefix="probe").ok

    def read_text(self, path: str) -> str:
        result = self.run(f"cat {util.shell_join([path])}",
                          title=f"read {path}", step_prefix="probe")
        return result.stdout if result.ok else ""

    def write_text(self, path: str, content: str, mode: str = "") -> None:
        """Write a file inside the environment via a here-doc: no temp file on the
        host, no quoting surprises, works identically on all backends."""
        marker = "LB_EOF_9f3a"
        script = (
            f"mkdir -p $(dirname {util.shell_join([path])}) && "
            f"cat > {util.shell_join([path])} <<'{marker}'\n{content}\n{marker}\n"
        )
        if mode:
            script += f"chmod {mode} {util.shell_join([path])}\n"
        self.run(script, title=f"write {path}", check=True, step_prefix="write")

    def mkdir(self, path: str) -> None:
        self.run(f"mkdir -p {util.shell_join([path])}", title=f"mkdir {path}",
                 check=True, step_prefix="setup")

    # -- introspection ------------------------------------------------------

    def describe(self) -> dict[str, str]:
        return {
            "backend": self.kind,
            "label": self.label,
            "image": self.spec.image,
            "rootfs": self.spec.rootfs,
            "engine": self.spec.engine if self.kind == "oci" else "",
            "network": "enabled" if self.spec.network else "disabled",
        }


def _decode(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def host_proxy_env() -> dict[str, str]:
    """Proxy variables to hand to a backend that shares the host's loopback."""
    settings = proxy_mod.detect()
    return dict(settings.env) if settings.active else {}


def env_prefix(env: Mapping[str, str]) -> list[str]:
    """`env A=1 B=2 --` fragment shared by the backends that shell out."""
    if not env:
        return []
    return ["env"] + [f"{k}={v}" for k, v in env.items()]


def require(program: str, hint: str = "") -> str:
    path = shutil.which(program)
    if not path:
        raise ExecutorError(f"{program} not found on PATH{f' ({hint})' if hint else ''}")
    return path


def host_tmpdir(base: str | Path, name: str) -> Path:
    path = Path(base) / name
    util.ensure_dir(path)
    return path


def is_root() -> bool:
    return os.geteuid() == 0


def ensure_resolver(rootfs: str | Path) -> list[list[str]]:
    """Make DNS work inside a rootfs, and return how to undo it.

    An image's /etc/resolv.conf is usually an empty placeholder that the
    container engine replaces at run time; a chroot gets no such treatment, so
    every fetch fails with "Temporary failure resolving". Bind-mounting the
    host's file leaves the rootfs unmodified, which matters when the rootfs is
    a cached artefact reused by the next run; copying is the fallback when
    mounting is not possible.
    """
    import shutil
    import subprocess

    undo: list[list[str]] = []
    root = Path(rootfs)
    for host_file in ("/etc/resolv.conf", "/etc/hosts"):
        source = Path(host_file)
        if not source.exists():
            continue
        target = root / host_file.lstrip("/")
        util.ensure_dir(target.parent)
        if not target.exists():
            target.touch()
        if is_root():
            proc = subprocess.run(["mount", "--bind", str(source), str(target)],
                                  capture_output=True, text=True)
            if proc.returncode == 0:
                undo.append(["umount", "-l", str(target)])
                continue
        try:
            shutil.copy2(source, target)
        except OSError:
            pass
    return undo
