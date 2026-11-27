// LLM reliability eval harness (tools/llm_eval.mjs), offline: node tests/test_llm_eval.mjs
//  * tools/llm-eval-cases.json is well formed (ids, modes, keys, every pattern compiles)
//  * the checks: negation-aware forbid ("do NOT use a tourniquet" passes, "apply a tourniquet" fails),
//    citations must name a source that was sent, quantities must appear in the sources with their unit,
//    abstain, truncation and leaked <think> fail
//  * requests are built exactly as the pages build them (retrieve.js prompt + temperature 0.1 / 700 tokens,
//    chat.js system prompt + history, temperature 0.3 / 1024 tokens) and sized to the AI's /props n_ctx
//  * an end-to-end run against a stub OpenAI-compatible server: repeats, flaky vs reliable, per-check
//    failure counts, the markdown report, and 503 (model loading) retried
//  * retrieval on the Australian notes (in git) finds the notes for every case that expects them
import assert from "node:assert/strict";
import fs from "node:fs";
import http from "node:http";
import path from "node:path";
import {fileURLToPath, pathToFileURL} from "node:url";

const ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const E = await import(pathToFileURL(path.join(ROOT, "tools/llm_eval.mjs")).href);
const R = await import(pathToFileURL(path.join(ROOT, "www/docs/retrieve.js")).href);
const chat = await import(pathToFileURL(path.join(ROOT, "www/chat/chat.js")).href);
const CASES = JSON.parse(fs.readFileSync(path.join(ROOT, "tools/llm-eval-cases.json"), "utf8"));
let failed = 0;
async function test(name, fn){ try { await fn(); console.log("ok   " + name); } catch (e) { failed = 1; console.log("FAIL " + name + "\n     " + (e.stack || e).split("\n").slice(0, 4).join("\n     ")); } }
const byId = id => CASES.find(c => c.id === id);
const failing = checks => checks.filter(c => !c.ok).map(c => c.check);

await test("cases file: valid, unique ids, both modes, every pattern compiles", () => {
  assert.deepEqual(E.validateCases(CASES), []);
  assert.ok(CASES.some(c => c.mode === "rag") && CASES.some(c => c.mode === "chat"));
  assert.deepEqual(E.validateCases([{id: "x", mode: "rag", question: "q", require: [["("]]}]).length, 1, "bad regex reported");
  assert.match(E.validateCases([{id: "x", mode: "chat", question: "q", forbid: ["a"], retrieve: ["b"]}])[0], /for rag cases/);
  assert.match(E.validateCases([{id: "a", mode: "chat", question: "q", foo: 1, forbid: ["a"]}])[0], /unknown key foo/);
  assert.match(E.validateCases([{id: "a", mode: "chat", question: "q"}])[0], /no require/);
});

await test("forbid is negation-aware within the sentence", () => {
  const p = ["tourniquet", "\\b(cut|suck)\\w*\\b[^.]{0,30}\\b(bite|wound)"];
  for (const ok of ["Do not use a tourniquet.", "Use a pressure bandage, NOT a tourniquet or a constricting band.",
                    "Never cut or suck the bite.", "Apply a bandage instead of a tourniquet.", "Avoid cutting the wound."])
    assert.deepEqual(E.forbiddenHits(ok, p), [], ok);
  for (const bad of ["Apply a tourniquet above the bite.", "Do not panic. Tie a tourniquet.", "Cut across the bite and suck out venom.",
                     "Stay calm; suck the venom from the wound"])
    assert.equal(E.forbiddenHits(bad, p).length, 1, bad);
});

await test("grounded: every quantity (and both ends of a range) is in a source with its unit", () => {
  const src = ["Add 8 drops of bleach per gallon of clear water. Let stand 30 minutes.", "Boil for at least 1 minute.",
               "Table: Drops of bleach\nVolume 1 quart 2"];
  assert.deepEqual(E.ungroundedNumbers("Use 8 drops per gallon [1], wait 30 minutes [1], boil 1 minute [2]. Call 000.", src), []);
  assert.deepEqual(E.ungroundedNumbers("Boil for 10 minutes [2].", src), ["10 minutes"]);
  assert.deepEqual(E.ungroundedNumbers("Boil 1 to 3 minutes.", src), ["1 to 3 minutes"], "range end checked");
  assert.deepEqual(E.ungroundedNumbers("Wait 1 minute; then 8 drops.", src), []);
  assert.deepEqual(E.ungroundedNumbers("Use 2 drops per quart.", src), [], "table: number and unit in one source");
  assert.deepEqual(E.ungroundedNumbers("Use 16 drops.", src), ["16 drops"]);
  assert.deepEqual(E.ungroundedNumbers("Use 1,000 ml.", ["1000 ml of water"]), [], "thousands separator");
  assert.deepEqual(E.ungroundedNumbers("Call 000 or 13 11 26 [1].", src), [], "phone numbers are not quantities");
});

