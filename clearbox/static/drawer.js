// The node drawer and its full-screen view: node info rows, diffs, review, rework and the live
// transcript.

import {
  $, agentModel, agentSelect, api, capBadge, el, fmtDur, fmtTime, hcls, isUserNode, isWaiting, md,
  NODE_H, planDefaultAgent, post, put, reviewActivity, reviewKey, sourceText, state,
  truncate,
} from "./core.js";
import { applyView, renderGraph, svg } from "./graph.js";
import { closeAgentPanel } from "./agents.js";
import { diffView } from "./diffview.js";
import {
  act, loadPlans, openPlan, refreshPlan, renderReviewPane, setHash, triggerNodeReview,
} from "./app.js";

// --------------------------------------------------------------- drawer
export async function selectNode(planId, nodeId) {
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
  const x1 = v.x + p.x * v.k, y1 = v.y + p.y * v.k, x2 = x1 + p.w * v.k, y2 = y1 + NODE_H * v.k;
  if (x1 < m || y1 < m || x2 > r.width - m || y2 > r.height - m) {
    v.x = r.width / 2 - (p.x + p.w / 2) * v.k; v.y = r.height / 2 - (p.y + NODE_H / 2) * v.k; applyView();
  }
}
export function closeDrawer(update = true) {
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
    diffView(d.diff, { split: true }));
  pane.scrollTop = 0;
}
// The files a run changed outside its declared ones (diffs.py, names only: they are not diffed),
// shown above the diffs in the drawer and the full view; null when there are none.
function undeclaredBlock(n) {
  const files = n?.undeclared_files || [];
  if (!files.length) return null;
  const total = files.length + (n.undeclared_more || 0);
  return el("div", { class: "undeclared" },
    el("div", { class: "uw-head" }, `Changed outside the declared files (${total})`),
    el("div", { class: "uw-note" }, "Names only: these changes are not diffed and the reviewer does not see them."
      + (n.undeclared_shared ? " May include other steps' changes: another step of this plan ran at the same time." : "")),
    el("ul", { class: "uw-list" }, ...files.map((f) => el("li", { title: f }, f)),
      ...(n.undeclared_more ? [el("li", { class: "uw-more" }, `… and ${n.undeclared_more} more`)] : [])));
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
  const warn = state.nodeData?.node_id === nodeId ? undeclaredBlock(state.nodeData) : null;
  if (warn) list.append(warn);
  if (!r.diffs.length) {
    list.append(el("div", { class: "files-empty" }, "no file changes captured"));
    pane.append(el("div", { class: "files-empty pad" },
      "This node recorded no file diffs. Only the files a node declares are diffed"
      + (warn ? "; what it changed outside them is listed on the left by name." : ".")));
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
export function renderFullReview() {
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
export function openFullView(push = true) {
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
export function closeFullView(push = true) {
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

export async function refreshNodeInfo(withEvents = false) {
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
        capBadge(a.capability, a.capability_source), el("span", { class: "asrc" }, sourceText(a)), sel),
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

// Changes row: git diffs of this node's declared files between the start and the end of its run
// (diffs.py, stored by engine._record_diffs), under the names of anything it changed outside
// them, one column wide here; the full view shows them side
// by side. Fetched lazily since most nodes never need it open; a node that is still running
// simply has none recorded yet.
function renderChangesRow(pid, n) {
  const v = el("div", { class: "v diffs-row" }, el("div", { class: "files-empty" }, "loading…"));
  const wrap = el("div", { id: "changes-row" }, el("div", { class: "k" }, "Changes"), v);
  api(`/api/plans/${pid}/nodes/${n.node_id}/diffs`).then((r) => {
    v.innerHTML = "";
    const warn = undeclaredBlock(n);
    if (warn) v.append(warn);
    if (!r.diffs.length) { v.append(el("div", { class: "files-empty" }, "no file changes captured")); return; }
    for (const d of r.diffs) {
      v.append(el("details", { class: "diff-file" },
        el("summary", {}, d.path),
        diffView(d.diff)));
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
export function appendEvent(ev) {
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
export function updateTyping() {
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
