"""Build and validate normal full-media successor references.

The legacy lane binds a prepared handle to same-BV publication authority.  The
fresh lane binds an unpublished private package to current prepared bytes;
private cover reuse separately replays its immutable source generation.
"""

from __future__ import annotations

from src.autoslice.evidence_identity import file_sha256 as _evidence_file_sha256
import hashlib
import json
import re
import stat
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

from src.autoslice.authorized_upload_title_repair import validate_title_repair_successor
from src.autoslice.builtin_imagegen_cover import (
    _read_source,
    validate_builtin_imagegen_provenance,
)
from src.autoslice.cover_route_evidence import validate_cover_route_decision
from src.autoslice.new_bv_repair_authority import (
    NewBvRepairAuthorityError,
    replay_first_publication_manifest,
    validate_new_bv_repair_authority,
)
from src.autoslice.producer_delivery_transaction import (
    ProducerDeliveryTransactionError,
    _read_document,
    _read_regular_bytes,
    _safe_directory,
    deployment_authority_binding,
    load_prepared_delivery,
)
from src.autoslice.prepared_review_recovery import (
    historical_prepared_review,
)
from src.autoslice.qixi_transaction_core import exclusive_runner_commit
from src.autoslice.story_contract import (
    cover_story_contract_binding,
    cover_story_contract_binding_matches,
)


SCHEMA_VERSION = "normal-first-publication-media-successor-reference.v1"
FRESH_SCHEMA_VERSION = "normal-unpublished-prepared-delivery-reference.v1"
PRIVATE_REUSE_SCHEMA_VERSION = "normal-private-cover-reuse.v1"
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")


def _require_prepared_deployment(document, runtime_root, *, frozen_private_provenance=False):
    current=deployment_authority_binding(runtime_root)
    expected=document.get('deployed_authority')
    if expected==current:
        return
    if not frozen_private_provenance:
        _fail('prepared deployment authority drifts from current runtime')
    # Package audit reads the original handle as byte provenance, not as a
    # current commit capability. The complete original deployed tree must
    # still verify; all staged roles and current package gates run separately.
    if not isinstance(expected,Mapping) or re.fullmatch(r'[0-9a-f]{40}',str(expected.get('commit') or '')) is None:
        _fail('prepared original deployment identity is invalid')
    from src.autoslice.overflow_install import verify_deployed
    for raw in (runtime_root/'repo',runtime_root/'versions'/expected['commit']/'repo'):
        try:
            repo=_safe_directory(raw)
            commit=_read_regular_bytes(repo/'DEPLOYED_COMMIT')
            authority=_read_regular_bytes(repo/'DEPLOYED_AUTHORITY_MANIFEST.json')
            binding={'commit':commit.decode().strip(),
                'commit_bytes_sha256':'sha256:'+hashlib.sha256(commit).hexdigest(),
                'authority_manifest_sha256':'sha256:'+hashlib.sha256(authority).hexdigest()}
            if binding!=expected:
                continue
            verify_deployed(repo)
        except (OSError,ValueError,TypeError,KeyError,ProducerDeliveryTransactionError):
            continue
        return
    _fail('prepared original deployment snapshot is missing or changed')

# These roles are the actual private Talk producer roles.  Keeping the mapping
# here makes the consumer check every staged evidence sidecar present in the
# prepared handle rather than only the MP4 and SRT.
ROLE_SUFFIXES: dict[str, str] = {
    "video": ".mp4",
    "source_provenance": ".provenance.json",
    "subtitle": ".srt",
    "uniform_host_ass": ".final-sapphire72.ass",
    "speaker_srt": ".speaker.srt",
    "speaker_ass": ".speaker.ass",
    "speaker_manifest": ".speaker.json",
    "chat_authority": ".chat-authority.json",
    "clip_context": ".clip-context.json",
    "redelivery_baseline": ".redelivery-baseline.json",
    "subtitle_regression": ".subtitle-regression.json",
    "filler_audit": ".filler-audit.json",
    "text_finalization": ".text-finalization.json",
    "cover_title_mask": ".cover.title-mask.png",
    "cover_pre_overlay": ".cover.pre-overlay.png",
    "cover_route_background": ".cover.route-background.png",
    "cover_request": ".cover-request.json",
    "cover_response": ".cover-response.json",
    "publish": ".publish.json",
    "record": ".record.json",
    "cover": ".cover.png",
    "subtitle_audio_witness": ".subtitle-audio-witness.srt",
    "subtitle_audio_provenance": ".subtitle-audio-provenance.json",
    "subtitle_audio_correspondence": ".subtitle-audio-correspondence.json",
    "subtitle_audio_raw_result": ".subtitle-audio-bcut.raw.json",
}

REQUIRED_ROLES = frozenset(
    {"video", "subtitle", "record", "publish", "cover", "chat_authority", "clip_context"}
)


class NormalFirstPublicationMediaSuccessorError(ValueError):
    """A prepared full-media successor is not bound to the current package."""


def _fail(reason: str) -> None:
    raise NormalFirstPublicationMediaSuccessorError(reason)


def _sha256(path: Path) -> str:
    digest = _evidence_file_sha256(path)
    return digest


def _regular(path: Path, *, label: str) -> Path:
    try:
        metadata = path.lstat()
    except OSError:
        _fail(f"{label} is unavailable")
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        _fail(f"{label} is not a regular file")
    return path


