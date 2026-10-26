"""Strict, offline policy loader. This module grants no edit/release authority."""

from __future__ import annotations

import json
import math
import re
from pathlib import Path

DEFAULT_POLICY_PATH = (
    Path(__file__).resolve().parents[2] / "assets/lidousha/subtitle_acceptability_policy.v1.json"
)
_STRINGS = [str]
_SCHEMA = {
    "schema_version": str,
    "authority": str,
    "terminal_choices": _STRINGS,
    "grades": {
        "A": {
            "condition": str,
            "action": str,
            "min_independent_wide_witnesses": int,
            "require_context_and_refrain_agreement": bool,
        },
        **{g: {"condition": str, "action": str} for g in "BCD"},
    },
    "hard_prohibitions": {
        k: bool
        for k in (
            "change_clear_audio",
            "invent_unsourced_entities",
            "unsupported_derogatory_reading",
        )
    },
    "features": {
        **{
            k: _STRINGS
            for k in (
                "meaning_carrying",
                "non_meaning_carrying",
                "negation_polarity",
                "referent",
                "action",
                "evaluation_stance",
                "derogatory",
                "object_place",
                "syntax_role",
                "persona_action",
                "modal_particles",
                "fillers",
                "unfinished_restart_patterns",
            )
        },
        "number_time_pattern": str,
        "synonym_groups": [[str]],
        "word_order_rules": [{"pattern": str, "replacement": str}],
        "shortening_rules": [{"core_pattern": str, "tail_pattern": str}],
    },
    "c_rendering": {
        "clear_particles_stutters_restarts": str,
        "unclear_default": str,
        "shorten_only_if_removed_fully_inaudible": bool,
        "require_core_meaning_retained": bool,
        "forbid_new_facts_names_stance": bool,
    },
    "disclosure": {
        "internal_audio_marker": str,
        "audience_visible_allowed": bool,
        "audience_surfaces": _STRINGS,
        "developer_marker_patterns": _STRINGS,
    },
    "unsourced_name": {
        k: bool
        for k in ("retain_current", "provide_best_guess_internally", "block", "invent_entity")
    },
    "refrain": {
        "min_occurrences": int,
        "min_syllables": int,
        "min_pinyin_similarity": float,
        "max_extra_syllables": int,
        "require_meaning_equivalence": bool,
    },
    "witness_consistency": {
        "min_readings": int,
        "max_normalized_edit_distance": float,
        "min_conflicting_pair_fraction": float,
        "can_raise_question": bool,
        "can_sustain_question_if_inconsistent": bool,
    },
}


def _validate(value, schema, path="policy"):
    if isinstance(schema, dict):
        if not isinstance(value, dict) or set(value) != set(schema):
            raise ValueError(f"{path}: missing/unknown fields; expected {sorted(schema)}")
        for key, spec in schema.items():
            _validate(value[key], spec, f"{path}.{key}")
    elif isinstance(schema, list):
        if not isinstance(value, list) or not value:
            raise ValueError(f"{path}: expected nonempty list")
        for i, item in enumerate(value):
            _validate(item, schema[0], f"{path}[{i}]")
    elif schema is float:
        if type(value) not in {float, int} or not math.isfinite(value) or not 0 <= value <= 1:
            raise ValueError(f"{path}: expected finite probability")
    elif type(value) is not schema or (schema is str and not value.strip()):
        raise ValueError(f"{path}: expected {schema.__name__}")


def validate_policy(policy: dict) -> dict:
    """Reject unknown fields recursively, malformed types, ranges and regexes."""
    _validate(policy, _SCHEMA)
    if policy["schema_version"] != "subtitle-acceptability-policy.v1":
        raise ValueError("unsupported policy schema_version")
    if policy['terminal_choices'] != ['CURRENT', 'PROPOSED', 'CONTEXT_FIT', 'UNRESOLVABLE']:
        raise ValueError('unsupported terminal choices')
    for section, names in [
        ("refrain", ["min_occurrences", "min_syllables"]),
        ("witness_consistency", ["min_readings"]),
    ]:
        if any(policy[section][key] < 2 for key in names):
            raise ValueError(f"{section}: minimum must be >= 2")
    if (
        policy["refrain"]["max_extra_syllables"] < 0
        or policy["grades"]["A"]["min_independent_wide_witnesses"] < 2
    ):
        raise ValueError("invalid count threshold")
    f = policy["features"]
    if set(f["meaning_carrying"]) & set(f["non_meaning_carrying"]):
        raise ValueError("feature classes overlap")
    if any(len(group) < 2 or len(set(group)) != len(group) for group in f["synonym_groups"]):
        raise ValueError("synonym groups require distinct alternatives")
    for pattern in [
        f["number_time_pattern"],
        *f["unfinished_restart_patterns"],
        *policy["disclosure"]["developer_marker_patterns"],
    ]:
        re.compile(pattern)
    for rule in f["word_order_rules"]:
        re.compile(rule["pattern"]).sub(rule["replacement"], "")
    if any(
        "stem" not in re.compile(pattern).groupindex for pattern in f["unfinished_restart_patterns"]
    ):
        raise ValueError("restart patterns require a named stem to preserve subject/referent")
    for rule in f["shortening_rules"]:
        re.compile(rule["core_pattern"])
        re.compile(rule["tail_pattern"])
    expected = [
        ("clear_audio", "verbatim"),
        ("unclear_but_evidence_decisive", "best_supported_reading"),
        ("unclear_undecidable_meaning_equivalent", "retain_and_make_fluent"),
        ("unclear_undecidable_meaning_carrying", "block"),
    ]
    if any(
        (policy["grades"][g]["condition"], policy["grades"][g]["action"]) != pair
        for g, pair in zip("ABCD", expected)
    ):
        raise ValueError("unsupported grade condition/action")
    return policy


def _unique_object(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate policy key: {key}")
        out[key] = value
    return out


def load_policy(path: str | Path = DEFAULT_POLICY_PATH) -> dict:
    return validate_policy(
        json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
    )


def classify_grade(
    *,
    clear_audio: bool | None,
    independent_wide_witnesses: int = 0,
    context_and_refrain_agree: bool = False,
    evidence_decisive: bool | None,
    meaning_carrying: bool,
    policy: dict,
    unsourced_name_only: bool = False,
) -> str:
    """Evaluate explicitly supplied facts, never infer audio clarity from text.

    C/D require the caller to establish both unclear audio and no decisive
    evidence (including whole-session context). UNKNOWN is missing evidence,
    not a release decision. An unsourced name alone never blocks.
    """
    if clear_audio is True:
        a = policy["grades"]["A"]
        if independent_wide_witnesses >= a["min_independent_wide_witnesses"] and (
            context_and_refrain_agree or not a["require_context_and_refrain_agreement"]
        ):
            return "A"
        return "UNKNOWN"
    if clear_audio is not False:
        return "UNKNOWN"
    if evidence_decisive is True:
        return "B"
    if evidence_decisive is not False:
        return "UNKNOWN"
    if unsourced_name_only and not policy["unsourced_name"]["block"]:
        return "C"
    return "D" if meaning_carrying else "C"
