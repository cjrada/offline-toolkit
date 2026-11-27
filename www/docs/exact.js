// Ask the Manuals: source amounts shown WORD FOR WORD. No dependencies besides md.js / retrieve.js; tests run it under node.
//
// A 4B model garbles numbers when it paraphrases a dosing table (it merged "5 drops per quart" with "per gallon"
// rows, dropped the bleach strength and invented "20 drops per gallon"), and prompt rules alone do not stop it.
// So for a question about amounts the page shows the source passages that hold them, exactly as they are in the
// index (line breaks and table layout kept), each with its [n] link, rendered here and never by the model:
//   1. quantityQuestion(q): the question asks for an amount (how much / how many / how long / dose / ratio /
//      drops / teaspoon / ml / litre / tablets / ppm / percent / minutes / temperature...) or is a "make water
//      safe to drink" question (retrieve.js wantsSafeWater), where the answer is a dose.
//   2. extractExact(ix, q, hits): in each retrieved source, the lines (or sentences, for PDFs added in the
//      browser, whose text has no line breaks) holding a figure = a number with a unit (FIGURE). Runs of such
//      lines are a group (a table), with up to 3 lines before it (heading, column names) and the rest of a
//      sentence cut at a line end. A group is kept only if it shares topic words with the question (idf-weighted,
//      at least half the best group of that source; a source whose best group scores under 0.3 x the best of
//      all sources gets none). Lines that set the scope of the figures ("All figures are based on
//      one quart of water") are added. The result is one or more excerpts per source: slices of the source text,
//      never edited.
//   3. The model is told the amounts are shown to the reader and that it must not write any figure itself
//      (retrieve.js buildMessages with the excerpts).
//   4. renderAnswer(): every figure in the model's answer that is not in the sources sent (same number(s) and unit,
//      however written: "one full minute" = "1 minute", "5.25% to 8.25%" = "5.25-8.25 percent"; a "per" figure must be
//      in the sources as such or as one table row) is removed and marked, with a caution under the answer.
//   5. Before that, the dose guard (dose.js): a dose (drops, teaspoons, tablespoons, tablets) must also match one source
//      fact on its volume of water, clear / cloudy / cold water, strength and agent. "For a gallon of water add 10 drops
//      of bleach" passes the figure check ("10 drops" and "1 gallon" are each in the WA DoH table) but not this one:
//      10 drops is that table's figure for 1/2 gallon. This runs on every answer, amount question or not.
//   Retrieval (retrieve.js scoreAll) also ranks passages with dose figures higher for amount questions.

import {queryTerms, tok, FIGURE_SRC, quantityQuestion, figureInText} from "./retrieve.js";
import {renderMarkdown, esc} from "../md.js";
import {checkDoses, markSpans} from "./dose.js";
import {medicineDoseQuestion, medicineDoseIn, doseSubjectWords, sourceDoseNote, NOT_COVERED} from "../medicine.js";

export {FIGURE_SRC, figures, quantityQuestion, figureInText} from "./retrieve.js";
const hasFigure = s => new RegExp(FIGURE_SRC, "i").test(s);

// Question words that say what KIND of answer is wanted, not its topic: they do not make a passage relevant.
const GENERIC = new Set(["much", "many", "long", "often", "strong", "add", "use", "make", "need", "put", "get", "take", "give",
  "dose", "dosage", "amount", "quantity", "quantities", "ratio", "drop", "teaspoon", "tablespoon", "ml", "litre", "liter", "quart", "gallon",
  "tablet", "ppm", "percent", "minute", "hour", "temperature", "degree", "safe",
  "have", "has", "had", "not", "no", "any", "all", "some", "more", "most", "less", "needs", "one", "every", "each", "per", "used",
  "go", "keep", "very", "just", "also", "only", "other", "own", "same", "too", "after", "before", "up", "out", "off", "over"]
  .map(w => tok(w)[0] || w));

// Lines (text with line breaks, as in index.js) or sentences (one-line text, as for PDFs added in the browser),
// as [start, end] offsets into t.
function segments(t){
  const out = [];
  if ((t.match(/\n/g) || []).length >= 2){ let s = 0; for (const line of t.split("\n")){ out.push([s, s + line.length]); s += line.length + 1; } }
  else { const re = /[.!?](?=\s+[A-Z(0-9])/g; let s = 0, m;
    while ((m = re.exec(t))){ out.push([s, m.index + 1]); s = m.index + 1; while (s < t.length && /\s/.test(t[s])) s++; }
    if (s < t.length) out.push([s, t.length]); }
  return out.filter(([a, b]) => t.slice(a, b).trim());
}
const SCOPE = /\b(?:all\s+(?:figures|charts|amounts|doses|tables|quantities)\b|(?:figures|charts?|amounts|doses)\s+(?:are\s+)?based\s+on)/i;
const ends = s => /[.:;)!?]\s*$/.test(s), continues = s => /^\s*[a-z][a-z]/.test(s);
// sources: at most this many sources get excerpts; chars: per source; source / group: a source is used if its best group
// scores at least `source` x the best group of all sources, and its groups that score at least `group` x its own best.
export const LIMITS = {sources: 4, chars: 2200, leadIn: 3, leadChars: 240, gap: 2, source: 0.3, group: 0.5};

