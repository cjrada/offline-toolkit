#!/bin/sh
# Offline tests for tools/gcs_cache.sh with a stub `gcloud` that keeps "objects" in a local folder.
#   sh tests/test_gcs_cache.sh
set -eu
ROOT=$(cd "$(dirname "$0")/.." && pwd)
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
mkdir -p "$T/bin" "$T/gcs" "$T/cache"
fail() { echo "FAIL: $*"; exit 1; }

# stub gcloud: storage cp SRC DST [--if-generation-match=0], storage cat URL, storage objects describe URL
cat > "$T/bin/gcloud" <<'EOF'
#!/bin/sh
store="$GCS_STUB"; echo "gcloud $*" >> "$store/../calls"
[ "$1" = storage ] || exit 9
shift
obj() { echo "$store/$(echo "$1" | sed 's#^gs://##; s#/#__#g')"; }
case "$1" in
  cp)
    shift; nc=0; args=""
    for a in "$@"; do case "$a" in --if-generation-match=0) nc=1 ;; -*) [ "$a" = - ] && args="$args $a" ;; *) args="$args $a" ;; esac; done
    # shellcheck disable=SC2086
    set -- $args
    case "$1" in
      gs://*) [ -f "$(obj "$1")" ] || { echo "ERROR: No URLs matched: $1" >&2; exit 1; }; cp "$(obj "$1")" "$2" ;;
      *) d=$(obj "$2"); [ "$nc" = 1 ] && [ -f "$d" ] && { echo "412 Precondition Failed" >&2; exit 1; }
         if [ "$1" = - ]; then cat > "$d"; else cp "$1" "$d"; fi ;;
    esac ;;
  cat) [ -f "$(obj "$2")" ] || exit 1; cat "$(obj "$2")" ;;
  objects) [ -f "$(obj "$3")" ] ;;
  *) exit 9 ;;
esac
EOF
chmod +x "$T/bin/gcloud"
export PATH="$T/bin:$PATH" GCS_STUB="$T/gcs"
put() { printf '%s' "$2" > "$T/gcs/$(echo "b-kits/$1" | sed 's#/#__#g')"; }
has() { [ -f "$T/gcs/$(echo "b-kits/$1" | sed 's#/#__#g')" ]; }
sha() { printf '%s' "$1" | sha256sum | cut -d' ' -f1; }

