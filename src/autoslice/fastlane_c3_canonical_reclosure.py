"""C3-only canonical package reclosure.

This lane is deliberately a create-new-package transaction.  It consumes the
sealed v3 role inventory, applies only the C3 source-fact/speaker/boundary
receipts, and leaves the inventory and repository assets untouched.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from src.autoslice.addressee_attribution import rebuild_speaker_evidence
from src.autoslice.c3_boundary_reclosure_authority import (
    derive_current_boundary_review,
    load_c3_boundary_authority,
    validate_authority_document,
)
from src.autoslice.fastlane_c3_speaker_authority import ASSET as SPEAKER_AUTHORITY_ASSET
from src.autoslice.fastlane_c3_source_fact_supersession import (
    CANDIDATE_ID,
    build_c3_source_fact_supersession,
    load_authority as load_source_fact_supersession_authority,
)
from src.autoslice.fastlane_c3_v3_direct import load_c3_v3_authority
from src.autoslice.jingting_chunker import parse_srt_cues
from src.autoslice.clip_context import clip_context_prompt_text, validate_clip_context
from src.autoslice.review_evidence import SourceCue
from src.autoslice.repository_asset_authority import require_repository_asset_authority


SCHEMA = "c3-canonical-package-reclosure-receipt.v1"
RECEIPT_NAME = "c3-canonical-reclosure-receipt.json"
_STEM = f"{CANDIDATE_ID}.recut.burned-successor-v2"
_DELIVERY_STEM = f"{CANDIDATE_ID}.recut"


class C3CanonicalReclosureError(ValueError):
    """The C3 transaction cannot be proved without guessing."""


@dataclass(frozen=True, slots=True)
class C3CanonicalReclosureResult:
    package_root: Path
    receipt_path: Path
    receipt_sha256: str
    audit_json_path: Path | None = None


@dataclass(frozen=True, slots=True)
class _C3ReclosurePlan:
    """Immutable identity passed to the line947 supersession builder."""

    candidate_id: str
    date: str
    input_manifest_raw_sha256: str
    input_manifest_self_seal: str


def _sha(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _json_sha(value: object) -> str:
    return _sha(_canonical(value))


def _read_regular(path: Path, *, label: str) -> bytes:
    try:
        st = os.lstat(path)
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            raise C3CanonicalReclosureError(f"{label}_NOT_REGULAR")
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            before = os.fstat(fd)
            chunks: list[bytes] = []
            while chunk := os.read(fd, 1 << 20):
                chunks.append(chunk)
            after = os.fstat(fd)
        finally:
            os.close(fd)
        if (before.st_dev, before.st_ino, before.st_size, before.st_nlink) != (after.st_dev, after.st_ino, after.st_size, after.st_nlink):
            raise C3CanonicalReclosureError(f"{label}_DRIFT")
        payload = b"".join(chunks)
        if len(payload) != before.st_size:
            raise C3CanonicalReclosureError(f"{label}_DRIFT")
        return payload
    except C3CanonicalReclosureError:
        raise
    except OSError as exc:
        raise C3CanonicalReclosureError(f"{label}_UNAVAILABLE") from exc


def _fsync_directory(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError as exc:
        raise C3CanonicalReclosureError("C3_DIRECTORY_FSYNC_FAILED") from exc
    try:
        os.fsync(fd)
    except OSError as exc:
        raise C3CanonicalReclosureError("C3_DIRECTORY_FSYNC_FAILED") from exc
    finally:
        os.close(fd)


def _write_new(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        view = memoryview(payload)
        while view:
            count = os.write(fd, view)
            if count <= 0:
                raise C3CanonicalReclosureError("C3_WRITE_FAILED")
            view = view[count:]
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_json(path: Path, value: object) -> bytes:
    payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    if path.exists():
        temporary = path.with_name(f".{path.name}.reclosure-tmp")
        _write_new(temporary, payload)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    else:
        _write_new(path, payload)
    return payload


def _walk_replace(value: object, *, source_fact: Mapping[str, object], boundary: Mapping[str, object]) -> None:
    if isinstance(value, dict):
        for key in list(value):
            if key == "source_fact_review" and isinstance(value[key], Mapping):
                value[key] = json.loads(json.dumps(source_fact, ensure_ascii=False))
            elif key in {"final_delivery_boundary_semantic_review", "boundary_semantic_review"} and isinstance(value[key], Mapping):
                # Only final-delivery reviews are replaced.  Source-full-window
                # review contracts remain evidence and are bound by the C3
                # source-separation witness.
                if value[key].get("review_scope") == "final_delivery":
                    value[key] = json.loads(json.dumps(boundary, ensure_ascii=False))
            else:
                _walk_replace(value[key], source_fact=source_fact, boundary=boundary)
    elif isinstance(value, list):
        for item in value:
            _walk_replace(item, source_fact=source_fact, boundary=boundary)


def _relocate_cover_paths(value: object, *, stage: Path) -> None:
    mapping = {
        "final_cover": stage / f"{CANDIDATE_ID}.recut.cover.png",
        "pre_overlay_path": stage / f"{_STEM}.cover.pre-overlay.png",
        "ai_background": stage / f"{_STEM}.cover.ai-bg.png",
        "mask_path": stage / f"{_STEM}.cover.title-mask.png",
    }
    if isinstance(value, dict):
        for key, item in list(value.items()):
            if key in mapping and isinstance(item, str):
                value[key] = str(mapping[key])
            else:
                _relocate_cover_paths(item, stage=stage)
    elif isinstance(value, list):
        for item in value:
            _relocate_cover_paths(item, stage=stage)


def _set_json_path(root: dict[str, Any], path: tuple[str, ...], value: object) -> None:
    cursor: Any = root
    for part in path[:-1]:
        if not isinstance(cursor, dict) or part not in cursor:
            raise C3CanonicalReclosureError("C3_REQUIRED_POINTER_MISSING:" + "/" + "/".join(path))
        cursor = cursor[part]
    if not isinstance(cursor, dict):
        raise C3CanonicalReclosureError("C3_REQUIRED_POINTER_NOT_OBJECT:" + "/" + "/".join(path))
    cursor[path[-1]] = value


def _changed_pointers(before: object, after: object, path: str = "") -> list[dict[str, str]]:
    if isinstance(before, dict) and isinstance(after, dict):
        rows: list[dict[str, str]] = []
        for key in sorted(set(before) | set(after)):
            child = (path + "/" + key) if path else "/" + key
            if key not in before or key not in after:
                rows.append({"pointer": child, "old": _json_sha(before.get(key)), "new": _json_sha(after.get(key))})
            else:
                rows.extend(_changed_pointers(before[key], after[key], child))
        return rows
    if isinstance(before, list) and isinstance(after, list) and len(before) == len(after):
        rows: list[dict[str, str]] = []
        for index, (old, new) in enumerate(zip(before, after)):
            rows.extend(_changed_pointers(old, new, f"{path}/{index}"))
        return rows
    if before != after:
        return [{"pointer": path or "/", "old": _json_sha(before), "new": _json_sha(after)}]
    return []


def _copy_role(authority: object, role: str, destination: Path) -> Path:
    path = authority.roles.get(role)  # type: ignore[attr-defined]
    if path is None:
        raise C3CanonicalReclosureError(f"C3_ROLE_MISSING:{role}")
    payload = _read_regular(path, label=f"C3_ROLE_{role}")
    target = destination / Path(path).name
    _write_new(target, payload)
    return target


def _copy_alias(source: Path, destination: Path, name: str) -> Path:
    target = destination / name
    payload = _read_regular(source, label="C3_ALIAS_SOURCE")
    if target.exists():
        if target.is_symlink() or not target.is_file() or _read_regular(target, label="C3_ALIAS_TARGET") != payload:
            raise C3CanonicalReclosureError("C3_ALIAS_COLLISION")
        return target
    _write_new(target, payload)
    return target


def _source_cues(path: Path) -> list[SourceCue]:
    cues = parse_srt_cues(path.read_text(encoding="utf-8"))
    return [
        SourceCue(cue_id=str(cue.index), source_start_ms=cue.start_ms, source_end_ms=cue.end_ms, text=cue.text)
        for cue in cues
    ]


def _final_transcript(cues: list[SourceCue]) -> str:
    return "\n".join(cue.text.strip() for cue in cues if cue.text.strip())


def _rebind_story_contract(
    story: dict[str, Any],
    *,
    clip_doc: Mapping[str, object],
    clip_prompt: str,
    final_transcript_sha256: str,
) -> None:
    """Project the one current sidecar into one current StoryContract copy."""
    if story.get("candidate_id") != CANDIDATE_ID:
        raise C3CanonicalReclosureError("C3_STORY_CANDIDATE_DRIFT")
    if story.get("selection_hook") != clip_doc.get("selection_hook"):
        raise C3CanonicalReclosureError("C3_CLIP_CONTEXT_SELECTION_HOOK_DRIFT")
    speech_memory = clip_doc.get("speech_memory")
    if not isinstance(speech_memory, Mapping):
        raise C3CanonicalReclosureError("C3_CLIP_CONTEXT_SPEECH_MEMORY_MISSING")
    expected_binding = {
        "context_sha256": clip_doc.get("context_sha256"),
        "mutation_authorized": clip_doc.get("mutation_authorized"),
        "schema_version": clip_doc.get("schema_version"),
        "speech_memory_ledger_sha256": speech_memory.get("ledger_sha256"),
        "whole_clip_draft_srt_sha256": clip_doc.get("whole_clip_draft_srt_sha256"),
    }
    if any(not isinstance(value, str) for key, value in expected_binding.items() if key != "mutation_authorized"):
        raise C3CanonicalReclosureError("C3_CLIP_CONTEXT_BINDING_MISSING")
    if expected_binding["mutation_authorized"] is not False:
        raise C3CanonicalReclosureError("C3_CLIP_CONTEXT_MUTATION_AUTHORIZED")
    existing = story.get("clip_context_binding")
    if not isinstance(existing, Mapping):
        raise C3CanonicalReclosureError("C3_STORY_CLIP_CONTEXT_BINDING_MISSING")
    story["clip_context_binding"] = expected_binding
    story["clip_context_prompt"] = clip_prompt
    story["transcript_sha256"] = final_transcript_sha256


def _load_json(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    raw = _read_regular(path, label=label)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise C3CanonicalReclosureError(f"{label}_JSON_INVALID") from exc
    if not isinstance(value, dict):
        raise C3CanonicalReclosureError(f"{label}_JSON_OBJECT_REQUIRED")
    return value, raw


def reclose_c3_canonical_package(*, v3_root: Path, destination: Path, repo_root: Path) -> C3CanonicalReclosureResult:
    """Create one deterministic C3 package from a pinned v3 inventory."""
    destination = destination.absolute()
    if destination.exists() or destination.is_symlink():
        raise C3CanonicalReclosureError("C3_DESTINATION_EXISTS")
    authority = load_c3_v3_authority(root=v3_root.absolute())
    source_auth = load_source_fact_supersession_authority(repo_root=repo_root)
    boundary_auth = validate_authority_document(load_c3_boundary_authority(repo_root=repo_root))
    stage = Path(tempfile.mkdtemp(prefix=f".{destination.name}.txn-", dir=str(destination.parent)))
    published = False
    try:
        copied: dict[str, Path] = {}
        for role in authority.roles:
            copied[role] = _copy_role(authority, role, stage)
        # Canonical portable aliases are exact byte copies of pinned roles.
        aliases = {
            "record": f"{CANDIDATE_ID}.record.json",
            "publish": f"{_STEM}.publish.json",
            "plain_srt": f"{CANDIDATE_ID}.recut.srt",
            "delivery_video": f"{CANDIDATE_ID}.recut.mp4",
            "burned_video": f"{_DELIVERY_STEM}.burned-final-speaker.mp4",
            "ass": f"{_DELIVERY_STEM}.speaker-final.ass",
            "speaker_srt": f"{_DELIVERY_STEM}.speaker-final.srt",
            "speaker_manifest": f"{_DELIVERY_STEM}.speaker-finalization.json",
            "chat": f"{CANDIDATE_ID}.chat-authority.json",
            "clip_context": f"{CANDIDATE_ID}.clip-context.json",
            "cover": f"{CANDIDATE_ID}.recut.cover.png",
        }
        for role, name in aliases.items():
            _copy_alias(copied[role], stage, name)
        # Names required by the canonical builder/auditor.
        _copy_alias(copied["publish"], stage, f"{_DELIVERY_STEM}.publish.json")
        _copy_alias(copied["plain_srt"], stage, f"{_DELIVERY_STEM}.srt")
        _copy_alias(copied["plain_srt"], stage, f"{_STEM}.srt")
        _copy_alias(copied["plain_srt"], stage, f"{_STEM}.reviewed-baseline.srt")
        _copy_alias(copied["speaker_srt"], stage, f"{_DELIVERY_STEM}.speaker.srt")
        _copy_alias(copied["speaker_srt"], stage, f"{_STEM}.speaker.srt")
        _copy_alias(copied["speaker_manifest"], stage, f"{_DELIVERY_STEM}.speaker-finalization.json")
        _copy_alias(copied["speaker_manifest"], stage, f"{_STEM}.speaker-finalization.json")
        _copy_alias(copied["speaker_srt"], stage, f"{_DELIVERY_STEM}.burned-final-speaker.speaker.srt")
        _copy_alias(copied["speaker_manifest"], stage, f"{_DELIVERY_STEM}.burned-final-speaker.speaker-finalization.json")
        _copy_alias(copied["ass"], stage, f"{_DELIVERY_STEM}.speaker.ass")
        _copy_alias(copied["ass"], stage, f"{_STEM}.speaker.ass")
        _copy_alias(copied["cover_pre_overlay"], stage, f"{_STEM}.cover.pre-overlay.png")
        _copy_alias(copied["cover_background"], stage, f"{_STEM}.cover.ai-bg.png")
        _copy_alias(copied["cover_title_mask"], stage, f"{_STEM}.cover.title-mask.png")
        _copy_alias(copied["cover_reference"], stage, f"{_STEM}.cover.cover-ref.png")
        _copy_alias(copied["speaker_truth"], stage, f"{_STEM}.operator-reviewed-speaker-truth.json")
        _copy_alias(copied["delivery_binding"], stage, f"{_STEM}.operator-reviewed-delivery-binding.json")
        speaker_authority_path = repo_root / SPEAKER_AUTHORITY_ASSET
        speaker_authority_bytes = _read_regular(
            speaker_authority_path,
            label="C3_SPEAKER_AUTHORITY",
        )
        require_repository_asset_authority(
            repo_root=repo_root,
            relative_path=SPEAKER_AUTHORITY_ASSET,
            observed_bytes=speaker_authority_bytes,
        )
        _write_new(stage / f"{_STEM}.line947-speaker-authority.json", speaker_authority_bytes)
        _write_new(stage / f"{_DELIVERY_STEM}.line947-speaker-authority.json", speaker_authority_bytes)
        _write_new(stage / f"{_DELIVERY_STEM}.burned-final-speaker.line947-speaker-authority.json", speaker_authority_bytes)

        record_path = stage / aliases["record"]
        publish_path = stage / aliases["publish"]
        record, _ = _load_json(record_path, "C3_RECORD")
        publish, _ = _load_json(publish_path, "C3_PUBLISH")
        chat, _ = _load_json(stage / aliases["chat"], "C3_CHAT")
        subtitle_path = stage / f"{_STEM}.srt"
        speaker_srt_path = stage / f"{_STEM}.speaker.srt"
        speaker_manifest_path = stage / f"{_STEM}.speaker-finalization.json"
        subtitle_text = subtitle_path.read_text(encoding="utf-8")
        cues = parse_srt_cues(subtitle_text)
        source_cues = [
            SourceCue(
                cue_id=str(cue.index),
                source_start_ms=cue.start_ms,
                source_end_ms=cue.end_ms,
                text=cue.text,
            )
            for cue in cues
        ]
        speaker_evidence = rebuild_speaker_evidence(
            record,
            source_cues,
            speaker_srt_bytes=speaker_srt_path.read_bytes(),
            speaker_manifest_bytes=speaker_manifest_path.read_bytes(),
        ).speaker_evidence
        story = record.get("story_contract")
        if not isinstance(story, dict) or not isinstance(publish.get("title"), str):
            raise C3CanonicalReclosureError("C3_STORY_CONTRACT_MISSING")
        clip_doc, _ = _load_json(stage / aliases["clip_context"], "C3_CLIP_CONTEXT")
        try:
            validate_clip_context(
                clip_doc,
                candidate_id=CANDIDATE_ID,
                recording_date="2026-08-13",
                source_media_sha256s=[str(x) for x in (story.get("source_media_sha256s") or [])],
            )
        except ValueError as exc:
            raise C3CanonicalReclosureError("C3_CLIP_CONTEXT_AUTHORITY_INVALID") from exc
        clip_prompt = clip_context_prompt_text(clip_doc)
        clip_prompt_sha256 = _sha(clip_prompt.encode("utf-8"))
        source_authority = source_auth.get("authority")
        if not isinstance(source_authority, Mapping):
            raise C3CanonicalReclosureError("C3_SOURCE_FACT_AUTHORITY_INVALID")
        if source_authority.get("clip_context_prompt_sha256") != clip_prompt_sha256:
            raise C3CanonicalReclosureError("C3_CLIP_CONTEXT_AUTHORITY_MISSING")
        final_transcript = _final_transcript(source_cues)
        final_transcript_sha256 = _sha(final_transcript.encode("utf-8"))
        if source_authority.get("final_transcript_sha256") != final_transcript_sha256:
            raise C3CanonicalReclosureError("C3_FINAL_TRANSCRIPT_AUTHORITY_MISSING")
        outer_source_fact, _ = _load_json(copied["source_fact"], "C3_OUTER_SOURCE_FACT")
        source_fact = build_c3_source_fact_supersession(
            repo_root=repo_root,
            outer_receipt=outer_source_fact,
            candidate_id=CANDIDATE_ID,
            recording_date="2026-08-13",
            title=str(publish["title"]),
            selection_hook=str(story.get("selection_hook") or ""),
            final_transcript=final_transcript,
            clip_context_prompt=clip_prompt,
            selection_scorecard=story.get("selection_scorecard"),
            speaker_evidence=speaker_evidence,
            speaker_finalization_sha256=str(
                outer_source_fact.get("fastlane_terminal_source_fact_preservation", {}).get(
                    "speaker_finalization_sha256"
                )
                if isinstance(
                    outer_source_fact.get("fastlane_terminal_source_fact_preservation"),
                    Mapping,
                )
                else ""
            ),
            final_reviewed_srt_path=subtitle_path,
            replay_plan=_C3ReclosurePlan(
                candidate_id=CANDIDATE_ID,
                date="2026-08-13",
                input_manifest_raw_sha256=authority.manifest_raw_sha256,
                input_manifest_self_seal=authority.manifest_self_seal,
            ),
        )
        stale_review = chat.get("final_review_audit", {}).get("boundary_semantic_review")
        boundary = derive_current_boundary_review(
            stale_review=stale_review,
            cues=cues,
            owner_verification=record["boundary_audit"]["required_boundary_owner_verification"],
            coverage_verification=record["boundary_audit"]["delivery_coverage_verification"],
            authority=boundary_auth,
        )
        before_docs = copy.deepcopy({"record": record, "publish": publish, "chat": chat})
        _walk_replace(record, source_fact=source_fact, boundary=boundary)
        _walk_replace(publish, source_fact=source_fact, boundary=boundary)
        _walk_replace(chat, source_fact=source_fact, boundary=boundary)
        record_story = record.get("story_contract")
        record_staging = record.get("publish_staging")
        staging_cover = (
            record_staging.get("cover_generation")
            if isinstance(record_staging, Mapping)
            else None
        )
        staging_story = (
            staging_cover.get("story_contract")
            if isinstance(staging_cover, Mapping)
            else None
        )
        publish_cover = publish.get("cover_generation")
        publish_story = (
            publish_cover.get("story_contract")
            if isinstance(publish_cover, Mapping)
            else None
        )
        for story_copy in (record_story, staging_story, publish_story):
            if not isinstance(story_copy, dict):
                raise C3CanonicalReclosureError("C3_STORY_CONTRACT_COPY_MISSING")
            _rebind_story_contract(
                story_copy,
                clip_doc=clip_doc,
                clip_prompt=clip_prompt,
                final_transcript_sha256=final_transcript_sha256,
            )
        record["clip_context_payload_sha256"] = str(clip_doc["context_sha256"])
        _relocate_cover_paths(record, stage=destination)
        _relocate_cover_paths(publish, stage=destination)
        # Portable paths and exact derived hashes.
        _set_json_path(record, ("speaker_review_srt_path",), f"{_STEM}.speaker.srt")
        _set_json_path(record, ("speaker_finalization_manifest_path",), f"{_STEM}.speaker-finalization.json")
        _set_json_path(record, ("chat_authority_audit_path",), f"{CANDIDATE_ID}.chat-authority.json")
        _set_json_path(record, ("clip_context_path",), f"{CANDIDATE_ID}.clip-context.json")
        _set_json_path(record, ("subtitle_path",), f"{_STEM}.srt")
        _set_json_path(record, ("media_path",), f"{CANDIDATE_ID}.recut.mp4")
        record.setdefault("artifact_hashes", {}).update({"chat_authority_audit_sha256": _sha(copied["chat"].read_bytes()), "clip_context_file_sha256": _sha(copied["clip_context"].read_bytes()), "speaker_review_srt_sha256": _sha(speaker_srt_path.read_bytes()), "subtitle_sha256": _sha(subtitle_path.read_bytes()), "ass_sha256": _sha((stage / f"{_STEM}.speaker.ass").read_bytes()), "burned_video_sha256": _sha((stage / aliases["burned_video"]).read_bytes()), "video_sha256": _sha((stage / aliases["delivery_video"]).read_bytes())})
        _set_json_path(publish, ("cover_path",), f"{CANDIDATE_ID}.recut.cover.png")
        _set_json_path(publish, ("media_path",), f"{CANDIDATE_ID}.recut.mp4")
        _set_json_path(publish, ("video_path",), f"{CANDIDATE_ID}.recut.mp4")
        publish.setdefault("artifact_hashes", {}).update(record["artifact_hashes"])
        _set_json_path(chat, ("input_srt_sha256",), _sha(subtitle_path.read_bytes()).removeprefix("sha256:"))
        for key in ("output_srt_sha256", "final_output_srt_sha256", "post_transcript_entity_output_srt_sha256"):
            chat[key] = _sha(subtitle_path.read_bytes()).removeprefix("sha256:")
        chat["speaker_ass_sha256"] = _sha((stage / f"{_STEM}.speaker.ass").read_bytes()).removeprefix("sha256:")
        chat["final_speaker_srt_sha256"] = _sha(speaker_srt_path.read_bytes()).removeprefix("sha256:")
        chat_raw = _write_json(stage / aliases["chat"], chat)
        record["artifact_hashes"]["chat_authority_audit_sha256"] = _sha(chat_raw)
        publish.setdefault("artifact_hashes", {})["chat_authority_audit_sha256"] = _sha(chat_raw)
        _write_json(publish_path, publish)
        _write_json(stage / f"{_DELIVERY_STEM}.publish.json", publish)
        _write_json(record_path, record)
        # All changed pointers are recorded against the immutable v3 role JSON.
        changes = []
        for name, old in before_docs.items():
            changes.extend({"surface": name, **row} for row in _changed_pointers(old, {"record": record, "publish": publish, "chat": chat}[name]))
        receipt = {"schema_version": SCHEMA, "candidate_id": CANDIDATE_ID, "input_v3": {"manifest_raw_sha256": authority.manifest_raw_sha256, "manifest_self_seal": authority.manifest_self_seal, "root_tree_sha256": authority.root_tree_sha256, "role_bytes": {role: _sha(path.read_bytes()) for role, path in sorted(copied.items())}}, "changed_json_pointers": changes, "source_fact_review": {"decision": source_fact["decision"], "receipt_sha256": source_fact["receipt_sha256"], "authority_sha256": source_fact["c3_terminal_source_fact_supersession"]["authority_sha256"]}, "boundary_reclosure": boundary["c3_derived_boundary_reclosure_receipt"], "speaker_authority": {"asset": str(SPEAKER_AUTHORITY_ASSET), "sha256": _sha((stage / f"{_STEM}.line947-speaker-authority.json").read_bytes())}, "direct_ivan_authority": {"source_fact_authority_sha256": source_auth["authority_sha256"], "boundary_authority_sha256": boundary_auth["authority_sha256"]}, "output": {"tree_sha256": None, "provider": False, "upload": False, "state": False, "deploy": False}, "status": "PASS"}
        receipt["output"]["tree_sha256"] = _tree_sha(stage)
        receipt_raw = _write_json(stage / RECEIPT_NAME, receipt)
        receipt_sha = _sha(receipt_raw)
        _fsync_directory(stage)
        os.replace(stage, destination)
        published = True
        try:
            _fsync_directory(destination.parent)
        except Exception as exc:
            raise C3CanonicalReclosureError("C3_COMMITTED_BUT_DURABILITY_UNCONFIRMED") from exc
        return C3CanonicalReclosureResult(destination, destination / RECEIPT_NAME, receipt_sha)
    except Exception:
        if not published:
            shutil.rmtree(stage, ignore_errors=True)
        raise


def _tree_sha(root: Path) -> str:
    rows: list[dict[str, object]] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        rows.append({"path": path.relative_to(root).as_posix(), "bytes": path.stat().st_size, "sha256": _sha(path.read_bytes())})
    return _sha(_canonical(rows))

# Explicit aliases for callers and tests.
build_c3_canonical_reclosure = reclose_c3_canonical_package
create_c3_canonical_reclosure = reclose_c3_canonical_package
