"""Sealed, no-provider preservation of Qixi's source-fact surfaces.

This is deliberately a single-candidate bridge.  The historical CPA receipt is
preserved as diagnostic evidence and never relabelled or replayed as if its
old transcript hash were current.  A new receipt is instead derived from the
sealed terminal subtitle projection and a closed set of direct cue evidence.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from src.autoslice.jingting_chunker import parse_srt_cues
from src.autoslice.qixi_terminal_subtitle_projection import validate_projection_assets
from src.autoslice.repository_asset_authority import require_repository_asset_authority


CANDIDATE_ID = "auto_113022_354_496"
AUTHORITY_RELATIVE_PATH = Path(
    "assets/lidousha/qixi_source_fact_terminal_preservation/"
    "auto_113022_354_496.v1.json"
)
FINALIZATION_AUTHORITY_RELATIVE_PATH = Path(
    "assets/lidousha/qixi_corrected_package_finalization_authority.v1.json"
)
SCHEMA = "qixi-source-fact-terminal-preservation-authority.v1"
CONSUMPTION_SCHEMA = "qixi-source-fact-terminal-preservation-consumption.v1"
SOURCE_FACT_SCHEMA = "lidousha-source-fact-review.v1"
DECISION = "OPERATOR_TERMINAL_TEXT_PRESERVATION"
_SHA = "sha256:"


class QixiSourceFactTerminalPreservationError(ValueError):
    """The sealed Qixi source-fact preservation proof cannot be replayed."""


def _canonical_sha(value: object) -> str:
    return _SHA + hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _text_sha(value: str) -> str:
    return _SHA + hashlib.sha256(value.encode()).hexdigest()


def _normal_sha(value: object, *, label: str) -> str:
    result = str(value or "").removeprefix(_SHA)
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        raise QixiSourceFactTerminalPreservationError(f"{label} must be a sha256")
    return _SHA + result


def _load_authority(repo_root: Path) -> dict[str, Any]:
    path = repo_root / AUTHORITY_RELATIVE_PATH
    try:
        payload = path.read_bytes()
        require_repository_asset_authority(
            repo_root=repo_root, relative_path=AUTHORITY_RELATIVE_PATH, observed_bytes=payload
        )
        document = json.loads(payload.decode("utf-8"))
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise QixiSourceFactTerminalPreservationError(
            "Qixi source-fact preservation authority is unavailable or unsealed"
        ) from exc
    if not isinstance(document, Mapping):
        raise QixiSourceFactTerminalPreservationError("Qixi source-fact preservation authority is invalid")
    authority = dict(document)
    claimed = _normal_sha(authority.pop("authority_sha256", None), label="preservation authority")
    if _canonical_sha(authority) != claimed:
        raise QixiSourceFactTerminalPreservationError("Qixi source-fact preservation authority hash drifts")
    authority["authority_sha256"] = claimed
    return authority


def _bound_finalization_authority_sha(repo_root: Path) -> str:
    """Replay the acyclic finalization-authority -> preservation-asset edge."""

    path = repo_root / FINALIZATION_AUTHORITY_RELATIVE_PATH
    try:
        payload = path.read_bytes()
        require_repository_asset_authority(
            repo_root=repo_root,
            relative_path=FINALIZATION_AUTHORITY_RELATIVE_PATH,
            observed_bytes=payload,
        )
        document = json.loads(payload.decode("utf-8"))
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise QixiSourceFactTerminalPreservationError(
            "Qixi finalization authority is unavailable or unsealed"
        ) from exc
    if not isinstance(document, Mapping):
        raise QixiSourceFactTerminalPreservationError("Qixi finalization authority is invalid")
    body = dict(document)
    claimed = _normal_sha(body.pop("authority_sha256", None), label="finalization authority")
    if _canonical_sha(body) != claimed:
        raise QixiSourceFactTerminalPreservationError("Qixi finalization authority hash drifts")
    descriptor = body.get("source_fact_terminal_preservation")
    preservation = _load_authority(repo_root)
    if (
        not isinstance(descriptor, Mapping)
        or set(descriptor) != {"relative_path", "authority_sha256"}
        or descriptor.get("relative_path") != AUTHORITY_RELATIVE_PATH.as_posix()
        or _normal_sha(
            descriptor.get("authority_sha256"), label="Qixi preservation descriptor"
        )
        != preservation["authority_sha256"]
    ):
        raise QixiSourceFactTerminalPreservationError(
            "Qixi finalization authority does not bind terminal preservation"
        )
    return claimed


def validate_terminal_preservation_finalization_authority(
    *, repo_root: Path, authority_sha256: str
) -> None:
    """Require the current sealed finalization authority to name this asset."""

    if _normal_sha(authority_sha256, label="runtime finalization authority") != (
        _bound_finalization_authority_sha(repo_root)
    ):
        raise QixiSourceFactTerminalPreservationError(
            "Qixi finalization authority binding drifts"
        )


def _transcript(subtitle_text: str) -> tuple[list[Any], str]:
    try:
        cues = parse_srt_cues(subtitle_text)
    except ValueError as exc:
        raise QixiSourceFactTerminalPreservationError("Qixi terminal subtitle is invalid") from exc
    value = "\n".join(cue.text.strip() for cue in cues if cue.text.strip())
    if not value:
        raise QixiSourceFactTerminalPreservationError("Qixi terminal transcript is empty")
    return cues, value


def _cue_rows_match(cues: list[Any], rows: object, *, label: str) -> None:
    if not isinstance(rows, list) or not rows:
        raise QixiSourceFactTerminalPreservationError(f"Qixi {label} cue proof is invalid")
    for row in rows:
        if not isinstance(row, Mapping) or set(row) != {"cue", "start_ms", "end_ms", "text"}:
            raise QixiSourceFactTerminalPreservationError(f"Qixi {label} cue proof schema drifts")
        index = row["cue"]
        if isinstance(index, bool) or not isinstance(index, int) or not 1 <= index <= len(cues):
            raise QixiSourceFactTerminalPreservationError(f"Qixi {label} cue index drifts")
        cue = cues[index - 1]
        if (
            cue.start_ms != row["start_ms"]
            or cue.end_ms != row["end_ms"]
            or cue.text != row["text"]
        ):
            raise QixiSourceFactTerminalPreservationError(f"Qixi {label} cue evidence drifts")


def _validate_authority(
    authority: Mapping[str, Any],
    *,
    repo_root: Path,
    subtitle_text: str,
    final_transcript: str,
    title: str,
    selection_hook: str,
    clip_context_prompt: str,
    selection_scorecard: object,
    speaker_evidence: object,
    correction_sha256: str,
    ass_repair_receipt_sha256: str,
    recovery_publication_authority: object,
    finalization_authority_sha256: str,
) -> dict[str, Any]:
    expected = {
        "schema_version", "candidate_id", "terminal_projection",
        "release_bindings", "surfaces", "historical_provider_receipt",
        "direct_terminal_evidence", "authority_sha256",
    }
    if set(authority) != expected or authority.get("schema_version") != SCHEMA or authority.get("candidate_id") != CANDIDATE_ID:
        raise QixiSourceFactTerminalPreservationError("Qixi source-fact preservation authority schema drifts")
    projection = authority["terminal_projection"]
    release = authority["release_bindings"]
    surfaces = authority["surfaces"]
    old = authority["historical_provider_receipt"]
    evidence = authority["direct_terminal_evidence"]
    if not all(isinstance(item, Mapping) for item in (projection, release, surfaces, old, evidence)):
        raise QixiSourceFactTerminalPreservationError("Qixi source-fact preservation authority bindings are invalid")
    validate_terminal_preservation_finalization_authority(
        repo_root=repo_root, authority_sha256=finalization_authority_sha256
    )
    if (
        set(projection) != {"relative_path", "authority_sha256", "terminal_srt_sha256", "terminal_transcript_sha256"}
        or projection.get("relative_path")
        != "assets/lidousha/qixi_terminal_subtitle_projection/auto_113022_354_496.terminal-projection.v1.json"
    ):
        raise QixiSourceFactTerminalPreservationError("Qixi terminal projection binding schema drifts")
    replayed_projection = validate_projection_assets(repo_root)
    if (
        _normal_sha(projection.get("authority_sha256"), label="Qixi terminal projection")
        != _normal_sha(replayed_projection.get("authority_sha256"), label="replayed terminal projection")
        or _normal_sha(projection.get("terminal_srt_sha256"), label="Qixi terminal SRT")
        != _normal_sha(replayed_projection["terminal_srt"].get("sha256"), label="replayed terminal SRT")
    ):
        raise QixiSourceFactTerminalPreservationError("Qixi terminal projection binding drifts")
    cues, derived_transcript = _transcript(subtitle_text)
    if (
        _text_sha(derived_transcript)
        != _normal_sha(projection.get("terminal_transcript_sha256"), label="Qixi terminal transcript")
        or final_transcript != derived_transcript
    ):
        raise QixiSourceFactTerminalPreservationError("Qixi terminal transcript drifts")
    if (
        _SHA + hashlib.sha256(subtitle_text.encode()).hexdigest()
        != _normal_sha(projection.get("terminal_srt_sha256"), label="Qixi terminal SRT")
        or len(cues) != 54
    ):
        raise QixiSourceFactTerminalPreservationError("Qixi terminal subtitle bytes drift")
    if (
        set(release) != {"correction_sha256", "ass_repair_receipt_sha256", "recovery_publication_authority_sha256"}
        or _normal_sha(release.get("correction_sha256"), label="Qixi correction")
        != _normal_sha(correction_sha256, label="runtime correction")
        or _normal_sha(release.get("ass_repair_receipt_sha256"), label="Qixi ASS receipt")
        != _normal_sha(ass_repair_receipt_sha256, label="runtime ASS receipt")
        or not isinstance(recovery_publication_authority, Mapping)
        or _normal_sha(release.get("recovery_publication_authority_sha256"), label="Qixi same-BV authority")
        != _normal_sha(recovery_publication_authority.get("authority_sha256"), label="runtime same-BV authority")
    ):
        raise QixiSourceFactTerminalPreservationError("Qixi release binding drifts")
    if (
        set(surfaces) != {
            "title", "title_sha256", "selection_hook", "selection_hook_sha256",
            "clip_context_prompt_sha256", "selection_scorecard_sha256", "speaker_evidence",
            "speaker_evidence_sha256",
        }
        or title != surfaces.get("title")
        or selection_hook != surfaces.get("selection_hook")
        or _text_sha(title) != _normal_sha(surfaces.get("title_sha256"), label="Qixi title")
        or _text_sha(selection_hook)
        != _normal_sha(surfaces.get("selection_hook_sha256"), label="Qixi selection hook")
        or _text_sha(clip_context_prompt)
        != _normal_sha(surfaces.get("clip_context_prompt_sha256"), label="Qixi clip context")
        or _canonical_sha(selection_scorecard)
        != _normal_sha(surfaces.get("selection_scorecard_sha256"), label="Qixi scorecard")
        or speaker_evidence != surfaces.get("speaker_evidence")
        or _canonical_sha(speaker_evidence)
        != _normal_sha(surfaces.get("speaker_evidence_sha256"), label="Qixi speaker evidence")
    ):
        raise QixiSourceFactTerminalPreservationError("Qixi source-fact surface binding drifts")
    old_body = dict(old)
    old_receipt_sha = _normal_sha(old_body.pop("receipt_sha256", None), label="historical provider receipt")
    if _canonical_sha(old_body) != old_receipt_sha or old.get("schema_version") != SOURCE_FACT_SCHEMA or old.get("status") != "PASS" or old.get("decision") != "REPAIRED":
        raise QixiSourceFactTerminalPreservationError("Qixi historical provider receipt drifts")
    if old.get("final_title") != title or old.get("final_selection_hook") != selection_hook:
        raise QixiSourceFactTerminalPreservationError("Qixi historical provider surfaces drift")
    if set(evidence) != {"cited_cues", "structured_chat_line", "balance_cue", "reviewer_balance_authority", "cue21_was_not_historical_direct_evidence"}:
        raise QixiSourceFactTerminalPreservationError("Qixi direct terminal evidence schema drifts")
    _cue_rows_match(cues, evidence.get("cited_cues"), label="historical direct")
    balance = evidence.get("balance_cue")
    if not isinstance(balance, Mapping):
        raise QixiSourceFactTerminalPreservationError("Qixi balance cue is invalid")
    _cue_rows_match(cues, [balance], label="balance")
    if balance.get("cue") != 21 or balance.get("text") != "非常 balance いいや" or not isinstance(evidence.get("reviewer_balance_authority"), str) or evidence.get("cue21_was_not_historical_direct_evidence") is not True:
        raise QixiSourceFactTerminalPreservationError("Qixi balance authority drifts")
    passes = old.get("passes")
    if not isinstance(passes, list) or len(passes) != 2 or not isinstance(passes[0], Mapping):
        raise QixiSourceFactTerminalPreservationError("Qixi historical provider passes drift")
    changes = passes[0].get("changed_surfaces")
    if not isinstance(changes, list) or len(changes) != 1 or not isinstance(changes[0], Mapping):
        raise QixiSourceFactTerminalPreservationError("Qixi historical provider evidence drifts")
    cited = changes[0].get("evidence")
    expected_cited = [
        cues[14].text,
        cues[15].text,
        cues[17].text + "\n" + cues[18].text,
        evidence["structured_chat_line"],
    ]
    if cited != expected_cited or balance["text"] in cited:
        raise QixiSourceFactTerminalPreservationError("Qixi historical direct evidence is not preserved")
    return {
        "schema_version": CONSUMPTION_SCHEMA,
        "status": "VALID",
        "candidate_id": CANDIDATE_ID,
        "authority_sha256": authority["authority_sha256"],
        "finalization_authority_sha256": _normal_sha(finalization_authority_sha256, label="runtime finalization authority"),
        "terminal_projection_authority_sha256": _normal_sha(projection["authority_sha256"], label="terminal projection"),
        "terminal_srt_sha256": _normal_sha(projection["terminal_srt_sha256"], label="terminal SRT"),
        "terminal_transcript_sha256": _normal_sha(projection["terminal_transcript_sha256"], label="terminal transcript"),
        "historical_provider_receipt_sha256": old_receipt_sha,
        "correction_sha256": _normal_sha(correction_sha256, label="runtime correction"),
        "ass_repair_receipt_sha256": _normal_sha(ass_repair_receipt_sha256, label="runtime ASS receipt"),
        "recovery_publication_authority_sha256": _normal_sha(recovery_publication_authority["authority_sha256"], label="runtime same-BV authority"),
    }


def build_terminal_preservation_review(
    *,
    repo_root: Path,
    subtitle_text: str,
    final_transcript: str,
    title: str,
    selection_hook: str,
    clip_context_prompt: str,
    selection_scorecard: object,
    speaker_evidence: object,
    correction_sha256: str,
    ass_repair_receipt_sha256: str,
    recovery_publication_authority: object,
    finalization_authority_sha256: str,
) -> dict[str, Any]:
    """Derive the distinct Qixi preservation receipt without provider calls."""

    authority = _load_authority(repo_root)
    consumption = _validate_authority(
        authority,
        repo_root=repo_root,
        subtitle_text=subtitle_text,
        final_transcript=final_transcript,
        title=title,
        selection_hook=selection_hook,
        clip_context_prompt=clip_context_prompt,
        selection_scorecard=selection_scorecard,
        speaker_evidence=speaker_evidence,
        correction_sha256=correction_sha256,
        ass_repair_receipt_sha256=ass_repair_receipt_sha256,
        recovery_publication_authority=recovery_publication_authority,
        finalization_authority_sha256=finalization_authority_sha256,
    )
    historical = copy.deepcopy(authority["historical_provider_receipt"])
    result: dict[str, Any] = {
        "schema_version": SOURCE_FACT_SCHEMA,
        "status": "PASS",
        "decision": DECISION,
        "original_selection_hook": historical["original_selection_hook"],
        "original_title": historical["original_title"],
        "final_selection_hook": selection_hook,
        "final_title": title,
        "historical_provider_receipt": historical,
        "terminal_text_preservation": consumption,
    }
    result["receipt_sha256"] = _canonical_sha(result)
    return result


def validate_terminal_preservation_review(
    review: object,
    **kwargs: Any,
) -> bool:
    """Rebuild the closed receipt; no generic KEEP/REPAIRED path accepts it."""

    if not isinstance(review, Mapping):
        return False
    try:
        expected = build_terminal_preservation_review(**kwargs)
    except (OSError, TypeError, ValueError, KeyError):
        return False
    return dict(review) == expected


def validate_terminal_preservation_source_fact_review(
    review: object,
    *,
    repo_root: Path,
    final_reviewed_srt_path: Path,
    final_transcript: str,
    title: str,
    selection_hook: str,
    clip_context_prompt: str,
    selection_scorecard: object,
    candidate_id: str | None,
    speaker_evidence: object,
) -> bool:
    """Replay the distinct receipt from a package source-fact validation call."""

    if not isinstance(review, Mapping) or candidate_id != CANDIDATE_ID:
        return False
    consumption = review.get("terminal_text_preservation")
    if not isinstance(consumption, Mapping):
        return False
    try:
        return validate_terminal_preservation_review(
            review,
            repo_root=repo_root,
            subtitle_text=final_reviewed_srt_path.read_text(encoding="utf-8"),
            final_transcript=final_transcript,
            title=title,
            selection_hook=selection_hook,
            clip_context_prompt=clip_context_prompt,
            selection_scorecard=selection_scorecard,
            speaker_evidence=speaker_evidence,
            correction_sha256=str(consumption.get("correction_sha256") or ""),
            ass_repair_receipt_sha256=str(
                consumption.get("ass_repair_receipt_sha256") or ""
            ),
            recovery_publication_authority={
                "authority_sha256": str(
                    consumption.get("recovery_publication_authority_sha256") or ""
                )
            },
            finalization_authority_sha256=str(
                consumption.get("finalization_authority_sha256") or ""
            ),
        )
    except OSError:
        return False


def validate_terminal_preservation_finalization_binding(
    *, review: object, finalization_receipt: Mapping[str, object], **kwargs: Any
) -> bool:
    """Require the same typed finalization authority that materialized the package."""

    if not isinstance(finalization_receipt, Mapping):
        return False
    if not validate_terminal_preservation_review(review, **kwargs):
        return False
    consumption = review.get("terminal_text_preservation") if isinstance(review, Mapping) else None
    return bool(
        isinstance(consumption, Mapping)
        and finalization_receipt.get("candidate_id") == CANDIDATE_ID
        and _normal_sha(finalization_receipt.get("authority_sha256"), label="finalization receipt")
        == consumption.get("finalization_authority_sha256")
    )
