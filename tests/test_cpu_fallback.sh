#!/bin/sh
# start.sh GPU/CPU behaviour with stub binaries (no real llamafile, no network):
#  * GPU starts pass "-fa off" (Vulkan flash-attention segfault) and llamafile always gets
#    DISABLE_VULKAN_OBS_CAPTURE=1 / VK_LOADER_LAYERS_DISABLE=~implicit~ (OBS Vulkan hook crash);
#    FA / NGL / GPU overrides honoured
#  * llamafile exiting in GPU mode, or never answering /health, is retried once on the CPU
#    with the model re-picked against RAM - but only after a real GPU attempt (GPU memory seen, or
#    llamafile's log names a GPU device), never when GPU=... was set by hand;
#    a slow load with no GPU / on the CPU is reported and left running (no --gpu disable restart);
#    CPU-mode exits are reported without a restart loop
#  * the home page's "Use CPU only" toggle (switch request line 3: cpu=on / cpu=off)
# Usage: sh tests/test_cpu_fallback.sh      (needs curl, for the file:// health check)
set -u
HERE="$(cd "$(dirname "$0")/.." && pwd)"
command -v curl >/dev/null 2>&1 || { echo "skip: curl not installed"; exit 0; }
T="$(mktemp -d)"; LPID=""
trap '[ -n "$LPID" ] && kill "$LPID" 2>/dev/null; rm -rf "$T"' EXIT
fail=0
ok(){ echo "ok   $1"; }
bad(){ echo "FAIL $1"; fail=1; }

mkkit() { # dir, behaviour on GPU, behaviour on CPU
  # run | exit | hang | slow (healthy after 6 s) | vkexit (logs a Vulkan device, then exits)
  K="$1"; mkdir -p "$K/tools" "$K/bin/linux" "$K/models" "$K/www" "$K/tmp"
  cp "$HERE/start.sh" "$K/"; cp "$HERE/tools/pick-model.sh" "$K/tools/"
  truncate -s 1100M "$K/models/qwen3-1.7b-q4_k_m.gguf"
  truncate -s 5000M "$K/models/qwen3-8b-q4_k_m.gguf"
  for b in miniserve kiwix-serve; do
    printf '#!/bin/sh\n[ "${1:-}" = --version ] && exit 0\nexec sleep 600\n' >"$K/bin/linux/$b"; chmod +x "$K/bin/linux/$b"
  done
  # Fake llamafile: records its arguments; "healthy" means it would answer /health.
  cat >"$K/bin/llamafile.exe" <<STUB
rm -f "$K/healthy"; echo "\$*" >>"$K/calls"; echo \$\$ >>"$K/pids"
echo "\${DISABLE_VULKAN_OBS_CAPTURE:-unset} \${VK_LOADER_LAYERS_DISABLE:-unset}" >>"$K/env"
case " \$* " in *" --gpu disable "*) m=$3 ;; *) m=$2 ;; esac
case \$m in
  run) : >"$K/healthy"; exec sleep 600 ;;
  hang) exec sleep 600 ;;
  slow) (sleep 6; : >"$K/healthy") & exec sleep 600 ;;
  vkexit) echo "ggml_vulkan: Found 1 Vulkan devices:"; echo "Vulkan0 : Some GPU (8192 MiB, 7000 MiB free)"; exit 11 ;;
  *) exit 3 ;;
esac
STUB
}

run_kit() { # dir [VAR=value ...]  -> starts the launcher (fake 3.9 GB RAM, 12 GB GPU)
  _k="$1"; shift
  (cd "$_k" && env TMPDIR="$_k/tmp" NO_BROWSER=1 RAM_MB=3900 VRAM_MB=12288 HEALTH_URL="file://$_k/healthy" \
     "$@" sh start.sh >"$_k/out" 2>&1) &
  LPID=$!
}

calls() { [ -f "$1/calls" ] && wc -l <"$1/calls" | tr -d ' ' || echo 0; }
wait_calls() { # dir, n, [seconds]  wait until llamafile was started n times
  _i=0; while [ "$(calls "$1")" -lt "$2" ] && [ $_i -lt "${3:-15}" ]; do sleep 1; _i=$((_i + 1)); done
  sleep 2   # let the launcher write models.json / print, and give a chance for unwanted extra starts
}
call() { sed -n "${2}p" "$1/calls"; }
json() { python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(eval(sys.argv[2]))' "$1/www/models.json" "$2"; }
send() { # dir, model, [cpu flag line]
  _tok=$(json "$1" 'd["switch_token"]')
  printf '%s\n%s\n%s\n' "$_tok" "$2" "${3:-}" >"$1/tmp/offline-toolkit-control-$(id -u)/switch.txt"
}
stop_kit() { kill "$(cat "$1"/tmp/offline-toolkit-*.pid)" 2>/dev/null; wait "$LPID" 2>/dev/null; LPID=""; }
has() { case "$1" in *"$2"*) return 0 ;; *) return 1 ;; esac; }

