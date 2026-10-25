"""Exercise real consumers with API fields and native stdout events, offline."""
from contextlib import contextmanager, nullcontext
from dataclasses import replace
import importlib.util
import importlib
import inspect
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

import pytest

from src.autoslice import llm_client as llm, structured_output as output


ROOT = Path(__file__).resolve().parents[1]
CONSUMER_COVERAGE = set()


@pytest.fixture(autouse=True)
def _save_consumer_coverage():
    yield
    if os.environ.get('STRUCTURED_CONSUMER_COVERAGE'):
        Path(os.environ['STRUCTURED_CONSUMER_COVERAGE']).write_text(json.dumps(sorted(CONSUMER_COVERAGE), indent=2))


def _scenario(path):
    name = '_structured_scenario_' + path.replace('/', '_').replace('.', '_')
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, ROOT / path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def _api_probe(monkeypatch, field):
    """Send each scenario's fake answer through the actual CPA decoder.

    Keep each scenario's business assertions. Only the fake provider boundary
    changes: consumers must receive the field's text, never the API envelope.
    """
    # Eager imports prevent a lazy module from retaining a previous case's
    # monkeypatched function after pytest restores the common module.
    for name in ('pronoun_stage_trace', 'pronoun_consistency', 'song_alignment', 'speaker_context',
                 'editorial_subtitle', 'foreign_closed_set_rebuild', 'missing_proposal_bootstrap',
                 'semantic_terminal_adjudication', 'acoustic_witness_adjudication'):
        importlib.import_module('src.autoslice.' + name)
    class Probe(list):
        pass
    hits = Probe()
    config = llm.LlmConfig(transport='direct', api_mode='responses', model='offline',
        api_base='https://offline.invalid/v1', api_key_env='STRUCTURED_CONSUMER_TEST_KEY')
    monkeypatch.setenv('STRUCTURED_CONSUMER_TEST_KEY', 'fictional')
    original_json = output.call_json
    original_retry = llm.call_and_extract_json_with_parse_retry

    def transport(call):
        identity = getattr(call, 'cpa_cache_identity', None) or {}
        models = identity.get('models') or []
        bound_config = replace(config, model=models[0]) if models else config
        def send(prompt):
            saved = llm.observed_cpa_request
            @contextmanager
            def post(request, **kwargs):
                body = json.loads(request.data)
                # The original callback remains a test fixture, with its own
                # invocation counters, failures, and evidence assertions.
                with patch.object(llm, 'observed_cpa_request', saved):
                    raw = call(body['input'])
                envelope = {'model': getattr(raw, 'answering_model', None) or body['model'],
                    'choices': [{'message': {'content': 'WRONG_OLD_FIELD'}}],
                    'structured_output': {'wrong': 'WRONG_ENVELOPE'}}
                if field == 'output_text':
                    envelope['output_text'] = raw
                else:
                    envelope['output'] = [{'type': 'reasoning', 'content': []},
                        {'type': 'message', 'content': [{'type': 'output_text', 'text': raw}]}]
                hits.append((body, raw))
                yield io.BytesIO(json.dumps(envelope).encode())
            with patch.object(llm, 'observed_cpa_request', post):
                # This adapter probes one physical API decode; the surrounding
                # scenario owns retries and its declared model/cache binding.
                return llm._call_direct_once(prompt, bound_config)
        for key, value in getattr(call, '__dict__', {}).items():
            setattr(send, key, value)
        return send

    def mark_site():
        caller = inspect.stack()[2]
        CONSUMER_COVERAGE.add(f'{Path(caller.filename).relative_to(ROOT)}:{caller.function}:{caller.lineno}')

    def call_json(call, prompt, **kwargs):
        mark_site()
        return original_json(transport(call), prompt, **kwargs)

    def parse_retry(prompt, *, llm_call, **kwargs):
        mark_site()
        return original_retry(prompt, llm_call=transport(llm_call), **kwargs)

    for module in list(sys.modules.values()):
        if not getattr(module, '__name__', '').startswith(('src.autoslice.', 'scripts.')):
            continue
        for name, value in list(vars(module).items()):
            if value is original_json:
                monkeypatch.setattr(module, name, call_json)
            elif value is original_retry:
                monkeypatch.setattr(module, name, parse_retry)
    hits.transport = transport
    return hits


