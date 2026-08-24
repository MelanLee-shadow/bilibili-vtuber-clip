"""Narrow, sealed Qixi screenshot-to-redraw displacement authority.

This module deliberately authorizes one candidate only.  It is not consulted
by generic cover maintenance, so a recoverable screenshot route remains
fail-closed everywhere else.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path

from .repository_asset_authority import require_repository_asset_authority


AUTHORITY_RELATIVE_PATH = Path(
    "assets/lidousha/qixi_cover_route_displacement/auto_113022_354_496.v1.json"
)
SCHEMA = "qixi-cover-route-displacement-authority.v1"
CANDIDATE_ID = "auto_113022_354_496"


class QixiCoverRouteDisplacementError(ValueError):
    """The sole sealed route displacement cannot be replayed."""


def _canonical_sha(value: object) -> str:
    return "sha256:" + hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _require_sha(value: object, *, label: str) -> str:
    if not isinstance(value, str) or len(value) != 71 or not value.startswith("sha256:"):
        raise QixiCoverRouteDisplacementError(f"{label} is not a sha256")
    if any(char not in "0123456789abcdef" for char in value[7:]):
        raise QixiCoverRouteDisplacementError(f"{label} is not a sha256")
    return value


def validate_authority_document(value: object) -> dict[str, object]:
    """Validate exact scope and immutable evidence bindings before any use."""

    if not isinstance(value, Mapping):
        raise QixiCoverRouteDisplacementError("route displacement authority is not an object")
    document = dict(value)
    claimed = document.pop("authority_sha256", None)
    required = {
        "schema_version", "candidate_id", "recording_date", "upload_enabled",
        "operator_scope", "predecessor", "route_preserving_attempt", "joint_qc",
        "replacement", "required_gates", "allowed_mutations",
    }
    if (
        set(document) != required
        or document.get("schema_version") != SCHEMA
        or document.get("candidate_id") != CANDIDATE_ID
        or document.get("recording_date") != "2026-08-17"
        or document.get("upload_enabled") is not False
        or _require_sha(claimed, label="authority") != _canonical_sha(document)
    ):
        raise QixiCoverRouteDisplacementError("route displacement authority is invalid")
    predecessor = document.get("predecessor")
    attempt = document.get("route_preserving_attempt")
    joint_qc = document.get("joint_qc")
    replacement = document.get("replacement")
    if not (
        isinstance(predecessor, Mapping)
        and predecessor.get("selected_treatment") == "screenshot_polish"
        and predecessor.get("source_frame_ms") == 116000
        and isinstance(attempt, Mapping)
        and attempt.get("status") == "REPAIRED"
        and attempt.get("qc_conclusion") == "CLASSROOM_NARRATIVE_REMAINS"
        and isinstance(joint_qc, Mapping)
        and joint_qc.get("status") == "PASS"
        and joint_qc.get("title") == "【李豆沙】七夕中午唱甜到腻，晚上苦情歌唱到分号！"
        and isinstance(replacement, Mapping)
        and replacement.get("actual_treatment") == "cpa_redraw"
        and replacement.get("required_title_lines") == ["李豆沙中午唱甜到腻", "晚上苦情歌唱到分号"]
    ):
        raise QixiCoverRouteDisplacementError("route displacement scope is invalid")
    for key, raw in predecessor.items():
        if key.endswith("sha256"):
            _require_sha(raw, label=f"predecessor {key}")
    for key, raw in attempt.items():
        if key.endswith("sha256"):
            _require_sha(raw, label=f"attempt {key}")
    for key, raw in replacement.items():
        if key.endswith("sha256"):
            _require_sha(raw, label=f"replacement {key}")
    document["authority_sha256"] = claimed
    return document


def load_authority(repo_root: Path) -> dict[str, object]:
    """Load an authority only when its bytes are committed/deployment-sealed."""

    path = repo_root / AUTHORITY_RELATIVE_PATH
    try:
        payload = path.read_bytes()
        require_repository_asset_authority(
            repo_root=repo_root,
            relative_path=AUTHORITY_RELATIVE_PATH,
            observed_bytes=payload,
        )
        return validate_authority_document(json.loads(payload.decode("utf-8")))
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise QixiCoverRouteDisplacementError(
            "QIXI_COVER_ROUTE_DISPLACEMENT_AUTHORITY_UNSEALED"
        ) from exc


def validate_provider_evidence(*, authority: Mapping[str, object], bundle_root: Path) -> dict[str, object]:
    """Bind the private redraw bundle to the sole permitted replacement bytes."""

    normalized = validate_authority_document(authority)
    replacement = normalized["replacement"]
    assert isinstance(replacement, Mapping)
    paths = {
        "provider_generation": ("provider_generation_relative_path", "provider_generation_sha256"),
        "provider_request": ("provider_request_relative_path", "provider_request_sha256"),
        "provider_response": ("provider_response_relative_path", "provider_response_sha256"),
    }
    checked: dict[str, object] = {}
    for name, (path_key, sha_key) in paths.items():
        relative = replacement.get(path_key)
        if not isinstance(relative, str) or Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise QixiCoverRouteDisplacementError(f"{name} path is unsafe")
        path = bundle_root / relative
        if path.is_symlink() or not path.is_file() or _sha(path) != replacement.get(sha_key):
            raise QixiCoverRouteDisplacementError(f"{name} evidence drifts")
        checked[name] = {"path": str(path), "sha256": _sha(path)}
    generation_path = bundle_root / str(replacement["provider_generation_relative_path"])
    try:
        generation = json.loads(generation_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise QixiCoverRouteDisplacementError("provider generation is unreadable") from exc
    identity = generation.get("final_host_identity_verification") if isinstance(generation, Mapping) else None
    pixels = generation.get("rendered_text_pixels") if isinstance(generation, Mapping) else None
    if not (
        isinstance(generation, Mapping)
        and generation.get("status") == "AI_COVER_READY"
        and generation.get("method") == "images.edit"
        and generation.get("image_gen_model") == "cpa"
        and generation.get("reference_sha256") == normalized["predecessor"]["source_reference_sha256"]
        and generation.get("final_cover_sha256") == replacement["final_cover_sha256"]
        and generation.get("ai_background_sha256") == replacement["route_background_sha256"]
        and generation.get("pre_overlay_sha256") == replacement["pre_overlay_sha256"]
        and isinstance(identity, Mapping) and identity.get("status") == "PASS"
        and identity.get("comparison_sha256") == replacement["identity_witness_sha256"]
        and isinstance(pixels, Mapping) and pixels.get("status") == "PASS"
        and pixels.get("mask_sha256") == replacement["title_mask_sha256"]
        and generation.get("rendered_lines") == replacement["required_title_lines"]
    ):
        raise QixiCoverRouteDisplacementError("provider generation does not meet sealed replacement contract")
    joint = normalized["joint_qc"]
    assert isinstance(joint, Mapping)
    joint_path = bundle_root / str(joint["relative_path"])
    try:
        joint_value = json.loads(joint_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise QixiCoverRouteDisplacementError("joint QC is unreadable") from exc
    if not (
        joint_path.is_file()
        and not joint_path.is_symlink()
        and _sha(joint_path) == joint["sha256"]
        and isinstance(joint_value, Mapping)
        and joint_value.get("status") == "PASS"
        and joint_value.get("pass") is True
        and joint_value.get("title") == joint["title"]
        and joint_value.get("cover_sha256") == replacement["final_cover_sha256"]
        and isinstance(joint_value.get("verdict"), Mapping)
        and joint_value["verdict"].get("unrelated_or_misleading_elements") == []
    ):
        raise QixiCoverRouteDisplacementError("joint QC does not bind replacement cover")
    checked["joint_qc"] = {"path": str(joint_path), "sha256": _sha(joint_path)}
    checked["final_cover_sha256"] = replacement["final_cover_sha256"]
    return checked
