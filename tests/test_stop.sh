#!/bin/sh
# stop.sh stops only the kit it belongs to (stub binaries, no network):
#  * the launcher is asked to exit (its PID file is per user AND per kit folder), and it stops its servers
#  * a stale PID file whose PID now belongs to some other process does not get that process killed
#  * leftovers of a launcher killed with -9 are found by this kit's absolute paths - also when the
#    binaries ran from the $TMPDIR/offline-toolkit-bin copy (noexec drive)
#  * another kit's servers, and a kit in a folder whose path merely CONTAINS this kit's path, keep running
# Usage: sh tests/test_stop.sh
set -u
HERE="$(cd "$(dirname "$0")/.." && pwd)"
T="$(mktemp -d)"
FOREIGN=""
cleanup(){
  for p in $FOREIGN; do kill "$p" 2>/dev/null; done
  for d in "$T/ka" "$T/kb" "$T/kc$T/ka" "$T/kd"; do [ -f "$d/stop.sh" ] && TMPDIR="$T/tmp" sh "$d/stop.sh" >/dev/null 2>&1; done
  rm -rf "$T"
}
trap cleanup EXIT
mkdir -p "$T/tmp"
fail=0
ok(){ echo "ok   $1"; }
bad(){ echo "FAIL $1"; fail=1; }

# A stub server: keeps its command line visible to ps and exits on TERM.
STUB='#!/bin/sh
[ "${1:-}" = --version ] && exit ${STUB_VERSION_RC:-0}
trap "exit 0" TERM
while :; do sleep 1; done'

mkkit(){ # dir [noexec]  - a kit with stub binaries; noexec: bin/linux fails --version, so start.sh uses the /tmp copy
  K="$1"; mkdir -p "$K/tools" "$K/bin/linux" "$K/models" "$K/www" "$K/library"
  cp "$HERE/start.sh" "$HERE/stop.sh" "$K/"; cp "$HERE/tools/pick-model.sh" "$K/tools/"
  truncate -s 100M "$K/models/qwen3-1.7b-q4_k_m.gguf"; : >"$K/library/test lib.zim"
  for b in miniserve kiwix-serve; do printf '%s\n' "$STUB" >"$K/bin/linux/$b"; chmod +x "$K/bin/linux/$b"; done
  printf '%s\n' "$STUB" >"$K/bin/llamafile.exe"
  if [ "${2:-}" = noexec ]; then
    # fails only where it lies (like a noexec mount); the copy start.sh makes in $TMPDIR runs
    { echo '#!/bin/sh'; echo 'case "$0" in */bin/linux/*) exit 1 ;; esac'; printf '%s\n' "$STUB" | sed 1d; } >"$K/bin/linux/miniserve"
  fi
}
start_kit(){ # dir -> waits until its three servers run
  (cd "$1" && env TMPDIR="$T/tmp" NO_BROWSER=1 RAM_MB=8000 VRAM_MB=0 HEALTH_URL="file://$1/none" sh start.sh >"$1/out" 2>&1) &
  i=0; while [ "$(procs "$1" | wc -l)" -lt 4 ] && [ $i -lt 20 ]; do sleep 0.5; i=$((i + 1)); done
}
procs(){ # dir -> PIDs of processes whose command line names the dir (launcher not included: it runs as "sh start.sh")
  ps -u "$(id -u)" -o pid= -o args= | while read -r pid args; do
    case " $args" in *" $1/"*) echo "$pid" ;; esac
  done
}
launcher(){ cat "$T"/tmp/offline-toolkit-"$(id -u)"-"$(printf '%s' "$1" | cksum | cut -d' ' -f1)".pid 2>/dev/null; }
alive(){ # running, not a zombie (orphans of a kill -9'd launcher may never be reaped in a container)
  _st=$(ps -o stat= -p "$1" 2>/dev/null); [ -n "$_st" ] && case "$_st" in Z*) return 1 ;; esac; }
all_alive(){ for p in $1; do alive "$p" || return 1; done; [ -n "$1" ]; }
none_alive(){ for p in $1; do alive "$p" && return 1; done; return 0; }

