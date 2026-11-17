#!/bin/sh
# start.sh restarts llamafile on the CPU (re-picking the model against RAM) when it exits in GPU mode,
# and does not loop if CPU mode exits too. Uses stub binaries. Usage: sh tests/test_cpu_fallback.sh
set -u
HERE="$(cd "$(dirname "$0")/.." && pwd)"
T="$(mktemp -d)"; LPID=""
trap '[ -n "$LPID" ] && kill "$LPID" 2>/dev/null; rm -rf "$T"' EXIT
fail=0
ok(){ echo "ok   $1"; }
bad(){ echo "FAIL $1"; fail=1; }

mkkit() { # dir, llamafile behaviour on CPU: run | exit
  K="$1"; mkdir -p "$K/tools" "$K/bin/linux" "$K/models" "$K/www" "$K/tmp"
  cp "$HERE/start.sh" "$K/"; cp "$HERE/tools/pick-model.sh" "$K/tools/"
  truncate -s 1100M "$K/models/qwen3-1.7b-q4_k_m.gguf"
  truncate -s 5000M "$K/models/qwen3-8b-q4_k_m.gguf"
  for b in miniserve kiwix-serve; do
    printf '#!/bin/sh\n[ "${1:-}" = --version ] && exit 0\nexec sleep 600\n' >"$K/bin/linux/$b"; chmod +x "$K/bin/linux/$b"
  done
  # Fake llamafile: records its arguments; "crashes" unless --gpu disable (and, with "exit", always).
  cat >"$K/bin/llamafile.exe" <<STUB
echo "\$*" >>"$K/calls"
case " \$* " in *" --gpu disable "*) [ "$2" = run ] && exec sleep 600 ;; esac
exit 3
STUB
}

run_kit() { # dir -> starts launcher, waits for it to settle
  (cd "$1" && TMPDIR="$1/tmp" NO_BROWSER=1 RAM_MB=3900 VRAM_MB=12288 sh start.sh >"$1/out" 2>&1) &
  LPID=$!
  i=0; while [ $i -lt 10 ]; do sleep 1; i=$((i + 1)); done
}

stop_kit() { # dir
  kill "$(cat "$1"/tmp/offline-toolkit-*.pid)" 2>/dev/null; wait "$LPID" 2>/dev/null; LPID=""
}

# 1. GPU crash -> CPU retry with the model re-picked for 3.9 GB RAM
K="$T/a"; mkkit "$K" run; run_kit "$K"
n=$(wc -l <"$K/calls")
first=$(sed -n 1p "$K/calls"); second=$(sed -n 2p "$K/calls")
[ "$n" -eq 2 ] && ok "two starts (GPU, then CPU)" || bad "expected 2 starts, got $n: $(cat "$K/calls")"
case "$first" in *qwen3-8b*) case "$first" in *"--gpu"*) bad "first start already had --gpu: $first" ;; *) ok "first start: 8B on GPU" ;; esac ;;
  *) bad "first start: $first" ;; esac
case "$second" in *"qwen3-1.7b"*"-c 4096"*"--gpu disable"*) ok "CPU retry: 1.7B, 4k, --gpu disable" ;;
  *) bad "CPU retry: $second" ;; esac
python3 -c '
import json,sys; d=json.load(open(sys.argv[1]))
assert d["gpu_fallback"] is True and d["vram_mb"] == 0, d
assert d["reason"].startswith("GPU mode failed"), d["reason"]
assert d["current"].endswith("qwen3-1.7b-q4_k_m.gguf"), d["current"]
' "$K/www/models.json" && ok "models.json reports the CPU fallback" || bad "models.json: $(cat "$K/www/models.json")"
grep -q "retrying on the CPU" "$K/out" && ok "launcher says it is retrying on the CPU" || bad "no message: $(cat "$K/out")"
# A model switch after the fallback stays on the CPU
tok=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["switch_token"])' "$K/www/models.json")
printf '%s\nmodels/qwen3-8b-q4_k_m.gguf\n' "$tok" >"$K/tmp/offline-toolkit-control-$(id -u)/switch.txt"
sleep 3
third=$(sed -n 3p "$K/calls")
case "$third" in *"qwen3-8b"*"--gpu disable"*) ok "switch after fallback stays on the CPU" ;; *) bad "switch: $third" ;; esac
stop_kit "$K"

# 2. CPU mode exits too -> no restart loop
K="$T/b"; mkkit "$K" exit; run_kit "$K"
n=$(wc -l <"$K/calls")
[ "$n" -eq 2 ] && ok "no restart loop when CPU mode also exits" || bad "expected 2 starts, got $n"
grep -q "The AI model stopped (exit code 3)\. Last lines" "$K/out" && ok "reports the CPU failure" || bad "out: $(cat "$K/out")"
stop_kit "$K"
exit $fail
