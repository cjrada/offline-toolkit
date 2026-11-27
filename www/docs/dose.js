// Doses read WITH their conditions (no dependencies; used by exact.js on the page and by tools/llm_eval.mjs).
//
// A dose (drops, teaspoons, tablespoons, tablets) is read as a fact: amount + unit + the volume of water it is for +
// water state (clear / cloudy / cold) + product strength (%) + agent (bleach / iodine). A dose in an answer is
// grounded only when one source fact agrees on all of them that the answer states. So "10 drops per gallon" is NOT
// grounded by a table that gives 10 drops for 1/2 gallon and 5 drops for 1 quart, although "10 drops" and "1 gallon"
// are each in that table (the figure check in exact.js / retrieve.js figureInText looks at figures one by one, and
// "For a gallon of water add 10 drops of bleach" passes it).
// Sources are read the same way: a table row's volume ("1 quart/1 liter   5 drops"), a column header (CLEAR WATER
// CLOUDY WATER  COLD WATER), a note under a table ("All figures are based on one quart of water"), the strength /
// agent named earlier in the passage, and a "double the amount" rule for cloudy water.
// Each answer dose has a span (offsets into the text as given), so the page can remove exactly that dose.

const WORDNUM = {one: 1, two: 2, three: 3, four: 4, five: 5, six: 6, seven: 7, eight: 8, nine: 9, ten: 10, twenty: 20, thirty: 30, sixty: 60};
export const UNIT_WORDS = "drops?|gtts?|gts|teaspoons?|tablespoons?|tablets?|minutes?|hours?|seconds?|quarts?|gallons?|lit(?:re|er)s?|pints?|cups?|canteens?|percent|per ?cent|days?|metres?|meters?";
const NORM = [
  [/\*\*?|__/g, () => ""],                                                       // markdown emphasis
  [/(\d),(\d{3})\b/g, (m, a, b) => a + b],                                        // 1,000 -> 1000
  [new RegExp(`\\b(${Object.keys(WORDNUM).join("|")})\\b(?=[\\s-]+(?:${UNIT_WORDS})\\b)`, "gi"), w => String(WORDNUM[w.toLowerCase()])],  // five drops
];
// The text as the readers below see it, and where each of its characters came from: map[i] = offset in s.
export function normMapped(s){
  s = String(s); let text = s, map = Array.from({length: s.length}, (_, i) => i);
  for (const [re, f] of NORM){
    let out = "", m2 = [], last = 0;
    for (const m of text.matchAll(re)){
      out += text.slice(last, m.index); for (let i = last; i < m.index; i++) m2.push(map[i]);
      const r = f(...m); out += r;
      for (let k = 0; k < r.length; k++) m2.push(map[m.index + Math.min(k, m[0].length - 1)]);
      last = m.index + m[0].length;
    }
    out += text.slice(last); for (let i = last; i < text.length; i++) m2.push(map[i]);
    text = out; map = m2;
  }
  map.push(s.length);
  return {text, map};
}
export const normText = s => normMapped(s).text;

const FRAC = {"½": 0.5, "¼": 0.25, "¾": 0.75};
export const AMT = "\\d+\\s*\\/\\s*\\d+|\\d+(?:\\.\\d+)?|[½¼¾]";
const amount = a => { if (a == null) return 1; a = a.trim().toLowerCase(); if (FRAC[a]) return FRAC[a];
  if (/^half/.test(a)) return 0.5; const f = a.match(/^(\d+)\s*\/\s*(\d+)$/); if (f) return +f[1] / +f[2];
  return /^\d/.test(a) ? +a : 1; };
