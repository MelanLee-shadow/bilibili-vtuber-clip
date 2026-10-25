"""Public compatibility boundary: candidate-specific text authorities are private."""
from __future__ import annotations
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from src.autoslice.channel_profile import load_channel_profile

ROOT = Path(__file__).resolve().parents[2]
CHANNEL_PROFILE = load_channel_profile(ROOT)
ALGORITHM_ID = "exact-candidate-generated-surface-resolution.v1"
AUTHORITY_DIRECTORY = "candidate_public_text_surface_authorities"
AUTHORITY_SUFFIX = ".public-text-surface-authority.v1.json"
_UNAVAILABLE = "PUBLIC_TEXT_PRIVATE_AUTHORITY_UNAVAILABLE"

class CandidatePublicTextSurfaceAuthorityError(ValueError):
    pass

def _unavailable(*_args, **_kwargs):
    raise CandidatePublicTextSurfaceAuthorityError(_UNAVAILABLE)

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
    algorithm_id: str = ALGORITHM_ID
    story_contract_sha256: str | None = None
    clip_context_prompt_sha256: str | None = None
    decision_authorization: dict[str, object] | None = None
    resolved_cover_lines: tuple[str, ...] = ()

    @property
    def is_manual_title_resolution(self) -> bool:
        _unavailable()

    @property
    def is_root_reviewed_resolution(self) -> bool:
        _unavailable()

    @property
    def title_source(self) -> str:
        _unavailable()

    def require_text_surfaces(self, *, surface_type: str, text: str) -> str:
        _unavailable()

    def require_artifact_text(self, *, artifact_kind: str, text: str) -> str:
        _unavailable()

    def validate_clip_context_prompt(self, prompt: str) -> None:
        _unavailable()

@dataclass(frozen=True, slots=True)
class CandidatePublicTextStagingResolution:
    selection_hook: str
    title: str
    story_contract: dict[str, object]
    consumption: dict[str, object]
    title_source: str

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

def load_candidate_public_text_surface_authority(candidate_id, *, root=ROOT):
    if not isinstance(candidate_id, str) or not candidate_id or Path(candidate_id).name != candidate_id:
        return None
    mode = os.environ.get("AUTOSLICE_HUMAN_TRUTH_MODE", "delivery").strip().lower()
    if mode not in {"delivery", "withheld"}:
        raise CandidatePublicTextSurfaceAuthorityError("PUBLIC_TEXT_HUMAN_TRUTH_MODE_INVALID")
    if mode == "withheld":
        return None
    relative = CHANNEL_PROFILE.asset_root.relative_to(CHANNEL_PROFILE.repo_root)
    path = Path(root) / relative / AUTHORITY_DIRECTORY / (candidate_id + AUTHORITY_SUFFIX)
    if path.exists() or path.is_symlink():
        _unavailable()
    return None

def resolve_candidate_public_text_staging(*, candidate_id, root=ROOT, **_kwargs):
    load_candidate_public_text_surface_authority(candidate_id, root=root)
    return None

def candidate_public_text_result_projection(*, work_dir, publish_path, publish):
    try:
        load_candidate_public_text_surface_authority(work_dir.name)
    except CandidatePublicTextSurfaceAuthorityError as exc:
        return {"hook": "", "title": None,
                "title_authority_status": "UNRESOLVED_PUBLIC_TEXT_RESULT_PROJECTION",
                "title_authority_error": f"candidate_public_text_result_projection_failed:{exc}"}
    return {}

def resolve_candidate_public_cover_text(*, candidate_id, default_cover_text):
    load_candidate_public_text_surface_authority(candidate_id)
    return default_cover_text

consume_candidate_public_text_surface_authority = _unavailable
validated_candidate_public_text_result_hook = _unavailable
build_public_text_source_fact_context = _unavailable

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
    public_text_staging_resolver: Callable[..., CandidatePublicTextStagingResolution | None] | None = None,
) -> CandidatePublicTextTitleState:
    """Resolve the narrow exact substitution before any model-generated title."""

    try:
        resolution = resolve_candidate_public_text_staging(
            candidate_id=candidate_id,
            selection_hook=selection_hook,
            story_contract=story_contract,
            story_contract_rebuilder=story_contract_rebuilder,
            public_text_staging_resolver=public_text_staging_resolver,
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
            title_source=resolution.title_source,
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
            title_source="operator_manual_override",
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
