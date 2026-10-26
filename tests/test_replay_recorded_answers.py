"""固定录音回答夹具的离线 CLI 回归；不使用生产输入或网络。"""
import json
from pathlib import Path
import subprocess
import sys

from scripts.replay_recorded_answers import offline_guard, validate_case


ROOT = Path(__file__).resolve().parents[1]


def test_cli_fixed_answer_summary(tmp_path):
    exchange = tmp_path / 'sister-28919589' / 'cpa-text-exchanges' / 'request-fixed'
    exchange.mkdir(parents=True)
    (exchange / 'prompt.txt').write_text('你在校对李豆沙直播切片字幕。\n固定夹具\n')
    (exchange / 'completion.txt').write_text('{"is_read_aloud":false,"confidence":0.9,"reason":"回答而非照念"}')
    output = tmp_path / 'result.json'
    process = subprocess.run([sys.executable, str(ROOT/'scripts/replay_recorded_answers.py'),
                              str(tmp_path), '-o', str(output)], cwd=ROOT, text=True, capture_output=True)
    assert process.returncode == 0, process.stderr
    result = json.loads(output.read_text())
    assert result['schema_version'] == 'recorded-answer-replay.v1'
    assert result['unique_answers'] == result['physical_exchanges'] == 1
    assert result['summary'] == [{'kind':'念弹幕','status':'ACCEPT','code':'PASS','count':1}]


def test_reuses_closed_set_consumer_and_preserves_rejection():
    prompt = '# 字幕文字闭集裁决\n## 闭集候选\n[{"canonical":"甲"},{"canonical":"乙"}]\n'
    good = validate_case({'prompt':prompt,'raw':'{"ranking":[{"canonical":"甲","p":0.6},{"canonical":"乙","p":0.4}],"choice":"乙"}'})
    assert good['status'] == 'ACCEPT'
    assert good['choice'] == '甲'  # 原 consumer 按最高概率纠正 choice。
    bad = validate_case({'prompt':prompt,'raw':'{"ranking":[{"canonical":"甲","p":0.6}],"choice":"甲"}'})
    assert bad['status'] == 'REJECT'
    assert bad['code'] == 'REPLAY_CONSUMER_RETURNED_NONE'


def test_offline_guard_rejects_network_and_process_events():
    import pytest
    for event in ['socket.connect','socket.getaddrinfo','subprocess.Popen','os.system']:
        with pytest.raises(RuntimeError, match='REPLAY_NETWORK_OR_PROCESS_FORBIDDEN'):
            offline_guard(event, ())


def test_word_choice_reuses_support_contract():
    prompt = '## 声音证据的判断次序\n# 字幕选字裁决（闭集）\n- CURRENT（现字幕整句）: 甲\n- PROPOSED（提案整句）: 乙\n## exact-final CURRENT 整句闭合要求\n'
    result = validate_case({'prompt':prompt,'raw':'{"choice":"CURRENT","needs_audio":false,"reason":"保留"}'})
    assert result['status'] == 'REJECT'
    assert result['code'] == 'CURRENT_UTTERANCE_SUPPORT_REQUIRED'


def test_missing_original_artifact_is_not_a_false_validator_bug(tmp_path, monkeypatch):
    from scripts import replay_recorded_answers as replay_module
    def needs_original(case, answer):
        raise ValueError('FINAL_STOP_OBSERVATION_DRIFT')
    monkeypatch.setattr(replay_module,'_terminal',needs_original)
    result=validate_case({'prompt':'# 字幕材料穷尽终裁 v1\n','raw':'{}',
                          'record':{'audio_path':str(tmp_path/'missing.wav')}})
    assert result['status']=='INCOMPLETE'
    assert result['native_rejection']=='FINAL_STOP_OBSERVATION_DRIFT'
    assert result['missing_artifact_count']==1


def test_discovers_bare_review_flags_snapshot(tmp_path):
    from scripts.replay_recorded_answers import replay
    run=tmp_path/'sister-9e28f907';run.mkdir()
    snapshot=run/'review-flags.json'
    snapshot.write_text('{"findings":[]}')
    result=replay([tmp_path])
    assert len(result['audit_checks'])==1
    assert result['audit_checks'][0]['path']==str(snapshot)


