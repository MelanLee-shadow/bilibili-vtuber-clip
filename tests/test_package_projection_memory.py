"""Progress snapshots and immutable ledger writes must not unfold history."""
import gc
import tracemalloc

import pytest

from src.autoslice import finding_closure as closure
from src.autoslice import finding_closure_shell as shell
# Consumer imports are setup costs, outside the measured snapshot operation.
from src.autoslice import producer_package_finalization  # noqa: F401
from src.autoslice import human_sequence_alignment  # noqa: F401
from src.autoslice.argon_escalation_storage import MAX_ARTIFACT_BYTES
from src.autoslice.canonical_json_hash import canonical_sha256
from src.autoslice.finding_closure_ports import RetainedObject
from src.autoslice.review_evidence_store import load_review

SRT = '1\n00:00:00,000 --> 00:00:01,000\n原话\n'


def history():
    value = {'rows': [{'index': i, 'words': [i, '原答', None]} for i in range(100)]}
    for _ in range(10):
        value = {'previous': value, 'same_original': value}
    return value


class ProjectionFinished(Exception):
    pass


def project_once(tmp_path, monkeypatch, authority, baseline):
    configuration = RetainedObject.from_value({'legacy_prefix': None})
    reviewer = shell.PerFindingReviewer(lambda _: shell.ClosureSession(
        'a' * 64, object(), configuration, ('recorded',)))
    path = tmp_path / 'current.srt'
    path.write_text(SRT)

    def run(runner):
        runner.on_projection(runner.initial)
        raise ProjectionFinished

    monkeypatch.setattr(shell.ClosureRunner, 'run', run)
    call = shell.PackageCall(reviewer, 'candidate', tmp_path, path, authority,
        tmp_path / 'authority.json', 0, 1000, baseline=baseline,
        baseline_path=tmp_path / 'baseline.json')
    with pytest.raises(ProjectionFinished):
        shell.run_package_closure(call)


def test_progress_only_package_snapshot_keeps_memory_bounded_and_exact_content(tmp_path, monkeypatch):
    original = history()
    original_sha = canonical_sha256(original)
    authority, baseline = {'history': original}, {'history': original}
    tracemalloc.start()
    try:
        project_once(tmp_path, monkeypatch, authority, baseline)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    # The protected blob reader may reserve MAX_ARTIFACT_BYTES; retaining a
    # separately expanded history at each progress event is unnecessary.
    assert peak < MAX_ARTIFACT_BYTES + 4_000_000, peak
    assert canonical_sha256(original) == original_sha
    for filename, value in [('authority.json', authority), ('baseline.json', baseline)]:
        restored = load_review(tmp_path / filename)
        assert canonical_sha256(restored) == canonical_sha256(value)
        assert canonical_sha256(restored['history']) == original_sha
        assert restored['finding_closure_progress']['applied_patch_count'] == 0
    assert authority['final_output_srt_sha256'] == closure.sha(SRT)
    assert baseline['post_exact_final_cpa_output_sha256'] == closure.sha(SRT)


def test_progress_projection_failure_leaves_authority_and_baseline_unmodified(tmp_path, monkeypatch):
    from src.autoslice import review_evidence_store
    authority, baseline = {'history': history()}, {'history': history()}
    before = [canonical_sha256(authority), canonical_sha256(baseline)]
    original = review_evidence_store.persist_document

    def fail_baseline(path, document):
        if path.name == 'baseline.json':
            raise OSError('test storage failure')
        return original(path, document)

    monkeypatch.setattr(review_evidence_store, 'persist_document', fail_baseline)
    with pytest.raises(OSError, match='test storage failure'):
        project_once(tmp_path, monkeypatch, authority, baseline)
    assert [canonical_sha256(authority), canonical_sha256(baseline)] == before


def test_immutable_ledger_writer_preserves_graph_memory_and_public_value_isolation(tmp_path):
    initial = closure.initial_state(source_sha256='a' * 64, srt_text=SRT,
        scan_channels=('recorded',))
    ledger = shell.FileLedger(tmp_path / 'ledger', initial)
    document = RetainedObject.from_value(history())
    tracemalloc.start()
    try:
        digest = ledger.put(document)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < MAX_ARTIFACT_BYTES + 4_000_000, peak
    assert ledger.get(digest) == document
    public = ledger.get(digest).value()
    public['previous']['changed'] = True
    assert 'changed' not in public['same_original']
    assert ledger.get(digest).sha256 == digest


@pytest.mark.parametrize('invalid', [False, True])
def test_validation_key_releases_scratch_digests_before_cyclic_gc(invalid):
    from src.autoslice.validation_memo import validation_content_key
    value = {'a': [{'index': i, 'values': [i, str(i), None]} for i in range(1000)]}
    if invalid:
        value['z'] = object()
    else:
        expected = validation_content_key(value)
    gc.collect()
    was_enabled = gc.isenabled()
    gc.disable()
    tracemalloc.start()
    try:
        for _ in range(20):
            if invalid:
                with pytest.raises(TypeError, match='non-JSON validation input'):
                    validation_content_key(value)
            else:
                assert validation_content_key(value) == expected
        retained, _ = tracemalloc.get_traced_memory()
        assert retained < 100_000, retained
    finally:
        tracemalloc.stop()
        if was_enabled:
            gc.enable()
        gc.collect()
