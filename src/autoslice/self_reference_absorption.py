"""Evidence-based resolution for host self-reference name slots.

Static glossary membership and the current final subtitle must not make one
proper name outrank another.  Every candidate in the same semantic slot is
scored against the source witness with the same phonetic gate.  A name is used
only when it is the unambiguous winner, or when earlier independently resolved
slots in the same clip provide unanimous local evidence.
"""

from __future__ import annotations

from difflib import SequenceMatcher
import re
from typing import Any

from src.autoslice.jingting_chunker import parse_srt_cues
from src.autoslice.subtitle_fidelity import _toneless_syllables


_CJK_NAME = r"[\u3400-\u9fff]{2,4}"
_SELF_REFERENCE_SLOT_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern)
    for pattern in (
        rf"找(?P<surface>{_CJK_NAME})来",
        rf"想让(?P<surface>{_CJK_NAME})说",
        rf"让(?P<surface>{_CJK_NAME})任选",
        rf"然后(?P<surface>{_CJK_NAME})(?:[，,、\s]*(?P=surface))?就任选",
        rf"(?P<surface>{_CJK_NAME})就任选",
        rf"由(?P<surface>{_CJK_NAME})自己",
        # Keep the optional preposition outside the proper-name slot.  If
        # ``由`` is swallowed into the surface (``由李豆沙``), repeat
        # consensus can incorrectly rewrite an already-correct ordinary
        # sentence by replacing four characters with a three-character name.
        rf"可以(?:由)?(?P<surface>{_CJK_NAME})自己",
        rf"因为(?P<surface>{_CJK_NAME})(?:$|[，,。！？\s]|切|是|把)",
        rf"用(?P<surface>{_CJK_NAME})的方式",
        rf"同意(?P<surface>{_CJK_NAME})把",
        rf"给了(?P<surface>{_CJK_NAME})一个",
        rf"(?P<surface>{_CJK_NAME})一直是",
        rf"(?P<surface>{_CJK_NAME})是什么",
        rf"让(?P<surface>[\u3400-\u9fff]{{2,4}}?)(?:线下)?(?:叫|喊)",
    )
)
_CANONICAL_NAMES = ("李豆沙", "小李", "豆沙")
_CANONICALS = frozenset(_CANONICAL_NAMES)
_EXCLUDED = frozenset({"礼墨", "李姐", "老师", "官方"})
_STANDALONE_ROLE_SLOT = re.compile(
    rf"^(?:做|作为)(?P<surface>{_CJK_NAME})[，,。！？!?]?$"
)
_ROLE_RESTATEMENT = re.compile(r"^作为一个")
_DISCOURSE_LOOKBACK_CUES = 8
_MIN_PRIOR_HOST_REFERENCES = 2
_MIN_DIRECT_NAME_RATIO = 0.80
_MIN_DIRECT_NAME_MARGIN = 0.10
_MIN_SOURCE_REPEAT_BACKED_NAME_RATIO = 0.45
_MIN_TEXT_REPEAT_BACKED_NAME_RATIO = 0.55


def _phonetic_text(value: str) -> str:
    syllables = _toneless_syllables(value)
    return " ".join(syllables)


def lidousha_phonetic_ratio(value: str) -> float:
    candidate = _phonetic_text(value)
    target = _phonetic_text("李豆沙")
    if not candidate or not target:
        return 0.0
    return SequenceMatcher(None, candidate, target, autojunk=False).ratio()


def _phonetic_ratio(value: str, target: str) -> float:
    candidate = _phonetic_text(value)
    target_value = _phonetic_text(target)
    if not candidate or not target_value:
        return 0.0
    return SequenceMatcher(None, candidate, target_value, autojunk=False).ratio()


def _slot_matches(text: str) -> list[tuple[int, int, str, str]]:
    matches: list[tuple[int, int, str, str]] = []
    for pattern in _SELF_REFERENCE_SLOT_PATTERNS:
        for match in pattern.finditer(text):
            start, end = match.span("surface")
            matches.append((start, end, match.group("surface"), pattern.pattern))
    return matches