// The verbatim excerpts for q: [{n, hit, excerpts: [string]}] (n = the source's number in the answer), or [].
export function extractExact(ix, q, hits){
  if (!quantityQuestion(q) && !medicineDoseQuestion(q)) return [];
  const qt = queryTerms(q), N = Math.max(1, ix.chunks.length);
  const topic = [...qt].filter(([t]) => !GENERIC.has(t) && !/^\d+$/.test(t));
  // idf, at least 1: in a small library (a few PDFs added in the browser) every word is "common"
  const idf = t => { const n = ix.df.get(t) || 0; return Math.max(1, Math.log(1 + (N - n + 0.5) / (n + 0.5))); };
  const score = s => { const have = new Set(tok(s)); return topic.reduce((x, [t, w]) => x + (have.has(t) ? w * idf(t) : 0), 0); };
  const per = hits.map((hit, h) => {
    const t = hit.t, seg = segments(t), txt = seg.map(([a, b]) => t.slice(a, b)), qty = txt.map(hasFigure), n = seg.length, groups = [];
    for (let i = 0; i < n;){
      if (!qty[i]){ i++; continue; }
      let j = i;                                        // a run of figure lines, allowing one line without a figure
      while (j + 1 < n && (qty[j + 1] || (j + 2 < n && qty[j + 2]))) j += qty[j + 1] ? 1 : 2;
      let a = i, lead = 0; const stop = groups.length ? groups.at(-1).b : -1;
      while (a - 1 > stop && !qty[a - 1] && i - a < LIMITS.leadIn && lead + txt[a - 1].length <= LIMITS.leadChars) lead += txt[--a].length;
      let b = j;
      for (let k = 0; k < 2 && b + 1 < n && !ends(txt[b]) && continues(txt[b + 1]); k++) b++;
      groups.push({a, b, s: score(txt.slice(a, b + 1).join("\n"))});
      i = b + 1;
    }
    return {hit, h, t, seg, txt, groups};
  });
  const best = Math.max(0, ...per.flatMap(p => p.groups.map(g => g.s)));
  if (!(best > 0)) return [];
  const out = [];
  for (const p of per){
    const top = Math.max(0, ...p.groups.map(g => g.s));
    if (!(top > 0) || top < best * LIMITS.source) continue;
    let keep = p.groups.filter(g => g.s >= top * LIMITS.group);
    const pick = sel => { const on = new Set();
      for (const g of sel) for (let i = g.a; i <= g.b; i++) on.add(i);
      p.txt.forEach((s, i) => { if (!SCOPE.test(s)) return;   // the scope of the figures, with the rest of its sentence
        on.add(i); for (let k = i; k < i + 2 && k + 1 < p.txt.length && !ends(p.txt[k]) && continues(p.txt[k + 1]); k++) on.add(k + 1); });
      const idx = [...on].sort((x, y) => x - y), runs = [];
      for (const i of idx){ const r = runs.at(-1); if (r && i - r[1] <= LIMITS.gap + 1) r[1] = i; else runs.push([i, i]); }
      return runs.map(([x, y]) => p.t.slice(p.seg[x][0], p.seg[y][1]));
    };
    let ex = pick(keep);
    while (keep.length > 1 && ex.join("").length > LIMITS.chars){   // too long: drop the least relevant group
      const worst = keep.reduce((w, g) => g.s < w.s ? g : w); keep = keep.filter(g => g !== worst); ex = pick(keep); }
    out.push({n: p.h + 1, hit: p.hit, excerpts: ex, s: Math.max(...keep.map(g => g.s))});
  }
  return out.sort((x, y) => y.s - x.s).slice(0, LIMITS.sources).sort((x, y) => x.n - y.n).map(({n, hit, excerpts}) => ({n, hit, excerpts}));
}

