from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from src.autoslice.nested_media_caption_attribution import (
    AttributionInputError,
    build_caption_source_attribution,
    materialize_private_host_track,
)


_SRT = """1
00:00:00,000 --> 00:00:01,000
我打嗝跟打雷似的

2
00:00:01,000 --> 00:00:02,000
雷公电母来了，有感觉吗

3
00:00:02,000 --> 00:00:03,000
露蒂丝打嗝可爱吗

4
00:00:03,000 --> 00:00:03,500
哎，哎呀

5
00:00:03,500 --> 00:00:05,000
就是因为我们长得太像了

6
00:00:05,000 --> 00:00:06,000
主播回应
"""


def _sha(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _fixture(tmp_path: Path) -> tuple[Path, Path]:
    media = tmp_path / "source.mp4"
    media.write_bytes(b"frozen-media")
    srt = tmp_path / "final.srt"
    srt.write_text(_SRT, encoding="utf-8")
    return media, srt


def _observation(
    tmp_path: Path,
    *,
    observation_id: str,
    frame_ms: int,
    text: str | None,
) -> dict[str, object]:
    frame = tmp_path / f"{observation_id}.png"
    frame.write_bytes((observation_id + "-frame").encode())
    return {
        "schema_version": "embedded-media-caption-observation.v1",
        "observation_id": observation_id,
        "candidate_id": "candidate",
        "frame_ms": frame_ms,
        "caption_present": text is not None,
        "visible_text": text or "",
        "role": (
            "WATCHED_MEDIA_CAPTION"
            if text is not None
            else "NO_EMBEDDED_CAPTION_OBSERVED"
        ),
        "pipeline_burned_subtitle": False,
        "frame_path": str(frame),
        "frame_sha256": _sha(frame),
    }


def _acoustic(
    tmp_path: Path,
    *,
    observation_id: str,
    source_media_speech: str,
    host_speech: str,
    overlap_speech: str = "ABSENT",
) -> dict[str, object]:
    media, srt = _fixture(tmp_path)
    frame = tmp_path / f"{observation_id}.png"
    receipt = tmp_path / f"{observation_id}.acoustic.json"
    payload = {
        "schema_version": "nested-media-acoustic-attribution.v1",
        "status": "PASS",
        "observation_id": observation_id,
        "candidate_id": "candidate",
        "interval_ms": [0, 1000],
        "source_media_speech": source_media_speech,
        "host_speech": host_speech,
        "overlap_speech": overlap_speech,
        "method": "HASH_BOUND_OPERATOR_LOCAL_AUDIO_REVIEW",
        "accepted_by": "test-operator",
        "accepted_at": "2026-09-24T00:00:00Z",
        "bindings": {
            "source_media_sha256": _sha(media),
            "final_srt_sha256": _sha(srt),
            "caption_frame_sha256": _sha(frame),
        },
    }
    receipt.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {"path": str(receipt), "sha256": _sha(receipt)}


def _build(
    tmp_path: Path,
    observations: list[dict[str, object]],
    *,
    acoustic: list[dict[str, object]] | None = None,
) -> tuple[dict[str, object], Path]:
    media, srt = _fixture(tmp_path)
    result = build_caption_source_attribution(
        candidate_id="candidate",
        source_media_path=media,
        source_media_sha256=_sha(media),
        final_srt_path=srt,
        final_srt_sha256=_sha(srt),
        observations=observations,
        acoustic_receipt_bindings=acoustic or [],
    )
    return result, srt


def test_caption_only_is_unknown_and_cannot_drop(tmp_path: Path):
    result, _ = _build(
        tmp_path,
        [_observation(tmp_path, observation_id="caption", frame_ms=500, text="我打嗝跟打雷似的")],
    )

    row = result["rows"][0]
    assert row["cue_indexes"] == [1]
    assert row["visual_match"] == "exact"
    assert row["label"] == "unknown"
    assert row["action"] == "KEEP"
    assert result["safe_to_drop_cue_indexes"] == []


def test_no_caption_does_not_default_to_host(tmp_path: Path):
    result, _ = _build(
        tmp_path,
        [_observation(tmp_path, observation_id="blank", frame_ms=5500, text=None)],
    )

    row = result["rows"][0]
    assert row["cue_indexes"] == [6]
    assert row["label"] == "unknown"
    assert row["action"] == "KEEP"
    assert "NO_CAPTION_IS_NOT_HOST_EVIDENCE" in row["reason_codes"]


def test_source_and_host_present_is_overlap_and_kept(tmp_path: Path):
    observation = _observation(
        tmp_path,
        observation_id="overlap",
        frame_ms=500,
        text="我打嗝跟打雷似的",
    )
    result, _ = _build(
        tmp_path,
        [observation],
        acoustic=[
            _acoustic(
                tmp_path,
                observation_id="overlap",
                source_media_speech="PRESENT",
                host_speech="PRESENT",
            )
        ],
    )

    row = result["rows"][0]
    assert row["label"] == "overlap"
    assert row["action"] == "KEEP"
    assert result["safe_to_drop_cue_indexes"] == []


def test_only_hash_bound_source_without_host_can_drop(tmp_path: Path):
    observation = _observation(
        tmp_path,
        observation_id="source-only",
        frame_ms=500,
        text="我打嗝跟打雷似的",
    )
    result, srt = _build(
        tmp_path,
        [observation],
        acoustic=[
            _acoustic(
                tmp_path,
                observation_id="source-only",
                source_media_speech="PRESENT",
                host_speech="ABSENT",
            )
        ],
    )
    output = tmp_path / "private-host-track.srt"
    receipt = materialize_private_host_track(
        attribution=result,
        final_srt_path=srt,
        output_path=output,
    )

    row = result["rows"][0]
    assert row["label"] == "source_media"
    assert row["action"] == "DROP_SOURCE_MEDIA"
    assert result["safe_to_drop_cue_indexes"] == [1]
    assert "我打嗝跟打雷似的" not in output.read_text(encoding="utf-8")
    assert receipt["status"] == "PRIVATE_DIAGNOSTIC_HOST_TRACK_CANDIDATE"



def test_source_present_without_overlap_exclusion_is_unknown_and_kept(tmp_path: Path):
    observation = _observation(
        tmp_path,
        observation_id="overlap-unresolved",
        frame_ms=500,
        text="我打嗝跟打雷似的",
    )
    result, _ = _build(
        tmp_path,
        [observation],
        acoustic=[
            _acoustic(
                tmp_path,
                observation_id="overlap-unresolved",
                source_media_speech="PRESENT",
                host_speech="ABSENT",
                overlap_speech="UNKNOWN",
            )
        ],
    )

    row = result["rows"][0]
    assert row["label"] == "unknown"
    assert row["action"] == "KEEP"
    assert result["safe_to_drop_cue_indexes"] == []


def test_acoustic_caption_frame_binding_drift_fails_closed(tmp_path: Path):
    observation = _observation(
        tmp_path,
        observation_id="bound-frame",
        frame_ms=500,
        text="我打嗝跟打雷似的",
    )
    binding = _acoustic(
        tmp_path,
        observation_id="bound-frame",
        source_media_speech="PRESENT",
        host_speech="ABSENT",
    )
    receipt_path = Path(str(binding["path"]))
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["bindings"]["caption_frame_sha256"] = "sha256:" + "0" * 64
    receipt_path.write_text(
        json.dumps(receipt, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    binding["sha256"] = _sha(receipt_path)

    with pytest.raises(
        AttributionInputError, match="ACOUSTIC_CAPTION_FRAME_BINDING_MISMATCH"
    ):
        _build(tmp_path, [observation], acoustic=[binding])

def test_fuzzy_and_cross_cue_caption_alignment(tmp_path: Path):
    result, _ = _build(
        tmp_path,
        [
            _observation(
                tmp_path,
                observation_id="fuzzy",
                frame_ms=2500,
                text="露医生打嗝可爱吗",
            ),
            _observation(
                tmp_path,
                observation_id="cross",
                frame_ms=3800,
                text="哎呀就是因为我们 长得太像了",
            ),
        ],
    )

    fuzzy, cross = result["rows"]
    assert fuzzy["cue_indexes"] == [3]
    assert fuzzy["visual_match"] == "fuzzy"
    assert cross["cue_indexes"] == [4, 5]
    assert cross["visual_match"] == "cross_cue_segmentation"
    assert all(row["action"] == "KEEP" for row in result["rows"])


def test_frame_hash_drift_fails_closed(tmp_path: Path):
    observation = _observation(
        tmp_path,
        observation_id="drift",
        frame_ms=500,
        text="我打嗝跟打雷似的",
    )
    Path(str(observation["frame_path"])).write_bytes(b"changed")

    with pytest.raises(AttributionInputError, match="CAPTION_FRAME_HASH_MISMATCH"):
        _build(tmp_path, [observation])


def test_zero_drop_private_candidate_is_byte_identical(tmp_path: Path):
    result, srt = _build(
        tmp_path,
        [_observation(tmp_path, observation_id="caption", frame_ms=500, text="我打嗝跟打雷似的")],
    )
    output = tmp_path / "private-host-track.srt"
    receipt = materialize_private_host_track(
        attribution=result,
        final_srt_path=srt,
        output_path=output,
    )

    assert output.read_bytes() == srt.read_bytes()
    assert receipt["status"] == "PRIVATE_DIAGNOSTIC_NO_MUTATION_PENDING_ACOUSTIC"
    assert receipt["output_srt_sha256"] == receipt["source_srt_sha256"]
