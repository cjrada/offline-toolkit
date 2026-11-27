// Kit chat page: a small chat client for the local AI (llamafile's OpenAI-compatible API on :8080), so the
// kit can show its "AI can make mistakes" notice on screen at all times and send a kit system prompt
// (llamafile 0.10.6's bundled chat page offers neither). Same host as this page, so LAN mode works too.
// Replies are rendered with ../md.js (escaped first, then a small markdown subset; no links, no HTML).
import {renderMarkdown, esc} from "../md.js";

export const SYSTEM_PROMPT = [
  "You are the offline emergency assistant of a survival kit used without internet, often in Australia.",
  "Give practical, safe, plain-language answers. Only suggest common, safe items and actions; never suggest anything unusual, improvised or dangerous (no open flames or torches for purifying water, no mixing chemicals).",
  "If you are not sure, say so, and suggest checking the kit's Library or manuals (Ask the Manuals) or a professional.",
  "For emergencies in Australia tell the user to call 000.",
  "Do not give medication doses; refer to the packet, a pharmacist, Healthdirect (1800 022 222) or 000.",
  "Never say a wild plant, berry or mushroom is safe to eat, and do not identify wild food.",
  "Be concise.",
].join(" ");

const MAX_HISTORY_CHARS = 6000;   // keeps the prompt inside a 4k context with room for the answer

if (typeof document !== "undefined") {
  const $ = id => document.getElementById(id);
  const base = `${location.protocol}//${location.hostname}`;
  const API = `${base}:8080/v1/chat/completions`;
  $("raw").href = `${base}:8080/`;
  $("lib").href = `${base}:8081/`;
  let history = [], ctl = null;
  try { history = JSON.parse(sessionStorage.getItem("kit-chat") || "[]"); } catch { history = []; }
  const save = () => { try { sessionStorage.setItem("kit-chat", JSON.stringify(history)); } catch {} };

  function bubble(role, text){
    const d = document.createElement("div"); d.className = "msg " + role;
    d.innerHTML = `<div class="who">${role === "user" ? "You" : "AI"}</div><div class="body answer md"></div>`;
    $("log").append(d); setBody(d, role, text); return d;
  }
  function setBody(d, role, text){
    const b = d.querySelector(".body");
    if (role === "user") b.innerHTML = `<p>${esc(text).replace(/\n/g, "<br>")}</p>`; else b.innerHTML = renderMarkdown(text);
  }
  for (const m of history) bubble(m.role, m.content);

  async function health(){
    try { const r = await fetch(`${base}:8080/health`, {cache: "no-store"}); const j = await r.json().catch(() => ({}));
      $("aiStatus").textContent = r.ok && j.status === "ok" ? "AI: ready" : "AI: loading…"; }
    catch { $("aiStatus").textContent = "AI: starting / not running"; }
  }
  health();

  $("clear").onclick = () => { if (ctl) ctl.abort(); history = []; save(); $("log").innerHTML = ""; $("q").focus(); };
  $("stop").onclick = () => ctl && ctl.abort();
  $("q").addEventListener("keydown", e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("f").requestSubmit(); } });
  $("f").onsubmit = async e => {
    e.preventDefault(); const q = $("q").value.trim(); if (!q || ctl) return;
    $("q").value = ""; history.push({role: "user", content: q}); save(); bubble("user", q);
    const d = bubble("assistant", ""); d.querySelector(".body").textContent = "Thinking… (on a laptop without a GPU this can take a minute)";
    d.scrollIntoView({block: "end"});
    let txt = ""; ctl = new AbortController(); $("stop").hidden = false; $("send").disabled = true;
    try {
      const r = await fetch(API, {method: "POST", headers: {"Content-Type": "application/json"}, signal: ctl.signal,
        body: JSON.stringify({messages: messages(history), stream: true, temperature: 0.3, max_tokens: 1024})});
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
    if (txt) { history.push({role: "assistant", content: txt}); save(); } else history.pop();
    ctl = null; $("stop").hidden = true; $("send").disabled = false; $("q").focus();
  };
}

// The request: kit system prompt + the newest turns that fit MAX_HISTORY_CHARS (always the last user turn).
export function messages(history){
  const out = []; let n = 0;
  for (let i = history.length - 1; i >= 0; i--) {
    n += history[i].content.length;
    if (out.length && n > MAX_HISTORY_CHARS) break;
    out.unshift(history[i]);
  }
  while (out.length && out[0].role !== "user") out.shift();
  return [{role: "system", content: SYSTEM_PROMPT}, ...out];
}
