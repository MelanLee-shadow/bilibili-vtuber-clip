"""Canonical result projection after a producer batch owns its target bytes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Mapping

from src.autoslice.producer_batch_transaction import PreparedBatchEntry
from src.autoslice.producer_delivery_transaction import (
    PreparedDelivery,
    _read_document,
    _read_regular_bytes,
    load_prepared_delivery,
)
from src.autoslice.published_cover_carry import accepted_result_cover_status


def _prepared_handle(*, runtime_root: Path, lane: str, candidate_id: str, value: object) -> PreparedBatchEntry:
    if not isinstance(value, Mapping):
        raise ValueError("prepared delivery handle is missing")
    path = value.get("manifest_path")
    digest = value.get("prepared_sha256")
    if not isinstance(path, str) or not isinstance(digest, str) or not digest.startswith("sha256:"):
        raise ValueError("prepared delivery handle is invalid")
    return PreparedBatchEntry(
        lane=lane, candidate_id=candidate_id,
        handle=PreparedDelivery(runtime_root, lane, candidate_id, digest.removeprefix("sha256:"), Path(path)),
    )


def prepared_batch_entry(*, runtime_root: Path, lane: str, candidate_id: str, result: Mapping[str, object]) -> PreparedBatchEntry | None:
    """Decode one non-public producer result without treating it as delivered."""

    if result.get("status") != "delivery_prepared_no_target":
        return None
    summary = result.get("prepared_summary")
    handle = summary.get("prepared_delivery") if isinstance(summary, Mapping) else result.get("prepared_delivery")
    return _prepared_handle(runtime_root=runtime_root, lane=lane, candidate_id=candidate_id, value=handle)


def project_materialized_talk(
    result: dict, *, item: Mapping[str, object], date: str, work_root: Path,
    runner: object, finalize: Callable[..., None],
) -> None:
    """Turn a private Talk summary into its legacy materialized result shape."""

    summary = result.pop("prepared_summary", None)
    if not isinstance(summary, dict):
        raise ValueError("prepared Talk summary is missing")
    prepared_value = summary.pop("prepared_delivery", None)
    prepared_bindings = summary.pop("prepared_artifacts", None)
    result.pop("prepared_delivery", None)
    summary.pop("prepared_delivery", None)
    result["summary"] = summary
    result["cover_status"] = summary.get("cover_status")
    result["delivered"] = summary["delivery"]
    if isinstance(summary.get("subtitle"), str):
        result["delivered_subtitle"] = summary["subtitle"]
    result["red_flags"] = list(summary.get("red_flags") or [])
    result["boundary_repairs"] = list(summary.get("boundary_repairs") or [])
    prepared_cover_ready = False
    if accepted_result_cover_status(
        result, marker=item.get("published_cover_carry"),
        base=Path(getattr(runner, "BASE")), date=date,
        candidate_id=str(item.get("cid") or item.get("candidate_id") or ""),
    ) is not None:
        entry = _prepared_handle(
            runtime_root=Path(getattr(runner, "BASE")), lane="talk",
            candidate_id=str(item.get("cid") or item.get("candidate_id") or ""),
            value=prepared_value,
        )
        prepared = load_prepared_delivery(
            runtime_root=entry.handle.runtime_root, manifest_path=entry.handle.manifest_path,
        )
        document = _read_document(prepared)
        artifacts = {
            str(row.get("role")): row
            for row in document.get("artifacts", []) if isinstance(row, Mapping)
        }
        video, subtitle, cover = (
            artifacts.get("video"), artifacts.get("subtitle"), artifacts.get("cover"),
        )
        sealed_bindings = {
            role: {"path": str(row.get("target_path")), "sha256": str(row.get("staged_sha256"))}
            for role, row in artifacts.items()
        }
        expected_cover = str(Path(str(summary.get("delivery"))).with_suffix(".cover.png"))
        prepared_cover_ready = bool(
            prepared_bindings == sealed_bindings
            and {"video", "subtitle", "cover", "record", "publish"}.issubset(artifacts)
            and isinstance(video, Mapping) and isinstance(subtitle, Mapping) and isinstance(cover, Mapping)
            and video.get("target_path") == summary.get("delivery")
            and subtitle.get("target_path") == summary.get("subtitle")
            and cover.get("target_path") == expected_cover
            and video.get("staged_sha256") == result.get("video_sha256")
            and subtitle.get("staged_sha256") == result.get("subtitle_sha256")
            and cover.get("staged_sha256") == result.get("cover_sha256")
        )
    finalize(
        result, str(item.get("cid") or item.get("candidate_id") or ""),
        work_root / str(item.get("cid") or item.get("candidate_id") or ""),
        item.get("published_cover_carry"), runner, date,
        prepared_cover_ready=prepared_cover_ready,
    )


def _sealed_song_projection(
    result: Mapping[str, object], *, runtime_root: Path, candidate_id: str,
) -> tuple[dict[str, dict[str, str]], dict]:
    entry = _prepared_handle(
        runtime_root=runtime_root, lane="song", candidate_id=candidate_id,
        value=result.get("prepared_delivery"),
    )
    prepared = load_prepared_delivery(
        runtime_root=entry.handle.runtime_root, manifest_path=entry.handle.manifest_path,
    )
    document = _read_document(prepared)
    expected: dict[str, dict[str, str]] = {}
    for row in document.get("artifacts", []):
        if not isinstance(row, Mapping):
            raise ValueError("prepared Song artifact map is invalid")
        role = row.get("role")
        path = row.get("target_path")
        digest = row.get("staged_sha256")
        if not all(isinstance(value, str) and value for value in (role, path, digest)) or role in expected:
            raise ValueError("prepared Song artifact map is invalid")
        expected[role] = {"path": path, "sha256": digest}
    if "video" not in expected or "delivery_manifest" not in expected or "active_record" not in expected:
        raise ValueError("prepared Song artifact map is incomplete")
    return expected, document


def _sealed_song_cover_generation(prepared: Mapping[str, object]) -> object:
    """Read the generation only from the hash-sealed active-record artifact."""

    rows = prepared.get("artifacts")
    if not isinstance(rows, list):
        raise ValueError("prepared Song artifact map is invalid")
    active = next((row for row in rows if isinstance(row, Mapping) and row.get("role") == "active_record"), None)
    if not isinstance(active, Mapping) or not isinstance(active.get("staged_path"), str):
        raise ValueError("prepared Song active record is missing")
    try:
        record = json.loads(_read_regular_bytes(Path(active["staged_path"])).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("prepared Song active record is invalid") from exc
    materialized = record.get("materialized_recut") if isinstance(record, Mapping) else None
    staging = materialized.get("publish_staging") if isinstance(materialized, Mapping) else None
    return staging.get("cover_generation") if isinstance(staging, Mapping) else None


def project_materialized_song(
    result: dict, *, runtime_root: Path, candidate_id: str,
    song_status: Callable[[int, bool], str],
) -> None:
    """Restore the direct Song delivery fields only after target materialization."""

    intended = result.pop("intended_delivery", None)
    manifest = result.pop("intended_delivery_manifest", None)
    sidecars = result.pop("intended_delivery_sidecars", None)
    cover_status = result.pop("intended_cover_status", None)
    intended_cover = result.pop("intended_cover", None)
    cover_generation = result.pop("prepared_cover_generation", None)
    if not isinstance(intended, Mapping) or not isinstance(manifest, Mapping):
        raise ValueError("prepared Song targets are missing")
    expected, document = _sealed_song_projection(
        result, runtime_root=runtime_root, candidate_id=candidate_id,
    )
    expected_sidecars = {role: value for role, value in expected.items() if role != "video"}
    if (
        dict(intended) != expected["video"]
        or dict(manifest) != expected["delivery_manifest"]
        or not isinstance(sidecars, Mapping)
        or dict(sidecars) != expected_sidecars
        or cover_status != (
            "AI_COVER_READY" if "cover" in expected else "BLOCKED_AI_COVER_REQUIRED"
        )
        or ("cover" in expected and (not isinstance(intended_cover, Mapping) or dict(intended_cover) != expected["cover"]))
        or ("cover" not in expected and intended_cover is not None)
        or ("cover" in expected and cover_generation != _sealed_song_cover_generation(document))
        or ("cover" not in expected and cover_generation is not None)
    ):
        raise ValueError("prepared Song result projection drifts from sealed targets")
    result.pop("prepared_delivery", None)
    result["delivered"] = intended["path"]
    result["delivered_sha256"] = intended["sha256"]
    result["video_sha256"] = intended["sha256"]
    result["delivered_sidecars"] = {
        role: value["path"] for role, value in (sidecars or {}).items()
        if isinstance(value, Mapping) and isinstance(value.get("path"), str)
    }
    result["delivered_sidecar_hashes"] = {
        role: value["sha256"] for role, value in (sidecars or {}).items()
        if isinstance(value, Mapping) and isinstance(value.get("sha256"), str)
    }
    result["delivery_manifest_path"] = manifest["path"]
    result["delivery_manifest_sha256"] = manifest["sha256"]
    result["delivery_upload_enabled"] = False
    result["cover_status"] = cover_status
    if isinstance(intended_cover, Mapping):
        result["cover_path"] = intended_cover.get("path")
        result["cover_sha256"] = intended_cover.get("sha256")
        result["cover_generation"] = cover_generation
    result["status"] = song_status(int(result.get("rc") or 0), True)
