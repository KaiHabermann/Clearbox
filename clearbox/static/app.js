// Clearbox frontend: plan DAG view + live agent transcripts. No build step, no deps.
// Entry module: routing, the plan list and plan header, plan reviews, the live feed and boot.

import {
  $, agentModel, agentSelect, api, debounce, el, fmtDur, fmtTime, harnessOfName, hcls, lastSeg, md,
  planDefaultAgent, post, put, reviewActivity, reviewKey, state, truncate, updateTokenCounters,
  verdictInfo,
} from "./core.js";
import { renderGraph } from "./graph.js";
import {
  appendEvent, closeDrawer, closeFullView, openFullView, refreshNodeInfo, renderFullReview,
  selectNode, updateTyping,
} from "./drawer.js";
import { closeAgentPanel, loadAgents, loadAgentsSoon, openAgentPanel } from "./agents.js";

// ---------------------------------------------------------------- routing
function parseHash() {
  const m = location.hash.match(/^#\/plan\/(\d+)(?:\/node\/(\d+)(\/full)?)?/);
  return m ? { plan: +m[1], node: m[2] ? +m[2] : null, full: !!m[3] } : null;
}
function parseAgentHash() {
  const m = location.hash.match(/^#\/agents(?:\/(\d+|new))?\/?$/);
  return m ? { agent: m[1] == null ? "list" : m[1] === "new" ? "new" : +m[1] } : null;
}
export function setHash() {
  let h;
  if (state.agentSel != null) h = state.agentSel === "list" ? "#/agents" : `#/agents/${state.agentSel}`;
  else if (state.planId != null) {
    h = `#/plan/${state.planId}`;
    if (state.sel && state.sel.plan_id === state.planId) h += `/node/${state.sel.node_id}${state.fullOpen ? "/full" : ""}`;
  } else if (parseAgentHash()) h = location.pathname + location.search;  // agent panel closed, no plan shown
  else return;
  if (location.hash !== h) history.replaceState(null, "", h);
}
window.addEventListener("hashchange", () => {
  const ag = parseAgentHash();
  if (ag) { if (ag.agent !== state.agentSel) openAgentPanel(ag.agent); return; }
  if (state.agentSel != null) closeAgentPanel(false);
  const r = parseHash();
  if (!r) return;
  if (r.plan !== state.planId) {
    openPlan(r.plan, r.node).then(() => { if (r.full && r.node != null) openFullView(false); });
    return;
  }
  if (r.node != null && (!state.sel || state.sel.node_id !== r.node)) selectNode(r.plan, r.node);
  if (r.full && r.node != null && !state.fullOpen) openFullView(false);
  if (!r.full && state.fullOpen) closeFullView(false);
});

// ------------------------------------------------------------- plan list
export async function loadPlans() {
  state.plans = await api("/api/plans");
  renderPlanList();
}
function renderPlanList() {
  const ul = $("#plan-list");
  ul.innerHTML = "";
  const rootId = state.data?.root?.id;
  if (!state.plans.length) ul.append(el("li", { style: "cursor:default;color:var(--faint)" }, "", "No plans yet"));
  for (const p of state.plans) {
    const pr = p.progress, tot = Math.max(pr.total, 1);
    const st = p.status === "empty" ? "empty" : (pr.running ? "running" : p.status);
    const bar = el("div", { class: "bar" },
      el("i", { style: `width:${(pr.done / tot) * 100}%;background:var(--done)` }),
      el("i", { style: `width:${(pr.running / tot) * 100}%;background:var(--running)` }),
      el("i", { style: `width:${(pr.failed / tot) * 100}%;background:var(--failed)` }));
    ul.append(el("li", { class: p.id === rootId ? "sel" : "", onclick: () => openPlan(p.id) },
      el("i", { class: `dot ${st}` }),
      el("span", { class: "pl-title", title: p.title }, p.title),
      el("span", { class: "pl-id" }, `#${p.id}`),
      bar,
      el("span", { class: "pl-dir", title: p.working_directory }, lastSeg(p.working_directory))));
  }
}

// ------------------------------------------------------------- plan view
export async function openPlan(planId, nodeId = null) {
  if (planId !== state.planId) { state.sel = null; closeDrawer(false); state.workdirEdit = false; state.testsEdit = false; }
  state.planId = planId;
  await refreshPlan();
  if (nodeId != null) selectNode(planId, nodeId);
  setHash();
  renderPlanList();
}

export async function refreshPlan() {
  if (state.planId == null) return;
  try {
    state.data = await api(`/api/plans/${state.planId}`);
  } catch (e) {
    state.data = null; state.planId = null;
    renderEmpty("Plan not found."); return;
  }
  renderHeader();
  renderGraph();
  if (state.sel) refreshNodeInfo();
}
const refreshPlanSoon = debounce(refreshPlan, 120);
const loadPlansSoon = debounce(loadPlans, 400);

function renderEmpty(html) {
  $("#nodes").innerHTML = ""; $("#edges").innerHTML = "";
  const e = $("#empty"); e.innerHTML = html; e.classList.remove("hidden");
  $("#crumbs").innerHTML = ""; $("#controls").innerHTML = ""; $("#plan-status").textContent = ""; $("#active-bar").innerHTML = ""; $("#plan-agent").innerHTML = ""; $("#plan-review-agent").innerHTML = ""; $("#plan-review").innerHTML = ""; $("#plan-workdir").innerHTML = ""; $("#plan-tests").innerHTML = "";
}

// Working-directory pill: shows the resolved directory (hover for the full path), clickable to
// change it on a not-yet-started top-level plan. Skipped while state.workdirEdit is true so an SSE-
// triggered refresh does not clobber an input the user is actively typing into.
function renderWorkdir(d) {
  if (state.workdirEdit) return;
  const box = $("#plan-workdir");
  box.innerHTML = "";
  const dir = d.working_directory, topLevel = !d.ancestors.length, editable = topLevel && !d.root.started;
  box.className = `workdir-ctl${editable ? "" : " locked"}`;
  box.title = editable ? `Working directory — click to change:\n${dir}`
    : `Working directory:\n${dir}` + (topLevel ? "\n(locked while the plan is running)" : "\n(set on the top-level plan)");
  box.append(el("span", { class: "wd-ic" }, "📁"), el("span", { class: "wd-txt" }, lastSeg(dir)));
  box.onclick = editable ? () => startWorkdirEdit(d.plan.id, dir) : null;
}
function startWorkdirEdit(planId, current) {
  state.workdirEdit = true;
  const box = $("#plan-workdir");
  box.onclick = null;
  box.className = "workdir-ctl editing";
  box.innerHTML = "";
  const input = el("input", { class: "workdir-input", value: current });
  const err = el("span", { class: "workdir-err" });
  let done = false;
  const finish = async () => {
    if (done) return;
    const val = input.value.trim();
    if (val === current) { done = true; state.workdirEdit = false; renderHeader(); return; }
    try {
      await put(`/api/plans/${planId}/workdir`, { dir: val || null });
      done = true; state.workdirEdit = false;
      await refreshPlan();
    } catch (e) { err.textContent = e.message; }
  };
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") finish();
    if (e.key === "Escape") { done = true; state.workdirEdit = false; renderHeader(); }
  });
  input.addEventListener("blur", () => setTimeout(finish, 100));
  box.append(input, err);
  input.focus(); input.select();
}

