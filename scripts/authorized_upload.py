#!/usr/bin/env python3
"""Manifest-bound Li Dousha publish and existing-BV repair entry point.

``make-manifest`` freezes reviewed bytes and Ivan's authorization; ``upload``
and ``season-add`` enforce hashes, idempotency, metadata, public, Creator, and
exact-section readback.  ``repair-*`` uses a separate crash-safe journal and
can only append/edit an explicitly named existing BV; it cannot create one.
``repair-verify-live`` re-observes all four public/Creator surfaces after the
transaction reaches VERIFIED and writes a create-only completed sidecar.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import uuid
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Iterator

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.audit_lidousha_review_package import (  # noqa: E402
    AUDIT_POLICY_EPOCH,
    AUDIT_SCHEMA_VERSION,
    audit_package,
)
from src.autoslice import authorized_upload_cli_parser  # noqa: E402
from src.autoslice import authorized_upload_recovery_cli as upload_recovery  # noqa: E402
from src.autoslice import bilibili_member_api as member_api  # noqa: E402
from src.autoslice import authorized_upload_published_recovery as published_recovery  # noqa: E402
from src.autoslice.package_audit_binding import (
    audit_content_binding as _audit_content_binding,
)
from src.autoslice import final_human_review as human_review  # noqa: E402
from src.autoslice import publication_reconciliation  # noqa: E402
from src.autoslice import publication_registry  # noqa: E402
from src.autoslice import same_bv_repair as repair_binding  # noqa: E402
from src.autoslice import authorized_upload_cover_repair_cli as cover_repair_cli  # noqa: E402
from src.autoslice import cover_only_audit_scope  # noqa: E402
from src.autoslice import same_bv_live_verification  # noqa: E402
from src.autoslice import fastlane_c2_authorized_upload as c2_upload  # noqa: E402
from src.autoslice import fastlane_c1_technical_receipt as c1_projection  # noqa: E402
from src.autoslice.subtitle_validation import validate_srt_file  # noqa: E402
from src.autoslice.publication_title_exception import upload_manifest_title_policy_violations  # noqa: E402
from src.autoslice.same_bv_repair import (  # noqa: E402
    BilibiliRepairAdapter,
    PlanInvalid,
    RepairError,
    assert_bvid_unowned as assert_same_bv_unowned,
    create_plan as create_same_bv_repair_plan,
    initialise_journal as initialise_same_bv_repair_journal,
    load_plan as load_same_bv_repair_plan,
    preview_repair as preview_same_bv_repair,
    repair_status as same_bv_repair_status,
    run_repair as run_same_bv_repair,
    write_plan as write_same_bv_repair_plan,
)
from src.autoslice.same_bv_repair_cli import (  # noqa: E402
    repair_reconcile_blocked,
    repair_verify_live,
)

CookieSchemaError = member_api.CookieSchemaError
_publication_block = publication_registry.manifest_upload_block_reason
DEFAULT_BASE = Path(os.environ.get("AUTOSLICE_BASE", "/opt/bilive/autoslice"))
DEFAULT_LEDGER = DEFAULT_BASE / "reports" / "upload_ledger.jsonl"
DEFAULT_REPAIR_LEDGER = DEFAULT_BASE / "reports" / "same_bv_repair_ledger.jsonl"
DEFAULT_COVER_REPAIR_LEDGER = DEFAULT_BASE / "reports" / "same_bv_cover_repair_ledger.jsonl"
DEFAULT_UPLOAD_LOCK = DEFAULT_BASE / "upload.lock"
DEFAULT_UPLOADER = "/opt/bilive/app/tmp_manual_upload/do_upload.sh"
DEFAULT_COOKIE_JSON = Path("/opt/bilive/app/cookie.json")
DEFAULT_REPAIR_BILIUP_COOKIE_JSON = member_api.DEFAULT_BILIUP_COOKIES


def _reconcile_new_bv_publication(**kwargs) -> dict:
    try:
        return publication_reconciliation.reconcile_new_bv_publication(
            **kwargs,
            autoslice_base=DEFAULT_BASE,
            registry_path=publication_registry.DEFAULT_REGISTRY_PATH,
        )
    except OSError as exc:
        raise publication_reconciliation.PublicationReconciliationError(
            f"publication reconciliation filesystem failure: {exc}"
        ) from exc


def _reconcile_same_bv_publication(**kwargs) -> dict:
    try:
        return publication_reconciliation.reconcile_same_bv_publication(
            **kwargs,
            autoslice_base=DEFAULT_BASE,
            registry_path=publication_registry.DEFAULT_REGISTRY_PATH,
        )
    except OSError as exc:
        raise publication_reconciliation.PublicationReconciliationError(
            f"publication reconciliation filesystem failure: {exc}"
        ) from exc


def _reconcile_same_bv_cover_publication(**kwargs) -> dict:
    try:
        return publication_reconciliation.reconcile_same_bv_cover_publication(
            **kwargs,
            autoslice_base=DEFAULT_BASE,
            registry_path=publication_registry.DEFAULT_REGISTRY_PATH,
        )
    except OSError as exc:
        raise publication_reconciliation.PublicationReconciliationError(
            f"publication reconciliation filesystem failure: {exc}"
        ) from exc


# Season (合集) policy — membership is part of the publish (Ivan 2026-07-20).
# The LANE is a deterministic choke point on the frozen title: the song catalog
# prefix/catalog form is enforced by the shared publish-title validator,
# so title→lane cannot drift from content.  Season IDs are deliberately NOT
# selected by a live title query, then checked against the channel's committed
# talk/song IDs so a renamed or wrong collection cannot silently become truth.
SONG_TITLE_PREFIX = "【李豆沙】豆沙歌，"
SEASON_TITLES = {"talk": "小李切片", "song": "小李歌唱"}
SEASON_ADD_ALREADY_IN = 20080  # episodes/add: already in the season (idempotent OK)
VIEW_API = "https://api.bilibili.com/x/web-interface/view?bvid={bvid}"
TAGS_API = "https://api.bilibili.com/x/tag/archive/tags?bvid={bvid}"
SEASONS_API = "https://member.bilibili.com/x2/creative/web/seasons?pn=1&ps=30"
EPISODES_ADD_API = (
    "https://member.bilibili.com/x2/creative/web/season/section/episodes/add?csrf={csrf}"
)
SECTION_VIEW_API = "https://member.bilibili.com/x2/creative/web/season/section?id={section_id}"
MEMBER_ARCHIVE_VIEW_API = "https://member.bilibili.com/x/vupre/web/archive/view?bvid={bvid}"
CREATOR_ARCHIVES_API = (
    "https://member.bilibili.com/x/web/archives?pn={page}&ps=30&status=is_pubing,pubed,not_pubed"
)
QUOTA_FREQUENCY_CODE = 21566
ROLLING_UPLOAD_LIMIT = 10
ROLLING_UPLOAD_WINDOW_SECONDS = 24 * 60 * 60
EXPECTED_SEASON_IDS = {
    "talk": {"season_id": 8383206, "section_id": 9320779},
    "song": {"season_id": 8410735, "section_id": 9364628},
}
# 简介第一行固定项目署名（Ivan 指令：默认带上，含开源项目名与网址）。
PROJECT_ATTRIBUTION_LINE = (
    "本切片由 bilibili-vtuber-clip 项目提供："
    "https://github.com/MelanLee-shadow/bilibili-vtuber-clip"
)
SUBMISSION_DESCRIPTION = (
    PROJECT_ATTRIBUTION_LINE + "\n"
    "李豆沙个人主页：https://space.bilibili.com/1703797642\n"
    "李豆沙直播间：https://live.bilibili.com/22966160"
)
EXPECTED_TID = 21
EXPECTED_COPYRIGHT = 2
EXPECTED_SOURCE = "https://live.bilibili.com/"
TITLE_COVER_QC_SCHEMA_VERSION = "lidousha-title-cover-joint-qc.v1"
TITLE_COVER_QC_WITNESS_SCHEMA_VERSION = "cpa-frame-witness.v1"
# biliup's APP submission sends the two-line description above, while
# Bilibili's public/member archive surfaces prepend the copyright source URL.
# Freeze the observed public contract instead of treating that deterministic
# server-side projection as metadata drift.
DEFAULT_DESCRIPTION = EXPECTED_SOURCE + "\n" + SUBMISSION_DESCRIPTION
_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


class UploadLockBusy(RuntimeError):
    """Another upload/repair transaction owns the shared critical section."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# Tag policy (Ivan 2026-07-13, see scripts/suggest_upload_tags.py + memory
# lidousha-upload-tags-policy): per-archive cap 12 (empirically probed via a
# 12-tag edit on BV1EQNk6KErE), per-tag <=20 chars, no separators, no dups.
MAX_TAGS = 12
MAX_TAG_CHARS = 20


def sidecar_record_path(video: Path) -> Path:
    """Delivered clips ship a `<stem>.record.json` next to `<stem>.mp4`.

    Stems may contain dots, so strip only a known media suffix instead of
    Path.with_suffix (which would eat everything after the last dot).
    """
    name = video.name
    for ext in (".mp4", ".flv", ".mkv"):
        if name.endswith(ext):
            return video.parent / (name[: -len(ext)] + ".record.json")
    return video.parent / (name + ".record.json")


def sidecar_subtitle_path(video: Path) -> Path:
    name = video.name
    for ext in (".mp4", ".flv", ".mkv"):
        if name.endswith(ext):
            return video.parent / (name[: -len(ext)] + ".srt")
    return video.parent / (name + ".srt")


def _sha_entry(path: Path) -> dict:
    return {
        "path": str(path.resolve()),
        "sha256": sha256_file(path),
        "bytes": path.stat().st_size,
    }


def _strip_sha_prefix(value: object) -> str:
    text = str(value or "")
    return text.removeprefix("sha256:")


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _load_json_object(path: Path, label: str, problems: list[str]) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        problems.append(f"{label} unreadable: {path} ({exc})")
        return {}
    if not isinstance(value, dict):
        problems.append(f"{label} must be a JSON object: {path}")
        return {}
    return value


def _zero_blocking_issues(audit: dict) -> bool:
    blocking = audit.get("blocking_issue_count")
    issue_count = audit.get("issue_count")
    issues = audit.get("issues")
    return bool(
        isinstance(blocking, int)
        and not isinstance(blocking, bool)
        and blocking == 0
        and isinstance(issue_count, int)
        and not isinstance(issue_count, bool)
        and isinstance(issues, list)
        and issue_count == len(issues)
    )


def _resolved_manifest_item_path(root: Path, value: object) -> Path | None:
    if not isinstance(value, str) or not value:
        return None
    path = Path(value)
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def _find_review_item(review_manifest: dict, root: Path, video: Path) -> dict | None:
    expected = video.resolve()
    for item in review_manifest.get("items") or []:
        if not isinstance(item, dict):
            continue
        media = _resolved_manifest_item_path(root, item.get("media") or item.get("video"))
        if media == expected:
            return item
    return None


VERIFIED_SONG_REVIEW_SCHEMA = "lidousha-song-review-manifest.v1"
VERIFIED_SONG_DELIVERY_SCHEMA = "verified-song-delivery.v1"
VERIFIED_SONG_REVIEW_PATH_KEYS = {
    "video": ("video", "media"),
    "subtitle": ("subtitle_srt", "subtitle"),
    "record": ("record", "record_json"),
    "publish": ("publish_json", "publish"),
    "cover": ("cover",),
    "cover_title_mask": ("cover_title_mask",),
    "cover_pre_overlay": ("cover_pre_overlay",),
    "cover_route_background": ("cover_route_background",),
    "lyrics_alignment_report": ("lyrics_alignment_report",),
    "host_vocal_proof": ("host_vocal_proof",),
    "recut_manifest": ("recut_manifest",),
    "delivery_manifest": ("delivery_manifest",),
}


def _first_item_value(item: dict, keys: tuple[str, ...]) -> object:
    for key in keys:
        value = item.get(key)
        if value is not None:
            return value
    return None