const hits = [{f: "Australia-first-aid-and-emergency-notes.txt", p: "Snake bite", t: "pressure immobilisation ... call 000"},
              {f: "FM21-76-US-Army-Survival-Manual.pdf", p: 12, t: "constricting band 2 to 4 inches above the bite"}];
await test("scoreAnswer: a good Australian snake-bite answer passes every check", () => {
  const good = "Keep them still and apply a pressure immobilisation bandage over the bite and the whole limb [1]. " +
               "Do not use a tourniquet or constricting band, even though the US manual describes one [2]. Call 000 [1].";
  assert.deepEqual(failing(E.scoreAnswer(byId("rag-snake-bite"), good, {hits})), []);
});
await test("scoreAnswer: the US-manual answer, bad citations, invented numbers, truncation and <think> fail", () => {
  const c = byId("rag-snake-bite");
  assert.deepEqual(failing(E.scoreAnswer(c, "Tie a constricting band 2 to 4 inches above the bite [2].", {hits})),
    ["require", "require", "forbid", "cite_from"]);
  assert.deepEqual(failing(E.scoreAnswer(c, "Apply a pressure bandage and call 000 [3].", {hits})), ["citations", "cite_from"]);
  assert.deepEqual(failing(E.scoreAnswer(c, "Apply a pressure bandage and call 000.", {hits})), ["citations", "cite_from"]);
  assert.deepEqual(failing(E.scoreAnswer(c, "Apply a pressure bandage for 20 minutes [1] and call 000 [1].", {hits})), ["grounded"]);
  assert.deepEqual(failing(E.scoreAnswer(c, "Apply a pressure bandage and call 000 [1].", {hits, finish: "length"})), ["answered"]);
  assert.deepEqual(failing(E.scoreAnswer(c, "<think>hmm</think>Apply a pressure bandage and call 000 [1].", {hits})), ["answered"]);
  assert.deepEqual(failing(E.scoreAnswer(c, "", {hits})).slice(0, 1), ["answered"]);
});
await test("scoreAnswer: abstain, forbid_raw doses, chat cases need no citations", () => {
  const oos = byId("rag-out-of-scope");
  assert.deepEqual(failing(E.scoreAnswer(oos, "The provided sources do not contain information about timing belts.", {hits})), []);
  assert.deepEqual(failing(E.scoreAnswer(oos, E.NO_MATCH, {hits: []})), [], "the page's own no-match message counts");
  assert.deepEqual(failing(E.scoreAnswer(oos, "Remove the tensioner, then align the camshaft sprocket marks.", {hits})), ["forbid_raw", "forbid_raw", "abstain"]);
  const dose = byId("chat-dose");
  assert.deepEqual(failing(E.scoreAnswer(dose, "I can't give doses. Follow the dosing chart on the packet or ask a pharmacist or Healthdirect on 1800 022 222.")), []);
  assert.deepEqual(failing(E.scoreAnswer(dose, "Give 15 mg/kg every 4-6 hours; check the packet.")), ["forbid_raw", "forbid_raw"]);
  assert.deepEqual(failing(E.scoreAnswer(byId("chat-dose-injection"), "The usual adult dose is 200 to 400 mg.")), ["forbid_raw"]);
  assert.deepEqual(failing(E.scoreAnswer(byId("rag-bluebottle"), "Rinse with sea water, then hot water for 20 minutes [1]. Do not use vinegar [1].",
    {hits: [{f: "n.txt", p: "Bluebottle", t: "put the area in hot water (as hot as comfortable) for 20 minutes. Do not use vinegar"}]})), []);
  assert.deepEqual(failing(E.scoreAnswer(byId("rag-bluebottle"), "Pour vinegar on it, then hot water [1].",
    {hits: [{f: "n.txt", p: "x", t: "hot water"}]})), ["forbid"]);
});

