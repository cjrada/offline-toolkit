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
ok(){ echo "ok   $1"; }
bad(){ echo "FAIL $1"; fail=1; }

# name, RAM_MB, VRAM_MB, expected model basename, expected ctx, [expected mmproj basename]
check() {
  out=$(cd "$T" && unset MODEL CTX GPU; RAM_MB=$2 VRAM_MB=$3 sh -c ". '$HERE/tools/pick-model.sh'; pick_model && echo \"\$MODEL|\$CTX|\$MMPROJ\"")
  m=$(basename "${out%%|*}"); rest=${out#*|}; c=${rest%%|*}; mm=${rest#*|}; mm=${mm:+$(basename "$mm")}
  if [ "$m" = "$4" ] && [ "$c" = "$5" ] && [ "$mm" = "${6:-}" ]; then ok "$1"; else bad "$1: got $out"; fi
}
check "4 GB laptop -> smallest, 4k"   3900  0 qwen3-1.7b-q4_k_m.gguf 4096
check "6 GB laptop -> 1.7B"           5900  0 qwen3-1.7b-q4_k_m.gguf 8192
check "8 GB laptop -> 4B"             7800  0 qwen3-4b-instruct-2507-q4_k_m.gguf 8192
check "12 GB laptop -> 8B"           11800  0 qwen3-8b-q4_k_m.gguf 8192 qwen3-8b-q4_k_m.mmproj.gguf
check "16 GB laptop -> 8B+mmproj"    15800  0 qwen3-8b-q4_k_m.gguf 8192 qwen3-8b-q4_k_m.mmproj.gguf
check "2 GB -> smallest, 4k"          2048  0 qwen3-1.7b-q4_k_m.gguf 4096
check "3 GB GPU + 8 GB RAM -> 1.7B on GPU"  7800 3072 qwen3-1.7b-q4_k_m.gguf 8192
check "4 GB GPU + 8 GB RAM -> 4B on GPU"    7800 4096 qwen3-4b-instruct-2507-q4_k_m.gguf 8192
check "8 GB GPU + 8 GB RAM -> 8B on GPU"    7800 8192 qwen3-8b-q4_k_m.gguf 8192 qwen3-8b-q4_k_m.mmproj.gguf
check "12 GB GPU + 4 GB RAM -> 8B on GPU"   3900 12288 qwen3-8b-q4_k_m.gguf 8192 qwen3-8b-q4_k_m.mmproj.gguf
check "tiny GPU ignored -> RAM rule"        7800 512 qwen3-4b-instruct-2507-q4_k_m.gguf 8192

out=$(cd "$T" && RAM_MB=7800 VRAM_MB=12288 GPU=disable sh -c ". '$HERE/tools/pick-model.sh'; pick_model && echo \"\$MODEL\"")
[ "$out" = "models/qwen3-4b-instruct-2507-q4_k_m.gguf" ] && ok "GPU=disable ignores VRAM" || bad "GPU=disable: $out"

out=$(cd "$T" && RAM_MB=2048 MODEL=models/spare/tiny.gguf CTX=2048 sh -c ". '$HERE/tools/pick-model.sh'; pick_model && echo \"\$MODEL|\$CTX\"")
[ "$out" = "models/spare/tiny.gguf|2048" ] && ok "MODEL/CTX override" || bad "override: $out"

(cd "$T" && MODEL=models/nope.gguf sh -c ". '$HERE/tools/pick-model.sh'; pick_model" >/dev/null 2>&1) && bad "missing MODEL accepted" || ok "missing MODEL rejected"

mkdir -p "$T/empty/models"
(cd "$T/empty" && sh -c ". '$HERE/tools/pick-model.sh'; pick_model" >/dev/null 2>&1) && bad "empty models dir accepted" || ok "empty models dir rejected"

mkdir -p "$T/sp/models"; truncate -s 10M "$T/sp/models/my model.gguf"
out=$(cd "$T/sp" && RAM_MB=8192 sh -c ". '$HERE/tools/pick-model.sh'; pick_model && echo \"\$MODEL\"")
[ "$out" = "models/my model.gguf" ] && ok "file name with a space" || bad "space: $out"

# Switch-request validation (requests come from the home page via the 127.0.0.1 control server)
for r in models/qwen3-8b-q4_k_m.gguf models/spare/tiny.gguf; do
  (cd "$T" && sh -c ". '$HERE/tools/pick-model.sh'; pick_valid_request '$r'") && ok "accept $r" || bad "rejected valid $r"
done
for r in ../etc/passwd models/../x.gguf /etc/x.gguf models/qwen3-8b-q4_k_m.mmproj.gguf models/missing.gguf \
         'models/x.gguf;rm -rf /' models/a/b/c.gguf library/x.zim; do
  (cd "$T" && sh -c ". '$HERE/tools/pick-model.sh'; pick_valid_request \"\$1\"" _ "$r") && bad "accepted invalid $r" || ok "reject $r"
done

# models.json is valid JSON and lists spare models but not projectors
json=$(cd "$T" && SWITCH_TOKEN='ab12<"x' RAM_MB=7800 VRAM_MB=0 sh -c ". '$HERE/tools/pick-model.sh'; pick_model && pick_models_json")
echo "$json" | python3 -c '
import json,sys; d=json.load(sys.stdin); names=[m["name"] for m in d["models"]]
assert d["current"].endswith("qwen3-4b-instruct-2507-q4_k_m.gguf"), d["current"]
assert d["switch_token"] == "ab12", d["switch_token"]   # only hex survives, so the JSON stays valid
assert "tiny" in names and not any("mmproj" in n for n in names), names
assert {m["name"]: m["fit"] for m in d["models"]}["qwen3-8b-q4_k_m"] == "tight"
assert {m["name"]: m["vision"] for m in d["models"]}["qwen3-8b-q4_k_m"] is True
' && ok "models.json valid" || bad "models.json: $json"
exit $fail
