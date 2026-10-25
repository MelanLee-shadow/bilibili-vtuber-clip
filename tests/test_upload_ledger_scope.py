from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts.authorized_upload import append_ledger, ledger_guard
from src.autoslice.upload_ledger_scope import content_identity, row_matches_target


def bound_manifest(tmp_path: Path, candidate: str, date='2026-10-13', *, pieces=None, **aliases):
    root = tmp_path / 'out' / date / candidate
    root.mkdir(parents=True, exist_ok=True)
    record = root / 'record.json'
    payload = {'candidate_id': candidate, 'recording_date': date, **aliases}
    if pieces is not None:
        transcript = '1\n00:00:00,000 --> 00:00:01,000\n测试\n'
        context = {'schema_version': 'lidousha-clip-context.v1', 'candidate_id': candidate,
                   'recording_date': date, 'pieces': pieces, 'mutation_authorized': False,
                   'whole_clip_draft_srt': transcript,
                   'whole_clip_draft_srt_sha256': 'sha256:' + hashlib.sha256(transcript.encode()).hexdigest(),
                   'retrieval_budget': {'whole_clip_transcript_truncated': False}}
        context['context_sha256'] = 'sha256:' + hashlib.sha256(json.dumps(
            context, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        context_path = root / 'context.json'
        context_path.write_text(json.dumps(context))
        payload.update(clip_context_path=str(context_path), clip_context_payload_sha256=context['context_sha256'])
    record.write_text(json.dumps(payload))
    return {'package_attestation': {'package_root': str(root), 'record': {
        'path': str(record), 'sha256': hashlib.sha256(record.read_bytes()).hexdigest()}}}


def test_frozen_identity_keeps_alias_and_source_holds_after_files_disappear(tmp_path):
    pieces = [{'source_media_sha256': 'sha256:' + 'a' * 64, 'start_ms': 1000, 'end_ms': 9000}]
    original = bound_manifest(tmp_path, 'original', pieces=pieces)
    frozen = content_identity(original)
    assert frozen['recording_date'] == '2026-10-13'
    assert frozen['source_windows'] == [{'source_sha256': 'a' * 64, 'start_ms': 1000, 'end_ms': 9000}]
    row = {'video_sha256': 'old-video', 'content_identity': frozen}
    # Lost historical files cannot clear the durable hold or block other products.
    for path in Path(original['package_attestation']['package_root']).iterdir():
        path.unlink()
    alias = bound_manifest(tmp_path, 'new-render', source_candidate_id='original')
    assert row_matches_target(row, 'new-video', alias)
    moved_alias = bound_manifest(tmp_path, 'moved-render', date='2026-10-14', source_candidate_id='original')
    assert row_matches_target(row, 'moved-video', moved_alias)
    overlap = bound_manifest(tmp_path, 'different-name', pieces=[{
        'source_media_sha256': 'sha256:' + 'a' * 64, 'start_ms': 8000, 'end_ms': 12000}])
    assert row_matches_target(row, 'different-video', overlap)
    disjoint = bound_manifest(tmp_path, 'next-clip', pieces=[{
        'source_media_sha256': 'sha256:' + 'a' * 64, 'start_ms': 9000, 'end_ms': 12000}])
    assert not row_matches_target(row, 'next-video', disjoint)
    next_day = bound_manifest(tmp_path, 'original', date='2026-10-14')
    assert not row_matches_target(row, 'next-day-video', next_day)


def test_missing_legacy_manifest_holds_its_locator_without_global_dependency(tmp_path):
    ledger = tmp_path / 'ledger.jsonl'
    append_ledger(ledger, {'video_sha256': 'old-video', 'rc': 1,
                          'manifest': '/retired/out/2026-10-13/held/clip.upload_manifest.json'})
    before = ledger.read_bytes()
    same = bound_manifest(tmp_path, 'held')
    independent = bound_manifest(tmp_path, 'independent')
    assert ledger_guard(ledger, 'reencoded', manifest=same)[0] == 'unresolved'
    assert ledger_guard(ledger, 'new-video', manifest=independent) == (None, None, [])
    assert ledger_guard(ledger, 'old-video')[0] == 'unresolved'
    assert ledger.read_bytes() == before


def test_bound_context_drift_is_refused_instead_of_losing_source_identity(tmp_path):
    manifest = bound_manifest(tmp_path, 'candidate', pieces=[{
        'source_media_sha256': 'sha256:' + 'a' * 64, 'start_ms': 100, 'end_ms': 200}])
    path = Path(manifest['package_attestation']['package_root']) / 'context.json'
    context = json.loads(path.read_text())
    context['pieces'][0]['end_ms'] = 250
    path.write_text(json.dumps(context))
    with pytest.raises(ValueError, match='DIGEST_MISMATCH'):
        content_identity(manifest)


def test_malformed_scope_and_unidentified_ledger_rows_remain_global_errors(tmp_path):
    ledger = tmp_path / 'ledger.jsonl'
    append_ledger(ledger, {'video_sha256': 'old-video', 'rc': 1, 'content_identity': {'schema_version': 'wrong'}})
    assert ledger_guard(ledger, 'independent')[2]
    ledger.write_text('{"rc":1}\n')
    assert ledger_guard(ledger, 'independent')[2]


@pytest.mark.parametrize(('rc', 'bvid', 'expected'), [
    (1, None, 'unresolved'), (6, 'BV1ABC234XYZ', 'posted_unverified'),
    (0, 'BV1ABC234XYZ', 'uploaded')])
def test_existing_content_status_also_holds_a_new_encoding(tmp_path, rc, bvid, expected):
    manifest = bound_manifest(tmp_path, 'candidate')
    ledger = tmp_path / 'ledger.jsonl'
    append_ledger(ledger, {'video_sha256': 'old-encoding', 'rc': rc, 'bvid': bvid,
                          'content_identity': content_identity(manifest)})
    assert ledger_guard(ledger, 'new-encoding', manifest=manifest)[0] == expected