def _package_file(root: Path, name: str, *, label: str) -> Path:
    path = (root / name).absolute()
    try:
        path.relative_to(root)
    except ValueError:
        _fail(f"{label} escapes package root")
    return _regular(path, label=label)


def _normal_sha(value: object, *, label: str) -> str:
    raw = str(value or "")
    if raw.startswith("sha256:"):
        raw = raw[7:]
    if re.fullmatch(r"[0-9a-f]{64}", raw) is None:
        _fail(f"{label} hash is invalid")
    return raw


def _canonical_sha(value: object) -> str:
    return "sha256:" + hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


def _canonical_file(value: object, *, label: str, base: Path | None = None) -> Path:
    if isinstance(value, Path):
        raw = value
    elif isinstance(value, str) and value:
        raw = Path(value)
    else:
        _fail(f"{label} is missing")
    if not raw.is_absolute():
        if base is None:
            _fail(f"{label} must be absolute")
        raw = base / raw
    try:
        if raw.is_symlink():
            _fail(f"{label} is a symlink")
        resolved = raw.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise NormalFirstPublicationMediaSuccessorError(
            f"{label} is unavailable"
        ) from exc
    if str(resolved) != str(raw.absolute()):
        _fail(f"{label} is not canonical")
    _regular(resolved, label=label)
    return resolved


def _load_json_file(path: Path, *, label: str) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise NormalFirstPublicationMediaSuccessorError(f"{label} is invalid") from exc
    if not isinstance(value, dict):
        _fail(f"{label} is invalid")
    return value


_SAME_BV_RECEIPT_NAMES = frozenset(
    {
        "qixi-corrected-package-finalization.json",
        "qixi-cover-successor-finalization.json",
    }
)
_SAME_BV_RECEIPT_MARKERS = (
    "published-recovery-package-receipt.json",
    "same-bv-repair-completed",
    "same-bv-cover-repair-completed",
)


def _has_same_bv_receipt(package_root: Path, candidate_id: str, *documents: Mapping[str, object]) -> bool:
    for document in documents:
        for key in (
            "published_recovery_package_receipt",
            "published_recovery_preflight",
            "qixi_cover_successor_finalization",
            "manual_corrected_same_bv",
            "manual_corrected_same_bv_receipt",
            "same_bv_repair_completed",
            "same_bv_cover_repair_completed",
        ):
            if document.get(key) is not None:
                return True
    try:
        for path in package_root.iterdir():
            if not path.is_file() and not path.is_symlink():
                continue
            name = path.name
            if name in _SAME_BV_RECEIPT_NAMES or any(
                marker in name for marker in _SAME_BV_RECEIPT_MARKERS
            ):
                return True
            if name == f"{candidate_id}.published-recovery-preflight.json":
                return True
    except OSError as exc:
        raise NormalFirstPublicationMediaSuccessorError(
            "same-BV receipt inventory is unavailable"
        ) from exc
    return False


def _require_private_surfaces(
    *,
    record: Mapping[str, object],
    staging: Mapping[str, object],
    publish: Mapping[str, object],
    package_root: Path,
    candidate_id: str,
    title: str,
    label: str,
    allow_absent_staging_candidate: bool = False,
    require_record_candidate: bool = False,
    require_record_title: bool = False,
) -> None:
    documents = (record, staging, publish)
    for document_name, document in zip(("record", "publish_staging", "publish"), documents):
        # Normal producer records intentionally omit this legacy top-level
        # field.  The staging and publish surfaces are the authoritative
        # private publication policy and must explicitly say false.
        upload_enabled = document.get("upload_enabled")
        if document_name == "record":
            if upload_enabled not in (None, False):
                _fail(f"{label} {document_name} upload policy is invalid")
        elif upload_enabled is not False:
            _fail(f"{label} {document_name} upload policy is invalid")
        if document.get("recovery_publication_authority") is not None:
            _fail(f"{label} {document_name} has publication authority")
        if document.get("verified_title_repair") is not None:
            _fail(f"{label} {document_name} has verified title repair")
        if document_name == "record" and require_record_candidate:
            if document.get("candidate_id") != candidate_id:
                _fail(f"{label} {document_name} candidate differs")
        elif document.get("candidate_id") not in (None, candidate_id):
            _fail(f"{label} {document_name} candidate differs")
        if document_name == "record" and require_record_title:
            if document.get("title") != title:
                _fail(f"{label} {document_name} title differs")
        elif document.get("title") not in (None, title):
            _fail(f"{label} {document_name} title differs")
    if allow_absent_staging_candidate:
        if staging.get("candidate_id") not in (None, candidate_id):
            _fail(f"{label} staging candidate differs")
    elif staging.get("candidate_id") != candidate_id:
        _fail(f"{label} staging candidate differs")
    if staging.get("title") != title or publish.get("title") != title:
        _fail(f"{label} title surfaces differ")
    if publish.get("candidate_id") != candidate_id:
        _fail(f"{label} publish candidate differs")
    if _has_same_bv_receipt(package_root, candidate_id, *documents):
        _fail(f"{label} contains a published or same-BV receipt")


