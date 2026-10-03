"""Exercise the caption policy through the actual production materializer."""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from src.autoslice import producer_package_finalization as finalization
from src.autoslice import talk_lane
from src.autoslice.nested_media_caption_dedup import POLICY, SCHEMA, CaptionDedupError
from src.autoslice.producer_nested_caption import consume_nested_caption_policy

TRANSCRIPT = ("1\n00:00:01,000 --> 00:00:03,000\n这视频看多了\n\n"
              "2\n00:00:04,000 --> 00:00:06,000\n这是主播自己的评论\n")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def inputs(tmp_path):
    source = tmp_path / "padded.mp4"
    source.write_bytes(b"fixture media only")
    provenance = tmp_path / "padded.provenance.json"
    provenance.write_text("{}\n")
    frame = tmp_path / "frame.png"
    frame.write_bytes(b"fixture frame only")
    document = {"schema_version": SCHEMA, "candidate_id": "test-reaction",
                "source_role": "WATCHED_MEDIA", "policy": POLICY,
                "source_media_sha256": sha(source.read_bytes()),
                "source_interval_ms": [0, 6400],
                "input_transcript_sha256": sha(TRANSCRIPT.encode()),
                "observations": [{"observation_id": "caption-frame", "cue_index": 1,
                    "frame_ms": 2000, "visible_text": "这视频看多了",
                    "role": "WATCHED_MEDIA_CAPTION", "pipeline_burned_subtitle": False,
                    "host_repeat_or_overlap": None, "frame_path": "frame.png",
                    "frame_sha256": sha(frame.read_bytes())}]}
    evidence = tmp_path / "evidence.json"
    evidence.write_text(json.dumps(document, ensure_ascii=False))
    spec = {"pieces": [{"start_ms": 0, "end_ms": 6400}], "watched_media": True,
            "nested_media_caption_dedup": {"policy": POLICY, "evidence_path": "evidence.json",
                "evidence_sha256": sha(evidence.read_bytes())}}
    return source, provenance, spec


def adapters(tmp_path):
    def unused(*args, **kwargs):
        raise AssertionError("No model, upload, speaker or burn call belongs in this fixture")

    return finalization.ProducerFinalizationAdapters(
        accurate_recut_command=lambda **kwargs: ["recut", str(kwargs["output_media"])],
        run_command=lambda command, **kwargs: Path(command[-1]).write_bytes(b"rendered fixture"),
        write_source_range_srt=lambda cues, start, end, path: path.write_text(TRANSCRIPT),
        apply_text_override_document=unused,
        run_speaker_finalization=unused,
        burn_preview_subtitles=unused,
        stage_publish_draft=unused,
        generate_upload_tags=unused,
        delivery_root=lambda: tmp_path / "delivery",
    )


def materialize(tmp_path, spec, source, provenance, chat, observer=None):
    (tmp_path / "out").mkdir()
    return finalization._materialize_final_recut(
        spec=spec, cid="test-reaction", out_root=tmp_path / "out", padded=source,
        padded_provenance_path=provenance, piece_provenance_rows=[],
        final_start=0, final_end=6400, sanitized=[], timing_qa={}, text_override_path=None,
        adapters=replace(adapters(tmp_path), observe_nested_captions=observer), spec_parent=tmp_path, chat_authority_audit=chat,
    )


def test_production_materializer_filters_before_final_srt_hash_and_burn(tmp_path):
    source, provenance, spec = inputs(tmp_path)
    chat = {}
    recut = materialize(tmp_path, spec, source, provenance, chat)
    output = recut.subtitle_path.read_bytes()
    assert "这视频看多了" not in output.decode()
    assert "这是主播自己的评论" in output.decode()
    assert chat["final_output_srt_sha256"] == sha(output)
    audit = chat["nested_media_caption_dedup"]
    assert audit["dropped_input_cue_indexes"] == [1]
    assert Path(audit["all_source_transcript_path"]).read_text() == TRANSCRIPT
    assert audit["source_interval_unchanged"] is True
    assert audit["exact_final_review_required"] is True
    provenance_after = json.loads(recut.media_path.with_suffix(".provenance.json").read_text())
    assert provenance_after["final_recut"]["start_ms"] == 0
    assert provenance_after["final_recut"]["end_ms"] == 6400


