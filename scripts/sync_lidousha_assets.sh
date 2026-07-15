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
scp -q "$ROOT/assets/lidousha/slice_selection_metric.md" "$HOST:/opt/bilive/app/lidousha_slice_metric.md"
scp -q "$ROOT/scripts/free_silero_vad_spans.py" "$HOST:/opt/bilive/vad/silero_vad_spans.py"

# 无人值守 runner 的 repo 副本优先于 /opt/bilive/app 回退路径——必须同步更新，
# 否则 runner 用旧资产跑（2026-07-06 亲历漂移）。以 profile 清单为边界
# 同步完整 manifest + asset tree，避免每加一个独立资产都漏改这份脚本。
# tar 只覆盖 repo 内的 tracked/staged tree，不会删除或触碰 repo 外的片头媒体。
if ssh "$HOST" "test -d /opt/bilive/autoslice/repo/assets/lidousha"; then
  python3 "$ROOT/scripts/validate_channel_profile.py" --profile lidousha >/dev/null
  tar -C "$ROOT" -cf - assets/lidousha profiles/lidousha \
    | ssh "$HOST" "tar -C /opt/bilive/autoslice/repo -xf -"
  echo "runner profile manifest + complete asset tree synced"
fi
echo "synced glossary + subtitle principles + slice metric + vad script to $HOST"