def _strict_verified_song_package(
    *,
    root: Path,
    review: dict,
    review_item: dict,
    record: dict,
    audit: dict,
) -> bool:
    """Recognize only the canonical verified-Song authority envelope.

    This is deliberately stronger than a title/classification check.  The
    package must be the Song builder's no-upload schema, carry every portable
    Song proof, bind those exact bytes in the current package audit, and retain
    the manifest-last verified delivery authority.
    """

    candidate_id = str(review_item.get("candidate_id") or "")
    delivery_authority = review.get("delivery_authority")
    if (
        review.get("schema_version") != VERIFIED_SONG_REVIEW_SCHEMA
        or review.get("generated_by") != "build_lidousha_song_review_manifest.v1"
        or review.get("status") != "finished_review_package_no_upload_pending_human_review"
        or review.get("classification") != "Song"
        or review.get("candidate_id") != candidate_id
        or review.get("story_contract_required") is not False
        or review.get("run_mode") != "PRODUCTION_REVIEW"
        or review.get("upload_allowed") is not False
        or review_item.get("classification") != "Song"
        or review_item.get("kind") != "song"
        or not candidate_id
        or record.get("delivery_candidate_id") != candidate_id
        or not isinstance(delivery_authority, dict)
        or delivery_authority.get("schema_version") != VERIFIED_SONG_DELIVERY_SCHEMA
        or delivery_authority.get("upload_enabled") is not False
        or audit.get("schema_version") != AUDIT_SCHEMA_VERSION
        or audit.get("policy_epoch") != AUDIT_POLICY_EPOCH
        or audit.get("passed") is not True
        or not _zero_blocking_issues(audit)
        or Path(str(audit.get("root") or "")).resolve() != root
    ):
        return False

    audited_inputs = audit.get("audited_inputs")
    if not isinstance(audited_inputs, list):
        return False
    audited_hashes = {
        str(row.get("path")): str(row.get("sha256"))
        for row in audited_inputs
        if isinstance(row, dict)
        and isinstance(row.get("path"), str)
        and isinstance(row.get("sha256"), str)
    }
    review_path = root / "review_manifest.json"
    if not review_path.is_file() or audited_hashes.get("review_manifest.json") != sha256_file(
        review_path
    ):
        return False

    resolved_paths: dict[str, Path] = {}
    for role, keys in VERIFIED_SONG_REVIEW_PATH_KEYS.items():
        path = _resolved_manifest_item_path(root, _first_item_value(review_item, keys))
        if path is None or not path.is_file() or not _is_within(path, root):
            return False
        relative = path.relative_to(root).as_posix()
        if audited_hashes.get(relative) != sha256_file(path):
            return False
        resolved_paths[role] = path

    delivery_path = resolved_paths["delivery_manifest"]
    completion = delivery_authority.get("song_completion_evidence")
    if (
        delivery_authority.get("manifest") != delivery_path.name
        or _strip_sha_prefix(delivery_authority.get("manifest_sha256"))
        != sha256_file(delivery_path)
        or not isinstance(completion, dict)
        or completion.get("ready") is not True
        or completion.get("reason_codes") != []
        or completion.get("song_boundary_status") != "FULL_SONG_READY"
        or completion.get("lyrics_alignment_status") != "READY"
        or completion.get("host_vocal_status") != "READY"
        or completion.get("live_performance_status") != "READY"
        or completion.get("live_performance_mode") != "LIVE_STREAMER_SINGING"
        or completion.get("joint_singing_decision") != "VERIFIED_LIDOUSHA_SINGING"
        or completion.get("subtitle_source") != "external_lrc_global_shift"
    ):
        return False

    completion_hashes = {
        "video": completion.get("burned_preview_sha256"),
        "lyrics_alignment_report": completion.get("alignment_report_sha256"),
        "host_vocal_proof": completion.get("host_vocal_proof_sha256"),
        "recut_manifest": completion.get("recut_manifest_sha256"),
    }
    if any(
        _strip_sha_prefix(completion_hashes[role]) != sha256_file(resolved_paths[role])
        for role in completion_hashes
    ):
        return False

    song_problems: list[str] = []
    alignment = _load_json_object(
        resolved_paths["lyrics_alignment_report"],
        "verified Song lyrics alignment report",
        song_problems,
    )
    host_proof = _load_json_object(
        resolved_paths["host_vocal_proof"],
        "verified Song host-vocal proof",
        song_problems,
    )
    recut = _load_json_object(
        resolved_paths["recut_manifest"],
        "verified Song recut manifest",
        song_problems,
    )
    output_binding = recut.get("verified_output_binding")
    output_artifacts = output_binding.get("artifacts") if isinstance(output_binding, dict) else None
    output_proofs = output_binding.get("proofs") if isinstance(output_binding, dict) else None
    if (
        song_problems
        or alignment.get("schema_version") != "lyrics-alignment-report.v1"
        or host_proof.get("schema_version") != "host-vocal-proof.v3"
        or host_proof.get("status") != "READY"
        or host_proof.get("decision") != "LIDOUSHA_VOCAL_PRESENT_ON_LYRIC_CHECKPOINTS"
        or recut.get("schema_version") != "materialized-recut.v2"
        or recut.get("status") != "MATERIALIZED"
        or recut.get("reason_codes") != []
        or recut.get("subtitle_source") != "external_lrc_global_shift"
        or not isinstance(output_binding, dict)
        or output_binding.get("schema_version") != "verified-song-output-binding.v1"
        or not isinstance(output_artifacts, dict)
        or not isinstance(output_proofs, dict)
        or _strip_sha_prefix(output_artifacts.get("burned_media_sha256"))
        != sha256_file(resolved_paths["video"])
        or _strip_sha_prefix(output_artifacts.get("subtitle_sha256"))
        != sha256_file(resolved_paths["subtitle"])
        or _strip_sha_prefix(output_proofs.get("lyrics_alignment_report_sha256"))
        != sha256_file(resolved_paths["lyrics_alignment_report"])
        or _strip_sha_prefix(output_proofs.get("host_vocal_proof_sha256"))
        != sha256_file(resolved_paths["host_vocal_proof"])
    ):
        return False

    delivery_problems: list[str] = []
    delivery = _load_json_object(
        delivery_path, "verified Song delivery manifest", delivery_problems
    )
    delivery_artifacts = delivery.get("artifacts")
    if (
        delivery_problems
        or delivery.get("schema_version") != VERIFIED_SONG_DELIVERY_SCHEMA
        or delivery.get("status") != "DELIVERED_NO_UPLOAD"
        or delivery.get("candidate_id") != candidate_id
        or delivery.get("upload_enabled") is not False
        or not isinstance(delivery_artifacts, dict)
        or not set(VERIFIED_SONG_REVIEW_PATH_KEYS).issubset(
            set(delivery_artifacts) | {"delivery_manifest", "record"}
        )
        or "active_record" not in delivery_artifacts
    ):
        return False
    item_hashes = review_item.get("sha256")
    if not isinstance(item_hashes, dict):
        return False
    for review_role, path in resolved_paths.items():
        if review_role == "delivery_manifest":
            continue
        delivery_role = "active_record" if review_role == "record" else review_role
        entry = delivery_artifacts.get(delivery_role)
        actual = sha256_file(path)
        if (
            not isinstance(entry, dict)
            or _strip_sha_prefix(entry.get("sha256")) != actual
            or _strip_sha_prefix(entry.get("source_sha256")) != actual
            or _strip_sha_prefix(item_hashes.get(delivery_role)) != actual
        ):
            return False
    return True


def _record_artifact_hash_problems(
    record: dict,
    *,
    video: Path,
    cover: Path,
    subtitle: Path,
    title: str,
    story_contract_required: bool = True,
) -> list[str]:
    problems: list[str] = []
    artifact_hashes = record.get("artifact_hashes")
    if not isinstance(artifact_hashes, dict):
        return ["record.json has no artifact_hashes object"]

    expected = {
        "video": sha256_file(video),
        "cover": sha256_file(cover),
        "subtitle": sha256_file(subtitle),
    }
    accepted_video = {
        _strip_sha_prefix(artifact_hashes.get("burned_video_sha256")),
        _strip_sha_prefix(artifact_hashes.get("video_sha256")),
    }
    if expected["video"] not in accepted_video:
        problems.append("record artifact hashes do not bind the reviewed video")
    if expected["cover"] != _strip_sha_prefix(artifact_hashes.get("cover_sha256")):
        problems.append("record artifact hashes do not bind the reviewed cover")
    accepted_subtitle = {
        _strip_sha_prefix(artifact_hashes.get("delivery_subtitle_sha256")),
        _strip_sha_prefix(artifact_hashes.get("subtitle_sha256")),
    }
    if expected["subtitle"] not in accepted_subtitle:
        problems.append("record artifact hashes do not bind the reviewed SRT")

    publish_staging = record.get("publish_staging")
    record_title = publish_staging.get("title") if isinstance(publish_staging, dict) else None
    if record_title != title:
        problems.append(
            f"record publish title mismatch: record={record_title!r} manifest={title!r}"
        )
    if story_contract_required:
        story_contract = record.get("story_contract")
        if not isinstance(story_contract, dict):
            problems.append("record.json has no story_contract object")
        else:
            if not str(story_contract.get("schema_version") or "").strip():
                problems.append("record story_contract has no schema_version")
            if not str(story_contract.get("candidate_id") or "").strip():
                problems.append("record story_contract has no candidate_id")
            if not str(story_contract.get("transcript_sha256") or "").strip():
                problems.append("record story_contract has no transcript_sha256")
    return problems


def _validate_v3_package_attestation(
    manifest: dict,
    *,
    verify_hashes: bool,
    live_policy_recheck: bool = True,
) -> list[str]:
    problems: list[str] = []
    attestation = manifest.get("package_attestation")
    if not isinstance(attestation, dict):
        return ["manifest v3 has no package_attestation object"]
    if "c1_technical_receipt" in attestation:
        try:
            c1_projection.validate_authorized_projection_manifest(manifest)
        except c1_projection.C1TechnicalReceiptError as exc:
            return [f"C1 authorized projection rejected: {exc}"]
        return []
    if attestation.get("schema_version") != "authorized-upload-package-attestation.v1":
        problems.append("package_attestation schema_version is invalid")

    entries: dict[str, tuple[Path, dict]] = {}
    for key in ("package_audit", "review_manifest", "record", "subtitle"):
        entry = attestation.get(key)
        if not isinstance(entry, dict):
            problems.append(f"package_attestation.{key} is missing")
            continue
        path = Path(str(entry.get("path") or ""))
        entries[key] = (path, entry)
        if not path.is_file():
            problems.append(f"package_attestation.{key} missing: {path}")
            continue
        if verify_hashes:
            actual = sha256_file(path)
            if actual != entry.get("sha256"):
                problems.append(
                    f"package_attestation.{key} HASH DRIFT: "
                    f"manifest={str(entry.get('sha256'))[:12]} actual={actual[:12]} ({path})"
                )

    video_entry = manifest.get("video") or {}
    cover_entry = manifest.get("cover") or {}
    video = Path(str(video_entry.get("path") or ""))
    cover = Path(str(cover_entry.get("path") or ""))
    if not video.is_file() or not cover.is_file():
        return problems
    root_text = attestation.get("package_root")
    root = Path(str(root_text or "")).resolve()
    if not root_text or not root.is_dir():
        problems.append(f"package root missing: {root}")
        return problems
    problems.extend(published_recovery.attestation_problems(attestation, root, live_authority_recheck=live_policy_recheck))
    if video.parent.resolve() != root:
        problems.append("reviewed video is not directly inside the audited package root")
    if not _is_within(cover, root):
        problems.append("reviewed cover is outside the audited package root")
    expected_stem = video.stem
    expected_paths = {
        "record": root / f"{expected_stem}.record.json",
        "subtitle": root / f"{expected_stem}.srt",
    }
    if cover.resolve() != (root / f"{expected_stem}.cover.png").resolve():
        problems.append("cover is not the same-stem <stem>.cover.png")
    for key, expected_path in expected_paths.items():
        current = entries.get(key)
        if current and current[0].resolve() != expected_path.resolve():
            problems.append(f"{key} is not the same-stem {expected_path.name}")

    audit_path = entries.get("package_audit", (Path(), {}))[0]
    audit = _load_json_object(audit_path, "package audit", problems) if audit_path.is_file() else {}
    audit_root = Path(str(audit.get("root") or "")).resolve()
    if audit.get("schema_version") != AUDIT_SCHEMA_VERSION:
        problems.append("package audit schema_version is stale or invalid")
    if audit.get("policy_epoch") != AUDIT_POLICY_EPOCH:
        problems.append("package audit policy_epoch is stale or invalid")
    if audit.get("passed") is not True:
        problems.append("package audit did not pass")
    if audit_root != root:
        problems.append(f"package audit root mismatch: audit={audit_root} manifest={root}")
    if not _zero_blocking_issues(audit):
        problems.append("package audit reports blocking issues")
    # 冻结计划恢复（repair-run resume）不做 live 政策重算：计划创建时已做
    # 过一次 canonical 校验并按 sha 冻结全部产物；平台审核等待期间的政策
    # 演进（1573 案，2026-07-27）不得让已冻结的置换事务失去可恢复性。
    # 哈希不可变性检查（上方 verify_hashes）在恢复路径照常执行。
    if live_policy_recheck and root.is_dir():
        current_audit = audit_package(root)
        if current_audit.get("passed") is not True:
            problems.append("canonical package auditor currently rejects the package")
        if _audit_content_binding(audit) != _audit_content_binding(current_audit):
            problems.append(
                "package audit is not the canonical current-policy result for the "
                "current package input closure"
            )

    review_path = entries.get("review_manifest", (Path(), {}))[0]
    review = (
        _load_json_object(review_path, "review manifest", problems) if review_path.is_file() else {}
    )
    problems.extend(human_review.final_human_review_attestation_problems(manifest))
    review_item = _find_review_item(review, root, video)
    if review_item is None:
        problems.append("review_manifest has no item for the reviewed video")
    else:
        expected_item_paths = {
            "cover": cover.resolve(),
            "record": expected_paths["record"].resolve(),
            "subtitle": expected_paths["subtitle"].resolve(),
        }
        item_paths = {
            "cover": _resolved_manifest_item_path(root, review_item.get("cover")),
            "record": _resolved_manifest_item_path(
                root, review_item.get("record") or review_item.get("record_json")
            ),
            "subtitle": _resolved_manifest_item_path(
                root, review_item.get("subtitle_srt") or review_item.get("subtitle")
            ),
        }
        for key, expected_path in expected_item_paths.items():
            if item_paths[key] != expected_path:
                problems.append(
                    f"review_manifest {key} does not match the reviewed same-stem artifact"
                )
        if review_item.get("title") != manifest.get("title"):
            problems.append("review_manifest title does not match the upload title")
    problems.extend(
        _cover_only_audit_scope_attestation_problems(
            manifest,
            review_item=review_item,
        )
    )

    record_path = entries.get("record", (Path(), {}))[0]
    subtitle_path = entries.get("subtitle", (Path(), {}))[0]
    if record_path.is_file() and subtitle_path.is_file():
        subtitle_verdict = validate_srt_file(subtitle_path)
        if subtitle_verdict.get("status") != "PASS":
            codes = [
                str(row.get("code") or "SRT_RELEASE_VALIDATION_FAILED")
                for row in subtitle_verdict.get("errors") or []
                if isinstance(row, dict)
            ]
            problems.append("reviewed SRT fails release validation: " + ",".join(codes))
        record = _load_json_object(record_path, "record", problems)
        problems.extend(
            repair_binding.recovery_publication_package_problems(manifest, record, review_item)
        )
        verified_song = bool(
            isinstance(review_item, dict)
            and _strict_verified_song_package(
                root=root,
                review=review,
                review_item=review_item,
                record=record,
                audit=audit,
            )
        )
        problems.extend(
            _record_artifact_hash_problems(
                record,
                video=video,
                cover=cover,
                subtitle=subtitle_path,
                title=str(manifest.get("title") or ""),
                story_contract_required=not verified_song
                and c2_upload.verified_c2_release_candidate_id(root, record) is None,
            )
        )
        record_tags = (record.get("upload_tags") or {}).get("final_tags")
        if record_tags != manifest.get("tags"):
            problems.append("manifest tags do not exactly match record.upload_tags.final_tags")
    problems.extend(_title_cover_qc_attestation_problems(manifest))
    return problems


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _strict_bool(value: object, expected: bool) -> bool:
    return isinstance(value, bool) and value is expected


