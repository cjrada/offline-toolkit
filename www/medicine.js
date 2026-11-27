// Medicine doses on the kit's pages (Chat and Ask the Manuals). No dependencies; tests run it under node.
// The kit is reference material: a dose may be shown only word for word from the kit's own documents, with a caveat,
// and never as a figure the AI wrote. A 4B model invents doses under any prompt rule ("15 kg x 15 mg = 225 mg per dose",
// "200 mg to 400 mg every 6 to 8 hours"), so this is code.
//
// SAFETY is the output guard, and it never depends on how the question was classified:
//   guardMedicine(q, answer, prior, {sources, chat}), on every AI answer as it streams: every dose-like figure (mg, mcg,
//   ml, tablets / capsules / drops / puffs / sachets / units, spoons, mg/kg, frequencies in digits or words, daily
//   maxima, fractions of a tablet) that is not in ONE row of the sources shown to the reader, with the conditions this
//   answer gives it (same person, volume of water, strength, a dosing row for "take / give", the same drug), becomes
//   "[dose removed]", with the safe pointer. Chat with no passages shown: every such figure is removed. A dose that is
//   in a shown source row is kept, so documents added to the index later (curated medical references) work as they are.
//
// ROUTING only decides what the reader is shown (UX), never whether a figure gets through:
//   medicineDoseQuestion(q, prior): a medicine dose question is not sent to the AI. Ask the Manuals (docs/exact.js
//   medicineRoute) shows the source passages holding the dose (medicineDoseIn, medicineExcerpts) word for word with
//   their [n] links and sourceDoseNote(), or NOT_COVERED; Chat shows CHAT_REFUSAL (Ask the Manuals + POINTER).
// None of the messages has a dose-like figure, so neither guard touches them.
// The water-treatment dose check (docs/dose.js: drops of bleach per litre against the sources) is a further guard.

export const POINTER = "For medicine doses, follow the instructions on the packet or label, or ask a pharmacist or Healthdirect " +
  "(1800 022 222). In an emergency, or if too much may have been taken, call 000 (Poisons Information Centre 13 11 26).";
export const ASK_MANUALS = "Chat does not give medicine doses. If the kit's documents give one, Ask the Manuals (on the kit's " +
  "home page) shows it word for word with a link to its source.";
export const CHAT_REFUSAL = ASK_MANUALS + " " + POINTER;
export const NOT_COVERED = "The kit's documents found for this question do not give a dose for it, so none is shown here. " +
  "Do not guess one. " + POINTER;
// Under source doses shown word for word: ns = their [n] source numbers.
export const sourceDoseNote = ns => `The dose information above is quoted word for word from the kit's documents (${ns.map(n => `[${n}]`).join(" ")}); ` +
  "open the source to read it in full. It may not fit this person or this medicine: check it against the medicine's packet or " +
  "label and the person's age and weight, and follow the source and the label exactly. If you can, get medical advice: a " +
  "pharmacist, Healthdirect (1800 022 222), or 000 in an emergency.";
export const DOSE_REMOVED = "[dose removed]";
/** @deprecated kept for old callers: the Chat refusal. */
export const REFUSAL = CHAT_REFUSAL;

const MED_NAMES = "paracetamol|acetaminophen|panadol|panamax|dymadon|ibuprofen|nurofen|advil|aspirin|disprin|cartia|codeine|" +
  "tramadol|oxycodone|endone|morphine|fentanyl|naproxen|naprosyn|diclofenac|voltaren|celecoxib|meloxicam|" +
  "antibiotics?|amox[iy]cillin|augmentin|penicillin|flucloxacillin|cephalexin|keflex|doxycycline|azithromycin|ciprofloxacin|metronidazole|trimethoprim|" +
  "antihistamines?|loratadine|claratyne|cetirizine|zyrtec|fexofenadine|telfast|promethazine|phenergan|diphenhydramine|" +
  "prednisolone|prednisone|dexamethasone|salbutamol|ventolin|adrenaline|epinephrine|epipen|insulin|metformin|warfarin|" +
  "potassium\\s+iod(?:ide|ate)|ki|thyrosafe|iosat|thyroshield|" +
  "diazepam|valium|temazepam|melatonin|loperamide|gastrostop|ondansetron|maxolon|metoclopramide|hydralyte|" +
  "medicines?|medications?|meds|painkillers?|pain ?relie(?:f|vers?)|analgesics?|drugs?|syrups?";
