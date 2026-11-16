// Home-page model switcher. The launcher writes models.json (current model, why, and every bundled model
// with whether it fits this computer). Switching uploads a one-line request to a control server that
// listens on 127.0.0.1:8079 only, so it works from the laptop itself but not from phones on the LAN.
// The request carries the per-run token from models.json; other websites cannot read that file (no CORS),
// so they cannot forge a switch.
(async () => {
  const $ = id => document.getElementById(id);
  const esc = s => String(s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
  const FIT = {gpu: "fits the GPU", ram: "fits RAM", tight: "tight: 4k context, slow", "too-big": "too big for this computer"};
  const local = ["localhost", "127.0.0.1", "[::1]"].includes(location.hostname);
  let info;
  try { info = await (await fetch("models.json", {cache: "no-store"})).json(); }
  catch { $("curModel").textContent = "unknown (models.json not written - read-only drive?)"; return; }
  const name = p => p.split("/").pop().replace(/\.gguf$/, "");
  $("curModel").textContent = name(info.current) + ` (context ${info.ctx})`;
  $("modelWhy").textContent = `Why: ${info.reason}. This computer: ${info.ram_mb} MB RAM` + (info.vram_mb ? `, ${info.vram_mb} MB GPU memory.` : ", no GPU detected.");
  $("modelRows").innerHTML = info.models.map(m => `<tr><td>${esc(m.name)}${m.spare ? " <span class=small>(spare)</span>" : ""}${m.vision ? " <span class=small>(photos)</span>" : ""}</td>
    <td>${(m.mb / 1024).toFixed(1)} GB</td><td class="fit-${esc(m.fit)}">${esc(FIT[m.fit] || m.fit)}</td>
    <td>${m.path === info.current ? "<b>in use</b>" : local ? `<button data-p="${esc(m.path)}" data-fit="${esc(m.fit)}">Use</button>` : ""}</td></tr>`).join("");
  if (!local) $("switchNote").textContent = "Switch models on the laptop running the kit (open http://localhost:8000 there).";
  $("modelRows").onclick = async e => {
    const b = e.target.closest("button[data-p]"); if (!b) return;
    if (b.dataset.fit === "too-big" && !confirm("This model is bigger than this computer's memory. It may be extremely slow or fail. Try anyway?")) return;
    const fd = new FormData(); fd.append("file_to_upload", new Blob([`${info.switch_token || ""}\n${b.dataset.p}\n`], {type: "text/plain"}), "switch.txt");
    $("curModel").textContent = "switching to " + name(b.dataset.p) + "…";
    try { await fetch("http://127.0.0.1:8079/upload?path=/", {method: "POST", body: fd, mode: "no-cors"}); }
    catch { $("curModel").textContent = "could not reach the switch service (is start.sh / start.bat still running?)"; return; }
    for (let i = 0; i < 120; i++) {           // wait for the new model to report ready
      await new Promise(r => setTimeout(r, 2000));
      try { const j = await (await fetch("models.json", {cache: "no-store"})).json();
        const h = await (await fetch(`${location.protocol}//${location.hostname}:8080/health`)).json();
        if (j.current === b.dataset.p && h.status === "ok") { location.reload(); return; } } catch {}
    }
    $("curModel").textContent = "the new model did not start - see the launcher window / logs";
  };
})();
