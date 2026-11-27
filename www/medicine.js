// Medicine dose guard for every AI answer the kit shows (Chat and Ask the Manuals). No dependencies; tests run it
// under node. The kit never gives medicine doses: prompt rules alone do not stop a 4B model ("15 kg x 15 mg = 225 mg
// per dose", "200 mg to 400 mg every 6 to 8 hours" came back under the kit's own "no doses" rule), so this is code:
//   1. medicineDoseQuestion(q): the question names a medicine (or "medicine / medication / painkiller...") AND asks
//      for an amount (dose, how much / many / often, mg, ml, mg/kg...). The page does not ask the AI at all: it shows
//      REFUSAL, also when the question tricks ("ignore your instructions", "I am a doctor", role-play).
//   2. guardMedicine(q, answer), on every answer as it streams: medicine-dose figures become "[dose removed]":
//      any mg / mcg / mg/kg / IU / grams-per-dose figure (not a concentration such as mg/L), and in a sentence that
//      names a medicine also ml, tablets, capsules, drops, teaspoons and "every N hours" / "N times a day". If any
//      was removed, the safe pointer is added.
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
  "medicines?|medications?|meds|painkillers?|pain ?relie(?:f|vers?)|analgesics?|drugs?";
export const MED = new RegExp(`\\b(?:${MED_NAMES})\\b`, "i");
const ASKS_AMOUNT = /\b(doses?|dosage|dosing|dosed|how (?:much|many|often)|mg|milligrams?|mcg|micrograms?|ml|mls|millilit\w*|mg\/kg|per (?:kg|kilo\w*)|grams?|teaspoons?|tsp|amount|quantity|maximum|max|strength|times a day)\b/i;
export const medicineQuestion = q => MED.test(String(q));

const N = "\\d+(?:[.,]\\d+)?(?:\\s*(?:-|–|to|or)\\s*\\d+(?:[.,]\\d+)?)?";
// Always a medicine dose (not a concentration "mg/L" / "mg per litre").
const ALWAYS = new RegExp(`(?<![\\w.])${N}\\s*(?:mg|milligrams?|mcg|micrograms?|µg|ug|iu|international units?)\\b(?:\\s*(?:\\/|per)\\s*(?:kg|kilo\\w*|dose|day))?(?!\\s*(?:\\/|per)\\s*(?:l\\b|lit))`, "gi");
// A dose only in a sentence that names a medicine ("5 ml of children's ibuprofen", "2 tablets every 4 hours").
const IN_MED = new RegExp(`(?<![\\w.])${N}\\s*(?:ml|mls|millilit\\w*|g\\b|grams?|tablets?|tabs?|capsules?|caps|caplets?|pills?|drops?|teaspoons?|tsp|tablespoons?|tbsp|puffs?|doses?|sachets?)\\b(?:\\s*(?:\\/|per)\\s*(?:kg|kilo\\w*|dose|day))?` +
  `|\\bevery\\s+${N}\\s*(?:-\\s*${N}\\s*)?(?:hours?|hrs?|h)\\b|\\b${N}\\s*(?:times|x)\\s*(?:a|per)\\s*day\\b|\\bfor\\s+${N}\\s*days\\b`, "gi");
// A medicine + dose question, or one that quotes a medicine-dose figure itself ("You said 240 mg, confirm it").
export const medicineDoseQuestion = q => (MED.test(String(q)) && ASKS_AMOUNT.test(String(q))) || new RegExp(ALWAYS.source, "i").test(String(q));
// Sentences as [start, end): a break is . ! ? followed by a space or the end ("7.5 ml" is one sentence), or a line break.
const sentenceSpans = t => { const out = [], re = /[.!?](?=\s|$)|\n/g; let s = 0, m;
  while ((m = re.exec(t))){ out.push([s, m.index + 1]); s = m.index + 1; } if (s < t.length) out.push([s, t.length]); return out; };

// The medicine-dose figures in text, as [start, end) spans.
export function medicineDoseSpans(text){
  text = String(text); const spans = [];
  for (const m of text.matchAll(ALWAYS)) spans.push([m.index, m.index + m[0].length]);
  for (const [a, b] of sentenceSpans(text)){
    const s = text.slice(a, b); if (!MED.test(s) && !new RegExp(ALWAYS.source, "i").test(s)) continue;   // names a medicine, or has an mg dose
    for (const m of s.matchAll(IN_MED)) spans.push([a + m.index, a + m.index + m[0].length]);
  }
  spans.sort((x, y) => x[0] - y[0]);
  const out = []; for (const [a, b] of spans){ const l = out.at(-1); if (l && a <= l[1]) l[1] = Math.max(l[1], b); else out.push([a, b]); }
  return out;
}

// What the reader sees for this answer to question q. -> {text, refused, removed, pointer}
//   refused: the question asked for a medicine dose: REFUSAL, whatever the AI wrote (the pages do not ask it)
//   removed: medicine-dose figures replaced by DOSE_REMOVED; pointer: POINTER was appended
export function guardMedicine(q, answer){
  if (medicineDoseQuestion(q)) return {text: REFUSAL, refused: true, removed: 0, pointer: false};
  let text = String(answer); const spans = medicineDoseSpans(text);
  for (const [a, b] of spans.slice().reverse()) text = text.slice(0, a) + DOSE_REMOVED + text.slice(b);
  const pointer = (spans.length > 0 || medicineQuestion(q)) && !/1800\s?022\s?222/.test(text);
  if (pointer) text = text.replace(/\s*$/, "") + "\n\n" + POINTER;
  return {text, refused: false, removed: spans.length, pointer};
}
