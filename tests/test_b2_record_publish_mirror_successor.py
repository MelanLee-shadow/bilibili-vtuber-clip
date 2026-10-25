from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path

import pytest

from src.autoslice.b2_record_publish_mirror_successor import (
    B2RecordPublishMirrorSuccessorError,
    CANDIDATE_ID,
    plan_successor,
    prepare_successor,
)
from src.autoslice.package_publish_mirror import validate_publish_staging_mirror


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_bytes(raw)
    os.chmod(path, 0o600)


def _json(path: Path, value: object) -> None:
    _write(
        path,
        (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(),
    )


def _fixture(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    root = tmp_path / "source"
    root.mkdir(mode=0o700)
    n = {
        "record": f"{CANDIDATE_ID}.record.json",
        "publish": f"{CANDIDATE_ID}.publish.json",
        "chat": f"{CANDIDATE_ID}.chat-authority.json",
        "clip": f"{CANDIDATE_ID}.clip-context.json",
        "video": f"{CANDIDATE_ID}.mp4",
        "srt": f"{CANDIDATE_ID}.srt",
        "ass": f"{CANDIDATE_ID}.ass",
        "cover": f"{CANDIDATE_ID}.cover.png",
        "pre": f"{CANDIDATE_ID}.pre.png",
        "mask": f"{CANDIDATE_ID}.mask.png",
        "builtin": f"{CANDIDATE_ID}.builtin.json",
        "ref": f"evidence/{CANDIDATE_ID}.ref.png",
        "witness": f"{CANDIDATE_ID}.subtitle-audio-witness.srt",
        "provenance": f"{CANDIDATE_ID}.subtitle-audio-provenance.json",
        "correspondence": f"{CANDIDATE_ID}.subtitle-audio-correspondence.json",
        "raw": f"{CANDIDATE_ID}.subtitle-audio-bcut.raw.json",
    }
    for key, name in n.items():
        if key not in {"record", "publish", "chat", "clip"}:
            _write(root / name, f"fixture-{key}".encode())
    authority = {"candidate_id": CANDIDATE_ID, "bvid": "BVfixture"}
    generation = {
        "final_cover": str((root / n["cover"]).resolve()),
        "reference_image": str((root / n["ref"]).resolve()),
        "pre_overlay_path": str((root / n["pre"]).resolve()),
        "ai_background": str((root / n["pre"]).resolve()),
        "builtin_imagegen_provenance_path": str((root / n["builtin"]).resolve()),
        "rendered_text_pixels": {
            "mask_path": str((root / n["mask"]).resolve()),
            "pre_overlay_path": str((root / n["pre"]).resolve()),
        },
    }
    hashes = {
        "video_sha256": "sha256:" + _sha(root / n["video"]),
        "delivery_subtitle_sha256": "sha256:" + _sha(root / n["srt"]),
        "chat_authority_audit_sha256": "sha256:" + "0" * 64,
    }
    publish = {
        "schema_version": "fixture-publish.v1",
        "candidate_id": CANDIDATE_ID,
        "title": "【李豆沙】fixture",
        "video_path": str((root / n["video"]).resolve()),
        "cover_path": str((root / n["cover"]).resolve()),
        "cover_generation": deepcopy(generation),
        "recovery_publication_authority": deepcopy(authority),
        "artifact_hashes": {
            "video_sha256": "sha256:" + "1" * 64,
            "chat_authority_audit_sha256": "sha256:" + "2" * 64,
            "publish_draft_sha256": "sha256:" + "3" * 64,
        },
    }
    staging = {
        key: deepcopy(value)
        for key, value in publish.items()
        if key not in {"artifact_hashes", "candidate_id", "schema_version", "video_path"}
    }
    staging.update(
        publish_json_path=str((root / n["publish"]).resolve()),
        status="STAGED",
        video_path=str((root / n["video"]).resolve()),
        subtitle_path=str((root / n["srt"]).resolve()),
    )
    frozen = {"status": "PASS", "frozen": True}
    record = {
        "candidate_id": CANDIDATE_ID,
        "publish_staging": staging,
        "cover_generation": deepcopy(generation),
        "artifact_hashes": {**hashes, "publish_draft_sha256": "sha256:" + "4" * 64},
        "story_contract": {"candidate_id": CANDIDATE_ID},
        "boundary_audit": frozen,
        "exact_final_review": frozen,
        "recovery_publication_authority": deepcopy(authority),
        "subtitle_audio_correspondence": {
            "witness_srt_path": str((root / n["witness"]).resolve()),
            "provenance_path": str((root / n["provenance"]).resolve()),
            "correspondence_path": str((root / n["correspondence"]).resolve()),
            "raw_result_path": str((root / n["raw"]).resolve()),
            "output_dir": str(root),
            "receipt": frozen,
        },
        "media_path": str((root / n["video"]).resolve()),
        "subtitle_path": str((root / n["srt"]).resolve()),
        "subtitle_ass_path": str((root / n["ass"]).resolve()),
        "chat_authority_audit_path": str((root / n["chat"]).resolve()),
        "clip_context_path": str((root / n["clip"]).resolve()),
        "burned_preview": {
            "path": str((root / n["video"]).resolve()),
            "ass_path": str((root / n["ass"]).resolve()),
        },
    }
    _json(root / n["record"], record)
    _json(root / n["publish"], publish)
    _json(root / n["chat"], {"candidate_id": CANDIDATE_ID, "final_review_audit": frozen})
    _json(root / n["clip"], {"candidate_id": CANDIDATE_ID})
    _json(root / f"{CANDIDATE_ID}.title-cover-joint-qc.json", {"pass": True})
    _json(root / "package_audit.json", {"passed": True})
    _json(root / "verification/old-state.json", {"status": "OLD"})
    _json(
        root / "review_manifest.json",
        {
            "schema_version": "lidousha-review-package.v1",
            "status": "finished_review_package_no_upload_pending_human_review",
            "upload_allowed": False,
            "exact_candidate_ids": [CANDIDATE_ID],
            "items": [
                {
                    "candidate_id": CANDIDATE_ID,
                    "title": publish["title"],
                    "record": n["record"],
                    "publish_json": n["publish"],
                    "video": n["video"],
                    "mp4": n["video"],
                    "subtitle_srt": n["srt"],
                    "speaker_srt": n["srt"],
                    "ass_path": n["ass"],
                    "cover": n["cover"],
                    "chat_authority": n["chat"],
                    "clip_context": n["clip"],
                    "cover_pre_overlay": n["pre"],
                    "cover_title_mask": n["mask"],
                    "cover_builtin_provenance": n["builtin"],
                    "host_only_v4_binding": {"reference_path": n["ref"]},
                    "recovery_publication_authority": authority,
                }
            ],
        },
    )
    return root, n


def _projector(**kw):
    paths = kw["paths"]
    record, publish, chat = kw["record"], kw["publish"], kw["chat"]
    record.update(
        media_path=str(paths["final_video"]),
        subtitle_path=str(paths["plain_srt"]),
        subtitle_ass_path=str(paths["ass"]),
        chat_authority_audit_path=str(paths["chat"]),
        clip_context_path=str(paths["clip"]),
    )
    record["burned_preview"].update(path=str(paths["final_video"]), ass_path=str(paths["ass"]))
    record["publish_staging"].update(
        cover_path=str(paths["cover"]),
        publish_json_path=str(paths["publish"]),
    )
    record["subtitle_audio_correspondence"].update(
        output_dir=str(kw["output"]),
        witness_srt_path=str(paths["audio_witness"]),
        provenance_path=str(paths["audio_provenance"]),
        correspondence_path=str(paths["audio_correspondence"]),
        raw_result_path=str(paths["audio_raw"]),
    )
    publish.update(video_path=str(paths["final_video"]), cover_path=str(paths["cover"]))
    for generation in (
        publish["cover_generation"],
        record["cover_generation"],
        record["publish_staging"]["cover_generation"],
    ):
        generation.update(
            final_cover=str(paths["cover"]),
            reference_image=str(paths["reference"]),
            pre_overlay_path=str(paths["ai_background"]),
            ai_background=str(paths["ai_background"]),
            builtin_imagegen_provenance_path=str(paths["builtin_provenance"]),
        )
        generation["rendered_text_pixels"].update(
            mask_path=str(paths["title_mask"]),
            pre_overlay_path=str(paths["ai_background"]),
        )
    return record, publish, chat


def _qc_creator(**kw):
    path = kw["output_path"]
    cover = kw["destination_package_root"] / f"{CANDIDATE_ID}.cover.png"
    value = {
        "schema_version": "lidousha-title-cover-joint-qc.v1",
        "candidate_id": CANDIDATE_ID,
        "title": kw["title"],
        "status": "PASS",
        "pass": True,
        "cover_path": str(cover.resolve()),
        "cover_sha256": "sha256:" + _sha(cover),
        "locator_successor": {"provider_calls": 0},
    }
    _json(path, value)
    return value


def _final_media_binder(**_kwargs):
    return {
        "state_status": "BLOCKED_INPUT",
        "content_review_status": "UNASSESSED",
        "reason_codes": ["FINAL_MEDIA_REVIEW_AUDIO_TRANSPORT_MISSING"],
        "provider_calls": 0,
    }


def _file_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): _sha(path)
        for path in root.rglob("*")
        if path.is_file()
    }


