"""Dependency registry, space budgeting, and the policy that prevents livelock.

Rebuilding a hundred packages prepares a hundred environments, and nearly all
of them install the same debhelper/dh-*/build-essential set. Downloading that
once instead of a hundred times is the point of this module.

Everything here is pure logic over sizes and names - no network, no disk. That
is deliberate: the interesting failures (a working set that cannot fit, two
units evicting each other forever) are policy failures, and policy that can
only be tested against a real archive does not get tested.

The three invariants named in docs/CACHE.md are enforced in `CacheBudget`:

    I1 admission - a unit runs only once its whole working set is reserved
    I2 pinning   - a running unit's working set cannot be evicted
    I3 bypass    - a working set larger than the budget never waits

Together they guarantee progress. See `CacheBudget.admit`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .orchestration.logs import get

log = get("cache")

#: Build trees, object files and the packaged result have no metadata anywhere
#: to read, so their size is guessed as a multiple of the source. Deliberately
#: generous: the estimate is used to decline work, and declining a package that
#: would have fitted is cheaper than dying with ENOSPC halfway through.
DEFAULT_BUILD_FACTOR = 8


@dataclass
class Dep:
    """One build-dependency, with whatever the package manager knows about it."""

    name: str
    version: str = ""
    download: int = 0          # bytes fetched
    installed: int = 0         # bytes once unpacked
    needed_by: set[str] = field(default_factory=set)

    @property
    def frequency(self) -> int:
        return len(self.needed_by)

    @property
    def key(self) -> str:
        return f"{self.name}={self.version}" if self.version else self.name


class DepRegistry:
    """Every unit's build-dependencies, merged and counted.

    The merge is the useful part: `debhelper` needed by eleven units is one
    entry with a frequency of eleven, not eleven entries. Frequency is what
    prefetch orders by, so the bytes that serve the most builds are fetched
    first.
    """

    def __init__(self) -> None:
        self._deps: dict[str, Dep] = {}
        self._units: dict[str, set[str]] = {}

    def add(self, unit: str, deps: list[Dep]) -> None:
        self._units.setdefault(unit, set())
        for dep in deps:
            existing = self._deps.get(dep.key)
            if existing is None:
                existing = Dep(name=dep.name, version=dep.version,
                               download=dep.download, installed=dep.installed)
                self._deps[dep.key] = existing
            # Sizes can arrive from different queries; keep the larger rather
            # than the newest, so a metadata gap never shrinks an estimate.
            existing.download = max(existing.download, dep.download)
            existing.installed = max(existing.installed, dep.installed)
            existing.needed_by.add(unit)
            self._units[unit].add(existing.key)

    def by_frequency(self) -> list[Dep]:
        """Most widely needed first; ties broken by smallest download.

        Smallest-first within a frequency band is not cosmetic: it maximises
        the number of dependencies resolved per byte spent, which matters most
        when the budget runs out partway down the list.
        """
        return sorted(self._deps.values(),
                      key=lambda d: (-d.frequency, d.download, d.name))

    def working_set(self, unit: str) -> list[Dep]:
        return [self._deps[k] for k in sorted(self._units.get(unit, ()))]

    def download_bytes(self, unit: str) -> int:
        return sum(d.download for d in self.working_set(unit))

    def installed_bytes(self, unit: str) -> int:
        return sum(d.installed for d in self.working_set(unit))

    def total_download_bytes(self) -> int:
        """What a job costs with no cache and no sharing - the number to beat."""
        return sum(d.download * d.frequency for d in self._deps.values())

    def unique_download_bytes(self) -> int:
        """What the same job costs when every dependency is fetched once."""
        return sum(d.download for d in self._deps.values())

    @property
    def units(self) -> list[str]:
        return sorted(self._units)

    def __len__(self) -> int:
        return len(self._deps)


class Admission(str, Enum):
    """What `CacheBudget.admit` decided."""

    ADMITTED = "admitted"   # reserved and pinned; the unit may run
    QUEUED = "queued"       # would fit eventually, but not right now
    BYPASS = "bypass"       # can never fit; run without the cache


@dataclass
class Entry:
    """A dependency present in the cache, and who is currently relying on it."""

    key: str
    size: int
    frequency: int = 0
    last_used: int = 0
    pinned_by: set[str] = field(default_factory=set)

    @property
    def pinned(self) -> bool:
        return bool(self.pinned_by)


class CacheBudget:
    """Admission, pinning and eviction over a fixed number of bytes.

    The failure this exists to prevent: two units whose working sets do not fit
    together, each evicting what the other is about to use. Both keep
    downloading, neither ever assembles a complete set, and the job makes no
    progress while looking busy. Naive LRU does exactly this.
    """

    def __init__(self, budget: int, *, name: str = "cache") -> None:
        if budget < 0:
            raise ValueError("budget cannot be negative")
        self.budget = budget
        self.name = name
        self._entries: dict[str, Entry] = {}
        self._reserved: dict[str, int] = {}     # unit -> bytes held
        self._clock = 0
        self.bypassed: list[str] = []

    # -- accounting ---------------------------------------------------------

    @property
    def reserved(self) -> int:
        return sum(self._reserved.values())

    @property
    def used(self) -> int:
        return sum(e.size for e in self._entries.values())

    @property
    def free(self) -> int:
        return max(0, self.budget - self.reserved)

    # -- I1 / I3 ------------------------------------------------------------

    def admit(self, unit: str, working_set: list[Dep]) -> Admission:
        """Decide whether `unit` may run now.

        I1: admitted only when the whole working set is reserved, so a running
            unit never competes with a peer for the last free byte.
        I3: a working set larger than the entire budget can never be reserved,
            so it is sent to bypass immediately instead of queueing forever on
            a condition that cannot become true.

        Progress follows: the first caller is always admitted - by I1 if it
        fits, by I3 if it does not - and an admitted unit's dependencies cannot
        be evicted (I2), so some unit always finishes and frees its
        reservation.
        """
        if unit in self._reserved:
            return Admission.ADMITTED
        need = sum(d.download for d in working_set)

        if need > self.budget:
            if unit not in self.bypassed:
                self.bypassed.append(unit)
                log.warning(
                    "%s: %s needs %s but the whole budget is %s - running it "
                    "without the cache rather than waiting for room that "
                    "cannot appear", self.name, unit,
                    human(need), human(self.budget))
            return Admission.BYPASS

        if need > self.free:
            # Room may exist once unpinned entries go; try before queueing.
            self._evict(need - self.free, protect=working_set)
        if need > self.free:
            return Admission.QUEUED

        self._reserved[unit] = need
        for dep in working_set:
            entry = self._entries.get(dep.key)
            if entry is None:
                entry = Entry(key=dep.key, size=dep.download)
                self._entries[dep.key] = entry
            entry.frequency = max(entry.frequency, dep.frequency)
            entry.pinned_by.add(unit)
            self._clock += 1
            entry.last_used = self._clock
        return Admission.ADMITTED

    def release(self, unit: str) -> None:
        """Unit finished: drop its reservation and unpin what it held."""
        self._reserved.pop(unit, None)
        for entry in self._entries.values():
            entry.pinned_by.discard(unit)

    # -- I2 -----------------------------------------------------------------

    def _evict(self, wanted: int, protect: list[Dep] | None = None) -> int:
        """Free up to `wanted` bytes from unpinned entries only (I2).

        Least valuable first: lowest frequency, then least recently used. What
        eleven units still need is the last thing to go. Entries in `protect`
        are excluded even when unpinned - evicting something we are about to
        pin is the thrash this class exists to avoid.
        """
        keep = {d.key for d in (protect or ())}
        candidates = [e for e in self._entries.values()
                      if not e.pinned and e.key not in keep]
        candidates.sort(key=lambda e: (e.frequency, e.last_used))
        freed = 0
        for entry in candidates:
            if freed >= wanted:
                break
            del self._entries[entry.key]
            freed += entry.size
        if freed:
            log.debug("%s: evicted %s across %d entries",
                      self.name, human(freed), len(candidates))
        return freed

    def touch(self, key: str) -> None:
        entry = self._entries.get(key)
        if entry is not None:
            self._clock += 1
            entry.last_used = self._clock

    def holds(self, key: str) -> bool:
        return key in self._entries


class AdmissionQueue:
    """FIFO with aging, so a large unit is not overtaken forever.

    Livelock and starvation are different problems and get different fixes.
    `CacheBudget` guarantees that *someone* progresses; without aging that
    someone can be a stream of small units while one large unit waits for a
    gap that a steady arrival rate never leaves.
    """

    def __init__(self, budget: CacheBudget, *, max_skips: int = 3) -> None:
        self.budget = budget
        self.max_skips = max_skips
        self._skips: dict[str, int] = {}

    def next_admissible(self, pending: list[tuple[str, list[Dep]]]
                        ) -> tuple[str, Admission] | None:
        """Pick the next unit to run from `pending`, in order.

        A unit that has been skipped `max_skips` times blocks the queue behind
        it: nothing later is admitted until it gets its turn. That trades a
        little throughput for the guarantee that every unit eventually runs.
        """
        for unit, working_set in pending:
            decision = self.budget.admit(unit, working_set)
            if decision in (Admission.ADMITTED, Admission.BYPASS):
                self._skips.pop(unit, None)
                return unit, decision
            self._skips[unit] = self._skips.get(unit, 0) + 1
            if self._skips[unit] >= self.max_skips:
                log.info("cache: %s has been passed over %d times; holding the "
                         "queue until it fits", unit, self._skips[unit])
                return None
        return None


# -- environment space ------------------------------------------------------

@dataclass
class SpaceEstimate:
    """What a unit is expected to need, and whether that is affordable.

    Every field is an estimate from package metadata and is reported as one.
    Estimates are used to decline work, never to promise it.
    """

    deps_installed: int
    source: int
    build_factor: int = DEFAULT_BUILD_FACTOR

    @property
    def required(self) -> int:
        return self.deps_installed + self.source + self.build_factor * self.source

    def fits_in(self, available: int) -> bool:
        return self.required <= available

    def explain(self, available: int) -> str:
        return (f"needs about {human(self.required)} "
                f"({human(self.deps_installed)} build-deps + "
                f"{human(self.source)} source x{self.build_factor + 1} for the "
                f"build), {human(available)} available")


def human(size: int) -> str:
    """Bytes as something a person can compare at a glance."""
    step = float(size)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if abs(step) < 1024 or unit == "GiB":
            return f"{step:.0f} {unit}" if unit == "B" else f"{step:.1f} {unit}"
        step /= 1024
    return f"{step:.1f} GiB"


__all__ = ["Dep", "DepRegistry", "Admission", "CacheBudget", "AdmissionQueue",
           "SpaceEstimate", "Entry", "human", "DEFAULT_BUILD_FACTOR"]
