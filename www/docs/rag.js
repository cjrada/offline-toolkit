// Offline RAG: BM25 keyword retrieval in the browser (retrieve.js) + local LLM (llamafile, OpenAI-compatible API).
// Answers are rendered with md.js: escaped first, then a small markdown subset; [n] links to source n.
import {createIndex, search, buildMessages, sourceBudget, ANSWER_TOKENS, tidyAnswer} from "./retrieve.js";
import {renderMarkdown, esc} from "../md.js";
import {aiContext} from "../ai-status.js";
const LLM = `${location.protocol}//${location.hostname}:8080/v1/chat/completions`;

let chunks = [], docs = {}, ix = createIndex([]);
function indexAll(){ ix = createIndex(chunks); }
const $ = id => document.getElementById(id);
function srcLink(c){ return c.blob ? `${c.blob}#page=${c.p}` : c.u ? c.u : `pdf/${encodeURIComponent(c.f)}#page=${c.p}`; }
const where = c => typeof c.p === 'number' ? `page ${c.p}` : `section "${c.p}"`;

// ---- user-added PDFs (IndexedDB) ----
const db = await new Promise((res,rej)=>{ const r=indexedDB.open("offline-kit-docs",1);
  r.onupgradeneeded=()=>r.result.createObjectStore("docs",{keyPath:"file"}); r.onsuccess=()=>res(r.result); r.onerror=()=>rej(r.error); });
const idb = (mode, fn) => new Promise((res,rej)=>{ const tx=db.transaction("docs",mode); const st=tx.objectStore("docs"); const r=fn(st); tx.oncomplete=()=>res(r && r.result); tx.onerror=()=>rej(tx.error); });

async function load(){
  chunks = []; docs = {};
  for (const d of (window.DOCS_INDEX?.docs||[])) docs[d.file] = d.title;
  for (const c of (window.DOCS_INDEX?.chunks||[])) chunks.push(c);
  const mine = await idb("readonly", st => st.getAll()) || [];
  for (const d of mine){ const url = URL.createObjectURL(d.pdf); docs[d.file] = d.title + " (added in this browser)";
    for (const c of d.chunks) chunks.push({...c, blob:url}); }
  indexAll();
  $("ndocs").textContent = `${Object.keys(docs).length} PDFs, ${chunks.length} passages`;
  $("doclist").innerHTML = Object.entries(docs).map(([f,t]) => `<li>${esc(t)}</li>`).join("");
}
function chunkText(text){ text = text.replace(/\s+/g," ").replace(/\.{4,}/g," ").trim(); const out=[]; let i=0;
  while(i<text.length){ let j=Math.min(text.length,i+900); if(j<text.length){const k=text.lastIndexOf(" ",j); if(k>i+700) j=k;}
    out.push(text.slice(i,j)); if(j>=text.length) break; i=j-150; } return out.filter(t=>t.length>80); }
$("add").onchange = async e => {
  const pdfjs = await import("./lib/pdf.min.mjs"); pdfjs.GlobalWorkerOptions.workerSrc = "./lib/pdf.worker.min.mjs";
  for (const file of e.target.files){
    $("addStatus").textContent = `reading ${file.name}…`;
    const pdf = await pdfjs.getDocument({data: await file.arrayBuffer(), isEvalSupported:false}).promise;
    const cs = [];
    for (let p=1; p<=pdf.numPages; p++){ const tc = await (await pdf.getPage(p)).getTextContent();
      for (const t of chunkText(tc.items.map(i=>i.str).join(" "))) cs.push({f:file.name, p, t}); }
    await idb("readwrite", st => st.put({file:file.name, title:file.name.replace(/\.pdf$/i,""), pdf:file, chunks:cs}));
    $("addStatus").textContent = `added ${file.name}: ${pdf.numPages} pages, ${cs.length} passages`;
  }
  await load();
};

// ---- ask ----
$("f").onsubmit = async e => {
  e.preventDefault(); const q = $("q").value.trim(); if(!q) return;
  // Sources sized to the context the AI runs with now (models.json; 4096 if unknown), so the prompt fits.
  const ctx = await aiContext("../models.json");
  const hits = search(ix, q, {maxChars: sourceBudget(ctx, q)});
  $("srcH").hidden = false;
  $("sources").innerHTML = hits.map(c => `<li><a href="${esc(srcLink(c))}" target="_blank" rel="noopener">${esc(docs[c.f]||c.f)}, ${esc(where(c))}</a><div class="snip">${esc(c.t.slice(0,300))}…</div></li>`).join("") || "<li>No matching passages.</li>";
  if ($("searchOnly").checked || !hits.length){ $("answer").textContent = hits.length ? "" : "Nothing in the manuals matched. Try other words, or search the Library (Kiwix)."; return; }
  const messages = buildMessages(q, hits, c => docs[c.f]||c.f, where);
  const out = $("answer"); out.textContent = "Thinking… (first answer on a slow laptop can take a minute)";
  try {
    out.classList.add("md");
    const r = await fetch(LLM, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({messages, stream:true, temperature:0.1, max_tokens:ANSWER_TOKENS})});
    if (!r.ok) throw new Error(r.status === 503 ? "the model is still loading" : "HTTP " + r.status);
    const rd = r.body.getReader(), dec = new TextDecoder(); let buf = "", txt = "";
    for(;;){ const {done,value} = await rd.read(); if(done) break; buf += dec.decode(value,{stream:true});
      let i; while((i = buf.indexOf("\n")) >= 0){ const line = buf.slice(0,i).trim(); buf = buf.slice(i+1);
        if(!line.startsWith("data:")) continue; const d = line.slice(5).trim(); if(d==="[DONE]") continue;
        try { txt += JSON.parse(d).choices[0].delta.content || ""; } catch {}
        out.innerHTML = renderMarkdown(tidyAnswer(txt), n => hits[n-1] ? srcLink(hits[n-1]) : null); } }
    if (!txt) out.textContent = "The AI returned no answer. Try again, or tick \"search only\" and read the sources.";
  } catch(err){ out.textContent = "Could not reach the local AI on port 8080 — is it still starting? (" + err + ")"; }
};
await load();
