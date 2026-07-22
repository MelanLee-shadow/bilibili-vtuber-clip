"""终稿忠实性守卫：无证人不得改写（Ivan 2026-07-14 一九零/小李两案抽象）。

类定义：LLM 修正层（AGY 缺席时尤甚）会把口语"规范化"成它认为更通顺的
转述——「190」→「一米九」、「留下(误听的李豆沙)」→猜成「小李」。这不是
风格问题而是无证据改写。原则（Ivan 原话）：没有相关专名依据时自然要忠实
原文。

机制：BCUT draft 是逐字形态证人（ASR 按字面转写），AGY 精听是音频证人。
draft→终稿 的每个编辑跨度必须被至少一类证人背书：

- 同音重拼（内置封闭同音组：他她它TA / 的得地 / 吗嘛）；
- 数字读法等价（190 ↔ 一九零/幺九零 逐字读法）；
- 白名单规范表（硬梗表、代码切换表、实体混淆面→canonical、时效词
  alias/confusable→canonical）——这些表本身就是有据词典；
- 音频证人：新文本逐字出现在同 cue 的 AGY 文本里；
- 纯标点/空白差异；
- 整 cue 置空（幻听丢弃，修正合同明确允许）。

违者只回退该最小编辑跨度；同 cue 内已经有证据的修复继续保留。终稿先按
draft 的时间键对齐，删除/增补 cue 不再让整个守卫跳过。后续证据车道
（chat authority / 实体音频仲裁 / 礼物链 / 硬表）
在本守卫之后运行且各自带证据，不受影响；代词终审(_cpa_pronoun_ta_pass)
的 tā 组已在同音白名单内。
"""

from __future__ import annotations

from difflib import SequenceMatcher
import json
import re
from pathlib import Path
from typing import Any, Iterable, Mapping

from src.autoslice.jingting_chunker import parse_srt_cues

try:  # 可选依赖（2026-07-14 季下/小丽冤杀案）：有 pypinyin 时同音判定是
    # 真声学等价；缺失时退回下方手写封闭组保守运行，绝不因缺依赖崩产线。
    from pypinyin import lazy_pinyin as _lazy_pinyin

    _HAS_PYPINYIN = True
except Exception:  # pragma: no cover - 依赖缺失环境
    _lazy_pinyin = None
    _HAS_PYPINYIN = False

_HOMOPHONE_SETS: tuple[frozenset[str], ...] = (
    frozenset({"他", "她", "它", "TA", "ta"}),
    frozenset({"的", "得", "地"}),
    frozenset({"吗", "嘛"}),
)

_DIGIT_READINGS: dict[str, frozenset[str]] = {
    "0": frozenset({"零", "〇"}),
    "1": frozenset({"一", "幺"}),
    "2": frozenset({"二", "两"}),
    "3": frozenset({"三"}),
    "4": frozenset({"四"}),
    "5": frozenset({"五"}),
    "6": frozenset({"六"}),
    "7": frozenset({"七"}),
    "8": frozenset({"八"}),
    "9": frozenset({"九"}),
}

_NON_TEXT_RX = re.compile(r"[^0-9A-Za-z一-鿿]+")
_ARABIC_NUMBER_RX = re.compile(r"(?<![0-9A-Za-z])\d+(?:\.\d+)?(?![0-9A-Za-z])")
_JAPANESE_KANA_RX = re.compile(r"[ぁ-ゖァ-ヺー]")
_LATIN_WORD_RX = re.compile(r"\b[A-Za-z]+(?:['’-][A-Za-z]+)?\b")
_EMBEDDED_LATIN_WORD_RX = re.compile(
    r"(?<![A-Za-z])[A-Za-z]+(?:['’-][A-Za-z]+)?(?![A-Za-z])"
)
_SAFE_CODE_SWITCH_PHRASE_RX = re.compile(
    r"(?i)(?<![A-Za-z0-9])3D\s*Live(?![A-Za-z0-9])"
)
_SRT_CLOCK_RX = re.compile(r"^(\d{2}):(\d{2}):(\d{2}),(\d{3})$")
_IMPOSSIBLE_PUNCTUATION_RX = re.compile(r"[,，]\s*([。！？!?])")
_SAFE_CODE_SWITCH_WORDS = frozenset(
    {
        "ado",
        "ai",
        "awa",
        "fate",
        "galgame",
        "hime",
        "himehina",
        "hina",
        "id",
        "kmx",
        "level",
        "mujica",
        "mygo",
        "ok",
        "san",
        "sc",
        "soyo",
        "sumi",
        "testarossa",
        "vip",
    }
)


def _strip_non_text(value: str) -> str:
    return _NON_TEXT_RX.sub("", value)


def _homophone_representative(char: str) -> str:
    for group in _HOMOPHONE_SETS:
        if char in group:
            return sorted(group)[0]
    return char


def _toneless_syllables(value: str) -> tuple[str, ...]:
    """去声调音节序列（非汉字符号原样小写保留，如 TA/psp/数字）。"""

    stripped = _strip_non_text(value)
    if not stripped or _lazy_pinyin is None:
        return ()
    return tuple(s.lower() for s in _lazy_pinyin(stripped) if s)


def _homophone_equal(left: str, right: str) -> bool:
    a, b = _strip_non_text(left), _strip_non_text(right)
    if len(a) == len(b) and all(
        _homophone_representative(x) == _homophone_representative(y)
        for x, y in zip(a, b)
    ):
        # 手写封闭组优先：覆盖 pypinyin 多音字口径差（的/地、TA）。
        return True
    if _HAS_PYPINYIN and a and b:
        sa, sb = _toneless_syllables(a), _toneless_syllables(b)
        return bool(sa) and sa == sb
    return False


