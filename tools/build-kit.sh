#!/usr/bin/env bash
# Build a ready-to-copy Offline Survival Kit from the original publishers.
#
#   bash tools/build-kit.sh [--tier base|small|medium|large] [options]
#
# Output (default dist/):
#   dist/<tier>/offline-survival-kit/        the kit folder - copy its contents to an exFAT drive
#   dist/<tier>/offline-survival-kit-<tier>-<version>.zip
#   dist/<tier>/SHA256SUMS, manifest.json    checksums / metadata of the zip
#   dist/<tier>/kit.lock                     exact URL + SHA-256 of everything downloaded
#   dist/cache/                              downloads (re-used by later builds)
#
# Integrity: every download is checked against a SHA-256. The hash comes from tools/kit.lock
# (pinned, reviewed in git) or - for items not yet pinned - from the publisher (Hugging Face
# LFS etag, Kiwix .sha256, GitHub release digest). Items with no publisher checksum are
# trusted on first download and flagged. --strict (used by CI publishing) refuses anything
# that is not pinned in tools/kit.lock. To pin/refresh: run with --update, review the
# generated dist/<tier>/kit.lock, and copy it over tools/kit.lock in a reviewed commit.
#
# Options:
#   --tier T              base (code, binaries, web libs, manuals; no models/ZIMs/map), small (default), medium, large
#   --out DIR             output directory (default: dist)
#   --version V           version label (default: UTC date YYYY.MM.DD)
#   --lock FILE           lock file to read (default: tools/kit.lock)
#   --strict              every download must be pinned in the lock file
#   --update              ignore the lock file; resolve newest versions (writes a fresh kit.lock)
#   --map-build YYYYMMDD  Protomaps daily build to extract the map from (default: lock, else newest)
#   --skip-map            no map extract (saves 2.2 GB download + time)
#   --no-zip              only build the folder
#   --with-uncensored     add Dolphin 3.0 Llama-3.1-8B to models/spare/ (manual use only)
#   --with-post-disaster  add zimgit post-disaster ZIM (contains commercial books: personal use only,
#                         marks the kit NOT-FOR-REDISTRIBUTION; CI refuses to publish it)
# Needs: bash, curl, python3, tar, unzip, zip, sha256sum, pdftotext (poppler-utils);
#        for the map: go (builds go-pmtiles from the checksummed Go module proxy) or pmtiles on PATH.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)

# ---- Pinned tool / library versions -------------------------------------------------------
LLAMAFILE_VER=0.10.6        # github.com/mozilla-ai/llamafile
MINISERVE_VER=0.35.0        # github.com/svenstaro/miniserve
KIWIX_LINUX_VER=3.8.2       # download.kiwix.org/release/kiwix-tools
KIWIX_WIN_VER=3.8.1
PDFJS_VER=6.5.438           # npm pdfjs-dist
MAPLIBRE_VER=5.24.1         # npm maplibre-gl (5.x: last line with a UMD build for <script src>)
PMTILES_JS_VER=4.5.0        # npm pmtiles
BASEMAPS_VER=5.7.2          # npm @protomaps/basemaps
GO_PMTILES_VER=v1.31.2      # go module github.com/protomaps/go-pmtiles
MAP_BBOX=112.5,-44,154,-9.5 # Australia
MAP_MAXZOOM=15
HF=https://huggingface.co
KZ=https://download.kiwix.org

TIER=small OUT=$ROOT/dist VERSION=$(date -u +%Y.%m.%d) LOCK=$ROOT/tools/kit.lock
STRICT=0 UPDATE=0 SKIP_MAP=0 NO_ZIP=0 MAP_BUILD="" WITH_UNCENSORED=0 WITH_POST_DISASTER=0

die(){ echo "build-kit: ERROR: $*" >&2; exit 1; }
log(){ echo "build-kit: $*" >&2; }
warn(){ echo "build-kit: WARNING: $*" >&2; WARNINGS+=("$*"); }
WARNINGS=()