# 1. GPU exit -> CPU retry with the model re-picked for 3.9 GB RAM; a later switch stays on the CPU
K="$T/a"; mkkit "$K" exit run; run_kit "$K"; wait_calls "$K" 2
[ "$(calls "$K")" -eq 2 ] && ok "two starts (GPU, then CPU)" || bad "expected 2 starts, got: $(cat "$K/calls")"
c1=$(call "$K" 1); c2=$(call "$K" 2)
has "$c1" qwen3-8b && has "$c1" "-fa off" && ! has "$c1" "--gpu" && ! has "$c1" "-ngl" \
  && ok "GPU start: 8B with -fa off" || bad "GPU start: $c1"
has "$c2" "qwen3-1.7b" && has "$c2" "-c 4096" && has "$c2" "--gpu disable" && ! has "$c2" "-fa" \
  && ok "CPU retry: 1.7B, 4k, --gpu disable, no -fa" || bad "CPU retry: $c2"
[ "$(sort -u "$K/env")" = "1 ~implicit~" ] && ok "OBS Vulkan hook disabled for every start (GPU and CPU)" || bad "env: $(cat "$K/env")"
[ "$(json "$K" 'd["gpu_fallback"], d["cpu_only"], d["vram_mb"], d["run"]')" = "(True, True, 0, 2)" ] \
  && ok "models.json: gpu_fallback, cpu_only, run=2" || bad "models.json: $(cat "$K/www/models.json")"
has "$(json "$K" 'd["reason"]')" "GPU mode failed" && ok "reason mentions the fallback" || bad "reason"
[ "$(grep -c "fell back to the CPU" "$K/out")" -eq 1 ] && ok "one clear 'fell back to the CPU' line" || bad "no message: $(cat "$K/out")"
grep -q "AI model ready" "$K/out" && ok "health check saw the CPU start become ready" || bad "never ready: $(cat "$K/out")"
send "$K" models/qwen3-8b-q4_k_m.gguf; wait_calls "$K" 3
c3=$(call "$K" 3)
has "$c3" qwen3-8b && has "$c3" "--gpu disable" && ok "model switch after fallback stays on the CPU" || bad "switch: $c3"
# 1b. "Use CPU only" off after a fallback: honoured, the GPU is tried again (and here fails again)
send "$K" models/qwen3-8b-q4_k_m.gguf cpu=off; wait_calls "$K" 5
c4=$(call "$K" 4); c5=$(call "$K" 5)
has "$c4" "-fa off" && ! has "$c4" "--gpu disable" && ok "toggle off retries the GPU" || bad "toggle off: $c4"
has "$c5" "--gpu disable" && ok "...and falls back to the CPU again" || bad "second fallback: $c5"
stop_kit "$K"

# 2. CPU mode exits too -> reported, no restart loop
K="$T/b"; mkkit "$K" exit exit; run_kit "$K"; wait_calls "$K" 3 6
[ "$(calls "$K")" -eq 2 ] && ok "no restart loop when CPU mode also exits" || bad "expected 2 starts, got $(calls "$K")"
grep -q "The AI model stopped (exit code 3)\. Last lines" "$K/out" && ok "reports the CPU failure" || bad "out: $(cat "$K/out")"
stop_kit "$K"

# 3. GPU start hangs (alive, never healthy) -> stopped after HEALTH_TIMEOUT and retried on the CPU
K="$T/c"; mkkit "$K" hang run; run_kit "$K" HEALTH_TIMEOUT=3; wait_calls "$K" 2
c2=$(call "$K" 2)
has "$c2" "--gpu disable" && ok "hung GPU start retried on the CPU" || bad "hang: $(cat "$K/calls")"
grep -q "GPU start failed (AI not ready after 3 s) - fell back to the CPU" "$K/out" && ok "reports the GPU load timeout" || bad "out: $(cat "$K/out")"
kill -0 "$(sed -n 1p "$K/pids")" 2>/dev/null && bad "hung GPU process left running" || ok "hung GPU process was stopped"
stop_kit "$K"