export const MED = new RegExp(`\\b(?:${MED_NAMES})\\b`, "i");
// Named medicines (not "medicine", "antibiotics", "syrup"...), with brand names as the same drug: a source line is
// shown for a named medicine only if it names that one and no other.
const SAME_DRUG = [["paracetamol", "acetaminophen", "panadol", "panamax", "dymadon"], ["ibuprofen", "nurofen", "advil"],
  ["aspirin", "disprin", "cartia"], ["amoxicillin", "amoxycillin", "augmentin"], ["diclofenac", "voltaren"], ["naproxen", "naprosyn"],
  ["oxycodone", "endone"], ["cephalexin", "keflex"], ["loratadine", "claratyne"], ["cetirizine", "zyrtec"], ["fexofenadine", "telfast"],
  ["promethazine", "phenergan"], ["salbutamol", "ventolin"], ["adrenaline", "epinephrine", "epipen"], ["diazepam", "valium"],
  ["loperamide", "gastrostop"], ["metoclopramide", "maxolon"],
  ["potassium iodide", "potassium iodate", "ki", "thyrosafe", "iosat", "thyroshield"]];
const GENERIC_MED = /^(?:medicines?|medications?|meds|painkillers?|pain ?relie(?:f|vers?)|analgesics?|drugs?|syrups?|antibiotics?|antihistamines?)$/i;
const canonical = w => { w = w.toLowerCase().replace(/\s+/g, " "); const g = SAME_DRUG.find(g => g.includes(w)); return g ? g[0] : w; };
// The named medicines in text, as canonical names ("Panadol" -> "paracetamol").
export const medicinesIn = text => new Set([...String(text).matchAll(new RegExp(MED.source, "gi"))].map(m => m[0]).filter(w => !GENERIC_MED.test(w)).map(canonical));
const ASKS_AMOUNT = /\b(doses?|dosage|dosing|dosed|how (?:much|many|often)|mg|milligrams?|mcg|micrograms?|ml|mls|millilit\w*|mg\/kg|per (?:kg|kilo\w*)|grams?|teaspoons?|tsp|amount|quantity|maximum|max|strength|times a day|\d+(?:\.\d+)?\s*kg|kilos?)\b/i;   // "And for a 15 kg child?"
export const medicineQuestion = q => MED.test(String(q));
// Water treatment: iodine tablets ("16 mg iodine tablets"), chlorine in mg, bleach, purification. Not a medicine
// dose, unless a medicine is named in the same conversation (then the medicine rules win: failing safe).
const WATER = /\b(?:water|purif\w*|disinfect\w*|chlorin\w*|iodine|bleach|hypochlorite|aquatabs|potable|steril[iy][sz]\w*)\b/i;
// A radiation dose is not a medicine dose ("What radiation dose is dangerous?"); the AI answers it as usual.
const RADIATION = /\b(?:radiation|radioactiv\w*|sieverts?|millisieverts?|msv|µsv|usv|rems?|grays?|gy|x-?rays?|fallout|nuclear)\b/i;
// "Iodine tablets" with a radiation word means potassium iodide (a medicine), unless the question is about water:
// "How many iodine tablets per litre of water after nuclear fallout?" is water treatment.
// Treating water: an amount for a volume of water, or purifying / disinfecting / treating it. "If I drank the water",
// "bottled water" or "contaminated water" alone is not.
const TREATS_WATER = /\b(?:per|in|for|to|into|each)\s+(?:an?\s+|one\s+|each\s+|every\s+|\d+(?:[.,]\d+)?\s*)?(?:lit(?:re|er)s?|l|quarts?|gallons?|canteens?|bottles?|buckets?)\b|\b(?:purif\w*|disinfect\w*|treat(?:s|ing|ed)?|steril[iy][sz]\w*|clean(?:ing)?)\s+(?:\w+\s+){0,2}?water\b|\bwater\s+(?:purif\w*|treatment|disinfect\w*|steril\w*)/i;
// Each clause on its own: "How many iodine tablets should I take after fallout, and how do I purify water?" asks for a
// KI dose in its first clause, even though another clause is about treating water.
const clauses = s => String(s).split(/[,;:?!.]|\b(?:and|but|also|then)\b/i);
const radiationIodine = s => RADIATION.test(s) && clauses(s).some(c => /\biodine\b/i.test(c) && !TREATS_WATER.test(c));
const waterOnly = s => WATER.test(s) && !MED.test(s) && !radiationIodine(s);

