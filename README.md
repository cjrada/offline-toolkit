# Offline Survival Kit (DIY, free, open source)

A do-it-yourself version of the "AI offline survival drive" idea: copy this folder to a USB stick or
external SSD, plug it into an ordinary **Windows 10/11 or Linux** laptop (64-bit), and run one file.
No install, no internet, no accounts. Everything runs on `localhost` in your web browser.

> **Not medical advice.** Check current guidance (ANZCOR, St John, Red Cross, Healthdirect). In an emergency call **000**.

## Get a kit onto a drive

Prebuilt kits live in a private Google Cloud Storage bucket (set up with `scripts/setup-gcp.sh`, see
[docs/SETUP.md](docs/SETUP.md)). With the Google Cloud CLI (`gcloud auth login` first):

```sh
B=gs://offline-toolkit-kits                         # your bucket
gcloud storage cat $B/kits/small/latest.json        # shows "version" and "zip"
V=<version from latest.json>
gcloud storage cp "$B/kits/small/$V/offline-toolkit-small-$V.zip" "$B/kits/small/$V/offline-toolkit-small-$V.zip.sha256" .
sha256sum -c offline-toolkit-small-$V.zip.sha256    # must print: OK
```

On Windows (PowerShell), check the hash with `(Get-FileHash offline-toolkit-small-$V.zip).Hash` and
compare it with the first word of the `.sha256` file.

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
| AI chat (general assistant) + API used by Ask-the-Manuals | `llamafile` 0.10.6 (llama.cpp) | 8080 |
| Offline library (wiki / guides) | `kiwix-serve` 3.8.x | 8081 |

## Start / stop

* **Windows:** double-click `start.bat` (or `start.bat lan` to let phones on the same Wi-Fi connect). Stop with `stop.bat`.
  First run: Windows SmartScreen / Defender may ask about `llamafile.exe`, `kiwix-serve.exe`, `miniserve.exe` -
  choose *More info -> Run anyway*; allow the firewall prompt only if you want LAN/phone access.
* **Linux:** `sh start.sh` (or `sh start.sh lan`). Ctrl+C or `sh stop.sh` to stop.
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
  * The page uploads a one-line request to a second miniserve that listens on **127.0.0.1:8079 only**,
    into a private temp folder.
  * The launcher accepts it only if it names an existing model in `models/` or `models/spare/`, then
    restarts llamafile.
  * Phones in LAN mode can see the panel but cannot switch.
  * No Python is involved. llamafile 0.10.6 includes llama.cpp's multi-model "router" code, but its
    command line always loads exactly one `-m` model, so the kit restarts it instead.
* Options: `MODEL=models/spare/other.gguf sh start.sh`, `CTX=4096` (less RAM), `GPU=disable`.
  On Windows: `set MODEL=models\x.gguf` then `start.bat`.

## What's on the drive (prototype, ~5.1 GB)

```
start.sh / start.bat / stop.sh / stop.bat
bin/llamafile.exe            llamafile 0.10.6 - ONE Actually Portable Executable for Windows + Linux (+ macOS), x86-64 & ARM64
bin/linux/                   kiwix-serve 3.8.2, miniserve 0.35.0 (static Linux x86-64)
bin/windows/                 kiwix-serve.exe 3.8.1 (+ ICU DLLs), miniserve.exe 0.35.0 (Windows x86-64)
models/                      qwen3-4b-instruct-2507-q4_k_m.gguf (2.5 GB) + qwen3-1.7b-q4_k_m.gguf (1.1 GB, for 4 GB laptops), Apache-2.0
library/                     Kiwix ZIMs: WikiMed mini (166 MB), zimgit Water, Medicine, Knots (~115 MB), Wikivoyage nopic, Outdoors SE
                             (saved without the date in the name, e.g. wikipedia_en_medicine_mini.zim; KIT-INFO.json records the dated source)
www/index.html               home page
www/docs/                    Ask-the-Manuals (RAG): index.js (prebuilt), rag.js, pdf/, notes/, lib/pdf.js
www/maps/                    australia.pmtiles (2.2 GB, all of Australia to street level) + MapLibre viewer, fonts, sprites
tools/build_index.py         rebuild the manuals index (needs python3 + pdftotext; only for rebaking)
tools/pick-model.sh / .ps1   automatic model choice used by start.sh / start.bat
tools/build_kit.py           builds the whole kit from tools/kit-manifest.json (hash-pinned downloads)
tools/publish_gcs.py         uploads a built kit zip to Google Cloud Storage (create-only; used by CI and Cloud Build)
scripts/setup-gcp.sh         one-time, re-runnable GCP setup (bucket, Workload Identity Federation, service accounts)
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

Needs python3, `pdftotext` (poppler-utils), Go (for go-pmtiles, the map extractor) and `libzim`
(`pip install --require-hashes --only-binary :all: -r tools/requirements-build.txt`, to pull three
manuals out of the pinned zimgit ZIMs). Prebuilt kits are
published to Google Cloud Storage by the manually started `publish-kit` workflow. See
[docs/SETUP.md](docs/SETUP.md) for the security model, one-time GCP/GitHub setup and costs.

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
* Model switching (Linux, llamafile 0.10.6, tiny test GGUFs): the home page listed both models, *Use* restarted
  llamafile on the spare model, and the page reloaded showing it. `tools/pick-model.ps1` passes the same cases
  as the sh picker under PowerShell 7. Windows-only probes (CIM, registry, `nvidia-smi.exe`) were not run on Windows.
* Windows: binaries verified as PE x86-64 (`llamafile.exe` is an APE polyglot with a valid PE header); not executed.

## Licences
* **Code** in this repository (launchers, web pages, scripts): MIT - see `LICENSE`.
* **Content is not in this repository**; build scripts download it from the original publishers. Each item keeps its own licence:
  * US federal manuals (FM 21-76, FM 4-25.11, USMC, FEMA/Ready.gov): public domain.
  * Wikipedia / WikiMed / Wikivoyage / Wikibooks ZIMs: CC BY-SA (attribution travels with the ZIM). StackExchange ZIMs: CC BY-SA.
  * Map data: (c) OpenStreetMap contributors, ODbL; Protomaps basemap; MapLibre (BSD-3), pdf.js (Apache-2.0), Noto fonts (OFL).
  * llamafile (Apache-2.0), kiwix-tools (GPL-3.0), miniserve (MIT). Models: Qwen3 (Apache-2.0); Gemma (Gemma terms); Llama-based models (Llama licence).
  * Hesperian books inside the zimgit medicine ZIM (e.g. "Where There Is No Doctor"): free for personal/non-profit use.
  * **Do not redistribute commercial books.** The zimgit post-disaster ZIM contains some, so it stays opt-in (`build_kit.py --with post-disaster`), marks the kit `"redistributable": false`, and `publish_gcs.py` refuses to upload such a kit.
* `www/docs/notes/Australia-first-aid-and-emergency-notes.txt` is **not medical advice** - check current ANZCOR / St John / Red Cross guidance.
