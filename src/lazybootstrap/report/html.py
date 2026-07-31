"""Self-contained HTML report: the "GUI" side of the tool (D-15).

One file, CSS and JS inline, no network at load time. Open it with a double
click, attach it to a mail, publish it as a CI artifact - it works everywhere,
including on machines that have never heard of this project.
"""

from __future__ import annotations

import html
import json

from .. import util
from ..model import STATUS_ORDER, RunReport
from .compare import FIX, REGRESSION, Comparison

_STATUS_COLORS = {
    "ok": "#2f9e44", "fallback": "#f08c00", "failed": "#e03131",
    "timeout": "#c2255c", "nosource": "#868e96", "blocked": "#7048e8",
    "skipped": "#adb5bd",
}


def render(report: RunReport) -> str:
    stats = report.stats
    rows = []
    for result in sorted(report.results,
                         key=lambda r: (not r.status.is_failure, r.package.name)):
        rows.append({
            "package": result.package.name,
            "version": result.package.version,
            "source": result.package.source,
            "status": result.status.value,
            "toolchain": result.toolchain,
            "seconds": round(result.seconds, 2),
            "unit": result.unit,
            "error": util.tail(result.error, 6000),
            "steps": [
                {"name": s.name, "rc": s.rc, "seconds": round(s.seconds, 2),
                 "output": util.tail(s.output, 6000)}
                for a in result.attempts for s in a.steps
            ],
        })

    facts = [
        ("run id", report.run_id), ("image", report.image),
        ("distro", f"{report.distro} {report.environment.get('distro_version', '')}"),
        ("arch / libc", f"{report.environment.get('arch', '?')} / {report.environment.get('libc', '?')}"),
        ("backend", f"{report.backend} {report.environment.get('engine', '')}".strip()),
        ("toolchain", report.toolchain + (f" (fallback {report.fallback})" if report.fallback else "")),
        ("grouping", report.grouping), ("started", report.started_at),
        ("finished", report.finished_at),
    ]
    if report.environment.get("source_mirror"):
        facts.append(("source mirror", report.environment["source_mirror"]))

    bars = "".join(
        f'<div class="bar-seg" style="width:{(stats.by_status.get(s.value, 0) / max(stats.total, 1)) * 100:.3f}%;'
        f'background:{_STATUS_COLORS[s.value]}" title="{s.value}: {stats.by_status.get(s.value, 0)}"></div>'
        for s in STATUS_ORDER if stats.by_status.get(s.value, 0)
    )

    return _PAGE.format(
        title=f"lazy-bootstrap · {html.escape(report.toolchain)} · {html.escape(report.image)}",
        style=_STYLE,
        header=f"""
        <h1>lazy-bootstrap <span class="muted">/</span> {html.escape(report.toolchain)}</h1>
        <p class="sub">{html.escape(report.image)}</p>
        <div class="kpis">
          <div class="kpi"><b>{stats.success}</b><span>rebuilt</span></div>
          <div class="kpi"><b>{stats.success_rate:.1f}%</b><span>success</span></div>
          <div class="kpi"><b>{stats.total}</b><span>packages</span></div>
          <div class="kpi"><b>{html.escape(util.human_seconds(stats.seconds_total))}</b><span>build time</span></div>
          <div class="kpi"><b>{html.escape(util.human_seconds(stats.seconds_median))}</b><span>median</span></div>
        </div>
        <div class="bar">{bars}</div>
        <div class="legend">{_legend(stats)}</div>
        """,
        body=f"""
        <section>
          <h2>Run</h2>
          <table class="facts">{_facts(facts)}</table>
        </section>
        <section>
          <h2>Toolchains</h2>
          <table class="grid">
            <thead><tr><th>id</th><th>kind</th><th>from</th><th>version</th><th>probe</th></tr></thead>
            <tbody>{_toolchains(report)}</tbody>
          </table>
        </section>
        <section>
          <h2>Packages</h2>
          <div class="controls">
            <input id="q" type="search" placeholder="filter packages…" autocomplete="off">
            <span id="filters"></span>
            <span class="muted" id="count"></span>
          </div>
          <table class="grid" id="pkgs">
            <thead><tr>
              <th data-sort="package">package</th><th data-sort="status">status</th>
              <th data-sort="seconds">time</th><th data-sort="unit">unit</th><th></th>
            </tr></thead>
            <tbody></tbody>
          </table>
        </section>
        {_notes(report)}
        """,
        data=json.dumps({"rows": rows, "colors": _STATUS_COLORS}),
        script=_SCRIPT,
    )


