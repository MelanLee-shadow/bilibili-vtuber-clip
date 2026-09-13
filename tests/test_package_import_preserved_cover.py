"""Reuse full native cover proof before projecting only portable locators."""

import copy
import json
import shutil
from pathlib import Path

import pytest

from scripts import session_autoslice as runner
from src.autoslice import package_import as pi
from src.autoslice.package_import_cover import project_preserved_cover_locators
from tests.test_cover_pixel_preservation import _fixture, _invoke


def _bound(tmp_path, monkeypatch, direct=False):
    fx, input_record, input_path, _ = _fixture(tmp_path, monkeypatch, direct)
    P = fx["source_record"].parent
    cid = fx["cid"]
    media = P / f"{cid}.recut.burned-final-sapphire72.mp4"
    media.write_bytes(fx["mp4"].read_bytes())
    cover = media.with_suffix(".cover.png")
    cover.write_bytes(fx["cover"].read_bytes())
    rec = json.loads(fx["source_record"].read_bytes())
    g = input_record["publish_staging"]["cover_generation"]
    rec.update(subtitle_path=str(P / f"{cid}.recut.srt"), burned_preview={"path": str(media)})
    rec["publish_staging"].update(cover_generation=g, status="STAGED", cover_path=g["final_cover"])
    pub = json.loads(fx["publish_path"].read_bytes())
    pub.update(cover_generation=g, cover_path=g["final_cover"], video_path=str(media))
    fx["publish_path"].write_bytes(pi.json_bytes(pub))
    rec["artifact_hashes"]["publish_draft_sha256"] = "sha256:" + pi.sha256_file(fx["publish_path"])
    for path in [fx["source_record"], media.with_suffix(".record.json"), input_path]:
        path.write_bytes(pi.json_bytes(rec))
    out = fx["generated_cover"].parent.parent / "preserved" / "final.cover.png"
    assert _invoke(monkeypatch, fx, input_path, out) == 0
    bound_row = {"candidate_id": cid, "title": fx["title"], "cover_generation": g}
    runner._bind_repaired_cover(fx["date"], bound_row, media, cover, out)
    assert runner._cover_binding_valid(fx["date"], bound_row, media, cover)
    record = json.loads(fx["source_record"].read_bytes())
    publish = json.loads(fx["publish_path"].read_bytes())
    new = publish["cover_generation"]
    item = {"candidate_id": cid, "title": fx["title"]}
    specs = [
        ("covers/final.cover.png", new["final_cover"], None),
        (
            "covers_ai_original/" + Path(new["pre_overlay_path"]).name,
            new["pre_overlay_path"],
            "cover_pre_overlay",
        ),
        (
            "covers_ai_original/" + Path(new["ai_background"]).name,
            new["ai_background"],
            "cover_route_background",
        ),
        (
            "covers_ai_original/" + Path(new["rendered_text_pixels"]["mask_path"]).name,
            new["rendered_text_pixels"]["mask_path"],
            "cover_title_mask",
        ),
        ("cover_refs/" + Path(new["reference_image"]).name, new["reference_image"], None),
    ]
    for relative, original, key in specs:
        path = P / relative
        path.parent.mkdir(exist_ok=True)
        shutil.copyfile(original, path)
        if key:
            item[key] = relative
    (P / "review_manifest.json").write_bytes(pi.json_bytes({"items": [item]}))
    return fx, P, record, publish, item


@pytest.mark.parametrize("direct", [False, True])
def test_projects_real_bound_cover_without_changing_old_witnesses(tmp_path, monkeypatch, direct):
    fx, P, record, publish, item = _bound(tmp_path, monkeypatch, direct)
    original_r, original_p = copy.deepcopy(record), copy.deepcopy(publish)
    before = {p: p.read_bytes() for p in P.rglob("*") if p.is_file()}
    # The native binding must use its explicit source root, not process-global BASE.
    monkeypatch.setattr(runner, "BASE", tmp_path / "different-runtime")
    r, p = project_preserved_cover_locators(record, publish, package_root=P, candidate_id=fx["cid"])
    assert p["cover_generation"]["final_cover"] == str(P / "covers/final.cover.png")
    assert r["publish_staging"]["cover_generation"] == p["cover_generation"]
    assert r["publish_staging"]["cover_path"] == p["cover_path"]
    for key in [
        "polish_face_verification",
        "final_host_identity_verification",
        "route_decision",
        "pixel_preserving_successor",
    ]:
        assert p["cover_generation"][key] == publish["cover_generation"][key]
    assert r["cover_repair_binding"] == record["cover_repair_binding"]
    assert r["artifact_hashes"] == record["artifact_hashes"]
    assert record == original_r and publish == original_p
    assert before == {p: p.read_bytes() for p in P.rglob("*") if p.is_file()}
    assert project_preserved_cover_locators(r, p, package_root=P, candidate_id=fx["cid"]) == (r, p)