def test_ordinary_materializer_needs_no_caption_evidence_and_keeps_original_text(tmp_path):
    source, provenance, _ = inputs(tmp_path)
    chat = {}
    recut = materialize(tmp_path, {"pieces": [{"start_ms": 0}]}, source, provenance, chat)
    assert recut.subtitle_path.read_text() == TRANSCRIPT
    assert "nested_media_caption_dedup" not in chat
    assert not recut.subtitle_path.with_suffix(".caption-dedup.json").exists()


def test_real_materializer_observer_receives_current_srt_before_dedup(tmp_path):
    source, provenance, bound = inputs(tmp_path)
    calls = []
    def observer(**kwargs):
        calls.append(kwargs)
        assert kwargs["subtitle_path"].read_text() == TRANSCRIPT
        assert kwargs["source_media"] == source
        assert kwargs["source_start_ms"] == 0 and kwargs["source_end_ms"] == 6400
        return bound
    recut = materialize(tmp_path, {"pieces": [{"start_ms": 0}]}, source, provenance, {}, observer)
    assert len(calls) == 1
    assert "这视频看多了" not in recut.subtitle_path.read_text()
    assert recut.subtitle_path.with_suffix(".all-source-transcript.txt").read_text() == TRANSCRIPT


def test_watched_materializer_cannot_silently_skip_missing_caption_evidence(tmp_path):
    source, provenance, spec = inputs(tmp_path)
    del spec["nested_media_caption_dedup"]
    with pytest.raises(CaptionDedupError, match="WATCHED_MEDIA_CAPTION_EVIDENCE_REQUIRED"):
        materialize(tmp_path, spec, source, provenance, {})


def test_bad_evidence_does_not_modify_existing_current_subtitle(tmp_path):
    source, _, spec = inputs(tmp_path)
    spec["nested_media_caption_dedup"]["evidence_sha256"] = "0" * 64
    subtitle = tmp_path / "display.srt"
    subtitle.write_text(TRANSCRIPT)
    with pytest.raises(CaptionDedupError, match="HASH_MISMATCH"):
        consume_nested_caption_policy(spec=spec, candidate_id="test-reaction", source_media=source,
            source_start_ms=0, source_end_ms=6400, subtitle_path=subtitle, evidence_root=tmp_path)
    assert subtitle.read_text() == TRANSCRIPT
    assert not subtitle.with_suffix(".all-source-transcript.txt").exists()


def test_previous_sidecar_conflict_is_not_overwritten(tmp_path):
    source, _, spec = inputs(tmp_path)
    subtitle = tmp_path / "display.srt"
    subtitle.write_text(TRANSCRIPT)
    archive = subtitle.with_suffix(".all-source-transcript.txt")
    archive.write_text("unrelated predecessor evidence")
    with pytest.raises(CaptionDedupError, match="SIDECAR_CONFLICT"):
        consume_nested_caption_policy(spec=spec, candidate_id="test-reaction", source_media=source,
            source_start_ms=0, source_end_ms=6400, subtitle_path=subtitle, evidence_root=tmp_path)
    assert subtitle.read_text() == TRANSCRIPT
    assert archive.read_text() == "unrelated predecessor evidence"


def test_normal_talk_spec_handoff_reaches_materializer(tmp_path, monkeypatch):
    source, provenance, declared = inputs(tmp_path)
    monkeypatch.setattr(talk_lane, "_runner", object())
    spec = {"pieces": declared["pieces"]}
    talk_lane._apply_optional_talk_spec_fields(spec, declared)
    chat = {}
    recut = materialize(tmp_path, spec, source, provenance, chat)
    assert chat["nested_media_caption_dedup"]["dropped_input_cue_indexes"] == [1]
    assert "这是主播自己的评论" in recut.subtitle_path.read_text()
    assert spec["nested_media_caption_dedup"] == declared["nested_media_caption_dedup"]
    assert spec["nested_media_caption_dedup"] is not declared["nested_media_caption_dedup"]


def test_normal_talk_handoff_cannot_lose_declared_missing_evidence(tmp_path, monkeypatch):
    source, provenance, declared = inputs(tmp_path)
    monkeypatch.setattr(talk_lane, "_runner", object())
    spec = {"pieces": declared["pieces"]}
    talk_lane._apply_optional_talk_spec_fields(spec, {"watched_media": True})
    with pytest.raises(CaptionDedupError, match="WATCHED_MEDIA_CAPTION_EVIDENCE_REQUIRED"):
        materialize(tmp_path, spec, source, provenance, {})