def _title_cover_qc_required(manifest: dict) -> bool:
    """New-BV manifests need joint QC; exact same-BV uses its v2 review lane."""

    attestation = manifest.get("package_attestation")
    final_review = attestation.get("final_human_review") if isinstance(attestation, dict) else None
    c1_review = attestation.get("c1_technical_receipt") if isinstance(attestation, dict) else None
    return not (
        isinstance(manifest.get("recovery_publication_authority"), dict)
        and (isinstance(final_review, dict) or isinstance(c1_review, dict))
    )


def _title_cover_qc_attestation_problems(
    manifest: dict,
    *,
    required: bool | None = None,
) -> list[str]:
    """Replay the CPA title+final-cover receipt against current manifest bytes."""

    problems: list[str] = []
    attestation = manifest.get("package_attestation")
    if not isinstance(attestation, dict):
        return ["manifest v3 has no package_attestation object"]
    entry = attestation.get("title_cover_qc")
    must_exist = _title_cover_qc_required(manifest) if required is None else required
    if not isinstance(entry, dict):
        if must_exist:
            problems.append(
                "new-BV manifest requires package_attestation.title_cover_qc from --title-cover-qc"
            )
        return problems

    receipt_path = Path(str(entry.get("path") or ""))
    if not receipt_path.is_file():
        return [f"package_attestation.title_cover_qc missing: {receipt_path}"]
    actual_sha = sha256_file(receipt_path)
    if entry.get("sha256") != actual_sha:
        problems.append(
            "package_attestation.title_cover_qc HASH DRIFT: "
            f"manifest={str(entry.get('sha256'))[:12]} "
            f"actual={actual_sha[:12]} ({receipt_path})"
        )
    actual_bytes = receipt_path.stat().st_size
    if (
        isinstance(entry.get("bytes"), bool)
        or not isinstance(entry.get("bytes"), int)
        or entry.get("bytes") != actual_bytes
    ):
        problems.append(
            "package_attestation.title_cover_qc byte-size drift: "
            f"manifest={entry.get('bytes')!r} actual={actual_bytes}"
        )

    receipt = _load_json_object(receipt_path, "title+cover joint-QC receipt", problems)
    if not receipt:
        return problems
    if receipt.get("schema_version") != TITLE_COVER_QC_SCHEMA_VERSION:
        problems.append("title+cover joint-QC schema_version is invalid")

    title = str(manifest.get("title") or "")
    expected_title_sha = "sha256:" + _sha256_text(title)
    if receipt.get("title") != title:
        problems.append("title+cover joint-QC title does not match manifest title")
    if receipt.get("title_sha256") != expected_title_sha:
        problems.append("title+cover joint-QC title_sha256 does not bind manifest title")

    cover_entry = manifest.get("cover")
    cover_entry = cover_entry if isinstance(cover_entry, dict) else {}
    cover_path = Path(str(cover_entry.get("path") or ""))
    if receipt.get("cover_path") != str(cover_path.resolve()):
        problems.append("title+cover joint-QC cover_path does not bind final cover")
    expected_cover_sha = str(cover_entry.get("sha256") or "")
    if receipt.get("cover_sha256") != "sha256:" + expected_cover_sha:
        problems.append("title+cover joint-QC cover_sha256 does not bind final cover")

    record_entry = attestation.get("record")
    record_entry = record_entry if isinstance(record_entry, dict) else {}
    record_path = Path(str(record_entry.get("path") or ""))
    record_problems: list[str] = []
    record = (
        _load_json_object(record_path, "record", record_problems) if record_path.is_file() else {}
    )
    problems.extend(record_problems)
    package_root = Path(str(attestation.get("package_root") or "")).resolve()
    expected_candidate = c2_upload.candidate_id_from_record(package_root, record)
    if not expected_candidate:
        problems.append("title+cover joint-QC cannot resolve candidate from record")
    elif receipt.get("candidate_id") != expected_candidate:
        problems.append("title+cover joint-QC candidate_id does not match record")

    if receipt.get("selected_provider") != "cpa":
        problems.append("title+cover joint-QC selected_provider must be cpa")
    preferred_provider = receipt.get("preferred_provider")
    if preferred_provider is not None and preferred_provider != "cpa":
        problems.append("title+cover joint-QC preferred_provider must be cpa")

    witness = receipt.get("witness")
    if not isinstance(witness, dict):
        problems.append("title+cover joint-QC has no CPA witness object")
    else:
        if witness.get("schema_version") != TITLE_COVER_QC_WITNESS_SCHEMA_VERSION:
            problems.append("title+cover joint-QC witness schema_version is invalid")
        if witness.get("provider") != "cpa":
            problems.append("title+cover joint-QC witness provider must be cpa")
        if witness.get("status") != "OBSERVED":
            problems.append("title+cover joint-QC CPA witness was not OBSERVED")
        if not str(witness.get("model") or "").strip():
            problems.append("title+cover joint-QC CPA witness has no model")
        if witness.get("image_path") != str(cover_path.resolve()):
            problems.append("title+cover joint-QC witness image_path is not final cover")
        if _strip_sha_prefix(witness.get("image_sha256")) != expected_cover_sha:
            problems.append("title+cover joint-QC witness image_sha256 is not final cover")

    verdict = receipt.get("verdict")
    if not isinstance(verdict, dict):
        problems.append("title+cover joint-QC has no verdict object")
        verdict = {}
    expected_bools = {
        "lidousha_primary": True,
        "thumbnail_readable": True,
        "single_clear_hook": True,
        "text_overcrowded": False,
        "title_cover_aligned": True,
        "pass": True,
    }
    for key, expected in expected_bools.items():
        if not _strict_bool(verdict.get(key), expected):
            problems.append(f"title+cover joint-QC verdict.{key} must be {str(expected).lower()}")
    line_count = verdict.get("physical_text_line_count")
    if isinstance(line_count, bool) or not isinstance(line_count, int) or line_count not in (1, 2):
        problems.append("title+cover joint-QC verdict.physical_text_line_count must be 1 or 2")
    if verdict.get("unrelated_or_misleading_elements") != []:
        problems.append(
            "title+cover joint-QC verdict.unrelated_or_misleading_elements must be empty"
        )
    if not str(verdict.get("reason") or "").strip():
        problems.append("title+cover joint-QC verdict.reason must be non-empty")

    if isinstance(witness, dict):
        answer = witness.get("answer")
        try:
            answer_verdict = json.loads(answer) if isinstance(answer, str) else None
        except ValueError:
            answer_verdict = None
        if answer_verdict != verdict:
            problems.append(
                "title+cover joint-QC verdict is not the exact parsed CPA witness answer"
            )

    if receipt.get("status") != "PASS":
        problems.append("title+cover joint-QC status must be PASS")
    if not _strict_bool(receipt.get("pass"), True):
        problems.append("title+cover joint-QC top-level pass must be true")
    return problems


def _attach_title_cover_qc(
    manifest: dict,
    receipt_arg: str | None,
) -> list[str]:
    attestation = manifest.get("package_attestation")
    if not isinstance(attestation, dict):
        return ["manifest v3 has no package_attestation object"]
    if receipt_arg:
        receipt_path = Path(receipt_arg).resolve()
        if not receipt_path.is_file():
            return [f"title+cover joint-QC receipt missing: {receipt_path}"]
        attestation["title_cover_qc"] = _sha_entry(receipt_path)
    return []


def _attach_cover_only_audit_scope(
    manifest: dict,
    review_manifest: dict,
    video: Path,
) -> list[str]:
    """Freeze an explicitly declared cover-only audit bridge, if present."""

    attestation = manifest.get("package_attestation")
    if not isinstance(attestation, dict):
        return ["manifest v3 has no package_attestation object"]
    root = Path(str(attestation.get("package_root") or "")).resolve()
    item = _find_review_item(review_manifest, root, video)
    if item is None or item.get("cover_only_audit_scope") is None:
        return []
    scope_path = _resolved_manifest_item_path(root, item.get("cover_only_audit_scope"))
    if scope_path is None or scope_path.is_symlink() or not scope_path.is_file():
        return [f"cover-only audit scope missing or unsafe: {scope_path}"]
    try:
        scope_path.relative_to(root)
    except ValueError:
        return ["cover-only audit scope escapes the reviewed package root"]
    attestation["cover_only_audit_scope"] = _sha_entry(scope_path)
    return []


def _cover_only_audit_scope_attestation_problems(
    manifest: dict,
    *,
    review_item: dict | None,
) -> list[str]:
    """Replay the narrow bridge and bind it to upload metadata/authorization."""

    problems: list[str] = []
    attestation = manifest.get("package_attestation")
    if not isinstance(attestation, dict):
        return ["manifest v3 has no package_attestation object"]
    entry = attestation.get("cover_only_audit_scope")
    item_declares = bool(
        isinstance(review_item, dict) and review_item.get("cover_only_audit_scope") is not None
    )
    if entry is None and not item_declares:
        return []
    if not isinstance(entry, dict) or not item_declares:
        return [
            "package_attestation.cover_only_audit_scope and review item must be declared together"
        ]
    root = Path(str(attestation.get("package_root") or "")).resolve()
    path = Path(str(entry.get("path") or ""))
    if path.is_symlink() or not path.is_absolute() or not path.is_file():
        return [f"package_attestation.cover_only_audit_scope missing: {path}"]
    try:
        path.resolve().relative_to(root)
    except ValueError:
        problems.append("cover-only audit scope escapes package root")
    actual_sha = sha256_file(path)
    if entry.get("sha256") != actual_sha:
        problems.append(
            "package_attestation.cover_only_audit_scope HASH DRIFT: "
            f"manifest={str(entry.get('sha256'))[:12]} "
            f"actual={actual_sha[:12]} ({path})"
        )
    if entry.get("bytes") != path.stat().st_size:
        problems.append("package_attestation.cover_only_audit_scope byte-size drift")
    expected_path = _resolved_manifest_item_path(root, review_item.get("cover_only_audit_scope"))
    if expected_path != path.resolve():
        problems.append("package_attestation.cover_only_audit_scope differs from review item")
    scope_problems: list[str] = []
    scope = _load_json_object(path, "cover-only audit scope", scope_problems)
    problems.extend(scope_problems)
    if scope:
        try:
            normalized = cover_only_audit_scope.validate_scope(
                scope,
                package_root=root,
                item=review_item,
            )
        except cover_only_audit_scope.CoverOnlyAuditScopeError as exc:
            problems.append(f"cover-only audit scope rejected: {exc}")
        else:
            scope_auth = normalized.get("authorization") or {}
            manifest_auth = manifest.get("authorization") or {}
            if scope_auth != {
                "by": manifest_auth.get("by"),
                "quote": manifest_auth.get("quote"),
            }:
                problems.append("cover-only audit scope authorization differs from manifest")
            frozen = normalized.get("frozen_noncover") or {}
            if (
                frozen.get("title") != manifest.get("title")
                or frozen.get("description") != manifest.get("description")
                or frozen.get("tags") != manifest.get("tags")
                or frozen.get("publish_policy") != manifest.get("publish_policy")
            ):
                problems.append("cover-only audit scope frozen metadata differs from manifest")
            scope_video = frozen.get("video") or {}
            manifest_video = manifest.get("video") or {}
            current_package = normalized.get("current_package") or {}
            scope_cover = current_package.get("cover") or {}
            manifest_cover = manifest.get("cover") or {}
            if any(scope_video.get(key) != manifest_video.get(key) for key in ("sha256", "bytes")):
                problems.append("cover-only audit scope video differs from manifest")
            if any(scope_cover.get(key) != manifest_cover.get(key) for key in ("sha256", "bytes")):
                problems.append("cover-only audit scope cover differs from manifest")
    return problems


