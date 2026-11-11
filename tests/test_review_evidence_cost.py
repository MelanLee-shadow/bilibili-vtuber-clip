"""Cold-read work reuse preserves file, binding and publication decisions."""
import hashlib
import os
from pathlib import Path
import time

import pytest

from src.autoslice.continuation_subchecks import cold_read_session, end_cold_read
from src.autoslice import evidence_identity as identity
from src.autoslice.validation_memo import memoized_validation


@pytest.mark.parametrize('provider', ['mai', 'moss'])
def test_generated_native_range_reuses_bytes_but_not_verdicts(tmp_path, monkeypatch, provider):
    from tests.test_native_context_witness import _install_range_writer, _range_requests
    from src.autoslice import native_context_witness as native
    from src.autoslice.independent_context_adjudication import validate_native_range_context_observation

    source = tmp_path/'source.mp4'
    source.write_bytes(b'new ordinary range source')
    dispatches = []
    digest = _install_range_writer(monkeypatch, source, tmp_path/'cache', dispatches=dispatches)
    check, request = _range_requests(digest)
    wrapper = native.build_independent_context_witnesses(
        fallback=lambda r: {}, source_media=source, output_dir=tmp_path/'out',
        providers=(provider,), native_observation_cache_root=tmp_path/'cache')
    observation, = wrapper.observe_range_context(check_request=check, range_witness_request=request)
    audio = Path(observation['artifact_bindings']['audio_clip_path'])
    original = {p: p.read_bytes() for p in (tmp_path/'out').rglob('*.json')}
    time.sleep(.02)  # Newly generated files must become cold before hash reuse.
    opens = []
    real = Path.open
    def counted(path, mode='r', *a, **kw):
        if path == audio and mode == 'rb':
            opens.append(path)
        return real(path, mode, *a, **kw)
    monkeypatch.setattr(Path, 'open', counted)
    with cold_read_session():
        expected = validate_native_range_context_observation(check['whole_clip_current_srt'], check, observation)
        assert expected[0] is not None, expected[1]
        first = len(opens)
        for _ in range(4):
            assert validate_native_range_context_observation(check['whole_clip_current_srt'], check, observation) == expected
        assert len(opens) == first  # Guard: the old consumer reopens every time.
        wrong = dict(check, source_media_timeline_offset_ms=101)
        invalid, reason = validate_native_range_context_observation(check['whole_clip_current_srt'], wrong, observation)
        assert invalid is None and reason is not None
        assert expected[0]['mutation_authorized'] is False
        end_cold_read()
        assert validate_native_range_context_observation(check['whole_clip_current_srt'], check, observation) == expected
        assert len(opens) > first
    assert len(dispatches) == 1
    assert {p: p.read_bytes() for p in original} == original


@pytest.mark.parametrize('damage', ['same_inode', 'replace', 'delete'])
@pytest.mark.parametrize('role', ['source', 'frame', 'evidence'])
def test_ordinary_caption_successor_memo_rechecks_each_dependency(tmp_path, role, damage):
    from tests.test_nested_caption_final_review_history import input_case, consume, sha
    from src.autoslice.final_review_contract import _validate_exact_final_cpa_self_heal, FinalReviewContractError
    case = input_case(tmp_path)
    chat, _, source, frame, evidence, *_ = case
    after = consume(tmp_path, case)
    def validate():
        _validate_exact_final_cpa_self_heal(chat, expected_srt_sha256='sha256:'+sha(after.encode()), allow_pending=True)
    with cold_read_session():
        validate()
        validate()
        path = dict(source=source, frame=frame, evidence=evidence)[role]
        before = path.stat()
        if damage == 'same_inode':
            raw = path.read_bytes()
            path.write_bytes(bytes([raw[0] ^ 1])+raw[1:])
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
            assert path.stat().st_ino == before.st_ino and path.stat().st_size == before.st_size
        elif damage == 'replace':
            other = tmp_path/'replacement'
            other.write_bytes(path.read_bytes())
            other.replace(path)
            validate()  # A byte-identical replacement revalidates, still passes.
            path.write_bytes(b'changed replacement')
        else:
            path.unlink()
        for _ in range(2):
            with pytest.raises(FinalReviewContractError):
                validate()


def test_new_reader_inherits_nested_blob_dependencies_without_registration(tmp_path):
    from src.autoslice import review_evidence_store as store
    path = tmp_path/'new-document.json'
    original = {'request': {'new_member': 'UNKNOWN'*2000}}
    packed = store.persist_document(path, original)
    blob = Path(packed['request']['path'])
    calls = []
    @memoized_validation(lambda: str(path))
    def consume():
        calls.append(1)
        return store.review_from_bytes(identity.read_file_bytes(path), directory=path.parent)
    with cold_read_session():
        assert consume() == original
        assert consume() == original and len(calls) == 1
        blob.write_bytes(b'{}')
        with pytest.raises(ValueError, match='ARTIFACT_SHA_MISMATCH'):
            consume()


@pytest.mark.parametrize('delay_ns', [0, 1_000_000_000])
def test_shared_bytes_do_not_age_a_timestamp_collision_into_trust(tmp_path, monkeypatch, delay_ns):
    path = tmp_path/'racy'
    path.write_bytes(b'before')
    before = path.stat()
    now = [max(before.st_mtime_ns, before.st_ctime_ns)]
    placement = identity._placement(before)
    monkeypatch.setattr(identity, 'time_ns', lambda: now[0])
    monkeypatch.setattr(identity, '_placement', lambda info: placement)
    with cold_read_session():
        assert identity.read_file_bytes(path) == b'before'
        path.write_bytes(b'after!')
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        now[0] += delay_ns
        assert identity.read_file_bytes(path) == b'after!'
    assert identity.file_sha256(path) == hashlib.sha256(b'after!').hexdigest()
