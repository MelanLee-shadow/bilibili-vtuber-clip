"""F2 wrap-up: rejected answers are engineering failures with a real reason and a retry route.

Members come from the real producers (terminal funnel / offline acquisition
rig). An "older contract" request is the same producer's request without the
later contract stamp, rendered by the frozen historical renderer, which is how
every recorded production request (no terminal_contract_version) looks.
"""
from copy import deepcopy
import hashlib
import json

import pytest

from src.autoslice import finding_lifecycle as lifecycle
from src.autoslice.semantic_terminal_adjudication import terminal_prompt, validate_terminal_answer
from tests.test_exact_final_funnel import terminal_rig, run_terminal, unresolved, answer  # noqa: F401
from tests.test_targeted_relisten_public_contract import rig  # noqa: F401
from tests.test_terminal_contract_roundtrip import c_reply, produce
from tests.test_terminal_rework2_routes import _b_supported, _cue25_rig, _patch_cue25_range_plans

KEEP_B = 'B_EVIDENCE_SUPPORTS_CURRENT'
KEEP_C = 'C_MEANING_EQUIVALENT'


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def older_contract(request):
    """The recorded production shape: same request, no later contract stamp."""
    legacy = {key: deepcopy(value) for key, value in request.items()
              if key not in {'terminal_contract_version', 'answer_contract_version'}}
    return legacy, terminal_prompt(legacy)


def review_of(request, prompt, raw):
    return {'prompt': prompt, 'raw_completion': raw, 'prompt_sha256': sha(prompt),
            'completion_sha256': sha(raw), 'request': request}


def prompt_refs(prompt):
    return json.loads(next(line.split('：', 1)[1] for line in prompt.splitlines()
                           if line.startswith('可引用编号')))


def test_contract_versions_are_enumerated_and_unknown_fails():
    from src.autoslice.semantic_terminal_adjudication import TERMINAL_CONTRACT_VERSION, terminal_contract_retry
    assert terminal_contract_retry({})['kind'] == 'REASK_UNDER_CURRENT_CONTRACT'
    current = terminal_contract_retry({'terminal_contract_version': TERMINAL_CONTRACT_VERSION})
    assert current['kind'] == 'REVALIDATE_AFTER_CONSUMER_REPAIR_OR_NEW_MATERIAL'
    assert current['reask_under_current_contract'] is False
    for invalid in (TERMINAL_CONTRACT_VERSION + 1, 0, '4', 2.0):
        with pytest.raises(ValueError, match='TERMINAL_CONTRACT_VERSION_UNSUPPORTED'):
            terminal_contract_retry({'terminal_contract_version': invalid})
    # An undeterminable recorded version is never presumed old (no re-ask).
    row = {'exact_release_adjudication': {'status': 'UNCERTAIN', 'reason_code': 'X',
        'failure_layer': 'CONTRACT_ERROR', 'editorial_authority': {'request': {'schema_version': 'unknown'}}}}
    view = lifecycle.classify(row, terminal=True)
    assert view['state'] == 'CONTRACT_ERROR'
    assert view['recovery'] == 'REVALIDATE_ORIGINAL_ANSWER_AFTER_CONSUMER_REPAIR'
    assert view['contract_retry']['kind'] == 'CONTRACT_VERSION_UNDETERMINED'


