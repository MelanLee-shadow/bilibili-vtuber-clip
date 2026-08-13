"""Candidate-only authority for generated public text, never transcript truth.

This is deliberately narrower than a reviewed-SRT entity projection.  It binds
one user-adjudicated spelling repair to one candidate, one hash-bound clip
context, and one original selection hook.  It may constrain generated hook,
title, cover, and publication text, but it cannot mutate subtitles or speaker
labels, release a registry hold, or authorize upload.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping

from src.autoslice.channel_profile import load_channel_profile
from src.autoslice.repository_asset_authority import (
    RepositoryAssetAuthorityError,
    repository_authority_expects_asset,
    require_repository_asset_authority,
)


ROOT = Path(__file__).resolve().parents[2]
CHANNEL_PROFILE = load_channel_profile(ROOT)
SCHEMA_VERSION = "lidousha-candidate-public-text-surface-authority.v1"
ALGORITHM_ID = "exact-candidate-generated-surface-resolution.v1"
CONSUMPTION_SCHEMA_VERSION = "candidate-public-text-surface-consumption.v1"
AUTHORITY_DIRECTORY = "candidate_public_text_surface_authorities"
AUTHORITY_SUFFIX = ".public-text-surface-authority.v1.json"
AUTHORITY_REQUIRED_CANDIDATES = frozenset({"auto_210739_1142_1436"})
PUBLIC_ARTIFACT_KINDS = ("selection_hook", "title", "cover", "publication")
_CANDIDATE_RX = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{2,127}\Z")
_SHA_RX = re.compile(r"sha256:[0-9a-f]{64}\Z")
_ROOT_FIELDS = {
    "schema_version",
    "algorithm_id",
    "candidate_binding",
    "input_surface_binding",
    "resolved_surfaces",
    "entity_surface_policy",
    "scope",
    "user_authorization",
    "authority_sha256",
}
_CANDIDATE_FIELDS = {
    "candidate_id",
    "recording_date",
    "clip_context_sha256",
    "selected_interval",
    "clip_context_source_pieces",
}
_SELECTED_INTERVAL_FIELDS = {"absolute_start_ms", "absolute_end_ms"}
_PIECE_FIELDS = {
    "recording_basename",
    "source_media_sha256",
    "start_ms",
    "end_ms",
}
_INPUT_FIELDS = {
    "selection_hook",
    "selection_hook_sha256",
    "superseded_title",
    "superseded_title_sha256",
}
_RESOLVED_FIELDS = {
    "selection_hook",
    "selection_hook_sha256",
    "title",
    "title_sha256",
}
_ENTITY_FIELDS = {
    "entity_id",
    "equivalent_surfaces",
    "required_public_surface",
    "forbidden_public_surfaces",
    "exact_substitution",
}
_SCOPE_FIELDS = {
    "artifact_kinds",
    "subtitle_text_mutation_authorized",
    "speaker_label_mutation_authorized",
    "upload_authorized",
    "registry_hold_released",
}


class CandidatePublicTextSurfaceAuthorityError(ValueError):
    """The optional candidate authority exists but is invalid or out of scope."""


def _sha256_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_json(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _required_object(value: object, fields: set[str], code: str) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise CandidatePublicTextSurfaceAuthorityError(code)
    return dict(value)


def _required_text(value: object, code: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CandidatePublicTextSurfaceAuthorityError(code)
    return value


def _validate_source_pieces(value: object) -> tuple[dict[str, object], ...]:
    if not isinstance(value, list) or not value:
        raise CandidatePublicTextSurfaceAuthorityError("PUBLIC_TEXT_SOURCE_PIECES_INVALID")
    pieces: list[dict[str, object]] = []
    for raw in value:
        piece = _required_object(raw, _PIECE_FIELDS, "PUBLIC_TEXT_SOURCE_PIECE_INVALID")
        if not (
            isinstance(piece["recording_basename"], str)
            and Path(piece["recording_basename"]).name == piece["recording_basename"]
            and _SHA_RX.fullmatch(str(piece["source_media_sha256"] or ""))
            and isinstance(piece["start_ms"], int)
            and not isinstance(piece["start_ms"], bool)
            and isinstance(piece["end_ms"], int)
            and not isinstance(piece["end_ms"], bool)
            and 0 <= piece["start_ms"] < piece["end_ms"]
        ):
            raise CandidatePublicTextSurfaceAuthorityError("PUBLIC_TEXT_SOURCE_PIECE_INVALID")
        pieces.append(piece)
    return tuple(pieces)


@dataclass(frozen=True, slots=True)
class CandidatePublicTextSurfaceAuthority:
    candidate_id: str
    recording_date: str
    clip_context_sha256: str
    source_pieces: tuple[dict[str, object], ...]
    selected_start_ms: int
    selected_end_ms: int
    input_selection_hook: str
    superseded_title: str
    resolved_selection_hook: str
    resolved_title: str
    entity_id: str
    equivalent_surfaces: tuple[str, ...]
    required_public_surface: str
    forbidden_public_surfaces: tuple[str, ...]
    authority_sha256: str
    user_authorization: dict[str, object]

    def require_text_surfaces(self, *, surface_type: str, text: str) -> str:
        """Enforce only generated title/cover spelling, never source transcript."""

        if surface_type != "title_cover" or not isinstance(text, str) or not text:
            raise CandidatePublicTextSurfaceAuthorityError("PUBLIC_TEXT_SURFACE_TYPE_INVALID")
        for surface in self.forbidden_public_surfaces:
            if surface in text:
                raise CandidatePublicTextSurfaceAuthorityError(
                    f"PUBLIC_TEXT_FORBIDDEN_ENTITY_SURFACE:{surface}"
                )
        return text

    def require_artifact_text(self, *, artifact_kind: str, text: str) -> str:
        if artifact_kind not in PUBLIC_ARTIFACT_KINDS:
            raise CandidatePublicTextSurfaceAuthorityError(
                f"PUBLIC_TEXT_ARTIFACT_OUT_OF_SCOPE:{artifact_kind}"
            )
        self.require_text_surfaces(surface_type="title_cover", text=text)
        if artifact_kind in {"title", "publication"} and text != self.resolved_title:
            raise CandidatePublicTextSurfaceAuthorityError("PUBLIC_TEXT_EXACT_TITLE_MISMATCH")
        return text

    def validate_clip_context_prompt(self, prompt: str) -> None:
        """Rebind a receipt to the current prompt's mandatory context header."""

        expected_pieces = [dict(piece) for piece in self.source_pieces]
        fields: dict[str, str] = {}
        for line in prompt.splitlines():
            for key in (
                "clip_context_sha256",
                "candidate_id",
                "recording_date",
                "selection_hook",
                "source_pieces",
            ):
                prefix = key + ": "
                if line.startswith(prefix) and key not in fields:
                    fields[key] = line[len(prefix) :]
        try:
            prompt_pieces = json.loads(fields.get("source_pieces", ""))
        except json.JSONDecodeError as exc:
            raise CandidatePublicTextSurfaceAuthorityError(
                "PUBLIC_TEXT_CLIP_CONTEXT_PROMPT_INVALID"
            ) from exc
        if (
            fields
            != {
                "clip_context_sha256": self.clip_context_sha256,
                "candidate_id": self.candidate_id,
                "recording_date": self.recording_date,
                "selection_hook": self.input_selection_hook,
                "source_pieces": fields.get("source_pieces", ""),
            }
            or prompt_pieces != expected_pieces
        ):
            raise CandidatePublicTextSurfaceAuthorityError(
                "PUBLIC_TEXT_CLIP_CONTEXT_BINDING_MISMATCH"
            )


