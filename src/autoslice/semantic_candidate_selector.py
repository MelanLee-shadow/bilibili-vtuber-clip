"""LLM semantic recall selector — viewer-perspective candidate discovery.

Keyword selectors structurally under-recall a reactive streamer's actual humor: their
funny moments are usually reactive — danmaku-triggered banter (a comment
starts it, she reads/paraphrases it and riffs), quips at whatever is on
screen, tongue-twisters, meltdowns — phrased with none of the storytelling
markers the keyword lanes key on.  This lane hands the whole-session
transcript to an LLM and asks for viewer-perspective candidate windows.

Recall-stage only: every candidate still earns release through the review
gates.  The semantic contract per candidate:

- the window must include its own context trigger (the cue where the danmaku
  is read out, the topic starts, or the on-screen thing is introduced); if the
  trigger sits before the window, the LLM must extend the window start to it;
- a viewer who never saw the stream must be able to follow the clip, or at
  least reasonably infer the missing context from inside the clip.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping, Sequence

from src.autoslice.auto_review import DecisionAction
from src.autoslice.boundary_resolver import AnchorCandidate, BoundaryResolution
from src.autoslice.channel_profile import load_channel_profile
from src.autoslice.content_ip_signal import detect_important_content_ips
# Same-package reuse of the recall-stage plumbing: candidate dataclass, window
# overlap dedupe, and the canonical song-anchor boundary (song candidates must
# always go through the full-source song-boundary redo).
from src.autoslice.full_session_candidate_selector import (
    FullSessionCandidate,
    _join_text,
    _overlaps_selected,
    _song_anchor_boundary,
)
from src.autoslice.llm_client import LlmCall, LlmCallError, extract_json_object
from src.autoslice.review_evidence import SourceCue
from src.autoslice.selection_scorecard import (
    SelectionCalibrationPolicyError,
    apply_reviewed_selection_calibration,
    normalize_selection_scorecard,
    selection_rank_key,
    selection_scorecard_is_valid,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
CHANNEL_PROFILE = load_channel_profile(REPO_ROOT)

DEFAULT_MIN_TALK_WINDOW_MS = 45_000
DEFAULT_MAX_TALK_WINDOW_MS = 300_000
MAX_CONTEXT_BACKTRACK_MS = 120_000
# 同主题合并跳切（Ivan 2026-07-19）：同一 event_key 的分离窗口之间允许的
# 最大缝隙（与 talk_filler.MAX_MERGE_GAP_MS 对齐）；小于 MIN 的缝隙直接
# 吞进窗口，不值得跳切。
MAX_SAME_TOPIC_MERGE_GAP_MS = 600_000
MIN_SAME_TOPIC_MERGE_GAP_MS = 5_000
SEMANTIC_RECALL_STAGE = "semantic_recall"
SEMANTIC_RECALL_SINGLE_PASS_MAX_MS = 45 * 60 * 1000
SEMANTIC_RECALL_SHARD_DURATION_MS = 30 * 60 * 1000
SEMANTIC_RECALL_SHARD_OVERLAP_MS = 2 * 60 * 1000
SEMANTIC_RECALL_CANDIDATES_PER_SHARD = 4


def _slice_selection_metric() -> str:
    """Ivan's curated slice-selection metric (single authority asset).

    Mirrors the glossary loader pattern: repo asset first, then the free-host
    production copies; missing everywhere → empty string (prompt still builds
    with its structural rules, it just loses the preference calibration).
    """
    override = os.environ.get("AUTOSLICE_SLICE_METRIC") or os.environ.get(
        "LIDOUSHA_SLICE_METRIC"
    )
    candidates = [override] if override else [str(CHANNEL_PROFILE.asset_file("slice_selection_metric"))]
    if not override and CHANNEL_PROFILE.profile_id == "lidousha":
        candidates.extend(
            [
                "/opt/bilive/app/lidousha_slice_metric.md",
                "/app/lidousha_slice_metric.md",
            ]
        )
    for path in candidates:
        if not path:
            continue
        try:
            return Path(path).read_text(encoding="utf-8").strip()
        except OSError:
            continue
    return ""


# 共用评分卡量化规则段：既是整场召回 prompt 的一部分，也是狍哥案有界重
# 评分车道（``rescore_candidate_scorecard``）单候选提卡 prompt 的核心——
# 两处必须逐字同一份规则，不许各自维护一份漂移的拷贝（design §3.5「复用」）。
_SCORECARD_RUBRIC_BLOCK = """选片量化表（talk 必填，song 可省略）：
- tier 是硬层级 1/2/3；Tier 1 不是“出现了人名”就算，必须由至少两个本候选 cue 组成可核验的
  关系/CP/GL 立场链，或“观众起哄→她照做→人设反差”的完整互动链。把证据 cue 编号放入
  tier_evidence_cues；系统会拒绝候选窗外编号并确定性复算分数。
- tier_basis 只能按事实选择 relationship_chain / explicit_gl_stance / cp_positioning /
  audience_driven_performance / personal_stance / generic_event。
- dimensions 七项都打 0..4 整数：lidousha_centrality（李豆沙不可替代性）、stance_intensity、
  audience_salience（频道受众/人物题材显著度）、relationship_interaction、persona_reversal、
  comedic_payoff、self_contained。不要把 ASR/证据可靠性混进内容质量分。
