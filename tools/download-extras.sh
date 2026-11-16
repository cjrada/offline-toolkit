#!/bin/sh
# Optional: hand-pick extras. Easier: `bash tools/build-kit.sh --tier medium|large` builds a whole tier with
# checksum verification. Run on an internet-connected Linux box from the kit folder.
# Nothing here runs automatically. Uncomment what you want. Sizes are approximate (Nov 2026).
# shellcheck disable=SC2034 # used by the commented-out lines below
HF=https://huggingface.co; KZ=https://download.kiwix.org/zim
cd "$(dirname "$0")/.." || exit 1
mkdir -p models/spare library
## ---- Models. Files in models/ are auto-picked by RAM (tools/pick-model.sh); models/spare/ only via
##      MODEL=models/spare/xxx.gguf sh start.sh. Change models/spare/ to models/ below to make one auto-pickable.
# curl -L -o models/spare/qwen3-1.7b-q4_k_m.gguf  $HF/unsloth/Qwen3-1.7B-GGUF/resolve/main/Qwen3-1.7B-Q4_K_M.gguf            # 1.1 GB, old laptops
# curl -L -o models/spare/qwen3-8b-q4_k_m.gguf    $HF/unsloth/Qwen3-8B-GGUF/resolve/main/Qwen3-8B-Q4_K_M.gguf                # 5.0 GB, 16 GB RAM
# curl -L -o models/spare/qwen3-30b-a3b-instruct-2507-q4_k_m.gguf $HF/unsloth/Qwen3-30B-A3B-Instruct-2507-GGUF/resolve/main/Qwen3-30B-A3B-Instruct-2507-Q4_K_M.gguf  # 18.6 GB, 32 GB RAM
# Vision (photo questions): the projector must be named <model>-mmproj-*.gguf next to its model so they pair.
# curl -L -o models/spare/gemma-3-4b-it-q4_k_m.gguf $HF/unsloth/gemma-3-4b-it-GGUF/resolve/main/gemma-3-4b-it-Q4_K_M.gguf     # 2.5 GB
# curl -L -o models/spare/gemma-3-4b-it-q4_k_m-mmproj-f16.gguf $HF/unsloth/gemma-3-4b-it-GGUF/resolve/main/mmproj-F16.gguf             # 0.85 GB
# "Uncensored" (fewer refusals, NOT more accurate):
# curl -L -o models/spare/dolphin3-llama3.1-8b-q4_k_m.gguf $HF/bartowski/Dolphin3.0-Llama3.1-8B-GGUF/resolve/main/Dolphin3.0-Llama3.1-8B-Q4_K_M.gguf  # 4.9 GB
## ---- Library (Kiwix ZIMs). Check $KZ/<folder>/ for the newest date and replace it below.
# curl -L -o library/wikipedia_en_all_mini.zim    $KZ/wikipedia/wikipedia_en_all_mini_2026-09.zim      # 13 GB  all articles, intro only
# curl -L -o library/wikipedia_en_all_nopic.zim   $KZ/wikipedia/wikipedia_en_all_nopic_2026-06.zim     # 49 GB  full text, no images
# curl -L -o library/wikipedia_en_all_maxi.zim    $KZ/wikipedia/wikipedia_en_all_maxi_2026-08.zim      # 119 GB full with images
# curl -L -o library/wikipedia_en_medicine_maxi.zim $KZ/wikipedia/wikipedia_en_medicine_maxi_2026-10.zim # 1.7 GB WikiMed full
# curl -L -o library/ifixit_en_all.zim            $KZ/ifixit/ifixit_en_all_2025-12.zim                 # 3.3 GB repair guides
# curl -L -o library/wikivoyage_en_all_maxi.zim   $KZ/wikivoyage/wikivoyage_en_all_maxi_2026-09.zim    # 1.0 GB
# curl -L -o library/wikibooks_en_all_nopic.zim   $KZ/wikibooks/wikibooks_en_all_nopic_2026-10.zim     # 3.3 GB (incl. cookbook, how-tos)
# curl -L -o library/diy.stackexchange.zim        $KZ/stack_exchange/diy.stackexchange.com_en_all_2026-10.zim      # 1.9 GB
# curl -L -o library/outdoors.stackexchange.zim   $KZ/stack_exchange/outdoors.stackexchange.com_en_all_2026-10.zim # 0.14 GB
# curl -L -o library/zimgit-food-preparation_en.zim $KZ/other/zimgit-food-preparation_en_2025-04.zim   # 0.1 GB