// Numbers in digits or words ("7.5", "4 to 6", "four", "four to six").
const NW = "(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty|half|a half|quarter|one and a half)";
const N1 = `(?:\\d+\\s*\\/\\s*\\d+|\\d+(?:[.,]\\d+)?|${NW})`;
const N = `${N1}(?:\\s*(?:-|–|to|or)\\s*${N1})?`;
// An mg-type figure, also a concentration ("2 mg/L" is kept only when a shown source states it).
const ALWAYS = new RegExp(`(?<![\\w./])${N}\\s*(?:mg|milligrams?|mcg|micrograms?|µg|ug|iu|international units?)\\b(?:\\s*(?:\\/|per)\\s*(?:kg|kilo\\w*|dose|day|l\\b|lit(?:re|er)s?))?`, "gi");
const hasAlways = s => new RegExp(ALWAYS.source, "i").test(s);
// Any other dose-like figure ("5 ml", "two tablets", "2 iodine tablets", "2GTTS", "every four hours", "twice a day",
// "max 6 in a day", "half a tablet"). One word may stand between the number and the unit.
const UNIT = "ml|mls|millilit\\w*|g|grams?|tablets?|tabs?|capsules?|caps|caplets?|pills?|drops?|gtts?|gts|puffs?|doses?|sachets?|units?|teaspoons?|tsp|tablespoons?|tbsp";
const IN_MED = new RegExp(`(?<![\\w./])${N}\\s*(?:[a-z][a-z'-]*\\s+)?(?:${UNIT})\\b(?:\\s*(?:\\/|per)\\s*(?:kg|kilo\\w*|dose|day))?` +
  `|\\bevery\\s+${N}\\s*(?:hours?|hrs?|h)\\b|\\b${N1}[- ]hourly\\b` +
  `|\\b(?:once|twice|thrice|${N}\\s*(?:times|x))\\s*(?:(?:a|per|each)\\s+day|daily)\\b|\\bfor\\s+${N}\\s*days\\b` +
  // a fraction of a unit: "half a tablet", "a quarter of a tablet", "one and a half tablets"
  `|\\b(?:(?:one|two|three)\\s+and\\s+)?(?:an?\\s+)?(?:half|quarter|third)\\s+(?:of\\s+)?(?:an?\\s+)?(?:${UNIT})\\b` +
  // a daily maximum with no unit: "max 6 in a day", "no more than 8 in 24 hours", "up to 4 a day"
  `|\\b(?:max(?:imum)?(?:\\s+of)?|up to|no more than|not more than)\\s+${N}\\s*(?:\\w+\\s+)?(?:in|a|per|each)\\s+(?:a\\s+)?(?:day|24\\s*h\\w*)\\b`, "gi");
