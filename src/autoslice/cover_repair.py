"""Cover-repair machinery: transactional AI-cover regeneration + binding proofs.

Extracted from scripts/free_session_autoslice.py (2026-07-15 屎山治理第三刀).
The runner keeps only the `repair_covers` entry point (which drives the AI
image subprocess and is the sole caller of the three monkeypatched functions
cover_repair_needed / _cover_authority_preflight / _bind_repaired_cover — so
their patches reach through the runner namespace unchanged). Everything else
moves here.

Runner globals (paths, constants, delivery helpers) are resolved at CALL TIME
through the ``_runner`` module reference, so a test that monkeypatches them on
the runner steers this code with zero context threading. The shared proxy
resolves the already-live runner module lazily, avoiding a circular import in
both package and cron script execution modes.
"""

from __future__ import annotations

import copy
import hashlib
import json
import time
from pathlib import Path

from src.autoslice.runner_proxy import RunnerProxy
from src.autoslice.cover_route_evidence import (
    build_cover_route_decision,
    record_cover_route_execution,
    relationship_visual_safety_required,
    validate_cover_route_decision,
)
from src.autoslice.story_contract import cover_story_contract_binding_matches
from src.autoslice.verified_io import (
    _matches_sha256,
    _read_json_object,
    _document_video_hash,
)


_runner = RunnerProxy()


def _validate_repaired_cover_generation(
    *, cover: Path, title: str, candidate_id: str
) -> tuple[dict, Path]:
    manifest_path = cover.with_suffix(".cover_generation.json")
    try:
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"cover generation manifest is missing or invalid: {exc}") from exc
    if not isinstance(document, dict):
        raise ValueError("cover generation manifest must be an object")
    if document.get("status") != "AI_COVER_READY":
        raise ValueError("cover generation did not reach AI_COVER_READY")
    if document.get("workflow") != "regenerate_lidousha_cover":
        raise ValueError("unexpected cover generation workflow")
    if (
        document.get("method") != "images.edit"
        or document.get("image_gen_model") != "cpa"
        or document.get("fallback_used") is not False
    ):
        raise ValueError("cover generation must be a real images.edit result without frame fallback")
    if document.get("candidate_id") != candidate_id or document.get("title") != title:
        raise ValueError("cover generation candidate/title binding mismatch")
    model = str(document.get("model") or "")
    if model not in {"gpt-image-2", "gpt-image-1.5"}:
        raise ValueError(f"unapproved cover image model: {model!r}")
    attempted = [str(item) for item in document.get("attempted_models") or []]
    fallback_used = bool(document.get("model_fallback_used"))
    if not attempted or attempted[-1] != model or len(attempted) != len(set(attempted)):
        raise ValueError("attempted_models must be a unique ordered chain ending in the selected model")
    if fallback_used != (len(attempted) > 1):
        raise ValueError("cover model fallback flag does not match attempted_models")
    if fallback_used and attempted != ["gpt-image-2", "gpt-image-1.5"]:
        raise ValueError("unapproved cover model fallback chain")
    try:
        if Path(str(document.get("final_cover") or "")).resolve(strict=True) != cover.resolve(strict=True):
            raise ValueError("cover generation final_cover path mismatch")
    except OSError as exc:
        raise ValueError(f"cover generation final artifact is missing: {exc}") from exc
    if not _matches_sha256(cover, str(document.get("final_cover_sha256") or "")):
        raise ValueError("cover generation final_cover hash mismatch")
    for path_key, hash_key in (
        ("ai_background", "ai_background_sha256"),
        ("reference_image", "reference_sha256"),
        ("request_path", "request_sha256"),
        ("response_path", "response_sha256"),
    ):
        path_value, hash_value = document.get(path_key), document.get(hash_key)
        if not isinstance(path_value, str) or not isinstance(hash_value, str):
            raise ValueError(f"cover generation missing {path_key}/{hash_key}")
        if not _matches_sha256(Path(path_value), hash_value):
            raise ValueError(f"cover generation {path_key} hash mismatch")
    return document, manifest_path


def _active_story_contract(
    documents: list[tuple[Path, dict]],
) -> dict | None:
    """Resolve one frozen story contract from the active publication surface."""

    authoritative_contracts: list[dict] = []
    cover_bindings: list[dict] = []
    for _path, document in documents:
        authoritative = document.get("story_contract")
        if (
            isinstance(authoritative, dict)
            and authoritative not in authoritative_contracts
        ):
            authoritative_contracts.append(authoritative)
        publish_view = (
            document
            if document.get("schema_version") == "shadow-publish-draft.v1"
            else document.get("publish_staging")
        )
        if isinstance(publish_view, dict):
            generation = publish_view.get("cover_generation")
            if isinstance(generation, dict):
                binding = generation.get("story_contract")
                if isinstance(binding, dict) and binding not in cover_bindings:
                    cover_bindings.append(binding)
    if not authoritative_contracts and not cover_bindings:
        return None
    if len(authoritative_contracts) > 1:
        raise ValueError("active cover documents disagree on story contract")
    if authoritative_contracts:
        authority = authoritative_contracts[0]
        if any(
            not cover_story_contract_binding_matches(authority, binding)
            for binding in cover_bindings
        ):
            raise ValueError(
                "active cover StoryContract projection disagrees with authority"
            )
        return copy.deepcopy(authority)
    if len(cover_bindings) != 1:
        raise ValueError("active cover documents disagree on story contract")
    # Legacy packages may predate a complete top-level StoryContract.  Preserve
    # their exact, mutually-agreed cover binding as the best active authority.
    return copy.deepcopy(cover_bindings[0])


def _enrich_repaired_cover_generation(
    *,
    generation: dict,
    generation_path: Path,
    documents: list[tuple[Path, dict]],
    title: str,
) -> tuple[dict, Path]:
    """Bind generic cover repair to the active story and route authority.

    ``regenerate_lidousha_cover`` owns the paid image result, but it does not
    know the producer's frozen StoryContract or treatment-router evidence.
    Before the new generation becomes immutable through a repair binding,
    attach those active documents and record the actual cpa_redraw execution.
    """

    story_contract = _active_story_contract(documents)
    if story_contract is None:
        # Legacy packages predate StoryContract.  Keep their historical repair
        # behavior; current package audits will still refuse missing authority.
        return generation, generation_path
    if relationship_visual_safety_required(story_contract):
        raise ValueError(
            "COVER_RELATIONSHIP_REFERENCE_UNRESOLVED: generic cover repair "
            "cannot prove every participant in a relationship story"
        )
    enriched = copy.deepcopy(generation)
    cover_text = str(enriched.get("cover_text") or title)
    reference_authority = story_contract.get("cover_reference_authority")
    enriched["story_contract"] = story_contract
    enriched["cover_origin"] = "AI_REDRAW"
    enriched["reference_authority"] = (
        copy.deepcopy(reference_authority)
        if isinstance(reference_authority, dict)
        else None
    )
    enriched["route_decision"] = build_cover_route_decision(
        selected_treatment="cpa_redraw",
        selected_rationale=(
            "cover-only repair replaced a missing or invalid cover under the "
            "active title and StoryContract authority"
        ),
        story_contract=story_contract,
        reference_authority=reference_authority,
        title=title,
        cover_text=cover_text,
        decision_inputs={
            "cover_mode": "repair",
            "is_song": bool(enriched.get("is_song")),
            "manual_title_or_full_text_contract": False,
            "frame_score": None,
            "frame_emotion": None,
            "subject_confident": None,
            "motion_dispersion_frac": None,
            "verified_stream_frame": False,
            "reference_authority_id": (
                reference_authority.get("candidate_id")
                if isinstance(reference_authority, dict)
                else None
            ),
        },
    )
    record_cover_route_execution(
        enriched,
        actual_treatment="cpa_redraw",
        execution_status="READY",
        image_generation_attempted=True,
        image_generation_used=True,
        detail="transactional cover-only repair completed",
    )
    _runner._atomic_write_json_file(generation_path, enriched)
    return _validate_repaired_cover_generation(
        cover=Path(str(enriched["final_cover"])),
        title=title,
        candidate_id=str(enriched["candidate_id"]),
    )


