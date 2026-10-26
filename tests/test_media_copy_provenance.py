"""Independent-copy provenance is consumed by the ordinary source and range entries."""
import json
from pathlib import Path

import pytest

from src.autoslice import independent_context_adjudication as independent
from src.autoslice.media_copy_provenance import materialize_media_copy, value_sha
from src.autoslice.producer_recut_provenance import prepared_boundary_clock_binding
from tests.test_final_recut_range_source_binding import package, sha, install_gemini
from tests.test_native_context_witness import _range_requests, _install_range_writer
from src.autoslice import native_context_witness as native


def fixture_input(tmp_path):
    media, recording, padded, proof = package(tmp_path)
    cid = "candidate-range-test"
    context = {"candidate_id": cid, "pieces": [{"source_media_sha256": sha(recording)}]}
    record = {"candidate_id": cid, "boundary_audit": {
        "final_start_ms": 5000, "final_end_ms": 35000,
        "boundary_semantic_review": {"candidate_id": cid, "final_endpoint_binding": {
            "status": "PASS", "final_start_ms": 5000, "final_end_ms": 35000}}}}
    return media, recording, context, record


def copy_media(media, context, record, target):
    return materialize_media_copy(source=media, target=target, expected_sha256=sha(media),
        clip_context=context, completed_record=record, candidate_id=context["candidate_id"])


def prepared(target, context, record):
    return prepared_boundary_clock_binding(media_path=target, clip_context=context,
        completed_record=record, candidate_id=context["candidate_id"])


def snapshots(paths):
    return {str(p): (p.read_bytes(), p.stat()) for p in paths}


def assert_unchanged(before):
    for name, (raw, info) in before.items():
        path = Path(name)
        assert path.read_bytes() == raw
        assert tuple(getattr(path.stat(), k) for k in ("st_dev", "st_ino", "st_mtime_ns", "st_ctime_ns")) == tuple(
            getattr(info, k) for k in ("st_dev", "st_ino", "st_mtime_ns", "st_ctime_ns"))


def test_copy_cannot_lose_candidate_at_range_ingress(tmp_path):
    media, recording, context, record = fixture_input(tmp_path)
    target = copy_media(media, context, record, tmp_path / "copy/media.mp4")
    request = {"clip_context_binding": {"source_media_sha256s": [sha(recording)]}}
    assert independent._range_media_source_binding(request, str(target), sha(target)) is None


def test_copy_preserves_source_chain_clock_and_cold_identity(tmp_path):
    media, recording, context, record = fixture_input(tmp_path)
    before = snapshots([p for p in tmp_path.iterdir() if p.is_file()])
    target = copy_media(media, context, record, tmp_path / "copy/media.mp4")
    receipt = json.loads(target.with_suffix(".provenance.json").read_bytes())
    assert receipt["operation"] == "INDEPENDENT_BYTE_COPY"
    assert receipt["provider_observation"] is False
    assert receipt["source"]["stat"]["st_ino"] != receipt["output"]["stat"]["st_ino"]
    assert target.read_bytes() == media.read_bytes()
    assert target.stat().st_nlink == 1
    assert str(recording) in {r["path"] for r in receipt["source_reads"]}
    assert str(media.with_suffix(".provenance.json")) in {r["path"] for r in receipt["source_reads"]}
    clock = prepared(target, context, record)
    assert clock["source_binding"]["role"] == "PRODUCER_FINAL_RECUT_FROM_RECORDING"
    assert clock["source_boundary_timeline_offset_ms"] == 5000
    assert clock["acoustic_timeline_offset_changed"] is False
    saved = snapshots([target, target.with_suffix(".provenance.json")])
    assert copy_media(media, context, record, target) == target
    assert prepared(target, context, record) == clock
    assert_unchanged(saved)
    assert_unchanged(before)


def test_copy_of_valid_copy_replays_all_original_proofs(tmp_path):
    media, recording, context, record = fixture_input(tmp_path)
    first = copy_media(media, context, record, tmp_path / "first/media.mp4")
    second = copy_media(first, context, record, tmp_path / "second/media.mp4")
    assert prepared(second, context, record)["source_boundary_timeline_offset_ms"] == 5000
    second_proof = json.loads(second.with_suffix(".provenance.json").read_bytes())
    paths = {r["path"] for r in second_proof["source_reads"]}
    assert {str(recording), str(first), str(first.with_suffix(".provenance.json"))} <= paths


