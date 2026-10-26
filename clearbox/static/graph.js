// The plan DAG: layered layout, SVG rendering, pan and zoom.

import {
  $, agentKey, agentModel, CHIP_H, CHIP_Y, CONFLICT_DY, debounce, el, fmtDur, H_GAP, hcls,
  isUserNode, isWaiting, NODE_H, NODE_W, NODE_W_MAX, planDefaultAgent, sourceText, state,
  TITLE_MIN_PX, TITLE_PX, tokenCounters, truncate, V_GAP, verdictInfo,
} from "./core.js";
import { closeDrawer, selectNode } from "./drawer.js";
import { openPlan } from "./app.js";

// ----------------------------------------------------------- card titles
// A title that fits keeps the default card. A longer one first widens its card (up to NODE_W_MAX),
// then shrinks (down to TITLE_MIN_PX), and only what still does not fit is cut off -- so a plan
// reads at a glance without every card turning wide or tiny.
const measure = document.createElement("canvas").getContext("2d");
const TITLE_RIGHT = 40;  // kept clear for the "1.3" reference in the top-right corner
function textWidth(s, px) {
  measure.font = `560 ${px}px ${getComputedStyle(document.body).fontFamily}`;
  return measure.measureText(s).width + 2;  // a little slack: canvas and SVG metrics differ slightly
}
function fitTitle(title, tx) {
  const room = (w) => w - tx - TITLE_RIGHT;
  const full = textWidth(title, TITLE_PX);
  if (full <= room(NODE_W)) return { w: NODE_W, px: TITLE_PX, text: title };
  const w = Math.min(NODE_W_MAX, Math.ceil(full + tx + TITLE_RIGHT));
  const px = Math.max(TITLE_MIN_PX, Math.min(TITLE_PX, (TITLE_PX * room(w)) / full));
  if (textWidth(title, px) <= room(w)) return { w, px, text: title };
  let n = title.length;
  while (n > 1 && textWidth(title.slice(0, n).trimEnd() + "…", px) > room(w)) n--;
  return { w, px, text: title.slice(0, n).trimEnd() + "…" };
}

// ---------------------------------------------------------------- layout
// widths: node id -> card width (fitTitle); every card keeps NODE_H
function computeLayout(nodes, edges, planId, widths) {
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
  const width = new Map(ids.map((i) => [i, widths.get(i) ?? NODE_W]));
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
      coords.set(v, { x, w: width.get(v), cx: x + width.get(v) / 2, y: top[li] + off, off, top: top[li], bot: top[li] + NODE_H + maxOff[li] });
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
    const dir = b.x > a.x ? 1 : -1, sx = a.cx + dir * (a.w / 2 - 28), ex = b.cx - dir * (b.w / 2 - 28), low = a.bot + arc;
    return `M${sx},${a.y + NODE_H + 2} C${sx},${low} ${ex},${low} ${ex},${b.y + NODE_H + 3}`;
  }
  const c1 = { x: a.cx, y: a.y + NODE_H / 2 }, c2 = { x: b.cx, y: b.y + NODE_H / 2 };
  const dx = c2.x - c1.x, dy = c2.y - c1.y;
  // each end is clipped to its own card, which may be wider than the other
  const clip = (w) => Math.min((w / 2 + 3) / Math.max(Math.abs(dx), 1e-6), (NODE_H / 2 + 3) / Math.max(Math.abs(dy), 1e-6));
  const ta = clip(a.w), tb = clip(b.w);
  return `M${c1.x + dx * ta},${c1.y + dy * ta} L${c2.x - dx * tb},${c2.y - dy * tb}`;
}