def test_duplicate_group_reuses_original_and_preserves_current_consumer(tmp_path):
    from scripts.replay_recorded_answers import replay_ruling_reuse
    identity = {'transport': 'recorded', 'models': ['offline'], 'effort': 'high'}
    for i, answer in enumerate(['甲', '乙', '乙']):
        exchange = tmp_path / f'sister-{i:08x}' / 'cpa-text-exchanges' / f'request-{i}'
        exchange.mkdir(parents=True)
        (exchange/'prompt.txt').write_text('# 字幕文字闭集裁决\n## 闭集候选\n[{"canonical":"甲"},{"canonical":"乙"}]\n')
        (exchange/'completion.txt').write_text(json.dumps({'choice': answer,
            'ranking': [{'canonical': answer, 'p': 1}, {'canonical': '乙' if answer == '甲' else '甲', 'p': 0}]}))
        (exchange/'request.json').write_text(json.dumps({'model_identity': identity}))
    result = replay_ruling_reuse([tmp_path], tmp_path/'cache')
    assert result['duplicate_groups'] == result['new_questions'] == 1
    assert result['cache_hits'] == 2 and result['all_consumer_matches_original']
    assert result['recorded_flip_groups'] == 1 and result['reused_flip_groups'] == 0


def test_unsupported_answer_cannot_count_as_successful_revalidation(tmp_path):
    from scripts.replay_recorded_answers import replay_ruling_reuse
    for i in range(2):
        exchange = tmp_path / 'cpa-text-exchanges' / f'request-{i}'
        exchange.mkdir(parents=True)
        (exchange/'prompt.txt').write_text('unsupported question')
        (exchange/'completion.txt').write_text('{}')
        (exchange/'request.json').write_text(json.dumps({'model_identity': {
            'transport': 'recorded', 'models': ['offline'], 'effort': 'high'}}))
    result = replay_ruling_reuse([tmp_path], tmp_path/'cache')
    assert result['new_questions'] == 1 and result['all_one_question']
    assert result['complete_consumer_groups'] == 0
    assert result['all_consumer_matches_original'] is False


def test_additional_recorded_types_use_actual_consumer_contracts():
    from src.autoslice import pronoun_consistency as pronoun
    _, occurrences = pronoun._occurrences('1\n00:00:00,000 --> 00:00:01,000\n她来了\n')
    prompt = pronoun._PROMPT.format(policy_text='', candidate_context_text='',
        speaker_attribution_text='', numbered_srt='1. 她来了', occurrences_json=json.dumps(occurrences))
    raw = {'decisions': [{'occurrence_id': occurrences[0]['occurrence_id'], 'action': 'KEEP_CURRENT',
        'current_token': '她', 'replacement_token': '她', 'referent': 'HOST', 'reason': '原答'}]}
    assert validate_case({'prompt': prompt, 'raw': json.dumps(raw)})['status'] == 'ACCEPT'
    raw['decisions'] = []
    assert validate_case({'prompt': prompt, 'raw': json.dumps(raw)})['code'] == 'CANDIDATE_PRONOUN_DECISION_COVERAGE_INVALID'
    prompt = '你是李豆沙切片的终审审片员\n绑定原材料 JSON'
    assert validate_case({'prompt': prompt, 'raw': '{"findings":[]}'})['status'] == 'ACCEPT'
    assert validate_case({'prompt': prompt, 'raw': '{"findings":{}}'})['code'] == 'FINAL_REVIEW_RESPONSE_FINDINGS_INVALID'


def test_boundary_replays_real_saved_grids_without_creating_release_authority(tmp_path):
    from scripts.replay_recorded_answers import replay_ruling_reuse
    from src.autoslice.boundary_semantic_review import review_talk_boundary_semantics
    from tests.test_boundary_semantic_review import _completed_boundary_fixture
    kwargs, call, prompts = _completed_boundary_fixture(tmp_path)
    original = review_talk_boundary_semantics(**kwargs)
    raw = call(prompts[0])
    for i in range(2):
        root = tmp_path / f'run-{i}'
        exchange = root / 'cpa-text-exchanges' / 'request-fixed'
        exchange.mkdir(parents=True)
        (root/'original.srt').write_text('1\n00:00:00,000 --> 00:00:09,000\n当前完整文字\n')
        (exchange/'prompt.txt').write_text(prompts[0])
        (exchange/'completion.txt').write_text(raw)
        (exchange/'request.json').write_text(json.dumps({'model_identity': call.cpa_cache_identity}))
    result = replay_ruling_reuse([tmp_path], tmp_path/'replay-cache')
    assert result['complete_consumer_groups'] == 1 and result['all_consumer_matches_original']
    group = result['groups'][0]
    assert group['new_questions'] == group['cache_hits'] == 1
    outcome = group['original_outcome']
    assert outcome['boundary_decision']['status'] == original['status'] == 'BLOCK'
    assert outcome['release_action'] == 'NO_RELEASE_AUTHORITY'
    assert len(group['revalidations']) == 2


def _convergence_case(answer):
    from scripts.replay_recorded_answers import sha
    prompt = ('# 字幕缺失候选最终收敛（CPA 文字候选裁决）\n'
        '- CURRENT: 我说好吧\n- SUSPECT: 好吧\n- CUE_INDEX: 1\n'
        f'- CURRENT_CUE_SHA256: {sha("我说好吧")}\n'
        f'- FINAL_SRT_SHA256: {"1" * 64}\n- CONTEXT_SHA256: {"2" * 64}\n')
    return {'prompt': prompt, 'raw': json.dumps(answer)}