def validate_tags(tags: list[str]) -> list[str]:
    """Return problems; empty list means the tag list is manifest-worthy."""
    problems: list[str] = []
    if not isinstance(tags, list) or any(not isinstance(t, str) for t in tags):
        return ["tags must be a list of strings"]
    cleaned = [t.strip() for t in tags]
    if any(not t for t in cleaned):
        problems.append("tags contain an empty item")
    if len(cleaned) > MAX_TAGS:
        problems.append(f"{len(cleaned)} tags exceed the cap of {MAX_TAGS}")
    if len({t.casefold() for t in cleaned}) != len(cleaned):
        problems.append("tags contain duplicates")
    for tag in cleaned:
        if len(tag) > MAX_TAG_CHARS:
            problems.append(f"tag too long (>{MAX_TAG_CHARS} chars): {tag!r}")
        if any(ch in tag for ch in ",，\n\t"):
            problems.append(f"tag contains a separator character: {tag!r}")
    return problems


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def derive_season_lane(title: str) -> str:
    """talk|song from the frozen title — the song catalog prefix is the choke point."""
    return "song" if title.startswith(SONG_TITLE_PREFIX) else "talk"


def season_block_for(title: str, choice: str) -> dict | None:
    """The manifest's frozen season binding.  ``none`` opts out explicitly;
    an explicit talk/song that contradicts the title-derived lane is refused
    (song titles publish to 小李歌唱, everything else to 小李切片 — no exceptions
    without changing the title first)."""
    derived = derive_season_lane(title)
    if choice == "none":
        return None
    if choice == "auto":
        lane = derived
    elif choice in SEASON_TITLES:
        if choice != derived:
            raise ValueError(
                f"--season {choice} contradicts the title-derived lane {derived!r}"
                " — the title decides the season; fix the title instead"
            )
        lane = choice
    else:
        raise ValueError(f"unknown season choice {choice!r}")
    return {"lane": lane, "season_title": SEASON_TITLES[lane], "source": f"{choice}:title-prefix"}


def validate_season_block(block: object) -> list[str]:
    if block is None:
        return []
    if not isinstance(block, dict):
        return ["season block must be an object or null"]
    lane = block.get("lane")
    if lane not in SEASON_TITLES:
        return [f"season lane must be one of {sorted(SEASON_TITLES)}: {lane!r}"]
    if block.get("season_title") != SEASON_TITLES[lane]:
        return [f"season title for lane {lane!r} must be {SEASON_TITLES[lane]!r}"]
    return []


def effective_season_block(manifest: dict) -> tuple[dict | None, str]:
    """(block, provenance).  Legacy manifests (pre-2026-07-20, no season key)
    derive the lane from the frozen title so the completion contract still
    applies to them."""
    if "season" in manifest:
        return manifest["season"], "manifest"
    return season_block_for(str(manifest.get("title") or ""), "auto"), "derived-from-frozen-title"


def _build_season_http(cookie_json: Path):
    """(http, csrf) using the production bilibili cookie file.

    ``http(url, data=None, is_json=False) -> dict`` — member.* endpoints get the
    cookie jar; the public view/tags API only needs a browser UA.  Cookie values
    are never printed or embedded in results."""
    session = member_api.BiliSession(cookie_path=cookie_json)
    jar = session.cookie_header
    csrf = session.csrf

    def http(url: str, data: dict | None = None, is_json: bool = False) -> dict:
        headers = {"User-Agent": _BROWSER_UA}
        if "member.bilibili.com" in url:
            headers["Cookie"] = jar
            headers["Referer"] = "https://member.bilibili.com/"
        body: bytes | None = None
        if data is not None:
            if is_json:
                body = json.dumps(data).encode("utf-8")
                headers["Content-Type"] = "application/json"
            else:
                body = urllib.parse.urlencode(data).encode("utf-8")
        request = urllib.request.Request(url, data=body, headers=headers)
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)

    return http, csrf


def season_add_flow(
    manifest: dict,
    bvid: str,
    *,
    http,
    csrf: str,
    wait_seconds: float = 900.0,
    poll_seconds: float = 30.0,
    display_wait_seconds: float = 240.0,
    sleeper=time.sleep,
) -> dict:
    """Add the archive to its manifest-bound season and PUBLICLY verify it.

    Returns an evidence dict whose ``status`` is the completion truth:
    IN_SEASON_PUBLIC is the only success; everything else means the publish is
    not finished (re-run ``season-add``).  API pitfalls encoded here, from the
    2026-06-22/07-04 incidents: episodes/add wants camelCase ``sectionId`` +
    ``episodes`` (snake_case returns code 0 without taking effect — which is why
    this flow re-reads the PUBLIC view instead of trusting code 0), season/switch
    is dead (-404), and 20080 means already-in-season (idempotent success)."""
    block, provenance = effective_season_block(manifest)
    result: dict = {
        "schema_version": "authorized-upload-season-verify.v1",
        "bvid": bvid,
        "title": manifest.get("title"),
        "season_binding": block,
        "season_binding_source": provenance,
        "verified_at": now(),
    }
    if block is None:
        result["status"] = "SEASON_OPTED_OUT"
        return result
    expected_season = block["season_title"]

    deadline = time.monotonic() + max(0.0, wait_seconds)
    view_data: dict = {}
    while True:
        view = http(VIEW_API.format(bvid=bvid))
        view_data = view.get("data") or {}
        state = view_data.get("state")
        result["last_view_code"], result["state"] = view.get("code"), state
        if view.get("code") == 0 and state == 0:
            break
        if time.monotonic() >= deadline:
            result["status"] = "PENDING_TRANSCODE"
            return result
        sleeper(poll_seconds)
    aid, cid = view_data.get("aid"), view_data.get("cid")
    result["aid"], result["cid"] = aid, cid
    if not aid or not cid:
        result["status"] = "PENDING_VIEW_INCOMPLETE"
        return result

    seasons = http(SEASONS_API)
    season_id = section_id = None
    for entry in (seasons.get("data") or {}).get("seasons") or []:
        season = entry.get("season") or {}
        if season.get("title") != expected_season:
            continue
        sections = (entry.get("sections") or {}).get("sections") or []
        chosen = next((s for s in sections if s.get("title") == "正片"), None) or (
            sections[0] if sections else None
        )
        if chosen:
            season_id, section_id = season.get("id"), chosen.get("id")
        break
    result["season_id"], result["section_id"] = season_id, section_id
    if not season_id or not section_id:
        result["status"] = "SEASON_NOT_FOUND"
        return result
    expected_ids = EXPECTED_SEASON_IDS.get(str(block.get("lane") or ""))
    if expected_ids and (
        season_id != expected_ids["season_id"] or section_id != expected_ids["section_id"]
    ):
        result["expected_season_id"] = expected_ids["season_id"]
        result["expected_section_id"] = expected_ids["section_id"]
        result["status"] = "SEASON_ID_MISMATCH"
        return result

    add = http(
        EPISODES_ADD_API.format(csrf=csrf),
        data={
            "sectionId": section_id,
            "episodes": [
                {"aid": aid, "cid": cid, "title": manifest.get("title"), "charging_pay": 0}
            ],
        },
        is_json=True,
    )
    result["season_add_code"], result["season_add_message"] = add.get("code"), add.get("message")
    if add.get("code") not in (0, SEASON_ADD_ALREADY_IN):
        result["status"] = f"ADD_FAILED_{add.get('code')}"
        return result

    display_deadline = time.monotonic() + max(0.0, display_wait_seconds)
    while True:
        view = http(VIEW_API.format(bvid=bvid))
        data = view.get("data") or {}
        ugc_season = (data.get("ugc_season") or {}).get("title")
        displayed = bool(data.get("is_season_display"))
        result["ugc_season_title"], result["is_season_display"] = ugc_season, displayed
        if view.get("code") == 0 and ugc_season == expected_season and displayed:
            break
        if time.monotonic() >= display_deadline:
            result["status"] = "PENDING_DISPLAY"
            return result
        sleeper(poll_seconds)
    result["verified_at"] = now()
    result["status"] = "IN_SEASON_PUBLIC"
    return result


def season_verify_sidecar_path(manifest_path: Path) -> Path:
    name = manifest_path.name
    stem = name[: -len(".upload_manifest.json")] if name.endswith(".upload_manifest.json") else name
    return manifest_path.parent / (stem + ".season_verify.json")


def public_verify_sidecar_path(manifest_path: Path) -> Path:
    name = manifest_path.name
    stem = name[: -len(".upload_manifest.json")] if name.endswith(".upload_manifest.json") else name
    return manifest_path.parent / (stem + ".public_verify.json")


def uploaded_sidecar_path(manifest_path: Path) -> Path:
    name = manifest_path.name
    stem = name[: -len(".upload_manifest.json")] if name.endswith(".upload_manifest.json") else name
    return manifest_path.parent / (stem + ".uploaded.json")


def _normalise_tags(value: object) -> list[str]:
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, list):
        return [str(part).strip() for part in value if str(part).strip()]
    return []


def _section_episode_rows(payload: object) -> list[dict]:
    rows: list[dict] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key == "episodes" and isinstance(value, list):
                rows.extend(row for row in value if isinstance(row, dict))
            else:
                rows.extend(_section_episode_rows(value))
    elif isinstance(payload, list):
        for value in payload:
            rows.extend(_section_episode_rows(value))
    return rows


def _section_episode_title(row: dict) -> str:
    """Read the title from the known exact-section response shapes."""

    for key in ("title", "episode_title"):
        value = row.get(key)
        if isinstance(value, str):
            return value
    for key in ("archive", "arc"):
        nested = row.get(key)
        if isinstance(nested, dict) and isinstance(nested.get("title"), str):
            return str(nested["title"])
    return ""


def _section_ids(payload: object) -> set[int]:
    ids: set[int] = set()
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key in {"section_id", "sectionId"} and isinstance(value, int):
                ids.add(value)
            elif (
                key == "id"
                and isinstance(value, int)
                and ("episodes" in payload or "season_id" in payload or "seasonId" in payload)
            ):
                ids.add(value)
            ids.update(_section_ids(value))
    elif isinstance(payload, list):
        for value in payload:
            ids.update(_section_ids(value))
    return ids