A=$(sha aaa); B=$(sha bbb); M=$(sha map)
nA="${A%"${A#????????????????}"}-a.zim"; nB="${B%"${B#????????????????}"}-b.zim"; nM="0123456789abcdef-australia.pmtiles"
cat > "$T/plan" <<EOF
cache/sha256/$A $nA $A
cache/sha256/$B $nB $B
cache/pmtiles/$nM $nM -
EOF

# ---- pull: A is cached, B is not, the map is cached with a matching .sha256
put "cache/sha256/$A" aaa
put "cache/pmtiles/$nM" map
put "cache/pmtiles/$nM.sha256" "$M"
out=$(bash "$ROOT/tools/gcs_cache.sh" pull "$T/plan" b-kits "$T/cache")
echo "$out"
[ "$(cat "$T/cache/$nA")" = aaa ] || fail "A not pulled"
[ ! -e "$T/cache/$nB" ] || fail "B should be a miss"
[ "$(cat "$T/cache/$nM")" = map ] || fail "map not pulled"
echo "$out" | grep -q "$nB: miss" || fail "miss not logged"
echo "$out" | grep -q "$nA: hit, .* MB/s" || fail "hit speed not logged"
grep -qx "$nA" "$T/cache/.gcs-pulled" || fail ".gcs-pulled not written"

# ---- pull ignores a map whose .sha256 does not match
rm -f "$T/cache/$nM" "$T/cache/.gcs-pulled"
put "cache/pmtiles/$nM.sha256" "$(sha other)"
bash "$ROOT/tools/gcs_cache.sh" pull "$T/plan" b-kits "$T/cache" | grep -q "$nM: cached copy does not match" || fail "bad map sha not reported"
[ ! -e "$T/cache/$nM" ] || fail "bad map kept"
printf '%s\n' "$nA" > "$T/cache/.gcs-pulled"

# ---- push: B downloaded and verified -> pushed create-only; an unverified file is not shared; A came from the cache
printf bbb > "$T/cache/$nB"; printf map > "$T/cache/$nM"
echo "$B  $nB  3 0 0" > "$T/cache/.verified"
out=$(bash "$ROOT/tools/gcs_cache.sh" push "$T/plan" b-kits "$T/cache")
echo "$out"
has "cache/sha256/$B" || fail "B not pushed"
grep -q -- "cp --if-generation-match=0 $T/cache/$nB gs://b-kits/cache/sha256/$B" "$T/calls" || fail "push not create-only"
echo "$out" | grep -q "$nM: not verified" || fail "unverified map was shared"
echo "$out" | grep -q "$nA: came from the cache" || fail "pulled file re-pushed"

# ---- push of a verified map writes its .sha256; a second push finds everything cached
echo "$M  $nM  3 0 0" >> "$T/cache/.verified"
rm -f "$T/gcs/b-kits__cache__pmtiles__$nM" "$T/gcs/b-kits__cache__pmtiles__$nM.sha256"
bash "$ROOT/tools/gcs_cache.sh" push "$T/plan" b-kits "$T/cache" >/dev/null
has "cache/pmtiles/$nM.sha256" || fail "map .sha256 not pushed"
[ "$(cat "$T/gcs/b-kits__cache__pmtiles__$nM.sha256")" = "$M" ] || fail "wrong map .sha256"
bash "$ROOT/tools/gcs_cache.sh" push "$T/plan" b-kits "$T/cache" | grep -q "0 pushed" || fail "second push re-uploaded"

# ---- a verified hash that differs from the plan's sha is not shared
echo "$(sha zzz)  $nB  3 0 0" >> "$T/cache/.verified"
rm -f "$T/gcs/b-kits__cache__sha256__$B"
bash "$ROOT/tools/gcs_cache.sh" push "$T/plan" b-kits "$T/cache" | grep -q "$nB: not verified" || fail "mismatched hash shared"

# ---- a push failure warns but never fails the build
printf '#!/bin/sh\necho "gcloud $*" >> "%s/calls"; case "$*" in *describe*) exit 1;; esac; echo "403 Forbidden" >&2; exit 1\n' "$T" > "$T/bin/gcloud"
echo "$B  $nB  3 0 0" >> "$T/cache/.verified"
out=$(bash "$ROOT/tools/gcs_cache.sh" push "$T/plan" b-kits "$T/cache") || fail "push failure failed the step"
echo "$out" | grep -q "WARNING $nB: push failed" || fail "push failure not reported"
# and a read failure on pull is just a miss
rm -f "$T/cache/$nB"
bash "$ROOT/tools/gcs_cache.sh" pull "$T/plan" b-kits "$T/cache" | grep -q "$nB: miss (cache read failed" || fail "read error not a miss"

# ---- bad plans are refused
printf 'cache/sha256/../../kits/x %s %s\n' "$nA" "$A" > "$T/badplan"
if bash "$ROOT/tools/gcs_cache.sh" pull "$T/badplan" b-kits "$T/cache" 2>/dev/null; then fail "bad object accepted"; fi
printf 'cache/sha256/%s ../evil %s\n' "$A" "$A" > "$T/badplan"
if bash "$ROOT/tools/gcs_cache.sh" pull "$T/badplan" b-kits "$T/cache" 2>/dev/null; then fail "bad file name accepted"; fi
printf 'cache/sha256/%s %s %s\n' "$A" "$nA" "$B" > "$T/badplan"
if bash "$ROOT/tools/gcs_cache.sh" pull "$T/badplan" b-kits "$T/cache" 2>/dev/null; then fail "object/sha mismatch accepted"; fi

# ---- tools/cloudsdk_python.sh: no python3 on PATH (as in the cloud-sdk image) -> gcloud's own python
PY=$(command -v python3); SH=$(command -v sh)
mkdir -p "$T/nopy"
printf '#!/bin/sh\n[ "$*" = "info --format=value(basic.python_location)" ] && echo "%s"\n' "$PY" > "$T/nopy/gcloud"
chmod +x "$T/nopy/gcloud"
got=$(PATH="$T/nopy" "$SH" "$ROOT/tools/cloudsdk_python.sh" -c 'import sys; print(sys.flags.isolated)') || fail "cloudsdk_python.sh found no python"
[ "$got" = 1 ] || fail "cloudsdk_python.sh did not run python -I"
printf '#!/bin/sh\nexit 1\n' > "$T/nopy/gcloud"
if PATH="$T/nopy" "$SH" "$ROOT/tools/cloudsdk_python.sh" -c 'pass' 2>/dev/null; then fail "no python should fail"; fi
echo "ok: gcs_cache.sh pull/push, cloudsdk_python.sh"
