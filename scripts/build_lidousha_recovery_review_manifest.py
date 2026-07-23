#!/usr/bin/env python3
"""Build an exact, record-derived recovery review manifest.

The manifest is a projection of the final state and delivered records.  It is
never patched in place: every invocation re-derives titles, cover provenance,
paths, and the exact candidate set so a rerun cannot leave stale review or
upload metadata behind.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.autoslice.cover_route_evidence import validate_cover_route_decision


MANIFEST_SCHEMA = "lidousha-review-package.v1"
EXACT_CONTRACT_MODE = "EXACT_CANDIDATE_SET_NO_BACKFILL"
DELIVERED_STATUSES = {"ok", "review_ready"}
COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z")


class ManifestBuildError(ValueError):
    """The state/package pair cannot prove an exact review package."""


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ManifestBuildError(f"unreadable JSON: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ManifestBuildError(f"JSON must be an object: {path}")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _required_file(root: Path, name: str) -> Path:
    path = root / name
    if not path.is_file() or path.is_symlink():
        raise ManifestBuildError(f"required regular file missing: {path}")
    return path


def _record_generation(record: dict[str, Any]) -> dict[str, Any]:
    generation = record.get("cover_generation")
    if not isinstance(generation, dict):
        staging = record.get("publish_staging")
        generation = (
            staging.get("cover_generation")
            if isinstance(staging, dict)
            else None
        )
    if not isinstance(generation, dict) or not validate_cover_route_decision(
        generation, allow_legacy_v1=False
    ):
        raise ManifestBuildError(
            "record has no valid lidousha-cover-route-decision.v2"
        )
    return generation


def _record_title(record: dict[str, Any]) -> str:
    staging = record.get("publish_staging")
    title = (
        str(staging.get("title") or "").strip()
        if isinstance(staging, dict)
        else ""
    )
    if not title:
        raise ManifestBuildError("record publish_staging.title is missing")
    return title


def _story_candidate_id(record: dict[str, Any]) -> str:
    story_contract = record.get("story_contract")
    candidate_id = (
        str(story_contract.get("candidate_id") or "").strip()
        if isinstance(story_contract, dict)
        else ""
    )
    if not candidate_id:
        raise ManifestBuildError("record story_contract.candidate_id is missing")
    return candidate_id


def _verify_artifact_hashes(
    record: dict[str, Any], *, video: Path, cover: Path, subtitle: Path
) -> None:
    artifact_hashes = record.get("artifact_hashes")
    if not isinstance(artifact_hashes, dict):
        raise ManifestBuildError("record artifact_hashes is missing")
    actual_video = _sha256(video)
    if actual_video not in {
        artifact_hashes.get("burned_video_sha256"),
        artifact_hashes.get("video_sha256"),
    }:
        raise ManifestBuildError(f"video hash drift: {video}")
    if artifact_hashes.get("cover_sha256") != _sha256(cover):
        raise ManifestBuildError(f"cover hash drift: {cover}")
    if _sha256(subtitle) not in {
        artifact_hashes.get("delivery_subtitle_sha256"),
        artifact_hashes.get("subtitle_sha256"),
    }:
        raise ManifestBuildError(f"subtitle hash drift: {subtitle}")


def _cover_route_summary(generation: dict[str, Any]) -> dict[str, Any]:
    route = generation["route_decision"]
    return {
        "schema_version": route.get("schema_version"),
        "selected_treatment": route.get("selected_treatment"),
        "actual_treatment": route.get("actual_treatment"),
        "execution_status": route.get("execution_status"),
        "selected_rationale": route.get("selected_rationale"),
        "rejected_alternatives": route.get("rejected_alternatives"),
        "image_generation_planned": route.get("image_generation_planned"),
        "image_generation_attempted": route.get(
            "image_generation_attempted"
        ),
        "image_generation_used": route.get("image_generation_used"),
        "source_visible_participant_ids": route.get(
            "source_visible_participant_ids"
        ),
        "final_visible_participant_ids": route.get(
            "final_visible_participant_ids"
        ),
    }


def build_manifest(
    *,
    package_root: Path,
    state: dict[str, Any],
    deployed_commit: str,
    created_at: str | None = None,
) -> dict[str, Any]:
    package_root = package_root.resolve()
    if not package_root.is_dir():
        raise ManifestBuildError(f"package root missing: {package_root}")
    commit = deployed_commit.strip().split()[0] if deployed_commit.strip() else ""
    if COMMIT_RE.fullmatch(commit) is None:
        raise ManifestBuildError("deployed commit must be a full 40-hex commit")
    if state.get("status") != "review_ready":
        raise ManifestBuildError(f"state is not review_ready: {state.get('status')}")
    if state.get("run_mode") != "RECOVERY_REVIEW":
        raise ManifestBuildError(f"state is not RECOVERY_REVIEW: {state.get('run_mode')}")
    if state.get("upload_allowed") is not False:
        raise ManifestBuildError("recovery state must bind upload_allowed=false")
    contract = state.get("talk_selection_contract")
    if not isinstance(contract, dict) or contract.get("mode") != EXACT_CONTRACT_MODE:
        raise ManifestBuildError("exact no-backfill talk_selection_contract missing")
    candidate_ids = contract.get("candidate_ids")
    if (
        not isinstance(candidate_ids, list)
        or not candidate_ids
        or len(candidate_ids) != len(set(candidate_ids))
        or not all(isinstance(value, str) and value for value in candidate_ids)
    ):
        raise ManifestBuildError("exact candidate_ids must be a unique non-empty list")

    picks = state.get("picks")
    if not isinstance(picks, list):
        raise ManifestBuildError("state picks missing")
    pick_ids = [
        str(row.get("candidate_id") or "")
        for row in picks
        if isinstance(row, dict) and row.get("candidate_id")
    ]
    if len(pick_ids) != len(set(pick_ids)):
        raise ManifestBuildError("duplicate candidate pick in exact recovery state")
    picks_by_id = {
        str(row.get("candidate_id") or ""): row
        for row in picks
        if isinstance(row, dict) and row.get("candidate_id")
    }
    if set(picks_by_id) != set(candidate_ids):
        raise ManifestBuildError(
            "final picks do not exactly match the no-backfill contract"
        )
    for candidate_id in candidate_ids:
        pick = picks_by_id[candidate_id]
        if (
            pick.get("status") not in DELIVERED_STATUSES
            or pick.get("bundle_lifecycle") != "CURRENT"
            or pick.get("bundle_compliance") != "COMPLIANT"
            or pick.get("rc") != 0
        ):
            raise ManifestBuildError(
                f"candidate is not a current compliant delivery: {candidate_id}"
            )

    records_by_id: dict[str, tuple[str, Path, dict[str, Any]]] = {}
    for record_path in sorted(package_root.glob("*.record.json")):
        stem = record_path.name[: -len(".record.json")]
        record = _load_json(record_path)
        candidate_id = _story_candidate_id(record)
        if candidate_id in records_by_id:
            raise ManifestBuildError(f"duplicate candidate record: {candidate_id}")
        records_by_id[candidate_id] = (stem, record_path, record)
    if set(records_by_id) != set(candidate_ids):
        raise ManifestBuildError(
            "delivered record set does not exactly match the selection contract"
        )

    items: list[dict[str, Any]] = []
    attestations: list[dict[str, Any]] = []
    for candidate_id in candidate_ids:
        stem, record_path, record = records_by_id[candidate_id]
        video = _required_file(package_root, f"{stem}.mp4")
        cover = _required_file(package_root, f"{stem}.cover.png")
        cover_title_mask = _required_file(
            package_root, f"{stem}.cover.title-mask.png"
        )
        cover_pre_overlay = _required_file(
            package_root, f"{stem}.cover.pre-overlay.png"
        )
        cover_route_background = _required_file(
            package_root, f"{stem}.cover.route-background.png"
        )
        subtitle = _required_file(package_root, f"{stem}.srt")
        clip_context = _required_file(package_root, f"{stem}.clip-context.json")
        regression = _required_file(
            package_root, f"{stem}.subtitle-regression.json"
        )
        chat_authority = _required_file(
            package_root, f"{stem}.chat-authority.json"
        )
        _verify_artifact_hashes(
            record, video=video, cover=cover, subtitle=subtitle
        )
        generation = _record_generation(record)
        if generation.get("final_cover_sha256") != _sha256(cover):
            raise ManifestBuildError(
                f"cover_generation final hash drift: {candidate_id}"
            )
        rendered_text_pixels = generation.get("rendered_text_pixels")
        if (
            not isinstance(rendered_text_pixels, dict)
            or rendered_text_pixels.get("mask_sha256")
            != _sha256(cover_title_mask)
            or rendered_text_pixels.get("pre_overlay_sha256")
            != _sha256(cover_pre_overlay)
            or generation.get("pre_overlay_sha256")
            != _sha256(cover_pre_overlay)
            or generation.get("ai_background_sha256")
            != _sha256(cover_route_background)
        ):
            raise ManifestBuildError(
                f"cover title replay artifact hash drift: {candidate_id}"
            )
        title = _record_title(record)
        burned_preview = record.get("burned_preview")
        ass_path = (
            Path(str(burned_preview.get("ass_path") or ""))
            if isinstance(burned_preview, dict)
            else Path()
        )
        if not ass_path.is_file():
            raise ManifestBuildError(f"record ASS path missing: {ass_path}")
        item = {
            "stem": stem,
            "candidate_id": candidate_id,
            "title": title,
            "mp4": video.name,
            "video": video.name,
            "cover": cover.name,
            "cover_title_mask": cover_title_mask.name,
            "cover_pre_overlay": cover_pre_overlay.name,
            "cover_route_background": cover_route_background.name,
            "subtitle_srt": subtitle.name,
            "record": record_path.name,
            "clip_context": clip_context.name,
            "subtitle_regression_audit": regression.name,
            "chat_authority": chat_authority.name,
            "ass_path": str(ass_path.resolve()),
            "cover_route_summary": _cover_route_summary(generation),
        }
        baseline = package_root / f"{stem}.redelivery-baseline.json"
        if baseline.is_file() and not baseline.is_symlink():
            item["redelivery_baseline"] = baseline.name
        items.append(item)
        attestations.append(
            {
                "candidate_id": candidate_id,
                "stem": stem,
                "reference_sha256": generation.get("reference_sha256"),
                "final_cover_sha256": generation.get("final_cover_sha256"),
                "method": generation.get("method"),
                "route_decision": generation.get("route_decision"),
                "reference_authority": generation.get("reference_authority"),
            }
        )

    return {
        "schema_version": MANIFEST_SCHEMA,
        "created_at": created_at
        or datetime.now(timezone.utc).isoformat(),
        "generated_by": "build_lidousha_recovery_review_manifest.v1",
        "date": str(state.get("date") or package_root.name),
        "status": "finished_review_package_no_upload_pending_human_review",
        "run_mode": "RECOVERY_REVIEW",
        "upload_allowed": False,
        "story_contract_required": True,
        "subtitle_visual_contract": {
            "profile": "autoslice-sapphire72",
            "max_visual_lines": 2,
            "max_chars_per_line": 28,
        },
        "deployed_commit": commit,
        "selection_contract": contract,
        "exact_candidate_ids": list(candidate_ids),
        "counts": {"items": len(items), "talk": len(items), "song": 0},
        "cover_route_attestations": attestations,
        "items": items,
    }


def write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Rebuild an exact Li Dousha recovery review manifest"
    )
    parser.add_argument("package_root", type=Path)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--deployed-commit-file", type=Path, required=True)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    try:
        state = _load_json(args.state)
        commit = args.deployed_commit_file.read_text(encoding="utf-8")
        manifest = build_manifest(
            package_root=args.package_root,
            state=state,
            deployed_commit=commit,
        )
        output = args.out or args.package_root / "review_manifest.json"
        write_manifest(output, manifest)
    except (ManifestBuildError, OSError) as exc:
        print(f"REFUSE: {exc}")
        return 2
    print(
        json.dumps(
            {
                "manifest": str(output),
                "items": len(manifest["items"]),
                "deployed_commit": manifest["deployed_commit"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