@dataclass(frozen=True, slots=True)
class CandidatePublicTextStagingResolution:
    selection_hook: str
    title: str
    story_contract: dict[str, object]
    consumption: dict[str, object]


@dataclass(frozen=True, slots=True)
class CandidatePublicTextTitleState:
    title: str
    selection_hook: str
    story_contract: object
    title_source: str
    title_authority_status: str
    title_authority_error: str | None
    title_llm_call: object
    consumption: dict[str, object] | None


def _freeze_authority(document: dict[str, object]) -> CandidatePublicTextSurfaceAuthority:
    candidate = _required_object(
        document["candidate_binding"], _CANDIDATE_FIELDS, "PUBLIC_TEXT_BINDING_INVALID"
    )
    inputs = _required_object(
        document["input_surface_binding"], _INPUT_FIELDS, "PUBLIC_TEXT_INPUT_INVALID"
    )
    resolved = _required_object(
        document["resolved_surfaces"], _RESOLVED_FIELDS, "PUBLIC_TEXT_RESOLUTION_INVALID"
    )
    entity = _required_object(
        document["entity_surface_policy"], _ENTITY_FIELDS, "PUBLIC_TEXT_ENTITY_POLICY_INVALID"
    )
    scope = _required_object(document["scope"], _SCOPE_FIELDS, "PUBLIC_TEXT_SCOPE_INVALID")
    authorization = _required_object(
        document["user_authorization"], {"quote", "timestamp"}, "PUBLIC_TEXT_USER_AUTHORITY_INVALID"
    )
    candidate_id = _required_text(candidate["candidate_id"], "PUBLIC_TEXT_CANDIDATE_INVALID")
    selected = _required_object(
        candidate["selected_interval"],
        _SELECTED_INTERVAL_FIELDS,
        "PUBLIC_TEXT_SELECTED_INTERVAL_INVALID",
    )
    input_hook = _required_text(inputs["selection_hook"], "PUBLIC_TEXT_INPUT_INVALID")
    old_title = _required_text(inputs["superseded_title"], "PUBLIC_TEXT_INPUT_INVALID")
    resolved_hook = _required_text(resolved["selection_hook"], "PUBLIC_TEXT_RESOLUTION_INVALID")
    resolved_title = _required_text(resolved["title"], "PUBLIC_TEXT_RESOLUTION_INVALID")
    substitution = _required_object(
        entity["exact_substitution"], {"from", "to"}, "PUBLIC_TEXT_SUBSTITUTION_INVALID"
    )
    source = _required_text(substitution["from"], "PUBLIC_TEXT_SUBSTITUTION_INVALID")
    target = _required_text(substitution["to"], "PUBLIC_TEXT_SUBSTITUTION_INVALID")
    equivalents = entity["equivalent_surfaces"]
    forbidden = entity["forbidden_public_surfaces"]
    if not (
        _CANDIDATE_RX.fullmatch(candidate_id)
        and Path(candidate_id).name == candidate_id
        and _SHA_RX.fullmatch(str(candidate["clip_context_sha256"] or ""))
        and isinstance(selected["absolute_start_ms"], int)
        and not isinstance(selected["absolute_start_ms"], bool)
        and isinstance(selected["absolute_end_ms"], int)
        and not isinstance(selected["absolute_end_ms"], bool)
        and 0 <= selected["absolute_start_ms"] < selected["absolute_end_ms"]
        and isinstance(equivalents, list)
        and len(equivalents) == len(set(equivalents))
        and all(isinstance(value, str) and value for value in equivalents)
        and isinstance(forbidden, list)
        and len(forbidden) == len(set(forbidden))
        and all(isinstance(value, str) and value for value in forbidden)
        and source in input_hook
        and source in old_title
        and resolved_hook == input_hook.replace(source, target)
        and resolved_title == old_title.replace(source, target)
        and entity["required_public_surface"] == target
        and source in forbidden
        and scope
        == {
            "artifact_kinds": list(PUBLIC_ARTIFACT_KINDS),
            "subtitle_text_mutation_authorized": False,
            "speaker_label_mutation_authorized": False,
            "upload_authorized": False,
            "registry_hold_released": False,
        }
    ):
        raise CandidatePublicTextSurfaceAuthorityError("PUBLIC_TEXT_AUTHORITY_INVALID")
    for text, declared in (
        (input_hook, inputs["selection_hook_sha256"]),
        (old_title, inputs["superseded_title_sha256"]),
        (resolved_hook, resolved["selection_hook_sha256"]),
        (resolved_title, resolved["title_sha256"]),
    ):
        if declared != _sha256_text(text):
            raise CandidatePublicTextSurfaceAuthorityError("PUBLIC_TEXT_SURFACE_HASH_MISMATCH")
    _required_text(authorization["quote"], "PUBLIC_TEXT_USER_AUTHORITY_INVALID")
    timestamp = _required_text(authorization["timestamp"], "PUBLIC_TEXT_USER_AUTHORITY_INVALID")
    pieces = _validate_source_pieces(candidate["clip_context_source_pieces"])
    if not any(
        int(piece["start_ms"]) <= selected["absolute_start_ms"]
        and selected["absolute_end_ms"] <= int(piece["end_ms"])
        for piece in pieces
    ):
        raise CandidatePublicTextSurfaceAuthorityError(
            "PUBLIC_TEXT_SELECTED_INTERVAL_OUTSIDE_CONTEXT_PIECES"
        )
    try:
        parsed_timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CandidatePublicTextSurfaceAuthorityError(
            "PUBLIC_TEXT_USER_AUTHORITY_INVALID"
        ) from exc
    if parsed_timestamp.tzinfo != timezone.utc or not timestamp.endswith("Z"):
        raise CandidatePublicTextSurfaceAuthorityError("PUBLIC_TEXT_USER_AUTHORITY_INVALID")
    return CandidatePublicTextSurfaceAuthority(
        candidate_id=candidate_id,
        recording_date=_required_text(candidate["recording_date"], "PUBLIC_TEXT_BINDING_INVALID"),
        clip_context_sha256=str(candidate["clip_context_sha256"]),
        source_pieces=pieces,
        selected_start_ms=int(selected["absolute_start_ms"]),
        selected_end_ms=int(selected["absolute_end_ms"]),
        input_selection_hook=input_hook,
        superseded_title=old_title,
        resolved_selection_hook=resolved_hook,
        resolved_title=resolved_title,
        entity_id=_required_text(entity["entity_id"], "PUBLIC_TEXT_ENTITY_POLICY_INVALID"),
        equivalent_surfaces=tuple(equivalents),
        required_public_surface=target,
        forbidden_public_surfaces=tuple(forbidden),
        authority_sha256=str(document["authority_sha256"]),
        user_authorization=authorization,
    )


