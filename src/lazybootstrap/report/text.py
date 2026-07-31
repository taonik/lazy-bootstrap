"""Plain-text rendering: what you want on a terminal right after a run."""

from __future__ import annotations

from ..orchestration import util
from ..model import STATUS_ORDER, RunReport, Status

_MARK = {
    Status.OK: "✓",
    Status.FALLBACK: "↻",
    Status.FAILED: "✗",
    Status.TIMEOUT: "⏱",
    Status.NOSOURCE: "∅",
    Status.BLOCKED: "⛔",
    Status.SKIPPED: "·",
}


def render(report: RunReport, failures: int = 20, verbose: bool = False) -> str:
    out: list[str] = []
    add = out.append

    add(_rule())
    add(f"lazy-bootstrap run {report.run_id}")
    add(_rule())
    add(f"  image      : {report.image}")
    add(f"  distro     : {report.distro} {report.environment.get('distro_version', '')}"
        f" ({report.environment.get('arch', '?')}, {report.environment.get('libc', '?')})")
    add(f"  backend    : {report.backend}"
        f"{'/' + report.environment.get('engine', '') if report.environment.get('engine') else ''}")
    add(f"  toolchain  : {report.toolchain}"
        f"{f'  (fallback: {report.fallback})' if report.fallback else ''}")
    add(f"  grouping   : {report.grouping}")
    add(f"  started    : {report.started_at}    finished: {report.finished_at}")
    if report.environment.get("source_mirror"):
        add(f"  src mirror : {report.environment['source_mirror']}")
    add("")

    # -- toolchains --------------------------------------------------------
    if report.toolchains:
        add("Toolchains")
        for tc in report.toolchains:
            mark = "ok " if tc.ok else "FAIL"
            add(f"  [{mark}] {tc.id:<16} {tc.kind:<6} {tc.source:<12} {tc.version}")
            if tc.detail and (verbose or not tc.ok):
                add(util.indent(util.tail(tc.detail, 800), "         "))
        add("")

    # -- summary -----------------------------------------------------------
    stats = report.stats
    add("Summary")
    add(f"  packages   : {stats.total}")
    for status in STATUS_ORDER:
        count = stats.by_status.get(status.value, 0)
        if count:
            add(f"    {_MARK[status]} {status.value:<10} {count:>5}"
                f"   {count / stats.total * 100:5.1f}%" if stats.total else "")
    add(f"  success    : {stats.success}/{stats.total} ({stats.success_rate:.1f}%)")
    add(f"  build time : total {util.human_seconds(stats.seconds_total)}, "
        f"median {util.human_seconds(stats.seconds_median)}")
    if stats.slowest:
        add("  slowest    : " + ", ".join(f"{n} ({util.human_seconds(s)})"
                                          for n, s in stats.slowest[:5]))
    add("")

    # -- failures ----------------------------------------------------------
    bad = [r for r in report.results if r.status.is_failure]
    if bad:
        add(f"Failures ({len(bad)})")
        for result in bad[:failures]:
            add(f"  {_MARK[result.status]} {result.package.name:<32} {result.status.value:<9}"
                f" {util.human_seconds(result.seconds)}")
            reason = _first_error_line(result)
            if reason:
                add(f"      {reason}")
        if len(bad) > failures:
            add(f"  ... and {len(bad) - failures} more (see the JSON or HTML report)")
        add("")

    if report.notes:
        add("Notes")
        for note in report.notes:
            add(f"  - {note}")
        add("")
    return "\n".join(out)


def _first_error_line(result) -> str:  # type: ignore[no-untyped-def]
    for attempt in reversed(result.attempts):
        failed = attempt.failed_step
        if failed and failed.output:
            for line in reversed(failed.output.strip().splitlines()):
                stripped = line.strip()
                if stripped and not stripped.startswith(("make[", "make:")):
                    return stripped[:160]
        if attempt.error:
            return attempt.error.strip().splitlines()[-1][:160]
    return ""


def _rule(width: int = 78) -> str:
    return "=" * width
