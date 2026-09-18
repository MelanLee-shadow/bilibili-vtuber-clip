from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from src.autoslice.cover_host_identity_gate import (
    HOST_ONLY_AUTHORITY,
    HOST_ONLY_SCHEMA_VERSION,
)
from src.autoslice.host_only_v4_package_binding import (
    BINDING_ITEM_KEY,
    HostOnlyV4PackageBindingError,
    materialize_package_binding,
    validate_package_binding,
)


def _sha(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _generation(
    *,
    cover_bytes: bytes,
    reference_path: str,
    reference_bytes: bytes,
    comparison_path: str,
    comparison_bytes: bytes,
) -> dict[str, object]:
    return {
        "final_host_identity_verification": {
            "schema_version": HOST_ONLY_SCHEMA_VERSION,
            "authority": HOST_ONLY_AUTHORITY,
            "status": "PASS",
            "host_only_required": True,
            "final_cover_path": "/producer/final.cover.png",
            "final_cover_sha256": _sha(cover_bytes),
            "reference_path": reference_path,
            "reference_sha256": _sha(reference_bytes),
            "comparison_path": comparison_path,
            "comparison_sha256": _sha(comparison_bytes),
            "witness": {
                "status": "OBSERVED",
                "provider": "cpa",
                "image_path": comparison_path,
                "image_sha256": _sha(comparison_bytes).removeprefix("sha256:"),
            },
        }
    }


def test_materialize_freezes_external_sources_into_portable_binding(
    tmp_path: Path,
) -> None:
    root = tmp_path / "package"
    root.mkdir()
    cover_bytes = b"final cover pixels"
    reference_bytes = b"reference pixels"
    comparison_bytes = b"comparison pixels"
    (root / "cover.png").write_bytes(cover_bytes)
    external = tmp_path / "external"
    external.mkdir()
    reference = external / "reference.png"
    comparison = external / "comparison.png"
    reference.write_bytes(reference_bytes)
    comparison.write_bytes(comparison_bytes)
    item: dict[str, object] = {
        "candidate_id": "auto_test",
        "cover": "cover.png",
    }
    generation = _generation(
        cover_bytes=cover_bytes,
        reference_path=str(reference),
        reference_bytes=reference_bytes,
        comparison_path=str(comparison),
        comparison_bytes=comparison_bytes,
    )

    binding = materialize_package_binding(
        root=root,
        item=item,
        generation=generation,
    )
    assert binding is not None
    item[BINDING_ITEM_KEY] = binding
    validate_package_binding(root=root, item=item, generation=generation)

    reference.write_bytes(b"changed external reference")
    comparison.write_bytes(b"changed external comparison")
    validate_package_binding(root=root, item=item, generation=generation)
    assert (root / str(binding["reference_path"])).read_bytes() == reference_bytes
    assert (root / str(binding["comparison_path"])).read_bytes() == comparison_bytes


def test_materialize_recovers_from_package_hash_when_source_locator_is_stale(
    tmp_path: Path,
) -> None:
    root = tmp_path / "package"
    (root / "cover_refs").mkdir(parents=True)
    (root / "covers").mkdir()
    cover_bytes = b"final cover pixels"
    reference_bytes = b"already packaged reference pixels"
    comparison_bytes = b"already packaged comparison pixels"
    (root / "cover.png").write_bytes(cover_bytes)
    (root / "cover_refs" / "reference.png").write_bytes(reference_bytes)
    (root / "covers" / "comparison.png").write_bytes(comparison_bytes)
    item: dict[str, object] = {
        "candidate_id": "auto_portable",
        "cover": "cover.png",
    }
    generation = _generation(
        cover_bytes=cover_bytes,
        reference_path="/missing/producer/reference.png",
        reference_bytes=reference_bytes,
        comparison_path="/missing/producer/comparison.png",
        comparison_bytes=comparison_bytes,
    )

    binding = materialize_package_binding(
        root=root,
        item=item,
        generation=generation,
    )
    assert binding is not None
    item[BINDING_ITEM_KEY] = binding
    validate_package_binding(root=root, item=item, generation=generation)
    assert (root / str(binding["reference_path"])).read_bytes() == reference_bytes
    assert (root / str(binding["comparison_path"])).read_bytes() == comparison_bytes


def test_materialize_rejects_symlinked_evidence_directory_without_outside_write(
    tmp_path: Path,
) -> None:
    root = tmp_path / "package"
    root.mkdir()
    cover_bytes = b"final cover pixels"
    reference_bytes = b"reference pixels"
    comparison_bytes = b"comparison pixels"
    (root / "cover.png").write_bytes(cover_bytes)
    external = tmp_path / "external"
    external.mkdir()
    reference = external / "reference.png"
    comparison = external / "comparison.png"
    reference.write_bytes(reference_bytes)
    comparison.write_bytes(comparison_bytes)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "evidence").symlink_to(outside, target_is_directory=True)
    item = {"candidate_id": "auto_symlink", "cover": "cover.png"}
    generation = _generation(
        cover_bytes=cover_bytes,
        reference_path=str(reference),
        reference_bytes=reference_bytes,
        comparison_path=str(comparison),
        comparison_bytes=comparison_bytes,
    )

    with pytest.raises(
        HostOnlyV4PackageBindingError,
        match="destination is unavailable or unsafe",
    ):
        materialize_package_binding(
            root=root,
            item=item,
            generation=generation,
        )

    assert list(outside.iterdir()) == []


def test_materialize_ignores_non_v4_generation(tmp_path: Path) -> None:
    root = tmp_path / "package"
    root.mkdir()
    (root / "cover.png").write_bytes(b"cover")
    item = {"candidate_id": "auto_v3", "cover": "cover.png"}
    generation = {
        "final_host_identity_verification": {
            "schema_version": "lidousha-cover-final-host-identity-verification.v3"
        }
    }

    assert (
        materialize_package_binding(
            root=root,
            item=item,
            generation=generation,
        )
        is None
    )
    assert not (root / "evidence").exists()
