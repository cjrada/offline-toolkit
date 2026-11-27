// Kit web pages, offline (node, no dependencies): node tests/test_kit_js.mjs
//  * www/md.js renders a small markdown subset AFTER escaping: model output cannot inject tags, attributes
//    or javascript: links; [n] becomes a link only to the caller's own source URL; partial (streaming)
//    markup stays literal
//  * www/docs/retrieve.js: for "How do I make water safe to drink?" the passages sent to the model contain
//    actual bleach quantities (the drops table that the chunker had split off, and the WA DoH table),
//    on a fixture that reproduces the failure seen with the old retrieval; stemming / expansion; the
//    system prompt (answer only the question, quote quantities exactly, no snake-bite example)
//  * if www/docs/index.js is a full built index (a kit build), the same check runs against it too
//  * the "AI can make mistakes" notice (tools/ai-notice.json) is on the home, chat and manuals pages
//  * www/chat/chat.js: kit system prompt, history trimming, "New chat" drops an answer still streaming
//  * www/ai-status.js: status re-checked (3 s until ready, then 30 s); context from models.json
//  * prompts fit a 4096-token context: sources and chat history are sized from the AI's ctx
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import {fileURLToPath, pathToFileURL} from "node:url";

const ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const imp = rel => import(pathToFileURL(path.join(ROOT, rel)).href);
const {renderMarkdown} = await imp("www/md.js");
const R = await imp("www/docs/retrieve.js");
const chat = await imp("www/chat/chat.js");
const AS = await imp("www/ai-status.js");
let failed = 0;
async function test(name, fn){ try { await fn(); console.log("ok   " + name); } catch (e) { failed = 1; console.log("FAIL " + name + "\n     " + (e.message || e).split("\n").join("\n     ")); } }

