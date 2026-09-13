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
    assert record["publish_staging"]["cover_repair_binding"] == publish["cover_repair_binding"]
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


def test_bind_replaces_existing_top_level_generation_without_changing_history(tmp_path):
    # Some real producer records mirror current generation both here and in
    # publish_staging. Both are current surfaces, not an archival generation.
    pkg, _mp4 = _package(tmp_path)
    cover = _cover(tmp_path)
    record_path = pkg / "delivery.record.json"
    old = {"final_cover": "old-cover.png", "final_cover_sha256": "sha256:" + "a" * 64}
    record = json.loads(record_path.read_text())
    record.update(cover_generation=old, cover_path="old-cover.png",
                  cover_status="BLOCKED_AI_COVER_REQUIRED", cover_text="old caption")
    record["publish_staging"]["cover_generation"] = old
    record["previous_cover_generation"] = old
    record["cover_repair_history"] = [{"generation": old, "status": "FAIL"}]
    _write_json(record_path, record)
    generation_path = cover.with_suffix(".cover_generation.json")
    generation = json.loads(generation_path.read_text())
    generation["cover_text"] = "new caption"
    _write_json(generation_path, generation)

    result = bind_manual_package_cover(package_dir=pkg, cover=cover)
    current = json.loads(record_path.read_text())
    publish = json.loads((pkg / "delivery.publish.json").read_text())
    assert result["status"] == "BOUND"
    assert current["cover_generation"] == current["publish_staging"]["cover_generation"]
    assert current["cover_generation"] == publish["cover_generation"]
    assert current["cover_path"] == str(cover)
    assert current["cover_status"] == "AI_COVER_READY"
    assert current["cover_text"] == "new caption"
    assert current["previous_cover_generation"] == old
    assert current["cover_repair_history"] == [{"generation": old, "status": "FAIL"}]
    assert current["publish_staging"]["upload_enabled"] is False
    assert current["artifact_hashes"]["publish_draft_sha256"] == _sha(pkg / "delivery.publish.json")


def test_bind_does_not_add_legacy_top_level_generation_to_staging_only_record(tmp_path):
    pkg, _mp4 = _package(tmp_path)
    cover = _cover(tmp_path)
    bind_manual_package_cover(package_dir=pkg, cover=cover)
    current = json.loads((pkg / "delivery.record.json").read_text())
    assert "cover_generation" not in current
    assert "cover_path" not in current
    assert current["publish_staging"]["cover_generation"]["final_cover"] == str(cover)


def test_bad_generation_preserves_all_current_and_historical_record_surfaces(tmp_path):
    pkg, _mp4 = _package(tmp_path)
    cover = _cover(tmp_path)
    record_path = pkg / "delivery.record.json"
    record = json.loads(record_path.read_text())
    old = {"final_cover": "old-cover.png", "final_cover_sha256": "sha256:" + "a" * 64}
    record["cover_generation"] = old
    record["publish_staging"]["cover_generation"] = old
    record["previous_cover_generation"] = old
    _write_json(record_path, record)
    originals = {p: p.read_bytes() for p in pkg.iterdir()}
    cover.write_bytes(b"changed png without valid generation")
    with pytest.raises(ValueError, match="hash mismatch"):
        bind_manual_package_cover(package_dir=pkg, cover=cover)
    assert {p: p.read_bytes() for p in pkg.iterdir()} == originals
    assert not cover.with_suffix(".cover-binding.json").exists()


@pytest.mark.parametrize("entrypoint", ["manual", "runner"])
@pytest.mark.parametrize("occupied_kind", ["file", "malformed", "directory", "dangling_link"])
def test_occupied_cover_binding_rejected_before_enrichment(
    tmp_path: Path, monkeypatch, entrypoint: str, occupied_kind: str
) -> None:
    """The native write-capable preparation boundary must never run on a retry.

    The preparation double deliberately changes generation bytes, exposing the
    ordering defect without a provider, image render, or real delivery write.
    """
    from src.autoslice import cover_repair as repair

    package, media = _package(tmp_path)
    cover = _cover(tmp_path)
    generation_path = cover.with_suffix(".cover_generation.json")
    binding_path = cover.with_suffix(".cover-binding.json")
    if occupied_kind == "directory":
        binding_path.mkdir()
    elif occupied_kind == "dangling_link":
        binding_path.symlink_to(tmp_path / "missing-binding-target")
    else:
        binding_path.write_bytes(b'{"historical": true}\n' if occupied_kind == "file" else b'invalid old receipt')
    files = [generation_path, cover, media, package / "delivery.record.json",
             package / "delivery.publish.json"]
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_ino) for p in files}
    binding_before = binding_path.lstat()
    calls = []

    def write_generation():
        calls.append("write-capable preparation")
        generation = json.loads(generation_path.read_bytes())
        generation["synthetic_enrichment"] = "must never be written"
        _write_json(generation_path, generation)
        return generation

    if entrypoint == "manual":
        def enrich(**kwargs):
            return write_generation(), generation_path
        monkeypatch.setattr(repair, "_enrich_repaired_cover_generation", enrich)
        def invoke():
            return repair.bind_manual_package_cover(package_dir=package, cover=cover)
    else:
        def prepare(**kwargs):
            return write_generation(), generation_path, _sha(cover), _sha(media), []
        monkeypatch.setattr(repair._route_lineage, "prepare_active_cover_binding", prepare)
        monkeypatch.setattr(repair, "_active_song_delivery_manifest", lambda *a, **kw: None)
        def invoke():
            return repair._bind_repaired_cover(
                "2026-08-21", {"candidate_id": "manual_cand_1", "title": "unchanged title"},
                media, cover,
            )
    with pytest.raises(ValueError, match="immutable cover binding already exists"):
        invoke()
    assert calls == []
    assert {p: (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_ino) for p in files} == before
    after = binding_path.lstat()
    assert (after.st_mode, after.st_ino, after.st_mtime_ns) == (
        binding_before.st_mode, binding_before.st_ino, binding_before.st_mtime_ns,
    )


@pytest.mark.parametrize("entrypoint", ["manual", "runner"])
def test_binding_created_during_enrichment_still_refused(tmp_path: Path, monkeypatch, entrypoint: str) -> None:
    """An early check must not replace the established post-preparation recheck."""
    from src.autoslice import cover_repair as repair

    package, media = _package(tmp_path)
    cover = _cover(tmp_path)
    generation_path = cover.with_suffix(".cover_generation.json")
    binding_path = cover.with_suffix(".cover-binding.json")
    occupied = b"another writer's immutable binding"
    before = {p: p.read_bytes() for p in package.iterdir()}

    def enrich(**kwargs):
        binding_path.write_bytes(occupied)
        return json.loads(generation_path.read_bytes()), generation_path

    monkeypatch.setattr(repair, "_enrich_repaired_cover_generation", enrich)
    if entrypoint == "manual":
        with pytest.raises(ValueError, match="immutable cover binding already exists"):
            repair.bind_manual_package_cover(package_dir=package, cover=cover)
    else:
        def prepare(**kwargs):
            generation, path = enrich()
            return generation, path, _sha(cover), _sha(media), []
        monkeypatch.setattr(repair._route_lineage, "prepare_active_cover_binding", prepare)
        monkeypatch.setattr(repair, "_active_song_delivery_manifest", lambda *a, **kw: None)
        with pytest.raises(ValueError, match="immutable cover binding already exists"):
            repair._bind_repaired_cover("2026-08-21", {"candidate_id": "manual_cand_1"}, media, cover)
    assert binding_path.read_bytes() == occupied
    assert {p: p.read_bytes() for p in package.iterdir()} == before
