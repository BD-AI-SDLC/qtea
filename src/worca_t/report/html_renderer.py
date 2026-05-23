"""Stdlib-only HTML report renderer. No Jinja2."""

from __future__ import annotations

import base64
from pathlib import Path
from string import Template

from worca_t.report.data_builder import RunReport

_STATUS_COLORS = {
    "passed": "#22c55e",
    "failed": "#ef4444",
    "skipped": "#9ca3af",
    "error": "#f97316",
}

_SEVERITY_COLORS = {
    "critical": "#dc2626",
    "major": "#ea580c",
    "minor": "#eab308",
    "cosmetic": "#6b7280",
}

_PAGE_TEMPLATE = Template("""\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Worca-T Report - $run_id</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:#f8fafc;color:#1e293b;line-height:1.6;padding:2rem}
h1{font-size:1.5rem;margin-bottom:.25rem}
h2{font-size:1.25rem;margin:1.5rem 0 .75rem;border-bottom:2px solid #e2e8f0;padding-bottom:.25rem}
h3{font-size:1.1rem;margin:.75rem 0 .5rem}
.meta{color:#64748b;font-size:.875rem;margin-bottom:1.5rem}
.cards{display:flex;gap:1rem;flex-wrap:wrap;margin-bottom:1.5rem}
.card{background:#fff;border-radius:.5rem;padding:1rem 1.25rem;box-shadow:0 1px 3px rgba(0,0,0,.1);min-width:120px;text-align:center}
.card .num{font-size:1.75rem;font-weight:700}
.card .lbl{font-size:.75rem;text-transform:uppercase;color:#64748b}
.bar{height:8px;background:#e2e8f0;border-radius:4px;overflow:hidden;max-width:300px;margin:.5rem auto 0}
.bar-fill{height:100%;border-radius:4px}
table{width:100%;border-collapse:collapse;background:#fff;border-radius:.5rem;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.1);margin-bottom:1.5rem}
th,td{text-align:left;padding:.5rem .75rem;border-bottom:1px solid #f1f5f9}
th{background:#f8fafc;font-size:.75rem;text-transform:uppercase;color:#64748b}
.badge{display:inline-block;padding:.125rem .5rem;border-radius:.25rem;font-size:.75rem;font-weight:600;color:#fff}
.filters{margin-bottom:.75rem;display:flex;gap:.5rem}
.filters button{border:1px solid #cbd5e1;background:#fff;border-radius:.25rem;padding:.25rem .75rem;cursor:pointer;font-size:.8rem}
.filters button.active{background:#3b82f6;color:#fff;border-color:#3b82f6}
.bug-card{background:#fff;border-radius:.5rem;padding:1rem 1.25rem;box-shadow:0 1px 3px rgba(0,0,0,.1);margin-bottom:1rem;border-left:4px solid #e2e8f0}
.attachment-img{max-width:480px;margin:.5rem 0;border:1px solid #e2e8f0;border-radius:.25rem}
details{margin:.5rem 0}
summary{cursor:pointer;font-weight:600;color:#3b82f6}
.empty{color:#64748b;font-style:italic}
</style>
</head>
<body>
<h1>Worca-T Test Report</h1>
<div class="meta">Run: <code>$run_id</code> &mdash; $generated_at &mdash; Framework: $framework</div>

<h2>Summary</h2>
<div class="cards">
<div class="card"><div class="num">$total_tests</div><div class="lbl">Total</div></div>
<div class="card"><div class="num" style="color:#22c55e">$passed</div><div class="lbl">Passed</div></div>
<div class="card"><div class="num" style="color:#ef4444">$failed</div><div class="lbl">Failed</div></div>
<div class="card"><div class="num" style="color:#9ca3af">$skipped</div><div class="lbl">Skipped</div></div>
<div class="card"><div class="num" style="color:#f97316">$errors</div><div class="lbl">Errors</div></div>
<div class="card"><div class="num">$total_bugs</div><div class="lbl">Bugs</div></div>
<div class="card"><div class="num">$pass_rate_pct</div><div class="lbl">Pass Rate</div><div class="bar"><div class="bar-fill" style="width:$pass_rate_pct;background:$pass_rate_color"></div></div></div>
$duration_card
</div>

<h2>Test Results</h2>
$test_section

$bug_section

$plan_section

<script>
document.querySelectorAll('.filters button').forEach(function(btn){
  btn.addEventListener('click',function(){
    var f=this.dataset.filter;
    document.querySelectorAll('.filters button').forEach(function(b){b.classList.remove('active')});
    this.classList.add('active');
    document.querySelectorAll('#results-body tr').forEach(function(r){
      r.style.display=(f==='all'||r.dataset.status===f)?'':'none';
    });
  });
});
</script>
</body>
</html>
""")


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _status_badge(status: str) -> str:
    color = _STATUS_COLORS.get(status, "#6b7280")
    return f'<span class="badge" style="background:{color}">{_escape(status)}</span>'


def _severity_badge(severity: str) -> str:
    color = _SEVERITY_COLORS.get(severity, "#6b7280")
    return f'<span class="badge" style="background:{color}">{_escape(severity)}</span>'


