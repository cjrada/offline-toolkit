// Shared AI status for the kit pages (home, chat) and the context size the AI runs with.
// llamafile's /health answers 200 {"status":"ok"} when ready and 503 while the model loads; no answer at all
// means it is starting or not running. Re-checks every 3 s until ready, then every 30 s (the launcher can
// restart the AI on a model switch or CPU fallback).

export async function aiState(base, fetchFn = fetch){
  try {
    const r = await fetchFn(`${base}:8080/health`, {cache: "no-store"});
    const j = await r.json().catch(() => ({}));
    return r.ok && j.status === "ok" ? "ready" : "loading";
  } catch { return "down"; }
}
export const LABEL = {ready: "AI: ready", loading: "AI: loading…", down: "AI: starting / not running"};
export const nextCheckMs = state => state === "ready" ? 30000 : 3000;

// Poll and show the state in el; returns a function that stops polling (for tests).
export function watchAI(el, base, {fetchFn = fetch, setTimer = setTimeout} = {}){
  let stopped = false;
  const tick = async () => {
    if (stopped) return;
    const s = await aiState(base, fetchFn);
    el.textContent = LABEL[s]; el.dataset.state = s;
    if (!stopped) setTimer(tick, nextCheckMs(s));
  };
  tick();
  return () => { stopped = true; };
}

// The context size (tokens) the AI really runs with: llama.cpp's /props (default_generation_settings.n_ctx,
// same CORS as /health) first; then www/models.json, which the launcher writes but which can be stale (a
// read-only drive, or start.bat without PowerShell); else 4096, the smallest the kit uses, so prompts fit
// any start.
export const DEFAULT_CTX = 4096;
export async function aiContext(base, fetchFn = fetch, modelsUrl = "/models.json"){
  const read = async (url, pick) => { try { const c = +pick(await (await fetchFn(url, {cache: "no-store"})).json()); return c >= 1024 ? c : 0; }
    catch { return 0; } };
  return await read(`${base}:8080/props`, j => j?.default_generation_settings?.n_ctx ?? j?.n_ctx)
      || await read(modelsUrl, j => j?.ctx) || DEFAULT_CTX;
}
