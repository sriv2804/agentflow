"""
Reports from results.json:

    report.html  one self-contained file: results, grades and transcripts embedded as
                 JSON, inline CSS and JS, no network requests, charts as CSS bars.
                 Hash navigation: #/, #/task/<id>, #/dimension/<name>,
                 #/trial/<task>/<n>, plus #/list/<kind>/<key> for the trials behind
                 an aggregate number.
    report.md    the headline tables only, for CI logs and PR comments.

Transcript content is only ever inserted as text (textContent), never as HTML,
and every embedded string is cut at FIELD_LIMIT characters; the full text stays
in transcript.jsonl.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.evals.aggregate import load_trials

ASSETS = Path(__file__).parent / "assets"
FIELD_LIMIT = 2000


def truncate(value: Any, limit: int = FIELD_LIMIT) -> Any:
    if isinstance(value, str):
        if len(value) <= limit:
            return value
        return value[:limit] + f" …[{len(value) - limit} more characters; full text in transcript.jsonl]"
    if isinstance(value, list):
        return [truncate(v, limit) for v in value]
    if isinstance(value, dict):
        return {k: truncate(v, limit) for k, v in value.items()}
    return value


def embed_json(data: Any) -> str:
    """JSON safe inside <script type="application/json">: no '<' can close the tag or open a comment."""
    text = json.dumps(data, default=str, ensure_ascii=False)
    return text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026") \
               .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def report_data(run_dir: Path, results: Dict[str, Any], comparison: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    trials = {}
    for t in load_trials(Path(run_dir)):
        trials[f"{t['task']}/{t['trial']}"] = {"events": t["events"], "grades": t["grades"],
                                               "observations": _read_json(Path(run_dir) / t["dir"] / "observations.json")}
    return truncate({"results": results, "trials": trials, "compare": comparison})


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def render_html(run_dir: Path, results: Dict[str, Any], comparison: Optional[Dict[str, Any]] = None) -> str:
    template = (ASSETS / "report.html").read_text(encoding="utf-8")
    title = f"{results['suite'].get('name') or 'eval'} · {results['run_id']}"
    return (template
            .replace("{{TITLE}}", _escape(title))
            .replace("{{CSS}}", (ASSETS / "report.css").read_text(encoding="utf-8"))
            .replace("{{JS}}", (ASSETS / "report.js").read_text(encoding="utf-8"))
            .replace("{{DATA}}", embed_json(report_data(run_dir, results, comparison))))


def _escape(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                .replace('"', "&quot;").replace("'", "&#39;"))


# ---------- markdown ----------

def _pct(v):
    return "–" if v is None else f"{v * 100:.0f}%"


def _num(v, digits=2):
    return "–" if v is None else f"{v:.{digits}f}"


def _md_cell(text: Any) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def render_md(results: Dict[str, Any], comparison: Optional[Dict[str, Any]] = None) -> str:
    s, m = results["suite"], results["manifest"]
    agents = ", ".join(f"{k}: {v.get('model_name')}" for k, v in (m.get("agents") or {}).items())
    judge = (m.get("judge") or {}).get("model_name")
    lines = [f"# Eval report: {s.get('name')} ({results['run_id']})", "",
             f"Flow `{m.get('flow')}` · agents {agents or '–'} · judge {judge or '–'} · "
             f"git {((m.get('agentflow_version') or {}).get('git_sha') or '–')[:10]}", "",
             "| trial pass rate | tasks passing all trials | tasks passing any trial | timeouts | infra errors | grader errors |",
             "|---|---|---|---|---|---|",
             f"| {_pct(s['pass_rate'])} ({s['passed']}/{s['decided']}) | {s['tasks_pass_all']}/{s['tasks']} | "
             f"{s['tasks_pass_any']}/{s['tasks']} | {s['counts']['timeouts']} | {s['counts']['infra_errors']} | "
             f"{s['counts']['grader_errors']} |", "",
             "| task | client | passed | pass@k | pass^k | mean score | top failure reason |",
             "|---|---|---|---|---|---|---|"]
    yes = lambda v: "–" if v is None else ("✓ yes" if v else "✗ no")
    for t in results["tasks"]:
        reason = t["failure_reasons"][0] if t["failure_reasons"] else None
        top = f"{reason['reason']} `{reason['grader_id']}` ({reason['trials']})" if reason and reason["grader_id"] \
            else (f"{reason['reason']} ({reason['trials']})" if reason else "")
        client = {"scripted": "scripted", "llm": "LLM-simulated"}.get(t["user_mode"], t["user_mode"] or "–")
        if t["default_reply_trials"]:
            client += f" (default reply in {t['default_reply_trials']}/{t['trials']})"
        lines.append(f"| {_md_cell(t['id'])} | {client} | {t['passed']}/{t['k']} | {yes(t['pass_at_k'])} | "
                     f"{yes(t['pass_pow_k'])} | {_num(t['mean_score'])} | {_md_cell(top)} |")
    if s["dimensions"]:
        lines += ["", "| dimension | mean (0–1) | trials scored | tasks scored |", "|---|---|---|---|"]
        for name, d in s["dimensions"].items():
            lines.append(f"| {_md_cell(name)} | {_num(d['mean'])} | {d['trials_scored']} | {d['tasks_scored']} |")
    kf = s["key_facts"]
    lines += ["", "Key facts: " + ", ".join(f"{v} {kf[v]['count']}" for v in kf)]
    if s["failure_reasons"]:
        lines += ["", "| failure reason | grader | trials |", "|---|---|---|"]
        for r in s["failure_reasons"]:
            lines.append(f"| {r['reason']} | {_md_cell(r['grader_id'] or '–')} | {r['trials']} |")
    defined = [t for t in results["tasks"] if t.get("definition")]
    if defined:
        lines += ["", "## Tasks"]
        for t in defined:
            lines += ["", *_md_task(t["id"], t["definition"])]
    if comparison:
        from src.evals.compare import render_compare_md
        lines += ["", render_compare_md(comparison).replace("## Compare", "## Compare with baseline")]
    return "\n".join(lines).rstrip() + "\n"


def _md_task(task_id: str, d: Dict[str, Any]) -> List[str]:
    u = d.get("user") or {}
    if u.get("mode") == "llm":
        client = f"LLM-simulated user ({u.get('persona') or 'no persona'}); knows: " + \
                 "; ".join(u.get("facts") or []) if u.get("facts") else "LLM-simulated user"
    else:
        replies = u.get("replies") or []
        client = "scripted user; replies: " + (" → ".join(f'"{r}"' for r in replies) if replies else "none")
    graders = []
    for g in d.get("graders") or []:
        opts = [f"{k}={v if isinstance(v, str) else json.dumps(v)}" for k, v in g.items()
                if k not in ("type", "required", "weight", "min_score")]
        flags = (["required"] if g.get("required") else []) + [f"weight {g.get('weight', 1)}"] + \
                ([f"min_score {g['min_score']}"] if g.get("min_score") is not None else [])
        graders.append(f"`{g['type']}` ({', '.join(flags)})" + (f": {', '.join(opts)}" if opts else ""))
    lines = [f"### {task_id}", "",
             f"- **Prompt:** {_md_cell(d.get('prompt', ''))}",
             f"- **Setup:** `{d['setup']}`" + (f": {_md_cell(d['setup_doc'])}" if d.get("setup_doc") else "")
             if d.get("setup") else "- **Setup:** none",
             f"- **User:** {_md_cell(client)}",
             f"- **Expected:** {_md_cell((d.get('expected') or '–').strip())}"]
    if d.get("key_facts"):
        lines.append("- **Key facts:** " + "; ".join(_md_cell(f) for f in d["key_facts"]))
    if d.get("context"):
        lines.append("- **Context:** " + ", ".join(f"`{k}`" + (f" ({c['source']})" if c.get("source") else "")
                                                  for k, c in d["context"].items()))
    lines.append(f"- **Graders** (pass threshold {d.get('pass_threshold')}):")
    lines += [f"  - {_md_cell(g)}" for g in graders]
    return lines


def write_reports(run_dir: Path, results: Dict[str, Any], comparison: Optional[Dict[str, Any]] = None) -> Dict[str, Path]:
    run_dir = Path(run_dir)
    html_path, md_path = run_dir / "report.html", run_dir / "report.md"
    html_path.write_text(render_html(run_dir, results, comparison), encoding="utf-8")
    md_path.write_text(render_md(results, comparison), encoding="utf-8")
    return {"html": html_path, "md": md_path}
