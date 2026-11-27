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
  if (safeWaterTerms(base)) WATER_SAFETY.forEach(add);
  return w;
}
const safeWaterTerms = base => base.includes("water") && base.some(t => ["safe", "drink", "clean", "purif", "treat", "disinfect", "potable", "contamin", "steril"].some(p => t.startsWith(p)));
// "Make water safe / drinkable / clean" and the like: a question whose answer is a dose (exact.js shows it verbatim).
export const wantsSafeWater = q => safeWaterTerms([...new Set(tok(q))]);

// ---- figures: a number with a unit (exact.js shows them verbatim; amount questions favour passages that have them)
const NUMW = "one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty|thirty|forty|fifty|sixty|ninety|hundred|half|quarter";
// mixed numbers first ("1 1/2", "1½"), then decimals and fractions, unicode fractions, number words
const N1 = `(?:\\d+\\s+\\d+\\s*\\/\\s*\\d+|\\d+\\s*[½¼¾⅓⅔⅛]|\\d+(?:[.,]\\d+)?(?:\\s*\\/\\s*\\d+)?|[½¼¾⅓⅔⅛]|\\b(?:${NUMW})\\b)`;
// not inside a word, a decimal or a fraction ("4" in "1/4", "21" in "12/20/2013"); "/2 liters" after a unit is fine
const NUM = `(?<![\\w.])(?<!\\d\\s*\\/\\s*)${N1}(?:\\s*(?:-|–|to)\\s*${N1})?`;
const UNIT = "(?:drops?|gtts?|gts|tsp|teaspoon(?:ful)?s?|tbsp|tablespoon(?:ful)?s?|ml|mls|millilit(?:re|er)s?|mg|milligrams?|" +
  "lit(?:re|er)s?|l|quarts?|qts?|gallons?|gal|pints?|cups?|fl\\.?\\s*oz|ounces?|oz|tablets?|tabs?|ppm|per\\s?cent|percent|%|" +
  "minutes?|mins?|hours?|hrs?|seconds?|secs?|°\\s*[cf]|degrees(?:\\s+(?:celsius|fahrenheit|c|f))?)(?![a-z])";
const ADJ = "(?:(?:full|whole|level|heaped|heaping)\\s+)?";
const SEP = "(?:\\/|\\bper\\b|\\bfor\\s+(?:each|every)\\b|\\bto\\b|\\bin\\b|\\bof\\s+[a-z]+(?:\\s+[a-z]+)?\\s+(?:per|to|in|for\\s+(?:each|every))\\b)";
// A figure: a number with a unit, with an optional "per gallon" / "/30 MIN" / "of bleach per litre" part.
export const FIGURE_SRC = `(?<n>${NUM})\\s*(?:-\\s*)?${ADJ}(?:a\\s+)?(?<u>${UNIT})` +
  `(?:\\s*(?<sep>${SEP})\\s*(?:(?<n2>(?:a|an|each|every)\\b|${N1})\\s*(?:-\\s*)?)?${ADJ}(?<u2>${UNIT}))?`;
export const figures = s => [...String(s).matchAll(new RegExp(FIGURE_SRC, "gi"))].map(m => m[0]);

// The same figure however it is written: number words or digits ("one full minute" = "1 minute"), "%" / "percent",
// units singular, plural or abbreviated (gtts = drops, tsp = teaspoon, L = litre), "5.25% to 8.25%" = "5.25-8.25 percent".
const WORDS = {one: 1, two: 2, three: 3, four: 4, five: 5, six: 6, seven: 7, eight: 8, nine: 9, ten: 10, eleven: 11, twelve: 12,
  fifteen: 15, twenty: 20, thirty: 30, forty: 40, fifty: 50, sixty: 60, ninety: 90, hundred: 100, half: 0.5, quarter: 0.25,
  a: 1, an: 1, each: 1, every: 1, "½": 0.5, "¼": 0.25, "¾": 0.75, "⅓": 1 / 3, "⅔": 2 / 3, "⅛": 0.125};