def test_convergence_replays_native_keep_and_preserves_unresolved():
    kept = validate_case(_convergence_case({'decision': 'KEEP_EXISTING',
        'replacement_text': None, 'needs_audio': False, 'reason': '原答保留'}))
    assert kept['status'] == 'ACCEPT'
    assert kept['native_status'] == 'RESOLVED'
    assert kept['release_action'] == 'NO_RELEASE_AUTHORITY'
    needs_audio = validate_case(_convergence_case({'decision': 'KEEP_EXISTING',
        'replacement_text': '', 'needs_audio': True, 'reason': '还需原音'}))
    assert needs_audio['native_status'] == 'UNRESOLVED'
    assert needs_audio['native_reason'] == 'CPA_CONVERGENCE_KEEP_REQUIRES_AUDIO'
    unresolved = validate_case(_convergence_case({'decision': 'UNRESOLVED',
        'replacement_text': '', 'needs_audio': True, 'reason': '无法判断'}))
    assert unresolved['native_reason'] == 'CPA_CONVERGENCE_UNRESOLVED'
    invalid = validate_case(_convergence_case({'decision': 'UNKNOWN', 'reason': '未知路由'}))
    assert invalid['status'] == 'REJECT'
    assert invalid['code'] == 'CPA_CONVERGENCE_DECISION_CONTRACT_INVALID'


def test_convergence_replacement_cannot_bypass_missing_original_evidence():
    case = _convergence_case({'decision': 'REPLACE_WITH_EXACT_TEXT',
        'replacement_text': '我说好的', 'needs_audio': True, 'reason': '候选'})
    result = validate_case(case)
    assert result['status'] == 'INCOMPLETE'
    assert result['detail'] == 'REPLAY_ORIGINAL_CONVERGENCE_PHONEMES_UNAVAILABLE'
    case['prompt'] += ('## 音素约束及证据等级\n{}\n'
        '## 整片主题线索与宽窗转写（数据，只作语境，非逐字真值）\n语境\n'
        '## 提案召回与最终确认的边界\n'
        '## 邻近结构化消息链（按时间顺序，只作语境）\n（无）\n'
        '## 三路结构化保真证据\n{}\n')
    # No-op replacements are rejected by the ordinary text contract.
    case['raw'] = json.dumps({'decision': 'REPLACE_WITH_EXACT_TEXT',
        'replacement_text': '我说好吧', 'needs_audio': True, 'reason': '不改字'})
    assert validate_case(case)['code'] == 'CPA_CONVERGENCE_TEXT_CONTRACT_INVALID'
    case['raw'] = json.dumps({'decision': 'REPLACE_WITH_EXACT_TEXT',
        'replacement_text': '我说好的', 'needs_audio': True, 'reason': '候选',
        'proposal_basis': 'topic_homophone', 'topic_support': ['原语境没有的引用']})
    assert validate_case(case)['code'] == 'TOPIC_PROPOSAL_SUPPORT_MISSING'


def test_draft_review_uses_complete_native_cue_contract():
    prompt = ('\n【字幕分段也是语义检查，不限词表专名】\n'
        '你在校对李豆沙(B站虚拟主播)直播切片的字幕草稿。\n'
        '字幕草稿(每行:[时间] 编号. 文本):\n'
        '[00:00:00] 1. 甲\n[00:00:01] 2. 乙\n'
        '\n只输出一个 JSON 对象')
    good = {'prompt': prompt, 'raw': '{"cues":[{"n":1,"text":"甲"},{"n":2,"text":""}]}'}
    assert validate_case(good)['cue_count'] == 2
    for raw in ['{"cues":[]}', '{"cues":[{"n":1,"text":"甲"},{"n":1,"text":"乙"}]}']:
        result = validate_case({**good, 'raw': raw})
        assert result['status'] == 'REJECT'
        assert result['code'] == 'CPA_CORRECTION_INVALID_OUTPUT'
    missing = validate_case({**good, 'prompt': prompt.replace('[00:00:01] 2.', '[00:00:01] 3.')})
    assert missing['status'] == 'INCOMPLETE'


