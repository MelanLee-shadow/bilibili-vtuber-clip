"""Sealed no-provider source-fact receipt refresh for one public-title repair.

The normal CPA receipt is a record of a provider adjudication.  It must not be
edited to make a later, human-authorized public title look as if CPA emitted
it.  This module instead retains the exact historical receipt and derives a
separate, explicitly named receipt only when a repository-sealed authority
binds both the old receipt and every current source-fact input.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from src.autoslice.candidate_public_text_surface_authority import (
    CandidatePublicTextSurfaceAuthorityError,
    build_public_text_source_fact_context,
    load_candidate_public_text_surface_authority,
)
from src.autoslice.repository_asset_authority import (
    RepositoryAssetAuthorityError,
    require_repository_asset_authority,
)


SOURCE_FACT_SCHEMA = "lidousha-source-fact-review.v1"
AUTHORITY_SCHEMA = "candidate-public-text-source-fact-refresh-authority.v1"
CONSUMPTION_SCHEMA = "candidate-public-text-source-fact-refresh-consumption.v1"
DECISION = "CANDIDATE_PUBLIC_TEXT_SOURCE_FACT_REFRESH"
CANDIDATE_ID = "auto_173005_934_1166"
AUTHORITY_RELATIVE_PATH = Path(
    "assets/lidousha/candidate_source_fact_refresh_authorities/"
    "auto_173005_934_1166.v1.json"
)
_SHA = "sha256:"


class CandidateSourceFactRefreshError(ValueError):
    """The candidate-only historical receipt rebind cannot be replayed."""


def _canonical_sha(value: object) -> str:
    return _SHA + hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _text_sha(value: str) -> str:
    return _SHA + hashlib.sha256(value.encode()).hexdigest()


def _file_sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return _SHA + digest.hexdigest()


def _regular_file_no_symlink(path: Path) -> bool:
    absolute = path.absolute()
    cursor = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        cursor = cursor / part
        if cursor.is_symlink():
            return False
    return path.is_file() and not path.is_symlink()


def _sha(value: object, *, label: str) -> str:
    result = str(value or "")
    if not result.startswith(_SHA):
        result = _SHA + result
    if len(result) != len(_SHA) + 64 or any(char not in "0123456789abcdef" for char in result[7:]):
        raise CandidateSourceFactRefreshError(f"{label} must be a sha256")
    return result


def _object(value: object, *, fields: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise CandidateSourceFactRefreshError(f"{label} schema is invalid")
    return dict(value)


def _load_authority(repo_root: Path) -> dict[str, Any]:
    path = repo_root / AUTHORITY_RELATIVE_PATH
    try:
        raw = path.read_bytes()
        require_repository_asset_authority(
            repo_root=repo_root,
            relative_path=AUTHORITY_RELATIVE_PATH,
            observed_bytes=raw,
        )
        loaded = json.loads(raw.decode("utf-8"))
    except (
        OSError,
        UnicodeError,
        ValueError,
        json.JSONDecodeError,
        RepositoryAssetAuthorityError,
    ) as exc:
        raise CandidateSourceFactRefreshError(
            "candidate source-fact refresh authority is unavailable or unsealed"
        ) from exc
    expected = {
        "schema_version",
        "candidate_id",
        "recording_date",
        "historical_provider_receipt",
        "public_text_authority",
        "runtime_bindings",
        "preimages",
        "scope",
        "authority_sha256",
    }
    authority = _object(loaded, fields=expected, label="refresh authority")
    claimed = _sha(authority.pop("authority_sha256"), label="refresh authority")
    if _canonical_sha(authority) != claimed:
        raise CandidateSourceFactRefreshError("candidate source-fact refresh authority hash drifts")
    authority["authority_sha256"] = claimed
    if (
        authority["schema_version"] != AUTHORITY_SCHEMA
        or authority["candidate_id"] != CANDIDATE_ID
        or authority["recording_date"] != "2026-08-11"
    ):
        raise CandidateSourceFactRefreshError("candidate source-fact refresh authority identity drifts")
    return authority


def _validate_authority(
    authority: Mapping[str, Any],
    *,
    repo_root: Path,
    historical_provider_receipt: Mapping[str, object],
    candidate_id: str,
    selection_hook: str,
    title: str,
    final_transcript: str,
    clip_context_prompt: str,
    selection_scorecard: object,
    story_contract: Mapping[str, object],
    final_reviewed_srt_path: Path,
    speaker_evidence: object,
) -> dict[str, Any]:
    historical = _object(
        authority.get("historical_provider_receipt"),
        fields={
            "receipt_sha256",
            "original_title",
            "final_title",
            "original_selection_hook",
            "final_selection_hook",
            "status",
            "decision",
        },
        label="historical provider receipt binding",
    )
    public = _object(
        authority.get("public_text_authority"),
        fields={"relative_path", "authority_sha256"},
        label="public text authority binding",
    )
    runtime = _object(
        authority.get("runtime_bindings"),
        fields={
            "selection_hook",
            "selection_hook_sha256",
            "title",
            "title_sha256",
            "final_reviewed_srt_sha256",
            "final_transcript_sha256",
            "clip_context_prompt_sha256",
            "selection_scorecard_sha256",
            "speaker_evidence_sha256",
            "entity_context_sha256",
            "story_contract_sha256",
        },
        label="runtime bindings",
    )
    preimages = _object(
        authority.get("preimages"),
        fields={"state_record_canonical_sha256", "state_file", "documents"},
        label="preimages",
    )
    documents = preimages["documents"]
    if (
        _sha(preimages["state_record_canonical_sha256"], label="state record preimage")
        != "sha256:9c712111a12183b6d664fe55a87c62be5e401f6da7c4e6acf181e4f918d20043"
        or not isinstance(documents, list)
        or len(documents) != 3
        or any(
            not isinstance(row, Mapping) or set(row) != {"path", "sha256", "bytes", "uid", "gid", "mode", "post_mode", "type"}
            or not isinstance(row.get("path"), str)
            or not str(row["path"]).startswith("/opt/bilive/autoslice/")
            or _sha(row.get("sha256"), label="document preimage") != row.get("sha256")
            or not isinstance(row.get("bytes"), int) or row["bytes"] < 0
            or not isinstance(row.get("uid"), int) or row["uid"] < 0
            or not isinstance(row.get("gid"), int) or row["gid"] < 0
            or not isinstance(row.get("mode"), int) or row["mode"] < 0 or row["mode"] > 0o777
            or row.get("post_mode") != row.get("mode")
            or row.get("type") != "regular"
            for row in documents
        )
    ):
        raise CandidateSourceFactRefreshError("candidate source-fact refresh preimages drift")
    state_file = _object(
        preimages["state_file"],
        fields={"path", "sha256", "bytes", "uid", "gid", "mode", "post_mode", "type"},
        label="state file preimage",
    )
    if (
        state_file["path"] != "/opt/bilive/autoslice/state/2026-08-11.json"
        or _sha(state_file["sha256"], label="state file preimage") != state_file["sha256"]
        or not isinstance(state_file["bytes"], int) or state_file["bytes"] < 0
        or not isinstance(state_file["uid"], int) or state_file["uid"] < 0
        or not isinstance(state_file["gid"], int) or state_file["gid"] < 0
        or not isinstance(state_file["mode"], int) or state_file["mode"] < 0 or state_file["mode"] > 0o777
        or state_file["post_mode"] != state_file["mode"]
        or state_file["type"] != "regular"
    ):
        raise CandidateSourceFactRefreshError("candidate source-fact state preimage drifts")
    scope = _object(
        authority.get("scope"),
        fields={
            "candidate_scoped",
            "provider_call_required",
            "subtitle_text_mutation_authorized",
            "speaker_label_mutation_authorized",
            "upload_authorized",
        },
        label="refresh scope",
    )
    if scope != {
        "candidate_scoped": True,
        "provider_call_required": False,
        "subtitle_text_mutation_authorized": False,
        "speaker_label_mutation_authorized": False,
        "upload_authorized": False,
    }:
        raise CandidateSourceFactRefreshError("candidate source-fact refresh scope drifts")
    if candidate_id != CANDIDATE_ID or not _regular_file_no_symlink(final_reviewed_srt_path):
        raise CandidateSourceFactRefreshError("candidate source-fact refresh candidate drifts")
    historical_body = dict(historical_provider_receipt)
    receipt_sha = _sha(historical_body.pop("receipt_sha256", None), label="historical receipt")
    if (
        _canonical_sha(historical_body) != receipt_sha
        or receipt_sha != _sha(historical["receipt_sha256"], label="sealed historical receipt")
        or historical_provider_receipt.get("schema_version") != SOURCE_FACT_SCHEMA
        or historical_provider_receipt.get("status") != historical["status"]
        or historical_provider_receipt.get("status") != "PASS"
        or historical_provider_receipt.get("decision") != historical["decision"]
        or historical_provider_receipt.get("decision") != "REPAIRED"
        or any(
            historical_provider_receipt.get(field) != historical[field]
            for field in (
                "original_title",
                "final_title",
                "original_selection_hook",
                "final_selection_hook",
            )
        )
    ):
        raise CandidateSourceFactRefreshError("historical source-fact receipt drifts")
    try:
        public_authority = load_candidate_public_text_surface_authority(candidate_id, root=repo_root)
    except CandidatePublicTextSurfaceAuthorityError as exc:
        raise CandidateSourceFactRefreshError("public text authority is invalid") from exc
    if public_authority is None or not public_authority.is_manual_title_resolution:
        raise CandidateSourceFactRefreshError("public text authority is not manual-title scoped")
    if (
        public["relative_path"]
        != "assets/lidousha/candidate_public_text_surface_authorities/"
        "auto_173005_934_1166.public-text-surface-authority.v1.json"
        or _sha(public["authority_sha256"], label="public text authority")
        != public_authority.authority_sha256
        or public_authority.authority_sha256 != _sha(
            authority["public_text_authority"]["authority_sha256"], label="sealed public authority"
        )
        or public_authority.resolved_title != title
        or public_authority.resolved_selection_hook != selection_hook
        or public_authority.superseded_title != historical_provider_receipt.get("final_title")
        or public_authority.input_selection_hook
        != historical_provider_receipt.get("final_selection_hook")
        != selection_hook
    ):
        raise CandidateSourceFactRefreshError("public title authority binding drifts")
    try:
        context = build_public_text_source_fact_context(
            public_authority, clip_context_prompt=clip_context_prompt
        )
    except CandidatePublicTextSurfaceAuthorityError as exc:
        raise CandidateSourceFactRefreshError("public title entity context drifts") from exc
    if (
        runtime.get("selection_hook") != selection_hook
        or runtime.get("title") != title
        or _text_sha(selection_hook)
        != _sha(runtime["selection_hook_sha256"], label="selection hook")
        or _text_sha(title) != _sha(runtime["title_sha256"], label="title")
        or _text_sha(final_transcript)
        != _sha(runtime["final_transcript_sha256"], label="final transcript")
        or _text_sha(clip_context_prompt)
        != _sha(runtime["clip_context_prompt_sha256"], label="clip context")
    ):
        raise CandidateSourceFactRefreshError("source-fact refresh text runtime binding drifts")
    if (
        _file_sha(final_reviewed_srt_path)
        != _sha(runtime["final_reviewed_srt_sha256"], label="reviewed SRT")
        or _canonical_sha(selection_scorecard)
        != _sha(runtime["selection_scorecard_sha256"], label="selection scorecard")
        or _canonical_sha(speaker_evidence)
        != _sha(runtime["speaker_evidence_sha256"], label="speaker evidence")
        or context.get("context_sha256")
        != _sha(runtime["entity_context_sha256"], label="entity context")
        or _canonical_sha(_normalized_story_contract(story_contract, historical_provider_receipt))
        != _sha(runtime["story_contract_sha256"], label="StoryContract")
    ):
        raise CandidateSourceFactRefreshError("source-fact refresh evidence binding drifts")
    if speaker_evidence != historical_provider_receipt.get("speaker_evidence"):
        raise CandidateSourceFactRefreshError("source-fact refresh speaker evidence chain drifts")
    return {
        "schema_version": CONSUMPTION_SCHEMA,
        "status": "VALID",
        "candidate_id": CANDIDATE_ID,
        "authority_sha256": _sha(authority["authority_sha256"], label="refresh authority"),
        "historical_provider_receipt_sha256": receipt_sha,
        "public_text_authority_sha256": public_authority.authority_sha256,
        "entity_context_sha256": context["context_sha256"],
        "final_reviewed_srt_sha256": _sha(
            runtime["final_reviewed_srt_sha256"], label="reviewed SRT"
        ),
        "final_transcript_sha256": _sha(
            runtime["final_transcript_sha256"], label="final transcript"
        ),
        "clip_context_prompt_sha256": _sha(
            runtime["clip_context_prompt_sha256"], label="clip context"
        ),
        "selection_scorecard_sha256": _sha(
            runtime["selection_scorecard_sha256"], label="selection scorecard"
        ),
        "speaker_evidence_sha256": _sha(runtime["speaker_evidence_sha256"], label="speaker evidence"),
    }


def _normalized_story_contract(
    story_contract: Mapping[str, object], historical_provider_receipt: Mapping[str, object]
) -> dict[str, object]:
    """Eliminate only the self-referential receipt pointer before hashing."""

    normalized = copy.deepcopy(dict(story_contract))
    normalized["source_fact_review"] = copy.deepcopy(dict(historical_provider_receipt))
    return normalized


def load_candidate_source_fact_refresh_preimages(*, repo_root: Path) -> dict[str, object]:
    """Return the sealed candidate-only document/state preimages."""

    authority = _load_authority(repo_root)
    preimages = authority.get("preimages")
    if not isinstance(preimages, Mapping):
        raise CandidateSourceFactRefreshError("candidate source-fact refresh preimages are absent")
    return copy.deepcopy(dict(preimages))


def build_candidate_public_text_source_fact_refresh_review(
    *,
    repo_root: Path,
    historical_provider_receipt: Mapping[str, object],
    candidate_id: str,
    selection_hook: str,
    title: str,
    final_transcript: str,
    clip_context_prompt: str,
    selection_scorecard: object,
    story_contract: Mapping[str, object],
    final_reviewed_srt_path: Path,
    speaker_evidence: object,
) -> dict[str, Any]:
    """Build the new typed receipt; this never invokes a provider."""

    authority = _load_authority(repo_root)
    consumption = _validate_authority(
        authority,
        repo_root=repo_root,
        historical_provider_receipt=historical_provider_receipt,
        candidate_id=candidate_id,
        selection_hook=selection_hook,
        title=title,
        final_transcript=final_transcript,
        clip_context_prompt=clip_context_prompt,
        selection_scorecard=selection_scorecard,
        story_contract=story_contract,
        final_reviewed_srt_path=final_reviewed_srt_path,
        speaker_evidence=speaker_evidence,
    )
    result: dict[str, Any] = {
        "schema_version": SOURCE_FACT_SCHEMA,
        "status": "PASS",
        "decision": DECISION,
        "original_selection_hook": historical_provider_receipt["original_selection_hook"],
        "original_title": historical_provider_receipt["original_title"],
        "final_selection_hook": selection_hook,
        "final_title": title,
        # Keep all claims, cited evidence and provider pass responses as the
        # original hash-bound bytes; the new title is a separate human grant.
        "historical_provider_receipt": copy.deepcopy(dict(historical_provider_receipt)),
        "candidate_public_text_source_fact_refresh": consumption,
    }
    result["receipt_sha256"] = _canonical_sha(result)
    return result


def validate_candidate_public_text_source_fact_refresh_review(
    review: object,
    **kwargs: Any,
) -> bool:
    if not isinstance(review, Mapping):
        return False
    historical = review.get("historical_provider_receipt")
    if not isinstance(historical, Mapping):
        return False
    try:
        expected = build_candidate_public_text_source_fact_refresh_review(
            historical_provider_receipt=historical,
            **kwargs,
        )
    except (OSError, TypeError, ValueError, KeyError):
        return False
    return dict(review) == expected


def validate_candidate_public_text_source_fact_refresh_from_source_fact(
    review: object,
    *,
    repo_root: Path,
    validation: Mapping[str, object],
) -> bool:
    """Adapt the generic validator's current inputs without widening its API."""

    candidate_id = validation.get("candidate_id")
    final_srt = validation.get("final_reviewed_srt_path")
    story_contract = validation.get("story_contract")
    if (
        candidate_id is None
        or not isinstance(final_srt, Path)
        or not isinstance(story_contract, Mapping)
    ):
        return False
    return validate_candidate_public_text_source_fact_refresh_review(
        review,
        repo_root=repo_root,
        candidate_id=str(candidate_id),
        selection_hook=str(validation.get("selection_hook") or ""),
        title=str(validation.get("title") or ""),
        final_transcript=str(validation.get("final_transcript") or ""),
        clip_context_prompt=str(validation.get("clip_context_prompt") or ""),
        selection_scorecard=validation.get("selection_scorecard"),
        story_contract=story_contract,
        final_reviewed_srt_path=final_srt,
        speaker_evidence=validation.get("speaker_evidence"),
    )