def _source_publish_path(source_record_path: Path, record: Mapping[str, object]) -> Path:
    name = source_record_path.name
    if not name.endswith(".record.json"):
        _fail("private cover reuse source record name is invalid")
    sibling = source_record_path.with_name(name[: -len(".record.json")] + ".publish.json")
    # The canonical sibling is the source's immutable publish surface.  If it
    # exists but is malformed, fail closed instead of falling through to a
    # possibly stale path recorded during an earlier construction step.
    if sibling.exists() or sibling.is_symlink():
        return _canonical_file(sibling, label="private cover reuse source publish")
    staging = record.get("publish_staging")
    declared = staging.get("publish_json_path") if isinstance(staging, Mapping) else None
    if declared:
        return _canonical_file(
            declared,
            label="private cover reuse source publish",
            base=source_record_path.parent,
        )
    _fail("private cover reuse source publish is missing")


def _resolve_private_source_generation(
    generation: Mapping[str, object], *, source_root: Path
) -> dict[str, object]:
    """Resolve portable builtin-image_gen locators for source replay only.

    Private source records can retain the producer's relative provenance and
    cover names (for example ``builtin_imagegen_source_v02/...`` and
    ``CID.cover.png``).  The canonical builtin validator intentionally treats
    those locators as process-relative, so replay must first bind an isolated
    copy to the immutable source package.  The raw generation remains the
    stored source binding; this returned mapping is only the physical-path
    view used by route/provenance validation and by the new wrapper.
    """

    resolved = deepcopy(dict(generation))
    if generation.get("method") != "image_gen.imagegen":
        return resolved

    try:
        source_root = source_root.resolve(strict=True)
        provenance = _canonical_file(
            generation.get("builtin_imagegen_provenance_path"),
            label="private cover reuse source builtin provenance",
            base=source_root,
        )
        try:
            provenance.relative_to(source_root)
        except ValueError:
            _fail("private cover reuse source builtin provenance escapes source root")
        source, _assets = _read_source(provenance)
        physical_assets: dict[str, Path] = {}
        for source_key, generation_key in (
            ("identity_reference", "reference_image"),
            ("background", "ai_background"),
            ("final_cover", "final_cover"),
        ):
            if not isinstance(source.get(source_key), Mapping):
                _fail("private cover reuse source builtin provenance asset is invalid")
            # The source generation's locators are part of the immutable
            # record.  Resolve those locators against the source package, then
            # let the builtin validator compare their bytes with the manifest
            # asset hashes.  Do not silently replace a stale declaration with
            # the manifest's sibling path.
            physical_assets[generation_key] = _canonical_file(
                generation.get(generation_key),
                label=f"private cover reuse source {generation_key}",
                base=source_root,
            )
            try:
                physical_assets[generation_key].relative_to(source_root)
            except ValueError:
                _fail(f"private cover reuse source {generation_key} escapes source root")
        resolved.update(
            {
                "builtin_imagegen_provenance_path": str(provenance),
                "reference_image": str(physical_assets["reference_image"]),
                "ai_background": str(physical_assets["ai_background"]),
                "final_cover": str(physical_assets["final_cover"]),
            }
        )
        # Reuse the authoritative builtin source loader/hash checks after the
        # in-memory locator rewrite; do not weaken route validation or mutate
        # the source manifest/record.
        if not validate_builtin_imagegen_provenance(
            resolved, manifest_path=provenance
        ):
            _fail("private cover reuse source builtin provenance is invalid")
    except NormalFirstPublicationMediaSuccessorError:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        raise NormalFirstPublicationMediaSuccessorError(
            "private cover reuse source builtin provenance is invalid"
        ) from exc
    return resolved


def _private_source_bundle(
    *, source_record_path: Path, candidate_id: str, title: str
) -> dict[str, object]:
    source_record = _canonical_file(
        str(source_record_path), label="private cover reuse source record"
    )
    record = _load_json_file(source_record, label="private cover reuse source record")
    staging = record.get("publish_staging")
    if not isinstance(staging, Mapping):
        _fail("private cover reuse source staging is invalid")
    source_publish = _source_publish_path(source_record, record)
    publish = _load_json_file(source_publish, label="private cover reuse source publish")
    _require_private_surfaces(
        record=record,
        staging=staging,
        publish=publish,
        package_root=source_record.parent,
        candidate_id=candidate_id,
        title=title,
        label="private cover reuse source",
        allow_absent_staging_candidate=True,
        require_record_candidate=True,
        require_record_title=True,
    )
    if staging.get("cover_status") != "AI_COVER_READY" or publish.get("cover_status") != "AI_COVER_READY":
        _fail("private cover reuse source must be AI_COVER_READY")
    source_generation = staging.get("cover_generation")
    publish_generation = publish.get("cover_generation")
    if not isinstance(source_generation, Mapping) or dict(source_generation) != dict(publish_generation or {}):
        _fail("private cover reuse source cover generation drifts")
    if source_generation.get("candidate_id") not in (None, candidate_id):
        _fail("private cover reuse source generation candidate differs")
    if source_generation.get("private_carried_forward_from_source_record") is True:
        _fail("private cover reuse source is already a private reuse")
    resolved_generation = _resolve_private_source_generation(
        source_generation, source_root=source_record.parent
    )
    if not validate_cover_route_decision(resolved_generation, allow_legacy_v1=False):
        _fail("private cover reuse source route proof is invalid")
    cover_candidates = (
        publish.get("cover_path"),
        staging.get("cover_path"),
        source_generation.get("final_cover"),
    )
    cover_path: Path | None = None
    for value in cover_candidates:
        if value:
            cover_path = _canonical_file(
                value,
                label="private cover reuse source cover",
                base=source_record.parent,
            )
            break
    if cover_path is None:
        stem = source_record.name[: -len(".record.json")]
        cover_path = _canonical_file(
            str(source_record.parent / f"{stem}.cover.png"),
            label="private cover reuse source cover",
        )
    cover_sha = "sha256:" + _sha256(cover_path)
    if cover_sha != str(source_generation.get("final_cover_sha256") or ""):
        _fail("private cover reuse source cover hash differs from generation")
    if resolved_generation.get("method") == "skill.candidates":
        from .title_cover_candidates import validate_choice_bundle
        problems = validate_choice_bundle(resolved_generation, final_cover=cover_path)
        if problems:
            _fail("private candidate cover evidence is invalid: " + "; ".join(problems))
    return {
        "record_path": source_record,
        "publish_path": source_publish,
        "record": record,
        "staging": dict(staging),
        "publish": publish,
        "generation": dict(source_generation),
        "resolved_generation": resolved_generation,
        "cover_path": cover_path,
        "cover_sha256": cover_sha,
        "record_sha256": "sha256:" + _sha256(source_record),
        "publish_sha256": "sha256:" + _sha256(source_publish),
        "generation_sha256": _canonical_sha(source_generation),
    }


