#!/bin/sh
# Offline Survival Kit launcher (Linux). Usage: sh start.sh [lan]
#   lan  = also allow phones/other laptops on the same Wi-Fi/hotspot to connect.
# Works from a USB/SSD even if it's mounted noexec (binaries are copied to a temp dir).
KIT="$(cd "$(dirname "$0")" && pwd)"; cd "$KIT" || exit 1
BIND=127.0.0.1; [ "${1:-}" = "lan" ] && BIND=0.0.0.0
TMPD="${TMPDIR:-/tmp}"
LOG="$TMPD/offline-toolkit-logs"; mkdir -p "$LOG"

# Pick the biggest model that fits this computer's GPU or RAM (override: MODEL=models/x.gguf, CTX=4096).
# Thinking is off by default: Qwen3 1.7B/8B otherwise spend their answer budget on hidden reasoning
# and the manuals page looks stuck. Override with REASONING=on or REASONING=auto.
REASONING="${REASONING:-off}"
case "$REASONING" in on|off|auto) ;; *) echo "REASONING must be on, off or auto"; exit 1 ;; esac
# GPU workarounds for llamafile 0.10.6 (Vulkan; found on an RTX 2070 Max-Q, driver 551.61):
#  * OBS Studio's implicit Vulkan layer (VK_LAYER_OBS_HOOK) crashes Vulkan init (0xC0000005), so the
#    llamafile process gets DISABLE_VULKAN_OBS_CAPTURE=1 and VK_LOADER_LAYERS_DISABLE=~implicit~
#    (harmless without OBS; values you set yourself are kept).
#  * Vulkan flash attention segfaults (exit 11 in sched_reserve) with e.g. qwen3-4b at -c 8192 and
#    35+ GPU layers, so GPU runs pass "-fa off". Override: FA=on|off|auto (FA set by you also applies
#    on the CPU).
# Other overrides: GPU=disable (CPU only) or GPU=<llamafile --gpu value> (then no automatic CPU
# fallback), NGL=<layers> (GPU layers; default: llamafile decides).
FA_GPU="${FA:-off}"; USER_FA="${FA:-}"
case "$FA_GPU" in on|off|auto) ;; *) echo "FA must be on, off or auto"; exit 1 ;; esac
case "${NGL:-}" in *[!0-9]*) echo "NGL must be a number of layers"; exit 1 ;; esac
case "${GPU:-}" in *[!A-Za-z0-9_-]*) echo "GPU must be disable or a llamafile --gpu value"; exit 1 ;; esac
DISABLE_VULKAN_OBS_CAPTURE="${DISABLE_VULKAN_OBS_CAPTURE:-1}"
VK_LOADER_LAYERS_DISABLE="${VK_LOADER_LAYERS_DISABLE:-~implicit~}"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8080/health}"
. "$KIT/tools/pick-model.sh"
USER_CTX="${CTX:-}"; USER_MODEL="${MODEL:-}"; USER_GPU="${GPU:-}"
GPU_FALLBACK=""; export GPU_FALLBACK   # 1 once GPU mode has failed; stays on CPU until the home-page toggle
LLM_RUN=0; export LLM_RUN              # counts llamafile starts; the home page waits for it to change
pick_model || exit 1

# Static Linux binaries; if the drive won't let us execute, run copies from /tmp.
BIN="$KIT/bin/linux"
if ! "$BIN/miniserve" --version >/dev/null 2>&1; then
  BIN="$TMPD/offline-toolkit-bin"; mkdir -p "$BIN"
  cp "$KIT/bin/linux/kiwix-serve" "$KIT/bin/linux/miniserve" "$BIN/" && chmod +x "$BIN"/*
fi

PIDS=""; LLM_PID=""
CTL="$TMPD/offline-toolkit-control-$(id -u)"   # model-switch requests land here (127.0.0.1 only)
# Per-run secret: written only into www/models.json (other websites cannot read it - no CORS) and
# required as the first line of every switch request, so a page on another site cannot switch models.
SWITCH_TOKEN=$(od -An -N16 -tx1 /dev/urandom | tr -dc '0-9a-f')
export SWITCH_TOKEN
# The launcher's PID, for stop.sh. Keyed by user and by this kit's folder, so two kits (or two users) never
# share it. stop.sh checks the PID still belongs to a start.sh before signalling it.
KIT_ID=$(printf '%s' "$KIT" | cksum | cut -d' ' -f1)
PIDFILE="$TMPD/offline-toolkit-$(id -u)-$KIT_ID.pid"; echo $$ >"$PIDFILE"
cleanup(){ echo; echo "Stopping..."; kill $PIDS $LLM_PID 2>/dev/null; rm -rf "$CTL" "$PIDFILE"; exit 0; }
trap cleanup INT TERM

write_status(){ # www/models.json feeds the home page's model switcher; skipped if the drive is read-only
  pick_models_json >"$KIT/www/models.json.tmp" 2>/dev/null && mv "$KIT/www/models.json.tmp" "$KIT/www/models.json" 2>/dev/null
}

gpu_args(){ # llamafile flags for the current CPU/GPU mode (values validated above, so no quoting needed)
  if [ "${GPU:-}" = disable ]; then echo "--gpu disable${USER_FA:+ -fa $USER_FA}"; return; fi
  _a="-fa $FA_GPU"; [ -n "${GPU:-}" ] && _a="--gpu $GPU $_a"
  [ -n "${NGL:-}" ] && _a="$_a -ngl $NGL"
  echo "$_a"
}

# CPU fallback only after a real GPU attempt: GPU/auto mode with no backend pinned by GPU=..., and a GPU
# device in use - pick-model saw GPU memory at this start, or llamafile's log names a GPU device or
# offloaded layers. With no GPU (or GPU=disable) a slow load is just slow: never restart it.
gpu_in_log(){
  grep -Eqi 'ggml_vulkan: found [1-9]|^ *vulkan[0-9]+ *:|ggml_cuda_init: found [1-9]|offloaded [1-9][0-9]*/[0-9]+ layers to gpu' \
    "$LOG/llamafile.log" 2>/dev/null
}
gpu_attempt(){ [ -z "${GPU:-}" ] && { [ "${LLM_VRAM:-0}" -gt 0 ] || gpu_in_log; }; }

