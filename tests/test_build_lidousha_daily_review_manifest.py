from pathlib import Path

import pytest

from scripts.build_lidousha_daily_review_manifest import (
    DailyManifestError,
    _sha256,
    _resolve_final_cover,
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


def test_resolve_final_cover_uses_exact_state_and_record_binding(
    tmp_path: Path,
) -> None:
    covers = tmp_path / "covers"
    covers.mkdir()
    stale = covers / "candidate.ai-title.cover.png"
    current = covers / "candidate.screenshot-title.cover.png"
    stale.write_bytes(b"stale ai route\n")
    current.write_bytes(b"current screenshot route\n")
    current_sha = _sha256(current)

    resolved = _resolve_final_cover(
        package_root=tmp_path,
        pick={
            "cover_path": str(current),
            "cover_sha256": "sha256:" + current_sha,
        },
        cover_generation={
            "final_cover": str(current),
            "final_cover_sha256": "sha256:" + current_sha,
        },
    )

    assert resolved == "covers/candidate.screenshot-title.cover.png"


def test_resolve_final_cover_refuses_state_record_route_drift(
    tmp_path: Path,
) -> None:
    covers = tmp_path / "covers"
    covers.mkdir()
    ai = covers / "candidate.ai-title.cover.png"
    screenshot = covers / "candidate.screenshot-title.cover.png"
    ai.write_bytes(b"ai route\n")
    screenshot.write_bytes(b"screenshot route\n")

    with pytest.raises(
        DailyManifestError,
        match="state/record final cover binding drift",
    ):
        _resolve_final_cover(
            package_root=tmp_path,
            pick={
                "cover_path": str(screenshot),
                "cover_sha256": "sha256:" + _sha256(screenshot),
            },
            cover_generation={
                "final_cover": str(ai),
                "final_cover_sha256": "sha256:" + _sha256(ai),
            },
        )