# 4. A CPU start that is slow is only reported, never killed
K="$T/d"; mkkit "$K" run hang; run_kit "$K" GPU=disable HEALTH_TIMEOUT=2; wait_calls "$K" 2 5
[ "$(calls "$K")" -eq 1 ] && grep -q "still loading after 2 s" "$K/out" \
  && ok "slow CPU start reported, not restarted" || bad "slow CPU: $(cat "$K/calls") / $(cat "$K/out")"
stop_kit "$K"

# 4b. No GPU detected (VRAM 0), GPU/auto mode: a slow load is NOT a GPU failure - no --gpu disable
#     restart; reported once, polling continues and "ready" is printed when it comes
K="$T/d2"; mkkit "$K" slow run; run_kit "$K" VRAM_MB=0 HEALTH_TIMEOUT=2; wait_calls "$K" 2 8
[ "$(calls "$K")" -eq 1 ] && ! has "$(call "$K" 1)" "--gpu disable" && ok "no GPU: slow load not restarted with --gpu disable" \
  || bad "no GPU slow: $(cat "$K/calls")"
grep -q "still loading after 2 s - leaving it running (no restart)" "$K/out" && ! grep -q "fell back" "$K/out" \
  && ok "no GPU: timeout reported, no fallback" || bad "out: $(cat "$K/out")"
[ "$(grep -c "still loading" "$K/out")" -eq 1 ] && grep -q "AI model ready" "$K/out" \
  && ok "no GPU: reported once, then ready" || bad "out: $(cat "$K/out")"
kill -0 "$(sed -n 1p "$K/pids")" 2>/dev/null && ok "no GPU: slow process still running" || bad "slow process was killed"
stop_kit "$K"
# 4c. No GPU detected and the load hangs: never restarted
K="$T/d3"; mkkit "$K" hang run; run_kit "$K" VRAM_MB=0 HEALTH_TIMEOUT=2; wait_calls "$K" 2 6
[ "$(calls "$K")" -eq 1 ] && ! grep -q "fell back" "$K/out" && ok "no GPU: hanging load not restarted" || bad "no GPU hang: $(cat "$K/calls")"
stop_kit "$K"
# 4d. No GPU detected and llamafile exits: reported, no pointless --gpu disable restart
K="$T/d4"; mkkit "$K" exit run; run_kit "$K" VRAM_MB=0; wait_calls "$K" 2 6
[ "$(calls "$K")" -eq 1 ] && grep -q "The AI model stopped (exit code 3)" "$K/out" && ! grep -q "fell back" "$K/out" \
  && ok "no GPU: exit reported, no fallback" || bad "no GPU exit: $(cat "$K/calls") / $(cat "$K/out")"
stop_kit "$K"
# 4e. VRAM not detected, but llamafile's log shows it used a Vulkan device before exiting: GPU attempt -> fallback
K="$T/d5"; mkkit "$K" vkexit run; run_kit "$K" VRAM_MB=0; wait_calls "$K" 2
has "$(call "$K" 2)" "--gpu disable" && grep -q "GPU start failed (AI exited with code 11) - fell back to the CPU" "$K/out" \
  && ok "GPU device in the log: exit falls back" || bad "vkexit: $(cat "$K/calls") / $(cat "$K/out")"
stop_kit "$K"
# 4f. GPU in use and it hangs (VRAM seen): still falls back (same as 3, with a GPU-sized load timeout cap)
K="$T/d6"; mkkit "$K" slow run; run_kit "$K" HEALTH_TIMEOUT=2; wait_calls "$K" 2
has "$(call "$K" 2)" "--gpu disable" && ok "GPU in use: slow/hung load still falls back after the timeout" || bad "gpu slow: $(cat "$K/calls")"
stop_kit "$K"

# 5. "Use CPU only" toggle on a healthy GPU run: on -> --gpu disable, off -> GPU flags again
K="$T/e"; mkkit "$K" run run; run_kit "$K"; wait_calls "$K" 1
[ "$(json "$K" 'd["cpu_only"], d["gpu_fallback"], d["run"]')" = "(False, False, 1)" ] && ok "starts in GPU mode" || bad "json: $(cat "$K/www/models.json")"
send "$K" "$(json "$K" 'd["current"]')" cpu=on; wait_calls "$K" 2
c2=$(call "$K" 2)
has "$c2" "--gpu disable" && has "$c2" qwen3-1.7b && ok "toggle on: CPU only, auto-pick re-run for RAM" || bad "toggle on: $c2"
[ "$(json "$K" 'd["cpu_only"], d["gpu_fallback"], d["run"], len(d["switch_token"]), d["switch_port"]')" = "(True, False, 2, 32, 8079)" ] \
  && ok "models.json after toggle: cpu_only, run=2, token/port kept" || bad "json: $(cat "$K/www/models.json")"
