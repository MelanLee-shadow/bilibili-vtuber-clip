#!/usr/bin/env python3
"""Resume one talk package from a hash-bound reviewed subtitle surface.

This is the deterministic escape hatch for a producer that already cut the
right media but whose generative transcription drifts between retries.  It
never runs ASR, title, cover, selector, or upload models.  One immutable plan
binds the reviewed text source, structured-chat audit, media, boundary/timing
audits, human text decision, subtitle regression, and the already-approved
cover generation.  The script then runs only the candidate-specific speaker
tail, burns the verified ASS, commits the package through a roll-forward
journal, rebinds the unchanged cover without a provider call, and atomically
updates state.
"""

from __future__ import annotations

import argparse
import copy
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.apply_subtitle_text_overrides import (  # noqa: E402
    apply_document as apply_text_override_document,
)
from scripts.authorized_upload import (  # noqa: E402
    DEFAULT_UPLOAD_LOCK,
    UploadLockBusy,
    exclusive_upload_lock,
)
from scripts.free_session_autoslice import (  # noqa: E402
    BASE,
    _atomic_write_bytes_file,
    _atomic_write_json_file,
    _bind_repaired_cover,
    _cover_binding_valid,
    _recover_committed_cover_binding,
    _roll_forward_prepared_cover_transactions,
    _validate_repaired_cover_generation,
    delivered_paths,
    read_state,
    talk_pipeline_fingerprint,
    write_reports,
    write_state,
)
from scripts.produce_slice_package import (  # noqa: E402
    _burn_preview_subtitles,
    _sha256,
    _stage_publish_draft,
    _validated_burned_artifact,
    run_speaker_finalizer,
    verify_chat_authority_final_surfaces,
)
from scripts.apply_speaker_turn_overrides import SPEAKER_SUBTITLE_STYLE_ID  # noqa: E402
from src.autoslice.branding_intro import BrandingIntroError, require_branding_intro  # noqa: E402
from src.autoslice.chat_authority import reconcile_pending_text_overrides  # noqa: E402
from src.autoslice.jingting_chunker import parse_srt_cues  # noqa: E402
from src.autoslice.review_evidence import SourceCue  # noqa: E402
from src.autoslice.subtitle_regression import (  # noqa: E402
    verify_subtitle_regression_surfaces,
)


PLAN_SCHEMA = "frozen-talk-resume-plan.v1"
TRANSACTION_SCHEMA = "frozen-talk-resume-transaction.v1"
SHA256_RE = re.compile(r"(?:sha256:)?([0-9a-f]{64})")
SAFE_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,96}")
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


class FrozenTalkResumeError(RuntimeError):
    pass


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalized_sha256(value: object) -> str:
    match = SHA256_RE.fullmatch(str(value or ""))
    if match is None:
        raise FrozenTalkResumeError(f"invalid SHA256 value: {value!r}")
    return match.group(1)