def render_comparison(comparison: Comparison) -> str:
    labels = comparison.labels
    totals = comparison.totals()
    rows = [
        {
            "package": row.package,
            "statuses": {l: (row.statuses[l].value if l in row.statuses else None) for l in labels},
            "seconds": {l: round(row.seconds.get(l, 0.0), 2) for l in labels},
            "transitions": row.transitions,
        }
        for row in comparison.rows
    ]

    cards = []
    for label in labels:
        counts = totals[label]
        total = sum(counts.values()) or 1
        success = counts.get("ok", 0) + counts.get("fallback", 0)
        classes = comparison.transitions(label) if label != comparison.baseline else {}
        badge = " <span class='pill'>baseline</span>" if label == comparison.baseline else ""
        delta = ""
        if label != comparison.baseline:
            delta = (f"<div class='delta'><span class='bad'>−{len(classes.get(REGRESSION, []))} regressions</span>"
                     f" · <span class='good'>+{len(classes.get(FIX, []))} fixes</span></div>")
        cards.append(
            f"<div class='card'><h3>{html.escape(label)}{badge}</h3>"
            f"<div class='big'>{success}/{total}</div>"
            f"<div class='muted'>{success / total * 100:.1f}% success</div>{delta}</div>"
        )

    head = "".join(f"<th data-sort='{html.escape(l)}'>{html.escape(l)}</th>" for l in labels)
    return _PAGE.format(
        title="lazy-bootstrap · comparison",
        style=_STYLE,
        header=f"""
        <h1>lazy-bootstrap <span class="muted">/</span> comparison</h1>
        <p class="sub">{len(labels)} runs · baseline <code>{html.escape(comparison.baseline)}</code></p>
        <div class="cards">{''.join(cards)}</div>
        """,
        body=f"""
        <section>
          <h2>Matrix</h2>
          <div class="controls">
            <input id="q" type="search" placeholder="filter packages…" autocomplete="off">
            <label><input type="checkbox" id="only"> only changes (regressions / fixes)</label>
            <span class="muted" id="count"></span>
          </div>
          <table class="grid" id="matrix">
            <thead><tr><th data-sort="package">package</th>{head}</tr></thead>
            <tbody></tbody>
          </table>
        </section>
        """,
        data=json.dumps({"rows": rows, "labels": labels, "baseline": comparison.baseline,
                         "colors": _STATUS_COLORS}),
        script=_COMPARE_SCRIPT,
    )


# --- fragments --------------------------------------------------------------


def _facts(pairs: list[tuple[str, str]]) -> str:
    return "".join(f"<tr><th>{html.escape(k)}</th><td>{html.escape(str(v))}</td></tr>"
                   for k, v in pairs if v)


def _toolchains(report: RunReport) -> str:
    out = []
    for tc in report.toolchains:
        mark = "<span class='good'>ok</span>" if tc.ok else "<span class='bad'>failed</span>"
        detail = f"<pre class='detail'>{html.escape(util.tail(tc.detail, 2500))}</pre>" if tc.detail else ""
        out.append(
            f"<tr><td><code>{html.escape(tc.id)}</code></td><td>{html.escape(tc.kind)}</td>"
            f"<td>{html.escape(tc.source)}</td><td>{html.escape(tc.version)}</td><td>{mark}{detail}</td></tr>"
        )
    return "".join(out) or "<tr><td colspan='5' class='muted'>none recorded</td></tr>"


def _legend(stats) -> str:  # type: ignore[no-untyped-def]
    out = []
    for status in STATUS_ORDER:
        count = stats.by_status.get(status.value, 0)
        if count:
            out.append(f"<span class='dot' style='background:{_STATUS_COLORS[status.value]}'></span>"
                       f"{status.value} <b>{count}</b>")
    return " ".join(out)


def _notes(report: RunReport) -> str:
    if not report.notes:
        return ""
    items = "".join(f"<li>{html.escape(n)}</li>" for n in report.notes)
    return f"<section><h2>Notes</h2><ul class='notes'>{items}</ul></section>"


# --- page template ----------------------------------------------------------

_PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>{style}</style>
</head><body>
<header>{header}</header>
<main>{body}</main>
<script id="data" type="application/json">{data}</script>
<script>{script}</script>
</body></html>
"""

_STYLE = """
:root {
  --bg:#ffffff; --fg:#1a1c1e; --muted:#6b7280; --line:#e5e7eb; --panel:#f8f9fa;
  --good:#2f9e44; --bad:#e03131; --accent:#1971c2;
}
@media (prefers-color-scheme: dark) {
  :root { --bg:#16181c; --fg:#e6e8ea; --muted:#9aa3ad; --line:#2b2f36; --panel:#1d2026; }
}
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--fg); font:14px/1.5 ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif; }
header, main { max-width:1200px; margin:0 auto; padding:24px 20px; }
header { border-bottom:1px solid var(--line); }
h1 { margin:0 0 4px; font-size:22px; font-weight:650; }
h2 { font-size:16px; margin:32px 0 10px; }
h3 { font-size:14px; margin:0 0 6px; }
.sub { margin:0 0 16px; color:var(--muted); font-family:ui-monospace,SFMono-Regular,Menlo,monospace; }
.muted { color:var(--muted); font-weight:400; }
.good { color:var(--good); } .bad { color:var(--bad); }
.kpis, .cards { display:flex; gap:10px; flex-wrap:wrap; margin:14px 0; }
.kpi, .card { background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:10px 14px; }
.kpi b { display:block; font-size:20px; } .kpi span { color:var(--muted); font-size:12px; }
.card { min-width:170px; } .card .big { font-size:22px; font-weight:650; }
.pill { background:var(--accent); color:#fff; border-radius:99px; font-size:10px; padding:2px 7px; vertical-align:middle; }
.delta { margin-top:6px; font-size:12px; }
.bar { display:flex; height:9px; border-radius:99px; overflow:hidden; background:var(--line); margin:10px 0 8px; }
.bar-seg { height:100%; }
.legend { font-size:12px; color:var(--muted); display:flex; gap:14px; flex-wrap:wrap; }
.dot { display:inline-block; width:9px; height:9px; border-radius:3px; margin-right:5px; vertical-align:middle; }
table { border-collapse:collapse; width:100%; }
.facts th { text-align:left; color:var(--muted); font-weight:500; width:150px; padding:4px 10px 4px 0; vertical-align:top; }
.facts td { padding:4px 0; font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:12.5px; word-break:break-all; }
.grid { font-size:13px; }
.grid th { text-align:left; border-bottom:1px solid var(--line); padding:7px 8px; color:var(--muted); font-weight:500; cursor:pointer; white-space:nowrap; }
.grid td { border-bottom:1px solid var(--line); padding:6px 8px; vertical-align:top; }
.grid tbody tr:hover { background:var(--panel); }
.controls { display:flex; gap:12px; align-items:center; flex-wrap:wrap; margin:10px 0; }
input[type=search] { background:var(--bg); color:var(--fg); border:1px solid var(--line); border-radius:7px; padding:6px 10px; min-width:220px; font:inherit; }
.tag { display:inline-block; border-radius:5px; padding:1px 7px; color:#fff; font-size:11.5px; font-weight:600; }
.chip { border:1px solid var(--line); border-radius:99px; padding:2px 9px; font-size:12px; cursor:pointer; user-select:none; }
.chip.off { opacity:.35; }
pre { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:10px; overflow:auto; font-size:12px; margin:6px 0 0; max-height:420px; }
pre.detail { max-height:180px; }
code { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; }
.notes li { margin:4px 0; }
.expand { cursor:pointer; color:var(--accent); user-select:none; }
tr.detail-row td { background:var(--panel); }
"""

_SCRIPT = """
const DATA = JSON.parse(document.getElementById('data').textContent);
const tbody = document.querySelector('#pkgs tbody');
const q = document.getElementById('q');
const countEl = document.getElementById('count');
const hidden = new Set();
let sortKey = 'status', sortDir = 1;

const statuses = [...new Set(DATA.rows.map(r => r.status))];
document.getElementById('filters').innerHTML = statuses
  .map(s => `<span class="chip" data-s="${s}" style="border-color:${DATA.colors[s]}">${s}</span>`).join(' ');
document.getElementById('filters').addEventListener('click', e => {
  const chip = e.target.closest('.chip'); if (!chip) return;
  const s = chip.dataset.s;
  hidden.has(s) ? hidden.delete(s) : hidden.add(s);
  chip.classList.toggle('off', hidden.has(s));
  draw();
});

document.querySelectorAll('#pkgs th[data-sort]').forEach(th => th.onclick = () => {
  const key = th.dataset.sort;
  sortDir = (key === sortKey) ? -sortDir : 1;
  sortKey = key; draw();
});

function tag(s) { return `<span class="tag" style="background:${DATA.colors[s]}">${s}</span>`; }

function draw() {
  const needle = q.value.trim().toLowerCase();
  let rows = DATA.rows.filter(r => !hidden.has(r.status));
  if (needle) rows = rows.filter(r => r.package.toLowerCase().includes(needle)
                                    || (r.error || '').toLowerCase().includes(needle));
  rows.sort((a, b) => {
    const x = a[sortKey], y = b[sortKey];
    return (x < y ? -1 : x > y ? 1 : 0) * sortDir;
  });
  countEl.textContent = `${rows.length} / ${DATA.rows.length} packages`;
  tbody.innerHTML = rows.map((r, i) => `
    <tr data-i="${i}">
      <td><code>${r.package}</code>${r.source && r.source !== r.package ? ` <span class="muted">(${r.source})</span>` : ''}</td>
      <td>${tag(r.status)}</td>
      <td>${r.seconds}s</td>
      <td class="muted">${r.unit || ''}</td>
      <td>${(r.error || r.steps.length) ? '<span class="expand">details</span>' : ''}</td>
    </tr>`).join('');
  tbody.querySelectorAll('.expand').forEach(el => el.onclick = ev => {
    const tr = ev.target.closest('tr');
    const next = tr.nextElementSibling;
    if (next && next.classList.contains('detail-row')) { next.remove(); return; }
    const r = rows[+tr.dataset.i];
    const steps = r.steps.map(s =>
      `<b>${s.name}</b> rc=${s.rc} (${s.seconds}s)\\n${escapeHtml(s.output || '')}`).join('\\n\\n');
    const row = document.createElement('tr');
    row.className = 'detail-row';
    row.innerHTML = `<td colspan="5"><pre>${steps || escapeHtml(r.error)}</pre></td>`;
    tr.after(row);
  });
}
function escapeHtml(s) {
  return String(s).replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
}
q.addEventListener('input', draw);
draw();
"""

_COMPARE_SCRIPT = """
const DATA = JSON.parse(document.getElementById('data').textContent);
const tbody = document.querySelector('#matrix tbody');
const q = document.getElementById('q');
const only = document.getElementById('only');
const countEl = document.getElementById('count');
let sortKey = 'package', sortDir = 1;

document.querySelectorAll('#matrix th[data-sort]').forEach(th => th.onclick = () => {
  const key = th.dataset.sort;
  sortDir = (key === sortKey) ? -sortDir : 1;
  sortKey = key; draw();
});

function tag(s) {
  if (!s) return '<span class="muted">–</span>';
  return `<span class="tag" style="background:${DATA.colors[s]}">${s}</span>`;
}
function changed(r) {
  return Object.values(r.transitions || {}).some(t => t === 'regression' || t === 'fix');
}

function draw() {
  const needle = q.value.trim().toLowerCase();
  let rows = DATA.rows.slice();
  if (only.checked) rows = rows.filter(changed);
  if (needle) rows = rows.filter(r => r.package.toLowerCase().includes(needle));
  rows.sort((a, b) => {
    const x = sortKey === 'package' ? a.package : (a.statuses[sortKey] || '');
    const y = sortKey === 'package' ? b.package : (b.statuses[sortKey] || '');
    return (x < y ? -1 : x > y ? 1 : 0) * sortDir;
  });
  countEl.textContent = `${rows.length} / ${DATA.rows.length} packages`;
  tbody.innerHTML = rows.map(r => `
    <tr>
      <td><code>${r.package}</code></td>
      ${DATA.labels.map(l => {
        const t = r.transitions[l];
        const mark = t === 'regression' ? ' ⬇' : t === 'fix' ? ' ⬆' : '';
        return `<td>${tag(r.statuses[l])}${mark}</td>`;
      }).join('')}
    </tr>`).join('');
}
q.addEventListener('input', draw);
only.addEventListener('change', draw);
draw();
"""
