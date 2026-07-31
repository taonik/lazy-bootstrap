"""Result model.

Every command produces (or reads) a `RunReport`. The JSON serialisation of
that object is the single source of truth: the text/markdown/HTML renderers and
the run comparator all consume it, and nothing else. Keeping one canonical
document means a report can be re-rendered months later without re-running the
build.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable

SCHEMA_VERSION = 1


class Status(str, Enum):
    """Outcome of a single package (or of a whole run)."""

    OK = "ok"                  # rebuilt with the requested toolchain
    FALLBACK = "fallback"      # requested toolchain failed, fallback succeeded
    FAILED = "failed"          # build error
    TIMEOUT = "timeout"        # exceeded the per-package time budget
    NOSOURCE = "nosource"      # no source package available for this binary
    BLOCKED = "blocked"        # environment could not fetch what it needed
    SKIPPED = "skipped"        # excluded by filters or by a failed prerequisite

    @property
    def is_success(self) -> bool:
        return self in (Status.OK, Status.FALLBACK)

    @property
    def is_failure(self) -> bool:
        return self in (Status.FAILED, Status.TIMEOUT, Status.BLOCKED, Status.NOSOURCE)


# Order used by every renderer, so tables always list statuses the same way.
STATUS_ORDER = [
    Status.OK,
    Status.FALLBACK,
    Status.FAILED,
    Status.TIMEOUT,
    Status.NOSOURCE,
    Status.BLOCKED,
    Status.SKIPPED,
]


@dataclass
class PackageRef:
    """One installed package as reported by the image's package manager."""

    name: str
    version: str = ""
    arch: str = ""
    source_name: str = ""
    source_version: str = ""
    repo: str = ""

    @property
    def source(self) -> str:
        return self.source_name or self.name

    @property
    def key(self) -> str:
        return f"{self.name}={self.version}" if self.version else self.name


@dataclass
class StepLog:
    """One shell step inside a package build (fetch / deps / build / ...)."""

    name: str
    rc: int
    seconds: float
    command: str = ""
    output: str = ""      # already tail-trimmed by the caller
    #: False for steps that may fail without dooming the unit (an index refresh
    #: where one third-party repository is unreachable, say).
    fatal: bool = True


@dataclass
class Attempt:
    """One (package, toolchain) build attempt. A package may have two: the
    requested toolchain and, if that failed, the fallback."""

    toolchain: str
    status: Status
    seconds: float
    steps: list[StepLog] = field(default_factory=list)
    error: str = ""

    @property
    def failed_step(self) -> StepLog | None:
        return next((s for s in self.steps if s.rc != 0), None)


@dataclass
class PackageResult:
    package: PackageRef
    status: Status
    toolchain: str                       # toolchain that produced the final status
    attempts: list[Attempt] = field(default_factory=list)
    seconds: float = 0.0
    artifacts: list[str] = field(default_factory=list)
    unit: str = ""                       # build unit / group this package ran in

    @property
    def error(self) -> str:
        for attempt in reversed(self.attempts):
            if attempt.error:
                return attempt.error
        return ""


@dataclass
class ToolchainReport:
    """Provisioning + probe outcome for one toolchain, recorded once per run."""

    id: str
    kind: str
    version: str = ""
    source: str = ""        # distro | binary | source | preinstalled
    cc: str = ""
    cxx: str = ""
    ok: bool = False
    detail: str = ""


@dataclass
class Stats:
    total: int = 0
    by_status: dict[str, int] = field(default_factory=dict)
    seconds_total: float = 0.0
    seconds_median: float = 0.0
    slowest: list[tuple[str, float]] = field(default_factory=list)

    @property
    def success(self) -> int:
        return self.by_status.get(Status.OK.value, 0) + self.by_status.get(Status.FALLBACK.value, 0)

    @property
    def success_rate(self) -> float:
        return (self.success / self.total * 100.0) if self.total else 0.0