// The "From the sources (exact wording)" section. Everything from a source is escaped.
export function renderExact(blocks, title, where, link){
  if (!blocks.length) return "";
  return `<h3>From the sources (exact wording)</h3><p class="small">Amounts exactly as printed in each source, with the conditions it gives. ` +
    `Use these, not figures from the AI, and check the page.</p>` +
    blocks.map(b => `<figure class="exact-src"><figcaption><a href="${esc(link(b.hit))}" target="_blank" rel="noopener">[${b.n}]</a> ` +
      `${esc(title(b.hit))}, ${esc(where(b.hit))}</figcaption>` +
      b.excerpts.map(x => `<pre>${esc(x)}</pre>`).join('<p class="gap">…</p>') + `</figure>`).join("");
}

// The text the answer's figures are checked against: all the sources sent (the excerpts shown are slices of them).
// A blank line between sources: the dose guard reads each on its own (one source's bleach strength is not another's).
export const allowedText = (blocks, hits) => [...hits.map(h => h.t), ...blocks.flatMap(b => b.excerpts)].join("\n\n");

// Figures in the answer that are not in `allowed` (same numbers and unit, however written: see retrieve.js
// figureInText) become a marker. -> {text, removed}
export function guardFigures(text, allowed){
  let removed = 0;
  text = String(text).replace(/\u0001/g, "").replace(new RegExp(FIGURE_SRC, "gi"), (...a) => figureInText(a.at(-1), allowed) ? a[0] : (removed++, "\u0001"));
  return {text, removed};
}
// Both guards, in one pass over the answer: a dose (dose.js) is removed when no source gives it with its conditions,
// and kept when the answer states it in full, with every condition its source fact has ("2 drops per quart of clear
// 4-6% bleach" for a USMC row whose quart is in the note under the table); any other figure, and a dose not stated
// in full, must be in the sources (figureInText). The model cannot forge a marker (\u0001 is dropped first).
// -> {text, removed, doses: [description of each dose removed]}
export function guardAnswer(text, allowed){
  text = String(text).replace(/\u0001/g, "");
  const d = checkDoses(text, allowed), over = (s, [a, b]) => s.some(([x, y]) => a < y && b > x), inside = (s, [a, b]) => s.some(([x, y]) => a >= x && b <= y);
  const cut = [...d.bad];
  for (const m of text.matchAll(new RegExp(FIGURE_SRC, "gi"))){
    const r = [m.index, m.index + m[0].length];
    if (over(d.bad, r) || inside(d.full, r)) continue;
    if (!figureInText(m.groups, allowed)) cut.push(r);
  }
  return {...markSpans(text, cut), doses: d.doses};
}
export const REMOVED_HTML = '<span class="removed" title="The AI wrote an amount that is not word for word in the sources">[amount removed]</span>';
export function renderAnswer(text, allowed, cite, exactShown){
  const g = guardAnswer(text, allowed);
  let html = renderMarkdown(g.text, cite).replace(/\u0001/g, REMOVED_HTML);
  if (g.removed) html += `<p class="caution">${g.removed === 1 ? "An amount in the AI's answer wasn't" : g.removed + " amounts in the AI's answer weren't"} ` +
    `found in the sources and ${g.removed === 1 ? "was" : "were"} removed - ${exactShown ? "use the exact wording above" : "check the cited pages"}.</p>`;
  return {html, removed: g.removed};
}

// ---- medicine doses: only word for word from the kit's documents, never from the AI (../medicine.js)
// The excerpts of the box that hold a medicine dose (by content: any document, also one added to the index later) for
// what the question asks about: one of its own words (the drug, or the condition) must be in the excerpt or in the
// 300 characters of its source just before it. So a paracetamol question never shows an ibuprofen table.
export function medicineExcerpts(q, exact){
  const words = doseSubjectWords(q).map(w => new RegExp(`\\b${w.replace(/[-]/g, "[- ]?")}`, "i"));
  if (!words.length) return [];
  return exact.map(b => ({...b, excerpts: b.excerpts.filter(x => {
    if (!medicineDoseIn(x)) return false;
    const at = b.hit.t.indexOf(x), ctx = at >= 0 ? b.hit.t.slice(Math.max(0, at - 300), at + x.length) : x;
    return words.some(w => w.test(ctx));
  })})).filter(b => b.excerpts.length);
}
// What Ask the Manuals shows for a medicine dose question, or for an amount question whose box holds a medicine dose,
// without asking the AI. -> null (not a medicine dose: the usual path), or {box, text}:
//   box: the source passages holding the dose, shown word for word with their [n] links (renderExact); text: the caveat
//   (sourceDoseNote), or with no such passage NOT_COVERED (the kit's documents do not give it + the safe pointer).
export function medicineRoute(q, exact){
  const box = medicineExcerpts(q, exact);
  if (!box.length && !medicineDoseQuestion(q)) return null;
  return box.length ? {box, text: sourceDoseNote(box.map(b => b.n))} : {box: [], text: NOT_COVERED};
}
