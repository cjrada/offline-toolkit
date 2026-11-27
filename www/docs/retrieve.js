// Ask the Manuals retrieval: BM25 keyword search over the pre-chunked passages (index.js) plus PDFs added
// in this browser. No dependencies; tests/test_kit_js.mjs runs it under node.
//
// Passages are ~900 characters with a 150-character overlap, so a table or list is often split across two
// chunks of the same page ("(b) Chlorine bleach. The strength of the solution, number of" | the drops table).
// So each hit is sent with the NEXT chunk of its page (and the previous one when the match sits at the very
// start of the hit), merged without the overlap; consecutive hits on one page become one source.
// The query is stemmed (purify / purified / purifying / purification -> "purif") and lightly expanded:
// a "make water safe / drinkable / clean" question also looks for the disinfection words (bleach, boil,
// chlorine, iodine...) at half weight, so the passages with real quantities are found.

const STOP = new Set(("a an and are as at be but by can do does for from how i if in into is it its me my of on or our " +
  "so than that the their then there these this to was we what when where which who why will with you your should " +
  "would could about").split(" "));

export function stem(w){
  if (w.length <= 4) return w;
  w = w.replace(/ication(s)?$/, "")              // purification -> purif
       .replace(/(ifying|ified|ifies|ify)$/, "if")  // purify / purified / purifying -> purif
       .replace(/ect(ions?|ants?)$/, "ect");        // disinfection / disinfectant -> disinfect
  const m = w.match(/(ings?|edly|ed|ies|es)$/);
  if (m && w.length - m[0].length >= 4) return w.slice(0, -m[0].length);
  return /[^s]s$/.test(w) && w.length > 4 ? w.slice(0, -1) : w;
}
export const tok = s => (s.toLowerCase().match(/[a-z0-9]+/g) || []).filter(w => w.length > 1 && !STOP.has(w)).map(stem);

// Query expansion (stemmed terms, added at EXPAND_WEIGHT). Kept small and specific on purpose.
export const EXPAND_WEIGHT = 0.5;
const WATER_SAFETY = ["purif", "disinfect", "bleach", "chlorine", "iodine", "boil", "treat", "tablet"].map(stem);
const EXPAND = {
  purif: ["disinfect", "treat", "bleach", "boil"], disinfect: ["purif", "bleach", "chlorine"],
  potable: ["drink", "purif"], drinkable: ["drink", "purif"], sterilise: ["purif", "boil"], sterilize: ["purif", "boil"],
};
export function queryTerms(q){
  const base = [...new Set(tok(q))], w = new Map(base.map(t => [t, 1]));
  const add = t => { t = stem(t); if (!w.has(t)) w.set(t, EXPAND_WEIGHT); };
  for (const t of base) for (const x of EXPAND[t] || []) add(x);
  const wantsSafeWater = w.has("water") && base.some(t => ["safe", "drink", "clean", "purif", "treat", "disinfect", "potable", "contamin", "steril"].some(p => t.startsWith(p)));
  if (wantsSafeWater) WATER_SAFETY.forEach(add);
  return w;
}

export function createIndex(chunks){
  const df = new Map(); let total = 0;
  chunks.forEach((c, i) => { c.i = i; c.tf = new Map(); const ts = tok(c.t); c.len = ts.length; total += ts.length;
    for (const t of ts) c.tf.set(t, (c.tf.get(t) || 0) + 1);
    for (const t of c.tf.keys()) df.set(t, (df.get(t) || 0) + 1); });
  return {chunks, df, avgdl: total / Math.max(1, chunks.length)};
}

export function scoreAll(ix, q){
  const qt = queryTerms(q), N = ix.chunks.length, k1 = 1.4, b = 0.75, scored = [];
  for (const c of ix.chunks){ let s = 0;
    for (const [t, wt] of qt){ const f = c.tf.get(t); if (!f) continue;
      const n = ix.df.get(t); const idf = Math.log(1 + (N - n + 0.5) / (n + 0.5));
      s += wt * idf * f * (k1 + 1) / (f + k1 * (1 - b + b * c.len / ix.avgdl)); }
    if (s > 0) scored.push([s * (c.boost || 1), c]); }
  return scored.sort((a, b) => b[0] - a[0]);
}

const samePage = (a, b) => a && b && a.f === b.f && a.p === b.p && a.blob === b.blob;
// Join two consecutive chunks of one page, dropping the overlap (the start of b repeats the end of a).
export function joinText(a, b){
  for (let n = Math.min(a.length, b.length, 400); n >= 20; n--) if (a.endsWith(b.slice(0, n))) return a + b.slice(n);
  const k = a.indexOf(b.slice(0, 40));
  return k >= 0 && k > a.length - 400 ? a.slice(0, k) + b : a + " " + b;
}
function startsWithMatch(c, qt){ // a query word in the first 150 characters: the answer may begin on the chunk before
  const head = new Set(tok(c.t.slice(0, 150)));
  return [...qt.keys()].some(t => qt.get(t) === 1 && head.has(t));
}

