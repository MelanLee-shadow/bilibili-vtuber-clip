"""Display de-duplication tests; synthetic inputs are not speaker ground truth."""
from __future__ import annotations

import hashlib
import json

import pytest

from src.autoslice.nested_media_caption_dedup import (
    POLICY, SCHEMA, CaptionDedupError, apply_bound_display_policy, derive_display_track,
)

TEXT = ("1\n00:00:01,000 --> 00:00:03,000\n这视频看多了\n\n"
        "2\n00:00:04,000 --> 00:00:06,000\n这是主播自己的评论\n")


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def observation(**changes):
    return {"observation_id": "frame-1", "cue_index": 1, "frame_ms": 2000,
            "visible_text": "这视频看多了", "role": "WATCHED_MEDIA_CAPTION",
            "pipeline_burned_subtitle": False, "host_repeat_or_overlap": None,
            **changes}


def bound_inputs(tmp_path):
    media = tmp_path / "source.mp4"
    media.write_bytes(b"synthetic media bytes; no provider called")
    frame = tmp_path / "frame.png"
    frame.write_bytes(b"synthetic frame bytes")
    doc = {"schema_version": SCHEMA, "candidate_id": "ordinary-test",
           "source_role": "WATCHED_MEDIA", "policy": POLICY,
           "source_interval_ms": [0, 6400], "input_transcript_sha256": digest(TEXT.encode()),
           "source_media_sha256": digest(media.read_bytes()),
           "observations": [observation(frame_path="frame.png", frame_sha256=digest(frame.read_bytes()))]}
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(doc, ensure_ascii=False))
    config = {"policy": POLICY, "evidence_path": "evidence.json",
              "evidence_sha256": digest(path.read_bytes())}
    args = dict(config=config, candidate_id="ordinary-test", source_media=media,
                transcript=TEXT, source_start_ms=0, source_end_ms=6400, evidence_root=tmp_path)
    return args, doc, path


def test_synchronized_complete_caption_removes_only_duplicate_display_cue():
    output, audit = derive_display_track(transcript=TEXT, observations=[observation()])
    assert "这视频看多了" not in output
    assert output == "1\n00:00:04,000 --> 00:00:06,000\n这是主播自己的评论\n"
    assert audit["dropped_input_cue_indexes"] == [1]
    assert audit["speaker_identity_proven"] is False
    assert audit["publication_authority"] is False
    assert audit["input_transcript_sha256"] == digest(TEXT.encode())


def test_punctuation_difference_does_not_require_a_new_transcription():
    output, audit = derive_display_track(transcript=TEXT, observations=[observation(visible_text="这，视频看多了！")])
    assert audit["dropped_input_cue_indexes"] == [1]
    assert "主播自己的评论" in output


@pytest.mark.parametrize("frame_ms", [999, 3000, 12000])
def test_same_text_at_another_time_does_not_delete_current_host_speech(frame_ms):
    output, audit = derive_display_track(transcript=TEXT, observations=[observation(frame_ms=frame_ms)])
    assert output == TEXT
    assert audit["dropped_input_cue_indexes"] == []


def test_half_open_source_offset_is_applied_once():
    output, audit = derive_display_track(transcript=TEXT, observations=[observation(frame_ms=11750)], source_start_ms=9750)
    assert audit["dropped_input_cue_indexes"] == [1]
    assert "00:00:04,000" in output


@pytest.mark.parametrize("changes", [
    {"role": "CHAT"}, {"role": "GAME_UI"}, {"role": "VIDEO_TITLE"},
    {"pipeline_burned_subtitle": True}, {"pipeline_burned_subtitle": None},
])
def test_unrelated_text_or_self_generated_subtitles_never_prove_duplication(changes):
    assert derive_display_track(transcript=TEXT, observations=[observation(**changes)])[0] == TEXT


def test_host_repeat_and_overlap_evidence_vetoes_visual_deletion():
    _, audit = derive_display_track(transcript=TEXT, observations=[observation(),
        observation(observation_id="host-window", visible_text="", host_repeat_or_overlap=True)])
    assert audit["dropped_input_cue_indexes"] == []
    assert audit["protected_host_or_overlap_cue_indexes"] == [1]