def load_candidate_public_text_surface_authority(
    candidate_id: str, *, root: Path = ROOT
) -> CandidatePublicTextSurfaceAuthority | None:
    """Load exactly the named candidate asset; absence preserves legacy behavior."""

    if (
        not _CANDIDATE_RX.fullmatch(str(candidate_id or ""))
        or Path(candidate_id).name != candidate_id
    ):
        return None
    truth_mode = os.environ.get("AUTOSLICE_HUMAN_TRUTH_MODE", "delivery").strip().lower()
    if truth_mode not in {"delivery", "withheld"}:
        raise CandidatePublicTextSurfaceAuthorityError("PUBLIC_TEXT_HUMAN_TRUTH_MODE_INVALID")
    if truth_mode == "withheld":
        return None
    asset_root = CHANNEL_PROFILE.asset_root.relative_to(CHANNEL_PROFILE.repo_root)
    relative = asset_root / AUTHORITY_DIRECTORY / f"{candidate_id}{AUTHORITY_SUFFIX}"
    path = root / relative
    if (
        candidate_id not in AUTHORITY_REQUIRED_CANDIDATES
        and not path.exists()
        and not path.is_symlink()
    ):
        # The publication choke point calls this loader for every candidate.
        # Avoid a repository subprocess for candidates that have no declared
        # authority at all; every production authority must also be listed in
        # AUTHORITY_REQUIRED_CANDIDATES, so a deleted required asset still
        # fails closed below.
        return None
    try:
        expected = repository_authority_expects_asset(
            repo_root=root,
            relative_path=relative,
        )
    except RepositoryAssetAuthorityError as exc:
        raise CandidatePublicTextSurfaceAuthorityError(
            "PUBLIC_TEXT_REPOSITORY_AUTHORITY_INVALID"
        ) from exc
    if not expected:
        # Self-consistent worktree bytes are inert until HEAD or a deployed
        # manifest grants authority to this exact repository-relative path.
        return None
    if not (path.exists() or path.is_symlink()):
        if candidate_id in AUTHORITY_REQUIRED_CANDIDATES:
            raise CandidatePublicTextSurfaceAuthorityError(
                "PUBLIC_TEXT_AUTHORITY_REQUIRED_BUT_MISSING"
            )
        return None
    if path.is_symlink() or not path.is_file():
        raise CandidatePublicTextSurfaceAuthorityError("PUBLIC_TEXT_AUTHORITY_FILE_INVALID")
    try:
        raw = path.read_bytes()
        require_repository_asset_authority(
            repo_root=root,
            relative_path=relative,
            observed_bytes=raw,
        )
        loaded = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CandidatePublicTextSurfaceAuthorityError("PUBLIC_TEXT_AUTHORITY_UNREADABLE") from exc
    except RepositoryAssetAuthorityError as exc:
        raise CandidatePublicTextSurfaceAuthorityError(
            "PUBLIC_TEXT_AUTHORITY_UNREADABLE_OR_UNSEALED"
        ) from exc
    document = _required_object(loaded, _ROOT_FIELDS, "PUBLIC_TEXT_AUTHORITY_SCHEMA_INVALID")
    if document["schema_version"] != SCHEMA_VERSION or document["algorithm_id"] != ALGORITHM_ID:
        raise CandidatePublicTextSurfaceAuthorityError("PUBLIC_TEXT_AUTHORITY_SCHEMA_INVALID")
    declared = document.pop("authority_sha256")
    if declared != _sha256_json(document):
        raise CandidatePublicTextSurfaceAuthorityError("PUBLIC_TEXT_AUTHORITY_HASH_MISMATCH")
    document["authority_sha256"] = declared
    authority = _freeze_authority(document)
    if authority.candidate_id != candidate_id:
        raise CandidatePublicTextSurfaceAuthorityError("PUBLIC_TEXT_CANDIDATE_MISMATCH")
    return authority


