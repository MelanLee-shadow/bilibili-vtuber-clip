"""Local synthetic artifact checks; no SSH, provider, account or publication calls."""

import copy
import hashlib
import json

import pytest

from src.autoslice import original_patch_package as owner


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


@pytest.fixture
def bundle(tmp_path):
    root = tmp_path.resolve()
    names = {
        "media": "test.mp4",
        "subtitle_srt": "test.srt",
        "ass_path": "test.ass",
        "cover": "test.cover.png",
        "publish_json": "test.publish.json",
    }
    for key in ("media", "subtitle_srt", "ass_path", "cover"):
        (root / names[key]).write_bytes(("synthetic " + key).encode())
    hashes = {
        k: "sha256:" + sha((root / names[role]).read_bytes())
        for k, role in {
            "burned_video_sha256": "media",
            "subtitle_sha256": "subtitle_srt",
            "ass_sha256": "ass_path",
            "cover_sha256": "cover",
        }.items()
    }
    fact = {
        "schema_version": "fixture-only",
        "status": "PASS",
        "final_title": "【李豆沙】原稿定点修正",
    }
    publish = {
        "title": fact["final_title"],
        "source_fact_review": fact,
        "cover_path": str(root / names["cover"]),
        "artifact_hashes": dict(hashes),
    }
    raw = (json.dumps(publish, ensure_ascii=False) + "\n").encode()
    (root / names["publish_json"]).write_bytes(raw)
    record = {
        "artifact_hashes": {**hashes, "publish_draft_sha256": "sha256:" + sha(raw)},
        "publish_staging": {"title": fact["final_title"], "source_fact_review": fact},
    }
    return root, {**names, "title": fact["final_title"]}, record, publish


def check(bundle):
    root, item, record, _ = bundle
    owner.verify_publish_artifact_bindings(root, item, record)


def rewrite(bundle, publish):
    root, item, record, _ = bundle
    raw = (json.dumps(publish, ensure_ascii=False) + "\n").encode()
    (root / item["publish_json"]).write_bytes(raw)
    record["artifact_hashes"]["publish_draft_sha256"] = "sha256:" + sha(raw)


def test_actual_primary_artifacts_and_publish_draft_align(bundle):
    check(bundle)


@pytest.mark.parametrize("field", ["title", "source_fact_review", "cover_path", "artifact_hashes"])
def test_resigned_publish_draft_cannot_change_meaning(bundle, field):
    bad = copy.deepcopy(bundle[3])
    if field == "title":
        bad[field] = "【李豆沙】另一条内容"
    elif field == "source_fact_review":
        bad[field]["final_title"] = "未检查的标题"
    elif field == "cover_path":
        bad[field] = str(bundle[0] / "another.png")
    else:
        bad[field]["subtitle_sha256"] = "sha256:" + "0" * 64
    rewrite(bundle, bad)
    with pytest.raises(owner.OriginalPackageError):
        check(bundle)


@pytest.mark.parametrize(
    "field",
    [
        "burned_video_sha256",
        "subtitle_sha256",
        "ass_sha256",
        "cover_sha256",
        "publish_draft_sha256",
    ],
)
def test_record_must_bind_actual_bytes_not_only_matching_native_claim(bundle, field):
    bundle[2]["artifact_hashes"][field] = "sha256:" + "1" * 64
    with pytest.raises(owner.OriginalPackageError):
        check(bundle)


@pytest.mark.parametrize("role", ["media", "subtitle_srt", "ass_path", "cover"])
def test_replacement_of_a_primary_file_fails(bundle, role):
    (bundle[0] / bundle[1][role]).write_bytes(b"unlisted change")
    with pytest.raises(owner.OriginalPackageError):
        check(bundle)


def test_old_before_image_never_becomes_new_published_draft(bundle):
    (bundle[0] / bundle[1]["publish_json"]).write_text("{}")
    with pytest.raises(owner.OriginalPackageError):
        check(bundle)


def test_missing_fields_are_not_silently_defaulted(bundle):
    del bundle[2]["artifact_hashes"]["ass_sha256"]
    with pytest.raises(owner.OriginalPackageError):
        check(bundle)


def test_new_upload_remains_disallowed_for_original_repair_authority():
    from src.autoslice.final_human_review import ordinary_upload_problems

    manifest = {
        "recovery_publication_authority": {
            "schema_version": owner.AUTHORITY_SCHEMA,
            "same_bv_only": True,
        }
    }
    assert ordinary_upload_problems(manifest)


@pytest.mark.parametrize(
    ("timestamp", "expected"),
    [("0:00:00.00", 0), ("0:01:42.37", 102370), ("12:59:59.99", 46799990)],
)
def test_exact_ass_timestamp_parser(timestamp, expected):
    assert owner._ass_milliseconds(timestamp) == expected


