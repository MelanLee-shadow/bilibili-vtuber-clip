from pathlib import Path

import pytest

from scripts.build_lidousha_daily_review_manifest import (
    DailyManifestError,
    _sha256,
    _sync_declared_artifact,
)


def test_sync_declared_artifact_replaces_stale_package_copy(
    tmp_path: Path,
) -> None:
    source = tmp_path / "candidate" / "chat.json"
    target = tmp_path / "candidate" / "package" / "chat.json"
    source.parent.mkdir()
    target.parent.mkdir()
    source.write_bytes(b"current authority\n")
    target.write_bytes(b"stale authority\n")

    _sync_declared_artifact(
        target=target,
        source=source,
        declared_sha256="sha256:" + _sha256(source),
        label="chat authority",
    )

    assert target.read_bytes() == source.read_bytes()


def test_sync_declared_artifact_refuses_unbound_source_without_touching_target(
    tmp_path: Path,
) -> None:
    source = tmp_path / "candidate" / "chat.json"
    target = tmp_path / "candidate" / "package" / "chat.json"
    source.parent.mkdir()
    target.parent.mkdir()
    source.write_bytes(b"unexpected authority\n")
    target.write_bytes(b"previous package authority\n")

    with pytest.raises(DailyManifestError, match="chat authority sha drift"):
        _sync_declared_artifact(
            target=target,
            source=source,
            declared_sha256="sha256:" + "0" * 64,
            label="chat authority",
        )

    assert target.read_bytes() == b"previous package authority\n"