def _overlapping_witness_text(
    source_cues: list[Any], *, start_ms: int, end_ms: int
) -> str:
    return " ".join(
        cue.text
        for cue in source_cues
        if cue.start_ms < end_ms and cue.end_ms > start_ms
    ).strip()


def _recent_unanimous_name(
    history: list[dict[str, Any]],
    *,
    cue_index: int,
) -> tuple[str | None, int]:
    by_cue: dict[int, set[str]] = {}
    for row in history:
        prior_index = int(row["cue_index"])
        if prior_index >= cue_index:
            continue
        if cue_index - prior_index > _DISCOURSE_LOOKBACK_CUES:
            continue
        by_cue.setdefault(prior_index, set()).add(str(row["resolved_name"]))
    votes = [
        next(iter(names))
        for names in by_cue.values()
        if len(names) == 1
    ]
    if len(votes) < _MIN_PRIOR_HOST_REFERENCES or len(votes) != len(by_cue):
        return None, len(votes)
    unique = set(votes)
    if len(unique) != 1:
        return None, len(votes)
    return votes[0], len(votes)


def _resolve_name_surface(
    surface: str,
    *,
    history: list[dict[str, Any]],
    cue_index: int,
    allow_repeat_consensus: bool,
    repeat_min_ratio: float,
) -> dict[str, Any] | None:
    if surface in _EXCLUDED:
        return None
    scores = {
        name: _phonetic_ratio(surface, name)
        for name in _CANONICAL_NAMES
    }
    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    winner, winner_score = ranked[0]
    runner_up, runner_up_score = ranked[1]
    margin = winner_score - runner_up_score
    if (
        winner_score >= _MIN_DIRECT_NAME_RATIO
        and margin >= _MIN_DIRECT_NAME_MARGIN
    ):
        return {
            "resolved_name": winner,
            "scores": scores,
            "winner_score": winner_score,
            "runner_up": runner_up,
            "runner_up_score": runner_up_score,
            "phonetic_margin": margin,
            "evidence_mode": "DIRECT_PHONETIC_WINNER",
            "prior_consensus_votes": 0,
        }
    if not allow_repeat_consensus:
        return None
    consensus, vote_count = _recent_unanimous_name(
        history,
        cue_index=cue_index,
    )
    if (
        consensus is None
        or scores[consensus] < repeat_min_ratio
    ):
        return None
    return {
        "resolved_name": consensus,
        "scores": scores,
        "winner_score": scores[consensus],
        "runner_up": winner,
        "runner_up_score": winner_score,
        "phonetic_margin": scores[consensus] - winner_score,
        "evidence_mode": "LOCAL_REPEAT_CONSENSUS",
        "prior_consensus_votes": vote_count,
    }


def _source_name_evidence(
    witness_text: str,
    *,
    history: list[dict[str, Any]],
    cue_index: int,
) -> dict[str, Any] | None:
    candidates: list[dict[str, Any]] = []
    for _start, _end, surface, grammar in _slot_matches(witness_text):
        resolution = _resolve_name_surface(
            surface,
            history=history,
            cue_index=cue_index,
            allow_repeat_consensus=True,
            repeat_min_ratio=_MIN_SOURCE_REPEAT_BACKED_NAME_RATIO,
        )
        if resolution is None:
            continue
        candidates.append(
            {
                "surface": surface,
                "grammar_pattern": grammar,
                **resolution,
            }
        )
    if not candidates:
        return None
    # One overlapping witness window can contain more than one legitimate
    # self-reference slot.  If those slots resolve to different proper names,
    # proximity alone cannot tell which one corresponds to the final slot.
    # Fail closed instead of letting pattern order become an implicit prior.
    if len({str(row["resolved_name"]) for row in candidates}) != 1:
        return None
    return max(
        candidates,
        key=lambda row: (
            row["evidence_mode"] == "DIRECT_PHONETIC_WINNER",
            row["winner_score"],
            row["phonetic_margin"],
        ),
    )


