"""Pure diagnostics for review-package cover blockers.

These helpers never mutate a real package and never relax release policy.  They
only distinguish a stale HOST_ONLY identity receipt from unrelated route-v2 or
pixel corruption by replaying the existing validators on an isolated copy.
"""

from __future__ import annotations

import copy
import json
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
