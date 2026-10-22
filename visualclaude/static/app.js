// VisualClaude frontend: plan DAG view + live agent transcripts. No build step, no deps.
"use strict";

const NODE_W = 230, NODE_H = 74, H_GAP = 46, V_GAP = 70;
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
};

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
  for (const kid of kids.flat()) if (kid != null) e.append(kid.nodeType ? kid : document.createTextNode(kid));
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
function md(text) {
  // tiny, safe markdown: code fences, inline code, bold
  const parts = String(text ?? "").split(/```/);
  return parts.map((p, i) => {
    if (i % 2) return `<pre>${esc(p.replace(/^\w*\n/, ""))}</pre>`;
    return esc(p).replace(/`([^`]+)`/g, "<code>$1</code>").replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>");
  }).join("");
}
function truncate(s, n) { s = String(s ?? ""); return s.length > n ? s.slice(0, n - 1) + "…" : s; }

// ---------------------------------------------------------------- routing
function parseHash() {
  const m = location.hash.match(/^#\/plan\/(\d+)(?:\/node\/(\d+))?/);
  return m ? { plan: +m[1], node: m[2] ? +m[2] : null } : null;
}
function setHash() {
  if (state.planId == null) return;
  let h = `#/plan/${state.planId}`;
  if (state.sel && state.sel.plan_id === state.planId) h += `/node/${state.sel.node_id}`;
  if (location.hash !== h) history.replaceState(null, "", h);
}
window.addEventListener("hashchange", () => {
  const r = parseHash();
  if (r && r.plan !== state.planId) openPlan(r.plan, r.node);
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
      bar));
  }
}

// ------------------------------------------------------------- plan view
async function openPlan(planId, nodeId = null) {
  if (planId !== state.planId) { state.sel = null; closeDrawer(false); }
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
  $("#crumbs").innerHTML = ""; $("#controls").innerHTML = ""; $("#plan-status").textContent = ""; $("#active-bar").innerHTML = "";
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
  c.append(el("button", { title: "clear all results and transcripts", onclick: () => confirm("Reset all nodes of this plan (results and transcripts are cleared)?") && act(post(`/api/plans/${rid}/reset`)) }, "Reset"));
  if (!d.ancestors.length)
    c.append(el("button", { class: "danger", onclick: () => confirm(`Delete plan #${rid} and all its subplans?`) && act(api(`/api/plans/${rid}`, { method: "DELETE" }).then(() => { state.planId = null; location.hash = ""; renderEmpty(""); loadPlans().then(autoOpen); })) }, "Delete"));

  const ab = $("#active-bar");
  ab.innerHTML = "";
  if (d.active.length) {
    ab.append(el("span", { class: "lbl" }, "Working now:"));
    for (const a of d.active)
      ab.append(el("span", { class: "chip", title: `${a.agent_kind} agent ${a.agent_name || ""}`, onclick: () => openPlan(a.plan_id, a.node_id) },
        el("i", { class: "dot running" }), a.title, el("span", { class: "ref" }, `${a.plan_id}.${a.node_id}`)));
  }
}

async function act(p) {
  try { await p; } catch (e) { alert(e.message); }
  refreshPlan(); loadPlans();
}

// ---------------------------------------------------------------- layout
function computeLayout(nodes, edges) {
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
  // 4. x positions: each layer centred, widths respected
  const gap = (a, b) => (typeof a === "number" && typeof b === "number" ? H_GAP : 18);
  const layerW = layers.map((L) => L.reduce((s, v, i) => s + width.get(v) + (i ? gap(L[i - 1], v) : 0), 0));
  const maxW = Math.max(NODE_W, ...layerW);
  const coords = new Map();
  layers.forEach((L, li) => {
    let x = (maxW - layerW[li]) / 2;
    L.forEach((v, i) => {
      if (i) x += gap(L[i - 1], v);
      coords.set(v, { x: x + (width.get(v) - NODE_W) / 2, cx: x + width.get(v) / 2, y: li * (NODE_H + V_GAP) });
      x += width.get(v);
    });
  });
  return { coords, chains, width: maxW, height: layers.length * (NODE_H + V_GAP) - V_GAP };
}

