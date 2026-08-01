"""Downloading a job's dependencies ahead of the builds that need them.

This is the fallback for having no external cacher, and it defers to one when
there is: see `should_run` below. Two mechanisms doing the same work is not
twice as good, it is the same bytes stored twice.

Order comes from `DepRegistry.by_frequency()`, admission from `CacheBudget`, so
the guarantees in docs/CACHE.md hold here too - in particular, prefetch never
holds budget against a unit that is trying to build. It is best-effort by
construction: every failure is logged and dropped, because a dependency that
fails to prefetch is simply fetched by the build that needs it, the way it
would have been anyway.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from .cache import CacheBudget, Dep, DepRegistry, human
from .orchestration.logs import get

log = get("prefetch")

#: More than a handful of parallel fetches starves the builds of the bandwidth
#: they are waiting on, which is the opposite of the point.
DEFAULT_CONCURRENCY = 4


def should_run(package_cache: str, explicit: bool | None) -> tuple[bool, str]:
    """Decide whether prefetching is worth doing at all.

    An external cacher already deduplicates, across runs and across machines,
    which is strictly better than a per-run prefetch: it survives the run.
    Prefetching alongside it stores the same bytes a second time and competes
    for the same bandwidth. So a configured cacher turns prefetch off unless
    the user asked for it by name.
    """
    if explicit is not None:
        if explicit and package_cache:
            return True, "asked for explicitly, alongside the package cache"
        return explicit, "asked for explicitly"
    if package_cache:
        return False, ("a package cache is configured, which already "
                       "deduplicates across runs - prefetch would store the "
                       "same bytes twice")
    return True, "no package cache configured"


@dataclass
class PrefetchStats:
    """What the prefetcher managed to do, for the report."""

    considered: int = 0
    fetched: int = 0
    skipped: int = 0
    failed: int = 0
    bytes_fetched: int = 0
    reason: str = ""
    order: list[str] = field(default_factory=list)

    def summary(self) -> str:
        if not self.considered:
            return f"prefetch not run: {self.reason}"
        return (f"prefetch: {self.fetched}/{self.considered} dependencies, "
                f"{human(self.bytes_fetched)}"
                + (f", {self.failed} failed" if self.failed else ""))


class Prefetcher:
    """Fetches dependencies in frequency order, inside a budget, in the background.

    `fetch_one` is injected rather than hard-coded so the policy can be tested
    without a network: the interesting behaviour here is ordering, budgeting and
    yielding, none of which needs a real download to exercise.
    """

    def __init__(self, registry: DepRegistry, budget: CacheBudget, fetch_one,
                 *, concurrency: int = DEFAULT_CONCURRENCY) -> None:
        self.registry = registry
        self.budget = budget
        self.fetch_one = fetch_one
        self.concurrency = max(1, concurrency)
        self.stats = PrefetchStats()
        self._stop = threading.Event()
        self._pool: ThreadPoolExecutor | None = None
        self._lock = threading.Lock()

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        """Begin fetching in the background. Returns immediately."""
        candidates = self._plan()
        self.stats.considered = len(candidates)
        self.stats.order = [d.key for d in candidates]
        if not candidates:
            return
        self._pool = ThreadPoolExecutor(max_workers=self.concurrency,
                                        thread_name_prefix="lb-prefetch")
        for dep in candidates:
            self._pool.submit(self._fetch, dep)

    def stop(self, wait: bool = False) -> PrefetchStats:
        """Stop fetching. Called when the run ends, or when a build needs room."""
        self._stop.set()
        if self._pool is not None:
            self._pool.shutdown(wait=wait, cancel_futures=True)
            self._pool = None
        return self.stats

    def yield_to_builds(self) -> None:
        """Give up remaining prefetch work: a build needs the budget more."""
        if not self._stop.is_set():
            log.info("prefetch yielding: a build needs the space")
        self.stop(wait=False)

    # -- internals ----------------------------------------------------------

    def _plan(self) -> list[Dep]:
        """Frequency order, trimmed to what the budget can hold.

        Deliberately not admitted through `CacheBudget.admit`: that reserves
        and pins on behalf of a *running unit*, and prefetch is not one. Taking
        a reservation here is exactly how a background task starves the work it
        is supposed to be helping.
        """
        planned: list[Dep] = []
        room = self.budget.budget
        for dep in self.registry.by_frequency():
            if dep.frequency < 2 and len(planned) >= 1:
                # Something only one unit wants is no cheaper to fetch early;
                # spend the budget on what several units share.
                continue
            if dep.download > room:
                continue
            planned.append(dep)
            room -= dep.download
        return planned

    def _fetch(self, dep: Dep) -> None:
        if self._stop.is_set():
            with self._lock:
                self.stats.skipped += 1
            return
        try:
            got = self.fetch_one(dep)
        except Exception as exc:                      # noqa: BLE001
            # Best-effort by design: the build that needs this will fetch it.
            log.debug("prefetch of %s failed: %s", dep.key, exc)
            with self._lock:
                self.stats.failed += 1
            return
        with self._lock:
            if got:
                self.stats.fetched += 1
                self.stats.bytes_fetched += dep.download
            else:
                self.stats.skipped += 1


__all__ = ["Prefetcher", "PrefetchStats", "should_run", "DEFAULT_CONCURRENCY"]