A="$T/ka"; B="$T/kb"; mkkit "$A"; mkkit "$B" noexec
# A "kit" whose folder path contains A's path ($T/kc$T/ka/...): must never match A.
C="$T/kc$A"; mkkit "$C"
start_kit "$A"; start_kit "$B"; start_kit "$C"
PA=$(procs "$A"); PB=$(procs "$B"); PC=$(procs "$C"); LA=$(launcher "$A"); LB=$(launcher "$B"); LC=$(launcher "$C")
[ "$(echo $PA | wc -w)" -ge 3 ] && [ "$(echo $PB | wc -w)" -ge 3 ] && [ -n "$LA" ] && [ -n "$LB" ] \
  && ok "three kits started (A, B from the /tmp binary copy, C nested)" || bad "kits did not start: A=[$PA] B=[$PB] C=[$PC] $(cat "$A/out" "$B/out")"
[ -x "$T/tmp/offline-toolkit-bin/kiwix-serve" ] && ok "noexec path: B runs kiwix-serve/miniserve from \$TMPDIR/offline-toolkit-bin" \
  || bad "no /tmp binary copy"
[ "$LA" != "$LB" ] && ok "per-kit PID files" || bad "PID files collide"

# Your own servers of the same kind, outside any kit: the old pkill -f patterns matched these.
mkdir -p "$T/own/www"; printf '%s\n' "$STUB" >"$T/own/llamafile.exe"; printf '%s\n' "$STUB" >"$T/own/miniserve"; chmod +x "$T/own/miniserve"
sh "$T/own/llamafile.exe" --server -m x.gguf & FOREIGN="$FOREIGN $!"
"$T/tmp/offline-toolkit-bin/kiwix-serve" --address=127.0.0.1 --port=9999 "$T/own/x.zim" & FOREIGN="$FOREIGN $!"
"$T/own/miniserve" "$T/own/www" -p 9998 & FOREIGN="$FOREIGN $!"
sleep 1

# 1. stop A: launcher + its servers gone; B, C and your own servers untouched
TMPDIR="$T/tmp" sh "$A/stop.sh" >"$T/stopA.out" 2>&1; sleep 1.5
none_alive "$PA $LA" && ok "stop.sh stopped kit A (launcher and servers)" || bad "kit A still running: $(ps -o pid=,args= -p "$(echo $PA $LA | tr ' ' ,)")"
all_alive "$PB $LB" && ok "kit B untouched" || bad "kit B was hit"
all_alive "$PC $LC" && ok "kit C (path contains A's path) untouched" || bad "kit C was hit"
all_alive "$FOREIGN" && ok "your own llamafile / kiwix-serve / miniserve untouched" || bad "unrelated servers were killed"
[ ! -e "$T/tmp/offline-toolkit-$(id -u)-$(printf '%s' "$A" | cksum | cut -d' ' -f1).pid" ] && ok "A's PID file removed" || bad "A's PID file left"

# 2. B's launcher dies hard (kill -9): stop.sh still finds B's servers by path, from the /tmp copy too
kill -9 "$LB"; sleep 0.5
all_alive "$PB" && ok "B's servers outlived a kill -9 of its launcher (setup)" || bad "setup: B's servers died with the launcher"
TMPDIR="$T/tmp" sh "$B/stop.sh" >"$T/stopB.out" 2>&1; sleep 1.5
none_alive "$PB" && ok "leftovers of B stopped by path" || bad "B leftovers still running: $(cat "$T/stopB.out")"
grep -q "Stopped leftover kit processes" "$T/stopB.out" && ok "stop.sh reports the leftovers" || bad "no leftover report: $(cat "$T/stopB.out")"
all_alive "$PC $LC" && all_alive "$FOREIGN" && ok "C and your own servers still untouched" || bad "C or own servers hit by B's stop"

# 3. A stale PID file naming some other process: that process is not killed
D="$T/kd"; mkkit "$D"
sleep 600 & VICTIM=$!; FOREIGN="$FOREIGN $VICTIM"
echo "$VICTIM" >"$T/tmp/offline-toolkit-$(id -u)-$(printf '%s' "$D" | cksum | cut -d' ' -f1).pid"
TMPDIR="$T/tmp" sh "$D/stop.sh" >/dev/null 2>&1; sleep 0.5
alive "$VICTIM" && ok "stale PID file (PID reused by another program): not killed" || bad "killed an unrelated PID"

# 4. C stops cleanly too
TMPDIR="$T/tmp" sh "$C/stop.sh" >/dev/null 2>&1; sleep 1.5
none_alive "$PC $LC" && ok "kit in a nested path stops itself" || bad "C still running"
grep -q "Offline Kit stopped." "$T/stopA.out" && ok "prints 'Offline Kit stopped.'" || bad "message: $(cat "$T/stopA.out")"
exit $fail