- uncertainty_penalty 0..15，证据/指代/身份不确定才扣；fatigue_penalty 0..10，同场高度同质才扣。
- 系统按固定 25/20/15/15/10/10/5 权重计算 effective_score，confidence 不再代替内容价值。
"""


def build_semantic_recall_prompt(
    cues: Sequence[SourceCue],
    *,
    max_candidates: int,
    danmaku_hints: str | None = None,
    scope_note: str | None = None,
) -> str:
    lines = []
    for position, cue in enumerate(cues, start=1):
        text = " ".join(cue.text.split())
        lines.append(f"#{position} [{_mmss(cue.source_start_ms)}-{_mmss(cue.source_end_ms)}] {text}")
    transcript = "\n".join(lines)
    important_ip_block = detect_important_content_ips(title="", body=transcript).prompt_block
    danmaku_block = ""
    if danmaku_hints:
        danmaku_block = f"""
观众弹幕突发区(弹幕密度显著高于全场基线的时段,观众反应最强,大概率有值得切的内容;优先检查这些时段,但窗口边界仍要按字幕内容判断):
{danmaku_hints}
"""
    metric = _slice_selection_metric()
    metric_block = f"\n选题优先级 metric(Ivan 逐条校准过的权威,选题和排序都必须对照它;历史真例/反例都在里面):\n{metric}\n" if metric else ""
    scope_block = f"\n召回范围说明:{scope_note}\n" if scope_note else ""
    return f"""你是{CHANNEL_PROFILE.display_name}(B站虚拟主播)切片频道的选题编辑。下面是一场直播的完整字幕时间轴,每行格式是 #编号 [开始-结束] 文本。{danmaku_block}{metric_block}{scope_block}{important_ip_block}

你的任务:站在一个没看过这场直播的普通观众视角,从整场里选出最值得做成切片的片段(最多 {max_candidates} 个)。
“最多”是上限，不是必须凑满的数量。**同一场连续事件只能占一个候选**：话题中间即使有短暂停顿、
读别的弹幕、操作游戏或换了一个同类名字，只要后段仍在延续同一问题/分类/讨价还价，且后段笑点依赖
前段铺垫，就必须从首次触发点一直框到最后 payoff，绝不能拆成两条来满足数量。给同一事件稳定填写相同
event_key（简短中文，如“妈感姐妹分类”）；不同事件的 event_key 必须不同。
**同一个梗/称呼/话题隔几分钟被再次触发（比如新弹幕或 SC 又提起同一件事、主播回访之前的梗）也算
同一事件**：两段窗口都列出来，但用**同一个 event_key**——系统会自动把它们合并成一条带跳切的切片，
不要因为中间隔了别的内容就换 key（Ivan 2026-07-19 规定：主题一致尽量放在一个切片里）。

值得选的片段类型(语义判断,不要机械找关键词):
1. 讲故事/完整叙事:主播在讲一件事,有起因和结局。
2. 弹幕互动打闹:某条弹幕起了头,主播读出/复述弹幕后接梗、吐槽、破防。读弹幕/复述问题的那句就是上下文起点,必须包含在片段里。
3. 玩梗/绕口令/翻车/爆笑反应:包括对屏幕上正在看的图、玩的游戏的连续反应。
4. {CHANNEL_PROFILE.display_name}本人现场唱歌:kind 填 "song",范围大致覆盖整首歌即可(后续有专门的歌词边界+本人声纹硬门)。只播放原唱、片尾曲、待机/下播画面的音乐、游戏或视频背景音乐都不是歌切，禁止选为 song。

对每个候选片段必须做上下文检查(观众视角):
- 如果片段开头是悬空的(接续语、回应某个看不到的东西),往前找触发点(弹幕、话题开始、开始看某个东西的时刻),把 start_cue 前移到触发点,或填 context_trigger_cue。
- 如果观众能从片段内部合理推测出缺失的背景,可以接受,context_inferable 填 true。
- 如果上下文既不在片段里、也推测不出来、也找不到触发点,不要选这个片段。

谈话内部跳切提议(只提议,后续还会由确定性规则逐条验收):
- talk 候选可以填写 filler_removals,最多 3 段。只有删除后左右仍是同一话题、因果和指代都完整时才提议。
- mode="remove_cues": start_cue/end_cue 是可整段删除的首尾字幕(含);只用于礼物致谢 gift_thanks、
  短进场欢迎 welcome_chatter 或完全无关的插话 unrelated_aside。
- mode="gap_only": start_cue 是停顿左边保留的字幕,end_cue 是右边紧邻保留的字幕;只用于至少约 3 秒、
  没有承载画面反应或话题节奏的 dead_pause。
- 不得删除 SC/礼物所引发的实质回答,也不得删除起因、铺垫、指代来源、纠正、笑点、结论和收尾。
- bridge_coherent 只有在删除后左右两句可自然直连时才填 true;bridge 简述为什么语义可直连。
- 对每段语音删除还必须逐项检查 contains_setup/contains_cause/contains_answer/contains_punchline/
  contains_referent_intro/contains_correction/contains_resolution/later_dependency/interaction_relevant/
  meaning_or_stance_changed。只要有一项为 true 或不确定,就不要提议。
- 没有十分把握就返回空数组,让成品保持连续。confidence 是对“这段可安全删除”的信心。

