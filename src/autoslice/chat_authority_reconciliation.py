"""Bound evidence preparation for a chat owner's final-review conflict.

This module does not decide text or grant mutation authority.  The original
receipt and terminal answer remain immutable evidence for a new owner call.
"""
from __future__ import annotations

import hashlib
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable, Literal, TypedDict, cast

from .chat_authority_ownership import ChatAuthorityProjection, build_chat_authority_conflict_projection
from .chat_evidence import ChatEvidence, prepare_chat_evidence
from .danmaku_evidence import load_danmaku_xml
from .review_errors import ContractRejection
from .semantic_terminal_adjudication import terminal_prompt_matches
from .targeted_relisten import validate_current_no_gain
from .context_assisted_audio import digest
from .continuation_subchecks import session_validated


class SourceConflictEvidence(TypedDict):
    schema_version: Literal['chat-source-conflict-evidence.v1']
    mutation_authorized: Literal[False]
    prior_chat_authority: ChatAuthorityProjection
    source_recording_start_ms: int
    terminal_review: Any  # Historical provider receipts are validated on entry.


def prepare_conflict_evidence(srt_text: str, finding: Any, authority: Any, *, delivery_start_ms: int) -> SourceConflictEvidence:
    """Reopen the real owner, XML event and terminal evidence, without a call."""
    if not isinstance(finding, Mapping):
        raise ContractRejection('CHAT_RECONCILIATION_FINDING_INVALID')
    cue_index = finding.get('cue_index')
    if type(cue_index) is not int:
        raise ContractRejection('CHAT_RECONCILIATION_FINDING_INVALID')
    projection = build_chat_authority_conflict_projection(
        srt_text, authority, cue_index=cue_index,
        delivery_start_ms=delivery_start_ms)
    if not isinstance(projection, Mapping) or projection.get('status') != 'BOUND':
        raise ContractRejection('CHAT_RECONCILIATION_OWNER_INVALID')
    adjudication = finding.get('exact_release_adjudication') or {}
    review = adjudication.get('terminal_review') if isinstance(adjudication, Mapping) else None
    if not isinstance(review, Mapping) or not isinstance(review.get('request'), Mapping):
        raise ContractRejection('CHAT_RECONCILIATION_TERMINAL_MISSING')
    request = review['request']
    check = request.get('check_request')
    prompt, raw = review.get('prompt'), review.get('raw_completion')
    def sha(text: str) -> str:
        return hashlib.sha256(text.encode('utf-8')).hexdigest()
    if (not isinstance(check, Mapping) or not isinstance(prompt, str) or not isinstance(raw, str)
            or review.get('prompt_sha256') != sha(prompt)
            or review.get('completion_sha256') != sha(raw)
            or request.get('srt_sha256') != sha(srt_text)
            or check.get('current_cue') != request.get('current_cue')
            or not isinstance(check.get('current_cue'), str)
            or sha(check['current_cue']) != projection['base_text_sha256']
            or check.get('cue_indexes') != [projection['cue_index']]
            or check.get('matched_start_ms') != projection['cue_start_ms']
            or check.get('matched_end_ms') != projection['cue_end_ms']
            or check.get('source_media_timeline_offset_ms') != delivery_start_ms
            or not cast(Callable[[Any, str], bool], terminal_prompt_matches)(request, prompt)):
        raise ContractRejection('CHAT_RECONCILIATION_TERMINAL_BINDING_DRIFT')
    proof = validate_current_no_gain(request['audio_gain_termination'], check)
    source = projection['source']
    if projection.get('kind') != 'danmaku' or source['event_id']:
        raise ContractRejection('CHAT_RECONCILIATION_SOURCE_KIND_UNSUPPORTED')
    try:
        payload = Path(source['path']).read_bytes()
    except OSError as exc:
        raise ContractRejection('CHAT_RECONCILIATION_SOURCE_UNAVAILABLE') from exc
    if hashlib.sha256(payload).hexdigest() != source['sha256']:
        raise ContractRejection('CHAT_RECONCILIATION_SOURCE_DRIFT')
    from .validation_memo import note_hashed_file
    note_hashed_file(Path(source['path']), source['sha256'])
    origins: set[int] = set()
    for observation in proof['projection']['observations']:
        binding = (observation.get('media_source_projection') or {}).get('media_source_binding')
        if isinstance(binding, Mapping) and type(binding.get('recording_start_ms')) is int:
            origins.add(binding['recording_start_ms'])
    if len(origins) != 1:
        raise ContractRejection('CHAT_RECONCILIATION_SOURCE_CLOCK_AMBIGUOUS')
    origin = origins.pop()
    items = load_danmaku_xml(Path(source['path']), source_bytes=payload)
    events = prepare_chat_evidence(
        ChatEvidence('danmaku', item.offset_ms - origin, item.text,
                     source=source['path'], source_sha256=source['sha256'])
        for item in items if item.offset_ms - origin == source['offset_ms'])
    matches = [item for item in events if item.evidence_id == projection['applied_evidence_id']
               and item.text == projection['exact_text']]
    if len(matches) != 1:
        raise ContractRejection('CHAT_RECONCILIATION_SOURCE_EVENT_INVALID')
    return {'schema_version': 'chat-source-conflict-evidence.v1',
            'mutation_authorized': False, 'prior_chat_authority': deepcopy(projection),
            'source_recording_start_ms': origin,
            'terminal_review': deepcopy(dict(review))}