def absorb_host_self_references(
    srt_text: str,
    *,
    source_witness_srt: str | None = None,
) -> tuple[str, dict[str, Any]]:
    cues = [cue for cue in parse_srt_cues(srt_text) if cue.text.strip()]
    source_cues = (
        [cue for cue in parse_srt_cues(source_witness_srt) if cue.text.strip()]
        if source_witness_srt
        else []
    )
    texts = [cue.text for cue in cues]
    repairs: list[dict[str, Any]] = []
    source_resolution_history: list[dict[str, Any]] = []
    local_resolution_history: list[dict[str, Any]] = []
    for cue_offset, (cue, text) in enumerate(zip(cues, texts)):
        cue_index = cue_offset + 1
        replacements: list[dict[str, Any]] = []
        witness_text = (
            _overlapping_witness_text(
                source_cues,
                start_ms=cue.start_ms,
                end_ms=cue.end_ms,
            )
            if source_cues
            else ""
        )
        for start, end, surface, grammar in _slot_matches(text):
            resolution: dict[str, Any] | None = None
            authority = "HOST_SELF_REFERENCE_EQUAL_NAME_AUDIO_RESOLUTION"
            source_evidence: dict[str, Any] | None = None
            if witness_text:
                source_evidence = _source_name_evidence(
                    witness_text,
                    history=source_resolution_history,
                    cue_index=cue_index,
                )
                resolution = source_evidence
            # Excluded names are real people/roles, so the final text alone
            # must never absorb them into the host.  They can be corrected only
            # when the independently timed BCUT witness resolves the same slot
            # unambiguously to a host self-reference.
            if surface in _EXCLUDED and source_evidence is None:
                continue
            if resolution is None:
                resolution = _resolve_name_surface(
                    surface,
                    history=local_resolution_history,
                    cue_index=cue_index,
                    allow_repeat_consensus=True,
                    repeat_min_ratio=_MIN_TEXT_REPEAT_BACKED_NAME_RATIO,
                )
                authority = "HOST_SELF_REFERENCE_EQUAL_NAME_TEXT_RESOLUTION"
            if resolution is None:
                continue
            resolved_name = str(resolution["resolved_name"])
            resolution_row = {
                "cue_index": cue_index,
                "resolved_name": resolved_name,
            }
            local_resolution_history.append(resolution_row)
            if source_evidence is not None:
                source_resolution_history.append(resolution_row)
            if resolved_name == surface:
                continue
            replacements.append(
                {
                    "start": start,
                    "end": end,
                    "surface": surface,
                    "grammar_pattern": grammar,
                    "authority": authority,
                    "resolution": resolution,
                    "source_witness_text": (
                        witness_text if source_evidence is not None else ""
                    ),
                    "source_evidence": source_evidence,
                }
            )
        # Replace right-to-left; overlapping grammar matches collapse to one.
        selected: list[dict[str, Any]] = []
        for row in sorted(
            replacements,
            key=lambda item: (int(item["start"]), int(item["end"])),
        ):
            if selected and int(row["start"]) < int(selected[-1]["end"]):
                if float(row["resolution"]["winner_score"]) > float(
                    selected[-1]["resolution"]["winner_score"]
                ):
                    selected[-1] = row
                continue
            selected.append(row)
        after = text
        for selected_row in reversed(selected):
            start = int(selected_row["start"])
            end = int(selected_row["end"])
            surface = str(selected_row["surface"])
            grammar = str(selected_row["grammar_pattern"])
            resolution = selected_row["resolution"]
            resolved_name = str(resolution["resolved_name"])
            after = after[:start] + resolved_name + after[end:]
            score_map = {
                name: round(float(score), 4)
                for name, score in resolution["scores"].items()
            }
            repair_row: dict[str, Any] = {
                "cue_index": cue_index,
                "matched_start_ms": cue.start_ms,
                "matched_end_ms": cue.end_ms,
                "before": surface,
                "after": resolved_name,
                "candidate_scores": score_map,
                "winner_score": round(float(resolution["winner_score"]), 4),
                "runner_up": resolution["runner_up"],
                "runner_up_score": round(
                    float(resolution["runner_up_score"]),
                    4,
                ),
                "phonetic_margin": round(
                    float(resolution["phonetic_margin"]),
                    4,
                ),
                "evidence_mode": resolution["evidence_mode"],
                "prior_consensus_votes": resolution["prior_consensus_votes"],
                "grammar_pattern": grammar,
                "authority": selected_row["authority"],
            }
            if selected_row["source_evidence"] is not None:
                source_evidence = selected_row["source_evidence"]
                repair_row.update(
                    {
                        "source_witness_text": selected_row[
                            "source_witness_text"
                        ],
                        "source_witness_surface": source_evidence["surface"],
                        "source_witness_grammar_pattern": source_evidence[
                            "grammar_pattern"
                        ],
                    }
                )
            repairs.append(repair_row)
        # A common spoken construction is “作为小李，作为一个……的人”.  A
        # rough ASR/final-review lane may collapse the short first clause into
        # an unrelated-looking noun such as “做刘翔/做流量”.  This is still a
        # host-name slot when the immediately following clause restates the
        # same role and the nearby discourse has already established the host
        # at least twice.  Resolve it after the ordinary grammar repairs so the
        # evidence is the repaired local discourse, not a channel-wide prior.
        role_match = _STANDALONE_ROLE_SLOT.fullmatch(after.strip())
        next_text = texts[cue_offset + 1].strip() if cue_offset + 1 < len(texts) else ""
        prior_name, prior_host_references = _recent_unanimous_name(
            local_resolution_history,
            cue_index=cue_index,
        )
        if (
            role_match is not None
            and role_match.group("surface") not in _CANONICALS
            and role_match.group("surface") not in _EXCLUDED
            and _ROLE_RESTATEMENT.match(next_text)
            and prior_name is not None
            and prior_host_references >= _MIN_PRIOR_HOST_REFERENCES
        ):
            before = after
            after = f"作为{prior_name}"
            repairs.append(
                {
                    "cue_index": cue_index,
                    "matched_start_ms": cue.start_ms,
                    "matched_end_ms": cue.end_ms,
                    "before": before,
                    "after": after,
                    "resolved_name": prior_name,
                    "prior_host_references": prior_host_references,
                    "next_cue": next_text,
                    "authority": (
                        "HOST_SELF_REFERENCE_EQUAL_NAME_REPEAT_PLUS_LOCAL_SLOT"
                    ),
                }
            )
            local_resolution_history.append(
                {
                    "cue_index": cue_index,
                    "resolved_name": prior_name,
                }
            )
        texts[cue_offset] = after
    if not repairs:
        return srt_text, {
            "schema_version": "self-reference-absorption-audit.v3",
            "status": "NO_MATCH",
            "source_witness_available": bool(source_cues),
            "candidate_names": sorted(_CANONICALS),
            "repairs": [],
        }
    output = "\n".join(
        f"{index}\n{_ms(cue.start_ms)} --> {_ms(cue.end_ms)}\n{text}\n"
        for index, (cue, text) in enumerate(zip(cues, texts), start=1)
    )
    return output, {
        "schema_version": "self-reference-absorption-audit.v3",
        "status": "APPLIED",
        "source_witness_available": bool(source_cues),
        "candidate_names": sorted(_CANONICALS),
        "repairs": repairs,
    }


def _ms(value_ms: int) -> str:
    hours, rem = divmod(int(value_ms), 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    seconds, millis = divmod(rem, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"
