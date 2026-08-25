"""手动产线包的 review manifest：hash-bound 自真实产物 + 显式操作者署名。"""

import hashlib
import json
from pathlib import Path

import pytest

import scripts.build_manual_review_manifest as manual_review_manifest

from scripts.build_manual_review_manifest import DailyManifestError, build_manual
from src.autoslice.addressee_attribution import rebuild_speaker_evidence
from src.autoslice.review_evidence import SourceCue
from src.autoslice.speaker_common import SPEAKER_FINALIZATION_SCHEMA
from src.autoslice.source_fact_review import review_and_repair_source_facts
from src.autoslice.published_recovery_package_contract import (
    STATE_AUTHORITY_SHA256,
    build_published_recovery_package_receipt,
)
from src.autoslice.recovery_title_authority import build_recovery_publication_authorities

TITLE = "【李豆沙】手动包审计闭环用例标题够长了"
HOOK = "小李当场语塞三秒，弹幕全体起立。"
TRANSCRIPT = "当场语塞了三秒，弹幕全体起立"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _receipt(
    *,
    title: str = TITLE,
    speaker_evidence: dict[str, object] | None = None,
    speaker_transcript: str | None = None,
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
        final_transcript=TRANSCRIPT,
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
    speaker_record_fields: dict[str, object]
    speaker_evidence: dict[str, object] | None = None
    speaker_transcript: str | None = None
    if speaker_finalized:
        speaker_srt = w(
            f"{stem}.speaker.srt",
            (f"1\n00:00:00,000 --> 00:00:02,000\n[李豆沙] {TRANSCRIPT}\n").encode("utf-8"),
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
                    "speaker": "李豆沙",
                    "text": TRANSCRIPT,
                    "decision_source": "ivan_reviewed_truth",
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


def test_manual_manifest_is_hash_bound_with_attestation(tmp_path: Path) -> None:
    pkg = _package(tmp_path)
    manifest = build_manual(pkg, operator="operator-a", note="边界与字幕人工复核通过")

    assert manifest["schema_version"] == "lidousha-manual-review-manifest.v1"
    assert manifest["status"] == "review_ready"
    assert manifest["upload_allowed"] is False
    assert manifest["manual_attestation"]["operator"] == "operator-a"
    assert manifest["date"] == "2026-08-02"
    assert manifest["story_contract_required"] is True
    assert manifest["source_fact_review_required"] is True
    item = manifest["items"][0]
    assert item["kind"] == "talk"
    assert item["video"] == "手动闭环用例.mp4"
    assert item["sha256"]["video"] == _sha(pkg / "手动闭环用例.mp4")
    assert item["cover_pre_overlay"] == "手动闭环用例.cover.pre-overlay.png"


def test_manual_manifest_binds_real_speaker_artifacts(tmp_path: Path) -> None:
    pkg = _package(tmp_path, speaker_finalized=True)
    manifest = build_manual(pkg, operator="operator-a", note="说话人存疑转人工审阅")

    item = manifest["items"][0]
    assert item["ass_path"] == "手动闭环用例.speaker.ass"
    assert item["speaker_srt"] == "手动闭环用例.speaker.srt"
    assert item["speaker_srt"] != item["subtitle_srt"]
    assert item["speaker_srt_sha256"] == _sha(pkg / "手动闭环用例.speaker.srt")
    speaker_manifest = pkg / item["speaker_finalization_manifest"]
    assert speaker_manifest.is_file()
    assert not speaker_manifest.is_symlink()
    assert item["speaker_finalization_manifest_sha256"] == ("sha256:" + _sha(speaker_manifest))


def test_manual_manifest_refuses_missing_bound_speaker_manifest(
    tmp_path: Path,
) -> None:
    pkg = _package(tmp_path, speaker_finalized=True)
    record = json.loads(next(pkg.glob("*.record.json")).read_text(encoding="utf-8"))
    Path(record["speaker_finalization_manifest_path"]).unlink()

    with pytest.raises(DailyManifestError, match="source missing|invalid"):
        build_manual(pkg, operator="operator-a", note="说话人存疑转人工审阅")


def test_manual_manifest_refuses_speaker_manifest_hash_drift(
    tmp_path: Path,
) -> None:
    pkg = _package(tmp_path, speaker_finalized=True)
    record = json.loads(next(pkg.glob("*.record.json")).read_text(encoding="utf-8"))
    Path(record["speaker_finalization_manifest_path"]).write_bytes(b"drifted\n")

    with pytest.raises(DailyManifestError, match="sha drift"):
        build_manual(pkg, operator="operator-a", note="说话人存疑转人工审阅")


def test_manual_manifest_refuses_symlinked_package_speaker_manifest(
    tmp_path: Path,
) -> None:
    pkg = _package(tmp_path, speaker_finalized=True)
    record = json.loads(next(pkg.glob("*.record.json")).read_text(encoding="utf-8"))
    source = Path(record["speaker_finalization_manifest_path"])
    (pkg / source.name).symlink_to(source)

    with pytest.raises(DailyManifestError, match="symlink"):
        build_manual(pkg, operator="operator-a", note="说话人存疑转人工审阅")


def test_manual_manifest_refuses_receipt_without_current_speaker_binding(
    tmp_path: Path,
) -> None:
    pkg = _package(tmp_path, speaker_finalized=True)
    legacy_receipt = _receipt()
    record_path = next(pkg.glob("*.record.json"))
    publish_path = next(pkg.glob("*.publish.json"))
    record = json.loads(record_path.read_text(encoding="utf-8"))
    publish = json.loads(publish_path.read_text(encoding="utf-8"))
    record["story_contract"]["source_fact_review"] = legacy_receipt
    record["publish_staging"]["source_fact_review"] = legacy_receipt
    publish["source_fact_review"] = legacy_receipt
    record_path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    publish_path.write_text(json.dumps(publish, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(DailyManifestError, match="invalid or stale"):
        build_manual(pkg, operator="operator-a", note="说话人存疑转人工审阅")


def test_manual_manifest_accepts_fresh_uniform_host_evidence(
    tmp_path: Path,
) -> None:
    pkg = _package(tmp_path)
    absent = rebuild_speaker_evidence(
        {"speaker_mode": "uniform_host"},
        [SourceCue("1", 0, 2_000, TRANSCRIPT)],
    )
    receipt = _receipt(speaker_evidence=absent.speaker_evidence)
    record_path = next(pkg.glob("*.record.json"))
    publish_path = next(pkg.glob("*.publish.json"))
    record = json.loads(record_path.read_text(encoding="utf-8"))
    publish = json.loads(publish_path.read_text(encoding="utf-8"))
    record["story_contract"]["source_fact_review"] = receipt
    record["publish_staging"]["source_fact_review"] = receipt
    publish["source_fact_review"] = receipt
    record_path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    publish_path.write_text(json.dumps(publish, ensure_ascii=False), encoding="utf-8")

    manifest = build_manual(pkg, operator="operator-a", note="单人包进入人工审阅")

    assert "speaker_finalization_manifest" not in manifest["items"][0]
    assert manifest["source_fact_review_sha256"] == receipt["receipt_sha256"]


def test_manual_manifest_keeps_legacy_song_lane_independent(
    tmp_path: Path,
) -> None:
    pkg = _package(tmp_path)
    record_path = next(pkg.glob("*.record.json"))
    publish_path = next(pkg.glob("*.publish.json"))
    record = json.loads(record_path.read_text(encoding="utf-8"))
    publish = json.loads(publish_path.read_text(encoding="utf-8"))
    record["classification"] = "song"
    record.pop("speaker_mode")
    record["story_contract"].pop("source_fact_review")
    record["publish_staging"].pop("source_fact_review")
    publish.pop("source_fact_review")
    record_path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    publish_path.write_text(json.dumps(publish, ensure_ascii=False), encoding="utf-8")

    manifest = build_manual(pkg, operator="operator-a", note="旧歌切歌词已人工复核")

    assert manifest["items"][0]["kind"] == "song"
    assert "source_fact_review_required" not in manifest


def test_manual_manifest_refuses_missing_speaker_ass(tmp_path: Path) -> None:
    pkg = _package(tmp_path, speaker_finalized=True)
    (pkg / "手动闭环用例.speaker.ass").unlink()

    with pytest.raises(DailyManifestError, match="speaker.ass"):
        build_manual(pkg, operator="operator-a", note="说话人存疑转人工审阅")


def test_manual_manifest_refuses_invalid_chat_authority(tmp_path: Path) -> None:
    pkg = _package(tmp_path)
    next(pkg.glob("*.chat-authority.json")).write_text("{not-json", encoding="utf-8")

    with pytest.raises(DailyManifestError, match="chat authority"):
        build_manual(pkg, operator="operator-a", note="单人包进入人工审阅")


def test_manual_manifest_requires_ready_cover(tmp_path: Path) -> None:
    pkg = _package(tmp_path)
    publish_path = next(pkg.glob("*.publish.json"))
    doc = json.loads(publish_path.read_text())
    doc["cover_status"] = "BLOCKED_AI_COVER_REQUIRED"
    publish_path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(DailyManifestError, match="AI_COVER_READY"):
        build_manual(pkg, operator="op", note="x")


def test_manual_manifest_requires_operator_attestation(tmp_path: Path) -> None:
    pkg = _package(tmp_path)
    with pytest.raises(DailyManifestError, match="attestation"):
        build_manual(pkg, operator=" ", note="")


def test_receipt_drift_across_surfaces_is_refused(tmp_path: Path) -> None:
    pkg = _package(tmp_path)
    record_path = next(pkg.glob("*.record.json"))
    doc = json.loads(record_path.read_text())
    doc["publish_staging"]["source_fact_review"] = {"schema_version": "x"}
    record_path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(DailyManifestError, match="drift|missing"):
        build_manual(pkg, operator="op", note="x")


def test_cover_bytes_must_match_generation_hash(tmp_path: Path) -> None:
    pkg = _package(tmp_path)
    cover = next(pkg.glob("*.cover.png"))
    cover.write_bytes(b"tampered-cover")
    with pytest.raises(DailyManifestError, match="final_cover_sha256"):
        build_manual(pkg, operator="op", note="x")


def test_manual_manifest_refuses_partial_same_bv_authority_surface(
    tmp_path: Path,
) -> None:
    pkg = _package(tmp_path)
    record_path = next(pkg.glob("*.record.json"))
    record = json.loads(record_path.read_text(encoding="utf-8"))
    # A malformed object is enough here: the builder must reject a partial
    # five-surface same-BV contract before it can reach unrelated source-fact
    # checks on this deliberately generic manual fixture.
    record["recovery_publication_authority"] = {"schema_version": "x"}
    record_path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(DailyManifestError, match="drifts"):
        build_manual(pkg, operator="op", note="same-BV corrected package")


def test_manual_manifest_projects_typed_qixi_same_bv_receipt(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    registry = root / "assets/lidousha/daily_same_bv_publication_authority.v1.json"
    candidate_id = "auto_113022_354_496"
    authority = build_recovery_publication_authorities(
        candidate_ids={candidate_id},
        registry_path=registry,
        expected_registry_sha256="sha256:" + _sha(registry),
        repo_root=root,
    )[candidate_id]
    pkg = _package(
        tmp_path,
        candidate_id=candidate_id,
        title=authority["observed_public_title"],
    )
    record_path = next(pkg.glob("*.record.json"))
    publish_path = next(pkg.glob("*.publish.json"))
    record = json.loads(record_path.read_text(encoding="utf-8"))
    publish = json.loads(publish_path.read_text(encoding="utf-8"))
    record["recovery_publication_authority"] = authority
    record["publish_staging"]["recovery_publication_authority"] = authority
    publish["recovery_publication_authority"] = authority
    record_path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    publish_path.write_text(json.dumps(publish, ensure_ascii=False), encoding="utf-8")
    stem = publish_path.name[: -len(".publish.json")]
    artifact_names = {
        "video": f"{stem}.mp4",
        "subtitle": f"{stem}.srt",
        "cover": f"{stem}.cover.png",
        "record": record_path.name,
        "publish": publish_path.name,
        "chat_authority": f"{stem}.chat-authority.json",
    }
    artifact_entries = {
        key: {"target_role": "package", "target": name}
        for key, name in artifact_names.items()
    }
    generated = {
        key: "sha256:" + _sha(pkg / artifact_names[key])
        for key in ("record", "publish", "chat_authority")
    }
    (pkg / "qixi-corrected-package-finalization.json").write_text(
        json.dumps(
            {
                "schema_version": "qixi-corrected-package-finalization-receipt.v1",
                "mode": "APPLIED",
                "candidate_id": candidate_id,
                "target_candidate_root": str(pkg.parent),
                "package_root": str(pkg),
                "authority_sha256": "sha256:" + "a" * 64,
                "artifacts": artifact_entries,
                "manual_corrected_same_bv": {
                    "schema_version": "manual-corrected-same-bv.v1",
                    "candidate_id": candidate_id,
                    "recovery_publication_authority": authority,
                    "approved_burned_video_sha256": "sha256:" + _sha(pkg / f"{stem}.mp4"),
                    "approved_subtitle_sha256": "sha256:" + _sha(pkg / f"{stem}.srt"),
                    "approved_cover_sha256": "sha256:" + _sha(pkg / f"{stem}.cover.png"),
                }
                ,"after_image_sha256": generated,
                "generated_record_sha256": generated["record"],
                "generated_publish_sha256": generated["publish"],
                "generated_chat_authority_sha256": generated["chat_authority"],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    # A hand-written outer receipt cannot impersonate the sealed finalizer
    # plan, even when its inner video/SRT/cover bindings happen to match.
    with pytest.raises(DailyManifestError, match="receipt"):
        build_manual(pkg, operator="op", note="Qixi manual correction")


def test_manual_builder_projects_replayed_typed_qixi_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = Path(__file__).resolve().parents[1]
    registry = root / "assets/lidousha/daily_same_bv_publication_authority.v1.json"
    candidate_id = "auto_113022_354_496"
    authority = build_recovery_publication_authorities(
        candidate_ids={candidate_id},
        registry_path=registry,
        expected_registry_sha256="sha256:" + _sha(registry),
        repo_root=root,
    )[candidate_id]
    pkg = _package(tmp_path, candidate_id=candidate_id, title=authority["observed_public_title"])
    record_path = next(pkg.glob("*.record.json"))
    publish_path = next(pkg.glob("*.publish.json"))
    record = json.loads(record_path.read_text(encoding="utf-8"))
    publish = json.loads(publish_path.read_text(encoding="utf-8"))
    record["recovery_publication_authority"] = authority
    record["publish_staging"]["recovery_publication_authority"] = authority
    publish["recovery_publication_authority"] = authority
    record_path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    publish_path.write_text(json.dumps(publish, ensure_ascii=False), encoding="utf-8")
    stem = publish_path.name[: -len(".publish.json")]
    inner = {
        "schema_version": "manual-corrected-same-bv.v1",
        "candidate_id": candidate_id,
        "recovery_publication_authority": authority,
        "approved_burned_video_sha256": "sha256:" + _sha(pkg / f"{stem}.mp4"),
        "approved_subtitle_sha256": "sha256:" + _sha(pkg / f"{stem}.srt"),
        "approved_cover_sha256": "sha256:" + _sha(pkg / f"{stem}.cover.png"),
    }
    outer = {"manual_corrected_same_bv": inner}
    (pkg / "qixi-corrected-package-finalization.json").write_text(
        json.dumps(outer, ensure_ascii=False), encoding="utf-8"
    )
    monkeypatch.setattr(
        manual_review_manifest,
        "validate_applied_receipt",
        lambda raw, *, package_root, repo_root=None: raw,
    )

    manifest = build_manual(pkg, operator="op", note="Qixi manual correction")
    item = manifest["items"][0]
    assert item["manual_corrected_same_bv"] == inner
    assert item["manual_corrected_same_bv_receipt"] == "qixi-corrected-package-finalization.json"
    assert item["manual_corrected_same_bv_receipt_sha256"].startswith("sha256:")
    assert manifest["status"] == (
        "finished_review_package_no_upload_pending_human_review"
    )
    assert manifest["upload_allowed"] is False
    assert manifest["exact_candidate_ids"] == [candidate_id]
    assert manifest["selection_contract"] == {
        "mode": "EXACT_CANDIDATE_SET_NO_BACKFILL",
        "candidate_ids": [candidate_id],
    }


def test_manual_builder_accepts_typed_published_recovery_package_receipt(
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parents[1]
    registry = root / "assets/lidousha/recovery_publication_authority_2026-08-25_c4_c5_timeaxis.v1.json"
    candidate_id = "auto_113028_1271_1328"
    authority = build_recovery_publication_authorities(
        candidate_ids={candidate_id},
        registry_path=registry,
        expected_registry_sha256="sha256:" + _sha(registry),
        repo_root=root,
    )[candidate_id]
    pkg = _package(
        tmp_path,
        candidate_id=candidate_id,
        title=authority["observed_public_title"],
        stem=candidate_id,
    )
    record_path = pkg / f"{candidate_id}.record.json"
    publish_path = pkg / f"{candidate_id}.publish.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    publish = json.loads(publish_path.read_text(encoding="utf-8"))
    record["recovery_publication_authority"] = authority
    record["publish_staging"]["recovery_publication_authority"] = authority
    publish["recovery_publication_authority"] = authority
    record["artifact_hashes"].update({
        "chat_authority_audit_sha256": (
            "sha256:" + _sha(pkg / f"{candidate_id}.chat-authority.json")
        ),
        "clip_context_file_sha256": (
            "sha256:" + _sha(pkg / f"{candidate_id}.clip-context.json")
        ),
    })
    record_path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    publish_path.write_text(json.dumps(publish, ensure_ascii=False), encoding="utf-8")
    preflight = {
        "schema_version": "published-same-bv-recovery-preflight.v1",
        "candidate_id": candidate_id,
        "date": "2026-08-14",
        "state_transition": "none",
        "upload_allowed": False,
        "publication_allowed": False,
        "same_bv_only": True,
        "target": {
            "bvid": authority["bvid"],
            "aid": authority["aid"],
            "current_state_cid": authority["cid"],
            "title": authority["observed_public_title"],
        },
        "production_state": {
            "path": str((tmp_path / "runtime/state/2026-08-14.json").absolute()),
            "sha256": "sha256:" + "c" * 64,
        },
        "deployment_authority": {"deployed_commit": "a" * 40},
        "published_state_authority": {
            "candidate_id": candidate_id,
            "status": "published",
            "bvid": authority["bvid"],
            "aid": authority["aid"],
            "published_cid": authority["cid"],
            "reconciliation_status": "VERIFIED_PUBLIC",
            "reconciliation_cid": authority["cid"],
            "title": authority["observed_public_title"],
            "publication_authority_cid": authority["cid"],
            "predecessor_completed": None,
            "registry_sha256": STATE_AUTHORITY_SHA256,
            "entry_sha256": "sha256:" + "f" * 64,
        },
        "publication_authority_sha256": authority["authority_sha256"],
        "source_record_sha256": "sha256:" + "d" * 64,
    }
    preflight_path = pkg / f"{candidate_id}.published-recovery-preflight.json"
    preflight_path.write_text(json.dumps(preflight, ensure_ascii=False), encoding="utf-8")
    package_receipt = build_published_recovery_package_receipt(
        package_root=pkg,
        candidate_id=candidate_id,
        recovery_publication_authority=authority,
    )
    receipt_path = pkg / f"{candidate_id}.published-recovery-package-receipt.json"
    receipt_path.write_text(json.dumps(package_receipt, ensure_ascii=False), encoding="utf-8")

    manifest = build_manual(pkg, operator="op", note="published package-only recovery")
    item = manifest["items"][0]
    assert item["published_recovery_package_receipt"] == receipt_path.name
    assert item["published_recovery_package_receipt_sha256"] == "sha256:" + _sha(receipt_path)
    assert item["recovery_publication_authority"] == authority
    assert manifest["status"] == "finished_review_package_no_upload_pending_human_review"
    assert manifest["upload_allowed"] is False