def build_private_reuse_generation(
    *,
    source_record_path: Path,
    candidate_id: str,
    title: str,
    story_contract: Mapping[str, object] | None,
) -> dict[str, object]:
    """Replay a private AI cover source and emit a bound, non-public carry bundle."""

    source = _private_source_bundle(
        source_record_path=source_record_path,
        candidate_id=candidate_id,
        title=title,
    )
    # The nested source generation below stays byte-for-byte faithful to the
    # immutable record.  The outer successor gets physical builtin locators so
    # downstream source-copy and JQC validators can replay the same evidence.
    generation = deepcopy(dict(source["resolved_generation"]))
    generation.pop("carried_forward_from_published_record", None)
    generation.update(
        {
            "private_reuse_schema_version": PRIVATE_REUSE_SCHEMA_VERSION,
            "private_carried_forward_from_source_record": True,
            "candidate_id": candidate_id,
            "title": title,
            "private_source_record_path": str(source["record_path"]),
            "private_source_record_sha256": str(source["record_sha256"]),
            "private_source_publish_path": str(source["publish_path"]),
            "private_source_publish_sha256": str(source["publish_sha256"]),
            "private_source_cover_path": str(source["cover_path"]),
            "private_source_cover_sha256": str(source["cover_sha256"]),
            "private_source_generation_sha256": str(source["generation_sha256"]),
            "private_source_cover_generation": deepcopy(source["generation"]),
        }
    )
    if isinstance(story_contract, Mapping):
        generation["story_contract"] = cover_story_contract_binding(story_contract)
    return generation


def validate_private_reuse_generation(
    *,
    package_root: Path,
    record: Mapping[str, object],
    staging: Mapping[str, object],
    publish: Mapping[str, object],
    candidate_id: str,
    title: str,
    generation: Mapping[str, object],
) -> None:
    """Replay a private source binding; a boolean carry flag is insufficient."""

    if generation.get("private_reuse_schema_version") != PRIVATE_REUSE_SCHEMA_VERSION:
        _fail("private cover reuse binding schema is invalid")
    if generation.get("private_carried_forward_from_source_record") is not True:
        _fail("private cover reuse carried flag is invalid")
    if generation.get("candidate_id") != candidate_id:
        _fail("private cover reuse candidate differs")
    if generation.get("title") != title:
        _fail("private cover reuse title differs")
    if "carried_forward_from_published_record" in generation:
        _fail("private cover reuse retains published carry flag")
    source = _private_source_bundle(
        source_record_path=Path(str(generation.get("private_source_record_path") or "")),
        candidate_id=candidate_id,
        title=title,
    )
    if generation.get("private_source_record_sha256") != source["record_sha256"]:
        _fail("private cover reuse source record hash drifts")
    if generation.get("private_source_publish_path") != str(source["publish_path"]):
        _fail("private cover reuse source publish path drifts")
    if generation.get("private_source_publish_sha256") != source["publish_sha256"]:
        _fail("private cover reuse source publish hash drifts")
    if generation.get("private_source_cover_sha256") != source["cover_sha256"]:
        _fail("private cover reuse source cover hash drifts")
    if generation.get("private_source_cover_path") != str(source["cover_path"]):
        _fail("private cover reuse source cover path drifts")
    if generation.get("private_source_generation_sha256") != source["generation_sha256"]:
        _fail("private cover reuse source generation hash drifts")
    if generation.get("private_source_cover_generation") != source["generation"]:
        _fail("private cover reuse source generation drifts")
    resolved_source_generation = source["resolved_generation"]
    if not isinstance(resolved_source_generation, Mapping):
        _fail("private cover reuse resolved source generation is invalid")
    for key in (
        "builtin_imagegen_provenance_path",
        "reference_image",
        "reference_sha256",
        "ai_background",
        "ai_background_sha256",
        "final_cover",
        "final_cover_sha256",
    ):
        if generation.get(key) != resolved_source_generation.get(key):
            _fail(f"private cover reuse resolved source {key} drifts")
    if (
        "sha256:" + _normal_sha(
            generation.get("final_cover_sha256"), label="private reused cover"
        )
        != source["cover_sha256"]
    ):
        _fail("private reused cover hash differs from source")
    if not validate_cover_route_decision(generation, allow_legacy_v1=False):
        _fail("private reused cover route proof is invalid")
    _require_private_surfaces(
        record=record,
        staging=staging,
        publish=publish,
        package_root=package_root,
        candidate_id=candidate_id,
        title=title,
        label="fresh private package",
        require_record_candidate=True,
    )
    if staging.get("cover_status") != "REUSED_COVER" or publish.get("cover_status") != "REUSED_COVER":
        _fail("private reused cover status is invalid")
    if staging.get("cover_generation") != publish.get("cover_generation"):
        _fail("private reused cover generation surfaces drift")
    cover_path = publish.get("cover_path") or staging.get("cover_path")
    target_cover = _canonical_file(cover_path, label="private reused package cover", base=package_root)
    if "sha256:" + _sha256(target_cover) != source["cover_sha256"]:
        _fail("private reused package cover bytes drift")
    story_contract = record.get("story_contract")
    binding = generation.get("story_contract")
    if not isinstance(story_contract, Mapping) or not isinstance(binding, Mapping):
        _fail("private reused cover story contract binding is missing")
    if not cover_story_contract_binding_matches(story_contract, binding):
        _fail("private reused cover story contract binding drifts")