@pytest.mark.parametrize(
    "timestamp", ["0:60:00.00", "0:01:60.00", "-1:00:00.00", "0:1:00.00", "0:00:01.001", None, True]
)
def test_invalid_ass_clock_never_passes_caption_gate(timestamp):
    with pytest.raises(owner.OriginalPackageError):
        owner._ass_milliseconds(timestamp)


def test_json_reader_rejects_link_and_nonobject(tmp_path):
    regular = tmp_path / "plain.json"
    regular.write_text("{}")
    link = tmp_path / "link.json"
    link.symlink_to(regular)
    with pytest.raises(owner.OriginalPackageError):
        owner.json_file(link)
    regular.write_text("[]")
    with pytest.raises(owner.OriginalPackageError):
        owner.json_file(regular)


def test_json_reader_enforces_size_bound(tmp_path):
    p = tmp_path / "oversized.json"
    with p.open("wb") as f:
        f.truncate(8 * 1024 * 1024 + 1)
    with pytest.raises(owner.OriginalPackageError):
        owner.json_file(p)


def test_original_schema_really_calls_current_package_validator(monkeypatch, tmp_path):
    from scripts import audit_review_package as auditor

    calls = []

    def reject(root, manifest):
        calls.append((root, manifest))
        raise owner.OriginalPackageError("unsealed original source")

    monkeypatch.setattr(owner, "validate_package", reject)
    (tmp_path / "review_manifest.json").write_text(
        json.dumps({"schema_version": owner.MANIFEST_SCHEMA, "upload_allowed": False, "items": []})
    )
    result = auditor.audit_package(tmp_path)
    assert len(calls) == 1 and result["passed"] is False
    assert any(row["code"] == "ORIGINAL_REVIEW_PATCH_CLOSURE_INVALID" for row in result["issues"])


def test_ordinary_package_does_not_select_original_lane(monkeypatch, tmp_path):
    monkeypatch.setattr(owner, "validate_package", lambda *_: pytest.fail("wrong dispatch"))
    assert owner.audit_if_original(tmp_path, {"schema_version": "ordinary-private.v1"}) is None


def test_bad_record_shape_is_rejected_before_burn_or_network(monkeypatch, tmp_path):
    from src.autoslice import original_patch_review as review

    monkeypatch.setattr(
        review,
        "_bindings",
        lambda *_args, **_kwargs: pytest.fail("bad receipt should reject first"),
    )
    with pytest.raises(owner.OriginalPackageError):
        review.validate_technical_receipt(
            {"schema_version": owner.TECHNICAL_SCHEMA},
            tmp_path,
            tmp_path / "audit.json",
            publication_authority={},
        )


def technical_binding():
    return {
        "closure": {
            "candidate_id": "synthetic",
            "authority": {"bvid": "BV0123456789", "same_bv_only": True},
        },
        "review_manifest_sha256": "a" * 64,
        "package_audit_sha256": "b" * 64,
    }


def technical_receipt(binding):
    return {
        "schema_version": owner.TECHNICAL_SCHEMA,
        "candidate_id": "synthetic",
        "reviewed_at": "2026-09-09T00:00:00+00:00",
        "reviewed_by": "ChatGPT root",
        "status": "VERIFIED_ORIGINAL_PLUS_DELTA",
        "scope": "original_review_preserved_and_actual_delta_verified",
        "fresh_human_full_playback_claimed": False,
        "new_upload_authorized": False,
        "bindings": binding,
    }


def test_technical_receipt_is_explicitly_not_a_human_review_or_upload_permit(monkeypatch, tmp_path):
    from src.autoslice import original_patch_review as review

    binding = technical_binding()
    r = technical_receipt(binding)
    monkeypatch.setattr(review, "_bindings", lambda *_a, **_kw: copy.deepcopy(binding))
    assert (
        review.validate_technical_receipt(
            r,
            tmp_path,
            tmp_path / "audit.json",
            publication_authority=binding["closure"]["authority"],
        )
        == r
    )