def digit_reading_equivalent(left: str, right: str) -> bool:
    """「190」↔「一九零/幺九零」逐字读法等价；插入量词（一米九）不算。"""

    a, b = _strip_non_text(left), _strip_non_text(right)
    if not a or not b:
        return False
    if not (a.isdigit() or b.isdigit()):
        return False
    digits, reading = (a, b) if a.isdigit() else (b, a)
    if len(digits) != len(reading):
        return False
    return all(
        reading[i] in _DIGIT_READINGS.get(digit, frozenset())
        for i, digit in enumerate(digits)
    )


def _sanctioned_match(
    draft_span: str, final_span: str, pairs: Iterable[tuple[str, str]]
) -> bool:
    for surface, canonical in pairs:
        if not surface or not canonical:
            continue
        if draft_span == surface and final_span == canonical:
            return True
        if surface in draft_span and draft_span.replace(surface, canonical, 1) == final_span:
            return True
    return False


def _sanctioned_cue_equal(
    draft_text: str, final_text: str, pairs: Iterable[tuple[str, str]]
) -> bool:
    """Cue 级白名单改写（SequenceMatcher 会把「直女→侄女」切成「直→侄」，
    跨度级匹配不到表对，所以整句层面先试一次单表对应用）。

    比对做去标点归一（2026-07-19 看花篮 ありがとう 案）：修复层按词表把
    音译误听换成日语原词时顺带调了标点（尾部「！」），精确相等把内容
    正确的白名单改写冤枉成无见证外语引入。标点渲染差异不是内容差异。
    """

    final_norm = _strip_non_text(final_text)
    for surface, canonical in pairs:
        if surface and canonical and surface in draft_text:
            if _strip_non_text(draft_text.replace(surface, canonical)) == final_norm:
                return True
    return False


def sanctioned_respell_pairs() -> frozenset[tuple[str, str]]:
    """白名单规范表汇总——委托唯一加载源 term_authority（2026-07-14 屎山
    整改：此前与审片员各自读表，两把尺漂移正是「立语→俚语」险案的病根）。"""

    from src.autoslice.term_authority import respell_pairs

    return respell_pairs()


def _span_verdict(
    op: str,
    draft_span: str,
    final_span: str,
    *,
    agy_cue_text: str | None,
    corroborating_cue_text: str | None,
    corroborating_texts: Iterable[str],
    pairs: Iterable[tuple[str, str]],
) -> str | None:
    """Return None when the span is witnessed, else a bounded violation code."""

    if not _strip_non_text(draft_span) and not _strip_non_text(final_span):
        return None
    if op == "replace":
        if _homophone_equal(draft_span, final_span):
            return None
        if digit_reading_equivalent(draft_span, final_span):
            return None
        if _sanctioned_match(draft_span, final_span, pairs):
            return None
        if agy_cue_text and _strip_non_text(final_span) and _strip_non_text(final_span) in _strip_non_text(agy_cue_text):
            return None
        if _corroborating_repeat_support(
            final_span,
            cue_text=corroborating_cue_text,
            all_texts=corroborating_texts,
        ):
            return None
        return "REPLACE_UNWITNESSED"
    if op == "insert":
        if agy_cue_text and _strip_non_text(final_span) and _strip_non_text(final_span) in _strip_non_text(agy_cue_text):
            return None
        if _corroborating_repeat_support(
            final_span,
            cue_text=corroborating_cue_text,
            all_texts=corroborating_texts,
        ):
            return None
        return "INSERT_UNWITNESSED"
    if op == "delete":
        if len(_strip_non_text(draft_span)) <= 2:
            return None
        if agy_cue_text is not None and _strip_non_text(draft_span) not in _strip_non_text(agy_cue_text):
            # 双证人都没有这段（BCUT 幻听、AGY 未闻）→ 允许删。
            return None
        return "DELETE_UNWITNESSED"
    return None


def _corroborating_repeat_support(
    final_span: str,
    *,
    cue_text: str | None,
    all_texts: Iterable[str],
) -> bool:
    """Use a fallback listen only when the same non-trivial span recurs.

    A hash-bound API fallback did hear the audio, but it is not an independent
    witness for its own one-off guess.  Requiring the current cue plus another
    cue to contain the same span turns it into bounded repetition support
    without globally trusting every fallback rewrite.  Single-character slots
    such as ``提`` are intentionally ineligible.
    """

    needle = _strip_non_text(final_span)
    if len(needle) < 2 or cue_text is None:
        return False
    if needle not in _strip_non_text(cue_text):
        return False
    return (
        sum(1 for text in all_texts if needle in _strip_non_text(text))
        >= 2
    )


