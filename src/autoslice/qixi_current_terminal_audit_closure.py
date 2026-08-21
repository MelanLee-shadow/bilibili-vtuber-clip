"""Replay Qixi's terminal text audit closure without rerunning providers.

The current-terminal package has one deliberately narrow derivation: a
previously positive final review is rebound from the superseded delivery SRT
to the repository-sealed 54-cue terminal projection.  Everything else is
either copied byte-for-byte from the sealed source evidence or recomputed by
the existing final-surface verifier.  This module owns that boundary so a
caller cannot turn an arbitrary old ``CLEAN`` audit into a new release pass.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any, Mapping

from src.autoslice.boundary_endpoint_binding import bind_final_semantic_endpoint
from src.autoslice.boundary_semantic_review import cue_grid_sha256
from src.autoslice.final_review_contract import (
    FinalReviewContractError,
    validate_final_review_release,
)
from src.autoslice.jingting_chunker import parse_srt_cues
from src.autoslice.producer_boundary_owner_contract import (
    validate_frozen_boundary_owner_contract,
)
from src.autoslice.qixi_terminal_subtitle_projection import (
    TERMINAL_PROJECTION_RELATIVE_PATH,
    TerminalProjectionError,
    validate_projection_assets,
)
from src.autoslice.repository_asset_authority import require_repository_asset_authority


AUTHORITY_RELATIVE_PATH = Path(
    "assets/lidousha/qixi_current_terminal_audit_closure/"
    "auto_113022_354_496.v1.json"
)
SCHEMA = "qixi-current-terminal-audit-closure-authority.v1"
_SHA = "sha256:"


class QixiCurrentTerminalAuditClosureError(ValueError):
    """A terminal audit closure is incomplete, stale, or not replayable."""


def _canonical_sha(value: object) -> str:
    return _SHA + hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _normal_sha(value: object, *, label: str) -> str:
    text = str(value or "").removeprefix(_SHA)
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise QixiCurrentTerminalAuditClosureError(f"{label} is not a sha256")
    return _SHA + text


def _safe_relative(value: object, *, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise QixiCurrentTerminalAuditClosureError(f"{label} path is missing")
    path = Path(value)
    if path.is_absolute() or not path.parts or "." in path.parts or ".." in path.parts:
        raise QixiCurrentTerminalAuditClosureError(f"{label} path is unsafe")
    return path


def _sealed_bytes(repo_root: Path, relative: Path, *, label: str) -> bytes:
    path = repo_root / relative
    cursor = repo_root
    for part in relative.parts:
        cursor /= part
        try:
            metadata = os.lstat(cursor)
        except OSError as exc:
            raise QixiCurrentTerminalAuditClosureError(f"{label} is unavailable") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise QixiCurrentTerminalAuditClosureError(f"{label} contains a symlink")
    if not path.is_file():
        raise QixiCurrentTerminalAuditClosureError(f"{label} is not a regular file")
    try:
        payload = path.read_bytes()
        require_repository_asset_authority(
            repo_root=repo_root, relative_path=relative, observed_bytes=payload
        )
    except (OSError, ValueError) as exc:
        raise QixiCurrentTerminalAuditClosureError(f"{label} is not repository sealed") from exc
    return payload


def load_terminal_audit_closure(repo_root: Path) -> dict[str, Any]:
    """Load and validate the acyclic terminal audit-closure authority."""

    payload = _sealed_bytes(repo_root, AUTHORITY_RELATIVE_PATH, label="Qixi terminal audit closure")
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise QixiCurrentTerminalAuditClosureError("Qixi terminal audit closure is unreadable") from exc
    if not isinstance(value, Mapping):
        raise QixiCurrentTerminalAuditClosureError("Qixi terminal audit closure is invalid")
    authority = dict(value)
    claimed = _normal_sha(authority.pop("authority_sha256", None), label="terminal audit closure")
    required = {
        "schema_version", "candidate_id", "status", "terminal_projection",
        "historical_reviewed_srt_sha256", "source_evidence",
    }
    if (
        set(authority) != required
        or authority.get("schema_version") != SCHEMA
        or authority.get("candidate_id") != "auto_113022_354_496"
        or authority.get("status") != "SEALED_TERMINAL_REBIND"
        or _canonical_sha(authority) != claimed
    ):
        raise QixiCurrentTerminalAuditClosureError("Qixi terminal audit closure schema/hash drifts")
    projection = authority.get("terminal_projection")
    evidence = authority.get("source_evidence")
    if not isinstance(projection, Mapping) or not isinstance(evidence, Mapping) or (
        set(projection) != {"relative_path", "authority_sha256", "terminal_srt_sha256"}
        or _safe_relative(projection.get("relative_path"), label="terminal projection")
        != TERMINAL_PROJECTION_RELATIVE_PATH
        or set(evidence) != {"chat_authority_sha256", "boundary_audit_sha256", "review_flags_sha256"}
    ):
        raise QixiCurrentTerminalAuditClosureError("Qixi terminal audit closure bindings are invalid")
    authority["authority_sha256"] = claimed
    return authority


def _validate_bound_inputs(
    *,
    authority: Mapping[str, Any],
    repo_root: Path,
    source_chat_sha256: str,
    source_boundary_sha256: str,
    source_flags_sha256: str,
    subtitle_text: str,
) -> tuple[dict[str, Any], list[Any]]:
    projection = authority["terminal_projection"]
    assert isinstance(projection, Mapping)
    try:
        terminal = validate_projection_assets(repo_root)
    except TerminalProjectionError as exc:
        raise QixiCurrentTerminalAuditClosureError(str(exc)) from exc
    if (
        _normal_sha(projection.get("authority_sha256"), label="terminal projection")
        != _normal_sha(terminal.get("authority_sha256"), label="replayed terminal projection")
        or _normal_sha(projection.get("terminal_srt_sha256"), label="terminal SRT")
        != _normal_sha(terminal["terminal_srt"].get("sha256"), label="replayed terminal SRT")
        or _SHA + hashlib.sha256(subtitle_text.encode()).hexdigest()
        != _normal_sha(projection.get("terminal_srt_sha256"), label="runtime terminal SRT")
    ):
        raise QixiCurrentTerminalAuditClosureError("Qixi terminal audit closure subtitle binding drifts")
    evidence = authority["source_evidence"]
    assert isinstance(evidence, Mapping)
    if (
        _normal_sha(evidence.get("chat_authority_sha256"), label="source chat")
        != _normal_sha(source_chat_sha256, label="runtime source chat")
        or _normal_sha(evidence.get("boundary_audit_sha256"), label="source boundary")
        != _normal_sha(source_boundary_sha256, label="runtime source boundary")
        or _normal_sha(evidence.get("review_flags_sha256"), label="source review flags")
        != _normal_sha(source_flags_sha256, label="runtime source review flags")
    ):
        raise QixiCurrentTerminalAuditClosureError("Qixi terminal audit closure source evidence drifts")
    try:
        cues = parse_srt_cues(subtitle_text)
    except ValueError as exc:
        raise QixiCurrentTerminalAuditClosureError("Qixi terminal subtitle is invalid") from exc
    if len(cues) != 54:
        raise QixiCurrentTerminalAuditClosureError("Qixi terminal subtitle cue count drifts")
    return terminal, cues


def _terminal_final_review(
    *,
    source_audit: object,
    authority: Mapping[str, Any],
    terminal: Mapping[str, Any],
    cues: list[Any],
) -> dict[str, Any]:
    if not isinstance(source_audit, Mapping):
        raise QixiCurrentTerminalAuditClosureError("Qixi source final review is missing")
    historical_sha = _normal_sha(
        authority.get("historical_reviewed_srt_sha256"), label="historical reviewed SRT"
    )
    try:
        validate_final_review_release(source_audit, expected_srt_sha256=historical_sha)
    except FinalReviewContractError as exc:
        raise QixiCurrentTerminalAuditClosureError("Qixi historical final review is invalid") from exc
    review = copy.deepcopy(dict(source_audit))
    boundary = review.get("boundary_semantic_review")
    terminal_boundary = terminal.get("terminal_boundary")
    if not isinstance(boundary, Mapping) or not isinstance(terminal_boundary, Mapping):
        raise QixiCurrentTerminalAuditClosureError("Qixi terminal boundary evidence is missing")
    expected_grid = cue_grid_sha256(cues)
    rebound_boundary = copy.deepcopy(dict(boundary))
    rebound_boundary["cue_grid_sha256"] = expected_grid
    rebound, reasons = bind_final_semantic_endpoint(
        semantic_review=rebound_boundary,
        cues=cues,
        closure_cue=cues[-1],
        snapped_end_ms=int(terminal_boundary["final_closure_end_ms"]),
        final_start_ms=int(terminal_boundary["final_start_ms"]),
        final_end_ms=int(terminal_boundary["final_end_ms"]),
    )
    if reasons or rebound.get("cue_grid_sha256") != expected_grid:
        raise QixiCurrentTerminalAuditClosureError("Qixi terminal final-review endpoint cannot rebind")
    terminal_sha = _normal_sha(
        authority["terminal_projection"].get("terminal_srt_sha256"), label="terminal reviewed SRT"
    )
    review["reviewed_srt_sha256"] = terminal_sha
    review["boundary_semantic_review"] = rebound
    try:
        return validate_final_review_release(review, expected_srt_sha256=terminal_sha)
    except FinalReviewContractError as exc:
        raise QixiCurrentTerminalAuditClosureError("Qixi terminal final review is invalid") from exc


def build_terminal_audit_closure(
    *,
    repo_root: Path,
    source_chat: Mapping[str, Any],
    source_chat_sha256: str,
    source_boundary: Mapping[str, Any],
    source_boundary_sha256: str,
    source_review_flags: Mapping[str, Any],
    source_flags_sha256: str,
    record_boundary: Mapping[str, Any],
    subtitle_text: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return re-verifiable chat and boundary closures for the terminal SRT.

    This function intentionally does *not* validate final text owners itself:
    the caller must invoke ``verify_chat_authority_final_surfaces`` after it
    binds final package paths.  That verifier recomputes all owner receipts
    from the terminal SRT rather than trusting carried status fields.
    """

    authority = load_terminal_audit_closure(repo_root)
    terminal, cues = _validate_bound_inputs(
        authority=authority,
        repo_root=repo_root,
        source_chat_sha256=source_chat_sha256,
        source_boundary_sha256=source_boundary_sha256,
        source_flags_sha256=source_flags_sha256,
        subtitle_text=subtitle_text,
    )
    # The sealed source review-flags artifact is the final-review audit itself,
    # not a wrapper containing one.  Compare its complete historical object
    # with the chat carry before deriving the terminal review.
    flags_audit = source_review_flags
    chat_audit = source_chat.get("final_review_audit")
    if not isinstance(flags_audit, Mapping) or flags_audit != chat_audit:
        raise QixiCurrentTerminalAuditClosureError("Qixi source final-review evidence differs")
    final_review = _terminal_final_review(
        source_audit=flags_audit, authority=authority, terminal=terminal, cues=cues
    )
    frozen = source_chat.get("frozen_boundary_owner_contract")
    try:
        frozen_checked = validate_frozen_boundary_owner_contract(frozen)
    except (KeyError, RuntimeError, TypeError, ValueError) as exc:
        raise QixiCurrentTerminalAuditClosureError("Qixi frozen boundary owner evidence is invalid") from exc
    source_review = source_boundary.get("boundary_semantic_review")
    record_source_review = record_boundary.get("boundary_semantic_review")
    if not isinstance(source_review, Mapping) or source_review != record_source_review:
        raise QixiCurrentTerminalAuditClosureError("Qixi source boundary evidence differs")
    current_boundary = copy.deepcopy(dict(record_boundary))
    owners = copy.deepcopy(frozen_checked["owners"])
    owner_verification = current_boundary.get("required_boundary_owner_verification")
    coverage = current_boundary.get("delivery_coverage_verification")
    if not (
        isinstance(owner_verification, Mapping)
        and owner_verification.get("status") == "PASS"
        and owner_verification.get("failures") == []
        and isinstance(coverage, Mapping)
        and coverage.get("status") == "PASS"
        and coverage.get("failure") is None
    ):
        raise QixiCurrentTerminalAuditClosureError("Qixi source boundary owner/coverage evidence is invalid")
    current_boundary.update(
        {
            "final_delivery_boundary_semantic_review": copy.deepcopy(
                final_review["boundary_semantic_review"]
            ),
            "frozen_required_boundary_owner_count": len(owners),
            "frozen_required_boundary_owners": owners,
        }
    )
    chat = copy.deepcopy(dict(source_chat))
    chat["final_review_audit"] = final_review
    return chat, current_boundary


