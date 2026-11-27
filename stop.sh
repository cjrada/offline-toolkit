#!/bin/sh
# Stop THIS kit: ask its launcher (start.sh) to shut down cleanly, then stop any of its servers that are
# still running. Only processes of this user that belong to this kit folder are touched: another kit copy,
# or your own llamafile / kiwix-serve / miniserve, keeps running. A server counts as this kit's when its
# command line names this folder: bin/llamafile.exe, www/ and library/ are passed as absolute paths, also
# when the binaries run from the $TMPDIR/offline-toolkit-bin copy (noexec drives).
KIT="$(cd "$(dirname "$0")" && pwd)" || exit 1
TMPD="${TMPDIR:-/tmp}"
KIT_ID=$(printf '%s' "$KIT" | cksum | cut -d' ' -f1)
PIDFILE="$TMPD/offline-toolkit-$(id -u)-$KIT_ID.pid"
CTL="$TMPD/offline-toolkit-control-$(id -u)"

STOPPED_LAUNCHER=""
args_of(){ ps -o args= -p "$1" 2>/dev/null; }

# 1. The launcher, if the PID file still names a running start.sh (not a reused PID). Its Ctrl+C handler
#    stops the AI, the library and both web servers.
if [ -f "$PIDFILE" ]; then
  LPID=$(head -c 20 "$PIDFILE" | tr -dc '0-9')
  if [ -n "$LPID" ] && case "$(args_of "$LPID")" in *start.sh*) true ;; *) false ;; esac; then
    kill "$LPID" 2>/dev/null && STOPPED_LAUNCHER=1
    i=0; while kill -0 "$LPID" 2>/dev/null && [ $i -lt 10 ]; do sleep 0.5; i=$((i + 1)); done
  fi
  rm -f "$PIDFILE"
fi

# 2. Leftovers (the launcher was killed hard, or is gone): this user's processes whose command line names
#    this kit's own files. Plain string matching (case), no regex, so any folder name is safe; " $args"
#    makes a path start a word, so a kit at /other/place/kit never matches the kit at /place/kit.
#    The model-switch service (127.0.0.1:8079) serves a per-user folder, so it only counts as this kit's
#    when it runs from this kit's bin/linux, or from the shared /tmp copy while this kit has other leftovers.
pids_where(){ # $1 = kit | control
  ps -u "$(id -u)" -o pid= -o args= 2>/dev/null | while read -r pid args; do
    case "$1: $args" in
      kit:*" $KIT/bin/llamafile.exe "*--server*|kit:*kiwix-serve*--address*" $KIT/library/"*|kit:*miniserve*" $KIT/www "*) echo "$pid" ;;
      control:*" $KIT/bin/linux/miniserve $CTL "*|control:*" $TMPD/offline-toolkit-bin/miniserve $CTL "*) echo "$pid" ;;
    esac
  done
}
gone(){ for p in $1; do kill -0 "$p" 2>/dev/null && return 1; done; return 0; }
LEFT=$(pids_where kit); i=0
while [ -n "$LEFT" ] && [ -n "$STOPPED_LAUNCHER" ] && [ $i -lt 6 ]; do   # its servers may take a moment to exit
  sleep 0.5; i=$((i + 1)); gone "$LEFT" && LEFT=""
done
LEFT=$(pids_where kit)
if [ -n "$LEFT" ]; then
  # shellcheck disable=SC2046,SC2086
  kill $LEFT $(pids_where control) 2>/dev/null
  echo "Stopped leftover kit processes:" $LEFT
else
  # this kit's own switch service (not the shared /tmp copy) left behind
  # shellcheck disable=SC2046
  kill $(ps -u "$(id -u)" -o pid= -o args= 2>/dev/null | while read -r pid args; do
    case " $args" in *" $KIT/bin/linux/miniserve $CTL "*) echo "$pid" ;; esac; done) 2>/dev/null
fi
echo "Offline Kit stopped."
