"""
compare(run_a, run_b): what changed from a baseline (a) to a candidate (b).

A change is flagged only when it is larger than one trial flipping could cause:
- a pass rate over k decided trials moves by 1/k when one trial flips, so a task's
  pass-rate delta is flagged when |delta| > 1 / min(k_a, k_b) (the suite's likewise
  over its decided trials);
- a mean score over n trials moves by at most 1/n when one trial's score changes
  completely (scores are 0..1), so mean-score and dimension deltas are flagged when
  |delta| > 1 / min(n_a, n_b).
Flagged changes are "regression" or "improvement". Efficiency deltas (LLM calls,
tokens, latency) are reported but never flagged.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

# manifest keys that always differ between runs and say nothing about the setup
VOLATILE_MANIFEST_KEYS = ("run_id", "started_at", "finished_at", "run_totals", "grading")


def _delta(a: Optional[float], b: Optional[float]) -> Optional[float]:
    return None if a is None or b is None else round(b - a, 4)


def _flag(delta: Optional[float], n_a: int, n_b: int) -> Optional[str]:
    n = min(n_a, n_b)
    if delta is None or n <= 0:
        return None
    threshold = 1.0 / n
    if abs(delta) <= threshold + 1e-9:
        return None
    return "improvement" if delta > 0 else "regression"


def _flatten(d: Any, prefix: str = "") -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if isinstance(d, dict):
        for k, v in d.items():
            out.update(_flatten(v, f"{prefix}.{k}" if prefix else str(k)))
    else:
        out[prefix] = d
    return out


def manifest_diff(a: Dict[str, Any], b: Dict[str, Any]) -> List[Dict[str, Any]]:
    fa = _flatten({k: v for k, v in a.items() if k not in VOLATILE_MANIFEST_KEYS})
    fb = _flatten({k: v for k, v in b.items() if k not in VOLATILE_MANIFEST_KEYS})
    return [{"key": k, "a": fa.get(k), "b": fb.get(k)} for k in sorted(set(fa) | set(fb)) if fa.get(k) != fb.get(k)]


def _efficiency(a: Dict[str, Any], b: Dict[str, Any]) -> Dict[str, Any]:
    out = {}
    for key in ("mean_llm_calls", "mean_tokens_in", "mean_tokens_out", "mean_latency_s"):
        va, vb = a.get(key), b.get(key)
        d = _delta(va, vb)
        out[key] = {"a": va, "b": vb, "delta": d,
                    "relative": None if d is None or not va else round(d / va, 4)}
    return out


def compare(results_a: Dict[str, Any], results_b: Dict[str, Any]) -> Dict[str, Any]:
    sa, sb = results_a["suite"], results_b["suite"]
    tasks_a = {t["id"]: t for t in results_a["tasks"]}
    tasks_b = {t["id"]: t for t in results_b["tasks"]}

    tasks = []
    for tid in list(tasks_a) + [t for t in tasks_b if t not in tasks_a]:
        a, b = tasks_a.get(tid), tasks_b.get(tid)
        if a is None or b is None:
            tasks.append({"id": tid, "only_in": "a" if b is None else "b"})
            continue
        pr = _delta(a["pass_rate"], b["pass_rate"])
        ms = _delta(a["mean_score"], b["mean_score"])
        n_a = a["trials"] - a["statuses"].get("infra_error", 0)
        n_b = b["trials"] - b["statuses"].get("infra_error", 0)
        tasks.append({
            "id": tid,
            "pass_rate": {"a": a["pass_rate"], "b": b["pass_rate"], "delta": pr,
                          "k_a": a["k"], "k_b": b["k"], "flag": _flag(pr, a["k"], b["k"])},
            "mean_score": {"a": a["mean_score"], "b": b["mean_score"], "delta": ms,
                           "flag": _flag(ms, n_a, n_b)},
            "efficiency": _efficiency(a, b),
        })

    dims = []
    da, db = sa["dimensions"], sb["dimensions"]
    for name in sorted(set(da) | set(db)):
        a, b = da.get(name), db.get(name)
        if a is None or b is None:
            dims.append({"name": name, "only_in": "a" if b is None else "b"})
            continue
        d = _delta(a["mean"], b["mean"])
        dims.append({"name": name, "a": a["mean"], "b": b["mean"], "delta": d,
                     "n_a": a["trials_scored"], "n_b": b["trials_scored"],
                     "flag": _flag(d, a["trials_scored"], b["trials_scored"])})

    pr = _delta(sa["pass_rate"], sb["pass_rate"])
    suite = {"pass_rate": {"a": sa["pass_rate"], "b": sb["pass_rate"], "delta": pr,
                           "k_a": sa["decided"], "k_b": sb["decided"],
                           "flag": _flag(pr, sa["decided"], sb["decided"])},
             "efficiency": _efficiency(sa["efficiency"], sb["efficiency"])}

    flagged = ([{"kind": "task", "id": t["id"], "metric": m, "flag": t[m]["flag"], "delta": t[m]["delta"]}
                for t in tasks if "only_in" not in t for m in ("pass_rate", "mean_score") if t[m]["flag"]]
               + [{"kind": "dimension", "id": d["name"], "metric": "mean", "flag": d["flag"], "delta": d["delta"]}
                  for d in dims if d.get("flag")]
               + ([{"kind": "suite", "id": "suite", "metric": "pass_rate", "flag": suite["pass_rate"]["flag"],
                    "delta": pr}] if suite["pass_rate"]["flag"] else []))
    return {
        "run_a": results_a["run_id"], "run_b": results_b["run_id"],
        "suite": suite, "tasks": tasks, "dimensions": dims,
        "manifest_diff": manifest_diff(results_a.get("manifest") or {}, results_b.get("manifest") or {}),
        "flagged": flagged,
        "regressions": [f for f in flagged if f["flag"] == "regression"],
    }


def _fmt(v: Optional[float], pct: bool = False) -> str:
    if v is None:
        return "–"
    return f"{v * 100:.0f}%" if pct else f"{v:.2f}"


def _fmt_delta(v: Optional[float], pct: bool = False) -> str:
    if v is None:
        return "–"
    return f"{v * 100:+.0f} pts" if pct else f"{v:+.2f}"


def render_compare_md(c: Dict[str, Any]) -> str:
    mark = {"regression": "▼ regression", "improvement": "▲ improvement", None: ""}
    lines = [f"## Compare: {c['run_a']} → {c['run_b']}", ""]
    s = c["suite"]["pass_rate"]
    lines += [f"Suite pass rate: {_fmt(s['a'], True)} → {_fmt(s['b'], True)} ({_fmt_delta(s['delta'], True)}) "
              f"{mark[s['flag']]}".rstrip(), "",
              "| task | pass rate a → b | Δ | mean score a → b | Δ | flag |", "|---|---|---|---|---|---|"]
    for t in c["tasks"]:
        if "only_in" in t:
            lines.append(f"| {t['id']} | only in run {t['only_in']} | | | | |")
            continue
        p, m = t["pass_rate"], t["mean_score"]
        flag = " / ".join(x for x in (mark[p["flag"]], mark[m["flag"]]) if x)
        lines.append(f"| {t['id']} | {_fmt(p['a'], True)} → {_fmt(p['b'], True)} | {_fmt_delta(p['delta'], True)} | "
                     f"{_fmt(m['a'])} → {_fmt(m['b'])} | {_fmt_delta(m['delta'])} | {flag} |")
    if c["dimensions"]:
        lines += ["", "| dimension | mean a → b | Δ | flag |", "|---|---|---|---|"]
        for d in c["dimensions"]:
            if "only_in" in d:
                lines.append(f"| {d['name']} | only in run {d['only_in']} | | |")
            else:
                lines.append(f"| {d['name']} | {_fmt(d['a'])} → {_fmt(d['b'])} | {_fmt_delta(d['delta'])} | "
                             f"{mark[d['flag']]} |")
    eff = c["suite"]["efficiency"]
    lines += ["", "| efficiency (mean per trial) | a | b | Δ |", "|---|---|---|---|"]
    for key, label in (("mean_llm_calls", "LLM calls"), ("mean_tokens_in", "tokens in"),
                       ("mean_tokens_out", "tokens out"), ("mean_latency_s", "latency (s)")):
        e = eff[key]
        rel = f" ({e['relative'] * 100:+.0f}%)" if e["relative"] is not None else ""
        lines.append(f"| {label} | {_fmt(e['a'])} | {_fmt(e['b'])} | {_fmt_delta(e['delta'])}{rel} |")
    if c["manifest_diff"]:
        lines += ["", "Manifest differences:", ""]
        lines += [f"- `{d['key']}`: {d['a']!r} → {d['b']!r}" for d in c["manifest_diff"]]
    lines.append("")
    lines.append("Flags mark changes larger than one trial flipping could cause.")
    return "\n".join(lines) + "\n"
