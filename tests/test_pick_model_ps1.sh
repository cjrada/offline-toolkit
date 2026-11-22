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
# CPU fallback (start.bat sets GPU=disable GPU_FALLBACK=1 after llamafile exits in GPU mode)
out=$(env -u MODEL -u CTX GPU=disable GPU_FALLBACK=1 RAM_MB=3900 VRAM_MB=12288 pwsh -NoProfile -File tools/pick-model.ps1 -Json www/models.json 2>/dev/null | tr '\r\n' '  ')
expect "GPU fallback re-picks for RAM" "$out" 'MODEL=models\qwen3-1.7b-q4_k_m.gguf MMPROJ= CTX=4096 REASON=GPU mode failed, running on the CPU;'
python3 -c 'import json;d=json.load(open("www/models.json"));assert d["gpu_fallback"] is True and d["vram_mb"]==0' \
  && echo "ok   models.json gpu_fallback" || { echo "FAIL models.json gpu_fallback"; fail=1; }
# "Use CPU only" toggle: optional third request line, validated exactly like sh pick_valid_cpu_flag
for f in on off; do
  printf '%s\nmodels/qwen3-8b-q4_k_m.gguf\ncpu=%s\n' "$SWITCH_TOKEN" "$f" >req.txt
  expect "check cpu=$f" "$(run 3900 12288 -RequestFile req.txt -CheckOnly)" "REQ_MODEL=models\qwen3-8b-q4_k_m.gguf REQ_CPU=$f"
done
printf '%s\nmodels/qwen3-8b-q4_k_m.gguf\n' "$SWITCH_TOKEN" >req.txt
expect "check without flag keeps mode" "$(run 3900 12288 -RequestFile req.txt -CheckOnly)" 'REQ_CPU=keep'
for f in cpu=ON cpu= ' cpu=on' 'cpu=on;calc' gpu=off cpu=auto; do
  printf '%s\nmodels/qwen3-8b-q4_k_m.gguf\n%s\n' "$SWITCH_TOKEN" "$f" >req.txt
  [ -z "$(run 3900 12288 -RequestFile req.txt -CheckOnly)" ] && echo "ok   reject cpu flag '$f'" || { echo "FAIL accepted cpu flag '$f'"; fail=1; }
done
printf 'wrong\nmodels/qwen3-8b-q4_k_m.gguf\ncpu=on\n' >req.txt
[ -z "$(run 3900 12288 -RequestFile req.txt -CheckOnly)" ] && echo "ok   reject cpu toggle with wrong token" || { echo "FAIL toggle wrong token"; fail=1; }
# CPU only chosen on the page (start.bat sets GPU=disable): VRAM ignored, reason + models.json say so
out=$(env -u MODEL -u CTX GPU=disable LLM_RUN=4 RAM_MB=3900 VRAM_MB=12288 pwsh -NoProfile -File tools/pick-model.ps1 -Json www/models.json 2>/dev/null | tr '\r\n' '  ')
expect "CPU only re-picks for RAM" "$out" 'MODEL=models\qwen3-1.7b-q4_k_m.gguf MMPROJ= CTX=4096 REASON=CPU only;'
python3 -c 'import json;d=json.load(open("www/models.json"));assert d["cpu_only"] is True and d["gpu_fallback"] is False and d["run"]==4 and d["switch_port"]==8079 and len(d["switch_token"])==32' \
  && echo "ok   models.json cpu_only / run" || { echo "FAIL models.json cpu_only"; fail=1; }
expect "load timeout from size" "$(run 3900 12288)" 'LOAD_TIMEOUT=160'
expect "HEALTH_TIMEOUT override" "$(HEALTH_TIMEOUT=30 run 3900 12288)" 'LOAD_TIMEOUT=30'
# GPU_MB: start.bat falls back to the CPU only after a GPU attempt (GPU_MB > 0)
expect "GPU_MB with a GPU"       "$(run 3900 12288)" 'GPU_MB=12288'
expect "GPU_MB with no GPU"      "$(run 3900 0)"     'GPU_MB=0'
expect "GPU_MB with GPU=disable" "$(env -u MODEL -u CTX GPU=disable RAM_MB=3900 VRAM_MB=12288 pwsh -NoProfile -File tools/pick-model.ps1 2>/dev/null | tr '\r\n' '  ')" 'GPU_MB=0'
# GPU_CC: NVIDIA compute capabilities from nvidia-smi (start.bat turns Vulkan coopmat2 off on 7.5 / Turing).
# A fake nvidia-smi (sh for Linux, .cmd for Windows) reports two GPUs; FAKE_SMI_OLD=1 mimics an nvidia-smi
# without the compute_cap field, which must not cost the VRAM reading.
mkdir -p "$T/fakebin"
cat > "$T/fakebin/nvidia-smi" <<'EOF'
#!/bin/sh
case "$*" in
  *memory.total*) echo 8192 ;;
  *compute_cap*) if [ "$FAKE_SMI_OLD" = 1 ]; then echo 'Field "compute_cap" is not a valid field to query.'; exit 2; fi
                 printf '7.5\n8.6\n' ;;
esac
EOF
chmod +x "$T/fakebin/nvidia-smi"
printf '@echo off\r\nset "A=%%*"\r\nif not "%%A:memory.total=%%"=="%%A%%" (echo 8192& exit /b 0)\r\nif "%%FAKE_SMI_OLD%%"=="1" (echo Field "compute_cap" is not a valid field to query.& exit /b 2)\r\necho 7.5\r\necho 8.6\r\n' > "$T/fakebin/nvidia-smi.cmd"
smi(){ env -u MODEL -u CTX -u GPU -u VRAM_MB RAM_MB=15800 PATH="$T/fakebin:$PATH" "$@" pwsh -NoProfile -File tools/pick-model.ps1 2>/dev/null | tr '\r\n' '  '; }
out=$(smi); expect "nvidia-smi VRAM" "$out" 'GPU_MB=8192'; expect "GPU_CC lists each GPU" "$out" 'GPU_CC=7.5 8.6'
out=$(smi FAKE_SMI_OLD=1); expect "old nvidia-smi: VRAM still read" "$out" 'GPU_MB=8192'; expect "old nvidia-smi: GPU_CC empty" "$out" 'GPU_CC= '
expect "GPU_CC empty with GPU=disable" "$(smi GPU=disable)" 'GPU_CC= '
exit $fail