function num(x){
  x = x.trim().toLowerCase(); if (x in WORDS) return WORDS[x];
  let w = x.match(/^(\d+)\s+(\d+)\s*\/\s*(\d+)$/); if (w) return +w[1] + +w[2] / +w[3];           // 1 1/2
  w = x.match(/^(\d+)\s*([½¼¾⅓⅔⅛])$/); if (w) return +w[1] + WORDS[w[2]];                          // 1½
  let m = x.match(/^(\d+(?:\.\d+)?)\s*\/\s*(\d+)$/); if (m) return +m[1] / +m[2];
  if (/^\d{1,3},\d{3}$/.test(x)) return +x.replace(",", "");
  return +x.replace(",", ".");
}
const UNITS = [[/^(drops?|gtts?|gts)$/, "drop"], [/^(tsp|teaspoon)/, "tsp"], [/^(tbsp|tablespoon)/, "tbsp"], [/^(ml|mls|millilit)/, "ml"],
  [/^(mg|milligram)/, "mg"], [/^(l|lit(re|er)s?)$/, "l"], [/^(quart|qt)/, "quart"], [/^(gallon|gal)/, "gallon"], [/^pint/, "pint"], [/^cup/, "cup"],
  [/^(fl\.?oz|ounce|oz)/, "oz"], [/^tab/, "tablet"], [/^ppm$/, "ppm"], [/^(%|percent)$/, "%"], [/^min/, "min"], [/^(hour|hr)/, "h"],
  [/^sec/, "s"], [/^(°c|degreesc|degreescelsius)$/, "°c"], [/^(°f|degreesf|degreesfahrenheit)$/, "°f"], [/^degree/, "deg"]];
const unit = u => { u = u.toLowerCase().replace(/\s+/g, ""); for (const [re, c] of UNITS) if (re.test(u)) return c; return u; };
const k = n => String(Math.round(n * 10000) / 10000);
export const TIME_UNITS = new Set(["min", "h", "s", "°c", "°f", "deg"]);
// {nums: [n] or [a, b], u, tail: [n, unit] or null}
export function parseFigure(g){
  const nums = g.n.split(/\s*(?:-|–|\bto\b)\s*/i).filter(Boolean).map(num), u = unit(g.u);
  if (!g.u2) return {nums, u, tail: null};
  const n2 = g.n2 ? num(g.n2) : 1, u2 = unit(g.u2);
  if (/^to$/i.test(g.sep.trim()) && u2 === u && nums.length === 1 && g.n2) return {nums: [nums[0], n2], u, tail: null};  // 5.25% to 8.25%
  return {nums, u, tail: [n2, u2]};
}
const atom = (n, u) => `${k(n)} ${u}`;
function figuresIn(text){
  const atoms = new Set(), ranges = new Set(), compounds = new Set(), rows = [];
  for (const line of String(text).split("\n")){
    const byUnit = new Map(), add = (n, u) => { const a = atom(n, u); atoms.add(a); (byUnit.get(u) || byUnit.set(u, new Set()).get(u)).add(a); };
    for (const m of line.matchAll(new RegExp(FIGURE_SRC, "gi"))){
      const f = parseFigure(m.groups), head = f.nums.map(n => atom(n, f.u));
      f.nums.forEach(n => add(n, f.u));
      if (f.nums.length === 2) ranges.add(head.join("-"));
      if (f.tail){ add(...f.tail); compounds.add(head.join("-") + " / " + atom(...f.tail)); }
    }
    if (byUnit.size) rows.push(byUnit);
  }
  return {atoms, ranges, compounds, rows};
}
let memo = ["", null];
// Is this figure (a regexp match's groups) in the text, as the same number(s) and unit, however written? A "per" figure
// ("5 drops per quart") also counts as a table row: a line of the text (a real line break, not a sentence) whose only
// figure in each of the two units is that one ("1 quart/1 liter 5 drops"). A table flattened to one line (a PDF added
// in the browser) has many drops figures on its line, so it never pairs figures from different rows.
export function figureInText(g, text){
  if (memo[0] !== text) memo = [text, figuresIn(text)];
  const F = memo[1], f = parseFigure(g), head = f.nums.map(n => atom(n, f.u));
  const headOk = head.every(a => F.atoms.has(a)) || (head.length === 2 && F.ranges.has(head.join("-")));
  if (!f.tail) return headOk;
  const t = atom(...f.tail), only = (row, u, want) => { const got = row.get(u); return !!got && got.size === want.length && want.every(a => got.has(a)); };
  return F.compounds.has(head.join("-") + " / " + t) || (f.u !== f.tail[1] && F.rows.some(r => only(r, f.u, head) && only(r, f.tail[1], [t])));
}
// How many dose figures (not times or temperatures) a passage holds, for ranking amount questions.
export const doseFigures = t => [...String(t).matchAll(new RegExp(FIGURE_SRC, "gi"))].filter(m => !TIME_UNITS.has(unit(m.groups.u))).length;