# Reuse complete business scenarios instead of duplicating their large evidence
# fixtures. Every case keeps the original consumer's assertions on its result.
SCENARIOS = [
    ('tests/test_boundary_semantic_review.py', 'test_independent_boundary_review_binds_recommendation_to_cue_grid'),
    ('tests/test_boundary_semantic_review.py', 'test_completed_boundary_response_parser_pass_reuses_raw_without_new_vote'),
    ('tests/test_community_name_crawler.py', 'test_repeated_cross_uploader_name_is_accepted_but_remains_occurrence_neutral'),
    ('tests/test_community_name_crawler.py', 'test_relation_review_splits_cards_into_bounded_cpa_requests_in_order'),
    ('tests/test_cover_punch_semantics_retry.py', 'test_cover_punch_semantic_review_retries_one_fragmentary_rejection'),
    ('tests/test_cover_story_direction.py', 'test_semantic_copy_change_replaces_stale_brief_before_image_prompt'),
    ('tests/test_exact_final_convergence.py', 'test_history_aware_cpa_can_apply_proposed_then_memo_closes_repeat'),
    ('tests/test_final_review_candidate_risk.py', 'test_context_supported_same_reference_nickname_can_be_disclosed'),
    ('tests/test_nested_media_acoustic_runtime.py', 'test_cpa_raw_completion_and_parsed_evidence_are_bound'),
    ('tests/test_full_session_transcription.py', 'test_cpa_pronoun_ta_pass_still_validates_minimal_legal_rewrite'),
    ('tests/test_pronoun_referent_consistency.py', 'test_mixed_forms_for_one_referent_trigger_one_retry_that_can_resolve'),
    ('tests/test_read_aloud_llm_verifier.py', 'test_confident_context_judgment_returns_hash_bound_exact_chat_verdict'),
    ('tests/test_read_aloud_llm_verifier.py', 'test_weak_context_uses_candidate_blind_audio_then_cpa_highest_probability'),
    ('tests/test_wholepath_phase2_regressions.py', 'test_k21_read_aloud_same_binding_reuses_the_completed_response'),
    ('tests/test_semantic_audio_recovery.py', 'test_corrupt_saved_range_plan_blocks_without_redispatch'),
    ('tests/lidousha/test_semantic_evidence_scorecard_refresh.py', 'test_refreshes_two_known_20260809_candidates_without_rediscovery'),
    ('tests/test_source_context_cpa_refinement.py', 'test_source_context_cpa_is_numbered_text_only_and_preserves_grid'),
    ('tests/lidousha/test_source_fact_review.py', 'test_game_relationship_title_repair_preserves_context_in_final_surface'),
    ('tests/lidousha/test_source_fact_review.py', 'test_completed_source_fact_response_keep_parser_pass_still_validates_current_srt'),
    ('tests/test_talk_filler.py', 'test_global_verifier_prompt_contains_original_source_and_removed_cues'),
    ('tests/lidousha/test_cpa_semantic_qa_llm.py', 'test_judge_binds_hash_and_paths_from_request_not_llm'),
    ('tests/test_topic_context_proposals.py', 'test_foreign_original_language_observation_reaches_proposal'),
    ('tests/test_exact_final_funnel.py', 'test_no_modification_routes_every_unresolved_through_real_terminal'),
    ('tests/test_normal_text_generation_checkpoint.py', 'test_title_replays_rejected_comparisons_then_original_bounded_retry'),
    ('tests/test_selection_rescore.py', 'test_scenario_3_rescore_success_produces_a_valid_rankable_scorecard'),
]


