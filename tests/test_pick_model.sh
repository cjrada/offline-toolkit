#!/bin/sh
# Tests for tools/pick-model.sh using sparse fake model files. Usage: sh tests/test_pick_model.sh
set -u
HERE="$(cd "$(dirname "$0")/.." && pwd)"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
mkdir -p "$T/models/spare"
truncate -s 1100M "$T/models/qwen3-1.7b-q4_k_m.gguf"
truncate -s 2500M "$T/models/qwen3-4b-instruct-2507-q4_k_m.gguf"
truncate -s 5000M "$T/models/qwen3-8b-q4_k_m.gguf"
truncate -s 900M  "$T/models/qwen3-8b-q4_k_m.mmproj.gguf"   # must never be picked as a model
truncate -s 100M  "$T/models/spare/tiny.gguf"                # spare/ is never auto-picked
fail=0

check() { # name, RAM_MB, expected model basename, expected ctx, [expected mmproj basename]
  out=$(cd "$T" && unset MODEL CTX; RAM_MB=$2 MODELS_DIR=models sh -c ". '$HERE/tools/pick-model.sh'; pick_model && echo \"\$MODEL|\$CTX|\$MMPROJ\"")
  m=$(basename "${out%%|*}"); rest=${out#*|}; c=${rest%%|*}; mm=${rest#*|}; mm=${mm:+$(basename "$mm")}
  if [ "$m" = "$3" ] && [ "$c" = "$4" ] && [ "$mm" = "${5:-}" ]; then echo "ok   $1"; else echo "FAIL $1: got $out"; fail=1; fi
}
check "4 GB laptop -> 1.7B, 4k"  3900  qwen3-1.7b-q4_k_m.gguf 4096
check "5 GB laptop -> 1.7B"     5000  qwen3-1.7b-q4_k_m.gguf 8192
check "8 GB laptop -> 4B"       7800  qwen3-4b-instruct-2507-q4_k_m.gguf 8192
check "16 GB laptop -> 8B+mm"   15800 qwen3-8b-q4_k_m.gguf 8192 qwen3-8b-q4_k_m.mmproj.gguf
check "2 GB -> smallest, 4k"    2048  qwen3-1.7b-q4_k_m.gguf 4096

out=$(cd "$T" && RAM_MB=2048 MODEL=models/spare/tiny.gguf CTX=2048 sh -c ". '$HERE/tools/pick-model.sh'; pick_model && echo \"\$MODEL|\$CTX\"")
[ "$out" = "models/spare/tiny.gguf|2048" ] && echo "ok   MODEL/CTX override" || { echo "FAIL override: $out"; fail=1; }

out=$(cd "$T" && MODEL=models/nope.gguf sh -c ". '$HERE/tools/pick-model.sh'; pick_model" 2>&1) && { echo "FAIL missing MODEL accepted"; fail=1; } || echo "ok   missing MODEL rejected"

mkdir -p "$T/empty/models"
(cd "$T/empty" && sh -c ". '$HERE/tools/pick-model.sh'; pick_model" >/dev/null 2>&1) && { echo "FAIL empty models dir accepted"; fail=1; } || echo "ok   empty models dir rejected"

mkdir -p "$T/sp/models"; truncate -s 10M "$T/sp/models/my model.gguf"
out=$(cd "$T/sp" && RAM_MB=8192 sh -c ". '$HERE/tools/pick-model.sh'; pick_model && echo \"\$MODEL\"")
[ "$out" = "models/my model.gguf" ] && echo "ok   file name with a space" || { echo "FAIL space: $out"; fail=1; }
exit $fail