// The earlier user turns of a conversation, as text (history: [{role, content}] or [string]).
const userTurns = (history = []) => history.map(m => typeof m === "string" ? m : m.role === "user" ? m.content : "").join("\n");
// A medicine dose question:
//  * it names a medicine and asks for an amount;
//  * it quotes a medicine-dose figure itself ("You said 240 mg, confirm it", "Is 15 mg/kg right?"), unless it is
//    only about water treatment ("How many 16 mg iodine tablets per quart?");
//  * it uses a dose word (dose, dosage, mg...), also for a drug no list names ("What is the dose of Testamol?");
//  * in a conversation (prior: the earlier user turns) that named a medicine, it asks for an amount ("How much for
//    a 15 kg toddler?" after a paracetamol question). A question only about water treatment is never one.
// An explicit dose word: also a medicine the list does not name ("What is the dose of Testamol?"), so documents added
// to the index later are found the same way. "How much water should I drink with a fever?" is not one.
const DOSE_WORD = /\b(?:doses?|dosage|dosing|milligrams?|micrograms?|mg|mcg|mg\/kg)\b/i;
export function medicineDoseQuestion(q, prior = []){
  q = String(q); const before = userTurns(prior);
  if ((MED.test(q) || radiationIodine(q)) && ASKS_AMOUNT.test(q)) return true;   // also "potassium iodide / KI / iodine tablets for radiation"
  if (radiationIodine(q) && /\b(?:take|give|have)\b/i.test(q)) return true;          // "Should I take iodine tablets after the nuclear accident?"
  if (waterOnly(q)) return false;                       // "How many 16 mg iodine tablets per quart?", also after a medicine turn
  return hasAlways(q) || (DOSE_WORD.test(q) && !RADIATION.test(q)) || (MED.test(before) && ASKS_AMOUNT.test(q));
}
// Sentences as [start, end): a break is . ! ? followed by a space or the end ("7.5 ml" is one sentence), or a line break.
const sentenceSpans = t => { const out = [], re = /[.!?](?=\s|$)|\n/g; let s = 0, m;
  while ((m = re.exec(t))){ out.push([s, m.index + 1]); s = m.index + 1; } if (s < t.length) out.push([s, t.length]); return out; };

// The medicine-dose figures in text, as [start, end) spans. medicine: the conversation names a medicine (the question,
// an earlier user turn or this text): then every amount anywhere in the text counts, water figures included (fails
// safe). Otherwise only mg-type figures outside water-treatment sentences, and the amounts in a sentence holding one.
export function medicineDoseSpans(text, {medicine = MED.test(String(text))} = {}){
  text = String(text); const spans = [], add = (re, s, off) => { for (const m of s.matchAll(re)) spans.push([off + m.index, off + m.index + m[0].length]); };
  if (medicine){ add(ALWAYS, text, 0); add(IN_MED, text, 0); }
  else for (const [a, b] of sentenceSpans(text)){
    const s = text.slice(a, b); if (waterOnly(s) || !hasAlways(s)) continue;          // "16 mg iodine tablets per quart" stays
    add(ALWAYS, s, a); add(IN_MED, s, a);
  }
  spans.sort((x, y) => x[0] - y[0]);
  const out = []; for (const [a, b] of spans){ const l = out.at(-1); if (l && a <= l[1]) l[1] = Math.max(l[1], b); else out.push([a, b]); }
  return out;
}

