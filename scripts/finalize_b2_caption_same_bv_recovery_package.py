#!/usr/bin/env python3
"""Create B2's terminal, mechanically reviewed same-BV recovery package.

The finalizer is candidate-bound and create-only. It starts from the canonical
corrected-caption package, removes the stale duplicate record, projects only
runtime locators into the new package, binds the committed publication
identity, rebuilds the formal recovery manifest, and creates current canonical
and mechanical receipts. It never uploads or mutates media bytes.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
from typing import Any, Mapping

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.audit_review_package import audit_package
from scripts.build_recovery_review_manifest import build_manifest
from src.autoslice.b2_caption_formal_successor import validate_final_review_successor
from src.autoslice.final_subtitle_audio_gate import validate_final_subtitle_audio_check
from src.autoslice.mechanical_delivery_review import (
    build_mechanical_receipt,
    validate_mechanical_receipt,
)
from src.autoslice.package_audit_binding import audit_binding
from src.autoslice.package_relocation_contract import (
    get_value,
    set_value,
    validate_document_root_roles,
)
from src.autoslice.recovery_title_authority import (
    build_recovery_publication_authorities,
    validate_recovery_publication_authority,
)
from src.autoslice.review_evidence_store import load_review

CANDIDATE_ID = "auto_120029_1409_1578"
STEM = f"{CANDIDATE_ID}.recut.burned-final-sapphire72"
TITLE = "【李豆沙】打嗝视频里怎么还藏着雷公电母？！"
REGISTRY_RELATIVE = Path(
    "assets/lidousha/b2_caption_successor/"
    "auto_120029_1409_1578.same-bv-publication-registry.v1.json"
)
REGISTRY_SHA256 = (
    "sha256:2c9c570c406e4e71bc7569b26611e898cc8648b49651496dc91760f7e267e845"
)
EXPECTED_FINAL_VIDEO_SHA256 = (
    "sha256:de28a0237bd9255fdcc97db41ce0762d57dadc45c6a3175dd3393bf2704751a2"
)
EXPECTED_SRT_SHA256 = (
    "sha256:f10409d4ebe626a15ca3414dab41b1efcc0faee8bd805ee848d7967046f2047a"
)
EXPECTED_ASS_SHA256 = (
    "sha256:14823cd0e2ddafd82895a7acd3545a4fa661490ecae840a3ee77bbbd7853bcce"
)
EXPECTED_COVER_SHA256 = (
    "sha256:7c9a2bd2b4551032f3def92add739014d10b6e2108aec16efc09334a516b3d6a"
)
COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z")

RECORD_NAME = f"{STEM}.record.json"
PUBLISH_NAME = f"{STEM}.publish.json"
CHAT_NAME = f"{STEM}.chat-authority.json"
CLIP_NAME = f"{STEM}.clip-context.json"
RAW_VIDEO_NAME = f"{CANDIDATE_ID}.recut.mp4"
FINAL_VIDEO_NAME = f"{STEM}.mp4"
SRT_NAME = f"{STEM}.srt"
PLAIN_SRT_NAME = f"{CANDIDATE_ID}.recut.srt"
ASS_NAME = f"{STEM}.final-sapphire72.ass"
COVER_NAME = f"{STEM}.cover.png"
AI_BACKGROUND_RELATIVE = Path("covers_ai_original/B2-background.png")
TITLE_MASK_RELATIVE = Path("covers_ai_original/B2-cover.title-mask.png")
REFERENCE_RELATIVE = Path("evidence") / f"{CANDIDATE_ID}.host-only-v4-reference.png"
WITNESS_RELATIVE = Path("evidence") / f"{CANDIDATE_ID}.host-only-v4-witness.png"
BUILTIN_PROVENANCE_RELATIVE = Path("builtin_imagegen_source/source.json")

STALE_ROOT_FILES = frozenset(
    {
        f"{CANDIDATE_ID}.record.json",
        "review_manifest.json",
        "package_audit.json",
        "verification",
    }
)


class B2RecoveryFinalizationError(RuntimeError):
    pass


def _need(condition: bool, detail: str) -> None:
    if not condition:
        raise B2RecoveryFinalizationError(detail)


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _json(path: Path) -> dict[str, Any]:
    try:
        value = load_review(path)
    except (OSError, UnicodeError, ValueError) as exc:
        raise B2RecoveryFinalizationError(f"JSON unreadable: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise B2RecoveryFinalizationError(f"JSON object required: {path}")
    return value


def _json_bytes(value: Mapping[str, object]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(raw)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _create_only_json(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            descriptor = -1
            handle.write(_json_bytes(value))
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _copy_create_only(source: Path, output: Path) -> None:
    if output.exists() or output.is_symlink():
        raise B2RecoveryFinalizationError(f"CREATE_ONLY_OUTPUT_EXISTS:{output}")

    def ignore(directory: str, names: list[str]) -> set[str]:
        return set(names).intersection(STALE_ROOT_FILES) if Path(directory) == source else set()

    try:
        shutil.copytree(source, output, copy_function=os.link, ignore=ignore)
    except OSError:
        if output.exists():
            shutil.rmtree(output)
        shutil.copytree(source, output, copy_function=shutil.copy2, ignore=ignore)


def _authority() -> dict[str, object]:
    authorities = build_recovery_publication_authorities(
        candidate_ids={CANDIDATE_ID},
        registry_path=REPO_ROOT / REGISTRY_RELATIVE,
        expected_registry_sha256=REGISTRY_SHA256,
        require_exact_candidate_set=True,
        repo_root=REPO_ROOT,
    )
    return validate_recovery_publication_authority(
        authorities[CANDIDATE_ID],
        candidate_id=CANDIDATE_ID,
        expected_final_title=TITLE,
    )


def _target_paths(output: Path) -> dict[str, Path]:
    return {
        "raw_video": output / RAW_VIDEO_NAME,
        "final_video": output / FINAL_VIDEO_NAME,
        "srt": output / SRT_NAME,
        "plain_srt": output / PLAIN_SRT_NAME,
        "ass": output / ASS_NAME,
        "cover": output / COVER_NAME,
        "ai_background": output / AI_BACKGROUND_RELATIVE,
        "title_mask": output / TITLE_MASK_RELATIVE,
        "reference": output / REFERENCE_RELATIVE,
        "witness": output / WITNESS_RELATIVE,
        "builtin_provenance": output / BUILTIN_PROVENANCE_RELATIVE,
        "chat": output / CHAT_NAME,
        "clip": output / CLIP_NAME,
        "publish": output / PUBLISH_NAME,
        "record": output / RECORD_NAME,
        "audio_witness": output / f"{CANDIDATE_ID}.subtitle-audio-witness.srt",
        "audio_provenance": output / f"{CANDIDATE_ID}.subtitle-audio-provenance.json",
        "audio_correspondence": output / f"{CANDIDATE_ID}.subtitle-audio-correspondence.json",
        "audio_raw": output / f"{CANDIDATE_ID}.subtitle-audio-bcut.raw.json",
    }


def _require_artifacts(paths: Mapping[str, Path]) -> None:
    for label, path in paths.items():
        _need(path.is_file() and not path.is_symlink(), f"regular artifact missing: {label}: {path}")
    for label, digest in {
        "final_video": EXPECTED_FINAL_VIDEO_SHA256,
        "srt": EXPECTED_SRT_SHA256,
        "plain_srt": EXPECTED_SRT_SHA256,
        "ass": EXPECTED_ASS_SHA256,
        "cover": EXPECTED_COVER_SHA256,
    }.items():
        _need(_sha(paths[label]) == digest, f"artifact hash drift: {label}")


def _set(
    document: dict[str, Any],
    pointer: tuple[str, ...],
    target: Path,
    *,
    directory: bool = False,
) -> None:
    if get_value(document, pointer) is None:
        return
    valid = target.is_dir() if directory else target.is_file() and not target.is_symlink()
    _need(valid, f"locator target missing: {target}")
    set_value(document, pointer, str(target))


def _project_cover_generation(generation: object, paths: Mapping[str, Path]) -> None:
    if not isinstance(generation, dict):
        return
    for key, target in {
        "final_cover": paths["cover"],
        "reference_image": paths["reference"],
        "pre_overlay_path": paths["ai_background"],
        "ai_background": paths["ai_background"],
        "builtin_imagegen_provenance_path": paths["builtin_provenance"],
    }.items():
        if key in generation:
            _need(target.is_file() and not target.is_symlink(), f"cover target missing: {target}")
            generation[key] = str(target)
    rendered = generation.get("rendered_text_pixels")
    if isinstance(rendered, dict):
        if "mask_path" in rendered:
            rendered["mask_path"] = str(paths["title_mask"])
        if "pre_overlay_path" in rendered:
            rendered["pre_overlay_path"] = str(paths["ai_background"])
    # Host-identity and source-composition witnesses are immutable evidence,
    # including their historical paths. The native package-binding builder
    # validates portable copies without rewriting those receipt bytes.


def _project_documents(
    *,
    source: Path,
    output: Path,
    record: dict[str, Any],
    publish: dict[str, Any],
    chat: dict[str, Any],
    authority: dict[str, object],
    paths: Mapping[str, Path],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    record = deepcopy(record)
    publish = deepcopy(publish)
    chat = deepcopy(chat)
    for pointer, target in {
        ("media_path",): paths["raw_video"],
        ("subtitle_path",): paths["plain_srt"],
        ("chat_authority_audit_path",): paths["chat"],
        ("clip_context_path",): paths["clip"],
        ("subtitle_ass_path",): paths["ass"],
        ("subtitle_audio_correspondence", "witness_srt_path"): paths["audio_witness"],
        ("subtitle_audio_correspondence", "provenance_path"): paths["audio_provenance"],
        ("subtitle_audio_correspondence", "correspondence_path"): paths["audio_correspondence"],
        ("subtitle_audio_correspondence", "raw_result_path"): paths["audio_raw"],
        ("subtitle_audio_correspondence", "receipt", "inputs", "actual_media", "path"): paths["final_video"],
        ("subtitle_audio_correspondence", "receipt", "inputs", "final_srt", "path"): paths["plain_srt"],
        ("subtitle_audio_correspondence", "receipt", "inputs", "provenance", "path"): paths["audio_provenance"],
        ("subtitle_audio_correspondence", "receipt", "inputs", "witness_srt", "path"): paths["audio_witness"],
        ("burned_preview", "path"): paths["final_video"],
        ("burned_preview", "ass_path"): paths["ass"],
        ("publish_staging", "cover_path"): paths["cover"],
        ("publish_staging", "publish_json_path"): paths["publish"],
    }.items():
        _set(record, pointer, target)
    if get_value(record, ("subtitle_audio_correspondence", "output_dir")) is not None:
        _set(record, ("subtitle_audio_correspondence", "output_dir"), output, directory=True)
    for pointer, target in {
        ("video_path",): paths["final_video"],
        ("cover_path",): paths["cover"],
    }.items():
        _set(publish, pointer, target)
    for pointer, target in {
        ("final_text_srt_path",): paths["plain_srt"],
        ("final_speaker_srt_path",): paths["plain_srt"],
        ("burn_binding", "burned_media_path"): paths["final_video"],
        ("burn_binding", "ass_path"): paths["ass"],
    }.items():
        _set(chat, pointer, target)

    _project_cover_generation(publish.get("cover_generation"), paths)
    staging = record.get("publish_staging")
    _need(isinstance(staging, dict), "record publish_staging missing")
    _project_cover_generation(staging.get("cover_generation"), paths)
    _project_cover_generation(record.get("cover_generation"), paths)

    record["recovery_publication_authority"] = deepcopy(authority)
    staging["recovery_publication_authority"] = deepcopy(authority)
    publish["recovery_publication_authority"] = deepcopy(authority)

    dummy_source = output / ".unused-candidate-source"
    dummy_destination = output / ".unused-candidate-destination"
    mappings = (
        (str(source), str(output)),
        (str(dummy_source), str(dummy_destination)),
        (str(REPO_ROOT), str(REPO_ROOT)),
    )
    validate_document_root_roles(record, kind="record", mappings=mappings)
    validate_document_root_roles(publish, kind="publish", mappings=mappings)
    validate_document_root_roles(chat, kind="chat", mappings=mappings)
    return record, publish, chat


def _write_documents(
    *,
    paths: Mapping[str, Path],
    record: dict[str, Any],
    publish: dict[str, Any],
    chat: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    _atomic_write(paths["chat"], _json_bytes(chat))
    chat_sha = _sha(paths["chat"])
    publish_hashes = publish.get("artifact_hashes")
    _need(isinstance(publish_hashes, dict), "publish artifact_hashes missing")
    publish_hashes["chat_authority_audit_sha256"] = chat_sha
    _atomic_write(paths["publish"], _json_bytes(publish))
    publish_sha = _sha(paths["publish"])
    record_hashes = record.get("artifact_hashes")
    _need(isinstance(record_hashes, dict), "record artifact_hashes missing")
    record_hashes["chat_authority_audit_sha256"] = chat_sha
    record_hashes["publish_draft_sha256"] = publish_sha
    _atomic_write(paths["record"], _json_bytes(record))
    return _json(paths["record"]), _json(paths["publish"]), _json(paths["chat"])


def _state(authority: Mapping[str, object]) -> dict[str, object]:
    return {
        "date": "2026-08-23",
        "status": "review_ready",
        "run_mode": "RECOVERY_REVIEW",
        "upload_allowed": False,
        "talk_selection_contract": {
            "mode": "EXACT_CANDIDATE_SET_NO_BACKFILL",
            "candidate_ids": [CANDIDATE_ID],
        },
        "delivery_rerun_plan": {
            "schema_version": "recovery-review-talk-rerun-plan.v7",
            "recovery_publication_authorities_by_candidate": {
                CANDIDATE_ID: dict(authority)
            },
        },
        "picks": [
            {
                "candidate_id": CANDIDATE_ID,
                "status": "review_ready",
                "bundle_lifecycle": "CURRENT",
                "bundle_compliance": "COMPLIANT",
                "rc": 0,
            }
        ],
    }


def finalize(
    *, source_package: Path, output: Path, deployed_commit: str
) -> dict[str, object]:
    source = source_package.resolve(strict=True)
    output = output.absolute()
    _need(source.is_dir() and not source.is_symlink(), "source package must be a real directory")
    _need(COMMIT_RE.fullmatch(deployed_commit) is not None, "deployed commit must be full 40-hex")
    source_audit = audit_package(source)
    _need(
        source_audit.get("passed") is True
        and source_audit.get("blocking_issue_count") == 0,
        "source package canonical audit failed",
    )

    _copy_create_only(source, output)
    # The canonical recovery builder resolves sidecars by the final record
    # stem. Materialize byte-identical inputs before projecting mutable
    # locators; historical files and their provenance remain untouched.
    for original, canonical in {
        f"{CANDIDATE_ID}.recut.publish.json": PUBLISH_NAME,
        f"{CANDIDATE_ID}.chat-authority.json": CHAT_NAME,
        f"{CANDIDATE_ID}.clip-context.json": CLIP_NAME,
        f"{CANDIDATE_ID}.recut.final-sapphire72.ass": ASS_NAME,
    }.items():
        _need(not (output / canonical).exists(), f"canonical sidecar already exists: {canonical}")
        shutil.copy2(output / original, output / canonical)
    for suffix in (
        "subtitle-audio-witness.srt", "subtitle-audio-provenance.json",
        "subtitle-audio-correspondence.json", "subtitle-audio-bcut.raw.json",
    ):
        shutil.copy2(output / f"{CANDIDATE_ID}.{suffix}", output / f"{STEM}.{suffix}")
    paths = _target_paths(output)
    _require_artifacts(paths)
    authority = _authority()
    _need(authority.get("bvid") == "BV18dac6iEvJ", "authority BVID drift")
    _need(authority.get("aid") == 117324162076744, "authority AID drift")
    _need(authority.get("cid") == 42154658915, "authority CID drift")

    record = _json(paths["record"])
    publish = _json(paths["publish"])
    chat = _json(paths["chat"])
    _need(publish.get("title") == TITLE, "publish title drift")
    staging = record.get("publish_staging")
    _need(isinstance(staging, dict), "record publish_staging missing")
    source_fact = staging.get("source_fact_review")
    _need(isinstance(source_fact, dict), "B2 source fact successor missing")
    validate_final_review_successor(
        source_fact.get("final_review_successor"),
        expected_srt_sha256=EXPECTED_SRT_SHA256,
    )

    record, publish, chat = _project_documents(
        source=source,
        output=output,
        record=record,
        publish=publish,
        chat=chat,
        authority=authority,
        paths=paths,
    )
    record, publish, chat = _write_documents(
        paths=paths,
        record=record,
        publish=publish,
        chat=chat,
    )
    # Match the uploader's final-SRT-stem lookup, not just the auditor's
    # candidate-ID lookup. Both must replay the same evidence successfully.
    validate_final_subtitle_audio_check(
        record, paths["srt"], paths["final_video"], output
    )

    state = _state(authority)
    _create_only_json(output / "b2-caption-recovery-state.json", state)
    _create_only_json(
        output / "b2-caption-recovery-authority.json",
        {
            "schema_version": "b2-caption-same-bv-recovery-authority-consumption.v1",
            "candidate_id": CANDIDATE_ID,
            "registry_repo_path": str(REGISTRY_RELATIVE),
            "registry_sha256": REGISTRY_SHA256,
            "deployed_commit": deployed_commit,
            "recovery_publication_authority": authority,
        },
    )

    manifest = build_manifest(
        package_root=output,
        state=state,
        deployed_commit=deployed_commit,
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    _atomic_write(output / "review_manifest.json", _json_bytes(manifest))

    audit = audit_package(output)
    _need(
        audit.get("passed") is True
        and audit.get("blocking_issue_count") == 0
        and not audit.get("issues"),
        "terminal package canonical audit failed",
    )
    verification = output / "verification"
    verification.mkdir(mode=0o700)
    audit_path = verification / "package-audit.json"
    _create_only_json(audit_path, audit)
    _need(audit_binding(audit) == audit_binding(audit_package(output)), "saved canonical audit is stale")

    mechanical = build_mechanical_receipt(output, audit_path)
    validate_mechanical_receipt(
        mechanical,
        output,
        audit_path,
        publication_authority=authority,
    )
    mechanical_path = verification / "mechanical-delivery-review.json"
    _create_only_json(mechanical_path, mechanical)
    final_audit = audit_package(output)
    _need(
        final_audit.get("passed") is True
        and final_audit.get("blocking_issue_count") == 0
        and audit_binding(final_audit) == audit_binding(audit),
        "canonical audit drifted after mechanical receipt",
    )

    final_manifest = _json(output / "review_manifest.json")
    items = final_manifest.get("items")
    _need(isinstance(items, list) and len(items) == 1, "terminal manifest item count drift")
    item = items[0]
    _need(isinstance(item, dict), "terminal manifest item invalid")
    _need(
        final_manifest.get("status")
        == "finished_review_package_no_upload_pending_human_review",
        "terminal manifest status invalid",
    )
    _need(
        item.get("recovery_publication_authority") == authority
        and record.get("recovery_publication_authority") == authority
        and publish.get("recovery_publication_authority") == authority,
        "same-BV authority surface drift",
    )

    result = {
        "schema_version": "b2-caption-same-bv-terminal-recovery-package.v1",
        "status": "READY_FOR_AUTHORIZED_SAME_BV_PREFLIGHT",
        "candidate_id": CANDIDATE_ID,
        "output": str(output),
        "deployed_commit": deployed_commit,
        "bvid": authority["bvid"],
        "aid": authority["aid"],
        "cid": authority["cid"],
        "title": TITLE,
        "authority_sha256": authority["authority_sha256"],
        "manifest_sha256": _sha(output / "review_manifest.json"),
        "record_sha256": _sha(paths["record"]),
        "publish_sha256": _sha(paths["publish"]),
        "chat_sha256": _sha(paths["chat"]),
        "final_video_sha256": _sha(paths["final_video"]),
        "srt_sha256": _sha(paths["srt"]),
        "ass_sha256": _sha(paths["ass"]),
        "cover_sha256": _sha(paths["cover"]),
        "canonical_audit": {
            "passed": final_audit["passed"],
            "blocking_issue_count": final_audit["blocking_issue_count"],
            "issue_count": final_audit.get("issue_count", len(final_audit.get("issues") or [])),
            "sha256": _sha(audit_path),
        },
        "mechanical_delivery_review": {
            "status": mechanical["status"],
            "fresh_human_full_playback_claimed": mechanical[
                "fresh_human_full_playback_claimed"
            ],
            "new_upload_authorized": mechanical["new_upload_authorized"],
            "sha256": _sha(mechanical_path),
        },
        "provider_calls": 0,
        "upload_performed": False,
    }
    _create_only_json(output / "b2-caption-terminal-recovery-package.json", result)
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-package", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--deployed-commit", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = finalize(
        source_package=args.source_package,
        output=args.output,
        deployed_commit=args.deployed_commit,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
