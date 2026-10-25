"""Final, profile-bound surface invariants.

Ordinary transcript canonicalization is advisory and may be superseded by
better evidence. ``hard-meme-canon`` and ``caption-display-canon`` instead
declare fixed final spellings and run after every mutable text stage.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from src.autoslice.channel_profile import load_channel_profile
from src.autoslice.jingting_chunker import parse_srt_cues
from src.autoslice.term_authority import expected_value_surface_rules


REPO_ROOT = Path(__file__).resolve().parents[2]
CHANNEL_PROFILE = load_channel_profile(REPO_ROOT)
_CAPTION_DISPLAY_CANON_AUTHORITY = "lidousha-caption-display-canon.v1"
_HARD_MEME_SURFACE_RULES = tuple(
    rule
    for rule in CHANNEL_PROFILE.canonical_surface_rules
    if rule.authority.endswith(("-hard-meme-canon.v1", "-caption-display-canon.v1"))
)
_CAPTION_DISPLAY_SURFACE_RULES = tuple(
    rule
    for rule in CHANNEL_PROFILE.canonical_surface_rules
    if rule.authority == _CAPTION_DISPLAY_CANON_AUTHORITY
)
_EXPECTED_VALUE_SURFACE_RULES = expected_value_surface_rules()
_JAPANESE_NATIVE_SCRIPT_RULES = tuple(
    rule
    for rule in CHANNEL_PROFILE.canonical_surface_rules
    if rule.authority.endswith("-japanese-native-script-canon.v1")
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


def hard_meme_surface_rules():
    """Expose the unbypassable meme-canon rules to downstream judges.

    Text gates that reason over evidence (e.g. source-fact review) must be
    taught these rules explicitly: the canonical spelling carries the banned
    surface's full semantics, so a literal-evidence judge may neither treat
    the canonical form as unsupported nor repair it back to the banned form.
    """

    return _HARD_MEME_SURFACE_RULES


def _hard_meme_canon_prompt_block() -> str:
    """Teach the judge the channel's unbypassable meme canon.

    Without this the literal-evidence gate and the meme canon deadlock: the
    final transcript spells the meme canonically, raw danmaku keeps the banned
    surface, and a judge that only does literal binding "repairs" derived copy
    back and forth until the candidate dies (hook rewrite then also trips the
    scorecard-stale gate).  The canon is final-output law, so the judge must
    read canonical spellings as carrying the original surface's semantics.
    """

    rules = hard_meme_surface_rules()
    if not rules:
        return ""
    listing = "；".join(f"「{rule.surface}」一律写作「{rule.canonical}」" for rule in rules)
    return (
        "频道固定显示规范（梗词和已登记的口播缩写，最终输出铁律）：" + listing + "。"
        "规范词面承载同一原词的语义，不改变原音的读法：最终字幕与两份文案里的规范"
        "词面承载原词的完整语义，判断事实支持时必须按原词语义理解；不得因为"
        "弹幕/证据原文用了被禁拼写而判定文案不受支持，也永远不得把规范词面"
        "改回被禁拼写。你输出的一切文案必须使用规范词面。\n"
    )


def _canonicalize_surface_rules(
    text: str,
    rules,
) -> tuple[str, list[dict[str, str | int]]]:
    normalized = text
    replacements: list[dict[str, str | int]] = []
    for rule in rules:
        if rule.authority.endswith("-caption-display-canon.v1") and rule.surface.isascii():
            pattern = re.compile(
                rf"(?<![A-Za-z0-9_]){re.escape(rule.surface)}(?![A-Za-z0-9_])",
                flags=re.IGNORECASE,
            )
            normalized, count = pattern.subn(rule.canonical, normalized)
        elif (rule.authority.endswith("-caption-display-canon.v1")
              and rule.surface[-1:].isascii() and rule.surface[-1:].isdigit()):
            # A mixed-script role spelling is not a numeric/identifier prefix.
            # For example an explicitly registered X0 does not rewrite X01.
            pattern = re.compile(re.escape(rule.surface) + r"(?![A-Za-z0-9_])")
            normalized, count = pattern.subn(rule.canonical, normalized)
        else:
            count = normalized.count(rule.surface)
            if count:
                normalized = normalized.replace(rule.surface, rule.canonical)
        if not count:
            continue
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
    return _canonicalize_surface_rules(text, _HARD_MEME_SURFACE_RULES)


def canonicalize_caption_display_surfaces(
    text: str,
) -> tuple[str, list[dict[str, str | int]]]:
    """Apply only the profile's exact caption-display canon rules."""

    return _canonicalize_surface_rules(text, _CAPTION_DISPLAY_SURFACE_RULES)


def canonicalize_expected_value_surfaces(
    text: str,
) -> tuple[str, list[dict[str, str | int]]]:
    """Apply selected high-prior canon unless both spellings are registered."""

    eligible, _conflicts = _partition_expected_value_surface_rules()
    from src.autoslice.term_authority import spelling_equivalence_rules
    return _canonicalize_expected_value_surfaces_with_rules(text, (*spelling_equivalence_rules(), *eligible))


