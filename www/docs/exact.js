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
//   4. renderAnswer(): every figure in the model's answer that is not word for word in the excerpts shown (or, with
//      no excerpts, in the sources sent) is removed and marked, with a caution under the answer.

import {queryTerms, wantsSafeWater, tok} from "./retrieve.js";
import {renderMarkdown, esc} from "../md.js";

const NUMW = "one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty|thirty|forty|fifty|sixty|ninety|hundred|half|quarter";
const N1 = `(?:\\d+(?:[.,]\\d+)?(?:\\s*\\/\\s*\\d+)?|[½¼¾⅓⅔⅛]|\\b(?:${NUMW})\\b)`;
const NUM = `(?<![\\w.\\/])${N1}(?:\\s*(?:-|–|to)\\s*${N1})?`;
const UNIT = "(?:drops?|gtts?|gts|tsp|teaspoon(?:ful)?s?|tbsp|tablespoon(?:ful)?s?|ml|mls|millilit(?:re|er)s?|mg|milligrams?|" +
  "lit(?:re|er)s?|l|quarts?|qts?|gallons?|gal|pints?|cups?|fl\\.?\\s*oz|ounces?|oz|tablets?|tabs?|ppm|per\\s?cent|percent|%|" +
  "minutes?|mins?|hours?|hrs?|seconds?|secs?|°\\s*[cf]|degrees(?:\\s+(?:celsius|fahrenheit|c|f))?)(?![a-z])";
const ADJ = "(?:(?:full|whole|level|heaped|heaping)\\s+)?";
const TAIL = `(?:\\s*(?:\\/|\\bper\\b|\\bfor\\s+(?:each|every)\\b|\\bto\\b|\\bin\\b|\\bof\\s+[a-z]+(?:\\s+[a-z]+)?\\s+(?:per|to|in|for\\s+(?:each|every))\\b)` +
  `\\s*(?:(?:a|an|each|every|${N1})\\s*-?\\s*)?${ADJ}${UNIT})?`;
// A figure: a number with a unit, with an optional "per gallon" / "/30 MIN" / "of bleach per litre" part.
export const FIGURE_SRC = `${NUM}\\s*(?:-\\s*)?${ADJ}(?:a\\s+)?${UNIT}${TAIL}`;
const hasFigure = s => new RegExp(FIGURE_SRC, "i").test(s);
export const figures = s => [...String(s).matchAll(new RegExp(FIGURE_SRC, "gi"))].map(m => m[0]);

const AMOUNT_Q = new RegExp("\\bhow\\s+(?:much|many|long|often|strong)\\b|\\b(?:dose|doses|dosage|dosing|amounts?|quantit(?:y|ies)|ratio|proportions?|" +
  "dilut\\w*|strength|concentration|drops?|teaspoons?|tablespoons?|tsp|tbsp|ml|millilit\\w*|litres?|liters?|quarts?|gallons?|tablets?|" +
  "ppm|percent|per\\s?cent|minutes?|hours?|temperature|degrees)\\b|%|°", "i");
export const quantityQuestion = q => AMOUNT_Q.test(q) || wantsSafeWater(q);

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
  if (!quantityQuestion(q)) return [];
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

// The text the answer's figures are checked against: the excerpts shown, or (no excerpts) the sources sent.
export const allowedText = (blocks, hits) => (blocks.length ? blocks.flatMap(b => b.excerpts) : hits.map(h => h.t)).join("\n");
const norm = s => s.toLowerCase().replace(/\s+/g, " ");

// Figures in the answer that are not word for word in `allowed` become a marker. -> {text, removed}
// A whole-figure match only: "4 teaspoon" is not in "1/4 teaspoon", nor "5 drops" in "15 drops", nor "6 hours" in "16 hours".
export function inWords(allowed, fig){
  const a = norm(allowed), f = norm(fig).trim();
  for (let i = a.indexOf(f); f && i >= 0; i = a.indexOf(f, i + 1))
    if (!/[\w.\/]/.test(a[i - 1] || "") && !/[a-z0-9]/.test(a[i + f.length] || "")) return true;
  return false;
}
export function guardFigures(text, allowed){
  let removed = 0;
  text = String(text).replace(/\u0001/g, "").replace(new RegExp(FIGURE_SRC, "gi"), m => inWords(allowed, m) ? m : (removed++, "\u0001"));
  return {text, removed};
}
export const REMOVED_HTML = '<span class="removed" title="The AI wrote an amount that is not word for word in the sources">[amount removed]</span>';
export function renderAnswer(text, allowed, cite, exactShown){
  const g = guardFigures(text, allowed);
  let html = renderMarkdown(g.text, cite).replace(/\u0001/g, REMOVED_HTML);
  if (g.removed) html += `<p class="caution">The AI wrote ${g.removed === 1 ? "an amount" : g.removed + " amounts"} not found word for word in ` +
    `the sources; ${g.removed === 1 ? "it was" : "they were"} removed. ` +
    (exactShown ? "Use the exact wording from the sources above." : "Check the cited pages for the exact amounts.") + "</p>";
  return {html, removed: g.removed};
}
