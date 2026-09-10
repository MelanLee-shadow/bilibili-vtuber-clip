"""Portable synthetic cover fixtures shared by private and public regressions."""
from __future__ import annotations
import hashlib
import json
import scripts.session_autoslice as runner
from src.autoslice.cover_generation import LidoushaCoverArtDirection, _overlay_cover_title
from src.autoslice.cover_route_evidence import build_cover_route_decision, record_cover_route_execution
from src.autoslice.cover_polish_gate import POLISH_FACE_AUTHORITY, POLISH_FACE_SCHEMA_VERSION
from src.autoslice.cover_host_identity_gate import (
    AUTHORITY as HOST_IDENTITY_AUTHORITY, SCHEMA_VERSION as HOST_IDENTITY_SCHEMA_VERSION,
)

def _cover_binding_fixture(tmp_path, monkeypatch, *, song=False):
    monkeypatch.setattr(runner, "BASE", tmp_path / "autoslice")
    monkeypatch.setattr(runner, "REPO_ROOT", tmp_path / "repo")
    date = "2026-07-10"
    cid = "song_outer" if song else "auto_1"
    source_cid = "seededsong_100_200" if song else cid
    title = "【李豆沙】标题"
    delivery = runner.REPO_ROOT / "lidousha" / date
    delivery.mkdir(parents=True)
    mp4 = delivery / "钩子.mp4"
    cover = delivery / "钩子.cover.png"
    mp4.write_bytes(b"video")
    cover.write_bytes(b"stale-cover")
    generation_root = runner.BASE / "out" / date / cid / "cover_repair" / "generations" / "attempt-1"
    generation_root.mkdir(parents=True)
    generated_cover = generation_root / "final.cover.png"
    generated_cover.write_bytes(b"new-cover")
    evidence = generation_root / "evidence"
    evidence.mkdir()
    ai_bg = evidence / "ai.png"
    pre_overlay = evidence / "pre-overlay.png"
    title_mask = evidence / "title-mask.png"
    reference = evidence / "ref.png"
    request = evidence / "request.json"
    response = evidence / "response.json"
    for path, value in (
        (ai_bg, b"ai"),
        (pre_overlay, b"pre-overlay"),
        (title_mask, b"title-mask"),
        (reference, b"ref"),
        (request, b"request"),
        (response, b"response"),
    ):
        path.write_bytes(value)

    def digest(path):
        return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()

    generation_path = generated_cover.with_suffix(".cover_generation.json")
    generation = {
        "workflow": "regenerate_channel_cover",
        "status": "AI_COVER_READY",
        "method": "images.edit",
        "image_gen_model": "cpa",
        "model": "gpt-image-1.5",
        "fallback_used": False,
        "model_fallback_used": True,
        "attempted_models": ["gpt-image-2", "gpt-image-1.5"],
        "candidate_id": cid,
        # 歌切标记的权威位置是 art_direction 子字典（生产上 regenerate 工作流
        # 和 publish_staging 都只写这里，顶层从来没有 is_song）。夹具以前只写
        # 顶层，跟真包对不上，把 cover_repair 的键位错配整个盖住了。
        "art_direction": {
            "role": "host",
            "expression_en": "gentle singing face",
            "background_style": "calm",
            "layout": "song-clean" if song else "left-split",
            "hook_color": "cream",
            "hook_word": "",
            "is_song": song,
            "emote_id": "",
            "emote_mode": "",
            "emote_reason": "",
            "cover_punch": [],
            "cover_punch_semantic_review": {},
            "scene_props": [],
        },
        "title": title,
        "cover_text": "修复后的封面文案",
        "final_cover": str(generated_cover),
        "final_cover_sha256": digest(generated_cover),
        "ai_background": str(ai_bg),
        "ai_background_sha256": digest(ai_bg),
        "pre_overlay_path": str(pre_overlay),
        "pre_overlay_sha256": digest(pre_overlay),
        "rendered_text_pixels": {
            "mask_path": str(title_mask),
            "mask_sha256": digest(title_mask),
        },
        "reference_image": str(reference),
        "reference_sha256": digest(reference),
        "request_path": str(request),
        "request_sha256": digest(request),
        "response_path": str(response),
        "response_sha256": digest(response),
        "final_host_identity_verification": {
            "schema_version": "lidousha-cover-final-host-identity-verification.v3",
            "authority": "CPA_PRIMARY_HASH_BOUND_SOURCE_FINAL_IDENTITY_AND_PROMINENCE_COMPARISON",
            "status": "PASS",
            "final_cover_sha256": digest(generated_cover),
            "witness": {
                "provider": "cpa", "image_sha256": digest(reference).removeprefix("sha256:"),
                "routing": {"preferred_provider": "cpa", "fallback_used": False},
            },
            "comparison_sha256": digest(reference),
        },
    }
    generation_path.write_text(json.dumps(generation), encoding="utf-8")
    artifact_root = runner.BASE / "out" / date / cid
    if song:
        artifact_root = artifact_root / "song_selector_full" / "attempt-1"
    artifact_root = artifact_root / "replacement_recuts"
    artifact_root.mkdir(parents=True)
    publish_path = artifact_root / f"{source_cid}.recut.publish.json"
    video_sha = digest(mp4)
    record_payload = {
        **(
            {"delivery_candidate_id": cid, "source_candidate_id": source_cid}
            if song
            else {}
        ),
        "artifact_hashes": {"burned_video_sha256": video_sha},
        "publish_staging": {
            "title": title,
            "cover_text": "修复前的旧封面文案",
            "cover_status": "BLOCKED_AI_COVER_REQUIRED",
            "reason_codes": ["CPA_AI_COVER_REQUIRED"],
            "publish_json_path": str(publish_path),
            "upload_enabled": False,
        },
    }
    delivery_record = mp4.with_suffix(".record.json")
    delivery_record.write_text(json.dumps(record_payload), encoding="utf-8")
    source_record = artifact_root / f"{source_cid}.record.json"
    source_record.write_text(json.dumps(record_payload), encoding="utf-8")
    publish_path.write_text(
        json.dumps(
            {
                "schema_version": "shadow-publish-draft.v1",
                "candidate_id": source_cid,
                "title": title,
                "cover_text": "修复前的旧封面文案",
                "artifact_hashes": {"burned_video_sha256": video_sha},
                "cover_status": "BLOCKED_AI_COVER_REQUIRED",
                "reason_codes": ["CPA_IMAGE_EDIT_HTTP_ERROR"],
                "upload_enabled": False,
            }
        ),
        encoding="utf-8",
    )
    rec = {
        "candidate_id": cid,
        "title": title,
        "status": "review_ready",
        "cover_status": "BLOCKED_AI_COVER_REQUIRED",
        "summary": {},
    }
    delivery_manifest = None
    if song:
        delivery_manifest = mp4.with_suffix(".delivery.manifest.json")
        manifest_payload = {
            "schema_version": runner.VERIFIED_SONG_DELIVERY_SCHEMA_VERSION,
            "status": "DELIVERED_NO_UPLOAD",
            "candidate_id": cid,
            "upload_enabled": False,
            "artifacts": {
                "video": {"path": str(mp4), "sha256": video_sha},
                "active_record": {
                    "path": str(delivery_record),
                    "sha256": digest(delivery_record),
                    "source_path": str(source_record),
                    "source_sha256": digest(source_record),
                },
            },
            "absent_artifacts": {"cover": {"path": str(cover), "status": "ABSENT"}},
        }
        delivery_manifest.write_text(json.dumps(manifest_payload), encoding="utf-8")
        rec.update(
            {
                "delivered": str(mp4),
                "delivered_sha256": video_sha,
                "video_sha256": video_sha,
                "delivered_sidecars": {"active_record": str(delivery_record)},
                "delivered_sidecar_hashes": {
                    "active_record": digest(delivery_record)
                },
                "delivery_manifest_path": str(delivery_manifest),
                "delivery_manifest_sha256": digest(delivery_manifest),
                "delivery_upload_enabled": False,
            }
        )
    return {
        "date": date,
        "cid": cid,
        "source_cid": source_cid,
        "title": title,
        "mp4": mp4,
        "cover": cover,
        "generated_cover": generated_cover,
        "generation_path": generation_path,
        "delivery_record": delivery_record,
        "source_record": source_record,
        "publish_path": publish_path,
        "delivery_manifest": delivery_manifest,
        "rec": rec,
        "digest": digest,
    }

