// Clearbox frontend: plan DAG view + live agent transcripts. No build step, no deps.
"use strict";

const NODE_W = 230, NODE_H = 88, H_GAP = 46, V_GAP = 66;
const CONFLICT_DY = 26;            // file stagger: each later node of a shares-a-file group sits this much lower
const CHIP_Y = 60, CHIP_H = 17;   // agent chip row inside a node box (between the subtitle and the bottom edge)
const $ = (s) => document.querySelector(s);
const SVGNS = "http://www.w3.org/2000/svg";

const state = {
  plans: [],
  planId: null,        // plan currently shown
  data: null,          // /api/plans/:id response
  sel: null,           // {plan_id, node_id}
  nodeData: null,
  lastEventId: 0,
  view: { x: 0, y: 0, k: 1 },
  fittedFor: null,
  layout: null,
  agents: [],          // /api/agents (public profiles: no secrets)
  agentSel: null,      // agent panel: null (closed) | "list" | "new" | profile id
  agentDirty: false,   // the open agent form has unsaved edits
  workdirEdit: false,  // the topbar working-directory pill is open as an editable input
  fullOpen: false,     // the full-screen node review is open
  fullTab: "changes",  // which full-view tab is showing; kept across nodes while reviewing
  nodeInfoResp: null,  // last /nodes/:id response, used to rebuild #node-info when full view closes
};

// ----------------------------------------------------------------- reviews
// A review (per-node or, with node_id 0, per-plan) is advisory: verdict/summary/findings written
// by a separate reviewer agent, never blocking or editing anything. Manual triggers (POST
// .../review) await the finished review and return it directly; the engine's own automatic
// reviews run in the background and are only visible through review-tagged transcript events
// (data.review === true, same plan_id/node_id as the thing being reviewed, node_id 0 for a
// plan-level review) -- reviewActivity tracks those so the UI can say "running" meanwhile.
const reviewActivity = new Set();
const reviewKey = (planId, nodeId) => `${planId}:${nodeId}`;
const VERDICT_INFO = {
  ok: { glyph: "✓", label: "ok" },
  concerns: { glyph: "!", label: "concerns" },
  error: { glyph: "?", label: "couldn't review" },
};
function verdictInfo(v) { return VERDICT_INFO[v] || { glyph: "?", label: v }; }

// ------------------------------------------------------------------ utils
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
function el(tag, attrs = {}, ...kids) {
  const e = tag.startsWith("svg:") ? document.createElementNS(SVGNS, tag.slice(4)) : document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "class") e.setAttribute("class", v);
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (k === "html") e.innerHTML = v;
    else e.setAttribute(k, v);
  }
  for (const kid of kids.flat()) if (kid != null && kid !== false) e.append(kid.nodeType ? kid : document.createTextNode(kid));
  return e;
}
async function api(path, opts = {}) {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  if (!r.ok) { const t = await r.text(); let m = t; try { m = JSON.parse(t).detail; } catch {} throw new Error(m); }
  return r.json();
}
const post = (p, body) => api(p, { method: "POST", body: JSON.stringify(body || {}) });
function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }
function fmtTime(ts) { return new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }); }
function fmtDur(s) { if (s == null) return ""; s = Math.max(0, Math.round(s)); return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${s % 60}s`; }
function fmtTokens(n) { return n < 1000 ? `${n}` : `${(n / 1000).toFixed(n < 10000 ? 1 : 0)}k`; }

// "↓12.3k ↑4.5k" in the node box: input and output tokens as separate live counters.
// Tagged with the node id so a {"type":"tokens"} message can retarget them without a redraw.
function tokenCounters(tin, tout, nodeId) {
  const g = el("svg:g", { class: "tok", "data-tok": nodeId });
  const t = el("svg:text", { x: NODE_W - 12, y: 50, "text-anchor": "end" },
    el("svg:tspan", { class: "tok-in" }, `↓${fmtTokens(tin)}`),
    el("svg:tspan", { class: "tok-out", dx: 7 }, `↑${fmtTokens(tout)}`));
  t.append(el("svg:title", {}, `${tin.toLocaleString()} tokens in · ${tout.toLocaleString()} out`));
  g.append(t);
  return g;
}

function updateTokenCounters(nodeId, tin, tout) {
  const g = document.querySelector(`#nodes g.tok[data-tok="${nodeId}"]`);
  if (!g) return false;
  g.querySelector(".tok-in").textContent = `↓${fmtTokens(tin)}`;
  g.querySelector(".tok-out").textContent = `↑${fmtTokens(tout)}`;
  g.querySelector("title").textContent = `${tin.toLocaleString()} tokens in · ${tout.toLocaleString()} out`;
  return true;
}
function mdInline(s) {
  return esc(s).replace(/`([^`]+)`/g, "<code>$1</code>").replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>");
}
function mdBlock(text) {
  // headings, bullet/numbered lists; everything else keeps its raw newlines (containers use pre-wrap)
  const lines = String(text).split("\n");
  let html = "", list = null;
  const closeList = () => { if (list) { html += `</${list}>`; list = null; } };
  for (const line of lines) {
    const h = line.match(/^(#{1,6})\s+(.*)$/);
    const ul = line.match(/^\s*[-*]\s+(.*)$/);
    const ol = line.match(/^\s*\d+[.)]\s+(.*)$/);
    if (h) { closeList(); html += `<h${h[1].length}>${mdInline(h[2])}</h${h[1].length}>\n`; }
    else if (ul) { if (list !== "ul") { closeList(); html += "<ul>"; list = "ul"; } html += `<li>${mdInline(ul[1])}</li>`; }
    else if (ol) { if (list !== "ol") { closeList(); html += "<ol>"; list = "ol"; } html += `<li>${mdInline(ol[1])}</li>`; }
    else { closeList(); html += mdInline(line) + "\n"; }
  }
  closeList();
  return html;
}
function md(text) {
  // tiny, safe markdown: code fences, headings, lists, inline code, bold
  const parts = String(text ?? "").split(/```/);
  return parts.map((p, i) => (i % 2) ? `<pre>${esc(p.replace(/^\w*\n/, ""))}</pre>` : mdBlock(p)).join("");
}
function truncate(s, n) { s = String(s ?? ""); return s.length > n ? s.slice(0, n - 1) + "…" : s; }
function lastSeg(path) {
  const parts = String(path ?? "").split(/[\\/]/).filter(Boolean);
  return parts.length ? parts[parts.length - 1] : String(path ?? "");
}
const put = (p, body) => api(p, { method: "PUT", body: JSON.stringify(body || {}) });

// ---------------------------------------------------------------- agents
// Nodes carry their resolved agent ({name, harness, model, profile_id, source, from_plan});
// these helpers turn that into chips, labels and selects. Colours come from --h-<harness>.
const HARNESSES = ["claude-sdk", "openai", "mock"];
const hcls = (harness) => `h-${HARNESSES.includes(harness) ? harness : "other"}`;
const agentKey = (a) => (a ? (a.profile_id != null ? `p${a.profile_id}` : `n${a.name}`) : "");
const agentModel = (a) => (a.harness === "mock" ? "mock" : `${a.harness} / ${a.model || "default model"}`);
function harnessOfName(name) {
  // active_nodes only carries agent_name; map it back to a harness via the profile list
  const p = state.agents.find((x) => x.name === name);
  if (p) return p.harness;
  return name === "mock" ? "mock" : name === "claude" ? "claude-sdk" : null;
}
function sourceText(a) {
  if (a.source === "node") return "set on this node";
  if (a.source === "plan") return "plan default";
  if (a.source === "inherited") return a.from_plan != null ? `inherited from plan ${a.from_plan}` : "inherited";
  return "server default";
}
// User-job nodes: a step a person does (push a branch, open an MR, approve something). No agent
// runs one; the plan parks on it until the user marks it done. kind is "agent" or "user".
const isUserNode = (n) => n.kind === "user";
const isWaiting = (n) => isUserNode(n) && (n.waiting_for_user || n.status === "ready");
function planDefaultAgent(d) {
  // the agent a node of this plan gets when it has no override of its own
  if (!d) return null;
  if (d.default_agent) return { ...d.default_agent, profile_id: d.default_agent.id };
  return d.nodes.map((n) => n.agent).find((a) => a && a.source !== "node") || null;
}
async function loadAgents() {
  try { state.agents = await api("/api/agents"); } catch { state.agents = []; }
  agentsLoaded();
}
const loadAgentsSoon = debounce(async () => { await loadAgents(); refreshPlan(); }, 150);
function agentSelect(current, inheritLabel, onchange, attrs = {}) {
  // "" = inherit (PUT agent: null); otherwise a profile id
  const sel = el("select", { class: "agent-sel", ...attrs },
    el("option", { value: "" }, inheritLabel),
    state.agents.map((p) => el("option", { value: p.id }, `${p.name} (${agentModel(p)})${p.default ? " ★" : ""}`)));
  if (current != null && !state.agents.some((p) => p.id === current)) sel.append(el("option", { value: current }, `profile #${current}`));
  sel.value = current == null ? "" : String(current);
  sel.addEventListener("change", async () => {
    sel.disabled = true;
    try { await onchange(sel.value === "" ? null : +sel.value); }
    catch (e) { alert(e.message); sel.value = current == null ? "" : String(current); sel.disabled = false; }
  });
  return sel;
}