def test_old_contract_b_without_offered_support_is_contract_error_then_reasked(tmp_path, monkeypatch):
    """cue25 class: route authority comes from the prompt the answer saw."""
    from src.autoslice import targeted_relisten as relisten
    from src.autoslice.editorial_subtitle import replay_terminal_adjudication
    from src.autoslice.final_review_contract import is_release_resolved_keep
    _patch_cue25_range_plans(monkeypatch)
    rig = _cue25_rig(tmp_path, monkeypatch, 'old-contract-b')
    current_audit, captured = produce(rig, _b_supported, native=('你喜欢', '你喜欢'))
    request = captured['request']
    current_version = request['terminal_contract_version']
    assert type(current_version) is int and current_version > 1
    assert current_audit.get('keep_route') == KEEP_B  # control: current contract offers B
    legacy, legacy_prompt = older_contract(request)
    legacy_data = json.loads(legacy_prompt.rsplit('\n', 1)[1])
    # The old prompt listed B, but its support citation list was empty because
    # the laughter was not literally transcribed (SUB-26 relaxation came later).
    assert set(legacy_data['keep_routes']) == {KEEP_B, KEEP_C}
    assert legacy_data['current_support_observation_refs'] == []
    refs = [ref for ref in captured['answer']['evidence_refs'] if ref in prompt_refs(legacy_prompt)]
    assert refs
    old_answer = {**captured['answer'], 'evidence_refs': refs}
    with pytest.raises(ValueError, match='TERMINAL_CURRENT_OBSERVATION_SUPPORT_REQUIRED'):
        validate_terminal_answer(legacy, old_answer, prompt=legacy_prompt)
    raw = json.dumps(old_answer, ensure_ascii=False)
    finding = {**rig.finding, 'exact_release_adjudication': {
        'status': 'UNCERTAIN', 'repaired': False, 'terminal_review': review_of(legacy, legacy_prompt, raw)}}
    rejected = replay_terminal_adjudication(rig.srt, finding, check_request=legacy['check_request'],
                                            clip_context=rig.context)
    assert rejected['reason_code'] == 'TERMINAL_CURRENT_OBSERVATION_SUPPORT_REQUIRED'
    assert rejected['contract_retry']['kind'] == 'REASK_UNDER_CURRENT_CONTRACT'
    row = {**rig.finding, 'exact_release_adjudication': rejected}
    view = lifecycle.classify(row, terminal=True)
    assert view['state'] == 'CONTRACT_ERROR' and view['recovery'] == 'REASK_UNDER_CURRENT_CONTRACT'
    assert not is_release_resolved_keep(row)  # not PASS
    assert lifecycle.release_reason_codes([row], terminal=True) == [lifecycle.ENGINEERING_FAILURE]
    # The ordinary owner asks the same physical question under the current contract.
    calls = []
    def judge(prompt):
        calls.append(prompt)
        data = json.loads(prompt.rsplit('\n', 1)[1])
        return json.dumps(_b_supported(data, prompt_refs(prompt)), ensure_ascii=False)
    retried = relisten.close_with_existing_editorial(srt_text=rig.srt, finding=row,
        recovery_audit={'terminal_check_request': legacy['check_request'],
                        'no_gain_proof': legacy['audio_gain_termination']},
        prior=rejected, clip_context=rig.context, entity_verifier=rig.verifier, judge_llm_call=judge)
    assert len(calls) <= 1  # one new question version; an identical cached answer is reused
    assert retried.get('keep_route') == KEEP_B, retried.get('reason_code')
    assert retried['terminal_review']['request']['terminal_contract_version'] == current_version
    link = retried['superseded_contract_answer']
    assert link['contract_retry']['kind'] == 'REASK_UNDER_CURRENT_CONTRACT'
    assert link['original_raw_sha256'] == sha(raw) and link['original_prompt_sha256'] == sha(legacy_prompt)
    assert is_release_resolved_keep({**retried['rebuilt_finding'], 'exact_release_adjudication': retried})


def test_current_contract_bad_answer_is_not_reasked(monkeypatch, terminal_rig):
    monkeypatch.delenv('AUTOSLICE_BASE', raising=False)
    calls = []
    def reply(prompt):
        calls.append(prompt)
        return json.dumps({**answer(), 'evidence_refs': ['invented-ref']}, ensure_ascii=False)
    first = run_terminal([unresolved()], reply, terminal_rig=terminal_rig)
    second = run_terminal(first['findings'], reply, terminal_rig=terminal_rig)
    assert len(calls) == 1
    view = second['findings'][0]['lifecycle']
    assert view['state'] == 'CONTRACT_ERROR'
    assert view['recovery'] == 'REVALIDATE_ORIGINAL_ANSWER_AFTER_CONSUMER_REPAIR'
    assert view['contract_retry']['reask_under_current_contract'] is False


def _as_older_contract_rows(rows):
    output = []
    for row in rows:
        row = deepcopy(row)
        audit = row['exact_release_adjudication']
        legacy, prompt = older_contract(audit['terminal_review']['request'])
        audit['terminal_review'] = review_of(legacy, prompt, audit['terminal_review']['raw_completion'])
        row.pop('lifecycle', None)
        output.append(row)
    return output


def test_old_contract_unproven_d_is_reasked_once_and_proven_d_stays_content(monkeypatch, terminal_rig):
    monkeypatch.delenv('AUTOSLICE_BASE', raising=False)
    unproven = {**answer('UNRESOLVABLE'), 'comparison_text': '可能说我不喜欢她，也可能喜欢；不能据此确定。'}
    first = run_terminal([unresolved()], lambda _: json.dumps(unproven, ensure_ascii=False),
                         terminal_rig=terminal_rig)
    assert first['findings'][0]['lifecycle']['state'] == 'CONTRACT_ERROR'
    older = _as_older_contract_rows(first['findings'])
    view = lifecycle.classify(older[0], terminal=True)
    assert view['state'] == 'CONTRACT_ERROR' and view['recovery'] == 'REASK_UNDER_CURRENT_CONTRACT'
    calls = []
    def reply(prompt):
        calls.append(prompt)
        return json.dumps(answer(), ensure_ascii=False)
    retried = run_terminal(older, reply, terminal_rig=terminal_rig)
    assert len(calls) == 1
    assert retried['findings'] == [], [r['lifecycle'] for r in retried['findings']]
    kept = retried['unresolved_findings_disclosed'][0]
    assert kept['lifecycle']['state'] == 'KEEP'
    assert kept['exact_release_adjudication']['superseded_contract_answer']['contract_retry'][
        'recorded_contract_version'] == 1
    # Control: a D with written-meaning proof is a genuine content block; an
    # older contract does not make it retryable and the G gate is unchanged.
    row = {**unresolved(), 'suspect': '喜欢她啊', 'suggestion': '不喜欢她', 'proposed_full_cue': '我不喜欢她'}
    blocked = run_terminal([row], lambda _: json.dumps(answer('UNRESOLVABLE'), ensure_ascii=False),
                           terminal_rig=terminal_rig)
    assert blocked['findings'][0]['lifecycle']['state'] == 'TRUE_BLOCK'
    older_blocked = _as_older_contract_rows(blocked['findings'])
    def never(_):
        raise AssertionError('PROVEN_D_MUST_NOT_BE_REASKED')
    again = run_terminal(older_blocked, never, terminal_rig=terminal_rig)
    assert again['findings'][0]['lifecycle']['state'] == 'TRUE_BLOCK'
    assert again['reason_codes'] == [lifecycle.CONTENT_BLOCK]