{_SCORECARD_RUBRIC_BLOCK}
约束:
- talk 片段有效内容必须长于 45 秒且不超过 5 分钟;song 不限。
- 按有趣程度从高到低排序。confidence 是你对"路人观众会觉得有趣"的信心(0-1)。
- hook 用一句中文概括这个片段的看点。

只输出一个 JSON 对象,不要任何其他文字:
{{"candidates": [{{"start_cue": 整数, "end_cue": 整数, "kind": "talk"或"song", "event_key": "同一事件稳定键", "hook": "一句话看点", "context_trigger_cue": 整数或null, "context_inferable": true或false, "confidence": 0到1小数, "selection_scorecard": {{"tier": 1或2或3, "tier_basis": "上述枚举", "tier_reason": "准入理由", "tier_evidence_cues": [整数], "dimensions": {{"lidousha_centrality": 0到4整数, "stance_intensity": 0到4整数, "audience_salience": 0到4整数, "relationship_interaction": 0到4整数, "persona_reversal": 0到4整数, "comedic_payoff": 0到4整数, "self_contained": 0到4整数}}, "uncertainty_penalty": 0到15, "fatigue_penalty": 0到10}}, "filler_removals": [{{"mode": "remove_cues"或"gap_only", "start_cue": 整数, "end_cue": 整数, "reason": "gift_thanks"或"welcome_chatter"或"dead_pause"或"unrelated_aside", "topic_relation": "incidental"或"unrelated", "bridge_coherent": true或false, "bridge": "左右可直连的理由", "contains_setup": false, "contains_cause": false, "contains_answer": false, "contains_punchline": false, "contains_referent_intro": false, "contains_correction": false, "contains_resolution": false, "later_dependency": false, "interaction_relevant": false, "meaning_or_stance_changed": false, "confidence": 0到1小数}}]}}]}}