// ----------------------------------------------------------------- graph
export function renderGraph() {
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
  const fits = new Map(d.nodes.map((n) => [n.node_id, fitTitle(n.title, isUserNode(n) ? 36 : 16)]));
  const L = computeLayout(d.nodes, d.edges, d.plan.id, new Map([...fits].map(([id, f]) => [id, f.w])));
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
    const p = L.coords.get(n.node_id), fit = fits.get(n.node_id), W = fit.w;
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
      g.append(el("svg:rect", { class: "ghost", x: 8, y: -8, width: W, height: NODE_H, rx: 10 }));
      g.append(el("svg:rect", { class: "ghost", x: 4, y: -4, width: W, height: NODE_H, rx: 10 }));
    }
    // a user job is a different kind of thing, not just a queued node: pill-shaped card with a
    // double border, a person icon instead of the stripe, and no agent chip
    const rx = user ? NODE_H / 2 : 10;
    g.append(el("svg:rect", { class: "halo", x: 0, y: 0, width: W, height: NODE_H, rx }));
    g.append(el("svg:rect", { class: "card", x: 0, y: 0, width: W, height: NODE_H, rx }));
    if (user) {
      g.append(el("svg:rect", { class: "card-inner", x: 4, y: 4, width: W - 8, height: NODE_H - 8, rx: rx - 4 }));
      g.append(el("svg:g", { class: "user-glyph" },
        el("svg:circle", { cx: 24, cy: 22, r: 4.5 }),
        el("svg:path", { d: "M15,37 a9,9 0 0,1 18,0" })));
    } else {
      g.append(el("svg:rect", { class: "stripe", x: 0, y: 10, width: 4, height: NODE_H - 20, rx: 2 }));
    }
    const tx = user ? 36 : 16;
    g.append(el("svg:text", { class: "title", x: tx, y: 27, style: fit.px !== TITLE_PX ? `font-size:${fit.px}px` : null }, fit.text));

    let sub;
    if (n.subplan_id) {
      const pr = n.progress;
      sub = `subplan #${n.subplan_id} · ${pr.done}/${pr.total} done${pr.running ? ` · ${pr.running} running` : ""}`;
      const w = W - 32, tot = Math.max(pr.total, 1);
      g.append(el("svg:rect", { class: "pbar-bg", x: 16, y: CHIP_Y + 6, width: w, height: 4, rx: 2 }));
      g.append(el("svg:rect", { class: "pbar", x: 16, y: CHIP_Y + 6, width: (w * pr.done) / tot, height: 4, rx: 2 }));
      if (pr.running) g.append(el("svg:rect", { class: "pbar run", x: 16 + (w * pr.done) / tot, y: CHIP_Y + 6, width: (w * pr.running) / tot, height: 4, rx: 2 }));
    } else if (user) {
      sub = waitingUser ? "waiting for you — mark done when it is" : n.status === "done" ? "done by you" : n.status === "failed" ? n.error || "failed" : "your job · not its turn yet";
    } else if (n.status === "running") {
      const dur = n.started_at ? ` · ${fmtDur(Date.now() / 1000 - n.started_at)}` : "";
      sub = `${n.agent_name || (n.agent_kind === "external" ? "external" : "agent")} working${dur}`;
    } else if (n.status === "done" && n.started_at && n.finished_at) {
      sub = `done in ${fmtDur(n.finished_at - n.started_at)}`;
    } else if (n.status === "failed") {
      sub = n.error || "failed";
    } else {
      sub = bb ? `${bb.reason === "running" ? "waiting for" : "queued after"} ${bb.plan_id}.${bb.node_id} · shares files`
        : n.status === "ready" ? "ready – waiting for agent" : n.description || "pending";
    }
    g.append(el("svg:text", {
      class: "sub", x: tx, y: n.subplan_id ? 47 : 50,
      "data-started": !n.subplan_id && n.status === "running" && n.started_at ? n.started_at : null,
      "data-who": n.agent_name || (n.agent_kind === "external" ? "external" : "agent"),
    }, truncate(sub, Math.floor((34 * W) / NODE_W))));  // a wider card shows more of it
    g.append(el("svg:text", { class: "ref", x: W - 12, y: 20, "text-anchor": "end" }, `${d.plan.id}.${n.node_id}`));

    if (!n.subplan_id && (n.tokens_in != null || n.tokens_out != null)) {
      // two counters, updated live from {"type":"tokens"} while the node runs
      g.append(tokenCounters(n.tokens_in || 0, n.tokens_out || 0, n.node_id, W));
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
    const gx = W - 22, gy = n.subplan_id ? 50 : CHIP_Y + CHIP_H / 2;
    if (n.status === "running") g.append(el("svg:circle", { class: "spinner", cx: gx, cy: gy, r: 7 }));
    else if (n.status === "done") g.append(el("svg:path", { class: "check", d: `M${gx - 6},${gy} l4,4 l8,-8` }));
    else if (waitingUser) g.append(el("svg:path", { class: "handglyph", d: `M${gx - 7},${gy} h14 M${gx + 1},${gy - 6} l6,6 l-6,6` }));  // "over to you" arrow
    else if (bb) g.append(el("svg:path", { class: "waitglyph", d: `M${gx},${gy - 4} v4 l3,2 M${gx + 7},${gy} a7,7 0 1,1 -14,0 a7,7 0 1,1 14,0` }));
    else if (n.status === "failed") g.append(el("svg:path", { class: "cross", d: `M${gx - 5},${gy - 5} l10,10 M${gx + 5},${gy - 5} l-10,10` }));

    // review marker: quiet for ok, a plainer badge for concerns (advice, not a failure -- a
    // failed node already has its own red state); nothing at all when the node has no review
    if (n.review) {
      const vi = verdictInfo(n.review.verdict), rx = W - 16, ry = 34;
      const rg = el("svg:g", { class: `review-mark ${n.review.verdict}` },
        el("svg:title", {}, `review (${vi.label}): ${n.review.summary}`),
        el("svg:circle", { cx: rx, cy: ry, r: 6 }),
        el("svg:text", { x: rx, y: ry + 3.5, "text-anchor": "middle" }, vi.glyph));
      g.append(rg);
    }
    gN.append(g);
    if (chip) {  // size the pill to its rendered text, never past the status glyph
      const t = chip.querySelector("text"), pad = +t.getAttribute("x") - tx;
      chip.querySelector("rect").setAttribute("width", Math.min(t.getComputedTextLength() + pad + 9, W - 16 - 40));
    }
  }

  if (state.fittedFor !== d.plan.id) { fit(); state.fittedFor = d.plan.id; }
}

// ----------------------------------------------------------- pan & zoom
export const svg = $("#canvas"), vp = $("#viewport");
export function applyView() { const v = state.view; vp.setAttribute("transform", `translate(${v.x},${v.y}) scale(${v.k})`); }
function fit() {
  const L = state.layout; if (!L) return;
  const r = svg.getBoundingClientRect(), pad = 60;
  // the legend floats over the bottom of the canvas: fit the graph above it, not behind it
  const lg = $("#legend").getBoundingClientRect();
  const padB = lg.height ? Math.max(pad, r.bottom - lg.top + 24) : pad, room = r.height - pad - padB;
  const k = Math.min(1.15, (r.width - pad * 2) / Math.max(L.width, 1), room / Math.max(L.height + 10, 1));
  state.view = { k, x: (r.width - L.width * k) / 2, y: pad + Math.max(0, (room - L.height * k) / 2) };
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