def _conflicting_wide_rig(tmp_path, monkeypatch, current):
    from tests import test_targeted_relisten_public_contract as protocol
    source = protocol.SRT.replace('我听到了，呜呜', current)
    finding = {**protocol.FINDING, 'suspect': current, 'suggestion': current + '呀',
               'proposed_full_cue': current + '呀'}
    monkeypatch.setattr(protocol, 'SRT', source)
    return protocol.build_offline_rig(tmp_path, monkeypatch, srt=source, finding=finding)


def test_clear_audio_guard_reason_surfaces_instead_of_meaning_contract(tmp_path, monkeypatch):
    """cue36 (b): the guard's own failure must not pose as another code."""
    rig = _conflicting_wide_rig(tmp_path, monkeypatch, '我没听到了')
    # Two independent wide windows (the A threshold) both heard the opposite polarity.
    audit, captured = produce(rig, c_reply, wide=('我听到了', '我听到了'))
    assert captured['data']['clear_audio_constraints'], captured['data']['observations']
    assert audit.get('reason_code') == 'EDITORIAL_CLEAR_AUDIO_CONFLICT', audit.get('reason_code')
    assert audit.get('failure_layer') == 'CONTRACT_ERROR'
    view = lifecycle.classify({**rig.finding, 'exact_release_adjudication': audit}, terminal=True)
    assert view['state'] == 'CONTRACT_ERROR'
    assert view['detail']['reason_code'] == 'EDITORIAL_CLEAR_AUDIO_CONFLICT'
    assert lifecycle.release_reason_codes([{**rig.finding, 'exact_release_adjudication': audit}],
                                          terminal=True) == [lifecycle.ENGINEERING_FAILURE]


def test_single_narrow_window_is_not_a_clear_audio_veto_for_an_older_contract_keep(rig):
    """cue36 (a): one definition of clear audio for every contract version's guard."""
    from src.autoslice.semantic_terminal_adjudication import keep_from_terminal
    from src.autoslice.targeted_relisten import terminal_clear_audio_catalogue
    from src.autoslice.semantic_terminal_adjudication import terminal_observation_projection
    # A single bound narrow ASR reading conflicting in polarity with the cue.
    audit, captured = produce(rig, c_reply, native=('你没听到了，呜呜',))
    assert audit.get('policy_branch') == 'SUB18_CONTEXT_MEANING_EQUIVALENT_KEEP', audit.get('reason_code')
    legacy, legacy_prompt = older_contract(captured['request'])
    projection = terminal_observation_projection(legacy)
    # The frozen historical prompt still renders its original (unqualified)
    # catalogue for byte binding; the guard reads only the qualified view.
    assert terminal_clear_audio_catalogue(projection, current_text=legacy['current_cue'])
    assert not terminal_clear_audio_catalogue(projection, current_text=legacy['current_cue'],
        qualification_policy=legacy['terminal_policy'])
    raw = json.dumps(captured['answer'], ensure_ascii=False)
    keep = keep_from_terminal(srt_text=rig.srt, finding=rig.finding, request=legacy,
        answer=json.loads(raw), prompt=legacy_prompt, raw=raw)
    assert keep['policy_branch'] == 'SUB18_CONTEXT_MEANING_EQUIVALENT_KEEP'


def test_saved_keep_failing_cold_revalidation_reports_guard_reason(tmp_path, monkeypatch):
    from src.autoslice import targeted_relisten as relisten
    rig = _conflicting_wide_rig(tmp_path, monkeypatch, '我听到了')
    audit, _ = produce(rig, c_reply, wide=('我听到了', '我听到了'))
    row = {**audit['rebuilt_finding'], 'exact_release_adjudication': audit}
    assert lifecycle.classify(row, terminal=True)['state'] == 'KEEP'
    # A later consumer that would veto the saved keep must name its own reason.
    real = relisten.protect_clear_transcript
    def stricter(*args, **kwargs):
        raise ValueError('EDITORIAL_CLEAR_AUDIO_CONFLICT')
    monkeypatch.setattr(relisten, 'protect_clear_transcript', stricter)
    view = lifecycle.classify(row, terminal=True)
    assert view['state'] == 'CONTRACT_ERROR'
    assert view['detail']['validation_error'] == 'EDITORIAL_CLEAR_AUDIO_CONFLICT'
    monkeypatch.setattr(relisten, 'protect_clear_transcript', real)
    assert lifecycle.classify(row, terminal=True)['state'] == 'KEEP'