字幕时间轴:
{transcript}
"""


def _parse_filler_removals(
    item: dict[str, object], *, item_index: int, cue_count: int, kind: str
) -> list[dict[str, object]]:
    if kind != "talk" or not isinstance(item.get("filler_removals"), list):
        return []
    proposals: list[dict[str, object]] = []
    for removal_index, removal in enumerate(item["filler_removals"][:3]):
        if not isinstance(removal, dict):
            continue
        removal_start = _cue_position(removal.get("start_cue"), cue_count)
        removal_end = _cue_position(removal.get("end_cue"), cue_count)
        if (
            removal_start is None
            or removal_end is None
            or removal_start > removal_end
        ):
            continue
        confidence = removal.get("confidence")
        proposals.append(
            {
                "proposal_id": f"semantic_{item_index + 1}_{removal_index + 1}",
                "mode": str(removal.get("mode") or "remove_cues"),
                "start_cue": removal_start,
                "end_cue": removal_end,
                "reason": str(removal.get("reason") or ""),
                "bridge_coherent": removal.get("bridge_coherent") is True,
                "bridge": str(removal.get("bridge") or "").strip()[:240],
                "topic_relation": str(removal.get("topic_relation") or ""),
                "contains_setup": removal.get("contains_setup"),
                "contains_cause": removal.get("contains_cause"),
                "contains_answer": removal.get("contains_answer"),
                "contains_punchline": removal.get("contains_punchline"),
                "contains_referent_intro": removal.get("contains_referent_intro"),
                "contains_correction": removal.get("contains_correction"),
                "contains_resolution": removal.get("contains_resolution"),
                "later_dependency": removal.get("later_dependency"),
                "interaction_relevant": removal.get("interaction_relevant"),
                "meaning_or_stance_changed": removal.get(
                    "meaning_or_stance_changed"
                ),
                "confidence": (
                    min(1.0, max(0.0, float(confidence)))
                    if isinstance(confidence, (int, float))
                    and not isinstance(confidence, bool)
                    else 0.0
                ),
            }
        )
    return proposals


def select_semantic_session_candidates(
    cues: Sequence[SourceCue],
    *,
    llm_call: LlmCall,
    max_candidates: int = 3,
    min_talk_window_ms: int = DEFAULT_MIN_TALK_WINDOW_MS,
    max_talk_window_ms: int = DEFAULT_MAX_TALK_WINDOW_MS,
    danmaku_hints: str | None = None,
    scope_note: str | None = None,
) -> tuple[list[FullSessionCandidate], dict[str, object]]:
    """Semantic recall over the full session; returns (candidates, diagnostics).

    Raises LlmCallError on transport/parse failure — the caller decides how to
    fall back (the runner drops to the keyword lanes so zero-output never
    silently happens because the LLM was down).
    """

    ordered = sorted(cues, key=lambda cue: (cue.source_start_ms, cue.source_end_ms, cue.cue_id))
    if not ordered:
        return [], {"skipped": [], "raw_candidates": 0, "error": "no cues"}
    prompt = build_semantic_recall_prompt(
        ordered, max_candidates=max_candidates, danmaku_hints=danmaku_hints,
        scope_note=scope_note,
    )
    completion = llm_call(prompt)
    payload = extract_json_object(completion)
    raw_candidates = payload.get("candidates")
    if not isinstance(raw_candidates, list):
        raise LlmCallError("semantic recall completion has no candidates array")

    skipped: list[dict[str, object]] = []
    parsed: list[tuple[float, dict[str, object]]] = []
    for item_index, item in enumerate(raw_candidates):
        if not isinstance(item, dict):
            skipped.append({"item": item_index, "reason": "not_an_object"})
            continue
        start_cue = _cue_position(item.get("start_cue"), len(ordered))
        end_cue = _cue_position(item.get("end_cue"), len(ordered))
        if start_cue is None or end_cue is None or start_cue > end_cue:
            skipped.append(
                {
                    "item": item_index,
                    "reason": "cue_range_invalid",
                    "raw_start_cue": repr(item.get("start_cue")),
                    "raw_end_cue": repr(item.get("end_cue")),
                }
            )
            continue
        kind = str(item.get("kind") or "talk")
        if kind not in ("talk", "song"):
            kind = "talk"
        trigger_cue = _cue_position(item.get("context_trigger_cue"), len(ordered))
        if trigger_cue is not None and trigger_cue < start_cue:
            # Context trigger before the window: pull the start back to it, but
            # never further than the backtrack cap — a "trigger" 10 minutes
            # earlier is a hallucination, not a setup line.
            backtrack_limit_ms = ordered[start_cue - 1].source_start_ms - MAX_CONTEXT_BACKTRACK_MS
            if ordered[trigger_cue - 1].source_start_ms >= backtrack_limit_ms:
                start_cue = trigger_cue
        confidence = item.get("confidence")
        confidence_value = float(confidence) if isinstance(confidence, (int, float)) and not isinstance(confidence, bool) else 0.5
        selection_scorecard = (
            normalize_selection_scorecard(
                item.get("selection_scorecard"),
                start_cue=start_cue,
                end_cue=end_cue,
            )
            if kind == "talk"
            else None
        )
        raw_event_key = str(item.get("event_key") or "").strip()
        event_key = " ".join(raw_event_key.split())[:80]
        filler_removals = _parse_filler_removals(
            item, item_index=item_index, cue_count=len(ordered), kind=kind
        )
        parsed.append(
            (
                min(1.0, max(0.0, confidence_value)),
                {
                    "start_cue": start_cue,
                    "end_cue": end_cue,
                    "kind": kind,
                    "event_key": event_key,
                    "hook": str(item.get("hook") or ""),
                    "context_inferable": bool(item.get("context_inferable", True)),
                    "selection_scorecard": selection_scorecard,
                    "filler_removals": filler_removals,
                },
            )
        )

    # Models occasionally return two non-overlapping windows for one event even
    # after being told not to.  event_key makes the invariant deterministic:
    # merge before confidence ranking/quota, so one continuous story can never
    # consume two top-N slots.  A missing key stays unique for compatibility
    # with older/fallback completions.
    grouped: dict[tuple[str, str], list[tuple[float, dict[str, object]]]] = {}
    unkeyed = 0
    for confidence_value, spec in parsed:
        event_key = str(spec.get("event_key") or "")
        if not event_key:
            unkeyed += 1
            event_key = f"__unkeyed_{unkeyed}"
        grouped.setdefault((str(spec["kind"]), event_key.casefold()), []).append(
            (confidence_value, spec)
        )
    merged_parsed: list[tuple[float, dict[str, object]]] = []
    merged_events: list[dict[str, object]] = []
    for (kind_key, _event_key), rows in grouped.items():
        if len(rows) == 1:
            merged_parsed.append(rows[0])
            continue
        winner_confidence, winner_spec = min(
            rows,
            key=lambda row: selection_rank_key(
                {
                    "candidate_id": str(row[1].get("event_key") or ""),
                    "confidence": row[0],
                    "selection_scorecard": row[1].get("selection_scorecard"),
                }
            ),
        )
        merged_spec = dict(winner_spec)
        input_ranges = [
            [int(row[1]["start_cue"]), int(row[1]["end_cue"])] for row in rows
        ]
        event_audit: dict[str, object] = {
            "event_key": merged_spec["event_key"],
            "input_ranges": input_ranges,
        }
        # 归一化重叠/相邻区间（cue 位置）。
        normalized: list[list[int]] = []
        for start_cue, end_cue in sorted(
            (int(row[1]["start_cue"]), int(row[1]["end_cue"])) for row in rows
        ):
            if normalized and start_cue <= normalized[-1][1] + 1:
                normalized[-1][1] = max(normalized[-1][1], end_cue)
            else:
                normalized.append([start_cue, end_cue])
        # talk 的分离同主题段走 merge_gap 跳切合并（Ivan 2026-07-19「主题一致
        # 尽量放在一个切片里」；7/18 kmx 称呼两条切片案）。gap 超上限时不
        # 盲扫中间内容，退回 winner 单段并留审计——旧的 min..max sweep 会把
        # 8 分钟无关内容框进窗口，被时长门整条毙掉，两段全丢。
        merge_gaps: list[dict[str, object]] = []
        gap_too_large = False
        for left, right in zip(normalized, normalized[1:]):
            gap_start_ms = ordered[left[1] - 1].source_end_ms
            gap_end_ms = ordered[right[0] - 1].source_start_ms
            gap_ms = gap_end_ms - gap_start_ms
            if gap_ms > MAX_SAME_TOPIC_MERGE_GAP_MS:
                gap_too_large = True
                break
            if gap_ms >= MIN_SAME_TOPIC_MERGE_GAP_MS:
                merge_gaps.append(
                    {
                        "start_ms": gap_start_ms,
                        "end_ms": gap_end_ms,
                        "event_key": str(merged_spec.get("event_key") or ""),
                    }
                )
        if kind_key == "talk" and gap_too_large:
            merged_spec = dict(winner_spec)
            event_audit["merge_outcome"] = "kept_winner_gap_too_large"
            event_audit["merged_range"] = [
                int(merged_spec["start_cue"]),
                int(merged_spec["end_cue"]),
            ]
            merged_parsed.append((winner_confidence, merged_spec))
            merged_events.append(event_audit)
            continue
        merged_spec["start_cue"] = normalized[0][0]
        merged_spec["end_cue"] = normalized[-1][1]
        if kind_key == "talk" and merge_gaps:
            merged_spec["merge_gaps"] = merge_gaps
            event_audit["merge_gaps_ms"] = [
                [int(gap["start_ms"]), int(gap["end_ms"])] for gap in merge_gaps
            ]
        merged_spec["filler_removals"] = [
            removal
            for row in rows
            for removal in row[1].get("filler_removals", [])
            if isinstance(removal, dict)
        ][:3]
        event_audit["merge_outcome"] = "merged"
        event_audit["merged_range"] = [
            int(merged_spec["start_cue"]),
            int(merged_spec["end_cue"]),
        ]
        merged_parsed.append((winner_confidence, merged_spec))
        merged_events.append(event_audit)
    parsed = merged_parsed
    parsed.sort(
        key=lambda entry: selection_rank_key(
            {
                "candidate_id": str(entry[1].get("event_key") or ""),
                "confidence": entry[0],
                "selection_scorecard": entry[1].get("selection_scorecard"),
            }
        )
    )
    selected: list[FullSessionCandidate] = []
    hooks: dict[str, str] = {}
    scorecards: dict[str, dict[str, object]] = {}
    filler_proposals: dict[str, list[dict[str, object]]] = {}
    merge_gap_plans: dict[str, list[dict[str, object]]] = {}
    for confidence_value, spec in parsed:
        if len(selected) >= max_candidates:
            break
        window = tuple(ordered[spec["start_cue"] - 1 : spec["end_cue"]])
        start_ms = window[0].source_start_ms
        end_ms = window[-1].source_end_ms
        duration_ms = end_ms - start_ms
        # 合并候选按有效时长（扣掉 merge_gap 缝隙）过时长门，否则跨缝
        # 合并的窗口必超 5 分钟上限、被整条毙掉。
        spec_merge_gaps = [
            gap for gap in (spec.get("merge_gaps") or []) if isinstance(gap, dict)
        ]
        gap_total_ms = sum(
            max(0, int(gap["end_ms"]) - int(gap["start_ms"])) for gap in spec_merge_gaps
        )
        effective_duration_ms = duration_ms - gap_total_ms
        if spec["kind"] == "talk" and not (
            min_talk_window_ms < effective_duration_ms <= max_talk_window_ms
        ):
            skipped.append({"start_cue": spec["start_cue"], "end_cue": spec["end_cue"], "reason": "talk_duration_out_of_range", "duration_ms": duration_ms, "effective_duration_ms": effective_duration_ms})
            continue
        anchor = AnchorCandidate(
            candidate_id=f"semantic{spec['kind']}_{start_ms}_{end_ms}",
            anchor_start_ms=start_ms,
            anchor_end_ms=end_ms,
        )
        if spec["kind"] == "song":
            boundary = _song_anchor_boundary(anchor)
        else:
            boundary = BoundaryResolution(
                candidate_id=anchor.candidate_id,
                action=DecisionAction.AUTO_RECUT,
                resolved_start_ms=start_ms,
                resolved_end_ms=end_ms,
                start_boundary_score=confidence_value,
                end_boundary_score=confidence_value,
                reason_codes=("SEMANTIC_RECALL",),
                next_start_ms=start_ms,
                next_end_ms=end_ms,
            )
        candidate = FullSessionCandidate(
            anchor=anchor,
            boundary=boundary,
            cues=window,
            text_preview=_join_text(window)[:160],
            content_type_hint=spec["kind"],
        )
        if _overlaps_selected(candidate, selected):
            skipped.append({"candidate_id": anchor.candidate_id, "reason": "overlaps_selected"})
            continue
        selected.append(candidate)
        hooks[anchor.candidate_id] = spec["hook"]
        scorecard = spec.get("selection_scorecard")
        if isinstance(scorecard, dict):
            # 狍哥案修复（2026-08-07 design §3.5）：start_cue/end_cue 此前只
            # 活在这个函数的局部 spec 里，重评分车道需要它们在 pick 行上
            # 持久化才能重建 cue 窗；缺失时下游走 tier_evidence_cues
            # min/max 兜底（与 apply_reviewed_selection_calibration 同款）。
            scorecards[anchor.candidate_id] = {
                **scorecard,
                "start_cue": int(spec["start_cue"]),
                "end_cue": int(spec["end_cue"]),
            }
        if spec_merge_gaps:
            merge_gap_plans[anchor.candidate_id] = [dict(gap) for gap in spec_merge_gaps]
        raw_filler_proposals = spec.get("filler_removals")
        if isinstance(raw_filler_proposals, list) and raw_filler_proposals:
            filler_proposals[anchor.candidate_id] = [
                dict(row)
                for row in raw_filler_proposals
                if isinstance(row, dict)
            ][:3]

    diagnostics = {
        "stage": SEMANTIC_RECALL_STAGE,
        "raw_candidates": len(raw_candidates),
        "raw_response_candidates": raw_candidates[:10],
        "merged_events": merged_events,
        "selected": [candidate.anchor.candidate_id for candidate in selected],
        "hooks": hooks,
        "scorecards": scorecards,
        "filler_proposals": filler_proposals,
        "merge_gaps": merge_gap_plans,
        "skipped": skipped,
    }
    return selected, diagnostics


def plan_semantic_recall_shards(
    cues: Sequence[SourceCue],
    *,
    single_pass_max_ms: int = SEMANTIC_RECALL_SINGLE_PASS_MAX_MS,
    shard_duration_ms: int = SEMANTIC_RECALL_SHARD_DURATION_MS,
    overlap_ms: int = SEMANTIC_RECALL_SHARD_OVERLAP_MS,
) -> list[dict[str, object]]:
    """Plan bounded semantic-recall windows over an absolute source timeline.

    One huge two-hour prompt under-recalled the 2026-07-22 stream: it returned
    two events and even missed three already-known strong events in the first
    half hour.  Long sessions therefore get independent 30-minute recall
    opportunities.  Two minutes of overlap preserves triggers and payoffs at
    a boundary; the global pass below removes overlapping duplicates.
    """

    ordered = sorted(
        cues,
        key=lambda cue: (
            cue.source_start_ms,
            cue.source_end_ms,
            cue.cue_id,
        ),
    )
    if not ordered:
        return []
    coverage_end_ms = max(cue.source_end_ms for cue in ordered)
    if coverage_end_ms <= single_pass_max_ms:
        return [
            {
                "index": 0,
                "core_start_ms": 0,
                "core_end_ms": coverage_end_ms,
                "window_start_ms": 0,
                "window_end_ms": coverage_end_ms,
                "cues": tuple(ordered),
            }
        ]

    shards: list[dict[str, object]] = []
    core_start_ms = 0
    while core_start_ms < coverage_end_ms:
        core_end_ms = min(coverage_end_ms, core_start_ms + shard_duration_ms)
        window_start_ms = max(0, core_start_ms - overlap_ms)
        window_end_ms = min(coverage_end_ms, core_end_ms + overlap_ms)
        shard_cues = tuple(
            cue
            for cue in ordered
            if cue.source_end_ms > window_start_ms
            and cue.source_start_ms < window_end_ms
        )
        if shard_cues:
            shards.append(
                {
                    "index": len(shards),
                    "core_start_ms": core_start_ms,
                    "core_end_ms": core_end_ms,
                    "window_start_ms": window_start_ms,
                    "window_end_ms": window_end_ms,
                    "cues": shard_cues,
                }
            )
        core_start_ms = core_end_ms
    return shards


def select_semantic_session_candidates_covered(
    cues: Sequence[SourceCue],
    *,
    llm_call: LlmCall,
    max_candidates: int,
    min_talk_window_ms: int = DEFAULT_MIN_TALK_WINDOW_MS,
    max_talk_window_ms: int = DEFAULT_MAX_TALK_WINDOW_MS,
    danmaku_hints: str | None = None,
) -> tuple[list[FullSessionCandidate], dict[str, object]]:
    """Recall a whole session without letting long timelines starve later time.

    Short sessions retain the exact single-call behavior.  Long sessions are
    recalled independently per overlapping window, then confidence-ranked and
    overlap-deduplicated under one global candidate-pool cap.  Any shard call
    failure still raises ``LlmCallError`` so the existing deterministic
    fallback remains authoritative instead of silently accepting partial
    semantic coverage.
    """

    shards = plan_semantic_recall_shards(cues)
    if not shards:
        return [], {
            "stage": SEMANTIC_RECALL_STAGE,
            "mode": "empty",
            "coverage_end_ms": 0,
            "shards": [],
            "selected": [],
            "hooks": {},
            "scorecards": {},
            "filler_proposals": {},
            "merge_gaps": {},
            "skipped": [],
        }
    coverage_end_ms = max(cue.source_end_ms for cue in cues)
    if len(shards) == 1:
        selected, diagnostics = select_semantic_session_candidates(
            tuple(shards[0]["cues"]),
            llm_call=llm_call,
            max_candidates=max_candidates,
            min_talk_window_ms=min_talk_window_ms,
            max_talk_window_ms=max_talk_window_ms,
            danmaku_hints=danmaku_hints,
        )
        diagnostics = dict(diagnostics)
        diagnostics.update(
            {
                "mode": "single",
                "coverage_end_ms": coverage_end_ms,
                "shards": [
                    {
                        key: shards[0][key]
                        for key in (
                            "index",
                            "core_start_ms",
                            "core_end_ms",
                            "window_start_ms",
                            "window_end_ms",
                        )
                    }
                ],
            }
        )
        return selected, diagnostics

    recalled: list[FullSessionCandidate] = []
    hooks: dict[str, str] = {}
    scorecards: dict[str, dict[str, object]] = {}
    filler_proposals: dict[str, list[dict[str, object]]] = {}
    merge_gaps: dict[str, list[dict[str, object]]] = {}
    shard_diagnostics: list[dict[str, object]] = []
    skipped: list[dict[str, object]] = []
    for shard in shards:
        core_start_ms = int(shard["core_start_ms"])
        core_end_ms = int(shard["core_end_ms"])
        window_start_ms = int(shard["window_start_ms"])
        window_end_ms = int(shard["window_end_ms"])
        scope_note = (
            "这是整场直播的一个覆盖窗,核心范围 "
            f"{_mmss(core_start_ms)}-{_mmss(core_end_ms)},为补齐上下文实际提供 "
            f"{_mmss(window_start_ms)}-{_mmss(window_end_ms)}。优先选择事件中心落在核心范围内的内容;"
            "两侧重叠只用于保住触发点和收尾,不要把重叠内容当成额外配额。"
        )
        shard_selected, diagnostics = select_semantic_session_candidates(
            tuple(shard["cues"]),
            llm_call=llm_call,
            max_candidates=min(
                max_candidates,
                SEMANTIC_RECALL_CANDIDATES_PER_SHARD,
            ),
            min_talk_window_ms=min_talk_window_ms,
            max_talk_window_ms=max_talk_window_ms,
            danmaku_hints=danmaku_hints,
            scope_note=scope_note,
        )
        recalled.extend(shard_selected)
        hooks.update(
            {
                str(key): str(value)
                for key, value in (diagnostics.get("hooks") or {}).items()
            }
        )
        scorecards.update(
            {
                str(key): dict(value)
                for key, value in (diagnostics.get("scorecards") or {}).items()
                if isinstance(value, dict)
            }
        )
        filler_proposals.update(
            {
                str(key): list(value)
                for key, value in (diagnostics.get("filler_proposals") or {}).items()
            }
        )
        merge_gaps.update(
            {
                str(key): list(value)
                for key, value in (diagnostics.get("merge_gaps") or {}).items()
            }
        )
        shard_diagnostics.append(
            {
                "index": int(shard["index"]),
                "core_start_ms": core_start_ms,
                "core_end_ms": core_end_ms,
                "window_start_ms": window_start_ms,
                "window_end_ms": window_end_ms,
                "cue_count": len(shard["cues"]),
                "raw_candidates": int(diagnostics.get("raw_candidates") or 0),
                "selected": list(diagnostics.get("selected") or []),
                "skipped": list(diagnostics.get("skipped") or []),
            }
        )

    recalled.sort(
        key=lambda candidate: selection_rank_key(
            {
                "candidate_id": candidate.anchor.candidate_id,
                "confidence": float(
                    getattr(candidate.boundary, "start_boundary_score", 0.0)
                    or 0.0
                ),
                "selection_scorecard": scorecards.get(
                    candidate.anchor.candidate_id
                ),
            }
        )
    )
    selected: list[FullSessionCandidate] = []
    for candidate in recalled:
        if len(selected) >= max_candidates:
            break
        if _overlaps_selected(candidate, selected):
            skipped.append(
                {
                    "candidate_id": candidate.anchor.candidate_id,
                    "reason": "overlaps_cross_shard_selected",
                }
            )
            continue
        selected.append(candidate)

    selected_ids = {candidate.anchor.candidate_id for candidate in selected}
    return selected, {
        "stage": SEMANTIC_RECALL_STAGE,
        "mode": "sharded",
        "coverage_end_ms": coverage_end_ms,
        "shards": shard_diagnostics,
        "raw_candidates": sum(
            int(shard.get("raw_candidates") or 0) for shard in shard_diagnostics
        ),
        "selected": [candidate.anchor.candidate_id for candidate in selected],
        "hooks": {key: value for key, value in hooks.items() if key in selected_ids},
        "scorecards": {
            key: value for key, value in scorecards.items() if key in selected_ids
        },
        "filler_proposals": {
            key: value for key, value in filler_proposals.items() if key in selected_ids
        },
        "merge_gaps": {
            key: value for key, value in merge_gaps.items() if key in selected_ids
        },
        "skipped": skipped,
    }


RESCORE_SCORECARD_SCHEMA = "source-fact-rescore-scorecard.v1"


def _rescore_prompt(
    *,
    cues: Sequence[SourceCue],
    repaired_hook: str,
    clip_context_prompt: str,
) -> str:
    lines = []
    for position, cue in enumerate(cues, start=1):
        text = " ".join(cue.text.split())
        lines.append(f"#{position} [{_mmss(cue.source_start_ms)}-{_mmss(cue.source_end_ms)}] {text}")
    transcript = "\n".join(lines)
    return f"""你是{CHANNEL_PROFILE.display_name}(B站虚拟主播)切片频道的选题编辑。source-fact 事实修正已经把这条候选的