// Test-instructions pill: how the project's tests are run, handed verbatim to workers and
// reviewers. Set on the top-level plan (subplans inherit it); editable at any time, since each
// node reads it when it starts. Ctrl+Enter or leaving the box saves, Escape cancels.
function renderTests(d) {
  if (state.testsEdit) return;
  const box = $("#plan-tests");
  box.innerHTML = "";
  const text = d.test_instructions || "", topLevel = !d.ancestors.length;
  box.className = `workdir-ctl${topLevel ? "" : " locked"}`;
  box.title = (text ? `How to run the tests:\n${text}` : "No test instructions: agents work out how to run the tests from the repository")
    + (topLevel ? "\n— click to change" : "\n(set on the top-level plan)");
  box.append(el("span", { class: "wd-ic" }, "🧪"), el("span", { class: "wd-txt" }, text ? "tests" : "tests: auto"));
  box.onclick = topLevel ? () => startTestsEdit(d.plan.id, text) : null;
}
function startTestsEdit(planId, current) {
  state.testsEdit = true;
  const box = $("#plan-tests");
  box.onclick = null;
  box.className = "workdir-ctl editing";
  box.innerHTML = "";
  const input = el("textarea", { class: "workdir-input", rows: 4, placeholder: "how to run this project's tests (empty = agents work it out)" });
  input.value = current;
  const err = el("span", { class: "workdir-err" });
  let done = false;
  const finish = async () => {
    if (done) return;
    const val = input.value.trim();
    if (val === current) { done = true; state.testsEdit = false; renderHeader(); return; }
    try {
      await api(`/api/plans/${planId}`, { method: "PATCH", body: JSON.stringify({ test_instructions: val || null }) });
      done = true; state.testsEdit = false;
      await refreshPlan();
    } catch (e) { err.textContent = e.message; }
  };
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && e.ctrlKey) finish();
    if (e.key === "Escape") { done = true; state.testsEdit = false; renderHeader(); }
  });
  input.addEventListener("blur", () => setTimeout(finish, 100));
  box.append(input, err);
  input.focus();
}