def rebind_terminal_story_transcript(
    story_contract: Mapping[str, Any], *, final_transcript: str
) -> dict[str, Any]:
    """Advance only the subtitle-derived story facts to the terminal text."""

    story = dict(story_contract)
    old_sha = _normal_sha(story.get("transcript_sha256"), label="source story transcript")
    audits = story.get("input_audits")
    if not isinstance(audits, list):
        raise QixiCurrentTerminalAuditClosureError("source story subtitle audit is missing")
    subtitle_rows = [
        row for row in audits if isinstance(row, Mapping) and row.get("artifact_kind") == "subtitle"
    ]
    if len(subtitle_rows) != 1 or _normal_sha(
        subtitle_rows[0].get("text_sha256"), label="source story subtitle audit"
    ) != old_sha:
        raise QixiCurrentTerminalAuditClosureError("source story subtitle audit drifts")
    rebound_sha = _SHA + hashlib.sha256(final_transcript.encode("utf-8")).hexdigest()
    rebound_audits = [dict(row) if isinstance(row, Mapping) else row for row in audits]
    for row in rebound_audits:
        if isinstance(row, dict) and row.get("artifact_kind") == "subtitle":
            row["text_sha256"] = rebound_sha
    story["transcript_sha256"] = rebound_sha
    story["input_audits"] = rebound_audits
    return story
