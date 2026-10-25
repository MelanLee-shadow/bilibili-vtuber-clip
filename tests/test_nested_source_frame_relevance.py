from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from src.autoslice.nested_source_frame_relevance import (
    DEFAULT_MODEL,
    observe_candidate_source_frame_relevance,
)


def _sha_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _write(path: Path, payload: bytes) -> str:
    path.write_bytes(payload)
    return _sha_bytes(payload)


def _fixture(tmp_path: Path) -> tuple[dict[str, object], dict[str, object], Path, Path]:
    outer_media = tmp_path / "outer.mp4"
    source_media = tmp_path / "candidate.mp4"
    srt = tmp_path / "current.srt"
    outer_sha = _write(outer_media, b"outer-video-bytes")
    source_sha = _write(source_media, b"independent-video-bytes")
    srt_sha = _write(srt, b"1\n00:00:00,000 --> 00:00:01,000\nhello\n")
    frame = tmp_path / "outer.jpg"
    frame_sha = _write(frame, b"outer-jpeg-bytes")
    source_frames = []
    for index, payload in enumerate((b"source-jpeg-one", b"source-jpeg-two", b"source-jpeg-three"), 1):
        path = tmp_path / f"source-{index}.jpg"
        frame_hash = _write(path, payload)
        source_frames.append(
            {
                "frame_path": str(path),
                "frame_sha256": frame_hash,
                "frame_ms": index * 1000,
                "source_media_path": str(source_media),
                "source_media_sha256": source_sha,
            }
        )
    evidence = tmp_path / "scene-evidence.json"
    evidence_document = {
        "schema_version": "caption-evidence.v1",
        "candidate_id": "candidate-1",
        "source_media_sha256": outer_sha,
        "input_transcript_sha256": srt_sha,
        "source_role": "WATCHED_MEDIA",
        "observations": [
            {
                "observation_id": "caption-observation-1",
                "frame_ms": 500,
                "frame_sha256": frame_sha,
                "role": "WATCHED_MEDIA_CAPTION",
                "frame_path": str(frame),
            }
        ],
    }
    evidence_sha = _write(
        evidence,
        (json.dumps(evidence_document, sort_keys=True) + "\n").encode(),
    )
    descriptor: dict[str, object] = {
        "observation_id": "scene-1",
        "outer_media_sha256": outer_sha,
        "final_srt_sha256": srt_sha,
        "caption_frame_path": str(frame),
        "caption_frame_sha256": frame_sha,
        "caption_frame_ms": 500,
        "interval_ms": [0, 1000],
        "target_interval_ms": [0, 1000],
        "target_cue_indexes": [1],
        "visual_observation": {
            "observation_id": "scene-1",
            "frame_ms": 500,
            "frame_path": str(frame),
            "frame_sha256": frame_sha,
            "role": "WATCHED_MEDIA_CAPTION",
            "pipeline_burned_subtitle": False,
        },
        "nested_media_caption_dedup": {
            "evidence_path": str(evidence),
            "evidence_sha256": evidence_sha,
        },
    }
    cached_source: dict[str, object] = {
        "status": "CANDIDATE_CACHE_AVAILABLE",
        "action": "KEEP",
        "mutation_authorized": False,
        "source_role_authority": False,
        "uniqueness_claim": False,
        "consumer_candidate_id": "candidate-1",
        "independent_source_media_path": str(source_media),
        "independent_source_media_sha256": source_sha,
        "source_cache_binding": {
            "bvid": "BV1candidate",
            "cid": 123,
            "cache_entry_path": str(tmp_path / "source.json"),
            "cache_entry_sha256": "sha256:" + "1" * 64,
            "source_role_authority": False,
        },
        "source_frames": source_frames,
    }
    return descriptor, cached_source, outer_media, srt


def _probe(answer: object, calls: list[tuple[list[Path], str]]):
    def run(paths: list[Path], question: str) -> dict[str, object]:
        calls.append((paths, question))
        image_hashes = [_sha_bytes(path.read_bytes()) for path in paths]
        prompt_sha = _sha_bytes(
            json.dumps(
                {
                    "question": question,
                    "image_sha256s": image_hashes,
                    "model": DEFAULT_MODEL,
                },
                sort_keys=True,
            ).encode()
        )
        return {
            "schema_version": "cpa-frame-witness.v1",
            "status": "OBSERVED",
            "question": question,
            "model": DEFAULT_MODEL,
            "images": [{"image_sha256": value} for value in image_hashes],
            "prompt_sha256": prompt_sha,
            "answer": (
                json.dumps(answer, ensure_ascii=False)
                if isinstance(answer, dict)
                else answer
            ),
            "response_sha256": "sha256:" + "2" * 64,
        }

    return run


