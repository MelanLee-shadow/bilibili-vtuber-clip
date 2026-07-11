import hashlib
import json
import shutil
from pathlib import Path

import pytest

import scripts.batch_speaker_review as batch_review
from scripts.apply_subtitle_text_overrides import apply_document
from scripts.batch_speaker_review import (
    PLAN_SCHEMA,
    BatchSpeakerReviewError,
    REQUIRED_ARTIFACTS,
    _generator_sha256,
    _result_is_reusable,
    resolve_staged_repo_asset,
    validate_plan,
)


SHA_A = "a" * 64
SHA_B = "b" * 64


def _plan() -> dict:
    return {
        "schema_version": PLAN_SCHEMA,
        "date": "2026-07-09",
        "production_scope": "retrospective_speaker_rerender",
        "expected_count": 1,
        "upload_authorized": False,
        "entries": [
            {
                "candidate_id": "promo_1",
                "review_name": "01_测试",
                "production_scope": "retrospective_speaker_rerender",
                "media_path": "/tmp/source.mp4",
                "source_media_sha256": SHA_A,
                "text_srt_path": "/tmp/final.srt",
                "text_authority_mode": "historical_published_final",
                "chat_authority_status": "NOT_EVALUATED_RETROSPECTIVE",
                "text_revalidation": False,
                "text_final_srt_sha256": "sha256:" + SHA_B,
            }
        ],
    }


def test_batch_plan_requires_explicit_no_upload_and_exact_count() -> None:
    plan = validate_plan(_plan())
    assert plan["entries"][0]["source_media_sha256"] == SHA_A
    assert plan["entries"][0]["text_final_srt_sha256"] == SHA_B
    assert plan["entries"][0]["text_authority_mode"] == "historical_published_final"

    bad_upload = _plan()
    bad_upload["upload_authorized"] = True
    with pytest.raises(BatchSpeakerReviewError, match="upload_authorized=false"):
        validate_plan(bad_upload)

    bad_count = _plan()
    bad_count["expected_count"] = 10
    with pytest.raises(BatchSpeakerReviewError, match="expected_count"):
        validate_plan(bad_count)

    misleading_scope = _plan()
    misleading_scope["production_scope"] = "new_end_to_end_text_production"
    with pytest.raises(BatchSpeakerReviewError, match="production_scope"):
        validate_plan(misleading_scope)


def test_batch_plan_requires_explicit_retrospective_text_authority() -> None:
    missing_mode = _plan()
    del missing_mode["entries"][0]["text_authority_mode"]
    with pytest.raises(BatchSpeakerReviewError, match="text_authority_mode"):
        validate_plan(missing_mode)

    false_verified_chat = _plan()
    false_verified_chat["entries"][0]["chat_authority_status"] = "VERIFIED"
    with pytest.raises(BatchSpeakerReviewError, match="NOT_EVALUATED_RETROSPECTIVE"):
        validate_plan(false_verified_chat)

    false_revalidation = _plan()
    false_revalidation["entries"][0]["text_revalidation"] = True
    with pytest.raises(BatchSpeakerReviewError, match="text_revalidation"):
        validate_plan(false_revalidation)

    unbound_ivan_claim = _plan()
    unbound_ivan_claim["entries"][0]["text_authority_mode"] = (
        "historical_published_final_plus_ivan_override"
    )
    with pytest.raises(BatchSpeakerReviewError, match="mode and operational Ivan override disagree"):
        validate_plan(unbound_ivan_claim)

    mislabeled_human_override = _plan()
    mislabeled_human_override["entries"][0].update(
        text_source_srt_sha256=SHA_A,
        subtitle_text_override_path="/tmp/text-overrides.json",
        subtitle_text_override_sha256="c" * 64,
    )
    with pytest.raises(BatchSpeakerReviewError, match="mode and operational Ivan override disagree"):
        validate_plan(mislabeled_human_override)

    hash_only_authority_ref = _plan()
    hash_only_authority_ref["entries"][0].update(
        text_authority_ref_path="/tmp/speaker-override.json",
        text_authority_ref_sha256="d" * 64,
    )
    with pytest.raises(BatchSpeakerReviewError, match="references are unsupported"):
        validate_plan(hash_only_authority_ref)


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