def consume_candidate_public_text_surface_authority(
    authority: CandidatePublicTextSurfaceAuthority,
    *,
    candidate_id: str,
    selection_hook: str,
    story_contract: Mapping[str, object],
) -> dict[str, object]:
    """Validate current source/context inputs and emit a relocation-safe receipt."""

    binding = story_contract.get("clip_context_binding")
    source_hashes = [str(piece["source_media_sha256"]) for piece in authority.source_pieces]
    if not (
        candidate_id == authority.candidate_id
        and story_contract.get("candidate_id") == candidate_id
        and story_contract.get("selection_hook") == selection_hook
        and selection_hook in {authority.input_selection_hook, authority.resolved_selection_hook}
        and story_contract.get("source_media_sha256s") == sorted(set(source_hashes))
        and isinstance(binding, Mapping)
        and binding.get("context_sha256") == authority.clip_context_sha256
    ):
        raise CandidatePublicTextSurfaceAuthorityError("PUBLIC_TEXT_RUNTIME_BINDING_MISMATCH")
    prompt = str(story_contract.get("clip_context_prompt") or "")
    authority.validate_clip_context_prompt(prompt)
    return {
        "schema_version": CONSUMPTION_SCHEMA_VERSION,
        "status": "CONSUMED",
        "candidate_id": candidate_id,
        "authority_sha256": authority.authority_sha256,
        "clip_context_sha256": authority.clip_context_sha256,
        "clip_context_prompt_sha256": _sha256_text(prompt),
        "selected_interval": {
            "absolute_start_ms": authority.selected_start_ms,
            "absolute_end_ms": authority.selected_end_ms,
        },
        "input_selection_hook_sha256": _sha256_text(selection_hook),
        "resolved_selection_hook": authority.resolved_selection_hook,
        "resolved_title": authority.resolved_title,
        "subtitle_text_mutation_authorized": False,
        "speaker_label_mutation_authorized": False,
        "upload_authorized": False,
        "registry_hold_released": False,
        "user_authorization": dict(authority.user_authorization),
    }


