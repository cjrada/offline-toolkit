#!/bin/sh
# Model selection, sourced by start.sh (POSIX sh). pick_model sets MODEL, MMPROJ, CTX, PICK_REASON.
#
# Rule (same as tools/pick-model.ps1):
#  1. GPU: if a GPU reports VRAM (nvidia-smi; AMD via sysfs or rocm-smi), use the LARGEST models/*.gguf
#     with  size + 1024 MB <= VRAM.
#  2. Otherwise RAM: the LARGEST models/*.gguf with  size + 4096 MB <= RAM  (context cache + OS/browser).
#  3. Otherwise the smallest model with a 4k context (slow, but it runs).
# models/spare/ is never auto-picked (vision / "uncensored" / experimental) but can be chosen by hand,
# from the home page or with MODEL=models/spare/x.gguf. Projectors pair by name: x.gguf + x.mmproj.gguf.
# Overrides: MODEL, CTX, GPU=disable (ignore the GPU), RAM_MB / VRAM_MB (pretend sizes, for tests),
# HEADROOM_MB (default 4096), GPU_HEADROOM_MB (default 1024).
# Show the decision and the list without starting anything:  sh tools/pick-model.sh [--json]

pick_ram_mb() {
  if [ -n "${RAM_MB:-}" ]; then echo "$RAM_MB"; return; fi
  if [ -r /proc/meminfo ]; then awk '/^MemTotal:/ {print int($2/1024); exit}' /proc/meminfo; return; fi
  b=$(sysctl -n hw.memsize 2>/dev/null) && [ -n "$b" ] && { echo $((b / 1048576)); return; }
  echo 0
}

pick_vram_mb() { # largest single GPU's memory in MB, 0 if none / GPU=disable
  if [ "${GPU:-}" = "disable" ]; then echo 0; return; fi
  if [ -n "${VRAM_MB:-}" ]; then echo "$VRAM_MB"; return; fi
  best=0
  if command -v nvidia-smi >/dev/null 2>&1; then
    for v in $(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null | tr -dc '0-9\n'); do
      [ "$v" -gt "$best" ] && best=$v
    done
  fi
  for f in /sys/class/drm/card*/device/mem_info_vram_total; do # amdgpu driver, no tools needed
    [ -r "$f" ] || continue
    v=$(( $(cat "$f" 2>/dev/null || echo 0) / 1048576 ))
    [ "$v" -gt "$best" ] && best=$v
  done
  if [ "$best" -eq 0 ] && command -v rocm-smi >/dev/null 2>&1; then
    for v in $(rocm-smi --showmeminfo vram --csv 2>/dev/null | awk -F, 'NR>1 && $2+0>0 {print int($2/1048576)}'); do
      [ "$v" -gt "$best" ] && best=$v
    done
  fi
  echo "$best"
}

pick_size_mb() { # apparent size in MB (wc -c uses stat for regular files)
  s=$(wc -c <"$1" 2>/dev/null) || s=0
  echo $(( (s + 1048575) / 1048576 ))
}

pick_fit() { # size_mb -> gpu | ram | tight | too-big   (uses _ram _vram _head _ghead)
  if [ "$_vram" -gt 0 ] && [ $(($1 + _ghead)) -le "$_vram" ]; then echo gpu
  elif [ "$_ram" -gt 0 ] && [ $(($1 + _head)) -le "$_ram" ]; then echo ram
  elif [ "$_ram" -gt 0 ] && [ $(($1 + 2048)) -le "$_ram" ]; then echo tight
  else echo too-big; fi
}

pick_is_model() { case "$(basename "$1" | tr 'A-Z' 'a-z')" in *mmproj*) return 1 ;; *.gguf) [ -f "$1" ] ;; *) return 1 ;; esac; }