def build_owner_request(srt_text: str, finding: Any, authority: Any, *, delivery_start_ms: int) -> Any:
    """Create a new question; never change or re-sign the old review."""
    evidence = prepare_conflict_evidence(srt_text, finding, authority,
                                         delivery_start_ms=delivery_start_ms)
    request = deepcopy(evidence['terminal_review']['request'])
    check = request['check_request']
    check['prior_chat_authority'] = deepcopy(evidence['prior_chat_authority'])
    check.pop('request_sha256', None)
    check['request_sha256'] = cast(Callable[[Any], str], digest)(check)
    request['chat_source_conflict'] = {
        'schema_version': 'chat-source-owner-reevaluation.v1',
        'evidence': evidence,
        'original_applied': deepcopy(authority['applied']),
    }
    # Adding source evidence cannot change any observation or no-gain proof.
    validate_current_no_gain(request['audio_gain_termination'], check)
    return request


def validate_owner_request(request: Any) -> None:
    """Cold-reconstruct the whole request from original inputs and live XML."""
    if not isinstance(request, Mapping):
        raise ContractRejection('CHAT_RECONCILIATION_REQUEST_INVALID')
    context = request.get('chat_source_conflict')
    if not isinstance(context, Mapping) or context.get('schema_version') != 'chat-source-owner-reevaluation.v1':
        raise ContractRejection('CHAT_RECONCILIATION_REQUEST_INVALID')
    evidence = context.get('evidence')
    if not isinstance(evidence, Mapping):
        raise ContractRejection('CHAT_RECONCILIATION_REQUEST_INVALID')
    projection, review = evidence.get('prior_chat_authority'), evidence.get('terminal_review')
    if not isinstance(projection, Mapping) or not isinstance(review, Mapping):
        raise ContractRejection('CHAT_RECONCILIATION_REQUEST_INVALID')
    check = request.get('check_request') or {}
    if not isinstance(check, Mapping):
        raise ContractRejection('CHAT_RECONCILIATION_REQUEST_INVALID')
    srt_text, offset = check.get('whole_clip_current_srt'), check.get('source_media_timeline_offset_ms')
    if not isinstance(srt_text, str) or type(offset) is not int:
        raise ContractRejection('CHAT_RECONCILIATION_REQUEST_INVALID')
    finding = {'cue_index': projection.get('cue_index'),
               'exact_release_adjudication': {'terminal_review': review}}
    rebuilt = build_owner_request(srt_text, finding,
        {'applied': context.get('original_applied')},
        delivery_start_ms=offset)
    if rebuilt != dict(request):
        raise ContractRejection('CHAT_RECONCILIATION_REQUEST_DRIFT')


