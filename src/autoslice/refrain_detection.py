"""Detect repeated quoted lines within one subtitle; never nominate edits."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re

from src.autoslice.acoustic_pinyin import text_pinyin_tokens
from src.autoslice.channel_profile import LABEL_RE
from src.autoslice.meaning_difference import classify_difference, equivalence_key
from src.autoslice.restatement_recall import is_nonlexical_interjection
from src.autoslice.subtitle_acceptability_policy import load_policy
from src.autoslice.witness_self_consistency import normalized_edit_distance


def _tokens(text, policy):
    tokens = text_pinyin_tokens(equivalence_key(text, policy))
    if tokens is None:
        raise RuntimeError(
            "pypinyin unavailable; refrain detection requires existing pinyin backend"
        )
    return tokens


def is_refrain_variant(a: str, b: str, *, policy=None, glossary_terms=()) -> bool:
    policy = policy if policy is not None else load_policy()
    if not a.strip() or not b.strip():
        return False
    rules = policy["refrain"]
    if (
        rules["require_meaning_equivalence"]
        and classify_difference(a, b, glossary_terms=glossary_terms, policy=policy)[
            "meaning_carrying"
        ]
    ):
        return False
    x, y = _tokens(a, policy), _tokens(b, policy)
    return bool(
        x
        and y
        and abs(len(x) - len(y)) <= rules["max_extra_syllables"]
        and 1 - normalized_edit_distance(x, y) >= rules["min_pinyin_similarity"]
    )


def _cue(row, pos):
    if isinstance(row, str):
        return pos + 1, row
    if isinstance(row, dict):
        return int(row.get("index", row.get("cue_index", row.get("cue", pos + 1)))), row["text"]
    return int(getattr(row, "index", pos + 1)), row.text


@dataclass(frozen=True)
class RefrainOccurrence:
    cue_index: int
    text: str
    refrain_ids: tuple[str, ...]


def detect_refrains(cues, *, policy=None, glossary_terms=()) -> list[dict]:
    """Count distinct cues, not duplicate rows or copies within one cue.

    Punctuation-delimited clauses allow a quoted line inside a longer cue;
    every cluster member must match its representative (no transitive chaining).
    """
    policy = policy if policy is not None else load_policy()
    rules = policy["refrain"]
    groups = []
    cues = list(cues)
    film_id = hashlib.sha256(
        repr([_cue(row, pos) for pos, row in enumerate(cues)]).encode()
    ).hexdigest()[:16]
    for pos, row in enumerate(cues):
        index, text = _cue(row, pos)
        text = LABEL_RE.sub(r"\2", text)
        for part in dict.fromkeys([text, *re.split(r"[，。！？,!?；;：:\n]+", text)]):
            if (
                is_nonlexical_interjection(part)
                or len(_tokens(part, policy)) < rules["min_syllables"]
            ):
                continue
            group = next(
                (
                    g
                    for g in groups
                    if is_refrain_variant(
                        part, g["text"], policy=policy, glossary_terms=glossary_terms
                    )
                ),
                None,
            )
            if group is None:
                group = {"text": part, "variants": [], "cue_indexes": []}
                groups.append(group)
            if index not in group["cue_indexes"]:
                group["cue_indexes"].append(index)
            if part not in group["variants"]:
                group["variants"].append(part)
    result = []
    for group in groups:
        if len(group["cue_indexes"]) >= rules["min_occurrences"]:
            result.append(
                {
                    "id": f"{film_id}:refrain-{len(result) + 1}",
                    **group,
                    "occurrences": len(group["cue_indexes"]),
                }
            )
    return result


def refrain_support(text: str, refrains, *, policy=None, glossary_terms=()) -> list[dict]:
    policy = policy if policy is not None else load_policy()
    return [
        r
        for r in refrains
        if any(
            is_refrain_variant(text, v, policy=policy, glossary_terms=glossary_terms)
            for v in r["variants"]
        )
    ]


def bind_refrain_occurrence(
    cue_index: int, text: str, refrains, *, policy=None, glossary_terms=()
) -> RefrainOccurrence:
    ids = tuple(
        r["id"]
        for r in refrain_support(text, refrains, policy=policy, glossary_terms=glossary_terms)
        if cue_index in r["cue_indexes"]
    )
    return RefrainOccurrence(cue_index, text, ids)


def is_restatement_from_refrain(early: RefrainOccurrence, late: RefrainOccurrence) -> bool:
    """Require same-film counted refrain evidence, not two similar strings."""
    if not isinstance(early, RefrainOccurrence) or not isinstance(late, RefrainOccurrence):
        raise TypeError("bind occurrences against detect_refrains(cues) first")
    return early.cue_index < late.cue_index and bool(set(early.refrain_ids) & set(late.refrain_ids))