@pytest.mark.parametrize('field', ['output_text', 'output'])
@pytest.mark.parametrize('path,name', SCENARIOS, ids=[name for _, name in SCENARIOS])
def test_cpa_business_consumer_reads_api_text_field(tmp_path, monkeypatch, path, name, field):
    scenario = getattr(_scenario(path), name)
    hits = _api_probe(monkeypatch, field)
    if path == 'tests/test_normal_text_generation_checkpoint.py':
        module = _scenario(path)
        original_fixture = module.fixture_call
        monkeypatch.setattr(module, 'fixture_call', lambda *args, **kwargs:
            hits.transport(original_fixture(*args, **kwargs)))
    fixtures = {'tmp_path': tmp_path, 'monkeypatch': monkeypatch}
    if 'terminal_rig' in inspect.signature(scenario).parameters:
        module = _scenario(path)
        fixtures['terminal_rig'] = module.terminal_rig.__wrapped__(tmp_path, monkeypatch)
    kwargs = {key: fixtures[key]
              for key in inspect.signature(scenario).parameters}
    scenario(**kwargs)
    assert hits, 'scenario must exercise a JSON dispatch and its real consumer'
    for body, _ in hits:
        assert body['input']
        if 'json' in body['input'].lower():
            assert body['text']['format']['type'] in {'json_object', 'json_schema'}
    if os.environ.get('STRUCTURED_CONSUMER_COVERAGE'):
        Path(os.environ['STRUCTURED_CONSUMER_COVERAGE']).write_text(json.dumps(sorted(CONSUMER_COVERAGE), indent=2))


@pytest.mark.parametrize('field', ['output_text', 'output'])
@pytest.mark.parametrize('lane', ['bootstrap', 'third', 'convergence'])
def test_proposal_consumers_read_api_field(tmp_path, monkeypatch, field, lane):
    scenario = _scenario('tests/test_topic_context_proposals.py')
    hits = _api_probe(monkeypatch, field)
    scenario.test_topic_candidate_requires_sound_and_real_context(lane, '我们分析隐含波动率', ['期权价格和波动率'], None)
    assert hits


@pytest.mark.parametrize('field', ['output_text', 'output'])
def test_candidate_risk_consumer_reads_api_field(monkeypatch, field):
    scenario = _scenario('tests/test_final_review_candidate_risk.py')
    hits = _api_probe(monkeypatch, field)
    scenario.test_source_candidate_and_content_risk_have_separate_bound_decisions('factual_error')
    assert hits


@pytest.mark.parametrize('field', ['output_text', 'output'])
def test_closed_set_consumer_reads_api_field(monkeypatch, field):
    from src.autoslice.acoustic_witness_adjudication import _judge_word_choice_body
    scenario = _scenario('tests/lidousha/test_acoustic_witness_adjudication.py')
    hits = _api_probe(monkeypatch, field)
    result = _judge_word_choice_body(check_request=scenario.CHECK_REQUEST,
        witness=scenario._witness('hai mei you ge zhai ne'),
        llm_call=lambda prompt: '{"choice":"PROPOSED","needs_audio":false,"reason":"API_FIELD"}')
    assert result['status'] == 'JUDGED' and result['choice'] == 'PROPOSED'
    assert result['reason'] == 'API_FIELD'
    assert hits[0][0]['text']['format']['type'] == 'json_schema'


@pytest.mark.parametrize('field', ['output_text', 'output'])
def test_uncached_range_plan_consumer_reads_api_field(monkeypatch, field):
    from src.autoslice.semantic_audio_recovery import _range_plan_completion
    monkeypatch.delenv('AUTOSLICE_BASE', raising=False)
    hits = _api_probe(monkeypatch, field)
    raw, cached = _range_plan_completion('Return JSON plan',
        lambda prompt: '{"decision":"NO_NEW_AUDIO","reason":"API_FIELD"}', {})
    assert llm.extract_json_object(raw)['reason'] == 'API_FIELD' and cached is False
    assert len(hits) == 1