def owner_answer_to_adjudication(answer_receipt: Any, *, srt_text: str, finding: Any, authority: Any,
                                 delivery_start_ms: int, clip_context: Any) -> Any:
    """Cold-consume one fresh source-owner answer under the typed editor."""
    from .editorial_subtitle import SCHEMA, _decision, sha, validate_authority
    from .llm_client import extract_json_object, LlmJsonParseError
    from .semantic_terminal_adjudication import (_is_keep, keep_from_terminal,
        terminal_prompt_matches, validate_terminal_answer)
    if not isinstance(answer_receipt, Mapping) or answer_receipt.get('status') != 'VALIDATED':
        raise ContractRejection('CHAT_RECONCILIATION_ANSWER_REQUIRED')
    request = answer_receipt.get('terminal_request')
    if not isinstance(request, Mapping):
        raise ContractRejection('CHAT_RECONCILIATION_REQUEST_INVALID')
    validate_owner_request(request)
    conflict = request.get('chat_source_conflict')
    if not isinstance(conflict, Mapping) or not isinstance(authority, Mapping):
        raise ContractRejection('CHAT_RECONCILIATION_REQUEST_INVALID')
    if conflict.get('original_applied') != authority.get('applied'):
        raise ContractRejection('CHAT_RECONCILIATION_LIVE_OWNER_DRIFT')
    if request['srt_sha256'] != sha(srt_text) or request['source_media_timeline_offset_ms'] != delivery_start_ms:
        raise ContractRejection('CHAT_RECONCILIATION_LIVE_TEXT_DRIFT')
    prompt, raw = answer_receipt.get('prompt'), answer_receipt.get('raw_completion')
    if (not isinstance(prompt, str) or not isinstance(raw, str)
            or answer_receipt.get('prompt_sha256') != sha(prompt)
            or answer_receipt.get('completion_sha256') != sha(raw)
            or not cast(Callable[[Any, str], bool], terminal_prompt_matches)(request, prompt)):
        raise ContractRejection('CHAT_RECONCILIATION_ANSWER_BINDING_DRIFT')
    try:
        answer = extract_json_object(raw)
    except LlmJsonParseError as exc:
        raise ContractRejection('CHAT_RECONCILIATION_ANSWER_INVALID') from exc
    choice = validate_terminal_answer(request, answer, prompt=prompt,
                                      _source_owner_reconciliation=True)
    if choice != answer_receipt.get('choice'):
        raise ContractRejection('CHAT_RECONCILIATION_ANSWER_CHOICE_DRIFT')
    if choice == 'UNRESOLVABLE':
        return {'schema_version': 'subtitle-editorial-adjudication.v1', 'status': 'UNCERTAIN',
                'reason_code': 'TERMINAL_UNRESOLVABLE', 'repaired': False,
                'terminal_review': {'prompt': prompt, 'raw_completion': raw,
                                    'request': deepcopy(dict(request))}}
    if cast(Callable[[Any, Any], bool], _is_keep)(request, answer):
        return keep_from_terminal(srt_text=srt_text, finding=finding, request=request,
                                  answer=answer, prompt=prompt, raw=raw)
    receipt = {'schema_version': SCHEMA, 'request': deepcopy(dict(request)),
               'request_sha256': cast(Callable[[Any], str], digest)(request), 'prompt': prompt, 'prompt_sha256': sha(prompt),
               'raw_completion': raw, 'raw_sha256': sha(raw), 'source_owner_reconciliation': True}
    current = request.get('current_cue')
    if not isinstance(current, str):
        raise ContractRejection('CHAT_RECONCILIATION_REQUEST_INVALID')
    receipt['proposed_cue'] = _decision(request, answer, prompt=prompt,
                                         _source_owner_reconciliation=True)
    validate_authority(receipt, current=current, srt_text=srt_text,
                       clip_context=clip_context)
    return {'schema_version': 'subtitle-editorial-adjudication.v1', 'status': 'OBSERVED',
            'repaired': True, 'timing_immutable': True, 'decision_authority': 'CPA_JUDGE',
            'editorial_authority': receipt,
            'mutation_authority': {'schema_version': 'subtitle-correction-mutation-authority.v1',
                                   'status': 'PASS', 'basis': SCHEMA},
            'rebuilt_finding': {**dict(finding), 'repair_class': 'editorial_rendering',
                                'proposed_full_cue': receipt['proposed_cue']}}


