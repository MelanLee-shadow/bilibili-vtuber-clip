"""手动产线包的封面回写：同一套校验/回执，失败包保持原样。"""

import hashlib
import json
from pathlib import Path

import pytest

from src.autoslice.cover_repair import bind_manual_package_cover


def _sha(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _package(tmp_path: Path, *, cid: str = "manual_cand_1", title: str = "【主播】手动包封面回写用例") -> tuple[Path, Path]:
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    mp4 = pkg / "delivery.mp4"
    mp4.write_bytes(b"fake-media-bytes")
    publish = {
        "schema_version": "shadow-publish-draft.v1",
        "candidate_id": cid,
        "title": title,
        "upload_enabled": False,
        "cover_status": "BLOCKED_AI_COVER_REQUIRED",
        "cover_path": None,
        "reason_codes": ["SCREENSHOT_ROUTE_MATERIALIZATION_FAILED"],
        "artifact_hashes": {"video_sha256": _sha(mp4)},
    }
    _write_json(pkg / "delivery.publish.json", publish)
    record = {
        "schema_version": "delivery-record.v1",
        "candidate_id": cid,
        "publish_staging": {"title": title, "upload_enabled": False},
        "artifact_hashes": {"video_sha256": _sha(mp4)},
    }
    _write_json(pkg / "delivery.record.json", record)
    return pkg, mp4


def _cover(tmp_path: Path, *, cid: str = "manual_cand_1", title: str = "【主播】手动包封面回写用例") -> Path:
    cover = tmp_path / "cover.png"
    cover.write_bytes(b"png-bytes")
    aux = {}
    for name in ("ai_bg.png", "reference.png", "request.json", "response.json"):
        p = tmp_path / name
        p.write_bytes(b"aux-" + name.encode())
        aux[name] = p
    manifest = {
        "workflow": "regenerate_channel_cover",
        "status": "AI_COVER_READY",
        "method": "images.edit",
        "image_gen_model": "cpa",
        "fallback_used": False,
        "candidate_id": cid,
        "title": title,
        "model": "gpt-image-2",
        "attempted_models": ["gpt-image-2"],
        "model_fallback_used": False,
        "final_cover": str(cover),
        "final_cover_sha256": _sha(cover),
        "ai_background": str(aux["ai_bg.png"]),
        "ai_background_sha256": _sha(aux["ai_bg.png"]),
        "reference_image": str(aux["reference.png"]),
        "reference_sha256": _sha(aux["reference.png"]),
        "request_path": str(aux["request.json"]),
        "request_sha256": _sha(aux["request.json"]),
        "response_path": str(aux["response.json"]),
        "response_sha256": _sha(aux["response.json"]),
    }
    _write_json(cover.with_suffix(".cover_generation.json"), manifest)
    return cover


def test_bind_updates_publish_and_record_with_binding_receipt(tmp_path: Path) -> None:
    pkg, _mp4 = _package(tmp_path)
    cover = _cover(tmp_path)

    result = bind_manual_package_cover(package_dir=pkg, cover=cover)

    assert result["status"] == "BOUND"
    publish = json.loads((pkg / "delivery.publish.json").read_text())
    assert publish["cover_status"] == "AI_COVER_READY"
    assert publish["cover_path"] == str(cover)
    assert publish["upload_enabled"] is False
    assert publish["artifact_hashes"]["cover_sha256"] == _sha(cover)
    assert "SCREENSHOT_ROUTE_MATERIALIZATION_FAILED" not in publish["reason_codes"]
    binding = json.loads(Path(result["binding_path"]).read_text())
    assert binding["schema_version"] == "lidousha-cover-repair-binding.v1"
    assert binding["authority_type"] == "talk_delivery_record"
    assert binding["upload_enabled"] is False
    record = json.loads((pkg / "delivery.record.json").read_text())
    assert record["publish_staging"]["cover_status"] == "AI_COVER_READY"
    assert (
        record["artifact_hashes"]["publish_draft_sha256"]
        == result["publish_draft_sha256"]
    )
    on_disk_publish_sha = "sha256:" + hashlib.sha256(
        (pkg / "delivery.publish.json").read_bytes()
    ).hexdigest()
    assert on_disk_publish_sha == result["publish_draft_sha256"]


def test_video_hash_mismatch_leaves_package_untouched(tmp_path: Path) -> None:
    pkg, mp4 = _package(tmp_path)
    cover = _cover(tmp_path)
    before = (pkg / "delivery.publish.json").read_bytes()
    mp4.write_bytes(b"tampered-media")

    with pytest.raises(ValueError, match="video hash"):
        bind_manual_package_cover(package_dir=pkg, cover=cover)

    assert (pkg / "delivery.publish.json").read_bytes() == before
    assert not cover.with_suffix(".cover-binding.json").exists()


def test_existing_binding_is_immutable(tmp_path: Path) -> None:
    pkg, _mp4 = _package(tmp_path)
    cover = _cover(tmp_path)
    bind_manual_package_cover(package_dir=pkg, cover=cover)
    with pytest.raises(ValueError, match="immutable cover binding"):
        bind_manual_package_cover(package_dir=pkg, cover=cover)


def test_generation_title_binding_mismatch_refused(tmp_path: Path) -> None:
    pkg, _mp4 = _package(tmp_path)
    cover = _cover(tmp_path, title="【主播】另一条标题")
    with pytest.raises(ValueError, match="binding mismatch"):
        bind_manual_package_cover(package_dir=pkg, cover=cover)
    publish = json.loads((pkg / "delivery.publish.json").read_text())
    assert publish["cover_status"] == "BLOCKED_AI_COVER_REQUIRED"