def _active_cover_documents(
    *, date: str, candidate_id: str, title: str, mp4: Path, media_sha256: str
) -> list[tuple[Path, dict]]:
    """Load and validate only the delivery record plus its explicitly-bound
    active source record/publish draft.  Never recursively glob a candidate
    directory: old attempts and quarantines are immutable evidence."""

    delivery_record = mp4.with_suffix(".record.json")
    delivery_document = _read_json_object(delivery_record, label="delivery record")
    delivery_staging = delivery_document.get("publish_staging")
    if not isinstance(delivery_staging, dict):
        raise ValueError("delivery record has no publish_staging object")
    if delivery_staging.get("title") != title or delivery_staging.get("upload_enabled") is not False:
        raise ValueError("delivery record title/upload binding mismatch")
    if delivery_document.get("delivery_candidate_id") not in (None, candidate_id):
        raise ValueError("delivery record candidate binding mismatch")
    source_candidate_id = str(delivery_document.get("source_candidate_id") or candidate_id)
    if _document_video_hash(delivery_document) != media_sha256:
        raise ValueError("delivery record video hash does not match current delivery")

    active_root = (_runner.BASE / "out" / date / candidate_id).resolve()
    explicit_publish = delivery_staging.get("publish_json_path")
    if isinstance(explicit_publish, str) and explicit_publish:
        publish_path = Path(explicit_publish)
    else:
        publish_path = active_root / "replacement_recuts" / f"{candidate_id}.recut.publish.json"
    try:
        publish_resolved = publish_path.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"active publish draft is missing: {publish_path}") from exc
    if not publish_resolved.is_relative_to(active_root):
        raise ValueError("active publish draft escapes the current candidate root")

    publish_document = _read_json_object(publish_resolved, label="active publish draft")
    if (
        publish_document.get("schema_version") != "shadow-publish-draft.v1"
        or publish_document.get("candidate_id") != source_candidate_id
        or publish_document.get("title") != title
        or publish_document.get("upload_enabled") is not False
        or _document_video_hash(publish_document) != media_sha256
    ):
        raise ValueError("active publish candidate/title/video/upload binding mismatch")

    name = publish_resolved.name
    if name.endswith(".recut.publish.json"):
        source_record = publish_resolved.with_name(name[: -len(".recut.publish.json")] + ".record.json")
    elif name.endswith(".publish.json"):
        source_record = publish_resolved.with_name(name[: -len(".publish.json")] + ".record.json")
    else:
        raise ValueError(f"unrecognized active publish filename: {publish_resolved.name}")
    source_document = _read_json_object(source_record, label="active source record")
    source_staging = source_document.get("publish_staging")
    if not isinstance(source_staging, dict):
        raise ValueError("active source record has no publish_staging object")
    if (
        source_staging.get("title") != title
        or source_staging.get("upload_enabled") is not False
        or _document_video_hash(source_document) != media_sha256
    ):
        raise ValueError("active source record title/video/upload binding mismatch")
    if source_document.get("delivery_candidate_id") not in (None, candidate_id):
        raise ValueError("active source record candidate binding mismatch")
    if str(source_document.get("source_candidate_id") or source_candidate_id) != source_candidate_id:
        raise ValueError("active source record source-candidate binding mismatch")
    explicit_source_publish = source_staging.get("publish_json_path")
    if isinstance(explicit_source_publish, str) and Path(explicit_source_publish).resolve() != publish_resolved:
        raise ValueError("active source record points at a different publish draft")

    documents: list[tuple[Path, dict]] = [(delivery_record, delivery_document)]
    if source_record.resolve() != delivery_record.resolve():
        documents.append((source_record, source_document))
    documents.append((publish_resolved, publish_document))
    return documents


def _active_song_delivery_manifest(
    rec: dict,
    *,
    candidate_id: str,
    mp4: Path,
    documents: list[tuple[Path, dict]],
) -> tuple[Path, dict] | None:
    manifest_value = rec.get("delivery_manifest_path")
    if manifest_value is None:
        return None
    manifest_sha256 = rec.get("delivery_manifest_sha256")
    if not isinstance(manifest_value, str) or not isinstance(manifest_sha256, str):
        raise ValueError("song delivery manifest state binding is incomplete")
    manifest_path = Path(manifest_value)
    try:
        manifest_resolved = manifest_path.resolve(strict=True)
    except OSError as exc:
        raise ValueError("song delivery manifest is missing") from exc
    if manifest_resolved.parent != mp4.parent.resolve() or not _matches_sha256(
        manifest_resolved, manifest_sha256
    ):
        raise ValueError("song delivery manifest path/hash binding mismatch")
    manifest = _read_json_object(manifest_resolved, label="song delivery manifest")
    artifacts = manifest.get("artifacts")
    if (
        manifest.get("schema_version") != _runner.VERIFIED_SONG_DELIVERY_SCHEMA_VERSION
        or manifest.get("status") != "DELIVERED_NO_UPLOAD"
        or manifest.get("candidate_id") != candidate_id
        or manifest.get("upload_enabled") is not False
        or not isinstance(artifacts, dict)
    ):
        raise ValueError("song delivery manifest authority mismatch")
    video = artifacts.get("video")
    if not isinstance(video, dict):
        raise ValueError("song delivery manifest has no video artifact")
    try:
        video_path = Path(str(video.get("path") or "")).resolve(strict=True)
    except OSError as exc:
        raise ValueError("song delivery manifest video is missing") from exc
    if video_path != mp4.resolve() or not _matches_sha256(mp4, str(video.get("sha256") or "")):
        raise ValueError("song delivery manifest video binding mismatch")

    delivery_record = mp4.with_suffix(".record.json").resolve(strict=True)
    source_records = [path.resolve(strict=True) for path, _doc in documents if path.suffixes[-2:] == [".record", ".json"] and path.resolve() != delivery_record]
    if len(source_records) != 1:
        raise ValueError("song delivery manifest requires one active source record")
    active_record = artifacts.get("active_record")
    if not isinstance(active_record, dict):
        raise ValueError("song delivery manifest has no active_record artifact")
    try:
        delivered_record_path = Path(str(active_record.get("path") or "")).resolve(strict=True)
        source_record_path = Path(str(active_record.get("source_path") or "")).resolve(strict=True)
    except OSError as exc:
        raise ValueError("song delivery manifest record artifact is missing") from exc
    if (
        delivered_record_path != delivery_record
        or source_record_path != source_records[0]
        or not _matches_sha256(delivery_record, str(active_record.get("sha256") or ""))
        or not _matches_sha256(source_records[0], str(active_record.get("source_sha256") or ""))
    ):
        raise ValueError("song delivery manifest active_record binding mismatch")
    delivered_sidecars = rec.get("delivered_sidecars")
    delivered_sidecar_hashes = rec.get("delivered_sidecar_hashes")
    if not isinstance(delivered_sidecars, dict) or not isinstance(
        delivered_sidecar_hashes, dict
    ):
        raise ValueError("song delivery state has no sidecar authority maps")
    if (
        delivered_sidecars.get("active_record") != str(delivery_record)
        or delivered_sidecar_hashes.get("active_record") != active_record.get("sha256")
    ):
        raise ValueError("song delivery state active_record binding mismatch")
    cover_artifact = artifacts.get("cover")
    if cover_artifact is not None:
        if not isinstance(cover_artifact, dict) or (
            delivered_sidecars.get("cover") != cover_artifact.get("path")
            or delivered_sidecar_hashes.get("cover") != cover_artifact.get("sha256")
        ):
            raise ValueError("song delivery state cover binding mismatch")
    return manifest_resolved, manifest