health_ok(){ # 0 = ready, 1 = not (yet), 2 = no curl/wget to ask with
  if command -v curl >/dev/null 2>&1; then curl -sf -m 2 -o /dev/null "$HEALTH_URL"
  elif command -v wget >/dev/null 2>&1; then wget -q -T 2 -t 1 -O /dev/null "$HEALTH_URL"
  else return 2; fi
}

start_llm(){
  LLM_RUN=$((LLM_RUN + 1)); _ga=$(gpu_args)
  echo "Starting AI model ($(basename "$MODEL"), context $CTX, ${_ga:-auto}) - $PICK_REASON..."
  # llamafile is an Actually Portable Executable; running it via sh works even on noexec mounts
  # and avoids binfmt/WINE hijacking on some distros.
  # shellcheck disable=SC2086
  DISABLE_VULKAN_OBS_CAPTURE="$DISABLE_VULKAN_OBS_CAPTURE" VK_LOADER_LAYERS_DISABLE="$VK_LOADER_LAYERS_DISABLE" \
  sh "$KIT/bin/llamafile.exe" --server -m "$MODEL" ${MMPROJ:+--mmproj "$MMPROJ"} \
     --host "$BIND" --port 8080 -c "$CTX" --reasoning "$REASONING" $_ga >"$LOG/llamafile.log" 2>&1 &
  LLM_PID=$!; LLM_T0=$(date +%s); LLM_READY=""; LLM_WARNED=""; LLM_TIMEOUT=$(pick_load_timeout)
  LLM_VRAM="${_vram:-0}"   # GPU memory pick_model saw for this start (0: no GPU, or GPU=disable)
  write_status
}

stop_llm(){ [ -n "$LLM_PID" ] && { kill "$LLM_PID" 2>/dev/null; wait "$LLM_PID" 2>/dev/null; }; LLM_PID=""; }

cpu_fallback(){ # $1 = what went wrong in GPU mode
  cp "$LOG/llamafile.log" "$LOG/llamafile-gpu.log" 2>/dev/null
  echo "GPU start failed ($1) - fell back to the CPU. GPU log: $LOG/llamafile-gpu.log"
  tail -n 5 "$LOG/llamafile-gpu.log" 2>/dev/null | sed 's/^/    /'
  GPU=disable; GPU_FALLBACK=1; MODEL="$USER_MODEL"; CTX="$USER_CTX"
  pick_model && start_llm
}

# llamafile exited by itself (we never kill it except to switch models or stop). The usual cause with
# a GPU is a driver/Vulkan/ROCm failure or running out of GPU memory, so retry once on the CPU,
# re-picking the model against RAM (the GPU-sized pick may not fit RAM). Later switches stay on the CPU;
# the home page's "Use CPU only" toggle (off) or a restart tries the GPU again. Not done when you
# chose the GPU backend yourself with GPU=..., or when no GPU was in use (see gpu_attempt).
llm_exited(){
  wait "$LLM_PID" 2>/dev/null; _st=$?; LLM_PID=""
  if gpu_attempt; then
    cpu_fallback "AI exited with code $_st"
  else
    echo "The AI model stopped (exit code $_st). Last lines of $LOG/llamafile.log:"
    tail -n 10 "$LOG/llamafile.log" 2>/dev/null | sed 's/^/    /'
    echo "  The library, manuals and maps keep working. Pick another model on the home page, or restart."
  fi
}