def test_batch_plan_hash_binds_optional_source_session_anchors() -> None:
    plan = _plan()
    plan["entries"][0].update(
        source_session_anchor_path="/tmp/session.json",
        source_session_anchor_sha256="c" * 64,
    )
    normalized = validate_plan(plan)["entries"][0]
    assert normalized["source_session_anchor_sha256"] == "c" * 64

    missing_hash = _plan()
    missing_hash["entries"][0]["source_session_anchor_path"] = "/tmp/session.json"
    with pytest.raises(BatchSpeakerReviewError, match="source_session_anchor"):
        validate_plan(missing_hash)


def test_batch_plan_hash_binds_optional_text_finalization() -> None:
    plan = _plan()
    plan["entries"][0].update(
        text_authority_mode="historical_published_final_plus_ivan_override",
        text_source_srt_sha256=SHA_A,
        subtitle_text_override_path="/tmp/text-overrides.json",
        subtitle_text_override_sha256="c" * 64,
    )
    normalized = validate_plan(plan)["entries"][0]
    assert normalized["text_source_srt_sha256"] == SHA_A
    assert normalized["text_final_srt_sha256"] == SHA_B
    assert normalized["subtitle_text_override_sha256"] == "c" * 64

    missing_source = _plan()
    missing_source["entries"][0].update(
        text_authority_mode="historical_published_final_plus_ivan_override",
        subtitle_text_override_path="/tmp/text-overrides.json",
        subtitle_text_override_sha256="c" * 64,
    )
    with pytest.raises(BatchSpeakerReviewError, match="requires text_source_srt_sha256"):
        validate_plan(missing_source)

    missing_override_hash = _plan()
    missing_override_hash["entries"][0].update(
        text_authority_mode="historical_published_final_plus_ivan_override",
        text_source_srt_sha256=SHA_A,
        subtitle_text_override_path="/tmp/text-overrides.json",
    )
    with pytest.raises(BatchSpeakerReviewError, match="subtitle_text_override"):
        validate_plan(missing_override_hash)

    unexplained_distinct_hash = _plan()
    unexplained_distinct_hash["entries"][0]["text_source_srt_sha256"] = SHA_A
    with pytest.raises(BatchSpeakerReviewError, match="without an override"):
        validate_plan(unexplained_distinct_hash)


def test_staged_repo_assets_are_canonical_and_contained(tmp_path: Path) -> None:
    staged = tmp_path / "stage"
    asset = staged / "assets/lidousha/decision.json"
    asset.parent.mkdir(parents=True)
    asset.write_text("{}", encoding="utf-8")
    assert resolve_staged_repo_asset(
        "/opt/bilive/autoslice/repo/assets/lidousha/decision.json",
        staged_root=staged,
    ) == asset.resolve()

    for escaped in (
        "/opt/bilive/autoslice/repo//etc/passwd",
        "/opt/bilive/autoslice/repo/../decision.json",
        "/etc/passwd",
    ):
        with pytest.raises(BatchSpeakerReviewError):
            resolve_staged_repo_asset(escaped, staged_root=staged)


