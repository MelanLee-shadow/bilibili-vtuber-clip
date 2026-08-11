#!/bin/bash
# 负向金丝雀：逐条破坏实现，确认对应测试真的变红（不是恒绿的装饰）。
set -u
ROOT=/Users/ivan/Project/vtuber-slice-wt/host-occupancy
PY=/Users/ivan/Project/vtuber-slice/.venv/bin/python
SRC=$ROOT/src/autoslice/host_occupancy.py
BAK=$(mktemp)
cp "$SRC" "$BAK"

run() { # $1 = label, $2 = test selector
  cd "$ROOT" || exit 1
  find "$ROOT/src" -name '*.pyc' -delete 2>/dev/null
  out=$("$PY" -m pytest "tests/lidousha/test_host_occupancy.py::$2" -q 2>&1 | tail -1)
  if echo "$out" | grep -q "failed\|error"; then
    echo "CANARY $1: RED (good) -- $out"
  else
    echo "CANARY $1: STILL GREEN (BAD) -- $out"
  fi
  cp "$BAK" "$SRC"
  find "$ROOT/src" -name '*.pyc' -delete 2>/dev/null
}

mutate() { "$PY" - "$@" <<'EOF'
import sys, pathlib
path = pathlib.Path("/Users/ivan/Project/vtuber-slice-wt/host-occupancy/src/autoslice/host_occupancy.py")
text = path.read_text(encoding="utf-8")
old, new = sys.argv[1], sys.argv[2]
assert old in text, f"pattern not found: {old!r}"
path.write_text(text.replace(old, new, 1), encoding="utf-8")
EOF
}

# ① 声学同质当单人放行（把 SOLO 严格门降成"没听见别人就算单人"）
mutate '    if all(solo_audit.values()):' '    if longest_other_ms < SOLO_MAX_OTHER_RUN_MS:'
run "1_solo_strict_gate" "test_quiet_but_not_strict_candidate_is_unknown_not_solo"

# ② UNKNOWN 猜成 SOLO 放行
mutate '    if state == UNKNOWN:
        return ATTRIBUTION_UNVERIFIED' '    if state == UNKNOWN:
        return ATTRIBUTION_VERIFIED_SOLO'
run "2_unknown_must_park" "test_unknown_maps_to_unverified_and_parks"

# ③ 主播不是主体 → 当存疑送人工（淹没人工队列 + 强制压低）
mutate '    return ATTRIBUTION_VERIFIED_HOST_MINOR' '    return ATTRIBUTION_UNVERIFIED'
run "3_host_minor_never_parks" "test_host_minor_is_verified_and_never_parks"

# ④ 栅格锚在候选起点（重叠候选缓存永不命中）
mutate '    first_index = max(0, -(-(start_ms - GRID_ANCHOR_MS) // HOP_MS))' '    first_index = 0
    start_ms, end_ms = 0, end_ms - start_ms'
run "4_global_grid_anchor" "test_window_grid_is_globally_anchored_not_candidate_anchored"

# ⑤ 不扩窗
mutate '    requested_start = span.start_ms - CANDIDATE_PAD_MS
    requested_end = span.end_ms + CANDIDATE_PAD_MS' '    requested_start = span.start_ms
    requested_end = span.end_ms'
run "5_padding" "test_padding_extends_both_sides"

# ⑥ 取消最短持续时间约束
mutate 'SMOOTHING_MIN_RUN_WINDOWS = 2' 'SMOOTHING_MIN_RUN_WINDOWS = 1'
run "6_min_run_smoothing" "test_single_window_other_island_cannot_support_multi"

# ⑦ 补位无界
mutate 'CONTENTION_DETECTION_CAP = 2 * CONTENTION_SET_SIZE' 'CONTENTION_DETECTION_CAP = 10_000'
run "7_bounded_backfill" "test_backfill_stops_at_the_detection_cap"

# ⑧ 争席排序看 centrality
mutate '    return (float(scorecard["tier"]), -bounds[1], -confidence, candidate_id)' '    return (
        float(scorecard["tier"]),
        -float(scorecard["effective_score"]),
        -confidence,
        candidate_id,
    )'
run "8_order_ignores_centrality" "test_contention_order_ignores_centrality"

# ⑨ 归属门失效（覆盖率不够也敢下结论）
mutate '    if not all(gates.values()):' '    if False:'
run "9_attribution_gate" "test_attribution_gate_failure_precedes_every_other_verdict"

# ⑩ 双阈值塌成单阈值（取消 abstain 带）
mutate 'OTHER_SIMILARITY_MAX = 0.31' 'OTHER_SIMILARITY_MAX = 0.50'
run "10_dual_threshold_band" "test_dual_threshold_band_abstains"

cp "$BAK" "$SRC"
cd "$ROOT" && git diff --stat -- src/autoslice/host_occupancy.py
echo "restored (empty diff above = clean)"

# 2026-08-11 实跑结果（10/10 全红，即每条守卫都真的被测到）：
#   1_solo_strict_gate           RED   声学同质当单人放行
#   2_unknown_must_park          RED   UNKNOWN 猜成 SOLO 放行
#   3_host_minor_never_parks     RED   主播不是主体 → 误送人工
#   4_global_grid_anchor         RED   栅格锚在候选起点（缓存永不命中）
#   5_padding                    RED   不扩窗
#   6_min_run_smoothing          RED   取消最短持续时间约束
#   7_bounded_backfill           RED   补位无界
#   8_order_ignores_centrality   RED   争席排序看 centrality
#   9_attribution_gate           RED   归属门失效
#  10_dual_threshold_band        RED   双阈值塌成单阈值