def _render_test_rows(results: list[dict], inline_images: bool) -> str:
    if not results:
        return '<p class="empty">No test results.</p>'

    rows: list[str] = []
    for r in results:
        tid = _escape(r.get("id", ""))
        name = _escape(r.get("name", ""))
        file = _escape(r.get("file", ""))
        status = r.get("status", "unknown")
        dur = r.get("duration_s")
        dur_str = f"{dur:.2f}s" if dur is not None else "-"

        attachments_html = ""
        for a in r.get("attachments") or []:
            a_path = a.get("path", "")
            a_type = a.get("type", "other")
            if inline_images and a_type == "screenshot" and a_path:
                img_data = _try_inline_image(a_path)
                if img_data:
                    attachments_html += f'<img class="attachment-img" src="{img_data}" alt="screenshot">'
                    continue
            if a_path:
                abs_path = Path(a_path).resolve()
                href = abs_path.as_uri()
                attachments_html += f'<a href="{href}">{_escape(a_type)}: {_escape(a_path)}</a><br>'

        row = (
            f'<tr data-status="{_escape(status)}">'
            f"<td>{tid}</td><td>{name}</td><td>{file}</td>"
            f"<td>{_status_badge(status)}</td><td>{dur_str}</td>"
            f"<td>{attachments_html}</td></tr>"
        )
        rows.append(row)

    filters = (
        '<div class="filters">'
        '<button class="active" data-filter="all">All</button>'
        '<button data-filter="passed">Passed</button>'
        '<button data-filter="failed">Failed</button>'
        '<button data-filter="skipped">Skipped</button>'
        '<button data-filter="error">Error</button>'
        "</div>"
    )
    header = "<tr><th>ID</th><th>Name</th><th>File</th><th>Status</th><th>Duration</th><th>Attachments</th></tr>"
    return f'{filters}<table><thead>{header}</thead><tbody id="results-body">{"".join(rows)}</tbody></table>'


def _try_inline_image(path_str: str) -> str | None:
    p = Path(path_str)
    if not p.exists() or p.suffix.lower() not in (".png", ".jpg", ".jpeg"):
        return None
    try:
        data = base64.b64encode(p.read_bytes()).decode("ascii")
        mime = "image/png" if p.suffix.lower() == ".png" else "image/jpeg"
        return f"data:{mime};base64,{data}"
    except OSError:
        return None


def _render_bug_cards(bugs: list[dict]) -> str:
    if not bugs:
        return ""

    cards: list[str] = []
    for b in bugs:
        bid = _escape(b.get("id", ""))
        title = _escape(b.get("title", ""))
        severity = b.get("severity", "")
        priority = _escape(b.get("priority", ""))
        category = _escape(b.get("category", ""))
        test_id = _escape(b.get("test_id", ""))
        rationale = _escape(b.get("rationale", ""))
        expected = _escape(b.get("expected", ""))
        actual = _escape(b.get("actual", ""))

        sev_color = _SEVERITY_COLORS.get(severity, "#6b7280")
        card = (
            f'<div class="bug-card" style="border-left-color:{sev_color}">'
            f"<h3>{bid} &mdash; {title}</h3>"
            f"<p>{_severity_badge(severity)} <strong>{priority}</strong> &middot; {category}"
            f" &middot; Test: <code>{test_id}</code></p>"
        )
        if rationale:
            card += f"<p><strong>Rationale:</strong> {rationale}</p>"
        if expected:
            card += f"<p><strong>Expected:</strong> {expected}</p>"
        if actual:
            card += f"<p><strong>Actual:</strong> {actual}</p>"

        actions = b.get("recommended_action") or {}
        if any(actions.values()):
            card += "<details><summary>Recommended Actions</summary><ul>"
            for k in ("immediate", "short_term", "long_term"):
                v = actions.get(k)
                if v:
                    card += f"<li><strong>{_escape(k)}:</strong> {_escape(v)}</li>"
            card += "</ul></details>"

        card += "</div>"
        cards.append(card)

    return f'<h2>Bug Reports ({len(bugs)})</h2>{"".join(cards)}'


def _render_plan_section(report: RunReport) -> str:
    parts: list[str] = []
    if report.plan:
        title = _escape(report.plan.get("title", "Test Plan"))
        phases = report.plan.get("phases", [])
        parts.append(
            f"<details><summary>Plan: {title} ({len(phases)} phases)</summary>"
            f"<pre>{_escape(str(len(phases)))} phases defined</pre></details>"
        )
    if report.strategy:
        title = _escape(report.strategy.get("title", "Test Strategy"))
        tcs = report.strategy.get("test_cases", [])
        parts.append(
            f"<details><summary>Strategy: {title} ({len(tcs)} test cases)</summary>"
            f"<pre>{_escape(str(len(tcs)))} test cases defined</pre></details>"
        )
    if parts:
        return '<h2>Plan &amp; Strategy</h2>' + "".join(parts)
    return ""


def render_html(report: RunReport, *, inline_images: bool = False) -> str:
    s = report.summary
    pass_pct = f"{s.pass_rate * 100:.0f}%"
    pass_color = "#22c55e" if s.pass_rate >= 0.9 else "#eab308" if s.pass_rate >= 0.7 else "#ef4444"

    duration_card = ""
    if s.duration_s is not None:
        duration_card = f'<div class="card"><div class="num">{s.duration_s:.1f}s</div><div class="lbl">Duration</div></div>'

    results = report.run_results.get("results", [])
    test_section = _render_test_rows(results, inline_images)
    bug_section = _render_bug_cards(report.bug_reports.get("bugs", []))
    plan_section = _render_plan_section(report)

    return _PAGE_TEMPLATE.substitute(
        run_id=_escape(report.run_id),
        generated_at=_escape(report.generated_at),
        framework=_escape(report.run_results.get("framework", "unknown")),
        total_tests=s.total_tests,
        passed=s.passed,
        failed=s.failed,
        skipped=s.skipped,
        errors=s.errors,
        total_bugs=s.total_bugs,
        pass_rate_pct=pass_pct,
        pass_rate_color=pass_color,
        duration_card=duration_card,
        test_section=test_section,
        bug_section=bug_section,
        plan_section=plan_section,
    )