def _screenshot_polish_binding_fixture(tmp_path, monkeypatch):
    """Make one real renderer-backed screenshot-polish binding fixture."""

    from PIL import Image

    fx = _cover_binding_fixture(tmp_path, monkeypatch)
    generated = fx["generated_cover"]
    poster = generated.with_name("screenshot-poster.png")
    polished = generated.with_name("screenshot-polished.png")
    reference = generated.with_name("screenshot-reference.png")
    Image.new("RGB", (1920, 1080), (210, 120, 120)).save(poster)
    Image.new("RGB", (1920, 1080), (190, 100, 130)).save(polished)
    Image.new("RGB", (1920, 1080), (80, 110, 160)).save(reference)
    direction = LidoushaCoverArtDirection(
        role="host",
        expression_en="soft smile",
        background_style="calm",
        layout="banner",
        hook_color="purple",
        is_song=False,
        cover_punch=("修复后的封面文案",),
    )
    overlay = _overlay_cover_title(
        poster,
        generated,
        cover_text="修复后的封面文案",
        art_direction=direction,
    )
    digest = fx["digest"]
    host_verification = {
        "schema_version": HOST_IDENTITY_SCHEMA_VERSION,
        "authority": HOST_IDENTITY_AUTHORITY,
        "status": "PASS",
        "final_cover_sha256": digest(generated),
        "comparison_sha256": digest(reference),
        "witness": {
            "provider": "cpa",
            "image_sha256": digest(reference).removeprefix("sha256:"),
            "routing": {"preferred_provider": "cpa", "fallback_used": False},
        },
    }
    generation = {
        "status": "AI_COVER_READY",
        "candidate_id": fx["cid"],
        "title": fx["title"],
        "method": "screenshot_polish",
        "model": "gpt-image-2",
        "image_gen_model": "gpt-image-2",
        "fallback_used": False,
        "cover_origin": "SOURCE_SCREENSHOT_AI_POLISH",
        "image_generation_used": True,
        "cover_text": "修复后的封面文案",
        "art_direction": direction.__dict__,
        "reference_image": str(reference),
        "reference_sha256": digest(reference),
        "screenshot_frame": {"frame_ms": 21_500},
        "reference_selection": {"best_ms": 21_500},
        "ai_background": str(poster),
        "ai_background_sha256": digest(poster),
        "final_cover": str(generated),
        "final_cover_sha256": digest(generated),
        "attempted_models": ["gpt-image-2"],
        "model_fallback_used": False,
        "screenshot_graphic_poster": {
            "source_frame_transform": {
                "input_sha256": digest(polished),
            },
        },
        "polish_face_verification": {
            "schema_version": POLISH_FACE_SCHEMA_VERSION,
            "authority": POLISH_FACE_AUTHORITY,
            "status": "PASS",
            "witness": {
                "provider": "cpa",
                "image_sha256": digest(generated).removeprefix("sha256:"),
                "routing": {"preferred_provider": "cpa", "fallback_used": False},
            },
        },
        "final_host_identity_verification": host_verification,
        **overlay,
    }
    generation["route_decision"] = build_cover_route_decision(
        selected_treatment="screenshot_polish",
        selected_rationale="existing polished screenshot bytes are still available",
        story_contract=None,
        reference_authority=None,
        title=fx["title"],
        cover_text="修复后的封面文案",
        decision_inputs={
            "cover_mode": "screenshot",
            "subject_confident": True,
            "verified_stream_frame": True,
        },
    )
    generation["route_decision"]["host_identity_required"] = True
    record_cover_route_execution(
        generation,
        actual_treatment="screenshot_polish",
        execution_status="READY",
        image_generation_attempted=True,
        image_generation_used=True,
    )
    fx["generation_path"].write_text(json.dumps(generation), encoding="utf-8")
    for record_path in (fx["delivery_record"], fx["source_record"]):
        record = json.loads(record_path.read_text(encoding="utf-8"))
        record["artifact_hashes"]["cover_sha256"] = digest(generated)
        record["publish_staging"].update(
            {
                "cover_status": "AI_COVER_READY",
                "cover_path": str(generated),
                "cover_generation": generation,
            }
        )
        record_path.write_text(json.dumps(record), encoding="utf-8")
    publish = json.loads(fx["publish_path"].read_text(encoding="utf-8"))
    publish["artifact_hashes"]["cover_sha256"] = digest(generated)
    publish.update(
        {
            "cover_status": "AI_COVER_READY",
            "cover_path": str(generated),
            "cover_generation": generation,
        }
    )
    fx["publish_path"].write_text(json.dumps(publish), encoding="utf-8")
    return fx
