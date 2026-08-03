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
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.autoslice.cover_route_evidence import validate_cover_route_decision
from src.autoslice.cover_only_audit_scope import (
    CoverOnlyAuditScopeError,
    validate_scope as validate_cover_only_audit_scope,
)
from src.autoslice.recovery_title_authority import (
    RecoveryTitleAuthorityError,
    validate_recovery_publication_authority,
)


MANIFEST_SCHEMA = "lidousha-review-package.v1"
EXACT_CONTRACT_MODE = "EXACT_CANDIDATE_SET_NO_BACKFILL"
DELIVERED_STATUSES = {"ok", "review_ready"}
COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z")
SHA256_RE = re.compile(r"(?:sha256:)?([0-9a-f]{64})\Z")


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


def _matches_sha256(path: Path, expected: object) -> bool:
    match = SHA256_RE.fullmatch(str(expected or ""))
    return bool(match and _sha256(path).removeprefix("sha256:") == match.group(1))


def _required_file(root: Path, name: str) -> Path:
    path = root / name
    if not path.is_file() or path.is_symlink():
        raise ManifestBuildError(f"required regular file missing: {path}")
    return path


def _project_optional_audit(
    *,
    package_root: Path,
    stem: str,
    record: dict[str, Any],
    audit_key: str,
    audit_path_key: str,
    suffix: str,
) -> tuple[str, Path | None]:
    """Project an optional producer audit without inventing authority.

    The producer deliberately leaves candidate-scoped subtitle regression and
    reviewed-baseline fields null when no canonical asset was configured.  A
    recovery package must preserve that typed state instead of requiring a
    made-up sidecar.  Conversely, a configured audit must have both the record
    payload and source path, and its packaged JSON must match the record.
    """

    audit = record.get(audit_key)
    declared_path = record.get(audit_path_key)
    if audit is None and declared_path is None:
        return "NOT_CONFIGURED", None
    if not isinstance(audit, dict) or not str(declared_path or "").strip():
        raise ManifestBuildError(
            f"optional audit record binding is incomplete: {audit_key}"
        )
    artifact = _required_file(package_root, f"{stem}.{suffix}")
    if _load_json(artifact) != audit:
        raise ManifestBuildError(
            f"optional audit payload differs from record: {audit_key}"
        )
    return "CONFIGURED", artifact


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


def _sync_cover_title_replay_artifacts(
    *,
    package_root: Path,
    stem: str,
    generation: dict[str, Any],
) -> tuple[Path, Path, Path]:
    """Refresh portable title-compositor evidence after a cover-only repair.

    Recovery packages keep stable, stem-named copies at the package root, while
    a reviewed cover repair writes immutable evidence under a new generation
    directory.  Rebuilding the manifest must project the *current* generation
    bytes, not reject the package merely because the old portable copies still
    exist.  Every source remains hash-bound by the current record before it is
    copied; undeclared, missing, symlinked, or drifted sources fail closed.
    """

    rendered = generation.get("rendered_text_pixels")
    if not isinstance(rendered, dict):
        raise ManifestBuildError("cover generation lacks rendered_text_pixels")
    specs = (
        (
            package_root / f"{stem}.cover.title-mask.png",
            rendered.get("mask_path"),
            rendered.get("mask_sha256"),
            "title mask",
        ),
        (
            package_root / f"{stem}.cover.pre-overlay.png",
            generation.get("pre_overlay_path"),
            generation.get("pre_overlay_sha256"),
            "pre-overlay",
        ),
        (
            package_root / f"{stem}.cover.route-background.png",
            generation.get("ai_background"),
            generation.get("ai_background_sha256"),
            "route background",
        ),
    )
    projected: list[Path] = []
    for target, declared_source, declared_sha, label in specs:
        if target.is_file() and not target.is_symlink() and _matches_sha256(
            target, declared_sha
        ):
            projected.append(target)
            continue
        if target.is_symlink():
            raise ManifestBuildError(
                f"cover {label} portable target may not be a symlink: {target}"
            )
        if not isinstance(declared_source, str) or not declared_source:
            raise ManifestBuildError(
                f"cover {label} portable copy drifted without a declared source"
            )
        source = Path(declared_source)
        if (
            source.is_symlink()
            or not source.is_file()
            or not _matches_sha256(source, declared_sha)
        ):
            raise ManifestBuildError(
                f"cover {label} declared source is missing or hash-drifted: {source}"
            )
        payload = source.read_bytes()
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=target.parent,
                prefix=f".{target.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
            temporary = None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        if not _matches_sha256(target, declared_sha):
            raise ManifestBuildError(
                f"cover {label} portable projection verification failed: {target}"
            )
        projected.append(target)
    return projected[0], projected[1], projected[2]


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
    record: dict[str, Any],
    *,
    video: Path,
    cover: Path,
    subtitle: Path,
    speaker_srt: Path,
    speaker_ass: Path,
    chat_authority: dict[str, Any],
    uniform_host: bool = False,
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
    if artifact_hashes.get("ass_sha256") != _sha256(speaker_ass):
        raise ManifestBuildError(f"speaker ASS hash drift: {speaker_ass}")
    if not _matches_sha256(
        speaker_srt, chat_authority.get("final_speaker_srt_sha256")
    ):
        raise ManifestBuildError(
            f"speaker SRT differs from chat authority: {speaker_srt}"
        )
    if not uniform_host and not _matches_sha256(
        speaker_ass, chat_authority.get("speaker_ass_sha256")
    ):
        raise ManifestBuildError(
            f"speaker ASS differs from chat authority: {speaker_ass}"
        )


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