while [ $# -gt 0 ]; do
  case $1 in
    --tier) TIER=${2:?}; shift ;;
    --out) OUT=${2:?}; shift ;;
    --version) VERSION=${2:?}; shift ;;
    --lock) LOCK=${2:?}; shift ;;
    --strict) STRICT=1 ;;
    --update) UPDATE=1 ;;
    --map-build) MAP_BUILD=${2:?}; shift ;;
    --skip-map) SKIP_MAP=1 ;;
    --no-zip) NO_ZIP=1 ;;
    --with-uncensored) WITH_UNCENSORED=1 ;;
    --with-post-disaster) WITH_POST_DISASTER=1 ;;
    -h|--help) sed -n '2,/^set -euo/p' "$0" | sed 's/^# \{0,1\}//; $d'; exit 0 ;;
    *) die "unknown option $1 (see --help)" ;;
  esac
  shift
done
case $TIER in base|small|medium|large) ;; *) die "--tier must be base, small, medium or large" ;; esac
[[ $VERSION =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]] || die "--version may only contain letters, digits, . _ -"
[[ -z $MAP_BUILD || $MAP_BUILD =~ ^[0-9]{8}$ ]] || die "--map-build must be YYYYMMDD"
(( STRICT && UPDATE )) && die "--strict and --update are mutually exclusive"
for t in curl python3 tar unzip sha256sum openssl; do command -v "$t" >/dev/null || die "missing tool: $t"; done
(( NO_ZIP )) || command -v zip >/dev/null || die "missing tool: zip (or use --no-zip)"

rank(){ case $1 in base) echo 0;; small) echo 1;; medium) echo 2;; large) echo 3;; esac; }
tier_ge(){ [ "$(rank "$TIER")" -ge "$(rank "$1")" ]; }   # tier_ge small: small, medium, large
tier_in(){ local t; for t in "$@"; do [ "$TIER" = "$t" ] && return 0; done; return 1; }

