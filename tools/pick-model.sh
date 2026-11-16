#!/bin/sh
# Auto model selection, sourced by start.sh (POSIX sh). Sets MODEL, MMPROJ, CTX and PICK_REASON.
#
# Rule: use the LARGEST models/*.gguf whose file size + headroom fits in this computer's RAM, where
#   headroom = context cache (~1 GB at 8k tokens) + ~2 GB for the OS and browser = 3072 MB.
# If nothing fits, use the smallest model with a 4k context. models/spare/ is never auto-picked
# (vision / "uncensored" / experimental models) - choose those with MODEL=models/spare/x.gguf.
# A vision model's projector is paired by name: models/x.gguf -> models/x.mmproj.gguf.
# Overrides: MODEL=..., CTX=..., RAM_MB=... (pretend RAM size, for testing), HEADROOM_MB=...
# Show the decision without starting anything: sh tools/pick-model.sh

pick_ram_mb() {
  if [ -n "$RAM_MB" ]; then echo "$RAM_MB"; return; fi
  if [ -r /proc/meminfo ]; then awk '/^MemTotal:/ {print int($2/1024); exit}' /proc/meminfo; return; fi
  b=$(sysctl -n hw.memsize 2>/dev/null) && [ -n "$b" ] && { echo $((b / 1048576)); return; }
  echo 0
}

pick_size_mb() { # apparent size of a file in MB (wc -c uses stat for regular files)
  s=$(wc -c <"$1" 2>/dev/null) || s=0
  echo $(( (s + 1048575) / 1048576 ))
}

pick_model() {
  _dir="${MODELS_DIR:-models}"
  _ram=$(pick_ram_mb)
  _head="${HEADROOM_MB:-3072}"
  if [ -n "$MODEL" ]; then
    [ -f "$MODEL" ] || { echo "MODEL=$MODEL does not exist" >&2; return 1; }
    PICK_REASON="MODEL set by you"
  else
    _best=""; _best_mb=0; _small=""; _small_mb=0
    for _f in "$_dir"/*.gguf; do
      [ -f "$_f" ] || continue
      case "$(basename "$_f" | tr 'A-Z' 'a-z')" in *mmproj*) continue ;; esac
      _mb=$(pick_size_mb "$_f")
      if [ -z "$_small" ] || [ "$_mb" -lt "$_small_mb" ]; then _small="$_f"; _small_mb=$_mb; fi
      if [ "$_ram" -gt 0 ] && [ $((_mb + _head)) -le "$_ram" ] && [ "$_mb" -gt "$_best_mb" ]; then
        _best="$_f"; _best_mb=$_mb
      fi
    done
    [ -n "$_small" ] || { echo "No .gguf model in $_dir/" >&2; return 1; }
    if [ -n "$_best" ]; then
      MODEL="$_best"; PICK_REASON="largest model that fits ${_ram} MB RAM (${_best_mb} MB + ${_head} MB headroom)"
    elif [ "$_ram" -eq 0 ]; then
      MODEL="$_small"; PICK_REASON="could not read RAM size; using the smallest model"
    else
      MODEL="$_small"; PICK_REASON="nothing fits ${_ram} MB RAM comfortably; using the smallest model with a 4k context (expect it to be slow)"
      CTX="${CTX:-4096}"
    fi
  fi
  CTX="${CTX:-8192}"
  MMPROJ=""
  _mm="${MODEL%.gguf}.mmproj.gguf"
  [ -f "$_mm" ] && MMPROJ="$_mm"
  return 0
}

# Run directly: print the decision.
case "$0" in
  *pick-model.sh)
    cd "$(dirname "$0")/.." || exit 1
    pick_model || exit 1
    echo "RAM:    $(pick_ram_mb) MB"
    echo "MODEL:  $MODEL"
    echo "MMPROJ: ${MMPROJ:-none}"
    echo "CTX:    $CTX"
    echo "Why:    $PICK_REASON"
    ;;
esac
