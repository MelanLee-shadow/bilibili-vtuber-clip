"""Recomputable candidate-only evidence for subtitle closed-set judges."""

from __future__ import annotations

import hashlib
import json
import re
from difflib import SequenceMatcher
from typing import Any, Mapping, Sequence

from src.autoslice.jingting_chunker import parse_srt_cues


EVIDENCE_SCHEMA = "subtitle-closed-set-structured-evidence.v1"
DRAFT_FIDELITY_PROVENANCE_SCHEMA = "draft-fidelity-kept-candidate.v1"
_HEX64 = re.compile(r"^(?:sha256:)?[0-9a-f]{64}$")
_HAN = re.compile(r"[\u3400-\u9fff]")
_LEXICAL = re.compile(r"^[A-Za-z0-9_\-\u3400-\u9fff]+$")


def evidence_cue_ids(
    row: Mapping[str, Any], *, cue_count: int, target_cue_index: int
) -> list[int]:
    """Normalize bounded, distinct non-target evidence cue indexes."""

    values: set[int] = set()
    for raw in row.get("evidence_cue_ids") or []:
        try:
            cue_index = int(raw)
        except (TypeError, ValueError):
            continue
        if 1 <= cue_index <= cue_count and cue_index != target_cue_index:
            values.add(cue_index)
    return sorted(values)


def _valid_digest(value: object) -> bool:
    return isinstance(value, str) and bool(_HEX64.fullmatch(value))


def _validated_draft_fidelity_provenance(
    provenance: Mapping[str, Any], *, proposed_cue: str, current_cue: str, cue_index: int
) -> dict[str, object] | None:
    kept_hash = str(provenance.get("kept_text_sha256") or "")
    candidate_hashes = {
        "CURRENT": "sha256:" + hashlib.sha256(current_cue.encode("utf-8")).hexdigest(),
        "PROPOSED": "sha256:" + hashlib.sha256(proposed_cue.encode("utf-8")).hexdigest(),
    }
    declared_kept_candidate = str(provenance.get("kept_candidate") or "")
    kept_candidate = (
        declared_kept_candidate
        if candidate_hashes.get(declared_kept_candidate) == kept_hash
        and (current_cue != proposed_cue or declared_kept_candidate == "CURRENT")
        else ""
    )
    surface = str(provenance.get("surface") or "")
    if not (
        provenance.get("schema_version") == DRAFT_FIDELITY_PROVENANCE_SCHEMA
        and provenance.get("kind") == "draft_fidelity_kept"
        and provenance.get("scope") == "cue"
        and provenance.get("draft_fidelity_kept") is True
        and provenance.get("mutation_authorized") is False
        and provenance.get("cue_index") == cue_index
        and _valid_digest(provenance.get("audit_sha256"))
        and _valid_digest(provenance.get("source_srt_sha256"))
        and kept_candidate
        and provenance.get("kept_candidate") == kept_candidate
        and provenance.get("current_text_sha256")
        == "sha256:" + hashlib.sha256(current_cue.encode("utf-8")).hexdigest()
        and (not surface or surface in (current_cue if kept_candidate == "CURRENT" else proposed_cue))
    ):
        return None
    return {
        "schema_version": DRAFT_FIDELITY_PROVENANCE_SCHEMA,
        "kind": "draft_fidelity_kept",
        "scope": "cue",
        "cue_index": cue_index,
        "start_ms": provenance.get("start_ms"),
        "end_ms": provenance.get("end_ms"),
        "draft_fidelity_kept": True,
        "kept_candidate": kept_candidate,
        "surface": surface,
        "audit_sha256": provenance["audit_sha256"],
        "source_srt_sha256": provenance["source_srt_sha256"],
        "kept_text_sha256": provenance["kept_text_sha256"],
        "current_text_sha256": provenance["current_text_sha256"],
        "violation_reason_codes": sorted(
            {
                str(value)
                for value in provenance.get("violation_reason_codes") or []
                if str(value)
            }
        ),
        "mutation_authorized": False,
    }


