"""Candidate-scoped successor binding for one sealed source-fact refresh.

The public-title authority remains immutable: it still seals the predecessor
StoryContract and its existing consumption receipt.  This module recognizes
only the one later StoryContract whose sole change is the separately sealed
no-provider source-fact receipt.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from src.autoslice.repository_asset_authority import (
    RepositoryAssetAuthorityError,
    require_repository_asset_authority,
)


CANDIDATE_ID = "auto_173005_934_1166"
RECORDING_DATE = "2026-08-11"
SCHEMA_VERSION = "candidate-manual-title-source-fact-successor.v1"
RELATIVE_PATH = Path(
    "assets/lidousha/candidate_manual_title_source_fact_successors/"
    "auto_173005_934_1166.v1.json"
)
_SHA_PREFIX = "sha256:"
_SHA_LENGTH = len(_SHA_PREFIX) + 64
_ROOT_FIELDS = {
    "schema_version",
    "candidate_id",
    "recording_date",
    "public_text_authority",
    "source_fact_refresh_authority",
    "predecessor",
    "successor",
    "allowed_json_pointers",
    "authority_sha256",
}
_PUBLIC_TEXT_RELATIVE = Path(
    "assets/lidousha/candidate_public_text_surface_authorities/"
    "auto_173005_934_1166.public-text-surface-authority.v1.json"
)
_REFRESH_RELATIVE = Path(
    "assets/lidousha/candidate_source_fact_refresh_authorities/auto_173005_934_1166.v1.json"
)


class ManualTitleSourceFactSuccessorError(ValueError):
    """The narrow successor proof is unavailable or does not replay."""


def _canonical_sha(value: object) -> str:
    return _SHA_PREFIX + hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _sha(value: object, *, label: str) -> str:
    text = str(value or "")
    if (
        len(text) != _SHA_LENGTH
        or not text.startswith(_SHA_PREFIX)
        or any(char not in "0123456789abcdef" for char in text[len(_SHA_PREFIX) :])
    ):
        raise ManualTitleSourceFactSuccessorError(f"{label} is not a sha256")
    return text


def _exact_object(value: object, *, fields: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ManualTitleSourceFactSuccessorError(f"{label} schema is invalid")
    return dict(value)


def _load_sealed_json(*, repo_root: Path, relative_path: Path, label: str) -> dict[str, Any]:
    path = repo_root / relative_path
    try:
        raw = path.read_bytes()
        require_repository_asset_authority(
            repo_root=repo_root, relative_path=relative_path, observed_bytes=raw
        )
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, RepositoryAssetAuthorityError) as exc:
        raise ManualTitleSourceFactSuccessorError(f"{label} is unavailable or unsealed") from exc
    if not isinstance(value, Mapping):
        raise ManualTitleSourceFactSuccessorError(f"{label} schema is invalid")
    return dict(value)


def _load_successor_authority(*, repo_root: Path) -> dict[str, Any]:
    authority = _exact_object(
        _load_sealed_json(
            repo_root=repo_root, relative_path=RELATIVE_PATH, label="manual title successor authority"
        ),
        fields=_ROOT_FIELDS,
        label="manual title successor authority",
    )
    declared = _sha(authority.pop("authority_sha256"), label="manual title successor authority")
    if _canonical_sha(authority) != declared:
        raise ManualTitleSourceFactSuccessorError("manual title successor authority hash drifts")
    authority["authority_sha256"] = declared
    if (
        authority["schema_version"] != SCHEMA_VERSION
        or authority["candidate_id"] != CANDIDATE_ID
        or authority["recording_date"] != RECORDING_DATE
        or authority["allowed_json_pointers"] != ["/source_fact_review"]
    ):
        raise ManualTitleSourceFactSuccessorError("manual title successor authority identity drifts")
    for key, fields in (
        ("public_text_authority", {"relative_path", "authority_sha256"}),
        ("source_fact_refresh_authority", {"relative_path", "authority_sha256"}),
        ("predecessor", {"story_contract_sha256", "historical_provider_receipt_sha256"}),
        ("successor", {"story_contract_sha256", "source_fact_review_sha256"}),
    ):
        _exact_object(authority[key], fields=fields, label=f"manual title successor {key}")
    for location, field in (
        ("public_text_authority", "authority_sha256"),
        ("source_fact_refresh_authority", "authority_sha256"),
        ("predecessor", "story_contract_sha256"),
        ("predecessor", "historical_provider_receipt_sha256"),
        ("successor", "story_contract_sha256"),
        ("successor", "source_fact_review_sha256"),
    ):
        _sha(authority[location][field], label=f"manual title successor {location}")
    return authority


def _validate_refresh_asset(*, repo_root: Path, authority: Mapping[str, Any]) -> dict[str, Any]:
    binding = _exact_object(
        authority["source_fact_refresh_authority"],
        fields={"relative_path", "authority_sha256"},
        label="manual title successor refresh binding",
    )
    relative = Path(str(binding["relative_path"] or ""))
    if relative != _REFRESH_RELATIVE:
        raise ManualTitleSourceFactSuccessorError("manual title successor refresh path drifts")
    refresh = _load_sealed_json(repo_root=repo_root, relative_path=relative, label="source-fact refresh authority")
    declared = _sha(refresh.pop("authority_sha256", None), label="source-fact refresh authority")
    if _canonical_sha(refresh) != declared or declared != binding["authority_sha256"]:
        raise ManualTitleSourceFactSuccessorError("manual title successor refresh authority drifts")
    runtime = refresh.get("runtime_bindings")
    scope = refresh.get("scope")
    predecessor = _exact_object(
        authority["predecessor"],
        fields={"story_contract_sha256", "historical_provider_receipt_sha256"},
        label="manual title successor predecessor",
    )
    if (
        refresh.get("schema_version") != "candidate-public-text-source-fact-refresh-authority.v1"
        or refresh.get("candidate_id") != CANDIDATE_ID
        or refresh.get("recording_date") != RECORDING_DATE
        or not isinstance(runtime, Mapping)
        or runtime.get("story_contract_sha256") != predecessor["story_contract_sha256"]
        or scope
        != {
            "candidate_scoped": True,
            "provider_call_required": False,
            "subtitle_text_mutation_authorized": False,
            "speaker_label_mutation_authorized": False,
            "upload_authorized": False,
        }
    ):
        raise ManualTitleSourceFactSuccessorError("manual title successor refresh contract drifts")
    historical = _exact_object(
        refresh.get("historical_provider_receipt"),
        fields={
            "receipt_sha256", "original_title", "final_title", "original_selection_hook",
            "final_selection_hook", "status", "decision",
        },
        label="source-fact refresh historical binding",
    )
    public = _exact_object(
        refresh.get("public_text_authority"),
        fields={"relative_path", "authority_sha256"},
        label="source-fact refresh public-title binding",
    )
    public_binding = _exact_object(
        authority["public_text_authority"],
        fields={"relative_path", "authority_sha256"},
        label="manual title successor public-title binding",
    )
    if (
        public_binding["relative_path"] != _PUBLIC_TEXT_RELATIVE.as_posix()
        or historical.get("receipt_sha256") != predecessor["historical_provider_receipt_sha256"]
        or public.get("authority_sha256") != public_binding["authority_sha256"]
        or public.get("relative_path") != public_binding["relative_path"]
    ):
        raise ManualTitleSourceFactSuccessorError("manual title successor predecessor chain drifts")
    return refresh


def accepts_manual_title_source_fact_successor(
    *,
    repo_root: Path,
    candidate_id: str,
    public_text_authority_sha256: str,
    story_contract: Mapping[str, object],
) -> bool:
    """Return true only for the sealed receipt-only StoryContract successor."""

    if candidate_id != CANDIDATE_ID:
        return False
    authority = _load_successor_authority(repo_root=repo_root)
    public = _exact_object(
        authority["public_text_authority"],
        fields={"relative_path", "authority_sha256"},
        label="manual title successor public-title binding",
    )
    successor = _exact_object(
        authority["successor"],
        fields={"story_contract_sha256", "source_fact_review_sha256"},
        label="manual title successor successor",
    )
    predecessor = _exact_object(
        authority["predecessor"],
        fields={"story_contract_sha256", "historical_provider_receipt_sha256"},
        label="manual title successor predecessor",
    )
    if public_text_authority_sha256 != public["authority_sha256"]:
        raise ManualTitleSourceFactSuccessorError("manual title successor public authority drifts")
    refresh = _validate_refresh_asset(repo_root=repo_root, authority=authority)
    review = story_contract.get("source_fact_review")
    if not isinstance(review, Mapping):
        raise ManualTitleSourceFactSuccessorError("manual title successor review is absent")
    review_body = dict(review)
    review_sha = _sha(review_body.pop("receipt_sha256", None), label="manual title successor review")
    if (
        _canonical_sha(review_body) != review_sha
        or review_sha != successor["source_fact_review_sha256"]
        or review.get("schema_version") != "lidousha-source-fact-review.v1"
        or review.get("status") != "PASS"
        or review.get("decision") != "CANDIDATE_PUBLIC_TEXT_SOURCE_FACT_REFRESH"
    ):
        raise ManualTitleSourceFactSuccessorError("manual title successor review drifts")
    nested = review.get("candidate_public_text_source_fact_refresh")
    if not isinstance(nested, Mapping) or nested.get("authority_sha256") != authority[
        "source_fact_refresh_authority"
    ]["authority_sha256"]:
        raise ManualTitleSourceFactSuccessorError("manual title successor refresh receipt drifts")
    historical = review.get("historical_provider_receipt")
    if not isinstance(historical, Mapping):
        raise ManualTitleSourceFactSuccessorError("manual title successor historical receipt is absent")
    historical_body = dict(historical)
    historical_sha = _sha(historical_body.pop("receipt_sha256", None), label="manual title successor historical receipt")
    refresh_historical = refresh["historical_provider_receipt"]
    if (
        _canonical_sha(historical_body) != historical_sha
        or historical_sha != predecessor["historical_provider_receipt_sha256"]
        or any(historical.get(key) != refresh_historical.get(key) for key in refresh_historical)
    ):
        raise ManualTitleSourceFactSuccessorError("manual title successor historical receipt drifts")
    normalized = copy.deepcopy(dict(story_contract))
    normalized["source_fact_review"] = copy.deepcopy(dict(historical))
    if (
        _canonical_sha(dict(story_contract)) != successor["story_contract_sha256"]
        or _canonical_sha(normalized) != predecessor["story_contract_sha256"]
    ):
        raise ManualTitleSourceFactSuccessorError("manual title successor StoryContract drifts")
    return True
