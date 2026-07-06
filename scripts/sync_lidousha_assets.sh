#!/bin/bash
# Push the repo-vendored Li Dousha knowledge assets to the free host.
# Repo assets/lidousha/ is the single source of truth; the production daemon
# on free reads /opt/bilive/app/lidousha_glossary.txt.
# Term-learning loop: Ivan corrects a name -> edit assets/lidousha/glossary.txt
# -> run this script. Never edit the copy on free directly.
set -euo pipefail

HOST="${1:-free}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

scp -q "$ROOT/assets/lidousha/glossary.txt" "$HOST:/opt/bilive/app/lidousha_glossary.txt"
scp -q "$ROOT/assets/lidousha/subtitle_correction_principles.md" "$HOST:/opt/bilive/app/lidousha_subtitle_principles.md"
scp -q "$ROOT/scripts/free_silero_vad_spans.py" "$HOST:/opt/bilive/vad/silero_vad_spans.py"
echo "synced glossary + subtitle principles + vad script to $HOST"
