"""Small helpers shared across the code base. Nothing here should import
anything else from lazybootstrap, so it stays safe to import from everywhere."""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import shlex
import shutil
import tempfile
from pathlib import Path
from typing import Any, Iterable, Sequence

# --- time -------------------------------------------------------------------


def now_iso() -> str:
    """UTC timestamp, second resolution, stable across runs and machines."""
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


def human_seconds(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, rest = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m{rest:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


# --- text -------------------------------------------------------------------

_SLUG_RE = re.compile(r"[^a-zA-Z0-9._-]+")


def slugify(value: str) -> str:
    """Filesystem- and container-name-safe rendering of an arbitrary string."""
    return _SLUG_RE.sub("-", value).strip("-").lower() or "x"


def tail(text: str, limit: int = 4000) -> str:
    """Keep the end of a log: that is where the error message lives."""
    if len(text) <= limit:
        return text
    return "...[truncated]...\n" + text[-limit:]


def shell_join(argv: Sequence[str]) -> str:
    return " ".join(shlex.quote(a) for a in argv)


def indent(text: str, prefix: str = "  ") -> str:
    return "\n".join(prefix + line for line in text.splitlines())


# --- filesystem -------------------------------------------------------------


SYSTEM_CACHE = "/var/cache/lazy-bootstrap"


def default_cache_dir() -> str:
    """The system cache when it is usable, a per-user one otherwise.

    Running as root is not a requirement of this tool, so defaulting to a
    directory only root can create would make every non-root invocation fail
    at construction time - including the ones that only want to ask a question.
    """
    import os

    candidate = Path(SYSTEM_CACHE)
    try:
        candidate.mkdir(parents=True, exist_ok=True)
        probe = candidate / ".writable"
        probe.touch()
        probe.unlink()
        return str(candidate)
    except OSError:
        base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
        return str(Path(base) / "lazy-bootstrap")


def ensure_dir(path: str | os.PathLike[str]) -> Path:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def write_text_atomic(path: str | os.PathLike[str], data: str) -> Path:
    """Write via a temp file in the same directory, then rename: a crashed run
    never leaves a half-written report behind."""
    p = Path(path)
    ensure_dir(p.parent)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=f".{p.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
        os.replace(tmp, p)
    except BaseException:
        _silent_unlink(tmp)
        raise
    return p


def write_json_atomic(path: str | os.PathLike[str], payload: Any) -> Path:
    return write_text_atomic(path, json.dumps(payload, indent=2, sort_keys=False) + "\n")


def _silent_unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def sha256_file(path: str | os.PathLike[str]) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def which(program: str) -> str | None:
    return shutil.which(program)


def dir_size(path: str | os.PathLike[str]) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


# --- misc -------------------------------------------------------------------


def unmount_below(path: str | os.PathLike[str]) -> list[str]:
    """Lazily unmount everything under `path`, deepest first.

    A rootfs directory can carry bind mounts (the resolver, /proc, a bound
    cache) and an interrupted run leaves them behind. Without this, the next run
    fails on "Device or resource busy" while trying to refresh the rootfs -
    which is a confusing way to say "someone pressed Ctrl-C last time".
    """
    import subprocess

    target = str(Path(path).resolve())
    try:
        mounts = Path("/proc/self/mounts").read_text(encoding="utf-8")
    except OSError:
        return []
    points = []
    for line in mounts.splitlines():
        fields = line.split()
        if len(fields) < 2:
            continue
        mountpoint = fields[1].replace("\\040", " ")
        if mountpoint == target or mountpoint.startswith(target.rstrip("/") + "/"):
            points.append(mountpoint)
    released = []
    for mountpoint in sorted(points, key=len, reverse=True):
        if subprocess.run(["umount", "-l", mountpoint],
                          capture_output=True, text=True).returncode == 0:
            released.append(mountpoint)
    return released


def chunked(items: Sequence[Any], size: int) -> Iterable[list[Any]]:
    """Split a sequence into fixed-size chunks (used by the group planner)."""
    if size <= 0:
        yield list(items)
        return
    for start in range(0, len(items), size):
        yield list(items[start : start + size])


def unique(items: Iterable[Any]) -> list[Any]:
    seen: set[Any] = set()
    out: list[Any] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out