_chain_session = cast(Callable[[str], Callable[[Callable[[Any, str], None]],
                                              Callable[[Any, str], None]]], session_validated)


@_chain_session('chat_source_owner_self_heal_chain')
def _validate_successor_chain(run: Any, expected_sha256: str) -> None:
    """Reuse the canonical run validator once per cold-read session."""
    from .final_review_contract import _validate_exact_final_cpa_self_heal
    _validate_exact_final_cpa_self_heal({'exact_final_cpa_self_heal': run}, expected_srt_sha256=expected_sha256,
                                       allow_pending=True)


def _source_check(repair: Any) -> Any:
    if not isinstance(repair, Mapping):
        return None
    editorial = repair.get('editorial_authority')
    if isinstance(editorial, Mapping):
        request = editorial.get('request')
        if isinstance(request, Mapping):
            return request.get('check_request')
    for key in ('acoustic_witness_binding', 'range_word_projection_binding'):
        binding = repair.get(key)
        if isinstance(binding, Mapping) and isinstance(binding.get('check_request'), Mapping):
            return binding['check_request']
    return None


def _source_input_srt_sha(repair: Any, check: Any) -> str | None:
    """Read the input identity from the existing bound edit/check request."""
    editorial = repair.get('editorial_authority') if isinstance(repair, Mapping) else None
    request = editorial.get('request') if isinstance(editorial, Mapping) else None
    if isinstance(request, Mapping) and isinstance(request.get('srt_sha256'), str):
        return 'sha256:' + str(request['srt_sha256']).removeprefix('sha256:')
    text = check.get('whole_clip_current_srt') if isinstance(check, Mapping) else None
    return 'sha256:' + hashlib.sha256(text.encode()).hexdigest() if isinstance(text, str) else None