mkdir -p "$OUT"; OUT=$(cd "$OUT" && pwd)
TDIR=$OUT/$TIER; KIT=$TDIR/offline-survival-kit; CACHE=$OUT/cache
[[ $KIT == "$OUT"/*/offline-survival-kit ]] || die "refusing odd kit path $KIT"
rm -rf "$KIT"; mkdir -p "$KIT" "$CACHE/tmp"
NEWLOCK=$TDIR/kit.lock
printf '# offline-toolkit kit.lock - key<TAB>url<TAB>sha256. Generated %s, tier %s.\n' \
  "$(date -u +%FT%TZ)" "$TIER" > "$NEWLOCK"

# ---- Lock file --------------------------------------------------------------------------------
declare -A LOCK_URL=() LOCK_SHA=()
if (( ! UPDATE )) && [ -f "$LOCK" ]; then
  while IFS=$'\t' read -r k u h; do
    [[ -z $k || $k == \#* ]] && continue
    [[ $u == https://* ]] || die "$LOCK: $k: URL must be https"
    [[ $h =~ ^[0-9a-f]{64}$ || $h == - ]] || die "$LOCK: $k: bad sha256"
    LOCK_URL[$k]=$u; LOCK_SHA[$k]=$h
  done < "$LOCK"
  log "loaded ${#LOCK_URL[@]} pinned entries from $LOCK"
elif (( STRICT )); then
  die "--strict needs a lock file ($LOCK)"
fi

# ---- HTTP helpers -------------------------------------------------------------------------------
CURL=(curl --proto "=https" --proto-redir "=https" -fsSL --retry 5 --retry-delay 5 --connect-timeout 30)
json(){ python3 -c 'import json,sys
d=json.load(sys.stdin)
for k in sys.argv[1].split("."):
    d = d[int(k)] if isinstance(d, list) else d[k]
print(d)' "$1"; }
sha256(){ sha256sum "$1" | cut -c1-64; }

# Resolvers print "URL SHA256|-". They only run when the item is not pinned in the lock file.
r_static(){ echo "$1 ${2:--}"; }
r_hf(){ # repo file  -> immutable /resolve/<commit>/ URL + LFS sha256 from the x-linked-etag header
  local rev url h
  rev=$("${CURL[@]}" "$HF/api/models/$1/revision/main" | json sha)
  [[ $rev =~ ^[0-9a-f]{40}$ ]] || die "HF: bad revision for $1"
  url="$HF/$1/resolve/$rev/$2"
  h=$(curl --proto =https -fsSI --retry 3 "$url" | tr -d '\r' |
      awk 'tolower($1)=="x-linked-etag:" { gsub(/"/, "", $2); print $2 }' | head -1)
  [[ $h =~ ^[0-9a-f]{64}$ ]] || h=-
  echo "$url $h"
}
r_gh_asset(){ # owner/repo tag asset -> release asset URL + GitHub's recorded sha256 digest
  local d url h
  d=$("${CURL[@]}" -H 'Accept: application/vnd.github+json' "https://api.github.com/repos/$1/releases/tags/$2")
  read -r url h < <(python3 -c 'import json,sys
d=json.load(sys.stdin)
for a in d["assets"]:
    if a["name"] == sys.argv[1]:
        dg = a.get("digest") or ""
        print(a["browser_download_url"], dg[7:] if dg.startswith("sha256:") else "-"); break
else: sys.exit("asset not found: " + sys.argv[1])' "$3" <<<"$d")
  echo "$url ${h:--}"
}
r_gh_archive(){ # owner/repo ref -> tarball of the exact commit (no publisher hash: trust on first use)
  local sha
  sha=$("${CURL[@]}" -H 'Accept: application/vnd.github+json' "https://api.github.com/repos/$1/commits/$2" | json sha)
  [[ $sha =~ ^[0-9a-f]{40}$ ]] || die "GitHub: bad commit for $1@$2"
  echo "https://github.com/$1/archive/$sha.tar.gz -"
}
r_kiwix_file(){ # full URL -> URL + Kiwix-published .sha256
  local h; h=$("${CURL[@]}" "$1.sha256" | awk '{print $1}') || h=-
  [[ $h =~ ^[0-9a-f]{64}$ ]] || h=-
  echo "$1 $h"
}
r_kiwix_zim(){ # folder book -> newest dated ZIM for that book
  local f
  f=$("${CURL[@]}" "$KZ/zim/$1/" | grep -oE "href=\"$2_[0-9]{4}-[0-9]{2}\.zim\"" |
      sed 's/^href="//; s/"$//' | sort -u | tail -1)
  [ -n "$f" ] || die "Kiwix: no ZIM found for $1/$2"
  r_kiwix_file "$KZ/zim/$1/$f"
}
r_npm(){ # package version -> registry tarball (hash pinned in kit.lock; npm's sha512 is also checked)
  local meta url integ
  meta=$("${CURL[@]}" "https://registry.npmjs.org/$1/$2")
  url=$(json dist.tarball <<<"$meta"); integ=$(json dist.integrity <<<"$meta")
  echo "$url - $integ"
}

# fetch KEY RESOLVER ARGS... -> verified file path in $FETCHED; appends to the new lock
FETCHED=""
fetch(){
  local key=$1; shift
  local url sha res integ="" f got
  if [[ -n ${LOCK_URL[$key]:-} ]]; then
    url=${LOCK_URL[$key]}; sha=${LOCK_SHA[$key]}
    (( STRICT )) && [[ $sha == - ]] && die "--strict: $key has no pinned sha256 in $LOCK"
  else
    (( STRICT )) && die "--strict: $key is not pinned in $LOCK (run --update locally, review, commit tools/kit.lock)"
    res=$("$@") || die "could not resolve $key"
    read -r url sha integ <<<"$res"
  fi
  [[ $url == https://* ]] || die "$key: refusing non-https URL $url"
  f=$CACHE/${key//\//__}
  if [[ -f $f && $sha != - && $(sha256 "$f") == "$sha" ]]; then
    log "cached   $key"
  else
    log "download $key <- $url"
    rm -f "$f.part"
    "${CURL[@]}" --retry-all-errors -o "$f.part" "$url" || { rm -f "$f.part"; die "download failed: $url"; }
    got=$(sha256 "$f.part")
    if [[ $sha != - && $got != "$sha" ]]; then
      rm -f "$f.part"; die "SHA-256 mismatch for $key: expected $sha, got $got"
    fi
    if [[ -n $integ ]]; then # npm: also check the registry's sha512 integrity
      [[ "sha512-$(openssl dgst -sha512 -binary "$f.part" | base64 -w0)" == "$integ" ]] ||
        { rm -f "$f.part"; die "npm integrity mismatch for $key"; }
    fi
    [[ $sha == - && -z $integ ]] && warn "$key: publisher gives no checksum - trusting first download (sha256 $got); review before pinning"
    sha=$got; mv "$f.part" "$f"
  fi
  printf '%s\t%s\t%s\n' "$key" "$url" "$sha" >> "$NEWLOCK"
  FETCHED=$f
}

place(){ # src dest(relative to kit): hard-link from cache (no extra disk), else copy
  mkdir -p "$(dirname "$KIT/$2")"; ln -f "$1" "$KIT/$2" 2>/dev/null || cp -f "$1" "$KIT/$2"
}
npm_files(){ # tarball destdir file... (paths inside package/)
  local tgz=$1 dst=$2 x; shift 2
  local t; t=$(mktemp -d "$CACHE/tmp/npm.XXXXXX")
  tar -xzf "$tgz" -C "$t" "${@/#/package/}"
  mkdir -p "$KIT/$dst"; for x in "$@"; do cp "$t/package/$x" "$KIT/$dst/"; done
  rm -rf "$t"
}

# ---- 1. Kit code from this repository -------------------------------------------------------
log "tier=$TIER version=$VERSION out=$TDIR"
if git -C "$ROOT" rev-parse --git-dir >/dev/null 2>&1; then
  COMMIT=$(git -C "$ROOT" rev-parse HEAD)
  git -C "$ROOT" diff --quiet HEAD -- || warn "working tree has uncommitted changes (kit includes them)"
  (cd "$ROOT" && git ls-files -z --cached --others --exclude-standard | grep -zvE '^(\.github/|\.gitignore$)' | tar --null -T - -cf -) | tar -xf - -C "$KIT"
else
  COMMIT=unknown
  (cd "$ROOT" && tar -cf - --exclude=dist --exclude=.git ./*) | tar -xf - -C "$KIT"
fi

# ---- 2. Programs ------------------------------------------------------------------------------
fetch bin/llamafile r_gh_asset mozilla-ai/llamafile "$LLAMAFILE_VER" "llamafile-$LLAMAFILE_VER"
place "$FETCHED" bin/llamafile.exe
fetch bin/miniserve-linux r_gh_asset svenstaro/miniserve "v$MINISERVE_VER" "miniserve-$MINISERVE_VER-x86_64-unknown-linux-musl"
place "$FETCHED" bin/linux/miniserve
fetch bin/miniserve-windows r_gh_asset svenstaro/miniserve "v$MINISERVE_VER" "miniserve-$MINISERVE_VER-x86_64-pc-windows-msvc.exe"
place "$FETCHED" bin/windows/miniserve.exe
fetch bin/kiwix-tools-linux r_kiwix_file "$KZ/release/kiwix-tools/kiwix-tools_linux-x86_64-$KIWIX_LINUX_VER.tar.gz"
T=$(mktemp -d "$CACHE/tmp/kiwix.XXXXXX"); tar -xzf "$FETCHED" -C "$T"
K=$(find "$T" -type f -name kiwix-serve | head -1); [ -n "$K" ] || die "kiwix-serve not in archive"
mkdir -p "$KIT/bin/linux"; cp "$K" "$KIT/bin/linux/kiwix-serve"; rm -rf "$T"
fetch bin/kiwix-tools-windows r_kiwix_file "$KZ/release/kiwix-tools/kiwix-tools_win-x86_64-$KIWIX_WIN_VER.zip"
T=$(mktemp -d "$CACHE/tmp/kiwix.XXXXXX"); unzip -q "$FETCHED" -d "$T"
K=$(find "$T" -type f -name kiwix-serve.exe | head -1); [ -n "$K" ] || die "kiwix-serve.exe not in archive"
mkdir -p "$KIT/bin/windows"; cp "$K" "$(dirname "$K")"/*.dll "$KIT/bin/windows/" 2>/dev/null || cp "$K" "$KIT/bin/windows/"
rm -rf "$T"
chmod +x "$KIT/bin/llamafile.exe" "$KIT/bin/linux/"*

# ---- 3. Web libraries (npm registry) + map styles ---------------------------------------------
fetch npm/pdfjs-dist r_npm pdfjs-dist "$PDFJS_VER"
npm_files "$FETCHED" www/docs/lib build/pdf.min.mjs build/pdf.worker.min.mjs
fetch npm/maplibre-gl r_npm maplibre-gl "$MAPLIBRE_VER"
npm_files "$FETCHED" www/maps/lib dist/maplibre-gl.js dist/maplibre-gl.css
fetch npm/pmtiles r_npm pmtiles "$PMTILES_JS_VER"
npm_files "$FETCHED" www/maps/lib dist/pmtiles.js
fetch npm/protomaps-basemaps r_npm @protomaps/basemaps "$BASEMAPS_VER"
npm_files "$FETCHED" www/maps/lib dist/basemaps.js
fetch web/basemaps-assets r_gh_archive protomaps/basemaps-assets main
T=$(mktemp -d "$CACHE/tmp/assets.XXXXXX"); tar -xzf "$FETCHED" -C "$T"
A=$(find "$T" -mindepth 1 -maxdepth 1 -type d | head -1)
mkdir -p "$KIT/www/maps/fonts" "$KIT/www/maps/sprites"
cp -r "$A"/fonts/Noto\ Sans* "$KIT/www/maps/fonts/"   # the "light" flavour only uses Noto Sans
cp -r "$A/sprites/v4" "$KIT/www/maps/sprites/"
rm -rf "$T"

# ---- 4. Manuals (PDFs) + search index ---------------------------------------------------------
# tools/sources/pdfs.tsv: file<TAB>url. A "-" URL means "no stable official URL known yet": put
# the PDF in www/docs/pdf/ yourself (it is git-ignored) and it is copied in; otherwise skipped.
while IFS=$'\t' read -r file url; do
  [[ -z $file || $file == \#* ]] && continue
  [[ $file =~ ^[A-Za-z0-9._-]+\.pdf$ ]] || die "pdfs.tsv: bad file name $file"
  if [[ $url == https://* ]]; then
    fetch "pdf/$file" r_static "$url"; place "$FETCHED" "www/docs/pdf/$file"
  elif [ -f "$ROOT/www/docs/pdf/$file" ]; then
    cp "$ROOT/www/docs/pdf/$file" "$KIT/www/docs/pdf/$file"; warn "pdf/$file: copied from local www/docs/pdf (not verified)"
  else
    warn "pdf/$file: no URL in tools/sources/pdfs.tsv and no local copy - skipped"
  fi
done < "$ROOT/tools/sources/pdfs.tsv"
if command -v pdftotext >/dev/null; then
  python3 "$KIT/tools/build_index.py"
else
  (( STRICT )) && die "pdftotext missing (apt install poppler-utils)"
  warn "pdftotext missing - Ask-the-Manuals index not built"
fi

# ---- 5. Models ------------------------------------------------------------------------------------
# Everything in models/ is a candidate for auto-selection (tools/pick-model.sh). A vision projector
# is named "<model>-mmproj-*.gguf" so it pairs with its model. models/spare/ is manual-only.
model(){ fetch "models/$1" r_hf "$2" "$3"; place "$FETCHED" "models/$1"; }
if tier_ge small; then
  model qwen3-4b-instruct-2507-q4_k_m.gguf unsloth/Qwen3-4B-Instruct-2507-GGUF Qwen3-4B-Instruct-2507-Q4_K_M.gguf  # 2.5 GB
  model qwen3-1.7b-q4_k_m.gguf unsloth/Qwen3-1.7B-GGUF Qwen3-1.7B-Q4_K_M.gguf                                       # 1.1 GB, 4 GB laptops
fi
if tier_ge medium; then
  model qwen3-8b-q4_k_m.gguf unsloth/Qwen3-8B-GGUF Qwen3-8B-Q4_K_M.gguf                                             # 5.0 GB
  model gemma-3-4b-it-q4_k_m.gguf unsloth/gemma-3-4b-it-GGUF gemma-3-4b-it-Q4_K_M.gguf                              # 2.5 GB vision
  model gemma-3-4b-it-q4_k_m-mmproj-f16.gguf unsloth/gemma-3-4b-it-GGUF mmproj-F16.gguf                             # 0.85 GB
fi
if tier_ge large; then
  model qwen3-30b-a3b-instruct-2507-q4_k_m.gguf unsloth/Qwen3-30B-A3B-Instruct-2507-GGUF Qwen3-30B-A3B-Instruct-2507-Q4_K_M.gguf  # 18.6 GB
  model gemma-3-12b-it-q4_k_m.gguf unsloth/gemma-3-12b-it-GGUF gemma-3-12b-it-Q4_K_M.gguf                          # 7.3 GB vision
  model gemma-3-12b-it-q4_k_m-mmproj-f16.gguf unsloth/gemma-3-12b-it-GGUF mmproj-F16.gguf                           # 0.85 GB
fi
if (( WITH_UNCENSORED )); then
  model spare/dolphin3-llama3.1-8b-q4_k_m.gguf bartowski/Dolphin3.0-Llama3.1-8B-GGUF Dolphin3.0-Llama3.1-8B-Q4_K_M.gguf
fi

# ---- 6. Library (Kiwix ZIMs) ------------------------------------------------------------------------
zim(){ fetch "library/$2.zim" r_kiwix_zim "$1" "$2"; place "$FETCHED" "library/$2.zim"; }
if tier_ge small; then
  zim other zimgit-water_en; zim other zimgit-medicine_en; zim other zimgit-knots_en
  zim stack_exchange outdoors.stackexchange.com_en_all
fi
tier_in small && { zim wikipedia wikipedia_en_medicine_mini; zim wikivoyage wikivoyage_en_all_nopic; }
if tier_ge medium; then
  zim wikipedia wikipedia_en_medicine_maxi; zim wikivoyage wikivoyage_en_all_maxi
  zim ifixit ifixit_en_all; zim wikibooks wikibooks_en_all_nopic
  zim stack_exchange diy.stackexchange.com_en_all; zim other zimgit-food-preparation_en
fi
tier_in medium && zim wikipedia wikipedia_en_all_mini
tier_in large && zim wikipedia wikipedia_en_all_maxi
if (( WITH_POST_DISASTER )); then
  zim other zimgit-post-disaster_en
  cat > "$KIT/NOT-FOR-REDISTRIBUTION.txt" <<'EOF'
This kit contains library/zimgit-post-disaster_en.zim, which bundles commercial books.
It is for your own personal use only. Do not share, upload or publish this kit.
EOF
  warn "post-disaster ZIM included: this kit must NOT be redistributed"
fi

# ---- 7. Map (Protomaps extract of Australia) ------------------------------------------------------
if tier_ge small && (( ! SKIP_MAP )); then
  if [[ -n $MAP_BUILD ]]; then SRC="https://build.protomaps.com/$MAP_BUILD.pmtiles"
  elif [[ -n ${LOCK_URL[map/source]:-} ]]; then SRC=${LOCK_URL[map/source]}
  else
    (( STRICT )) && die "--strict: map/source not pinned"
    SRC=""
    for i in 1 2 3 4 5 6 7 8 9 10; do
      d=$(date -u -d "-$i day" +%Y%m%d)
      if curl --proto =https -fsSI "https://build.protomaps.com/$d.pmtiles" >/dev/null 2>&1; then
        SRC="https://build.protomaps.com/$d.pmtiles"; break; fi
    done
    [ -n "$SRC" ] || die "no recent Protomaps build found (use --map-build YYYYMMDD)"
  fi
  [[ $SRC =~ ^https://build\.protomaps\.com/[0-9]{8}\.pmtiles$ ]] || die "unexpected map source $SRC"
  printf '%s\t%s\t%s\n' map/source "$SRC" - >> "$NEWLOCK"
  OUTMAP=$CACHE/australia-$(basename "$SRC" .pmtiles)-z$MAP_MAXZOOM.pmtiles
  if [ ! -s "$OUTMAP" ]; then
    PMT=$(command -v pmtiles || true)
    if [ -z "$PMT" ]; then
      command -v go >/dev/null || die "need 'go' (or 'pmtiles' on PATH) for the map, or use --skip-map"
      log "building go-pmtiles $GO_PMTILES_VER (verified by the Go checksum database)"
      GOBIN=$CACHE/gobin go install "github.com/protomaps/go-pmtiles@$GO_PMTILES_VER"
      PMT=$CACHE/gobin/go-pmtiles
    fi
    log "extracting map from $SRC (~2.2 GB)"
    "$PMT" extract "$SRC" "$OUTMAP.part" --bbox="$MAP_BBOX" --maxzoom="$MAP_MAXZOOM"
    mv "$OUTMAP.part" "$OUTMAP"
  fi
  MAPSHA=$(sha256 "$OUTMAP")
  if [[ -n ${LOCK_SHA[map/australia.pmtiles]:-} && ${LOCK_URL[map/source]:-} == "$SRC" &&
        ${LOCK_SHA[map/australia.pmtiles]} != - && ${LOCK_SHA[map/australia.pmtiles]} != "$MAPSHA" ]]; then
    warn "map extract differs from the pinned hash (same source build) - check go-pmtiles version"
  fi
  printf '%s\t%s\t%s\n' map/australia.pmtiles "$SRC" "$MAPSHA" >> "$NEWLOCK"
  place "$OUTMAP" www/maps/australia.pmtiles
fi

# ---- 8. Kit info, checksums, zip ----------------------------------------------------------------
{
  echo "Offline Survival Kit - tier $TIER, version $VERSION"
  echo "Built $(date -u +%FT%TZ) from offline-toolkit commit $COMMIT"
  echo "Start: Windows start.bat | Linux sh start.sh | then http://localhost:8000/"
  echo "Verify the drive: sha256sum -c SHA256SUMS   (Linux, from this folder)"
  echo
  echo "Contents (see README.md 'Licences'; each item keeps its publisher's licence):"
  grep -v '^#' "$NEWLOCK" | cut -f1,2 | sed 's/\t/  <-  /; s/^/  /'
  if [ ${#WARNINGS[@]} -gt 0 ]; then echo; echo "Build warnings:"; printf '  - %s\n' "${WARNINGS[@]}"; fi
} > "$KIT/KIT-INFO.txt"
(cd "$KIT" && find . -type f ! -name SHA256SUMS -print0 | LC_ALL=C sort -z | xargs -0 sha256sum > SHA256SUMS)

if (( ! NO_ZIP )); then
  NAME=offline-survival-kit-$TIER-$VERSION; (( WITH_POST_DISASTER )) && NAME=$NAME-PRIVATE
  ZIP=$TDIR/$NAME.zip; rm -f "$ZIP"
  log "zipping $ZIP"
  # Big files are already compressed: store them (-n) so zipping is fast.
  (cd "$TDIR" && zip -r -q -n .gguf:.zim:.pmtiles:.exe:.png:.pbf "$ZIP" offline-survival-kit)
  (cd "$TDIR" && sha256sum "$NAME.zip" > SHA256SUMS)
  python3 - "$TDIR/manifest.json" "$NAME.zip" "$TIER" "$VERSION" "$COMMIT" "$(stat -c %s "$ZIP")" \
            "$(cut -c1-64 "$TDIR/SHA256SUMS")" "$WITH_POST_DISASTER" <<'EOF'
import json, sys, datetime
out, z, tier, ver, commit, size, sha, private = sys.argv[1:]
json.dump({"name": z, "tier": tier, "version": ver, "commit": commit, "size": int(size), "sha256": sha,
           "redistributable": private != "1",
           "built": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")},
          open(out, "w"), indent=1)
EOF
fi
rm -rf "$CACHE/tmp"
log "done: $KIT ($(du -sh "$KIT" | cut -f1))"
[ ${#WARNINGS[@]} -eq 0 ] || { log "${#WARNINGS[@]} warning(s):"; printf '  - %s\n' "${WARNINGS[@]}" >&2; }
(( UPDATE )) && log "review $NEWLOCK, then: cp $NEWLOCK tools/kit.lock && git diff tools/kit.lock"
exit 0
