# Offline Survival Kit (DIY, free, open source)

A do-it-yourself version of the "AI offline survival drive" idea: copy this folder to a USB stick or
external SSD, plug it into an ordinary **Windows 10/11 or Linux** laptop (64-bit), and run one file.
No install, no internet, no accounts. Everything runs on `localhost` in your web browser.

| What | Tool | Port |
|---|---|---|
| Home page, Ask-the-Manuals, Maps, PDF shelf | `miniserve` (static web server) | 8000 |
| AI chat (general assistant) + API used by Ask-the-Manuals | `llamafile` 0.10.6 (llama.cpp) | 8080 |
| Offline library (wiki / guides) | `kiwix-serve` 3.8.x | 8081 |

## Start / stop

* **Windows:** double-click `start.bat` (or `start.bat lan` to let phones on the same Wi-Fi connect). Stop with `stop.bat`.
  First run: Windows SmartScreen / Defender may ask about `llamafile.exe`, `kiwix-serve.exe`, `miniserve.exe` -
  choose *More info -> Run anyway*; allow the firewall prompt only if you want LAN/phone access.
* **Linux:** `sh start.sh` (or `sh start.sh lan`). Ctrl+C or `sh stop.sh` to stop.
  Works even if the drive is mounted `noexec` (it runs llamafile via `sh` and copies the two small static binaries to /tmp).
* Then open <http://localhost:8000/>. The AI needs ~10-60 s to load (longer from a slow USB stick).
* Options: `MODEL=models/spare/other.gguf sh start.sh`, `CTX=4096` (less RAM), `GPU=disable`, `VISION=1`.
  On Windows: `set MODEL=models\x.gguf` then `start.bat`.

### Model selection (automatic)
The launchers pick the **biggest model in `models/` that fits this laptop's RAM** (`tools/pick-model.sh`,
`tools/pick-model.ps1` on Windows). RAM needed ≈ model file (+ vision projector) + context cache
(~1.5 GB at 8k tokens) + ~2 GB for the OS/browser. If nothing fits, the context is shrunk to 4k/2k, and
as a last resort the smallest model is used with a warning. So one drive works on a 4 GB netbook
(Qwen3-1.7B), an 8 GB laptop (Qwen3-4B), 16 GB (Qwen3-8B) and 32 GB (Qwen3-30B-A3B).
* Plain text models are preferred; `VISION=1` prefers a vision model. A vision model's projector must be
  named `<model>-mmproj-*.gguf` next to it (e.g. `gemma-3-4b-it-q4_k_m.gguf` +
  `gemma-3-4b-it-q4_k_m-mmproj-f16.gguf`).
* `models/spare/` is never auto-picked (e.g. "uncensored" models) - use `MODEL=`.
* See what it would choose: `sh tools/pick-model.sh` (pretend: `KIT_RAM_MB=4096 sh tools/pick-model.sh`).

## Building a kit (`tools/build-kit.sh`)
On an internet-connected Linux box (needs `curl python3 tar unzip zip openssl pdftotext`; `go` for the map):

```sh
bash tools/build-kit.sh --tier small        # base | small | medium | large
# -> dist/small/offline-survival-kit/  (copy its contents to the drive) + .zip + SHA256SUMS
```
Every download is verified against a SHA-256 - pinned in `tools/kit.lock`, otherwise the publisher's own
checksum (Hugging Face, Kiwix, GitHub release digests, npm integrity); anything without one is flagged.
`--strict` accepts pinned hashes only; `--update` re-resolves newest versions and writes a new lock to review.
Other flags: `--skip-map`, `--no-zip`, `--map-build YYYYMMDD`, `--with-uncensored`, `--with-post-disaster`
(personal use only - marks the kit not redistributable). Manuals come from `tools/sources/pdfs.tsv`; PDFs
without a stable official URL there are copied from your local `www/docs/pdf/` if present.
Each kit has `KIT-INFO.txt` (sources + build warnings) and `SHA256SUMS` (check the drive: `sha256sum -c SHA256SUMS`).

**Prebuilt kits** (small, base) are published by CI to Google Cloud Storage - see `docs/publishing.md`
for the download layout and the (keyless, reviewer-gated) security setup.

## What's on the drive (prototype, ~5.1 GB)

