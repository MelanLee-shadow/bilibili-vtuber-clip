from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts.build_lidousha_recovery_review_manifest import (
    ManifestBuildError,
    _sync_cover_title_replay_artifacts,
    build_manifest,
)
from src.autoslice.addressee_attribution import rebuild_speaker_evidence
from src.autoslice.cover_route_evidence import (
    build_cover_route_decision,
    record_cover_route_execution,
)
from src.autoslice.review_evidence import SourceCue
from src.autoslice.source_fact_review import review_and_repair_source_facts
from src.autoslice.speaker_common import SPEAKER_FINALIZATION_SCHEMA
from src.autoslice.recovery_title_authority import (
    ROOT,
    build_recovery_publication_authorities,
    expected_recovery_publish_title,
)


def _sha(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _keep_completion(hook: str, title: str) -> str:
    return json.dumps(
        {
            "schema_version": "lidousha-source-fact-review.v1",
            "status": "KEEP",
            "final_selection_hook": hook,
            "final_title": title,
            "supported_by": ["final_transcript", "speaker_transcript"],
            "changed_surfaces": [],
            "addressee_attribution": [
                {
                    "assertion": "弹幕让左边的人弹右边一个脑瓜崩",
                    "verdict": "SUPPORTED",
                    "reason": "人工说话人证据给出了可复核的在场发言。",
                    "evidence": ["speaker_transcript: 1 [李豆沙] hello"],
                }
            ],
            "selection_scorecard_review": {
                "status": "NOT_NEEDED",
                "reason": "selection hook remains unchanged",
            },
            "summary": "最终字幕与人工说话人证据支持现有派生事实。",
        },
        ensure_ascii=False,
    )


def test_recovery_builder_refreshes_current_cover_replay_artifacts(
    tmp_path: Path,
) -> None:
    package_root = tmp_path / "package"
    generation_root = tmp_path / "generation"
    package_root.mkdir()
    generation_root.mkdir()
    stem = "reviewed-cover"
    sources = {}
    for name, payload in (
        ("mask", b"current-mask"),
        ("pre", b"current-pre-overlay"),
        ("background", b"current-background"),
    ):
        source = generation_root / f"{name}.png"
        source.write_bytes(payload)
        sources[name] = source
    for suffix in (
        "cover.title-mask.png",
        "cover.pre-overlay.png",
        "cover.route-background.png",
    ):
        (package_root / f"{stem}.{suffix}").write_bytes(b"stale")

    projected = _sync_cover_title_replay_artifacts(
        package_root=package_root,
        stem=stem,
        generation={
            "pre_overlay_path": str(sources["pre"]),
            "pre_overlay_sha256": _sha(sources["pre"]),
            "ai_background": str(sources["background"]),
            "ai_background_sha256": _sha(sources["background"]),
            "rendered_text_pixels": {
                "mask_path": str(sources["mask"]),
                "mask_sha256": _sha(sources["mask"]),
            },
        },
    )

    assert [path.read_bytes() for path in projected] == [
        b"current-mask",
        b"current-pre-overlay",
        b"current-background",
    ]


def test_builder_reprojects_record_title_and_exact_cover_evidence(
    tmp_path: Path,
) -> None:
    root = tmp_path / "2026-07-22"
    root.mkdir()
    stem = "当面对质"
    candidate_id = "auto_193450_1475_1543"
    authority = build_recovery_publication_authorities(
        candidate_ids={candidate_id},
        registry_path=(ROOT / "assets/lidousha/recovery_publication_authority.v1.json"),
        expected_registry_sha256=(
            "sha256:0bbb26c63c30b1e30af13e33d5513c49aa10b98afa8730ee9761f59865317e30"
        ),
    )[candidate_id]
    title = expected_recovery_publish_title(authority)
    files = {}
    for suffix, payload in (
        ("mp4", b"video"),
        ("cover.png", b"cover"),
        ("cover.title-mask.png", b"mask"),
        ("cover.pre-overlay.png", b"pre-overlay"),
        ("cover.route-background.png", b"route-background"),
        ("srt", b"1\n00:00:00,000 --> 00:00:01,000\nhello\n"),
        (
            "speaker.srt",
            "1\n00:00:00,000 --> 00:00:01,000\n[李豆沙] hello\n".encode(),
        ),
        (
            "speaker.ass",
            b"[Events]\nDialogue: 0,0:00:00.00,0:00:01.00,LDS,,0,0,0,,hello\n",
        ),
        ("clip-context.json", b"{}\n"),
        ("subtitle-regression.json", b"{}\n"),
        ("chat-authority.json", b"{}\n"),
        (
            "publish.json",
            json.dumps(
                {
                    "title": title,
                    "recovery_publication_authority": authority,
                },
                ensure_ascii=False,
            ).encode("utf-8"),
        ),
        ("redelivery-baseline.json", b"{}\n"),
    ):
        path = root / f"{stem}.{suffix}"
        path.write_bytes(payload)
        files[suffix] = path
    files["chat-authority.json"].write_text(
        json.dumps(
            {
                "final_speaker_srt_sha256": _sha(files["speaker.srt"]),
                "speaker_ass_sha256": _sha(files["speaker.ass"]),
            }
        ),
        encoding="utf-8",
    )
    ass = tmp_path / "final.ass"
    ass.write_bytes(files["speaker.ass"].read_bytes())
    speaker_manifest = {
        "schema_version": SPEAKER_FINALIZATION_SCHEMA,
        "status": "READY",
        "production_ready": True,
        "text_final_srt_sha256": _sha(files["srt"]),
        "output_review_srt_sha256": _sha(files["speaker.srt"]),
        "source_cue_count": 1,
        "output_cue_count": 1,
        "final_decisions": [
            {
                "source_index": 1,
                "start": "00:00:00,000",
                "end": "00:00:01,000",
                "speaker": "李豆沙",
                "text": "hello",
                "decision_source": "ivan_reviewed_truth",
                "layer": 0,
                "placement": "main",
            }
        ],
    }
    speaker_manifest_source = tmp_path / "producer" / f"{candidate_id}.speaker-finalization.json"
    speaker_manifest_source.parent.mkdir()
    speaker_manifest_source.write_text(
        json.dumps(
            speaker_manifest,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    speaker_record = {
        "speaker_mode": "required",
        # The package builder must consume package-local SRT bytes, never this
        # stale producer pointer.
        "speaker_review_srt_path": "/vanished/recovery.speaker-final.srt",
        "speaker_finalization_manifest_path": str(speaker_manifest_source),
        "speaker_finalization_manifest_sha256": _sha(speaker_manifest_source),
        "speaker_finalization": speaker_manifest,
        "artifact_hashes": {
            "subtitle_sha256": _sha(files["srt"]),
            "speaker_review_srt_sha256": _sha(files["speaker.srt"]),
        },
    }
    rebuilt_speaker = rebuild_speaker_evidence(
        speaker_record,
        [SourceCue("1", 0, 1_000, "hello")],
        speaker_srt_bytes=files["speaker.srt"].read_bytes(),
        speaker_manifest_bytes=speaker_manifest_source.read_bytes(),
    )
    source_fact_receipt = review_and_repair_source_facts(
        selection_hook="当面对质",
        title=title,
        final_transcript="hello",
        clip_context_prompt="",
        llm_call=lambda _prompt: _keep_completion("当面对质", title),
        speaker_transcript=rebuilt_speaker.transcript,
        speaker_evidence=rebuilt_speaker.speaker_evidence,
        candidate_id=candidate_id,
        final_reviewed_srt_path=files["srt"],
    )
    assert source_fact_receipt["status"] == "PASS", source_fact_receipt
    story_contract = {
        "candidate_id": candidate_id,
        "selection_hook": "当面对质",
        "relation_state": "UNKNOWN",
        "participants": [],
        "source_fact_review": source_fact_receipt,
    }
    generation: dict[str, object] = {
        "story_contract": story_contract,
        "title": title,
        "cover_text": "对质",
        "method": "screenshot_direct",
        "cover_origin": "SOURCE_SCREENSHOT",
        "reference_sha256": "sha256:" + "1" * 64,
        "reference_authority": {},
        "final_cover_sha256": _sha(files["cover.png"]),
        "pre_overlay_sha256": _sha(files["cover.pre-overlay.png"]),
        "ai_background_sha256": _sha(files["cover.route-background.png"]),
        "rendered_text_pixels": {
            "mask_sha256": _sha(files["cover.title-mask.png"]),
            "pre_overlay_sha256": _sha(files["cover.pre-overlay.png"]),
        },
    }
    generation["route_decision"] = build_cover_route_decision(
        selected_treatment="screenshot_direct",
        selected_rationale="真实帧已经表达钩子",
        story_contract=story_contract,
        reference_authority={},
        decision_inputs={"composition_strength": "STRONG"},
        title=title,
        cover_text="对质",
    )
    record_cover_route_execution(
        generation,
        actual_treatment="screenshot_direct",
        execution_status="READY",
        image_generation_attempted=False,
        image_generation_used=False,
    )
    record = {
        **speaker_record,
        "story_contract": story_contract,
        "recovery_publication_authority": authority,
        "publish_staging": {
            "title": title,
            "recovery_publication_authority": authority,
            "cover_generation": generation,
            "source_fact_review": source_fact_receipt,
        },
        "artifact_hashes": {
            **speaker_record["artifact_hashes"],
            "burned_video_sha256": _sha(files["mp4"]),
            "cover_sha256": _sha(files["cover.png"]),
            "subtitle_sha256": _sha(files["srt"]),
            "ass_sha256": _sha(files["speaker.ass"]),
            "publish_draft_sha256": _sha(files["publish.json"]),
        },
        "burned_preview": {"ass_path": str(ass)},
        "subtitle_regression": {},
        "subtitle_regression_audit_path": str(files["subtitle-regression.json"]),
        "redelivery_baseline": {},
        "redelivery_baseline_audit_path": str(files["redelivery-baseline.json"]),
    }
    publish_payload = json.loads(files["publish.json"].read_text(encoding="utf-8"))
    publish_payload["source_fact_review"] = source_fact_receipt
    files["publish.json"].write_text(
        json.dumps(publish_payload, ensure_ascii=False),
        encoding="utf-8",
    )
    record["artifact_hashes"]["publish_draft_sha256"] = _sha(files["publish.json"])
    record_path = root / f"{stem}.record.json"
    record_path.write_text(json.dumps(record), encoding="utf-8")
    state = {
        "date": "2026-07-22",
        "status": "review_ready",
        "run_mode": "RECOVERY_REVIEW",
        "upload_allowed": False,
        "talk_selection_contract": {
            "mode": "EXACT_CANDIDATE_SET_NO_BACKFILL",
            "candidate_ids": [candidate_id],
        },
        "delivery_rerun_plan": {
            "schema_version": "recovery-review-talk-rerun-plan.v7",
            "recovery_publication_authorities_by_candidate": {candidate_id: authority},
        },
        "picks": [
            {
                "candidate_id": candidate_id,
                "status": "review_ready",
                "bundle_lifecycle": "CURRENT",
                "bundle_compliance": "COMPLIANT",
                "rc": 0,
            }
        ],
    }

    first = build_manifest(
        package_root=root,
        state=state,
        deployed_commit="a" * 40,
        created_at="2026-07-23T00:00:00+00:00",
    )
    assert first["items"][0]["title"] == title
    assert first["exact_candidate_ids"] == [candidate_id]
    assert first["cover_route_attestations"][0]["final_cover_sha256"] == _sha(files["cover.png"])
    assert first["items"][0]["cover_route_summary"]["actual_treatment"] == ("screenshot_direct")
    assert first["items"][0]["ass_path"] == f"{stem}.speaker.ass"
    assert first["items"][0]["ass_sha256"] == _sha(files["speaker.ass"])
    assert first["items"][0]["speaker_srt"] == f"{stem}.speaker.srt"
    assert first["items"][0]["speaker_srt_sha256"] == _sha(files["speaker.srt"])
    assert first["items"][0]["publish_json"] == f"{stem}.publish.json"
    assert first["items"][0]["subtitle_regression_status"] == "CONFIGURED"
    assert first["items"][0]["subtitle_regression_audit"] == (f"{stem}.subtitle-regression.json")
    assert first["items"][0]["redelivery_baseline_status"] == "CONFIGURED"
    assert first["items"][0]["redelivery_baseline"] == (f"{stem}.redelivery-baseline.json")
    packaged_speaker_manifest = root / first["items"][0]["speaker_finalization_manifest"]
    assert packaged_speaker_manifest.is_file()
    assert not packaged_speaker_manifest.is_symlink()
    assert first["items"][0]["speaker_finalization_manifest_sha256"] == _sha(
        packaged_speaker_manifest
    )

    regression_bytes = files["subtitle-regression.json"].read_bytes()
    baseline_bytes = files["redelivery-baseline.json"].read_bytes()
    files["subtitle-regression.json"].unlink()
    files["redelivery-baseline.json"].unlink()
    record["subtitle_regression"] = None
    record["subtitle_regression_audit_path"] = None
    record["redelivery_baseline"] = None
    record["redelivery_baseline_audit_path"] = None
    record_path.write_text(json.dumps(record), encoding="utf-8")
    unconfigured = build_manifest(
        package_root=root,
        state=state,
        deployed_commit="a" * 40,
        created_at="2026-07-23T00:05:00+00:00",
    )
    assert unconfigured["items"][0]["subtitle_regression_status"] == ("NOT_CONFIGURED")
    assert "subtitle_regression_audit" not in unconfigured["items"][0]
    assert unconfigured["items"][0]["redelivery_baseline_status"] == ("NOT_CONFIGURED")
    assert "redelivery_baseline" not in unconfigured["items"][0]

    record["subtitle_regression"] = {}
    record_path.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(
        ManifestBuildError,
        match="optional audit record binding is incomplete",
    ):
        build_manifest(
            package_root=root,
            state=state,
            deployed_commit="a" * 40,
            created_at="2026-07-23T00:06:00+00:00",
        )
    record["subtitle_regression_audit_path"] = str(files["subtitle-regression.json"])
    files["subtitle-regression.json"].write_text(json.dumps({"status": "DRIFT"}), encoding="utf-8")
    record_path.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(
        ManifestBuildError,
        match="optional audit payload differs from record",
    ):
        build_manifest(
            package_root=root,
            state=state,
            deployed_commit="a" * 40,
            created_at="2026-07-23T00:07:00+00:00",
        )
    files["subtitle-regression.json"].write_bytes(regression_bytes)
    files["redelivery-baseline.json"].write_bytes(baseline_bytes)
    record["subtitle_regression"] = {}
    record["subtitle_regression_audit_path"] = str(files["subtitle-regression.json"])
    record["redelivery_baseline"] = {}
    record["redelivery_baseline_audit_path"] = str(files["redelivery-baseline.json"])
    record_path.write_text(json.dumps(record), encoding="utf-8")

    # Ivan 2026-07-26 per-BV ruling: an explicit release scope unlocks a
    # single delivered candidate while the batch is still incomplete.
    partial_state = json.loads(json.dumps(state))
    partial_state["status"] = "recovery_incomplete"
    with pytest.raises(ManifestBuildError, match="state is not review_ready"):
        build_manifest(
            package_root=root,
            state=partial_state,
            deployed_commit="a" * 40,
            created_at="2026-07-23T00:10:00+00:00",
        )
    scoped = build_manifest(
        package_root=root,
        state=partial_state,
        deployed_commit="a" * 40,
        created_at="2026-07-23T00:10:00+00:00",
        release_scope=[candidate_id],
    )
    assert scoped["partial_release_scope"]["candidates"] == [candidate_id]
    assert scoped["partial_release_scope"]["batch_status"] == "recovery_incomplete"
    with pytest.raises(ManifestBuildError, match="subset of the exact contract"):
        build_manifest(
            package_root=root,
            state=partial_state,
            deployed_commit="a" * 40,
            created_at="2026-07-23T00:10:00+00:00",
            release_scope=[candidate_id, "auto_193450_9999_0000"],
        )

    original_ass = files["speaker.ass"].read_bytes()
    files["speaker.ass"].write_bytes(original_ass + b"; drift\n")
    with pytest.raises(ManifestBuildError, match="speaker ASS hash drift"):
        build_manifest(
            package_root=root,
            state=state,
            deployed_commit="a" * 40,
            created_at="2026-07-23T00:30:00+00:00",
        )
    files["speaker.ass"].write_bytes(original_ass)

    original_speaker_srt = files["speaker.srt"].read_bytes()
    files["speaker.srt"].write_bytes(original_speaker_srt + b"\n")
    with pytest.raises(ManifestBuildError, match="speaker SRT differs from chat authority"):
        build_manifest(
            package_root=root,
            state=state,
            deployed_commit="a" * 40,
            created_at="2026-07-23T00:45:00+00:00",
        )
    files["speaker.srt"].write_bytes(original_speaker_srt)

    speaker_manifest_bytes = speaker_manifest_source.read_bytes()
    packaged_speaker_manifest.unlink()
    speaker_manifest_source.unlink()
    with pytest.raises(
        ManifestBuildError,
        match="speaker/source-fact package evidence invalid.*source missing|invalid",
    ):
        build_manifest(
            package_root=root,
            state=state,
            deployed_commit="a" * 40,
            created_at="2026-07-23T00:50:00+00:00",
        )
    speaker_manifest_source.write_bytes(speaker_manifest_bytes)

    speaker_manifest_source.write_bytes(b"drifted\n")
    with pytest.raises(
        ManifestBuildError,
        match="speaker/source-fact package evidence invalid.*sha drift",
    ):
        build_manifest(
            package_root=root,
            state=state,
            deployed_commit="a" * 40,
            created_at="2026-07-23T00:51:00+00:00",
        )
    speaker_manifest_source.write_bytes(speaker_manifest_bytes)

    packaged_speaker_manifest.symlink_to(speaker_manifest_source)
    with pytest.raises(
        ManifestBuildError,
        match="speaker/source-fact package evidence invalid.*symlink",
    ):
        build_manifest(
            package_root=root,
            state=state,
            deployed_commit="a" * 40,
            created_at="2026-07-23T00:52:00+00:00",
        )
    packaged_speaker_manifest.unlink()

    legacy_receipt = review_and_repair_source_facts(
        selection_hook="当面对质",
        title=title,
        final_transcript="hello",
        clip_context_prompt="",
        llm_call=lambda _prompt: json.dumps(
            {
                "schema_version": "lidousha-source-fact-review.v1",
                "status": "KEEP",
                "final_selection_hook": "当面对质",
                "final_title": title,
                "supported_by": ["final_transcript"],
                "changed_surfaces": [],
                "addressee_attribution": [],
                "selection_scorecard_review": {
                    "status": "NOT_NEEDED",
                    "reason": "selection hook remains unchanged",
                },
                "summary": "旧回执没有当前人工说话人证据绑定。",
            },
            ensure_ascii=False,
        ),
        candidate_id=candidate_id,
        final_reviewed_srt_path=files["srt"],
    )
    assert legacy_receipt["status"] == "PASS"
    record["story_contract"]["source_fact_review"] = legacy_receipt
    record["publish_staging"]["source_fact_review"] = legacy_receipt
    publish_payload["source_fact_review"] = legacy_receipt
    files["publish.json"].write_text(
        json.dumps(publish_payload, ensure_ascii=False),
        encoding="utf-8",
    )
    record["artifact_hashes"]["publish_draft_sha256"] = _sha(files["publish.json"])
    record_path.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(
        ManifestBuildError,
        match="speaker/source-fact package evidence invalid.*invalid or stale",
    ):
        build_manifest(
            package_root=root,
            state=state,
            deployed_commit="a" * 40,
            created_at="2026-07-23T00:53:00+00:00",
        )
    record["story_contract"]["source_fact_review"] = source_fact_receipt
    record["publish_staging"]["source_fact_review"] = source_fact_receipt
    publish_payload["source_fact_review"] = source_fact_receipt
    files["publish.json"].write_text(
        json.dumps(publish_payload, ensure_ascii=False),
        encoding="utf-8",
    )
    record["artifact_hashes"]["publish_draft_sha256"] = _sha(files["publish.json"])

    record["publish_staging"]["title"] = "重跑后的标题"
    record_path.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(
        ManifestBuildError,
        match="recovery publication authority invalid",
    ):
        build_manifest(
            package_root=root,
            state=state,
            deployed_commit="b" * 40,
            created_at="2026-07-23T01:00:00+00:00",
        )

    record["publish_staging"]["title"] = title
    record["speaker_mode"] = "uniform_host"
    for key in (
        "speaker_review_srt_path",
        "speaker_finalization_manifest_path",
        "speaker_finalization_manifest_sha256",
        "speaker_finalization",
    ):
        record.pop(key)
    record["artifact_hashes"].pop("speaker_review_srt_sha256")
    uniform_ass = root / f"{stem}.final-sapphire72.ass"
    uniform_ass.write_bytes(original_ass)
    record["artifact_hashes"]["ass_sha256"] = _sha(uniform_ass)
    record["burned_preview"] = {"ass_path": str(uniform_ass)}
    files["chat-authority.json"].write_text(
        json.dumps(
            {
                "final_speaker_srt_sha256": _sha(files["srt"]),
                "speaker_ass_sha256": None,
            }
        ),
        encoding="utf-8",
    )
    absent = rebuild_speaker_evidence(
        {"speaker_mode": "uniform_host"},
        [SourceCue("1", 0, 1_000, "hello")],
    )
    uniform_receipt = review_and_repair_source_facts(
        selection_hook="当面对质",
        title=title,
        final_transcript="hello",
        clip_context_prompt="",
        llm_call=lambda _prompt: json.dumps(
            {
                "schema_version": "lidousha-source-fact-review.v1",
                "status": "KEEP",
                "final_selection_hook": "当面对质",
                "final_title": title,
                "supported_by": ["final_transcript"],
                "changed_surfaces": [],
                "addressee_attribution": [],
                "selection_scorecard_review": {
                    "status": "NOT_NEEDED",
                    "reason": "selection hook remains unchanged",
                },
                "summary": "单人车道没有独立说话人终稿。",
            },
            ensure_ascii=False,
        ),
        speaker_evidence=absent.speaker_evidence,
        candidate_id=candidate_id,
        final_reviewed_srt_path=files["srt"],
    )
    assert uniform_receipt["status"] == "PASS"
    record["story_contract"]["source_fact_review"] = uniform_receipt
    record["publish_staging"]["source_fact_review"] = uniform_receipt
    publish_payload["source_fact_review"] = uniform_receipt
    files["publish.json"].write_text(
        json.dumps(publish_payload, ensure_ascii=False),
        encoding="utf-8",
    )
    record["artifact_hashes"]["publish_draft_sha256"] = _sha(files["publish.json"])
    record_path.write_text(json.dumps(record), encoding="utf-8")

    uniform = build_manifest(
        package_root=root,
        state=state,
        deployed_commit="c" * 40,
        created_at="2026-07-23T01:05:00+00:00",
    )
    assert uniform["items"][0]["speaker_srt"] == f"{stem}.srt"
    assert "speaker_finalization_manifest" not in uniform["items"][0]