@pytest.mark.parametrize('field', ['output_text', 'output'])
def test_editorial_consumer_reads_api_field(tmp_path, monkeypatch, field):
    from src.autoslice import editorial_subtitle as editorial
    from src.autoslice.final_review_auditor import build_context_adjudication_request
    scenario = _scenario('tests/test_editorial_subtitle.py')
    hits = _api_probe(monkeypatch, field)
    finding = {'cue_index': 1, 'suspect': '人鱼', 'suggestion': '流星雨',
        'repair_class': 'editorial_rendering', 'proposed_full_cue': scenario.TEXT[:-2] + '流星雨'}
    check = build_context_adjudication_request(scenario.SRT, finding, clip_context=scenario.CONTEXT,
        source_media_timeline_offset_ms=5000)
    result = editorial.adjudicate(scenario.SRT, finding, check_request=check,
        clip_context=scenario.CONTEXT, entity_verifier=scenario.witness,
        judge_llm_call=lambda prompt: json.dumps(scenario.answer()), screen_read_probe=None)
    assert hits and result['status'] == 'OBSERVED', result
    assert result['editorial_authority']['proposed_cue'].endswith('流星雨')


@pytest.mark.parametrize('field', ['output_text', 'output'])
def test_song_and_speaker_and_foreign_consumers_read_api_field(tmp_path, monkeypatch, field):
    from src.autoslice import song_alignment, speaker_context, foreign_closed_set_rebuild
    from src.autoslice.review_evidence import SourceCue
    from scripts.apply_subtitle_text_overrides import TextCue
    hits = _api_probe(monkeypatch, field)
    assert song_alignment.generate_llm_song_queries(
        [SourceCue('one', 0, 1000, '歌词', 'zh', 'speech', 1.0)],
        lambda prompt: '{"guesses":[{"title":"歌名","artist":"歌手"}]}') == ['歌名 歌手', '歌名']
    votes, attempts, errors = speaker_context._whole_clip_context_votes(
        [TextCue(1, '00:00:00,000', '00:00:01,000', '讲话')], [None], [0],
        lambda prompt: json.dumps({'labels': [{'n': 1, 'speaker': speaker_context.HOST_SPEAKER,
            'confidence': 0.95, 'reason': 'context'}]}))
    assert votes[0]['speaker'] == speaker_context.HOST_SPEAKER and attempts == 1 and not errors
    proposed, audit = foreign_closed_set_rebuild.rebuild_foreign_closed_set(current='旧句', rejected='被拒句',
        before='前文', after='后文', witness={}, judge_reason='待组合',
        llm_call=lambda prompt: '{"status":"PROPOSED","proposed_cue":"新句","reason":"依据"}')
    assert proposed == '新句' and audit['status'] == 'PROPOSED'
    assert len(hits) == 3


@pytest.mark.parametrize('field', ['output_text', 'output'])
def test_title_cover_consumer_reads_api_field(tmp_path_factory, monkeypatch, field):
    scenario = _scenario('tests/test_title_cover_skill_integration.py')
    hits = _api_probe(monkeypatch, field)
    bundle = scenario.offline_bundle.__wrapped__(tmp_path_factory)
    scenario.test_offline_candidate_producer_renders_scores_and_seals(bundle)
    assert hits


@pytest.mark.parametrize('field', ['output_text', 'output'])
def test_explicit_contract_consumers_read_api_field(tmp_path, monkeypatch, field):
    from src.autoslice.subtitle_draft_preparation import _required_cpa_cues
    from scripts.suggest_upload_tags import llm_content_tags
    hits = _api_probe(monkeypatch, field)
    assert _required_cpa_cues('Return JSON cues', hits.transport(lambda prompt:
        '{"cues":[{"n":1,"text":"API_FIELD"}]}'), 1) == {1: 'API_FIELD'}
    # Tag parsing reads the same text boundary with a different consumer shape.
    tags, warnings = llm_content_tags('标题', '1\n00:00:00,000 --> 00:00:01,000\n字幕\n', [], 30,
        llm_call=hits.transport(lambda prompt: '{"tags":[{"tag":"API_FIELD","why":"字幕"}]}'))
    assert tags[0]['tag'] == 'API_FIELD' and not warnings
    assert len(hits) == 2