def test_resume_requires_every_artifact_hash_to_match(tmp_path: Path) -> None:
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
    text_sha = hashlib.sha256(paths["text_final_srt"].read_bytes()).hexdigest()
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
                "host_identity_aliases": ["李豆沙", "shadow"],
                "profile_sha256": hashlib.sha256(
                    (
                        Path(__file__).resolve().parents[1]
                        / "assets/lidousha/voiceprint_profile.v1.json"
                    ).read_bytes()
                ).hexdigest(),
                "source_media_sha256": SHA_A,
                "text_final_srt_sha256": text_sha,
                "speaker_override_sha256": None,
                "source_session_anchor_manifest_sha256": None,
                "host_anchor_scope": "clip",
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
                "production_scope": "retrospective_speaker_rerender",
                "source_media_sha256": SHA_A,
                "text_source_srt_sha256": text_sha,
                "text_final_srt_sha256": text_sha,
                "subtitle_text_override_sha256": None,
                "text_authority_mode": "historical_published_final",
                "chat_authority_status": "NOT_EVALUATED_RETROSPECTIVE",
                "text_revalidation": False,
                "speaker_override_sha256": None,
                "source_session_anchor_sha256": None,
                "generator_sha256": _generator_sha256(),
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
        "text_source_srt_sha256": text_sha,
        "text_final_srt_sha256": text_sha,
        "subtitle_text_override_sha256": None,
        "text_authority_mode": "historical_published_final",
        "chat_authority_status": "NOT_EVALUATED_RETROSPECTIVE",
        "text_revalidation": False,
        "speaker_override_sha256": None,
        "source_session_anchor_sha256": None,
    }
    generator_sha256 = _generator_sha256()
    assert _result_is_reusable(result, entry, generator_sha256=generator_sha256)
    paths["text_final_srt"].write_bytes(b"self-consistent but wrong packaged text")
    result_document = json.loads(result.read_text(encoding="utf-8"))
    result_document["artifacts"]["text_final_srt"].update(
        sha256=hashlib.sha256(paths["text_final_srt"].read_bytes()).hexdigest(),
        bytes=paths["text_final_srt"].stat().st_size,
    )
    result.write_text(json.dumps(result_document), encoding="utf-8")
    assert not _result_is_reusable(result, entry, generator_sha256=generator_sha256)

    paths["text_final_srt"].write_bytes(b"text")
    result_document["artifacts"]["text_final_srt"].update(
        sha256=hashlib.sha256(paths["text_final_srt"].read_bytes()).hexdigest(),
        bytes=paths["text_final_srt"].stat().st_size,
    )
    result.write_text(json.dumps(result_document), encoding="utf-8")
    assert _result_is_reusable(result, entry, generator_sha256=generator_sha256)
    paths["video"].write_bytes(b"drift")
    assert not _result_is_reusable(result, entry, generator_sha256=generator_sha256)


def test_resume_rejects_empty_partial_wrong_identity_override_or_style(tmp_path: Path) -> None:
    generator_sha256 = _generator_sha256()
    result = tmp_path / "item.result.json"
    base = {
        "schema_version": "lidousha-speaker-review-item.v1",
        "status": "READY",
        "candidate_id": "wrong",
        "review_name": "01_测试",
        "source_media_sha256": SHA_A,
        "text_source_srt_sha256": SHA_B,
        "text_final_srt_sha256": SHA_B,
        "subtitle_text_override_sha256": None,
        "text_authority_mode": "historical_published_final",
        "chat_authority_status": "NOT_EVALUATED_RETROSPECTIVE",
        "text_revalidation": False,
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
        "text_source_srt_sha256": SHA_B,
        "text_final_srt_sha256": SHA_B,
        "subtitle_text_override_sha256": None,
        "text_authority_mode": "historical_published_final",
        "chat_authority_status": "NOT_EVALUATED_RETROSPECTIVE",
        "text_revalidation": False,
        "speaker_override_sha256": None,
    }
    assert not _result_is_reusable(result, entry, generator_sha256=generator_sha256)
    base.update(candidate_id="promo_1", speaker_override_sha256=None, subtitle_style="lidousha-speaker-sapphire-host-white-guest-v2")
    base["artifacts"] = {name: {} for name in list(REQUIRED_ARTIFACTS)[:1]}
    result.write_text(json.dumps(base), encoding="utf-8")
    assert not _result_is_reusable(result, entry, generator_sha256=generator_sha256)


