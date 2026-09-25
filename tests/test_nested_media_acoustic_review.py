from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from scripts import nested_media_acoustic_review as cli
from src.autoslice.nested_media_acoustic_review import (
    COMPLETE_STATUS,
    PLAN_STATUS,
    NestedMediaAcousticReviewError,
    build_acoustic_review_plan,
    consume_acoustic_review_batch,
    validate_acoustic_review_plan,
)


_SRT = """1
00:00:00,000 --> 00:00:01,000
被观看媒体第一句

2
00:00:01,000 --> 00:00:02,000
主播可能跟读第二句
"""


def _sha(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _files(tmp_path: Path) -> tuple[Path, Path]:
    media = tmp_path / "source.mp4"
    media.write_bytes(b"frozen nested-media audio/video")
    srt = tmp_path / "final.srt"
    srt.write_text(_SRT, encoding="utf-8")
    return media, srt


def _observation(
    tmp_path: Path, *, observation_id: str, frame_ms: int, text: str
) -> dict[str, object]:
    frame = tmp_path / f"{observation_id}.png"
    frame.write_bytes(f"frame:{observation_id}".encode())
    return {
        "schema_version": "embedded-media-caption-observation.v1",
        "observation_id": observation_id,
        "candidate_id": "candidate",
        "frame_ms": frame_ms,
        "caption_present": True,
        "visible_text": text,
        "role": "WATCHED_MEDIA_CAPTION",
        "pipeline_burned_subtitle": False,
        "frame_path": str(frame),
        "frame_sha256": _sha(frame),
    }


def _observations(tmp_path: Path) -> list[dict[str, object]]:
    return [
        _observation(
            tmp_path,
            observation_id="source-only",
            frame_ms=500,
            text="被观看媒体第一句",
        ),
        _observation(
            tmp_path,
            observation_id="possible-overlap",
            frame_ms=1_500,
            text="主播可能跟读第二句",
        ),
    ]


def _manifest(tmp_path: Path) -> Path:
    evidence = tmp_path / "visual-observation-source.json"
    evidence.write_text('{"status":"OBSERVED"}\n', encoding="utf-8")
    observations = _observations(tmp_path)
    manifest = tmp_path / "observations.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": "nested-media-observation-manifest.v1",
                "status": "COMPLETE_OBSERVATION_SET",
                "candidate_id": "candidate",
                "expected_observation_count": len(observations),
                "source_evidence": [
                    {
                        "path": str(evidence),
                        "sha256": _sha(evidence),
                        "bytes": evidence.stat().st_size,
                    }
                ],
                "observations": observations,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return manifest


def _plan(tmp_path: Path) -> dict[str, object]:
    media, srt = _files(tmp_path)
    manifest = _manifest(tmp_path)
    return build_acoustic_review_plan(
        candidate_id="candidate",
        source_media_path=media,
        source_media_sha256=_sha(media),
        final_srt_path=srt,
        final_srt_sha256=_sha(srt),
        observation_manifest_path=manifest,
        observation_manifest_sha256=_sha(manifest),
    )


def _receipt(
    tmp_path: Path,
    *,
    plan: dict[str, object],
    observation_id: str,
    source: str,
    host: str,
    overlap: str,
) -> dict[str, object]:
    items = {
        str(item["observation_id"]): item
        for item in plan["items"]
        if isinstance(item, dict)
    }
    observations = {
        str(item["observation_id"]): item
        for item in plan["observations"]
        if isinstance(item, dict)
    }
    item = items[observation_id]
    observation = observations[observation_id]
    receipt = tmp_path / f"{observation_id}.acoustic.json"
    payload = {
        "schema_version": "nested-media-acoustic-attribution.v1",
        "status": "PASS",
        "observation_id": observation_id,
        "candidate_id": "candidate",
        "interval_ms": item["review_interval_ms"],
        "source_media_speech": source,
        "host_speech": host,
        "overlap_speech": overlap,
        "method": "HASH_BOUND_OPERATOR_LOCAL_AUDIO_REVIEW",
        "accepted_by": "synthetic-reviewer",
        "accepted_at": "2026-09-25T00:00:00Z",
        "review_plan_sha256": plan["plan_sha256"],
        "bindings": {
            "source_media_sha256": plan["source_media"]["sha256"],
            "final_srt_sha256": plan["final_srt"]["sha256"],
            "caption_frame_sha256": observation["frame_sha256"],
        },
    }
    receipt.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {"path": str(receipt), "sha256": _sha(receipt)}


def test_observation_manifest_hash_drift_is_rejected(tmp_path: Path):
    media, srt = _files(tmp_path)
    manifest = _manifest(tmp_path)
    expected = _sha(manifest)
    manifest.write_text('{"observations": []}\n', encoding="utf-8")

    with pytest.raises(
        NestedMediaAcousticReviewError, match="OBSERVATION_MANIFEST_HASH_DRIFT"
    ):
        build_acoustic_review_plan(
            candidate_id="candidate",
            source_media_path=media,
            source_media_sha256=_sha(media),
            final_srt_path=srt,
            final_srt_sha256=_sha(srt),
            observation_manifest_path=manifest,
            observation_manifest_sha256=expected,
        )


def test_incomplete_observation_manifest_cannot_start_plan(tmp_path: Path):
    media, srt = _files(tmp_path)
    manifest = _manifest(tmp_path)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["status"] = "PARTIAL_OBSERVATION_SET"
    manifest.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(
        NestedMediaAcousticReviewError, match="OBSERVATION_MANIFEST_INCOMPLETE"
    ):
        build_acoustic_review_plan(
            candidate_id="candidate",
            source_media_path=media,
            source_media_sha256=_sha(media),
            final_srt_path=srt,
            final_srt_sha256=_sha(srt),
            observation_manifest_path=manifest,
            observation_manifest_sha256=_sha(manifest),
        )


def test_observation_source_evidence_drift_is_rejected(tmp_path: Path):
    media, srt = _files(tmp_path)
    manifest = _manifest(tmp_path)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    evidence = Path(payload["source_evidence"][0]["path"])
    evidence.write_text('{"status":"DRIFTED"}\n', encoding="utf-8")

    with pytest.raises(
        NestedMediaAcousticReviewError,
        match="OBSERVATION_SOURCE_EVIDENCE_HASH_DRIFT",
    ):
        build_acoustic_review_plan(
            candidate_id="candidate",
            source_media_path=media,
            source_media_sha256=_sha(media),
            final_srt_path=srt,
            final_srt_sha256=_sha(srt),
            observation_manifest_path=manifest,
            observation_manifest_sha256=_sha(manifest),
        )


def test_plan_freezes_exact_observation_set_without_mutation(tmp_path: Path):
    plan = _plan(tmp_path)

    assert plan["status"] == PLAN_STATUS
    assert plan["mutation_authorized"] is False
    assert plan["publication_authority"] is False
    assert [item["observation_id"] for item in plan["items"]] == [
        "source-only",
        "possible-overlap",
    ]
    assert [item["review_interval_ms"] for item in plan["items"]] == [
        [0, 1_000],
        [1_000, 2_000],
    ]
    assert validate_acoustic_review_plan(plan) == plan


def test_partial_batch_never_authorizes_a_drop(tmp_path: Path):
    plan = _plan(tmp_path)
    first = _receipt(
        tmp_path,
        plan=plan,
        observation_id="source-only",
        source="PRESENT",
        host="ABSENT",
        overlap="ABSENT",
    )

    result = consume_acoustic_review_batch(
        plan=plan, acoustic_receipt_bindings=[first]
    )

    assert result["status"] == PLAN_STATUS
    assert result["complete"] is False
    assert result["missing_observation_ids"] == ["possible-overlap"]
    assert result["diagnostic_attribution"]["safe_to_drop_cue_indexes"] == [1]
    assert result["evidence_supported_source_only_cue_indexes"] == []
    assert result["partial_drop_evidence_exposed"] is False
    assert result["mutation_authorized"] is False


def test_complete_batch_converges_but_remains_evidence_only(tmp_path: Path):
    plan = _plan(tmp_path)
    source_only = _receipt(
        tmp_path,
        plan=plan,
        observation_id="source-only",
        source="PRESENT",
        host="ABSENT",
        overlap="ABSENT",
    )
    overlap = _receipt(
        tmp_path,
        plan=plan,
        observation_id="possible-overlap",
        source="PRESENT",
        host="PRESENT",
        overlap="PRESENT",
    )

    result = consume_acoustic_review_batch(
        plan=plan, acoustic_receipt_bindings=[source_only, overlap]
    )

    assert result["status"] == COMPLETE_STATUS
    assert result["complete"] is True
    assert result["missing_observation_ids"] == []
    assert result["evidence_supported_source_only_cue_indexes"] == [1]
    assert result["diagnostic_attribution"]["counts"] == {
        "host": 0,
        "overlap": 1,
        "source_media": 1,
        "unknown": 0,
    }
    assert result["mutation_authorized"] is False
    assert result["publication_authority"] is False


@pytest.mark.parametrize(
    ("field", "error"),
    [
        ("review_plan_sha256", "PLAN_BINDING_MISMATCH"),
        ("interval_ms", "REVIEW_INTERVAL_MISMATCH"),
    ],
)
def test_receipt_plan_or_interval_drift_fails_closed(
    tmp_path: Path, field: str, error: str
):
    plan = _plan(tmp_path)
    binding = _receipt(
        tmp_path,
        plan=plan,
        observation_id="source-only",
        source="PRESENT",
        host="ABSENT",
        overlap="ABSENT",
    )
    receipt = Path(str(binding["path"]))
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    payload[field] = "sha256:" + "0" * 64 if field.endswith("sha256") else [0, 999]
    receipt.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    binding["sha256"] = _sha(receipt)

    with pytest.raises(NestedMediaAcousticReviewError, match=error):
        consume_acoustic_review_batch(
            plan=plan, acoustic_receipt_bindings=[binding]
        )


def test_plan_tamper_fails_even_when_shape_still_looks_valid(tmp_path: Path):
    plan = _plan(tmp_path)
    tampered = copy.deepcopy(plan)
    tampered["items"][0]["review_interval_ms"] = [0, 999]

    with pytest.raises(
        NestedMediaAcousticReviewError, match="REVIEW_PLAN_DIGEST_MISMATCH"
    ):
        validate_acoustic_review_plan(tampered)


def test_rehashed_plan_tamper_still_fails_recomputation(tmp_path: Path):
    plan = _plan(tmp_path)
    tampered = copy.deepcopy(plan)
    tampered["items"][0]["review_interval_ms"] = [0, 999]
    body = dict(tampered)
    body.pop("plan_sha256")
    encoded = json.dumps(
        body,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    tampered["plan_sha256"] = "sha256:" + hashlib.sha256(encoded).hexdigest()

    with pytest.raises(
        NestedMediaAcousticReviewError, match="REVIEW_PLAN_RECOMPUTE_MISMATCH"
    ):
        validate_acoustic_review_plan(tampered)


def test_unaligned_observation_cannot_enter_exact_batch(tmp_path: Path):
    media, srt = _files(tmp_path)
    observation = _observation(
        tmp_path,
        observation_id="outside-cue-grid",
        frame_ms=10_000,
        text="不在任何当前 cue 时间窗内",
    )

    with pytest.raises(
        NestedMediaAcousticReviewError, match="OBSERVATION_CUE_ALIGNMENT_REQUIRED"
    ):
        evidence = tmp_path / "outside-visual-source.json"
        evidence.write_text('{"status":"OBSERVED"}\n', encoding="utf-8")
        manifest = tmp_path / "outside-observations.json"
        manifest.write_text(
            json.dumps(
                {
                    "schema_version": "nested-media-observation-manifest.v1",
                    "status": "COMPLETE_OBSERVATION_SET",
                    "candidate_id": "candidate",
                    "expected_observation_count": 1,
                    "source_evidence": [
                        {
                            "path": str(evidence),
                            "sha256": _sha(evidence),
                            "bytes": evidence.stat().st_size,
                        }
                    ],
                    "observations": [observation],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        build_acoustic_review_plan(
            candidate_id="candidate",
            source_media_path=media,
            source_media_sha256=_sha(media),
            final_srt_path=srt,
            final_srt_sha256=_sha(srt),
            observation_manifest_path=manifest,
            observation_manifest_sha256=_sha(manifest),
        )


def test_cli_plan_and_partial_consume_are_create_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    media, srt = _files(tmp_path)
    observations = _manifest(tmp_path)
    plan_path = tmp_path / "plan.json"
    assert cli.main(
        [
            "plan",
            "--candidate-id",
            "candidate",
            "--source-media",
            str(media),
            "--source-media-sha256",
            _sha(media),
            "--final-srt",
            str(srt),
            "--final-srt-sha256",
            _sha(srt),
            "--observations",
            str(observations),
            "--observations-sha256",
            _sha(observations),
            "--out",
            str(plan_path),
        ]
    ) == 0
    json.loads(plan_path.read_text(encoding="utf-8"))
    bindings = tmp_path / "bindings.json"
    bindings.write_text('{"bindings": []}\n', encoding="utf-8")
    result_path = tmp_path / "batch.json"
    assert cli.main(
        [
            "consume",
            "--plan",
            str(plan_path),
            "--receipts",
            str(bindings),
            "--out",
            str(result_path),
        ]
    ) == 0
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["status"] == PLAN_STATUS
    assert result["evidence_supported_source_only_cue_indexes"] == []
    assert cli.main(
        [
            "consume",
            "--plan",
            str(plan_path),
            "--receipts",
            str(bindings),
            "--out",
            str(result_path),
        ]
    ) == 2
    assert "CREATE_ONLY_OUTPUT_EXISTS" in capsys.readouterr().err