const DOSE_UNITS = {drop: "drops?|gtts?|gts", tsp: "teaspoons?|tsp\\b", tbsp: "tablespoons?|tbsp\\b", tablet: "tablets?"};
export const DOSE = new RegExp(`(?<![\\d.,/])(${AMT})(?:\\s*(?:-|–|to|or)\\s*(${AMT}))?\\s*(${Object.values(DOSE_UNITS).join("|")})`, "gi");
const VOLS = [["quarts?|qts?\\b", 946], ["lit(?:re|er)s?|ltrs?\\b", 1000], ["gallons?|gal\\b", 3785], ["pints?", 473], ["canteens?", 946]];
const VOL = new RegExp(`(?:(${AMT}|half(?: an?)?|an?|each|every|per)\\s*(?:-\\s*)?)?\\b(${VOLS.map(v => v[0]).join("|")})`, "gi");
const doseUnit = u => Object.keys(DOSE_UNITS).find(k => new RegExp(`^(?:${DOSE_UNITS[k]})$`, "i").test(u));
const volMl = u => VOLS.find(([p]) => new RegExp(`^(?:${p})$`, "i").test(u))[1];
const STATES = [["clear", /\bclear\b/i], ["cloudy", /\b(cloudy|murky|turbid|muddy|dirty)\b/i], ["cold", /\b(cold|very cold)\b(?!\s*(pack|compress))/i]];
const statesIn = s => new Set(STATES.filter(([, r]) => r.test(s)).map(([k]) => k));
const agentIn = s => { const b = /\b(bleach|chlorine|hypochlorite)\b/i.test(s), i = /\biodine\b/i.test(s); return b && !i ? "bleach" : i && !b ? "iodine" : null; };
const PCT = /(\d+(?:\.\d+)?)\s*%?\s*(?:-|–|to)\s*(\d+(?:\.\d+)?)\s*(?:%|per ?cent)|(\d+(?:\.\d+)?)\s*(?:%|per ?cent)/i;
const pctIn = s => { const m = PCT.exec(s); return !m ? null : m[3] ? [+m[3], +m[3]] : [+m[1], +m[2]]; };

// Units of text, with their offsets: wrapped prose lines are joined and split into sentences; table rows, table
// headers, list items and markdown table lines stay units of their own (pdftotext and the index keep one table row
// per line). Joining a line break as a space keeps every offset.
const LIST_LINE = /^\s*([-*•]|\d+[.)])\s|\|/;
const isTabular = l => !/[.!?;]\s*$/.test(l.trim()) && l.trim().length < 100 && (
  [...l.matchAll(DOSE)].length >= 2 || new RegExp(`^\\s*(?:${AMT})\\s*(?:%|${VOLS.map(v => v[0]).join("|")})`, "i").test(l) ||
  ([...l.matchAll(/\b(clear|cloudy|cold)\b/gi)].length >= 2 && ![...l.matchAll(DOSE)].length));
function units(para, base){
  const out = []; let buf = null;
  const flush = () => { if (!buf) return; const t = buf.t, re = /(?<=[.!?;])\s+(?=\S)/g; let s = 0, m;
    while ((m = re.exec(t))){ out.push({t: t.slice(s, m.index), off: buf.off + s, tab: false}); s = m.index + m[0].length; }
    out.push({t: t.slice(s), off: buf.off + s, tab: false}); buf = null; };
  let off = base;
  for (const line of para.split("\n")){
    const here = off; off += line.length + 1;
    if (!line.trim()){ flush(); continue; }
    if (isTabular(line) || LIST_LINE.test(line)){ flush(); out.push({t: line, off: here, tab: isTabular(line)}); }
    else if (buf) buf.t += " " + line; else buf = {t: line, off: here};
  }
  flush(); return out;
}
function paragraphs(text){
  const out = [], re = /\n\s*\n/g; let s = 0, m;
  while ((m = re.exec(text))){ out.push([text.slice(s, m.index), s]); s = m.index + m[0].length; }
  out.push([text.slice(s), s]); return out;
}

