#!/usr/bin/env bash
# Share big downloads between Cloud Build runs through the kit bucket, so each pinned file is fetched from
# upstream once. Used only by cloudbuild.yaml (which only the protected publish-kit workflow submits, as the
# kit-builder service account); pull requests never run it and have no Google credentials.
#
#   tools/gcs_cache.sh pull PLAN BUCKET CACHE_DIR   # before the downloads
#   tools/gcs_cache.sh push PLAN BUCKET CACHE_DIR   # after them (never fails the build)
#
# PLAN comes from `build_kit.py --cache-plan` and has one "<object> <cache file> <sha256|->" per line:
#   cache/sha256/<sha256>       a pinned download, named by its sha256
#   cache/pmtiles/<cache file>  a map extract (no pin possible), with its sha256 in <object>.sha256
#
# The cache is only a faster source, never a trusted one:
#   * pull: build_kit.py checks every pinned file against the manifest's sha256 before use and downloads it
#     again from upstream if it does not match. Map extracts are checked here against their .sha256.
#   * push: only files that build_kit.py verified (CACHE_DIR/.verified) are uploaded, create-only
#     (--if-generation-match=0, and the service account cannot overwrite or delete objects), so a cache
#     object can never be replaced once written.
# Each object logs its size, time and MB/s.
set -euo pipefail

mode="${1:-}"; plan="${2:-}"; bucket="${3:-}"; dir="${4:-}"
case "$mode" in pull|push) ;; *) echo "usage: $0 pull|push PLAN BUCKET CACHE_DIR" >&2; exit 2 ;; esac
[ -f "$plan" ] && [ -d "$dir" ] || { echo "$0: plan $plan or cache dir $dir missing" >&2; exit 2; }
echo "$bucket" | grep -Eqx '[a-z0-9][a-z0-9._-]{1,61}[a-z0-9]' || { echo "$0: bad bucket name" >&2; exit 2; }
export CLOUDSDK_CORE_DISABLE_PROMPTS=1
# One stream per object: parallel composite uploads need delete permission (the SA has none) and make
# objects without an md5.
export CLOUDSDK_STORAGE_PARALLEL_COMPOSITE_UPLOAD_ENABLED=False

now() { date +%s.%N; }
rate() {  # bytes start end
  awk -v b="$1" -v s="$2" -v e="$3" 'BEGIN { d = e - s; if (d < 0.001) d = 0.001;
    printf "%.0f MB in %dm%02ds (%.1f MB/s)", b / 1e6, int(d / 60), int(d) % 60, b / 1e6 / d }'
}
size_of() { stat -c %s "$1"; }
verified_sha() {  # file -> sha256 build_kit.py recorded for it (last record wins), or nothing
  [ -f "$dir/.verified" ] || return 0
  awk -v f="$1" '$2 == f { h = $1 } END { if (h != "") print h }' "$dir/.verified"
}
errlog=$(mktemp); trap 'rm -f "$errlog"' EXIT
hits=0; misses=0; pushed=0; skipped=0; failed=0
t_all=$(now)

while read -r obj file sha extra <&3; do  # fd 3: gcloud must not eat the plan from stdin
  [ -n "${obj:-}" ] || continue
  if [ -n "${extra:-}" ] || ! echo "$obj" | grep -Eqx 'cache/(sha256/[0-9a-f]{64}|pmtiles/[0-9a-f]{16}-[A-Za-z0-9._-]+)' \
     || ! echo "$file" | grep -Eqx '[0-9a-f]{16}-[A-Za-z0-9._-]+' || ! echo "$sha" | grep -Eqx '[0-9a-f]{64}|-'; then
    echo "$0: bad plan line: $obj $file $sha" >&2; exit 2
  fi
  case "$obj" in cache/sha256/*) [ "$obj" = "cache/sha256/$sha" ] || { echo "$0: object/sha mismatch: $obj" >&2; exit 2; } ;; esac
  url="gs://$bucket/$obj"; dst="$dir/$file"

  if [ "$mode" = pull ]; then
    if [ -e "$dst" ]; then echo "  $file: already on disk"; continue; fi
    t0=$(now)
    if ! gcloud storage cp "$url" "$dst.gcspart" >"$errlog" 2>&1; then
      rm -f "$dst.gcspart"
      if grep -qiE 'not found|no urls matched|404' "$errlog"; then echo "  $file: miss"
      else echo "  $file: miss (cache read failed: $(tail -n 2 "$errlog" | tr '\n' ' '))"; fi
      misses=$((misses + 1)); continue
    fi
    if [ "$sha" = - ]; then  # map extract: no manifest pin, so check the hash stored next to it
      want=$(gcloud storage cat "$url.sha256" 2>/dev/null | head -c 64 || true)
      got=$(sha256sum "$dst.gcspart" | cut -d' ' -f1)
      if ! echo "$want" | grep -Eqx '[0-9a-f]{64}' || [ "$got" != "$want" ]; then
        echo "  $file: cached copy does not match its .sha256; ignoring it"
        rm -f "$dst.gcspart"; misses=$((misses + 1)); continue
      fi
    fi
    mv "$dst.gcspart" "$dst"
    echo "$file" >> "$dir/.gcs-pulled"
    echo "  $file: hit, $(rate "$(size_of "$dst")" "$t0" "$(now)")"
    hits=$((hits + 1))
  else
    if [ ! -f "$dst" ]; then echo "  $file: not on disk, skipped"; skipped=$((skipped + 1)); continue; fi
    if [ -f "$dir/.gcs-pulled" ] && grep -qxF "$file" "$dir/.gcs-pulled"; then
      echo "  $file: came from the cache"; skipped=$((skipped + 1)); continue
    fi
    h=$(verified_sha "$file")
    if [ -z "$h" ] || { [ "$sha" != - ] && [ "$h" != "$sha" ]; }; then
      echo "  $file: not verified by build_kit.py, not shared"; skipped=$((skipped + 1)); continue
    fi
    if gcloud storage objects describe "$url" >/dev/null 2>&1; then
      echo "  $file: already cached"; skipped=$((skipped + 1)); continue
    fi
    t0=$(now)
    if gcloud storage cp --if-generation-match=0 "$dst" "$url" >"$errlog" 2>&1; then
      if [ "$sha" = - ]; then
        printf '%s\n' "$h" | gcloud storage cp --if-generation-match=0 - "$url.sha256" >"$errlog" 2>&1 ||
          echo "  WARNING $file: uploaded, but its .sha256 was not ($(tail -n 1 "$errlog")); later pulls will ignore it"
      fi
      echo "  $file: pushed, $(rate "$(size_of "$dst")" "$t0" "$(now)")"
      pushed=$((pushed + 1))
    else
      echo "  WARNING $file: push failed: $(tail -n 2 "$errlog" | tr '\n' ' ')"
      failed=$((failed + 1))
    fi
  fi
done 3< "$plan"

secs=$(awk -v s="$t_all" -v e="$(now)" 'BEGIN { printf "%d", e - s }')
if [ "$mode" = pull ]; then echo "cache pull: $hits hit(s), $misses miss(es) in ${secs}s"
else echo "cache push: $pushed pushed, $skipped skipped, $failed failed in ${secs}s"; fi
exit 0
