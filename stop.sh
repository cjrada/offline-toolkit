#!/bin/sh
# Stop the kit: ask the launcher to shut down cleanly, then make sure every server is gone.
PIDFILE="${TMPDIR:-/tmp}/offline-toolkit-$(id -u).pid"
[ -f "$PIDFILE" ] && kill "$(cat "$PIDFILE")" 2>/dev/null && sleep 1
pkill -f 'llamafile.exe --server' ; pkill -f 'kiwix-serve --address' ; pkill -f 'miniserve .*/www' ; pkill -f 'miniserve .*offline-toolkit-control'
rm -f "$PIDFILE"; echo "Offline Kit stopped."
