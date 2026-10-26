"""Producer-shaped synthetic packages, without historical candidate tests."""
from pathlib import Path
import hashlib
import json
import shutil
from typing import TypeAlias
import pytest
import scripts.build_manual_review_manifest as manual_review_manifest
from src.autoslice.addressee_attribution import rebuild_speaker_evidence
from src.autoslice.review_evidence import SourceCue
from src.autoslice.speaker_common import SPEAKER_FINALIZATION_SCHEMA
from src.autoslice.source_fact_review import review_and_repair_source_facts
from src.autoslice.cover_route_evidence import build_cover_route_decision, record_cover_route_execution
from src.autoslice.producer_delivery_transaction import DeliveryArtifact, deployment_authority_binding, prepare_delivery
from src.autoslice.repository_asset_authority import _canonical_sha256
import src.autoslice.normal_first_publication_media_successor as normal_media_successor
from src.autoslice.channel_envelope import render_talk_title_prefix

Json: TypeAlias = "None | bool | int | float | str | list[Json] | dict[str, Json]"
JsonMap: TypeAlias = dict[str, Json]

TITLE = render_talk_title_prefix() + "手动包审计闭环用例标题够长了"
HOOK = "测试主播当场语塞三秒，弹幕全体起立。"
TRANSCRIPT = "当场语塞了三秒，弹幕全体起立"

def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()



def _receipt(
    *,
    title: str = TITLE,
    speaker_evidence: JsonMap | None = None,
    speaker_transcript: str | None = None,
    transcript: str = TRANSCRIPT,
) -> dict:
    def cpa(_prompt: str) -> str:
        return json.dumps(
            {
                "schema_version": "lidousha-source-fact-review.v1",
                "status": "KEEP",
                "final_selection_hook": HOOK,
                "final_title": title,
                "supported_by": ["final_transcript"],
                "changed_surfaces": [],
                # F12：判项必填；无说话人转写 -> UNVERIFIABLE 车道，留空数组。
                "addressee_attribution": [],
                "selection_scorecard_review": {
                    "status": "NOT_NEEDED",
                    "reason": "selection hook remains unchanged",
                },
                "summary": "同片文字证据足以完成事实裁决。",
            },
            ensure_ascii=False,
        )

    review = review_and_repair_source_facts(
        selection_hook=HOOK,
        title=title,
        final_transcript=transcript,
        clip_context_prompt="",
        llm_call=cpa,
        speaker_evidence=speaker_evidence,
        speaker_transcript=speaker_transcript,
    )
    assert review["status"] == "PASS"
    return review