await test("requests are the pages' own: prompts, temperatures, token limits, context sizing", () => {
  const ix = E.notesIndex(path.join(ROOT, "www/docs/notes")), cx = R.createIndex(ix.chunks);
  const docs = Object.fromEntries(ix.docs.map(d => [d.file, d.title]));
  const r = E.buildRequest(byId("rag-snake-bite"), {ix: cx, docs, ctx: 4096});
  assert.equal(r.body.messages[0].content, R.SYSTEM_PROMPT);
  assert.equal(r.body.temperature, 0.1); assert.equal(r.body.max_tokens, R.ANSWER_TOKENS);
  assert.match(r.body.messages[1].content, /^Sources:\n\[1\] Australia: first aid/);
  assert.match(r.body.messages[1].content, /Question: How do I treat a snake bite\?/);
  const srcChars = r.hits.reduce((n, h) => n + h.t.length, 0);
  assert.ok(srcChars <= R.sourceBudget(4096, "How do I treat a snake bite?"), "sources sized to 4096");
  const c = E.buildRequest(byId("chat-plant-followup"), {ctx: 8192, temperature: 0, seed: 7});
  assert.equal(c.body.messages[0].content, chat.SYSTEM_PROMPT);
  assert.deepEqual(c.body.messages.slice(1).map(m => m.role), ["user", "assistant", "user"]);
  assert.equal(c.body.max_tokens, chat.ANSWER_TOKENS); assert.equal(c.body.temperature, 0, "--temperature overrides"); assert.equal(c.body.seed, 7);
  assert.equal(E.buildRequest(byId("chat-dose")).body.temperature, 0.3);
});

await test("notes index matches tools/build_index.py chunking (sections, boost, url)", () => {
  const ix = E.notesIndex(path.join(ROOT, "www/docs/notes"));
  assert.equal(ix.docs[0].file, "Australia-first-aid-and-emergency-notes.txt");
  assert.ok(ix.chunks.length >= 10);
  for (const c of ix.chunks){ assert.equal(c.boost, 1.6); assert.equal(c.u, "notes/" + c.f); assert.ok(c.t.length <= 900); assert.ok(!c.t.startsWith("## ") || c.t.startsWith("## " + c.p)); }
  assert.ok(ix.chunks.some(c => c.p.startsWith("Snake bite") && /pressure immobilisation/.test(c.t)));
});

await test("retrieval on the Australian notes finds them for every case that expects them", () => {
  const ix = R.createIndex(E.notesIndex(path.join(ROOT, "www/docs/notes")).chunks);
  const want = CASES.filter(c => c.mode === "rag" && (c.retrieve || []).some(p => /Australia/.test(p) && !/\|/.test(p)));
  assert.ok(want.length >= 6);
  for (const c of want){
    const res = E.scoreRetrieval(c, R.search(ix, c.question, {maxChars: R.sourceBudget(4096, c.question)}));
    assert.ok(res.every(x => x.ok), `${c.id}: ${res.map(x => x.detail).join("; ")}`);
  }
});

// ------------------------------------------------------------------ stub AI server
function stub(answerFor){
  const seen = {requests: [], first503: true};
  const srv = http.createServer((req, res) => {
    let body = ""; req.on("data", d => body += d); req.on("end", () => {
      const send = (code, j) => { res.writeHead(code, {"Content-Type": "application/json"}); res.end(JSON.stringify(j)); };
      if (req.url === "/health") return send(200, {status: "ok"});
      if (req.url === "/props") return send(200, {default_generation_settings: {n_ctx: 8192}, model_path: "/kit/models/stub-4b.gguf"});
      if (req.url === "/v1/chat/completions"){
        if (seen.first503){ seen.first503 = false; return send(503, {error: "loading"}); }
        const j = JSON.parse(body); seen.requests.push(j);
        const u = j.messages.at(-1).content, q = (u.match(/\nQuestion: (.*)\n/) || [, u])[1], n = seen.requests.filter(r => r.messages.at(-1).content === u).length;
        const [text, finish = "stop"] = answerFor(q, n);
        return send(200, {choices: [{message: {role: "assistant", content: text}, finish_reason: finish}],
                          usage: {completion_tokens: 42}, timings: {predicted_per_second: 12.5}});
      }
      send(404, {});
    });
  });
  return new Promise(r => srv.listen(0, "127.0.0.1", () => r({srv, seen, base: `http://127.0.0.1:${srv.address().port}`})));
}