def test_build_applies_bound_text_finalization_before_speaker_inference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_media = tmp_path / "source.mp4"
    source_media.write_bytes(b"source media")
    source_srt = tmp_path / "source.srt"
    source_srt.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nTA想解决的只有学校这个建筑\n",
        encoding="utf-8",
    )
    source_srt_sha = hashlib.sha256(source_srt.read_bytes()).hexdigest()
    override = tmp_path / "text-override.json"
    decision = {
        "schema_version": 1,
        "candidate_id": "promo_test",
        "source_srt_sha256": source_srt_sha,
        "overrides": [
            {
                "source_cue": 1,
                "expect": {
                    "start": "00:00:00,000",
                    "end": "00:00:01,000",
                    "text": "TA想解决的只有学校这个建筑",
                },
                "authority": "Ivan: known Li Dousha referent uses 她",
                "text": "她想解决的只有学校这个建筑",
            }
        ],
    }
    override.write_text(json.dumps(decision, ensure_ascii=False), encoding="utf-8")
    expected = tmp_path / "expected.srt"
    apply_document(source_srt, override, expected, tmp_path / "expected.json")
    expected_sha = hashlib.sha256(expected.read_bytes()).hexdigest()
    decision["text_final_srt_sha256"] = expected_sha
    override.write_text(json.dumps(decision, ensure_ascii=False), encoding="utf-8")

    output_dir = tmp_path / "review"
    output_dir.mkdir()
    observed: dict[str, str] = {}

    def fake_speaker_finalizer(**kwargs: object) -> dict[str, object]:
        text_path = Path(str(kwargs["text_srt_path"]))
        observed["text_path"] = str(text_path)
        observed["text"] = text_path.read_text(encoding="utf-8")
        assert observed["text"] == expected.read_text(encoding="utf-8")
        assert "TA想解决" not in observed["text"]

        speaker_srt = Path(str(kwargs["output_srt_path"]))
        speaker_srt.write_text(observed["text"], encoding="utf-8")
        ass = Path(str(kwargs["output_ass_path"]))
        ass.write_text(
            "[V4+ Styles]\n"
            f"Style: LDS,{batch_review.LDS_SAPPHIRE_STYLE}\n"
            f"Style: GUEST,{batch_review.GUEST_WHITE_STYLE}\n"
            "[Events]\n"
            "Dialogue: 0,0:00:00.00,0:00:01.00,LDS,,0,0,0,,她想解决的只有学校这个建筑\n",
            encoding="utf-8",
        )
        manifest = {
            "status": "READY",
            "production_ready": True,
            "subtitle_style": batch_review.SPEAKER_SUBTITLE_STYLE_ID,
            "speaker_taxonomy": "binary_visual_host_vs_guest",
            "host_identity_aliases": ["李豆沙", "shadow"],
            "profile_sha256": hashlib.sha256(
                (batch_review.ROOT / "assets/lidousha/voiceprint_profile.v1.json").read_bytes()
            ).hexdigest(),
            "source_media_sha256": hashlib.sha256(source_media.read_bytes()).hexdigest(),
            "text_final_srt_sha256": expected_sha,
            "speaker_override_sha256": None,
            "source_session_anchor_manifest_sha256": None,
            "host_anchor_scope": "clip",
            "output_review_srt_sha256": hashlib.sha256(speaker_srt.read_bytes()).hexdigest(),
            "output_ass_sha256": hashlib.sha256(ass.read_bytes()).hexdigest(),
            "source_cue_count": 1,
            "output_cue_count": 1,
            "final_decisions": [{"speaker": "李豆沙"}],
        }
        Path(str(kwargs["output_manifest_path"])).write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
        )
        return manifest

    def fake_burn(state: dict[str, object], *, run_ffmpeg: bool) -> dict[str, object]:
        assert run_ffmpeg is True
        burned = tmp_path / "burned.mp4"
        shutil.copy2(Path(str(state["media_path"])), burned)
        return {"burned_preview": {"status": "BURNED", "path": str(burned)}}

    monkeypatch.setattr(batch_review, "run_speaker_finalizer", fake_speaker_finalizer)
    monkeypatch.setattr(batch_review, "_burn_preview_subtitles", fake_burn)

    entry = validate_plan(
        {
            "schema_version": PLAN_SCHEMA,
            "date": "2026-07-09",
            "production_scope": "retrospective_speaker_rerender",
            "expected_count": 1,
            "upload_authorized": False,
            "entries": [
                {
                    "candidate_id": "promo_test",
                    "review_name": "15_测试",
                    "media_path": str(source_media),
                    "source_media_sha256": hashlib.sha256(source_media.read_bytes()).hexdigest(),
                    "text_srt_path": str(source_srt),
                    "text_authority_mode": "historical_published_final_plus_ivan_override",
                    "chat_authority_status": "NOT_EVALUATED_RETROSPECTIVE",
                    "text_revalidation": False,
                    "text_source_srt_sha256": source_srt_sha,
                    "text_final_srt_sha256": expected_sha,
                    "subtitle_text_override_path": str(override),
                    "subtitle_text_override_sha256": hashlib.sha256(
                        override.read_bytes()
                    ).hexdigest(),
                }
            ],
        }
    )["entries"][0]
    result = batch_review.build_review_item(
        entry,
        output_dir=output_dir,
        speaker_python=Path("/unused/python"),
        resume=False,
        generator_sha256=_generator_sha256(),
    )

    assert Path(observed["text_path"]).name == "15_测试.text-final.srt"
    assert result["text_source_srt_sha256"] == source_srt_sha
    assert result["text_final_srt_sha256"] == expected_sha
    assert result["text_authority_mode"] == (
        "historical_published_final_plus_ivan_override"
    )
    assert result["chat_authority_status"] == "NOT_EVALUATED_RETROSPECTIVE"
    assert result["text_revalidation"] is False
    assert result["subtitle_text_override_sha256"] == hashlib.sha256(
        override.read_bytes()
    ).hexdigest()
    assert Path(str(result["artifacts"]["text_final_srt"]["path"])).read_text(
        encoding="utf-8"
    ) == expected.read_text(encoding="utf-8")


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
    assert reviewed["text_authority_mode"] == (
        "historical_published_final_plus_ivan_override"
    )
    assert reviewed["text_source_srt_sha256"] == (
        "a61938aec26d340c6d6abb9460bc38b769ee3a26e4bd49ed11006efb4a35c55c"
    )
    assert reviewed["subtitle_text_override_sha256"] == (
        "a6f9c52517a422ccfeeec5295a6175525de84312395cde6299641b697ee88281"
    )
    assert reviewed["speaker_override_sha256"] == "ba8f8386ae614cb338af84f273856e7228ce7893cc15fae3719a6d0565008d4c"
    kitchen = next(entry for entry in plan["entries"] if entry["candidate_id"] == "promo_220021_125_232")
    assert kitchen["source_session_anchor_sha256"] == "2d869a8efae298257e0f17be8d15f9c51e4c64aa15a7a840f176b1b56fd99b94"
    anchor_path = Path(__file__).resolve().parents[1] / "assets/lidousha/speaker_session_anchors/2026-07-09-220021.v1.json"
    assert hashlib.sha256(anchor_path.read_bytes()).hexdigest() == kitchen["source_session_anchor_sha256"]
    monologue = next(
        entry for entry in plan["entries"] if entry["candidate_id"] == "promo_223019_374_480"
    )
    monologue_anchor = (
        Path(__file__).resolve().parents[1]
        / "assets/lidousha/speaker_session_anchors/2026-07-09-223019.v1.json"
    )
    assert hashlib.sha256(monologue_anchor.read_bytes()).hexdigest() == monologue[
        "source_session_anchor_sha256"
    ]
    pronoun_fix = next(
        entry for entry in plan["entries"] if entry["candidate_id"] == "promo_193036_367_476"
    )
    assert pronoun_fix["text_source_srt_sha256"] == (
        "1529026cf1bdc55424dc271eecd4f7cd034f00f5b336dc18ad3e3e45b37343b5"
    )
    assert pronoun_fix["text_final_srt_sha256"] == (
        "66df43bb0478ba0106617ad1ae77f425cb3dac359e85a6d1d36db95e44debd15"
    )
    assert pronoun_fix["text_authority_mode"] == (
        "historical_published_final_plus_ivan_override"
    )
    assert {entry["chat_authority_status"] for entry in plan["entries"]} == {
        "NOT_EVALUATED_RETROSPECTIVE"
    }
    assert all(entry["text_revalidation"] is False for entry in plan["entries"])
    text_override = (
        Path(__file__).resolve().parents[1]
        / "assets/lidousha/subtitle_text_overrides/promo_193036_367_476.text.v1.json"
    )
    assert hashlib.sha256(text_override.read_bytes()).hexdigest() == pronoun_fix[
        "subtitle_text_override_sha256"
    ]
