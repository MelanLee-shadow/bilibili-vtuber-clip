"""Evidence-bound session relationship authority.

The passive collaboration evidence-capture sidecar is a dataset collector; its
``NO_TRIGGER`` status is not a semantic verdict. Session relationship claims
therefore live in a separate committed ledger and may be used by selection,
title, cover, and reporting only after an exact date/recording binding.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Mapping


SCHEMA_VERSION = "session-relation-ledger.v1"
AUTHORITY_SCHEMA_VERSION = "session-relation-authority.v1"
RELATION_STATES = frozenset({"CONFIRMED", "CLAIMED", "UNKNOWN", "CONFLICTED"})


class SessionRelationAuthorityError(ValueError):
    pass


def _sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def resolve_session_relation(
    *,
    ledger_path: Path,
    date: str,
    recording_path: str | Path,
    source_sha256: str | None = None,
) -> dict[str, object] | None:
    if not ledger_path.is_file() or ledger_path.is_symlink():
        raise SessionRelationAuthorityError("SESSION_RELATION_LEDGER_INVALID")
    try:
        document = json.loads(ledger_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SessionRelationAuthorityError("SESSION_RELATION_LEDGER_INVALID") from exc
    if not isinstance(document, Mapping) or document.get("schema_version") != SCHEMA_VERSION:
        raise SessionRelationAuthorityError("SESSION_RELATION_LEDGER_SCHEMA_INVALID")
    sessions = document.get("sessions")
    if not isinstance(sessions, list):
        raise SessionRelationAuthorityError("SESSION_RELATION_LEDGER_SESSIONS_INVALID")

    basename = Path(recording_path).name
    normalized_sha = str(source_sha256 or "")
    matches: list[Mapping[str, object]] = []
    for raw in sessions:
        if not isinstance(raw, Mapping) or raw.get("date") != date:
            continue
        basenames = raw.get("recording_basenames")
        hashes = raw.get("source_sha256s", [])
        if not isinstance(basenames, list) or not all(
            isinstance(value, str) and Path(value).name == value for value in basenames
        ):
            raise SessionRelationAuthorityError("SESSION_RELATION_BINDING_INVALID")
        if not isinstance(hashes, list) or not all(isinstance(value, str) for value in hashes):
            raise SessionRelationAuthorityError("SESSION_RELATION_HASH_BINDING_INVALID")
        if basename in basenames or (normalized_sha and normalized_sha in hashes):
            matches.append(raw)
    if not matches:
        return None
    if len(matches) != 1:
        raise SessionRelationAuthorityError("SESSION_RELATION_BINDING_AMBIGUOUS")
    row = matches[0]
    state = str(row.get("state") or "")
    if state not in RELATION_STATES:
        raise SessionRelationAuthorityError("SESSION_RELATION_STATE_INVALID")
    relation_id = str(row.get("relation_id") or "")
    relation_type = str(row.get("relation_type") or "")
    participants = row.get("participants")
    evidence = row.get("evidence")
    if (
        not relation_id
        or not relation_type
        or not isinstance(participants, list)
        or len(participants) < 2
        or not all(isinstance(value, Mapping) for value in participants)
        or not isinstance(evidence, list)
        or not evidence
    ):
        raise SessionRelationAuthorityError("SESSION_RELATION_ROW_INVALID")
    return {
        "schema_version": AUTHORITY_SCHEMA_VERSION,
        "relation_id": relation_id,
        "state": state,
        "relation_type": relation_type,
        "participants": [dict(value) for value in participants],
        "presence_intervals": list(row.get("presence_intervals") or []),
        "evidence": list(evidence),
        "bound_date": date,
        "bound_recording_basename": basename,
        "bound_source_sha256": normalized_sha or None,
        "ledger_sha256": _sha256(ledger_path),
    }