def public_verify_flow(
    manifest: dict,
    bvid: str,
    *,
    http,
    season_result: dict,
    wait_seconds: float = 240.0,
    poll_seconds: float = 30.0,
    sleeper=time.sleep,
) -> dict:
    """Verify the public/member/tag/EXACT-section surfaces as one contract."""
    expected_tags = list(manifest.get("tags") or [])
    result: dict = {
        "schema_version": "authorized-upload-public-verify.v2",
        "bvid": bvid,
        "manifest_title": manifest.get("title"),
        "expected": {
            "title": manifest.get("title"),
            "description": manifest.get("description"),
            "tags": expected_tags,
            "tid": EXPECTED_TID,
            "copyright": EXPECTED_COPYRIGHT,
            "source": EXPECTED_SOURCE,
            "season_id": season_result.get("season_id"),
            "section_id": season_result.get("section_id"),
        },
        "verified_at": now(),
    }
    deadline = time.monotonic() + max(0.0, wait_seconds)
    while True:
        problems: list[str] = []
        public: dict = {}
        public_data: dict = {}
        public_tags: list[str] = []
        archive: dict = {}
        section: dict = {}
        try:
            public = http(VIEW_API.format(bvid=bvid))
            public_data = public.get("data") or {}
            tags_payload = http(TAGS_API.format(bvid=bvid))
            public_tags = [
                str(row.get("tag_name") or "").strip()
                for row in (tags_payload.get("data") or [])
                if isinstance(row, dict) and str(row.get("tag_name") or "").strip()
            ]
            member = http(MEMBER_ARCHIVE_VIEW_API.format(bvid=bvid))
            member_data = member.get("data") or {}
            archive = member_data.get("archive") or {}
            section_id = season_result.get("section_id")
            section = (
                http(SECTION_VIEW_API.format(section_id=section_id))
                if section_id
                else {"code": 0, "data": {}}
            )
        except Exception as exc:
            problems.append(f"verification API error: {exc}")

        result["public_view"] = {
            "code": public.get("code"),
            "state": public_data.get("state"),
            "aid": public_data.get("aid"),
            "cid": public_data.get("cid"),
            "title": public_data.get("title"),
            "desc": public_data.get("desc"),
            "tid": public_data.get("tid"),
            "copyright": public_data.get("copyright"),
            "ugc_season_id": (public_data.get("ugc_season") or {}).get("id"),
            "ugc_season_title": (public_data.get("ugc_season") or {}).get("title"),
            "is_season_display": public_data.get("is_season_display"),
        }
        result["public_tags"] = public_tags
        result["member_archive"] = {
            key: archive.get(key)
            for key in ("aid", "bvid", "title", "desc", "tag", "tid", "copyright", "source")
        }
        result["section_api"] = {
            "url": SECTION_VIEW_API.format(section_id=season_result.get("section_id")),
            "code": section.get("code") if isinstance(section, dict) else None,
        }

        expected_title = manifest.get("title")
        expected_desc = manifest.get("description")
        if public_data.get("state") != 0:
            problems.append(f"public state is {public_data.get('state')!r}, expected 0")
        for field, expected in (
            ("title", expected_title),
            ("desc", expected_desc),
            ("tid", EXPECTED_TID),
            ("copyright", EXPECTED_COPYRIGHT),
        ):
            if public_data.get(field) != expected:
                problems.append(f"public {field} mismatch")
        if public_tags != expected_tags and set(public_tags) != set(expected_tags):
            problems.append("public tags mismatch")
        for field, expected in (
            ("title", expected_title),
            ("desc", expected_desc),
            ("tid", EXPECTED_TID),
            ("copyright", EXPECTED_COPYRIGHT),
            ("source", EXPECTED_SOURCE),
        ):
            if archive.get(field) != expected:
                problems.append(f"Creator archive {field} mismatch")
        member_tags = _normalise_tags(archive.get("tag"))
        if member_tags != expected_tags and set(member_tags) != set(expected_tags):
            problems.append("Creator archive tags mismatch")
        if archive.get("bvid") not in (None, bvid):
            problems.append("Creator archive BVID mismatch")

        block, _ = effective_season_block(manifest)
        if block is not None:
            season_id = season_result.get("season_id")
            section_id = season_result.get("section_id")
            if (public_data.get("ugc_season") or {}).get("id") != season_id:
                problems.append("public season id mismatch")
            if (public_data.get("ugc_season") or {}).get("title") != block.get("season_title"):
                problems.append("public season title mismatch")
            if public_data.get("is_season_display") is not True:
                problems.append("public season is not displayed")
            section_ids = _section_ids(section)
            if section_ids and section_id not in section_ids:
                problems.append("exact section API returned a different section")
            aid = public_data.get("aid")
            episodes = _section_episode_rows(section)
            membership = [
                row for row in episodes if row.get("aid") == aid or row.get("bvid") == bvid
            ]
            result["section_api"]["episode_match_count"] = len(membership)
            result["section_api"]["section_ids_seen"] = sorted(section_ids)
            result["section_api"]["episode_titles"] = [
                _section_episode_title(row) for row in membership
            ]
            if not membership:
                problems.append("aid/BVID absent from exact section API")
            elif len(membership) != 1:
                problems.append("aid/BVID appears more than once in exact section API")
            elif _section_episode_title(membership[0]) != expected_title:
                problems.append("exact section episode title mismatch")

        result["problems"] = problems
        if not problems:
            result["status"] = "VERIFIED_PUBLIC"
            result["verified_at"] = now()
            return result
        if time.monotonic() >= deadline:
            result["status"] = "PUBLIC_VERIFY_FAILED"
            result["verified_at"] = now()
            return result
        sleeper(poll_seconds)


def _parse_ledger_time(value: object) -> float | None:
    try:
        return dt.datetime.fromisoformat(str(value)).timestamp()
    except (TypeError, ValueError):
        return None


def _recent_ledger_success_count(entries: list[dict], since_epoch: float) -> int:
    videos: set[str] = set()
    for entry in entries:
        if entry.get("rc") != 0:
            continue
        timestamp = _parse_ledger_time(entry.get("at"))
        if timestamp is None or timestamp < since_epoch:
            continue
        video_sha = entry.get("video_sha256")
        if isinstance(video_sha, str) and video_sha:
            videos.add(video_sha)
    return len(videos)


def _recent_creator_archive_count(http, since_epoch: float) -> int:
    archives: dict[str, dict] = {}
    page = 1
    while True:
        payload = http(CREATOR_ARCHIVES_API.format(page=page))
        if payload.get("code") != 0:
            raise RuntimeError(
                f"Creator archives query failed: {payload.get('code')} {payload.get('message')}"
            )
        data = payload.get("data") or {}
        audits = data.get("arc_audits") or []
        for row in audits:
            if not isinstance(row, dict):
                continue
            archive = row.get("Archive") or row.get("archive") or {}
            if not isinstance(archive, dict):
                continue
            ptime = archive.get("ptime")
            if isinstance(ptime, (int, float)) and ptime >= since_epoch:
                key = str(archive.get("bvid") or archive.get("aid") or id(archive))
                archives[key] = archive
        page_info = data.get("page") or {}
        total = page_info.get("count") or page_info.get("total") or 0
        if not audits or page * 30 >= int(total or 0):
            break
        page += 1
    return len(archives)


def rolling_quota_guard(
    ledger: Path, *, http, now_epoch: float | None = None
) -> tuple[dict, list[str]]:
    entries, problems = read_ledger(ledger)
    if problems:
        return {}, problems
    current = time.time() if now_epoch is None else now_epoch
    since = current - ROLLING_UPLOAD_WINDOW_SECONDS
    local_count = _recent_ledger_success_count(entries, since)
    try:
        creator_count = _recent_creator_archive_count(http, since)
    except Exception as exc:
        return {}, [f"rolling quota Creator estimate unavailable: {exc}"]
    estimate = max(local_count, creator_count)
    evidence = {
        "schema_version": "authorized-upload-rolling-quota.v1",
        "window_seconds": ROLLING_UPLOAD_WINDOW_SECONDS,
        "limit": ROLLING_UPLOAD_LIMIT,
        "since_epoch": since,
        "local_ledger_successes": local_count,
        "creator_recent_archives": creator_count,
        "estimated_used": estimate,
    }
    if estimate >= ROLLING_UPLOAD_LIMIT:
        return evidence, [
            f"rolling 24h upload estimate is {estimate}/{ROLLING_UPLOAD_LIMIT}; "
            "stop before upload (Bilibili code 21566 remains authoritative)"
        ]
    return evidence, []


def _run_season_step(
    manifest: dict, manifest_path: Path, bvid: str | None, args: argparse.Namespace
) -> int:
    """Shared by upload (post-success) and season-add.  0 = publicly in-season."""
    block, provenance = effective_season_block(manifest)
    if block is None:
        print(
            "season: manifest explicitly opts out (season=null) — archive stays outside collections"
        )
        return 0
    if not bvid:
        print(
            "SEASON PENDING: no bvid available (uploader output had no BVID= line); "
            "run: authorized_upload.py season-add --manifest <manifest> --bvid <BV...>",
            file=sys.stderr,
        )
        return 6
    http, csrf = _build_season_http(Path(args.cookie_json))
    result = season_add_flow(
        manifest,
        bvid,
        http=http,
        csrf=csrf,
        wait_seconds=args.season_wait,
        poll_seconds=args.season_poll,
    )
    sidecar = season_verify_sidecar_path(manifest_path)
    sidecar.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        f"SEASON {result['status']}: bvid={bvid} season={block['season_title']} ({provenance}) "
        f"evidence={sidecar}"
    )
    if result["status"] == "IN_SEASON_PUBLIC":
        return 0
    print(
        f"SEASON INCOMPLETE ({result['status']}): the publish is NOT finished — "
        f"re-run: authorized_upload.py season-add --manifest {manifest_path}",
        file=sys.stderr,
    )
    return 6


def _write_json_sidecar(path: Path, payload: dict) -> None:
    upload_recovery.write_json_sidecar(path, payload)