function renderHeader() {
  const d = state.data, crumbs = $("#crumbs");
  crumbs.innerHTML = "";
  for (const a of d.ancestors) {
    crumbs.append(el("a", { onclick: () => openPlan(a.plan_id), title: `plan #${a.plan_id}` }, a.title));
    crumbs.append(el("span", { class: "sep" }, "›"));
    crumbs.append(el("a", { onclick: () => openPlan(a.plan_id, a.node_id), title: `node ${a.node_id}` }, `[${a.node_id}] ${a.node_title}`));
    crumbs.append(el("span", { class: "sep" }, "›"));
  }
  crumbs.append(el("span", { class: "cur" }, d.plan.title), " ", el("span", { class: "pl-id", style: "color:var(--faint);font-family:var(--mono);font-size:12px" }, `#${d.plan.id}`));

  const pill = $("#plan-status");
  const st = d.root.started ? (d.status === "done" ? "done" : d.status === "failed" ? "failed" : "running") : (d.status === "done" ? "done" : "idle");
  pill.className = `pill ${st}`;
  pill.textContent = d.root.started ? `${st} · ${d.root.executor}` : st;
  pill.title = `${d.progress.done}/${d.progress.total} tasks done`;

  renderWorkdir(d);
  renderTests(d);

  // plan-wide default agent: applies to this plan's nodes and (unless they set their own) its subplans
  const pa = $("#plan-agent");
  pa.innerHTML = "";
  const eff = planDefaultAgent(d), own = d.plan.default_agent_profile_id ?? null;
  const inherit = d.ancestors.length ? "inherit from parent plan" : "server default";
  pa.className = `agent-ctl ${eff ? hcls(eff.harness) : ""}`;
  pa.title = "Default agent for every node of this plan and its subplans (a node or subplan can override it)."
    + (eff ? `\nNow: ${eff.name} (${agentModel(eff)})` : "");
  pa.append(el("i", { class: "hsw" }), el("span", { class: "lbl" }, "Agent (plan + subplans)"),
    agentSelect(own, own == null && eff ? `— ${inherit}: ${eff.name} —` : `— ${inherit} —`,
      async (id) => { await put(`/api/plans/${d.plan.id}/agent`, { agent: id }); await refreshPlan(); }));

  // reviewer agent: off unless set here (or, for a subplan, on an ancestor -- the API has no
  // resolved "effective reviewer" the way it does for the default agent, so that case is only
  // hinted at, not named)
  const ra = $("#plan-review-agent");
  ra.innerHTML = "";
  const revOwn = d.review_agent_profile_id ?? null;
  const revAgent = revOwn != null ? state.agents.find((p) => p.id === revOwn) : null;
  ra.className = `agent-ctl${revOwn == null ? " review-off" : ""}`;
  ra.title = "Agent that reviews this plan's finished steps, and the plan as a whole once it is done. Advisory only -- it never blocks or edits anything."
    + (revOwn == null ? (d.ancestors.length ? "\nOff here (may still be set on an ancestor plan)." : "\nOff: this plan is not reviewed.") : `\nNow: ${revAgent ? revAgent.name : `profile #${revOwn}`}`);
  ra.append(el("i", { class: "hsw" }), el("span", { class: "lbl" }, revOwn == null ? "Review: off" : "Review agent"),
    agentSelect(revOwn, d.ancestors.length ? "— off (or inherited) —" : "— off —",
      async (id) => { await put(`/api/plans/${d.plan.id}/review-agent`, { agent: id }); await refreshPlan(); }));

  renderPlanReview(d);

  const c = $("#controls");
  c.innerHTML = "";
  const rid = d.root.id;
  if (!d.root.started) {
    const menu = el("div", { class: "menu" });
    const pop = el("div", { class: "menu-pop hidden" },
      el("button", { onclick: () => act(post(`/api/plans/${rid}/start`, { executor: "builtin" })) },
        "Run with built-in agents", el("small", {}, "The service starts a Claude session per node")),
      el("button", { onclick: () => act(post(`/api/plans/${rid}/start`, { executor: "external" })) },
        "Open for external agents", el("small", {}, "Nodes wait for claim_next_node via MCP")));
    const btn = el("button", { class: "primary", onclick: (e) => { e.stopPropagation(); pop.classList.toggle("hidden"); } }, "▶ Start");
    menu.append(btn, pop);
    c.append(menu);
  } else if (d.status !== "done") {
    c.append(el("button", { title: "cancel running built-in agents and pause", onclick: () => act(post(`/api/plans/${rid}/stop`)) }, "❚❚ Stop"));
  }
  if (d.progress.failed > 0) {
    const resume = !d.root.started;
    c.append(el("button", {
      title: resume
        ? "Retry every failed node and resume the plan (it is currently stopped)"
        : "Retry every failed node, leaving done nodes and their results alone",
      onclick: () => act(post(`/api/plans/${rid}/retry-failed`).then(() => resume && post(`/api/plans/${rid}/start`, { executor: d.root.executor || "builtin" }))),
    }, `↻ Retry failed (${d.progress.failed})`));
  }
  c.append(el("button", { title: "clear all results and transcripts", onclick: () => confirm("Reset all nodes of this plan (results and transcripts are cleared)?") && act(post(`/api/plans/${rid}/reset`)) }, "Reset"));
  if (!d.ancestors.length)
    c.append(el("button", { class: "danger", onclick: () => confirm(`Delete plan #${rid} and all its subplans?`) && act(api(`/api/plans/${rid}`, { method: "DELETE" }).then(() => { state.planId = null; location.hash = ""; renderEmpty(""); loadPlans().then(autoOpen); })) }, "Delete"));

  const ab = $("#active-bar");
  ab.innerHTML = "";
  if (d.active.length) {
    ab.append(el("span", { class: "lbl" }, "Working now:"));
    for (const a of d.active) {
      const h = a.agent_kind === "external" ? null : harnessOfName(a.agent_name);
      ab.append(el("span", { class: "chip", title: `${a.agent_kind} agent ${a.agent_name || ""}${h ? ` (${h})` : ""}`, onclick: () => openPlan(a.plan_id, a.node_id) },
        el("i", { class: "dot running" }), a.title,
        a.agent_name && el("span", { class: `who ${h ? hcls(h) : ""}` }, a.agent_kind === "external" ? `ext · ${a.agent_name}` : a.agent_name),
        el("span", { class: "ref" }, `${a.plan_id}.${a.node_id}`)));
    }
  }
  // user-job nodes the whole plan is parked on: impossible to miss, one click to jump there
  if (d.waiting?.length) {
    ab.append(el("span", { class: "lbl user" }, "Waiting for you:"));
    for (const w of d.waiting) {
      ab.append(el("span", { class: "chip user", title: "this step is your job: open the node and mark it done when it is", onclick: () => openPlan(w.plan_id, w.node_id) },
        el("i", { class: "user-ic" }), w.title,
        el("span", { class: "ref" }, `${w.plan_id}.${w.node_id}`)));
    }
  }
}

export async function act(p) {
  try { await p; } catch (e) { alert(e.message); }
  refreshPlan(); loadPlans();
}

// Shared body for a review's verdict/summary/findings, used by the full-view Review tab and the
// plan-level review popover. `review` is the {verdict, summary, findings, agent, when} shape from
// review_view(), or null if nothing has run yet.
export function renderReviewPane(review, running) {
  const wrap = el("div", { class: "review-pane" });
  if (running) wrap.append(el("div", { class: "review-running" }, el("i", { class: "spinner-dot" }), "Review running…"));
  if (!review) {
    if (!running) wrap.append(el("div", { class: "files-empty" }, "Not reviewed yet."));
    return wrap;
  }
  const vi = verdictInfo(review.verdict);
  wrap.append(
    el("div", { class: `review-verdict ${review.verdict}` }, el("span", { class: "rv-glyph" }, vi.glyph), vi.label,
      el("button", { class: "copy-review", title: "copy this review as text",
        onclick: (e) => copyText(reviewAsText(review), e.target) }, "Copy")),
    el("div", { class: "review-meta" }, [review.agent, review.when && fmtTime(review.when)].filter(Boolean).join(" · ")),
    el("div", { class: "review-summary", html: md(review.summary) }),
    review.findings && el("div", { class: "review-findings", html: md(review.findings) }));
  return wrap;
}

// Reviews get pasted into a chat to act on, so copy the markdown the reviewer wrote rather than
// the rendered DOM: the verdict line, the summary and the findings, in that order.
function reviewAsText(review) {
  return [
    `VERDICT: ${review.verdict}`,
    (review.summary || "").trim(),
    (review.findings || "").trim(),
  ].filter(Boolean).join("\n\n");
}

async function copyText(text, btn) {
  const done = (ok) => {
    if (!btn) return;
    const was = btn.textContent;
    btn.textContent = ok ? "Copied" : "Copy failed";
    setTimeout(() => { btn.textContent = was; }, 1400);
  };
  try {
    await navigator.clipboard.writeText(text);
    done(true);
  } catch {
    // clipboard API needs a secure context; plain http://127.0.0.1 counts, but fall back anyway
    const ta = el("textarea", { style: "position:fixed;opacity:0" });
    ta.value = text;
    document.body.append(ta);
    ta.select();
    let ok = false;
    try { ok = document.execCommand("copy"); } catch { ok = false; }
    ta.remove();
    done(ok);
  }
}

// Plan-level review: shown where the plan's own status lives, next to #plan-status. A button-
// triggered popover (same pattern as the Start menu) since findings need room to be read, not a pill.
function renderPlanReview(d) {
  const box = $("#plan-review");
  box.innerHTML = "";
  const key = reviewKey(d.plan.id, 0), running = reviewActivity.has(key), rv = d.review;
  const vi = rv && verdictInfo(rv.verdict);
  const cls = running ? "running" : rv ? rv.verdict : "none";
  const label = running ? "reviewing…" : rv ? `plan review: ${vi.label}` : "plan: not reviewed";
  const pop = el("div", { class: "menu-pop review-pop hidden" },
    renderReviewPane(rv, running),
    el("div", { class: "review-actions" },
      el("button", { disabled: running, onclick: (e) => { e.stopPropagation(); triggerPlanReview(d.plan.id); } },
        running ? "Reviewing…" : rv ? "↻ Re-review plan" : "Review plan now")));
  const btn = el("button", { class: `review-chip ${cls}`, title: "Review this plan as a whole for oversights -- titles, descriptions and results, never diffs or source.",
    onclick: (e) => { e.stopPropagation(); pop.classList.toggle("hidden"); } },
    el("span", { class: "rv-glyph" }, running ? "" : vi ? vi.glyph : "–"), label);
  box.append(btn, pop);
}

async function triggerPlanReview(planId) {
  reviewActivity.add(reviewKey(planId, 0));
  if (state.data && state.data.plan.id === planId) renderPlanReview(state.data);
  try { await post(`/api/plans/${planId}/review`); }
  catch (e) { alert(e.message); }
  finally { reviewActivity.delete(reviewKey(planId, 0)); }
  if (state.planId === planId) await refreshPlan(); else loadPlans();
}

export async function triggerNodeReview(planId, nodeId) {
  reviewActivity.add(reviewKey(planId, nodeId));
  renderFullReview();
  try { await post(`/api/plans/${planId}/nodes/${nodeId}/review`); }
  catch (e) { alert(e.message); }
  finally { reviewActivity.delete(reviewKey(planId, nodeId)); }
  if (state.sel && state.sel.plan_id === planId && state.sel.node_id === nodeId) await refreshNodeInfo();
}

// ----------------------------------------------------------- live feed
function connect() {
  const es = new EventSource("/api/stream");
  es.addEventListener("hello", () => { $("#conn").className = "conn on"; $("#conn-text").textContent = "live"; loadAgentsSoon(); loadPlans(); });
  es.onmessage = (m) => {
    const msg = JSON.parse(m.data);
    if (msg.type === "plan") {
      loadPlansSoon();
      if (state.data && msg.root_id === state.data.root.id) refreshPlanSoon();
      else if (msg.deleted && state.planId === msg.plan_id) { state.planId = null; renderEmpty("Plan deleted."); }
    } else if (msg.type === "agents") {
      loadAgentsSoon();
    } else if (msg.type === "tokens") {
      const n = state.data?.nodes.find((x) => x.node_id === msg.node_id);
      if (n && state.data.plan.id === msg.plan_id) {
        n.tokens_in = msg.tokens_in; n.tokens_out = msg.tokens_out;
        // patch the counters in place; only redraw if the node had none yet
        if (!updateTokenCounters(msg.node_id, msg.tokens_in, msg.tokens_out)) renderGraph();
      }
    } else if (msg.type === "event") {
      const ev = msg.event;
      if (ev.data && ev.data.review) {
        // an automatic review's own transcript, tagged so it can be told apart from the node's
        // own run: "running" from its first event to its tagged result, which is written to the
        // store just before this arrives
        const key = reviewKey(ev.plan_id, ev.node_id);
        if (ev.kind === "result") reviewActivity.delete(key); else reviewActivity.add(key);
        if (ev.node_id === 0 && state.data && state.data.plan.id === ev.plan_id) renderPlanReview(state.data);
        else if (state.sel && ev.plan_id === state.sel.plan_id && ev.node_id === state.sel.node_id) renderFullReview();
      }
      if (state.sel && ev.plan_id === state.sel.plan_id && ev.node_id === state.sel.node_id) {
        $("#transcript .typing")?.remove();
        appendEvent(ev);
        if (ev.kind === "status" || ev.kind === "result") refreshNodeInfo(); else updateTyping();
      }
    }
  };
  es.onerror = () => { $("#conn").className = "conn off"; $("#conn-text").textContent = "reconnecting…"; };
}
// keep "working · 1m 12s" labels ticking
setInterval(() => {
  document.querySelectorAll("#nodes text[data-started]").forEach((t) => {
    t.textContent = truncate(`${t.dataset.who} working · ${fmtDur(Date.now() / 1000 - +t.dataset.started)}`, 34);
  });
}, 1000);
document.addEventListener("click", () => document.querySelectorAll(".menu-pop").forEach((p) => p.classList.add("hidden")));

// ------------------------------------------------------------------ boot
async function autoOpen() {
  const ag = parseAgentHash();
  if (ag) openAgentPanel(ag.agent);  // the graph behind it still shows a plan
  const r = parseHash();
  if (r) return openPlan(r.plan, r.node);
  if (state.plans.length) return openPlan(state.plans[0].id);
  renderEmpty(`No plans yet.<br><br>Connect a model to the MCP endpoint <code>${location.origin}/mcp</code><br>and ask it to <code>create_plan</code>, <code>add_node</code> and <code>add_edge</code>.`);
}
(async () => {
  await Promise.all([loadPlans(), loadAgents()]);
  await autoOpen();
  connect();
})();