def test_partial_mixed_cue_is_not_deleted_or_arbitrarily_split():
    text = TEXT.replace("这视频看多了", "这视频看多了，哈哈我也觉得")
    output, audit = derive_display_track(transcript=text, observations=[observation()])
    assert output == text
    assert audit["partial_or_short_match_cue_indexes"] == [1]


def test_short_common_interjection_is_not_automatic_identity_evidence():
    text = TEXT.replace("这视频看多了", "哈哈")
    output, audit = derive_display_track(transcript=text, observations=[observation(visible_text="哈哈")])
    assert output == text
    assert audit["partial_or_short_match_cue_indexes"] == [1]


def test_fuzzy_but_different_text_is_not_silently_deleted():
    assert derive_display_track(transcript=TEXT, observations=[observation(visible_text="这个视频看多了")])[0] == TEXT


def test_no_observation_does_not_claim_the_host_spoke():
    output, audit = derive_display_track(transcript=TEXT, observations=[])
    assert output == TEXT
    assert audit["no_caption_means_host"] is False


@pytest.mark.parametrize("changes", [
    {"cue_index": True}, {"cue_index": 100}, {"frame_ms": True},
    {"frame_ms": -1}, {"visible_text": None}, {"observation_id": ""},
    {"host_repeat_or_overlap": "false"}, {"host_repeat_or_overlap": 0},
])
def test_malformed_observation_is_rejected(changes):
    with pytest.raises(CaptionDedupError):
        derive_display_track(transcript=TEXT, observations=[observation(**changes)])


def test_duplicate_observation_identity_is_rejected():
    with pytest.raises(CaptionDedupError):
        derive_display_track(transcript=TEXT, observations=[observation(), observation()])


def test_bound_consumer_recomputes_filter_and_preserves_evidence(tmp_path):
    args, _, path = bound_inputs(tmp_path)
    before = path.read_bytes()
    output, audit = apply_bound_display_policy(**args)
    assert "这视频看多了" not in output
    assert path.read_bytes() == before
    assert audit["evidence_sha256"] == digest(before)


@pytest.mark.parametrize("role", ["source", "frame", "evidence"])
def test_changed_bound_bytes_fail_before_subtitle_mutation(tmp_path, role):
    args, _, path = bound_inputs(tmp_path)
    changed = {"source": args["source_media"], "frame": tmp_path / "frame.png", "evidence": path}[role]
    changed.write_bytes(changed.read_bytes() + b"drift")
    with pytest.raises(CaptionDedupError, match="HASH_MISMATCH"):
        apply_bound_display_policy(**args)


@pytest.mark.parametrize("field,value", [
    ("candidate_id", "another-candidate"), ("source_interval_ms", [0, 6800]),
    ("input_transcript_sha256", "0" * 64), ("source_role", "UNKNOWN"),
])
def test_resealed_evidence_cannot_change_input_identity(tmp_path, field, value):
    args, doc, path = bound_inputs(tmp_path)
    doc[field] = value
    path.write_text(json.dumps(doc))
    args["config"]["evidence_sha256"] = digest(path.read_bytes())
    with pytest.raises(CaptionDedupError, match="BINDING_MISMATCH"):
        apply_bound_display_policy(**args)


def test_ordinary_non_watched_media_does_not_read_files_or_rewrite_text(tmp_path):
    output, audit = apply_bound_display_policy(config=None, candidate_id="normal",
        source_media=tmp_path / "does-not-exist", transcript=TEXT,
        source_start_ms=0, source_end_ms=6400, evidence_root=tmp_path)
    assert output == TEXT
    assert audit is None


def test_declared_watched_policy_without_evidence_is_not_silently_ignored(tmp_path):
    with pytest.raises(CaptionDedupError, match="EVIDENCE_REQUIRED"):
        apply_bound_display_policy(config={"policy": POLICY}, candidate_id="normal",
            source_media=tmp_path / "does-not-exist", transcript=TEXT,
            source_start_ms=0, source_end_ms=6400, evidence_root=tmp_path)
