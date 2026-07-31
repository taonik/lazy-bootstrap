"""Renderers. All of them consume a RunReport (or a Comparison) and nothing else."""

from __future__ import annotations

import json
from pathlib import Path

from ..orchestration import util
from ..model import RunReport
from . import compare as compare_mod
from . import html as html_mod
from . import markdown as md_mod
from . import text as text_mod

FORMATS = ("text", "md", "json", "html")


def render(report: RunReport, fmt: str = "text") -> str:
    if fmt in ("text", "txt"):
        return text_mod.render(report)
    if fmt in ("md", "markdown"):
        return md_mod.render(report)
    if fmt == "json":
        return json.dumps(report.to_dict(), indent=2) + "\n"
    if fmt == "html":
        return html_mod.render(report)
    raise ValueError(f"unknown format {fmt!r}; known: {', '.join(FORMATS)}")


def render_comparison(comparison: compare_mod.Comparison, fmt: str = "text") -> str:
    if fmt in ("text", "txt"):
        return compare_mod.render_text(comparison)
    if fmt in ("md", "markdown"):
        return compare_mod.render_markdown(comparison)
    if fmt == "html":
        return html_mod.render_comparison(comparison)
    if fmt == "json":
        return json.dumps({
            "baseline": comparison.baseline,
            "labels": comparison.labels,
            "totals": comparison.totals(),
            "rows": [
                {
                    "package": row.package,
                    "statuses": {k: v.value for k, v in row.statuses.items()},
                    "seconds": row.seconds,
                    "transitions": row.transitions,
                }
                for row in comparison.rows
            ],
        }, indent=2) + "\n"
    raise ValueError(f"unknown format {fmt!r}; known: {', '.join(FORMATS)}")


def write_all(report: RunReport, out_dir: str | Path) -> dict[str, Path]:
    """Write every format next to the canonical JSON: consultation and export
    are the same artefacts, just different files."""
    directory = util.ensure_dir(Path(out_dir))
    written: dict[str, Path] = {}
    for fmt, name in (("json", "report.json"), ("text", "report.txt"),
                      ("md", "report.md"), ("html", "report.html")):
        written[fmt] = util.write_text_atomic(directory / name, render(report, fmt))
    return written


__all__ = ["FORMATS", "compare_mod", "render", "render_comparison", "write_all"]