def _validated_session_recurrence_provenance(
    provenance: Mapping[str, Any], *, proposed_cue: str, current_srt_sha256: str,
    cue_count: int, target_cue_index: int
) -> dict[str, object] | None:
    surface = str(provenance.get("surface") or "")
    positions = provenance.get("occurrence_positions")
    max_distance = provenance.get("max_cue_distance")
    if not (
        provenance.get("schema_version")
        == "session-transcript-recurrence-provenance.v1"
        and provenance.get("kind") == "session_transcript_recurrence"
        and provenance.get("scope") == "session"
        and provenance.get("mutation_authorized") is False
        and provenance.get("global_glossary_authorized") is False
        and 2 <= len(surface) <= 8
        and _LEXICAL.fullmatch(surface)
        and _HAN.search(surface)
        and surface in proposed_cue
        and _valid_digest(provenance.get("session_scope_id"))
        and _valid_digest(provenance.get("source_srt_sha256"))
        and provenance.get("session_scope_id") == provenance.get("source_srt_sha256")
        and provenance.get("current_srt_sha256") == current_srt_sha256
        and isinstance(max_distance, int)
        and not isinstance(max_distance, bool)
        and 1 <= max_distance <= 64
        and isinstance(positions, list)
    ):
        return None
    sanitized: list[dict[str, object]] = []
    for position in positions:
        if not isinstance(position, Mapping):
            return None
        try:
            cue_index = int(position.get("cue_index"))
            start_ms = int(position.get("start_ms"))
            end_ms = int(position.get("end_ms"))
            start = int(position.get("start_codepoint"))
            end = int(position.get("end_codepoint"))
        except (TypeError, ValueError):
            return None
        if not (
            1 <= cue_index <= cue_count
            and 0 <= start_ms < end_ms
            and 0 <= start < end
            and _valid_digest(position.get("cue_text_sha256"))
        ):
            return None
        sanitized.append(
            {
                "cue_index": cue_index,
                "start_ms": start_ms,
                "end_ms": end_ms,
                "start_codepoint": start,
                "end_codepoint": end,
                "cue_text_sha256": position["cue_text_sha256"],
            }
        )
    occurrence_cues = sorted({int(row["cue_index"]) for row in sanitized})
    position_keys = {
        (row["cue_index"], row["start_codepoint"], row["end_codepoint"])
        for row in sanitized
    }
    if not (
        len(position_keys) == len(sanitized)
        and len(occurrence_cues) >= 2
        and target_cue_index in occurrence_cues
        and all(abs(index - target_cue_index) <= max_distance for index in occurrence_cues)
        and int(provenance.get("occurrence_count") or 0) == len(sanitized)
        and int(provenance.get("occurrence_cue_count") or 0) == len(occurrence_cues)
    ):
        return None
    return {
        "schema_version": "session-transcript-recurrence-provenance.v1",
        "kind": "session_transcript_recurrence",
        "scope": "session",
        "session_scope_id": provenance["session_scope_id"],
        "source_srt_sha256": provenance["source_srt_sha256"],
        "current_srt_sha256": provenance["current_srt_sha256"],
        "surface": surface,
        "occurrence_count": len(sanitized),
        "occurrence_cue_count": len(occurrence_cues),
        "occurrence_positions": sanitized,
        "max_cue_distance": max_distance,
        "mutation_authorized": False,
        "global_glossary_authorized": False,
    }


def validated_priority_candidate_provenance(
    row: Mapping[str, Any], *, proposed_cue: str, current_cue: str, cue_index: int,
    current_srt_sha256: str, cue_count: int, trusted_priority: bool
) -> dict[str, object] | None:
    """Accept only deterministic candidate-only provenance from priority rows."""

    if not trusted_priority:
        return None
    provenance = row.get("candidate_provenance")
    if not isinstance(provenance, Mapping):
        return None
    if provenance.get("kind") == "draft_fidelity_kept":
        return _validated_draft_fidelity_provenance(
            provenance,
            proposed_cue=proposed_cue,
            current_cue=current_cue,
            cue_index=cue_index,
        )
    if provenance.get("kind") == "session_transcript_recurrence":
        return _validated_session_recurrence_provenance(
            provenance, proposed_cue=proposed_cue,
            current_srt_sha256=current_srt_sha256, cue_count=cue_count,
            target_cue_index=cue_index,
        )
    return None


