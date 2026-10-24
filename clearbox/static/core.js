// Shared by every module: constants, the one state object, DOM/fetch/format helpers and the
// agent helpers the graph, drawer and panels all use.

export const NODE_W = 230, NODE_H = 88, H_GAP = 46, V_GAP = 66;
export const CONFLICT_DY = 26;            // file stagger: each later node of a shares-a-file group sits this much lower
export const CHIP_Y = 60, CHIP_H = 17;   // agent chip row inside a node box (between the subtitle and the bottom edge)
export const $ = (s) => document.querySelector(s);
const SVGNS = "http://www.w3.org/2000/svg";

export const state = {
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
  testsEdit: false,    // the topbar test-instructions pill is open as an editable textarea
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
export const reviewActivity = new Set();
export const reviewKey = (planId, nodeId) => `${planId}:${nodeId}`;
const VERDICT_INFO = {
  ok: { glyph: "✓", label: "ok" },
  concerns: { glyph: "!", label: "concerns" },
  error: { glyph: "?", label: "couldn't review" },
};
export function verdictInfo(v) { return VERDICT_INFO[v] || { glyph: "?", label: v }; }

// ------------------------------------------------------------------ utils
export const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
export function el(tag, attrs = {}, ...kids) {
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
export async function api(path, opts = {}) {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  if (!r.ok) { const t = await r.text(); let m = t; try { m = JSON.parse(t).detail; } catch {} throw new Error(m); }
  return r.json();
}
export const post = (p, body) => api(p, { method: "POST", body: JSON.stringify(body || {}) });
export function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }
export function fmtTime(ts) { return new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }); }
export function fmtDur(s) { if (s == null) return ""; s = Math.max(0, Math.round(s)); return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${s % 60}s`; }
function fmtTokens(n) { return n < 1000 ? `${n}` : `${(n / 1000).toFixed(n < 10000 ? 1 : 0)}k`; }

// "↓12.3k ↑4.5k" in the node box: input and output tokens as separate live counters.
// Tagged with the node id so a {"type":"tokens"} message can retarget them without a redraw.
export function tokenCounters(tin, tout, nodeId) {
  const g = el("svg:g", { class: "tok", "data-tok": nodeId });
  const t = el("svg:text", { x: NODE_W - 12, y: 50, "text-anchor": "end" },
    el("svg:tspan", { class: "tok-in" }, `↓${fmtTokens(tin)}`),
    el("svg:tspan", { class: "tok-out", dx: 7 }, `↑${fmtTokens(tout)}`));
  t.append(el("svg:title", {}, `${tin.toLocaleString()} tokens in · ${tout.toLocaleString()} out`));
  g.append(t);
  return g;
}

export function updateTokenCounters(nodeId, tin, tout) {
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
export function md(text) {
  // tiny, safe markdown: code fences, headings, lists, inline code, bold
  const parts = String(text ?? "").split(/```/);
  return parts.map((p, i) => (i % 2) ? `<pre>${esc(p.replace(/^\w*\n/, ""))}</pre>` : mdBlock(p)).join("");
}
export function truncate(s, n) { s = String(s ?? ""); return s.length > n ? s.slice(0, n - 1) + "…" : s; }
export function lastSeg(path) {
  const parts = String(path ?? "").split(/[\\/]/).filter(Boolean);
  return parts.length ? parts[parts.length - 1] : String(path ?? "");
}
export const put = (p, body) => api(p, { method: "PUT", body: JSON.stringify(body || {}) });

// ---------------------------------------------------------------- agents
// Nodes carry their resolved agent ({name, harness, model, profile_id, source, from_plan});
// these helpers turn that into chips, labels and selects. Colours come from --h-<harness>.
export const HARNESSES = ["claude-sdk", "openai", "mock"];
export const hcls = (harness) => `h-${HARNESSES.includes(harness) ? harness : "other"}`;
export const agentKey = (a) => (a ? (a.profile_id != null ? `p${a.profile_id}` : `n${a.name}`) : "");
export const agentModel = (a) => (a.harness === "mock" ? "mock" : `${a.harness} / ${a.model || "default model"}`);
export function harnessOfName(name) {
  // active_nodes only carries agent_name; map it back to a harness via the profile list
  const p = state.agents.find((x) => x.name === name);
  if (p) return p.harness;
  return name === "mock" ? "mock" : name === "claude" ? "claude-sdk" : null;
}
export function sourceText(a) {
  if (a.source === "node") return "set on this node";
  if (a.source === "plan") return "plan default";
  if (a.source === "inherited") return a.from_plan != null ? `inherited from plan ${a.from_plan}` : "inherited";
  return "server default";
}
// User-job nodes: a step a person does (push a branch, open an MR, approve something). No agent
// runs one; the plan parks on it until the user marks it done. kind is "agent" or "user".
export const isUserNode = (n) => n.kind === "user";
export const isWaiting = (n) => isUserNode(n) && (n.waiting_for_user || n.status === "ready");
export function planDefaultAgent(d) {
  // the agent a node of this plan gets when it has no override of its own
  if (!d) return null;
  if (d.default_agent) return { ...d.default_agent, profile_id: d.default_agent.id };
  return d.nodes.map((n) => n.agent).find((a) => a && a.source !== "node") || null;
}

export function agentSelect(current, inheritLabel, onchange, attrs = {}) {
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