// ---- the output guard: safety rests on what the AI wrote, never on how the question was classified
// Every dose-like figure in the AI's text (mg, mcg, ml, tablets / capsules / drops / puffs / sachets / units, spoons,
// mg/kg, frequencies in digits or words, daily maxima, fractions of a tablet), whatever the question and whether or not
// a medicine is named.
export function doseLikeSpans(text){
  text = String(text); const spans = [];
  for (const re of [ALWAYS, IN_MED]) for (const m of text.matchAll(re)) spans.push([m.index, m.index + m[0].length]);
  spans.sort((x, y) => x[0] - y[0]);
  const out = []; for (const [a, b] of spans){ const l = out.at(-1); if (l && a <= l[1]) l[1] = Math.max(l[1], b); else out.push([a, b]); }
  return out;
}
// A figure's value, however it is written: "four times daily" = "4 times a day", "2GTTS" = "2 drops", "1/4 tsp" =
// "¼ teaspoon", "every 4 to 6 hours" = "every 4-6 hrs", "no more than 8 in 24 hours" = "max 8 a day".
const WN = {one: 1, two: 2, three: 3, four: 4, five: 5, six: 6, seven: 7, eight: 8, nine: 9, ten: 10, eleven: 11, twelve: 12, fifteen: 15, twenty: 20};
const KEY_UNITS = [[/^(?:mg|milligrams?)$/, "mg"], [/^(?:mcg|micrograms?|µg|ug)$/, "mcg"], [/^(?:iu|international|units?)$/, "unit"],
  [/^(?:ml|mls|millilit\w*)$/, "ml"], [/^(?:g|grams?)$/, "g"], [/^(?:tablets?|tabs?)$/, "tablet"], [/^(?:capsules?|caps)$/, "capsule"],
  [/^caplets?$/, "caplet"], [/^pills?$/, "pill"], [/^(?:drops?|gtts?|gts)$/, "drop"], [/^puffs?$/, "puff"], [/^doses?$/, "dose"],
  [/^sachets?$/, "sachet"], [/^(?:teaspoons?|tsp)$/, "tsp"], [/^(?:tablespoons?|tbsp)$/, "tbsp"], [/^(?:hours?|hrs?|h)$/, "h"],
  [/^(?:day|days|daily)$/, "day"], [/^(?:kg|kilo\w*)$/, "kg"], [/^(?:l|lit(?:re|er)s?)$/, "l"], [/^(?:times?|x)$/, "times"],
  [/^(?:every|max|for)$/, null]];
