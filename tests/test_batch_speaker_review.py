import json
from pathlib import Path

import pytest

from scripts.batch_speaker_review import (
    PLAN_SCHEMA,
    BatchSpeakerReviewError,
    REQUIRED_ARTIFACTS,
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
    import hashlib

    names = {
        "video": "01_测试.mp4",
        "text_final_srt": "01_测试.text-final.srt",
        "speaker_srt": "01_测试.speaker.srt",
        "ass": "01_测试.ass",
        "speaker_manifest": "01_测试.speaker.json",
    }
    paths = {key: tmp_path / value for key, value in names.items()}
    paths["video"].write_bytes(b"video")
    paths["text_final_srt"].write_bytes(b"text")
    paths["speaker_srt"].write_bytes(b"speaker")
    paths["ass"].write_text(
        "[V4+ Styles]\n"
        "Style: LDS,Microsoft YaHei,72,&H00FFFFFF,&H000000FF,&H00BA520F,&H70000000,0,0,0,0,100,100,0,0,1,3,2,2,60,60,40,1\n"
        "Style: GUEST,Microsoft YaHei,72,&H00FFFFFF,&H000000FF,&H00203050,&H70000000,-1,0,0,0,100,100,0,0,1,3,2,2,60,60,40,1\n"
        "[Events]\nDialogue: 0,0:00:00.00,0:00:01.00,LDS,,0,0,0,,测试\n",
        encoding="utf-8",
    )
    artifact_rows = {
        key: {
            "path": str(path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "bytes": path.stat().st_size,
        }
        for key, path in paths.items()
        if key != "speaker_manifest"
    }
    paths["speaker_manifest"].write_text(
        json.dumps(
            {
                "status": "READY",
                "production_ready": True,
                "subtitle_style": "lidousha-speaker-sapphire-host-white-guest-v2",
                "speaker_taxonomy": "binary_visual_host_vs_guest",
                "source_media_sha256": SHA_A,
                "text_final_srt_sha256": SHA_B,
                "speaker_override_sha256": None,
                "output_review_srt_sha256": artifact_rows["speaker_srt"]["sha256"],
                "output_ass_sha256": artifact_rows["ass"]["sha256"],
            }
        ),
        encoding="utf-8",
    )
    artifact_rows["speaker_manifest"] = {
        "path": str(paths["speaker_manifest"]),
        "sha256": hashlib.sha256(paths["speaker_manifest"].read_bytes()).hexdigest(),
        "bytes": paths["speaker_manifest"].stat().st_size,
    }

    result = tmp_path / "item.result.json"
    result.write_text(
        json.dumps(
            {
                "schema_version": "lidousha-speaker-review-item.v1",
                "status": "READY",
                "candidate_id": "promo_1",
                "review_name": "01_测试",
                "source_media_sha256": SHA_A,
                "text_final_srt_sha256": SHA_B,
                "speaker_override_sha256": None,
                "subtitle_style": "lidousha-speaker-sapphire-host-white-guest-v2",
                "upload_authorized": False,
                "artifacts": artifact_rows,
            }
        ),
        encoding="utf-8",
    )
    entry = {
        "candidate_id": "promo_1",
        "review_name": "01_测试",
        "source_media_sha256": SHA_A,
        "text_final_srt_sha256": SHA_B,
        "speaker_override_sha256": None,
    }
    assert _result_is_reusable(result, entry)
    paths["video"].write_bytes(b"drift")
    assert not _result_is_reusable(result, entry)


def test_resume_rejects_empty_partial_wrong_identity_override_or_style(tmp_path: Path) -> None:
    result = tmp_path / "item.result.json"
    base = {
        "schema_version": "lidousha-speaker-review-item.v1",
        "status": "READY",
        "candidate_id": "wrong",
        "review_name": "01_测试",
        "source_media_sha256": SHA_A,
        "text_final_srt_sha256": SHA_B,
        "speaker_override_sha256": "c" * 64,
        "subtitle_style": "old-yellow-style",
        "upload_authorized": False,
        "artifacts": {},
    }
    result.write_text(json.dumps(base), encoding="utf-8")
    entry = {
        "candidate_id": "promo_1",
        "review_name": "01_测试",
        "source_media_sha256": SHA_A,
        "text_final_srt_sha256": SHA_B,
        "speaker_override_sha256": None,
    }
    assert not _result_is_reusable(result, entry)
    base.update(candidate_id="promo_1", speaker_override_sha256=None, subtitle_style="lidousha-speaker-sapphire-host-white-guest-v2")
    base["artifacts"] = {name: {} for name in list(REQUIRED_ARTIFACTS)[:1]}
    result.write_text(json.dumps(base), encoding="utf-8")
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
