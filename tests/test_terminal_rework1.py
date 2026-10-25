from copy import deepcopy
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from tests.test_terminal_contract_roundtrip import produce, c_reply
from tests.test_targeted_relisten_public_contract import rig  # noqa: F401


def test_name_mention_does_not_invent_a_name_slot():
    from src.autoslice.meaning_difference import classify_difference
    from src.autoslice.subtitle_acceptability_policy import load_policy
    from src.autoslice.semantic_terminal_adjudication import name_only_difference
    diff = classify_difference('真也不许跟别人说话', '睁眼，不许跟别人说话',
        glossary_terms=['周也'], policy=load_policy())
    assert 'proper_name' not in diff['features']
    assert not name_only_difference(diff, {'reason': '不能据此认定人名', 'name_only': False})
    sourced = classify_difference('周也来了', '周野来了', glossary_terms=['周也'], policy=load_policy())
    assert name_only_difference(sourced, {'reason': '字形待核', 'name_only': False})
    assert name_only_difference(diff, {'reason': '明确姓名槽', 'name_only': True})


def test_real_prompts_share_materials_without_cross_question_evidence(rig):
    from scripts.predeploy_contract_gauntlet import batch_terminal_prompt
    _, one = produce(rig, c_reply)
    rig.finding['why'] = '同一来源的另一条提名，保留自己的停止证据'
    _, two = produce(rig, c_reply)
    prompts = {'one': one['prompt'], 'two': two['prompt']}
    batch = batch_terminal_prompt(prompts)
    data = json.loads(batch.split('\n', 1)[1])
    assert data['shared']['materials']['policy']
    for key, question in data['questions'].items():
        materials = {**data['shared']['materials'], **question['materials']}
        refs = {**data['shared']['citations'], **question['citations']}
        restored = (question['instructions'] + '\n' + question['citation_label'] + '：'
            + json.dumps(refs, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
            + '\n' + json.dumps(materials, ensure_ascii=False, sort_keys=True, separators=(',', ':')))
        assert restored == prompts[key]
    assert len(batch) < sum(map(len, prompts.values()))


def test_context_projection_uses_g6_source_clock_and_never_drops_large_facts(rig):
    from src.autoslice.audio_question_identity import physical_target
    from src.autoslice.clip_context import _canonical_json_sha256
    _, initial = produce(rig, c_reply)
    check = initial['request']['check_request']
    start, end = physical_target(check)
    rig.context['source_human_evidence'] = {'policy': 'synthetic scoped context, no mutation authority',
        'entries': [
            {'truth_id': 'target', 'local_windows': [{'start_ms': start, 'end_ms': end}],
             'source_evidence': {'raw_excerpt': '只约束本目标的语境，不是新的听音'}},
            {'truth_id': 'other', 'local_windows': [{'start_ms': end + 1, 'end_ms': end + 10}],
             'source_evidence': {'raw_excerpt': '另一物理问题，不得扩大槽位'}}]}
    rig.context['source_fact'] = '有来源的长语境材料' * 600
    payload = {k: v for k, v in rig.context.items() if k != 'context_sha256'}
    rig.context['context_sha256'] = _canonical_json_sha256(payload)
    _, captured = produce(rig, c_reply)
    context = captured['data']['whole_session_and_session_context']
    assert context['source_fact'] == rig.context['source_fact']
    assert [row['truth_id'] for row in context['source_human_evidence']['entries']] == ['target']
    assert 'structured_chat' not in context  # It has the ordinary events projection.


def test_record_failure_reaches_cli_exit_and_content_block_stays_distinct(rig, monkeypatch):
    import sys,os
    import scripts.predeploy_contract_gauntlet as gate
    from src.autoslice.review_evidence_store import record_metrics,validate_record_metrics
    audit,_ = produce(rig,c_reply)
    metrics=record_metrics({'findings':[audit]})
    metrics['max_finding_bytes']=100_001
    with pytest.raises(ValueError) as error:
        validate_record_metrics(metrics)
    failed={'status':'REJECTED','reason':str(error.value)}
    assert gate.replay_exit_code([failed])==1
    assert gate.replay_exit_code([{'status':'ANSWER_UNAVAILABLE'}])==1
    assert gate.replay_exit_code([{'status':'REJECTED','reason':'TERMINAL_UNRESOLVABLE'}])==0
    monkeypatch.setenv('AUTOSLICE_BASE',str(rig.tmp))
    for phase,callback,bad,good in (
        ('worker','worker',gate.replay_exit_code([failed]),0),
        ('prepare','prepare_cases',[{'status':'PREPARE_REJECTED'}],[{'status':'PREPARED'}]),
        ('legacy','legacy_guard',[{'matches':False}],[{'matches':True}])):
        monkeypatch.setattr(sys,'argv',['gauntlet','--phase',phase,'--state',str(rig.tmp/'cli'),
            '--evidence',str(rig.tmp)])
        def reply(args,value):
            assert os.environ['AUTOSLICE_BASE']==str(args.production_root)
            return value
        monkeypatch.setattr(gate,callback,lambda args,value=bad:reply(args,value))
        assert gate.main()==1
        monkeypatch.setattr(gate,callback,lambda args,value=good:reply(args,value))
        assert gate.main()==0


def test_real_rendered_edit_contract_roundtrip_and_invalid_bounds(rig):
    from src.autoslice.editorial_subtitle import _decision
    from src.autoslice.editorial_edit_contract import apply_edit
    def reply(data, refs):
        contract = data['edit_contract']
        assert contract['instructions'] and contract['base_text'] == data['current']
        return {'choice': 'CONTEXT_FIT', 'grade': 'B', 'keep_route': None,
            'evidence_decisive': True, 'reason': '同一普通观察支持完整保留，修复尾音呈现',
            'comparison_text': '我听到了，哼唱', 'status': 'RESOLVED', 'mode': 'faithful_paraphrase',
            'edit_start': contract['minimum_start'], 'edit_end': contract['length'],
            'replacement': '我听到了，哼唱', 'reliable_fragments': [{'text': '我听到了', 'basis': '原音'}],
            'evidence_refs': ['audio'], 'meaning_basis': '上下文说明声音',
            'literal_uncertainty': '尾音', 'no_new_facts': True}
    audit, captured = produce(rig, reply)
    assert audit.get('repaired') is True, audit.get('reason_code')
    assert _decision(captured['request'], captured['answer']) == '我听到了，哼唱'
    assert captured['response_schema']['properties']['edit_end']['maximum'] == len(captured['data']['current'])
    for bounds in [(0, 20), (True, 2), (2, 2), (-1, 2)]:
        with pytest.raises(ValueError, match='EDITORIAL_EDIT_INVALID'):
            apply_edit(captured['data']['current'], {**captured['answer'], 'edit_start': bounds[0], 'edit_end': bounds[1]})
    assert apply_edit('甲😀，乙', {'edit_start': 1, 'edit_end': 2, 'replacement': '😃'}) == '甲😃，乙'


def test_g6_duplicate_questions_call_once_and_cold_consume_all_rows(rig):
    from src.autoslice.exact_final_terminal_funnel import terminalize_review, review_pass_receipt
    from src.autoslice.final_review_contract import is_release_resolved_keep
    _, captured = produce(rig, c_reply)
    finding = captured['request']['audio_gain_termination']['stopped_finding']
    other = {**deepcopy(finding), 'why': '同窗另一提名，措辞不同'}
    audit = {'findings': [finding, other], 'unresolved_findings_disclosed': [], 'reason_codes': ['FINAL_REVIEW_UNRESOLVED_FINDINGS']}
    receipt = review_pass_receipt(pass_index=1, srt_text=rig.srt, audit=audit, repaired_text=rig.srt, repairs=[])
    calls = []
    def call(prompt):
        calls.append(prompt)
        data = json.loads(prompt.rsplit('\n', 1)[1])
        refs = json.loads(next(x.split('：', 1)[1] for x in prompt.splitlines() if x.startswith('可引用编号')))
        assert len(data['terminal_group_questions']) == 2
        return json.dumps(c_reply(data, refs), ensure_ascii=False)
    result = terminalize_review(rig.srt, audit, pass_receipts=[receipt], adapters={
        'clip_context': rig.context, 'source_media_timeline_offset_ms': rig.offset_ms,
        'prior_range_acoustic_observations': captured['request']['check_request'].get('context_only_range_acoustic_observations', []),
        'judge_llm_call': call})
    assert len(calls) == 1
    assert len(result['unresolved_findings_disclosed']) == 2, result.get('findings')
    assert all(is_release_resolved_keep(row) for row in result['unresolved_findings_disclosed'])
    assert result['unresolved_findings_disclosed'][0]['exact_release_adjudication'] == result['unresolved_findings_disclosed'][1]['exact_release_adjudication']
    failed_calls = []
    def fail(prompt):
        failed_calls.append(prompt)
        # This exercises duplicate fan-out after a permanent owner failure.
        # Transport failures now retry under their separate wall-clock contract.
        raise RuntimeError('synthetic terminal owner failure')
    failed = terminalize_review(rig.srt, audit, pass_receipts=[receipt], adapters={
        'clip_context': rig.context, 'source_media_timeline_offset_ms': rig.offset_ms,
        'judge_llm_call': fail})
    assert len(failed_calls) == 1
    assert len(failed['findings']) == 2


def test_historical_native_receipts_survive_terminal_projection_under_own_requests(rig):
    from src.autoslice.exact_final_terminal_funnel import terminal_check
    from src.autoslice.targeted_relisten import evidence_projection
    _, captured = produce(rig, c_reply, native=True)
    check = captured['request']['check_request']
    # Ordinary request producer already bound two independent native receipts.
    historical = {'exact_release_adjudication': {'request': check}, **rig.finding}
    current = terminal_check(rig.srt, historical, clip_context=rig.context, offset_ms=rig.offset_ms,
        ranges=check.get('context_only_range_acoustic_observations', []))
    view = evidence_projection(current, include_target_text=True)
    initial = evidence_projection(check, include_target_text=True)
    assert {row['observation_id'] for row in view['observations'] if row['receipt_kind'] == 'native_transcript'} == {
        row['observation_id'] for row in initial['observations'] if row['receipt_kind'] == 'native_transcript'}
    assert any(row['receipt_kind'] == 'native_transcript' for row in view['observations'])
    changed = deepcopy(check)
    changed['clip_context_binding']['source_media_sha256s'] = ['0' * 64]
    negative = terminal_check(rig.srt, {**rig.finding, 'exact_release_adjudication': {'request': changed}},
        clip_context=rig.context, offset_ms=rig.offset_ms)
    assert not negative['prior_acoustic_observations']


def test_real_keep_content_addressed_roundtrip_parallel_writer_and_tamper(rig):
    from src.autoslice.final_review_contract import is_release_resolved_keep
    from src.autoslice.review_evidence_store import persist_review, expand, record_metrics, validate_record_metrics
    def supported(data, refs):
        return {**c_reply(data, refs), 'grade': 'B', 'keep_route': 'B_EVIDENCE_SUPPORTS_CURRENT',
            'evidence_decisive': True, 'comparison_text': data['current'],
            'evidence_refs': data['current_support_observation_refs']}
    audit, captured = produce(rig, supported, native=True)
    row = {**audit['rebuilt_finding'], 'exact_release_adjudication': audit}
    from src.autoslice.semantic_terminal_adjudication import terminal_keep_matches
    assert terminal_keep_matches(row, audit)
    assert is_release_resolved_keep(row)
    whole = {'schema_version': 'final-review-audit.v2', 'findings': [], 'unresolved_findings_disclosed': [row],
        'terminal_funnel': {'attempts': [deepcopy(audit['terminal_review'])] * 3}}
    path = rig.tmp / 'store-run' / 'review-flags.json'
    with ThreadPoolExecutor(max_workers=4) as pool:
        writes = list(pool.map(lambda _: persist_review(path, whole), range(4)))
    assert all(value == writes[0] for value in writes)
    packed = json.loads(path.read_text())
    from src.autoslice.review_evidence_store import persist_document
    from src.autoslice.talk_lane import _read_final_review_surface
    reviewed = {'schema_version': 'final-review-audit.v2', **whole}
    persist_review(path, reviewed)
    chat_path = path.with_name('chat-authority.json')
    persist_document(chat_path, {'final_review_audit': reviewed, 'other_owner': {'status': 'UNCHANGED'}})
    direct, raw_sha = _read_final_review_surface(path)
    chat, _ = _read_final_review_surface(chat_path)
    assert direct == chat['final_review_audit'] == reviewed
    import hashlib
    assert raw_sha == 'sha256:' + hashlib.sha256(path.read_bytes()).hexdigest()
    assert chat['other_owner'] == {'status': 'UNCHANGED'}
    packed = json.loads(path.read_text())
    from scripts.predeploy_contract_gauntlet import original_cases
    evidence = rig.tmp / 'preflight-evidence'
    source_path = evidence / 'group' / 'clip.review-flags.json'
    source_audit = {'schema_version': 'final-review-audit.v2', 'findings': [row]}
    persist_review(source_path, source_audit)
    cases = list(original_cases(evidence))
    assert len(cases) == 1 and cases[0]['audit'] == source_audit
    assert cases[0]['old_review']['raw_completion'] == audit['terminal_review']['raw_completion']
    assert cases[0]['audit_sha256'] == hashlib.sha256(source_path.read_bytes()).hexdigest()
    assert expand(packed) == whole
    import shutil
    from src.autoslice.review_evidence_store import load_review
    archive = rig.tmp / 'relocated-run'
    shutil.copytree(path.parent, archive)
    assert (archive / path.name).read_bytes() == path.read_bytes()
    assert load_review(archive / path.name) == whole
    assert is_release_resolved_keep(packed['unresolved_findings_disclosed'][0])
    validate_record_metrics(record_metrics(packed, prompts=[captured['prompt']]))
    assert is_release_resolved_keep(row)  # Inline legacy representation still reads.
    blob = next((path.parent / 'evidence-store/blobs').iterdir())
    blob.write_bytes(b'{}')
    with pytest.raises(ValueError, match='ARTIFACT_SHA_MISMATCH'):
        expand(packed)


def test_original_v1_producer_receipt_remains_cold_valid_after_reference_roundtrip(rig, monkeypatch):
    import ast
    import subprocess
    from src.autoslice import editorial_subtitle as editorial
    from src.autoslice import semantic_terminal_adjudication as terminal
    from src.autoslice import terminal_legacy_v1 as legacy
    from src.autoslice.final_review_contract import is_release_resolved_keep
    from src.autoslice.review_evidence_store import persist_review, load_review
    # HEAD advances after integration. The producer under test is the actual
    # deployed v1 artifact, not whichever new producer happens to be HEAD.
    source = subprocess.check_output(['git', 'show',
        '9e28f907280467091d81d9f9b39c09aded5b8e85:src/autoslice/editorial_subtitle.py'], text=True)
    original = next(n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name == 'adjudicate')
    namespace = dict(vars(editorial))
    exec(compile(ast.Module(body=[original], type_ignores=[]), '<pinned-v1-editorial-producer>', 'exec'), namespace)
    def supported(data, refs):
        return {**c_reply(data, refs), 'grade': 'B', 'keep_route': 'B_EVIDENCE_SUPPORTS_CURRENT',
            'evidence_decisive': True, 'comparison_text': data['current'],
            'evidence_refs': data['current_support_observation_refs']}
    with monkeypatch.context() as old:
        old.setattr(editorial, 'adjudicate', namespace['adjudicate'])
        old.setattr(terminal, 'terminal_prompt', legacy.terminal_prompt)
        old.setattr(terminal, 'terminal_materials', legacy.terminal_materials)
        audit, captured = produce(rig, supported, native=True)
    assert 'terminal_contract_version' not in captured['request']
    row = {**audit['rebuilt_finding'], 'exact_release_adjudication': audit}
    assert is_release_resolved_keep(row)
    path = rig.tmp / 'legacy-run' / 'review-flags.json'
    persist_review(path, {'findings': [], 'unresolved_findings_disclosed': [row]})
    assert is_release_resolved_keep(load_review(path)['unresolved_findings_disclosed'][0])
    forged = deepcopy(load_review(path)['unresolved_findings_disclosed'][0])
    review = forged['exact_release_adjudication']['terminal_review']
    prefix, data = review['prompt'].rsplit('\n', 1)
    materials = json.loads(data)
    materials['current'] += '伪造事实'
    review['prompt'] = prefix + '\n' + json.dumps(materials, ensure_ascii=False, sort_keys=True)
    import hashlib
    review['prompt_sha256'] = hashlib.sha256(review['prompt'].encode()).hexdigest()
    assert not is_release_resolved_keep(forged)