def _manifest_path(value: object) -> Path:
    if not isinstance(value, str) or not value:
        _fail("prepared handle path is missing")
    path = Path(value)
    if not path.is_absolute():
        _fail("prepared handle path must be absolute")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise NormalFirstPublicationMediaSuccessorError(
            "prepared handle path is unavailable"
        ) from exc
    if str(resolved) != value:
        _fail("prepared handle path is not canonical")
    if resolved.name != "prepared.json" or len(resolved.parents) < 5:
        _fail("prepared handle path is unsafe")
    return resolved


def _validate_authority(
    authority: object,
    *,
    candidate_id: str,
    title: str,
    verified_title_repair: object = None,
) -> dict[str, object]:
    if not isinstance(authority, Mapping):
        _fail("normal media successor requires publication authority")
    if authority.get("schema_version") != "new-bv-same-bv-publication-authority.v1":
        _fail("normal media successor requires new-BV publication authority")
    try:
        validated = validate_new_bv_repair_authority(
            authority,
            candidate_id=candidate_id,
            expected_final_title=title,
        )
    except (NewBvRepairAuthorityError, OSError, ValueError, TypeError, KeyError) as exc:
        raise NormalFirstPublicationMediaSuccessorError(
            f"normal media successor authority invalid: {exc}"
        ) from exc
    if not isinstance(validated, Mapping):
        _fail("normal media successor authority validator returned an invalid object")
    # Retain the immutable first-publication identity, while allowing an
    # already verified title transaction to carry its current title forward.
    # The native title validator replays the exact receipt and predecessor
    # chain; a new title or a status label alone still cannot pass.
    if validated.get("observed_public_title") != title:
        try:
            validate_title_repair_successor(
                verified_title_repair,
                authority=validated,
                candidate_id=candidate_id,
                title=title,
            )
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise NormalFirstPublicationMediaSuccessorError(
                "normal media successor title differs from observed original title "
                "without a valid verified title repair"
            ) from exc
    try:
        source_manifest = replay_first_publication_manifest(validated)
    except (NewBvRepairAuthorityError, OSError, ValueError, TypeError, KeyError) as exc:
        raise NormalFirstPublicationMediaSuccessorError(
            f"normal media successor source manifest invalid: {exc}"
        ) from exc
    # replay_first_publication_manifest has already revalidated candidate,
    # BV/AID/CID and source title through the authority's publication closure.
    # Keep the returned manifest as a required replay side effect without
    # guessing at optional top-level aliases that are not part of its schema.
    if not isinstance(source_manifest, Mapping):
        _fail("normal media successor source manifest is invalid")
    return validated