pick_model() {
  _dir="${MODELS_DIR:-models}"
  _ram=$(pick_ram_mb); _vram=$(pick_vram_mb)
  _head="${HEADROOM_MB:-4096}"; _ghead="${GPU_HEADROOM_MB:-1024}"
  if [ -n "${MODEL:-}" ]; then
    [ -f "$MODEL" ] || { echo "MODEL=$MODEL does not exist" >&2; return 1; }
    _fit=$(pick_fit "$(pick_size_mb "$MODEL")")
    PICK_REASON="chosen by you ($_fit)"
  else
    _gpu=""; _gpu_mb=0; _best=""; _best_mb=0; _small=""; _small_mb=0
    for _f in "$_dir"/*.gguf; do
      pick_is_model "$_f" || continue
      _mb=$(pick_size_mb "$_f")
      if [ -z "$_small" ] || [ "$_mb" -lt "$_small_mb" ]; then _small="$_f"; _small_mb=$_mb; fi
      case $(pick_fit "$_mb") in
        gpu) [ "$_mb" -gt "$_gpu_mb" ] && { _gpu="$_f"; _gpu_mb=$_mb; }
             [ $((_mb + _head)) -le "$_ram" ] && [ "$_mb" -gt "$_best_mb" ] && { _best="$_f"; _best_mb=$_mb; } ;;
        ram) [ "$_mb" -gt "$_best_mb" ] && { _best="$_f"; _best_mb=$_mb; } ;;
      esac
    done
    [ -n "$_small" ] || { echo "No .gguf model in $_dir/" >&2; return 1; }
    if [ -n "$_gpu" ]; then
      MODEL="$_gpu"; _fit=gpu; PICK_REASON="largest model that fits the GPU (${_vram} MB VRAM)"
    elif [ -n "$_best" ]; then
      MODEL="$_best"; _fit=ram; PICK_REASON="largest model that fits ${_ram} MB RAM (+${_head} MB headroom)"
    elif [ "$_ram" -eq 0 ]; then
      MODEL="$_small"; _fit=tight; PICK_REASON="could not read RAM size; smallest model"
    else
      MODEL="$_small"; _fit=tight; PICK_REASON="nothing fits ${_ram} MB RAM comfortably; smallest model, 4k context (slow)"
    fi
  fi
  case "$_fit" in gpu|ram) CTX="${CTX:-8192}" ;; *) CTX="${CTX:-4096}" ;; esac
  MMPROJ=""
  _mm="${MODEL%.gguf}.mmproj.gguf"
  [ -f "$_mm" ] && MMPROJ="$_mm"
  return 0
}

# JSON for the home page's model switcher (www/models.json). Only names made of safe characters are
# listed, so no escaping is needed; others still work with MODEL=.
pick_models_json() {
  _dir="${MODELS_DIR:-models}"
  printf '{"current":"%s","ctx":%s,"reason":"%s","ram_mb":%s,"vram_mb":%s,"switch_port":8079,"models":[' \
    "$MODEL" "$CTX" "$PICK_REASON" "$_ram" "$_vram"
  _sep=""
  for _f in "$_dir"/*.gguf "$_dir"/spare/*.gguf; do
    pick_is_model "$_f" || continue
    case "$_f" in *[!A-Za-z0-9._/-]*) continue ;; esac
    _mb=$(pick_size_mb "$_f"); _sp=false; case "$_f" in */spare/*) _sp=true ;; esac
    _v=false; [ -f "${_f%.gguf}.mmproj.gguf" ] && _v=true
    printf '%s{"path":"%s","name":"%s","mb":%s,"fit":"%s","spare":%s,"vision":%s}' \
      "$_sep" "$_f" "$(basename "$_f" .gguf)" "$_mb" "$(pick_fit "$_mb")" "$_sp" "$_v"
    _sep=","
  done
  printf ']}\n'
}

# A switch request is only accepted if it names an existing model inside models/ or models/spare/.
pick_valid_request() {
  case "$1" in
    *[!A-Za-z0-9._/-]*|*..*) return 1 ;;
    models/*/*/*) return 1 ;;
    models/spare/*.gguf|models/*.gguf) pick_is_model "$1" ;;
    *) return 1 ;;
  esac
}

case "$0" in
  *pick-model.sh)
    cd "$(dirname "$0")/.." || exit 1
    pick_model || exit 1
    if [ "${1:-}" = "--json" ]; then pick_models_json; exit 0; fi
    echo "RAM:    $_ram MB    GPU VRAM: $_vram MB"
    echo "MODEL:  $MODEL"
    echo "MMPROJ: ${MMPROJ:-none}"
    echo "CTX:    $CTX"
    echo "Why:    $PICK_REASON"
    ;;
esac
