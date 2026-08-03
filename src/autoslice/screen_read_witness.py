"""Screen-read witness: recover fast-spoken lines from on-screen text.

维护者（424_522 BV1d73P6iErL 1:24 案）：她快速念屏幕上的
「战斗回合用尽，即将离开战场」，音频糊但**看一眼画面就知道全文**。

两个设计裁定（他亲自点的）：
- 何时看画面：不盲扫——声学证人自己承认失败（confidence 低 / 不确定位
  密集）时才升级视觉，一次 finding 至多采样两帧，成本只花在耳朵糊掉处。
- 如何找到她念的词：不定位屏幕区域——全帧 OCR 出文本池，用**拼音对齐**
  挑出与听写最相似的一段（她念的就是池里读音最像的那条），需边际优势，
  平票不选。

命中的屏幕文本以 ``verified_ocr`` 出处进入既有裁决引擎（正字法权威白名单
原生认这个 kind），judge 照常闭集裁定——视觉只供**候选与出处**，永不直改。
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from difflib import SequenceMatcher
from typing import Any

SCREEN_TEXT_QUESTION = (
    "Transcribe EVERY piece of legible on-screen text in this frame "
    "(game UI, dialog boxes, system messages, buttons, subtitles burned "
    "into the game — NOT the streamer webcam overlay). Verbatim, keep the "
    "original language, no translation, no guessing at blurry text. "
    'Reply with STRICT JSON only: {"texts": ["...", "..."]} — one entry '
    "per visually distinct text region, longest regions first."
)

_MIN_MATCH_RATIO = 0.55
_MIN_MATCH_MARGIN = 0.10
_MIN_TEXT_CHARS = 4
WEAK_WITNESS_CONFIDENCE = 0.75
WEAK_WITNESS_UNCERTAIN_FRACTION = 1 / 3


def witness_is_weak(witness: Mapping[str, Any]) -> bool:
    """Whether the dictation itself admits it could not hear clearly."""

    if witness.get("status") != "OBSERVED" or witness.get(
        "target_audible"
    ) is not True:
        return False
    heard = str(witness.get("heard_pinyin") or "")
    tokens = heard.split()
    confidence = witness.get("confidence")
    if (
        isinstance(confidence, (int, float))
        and not isinstance(confidence, bool)
        and float(confidence) < WEAK_WITNESS_CONFIDENCE
    ):
        return True
    uncertain = witness.get("uncertain_positions")
    return bool(
        tokens
        and isinstance(uncertain, list)
        and len(uncertain) >= max(2, len(tokens) * WEAK_WITNESS_UNCERTAIN_FRACTION)
    )


def _toneless_pinyin(text: str) -> list[str]:
    try:
        from pypinyin import lazy_pinyin
    except ImportError:
        return []
    return [
        re.sub(r"\s+", "", str(token).lower())
        for token in lazy_pinyin(text)
        if str(token).strip()
    ]


def _pinyin_ratio(heard_tokens: Sequence[str], candidate: str) -> float:
    """Best alignment of ``candidate`` against any subwindow of the dictation.

    她常把念的内容嵌在自己的句子里（「大家说的都是〈弹幕原文〉」）——
    整句比对会稀释相似度，按候选长度±1 开滑窗取最大比。
    """

    cand_tokens = _toneless_pinyin(candidate)
    if not heard_tokens or not cand_tokens:
        return 0.0
    cand_joined = " ".join(cand_tokens)
    best = 0.0
    for window in range(
        max(1, len(cand_tokens) - 2),
        min(len(heard_tokens), len(cand_tokens) + 2) + 1,
    ):
        for start in range(0, len(heard_tokens) - window + 1):
            ratio = SequenceMatcher(
                None,
                " ".join(heard_tokens[start : start + window]),
                cand_joined,
            ).ratio()
            if ratio > best:
                best = ratio
    return best


def parse_screen_text_pool(receipts: Sequence[Mapping[str, Any]]) -> list[str]:
    """Union the verbatim strings from frame-probe receipts (order-stable)."""

    pool: list[str] = []
    seen: set[str] = set()
    for receipt in receipts:
        answer = receipt.get("answer") if isinstance(receipt, Mapping) else None
        if not isinstance(answer, str):
            continue
        try:
            match = re.search(r"\{.*\}", answer, re.S)
            payload = json.loads(match.group(0)) if match else {}
        except (ValueError, AttributeError):
            continue
        for text in payload.get("texts") or []:
            cleaned = str(text).strip()
            if len(cleaned) < _MIN_TEXT_CHARS or cleaned in seen:
                continue
            seen.add(cleaned)
            pool.append(cleaned)
    return pool


def _screen_answer_valid(answer: str) -> bool:
    try:
        match = re.search(r"\{.*\}", answer, re.S)
        payload = json.loads(match.group(0)) if match else None
    except (ValueError, AttributeError, json.JSONDecodeError):
        return False
    return bool(isinstance(payload, dict) and isinstance(payload.get("texts"), list))


def match_screen_read(
    *,
    heard_pinyin: str,
    pool: Sequence[str],
) -> dict[str, Any] | None:
    """Pick the pool string she was reading, by pinyin alignment with margin."""

    heard_tokens = [t for t in str(heard_pinyin or "").lower().split() if t]
    if not heard_tokens or not pool:
        return None
    scored = sorted(
        (
            (_pinyin_ratio(heard_tokens, candidate), candidate)
            for candidate in pool
        ),
        reverse=True,
    )
    best_score, best_text = scored[0]
    runner_up = scored[1][0] if len(scored) > 1 else 0.0
    if best_score < _MIN_MATCH_RATIO:
        return None
    if best_score - runner_up < _MIN_MATCH_MARGIN and runner_up > 0.0:
        return None
    return {
        "text": best_text,
        "pinyin_ratio": round(best_score, 6),
        "runner_up_ratio": round(runner_up, 6),
    }


def make_screen_read_probe(
    *,
    media_path: Any,
    api_base: str,
    api_key: str,
    frame_probe: Callable[..., Mapping[str, Any]] | None = None,
) -> Callable[[int, int], dict[str, Any]]:
    """Build a bounded two-frame screen-text prober for one media file."""

    def probe(span_start_ms: int, span_end_ms: int) -> dict[str, Any]:
        probe_kwargs: dict[str, Any] = {
            "api_base": api_base,
            "api_key": api_key,
        }
        if frame_probe is None:
            from src.autoslice.visual_witness import frame_vision_probe

            probe_kwargs["answer_validator"] = _screen_answer_valid
        else:
            frame_vision_probe = frame_probe  # type: ignore[assignment]
        duration = max(0, int(span_end_ms) - int(span_start_ms))
        sample_ms = sorted(
            {
                int(span_start_ms) + duration // 3,
                int(span_start_ms) + (2 * duration) // 3,
            }
        )
        receipts = [
            frame_vision_probe(
                media_path,
                ms,
                SCREEN_TEXT_QUESTION,
                **probe_kwargs,
            )
            for ms in sample_ms
        ]
        return {
            "schema_version": "screen-read-witness.v1",
            "media_path": str(media_path),
            "span_start_ms": int(span_start_ms),
            "span_end_ms": int(span_end_ms),
            "frame_ms": sample_ms,
            "receipts": [dict(row) for row in receipts],
            "pool": parse_screen_text_pool(receipts),
        }

    return probe


def locate_read_span(
    cue_text: str,
    candidate: str,
) -> tuple[int, int, float] | None:
    """Locate the character span of ``cue_text`` she used to read ``candidate``.

    嵌入式念读（「大家说的都是〈弹幕原文〉」）：在 cue 的逐字拼音序列上
    按候选长度±1 开滑窗，取拼音相似度最高的字符窗；命中返回
    (start_cp, end_cp, ratio)。CJK 一字一音节的近似在此场景成立；含
    拉丁段的 cue 由 lazy_pinyin 原样保留 token，窗口仍按 token 对齐。
    """

    try:
        from pypinyin import lazy_pinyin
    except ImportError:
        return None
    chars = list(cue_text)
    if not chars:
        return None
    char_tokens = [
        re.sub(r"\s+", "", str(token).lower())
        for token in lazy_pinyin(cue_text)
    ]
    if len(char_tokens) != len(chars):
        # token↔字符不可逐位对齐（连写拉丁等）：退化为整 cue 替换判定
        ratio = _pinyin_ratio(
            [t for t in char_tokens if t], candidate
        )
        return (0, len(chars), ratio) if ratio > 0 else None
    cand_tokens = _toneless_pinyin(candidate)
    if not cand_tokens:
        return None
    cand_joined = " ".join(cand_tokens)
    best: tuple[int, int, float] | None = None
    for window in range(
        max(1, len(cand_tokens) - 2),
        min(len(chars), len(cand_tokens) + 2) + 1,
    ):
        for start in range(0, len(chars) - window + 1):
            ratio = SequenceMatcher(
                None,
                " ".join(char_tokens[start : start + window]),
                cand_joined,
            ).ratio()
            if best is None or ratio > best[2]:
                best = (start, start + window, ratio)
    return best


def danmaku_text_pool(
    clip_context: Mapping[str, Any] | None,
) -> list[str]:
    """结构化弹幕/SC 文本池（维护者 扩展：查证先弹幕后画面）。

    语境不通的 finding 里，她念的可能是弹幕原文——池按拼音对齐挑选，
    命中走 structured_chat_bound 出处。零外部调用。
    """

    if not isinstance(clip_context, Mapping):
        return []
    rows = clip_context.get("structured_chat")
    if not isinstance(rows, list):
        return []
    pool: list[str] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        text = str(row.get("text") or row.get("message") or "").strip()
        if len(text) < _MIN_TEXT_CHARS or text in seen:
            continue
        seen.add(text)
        pool.append(text)
    return pool


__all__ = [
    "SCREEN_TEXT_QUESTION",
    "danmaku_text_pool",
    "make_screen_read_probe",
    "match_screen_read",
    "parse_screen_text_pool",
    "witness_is_weak",
]


def build_env_screen_read_probe(media_path: Any):
    """CPA-primary 视觉读屏探针；AGY 仅作后备。"""

    import os
    import shutil
    from pathlib import Path

    agy_bin = os.environ.get(
        "AGY_BIN",
        str(Path.home() / ".local" / "bin" / "agy"),
    )
    api_base = os.environ.get("CPA_BASE_URL", "").strip()
    api_key = os.environ.get("CPA_API_KEY", "").strip()
    agy_available = bool(Path(agy_bin).is_file() or shutil.which(agy_bin))
    if not Path(media_path).is_file() or not (
        (api_base and api_key) or agy_available
    ):
        return None
    return make_screen_read_probe(
        media_path=media_path,
        api_base=api_base,
        api_key=api_key,
    )
