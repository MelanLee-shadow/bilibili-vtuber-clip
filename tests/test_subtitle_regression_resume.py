"""Private regression continuation retains evidence, identity, budgets and scoring."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import ast
import fcntl
import hashlib
import json
import os
import socket
import subprocess

import pytest

from scripts import subtitle_regression_eval as evaluation

SRT = '1\n00:00:01,000 --> 00:00:03,000\n合成初始文字\n'
CURRENT = SRT.replace('合成初始文字', '合成当前文字')


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setenv('AUTOSLICE_BASE', '')
    monkeypatch.setenv('AUTOSLICE_CPA_USAGE_FILE', '')
    monkeypatch.setenv('AUTOSLICE_CPA_ARCHIVE_TEXT_EXCHANGES', '')
    def forbidden(*args, **kwargs):
        pytest.fail('Private resume tests must not use the network')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def specimen(tmp_path, *, inside_runtime=False):
    from src.autoslice.clip_context import SCHEMA_VERSION, _canonical_json_sha256
    runtime = tmp_path / 'production'
    paid = runtime / 'state/gemini-paid-backup'
    paid.mkdir(parents=True)
    write(paid / 'usage.json', {'count': 11, 'do_not_reset': True})
    output = (runtime / 'private-runs/synthetic-task/run-01' if inside_runtime
              else tmp_path / 'experiment/results')
    state = evaluation.configure_replay_state(output, runtime)
    write(output / 'replay-state.json', state)
    unknown = output / 'runtime-state/cache/semantic-cpa-attempts/unknown.json'
    write(unknown, {'status': 'OUTCOME_UNKNOWN', 'material': 'original synthetic bytes'})
    originals = tmp_path / 'originals'
    originals.mkdir()
    (originals / 'source.mp4').write_bytes(b'synthetic media, no audio operation')
    (originals / 'original.srt').write_text(SRT)
    context = {'schema_version': SCHEMA_VERSION, 'candidate_id': 'synthetic-resume',
        'recording_date': '2026-10-16', 'mutation_authorized': False,
        'whole_clip_draft_srt': SRT, 'whole_clip_draft_srt_sha256': 'sha256:' + hashlib.sha256(SRT.encode()).hexdigest(),
        'retrieval_budget': {'whole_clip_transcript_truncated': False},
        'pieces': [{'source_media_sha256': 'sha256:' + evaluation.digest(originals / 'source.mp4'),
                    'start_ms': 0, 'end_ms': 4000}]}
    context['context_sha256'] = _canonical_json_sha256(context)
    write(originals / 'context.json', context)
    write(originals / 'authority.json', {'frozen_original': True})
    record = {'start_ms': 0, 'end_ms': 4000, 'clip_context_payload_sha256': context['context_sha256'],
              'subtitle_audio_correspondence': {'intro_offset_ms': 1000}}
    write(originals / 'record.json', record)
    package = {'candidate_id': 'synthetic-resume', 'intro_offset_ms': 1000,
               'context_path': str(originals / 'context.json')}
    for key, name in [('record', 'record.json'), ('authority', 'authority.json'),
                      ('srt', 'original.srt'), ('media', 'source.mp4')]:
        package[key + '_path'] = str(originals / name)
        package[key + '_sha256'] = evaluation.digest(originals / name)
    folder = output / 'example'
    folder.mkdir()
    (folder / 'original.srt').write_text(SRT)
    (folder / 'final.srt').write_text(CURRENT)
    write(folder / 'authority.json', {'private_resume_marker': 'preserve actual prior attempt'})
    write(folder / 'review-01.json', {'status': 'prior review; preserve exactly'})
    (folder / 'review-input-01.srt').write_text(SRT)
    (folder / 'provider-usage.jsonl').write_text('{"event_type":"model_request"}\n')
    history = [{'key_tier': 'paid_backup', 'http_status': 200, 'model': 'synthetic-previous'}]
    write(folder / 'gemini-http-attempts.json', history)
    run = {'candidate_id': package['candidate_id'], 'input_drift': [],
           'input_hashes': {str(path): evaluation.digest(path) for path in originals.iterdir()},
           'final_srt_sha256': evaluation.digest(folder / 'final.srt'),
           'provider_usage': {'gemini_http_attempts': history}, 'reviews': [], 'would_release': False}
    write(folder / 'run.json', run)
    cases = [{'id': 'synthetic-case', 'package': 'example', 'status': 'ready',
              'expected': 'ORACLE_MUST_NOT_REACH_REVIEWER'}]
    fixture = tmp_path / 'cases.json'
    write(fixture, {'cases': cases, 'packages': {'example': package}})
    write(output / 'evaluation.json', {'schema_version': 'subtitle-regression-evaluation.v1',
          'fixture_sha256': evaluation.digest(fixture), 'code_identity': 'old-test-code',
          'results': [{'case_id': 'synthetic-case'}]})
    return output, runtime, fixture, cases, package, unknown, paid


def test_resume_archives_exact_results_without_resetting_private_or_paid_state(tmp_path):
    output, runtime, fixture, cases, _, unknown, paid = specimen(tmp_path)
    before_unknown = unknown.read_bytes()
    before_paid = (paid / 'usage.json').read_bytes()
    original_evaluation = (output / 'evaluation.json').read_bytes()
    record = evaluation.prepare_experiment_resume(output, fixture, cases, 'new-test-code', runtime)
    archive = Path(record['archive'])
    assert (archive / 'evaluation.json').read_bytes() == original_evaluation
    assert record['parent_evaluation_sha256'] == hashlib.sha256(original_evaluation).hexdigest()
    assert record['history_reset'] is False
    assert not any('gemini-paid-backup' in row['path'] for row in record['files'])
    assert unknown.read_bytes() == before_unknown
    assert (paid / 'usage.json').read_bytes() == before_paid
    assert os.environ['AUTOSLICE_BASE'] == str(output / 'runtime-state')
    for row in record['files']:
        assert evaluation.digest(archive / row['path']) == row['sha256']


@pytest.mark.parametrize('damage', ['fixture', 'selected_cases', 'current_text', 'original_source',
    'paid_link', 'state_metadata', 'unfinished', 'same_code', 'rescore', 'no_terminal'])
def test_resume_rejects_drift_or_unknown_before_any_archive_or_dispatch(tmp_path, damage):
    output, runtime, fixture, cases, _, unknown, paid = specimen(tmp_path)
    identity = 'new-test-code'
    prior = json.loads((output / 'evaluation.json').read_text())
    if damage == 'fixture':
        fixture.write_text(fixture.read_text() + ' ')
    elif damage == 'selected_cases':
        cases[0]['id'] = 'different-case'
    elif damage == 'current_text':
        (output / 'example/final.srt').write_text('changed after prior terminal')
    elif damage == 'original_source':
        (tmp_path / 'originals/source.mp4').write_bytes(b'changed source')
    elif damage == 'paid_link':
        link = output / 'runtime-state/state/gemini-paid-backup'
        link.unlink()
        link.symlink_to(tmp_path / 'other-paid-ledger')
    elif damage == 'state_metadata':
        data = json.loads((output / 'replay-state.json').read_text())
        data['production_retry_history_consumed'] = True
        write(output / 'replay-state.json', data)
    elif damage == 'unfinished':
        write(output / 'resume-status.json', {'status': 'RUNNING'})
    elif damage == 'same_code':
        identity = 'old-test-code'
    elif damage == 'rescore':
        prior['rescore_source'] = '/not-original'
        write(output / 'evaluation.json', prior)
    else:
        (output / 'evaluation.json').unlink()
    before = unknown.read_bytes(), (paid / 'usage.json').read_bytes()
    with pytest.raises(ValueError):
        evaluation.prepare_experiment_resume(output, fixture, cases, identity, runtime)
    assert not (output.parent / 'resume-history').exists()
    assert (unknown.read_bytes(), (paid / 'usage.json').read_bytes()) == before


def test_run_package_reuses_current_text_and_actual_authority_via_ordinary_gate(tmp_path, monkeypatch):
    from src.autoslice import producer_text_pipeline as pipeline
    from src.autoslice.final_review_meaning_policy import POLICY_VERSION
    output, runtime, fixture, cases, package, unknown, paid = specimen(tmp_path)
    evaluation.prepare_experiment_resume(output, fixture, cases, 'new-test-code', runtime)
    observed = []
    def build(**kwargs):
        assert kwargs['authority']['private_resume_marker'] == 'preserve actual prior attempt'
        assert 'ORACLE_MUST_NOT_REACH_REVIEWER' not in json.dumps(kwargs['spec'])
        def reviewer(text, authority, start, end):
            observed.append((text, deepcopy(authority)))
            # The actual ordinary gate remains in place. An unresolved empty
            # synthetic finding cannot become an accepted production release.
            return {'schema_version': 'final-review-audit.v2', 'meaning_policy': POLICY_VERSION,
                    'reviewed_srt_sha256': 'sha256:' + hashlib.sha256(text.encode()).hexdigest(),
                    'status': 'FLAGGED', 'release_gate': 'BLOCK', 'validated_finding_count': 0,
                    'findings': [], 'reason_codes': ['SYNTHETIC_REVIEW_NOT_RELEASE']}
        return reviewer
    monkeypatch.setattr(pipeline, 'build_prepared_exact_final_reviewer', build)
    folder = output / 'example'
    old_review = (folder / 'review-01.json').read_bytes()
    old_unknown = unknown.read_bytes()
    old_run = (folder / 'run.json').read_bytes()
    preparing = []
    write_json = evaluation.write_json
    def capture_preparing(path, value):
        write_json(path, value)
        if path == folder / 'run.json' and value.get('phase') == 'PACKAGE_PREPARATION_STARTED':
            preparing.append(path.read_bytes())
    monkeypatch.setattr(evaluation, 'write_json', capture_preparing)
    result = evaluation.run_package(package, folder, tmp_path, {}, resume=True,
                                    checkpoint_identity={'code_identity': 'synthetic-resume'})
    assert observed and observed[0][0] == CURRENT
    assert observed[0][1]['private_resume_marker'] == 'preserve actual prior attempt'
    assert (folder / 'final.srt').read_text() == CURRENT
    assert (folder / 'review-01.json').read_bytes() == old_review
    assert (folder / 'review-02.json').is_file()
    assert unknown.read_bytes() == old_unknown
    assert result['parent_run_sha256'] == hashlib.sha256(old_run).hexdigest()
    assert preparing and result['parent_run_sha256'] != hashlib.sha256(preparing[0]).hexdigest()
    assert result['resume_parent_run']['validation'] == 'VALIDATED'
    assert Path(result['resume_parent_run']['archive_path']).read_bytes() == old_run
    first_stage = json.loads((folder / 'regression-checkpoints/stage-0001.json').read_bytes())
    assert first_stage['run']['resume_parent_run']['sha256'] == result['parent_run_sha256']
    assert first_stage['run']['resume_parent_run']['validation'] == 'PENDING'
    assert 'parent_run_sha256' not in first_stage['run']
    assert result['provider_usage']['route_counts']['gemini_paid_http_attempts'] == 1
    assert result['provider_usage']['continuation_delta']['gemini_http_attempts'] == []
    assert result['provider_usage']['continuation_delta']['model_requests'] == 0
    assert result['provider_usage']['cost_usd'] is None
    assert result['would_release'] is False



@pytest.mark.parametrize('failure', ['constructor_error', 'constructor_interrupt',
                                     'current_text_drift', 'malformed_parent'])
def test_resume_preparation_failure_keeps_exact_parent_evidence(tmp_path, monkeypatch, failure):
    from src.autoslice import producer_text_pipeline as pipeline
    output, runtime, fixture, cases, package, unknown, paid = specimen(tmp_path)
    evaluation.prepare_experiment_resume(output, fixture, cases, 'new-test-code', runtime)
    folder = output / 'example'
    if failure == 'current_text_drift':
        (folder / 'final.srt').write_text('changed after resume admission')
    elif failure == 'malformed_parent':
        (folder / 'run.json').write_bytes(b'{invalid prior run')
    old_run = (folder / 'run.json').read_bytes()
    old_unknown, old_paid = unknown.read_bytes(), (paid / 'usage.json').read_bytes()
    reached = []
    def build(**kwargs):
        reached.append(True)
        if failure == 'constructor_interrupt':
            raise KeyboardInterrupt('synthetic constructor interruption')
        raise ValueError('synthetic constructor failure')
    monkeypatch.setattr(pipeline, 'build_prepared_exact_final_reviewer', build)
    args = (package, folder, tmp_path, {})
    kwargs = {'resume': True, 'checkpoint_identity': {'code_identity': 'synthetic-failure'}}
    if failure == 'constructor_interrupt':
        with pytest.raises(KeyboardInterrupt):
            evaluation.run_package(*args, **kwargs)
    else:
        evaluation.run_package(*args, **kwargs)
    run = json.loads((folder / 'run.json').read_bytes())
    parent = run['resume_parent_run']
    assert parent['sha256'] == hashlib.sha256(old_run).hexdigest()
    assert parent['bytes'] == len(old_run)
    assert Path(parent['archive_path']).read_bytes() == old_run
    assert run['quality_outcome'] == 'EVALUATION_ERROR' and run['would_release'] is None
    assert run['error'] and run['preparation_status'] == 'FAILED'
    validated = failure.startswith('constructor_')
    assert bool(reached) == validated
    assert parent['validation'] == ('VALIDATED' if validated else 'REJECTED')
    assert run.get('parent_run_sha256') == (parent['sha256'] if validated else None)
    stages = [json.loads(p.read_bytes()) for p in sorted(
        (folder / 'regression-checkpoints').glob('stage-*.json'))]
    assert stages[0]['run']['resume_parent_run']['sha256'] == parent['sha256']
    assert stages[-1]['run']['resume_parent_run'] == parent
    assert stages[-1]['run'].get('parent_run_sha256') == run.get('parent_run_sha256')
    assert stages[-1]['phase'] == ('PACKAGE_PREPARATION_INTERRUPTED'
        if failure == 'constructor_interrupt' else 'PACKAGE_PREPARATION_FAILED')
    assert unknown.read_bytes() == old_unknown and (paid / 'usage.json').read_bytes() == old_paid


@pytest.mark.parametrize('failure', ['write_error', 'conflicting_archive', 'symlink_archive'])
def test_resume_parent_archive_failure_never_overwrites_original(tmp_path, monkeypatch, failure):
    from src.autoslice import argon_escalation_storage as store
    from src.autoslice import producer_text_pipeline as pipeline
    output, _, _, _, package, _, _ = specimen(tmp_path)
    folder = output / 'example'
    before = (folder / 'run.json').read_bytes()
    mode = folder.stat().st_mode
    archive = folder / 'run-history' / (hashlib.sha256(before).hexdigest() + '.json')
    archive.parent.mkdir(mode=0o700)
    if failure == 'write_error':
        def fail_save(path, raw):
            raise OSError('synthetic immutable write failure')
        monkeypatch.setattr(store, 'immutable', fail_save)
    elif failure == 'conflicting_archive':
        archive.write_bytes(b'conflicting archived raw')
        archive.chmod(0o600)
    else:
        archive.symlink_to(folder / 'run.json')
    monkeypatch.setattr(pipeline, 'build_prepared_exact_final_reviewer',
                        lambda **kwargs: pytest.fail('constructor reached before durable parent'))
    with pytest.raises((OSError, ValueError)):
        evaluation.run_package(package, folder, tmp_path, {}, resume=True)
    assert (folder / 'run.json').read_bytes() == before
    assert folder.stat().st_mode == mode
    assert not (folder / 'regression-checkpoints').exists()


def test_resume_reuses_parent_archive_after_precheckpoint_interruption(tmp_path, monkeypatch):
    from src.autoslice import argon_escalation_storage as store
    from src.autoslice import producer_text_pipeline as pipeline
    output, _, _, _, package, _, _ = specimen(tmp_path)
    folder = output / 'example'
    before = (folder / 'run.json').read_bytes()
    saved = []
    immutable = store.immutable
    def save_then_interrupt(path, raw):
        immutable(path, raw)
        saved.append(path)
        raise KeyboardInterrupt('synthetic interruption before first checkpoint')
    monkeypatch.setattr(store, 'immutable', save_then_interrupt)
    with pytest.raises(KeyboardInterrupt):
        evaluation.run_package(package, folder, tmp_path, {}, resume=True)
    assert (folder / 'run.json').read_bytes() == before
    assert len(saved) == 1 and saved[0].read_bytes() == before
    monkeypatch.setattr(store, 'immutable', immutable)
    def constructor_failure(**kwargs):
        raise ValueError('synthetic stop after archive reuse')
    monkeypatch.setattr(pipeline, 'build_prepared_exact_final_reviewer', constructor_failure)
    result = evaluation.run_package(package, folder, tmp_path, {}, resume=True)
    assert result['parent_run_sha256'] == hashlib.sha256(before).hexdigest()
    assert Path(result['resume_parent_run']['archive_path']) == saved[0]
    assert saved[0].read_bytes() == before


def test_worker_lock_rejects_a_second_writer_before_loading_any_provider(tmp_path, monkeypatch):
    output = tmp_path / 'results'
    called = []
    monkeypatch.setattr(evaluation, '_worker_body', lambda args: called.append(args) or 0)
    args = SimpleNamespace(output=output)
    with (tmp_path / '.results.worker.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            evaluation.worker(args)
    assert called == []
    assert evaluation.worker(args) == 0 and called == [args]


def test_resume_is_worker_only_and_cannot_run_rescore(tmp_path):
    with pytest.raises(SystemExit) as exc:
        evaluation.main(['--resume-existing', '--output', str(tmp_path / 'out')])
    assert exc.value.code == 2
    assert not (tmp_path / 'out').exists()


def test_frozen_scoring_and_input_contract_unchanged_from_approved_head():
    path = 'scripts/subtitle_regression_eval.py'
    old = subprocess.check_output(['git', 'show', 'ada052f06d0b57d2733c96168a15b7fec1a64068:' + path], text=True)
    def functions(text):
        return {node.name: ast.dump(node, include_attributes=False) for node in ast.parse(text).body
                if isinstance(node, ast.FunctionDef)}
    before, after = functions(old), functions(Path(path).read_text())
    names = ('load_cases', 'body_ms', 'validate_original_cases', 'summarize_case', 'aggregate', 'rescore')
    assert all(before[name] == after[name] for name in names)


@pytest.mark.parametrize('already_repaired', [True, False])
def test_worker_continues_only_unrepaired_target_with_unchanged_scoring(tmp_path, monkeypatch, already_repaired):
    import sys
    output, runtime, fixture, cases, package, unknown, paid = specimen(tmp_path)
    case = cases[0]
    case.update(candidate_id=package['candidate_id'], source={'quote': 'Synthetic case, not real testimony'},
                cue_ids=[1], body_window_ms=[1000, 3000], finished_window_ms=[2000, 4000],
                wrong_text='合成初始文字', expected={'required_severity': 'critical', 'correct_text': '合成当前文字'})
    write(fixture, {'schema_version': 'subtitle-regressions.v1', 'packages': {'example': package}, 'cases': cases})
    previous = json.loads((output / 'evaluation.json').read_text())
    previous['fixture_sha256'] = evaluation.digest(fixture)
    write(output / 'evaluation.json', previous)
    folder = output / 'example'
    prior = json.loads((folder / 'run.json').read_text())
    text = '合成当前文字' if already_repaired else '合成初始文字'
    (folder / 'final.srt').write_text(CURRENT if already_repaired else SRT)
    cue = {'index': 1, 'start_ms': 1000, 'end_ms': 3000, 'text': text}
    prior.update(final_srt_sha256=evaluation.digest(folder / 'final.srt'), final_cues=[cue],
                 reviews=[{'discovery': {'status': 'COMPLETE'}, 'release_gate': 'BLOCK',
                           'findings': [], 'evaluation_input_cues': [cue]}])
    write(folder / 'run.json', prior)
    old_run_sha = evaluation.digest(folder / 'run.json')
    old_unknown = unknown.read_bytes()
    requested = []
    def run_package(p, out, root, env, *, resume):
        requested.append(p)
        assert resume is True
        assert 'expected' not in p and 'ORACLE' not in json.dumps(p)
        assert json.loads((output / 'resume-status.json').read_text())['status'] == 'RUNNING'
        return prior  # No synthetic success is invented for the unrepaired case.
    monkeypatch.setattr(evaluation, 'run_package', run_package)
    monkeypatch.setattr(evaluation, 'load_production_gemini_environment', lambda: {
        'source': '/synthetic/env', 'configured_key_count': 0, 'paid_backup_configured': False})
    monkeypatch.setattr(sys, 'path', list(sys.path))
    monkeypatch.setenv('AUTOSLICE_FINAL_REVIEW_RUNTIME_ROOT', str(runtime))
    monkeypatch.chdir(evaluation.ROOT)
    args = SimpleNamespace(output=output, cases=fixture, case=['synthetic-case'],
        code_root=evaluation.ROOT, code_identity='new-test-code', runtime_root=str(runtime), resume_existing=True)
    code = evaluation.worker(args)
    result = json.loads((output / 'evaluation.json').read_text())
    assert len(requested) == (0 if already_repaired else 1)
    assert code == (0 if already_repaired else 1)
    if already_repaired:
        assert result['results'][0]['outcome'] == 'PASS_REPAIRED'
        assert result['reused_package_runs']['example']['source_code_identity'] == 'old-test-code'
        assert result['reused_package_runs']['example']['new_provider_calls'] == 0
        assert evaluation.digest(folder / 'run.json') == old_run_sha
    else:
        assert result['results'][0]['outcome'] != 'PASS_REPAIRED'
        assert result['reused_package_runs'] == {}
    assert unknown.read_bytes() == old_unknown
    status = json.loads((output / 'resume-status.json').read_text())
    assert status['status'] == 'COMPLETED' and status['evaluation_sha256'] == evaluation.digest(output / 'evaluation.json')


def test_resume_real_private_runtime_leaf_retains_terminal_history_and_budget(tmp_path):
    output, runtime, fixture, cases, _, unknown, paid = specimen(tmp_path, inside_runtime=True)
    old_evaluation = (output / "evaluation.json").read_bytes()
    old_unknown = unknown.read_bytes()
    old_paid = (paid / "usage.json").read_bytes()
    record = evaluation.prepare_experiment_resume(output, fixture, cases, "new-test-code", runtime)
    assert (Path(record["archive"]) / "evaluation.json").read_bytes() == old_evaluation
    assert record["history_reset"] is False
    assert unknown.read_bytes() == old_unknown
    assert (paid / "usage.json").read_bytes() == old_paid


@pytest.mark.parametrize("relative", [
    ".", "current", "state/example/run", "out/example/run", "versions/example/repo",
    "private-runs", "private-runs/task", "private-runs/task/run/child",
    "private-runs-other/task/run",
])
def test_resume_runtime_path_exception_cannot_enter_production_or_parent_directories(tmp_path, relative):
    runtime = tmp_path / "runtime"
    output = runtime / relative
    output.mkdir(parents=True, exist_ok=True)
    with pytest.raises(ValueError, match="existing private experiment"):
        evaluation.prepare_experiment_resume(output, tmp_path / "unread.json", [], "new", runtime)
    assert not list(runtime.rglob("ARCHIVE.json"))


@pytest.mark.parametrize("alias", ["output", "task", "private_root", "outside_ancestor"])
def test_resume_private_runtime_leaf_rejects_directory_aliases(tmp_path, alias):
    output, runtime, fixture, cases, _, _, _ = specimen(tmp_path, inside_runtime=True)
    if alias == "outside_ancestor":
        link = tmp_path / "alias"
        link.symlink_to(output.parent, target_is_directory=True)
        chosen = link / output.name
    else:
        chosen = output
        original = {"output": output, "task": output.parent,
                    "private_root": output.parent.parent}[alias]
        moved = original.with_name(original.name + "-physical")
        original.rename(moved)
        original.symlink_to(moved, target_is_directory=True)
    with pytest.raises(ValueError, match="existing private experiment"):
        evaluation.prepare_experiment_resume(chosen, fixture, cases, "new-test-code", runtime)
    assert not list(runtime.rglob("ARCHIVE.json"))


@pytest.mark.parametrize("damage", ["no_terminal", "same_code", "source_drift", "unfinished"])
def test_resume_private_runtime_path_does_not_bypass_existing_identity_gates(tmp_path, damage):
    output, runtime, fixture, cases, package, unknown, paid = specimen(tmp_path, inside_runtime=True)
    before_unknown = unknown.read_bytes()
    before_paid = (paid / "usage.json").read_bytes()
    identity = "new-test-code"
    if damage == "no_terminal":
        (output / "evaluation.json").unlink()
    elif damage == "same_code":
        identity = "old-test-code"
    elif damage == "source_drift":
        Path(package["media_path"]).write_bytes(b"changed synthetic source")
    else:
        write(output / "resume-status.json", {"status": "RUNNING"})
    with pytest.raises(ValueError):
        evaluation.prepare_experiment_resume(output, fixture, cases, identity, runtime)
    assert not (output.parent / "resume-history").exists()
    assert unknown.read_bytes() == before_unknown
    assert (paid / "usage.json").read_bytes() == before_paid


def test_normal_off_resume_rechecks_prior_correct_targets_on_the_new_code(tmp_path, monkeypatch):
    """Route-only synthetic control; an old PASS is not a new-code live run."""
    import sys
    from src.autoslice import native_audio_runtime, agy_gemini_client
    output, runtime, fixture, cases, package, unknown, paid = specimen(tmp_path)
    case = cases[0]
    case.update(candidate_id=package["candidate_id"], source={"quote": "Synthetic case"},
                cue_ids=[1], body_window_ms=[1000, 3000], finished_window_ms=[2000, 4000],
                wrong_text="合成初始文字",
                expected={"required_severity": "critical", "correct_text": "合成当前文字"})
    write(fixture, {"schema_version": "subtitle-regressions.v1",
                    "packages": {"example": package}, "cases": cases})
    previous = json.loads((output / "evaluation.json").read_text())
    previous["fixture_sha256"] = evaluation.digest(fixture)
    write(output / "evaluation.json", previous)
    folder = output / "example"
    prior = json.loads((folder / "run.json").read_text())
    cue = {"index": 1, "start_ms": 1000, "end_ms": 3000, "text": "合成当前文字"}
    prior.update(final_cues=[cue], reviews=[{"discovery": {"status": "COMPLETE"},
                 "release_gate": "BLOCK", "findings": [], "evaluation_input_cues": [cue]}])
    write(folder / "run.json", prior)
    before_unknown, before_paid = unknown.read_bytes(), (paid / "usage.json").read_bytes()
    calls = []
    def run_package(p, out, root, env, *, resume, normal_samples):
        assert resume is True and len(normal_samples) == 1
        assert "expected" not in p and "ORACLE" not in json.dumps(normal_samples)
        calls.append(normal_samples[0]["case_id"])
        return prior
    monkeypatch.setattr(evaluation, "run_package", run_package)
    monkeypatch.setattr(evaluation, "load_production_gemini_environment",
                        lambda: {"source": "/synthetic/env", "configured_key_count": 3,
                                 "paid_backup_configured": True})
    monkeypatch.setattr(native_audio_runtime, "native_audio_readiness",
                        lambda _root: [{"ready": True}, {"ready": True}])
    monkeypatch.setattr(agy_gemini_client, "resolve_local_agy_binary", lambda _root: "/synthetic/agy")
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.chdir(evaluation.ROOT)
    args = SimpleNamespace(output=output, cases=fixture, case=["synthetic-case"],
        code_root=evaluation.ROOT, code_identity="new-test-code",
        runtime_root=str(runtime), resume_existing=True, normal_off=True)
    assert evaluation.worker(args) == 0
    report = json.loads((output / "evaluation.json").read_text())
    assert calls == ["synthetic-case"]
    assert report["reused_package_runs"] == {}
    assert unknown.read_bytes() == before_unknown
    assert (paid / "usage.json").read_bytes() == before_paid