def current_draft_fidelity_context(
    rows: Sequence[object], *, current_cue: str, cue_index: int,
    trusted_sentinel: object
) -> dict[str, object] | None:
    """Recover a trusted cue-level kept=CURRENT signal for any fresh proposal."""

    for row in rows:
        if not isinstance(row, Mapping) or row.get("_trusted_priority_candidate") is not trusted_sentinel:
            continue
        try:
            row_cue = int(row.get("cue") or row.get("cue_index") or 0)
        except (TypeError, ValueError):
            continue
        provenance = row.get("draft_fidelity_kept_provenance")
        if not isinstance(provenance, Mapping):
            provenance = row.get("candidate_provenance")
        if row_cue != cue_index or not isinstance(provenance, Mapping):
            continue
        validated = _validated_draft_fidelity_provenance(
            provenance,
            proposed_cue=current_cue,
            current_cue=current_cue,
            cue_index=cue_index,
        )
        if validated is not None and validated.get("kept_candidate") == "CURRENT":
            return validated
    return None


def _candidate_surfaces(
    *, current_cue: str, proposed_cue: str, finding: Mapping[str, Any]
) -> list[str]:
    values: list[str] = []
    def add(value: str) -> None:
        if len(value) >= 2 and value not in values:
            values.append(value)

    fidelity_context = finding.get("draft_fidelity_kept_provenance")
    candidate_provenance = finding.get("candidate_provenance")
    provenance = (
        fidelity_context
        if isinstance(fidelity_context, Mapping)
        else candidate_provenance
    )
    if isinstance(provenance, Mapping):
        add(str(provenance.get("surface") or ""))
    if isinstance(candidate_provenance, Mapping) and candidate_provenance is not provenance:
        add(str(candidate_provenance.get("surface") or ""))
    suggestion = str(finding.get("suggestion") or finding.get("replacement") or "")
    add(suggestion)
    favored = (
        str(provenance.get("kept_candidate") or "")
        if isinstance(provenance, Mapping)
        and provenance.get("kind") == "draft_fidelity_kept"
        else "PROPOSED"
    )
    favored_text = current_cue if favored == "CURRENT" else proposed_cue
    other_text = proposed_cue if favored == "CURRENT" else current_cue
    if favored == "CURRENT":
        add(str(finding.get("suspect") or ""))
    for width in range(2, min(8, len(favored_text)) + 1):
        for start in range(0, len(favored_text) - width + 1):
            surface = favored_text[start : start + width]
            if (
                surface not in other_text
                and _LEXICAL.fullmatch(surface)
                and _HAN.search(surface)
            ):
                add(surface)
    return values


def _bound_chat_support(
    *,
    clip_context: Mapping[str, object] | None,
    cues: Sequence[Any],
    surfaces: Sequence[str],
    candidate: str,
) -> dict[str, object] | None:
    rows = clip_context.get("structured_chat") if isinstance(clip_context, Mapping) else None
    if not isinstance(rows, list):
        return None
    matches: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        if not isinstance(row, Mapping) or not _valid_digest(row.get("source_sha256")):
            continue
        text = str(row.get("text") or row.get("message") or "")
        for surface in surfaces:
            if surface and surface.casefold() in text.casefold():
                matches.setdefault(surface, []).append(row)
    if not matches:
        return None
    surface, bound_rows = max(
        matches.items(), key=lambda item: (len(item[1]), len(item[0]), item[0])
    )
    cue_ids: set[int] = set()
    for row in bound_rows:
        offset = row.get("offset_ms")
        if not isinstance(offset, (int, float)) or isinstance(offset, bool):
            continue
        for cue_index, cue in enumerate(cues, start=1):
            if cue.start_ms <= float(offset) <= cue.end_ms:
                cue_ids.add(cue_index)
                break
    return {
        "kind": "structured_chat_bound",
        "candidate": candidate,
        "surface": surface,
        "bound_event_count": len(bound_rows),
        "evidence_cue_ids": sorted(cue_ids),
        "evidence_cue_count": len(cue_ids),
        "source_sha256s": sorted(
            {str(row["source_sha256"]) for row in bound_rows}
        ),
        "source_event_ids": sorted(
            {
                str(row.get("source_event_id"))
                for row in bound_rows
                if str(row.get("source_event_id") or "")
            }
        ),
    }