@pytest.mark.parametrize('explicit', [False, True])
@pytest.mark.parametrize('api_mode', ['responses', 'chat'])
def test_no_json_word_omits_format_and_records_reason(monkeypatch, tmp_path, explicit, api_mode):
    monkeypatch.setenv('STRUCTURED_CONSUMER_TEST_KEY', 'fictional')
    monkeypatch.setenv('AUTOSLICE_CPA_USAGE_FILE', str(tmp_path / 'usage.jsonl'))
    bodies = []
    @contextmanager
    def post(request, **kwargs):
        bodies.append(json.loads(request.data))
        yield io.BytesIO(json.dumps({'output_text': 'free text',
            'choices': [{'message': {'content': 'free text'}}]}).encode())
    monkeypatch.setattr(llm, 'observed_cpa_request', post)
    config = llm.LlmConfig(transport='direct', api_mode=api_mode, model='offline',
        api_base='https://offline.invalid/v1', api_key_env='STRUCTURED_CONSUMER_TEST_KEY', json_mode=explicit)
    assert llm.build_llm_call(config)('Please answer freely') == 'free text'
    assert 'text' not in bodies[0] and 'response_format' not in bodies[0]
    if explicit:
        assert 'JSON_MODE_SKIPPED_NO_JSON_WORD' in (tmp_path / 'usage.jsonl').read_text()
    assert output.current_contract()['json_mode'] is False


def test_default_prompt_containing_json_does_not_opt_in(monkeypatch):
    monkeypatch.setenv('STRUCTURED_CONSUMER_TEST_KEY', 'fictional')
    @contextmanager
    def post(request, **kwargs):
        assert 'text' not in json.loads(request.data)
        yield io.BytesIO(b'{"output_text":"freely discuss JSON"}')
    monkeypatch.setattr(llm, 'observed_cpa_request', post)
    call = llm.build_llm_call(llm.LlmConfig(transport='direct', api_mode='responses', model='offline',
        api_base='https://offline.invalid/v1', api_key_env='STRUCTURED_CONSUMER_TEST_KEY'))
    assert call('Explain JSON freely') == 'freely discuss JSON'


@pytest.mark.parametrize('explicit', [False, True])
def test_cpa_command_default_or_missing_word_has_no_format(tmp_path, monkeypatch, explicit):
    seen = []
    def run(argv, **kwargs):
        assert kwargs['env'] is None
        seen.append((Path(argv[-2]).read_text(),
            json.loads(Path(argv[-2] + '.response-format.json').read_text())))
        Path(argv[-1]).write_text('free answer')
        return subprocess.CompletedProcess(argv, 0, '', '')
    monkeypatch.setattr(llm.subprocess, 'run', run)
    call = llm.build_llm_call(llm.LlmConfig(transport='command', json_mode=explicit,
        command_template='offline-command {prompt_file} {completion_file}'))
    prompt = 'Please answer freely' if explicit else 'Explain JSON freely'
    assert call(prompt) == 'free answer'
    assert seen == [(prompt, None)]


@pytest.mark.parametrize('endpoint', ['single', 'batch'])
@pytest.mark.parametrize('field', ['output_text', 'output'])
def test_cpa_visual_consumers_read_api_field(tmp_path, monkeypatch, endpoint, field):
    from src.autoslice import cpa_frame_witness as frame
    bodies = []
    raw = '{"source":"API_FIELD"}'
    @contextmanager
    def post(request, **kwargs):
        bodies.append(json.loads(request.data))
        payload = {'choices': [{'message': {'content': 'WRONG_FIELD'}}]}
        if field == 'output_text':
            payload['output_text'] = raw
        else:
            payload['output'] = [{'type': 'message', 'content': [{'type': 'output_text', 'text': raw}]}]
        yield io.BytesIO(json.dumps(payload).encode())
    monkeypatch.setattr(frame, 'observed_cpa_request', post)
    monkeypatch.setattr(frame, 'runtime_provider_slot', lambda **kwargs: nullcontext())
    kwargs = dict(api_base='https://offline.invalid/v1', api_key='fictional', model='offline',
        response_schema={'type': 'object', 'properties': {'source': {'type': 'string'}}, 'required': ['source']})
    if endpoint == 'single':
        result = frame._vision_qa({}, b'pixels', 'Describe visible pixels', timeout_seconds=30, max_tokens=100, **kwargs)
    else:
        path = tmp_path / 'frame.jpg'
        path.write_bytes(b'pixels')
        result = frame.batch_jpeg_vision_probe([path], 'Describe visible pixels', **kwargs)
    assert json.loads(result['answer']) == {'source': 'API_FIELD'}
    assert bodies[0]['text']['format']['type'] == 'json_schema'