function edgePath(chain, coords) {
  // vertical-tangent cubic segments through each layer; virtual nodes are passed straight through
  let d = "";
  for (let i = 0; i + 1 < chain.length; i++) {
    const a = coords.get(chain[i]), b = coords.get(chain[i + 1]);
    const x1 = a.cx, y1 = typeof chain[i] === "number" ? a.y + NODE_H : a.y + NODE_H;
    const x2 = b.cx, y2 = typeof chain[i + 1] === "number" ? b.y - 3 : b.y;
    const dy = (y2 - y1) / 2;
    if (!d) d = `M${x1},${typeof chain[i] === "number" ? y1 : a.y}` + (typeof chain[i] === "number" ? "" : ` L${x1},${y1}`);
    d += ` C${x1},${y1 + dy} ${x2},${y2 - dy} ${x2},${y2}`;
    if (typeof chain[i + 1] !== "number") d += ` L${x2},${b.y + NODE_H}`;
  }
  return d;
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
  const L = computeLayout(d.nodes, d.edges);
  state.layout = L;
  const byId = new Map(d.nodes.map((n) => [n.node_id, n]));

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
    const g = el("svg:g", {
      class: `node ${n.status}${isSel ? " sel" : ""}`, transform: `translate(${p.x},${p.y})`,
      onclick: (e) => { e.stopPropagation(); selectNode(d.plan.id, n.node_id); },
      ondblclick: (e) => { e.stopPropagation(); if (n.subplan_id) openPlan(n.subplan_id); },
    });
    g.append(el("svg:title", {}, `${n.title}\n\n${n.description || ""}${n.subplan_id ? "\n\n(double-click to open subplan)" : ""}`));
    if (n.subplan_id) {
      g.append(el("svg:rect", { class: "ghost", x: 8, y: -8, width: NODE_W, height: NODE_H, rx: 10 }));
      g.append(el("svg:rect", { class: "ghost", x: 4, y: -4, width: NODE_W, height: NODE_H, rx: 10 }));
    }
    g.append(el("svg:rect", { class: "halo", x: 0, y: 0, width: NODE_W, height: NODE_H, rx: 10 }));
    g.append(el("svg:rect", { class: "card", x: 0, y: 0, width: NODE_W, height: NODE_H, rx: 10 }));
    g.append(el("svg:rect", { class: "stripe", x: 0, y: 10, width: 4, height: NODE_H - 20, rx: 2 }));
    g.append(el("svg:text", { class: "title", x: 16, y: 27 }, truncate(n.title, 25)));

    let sub;
    if (n.subplan_id) {
      const pr = n.progress;
      sub = `subplan #${n.subplan_id} · ${pr.done}/${pr.total} done${pr.running ? ` · ${pr.running} running` : ""}`;
      const w = NODE_W - 32, tot = Math.max(pr.total, 1);
      g.append(el("svg:rect", { class: "pbar-bg", x: 16, y: NODE_H - 14, width: w, height: 4, rx: 2 }));
      g.append(el("svg:rect", { class: "pbar", x: 16, y: NODE_H - 14, width: (w * pr.done) / tot, height: 4, rx: 2 }));
      if (pr.running) g.append(el("svg:rect", { class: "pbar run", x: 16 + (w * pr.done) / tot, y: NODE_H - 14, width: (w * pr.running) / tot, height: 4, rx: 2 }));
    } else if (n.status === "running") {
      const dur = n.started_at ? ` · ${fmtDur(Date.now() / 1000 - n.started_at)}` : "";
      sub = `${n.agent_kind === "external" ? (n.agent_name || "external") : "claude"} working${dur}`;
    } else if (n.status === "done" && n.started_at && n.finished_at) {
      sub = `done in ${fmtDur(n.finished_at - n.started_at)}`;
    } else if (n.status === "failed") {
      sub = truncate(n.error || "failed", 32);
    } else {
      sub = n.status === "ready" ? "ready – waiting for agent" : truncate(n.description || "pending", 32);
    }
    g.append(el("svg:text", {
      class: "sub", x: 16, y: n.subplan_id ? 47 : 50,
      "data-started": !n.subplan_id && n.status === "running" && n.started_at ? n.started_at : null,
      "data-who": n.agent_kind === "external" ? (n.agent_name || "external") : "claude",
    }, truncate(sub, 34)));
    g.append(el("svg:text", { class: "ref", x: NODE_W - 12, y: 20, "text-anchor": "end" }, `${d.plan.id}.${n.node_id}`));

    // status glyph
    const gx = NODE_W - 22, gy = NODE_H - (n.subplan_id ? 34 : 24);
    if (n.status === "running") g.append(el("svg:circle", { class: "spinner", cx: gx, cy: gy, r: 7 }));
    else if (n.status === "done") g.append(el("svg:path", { class: "check", d: `M${gx - 6},${gy} l4,4 l8,-8` }));
    else if (n.status === "failed") g.append(el("svg:path", { class: "cross", d: `M${gx - 5},${gy - 5} l10,10 M${gx + 5},${gy - 5} l-10,10` }));
    gN.append(g);
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
  $("#drawer").classList.add("closed");
  state.sel = null; state.nodeData = null;
  if (update && state.data) { renderGraph(); setHash(); }
}