def _updated_song_delivery_manifest(
    manifest: dict,
    *,
    delivery_record_path: Path,
    delivery_record: dict,
    source_record_path: Path,
    source_record: dict,
    cover: Path,
    generated_cover: Path,
    cover_sha256: str,
    binding_path: Path,
    binding_sha256: str,
) -> dict:
    updated = copy.deepcopy(manifest)
    artifacts = updated.get("artifacts")
    if not isinstance(artifacts, dict):
        raise ValueError("song delivery manifest artifacts is not an object")
    delivery_record_sha = "sha256:" + hashlib.sha256(_runner._json_file_bytes(delivery_record)).hexdigest()
    source_record_sha = "sha256:" + hashlib.sha256(_runner._json_file_bytes(source_record)).hexdigest()
    artifacts["active_record"] = {
        "path": str(delivery_record_path),
        "sha256": delivery_record_sha,
        "source_path": str(source_record_path),
        "source_sha256": source_record_sha,
    }
    artifacts["cover"] = {
        "path": str(cover),
        "sha256": cover_sha256,
        "source_path": str(generated_cover),
        "source_sha256": cover_sha256,
    }
    absent = updated.get("absent_artifacts")
    if isinstance(absent, dict):
        absent.pop("cover", None)
    updated["cover_repair_binding"] = {
        "path": str(binding_path),
        "sha256": binding_sha256,
    }
    return updated


def _cover_reason_codes_without_transient_failure(value: object) -> list[str]:
    prefixes = ("CPA_AI_COVER", "CPA_IMAGE_EDIT", "COVER_REFERENCE_EXTRACTION")
    return [
        str(code)
        for code in (value if isinstance(value, list) else [])
        if not str(code).startswith(prefixes)
        and str(code) != "REVIEWED_COVER_REPLACEMENT_REQUIRED"
    ]


def _updated_cover_document(
    document: dict,
    *,
    cover: Path,
    cover_sha256: str,
    generation: dict,
    binding_path: Path,
    binding_sha256: str,
) -> dict:
    updated = copy.deepcopy(document)
    generation_cover_text = generation.get("cover_text")
    if (
        not isinstance(generation_cover_text, str)
        or not generation_cover_text.strip()
    ):
        generation_cover_text = None
    artifacts = updated.setdefault("artifact_hashes", {})
    if not isinstance(artifacts, dict):
        raise ValueError("artifact_hashes is not an object")
    artifacts["cover_sha256"] = cover_sha256
    updated["cover_repair_binding"] = {
        "path": str(binding_path),
        "sha256": binding_sha256,
    }
    if updated.get("schema_version") == "shadow-publish-draft.v1":
        updated.update(
            {
                "cover_status": "AI_COVER_READY",
                "cover_path": str(cover),
                "cover_generation": generation,
                "reason_codes": _cover_reason_codes_without_transient_failure(
                    updated.get("reason_codes")
                ),
                "upload_enabled": False,
            }
        )
        if generation_cover_text is not None:
            updated["cover_text"] = generation_cover_text
    staging = updated.get("publish_staging")
    if isinstance(staging, dict):
        staging.update(
            {
                "cover_status": "AI_COVER_READY",
                "cover_path": str(cover),
                "cover_generation": generation,
                "reason_codes": _cover_reason_codes_without_transient_failure(
                    staging.get("reason_codes")
                ),
                "upload_enabled": False,
            }
        )
        if generation_cover_text is not None:
            staging["cover_text"] = generation_cover_text
    return updated


def _restore_transaction_files(originals: dict[Path, bytes | None]) -> bool:
    restored = True
    for path, content in reversed(list(originals.items())):
        try:
            if content is None:
                path.unlink(missing_ok=True)
            else:
                _runner._atomic_write_bytes_file(path, content)
        except OSError:
            # Preserve the original exception.  The state remains uncommitted,
            # and the next tick's binding validator will loudly reject any
            # partial filesystem state left by an actual disk failure.
            restored = False
    for path, content in originals.items():
        if content is None:
            restored = restored and not path.exists()
        else:
            try:
                restored = restored and path.read_bytes() == content
            except OSError:
                restored = False
    return restored


COVER_TRANSACTION_SCHEMA_VERSION = "lidousha-cover-transaction.v1"