def _read_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise FrozenTalkResumeError(f"{label} is unreadable: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise FrozenTalkResumeError(f"{label} must be a JSON object: {path}")
    return payload


def _is_within(path: Path, roots: tuple[Path, ...]) -> bool:
    return any(path.is_relative_to(root) for root in roots)


def _resolve_input_path(value: object) -> Path:
    if not isinstance(value, str) or not value:
        raise FrozenTalkResumeError("plan input path must be a non-empty string")
    raw = Path(value)
    return raw if raw.is_absolute() else ROOT / raw


def _validated_input(
    plan: Mapping[str, Any],
    name: str,
    *,
    allowed_roots: tuple[Path, ...],
) -> Path:
    inputs = plan.get("inputs")
    entry = inputs.get(name) if isinstance(inputs, Mapping) else None
    if not isinstance(entry, Mapping):
        raise FrozenTalkResumeError(f"plan is missing input {name!r}")
    path = _resolve_input_path(entry.get("path"))
    archive_value = entry.get("archive_path")
    archive = _resolve_input_path(archive_value) if archive_value is not None else None
    expected = _normalized_sha256(entry.get("sha256"))
    if archive is not None and archive.is_file():
        if archive.is_symlink():
            raise FrozenTalkResumeError(
                f"archived input {name} may not be a symlink: {archive}"
            )
        resolved_archive = archive.resolve(strict=True)
        if not _is_within(resolved_archive, allowed_roots):
            raise FrozenTalkResumeError(
                f"archived input {name} escapes allowed roots: {resolved_archive}"
            )
        actual_archive = _sha256_file(resolved_archive)
        if actual_archive != expected:
            raise FrozenTalkResumeError(
                f"archived input {name} hash mismatch: expected {expected}, "
                f"got {actual_archive}"
            )
        return resolved_archive
    if path.is_symlink():
        raise FrozenTalkResumeError(f"input {name} may not be a symlink: {path}")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise FrozenTalkResumeError(f"input {name} is missing: {path}: {exc}") from exc
    if not resolved.is_file() or not _is_within(resolved, allowed_roots):
        raise FrozenTalkResumeError(f"input {name} escapes allowed roots: {resolved}")
    actual = _sha256_file(resolved)
    if actual != expected:
        raise FrozenTalkResumeError(
            f"input {name} hash mismatch: expected {expected}, got {actual}"
        )
    if archive is None:
        return resolved
    if archive.is_symlink():
        raise FrozenTalkResumeError(
            f"archive target for {name} may not be a symlink: {archive}"
        )
    archive_parent = archive.parent.resolve()
    if not _is_within(archive_parent, allowed_roots):
        raise FrozenTalkResumeError(
            f"archive target for {name} escapes allowed roots: {archive}"
        )
    _atomic_write_bytes_file(archive, resolved.read_bytes())
    if _sha256_file(archive) != expected:
        raise FrozenTalkResumeError(f"archived input {name} failed verification")
    return archive.resolve(strict=True)


def load_and_validate_plan(path: Path) -> tuple[dict[str, Any], str, dict[str, Path]]:
    raw = path.read_bytes()
    plan = json.loads(raw)
    if not isinstance(plan, dict) or plan.get("schema_version") != PLAN_SCHEMA:
        raise FrozenTalkResumeError(f"plan schema must be {PLAN_SCHEMA}")
    candidate_id = str(plan.get("candidate_id") or "")
    date = str(plan.get("date") or "")
    if SAFE_ID_RE.fullmatch(candidate_id) is None or DATE_RE.fullmatch(date) is None:
        raise FrozenTalkResumeError("plan candidate_id/date is invalid")
    if plan.get("upload_enabled") is not False:
        raise FrozenTalkResumeError("frozen talk resume must bind upload_enabled=false")
    if not isinstance(plan.get("title"), str) or not plan["title"].strip():
        raise FrozenTalkResumeError("plan title is missing")
    delivery_name = str(plan.get("delivery_name") or "")
    if not delivery_name or Path(delivery_name).name != delivery_name:
        raise FrozenTalkResumeError("plan delivery_name must be one safe basename")

    plan_resolved = path.resolve(strict=True)
    candidate_root = (BASE / "out" / date / candidate_id).resolve()
    delivery_root = (ROOT / "lidousha" / date).resolve()
    allowed_roots = (BASE.resolve(), ROOT.resolve())
    if not _is_within(plan_resolved, allowed_roots):
        raise FrozenTalkResumeError("resume plan escapes the deployment roots")
    names = (
        "media",
        "frozen_text_srt",
        "chat_authority_audit",
        "boundary_audit",
        "timing_qa",
        "producer_spec",
        "text_override",
        "subtitle_regression",
        "speaker_profile",
        "existing_cover",
        "cover_generation_cover",
        "cover_generation_manifest",
        "cover_binding",
        "upload_ledger",
    )
    paths = {
        name: _validated_input(plan, name, allowed_roots=allowed_roots)
        for name in names
    }
    if not paths["media"].is_relative_to(candidate_root):
        raise FrozenTalkResumeError("media is outside the candidate root")
    if not paths["chat_authority_audit"].is_relative_to(candidate_root):
        raise FrozenTalkResumeError("chat audit is outside the candidate root")
    if paths["existing_cover"].parent != delivery_root:
        raise FrozenTalkResumeError("existing cover is outside the bound delivery root")

    text_override = _read_json(paths["text_override"], label="text override")
    if (
        text_override.get("candidate_id") != candidate_id
        or text_override.get("source_srt_sha256")
        != _sha256_file(paths["frozen_text_srt"])
        or text_override.get("text_final_srt_sha256")
        != _normalized_sha256(plan.get("expected_final_text_srt_sha256"))
    ):
        raise FrozenTalkResumeError("text override does not bind the frozen source/final")
    regression = _read_json(paths["subtitle_regression"], label="subtitle regression")
    if regression.get("candidate_id") != candidate_id:
        raise FrozenTalkResumeError("subtitle regression candidate mismatch")
    if paths["cover_generation_manifest"] != paths[
        "cover_generation_cover"
    ].with_suffix(".cover_generation.json").resolve():
        raise FrozenTalkResumeError("cover generation sidecar path mismatch")
    if paths["cover_binding"] != paths[
        "cover_generation_cover"
    ].with_suffix(".cover-binding.json").resolve():
        raise FrozenTalkResumeError("cover binding sidecar path mismatch")
    expected_profile = (
        ROOT / "assets" / "lidousha" / "voiceprint_profile.v1.json"
    ).resolve(strict=True)
    if paths["speaker_profile"] != expected_profile:
        raise FrozenTalkResumeError("speaker profile is not the deployed pinned profile")
    return plan, _sha256_bytes(raw), paths


def _recursive_path_rewrite(value: Any, mapping: Mapping[str, str]) -> Any:
    if isinstance(value, str):
        return mapping.get(value, value)
    if isinstance(value, list):
        return [_recursive_path_rewrite(item, mapping) for item in value]
    if isinstance(value, dict):
        return {
            key: _recursive_path_rewrite(item, mapping)
            for key, item in value.items()
        }
    return value


def _write_intended_blob(root: Path, label: str, payload: bytes) -> Path:
    path = root / "intended" / label
    _atomic_write_bytes_file(path, payload)
    return path


def _commit_transaction(
    *,
    transaction_path: Path,
    plan_sha256: str,
    recovery_code_fingerprint: str,
    candidate_id: str,
    date: str,
    entries: list[dict[str, Any]],
    allowed_target_roots: tuple[Path, ...],
) -> dict[str, Any]:
    if transaction_path.is_file():
        journal = _read_json(transaction_path, label="resume transaction")
        if (
            journal.get("schema_version") != TRANSACTION_SCHEMA
            or journal.get("plan_sha256") != plan_sha256
            or journal.get("recovery_code_fingerprint")
            != recovery_code_fingerprint
            or journal.get("candidate_id") != candidate_id
            or journal.get("date") != date
        ):
            raise FrozenTalkResumeError("existing resume transaction authority drifted")
        status = journal.get("status")
        if status in {"COMMITTED", "FINALIZED"}:
            for entry in journal.get("entries") or []:
                if entry.get("mutable_after_commit"):
                    continue
                target = Path(str(entry.get("target") or ""))
                if not target.is_file() or _sha256_file(target) != entry.get(
                    "intended_sha256"
                ):
                    raise FrozenTalkResumeError(
                        f"committed immutable target drifted: {target}"
                    )
            return journal
        if status != "PREPARED":
            raise FrozenTalkResumeError(f"unsupported transaction state: {status!r}")
        entries = list(journal.get("entries") or [])
    else:
        journal = {
            "schema_version": TRANSACTION_SCHEMA,
            "status": "PREPARED",
            "plan_sha256": plan_sha256,
            "recovery_code_fingerprint": recovery_code_fingerprint,
            "candidate_id": candidate_id,
            "date": date,
            "upload_enabled": False,
            "prepared_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "entries": entries,
        }
        _atomic_write_json_file(transaction_path, journal)

    seen_targets: set[Path] = set()
    blob_root = transaction_path.parent.resolve()
    resolved_target_roots = tuple(root.resolve() for root in allowed_target_roots)
    for entry in entries:
        blob = Path(str(entry.get("intended_blob") or ""))
        target = Path(str(entry.get("target") or ""))
        expected = _normalized_sha256(entry.get("intended_sha256"))
        try:
            blob_resolved = blob.resolve(strict=True)
            target_resolved = target.resolve(strict=False)
        except OSError as exc:
            raise FrozenTalkResumeError(
                f"resume transaction path resolution failed: {target}: {exc}"
            ) from exc
        if (
            blob.is_symlink()
            or not blob.is_file()
            or _sha256_file(blob) != expected
            or target.is_symlink()
            or not blob_resolved.is_relative_to(blob_root)
            or not _is_within(target_resolved, resolved_target_roots)
            or target_resolved in seen_targets
        ):
            raise FrozenTalkResumeError(f"resume transaction entry is invalid: {target}")
        seen_targets.add(target_resolved)
        _atomic_write_bytes_file(target, blob.read_bytes())
        if _sha256_file(target) != expected:
            raise FrozenTalkResumeError(f"resume transaction verification failed: {target}")
    journal["status"] = "COMMITTED"
    journal["committed_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _atomic_write_json_file(transaction_path, journal)
    return journal


def _clone_approved_cover_generation(
    *,
    source_cover: Path,
    target_cover: Path,
    title: str,
    candidate_id: str,
) -> Path:
    source_generation, source_manifest = _validate_repaired_cover_generation(
        cover=source_cover,
        title=title,
        candidate_id=candidate_id,
    )
    target_manifest = target_cover.with_suffix(".cover_generation.json")
    source_bytes = source_cover.read_bytes()
    source_sha256 = _sha256_bytes(source_bytes)
    cloned = copy.deepcopy(source_generation)
    cloned["final_cover"] = str(target_cover.resolve())
    cloned["final_cover_sha256"] = "sha256:" + source_sha256
    cloned["reused_approved_generation"] = {
        "mode": "hash_identical_no_provider_call",
        "source_cover": str(source_cover),
        "source_cover_sha256": "sha256:" + source_sha256,
        "source_manifest": str(source_manifest),
        "source_manifest_sha256": "sha256:" + _sha256_file(source_manifest),
    }

    # The clone is a deterministic two-file generation.  Recover a host loss
    # between the atomic PNG and manifest writes without another paid provider
    # request, but only when the surviving side is exactly the intended one.
    if target_cover.exists() and (
        target_cover.is_symlink()
        or not target_cover.is_file()
        or _sha256_file(target_cover) != source_sha256
    ):
        raise FrozenTalkResumeError("partial cloned cover bytes drifted")
    if target_manifest.exists():
        if target_manifest.is_symlink() or not target_manifest.is_file():
            raise FrozenTalkResumeError("partial cloned cover manifest is invalid")
        existing_manifest = _read_json(
            target_manifest, label="cloned cover generation"
        )
        if existing_manifest != cloned:
            raise FrozenTalkResumeError("partial cloned cover manifest drifted")
    if not target_cover.is_file():
        _atomic_write_bytes_file(target_cover, source_bytes)
    if not target_manifest.is_file():
        _atomic_write_json_file(target_manifest, cloned)
    _validate_repaired_cover_generation(
        cover=target_cover,
        title=title,
        candidate_id=candidate_id,
    )
    return target_cover


def _state_record(state: dict[str, Any], candidate_id: str) -> dict[str, Any]:
    rows = [
        row
        for row in state.get("picks", [])
        if isinstance(row, dict) and row.get("candidate_id") == candidate_id
    ]
    if len(rows) != 1:
        raise FrozenTalkResumeError(
            f"state must contain exactly one talk record for {candidate_id}"
        )
    return rows[0]


def _ffprobe_duration_ms(path: Path) -> int:
    completed = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    return round(float(completed.stdout.strip()) * 1000)


def _assert_upload_ledger_unchanged(
    plan: Mapping[str, Any], ledger_path: Path
) -> None:
    expected = _normalized_sha256(plan["inputs"]["upload_ledger"]["sha256"])
    if _sha256_file(ledger_path) != expected:
        raise FrozenTalkResumeError("upload ledger changed during no-upload resume")


def resume(plan_path: Path, *, speaker_python: Path) -> dict[str, Any]:
    plan, plan_sha256, paths = load_and_validate_plan(plan_path)
    candidate_id = str(plan["candidate_id"])
    date = str(plan["date"])
    title = str(plan["title"])
    recovery_fingerprint = talk_pipeline_fingerprint(candidate_id)
    delivery_name = str(plan["delivery_name"])
    candidate_root = BASE / "out" / date / candidate_id
    recut_root = candidate_root / "replacement_recuts"
    delivery_root = ROOT / "lidousha" / date
    delivery_root.mkdir(parents=True, exist_ok=True)
    delivery_stem = delivery_root / delivery_name
    active_media = recut_root / f"{candidate_id}.recut.mp4"
    if active_media.resolve(strict=True) != paths["media"]:
        raise FrozenTalkResumeError("plan media is not the active recut media")

    boundary = _read_json(paths["boundary_audit"], label="boundary audit")
    timing_qa = _read_json(paths["timing_qa"], label="timing audit")
    start_ms = int(plan.get("delivery_start_ms", -1))
    end_ms = int(plan.get("delivery_end_ms", -1))
    if not (
        boundary.get("verdict") == "ok_sentence_boundary_cut"
        and boundary.get("red_flags") == []
        and boundary.get("final_start_ms") == start_ms
        and boundary.get("final_end_ms") == end_ms
        and isinstance(timing_qa.get("window"), dict)
        and timing_qa["window"].get("start_ms") == start_ms
        and timing_qa["window"].get("end_ms") == end_ms
    ):
        raise FrozenTalkResumeError("boundary/timing audit envelope mismatch")
    expected_duration = end_ms - start_ms
    actual_duration = _ffprobe_duration_ms(active_media)
    if abs(actual_duration - expected_duration) > 120:
        raise FrozenTalkResumeError(
            f"recut duration mismatch: expected {expected_duration}, got {actual_duration}"
        )

    state_file = BASE / "state" / f"{date}.json"
    state = read_state(date)
    record_state = _state_record(state, candidate_id)
    already_bound = (
        isinstance(record_state.get("frozen_talk_resume"), dict)
        and record_state["frozen_talk_resume"].get("plan_sha256") == plan_sha256
        and record_state["frozen_talk_resume"].get(
            "recovery_code_fingerprint"
        )
        == recovery_fingerprint
    )
    expected_state_hash = _normalized_sha256(plan.get("expected_state_sha256"))
    if not already_bound and _sha256_file(state_file) != expected_state_hash:
        raise FrozenTalkResumeError("state changed since the frozen resume plan was approved")
    if record_state.get("title") != title or record_state.get("status") not in {
        "review_ready",
        "ok",
        "quarantine",
    }:
        raise FrozenTalkResumeError("state talk record title/status drifted")
    current_delivery = delivered_paths(date, record_state)
    if current_delivery is None or (
        current_delivery[0].resolve()
        != delivery_stem.with_suffix(".mp4").resolve()
        or current_delivery[1].resolve() != paths["existing_cover"]
    ):
        raise FrozenTalkResumeError("state delivery path drifted from the plan")
    if not already_bound and (
        Path(str(record_state.get("cover_path") or "")).resolve()
        != paths["existing_cover"]
        or _normalized_sha256(record_state.get("cover_sha256"))
        != _sha256_file(paths["existing_cover"])
        or Path(str(record_state.get("cover_generation_path") or "")).resolve()
        != paths["cover_generation_manifest"]
        or _normalized_sha256(record_state.get("cover_generation_sha256"))
        != _sha256_file(paths["cover_generation_manifest"])
        or Path(str(record_state.get("cover_binding_path") or "")).resolve()
        != paths["cover_binding"]
        or _normalized_sha256(record_state.get("cover_binding_sha256"))
        != _sha256_file(paths["cover_binding"])
    ):
        raise FrozenTalkResumeError("state cover authority drifted from the plan")

    resume_id = hashlib.sha256(
        (plan_sha256 + "\0" + recovery_fingerprint).encode("utf-8")
    ).hexdigest()[:16]
    generation_root = candidate_root / "frozen_resume" / "generations" / resume_id
    generation_root.mkdir(parents=True, exist_ok=True)
    transaction_path = generation_root / "resume-transaction.json"

    if not transaction_path.is_file():
        generation_media = generation_root / f"{candidate_id}.recut.mp4"
        generation_source = generation_root / f"{candidate_id}.reviewed-source.srt"
        _atomic_write_bytes_file(generation_media, paths["media"].read_bytes())
        _atomic_write_bytes_file(
            generation_source, paths["frozen_text_srt"].read_bytes()
        )
        if (
            _sha256_file(generation_media) != _sha256_file(paths["media"])
            or _sha256_file(generation_source)
            != _sha256_file(paths["frozen_text_srt"])
        ):
            raise FrozenTalkResumeError("generation input copy drifted")

        text_srt = generation_root / f"{candidate_id}.recut.srt"
        text_manifest_path = generation_root / f"{candidate_id}.recut.text-finalization.json"
        text_manifest = apply_text_override_document(
            generation_source,
            paths["text_override"],
            text_srt,
            text_manifest_path,
        )
        if _sha256_file(text_srt) != _normalized_sha256(
            plan.get("expected_final_text_srt_sha256")
        ):
            raise FrozenTalkResumeError("derived final text hash drifted")

        speaker_srt = generation_root / f"{candidate_id}.recut.speaker-final.srt"
        speaker_ass = generation_root / f"{candidate_id}.recut.speaker-final.ass"
        speaker_manifest_path = generation_root / f"{candidate_id}.recut.speaker-final.json"
        speaker_manifest = run_speaker_finalizer(
            host="localhost",
            candidate_id=candidate_id,
            media_path=generation_media,
            text_srt_path=text_srt,
            output_srt_path=speaker_srt,
            output_ass_path=speaker_ass,
            output_manifest_path=speaker_manifest_path,
            work_dir=generation_root / f"{candidate_id}.speaker-work",
            speaker_python=speaker_python,
        )

        final_text = text_srt.read_text(encoding="utf-8", errors="replace")
        final_speaker = speaker_srt.read_text(encoding="utf-8", errors="replace")
        chat_audit = _read_json(paths["chat_authority_audit"], label="chat audit")
        if not reconcile_pending_text_overrides(
            chat_audit, text_manifest, delivery_start_ms=start_ms
        ):
            raise FrozenTalkResumeError("pending human text authority did not reconcile")
        if not verify_chat_authority_final_surfaces(
            chat_audit,
            final_text_srt=final_text,
            final_speaker_srt=final_speaker,
            delivery_start_ms=start_ms,
            delivery_end_ms=end_ms,
        ):
            raise FrozenTalkResumeError("final speaker/text surfaces broke chat authority")

        regression = verify_subtitle_regression_surfaces(
            paths["subtitle_regression"],
            candidate_id=candidate_id,
            final_text_srt=final_text,
            final_speaker_srt=final_speaker,
        )
        if regression.get("status") != "PASS":
            raise FrozenTalkResumeError("subtitle regression failed")
        regression_path = generation_root / f"{candidate_id}.recut.subtitle-regression.json"
        _atomic_write_json_file(regression_path, regression)

        preliminary_record: dict[str, Any] = {
            "status": "MATERIALIZED",
            "media_path": str(generation_media),
            "subtitle_path": str(text_srt),
            "subtitle_source": "frozen_reviewed_delivery+hash_bound_human_text",
            "start_ms": 0,
            "end_ms": expected_duration,
            "duration_ms": expected_duration,
            "artifact_hashes": {
                "video_sha256": "sha256:" + _sha256(generation_media),
                "subtitle_sha256": "sha256:" + _sha256(text_srt),
                "ass_sha256": "sha256:" + _sha256(speaker_ass),
                "speaker_review_srt_sha256": "sha256:" + _sha256(speaker_srt),
                "subtitle_regression_audit_sha256": "sha256:"
                + _sha256(regression_path),
            },
            "subtitle_regression_audit_path": str(regression_path),
            "subtitle_regression": regression,
            "text_finalization_manifest_path": str(text_manifest_path),
            "speaker_review_srt_path": str(speaker_srt),
            "subtitle_ass_path": str(speaker_ass),
            "subtitle_style": SPEAKER_SUBTITLE_STYLE_ID,
            "speaker_finalization_manifest_path": str(speaker_manifest_path),
            "speaker_finalization": speaker_manifest,
            "subtitle_timing_qa": timing_qa,
            "boundary_audit": boundary,
            "frozen_talk_resume": {
                "schema_version": PLAN_SCHEMA,
                "plan_path": str(plan_path.resolve()),
                "plan_sha256": plan_sha256,
                "recovery_code_fingerprint": recovery_fingerprint,
                "generation_root": str(generation_root),
                "upload_enabled": False,
            },
        }
        try:
            branding_intro = require_branding_intro(ROOT)
        except BrandingIntroError as exc:
            raise FrozenTalkResumeError(f"branding intro unavailable: {exc}") from exc
        burned_record = _burn_preview_subtitles(
            preliminary_record, run_ffmpeg=True, branding_intro=branding_intro
        )
        if not isinstance(burned_record, dict):
            raise FrozenTalkResumeError("speaker burn did not return a record")
        burned_media = _validated_burned_artifact(burned_record)

        active_text = recut_root / f"{candidate_id}.recut.srt"
        active_text_manifest = recut_root / f"{candidate_id}.recut.text-finalization.json"
        active_speaker_srt = recut_root / f"{candidate_id}.recut.speaker-final.srt"
        active_speaker_ass = recut_root / f"{candidate_id}.recut.speaker-final.ass"
        active_speaker_manifest = recut_root / f"{candidate_id}.recut.speaker-final.json"
        active_regression = recut_root / f"{candidate_id}.recut.subtitle-regression.json"
        active_burned = recut_root / f"{candidate_id}.recut.burned-final-speaker.mp4"
        active_record_path = recut_root / f"{candidate_id}.record.json"
        active_publish_path = recut_root / f"{candidate_id}.recut.publish.json"
        active_chat_audit = candidate_root / f"{candidate_id}.chat-authority.json"

        path_mapping = {
            str(generation_media): str(active_media),
            str(text_srt): str(active_text),
            str(text_manifest_path): str(active_text_manifest),
            str(speaker_srt): str(active_speaker_srt),
            str(speaker_ass): str(active_speaker_ass),
            str(speaker_manifest_path): str(active_speaker_manifest),
            str(regression_path): str(active_regression),
            str(burned_media): str(active_burned),
        }
        active_text_manifest_document = _recursive_path_rewrite(
            text_manifest, path_mapping
        )
        active_speaker_manifest_document = _recursive_path_rewrite(
            speaker_manifest, path_mapping
        )
        active_speaker_manifest_bytes = (
            json.dumps(
                active_speaker_manifest_document,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")

        chat_audit.update(
            {
                "final_status": "FINAL_ARTIFACTS_VERIFIED",
                "final_text_srt_path": str(active_text),
                "final_text_srt_sha256": _sha256(text_srt),
                "final_speaker_srt_path": str(active_speaker_srt),
                "final_speaker_srt_sha256": _sha256(speaker_srt),
                "speaker_ass_path": str(active_speaker_ass),
                "speaker_ass_sha256": _sha256(speaker_ass),
                "speaker_manifest_sha256": _sha256_bytes(
                    active_speaker_manifest_bytes
                ),
                "burn_binding": {
                    "burned_media_path": str(delivery_stem.with_suffix(".mp4")),
                    "burned_media_sha256": _sha256(burned_media),
                    "ass_path": str(active_speaker_ass),
                    "ass_sha256": _sha256(speaker_ass),
                },
                "frozen_resume_binding": {
                    "schema_version": PLAN_SCHEMA,
                    "plan_path": str(plan_path.resolve()),
                    "plan_sha256": plan_sha256,
                    "source_text_sha256": _sha256(paths["frozen_text_srt"]),
                    "source_chat_audit_sha256": _sha256(
                        paths["chat_authority_audit"]
                    ),
                    "upload_enabled": False,
                },
            }
        )
        chat_audit_bytes = (
            json.dumps(chat_audit, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n"
        ).encode("utf-8")
        burned_record["artifact_hashes"]["chat_authority_audit_sha256"] = (
            "sha256:" + _sha256_bytes(chat_audit_bytes)
        )
        burned_record["chat_authority_audit_path"] = str(active_chat_audit)
        burned_record["speaker_finalization"] = active_speaker_manifest_document
        burned_record["speaker_finalization_manifest_sha256"] = (
            "sha256:" + _sha256_bytes(active_speaker_manifest_bytes)
        )

        final_cues = [
            SourceCue(
                f"text_final_{index:04d}",
                cue.start_ms,
                cue.end_ms,
                cue.text.strip(),
                "zh",
                "speech",
                1.0,
            )
            for index, cue in enumerate(parse_srt_cues(final_text), start=1)
            if cue.text.strip()
        ]
        staged = _stage_publish_draft(
            burned_record,
            candidate_id=candidate_id,
            title=title,
            cues=final_cues,
            run_ffmpeg=True,
            title_llm_call=None,
            art_direction_llm_call=None,
            skip_cover=True,
            selection_hook=str(plan.get("selection_hook") or ""),
        )
        if not isinstance(staged, dict):
            raise FrozenTalkResumeError("manual publish staging failed")
        staging = staged.get("publish_staging")
        if (
            not isinstance(staging, dict)
            or staging.get("title_authority_status") != "RESOLVED_MANUAL"
            or staging.get("upload_enabled") is not False
        ):
            raise FrozenTalkResumeError("manual title/no-upload staging drifted")
        generation_publish = generation_media.with_suffix(".publish.json")
        publish_document = _read_json(generation_publish, label="publish draft")

        path_mapping.update(
            {
                str(generation_publish): str(active_publish_path),
                str(generation_root / f"{candidate_id}.record.json"): str(
                    active_record_path
                ),
            }
        )
        active_record = _recursive_path_rewrite(staged, path_mapping)
        active_record["delivery_candidate_id"] = candidate_id
        active_record["source_candidate_id"] = candidate_id
        active_record["publish_staging"]["publish_json_path"] = str(
            active_publish_path
        )
        active_publish = _recursive_path_rewrite(publish_document, path_mapping)
        active_publish["video_path"] = str(active_media)

        active_record_bytes = (
            json.dumps(active_record, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n"
        ).encode("utf-8")
        active_publish_bytes = (
            json.dumps(active_publish, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n"
        ).encode("utf-8")
        active_text_manifest_bytes = (
            json.dumps(
                active_text_manifest_document,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")

        intended_root = generation_root
        raw_entries: list[tuple[str, Path, bytes, bool]] = [
            ("active-text.srt", active_text, text_srt.read_bytes(), False),
            (
                "active-text-finalization.json",
                active_text_manifest,
                active_text_manifest_bytes,
                False,
            ),
            (
                "active-speaker.srt",
                active_speaker_srt,
                speaker_srt.read_bytes(),
                False,
            ),
            (
                "active-speaker.ass",
                active_speaker_ass,
                speaker_ass.read_bytes(),
                False,
            ),
            (
                "active-speaker.json",
                active_speaker_manifest,
                active_speaker_manifest_bytes,
                False,
            ),
            (
                "active-regression.json",
                active_regression,
                regression_path.read_bytes(),
                False,
            ),
            (
                "active-burned.mp4",
                active_burned,
                burned_media.read_bytes(),
                False,
            ),
            (
                "active-chat-authority.json",
                active_chat_audit,
                chat_audit_bytes,
                False,
            ),
            ("active-record.json", active_record_path, active_record_bytes, True),
            ("active-publish.json", active_publish_path, active_publish_bytes, True),
            ("delivery.mp4", delivery_stem.with_suffix(".mp4"), burned_media.read_bytes(), False),
            ("delivery.srt", delivery_stem.with_suffix(".srt"), text_srt.read_bytes(), False),
            (
                "delivery-speaker.srt",
                delivery_stem.with_suffix(".speaker.srt"),
                speaker_srt.read_bytes(),
                False,
            ),
            (
                "delivery-speaker.ass",
                delivery_stem.with_suffix(".speaker.ass"),
                speaker_ass.read_bytes(),
                False,
            ),
            (
                "delivery-speaker.json",
                delivery_stem.with_suffix(".speaker.json"),
                active_speaker_manifest_bytes,
                False,
            ),
            (
                "delivery-chat-authority.json",
                delivery_stem.with_suffix(".chat-authority.json"),
                chat_audit_bytes,
                False,
            ),
            (
                "delivery-regression.json",
                delivery_stem.with_suffix(".subtitle-regression.json"),
                regression_path.read_bytes(),
                False,
            ),
            (
                "delivery-text-finalization.json",
                delivery_stem.with_suffix(".text-finalization.json"),
                active_text_manifest_bytes,
                False,
            ),
            (
                "delivery-record.json",
                delivery_stem.with_suffix(".record.json"),
                active_record_bytes,
                True,
            ),
        ]
        entries: list[dict[str, Any]] = []
        for label, target, payload, mutable in raw_entries:
            blob = _write_intended_blob(intended_root, label, payload)
            entries.append(
                {
                    "label": label,
                    "target": str(target),
                    "intended_blob": str(blob),
                    "intended_sha256": _sha256_bytes(payload),
                    "mutable_after_commit": mutable,
                }
            )
        _assert_upload_ledger_unchanged(plan, paths["upload_ledger"])
        _commit_transaction(
            transaction_path=transaction_path,
            plan_sha256=plan_sha256,
            recovery_code_fingerprint=recovery_fingerprint,
            candidate_id=candidate_id,
            date=date,
            entries=entries,
            allowed_target_roots=(candidate_root, delivery_root),
        )
    else:
        _assert_upload_ledger_unchanged(plan, paths["upload_ledger"])
        _commit_transaction(
            transaction_path=transaction_path,
            plan_sha256=plan_sha256,
            recovery_code_fingerprint=recovery_fingerprint,
            candidate_id=candidate_id,
            date=date,
            entries=[],
            allowed_target_roots=(candidate_root, delivery_root),
        )

    state = read_state(date)
    record_state = _state_record(state, candidate_id)
    delivery_mp4 = delivery_stem.with_suffix(".mp4")
    delivery_cover = delivery_stem.with_suffix(".cover.png")
    cover_generation_root = (
        candidate_root
        / "cover_repair"
        / "generations"
        / f"{resume_id}-frozen-rebind"
    )
    cloned_cover = cover_generation_root / "final.cover.png"
    cloned_cover = _clone_approved_cover_generation(
        source_cover=paths["cover_generation_cover"],
        target_cover=cloned_cover,
        title=title,
        candidate_id=candidate_id,
    )
    _roll_forward_prepared_cover_transactions(
        date, record_state, delivery_mp4, delivery_cover
    )
    _recover_committed_cover_binding(date, record_state, delivery_mp4, delivery_cover)
    clone_binding = cloned_cover.with_suffix(".cover-binding.json")
    if not clone_binding.is_file():
        _bind_repaired_cover(
            date,
            record_state,
            delivery_mp4,
            delivery_cover,
            cloned_cover,
        )
    else:
        _recover_committed_cover_binding(
            date, record_state, delivery_mp4, delivery_cover
        )
    if not _cover_binding_valid(
        date, record_state, delivery_mp4, delivery_cover
    ):
        raise FrozenTalkResumeError("reused approved cover did not rebind")

    # Do not expose FINALIZED state when any concurrent/manual publisher has
    # changed the ledger during the potentially long speaker/burn/cover tail.
    _assert_upload_ledger_unchanged(plan, paths["upload_ledger"])

    active_record_path = recut_root / f"{candidate_id}.record.json"
    active_record = _read_json(active_record_path, label="active resumed record")
    active_record_sha = "sha256:" + _sha256_file(active_record_path)
    summary = {
        "candidate_id": candidate_id,
        "final_end_ms": end_ms,
        "closure_sentence": boundary.get("closure_sentence"),
        "boundary_verdict": boundary.get("verdict"),
        "red_flags": boundary.get("red_flags") or [],
        "boundary_repairs": boundary.get("boundary_repairs") or [],
        "timing_qa": timing_qa.get("counts"),
        "cover_status": record_state.get("cover_status"),
        "title": title,
        "delivery": str(delivery_mp4),
        "subtitle": str(delivery_stem.with_suffix(".srt")),
        "speaker_subtitle": str(delivery_stem.with_suffix(".speaker.srt")),
        "speaker_ass": str(delivery_stem.with_suffix(".speaker.ass")),
        "speaker_status": "READY",
        "subtitle_regression_status": "PASS",
        "cover_path": str(delivery_cover),
        "cover_sha256": "sha256:" + _sha256_file(delivery_cover),
        "cover_binding_path": record_state.get("cover_binding_path"),
        "cover_binding_sha256": record_state.get("cover_binding_sha256"),
        "frozen_resume_plan_sha256": "sha256:" + plan_sha256,
        "frozen_resume_recovery_code_fingerprint": recovery_fingerprint,
        "active_record_sha256": active_record_sha,
    }
    record_state.update(
        {
            "status": "review_ready",
            "title": title,
            "summary": summary,
            "red_flags": boundary.get("red_flags") or [],
            "boundary_repairs": boundary.get("boundary_repairs") or [],
            "pipeline_fingerprint": talk_pipeline_fingerprint(candidate_id),
            "frozen_talk_resume": {
                "schema_version": PLAN_SCHEMA,
                "status": "FINALIZED",
                "plan_path": str(plan_path.resolve()),
                "plan_sha256": plan_sha256,
                "recovery_code_fingerprint": recovery_fingerprint,
                "transaction_path": str(transaction_path),
                "generation_root": str(generation_root),
                "active_record_sha256": active_record_sha,
                "upload_enabled": False,
                "finalized_at": time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                ),
            },
        }
    )
    write_state(date, state)
    write_reports(date, state)

    transaction = _read_json(transaction_path, label="resume transaction")
    transaction["status"] = "FINALIZED"
    transaction["finalized_at"] = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
    )
    transaction["cover_binding_path"] = record_state.get("cover_binding_path")
    transaction["cover_binding_sha256"] = record_state.get("cover_binding_sha256")
    _atomic_write_json_file(transaction_path, transaction)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument(
        "--speaker-python",
        type=Path,
        default=Path(
            os.environ.get(
                "AUTOSLICE_SPEAKER_PYTHON",
                "/opt/bilive/autoslice/venv-diar/bin/python",
            )
        ),
    )
    args = parser.parse_args(argv)
    if not (BASE / "DISABLED").is_file():
        raise FrozenTalkResumeError("DISABLED must exist for a frozen production resume")
    lock_path = BASE / "runner.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock_handle:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise FrozenTalkResumeError("runner.lock is busy") from exc
        # Lock order is always runner -> upload.  DISABLED stops unattended
        # ticks, but an already authorized manual uploader is serialized by a
        # different lock and could otherwise read a delivery while we replace
        # it.  Hold the production uploader's fixed lock for the entire tail.
        try:
            with exclusive_upload_lock(DEFAULT_UPLOAD_LOCK):
                summary = resume(args.plan, speaker_python=args.speaker_python)
        except UploadLockBusy as exc:
            raise FrozenTalkResumeError("upload.lock is busy") from exc
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