async function refreshNodeInfo(withEvents = false) {
  const s = state.sel; if (!s) return;
  let r;
  try { r = await api(`/api/plans/${s.plan_id}/nodes/${s.node_id}?after=${withEvents ? state.lastEventId : 1e18}`); }
  catch { closeDrawer(); return; }
  if (!state.sel || state.sel.plan_id !== s.plan_id || state.sel.node_id !== s.node_id) return;
  state.nodeData = r.node;
  renderNodeInfo(r);
  if (withEvents) r.events.forEach(appendEvent);
  updateTyping();
}

function renderNodeInfo(r) {
  const n = r.node, pid = state.sel.plan_id;
  const head = $("#drawer-head");
  head.innerHTML = "";
  const agent = n.agent_kind ? (n.agent_kind === "external" ? `external · ${n.agent_name || "?"}` : "built-in claude") : null;
  head.append(
    el("div", { class: "t" },
      el("h2", {}, n.title),
      el("div", { class: "meta" },
        el("span", { class: `pill ${n.status}` }, n.status),
        el("span", { style: "font-family:var(--mono)" }, `plan ${pid} · node ${n.node_id}`),
        agent && el("span", {}, agent),
        n.session_id && el("span", { title: "agent session id", style: "font-family:var(--mono)" }, truncate(n.session_id, 14)))),
    el("button", { class: "close", onclick: () => closeDrawer() }, "✕"));

  const info = $("#node-info");
  info.innerHTML = "";
  info.append(el("div", {}, el("div", { class: "k" }, "Task"), el("div", { class: "v" }, n.description || "—")));
  if (r.predecessors.length) {
    const titles = r.predecessors.map((p) => { const x = state.data.nodes.find((m) => m.node_id === p); return x ? `[${p}] ${x.title}` : `[${p}]`; });
    info.append(el("div", {}, el("div", { class: "k" }, "Depends on"), el("div", { class: "v" }, titles.join(", "))));
  }
  if (n.error) info.append(el("div", {}, el("div", { class: "k" }, "Error"), el("div", { class: "v err" }, n.error)));
  if (n.result) info.append(el("div", {}, el("div", { class: "k" }, "Result"), el("div", { class: "result-box" }, n.result)));
  const actions = el("div", { class: "actions" });
  if (n.subplan_id) actions.append(el("button", { class: "primary", onclick: () => openPlan(n.subplan_id) }, `Open subplan #${n.subplan_id} →`));
  if (n.status === "failed") actions.append(el("button", { onclick: () => act(post(`/api/plans/${pid}/nodes/${n.node_id}/retry`)) }, "↻ Retry"));
  if (n.status === "running" && n.agent_kind === "builtin" && !n.agent_done) actions.append(el("button", { class: "danger", onclick: () => act(post(`/api/plans/${pid}/nodes/${n.node_id}/stop`)) }, "■ Stop agent"));
  if (actions.children.length) info.append(actions);
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
    case "system": node = el("div", { class: "ev-system" }, `⏺ ${d.text || ""}${d.model ? ` (${d.model})` : ""}`); break;
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
  if (!t.children.length && !n.agent_kind) t.append(el("div", { class: "none" }, n.subplan_id ? "This node is a subplan – open it to see its agents." : "No agent has worked on this node yet."));
  if (n.status === "running" && n.agent_kind && !n.agent_done) { t.append(el("div", { class: "typing" }, el("i"), el("i"), el("i"))); scrollTranscript(); }
}
function scrollTranscript() {
  if (!$("#follow").checked) return;
  const b = $("#drawer-body"); b.scrollTop = b.scrollHeight;
}

// ----------------------------------------------------------- live feed
function connect() {
  const es = new EventSource("/api/stream");
  es.addEventListener("hello", () => { $("#conn").className = "conn on"; $("#conn-text").textContent = "live"; refreshPlan(); loadPlans(); });
  es.onmessage = (m) => {
    const msg = JSON.parse(m.data);
    if (msg.type === "plan") {
      loadPlansSoon();
      if (state.data && msg.root_id === state.data.root.id) refreshPlanSoon();
      else if (msg.deleted && state.planId === msg.plan_id) { state.planId = null; renderEmpty("Plan deleted."); }
    } else if (msg.type === "event") {
      const ev = msg.event;
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
  const r = parseHash();
  if (r) return openPlan(r.plan, r.node);
  if (state.plans.length) return openPlan(state.plans[0].id);
  renderEmpty(`No plans yet.<br><br>Connect a model to the MCP endpoint <code>${location.origin}/mcp</code><br>and ask it to <code>create_plan</code>, <code>add_node</code> and <code>add_edge</code>.`);
}
(async () => {
  await loadPlans();
  await autoOpen();
  connect();
})();
