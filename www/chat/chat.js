// Kit chat page: a small chat client for the local AI (llamafile's OpenAI-compatible API on :8080), so the
// kit can show its "AI can make mistakes" notice on screen at all times and send a kit system prompt
// (llamafile 0.10.6's bundled chat page offers neither). Same host as this page, so LAN mode works too.
// Replies are rendered with ../md.js (escaped first, then a small markdown subset; no links, no HTML).
import {renderMarkdown, esc} from "../md.js";
import {watchAI, aiContext, DEFAULT_CTX} from "../ai-status.js";

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
  watchAI($("aiStatus"), base);   // re-checked every 3 s until ready, then every 30 s

  $("clear").onclick = () => { session.clear(); if (ctl) ctl.abort(); $("log").innerHTML = ""; $("q").focus(); };
  $("stop").onclick = () => ctl && ctl.abort();
  $("q").addEventListener("keydown", e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("f").requestSubmit(); } });
  $("f").onsubmit = async e => {
    e.preventDefault(); const q = $("q").value.trim(); if (!q || ctl) return;
    $("q").value = ""; const turn = session.ask(q); bubble("user", q);
    const budget = historyBudget(await aiContext("../models.json"));
    const d = bubble("assistant", ""); d.querySelector(".body").textContent = "Thinking… (on a laptop without a GPU this can take a minute)";
    d.scrollIntoView({block: "end"});
    let txt = ""; ctl = new AbortController(); $("stop").hidden = false; $("send").disabled = true;
    try {
      const r = await fetch(API, {method: "POST", headers: {"Content-Type": "application/json"}, signal: ctl.signal,
        body: JSON.stringify({messages: messages(session.history, budget), stream: true, temperature: 0.3, max_tokens: ANSWER_TOKENS})});
      if (!r.ok) throw new Error(r.status === 503 ? "the model is still loading" : "HTTP " + r.status);
      const rd = r.body.getReader(), dec = new TextDecoder(); let buf = "";
      for (;;) { const {done, value} = await rd.read(); if (done) break; buf += dec.decode(value, {stream: true});
        let i; while ((i = buf.indexOf("\n")) >= 0) { const line = buf.slice(0, i).trim(); buf = buf.slice(i + 1);
          if (!line.startsWith("data:")) continue; const s = line.slice(5).trim(); if (s === "[DONE]") continue;
          try { txt += JSON.parse(s).choices[0].delta.content || ""; } catch {}
          setBody(d, "assistant", txt); } }
      if (!txt) d.querySelector(".body").textContent = "The AI returned no answer. Try again.";
    } catch (err) {
      if (err.name === "AbortError") txt += txt ? "\n\n*(stopped)*" : "*(stopped)*";
      else d.querySelector(".body").textContent = "Could not reach the local AI on port 8080 - is it still starting? (" + err.message + ")";
      if (txt) setBody(d, "assistant", txt);
    }
    session.finish(turn, txt);   // dropped if "New chat" was pressed meanwhile
    ctl = null; $("stop").hidden = true; $("send").disabled = false; $("q").focus();
  };
}

// The request: kit system prompt + the newest turns that fit maxChars (always the last user turn).
export function messages(history, maxChars = historyBudget(DEFAULT_CTX)){
  const out = []; let n = 0;
  for (let i = history.length - 1; i >= 0; i--) {
    n += history[i].content.length;
    if (out.length && n > maxChars) break;
    out.unshift(history[i]);
  }
  while (out.length && out[0].role !== "user") out.shift();
  return [{role: "system", content: SYSTEM_PROMPT}, ...out];
}