@pytest.mark.parametrize('field', ['output_text', 'output'])
def test_experiment_consumer_reads_api_field(field):
    from scripts.astra_eval_transport import message_text
    payload = {'choices': [{'message': {'content': 'WRONG_FIELD'}}]}
    raw = '{"source":"API_FIELD"}'
    if field == 'output_text':
        payload['output_text'] = raw
    else:
        payload['output'] = [{'type': 'message', 'content': [{'type': 'output_text', 'text': raw}]}]
    assert llm.extract_json_object(message_text(payload)) == {'source': 'API_FIELD'}


def test_agy_visual_consumer_reads_native_stdout_result(tmp_path, monkeypatch):
    from src.autoslice import agy_frame_witness as frame, agy_gemini_client as gemini
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        Path(kwargs['cwd'], 'verdict.json').write_text('{"text":"WRONG_FILE"}')
        raw = '\n'.join([json.dumps({'type': 'assistant', 'text': 'WRONG_PROSE'}),
            json.dumps({'type': 'result', 'result': {'status': 'SUCCESS',
                'structured_output': {'text': 'STDOUT_RESULT'}}})])
        return gemini.AgyRun(completed=subprocess.CompletedProcess(argv, 0, raw, ''), failure_category=None)
    monkeypatch.setattr(gemini, 'run_local_agy', run)
    receipt = frame._vision_qa({}, b'pixels', 'Return JSON text', timeout_seconds=30)
    assert json.loads(receipt['answer']) == {'text': 'STDOUT_RESULT'}
    assert '--json-schema' in calls[0] and '--output-format' in calls[0]


def test_screen_text_remote_keeps_file_contract_without_structured_flags(tmp_path, monkeypatch):
    from src.autoslice import full_session_transcription as full
    calls = []
    monkeypatch.setattr(full, '_is_local_host', lambda host: False)
    def run(argv, **kwargs):
        calls.append(argv)
        command = argv[-1]
        raw = 'rc=0' if '/agy.rc' in command and command.startswith('cat ') else 'WRONG_STDOUT'
        if command.startswith('cat ') and '/screen_text.json' in command:
            raw = '[{"time":"00:01","text":"FILE_RESULT","kind":"caption"}]'
        return subprocess.CompletedProcess(argv, 0, raw, '')
    monkeypatch.setattr(full.subprocess, 'run', run)
    media = tmp_path / 'input.mp4'
    media.write_bytes(b'fixture')
    assert full._agy_screen_text_lines('offline-host', media) == ['00:01 [caption] FILE_RESULT']
    commands = '\n'.join(' '.join(argv) for argv in calls)
    assert '--json-schema' not in commands and '--output-format' not in commands
    assert 'response-schema.json' not in commands
    assert any(argv[-1].startswith('cat ') and '/screen_text.json' in argv[-1] for argv in calls)


def test_screen_text_local_process_has_no_structured_flags(tmp_path, monkeypatch):
    from src.autoslice import jingting_remote_runner as runner
    commands = []
    monkeypatch.setattr(runner.subprocess, 'Popen', lambda command, **kwargs: commands.append(command))
    runner._start_local_agy_process(tmp_path, binary='offline-agy', model='same-model',
        short_prompt='write screen_text.json', print_timeout='30s')
    assert '--json-schema' not in commands[0] and '--output-format' not in commands[0]