_DATE_RX_STRICT = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _resolve_manifest_date(
    state: dict[str, Any],
    package_root: Path,
    records_by_id: dict[str, tuple[str, Path, dict[str, Any]]],
) -> str:
    """Manifest date must be a real recording date, never a staging dir name.

    Release-scoped staging dirs (release-3573) exposed the old fallback: with
    no state date, the dir name leaked in and every clip-context date check
    mismatched. The records' story dates are the next authority; they must
    all agree.
    """

    for source in (state.get("date"), package_root.name):
        value = str(source or "")
        if _DATE_RX_STRICT.fullmatch(value):
            return value
    record_dates = set()
    for stem, _, _ in records_by_id.values():
        try:
            payload = _load_json(package_root / f"{stem}.clip-context.json")
        except (OSError, ValueError):
            continue
        record_dates.add(str(payload.get("recording_date") or ""))
    record_dates.discard("")
    if len(record_dates) == 1:
        value = record_dates.pop()
        if _DATE_RX_STRICT.fullmatch(value):
            return value
    raise ManifestBuildError(
        f"cannot resolve a recording date (state/none, root={package_root.name!r},"
        f" record dates={sorted(record_dates)})"
    )


def _normalized_release_authorities(
    state: dict[str, Any],
    candidate_ids: list[str],
    release_scope: list[str] | None,
) -> dict[str, dict[str, object]]:
    """Validate the exact rerun plan and normalize scoped authorities."""

    if release_scope is not None:
        unknown = set(release_scope) - set(candidate_ids)
        if not release_scope or unknown:
            raise ManifestBuildError(
                f"release scope must be a subset of the exact contract: {sorted(unknown)}"
            )
    rerun_plan = state.get("delivery_rerun_plan")
    publication_authorities = (
        rerun_plan.get(
            "recovery_publication_authorities_by_candidate"
        )
        if isinstance(rerun_plan, dict)
        else None
    )
    if (
        not isinstance(rerun_plan, dict)
        or rerun_plan.get("schema_version")
        != "recovery-review-talk-rerun-plan.v7"
        or not isinstance(publication_authorities, dict)
        or set(publication_authorities) != set(candidate_ids)
    ):
        raise ManifestBuildError(
            "exact recovery publication authority map is missing"
        )
    normalized_publication_authorities: dict[
        str, dict[str, object]
    ] = {}
    for candidate_id in (release_scope or candidate_ids):
        try:
            normalized_publication_authorities[candidate_id] = (
                validate_recovery_publication_authority(
                    publication_authorities[candidate_id],
                    candidate_id=candidate_id,
                )
            )
        except RecoveryTitleAuthorityError as exc:
            raise ManifestBuildError(
                "recovery publication authority invalid: "
                f"{candidate_id}: {exc}"
            ) from exc

    return normalized_publication_authorities


