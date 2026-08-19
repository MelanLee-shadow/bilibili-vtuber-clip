"""Candidate-scoped provenance for standalone source-fact scorecard rescores.

The normal source-fact lane can discover that a scorecard became stale, but an
operator correction is stronger evidence than hoping a later model call
rediscovers the same mismatch.  This module binds one committed correction
authority, one create-only rescore receipt, and the immutable source spec that
was rebound to the new card.

The provenance is deliberately self-contained: the exact authority and
rescore receipt are embedded in the rebound spec and later delivery records.
The authority is still checked byte-for-byte against the candidate-scoped
asset in the active repository, so copying or editing an embedded object cannot
manufacture authority.
"""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Mapping

from src.autoslice.selection_scorecard import (
    load_selected_selection_calibration_policy,
    selection_scorecard_is_valid,
)
from src.autoslice.repository_asset_authority import (
    RepositoryAssetAuthorityError,
    repository_authority_expects_asset,
    require_repository_asset_authority,
)


CORRECTION_AUTHORITY_SCHEMA = "candidate-source-fact-rescore-authority.v1"
RESCORE_RECEIPT_SCHEMA = "source-fact-rescore-scorecard.v1"
RESCORE_PROVENANCE_SCHEMA = "source-fact-scorecard-rescore-provenance.v1"
SPEC_REBIND_RECEIPT_SCHEMA = "source-fact-scorecard-rescore-spec-rebind.v1"
PROVENANCE_FIELD = "source_fact_scorecard_rescore_provenance"
AUTHORITY_REPO_DIRECTORY = Path("assets/lidousha/authorities")
MANUAL_RESCORE_PROVIDER = "cpa"
MANUAL_RESCORE_MODEL = "gpt-5.6-sol"
MANUAL_RESCORE_TRANSPORT = "command"
MANUAL_RESCORE_TIMEOUT_SECONDS = 600.0
MANUAL_RESCORE_COMMAND_TEMPLATE = (
    "bash scripts/llm_via_cpa.sh {prompt_file} {completion_file} "
    "'{model}' medium"
)

_CANDIDATE_ID_RE = re.compile(r"auto_[0-9]+_[0-9]+_[0-9]+\Z")
_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")


class SourceFactRescoreProvenanceError(ValueError):
    """A rescore authority, receipt, or spec binding is not trustworthy."""


def canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def bytes_sha256(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def normalize_sha256(value: object, *, label: str) -> str:
    candidate = str(value or "").strip().lower()
    if not candidate.startswith("sha256:"):
        candidate = "sha256:" + candidate
    if _SHA256_RE.fullmatch(candidate) is None:
        raise SourceFactRescoreProvenanceError(f"{label} is not a SHA-256 digest")
    return candidate


def authority_repo_path(candidate_id: str) -> Path:
    if _CANDIDATE_ID_RE.fullmatch(candidate_id) is None:
        raise SourceFactRescoreProvenanceError("candidate_id is invalid")
    return AUTHORITY_REPO_DIRECTORY / (
        f"{candidate_id}.source-fact-rescore-authority.v1.json"
    )


def _require_mapping(value: object, *, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise SourceFactRescoreProvenanceError(f"{label} must be an object")
    return value


def _require_nonempty_string(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SourceFactRescoreProvenanceError(f"{label} must be a non-empty string")
    return value


def _require_int(value: object, *, label: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise SourceFactRescoreProvenanceError(f"{label} must be an integer >= {minimum}")
    return value


def _without_hash(value: Mapping[str, object], field: str) -> dict[str, object]:
    return {key: item for key, item in value.items() if key != field}


def validate_correction_authority(
    value: object,
    *,
    candidate_id: str,
) -> dict[str, object]:
    """Validate and normalize one candidate correction authority payload."""

    authority = dict(_require_mapping(value, label="correction authority"))
    expected_keys = {
        "schema_version",
        "candidate_id",
        "original_selection_hook",
        "corrected_selection_hook",
        "stale_selection_scorecard_sha256",
        "reviewed_final_srt",
        "source",
        "reviewer_authority",
        "authority_scope",
        "authority_sha256",
    }
    if set(authority) != expected_keys:
        raise SourceFactRescoreProvenanceError(
            "correction authority field set is invalid"
        )
    if authority.get("schema_version") != CORRECTION_AUTHORITY_SCHEMA:
        raise SourceFactRescoreProvenanceError("correction authority schema is invalid")
    if authority.get("candidate_id") != candidate_id:
        raise SourceFactRescoreProvenanceError("correction authority candidate mismatch")

    original_hook = _require_nonempty_string(
        authority.get("original_selection_hook"), label="original selection hook"
    )
    corrected_hook = _require_nonempty_string(
        authority.get("corrected_selection_hook"), label="corrected selection hook"
    )
    if original_hook == corrected_hook:
        raise SourceFactRescoreProvenanceError(
            "correction authority does not change the selection hook"
        )
    authority["stale_selection_scorecard_sha256"] = normalize_sha256(
        authority.get("stale_selection_scorecard_sha256"),
        label="stale selection scorecard SHA-256",
    )

    reviewed = dict(
        _require_mapping(authority.get("reviewed_final_srt"), label="reviewed_final_srt")
    )
    if set(reviewed) != {"repo_path", "sha256", "cue_count"}:
        raise SourceFactRescoreProvenanceError("reviewed_final_srt field set is invalid")
    reviewed_path = Path(
        _require_nonempty_string(reviewed.get("repo_path"), label="reviewed SRT repo path")
    )
    if reviewed_path.is_absolute() or ".." in reviewed_path.parts:
        raise SourceFactRescoreProvenanceError("reviewed SRT repo path is unsafe")
    reviewed["repo_path"] = reviewed_path.as_posix()
    reviewed["sha256"] = normalize_sha256(
        reviewed.get("sha256"), label="reviewed SRT SHA-256"
    )
    reviewed["cue_count"] = _require_int(
        reviewed.get("cue_count"), label="reviewed SRT cue count", minimum=1
    )
    authority["reviewed_final_srt"] = reviewed

    source = dict(_require_mapping(authority.get("source"), label="source"))
    if set(source) != {
        "recording_basename",
        "sha256",
        "absolute_start_ms",
        "absolute_end_ms",
    }:
        raise SourceFactRescoreProvenanceError("source field set is invalid")
    basename = _require_nonempty_string(
        source.get("recording_basename"), label="source recording basename"
    )
    if Path(basename).name != basename:
        raise SourceFactRescoreProvenanceError("source recording basename is unsafe")
    source["sha256"] = normalize_sha256(source.get("sha256"), label="source SHA-256")
    start_ms = _require_int(source.get("absolute_start_ms"), label="source start ms")
    end_ms = _require_int(source.get("absolute_end_ms"), label="source end ms")
    if end_ms <= start_ms:
        raise SourceFactRescoreProvenanceError("source interval is invalid")
    authority["source"] = source

    维护者 = dict(_require_mapping(authority.get("reviewer_authority"), label="reviewer_authority"))
    if set(维护者) != {"story_truth_quote", "correction_quote", "publication_quote"}:
        raise SourceFactRescoreProvenanceError("reviewer_authority field set is invalid")
    for key in sorted(维护者):
        _require_nonempty_string(维护者.get(key), label=f"reviewer_authority.{key}")

    scope = dict(_require_mapping(authority.get("authority_scope"), label="authority_scope"))
    if set(scope) != {
        "scorecard_rescore",
        "provider_execution",
        "runner_state_mutation",
        "publication_manifest",
    }:
        raise SourceFactRescoreProvenanceError("authority_scope field set is invalid")
    if scope != {
        "scorecard_rescore": True,
        "provider_execution": False,
        "runner_state_mutation": False,
        "publication_manifest": False,
    }:
        raise SourceFactRescoreProvenanceError(
            "correction authority scope must authorize only the scorecard rescore"
        )

    expected_hash = canonical_sha256(_without_hash(authority, "authority_sha256"))
    if authority.get("authority_sha256") != expected_hash:
        raise SourceFactRescoreProvenanceError("correction authority hash is invalid")
    return authority


def load_committed_correction_authority(
    *,
    repo_root: Path,
    candidate_id: str,
) -> tuple[dict[str, object], Path, str]:
    """Load the only allowed candidate authority path from the active repo."""

    relative = authority_repo_path(candidate_id)
    root = repo_root.resolve(strict=True)
    unresolved = root / relative
    if unresolved.is_symlink():
        raise SourceFactRescoreProvenanceError(
            "correction authority must not be a symlink"
        )
    path = unresolved.resolve(strict=True)
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise SourceFactRescoreProvenanceError(
            "correction authority escapes the repository"
        ) from exc
    if not path.is_file():
        raise SourceFactRescoreProvenanceError(
            "correction authority must be a regular repository file"
        )
    raw = path.read_bytes()
    try:
        require_repository_asset_authority(
            repo_root=root,
            relative_path=relative,
            observed_bytes=raw,
        )
    except RepositoryAssetAuthorityError as exc:
        raise SourceFactRescoreProvenanceError(
            f"correction authority is not committed or deployed: {exc}"
        ) from exc
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise SourceFactRescoreProvenanceError(
            "correction authority is not valid UTF-8 JSON"
        ) from exc
    authority = validate_correction_authority(payload, candidate_id=candidate_id)
    return authority, path, bytes_sha256(raw)


def validate_rescore_receipt(
    value: object,
    *,
    authority: Mapping[str, object],
    authority_file_sha256: str,
) -> dict[str, object]:
    """Validate the create-only receipt emitted by the standalone rescore."""

    receipt = dict(_require_mapping(value, label="rescore receipt"))
    if receipt.get("schema_version") != RESCORE_RECEIPT_SCHEMA:
        raise SourceFactRescoreProvenanceError("rescore receipt schema is invalid")
    if receipt.get("status") != "RESCORED":
        raise SourceFactRescoreProvenanceError("rescore receipt status is not RESCORED")
    candidate_id = str(authority["candidate_id"])
    if receipt.get("candidate_id") != candidate_id:
        raise SourceFactRescoreProvenanceError("rescore receipt candidate mismatch")
    expected_output_hash = canonical_sha256(_without_hash(receipt, "output_sha256"))
    if receipt.get("output_sha256") != expected_output_hash:
        raise SourceFactRescoreProvenanceError("rescore receipt output hash is invalid")

    bindings = dict(_require_mapping(receipt.get("input_bindings"), label="rescore inputs"))
    if receipt.get("input_sha256") != canonical_sha256(bindings):
        raise SourceFactRescoreProvenanceError("rescore input hash is invalid")
    if bindings.get("input_mode") != "candidate_correction_authority":
        raise SourceFactRescoreProvenanceError(
            "rescore receipt is not based on candidate correction authority"
        )
    required_binding_keys = {
        "input_mode",
        "candidate_id",
        "reviewed_final_srt_sha256",
        "reviewed_final_cue_count",
        "reviewed_final_transcript_sha256",
        "corrected_hook",
        "corrected_hook_sha256",
        "original_selection_hook",
        "original_selection_hook_sha256",
        "source_interval",
        "stale_scorecard_file_sha256",
        "stale_scorecard_sha256",
        "provider_config_file_sha256",
        "provider",
        "model",
        "transport",
        "provider_command_template_sha256",
        "provider_timeout_seconds",
        "selection_calibration_sha256",
        "correction_authority_file_sha256",
        "correction_authority_sha256",
    }
    if set(bindings) != required_binding_keys:
        raise SourceFactRescoreProvenanceError(
            "candidate-authority rescore input field set is invalid"
        )
    if bindings.get("candidate_id") != candidate_id:
        raise SourceFactRescoreProvenanceError("rescore input candidate mismatch")
    expected_authority_file_hash = normalize_sha256(
        authority_file_sha256, label="authority file SHA-256"
    )
    if bindings.get("correction_authority_file_sha256") != expected_authority_file_hash:
        raise SourceFactRescoreProvenanceError("rescore authority file hash mismatch")
    if bindings.get("correction_authority_sha256") != authority.get("authority_sha256"):
        raise SourceFactRescoreProvenanceError("rescore authority payload hash mismatch")
    if bindings.get("original_selection_hook") != authority.get("original_selection_hook"):
        raise SourceFactRescoreProvenanceError("rescore original hook mismatch")
    if bindings.get("corrected_hook") != authority.get("corrected_selection_hook"):
        raise SourceFactRescoreProvenanceError("rescore corrected hook mismatch")
    if bindings.get("stale_scorecard_sha256") != authority.get(
        "stale_selection_scorecard_sha256"
    ):
        raise SourceFactRescoreProvenanceError("rescore stale scorecard mismatch")
    for value, label in (
        (bindings.get("reviewed_final_transcript_sha256"), "reviewed transcript"),
        (bindings.get("corrected_hook_sha256"), "corrected hook"),
        (bindings.get("original_selection_hook_sha256"), "original hook"),
        (bindings.get("stale_scorecard_file_sha256"), "stale scorecard file"),
        (bindings.get("provider_config_file_sha256"), "provider config file"),
        (bindings.get("provider_command_template_sha256"), "provider command template"),
        (bindings.get("selection_calibration_sha256"), "selection calibration"),
    ):
        normalize_sha256(value, label=f"{label} SHA-256")
    if bindings.get("corrected_hook_sha256") != bytes_sha256(
        str(bindings["corrected_hook"]).encode("utf-8")
    ):
        raise SourceFactRescoreProvenanceError("rescore corrected hook hash mismatch")
    if bindings.get("original_selection_hook_sha256") != bytes_sha256(
        str(bindings["original_selection_hook"]).encode("utf-8")
    ):
        raise SourceFactRescoreProvenanceError("rescore original hook hash mismatch")
    timeout = bindings.get("provider_timeout_seconds")
    if (
        bindings.get("provider") != MANUAL_RESCORE_PROVIDER
        or bindings.get("model") != MANUAL_RESCORE_MODEL
        or bindings.get("transport") != MANUAL_RESCORE_TRANSPORT
        or isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or float(timeout) != MANUAL_RESCORE_TIMEOUT_SECONDS
        or bindings.get("provider_command_template_sha256")
        != bytes_sha256(MANUAL_RESCORE_COMMAND_TEMPLATE.encode("utf-8"))
    ):
        raise SourceFactRescoreProvenanceError("rescore provider contract drifted")
    calibration = load_selected_selection_calibration_policy()
    if bindings.get("selection_calibration_sha256") != calibration.source_sha256:
        raise SourceFactRescoreProvenanceError("rescore calibration authority drifted")

    reviewed = _require_mapping(authority.get("reviewed_final_srt"), label="reviewed SRT")
    if bindings.get("reviewed_final_srt_sha256") != reviewed.get("sha256"):
        raise SourceFactRescoreProvenanceError("rescore reviewed SRT mismatch")
    if bindings.get("reviewed_final_cue_count") != reviewed.get("cue_count"):
        raise SourceFactRescoreProvenanceError("rescore reviewed cue count mismatch")
    source = _require_mapping(authority.get("source"), label="source")
    if bindings.get("source_interval") != {
        "absolute_start_ms": source.get("absolute_start_ms"),
        "absolute_end_ms": source.get("absolute_end_ms"),
    }:
        raise SourceFactRescoreProvenanceError("rescore source interval mismatch")

    scorecard = receipt.get("selection_scorecard")
    if not selection_scorecard_is_valid(scorecard):
        raise SourceFactRescoreProvenanceError("rescored selection scorecard is invalid")
    if receipt.get("selection_scorecard_sha256") != canonical_sha256(scorecard):
        raise SourceFactRescoreProvenanceError("rescored selection scorecard hash is invalid")
    if receipt.get("start_cue") != 1 or receipt.get("end_cue") != reviewed.get(
        "cue_count"
    ):
        raise SourceFactRescoreProvenanceError("rescore did not cover the full reviewed SRT")
    if receipt.get("authority") != {
        "kind": "MANUAL_RECOVERY_RESCORE_ONLY",
        "runner_state_mutated": False,
        "publication_authority": False,
    }:
        raise SourceFactRescoreProvenanceError("rescore receipt authority scope drifted")
    return receipt


def _baseline_binding(spec: Mapping[str, object]) -> Mapping[str, object]:
    baseline = _require_mapping(
        spec.get("subtitle_redelivery_baseline"), label="subtitle redelivery baseline"
    )
    return baseline


def validate_spec_against_authority(
    spec: Mapping[str, object],
    *,
    authority: Mapping[str, object],
    require_stale_scorecard: bool,
) -> None:
    """Validate candidate, corrected hook, reviewed SRT and source interval."""

    if spec.get("candidate_id") != authority.get("candidate_id"):
        raise SourceFactRescoreProvenanceError("spec candidate mismatch")
    if spec.get("selection_hook") != authority.get("corrected_selection_hook"):
        raise SourceFactRescoreProvenanceError("spec corrected hook mismatch")
    if require_stale_scorecard:
        if canonical_sha256(spec.get("selection_scorecard")) != authority.get(
            "stale_selection_scorecard_sha256"
        ):
            raise SourceFactRescoreProvenanceError("spec stale scorecard mismatch")

    baseline = _baseline_binding(spec)
    reviewed = _require_mapping(authority.get("reviewed_final_srt"), label="reviewed SRT")
    source = _require_mapping(authority.get("source"), label="source")
    if normalize_sha256(baseline.get("sha256"), label="baseline SHA-256") != reviewed.get(
        "sha256"
    ):
        raise SourceFactRescoreProvenanceError("spec reviewed SRT hash mismatch")
    expected = {
        "source_recording_basename": source.get("recording_basename"),
        "source_sha256": source.get("sha256"),
        "absolute_source_start_ms": source.get("absolute_start_ms"),
        "absolute_source_end_ms": source.get("absolute_end_ms"),
    }
    observed = {
        "source_recording_basename": baseline.get("source_recording_basename"),
        "source_sha256": normalize_sha256(
            baseline.get("source_sha256"), label="baseline source SHA-256"
        ),
        "absolute_source_start_ms": baseline.get("absolute_source_start_ms"),
        "absolute_source_end_ms": baseline.get("absolute_source_end_ms"),
    }
    if observed != expected:
        raise SourceFactRescoreProvenanceError("spec source interval binding mismatch")


def build_rescore_provenance(
    *,
    source_spec: Mapping[str, object],
    source_spec_file_sha256: str,
    authority: Mapping[str, object],
    authority_file_sha256: str,
    receipt: Mapping[str, object],
    receipt_file_sha256: str,
) -> dict[str, object]:
    """Build the self-contained provenance block for a rebound spec."""

    candidate_id = str(authority["candidate_id"])
    validate_spec_against_authority(
        source_spec, authority=authority, require_stale_scorecard=True
    )
    validated_receipt = validate_rescore_receipt(
        receipt,
        authority=authority,
        authority_file_sha256=authority_file_sha256,
    )
    bindings = _require_mapping(validated_receipt["input_bindings"], label="rescore inputs")
    source = _require_mapping(authority.get("source"), label="source")
    reviewed = _require_mapping(authority.get("reviewed_final_srt"), label="reviewed SRT")
    provenance: dict[str, object] = {
        "schema_version": RESCORE_PROVENANCE_SCHEMA,
        "candidate_id": candidate_id,
        "source_spec_file_sha256": normalize_sha256(
            source_spec_file_sha256, label="source spec file SHA-256"
        ),
        "source_spec_canonical_sha256": canonical_sha256(source_spec),
        "correction_authority_repo_path": authority_repo_path(candidate_id).as_posix(),
        "correction_authority_file_sha256": normalize_sha256(
            authority_file_sha256, label="authority file SHA-256"
        ),
        "correction_authority": dict(authority),
        "rescore_receipt_file_sha256": normalize_sha256(
            receipt_file_sha256, label="rescore receipt file SHA-256"
        ),
        "rescore_receipt": dict(validated_receipt),
        "original_selection_hook": authority["original_selection_hook"],
        "corrected_selection_hook": authority["corrected_selection_hook"],
        "stale_selection_scorecard_sha256": authority[
            "stale_selection_scorecard_sha256"
        ],
        "selection_scorecard_sha256": validated_receipt[
            "selection_scorecard_sha256"
        ],
        "reviewed_final_srt_sha256": reviewed["sha256"],
        "source_recording_basename": source["recording_basename"],
        "source_sha256": source["sha256"],
        "source_interval": {
            "absolute_start_ms": source["absolute_start_ms"],
            "absolute_end_ms": source["absolute_end_ms"],
        },
        "selection_calibration_sha256": bindings.get(
            "selection_calibration_sha256"
        ),
        "provider": {
            "provider": bindings.get("provider"),
            "model": bindings.get("model"),
            "transport": bindings.get("transport"),
            "timeout_seconds": bindings.get("provider_timeout_seconds"),
        },
    }
    provenance["provenance_sha256"] = canonical_sha256(provenance)
    return provenance


def validate_rebound_spec_provenance(
    spec: Mapping[str, object],
    *,
    repo_root: Path,
) -> dict[str, object] | None:
    """Validate a rebound spec, preserving legacy specs without an authority.

    If a candidate has a committed authority asset, its historical stale card
    remains a valid legacy input.  Any different card is accepted only when the
    spec carries a complete, valid receipt-backed provenance block.
    """

    candidate_id = str(spec.get("candidate_id") or "")
    if _CANDIDATE_ID_RE.fullmatch(candidate_id) is None:
        return None
    authority_relative = authority_repo_path(candidate_id)
    raw_provenance = spec.get(PROVENANCE_FIELD)
    try:
        authority_expected = repository_authority_expects_asset(
            repo_root=repo_root,
            relative_path=authority_relative,
        )
    except RepositoryAssetAuthorityError as exc:
        raise SourceFactRescoreProvenanceError(
            f"candidate authority repository identity is invalid: {exc}"
        ) from exc
    if not authority_expected:
        if raw_provenance is not None:
            raise SourceFactRescoreProvenanceError(
                "rescore provenance has no committed candidate authority"
            )
        return None

    authority, _path, authority_file_sha = load_committed_correction_authority(
        repo_root=repo_root, candidate_id=candidate_id
    )
    current_card_sha = canonical_sha256(spec.get("selection_scorecard"))
    stale_card_sha = str(authority["stale_selection_scorecard_sha256"])
    if raw_provenance is None:
        if current_card_sha != stale_card_sha:
            raise SourceFactRescoreProvenanceError(
                "candidate selection scorecard changed without a rescore receipt"
            )
        return None

    provenance = dict(_require_mapping(raw_provenance, label="rescore provenance"))
    if provenance.get("schema_version") != RESCORE_PROVENANCE_SCHEMA:
        raise SourceFactRescoreProvenanceError("rescore provenance schema is invalid")
    if provenance.get("candidate_id") != candidate_id:
        raise SourceFactRescoreProvenanceError("rescore provenance candidate mismatch")
    if provenance.get("provenance_sha256") != canonical_sha256(
        _without_hash(provenance, "provenance_sha256")
    ):
        raise SourceFactRescoreProvenanceError("rescore provenance hash is invalid")
    if provenance.get("correction_authority_repo_path") != authority_repo_path(
        candidate_id
    ).as_posix():
        raise SourceFactRescoreProvenanceError("rescore authority repo path mismatch")
    if provenance.get("correction_authority_file_sha256") != authority_file_sha:
        raise SourceFactRescoreProvenanceError("rescore authority file bytes mismatch")
    if provenance.get("correction_authority") != authority:
        raise SourceFactRescoreProvenanceError("embedded rescore authority mismatch")

    receipt = validate_rescore_receipt(
        provenance.get("rescore_receipt"),
        authority=authority,
        authority_file_sha256=authority_file_sha,
    )
    if provenance.get("rescore_receipt_file_sha256") is None:
        raise SourceFactRescoreProvenanceError("rescore receipt file hash is missing")
    normalize_sha256(
        provenance.get("rescore_receipt_file_sha256"),
        label="rescore receipt file SHA-256",
    )
    normalize_sha256(
        provenance.get("source_spec_file_sha256"),
        label="source spec file SHA-256",
    )
    normalize_sha256(
        provenance.get("source_spec_canonical_sha256"),
        label="source spec canonical SHA-256",
    )
    if current_card_sha != receipt.get("selection_scorecard_sha256"):
        raise SourceFactRescoreProvenanceError("rebound spec scorecard hash mismatch")
    validate_spec_against_authority(
        spec, authority=authority, require_stale_scorecard=False
    )

    required_equalities = {
        "original_selection_hook": authority["original_selection_hook"],
        "corrected_selection_hook": authority["corrected_selection_hook"],
        "stale_selection_scorecard_sha256": stale_card_sha,
        "selection_scorecard_sha256": receipt["selection_scorecard_sha256"],
        "reviewed_final_srt_sha256": _require_mapping(
            authority["reviewed_final_srt"], label="reviewed SRT"
        )["sha256"],
        "source_recording_basename": _require_mapping(
            authority["source"], label="source"
        )["recording_basename"],
        "source_sha256": _require_mapping(authority["source"], label="source")[
            "sha256"
        ],
        "source_interval": {
            "absolute_start_ms": _require_mapping(
                authority["source"], label="source"
            )["absolute_start_ms"],
            "absolute_end_ms": _require_mapping(
                authority["source"], label="source"
            )["absolute_end_ms"],
        },
        "selection_calibration_sha256": _require_mapping(
            receipt["input_bindings"], label="rescore inputs"
        )["selection_calibration_sha256"],
        "provider": {
            "provider": _require_mapping(
                receipt["input_bindings"], label="rescore inputs"
            )["provider"],
            "model": _require_mapping(
                receipt["input_bindings"], label="rescore inputs"
            )["model"],
            "transport": _require_mapping(
                receipt["input_bindings"], label="rescore inputs"
            )["transport"],
            "timeout_seconds": _require_mapping(
                receipt["input_bindings"], label="rescore inputs"
            )["provider_timeout_seconds"],
        },
    }
    for key, expected in required_equalities.items():
        if provenance.get(key) != expected:
            raise SourceFactRescoreProvenanceError(
                f"rescore provenance {key} mismatch"
            )
    return provenance


def bind_finalization_provenance(
    *,
    spec: Mapping[str, object],
    record: dict[str, object],
    story_contract: dict[str, object],
    selection_hook: str,
    selection_scorecard: object,
) -> dict[str, object] | None:
    """Recheck and persist a rebound scorecard at finalization's choke point."""

    raw = spec.get(PROVENANCE_FIELD)
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise SourceFactRescoreProvenanceError(
            "RESCORE_PROVENANCE_MUTATED_AFTER_BINDING"
        )
    provenance = deepcopy(dict(raw))
    if selection_hook != provenance.get("corrected_selection_hook"):
        raise SourceFactRescoreProvenanceError(
            "RESCORED_SELECTION_HOOK_MUTATED_AFTER_BINDING"
        )
    if canonical_sha256(selection_scorecard) != provenance.get(
        "selection_scorecard_sha256"
    ):
        raise SourceFactRescoreProvenanceError(
            "RESCORED_SELECTION_SCORECARD_MUTATED_AFTER_BINDING"
        )
    record[PROVENANCE_FIELD] = deepcopy(provenance)
    story_contract[PROVENANCE_FIELD] = deepcopy(provenance)
    return provenance


def bind_publish_staging_provenance(
    record: dict[str, object], provenance: Mapping[str, object] | None
) -> None:
    """Copy already-validated rescore provenance into publish staging."""

    if provenance is None:
        return
    staging = record.get("publish_staging")
    if not isinstance(staging, Mapping):
        raise SourceFactRescoreProvenanceError(
            "RESCORE_PUBLISH_STAGING_MISSING"
        )
    rebound = dict(staging)
    rebound[PROVENANCE_FIELD] = deepcopy(dict(provenance))
    record["publish_staging"] = rebound


def publish_staging_provenance_fields(
    record: Mapping[str, object],
) -> dict[str, object]:
    """Return the validated provenance fragment shared by both draft mirrors."""

    provenance = record.get(PROVENANCE_FIELD)
    if provenance is None:
        return {}
    if not isinstance(provenance, Mapping):
        raise SourceFactRescoreProvenanceError(
            "source-fact rescore provenance must be an object"
        )
    return {PROVENANCE_FIELD: deepcopy(dict(provenance))}


def bind_story_contract_provenance(
    story_contract: dict[str, object], provenance: Mapping[str, object] | None
) -> None:
    """Preserve provenance when source-fact review rebuilds a story contract."""

    if provenance is not None:
        story_contract[PROVENANCE_FIELD] = deepcopy(dict(provenance))