def _create_json_sidecar(path: Path, payload: dict) -> None:
    """Create one durable evidence file without overwriting prior truth."""

    path = path.resolve()
    if path.exists() or path.is_symlink():
        raise RepairError(f"completed sidecar already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}-{time.time_ns()}")
    body = (
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        try:
            view = memoryview(body)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("short write while creating completed sidecar")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        try:
            os.link(temporary, path)
        except FileExistsError as exc:
            raise RepairError(f"completed sidecar already exists: {path}") from exc
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    parent_descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(parent_descriptor)
    finally:
        os.close(parent_descriptor)


def _run_postpublish_verification(
    manifest: dict,
    manifest_path: Path,
    bvid: str | None,
    args: argparse.Namespace,
    *,
    quota_evidence: dict | None = None,
) -> tuple[int, dict | None]:
    if not bvid:
        print(
            "POSTED BUT UNVERIFIED: uploader returned no BVID; reconcile through Creator "
            "Center without re-uploading",
            file=sys.stderr,
        )
        return 6, None
    http, csrf = _build_season_http(Path(args.cookie_json))
    block, provenance = effective_season_block(manifest)
    if block is None:
        season_result = {
            "schema_version": "authorized-upload-season-verify.v1",
            "status": "SEASON_OPTED_OUT",
            "bvid": bvid,
            "season_binding": None,
            "season_binding_source": provenance,
            "verified_at": now(),
        }
    else:
        season_result = season_add_flow(
            manifest,
            bvid,
            http=http,
            csrf=csrf,
            wait_seconds=args.season_wait,
            poll_seconds=args.season_poll,
            display_wait_seconds=args.public_wait,
        )
    _write_json_sidecar(season_verify_sidecar_path(manifest_path), season_result)
    if block is not None and season_result.get("status") != "IN_SEASON_PUBLIC":
        blocked = {
            "schema_version": "authorized-upload-public-verify.v2",
            "status": "BLOCKED_BY_SEASON",
            "bvid": bvid,
            "season_status": season_result.get("status"),
            "verified_at": now(),
        }
        _write_json_sidecar(public_verify_sidecar_path(manifest_path), blocked)
        return 6, blocked

    public_result = public_verify_flow(
        manifest,
        bvid,
        http=http,
        season_result=season_result,
        wait_seconds=args.public_wait,
        poll_seconds=args.season_poll,
    )
    if quota_evidence is not None:
        public_result["preupload_quota_evidence"] = quota_evidence
    public_path = public_verify_sidecar_path(manifest_path)
    _write_json_sidecar(public_path, public_result)
    if public_result.get("status") != "VERIFIED_PUBLIC":
        print(
            f"PUBLIC VERIFY INCOMPLETE: {public_result.get('problems')}; evidence={public_path}",
            file=sys.stderr,
        )
        return 6, public_result

    attestation = manifest.get("package_attestation") or {}
    uploaded = {
        "schema_version": "authorized-upload-result.v3",
        "status": "VERIFIED_PUBLIC",
        "bvid": bvid,
        "aid": (public_result.get("public_view") or {}).get("aid"),
        "cid": (public_result.get("public_view") or {}).get("cid"),
        "title": manifest.get("title"),
        "uploaded_at": now(),
        "video_sha256": (manifest.get("video") or {}).get("sha256"),
        "cover_sha256": (manifest.get("cover") or {}).get("sha256"),
        "record_sha256": (attestation.get("record") or {}).get("sha256"),
        "subtitle_sha256": (attestation.get("subtitle") or {}).get("sha256"),
        "package_audit_sha256": (attestation.get("package_audit") or {}).get("sha256"),
        "review_manifest_sha256": (attestation.get("review_manifest") or {}).get("sha256"),
        "title_cover_qc_sha256": (attestation.get("title_cover_qc") or {}).get("sha256"),
        "manifest": str(manifest_path.resolve()),
        "manifest_sha256": sha256_file(manifest_path.resolve()),
        "public_verify": str(public_path.resolve()),
        "public_verify_sha256": sha256_file(public_path),
        "authorized_by": (manifest.get("authorization") or {}).get("by"),
        "authorization_quote": (manifest.get("authorization") or {}).get("quote"),
    }
    uploaded_path = uploaded_sidecar_path(manifest_path)
    _write_json_sidecar(uploaded_path, uploaded)
    try:
        reconciliation = _reconcile_new_bv_publication(
            manifest=manifest,
            manifest_path=manifest_path.resolve(),
            bvid=bvid,
            public_verify_path=public_path.resolve(),
            season_verify_path=season_verify_sidecar_path(manifest_path).resolve(),
            uploaded_path=uploaded_path.resolve(),
            reconciled_at=str(uploaded["uploaded_at"]),
        )
    except publication_reconciliation.PublicationReconciliationError as exc:
        print(
            "PUBLIC VERIFIED BUT LOCAL RECONCILIATION PENDING: "
            f"{exc}; re-run season-add for the same BVID (never re-upload)",
            file=sys.stderr,
        )
        return 6, public_result
    print(
        "PUBLICATION RECONCILED: "
        f"candidate={reconciliation['candidate_id']} "
        f"bvid={reconciliation['bvid']}"
    )
    return 0, public_result


def make_manifest(args: argparse.Namespace) -> int:
    video, cover = Path(args.video), Path(args.cover)
    package_audit = Path(args.package_audit)
    for path in (video, cover, package_audit):
        if not path.is_file():
            print(f"REFUSE: missing artifact {path}", file=sys.stderr)
            return 2
    if not args.title.strip() or not args.quote.strip():
        print(
            "REFUSE: --title and --quote (Ivan's authorization words) are required non-empty",
            file=sys.stderr,
        )
        return 2
    audit_problems: list[str] = []
    audit = _load_json_object(package_audit, "package audit", audit_problems)
    package_root = Path(str(audit.get("root") or "")).resolve()
    candidate_root = video.resolve().parent
    candidate_review = candidate_root / "review_manifest.json"
    candidate_payload = (
        _load_json_object(candidate_review, "review manifest", [])
        if candidate_review.is_file()
        else {}
    )
    is_c1_formal_package = (
        candidate_payload.get("schema_version")
        == "fastlane-c1-formal-private-review-manifest.v1"
    )
    if audit.get("passed") is not True:
        audit_problems.append("package audit did not pass")
    if not _zero_blocking_issues(audit):
        audit_problems.append("package audit reports blocking issues")
    # C1's accepted private audit is portable across its isolated worktree;
    # recognize only the formal schema beside the supplied final video, then
    # let the C1 validator prove that the sole difference is audit root path.
    if is_c1_formal_package:
        package_root = candidate_root
    else:
        if not package_root.is_dir():
            audit_problems.append(f"package audit root missing: {package_root}")
        if video.resolve().parent != package_root:
            audit_problems.append("video must be directly inside package audit root")
    # C1 has an intentionally non-generic formal record/review closure.  Route
    # it before the ordinary same-stem record convention is inspected.
    if (package_root / "review_manifest.json").is_file():
        try:
            review_candidate = _load_json_object(
                package_root / "review_manifest.json", "review manifest", audit_problems
            )
        except Exception:  # _load_json_object records the portable refusal
            review_candidate = {}
        if review_candidate.get("schema_version") == "fastlane-c1-formal-private-review-manifest.v1":
            if audit_problems:
                for problem in audit_problems:
                    print(f"REFUSE: {problem}", file=sys.stderr)
                return 2
            return _make_c1_authorized_projection(args, package_root, package_audit, video, cover)
    record_sidecar = sidecar_record_path(video)
    subtitle_sidecar = sidecar_subtitle_path(video)
    review_manifest = package_root / "review_manifest.json"
    for path, label in (
        (record_sidecar, "same-stem record.json"),
        (subtitle_sidecar, "same-stem SRT"),
        (review_manifest, "review_manifest.json"),
    ):
        if not path.is_file():
            audit_problems.append(f"{label} missing: {path}")
    if audit_problems:
        for problem in audit_problems:
            print(f"REFUSE: {problem}", file=sys.stderr)
        return 2

    record = _load_json_object(record_sidecar, "record", audit_problems)
    if audit_problems:
        for problem in audit_problems:
            print(f"REFUSE: {problem}", file=sys.stderr)
        return 2
    tags = [t.strip() for t in (args.tags or "").split(",") if t.strip()]
    tags_source = "cli" if tags else None
    upload_tags = record.get("upload_tags") or {}
    record_tags = [str(t).strip() for t in (upload_tags.get("final_tags") or []) if str(t).strip()]
    if not tags:
        tags = record_tags
        tags_source = f"record.json:{upload_tags.get('engine') or '?'}"
    if args.no_tags:
        print("REFUSE: manifest v3 does not allow --no-tags", file=sys.stderr)
        return 2
    if not tags:
        print("REFUSE: manifest v3 requires non-empty record-bound tags", file=sys.stderr)
        return 2
    if tags != record_tags:
        print(
            "REFUSE: --tags must exactly match record.upload_tags.final_tags; "
            "update and re-audit the package instead of overriding reviewed metadata",
            file=sys.stderr,
        )
        return 2
    tag_problems = validate_tags(tags)
    if tag_problems:
        for problem in tag_problems:
            print(f"REFUSE: {problem}", file=sys.stderr)
        return 2
    print(f"tags: {len(tags)} from {tags_source}", file=sys.stderr)
    try:
        season = season_block_for(args.title, args.season)
    except ValueError as exc:
        print(f"REFUSE: {exc}", file=sys.stderr)
        return 2
    if season is None:
        print("season: EXPLICITLY none — this archive will not join a collection", file=sys.stderr)
    else:
        print(f"season: {season['season_title']} ({season['source']})", file=sys.stderr)
    video_sha = sha256_file(video)
    manifest = {
        "manifest_version": 3,
        "schema_version": "authorized-upload-manifest.v3",
        "artifact_id": video_sha[:12],
        "video": {"path": str(video.resolve()), "sha256": video_sha, "bytes": video.stat().st_size},
        "cover": {
            "path": str(cover.resolve()),
            "sha256": sha256_file(cover),
            "bytes": cover.stat().st_size,
        },
        "title": args.title,
        "description": DEFAULT_DESCRIPTION,
        "publish_policy": {
            "tid": EXPECTED_TID,
            "copyright": EXPECTED_COPYRIGHT,
            "source": EXPECTED_SOURCE,
        },
        "season": season,
        "package_attestation": {
            "schema_version": "authorized-upload-package-attestation.v1",
            "package_root": str(package_root),
            "package_audit": _sha_entry(package_audit),
            "review_manifest": _sha_entry(review_manifest),
            "record": _sha_entry(record_sidecar),
            "subtitle": _sha_entry(subtitle_sidecar),
        },
        "authorization": {"by": args.authorized_by, "quote": args.quote, "at": now()},
        "created_at": now(),
        "tags": tags,
        "tags_source": tags_source,
    }
    package_problems = published_recovery.attach_manifest_attestation(manifest, package_root) + repair_binding.attach_package_recovery_publication_authority(manifest, record, review_manifest, video)
    review_payload = _load_json_object(review_manifest, "review manifest", package_problems)
    if review_payload:
        package_problems.extend(
            _attach_cover_only_audit_scope(
                manifest,
                review_payload,
                video,
            )
        )
    package_problems.extend(
        human_review.attach_final_human_review(
            manifest, args.final_human_review, season_ids=EXPECTED_SEASON_IDS
        )
    )
    package_problems.extend(_attach_title_cover_qc(manifest, args.title_cover_qc))
    package_problems.extend(_validate_v3_package_attestation(manifest, verify_hashes=True))
    # ``make-manifest`` is shared by new uploads and existing-BV repairs.  A
    # committed publication authority must block the ordinary ``upload`` lane,
    # but it is exactly the authority that ``repair-plan`` needs to bind a
    # reviewed replacement to its existing BV.  Applying the registry upload
    # gate here deadlocks the only authorized repair lane before it can create
    # a manifest.  ``upload()`` retains the side-effect boundary check below.
    if package_problems:
        for problem in package_problems:
            print(f"REFUSE: {problem}", file=sys.stderr)
        return 2
    out = Path(args.out) if args.out else video.with_suffix(".upload_manifest.json")
    out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {"manifest": str(out), "artifact_id": manifest["artifact_id"]}, ensure_ascii=False
        )
    )
    return 0


def _make_c1_authorized_projection(
    args: argparse.Namespace,
    package_root: Path,
    package_audit: Path,
    video: Path,
    cover: Path,
) -> int:
    """Build only C1's sealed same-BV after-image; never synthesize a record."""
    try:
        public = c1_projection.public_metadata_projection()
        authority = c1_projection.load_formal_authority()
        names = authority["output_names"]
        if (
            args.title != c1_projection.TITLE
            or video.resolve() != (package_root / names["burned_final"]).resolve()
            or cover.resolve() != (package_root / names["cover"]).resolve()
            or package_audit.resolve() != (package_root / "package_audit.json").resolve()
            or args.tags not in (None, "")
            or args.no_tags
            or args.season not in (None, "auto", "talk")
        ):
            raise c1_projection.C1TechnicalReceiptError("C1_AUTHORIZED_PROJECTION_INPUT_INVALID")
        season = season_block_for(args.title, "talk")
        assert season is not None
        manifest = {
            "manifest_version": 3,
            "schema_version": "authorized-upload-manifest.v3",
            "artifact_id": sha256_file(video)[:12],
            "video": _sha_entry(video),
            "cover": _sha_entry(cover),
            "title": c1_projection.TITLE,
            "description": DEFAULT_DESCRIPTION,
            "publish_policy": {"tid": EXPECTED_TID, "copyright": EXPECTED_COPYRIGHT, "source": EXPECTED_SOURCE},
            "season": season,
            "package_attestation": {
                "package_root": str(package_root.resolve()),
                "review_manifest": _c1_attested_entry(package_root / "review_manifest.json"),
                "package_audit": _c1_attested_entry(package_audit),
            },
            "authorization": {"by": args.authorized_by, "quote": args.quote, "at": now()},
            "created_at": now(),
            "tags": public["tags"],
            "tags_source": "c1-public-metadata-seal.v1",
            "recovery_publication_authority": c1_projection.projection_authority(),
        }
        problems = human_review.attach_final_human_review(
            manifest, args.final_human_review, season_ids=EXPECTED_SEASON_IDS
        )
        if problems:
            raise c1_projection.C1TechnicalReceiptError("; ".join(problems))
        c1_projection.validate_authorized_projection_manifest(manifest)
    except (c1_projection.C1TechnicalReceiptError, ValueError) as exc:
        print(f"REFUSE: {exc}", file=sys.stderr)
        return 2
    out = Path(args.out) if args.out else package_root / "c1.authorized-upload-manifest.v3.json"
    if out.parent.resolve() != package_root.resolve() or out.name != "c1.authorized-upload-manifest.v3.json":
        print("REFUSE: C1 authorized projection output path is fixed inside its formal package", file=sys.stderr)
        return 2
    if out.exists():
        print(f"REFUSE: output already exists: {out}", file=sys.stderr)
        return 2
    out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"manifest": str(out), "artifact_id": manifest["artifact_id"]}, ensure_ascii=False))
    return 0


def _c1_attested_entry(path: Path) -> dict[str, object]:
    """C1 receipt attestation uses the uploader's unprefixed digest form."""
    return {"path": str(path.resolve()), "sha256": sha256_file(path), "bytes": path.stat().st_size}


def load_and_verify(
    manifest_path: Path,
    *,
    ordinary_upload: bool = False,
    frozen_plan_resume: bool = False,
) -> tuple[dict | None, list[str]]:
    """(manifest, problems) — problems non-empty means REFUSE."""
    problems: list[str] = []
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, [f"manifest unreadable: {exc}"]
    auth = manifest.get("authorization") or {}
    if not str(auth.get("quote") or "").strip() or not str(auth.get("by") or "").strip():
        problems.append("manifest carries no authorization (by+quote required)")
    title = str(manifest.get("title") or "")
    if not title.strip():
        problems.append("manifest has no title")
    else:
        title_lane = derive_season_lane(title)
        title_policy_codes = upload_manifest_title_policy_violations(manifest, title, title_lane)
        for code in title_policy_codes:
            problems.append(f"manifest publish title violates {code}")
    if "season" in manifest:
        problems.extend(validate_season_block(manifest["season"]))
        if isinstance(manifest.get("season"), dict) and manifest["season"].get(
            "lane"
        ) != derive_season_lane(title):
            problems.append("manifest season lane contradicts the frozen title")
    if "tags" in manifest:
        problems.extend(validate_tags(manifest["tags"]))
    if manifest.get("manifest_version") == 3:
        if manifest.get("schema_version") != "authorized-upload-manifest.v3":
            problems.append("manifest v3 schema_version is invalid")
        if not manifest.get("tags"):
            problems.append("manifest v3 requires non-empty tags")
        if manifest.get("description") != DEFAULT_DESCRIPTION:
            problems.append("manifest v3 description drifted from the uploader contract")
        if manifest.get("publish_policy") != {
            "tid": EXPECTED_TID,
            "copyright": EXPECTED_COPYRIGHT,
            "source": EXPECTED_SOURCE,
        }:
            problems.append("manifest v3 publish_policy is invalid")
        problems.extend(
            _validate_v3_package_attestation(
                manifest, verify_hashes=True, live_policy_recheck=not frozen_plan_resume
            )
        )
    for kind in ("video", "cover"):
        entry = manifest.get(kind) or {}
        path = Path(entry.get("path") or "")
        if not path.is_file():
            problems.append(f"{kind} missing: {path}")
            continue
        actual = sha256_file(path)
        if actual != entry.get("sha256"):
            problems.append(
                f"{kind} HASH DRIFT since review: manifest={str(entry.get('sha256'))[:12]} actual={actual[:12]} ({path})"
            )
    if ordinary_upload:
        problems.extend(human_review.ordinary_upload_problems(manifest))
    return manifest, problems


