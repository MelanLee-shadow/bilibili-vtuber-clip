"""Adopt an already witnessed identity-card trial into a private review package.

The native trial is replayed only as a deterministic proof: its five image hashes
must exactly match the frozen existing trial. The installed images are the frozen
original bytes, not a new design. The established V4 successor then supplies the
package binding and canonical audit. Source media, source records and production
state remain untouched; this module never calls a provider or grants upload rights.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import stat
from pathlib import Path
from typing import Any, Callable, Mapping

from src.autoslice import host_only_identity_card_trial as trial
from src.autoslice import host_only_v4_successor as v4
from src.autoslice.cover_host_identity_gate import validate_final_host_identity_verification
from src.autoslice.cover_route_evidence import (
    record_cover_route_execution,
    validate_cover_route_decision,
)
from src.autoslice.host_only_v4_package_binding import parse_json_object, read_regular_file_once

SCHEMA_VERSION = "host-only-identity-card-review-package-successor.v1"
IMAGE_ROLES = ("base", "poster", "cover", "pre_overlay", "title_mask")


class IdentityCardSuccessorError(ValueError):
    """The immutable trial, its witness or the complete package did not close."""


def _remap(value: Any, roots: Mapping[str, str]) -> Any:
    if isinstance(value, str):
        for old, new in sorted(roots.items(), key=lambda pair: -len(pair[0])):
            if value == old or value.startswith(old + "/"):
                return new + value[len(old) :]
        return value
    if isinstance(value, dict):
        return {key: _remap(item, roots) for key, item in value.items()}
    if isinstance(value, list):
        return [_remap(item, roots) for item in value]
    return value


def _existing_direct_execution_status(predecessor: Mapping[str, object]) -> str:
    """Retain the original explicit polish-to-direct degradation, not its veto."""
    route = predecessor.get("route_decision")
    if (
        isinstance(route, Mapping)
        and route.get("actual_treatment") == "screenshot_direct"
        and route.get("execution_status") == "READY_DEGRADED"
    ):
        return "READY_DEGRADED"
    return "READY"


def _freeze_trial(result_path: Path, candidate_id: str) -> tuple[dict, dict[str, bytes]]:
    raw = read_regular_file_once(result_path, label="identity-card trial receipt")
    result = parse_json_object(raw, label="identity-card trial receipt")
    if (
        result.get("schema_version") != trial.SCHEMA_VERSION
        or result.get("candidate_id") != candidate_id
        or result.get("status") != "PASS_PIXELS_READY_WITNESS_REQUIRED"
        or result.get("upload_allowed") is not False
        or result.get("provider_calls") != 0
        or result.get("package_writes") != 0
        or result.get("upload_calls") != 0
    ):
        raise IdentityCardSuccessorError("trial is not an unadmitted current identity-card result")
    outputs = result.get("outputs")
    if not isinstance(outputs, dict) or set(outputs) != {*IMAGE_ROLES, "generation"}:
        raise IdentityCardSuccessorError("trial output set is incomplete")
    payloads = {"trial_result": raw}
    for role, item in outputs.items():
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise IdentityCardSuccessorError("trial output locator is invalid")
        data = read_regular_file_once(Path(item["path"]), label=f"trial {role}")
        if trial._sha_bytes(data) != item.get("sha256") or len(data) != item.get("bytes"):
            raise IdentityCardSuccessorError(f"trial {role} bytes drift")
        payloads[role] = data
    return result, payloads


def build_identity_card_successor(
    *,
    source_package: Path,
    destination_package: Path,
    candidate_id: str,
    trial_result: Path,
    witness_receipt: Path,
    comparison_image: Path,
    audit_package: Callable[[Path], dict[str, Any]],
) -> dict[str, object]:
    """Build a create-only, no-upload successor of one complete video package."""
    source, destination = v4._resolve_successor_paths(
        source_package=source_package, destination_package=destination_package
    )
    parent = destination.parent
    info = parent.stat()
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise IdentityCardSuccessorError("destination parent must be owner-private 0700")
    result, payloads = _freeze_trial(trial_result, candidate_id)
    witness_bytes = read_regular_file_once(witness_receipt, label="current native V4 witness")
    comparison_bytes = read_regular_file_once(comparison_image, label="current native comparison")
    receipt = parse_json_object(witness_bytes, label="current native V4 witness")
    preimage = trial._prepare_trial_preimage(source=source, candidate_id=candidate_id)
    declared_inputs = result.get("source_inputs")
    if not isinstance(declared_inputs, dict):
        raise IdentityCardSuccessorError("trial source-input bindings are absent")
    for role, actual in preimage.before.items():
        declared = declared_inputs.get(role)
        if not isinstance(declared, dict) or any(
            declared.get(key) != actual[key] for key in ("bytes", "sha256")
        ):
            raise IdentityCardSuccessorError(f"trial/source package preimage drifts: {role}")
    v4._validate_input_witness(
        receipt,
        cover_bytes=payloads["cover"],
        reference_bytes=read_regular_file_once(preimage.reference, label="bound source reference"),
        comparison_bytes=comparison_bytes,
    )
    supplied = parse_json_object(payloads["generation"], label="trial generation")
    probe = copy.deepcopy(supplied)
    probe["final_host_identity_verification"] = receipt
    if not validate_final_host_identity_verification(probe):
        raise IdentityCardSuccessorError("current V4 verdict does not replay")

    # A fresh private bridge is deliberately not a deliverable. It allows reuse
    # of the established byte-preserving V4 package binder without weakening it.
    verification = parent / "deterministic-verification"
    bridge = parent / "pixel-bridge-not-deliverable"
    outer_result = parent / "IDENTITY-CARD-SUCCESSOR.json"
    for path in (verification, bridge, outer_result, parent / "IDENTITY-CARD-FAILURE.json"):
        if os.path.lexists(path):
            raise IdentityCardSuccessorError(f"adapter destination already exists: {path.name}")
    source_snapshot = v4._snapshot(source)
    trial._write_new(
        parent / "ORIGINAL-PACKAGE-PREIMAGE.json",
        trial._json_bytes(
            {
                "source_package": str(source),
                "files": source_snapshot,
                "trial_receipt_sha256": trial._sha_bytes(payloads["trial_result"]),
                "native_witness_sha256": trial._sha_bytes(witness_bytes),
            }
        ),
    )
    try:
        verification.mkdir(mode=0o700)
        authority_path = None
        authority = declared_inputs.get("title_exclusion_authority")
        if authority is not None:
            if not isinstance(authority, dict) or not isinstance(authority.get("path"), str):
                raise IdentityCardSuccessorError("title authority binding is invalid")
            authority_path = Path(authority["path"])
            authority_bytes = read_regular_file_once(authority_path, label="bound title review")
            if trial._sha_bytes(authority_bytes) != authority.get("sha256"):
                raise IdentityCardSuccessorError("title authority bytes drift")
        replay = trial.build_host_only_identity_card_trial(
            source_package=source,
            destination=verification / "replay",
            candidate_id=candidate_id,
            title_exclusion_authority=authority_path,
        )
        if replay.get("status") != "PASS_PIXELS_READY_WITNESS_REQUIRED":
            raise IdentityCardSuccessorError("native deterministic trial replay did not close")
        for role in IMAGE_ROLES:
            if replay["outputs"][role]["sha256"] != trial._sha_bytes(payloads[role]):
                raise IdentityCardSuccessorError(
                    f"native {role} recomposition differs from witnessed bytes"
                )
        replay_generation = parse_json_object(
            read_regular_file_once(
                Path(replay["outputs"]["generation"]["path"]), label="replayed generation"
            ),
            label="replayed generation",
        )
        normalized_supplied = _remap(
            supplied,
            {
                str(result["source_package"]): str(source),
                str(result["destination"]): str(replay["destination"]),
            },
        )
        if normalized_supplied != replay_generation:
            differences = trial._json_difference_paths(normalized_supplied, replay_generation)
            raise IdentityCardSuccessorError(
                "trial generation replay drift: " + ", ".join(differences[:12])
            )
        shutil.copytree(source, bridge)
        if v4._snapshot(bridge) != source_snapshot or v4._snapshot(source) != source_snapshot:
            raise IdentityCardSuccessorError("complete source copy changed")
        manifest = v4._load_object(bridge / "review_manifest.json", label="bridge manifest")
        if (
            len(manifest.get("items") or []) != 1
            or manifest["items"][0].get("candidate_id") != candidate_id
        ):
            raise IdentityCardSuccessorError("complete source manifest is not candidate-specific")
        item = manifest["items"][0]
        # Requiring real media here prevents a cover-only subset from masquerading
        # as an audited video package. Its original bytes are never rewritten.
        video = v4._contained_regular(bridge, item.get("video"), label="complete video")
        cover = v4._contained_regular(bridge, item.get("cover"), label="existing package cover")
        asset_dir = bridge / "identity-card-successor"
        asset_dir.mkdir(mode=0o700)
        names = {
            "base": "base.png",
            "poster": "route-background.png",
            "pre_overlay": "pre-overlay.png",
            "title_mask": "title-mask.png",
        }
        for role, name in names.items():
            trial._write_new(asset_dir / name, payloads[role])
        trial._write_new(asset_dir / "parent-cover.png", cover.read_bytes())
        trial._write_new(asset_dir / "trial-result.original.json", payloads["trial_result"])
        trial._write_new(asset_dir / "trial-generation.original.json", payloads["generation"])
        trial._write_new(asset_dir / "native-witness.original.json", witness_bytes)
        v4._replace(cover, payloads["cover"])
        path_map = {str(source): str(bridge)}
        for role, name in names.items():
            path_map[replay["outputs"][role]["path"]] = str(asset_dir / name)
        path_map[replay["outputs"]["cover"]["path"]] = str(cover)
        generation = _remap(replay_generation, path_map)
        # Provenance objects are immutable evidence, not relocatable runtime
        # locators. In particular, moving source-composition witness paths
        # would invalidate its canonical seal even when image bytes match.
        for key in (
            "source_composition_verification",
            "story_contract",
            "art_direction",
            "identity_card_pixel_successor",
        ):
            if key in replay_generation:
                generation[key] = copy.deepcopy(replay_generation[key])
        generation["status"] = "AI_COVER_READY"
        record_cover_route_execution(
            generation,
            actual_treatment="screenshot_direct",
            execution_status=_existing_direct_execution_status(preimage.predecessor),
            image_generation_attempted=False,
            image_generation_used=False,
            detail="Existing identity-card bytes replayed exactly; current native V4 is frozen for package binding.",
        )
        checked = copy.deepcopy(generation)
        checked["final_host_identity_verification"] = receipt
        if not validate_cover_route_decision(checked, allow_legacy_v1=False):
            raise IdentityCardSuccessorError("witnessed current route does not replay")
        hashes = {
            "cover_sha256": trial._sha_bytes(payloads["cover"]),
            "ai_background_sha256": trial._sha_bytes(payloads["poster"]),
            "cover_reference_sha256": preimage.predecessor["reference_sha256"],
        }
        documents = {}
        for key in ("evidence_json", "record", "publish_json"):
            path = v4._contained_regular(bridge, item.get(key), label=key)
            raw = path.read_bytes()
            trial._write_new(asset_dir / (key + ".parent.json"), raw)
            documents[key] = (path, json.loads(raw))
        publish_path, publish = documents["publish_json"]
        publish["cover_generation"] = copy.deepcopy(generation)
        publish["cover_path"] = str(cover)
        publish.setdefault("artifact_hashes", {}).update(hashes)
        pub_bytes = v4._json_bytes(publish)
        v4._replace(publish_path, pub_bytes)
        for key in ("evidence_json", "record"):
            path, document = documents[key]
            document["publish_staging"]["cover_generation"] = copy.deepcopy(generation)
            document["publish_staging"]["cover_path"] = str(cover)
            document["artifact_hashes"].update(hashes)
            document["artifact_hashes"]["publish_draft_sha256"] = trial._sha_bytes(pub_bytes)
            v4._replace(path, v4._json_bytes(document))
        item["sha256"]["cover"] = hashes["cover_sha256"].removeprefix("sha256:")
        for key in ("publish_json", "evidence_json"):
            item["sha256"][key] = v4._sha(documents[key][0]).removeprefix("sha256:")
        for key, role in (
            ("cover_pre_overlay", "pre_overlay"),
            ("cover_title_mask", "title_mask"),
            ("cover_route_background", "poster"),
        ):
            item[key] = (asset_dir / names[role]).relative_to(bridge).as_posix()
        matches = [
            a
            for a in manifest.get("cover_route_attestations", [])
            if a.get("candidate_id") == candidate_id
        ]
        if len(matches) != 1:
            raise IdentityCardSuccessorError("source cover attestation is absent or ambiguous")
        matches[0].update(
            {
                key: copy.deepcopy(generation[key])
                for key in ("route_decision", "final_cover_sha256", "reference_sha256", "method")
            }
        )
        v4._replace(bridge / "review_manifest.json", v4._json_bytes(manifest))
        frozen_witness = parent / "frozen-native-v4.json"
        frozen_comparison = parent / "frozen-native-comparison.png"
        trial._write_new(frozen_witness, witness_bytes)
        trial._write_new(frozen_comparison, comparison_bytes)
        inner = v4.build_host_only_v4_successor(
            source_package=bridge,
            destination_package=destination,
            candidate_id=candidate_id,
            witness_receipt=frozen_witness,
            comparison_image=frozen_comparison,
            audit_package=audit_package,
        )
        if v4._snapshot(source) != source_snapshot or v4._sha(video) != inner["video_sha256"]:
            raise IdentityCardSuccessorError("original package or immutable video changed")
        result = {
            "schema_version": SCHEMA_VERSION,
            "status": "PASS",
            "candidate_id": candidate_id,
            "source_package": str(source),
            "destination_package": str(destination),
            "intermediate_package": str(bridge),
            "source_preimage_unchanged": True,
            "original_cover_pixels_changed": True,
            "video_pixels_changed": False,
            "deterministic_verification_image_count": 5,
            "installed_existing_pixels": True,
            "final_cover_sha256": inner["final_cover_sha256"],
            "video_sha256": inner["video_sha256"],
            "canonical_audit_sha256": inner["package_audit_sha256"],
            "inner_successor": inner,
            "provider_calls": 0,
            "image_generation_calls": 0,
            "production_state_writes": 0,
            "upload_calls": 0,
            "upload_allowed": False,
        }
        trial._write_new(outer_result, trial._json_bytes(result))
        return result
    except Exception as exc:
        failure = {
            "schema_version": SCHEMA_VERSION,
            "status": "FAILED",
            "candidate_id": candidate_id,
            "error_type": type(exc).__name__,
            "error": str(exc),
            "source_preimage_unchanged": v4._snapshot(source) == source_snapshot,
            "provider_calls": 0,
            "upload_calls": 0,
        }
        path = parent / "IDENTITY-CARD-FAILURE.json"
        if not os.path.lexists(path):
            trial._write_new(path, trial._json_bytes(failure))
        if isinstance(exc, IdentityCardSuccessorError):
            raise
        raise IdentityCardSuccessorError(str(exc)) from exc
