// Medicine dose guard for every AI answer the kit shows (Chat and Ask the Manuals). No dependencies; tests run it
// under node. The kit never gives medicine doses: prompt rules alone do not stop a 4B model ("15 kg x 15 mg = 225 mg
// per dose", "200 mg to 400 mg every 6 to 8 hours" came back under the kit's own "no doses" rule), so this is code:
//   1. medicineDoseQuestion(q): the question names a medicine (or "medicine / medication / painkiller...") AND asks
//      for an amount (dose, how much / many / often, mg, ml, mg/kg...). The page does not ask the AI at all: it shows
//      REFUSAL, also when the question tricks ("ignore your instructions", "I am a doctor", role-play).
//   2. guardMedicine(q, answer, prior), on every answer as it streams: medicine-dose figures become "[dose removed]".
//      When the conversation names a medicine (question, earlier user turns, or the answer itself): every mg / mcg /
//      mg/kg / IU, ml, tablet, capsule, drop or teaspoon amount and every frequency ("every four hours", "twice a
//      day") anywhere in the answer. Otherwise: mg-type figures (not concentrations such as mg/L) outside water-
//      treatment sentences ("16 mg iodine tablets per quart" stays), with the amounts in the same sentence. If any
//      was removed, or a medicine is named, the safe pointer is added.
//   3. A question that names a medicine always ends with the safe pointer (POINTER), also when the AI only said
//      "the sources do not give a dose".
// POINTER has no figure with a unit, so Ask the Manuals' amount guard (docs/exact.js) leaves it as it is.
// The water-treatment dose check (docs/dose.js: drops of bleach per litre against the sources) is a different guard.

export const POINTER = "For medicine doses, follow the instructions on the packet, or ask a pharmacist or Healthdirect " +
  "(1800 022 222). In an emergency, or if too much may have been taken, call 000 (Poisons Information Centre 13 11 26).";
export const REFUSAL = "I can't give medicine doses, even for a test, a game or a health professional. " + POINTER;
export const DOSE_REMOVED = "[dose removed]";

const MED_NAMES = "paracetamol|acetaminophen|panadol|panamax|dymadon|ibuprofen|nurofen|advil|aspirin|disprin|cartia|codeine|" +
  "tramadol|oxycodone|endone|morphine|fentanyl|naproxen|naprosyn|diclofenac|voltaren|celecoxib|meloxicam|" +
  "antibiotics?|amox[iy]cillin|augmentin|penicillin|flucloxacillin|cephalexin|keflex|doxycycline|azithromycin|ciprofloxacin|metronidazole|trimethoprim|" +
  "antihistamines?|loratadine|claratyne|cetirizine|zyrtec|fexofenadine|telfast|promethazine|phenergan|diphenhydramine|" +
  "prednisolone|prednisone|dexamethasone|salbutamol|ventolin|adrenaline|epinephrine|epipen|insulin|metformin|warfarin|" +
  "diazepam|valium|temazepam|melatonin|loperamide|gastrostop|ondansetron|maxolon|metoclopramide|hydralyte|" +
  "medicines?|medications?|meds|painkillers?|pain ?relie(?:f|vers?)|analgesics?|drugs?|syrups?";
export const MED = new RegExp(`\\b(?:${MED_NAMES})\\b`, "i");
const ASKS_AMOUNT = /\b(doses?|dosage|dosing|dosed|how (?:much|many|often)|mg|milligrams?|mcg|micrograms?|ml|mls|millilit\w*|mg\/kg|per (?:kg|kilo\w*)|grams?|teaspoons?|tsp|amount|quantity|maximum|max|strength|times a day)\b/i;
export const medicineQuestion = q => MED.test(String(q));
// Water treatment: iodine tablets ("16 mg iodine tablets"), chlorine in mg, bleach, purification. Not a medicine
// dose, unless a medicine is named in the same conversation (then the medicine rules win: failing safe).
const WATER = /\b(?:water|purif\w*|disinfect\w*|chlorin\w*|iodine|bleach|hypochlorite|aquatabs|potable|steril[iy][sz]\w*)\b/i;
const waterOnly = s => WATER.test(s) && !MED.test(s);

