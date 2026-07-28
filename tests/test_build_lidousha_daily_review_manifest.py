from pathlib import Path

import pytest

from scripts.build_lidousha_daily_review_manifest import (
    DailyManifestError,
    _sha256,
    _resolve_final_cover,
    _sync_record_bound_candidate_artifacts,
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


def test_resolve_final_cover_accepts_hash_identical_delivery_alias(
    tmp_path: Path,
) -> None:
    covers = tmp_path / "covers"
    delivery = tmp_path / "delivery"
    covers.mkdir()
    delivery.mkdir()
    packaged = covers / "final.cover.png"
    alias = delivery / "标题.cover.png"
    packaged.write_bytes(b"same repaired cover bytes\n")
    alias.write_bytes(packaged.read_bytes())
    expected = _sha256(packaged)

    resolved = _resolve_final_cover(
        package_root=tmp_path,
        pick={
            "cover_path": str(alias),
            "cover_sha256": "sha256:" + expected,
        },
        cover_generation={
            "final_cover": str(packaged),
            "final_cover_sha256": "sha256:" + expected,
        },
    )

    assert resolved == "covers/final.cover.png"


def test_sync_record_bound_candidate_artifacts_makes_evidence_portable(
    tmp_path: Path,
) -> None:
    candidate_root = tmp_path / "candidate"
    package_root = candidate_root / "replacement_recuts"
    package_root.mkdir(parents=True)
    candidate_id = "auto_test"
    chat = candidate_root / f"{candidate_id}.chat-authority.json"
    context = candidate_root / f"{candidate_id}.clip-context.json"
    chat.write_bytes(b"current chat authority\n")
    context.write_bytes(b"current clip context\n")
    (package_root / chat.name).write_bytes(b"stale chat authority\n")

    resolved = _sync_record_bound_candidate_artifacts(
        package_root=package_root,
        candidate_id=candidate_id,
        record_doc={
            "artifact_hashes": {
                "chat_authority_audit_sha256": "sha256:" + _sha256(chat),
                "clip_context_file_sha256": "sha256:" + _sha256(context),
            }
        },
    )

    assert resolved == {
        "chat_authority": chat.name,
        "clip_context": context.name,
    }
    assert (package_root / chat.name).read_bytes() == chat.read_bytes()
    assert (package_root / context.name).read_bytes() == context.read_bytes()
