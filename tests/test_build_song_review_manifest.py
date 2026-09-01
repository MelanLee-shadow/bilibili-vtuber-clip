from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

import scripts.build_song_review_manifest as builder
from src.autoslice.surface_canon import CHANNEL_PROFILE


CANDIDATE_ID = "song_192000_1321"
SOURCE_CANDIDATE_ID = "seededsong_120000_421470"
TITLE = CHANNEL_PROFILE.song_plain_template.format(song_title="海海海")
BASENAME = f"歌切_{TITLE}__{CANDIDATE_ID}"


def _sha(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _fixture(tmp_path: Path) -> dict:
    delivery_root = tmp_path / "channel" / "2026-07-25"
    source_root = tmp_path / "source"
    delivery_root.mkdir(parents=True)
    source_root.mkdir()

    artifact_paths = {
        role: delivery_root / f"{BASENAME}{suffix}"
        for role, suffix in builder.REQUIRED_ROLES.items()
    }
    source_paths = {
        role: source_root / f"{role}{suffix}"
        for role, suffix in builder.REQUIRED_ROLES.items()
    }

    raw_roles = {
        "video": b"video-bytes",
        "subtitle": (
            "1\n00:00:00,000 --> 00:00:02,000\n不能停止我对你的爱\n"
        ).encode(),
        # 两份歌切证人按生产形状写真 JSON：跨主机导入器会按 schema/decision
        # 逐条重验它们，占位字节会让"证据链同等严格"这条断言失去意义。
        "lyrics_alignment_report": json.dumps(
            {"schema_version": "lyrics-alignment-report.v1"}, sort_keys=True
        ).encode(),
        "host_vocal_proof": json.dumps(
            {
                "schema_version": "host-vocal-proof.v3",
                "status": "READY",
                "decision": "LIDOUSHA_VOCAL_PRESENT_ON_LYRIC_CHECKPOINTS",
            },
            sort_keys=True,
        ).encode(),
        "cover": b"cover",
        "cover_title_mask": b"mask",
        "cover_pre_overlay": b"pre-overlay",
        "cover_route_background": b"route-background",
    }
    for role, payload in raw_roles.items():
        artifact_paths[role].write_bytes(payload)
        source_paths[role].write_bytes(payload)

    generation = {
        "final_cover_sha256": _sha(artifact_paths["cover"]),
        "pre_overlay_sha256": _sha(artifact_paths["cover_pre_overlay"]),
        "ai_background_sha256": _sha(
            artifact_paths["cover_route_background"]
        ),
        "rendered_text_pixels": {
            "mask_sha256": _sha(artifact_paths["cover_title_mask"]),
            "pre_overlay_sha256": _sha(
                artifact_paths["cover_pre_overlay"]
            ),
            "final_cover_sha256": _sha(artifact_paths["cover"]),
        },
        "route_decision": {"schema_version": "test-route"},
        "method": "images.edit",
        "reference_sha256": "sha256:" + "a" * 64,
        "reference_authority": {"schema_version": "test-reference"},
    }
    hashes = {
        "burned_video_sha256": _sha(artifact_paths["video"]),
        "subtitle_sha256": _sha(artifact_paths["subtitle"]),
        "cover_sha256": _sha(artifact_paths["cover"]),
    }
    record = {
        "delivery_candidate_id": CANDIDATE_ID,
        "source_candidate_id": SOURCE_CANDIDATE_ID,
        "artifact_hashes": hashes,
        "publish_staging": {
            "status": "STAGED",
            "title": TITLE,
            "upload_enabled": False,
            "cover_generation": generation,
        },
    }
    publish = {
        "schema_version": "shadow-publish-draft.v1",
        "candidate_id": SOURCE_CANDIDATE_ID,
        "title": TITLE,
        "upload_enabled": False,
        "artifact_hashes": hashes,
        "cover_generation": generation,
    }
    _write_json(artifact_paths["active_record"], record)
    _write_json(source_paths["active_record"], record)
    _write_json(artifact_paths["publish"], publish)
    _write_json(source_paths["publish"], publish)

    completion = {
        "ready": True,
        "reason_codes": [],
        "song_boundary_status": "FULL_SONG_READY",
        "lyrics_alignment_status": "READY",
        "host_vocal_status": "READY",
        "live_performance_status": "READY",
        "live_performance_mode": "LIVE_STREAMER_SINGING",
        "joint_singing_decision": "VERIFIED_LIDOUSHA_SINGING",
        "subtitle_source": "external_lrc_global_shift",
        "burned_preview_path": str(source_paths["video"]),
        "burned_preview_sha256": _sha(source_paths["video"]),
        "alignment_report_path": str(
            source_paths["lyrics_alignment_report"]
        ),
        "alignment_report_sha256": _sha(
            source_paths["lyrics_alignment_report"]
        ),
        "host_vocal_proof_path": str(source_paths["host_vocal_proof"]),
        "host_vocal_proof_sha256": _sha(
            source_paths["host_vocal_proof"]
        ),
        "recut_manifest_path": str(source_paths["recut_manifest"]),
        "recut_manifest_sha256": "",
    }
    recut_manifest = {
        "schema_version": "materialized-recut.v2",
        "status": "MATERIALIZED",
        "subtitle_source": "external_lrc_global_shift",
        "reason_codes": [],
        "verified_output_binding": {
            "schema_version": "verified-song-output-binding.v1",
            "artifacts": {
                "burned_media_sha256": _sha(artifact_paths["video"]),
                "subtitle_sha256": _sha(artifact_paths["subtitle"]),
            },
            "proofs": {
                "lyrics_alignment_report_sha256": _sha(
                    artifact_paths["lyrics_alignment_report"]
                ),
                "host_vocal_proof_sha256": _sha(
                    artifact_paths["host_vocal_proof"]
                ),
            },
        },
    }
    _write_json(artifact_paths["recut_manifest"], recut_manifest)
    _write_json(source_paths["recut_manifest"], recut_manifest)
    completion["recut_manifest_sha256"] = _sha(
        source_paths["recut_manifest"]
    )

    summary = {"records": [{"candidate_id": SOURCE_CANDIDATE_ID}]}
    summary_path = source_root / "summary.json"
    _write_json(summary_path, summary)

    manifest_artifacts = {}
    for role in builder.REQUIRED_ROLES:
        manifest_artifacts[role] = {
            "path": str(artifact_paths[role]),
            "sha256": _sha(artifact_paths[role]),
            "source_path": str(source_paths[role]),
            "source_sha256": _sha(source_paths[role]),
        }
    delivery_manifest_path = (
        delivery_root / f"{BASENAME}.delivery.manifest.json"
    )
    delivery_manifest = {
        "schema_version": "verified-song-delivery.v1",
        "status": "DELIVERED_NO_UPLOAD",
        "candidate_id": CANDIDATE_ID,
        "upload_enabled": False,
        "artifacts": manifest_artifacts,
        "absent_artifacts": {},
    }
    _write_json(delivery_manifest_path, delivery_manifest)
    newest_ns = max(
        path.stat().st_mtime_ns for path in artifact_paths.values()
    )
    os.utime(
        delivery_manifest_path,
        ns=(newest_ns + 10_000_000, newest_ns + 10_000_000),
    )

    delivered_sidecars = {
        role: str(path)
        for role, path in artifact_paths.items()
        if role != "video"
    }
    delivered_hashes = {
        role: _sha(path)
        for role, path in artifact_paths.items()
        if role != "video"
    }
    state_row = {
        "candidate_id": CANDIDATE_ID,
        "status": "review_ready",
        "rc": 0,
        "title": TITLE,
        "delivery_upload_enabled": False,
        "delivery_manifest_path": str(delivery_manifest_path),
        "delivery_manifest_sha256": _sha(delivery_manifest_path),
        "delivered": str(artifact_paths["video"]),
        "delivered_sha256": _sha(artifact_paths["video"]),
        "delivered_sidecars": delivered_sidecars,
        "delivered_sidecar_hashes": delivered_hashes,
        "selector_summary_path": str(summary_path),
        "selector_summary_sha256": _sha(summary_path),
        "selector_record_candidate_id": SOURCE_CANDIDATE_ID,
        "song_completion_evidence": completion,
    }
    state_path = tmp_path / "state" / "2026-07-25.json"
    _write_json(
        state_path,
        {"status": "review_ready", "songs": [state_row]},
    )
    deployed = tmp_path / "DEPLOYED_COMMIT"
    deployed.write_text("1" * 40 + "\n", encoding="utf-8")
    return {
        "delivery_manifest": delivery_manifest_path,
        "state": state_path,
        "deployed": deployed,
        "package": tmp_path / "portable",
        "completion": completion,
        "artifact_paths": artifact_paths,
        "source_paths": source_paths,
    }


def _build(
    fx: dict,
    monkeypatch: pytest.MonkeyPatch,
    **overrides,
) -> dict:
    monkeypatch.setattr(
        builder, "validate_cover_route_decision", lambda *_a, **_k: True
    )
    monkeypatch.setattr(
        builder,
        "validate_rendered_text_pixel_evidence",
        lambda *_a, **_k: True,
    )
    return builder.build(
        fx["package"],
        delivery_manifest_path=fx["delivery_manifest"],
        state_path=fx["state"],
        candidate_id=CANDIDATE_ID,
        deployed_commit_file=fx["deployed"],
        completion_verifier=lambda _record: fx["completion"],
        **overrides,
    )


def test_builds_portable_no_upload_song_review_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fx = _fixture(tmp_path)

    manifest = _build(fx, monkeypatch)

    assert manifest["classification"] == "Song"
    assert manifest["story_contract_required"] is False
    assert manifest["upload_allowed"] is False
    assert manifest["items"][0]["classification"] == "Song"
    assert manifest["items"][0]["title"] == TITLE
    assert set(manifest["items"][0]["sha256"]) == set(
        builder.REQUIRED_ROLES
    )
    for suffix in builder.REQUIRED_ROLES.values():
        assert (fx["package"] / f"{BASENAME}{suffix}").is_file()
    assert (
        fx["package"] / f"{BASENAME}.delivery.manifest.json"
    ).is_file()
    assert (fx["package"] / "review_manifest.json").is_file()


def _seal_talk_refresh_block(state: dict, *, recording_date: str) -> str:
    """Build the exact canonical Talk-only refresh envelope for this state."""

    talk_id = "auto_130040_201_255"
    state["status"] = "semantic_chat_scorecard_refresh_blocked"
    state["upload_allowed"] = False
    state["pending_talk"] = [
        {"candidate_id": talk_id, "lane": "semantic_recall"}
    ]
    state["operator_processing_scope"] = {
        "schema_version": "operator-processing-scope-grant.v2",
        "grant_id": "2026-08-14-维护者-uniform-host-rerender-20260819T0100Z",
        "recording_date": recording_date,
        "reason": "维护者 reviewed this named Talk repair and requires a no-upload rerun.",
        "candidate_ids": [talk_id],
        "user_authorization": {
            "quote": "重新做一下这些切片，但不要上传，先交给我审阅。",
            "timestamp": "2026-08-19T00:05:00Z",
        },
        "expires_at": "2099-08-22T00:00:00Z",
        "intent": "RECOVER_NAMED_FAILED_PICKS",
    }
    receipt = {
        "schema_version": "semantic-evidence-scorecard-refresh-run.v1",
        "recording_date": recording_date,
        "scope_grant_id": state["operator_processing_scope"]["grant_id"],
        "scope_grant_sha256": builder.semantic_refresh._canonical_sha256(
            state["operator_processing_scope"]
        ),
        "status": "BLOCKED",
        "refreshed_candidate_ids": [],
        "current_candidate_ids": [],
        "blocked_candidate_ids": [talk_id],
        "deferred_candidate_ids": [],
        "duplicate_candidate_ids": [],
        "missing_never_recalled_candidates_outside_scope": True,
    }
    receipt["receipt_sha256"] = builder.semantic_refresh._canonical_sha256(receipt)
    state["semantic_evidence_scorecard_refresh_run"] = receipt
    return talk_id


def _reseal_talk_refresh_block(state: dict) -> None:
    receipt = state["semantic_evidence_scorecard_refresh_run"]
    assert isinstance(receipt, dict)
    receipt["scope_grant_sha256"] = builder.semantic_refresh._canonical_sha256(
        state["operator_processing_scope"]
    )
    receipt["receipt_sha256"] = builder.semantic_refresh._canonical_sha256(
        {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    )


def test_build_allows_completed_song_package_when_only_talk_refresh_is_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """8/14 园游会 shape: Talk refresh remains blocked; Song stays no-upload."""
    fx = _fixture(tmp_path)
    state = json.loads(fx["state"].read_text(encoding="utf-8"))
    row = state["songs"][0]
    row["candidate_id"] = "songvis_130040_670_11874655"
    row["selector_record_candidate_id"] = "seededsong_130040_670_11874655"
    _seal_talk_refresh_block(state, recording_date="2026-07-25")
    fx["state"].write_text(json.dumps(state), encoding="utf-8")

    # This is deliberately a state-gate regression: the remaining builder
    # stages separately replay every portable source binding and final cover.
    assert builder._candidate_row(
        state, "songvis_130040_670_11874655", recording_date="2026-07-25"
    ) is row
    assert state["status"] == "semantic_chat_scorecard_refresh_blocked"
    assert state["upload_allowed"] is False


def test_build_refuses_talk_refresh_block_without_typed_talk_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fx = _fixture(tmp_path)
    state = json.loads(fx["state"].read_text(encoding="utf-8"))
    state.update(
        {
            "status": "semantic_chat_scorecard_refresh_blocked",
            "upload_allowed": False,
        }
    )
    fx["state"].write_text(json.dumps(state), encoding="utf-8")

    with pytest.raises(builder.SongReviewManifestError, match="not reviewable"):
        _build(fx, monkeypatch)


@pytest.mark.parametrize(
    "mutation",
    ("extra_key", "bad_hash", "wrong_date", "mixed_song", "expired", "malformed_grant"),
)
def test_build_refuses_noncanonical_or_non_talk_refresh_block(
    tmp_path: Path, mutation: str
) -> None:
    fx = _fixture(tmp_path)
    state = json.loads(fx["state"].read_text(encoding="utf-8"))
    song_id = str(state["songs"][0]["candidate_id"])
    talk_id = _seal_talk_refresh_block(state, recording_date="2026-07-25")
    receipt = state["semantic_evidence_scorecard_refresh_run"]
    scope = state["operator_processing_scope"]
    assert isinstance(receipt, dict) and isinstance(scope, dict)
    if mutation == "extra_key":
        receipt["unexpected"] = True
        _reseal_talk_refresh_block(state)
    elif mutation == "bad_hash":
        receipt["receipt_sha256"] = "sha256:" + "0" * 64
    elif mutation == "wrong_date":
        receipt["recording_date"] = "2026-07-26"
        _reseal_talk_refresh_block(state)
    elif mutation == "mixed_song":
        scope["candidate_ids"].append(song_id)
        state["pending_talk"].append(
            {"candidate_id": song_id, "lane": "semantic_recall"}
        )
        receipt["blocked_candidate_ids"].append(song_id)
        _reseal_talk_refresh_block(state)
    elif mutation == "expired":
        scope["expires_at"] = "2000-08-22T00:00:00Z"
        _reseal_talk_refresh_block(state)
    else:
        scope["unexpected"] = True
        _reseal_talk_refresh_block(state)
    assert talk_id != song_id

    with pytest.raises(builder.SongReviewManifestError, match="not reviewable"):
        builder._candidate_row(state, song_id, recording_date="2026-07-25")


def test_refuses_artifact_hash_drift_before_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fx = _fixture(tmp_path)
    fx["artifact_paths"]["subtitle"].write_text(
        "tampered", encoding="utf-8"
    )

    with pytest.raises(
        builder.SongReviewManifestError, match="subtitle artifact hash drift"
    ):
        _build(fx, monkeypatch)


def test_refuses_non_manifest_last_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fx = _fixture(tmp_path)
    manifest_ns = fx["delivery_manifest"].stat().st_mtime_ns
    video = fx["artifact_paths"]["video"]
    os.utime(video, ns=(manifest_ns + 10_000_000, manifest_ns + 10_000_000))

    with pytest.raises(
        builder.SongReviewManifestError, match="manifest-last authority"
    ):
        _build(fx, monkeypatch)


def test_refuses_fresh_song_proof_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fx = _fixture(tmp_path)
    monkeypatch.setattr(
        builder, "validate_cover_route_decision", lambda *_a, **_k: True
    )
    monkeypatch.setattr(
        builder,
        "validate_rendered_text_pixel_evidence",
        lambda *_a, **_k: True,
    )
    drifted = dict(fx["completion"])
    drifted["live_performance_mode"] = "ORIGINAL_OR_BACKGROUND_PLAYBACK"

    with pytest.raises(
        builder.SongReviewManifestError,
        match="fresh Song completion proof is not delivery-ready",
    ):
        builder.build(
            fx["package"],
            delivery_manifest_path=fx["delivery_manifest"],
            state_path=fx["state"],
            candidate_id=CANDIDATE_ID,
            deployed_commit_file=fx["deployed"],
            completion_verifier=lambda _record: drifted,
        )


def test_refreshes_upload_tags_across_source_delivery_state_and_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fx = _fixture(tmp_path)
    calls: list[tuple[str, Path, float]] = []
    generated = {
        "engine": "suggest-upload-tags.v1",
        "status": "OK",
        "final_tags": ["主播", "虚拟主播", "翻唱"],
        "final_tag_line": "主播,虚拟主播,翻唱",
        "proper_noun_tags": [],
        "content_tags": [{"tag": "翻唱", "why": "歌切"}],
        "warnings": [],
    }

    def tag_generator(
        title: str, subtitle: Path, *, timeout: float
    ) -> dict:
        calls.append((title, subtitle, timeout))
        return generated

    manifest = _build(
        fx,
        monkeypatch,
        refresh_upload_tags=True,
        tag_generator=tag_generator,
    )

    assert calls == [
        (TITLE, fx["artifact_paths"]["subtitle"], 180.0)
    ]
    source_record = json.loads(
        fx["source_paths"]["active_record"].read_text(encoding="utf-8")
    )
    delivery_record = json.loads(
        fx["artifact_paths"]["active_record"].read_text(encoding="utf-8")
    )
    assert source_record == delivery_record
    assert source_record["upload_tags"] == generated

    delivery = json.loads(
        fx["delivery_manifest"].read_text(encoding="utf-8")
    )
    active = delivery["artifacts"]["active_record"]
    assert active["sha256"] == _sha(fx["artifact_paths"]["active_record"])
    assert active["source_sha256"] == _sha(
        fx["source_paths"]["active_record"]
    )
    state = json.loads(fx["state"].read_text(encoding="utf-8"))
    state_row = state["songs"][0]
    assert (
        state_row["delivered_sidecar_hashes"]["active_record"]
        == active["sha256"]
    )
    assert state_row["delivery_manifest_sha256"] == _sha(
        fx["delivery_manifest"]
    )
    assert (
        manifest["delivery_authority"]["manifest_sha256"]
        == _sha(fx["delivery_manifest"])
    )
    portable_record = json.loads(
        (
            fx["package"] / f"{BASENAME}.record.json"
        ).read_text(encoding="utf-8")
    )
    assert portable_record["upload_tags"] == generated
    assert manifest["upload_allowed"] is False


def test_refresh_rejects_empty_tags_without_mutating_authorities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fx = _fixture(tmp_path)
    watched = [
        fx["source_paths"]["active_record"],
        fx["artifact_paths"]["active_record"],
        fx["delivery_manifest"],
        fx["state"],
    ]
    before = {path: path.read_bytes() for path in watched}

    with pytest.raises(
        builder.SongReviewManifestError,
        match="invalid or empty tags",
    ):
        _build(
            fx,
            monkeypatch,
            refresh_upload_tags=True,
            tag_generator=lambda *_args, **_kwargs: {
                "engine": "suggest-upload-tags.v1",
                "status": "FAILED",
                "final_tags": [],
                "final_tag_line": "",
            },
        )

    assert {path: path.read_bytes() for path in watched} == before
    assert not fx["package"].exists()


def test_refresh_rolls_back_all_authorities_on_commit_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fx = _fixture(tmp_path)
    watched = [
        fx["source_paths"]["active_record"],
        fx["artifact_paths"]["active_record"],
        fx["delivery_manifest"],
        fx["state"],
    ]
    before = {path: path.read_bytes() for path in watched}
    failed = False

    def fail_once(path: Path, body: bytes) -> None:
        nonlocal failed
        if path == fx["delivery_manifest"].resolve() and not failed:
            failed = True
            raise OSError("injected manifest write failure")
        builder._atomic_bytes(path, body)

    with pytest.raises(
        builder.SongReviewManifestError,
        match="was rolled back",
    ):
        _build(
            fx,
            monkeypatch,
            refresh_upload_tags=True,
            tag_generator=lambda *_args, **_kwargs: {
                "engine": "suggest-upload-tags.v1",
                "status": "OK_NO_LLM",
                "final_tags": ["主播"],
                "final_tag_line": "主播",
                "proper_noun_tags": [],
                "content_tags": [],
                "warnings": ["degraded"],
            },
            closure_writer=fail_once,
        )

    assert {path: path.read_bytes() for path in watched} == before
    assert not fx["package"].exists()
