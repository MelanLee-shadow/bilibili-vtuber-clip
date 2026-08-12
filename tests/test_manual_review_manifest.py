"""手动产线包的 review manifest：hash-bound 自真实产物 + 显式操作者署名。"""

import hashlib
import json
from pathlib import Path

import pytest

from scripts.build_manual_review_manifest import DailyManifestError, build_manual
from src.autoslice.addressee_attribution import rebuild_speaker_evidence
from src.autoslice.review_evidence import SourceCue
from src.autoslice.speaker_common import SPEAKER_FINALIZATION_SCHEMA
from src.autoslice.source_fact_review import review_and_repair_source_facts

TITLE = "【李豆沙】手动包审计闭环用例标题够长了"
HOOK = "小李当场语塞三秒，弹幕全体起立。"
TRANSCRIPT = "当场语塞了三秒，弹幕全体起立"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _receipt(
    *,
    speaker_evidence: dict[str, object] | None = None,
    speaker_transcript: str | None = None,
) -> dict:
    def cpa(_prompt: str) -> str:
        return json.dumps(
            {
                "schema_version": "lidousha-source-fact-review.v1",
                "status": "KEEP",
                "final_selection_hook": HOOK,
                "final_title": TITLE,
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
        title=TITLE,
        final_transcript=TRANSCRIPT,
        clip_context_prompt="",
        llm_call=cpa,
        speaker_evidence=speaker_evidence,
        speaker_transcript=speaker_transcript,
    )
    assert review["status"] == "PASS"
    return review


def _package(tmp_path: Path, *, speaker_finalized: bool = False) -> Path:
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    stem = "手动闭环用例"

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
        "candidate_id": "manual_cand_9",
        **speaker_record_fields,
        "story_contract": story_contract,
        "publish_staging": {
            "title": TITLE,
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
        "candidate_id": "manual_cand_9",
        "title": TITLE,
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
