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

违者整 cue 回退 draft 原文（fail-open to verbatim，绝不阻塞产线），逐条
入 audit。后续证据车道（chat authority / 实体音频仲裁 / 礼物链 / 硬表）
在本守卫之后运行且各自带证据，不受影响；代词终审(_cpa_pronoun_ta_pass)
的 tā 组已在同音白名单内。
"""

from __future__ import annotations

from difflib import SequenceMatcher
import json
import re
from pathlib import Path
from typing import Any, Iterable

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
    跨度级匹配不到表对，所以整句层面先试一次单表对应用）。"""

    for surface, canonical in pairs:
        if surface and canonical and surface in draft_text:
            if draft_text.replace(surface, canonical) == final_text:
                return True
    return False


def sanctioned_respell_pairs() -> frozenset[tuple[str, str]]:
    """白名单规范表汇总（表本身即证据；加载失败即空集，守卫只会更严）。"""

    pairs: set[tuple[str, str]] = set()
    try:
        from src.autoslice.chat_authority import (
            _CODE_SWITCH_CANONICAL_SURFACES,
            _HARD_MEME_CANONICAL_SURFACES,
            load_referent_groups,
        )

        pairs.update(_CODE_SWITCH_CANONICAL_SURFACES)
        pairs.update(_HARD_MEME_CANONICAL_SURFACES)
        asset = Path(__file__).resolve().parents[2] / "assets/lidousha/entity_confusables.json"
        for group in load_referent_groups(asset):
            for entity in group.entities:
                for surface in entity.surfaces:
                    if surface != entity.canonical:
                        pairs.add((surface, entity.canonical))
    except Exception:
        pass
    try:
        from scripts.gemini_slice_jingting import approved_timely_terms

        for record in approved_timely_terms():
            canonical = str(record.get("canonical") or "")
            display = str(record.get("display_name") or "")
            for source_key in ("aliases", "confusables"):
                for raw in record.get(source_key) or []:
                    surface = str(raw)
                    for target in (canonical, display):
                        if surface and target and surface != target:
                            pairs.add((surface, target))
    except Exception:
        pass
    return frozenset(pairs)


def _span_verdict(
    op: str,
    draft_span: str,
    final_span: str,
    *,
    agy_cue_text: str | None,
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
        return "REPLACE_UNWITNESSED"
    if op == "insert":
        if agy_cue_text and _strip_non_text(final_span) and _strip_non_text(final_span) in _strip_non_text(agy_cue_text):
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


def apply_subtitle_fidelity_guard(
    draft_srt: str,
    corrected_srt: str,
    *,
    agy_srt: str | None = None,
    sanctioned: Iterable[tuple[str, str]] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Revert unwitnessed rewrites cue-by-cue; never blocks, only audits."""

    pairs = tuple(sanctioned) if sanctioned is not None else tuple(sanctioned_respell_pairs())
    draft_cues = parse_srt_cues(draft_srt)
    final_cues = parse_srt_cues(corrected_srt)
    audit: dict[str, Any] = {
        "schema_version": "subtitle-fidelity-audit.v1",
        "agy_witness_available": agy_srt is not None,
        "reverted": [],
        "hallucination_drops": [],
    }
    if len(draft_cues) != len(final_cues):
        audit["status"] = "SKIPPED_CUE_COUNT_MISMATCH"
        audit["draft_cue_count"] = len(draft_cues)
        audit["final_cue_count"] = len(final_cues)
        return corrected_srt, audit
    agy_cues = parse_srt_cues(agy_srt) if agy_srt else []
    agy_by_index: dict[int, str] = {i: c.text for i, c in enumerate(agy_cues, start=1)}

    out_lines: list[str] = []
    for index, (draft_cue, final_cue) in enumerate(zip(draft_cues, final_cues), start=1):
        draft_text = draft_cue.text
        final_text = final_cue.text
        agy_text = agy_by_index.get(index) if agy_srt else None
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
                matcher = SequenceMatcher(None, draft_text, final_text, autojunk=False)
                for op, a1, a2, b1, b2 in matcher.get_opcodes():
                    if op == "equal":
                        continue
                    verdict = _span_verdict(
                        op,
                        draft_text[a1:a2],
                        final_text[b1:b2],
                        agy_cue_text=agy_text,
                        pairs=pairs,
                    )
                    if verdict:
                        violations.append(
                            {
                                "op": op,
                                "draft_span": draft_text[a1:a2][:40],
                                "final_span": final_text[b1:b2][:40],
                                "reason": verdict,
                            }
                        )
                if violations:
                    kept = draft_text
                    audit["reverted"].append(
                        {
                            "cue_index": index,
                            "draft": draft_text,
                            "attempted": final_text,
                            "violations": violations,
                        }
                    )
        start = final_cue.start_ms
        end = final_cue.end_ms
        out_lines.append(
            f"{index}\n{_ms_to_ts(start)} --> {_ms_to_ts(end)}\n{kept}\n"
        )
    audit["status"] = "APPLIED" if audit["reverted"] else "CLEAN"
    audit["reverted_count"] = len(audit["reverted"])
    return "\n".join(out_lines), audit


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