// ---------------------------------------------------------------- routing
function parseHash() {
  const m = location.hash.match(/^#\/plan\/(\d+)(?:\/node\/(\d+)(\/full)?)?/);
  return m ? { plan: +m[1], node: m[2] ? +m[2] : null, full: !!m[3] } : null;
}
function parseAgentHash() {
  const m = location.hash.match(/^#\/agents(?:\/(\d+|new))?\/?$/);
  return m ? { agent: m[1] == null ? "list" : m[1] === "new" ? "new" : +m[1] } : null;
}
function setHash() {
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
async function loadPlans() {
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
async function openPlan(planId, nodeId = null) {
  if (planId !== state.planId) { state.sel = null; closeDrawer(false); state.workdirEdit = false; }
  state.planId = planId;
  await refreshPlan();
  if (nodeId != null) selectNode(planId, nodeId);
  setHash();
  renderPlanList();
}

async function refreshPlan() {
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
  $("#crumbs").innerHTML = ""; $("#controls").innerHTML = ""; $("#plan-status").textContent = ""; $("#active-bar").innerHTML = ""; $("#plan-agent").innerHTML = ""; $("#plan-review-agent").innerHTML = ""; $("#plan-review").innerHTML = ""; $("#plan-workdir").innerHTML = "";
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

async function act(p) {
  try { await p; } catch (e) { alert(e.message); }
  refreshPlan(); loadPlans();
}

// Shared body for a review's verdict/summary/findings, used by the full-view Review tab and the
// plan-level review popover. `review` is the {verdict, summary, findings, agent, when} shape from
// review_view(), or null if nothing has run yet.
function renderReviewPane(review, running) {
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

async function triggerNodeReview(planId, nodeId) {
  reviewActivity.add(reviewKey(planId, nodeId));
  renderFullReview();
  try { await post(`/api/plans/${planId}/nodes/${nodeId}/review`); }
  catch (e) { alert(e.message); }
  finally { reviewActivity.delete(reviewKey(planId, nodeId)); }
  if (state.sel && state.sel.plan_id === planId && state.sel.node_id === nodeId) await refreshNodeInfo();
}

// ---------------------------------------------------------------- layout
function computeLayout(nodes, edges, planId) {
  const ids = nodes.map((n) => n.node_id);
  const preds = new Map(ids.map((i) => [i, []])), succ = new Map(ids.map((i) => [i, []]));
  const valid = edges.filter(([a, b]) => preds.has(b) && succ.has(a));
  for (const [a, b] of valid) { preds.get(b).push(a); succ.get(a).push(b); }
  // 1. longest-path layering (Kahn order): every node sits below all its prerequisites
  const indeg = new Map(ids.map((i) => [i, preds.get(i).length])), layer = new Map();
  const queue = ids.filter((i) => indeg.get(i) === 0);
  queue.forEach((i) => layer.set(i, 0));
  while (queue.length) {
    const v = queue.shift();
    for (const w of succ.get(v)) {
      layer.set(w, Math.max(layer.get(w) ?? 0, layer.get(v) + 1));
      indeg.set(w, indeg.get(w) - 1);
      if (indeg.get(w) === 0) queue.push(w);
    }
  }
  // 2. split edges spanning several layers with virtual nodes so they route around real nodes
  const DUMMY_W = 16;
  const width = new Map(ids.map((i) => [i, NODE_W]));
  const up = new Map(ids.map((i) => [i, []])), down = new Map(ids.map((i) => [i, []]));
  const chains = new Map(); // "a-b" -> [a, d1, d2, ..., b]
  let dummy = 0;
  for (const [a, b] of valid) {
    const chain = [a];
    for (let l = layer.get(a) + 1; l < layer.get(b); l++) {
      const d = `d${dummy++}`;
      layer.set(d, l); width.set(d, DUMMY_W); up.set(d, []); down.set(d, []);
      chain.push(d);
    }
    chain.push(b);
    for (let i = 0; i + 1 < chain.length; i++) { down.get(chain[i]).push(chain[i + 1]); up.get(chain[i + 1]).push(chain[i]); }
    chains.set(`${a}-${b}`, chain);
  }
  const layers = [];
  for (const v of layer.keys()) (layers[layer.get(v)] ||= []).push(v);
  const key = (v) => (typeof v === "number" ? v : 1e6 + +v.slice(1));
  layers.forEach((L) => L.sort((x, y) => key(x) - key(y)));
  // 3. barycenter ordering sweeps to reduce crossings
  const pos = new Map();
  layers.forEach((L) => L.forEach((v, i) => pos.set(v, i)));
  const bary = (v, nb) => { const ns = nb.get(v); return ns.length ? ns.reduce((s, u) => s + pos.get(u), 0) / ns.length : pos.get(v); };
  for (let it = 0; it < 8; it++) {
    const goDown = it % 2 === 0;
    const order = goDown ? layers.map((_, i) => i) : layers.map((_, i) => layers.length - 1 - i);
    for (const li of order) {
      const nb = goDown ? up : down;
      const b = new Map(layers[li].map((v) => [v, bary(v, nb)]));
      layers[li].sort((x, y) => b.get(x) - b.get(y) || key(x) - key(y));
      layers[li].forEach((v, i) => pos.set(v, i));
    }
  }
  // 4. file stagger: nodes of one layer that share files run one after the other (lowest node id
  //    first), so each is nudged down one CONFLICT_DY per place in that order. Groups are the connected
  //    components of "shares a file" within the layer: in a chain A-B, B-C all three need their own
  //    slot, since B can run with neither. Conflicts come from the API (the scheduler's own rule,
  //    directory prefixes included) and are never re-derived here; other layers and other plans of the
  //    tree can conflict too, but only a same-layer group is drawn as a stagger.
  const byLayer = new Map(nodes.map((n) => [n.node_id, layer.get(n.node_id)]));
  const offset = new Map(), groups = [], seen = new Set();
  const adj = new Map(nodes.map((n) => [n.node_id, (n.conflicts || [])
    .filter((c) => c.plan_id === planId && c.node_id !== n.node_id && byLayer.get(c.node_id) === byLayer.get(n.node_id))
    .map((c) => c.node_id)]));
  for (const n of [...nodes].sort((a, b) => a.node_id - b.node_id)) {
    if (seen.has(n.node_id) || !adj.get(n.node_id).length) continue;
    const comp = [], stack = [n.node_id];
    seen.add(n.node_id);
    while (stack.length) {
      const v = stack.pop(); comp.push(v);
      for (const w of adj.get(v) || []) if (!seen.has(w)) { seen.add(w); stack.push(w); }
    }
    comp.sort((a, b) => a - b).forEach((v, r) => offset.set(v, r * CONFLICT_DY));
    groups.push(comp);
  }
  // 5. y: a layer is as tall as its deepest nudged node, so baselines accumulate instead of being uniform
  const maxOff = layers.map((L) => Math.max(0, ...L.map((v) => offset.get(v) || 0)));
  const top = [0];
  layers.forEach((_, li) => top.push(top[li] + NODE_H + maxOff[li] + V_GAP));
  // 6. x positions: each layer centred, widths respected
  const gap = (a, b) => (typeof a === "number" && typeof b === "number" ? H_GAP : 18);
  const layerW = layers.map((L) => L.reduce((s, v, i) => s + width.get(v) + (i ? gap(L[i - 1], v) : 0), 0));
  const maxW = Math.max(NODE_W, ...layerW);
  const coords = new Map();
  layers.forEach((L, li) => {
    let x = (maxW - layerW[li]) / 2;
    L.forEach((v, i) => {
      if (i) x += gap(L[i - 1], v);
      const off = offset.get(v) || 0;  // virtual nodes keep the baseline: they only route edges
      // top/bot: the layer's band, which edges leave and enter vertically so they never cut across a nudged card
      coords.set(v, { x: x + (width.get(v) - NODE_W) / 2, cx: x + width.get(v) / 2, y: top[li] + off, off, top: top[li], bot: top[li] + NODE_H + maxOff[li] });
      x += width.get(v);
    });
  });
  // 7. connectors between consecutive members of each group; one that would pass behind other cards
  //    of its layer dips under the band instead, interleaved groups nesting at different depths
  const links = [], arcs = layers.map(() => 0);
  for (const grp of groups) {
    for (let i = 0; i + 1 < grp.length; i++) {
      const li = layer.get(grp[i]), xa = coords.get(grp[i]).x, xb = coords.get(grp[i + 1]).x;
      const between = layers[li].some((v) => typeof v === "number" && coords.get(v).x > Math.min(xa, xb) && coords.get(v).x < Math.max(xa, xb));
      links.push({ from: grp[i], to: grp[i + 1], arc: between ? 22 + 12 * (arcs[li]++ % 3) : 0 });
    }
  }
  const last = layers.length - 1;
  const arcBelow = arcs[last] ? 22 + 12 * Math.min(arcs[last] - 1, 2) : 0;  // arcs under the last layer still fit the frame
  return { coords, chains, groups, links, width: maxW, height: top[last] + NODE_H + maxOff[last] + arcBelow };
}

function edgePath(chain, coords) {
  // vertical-tangent cubic segments between layer bands; virtual nodes are passed straight through.
  // A node nudged down by the file stagger is entered (and left) by a straight vertical run through its
  // layer's band, so the curve itself always bends in the gap between layers.
  let d = "";
  for (let i = 0; i + 1 < chain.length; i++) {
    const a = coords.get(chain[i]), b = coords.get(chain[i + 1]);
    const x1 = a.cx, y1 = a.bot;
    const x2 = b.cx, y2 = typeof chain[i + 1] === "number" ? b.top - 3 : b.top;
    const dy = (y2 - y1) / 2;
    const y0 = typeof chain[i] === "number" ? a.y + NODE_H : a.y;
    if (!d) d = `M${x1},${y0}` + (y1 !== y0 ? ` L${x1},${y1}` : "");
    d += ` C${x1},${y1 + dy} ${x2},${y2 - dy} ${x2},${y2}`;
    if (typeof chain[i + 1] !== "number") d += ` L${x2},${b.bot}`;
    else if (b.off) d += ` L${x2},${b.y - 3}`;
  }
  return d;
}

function nodeRef(planId, c) { return `${c.plan_id ?? planId}.${c.node_id} “${truncate(c.title, 40)}”`; }
function fileNote(planId, n) {
  // tooltip lines on a node's files: what it writes, which nodes it runs before/after, whom it waits for
  if (!n.files?.length) return "";
  const lines = [`writes: ${n.files.join(", ")}`];
  const first = (c) => c.plan_id < planId || (c.plan_id === planId && c.node_id < n.node_id);  // (plan_id, node_id) order
  for (const c of n.conflicts || []) lines.push(`${first(c) ? "runs after" : "runs before"} ${nodeRef(planId, c)} (both write ${c.files.join(", ")})`);
  const bb = n.status === "ready" && n.blocked_by;
  if (bb) lines.push(`waiting for ${nodeRef(planId, bb)}, which ${bb.reason === "running" ? "is running and writes" : "goes first on"} ${bb.files.join(", ")}`);
  return lines.join("\n");
}

function fileConnector(a, b, arc = 0) {
  // dashed line between two cards of one stagger group, clipped to their borders (+3 px) so the
  // "runs after" arrowhead lands visibly on the later card's edge. When other cards sit between the
  // two (the barycenter sweep may interleave groups), a straight line would hide behind them and cross
  // the other group's connector, so it dips under the layer band instead, `arc` px deep.
  if (arc) {
    const dir = b.x > a.x ? 1 : -1, sx = a.cx + dir * (NODE_W / 2 - 28), ex = b.cx - dir * (NODE_W / 2 - 28), low = a.bot + arc;
    return `M${sx},${a.y + NODE_H + 2} C${sx},${low} ${ex},${low} ${ex},${b.y + NODE_H + 3}`;
  }
  const c1 = { x: a.x + NODE_W / 2, y: a.y + NODE_H / 2 }, c2 = { x: b.x + NODE_W / 2, y: b.y + NODE_H / 2 };
  const dx = c2.x - c1.x, dy = c2.y - c1.y;
  const t = Math.min((NODE_W / 2 + 3) / Math.max(Math.abs(dx), 1e-6), (NODE_H / 2 + 3) / Math.max(Math.abs(dy), 1e-6));
  return `M${c1.x + dx * t},${c1.y + dy * t} L${c2.x - dx * t},${c2.y - dy * t}`;
}

// ----------------------------------------------------------------- graph
function renderGraph() {
  const d = state.data;
  const gN = $("#nodes"), gE = $("#edges");
  gN.innerHTML = ""; gE.innerHTML = "";
  const empty = $("#empty");
  if (!d.nodes.length) {
    empty.innerHTML = `This plan has no nodes yet.<br><br>Add some through MCP: <code>add_node(plan_id=${d.plan.id}, title, description)</code>`;
    empty.classList.remove("hidden");
    return;
  }
  empty.classList.add("hidden");
  const L = computeLayout(d.nodes, d.edges, d.plan.id);
  state.layout = L;
  const byId = new Map(d.nodes.map((n) => [n.node_id, n]));
  const defKey = agentKey(planDefaultAgent(d));

  // file-stagger connectors, under the dependency edges: muted and dashed, since they are not dependencies,
  // only "shares files with, runs after"; they tie a group together however far apart the sweep put it
  for (const { from, to, arc } of L.links) {
    const a = byId.get(from), b = byId.get(to);
    gE.append(el("svg:path", { class: "fconn", d: fileConnector(L.coords.get(from), L.coords.get(to), arc), "marker-end": "url(#arrow-files)" },
      el("svg:title", {}, `${nodeRef(d.plan.id, b)} runs after ${nodeRef(d.plan.id, a)}: they write the same files`)));
  }

  for (const [a, b] of d.edges) {
    const chain = L.chains.get(`${a}-${b}`);
    if (!chain) continue;
    const na = byId.get(a), nb = byId.get(b);
    const cls = na.status === "done" && nb.status === "running" ? "active" : na.status === "done" ? "done" : "";
    gE.append(el("svg:path", { class: `edge ${cls}`, d: edgePath(chain, L.coords), "marker-end": `url(#arrow${cls ? "-" + cls : ""})` }));
  }

  for (const n of d.nodes) {
    const p = L.coords.get(n.node_id);
    const isSel = state.sel && state.sel.plan_id === d.plan.id && state.sel.node_id === n.node_id;
    const user = isUserNode(n), waitingUser = isWaiting(n);
    const bb = n.status === "ready" && !user ? n.blocked_by : null;  // ready, but a node writing the same files goes first
    const g = el("svg:g", {
      class: `node ${n.status}${bb ? " waiting" : ""}${user ? " user" : ""}${waitingUser ? " for-you" : ""}${isSel ? " sel" : ""}`, transform: `translate(${p.x},${p.y})`,
      onclick: (e) => { e.stopPropagation(); selectNode(d.plan.id, n.node_id); },
      ondblclick: (e) => { e.stopPropagation(); if (n.subplan_id) openPlan(n.subplan_id); },
    });
    g.append(el("svg:title", {}, [n.title, n.description, fileNote(d.plan.id, n), n.subplan_id && "(double-click to open subplan)"].filter(Boolean).join("\n\n")));
    if (n.subplan_id) {
      g.append(el("svg:rect", { class: "ghost", x: 8, y: -8, width: NODE_W, height: NODE_H, rx: 10 }));
      g.append(el("svg:rect", { class: "ghost", x: 4, y: -4, width: NODE_W, height: NODE_H, rx: 10 }));
    }
    // a user job is a different kind of thing, not just a queued node: pill-shaped card with a
    // double border, a person icon instead of the stripe, and no agent chip
    const rx = user ? NODE_H / 2 : 10;
    g.append(el("svg:rect", { class: "halo", x: 0, y: 0, width: NODE_W, height: NODE_H, rx }));
    g.append(el("svg:rect", { class: "card", x: 0, y: 0, width: NODE_W, height: NODE_H, rx }));
    if (user) {
      g.append(el("svg:rect", { class: "card-inner", x: 4, y: 4, width: NODE_W - 8, height: NODE_H - 8, rx: rx - 4 }));
      g.append(el("svg:g", { class: "user-glyph" },
        el("svg:circle", { cx: 24, cy: 22, r: 4.5 }),
        el("svg:path", { d: "M15,37 a9,9 0 0,1 18,0" })));
    } else {
      g.append(el("svg:rect", { class: "stripe", x: 0, y: 10, width: 4, height: NODE_H - 20, rx: 2 }));
    }
    const tx = user ? 36 : 16;
    g.append(el("svg:text", { class: "title", x: tx, y: 27 }, truncate(n.title, user ? 21 : 25)));

    let sub;
    if (n.subplan_id) {
      const pr = n.progress;
      sub = `subplan #${n.subplan_id} · ${pr.done}/${pr.total} done${pr.running ? ` · ${pr.running} running` : ""}`;
      const w = NODE_W - 32, tot = Math.max(pr.total, 1);
      g.append(el("svg:rect", { class: "pbar-bg", x: 16, y: CHIP_Y + 6, width: w, height: 4, rx: 2 }));
      g.append(el("svg:rect", { class: "pbar", x: 16, y: CHIP_Y + 6, width: (w * pr.done) / tot, height: 4, rx: 2 }));
      if (pr.running) g.append(el("svg:rect", { class: "pbar run", x: 16 + (w * pr.done) / tot, y: CHIP_Y + 6, width: (w * pr.running) / tot, height: 4, rx: 2 }));
    } else if (user) {
      sub = waitingUser ? "waiting for you — mark done when it is" : n.status === "done" ? "done by you" : n.status === "failed" ? truncate(n.error || "failed", 32) : "your job · not its turn yet";
    } else if (n.status === "running") {
      const dur = n.started_at ? ` · ${fmtDur(Date.now() / 1000 - n.started_at)}` : "";
      sub = `${n.agent_name || (n.agent_kind === "external" ? "external" : "agent")} working${dur}`;
    } else if (n.status === "done" && n.started_at && n.finished_at) {
      sub = `done in ${fmtDur(n.finished_at - n.started_at)}`;
    } else if (n.status === "failed") {
      sub = truncate(n.error || "failed", 32);
    } else {
      sub = bb ? `${bb.reason === "running" ? "waiting for" : "queued after"} ${bb.plan_id}.${bb.node_id} · shares files`
        : n.status === "ready" ? "ready – waiting for agent" : truncate(n.description || "pending", 32);
    }
    g.append(el("svg:text", {
      class: "sub", x: tx, y: n.subplan_id ? 47 : 50,
      "data-started": !n.subplan_id && n.status === "running" && n.started_at ? n.started_at : null,
      "data-who": n.agent_name || (n.agent_kind === "external" ? "external" : "agent"),
    }, truncate(sub, 34)));
    g.append(el("svg:text", { class: "ref", x: NODE_W - 12, y: 20, "text-anchor": "end" }, `${d.plan.id}.${n.node_id}`));

    if (!n.subplan_id && (n.tokens_in != null || n.tokens_out != null)) {
      // two counters, updated live from {"type":"tokens"} while the node runs
      g.append(tokenCounters(n.tokens_in || 0, n.tokens_out || 0, n.node_id));
    }

    // agent chip: only where this node's agent differs from the plan's default (uniform plans stay quiet);
    // always on an explicit override, which gets a filled dot and a solid border
    const a = n.agent, override = a && a.source === "node";
    let chip = null;
    if (user) {
      chip = el("svg:g", { class: "achip ujob" },
        el("svg:title", {}, "your job: no agent runs this step"),
        el("svg:rect", { x: tx, y: CHIP_Y, width: 40, height: CHIP_H, rx: CHIP_H / 2 }),
        el("svg:text", { x: tx + 8, y: CHIP_Y + 12.5 }, "your job"));
      g.append(chip);
    } else if (a && !n.subplan_id && (override || agentKey(a) !== defKey)) {
      chip = el("svg:g", { class: `achip ${hcls(a.harness)}${override ? " override" : ""}` },
        el("svg:title", {}, `agent: ${a.name} (${agentModel(a)}) · ${sourceText(a)}`),
        el("svg:rect", { x: 16, y: CHIP_Y, width: 40, height: CHIP_H, rx: CHIP_H / 2 }),
        override && el("svg:circle", { cx: 25, cy: CHIP_Y + CHIP_H / 2, r: 3 }),
        el("svg:text", { x: override ? 32 : 24, y: CHIP_Y + 12.5 }, truncate(a.name, 22)));
      g.append(chip);
    }

    // status glyph
    const gx = NODE_W - 22, gy = n.subplan_id ? 50 : CHIP_Y + CHIP_H / 2;
    if (n.status === "running") g.append(el("svg:circle", { class: "spinner", cx: gx, cy: gy, r: 7 }));
    else if (n.status === "done") g.append(el("svg:path", { class: "check", d: `M${gx - 6},${gy} l4,4 l8,-8` }));
    else if (waitingUser) g.append(el("svg:path", { class: "handglyph", d: `M${gx - 7},${gy} h14 M${gx + 1},${gy - 6} l6,6 l-6,6` }));  // "over to you" arrow
    else if (bb) g.append(el("svg:path", { class: "waitglyph", d: `M${gx},${gy - 4} v4 l3,2 M${gx + 7},${gy} a7,7 0 1,1 -14,0 a7,7 0 1,1 14,0` }));
    else if (n.status === "failed") g.append(el("svg:path", { class: "cross", d: `M${gx - 5},${gy - 5} l10,10 M${gx + 5},${gy - 5} l-10,10` }));

    // review marker: quiet for ok, a plainer badge for concerns (advice, not a failure -- a
    // failed node already has its own red state); nothing at all when the node has no review
    if (n.review) {
      const vi = verdictInfo(n.review.verdict), rx = NODE_W - 16, ry = 34;
      const rg = el("svg:g", { class: `review-mark ${n.review.verdict}` },
        el("svg:title", {}, `review (${vi.label}): ${n.review.summary}`),
        el("svg:circle", { cx: rx, cy: ry, r: 6 }),
        el("svg:text", { x: rx, y: ry + 3.5, "text-anchor": "middle" }, vi.glyph));
      g.append(rg);
    }
    gN.append(g);
    if (chip) {  // size the pill to its rendered text, never past the status glyph
      const t = chip.querySelector("text"), pad = +t.getAttribute("x") - tx;
      chip.querySelector("rect").setAttribute("width", Math.min(t.getComputedTextLength() + pad + 9, NODE_W - 16 - 40));
    }
  }

  if (state.fittedFor !== d.plan.id) { fit(); state.fittedFor = d.plan.id; }
}

// ----------------------------------------------------------- pan & zoom
const svg = $("#canvas"), vp = $("#viewport");
function applyView() { const v = state.view; vp.setAttribute("transform", `translate(${v.x},${v.y}) scale(${v.k})`); }
function fit() {
  const L = state.layout; if (!L) return;
  const r = svg.getBoundingClientRect(), pad = 60;
  const k = Math.min(1.15, (r.width - pad * 2) / Math.max(L.width, 1), (r.height - pad * 2) / Math.max(L.height + 10, 1));
  state.view = { k, x: (r.width - L.width * k) / 2, y: Math.max(pad, (r.height - L.height * k) / 2) };
  applyView();
}
function zoomAt(f, cx, cy) {
  const v = state.view, k = Math.min(3, Math.max(0.15, v.k * f));
  v.x = cx - ((cx - v.x) * k) / v.k; v.y = cy - ((cy - v.y) * k) / v.k; v.k = k; applyView();
}
svg.addEventListener("wheel", (e) => {
  e.preventDefault();
  const r = svg.getBoundingClientRect();
  zoomAt(Math.exp(-e.deltaY * 0.0015), e.clientX - r.left, e.clientY - r.top);
}, { passive: false });
let drag = null;
svg.addEventListener("mousedown", (e) => { if (e.button === 0) { drag = { x: e.clientX, y: e.clientY, vx: state.view.x, vy: state.view.y, moved: false }; svg.classList.add("panning"); } });
window.addEventListener("mousemove", (e) => {
  if (!drag) return;
  const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
  if (Math.abs(dx) + Math.abs(dy) > 3) drag.moved = true;
  state.view.x = drag.vx + dx; state.view.y = drag.vy + dy; applyView();
});
window.addEventListener("mouseup", () => { svg.classList.remove("panning"); setTimeout(() => (drag = null), 0); });
svg.addEventListener("click", () => { if (!drag?.moved) closeDrawer(); });
$("#zoom").addEventListener("click", (e) => {
  const z = e.target.dataset.z, r = svg.getBoundingClientRect();
  if (z === "in") zoomAt(1.2, r.width / 2, r.height / 2);
  if (z === "out") zoomAt(1 / 1.2, r.width / 2, r.height / 2);
  if (z === "fit") fit();
});
window.addEventListener("resize", debounce(fit, 150));

// --------------------------------------------------------------- drawer
async function selectNode(planId, nodeId) {
  if (planId !== state.planId) return openPlan(planId, nodeId);
  closeAgentPanel(false);
  const same = state.sel && state.sel.plan_id === planId && state.sel.node_id === nodeId;
  state.sel = { plan_id: planId, node_id: nodeId };
  if (!same) { state.lastEventId = 0; $("#transcript").innerHTML = ""; toolCards.clear(); }
  $("#drawer").classList.remove("closed");
  setHash();
  renderGraph();
  setTimeout(() => ensureVisible(nodeId), 220);
  await refreshNodeInfo(true);
}
function ensureVisible(nodeId) {
  const p = state.layout?.coords.get(nodeId); if (!p) return;
  const r = svg.getBoundingClientRect(), v = state.view, m = 30;
  const x1 = v.x + p.x * v.k, y1 = v.y + p.y * v.k, x2 = x1 + NODE_W * v.k, y2 = y1 + NODE_H * v.k;
  if (x1 < m || y1 < m || x2 > r.width - m || y2 > r.height - m) {
    v.x = r.width / 2 - (p.x + NODE_W / 2) * v.k; v.y = r.height / 2 - (p.y + NODE_H / 2) * v.k; applyView();
  }
}
function closeDrawer(update = true) {
  if (state.fullOpen) closeFullView(false);
  $("#drawer").classList.add("closed");
  state.sel = null; state.nodeData = null; state.nodeInfoResp = null;
  if (update && state.data) { renderGraph(); setHash(); }
}

// ------------------------------------------------------------ full view
// Full-screen node review, opened from the drawer. It does not re-implement the drawer's renderers:
// it physically relocates the live #drawer-head / #node-info / #transcript-head / #transcript
// elements into the full-view layout (and back on close), so renderNodeInfo/appendEvent/etc keep
// working unmodified via their existing #id selectors regardless of which container currently holds
// them. Three tabs share the screen so each gets the full width: Task (#node-info), Changes (its
// own diff browser, below) and Agent output (the transcript). The drawer keeps its inline Changes
// accordion; in full view that copy is hidden by CSS and the Changes tab is used instead.
const drawerSlots = {
  head: $("#drawer-head"), info: $("#node-info"), thead: $("#transcript-head"), transcript: $("#transcript"),
};
const drawerHome = new Map(Object.values(drawerSlots).map((el) => [el, { parent: el.parentNode, next: el.nextSibling }]));

// Changes tab: file list on the left, the selected file's diff filling the rest of the screen.
const FULL_TABS = ["task", "changes", "output", "review"];
function setFullTab(tab) {
  if (!FULL_TABS.includes(tab)) return;
  state.fullTab = tab;
  for (const b of document.querySelectorAll("#full-tabs .ftab")) b.classList.toggle("active", b.dataset.tab === tab);
  for (const p of document.querySelectorAll("#full-body .fpane")) p.classList.toggle("active", p.dataset.pane === tab);
}
function diffStat(diff) {
  let add = 0, del = 0;
  for (const l of diff.split("\n")) {
    if (l.startsWith("+") && !l.startsWith("+++")) add++;
    else if (l.startsWith("-") && !l.startsWith("---")) del++;
  }
  return { add, del };
}
// Agents call the file tools with absolute paths, so strip the plan's working directory:
// "…\Clearbox\clearbox\app.py" reads as "clearbox/app.py" in the file list.
function shortPath(p) {
  const wd = state.data?.working_directory;
  let s = String(p).replace(/\\/g, "/");
  if (wd) {
    const w = wd.replace(/\\/g, "/").replace(/\/$/, "");
    if (s.toLowerCase().startsWith(w.toLowerCase() + "/")) s = s.slice(w.length + 1);
  }
  return s;
}
function showFullDiff(d) {
  for (const b of document.querySelectorAll("#full-file-list .dfile")) b.classList.toggle("active", b.dataset.path === d.path);
  const pane = $("#full-diff");
  pane.innerHTML = "";
  pane.append(
    el("div", { class: "diff-title", title: d.path }, shortPath(d.path)),
    el("pre", { class: "diff-pre full", html: diffHtml(d.diff) }));
  pane.scrollTop = 0;
}
async function loadFullChanges(pid, nodeId) {
  const list = $("#full-file-list"), pane = $("#full-diff");
  list.innerHTML = ""; pane.innerHTML = "";
  list.append(el("div", { class: "files-empty" }, "loading…"));
  const tab = document.querySelector('#full-tabs .ftab[data-tab="changes"]');
  let r;
  try { r = await api(`/api/plans/${pid}/nodes/${nodeId}/diffs`); }
  catch { list.innerHTML = ""; list.append(el("div", { class: "files-empty" }, "could not load diffs")); return; }
  if (!state.fullOpen || !state.sel || state.sel.node_id !== nodeId) return;  // navigated away mid-fetch
  list.innerHTML = "";
  tab.textContent = r.diffs.length ? `Changes (${r.diffs.length})` : "Changes";
  if (!r.diffs.length) {
    list.append(el("div", { class: "files-empty" }, "no file changes captured"));
    pane.append(el("div", { class: "files-empty pad" },
      "This node recorded no file diffs. Files written by bash are not captured."));
    return;
  }
  for (const d of r.diffs) {
    const s = diffStat(d.diff);
    list.append(el("button", { class: "dfile", "data-path": d.path, title: d.path, onclick: () => showFullDiff(d) },
      el("span", { class: "dpath" }, shortPath(d.path)),
      el("span", { class: "dstat" }, el("span", { class: "add" }, `+${s.add}`), " ", el("span", { class: "del" }, `−${s.del}`))));
  }
  showFullDiff(r.diffs[0]);
}
function relocateForFull() {
  if (state.fullOpen && state.sel) loadFullChanges(state.sel.plan_id, state.sel.node_id);
  renderFullReview();
}

// Review tab: verdict/summary/findings for the selected node, straight from state.nodeData (no
// extra fetch -- node_view already carries it), plus a running indicator for an automatic review
// still in flight (tracked in reviewActivity from review-tagged transcript events).
function renderFullReview() {
  const box = $("#full-review");
  if (!box) return;
  box.innerHTML = "";
  const s = state.sel, n = state.nodeData;
  if (!s || !n) return;
  box.append(renderReviewPane(n.review, reviewActivity.has(reviewKey(s.plan_id, s.node_id))));
  if (n.status === "done" && !n.subplan_id) {
    box.append(el("div", { class: "review-actions" }, reviewButton(s.plan_id, n), reworkButton(s.plan_id, n)));
    const form = state.fullOpen && reworkForm(s.plan_id, n);
    if (form) {
      $("#node-info .rework-form")?.remove();  // drawn in the drawer before the full view opened
      box.append(form);
    }
  }
}
function openFullView(push = true) {
  if (!state.sel || state.fullOpen) return;
  state.fullOpen = true;
  $("#full-head-slot").append(drawerSlots.head);
  $("#full-info-col").append(drawerSlots.info);
  $("#full-transcript-col").append(drawerSlots.thead, drawerSlots.transcript);
  setFullTab(state.fullTab || "changes");
  relocateForFull();
  $("#full-view").classList.remove("closed");
  if (push) setHash();
}
function closeFullView(push = true) {
  if (!state.fullOpen) return;
  state.fullOpen = false;
  $("#full-view").classList.add("closed");
  for (const [el, home] of drawerHome) home.parent.insertBefore(el, home.next);
  if (state.nodeInfoResp) renderNodeInfo(state.nodeInfoResp);  // rebuilds #node-info with Changes back inline
  if (push) setHash();
}
function navFull(dir) {
  if (!state.sel || !state.data) return;
  const ids = state.data.nodes.map((n) => n.node_id).sort((a, b) => a - b);
  const i = ids.indexOf(state.sel.node_id);
  if (i < 0) return;
  selectNode(state.planId, ids[(i + dir + ids.length) % ids.length]);
}
$("#full-exit").addEventListener("click", () => closeFullView());
$("#full-prev").addEventListener("click", () => navFull(-1));
$("#full-next").addEventListener("click", () => navFull(1));
for (const b of document.querySelectorAll("#full-tabs .ftab")) b.addEventListener("click", () => setFullTab(b.dataset.tab));
document.addEventListener("keydown", (e) => {
  if (!state.fullOpen || e.defaultPrevented || /^(input|textarea|select)$/i.test(e.target.tagName)) return;
  if (e.key === "Escape") { closeFullView(); return; }
  if (e.key === "ArrowLeft" || e.key === "k") { e.preventDefault(); navFull(-1); }
  else if (e.key === "ArrowRight" || e.key === "j") { e.preventDefault(); navFull(1); }
  else if (e.key >= "1" && e.key <= "4") { e.preventDefault(); setFullTab(FULL_TABS[+e.key - 1]); }
});

async function refreshNodeInfo(withEvents = false) {
  const s = state.sel; if (!s) return;
  let r;
  try { r = await api(`/api/plans/${s.plan_id}/nodes/${s.node_id}?after=${withEvents ? state.lastEventId : 1e18}`); }
  catch { closeDrawer(); return; }
  if (!state.sel || state.sel.plan_id !== s.plan_id || state.sel.node_id !== s.node_id) return;
  state.nodeData = r.node;
  state.nodeInfoResp = r;
  renderNodeInfo(r);
  if (withEvents) r.events.forEach(appendEvent);
  updateTyping();
}

function renderNodeInfo(r) {
  const n = r.node, pid = state.sel.plan_id;
  const head = $("#drawer-head");
  head.innerHTML = "";
  const user = isUserNode(n), waiting = isWaiting(n);
  const agent = user ? "your job" : n.agent_kind ? (n.agent_kind === "external" ? `external · ${n.agent_name || "?"}` : `built-in · ${n.agent_name || "agent"}`) : null;
  head.append(
    el("div", { class: "t" },
      el("h2", {}, n.title),
      el("div", { class: "meta" },
        el("span", { class: `pill ${waiting ? "waiting" : n.status}` }, waiting ? "waiting for you" : n.status),
        el("span", { style: "font-family:var(--mono)" }, `plan ${pid} · node ${n.node_id}`),
        agent && el("span", {}, agent),
        n.session_id && el("span", { title: "agent session id", style: "font-family:var(--mono)" }, truncate(n.session_id, 14)))),
    el("button", { class: "expand", title: "full screen (⤢)", "aria-label": "expand to full screen", onclick: () => openFullView() }, "⤢"),
    el("button", { class: "close", onclick: () => closeDrawer() }, "✕"));

  const info = $("#node-info");
  info.innerHTML = "";
  const markDone = (cls) => el("button", {
    class: cls,
    title: user ? "this step is done: mark it so the nodes after it start" : "stop whatever is working on this node and mark it done, so the nodes after it start",
    onclick: () => completeNode(pid, n),
  }, "✓ Mark done");
  if (user) {
    // the description is the instruction to the person, so it gets the room and the primary button
    info.append(el("div", { class: `user-task${waiting ? " now" : ""}` },
      el("div", { class: "k" }, waiting ? "Your job — the plan is waiting on this" : n.status === "done" ? "Your job — done" : n.status === "failed" ? "Your job — stopped; retry to park the plan here again" : "Your job — not its turn yet"),
      el("div", { class: "v", html: n.description ? md(n.description) : "—" }),
      n.status !== "done" && el("div", { class: "actions" }, markDone(waiting ? "primary" : ""))));
  } else {
    info.append(el("div", {}, el("div", { class: "k" }, "Task"), el("div", { class: "v", html: n.description ? md(n.description) : "—" })));
  }
  if (!n.subplan_id && (n.status === "pending" || n.status === "ready")) info.append(renderKindRow(pid, n));
  const a = n.agent || state.data?.nodes.find((m) => m.node_id === n.node_id)?.agent;
  if (a && !n.subplan_id && !user) {
    const running = n.status === "running", inh = planDefaultAgent(state.data);
    const sel = agentSelect(n.agent_profile_id ?? null, inh ? `— inherit: ${inh.name} —` : "— inherit —",
      async (id) => { await put(`/api/plans/${pid}/nodes/${n.node_id}/agent`, { agent: id }); await refreshPlan(); },
      { disabled: running,
        title: running ? "Locked while the node is running: a session keeps the agent it started with. Stop it or wait until it finishes; the change applies to the next run."
          : "Agent for this node only; \"— inherit —\" clears the override and uses the plan default." });
    info.append(el("div", {}, el("div", { class: "k" }, "Agent"),
      el("div", { class: `v agent-row ${hcls(a.harness)}` },
        el("span", { class: "aname" }, a.name), el("span", {}, `(${agentModel(a)})`),
        el("span", { class: "asrc" }, sourceText(a)), sel),
      a.error && el("div", { class: "v err" }, a.error)));
  }
  info.append(renderFilesRow(pid, n));
  if (r.predecessors.length) {
    const v = el("div", { class: "v" });
    r.predecessors.forEach((p, i) => { if (i) v.append(", "); v.append(planNodeLink(pid, p)); });
    v.append(el("span", { class: "hint" }, " — ordering only: runs after these, receives none of their results"));
    info.append(el("div", {}, el("div", { class: "k" }, "Depends on"), v));
  }
  if (!n.subplan_id && !user) info.append(renderResultsFromRow(pid, n), renderPromptRow(pid, n));  // a user node gets no prompt
  if (n.error) info.append(el("div", {}, el("div", { class: "k" }, "Error"), el("div", { class: "v err" }, n.error)));
  if (n.result) info.append(el("div", {}, el("div", { class: "k" }, "Result"), el("div", { class: "result-box" }, n.result)));
  if (!n.subplan_id && !user) info.append(renderChangesRow(pid, n));
  const actions = el("div", { class: "actions" });
  if (n.subplan_id) actions.append(el("button", { class: "primary", onclick: () => openPlan(n.subplan_id) }, `Open subplan #${n.subplan_id} →`));
  if (n.status === "failed") actions.append(el("button", { onclick: () => act(post(`/api/plans/${pid}/nodes/${n.node_id}/retry`)) }, "↻ Retry"));
  if (n.status === "running" && n.agent_kind === "builtin" && !n.agent_done) actions.append(el("button", { class: "danger", onclick: () => act(post(`/api/plans/${pid}/nodes/${n.node_id}/stop`)) }, "■ Stop agent"));
  if (n.status !== "done" && !user) actions.append(markDone(""));  // a user node's Mark done sits under its instruction above
  let rework = null;
  if (n.status === "done" && !n.subplan_id && !user) {
    actions.append(reviewButton(pid, n), reworkButton(pid, n));
    if (!state.fullOpen) rework = reworkForm(pid, n);  // in full view the form lives in the Review tab
  }
  if (actions.children.length) info.append(actions);
  if (rework) info.append(rework);
  relocateForFull();
}

// Done-by row: switch a not-yet-started node between an agent job and a user job (PUT .../kind).
// The server rejects the change once the node has run, so the row is only drawn while pending/ready.
function renderKindRow(pid, n) {
  const sel = el("select", { class: "agent-sel", title: "who does this step: an agent, or you (the plan stops here until you mark it done)" },
    el("option", { value: "agent" }, "an agent"),
    el("option", { value: "user" }, "you — the plan waits here until you mark it done"));
  sel.value = isUserNode(n) ? "user" : "agent";
  sel.addEventListener("change", async () => {
    sel.disabled = true;
    try { await put(`/api/plans/${pid}/nodes/${n.node_id}/kind`, { kind: sel.value }); await refreshPlan(); }
    catch (e) { alert(e.message); sel.value = isUserNode(n) ? "user" : "agent"; sel.disabled = false; }
  });
  return el("div", {}, el("div", { class: "k" }, "Done by"), el("div", { class: "v agent-row" }, sel));
}

function reviewButton(pid, n) {
  const reviewing = reviewActivity.has(reviewKey(pid, n.node_id));
  return el("button", {
    disabled: reviewing,
    title: "ask a reviewer agent to check this node's diff against its task, now (uses the plan's review agent unless none is set)",
    onclick: (e) => { e.target.disabled = true; e.target.textContent = "Reviewing…"; triggerNodeReview(pid, n.node_id); },
  }, reviewing ? "Reviewing…" : n.review ? "↻ Re-review step" : "Review this step");
}

// Rework: re-run a finished step's agent with the review findings (or the user's own
// instructions) in its prompt. The form is pre-filled with the findings but editable, since the
// reviewer's wording is often broader than what the user wants fixed. Drafts live in reworkDrafts
// (present = form open) so they survive the re-renders that SSE events trigger.
const reworkDrafts = new Map();  // reviewKey -> instructions being edited
function reworkButton(pid, n) {
  const key = reviewKey(pid, n.node_id);
  return el("button", {
    title: "re-run this step's agent to fix what the review raised (or what you write); the steps after it are not re-run",
    onclick: () => {
      if (!reworkDrafts.has(key)) reworkDrafts.set(key, n.review?.findings || "");
      if (state.fullOpen) setFullTab("review");
      renderNodeInfo(state.nodeInfoResp);
    },
  }, "⟲ Rework this step");
}
function reworkForm(pid, n) {
  const key = reviewKey(pid, n.node_id);
  if (!reworkDrafts.has(key)) return null;
  const ta = el("textarea", { rows: 6, placeholder: "what to fix -- leave empty to use the review findings" });
  ta.value = reworkDrafts.get(key);
  ta.addEventListener("input", () => reworkDrafts.set(key, ta.value));
  // every dependant that already started: a running or failed one was built on the output this
  // rework replaces just as much as a finished one, and REST sends exactly that set
  const ran = n.started_dependants || [];
  const stillGoing = ran.filter((d) => d.status === "running").length;
  return el("div", { class: "rework-form" },
    el("div", { class: "k" }, "Rework instructions"),
    ta,
    ran.length ? el("div", { class: "rework-warn" },
      `${ran.length} later step${ran.length > 1 ? "s" : ""} (${ran.map((d) => `${d.title} — ${d.status}`).join(", ")}) `
      + `already ran on the current output and will not be re-run.`
      + (stillGoing ? ` ${stillGoing === 1 ? "One is" : `${stillGoing} are`} still running right now.` : "")) : null,
    el("div", { class: "rework-actions" },
      el("button", { class: "primary", onclick: (e) => { e.target.disabled = true; reworkNode(pid, n.node_id, ta.value.trim()); } }, "Re-run with these instructions"),
      el("button", { onclick: () => { reworkDrafts.delete(key); renderNodeInfo(state.nodeInfoResp); } }, "Cancel")));
}
async function reworkNode(pid, nid, instructions) {
  try { await post(`/api/plans/${pid}/nodes/${nid}/rework`, { instructions: instructions || null }); }
  catch (e) { alert(e.message); renderNodeInfo(state.nodeInfoResp); return; }
  reworkDrafts.delete(reviewKey(pid, nid));
  await refreshNodeInfo();
  refreshPlan(); loadPlans();
}

function nodeLink(ref) {
  return el("a", { class: "nref", onclick: () => selectNode(ref.plan_id, ref.node_id) },
    `${ref.plan_id}.${ref.node_id} “${truncate(ref.title, 28)}”`);
}

// Files row: what this node may create/modify, which of those are shared with another node (so
// they are serialized rather than clobbering each other), and an editor for the set. A node that
// has started (not pending/ready) locks the editor, since the server rejects the change anyway.
function renderFilesRow(pid, n) {
  const wrap = el("div", {}, el("div", { class: "k" }, "Files"));
  const v = el("div", { class: "v files-row" });
  if (n.blocked_by) {
    const bb = n.blocked_by;
    v.append(el("div", { class: "files-waiting" },
      "waiting for ", nodeLink(bb), ` to release ${bb.files.join(", ")}`));
  }
  const sharedBy = new Map();  // file path -> [conflicting node refs]
  for (const c of n.conflicts || []) for (const f of c.files) {
    if (!sharedBy.has(f)) sharedBy.set(f, []);
    sharedBy.get(f).push(c);
  }
  if (!n.files.length) {
    v.append(el("div", { class: "files-empty" }, "no files declared — may run in parallel with any node"));
  } else {
    for (const f of n.files) {
      const shared = sharedBy.get(f);
      const row = el("div", { class: `file-item${shared ? " shared" : ""}` }, el("span", { class: "fpath" }, f));
      if (shared) {
        const fs = el("span", { class: "fshared" }, "shared with ");
        shared.forEach((c, i) => { if (i) fs.append(", "); fs.append(nodeLink(c)); });
        row.append(fs);
      }
      v.append(row);
    }
  }
  const editable = n.status === "pending" || n.status === "ready";
  const ta = el("textarea", {
    class: "files-edit", rows: 3, placeholder: "one path per line, or comma-separated",
    disabled: !editable,
    title: editable ? "" : "locked: the node has started, so its files are fixed for this run",
  }, n.files.join("\n"));
  const err = el("div", { class: "fld-err" });
  const save = el("button", { disabled: !editable }, "Save files");
  save.addEventListener("click", async () => {
    const files = ta.value.split(/[,\n]/).map((s) => s.trim()).filter(Boolean);
    save.disabled = true; ta.disabled = true; err.textContent = "";
    try { await put(`/api/plans/${pid}/nodes/${n.node_id}/files`, { files }); await refreshPlan(); }
    catch (e) { err.textContent = e.message; save.disabled = false; ta.disabled = false; }
  });
  wrap.append(v, el("div", { class: "files-edit-row" }, ta, save), err);
  return wrap;
}

function planNodeLink(pid, id) {
  const x = state.data?.nodes.find((m) => m.node_id === id);
  return nodeLink({ plan_id: pid, node_id: id, title: x ? x.title : "?" });
}
// Every node this one transitively depends on, by id: the only nodes results_from may name.
function ancestorsOf(nodeId) {
  const seen = new Set(), stack = [nodeId];
  while (stack.length) {
    const b = stack.pop();
    for (const [x, y] of state.data?.edges || []) if (y === b && !seen.has(x)) { seen.add(x); stack.push(x); }
  }
  return [...seen].sort((a, b) => a - b);
}

// Inherits-results row: which earlier nodes' result text goes into this node's prompt. Empty by
// default; edges alone pass nothing. Editable while pending/ready, as checkboxes over the node's
// (transitive) dependencies since those are the only valid choices.
function renderResultsFromRow(pid, n) {
  const wanted = n.results_from || [];
  const v = el("div", { class: "v files-row" });
  if (!wanted.length) v.append(el("div", { class: "files-empty" }, "none; starts with only its own task"));
  else {
    const line = el("div", {});
    wanted.forEach((id, i) => { if (i) line.append(", "); line.append(planNodeLink(pid, id)); });
    v.append(line);
  }
  const wrap = el("div", {}, el("div", { class: "k" }, "Inherits results from"), v);
  const editable = n.status === "pending" || n.status === "ready", cands = ancestorsOf(n.node_id);
  if (!editable || !cands.length) return wrap;
  const boxes = cands.map((id) => {
    const x = state.data.nodes.find((m) => m.node_id === id);
    const cb = el("input", { type: "checkbox", value: id });
    cb.checked = wanted.includes(id);
    return el("label", { class: "rf-opt" }, cb, ` [${id}] ${x ? truncate(x.title, 40) : ""}`);
  });
  const err = el("div", { class: "fld-err" });
  const save = el("button", {}, "Save");
  save.addEventListener("click", async () => {
    const node_ids = boxes.map((b) => b.firstChild).filter((c) => c.checked).map((c) => +c.value);
    save.disabled = true; err.textContent = "";
    try { await put(`/api/plans/${pid}/nodes/${n.node_id}/results-from`, { node_ids }); await refreshPlan(); }
    catch (e) { err.textContent = e.message; save.disabled = false; }
  });
  wrap.append(el("div", { class: "rf-edit" }, ...boxes, save), err);
  return wrap;
}

// Prompt row: the exact context string the engine hands this node's agent (build_context),
// built from the plan as it is now. Collapsed and fetched on open, since it can be long.
function renderPromptRow(pid, n) {
  const pre = el("pre", { class: "prompt-pre" }, "loading…");
  const d = el("details", { class: "prompt-block" }, el("summary", {}, "Show the prompt this node gets"), pre);
  d.addEventListener("toggle", () => {
    if (!d.open) return;
    api(`/api/plans/${pid}/nodes/${n.node_id}/context`).then((r) => {
      pre.textContent = r.context;
      d.querySelector("summary").textContent = `Show the prompt this node gets (${r.context.length.toLocaleString()} chars)`;
    }).catch(() => { pre.textContent = "could not load the prompt"; });
  });
  return el("div", {}, el("div", { class: "k" }, "Prompt"), el("div", { class: "v" }, d));
}

// Changes row: the file diffs captured from this node's Write/Edit/MultiEdit/NotebookEdit tool
// calls (engine._snapshot_file_tool / _record_diffs). Fetched lazily since most nodes never need
// it open; a node that is still running simply has none recorded yet.
function diffHtml(diff) {
  return diff.split("\n").map((line) => {
    const cls = line.startsWith("@@") ? "hunk"
      : (line.startsWith("+") && !line.startsWith("+++")) ? "add"
      : (line.startsWith("-") && !line.startsWith("---")) ? "del" : "";
    return `<span class="dl${cls ? " " + cls : ""}">${esc(line)}</span>`;
  }).join("\n");
}
function renderChangesRow(pid, n) {
  const v = el("div", { class: "v diffs-row" }, el("div", { class: "files-empty" }, "loading…"));
  const wrap = el("div", { id: "changes-row" }, el("div", { class: "k" }, "Changes"), v);
  api(`/api/plans/${pid}/nodes/${n.node_id}/diffs`).then((r) => {
    v.innerHTML = "";
    if (!r.diffs.length) { v.append(el("div", { class: "files-empty" }, "no file changes captured")); return; }
    for (const d of r.diffs) {
      v.append(el("details", { class: "diff-file" },
        el("summary", {}, d.path),
        el("pre", { class: "diff-pre", html: diffHtml(d.diff) })));
    }
  }).catch(() => { v.innerHTML = ""; v.append(el("div", { class: "files-empty" }, "could not load diffs")); });
  return wrap;
}

// Manual completion: the escape hatch for a node the service cannot finish itself
// (a hung agent, work done by hand, a step that turned out unnecessary).
function completeNode(pid, n) {
  const had = (n.result || "").trim();
  const result = prompt(
    `Mark "${n.title}" done.\n\nResult handed to the nodes that depend on it` +
    (had ? "" : isUserNode(n) ? " (e.g. the MR link; optional)" : " (leave empty to use the agent's own closing summary, if it wrote one)") + ":",
    had);
  if (result === null) return;  // cancelled
  act(post(`/api/plans/${pid}/nodes/${n.node_id}/complete`, { result: result.trim() || null }));
}

// ---------------------------------------------------------- transcript
const toolCards = new Map();
function toolArg(name, input) {
  if (!input || typeof input !== "object") return "";
  return input.command || input.file_path || input.path || input.pattern || input.url || input.query || input.description || input.title || JSON.stringify(input);
}
function appendEvent(ev) {
  if (ev.id <= state.lastEventId) return;
  state.lastEventId = ev.id;
  const t = $("#transcript");
  t.querySelector(".none")?.remove();
  const d = ev.data || {};
  const wrap = (node) => (d.parent_tool_use_id ? el("div", { class: "nested" }, node) : node);
  let node = null;
  switch (ev.kind) {
    case "status": node = el("div", { class: "ev-status" }, `${d.from} → ${d.to} · ${fmtTime(ev.ts)}`); break;
    case "system":
      // the engine opens every built-in session with "agent: <profile> (<harness> / <model>)" + data.agent
      if (d.agent && d.agent.name) {
        node = el("div", { class: `ev-agent ${hcls(d.agent.harness)}`, title: d.text || "" },
          el("span", { class: "lbl" }, "agent"), el("span", { class: "aname" }, d.agent.name),
          el("span", { class: "am" }, agentModel(d.agent)), el("span", { class: "spacer" }), el("span", { class: "ts" }, fmtTime(ev.ts)));
        break;
      }
      node = el("div", { class: "ev-system" }, `⏺ ${d.text || ""}${d.model ? ` (${d.model})` : ""}`); break;
    case "text": node = el("div", { class: "ev-text", html: md(d.text) }); break;
    case "thinking": node = el("div", { class: "ev-thinking" }, d.text); break;
    case "log": node = el("div", { class: "ev-log" }, d.text); break;
    case "error": node = el("div", { class: "ev-error" }, d.text); break;
    case "tool_use": {
      const stateEl = el("span", { class: "tstate wait" }, "running…");
      const det = el("details", { class: "ev-tool" },
        el("summary", {}, el("span", { class: "tname" }, d.name || "tool"), el("span", { class: "targ" }, truncate(toolArg(d.name, d.input), 140)), stateEl),
        el("pre", {}, typeof d.input === "string" ? d.input : JSON.stringify(d.input, null, 2)));
      if (d.id) toolCards.set(d.id, { det, stateEl });
      if (!d.input && d.text) { det.querySelector(".tname").textContent = "tool"; det.querySelector(".targ").textContent = truncate(d.text, 140); }
      node = det; break;
    }
    case "tool_result": {
      const card = toolCards.get(d.tool_use_id);
      const pre = el("pre", { class: d.is_error ? "err" : "" }, d.content ?? d.text ?? "");
      if (card) {
        card.stateEl.className = `tstate ${d.is_error ? "err" : "ok"}`;
        card.stateEl.textContent = d.is_error ? "error" : "done";
        card.det.append(pre);
        scrollTranscript(); return;
      }
      node = el("details", { class: "ev-tool" }, el("summary", {}, el("span", { class: "tname" }, "result")), pre); break;
    }
    case "result": {
      const bad = d.is_error;
      const meta = [d.num_turns && `${d.num_turns} turns`, d.duration_ms && fmtDur(d.duration_ms / 1000), d.cost_usd && `$${d.cost_usd.toFixed(3)}`].filter(Boolean).join(" · ");
      node = el("div", { class: `ev-result${bad ? " bad" : ""}` },
        el("div", { class: "rh" }, el("span", {}, bad ? "Failed" : "Result"), el("span", {}, meta)),
        el("div", { html: md(d.text || (d.errors || []).join("\n")) }));
      break;
    }
    default: node = el("div", { class: "ev-log" }, `${ev.kind}: ${JSON.stringify(d)}`);
  }
  t.append(wrap(node));
  scrollTranscript();
}
function updateTyping() {
  const t = $("#transcript");
  t.querySelector(".typing")?.remove();
  const n = state.nodeData;
  if (!n) return;
  if (!t.children.length && !n.agent_kind) t.append(el("div", { class: "none" }, n.subplan_id ? "This node is a subplan – open it to see its agents." : isUserNode(n) ? "This step is your job – no agent runs it." : "No agent has worked on this node yet."));
  if (n.status === "running" && n.agent_kind && !n.agent_done) { t.append(el("div", { class: "typing" }, el("i"), el("i"), el("i"))); scrollTranscript(); }
}
function scrollTranscript() {
  if (!$("#follow").checked) return;
  const b = $("#drawer-body"); b.scrollTop = b.scrollHeight;
}

// ---------------------------------------------------------------- agents
// Agent profiles (harness + model + endpoint). The panel is a second right-hand drawer so it sits
// next to the graph like the node drawer does; only one of the two is open at a time.
const PERMISSION_MODES = ["default", "acceptEdits", "plan", "bypassPermissions", "dontAsk", "auto"];
// capability: what the harness lets the agent do, the same for every harness (the registry maps it
// to the SDK's allowed tools or the openai tools preset). null = the role default: full for a worker, run for a reviewer.
const CAPABILITIES = [
  ["read", "read and search only; no edits, no commands"],
  ["run", "read, search and run commands; no edits"],
  ["edit", "read, search and edit files; no commands"],
  ["full", "everything"],
];
// test level: how much a worker agent is told to test; wording identical to the prompt it gets
const TEST_LEVELS = [
  ["none", "do not test; the node says what to verify, or nothing needs verifying"],
  ["light", "a quick sanity check that the change works -- one script or a couple of cases"],
  ["standard", "run the existing tests covering what you touched, and fix what you broke"],
  ["thorough", "run the full suite, and add tests for behaviour you added"],
];
const PARAMS_EXAMPLE = { temperature: 0.2, max_tokens: 4096, max_turns: 50, reasoning_effort: "medium", stream: true, input_price_per_mtok: 0.15, output_price_per_mtok: 0.6 };
const DEFAULT_TIP = "server default: runs every node for which neither the node nor any of its plans picks an agent";

// called by loadAgents() whenever /api/agents was (re-)fetched, e.g. on an SSE {"type":"agents"}
function agentsLoaded() {
  renderAgentList();
  refreshAgentPanel();
}
const agentById = (id) => state.agents.find((a) => a.id === id);
const asObj = (x) => { if (typeof x !== "string") return x || {}; try { return JSON.parse(x || "{}"); } catch { return {}; } };
const plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;

function renderAgentList() {
  const ul = $("#agent-list");
  ul.innerHTML = "";
  if (!state.agents.length) ul.append(el("li", { class: "none" }, "No agents yet"));
  for (const a of state.agents) {
    const open = () => openAgentPanel(a.id);
    ul.append(el("li", {
      class: state.agentSel === a.id ? "sel" : "", tabindex: 0, role: "button",
      title: `${a.name} · ${a.harness}${a.model ? " · " + a.model : ""}${a.default ? "\n" + DEFAULT_TIP : ""}`,
      onclick: open, onkeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); } },
    },
      el("span", { class: "ag-name" }, a.name, a.default ? el("span", { class: "ag-default", title: DEFAULT_TIP }, " ●") : null),
      el("span", { class: `hchip ${hcls(a.harness)}` }, a.harness),
      el("span", { class: "ag-model" }, a.model || "default model")));
  }
}

function openAgentPanel(sel) {
  if (state.sel) closeDrawer(false);
  state.agentSel = sel; state.agentDirty = false;
  $("#agent-drawer").classList.remove("closed");
  renderAgentPanel();
  renderAgentList();
  if (state.data) renderGraph();
  setHash();
}
function closeAgentPanel(update = true) {
  if (state.agentSel == null) return;
  state.agentSel = null; state.agentDirty = false;
  $("#agent-drawer").classList.add("closed");
  renderAgentList();
  if (update) setHash();
}

function agentHead(title, meta) {
  const head = $("#agent-head");
  head.innerHTML = "";
  head.append(el("div", { class: "t" }, el("h2", { id: "agent-title" }, title), el("div", { class: "meta" }, meta)),
    el("button", { class: "close", title: "close (Esc)", "aria-label": "close agent panel", onclick: () => closeAgentPanel() }, "✕"));
}
function agentMeta(a) {
  return [
    el("span", { class: `hchip ${hcls(a.harness)}` }, a.harness),
    el("span", { style: "font-family:var(--mono)" }, `agent #${a.id}`),
    a.default ? el("span", { class: "ag-default", title: DEFAULT_TIP }, "● server default") : null,
    a.harness !== "openai" ? null : el("span", { title: "a literal key is stored, or the api_key_env variable is set on the server" }, a.has_api_key ? "key: set" : "key: none"),
    el("span", {}, a.in_use ? `used by ${plural(a.in_use, "node/plan")}` : "not assigned"),
  ];
}

function renderAgentPanel() {
  const sel = state.agentSel, body = $("#agent-body");
  if (sel == null) return;
  body.innerHTML = "";
  if (sel === "list") return renderAgentOverview(body);
  if (sel === "new") { agentHead("New agent", [el("span", {}, "not saved yet")]); return renderAgentForm(body, null); }
  const a = agentById(sel);
  if (!a) {
    agentHead(`Agent #${sel}`, []);
    body.append(el("div", { class: "ag-missing" }, "This agent does not exist (any more).", el("br"), el("br"),
      el("button", { onclick: () => openAgentPanel("new") }, "+ New agent")));
    return;
  }
  agentHead(a.name, agentMeta(a));
  renderAgentForm(body, a);
}

// after /api/agents was re-fetched: refresh what is shown without throwing away the operator's typing
function refreshAgentPanel() {
  const sel = state.agentSel;
  if (sel == null || sel === "new") return;
  if (sel === "list") return renderAgentPanel();
  const a = agentById(sel), form = $("#agent-form");
  if (!a || !form || form.dataset.agentId !== String(sel)) return renderAgentPanel();
  agentHead(a.name, agentMeta(a));
  if (!state.agentDirty) {
    const test = $("#agent-test"), msg = $("#agent-form .form-msg");
    renderAgentPanel();
    $("#agent-test").replaceWith(test); $("#agent-form .form-msg").replaceWith(msg);
  }
}

function renderAgentOverview(body) {
  agentHead("Agents", [el("span", {}, plural(state.agents.length, "profile"))]);
  const wrap = el("div", { class: "ag-overview" });
  for (const a of state.agents)
    wrap.append(el("button", { class: "ag-card", onclick: () => openAgentPanel(a.id) },
      el("div", { class: "ag-card-h" }, el("b", {}, a.name), a.default ? el("span", { class: "ag-default", title: DEFAULT_TIP }, "●") : null, el("span", { class: `hchip ${hcls(a.harness)}` }, a.harness)),
      el("div", { class: "ag-card-m" }, [a.model || "default model", a.base_url, a.max_parallel ? `max ${a.max_parallel} parallel` : null, a.in_use ? `used by ${a.in_use}` : null].filter(Boolean).join(" · "))));
  if (!state.agents.length) wrap.append(el("div", { class: "ag-missing" }, "No agent profiles yet. Nodes run with the server's CLI harness."));
  wrap.append(el("div", {}, el("button", { class: "primary", onclick: () => openAgentPanel("new") }, "+ New agent")));
  body.append(wrap);
}

function renderAgentForm(body, a) {
  const isNew = !a;
  const v = a || { harness: "openai" };  // new agents default to the OpenAI-compatible harness
  const tools = asObj(v.tools), params = asObj(v.params);
  const fld = (id, label, input, hint, only) => el("div", { class: "fld", "data-only": only || null },
    el("label", { for: id }, label), input, hint ? el("small", {}, hint) : null);
  const inp = (id, value, attrs = {}) => el("input", { id, value: value ?? "", autocomplete: "off", ...attrs });
  const select = (id, opts, value) => {
    const s = el("select", { id }, opts.map(([val, txt]) => el("option", { value: val }, txt)));
    s.value = value ?? opts[0][0];
    return s;
  };

  const harness = select("ag-harness", HARNESSES.map((h) => [h, h]), v.harness);
  const preset = select("ag-preset", [["all", "all – read, write, edit, shell"], ["readonly", "readonly – read_file, list_dir, glob, grep"], ["none", "none – text only"]], tools.preset || "all");
  const mcp = el("input", { type: "checkbox", id: "ag-mcp" }); mcp.checked = tools.mcp ?? true;
  const perm = select("ag-perm", [["", "(server default)"], ...PERMISSION_MODES.map((m) => [m, m])], v.permission_mode || "");
  const cap = select("ag-cap", [["", "default — full as a worker, run as a reviewer"], ...CAPABILITIES.map(([k, t]) => [k, `${k} — ${t}`])], v.capability || "");
  const level = select("ag-level", TEST_LEVELS.map(([k, t]) => [k, `${k} — ${t}`]), v.test_level || "standard");
  const sys = el("textarea", { id: "ag-sys", rows: 3, placeholder: "extra system prompt text for this agent" }); sys.value = v.system_append || "";
  const paramsErr = el("div", { class: "fld-err", id: "ag-params-err", role: "alert" });
  const paramsTa = el("textarea", { id: "ag-params", rows: 7, spellcheck: "false", "aria-describedby": "ag-params-err ag-params-hint",
    onkeydown: (e) => { if (e.key === "Enter") e.stopPropagation(); }, oninput: () => checkParams() });
  paramsTa.value = Object.keys(params).length ? JSON.stringify(params, null, 2) : "{}";
  const checkParams = () => {
    let p = null, err = "";
    try {
      p = JSON.parse(paramsTa.value.trim() || "{}");
      if (!p || typeof p !== "object" || Array.isArray(p)) { err = 'must be a JSON object, e.g. {"temperature": 0.2}'; p = null; }
    } catch (e) { err = `invalid JSON: ${e.message}`; }
    paramsErr.textContent = err; paramsTa.classList.toggle("bad", !!err);
    return p;
  };
  const keyClear = el("input", { type: "checkbox", id: "ag-key-clear" });
  const msg = el("div", { class: "form-msg", role: "status", "aria-live": "polite" });
  const ctx = { checkParams, msg };

  const form = el("form", { id: "agent-form", class: "ag-form", "data-agent-id": a ? a.id : "new", "aria-labelledby": "agent-title", novalidate: true,
    oninput: () => { state.agentDirty = true; }, onchange: () => { state.agentDirty = true; },
    onsubmit: (e) => { e.preventDefault(); saveAgent(a, form, ctx); } },
    fld("ag-name", "Name", inp("ag-name", v.name, { required: true, placeholder: "e.g. local-qwen" })),
    fld("ag-harness", "Harness", harness, "openai = any OpenAI-compatible chat-completions endpoint (OpenAI, Azure, OpenRouter, vLLM, llama.cpp, LM Studio, Ollama …)"),
    fld("ag-model", "Model", inp("ag-model", v.model, { placeholder: "e.g. gpt-4.1-mini, qwen2.5-coder:14b" })),
    fld("ag-base", "Base URL", inp("ag-base", v.base_url, { placeholder: "https://api.openai.com/v1", spellcheck: "false" }), "the part before /chat/completions, e.g. http://localhost:11434/v1", "openai"),
    fld("ag-keyenv", "API key env var", inp("ag-keyenv", v.api_key_env, { placeholder: "e.g. OPENROUTER_API_KEY", spellcheck: "false" }), "preferred: the server reads the key from this environment variable", "openai"),
    el("div", { class: "fld", "data-only": "openai" },
      el("label", { for: "ag-key" }, "API key"),
      el("input", { id: "ag-key", type: "password", autocomplete: "off", spellcheck: "false", placeholder: isNew ? "optional – stored on the server, never shown again" : "unchanged" }),
      el("small", {}, isNew ? "leave empty for local endpoints or when using the env var"
        : (a.has_api_key ? "a key is available; leave empty to keep it" : "no key stored; type one to store it")),
      isNew ? null : el("label", { class: "chk" }, keyClear, " clear the stored key")),
    fld("ag-cap", "What may this agent do", cap, "same meaning on every harness; the default is full for a worker agent and run when it reviews"),
    fld("ag-level", "Test level", level, "how much a worker agent is told to test; a reviewer always runs the suite"),
    fld("ag-maxpar", "Max parallel", inp("ag-maxpar", v.max_parallel, { type: "number", min: 1, step: 1, placeholder: "unlimited" }), "at most this many nodes run on this agent at once (e.g. 1 for a single local GPU)"),
    el("div", { class: "fld", "data-only": "openai" },
      el("label", { class: "chk" }, mcp, " expose clearbox MCP tools (plan building, report_progress)")),
    fld("ag-sys", "System prompt addition", sys),
    el("details", { class: "fld adv", open: Object.keys(params).length || v.permission_mode || (tools.preset && tools.preset !== "all") ? true : null },
      el("summary", {}, "Advanced"),
      fld("ag-perm", "Permission mode (raw)", perm, "escape hatch: overrides the capability's mode for the Claude SDK session; leave on server default unless you know why", "claude-sdk"),
      fld("ag-preset", "Tools preset (raw)", preset, "escape hatch: the capability picks this for you; set it only to override", "openai"),
      el("label", { for: "ag-params" }, "Advanced params (JSON)"), paramsTa, paramsErr,
      el("small", { id: "ag-params-hint" }, "temperature, top_p, max_tokens, max_turns, reasoning_effort, stream, timeout, extra_body, input_price_per_mtok, output_price_per_mtok (USD per 1M tokens) · ",
        el("a", { href: "#", onclick: (e) => { e.preventDefault(); paramsTa.value = JSON.stringify({ ...PARAMS_EXAMPLE, ...(checkParams() || {}) }, null, 2); checkParams(); state.agentDirty = true; } }, "insert example"))),
    msg,
    el("div", { class: "actions" },
      el("button", { type: "submit", class: "primary" }, isNew ? "Create agent" : "Save"),
      el("button", { type: "button", id: "ag-test-btn", title: isNew ? "saves the agent first, then tests it" : "lists the endpoint's models, or sends a tiny completion",
        onclick: () => testAgent(a, form, ctx) }, isNew ? "Save & test connection" : "Test connection"),
      el("div", { class: "spacer" }),
      isNew ? null : el("button", { type: "button", class: "danger", onclick: () => deleteAgent(a.id) }, "Delete")),
    el("div", { id: "agent-test" }));
  form.dataset.mcpInit = tools.mcp == null ? "" : String(tools.mcp);
  const applyHarness = () => form.querySelectorAll("[data-only]").forEach((x) => x.classList.toggle("hidden", !x.dataset.only.split(" ").includes(harness.value)));
  harness.addEventListener("change", applyHarness);
  applyHarness();
  body.append(form);
  if (isNew) setTimeout(() => $("#ag-name")?.focus(), 200);
}

// the JSON body for POST/PATCH; null when the params JSON is invalid (the error is already shown inline)
function agentBody(a, form, checkParams) {
  const val = (id) => form.querySelector(`#${id}`).value.trim();
  const params = checkParams();
  if (!params) { form.querySelector(".adv").open = true; form.querySelector("#ag-params").focus(); return null; }
  const h = val("ag-harness"), mp = val("ag-maxpar");
  if (!val("ag-name")) throw new Error("Name is required.");
  if (mp && !/^[1-9]\d*$/.test(mp)) throw new Error("Max parallel must be a positive whole number (or empty for unlimited).");
  const body = { name: val("ag-name"), harness: h, model: val("ag-model") || null, system_append: form.querySelector("#ag-sys").value.trim() || null, max_parallel: mp ? +mp : null, params,
    capability: val("ag-cap") || null, test_level: val("ag-level") || null };
  if (h === "openai") {
    Object.assign(body, { base_url: val("ag-base") || null, api_key_env: val("ag-keyenv") || null });
    // tools is only meaningful for the sandboxed toolset of non-Claude harnesses; keep allow/deny lists
    const tools = { ...asObj(a?.tools), preset: val("ag-preset") };
    const mcp = form.querySelector("#ag-mcp").checked;
    if (form.dataset.mcpInit !== "" || !mcp) tools.mcp = mcp; else delete tools.mcp;  // untouched + on = follow the server setting
    body.tools = tools;
    const key = form.querySelector("#ag-key").value;  // only ever sent when the operator typed something
    if (key) body.api_key = key;
    else if (form.querySelector("#ag-key-clear")?.checked) body.api_key = "";
  } else if (h === "claude-sdk") body.permission_mode = val("ag-perm") || null;
  if (!a) for (const k of Object.keys(body)) if (body[k] == null) delete body[k];
  return body;
}

async function saveAgent(a, form, { checkParams, msg }) {
  msg.className = "form-msg"; msg.textContent = "";
  let body;
  try { body = agentBody(a, form, checkParams); } catch (e) { msg.className = "form-msg err"; msg.textContent = e.message; return null; }
  if (!body) return null;
  let saved;
  try { saved = a ? await api(`/api/agents/${a.id}`, { method: "PATCH", body: JSON.stringify(body) }) : await post("/api/agents", body); }
  catch (e) { msg.className = "form-msg err"; msg.textContent = e.message; return null; }
  const i = state.agents.findIndex((x) => x.id === saved.id);
  if (i >= 0) state.agents[i] = saved; else state.agents.push(saved);
  state.agentDirty = false;
  if (state.agentSel === (a ? a.id : "new")) {
    state.agentSel = saved.id; setHash();
    renderAgentPanel();
    const m = $("#agent-form .form-msg"); m.className = "form-msg ok"; m.textContent = a ? "Saved." : "Agent created.";
  }
  renderAgentList();
  return saved;
}

async function testAgent(a, form, ctx) {
  let id = a?.id;
  if (!a || state.agentDirty) { const saved = await saveAgent(a, form, ctx); if (!saved) return; id = saved.id; }
  const btn = $("#ag-test-btn"), box = $("#agent-test");
  btn.disabled = true; btn.textContent = "Testing…";
  box.className = "test-box wait"; box.textContent = "Contacting the endpoint…";
  let r;
  try { r = await post(`/api/agents/${id}/test`); } catch (e) { r = { ok: false, detail: e.message }; }
  if (state.agentSel !== id) return;
  const btn2 = $("#ag-test-btn"), out = $("#agent-test");  // the form may have been re-rendered meanwhile
  btn2.disabled = false; btn2.textContent = "Test connection";
  out.className = `test-box ${r.ok ? "ok" : "bad"}`; out.innerHTML = "";
  const kv = [["server", r.base_url], ["credential", r.credential], ["reachable", r.reachable], ["model listed", r.model_listed]].filter(([, x]) => x != null);
  out.append(...[el("div", { class: "th" }, r.ok ? "✓ Connection OK" : "✕ Connection failed"),
    el("div", { class: "td" }, r.detail || ""),
    kv.length ? el("div", { class: "tkv" }, kv.map(([k, x]) => el("span", {}, `${k}: `, el("b", {}, String(x))))) : null,
    r.models_sample?.length ? el("div", { class: "tmodels" }, el("span", {}, "models:"), r.models_sample.map((m) => el("code", {}, m))) : null].filter(Boolean));
}

async function deleteAgent(id) {
  const a = agentById(id); if (!a) return;
  const refs = a.in_use ? `${plural(a.in_use, "node/plan")} reference${a.in_use === 1 ? "s" : ""} this agent; they will go back to inheriting their plan's agent or the server default.`
    : "No nodes or plans reference this agent.";
  if (!confirm(`Delete agent "${a.name}"?\n\n${refs}${a.default ? "\n\nIt is the server default agent." : ""}`)) return;
  try { await api(`/api/agents/${id}`, { method: "DELETE" }); } catch (e) { alert(e.message); return; }
  state.agents = state.agents.filter((x) => x.id !== id);
  openAgentPanel("list");
  loadAgents();
}

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && state.agentSel != null && !e.defaultPrevented) closeAgentPanel();
});
$("#agent-new").addEventListener("click", () => openAgentPanel("new"));
$("#agent-all").addEventListener("click", () => openAgentPanel("list"));

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
