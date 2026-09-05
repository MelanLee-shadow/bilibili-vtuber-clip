#!/usr/bin/env bash
# 在指定 eval 沙箱 BASE 里安全地跑一轮 runner --once。
#
# 环境变量咒语的唯一权威（两个实战坑固化于此）：
#  - 录像根变量名是 AUTOSLICE_REC_ROOT（写成 REC_ROOT 会静默落回生产全日期
#    录像根，跑去扫无关日期烧配额）；
#  - runner 例行维护地平线默认 2026-07-11，重制更早日期必须显式放行
#    AUTOSLICE_AUTOMATIC_MAINTENANCE_NOT_BEFORE，否则 tick 永远不重排失败候选。
#
# 用法： run_eval_base_once.sh /opt/bilive/autoslice/evals/<BASE> [not_before_date]
set -euo pipefail

BASE="${1:?usage: run_eval_base_once.sh <BASE-dir> [not-before-date]}"
NOT_BEFORE="${2:-2026-07-10}"

for required in "$BASE/repo/scripts/session_autoslice.py" "$BASE/recordings" "$BASE/state"; do
  [ -e "$required" ] || { echo "REFUSE: missing $required" >&2; exit 2; }
done

exec flock -n "$BASE/tick.lock" env \
  AUTOSLICE_BASE="$BASE" \
  AUTOSLICE_REC_ROOT="$BASE/recordings" \
  AUTOSLICE_IGNORE_LIVE_HOLD=1 \
  AUTOSLICE_AUTOMATIC_MAINTENANCE_NOT_BEFORE="$NOT_BEFORE" \
  GEMINI_PAID_BACKUP_DEV_EXCEPTION=1 \
  python3 "$BASE/repo/scripts/session_autoslice.py" --once