def _package(
    tmp_path: Path,
    *,
    speaker_finalized: bool = False,
    candidate_id: str = "manual_cand_9",
    title: str = TITLE,
    stem: str = "手动闭环用例",
) -> Path:
    pkg = tmp_path / "pkg"
    pkg.mkdir()

    def w(name: str, payload: bytes) -> Path:
        path = pkg / name
        path.write_bytes(payload)
        return path

    mp4 = w(f"{stem}.mp4", b"burned-final-bytes")
    srt = w(
        f"{stem}.srt",
        f"1\n00:00:00,000 --> 00:00:02,000\n{TRANSCRIPT}\n".encode("utf-8"),
    )
    text_srt_sha = "sha256:" + _sha(srt)
    speaker_record_fields: JsonMap
    speaker_evidence: JsonMap | None = None
    speaker_transcript: str | None = None
    if speaker_finalized:
        speaker_srt = w(
            f"{stem}.speaker.srt",
            (f"1\n00:00:00,000 --> 00:00:02,000\n[测试主播] {TRANSCRIPT}\n").encode("utf-8"),
        )
        speaker_ass = w(f"{stem}.speaker.ass", b"speaker-ass-bytes")
        speaker_manifest = {
            "schema_version": SPEAKER_FINALIZATION_SCHEMA,
            "status": "READY",
            "production_ready": True,
            "text_final_srt_sha256": text_srt_sha,
            "output_review_srt_sha256": "sha256:" + _sha(speaker_srt),
            "source_cue_count": 1,
            "output_cue_count": 1,
            "final_decisions": [
                {
                    "source_index": 1,
                    "start": "00:00:00,000",
                    "end": "00:00:02,000",
                    "speaker": "测试主播",
                    "text": TRANSCRIPT,
                    "decision_source": "reviewer_reviewed_truth",
                    "layer": 0,
                    "placement": "main",
                }
            ],
        }
        speaker_manifest_source = tmp_path / "producer" / "manual_cand_9.speaker-finalization.json"
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
        speaker_record_fields = {
            "speaker_mode": "required",
            # Package rebuild must not dereference this producer SRT pointer.
            "speaker_review_srt_path": "/vanished/manual_cand_9.speaker-final.srt",
            "speaker_finalization_manifest_path": str(speaker_manifest_source),
            "speaker_finalization_manifest_sha256": ("sha256:" + _sha(speaker_manifest_source)),
            "speaker_finalization": speaker_manifest,
        }
        evidence_record = {
            **speaker_record_fields,
            "artifact_hashes": {
                "subtitle_sha256": text_srt_sha,
                "speaker_review_srt_sha256": "sha256:" + _sha(speaker_srt),
            },
        }
        rebuilt = rebuild_speaker_evidence(
            evidence_record,
            [SourceCue("1", 0, 2_000, TRANSCRIPT)],
            speaker_srt_bytes=speaker_srt.read_bytes(),
            speaker_manifest_bytes=speaker_manifest_source.read_bytes(),
        )
        speaker_evidence = rebuilt.speaker_evidence
        speaker_transcript = rebuilt.transcript
        chat_authority_doc = {
            "final_text_srt_sha256": text_srt_sha,
            "final_speaker_srt_sha256": "sha256:" + _sha(speaker_srt),
            "speaker_ass_path": str(speaker_ass),
            "speaker_ass_sha256": "sha256:" + _sha(speaker_ass),
        }
    else:
        w(f"{stem}.final-sapphire72.ass", b"ass-bytes")
        speaker_record_fields = {"speaker_mode": "uniform_host"}
        chat_authority_doc = {
            "final_text_srt_sha256": text_srt_sha,
            "final_speaker_srt_sha256": text_srt_sha,
            "speaker_ass_path": None,
            "speaker_ass_sha256": None,
        }
    receipt = _receipt(
        title=title,
        speaker_evidence=speaker_evidence,
        speaker_transcript=speaker_transcript,
    )
    w(
        f"{stem}.chat-authority.json",
        json.dumps(chat_authority_doc, ensure_ascii=False).encode("utf-8"),
    )
    w(f"{stem}.clip-context.json", json.dumps({"recording_date": "2026-08-02"}).encode())
    cover = w(f"{stem}.cover.png", b"cover-bytes")
    pre_overlay = w(f"{stem}.cover.pre-overlay.png", b"pre-overlay-bytes")
    ai_bg = w(f"{stem}.cover.ai-bg.png", b"ai-bg-bytes")
    mask = w(f"{stem}.cover.title-mask.png", b"mask-bytes")

    story_contract = {
        "selection_hook": HOOK,
        "clip_context_prompt": "",
        "selection_scorecard": None,
        "source_fact_review": receipt,
    }
    cover_generation = {
        "method": "images.edit",
        "final_cover_sha256": "sha256:" + _sha(cover),
        "reference_sha256": "sha256:deadbeef",
        "pre_overlay_path": str(pre_overlay),
        "pre_overlay_sha256": "sha256:" + _sha(pre_overlay),
        "ai_background": str(ai_bg),
        "ai_background_sha256": "sha256:" + _sha(ai_bg),
        "rendered_text_pixels": {
            "mask_path": str(mask),
            "mask_sha256": "sha256:" + _sha(mask),
        },
        "route_decision": {"selected_treatment": "cpa_redraw"},
        "reference_authority": None,
    }
    record_doc = {
        "schema_version": "delivery-record.v1",
        "classification": "talk",
        "candidate_id": candidate_id,
        **speaker_record_fields,
        "story_contract": story_contract,
        "publish_staging": {
            "title": title,
            "upload_enabled": False,
            "source_fact_review": receipt,
        },
        "artifact_hashes": {
            "video_sha256": "sha256:" + _sha(mp4),
            "subtitle_sha256": text_srt_sha,
            **(
                {"speaker_review_srt_sha256": ("sha256:" + _sha(pkg / f"{stem}.speaker.srt"))}
                if speaker_finalized
                else {}
            ),
        },
    }
    publish_doc = {
        "schema_version": "shadow-publish-draft.v1",
        "candidate_id": candidate_id,
        "title": title,
        "upload_enabled": False,
        "cover_status": "AI_COVER_READY",
        "cover_generation": cover_generation,
        "source_fact_review": receipt,
        "artifact_hashes": {"video_sha256": "sha256:" + _sha(mp4)},
    }
    (pkg / f"{stem}.record.json").write_text(
        json.dumps(record_doc, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (pkg / f"{stem}.publish.json").write_text(
        json.dumps(publish_doc, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return pkg



def _prepared_successor_authority(candidate_id: str = "manual_cand_9") -> JsonMap:
    return {
        "schema_version": "new-bv-same-bv-publication-authority.v1",
        "candidate_id": candidate_id,
        "observed_public_title": TITLE,
        "title": TITLE,
        "bvid": "BV1abcdefgh2",
        "aid": 101,
        "cid": 202,
        "authority_sha256": "sha256:" + "a" * 64,
    }



def _seal_prepared_successor_runtime(root: Path) -> None:
    repo = root / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    commit = "b" * 40
    authority_asset = repo / "docs/pipeline/80-package-delivery.md"
    authority_asset.parent.mkdir(parents=True, exist_ok=True)
    authority_asset.write_bytes(b"package-delivery-authority")
    body = {
        "schema_version": "deployed-authority-manifest.v1",
        "deployed_commit": commit,
        "entries": {
            "docs/pipeline/80-package-delivery.md": {
                "bytes": authority_asset.stat().st_size,
                "sha256": "sha256:" + _sha(authority_asset),
            }
        },
    }
    manifest = dict(body)
    manifest["manifest_sha256"] = _canonical_sha256(body)
    (repo / "DEPLOYED_COMMIT").write_text(commit + "\n", encoding="utf-8")
    (repo / "DEPLOYED_AUTHORITY_MANIFEST.json").write_text(
        json.dumps(manifest, sort_keys=True), encoding="utf-8"
    )



def _prepared_successor_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    lane: str = "talk",
    include_source_provenance: bool = True,
    reused_cover: bool = False,
    verified_title_repair: dict | None = None,
    fresh_private: bool = False,
    keep_story_contract: bool = False,
    include_cover_network: bool = False,
    include_extended_talk_roles: bool = False,
) -> tuple[Path, Path, JsonMap, Path]:
    """Build a producer-shaped prepared handle and package under one runtime root."""

    tmp_path.mkdir(parents=True, exist_ok=True)
    source_package = _package(tmp_path)
    runtime = tmp_path / "prepared-runtime"
    runtime.mkdir()
    _seal_prepared_successor_runtime(runtime)
    package = runtime / "package"
    shutil.copytree(source_package, package)
    stem = next(package.glob("*.publish.json")).name[: -len(".publish.json")]
    candidate_id = "manual_cand_9"
    authority = _prepared_successor_authority(candidate_id)
    if verified_title_repair is not None:
        authority["observed_public_title"] = "原始已公开标题"

    # This is the new full-media successor: its video and SRT differ from the
    # ordinary fixture's historical bytes, while the package hashes and
    # producer handle are updated to those exact bytes.
    (package / f"{stem}.mp4").write_bytes(b"prepared-successor-video")
    (package / f"{stem}.srt").write_text(
        "1\n00:00:00,000 --> 00:00:02,100\n"
        f"{TRANSCRIPT}\n",
        encoding="utf-8",
    )
    subtitle_sha = "sha256:" + _sha(package / f"{stem}.srt")
    video_sha = "sha256:" + _sha(package / f"{stem}.mp4")
    chat_path = package / f"{stem}.chat-authority.json"
    chat = json.loads(chat_path.read_text(encoding="utf-8"))
    chat["final_text_srt_sha256"] = subtitle_sha
    chat["final_speaker_srt_sha256"] = subtitle_sha
    chat_path.write_text(json.dumps(chat, ensure_ascii=False), encoding="utf-8")

    record_path = package / f"{stem}.record.json"
    publish_path = package / f"{stem}.publish.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    publish = json.loads(publish_path.read_text(encoding="utf-8"))
    record["recovery_publication_authority"] = authority
    if verified_title_repair is not None:
        record["verified_title_repair"] = verified_title_repair
    record["publish_staging"]["recovery_publication_authority"] = authority
    record["artifact_hashes"]["video_sha256"] = video_sha
    record["artifact_hashes"]["subtitle_sha256"] = subtitle_sha
    publish["recovery_publication_authority"] = authority
    publish["artifact_hashes"]["video_sha256"] = video_sha
    if reused_cover:
        generation = dict(publish["cover_generation"])
        generation.update(
            {
                "method": "screenshot_direct",
                "cover_origin": "SOURCE_SCREENSHOT",
                "reference_sha256": "sha256:" + "d" * 64,
                "final_cover_sha256": "sha256:" + _sha(package / f"{stem}.cover.png"),
                "carried_forward_from_published_record": True,
            }
        )
        generation["route_decision"] = build_cover_route_decision(
            selected_treatment="screenshot_direct",
            selected_rationale="published cover bytes are carried forward",
            story_contract=None,
            reference_authority=None,
            title=TITLE,
            cover_text="封面证据",
            decision_inputs={
                "cover_mode": "screenshot",
                "subject_confident": True,
                "verified_stream_frame": True,
            },
        )
        record_cover_route_execution(
            generation,
            actual_treatment="screenshot_direct",
            execution_status="READY",
            image_generation_attempted=False,
            image_generation_used=False,
        )
        publish["cover_status"] = "REUSED_COVER"
        publish["cover_generation"] = generation
        record["publish_staging"]["cover_status"] = "REUSED_COVER"
        record["publish_staging"]["cover_generation"] = generation
    if fresh_private:
        # The fresh lane has no same-BV authority.  Mirror the current
        # producer surfaces exactly, including the staging candidate binding
        # introduced for new packages; leave no story binding here so this
        # fixture isolates prepared/reference validation.
        record.pop("recovery_publication_authority", None)
        if not keep_story_contract:
            record.pop("story_contract", None)
        record["publish_staging"].pop("recovery_publication_authority", None)
        record["publish_staging"]["candidate_id"] = candidate_id
        record["publish_staging"]["cover_generation"] = dict(publish["cover_generation"])
        record["publish_staging"]["cover_status"] = publish["cover_status"]
        record["publish_staging"]["cover_path"] = str(package / f"{stem}.cover.png")
        publish.pop("recovery_publication_authority", None)
        publish["cover_path"] = str(package / f"{stem}.cover.png")
        if keep_story_contract:
            from src.autoslice.story_contract import cover_story_contract_binding

            record["story_contract"].update(
                {
                    "schema_version": "lidousha-story-contract.v1",
                    "relation_state": "UNVERIFIED",
                    "participants": [],
                    "cover_counterpart_reference_available": False,
                    "cover_reference_authority": None,
                    "source_media_sha256s": {},
                    "clip_context_binding": {},
                    "boundary_semantic_review": None,
                    "human_boundary_authority": None,
                    "cover_fallback_mode": "AI_GENERATED",
                }
            )
            story_binding = cover_story_contract_binding(record["story_contract"])
            record["publish_staging"]["cover_generation"]["story_contract"] = story_binding
            publish["cover_generation"]["story_contract"] = story_binding
    record_path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    publish_path.write_text(json.dumps(publish, ensure_ascii=False), encoding="utf-8")

    if include_source_provenance:
        (package / f"{stem}.provenance.json").write_bytes(b"producer-provenance")
    route_background = package / f"{stem}.cover.route-background.png"
    route_background.write_bytes((package / f"{stem}.cover.ai-bg.png").read_bytes())

    monkeypatch.setattr(
        manual_review_manifest,
        "validate_recovery_publication_authority",
        lambda value, **_: authority,
    )
    monkeypatch.setattr(
        normal_media_successor,
        "validate_new_bv_repair_authority",
        lambda value, **_: authority,
    )
    monkeypatch.setattr(
        normal_media_successor,
        "replay_first_publication_manifest",
        lambda value: {
            "candidate_id": candidate_id,
            "title": TITLE,
            "bvid": authority["bvid"],
            "aid": authority["aid"],
            "cid": authority["cid"],
        },
    )

    from src.autoslice.normal_first_publication_media_successor import ROLE_SUFFIXES

    source_by_role = {
        "video": package / f"{stem}.mp4",
        "subtitle": package / f"{stem}.srt",
        "uniform_host_ass": package / f"{stem}.final-sapphire72.ass",
        "chat_authority": package / f"{stem}.chat-authority.json",
        "clip_context": package / f"{stem}.clip-context.json",
        "cover_title_mask": package / f"{stem}.cover.title-mask.png",
        "cover_pre_overlay": package / f"{stem}.cover.pre-overlay.png",
        "cover_route_background": route_background,
        "publish": publish_path,
        "record": record_path,
        "cover": package / f"{stem}.cover.png",
    }
    if include_source_provenance:
        source_by_role["source_provenance"] = package / f"{stem}.provenance.json"
    if include_cover_network:
        for role, suffix in [('cover_request','.cover-request.json'),('cover_response','.cover-response.json')]:
            path=package/f'{stem}{suffix}'
            path.write_text(json.dumps({'offline_fixture_role':role}))
            source_by_role[role]=path
    if include_extended_talk_roles:
        for role in (
            "redelivery_baseline",
            "subtitle_regression",
            "filler_audit",
            "text_finalization",
        ):
            path = package / f"{stem}{ROLE_SUFFIXES[role]}"
            path.write_text(json.dumps({"offline_fixture_role": role}))
            source_by_role[role] = path
    artifacts = [
        DeliveryArtifact(
            role,
            source,
            runtime / "delivery" / "2026-08-02" / f"{stem}{ROLE_SUFFIXES[role]}",
            "sha256:" + _sha(source),
        )
        for role, source in source_by_role.items()
    ]
    prepared = prepare_delivery(
        runtime_root=runtime,
        lane=lane,
        candidate_id=candidate_id,
        artifacts=artifacts,
        deployed_authority=deployment_authority_binding(runtime),
    )
    return package, prepared.manifest_path, authority, runtime