def read_ledger(ledger: Path) -> tuple[list[dict], list[str]]:
    """Read the append-only ledger strictly enough for side-effect safety."""

    if not ledger.exists():
        return [], []
    try:
        raw = ledger.read_bytes()
    except OSError as exc:
        return [], [f"upload ledger unreadable: {exc}"]
    if raw and not raw.endswith(b"\n"):
        return [], ["upload ledger has a partial final row"]
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        return [], [f"upload ledger is not UTF-8: {exc}"]
    entries: list[dict] = []
    problems: list[str] = []
    for line_no, line in enumerate(lines, start=1):
        if not line:
            problems.append(f"upload ledger row {line_no} is empty")
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            problems.append(f"upload ledger row {line_no} is invalid JSON")
            continue
        if not isinstance(entry, dict):
            problems.append(f"upload ledger row {line_no} is not an object")
            continue
        entries.append(entry)
    return entries, problems


def ledger_guard(ledger: Path, video_sha256: str) -> tuple[str | None, dict | None, list[str]]:
    """Return ``uploaded``/``unresolved`` or a malformed-ledger problem.

    Legacy one-row entries remain readable.  New two-phase entries make an
    uploader crash distinguishable from a known failed attempt.  Any STARTED
    intent without its matching FINISHED row is globally ambiguous and blocks
    all further uploads until a human reconciles the external Bilibili state.
    """

    entries, problems = read_ledger(ledger)
    if problems:
        return None, None, problems
    started: dict[str, dict] = {}
    finished: set[str] = set()
    posted_unverified: dict | None = None
    successful: dict | None = None
    for row_no, entry in enumerate(entries, start=1):
        event = entry.get("event")
        if event is None:
            # Legacy terminal-only row.
            if entry.get("video_sha256") == video_sha256 and entry.get("rc") == 0:
                successful = entry
            continue
        attempt_id = entry.get("attempt_id")
        if not isinstance(attempt_id, str) or not attempt_id:
            problems.append(f"upload ledger row {row_no} has no attempt_id")
            continue
        if event == "UPLOAD_ATTEMPT_STARTED":
            if attempt_id in started or attempt_id in finished:
                problems.append(
                    f"upload ledger attempt {attempt_id} has a duplicate/out-of-order STARTED row"
                )
                continue
            required_started = (
                "artifact_id",
                "video_sha256",
                "cover_sha256",
                "manifest",
                "manifest_sha256",
                "uploader",
            )
            if any(
                not isinstance(entry.get(key), str) or not entry.get(key)
                for key in required_started
            ):
                problems.append(f"upload ledger attempt {attempt_id} has an incomplete STARTED row")
                continue
            started[attempt_id] = entry
        elif event == "UPLOAD_ATTEMPT_FINISHED":
            if attempt_id not in started or attempt_id in finished:
                problems.append(
                    f"upload ledger attempt {attempt_id} has an unmatched/duplicate FINISHED row"
                )
                continue
            finished.add(attempt_id)
            stable_keys = (
                "artifact_id",
                "video_sha256",
                "cover_sha256",
                "manifest",
                "manifest_sha256",
                "uploader",
                "package_audit_sha256",
                "record_sha256",
                "subtitle_sha256",
                "review_manifest_sha256",
                "title_cover_qc_sha256",
            )
            changed = [
                key
                for key in stable_keys
                if key in started[attempt_id] and entry.get(key) != started[attempt_id].get(key)
            ]
            if changed:
                problems.append(
                    f"upload ledger attempt {attempt_id} changed bound fields: {changed}"
                )
            if isinstance(entry.get("rc"), bool) or not isinstance(entry.get("rc"), int):
                problems.append(f"upload ledger attempt {attempt_id} has no integer terminal rc")
            if entry.get("video_sha256") == video_sha256 and entry.get("rc") == 0:
                successful = entry
            elif (
                entry.get("video_sha256") == video_sha256
                and entry.get("uploader_rc") == 0
                and entry.get("bvid")
            ):
                posted_unverified = entry
        elif event == "UPLOAD_PUBLICATION_VERIFIED":
            if attempt_id not in started or attempt_id not in finished:
                problems.append(
                    f"upload ledger attempt {attempt_id} has a publication verification "
                    "without a completed upload attempt"
                )
                continue
            origin = started[attempt_id]
            changed = [
                key
                for key in (
                    "artifact_id",
                    "video_sha256",
                    "cover_sha256",
                    "manifest",
                    "manifest_sha256",
                    "uploader",
                    "package_audit_sha256",
                    "record_sha256",
                    "subtitle_sha256",
                    "review_manifest_sha256",
                    "title_cover_qc_sha256",
                )
                if key in origin and entry.get(key) != origin.get(key)
            ]
            if changed:
                problems.append(
                    f"upload ledger attempt {attempt_id} changed verified bound fields: {changed}"
                )
            if entry.get("rc") != 0:
                problems.append(
                    f"upload ledger attempt {attempt_id} publication verification is not rc=0"
                )
            if entry.get("video_sha256") == video_sha256:
                successful = entry
        else:
            problems.append(f"upload ledger row {row_no} has unknown event {event!r}")
    if problems:
        return None, None, problems
    unresolved = [entry for attempt_id, entry in started.items() if attempt_id not in finished]
    if unresolved:
        return "unresolved", unresolved[0], []
    if successful is not None:
        return "uploaded", successful, []
    if posted_unverified is not None:
        return "posted_unverified", posted_unverified, []
    return None, None, []


def _append_publication_verified(
    ledger: Path,
    guard_row: dict,
    *,
    bvid: str,
    result: dict | None,
    manifest_path: Path,
) -> None:
    upload_recovery.append_publication_verified(
        ledger,
        guard_row,
        bvid=bvid,
        result=result,
        manifest_path=manifest_path,
        append_ledger=append_ledger,
        now=now,
        public_verify_sidecar_path=public_verify_sidecar_path,
    )


def append_ledger(ledger: Path, entry: dict) -> None:
    """Durably append one JSONL row before releasing the shared upload lock."""

    ledger.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    fd = os.open(ledger, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        view = memoryview(payload)
        while view:
            try:
                written = os.write(fd, view)
            except InterruptedError:
                continue
            if written <= 0:
                raise OSError("short write while appending upload ledger")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    parent_fd = os.open(ledger.parent, os.O_RDONLY)
    try:
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)


