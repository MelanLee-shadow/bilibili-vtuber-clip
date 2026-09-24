"""Pure diagnostics for review-package cover blockers.

These helpers never mutate a real package and never relax release policy.  They
only distinguish a stale HOST_ONLY identity receipt from unrelated route-v2 or
pixel corruption by replaying the existing validators on an isolated copy.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Mapping

from src.autoslice.cover_host_identity_gate import (
    HOST_ONLY_AUTHORITY,
    HOST_ONLY_SCHEMA_VERSION,
    cover_generation_requires_host_only_final,
    validate_final_host_identity_verification,
)
from src.autoslice.cover_route_evidence import (
    host_only_visual_safety_evidence,
    validate_cover_route_decision,
)
from src.autoslice.cover_source_composition import source_composition_scene_kind
from src.autoslice.builtin_imagegen_cover import _read_source
from src.autoslice.review_package_portable_evidence import portable_item_artifact_path
from src.autoslice.host_only_v4_package_binding import (
    BINDING_ISSUE_CODE,
    HostOnlyV4PackageBindingError,
    validate_package_binding,
)


def validate_package_cover_route(generation, *, root=None, item=None) -> bool:
    if generation.get("method") != "image_gen.imagegen":
        return validate_cover_route_decision(generation, allow_legacy_v1=False)
    if root is None or item is None:
        return False
    source = portable_item_artifact_path(root, item, "cover_builtin_provenance")
    portable = dict(generation)
    try:
        if source is None:
            return False
        manifest, _ = _read_source(source)
        portable.update(
            builtin_imagegen_provenance_path=str(source),
            reference_image=str(source.parent / manifest["identity_reference"]["path"]),
            ai_background=str(portable_item_artifact_path(root, item, "cover_route_background")),
            final_cover=str(portable_item_artifact_path(root, item, "cover")),
        )
        return validate_cover_route_decision(portable, allow_legacy_v1=False)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False


def audit_builtin_imagegen_cover(*, root, item, generation, rendered_text_ready,
                                issues, stem, record_path) -> None:
    if not (rendered_text_ready and validate_package_cover_route(generation, root=root, item=item)):
        issues.append({"code": "BUILTIN_IMAGEGEN_COVER_EVIDENCE_INVALID", "severity": "BLOCK", "stem": stem,
                       "path": str(record_path) if record_path else None,
                       "detail": "Builtin image_gen requires original tool/source bytes, current identity and glyph evidence"})


def host_only_identity_route_blocker_detail(
    generation: Mapping[str, Any],
) -> str:
    """Describe an otherwise-valid route blocked only by stale HOST_ONLY proof.

    The supplied generation is never changed.  An isolated deep copy disables
    HOST_ONLY and removes its host-identity requirement.  A precise diagnostic
    is returned only when every remaining current route-v2 predicate passes.
    The real package remains blocked by the original validator.
    """

    if (
        not cover_generation_requires_host_only_final(generation)
        or validate_final_host_identity_verification(generation)
    ):
        return ""
    story = generation.get("story_contract")
    route = generation.get("route_decision")
    if not isinstance(story, Mapping) or not isinstance(route, Mapping):
        return ""

    diagnostic = copy.deepcopy(dict(generation))
    diagnostic_story = copy.deepcopy(dict(story))
    diagnostic_story["cover_fallback_mode"] = "VERIFIED_DUAL_STREAM_FRAME"
    diagnostic["story_contract"] = diagnostic_story
    diagnostic_route = diagnostic.get("route_decision")
    if not isinstance(diagnostic_route, dict):
        return ""
    scene_kind = source_composition_scene_kind(
        diagnostic.get("source_composition_verification")
    )
    diagnostic_route["host_identity_required"] = False
    diagnostic_route["host_only_visual_required"] = False
    diagnostic_route["host_only_visual_safety_evidence"] = (
        host_only_visual_safety_evidence(
            diagnostic_story,
            scene_kind=scene_kind,
        )
    )
    if not validate_cover_route_decision(
        diagnostic,
        allow_legacy_v1=False,
    ):
        return ""

    verification = generation.get("final_host_identity_verification")
    actual = dict(verification) if isinstance(verification, Mapping) else {}
    return json.dumps(
        {
            "reason_code": (
                "HOST_ONLY_FINAL_IDENTITY_VERIFICATION_MISSING_OR_STALE"
            ),
            "required_schema_version": HOST_ONLY_SCHEMA_VERSION,
            "required_authority": HOST_ONLY_AUTHORITY,
            "actual_schema_version": actual.get("schema_version"),
            "actual_authority": actual.get("authority"),
            "actual_status": actual.get("status"),
            "actual_reason_code": actual.get("reason_code"),
            "diagnostic_counterfactual": (
                "ALL_OTHER_ROUTE_V2_PREDICATES_PASS_WITH_HOST_ONLY_DISABLED"
            ),
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def host_only_v4_package_binding_blocker_detail(
    *,
    root: Path,
    item: Mapping[str, object],
    generation: Mapping[str, object],
) -> str:
    """Return the current package-binding failure without mutating the package."""

    verification = generation.get("final_host_identity_verification")
    if not isinstance(verification, Mapping):
        return ""
    if verification.get("schema_version") != HOST_ONLY_SCHEMA_VERSION:
        return ""
    try:
        validate_package_binding(root=root, item=item, generation=generation)
    except HostOnlyV4PackageBindingError as exc:
        return str(exc)
    return ""


def audit_host_only_v4_package_binding(
    *,
    root: Path,
    item: Mapping[str, object],
    generation: Mapping[str, object],
    issues: list[dict[str, Any]],
    stem: str,
    record_path: Path | None,
) -> None:
    """Preserve the review-auditor compatibility surface around the focused check."""

    detail = host_only_v4_package_binding_blocker_detail(
        root=root, item=item, generation=generation
    )
    if not detail:
        return
    issue: dict[str, Any] = {
        "code": BINDING_ISSUE_CODE,
        "severity": "BLOCK",
        "detail": detail,
    }
    if stem:
        issue["stem"] = stem
    if record_path is not None:
        issue["path"] = str(record_path)
    issues.append(issue)