def _successor_preserves_owner(authority: Any, *, receipt: Any, repair_sha: str,
                               expected_text: str, current_srt_text: str,
                               delivery_start_ms: int, physical_window: tuple[int, int]) -> bool:
    """Follow only the authenticated pass chain for this physical source window."""
    from .audio_question_identity import physical_target, source_binding
    from .canonical_json_hash import canonical_sha256
    history = authority.get('exact_final_cpa_self_heal')
    if not isinstance(history, Mapping) or not isinstance(history.get('passes'), list):
        return False
    from .exact_final_cpa_history import repair_history
    repair_history(history)  # Reopen historical run seals before using their order.
    retained = history.get('retained_previous_runs', [])
    if not isinstance(retained, list):
        return False
    runs = [entry['audit'] for entry in retained] + [history]
    source_hits: list[int] = []
    for run_index, run in enumerate(runs):
        for pass_row in run['passes']:
            for repair in pass_row['repairs']:
                if (isinstance(repair, Mapping)
                        and type(repair.get('matched_start_ms')) is int
                        and type(repair.get('matched_end_ms')) is int
                        and (repair['matched_start_ms'] + delivery_start_ms,
                             repair['matched_end_ms'] + delivery_start_ms) == physical_window
                        and 'sha256:' + canonical_sha256(repair, allow_nan=True) == repair_sha):
                    source_hits.append(run_index)
    if len(source_hits) != 1:
        return False
    relevant_runs = runs[source_hits[0]:]
    previous_final = None
    for run_index, run in enumerate(relevant_runs):
        passes = run.get('passes')
        if not isinstance(passes, list) or not passes or not isinstance(passes[0], Mapping):
            return False
        if previous_final is not None and passes[0].get('input_srt_sha256') != previous_final:
            return False
        is_current = run_index == len(relevant_runs) - 1
        final_sha = ('sha256:' + hashlib.sha256(current_srt_text.encode('utf-8')).hexdigest()
                     if is_current else run.get('final_srt_sha256'))
        if not isinstance(final_sha, str):
            return False
        _validate_successor_chain(run, final_sha)
        previous_final = final_sha
    original_check = _source_check({'editorial_authority': receipt})
    if not isinstance(original_check, Mapping):
        return False
    source = source_binding(original_check)
    if source is None or physical_target(original_check) != physical_window:
        return False
    found = False
    for pass_row in (pass_row for run in relevant_runs for pass_row in run['passes']):
        was_found = found
        overlap_count = 0
        repairs = pass_row.get('repairs') if isinstance(pass_row, Mapping) else None
        if not isinstance(repairs, list):
            return False
        relevant = []
        for repair in repairs:
            if not isinstance(repair, Mapping):
                return False
            start, end = repair.get('matched_start_ms'), repair.get('matched_end_ms')
            if type(start) is not int or type(end) is not int:
                return False
            window = (start + delivery_start_ms, end + delivery_start_ms)
            if window[0] < physical_window[1] and window[1] > physical_window[0]:
                overlap_count += 1
            identity = ('sha256:' + canonical_sha256(repair, allow_nan=True)
                        if window == physical_window and not found else None)
            if identity == repair_sha:
                if found or window != physical_window or repair.get('editorial_authority') != receipt:
                    return False
                if _source_input_srt_sha(repair, original_check) != pass_row.get('input_srt_sha256'):
                    return False
                found = True
                if repair.get('after') != expected_text:
                    return False
                relevant.append(repair)
            elif found and window[0] < physical_window[1] and window[1] > physical_window[0]:
                if window != physical_window:
                    return False
                check = _source_check(repair)
                if (not isinstance(check, Mapping) or physical_target(check) != physical_window
                        or source_binding(check) != source or repair.get('before') != expected_text
                        or not isinstance(repair.get('after'), str) or not repair['after']
                        or _source_input_srt_sha(repair, check) != pass_row.get('input_srt_sha256')):
                    return False
                expected_text = repair['after']
                relevant.append(repair)
        if len(relevant) > 1 or (found and (was_found or relevant) and overlap_count > 1):
            return False
    if not found:
        return False
    from .jingting_chunker import parse_srt_cues
    matches = [cue for cue in parse_srt_cues(current_srt_text)
               if (cue.start_ms + delivery_start_ms, cue.end_ms + delivery_start_ms) == physical_window]
    return len(matches) == 1 and matches[0].text == expected_text


