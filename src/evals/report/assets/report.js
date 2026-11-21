"use strict";
/* agentflow eval report. Every piece of run data is inserted as text (textContent /
   createTextNode), never as HTML. Routes: #/, #/task/<id>, #/dimension/<name>,
   #/trial/<task>/<n>[?e=<event index>], #/list/<kind>/<key>. */
(function () {
  const D = JSON.parse(document.getElementById("report-data").textContent);
  const R = D.results, S = R.suite, M = R.manifest || {};
  const ATTENTION = 0.7;   // dimensions with a mean below this need attention

  // ---------- DOM helpers ----------
  function el(tag, attrs) {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k === "class") e.className = v;
      else e.setAttribute(k, String(v));
    }
    for (let i = 2; i < arguments.length; i++) add(e, arguments[i]);
    return e;
  }
  function add(parent, kid) {
    if (kid === null || kid === undefined || kid === false) return;
    if (Array.isArray(kid)) { kid.forEach(k => add(parent, k)); return; }
    parent.appendChild(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  function route(segs, query) {
    let h = "#/" + segs.map(s => encodeURIComponent(String(s))).join("/");
    if (query) h += "?" + Object.entries(query).map(([k, v]) => k + "=" + encodeURIComponent(v)).join("&");
    return h;
  }
  const a = (href, ...kids) => el("a", { href }, ...kids);
  const trialHref = (task, n, e) => route(["trial", task, n], e === undefined ? null : { e });
  const taskHref = id => route(["task", id]);
  const dimHref = name => route(["dimension", name]);
  const listHref = (kind, key) => route(["list", kind, key]);
  const card = (title, ...kids) => el("section", { class: "card" }, title ? el("h2", {}, title) : null, ...kids);

  const pct = v => v === null || v === undefined ? "–" : Math.round(v * 100) + "%";
  const num = (v, d = 2) => v === null || v === undefined ? "–" : Number(v).toFixed(d);
  const int = v => v === null || v === undefined ? "–" : Math.round(v).toLocaleString();

  const VERDICT = {
    pass: ["pass", "✓ PASS"], fail: ["fail", "✗ FAIL"], undetermined: ["warn", "? UNDETERMINED"],
    excluded: ["neutral", "⊘ EXCLUDED"], ungraded: ["neutral", "– UNGRADED"],
  };
  const verdictBadge = v => { const [c, t] = VERDICT[v] || ["neutral", v]; return el("span", { class: "badge " + c }, t); };
  function graderBadge(g) {
    if (g.status === "grader_error") return el("span", { class: "badge warn" }, "! grader error");
    if (g.passed === true) return el("span", { class: "badge pass" }, "✓ pass");
    if (g.passed === false) return el("span", { class: "badge fail" }, "✗ fail");
    return el("span", { class: "badge neutral" }, "◆ scored");
  }
  const yesNo = v => v === null || v === undefined ? el("span", { class: "badge neutral" }, "– n/a")
    : v ? el("span", { class: "badge pass" }, "✓ yes") : el("span", { class: "badge fail" }, "✗ no");
  const clientLabel = m => ({ scripted: "scripted user", llm: "LLM-simulated user" })[m] || (m || "–");

  function bar(value, cls) {
    const span = el("span");
    span.style.width = Math.max(0, Math.min(1, value || 0)) * 100 + "%";
    return el("div", { class: "bar " + (cls || ""), role: "img", "aria-label": pct(value) }, span);
  }
  function table(headers, rows) {
    return el("div", { class: "tablewrap" }, el("table", {},
      el("thead", {}, el("tr", {}, headers.map(h => el("th", { class: h.num ? "num" : null }, h.label || h)))),
      el("tbody", {}, rows.length ? rows : el("tr", {}, el("td", { colspan: headers.length, class: "empty" }, "none")))));
  }
  const td = (content, cls) => el("td", { class: cls || null }, content);

  // ---------- lookups ----------
  const taskById = id => R.tasks.find(t => t.id === id);
  function trialRow(task, n) {
    const t = taskById(task);
    return t ? t.trial_rows.find(r => String(r.trial) === String(n)) : undefined;
  }
  const trialKey = (task, n) => task + "/" + n;
  const refLabel = r => r.task + " · t" + r.trial;

  // ---------- views ----------
  function crumbs(parts) {
    const nav = document.getElementById("crumbs");
    nav.replaceChildren();
    parts.forEach((p, i) => { if (i) add(nav, " / "); add(nav, p[1] ? a(p[1], p[0]) : p[0]); });
  }

  function notFound(what) {
    crumbs([["not found"]]);
    return [el("h1", { id: "not-found" }, "Not found"), el("p", { class: "muted" }, String(what)),
            el("p", {}, a("#/", "Back to the overview"))];
  }

  function manifestCard() {
    const v = M.agentflow_version || {};
    const rows = [
      ["suite", (M.suite || {}).name], ["run", R.run_id], ["flow", M.flow],
      ["started", M.started_at], ["finished", M.finished_at],
      ["agents", Object.entries(M.agents || {}).map(([k, x]) => k + ": " + x.model_name + " (" + x.backend + ")").join(", ")],
      ["judge", M.judge ? M.judge.backend + ":" + M.judge.model_name : "–"],
      ["user simulator", M.user_sim ? M.user_sim.backend + ":" + M.user_sim.model_name : "not used"],
      ["git", (v.git_sha || "–").slice(0, 12) + (v.dirty ? " (dirty)" : "")],
      ["calls", "agent " + int(S.calls.agent) + " · simulator " + int(S.calls.user_sim) + " · judge " + int(S.calls.judge)],
    ];
    for (const [k, x] of Object.entries(M.metadata || {})) rows.push(["metadata." + k, typeof x === "string" ? x : JSON.stringify(x)]);
    return card("Run", el("dl", { class: "meta" }, rows.map(([k, x]) => [el("dt", {}, k), el("dd", {}, x === undefined || x === null ? "–" : x)])));
  }

  function tile(label, value, href) {
    return el("div", { class: "tile" }, el("div", { class: "label" }, label),
      el("div", { class: "value" }, href ? a(href, value) : value));
  }

  function needsAttention() {
    const items = [];
    for (const t of R.tasks) {
      if (t.pass_pow_k === false)
        items.push([el("span", { class: "badge fail" }, "✗ task"), a(taskHref(t.id), t.id),
                    "passed " + t.passed + "/" + t.k + " trials"]);
      else if (t.k === 0 && t.trials)
        items.push([el("span", { class: "badge warn" }, "? task"), a(taskHref(t.id), t.id), "no trial with a pass/fail verdict"]);
    }
    for (const [name, d] of Object.entries(S.dimensions))
      if (d.mean !== null && d.mean < ATTENTION)
        items.push([el("span", { class: "badge warn" }, "▼ dimension"), a(dimHref(name), name),
                    "mean " + num(d.mean) + " (below " + ATTENTION + ")"]);
    for (const f of S.contradicted_facts)
      items.push([el("span", { class: "badge fail" }, "✗ contradicted fact"), a(trialHref(f.task, f.trial), refLabel(f)), "“" + f.fact + "”"]);
    if (D.compare)
      for (const r of D.compare.regressions)
        items.push([el("span", { class: "badge fail" }, "▼ regression"),
                    r.kind === "task" ? a(taskHref(r.id), r.id) : r.kind === "dimension" ? a(dimHref(r.id), r.id) : r.id,
                    r.metric + " " + (r.delta > 0 ? "+" : "") + num(r.delta) + " vs baseline " + D.compare.run_a]);
    if (S.counts.grader_errors)
      items.push([el("span", { class: "badge warn" }, "! grader errors"), a(listHref("count", "grader_errors"), S.counts.grader_errors + " error(s)"),
                  "grading problems, not agent failures"]);
    if (S.counts.undetermined)
      items.push([el("span", { class: "badge warn" }, "? undetermined"), a(listHref("count", "undetermined"), S.counts.undetermined + " trial(s)"),
                  "a required grader could not grade"]);
    return card("Needs attention", items.length
      ? el("ul", { class: "plain" }, items.map(i => el("li", {}, i)))
      : el("p", {}, el("span", { class: "badge pass" }, "✓ nothing"), " every task passed every trial, with no flagged issues."));
  }

  function overview() {
    crumbs([["overview"]]);
    const c = S.counts;
    const out = [el("h1", {}, (S.name || "suite") + " · " + R.run_id)];
    out.push(el("div", { class: "tiles" },
      tile("trial pass rate", pct(S.pass_rate) + " (" + S.passed + "/" + S.decided + ")", listHref("count", "passed")),
      tile("tasks passing every trial", S.tasks_pass_all + "/" + S.tasks),
      tile("tasks passing any trial", S.tasks_pass_any + "/" + S.tasks),
      tile("timeouts", c.timeouts, c.timeouts ? listHref("count", "timeouts") : null),
      tile("limit exceeded", c.limit_exceeded, c.limit_exceeded ? listHref("count", "limit_exceeded") : null),
      tile("infra errors", c.infra_errors, c.infra_errors ? listHref("count", "infra_errors") : null),
      tile("grader errors", c.grader_errors, c.grader_errors ? listHref("count", "grader_errors") : null)));
    out.push(el("div", { class: "grid2", style: "margin-bottom:14px" }, needsAttention(), manifestCard()));

    const taskRows = R.tasks.map(t => el("div", { class: "barrow" },
      el("div", { class: "name" }, a(taskHref(t.id), t.id), " ",
         el("span", { class: "badge outline" }, clientLabel(t.user_mode)),
         t.default_reply_trials ? el("span", { class: "badge warn" }, "default reply in " + t.default_reply_trials + "/" + t.trials) : null),
      bar(t.pass_rate, t.pass_pow_k ? "pass" : "fail"),
      el("div", { class: "val" }, a(taskHref(t.id), (t.pass_pow_k ? "✓ " : "✗ ") + t.passed + "/" + t.k + " passed"),
         " · pass@k ", t.pass_at_k ? "✓" : "✗", " · pass^k ", t.pass_pow_k ? "✓" : "✗")));
    out.push(card("Pass rate per task", taskRows.length ? taskRows : el("p", { class: "empty" }, "no tasks")));

    const kf = S.key_facts, total = Object.values(kf).reduce((n, v) => n + v.count, 0);
    const segs = Object.entries(kf).filter(([, v]) => v.count).map(([verdict, v]) => {
      const s = a(listHref("facts", verdict), String(v.count));
      s.className = "seg-" + verdict;
      s.style.flex = String(v.count);
      s.setAttribute("title", verdict + ": " + v.count);
      s.setAttribute("aria-label", verdict + ": " + v.count + " facts");
      return s;
    });
    out.push(el("div", { class: "grid2" },
      card("Key facts", total ? el("div", { class: "stacked" }, segs) : el("p", { class: "empty" }, "no key_facts graders"),
        el("div", { class: "legend" }, Object.entries(kf).map(([verdict, v]) => {
          const s = el("span", { class: "seg-" + verdict }, verdict + " ", a(listHref("facts", verdict), String(v.count)));
          s.style.setProperty("--c", getComputedStyle(document.documentElement).getPropertyValue(
            { present: "--pass", absent: "--neutral", contradicted: "--fail", undetermined: "--warn" }[verdict]));
          return s;
        }))),
      card("Dimensions (mean, 0–1)", Object.keys(S.dimensions).length ? Object.entries(S.dimensions).map(([name, d]) =>
        el("div", { class: "barrow" }, el("div", { class: "name" }, a(dimHref(name), name)),
          bar(d.mean, d.mean !== null && d.mean < ATTENTION ? "fail" : "pass"),
          el("div", { class: "val" }, a(dimHref(name), (d.mean !== null && d.mean < ATTENTION ? "▼ " : "") + num(d.mean)),
             " · " + d.tasks_scored + " task(s), " + d.trials_scored + " trial(s)")))
        : el("p", { class: "empty" }, "no llm_rubric graders"))));

    const reasons = S.all_failure_reasons.map((r, i) => el("tr", {},
      td(r.reason), td(r.grader_id || "–", "mono"), td(a(listHref("reason", i), String(r.trials)), "num")));
    const tools = Object.entries(S.tool_health).map(([name, h]) => el("tr", {},
      td(name, "mono"), td(String(h.calls), "num"),
      td(h.errors ? a(listHref("tool", name), "✕ " + h.errors) : "0", "num"), td(pct(h.error_rate), "num")));
    out.push(el("div", { class: "grid2" },
      card("Failure reasons (trials, counted once per trial)", table(["reason", "grader", { label: "trials", num: true }], reasons)),
      card("Tool health", table(["tool", { label: "calls", num: true }, { label: "errors", num: true }, { label: "error rate", num: true }], tools))));

    if (D.compare) out.push(compareCard(D.compare));
    return out;
  }

  function compareCard(c) {
    const flagCell = f => f === "regression" ? el("span", { class: "badge fail" }, "▼ regression")
      : f === "improvement" ? el("span", { class: "badge pass" }, "▲ improvement") : "";
    const sign = v => v === null || v === undefined ? "–" : (v > 0 ? "+" : "") + num(v);
    const rows = c.tasks.filter(t => !t.only_in).map(t => el("tr", {},
      td(taskById(t.id) ? a(taskHref(t.id), t.id) : t.id),
      td(pct(t.pass_rate.a) + " → " + pct(t.pass_rate.b), "num"), td(sign(t.pass_rate.delta), "num"),
      td(num(t.mean_score.a) + " → " + num(t.mean_score.b), "num"), td(sign(t.mean_score.delta), "num"),
      td([flagCell(t.pass_rate.flag), " ", flagCell(t.mean_score.flag)])));
    const dims = c.dimensions.filter(d => !d.only_in).map(d => el("tr", {},
      td(S.dimensions[d.name] ? a(dimHref(d.name), d.name) : d.name), td(num(d.a) + " → " + num(d.b), "num"),
      td(sign(d.delta), "num"), td(flagCell(d.flag))));
    const diffs = c.manifest_diff.map(d => el("li", {}, el("span", { class: "mono" }, d.key), ": ",
      JSON.stringify(d.a), " → ", JSON.stringify(d.b)));
    return card("Compared with baseline " + c.run_a,
      el("p", { class: "muted" }, "Flags mark changes larger than one trial flipping could cause."),
      table(["task", { label: "pass rate", num: true }, { label: "Δ", num: true }, { label: "mean score", num: true },
             { label: "Δ", num: true }, "flag"], rows),
      el("h3", {}, "Dimensions"), table(["dimension", { label: "mean", num: true }, { label: "Δ", num: true }, "flag"], dims),
      el("h3", {}, "Manifest differences"), diffs.length ? el("ul", {}, diffs) : el("p", { class: "empty" }, "none"));
  }

  const GRADER_COMMON = ["type", "required", "weight", "min_score"];
  function graderSpecItem(g) {
    const opts = Object.entries(g).filter(([k]) => !GRADER_COMMON.includes(k))
      .map(([k, v]) => k + ": " + (typeof v === "string" ? v : JSON.stringify(v)));
    return el("li", {}, el("span", { class: "mono" }, g.type),
      g.required ? el("span", { class: "badge fail" }, "required") : null,
      el("span", { class: "badge outline" }, "weight " + (g.weight ?? 1)),
      g.min_score !== undefined && g.min_score !== null ? el("span", { class: "badge outline" }, "min_score " + g.min_score) : null,
      opts.length ? el("span", { class: "muted mono" }, opts.join(" · ")) : null);
  }
  const plainList = items => items.length ? el("ul", { class: "plain" }, items.map(x => el("li", {}, x)))
    : el("span", { class: "empty" }, "none");

  function definitionCard(t) {
    const d = t.definition;
    if (!d) return card("Task", el("p", { class: "empty" },
      "This run did not record the task's definition, and the suite file has changed since the run."));
    const u = d.user || {};
    const userRows = u.mode === "llm"
      ? [["client", "LLM-simulated user"], ["persona", u.persona || "–"], ["facts it knows", plainList(u.facts || [])]]
      : [["client", "scripted user"], ["replies, in order", plainList(u.replies || [])]];
    userRows.push(["default reply", u.on_exhausted || "–"], ["max clarifications", String(u.max_clarifications ?? "–")]);
    const context = Object.entries(d.context || {}).map(([k, c]) => el("li", {},
      el("details", {}, el("summary", {}, el("span", { class: "mono" }, k), c.source ? " (" + c.source + ")" : ""),
        el("pre", { class: "block" }, c.text))));
    const lim = d.limits || {};
    const rows = [
      ["prompt", el("div", { class: "quote" }, d.prompt)],
      ["setup", d.setup ? [el("span", { class: "mono" }, d.setup), d.setup_doc ? el("div", { class: "muted" }, d.setup_doc) : null] : "none"],
      ["teardown", d.teardown ? el("span", { class: "mono" }, d.teardown) : "none"],
      ...userRows,
      ["expected", d.expected ? el("div", { class: "quote" }, d.expected) : "–"],
      ["key facts", plainList(d.key_facts || [])],
      ["context", context.length ? el("ul", { class: "plain" }, context) : "none"],
      ["graders", el("ul", { class: "plain" }, (d.graders || []).map(graderSpecItem))],
      ["pass threshold", String(d.pass_threshold ?? "–")],
      ["limits", "timeout " + (lim.timeout_s ?? "–") + "s · max LLM calls " + (lim.max_llm_calls ?? "–")],
    ];
    return card("Task", el("dl", { class: "meta taskdef" }, rows.map(([k, x]) => [el("dt", {}, k), el("dd", {}, x)])),
      d.recorded === "suite" ? el("p", { class: "muted" },
        "Read from the suite file, unchanged since the run (this run's manifest predates recording it).") : null);
  }

  function taskView(id) {
    const t = taskById(id);
    if (!t) return notFound("No task " + id + " in this run.");
    crumbs([["overview", "#/"], ["task " + id]]);
    const graderIds = [];
    t.trial_rows.forEach(r => r.graders.forEach(g => { if (!graderIds.includes(g.id)) graderIds.push(g.id); }));
    const rows = t.trial_rows.map(r => el("tr", {},
      td(a(trialHref(id, r.trial), "t" + r.trial)), td(verdictBadge(r.verdict)), td(r.status),
      graderIds.map(gid => {
        const g = r.graders.find(x => x.id === gid);
        if (!g) return td("–", "cell");
        return td(a(trialHref(id, r.trial), graderBadge(g), g.score !== null && g.score !== undefined ? " " + num(g.score) : ""), "cell");
      }),
      td(num(r.score), "num"), td(int(r.tokens_in) + " / " + int(r.tokens_out), "num"),
      td(num(r.duration_s, 1) + "s", "num"), td(String(r.llm_calls ?? "–"), "num")));
    const reasons = t.failure_reasons.map(r => el("li", {}, el("span", { class: "badge fail" }, "✗ " + r.reason),
      r.grader_id ? el("span", { class: "mono" }, r.grader_id) : null,
      " in ", r.refs.map((ref, i) => [i ? ", " : "", a(trialHref(ref.task, ref.trial), "t" + ref.trial)])));
    return [
      el("h1", {}, id),
      el("div", { class: "tiles" },
        tile("passed", t.passed + "/" + t.k), tile("pass@k", t.pass_at_k === null ? "–" : t.pass_at_k ? "✓ yes" : "✗ no"),
        tile("pass^k", t.pass_pow_k === null ? "–" : t.pass_pow_k ? "✓ yes" : "✗ no"), tile("mean score", num(t.mean_score)),
        tile("client", clientLabel(t.user_mode)), tile("default reply used", t.default_reply_trials + "/" + t.trials),
        tile("mean LLM calls", num(t.mean_llm_calls, 1)), tile("mean latency", num(t.mean_latency_s, 1) + "s")),
      definitionCard(t),
      card("Trials × graders", table(["trial", "verdict", "status", ...graderIds.map(g => ({ label: g })),
        { label: "score", num: true }, { label: "tokens in / out", num: true }, { label: "latency", num: true },
        { label: "LLM calls", num: true }], rows)),
      card("Top failure reasons", reasons.length ? el("ul", { class: "plain" }, reasons) : el("p", { class: "empty" }, "none")),
    ];
  }

  function dimensionView(name) {
    const d = S.dimensions[name];
    if (!d) return notFound("No dimension " + name + " in this run.");
    crumbs([["overview", "#/"], ["dimension " + name]]);
    const rows = d.rows.map(r => el("tr", {},
      td(a(trialHref(r.task, r.trial), refLabel(r))), td(r.score === null ? "? undetermined" : String(r.score), "num"),
      td(num(r.normalised), "num"), td(r.reason || "–"), td(r.tool_errors ? "✕ " + r.tool_errors : "0", "num")));
    return [el("h1", {}, name),
      el("div", { class: "tiles" }, tile("mean (0–1)", num(d.mean)), tile("trials scored", d.trials_scored),
        tile("undetermined", d.undetermined), tile("tasks scored", d.tasks_scored)),
      card("Score per trial, lowest first", table(["trial", { label: "score", num: true }, { label: "0–1", num: true },
        "judge's reason", { label: "tool errors", num: true }], rows))];
  }

  function listView(kind, key) {
    let title, refs, extra = null;
    if (kind === "count") {
      refs = S.count_refs[key];
      title = { passed: "Passed trials", failed: "Failed trials", timeouts: "Timed-out trials", limit_exceeded: "Trials over a limit",
                agent_errors: "Agent errors", infra_errors: "Infra errors (excluded)", grader_errors: "Trials with grader errors",
                undetermined: "Undetermined trials" }[key];
    } else if (kind === "reason") {
      const r = S.all_failure_reasons[Number(key)];
      if (r) { refs = r.refs; title = "Failure: " + r.reason + (r.grader_id ? " · " + r.grader_id : ""); }
    } else if (kind === "facts") {
      const f = S.key_facts[key];
      if (f) {
        refs = f.refs; title = "Key facts: " + key;
        const rows = [];
        R.tasks.forEach(t => t.trial_rows.forEach(tr => {
          const g = (D.trials[trialKey(t.id, tr.trial)] || {}).grades;
          ((g && g.graders) || []).filter(x => x.type === "key_facts" && x.status === "ok").forEach(x =>
            (x.details.facts || []).filter(fact => fact.verdict === key).forEach(fact => rows.push(el("tr", {},
              td(a(trialHref(t.id, tr.trial), refLabel({ task: t.id, trial: tr.trial }))), td(fact.fact), td(fact.reason || "")))));
        }));
        extra = card("Facts", table(["trial", "fact", "judge's reason"], rows));
      }
    } else if (kind === "tool") {
      const h = S.tool_health[key];
      if (h) {
        title = "Errors from tool " + key;
        refs = h.error_refs;
        extra = card("Failing calls", el("ul", { class: "plain" }, h.error_refs.map(r => el("li", {},
          a(trialHref(r.task, r.trial), refLabel(r)), " events ",
          r.events.map((e, i) => [i ? ", " : "", a(trialHref(r.task, r.trial, e), "#" + e)])))));
      }
    }
    if (!refs) return notFound("No list " + kind + "/" + key + ".");
    crumbs([["overview", "#/"], [title]]);
    const rows = refs.map(ref => {
      const r = trialRow(ref.task, ref.trial) || {};
      return el("tr", {}, td(a(trialHref(ref.task, ref.trial), refLabel(ref))), td(verdictBadge(r.verdict)),
        td(r.status || "–"), td(num(r.score), "num"), td(r.reason || ""));
    });
    return [el("h1", {}, title), extra, card("Trials (" + refs.length + ")",
      table(["trial", "verdict", "status", { label: "score", num: true }, "status reason"], rows))];
  }

  function eventText(e) {
    switch (e.type) {
      case "user_message": return (e.initial ? "opening message: " : "reply: ") + e.content;
      case "clarification": return "asks " + e.to + ": " + e.query;
      case "llm_call": return e.agent + " · " + (e.parse_ok ? e.action : "unparsed output") + " · " + num(e.latency_s, 1) + "s · tokens " +
        int(e.tokens_in) + "/" + int(e.tokens_out) + (e.summary ? "\n" + e.summary : "") + (e.error ? "\nerror: " + e.error : "");
      case "tool_call": return e.tool + "(" + JSON.stringify(e.args) + ")\n→ " + (e.error ? "ERROR: " + e.error : String(e.output));
      case "final": return String(e.output);
      case "handoff": return e.from_agent + " → " + (e.to_agent || "(end)") + (e.action ? " (" + e.action + ")" : "");
      case "error": return String(e.error);
      default: return JSON.stringify(e);
    }
  }

  function graderBlock(task, n, g) {
    const ev = (g.evidence || []).map(i => a(trialHref(task, n, i), "#" + i));
    const blocks = [];
    if (g.type === "llm_rubric" && g.details && g.details.dimensions)
      blocks.push(table(["dimension", { label: "score", num: true }, "judge's comment", "evidence"],
        Object.entries(g.details.dimensions).map(([name, d]) => el("tr", {},
          td(S.dimensions[name] ? a(dimHref(name), name) : name),
          td(d.score === null ? "? undetermined" : d.score + " (" + ((d.scale || {})[String(d.score)] || "") + ")", "num"),
          td(d.comment || d.reasoning || ""), td((d.evidence || []).map((i, k) => [k ? " " : "", a(trialHref(task, n, i), "#" + i)]))))));
    if (g.type === "key_facts" && g.details && g.details.facts)
      blocks.push(table(["fact", "verdict", "judge's comment", "evidence"], g.details.facts.map(f => el("tr", {},
        td(f.fact), td(el("span", { class: "badge " + ({ present: "pass", contradicted: "fail", undetermined: "warn" }[f.verdict] || "neutral") },
          ({ present: "✓ ", contradicted: "✗ ", undetermined: "? ", absent: "– " }[f.verdict] || "") + f.verdict)),
        td(f.comment || f.reasoning || ""), td((f.evidence || []).map((i, k) => [k ? " " : "", a(trialHref(task, n, i), "#" + i)]))))));
    if (g.type === "state_check" && g.details && g.details.observed)
      blocks.push(el("pre", { class: "block" }, "observed " + JSON.stringify(g.details.observed) + "\nexpect   " + JSON.stringify(g.details.expect)));
    return el("div", { class: "grader" },
      el("header", {}, graderBadge(g), el("strong", { class: "mono" }, g.id),
        g.score !== null && g.score !== undefined ? el("span", { class: "muted" }, "score " + num(g.score)) : null,
        g.required ? el("span", { class: "badge outline" }, "required") : null,
        el("span", { class: "badge outline" }, "weight " + g.weight),
        g.min_score !== null && g.min_score !== undefined ? el("span", { class: "badge outline" }, "min_score " + g.min_score) : null),
      el("p", { class: "reason" }, g.reason || ""),
      ev.length ? el("div", { class: "evidence" }, "evidence:", ev.map((x, i) => [i ? " " : "", x])) : null,
      blocks);
  }

  function trialView(task, n, query) {
    const row = trialRow(task, n), data = D.trials[trialKey(task, n)];
    if (!row || !data) return notFound("No trial " + n + " of task " + task + " in this run.");
    crumbs([["overview", "#/"], ["task " + task, taskHref(task)], ["t" + n]]);
    const [cls, label] = VERDICT[row.verdict] || ["neutral", row.verdict];
    const events = data.events || [];
    const grades = data.grades || {};
    const out = [el("h1", {}, task + " · trial " + n),
      el("div", { class: "banner " + cls }, el("strong", {}, label),
        el("span", {}, "status " + row.status + (row.reason ? " — " + row.reason : "")),
        row.score !== null && row.score !== undefined ? el("span", {}, "weighted score " + num(row.score)) : null)];

    const reasons = (row.failure_reasons || []).map(r => el("li", {}, el("span", { class: "badge fail" }, "✗ " + r.reason),
      r.grader_id ? el("span", { class: "mono" }, r.grader_id) : null, el("span", {}, r.detail || "")));
    out.push(el("div", { class: "grid2" },
      card("Failure reasons", reasons.length ? el("ul", { class: "plain" }, reasons) : el("p", { class: "empty" }, "none")),
      card("Trial", el("dl", { class: "meta" }, [
        ["session", row.session_id], ["duration", num(row.duration_s, 1) + "s"], ["LLM calls", row.llm_calls],
        ["tokens in / out", int(row.tokens_in) + " / " + int(row.tokens_out)], ["tool calls / errors", row.tool_calls + " / " + row.tool_errors],
        ["clarifications", row.clarifications], ["client", clientLabel(row.user_mode) + (row.default_used ? " (default reply used)" : "")],
        ["simulator calls", row.sim_calls], ["judge calls", grades.judge ? grades.judge.calls : "–"],
        ["teardown", row.teardown_error ? "✕ " + row.teardown_error : "ok"],
      ].map(([k, v]) => [el("dt", {}, k), el("dd", {}, v === undefined || v === null ? "–" : v)])))));

    out.push(card("Graders", (grades.graders || []).length ? grades.graders.map(g => graderBlock(task, n, g))
      : el("p", { class: "empty" }, row.excluded ? "excluded from grading (infra error)" : "not graded")));

    // time strip
    const tmax = Math.max(0.001, ...events.map(e => e.t || 0));
    const strip = el("div", { class: "strip", role: "list", "aria-label": "events over time" });
    events.forEach(e => {
      const isErr = (e.type === "tool_call" && e.error) || e.type === "error" || (e.type === "llm_call" && e.error);
      const m = a(trialHref(task, n, e.index), isErr ? "✕" : "");
      m.className = "t-" + e.type + (isErr ? " err" : "");
      m.style.left = ((e.t || 0) / tmax * 100) + "%";
      m.setAttribute("title", "#" + e.index + " " + e.type + (isErr ? " (error)" : "") + " at " + num(e.t, 1) + "s");
      m.setAttribute("aria-label", "event " + e.index + " " + e.type + (isErr ? " error" : ""));
      m.setAttribute("role", "listitem");
      strip.appendChild(m);
    });
    const types = [...new Set(events.map(e => e.type))];
    const legend = el("div", { class: "legend" }, types.map(t => el("span", { class: "t-" + t }, t)),
      el("span", { class: "t-error" }, "✕ = error"));
    out.push(card("Timeline", strip, el("div", { class: "strip-axis" }, el("span", {}, "0s"), el("span", {}, num(tmax, 1) + "s")), legend));

    // filterable event list
    const shown = new Set(types);
    const search = el("input", { type: "search", placeholder: "filter events…", "aria-label": "filter events by text" });
    const list = el("div", {});
    function renderEvents() {
      const q = search.value.trim().toLowerCase();
      list.replaceChildren();
      const visible = events.filter(e => shown.has(e.type) && (!q || eventText(e).toLowerCase().includes(q)));
      visible.forEach(e => {
        const isErr = (e.type === "tool_call" && e.error) || e.type === "error";
        list.appendChild(el("div", { class: "event t-" + e.type, id: "ev-" + e.index },
          el("div", { class: "head" }, el("strong", {}, "#" + e.index), el("span", { class: "muted" }, num(e.t, 2) + "s"),
            el("span", { class: "badge outline" }, e.type), isErr ? el("span", { class: "badge fail" }, "✕ error") : null,
            e.agent ? el("span", { class: "muted" }, e.agent) : null),
          el("div", { class: "body" }, eventText(e)),
          el("details", {}, el("summary", {}, "raw event"), el("pre", {}, JSON.stringify(e, null, 2)))));
      });
      if (!visible.length) list.appendChild(el("p", { class: "empty" }, "no events match"));
    }
    const boxes = types.map(t => {
      const cb = el("input", { type: "checkbox", checked: "checked" });
      cb.addEventListener("change", () => { cb.checked ? shown.add(t) : shown.delete(t); renderEvents(); });
      return el("label", { class: "t-" + t }, cb, t);
    });
    search.addEventListener("input", renderEvents);
    renderEvents();
    out.push(card("Events (" + events.length + ")", el("div", { class: "filters" }, boxes, search), list));

    if (query.e !== undefined) setTimeout(() => {
      const target = document.getElementById("ev-" + query.e);
      if (target) { target.classList.add("hl"); target.scrollIntoView({ block: "center" }); }
    }, 0);
    return out;
  }

  // ---------- router ----------
  function parse(hash) {
    const raw = (hash || "#/").replace(/^#\/?/, "");
    const [path, qs] = raw.split("?");
    const segs = path ? path.split("/").map(s => { try { return decodeURIComponent(s); } catch (e) { return s; } }) : [];
    const query = {};
    (qs || "").split("&").filter(Boolean).forEach(p => { const [k, v] = p.split("="); query[k] = decodeURIComponent(v || ""); });
    return { segs, query };
  }
  function view(hash) {
    const { segs, query } = parse(hash);
    if (!segs.length) return overview();
    if (segs[0] === "task" && segs.length === 2) return taskView(segs[1]);
    if (segs[0] === "dimension" && segs.length === 2) return dimensionView(segs[1]);
    if (segs[0] === "trial" && segs.length === 3) return trialView(segs[1], segs[2], query);
    if (segs[0] === "list" && segs.length === 3) return listView(segs[1], segs[2]);
    return notFound("Unknown page " + hash);
  }
  function render(hash) {
    const app = document.getElementById("app");
    app.replaceChildren();
    add(app, view(hash));
    window.scrollTo(0, 0);
  }
  window.addEventListener("hashchange", () => render(location.hash));
  render(location.hash);

  // Visit every page reachable from the overview and check that every internal link resolves.
  window.reportSelfTest = function () {
    const seen = new Set(), broken = [], queue = ["#/"];
    while (queue.length && seen.size < 5000) {
      const h = queue.shift();
      const page = h.split("?")[0];
      if (seen.has(h)) continue;
      seen.add(h);
      render(h);
      if (document.getElementById("not-found")) broken.push(h);
      const q = parse(h).query;
      if (q.e !== undefined && !document.getElementById("ev-" + q.e)) broken.push(h + " (no event " + q.e + ")");
      document.querySelectorAll('a[href^="#/"]').forEach(x => {
        const href = x.getAttribute("href");
        if (!seen.has(href) && !queue.includes(href) && (href.split("?")[0] !== page || href.includes("?"))) queue.push(href);
      });
    }
    render(location.hash);
    return { visited: seen.size, broken };
  };
})();
