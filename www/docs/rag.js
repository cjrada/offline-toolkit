// Offline RAG: BM25 keyword retrieval in the browser + local LLM (llamafile, OpenAI-compatible API).
const LLM = `${location.protocol}//${location.hostname}:8080/v1/chat/completions`;
const STOP = new Set("a an and are as at be but by can do does for from how i if in into is it its me my of on or our so than that the their then there these this to was we what when where which who why will with you your should would could about".split(" "));
const tok = s => (s.toLowerCase().match(/[a-z0-9]+/g) || []).filter(w => w.length > 1 && !STOP.has(w)).map(stem);
function stem(w){ return w.length>4 ? w.replace(/(ing|ed|es|s)$/,"") : w; }

let chunks = [], docs = {}, df = new Map(), avgdl = 0;
function indexAll(){
  df = new Map(); let total = 0;
  for (const c of chunks){ c.tf = new Map(); const ts = tok(c.t); c.len = ts.length; total += ts.length;
    for (const t of ts) c.tf.set(t,(c.tf.get(t)||0)+1);
    for (const t of c.tf.keys()) df.set(t,(df.get(t)||0)+1); }
  avgdl = total / Math.max(1, chunks.length);
}
function search(q, k=6){
  const qt = [...new Set(tok(q))], N = chunks.length, k1 = 1.4, b = 0.75;
  const scored = [];
  for (const c of chunks){ let s = 0;
    for (const t of qt){ const f = c.tf.get(t); if(!f) continue;
      const n = df.get(t); const idf = Math.log(1 + (N - n + 0.5)/(n + 0.5));
      s += idf * f*(k1+1)/(f + k1*(1 - b + b*c.len/avgdl)); }
    if (s>0) scored.push([s*(c.boost||1),c]); }
  scored.sort((a,b)=>b[0]-a[0]);
  // max 2 chunks per page to keep sources diverse
  const out = [], seen = {};
  for (const [s,c] of scored){ const key=c.f+c.p; if((seen[key]=(seen[key]||0)+1)>2) continue; out.push(c); if(out.length>=k) break; }
  return out;
}
const $ = id => document.getElementById(id);
const esc = s => s.replace(/[&<>"]/g, ch => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[ch]));
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
  const hits = search(q);
  $("srcH").hidden = false;
  $("sources").innerHTML = hits.map(c => `<li><a href="${srcLink(c)}" target="_blank">${esc(docs[c.f]||c.f)}, ${where(c)}</a><div class="snip">${esc(c.t.slice(0,300))}…</div></li>`).join("") || "<li>No matching passages.</li>";
  if ($("searchOnly").checked || !hits.length){ $("answer").textContent = hits.length ? "" : "Nothing in the manuals matched. Try other words, or search the Library (Kiwix)."; return; }
  const ctx = hits.map((c,i)=>`[${i+1}] ${docs[c.f]||c.f}, ${where(c)}:\n${c.t}`).join("\n\n");
  const messages = [
    {role:"system", content:"You are an offline survival and emergency reference assistant. Answer ONLY using the numbered sources provided. Cite every claim with its source number in square brackets like [1] or [2][3]. If the sources do not contain the answer, say so plainly. Be practical and concise. The user is in Australia: where the Australian notes conflict with older US military manuals (for example snake bite: pressure immobilisation bandage, never a constricting band or tourniquet), follow the Australian notes and mention the conflict. Australian emergency number is 000. Never state that a wild plant or mushroom is safe to eat, and never give medication doses that are not in the sources; tell the user to verify with the cited page or a professional."},
    {role:"user", content:`Sources:\n${ctx}\n\nQuestion: ${q}`}];
  const out = $("answer"); out.textContent = "Thinking… (first answer on a slow laptop can take a minute)";
  try {
    const r = await fetch(LLM, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({messages, stream:true, temperature:0.2, max_tokens:700})});
    const rd = r.body.getReader(), dec = new TextDecoder(); let buf = "", txt = "";
    for(;;){ const {done,value} = await rd.read(); if(done) break; buf += dec.decode(value,{stream:true});
      let i; while((i = buf.indexOf("\n")) >= 0){ const line = buf.slice(0,i).trim(); buf = buf.slice(i+1);
        if(!line.startsWith("data:")) continue; const d = line.slice(5).trim(); if(d==="[DONE]") continue;
        try { txt += JSON.parse(d).choices[0].delta.content || ""; } catch {}
        out.innerHTML = esc(txt).replace(/\[(\d+)\]/g, (m,n)=> hits[n-1] ? `<a href="${srcLink(hits[n-1])}" target="_blank">[${n}]</a>` : m); } }
  } catch(err){ out.textContent = "Could not reach the local AI on port 8080 — is it still starting? (" + err + ")"; }
};
await load();
