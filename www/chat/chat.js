// Kit chat page: a small chat client for the local AI (llamafile's OpenAI-compatible API on :8080), so the
// kit can show its "AI can make mistakes" notice on screen at all times and send a kit system prompt
// (llamafile 0.10.6's bundled chat page offers neither). Same host as this page, so LAN mode works too.
// Replies are rendered with ../md.js (escaped first, then a small markdown subset; no links, no HTML).
// Medicine doses are never shown (../medicine.js): a dose question is refused without asking the AI, and every answer
// is guarded as it streams. First aid questions are answered from the kit's manuals: the page searches them
// (../docs/retrieve.js over ../docs/index.js, loaded only for such a question) and sends the passages with the prompt.
import {renderMarkdown, esc} from "../md.js";
import {watchAI, aiContext, DEFAULT_CTX} from "../ai-status.js";
import {medicineDoseQuestion, guardMedicine, REFUSAL} from "../medicine.js";
import {createIndex, search} from "../docs/retrieve.js";

// General rules only, no topic examples: a small model tends to repeat an example as advice (an earlier
// "(e.g. no flames or torches for purifying water)" came back as a tip in water answers).
export const SYSTEM_PROMPT = [
  "You are the offline emergency assistant of a survival kit used without internet, often in Australia.",
  "Give practical, safe, conventional advice in plain language. Only suggest common, well-established actions and items; never suggest anything unusual, improvised or dangerous.",
  "Answer only what was asked.",
  "If you are not sure, say so, and suggest checking the kit's Library or manuals (Ask the Manuals) or a professional.",
  "For emergencies in Australia tell the user to call 000.",
  "Do not give medication doses; refer to the packet, a pharmacist, Healthdirect (1800 022 222) or 000.",
  "Never say that any wild plant or fungus is safe to eat, and do not identify wild food.",
  "Be concise.",
].join(" ");

// ---- first aid: answered from the kit manuals (above all the Australian notes), not from the model's memory.
// Detected conservatively: a named emergency or injury, or a bite / sting by a named animal. Car, cooking and chat
// questions are not first aid ("burn the rubbish" is not a burn; a burn needs a body part or "treat a burn").
const FIRST_AID = new RegExp([
  /\b(?:snake|spider|redback|funnel[- ]?web|mouse spider|tick|centipede|scorpion|bee|wasp|ant|dog|bat)s?\b[^.?!]{0,40}\b(?:bit|bites?|bitten|biting|stings?|stung)\b/,
  /\b(?:bit|bitten|stung)\b[^.?!]{0,30}\b(?:snake|spider|redback|funnel[- ]?web|tick|bee|wasp|dog|bat)\b/,
  /\b(?:jelly ?fish|bluebottles?|irukandji|blue[- ]ringed|stonefish|cone shell|stingray)\b/,
  /\bscald(?:s|ed)?\b/, /\bburn(?:s|t|ed)?\b(?=[^.?!]{0,30}\b(?:hand|arm|leg|finger|skin|face|foot|feet|child|kid|baby|son|daughter|blister\w*|myself|himself|herself)\b)/,
  /\b(?:treat|first aid for|cool|cooling)\s+(?:a |the |my |his |her |their )?burns?\b/,
  /\bhypotherm\w*|\bheat ?stroke\b|\bheat exhaustion\b|\bheat (?:illness|cramps)\b|\bstopped sweating\b|\boverheat(?:ed|ing)\b|\bshivering\b|\bfrostbite\b|\bcpr\b|\bresuscitat\w*|\bnot breathing\b|\bunconscious\b|\bunresponsive\b|\bcollapsed\b/,
  /\bchoking\b|\bbleed(?:ing|s)?\b(?![^.?!]{0,30}\b(?:brakes?|radiator|lines?|air|clutch)\b)|\bhaemorrhag\w*|\bhemorrhag\w*|\bfractur\w*|\bbroken (?:arm|leg|bone|wrist|ankle|ribs?|collarbone)\b|\bsprain\w*|\bdislocat\w*/,
  /\bchest pain\b|\bheart attack\b|\bhaving a stroke\b|\bseizure\b|\banaphyla\w*|\ballergic reaction\b|\bpoison(?:ed|ing)\b|\bdrown\w*|\bconcussion\b|\bhead injury\b|\bfirst aid\b/,
].map(r => r.source).join("|"), "i");
export const firstAidQuestion = q => FIRST_AID.test(String(q));
export const FIRST_AID_RULE = "First aid: the numbered passages below are from the kit's first aid manuals. Base your answer on " +
  "them: give the steps they give for the condition the user describes, and do not add treatments, remedies or items they do " +
  "not mention. Check that a passage is about that condition before you use it, and never use one condition's treatment for " +
  "another. If an Australian note and an older US manual differ, follow the Australian note. If no passage covers the " +
  "condition, say so and tell the user to call 000. Do not mention passage numbers.";