def closed_set_structured_evidence(
    srt_text: str,
    finding: Mapping[str, Any],
    *,
    proposed_cue: str,
    clip_context: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Compute the three F16 judge inputs and bind them with one digest."""

    cues = [cue for cue in parse_srt_cues(srt_text) if cue.text.strip()]
    try:
        cue_index = int(finding.get("cue_index") or finding.get("cue") or 0)
    except (TypeError, ValueError):
        cue_index = 0
    current_cue = cues[cue_index - 1].text if 1 <= cue_index <= len(cues) else ""
    fidelity_context = finding.get("draft_fidelity_kept_provenance")
    candidate_provenance = finding.get("candidate_provenance")
    provenance = (
        fidelity_context
        if isinstance(fidelity_context, Mapping)
        else candidate_provenance
    )
    provenance_kind = (
        str(provenance.get("kind") or "") if isinstance(provenance, Mapping) else ""
    )
    draft_kept = provenance_kind == "draft_fidelity_kept" and bool(
        provenance.get("draft_fidelity_kept")
    )
    kept_candidate = (
        str(provenance.get("kept_candidate") or "")
        if draft_kept and isinstance(provenance, Mapping)
        else ""
    )
    evidence_candidate = kept_candidate or "PROPOSED"
    surfaces = _candidate_surfaces(
        current_cue=current_cue,
        proposed_cue=proposed_cue,
        finding=finding,
    )
    chat = _bound_chat_support(
        clip_context=clip_context,
        cues=cues,
        surfaces=surfaces,
        candidate=evidence_candidate,
    )
    if (
        chat is None
        and isinstance(candidate_provenance, Mapping)
        and candidate_provenance.get("kind") == "structured_chat_bound"
    ):
        ids = evidence_cue_ids(
            finding, cue_count=len(cues), target_cue_index=cue_index
        )
        chat = {
            "kind": "structured_chat_bound",
            "candidate": "PROPOSED",
            "surface": str(candidate_provenance.get("surface") or ""),
            "bound_event_count": 1 if _valid_digest(candidate_provenance.get("source_sha256")) else 0,
            "evidence_cue_ids": ids,
            "evidence_cue_count": len(ids),
            "source_sha256s": (
                [str(candidate_provenance["source_sha256"])]
                if _valid_digest(candidate_provenance.get("source_sha256"))
                else []
            ),
            "source_event_ids": (
                [str(candidate_provenance["source_event_id"])]
                if str(candidate_provenance.get("source_event_id") or "")
                else []
            ),
        }
    provenance_surface = (
        str(provenance.get("surface") or "")
        if isinstance(provenance, Mapping)
        else ""
    )
    suggested_surface = str(
        finding.get("suggestion") or finding.get("replacement") or ""
    )
    surface = (
        str((chat or {}).get("surface") or "")
        or provenance_surface
        or (suggested_surface if len(suggested_surface) >= 2 else "")
        or (surfaces[0] if surfaces else "")
    )
    neighbor_ids = [
        index
        for index in range(max(1, cue_index - 2), min(len(cues), cue_index + 2) + 1)
        if index != cue_index and surface and surface.casefold() in cues[index - 1].text.casefold()
    ]
    kept_text = current_cue if kept_candidate == "CURRENT" else proposed_cue
    payload: dict[str, object] = {
        "schema_version": EVIDENCE_SCHEMA,
        "cue_index": cue_index,
        "draft_fidelity": {
            "draft_fidelity_kept": draft_kept,
            "favored_candidate": kept_candidate or None,
            "current_similarity": (
                round(SequenceMatcher(None, current_cue, kept_text).ratio(), 6)
                if draft_kept
                else None
            ),
            "proposed_similarity": (
                round(SequenceMatcher(None, proposed_cue, kept_text).ratio(), 6)
                if draft_kept
                else None
            ),
            "kept_text_sha256": (
                provenance.get("kept_text_sha256")
                if draft_kept and isinstance(provenance, Mapping)
                else None
            ),
        },
        "neighbor_lexical_hits": {
            "candidate": evidence_candidate,
            "surface": surface,
            "radius_cues": 2,
            "cue_ids": neighbor_ids,
            "cue_count": len(neighbor_ids),
        },
        "structured_chat_binding": chat
        or {
            "kind": None,
            "candidate": evidence_candidate,
            "surface": surface,
            "bound_event_count": 0,
            "evidence_cue_ids": [],
            "evidence_cue_count": 0,
            "source_sha256s": [],
            "source_event_ids": [],
        },
    }
    payload["evidence_sha256"] = "sha256:" + hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return payload


def session_recurrence_acoustic_gate(
    check_request: Mapping[str, Any], witness: Mapping[str, Any]
) -> dict[str, object] | None:
    """Require a fresh audible observation for every recurrence candidate."""

    provenance = check_request.get("candidate_provenance")
    if not isinstance(provenance, Mapping) or provenance.get("kind") != "session_transcript_recurrence":
        return None
    passed = bool(
        witness.get("schema_version") == "subtitle-span-acoustic-witness.v1"
        and witness.get("status") == "OBSERVED"
        and witness.get("target_audible") is True
        and str(witness.get("heard_pinyin") or "").strip()
    )
    return {
        "schema_version": "session-transcript-recurrence-acoustic-gate.v1",
        "status": "PASS" if passed else "BLOCK",
        "candidate_scope": "session",
        "surface": str(provenance.get("surface") or ""),
        "occurrence_positions": list(provenance.get("occurrence_positions") or []),
        "witness_status": witness.get("status"),
        "target_audible": witness.get("target_audible"),
        "reason_code": None if passed else "SESSION_RECURRENCE_TARGET_ACOUSTIC_WITNESS_REQUIRED",
    }


def structured_chat_lines(
    clip_context: Mapping[str, object] | None,
    *,
    target_start_ms: int | None = None,
    target_end_ms: int | None = None,
) -> str:
    """Render a bounded platform-recorded chat/SC window for a judge."""

    if not isinstance(clip_context, Mapping):
        return ""
    rows = clip_context.get("structured_chat")
    if not isinstance(rows, list):
        return ""
    selected_rows = rows
    if isinstance(target_start_ms, int) and isinstance(target_end_ms, int) and target_end_ms >= target_start_ms:
        timed_rows = [
            row
            for row in rows
            if isinstance(row, Mapping)
            and isinstance(row.get("offset_ms"), (int, float))
            and not isinstance(row.get("offset_ms"), bool)
        ]
        timed_rows.sort(key=lambda row: float(row["offset_ms"]))
        selected_rows = [
            row
            for row in timed_rows
            if target_start_ms - 30_000 <= float(row["offset_ms"]) <= target_end_ms + 30_000
        ]
        if not selected_rows and timed_rows:
            target_mid = (target_start_ms + target_end_ms) / 2
            nearest = min(
                range(len(timed_rows)),
                key=lambda index: abs(float(timed_rows[index]["offset_ms"]) - target_mid),
            )
            selected_rows = timed_rows[max(0, nearest - 5) : nearest + 6]
        elif len(selected_rows) > 20:
            target_mid = (target_start_ms + target_end_ms) / 2
            selected_rows = sorted(
                selected_rows,
                key=lambda row: abs(float(row["offset_ms"]) - target_mid),
            )[:20]
            selected_rows.sort(key=lambda row: float(row["offset_ms"]))
    lines: list[str] = []
    for row in selected_rows[:20]:
        if not isinstance(row, Mapping):
            continue
        sender = str(row.get("sender") or "").strip()
        text = str(row.get("text") or row.get("message") or "").strip()
        if not (text or sender):
            continue
        offset = row.get("offset_ms")
        kind = str(row.get("kind") or "chat")
        prefix = (
            f"[{float(offset) / 1000:+.3f}s][{kind}] "
            if isinstance(offset, (int, float)) and not isinstance(offset, bool)
            else f"[{kind}] "
        )
        lines.append(f"- {prefix}{sender}: {text}"[:240])
    return "\n".join(lines)


def structured_chat_lines_for_finding(
    srt_text: str,
    finding: Mapping[str, Any],
    clip_context: Mapping[str, object] | None,
) -> str:
    try:
        cue_index = int(finding.get("cue_index") or 0)
    except (TypeError, ValueError):
        cue_index = 0
    cues = [cue for cue in parse_srt_cues(srt_text) if cue.text.strip()]
    if not 1 <= cue_index <= len(cues):
        return structured_chat_lines(clip_context)
    cue = cues[cue_index - 1]
    return structured_chat_lines(
        clip_context,
        target_start_ms=cue.start_ms,
        target_end_ms=cue.end_ms,
    )
