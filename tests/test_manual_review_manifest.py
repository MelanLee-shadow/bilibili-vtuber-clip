"""手动产线包的 review manifest：hash-bound 自真实产物 + 显式操作者署名。"""

import hashlib
import json
from pathlib import Path

import pytest

from scripts.build_manual_review_manifest import DailyManifestError, build_manual
from src.autoslice.source_fact_review import review_and_repair_source_facts

TITLE = "【李豆沙】手动包审计闭环用例标题够长了"
HOOK = "小李当场语塞三秒，弹幕全体起立。"
TRANSCRIPT = "当场语塞了三秒，弹幕全体起立"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _receipt() -> dict:
    def cpa(_prompt: str) -> str:
        return json.dumps(
            {
                "schema_version": "lidousha-source-fact-review.v1",
                "status": "KEEP",
                "final_selection_hook": HOOK,
                "final_title": TITLE,
                "supported_by": ["final_transcript"],
                "changed_surfaces": [],
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
    )
    assert review["status"] == "PASS"
    return review


def _package(tmp_path: Path) -> Path:
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    stem = "手动闭环用例"
    receipt = _receipt()

    def w(name: str, payload: bytes) -> Path:
        path = pkg / name
        path.write_bytes(payload)
        return path

    mp4 = w(f"{stem}.mp4", b"burned-final-bytes")
    srt = w(
        f"{stem}.srt",
        f"1\n00:00:00,000 --> 00:00:02,000\n{TRANSCRIPT}\n".encode("utf-8"),
    )
    w(f"{stem}.final-sapphire72.ass", b"ass-bytes")
    w(f"{stem}.chat-authority.json", b"{}")
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
        "story_contract": story_contract,
        "publish_staging": {
            "title": TITLE,
            "upload_enabled": False,
            "source_fact_review": receipt,
        },
        "artifact_hashes": {"video_sha256": "sha256:" + _sha(mp4)},
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
