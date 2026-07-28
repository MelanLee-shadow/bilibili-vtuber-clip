"""Final, profile-bound surface invariants.

Ordinary transcript canonicalization is advisory and may be superseded by
better evidence.  Rules marked ``hard-meme-canon`` are different: they are
unbypassable final-output policy and therefore run after every mutable text
stage.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from src.autoslice.channel_profile import load_channel_profile
from src.autoslice.jingting_chunker import parse_srt_cues


REPO_ROOT = Path(__file__).resolve().parents[2]
CHANNEL_PROFILE = load_channel_profile(REPO_ROOT)
_HARD_MEME_SURFACE_RULES = tuple(
    rule
    for rule in CHANNEL_PROFILE.canonical_surface_rules
    if rule.authority.endswith("-hard-meme-canon.v1")
)
_EXPECTED_VALUE_SURFACE_RULES = tuple(
    rule
    for rule in CHANNEL_PROFILE.canonical_surface_rules
    if rule.authority.endswith("-expected-value-canon.v1")
)


def _partition_expected_value_surface_rules():
    """Disable a statistical canon as soon as its source becomes a peer term."""

    # Delayed import avoids the term-authority/chat-authority import cycle at
    # module load time.  registered_terms() is the shared glossary/roster
    # authority; a profile typo surface is deliberately absent from it until
    # that surface is independently registered as a real name/term.
    from src.autoslice.term_authority import registered_terms

    registered = registered_terms()
    eligible = []
    conflicts = []
    for rule in _EXPECTED_VALUE_SURFACE_RULES:
        if rule.surface in registered and rule.canonical in registered:
            conflicts.append(rule)
        else:
            eligible.append(rule)
    return tuple(eligible), tuple(conflicts)


def _canonicalize_expected_value_surfaces_with_rules(
    text: str,
    rules,
) -> tuple[str, list[dict[str, str | int]]]:
    normalized = text
    replacements: list[dict[str, str | int]] = []
    for rule in rules:
        count = normalized.count(rule.surface)
        if not count:
            continue
        normalized = normalized.replace(rule.surface, rule.canonical)
        replacements.append(
            {
                "surface": rule.surface,
                "canonical": rule.canonical,
                "authority": rule.authority,
                "count": count,
            }
        )
    return normalized, replacements


def canonicalize_hard_meme_surfaces(
    text: str,
) -> tuple[str, list[dict[str, str | int]]]:
    normalized = text
    replacements: list[dict[str, str | int]] = []
    for rule in _HARD_MEME_SURFACE_RULES:
        count = normalized.count(rule.surface)
        if not count:
            continue
        normalized = normalized.replace(rule.surface, rule.canonical)
        replacements.append(
            {
                "surface": rule.surface,
                "canonical": rule.canonical,
                "authority": rule.authority,
                "count": count,
            }
        )
    return normalized, replacements


def canonicalize_expected_value_surfaces(
    text: str,
) -> tuple[str, list[dict[str, str | int]]]:
    """Apply selected high-prior canon unless both spellings are registered."""

    eligible, _conflicts = _partition_expected_value_surface_rules()
    return _canonicalize_expected_value_surfaces_with_rules(text, eligible)


def _srt_timestamp(ms: int) -> str:
    hours, remainder = divmod(max(0, int(ms)), 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def normalize_hard_meme_surfaces(
    srt_text: str,
) -> tuple[str, dict[str, Any]]:
    """Re-assert unbypassable meme canon without touching SRT timing."""

    cues = [cue for cue in parse_srt_cues(srt_text) if cue.text.strip()]
    texts = [cue.text for cue in cues]
    repairs: list[dict[str, Any]] = []
    for offset, before in enumerate(list(texts)):
        after, replacements = canonicalize_hard_meme_surfaces(before)
        if after == before:
            continue
        texts[offset] = after
        repairs.append(
            {
                "cue_index": offset + 1,
                "matched_start_ms": cues[offset].start_ms,
                "matched_end_ms": cues[offset].end_ms,
                "before": before,
                "after": after,
                "replacements": replacements,
            }
        )
    output = srt_text
    if repairs:
        output = "\n\n".join(
            (
                f"{index}\n{_srt_timestamp(cue.start_ms)} --> "
                f"{_srt_timestamp(cue.end_ms)}\n{text.strip()}"
            )
            for index, (cue, text) in enumerate(zip(cues, texts), start=1)
        ) + "\n"
    return output, {
        "schema_version": "hard-meme-surface-audit.v1",
        "status": "APPLIED" if repairs else "NO_CHANGE",
        "rule_count": len(_HARD_MEME_SURFACE_RULES),
        "repairs": repairs,
        "input_srt_sha256": hashlib.sha256(srt_text.encode()).hexdigest(),
        "output_srt_sha256": hashlib.sha256(output.encode()).hexdigest(),
    }


def normalize_expected_value_surfaces(
    srt_text: str,
) -> tuple[str, dict[str, Any]]:
    """Re-assert explicit expected-value canon after mutable model stages.

    The operator source-truth lane runs after this function and can therefore
    preserve a rare, source-bound exception.
    """

    cues = [cue for cue in parse_srt_cues(srt_text) if cue.text.strip()]
    texts = [cue.text for cue in cues]
    eligible_rules, conflict_rules = _partition_expected_value_surface_rules()
    repairs: list[dict[str, Any]] = []
    for offset, before in enumerate(list(texts)):
        after, replacements = _canonicalize_expected_value_surfaces_with_rules(
            before,
            eligible_rules,
        )
        if after == before:
            continue
        texts[offset] = after
        repairs.append(
            {
                "cue_index": offset + 1,
                "matched_start_ms": cues[offset].start_ms,
                "matched_end_ms": cues[offset].end_ms,
                "before": before,
                "after": after,
                "replacements": replacements,
                "decision_authority": "EXPECTED_VALUE_CANON",
            }
        )
    registered_name_conflicts = [
        {
            "surface": rule.surface,
            "canonical": rule.canonical,
            "authority": rule.authority,
            "count": sum(text.count(rule.surface) for text in texts),
            "routed": "CPA_REQUIRED",
            "registered_name_conflict": True,
        }
        for rule in conflict_rules
        if any(rule.surface in text for text in texts)
    ]
    output = srt_text
    if repairs:
        output = "\n\n".join(
            (
                f"{index}\n{_srt_timestamp(cue.start_ms)} --> "
                f"{_srt_timestamp(cue.end_ms)}\n{text.strip()}"
            )
            for index, (cue, text) in enumerate(zip(cues, texts), start=1)
        ) + "\n"
    return output, {
        "schema_version": "expected-value-surface-audit.v1",
        "status": "APPLIED" if repairs else "NO_CHANGE",
        "decision_authority": "EXPECTED_VALUE_CANON",
        "rule_count": len(_EXPECTED_VALUE_SURFACE_RULES),
        "eligible_rule_count": len(eligible_rules),
        "registered_name_conflicts": registered_name_conflicts,
        "repairs": repairs,
        "input_srt_sha256": hashlib.sha256(srt_text.encode()).hexdigest(),
        "output_srt_sha256": hashlib.sha256(output.encode()).hexdigest(),
    }


# ---------------------------------------------------------------------------
# 称呼串等价类（Ivan 2026-07-19 立项，kmx 称呼串 2:29 妈妈→吗 案）

ADDRESS_FORMULA_MEMBERS = ("姐姐", "妈妈", "宝宝", "老公", "主人", "宝贝")
_ADDRESS_SINGLE_TO_MEMBER = {"吗": "妈妈", "嘛": "妈妈", "妈": "妈妈"}
_ENUM_TAIL_PUNCT = "，。！？!?…"


def repair_address_enumerations(srt_text: str) -> tuple[str, dict[str, object]]:
    """固定称呼串{姐姐、妈妈、宝宝、老公、主人(、宝贝)}式内单字修复。

    只修最安全的形态：同一 cue 内以「、」分隔的枚举串里，至少两段是成员
    词时，夹在中间的单字近音 token（吗/嘛/妈）按成员词补全为「妈妈」。
    句尾疑问「…宝宝吗」没有顿号包夹，不在本规则射程（真疑问句保留——
    该形态交 glossary 规则和音频复核）。零模型、可审计（T0.5 同级）。
    """

    from src.autoslice.jingting_chunker import parse_srt_cues

    cues = [cue for cue in parse_srt_cues(srt_text) if cue.text.strip()]
    texts = [cue.text for cue in cues]
    repairs: list[dict[str, object]] = []
    for index, text in enumerate(texts):
        if "、" not in text:
            continue
        segments = text.split("、")
        member_hits = 0
        for segment in segments:
            probe = segment.strip().rstrip(_ENUM_TAIL_PUNCT)
            if any(probe == member or probe.endswith(member) for member in ADDRESS_FORMULA_MEMBERS):
                member_hits += 1
        if member_hits < 2:
            continue
        changed = False
        for position in range(1, len(segments)):
            probe = segments[position].strip().rstrip(_ENUM_TAIL_PUNCT)
            replacement = _ADDRESS_SINGLE_TO_MEMBER.get(probe)
            if replacement is None:
                continue
            tail = segments[position][len(segments[position].rstrip(_ENUM_TAIL_PUNCT)):]
            segments[position] = replacement + tail
            changed = True
        if changed:
            before = text
            texts[index] = "、".join(segments)
            repairs.append(
                {
                    "cue_index": index + 1,
                    "before": before,
                    "after": texts[index],
                }
            )
    output = srt_text
    if repairs:
        output = "\n\n".join(
            (
                f"{index}\n{_srt_timestamp(cue.start_ms)} --> "
                f"{_srt_timestamp(cue.end_ms)}\n{text.strip()}"
            )
            for index, (cue, text) in enumerate(zip(cues, texts), start=1)
        ) + "\n"
    return output, {
        "schema_version": "address-enumeration-audit.v1",
        "status": "APPLIED" if repairs else "NO_CHANGE",
        "repairs": repairs,
    }