@dataclass
class RunReport:
    run_id: str
    started_at: str
    finished_at: str = ""
    image: str = ""
    distro: str = ""
    backend: str = ""
    toolchain: str = ""                  # the toolchain this run targeted
    fallback: str = ""
    grouping: str = ""
    label: str = ""                      # free-form, used as the column name in comparisons
    toolchains: list[ToolchainReport] = field(default_factory=list)
    results: list[PackageResult] = field(default_factory=list)
    stats: Stats = field(default_factory=Stats)
    environment: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    schema_version: int = SCHEMA_VERSION

    # -- statistics ---------------------------------------------------------

    def recompute_stats(self) -> Stats:
        durations = sorted(r.seconds for r in self.results)
        by_status: dict[str, int] = {}
        for result in self.results:
            by_status[result.status.value] = by_status.get(result.status.value, 0) + 1
        slowest = sorted(((r.package.name, r.seconds) for r in self.results),
                         key=lambda kv: kv[1], reverse=True)[:10]
        self.stats = Stats(
            total=len(self.results),
            by_status=by_status,
            seconds_total=round(sum(durations), 2),
            seconds_median=round(_median(durations), 2),
            slowest=[(name, round(sec, 2)) for name, sec in slowest],
        )
        return self.stats

    def result_for(self, package_name: str) -> PackageResult | None:
        return next((r for r in self.results if r.package.name == package_name), None)

    # -- serialisation ------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return _asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RunReport":
        report = cls(
            run_id=data.get("run_id", ""),
            started_at=data.get("started_at", ""),
            finished_at=data.get("finished_at", ""),
            image=data.get("image", ""),
            distro=data.get("distro", ""),
            backend=data.get("backend", ""),
            toolchain=data.get("toolchain", ""),
            fallback=data.get("fallback", ""),
            grouping=data.get("grouping", ""),
            label=data.get("label", ""),
            environment=data.get("environment", {}) or {},
            notes=list(data.get("notes", []) or []),
            schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
        )
        report.toolchains = [ToolchainReport(**tc) for tc in data.get("toolchains", []) or []]
        report.results = [_result_from_dict(r) for r in data.get("results", []) or []]
        raw_stats = data.get("stats") or {}
        report.stats = Stats(
            total=raw_stats.get("total", len(report.results)),
            by_status=raw_stats.get("by_status", {}) or {},
            seconds_total=raw_stats.get("seconds_total", 0.0),
            seconds_median=raw_stats.get("seconds_median", 0.0),
            slowest=[tuple(item) for item in raw_stats.get("slowest", []) or []],
        )
        if not raw_stats:
            report.recompute_stats()
        return report


# --- helpers ----------------------------------------------------------------


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    mid = len(values) // 2
    if len(values) % 2:
        return values[mid]
    return (values[mid - 1] + values[mid]) / 2.0


def _asdict(obj: Any) -> Any:
    """dataclasses.asdict + Enum flattening, so json.dumps just works."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        out = {f.name: _asdict(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
        if isinstance(obj, RunReport):
            out["stats"] = _asdict(obj.stats)
        return out
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, dict):
        return {k: _asdict(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_asdict(v) for v in obj]
    return obj


def _result_from_dict(data: dict[str, Any]) -> PackageResult:
    return PackageResult(
        package=PackageRef(**data.get("package", {})),
        status=Status(data.get("status", "skipped")),
        toolchain=data.get("toolchain", ""),
        attempts=[
            Attempt(
                toolchain=a.get("toolchain", ""),
                status=Status(a.get("status", "skipped")),
                seconds=a.get("seconds", 0.0),
                steps=[StepLog(**s) for s in a.get("steps", []) or []],
                error=a.get("error", ""),
            )
            for a in data.get("attempts", []) or []
        ],
        seconds=data.get("seconds", 0.0),
        artifacts=list(data.get("artifacts", []) or []),
        unit=data.get("unit", ""),
    )


def summarise(results: Iterable[PackageResult]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for result in results:
        counts[result.status.value] = counts.get(result.status.value, 0) + 1
    return counts