def build_manifest(
    *,
    package_root: Path,
    state: dict[str, Any],
    deployed_commit: str,
    release_scope: list[str] | None = None,
    cover_only_audit_scopes: list[Path] | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    package_root = package_root.resolve()
    if not package_root.is_dir():
        raise ManifestBuildError(f"package root missing: {package_root}")
    commit = deployed_commit.strip().split()[0] if deployed_commit.strip() else ""
    if COMMIT_RE.fullmatch(commit) is None:
        raise ManifestBuildError("deployed commit must be a full 40-hex commit")
    if state.get("status") != "review_ready":
        # 维护者 per-BV ruling: 「没有任何纪律要求必须5个全complete
        # 才能动BV，修复时哪个好了就可以改哪个」。An explicit release scope
        # unlocks per-candidate freezing while the batch is still incomplete;
        # every scoped candidate must itself be a delivered compliant pick.
        if not (release_scope and state.get("status") == "recovery_incomplete"):
            raise ManifestBuildError(
                f"state is not review_ready: {state.get('status')}"
            )
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
    normalized_publication_authorities = _normalized_release_authorities(
        state, candidate_ids, release_scope
    )
    scope_payloads: dict[str, tuple[Path, dict[str, Any]]] = {}
    for raw_scope_path in cover_only_audit_scopes or []:
        if raw_scope_path.is_symlink():
            raise ManifestBuildError(
                f"cover-only audit scope may not be a symlink: {raw_scope_path}"
            )
        scope_path = raw_scope_path.resolve()
        try:
            scope_path.relative_to(package_root)
        except ValueError as exc:
            raise ManifestBuildError(
                f"cover-only audit scope must be inside package root: {scope_path}"
            ) from exc
        if not scope_path.is_file() or scope_path.is_symlink():
            raise ManifestBuildError(
                f"cover-only audit scope missing or unsafe: {scope_path}"
            )
        payload = _load_json(scope_path)
        scope_candidate = str(payload.get("candidate_id") or "")
        if (
            scope_candidate not in set(release_scope or candidate_ids)
            or scope_candidate in scope_payloads
        ):
            raise ManifestBuildError(
                "cover-only audit scope candidate is unknown or duplicated: "
                f"{scope_candidate!r}"
            )
        scope_payloads[scope_candidate] = (scope_path, payload)
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
    for candidate_id in (release_scope or candidate_ids):
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
    if set(records_by_id) != set(release_scope or candidate_ids):
        raise ManifestBuildError(
            "delivered record set does not exactly match the "
            + ("release scope" if release_scope else "selection contract")
        )

    items: list[dict[str, Any]] = []
    attestations: list[dict[str, Any]] = []
    for candidate_id in (release_scope or candidate_ids):
        stem, record_path, record = records_by_id[candidate_id]
        video = _required_file(package_root, f"{stem}.mp4")
        cover = _required_file(package_root, f"{stem}.cover.png")
        subtitle = _required_file(package_root, f"{stem}.srt")
        clip_context = _required_file(package_root, f"{stem}.clip-context.json")
        regression_status, regression = _project_optional_audit(
            package_root=package_root,
            stem=stem,
            record=record,
            audit_key="subtitle_regression",
            audit_path_key="subtitle_regression_audit_path",
            suffix="subtitle-regression.json",
        )
        chat_authority = _required_file(
            package_root, f"{stem}.chat-authority.json"
        )
        chat_authority_payload = _load_json(chat_authority)
        # uniform_host 政策（ee29e08，b19dfae daily 同规）：单说话人包没有独立
        # speaker 工件——chat authority 自证 final_speaker_srt_sha256 == 正文
        # srt 且 speaker_ass_sha256 为空时，speaker 面与正文同体，ASS 用烧录
        # 终 ASS（record.artifact_hashes.ass_sha256 仍逐字节校验）。带真
        # speaker 工件的历史 recovery 包走原严格路径。
        uniform_host = bool(
            _matches_sha256(
                subtitle, chat_authority_payload.get("final_speaker_srt_sha256")
            )
            and not chat_authority_payload.get("speaker_ass_sha256")
        )
        if uniform_host:
            speaker_srt = subtitle
            speaker_ass = _required_file(
                package_root, f"{stem}.final-sapphire72.ass"
            )
        else:
            speaker_srt = _required_file(
                package_root, f"{stem}.speaker.srt"
            )
            speaker_ass = _required_file(
                package_root, f"{stem}.speaker.ass"
            )
        publish = _required_file(package_root, f"{stem}.publish.json")
        publish_payload = _load_json(publish)
        _verify_artifact_hashes(
            record,
            video=video,
            cover=cover,
            subtitle=subtitle,
            speaker_srt=speaker_srt,
            speaker_ass=speaker_ass,
            chat_authority=chat_authority_payload,
            uniform_host=uniform_host,
        )
        generation = _record_generation(record)
        (
            cover_title_mask,
            cover_pre_overlay,
            cover_route_background,
        ) = _sync_cover_title_replay_artifacts(
            package_root=package_root,
            stem=stem,
            generation=generation,
        )
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
        try:
            recovery_publication_authority = (
                validate_recovery_publication_authority(
                    record.get("recovery_publication_authority"),
                    candidate_id=candidate_id,
                    expected_final_title=title,
                )
            )
        except RecoveryTitleAuthorityError as exc:
            raise ManifestBuildError(
                f"recovery publication authority invalid: "
                f"{candidate_id}: {exc}"
            ) from exc
        publish_staging = record.get("publish_staging")
        if (
            recovery_publication_authority
            != normalized_publication_authorities[candidate_id]
            or not isinstance(publish_staging, dict)
            or publish_staging.get("recovery_publication_authority")
            != recovery_publication_authority
            or publish_payload.get("recovery_publication_authority")
            != recovery_publication_authority
            or publish_payload.get("title") != title
        ):
            raise ManifestBuildError(
                f"recovery publication binding drift: {candidate_id}"
            )
        artifact_hashes = record.get("artifact_hashes")
        if (
            not isinstance(artifact_hashes, dict)
            or artifact_hashes.get("publish_draft_sha256")
            != _sha256(publish)
        ):
            raise ManifestBuildError(
                f"publish draft hash drift: {candidate_id}"
            )
        burned_preview = record.get("burned_preview")
        burned_ass_path = (
            Path(str(burned_preview.get("ass_path") or ""))
            if isinstance(burned_preview, dict)
            else Path()
        )
        if not burned_ass_path.is_file():
            raise ManifestBuildError(
                f"record ASS path missing: {burned_ass_path}"
            )
        if _sha256(burned_ass_path) != _sha256(speaker_ass):
            raise ManifestBuildError(
                f"packaged speaker ASS differs from burned ASS: {candidate_id}"
            )
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
            "publish_json": publish.name,
            "clip_context": clip_context.name,
            "subtitle_regression_status": regression_status,
            "chat_authority": chat_authority.name,
            "speaker_srt": speaker_srt.name,
            "speaker_srt_sha256": _sha256(speaker_srt),
            "ass_path": speaker_ass.name,
            "ass_sha256": _sha256(speaker_ass),
            "cover_route_summary": _cover_route_summary(generation),
        }
        item["recovery_publication_authority"] = (
            recovery_publication_authority
        )
        if regression is not None:
            item["subtitle_regression_audit"] = regression.name
        baseline_status, baseline = _project_optional_audit(
            package_root=package_root,
            stem=stem,
            record=record,
            audit_key="redelivery_baseline",
            audit_path_key="redelivery_baseline_audit_path",
            suffix="redelivery-baseline.json",
        )
        item["redelivery_baseline_status"] = baseline_status
        if baseline is not None:
            item["redelivery_baseline"] = baseline.name
        scope_entry = scope_payloads.get(candidate_id)
        if scope_entry is not None:
            scope_path, scope_payload = scope_entry
            item["cover_only_audit_scope"] = scope_path.relative_to(
                package_root
            ).as_posix()
            try:
                validate_cover_only_audit_scope(
                    scope_payload,
                    package_root=package_root,
                    item=item,
                )
            except CoverOnlyAuditScopeError as exc:
                raise ManifestBuildError(
                    f"cover-only audit scope invalid: {candidate_id}: {exc}"
                ) from exc
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
        "generated_by": "build_recovery_review_manifest.v1",
        "date": _resolve_manifest_date(state, package_root, records_by_id),
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
        "recovery_publication_authorities_by_candidate": (
            normalized_publication_authorities
        ),
        "exact_candidate_ids": list(candidate_ids),
        **(
            {
                "partial_release_scope": {
                    "candidates": sorted(release_scope),
                    "batch_status": str(state.get("status")),
                    "authority": (
                        "维护者 2026-07-26: 没有任何纪律要求必须5个全complete"
                        "才能动BV，修复时哪个好了就可以改哪个"
                    ),
                }
            }
            if release_scope is not None
            else {}
        ),
        "counts": {"items": len(items), "talk": len(items), "song": 0},
        "cover_route_attestations": attestations,
        **(
            {
                "cover_only_audit_scope_candidate_ids": sorted(
                    scope_payloads
                )
            }
            if scope_payloads
            else {}
        ),
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
    parser.add_argument(
        "--release-candidate",
        action="append",
        default=None,
        help="per-BV partial release scope (repeatable; 维护者 2026-07-26 ruling)",
    )
    parser.add_argument(
        "--cover-only-audit-scope",
        action="append",
        type=Path,
        default=None,
        help=(
            "explicit predecessor-bound cover-only audit scope inside the "
            "package (repeatable)"
        ),
    )
    args = parser.parse_args()
    try:
        state = _load_json(args.state)
        commit = args.deployed_commit_file.read_text(encoding="utf-8")
        manifest = build_manifest(
            package_root=args.package_root,
            state=state,
            deployed_commit=commit,
            release_scope=args.release_candidate,
            cover_only_audit_scopes=args.cover_only_audit_scope,
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