@pytest.mark.parametrize(
    "drift",
    [
        "source_record",
        "source_publish",
        "binding_hash",
        "binding_bytes",
        "media_bytes",
        "final_alias",
        "mask_alias",
        "missing_reference",
        "role_escape",
        "alias_symlink",
        "wrong_title",
        "wrong_candidate",
    ],
)
def test_rejects_invalid_source_or_portable_alias(tmp_path, monkeypatch, drift):
    fx, P, record, publish, item = _bound(tmp_path, monkeypatch)
    if drift == "source_record":
        fx["source_record"].write_text("{}")
    elif drift == "source_publish":
        fx["publish_path"].write_text("{}")
    elif drift == "binding_hash":
        record["cover_repair_binding"]["sha256"] = "sha256:" + "0" * 64
    elif drift == "binding_bytes":
        Path(record["cover_repair_binding"]["path"]).write_text("{}")
    elif drift == "media_bytes":
        Path(record["burned_preview"]["path"]).write_bytes(b"other media")
    elif drift == "final_alias":
        (P / "covers/final.cover.png").write_bytes(b"other cover")
    elif drift == "mask_alias":
        (P / item["cover_title_mask"]).write_bytes(b"other mask")
    elif drift == "missing_reference":
        (P / "cover_refs" / Path(publish["cover_generation"]["reference_image"]).name).unlink()
    elif drift == "role_escape":
        item["cover_pre_overlay"] = "../escape.png"
        (P / "review_manifest.json").write_bytes(pi.json_bytes({"items": [item]}))
    elif drift == "alias_symlink":
        path = P / "covers/final.cover.png"
        path.unlink()
        path.symlink_to(Path(publish["cover_generation"]["final_cover"]))
    elif drift == "wrong_title":
        publish["title"] = "wrong"
    else:
        fx["cid"] = "wrong"
    with pytest.raises(pi.PackageImportError, match="SOURCE_ROOT_INCONSISTENT"):
        project_preserved_cover_locators(record, publish, package_root=P, candidate_id=fx["cid"])


def test_unrelated_generation_is_not_normalized(tmp_path):
    record = {"x": 1}
    publish = {"cover_generation": {"final_cover": "/outside/not-approved.png"}}
    r, p = project_preserved_cover_locators(
        record, publish, package_root=tmp_path, candidate_id="a"
    )
    assert r is record and p is publish


@pytest.mark.parametrize("drift", [False, True])
def test_staged_copy_replays_source_binding_and_checks_its_own_alias(tmp_path, monkeypatch, drift):
    fx, P, record, publish, _ = _bound(tmp_path, monkeypatch)
    staged = tmp_path / "transport-copy" / "replacement_recuts"
    shutil.copytree(P, staged)
    original_record = (P / f"{fx['cid']}.record.json").read_bytes()
    original_publish = (P / f"{fx['cid']}.recut.publish.json").read_bytes()
    if drift:
        (staged / "covers/final.cover.png").write_bytes(b"transport drift")
        with pytest.raises(pi.PackageImportError, match="SOURCE_ROOT_INCONSISTENT"):
            project_preserved_cover_locators(record, publish, package_root=staged, candidate_id=fx["cid"])
    else:
        r, p = project_preserved_cover_locators(record, publish, package_root=staged, candidate_id=fx["cid"])
        # Normalization still names the frozen producer root. The subsequent
        # native relocation owns projection onto its exact destination.
        assert p["cover_generation"]["final_cover"] == str(P / "covers/final.cover.png")
        assert r["cover_repair_binding"] == record["cover_repair_binding"]
    assert (P / f"{fx['cid']}.record.json").read_bytes() == original_record
    assert (P / f"{fx['cid']}.recut.publish.json").read_bytes() == original_publish


def test_source_binding_pointer_is_history_not_a_rewritable_locator():
    from src.autoslice.package_relocation_contract import reject_unknown_wsl_paths

    pointer = {"path": "/producer/old.cover-binding.json", "sha256": "sha256:" + "1" * 64}
    for kind, document in [
        ("record", {"cover_repair_binding": pointer, "publish_staging": {"cover_repair_binding": pointer}}),
        ("publish", {"cover_repair_binding": pointer}),
    ]:
        frozen = copy.deepcopy(document)
        reject_unknown_wsl_paths(document, kind=kind, source_workspace_root="/producer")
        pi._guard_immutable(kind, frozen, document, source_workspace_root="/producer")
        changed = copy.deepcopy(document)
        changed["cover_repair_binding"]["path"] = "/destination/rewritten.json"
        with pytest.raises(pi.PackageImportError, match="IMMUTABLE_EVIDENCE_CHANGED"):
            pi._guard_immutable(kind, frozen, changed, source_workspace_root="/producer")
