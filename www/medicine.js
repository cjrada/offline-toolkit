// Medicine doses on the kit's pages (Chat and Ask the Manuals). No dependencies; tests run it under node.
// The kit is reference material: a medicine dose may be shown only word for word from the kit's own documents, with a
// caveat, and never as a figure the AI wrote. A 4B model invents doses under any prompt rule ("15 kg x 15 mg = 225 mg
// per dose", "200 mg to 400 mg every 6 to 8 hours"), so this is code:
//   1. medicineDoseQuestion(q, prior): the question names a medicine (or "medicine / medication / painkiller...") and
//      asks for an amount (dose, how much / many / often, mg, ml, mg/kg, a body weight...), or quotes an mg dose
//      itself, or is an amount follow-up in a conversation that named one. Water treatment is never one.
//      Such a question is never sent to the AI, however it is phrased ("ignore your instructions", "I am a doctor",
//      role-play):
//        Ask the Manuals (docs/exact.js medicineRoute): the source passages that hold a dose for it are shown word
//          for word with their [n] source links and sourceDoseNote() (check the packet / label, age and weight,
//          follow the source, get medical help); with none, NOT_COVERED (the kit's documents do not give it + POINTER).
//        Chat (no manuals on that path): CHAT_REFUSAL, which points to Ask the Manuals and gives POINTER.
//   2. medicineDoseIn(text): does a source passage hold a medicine dose? By content (an mg / mcg / IU / mg/kg figure
//      outside water treatment, or a dose amount where a medicine is named), not by a fixed list, so curated medical
//      references added to the index later are shown the same way.
//   3. guardMedicine(q, answer, prior, {chat}), on every AI answer as it streams: the AI's own medicine-dose figures
//      become "[dose removed]" (on Ask the Manuals the source amounts are in the exact box, never in the AI's text).
//      When the conversation names a medicine: every mg / mcg / mg/kg / IU, ml, tablet, capsule, drop or teaspoon
//      amount and every frequency or daily maximum ("every four hours", "twice a day", "max 6 in a day") anywhere in
//      the answer. Otherwise: mg-type figures (not concentrations such as mg/L) outside water-treatment sentences
//      ("16 mg iodine tablets per quart" stays), with the amounts in the same sentence. If any was removed, or a
//      medicine is named, the pointer is added (in Chat with the Ask the Manuals pointer).
// None of the messages has a figure with a unit, so Ask the Manuals' amount guard (docs/exact.js) leaves them as they are.
// The water-treatment dose check (docs/dose.js: drops of bleach per litre against the sources) is a different guard.

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
const radiationIodine = s => RADIATION.test(s) && /\biodine\b/i.test(s) && !TREATS_WATER.test(s);
const waterOnly = s => WATER.test(s) && !MED.test(s) && !radiationIodine(s);

// Numbers in digits or words ("7.5", "4 to 6", "four", "four to six").
const NW = "(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty|half|a half|quarter|one and a half)";
const N1 = `(?:\\d+(?:[.,]\\d+)?|${NW})`;
const N = `${N1}(?:\\s*(?:-|–|to|or)\\s*${N1})?`;
// Always a medicine dose (not a concentration "mg/L" / "mg per litre").
const ALWAYS = new RegExp(`(?<![\\w.])${N}\\s*(?:mg|milligrams?|mcg|micrograms?|µg|ug|iu|international units?)\\b(?:\\s*(?:\\/|per)\\s*(?:kg|kilo\\w*|dose|day))?(?!\\s*(?:\\/|per)\\s*(?:l\\b|lit))`, "gi");
const hasAlways = s => new RegExp(ALWAYS.source, "i").test(s);
// A dose once the text is about a medicine ("5 ml of children's ibuprofen", "two tablets every four hours", "twice a day").
// One word may stand between the number and the unit ("2 iodine tablets", "2 paracetamol tablets", "5 ml").
const IN_MED = new RegExp(`(?<![\\w.])${N}\\s*(?:[a-z][a-z'-]*\\s+)?(?:(?:ml|mls|millilit\\w*|g\\b|grams?|tablets?|tabs?|capsules?|caps|caplets?|pills?|drops?|puffs?|doses?|sachets?)\\b|(?:teaspoons?|tsp|tablespoons?|tbsp)\\b(?!\\s+(?:of\\s+)?(?:table\\s+)?(?:salt|sugar)(?![\\w-])(?!\\s+syrups?)))(?:\\s*(?:\\/|per)\\s*(?:kg|kilo\\w*|dose|day))?` +
  `|\\bevery\\s+${N}\\s*(?:hours?|hrs?|h)\\b|\\b${N1}[- ]hourly\\b` +
  `|\\b(?:once|twice|thrice|${N}\\s*(?:times|x))\\s*(?:(?:a|per|each)\\s+day|daily)\\b|\\bfor\\s+${N}\\s*days\\b` +
  // a fraction of a unit: "half a tablet", "a quarter of a tablet", "one and a half tablets"
  `|\\b(?:(?:one|two|three)\\s+and\\s+)?(?:an?\\s+)?(?:half|quarter|third)\\s+(?:of\\s+)?(?:an?\\s+)?(?:(?:tablets?|tabs?|capsules?|caplets?|pills?|doses?|sachets?)\\b|(?:teaspoons?|tsp|tablespoons?|tbsp)\\b(?!\\s+(?:of\\s+)?(?:table\\s+)?(?:salt|sugar)(?![\\w-])(?!\\s+syrups?)))` +
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

// What the reader sees for this answer to question q (prior: the earlier user turns, in Chat).
// -> {text, refused, removed, pointer}
//   refused: the question asked for a medicine dose: REFUSAL, whatever the AI wrote (the pages do not ask it)
//   removed: medicine-dose figures replaced by DOSE_REMOVED; pointer: POINTER was appended
// chat: the Chat page (its refusal and pointer name Ask the Manuals). On Ask the Manuals a medicine dose question never
// reaches the AI (docs/exact.js medicineRoute), so there the refusal branch is only a safety net.
export function guardMedicine(q, answer, prior = [], {chat = false} = {}){
  if (medicineDoseQuestion(q, prior)) return {text: chat ? CHAT_REFUSAL : NOT_COVERED, refused: true, removed: 0, pointer: false};
  let text = String(answer);
  // a water-treatment question is not in the medicine conversation before it; its own answer still counts
  const medicine = MED.test(String(q)) || radiationIodine(String(q)) || (!waterOnly(String(q)) && MED.test(userTurns(prior))) || MED.test(text);
  const spans = medicineDoseSpans(text, {medicine});
  for (const [a, b] of spans.slice().reverse()) text = text.slice(0, a) + DOSE_REMOVED + text.slice(b);
  const pointer = (spans.length > 0 || medicine) && !/1800\s?022\s?222/.test(text);
  if (pointer) text = text.replace(/\s*$/, "") + "\n\n" + (chat ? CHAT_REFUSAL : POINTER);
  return {text, refused: false, removed: spans.length, pointer};
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