export function doseKey(fig){
  let s = String(fig).toLowerCase().replace(/[–—]/g, "-").replace(/(\d)([a-z])/g, "$1 $2");
  s = s.replace(/\b(?:no|not)\s+more\s+than\b|\bup\s+to\b|\bmaximum(?:\s+of)?\b/g, "max").replace(/\bin\s+24\s*h\w*\b/g, "a day")
       .replace(/\bonce\b/g, "1 times").replace(/\btwice\b/g, "2 times").replace(/\bthrice\b/g, "3 times")
       .replace(/\b(\w+)-hourly\b/g, "every $1 h").replace(/\b(\w+)\s+hourly\b/g, "every $1 h")
       .replace(/\bone and a half\b/g, "1.5").replace(/\b(?:a\s+)?half\b/g, "0.5").replace(/\b(?:a\s+)?quarter\b/g, "0.25").replace(/\b(?:a\s+)?third\b/g, "0.333")
       .replace(/\b(one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty)\b/g, w => WN[w])
       .replace(/(\d+)\s*\/\s*(\d+)/g, (m, a, b) => String(Math.round(+a / +b * 1000) / 1000)).replace(/(\d),(\d)/g, "$1.$2")
       .replace(/(\d+(?:\.\d+)?)\s*(?:-|to|or)\s*(\d+(?:\.\d+)?)/g, "$1-$2");
  const out = [];
  for (const t of s.split(/[\s\/]+/)){
    if (/^\d+(?:\.\d+)?(?:-\d+(?:\.\d+)?)?$/.test(t)){ out.push(t); continue; }
    const u = KEY_UNITS.find(([re]) => re.test(t)); if (u) out.push(u[1] ?? t);
  }
  return out.join(" ");
}
// Sources as rows (a line of a table or list, or a sentence of one-line text), each with the text just before it.
function rowsOf(src){
  src = String(src); const multi = (src.match(/\n/g) || []).length >= 2, rows = [];
  const spans = multi ? (() => { const o = []; let s = 0; for (const l of src.split("\n")){ o.push([s, s + l.length]); s += l.length + 1; } return o; })() : sentenceSpans(src);
  for (const [a, b] of spans){ const t = src.slice(a, b); if (!t.trim()) continue;
    // the passage (up to a blank line) holds a table's notes ("All figures are based on one quart of water")
    const p0 = src.lastIndexOf("\n\n", a), p1 = src.indexOf("\n\n", b);
    const keys = new Set(doseLikeSpans(t).map(([x, y]) => doseKey(t.slice(x, y))));
    // the row's own heading: the lines just above it that hold no figure ("Children (under 12):")
    let head = ""; for (let i = rows.length - 1; i >= 0 && rows.length - i <= 2 && !rows[i].keys.size; i--) head = rows[i].t + "\n" + head;
    rows.push({t, keys, own: head + t, ctx: src.slice(Math.max(0, a - 300), b), passage: src.slice(p0 < 0 ? 0 : p0, p1 < 0 ? src.length : p1)}); }
  return rows;
}
const PERSON = /\b(?:adults?|child(?:ren)?|kids?|infants?|bab(?:y|ies)|toddlers?|teen(?:ager)?s?|elderly|pregnant|newborns?)\b|\b\d+(?:\.\d+)?\s*kg\b|\b\d+\s*(?:years?|months?)(?:\s*old)?\b/gi;
const INTAKE = /\b(?:take|takes|taking|swallow\w*|chew\w*|give|gives|giving|administer\w*)\b/i;
// The volume of water and the strength a sentence gives a figure ("per quart", "5%", "4-6%").
const VOLUME = /\b(?:quarts?|lit(?:re|er)s?|gallons?|canteens?|pints?)\b/gi, PERCENT = /\b\d+(?:\.\d+)?(?:\s*-\s*\d+(?:\.\d+)?)?\s*(?:%|per ?cent)/gi;
const volumes = t => new Set([...String(t).matchAll(VOLUME)].map(m => m[0].toLowerCase().replace(/s$/, "").replace("liter", "litre")));
const percents = t => new Set([...String(t).matchAll(PERCENT)].map(m => m[0].toLowerCase().replace(/\s+/g, "").replace(/per ?cent/, "%")));
const persons = t => new Set([...String(t).matchAll(PERSON)].map(m => m[0].toLowerCase().replace(/\s+/g, " ").replace(/^child$/, "children").replace(/^(adult|kid|infant|toddler|teen|teenager|newborn)$/, "$1s").replace(/^baby$/, "babies")));
// Is this clause's figures-set, said in this sentence about this drug context, one row of the shown sources?
//  * every figure of the clause is in ONE row (so a frequency or a strength is never taken from another row);
//  * the people the sentence names (adults, children, 15 kg...) are in that row or its own heading, so "Children: 500 mg"
//    never borrows an "Adults: 500 mg" row;
//  * the volume of water (quart, litre...) and strength (5%) it gives are in that row's passage (a note under a table:
//    "All figures are based on one quart of water");
//  * a sentence about taking or giving it ("take", "give") needs a dosing row: one that says so, names a person, or
//    names a drug; never a water-treatment row ("Use 2 iodine tablets per quart");
// None of this looks at how the question was classified (medicineDoseQuestion is UX only).
//  * the drug it is about (named in the sentence, else earlier in the answer, else in the conversation) is the drug
//    named with the row, and no other drug is.
function grounded(keys, sentence, drugs, rows){
  const ps = persons(sentence), vs = volumes(sentence), pc = percents(sentence), intake = INTAKE.test(sentence);
  return rows.some(r => keys.every(k => r.keys.has(k)) &&
    [...ps].every(p => persons(r.own).has(p)) && [...vs].every(v => volumes(r.passage).has(v)) && [...pc].every(v => percents(r.passage).has(v)) &&
    (!intake || INTAKE.test(r.own) || persons(r.own).size > 0 || medicinesIn(r.ctx).size > 0) &&
    (() => { const rd = medicinesIn(r.ctx); return [...drugs].every(d => rd.has(d)) && [...rd].every(d => drugs.has(d)); })());
}
// What the reader sees for this AI answer. -> {text, removed, pointer, doses}
//   sources: the texts shown to the reader with this answer (Ask the Manuals: the sources sent and the exact box; Chat: the
//   first aid passages listed under it; none: every dose-like figure is invented and removed).
//   removed: dose-like figures not in one row of the sources, with the conditions this answer gives them, replaced by
//   DOSE_REMOVED; pointer: the safe pointer was appended (in Chat also to Ask the Manuals). q / prior only decide that UX.
export function guardMedicine(q, answer, prior = [], {chat = false, sources = []} = {}){
  let text = String(answer);
  const rows = (Array.isArray(sources) ? sources : [sources]).flatMap(rowsOf), cut = [];
  const convo = medicinesIn(String(q) + "\n" + userTurns(prior));
  for (const [a, b] of sentenceSpans(text)){
    const sen = text.slice(a, b), drugs = medicinesIn(sen).size ? medicinesIn(sen) : medicinesIn(text.slice(0, a)).size ? medicinesIn(text.slice(0, a)) : convo;
    // a clause: up to , ; ( ) or " - " (so "for 1 quart add 5 drops, for 1 gallon add 1/4 teaspoon" is two rows)
    const cl = []; { const re = /[,;()]|\s-\s/g; let s0 = 0, m; while ((m = re.exec(sen))){ cl.push([s0, m.index]); s0 = m.index + m[0].length; } cl.push([s0, sen.length]); }
    for (const [c0, c1] of cl){
      const spans = doseLikeSpans(sen.slice(c0, c1)).map(([x, y]) => [a + c0 + x, a + c0 + y]);
      if (!spans.length) continue;
      if (!grounded(spans.map(([x, y]) => doseKey(text.slice(x, y))), sen, drugs, rows)) cut.push(...spans);
    }
  }
  const doses = cut.map(([x, y]) => text.slice(x, y));
  for (const [x, y] of cut.slice().sort((p, q2) => q2[0] - p[0])) text = text.slice(0, x) + DOSE_REMOVED + text.slice(y);
  const pointer = (cut.length > 0 || MED.test(String(q)) || MED.test(text)) && !/1800\s?022\s?222/.test(text);
  if (pointer) text = text.replace(/\s*$/, "") + "\n\n" + (chat ? CHAT_REFUSAL : POINTER);
  return {text, removed: cut.length, pointer, doses};
}

