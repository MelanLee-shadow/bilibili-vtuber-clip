import hashlib
import json
from pathlib import Path

import pytest

from src.autoslice.published_cover_carry import (
    ASSET_RELATIVE_PATH,
    MARKER_SCHEMA,
    PublishedCoverCarryError,
    materialized_marker,
    validate_materialized_marker,
)


def test_qixi_release_carry_authority_pins_public_8c_cover_not_r1_diagnostic() -> None:
    """The manual-release truth may never consume the failed r1 9a6 cover."""

    root = Path(__file__).resolve().parents[1]
    authority = json.loads((root / ASSET_RELATIVE_PATH).read_text(encoding="utf-8"))
    entry = authority["entries"][0]
    assert entry["candidate_id"] == "auto_113022_354_496"
    assert entry["artifacts"]["cover"]["sha256"] == (
        "sha256:8cacaafc502a31474f93e6472c945f8527dee23b5fdb39df27d672ee08f4d4e0"
    )
    assert "9a6a67b207825fbee4b367cf6dae50fa969798440c6b5e4ae522c15e9e51d5eb" not in json.dumps(
        authority, ensure_ascii=False, sort_keys=True
    )


def test_materialized_marker_rejects_unvalidated_generation_even_with_target_hashes(tmp_path: Path) -> None:
    cover = tmp_path / "out/2026-08-17/auto_x/replacement_recuts/covers/auto_x.published-carry.cover.png"
    generation = cover.with_name("auto_x.published-cover-generation.json")
    cover.parent.mkdir(parents=True)
    cover.write_bytes(b"cover")
    generation.write_bytes(b"generation")
    package = cover.parents[1]
    artifact_paths = {
        "cover": cover,
        "pre_overlay": package / "published-cover-carry/pre-overlay.png",
        "title_mask": package / "published-cover-carry/title-mask.png",
        "route_background": package / "published-cover-carry/route-background.png",
        "reference": package / "published-cover-carry/reference.png",
        "host_witness": package / "published-cover-carry/host-witness.png",
        "source_composition_receipt": package / "published-cover-carry/source-composition.json",
    }
    artifacts = {}
    for name, path in artifact_paths.items():
        if path != cover:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"cover")
        artifacts[name] = {"path": str(path), "sha256": "sha256:" + hashlib.sha256(b"cover").hexdigest(), "bytes": 5}
    marker = {
        "schema_version": MARKER_SCHEMA, "status": "MATERIALIZED", "candidate_id": "auto_x",
        "cover_path": str(cover), "generation_path": str(generation),
        "cover_sha256": "sha256:" + hashlib.sha256(b"cover").hexdigest(),
        "generation_sha256": "sha256:" + hashlib.sha256(b"generation").hexdigest(),
        "artifacts": artifacts,
    }
    marker["recording_date"] = "2026-08-17"
    assert not validate_materialized_marker(marker, base=tmp_path, date="2026-08-17", candidate_id="auto_x")
    cover.write_bytes(b"tampered")
    assert not validate_materialized_marker(marker, base=tmp_path, date="2026-08-17", candidate_id="auto_x")


def test_materialized_marker_rejects_symlink(tmp_path: Path) -> None:
    source = tmp_path / "source.png"
    source.write_bytes(b"cover")
    cover = tmp_path / "cover.png"
    cover.symlink_to(source)
    marker = {"schema_version": MARKER_SCHEMA, "status": "MATERIALIZED", "candidate_id": "auto_x", "cover_path": str(cover), "generation_path": str(cover), "cover_sha256": "sha256:" + "0" * 64, "generation_sha256": "sha256:" + "0" * 64}
    assert not validate_materialized_marker(marker, base=tmp_path, date="2026-08-17", candidate_id="auto_x")


def test_materialize_rolls_back_only_its_created_files_on_mid_copy_failure(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    names = ("cover", "pre_overlay", "title_mask", "route_background", "reference", "host_witness", "source_composition_receipt")
    artifacts = {}
    for name in names:
        path = source / name
        path.write_bytes(name.encode())
        artifacts[name] = {
            "source_path": str(path),
            "sha256": "sha256:" + hashlib.sha256(name.encode()).hexdigest(),
            "bytes": len(name),
        }
    entry = {"artifacts": artifacts, "cover_generation": {}}
    created = []
    def create(path: Path, payload: bytes) -> None:
        if len(created) == 2:
            raise RuntimeError("injected")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        meta = path.lstat()
        created.append((path, (meta.st_dev, meta.st_ino)))
    with pytest.raises(RuntimeError, match="injected"):
        materialized_marker(entry=entry, target_base=tmp_path / "target", date="2026-08-17", candidate_id="auto_x", create=create)
    assert all(not path.exists() for path, _identity in created)


@pytest.mark.parametrize("replacement_kind", ["regular", "symlink"])
def test_materialize_fails_closed_when_rollback_loses_target_ownership(
    tmp_path: Path, replacement_kind: str
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    names = (
        "cover", "pre_overlay", "title_mask", "route_background", "reference",
        "host_witness", "source_composition_receipt",
    )
    artifacts = {}
    for name in names:
        path = source / name
        payload = name.encode()
        path.write_bytes(payload)
        artifacts[name] = {
            "source_path": str(path),
            "sha256": "sha256:" + hashlib.sha256(payload).hexdigest(),
            "bytes": len(payload),
        }
    first_target: Path | None = None
    replacement = tmp_path / "replacement"
    replacement.write_bytes(b"replacement bytes")

    def create(path: Path, payload: bytes) -> None:
        nonlocal first_target
        if first_target is None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
            first_target = path
            return
        assert first_target is not None
        first_target.unlink()
        if replacement_kind == "regular":
            first_target.write_bytes(replacement.read_bytes())
        else:
            first_target.symlink_to(replacement)
        raise RuntimeError("injected after ownership replacement")

    with pytest.raises(PublishedCoverCarryError, match="PUBLISHED_COVER_CARRY_ROLLBACK_OWNERSHIP_LOST"):
        materialized_marker(
            entry={"artifacts": artifacts, "cover_generation": {}},
            target_base=tmp_path / "target",
            date="2026-08-17",
            candidate_id="auto_x",
            create=create,
        )
    assert first_target is not None
    if replacement_kind == "regular":
        assert first_target.read_bytes() == b"replacement bytes"
    else:
        assert first_target.is_symlink()
        assert first_target.resolve() == replacement
