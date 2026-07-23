"""Scoped, reviewable speech memory for subtitle candidate generation.

This ledger never rewrites subtitles.  It only supplies closed-set candidates
and discourse hints to the clip-context artifact.  Source-interval truth still
belongs to ``subtitle_truth_ledger`` and must independently authorize any
deterministic text mutation.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from pathlib import Path
from typing import Mapping


SCHEMA_VERSION = "lidousha-speech-memory-ledger.v1"
ALLOWED_KINDS = frozenset({"idiolect", "alias", "nickname", "recurring_callback"})
ALLOWED_ACTIONS = frozenset({"CANDIDATE_ONLY"})
_ID_RX = re.compile(r"[a-z0-9][a-z0-9._-]{2,95}")


class SpeechMemoryLedgerError(ValueError):
    pass


def _sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _date_in_scope(value: str, scope: Mapping[str, object]) -> bool:
    try:
        target = date.fromisoformat(value)
        start = date.fromisoformat(str(scope.get("date_start") or "0001-01-01"))
        end = date.fromisoformat(str(scope.get("date_end") or "9999-12-31"))
    except ValueError as exc:
        raise SpeechMemoryLedgerError("SPEECH_MEMORY_SCOPE_DATE_INVALID") from exc
    return start <= target <= end


def _candidate_family(candidate_id: str) -> str:
    return re.sub(r"r\d+$", "", str(candidate_id or ""))


def load_scoped_speech_memory(
    ledger_path: Path,
    *,
    speaker_id: str,
    channel_id: str,
    recording_date: str,
    candidate_id: str,
    relation_id: str | None = None,
) -> dict[str, object]:
    """Load active candidate-only memories matching one exact clip scope."""

    if not ledger_path.is_file() or ledger_path.is_symlink():
        raise SpeechMemoryLedgerError("SPEECH_MEMORY_LEDGER_INVALID")
    try:
        document = json.loads(ledger_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SpeechMemoryLedgerError("SPEECH_MEMORY_LEDGER_INVALID") from exc
    if not isinstance(document, Mapping) or document.get("schema_version") != SCHEMA_VERSION:
        raise SpeechMemoryLedgerError("SPEECH_MEMORY_LEDGER_SCHEMA_INVALID")
    raw_entries = document.get("entries")
    if not isinstance(raw_entries, list):
        raise SpeechMemoryLedgerError("SPEECH_MEMORY_ENTRIES_INVALID")

    seen_ids: set[str] = set()
    active: list[dict[str, object]] = []
    referenced_supersedes: set[str] = set()
    family = _candidate_family(candidate_id)
    for raw in raw_entries:
        if not isinstance(raw, Mapping):
            raise SpeechMemoryLedgerError("SPEECH_MEMORY_ENTRY_INVALID")
        entry_id = str(raw.get("memory_id") or "")
        if _ID_RX.fullmatch(entry_id) is None or entry_id in seen_ids:
            raise SpeechMemoryLedgerError("SPEECH_MEMORY_ID_INVALID_OR_DUPLICATE")
        seen_ids.add(entry_id)
        kind = str(raw.get("kind") or "")
        action = str(raw.get("action") or "")
        status = str(raw.get("status") or "ACTIVE")
        scope = raw.get("scope")
        surfaces = raw.get("surfaces")
        canonicals = raw.get("candidate_canonicals")
        evidence = raw.get("evidence")
        if (
            kind not in ALLOWED_KINDS
            or action not in ALLOWED_ACTIONS
            or status not in {"ACTIVE", "REVOKED"}
            or not isinstance(scope, Mapping)
            or not isinstance(surfaces, list)
            or not surfaces
            or not all(isinstance(value, str) and value.strip() for value in surfaces)
            or not isinstance(canonicals, list)
            or not canonicals
            or not all(isinstance(value, str) and value.strip() for value in canonicals)
            or not isinstance(evidence, list)
            or not evidence
            or not all(
                isinstance(row, Mapping)
                and str(row.get("authority") or "").strip()
                and str(row.get("evidence_class") or "").strip()
                for row in evidence
            )
        ):
            raise SpeechMemoryLedgerError("SPEECH_MEMORY_ENTRY_FIELDS_INVALID")
        supersedes = raw.get("supersedes", [])
        if not isinstance(supersedes, list) or not all(
            isinstance(value, str) for value in supersedes
        ):
            raise SpeechMemoryLedgerError("SPEECH_MEMORY_SUPERSEDES_INVALID")
        referenced_supersedes.update(str(value) for value in supersedes)
        if status != "ACTIVE":
            continue
        if str(scope.get("speaker_id") or "") not in {"*", speaker_id}:
            continue
        if str(scope.get("channel_id") or "") not in {"*", channel_id}:
            continue
        if not _date_in_scope(recording_date, scope):
            continue
        scoped_candidate = str(scope.get("candidate_id") or "")
        if scoped_candidate and _candidate_family(scoped_candidate) != family:
            continue
        scoped_relation = str(scope.get("relation_id") or "")
        if scoped_relation and scoped_relation != str(relation_id or ""):
            continue
        active.append(
            {
                "memory_id": entry_id,
                "kind": kind,
                "action": action,
                "surfaces": [str(value) for value in surfaces],
                "candidate_canonicals": [str(value) for value in canonicals],
                "scope": dict(scope),
                "evidence": [dict(row) for row in evidence],
                "supersedes": list(supersedes),
            }
        )

    if not referenced_supersedes <= seen_ids:
        raise SpeechMemoryLedgerError("SPEECH_MEMORY_SUPERSEDES_UNKNOWN_ID")
    superseded_ids = {
        str(value)
        for row in active
        for value in (row.get("supersedes") or [])
    }
    active = [row for row in active if row["memory_id"] not in superseded_ids]

    # Conflicting active memories are useful evidence of ambiguity, not a
    # license to pick one spelling.  Expose them explicitly to the reviewer.
    by_surface: dict[str, set[str]] = {}
    for row in active:
        for surface in row["surfaces"]:
            by_surface.setdefault(surface, set()).update(row["candidate_canonicals"])
    conflicts = [
        {"surface": surface, "candidate_canonicals": sorted(values)}
        for surface, values in sorted(by_surface.items())
        if len(values) > 1
    ]
    return {
        "schema_version": "lidousha-scoped-speech-memory.v1",
        "status": "CONFLICTED" if conflicts else "PASS",
        "ledger_sha256": _sha256(ledger_path),
        "speaker_id": speaker_id,
        "channel_id": channel_id,
        "recording_date": recording_date,
        "candidate_id": candidate_id,
        "relation_id": relation_id,
        "entries": active,
        "conflicts": conflicts,
        "mutation_authorized": False,
    }