def _set_cover_transaction_status(journal_path: Path, journal: dict, status: str) -> None:
    updated = copy.deepcopy(journal)
    updated["status"] = status
    updated["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _runner._atomic_write_json_file(journal_path, updated)
    journal.clear()
    journal.update(updated)


def _prepare_cover_transaction(
    *,
    generated_cover: Path,
    candidate_id: str,
    title: str,
    mp4: Path,
    target_payloads: list[tuple[Path, bytes]],
) -> tuple[Path, dict, dict[Path, bytes | None]]:
    transaction_root = generated_cover.parent / "transaction"
    originals_root = transaction_root / "originals"
    intended_root = transaction_root / "intended"
    journal_path = generated_cover.parent / "cover-transaction.json"
    if journal_path.exists():
        raise ValueError(f"immutable cover transaction already exists: {journal_path}")
    originals: dict[Path, bytes | None] = {}
    entries: list[dict] = []
    seen: set[Path] = set()
    for index, (target, intended) in enumerate(target_payloads):
        target = target.absolute()
        if target in seen:
            raise ValueError(f"duplicate cover transaction target: {target}")
        seen.add(target)
        original = target.read_bytes() if target.is_file() else None
        originals[target] = original
        intended_blob = intended_root / f"{index:03d}.bin"
        _runner._atomic_write_bytes_file(intended_blob, intended)
        original_blob = None
        original_sha256 = None
        if original is not None:
            original_blob = originals_root / f"{index:03d}.bin"
            _runner._atomic_write_bytes_file(original_blob, original)
            original_sha256 = "sha256:" + hashlib.sha256(original).hexdigest()
        entries.append(
            {
                "target": str(target),
                "intended_blob": str(intended_blob),
                "intended_sha256": "sha256:" + hashlib.sha256(intended).hexdigest(),
                "original_exists": original is not None,
                "original_blob": str(original_blob) if original_blob is not None else None,
                "original_sha256": original_sha256,
            }
        )
    journal = {
        "schema_version": COVER_TRANSACTION_SCHEMA_VERSION,
        "status": "PREPARED",
        "candidate_id": candidate_id,
        "title": title,
        "media_path": str(mp4.resolve(strict=True)),
        "media_sha256": "sha256:" + _runner._sha256_regular_file(mp4),
        "generation_dir": str(generated_cover.parent.resolve(strict=True)),
        "entries": entries,
        "upload_enabled": False,
        "prepared_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    _runner._atomic_write_json_file(journal_path, journal)
    return journal_path, journal, originals


def _roll_forward_prepared_cover_transactions(
    date: str, rec: dict, mp4: Path, cover: Path
) -> bool:
    """Idempotently complete a PREPARED filesystem transaction after SIGKILL
    or host loss.  Intended bytes are immutable, hash-checked blobs; target
    paths are restricted to this candidate's active/delivery/generation roots."""

    cid = str(rec.get("candidate_id") or "")
    title = str(rec.get("title") or "")
    root = _runner.BASE / "out" / date / cid / "cover_repair" / "generations"
    recovered = False
    for journal_path in sorted(root.glob("*/cover-transaction.json")):
        try:
            journal = _read_json_object(journal_path, label="cover transaction journal")
        except ValueError:
            continue
        if journal.get("status") != "PREPARED":
            continue
        entries = journal.get("entries")
        try:
            generation_dir = Path(str(journal.get("generation_dir") or "")).resolve(strict=True)
            media_path = Path(str(journal.get("media_path") or "")).resolve(strict=True)
        except OSError:
            continue
        if (
            journal.get("schema_version") != COVER_TRANSACTION_SCHEMA_VERSION
            or journal.get("candidate_id") != cid
            or journal.get("title") != title
            or journal.get("upload_enabled") is not False
            or generation_dir != journal_path.parent.resolve()
            or media_path != mp4.resolve()
            or not _matches_sha256(mp4, str(journal.get("media_sha256") or ""))
            or not isinstance(entries, list)
            or not entries
        ):
            continue
        validated: list[tuple[Path, bytes]] = []
        seen: set[Path] = set()
        binding_payload: dict | None = None
        valid = True
        for entry in entries:
            if not isinstance(entry, dict):
                valid = False
                break
            target = Path(str(entry.get("target") or "")).absolute()
            intended_blob = Path(str(entry.get("intended_blob") or ""))
            try:
                intended_allowed = intended_blob.resolve(strict=True).is_relative_to(
                    journal_path.parent.resolve(strict=True)
                )
                intended_matches = _matches_sha256(
                    intended_blob, str(entry.get("intended_sha256") or "")
                )
            except OSError:
                valid = False
                break
            if target in seen or target.is_symlink() or not intended_allowed or not intended_matches:
                valid = False
                break
            seen.add(target)
            try:
                payload = intended_blob.read_bytes()
            except OSError:
                valid = False
                break
            validated.append((target, payload))
            if target.name.endswith(".cover-binding.json"):
                try:
                    candidate_binding = json.loads(payload.decode("utf-8"))
                except (UnicodeError, ValueError):
                    valid = False
                    break
                if isinstance(candidate_binding, dict):
                    binding_payload = candidate_binding
        if not valid or binding_payload is None:
            continue
        try:
            generation_cover = Path(str(binding_payload.get("generation_cover_path") or "")).resolve(strict=True)
            generation, generation_path = _validate_repaired_cover_generation(
                cover=generation_cover,
                title=title,
                candidate_id=cid,
            )
        except (OSError, ValueError):
            continue
        if (
            binding_payload.get("schema_version") != "lidousha-cover-repair-binding.v1"
            or binding_payload.get("candidate_id") != cid
            or binding_payload.get("title") != title
            or binding_payload.get("upload_enabled") is not False
            or binding_payload.get("media_path") != str(mp4.resolve())
            or binding_payload.get("media_sha256") != journal.get("media_sha256")
            or binding_payload.get("cover_path") != str(cover.resolve())
            or binding_payload.get("cover_sha256")
            != binding_payload.get("generation_cover_sha256")
            or not _matches_sha256(
                generation_cover,
                str(binding_payload.get("generation_cover_sha256") or ""),
            )
            or binding_payload.get("generation_manifest_path") != str(generation_path.resolve())
            or not _matches_sha256(
                generation_path,
                str(binding_payload.get("generation_manifest_sha256") or ""),
            )
            or binding_payload.get("selected_model") != generation.get("model")
        ):
            continue
        binding_target = generation_cover.with_suffix(".cover-binding.json").absolute()
        try:
            active_documents = _active_cover_documents(
                date=date,
                candidate_id=cid,
                title=title,
                mp4=mp4,
                media_sha256=str(journal.get("media_sha256") or ""),
            )
        except (OSError, ValueError):
            continue
        expected_targets = {
            binding_target,
            *(path.absolute() for path, _document in active_documents),
        }
        if generation_cover.resolve() != cover.resolve():
            expected_targets.add(cover.absolute())
        if rec.get("delivered"):
            manifest_value = rec.get("delivery_manifest_path")
            if (
                binding_payload.get("authority_type") != "verified_song_delivery"
                or not isinstance(manifest_value, str)
                or binding_payload.get("delivery_manifest_path") != manifest_value
            ):
                continue
            expected_targets.add(Path(manifest_value).absolute())
        elif (
            binding_payload.get("authority_type") != "talk_delivery_record"
            or binding_payload.get("delivery_manifest_path") is not None
        ):
            continue
        if seen != expected_targets or binding_target not in seen:
            # Never accept a directory-wide capability.  The only writable
            # paths are the exact active record/publish files derived above,
            # the delivery artifacts, and this generation's binding.
            continue

        payloads = {target: payload for target, payload in validated}
        binding_sha256 = "sha256:" + hashlib.sha256(payloads[binding_target]).hexdigest()
        if generation_cover.resolve() != cover.resolve():
            if (
                "sha256:" + hashlib.sha256(payloads[cover.absolute()]).hexdigest()
                != binding_payload.get("cover_sha256")
            ):
                continue
        intended_documents: dict[Path, dict] = {}
        for document_path, current_document in active_documents:
            try:
                intended_document = json.loads(
                    payloads[document_path.absolute()].decode("utf-8")
                )
            except (KeyError, UnicodeError, ValueError):
                valid = False
                break
            if not isinstance(intended_document, dict):
                valid = False
                break
            hashes = intended_document.get("artifact_hashes")
            pointer = intended_document.get("cover_repair_binding")
            publish_view = (
                intended_document
                if intended_document.get("schema_version") == "shadow-publish-draft.v1"
                else intended_document.get("publish_staging")
            )
            if (
                not isinstance(hashes, dict)
                or hashes.get("cover_sha256") != binding_payload.get("cover_sha256")
                or not isinstance(pointer, dict)
                or pointer.get("path") != str(binding_target)
                or pointer.get("sha256") != binding_sha256
                or not isinstance(publish_view, dict)
                or publish_view.get("title") != title
                or publish_view.get("cover_status") != "AI_COVER_READY"
                or publish_view.get("cover_path") != str(cover)
                or publish_view.get("cover_generation") != generation
                or publish_view.get("upload_enabled") is not False
                or _document_video_hash(intended_document)
                != journal.get("media_sha256")
                or intended_document.get("schema_version")
                != current_document.get("schema_version")
                or intended_document.get("candidate_id")
                != current_document.get("candidate_id")
                or intended_document.get("delivery_candidate_id")
                != current_document.get("delivery_candidate_id")
                or intended_document.get("source_candidate_id")
                != current_document.get("source_candidate_id")
            ):
                valid = False
                break
            current_staging = current_document.get("publish_staging")
            intended_staging = intended_document.get("publish_staging")
            if isinstance(current_staging, dict) and (
                not isinstance(intended_staging, dict)
                or intended_staging.get("publish_json_path")
                != current_staging.get("publish_json_path")
            ):
                valid = False
                break
            intended_documents[document_path.resolve()] = intended_document
        if not valid:
            continue
        if rec.get("delivered"):
            manifest_target = Path(str(rec["delivery_manifest_path"])).absolute()
            try:
                intended_manifest = json.loads(payloads[manifest_target].decode("utf-8"))
            except (KeyError, UnicodeError, ValueError):
                continue
            if not isinstance(intended_manifest, dict):
                continue
            manifest_artifacts = (
                intended_manifest.get("artifacts")
                if isinstance(intended_manifest, dict)
                else None
            )
            video_artifact = (
                manifest_artifacts.get("video")
                if isinstance(manifest_artifacts, dict)
                else None
            )
            cover_artifact = (
                manifest_artifacts.get("cover")
                if isinstance(manifest_artifacts, dict)
                else None
            )
            active_record = (
                manifest_artifacts.get("active_record")
                if isinstance(manifest_artifacts, dict)
                else None
            )
            manifest_pointer = (
                intended_manifest.get("cover_repair_binding")
                if isinstance(intended_manifest, dict)
                else None
            )
            delivery_record_path = mp4.with_suffix(".record.json").resolve()
            source_record_paths = [
                path
                for path in intended_documents
                if path.name.endswith(".record.json") and path != delivery_record_path
            ]
            if len(source_record_paths) != 1:
                continue
            source_record_path = source_record_paths[0]
            if (
                intended_manifest.get("schema_version")
                != _runner.VERIFIED_SONG_DELIVERY_SCHEMA_VERSION
                or intended_manifest.get("status") != "DELIVERED_NO_UPLOAD"
                or intended_manifest.get("candidate_id") != cid
                or intended_manifest.get("upload_enabled") is not False
                or not isinstance(video_artifact, dict)
                or video_artifact.get("path") != str(mp4)
                or video_artifact.get("sha256") != journal.get("media_sha256")
                or not isinstance(cover_artifact, dict)
                or cover_artifact.get("path") != str(cover)
                or cover_artifact.get("sha256")
                != binding_payload.get("cover_sha256")
                or cover_artifact.get("source_path") != str(generation_cover)
                or cover_artifact.get("source_sha256")
                != binding_payload.get("generation_cover_sha256")
                or not isinstance(active_record, dict)
                or active_record.get("path") != str(delivery_record_path)
                or active_record.get("sha256")
                != "sha256:"
                + hashlib.sha256(payloads[delivery_record_path.absolute()]).hexdigest()
                or active_record.get("source_path") != str(source_record_path)
                or active_record.get("source_sha256")
                != "sha256:"
                + hashlib.sha256(payloads[source_record_path.absolute()]).hexdigest()
                or not isinstance(manifest_pointer, dict)
                or manifest_pointer.get("path") != str(binding_target)
                or manifest_pointer.get("sha256") != binding_sha256
            ):
                continue
        try:
            for target, payload in validated:
                _runner._atomic_write_bytes_file(target, payload)
            for entry in entries:
                if not _matches_sha256(
                    Path(str(entry["target"])), str(entry["intended_sha256"])
                ):
                    raise OSError("cover transaction roll-forward verification failed")
            _set_cover_transaction_status(journal_path, journal, "COMMITTED")
        except OSError:
            continue
        recovered = True
    return recovered


def _bind_repaired_cover(
    date: str,
    rec: dict,
    mp4: Path,
    cover: Path,
    generated_cover: Path | None = None,
) -> None:
    cid = str(rec.get("candidate_id") or "")
    title = str(rec.get("title") or "")
    generated_cover = generated_cover or cover
    generation, generation_path = _validate_repaired_cover_generation(
        cover=generated_cover, title=title, candidate_id=cid
    )
    cover_sha256 = "sha256:" + _runner._sha256_regular_file(generated_cover)
    media_sha256 = "sha256:" + _runner._sha256_regular_file(mp4)
    documents = _active_cover_documents(
        date=date,
        candidate_id=cid,
        title=title,
        mp4=mp4,
        media_sha256=media_sha256,
    )
    generation, generation_path = _enrich_repaired_cover_generation(
        generation=generation,
        generation_path=generation_path,
        documents=documents,
        title=title,
    )
    generation_sha256 = (
        "sha256:" + _runner._sha256_regular_file(generation_path)
    )
    song_manifest = _active_song_delivery_manifest(
        rec,
        candidate_id=cid,
        mp4=mp4,
        documents=documents,
    )
    binding_path = generated_cover.with_suffix(".cover-binding.json")
    if binding_path.exists():
        raise ValueError(f"immutable cover binding already exists: {binding_path}")
    binding = {
        "schema_version": "lidousha-cover-repair-binding.v1",
        "candidate_id": cid,
        "title": title,
        "media_path": str(mp4.resolve(strict=True)),
        "media_sha256": media_sha256,
        "cover_path": str(cover.resolve()),
        "cover_sha256": cover_sha256,
        "generation_cover_path": str(generated_cover.resolve(strict=True)),
        "generation_cover_sha256": cover_sha256,
        "generation_manifest_path": str(generation_path.resolve(strict=True)),
        "generation_manifest_sha256": generation_sha256,
        "selected_model": generation["model"],
        "attempted_models": generation.get("attempted_models") or [],
        "model_fallback_used": bool(generation.get("model_fallback_used")),
        "authority_type": "verified_song_delivery" if song_manifest is not None else "talk_delivery_record",
        "delivery_manifest_path": str(song_manifest[0]) if song_manifest is not None else None,
        "bound_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "upload_enabled": False,
    }
    binding_bytes = _runner._json_file_bytes(binding)
    binding_sha256 = "sha256:" + hashlib.sha256(binding_bytes).hexdigest()
    updated_documents = [
        (
            path,
            _updated_cover_document(
                document,
                cover=cover,
                cover_sha256=cover_sha256,
                generation=generation,
                binding_path=binding_path,
                binding_sha256=binding_sha256,
            ),
        )
        for path, document in documents
    ]
    updated_song_manifest: tuple[Path, dict] | None = None
    if song_manifest is not None:
        updated_by_path = {path.resolve(): (path, document) for path, document in updated_documents}
        delivery_record_path = mp4.with_suffix(".record.json").resolve(strict=True)
        source_record_paths = [
            path.resolve(strict=True)
            for path, _document in updated_documents
            if path.name.endswith(".record.json") and path.resolve() != delivery_record_path
        ]
        if len(source_record_paths) != 1:
            raise ValueError("song cover binding requires one updated active source record")
        source_record_path = source_record_paths[0]
        updated_song_manifest = (
            song_manifest[0],
            _updated_song_delivery_manifest(
                song_manifest[1],
                delivery_record_path=delivery_record_path,
                delivery_record=updated_by_path[delivery_record_path][1],
                source_record_path=source_record_path,
                source_record=updated_by_path[source_record_path][1],
                cover=cover,
                generated_cover=generated_cover,
                cover_sha256=cover_sha256,
                binding_path=binding_path,
                binding_sha256=binding_sha256,
            ),
        )
        # The verified song delivery manifest remains the commit marker and is
        # therefore installed after its active records and publish draft.
        updated_documents.append(updated_song_manifest)

    target_payloads: list[tuple[Path, bytes]] = []
    if generated_cover.resolve() != cover.resolve():
        target_payloads.append((cover, generated_cover.read_bytes()))
    target_payloads.append((binding_path, binding_bytes))
    target_payloads.extend(
        (document_path, _runner._json_file_bytes(document))
        for document_path, document in updated_documents
    )
    journal_path, journal, originals = _prepare_cover_transaction(
        generated_cover=generated_cover,
        candidate_id=cid,
        title=title,
        mp4=mp4,
        target_payloads=target_payloads,
    )
    try:
        for target, payload in target_payloads:
            _runner._atomic_write_bytes_file(target, payload)
        for target, payload in target_payloads:
            if target.read_bytes() != payload:
                raise OSError(f"cover transaction verification failed: {target}")
        _set_cover_transaction_status(journal_path, journal, "COMMITTED")
    except BaseException:
        restored = _restore_transaction_files(originals)
        if restored:
            try:
                _set_cover_transaction_status(journal_path, journal, "ROLLED_BACK")
            except OSError:
                # Keep PREPARED if the journal status cannot be updated.  The
                # next tick can safely roll the immutable intended bytes
                # forward instead of spending on another image request.
                pass
        raise

    repaired_record = copy.deepcopy(rec)
    repaired_record.update(
        {
            "cover_status": "REPAIRED_AI_COVER",
            "cover_path": str(cover),
            "cover_sha256": cover_sha256,
            "cover_generation": generation,
            "cover_generation_path": str(generation_path),
            "cover_generation_sha256": generation_sha256,
            "cover_binding_path": str(binding_path),
            "cover_binding_sha256": binding_sha256,
        }
    )
    if updated_song_manifest is not None:
        manifest_path, manifest_document = updated_song_manifest
        repaired_record["delivery_manifest_path"] = str(manifest_path)
        repaired_record["delivery_manifest_sha256"] = (
            "sha256:" + hashlib.sha256(_runner._json_file_bytes(manifest_document)).hexdigest()
        )
        delivered_sidecars = repaired_record.setdefault("delivered_sidecars", {})
        delivered_sidecar_hashes = repaired_record.setdefault("delivered_sidecar_hashes", {})
        manifest_artifacts = manifest_document.get("artifacts")
        active_record = (
            manifest_artifacts.get("active_record")
            if isinstance(manifest_artifacts, dict)
            else None
        )
        if not isinstance(active_record, dict):
            raise ValueError("updated song manifest lost active_record authority")
        if isinstance(delivered_sidecars, dict):
            delivered_sidecars["cover"] = str(cover)
            delivered_sidecars["active_record"] = str(active_record["path"])
        if isinstance(delivered_sidecar_hashes, dict):
            delivered_sidecar_hashes["cover"] = cover_sha256
            delivered_sidecar_hashes["active_record"] = str(active_record["sha256"])
    repaired_record["cover_transaction_path"] = str(journal_path)
    repaired_record["cover_transaction_status"] = "COMMITTED"
    if isinstance(repaired_record.get("summary"), dict):
        repaired_record["summary"].update(
            {
                "cover_status": "REPAIRED_AI_COVER",
                "cover_path": str(cover),
                "cover_sha256": cover_sha256,
                "cover_binding_path": str(binding_path),
                "cover_binding_sha256": binding_sha256,
            }
        )
    rec.clear()
    rec.update(repaired_record)


def _cover_binding_valid(date: str, rec: dict, mp4: Path, cover: Path) -> bool:
    binding_path_value = rec.get("cover_binding_path")
    binding_sha256 = rec.get("cover_binding_sha256")
    if not isinstance(binding_path_value, str) or not isinstance(binding_sha256, str):
        return False
    binding_path = Path(binding_path_value)
    expected_root = (_runner.BASE / "out" / date / str(rec.get("candidate_id") or "") / "cover_repair" / "generations").resolve()
    try:
        binding_resolved = binding_path.resolve(strict=True)
        state_cover_resolved = Path(str(rec.get("cover_path") or "")).resolve(
            strict=True
        )
    except OSError:
        return False
    if (
        state_cover_resolved != cover.resolve()
        or not binding_resolved.is_relative_to(expected_root)
        or not _matches_sha256(binding_resolved, binding_sha256)
    ):
        return False
    try:
        binding = _read_json_object(binding_resolved, label="cover repair binding")
        media_path = Path(str(binding.get("media_path") or "")).resolve(strict=True)
        cover_path = Path(str(binding.get("cover_path") or "")).resolve(strict=True)
        generation_path = Path(str(binding.get("generation_manifest_path") or "")).resolve(strict=True)
        generation_cover = Path(str(binding.get("generation_cover_path") or "")).resolve(strict=True)
    except (OSError, ValueError):
        return False
    if (
        binding.get("schema_version") != "lidousha-cover-repair-binding.v1"
        or binding.get("candidate_id") != rec.get("candidate_id")
        or binding.get("title") != rec.get("title")
        or binding.get("upload_enabled") is not False
        or media_path != mp4.resolve()
        or cover_path != cover.resolve()
        or not _matches_sha256(mp4, str(binding.get("media_sha256") or ""))
        or not _matches_sha256(cover, str(binding.get("cover_sha256") or ""))
        or not _matches_sha256(generation_cover, str(binding.get("generation_cover_sha256") or ""))
        or not _matches_sha256(generation_path, str(binding.get("generation_manifest_sha256") or ""))
        or rec.get("cover_sha256") != binding.get("cover_sha256")
        or rec.get("cover_generation_path") != str(generation_path)
        or rec.get("cover_generation_sha256") != binding.get("generation_manifest_sha256")
    ):
        return False
    try:
        generation, validated_path = _validate_repaired_cover_generation(
            cover=generation_cover,
            title=str(rec.get("title") or ""),
            candidate_id=str(rec.get("candidate_id") or ""),
        )
    except (OSError, ValueError):
        return False
    if not (
        validated_path.resolve() == generation_path
        and binding.get("selected_model") == generation.get("model")
        and str(binding.get("cover_sha256")) == str(binding.get("generation_cover_sha256"))
        and rec.get("cover_generation") == generation
    ):
        return False
    summary = rec.get("summary")
    if isinstance(summary, dict) and summary and (
        summary.get("cover_status") != "REPAIRED_AI_COVER"
        or summary.get("cover_path") != str(cover)
        or summary.get("cover_sha256") != binding.get("cover_sha256")
        or summary.get("cover_binding_path") != str(binding_path)
        or summary.get("cover_binding_sha256") != binding_sha256
    ):
        return False
    try:
        documents = _active_cover_documents(
            date=date,
            candidate_id=str(rec.get("candidate_id") or ""),
            title=str(rec.get("title") or ""),
            mp4=mp4,
            media_sha256=str(binding.get("media_sha256") or ""),
        )
    except (OSError, ValueError):
        return False
    for _path, document in documents:
        hashes = document.get("artifact_hashes")
        pointer = document.get("cover_repair_binding")
        if (
            not isinstance(hashes, dict)
            or hashes.get("cover_sha256") != binding.get("cover_sha256")
            or not isinstance(pointer, dict)
            or pointer.get("path") != str(binding_path)
            or pointer.get("sha256") != binding_sha256
        ):
            return False
        publish_view = (
            document
            if document.get("schema_version") == "shadow-publish-draft.v1"
            else document.get("publish_staging")
        )
        if not isinstance(publish_view, dict):
            return False
        try:
            published_cover = Path(str(publish_view.get("cover_path") or "")).resolve(strict=True)
        except OSError:
            return False
        if (
            publish_view.get("cover_status") != "AI_COVER_READY"
            or publish_view.get("upload_enabled") is not False
            or published_cover != cover.resolve()
            or publish_view.get("cover_generation") != generation
            or (
                isinstance(generation.get("cover_text"), str)
                and generation.get("cover_text")
                and publish_view.get("cover_text")
                != generation.get("cover_text")
            )
        ):
            return False
    try:
        song_manifest = _active_song_delivery_manifest(
            rec,
            candidate_id=str(rec.get("candidate_id") or ""),
            mp4=mp4,
            documents=documents,
        )
    except (OSError, ValueError):
        return False
    if song_manifest is None:
        return binding.get("authority_type") == "talk_delivery_record" and binding.get("delivery_manifest_path") is None
    manifest_path, manifest = song_manifest
    artifacts = manifest.get("artifacts")
    cover_artifact = artifacts.get("cover") if isinstance(artifacts, dict) else None
    pointer = manifest.get("cover_repair_binding")
    if not isinstance(cover_artifact, dict) or not isinstance(pointer, dict):
        return False
    try:
        manifest_cover = Path(str(cover_artifact.get("path") or "")).resolve(strict=True)
        manifest_source = Path(str(cover_artifact.get("source_path") or "")).resolve(strict=True)
    except OSError:
        return False
    return (
        binding.get("authority_type") == "verified_song_delivery"
        and binding.get("delivery_manifest_path") == str(manifest_path)
        and manifest_cover == cover.resolve()
        and manifest_source == generation_cover.resolve()
        and _matches_sha256(cover, str(cover_artifact.get("sha256") or ""))
        and _matches_sha256(generation_cover, str(cover_artifact.get("source_sha256") or ""))
        and pointer.get("path") == str(binding_path)
        and pointer.get("sha256") == binding_sha256
    )


def _recover_committed_cover_binding(date: str, rec: dict, mp4: Path, cover: Path) -> bool:
    """Recover state after a crash between the filesystem binding commit and
    ``write_state``.  Immutable generations plus active-doc/manifest pointers
    are sufficient to reconstruct state without another paid image request.
    Partial bindings are ignored because the ordinary full validator must pass
    before any state field is changed."""

    if not cover.is_file():
        return False
    if str(rec.get("cover_status") or "").upper() == "REPAIRED_AI_COVER" and _cover_binding_valid(
        date, rec, mp4, cover
    ):
        return False
    cid = str(rec.get("candidate_id") or "")
    title = str(rec.get("title") or "")
    root = _runner.BASE / "out" / date / cid / "cover_repair" / "generations"
    try:
        candidates = sorted(
            root.glob("*/*.cover-binding.json"),
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        )
    except OSError:
        return False
    for binding_path in candidates:
        try:
            binding = _read_json_object(binding_path, label="recoverable cover binding")
            generation_path = Path(str(binding.get("generation_manifest_path") or "")).resolve(strict=True)
            generation_cover = Path(str(binding.get("generation_cover_path") or "")).resolve(strict=True)
            generation, validated_path = _validate_repaired_cover_generation(
                cover=generation_cover,
                title=title,
                candidate_id=cid,
            )
            if validated_path.resolve() != generation_path:
                continue
            tentative = copy.deepcopy(rec)
            tentative.update(
                {
                    "cover_status": "REPAIRED_AI_COVER",
                    "cover_path": str(cover),
                    "cover_sha256": str(binding.get("cover_sha256") or ""),
                    "cover_generation": generation,
                    "cover_generation_path": str(generation_path),
                    "cover_generation_sha256": str(
                        binding.get("generation_manifest_sha256") or ""
                    ),
                    "cover_binding_path": str(binding_path),
                    "cover_binding_sha256": "sha256:" + _runner._sha256_regular_file(binding_path),
                    "cover_integrity_status": "VALID_BOUND_RECOVERED",
                }
            )
            if tentative.get("status") == _runner.TALK_COVER_PENDING_STATUS:
                tentative.update(
                    {
                        "status": "review_ready",
                        "bundle_lifecycle": "CURRENT",
                        "bundle_compliance": "COMPLIANT",
                    }
                )
            if binding.get("authority_type") == "verified_song_delivery":
                manifest_path = Path(str(binding.get("delivery_manifest_path") or "")).resolve(strict=True)
                tentative["delivery_manifest_path"] = str(manifest_path)
                tentative["delivery_manifest_sha256"] = (
                    "sha256:" + _runner._sha256_regular_file(manifest_path)
                )
                manifest = _read_json_object(
                    manifest_path, label="recoverable song delivery manifest"
                )
                artifacts = manifest.get("artifacts")
                active_record = (
                    artifacts.get("active_record") if isinstance(artifacts, dict) else None
                )
                if not isinstance(active_record, dict):
                    continue
                sidecars = tentative.setdefault("delivered_sidecars", {})
                sidecar_hashes = tentative.setdefault("delivered_sidecar_hashes", {})
                if isinstance(sidecars, dict):
                    sidecars["cover"] = str(cover)
                    sidecars["active_record"] = str(active_record.get("path") or "")
                if isinstance(sidecar_hashes, dict):
                    sidecar_hashes["cover"] = tentative["cover_sha256"]
                    sidecar_hashes["active_record"] = str(
                        active_record.get("sha256") or ""
                    )
            if not _cover_binding_valid(date, tentative, mp4, cover):
                continue
        except (OSError, ValueError, _runner.SongDeliveryError):
            continue
        tentative["cover_repair_recovered_at"] = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
        )
        if isinstance(tentative.get("summary"), dict):
            tentative["summary"].update(
                {
                    "cover_status": "REPAIRED_AI_COVER",
                    "cover_path": str(cover),
                    "cover_sha256": tentative["cover_sha256"],
                    "cover_binding_path": tentative["cover_binding_path"],
                    "cover_binding_sha256": tentative["cover_binding_sha256"],
                }
            )
        rec.clear()
        rec.update(tentative)
        return True
    return False


def _initial_cover_proof_valid(date: str, rec: dict, mp4: Path, cover: Path) -> bool:
    expected_cover = rec.get("cover_sha256")
    expected_video = rec.get("video_sha256") or rec.get("delivered_sha256")
    generation = rec.get("cover_generation")
    if (
        not isinstance(expected_cover, str)
        or not isinstance(expected_video, str)
        or not isinstance(generation, dict)
        or not _matches_sha256(cover, expected_cover)
        or not _matches_sha256(mp4, expected_video)
        or generation.get("title") != rec.get("title")
        or generation.get("fallback_used") is not False
        or generation.get("final_cover_sha256") != expected_cover
    ):
        return False
    try:
        source_cover = Path(str(generation.get("final_cover") or "")).resolve(strict=True)
    except OSError:
        return False
    if not _matches_sha256(source_cover, expected_cover):
        return False
    method = str(generation.get("method") or "")
    route_decision = generation.get("route_decision")
    if isinstance(route_decision, dict) and not validate_cover_route_decision(
        generation, allow_legacy_v1=True
    ):
        return False
    treatment = (
        str(route_decision.get("selected_treatment") or "")
        if isinstance(route_decision, dict)
        else ""
    )
    actual_treatment = (
        str(route_decision.get("actual_treatment") or treatment)
        if isinstance(route_decision, dict)
        else treatment
    )
    if actual_treatment in {"screenshot_direct", "screenshot_polish"}:
        degraded_polish = (
            treatment == "screenshot_polish"
            and method == "screenshot_direct"
            and isinstance(generation.get("screenshot_polish"), dict)
            and generation["screenshot_polish"].get("status")
            == "DEGRADED_TO_DIRECT"
        )
        valid_method = method == actual_treatment or degraded_polish
        rendered_lines = generation.get("rendered_lines")
        screenshot_frame = generation.get("screenshot_frame")
        reference_selection = generation.get("reference_selection")
        try:
            reference = Path(
                str(generation.get("reference_image") or "")
            ).resolve(strict=True)
        except OSError:
            return False
        if not (
            valid_method
            and validate_cover_route_decision(
                generation, allow_legacy_v1=True
            )
            and bool(str(route_decision.get("reason") or "").strip())
            and isinstance(rendered_lines, list)
            and bool("".join(str(value) for value in rendered_lines).strip())
            and isinstance(screenshot_frame, dict)
            and isinstance(screenshot_frame.get("frame_ms"), int)
            and not isinstance(screenshot_frame.get("frame_ms"), bool)
            and screenshot_frame.get("frame_ms") >= 0
            and isinstance(reference_selection, dict)
            and isinstance(reference_selection.get("best_ms"), int)
            and not isinstance(reference_selection.get("best_ms"), bool)
            and reference_selection.get("best_ms") >= 0
            and screenshot_frame.get("frame_ms")
            == reference_selection.get("best_ms")
            and _matches_sha256(
                reference, str(generation.get("reference_sha256") or "")
            )
        ):
            return False
    elif method != "images.edit":
        return False
    try:
        documents = _active_cover_documents(
            date=date,
            candidate_id=str(rec.get("candidate_id") or ""),
            title=str(rec.get("title") or ""),
            mp4=mp4,
            media_sha256=str(expected_video),
        )
    except (OSError, ValueError):
        return False
    for _path, document in documents:
        hashes = document.get("artifact_hashes")
        publish_view = (
            document
            if document.get("schema_version") == "shadow-publish-draft.v1"
            else document.get("publish_staging")
        )
        if not isinstance(hashes, dict) or not isinstance(publish_view, dict):
            return False
        try:
            published_source_cover = Path(str(publish_view.get("cover_path") or "")).resolve(strict=True)
        except OSError:
            return False
        if (
            hashes.get("cover_sha256") != expected_cover
            or publish_view.get("cover_status") != "AI_COVER_READY"
            or publish_view.get("upload_enabled") is not False
            or published_source_cover != source_cover
            or publish_view.get("cover_generation") != generation
        ):
            return False
    try:
        song_manifest = _active_song_delivery_manifest(
            rec,
            candidate_id=str(rec.get("candidate_id") or ""),
            mp4=mp4,
            documents=documents,
        )
    except (OSError, ValueError):
        return False
    if song_manifest is not None:
        artifacts = song_manifest[1].get("artifacts")
        cover_artifact = artifacts.get("cover") if isinstance(artifacts, dict) else None
        if not isinstance(cover_artifact, dict):
            return False
        try:
            manifest_cover = Path(str(cover_artifact.get("path") or "")).resolve(strict=True)
            manifest_source = Path(str(cover_artifact.get("source_path") or "")).resolve(strict=True)
        except OSError:
            return False
        if (
            manifest_cover != cover.resolve()
            or manifest_source != source_cover
            or not _matches_sha256(cover, str(cover_artifact.get("sha256") or ""))
            or not _matches_sha256(source_cover, str(cover_artifact.get("source_sha256") or ""))
        ):
            return False
    return True


def _refresh_cover_repair_budget(rec: dict, fingerprint: str) -> bool:
    if rec.get("cover_repair_generation") == fingerprint:
        return False
    old_attempts = max(0, int(rec.get("cover_repair_attempts") or 0))
    lifetime = rec.get("cover_repair_lifetime_attempts")
    if lifetime is None:
        lifetime = old_attempts
    history = rec.setdefault("cover_repair_attempt_history", [])
    if not isinstance(history, list):
        history = []
        rec["cover_repair_attempt_history"] = history
    if old_attempts or rec.get("cover_repair_generation"):
        history.append(
            {
                "pipeline_fingerprint": rec.get("cover_repair_generation") or "legacy-unversioned",
                "attempts": old_attempts,
            }
        )
    rec["cover_repair_generation"] = fingerprint
    rec["cover_repair_attempts"] = 0
    rec["cover_repair_lifetime_attempts"] = max(0, int(lifetime))
    return True


def cover_repair_needed(date: str, rec: dict) -> bool:
    # Delivered = passed the delivery gate (for songs: is-song + complete, see
    # song_delivery_ok) — every delivered clip deserves a cover, regardless of
    # what the ADVISORY semantic judge said (Ivan 2026-07-10).  Blocked/failed
    # records have no delivery and never get covers.
    delivered = (
        rec.get("status") in _runner.DELIVERED_TALK_STATUSES
        or rec.get("status") == _runner.TALK_COVER_PENDING_STATUS
        or bool(rec.get("delivered"))
    )
    if not delivered or not rec.get("title"):
        return False
    paths = _runner.delivered_paths(date, rec)
    if paths is None:
        return False
    mp4, cover = paths
    if not cover.is_file():
        return True
    status = str(rec.get("cover_status") or "").upper()
    if "BLOCKED_AI_COVER_REQUIRED" in status or "REPAIR_FAILED" in status:
        return True
    if status == "REPAIRED_AI_COVER":
        return not _cover_binding_valid(date, rec, mp4, cover)
    if status == "AI_COVER_READY":
        return not _initial_cover_proof_valid(date, rec, mp4, cover)
    # A bare PNG or an unknown status has no current title/video/hash authority.
    return True


def _cover_repair_eligible(rec: dict) -> bool:
    """Budget limits paid generation attempts, never integrity detection.
    ``cover_repair_needed`` must stay fail-closed even after exhaustion so a
    later title/media/document drift remains loud in state and reports."""

    return (
        int(rec.get("cover_repair_attempts") or 0) < _runner.COVER_REPAIR_MAX_ATTEMPTS
        and int(rec.get("cover_repair_lifetime_attempts") or 0)
        < _runner.COVER_REPAIR_LIFETIME_ATTEMPT_CAP
    )


def _cover_authority_preflight(date: str, rec: dict, mp4: Path) -> None:
    """Verify every mutable authority document before a paid image request.

    Binding used to discover missing legacy song records only after generation,
    which spent an image request on a package that could never commit.  Future
    song deliveries carry a verified manifest plus active-record state maps;
    older packages fail loudly and cost-free until an explicit migration is
    performed from their original proof chain.
    """

    candidate_id = str(rec.get("candidate_id") or "")
    documents = _active_cover_documents(
        date=date,
        candidate_id=candidate_id,
        title=str(rec.get("title") or ""),
        mp4=mp4,
        media_sha256="sha256:" + _runner._sha256_regular_file(mp4),
    )
    for path, document in documents:
        publish_view = (
            document
            if document.get("schema_version") == "shadow-publish-draft.v1"
            else document.get("publish_staging")
        )
        if not isinstance(publish_view, dict):
            raise ValueError(
                f"COVER_TITLE_AUTHORITY_UNRESOLVED: {path} has no publish view"
            )
        authority_status = str(
            publish_view.get("title_authority_status") or ""
        )
        authority_error = publish_view.get("title_authority_error")
        violations = publish_view.get("title_policy_violations")
        story_audit = publish_view.get("title_story_audit")
        if (
            authority_error not in (None, "")
            or authority_status.startswith("BLOCKED")
            or bool(violations)
            or (
                isinstance(story_audit, dict)
                and story_audit.get("status") != "PASS"
            )
        ):
            raise ValueError(
                "COVER_TITLE_AUTHORITY_UNRESOLVED: cover-only repair cannot "
                f"convert a blocked title into a review-ready package: {path}"
            )
    story_contract = _active_story_contract(documents)
    if (
        story_contract is not None
        and relationship_visual_safety_required(story_contract)
    ):
        raise ValueError(
            "COVER_RELATIONSHIP_REFERENCE_UNRESOLVED: generic cover repair "
            "cannot prove every participant in a relationship story"
        )
    manifest = _active_song_delivery_manifest(
        rec,
        candidate_id=candidate_id,
        mp4=mp4,
        documents=documents,
    )
    # `delivered` is the runner's stable song-lane signal.  Candidate ids can
    # be song_*, semanticsong_*, or a future anchor family, whereas delivered
    # talk records intentionally do not set this field.
    if rec.get("delivered") and manifest is None:
        raise ValueError("legacy delivered song has no verified active-record manifest")