def canonicalize_japanese_native_script_surfaces(
    text: str,
) -> tuple[str, list[dict[str, str | int]]]:
    """Write known ordinary Japanese lexemes in native script, never romaji.

    ASCII spellings use case-insensitive token boundaries so ``ore`` cannot
    corrupt an English word such as ``more``. Registered official Latin proper
    names and genuine English speech are outside this narrow rule table.
    """

    normalized = text
    replacements: list[dict[str, str | int]] = []
    for rule in _JAPANESE_NATIVE_SCRIPT_RULES:
        if rule.surface.isascii():
            pattern = re.compile(
                rf"(?<![A-Za-z0-9_]){re.escape(rule.surface)}(?![A-Za-z0-9_])",
                flags=re.IGNORECASE,
            )
            normalized, count = pattern.subn(rule.canonical, normalized)
        else:
            count = normalized.count(rule.surface)
            if count:
                normalized = normalized.replace(rule.surface, rule.canonical)
        if count:
            replacements.append(
                {
                    "surface": rule.surface,
                    "canonical": rule.canonical,
                    "authority": rule.authority,
                    "count": count,
                }
            )
    return normalized, replacements


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


def normalize_caption_display_surfaces(
    srt_text: str,
) -> tuple[str, dict[str, Any]]:
    """Apply only fixed caption-display spellings without touching timing."""

    cues = [cue for cue in parse_srt_cues(srt_text) if cue.text.strip()]
    texts = [cue.text for cue in cues]
    repairs: list[dict[str, Any]] = []
    for offset, before in enumerate(list(texts)):
        after, replacements = canonicalize_caption_display_surfaces(before)
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
        "schema_version": "caption-display-surface-audit.v1",
        "status": "APPLIED" if repairs else "NO_CHANGE",
        "decision_authority": "CAPTION_DISPLAY_CANON",
        "authority": _CAPTION_DISPLAY_CANON_AUTHORITY,
        "rule_count": len(_CAPTION_DISPLAY_SURFACE_RULES),
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
    from src.autoslice.term_authority import spelling_equivalence_rules
    eligible_rules = (*spelling_equivalence_rules(), *eligible_rules)
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
        "rule_count": len(eligible_rules) + len(conflict_rules),
        "eligible_rule_count": len(eligible_rules),
        "registered_name_conflicts": registered_name_conflicts,
        "repairs": repairs,
        "input_srt_sha256": hashlib.sha256(srt_text.encode()).hexdigest(),
        "output_srt_sha256": hashlib.sha256(output.encode()).hexdigest(),
    }


def normalize_japanese_native_script_surfaces(
    srt_text: str,
) -> tuple[str, dict[str, Any]]:
    """Re-assert the native-script policy without changing SRT timing."""

    cues = [cue for cue in parse_srt_cues(srt_text) if cue.text.strip()]
    texts = [cue.text for cue in cues]
    repairs: list[dict[str, Any]] = []
    for offset, before in enumerate(list(texts)):
        after, replacements = canonicalize_japanese_native_script_surfaces(before)
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
                "decision_authority": "JAPANESE_NATIVE_SCRIPT_CANON",
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
        "schema_version": "japanese-native-script-surface-audit.v1",
        "status": "APPLIED" if repairs else "NO_CHANGE",
        "decision_authority": "JAPANESE_NATIVE_SCRIPT_CANON",
        "rule_count": len(_JAPANESE_NATIVE_SCRIPT_RULES),
        "repairs": repairs,
        "input_srt_sha256": hashlib.sha256(srt_text.encode()).hexdigest(),
        "output_srt_sha256": hashlib.sha256(output.encode()).hexdigest(),
    }





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


def protect_reviewed_name_slots(srt_text: str, reviewed_srt: str) -> tuple[str, list[dict[str, Any]]]:
    """Restore only a confirmed name slot with identical time and surrounding text.

    The reference must be the ordinary entity-reviewed subtitle, never raw ASR.
    Common words and unaligned rewrites do not receive global name substitutions.
    """
    from src.autoslice.term_authority import registered_person_names

    names = registered_person_names()
    reference = {(cue.start_ms, cue.end_ms): cue.text for cue in parse_srt_cues(reviewed_srt)}
    cues = parse_srt_cues(srt_text)
    texts, repairs = [], []
    for index, cue in enumerate(cues, 1):
        original = reference.get((cue.start_ms, cue.end_ms), '')
        text = cue.text
        for name in sorted(names, key=len, reverse=True):
            if original.count(name) != 1 or name in text:
                continue
            start = original.index(name)
            prefix, suffix = original[:start], original[start + len(name):]
            if not prefix and not suffix:
                continue
            if not text.startswith(prefix) or not text.endswith(suffix):
                continue
            stop = len(text) - len(suffix) if suffix else len(text)
            replacement = text[len(prefix):stop]
            if not replacement or len(replacement) > max(12, len(name) * 2):
                continue
            restored = prefix + name + suffix
            repairs.append({'cue_index': index, 'before': text, 'after': restored,
                            'protected_name': name, 'basis': 'ENTITY_REVIEWED_SAME_TIME_AND_SLOT'})
            text = restored
        texts.append(text)
    if not repairs:
        return srt_text, []
    return '\n\n'.join(f'{i}\n{_srt_timestamp(c.start_ms)} --> {_srt_timestamp(c.end_ms)}\n{t}'
                       for i, (c, t) in enumerate(zip(cues, texts), 1)) + '\n', repairs