// Numbers in digits or words ("7.5", "4 to 6", "four", "four to six").
const NW = "(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty|half|a half|quarter|one and a half)";
const N1 = `(?:\\d+(?:[.,]\\d+)?|${NW})`;
const N = `${N1}(?:\\s*(?:-|–|to|or)\\s*${N1})?`;
// Always a medicine dose (not a concentration "mg/L" / "mg per litre").
const ALWAYS = new RegExp(`(?<![\\w.])${N}\\s*(?:mg|milligrams?|mcg|micrograms?|µg|ug|iu|international units?)\\b(?:\\s*(?:\\/|per)\\s*(?:kg|kilo\\w*|dose|day))?(?!\\s*(?:\\/|per)\\s*(?:l\\b|lit))`, "gi");
const hasAlways = s => new RegExp(ALWAYS.source, "i").test(s);
// A dose once the text is about a medicine ("5 ml of children's ibuprofen", "two tablets every four hours", "twice a day").
const IN_MED = new RegExp(`(?<![\\w.])${N}\\s*(?:ml|mls|millilit\\w*|g\\b|grams?|tablets?|tabs?|capsules?|caps|caplets?|pills?|drops?|teaspoons?|tsp|tablespoons?|tbsp|puffs?|doses?|sachets?)\\b(?:\\s*(?:\\/|per)\\s*(?:kg|kilo\\w*|dose|day))?` +
  `|\\bevery\\s+${N}\\s*(?:hours?|hrs?|h)\\b|\\b${N1}[- ]hourly\\b` +
  `|\\b(?:once|twice|thrice|${N}\\s*(?:times|x))\\s*(?:(?:a|per|each)\\s+day|daily)\\b|\\bfor\\s+${N}\\s*days\\b`, "gi");
// The earlier user turns of a conversation, as text (history: [{role, content}] or [string]).
const userTurns = (history = []) => history.map(m => typeof m === "string" ? m : m.role === "user" ? m.content : "").join("\n");
// A medicine dose question:
//  * it names a medicine and asks for an amount;
//  * it quotes a medicine-dose figure itself ("You said 240 mg, confirm it", "Is 15 mg/kg right?"), unless it is
//    only about water treatment ("How many 16 mg iodine tablets per quart?");
//  * in a conversation (prior: the earlier user turns) that named a medicine, it asks for an amount ("How much for
//    a 15 kg toddler?" after a paracetamol question). A question only about water treatment is never one.
export function medicineDoseQuestion(q, prior = []){
  q = String(q); const before = userTurns(prior);
  if (MED.test(q) && ASKS_AMOUNT.test(q)) return true;
  if (waterOnly(q)) return false;                       // "How many 16 mg iodine tablets per quart?", also after a medicine turn
  return hasAlways(q) || (MED.test(before) && ASKS_AMOUNT.test(q));
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

// What the reader sees for this answer to question q (prior: the earlier user turns, in Chat).
// -> {text, refused, removed, pointer}
//   refused: the question asked for a medicine dose: REFUSAL, whatever the AI wrote (the pages do not ask it)
//   removed: medicine-dose figures replaced by DOSE_REMOVED; pointer: POINTER was appended
export function guardMedicine(q, answer, prior = []){
  if (medicineDoseQuestion(q, prior)) return {text: REFUSAL, refused: true, removed: 0, pointer: false};
  let text = String(answer);
  // a water-treatment question is not in the medicine conversation before it; its own answer still counts
  const medicine = MED.test(String(q)) || (!waterOnly(String(q)) && MED.test(userTurns(prior))) || MED.test(text);
  const spans = medicineDoseSpans(text, {medicine});
  for (const [a, b] of spans.slice().reverse()) text = text.slice(0, a) + DOSE_REMOVED + text.slice(b);
  const pointer = (spans.length > 0 || medicine) && !/1800\s?022\s?222/.test(text);
  if (pointer) text = text.replace(/\s*$/, "") + "\n\n" + POINTER;
  return {text, refused: false, removed: spans.length, pointer};
}
