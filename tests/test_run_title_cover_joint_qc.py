import hashlib
from pathlib import Path

from scripts.run_title_cover_joint_qc import resolve_delivery_cover


def _package(tmp_path: Path, *, alias_bytes: bytes | None) -> tuple[Path, dict]:
    package_root = tmp_path / "replacement_recuts"
    covers = package_root / "covers"
    covers.mkdir(parents=True)
    route = covers / "auto_test_1_2.ai-title.cover.png"
    route.write_bytes(b"final cover bytes")
    if alias_bytes is not None:
        alias = package_root / "auto_test_1_2.recut.burned-final-speaker.cover.png"
        alias.write_bytes(alias_bytes)
    publish = {
        "cover_path": str(route),
        "cover_generation": {
            "final_cover": str(route),
            "final_cover_sha256": "sha256:"
            + hashlib.sha256(route.read_bytes()).hexdigest(),
        },
    }
    return package_root, publish


def test_binds_the_same_stem_upload_alias_when_bytes_are_identical(
    tmp_path: Path,
) -> None:
    package_root, publish = _package(tmp_path, alias_bytes=b"final cover bytes")

    resolved = resolve_delivery_cover(package_root, publish)

    assert resolved == (
        package_root / "auto_test_1_2.recut.burned-final-speaker.cover.png"
    )


def test_falls_back_to_the_declared_route_when_no_alias_exists(
    tmp_path: Path,
) -> None:
    package_root, publish = _package(tmp_path, alias_bytes=None)

    resolved = resolve_delivery_cover(package_root, publish)

    assert resolved == package_root / "covers" / "auto_test_1_2.ai-title.cover.png"


def test_refuses_to_bind_a_drifted_alias(tmp_path: Path) -> None:
    """A stale alias must not be bound; make-manifest then fails closed."""

    package_root, publish = _package(tmp_path, alias_bytes=b"stale cover bytes")

    resolved = resolve_delivery_cover(package_root, publish)

    assert resolved == package_root / "covers" / "auto_test_1_2.ai-title.cover.png"


def test_ambiguous_identical_aliases_fall_back_to_the_declared_route(
    tmp_path: Path,
) -> None:
    package_root, publish = _package(tmp_path, alias_bytes=b"final cover bytes")
    (package_root / "auto_test_1_2.recut.burned-final-sapphire72.cover.png").write_bytes(
        b"final cover bytes"
    )

    resolved = resolve_delivery_cover(package_root, publish)

    assert resolved == package_root / "covers" / "auto_test_1_2.ai-title.cover.png"