// How many characters of sources fit the model's context (tokens): the prompt must leave room for the
// system prompt, question and source headers (~900 tokens at this rate) and the answer (max_tokens 700). Manual text with tables
// and numbers tokenizes worse than prose, so this counts 2 characters per token (English prose is ~4):
// 4096 -> ~5,000 characters, 8192 and up -> 9,000 (six sources with their neighbour chunks).
export const ANSWER_TOKENS = 700, OVERHEAD_TOKENS = 900, CHARS_PER_TOKEN = 2;
export const sourceBudget = ctx => Math.max(2000, Math.min(9000, Math.floor(((ctx || 4096) - ANSWER_TOKENS - OVERHEAD_TOKENS) * CHARS_PER_TOKEN)));

// The top k sources for q: [{f, p, u, blob, t, first, last}], each one or more consecutive chunks of one page,
// at most maxPerPage sources per page and about maxChars of text in all.
export function search(ix, q, {k = 6, maxPerPage = 2, maxChars = 9000} = {}){
  const scored = scoreAll(ix, q), qt = queryTerms(q), picked = [], used = new Set(), perPage = {};
  let chars = 0;
  for (const [, c] of scored){
    if (picked.length >= k) break;
    if (used.has(c.i)) continue;
    const key = c.f + "\u0000" + c.p;
    // A hit next to a source already picked extends that source instead of taking a new slot.
    const nb = picked.find(s => samePage(s, c) && (s.first === c.i + 1 || s.last === c.i - 1));
    if (nb){ if (chars + c.t.length > maxChars) continue;
      if (nb.last === c.i - 1){ nb.t = joinText(nb.t, c.t); nb.last = c.i; } else { nb.t = joinText(c.t, nb.t); nb.first = c.i; }
      used.add(c.i); chars += c.t.length; continue; }
    if (picked.length && chars + c.t.length > maxChars) continue;   // the budget (always at least one source)
    if ((perPage[key] = (perPage[key] || 0) + 1) > maxPerPage) continue;
    const s = {f: c.f, p: c.p, u: c.u, blob: c.blob, t: c.t, first: c.i, last: c.i};
    used.add(c.i); chars += c.t.length; picked.push(s);
  }
  // Context: the next chunk of the page for every source, then the previous one where the match is at the start.
  for (const s of picked){
    const next = ix.chunks[s.last + 1];
    if (samePage(next, s) && !used.has(next.i) && chars + next.t.length <= maxChars){
      s.t = joinText(s.t, next.t); s.last = next.i; used.add(next.i); chars += next.t.length; }
  }
  for (const s of picked){
    const first = ix.chunks[s.first], prev = ix.chunks[s.first - 1];
    if (samePage(prev, s) && !used.has(prev.i) && startsWithMatch(first, qt) && chars + prev.t.length <= maxChars){
      s.t = joinText(prev.t, s.t); s.first = prev.i; used.add(prev.i); chars += prev.t.length; }
  }
  return picked;
}

export const SYSTEM_PROMPT = [
  "You are an offline survival and emergency reference assistant.",
  "Answer ONLY the question asked, using ONLY the numbered sources provided. Do not add facts, tips, warnings or topics that the question did not ask about, even if a source mentions them.",
  "Cite every claim with its source number in square brackets like [1] or [2][3]. If the sources do not contain the answer, say so plainly instead of answering from memory.",
  "Never invent or convert numbers. When you give any dose or quantity (bleach, iodine, chlorine, tablets, medication, mixing ratios, times), quote it exactly as the source states it, with its units and its citation, and add a short caution to check it against the cited page. If no source states a quantity, say that the sources give no amount; do not guess one.",
  "Be practical and concise.",
  "The user is in Australia. If an Australian note and an older US manual disagree on the question asked, follow the Australian note and mention the difference. The Australian emergency number is 000.",
  "Never state that a wild plant or mushroom is safe to eat.",
].join(" ");

export function buildMessages(q, hits, title, where){
  const ctx = hits.map((c, i) => `[${i + 1}] ${title(c)}, ${where(c)}:\n${c.t}`).join("\n\n");
  return [{role: "system", content: SYSTEM_PROMPT},
          {role: "user", content: `Sources:\n${ctx}\n\nQuestion: ${q}\n\nAnswer only this question, from these sources, with citations.`}];
}