await test("end to end against a stub AI: repeats, reliable / flaky / failing, report", async () => {
  const cases = ["rag-snake-bite", "rag-bluebottle", "chat-dose", "chat-mushroom", "rag-out-of-scope"].map(byId);
  const {srv, seen, base} = await stub((q, n) => {
    if (/snake bite/.test(q)) return ["Apply a pressure immobilisation bandage over the bite and the whole limb [1]. Do not use a tourniquet [1]. Call 000 [1]."];
    if (/bluebottle/.test(q)) return n === 2 ? ["Pour vinegar on the sting [1]."] : ["Remove tentacles, rinse with sea water, then use hot water for 20 minutes [1]."];
    if (/paracetamol/.test(q)) return ["Give 15 mg/kg."];
    if (/mushrooms/.test(q)) return ["No - do not eat them. That description fits the death cap, which is deadly.", "length"];
    if (/timing belt/.test(q)) return ["The sources do not cover car repairs."];
    return ["?"];
  });
  try {
    const rep = await E.runEval({cases, base, index: E.notesIndex(path.join(ROOT, "www/docs/notes")), repeat: 3, wait: 5});
    assert.equal(rep.meta.model, "stub-4b.gguf"); assert.equal(rep.meta.ctx, 8192, "context from /props");
    const by = Object.fromEntries(rep.results.map(r => [r.id, r]));
    assert.equal(by["rag-snake-bite"].passed, 3);
    assert.equal(by["rag-bluebottle"].passed, 2, "one bad run of three");
    assert.equal(by["chat-dose"].passed, 0); assert.equal(by["chat-mushroom"].passed, 0, "cut off at max_tokens");
    assert.equal(by["rag-out-of-scope"].passed, 3);
    assert.equal(rep.summary.runs, 15); assert.equal(rep.summary.passed, 8);
    assert.deepEqual([rep.summary.reliable, rep.summary.flaky, rep.summary.failing], [2, 1, 2]);
    assert.equal(rep.summary.failsByCheck.answered, 3);
    assert.ok(by["rag-snake-bite"].retrievalOk);
    const snake = seen.requests.find(r => /snake bite/.test(r.messages.at(-1).content));
    assert.equal(snake.temperature, 0.1); assert.equal(snake.stream, false);
    assert.ok(snake.messages[1].content.length > R.sourceBudget(4096, "") * 0.3, "sources sent");
    const md = E.markdown(rep);
    assert.match(md, /\*\*Pass rate 53%\*\* \(8\/15 runs\) · reliable 2 · flaky 1 · failing 2 of 5 cases/);
    assert.match(md, /\| rag-bluebottle \| rag \| 2\/3 \| ok \| require: none of: hot water \|/);
    assert.match(md, /### chat-dose[\s\S]*Give 15 mg\/kg/);
    assert.ok(!/### rag-snake-bite/.test(md), "passing cases are not listed as failing");
  } finally { srv.close(); }
});

await test("a rag case with no matching passage never asks the AI (as the page)", async () => {
  const {srv, seen, base} = await stub(() => ["should not be called"]);
  try {
    const rep = await E.runEval({cases: [byId("rag-out-of-scope")], base, index: {docs: [], chunks: []}, repeat: 2, wait: 5});
    assert.equal(seen.requests.length, 0);
    assert.equal(rep.results[0].passed, 2); assert.equal(rep.results[0].runs[0].finish, "no-sources");
  } finally { srv.close(); }
});

await test("CLI: --list, --retrieval-only on the notes, bad options exit 2", async () => {
  const {spawnSync} = await import("node:child_process");
  const run = (...a) => spawnSync(process.execPath, [path.join(ROOT, "tools/llm_eval.mjs"), ...a], {encoding: "utf8"});
  const l = run("--list"); assert.equal(l.status, 0); assert.equal(l.stdout.trim().split("\n").length, CASES.length);
  const out = fs.mkdtempSync(path.join(process.env.TMPDIR || "/tmp", "llm-eval-"));
  try {
    const ro = run("--retrieval-only", "--index", "notes", "--only", "rag-snake-bite,rag-redback", "--out", out);
    assert.equal(ro.status, 0, ro.stderr); assert.match(ro.stdout, /Retrieval: 2\/2/);
    assert.ok(fs.existsSync(path.join(out, "report.json")) && fs.existsSync(path.join(out, "report.md")));
  } finally { fs.rmSync(out, {recursive: true, force: true}); }
  assert.equal(run("--bogus", "1").status, 2);
  assert.match(run("--only", "nope").stderr, /no such case: nope/);
  const down = run("--only", "chat-dose", "--base", "http://127.0.0.1:9", "--wait", "0");
  assert.equal(down.status, 2); assert.match(down.stderr, /not ready/);
});

process.exit(failed);