def test_prepared_reviewer_binds_original_source_across_independent_copies(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from src.autoslice import producer_media, producer_text_pipeline as pipeline

    media, _, context, record = fixture_input(tmp_path)
    subtitles = tmp_path / "source.srt"
    subtitles.write_text("1\n00:00:00,000 --> 00:00:30,000\nsynthetic text\n")
    record["subtitle_path"] = str(subtitles)
    first = copy_media(media, context, record, tmp_path / "first/media.mp4")
    second = copy_media(first, context, record, tmp_path / "second/media.mp4")
    before = snapshots([p for p in tmp_path.rglob("*") if p.is_file()])
    captured = []

    def callback(**kwargs):
        captured.append(kwargs["prepared_review_binding"])
        return lambda *_args: {"scope": "SYNTHETIC_BINDING_ONLY"}

    monkeypatch.setattr(producer_media, "ffprobe_duration_ms", lambda _path: 30000)
    monkeypatch.setattr(pipeline, "_build_entity_verification_context",
                        lambda **_kwargs: SimpleNamespace(verify_confusable_entity=None))
    monkeypatch.setattr(pipeline, "_continuation_chat_from_context", lambda _context: ())
    monkeypatch.setattr(pipeline, "_build_exact_final_review_callback", callback)
    spec = {"clip_context": context, "comparison_ordinary_cache_only": True}
    for index, target in enumerate((media, first, second)):
        review = pipeline.build_prepared_exact_final_reviewer(
            spec=spec, out_root=tmp_path / f"review-{index}", cid=context["candidate_id"],
            padded=target, host="synthetic", authority={"final_review_audit": {}},
            completed_record=record, adapters=None, native_audio_state=None,
        )
        result = review(subtitles.read_text(), {}, 0, 30000)
        assert result["prepared_source_boundary_clock"] == prepared(target, context, record)
    assert len(captured) == 3 and captured[0] == captured[1] == captured[2]
    assert captured[0]["source_clock"] == prepared(media, context, record)
    assert captured[0]["completed_record_sha256"] == value_sha(record)
    assert captured[0]["clip_context_sha256"] == value_sha(context)
    assert_unchanged(before)


@pytest.mark.parametrize("damage", [
    "output_path", "output_hash", "source_path", "source_hash",
    "candidate", "record", "context", "endpoint", "clock", "source_binding", "chain_stat",
    "missing_chain", "operation", "provider_success", "release",
])
def test_resealed_copy_declaration_cannot_change_bound_identity(tmp_path, damage):
    media, _, context, record = fixture_input(tmp_path)
    target = copy_media(media, context, record, tmp_path / "copy/media.mp4")
    proof = target.with_suffix(".provenance.json")
    value = json.loads(proof.read_bytes())
    if damage == "output_path":
        value["output"]["path"] = str(media)
    elif damage == "output_hash":
        value["output"]["sha256"] = "0" * 64
    elif damage == "output_stat":
        value["output"]["stat"]["st_ctime_ns"] += 1
    elif damage == "source_path":
        value["source"]["path"] = str(target)
    elif damage == "source_hash":
        value["source"]["sha256"] = "0" * 64
    elif damage == "source_stat":
        value["source"]["stat"]["st_ino"] += 1
    elif damage in {"candidate", "record", "context"}:
        key = {"candidate": "candidate_id", "record": "completed_record_sha256",
               "context": "clip_context_sha256"}[damage]
        value["prepared_identity"][key] = "different"
    elif damage == "endpoint":
        value["source_clock"]["source_endpoint_ms"][0] += 1
    elif damage == "clock":
        value["source_clock"]["source_boundary_timeline_offset_ms"] += 1
    elif damage == "source_binding":
        value["source_binding"]["final_recut_interval_ms"][0] += 1
    elif damage == "chain_stat":
        value["source_reads"][-1]["stat"]["st_mtime_ns"] += 1
    elif damage == "missing_chain":
        value["source_reads"].pop()
    elif damage == "operation":
        value["operation"] = "FINAL_RECUT"
    elif damage == "provider_success":
        value["provider_observation"] = True
    else:
        value["release_authorized"] = True
    if damage in {"endpoint", "clock"}:
        clock = value["source_clock"]
        clock["receipt_sha256"] = value_sha({k: v for k, v in clock.items() if k != "receipt_sha256"})
    value["receipt_sha256"] = value_sha({k: v for k, v in value.items() if k != "receipt_sha256"})
    proof.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        prepared(target, context, record)


@pytest.mark.parametrize("damage", ["output_stat", "source_stat"])
def test_recorded_placement_is_not_copy_identity(tmp_path, damage):
    """Evidence identity is content (41, restore incident).

    A declaration whose recorded ctime/inode differs from the files' current
    placement is what a byte-identical restore produces; it stays the same
    copy. Content and restorable size/mtime are still bound.
    """
    media, _, context, record = fixture_input(tmp_path)
    target = copy_media(media, context, record, tmp_path / "copy/media.mp4")
    proof = target.with_suffix(".provenance.json")
    value = json.loads(proof.read_bytes())
    if damage == "output_stat":
        value["output"]["stat"]["st_ctime_ns"] += 1
    else:
        value["source"]["stat"]["st_ino"] += 1
    value["receipt_sha256"] = value_sha({k: v for k, v in value.items() if k != "receipt_sha256"})
    proof.write_text(json.dumps(value))
    assert prepared(target, context, record) is not None


@pytest.mark.parametrize("damage", ["missing", "old_sidecar", "changed_bytes", "changed_stat",
                                   "source_proof_stat", "source_recording_stat", "link"])
def test_actual_file_drift_and_unproven_copies_still_fail(tmp_path, damage):
    media, recording, context, record = fixture_input(tmp_path)
    target = copy_media(media, context, record, tmp_path / "copy/media.mp4")
    proof = target.with_suffix(".provenance.json")
    if damage == "missing":
        proof.unlink()
    elif damage == "old_sidecar":
        proof.write_bytes(media.with_suffix(".provenance.json").read_bytes())
    elif damage == "changed_bytes":
        target.write_bytes(b"unrelated media")
    elif damage == "changed_stat":
        target.write_bytes(target.read_bytes())
    elif damage == "source_proof_stat":
        source_proof = media.with_suffix(".provenance.json")
        source_proof.write_bytes(source_proof.read_bytes())
    elif damage == "source_recording_stat":
        recording.write_bytes(recording.read_bytes())
    else:
        target.unlink()
        target.symlink_to(media)
    with pytest.raises((ValueError, OSError)):
        prepared(target, context, record)


def test_orphan_copy_is_not_retroactively_signed(tmp_path):
    media, _, context, record = fixture_input(tmp_path)
    target = tmp_path / "copy/media.mp4"
    target.parent.mkdir(mode=0o700)
    target.write_bytes(media.read_bytes())
    before = snapshots([target])
    with pytest.raises(ValueError, match="INCOMPLETE_OR_UNPROVEN"):
        copy_media(media, context, record, target)
    assert not target.with_suffix(".provenance.json").exists()
    assert_unchanged(before)


@pytest.mark.parametrize("provider", ["mai", "moss", "gemini"])
def test_ordinary_range_observer_reuses_copy_and_keeps_acoustic_clock(tmp_path, monkeypatch, provider):
    media, recording, context, record = fixture_input(tmp_path)
    target = copy_media(media, context, record, tmp_path / "copy/media.mp4")
    check, broad = _range_requests(sha(recording))
    output = tmp_path / "observer"
    output.mkdir()
    dispatches = []
    if provider == "gemini":
        observe = install_gemini(monkeypatch, target, output, dispatches)
    else:
        _install_range_writer(monkeypatch, target, tmp_path / "cache", dispatches=dispatches)
        wrapper = native.build_independent_context_witnesses(
            fallback=lambda req: pytest.fail("copy provenance must not trigger fallback"),
            source_media=target, output_dir=output, providers=(provider,),
            native_observation_cache_root=tmp_path / "cache")
        def observe(request, window):
            return wrapper.observe_range_context(check_request=request, range_witness_request=window)
    first = observe(check, broad)
    assert first[0]["status"] == "OBSERVED", first[0].get("reason_code")
    validated, reason = independent.validate_range_acoustic_observation(
        check["whole_clip_current_srt"], check, first[0])
    assert validated is not None, reason
    assert first[0]["mutation_authorized"] is False
    again = observe(check, broad)
    assert again[0]["status"] == "OBSERVED"
    assert len(dispatches) == 1