# Still loading? A GPU start (GPU device in use) that never answers /health within LLM_TIMEOUT - a hang,
# not an exit - is stopped and retried on the CPU. Any other slow start (CPU only, no GPU, or GPU=...
# set by you) is reported once and left running; we keep polling so "ready" is still printed.
llm_check_ready(){
  [ -n "$LLM_PID" ] && [ -z "$LLM_READY" ] || return 0
  health_ok; _h=$?
  if [ $_h -eq 0 ]; then LLM_READY=1; echo "AI model ready after $(( $(date +%s) - LLM_T0 )) s."; return 0; fi
  if [ $_h -eq 2 ]; then LLM_READY=unknown; return 0; fi  # no curl/wget: the exit watch still works
  [ -z "$LLM_WARNED" ] && [ $(( $(date +%s) - LLM_T0 )) -ge "$LLM_TIMEOUT" ] || return 0
  if gpu_attempt; then
    stop_llm; cpu_fallback "AI not ready after $LLM_TIMEOUT s"
  else
    LLM_WARNED=1; echo "The AI model is still loading after $LLM_TIMEOUT s - leaving it running (no restart)."
  fi
}
start_llm

# Absolute ZIM paths (also when the folder has spaces): stop.sh recognises this kit's kiwix-serve by them,
# even when it runs from the shared $TMPDIR/offline-toolkit-bin copy.
set -- "$KIT"/library/*.zim
if [ -e "$1" ]; then
  echo "Starting library (Kiwix)..."
  "$BIN/kiwix-serve" --address="$BIND" --port=8081 "$@" >"$LOG/kiwix.log" 2>&1 &
  PIDS="$PIDS $!"
fi

echo "Starting home page, manuals and maps..."
"$BIN/miniserve" "$KIT/www" --index index.html -i "$BIND" -p 8000 -q >"$LOG/miniserve.log" 2>&1 &
PIDS="$PIDS $!"

# Model switching: a second miniserve that ONLY listens on 127.0.0.1 and only accepts uploads into a
# private temp folder. The home page uploads a one-line "switch.txt"; we accept it only if it names an
# existing model in models/ or models/spare/. Phones in LAN mode cannot reach it.
rm -rf "$CTL"; mkdir -p "$CTL" && chmod 700 "$CTL"
"$BIN/miniserve" "$CTL" -i 127.0.0.1 -p 8079 -u / -o overwrite -q >"$LOG/control.log" 2>&1 &
PIDS="$PIDS $!"

sleep 2
URL=http://localhost:8000/
echo
echo "  Open: $URL"
if [ "$BIND" = 0.0.0.0 ]; then
  for ip in $(hostname -I 2>/dev/null); do echo "  Phone/other devices: http://$ip:8000/"; done
fi
echo "  The AI takes ~10-60 s to load. Switch models on the home page. Logs: $LOG"
echo "  Press Ctrl+C here to stop everything."
[ -z "${NO_BROWSER:-}" ] && (xdg-open "$URL" >/dev/null 2>&1 || sensible-browser "$URL" >/dev/null 2>&1) &

while :; do
  sleep 1
  if [ -n "$LLM_PID" ] && ! kill -0 "$LLM_PID" 2>/dev/null; then llm_exited; fi
  llm_check_ready
  [ -f "$CTL/switch.txt" ] || continue
  # Request: line 1 = per-run token, line 2 = model path, optional line 3 = cpu=on | cpu=off
  # (the home page's "Use CPU only" toggle sends the current model with a cpu flag).
  TOK=$(sed -n 1p "$CTL/switch.txt" | head -c 100 | tr -d '\r')
  REQ=$(sed -n 2p "$CTL/switch.txt" | head -c 200 | tr -d '\r')
  CPUREQ=$(sed -n 3p "$CTL/switch.txt" | head -c 20 | tr -d '\r'); rm -f "$CTL"/*
  if [ -n "$SWITCH_TOKEN" ] && [ "$TOK" = "$SWITCH_TOKEN" ] && pick_valid_request "$REQ" && pick_valid_cpu_flag "$CPUREQ"; then
    case "$CPUREQ" in
      cpu=on)  echo "Switching to CPU only ..."; GPU=disable; GPU_FALLBACK="" ;;
      # Back to the GPU path even after a fallback this run, so the user can try the GPU again.
      cpu=off) echo "Switching to GPU / auto ..."; GPU="$USER_GPU"; [ "$GPU" = disable ] && GPU=""; GPU_FALLBACK="" ;;
    esac
    stop_llm; CTX="$USER_CTX"
    if [ -n "$CPUREQ" ] && [ "$REQ" = "$MODEL" ] && [ -z "$USER_MODEL" ]; then
      MODEL=""   # mode toggle on an auto-picked model: pick again for the new mode
    else
      echo "Switching model to $REQ ..."; USER_MODEL="$REQ"; MODEL="$REQ"
    fi
    pick_model && start_llm
  else
    echo "Ignored an invalid model-switch request."
  fi
done