// ---------------------------------------------------------------- markdown
const TAGS = /<\/?([a-z0-9]+)/gi;
const ALLOWED = new Set(["p", "br", "strong", "em", "code", "ul", "ol", "li", "h3", "h4", "h5", "h6", "hr", "a"]);
function onlyOurTags(html){
  for (const [, t] of html.matchAll(TAGS)) assert.ok(ALLOWED.has(t.toLowerCase()), `unexpected tag <${t}> in ${html}`);
  for (const m of html.matchAll(/<a ([^>]*)>/g))
    assert.match(m[1], /^href="[^"]*" target="_blank" rel="noopener"$/, "link attributes: " + m[1]);
  for (const m of html.matchAll(/<(?!a )[a-z0-9]+ ([^>]*)>/gi)) assert.match(m[1], /^start="\d+"$/, "attributes: " + m[0]);
}
await test("markdown: bold, italic, code, headings, lists, paragraphs", () => {
  const h = renderMarkdown("## Boil it\nUse **clean** water and *stir*.\nThen wait.\n\n- one\n- two `x`\n\n1. first\n2. second\n\nEnd __now__ _ok_");
  assert.equal(h, "<h4>Boil it</h4><p>Use <strong>clean</strong> water and <em>stir</em>.<br>Then wait.</p>" +
    "<ul><li>one</li><li>two <code>x</code></li></ul><ol><li>first</li><li>second</li></ol><p>End <strong>now</strong> <em>ok</em></p>");
  assert.equal(renderMarkdown("1. a\n\n2. b"), "<ol><li>a</li><li>b</li></ol>", "a blank line inside a list keeps the list");
  assert.equal(renderMarkdown("3. c"), '<ol start="3"><li>c</li></ol>');
  assert.equal(renderMarkdown("snake_case and 2*3*4"), "<p>snake_case and 2*3*4</p>");
});
await test("markdown: model output cannot inject HTML, attributes or links", () => {
  const evil = ['<script>alert(1)</script>', '<img src=x onerror=alert(1)>', '**<b onclick="x">hi</b>**',
    '[1](javascript:alert(1))', '[click](http://evil.example/)', '`<iframe src=//x>`', '" onmouseover="alert(1)',
    "<a href='javascript:alert(1)'>x</a>", '# <svg onload=alert(1)>', '- <style>*{}</style>', "\u0000<b>0</b>\u0000"];
  for (const e of evil){
    const h = renderMarkdown(e, n => "pdf/doc.pdf#page=" + n);
    onlyOurTags(h);
    assert.ok(!/<(script|img|iframe|svg|style|b )/i.test(h), "tag survived: " + h);
    assert.ok(!/href="javascript/i.test(h), "javascript link: " + h);
  }
  assert.equal(renderMarkdown('[2] and [9]', n => n === 2 ? 'pdf/a"b.pdf#page=3' : null),
    '<p><a href="pdf/a&quot;b.pdf#page=3" target="_blank" rel="noopener">[2]</a> and [9]</p>');
  assert.equal(renderMarkdown("[1]"), "<p>[1]</p>", "no links without a cite function");
});
await test("markdown: partial output while streaming stays sensible", () => {
  assert.equal(renderMarkdown("Use **cle"), "<p>Use **cle</p>");
  assert.equal(renderMarkdown("Use `ble"), "<p>Use `ble</p>");
  assert.equal(renderMarkdown("- a\n- "), "<ul><li>a</li></ul><p>-</p>");
  assert.equal(renderMarkdown("Source [1"), "<p>Source [1</p>");
  onlyOurTags(renderMarkdown("## H\n**b", () => "x"));
});

// ---------------------------------------------------------------- retrieval fixture
// Chunker: the same algorithm as tools/build_index.py chunks() (900 chars, 150 overlap, break at a space).
function chunks(text){
  text = text.replace(/[ \t]+/g, " ").replace(/\.{4,}/g, " ").replace(/\n\s*\n+/g, "\n").trim();
  const out = []; let i = 0;
  while (i < text.length){
    let j = Math.min(text.length, i + 900);
    if (j < text.length){ const k = text.lastIndexOf(" ", j); j = k >= i + 700 && k > i ? k : j; }
    out.push(text.slice(i, j).trim()); if (j >= text.length) break; i = j - 150;
  }
  return out;
}
// Notes: the real kit notes, split by "## " section like build_index.py.
const NOTES = "Australia-first-aid-and-emergency-notes.txt";
const notes = fs.readFileSync(path.join(ROOT, "www/docs/notes", NOTES), "utf8");
const all = [];
for (const sec of notes.split(/\n(?=## )/).slice(1)){
  const head = sec.split("\n")[0].replace(/^#+\s*/, "").trim();
  for (const t of chunks(sec)) all.push({f: NOTES, p: head, t, u: "notes/" + NOTES, boost: 1.6});
}
// PDF pages: the passages quoted in the bug report (USMC p152's table and the WA DoH tables verbatim; the rest
// of each page paraphrased to the same length and wording style), plus the distractors that outranked them.
const USMC_152 = `The water must be treated before drinking. Water from streams, lakes and wells in the field should
be considered contaminated. Disease causing organisms include bacteria, viruses, protozoa such as Giardia and
Cryptosporidium, and parasites. The safest methods of field water treatment are described below; when possible
use more than one. (a) Boiling. Bring the water to a rolling boil. Boiling for one minute kills most organisms.
Boiling does not remove chemicals or improve taste, and fuel may be limited. Filtering first through cloth
removes sediment and makes any later chemical treatment more effective, because cloudy water needs more chemical
and a longer contact time. Treated water must be kept in clean containers so it is not contaminated again
before it is used for drinking, cooking, washing wounds or brushing teeth. Label.
(b) Chlorine bleach. The strength of the solution, number of drops and contact time must be considered.
     PERCENT        CLEAR WATER           CLOUDY WATER             COLD WATER
         1%         10GTTS/30 MIN          20GTS/30 MIN            10GTTS/60MIN
        4-6%         2GTTS/30 MIN          4GTTS/30 MIN            2GTTS/60 MIN
       7-10%        1 GTTS/30 MIN          2GTTS/30 MIN            1 GTTS/60 MIN
(All figures are based on one quart of water.)
(c) Iodine. Iodine tablets or tincture of iodine may be used according to the directions on the package.`;
const WA_1 = `Purifying Water During an Emergency. Store at least one gallon of water per person per day for drinking
and cooking. Water stored in clean, tightly closed food grade containers keeps for a long time. If your stored water
runs out, or you are told your water is unsafe, you can make water safer to drink. Boiling is the surest method. Bring
water to a rolling boil for one minute. Let it cool before drinking. If the water is cloudy, let it settle and filter
it through a clean cloth, paper towel or coffee filter first. Store the boiled water in clean containers with covers.`;
const WA_2 = `If you can't boil water, you can disinfect it using household bleach. Bleach will kill some, but not all,
types of disease-causing organisms that may be in the water. Use regular unscented chlorine bleach products that
are suitable for disinfection and sanitization as indicated on the label. Use bleach that contains 5.25% to 8.25%
sodium hypochlorite. Do not use scented, color safe, or bleaches with added cleaners.
Volume of Water   Amount of 5.25-8.25% Bleach to Add
1 quart/1 liter   5 drops
1/2 gallon/2 quarts/2 liters   10 drops
1 gallon   1/4 teaspoon
5 gallons   1 teaspoon
10 gallons   2 teaspoons
Stir the water well and let it stand for at least 60 minutes before you use it. If the water is still cloudy
after filtering, double the amount of bleach.`;
const READY_31 = `If the power is off, have a licensed electrician check your home before you turn it back on. Make sure
the building is safe to enter; check for gas leaks, damaged water heaters and broken water pipes. Shut off water at
the main valve if pipes are damaged. Do not drink tap water until officials say it is safe. Hire a qualified
contractor to inspect the foundation, the roof and the walls. Keep children and pets away from damaged areas.`;
const FM_227 = `Boils. Apply warm compresses to bring the boil to a head. Then open the boil using a sterile knife, wire,
needle, or similar item. Thoroughly clean out the pus using soap and water. Cover the boil site, checking it
periodically to ensure no further infection develops. Drink plenty of safe water while you recover.`;
const pages = [["USMC-Wilderness-Medicine-Course.pdf", 152, USMC_152], ["WA-DoH-Purifying-Water-in-an-Emergency.pdf", 1, WA_1],
  ["WA-DoH-Purifying-Water-in-an-Emergency.pdf", 2, WA_2], ["Ready-gov-Are-You-Ready.pdf", 31, READY_31],
  ["FM21-76-US-Army-Survival-Manual.pdf", 227, FM_227]];
for (const [f, p, text] of pages) for (const t of chunks(text)) if (t.length > 80) all.push({f, p, t});
// Unrelated filler so document frequencies look like a real index (2,000+ passages).
const FILLER = "splint fracture bandage shelter fire signal mirror knot rope compass map navigation insect bite burn shock " +
  "hypothermia frostbite heat stroke radio battery generator evacuation flood cyclone bushfire smoke shelter food " +
  "ration fishing trap snare plant tree bark wound infection fever dehydration salt sugar sun hat clothing boots";
const fw = FILLER.split(" ");
for (let i = 0; i < 2000; i++)
  all.push({f: "filler.pdf", p: 1 + (i >> 2), t: Array.from({length: 120}, (_, j) => fw[(i * 7 + j * 13) % fw.length]).join(" ")});

const Q = "How do I make water safe to drink?";
const QUANTITY = /GTTS|\d+ drops|1\/4 teaspoon|teaspoons?\b/i;

// The old rag.js retrieval, to show the fixture reproduces the reported failure.
function oldSearch(chunks, q, k = 6){
  const STOP = new Set("a an and are as at be but by can do does for from how i if in into is it its me my of on or our so than that the their then there these this to was we what when where which who why will with you your should would could about".split(" "));
  const stem = w => w.length > 4 ? w.replace(/(ing|ed|es|s)$/, "") : w;
  const tok = s => (s.toLowerCase().match(/[a-z0-9]+/g) || []).filter(w => w.length > 1 && !STOP.has(w)).map(stem);
  const df = new Map(); let total = 0;
  const cs = chunks.map(c => { const tf = new Map(), ts = tok(c.t); total += ts.length; for (const t of ts) tf.set(t, (tf.get(t) || 0) + 1);
    for (const t of tf.keys()) df.set(t, (df.get(t) || 0) + 1); return {c, tf, len: ts.length}; });
  const avg = total / cs.length, qt = [...new Set(tok(q))], N = cs.length, scored = [];
  for (const x of cs){ let s = 0; for (const t of qt){ const f = x.tf.get(t); if (!f) continue; const n = df.get(t);
    s += Math.log(1 + (N - n + 0.5) / (n + 0.5)) * f * 2.4 / (f + 1.4 * (0.25 + 0.75 * x.len / avg)); }
    if (s > 0) scored.push([s * (x.c.boost || 1), x.c]); }
  scored.sort((a, b) => b[0] - a[0]);
  const out = [], seen = {};
  for (const [, c] of scored){ const key = c.f + c.p; if ((seen[key] = (seen[key] || 0) + 1) > 2) continue; out.push(c); if (out.length >= k) break; }
  return out;
}

await test("fixture: the chunker splits USMC p152 between '(b) Chlorine bleach' and its drops table (as in the kit)", () => {
  const usmc = all.filter(c => c.f.startsWith("USMC") && c.p === 152);
  assert.equal(usmc.length, 2);
  assert.match(usmc[0].t, /\(b\) Chlorine bleach\. The strength of the solution, number of$/); assert.doesNotMatch(usmc[0].t, /GTTS/);
  assert.match(usmc[1].t, /GTTS/);
});
await test("fixture reproduces the bug: the old retrieval sends 'bleach' but no quantity for the water question", () => {
  const old = oldSearch(all, Q);
  assert.ok(old.some(c => /bleach/i.test(c.t)), "old top 6 mention bleach");
  assert.ok(!old.some(c => QUANTITY.test(c.t)), "old top 6 had a quantity: " + old.map(c => c.f + " " + c.p).join("; "));
});
const ix = R.createIndex(all.map(c => ({...c})));
await test("retrieval: 'make water safe to drink' now gets passages with the actual bleach quantities", () => {
  const hits = R.search(ix, Q);
  assert.ok(hits.length <= 6);
  const ctx = hits.map(h => h.t).join("\n");
  assert.match(ctx, /10GTTS\/30 MIN/, "USMC drops table (the chunk after the cut) is sent");
  assert.match(ctx, /All figures are based on one quart of water/);
  assert.match(ctx, /1 gallon\s+1\/4 teaspoon/, "WA DoH bleach table is sent");
  const usmc = hits.filter(h => h.f.startsWith("USMC") && h.p === 152);
  assert.equal(usmc.length, 1, "the two USMC p152 chunks are one merged source");
  assert.match(usmc[0].t, /Chlorine bleach\. The strength of the solution, number of drops and contact time/, "merged without the overlap gap");
  assert.equal(usmc[0].t.split("(b) Chlorine bleach").length, 2, "overlap not duplicated");
  assert.ok(ctx.length <= 9000 + 900);
});
for (const q of ["How do I purify water?", "water purification", "purifying drinking water", "Is the creek water drinkable?", "how much bleach for water"]){
  await test(`retrieval: "${q}" reaches a bleach / iodine quantity`, () => {
    assert.match(R.search(ix, q).map(h => h.t).join("\n"), QUANTITY);
  });
}
await test("retrieval: stemming and query expansion", () => {
  for (const w of ["purify", "purified", "purifying", "purification", "purifies"]) assert.equal(R.stem(w), "purif");
  for (const [a, b] of [["drinking", "drink"], ["boiled", "boil"], ["disinfection", "disinfect"], ["disinfectant", "disinfect"], ["drops", "drop"]])
    assert.equal(R.stem(a), b);
  const t = R.queryTerms(Q);
  assert.equal(t.get("water"), 1); assert.equal(t.get("drink"), 1);
  assert.equal(t.get("bleach"), R.EXPAND_WEIGHT); assert.equal(t.get("purif"), R.EXPAND_WEIGHT);
  assert.equal(R.queryTerms("How do I treat a boil on my arm?").get("bleach"), undefined, "no water words, no expansion");
});
await test("retrieval: snake bite still finds the Australian snake-bite note in the top two", () => {
  const h = R.search(ix, "How do I treat a snake bite?");
  assert.ok(h.slice(0, 2).some(x => x.f === NOTES && /Snake bite/.test(x.p)), h.map(x => x.p).join("; "));
});
await test("retrieval: user-added PDFs (blob) and notes keep their own pages; at most 2 sources per page", () => {
  const extra = [{f: "x.pdf", p: 1, t: "alpha bravo charlie ".repeat(10), blob: "blob:1"}, {f: "x.pdf", p: 1, t: "delta echo alpha ".repeat(10), blob: "blob:1"},
    {f: "x.pdf", p: 2, t: "alpha zulu ".repeat(10), blob: "blob:1"}];
  const i2 = R.createIndex([...extra]);
  const h = R.search(i2, "alpha");
  assert.equal(h.length, 2); assert.equal(h[0].blob, "blob:1");
  assert.equal(h.filter(x => x.p === 1).length, 1, "consecutive chunks of page 1 merged into one source");
});
await test("prompt: no snake-bite example; only the question; quantities quoted exactly or 'no amount'", () => {
  const p = R.SYSTEM_PROMPT;
  assert.doesNotMatch(p, /snake|tourniquet|constricting/i);
  assert.match(p, /Answer ONLY the question asked/);
  assert.match(p, /using ONLY the numbered sources/);
  assert.match(p, /Never invent or convert numbers/);
  assert.match(p, /quote it exactly as the source states it, with its units and its citation/);
  assert.match(p, /bleach, iodine/); assert.match(p, /medication/);
  assert.match(p, /check it against the cited page/);
  assert.match(p, /the sources give no amount; do not guess/);
  assert.match(p, /000/);
  const m = R.buildMessages(Q, R.search(ix, Q), c => c.f, c => `page ${c.p}`);
  assert.equal(m[0].content, p);
  assert.match(m[1].content, /^Sources:\n\[1\] /); assert.match(m[1].content, /Question: How do I make water safe to drink\?/);
});

// A real index from a kit build (www/docs/index.js is generated, git-ignored): the same check on real data.
const IDX = path.join(ROOT, "www/docs/index.js");
const real = fs.existsSync(IDX) ? (() => { const sb = {window: {}}; vm.runInNewContext(fs.readFileSync(IDX, "utf8"), sb); return sb.window.DOCS_INDEX; })() : null;
if (real && real.chunks.length > 1000) {
  await test("real index: 'make water safe to drink' gets passages with bleach quantities", () => {
    const h = R.search(R.createIndex(real.chunks.map(c => ({...c}))), Q);
    assert.match(h.map(x => x.t).join("\n"), QUANTITY, h.map(x => `${x.f} ${x.p}`).join("; "));
  });
} else console.log("skip real-index check (www/docs/index.js is not a full kit index here)");

// ---------------------------------------------------------------- notice + chat page
const notice = JSON.parse(fs.readFileSync(path.join(ROOT, "tools/ai-notice.json"), "utf8"));
const box = k => `<p class="warn ai-notice" role="note">${notice[k]}</p>`;
await test("notice text says the three things", () => {
  for (const k of ["sources", "chat"]){
    assert.match(notice[k], /^<b>AI can make mistakes\.<\/b> Always cross-check answers /);
    assert.match(notice[k], /Sources can be wrong or out of date too, but they're far more reliable than the AI alone\.$/);
  }
  assert.match(notice.sources, /cross-check answers with the linked sources\./);
  assert.match(notice.chat, /against the Library or the manuals/);
});
const page = rel => fs.readFileSync(path.join(ROOT, rel), "utf8");
await test("notice on Ask the Manuals, next to the answer (visible before and after an answer)", () => {
  const h = page("www/docs/index.html");
  assert.equal(h.split(box("sources")).length, 2);
  const i = h.indexOf(box("sources"));
  assert.ok(i > h.indexOf('<form id="f"') && i < h.indexOf('<div id="answer"'), "between the question box and the answer");
  assert.doesNotMatch(h.slice(h.indexOf("ai-notice") - 200, i), /<details/, "not inside a collapsible");
});
await test("notice on the kit home page; the Chat tile opens the kit chat page", () => {
  const h = page("www/index.html");
  assert.ok(h.includes(box("sources")));
  assert.match(h, /<a class="tile" href="chat\/"><h2>Chat<\/h2>/);
  assert.doesNotMatch(h, /\bstatus\.textContent/, "bare `status` is window.status, not the element");
  assert.match(h, /document\.getElementById\("aiStatus"\)/);
});
await test("notice on the chat page, pinned above the conversation", () => {
  const h = page("www/chat/index.html");
  assert.ok(h.includes(`<div class="notice-bar">${box("chat")}</div>`));
  assert.ok(h.indexOf("notice-bar") < h.indexOf('id="log"'));
  assert.match(page("www/style.css"), /\.notice-bar\{position:sticky;top:0/);
  assert.match(h, /<script type="module" src="chat\.js"><\/script>/);
  assert.doesNotMatch(h, /https?:\/\//, "no external URLs");
});
await test("chat: kit system prompt and history trimming", () => {
  const p = chat.SYSTEM_PROMPT;
  for (const s of [/offline emergency assistant/, /practical, safe/, /never suggest anything unusual, improvised or dangerous/,
    /not sure, say so/, /call 000/, /Do not give medication doses/, /wild plant, berry or mushroom/]) assert.match(p, s);
  const hist = [];
  for (let i = 0; i < 20; i++) hist.push({role: "user", content: "q".repeat(500)}, {role: "assistant", content: "a".repeat(500)});
  hist.push({role: "user", content: "last"});
  const m = chat.messages(hist);
  assert.equal(m[0].role, "system"); assert.equal(m[0].content, p);
  assert.equal(m[1].role, "user"); assert.equal(m.at(-1).content, "last");
  assert.ok(m.slice(1).reduce((n, x) => n + x.content.length, 0) <= 6000);
  assert.equal(chat.messages([{role: "user", content: "x".repeat(9000)}]).length, 2, "one long question is still sent");
});
await test("chat: 'New chat' while an answer streams drops that answer (no stray AI turn in the new chat)", () => {
  let stored = null; const store = {get: () => stored, set: v => { stored = v; }};
  const s1 = chat.createSession(store);
  const t1 = s1.ask("first?");
  assert.equal(s1.finish(t1, "answer one"), true);
  const t2 = s1.ask("second?");            // streaming...
  s1.clear();                              // "New chat" pressed; the old stream then ends (aborted, partial text)
  assert.equal(s1.finish(t2, "partial *(stopped)*"), false);
  assert.deepEqual(s1.history, []);
  assert.deepEqual(JSON.parse(stored), [], "nothing left in sessionStorage");
  assert.deepEqual(chat.createSession(store).history, [], "reload: no stray AI bubble");
  const t3 = s1.ask("new question");
  assert.equal(s1.finish(t3, "new answer"), true);
  assert.deepEqual(s1.history.map(m => m.role), ["user", "assistant"]);
  const t4 = s1.ask("no answer came");
  s1.finish(t4, "");
  assert.deepEqual(s1.history.map(m => m.content), ["new question", "new answer"], "an unanswered question is removed");
  stored = "{bad json"; assert.deepEqual(chat.createSession(store).history, []);
  stored = '{"not":"a list"}'; assert.deepEqual(chat.createSession(store).history, []);
});
await test("AI status: ready / loading (503) / down, re-checked every 3 s until ready, then every 30 s", async () => {
  const replies = [new Error("refused"), {ok: false, status: 503, body: {error: {code: 503}}}, {ok: true, body: {status: "ok"}}];
  let i = 0, url = "";
  const fetchFn = async u => { url = u; const r = replies[Math.min(i++, replies.length - 1)]; if (r instanceof Error) throw r;
    return {ok: r.ok, status: r.status || 200, json: async () => r.body}; };
  const el = {textContent: "", dataset: {}}, timers = [];
  const stop = AS.watchAI(el, "http://192.168.1.5", {fetchFn, setTimer: (f, ms) => timers.push([f, ms])});
  const step = async () => { for (let k = 0; k < 5; k++) await new Promise(r => setImmediate(r)); };
  await step(); assert.equal(el.textContent, "AI: starting / not running"); assert.equal(timers.at(-1)[1], 3000);
  assert.equal(url, "http://192.168.1.5:8080/health", "same host as the page (LAN mode)");
  timers.at(-1)[0](); await step(); assert.equal(el.textContent, "AI: loading…"); assert.equal(timers.at(-1)[1], 3000);
  timers.at(-1)[0](); await step(); assert.equal(el.textContent, "AI: ready"); assert.equal(timers.at(-1)[1], 30000);
  stop(); const n = timers.length; timers.at(-1)[0](); await step(); assert.equal(timers.length, n, "stopped");
  for (const f of ["www/index.html", "www/chat/chat.js"]) assert.match(page(f), /watchAI\(/, f + " polls with watchAI");
  assert.equal(await AS.aiContext("m", async () => ({json: async () => ({ctx: 8192})})), 8192);
  assert.equal(await AS.aiContext("m", async () => { throw new Error("x"); }), 4096, "unknown ctx -> 4096");
  assert.equal(await AS.aiContext("m", async () => ({json: async () => ({})})), 4096);
});
await test("context budget: manuals prompt and chat history fit a 4096-token context with the answer", () => {
  // Estimate: 2 characters per token for the whole prompt (prose is ~4; tables and numbers are worse than prose,
  // so this is deliberately pessimistic), + the answer's max_tokens, + 50 tokens of chat-template markup.
  const tokens = msgs => Math.ceil(msgs.reduce((n, m) => n + m.content.length, 0) / R.CHARS_PER_TOKEN) + 50;
  assert.equal(R.sourceBudget(8192), 9000); assert.equal(R.sourceBudget(32768), 9000);
  assert.ok(R.sourceBudget(4096) <= 6000 && R.sourceBudget(4096) >= 4500, String(R.sourceBudget(4096)));
  assert.equal(R.sourceBudget(undefined), R.sourceBudget(4096));
  const many = [Q, "How do I treat a snake bite?", "bushfire smoke shelter food water burn", "How do I purify water?",
    fw.join(" "), "splint fracture bandage shelter fire signal"];
  for (const ctx of [4096, 8192]) for (const q of many) {
    const hits = R.search(ix, q, {maxChars: R.sourceBudget(ctx)});
    const chars = hits.reduce((n, h) => n + h.t.length, 0);
    assert.ok(chars <= R.sourceBudget(ctx), `${q} @${ctx}: ${chars} chars of sources`);
    const used = tokens(R.buildMessages(q, hits, c => "FM 21-76 US Army Survival Manual (public domain)", c => "page 227")) + R.ANSWER_TOKENS;
    assert.ok(used <= ctx, `${q} @${ctx}: ~${used} tokens > ${ctx}`);
  }
  // the water question still gets its quantities at 4096
  assert.match(R.search(ix, Q, {maxChars: R.sourceBudget(4096)}).map(h => h.t).join("\n"), QUANTITY);
  // chat: history budget by ctx
  const hist = Array.from({length: 40}, (_, i) => ({role: i % 2 ? "assistant" : "user", content: "x".repeat(700)}));
  for (const ctx of [4096, 8192]) {
    const m = chat.messages(hist, chat.historyBudget(ctx));
    assert.ok(tokens(m) + chat.ANSWER_TOKENS <= ctx, `chat @${ctx}`);
  }
  assert.ok(chat.historyBudget(8192) > chat.historyBudget(4096));
  assert.match(page("www/docs/rag.js"), /search\(ix, q, \{maxChars: sourceBudget\(ctx\)\}\)/);
  assert.match(page("www/docs/rag.js"), /aiContext\("\.\.\/models\.json"\)/);
  assert.match(page("www/chat/chat.js"), /historyBudget\(await aiContext\("\.\.\/models\.json"\)\)/);
});
await test("pages use md.js / retrieve.js, no CDN or remote scripts", () => {
  for (const f of ["www/index.html", "www/docs/index.html", "www/chat/index.html"])
    for (const [, src] of page(f).matchAll(/<script[^>]*src="([^"]+)"/g)) assert.doesNotMatch(src, /^(https?:)?\/\//, f + " " + src);
  assert.match(page("www/docs/rag.js"), /from "\.\.\/md\.js"/);
  assert.match(page("www/docs/rag.js"), /from "\.\/retrieve\.js"/);
  assert.doesNotMatch(page("www/docs/rag.js"), /innerHTML = esc\(txt\)/);
});
process.exit(failed);
