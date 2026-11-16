#!/bin/sh
# Pick the best AI model for this computer's RAM. Sourced by start.sh; can also be run on its own:
#   sh tools/pick-model.sh            -> prints MODEL / MMPROJ / CTX it would use
#   KIT_RAM_MB=4096 sh tools/pick-model.sh   (pretend the laptop has 4 GB, for testing)
#
# Rules (see README "Model selection"):
#   * Candidates are the .gguf files directly in models/ (models/spare/ is never auto-picked).
#   * A vision model is a model with a matching projector "<model-stem>-mmproj*.gguf". Plain
#     text models are preferred (vision needs more RAM and is slower for chat); VISION=1 prefers
#     vision models instead. A vision model is still used if it is the only one that fits.
#   * RAM needed ~= model file (+ projector) + context cache + ~2 GB for the OS and browser.
#     The largest model that fits wins. If nothing fits, the context is shrunk and finally the
#     smallest model is used anyway (with a warning).
#   * MODEL=path, CTX=n always override. MODEL may be "auto" (the default).

_pm_size_mb() { # file size in MiB (wc -c is portable and uses stat() for regular files)
  _b=$(wc -c <"$1" 2>/dev/null | tr -d ' ') || _b=0
  echo $(( ${_b:-0} / 1048576 ))
}

_pm_ram_mb() {
  if [ -n "${KIT_RAM_MB:-}" ]; then echo "$KIT_RAM_MB"; return; fi
  if [ -r /proc/meminfo ]; then
    awk '/^MemTotal:/ { printf "%d\n", $2 / 1024 }' /proc/meminfo; return
  fi
  # macOS / BSD
  _r=$(sysctl -n hw.memsize 2>/dev/null) && { echo $(( _r / 1048576 )); return; }
  echo 8192 # unknown: assume a typical 8 GB laptop
}

_pm_mmproj_for() { # $1 = model path; prints matching projector or nothing
  _stem=${1%.gguf}
  for _p in "$_stem"-mmproj*.gguf; do [ -f "$_p" ] && { echo "$_p"; return; }; done
  # Back-compat with the original prototype layout: exactly one model + one projector in models/.
  _n=0; _proj=""
  for _f in "$(dirname "$1")"/*.gguf; do
    case "$_f" in *mmproj*) _proj=$_f ;; *) _n=$((_n + 1)) ;; esac
  done
  [ "$_n" -eq 1 ] && [ -n "$_proj" ] && echo "$_proj"
}

_pm_ctx_mb() { # rough KV-cache + compute buffers for a context size
  if [ "$1" -ge 8192 ]; then echo 1536; elif [ "$1" -ge 4096 ]; then echo 768; else echo 384; fi
}

# pick_model KITDIR -> sets MODEL, MMPROJ, CTX, PICK_REASON
pick_model() {
  _kit=$1
  RAM_MB=$(_pm_ram_mb)
  _os_mb=2048; [ "$RAM_MB" -le 6144 ] && _os_mb=1536
  if [ -n "${MODEL:-}" ] && [ "$MODEL" != auto ]; then
    [ -f "$MODEL" ] || [ -f "$_kit/$MODEL" ] || { echo "MODEL=$MODEL not found" >&2; return 1; }
    MMPROJ=${MMPROJ:-$(_pm_mmproj_for "$MODEL")}
    CTX=${CTX:-8192}; PICK_REASON="set by MODEL="
    return 0
  fi
  _ctx_list=${CTX:-"8192 4096 2048"}
  # Plain text models first; vision models first only with VISION=1.
  _modes="text vision"; [ "${VISION:-0}" = 1 ] && _modes="vision text"
  for _mode in $_modes; do
  for _ctx in $_ctx_list; do
    _budget=$(( RAM_MB - _os_mb - $(_pm_ctx_mb "$_ctx") ))
    _best=""; _best_mb=-1; _best_proj=""
    for _f in "$_kit"/models/*.gguf; do
      [ -f "$_f" ] || continue
      case "$_f" in *mmproj*) continue ;; esac
      _proj=$(_pm_mmproj_for "$_f")
      if [ -n "$_proj" ]; then [ "$_mode" = vision ] || continue
      else [ "$_mode" = text ] || continue; fi
      _mb=$(_pm_size_mb "$_f")
      [ -n "$_proj" ] && _mb=$(( _mb + $(_pm_size_mb "$_proj") ))
      if [ "$_mb" -le "$_budget" ] && [ "$_mb" -gt "$_best_mb" ]; then
        _best=$_f; _best_mb=$_mb; _best_proj=$_proj
      fi
    done
    if [ -n "$_best" ]; then
      MODEL=${_best#"$_kit"/}; MMPROJ=${_best_proj#"$_kit"/}; CTX=$_ctx
      PICK_REASON="best fit for ${RAM_MB} MB RAM${_best_proj:+ (vision)}"
      return 0
    fi
  done
  done
  # Nothing fits: smallest model, small context.
  _best=""; _best_mb=""
  for _f in "$_kit"/models/*.gguf; do
    [ -f "$_f" ] || continue
    case "$_f" in *mmproj*) continue ;; esac
    _mb=$(_pm_size_mb "$_f")
    if [ -z "$_best_mb" ] || [ "$_mb" -lt "$_best_mb" ]; then _best=$_f; _best_mb=$_mb; fi
  done
  [ -n "$_best" ] || { echo "No .gguf model in $_kit/models/" >&2; return 1; }
  MODEL=${_best#"$_kit"/}; MMPROJ=$(_pm_mmproj_for "$_best"); MMPROJ=${MMPROJ#"$_kit"/}
  CTX=${CTX:-2048}
  PICK_REASON="WARNING: only ${RAM_MB} MB RAM - even the smallest model may be slow or fail; close other apps"
  return 0
}

case "$0" in
  *pick-model.sh)
    _here=$(cd "$(dirname "$0")/.." && pwd)
    pick_model "${1:-$_here}" || exit 1
    printf 'RAM_MB=%s\nMODEL=%s\nMMPROJ=%s\nCTX=%s\n# %s\n' "$RAM_MB" "$MODEL" "$MMPROJ" "$CTX" "$PICK_REASON"
    ;;
esac
