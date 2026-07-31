"""Saved build environments, so a rebuilt package need not be prepared twice.

This is the pbuilder idea (https://wiki.debian.org/pbuilder): prepare a chroot
with the build-dependencies once, keep it, and start from it next time. The
external cacher stops the *downloads* repeating; this stops the *installs*
repeating, which is the larger half - unpacking and configuring debhelper and
its tree costs more than fetching it.

Layout, as requested:

    env/packages/<os>/<os-version>/<package>/<version-slice>/<toolchain>.json

with the payload beside the manifest - a rootfs tarball for the chroot-like
backends, or an image reference for podman and docker, which can commit a
container directly.

The toolchain belongs in the key even though it is not in the path template:
an environment prepared for gcc has the wrong compiler for a filc rebuild, and
silently reusing it would produce a result labelled filc that is nothing of
the kind.

## Staleness is a correctness problem here, not a performance one

This tool exists to report whether a package still rebuilds *today*. An
environment saved last month may hold superseded build-dependencies, so
reusing it blindly answers a different question than the one asked, while
looking like an answer to this one. Hence the manifest records what the
environment was prepared from, and reuse is checked rather than assumed:

    strict  (default) reuse only when the archive index still matches
    relaxed reuse when Build-Depends match, refreshing packages first
    off     always prepare from scratch

Whichever applies, the report says which environments were reused and how old
they were. A reused environment is never invisible.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .orchestration.logs import get

log = get("envstore")

MANIFEST_VERSION = 1


class Reuse:
    """How eagerly a saved environment may be reused."""

    OFF = "off"
    STRICT = "strict"
    RELAXED = "relaxed"
    ALL = (OFF, STRICT, RELAXED)


def slice_version(version: str, mode: str = "exact") -> str:
    """Reduce a package version to the granularity we key on.

    Coarser slices hit more often and are more likely to be wrong: build
    dependencies do change across a major version, occasionally across a minor
    one. `exact` is the default because the case this feature is for -
    rebuilding the same package repeatedly - hits perfectly at `exact` anyway,
    so the looser modes buy nothing for it and only add risk.
    """
    if not version:
        return "any"
    if mode == "any":
        return "any"
    cleaned = version.split(":", 1)[-1]          # drop an epoch
    parts = cleaned.replace("-", ".").split(".")
    if mode == "major":
        return parts[0] or "any"
    if mode == "minor":
        return ".".join(parts[:2]) if len(parts) > 1 else parts[0]
    return cleaned


@dataclass
class Manifest:
    """What an environment was prepared from - the basis for trusting it."""

    version: int = MANIFEST_VERSION
    os_name: str = ""
    os_version: str = ""
    arch: str = ""
    package: str = ""
    package_version: str = ""
    version_slice: str = ""
    toolchain: str = ""
    toolchain_version: str = ""
    build_depends: str = ""
    index_fingerprint: str = ""      # InRelease / APKINDEX digest at save time
    backend: str = ""
    payload: str = ""                # image ref, or tarball file name
    created: float = 0.0
    hits: int = 0
    bytes: int = 0

    @property
    def age_seconds(self) -> float:
        return max(0.0, time.time() - self.created) if self.created else 0.0

    def describe_age(self) -> str:
        seconds = self.age_seconds
        if seconds < 3600:
            return f"{seconds / 60:.0f}m old"
        if seconds < 86400:
            return f"{seconds / 3600:.0f}h old"
        return f"{seconds / 86400:.0f}d old"

    def matches(self, other: "Manifest", mode: str) -> tuple[bool, str]:
        """Is `self` (stored) usable for `other` (wanted) under `mode`?

        Returns the reason on refusal, because "prepared from scratch" with no
        explanation is indistinguishable from the feature not working.
        """
        if mode == Reuse.OFF:
            return False, "reuse disabled"
        if self.version != other.version:
            return False, "manifest written by a different version of this tool"
        for attr, label in (("os_name", "distro"), ("os_version", "distro version"),
                            ("arch", "architecture"), ("package", "package"),
                            ("toolchain", "toolchain")):
            if getattr(self, attr) != getattr(other, attr):
                return False, f"{label} differs"
        if self.version_slice != other.version_slice:
            return False, "package version outside the reuse slice"
        # A changed Build-Depends means the saved environment is missing
        # something the build now needs; no mode may ignore that.
        if other.build_depends and self.build_depends != other.build_depends:
            return False, "build-dependencies changed since it was saved"
        if mode == Reuse.STRICT and other.index_fingerprint:
            if self.index_fingerprint != other.index_fingerprint:
                return False, "archive index moved on (relaxed mode would refresh)"
        return True, ""


class EnvStore:
    """The on-disk store of saved environments."""

    def __init__(self, root: str | Path, *, budget: int = 0) -> None:
        self.root = Path(root)
        self.budget = budget           # 0 = unbounded

    # -- layout -------------------------------------------------------------

    def dir_for(self, manifest: Manifest) -> Path:
        return (self.root / "packages" / _safe(manifest.os_name)
                / _safe(manifest.os_version) / _safe(manifest.package)
                / _safe(manifest.version_slice))

    def manifest_path(self, manifest: Manifest) -> Path:
        return self.dir_for(manifest) / f"{_safe(manifest.toolchain)}.json"

    # -- lookup -------------------------------------------------------------

    def find(self, wanted: Manifest, mode: str = Reuse.STRICT
             ) -> tuple[Manifest | None, str]:
        """Return a usable saved environment, or None and why not."""
        if mode == Reuse.OFF:
            return None, "reuse disabled"
        path = self.manifest_path(wanted)
        if not path.is_file():
            return None, "nothing saved for this package and toolchain"
        try:
            stored = Manifest(**json.loads(path.read_text()))
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            log.warning("ignoring unreadable manifest %s: %s", path, exc)
            return None, "saved manifest is unreadable"
        ok, reason = stored.matches(wanted, mode)
        if not ok:
            return None, reason
        return stored, ""

    # -- writing ------------------------------------------------------------

    def save(self, manifest: Manifest, payload_source: Path | None = None) -> Manifest:
        """Record an environment. A tarball is moved in; an image ref is noted.

        The manifest is written last and atomically: a manifest that exists
        promises a payload that exists, so a crash mid-save leaves no entry
        claiming something that was never finished.
        """
        target_dir = self.dir_for(manifest)
        target_dir.mkdir(parents=True, exist_ok=True)
        manifest.created = manifest.created or time.time()

        if payload_source is not None:
            payload_source = Path(payload_source)
            final = target_dir / payload_source.name
            partial = final.with_suffix(final.suffix + ".part")
            shutil.copy2(payload_source, partial)
            os.replace(partial, final)
            manifest.payload = final.name
            manifest.bytes = final.stat().st_size

        path = self.manifest_path(manifest)
        partial = path.with_suffix(".json.part")
        partial.write_text(json.dumps(asdict(manifest), indent=2, sort_keys=True))
        os.replace(partial, path)
        log.info("saved build environment for %s %s (%s, %s)",
                 manifest.package, manifest.package_version, manifest.toolchain,
                 _human(manifest.bytes) if manifest.bytes else manifest.payload)
        self.enforce_budget()
        return manifest

    def record_hit(self, manifest: Manifest) -> None:
        manifest.hits += 1
        path = self.manifest_path(manifest)
        if path.is_file():
            path.write_text(json.dumps(asdict(manifest), indent=2, sort_keys=True))

    # -- housekeeping -------------------------------------------------------

    def entries(self) -> list[Manifest]:
        out: list[Manifest] = []
        for path in sorted(self.root.glob("packages/*/*/*/*/*.json")):
            try:
                out.append(Manifest(**json.loads(path.read_text())))
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
        return out

    def total_bytes(self) -> int:
        return sum(m.bytes for m in self.entries())

    def enforce_budget(self) -> int:
        """Drop the least valuable saved environments until the store fits.

        Value is hits first, then recency. An environment reused nine times is
        worth more than a newer one that has never been reused - the whole
        point is to keep what gets used again.
        """
        if not self.budget:
            return 0
        entries = self.entries()
        total = sum(m.bytes for m in entries)
        if total <= self.budget:
            return 0
        entries.sort(key=lambda m: (m.hits, m.created))
        removed = 0
        for manifest in entries:
            if total <= self.budget:
                break
            removed += self._remove(manifest)
            total -= manifest.bytes
        if removed:
            log.info("env store over budget: removed %s", _human(removed))
        return removed

    def _remove(self, manifest: Manifest) -> int:
        target_dir = self.dir_for(manifest)
        freed = 0
        payload = target_dir / manifest.payload if manifest.payload else None
        if payload is not None and payload.is_file():
            freed = payload.stat().st_size
            payload.unlink()
        self.manifest_path(manifest).unlink(missing_ok=True)
        return freed


def _safe(part: str) -> str:
    """One path component, with nothing that could escape the store."""
    cleaned = "".join(c if c.isalnum() or c in "._+-" else "-" for c in part or "any")
    cleaned = cleaned.strip(".-") or "any"
    return cleaned[:96]


def _human(size: int) -> str:
    step = float(size)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if abs(step) < 1024 or unit == "GiB":
            return f"{step:.0f} {unit}" if unit == "B" else f"{step:.1f} {unit}"
        step /= 1024
    return f"{step:.1f} GiB"


__all__ = ["EnvStore", "Manifest", "Reuse", "slice_version"]
