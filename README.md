# Offline Survival Kit (DIY, free, open source)

A do-it-yourself version of the "AI offline survival drive" idea: copy this folder to a USB stick or
external SSD, plug it into an ordinary **Windows 10/11 or Linux** laptop (64-bit), and run one file.
No install, no internet, no accounts. Everything runs on `localhost` in your web browser.

> **Not medical advice.** Check current guidance (ANZCOR, St John, Red Cross, Healthdirect). In an emergency call **000**.

## Get a kit onto a drive

**Download a prebuilt kit from <https://cjrada.github.io/offline-toolkit/>.** The page lists the latest
small, medium and large kit (version, date, size, sha256) and the previous versions still kept. The files
themselves are served from Cloudflare R2, with no account or sign-in. They are big: small ~7 GB,
medium ~41 GB, large ~173 GB, so use a fast connection and a download manager that can resume.

Download the `.zip` and its `.sha256` file into the same folder and check it before you unzip:

```sh
sha256sum -c offline-toolkit-small-<version>.zip.sha256    # must print: OK   (macOS: shasum -a 256 -c ...)
```

On Windows (PowerShell), check the hash with `(Get-FileHash offline-toolkit-small-<version>.zip).Hash` and
compare it with the first word of the `.sha256` file.

The same files are listed by machine-readable manifests next to them on R2: `latest.json` (newest of each
tier), `index.json` (every kept version), and `<tier>/latest.json` / `<tier>/index.json`. How kits are
built and published: [docs/SETUP.md](docs/SETUP.md). (Publishing needs an R2 token, kept only as
`gcs-publish` environment secrets so it is released only behind that environment's approval on `main` or
a `v*` tag; it is never a repository secret.)

**Support:** the kits are free. If you would like to help cover hosting (R2 storage and publishing the large
kits), donations are welcome but entirely optional; the addresses are in the
[Support hosting](https://cjrada.github.io/offline-toolkit/#support) section of the downloads page
(they live in `tools/donate.json`).

1. **Format the drive exFAT.** FAT32 cannot hold files over 4 GB (models and ZIMs are bigger), and NTFS is
   awkward on some Linux systems. Windows: right-click the drive → Format → exFAT. Linux: `sudo mkfs.exfat -n KIT /dev/sdX1`.
2. **Unzip onto the drive.**
   * **Windows:** right-click the zip → *Extract All…* → choose the drive.
   * **Linux:** `unzip offline-toolkit-small-$V.zip -d /media/$USER/KIT/`. `unzip` keeps the executable bits.
3. **Optionally check every file.** On Linux, from the kit folder: `sha256sum -c --quiet SHA256SUMS`.
4. **Run it** as below. You can also build a kit yourself: see *Build a kit*.

| What | Tool | Port |
|---|---|---|
| Home page, Ask-the-Manuals, Maps, PDF shelf | `miniserve` (static web server) | 8000 |
| AI chat (general assistant) + API used by Ask-the-Manuals | `llamafile` 0.10.6 (llama.cpp); on a Windows GPU, llama.cpp's own `llama-server` (Vulkan, b12626) | 8080 |
| Offline library (wiki / guides) | `kiwix-serve` 3.8.x | 8081 |

## Start / stop

* **Windows:** double-click `start.bat` (or `start.bat lan` to let phones on the same Wi-Fi connect). Stop with `stop.bat`.
  First run: Windows SmartScreen / Defender may ask about `llamafile.exe`, `kiwix-serve.exe`, `miniserve.exe` -
  choose *More info -> Run anyway*; allow the firewall prompt only if you want LAN/phone access.
* **Linux:** `sh start.sh` (or `sh start.sh lan`). Ctrl+C or `sh stop.sh` to stop.
  `stop.sh` stops only this kit: it asks this kit's launcher to exit (PID file per user and kit folder,
  checked to still be a `start.sh`), then stops leftovers whose command line names this kit folder
  (`bin/llamafile.exe`, `www/`, `library/`), so another kit copy or your own llamafile / kiwix-serve /
  miniserve keeps running. `stop.bat` likewise stops only programs whose `.exe` is inside the kit folder;
  `stop.bat all` stops every llamafile / llama-server / kiwix-serve / miniserve (the old behaviour).
  Works even if the drive is mounted `noexec` (it runs llamafile via `sh` and copies the two small static binaries to /tmp).