def resolve_candidate_public_text_staging(
    *,
    candidate_id: str,
    selection_hook: str,
    story_contract: object,
    story_contract_rebuilder: Callable[[str], dict[str, object]] | None,
    root: Path = ROOT,
) -> CandidatePublicTextStagingResolution | None:
    """Apply the optional authority before title/source-fact/cover generation."""

    authority = load_candidate_public_text_surface_authority(candidate_id, root=root)
    if authority is None:
        return None
    if not isinstance(story_contract, Mapping):
        raise CandidatePublicTextSurfaceAuthorityError("PUBLIC_TEXT_STORY_CONTRACT_REQUIRED")
    consume_candidate_public_text_surface_authority(
        authority,
        candidate_id=candidate_id,
        selection_hook=selection_hook,
        story_contract=story_contract,
    )
    if story_contract_rebuilder is None:
        raise CandidatePublicTextSurfaceAuthorityError(
            "PUBLIC_TEXT_STORY_CONTRACT_REBUILDER_REQUIRED"
        )
    rebuilt = story_contract_rebuilder(authority.resolved_selection_hook)
    consumption = consume_candidate_public_text_surface_authority(
        authority,
        candidate_id=candidate_id,
        selection_hook=authority.resolved_selection_hook,
        story_contract=rebuilt,
    )
    return CandidatePublicTextStagingResolution(
        selection_hook=authority.resolved_selection_hook,
        title=authority.resolved_title,
        story_contract=rebuilt,
        consumption=consumption,
    )