def validated_retired_chat_rows(authority: Any, *, current_srt_text: str, delivery_start_ms: int,
                                chain_srt_text: str | None = None) -> frozenset[int]:
    """Cold-check each source-owner claim; any ambiguous claim remains owned."""
    from collections import Counter
    from .editorial_subtitle import validate_authority
    from .jingting_chunker import parse_srt_cues
    if not isinstance(authority, Mapping):
        return frozenset()
    applied, owners = authority.get('applied'), authority.get('entity_repairs')
    registrations = authority.get('exact_final_cpa_surface_registrations')
    if not isinstance(applied, list) or not isinstance(owners, list) or not isinstance(registrations, list):
        return frozenset()
    claims = Counter(index for reg in registrations if isinstance(reg, Mapping)
                     for index in (reg.get('superseded_applied_indexes') or [])
                     if type(index) is int)
    owner_claims = Counter(reg.get('owner_entity_repair_index') for reg in registrations
                           if isinstance(reg, Mapping) and type(reg.get('owner_entity_repair_index')) is int)
    cues = list(parse_srt_cues(current_srt_text))
    chain_cues = list(parse_srt_cues(chain_srt_text)) if chain_srt_text is not None else cues
    valid: set[int] = set()
    for reg in registrations:
        if (not isinstance(reg, Mapping) or reg.get('status') != 'REGISTERED'
                or reg.get('schema_version') != 'exact-final-cpa-surface-registration.v1'):
            continue
        owner_index, indexes = reg.get('owner_entity_repair_index'), reg.get('superseded_applied_indexes')
        if type(owner_index) is not int or not 0 <= owner_index < len(owners) or owner_claims[owner_index] != 1 or not isinstance(indexes, list) or len(indexes) != 1:
            continue
        index = indexes[0]
        if type(index) is not int or not 0 <= index < len(applied) or claims[index] != 1:
            continue
        owner, row = owners[owner_index], applied[index]
        if not isinstance(owner, Mapping) or not isinstance(row, Mapping):
            continue
        receipt = owner.get('editorial_authority')
        reconciliation = row.get('reconciliation')
        if (not isinstance(receipt, Mapping) or receipt.get('source_owner_reconciliation') is not True
                or not isinstance(reconciliation, Mapping)
                or reconciliation.get('schema_version') != 'chat-source-owner-exact-read-supersession.v1'
                or reconciliation.get('status') != 'SUPERSEDED_BY_EXACT_FINAL_CPA'
                or reconciliation.get('timing_immutable') is not True
                or reg.get('exact_final_repair_sha256') != reconciliation.get('exact_final_repair_sha256')
                or owner.get('exact_final_repair_sha256') != reg.get('exact_final_repair_sha256')
                or owner.get('mode') != 'exact_final_cpa_self_heal'
                or owner.get('decision_authority') != 'CPA_JUDGE'
                or owner.get('timing_immutable') is not True or owner.get('survived') is not True
                or (owner.get('mutation_authority') or {}).get('status') != 'PASS'
                or owner.get('superseded_applied_indexes') != indexes):
            continue
        try:
            request = receipt['request']
            original = request['chat_source_conflict']['original_applied'][index]
            if not isinstance(original, Mapping) or any(row.get(k) != v for k, v in original.items()):
                continue
            projection = request['chat_source_conflict']['evidence']['prior_chat_authority']
            if projection['applied_index'] != index or projection['delivery_start_ms'] != delivery_start_ms:
                continue
            after_rows = owner.get('after')
            if not isinstance(after_rows, list) or len(after_rows) != 1 or not isinstance(after_rows[0], str):
                continue
            after = after_rows[0]
            matches = [cue for cue in cues if cue.start_ms + delivery_start_ms == row['matched_start_ms']
                       and cue.end_ms + delivery_start_ms == row['matched_end_ms']]
            chain_matches = [cue for cue in chain_cues if cue.start_ms + delivery_start_ms == row['matched_start_ms']
                             and cue.end_ms + delivery_start_ms == row['matched_end_ms']]
            if len(matches) != 1 or len(chain_matches) != 1 or matches[0].text != chain_matches[0].text:
                continue
            if matches[0].text == after:
                if validate_authority(receipt) != after:
                    continue
            elif not _successor_preserves_owner(authority, receipt=receipt,
                    repair_sha=reg['exact_final_repair_sha256'], expected_text=after,
                    current_srt_text=chain_srt_text if chain_srt_text is not None else current_srt_text,
                    delivery_start_ms=delivery_start_ms,
                    physical_window=(row['matched_start_ms'], row['matched_end_ms'])):
                continue
            if (owner.get('after') != [after]
                    or owner.get('before') != [request['current_cue']]
                    or owner.get('matched_start_ms') != row['matched_start_ms']
                    or owner.get('matched_end_ms') != row['matched_end_ms']):
                continue
            if reconciliation.get('before_sha256') != 'sha256:' + hashlib.sha256(request['current_cue'].encode()).hexdigest():
                continue
            if reconciliation.get('after_sha256') != 'sha256:' + hashlib.sha256(after.encode()).hexdigest():
                continue
            valid.add(index)
        except (ValueError, TypeError, KeyError, IndexError, OSError):
            continue
    return frozenset(valid)