def apply_subtitle_fidelity_guard(
    draft_srt: str,
    corrected_srt: str,
    *,
    agy_srt: str | None = None,
    corroborating_srt: str | None = None,
    sanctioned: Iterable[tuple[str, str]] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Revert only unwitnessed spans on the immutable draft timeline."""

    pairs = tuple(sanctioned) if sanctioned is not None else tuple(sanctioned_respell_pairs())
    draft_cues = parse_srt_cues(draft_srt)
    final_cues = parse_srt_cues(corrected_srt)
    audit: dict[str, Any] = {
        "schema_version": "subtitle-fidelity-audit.v2",
        "agy_witness_available": agy_srt is not None,
        "corroborating_audio_available": corroborating_srt is not None,
        "reverted": [],
        "hallucination_drops": [],
        "alignment_gaps": [],
        "ignored_final_cues": [],
    }
    agy_cues = parse_srt_cues(agy_srt) if agy_srt else []
    corroborating_cues = (
        parse_srt_cues(corroborating_srt) if corroborating_srt else []
    )

    def by_timing(cues):
        grouped: dict[tuple[int, int], list[Any]] = {}
        for cue in cues:
            grouped.setdefault((cue.start_ms, cue.end_ms), []).append(cue)
        return grouped

    final_by_timing = by_timing(final_cues)
    agy_by_timing = by_timing(agy_cues)
    corroborating_by_timing = by_timing(corroborating_cues)
    draft_timing_keys = {(cue.start_ms, cue.end_ms) for cue in draft_cues}
    for cue in final_cues:
        if (cue.start_ms, cue.end_ms) not in draft_timing_keys:
            audit["ignored_final_cues"].append(
                {
                    "start_ms": cue.start_ms,
                    "end_ms": cue.end_ms,
                    "text": cue.text,
                    "reason_code": "FINAL_CUE_HAS_NO_DRAFT_TIMING_KEY",
                }
            )

    out_lines: list[str] = []
    for index, draft_cue in enumerate(draft_cues, start=1):
        timing_key = (draft_cue.start_ms, draft_cue.end_ms)
        exact_final = final_by_timing.get(timing_key) or []
        if len(exact_final) == 1:
            final_text = exact_final[0].text
        elif len(exact_final) > 1:
            final_text = draft_cue.text
            audit["alignment_gaps"].append(
                {
                    "cue_index": index,
                    "start_ms": draft_cue.start_ms,
                    "end_ms": draft_cue.end_ms,
                    "reason_code": "FINAL_TIMING_KEY_AMBIGUOUS",
                }
            )
        else:
            overlapping = [
                cue
                for cue in final_cues
                if min(cue.end_ms, draft_cue.end_ms)
                > max(cue.start_ms, draft_cue.start_ms)
            ]
            if overlapping:
                final_text = draft_cue.text
                audit["alignment_gaps"].append(
                    {
                        "cue_index": index,
                        "start_ms": draft_cue.start_ms,
                        "end_ms": draft_cue.end_ms,
                        "reason_code": "FINAL_TIMING_DRIFT_OR_MERGE",
                        "overlapping_final_cue_count": len(overlapping),
                    }
                )
            else:
                # An intentionally dropped hallucination/filler cue is an
                # allowed empty-cue decision.  Keep the draft timing key so
                # downstream indices remain stable.
                final_text = ""
                audit["alignment_gaps"].append(
                    {
                        "cue_index": index,
                        "start_ms": draft_cue.start_ms,
                        "end_ms": draft_cue.end_ms,
                        "reason_code": "FINAL_CUE_DROPPED",
                    }
                )
        draft_text = draft_cue.text
        exact_agy = agy_by_timing.get(timing_key) or []
        agy_text = exact_agy[0].text if len(exact_agy) == 1 else None
        exact_corroborating = corroborating_by_timing.get(timing_key) or []
        corroborating_text = (
            exact_corroborating[0].text
            if len(exact_corroborating) == 1
            else None
        )
        kept = final_text
        if final_text != draft_text:
            if not final_text.strip():
                audit["hallucination_drops"].append({"cue_index": index, "draft": draft_text})
            elif agy_text is not None and _strip_non_text(final_text) == _strip_non_text(agy_text):
                pass  # 整句采信音频证人
            elif _homophone_equal(draft_text, final_text):
                pass
            elif _sanctioned_cue_equal(draft_text, final_text, pairs):
                pass
            else:
                violations: list[dict[str, str]] = []
                rebuilt: list[str] = []
                matcher = SequenceMatcher(None, draft_text, final_text, autojunk=False)
                for op, a1, a2, b1, b2 in matcher.get_opcodes():
                    if op == "equal":
                        rebuilt.append(draft_text[a1:a2])
                        continue
                    verdict = _span_verdict(
                        op,
                        draft_text[a1:a2],
                        final_text[b1:b2],
                        agy_cue_text=agy_text,
                        corroborating_cue_text=corroborating_text,
                        corroborating_texts=(
                            cue.text for cue in corroborating_cues
                        ),
                        pairs=pairs,
                    )
                    if verdict:
                        rebuilt.append(draft_text[a1:a2])
                        violations.append(
                            {
                                "op": op,
                                "draft_span": draft_text[a1:a2][:40],
                                "final_span": final_text[b1:b2][:40],
                                "reason": verdict,
                            }
                        )
                    else:
                        rebuilt.append(final_text[b1:b2])
                if violations:
                    kept = "".join(rebuilt)
                    audit["reverted"].append(
                        {
                            "cue_index": index,
                            "draft": draft_text,
                            "attempted": final_text,
                            "kept": kept,
                            "violations": violations,
                        }
                    )
        start = draft_cue.start_ms
        end = draft_cue.end_ms
        out_lines.append(
            f"{index}\n{_ms_to_ts(start)} --> {_ms_to_ts(end)}\n{kept}\n"
        )
    audit["draft_cue_count"] = len(draft_cues)
    audit["final_cue_count"] = len(final_cues)
    audit["status"] = (
        "APPLIED"
        if audit["reverted"]
        else (
            "ALIGNED_WITH_GAPS"
            if audit["alignment_gaps"] or audit["ignored_final_cues"]
            else "CLEAN"
        )
    )
    audit["reverted_count"] = len(audit["reverted"])
    return "\n".join(out_lines), audit


def apply_numeric_fact_provenance_guard(
    draft_srt: str,
    final_srt: str,
    *,
    structured_evidence: Iterable[object] = (),
    matched_structured_evidence: Iterable[Mapping[str, Any]] = (),
    evidence_pre_ms: int = 10_000,
    evidence_post_ms: int = 15_000,
) -> tuple[str, dict[str, Any]]:
    """Revert Arabic-number facts introduced without an independent source.

    A multimodal correction model is not an independent witness for a number
    it introduced itself (``0.4`` incident).  A numeric token may survive when
    it was already present on the initial ASR timeline, when same-time
    structured chat/SC contains that exact token, or when the chat-authority
    stage has already matched that exact structured text to the cue's spoken
    span.  The full cue is reverted so the number and its surrounding fact
    phrase cannot be validated separately.
    """

    draft_cues = parse_srt_cues(draft_srt)
    final_cues = parse_srt_cues(final_srt)
    audit: dict[str, Any] = {
        "schema_version": "numeric-fact-provenance-audit.v1",
        "status": "CLEAN",
        "reverted": [],
        "supported": [],
    }
    if len(draft_cues) != len(final_cues):
        audit["status"] = "SKIPPED_CUE_COUNT_MISMATCH"
        return final_srt, audit
    evidence_rows: list[tuple[int, str, str]] = []
    for item in structured_evidence:
        try:
            offset_ms = int(getattr(item, "offset_ms"))
            text = str(getattr(item, "text"))
            kind = str(getattr(item, "kind"))
        except (TypeError, ValueError, AttributeError):
            continue
        evidence_rows.append((offset_ms, text, kind))
    matched_evidence_rows: list[dict[str, Any]] = []
    for item in matched_structured_evidence:
        if item.get("survived") is not True:
            continue
        try:
            matched_start_ms = int(item["matched_start_ms"])
            matched_end_ms = int(item["matched_end_ms"])
            text = str(item["exact_text"])
            kind = str(item["kind"])
        except (KeyError, TypeError, ValueError):
            continue
        if matched_end_ms <= matched_start_ms:
            continue
        matched_evidence_rows.append(
            {
                "evidence_id": str(item.get("evidence_id") or ""),
                "kind": kind,
                "matched_start_ms": matched_start_ms,
                "matched_end_ms": matched_end_ms,
                "text": text,
            }
        )

    rendered: list[str] = []
    for index, (draft_cue, final_cue) in enumerate(
        zip(draft_cues, final_cues), start=1
    ):
        final_tokens = tuple(match.group(0) for match in _ARABIC_NUMBER_RX.finditer(final_cue.text))
        draft_tokens = set(
            match.group(0) for match in _ARABIC_NUMBER_RX.finditer(draft_cue.text)
        )
        unsupported: list[dict[str, Any]] = []
        for token in final_tokens:
            if token in draft_tokens:
                continue
            support = [
                {
                    "kind": kind,
                    "offset_ms": offset_ms,
                    "text": text,
                }
                for offset_ms, text, kind in evidence_rows
                if final_cue.start_ms - evidence_pre_ms
                <= offset_ms
                <= final_cue.end_ms + evidence_post_ms
                and token in text
            ]
            support.extend(
                {
                    **row,
                    "basis": "chat_authority_matched_spoken_span",
                }
                for row in matched_evidence_rows
                if max(
                    0,
                    min(final_cue.end_ms, row["matched_end_ms"])
                    - max(final_cue.start_ms, row["matched_start_ms"]),
                )
                > 0
                and token in row["text"]
            )
            if support:
                audit["supported"].append(
                    {
                        "cue_index": index,
                        "token": token,
                        "evidence": support,
                    }
                )
                continue
            unsupported.append(
                {
                    "token": token,
                    "reason_code": "NUMERIC_TOKEN_ABSENT_FROM_INITIAL_ASR_AND_STRUCTURED_EVIDENCE",
                }
            )
        kept = draft_cue.text if unsupported else final_cue.text
        if unsupported:
            audit["reverted"].append(
                {
                    "cue_index": index,
                    "start_ms": final_cue.start_ms,
                    "end_ms": final_cue.end_ms,
                    "draft": draft_cue.text,
                    "attempted": final_cue.text,
                    "unsupported": unsupported,
                }
            )
        rendered.append(
            f"{index}\n{_ms_to_ts(final_cue.start_ms)} --> "
            f"{_ms_to_ts(final_cue.end_ms)}\n{kept}"
        )
    if audit["reverted"]:
        audit["status"] = "REVERTED_UNPROVEN_NUMERIC_FACT"
    audit["reverted_count"] = len(audit["reverted"])
    return "\n\n".join(rendered) + ("\n" if rendered else ""), audit


_WITNESS_STRIP_RX = re.compile(r"[\s，。！？!?、；;：:…“”\"'（）()《》]+")


def _phonetic_transliteration_witness(
    source_text: str, final_text: str
) -> dict[str, Any] | None:
    """假名引入的拼音见证（2026-07-20 领个多→ありがとう 案）。

    注册外语插话实体（term_authority.foreign_insert_entities）的注册误听面
    走 sanctioned 对；**新变体**靠这里：剥掉两文本公共前后缀，剩余段若
    final 侧＝实体 canonical、draft 侧与其 readings 拼音对齐 ≥0.55，即构成
    有见证的转写修复。发现引擎在此只当证人，不当改写权。"""

    try:
        from src.autoslice.term_authority import foreign_insert_entities
        from src.autoslice.phonetic_scan import _aligned_score, _syllables

        entities = foreign_insert_entities()
    except Exception:
        return None
    if not entities:
        return None
    # 公共前后缀在去标点形态上对齐（2026-07-20 r7 案：尾部多一个「。」就
    # 掐断了后缀匹配）——标点渲染差异不是内容差异。
    source_clean = _WITNESS_STRIP_RX.sub("", source_text)
    final_clean = _WITNESS_STRIP_RX.sub("", final_text)
    prefix = 0
    while (
        prefix < len(source_clean)
        and prefix < len(final_clean)
        and source_clean[prefix] == final_clean[prefix]
    ):
        prefix += 1
    suffix = 0
    while (
        suffix < len(source_clean) - prefix
        and suffix < len(final_clean) - prefix
        and source_clean[len(source_clean) - 1 - suffix]
        == final_clean[len(final_clean) - 1 - suffix]
    ):
        suffix += 1
    draft_mid = source_clean[prefix : len(source_clean) - suffix]
    final_mid = final_clean[prefix : len(final_clean) - suffix]
    if not draft_mid or not final_mid:
        return None
    for canonical, readings in entities:
        # 连说形态（2026-07-20 灵感多 案：她把 ありがとう 说了两遍）——
        # final 段允许是 canonical 的 1-3 次重复；draft 段只需与单次读音
        # 对齐（ASR 常把连说塌缩成一个乱码）。
        repeat = 0
        for n in (1, 2, 3):
            if final_mid == canonical * n:
                repeat = n
                break
        if repeat == 0:
            continue
        spaced = [r.split() for r in readings if " " in str(r)]
        window = _syllables(draft_mid)
        if not spaced or not window:
            continue
        score = max(
            _aligned_score(window, reading * n)
            for reading in spaced
            for n in range(1, repeat + 1)
        )
        if score >= 0.55:
            return {
                "target": canonical,
                "draft_segment": draft_mid,
                "repeat": repeat,
                "phonetic_score": round(score, 3),
            }
    return None


def apply_source_language_preservation_guard(
    draft_srt: str,
    final_srt: str,
    *,
    sanctioned: Iterable[tuple[str, str]] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Keep foreign-language speech in its spoken language during correction.

    The subtitle correction lane fixes transcription; it is not a translation
    lane.  In particular, an embedded Japanese game/anime voice must not become
    an invented Chinese paraphrase.  A whole-cue sanctioned proper-name
    respelling is still allowed because that is transcript normalization rather
    than translation.
    """

    pairs = (
        tuple(sanctioned)
        if sanctioned is not None
        else tuple(sanctioned_respell_pairs())
    )
    draft_cues = parse_srt_cues(draft_srt)
    final_cues = parse_srt_cues(final_srt)
    audit: dict[str, Any] = {
        "schema_version": "source-language-preservation-audit.v1",
        "status": "CLEAN",
        "reverted": [],
        "unproven_foreign_introductions": [],
    }
    cue_count_matches = len(draft_cues) == len(final_cues)
    if not cue_count_matches:
        audit["draft_cue_count"] = len(draft_cues)
        audit["final_cue_count"] = len(final_cues)

    introduced_kana_rows: list[dict[str, Any]] = []
    for index, final_cue in enumerate(final_cues, start=1):
        if cue_count_matches:
            source_text = draft_cues[index - 1].text
        else:
            # Correction providers occasionally re-segment without changing
            # the timeline.  Cue-count drift must not disable the language
            # gate: bind each final cue to every overlapping source witness.
            source_text = " ".join(
                cue.text
                for cue in draft_cues
                if cue.start_ms < final_cue.end_ms and cue.end_ms > final_cue.start_ms
            ).strip()
        draft_kana_count = len(_JAPANESE_KANA_RX.findall(source_text))
        final_kana_count = len(_JAPANESE_KANA_RX.findall(final_cue.text))
        if (
            draft_kana_count == 0
            and final_kana_count >= 2
            and len(re.findall(r"[\u3400-\u9fff]", source_text)) >= 2
            and _strip_non_text(source_text) != _strip_non_text(final_cue.text)
            and not _sanctioned_cue_equal(source_text, final_cue.text, pairs)
        ):
            phonetic_witness = _phonetic_transliteration_witness(
                source_text, final_cue.text
            )
            if phonetic_witness is not None:
                audit.setdefault("witnessed_foreign_introductions", []).append(
                    {
                        "cue_index": index,
                        "draft": source_text,
                        "attempted": final_cue.text,
                        "witness": phonetic_witness,
                    }
                )
                continue
            introduced_kana_rows.append(
                {
                    "cue_index": index,
                    "start_ms": final_cue.start_ms,
                    "end_ms": final_cue.end_ms,
                    "draft": source_text,
                    "attempted": final_cue.text,
                    "reason": "FOREIGN_LANGUAGE_INTRODUCED_WITHOUT_SOURCE_WITNESS",
                }
            )
    # An isolated foreign cue is not automatically a host code-switch.  The
    # 2026-07-16 watched-video incident was exactly one Japanese cue introduced
    # after BCUT and therefore escaped the old adjacent-cluster rule.  Keep the
    # recovered text for review, but fail closed until a timeline-bound human
    # decision either drops background media speech or explicitly preserves a
    # host-spoken code-switch.
    audit["unproven_foreign_introductions"] = introduced_kana_rows
    if not cue_count_matches:
        audit["status"] = (
            "BLOCKED_UNPROVEN_FOREIGN_SPEAKER"
            if introduced_kana_rows
            else "SKIPPED_CUE_COUNT_MISMATCH"
        )
        audit["reverted_count"] = 0
        return final_srt, audit

    rendered: list[str] = []
    for index, (draft_cue, final_cue) in enumerate(
        zip(draft_cues, final_cues), start=1
    ):
        draft_kana_count = len(_JAPANESE_KANA_RX.findall(draft_cue.text))
        final_kana_count = len(_JAPANESE_KANA_RX.findall(final_cue.text))
        draft_latin_words = _EMBEDDED_LATIN_WORD_RX.findall(draft_cue.text)
        final_latin_words = _EMBEDDED_LATIN_WORD_RX.findall(final_cue.text)
        draft_cjk_count = len(re.findall(r"[\u3400-\u9fff]", draft_cue.text))
        source_language_removed = (
            (draft_kana_count >= 2 and final_kana_count == 0)
            # Whole-cue rollback is safe only for a Latin-language cue.  A
            # Chinese cue with short Latin labels is mixed speech, not an
            # English passage; count-based rollback restored the deleted `h tb`
            # echo in the 2026-07-22 incident.
            or (
                draft_cjk_count == 0
                and len(draft_latin_words) >= 3
                and len(final_latin_words) <= 1
            )
        )
        translated = (
            source_language_removed
            and bool(final_cue.text.strip())
            and _strip_non_text(draft_cue.text) != _strip_non_text(final_cue.text)
            and not _sanctioned_cue_equal(draft_cue.text, final_cue.text, pairs)
        )
        kept = draft_cue.text if translated else final_cue.text
        if translated:
            audit["reverted"].append(
                {
                    "cue_index": index,
                    "start_ms": final_cue.start_ms,
                    "end_ms": final_cue.end_ms,
                    "draft": draft_cue.text,
                    "attempted": final_cue.text,
                    "reason": "SOURCE_LANGUAGE_TRANSLATED_IN_CORRECTION_LANE",
                }
            )
        rendered.append(
            f"{index}\n{_ms_to_ts(final_cue.start_ms)} --> "
            f"{_ms_to_ts(final_cue.end_ms)}\n{kept}"
        )
    if introduced_kana_rows:
        # Do not silently choose between the draft and the correction here:
        # either could be the wrong-language ASR.  The producer blocks unless
        # a timeline-bound reviewed override resolves every affected cue.
        audit["status"] = "BLOCKED_UNPROVEN_FOREIGN_SPEAKER"
    elif audit["reverted"]:
        audit["status"] = "REVERTED_TRANSLATION"
    audit["reverted_count"] = len(audit["reverted"])
    return "\n\n".join(rendered) + ("\n" if rendered else ""), audit


def unproven_foreign_introductions_covered_by_overrides(
    audit: dict[str, Any],
    document: dict[str, Any],
) -> bool:
    """Prove every un-witnessed foreign-language cue has reviewed authority.

    A model may legitimately recover Japanese that the first ASR missed, but a
    correction model may also hallucinate Japanese from similar-sounding
    Chinese.  Only an exact timeline-bound schema-v3 override can release an
    introduced foreign passage, including a single isolated cue.
    """

    findings = audit.get("unproven_foreign_introductions")
    overrides = document.get("overrides")
    if (
        not isinstance(findings, list)
        or not findings
        or document.get("schema_version") != 3
        or not isinstance(overrides, list)
    ):
        return False

    def overlaps(
        left_start: int, left_end: int, right_start: int, right_end: int
    ) -> bool:
        return left_start < right_end and left_end > right_start

    for finding in findings:
        if not isinstance(finding, dict):
            return False
        attempted = str(finding.get("attempted") or "")
        finding_start = int(finding.get("start_ms") or 0)
        finding_end = int(finding.get("end_ms") or 0)
        covered = False
        for override in overrides:
            if not isinstance(override, dict):
                continue
            action = str(override.get("action", "replace"))
            replacement = str(override.get("text") or "")
            if action in {"replace", "drop"}:
                expected = override.get("expect")
                if not isinstance(expected, dict):
                    continue
                expected_start = _srt_clock_ms(str(expected.get("start") or ""))
                expected_end = _srt_clock_ms(str(expected.get("end") or ""))
                expected_texts = [
                    expected.get("text"),
                    *(expected.get("text_alternatives") or []),
                ]
                covered = (
                    expected_start == finding_start
                    and expected_end == finding_end
                    and attempted in expected_texts
                )
                if action == "replace" and not replacement:
                    covered = False
            elif action == "replace_substring":
                if not replacement:
                    continue
                locator = override.get("locator")
                if not isinstance(locator, dict):
                    continue
                locator_start = _srt_clock_ms(str(locator.get("start") or ""))
                locator_end = _srt_clock_ms(str(locator.get("end") or ""))
                old_text = str(override.get("old_text") or "")
                covered = (
                    locator_start is not None
                    and locator_end is not None
                    and overlaps(
                        finding_start,
                        finding_end,
                        locator_start,
                        locator_end,
                    )
                    and bool(old_text)
                    and attempted.count(old_text) == 1
                )
            if covered:
                break
        if not covered:
            return False
    return True


def has_unapproved_mixed_cjk_latin_phrase(text: str) -> bool:
    """Return whether one Chinese talk cue contains unsupported Latin word salad."""

    text = _SAFE_CODE_SWITCH_PHRASE_RX.sub("", text)
    # Single letters inside Chinese talk are option/grade/label tokens
    # (\u9009A\u8fd8\u662f\u9009B, S\u7ea7), not words of a foreign phrase \u2014 2026-07-19 an A/B
    # game-choice readout blocked a whole delivery.
    latin_words = [
        word.lower()
        for word in _EMBEDDED_LATIN_WORD_RX.findall(text)
        if len(word) >= 2
    ]
    return (
        len(latin_words) >= 2
        and re.search(r"[\u3400-\u9fff]", text) is not None
        and _JAPANESE_KANA_RX.search(text) is None
        and any(word not in _SAFE_CODE_SWITCH_WORDS for word in latin_words)
    )


def _srt_clock_ms(value: str) -> int | None:
    match = _SRT_CLOCK_RX.fullmatch(value.strip())
    if match is None:
        return None
    hours, minutes, seconds, millis = (int(part) for part in match.groups())
    return ((hours * 60 + minutes) * 60 + seconds) * 1000 + millis


def mixed_cjk_latin_findings_covered_by_overrides(
    audit: dict[str, Any],
    document: dict[str, Any],
) -> bool:
    """Prove every mixed-language finding has an exact, timeline-bound repair.

    A configured override file alone is not enough: each blocked cue must be
    covered by a schema-v3 decision whose projected output removes the anomaly.
    This lets the automatic lane fail closed while still allowing a reviewed
    local substring repair to run after LLM sentence resegmentation.
    """

    findings = audit.get("mixed_cjk_latin_cues")
    overrides = document.get("overrides")
    if (
        not isinstance(findings, list)
        or not findings
        or document.get("schema_version") != 3
        or not isinstance(overrides, list)
    ):
        return False

    def overlaps(
        left_start: int, left_end: int, right_start: int, right_end: int
    ) -> bool:
        return left_start < right_end and left_end > right_start

    for finding in findings:
        if not isinstance(finding, dict):
            return False
        finding_text = str(finding.get("text") or "")
        finding_start = int(finding.get("start_ms") or 0)
        finding_end = int(finding.get("end_ms") or 0)
        covered = False
        for override in overrides:
            if not isinstance(override, dict):
                continue
            action = str(override.get("action", "replace"))
            if action == "replace_substring":
                locator = override.get("locator")
                if not isinstance(locator, dict):
                    continue
                locator_start = _srt_clock_ms(str(locator.get("start") or ""))
                locator_end = _srt_clock_ms(str(locator.get("end") or ""))
                old_text = str(override.get("old_text") or "")
                replacement = str(override.get("text") or "")
                if (
                    locator_start is None
                    or locator_end is None
                    or not overlaps(
                        finding_start,
                        finding_end,
                        locator_start,
                        locator_end,
                    )
                    or not old_text
                    or finding_text.count(old_text) != 1
                    or not replacement
                ):
                    continue
                projected = finding_text.replace(old_text, replacement, 1)
                covered = not has_unapproved_mixed_cjk_latin_phrase(projected)
            elif action == "replace":
                expected = override.get("expect")
                if not isinstance(expected, dict):
                    continue
                expected_start = _srt_clock_ms(str(expected.get("start") or ""))
                expected_end = _srt_clock_ms(str(expected.get("end") or ""))
                expected_texts = [
                    expected.get("text"),
                    *(expected.get("text_alternatives") or []),
                ]
                replacement = str(override.get("text") or "")
                covered = (
                    expected_start == finding_start
                    and expected_end == finding_end
                    and finding_text in expected_texts
                    and bool(replacement)
                    and not has_unapproved_mixed_cjk_latin_phrase(replacement)
                )
            if covered:
                break
        if not covered:
            return False
    return True


def audit_foreign_script_consistency(srt_text: str) -> dict[str, Any]:
    """Detect a Japanese passage decoded as several English-heavy ASR cues.

    Source-language preservation prevents translation, but the ASR witness can
    itself be wrong.  A nearby run of Latin-heavy cues after kana dialogue is
    therefore held for language-aware transcription instead of being delivered.
    """

    cues = parse_srt_cues(srt_text)
    kana_indexes = [
        index
        for index, cue in enumerate(cues, start=1)
        if len(_JAPANESE_KANA_RX.findall(cue.text)) >= 2
    ]
    latin_rows = [
        {
            "cue_index": index,
            "text": cue.text,
            "latin_word_count": len(_LATIN_WORD_RX.findall(cue.text)),
        }
        for index, cue in enumerate(cues, start=1)
        if len(_LATIN_WORD_RX.findall(cue.text)) >= 3
        and len(_JAPANESE_KANA_RX.findall(cue.text)) == 0
    ]
    clustered = [
        row
        for row in latin_rows
        if any(abs(int(row["cue_index"]) - kana_index) <= 12 for kana_index in kana_indexes)
    ]
    mixed_cjk_latin_rows = []
    for index, cue in enumerate(cues, start=1):
        latin_words = [
            word.lower() for word in _EMBEDDED_LATIN_WORD_RX.findall(cue.text)
        ]
        if has_unapproved_mixed_cjk_latin_phrase(cue.text):
            mixed_cjk_latin_rows.append(
                {
                    "cue_index": index,
                    "start_ms": cue.start_ms,
                    "end_ms": cue.end_ms,
                    "text": cue.text,
                    "latin_words": latin_words,
                }
            )
    foreign_cluster_blocked = bool(kana_indexes) and len(clustered) >= 2
    mixed_cjk_latin_blocked = bool(mixed_cjk_latin_rows)
    if foreign_cluster_blocked:
        status = "BLOCKED_MIXED_FOREIGN_SCRIPT_CLUSTER"
        reason = (
            "Japanese passage contains a nearby run of Latin-heavy ASR cues; "
            "language-aware source transcription is required"
        )
    elif mixed_cjk_latin_blocked:
        status = "BLOCKED_MIXED_CJK_LATIN_PHRASE"
        reason = (
            "Chinese talk cue contains an unapproved multi-word Latin phrase; "
            "source-aware transcription is required"
        )
    else:
        status = "CLEAN"
        reason = None
    return {
        "schema_version": "foreign-script-consistency-audit.v1",
        "status": status,
        "kana_cue_indexes": kana_indexes,
        "latin_heavy_cues": clustered,
        "mixed_cjk_latin_cues": mixed_cjk_latin_rows,
        "reason": reason,
    }


def apply_title_mark_balance_guard(
    srt_text: str,
) -> tuple[str, dict[str, Any]]:
    """Balance one clearly dangling Chinese title mark without rewriting text."""

    cues = parse_srt_cues(srt_text)
    audit: dict[str, Any] = {
        "schema_version": "title-mark-balance-audit.v1",
        "status": "CLEAN",
        "repairs": [],
        "unresolved": [],
        "cross_cue_pairs": [],
    }
    rendered: list[str] = []
    for index, cue in enumerate(cues, start=1):
        text = cue.text
        opening_count = text.count("《")
        closing_count = text.count("》")
        if (
            text.count("》》") == 1
            and closing_count == opening_count + 1
        ):
            repaired = text.replace("》》", "》", 1)
            if repaired.count("《") == repaired.count("》"):
                audit["repairs"].append(
                    {
                        "cue_index": index,
                        "before": text,
                        "after": repaired,
                        "reason": "ONE_DUPLICATED_CHINESE_TITLE_CLOSE_MARK",
                    }
                )
                text = repaired
                opening_count = text.count("《")
                closing_count = text.count("》")
        if opening_count == closing_count + 1:
            next_text = cues[index].text if index < len(cues) else ""
            if next_text.count("》") > next_text.count("《"):
                audit["cross_cue_pairs"].append(
                    {
                        "cue_index": index,
                        "next_cue_index": index + 1,
                        "text": text,
                        "reason": "POSSIBLE_CROSS_CUE_TITLE_MARK_PAIR",
                    }
                )
                rendered.append(
                    f"{index}\n{_ms_to_ts(cue.start_ms)} --> "
                    f"{_ms_to_ts(cue.end_ms)}\n{text}"
                )
                continue
            match = re.search(r"([。！？!?.,，]?)$", text)
            assert match is not None
            punctuation = match.group(1)
            body = text[: len(text) - len(punctuation)] if punctuation else text
            repaired = f"{body}》{punctuation}"
            audit["repairs"].append(
                {
                    "cue_index": index,
                    "before": text,
                    "after": repaired,
                    "reason": "ONE_DANGLING_CHINESE_TITLE_OPEN_MARK",
                }
            )
            text = repaired
        elif closing_count == opening_count + 1:
            previous_text = cues[index - 2].text if index > 1 else ""
            if previous_text.count("《") > previous_text.count("》"):
                # The opening cue recorded this pair.  A title may legally span
                # SRT cues, so a balanced adjacent pair is evidence, not an
                # unresolved single-cue structure error.
                pass
            else:
                leading_title = re.match(
                    r"^([^《》：:，。！？!?]{2,30})》(?=[，。！？!?.,、]|$)",
                    text,
                )
                prefixed_prose = (
                    "接下来",
                    "下一首",
                    "这个叫",
                    "作品叫",
                    "书名叫",
                    "标题叫",
                )
                if leading_title and not leading_title.group(1).startswith(prefixed_prose):
                    repaired = "《" + text
                    audit["repairs"].append(
                        {
                            "cue_index": index,
                            "before": text,
                            "after": repaired,
                            "reason": "ONE_DANGLING_CHINESE_TITLE_CLOSE_MARK",
                        }
                    )
                    text = repaired
                else:
                    audit["unresolved"].append(
                        {
                            "cue_index": index,
                            "text": text,
                            "opening_count": opening_count,
                            "closing_count": closing_count,
                        }
                    )
        elif opening_count != closing_count:
            audit["unresolved"].append(
                {
                    "cue_index": index,
                    "text": text,
                    "opening_count": opening_count,
                    "closing_count": closing_count,
                }
            )
        rendered.append(
            f"{index}\n{_ms_to_ts(cue.start_ms)} --> {_ms_to_ts(cue.end_ms)}\n{text}"
        )
    if audit["unresolved"]:
        audit["status"] = "UNRESOLVED_COMPLEX_IMBALANCE"
    elif audit["repairs"]:
        audit["status"] = "APPLIED"
    elif audit["cross_cue_pairs"]:
        audit["status"] = "CROSS_CUE_BALANCED"
    audit["repair_count"] = len(audit["repairs"])
    return "\n\n".join(rendered) + ("\n" if rendered else ""), audit


def apply_impossible_punctuation_guard(
    srt_text: str,
) -> tuple[str, dict[str, Any]]:
    """Collapse comma-plus-terminal punctuation without changing any words."""

    cues = parse_srt_cues(srt_text)
    audit: dict[str, Any] = {
        "schema_version": "impossible-punctuation-audit.v1",
        "status": "CLEAN",
        "repairs": [],
    }
    rendered: list[str] = []
    for index, cue in enumerate(cues, start=1):
        repaired = _IMPOSSIBLE_PUNCTUATION_RX.sub(r"\1", cue.text)
        if repaired != cue.text:
            audit["repairs"].append(
                {
                    "cue_index": index,
                    "start_ms": cue.start_ms,
                    "end_ms": cue.end_ms,
                    "before": cue.text,
                    "after": repaired,
                }
            )
        rendered.append(
            f"{index}\n{_ms_to_ts(cue.start_ms)} --> {_ms_to_ts(cue.end_ms)}\n{repaired}"
        )
    if audit["repairs"]:
        audit["status"] = "APPLIED"
    audit["repair_count"] = len(audit["repairs"])
    return "\n\n".join(rendered) + ("\n" if rendered else ""), audit


def _ms_to_ts(value_ms: int) -> str:
    hours, rem = divmod(int(value_ms), 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    seconds, millis = divmod(rem, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def persist_fidelity_audit(path: Path, audit: dict[str, Any]) -> None:
    try:
        path.write_text(
            json.dumps(audit, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except OSError:
        pass