```
start.sh / start.bat / stop.sh / stop.bat
bin/llamafile.exe            llamafile 0.10.6 - ONE Actually Portable Executable for Windows + Linux (+ macOS), x86-64 & ARM64
bin/linux/                   kiwix-serve 3.8.2, miniserve 0.35.0 (static Linux x86-64)
bin/windows/                 kiwix-serve.exe 3.8.1 (+ ICU DLLs), miniserve.exe 0.35.0 (Windows x86-64)
models/                      qwen3-4b-instruct-2507-q4_k_m.gguf (2.5 GB, Apache-2.0)
library/                     Kiwix ZIMs: WikiMed mini (166 MB), zimgit Water, Medicine, Knots (~115 MB)
www/index.html               home page
www/docs/                    Ask-the-Manuals (RAG): index.js (prebuilt), rag.js, pdf/, notes/, lib/pdf.js
www/maps/                    australia.pmtiles (2.2 GB, all of Australia to street level) + MapLibre viewer, fonts, sprites
tools/build_index.py         rebuild the manuals index (needs python3 + pdftotext; only for rebaking)
tools/build-kit.sh           build a whole tier from the publishers, checksum-verified (see below)
tools/pick-model.sh / .ps1   automatic model choice by RAM (used by start.sh / start.bat)
tools/download-extras.sh     commented download list for hand-picking extras
```

### Ask the Manuals (RAG) - how it works
Python-free at run time. `www/docs/index.js` holds ~2,200 pre-chunked passages from the PDFs. The page does
BM25 keyword search in the browser, sends the top 6 passages to the local model with strict "answer only from
these sources, cite [n]" instructions, and shows every citation as a link that opens the exact PDF page.
Seeded with: FM 21-76 US Army Survival Manual; FM 4-25.11 First Aid (2002); USMC Wilderness Medicine Course;
Ready.gov/FEMA "Are You Ready?"; Washington State DoH "Purifying Water During an Emergency"; and
`notes/Australia-first-aid-and-emergency-notes.txt` (kit-written summary of Australian practice - 000, snake bite
pressure-immobilisation, box jellyfish vinegar, etc.). The Australian notes are boosted and the model is told they
override the older US manuals (the 2002 US Army manual recommends a *constricting band* for snake bite, which is
**wrong for Australia**).

Add documents:
* **Any OS, no tools:** Ask-the-Manuals -> "add your own PDFs" (parsed in-browser with pdf.js, stored in that browser only).
* **Bake into the drive:** copy PDFs to `www/docs/pdf/` (titles in `pdf/titles.json`), text notes to `www/docs/notes/`,
  then run `python3 tools/build_index.py`.

### Maps
`www/maps/australia.pmtiles` is a Protomaps (OpenStreetMap) vector extract of Australia (bbox 112.5,-44,154,-9.5,
zoom 0-15, build 2026-11-15) shown with MapLibre GL. Pan/zoom to street level, coordinates under the cursor,
"locate me" button (works on the laptop via localhost; phones need GPS + HTTPS, so on a phone use **Organic Maps**).
No offline address search/routing in the browser viewer.
**Phone recommendation:** Organic Maps (free, open source, Android/iOS) - download the Australia regions while
online; it does offline search and turn-by-turn routing. Kiwix also has Android/iOS apps that open the same `.zim` files.
Refresh the map: `pmtiles extract https://build.protomaps.com/<YYYYMMDD>.pmtiles australia.pmtiles --bbox=112.5,-44,154,-9.5`
(go-pmtiles, ~2.2 GB download).

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
* Windows: binaries verified as PE x86-64 (`llamafile.exe` is an APE polyglot with a valid PE header); not executed.

## Licences
* **Code** in this repository (launchers, web pages, scripts): MIT - see `LICENSE`.
* **Content is not in this repository**; build scripts download it from the original publishers. Each item keeps its own licence:
  * US federal manuals (FM 21-76, FM 4-25.11, USMC, FEMA/Ready.gov): public domain.
  * Wikipedia / WikiMed / Wikivoyage / Wikibooks ZIMs: CC BY-SA (attribution travels with the ZIM). StackExchange ZIMs: CC BY-SA.
  * Map data: (c) OpenStreetMap contributors, ODbL; Protomaps basemap; MapLibre (BSD-3), pdf.js (Apache-2.0), Noto fonts (OFL).
  * llamafile (Apache-2.0), kiwix-tools (GPL-3.0), miniserve (MIT). Models: Qwen3 (Apache-2.0); Gemma (Gemma terms); Llama-based models (Llama licence).
  * Hesperian books inside the zimgit medicine ZIM (e.g. "Where There Is No Doctor"): free for personal/non-profit use.
  * **Do not redistribute commercial books.** The zimgit post-disaster ZIM contains some, so it stays opt-in (`--with-post-disaster`) and is never published in built kits.
* Published kits contain only redistributable items; each zip's `KIT-INFO.txt` lists every source URL.
* `www/docs/notes/Australia-first-aid-and-emergency-notes.txt` is **not medical advice** - check current ANZCOR / St John / Red Cross guidance.
