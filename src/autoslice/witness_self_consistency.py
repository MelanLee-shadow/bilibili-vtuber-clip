"""Same-target, same-witness-class pinyin consistency, without vote counting."""

from __future__ import annotations

from itertools import combinations
import re
import unicodedata
import hashlib
import json

from src.autoslice.subtitle_acceptability_policy import load_policy


def pinyin_tokens(value: str) -> list[str]:
    value = unicodedata.normalize("NFKD", value.lower().replace("u:", "v"))
    value = value.replace("u\N{COMBINING DIAERESIS}", "v")
    value = "".join(c for c in value if not unicodedata.combining(c))
    return re.findall(r"[a-zv]+|[?？]", re.sub(r"[1-5]", " ", value))


def normalized_edit_distance(a, b) -> float:
    """Levenshtein over syllables, divided by the longer reading length."""
    a = pinyin_tokens(a) if isinstance(a, str) else list(a)
    b = pinyin_tokens(b) if isinstance(b, str) else list(b)
    if not a and not b:
        return 0.0
    previous = list(range(len(b) + 1))
    for i, left in enumerate(a, 1):
        current = [i]
        for j, right in enumerate(b, 1):
            current.append(min(current[-1] + 1, previous[j] + 1, previous[j - 1] + (left != right)))
        previous = current
    return previous[-1] / max(len(a), len(b))


def assess_witness(readings, *, physical_target: str, witness_class: str, policy=None) -> dict:
    """Caller binds strings to one physical target and witness class.

    Duplicate cached readings are not extra observations. Missing or wildcard
    pinyin never establishes contradiction; preserve it as missing evidence.
    """
    if not physical_target or not witness_class:
        raise ValueError("physical_target and witness_class are required")
    policy = policy if policy is not None else load_policy()
    rules = policy["witness_consistency"]
    valid, missing = [], 0
    for value in readings:
        tokens = pinyin_tokens(value)
        if not tokens or "?" in tokens or "？" in tokens:
            missing += 1
        else:
            valid.append(" ".join(tokens))
    unique = list(dict.fromkeys(valid))
    pairs = [
        {"a": a, "b": b, "distance": round(normalized_edit_distance(a, b), 6)}
        for a, b in combinations(unique, 2)
    ]
    conflicts = [p for p in pairs if p["distance"] > rules["max_normalized_edit_distance"]]
    fraction = len(conflicts) / len(pairs) if pairs else 0.0
    inconsistent = (
        len(unique) >= rules["min_readings"]
        and bool(conflicts)
        and fraction >= rules["min_conflicting_pair_fraction"]
    )
    status = (
        "SELF_INCONSISTENT"
        if inconsistent
        else ("CONSISTENT" if len(valid) >= rules["min_readings"] else "INSUFFICIENT")
    )
    return {
        "physical_target": physical_target,
        "witness_class": witness_class,
        "status": status,
        "readings": unique,
        "reading_count": len(valid),
        "missing_count": missing,
        "conflicting_pair_count": len(conflicts),
        "pair_count": len(pairs),
        "conflicting_pair_fraction": round(fraction, 6),
        "pairs": pairs,
        "can_raise_question": rules["can_raise_question"],
        "can_sustain_question": rules["can_sustain_question_if_inconsistent"]
        if inconsistent
        else None,
    }


LOW_TRUST_NOTE = '该证人多次读法互相矛盾，可信度低；可以提出疑点，不计为实质冲突，不能维持未决。'


def bound_witness_assessment(check, witness=None):
    """Assess saved envelopes using G1, without rebinding a hearing."""
    from src.autoslice.acoustic_witness_adjudication import (build_witness_request,
        same_hearing, valid_witness_evidence)
    try:
        target = build_witness_request(check)
    except (KeyError, TypeError, ValueError):
        return assess_witness([], physical_target='UNBOUND_CLOCK',
            witness_class='candidate_blind_narrow_pinyin')
    readings = []
    rows = list(check.get('prior_acoustic_observations') or [])
    if witness is not None:
        rows.append({'witness_request': target, 'verdict': witness})
    for row in rows:
        own = row.get('witness_request')
        value = row.get('verdict', row)
        if (isinstance(value, dict) and same_hearing(value, target, bound_request=own)
                and valid_witness_evidence(value, request_sha256=value.get('request_sha256'))
                and value.get('status') == 'OBSERVED'
                and value.get('witness_protocol') == 'blind_pinyin'):
            readings.append(value.get('heard_pinyin', ''))
    return assess_witness(readings, physical_target=json.dumps([
        check['matched_start_ms'] + check['source_media_timeline_offset_ms'],
        check['matched_end_ms'] + check['source_media_timeline_offset_ms']]),
        witness_class='candidate_blind_narrow_pinyin')


def recorded_narrow_assessment(rows, *, question, physical_target):
    """Legacy prompt evidence scoped by the recorder's verified issue hash.

    Never returns an inferred envelope as a hearing or edit authority. The
    recorder verified source/target identity and raw hashes. Only its recorded
    current wording is restored to that original question representation.
    """
    from src.autoslice.audio_question_identity import same_audio_question
    readings = []
    for row in rows:
        if row.get('status') != 'COMPLETED':
            continue
        prompt = row['request'].get('prompt', '')
        match = re.search(r'^- CURRENT(?:（[^\n）]*）)?[：:][ \t]*([^\n]+)', prompt, re.M)
        if match is None:
            continue
        text = match[1].strip()
        original_question = {**question, 'current_cue': text,
            'base_text_sha256': hashlib.sha256(text.encode()).hexdigest()}
        if not same_audio_question(original_question, question, compare_proposed=False)[0]:
            continue
        readings.extend(re.findall(r'^- 疑似拼音:[ \t]*([a-zA-ZüvÜ?？0-5 ]+)$', prompt, re.M))
        readings.extend(re.findall(r'候选盲短句声学巡检[^\n]*?听得拼音\s+([a-zA-ZüvÜ0-5 ]+)',
            str(row['request'].get('material', {}).get('question', ''))))
    return assess_witness(readings, physical_target=physical_target,
        witness_class='candidate_blind_narrow_pinyin')


def assessment_prompt(assessment):
    if assessment['status'] != 'SELF_INCONSISTENT':
        return ''
    return '\n## 窄窗证人自一致性（内部审计，无改字权）\n' + LOW_TRUST_NOTE + '\n' + json.dumps(
        assessment, ensure_ascii=False, sort_keys=True) + '\n'