// The passages for a first aid question: the search over the kit index, plus the best Australian note when the search
// found none. -> {hits, text} (text: the passages as sent), or null when nothing matched.
export function firstAidPassages(ix, q, maxChars){
  const hits = search(ix, q, {k: 4, maxChars});
  if (!hits.some(isNote)){
    ix.notes ||= createIndex(ix.chunks.filter(isNote).map(c => ({...c})));
    const n = search(ix.notes, q, {k: 1, maxChars: Math.max(900, maxChars - hits.reduce((x, h) => x + h.t.length, 0))})[0];
    if (n){ if (hits.length >= 4) hits.pop(); hits.unshift(n); }
  }
  if (!hits.length) return null;
  const title = h => ix.titles?.[h.f] || h.f, where = h => typeof h.p === "number" ? `page ${h.p}` : `section "${h.p}"`;
  return {hits, text: hits.map((h, i) => `[${i + 1}] ${title(h)}, ${where(h)}:\n${h.t}`).join("\n\n")};
}
const isNote = c => /^notes\//.test(c.u || "");
// How many characters of passages a first aid question gets: half the history budget, at most 3,600.
export const passageBudget = ctx => Math.min(3600, Math.floor(historyBudget(ctx) / 2));

// What the page sends for the conversation history (last turn = the question). -> {refuse} for a medicine dose
// question (the page shows REFUSAL and does not ask the AI), else {messages, hits} (hits: the first aid passages sent).
export function chatRequest(history, {ctx = DEFAULT_CTX, ix = null} = {}){
  const q = history.at(-1)?.content || "", prior = history.slice(0, -1);
  if (medicineDoseQuestion(q, prior)) return {refuse: true, messages: null, hits: []};   // also "how much for a 15 kg toddler?" after a paracetamol question
  const fa = ix && firstAidQuestion(q) ? firstAidPassages(ix, q, passageBudget(ctx)) : null;
  const budget = historyBudget(ctx) - (fa ? fa.text.length : 0);
  return {refuse: false, hits: fa ? fa.hits : [], messages: messages(history, budget, fa ? `${FIRST_AID_RULE}\n\n${fa.text}` : "")};
}

// History sent with each question, sized to the AI's context (models.json): room for the system prompt
// (~300 tokens) and the answer (ANSWER_TOKENS), at 2 characters per token. 4096 -> ~5,500 characters.
export const ANSWER_TOKENS = 1024;
export const historyBudget = ctx => Math.max(1500, Math.min(16000, Math.floor(((ctx || DEFAULT_CTX) - ANSWER_TOKENS - 300) * 2)));

// The conversation: the turns, kept in sessionStorage through store {get, set}. Each answer belongs to the
// conversation it was asked in: "New chat" starts a new generation, and an answer still streaming from the
// old one (stopped or not) is dropped instead of landing in the new chat.
export function createSession(store){
  let history = [], gen = 0;
  try { history = JSON.parse(store.get() || "[]"); } catch { history = []; }
  if (!Array.isArray(history)) history = [];
  const save = () => { try { store.set(JSON.stringify(history)); } catch {} };
  return {
    get history(){ return history; },
    ask(q){ history.push({role: "user", content: q}); save(); return gen; },    // -> the turn's generation
    finish(turnGen, answer){                                                       // false: dropped (chat was cleared)
      if (turnGen !== gen) return false;
      if (answer) history.push({role: "assistant", content: answer});
      else if (history.at(-1)?.role === "user") history.pop();                     // no answer: let the user ask again
      save(); return true;
    },
    clear(){ gen++; history = []; save(); },
    isCurrent(turnGen){ return turnGen === gen; },                                 // false once "New chat" was pressed
  };
}