def test_final_pronoun_replays_native_application_on_original_saved_srt(tmp_path):
    from src.autoslice.pronoun_stage_trace import apply_pronoun_pass
    srt = '1\n00:00:00,000 --> 00:00:01,000\nTA来了\n'
    raw = '{"rewrites":[{"n":1,"occurrence":1,"from":"TA","to":"她"}]}'
    prompts = []
    def original(prompt):
        prompts.append(prompt)
        return raw
    revised = apply_pronoun_pass(srt, cpa_llm_call=original, display_name='李豆沙', required=True)
    (tmp_path/'source.srt').write_text(srt)
    path = tmp_path/'cpa-text-exchanges'/'request-fixed'/'completion.txt'
    case = {'prompt': prompts[0], 'raw': raw, 'path': str(path)}
    result = validate_case(case)
    assert result['status'] == 'ACCEPT'
    from scripts.replay_recorded_answers import sha
    assert result['saved_inputs'][0]['result_srt_sha256'] == sha(revised)
    bad = {**case, 'raw': raw.replace('"occurrence":1', '"occurrence":2')}
    assert validate_case(bad)['code'] == 'CPA_PRONOUN_INVALID_OUTPUT'
    missing = {**case, 'path': str(tmp_path/'absent'/'cpa-text-exchanges'/'request'/'completion.txt')}
    assert validate_case(missing)['status'] == 'INCOMPLETE'


def test_rewrite_convergence_preserves_full_native_audio_gate(tmp_path, monkeypatch):
    from src.autoslice.exact_final_convergence import _converge_one
    from tests.test_exact_final_convergence import _srt, _reconsidered_finding, _authority
    monkeypatch.delenv('AUTOSLICE_BASE', raising=False)
    current, proposed = '三二', '方案二'
    srt = _srt(current)
    finding = _reconsidered_finding(current, proposed)
    history = _authority('旧文本', current)['exact_final_cpa_self_heal']['passes'][0]['repairs']
    raw = '{"choice":"UNRESOLVED","needs_audio":true,"reason":"还需完整语义块补听"}'
    prompts = []
    def recorded(prompt):
        prompts.append(prompt)
        return raw
    original = _converge_one(srt, finding, history=history, judge_llm_call=recorded)
    case = {'prompt': prompts[0], 'raw': raw, 'record': {'replay_consumer_inputs': {
        'srt_text': srt, 'finding': finding, 'history': history}}}
    result = validate_case(case)
    assert result['status'] == 'ACCEPT'
    assert result['native_gate'] == original[0]['exact_release_adjudication']['history_convergence_acoustic_witness']
    assert result['native_gate']['status'] == 'BLOCK'
    assert result['native_gate']['reason_code'] == 'CPA_REQUESTS_MORE_AUDIO'
    assert validate_case({**case, 'record': {}})['status'] == 'INCOMPLETE'
    assert validate_case({**case, 'prompt': case['prompt'] + 'drift'})['detail'] == (
        'REPLAY_ORIGINAL_REWRITE_CONVERGENCE_PROMPT_MISMATCH')
    # The ordinary acoustic gate still runs for a proposed replacement.
    from copy import deepcopy
    missing_witness = deepcopy(case)
    missing_witness['record']['replay_consumer_inputs']['finding']['exact_release_adjudication'].pop('verdict')
    missing_witness['raw'] = '{"choice":"PROPOSED","needs_audio":false,"reason":"改字"}'
    # Capture the actual prompt for this separate input, not a stale prompt.
    prompts.clear()
    _converge_one(srt, missing_witness['record']['replay_consumer_inputs']['finding'],
        history=history, judge_llm_call=recorded)
    missing_witness['prompt'] = prompts[0]
    replaced = validate_case(missing_witness)
    assert replaced['native_gate']['status'] == 'BLOCK'


def test_pronoun_trace_retains_original_input_and_checks_raw_binding(tmp_path):
    from src.autoslice.pronoun_stage_trace import apply_pronoun_pass
    from scripts.replay_recorded_answers import sha
    srt = '1\n00:00:00,000 --> 00:00:01,000\n它是学校\n'
    prompts = []
    raw = '{"rewrites":[]}'
    apply_pronoun_pass(srt, cpa_llm_call=lambda p: prompts.append(p) or raw,
        display_name='李豆沙', required=True)
    def retained(text):
        return {'retention': 'RETAINED', 'text': text, 'bytes': len(text.encode()), 'sha256': sha(text)}
    folder = tmp_path/'source.pronoun-trace';folder.mkdir()
    path = folder/'original.json'
    trace = {'schema_version': 'aggregate-pronoun-stage-trace.v1', 'input_srt': retained(srt),
        'calls': [{'prompt': retained(prompts[0]), 'completion': retained(raw), 'outcome': 'RETURNED'}]}
    path.write_text(json.dumps(trace))
    case = {'prompt': prompts[0], 'raw': raw,
        'path': str(tmp_path/'cpa-text-exchanges'/'request'/'completion.txt')}
    result = validate_case(case)
    assert result['status'] == 'ACCEPT'
    assert result['saved_inputs'][0]['json_pointer'] == '/input_srt/text'
    trace['input_srt']['sha256'] = '0' * 64
    path.write_text(json.dumps(trace))
    assert validate_case(case)['detail'] == 'REPLAY_ORIGINAL_PRONOUN_TRACE_DRIFT'
