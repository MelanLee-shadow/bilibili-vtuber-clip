import json

import pytest

from src.autoslice import song_materialized_successor as successor
from src.autoslice.auto_review import DecisionAction, ReviewDecision
from src.autoslice.boundary_resolver import BoundaryResolution
from src.autoslice.live_source_review import _merge_boundary_resolution_into_decision
from src.autoslice.publish_staging import _stage_publish_after_release_gate


def fixture(tmp_path, monkeypatch):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"original source")
    job = {
        "candidate_id": "song",
        "timeline": {"anchor_start_ms": 32, "anchor_end_ms": 160},
        "song_boundary": {"status": "FULL_SONG_READY"},
    }
    recut = {
        "status": "MATERIALIZED",
        "start_ms": 44,
        "end_ms": 256,
        "source_binding": {"canonical_path": str(source.resolve()), "sha256": "source-bound"},
        "artifact_hashes": {"video_sha256": "original-render"},
        "manifest_path": str(tmp_path / "render.json"),
        "manifest_sha256": "original-manifest",
    }
    record = {
        "candidate_id": "song",
        "decision_action": "AUTO_RECUT",
        "reason_codes": ["SONG_FULL_BOUNDARY_READY"],
        "source_context_job": job,
        "materialized_recut": recut,
        "source_context": {
            "decision": "READY",
            "reason_codes": [],
            "context_media_path": "context.mp4",
            "context_draft_srt_path": "draft.srt",
            "context_refined_srt_path": "draft.srt",
            "jingting_manifest_path": None,
            "review_required_path": None,
            "source_cues_path": None,
            "jingting_done": False,
        },
    }
    (tmp_path / "summary.json").write_text(json.dumps({"records": [record]}))
    # Full hash/media/lyric/host proof verification is covered by the existing
    # song_completion contract tests and actual ordinary original-input replay.
    monkeypatch.setattr(
        successor, "completion", lambda _record: {"ready": True, "reason_codes": []}
    )
    boundary = BoundaryResolution(
        candidate_id="song",
        action=DecisionAction.AUTO_RECUT,
        resolved_start_ms=44,
        resolved_end_ms=256,
        start_boundary_score=0.98,
        end_boundary_score=0.98,
        reason_codes=("SONG_FULL_BOUNDARY_READY",),
        next_start_ms=44,
        next_end_ms=256,
    )
    return source, job, recut, boundary, record


@pytest.mark.parametrize("cover_appended", [False, True])
def test_completed_window_has_fresh_boundary_and_keeps_original_anchor(
    tmp_path, monkeypatch, cover_appended
):
    source, job, recut, boundary, record = fixture(tmp_path, monkeypatch)
    loaded = successor.load_materialized_successor(
        source_video=source, job=job, output_dir=tmp_path
    )
    current, receipt = successor.completed_boundary(loaded[0], loaded[2], boundary)
    assert current.action == DecisionAction.AUTO_UPLOAD and current.reason_codes == ()
    assert receipt["original_anchor"] == job["timeline"]
    assert receipt["predecessor_boundary"]["action"] == "AUTO_RECUT"
    receipt["predecessor_summary"] = loaded[3]
    record["materialized_song_successor"] = receipt
    record["decision_action"], record["reason_codes"] = "AUTO_UPLOAD", []
    if cover_appended:
        record["materialized_recut"]["artifact_hashes"]["cover_sha256"] = "post-review-cover"
    (tmp_path / "summary.json").write_text(json.dumps({"records": [record]}))
    second = successor.load_materialized_successor(
        source_video=source, job=job, output_dir=tmp_path
    )
    assert all(
        second[2]["artifact_hashes"].get(k) == loaded[2]["artifact_hashes"].get(k)
        for k in successor.MEDIA_HASH_KEYS
    )
    assert second[3] == loaded[3]
    assert len(list((tmp_path / "song-materialized-input").iterdir())) == 1


@pytest.mark.parametrize("damage", ["source", "window", "ambiguous", "proof", "history"])
def test_existing_invalid_successor_is_held_instead_of_rendered_again(
    tmp_path, monkeypatch, damage
):
    source, job, recut, boundary, record = fixture(tmp_path, monkeypatch)
    if damage == "source":
        source = tmp_path / "other.mp4"
        source.write_bytes(b"other")
    elif damage == "window":
        job = {**job, "timeline": {**job["timeline"], "anchor_end_ms": 161}}
    elif damage == "ambiguous":
        (tmp_path / "summary.json").write_text(json.dumps({"records": [record, record]}))
    elif damage == "proof":
        monkeypatch.setattr(
            successor,
            "completion",
            lambda _: {"ready": False, "reason_codes": ["SONG_RECUT_ARTIFACT_BINDING_INVALID"]},
        )
    else:
        record["materialized_song_successor"] = "invented receipt"
        (tmp_path / "summary.json").write_text(json.dumps({"records": [record]}))
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    with pytest.raises(ValueError):
        successor.load_materialized_successor(source_video=source, job=job, output_dir=tmp_path)
    assert {p: p.read_bytes() for p in before} == before


@pytest.mark.parametrize("damage", ["incomplete", "wrong_window", "other_reason"])
def test_unproved_window_does_not_consume_recut_reason(tmp_path, monkeypatch, damage):
    _, job, recut, boundary, _ = fixture(tmp_path, monkeypatch)
    if damage == "incomplete":
        monkeypatch.setattr(successor, "completion", lambda _: {"ready": False})
    elif damage == "wrong_window":
        recut["end_ms"] += 1
    else:
        from dataclasses import replace

        boundary = replace(boundary, reason_codes=("OTHER_UNRESOLVED_REASON",))
    assert successor.completed_boundary(job, recut, boundary) is None


@pytest.mark.parametrize("blocked", [False, True])
def test_fresh_successor_keeps_other_rejections_and_existing_cover_gate(
    tmp_path, monkeypatch, blocked
):
    _, job, recut, boundary, _ = fixture(tmp_path, monkeypatch)
    current, _ = successor.completed_boundary(job, recut, boundary)
    decision = (
        ReviewDecision(DecisionAction.BLOCK, ("CPA_SEMANTIC_FAILED",))
        if blocked
        else ReviewDecision(DecisionAction.AUTO_UPLOAD)
    )
    merged = _merge_boundary_resolution_into_decision(decision, current)
    calls = []
    _stage_publish_after_release_gate(
        recut,
        decision=merged,
        candidate_id="song",
        title="song",
        cues=[],
        output_dir=tmp_path,
        run_ffmpeg=False,
        title_llm_call=None,
        stage_publish=lambda result, **_: calls.append(result) or result,
    )
    assert bool(calls) is (not blocked)
    if blocked:
        assert merged.reason_codes == ("CPA_SEMANTIC_FAILED",)
    calls.clear()
    _stage_publish_after_release_gate(
        recut,
        decision=ReviewDecision(DecisionAction.AUTO_RECUT, ("SONG_FULL_BOUNDARY_READY",)),
        candidate_id="song",
        title="song",
        cues=[],
        output_dir=tmp_path,
        run_ffmpeg=False,
        title_llm_call=None,
        stage_publish=lambda result, **_: calls.append(result) or result,
    )
    assert calls == []