if (typeof document !== "undefined") {
  const $ = id => document.getElementById(id);
  const base = `${location.protocol}//${location.hostname}`;
  const API = `${base}:8080/v1/chat/completions`;
  $("raw").href = `${base}:8080/`;
  $("lib").href = `${base}:8081/`;
  const session = createSession({get: () => sessionStorage.getItem("kit-chat"), set: v => sessionStorage.setItem("kit-chat", v)});
  let ctl = null;
  function bubble(role, text){
    const d = document.createElement("div"); d.className = "msg " + role;
    d.innerHTML = `<div class="who">${role === "user" ? "You" : "AI"}</div><div class="body answer md"></div>`;
    $("log").append(d); setBody(d, role, text); return d;
  }
  function setBody(d, role, text){
    const b = d.querySelector(".body");
    if (role === "user") b.innerHTML = `<p>${esc(text).replace(/\n/g, "<br>")}</p>`; else b.innerHTML = renderMarkdown(text);
  }
  for (const m of session.history) bubble(m.role, m.content);
  // The kit's manuals index (../docs/index.js, a few MB), loaded the first time a first aid question is asked.
  let ixP = null;
  const kitIndex = () => ixP ||= new Promise(res => {
    const s = document.createElement("script"); s.src = "../docs/index.js";
    s.onload = () => { const d = window.DOCS_INDEX; if (!d?.chunks?.length) return res(null);
      const ix = createIndex(d.chunks); ix.titles = Object.fromEntries((d.docs || []).map(x => [x.file, x.title])); res(ix); };
    s.onerror = () => res(null); document.head.append(s); });
  const srcLink = h => h.u ? `../docs/${h.u}` : `../docs/pdf/${encodeURIComponent(h.f)}#page=${h.p}`;
  watchAI($("aiStatus"), base);   // re-checked every 3 s until ready, then every 30 s

  $("clear").onclick = () => { session.clear(); if (ctl) ctl.abort(); $("log").innerHTML = ""; $("q").focus(); };
  $("stop").onclick = () => ctl && ctl.abort();
  $("q").addEventListener("keydown", e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("f").requestSubmit(); } });
  $("f").onsubmit = async e => {
    // Busy from the first moment (before any await): a double Enter cannot send twice, and "New chat" can
    // abort this turn even while the context size is still being read.
    e.preventDefault(); const q = $("q").value.trim(); if (!q || ctl) return;
    ctl = new AbortController(); $("stop").hidden = false; $("send").disabled = true;
    const done = () => { ctl = null; $("stop").hidden = true; $("send").disabled = false; $("q").focus(); };
    $("q").value = ""; const turn = session.ask(q); bubble("user", q);
    const prior = session.history.slice(0, -1);          // the earlier turns: a follow-up can be a dose question too
    if (medicineDoseQuestion(q, prior)) {                // never ask the AI for a medicine dose
      bubble("assistant", REFUSAL); session.finish(turn, REFUSAL); done(); return; }
    const ctx = await aiContext(base);
    const ix = firstAidQuestion(q) ? await kitIndex() : null;
    if (!session.isCurrent(turn) || ctl.signal.aborted) { session.finish(turn, ""); done(); return; }   // cleared or stopped meanwhile
    const req = chatRequest(session.history, {ctx, ix});
    const d = bubble("assistant", ""); d.querySelector(".body").textContent = "Thinking… (on a laptop without a GPU this can take a minute)";
    const show = t => { setBody(d, "assistant", guardMedicine(q, t, prior).text);
      if (!req.hits.length) return;
      const byDoc = new Map(); for (const h of req.hits) (byDoc.get(h.f) || byDoc.set(h.f, []).get(h.f)).push(h);
      d.querySelector(".body").insertAdjacentHTML("beforeend", '<p class="small">From the kit manuals: ' + [...byDoc].map(([f, hs]) =>
        `${esc(ix.titles?.[f] || f)}: ` + hs.map(h => `<a href="${esc(srcLink(h))}" target="_blank" rel="noopener">${esc(typeof h.p === "number" ? "page " + h.p : h.p)}</a>`).join(", ")).join("; ") + "</p>"); };
    d.scrollIntoView({block: "end"});
    let txt = "";
    try {
      const r = await fetch(API, {method: "POST", headers: {"Content-Type": "application/json"}, signal: ctl.signal,
        body: JSON.stringify({messages: req.messages, stream: true, temperature: 0.3, max_tokens: ANSWER_TOKENS})});
      if (!r.ok) throw new Error(r.status === 503 ? "the model is still loading" : "HTTP " + r.status);
      const rd = r.body.getReader(), dec = new TextDecoder(); let buf = "";
      for (;;) { const {done, value} = await rd.read(); if (done) break; buf += dec.decode(value, {stream: true});
        let i; while ((i = buf.indexOf("\n")) >= 0) { const line = buf.slice(0, i).trim(); buf = buf.slice(i + 1);
          if (!line.startsWith("data:")) continue; const s = line.slice(5).trim(); if (s === "[DONE]") continue;
          try { txt += JSON.parse(s).choices[0].delta.content || ""; } catch {}
          if (session.isCurrent(turn)) show(txt); } }
      if (!txt) d.querySelector(".body").textContent = "The AI returned no answer. Try again.";
    } catch (err) {
      if (err.name === "AbortError") txt += txt ? "\n\n*(stopped)*" : "*(stopped)*";
      else d.querySelector(".body").textContent = "Could not reach the local AI on port 8080 - is it still starting? (" + err.message + ")";
      if (txt) show(txt);
    }
    if (txt) txt = guardMedicine(q, txt, prior).text;   // history keeps what the reader saw, never a removed dose
    session.finish(turn, txt);   // dropped if "New chat" was pressed meanwhile
    done();
  };
}

// The request: kit system prompt + the newest turns that fit maxChars (always the last user turn).
// grounding: first aid rule + passages, added to the system prompt.
export function messages(history, maxChars = historyBudget(DEFAULT_CTX), grounding = ""){
  const out = []; let n = 0;
  for (let i = history.length - 1; i >= 0; i--) {
    n += history[i].content.length;
    if (out.length && n > maxChars) break;
    out.unshift(history[i]);
  }
  while (out.length && out[0].role !== "user") out.shift();
  return [{role: "system", content: grounding ? `${SYSTEM_PROMPT}\n\n${grounding}` : SYSTEM_PROMPT}, ...out];
}