@contextmanager
def exclusive_upload_lock(path: Path) -> Iterator[None]:
    """Serialize verified upload+ledger writes with false-green repair.

    The repair transaction takes this same advisory lock while it proves that
    the rejected song was not uploaded and commits its tombstones.  Holding the
    lock from manifest verification through ledger append prevents either side
    from observing a half-finished upload decision.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    acquired = False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError as exc:
            raise UploadLockBusy(
                f"shared upload/repair lock is busy: {path}; retry after the active transaction finishes"
            ) from exc
        yield
    finally:
        if acquired:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def upload(args: argparse.Namespace) -> int:
    manifest_path = Path(args.manifest)
    ledger = Path(args.ledger)
    # The default is deliberately independent of --ledger.  A caller must not
    # bypass repair/upload serialization by pointing the ledger somewhere else.
    # Tests or genuinely isolated channels may opt into another lock explicitly.
    lock_path = Path(args.lock) if args.lock else DEFAULT_UPLOAD_LOCK

    with exclusive_upload_lock(lock_path):
        manifest, problems = load_and_verify(manifest_path, ordinary_upload=True)
        if problems:
            for p in problems:
                print(f"REFUSE: {p}", file=sys.stderr)
            return 2
        if manifest.get("manifest_version") != 3:
            print(
                "REFUSE: upload requires authorized-upload-manifest.v3; legacy manifests "
                "remain verify/season-add readable only",
                file=sys.stderr,
            )
            return 2
        registry_block = _publication_block(manifest)
        if registry_block:
            print(f"REFUSE: {registry_block}", file=sys.stderr)
            return 2
        video_sha = manifest["video"]["sha256"]
        guard_status, guard_row, ledger_problems = ledger_guard(ledger, video_sha)
        if ledger_problems:
            for problem in ledger_problems:
                print(f"REFUSE: {problem}", file=sys.stderr)
            return 5
        if guard_status == "unresolved":
            print(
                "REFUSE: upload ledger has an unresolved UPLOAD_ATTEMPT_STARTED "
                f"(attempt_id={guard_row.get('attempt_id')}); reconcile the external upload state before retrying",
                file=sys.stderr,
            )
            return 5
        if guard_status == "posted_unverified":
            print(
                "REFUSE: this exact video already created a Bilibili archive but public "
                f"verification is incomplete (bvid={guard_row.get('bvid')}); run season-add, "
                "never re-upload it",
                file=sys.stderr,
            )
            return 6
        if guard_status == "uploaded":
            print(
                f"REFUSE: this exact video was already uploaded at {guard_row.get('at')}"
                f" (bvid={guard_row.get('bvid') or '?'}) — duplicate posts are the 充电器 incident; not repeating it",
                file=sys.stderr,
            )
            return 3
        quota_http, _ = _build_season_http(Path(args.cookie_json))
        quota_evidence, quota_problems = rolling_quota_guard(ledger, http=quota_http)
        if quota_problems:
            for problem in quota_problems:
                print(f"REFUSE: {problem}", file=sys.stderr)
            return 8
        cmd = [
            args.uploader,
            manifest["video"]["path"],
            manifest["cover"]["path"],
            manifest["title"],
        ]
        manifest_tags = manifest.get("tags") or []
        # v3 always has a reviewed, record-bound tag line.
        cmd.append(",".join(manifest_tags))
        env = os.environ.copy()
        env["AUTHORIZED_UPLOAD"] = "1"
        attempt_id = uuid.uuid4().hex
        manifest_resolved = manifest_path.resolve()
        manifest_sha = sha256_file(manifest_resolved)
        common_ledger_fields = {
            "attempt_id": attempt_id,
            "artifact_id": manifest["artifact_id"],
            "video_sha256": video_sha,
            "cover_sha256": manifest["cover"]["sha256"],
            "title": manifest["title"],
            "authorized_by": manifest["authorization"]["by"],
            "authorization_quote": manifest["authorization"]["quote"],
            "manifest": str(manifest_resolved),
            "manifest_sha256": manifest_sha,
            "uploader": args.uploader,
            "manifest_version": manifest.get("manifest_version"),
            "package_audit_sha256": manifest["package_attestation"]["package_audit"]["sha256"],
            "record_sha256": manifest["package_attestation"]["record"]["sha256"],
            "subtitle_sha256": manifest["package_attestation"]["subtitle"]["sha256"],
            "review_manifest_sha256": manifest["package_attestation"]["review_manifest"]["sha256"],
            "title_cover_qc_sha256": manifest["package_attestation"]["title_cover_qc"]["sha256"],
            "quota_evidence": quota_evidence,
        }
        common_ledger_fields["tags"] = ",".join(manifest_tags)
        append_ledger(
            ledger,
            {
                "event": "UPLOAD_ATTEMPT_STARTED",
                "at": now(),
                **common_ledger_fields,
            },
        )
        print(f"uploading artifact {manifest['artifact_id']} via {args.uploader}", flush=True)
        completed = subprocess.run(
            cmd, check=False, capture_output=True, text=True, timeout=3600, env=env
        )
        output = (completed.stdout or "") + (completed.stderr or "")
        sys.stdout.write(output)
        bvid = None
        for token in output.split():
            if token.startswith("BVID="):
                bvid = token.removeprefix("BVID=").strip("\"' ,")
        if completed.returncode != 0:
            quota_code = QUOTA_FREQUENCY_CODE if str(QUOTA_FREQUENCY_CODE) in output else None
            append_ledger(
                ledger,
                {
                    "event": "UPLOAD_ATTEMPT_FINISHED",
                    "at": now(),
                    **common_ledger_fields,
                    "uploader_rc": completed.returncode,
                    "rc": completed.returncode,
                    "bvid": bvid,
                    "quota_frequency_code": quota_code,
                },
            )
            if quota_code == QUOTA_FREQUENCY_CODE:
                print(
                    "BILIBILI QUOTA AUTHORITATIVE: code 21566; stop further uploads "
                    "until the rolling window frees",
                    file=sys.stderr,
                )
            print(
                f"ledger += artifact {manifest['artifact_id']} rc={completed.returncode} "
                f"bvid={bvid or '?'}"
            )
            return completed.returncode
        if not bvid:
            print(
                "LEDGER LEFT UNRESOLVED: uploader succeeded without a BVID; all further "
                "uploads stay blocked until Creator Center reconciliation without re-uploading",
                file=sys.stderr,
            )
            return 6
        # The remote archive now exists.  Persist that fact before any sidecar,
        # season or public-readback operation can fail (for example ENOSPC).
        finished_row = {
            "event": "UPLOAD_ATTEMPT_FINISHED",
            "at": now(),
            **common_ledger_fields,
            "uploader_rc": 0,
            "rc": 6,
            "bvid": bvid,
            "public_verify_status": "POSTED_UNVERIFIED",
        }
        append_ledger(ledger, finished_row)

    if args.skip_season:
        post_rc, public_result = (
            6,
            {
                "schema_version": "authorized-upload-public-verify.v2",
                "status": "SKIPPED_BY_EMERGENCY_FLAG",
                "bvid": bvid,
                "verified_at": now(),
            },
        )
        _write_json_sidecar(
            public_verify_sidecar_path(manifest_path),
            public_result,
        )
    else:
        post_rc, public_result = _run_postpublish_verification(
            manifest,
            manifest_path,
            bvid,
            args,
            quota_evidence=quota_evidence,
        )
    if post_rc == 0:
        with exclusive_upload_lock(lock_path):
            _append_publication_verified(
                ledger,
                finished_row,
                bvid=bvid,
                result=public_result,
                manifest_path=manifest_path,
            )
    print(f"ledger += artifact {manifest['artifact_id']} rc={post_rc} bvid={bvid}")
    return post_rc


def verify(args: argparse.Namespace) -> int:
    manifest, problems = load_and_verify(Path(args.manifest))
    if problems:
        for p in problems:
            print(f"FAIL: {p}", file=sys.stderr)
        return 2
    print(
        f"OK: artifact {manifest['artifact_id']} matches its manifest (video+cover hashes, title, authorization present)"
    )
    return 0


def season_add(args: argparse.Namespace) -> int:
    return upload_recovery.season_add(
        args,
        default_upload_lock=DEFAULT_UPLOAD_LOCK,
        exclusive_upload_lock=exclusive_upload_lock,
        load_and_verify=load_and_verify,
        ledger_guard=ledger_guard,
        read_ledger=read_ledger,
        append_ledger=append_ledger,
        build_season_http=_build_season_http,
        run_postpublish_verification=_run_postpublish_verification,
        run_season_step=_run_season_step,
        public_verify_sidecar_path=public_verify_sidecar_path,
        normalise_tags=_normalise_tags,
        now=now,
        view_api=VIEW_API,
        tags_api=TAGS_API,
        member_archive_view_api=MEMBER_ARCHIVE_VIEW_API,
        expected_tid=EXPECTED_TID,
        expected_copyright=EXPECTED_COPYRIGHT,
        expected_source=EXPECTED_SOURCE,
    )


def _biliup_readonly_canary(cookie_json: Path, bvid: str) -> None:
    """Prove the append CLI login can read the target before APPEND_INTENT."""

    same_bv_live_verification.run_biliup_readonly_canary(
        cookie_json,
        bvid,
        validate_cookie=member_api.validate_biliup_cookie_file,
        biliup_bin=member_api.BILIUP_BIN,
        run=subprocess.run,
        repair_error=RepairError,
    )


def _same_bv_adapter(
    cookie_json: Path,
    biliup_cookie_json: Path,
    bvid: str | None = None,
) -> BilibiliRepairAdapter:
    """Build repair reads and biliup append from their explicit cookie files."""

    if bvid is not None:
        _biliup_readonly_canary(biliup_cookie_json, bvid)
    http, _csrf = _build_season_http(cookie_json)
    return BilibiliRepairAdapter(
        session=member_api.BiliSession(
            cookie_path=cookie_json, biliup_cookie_path=biliup_cookie_json
        ),
        http=http,
        view_url=VIEW_API,
        tags_url=TAGS_API,
        section_url=SECTION_VIEW_API,
    )


def _same_bv_cover_adapter(cookie_json: Path) -> BilibiliRepairAdapter:
    """Build the cover-only adapter without any append/biliup capability."""

    http, _csrf = _build_season_http(cookie_json)
    return BilibiliRepairAdapter(
        session=member_api.BiliSession(cookie_path=cookie_json),
        http=http,
        view_url=VIEW_API,
        tags_url=TAGS_API,
        section_url=SECTION_VIEW_API,
    )


def repair_plan(args: argparse.Namespace) -> int:
    """Freeze one exact existing-BV repair without changing remote state."""

    manifest_path = Path(args.manifest).resolve()
    lock_path = Path(args.lock) if args.lock else DEFAULT_UPLOAD_LOCK
    with exclusive_upload_lock(lock_path):
        manifest, problems = load_and_verify(manifest_path)
        problems.extend(
            repair_binding.repair_publication_target_problems(manifest or {}, args.bvid)
        )
        if problems:
            for problem in problems:
                print(f"REFUSE: {problem}", file=sys.stderr)
            return 2
        assert manifest is not None
        adapter = _same_bv_adapter(
            Path(args.cookie_json),
            Path(args.biliup_cookie_json),
            args.bvid,
        )
        season = manifest.get("season") or {}
        section_id = season.get("section_id")
        if not isinstance(section_id, int):
            print(
                "REFUSE: repair manifest has no exact season section_id",
                file=sys.stderr,
            )
            return 2
        snapshot = adapter.observe(args.bvid, section_id)
        plan = create_same_bv_repair_plan(
            manifest_path=manifest_path,
            manifest=manifest,
            bvid=args.bvid,
            snapshot=snapshot,
            predecessor_completed_path=(Path(args.predecessor_completed).resolve() if args.predecessor_completed else None),
            preserve_existing_tags=bool(args.preserve_existing_tags),
        )
        journal = Path(args.journal).resolve()
        predecessor = plan.get("predecessor_completion") or {}
        predecessor_plan = predecessor.get("plan") or {}
        predecessor_row = predecessor.get("verified_journal_row") or {}
        assert_same_bv_unowned(
            journal,
            bvid=args.bvid,
            plan_id=str(plan["plan_id"]),
            predecessor_plan_id=(
                str(predecessor_plan.get("plan_id")) if predecessor_plan.get("plan_id") else None
            ),
            predecessor_verified_row_sha256=(
                str(predecessor_row.get("row_sha256"))
                if predecessor_row.get("row_sha256")
                else None
            ),
        )
        if args.dry_run:
            print(json.dumps(plan, ensure_ascii=False, indent=2))
            return 0
        plan_path = Path(args.out).resolve()
        write_same_bv_repair_plan(plan_path, plan)
        initialise_same_bv_repair_journal(journal, plan_path, plan)
    print(
        json.dumps(
            {
                "status": "PLANNED",
                "bvid": args.bvid,
                "plan": str(plan_path),
                "journal": str(journal),
                "remote_mutation": False,
            },
            ensure_ascii=False,
        )
    )
    return 0


def _load_repair_manifest(plan_path: Path) -> tuple[dict | None, dict | None, list[str]]:
    try:
        plan = load_same_bv_repair_plan(plan_path)
    except PlanInvalid as exc:
        return None, None, [str(exc)]
    manifest_path = Path(str((plan.get("manifest") or {}).get("path") or ""))
    manifest, problems = load_and_verify(manifest_path, frozen_plan_resume=True)
    if problems or manifest is None:
        return plan, manifest, problems
    problems.extend(
        repair_binding.validate_plan_problems(plan, manifest=manifest, plan_path=plan_path)
    )
    return plan, manifest, problems


def repair_run(args: argparse.Namespace) -> int:
    """Resume a journaled same-BV transaction; never creates another BV."""

    plan_path = Path(args.plan).resolve()
    journal = Path(args.journal).resolve()
    lock_path = Path(args.lock) if args.lock else DEFAULT_UPLOAD_LOCK
    lock = nullcontext() if args.dry_run else exclusive_upload_lock(lock_path)
    with lock:
        plan, manifest, problems = _load_repair_manifest(plan_path)
        if problems or manifest is None or plan is None:
            for problem in problems:
                print(f"REFUSE: {problem}", file=sys.stderr)
            return 2
        if args.dry_run:
            result = preview_same_bv_repair(plan_path=plan_path, journal=journal, manifest=manifest)
        else:
            result = run_same_bv_repair(
                plan_path=plan_path,
                journal=journal,
                manifest=manifest,
                adapter=_same_bv_adapter(
                    Path(args.cookie_json),
                    Path(args.biliup_cookie_json),
                    str(plan["bvid"]),
                ),
                wait_seconds=args.wait,
                poll_seconds=args.poll,
            )
    print(
        json.dumps(
            {
                "state": result.state,
                "changed": result.changed,
                "message": result.message,
                "details": result.details,
                "dry_run": bool(args.dry_run),
            },
            ensure_ascii=False,
        )
    )
    if result.state == "VERIFIED":
        return 0
    if result.state == "BLOCKED_DRIFT":
        return 5
    return 6


def repair_status(args: argparse.Namespace) -> int:
    """Read and validate the local repair authority without remote writes."""

    return same_bv_live_verification.run_repair_status(
        args,
        load_repair_manifest=_load_repair_manifest,
        status_reader=same_bv_repair_status,
    )


def _load_cover_repair_manifest(plan_path: Path) -> tuple[dict | None, dict | None, list[str]]:
    """Bind the CLI's existing manifest gates to the cover-only flow."""

    return cover_repair_cli.load_manifest(
        plan_path,
        load_and_verify=load_and_verify,
        title_cover_qc_problems=_title_cover_qc_attestation_problems,
    )


def cover_repair_plan(args: argparse.Namespace) -> int:
    """Keep the public parser handler while delegating cover-only orchestration."""

    return cover_repair_cli.plan(
        args,
        default_upload_lock=DEFAULT_UPLOAD_LOCK,
        exclusive_upload_lock=exclusive_upload_lock,
        load_and_verify=load_and_verify,
        title_cover_qc_problems=_title_cover_qc_attestation_problems,
        same_bv_cover_adapter=_same_bv_cover_adapter,
    )


def cover_repair_status(args: argparse.Namespace) -> int:
    """Keep the public parser handler while delegating cover-only status."""

    return cover_repair_cli.status(
        args,
        load_cover_repair_manifest=_load_cover_repair_manifest,
    )


def cover_repair_run(args: argparse.Namespace) -> int:
    """Keep the public parser handler while delegating cover-only execution."""

    return cover_repair_cli.run(
        args,
        default_upload_lock=DEFAULT_UPLOAD_LOCK,
        exclusive_upload_lock=exclusive_upload_lock,
        load_cover_repair_manifest=_load_cover_repair_manifest,
        same_bv_cover_adapter=_same_bv_cover_adapter,
    )


def cover_repair_verify_live(args: argparse.Namespace) -> int:
    """Keep the public parser handler while delegating live verification."""

    return cover_repair_cli.verify_live(
        args,
        default_upload_lock=DEFAULT_UPLOAD_LOCK,
        exclusive_upload_lock=exclusive_upload_lock,
        load_cover_repair_manifest=_load_cover_repair_manifest,
        same_bv_cover_adapter=_same_bv_cover_adapter,
        reconcile_same_bv_cover_publication=_reconcile_same_bv_cover_publication,
        reconciliation_error=publication_reconciliation.PublicationReconciliationError,
        sha256_file=sha256_file,
    )


def main(argv: list[str] | None = None) -> int:
    args = authorized_upload_cli_parser.parse_args(
        argv,
        description=__doc__ or "",
        handlers={
            "make_manifest": make_manifest,
            "upload": upload,
            "season_add": season_add,
            "verify": verify,
            "repair_plan": repair_plan,
            "repair_run": repair_run,
            "repair_reconcile_blocked": repair_reconcile_blocked,
            "repair_status": repair_status,
            "repair_verify_live": repair_verify_live,
            "cover_repair_plan": cover_repair_plan,
            "cover_repair_run": cover_repair_run,
            "cover_repair_status": cover_repair_status,
            "cover_repair_verify_live": cover_repair_verify_live,
        },
        defaults={
            "ledger": DEFAULT_LEDGER,
            "repair_ledger": DEFAULT_REPAIR_LEDGER,
            "cover_repair_ledger": DEFAULT_COVER_REPAIR_LEDGER,
            "uploader": DEFAULT_UPLOADER,
            "cookie_json": DEFAULT_COOKIE_JSON,
            "biliup_cookie_json": DEFAULT_REPAIR_BILIUP_COOKIE_JSON,
        },
    )
    try:
        return args.func(args)
    except (UploadLockBusy, RepairError, CookieSchemaError) as exc:
        print(f"REFUSE: {exc}", file=sys.stderr)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
