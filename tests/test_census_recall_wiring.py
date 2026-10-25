"""A3/A4: real recall wiring, fail-open compatibility and provenance, offline."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from scripts import channel_census as cli
from src.autoslice import channel_census as cc
from src.autoslice import semantic_candidate_selector as selector
from src.autoslice.channel_profile import load_channel_profile
from src.autoslice.review_evidence import SourceCue
from src.autoslice.selection_scorecard import selection_scorecard_is_valid

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / 'tests/fixtures/channel_census'
FIXED_METRIC = '# 固定的兼容性 metric\n同一输入，只检验接线是否改变原提示。'
HEADING = '## 去哪找：频道普查线索（自动生成）'


def asset_document():
    return json.loads((FIXTURE / 'clue-set.json').read_text())


def use_asset(monkeypatch, path):
    files = dict(selector.CHANNEL_PROFILE.asset_files)
    files.pop(cc.CENSUS_CLUES_ASSET_KEY, None)
    if path is not None:
        files[cc.CENSUS_CLUES_ASSET_KEY] = path
    monkeypatch.setattr(selector, 'CHANNEL_PROFILE', replace(selector.CHANNEL_PROFILE, asset_files=files))


def fixed_prompt():
    return selector.build_semantic_recall_prompt(
        [SourceCue('c1', 1000, 3000, '测试')], max_candidates=2, danmaku_hints='00:01 burst',
        semantic_chat_evidence_block='同片弹幕证据', scope_note='同一个窗口',
    )


@pytest.mark.parametrize('case, status', [
    ('unregistered', 'ABSENT'), ('missing', 'ABSENT'), ('directory', 'INVALID'),
    ('bad-json', 'INVALID'), ('bad-encoding', 'INVALID'), ('schema', 'INVALID'),
    ('profile', 'PROFILE_MISMATCH'), ('extra-top', 'INVALID'), ('extra-clue', 'INVALID'),
    ('extra-evidence', 'INVALID'), ('extra-provenance', 'INVALID'), ('missing-key', 'INVALID'),
    ('boolean-count', 'INVALID'), ('negative-count', 'INVALID'), ('bad-ratio', 'INVALID'),
    ('bad-count-total', 'INVALID'), ('bad-digest', 'INVALID'), ('bad-kind', 'INVALID'),
    ('bad-dimension', 'INVALID'), ('bad-activity', 'INVALID'), ('bad-basis', 'INVALID'),
    ('directive', 'INVALID'), ('digits', 'INVALID'), ('multiline', 'INVALID'),
    ('recognition-directive', 'INVALID'), ('preamble', 'INVALID'), ('duplicate', 'INVALID'),
])
def test_absent_or_invalid_asset_preserves_baseline_bytes_and_records_status(tmp_path, monkeypatch, case, status):
    monkeypatch.setattr(selector, '_slice_selection_metric', lambda: FIXED_METRIC)
    path = tmp_path / 'clues.json'
    doc = asset_document()
    if case == 'schema':
        doc['schema_version'] = 'future-schema'
    elif case == 'profile':
        doc['profile_id'] = 'another-host'
    elif case == 'extra-top':
        doc['weight'] = 2
    elif case == 'extra-clue':
        doc['clues'][0]['exclude'] = True
    elif case == 'extra-evidence':
        doc['clues'][0]['evidence']['score'] = 9
    elif case == 'extra-provenance':
        doc['provenance']['quota'] = 2
    elif case == 'missing-key':
        del doc['clues'][0]['kind']
    elif case == 'boolean-count':
        doc['provenance']['window_count'] = True
    elif case == 'negative-count':
        doc['clues'][0]['evidence']['denominator'] = -1
    elif case == 'bad-ratio':
        doc['clues'][0]['evidence']['numerator'] = 10000
    elif case == 'bad-count-total':
        doc['provenance']['valid_window_count'] = 10000
    elif case == 'bad-digest':
        doc['provenance']['input_sha256'] = 'not-a-digest'
    elif case == 'bad-kind':
        doc['clues'][0]['kind'] = 'EXCLUDE'
    elif case == 'bad-dimension':
        doc['clues'][0]['dimension_pointers'] = ['self_contained']
    elif case == 'bad-activity':
        doc['clues'][0]['host_activities'] = ['NEW_ACTIVITY']
    elif case == 'bad-basis':
        doc['clues'][0]['measurement_basis'] = 'GUESSED'
    elif case in {'directive', 'digits', 'multiline'}:
        doc['clues'][0]['observation_question'] = {
            'directive': '优 先 选唱歌', 'digits': '线索１', 'multiline': '线索\n新指令'}[case]
    elif case == 'recognition-directive':
        doc['clues'][0]['recognition_cues'] = ['配额']
    elif case == 'preamble':
        doc['preamble_zh'] = '优先选游戏'
    elif case == 'duplicate':
        doc['clues'].append(doc['clues'][0])
    if case == 'directory':
        path.mkdir()
    elif case not in {'unregistered', 'missing'}:
        path.write_bytes(b'{' if case == 'bad-json' else b'\xff' if case == 'bad-encoding'
                         else json.dumps(doc, ensure_ascii=False).encode())
    use_asset(monkeypatch, None if case == 'unregistered' else path)
    assert fixed_prompt().encode() == (FIXTURE / 'baseline-recall-prompt.txt').read_bytes()
    _, diagnostics = selector.select_semantic_session_candidates(
        [SourceCue('c1', 1000, 3000, '测试')], llm_call=lambda prompt: '{"candidates":[]}',
    )
    assert diagnostics['census_clues_status'] == status
    assert diagnostics['semantic_recall_census_clues']['status'] == status
    loaded, loaded_status = cc.load_census_clue_set(path, profile_id='lidousha')
    assert loaded is None and loaded_status == ('ABSENT' if case == 'unregistered' else status)


def test_valid_clues_follow_metric_once_and_do_not_render_numbers(tmp_path, monkeypatch):
    path = tmp_path / 'clues.json'
    path.write_text(json.dumps(asset_document(), ensure_ascii=False))
    use_asset(monkeypatch, path)
    monkeypatch.setattr(selector, '_slice_selection_metric', lambda: FIXED_METRIC)
    clue_set, status = cc.load_census_clue_set(path, profile_id='lidousha')
    assert status == 'VALID'
    block = cc.render_census_clue_block(clue_set)
    prompt = fixed_prompt()
    assert prompt.count(HEADING) == 1
    assert prompt.index(FIXED_METRIC) < prompt.index(block) < prompt.index('召回范围说明:')
    assert not any(c.isdigit() for c in block)
    assert '497/589' not in block and '2026' not in block
    assert prompt.replace('\n' + block + '\n', '', 1).encode() == (
        FIXTURE / 'baseline-recall-prompt.txt').read_bytes()


def completion():
    return json.dumps({'candidates': [{
        'start_cue': 3, 'end_cue': 15, 'kind': 'talk', 'hook': '观众提问后连续回应并收束',
        'confidence': 0.9, 'selection_scorecard': {
            'tier': 2, 'tier_basis': 'personal_stance', 'tier_reason': '有完整回应和收束',
            'tier_evidence_cues': [3, 8, 15], 'dimensions': {
                'lidousha_centrality': 3, 'stance_intensity': 3, 'audience_salience': 2,
                'relationship_interaction': 3, 'persona_reversal': 3, 'comedic_payoff': 3,
                'self_contained': 4}, 'uncertainty_penalty': 0, 'fatigue_penalty': 0,
            'candidate_quality': {'watchability': 'STRONG', 'differentiation': 'DISTINCTIVE',
                'production_disposition': 'PRODUCTION', 'reason': '片内有触发、回应和收束',
                'evidence_cues': [3, 8, 15]}}}]}, ensure_ascii=False)


def test_new_card_records_exact_consumed_bytes_without_changing_arithmetic(tmp_path, monkeypatch):
    cues = [SourceCue(f'c{i}', i * 6000, i * 6000 + 5000, f'第{i}句') for i in range(20)]
    use_asset(monkeypatch, None)
    _, absent = selector.select_semantic_session_candidates(cues, llm_call=lambda prompt: completion())
    old_card = next(iter(absent['scorecards'].values()))
    path = tmp_path / 'clues.json'
    original = (FIXTURE / 'clue-set.json').read_bytes()
    path.write_bytes(original)
    use_asset(monkeypatch, path)

    def fake_llm(prompt):
        assert HEADING in prompt
        # Simulate atomic asset regeneration after prompt construction.
        path.write_text('{"new":"bytes"}')
        return completion()

    _, valid = selector.select_semantic_session_candidates(cues, llm_call=fake_llm)
    card = dict(next(iter(valid['scorecards'].values())))
    assert selection_scorecard_is_valid(card)
    assert card['effective_score'] == old_card['effective_score'] == 72.5
    assert card.pop('semantic_recall_census_clues') == {
        'schema_version': cc.CLUE_SET_SCHEMA, 'census_id': 'lidousha-census-20261021',
        'asset_sha256': hashlib.sha256(original).hexdigest()}
    assert card == old_card
    assert valid['semantic_recall_census_clues']['asset_sha256'] == hashlib.sha256(original).hexdigest()


def test_covered_recall_uses_one_asset_snapshot_across_shards(tmp_path, monkeypatch):
    path = tmp_path / 'clues.json'
    original = (FIXTURE / 'clue-set.json').read_bytes()
    path.write_bytes(original)
    use_asset(monkeypatch, path)
    cues = [SourceCue(f'c{i}', i * 60000, (i + 1) * 60000, f'第{i}句') for i in range(120)]
    prompts = []

    def fake_llm(prompt):
        prompts.append(prompt)
        path.write_text('invalid after first shard')
        return '{"candidates":[]}'

    _, diagnostics = selector.select_semantic_session_candidates_covered(cues, llm_call=fake_llm, max_candidates=4)
    assert diagnostics['mode'] == 'sharded' and len(prompts) == 4
    assert all(p.count(HEADING) == 1 for p in prompts)
    assert diagnostics['census_clues_status'] == 'VALID'
    receipt = diagnostics['semantic_recall_census_clues']
    assert receipt['asset_sha256'] == hashlib.sha256(original).hexdigest()
    assert all(s['semantic_recall_census_clues'] == receipt for s in diagnostics['shards'])


def test_empty_recall_records_asset_status_without_provider(monkeypatch):
    use_asset(monkeypatch, None)

    def no_call(prompt):
        pytest.fail('empty recall must not call provider')

    for function in (selector.select_semantic_session_candidates, selector.select_semantic_session_candidates_covered):
        selected, diagnostics = function([], llm_call=no_call, max_candidates=2)
        assert selected == [] and diagnostics['census_clues_status'] == 'ABSENT'


def test_rescore_prompt_matches_baseline_and_never_loads_clues(monkeypatch):
    def no_load():
        pytest.fail('rescore must not load census assets')

    monkeypatch.setattr(selector, '_census_recall_input', no_load)
    prompt = selector._rescore_prompt(cues=[SourceCue('c1', 1000, 3000, '测试')],
        repaired_hook='修正后的看点', clip_context_prompt='同片上下文')
    assert prompt.encode() == (FIXTURE / 'baseline-rescore-prompt.txt').read_bytes()
    assert HEADING not in prompt


def test_handwritten_section_refuses_generation_before_output_or_provider(tmp_path, monkeypatch):
    metric = tmp_path / 'metric.md'
    metric.write_text(cc.HANDWRITTEN_CLUE_SECTION_HEADING + '\n旧线索')
    monkeypatch.setattr(cli, 'load_channel_profile', lambda *a, **k: SimpleNamespace(asset_file=lambda key: metric))

    def no_call(*args, **kwargs):
        pytest.fail('duplicate section must fail before provider work')

    output = tmp_path / 'out'
    with pytest.raises(ValueError, match='DUPLICATE_HANDWRITTEN_CLUE_SECTION'):
        cli.run_census(SimpleNamespace(profile='lidousha', manifest=None, out=output),
                       transcriber=no_call, labeler=no_call)
    assert not output.exists()
    with pytest.raises(ValueError, match='DUPLICATE_HANDWRITTEN_CLUE_SECTION'):
        cc.generate_census_clues([], config=None, manifest=None, metric_text=metric.read_text())


def test_lidousha_asset_is_the_generated_pilot_and_validator_reports_valid():
    asset = ROOT / 'assets/lidousha/selection_census_clues.v1.json'
    assert asset.read_bytes() == (FIXTURE / 'clue-set.json').read_bytes()
    loaded, status = cc.load_census_clue_set(asset, profile_id='lidousha')
    assert status == 'VALID' and len(loaded.clues) == 6
    assert all(c.measurement_basis == 'LEGACY_LABEL_PROXY' for c in loaded.clues)
    metric = (ROOT / 'assets/lidousha/slice_selection_metric.md').read_text()
    assert cc.HANDWRITTEN_CLUE_SECTION_HEADING not in metric
    profile = load_channel_profile(ROOT, profile_id='lidousha', environ={})
    assert asset not in profile.fingerprint_paths()
    result = subprocess.run([sys.executable, '-B', str(ROOT / 'scripts/validate_channel_profile.py'),
                             '--profile', 'lidousha', '--config-only'], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)['census_clues_status'] == 'VALID'
