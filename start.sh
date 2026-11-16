#!/bin/sh
# Offline Survival Kit launcher (Linux). Usage: sh start.sh [lan]
#   lan  = also allow phones/other laptops on the same Wi-Fi/hotspot to connect.
# Works from a USB/SSD even if it's mounted noexec (binaries are copied to a temp dir).
KIT="$(cd "$(dirname "$0")" && pwd)"; cd "$KIT" || exit 1
BIND=127.0.0.1; [ "$1" = "lan" ] && BIND=0.0.0.0
LOG="${TMPDIR:-/tmp}/offline-ai-kit-logs"; mkdir -p "$LOG"

MODEL="${MODEL:-$(ls models/*.gguf 2>/dev/null | grep -v -i mmproj | head -1)}"
[ -n "$MODEL" ] || { echo "No .gguf model in models/"; exit 1; }
MMPROJ=$(ls models/*mmproj*.gguf 2>/dev/null | head -1)
CTX="${CTX:-8192}"

# Static Linux binaries; if the drive won't let us execute, run copies from /tmp.
BIN="$KIT/bin/linux"
if ! "$BIN/miniserve" --version >/dev/null 2>&1; then
  BIN="${TMPDIR:-/tmp}/offline-ai-kit-bin"; mkdir -p "$BIN"
  cp "$KIT/bin/linux/kiwix-serve" "$KIT/bin/linux/miniserve" "$BIN/" && chmod +x "$BIN"/*
fi

PIDS=""
cleanup(){ echo; echo "Stopping..."; kill $PIDS 2>/dev/null; exit 0; }
trap cleanup INT TERM

echo "Starting AI model ($(basename "$MODEL"))..."
# llamafile is an Actually Portable Executable; running it via sh works even on noexec mounts
# and avoids binfmt/WINE hijacking on some distros.
sh "$KIT/bin/llamafile.exe" --server -m "$MODEL" ${MMPROJ:+--mmproj "$MMPROJ"} \
   --host "$BIND" --port 8080 -c "$CTX" ${GPU:+--gpu "$GPU"} >"$LOG/llamafile.log" 2>&1 &
PIDS="$PIDS $!"

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

sleep 2
URL=http://localhost:8000/
echo
echo "  Open: $URL"
if [ "$BIND" = 0.0.0.0 ]; then
  for ip in $(hostname -I 2>/dev/null); do echo "  Phone/other devices: http://$ip:8000/"; done
fi
echo "  The AI takes ~10-60 s to load. Logs: $LOG"
echo "  Press Ctrl+C here to stop everything."
[ -z "$NO_BROWSER" ] && (xdg-open "$URL" >/dev/null 2>&1 || sensible-browser "$URL" >/dev/null 2>&1) &
wait
