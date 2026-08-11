from __future__ import annotations

import hashlib
import json
import wave
from array import array
from pathlib import Path

import pytest

from scripts import build_reviewed_voiceprint_enrollment as enrollment


REPO_ROOT = Path(__file__).resolve().parents[2]


def _row(
    source_cue: int,
    *,
    start: str,
    end: str,
    speakers: tuple[str, ...] = ("李豆沙",),
) -> dict[str, object]:
    return {
        "source_cue": source_cue,
        "authority": "Ivan-annotated-test",
        "expect": {"start": start, "end": end, "text": f"cue {source_cue}"},
        "segments": [
            {
                "start": start,
                "end": end,
                "speaker": speaker,
                "text": f"speaker {speaker}",
            }
            for speaker in speakers
        ],
    }


def _payload(media_sha256: str, text_final_srt_sha256: str = "0" * 64) -> dict[str, object]:
    return {
        "schema_version": 1,
        "candidate_id": "candidate-a",
        "status": "ivan_reviewed_speaker_binary_default_guest",
        "source_media_sha256": media_sha256,
        "text_final_srt_sha256": text_final_srt_sha256,
        "overrides": [
            _row(1, start="00:00:00,000", end="00:00:00,800"),
            _row(2, start="00:00:01,000", end="00:00:02,100"),
            _row(3, start="00:00:02,500", end="00:00:04,600"),
            _row(
                4,
                start="00:00:05,000",
                end="00:00:06,000",
                speakers=("李豆沙", "连线"),
            ),
            _row(
                5,
                start="00:00:06,500",
                end="00:00:07,500",
                speakers=("连线",),
            ),
        ],
    }


