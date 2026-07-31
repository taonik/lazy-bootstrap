"""Compare several runs (D-16).

The interesting question when comparing toolchains is not "which percentage is
higher" but "what broke". So the comparison is a package x run matrix plus a
classification of every transition against the first run, which is treated as
the baseline (by convention: the default toolchain).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..model import RunReport, Status

# Transition classes, relative to the baseline run.
REGRESSION = "regression"    # baseline ok  -> this run failed
FIX = "fix"                  # baseline bad -> this run ok
STABLE_OK = "stable-ok"
STABLE_FAIL = "stable-fail"
NEW = "new"                  # absent from the baseline
GONE = "gone"                # present in the baseline, absent here


@dataclass
class Row:
    package: str
    statuses: dict[str, Status] = field(default_factory=dict)   # label -> status
    seconds: dict[str, float] = field(default_factory=dict)
    transitions: dict[str, str] = field(default_factory=dict)   # label -> class

    def status(self, label: str) -> Status | None:
        return self.statuses.get(label)


@dataclass
class Comparison:
    labels: list[str]
    baseline: str
    rows: list[Row]
    reports: dict[str, RunReport]

    # -- aggregates ---------------------------------------------------------

    def totals(self) -> dict[str, dict[str, int]]:
        """label -> {status: count}"""
        out: dict[str, dict[str, int]] = {label: {} for label in self.labels}
        for row in self.rows:
            for label, status in row.statuses.items():
                out[label][status.value] = out[label].get(status.value, 0) + 1
        return out

    def transitions(self, label: str) -> dict[str, list[str]]:
        """class -> package names, for one non-baseline run."""
        out: dict[str, list[str]] = {}
        for row in self.rows:
            kind = row.transitions.get(label)
            if kind:
                out.setdefault(kind, []).append(row.package)
        return out

    def regressions(self, label: str) -> list[str]:
        return self.transitions(label).get(REGRESSION, [])

    def summary_lines(self) -> list[str]:
        lines = [f"baseline: {self.baseline}"]
        totals = self.totals()
        for label in self.labels:
            counts = totals[label]
            total = sum(counts.values())
            success = counts.get("ok", 0) + counts.get("fallback", 0)
            extra = ""
            if label != self.baseline:
                classes = self.transitions(label)
                extra = (f"  regressions={len(classes.get(REGRESSION, []))}"
                         f" fixes={len(classes.get(FIX, []))}")
            rate = success / total * 100 if total else 0.0
            lines.append(f"  {label:<20} {success:>4}/{total:<4} ({rate:5.1f}%){extra}")
        return lines


def compare(reports: list[RunReport], baseline: str = "") -> Comparison:
    """Build the package x run matrix. Labels come from the reports themselves."""
    labels: list[str] = []
    by_label: dict[str, RunReport] = {}
    for report in reports:
        label = report.label or report.toolchain or report.run_id
        # Two runs of the same toolchain would collide; disambiguate with the id.
        if label in by_label:
            label = f"{label}@{report.run_id[-6:]}"
        labels.append(label)
        by_label[label] = report

    base = baseline if baseline in labels else (labels[0] if labels else "")

    packages: list[str] = []
    seen: set[str] = set()
    for label in labels:
        for result in by_label[label].results:
            if result.package.name not in seen:
                seen.add(result.package.name)
                packages.append(result.package.name)
    packages.sort()

    rows: list[Row] = []
    for name in packages:
        row = Row(package=name)
        for label in labels:
            result = by_label[label].result_for(name)
            if result is None:
                continue
            row.statuses[label] = result.status
            row.seconds[label] = result.seconds
        base_status = row.statuses.get(base)
        for label in labels:
            if label == base:
                continue
            row.transitions[label] = _classify(base_status, row.statuses.get(label))
        rows.append(row)

    return Comparison(labels=labels, baseline=base, rows=rows, reports=by_label)


def _classify(base: Status | None, other: Status | None) -> str:
    if other is None:
        return GONE
    if base is None:
        return NEW
    if base.is_success and other.is_success:
        return STABLE_OK
    if base.is_success and not other.is_success:
        return REGRESSION
    if not base.is_success and other.is_success:
        return FIX
    return STABLE_FAIL


# --- renderers --------------------------------------------------------------


def render_text(comparison: Comparison, limit: int = 60) -> str:
    from .. import util

    out: list[str] = ["=" * 78, "lazy-bootstrap comparison", "=" * 78]
    out += comparison.summary_lines()
    out.append("")

    width = max((len(row.package) for row in comparison.rows), default=10)
    width = min(max(width, 12), 44)
    header = "package".ljust(width) + "  " + "  ".join(f"{label[:12]:<12}" for label in comparison.labels)
    out.append(header)
    out.append("-" * len(header))

    interesting = [r for r in comparison.rows
                   if any(t in (REGRESSION, FIX) for t in r.transitions.values())]
    shown = interesting or comparison.rows
    for row in shown[:limit]:
        cells = []
        for label in comparison.labels:
            status = row.statuses.get(label)
            cells.append(f"{(status.value if status else '-'):<12}")
        out.append(row.package[:width].ljust(width) + "  " + "  ".join(cells))
    if len(shown) > limit:
        out.append(f"... and {len(shown) - limit} more rows")
    out.append("")

    for label in comparison.labels:
        if label == comparison.baseline:
            continue
        classes = comparison.transitions(label)
        regressions = classes.get(REGRESSION, [])
        fixes = classes.get(FIX, [])
        out.append(f"{label} vs {comparison.baseline}:")
        out.append(f"  regressions ({len(regressions)}): "
                   + (", ".join(regressions[:15]) or "none")
                   + (" ..." if len(regressions) > 15 else ""))
        out.append(f"  fixes       ({len(fixes)}): "
                   + (", ".join(fixes[:15]) or "none")
                   + (" ..." if len(fixes) > 15 else ""))
        out.append("")
    _ = util
    return "\n".join(out)


def render_markdown(comparison: Comparison, limit: int = 100) -> str:
    emoji = {"ok": "✅", "fallback": "🔁", "failed": "❌", "timeout": "⏱",
             "nosource": "∅", "blocked": "⛔", "skipped": "·"}
    out: list[str] = ["# lazy-bootstrap — toolchain comparison", ""]

    totals = comparison.totals()
    out += ["| run | ok | fallback | failed | blocked | other | success |",
            "|---|---:|---:|---:|---:|---:|---:|"]
    for label in comparison.labels:
        counts = totals[label]
        total = sum(counts.values()) or 1
        other = sum(v for k, v in counts.items()
                    if k not in ("ok", "fallback", "failed", "blocked"))
        success = counts.get("ok", 0) + counts.get("fallback", 0)
        marker = " *(baseline)*" if label == comparison.baseline else ""
        out.append(f"| `{label}`{marker} | {counts.get('ok', 0)} | {counts.get('fallback', 0)} "
                   f"| {counts.get('failed', 0)} | {counts.get('blocked', 0)} | {other} "
                   f"| {success / total * 100:.1f}% |")
    out.append("")

    for label in comparison.labels:
        if label == comparison.baseline:
            continue
        classes = comparison.transitions(label)
        out.append(f"## `{label}` vs `{comparison.baseline}`")
        out.append("")
        out.append(f"- regressions: **{len(classes.get(REGRESSION, []))}** "
                   + (", ".join(f"`{p}`" for p in classes.get(REGRESSION, [])[:25]) or "_none_"))
        out.append(f"- fixes: **{len(classes.get(FIX, []))}** "
                   + (", ".join(f"`{p}`" for p in classes.get(FIX, [])[:25]) or "_none_"))
        out.append("")

    out += ["## Matrix", ""]
    out.append("| package | " + " | ".join(f"`{l}`" for l in comparison.labels) + " |")
    out.append("|---|" + "---|" * len(comparison.labels))
    interesting = [r for r in comparison.rows
                   if any(t in (REGRESSION, FIX) for t in r.transitions.values())]
    for row in (interesting or comparison.rows)[:limit]:
        cells = []
        for label in comparison.labels:
            status = row.statuses.get(label)
            cells.append(f"{emoji.get(status.value, '?')} {status.value}" if status else "–")
        out.append(f"| `{row.package}` | " + " | ".join(cells) + " |")
    out.append("")
    return "\n".join(out)