def test_match_is_evidence_only_and_replays_without_a_second_probe(tmp_path: Path) -> None:
    descriptor, cached, outer_media, srt = _fixture(tmp_path)
    calls: list[tuple[list[Path], str]] = []
    kwargs = {
        "candidate_id": "candidate-1",
        "descriptor": descriptor,
        "cached_source": cached,
        "source_media": outer_media,
        "subtitle_path": srt,
        "evidence_root": tmp_path,
        "vision_probe": _probe(
            {
                "decision": "MATCH",
                "relation": "SCENE_OR_PROGRAM",
                "visible_evidence": "same concrete program scene and same player background",
            },
            calls,
        ),
    }
    observed = observe_candidate_source_frame_relevance(**kwargs)
    replayed = observe_candidate_source_frame_relevance(**kwargs)

    assert observed["status"] == "OBSERVED"
    assert observed["authority"] == "EVIDENCE_ONLY"
    assert observed["decision"] == "MATCH"
    assert observed["action"] == "KEEP"
    assert observed["mutation_authorized"] is False
    assert observed["source_cache_verified"] is False
    assert observed["source_role_authority"] is False
    assert observed["uniqueness_claim"] is False
    assert observed["time_alignment_performed"] is False
    assert observed["source_subtraction_authorized"] is False
    assert observed["drop_authority"] is False
    assert replayed["status"] == "REPLAYED"
    assert replayed["decision"] == "MATCH"
    assert len(calls) == 1
    assert len(calls[0][0]) == 4
    assert calls[0][0][0].name == "outer-watched-player.jpg"


def test_title_or_style_only_cannot_become_match(tmp_path: Path) -> None:
    descriptor, cached, outer_media, srt = _fixture(tmp_path)
    calls: list[tuple[list[Path], str]] = []
    result = observe_candidate_source_frame_relevance(
        candidate_id="candidate-1",
        descriptor=descriptor,
        cached_source=cached,
        source_media=outer_media,
        subtitle_path=srt,
        evidence_root=tmp_path,
        vision_probe=_probe(
            {
                "decision": "MATCH",
                "relation": "APPEARANCE_ONLY",
                "visible_evidence": "the same title and visual style",
            },
            calls,
        ),
    )

    assert result["status"] == "OBSERVED"
    assert result["decision"] == "UNKNOWN"
    assert result["reason_code"] == "CPA_MATCH_RELATION_INSUFFICIENT"
    assert result["action"] == "KEEP"


@pytest.mark.parametrize("field,value", [("decision", "MATCH"), ("drop_authority", True)])
def test_cached_decision_or_authority_drift_cannot_promote_evidence(tmp_path, field, value):
    descriptor, cached, outer_media, srt = _fixture(tmp_path)
    calls = []
    kwargs = dict(
        candidate_id="candidate-1", descriptor=descriptor, cached_source=cached,
        source_media=outer_media, subtitle_path=srt, evidence_root=tmp_path,
        vision_probe=_probe(json.dumps({"decision": "UNKNOWN", "relation": "UNKNOWN",
                                      "visible_evidence": "No distinct event is visible."}), calls),
    )
    observed = observe_candidate_source_frame_relevance(**kwargs)
    response = Path(observed["response_path"])
    saved = json.loads(response.read_text())
    saved[field] = value
    response.write_text(json.dumps(saved))
    replayed = observe_candidate_source_frame_relevance(**kwargs)
    assert replayed["decision"] == "UNKNOWN"
    assert replayed["action"] == "KEEP"
    assert replayed["drop_authority"] is False
    assert replayed["reason_code"] in {
        "RELEVANCE_DECISION_CACHE_MISMATCH", "RELEVANCE_AUTHORITY_CACHE_MISMATCH",
    }
    assert len(calls) == 1


def test_structured_chinese_scene_evidence_can_match_without_phrase_heuristics(
    tmp_path: Path,
) -> None:
    descriptor, cached, outer_media, srt = _fixture(tmp_path)
    calls: list[tuple[list[Path], str]] = []
    result = observe_candidate_source_frame_relevance(
        candidate_id="candidate-1",
        descriptor=descriptor,
        cached_source=cached,
        source_media=outer_media,
        subtitle_path=srt,
        evidence_root=tmp_path,
        vision_probe=_probe(
            {
                "decision": "MATCH",
                "relation": "SCENE_OR_PROGRAM",
                "visible_evidence": "两组画面展示同一节目中的相同播放器布局和场景事件",
            },
            calls,
        ),
    )

    assert result["decision"] == "MATCH"
    assert result["relevance_observation"]["visible_evidence"].startswith("两组画面")


