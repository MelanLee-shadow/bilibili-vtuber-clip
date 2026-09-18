"""Create an audited HOST_ONLY v4 review-package successor without media changes.

The external receipt/comparison inputs are opened exactly once through no-follow
file descriptors and frozen in memory.  The source review package is copied into
a fresh namespace and proven byte-identical before any successor mutation.  The
builder then installs one package-contained v4 binding, refreshes the three
cover-generation surfaces plus manifest hashes/attestation, and requires the
canonical package auditor to return zero issues.  It never calls a provider,
changes media pixels, or grants upload authority.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import stat
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.autoslice.cover_host_identity_gate import (
    HOST_ONLY_AUTHORITY,
    HOST_ONLY_SCHEMA_VERSION,
    validate_final_host_identity_verification,
)
from src.autoslice.cover_route_evidence import (
    host_only_visual_safety_evidence,
    validate_cover_route_decision,
)
from src.autoslice.cover_source_composition import source_composition_scene_kind
from src.autoslice.host_only_v4_package_binding import (
    BINDING_ITEM_KEY,
    HostOnlyV4PackageBindingError,
    build_binding,
    parse_json_object,
    read_regular_file_once,
    sha256_bytes,
    validate_package_binding,
)
from src.autoslice.review_package_cover_diagnostics import (
    host_only_identity_route_blocker_detail,
)

SCHEMA_VERSION = "host-only-v4-review-package-successor.v2"
PREIMAGE_SCHEMA_VERSION = "host-only-v4-successor-preimage.v2"
FAILURE_SCHEMA_VERSION = "host-only-v4-successor-failure.v2"


class HostOnlyV4SuccessorError(ValueError):
    """The requested successor cannot be created or audited safely."""


def _sha_bytes(payload: bytes) -> str:
    return sha256_bytes(payload)


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _json_bytes(value: Mapping[str, object]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _load_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HostOnlyV4SuccessorError(f"{label} is unreadable JSON") from exc
    if not isinstance(value, dict):
        raise HostOnlyV4SuccessorError(f"{label} is not a JSON object")
    return value


def _assert_regular_tree(root: Path, *, label: str) -> None:
    try:
        entries = [root, *root.rglob("*")]
    except OSError as exc:
        raise HostOnlyV4SuccessorError(f"{label} cannot be enumerated") from exc
    for path in entries:
        info = os.lstat(path)
        if stat.S_ISLNK(info.st_mode):
            raise HostOnlyV4SuccessorError(f"{label} contains a symlink: {path}")
        if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
            raise HostOnlyV4SuccessorError(
                f"{label} contains a non-regular entry: {path}"
            )


def _snapshot(root: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        rows.append(
            {
                "path": path.relative_to(root).as_posix(),
                "bytes": path.stat().st_size,
                "sha256": _sha(path).removeprefix("sha256:"),
            }
        )
    return rows


def _contained_regular(root: Path, locator: object, *, label: str) -> Path:
    if not isinstance(locator, str) or not locator.strip():
        raise HostOnlyV4SuccessorError(f"{label} locator is absent")
    relative = Path(locator)
    if relative.is_absolute() or any(
        part in {"", ".", ".."} for part in relative.parts
    ):
        raise HostOnlyV4SuccessorError(f"{label} locator is unsafe")
    path = root.joinpath(*relative.parts)
    resolved_root = root.resolve(strict=True)
    try:
        resolved = path.resolve(strict=True)
        info = os.lstat(path)
    except OSError as exc:
        raise HostOnlyV4SuccessorError(f"{label} is unavailable") from exc
    if not resolved.is_relative_to(resolved_root):
        raise HostOnlyV4SuccessorError(f"{label} escapes the package")
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise HostOnlyV4SuccessorError(f"{label} is not a regular file")
    return path


def _write_new(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _replace(path: Path, payload: bytes) -> None:
    if path.is_symlink() or not path.is_file():
        raise HostOnlyV4SuccessorError(f"staged file is unsafe: {path}")
    temporary = path.with_name(f".{path.name}.host-only-v4.tmp")
    if os.path.lexists(temporary):
        raise HostOnlyV4SuccessorError(f"staged temporary already exists: {temporary}")
    try:
        _write_new(temporary, payload)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _find_reference(
    root: Path, *, expected_sha256: str, preferred_name: str
) -> Path:
    matches: list[Path] = []
    for path in root.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        if path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
            continue
        if _sha(path) == expected_sha256:
            matches.append(path)
    preferred = [path for path in matches if path.name == preferred_name]
    selected = preferred or matches
    if len(selected) != 1:
        raise HostOnlyV4SuccessorError(
            "package must contain exactly one bound cover reference"
        )
    return selected[0]


def _generation(document: Mapping[str, object], *, publish: bool) -> dict[str, Any]:
    if publish:
        value = document.get("cover_generation")
    else:
        staging = document.get("publish_staging")
        value = staging.get("cover_generation") if isinstance(staging, Mapping) else None
    if not isinstance(value, dict):
        raise HostOnlyV4SuccessorError("package surface lacks cover_generation")
    return value


def _preflight_frozen_witness(
    receipt: Mapping[str, object], *, comparison_bytes: bytes
) -> None:
    witness = receipt.get("witness")
    comparison_sha = _sha_bytes(comparison_bytes)
    if not (
        receipt.get("schema_version") == HOST_ONLY_SCHEMA_VERSION
        and receipt.get("authority") == HOST_ONLY_AUTHORITY
        and receipt.get("status") == "PASS"
        and receipt.get("host_only_required") is True
        and receipt.get("comparison_sha256") == comparison_sha
        and isinstance(witness, Mapping)
        and witness.get("provider") in {"cpa", "agy"}
        and witness.get("status") == "OBSERVED"
        and witness.get("image_sha256")
        == comparison_sha.removeprefix("sha256:")
    ):
        raise HostOnlyV4SuccessorError(
            "HOST_ONLY v4 receipt is failed, stale, or not bound to comparison pixels"
        )


def _validate_input_witness(
    receipt: Mapping[str, object],
    *,
    cover_bytes: bytes,
    reference_bytes: bytes,
    comparison_bytes: bytes,
) -> None:
    _preflight_frozen_witness(receipt, comparison_bytes=comparison_bytes)
    if not (
        receipt.get("final_cover_sha256") == _sha_bytes(cover_bytes)
        and receipt.get("reference_sha256") == _sha_bytes(reference_bytes)
    ):
        raise HostOnlyV4SuccessorError(
            "HOST_ONLY v4 receipt is failed, stale, or not bound to package pixels"
        )


def _audit_is_clean(result: Mapping[str, object], *, root: Path) -> bool:
    return bool(
        result.get("passed") is True
        and result.get("issues") == []
        and result.get("issue_count") == 0
        and result.get("blocking_issue_count") == 0
        and result.get("root") == str(root)
    )


def _resolve_successor_paths(
    *, source_package: Path, destination_package: Path
) -> tuple[Path, Path]:
    raw_source = source_package.absolute()
    raw_destination = destination_package.absolute()
    if not raw_source.is_dir() or raw_source.is_symlink():
        raise HostOnlyV4SuccessorError("source package is unavailable or unsafe")
    source = raw_source.resolve(strict=True)
    if os.path.lexists(raw_destination):
        raise HostOnlyV4SuccessorError("destination package already exists")
    raw_parent = raw_destination.parent
    if not raw_parent.is_dir() or raw_parent.is_symlink():
        raise HostOnlyV4SuccessorError("destination parent is unavailable or unsafe")
    destination_parent = raw_parent.resolve(strict=True)
    destination = destination_parent / raw_destination.name
    if destination_parent == source or destination_parent.is_relative_to(source):
        raise HostOnlyV4SuccessorError(
            "destination parent must be outside the immutable source package"
        )
    _assert_regular_tree(source, label="source package")
    return source, destination


def build_host_only_v4_successor(
    *,
    source_package: Path,
    destination_package: Path,
    candidate_id: str,
    witness_receipt: Path,
    comparison_image: Path,
    audit_package: Callable[[Path], dict[str, Any]],
    provider_receipt: Path | None = None,
) -> dict[str, object]:
    """Create and audit one byte-preserving HOST_ONLY review-package successor."""

    if not candidate_id.strip():
        raise HostOnlyV4SuccessorError("candidate_id is required")
    source, destination = _resolve_successor_paths(
        source_package=source_package, destination_package=destination_package
    )

    # Freeze each external input exactly once.  Every later validation and write
    # uses these bytes, so a pathname replacement after this point is irrelevant.
    try:
        witness_bytes = read_regular_file_once(
            witness_receipt, label="v4 witness receipt"
        )
        comparison_bytes = read_regular_file_once(
            comparison_image, label="v4 comparison image"
        )
        provider_bytes = (
            read_regular_file_once(provider_receipt, label="provider receipt")
            if provider_receipt is not None
            else None
        )
        receipt = parse_json_object(witness_bytes, label="v4 witness receipt")
        _preflight_frozen_witness(receipt, comparison_bytes=comparison_bytes)
    except HostOnlyV4PackageBindingError as exc:
        raise HostOnlyV4SuccessorError(str(exc)) from exc

    source_snapshot = _snapshot(source)
    preimage_path = destination.parent / "PREIMAGE-MANIFEST.json"
    build_path = destination.parent / "SUCCESSOR-BUILD.json"
    failure_path = destination.parent / "FAILURE.json"
    for path in (preimage_path, build_path, failure_path):
        if os.path.lexists(path):
            raise HostOnlyV4SuccessorError(f"successor sidecar already exists: {path}")
    _write_new(
        preimage_path,
        _json_bytes(
            {
                "schema_version": PREIMAGE_SCHEMA_VERSION,
                "source_package": str(source),
                "file_count": len(source_snapshot),
                "files": source_snapshot,
            }
        ),
    )

    try:
        shutil.copytree(source, destination, symlinks=False)
        _assert_regular_tree(destination, label="staged successor")
        copied_preimage = _snapshot(destination)
        source_after_copy = _snapshot(source)
        if copied_preimage != source_snapshot or source_after_copy != source_snapshot:
            raise HostOnlyV4SuccessorError(
                "source-to-destination preimage copy is not byte-identical"
            )
        (destination / "package_audit.json").unlink(missing_ok=True)

        manifest_path = destination / "review_manifest.json"
        manifest = _load_object(manifest_path, label="copied review manifest")
        items = manifest.get("items")
        if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
            raise HostOnlyV4SuccessorError(
                "successor requires one single-candidate manifest item"
            )
        item = items[0]
        if item.get("candidate_id") != candidate_id:
            raise HostOnlyV4SuccessorError("manifest candidate_id does not match request")
        cover = _contained_regular(destination, item.get("cover"), label="copied cover")
        video = _contained_regular(destination, item.get("video"), label="copied video")
        evidence_path = _contained_regular(
            destination, item.get("evidence_json"), label="copied evidence record"
        )
        record_path = _contained_regular(
            destination, item.get("record"), label="copied delivery record"
        )
        publish_path = _contained_regular(
            destination, item.get("publish_json"), label="copied publish draft"
        )
        if len({evidence_path, record_path, publish_path}) != 3:
            raise HostOnlyV4SuccessorError(
                "evidence, delivery, and publish surfaces must be distinct files"
            )

        evidence_doc = _load_object(evidence_path, label="copied evidence record")
        record_doc = _load_object(record_path, label="copied delivery record")
        publish_doc = _load_object(publish_path, label="copied publish draft")
        generations = (
            _generation(evidence_doc, publish=False),
            _generation(record_doc, publish=False),
            _generation(publish_doc, publish=True),
        )
        if not (generations[0] == generations[1] == generations[2]):
            raise HostOnlyV4SuccessorError("predecessor cover_generation surfaces drift")
        predecessor = generations[0]
        if not host_only_identity_route_blocker_detail(predecessor):
            raise HostOnlyV4SuccessorError(
                "predecessor is not blocked by stale HOST_ONLY identity evidence"
            )
        expected_reference_sha = predecessor.get("reference_sha256")
        if not isinstance(expected_reference_sha, str):
            raise HostOnlyV4SuccessorError("predecessor reference hash is absent")
        reference = _find_reference(
            destination,
            expected_sha256=expected_reference_sha,
            preferred_name=Path(str(predecessor.get("reference_image") or "")).name,
        )
        cover_bytes = cover.read_bytes()
        reference_bytes = reference.read_bytes()
        _validate_input_witness(
            receipt,
            cover_bytes=cover_bytes,
            reference_bytes=reference_bytes,
            comparison_bytes=comparison_bytes,
        )

        evidence_dir = destination / "evidence"
        comparison = evidence_dir / f"{candidate_id}.host-only-v4-witness.png"
        _write_new(comparison, comparison_bytes)
        provider_out: Path | None = None
        if provider_bytes is not None:
            provider_out = evidence_dir / f"{candidate_id}.host-only-v4-provider-receipt.json"
            _write_new(provider_out, provider_bytes)

        cover_rel = cover.relative_to(destination).as_posix()
        reference_rel = reference.relative_to(destination).as_posix()
        comparison_rel = comparison.relative_to(destination).as_posix()
        rebound = copy.deepcopy(receipt)
        rebound["final_cover_path"] = cover_rel
        rebound["reference_path"] = reference_rel
        rebound["comparison_path"] = comparison_rel
        witness = rebound.get("witness")
        if not isinstance(witness, dict):
            raise HostOnlyV4SuccessorError("v4 receipt witness is absent")
        witness["image_path"] = comparison_rel
        canonical_witness = evidence_dir / f"{candidate_id}.host-only-v4.json"
        canonical_bytes = _json_bytes(rebound)
        _write_new(canonical_witness, canonical_bytes)
        receipt_rel = canonical_witness.relative_to(destination).as_posix()

        successor_generation = copy.deepcopy(predecessor)
        successor_generation["final_host_identity_verification"] = rebound
        route = successor_generation.get("route_decision")
        story = successor_generation.get("story_contract")
        if not isinstance(route, dict) or not isinstance(story, Mapping):
            raise HostOnlyV4SuccessorError("HOST_ONLY route/story authority is absent")
        scene_kind = source_composition_scene_kind(
            successor_generation.get("source_composition_verification")
        )
        safety = host_only_visual_safety_evidence(story, scene_kind=scene_kind)
        if safety.get("status") != "REQUIRED":
            raise HostOnlyV4SuccessorError("story no longer requires HOST_ONLY output")
        route["host_identity_required"] = True
        route["host_only_visual_required"] = True
        route["host_only_visual_safety_evidence"] = safety
        if not (
            validate_final_host_identity_verification(successor_generation)
            and validate_cover_route_decision(
                successor_generation, allow_legacy_v1=False
            )
        ):
            raise HostOnlyV4SuccessorError("current v4 witness/route does not replay")

        evidence_doc["publish_staging"]["cover_generation"] = copy.deepcopy(
            successor_generation
        )
        record_doc["publish_staging"]["cover_generation"] = copy.deepcopy(
            successor_generation
        )
        publish_doc["cover_generation"] = copy.deepcopy(successor_generation)
        publish_bytes = _json_bytes(publish_doc)
        publish_sha = _sha_bytes(publish_bytes)
        _replace(publish_path, publish_bytes)
        for document in (evidence_doc, record_doc):
            artifact_hashes = document.get("artifact_hashes")
            if not isinstance(artifact_hashes, dict):
                raise HostOnlyV4SuccessorError("record artifact_hashes are absent")
            artifact_hashes["publish_draft_sha256"] = publish_sha
        evidence_bytes = _json_bytes(evidence_doc)
        record_bytes = _json_bytes(record_doc)
        _replace(evidence_path, evidence_bytes)
        _replace(record_path, record_bytes)

        item_sha = item.get("sha256")
        if not isinstance(item_sha, dict):
            raise HostOnlyV4SuccessorError("manifest item sha256 map is absent")
        item_sha["publish_json"] = publish_sha.removeprefix("sha256:")
        item_sha["evidence_json"] = _sha_bytes(evidence_bytes).removeprefix("sha256:")
        item[BINDING_ITEM_KEY] = build_binding(
            candidate_id=candidate_id,
            receipt_path=receipt_rel,
            receipt_bytes=canonical_bytes,
            comparison_path=comparison_rel,
            comparison_bytes=comparison_bytes,
            reference_path=reference_rel,
            reference_bytes=reference_bytes,
            final_cover_path=cover_rel,
            final_cover_bytes=cover_bytes,
        )
        attestations = manifest.get("cover_route_attestations")
        if not isinstance(attestations, list):
            raise HostOnlyV4SuccessorError("cover route attestations are absent")
        matching = [
            row
            for row in attestations
            if isinstance(row, dict) and row.get("candidate_id") == candidate_id
        ]
        if len(matching) != 1:
            raise HostOnlyV4SuccessorError(
                "manifest must contain one matching cover route attestation"
            )
        attestation = matching[0]
        attestation["route_decision"] = copy.deepcopy(route)
        attestation["final_cover_sha256"] = successor_generation[
            "final_cover_sha256"
        ]
        attestation["method"] = successor_generation["method"]
        attestation["reference_sha256"] = successor_generation["reference_sha256"]
        manifest_bytes = _json_bytes(manifest)
        _replace(manifest_path, manifest_bytes)

        try:
            validate_package_binding(
                root=destination,
                item=item,
                generation=successor_generation,
            )
        except HostOnlyV4PackageBindingError as exc:
            raise HostOnlyV4SuccessorError(str(exc)) from exc
        audit = audit_package(destination)
        audit_path = destination / "package_audit.json"
        _write_new(audit_path, _json_bytes(audit))
        if not _audit_is_clean(audit, root=destination):
            raise HostOnlyV4SuccessorError("successor package audit did not pass")
        if _snapshot(source) != source_snapshot:
            raise HostOnlyV4SuccessorError("source package changed during successor build")

        result: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "status": "PASS",
            "candidate_id": candidate_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source_package": str(source),
            "destination_package": str(destination),
            "final_cover_sha256": _sha(cover),
            "video_sha256": _sha(video),
            "reference_sha256": _sha(reference),
            "comparison_sha256": _sha(comparison),
            "canonical_witness": str(canonical_witness),
            "canonical_witness_sha256": _sha(canonical_witness),
            "provider_receipt": str(provider_out) if provider_out else None,
            "provider_receipt_sha256": _sha(provider_out) if provider_out else None,
            "publish_sha256": publish_sha,
            "evidence_record_sha256": _sha(evidence_path),
            "delivery_record_sha256": _sha(record_path),
            "review_manifest_sha256": _sha(manifest_path),
            "package_audit_sha256": _sha(audit_path),
            "route_safety_evidence": safety,
            "source_preimage_verified": True,
            "external_inputs_frozen_once": True,
            "cover_pixels_changed": False,
            "video_pixels_changed": False,
            "image_generation_calls": 0,
            "provider_calls": 0,
            "upload_calls": 0,
            "upload_allowed": False,
        }
        _write_new(build_path, _json_bytes(result))
        return result
    except Exception as exc:
        failure = {
            "schema_version": FAILURE_SCHEMA_VERSION,
            "status": "FAILED",
            "failed_at": datetime.now(timezone.utc).isoformat(),
            "candidate_id": candidate_id,
            "error_type": type(exc).__name__,
            "error": str(exc),
            "source_package_mutated": _snapshot(source) != source_snapshot,
            "provider_calls": 0,
            "upload_calls": 0,
        }
        if not os.path.lexists(failure_path):
            _write_new(failure_path, _json_bytes(failure))
        if isinstance(exc, HostOnlyV4SuccessorError):
            raise
        raise HostOnlyV4SuccessorError(str(exc)) from exc