def resolve_candidate_public_text_title_state(
    *,
    candidate_id: str,
    title: str,
    selection_hook: str,
    story_contract: object,
    story_contract_rebuilder: Callable[[str], dict[str, object]] | None,
    title_source: str,
    title_authority_status: str,
    title_llm_call: object,
    manual_title: str | None,
) -> CandidatePublicTextTitleState:
    """Resolve the narrow exact substitution before any model-generated title."""

    try:
        resolution = resolve_candidate_public_text_staging(
            candidate_id=candidate_id,
            selection_hook=selection_hook,
            story_contract=story_contract,
            story_contract_rebuilder=story_contract_rebuilder,
        )
    except CandidatePublicTextSurfaceAuthorityError as exc:
        return CandidatePublicTextTitleState(
            title=title,
            selection_hook=selection_hook,
            story_contract=story_contract,
            title_source=title_source,
            title_authority_status="BLOCKED_PUBLIC_TEXT_SURFACE_AUTHORITY",
            title_authority_error=f"candidate_public_text_surface_failed:{exc}",
            title_llm_call=None,
            consumption=None,
        )
    if resolution is not None:
        ambiguous = manual_title is not None
        return CandidatePublicTextTitleState(
            title=resolution.title,
            selection_hook=resolution.selection_hook,
            story_contract=resolution.story_contract,
            title_source=(
                "deterministic_candidate_public_surface_resolution+ivan_exact_substitution"
            ),
            title_authority_status=(
                "BLOCKED_PUBLIC_TEXT_SURFACE_AUTHORITY"
                if ambiguous
                else "RESOLVED_PUBLIC_TEXT_SURFACE_AUTHORITY"
            ),
            title_authority_error=(
                "candidate_public_text_surface_failed:AMBIGUOUS_MANUAL_TITLE" if ambiguous else None
            ),
            title_llm_call=None,
            consumption=resolution.consumption,
        )
    if manual_title is not None:
        return CandidatePublicTextTitleState(
            title=manual_title,
            selection_hook=selection_hook,
            story_contract=story_contract,
            title_source="ivan_manual_override",
            title_authority_status="RESOLVED_MANUAL",
            title_authority_error=None,
            title_llm_call=None,
            consumption=None,
        )
    return CandidatePublicTextTitleState(
        title=title,
        selection_hook=selection_hook,
        story_contract=story_contract,
        title_source=title_source,
        title_authority_status=title_authority_status,
        title_authority_error=None,
        title_llm_call=title_llm_call,
        consumption=None,
    )


def build_public_text_source_fact_context(
    authority: CandidatePublicTextSurfaceAuthority,
    *,
    clip_context_prompt: str,
) -> dict[str, object]:
    """Build the receipt projection without claiming reviewed subtitle authority."""

    authority.validate_clip_context_prompt(clip_context_prompt)
    body: dict[str, object] = {
        "schema_version": "source-fact-entity-context.v1",
        "authority_scope": "GENERATED_PUBLIC_TEXT_ONLY_NO_SUBTITLE_OR_SPEAKER_REVIEW",
        "candidate_id": authority.candidate_id,
        "public_text_surface_authority_sha256": authority.authority_sha256,
        "clip_context_prompt_sha256": _sha256_text(clip_context_prompt),
        "candidate_binding": {
            "recording_date": authority.recording_date,
            "clip_context_sha256": authority.clip_context_sha256,
            "source_pieces": [dict(piece) for piece in authority.source_pieces],
            "selected_interval": {
                "absolute_start_ms": authority.selected_start_ms,
                "absolute_end_ms": authority.selected_end_ms,
            },
        },
        "identity_spelling_rules": [
            {
                "entity_id": authority.entity_id,
                "identity_equivalent_surfaces": list(authority.equivalent_surfaces),
                "reviewed_surface": None,
                "title_cover_surface": authority.required_public_surface,
            }
        ],
        "subtitle_text_mutation_authorized": False,
        "speaker_label_mutation_authorized": False,
        "upload_authorized": False,
        "registry_hold_released": False,
    }
    return {**body, "context_sha256": _sha256_json(body)}
