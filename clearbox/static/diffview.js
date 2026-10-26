// Diff view: a git unified diff (diffs.py) rendered as a table with line numbers, either split
// (old on the left, new on the right, GitLab style) or unified (one column, for the narrow drawer).
// Removed and added lines of one change block are paired up row by row; within a pair, the part
// that actually differs is marked so a one-word edit does not read as a rewritten line.

import { el, esc } from "./core.js";

const HUNK_RE = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@ ?(.*)$/;
// git header lines worth saying in words; the rest (diff --git, index, ---/+++) is noise here
const META = [
  [/^new file mode/, "new file"],
  [/^deleted file mode/, "deleted file"],
  [/^Binary files .* differ$/, "binary file changed; no text diff"],
  [/^old mode (\d+)/, (m) => `mode was ${m[1]}`],
  [/^new mode (\d+)/, (m) => `mode is now ${m[1]}`],
];

// -> { notes: [str], hunks: [{ ctx, rows }] }, rows being
// { kind: "ctx", o, n, text } or { kind: "chg", del: { no, text } | null, add: { no, text } | null }.
// `nonl` on a side marks "\ No newline at end of file".
function parse(diff) {
  const notes = [], hunks = [];
  let hunk = null, o = 0, n = 0, dels = [], adds = [], last = null;
  const flush = () => {
    for (let i = 0; i < Math.max(dels.length, adds.length); i++)
      hunk.rows.push({ kind: "chg", del: dels[i] || null, add: adds[i] || null });
    dels = []; adds = [];
  };
  for (const line of diff.split("\n")) {
    const m = line.match(HUNK_RE);
    if (m) {
      if (hunk) flush();
      hunk = { ctx: m[3], rows: [] }; o = +m[1]; n = +m[2];
      hunks.push(hunk);
      continue;
    }
    if (!hunk) {
      for (const [re, say] of META) {
        const mm = line.match(re);
        if (mm) notes.push(typeof say === "function" ? say(mm) : say);
      }
      continue;
    }
    const sign = line[0], text = line.slice(1);
    if (sign === "-") { last = { no: o++, text }; dels.push(last); }
    else if (sign === "+") { last = { no: n++, text }; adds.push(last); }
    else if (sign === " ") { flush(); last = { kind: "ctx", o: o++, n: n++, text }; hunk.rows.push(last); }
    else if (sign === "\\" && last) last.nonl = true;
  }
  if (hunk) flush();
  return { notes, hunks };
}

// [old html, new html] of a removed/added pair, the differing middle wrapped in <span class="dx">.
// Left alone when the lines share nothing at either end: then the whole line is the change.
function pairHtml(a, b) {
  let p = 0;
  while (p < a.length && p < b.length && a[p] === b[p]) p++;
  let s = 0;
  while (s < a.length - p && s < b.length - p && a[a.length - 1 - s] === b[b.length - 1 - s]) s++;
  if (!p && !s) return [esc(a), esc(b)];
  const mark = (t) => esc(t.slice(0, p)) + (t.length - s > p ? `<span class="dx">${esc(t.slice(p, t.length - s))}</span>` : "")
    + esc(t.slice(t.length - s));
  return [mark(a), mark(b)];
}

const nonl = (side) => (side && side.nonl ? " nonl" : "");
const ln = (no) => `<td class="ln">${no ?? ""}</td>`;

function splitRows(hunk) {
  return hunk.rows.map((r) => {
    if (r.kind === "ctx") {
      const t = esc(r.text);
      return `<tr>${ln(r.o)}<td class="code${nonl(r)}">${t}</td>${ln(r.n)}<td class="code${nonl(r)}">${t}</td></tr>`;
    }
    const [dh, ah] = r.del && r.add ? pairHtml(r.del.text, r.add.text) : [esc(r.del?.text), esc(r.add?.text)];
    const left = r.del ? `${ln(r.del.no)}<td class="code${nonl(r.del)}">${dh}</td>` : `<td class="ln"></td><td class="code"></td>`;
    const right = r.add ? `${ln(r.add.no)}<td class="code${nonl(r.add)}">${ah}</td>` : `<td class="ln"></td><td class="code"></td>`;
    return `<tr class="${r.del ? "l-del" : "l-none"} ${r.add ? "r-add" : "r-none"}">${left}${right}</tr>`;
  }).join("");
}

function unifiedRows(hunk) {
  const out = [], adds = [];
  const row = (cls, o, n, html, side) => `<tr class="${cls}">${ln(o)}${ln(n)}<td class="code${nonl(side)}">${html}</td></tr>`;
  for (const r of hunk.rows) {
    if (r.kind === "ctx") { out.push(...adds.splice(0), row("", r.o, r.n, esc(r.text), r)); continue; }
    const [dh, ah] = r.del && r.add ? pairHtml(r.del.text, r.add.text) : [esc(r.del?.text), esc(r.add?.text)];
    // a change block reads as all its removed lines, then all its added ones (held back in `adds`
    // until the next context line)
    if (r.del) out.push(row("del", r.del.no, null, dh, r.del));
    if (r.add) adds.push(row("add", null, r.add.no, ah, r.add));
  }
  return out.concat(adds).join("");
}

export function diffView(diff, { split = false } = {}) {
  const { notes, hunks } = parse(diff);
  const cols = split ? '<col class="ln"><col><col class="ln"><col>' : '<col class="ln"><col class="ln"><col>';
  const span = split ? 4 : 3;
  const body = hunks.map((h, i) => {
    const head = i || h.ctx ? `<tr class="hunk"><td colspan="${span}">${esc(h.ctx) || "⋯"}</td></tr>` : "";
    return head + (split ? splitRows(h) : unifiedRows(h));
  }).join("");
  return el("div", { class: "diffview" },
    notes.map((t) => el("div", { class: "diff-note" }, t)),
    hunks.length ? el("table", { class: `diff ${split ? "split" : "unified"}`, html: `<colgroup>${cols}</colgroup><tbody>${body}</tbody>` }) : null,
    !hunks.length && !notes.length ? el("div", { class: "diff-note" }, "no textual change") : null);
}