def _load_prepared(
    reference: Mapping[str, object], *, candidate_id: str
) -> tuple[Any, dict[str, object], Path]:
    path = _manifest_path(reference.get("handle_path"))
    runtime_root = path.parents[4]
    try:
        handle = load_prepared_delivery(runtime_root=runtime_root, manifest_path=path)
        document = _read_document(handle)
    except (ProducerDeliveryTransactionError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise NormalFirstPublicationMediaSuccessorError(
            f"prepared handle invalid: {exc}"
        ) from exc
    if handle.manifest_path != path:
        _fail("prepared handle path drift")
    if handle.lane != "talk":
        _fail("normal media successor requires talk prepared lane")
    if handle.candidate_id != candidate_id:
        _fail("prepared handle candidate differs from package")
    if reference.get("candidate_id") != candidate_id:
        _fail("prepared reference candidate differs from package")
    if reference.get("prepared_sha256") != f"sha256:{handle.prepared_sha256}":
        _fail("prepared reference digest differs from handle")
    return handle, document, runtime_root


def _validate_staged_roles(
    *,
    package_root: Path,
    stem: str,
    document: Mapping[str, object],
    runtime_root: Path,
    checked: tuple | None = None,
) -> None:
    raw_artifacts = document.get("artifacts")
    if not isinstance(raw_artifacts, list):
        _fail("prepared handle artifacts are invalid")
    entries: dict[str, Mapping[str, object]] = {}
    checked_by_role: dict[str, tuple[Mapping[str, object], Path, Path, str]] = {}
    if checked is not None:
        if not isinstance(checked, tuple):
            _fail("prepared review artifact checks are invalid")
        for row in checked:
            if not isinstance(row, tuple) or len(row) != 4 or not isinstance(row[0], Mapping):
                _fail("prepared review artifact checks are invalid")
            role = row[0].get("role")
            if not isinstance(role, str) or role in checked_by_role:
                _fail("prepared review artifact role inventory drifts")
            checked_by_role[role] = (row[0], Path(row[1]), Path(row[2]), str(row[3]))
    for raw in raw_artifacts:
        if not isinstance(raw, Mapping):
            _fail("prepared handle artifact is invalid")
        role = raw.get("role")
        if not isinstance(role, str) or role not in ROLE_SUFFIXES:
            _fail(f"prepared artifact role is unsupported: {role!r}")
        if role in entries:
            _fail(f"prepared artifact role is duplicated: {role}")
        entries[role] = raw
        target_raw = raw.get("target_path")
        if not isinstance(target_raw, str) or not Path(target_raw).is_absolute():
            _fail(f"prepared target path is invalid: {role}")
        target = Path(target_raw)
        expected_name = f"{stem}{ROLE_SUFFIXES[role]}"
        if target.name != expected_name:
            _fail(f"prepared target name does not match package role: {role}")

        staged_raw = raw.get("staged_path")
        if not isinstance(staged_raw, str) or not Path(staged_raw).is_absolute():
            _fail(f"prepared staged path is invalid: {role}")
        staged = Path(staged_raw)
        try:
            staged = staged.resolve(strict=True)
            staged.relative_to(runtime_root)
        except (OSError, ValueError) as exc:
            raise NormalFirstPublicationMediaSuccessorError(
                f"prepared staged path escapes runtime: {role}"
            ) from exc
        _regular(staged, label=f"prepared staged artifact {role}")
        expected_sha = _normal_sha(raw.get("staged_sha256"), label=f"prepared {role}")
        if checked is not None:
            checked_row = checked_by_role.get(role)
            if checked_row is None or checked_row[0] != raw:
                _fail(f"prepared review artifact inventory drifts: {role}")
            if checked_row[1] != staged or checked_row[3] != expected_sha:
                _fail(f"prepared review artifact binding drifts: {role}")
        expected_size = raw.get("staged_size")
        if isinstance(expected_size, bool) or not isinstance(expected_size, int) or expected_size < 0:
            _fail(f"prepared staged size is invalid: {role}")
        if staged.stat().st_size != expected_size or _sha256(staged) != expected_sha:
            _fail(f"prepared staged artifact drifts: {role}")
        package = _package_file(
            package_root,
            expected_name,
            label=f"package artifact {role}",
        )
        if package.stat().st_size != expected_size or _sha256(package) != expected_sha:
            _fail(f"package artifact differs from prepared handle: {role}")

    missing = sorted(REQUIRED_ROLES - set(entries))
    if missing:
        _fail("prepared handle lacks required roles: " + ",".join(missing))
    if checked is not None and set(checked_by_role) != set(entries):
        _fail("prepared review artifact role inventory drifts")
    if "uniform_host_ass" not in entries and not {"speaker_srt", "speaker_ass"}.issubset(entries):
        _fail("prepared handle lacks uniform-host ASS or speaker evidence")


def _authority_surfaces(
    *, package_root: Path, stem: str, candidate_id: str, title: str
) -> tuple[dict[str, object], object]:
    record_path = _package_file(package_root, f"{stem}.record.json", label="record")
    publish_path = _package_file(package_root, f"{stem}.publish.json", label="publish")
    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
        publish = json.loads(publish_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise NormalFirstPublicationMediaSuccessorError("package authority JSON is invalid") from exc
    if not isinstance(record, dict) or not isinstance(publish, dict):
        _fail("package authority JSON is invalid")
    staging = record.get("publish_staging")
    authority = record.get("recovery_publication_authority")
    if not isinstance(staging, Mapping) or not isinstance(authority, Mapping):
        _fail("normal media successor authority is missing from record")
    if staging.get("recovery_publication_authority") != authority:
        _fail("record/publish_staging authority drifts")
    if publish.get("recovery_publication_authority") != authority:
        _fail("record/publish authority drifts")
    if publish.get("candidate_id") != candidate_id:
        _fail("publish candidate identity drifts")
    if publish.get("title") != title:
        _fail("publish title drifts")
    return dict(authority), record.get("verified_title_repair")


def _fresh_package_surfaces(
    *, package_root: Path, stem: str, candidate_id: str, title: str
) -> tuple[dict[str, object], dict[str, object], dict[str, object], dict[str, object]]:
    """Load the private record/staging/publish surfaces for the fresh lane."""

    record_path = _package_file(package_root, f"{stem}.record.json", label="record")
    publish_path = _package_file(package_root, f"{stem}.publish.json", label="publish")
    record = _load_json_file(record_path, label="package record")
    publish = _load_json_file(publish_path, label="package publish")
    staging = record.get("publish_staging")
    if not isinstance(staging, Mapping):
        _fail("fresh private package staging is missing")
    _require_private_surfaces(
        record=record,
        staging=staging,
        publish=publish,
        package_root=package_root,
        candidate_id=candidate_id,
        title=title,
        label="fresh private package",
    )
    if publish.get("schema_version") != "shadow-publish-draft.v1":
        _fail("fresh private package publish schema is invalid")
    return record, dict(staging), publish, {
        "record_path": record_path,
        "publish_path": publish_path,
    }


def _validate_fresh_cover_binding(
    *,
    package_root: Path,
    record: Mapping[str, object],
    staging: Mapping[str, object],
    publish: Mapping[str, object],
    candidate_id: str,
    title: str,
) -> None:
    staging_generation = staging.get("cover_generation")
    publish_generation = publish.get("cover_generation")
    staging_status = staging.get("cover_status")
    publish_status = publish.get("cover_status")
    if staging_status != publish_status:
        _fail("fresh private package cover status surfaces drift")
    if {staging_status, publish_status} & {"AI_COVER_READY", "REUSED_COVER"} and not (
        isinstance(staging_generation, Mapping)
        and isinstance(publish_generation, Mapping)
    ):
        _fail("fresh private cover generation is missing")
    if isinstance(staging_generation, Mapping) or isinstance(publish_generation, Mapping):
        if not isinstance(staging_generation, Mapping) or dict(staging_generation) != dict(
            publish_generation or {}
        ):
            _fail("fresh private package cover generation surfaces drift")
        if staging_status == "REUSED_COVER" or publish_status == "REUSED_COVER":
            validate_private_reuse_generation(
                package_root=package_root,
                record=record,
                staging=staging,
                publish=publish,
                candidate_id=candidate_id,
                title=title,
                generation=staging_generation,
            )
        elif staging_status not in (None, "AI_COVER_READY") or publish_status not in (
            None,
            "AI_COVER_READY",
        ):
            _fail("fresh private package cover status is invalid")
        story_contract = record.get("story_contract")
        binding = staging_generation.get("story_contract")
        if isinstance(story_contract, Mapping) and (
            not isinstance(binding, Mapping)
            or not cover_story_contract_binding_matches(story_contract, binding)
        ):
            _fail("fresh private package story contract binding drifts")
    elif staging_status not in (None, "AI_COVER_READY") or publish_status not in (
        None,
        "AI_COVER_READY",
    ):
        _fail("fresh private package cover status is invalid")


def _validate_fresh_reference(
    *,
    package_root: Path,
    stem: str,
    reference: Mapping[str, object],
    candidate_id: str,
    title: str,
    authority: object | None,
    frozen_private_provenance: bool = False,
) -> dict[str, object]:
    if authority is not None:
        _fail("fresh private reference cannot carry publication authority")
    if set(reference) != {"schema_version", "handle_path", "prepared_sha256", "candidate_id"}:
        _fail("fresh private reference schema is invalid")
    if reference.get("schema_version") != FRESH_SCHEMA_VERSION:
        _fail("fresh private reference schema is invalid")
    if reference.get("candidate_id") != candidate_id:
        _fail("fresh private reference candidate differs from package")
    package_root = package_root.resolve()
    record, staging, publish, _paths = _fresh_package_surfaces(
        package_root=package_root,
        stem=stem,
        candidate_id=candidate_id,
        title=title,
    )
    _validate_fresh_cover_binding(
        package_root=package_root,
        record=record,
        staging=staging,
        publish=publish,
        candidate_id=candidate_id,
        title=title,
    )
    handle, document, runtime_root = _load_prepared(reference, candidate_id=candidate_id)
    try:
        current_deployment = deployment_authority_binding(runtime_root)
    except ProducerDeliveryTransactionError as exc:
        raise NormalFirstPublicationMediaSuccessorError(
            f"current deployment authority is invalid: {exc}"
        ) from exc
    checked: tuple | None = None
    if document.get("deployed_authority") != current_deployment:
        if not frozen_private_provenance:
            _fail("prepared deployment authority drifts from current runtime")
        try:
            with exclusive_runner_commit(runtime_root) as lease:
                reviewed = historical_prepared_review(handle, lease, current_package_audit=True)
        except (OSError, ValueError, ProducerDeliveryTransactionError) as exc:
            raise NormalFirstPublicationMediaSuccessorError(
                f"prepared historical review failed: {exc}"
            ) from exc
        document = reviewed.document
        checked = reviewed.checked
    if document.get("upload_enabled") is not False:
        _fail("prepared handle upload policy is invalid")
    _validate_staged_roles(
        package_root=package_root,
        stem=stem,
        document=document,
        runtime_root=runtime_root,
        checked=checked,
    )
    return {
        "schema_version": FRESH_SCHEMA_VERSION,
        "handle_path": str(handle.manifest_path),
        "prepared_sha256": f"sha256:{handle.prepared_sha256}",
        "candidate_id": candidate_id,
    }


def validate_reference(
    *,
    package_root: Path,
    stem: str,
    reference: object,
    candidate_id: str,
    title: str,
    authority: object | None = None,
    frozen_private_provenance: bool = False,
) -> dict[str, object]:
    """Revalidate one reference and all current package/handle bytes."""

    if isinstance(reference, Mapping) and reference.get("schema_version") == FRESH_SCHEMA_VERSION:
        return _validate_fresh_reference(
            package_root=package_root,
            stem=stem,
            reference=reference,
            candidate_id=candidate_id,
            title=title,
            authority=authority,
            frozen_private_provenance=frozen_private_provenance,
        )

    if not isinstance(reference, Mapping) or set(reference) != {
        "schema_version", "handle_path", "prepared_sha256", "candidate_id", "authority_sha256"
    }:
        _fail("normal media successor reference schema is invalid")
    if reference.get("schema_version") != SCHEMA_VERSION:
        _fail("normal media successor reference schema is invalid")
    package_root = package_root.resolve()
    surface_authority, verified_title_repair = _authority_surfaces(
        package_root=package_root,
        stem=stem,
        candidate_id=candidate_id,
        title=title,
    )
    if authority is not None and dict(authority) != surface_authority:
        _fail("normal media successor authority differs from record surfaces")
    validated_authority = _validate_authority(
        surface_authority,
        candidate_id=candidate_id,
        title=title,
        verified_title_repair=verified_title_repair,
    )
    expected_authority_sha = validated_authority.get("authority_sha256")
    if reference.get("authority_sha256") != expected_authority_sha or not _SHA256.fullmatch(
        str(reference.get("authority_sha256") or "")
    ):
        _fail("normal media successor authority digest differs from package")
    handle, document, runtime_root = _load_prepared(reference, candidate_id=candidate_id)
    try:
        current_deployment = deployment_authority_binding(runtime_root)
    except ProducerDeliveryTransactionError as exc:
        raise NormalFirstPublicationMediaSuccessorError(
            f"current deployment authority is invalid: {exc}"
        ) from exc
    if document.get("deployed_authority") != current_deployment:
        _fail("prepared deployment authority drifts from current runtime")
    if document.get("upload_enabled") is not False:
        _fail("prepared handle upload policy is invalid")
    _validate_staged_roles(
        package_root=package_root,
        stem=stem,
        document=document,
        runtime_root=runtime_root,
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "handle_path": str(handle.manifest_path),
        "prepared_sha256": f"sha256:{handle.prepared_sha256}",
        "candidate_id": candidate_id,
        "authority_sha256": str(expected_authority_sha),
    }


def build_reference(
    *,
    package_root: Path,
    stem: str,
    prepared_delivery_path: Path,
    candidate_id: str,
    title: str,
    authority: object | None = None,
) -> dict[str, object]:
    """Validate and return the small immutable review-package reference."""

    if authority is None:
        raw_path = Path(prepared_delivery_path)
        if not raw_path.is_absolute():
            _fail("prepared delivery path must be absolute")
        path = raw_path.resolve(strict=True)
        try:
            handle = load_prepared_delivery(
                runtime_root=path.parents[4], manifest_path=path
            )
        except (ProducerDeliveryTransactionError, OSError) as exc:
            raise NormalFirstPublicationMediaSuccessorError(
                f"prepared handle invalid: {exc}"
            ) from exc
        reference = {
            "schema_version": FRESH_SCHEMA_VERSION,
            "handle_path": str(path),
            "prepared_sha256": f"sha256:{handle.prepared_sha256}",
            "candidate_id": candidate_id,
        }
        return validate_reference(
            package_root=package_root,
            stem=stem,
            reference=reference,
            candidate_id=candidate_id,
            title=title,
            authority=None,
            frozen_private_provenance=True,
        )

    raw_path = Path(prepared_delivery_path)
    if not raw_path.is_absolute():
        _fail("prepared delivery path must be absolute")
    path = raw_path.resolve(strict=True)
    # Read the handle once to derive its digest; validate_reference repeats the
    # read so builder and auditor share the exact same fail-closed consumer.
    runtime_root = path.parents[4] if len(path.parents) >= 5 else Path("/")
    try:
        handle = load_prepared_delivery(runtime_root=runtime_root, manifest_path=path)
    except (ProducerDeliveryTransactionError, OSError) as exc:
        raise NormalFirstPublicationMediaSuccessorError(
            f"prepared handle invalid: {exc}"
        ) from exc
    reference = {
        "schema_version": SCHEMA_VERSION,
        "handle_path": str(path),
        "prepared_sha256": f"sha256:{handle.prepared_sha256}",
        "candidate_id": candidate_id,
        "authority_sha256": str(
            authority.get("authority_sha256") if isinstance(authority, Mapping) else ""
        ),
    }
    return validate_reference(
        package_root=package_root,
        stem=stem,
        reference=reference,
        candidate_id=candidate_id,
        title=title,
        authority=authority,
    )


__all__ = [
    "SCHEMA_VERSION",
    "FRESH_SCHEMA_VERSION",
    "PRIVATE_REUSE_SCHEMA_VERSION",
    "NormalFirstPublicationMediaSuccessorError",
    "build_private_reuse_generation",
    "build_reference",
    "validate_private_reuse_generation",
    "validate_reference",
]
