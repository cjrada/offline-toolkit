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
. "$KIT/tools/pick-model.sh"
USER_CTX="${CTX:-}"
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
PIDFILE="$TMPD/offline-toolkit-$(id -u).pid"; echo $$ >"$PIDFILE"
cleanup(){ echo; echo "Stopping..."; kill $PIDS $LLM_PID 2>/dev/null; rm -rf "$CTL" "$PIDFILE"; exit 0; }
trap cleanup INT TERM

write_status(){ # www/models.json feeds the home page's model switcher; skipped if the drive is read-only
  pick_models_json >"$KIT/www/models.json.tmp" 2>/dev/null && mv "$KIT/www/models.json.tmp" "$KIT/www/models.json" 2>/dev/null
}

start_llm(){
  echo "Starting AI model ($(basename "$MODEL"), context $CTX) - $PICK_REASON..."
  # llamafile is an Actually Portable Executable; running it via sh works even on noexec mounts
  # and avoids binfmt/WINE hijacking on some distros.
  sh "$KIT/bin/llamafile.exe" --server -m "$MODEL" ${MMPROJ:+--mmproj "$MMPROJ"} \
     --host "$BIND" --port 8080 -c "$CTX" --reasoning "$REASONING" ${GPU:+--gpu "$GPU"} >"$LOG/llamafile.log" 2>&1 &
  LLM_PID=$!
  write_status
}
start_llm

ZIMS=$(ls library/*.zim 2>/dev/null)
if [ -n "$ZIMS" ]; then
  echo "Starting library (Kiwix)..."
  # shellcheck disable=SC2086
  "$BIN/kiwix-serve" --address="$BIND" --port=8081 $ZIMS >"$LOG/kiwix.log" 2>&1 &
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
  [ -f "$CTL/switch.txt" ] || continue
  TOK=$(sed -n 1p "$CTL/switch.txt" | head -c 100 | tr -d '\r')
  REQ=$(sed -n 2p "$CTL/switch.txt" | head -c 200 | tr -d '\r'); rm -f "$CTL"/*
  if [ -n "$SWITCH_TOKEN" ] && [ "$TOK" = "$SWITCH_TOKEN" ] && pick_valid_request "$REQ"; then
    echo "Switching model to $REQ ..."
    kill "$LLM_PID" 2>/dev/null; wait "$LLM_PID" 2>/dev/null
    MODEL="$REQ"; CTX="$USER_CTX"; pick_model && start_llm
  else
    echo "Ignored an invalid model-switch request."
  fi
done