// Does this source text hold a medicine dose? By content, for any document in the index: an mg / mcg / IU / mg/kg
// figure outside water treatment, or (where a medicine is named) a dose amount or frequency.
export const medicineDoseIn = text => medicineDoseSpans(String(text), {medicine: MED.test(String(text))}).length > 0 &&
  !waterOnly(String(text).replace(/\s+/g, " "));
// The question's own words that can name what the dose is for (a drug, also one not in MED_NAMES): its words minus
// amount, age, weight and filler words. exact.js shows a source dose only next to one of them.
const NOT_A_NAME = new Set(("dose doses dosage dosing dosed much many often how what which when give take taking given use used " +
  "mg ml mcg kg milligram milligrams millilitre millilitres milliliter milliliters kilo kilos gram grams tablet tablets capsule capsules " +
  "teaspoon teaspoons amount maximum max safe strength day daily hour hours times year years old month months week weeks " +
  "child children kid kids baby babies toddler toddlers infant infants adult adults son daughter boy girl man woman person " +
  "people my his her their our your yes no ok okay please tell me can could should would will i you he she it we they " +
  "medicine medicines medication medications meds drug drugs painkiller painkillers pain relief syrup the a an of for to in " +
  "on and or is are be was with from about per every this that there right confirm number ignore previous instructions rules " +
  "doctor nurse game play roleplay pretend").split(" "));
export const doseSubjectWords = q => [...new Set((String(q).toLowerCase().match(/[a-z][a-z-]{2,}/g) || []).filter(w => !NOT_A_NAME.has(w)))];