const AMOUNT_Q = new RegExp("\\bhow\\s+(?:much|many|long|often|strong)\\b|\\b(?:dose|doses|dosage|dosing|amounts?|quantit(?:y|ies)|ratio|proportions?|" +
  "dilut\\w*|strength|concentration|drops?|teaspoons?|tablespoons?|tsp|tbsp|ml|millilit\\w*|litres?|liters?|quarts?|gallons?|tablets?|" +
  "ppm|percent|per\\s?cent|minutes?|hours?|temperature|degrees)\\b|%|°", "i");
// A question about amounts: an amount word, or "make water safe to drink" (whose answer is a dose).
export const quantityQuestion = q => AMOUNT_Q.test(q) || wantsSafeWater(q);

export function createIndex(chunks){
  const df = new Map(); let total = 0;
  chunks.forEach((c, i) => { c.i = i; c.tf = new Map(); const ts = tok(c.t); c.len = ts.length; total += ts.length;
    for (const t of ts) c.tf.set(t, (c.tf.get(t) || 0) + 1);
    for (const t of c.tf.keys()) df.set(t, (df.get(t) || 0) + 1); });
  return {chunks, df, avgdl: total / Math.max(1, chunks.length)};
}

// For a question about amounts, a passage scores up to DOSE_BOOST_MAX x more for the dose figures it holds
// (DOSE_BOOST each, at most DOSE_BOOST_CAP of them): the table with the drops ranks above the prose around it.
export const DOSE_BOOST = 0.15, DOSE_BOOST_CAP = 8;
export function scoreAll(ix, q, {figures: useFigures = true} = {}){
  const qt = queryTerms(q), N = ix.chunks.length, k1 = 1.4, b = 0.75, scored = [], dose = useFigures && quantityQuestion(q);
  for (const c of ix.chunks){ let s = 0;
    for (const [t, wt] of qt){ const f = c.tf.get(t); if (!f) continue;
      const n = ix.df.get(t); const idf = Math.log(1 + (N - n + 0.5) / (n + 0.5));
      s += wt * idf * f * (k1 + 1) / (f + k1 * (1 - b + b * c.len / ix.avgdl)); }
    if (s > 0 && dose){ if (c.ndose === undefined) c.ndose = doseFigures(c.t); s *= 1 + DOSE_BOOST * Math.min(c.ndose, DOSE_BOOST_CAP); }
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

// How many characters of sources fit the model's context (tokens). The prompt must leave room for the answer
// (max_tokens 700), the system prompt, the six source headers and the closing line (~1,000 tokens at this
// rate) and the question itself. Manual text with tables and numbers tokenizes worse than prose, so this
// counts 2 characters per token (English prose is ~4): 4096 -> ~4,750 characters for a short question,
// 8192 and up -> 9,000 (six sources with their neighbour chunks).
export const ANSWER_TOKENS = 700, OVERHEAD_TOKENS = 1000, CHARS_PER_TOKEN = 2;
export const sourceBudget = (ctx, question = "") => Math.max(2000, Math.min(9000,
  Math.floor(((ctx || 4096) - ANSWER_TOKENS - OVERHEAD_TOKENS) * CHARS_PER_TOKEN) - question.length));

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

const RULES = {
  role: "You are an offline survival and emergency reference assistant.",
  only: "Answer ONLY the question asked, using ONLY the numbered sources provided. Do not add facts, tips, warnings or topics that the question did not ask about, even if a source mentions them.",
  cite: "Cite every claim with its source number in square brackets like [1] or [2][3]. If the sources do not contain the answer, say so plainly instead of answering from memory.",
  numbers: "Numbers: give every dose, amount or time exactly as its source states it, together with the conditions the source attaches to it (product strength or percentage, amount of water, clear, cloudy or cold water, waiting time, and purpose such as treatment or storage), with its citation.",
  noMix: "Never combine, average or convert figures from different sources, tables or table rows. If sources give different figures, list each one separately with its own citation. If you are not sure which row of a table applies, quote the rows as written.",
  check: "After any dose or amount, add a short caution to check it against the cited page. If no source states an amount, say that the sources give no amount; do not guess one.",
  style: "Be practical and concise. Put citations inside sentences; do not end with a line of citations only.",
  au: "The user is in Australia. If an Australian note and an older US manual disagree on the question asked, follow the Australian note and mention the difference. The Australian emergency number is 000.",
  food: "Never state that a wild plant or mushroom is safe to eat.",
};
export const SYSTEM_PROMPT = Object.values(RULES).join(" ");
// When exact.js shows the source amounts word for word, the model must not write figures at all: a 4B model
// paraphrasing a dosing table gets them wrong. It frames the answer and points to the sources instead.
export const exactRule = ns => `Exact amounts: the reader is shown the passages of source${ns.length > 1 ? "s" : ""} ${ns.map(n => `[${n}]`).join(" ")} ` +
  "that hold the amounts, word for word, next to your answer. So do NOT write any figure yourself: no numbers, doses, amounts, " +
  "ratios, percentages, temperatures or times, in digits or in words, and never convert or combine them. Instead, in a few short " +
  "sentences, say which method each source describes, which source [n] gives its amounts, and in words what the amounts depend on " +
  "(product strength, amount of water, clear, cloudy or cold water, waiting time, treatment or storage). Then tell the reader to " +
  "use the exact wording shown. If the sources do not answer the question, say so.";
export const exactPrompt = ns => [RULES.role, RULES.only, RULES.cite, exactRule(ns), RULES.style, RULES.au, RULES.food].join(" ");

// A final line or paragraph made only of citations ("[2]", "[1][2]") adds nothing: drop it.
export const tidyAnswer = txt => txt.replace(/\s*\n[ \t]*(\[\d{1,3}\][ \t,]*)+\s*$/, "\n");

// exact: the excerpts exact.js shows word for word ([{n, ...}]); with any, the model is told not to write figures.
export function buildMessages(q, hits, title, where, exact = []){
  const ctx = hits.map((c, i) => `[${i + 1}] ${title(c)}, ${where(c)}:\n${c.t}`).join("\n\n");
  const ns = exact.map(b => b.n);
  return [{role: "system", content: ns.length ? exactPrompt(ns) : SYSTEM_PROMPT},
          {role: "user", content: `Sources:\n${ctx}\n\nQuestion: ${q}\n\nAnswer only this question, from these sources, with citations.` +
            (ns.length ? " Do not write any amounts or times: they are shown to the reader word for word." : "")}];
}