def test_plan_is_read_only(tmp_path: Path) -> None:
    source, _ = _fixture(tmp_path)
    parent = tmp_path / "run"
    parent.mkdir(mode=0o700)
    before = _file_hashes(source)
    plan = plan_successor(source, parent / "successor")
    assert plan["status"] == "WOULD_CREATE_PRIVATE_SUCCESSOR"
    assert plan["provider_calls"] == plan["package_audit_calls"] == 0
    assert _file_hashes(source) == before


def test_prepare_repairs_mirror_and_archives_gates(tmp_path: Path) -> None:
    source, names = _fixture(tmp_path)
    parent = tmp_path / "run"
    parent.mkdir(mode=0o700)
    output = parent / "successor"
    before = _file_hashes(source)
    result = prepare_successor(
        source,
        output,
        projector=_projector,
        qc_creator=_qc_creator,
        final_media_binder=_final_media_binder,
    )
    assert result["status"].endswith("AUDIT_AND_MECHANICAL_PENDING")
    assert result["provider_calls"] == 0
    record = json.loads((output / names["record"]).read_text())
    publish = json.loads((output / names["publish"]).read_text())
    validate_publish_staging_mirror(record, publish)
    assert {k: v for k, v in record["artifact_hashes"].items() if k != "publish_draft_sha256"} == publish["artifact_hashes"]
    assert record["artifact_hashes"]["publish_draft_sha256"] == "sha256:" + _sha(output / names["publish"])
    history = output / "history/e570-record-publish-mirror-source"
    assert (history / names["record"]).is_file()
    assert (history / "package_audit.json").is_file()
    assert (history / "verification/old-state.json").is_file()
    assert not (output / "package_audit.json").exists()
    assert (output / "verification/b2-record-publish-mirror-successor.json").is_file()
    assert _file_hashes(source) == before


def test_create_only_and_semantic_guard(tmp_path: Path) -> None:
    source, _ = _fixture(tmp_path)
    parent = tmp_path / "run"
    parent.mkdir(mode=0o700)
    occupied = parent / "occupied"
    occupied.mkdir(mode=0o700)
    with pytest.raises(B2RecordPublishMirrorSuccessorError, match="CREATE_ONLY"):
        prepare_successor(
            source,
            occupied,
            projector=_projector,
            qc_creator=_qc_creator,
            final_media_binder=_final_media_binder,
        )

    def bad(**kw):
        record, publish, chat = _projector(**kw)
        record["story_contract"] = {"changed": True}
        return record, publish, chat

    with pytest.raises(B2RecordPublishMirrorSuccessorError, match="SEMANTIC_MUTATION"):
        prepare_successor(
            source,
            parent / "bad",
            projector=bad,
            qc_creator=_qc_creator,
            final_media_binder=_final_media_binder,
        )