selection_hook 改写为下面的修正稿,旧评分卡因此被判 INCOMPATIBLE 作废。只针对这条**修正后的
hook**重新打一张评分卡,不要重新判断候选边界或选题范围,也不要评价修正是否合理(那是上一步已经
做完的事)。如果修正后的 hook 在这段字幕窗口里找不到任何证据支持,status 填 "UNSUPPORTED",
selection_scorecard 可留空对象。

修正后的 hook:
{repaired_hook}

字幕窗口(cue 编号连续,#编号 [开始-结束] 文本):
{transcript}

同片 hash-bound 上下文(含按时间排列的结构化弹幕/SC):
{clip_context_prompt}

{_SCORECARD_RUBRIC_BLOCK}
只输出一个 JSON 对象,不要任何其他文字:
{{"status": "SUPPORTED"或"UNSUPPORTED", "selection_scorecard": {{"tier": 1或2或3, "tier_basis": "上述枚举", "tier_reason": "准入理由", "tier_evidence_cues": [整数], "dimensions": {{"lidousha_centrality": 0到4整数, "stance_intensity": 0到4整数, "audience_salience": 0到4整数, "relationship_interaction": 0到4整数, "persona_reversal": 0到4整数, "comedic_payoff": 0到4整数, "self_contained": 0到4整数}}, "uncertainty_penalty": 0到15, "fatigue_penalty": 0到10}}}}
"""


def rescore_candidate_scorecard(
    *,
    candidate_id: str,
    cues: Sequence[SourceCue],
    repaired_hook: str,
    clip_context_prompt: str,
    llm_call: LlmCall | None,
    stale_scorecard: object = None,
    start_cue: int | None = None,
    end_cue: int | None = None,
) -> dict[str, object]:
    """Regenerate one candidate's scorecard against its repaired hook.

    Bounded, single-candidate counterpart to ``select_semantic_session_
    candidates``: reuses the same deterministic scorecard arithmetic
    (``normalize_selection_scorecard``, ``apply_reviewed_selection_
    calibration``, ``selection_scorecard_is_valid``) instead of re-deriving
    it, so a rescored candidate is ranked by the exact same rules as every
    other candidate (design §4, 2026-08-07 狍哥案修复).

    Provider failures return ``PROVIDER_UNAVAILABLE`` — the caller must not
    treat that as a content rejection or consume any bounded retry budget.
    """

    ordered = sorted(
        cues, key=lambda cue: (cue.source_start_ms, cue.source_end_ms, cue.cue_id)
    )
    cue_count = len(ordered)
    base: dict[str, object] = {
        "schema_version": RESCORE_SCORECARD_SCHEMA,
        "candidate_id": candidate_id,
    }
    if cue_count == 0:
        return {**base, "outcome": "UNSUPPORTED", "reason_code": "RESCORE_NO_CUES"}
    resolved_start = (
        start_cue if isinstance(start_cue, int) and 1 <= start_cue <= cue_count else None
    )
    resolved_end = (
        end_cue if isinstance(end_cue, int) and 1 <= end_cue <= cue_count else None
    )
    if resolved_start is None or resolved_end is None or resolved_start > resolved_end:
        # 兜底：从旧卡的 tier_evidence_cues 取 min/max（与
        # apply_reviewed_selection_calibration 同款兜底策略），仍取不到就
        # 退化为整窗——不能因为窗口未知就直接判死这条候选。
        evidence = (
            [
                value
                for value in (stale_scorecard.get("tier_evidence_cues") or [])
                if isinstance(value, int)
                and not isinstance(value, bool)
                and 1 <= value <= cue_count
            ]
            if isinstance(stale_scorecard, Mapping)
            else []
        )
        resolved_start, resolved_end = (
            (min(evidence), max(evidence)) if evidence else (1, cue_count)
        )
    if llm_call is None:
        return {
            **base,
            "outcome": "PROVIDER_UNAVAILABLE",
            "reason_code": "RESCORE_PROVIDER_UNAVAILABLE",
        }
    prompt = _rescore_prompt(
        cues=ordered, repaired_hook=repaired_hook, clip_context_prompt=clip_context_prompt
    )
    try:
        raw = llm_call(prompt)
        payload = extract_json_object(raw)
    except (LlmCallError, Exception):  # noqa: BLE001
        return {
            **base,
            "outcome": "PROVIDER_UNAVAILABLE",
            "reason_code": "RESCORE_PROVIDER_CALL_FAILED",
        }
    if payload.get("status") == "UNSUPPORTED":
        return {**base, "outcome": "UNSUPPORTED", "reason_code": "RESCORE_HOOK_UNSUPPORTED"}
    normalized = normalize_selection_scorecard(
        payload.get("selection_scorecard"),
        start_cue=resolved_start,
        end_cue=resolved_end,
    )
    if normalized is None:
        return {
            **base,
            "outcome": "INVALID_CARD",
            "reason_code": "RESCORE_SCORECARD_SHAPE_INVALID",
        }
    try:
        calibrated = apply_reviewed_selection_calibration(candidate_id, normalized)
    except SelectionCalibrationPolicyError:
        return {
            **base,
            "outcome": "INVALID_CARD",
            "reason_code": "RESCORE_CALIBRATION_REJECTED",
        }
    if not selection_scorecard_is_valid(calibrated):
        return {
            **base,
            "outcome": "INVALID_CARD",
            "reason_code": "RESCORE_SCORECARD_INVALID_AFTER_CALIBRATION",
        }
    assert isinstance(calibrated, dict)
    return {
        **base,
        "outcome": "RESCORED",
        "selection_scorecard": calibrated,
        "start_cue": resolved_start,
        "end_cue": resolved_end,
    }


def _cue_position(value: object, cue_count: int) -> int | None:
    """Coerce an LLM-emitted cue reference: models emit `12`, `12.0`, `"12"`,
    or `"#12"` interchangeably — rejecting those wholesale silently empties
    the whole recall lane."""

    if isinstance(value, bool):
        return None
    if isinstance(value, float):
        if not value.is_integer():
            return None
        value = int(value)
    elif isinstance(value, str):
        stripped = value.strip().lstrip("#")
        if not stripped.isdigit():
            return None
        value = int(stripped)
    if not isinstance(value, int):
        return None
    if 1 <= value <= cue_count:
        return value
    return None


def _mmss(ms: int) -> str:
    seconds = max(0, ms) // 1000
    minutes, sec = divmod(seconds, 60)
    return f"{minutes:02d}:{sec:02d}"