def test_no_match_stays_keep_and_outer_identity_collision_skips_probe(tmp_path: Path) -> None:
    descriptor, cached, outer_media, srt = _fixture(tmp_path)
    calls: list[tuple[list[Path], str]] = []
    no_match = observe_candidate_source_frame_relevance(
        candidate_id="candidate-1",
        descriptor=descriptor,
        cached_source=cached,
        source_media=outer_media,
        subtitle_path=srt,
        evidence_root=tmp_path,
        vision_probe=_probe(
            {
                "decision": "NO_MATCH",
                "relation": "SCENE_OR_PROGRAM",
                "visible_evidence": "different concrete scene",
            },
            calls,
        ),
    )
    assert no_match["decision"] == "NO_MATCH"
    assert no_match["action"] == "KEEP"
    assert no_match["drop_authority"] is False

    descriptor["nested_media_outer_context"] = {"bvid": "BV1candidate", "cid": 123}
    collision = observe_candidate_source_frame_relevance(
        candidate_id="candidate-1",
        descriptor=descriptor,
        cached_source=cached,
        source_media=outer_media,
        subtitle_path=srt,
        evidence_root=tmp_path / "collision",
        vision_probe=_probe(
            {
                "decision": "MATCH",
                "relation": "SCENE_OR_PROGRAM",
                "visible_evidence": "same concrete program scene",
            },
            calls,
        ),
    )
    assert collision["decision"] == "UNKNOWN"
    assert collision["reason_code"] == "INDEPENDENT_SOURCE_OUTER_IDENTITY_COLLISION"
    assert collision["probe_invocations"] == 0


def test_same_outer_and_source_jpeg_is_allowed_when_media_bindings_differ(tmp_path: Path) -> None:
    descriptor, cached, outer_media, srt = _fixture(tmp_path)
    outer_frame = Path(str(descriptor["caption_frame_path"]))
    source_frame = Path(str(cached["source_frames"][0]["frame_path"]))
    same_bytes = outer_frame.read_bytes()
    source_frame.write_bytes(same_bytes)
    cached["source_frames"][0]["frame_sha256"] = _sha_bytes(same_bytes)
    calls: list[tuple[list[Path], str]] = []

    result = observe_candidate_source_frame_relevance(
        candidate_id="candidate-1",
        descriptor=descriptor,
        cached_source=cached,
        source_media=outer_media,
        subtitle_path=srt,
        evidence_root=tmp_path,
        vision_probe=_probe(
            {
                "decision": "UNKNOWN",
                "relation": "UNKNOWN",
                "visible_evidence": "the exact frame may be shared, but this does not establish identity",
            },
            calls,
        ),
    )

    assert result["decision"] == "UNKNOWN"
    assert result["probe_invocations"] == 1


def test_http_failure_keeps_provider_count_unknown(tmp_path: Path) -> None:
    descriptor, cached, outer_media, srt = _fixture(tmp_path)
    calls: list[tuple[list[Path], str]] = []

    def failed_probe(paths: list[Path], question: str) -> dict[str, object]:
        calls.append((paths, question))
        return {"status": "UNAVAILABLE", "reason_code": "VISION_CALL_FAILED"}

    result = observe_candidate_source_frame_relevance(
        candidate_id="candidate-1",
        descriptor=descriptor,
        cached_source=cached,
        source_media=outer_media,
        subtitle_path=srt,
        evidence_root=tmp_path,
        vision_probe=failed_probe,
    )

    assert result["decision"] == "UNKNOWN"
    assert result["probe_invocations"] == 1
    assert result["provider_calls"] is None
    assert result["provider_calls_this_invocation"] is None


def test_frame_or_evidence_drift_is_unknown_without_probe(tmp_path: Path) -> None:
    descriptor, cached, outer_media, srt = _fixture(tmp_path)
    frame_path = Path(str(descriptor["caption_frame_path"]))
    frame_path.write_bytes(b"drifted-outer-jpeg")
    calls: list[tuple[list[Path], str]] = []
    result = observe_candidate_source_frame_relevance(
        candidate_id="candidate-1",
        descriptor=descriptor,
        cached_source=cached,
        source_media=outer_media,
        subtitle_path=srt,
        evidence_root=tmp_path,
        vision_probe=_probe(
            {
                "decision": "MATCH",
                "relation": "SCENE_OR_PROGRAM",
                "visible_evidence": "same concrete program scene",
            },
            calls,
        ),
    )

    assert result["decision"] == "UNKNOWN"
    assert result["reason_code"] == "CURRENT_FRAME_HASH_MISMATCH"
    assert result["probe_invocations"] == 0


def test_missing_probe_is_unknown_and_does_not_extract_or_call(tmp_path: Path) -> None:
    descriptor, cached, outer_media, srt = _fixture(tmp_path)
    result = observe_candidate_source_frame_relevance(
        candidate_id="candidate-1",
        descriptor=descriptor,
        cached_source=cached,
        source_media=outer_media,
        subtitle_path=srt,
        evidence_root=tmp_path,
        vision_probe=None,
    )

    assert result["status"] == "UNAVAILABLE"
    assert result["decision"] == "UNKNOWN"
    assert result["reason_code"] == "VISION_PROBE_REQUIRED"
    assert result["probe_invocations"] == 0