* Then open <http://localhost:8000/>. The AI needs ~10-60 s to load (longer from a slow USB stick).
* **The model is picked automatically.** Each tier bundles several sizes (small: 1.7B + 4B; medium adds 8B
  and a vision model; large adds 30B-A3B).
  1. If a GPU reports enough memory (NVIDIA via `nvidia-smi`; AMD via the amdgpu driver/`rocm-smi` on Linux,
     or the display adapter's memory on Windows), it uses the biggest model with size + ~1 GB <= VRAM.
  2. Otherwise it uses the biggest model with size + ~4 GB <= RAM.
  3. Otherwise it uses the smallest model with a 4k context.

  The launcher prints which model it chose and why. Preview it with `sh tools/pick-model.sh`
  (Windows: `powershell -File tools\pick-model.ps1`). `models/spare/` is never auto-picked (vision,
  "uncensored", experiments). A vision model is paired with its projector by name: `x.gguf` + `x.mmproj.gguf`.
* **Switch models from the home page** ("AI model" panel). It shows the current model, why it was
  chosen, and every bundled model marked *fits the GPU / fits RAM / tight / too big*. Clicking *Use*
  restarts the AI with that model in 10-60 s. How it works:
  * The page uploads a small request to a second miniserve that listens on **127.0.0.1:8079 only**,
    into a private temp folder.
  * The request must carry a random per-run token. Only `models.json` contains it, and miniserve sends no
    CORS headers, so other websites open in the browser cannot read it and cannot forge a switch.
  * The launcher accepts it only if it names an existing model in `models/` or `models/spare/`, then
    restarts llamafile.
  * Phones in LAN mode can see the panel but cannot switch.
  * No Python is involved. llamafile 0.10.6 includes llama.cpp's multi-model "router" code, but its
    command line always loads exactly one `-m` model, so the kit restarts it instead.
* **"Use CPU only" toggle** (home page, under the model list). Shows whether the AI runs on the CPU only
  or in GPU / auto mode; ticking or unticking it restarts llamafile with or without `--gpu disable`. If the
  model was picked automatically, it is picked again for the new mode (RAM vs GPU memory); a model you chose
  is kept. It uses the same 127.0.0.1:8079 switch service and per-run token as *Use* (the request carries a
  third line, `cpu=on` or `cpu=off`, validated as strictly as the model path), so it is laptop-only too.
* **Windows GPU engine: llama.cpp's `llama-server` (Vulkan).** `start.bat` runs GPU starts on
  `bin\llama-vulkan\llama-server.exe` (llama.cpp b12626) when a GPU was detected (the picker saw GPU memory)
  and `GPU` is unset, or when you set `GPU=vulkan`. With no GPU detected, llamafile runs as before. Its newer ggml has
  no long-prompt crash on Turing cards (below), reads prompts about twice as fast as llamafile 0.10.6 and
  answers slightly faster (RTX 2070 Max-Q, Qwen3 8B: ~1,100 vs ~550 tok/s prompt, 32-36 vs 22-31 tok/s
  answers). Flash attention is `auto` there (`FA=` still overrides) - it switches itself off where the GPU
  lacks support. CPU starts, the CPU fallback and llamafile-only `GPU=` values (`nvidia`, `amd`, ...) still use
  llamafile; `set ENGINE=llamafile` uses llamafile on the GPU too. If llama-server exits or hangs, the
  launcher tries **llamafile on the GPU** next, and only then the CPU. Linux (`start.sh`) still uses llamafile.
* **GPU workarounds (llamafile 0.10.6, Vulkan).** Every llamafile start gets `DISABLE_VULKAN_OBS_CAPTURE=1`
  and `VK_LOADER_LAYERS_DISABLE=~implicit~` (OBS Studio's Vulkan hook crashes it), and GPU starts pass
  `-fa off` (Vulkan flash attention can segfault). On NVIDIA compute capability 7.5 (Turing: RTX 20xx,
  GTX 16xx), `start.bat` also sets `GGML_VK_DISABLE_COOPMAT2=1` for llamafile (long prompts crash it
  otherwise). See *Troubleshooting*.
* **One copy at a time.** `start.bat` refuses to start while the kit, or any llamafile / llama-server, is
  already running (Windows lets a second AI server listen on port 8080 next to the first, without an error).
  Run `stop.bat` first; if the engine is not the kit's (`stop.bat` leaves those alone), close it or run `stop.bat all`.
* **Automatic CPU fallback.** If the AI exits by itself while it is using the GPU, or is still not answering
  `/health` after 60 s + 1 s per 50 MB of model (`HEALTH_TIMEOUT=` to change) while a GPU is in use, the
  launcher restarts it once with `--gpu disable`, re-picking the model for RAM (the GPU-sized one may not fit), and prints one
  *"GPU start failed (...) - fell back to the CPU"* line. On Windows, `start.bat` starts the AI with `start`,
  so it watches `tasklist` and polls `/health` with `curl.exe`; the AI runs through `tools\run-ai.bat`, which
  writes its output and exit code to `%TEMP%\offline-kit-ai.log`, and the launcher prints why it stopped
  (exit code, SIGSEGV, out of GPU memory, last log line) and keeps the log as `offline-kit-ai.crash.log`. The home page
  says the GPU start failed; later model switches stay on the CPU until you untick *Use CPU only* (or restart).
  Linux keeps the GPU attempt's log as `llamafile-gpu.log`. If CPU mode exits too, the launcher reports it
  and does not loop. No automatic fallback when you pinned a backend yourself (`GPU=vulkan` etc.).
  It only falls back after a real GPU attempt: the picker saw GPU memory for that start (or, on Linux,
  llamafile's log names a GPU device). With no GPU, or on the CPU, a slow load (big model, slow USB) is
  reported once and left loading - restarting it with `--gpu disable` would only make you wait again.
* Options: `MODEL=models/spare/other.gguf sh start.sh`, `CTX=4096` (less RAM), `GPU=disable`,
  `FA=on|off|auto` (flash attention; GPU default `off`), `NGL=<layers>` (GPU layers; default: llamafile
  decides), `REASONING=on|auto`. Thinking is **off** by default: Qwen3 1.7B/8B would otherwise spend the answer budget
  on hidden reasoning, and Ask-the-Manuals would look stuck.
  On Windows: `set MODEL=models\x.gguf` then `start.bat`.

## Troubleshooting
* **Chat / `localhost:8080` refused, or "AI: starting / not running" on the home page.** llamafile is still
  loading (10-60 s, longer from a slow USB stick) or has exited. Read the launcher window: it says when the
  AI is ready, and whether it fell back to the CPU. Logs on Linux: `$TMPDIR/offline-toolkit-logs/llamafile.log`
  (`llamafile-gpu.log` for a failed GPU attempt). On Windows: `%TEMP%\offline-kit-ai.log` (the current AI)
  and `%TEMP%\offline-kit-ai.crash.log` (the last one that stopped, with its exit code).
* **"The Offline Kit or an AI engine (llamafile / llama-server) is already running".** Open
  <http://localhost:8000/> if it is the kit, or run `stop.bat` and then `start.bat`. A stray llamafile /
  llama-server that is not the kit's: close it, or run `stop.bat all` (stops every one of them).
* **OBS Studio installed (Windows).** OBS registers an implicit Vulkan layer (`VK_LAYER_OBS_HOOK`,
  `C:\ProgramData\obs-studio-hook\obs-vulkan64.json`) that crashes llamafile 0.10.6's Vulkan init with
  0xC0000005, even at `-ngl 1`. The launchers set `DISABLE_VULKAN_OBS_CAPTURE=1` and
  `VK_LOADER_LAYERS_DISABLE=~implicit~` for llamafile, which fixes it and still uses the GPU. If you start
  llamafile yourself, set either one first.
* **Vulkan flash attention can segfault** on some NVIDIA cards/drivers (seen on an RTX 2070 Max-Q, driver
  551.61): with higher `-ngl` / context (qwen3-4b at `-c 8192` and 35+ GPU layers) llamafile exits with
  code 11 right after "Flash Attention enabled". It is not out of memory. The kit passes `-fa off` to llamafile
  on the GPU; `FA=on` turns it back on.
* **Long prompts crash llamafile on Turing GPUs** (RTX 2070 Max-Q, driver 621.14): short chats work, but a
  ~2,000-token prompt (Ask-the-Manuals, pasted logs) ends with "Terminating on uncaught SIGSEGV" as the prompt
  is processed - really an uncaught C++ exception (0xE06D7363) from the Vulkan `NV_cooperative_matrix2`
  (coopmat2) path. `-np 1`, `-ub`, `-ngl` and `-c` only move the crash point. llama-server (the Windows GPU
  engine) does not have it. For llamafile, `start.bat` sets `GGML_VK_DISABLE_COOPMAT2=1` on compute capability
  7.5; prompt reading is slower (~230-610 tok/s), answers are not. Set `GGML_VK_DISABLE_COOPMAT2=0` to keep
  coopmat2, or `=1` to force it off on other cards (ggml only checks that the variable exists; the launcher
  clears it for `0`).
* **NVIDIA on Windows uses Vulkan.** Neither engine in the kit has a CUDA backend, so `GPU=nvidia` is fatal;
  leave `GPU` unset (auto) or use `GPU=vulkan`. An installed CUDA toolkit makes no difference.
* **If the GPU still fails,** the launcher falls back to the CPU by itself (above). To skip the GPU entirely,
  tick *Use CPU only* on the home page, or start with `GPU=disable` (Windows: `set GPU=disable`).

## What's on the drive (prototype, ~5.1 GB)

```
start.sh / start.bat / stop.sh / stop.bat
bin/llamafile.exe            llamafile 0.10.6 - ONE Actually Portable Executable for Windows + Linux (+ macOS), x86-64 & ARM64
bin/linux/                   kiwix-serve 3.8.2, miniserve 0.35.0 (static Linux x86-64)
bin/windows/                 kiwix-serve.exe 3.8.1 (+ ICU DLLs), miniserve.exe 0.35.0 (Windows x86-64)
bin/llama-vulkan/            llama.cpp b12626 llama-server.exe + DLLs (Vulkan, Windows x86-64, ~89 MB) - the Windows GPU engine
models/                      qwen3-4b-instruct-2507-q4_k_m.gguf (2.5 GB) + qwen3-1.7b-q4_k_m.gguf (1.1 GB, for 4 GB laptops), Apache-2.0
library/                     Kiwix ZIMs: WikiMed mini (166 MB), zimgit Water, Medicine, Knots (~115 MB), Wikivoyage nopic, Outdoors SE
                             (saved without the date in the name, e.g. wikipedia_en_medicine_mini.zim; KIT-INFO.json records the dated source)
www/index.html               home page
www/docs/                    Ask-the-Manuals (RAG): index.js (prebuilt), rag.js, pdf/, notes/, lib/pdf.js
www/maps/                    australia.pmtiles (2.2 GB, all of Australia to street level) + MapLibre viewer, fonts, sprites
tools/build_index.py         rebuild the manuals index (needs python3 + pdftotext; only for rebaking)
tools/pick-model.sh / .ps1   automatic model choice used by start.sh / start.bat
tools/build_kit.py           builds the whole kit from tools/kit-manifest.json (hash-pinned downloads)
tools/publish_r2.py          publishes a built kit zip to Cloudflare R2 (create-only versions, verify, manifests, retention 3/2/1)
tools/build_site.py          builds the GitHub Pages downloads page from the public R2 manifests
tools/publish_gcs.py         the old GCS uploader (no longer used for kits; kept with its tests)
tools/gcs_cache.sh           Cloud Build's shared download cache in the private GCS bucket (hash-checked, create-only)
tools/cloudsdk_python.sh     runs publish_r2.py with the cloud-sdk image's own Python
scripts/setup-gcp.sh         one-time, re-runnable GCP setup (cache bucket, Workload Identity Federation, service accounts,
                             Secret Manager secrets for R2)
cloudbuild.yaml              medium/large kit builds on Cloud Build (submitted by the publish-kit workflow)
tools/download-extras.sh     commented download list for one-off Medium/Large additions
KIT-INFO.json, SHA256SUMS    what was built, from which commit, with the hash of every file
```

## Build a kit

None of the large content is in git. `tools/build_kit.py` downloads it from the original publishers,
**checks each file against the sha256 pinned in `tools/kit-manifest.json`**, and lays out the folder:

```sh
python3 tools/build_kit.py --check              # list items, sizes, and what is still unpinned
python3 tools/build_kit.py --zip                # small tier -> dist/offline-toolkit-small/ (+ .zip)
python3 tools/build_kit.py --tier medium        # or large; each tier includes the ones below it
python3 tools/build_kit.py --lock               # pin new or changed downloads, then review and commit
```

Needs python3, `pdftotext` (poppler-utils), Go 1.25 or newer (for go-pmtiles, the map extractor) and `libzim`
(`pip install --require-hashes --only-binary :all: -r tools/requirements-build.txt`, to pull three
manuals out of the pinned zimgit ZIMs). Prebuilt kits are
published to Google Cloud Storage by the manually started `publish-kit` workflow. See
[docs/SETUP.md](docs/SETUP.md) for the security model, one-time GCP/GitHub setup and costs.

### Chat
The home page's **Chat** tile opens the kit's own chat page (`www/chat/`), a small client for llamafile's
OpenAI-compatible API on port 8080 (same host, so LAN mode works). It keeps an *"AI can make mistakes. Always
cross-check answers against the Library or the manuals..."* notice pinned on screen, and sends a short kit system
prompt (offline emergency assistant; practical and safe, no odd or unsafe suggestions; say when unsure; call 000
in Australia; no medication doses or wild-food identification). Replies use the same safe markdown as Ask the
Manuals. llamafile's own chat page (port 8080) is still linked from there for its extra settings; it shows
neither the notice nor the kit prompt (its system message is a per-browser setting in that page).
The notice text lives in `tools/ai-notice.json` and is shared by the home, chat and manuals pages and the
downloads site.

### Ask the Manuals (RAG) - how it works
Python-free at run time. `www/docs/index.js` holds ~2,200 pre-chunked passages from the PDFs. The page does
BM25 keyword search in the browser (`www/docs/retrieve.js`: light stemming, so purify / purifying /
purification match, and a "safe / drinkable water" question also looks for bleach, boiling, chlorine and iodine
passages), sends the top 6 sources to the local model, each with the next passage of its page (a table is often
split across two passages; all sources together are sized to the AI's context from `models.json`: ~4,750
characters at 4096, 9,000 at 8192 and up), and tells it to answer only the question asked, only from these sources, citing
[n], and to quote any dose or quantity exactly as the source states it (or say that no source gives one).
Answers render a small, safe markdown subset (`www/md.js`: bold, italics, lists, headings, code; model output
is escaped first, and the only links are the [n] citations, each opening the exact PDF page). A notice next to
the answer says the AI can make mistakes and to cross-check the linked sources.
Seeded with: FM 21-76 US Army Survival Manual; FM 4-25.11 First Aid (2002); USMC Wilderness Medicine Course;
Ready.gov/FEMA "Are You Ready?"; Washington State DoH "Purifying Water During an Emergency"; and
`notes/Australia-first-aid-and-emergency-notes.txt` (kit-written summary of Australian practice - 000, snake bite
pressure-immobilisation, box jellyfish vinegar, etc.). The Australian notes are boosted and the model is told to
follow them where they and an older US manual disagree (the 2002 US Army manual recommends a *constricting band*
for snake bite, which is **wrong for Australia**).

Add documents:
* **Any OS, no tools:** Ask-the-Manuals -> "add your own PDFs" (parsed in-browser with pdf.js, stored in that browser only).
* **Bake into the drive:** copy PDFs to `www/docs/pdf/` (titles in `tools/pdf-titles.json`, kept out of the PDF shelf), text notes to `www/docs/notes/`,
  then run `python3 tools/build_index.py`.

### Maps
`www/maps/australia.pmtiles` is a Protomaps (OpenStreetMap) vector extract of Australia (bbox 112.5,-44,154,-9.5,
zoom 0-15) shown with MapLibre GL. The Protomaps build it came from is in `KIT-INFO.json` (`protomaps_build`). Pan/zoom to street level, coordinates under the cursor,
"locate me" button (works on the laptop via localhost; phones need GPS + HTTPS, so on a phone use **Organic Maps**).
No offline address search/routing in the browser viewer.
**Phone recommendation:** Organic Maps (free, open source, Android/iOS) - download the Australia regions while
online; it does offline search and turn-by-turn routing. Kiwix also has Android/iOS apps that open the same `.zim` files.
Refresh the map: `pmtiles extract https://build.protomaps.com/<YYYYMMDD>.pmtiles australia.pmtiles --bbox=112.5,-44,154,-9.5`
(go-pmtiles, ~2.2 GB download; current builds are listed in <https://build-metadata.protomaps.dev/builds.json>).
Protomaps deletes daily builds after about a week. `tools/build_kit.py` tries the build named in
`tools/kit-manifest.json` first; if it is gone (HTTP 404) it uses the newest build that `builds.json`
lists with the same tile schema major version (`tiles_major`), or the next newest if that fails. It logs a warning and records the build in `KIT-INFO.json`,
so an old date in the manifest never breaks a build.

## Recommended tiers

Format the drive **exFAT** (FAT32 can't hold files over 4 GB; NTFS is awkward on some Linux). A USB 3 **SSD**
is much faster than a cheap stick for loading models and searching Wikipedia.

| Tier | Drive | Model(s) | Library / content | Laptop RAM |
|---|---|---|---|---|
| **Small (this prototype)** | 16 GB stick | Qwen3-4B-Instruct-2507 Q4 (2.5 GB); optional Qwen3-1.7B Q4 (1.1 GB) for old laptops | WikiMed mini, zimgit Water/Medicine/Knots, manuals, Australia map (2.2 GB); + Wikivoyage nopic 0.26 GB, Outdoors StackExchange 0.14 GB. ~6-7 GB | 8 GB (4 GB with the 1.7B model) |
| **Medium** | 64 GB SSD/stick | + Qwen3-8B Q4 (5.0 GB, noticeably smarter); + Gemma-3-4B vision (2.5 GB + 0.85 GB mmproj) for "what is this?" photos | + Wikipedia EN mini (13 GB), WikiMed maxi (1.7 GB), iFixit (3.3 GB), Wikibooks nopic (3.3 GB), DIY StackExchange (1.9 GB), Wikivoyage maxi (1 GB), zimgit post-disaster (0.6 GB) + food prep. ~40 GB | 16 GB |
| **Large** | 256 GB SSD | + Qwen3-30B-A3B-Instruct-2507 Q4 (18.6 GB, MoE - fast on CPU for its size); + Gemma-3-12B vision (7.3 + 0.85 GB) or Qwen3-VL-8B (5.0 + 1.2 GB); optional "uncensored" Dolphin 3.0 Llama-3.1-8B (4.9 GB) or gemma-3-12b-abliterated (7.3 GB) | Swap Wikipedia mini for **Wikipedia EN maxi with images (119 GB)** (or nopic 49 GB). ~175 GB | 32 GB |

Rule of thumb: RAM needed ≈ model file size + 1-3 GB (context) + ~2 GB for the OS/browser. The 4B model
answered at ~14-15 words/s on 8 CPU cores here; expect 3-8 on an older laptop, 2-3x slower for 8B,
and an NVIDIA/AMD GPU (if present) is used automatically. Bigger models also drain a battery/generator faster.
"Uncensored"/abliterated models only refuse less - they are *not* more accurate and are more confidently wrong.
(Model names/sizes as of Nov 2026; newer equivalents are fine - any llama.cpp GGUF works.)

## Caveats - read this
* **AI can be confidently wrong.** Always open the cited page. The small model sometimes garbles details
  (e.g. it shortened "add 1 minute per 300 m altitude" to "per elevation").
* **Never** use the AI (or any photo app) to decide a wild plant or mushroom is safe to eat. Death caps grow in Australia.
* **No medication doses from the AI.** Use the packet, a pharmacist, Healthdirect 1800 022 222, or 000.
* Many manuals are American and dated (2002). Australian first aid guidance (ANZCOR) differs - snake/funnel-web bite,
  bluebottle vs box jellyfish, etc. The included Australian notes are a summary, not training: do a first aid course.
* See **Licences** below. The zimgit "post-disaster" ZIM bundles some commercial books (e.g. SAS Survival Handbook),
  so it is opt-in only and must not be redistributed.
* Windows parts were **not run** on a real Windows machine yet (see Testing).
* Keep a paper copy of the essentials (first aid card, local evacuation map, contacts) - laptops die.

## Testing (done on a Linux box, 16 Nov 2026)
* `start.sh` started all three servers; `/health` ok; home 200; Kiwix 200.
* Chat: "power out for 3 days in Brisbane - top 5 things" -> sensible answer, 15.4 tok/s (CPU only).
* RAG: "How do I purify water?" -> cited answer, links to FM 21-76 p.61-62 (text verified on the page);
  "How do I treat a snake bite?" -> pressure-immobilisation answer citing the Australian notes, and flagged the
  conflicting US Army advice.
* Kiwix: opened WikiMed "Snakebite" article and the zimgit Medicine shelf.
* Maps: Brisbane at zoom 9 and the CBD at zoom 15.5 rendered.
* All of the above ran in Chrome with **every non-localhost request blocked** - zero external requests.
* Model switching (Linux, llamafile 0.10.6, tiny test GGUFs): the home page listed both models, *Use* restarted
  llamafile on the spare model, and the page reloaded showing it. `tools/pick-model.ps1` passes the same cases
  as the sh picker under PowerShell 7. Windows-only probes (CIM, registry, `nvidia-smi.exe`) were not run on Windows.
* GPU workarounds, Windows (RTX 2070 Max-Q, 32 GB RAM, driver 551.61, llamafile 0.10.6 Vulkan; tested on
  that laptop, not by CI): with the OBS env vars and `-fa off` the server reached 32 tok/s on the GPU.
* Long-prompt crash and the llama-server engine, Windows (same RTX 2070 Max-Q, driver 621.14, medium kit;
  tested on that laptop, not by CI), with a 2,462- and a 7,587-token prompt: llamafile crashed on both at
  every `-np`/`-ub`/`-ngl`/`-c` setting that kept the whole context, and survived with
  `GGML_VK_DISABLE_COOPMAT2=1` (4 runs each). llama-server b12626 Vulkan survived both with coopmat2 on
  (6 runs). The real `start.bat` was run from Explorer: GPU start on llama-server (all layers on the GPU),
  long prompts, model switches (4B, 8B, Gemma vision with its projector), *Use CPU only* on/off, a second
  start refused, llama-server killed -> llamafile on the GPU, llamafile killed -> CPU, exit codes logged.
  `tools/pick-model.ps1` read `compute_cap` 7.5 from the real `nvidia-smi`.
* CPU fallback and *Use CPU only* (`tests/test_cpu_fallback.sh`, stub llamafile/miniserve, Linux): GPU exit
  and GPU hang (no `/health`) both restart once on the CPU with the model re-picked for RAM and one
  "fell back" line; with no GPU detected, a slow or hanging load is left running and an exit is only
  reported (no `--gpu disable` restart), unless the log shows a Vulkan device; `-fa off` on GPU starts only; OBS env vars on every start; FA/NGL/GPU overrides; no
  fallback with `GPU=vulkan`; no restart loop when CPU mode exits; slow CPU starts are not killed; the toggle
  on/off (also after a fallback), keeping a hand-picked model, and rejecting bad flags / wrong token.
  `tests/test_pick_model*.sh` cover the request validation in both pickers; `tests/test_start_bat.py` checks
  `start.bat` statically. The home-page toggle was checked in headless Chromium (state shown, correct request
  sent, disabled when opened from another host). `start.bat` itself was not run.
* Windows: binaries verified as PE x86-64 (`llamafile.exe` is an APE polyglot with a valid PE header); not executed.

## Licences
* **Code** in this repository (launchers, web pages, scripts): MIT - see `LICENSE`.
* **Content is not in this repository**; build scripts download it from the original publishers. Each item keeps its own licence:
  * US federal manuals (FM 21-76, FM 4-25.11, USMC, FEMA/Ready.gov): public domain.
  * Wikipedia / WikiMed / Wikivoyage / Wikibooks ZIMs: CC BY-SA (attribution travels with the ZIM). StackExchange ZIMs: CC BY-SA.
  * Map data: (c) OpenStreetMap contributors, ODbL; Protomaps basemap; MapLibre (BSD-3), pdf.js (Apache-2.0), Noto fonts (OFL).
  * llamafile (Apache-2.0), llama.cpp (MIT; `bin/llama-vulkan/LICENSE-llama.cpp`, plus LLVM OpenMP's
    licence for `libomp.dll`), kiwix-tools (GPL-3.0), miniserve (MIT). Models: Qwen3 (Apache-2.0); Gemma (Gemma terms); Llama-based models (Llama licence).
  * Hesperian books inside the zimgit medicine ZIM (e.g. "Where There Is No Doctor"): free for personal/non-profit use.
  * **Do not redistribute commercial books.** The zimgit post-disaster ZIM contains some, so it stays opt-in (`build_kit.py --with post-disaster`), marks the kit `"redistributable": false`, and `publish_r2.py` refuses to upload such a kit.
* `www/docs/notes/Australia-first-aid-and-emergency-notes.txt` is **not medical advice** - check current ANZCOR / St John / Red Cross guidance.
