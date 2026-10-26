// The Agents panel: list, create, edit, test and delete agent profiles.

import { $, api, capBadge, debounce, el, HARNESSES, hcls, post, state } from "./core.js";
import { renderGraph } from "./graph.js";
import { closeDrawer } from "./drawer.js";
import { refreshPlan, setHash } from "./app.js";

// ---------------------------------------------------------------- agents
// Agent profiles (harness + model + endpoint). The panel is a second right-hand drawer so it sits
// next to the graph like the node drawer does; only one of the two is open at a time.
const PERMISSION_MODES = ["default", "acceptEdits", "plan", "bypassPermissions", "dontAsk", "auto"];
// capability: what the harness lets the agent do, the same for every harness (the registry maps it
// to the SDK's allowed tools or the openai tools preset). null = the role default: the server's
// --default-capability (full unless set) for a worker, run for a reviewer. The server reports the
// result as effective_capability / capability_source; the badges show that.
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

export async function loadAgents() {
  try { state.agents = await api("/api/agents"); } catch { state.agents = []; }
  agentsLoaded();
}
export const loadAgentsSoon = debounce(async () => { await loadAgents(); refreshPlan(); }, 150);

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
      el("span", { class: "ag-model" }, el("span", {}, a.model || "default model"), capBadge(a.effective_capability, a.capability_source))));
  }
}

export function openAgentPanel(sel) {
  if (state.sel) closeDrawer(false);
  state.agentSel = sel; state.agentDirty = false;
  $("#agent-drawer").classList.remove("closed");
  renderAgentPanel();
  renderAgentList();
  if (state.data) renderGraph();
  setHash();
}
export function closeAgentPanel(update = true) {
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
    capBadge(a.effective_capability, a.capability_source),
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
      el("div", { class: "ag-card-h" }, el("b", {}, a.name), a.default ? el("span", { class: "ag-default", title: DEFAULT_TIP }, "●") : null, capBadge(a.effective_capability, a.capability_source), el("span", { class: `hchip ${hcls(a.harness)}` }, a.harness)),
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
  const cap = select("ag-cap", [["", "default — the server's --default-capability as a worker, run as a reviewer"], ...CAPABILITIES.map(([k, t]) => [k, `${k} — ${t}`])], v.capability || "");
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
    fld("ag-cap", "What may this agent do", cap, "same meaning on every harness; the default is the server's --default-capability (full unless set) for a worker agent and run when it reviews"),
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