has "$(json "$K" 'd["reason"]')" "CPU only;" && ok "reason says CPU only" || bad "reason: $(json "$K" 'd["reason"]')"
send "$K" "$(json "$K" 'd["current"]')" cpu=off; wait_calls "$K" 3
c3=$(call "$K" 3)
has "$c3" qwen3-8b && has "$c3" "-fa off" && ! has "$c3" "--gpu disable" \
  && ok "toggle off: back on the GPU with the GPU-sized pick" || bad "toggle off: $c3"
# a model the user picked is kept across the toggle
send "$K" models/qwen3-1.7b-q4_k_m.gguf; wait_calls "$K" 4
send "$K" models/qwen3-1.7b-q4_k_m.gguf cpu=on; wait_calls "$K" 5
c5=$(call "$K" 5)
has "$c5" qwen3-1.7b && has "$c5" "--gpu disable" && ok "toggle keeps a model chosen by hand" || bad "keep model: $c5"
# invalid requests change nothing
n=$(calls "$K")
for bad_flag in cpu=maybe 'cpu=on;reboot' CPU=ON gpu=off; do send "$K" models/qwen3-1.7b-q4_k_m.gguf "$bad_flag"; sleep 2; done
_tok=$(json "$K" 'd["switch_token"]')
printf 'wrong\nmodels/qwen3-1.7b-q4_k_m.gguf\ncpu=off\n' >"$K/tmp/offline-toolkit-control-$(id -u)/switch.txt"; sleep 2
[ "$(calls "$K")" -eq "$n" ] && [ "$(grep -c "Ignored an invalid" "$K/out")" -eq 5 ] \
  && ok "invalid cpu flags / wrong token ignored" || bad "invalid requests: $(calls "$K") starts; $(grep -c "Ignored" "$K/out") ignored"
stop_kit "$K"

# 6. Overrides: FA / NGL / GPU / Vulkan env are honoured, bad values rejected
K="$T/f"; mkkit "$K" run run; run_kit "$K" FA=on NGL=33 GPU=vulkan VK_LOADER_LAYERS_DISABLE=VK_LAYER_X; wait_calls "$K" 1
c1=$(call "$K" 1)
has "$c1" "--gpu vulkan -fa on -ngl 33" && ok "FA=on NGL=33 GPU=vulkan honoured" || bad "overrides: $c1"
[ "$(cat "$K/env")" = "1 VK_LAYER_X" ] && ok "your own VK_LOADER_LAYERS_DISABLE is kept" || bad "env: $(cat "$K/env")"
stop_kit "$K"
K="$T/f2"; mkkit "$K" run run; run_kit "$K" GPU=disable FA=on; wait_calls "$K" 1
has "$(call "$K" 1)" "--gpu disable -fa on" && ok "FA set by hand also applies on the CPU" || bad "FA cpu: $(call "$K" 1)"
stop_kit "$K"

# 7. GPU backend pinned by hand (GPU=vulkan): an exit is reported, no automatic CPU fallback
K="$T/h"; mkkit "$K" exit run; run_kit "$K" GPU=vulkan; wait_calls "$K" 2 6
[ "$(calls "$K")" -eq 1 ] && grep -q "The AI model stopped (exit code 3)" "$K/out" && ! grep -q "fell back" "$K/out" \
  && ok "GPU=vulkan set by hand: no automatic fallback" || bad "pinned GPU: $(cat "$K/calls") / $(cat "$K/out")"
stop_kit "$K"

for v in "FA=maybe" "NGL=all" "GPU=x;y"; do
  K="$T/g"; rm -rf "$K"; mkkit "$K" run run
  (cd "$K" && env TMPDIR="$K/tmp" NO_BROWSER=1 RAM_MB=3900 "$v" sh start.sh >"$K/out" 2>&1) \
    && bad "accepted $v" || { [ "$(calls "$K")" -eq 0 ] && ok "rejects $v" || bad "started with $v"; }
done
exit $fail
