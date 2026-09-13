"""Readiness must inspect the same final cover as native joint-QC consumption."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from src.autoslice.publication_readiness import (
    NEEDS_PROVIDER, READY_TO_PREPARE, STATE_DRIFT, build_readiness_graph,
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def _fixture(tmp: Path) -> dict:
    cid, date, title = "cover-copy", "2026-08-20", "A complete fixture title"
    repo, runtime = tmp / "repo", tmp / "runtime"
    (repo / "assets/lidousha").mkdir(parents=True)
    (runtime / "state").mkdir(parents=True)
    (runtime / "reports").mkdir()
    (runtime / "reports/upload_ledger.jsonl").write_text("")
    root = runtime / "out" / date / cid / "replacement_recuts"
    (root / "covers").mkdir(parents=True)
    media, srt, ass, burn = (root / (cid + suffix) for suffix in (
        ".recut.mp4", ".recut.srt", ".recut.ass", ".burn.mp4"))
    generated, cover = root / "covers/generation.png", burn.with_suffix(".cover.png")
    for path, raw in ((media, b"media"), (srt, b"subtitle"), (ass, b"ASS"),
                      (burn, b"final-video"), (generated, b"same-image"), (cover, b"same-image")):
        path.write_bytes(raw)
    hashes = {key: "sha256:" + _sha(path) for key, path in (
        ("video_sha256", media), ("subtitle_sha256", srt), ("ass_sha256", ass),
        ("burned_video_sha256", burn), ("cover_sha256", generated))}
    record, delivery = root / (cid + ".record.json"), burn.with_suffix(".record.json")
    publish, manifest = root / (cid + ".publish.json"), root / "review_manifest.json"
    document = {"candidate_id": cid, "recording_date": date, "media_path": str(media),
                "subtitle_path": str(srt), "subtitle_ass_path": str(ass),
                "burned_video_path": str(burn), "artifact_hashes": hashes,
                "story_contract": {"candidate_id": cid, "source_fact_review": {"status": "PASS"}},
                "publish_staging": {"publish_json_path": str(publish)}}
    _write(record, document)
    delivery.write_bytes(record.read_bytes())
    _write(publish, {"candidate_id": cid, "title": title, "artifact_hashes": hashes,
                     "cover_generation": {"final_cover": str(generated),
                                          "final_cover_sha256": hashes["cover_sha256"]}})
    item = {"candidate_id": cid, "title": title, "video": burn.name, "cover": cover.name,
            "record": delivery.name, "evidence_json": record.name, "publish_json": publish.name,
            "sha256": {"evidence_json": _sha(delivery)}}
    _write(manifest, {"schema_version": "lidousha-daily-review-manifest.v1",
                      "candidate_id": cid, "items": [item]})
    qc = root / (cid + ".title-cover-joint-qc.json")
    verdict = {"lidousha_primary": True, "thumbnail_readable": True, "single_clear_hook": True,
               "text_overcrowded": False, "title_cover_aligned": True, "physical_text_line_count": 2,
               "unrelated_or_misleading_elements": [], "pass": True, "reason": "Fixture only"}
    _write(qc, {"schema_version": "lidousha-title-cover-joint-qc.v1", "candidate_id": cid,
                "title": title, "title_sha256": "sha256:" + hashlib.sha256(title.encode()).hexdigest(),
                "cover_path": str(cover), "cover_sha256": hashes["cover_sha256"],
                "preferred_provider": "cpa", "selected_provider": "cpa",
                "witness": {"schema_version": "cpa-frame-witness.v1", "provider": "cpa",
                            "status": "OBSERVED", "model": "fixture", "image_path": str(cover),
                            "image_sha256": hashes["cover_sha256"], "answer": json.dumps(verdict)},
                "verdict": verdict, "status": "PASS", "pass": True})
    _write(runtime / "state" / (date + ".json"), {
        "picks": [{"candidate_id": cid, "status": "review_ready", "rc": 0}]})
    return dict(repo=repo, runtime=runtime, root=root, manifest=manifest, qc=qc,
                cover=cover, generated=generated, publish=publish, burn=burn,
                record=record, delivery=delivery, cid=cid)


def _inspect(f: dict) -> dict:
    return build_readiness_graph(
        repository_root=f["repo"], runtime_root=f["runtime"],
        registry_loader=lambda *_args, **_kwargs: {"entries": []},
    )["rows"][0]


def test_graph_uses_frozen_final_cover_without_requesting_a_new_qc(tmp_path: Path, monkeypatch) -> None:
    from scripts import run_title_cover_joint_qc as native
    f = _fixture(tmp_path)
    before = {str(p): _sha(p) for p in tmp_path.rglob("*") if p.is_file()}
    monkeypatch.setattr(native, "image_vision_probe", lambda *_a, **_kw: pytest.fail("new model call"))
    row = _inspect(f)
    assert row["category"] == READY_TO_PREPARE, row
    assert row["package_dependencies"]["cover"] == str(f["cover"])
    assert row["package_dependencies"]["title_cover_qc"] == str(f["qc"])
    assert row["observational_only"] is True  # No upload manifest was created or approved.
    assert before == {str(p): _sha(p) for p in tmp_path.rglob("*") if p.is_file()}


@pytest.mark.parametrize("defect", ["missing", "bytes", "symlink", "cover-name", "candidate",
                                     "video", "publish", "title", "duplicate-manifest"])
def test_invalid_final_manifest_never_falls_back_to_generation_cover(tmp_path: Path, defect: str) -> None:
    f = _fixture(tmp_path)
    manifest = json.loads(f["manifest"].read_bytes())
    if defect == "missing":
        f["cover"].unlink()
    elif defect == "bytes":
        f["cover"].write_bytes(b"different final image")
    elif defect == "symlink":
        f["cover"].unlink()
        f["cover"].symlink_to(f["generated"])
    elif defect == "cover-name":
        manifest["items"][0]["cover"] = "covers/generation.png"
    elif defect == "candidate":
        manifest["items"][0]["candidate_id"] = "another-candidate"
    elif defect == "video":
        extra = f["root"] / "other.mp4"
        extra.write_bytes(f["burn"].read_bytes())
        manifest["items"][0]["video"] = extra.name
    elif defect == "publish":
        extra = f["root"] / "other.json"
        extra.write_bytes(f["publish"].read_bytes())
        manifest["items"][0]["publish_json"] = extra.name
    elif defect == "title":
        manifest["items"][0]["title"] = "another title"
    else:
        folder = f["root"] / "extra"
        folder.mkdir()
        (folder / "review_manifest.json").write_bytes(f["manifest"].read_bytes())
    _write(f["manifest"], manifest)
    row = _inspect(f)
    assert row["category"] == STATE_DRIFT, row
    assert any(code.startswith("PACKAGE_") for code in row["reason_codes"])


def test_old_generation_bound_qc_is_not_relabelled_as_final_qc(tmp_path: Path) -> None:
    f = _fixture(tmp_path)
    receipt = json.loads(f["qc"].read_bytes())
    receipt["cover_path"] = str(f["generated"])
    receipt["witness"]["image_path"] = str(f["generated"])
    _write(f["qc"], receipt)
    before = f["qc"].read_bytes()
    row = _inspect(f)
    assert row["category"] == NEEDS_PROVIDER
    assert "COVER_QC_MISSING" in row["reason_codes"]
    assert f["qc"].read_bytes() == before