@pytest.mark.parametrize(
    "field",
    [
        "new_upload_authorized",
        "fresh_human_full_playback_claimed",
        "reviewed_by",
        "candidate_id",
        "bindings",
        "naive_time",
        "wrong_BV",
    ],
)
def test_technical_receipt_rejects_overclaim_or_resigning(monkeypatch, tmp_path, field):
    from src.autoslice import original_patch_review as review

    binding = technical_binding()
    r = technical_receipt(copy.deepcopy(binding))
    authority = copy.deepcopy(binding["closure"]["authority"])
    monkeypatch.setattr(review, "_bindings", lambda *_a, **_kw: copy.deepcopy(binding))
    if field in ["new_upload_authorized", "fresh_human_full_playback_claimed"]:
        r[field] = True
    elif field == "reviewed_by":
        r[field] = "维护者"
    elif field == "candidate_id":
        r[field] = "different"
    elif field == "bindings":
        r[field]["package_audit_sha256"] = "c" * 64
    elif field == "naive_time":
        r["reviewed_at"] = "2026-09-09T00:00:00"
    else:
        authority["bvid"] = "BVdifferent"
    with pytest.raises(owner.OriginalPackageError):
        review.validate_technical_receipt(
            r, tmp_path, tmp_path / "audit.json", publication_authority=authority
        )


def test_preserved_original_media_is_verified_without_fabricated_recut_schema(
    tmp_path, monkeypatch
):
    def save(name, value):
        p = tmp_path / name
        p.write_bytes(value if isinstance(value, bytes) else json.dumps(value).encode())
        return p

    main = save("source.mp4", b"unchanged-main")
    oldvideo = save("old.mp4", b"prior-published")
    original = save("original.srt", b"original-reviewed")
    intro = {"intro_id": "same-intro", "intro_media_sha256": "a" * 64, "intro_offset_ms": 6184}
    before = {
        "start_ms": 0,
        "end_ms": 1000,
        "duration_ms": 1000,
        "artifact_hashes": {
            "video_sha256": "sha256:" + owner.sha_file(main),
            "burned_video_sha256": "sha256:" + owner.sha_file(oldvideo),
        },
        "burned_preview": {"branding_intro": intro},
    }
    oldrecord = save("old.record.json", before)
    intent = {
        "candidate_id": "synthetic",
        "raw_main_sha256": owner.sha_file(main),
        "corrected_srt_sha256": "b" * 64,
        "frozen_published_inputs": {
            "/prior/old.record.json": owner.sha_file(oldrecord),
            "/prior/old.mp4": owner.sha_file(oldvideo),
        },
        "branding_authority": {
            "record_path": "/prior/old.record.json",
            "burned_video_sha256": "sha256:" + owner.sha_file(oldvideo),
            "branding_intro": intro,
        },
    }
    save("intent.json", intent)
    contract = {
        "source_interval_ms": [0, 1000],
        "source_main_sha256": owner.sha_file(main),
        "audio_pcm_sha256": "SHA256=" + "c" * 64,
        "evidence": {
            "old_record": "old.record.json",
            "source_main": "source.mp4",
            "render_intent": "intent.json",
            "original_review_input": "original.srt",
            "old_video": "old.mp4",
        },
    }
    replay = (
        b"",
        {"original_review_input_sha256": owner.sha_file(original), "release_srt_sha256": "b" * 64},
    )
    monkeypatch.setattr(owner, "_pcm_hash", lambda p: "SHA256=" + "c" * 64)
    assert (
        owner.verify_original_continuity(
            tmp_path, contract, {"candidate_id": "synthetic"}, before, {}, replay
        )
        == before
    )
    # A merely matching model report cannot substitute different underlying media.
    main.write_bytes(b"other-main")
    with pytest.raises(owner.OriginalPackageError, match="source main"):
        owner.verify_original_continuity(
            tmp_path, contract, {"candidate_id": "synthetic"}, before, {}, replay
        )


def test_original_technical_lane_uses_same_minimal_boundary_events():
    from src.autoslice.jingting_chunker import parse_srt_cues

    cues = parse_srt_cues(
        "1\n00:00:00,250 --> 00:00:01,730\n好久不见小\n\n2\n00:00:01,730 --> 00:00:03,750\n李，总觉得上次见面后还没有分开过\n"
    )

    def event(start, end, text):
        return ["Dialogue: 0", start, end, "Default", "", "0", "0", "0", "", text]

    good = [
        event("0:00:00.25", "0:00:01.73", "好久不见小李，"),
        event("0:00:01.73", "0:00:03.75", "总觉得上次见面后还没有分开过"),
    ]
    owner.verify_original_display_events(cues, good)
    wrong = copy.deepcopy(good)
    wrong[0][-1] = wrong[0][-1].replace("小李", r"小\N李")
    with pytest.raises(owner.OriginalPackageError):
        owner.verify_original_display_events(cues, wrong)
    late = copy.deepcopy(good)
    late[0][1] = "0:00:00.75"
    with pytest.raises(owner.OriginalPackageError):
        owner.verify_original_display_events(cues, late)
    merged = [event("0:00:00.25", "0:00:03.75", "".join(c.text for c in cues))]
    with pytest.raises(owner.OriginalPackageError):
        owner.verify_original_display_events(cues, merged)
