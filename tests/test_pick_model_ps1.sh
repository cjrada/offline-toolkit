#!/bin/sh
# Runs tools/pick-model.ps1 under PowerShell 7 (pwsh) with fake sizes, checking it agrees with the sh rule.
# Windows-only probes (CIM, registry, nvidia-smi.exe) are bypassed with RAM_MB / VRAM_MB.
set -u
command -v pwsh >/dev/null 2>&1 || { echo "skip: pwsh not installed"; exit 0; }
HERE="$(cd "$(dirname "$0")/.." && pwd)"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
mkdir -p "$T/tools" "$T/models/spare" "$T/www"; cp "$HERE/tools/pick-model.ps1" "$T/tools/"
cd "$T" || exit 1
truncate -s 1100M models/qwen3-1.7b-q4_k_m.gguf; truncate -s 2500M models/qwen3-4b-instruct-2507-q4_k_m.gguf
truncate -s 5000M models/qwen3-8b-q4_k_m.gguf; truncate -s 900M models/qwen3-8b-q4_k_m.mmproj.gguf; truncate -s 100M models/spare/tiny.gguf
fail=0
export SWITCH_TOKEN=0123456789abcdef0123456789abcdef
run(){ r=$1 v=$2; shift 2; env -u MODEL -u CTX -u GPU RAM_MB="$r" VRAM_MB="$v" pwsh -NoProfile -File tools/pick-model.ps1 "$@" 2>/dev/null | tr '\r\n' '  '; }
expect(){ case "$2" in *"$3"*) echo "ok   $1" ;; *) echo "FAIL $1: $2"; fail=1 ;; esac; }
expect "4 GB -> smallest 4k"  "$(run 3900 0)"      'MODEL=models\qwen3-1.7b-q4_k_m.gguf MMPROJ= CTX=4096'
expect "8 GB -> 4B"           "$(run 7800 0)"      'MODEL=models\qwen3-4b-instruct-2507-q4_k_m.gguf MMPROJ= CTX=8192'
expect "16 GB -> 8B+mmproj"   "$(run 15800 0)"     'MMPROJ=models\qwen3-8b-q4_k_m.mmproj.gguf CTX=8192'
expect "3 GB GPU -> 1.7B"     "$(run 7800 3072)"   'MODEL=models\qwen3-1.7b-q4_k_m.gguf'
expect "12 GB GPU -> 8B"      "$(run 3900 12288)"  'MODEL=models\qwen3-8b-q4_k_m.gguf'
printf '%s\nmodels/spare/tiny.gguf\n' "$SWITCH_TOKEN" >req.txt
expect "valid switch request" "$(run 7800 0 -RequestFile req.txt -Json www/models.json)" 'MODEL=models\spare\tiny.gguf'
python3 -c 'import json,os;d=json.load(open("www/models.json"));assert d["current"]=="models/spare/tiny.gguf" and len(d["models"])==4 and d["switch_token"]==os.environ["SWITCH_TOKEN"]' \
  && echo "ok   models.json" || { echo "FAIL models.json"; fail=1; }
printf 'wrongtoken\nmodels/spare/tiny.gguf\n' >req.txt
[ -z "$(run 7800 0 -RequestFile req.txt)" ] && echo "ok   reject wrong token" || { echo "FAIL accepted wrong token"; fail=1; }
printf 'models/spare/tiny.gguf\n' >req.txt
[ -z "$(run 7800 0 -RequestFile req.txt)" ] && echo "ok   reject missing token" || { echo "FAIL accepted missing token"; fail=1; }
printf '%s\nmodels/spare/tiny.gguf\n' "$SWITCH_TOKEN" >req.txt
[ -z "$(SWITCH_TOKEN='' run 7800 0 -RequestFile req.txt)" ] && echo "ok   reject when no token configured" || { echo "FAIL no-token config accepted"; fail=1; }
for r in '../x.gguf' 'models/qwen3-8b-q4_k_m.mmproj.gguf' 'models/missing.gguf' 'models/x.gguf & calc'; do
  printf '%s\n%s' "$SWITCH_TOKEN" "$r" >req.txt
  [ -z "$(run 7800 0 -RequestFile req.txt)" ] && echo "ok   reject $r" || { echo "FAIL accepted $r"; fail=1; }
done
exit $fail
