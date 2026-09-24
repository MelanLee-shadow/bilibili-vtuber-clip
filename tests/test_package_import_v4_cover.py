"""V4 cover import uses bound in-package images, never producer path guesses."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from src.autoslice import package_import as pi
from src.autoslice.package_import_cover import project_preserved_cover_locators
from tests.test_host_only_identity_card_successor import _run, _inventory
from tests.test_host_only_identity_card_successor import ready_input as ready_input  # noqa: F401


@pytest.fixture
def package(request):
    f = request.getfixturevalue("ready_input")
    _run(f)
    root = f["destination"]
    manifest_path = root / "review_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    item = manifest["items"][0]
    item["title"] = json.loads((root / item["publish_json"]).read_text())["title"]
    old_root = Path("/producer/out/2026-08-22") / f["candidate"] / "replacement_recuts"
    for key in ("record", "evidence_json"):
        p = root / item[key]
        doc = json.loads(p.read_text())
        doc["subtitle_path"] = str(old_root / (f["candidate"] + ".recut.srt"))
        p.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n")
        if key in item["sha256"]:
            item["sha256"][key] = pi.sha256_file(p)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    rec = json.loads((root / item["evidence_json"]).read_text())
    pub = json.loads((root / item["publish_json"]).read_text())
    return root, item, rec, pub, old_root, f["candidate"]


def test_bound_v4_paths_project_without_touching_inputs(package):
    root, item, rec, pub, old_root, cid = package
    before = _inventory(root)
    rec_before, pub_before = copy.deepcopy(rec), copy.deepcopy(pub)
    r, p = project_preserved_cover_locators(rec, pub, package_root=root, candidate_id=cid)
    assert p["cover_generation"]["final_cover"] == str(old_root / item["cover"])
    assert p["cover_generation"]["ai_background"] == str(old_root / item["cover_route_background"])
    assert p["cover_generation"]["rendered_text_pixels"]["mask_path"] == str(
        old_root / item["cover_title_mask"]
    )
    assert r["publish_staging"]["cover_generation"] == p["cover_generation"]
    for key in (
        "final_host_identity_verification",
        "source_composition_verification",
        "route_decision",
        "identity_card_pixel_successor",
        "story_contract",
    ):
        assert p["cover_generation"].get(key) == pub["cover_generation"].get(key)
    assert rec == rec_before and pub == pub_before and _inventory(root) == before


def _strip_identity_card_marker(root: Path) -> tuple[dict, dict, dict]:
    manifest_path = root / "review_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    item = manifest["items"][0]
    for kind in ("evidence_json", "record", "publish_json"):
        path = root / item[kind]
        document = json.loads(path.read_text(encoding="utf-8"))
        generation = (
            document.get("cover_generation")
            if kind == "publish_json"
            else document.get("publish_staging", {}).get("cover_generation")
        )
        assert isinstance(generation, dict)
        generation.pop("identity_card_pixel_successor", None)
        path.write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        if kind in item.get("sha256", {}):
            item["sha256"][kind] = pi.sha256_file(path)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    record = json.loads((root / item["evidence_json"]).read_text(encoding="utf-8"))
    publish = json.loads((root / item["publish_json"]).read_text(encoding="utf-8"))
    return item, record, publish


def test_generic_host_only_v4_dispatches_without_identity_card_marker(package):
    root, _item, _record, _publish, old_root, cid = package
    before = _inventory(root)
    item, record, publish = _strip_identity_card_marker(root)
    normalized_before = _inventory(root)
    record_before, publish_before = copy.deepcopy(record), copy.deepcopy(publish)

    projected_record, projected_publish = project_preserved_cover_locators(
        record,
        publish,
        package_root=root,
        candidate_id=cid,
    )

    assert projected_publish["cover_generation"]["final_cover"] == str(
        old_root / item["cover"]
    )
    assert projected_publish["cover_generation"]["ai_background"] == str(
        old_root / item["cover_route_background"]
    )
    assert projected_record["publish_staging"]["cover_generation"] == (
        projected_publish["cover_generation"]
    )
    assert record == record_before and publish == publish_before
    assert _inventory(root) == normalized_before
    assert before != normalized_before  # Test setup changed only the package JSON bytes.


def test_generic_host_only_v4_without_package_binding_fails_closed(package):
    root, _item, _record, _publish, _old_root, cid = package
    item, record, publish = _strip_identity_card_marker(root)
    manifest_path = root / "review_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["items"][0].pop("host_only_v4_binding")
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(pi.PackageImportError, match="SOURCE_ROOT_INCONSISTENT"):
        project_preserved_cover_locators(
            record,
            publish,
            package_root=root,
            candidate_id=cid,
        )


def test_native_cover_resolver_uses_v4_relative_cover_not_parent_basename(package):
    root, item, rec, pub, old_root, cid = package
    g = pub["cover_generation"]
    assert pi._package_internal_cover(root, g) == root / item["cover"]


@pytest.mark.parametrize(
    "drift",
    [
        "manifest_candidate",
        "manifest_title",
        "record_hash",
        "receipt_bytes",
        "cover_bytes",
        "mask_bytes",
        "path_escape",
        "symlink",
        "raw_input",
        "source_namespace",
    ],
)
def test_bound_v4_projection_rejects_drift(package, drift, tmp_path):
    root, item, rec, pub, old_root, cid = package
    mpath = root / "review_manifest.json"
    m = json.loads(mpath.read_text())
    if drift == "manifest_candidate":
        m["items"][0]["candidate_id"] = "different"
    elif drift == "manifest_title":
        m["items"][0]["title"] = "different"
    elif drift == "record_hash":
        m["items"][0]["sha256"]["evidence_json"] = "0" * 64
    elif drift == "path_escape":
        m["items"][0]["cover_title_mask"] = "../outside.png"
    elif drift in ("receipt_bytes", "cover_bytes", "mask_bytes", "symlink"):
        rel = {
            "receipt_bytes": item["host_only_v4_binding"]["receipt_path"],
            "cover_bytes": item["cover"],
            "mask_bytes": item["cover_title_mask"],
            "symlink": item["cover_title_mask"],
        }[drift]
        path = root / rel
        if drift == "symlink":
            target = tmp_path / "outside.png"
            target.write_bytes(path.read_bytes())
            path.unlink()
            path.symlink_to(target)
        else:
            path.write_bytes(b"synthetic damaged input")
    elif drift == "raw_input":
        rec["extra"] = "changed outside frozen source"
    elif drift == "source_namespace":
        rec["subtitle_path"] = "/producer/wrong/other.recut.srt"
    mpath.write_text(json.dumps(m))
    with pytest.raises(pi.PackageImportError, match="SOURCE_ROOT_INCONSISTENT"):
        project_preserved_cover_locators(rec, pub, package_root=root, candidate_id=cid)


def test_v4_flat_cover_is_contained_and_hash_bound(tmp_path):
    cover = tmp_path / "final.cover.png"
    cover.write_bytes(b"synthetic image bytes")
    g = {
        "final_cover": "/producer/pixel-bridge/final.cover.png",
        "final_cover_sha256": "sha256:" + pi.sha256_file(cover),
        "final_host_identity_verification": {
            "schema_version": "lidousha-cover-final-host-identity-verification.v4",
            "final_cover_path": cover.name,
        },
    }
    assert pi._package_internal_cover(tmp_path, g) == cover
    cover.write_bytes(b"damaged")
    with pytest.raises(pi.PackageImportError, match="DECLARED_ARTIFACT_SHA_DRIFT"):
        pi._package_internal_cover(tmp_path, g)
    g["final_host_identity_verification"]["final_cover_path"] = "../outside.png"
    with pytest.raises(pi.PackageImportError):
        pi._package_internal_cover(tmp_path, g)


def test_same_source_destination_guard_remains_active(package):
    root, item, rec, pub, old_root, cid = package
    r, p = project_preserved_cover_locators(rec, pub, package_root=root, candidate_id=cid)
    with pytest.raises(pi.PackageImportError, match="SOURCE_IS_DESTINATION"):
        pi.derive_source_roots(
            record=r,
            publish=p,
            speaker=None,
            candidate_id=cid,
            destination_package_root=old_root,
            destination_repo_root=Path("/producer/repo"),
        )


def test_input_drift_during_final_readback_is_rejected(package, monkeypatch):
    from src.autoslice import package_import_v4_cover as projection

    root, item, rec, pub, old_root, cid = package
    reader = projection.read_package_file_once
    counts = {}

    def changing_read(package_root, relative, *, label):
        rel, data = reader(package_root, relative, label=label)
        name = rel.as_posix()
        counts[name] = counts.get(name, 0) + 1
        if name == "review_manifest.json" and counts[name] > 1:
            data += b"\n"
        return rel, data

    monkeypatch.setattr(projection, "read_package_file_once", changing_read)
    with pytest.raises(pi.PackageImportError, match="changed during normalization"):
        project_preserved_cover_locators(rec, pub, package_root=root, candidate_id=cid)


def test_projected_v4_source_namespace_resolves_flat_package_alias(tmp_path):
    cover_name = "auto_010203_4_5.recut.burned-final-sapphire72.cover.png"
    cover = tmp_path / cover_name
    cover.write_bytes(b"flat package cover bytes")
    generation = {
        "final_cover": (
            "/producer/out/2026-08-22/auto_010203_4_5/"
            f"replacement_recuts/{cover_name}"
        ),
        "final_cover_sha256": "sha256:" + pi.sha256_file(cover),
        "final_host_identity_verification": {
            "schema_version": "lidousha-cover-final-host-identity-verification.v4",
            "final_cover_path": "/producer/design/B2-cover.png",
        },
    }

    assert pi._package_internal_cover(tmp_path, generation) == cover