def _write_pcm(path: Path, *, duration_ms: int = 8_000) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    samples = array(
        "h",
        (6_000 if index % 2 else -6_000 for index in range(duration_ms * 16_000 // 1_000)),
    )
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16_000)
        handle.writeframes(samples.tobytes())
    return path


def _write_srt(path: Path) -> Path:
    path.write_text(
        """1
00:00:00,000 --> 00:00:00,800
cue 1

2
00:00:01,000 --> 00:00:02,100
cue 2

3
00:00:02,500 --> 00:00:04,600
cue 3

4
00:00:05,000 --> 00:00:06,000
cue 4

5
00:00:06,500 --> 00:00:07,500
cue 5
""",
        encoding="utf-8",
    )
    return path


def test_selection_excludes_mixed_and_non_host_rows() -> None:
    selected, excluded = enrollment._select_reviewed_host_rows(
        _payload("0" * 64), minimum_clip_ms=700
    )
    assert [row["source_cue"] for row in selected] == [1, 2, 3]
    assert excluded == {
        "mixed_or_multisegment": 1,
        "single_non_host": 1,
        "below_minimum_duration": 0,
    }


def test_7_22_reviewed_truth_yields_only_four_exact_host_cues() -> None:
    override = REPO_ROOT / "assets/lidousha/speaker_overrides/auto_200511_61_138.speaker.v1.json"
    payload = json.loads(override.read_text(encoding="utf-8"))
    selected, excluded = enrollment._select_reviewed_host_rows(payload, minimum_clip_ms=700)
    assert [row["source_cue"] for row in selected] == [4, 5, 27, 32]
    assert sum(int(row["duration_ms"]) for row in selected) == 4_860
    assert excluded["mixed_or_multisegment"] == 7
    cue_five = next(row for row in selected if row["source_cue"] == 5)
    assert cue_five["binding_text_sha256"] != cue_five["reviewed_text_sha256"]


@pytest.mark.parametrize(
    ("candidate_id", "expected_count", "expected_duration_ms", "expected_short"),
    [
        ("auto_200130_1323_1603", 33, 69_029, 2),
        ("auto_200130_1722_1792", 11, 17_960, 0),
    ],
)
def test_8_8_truth_yields_bound_development_samples_not_holdout(
    candidate_id: str,
    expected_count: int,
    expected_duration_ms: int,
    expected_short: int,
) -> None:
    override = REPO_ROOT / "assets/lidousha/speaker_overrides" / f"{candidate_id}.speaker.v1.json"
    payload = json.loads(override.read_text(encoding="utf-8"))
    selected, excluded = enrollment._select_reviewed_host_rows(payload, minimum_clip_ms=700)
    assert len(selected) == expected_count
    assert sum(int(row["duration_ms"]) for row in selected) == expected_duration_ms
    assert excluded["below_minimum_duration"] == expected_short


def test_single_speaker_segment_must_match_full_cue_binding() -> None:
    payload = _payload("0" * 64)
    payload["overrides"][0]["segments"][0]["end"] = "00:00:00,700"
    with pytest.raises(
        enrollment.ReviewedEnrollmentError,
        match="must match its full cue binding",
    ):
        enrollment._select_reviewed_host_rows(payload, minimum_clip_ms=700)


def test_builder_binds_media_override_and_extracted_audio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    media_path = tmp_path / "candidate.mp4"
    media_path.write_bytes(b"exact-reviewed-media")
    media_digest = hashlib.sha256(media_path.read_bytes()).hexdigest()
    text_final_srt_path = _write_srt(tmp_path / "candidate.recut.srt")
    text_final_srt_digest = hashlib.sha256(text_final_srt_path.read_bytes()).hexdigest()
    override_path = tmp_path / "candidate.speaker.v1.json"
    override_path.write_text(
        json.dumps(_payload(media_digest, text_final_srt_digest), ensure_ascii=False),
        encoding="utf-8",
    )

    monkeypatch.setattr(enrollment, "_duration_ms", lambda _path: 8_000)

    def fake_extract(_source: Path, *, start_ms: int, end_ms: int, output_path: Path) -> Path:
        assert (start_ms, end_ms) == (0, 8_000)
        return _write_pcm(output_path)

    monkeypatch.setattr(enrollment.ho, "extract_span_wav", fake_extract)
    result = enrollment.build_reviewed_enrollment(
        source_session_id="session-2026-07-22",
        media_path=media_path,
        text_final_srt_path=text_final_srt_path,
        override_path=override_path,
        work_dir=tmp_path / "work",
    )
    assert result["purpose"] == enrollment.PURPOSE
    assert result["selection"]["selected_clip_count"] == 3
    assert result["selection"]["excluded_rows"]["mixed_or_multisegment"] == 1
    assert result["bindings"]["text_final_srt_cue_count"] == 5
    assert result["bindings"]["pcm_duration_rounding_tolerance_ms"] == 1
    assert result["summary"]["production_enrollment_ready"] is False
    assert "RECOMMENDED_TOTAL_DURATION_NOT_MET" in result["summary"]["promotion_blockers"]
    assert len(result["clips"]) == 3
    for clip in result["clips"]:
        assert str(clip["sha256"]).startswith("sha256:")
        assert str(clip["binding_text_sha256"]).startswith("sha256:")
        assert str(clip["reviewed_text_sha256"]).startswith("sha256:")
        assert (tmp_path / "work" / str(clip["relative_path"])).is_file()


def test_builder_rejects_media_not_bound_by_override(tmp_path: Path) -> None:
    media_path = tmp_path / "candidate.mp4"
    media_path.write_bytes(b"wrong-media")
    text_final_srt_path = _write_srt(tmp_path / "candidate.recut.srt")
    override_path = tmp_path / "candidate.speaker.v1.json"
    override_path.write_text(json.dumps(_payload("0" * 64), ensure_ascii=False), encoding="utf-8")
    with pytest.raises(
        enrollment.ReviewedEnrollmentError,
        match="does not match reviewed override",
    ):
        enrollment.build_reviewed_enrollment(
            source_session_id="session-2026-07-22",
            media_path=media_path,
            text_final_srt_path=text_final_srt_path,
            override_path=override_path,
            work_dir=tmp_path / "work",
        )