// Every dose in text as [{amount, unit, ml, states, pct, agent, label, span}]. span: [start, end) of the dose (with
// its volume when that follows it) in the text as given. derive: read it as a source (a note under a table sets the
// volume of the rows above it; "double the amount" for cloudy water adds the doubled rows).
export function doseFacts(text, {derive = false} = {}){
  const {text: norm, map} = normMapped(text), facts = [];
  for (const [para, pOff] of paragraphs(norm)){
    let agent = null, header = null, defaultMl = null, lastMl = null; const doubles = [], pctBy = {}, pf = [];
    for (const {t: sen, off, tab} of units(para, pOff)){
      DOSE.lastIndex = 0; VOL.lastIndex = 0;
      const ds = [...sen.matchAll(DOSE)];
      const st = [...sen.matchAll(/\b(clear|cloudy|cold)\b/gi)].map(m => m[1].toLowerCase());
      if (tab && st.length >= 2 && !ds.length){ header = st; continue; }                 // CLEAR WATER  CLOUDY WATER  COLD WATER
      if (!tab) header = null;
      const a = agentIn(sen); if (a) agent = a;
      const p = pctIn(sen); if (p && agent && !ds.length) pctBy[agent] = p;
      if (p && agent && ds.length === 1) pctBy[agent] = p;
      const vs = [...sen.matchAll(VOL)].map(m => ({i: m.index, end: m.index + m[0].length, ml: amount(m[1]) * volMl(m[2]), t: m[0]}));
      if (!ds.length){
        if (derive && /\b(figures|amounts|values|quantities|doses)\b/i.test(sen) && vs.length) defaultMl = vs[0].ml;   // "All figures are based on one quart"
        if (derive && /\bdouble\b/i.test(sen) && /\b(cloudy|murky|turbid)\b/i.test(sen)) doubles.push(agent);
        continue;
      }
      // Each dose owns the part of the sentence up to the last , ; ( or "or" / "and" before the next dose.
      const cut = ds.map((d, k) => { if (k === ds.length - 1) return sen.length;
        const from = d.index + d[0].length, gap = sen.slice(from, ds[k + 1].index);
        const ms = [...gap.matchAll(/[,;(]|\s(?:or|and|but)\s|\s-\s/g)]; return ms.length ? from + ms.at(-1).index : ds[k + 1].index; });
      let prevStates = null;
      // A table laid out volume first, also when flattened to one line ("1 quart/1 liter 5 drops 1/2 gallon/2 quarts
      // 10 drops", PDFs added in the browser): an amount right after a volume (only spaces, "/" or ":" between) is that
      // row's, never the next row's. An amount-first table ("8 drops 1 gallon 1/2 teaspoon 5 gallons") pairs forward.
      const volFirst = vs.length && vs[0].i < ds[0].index;
      ds.forEach((d, k) => {
        const s0 = k ? cut[k - 1] : 0, s1 = cut[k], seg = sen.slice(s0, s1), dEnd = d.index + d[0].length;
        const prevEnd = k ? ds[k - 1].index + ds[k - 1][0].length : 0;
        const row = volFirst && vs.filter(v => v.i >= prevEnd && v.end <= d.index && /^[\s\/:]{0,3}$/.test(sen.slice(v.end, d.index))).at(-1);
        let v = row ? null : vs.find(v => v.i >= dEnd && v.end <= s1 && v.i - dEnd <= 60), label, end = dEnd;
        if (row){ v = row; label = `${d[0]} (${v.t})`; }
        else if (v){ label = sen.slice(d.index, v.end); end = v.end; }
        else { v = vs.filter(v => v.i >= s0 && v.end <= d.index).at(-1); label = v ? `${d[0]} (${v.t})` : d[0]; }
        let states = header && ds.length >= 2 && header[k] ? new Set([header[k]]) : statesIn(seg);
        // answer: "For cloudy or cold water add 20 drops per gallon (or 10 drops per quart)": the water applies to both
        if (!derive && !states.size && prevStates) states = prevStates;
        prevStates = states;
        const ag = agentIn(seg) || agent, pct = pctIn(seg) || (tab && pctIn(sen)) || (ag && pctBy[ag]) || null;
        let ml = v ? v.ml : null;
        if (ml == null && !derive) ml = lastMl;                                    // answer: "5 drops per quart. If cloudy, use 10 drops"
        if (v) lastMl = v.ml;
        const span = [map[off + d.index], map[off + end - 1] + 1];
        for (const am of [d[1], d[2]].filter(Boolean))
          pf.push({amount: amount(am), unit: doseUnit(d[3]), ml, states, pct, agent: ag, label: label.trim(), span});
      });
    }
    for (const f of pf) if (f.ml == null && defaultMl != null) f.ml = defaultMl;
    for (const ag of doubles) for (const f of pf.slice())
      if ((!f.states.size || f.states.has("clear")) && (!ag || !f.agent || f.agent === ag))
        pf.push({...f, amount: f.amount * 2, states: new Set(["cloudy"]), label: f.label + " doubled for cloudy water"});
    facts.push(...pf);
  }
  return facts;
}
const sameMl = (a, b) => a / b > 0.85 && a / b < 1.18;              // 1 quart ~ 1 litre, 1/2 gallon ~ 2 litres
export function doseGrounded(claim, facts){
  const cs = claim.states.size ? claim.states : new Set(["clear"]);
  return facts.some(f => Math.abs(f.amount - claim.amount) < 1e-6 && f.unit === claim.unit
    && (claim.ml == null || (f.ml != null && sameMl(claim.ml, f.ml)))
    && [...cs].every(s => (f.states.size ? f.states : new Set(["clear"])).has(s))
    && (!claim.pct || !f.pct || (claim.pct[0] <= f.pct[1] && claim.pct[1] >= f.pct[0]))
    && (!claim.agent || !f.agent || claim.agent === f.agent));
}
// sources: one text or several (each read on its own, so one source's bleach strength never applies to another's rows).
export const sourceFacts = sources => (Array.isArray(sources) ? sources : [sources]).flatMap(s => doseFacts(s, {derive: true}));
// The doses in answer that no source fact grounds.
export const ungroundedDoses = (answer, sources) => { const facts = sourceFacts(sources); return doseFacts(answer).filter(c => !doseGrounded(c, facts)); };
export const describeDose = c => c.label + ([...c.states].length ? ` [${[...c.states].join(", ")} water]` : "") +
  (c.pct ? ` [${c.pct[0] === c.pct[1] ? c.pct[0] : c.pct.join("-")}%]` : "");

// Grounded AND it states every condition its source fact attaches (volume, water, strength, agent): "2 drops per quart
// of clear 4-6% bleach" for the USMC row. "10 drops per quart" is grounded by USMC's 1% row but leaves out the 1%, so
// it is not stated in full (and the page's figure check then decides, which fails safe).
export function doseInFull(claim, facts){
  return facts.some(f => doseGrounded(claim, [f]) && (f.ml == null || claim.ml != null) && (!f.pct || claim.pct) &&
    (!f.agent || claim.agent) && (!f.states.size || claim.states.size));
}
// The page's dose guard: the doses in text, as spans [start, end) of the text as given. bad: no source grounds it (with its
// volume, water, strength and agent); full: a source gives it with every condition the answer states and no more.
// -> {bad: [[a, b]], full: [[a, b]], doses: [description of each bad one]}
export function checkDoses(text, sources){
  const facts = sourceFacts(sources), claims = doseFacts(String(text)), bad = claims.filter(c => !doseGrounded(c, facts));
  return {bad: bad.map(c => c.span), full: claims.filter(c => !bad.includes(c) && doseInFull(c, facts)).map(c => c.span),
          doses: [...new Set(bad.map(describeDose))]};
}
// Spans, merged where they overlap, in order.
export function mergeSpans(spans){
  const out = [];
  for (const [a, b] of [...spans].sort((x, y) => x[0] - y[0])){ const last = out.at(-1);
    if (last && a <= last[1]) last[1] = Math.max(last[1], b); else out.push([a, b]); }
  return out;
}
// Each span of text becomes the marker "\u0001". -> {text, removed}
export function markSpans(text, spans){
  spans = mergeSpans(spans); let out = "", at = 0;
  for (const [a, b] of spans){ out += text.slice(at, a) + "\u0001"; at = b; }
  return {text: out + text.slice(at), removed: spans.length};
}
export function guardDoses(text, sources){
  text = String(text); const c = checkDoses(text, sources);
  return {...markSpans(text, c.bad), doses: c.doses};
}
