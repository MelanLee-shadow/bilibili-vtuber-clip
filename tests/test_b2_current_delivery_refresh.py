from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from src.autoslice import b2_current_delivery_refresh as refresh


def _source_tree(root: Path) -> Path:
    source = root / "source"
    source.mkdir()
    (source / "keep.txt").write_text("keep", encoding="utf-8")
    (source / "review_manifest.json").write_text("stale", encoding="utf-8")
    (source / "verification").mkdir()
    (source / "verification/old.json").write_text("stale", encoding="utf-8")
    return source


def test_copy_plan_excludes_package_instance_receipts(tmp_path: Path) -> None:
    source = _source_tree(tmp_path)
    output = tmp_path / "output"

    plan = refresh.copy_plan(source, output)

    destinations = {Path(str(row["destination"])).relative_to(output).as_posix() for row in plan}
    assert "keep.txt" in destinations
    assert "review_manifest.json" not in destinations
    assert "verification/old.json" not in destinations
    assert ".planned-current-delivery-metadata" in destinations


def test_copy_plan_rejects_symlink_before_output_creation(tmp_path: Path) -> None:
    source = _source_tree(tmp_path)
    (source / "link").symlink_to(source / "keep.txt")
    output = tmp_path / "output"

    with pytest.raises(refresh.B2CurrentDeliveryRefreshError, match="symlink"):
        refresh.copy_plan(source, output)

    assert not output.exists()


def test_copy_plan_rejects_existing_output(tmp_path: Path) -> None:
    source = _source_tree(tmp_path)
    output = tmp_path / "output"
    output.mkdir()

    with pytest.raises(refresh.B2CurrentDeliveryRefreshError, match="already exists"):
        refresh.copy_plan(source, output)


def test_source_manifest_requires_exact_b2_bytes(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    video = source / refresh.FINAL_VIDEO_NAME
    cover = source / refresh.FINAL_COVER_NAME
    video.write_bytes(b"video")
    cover.write_bytes(b"cover")
    manifest = {
        "schema_version": "authorized-upload-manifest.v3",
        "title": refresh.TITLE,
        "video": {"path": str(video), "sha256": "0" * 64},
        "cover": {"path": str(cover), "sha256": "0" * 64},
        "authorization": {"by": "维护者", "quote": "如果没问题，可以重新上传"},
        "tags": ["李豆沙"],
        "recovery_publication_authority": {
            "candidate_id": refresh.CANDIDATE_ID,
            "bvid": refresh.TARGET_BVID,
        },
    }
    path = source / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(refresh.B2CurrentDeliveryRefreshError, match="scope or bytes drift"):
        refresh._validate_source_manifest(source, path)


def test_project_capacity_floor_is_never_below_16_gib(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        refresh,
        "copy_capacity",
        lambda _entries: [
            {
                "path": "/tmp",
                "device": 1,
                "planned_copy_bytes": 123,
                "reserve_bytes": 0,
                "available_bytes": refresh._MIN_FREE_RESERVE_BYTES + 123,
                "required_bytes": 123,
            }
        ],
    )

    capacity = refresh._copy_capacity_with_project_floor([])

    assert capacity[0]["configured_reserve_bytes"] == 0
    assert capacity[0]["reserve_bytes"] == refresh._MIN_FREE_RESERVE_BYTES
    assert capacity[0]["required_bytes"] == refresh._MIN_FREE_RESERVE_BYTES + 123
    assert capacity[0]["project_minimum_reserve_applied"] is True


def test_project_capacity_floor_refuses_below_16_gib(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        refresh,
        "copy_capacity",
        lambda _entries: [
            {
                "path": "/tmp",
                "device": 1,
                "planned_copy_bytes": 123,
                "reserve_bytes": 0,
                "available_bytes": refresh._MIN_FREE_RESERVE_BYTES + 122,
                "required_bytes": 123,
            }
        ],
    )

    with pytest.raises(refresh.B2CurrentDeliveryRefreshError, match="insufficient"):
        refresh._copy_capacity_with_project_floor([])


def test_capacity_failure_happens_before_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _source_tree(tmp_path)
    output = tmp_path / "output"
    source_manifest = source / "manifest.json"
    source_manifest.write_text("{}", encoding="utf-8")
    events: list[str] = []

    monkeypatch.setattr(
        refresh,
        "_validate_source_manifest",
        lambda *_args: {"authorization": {"quote": "q"}, "tags": ["t"]},
    )
    monkeypatch.setattr(
        refresh,
        "audit_package",
        lambda _root: {"passed": True, "blocking_issue_count": 0, "issues": []},
    )
    monkeypatch.setattr(
        refresh,
        "copy_plan",
        lambda *_args: [{"status": "WOULD_COPY", "destination": str(output / "x"), "bytes": 1}],
    )

    def fail_capacity(_entries):
        events.append("capacity")
        raise RuntimeError("no capacity")

    monkeypatch.setattr(refresh, "_copy_capacity_with_project_floor", fail_capacity)
    monkeypatch.setattr(refresh, "_copy_package", lambda *_args: events.append("copy"))

    with pytest.raises(RuntimeError, match="no capacity"):
        refresh.refresh_current_delivery(
            source_package=source,
            source_manifest_path=source_manifest,
            output=output,
            source_commit="a" * 40,
        )

    assert events == ["capacity"]
    assert not output.exists()


def test_result_writer_is_create_only(tmp_path: Path) -> None:
    output = tmp_path / "result.json"
    refresh.write_result_create_only(output, {"status": "PASS"})
    assert json.loads(output.read_text()) == {"status": "PASS"}

    with pytest.raises(FileExistsError):
        refresh.write_result_create_only(output, {"status": "OTHER"})


def test_cli_bootstrap_ignores_installed_scripts_package() -> None:
    script = Path(__file__).resolve().parents[1] / "scripts/refresh_b2_current_delivery_package.py"
    namespace: dict[str, object] = {"__file__": str(script), "__name__": "bootstrap_test"}
    source = script.read_text(encoding="utf-8")
    exec(compile(source, str(script), "exec"), namespace)
    previous = {name: sys.modules.get(name) for name in ("scripts", "src")}
    try:
        namespace["_bootstrap_repository_namespaces"]()
        assert (
            Path(os.fspath(__import__("scripts").__path__[0])).resolve() == script.parent.resolve()
        )
    finally:
        for name, module in previous.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
