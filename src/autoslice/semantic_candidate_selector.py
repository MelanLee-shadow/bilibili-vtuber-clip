"""LLM semantic recall selector — viewer-perspective candidate discovery.

The keyword selectors structurally under-recall Li Dousha's actual humor: her
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

from typing import Sequence

from src.autoslice.auto_review import DecisionAction
from src.autoslice.boundary_resolver import AnchorCandidate, BoundaryResolution
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

DEFAULT_MIN_TALK_WINDOW_MS = 12_000
DEFAULT_MAX_TALK_WINDOW_MS = 300_000
MAX_CONTEXT_BACKTRACK_MS = 120_000
SEMANTIC_RECALL_STAGE = "semantic_recall"


def _slice_selection_metric() -> str:
    """Ivan's curated slice-selection metric (single authority asset).

    Mirrors the glossary loader pattern: repo asset first, then the free-host
    production copies; missing everywhere → empty string (prompt still builds
    with its structural rules, it just loses the preference calibration).
    """
    import os
    from pathlib import Path

    override = os.environ.get("LIDOUSHA_SLICE_METRIC")
    candidates = (
        [override]
        if override
        else [
            str(Path(__file__).resolve().parents[2] / "assets" / "lidousha" / "slice_selection_metric.md"),
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


def build_semantic_recall_prompt(
    cues: Sequence[SourceCue],
    *,
    max_candidates: int,
    danmaku_hints: str | None = None,
) -> str:
    lines = []
    for position, cue in enumerate(cues, start=1):
        text = " ".join(cue.text.split())
        lines.append(f"#{position} [{_mmss(cue.source_start_ms)}-{_mmss(cue.source_end_ms)}] {text}")
    transcript = "\n".join(lines)
    danmaku_block = ""
    if danmaku_hints:
        danmaku_block = f"""
观众弹幕突发区(弹幕密度显著高于全场基线的时段,观众反应最强,大概率有值得切的内容;优先检查这些时段,但窗口边界仍要按字幕内容判断):
{danmaku_hints}
"""
    metric = _slice_selection_metric()
    metric_block = f"\n选题优先级 metric(Ivan 逐条校准过的权威,选题和排序都必须对照它;历史真例/反例都在里面):\n{metric}\n" if metric else ""
    return f"""你是李豆沙(B站虚拟主播)切片频道的选题编辑。下面是一场直播的完整字幕时间轴,每行格式是 #编号 [开始-结束] 文本。{danmaku_block}{metric_block}

你的任务:站在一个没看过这场直播的普通观众视角,从整场里选出最值得做成切片的片段(最多 {max_candidates} 个)。

值得选的片段类型(语义判断,不要机械找关键词):
1. 讲故事/完整叙事:主播在讲一件事,有起因和结局。
2. 弹幕互动打闹:某条弹幕起了头,主播读出/复述弹幕后接梗、吐槽、破防。读弹幕/复述问题的那句就是上下文起点,必须包含在片段里。
3. 玩梗/绕口令/翻车/爆笑反应:包括对屏幕上正在看的图、玩的游戏的连续反应。
4. 李豆沙本人现场唱歌:kind 填 "song",范围大致覆盖整首歌即可(后续有专门的歌词边界+本人声纹硬门)。只播放原唱、片尾曲、待机/下播画面的音乐、游戏或视频背景音乐都不是歌切，禁止选为 song。

对每个候选片段必须做上下文检查(观众视角):
- 如果片段开头是悬空的(接续语、回应某个看不到的东西),往前找触发点(弹幕、话题开始、开始看某个东西的时刻),把 start_cue 前移到触发点,或填 context_trigger_cue。
- 如果观众能从片段内部合理推测出缺失的背景,可以接受,context_inferable 填 true。
- 如果上下文既不在片段里、也推测不出来、也找不到触发点,不要选这个片段。

约束:
- talk 片段时长 15 秒到 4 分钟之间;song 不限。
- 按有趣程度从高到低排序。confidence 是你对"路人观众会觉得有趣"的信心(0-1)。
- hook 用一句中文概括这个片段的看点。

只输出一个 JSON 对象,不要任何其他文字:
{{"candidates": [{{"start_cue": 整数, "end_cue": 整数, "kind": "talk"或"song", "hook": "一句话看点", "context_trigger_cue": 整数或null, "context_inferable": true或false, "confidence": 0到1小数}}]}}

字幕时间轴:
{transcript}
"""


def select_semantic_session_candidates(
    cues: Sequence[SourceCue],
    *,
    llm_call: LlmCall,
    max_candidates: int = 3,
    min_talk_window_ms: int = DEFAULT_MIN_TALK_WINDOW_MS,
    max_talk_window_ms: int = DEFAULT_MAX_TALK_WINDOW_MS,
    danmaku_hints: str | None = None,
) -> tuple[list[FullSessionCandidate], dict[str, object]]:
    """Semantic recall over the full session; returns (candidates, diagnostics).

    Raises LlmCallError on transport/parse failure — the caller decides how to
    fall back (the runner drops to the keyword lanes so zero-output never
    silently happens because the LLM was down).
    """

    ordered = sorted(cues, key=lambda cue: (cue.source_start_ms, cue.source_end_ms, cue.cue_id))
    if not ordered:
        return [], {"skipped": [], "raw_candidates": 0, "error": "no cues"}
    completion = llm_call(
        build_semantic_recall_prompt(ordered, max_candidates=max_candidates, danmaku_hints=danmaku_hints)
    )
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
        parsed.append(
            (
                min(1.0, max(0.0, confidence_value)),
                {
                    "start_cue": start_cue,
                    "end_cue": end_cue,
                    "kind": kind,
                    "hook": str(item.get("hook") or ""),
                    "context_inferable": bool(item.get("context_inferable", True)),
                },
            )
        )

    parsed.sort(key=lambda entry: entry[0], reverse=True)
    selected: list[FullSessionCandidate] = []
    hooks: dict[str, str] = {}
    for confidence_value, spec in parsed:
        if len(selected) >= max_candidates:
            break
        window = tuple(ordered[spec["start_cue"] - 1 : spec["end_cue"]])
        start_ms = window[0].source_start_ms
        end_ms = window[-1].source_end_ms
        duration_ms = end_ms - start_ms
        if spec["kind"] == "talk" and not (min_talk_window_ms <= duration_ms <= max_talk_window_ms):
            skipped.append({"start_cue": spec["start_cue"], "end_cue": spec["end_cue"], "reason": "talk_duration_out_of_range", "duration_ms": duration_ms})
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

    diagnostics = {
        "stage": SEMANTIC_RECALL_STAGE,
        "raw_candidates": len(raw_candidates),
        "raw_response_candidates": raw_candidates[:10],
        "selected": [candidate.anchor.candidate_id for candidate in selected],
        "hooks": hooks,
        "skipped": skipped,
    }
    return selected, diagnostics


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
