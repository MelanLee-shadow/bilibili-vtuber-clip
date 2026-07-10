import json
from pathlib import Path

import pytest

from scripts.batch_speaker_review import (
    PLAN_SCHEMA,
    BatchSpeakerReviewError,
    _result_is_reusable,
    validate_plan,
)


SHA_A = "a" * 64
SHA_B = "b" * 64


def _plan() -> dict:
    return {
        "schema_version": PLAN_SCHEMA,
        "date": "2026-07-09",
        "expected_count": 1,
        "upload_authorized": False,
        "entries": [
            {
                "candidate_id": "promo_1",
                "review_name": "01_测试",
                "media_path": "/tmp/source.mp4",
                "source_media_sha256": SHA_A,
                "text_srt_path": "/tmp/final.srt",
                "text_final_srt_sha256": "sha256:" + SHA_B,
            }
        ],
    }


def test_batch_plan_requires_explicit_no_upload_and_exact_count() -> None:
    plan = validate_plan(_plan())
    assert plan["entries"][0]["source_media_sha256"] == SHA_A
    assert plan["entries"][0]["text_final_srt_sha256"] == SHA_B

    bad_upload = _plan()
    bad_upload["upload_authorized"] = True
    with pytest.raises(BatchSpeakerReviewError, match="upload_authorized=false"):
        validate_plan(bad_upload)

    bad_count = _plan()
    bad_count["expected_count"] = 10
    with pytest.raises(BatchSpeakerReviewError, match="expected_count"):
        validate_plan(bad_count)


def test_batch_plan_rejects_duplicate_identity_and_path_names() -> None:
    duplicate = _plan()
    duplicate["expected_count"] = 2
    duplicate["entries"].append(dict(duplicate["entries"][0]))
    with pytest.raises(BatchSpeakerReviewError, match="duplicate candidate_id"):
        validate_plan(duplicate)

    unsafe = _plan()
    unsafe["entries"][0]["review_name"] = "../escape"
    with pytest.raises(BatchSpeakerReviewError, match="invalid review_name"):
        validate_plan(unsafe)


def test_resume_requires_every_artifact_hash_to_match(tmp_path: Path) -> None:
    artifact = tmp_path / "review.mp4"
    artifact.write_bytes(b"video")
    import hashlib

    result = tmp_path / "item.result.json"
    result.write_text(
        json.dumps(
            {
                "schema_version": "lidousha-speaker-review-item.v1",
                "status": "READY",
                "source_media_sha256": SHA_A,
                "text_final_srt_sha256": SHA_B,
                "artifacts": {
                    "video": {
                        "path": str(artifact),
                        "sha256": hashlib.sha256(b"video").hexdigest(),
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    entry = {"source_media_sha256": SHA_A, "text_final_srt_sha256": SHA_B}
    assert _result_is_reusable(result, entry)
    artifact.write_bytes(b"drift")
    assert not _result_is_reusable(result, entry)


def test_july9_plan_is_exactly_the_ten_published_talk_clips() -> None:
    plan_path = Path(__file__).resolve().parents[1] / "assets/lidousha/speaker_batch_plans/2026-07-09.json"
    plan = validate_plan(json.loads(plan_path.read_text(encoding="utf-8")))
    assert {entry["candidate_id"] for entry in plan["entries"]} == {
        "auto_193036_487_758",
        "auto_200028_434_569",
        "auto_203027_549_697",
        "promo_193036_1006_1160",
        "promo_193036_367_476",
        "promo_203027_314_479",
        "promo_210025_643_801",
        "promo_220021_1091_1257",
        "promo_220021_125_232",
        "promo_223019_374_480",
    }
    reviewed = next(entry for entry in plan["entries"] if entry["candidate_id"] == "promo_210025_643_801")
    assert reviewed["text_final_srt_sha256"] == "63438b34dd879c077cc2af6c16f692ae9625a67ed851b31a2cd5a61098874750"
    assert reviewed["speaker_override_sha256"] == "ba8f8386ae614cb338af84f273856e7228ce7893cc15fae3719a6d0565008d4c"
