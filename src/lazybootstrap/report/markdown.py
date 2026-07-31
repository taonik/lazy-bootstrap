"""Markdown rendering: pasteable into an issue, a PR or a CI job summary."""

from __future__ import annotations

from .. import util
from ..model import STATUS_ORDER, RunReport

_EMOJI = {
    "ok": "✅", "fallback": "🔁", "failed": "❌", "timeout": "⏱",
    "nosource": "∅", "blocked": "⛔", "skipped": "·",
}


def render(report: RunReport, failures: int = 40) -> str:
    out: list[str] = []
    add = out.append
    stats = report.stats

    add(f"# lazy-bootstrap — `{report.toolchain}` on `{report.image}`")
    add("")
    add(f"**{stats.success}/{stats.total} packages rebuilt "
        f"({stats.success_rate:.1f}%)** · total {util.human_seconds(stats.seconds_total)} "
        f"· median {util.human_seconds(stats.seconds_median)}")
    add("")

    add("## Run")
    add("")
    add("| | |")
    add("|---|---|")
    add(f"| run id | `{report.run_id}` |")
    add(f"| image | `{report.image}` |")
    add(f"| distro | {report.distro} {report.environment.get('distro_version', '')} "
        f"({report.environment.get('arch', '?')}, {report.environment.get('libc', '?')}) |")
    add(f"| backend | {report.backend} {report.environment.get('engine', '')} |")
    add(f"| toolchain | `{report.toolchain}`"
        f"{f' (fallback `{report.fallback}`)' if report.fallback else ''} |")
    add(f"| grouping | {report.grouping} |")
    add(f"| started | {report.started_at} |")
    add(f"| finished | {report.finished_at} |")
    if report.environment.get("source_mirror"):
        add(f"| source mirror | `{report.environment['source_mirror']}` |")
    add("")

    if report.toolchains:
        add("## Toolchains")
        add("")
        add("| id | kind | provisioned from | version | probe |")
        add("|---|---|---|---|---|")
        for tc in report.toolchains:
            add(f"| `{tc.id}` | {tc.kind} | {tc.source} | {_cell(tc.version)} | "
                f"{'✅' if tc.ok else '❌'} |")
        add("")
        for tc in (t for t in report.toolchains if t.detail):
            add(f"<details><summary>{tc.id} details</summary>")
            add("")
            add("```")
            add(util.tail(tc.detail, 1500))
            add("```")
            add("</details>")
            add("")

    add("## Results")
    add("")
    add("| status | packages | share |")
    add("|---|---:|---:|")
    for status in STATUS_ORDER:
        count = stats.by_status.get(status.value, 0)
        if count:
            share = count / stats.total * 100 if stats.total else 0
            add(f"| {_EMOJI[status.value]} {status.value} | {count} | {share:.1f}% |")
    add("")

    bad = [r for r in report.results if r.status.is_failure]
    if bad:
        add(f"## Failures ({len(bad)})")
        add("")
        add("| package | status | time | first error |")
        add("|---|---|---:|---|")
        for result in bad[:failures]:
            add(f"| `{result.package.name}` | {_EMOJI[result.status.value]} {result.status.value} "
                f"| {util.human_seconds(result.seconds)} | {_cell(_error_line(result))} |")
        if len(bad) > failures:
            add("")
            add(f"_… and {len(bad) - failures} more; see `report.json`._")
        add("")

    if stats.slowest:
        add("## Slowest builds")
        add("")
        add("| package | time |")
        add("|---|---:|")
        for name, seconds in stats.slowest:
            add(f"| `{name}` | {util.human_seconds(seconds)} |")
        add("")

    if report.notes:
        add("## Notes")
        add("")
        for note in report.notes:
            add(f"- {note}")
        add("")
    return "\n".join(out)


def _error_line(result) -> str:  # type: ignore[no-untyped-def]
    for attempt in reversed(result.attempts):
        failed = attempt.failed_step
        if failed and failed.output:
            for line in reversed(failed.output.strip().splitlines()):
                if line.strip():
                    return line.strip()[:140]
        if attempt.error:
            return attempt.error.strip().splitlines()[-1][:140]
    return ""


def _cell(text: str) -> str:
    """Markdown tables break on pipes and newlines."""
    return (text or "").replace("|", "\\|").replace("\n", " ")[:200]
