"""Project foreign-language audit blockers onto the immutable story scope.

Padded source context is intentionally wider than the selected story.  Foreign
language audits still inspect all retained text, but next-topic context must not
block the selected story merely because it remains in the padded witness.  This
module preserves every original finding and releases a blocker only when every
candidate blocking row is provably and wholly outside the immutable story
scope.  Missing timing, boundary straddles, and in-scope rows remain blocking.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from src.autoslice.source_subtitle_truth import candidate_boundary_owner_scope
from src.autoslice.subtitle_audio_correspondence import parse_timed_srt

OUTSIDE_IMMUTABLE_STORY_SCOPE = "OUTSIDE_IMMUTABLE_STORY_SCOPE"
_PROJECTION_SCHEMA = "foreign-audit-story-scope-projection.v1"
_SOURCE_LANGUAGE_SCHEMA = "source-language-preservation-audit.v1"
_FOREIGN_SCRIPT_SCHEMA = "foreign-script-consistency-audit.v1"


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _srt_sha256(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _blocking_finding_keys(audit: Mapping[str, object]) -> tuple[str, ...]:
    schema = audit.get("schema_version")
    status = str(audit.get("status") or "")
    if schema == _SOURCE_LANGUAGE_SCHEMA and status.startswith(
        "BLOCKED_UNPROVEN_FOREIGN_"
    ):
        return ("unproven_foreign_introductions",)
    if schema == _FOREIGN_SCRIPT_SCHEMA and status in {
        "BLOCKED_MIXED_FOREIGN_SCRIPT_CLUSTER",
        "BLOCKED_MIXED_CJK_LATIN_PHRASE",
    }:
        return ("mixed_cjk_latin_cues", "latin_heavy_cues")
    return ()


def _cue_times(srt_text: str) -> dict[str, tuple[int, int]]:
    cues = parse_timed_srt(srt_text, label="foreign audit story scope SRT")
    return {str(cue.index): (cue.start_ms, cue.end_ms) for cue in cues}


def _valid_interval(start: object, end: object) -> tuple[int, int] | None:
    if (
        isinstance(start, int)
        and not isinstance(start, bool)
        and isinstance(end, int)
        and not isinstance(end, bool)
        and 0 <= start < end
    ):
        return start, end
    return None


def _locate_finding(
    row: Mapping[str, object],
    *,
    cue_times: Mapping[str, tuple[int, int]],
) -> tuple[int, int] | None:
    direct = _valid_interval(row.get("start_ms"), row.get("end_ms"))
    if direct is not None:
        return direct
    cue_index = row.get("cue_index")
    if isinstance(cue_index, (int, str)) and not isinstance(cue_index, bool):
        return cue_times.get(str(cue_index))
    return None


def _scope_disposition(
    interval: tuple[int, int] | None,
    *,
    story_start_ms: int,
    story_end_ms: int,
) -> str:
    if interval is None:
        return "UNLOCATED_FAIL_CLOSED"
    start_ms, end_ms = interval
    if end_ms <= story_start_ms:
        return "BEFORE_IMMUTABLE_STORY_SCOPE"
    if start_ms >= story_end_ms:
        return "AFTER_IMMUTABLE_STORY_SCOPE"
    if story_start_ms <= start_ms and end_ms <= story_end_ms:
        return "INSIDE_IMMUTABLE_STORY_SCOPE"
    return "STRADDLES_IMMUTABLE_STORY_SCOPE"


def project_foreign_audit_to_story_scope(
    audit: Mapping[str, object],
    *,
    srt_text: str,
    spec: Mapping[str, object],
    durations: Sequence[int],
) -> dict[str, Any]:
    """Return one fail-closed story-scope projection of a blocked audit.

    Clean, deferred, already-resolved, and unknown future schemas are copied
    without shape changes.  Supported blocked audits receive a typed projection;
    their top-level status is released only when every candidate blocker is
    located and wholly before or after the immutable story scope.
    """

    original = copy.deepcopy(dict(audit))
    finding_keys = _blocking_finding_keys(original)
    if not finding_keys:
        return original

    try:
        scope = candidate_boundary_owner_scope(spec=spec, durations=durations)
    except RuntimeError:
        return original
    story_start_ms = int(scope["story_start_ms"])
    story_end_ms = int(scope["story_end_ms"])
    times = _cue_times(srt_text)
    findings: list[dict[str, object]] = []
    for key in finding_keys:
        rows = original.get(key)
        if not isinstance(rows, list):
            continue
        for position, row in enumerate(rows):
            if not isinstance(row, Mapping):
                findings.append(
                    {
                        "source_key": key,
                        "source_position": position,
                        "scope_disposition": "UNLOCATED_FAIL_CLOSED",
                        "finding": copy.deepcopy(row),
                    }
                )
                continue
            interval = _locate_finding(row, cue_times=times)
            disposition = _scope_disposition(
                interval,
                story_start_ms=story_start_ms,
                story_end_ms=story_end_ms,
            )
            projected: dict[str, object] = {
                "source_key": key,
                "source_position": position,
                "cue_index": row.get("cue_index"),
                "scope_disposition": disposition,
                "finding": copy.deepcopy(dict(row)),
            }
            if interval is not None:
                projected["start_ms"], projected["end_ms"] = interval
            findings.append(projected)

    outside = {
        "BEFORE_IMMUTABLE_STORY_SCOPE",
        "AFTER_IMMUTABLE_STORY_SCOPE",
    }
    outside_count = sum(
        finding["scope_disposition"] in outside for finding in findings
    )
    blocking_count = len(findings) - outside_count
    release = bool(findings) and blocking_count == 0
    projected_audit = copy.deepcopy(original)
    projected_audit["story_scope_projection"] = {
        "schema_version": _PROJECTION_SCHEMA,
        "status": "RELEASED_OUTSIDE_SCOPE" if release else "BLOCKED_IN_OR_UNKNOWN_SCOPE",
        "audit_schema_version": original.get("schema_version"),
        "audit_sha256_before_projection": _canonical_sha256(original),
        "srt_sha256": _srt_sha256(srt_text),
        "scope": copy.deepcopy(scope),
        "finding_keys": list(finding_keys),
        "findings": findings,
        "outside_count": outside_count,
        "blocking_count": blocking_count,
    }
    if release:
        projected_audit["original_status"] = original.get("status")
        projected_audit["status"] = OUTSIDE_IMMUTABLE_STORY_SCOPE
    return projected_audit


def guard_source_language(
    source_srt: str,
    current_srt: str,
    spec: Mapping[str, object],
    durations: Sequence[int],
    *,
    structured_chat_names: Sequence[str] = (),
) -> tuple[str, dict[str, Any]]:
    """Run the established guard, then apply immutable-story projection."""

    from src.autoslice.subtitle_fidelity import apply_source_language_preservation_guard

    output_srt, audit = apply_source_language_preservation_guard(
        source_srt,
        current_srt,
        structured_chat_names=structured_chat_names,
    )
    return output_srt, project_foreign_audit_to_story_scope(
        audit,
        srt_text=output_srt,
        spec=spec,
        durations=durations,
    )


def audit_foreign(
    srt_text: str,
    spec: Mapping[str, object],
    durations: Sequence[int],
) -> dict[str, Any]:
    """Run the established mixed-script audit, then project its blockers."""

    from src.autoslice.subtitle_fidelity import audit_foreign_script_consistency

    audit = audit_foreign_script_consistency(srt_text)
    return project_foreign_audit_to_story_scope(
        audit,
        srt_text=srt_text,
        spec=spec,
        durations=durations,
    )


__all__ = [
    "OUTSIDE_IMMUTABLE_STORY_SCOPE",
    "audit_foreign",
    "guard_source_language",
    "project_foreign_audit_to_story_scope",
]
