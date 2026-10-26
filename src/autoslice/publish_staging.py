"""Fail-closed title, AI-cover, and publish-draft staging.

Prepares local evidence only; every document keeps upload disabled behind the release gate.
"""

from __future__ import annotations

from src.autoslice.structured_output import response_contract
import json
import hashlib
import os
import re
import subprocess
from copy import deepcopy
from dataclasses import asdict, replace as dataclasses_replace
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Callable, Mapping, NamedTuple, Sequence

from .auto_review import DecisionAction, ReviewDecision
from .channel_profile import load_channel_profile
from .chat_authority import canonicalize_hard_surfaces
from .candidate_entity_publish_gate import (
    enforce_candidate_cover_projection,
    evaluate_candidate_title_gates,
)
from .candidate_public_text_surface_authority import (
    resolve_candidate_public_cover_text,
    resolve_candidate_public_text_title_state,
)
from .content_ip_signal import important_content_ip_signal_from_srt
from .cover_emote import (
    EmoteLibrary,
    compose_companion_reference,
    load_emote_library,
    resolve_emote_reference,
)
from .cover_frame_selection import (
    DEFAULT_SKIP_HEAD_MS,
    extract_zoomed_cover_frame,
    select_expressive_cover_frame,
)
from .cover_generation import (
    _lidousha_is_song_title,
    LidoushaCoverArtDirection,
    _call_cpa_image_edit as _cover_call_cpa_image_edit,
    _cpa_image_model_candidates,
    _cover_art_direction,
    _cover_prompt,
    _cover_text,
    _overlay_cover_title,
    ordinary_talk_redraw_style_binding,
    ordinary_talk_redraw_text_palette,
)
from .cover_route_evidence import (
    build_no_crop_participant_verification,
    build_cover_route_decision,
    validate_cover_route_decision,
    is_hash_bound_reference_authority,
    relationship_source_participants_verified,
    relationship_visual_safety_required,
    record_cover_route_execution,
    source_visible_participant_ids,
    story_participant_ids,
    validate_final_participant_verification,
)
from .cover_scene_binding import run_final_host_identity_witness, run_source_composition_witness
from .cover_route_policy import decide_cover_treatment as _decide_cover_treatment
from .cover_source_composition import (
    extract_authority_source_crop,
    extract_authority_source_crop_or_full_frame,
    read_source_frame_size,
    source_composition_scene_kind,
    source_composition_recommends_redraw,
    validate_source_composition_verification,
    verify_source_composition,
    vertical_source_decision_inputs,
)
from .cover_host_identity_gate import (
    story_contract_requires_host_only_final,
    final_host_identity_witness_unavailable,
    validate_final_host_identity_verification,
    verify_final_host_identity,
)
from .cover_polish_gate import (
    _compose_screenshot_cover_with_face_gate,
    _degrade_rejected_polish_to_direct,
    _materialize_screenshot_polish,
    _polish_face_binding_failure,
    _verify_polish_face_integrity,
)
from .cover_punch_semantics import (
    COVER_THUMBNAIL_MAX_LINES,
    PUNCH_LINE_MAX_EM,
    cover_text_requires_punch_for_thumbnail,
    talk_cover_thumbnail_gate_violations,
    validate_full_text_cover_contract,
)
from .fixed_cover_stage import FixedCoverStageOptions, fixed_art_direction, resolved_cover_mode
from .llm_client import (
    LlmCall,
    LlmRuntimeEnvironmentError,
    extract_json_object,
    runtime_cpa_command_environment,
)
from .normal_text_generation_checkpoint import completed_generation, subtitle_dependency
from .manual_title_repair_authority import (
    ManualTitleRepairAuthorityError,
    load_manual_title_repair_authority,
    validate_manual_title_repair_authority,
)
from .publish_staging_paths import (
    current_editorial_binding,
    editorial_input_binding,
    validate_current_editorial_binding,
    materialized_artifact_root,
    private_publish_path,
    stage_cover_with_private_root,
    staged_transcript_sample,
)
from .manual_title_keep_authority import PASS_DECISION as MANUAL_TITLE_KEEP_PASS_DECISION
from .producer_final_review_transport import _resolved_runtime_root
from .review_evidence import SourceCue
from .recovery_title_authority import (
    RecoveryTitleAuthorityError,
    validate_recovery_publication_authority,
)
from .shadow_review import _sha256, _write_json_file
from .source_fact_review import (
    DETERMINISTIC_TEXT_NARROWING_PASS_DECISION,
    EDITORIAL_RESEARCH_METHOD_PROVENANCE,
    authorize_manual_title_repair,
    source_fact_review_passes,
)
from .source_fact_staging import resolve_initial_source_fact_review
from .source_fact_rescore_provenance import publish_staging_provenance_fields
from .normal_first_publication_media_successor import (
    NormalFirstPublicationMediaSuccessorError,
    PRIVATE_REUSE_SCHEMA_VERSION,
    build_private_reuse_generation,
)
from .story_contract import cover_relation_prompt, cover_story_contract_binding
from .channel_envelope import story_contract_partners
from .title_policy import (
    _TITLE_MAX_ATTEMPTS,
    _TITLE_MAX_LEN,
    _TITLE_MIN_LEN,
    _ensure_title_prefix,
    build_automatic_talk_title_prompt,
    canonicalize_automatic_title_fillers,
    canonicalize_publish_title,
    canonicalize_song_catalog_title,
    manual_title_override,
    publish_title_lane,
    _title_policy_violations,
)

ROOT = Path(__file__).resolve().parents[2]
CHANNEL_PROFILE = load_channel_profile(ROOT)
PROFILE_ID = CHANNEL_PROFILE.profile_id


def _resolve_cover_cpa_credentials() -> tuple[str, str]:
    """Resolve the image-edit credentials without weakening the cover gate.

    A complete explicit ambient pair remains the historical operator override.
    Normal producers clear that ambient pair and bind the same runtime-owned
    ``cpa.env`` used by final-review CPA calls.  Invalid or missing runtime
    credentials deliberately return an incomplete pair so existing cover
    routes stay fail-closed; this helper never logs or persists the key.
    """

    ambient_base_url = os.environ.get("CPA_BASE_URL", "").strip().rstrip("/")
    ambient_api_key = os.environ.get("CPA_API_KEY", "").strip()
    if ambient_base_url and ambient_api_key:
        return ambient_base_url, ambient_api_key

    try:
        runtime = _resolved_runtime_root(None)
        runtime_env = (
            runtime_cpa_command_environment(runtime)
            if runtime is not None
            else {}
        )
    except (LlmRuntimeEnvironmentError, OSError, ValueError):
        runtime_env = {}
    if not isinstance(runtime_env, Mapping):
        runtime_env = {}
    runtime_base_url = str(runtime_env.get("CPA_BASE_URL") or "").strip().rstrip("/")
    runtime_api_key = str(runtime_env.get("CPA_API_KEY") or "").strip()
    if runtime_base_url and runtime_api_key:
        return runtime_base_url, runtime_api_key
    return ambient_base_url, ambient_api_key


def profile_asset_text(key: str) -> str:
    try:
        return CHANNEL_PROFILE.asset_file(key, repo_root=ROOT).read_text(encoding="utf-8").strip()
    except OSError:
        return "(资产文件缺失)"


LIDOUSHA_COVER_WORKFLOW = "cpa-openai-compatible-image-edit-cover-plus-approved-local-title-overlay"


class _AutomaticTitleResult(NamedTuple):
    staged_title: str | None
    title_source: str
    title_authority_status: str | None
    title_authority_error: str | None
    title_policy_violations: list[str]
    title_generation_attempts: list[dict[str, object]] | None = None


def _json_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _sha256_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _bounded_source_entity_candidates(value: object) -> list[dict[str, object]]:
    """Keep explicit entity candidates in the existing research projection.

    This is a projection of already-authorized source/research input.  It is
    intentionally narrow: the normal tag consumer may read these rows, while
    identity spelling rules, free-form report prose, and guessed ASR names do
    not become tag authority by being copied into a publish draft.
    """

    if not isinstance(value, list):
        return []
    rows: list[dict[str, object]] = []
    for raw in value[:12]:
        if not isinstance(raw, Mapping):
            continue
        row: dict[str, object] = {}
        for key in (
            "tag",
            "role",
            "editorial_role",
            "content_role",
            "status",
            "surface",
            "applicability",
            "inapplicable_reason",
            "why",
            "research_basis",
        ):
            if key in raw and raw[key] is not None:
                value_item = raw[key]
                if isinstance(value_item, (str, int, float, bool)):
                    row[key] = str(value_item).strip() if isinstance(value_item, str) else value_item
        for key in ("surfaces", "evidence"):
            values = raw.get(key)
            if isinstance(values, (list, tuple)):
                row[key] = [str(item).strip()[:240] for item in values[:8] if str(item).strip()]
        source = raw.get("source")
        if isinstance(source, Mapping):
            bounded_source = {
                key: str(source[key]).strip()[:512]
                for key in (
                    "path",
                    "sha256",
                    "url",
                    "source_kind",
                    "candidate_id",
                    "application_id",
                    "context_sha256",
                    "srt_sha256",
                    "final_transcript_sha256",
                    "clip_context_prompt_sha256",
                    "request_sha256",
                    "response_sha256",
                )
                if source.get(key) is not None and str(source[key]).strip()
            }
            if bounded_source:
                row["source"] = bounded_source
        if row.get("tag") and row.get("status"):
            rows.append(row)
    return rows


def _normalize_editorial_research(value: object) -> dict[str, object] | None:
    """Keep a bounded, hashable receipt for already completed editorial research.

    Research is supplied as data by the caller (usually on the story contract).
    It remains advisory editorial input: source-fact review still owns factual
    support.  The normal consumer records the binding and the small set of
    candidate title applications so a later reviewer can tell which research
    was actually consumed.
    """

    if not isinstance(value, Mapping):
        return None
    applications = value.get("applications")
    if applications is None:
        applications = value.get("candidate_applications")
    kept_applications: list[dict[str, object]] = []
    if isinstance(applications, list):
        for row in applications:
            if not isinstance(row, Mapping):
                continue
            kept: dict[str, object] = {}
            for key in (
                "id",
                "name",
                "status",
                "selection_priority",
                "first_title",
                "alternative_title",
                "answerable_gap",
                "conditions",
                "rejected_version",
                "unused_tags",
                "tags",
            ):
                if key in row:
                    kept[key] = row[key]
            entity_candidates = row.get("source_entity_candidates")
            if entity_candidates is None:
                entity_candidates = row.get("entity_candidates")
            bounded_entity_candidates = _bounded_source_entity_candidates(entity_candidates)
            if bounded_entity_candidates:
                kept["source_entity_candidates"] = bounded_entity_candidates
            comparison_candidates = row.get("comparison_candidates")
            if isinstance(comparison_candidates, list):
                bounded_candidates: list[dict[str, str]] = []
                for candidate in comparison_candidates[:6]:
                    if not isinstance(candidate, Mapping):
                        continue
                    bounded_candidate = {
                        key: str(candidate[key]).strip()
                        for key in ("id", "title", "basis", "status", "reason")
                        if candidate.get(key) is not None and str(candidate[key]).strip()
                    }
                    if "title" in bounded_candidate:
                        bounded_candidates.append(bounded_candidate)
                if bounded_candidates:
                    kept["comparison_candidates"] = bounded_candidates
            if kept:
                kept_applications.append(kept)
    normalized: dict[str, object] = {}
    for key in (
        "schema",
        "schema_version",
        "path",
        "sha256",
        "source_path",
        "source_sha256",
        "candidate_id",
        "application_id",
        "version",
        "source_kind",
        "asset_key",
        "applicable",
        "applicability",
        "applicable_constraints",
        "inapplicable_reason",
        "status",
        "scope",
        "asset_bindings",
        "method",
        "research_method_sources",
    ):
        if key in value and value[key] is not None:
            if key == "research_method_sources":
                rows = value[key]
                if isinstance(rows, (list, tuple)):
                    bounded_sources: list[dict[str, str]] = []
                    for raw_source in rows[:6]:
                        if not isinstance(raw_source, Mapping):
                            continue
                        bounded_source = {
                            source_key: str(raw_source[source_key]).strip()[:1024]
                            for source_key in (
                                "id",
                                "schema",
                                "version",
                                "path",
                                "sha256",
                                "url",
                                "role",
                            )
                            if raw_source.get(source_key) is not None
                            and str(raw_source[source_key]).strip()
                        }
                        if bounded_source:
                            bounded_sources.append(bounded_source)
                    if bounded_sources:
                        normalized[key] = bounded_sources
                continue
            normalized[key] = value[key]
    candidate_projection = value.get("candidate_projection")
    if isinstance(candidate_projection, Mapping):
        bounded_projection: dict[str, object] = {}
        for key in (
            "status",
            "source_kind",
            "path",
            "source_sha256",
            "candidate_id",
            "application_id",
            "version",
            "applicability",
            "inapplicable_reason",
        ):
            if key in candidate_projection and candidate_projection[key] is not None:
                bounded_projection[key] = candidate_projection[key]
        if bounded_projection:
            projection_candidates = candidate_projection.get("source_entity_candidates")
            if projection_candidates is None:
                projection_candidates = candidate_projection.get("entity_candidates")
            bounded_projection_candidates = _bounded_source_entity_candidates(
                projection_candidates
            )
            if bounded_projection_candidates:
                bounded_projection["source_entity_candidates"] = bounded_projection_candidates
            normalized["candidate_projection"] = bounded_projection
    top_entity_candidates = value.get("source_entity_candidates")
    if top_entity_candidates is None:
        top_entity_candidates = value.get("entity_candidates")
    bounded_top_entity_candidates = _bounded_source_entity_candidates(top_entity_candidates)
    if bounded_top_entity_candidates:
        normalized["source_entity_candidates"] = bounded_top_entity_candidates
    if kept_applications:
        normalized["applications"] = kept_applications
    if not normalized:
        return None
    normalized["input_sha256"] = _json_sha256(normalized)
    return normalized


def _profile_editorial_research_projection() -> dict[str, object] | None:
    """Bind the deployed, profile-owned normal editorial method.

    A source-specific research projection is optional.  The normal title/tag
    consumer still has a real, deployed method in the profile assets, so a
    missing candidate report must not be represented as if that whole method
    were inapplicable.  This receipt only records the existing assets; it does
    not create a second research store or read undeployed reports.
    """

    bindings: list[dict[str, object]] = []
    for asset_key in ("title_style", "upload_tag_policy"):
        try:
            path = CHANNEL_PROFILE.asset_file(asset_key, repo_root=ROOT)
            data = path.read_bytes()
        except (OSError, KeyError, ValueError):
            return None
        bindings.append(
            {
                "asset_key": asset_key,
                "path": str(path.relative_to(ROOT)),
                "sha256": "sha256:" + hashlib.sha256(data).hexdigest(),
                "bytes": len(data),
            }
        )
    binding_sha256 = _json_sha256(bindings)
    return _normalize_editorial_research(
        {
            "schema": "editorial-research-consumption.v1",
            "schema_version": "profile-assets-fallback.v1",
            "version": "title-tag-editorial-method.v1",
            "source_kind": "deployed_profile_assets",
            "path": "profiles/lidousha/profile.json#assets.title_style,assets.upload_tag_policy",
            "source_path": "profiles/lidousha/profile.json#assets.title_style,assets.upload_tag_policy",
            "sha256": binding_sha256,
            "source_sha256": binding_sha256,
            "asset_key": "title_style+upload_tag_policy",
            "asset_bindings": bindings,
            "method": "normal_title_tag_editorial_selection",
            "applicable": True,
            "applicability": (
                "The deployed profile title-style and upload-tag policy provide the bounded "
                "normal title/tag editorial method for every ordinary record; a source-specific "
                "candidate application may be absent and is handled separately."
            ),
            "applicable_constraints": [
                "Use final SRT and same-clip context as factual authority.",
                "Use source-specific research applications only when explicitly hash-bound.",
                "Do not read undeployed reports or invent research candidates.",
            ],
            "research_method_sources": [
                dict(row) for row in EDITORIAL_RESEARCH_METHOD_PROVENANCE
            ],
            "inapplicable_reason": (
                "No source-specific editorial research projection is bound to this ordinary record; "
                "only the candidate-specific research layer is unavailable."
            ),
            "status": "APPLICABLE",
            "scope": "normal_title_tags",
        }
    )


def _editorial_research_from_record(
    record: Mapping[str, object], story_contract: object
) -> dict[str, object] | None:
    candidates: list[object] = [record.get("editorial_research")]
    record_scorecard = record.get("selection_scorecard")
    if isinstance(record_scorecard, Mapping):
        candidates.append(record_scorecard.get("editorial_research"))
    if isinstance(story_contract, Mapping):
        candidates.extend(
            [
                story_contract.get("editorial_research"),
                (
                    story_contract.get("selection_scorecard", {}).get("editorial_research")
                    if isinstance(story_contract.get("selection_scorecard"), Mapping)
                    else None
                ),
            ]
        )
    for candidate in candidates:
        normalized = _normalize_editorial_research(candidate)
        if normalized is not None:
            return normalized
    # Ordinary producers already write the selection scorecard, but they do
    # not all have a separate source-specific research writer.  Consume the
    # existing deployed profile method and disclose the missing candidate
    # projection in its inapplicable_reason instead of marking the method
    # itself inapplicable.
    scorecard: Mapping[str, object] | None = None
    if isinstance(record_scorecard, Mapping) and record_scorecard:
        scorecard = record_scorecard
    elif isinstance(story_contract, Mapping):
        story_scorecard = story_contract.get("selection_scorecard")
        if isinstance(story_scorecard, Mapping) and story_scorecard:
            scorecard = story_scorecard
    projection = _profile_editorial_research_projection()
    if projection is None:
        return None
    scorecard_sha256 = _json_sha256(dict(scorecard)) if scorecard is not None else _json_sha256(
        {"status": "MISSING_RECORD_SELECTION_SCORECARD"}
    )
    missing_reason = (
        "record and story_contract contain no selection_scorecard or editorial_research field; "
        "no report or undeployed research path is read as a fallback."
        if scorecard is None
        else "record and story_contract contain no editorial_research field; no report or undeployed "
        "research path is read as a fallback."
    )
    projection["candidate_projection"] = {
        "status": "INAPPLICABLE",
        "source_kind": "runtime_record_field",
        "path": "record.selection_scorecard",
        "source_sha256": scorecard_sha256,
        "applicability": "No source-specific candidate applications are bound to this record.",
        "inapplicable_reason": missing_reason,
    }
    projection["input_sha256"] = _json_sha256(projection)
    return projection


def _source_entity_candidate_sections(
    record: Mapping[str, object],
    story_contract: object,
    editorial_research: Mapping[str, object] | None,
) -> list[tuple[str, Mapping[str, object]]]:
    """Return only ordinary record fields that may carry typed candidates.

    ``publish_staging`` is deliberately absent from this walk.  A previous
    ``.publish.json`` is an output, not authority for the next normal run.
    Likewise, the source-fact ``entity_context`` is identity/spelling-only;
    only an explicit candidate array with its own source binding can enter the
    tag candidate pool.
    """

    sections: list[tuple[str, Mapping[str, object]]] = []
    seen: set[int] = set()

    def add(label: str, value: object) -> None:
        if not isinstance(value, Mapping) or id(value) in seen:
            return
        seen.add(id(value))
        sections.append((label, value))

    add("record", record)
    add("record.source_fact_review", record.get("source_fact_review"))
    add("record.editorial_research", record.get("editorial_research"))
    scorecard = record.get("selection_scorecard")
    add("record.selection_scorecard", scorecard)
    if isinstance(scorecard, Mapping):
        add("record.selection_scorecard.editorial_research", scorecard.get("editorial_research"))
    add("story_contract", story_contract)
    if isinstance(story_contract, Mapping):
        add("story_contract.source_fact_review", story_contract.get("source_fact_review"))
        add("story_contract.editorial_research", story_contract.get("editorial_research"))
        story_scorecard = story_contract.get("selection_scorecard")
        add("story_contract.selection_scorecard", story_scorecard)
        if isinstance(story_scorecard, Mapping):
            add(
                "story_contract.selection_scorecard.editorial_research",
                story_scorecard.get("editorial_research"),
            )
    add("provided.editorial_research", editorial_research)
    # Candidate rows are often nested under the existing research receipt or
    # its bounded applications. Walk only these named containers; do not turn
    # arbitrary record prose into an authority scan.
    for label, section in list(sections):
        add(f"{label}.editorial_research", section.get("editorial_research"))
        add(f"{label}.candidate_projection", section.get("candidate_projection"))
        for key in ("applications", "candidate_applications"):
            rows = section.get(key)
            if isinstance(rows, (list, tuple)):
                for index, row in enumerate(rows[:8]):
                    add(f"{label}.{key}[{index}]", row)
    return sections


def _retained_editorial_boundary(original: object, current: object) -> object:
    """Adopt the original complete boundary record when only its prose differs."""
    if not isinstance(original, Mapping) or not isinstance(current, Mapping):
        raise ValueError('RETAINED_EDITORIAL_SOURCE_DRIFT')
    if ({k: v for k, v in original.items() if k != 'summary'}
            != {k: v for k, v in current.items() if k != 'summary'}):
        raise ValueError('RETAINED_EDITORIAL_SOURCE_DRIFT')
    return deepcopy(dict(original))


def _retained_cover_editorial_stage(*, record: Mapping, story: object,
        candidate_id: str, title: str, selection_hook: str, artifact_root: Path) -> dict | None:
    """Recover the formal stage that produced explicitly retained cover pixels."""
    from .normal_cover_generation_checkpoint import retained_editorial_identity, file_identity
    pairs = [(os.environ.get(key, ''), os.environ.get(key + '_SHA256', ''), successful)
        for key, successful in (('AUTOSLICE_REVIEW_COVER_FAILURE', False),
                                ('AUTOSLICE_REUSE_COVER_RESULT', True))]
    active = [(path, sha, success) for path, sha, success in pairs if path or sha]
    if not active:
        return None
    if len(active) != 1 or not all(active[0][:2]):
        raise ValueError('COVER_REVIEW_ORIGIN_BINDING_INCOMPLETE')
    path, sha, successful = active[0]
    old = retained_editorial_identity(path=Path(path), expected_sha256=sha,
        artifact_root=artifact_root, candidate_id=candidate_id, successful=successful)
    previous = old.get('full_story_contract')
    if not isinstance(previous, Mapping) or not isinstance(story, Mapping):
        raise ValueError('RETAINED_EDITORIAL_SOURCE_DRIFT')
    fact = previous.get('source_fact_review') or {}
    previous_media = old.get('media')
    if isinstance(previous_media, Mapping):
        previous_media = {k: v for k, v in previous_media.items() if k != 'path'}
    current_media = file_identity(Path(str(record['media_path'])))
    if (fact.get('original_title') != title or fact.get('original_selection_hook') != selection_hook
            or previous_media != {k: v for k, v in current_media.items() if k != 'path'}
            or old.get('burned_preview') != json.loads(json.dumps(record.get('burned_preview')))
            or (old.get('editorial_input') or {}).get('subtitle_sha256')
                != file_identity(Path(str(record['subtitle_path'])))['sha256']
            or any(previous.get(key) != story.get(key) for key in (
                'schema_version', 'candidate_id', 'source_media_sha256s', 'clip_context_binding',
                'clip_context_prompt', 'human_boundary_authority', 'session_relation_authority'))):
        raise ValueError('RETAINED_EDITORIAL_SOURCE_DRIFT')
    _retained_editorial_boundary(previous.get('boundary_semantic_review'), story.get('boundary_semantic_review'))
    return dict(previous)


def _restore_reviewed_scorecard_input(story_contract: object, *, allow_pending_rescore: bool = False) -> object:
    """Recover only the checksum-proven input before our post-review projection.

    Older staging overwrote the reviewed research input with derived entity-tag
    output. The original pass keeps that input verbatim; quantities, method and
    asset bindings must still agree before restoring it. Full source-fact
    validation remains responsible for accepting the original receipt.
    """
    if not isinstance(story_contract, Mapping):
        return story_contract
    review = story_contract.get("source_fact_review")
    scorecard = story_contract.get("selection_scorecard")
    if not isinstance(review, Mapping) or not isinstance(scorecard, Mapping):
        return story_contract
    if review.get("decision") not in ({"KEEP", "REPAIRED", "REPAIR_SCORECARD_STALE"}
            if allow_pending_rescore else {"KEEP", "REPAIRED"}):
        return story_contract
    passes = review.get("passes")
    if not isinstance(passes, list) or not passes or not isinstance(passes[-1], Mapping):
        return story_contract
    original = passes[-1].get("editorial_research")
    current = scorecard.get("editorial_research")
    if not isinstance(original, Mapping) or not isinstance(current, Mapping):
        return story_contract
    projection = current.get("candidate_projection")
    if not isinstance(projection, Mapping) or (
        projection.get("schema") != "normal-source-entity-candidates.v1"
        or projection.get("source_kind") != "publish_staging_verified_context"
    ):
        return story_contract
    derived_fields = {"candidate_projection", "source_entity_candidates", "input_sha256"}
    if {k: v for k, v in original.items() if k not in derived_fields} != {
        k: v for k, v in current.items() if k not in derived_fields
    }:
        return story_contract
    recovered = deepcopy(dict(scorecard))
    recovered["editorial_research"] = deepcopy(dict(original))
    if _json_sha256(recovered) != passes[-1].get("selection_scorecard_sha256"):
        return story_contract
    restored = dict(story_contract)
    restored["selection_scorecard"] = recovered
    return restored


def _bind_normal_source_entity_projection(
    *,
    normalized_research: Mapping[str, object] | None,
    record: Mapping[str, object],
    story_contract: object,
    title: str,
    final_srt_text: str,
    final_srt_sha256: str | None,
) -> dict[str, object] | None:
    """Project verified source entities from the ordinary staging input.

    This is intentionally stricter than the tag prompt.  Candidate rows must
    already be typed, CONFIRMED/VERIFIED, exactly surface-bound to the current
    title or final SRT, and carry an explicit source path/URL plus digest and
    evidence.  Identity-only context, free-form research prose, and old
    publish outputs therefore produce an honest ``INAPPLICABLE`` projection
    instead of silently becoming proper-noun authority.
    """

    if normalized_research is None:
        return None
    title_sha256 = _sha256_text(title)
    body = str(final_srt_text or "")
    compact_body = "".join(body.split())
    sections = _source_entity_candidate_sections(
        record, story_contract, normalized_research
    )
    source_fact_pass_bindings: set[tuple[str, str, str, str]] = set()
    for section_label, section in sections:
        if "source_fact_review" not in section_label:
            continue
        passes = section.get("passes")
        if not isinstance(passes, list) or not passes or not isinstance(passes[-1], Mapping):
            continue
        final_pass = passes[-1]
        binding = tuple(
            str(final_pass.get(key) or "")
            for key in (
                "final_transcript_sha256",
                "clip_context_prompt_sha256",
                "request_sha256",
                "response_sha256",
            )
        )
        if all(re.fullmatch(r"sha256:[0-9a-f]{64}", value) for value in binding):
            source_fact_pass_bindings.add(binding)
    raw_rows: list[tuple[str, Mapping[str, object]]] = []
    for section_label, section in sections:
        for key in (
            "source_entity_candidates",
            "entity_candidates",
            "verified_source_entity_candidates",
            "verified_entity_candidates",
        ):
            values = section.get(key)
            if not isinstance(values, (list, tuple)):
                continue
            raw_rows.extend(
                (f"{section_label}.{key}[{index}]", row)
                for index, row in enumerate(values[:12])
                if isinstance(row, Mapping)
            )

    candidates: list[dict[str, object]] = []
    rejected_reasons: list[str] = []
    seen_tags: set[str] = set()
    for row_path, raw in raw_rows:
        tag = str(raw.get("tag") or "").strip()
        status = str(raw.get("status") or "").strip().upper()
        role = str(raw.get("role") or raw.get("entity_role") or "").strip()
        surfaces_value = raw.get("surfaces")
        if surfaces_value is None:
            surfaces_value = [raw.get("surface")]
        surfaces = (
            [str(surface).strip() for surface in surfaces_value if str(surface).strip()]
            if isinstance(surfaces_value, (list, tuple))
            else []
        )
        source = raw.get("source")
        source_map = dict(source) if isinstance(source, Mapping) else {}
        source_location = str(source_map.get("path") or source_map.get("url") or "").strip()
        source_digest = str(
            source_map.get("sha256") or source_map.get("source_sha256") or ""
        ).strip()
        evidence_value = raw.get("evidence")
        evidence = (
            [str(item).strip() for item in evidence_value if str(item).strip()]
            if isinstance(evidence_value, (list, tuple))
            else []
        )
        matched = [
            surface
            for surface in surfaces
            if surface in title or surface in body or surface.replace(" ", "") in compact_body
        ]
        reason: str | None = None
        if not final_srt_sha256:
            reason = "current-final-srt-hash-missing"
        elif not tag or not role or status not in {"CONFIRMED", "VERIFIED"}:
            reason = "tag-role-status"
        elif not surfaces or not matched:
            reason = "surface-not-in-current-title-or-srt"
        elif not source_location or not re.fullmatch(r"(?:sha256:)?[0-9a-fA-F]{64}", source_digest):
            reason = "source-path-or-digest-missing"
        elif source_map.get("source_kind") == "source_fact_cpa_final_srt" and (
            source_map.get("srt_sha256") != final_srt_sha256
            or source_digest != final_srt_sha256
            or not source_fact_pass_bindings
            or (
                str(source_map.get("final_transcript_sha256") or ""),
                str(source_map.get("clip_context_prompt_sha256") or ""),
                str(source_map.get("request_sha256") or ""),
                str(source_map.get("response_sha256") or ""),
            )
            not in source_fact_pass_bindings
            or any(
                not re.fullmatch(r"sha256:[0-9a-f]{64}", str(source_map.get(key) or ""))
                for key in (
                    "final_transcript_sha256",
                    "clip_context_prompt_sha256",
                    "request_sha256",
                    "response_sha256",
                )
            )
        ):
            reason = "source-fact-request-response-binding-missing-or-drifted"
        elif not evidence:
            reason = "evidence-missing"
        elif not any(
            item in title or item in body or item.replace(" ", "") in compact_body
            for item in evidence
        ):
            reason = "evidence-not-in-current-title-or-srt"
        elif tag.casefold() in seen_tags:
            continue
        if reason is not None:
            rejected_reasons.append(f"{row_path}:{reason}")
            continue
        normalized_row = dict(raw)
        normalized_row.update(
            {
                "tag": tag,
                "role": role,
                "status": status,
                "surfaces": surfaces,
                "matched_surfaces": matched,
                "title_sha256": title_sha256,
                "srt_sha256": final_srt_sha256,
            }
        )
        candidates.append(normalized_row)
        seen_tags.add(tag.casefold())

    source_input = {
        "title_sha256": title_sha256,
        "srt_sha256": final_srt_sha256,
        "candidate_row_count": len(raw_rows),
        "candidate_rows": raw_rows,
    }
    projection: dict[str, object] = {
        "schema": "normal-source-entity-candidates.v1",
        "status": "APPLIED" if candidates else "INAPPLICABLE",
        "source_kind": "publish_staging_verified_context",
        "path": "record/story_contract.source_fact_review.source_entity_candidates",
        "source_sha256": _json_sha256(source_input),
        "title_sha256": title_sha256,
        "srt_sha256": final_srt_sha256,
        "bound_title": title,
        "applicability": (
            "Typed source candidates are projected only when current final title/SRT, "
            "source binding, and exact evidence all agree."
        ),
    }
    if candidates:
        projection["source_entity_candidates"] = candidates
    else:
        projection["inapplicable_reason"] = (
            "No source-bound CONFIRMED/VERIFIED candidate survived current title/SRT and "
            "source/evidence checks; identity/spelling context and unstructured reports "
            "are not candidate authority."
        )
        if rejected_reasons:
            projection["rejected_candidates"] = rejected_reasons[:12]

    bound = dict(normalized_research)
    bound["candidate_projection"] = projection
    if candidates:
        bound["source_entity_candidates"] = candidates
    else:
        bound.pop("source_entity_candidates", None)
    bound["input_sha256"] = _json_sha256(bound)
    return bound


def _title_research_choice_receipt(payload: Mapping[str, object]) -> dict[str, object]:
    """Bound the optional candidate comparison emitted by the normal title call."""

    receipt: dict[str, object] = {}
    candidates = payload.get("candidates")
    if isinstance(candidates, list):
        bounded: list[dict[str, object]] = []
        for row in candidates[:6]:
            if not isinstance(row, Mapping):
                continue
            candidate: dict[str, object] = {}
            for key in ("title", "status", "rationale", "research_candidate_id"):
                if key in row and isinstance(row[key], (str, int, float, bool)):
                    candidate[key] = row[key]
            if candidate:
                bounded.append(candidate)
        if bounded:
            receipt["candidates"] = bounded
    choice = payload.get("editorial_choice")
    if isinstance(choice, Mapping):
        bounded_choice = {
            key: value
            for key, value in choice.items()
            if key in {"selected", "reason", "rejected"}
            and isinstance(value, (str, int, float, bool, list, tuple))
        }
        if bounded_choice:
            receipt["editorial_choice"] = bounded_choice
    for key in ("research_candidate_id", "research_decisions"):
        if key in payload:
            value = payload[key]
            if isinstance(value, (str, int, float, bool, list, tuple, dict)):
                receipt[key] = value
    return receipt


def _resolve_automatic_title(
    *,
    base_prompt: str,
    title_llm_call: LlmCall,
    selection_hook: str,
    initial_title_source: str,
    editorial_research_sha256: str | None = None,
    input_binding: Mapping[str, object] | None = None,
    generation_root: Path | None = None,
    generation_subtitle_path: Path | None = None,
    final_srt_text: str | None = None,
    envelope_partners: Sequence[str] | None = None,
) -> _AutomaticTitleResult:
    from .title_cover_quotes import check_title_candidates, check_title_quotes
    llm_title = ""
    llm_error: str | None = None
    violations: list[str] = []
    deterministic_filler_repair = False
    attempts: list[dict[str, object]] = []
    for attempt in range(_TITLE_MAX_ATTEMPTS):
        deterministic_filler_repair = False
        prompt = base_prompt
        if attempt > 0:
            prompt += (
                "\n注意：上一次标题违反了硬约束（违禁词、成对符号未闭合或长度），已被否决。"
                "不要用任何万能强调词，也不要把来源没有的事实写进标题；"
                "书名号、引号、括号必须左右成对；写她具体做了/说了什么，"
                "并按要求重新只输出 JSON。"
            )
        raw_response = None
        try:
            with response_contract():
                raw_response = (
                    completed_generation(
                        artifact_root=generation_root,
                        candidate_id=str((input_binding or {}).get("candidate_id") or ""),
                        lane="title", attempt=attempt + 1, prompt=prompt, call=title_llm_call,
                        dependencies=lambda: subtitle_dependency(generation_subtitle_path),
                    )
                    if generation_root is not None and generation_subtitle_path is not None
                    else title_llm_call(prompt)
                )
            payload = extract_json_object(raw_response)
        except Exception as exc:
            llm_error = type(exc).__name__
            attempts.append(
                {
                    "attempt": attempt + 1,
                    "request_sha256": _json_sha256(prompt),
                    "response_sha256": _json_sha256(raw_response) if isinstance(raw_response, str) else None,
                    "request_text": prompt,
                    "response_text": raw_response if isinstance(raw_response, str) else None,
                    "status": "CALL_FAILED",
                    "error": llm_error,
                    "input_binding": dict(input_binding) if input_binding is not None else None,
                    "editorial_research_sha256": editorial_research_sha256,
                }
            )
            break
        candidate = str(payload.get("title") or "").strip()
        if not candidate:
            llm_error = "empty_title"
            attempts.append(
                {
                    "attempt": attempt + 1,
                    "request_sha256": _json_sha256(prompt),
                    "response_sha256": _json_sha256(raw_response),
                    "request_text": prompt,
                    "response_text": raw_response,
                    "status": "EMPTY_TITLE",
                    "input_binding": dict(input_binding) if input_binding is not None else None,
                    "editorial_research_sha256": editorial_research_sha256,
                }
            )
            break
        raw_title = candidate
        research_choice = _title_research_choice_receipt(payload)
        skill_receipt = check_title_candidates(payload, final_srt_text) if final_srt_text is not None else None
        comparison_error = None
        if "candidates" in payload or "editorial_choice" in payload:
            rows = research_choice.get("candidates") or []
            selected = [row for row in rows if row.get("status") == "SELECTED"]
            choice = research_choice.get("editorial_choice") or {}
            if (
                len(selected) != 1
                or selected[0].get("title") != raw_title
                or choice.get("selected") != raw_title
                or not str(choice.get("reason") or "").strip()
            ):
                comparison_error = "title_candidate_selection_mismatch"
        repaired = canonicalize_automatic_title_fillers(candidate)
        if (
            repaired != candidate
            and not _title_policy_violations(repaired)
            and _TITLE_MIN_LEN
            <= len(_ensure_title_prefix(repaired, envelope_partners))
            <= _TITLE_MAX_LEN
        ):
            candidate = repaired
            deterministic_filler_repair = True
        llm_title = candidate
        violations = _title_policy_violations(candidate)
        if comparison_error:
            violations.append(comparison_error)
        if skill_receipt is not None:
            violations.extend(skill_receipt["reason_codes"])
            violations.extend(check_title_quotes(candidate, final_srt_text, declared_quote=skill_receipt.get("selected_quote"))["reason_codes"])
        attempts.append(
            {
                "attempt": attempt + 1,
                "request_sha256": _json_sha256(prompt),
                "response_sha256": _json_sha256(raw_response),
                "request_text": prompt,
                "response_text": raw_response,
                "status": "ACCEPTED" if not violations else "REJECTED_POLICY",
                "title": candidate,
                "raw_title": raw_title,
                "input_binding": dict(input_binding) if input_binding is not None else None,
                "title_policy_violations": list(violations),
                "editorial_research_sha256": editorial_research_sha256,
                **research_choice,
                **({"title_cover_skill": skill_receipt} if skill_receipt is not None else {}),
            }
        )
        if not violations:
            break

    title_source = initial_title_source
    if not llm_title:
        error = llm_error or "empty_title"
        return _AutomaticTitleResult(
            None,
            f"job_title(llm_failed: {error})",
            None,
            error,
            violations,
            attempts,
        )
    prefixed = _ensure_title_prefix(llm_title, envelope_partners)
    if not _TITLE_MIN_LEN <= len(prefixed) <= _TITLE_MAX_LEN:
        return _AutomaticTitleResult(
            None,
            f"job_title(llm_length_out_of_bounds:{len(prefixed)})",
            None,
            f"title_length_out_of_bounds:{len(prefixed)}",
            violations,
            attempts,
        )
    if deterministic_filler_repair:
        title_source = f"llm+{PROFILE_ID}_style_asset+deterministic_filler_removal"
    elif title_source == "job_title":
        title_source = f"llm+{PROFILE_ID}_style_asset"
    if violations:
        return _AutomaticTitleResult(
            prefixed,
            f"llm+{PROFILE_ID}_style_asset(title_policy_violation)",
            None,
            "title_policy_violation:" + ",".join(violations),
            violations,
            attempts,
        )
    status = (
        "RESOLVED_DETERMINISTIC_FILLER_REMOVAL"
        if deterministic_filler_repair
        else "RESOLVED_LLM"
    )
    return _AutomaticTitleResult(prefixed, title_source, status, None, violations, attempts)


def _stage_publish_after_release_gate(
    materialized_recut: dict[str, object] | None,
    *,
    decision: ReviewDecision,
    candidate_id: str,
    title: str,
    cues: Sequence[SourceCue],
    output_dir: Path,
    run_ffmpeg: bool,
    title_llm_call: LlmCall | None,
    art_direction_llm_call: LlmCall | None = None,
    stage_publish: Callable[..., dict[str, object] | None] | None = None,
) -> dict[str, object] | None:
    """Persist the final content decision before any title or cover side effect."""

    recut = dict(materialized_recut or {})
    reason_codes = list(decision.reason_codes)
    gate_satisfied = bool(
        decision.action == DecisionAction.AUTO_UPLOAD
        and not reason_codes
        and recut.get("status") == "MATERIALIZED"
    )
    snapshot_path = output_dir / f"{candidate_id}.cover-release-gate.json"
    snapshot = {
        "schema_version": "slice-cover-release-gate.v1",
        "candidate_id": candidate_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "decision_action": decision.action.value,
        "reason_codes": reason_codes,
        "materialized_recut_status": recut.get("status"),
        "artifact_hashes": dict(recut.get("artifact_hashes") or {}),
        "satisfied": gate_satisfied,
    }
    _write_json_file(snapshot_path, snapshot)
    if materialized_recut is None:
        # Keep the release decision snapshot as audit evidence, but do not
        # manufacture a recut-shaped record when the singing gate prevented
        # materialization in the first place.
        return None
    recut["cover_release_gate"] = {**snapshot, "path": str(snapshot_path)}

    if not gate_satisfied:
        recut["publish_staging"] = {
            "status": "SKIPPED_RELEASE_GATE",
            "decision_action": decision.action.value,
            "reason_codes": reason_codes,
            "release_gate_path": str(snapshot_path),
            "upload_enabled": False,
        }
        return recut

    staged = (stage_publish or _stage_publish_draft)(
        recut,
        candidate_id=candidate_id,
        title=title,
        cues=cues,
        run_ffmpeg=run_ffmpeg,
        title_llm_call=title_llm_call,
        art_direction_llm_call=art_direction_llm_call,
    )
    if staged is not None:
        staged = dict(staged)
        staged["cover_release_gate"] = recut["cover_release_gate"]
    return staged


def _recovery_publication_staging_state(
    *,
    candidate_id: str,
    title: str,
    title_llm_call: LlmCall | None,
    recovery_publication_authority: Mapping[str, object] | None,
) -> tuple[str, str, dict[str, object] | None]:
    source = "job_title"
    status = "RESOLVED_MANUAL" if title_llm_call is None else "UNRESOLVED_AUTO"
    if recovery_publication_authority is None:
        return source, status, None
    if title_llm_call is not None:
        raise ValueError("recovery publication authority requires a non-LLM title path")
    try:
        authority = validate_recovery_publication_authority(
            recovery_publication_authority,
            candidate_id=candidate_id,
            expected_final_title=title,
        )
    except RecoveryTitleAuthorityError as exc:
        raise ValueError(f"recovery publication authority invalid: {exc}") from exc
    if authority["title_mode"] == "reviewer_manual_override":
        return ("reviewer_manual_override", "RESOLVED_MANUAL", authority)
    return (
        "recovery_verified_same_bv_public_title",
        "RESOLVED_RECOVERY_PUBLIC",
        authority,
    )


def _stage_private_reused_cover(
    *,
    source_record: Path,
    candidate_id: str,
    title: str,
    story_contract: Mapping[str, object] | None,
) -> dict[str, object]:
    """Carry a verified private AI cover without inheriting public provenance."""

    try:
        generation = build_private_reuse_generation(
            source_record_path=source_record,
            candidate_id=candidate_id,
            title=title,
            story_contract=story_contract,
        )
    except (NormalFirstPublicationMediaSuccessorError, OSError, ValueError, TypeError) as exc:
        return {
            "status": "BLOCKED_COVER_REUSE",
            "cover_path": None,
            "cover_generation": {
                "status": "BLOCKED",
                "private_reuse_schema_version": PRIVATE_REUSE_SCHEMA_VERSION,
                "detail": f"private source cover binding invalid: {type(exc).__name__}",
            },
            "reason_codes": ["PRIVATE_COVER_REUSE_EVIDENCE_INVALID"],
        }
    cover_path = Path(str(generation.get("private_source_cover_path") or ""))
    cover_sha = str(generation.get("private_source_cover_sha256") or "")
    if not cover_path.is_file() or cover_path.is_symlink() or not cover_sha:
        return {
            "status": "BLOCKED_COVER_REUSE",
            "cover_path": None,
            "cover_generation": {
                **generation,
                "status": "BLOCKED",
                "detail": "private source cover artifact is unavailable",
            },
            "reason_codes": ["PRIVATE_COVER_REUSE_ARTIFACT_MISSING"],
        }
    return {
        "status": "REUSED_COVER",
        "cover_path": str(cover_path),
        "cover_sha256": cover_sha,
        "cover_generation": generation,
        "reason_codes": [],
    }

def _stage_publish_draft(
    materialized_recut: dict[str, object] | None,
    *,
    candidate_id: str,
    title: str,
    cues: Sequence[SourceCue],
    run_ffmpeg: bool,
    title_llm_call: LlmCall | None,
    art_direction_llm_call: LlmCall | None = None,
    skip_cover: bool = False,
    selection_hook: str | None = None,
    cover_diversity_slot: int | None = None,
    recovery_publication_authority: Mapping[str, object] | None = None,
    stage_cover: Callable[..., dict[str, object]] | None = None,
    source_fact_llm_call: LlmCall | None = None,
    story_contract_rebuilder: (Callable[[str], dict[str, object]] | None) = None,
    public_text_staging_resolver: Callable[..., object] | None = None,
    private_artifact_root: Path | None = None,
    private_publish_json_path: Path | None = None,
    reuse_cover_source_record: Path | None = None,
    enforce_final_host_identity: bool = False,
    editorial_research: Mapping[str, object] | None = None,
) -> dict[str, object] | None:
    """Mirror local_prepare while always keeping upload disabled."""
    if not materialized_recut or materialized_recut.get("status") != "MATERIALIZED":
        return materialized_recut
    record = dict(materialized_recut)
    if record.get("candidate_id") not in (None, candidate_id):
        raise ValueError("publish staging record candidate differs")
    record["candidate_id"] = candidate_id
    media_path = Path(str(record["media_path"]))
    publish_json_path = private_publish_path(media_path=media_path, private_artifact_root=private_artifact_root, private_publish_json_path=private_publish_json_path, stage_cover=stage_cover)
    staged_title = title
    important_content_ips: list[dict[str, object]] = []
    title_policy_violations: list[str] = []
    title_authority_error: str | None = None
    title_state = _recovery_publication_staging_state(
        candidate_id=candidate_id,
        title=title,
        title_llm_call=title_llm_call,
        recovery_publication_authority=recovery_publication_authority,
    )
    title_source, title_authority_status, normalized_recovery_publication_authority = title_state
    story_contract = record.get("story_contract")
    candidate_title = resolve_candidate_public_text_title_state(
        candidate_id=candidate_id,
        title=staged_title,
        selection_hook=str(selection_hook or ""),
        story_contract=story_contract,
        story_contract_rebuilder=story_contract_rebuilder,
        public_text_staging_resolver=public_text_staging_resolver,
        title_source=title_source,
        title_authority_status=title_authority_status,
        title_llm_call=title_llm_call,
        manual_title=manual_title_override(candidate_id),
    )
    staged_title, selection_hook, story_contract = candidate_title.title, candidate_title.selection_hook, candidate_title.story_contract
    title_source, title_authority_status = candidate_title.title_source, candidate_title.title_authority_status
    title_authority_error, title_llm_call = candidate_title.title_authority_error, candidate_title.title_llm_call
    public_text_surface_authority_consumption = candidate_title.consumption
    story_contract = _restore_reviewed_scorecard_input(story_contract)
    if public_text_surface_authority_consumption is not None:
        record["story_contract"] = story_contract
    normalized_editorial_research = _normalize_editorial_research(
        editorial_research
        if editorial_research is not None
        else _editorial_research_from_record(record, story_contract)
    )
    final_srt_text, final_srt_sha256 = _staged_final_srt_text_and_sha256(record, cues)
    if record.get("subtitle_path") and final_srt_sha256 is None:
        raise ValueError("CURRENT_EDITORIAL_SUBTITLE_INVALID")
    normalized_editorial_research = _bind_normal_source_entity_projection(
        normalized_research=normalized_editorial_research,
        record=record,
        story_contract=story_contract,
        title=staged_title,
        final_srt_text=final_srt_text,
        final_srt_sha256=final_srt_sha256,
    )
    if normalized_editorial_research is not None and isinstance(story_contract, Mapping):
        # Bind the research receipt into the existing scorecard field consumed
        # by source-fact review; no separate workflow/ledger is introduced.
        story_contract = dict(story_contract)
        scorecard = story_contract.get("selection_scorecard")
        if isinstance(scorecard, Mapping) and not story_contract.get("source_fact_review"):
            scorecard = dict(scorecard)
            scorecard["editorial_research"] = normalized_editorial_research
            story_contract["selection_scorecard"] = scorecard
        story_contract["editorial_research"] = normalized_editorial_research
        record["story_contract"] = story_contract
    title_generation_attempts: list[dict[str, object]] = []
    automatic_skill_required = (title_llm_call is not None and not _lidousha_is_song_title(staged_title)
                                and str(record.get("classification") or "").lower() != "song")
    # Automatic talk envelope = render(structured partners); the model writes only the body.
    auto_partners = story_contract_partners(story_contract) if automatic_skill_required else None
    from .title_cover_quotes import aligned_quote_subtitles, final_title_skill_receipt
    quote_srt = aligned_quote_subtitles(record, final_srt_text) if automatic_skill_required else final_srt_text
    if title_llm_call is not None:
        selection_hook = str(selection_hook or "").strip()
        important_ip_signal = important_content_ip_signal_from_srt(
            subtitle_path=record.get("subtitle_path"),
            fallback_body="\n".join(cue.text for cue in cues),
            title=selection_hook,
        )
        important_content_ips = important_ip_signal.as_receipt()
        base_prompt = build_automatic_talk_title_prompt(
            selection_hook=selection_hook,
            transcript_sample=staged_transcript_sample(record, cues),
            important_ip_prompt=important_ip_signal.prompt_block,
            clip_context_prompt=(
                str(story_contract.get("clip_context_prompt") or "").strip()
                if isinstance(story_contract, Mapping)
                else ""
            ),
            persona_asset=profile_asset_text("persona"),
            style_asset=profile_asset_text("title_style"),
            story_contract=story_contract,
            editorial_research=normalized_editorial_research,
            final_transcript=quote_srt,
            skill_required=automatic_skill_required,
            title_prefix=_ensure_title_prefix("", auto_partners),
        )
        automatic = _resolve_automatic_title(
            base_prompt=base_prompt,
            title_llm_call=title_llm_call,
            selection_hook=selection_hook,
            initial_title_source=title_source,
            final_srt_text=quote_srt if automatic_skill_required else None,
            envelope_partners=auto_partners,
            editorial_research_sha256=(
                str(normalized_editorial_research.get("input_sha256"))
                if normalized_editorial_research is not None
                else None
            ),
            input_binding=editorial_input_binding(
                record, candidate_id=candidate_id, title=staged_title,
                selection_hook=selection_hook, editorial_research=normalized_editorial_research,
            ),
            generation_root=(
                private_artifact_root or materialized_artifact_root(record, media_path)
                if record.get("subtitle_path") else None
            ),
            generation_subtitle_path=(
                Path(str(record["subtitle_path"])) if record.get("subtitle_path") else None
            ),
        )
        title_generation_attempts = list(automatic.title_generation_attempts or [])
        if automatic.staged_title is not None:
            staged_title = automatic.staged_title
        title_source = automatic.title_source
        title_authority_error = automatic.title_authority_error
        title_policy_violations = automatic.title_policy_violations
        if automatic.title_authority_status is not None:
            title_authority_status = automatic.title_authority_status
        # Shared publication choke point: only declared filler cleanup is allowed.
        choke_repaired_title = canonicalize_automatic_title_fillers(staged_title)
        if (
            choke_repaired_title != staged_title
            and not _title_policy_violations(choke_repaired_title)
            and _TITLE_MIN_LEN
            <= len(_ensure_title_prefix(choke_repaired_title, auto_partners))
            <= _TITLE_MAX_LEN
        ):
            staged_title = choke_repaired_title
            title_source = (
                f"llm+{PROFILE_ID}_style_asset+deterministic_filler_removal_at_publish_choke"
            )
            title_authority_status = "RESOLVED_DETERMINISTIC_FILLER_REMOVAL"
            title_authority_error = None
            title_policy_violations = []
    # Automatic titles receive deterministic surface canon. A human title body
    # is not silently rewritten; only the channel-owned publish envelope below
    # may be added.
    if title_llm_call is not None:
        staged_title = canonicalize_hard_surfaces(staged_title)



    if title_llm_call is not None:
        staged_title = canonicalize_song_catalog_title(staged_title)
    explicit_lane = publish_title_lane(
        staged_title,
        explicit_lane=(
            "song" if str(record.get("classification") or "").lower() == "song" else None
        ),
    )
    staged_title = canonicalize_publish_title(staged_title, lane=explicit_lane, partners=auto_partners)
    retained_editorial = _retained_cover_editorial_stage(record=record, story=story_contract,
        candidate_id=candidate_id, title=staged_title, selection_hook=str(selection_hook or ''),
        artifact_root=private_artifact_root or materialized_artifact_root(record, media_path))
    if retained_editorial is not None:
        boundary = retained_editorial['boundary_semantic_review']
        story_contract = dict(story_contract)
        story_contract['boundary_semantic_review'] = _retained_editorial_boundary(
            boundary, story_contract.get('boundary_semantic_review'))
        if story_contract_rebuilder is not None:
            original_rebuilder = story_contract_rebuilder
            def retained_rebuilder(hook):
                rebuilt = original_rebuilder(hook)
                rebuilt['boundary_semantic_review'] = _retained_editorial_boundary(
                    boundary, rebuilt.get('boundary_semantic_review'))
                return rebuilt
            story_contract_rebuilder = retained_rebuilder
    manual_title_repair_authority_consumption = None
    reviewed_scorecard_input = deepcopy(
        story_contract.get("selection_scorecard")
        if isinstance(story_contract, Mapping) else None
    )
    source_fact = resolve_initial_source_fact_review(
        candidate_id=candidate_id,
        title_source=title_source,
        title=staged_title,
        selection_hook=str(selection_hook or ""),
        story_contract=story_contract,
        record=record,
        cues=cues,
        source_fact_llm_call=source_fact_llm_call,
        recovery_publication_authority=normalized_recovery_publication_authority,
        title_authority_status=title_authority_status,
        title_llm_enabled=title_llm_call is not None,
        prior_authority_error=title_authority_error,
        completed_review=(retained_editorial.get('source_fact_review') if retained_editorial is not None else None),
    )
    source_fact_review = source_fact.review
    manual_title_keep_authority_consumption = source_fact.manual_title_keep_consumption
    if source_fact.violation is not None:
        title_policy_violations.append(source_fact.violation)
    title_authority_error = source_fact.authority_error
    title_authority_status = source_fact.authority_status
    if source_fact_review is not None:
        # Manual text remains immutable by default.  A checked-in authority
        # may unlock exactly one already-evidenced source-fact repair, bound
        # to its candidate, blocked CPA receipt, original surfaces, and exact
        # replacement surfaces.  It is not a general CPA override.
        if (
            not source_fact_review_passes(source_fact_review)
            and isinstance(source_fact_review, Mapping)
            and source_fact_review.get("decision") == "REPAIR_REQUIRES_TITLE_AUTHORITY"
            and title_source == "reviewer_manual_override"
        ):
            try:
                authority = load_manual_title_repair_authority(candidate_id)
                if authority is not None:
                    source_passes = source_fact_review.get("passes")
                    proposal = (
                        source_passes[0]
                        if isinstance(source_passes, list)
                        and len(source_passes) == 1
                        and isinstance(source_passes[0], Mapping)
                        else None
                    )
                    if isinstance(proposal, Mapping):
                        manual_title_repair_authority_consumption = (
                            validate_manual_title_repair_authority(
                                authority,
                                candidate_id=candidate_id,
                                original_title=staged_title,
                                original_selection_hook=str(selection_hook or ""),
                                source_fact_receipt_sha256=str(
                                    source_fact_review.get("receipt_sha256") or ""
                                ),
                                final_title=str(proposal.get("final_title") or ""),
                                final_selection_hook=str(
                                    proposal.get("final_selection_hook") or ""
                                ),
                            )
                        )
                        source_fact_review = authorize_manual_title_repair(
                            source_fact_review,
                            consumption=manual_title_repair_authority_consumption,
                        )
            except (ManualTitleRepairAuthorityError, ValueError):
                # Keep the pre-existing fail-closed source-fact block.  The
                # error is surfaced through its ordinary authority receipt.
                manual_title_repair_authority_consumption = None
        if not source_fact_review_passes(source_fact_review):
            reason = (
                str(
                    source_fact_review.get("reason_code")
                    or source_fact_review.get("decision")
                    or "unknown"
                )
                if isinstance(source_fact_review, Mapping)
                else "MANUAL_TITLE_KEEP_AUTHORITY_INVALID"
            )
            if "source_fact_review_failed" not in title_policy_violations:
                title_policy_violations.append("source_fact_review_failed")
            if title_authority_error is None:
                title_authority_error = "source_fact_review_failed:" + reason
            title_authority_status = "BLOCKED_SOURCE_FACT_REVIEW"
        else:
            reviewed_hook = str(source_fact_review["final_selection_hook"])
            reviewed_title = str(source_fact_review["final_title"])
            reviewed_lane = publish_title_lane(
                reviewed_title,
                explicit_lane=(
                    "song" if str(record.get("classification") or "").lower() == "song" else None
                ),
            )
            if canonicalize_publish_title(reviewed_title, lane=reviewed_lane) != reviewed_title:
                title_policy_violations.append("source_fact_repair_title_not_canonical")
                title_authority_error = (
                    "source_fact_review_failed:SOURCE_FACT_REPAIR_TITLE_NOT_CANONICAL"
                )
                title_authority_status = "BLOCKED_SOURCE_FACT_REVIEW"
            elif reviewed_hook != str(selection_hook or "") and story_contract_rebuilder is None:
                title_policy_violations.append("source_fact_hook_rebuild_unavailable")
                title_authority_error = (
                    "source_fact_review_failed:SOURCE_FACT_HOOK_REBUILD_UNAVAILABLE"
                )
                title_authority_status = "BLOCKED_SOURCE_FACT_REVIEW"
            else:
                if story_contract_rebuilder is not None:
                    story_contract = story_contract_rebuilder(reviewed_hook)
                elif isinstance(story_contract, Mapping):
                    story_contract = dict(story_contract)
                if isinstance(story_contract, dict):
                    if source_fact_review.get("decision") in {"KEEP", "REPAIRED"}:
                        story_contract["selection_scorecard"] = reviewed_scorecard_input
                    story_contract["source_fact_review"] = source_fact_review
                    record["story_contract"] = story_contract
                selection_hook = reviewed_hook
                staged_title = reviewed_title
                explicit_lane = reviewed_lane
                if source_fact_review.get("decision") == "REPAIRED":
                    title_source += "+cpa_source_fact_repair"
                    title_authority_status = (
                        "RESOLVED_MANUAL_SOURCE_FACT_REPAIR"
                        if manual_title_repair_authority_consumption is not None
                        else "RESOLVED_CPA_SOURCE_FACT_REPAIR"
                    )
                elif source_fact_review.get("decision") == MANUAL_TITLE_KEEP_PASS_DECISION:
                    title_authority_status = MANUAL_TITLE_KEEP_PASS_DECISION
                elif (
                    source_fact_review.get("decision") == DETERMINISTIC_TEXT_NARROWING_PASS_DECISION
                ):
                    title_source += "+deterministic_text_narrowing"
                    title_authority_status = "RESOLVED_DETERMINISTIC_TEXT_NARROWING"
    # Source-fact repair may replace the title/hook. Rebind the ordinary
    # candidate projection to the final title/SRT before title gates, cover
    # direction, and the publish draft consume it.
    normalized_editorial_research = _bind_normal_source_entity_projection(
        normalized_research=normalized_editorial_research,
        record=record,
        story_contract=story_contract,
        title=staged_title,
        final_srt_text=final_srt_text,
        final_srt_sha256=final_srt_sha256,
    )
    if normalized_editorial_research is not None and isinstance(story_contract, Mapping):
        story_contract = dict(story_contract)
        scorecard = story_contract.get("selection_scorecard")
        if isinstance(scorecard, Mapping) and not story_contract.get("source_fact_review"):
            scorecard = dict(scorecard)
            scorecard["editorial_research"] = normalized_editorial_research
            story_contract["selection_scorecard"] = scorecard
        story_contract["editorial_research"] = normalized_editorial_research
        record["story_contract"] = story_contract
    final_title_skill = (final_title_skill_receipt(staged_title, final_srt_text, quote_srt, title_generation_attempts)
                         if automatic_skill_required else None)
    # The receipt is always kept as a diagnostic.  It only takes authority over
    # a title that is otherwise resolved; an LLM failure, length block or
    # source-fact block keeps its own status (the replay consumer
    # validate_title_skill_selection already tolerates a failed quote check
    # when title_authority_error is set).
    if (
        final_title_skill is not None
        and final_title_skill["quote_check"]["status"] != "PASS"
        and title_authority_error is None
    ):
        title_policy_violations.extend(final_title_skill["quote_check"]["reason_codes"])
        title_authority_error = "TITLE_FINAL_QUOTE_CHECK_FAILED"
        title_authority_status = "BLOCKED_TITLE_QUOTE"
    title_gate = evaluate_candidate_title_gates(
        candidate_id=candidate_id,
        title=staged_title,
        lane=explicit_lane,
        enforce_automatic_style=title_llm_call is not None,
        prior_violations=title_policy_violations,
        prior_authority_error=title_authority_error,
        prior_authority_status=title_authority_status,
        story_contract=story_contract if isinstance(story_contract, Mapping) else None,
        source_fact_review=(
            source_fact_review if isinstance(source_fact_review, Mapping) else None
        ),
    )
    title_policy_violations = list(title_gate.violations)
    title_authority_error = title_gate.authority_error
    title_authority_status = title_gate.authority_status
    title_story_audit = title_gate.story_audit
    entity_projection_audit = title_gate.entity_projection_audit
    cover_text = resolve_candidate_public_cover_text(candidate_id=candidate_id, default_cover_text=_cover_text(staged_title))
    record["editorial_research"] = normalized_editorial_research
    if title_authority_error is not None:
        # A candidate id / job fallback is not publish-title authority.  Fail
        # before art direction or any paid image request; the runner will keep
        # this attempt as title_failed and retry it under the bounded policy.
        cover_result = {
            "status": "BLOCKED_TITLE_AUTHORITY",
            "cover_path": None,
            "cover_generation": {
                "status": "NOT_ATTEMPTED",
                "reason": "title authority unresolved before cover generation",
                "attempted_models": [],
            },
            "reason_codes": ["TITLE_AUTHORITY_UNRESOLVED"],
        }
    elif skip_cover and reuse_cover_source_record is not None:
        cover_result = _stage_private_reused_cover(
            source_record=reuse_cover_source_record,
            candidate_id=candidate_id,
            title=staged_title,
            story_contract=(story_contract if isinstance(story_contract, Mapping) else None),
        )
    elif skip_cover and normalized_recovery_publication_authority is None:
        cover_result = {
            "status": "BLOCKED_COVER_REUSE", "cover_path": None,
            "cover_generation": {"status": "BLOCKED", "carry_drop_reason": "same_bv_authority_missing"},
            "reason_codes": ["COVER_REUSE_PUBLICATION_AUTHORITY_MISSING"],
        }
    elif skip_cover:
        # Subtitle-only re-run: keep the existing delivered cover, skip the
        # expensive AI cover (art-direction LLM + gpt-image-2 ~90s/clip).
        #
        # （1013 jyl-r9 案）：reuse 此前不绑定被沿用封面的 sha，
        # record.artifact_hashes 里没有 cover_sha256，recovery review manifest
        # 的「delivery 封面 == record 封面」检查必然 REFUSE（r7 先例是重绘封面
        # 所以有绑定；reuse × recovery manifest 组合此前从未走通过）。既然语义
        # 是「沿用既有最终封面」，就把它找出来并绑定：恰好一个候选才绑，
        # 零个或多个都不猜——留空让下游 fail-closed。
        reused_cover_path: Path | None = None
        covers_dir = media_path.parent / "covers"
        reuse_matches = (
            sorted(covers_dir.glob(f"{candidate_id}.*.cover.png")) if covers_dir.is_dir() else []
        )
        if len(reuse_matches) == 1:
            reused_cover_path = reuse_matches[0]
        reused_sha = (
            "sha256:" + _sha256(reused_cover_path) if reused_cover_path is not None else None
        )
        # 已发布封面的完整证据包结转：recovery review
        # manifest 要求 record 携带合法 cover-route-decision.v2 等封面回执，而这些
        # 回执在**原次发布**的 record 里、逐一绑定同一份封面字节。字幕-only 重投
        # 复用同一字节时，诚实的表示就是结转原证据包——sidecar 的
        # final_cover_sha256 必须逐字节等于被复用的封面，否则不认（fail-closed，
        # 不新造回执、不放松审计门）。sidecar 由重跑装配从已发布 record 提取。
        carried_generation: dict[str, object] | None = None
        carry_drop_reason: str | None = None
        if len(reuse_matches) != 1:
            carry_drop_reason = (
                "reused_cover_missing" if not reuse_matches else "reused_cover_ambiguous"
            )
        if reused_cover_path is not None and reused_sha is not None:
            sidecar = reused_cover_path.with_name(f"{candidate_id}.published-cover-generation.json")
            if sidecar.is_file():
                try:
                    published_generation = json.loads(sidecar.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    published_generation = None
                # 只按 sha 相等决定是否进入结转流程；完整校验放在身份重打
                # **之后**——已发布回执的身份部分是摘要形态，先校验会让重打
                # 代码永远不可达（r16 案：鸡生蛋）。
                if (
                    isinstance(published_generation, dict)
                    and str(published_generation.get("final_cover_sha256") or "") == reused_sha
                ):
                    carried_generation = dict(published_generation)
                else:
                    carry_drop_reason = "published_sidecar_sha_mismatch"
            else:
                carry_drop_reason = "published_sidecar_missing"
        if carried_generation is not None:
            carried_generation.update(
                {
                    "status": "REUSED",
                    "note": (
                        "subtitle-only re-run: published cover evidence "
                        "carried forward byte-identically"
                    ),
                    "reused_cover_path": str(reused_cover_path),
                    "reused_cover_candidates": len(reuse_matches),
                    "carried_forward_from_published_record": True,
                }
            )
            # 字节证据结转，运行绑定重绑：StoryContract 投影是**本轮**的链接
            # （含 fresh 边界评审/clip-context sha），带旧的过来必然被审计判
            # STALE（r17 案）。
            if isinstance(story_contract, Mapping):
                carried_generation["story_contract"] = cover_story_contract_binding(story_contract)
            # 身份见证优先结转出版世代（v2 冻结条款，见
            # cover_host_identity_gate.PUBLISHED_CARRY_*）；仅当出版见证也
            # 过不了（形态残缺/哈希不符）才对同一份字节现场重打 CPA 见证。
            # 重打失败即丢弃整个结转（回落纯 REUSED 标记并披露丢弃原因），
            # 下游 manifest fail-closed——绝不带着假绿走。
            carried_route = carried_generation.get("route_decision")
            needs_identity = (
                isinstance(carried_route, Mapping)
                and carried_route.get("host_identity_required") is True
                and not validate_final_host_identity_verification(carried_generation)
            )
            if needs_identity and carried_generation.get("published_cover_carry_strict") is True:
                carry_drop_reason = "strict_published_carry_identity_invalid"
                carried_generation = None
            if needs_identity and carried_generation is not None:
                identity_reference = (
                        media_path.parent / "cover_refs" / f"{candidate_id}.cover-ref.png"
                )
                fresh_base_url, fresh_api_key = _resolve_cover_cpa_credentials()
                if identity_reference.is_file() and fresh_base_url and fresh_api_key:
                    try:
                        witness = run_final_host_identity_witness(
                            verify_final_host_identity,
                            final_cover_path=reused_cover_path,
                            final_cover_sha256=reused_sha,
                            reference_path=identity_reference,
                            base_url=fresh_base_url,
                            api_key=fresh_api_key, cover_generation=carried_generation,
                        )
                        carried_generation["final_host_identity_verification"] = dict(witness)
                    except Exception as exc:
                        carry_drop_reason = f"identity_refresh_error:{type(exc).__name__}"
                        carried_generation = None
                else:
                    carry_drop_reason = "identity_refresh_precondition_missing"
                    carried_generation = None
            # 终门：结转 bundle 的完整路由校验（含身份）决定去留。
            if carried_generation is not None and not (
                validate_cover_route_decision(carried_generation, allow_legacy_v1=False)
            ):
                carry_drop_reason = "carried_bundle_failed_route_validation"
                carried_generation = None
        carried_artifact_hashes: dict[str, str] = {}
        if carried_generation is not None:
            for result_key, generation_key in (
                ("ai_background_sha256", "ai_background_sha256"),
                ("cover_reference_sha256", "reference_sha256"),
            ):
                value = carried_generation.get(generation_key)
                if isinstance(value, str) and value:
                    carried_artifact_hashes[result_key] = value
        cover_result = {
            "status": (
                "REUSED_COVER" if carried_generation is not None else "BLOCKED_COVER_REUSE"
            ),
            "cover_path": (
                str(reused_cover_path)
                if carried_generation is not None and reused_cover_path is not None
                else None
            ),
            **({"cover_sha256": reused_sha} if reused_sha else {}),
            **carried_artifact_hashes,
            "cover_generation": (
                carried_generation
                if carried_generation is not None
                else {
                    "status": "BLOCKED",
                    "note": "requested existing cover has no validated carry evidence",
                    "reused_cover_path": (
                        str(reused_cover_path) if reused_cover_path is not None else None
                    ),
                    "reused_cover_candidates": len(reuse_matches),
                    **(
                        {"carry_drop_reason": carry_drop_reason}
                        if carry_drop_reason is not None
                        else {}
                    ),
                }
            ),
            "reason_codes": (
                [] if carried_generation is not None else ["COVER_REUSE_EVIDENCE_MISSING"]
            ),
        }
    else:
        requested_full_text_cover_contract = record.get("full_text_cover_contract")
        full_text_cover_contract = (
            dict(requested_full_text_cover_contract)
            if validate_full_text_cover_contract(
                requested_full_text_cover_contract,
                cover_text=cover_text,
            )
            else None
        )
        cover_kwargs: dict[str, object] = {
            "media_path": media_path,
            "candidate_id": candidate_id,
            "title": staged_title,
            "cover_text": cover_text,
            "run_ffmpeg": run_ffmpeg,
            "art_direction_llm_call": art_direction_llm_call,





            "punch_allowed": full_text_cover_contract is None,
            "full_text_cover_contract": full_text_cover_contract,
            "diversity_slot": cover_diversity_slot,
        }
        if enforce_final_host_identity:
            cover_kwargs.update(enforce_final_host_identity=True, final_host_identity_verifier=verify_final_host_identity)
        cover_result = stage_cover_with_private_root(stage_cover or _stage_ai_cover, record, private_artifact_root=private_artifact_root, kwargs=cover_kwargs)
    cover_result, cover_entity_projection_audit = enforce_candidate_cover_projection(
        candidate_id=candidate_id,
        cover_result=cover_result,
        projection_required=entity_projection_audit is not None,
    )
    cover_status = str(cover_result["status"])
    cover_path_value = (
        cover_result.get("cover_path")
        if cover_status in {"AI_COVER_READY", "REUSED_COVER"}
        else None
    )
    cover_generation = cover_result["cover_generation"]
    if cover_status == "REUSED_COVER" and isinstance(cover_generation, Mapping):
        reused_cover_text = cover_generation.get("cover_text")
        if isinstance(reused_cover_text, str) and reused_cover_text.strip():
            # Reusing verified pixels also reuses their actual short text.  A
            # newly staged title does not replace the text already rendered.
            cover_text = reused_cover_text
    raw_reason_codes = cover_result.get("reason_codes")
    reason_codes = (
        [str(value) for value in raw_reason_codes] if isinstance(raw_reason_codes, list) else []
    )
    artifact_hashes = {str(k): str(v) for k, v in dict(record.get("artifact_hashes") or {}).items()}
    for key in ("cover_sha256", "ai_background_sha256", "cover_reference_sha256"):
        artifact_hashes.pop(key, None)
    subtitle_path_value = str(record.get("subtitle_path") or "").strip()
    subtitle_path = Path(subtitle_path_value) if subtitle_path_value else None
    subtitle_sha256: str | None = None
    if subtitle_path is not None and subtitle_path.is_file() and not subtitle_path.is_symlink():
        subtitle_sha256 = "sha256:" + _sha256(subtitle_path)
        if subtitle_sha256 != final_srt_sha256:
            raise ValueError("CURRENT_EDITORIAL_SUBTITLE_CHANGED_DURING_STAGING")
        artifact_hashes["subtitle_sha256"] = subtitle_sha256
    for key in ("cover_sha256", "ai_background_sha256", "cover_reference_sha256"):
        value = cover_result.get(key)
        if isinstance(value, str) and value:
            artifact_hashes[key] = value
    from src.autoslice.audience_text import assert_audience_surfaces
    assert_audience_surfaces(title=staged_title or '', cover=cover_text or '',
        subtitle=subtitle_path.read_text() if subtitle_path is not None and subtitle_path.is_file() else '')
    publish_draft = {
        "schema_version": "shadow-publish-draft.v1",
        "candidate_id": candidate_id,
        "upload_enabled": False,
        "title": staged_title,
        "title_sha256": _sha256_text(staged_title),
        "subtitle_path": subtitle_path_value or None,
        "subtitle_sha256": subtitle_sha256,
        "title_source": title_source,
        "title_authority_status": title_authority_status,
        "recovery_publication_authority": normalized_recovery_publication_authority,
        "title_authority_error": title_authority_error,
        "title_policy_violations": title_policy_violations,
        "important_content_ips": important_content_ips,
        "editorial_research": normalized_editorial_research,
        "title_generation_attempts": title_generation_attempts,
        "title_story_audit": title_story_audit,
        "entity_projection_audit": entity_projection_audit,
        "cover_entity_projection_audit": cover_entity_projection_audit,
        "source_fact_review": source_fact_review,
        "manual_title_repair_authority_consumption": manual_title_repair_authority_consumption,
        "manual_title_keep_authority_consumption": manual_title_keep_authority_consumption,
        "public_text_surface_authority_consumption": public_text_surface_authority_consumption,
        "video_path": str(media_path),
        "cover_text": cover_text,
        "cover_path": cover_path_value,
        "cover_status": cover_status,
        "cover_generation": cover_generation,
        "reason_codes": reason_codes,
        "artifact_hashes": artifact_hashes,
        **publish_staging_provenance_fields(record),
    }
    publish_draft["current_binding"] = current_editorial_binding(record, publish_draft, candidate_id=candidate_id)
    if cover_path_value and cover_result.get("cover_sha256") not in (None, publish_draft["current_binding"]["cover_sha256"]):
        raise ValueError("CURRENT_EDITORIAL_COVER_DRIFT")
    publish_draft["title_selection"] = {
        "final_title": staged_title,
        "final_title_sha256": _sha256_text(staged_title),
        "final_input_sha256": publish_draft["current_binding"]["input_sha256"],
        "generation_attempt": len(title_generation_attempts) or None,
        "generated_title": title_generation_attempts[-1].get("title") if title_generation_attempts else None,
        "final_resolution": title_authority_status,
        **({"title_cover_skill": final_title_skill} if final_title_skill is not None else {}),
        "source_fact_receipt_sha256": source_fact_review.get("receipt_sha256") if isinstance(source_fact_review, Mapping) else None,
    }
    validate_current_editorial_binding(
        {key: value for key, value in record.items() if key != "upload_tags"},
        {**publish_draft, "current_binding": publish_draft["current_binding"]},
        candidate_id=candidate_id, publish=publish_draft,
    )
    publish_json = json.dumps(publish_draft, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    publish_json_path.write_text(publish_json, encoding="utf-8")
    record["artifact_hashes"] = artifact_hashes
    record["publish_staging"] = {
        "status": "STAGED" if title_authority_error is None else "BLOCKED_TITLE_AUTHORITY",
        "candidate_id": candidate_id,
        "title": staged_title,
        "title_source": title_source,
        "title_authority_status": title_authority_status,
        "recovery_publication_authority": normalized_recovery_publication_authority,
        "title_authority_error": title_authority_error,
        "title_policy_violations": title_policy_violations,
        "important_content_ips": important_content_ips,
        "editorial_research": normalized_editorial_research,
        "title_generation_attempts": title_generation_attempts,
        "title_story_audit": title_story_audit,
        "entity_projection_audit": entity_projection_audit,
        "cover_entity_projection_audit": cover_entity_projection_audit,
        "source_fact_review": source_fact_review,
        "manual_title_repair_authority_consumption": manual_title_repair_authority_consumption,
        "manual_title_keep_authority_consumption": manual_title_keep_authority_consumption,
        "public_text_surface_authority_consumption": public_text_surface_authority_consumption,
        "cover_status": cover_status,
        "cover_path": cover_path_value,
        "cover_text": cover_text,
        "cover_generation": cover_generation,
        "reason_codes": reason_codes,
        "publish_json_path": str(publish_json_path),
        "upload_enabled": False,
        "current_binding": publish_draft["current_binding"],
        "title_selection": publish_draft["title_selection"],
        **publish_staging_provenance_fields(record),
    }
    # Native director dataclasses contain tuples. Match the JSON surface we
    # just persisted, including blocked covers, before strict cross-surface
    # validation; do not weaken that validator's drift comparison.
    record["publish_staging"] = json.loads(json.dumps(record["publish_staging"], ensure_ascii=False, allow_nan=False))
    return record

def _prepare_lidousha_cover_reference(
    materialized_recut: Mapping[str, object],
    *,
    media_path: Path,
    candidate_id: str,
    story_contract: Mapping[str, object] | None,
    cover_refs_dir: Path,
    cover_generation: dict[str, object],
) -> tuple[
    Path | None,
    dict[str, object] | None,
    Mapping[str, object] | None,
    dict[str, object] | None,
]:
    """Select, extract, and verify the source-bound cover reference frame."""

    reference_path = cover_refs_dir / f"{candidate_id}.cover-ref.png"



    cover_ref_override = os.environ.get("AUTOSLICE_COVER_REF_MS", "").strip()
    reference_authority = (
        story_contract.get("cover_reference_authority")
        if isinstance(story_contract, Mapping)
        and isinstance(story_contract.get("cover_reference_authority"), Mapping)
        else None
    )
    authority_ref_ms = (
        int(reference_authority["content_time_ms"]) if reference_authority is not None else None
    )
    if (
        cover_ref_override.isdigit()
        and authority_ref_ms is not None
        and int(cover_ref_override) != authority_ref_ms
    ):
        return (
            None,
            None,
            reference_authority,
            _blocked_ai_cover_result(
                cover_generation,
                ["COVER_REFERENCE_AUTHORITY_CONFLICT"],
                "AUTOSLICE_COVER_REF_MS conflicts with the hash-bound candidate authority",
            ),
        )
    selected_override_ms = (
        authority_ref_ms
        if authority_ref_ms is not None
        else (int(cover_ref_override) if cover_ref_override.isdigit() else None)
    )
    if reference_authority is not None:
        cover_generation["reference_authority"] = dict(reference_authority)
    frame_selection: dict[str, object] | None = None
    if selected_override_ms is None:
        srt_value = materialized_recut.get("subtitle_path")
        # 片头跳过的权威来源=burn 阶段记录的 intro_offset_ms（片头版本轮换、
        # 时长不一，z1-budui=5749ms 曾越过固定 skip 造成过渡假峰）；record 缺失
        # 时由选帧器内置的切点探测兜底。
        intro_offset = (
            (materialized_recut.get("burned_preview") or {}).get("branding_intro") or {}
        ).get("intro_offset_ms")
        skip_head_ms = DEFAULT_SKIP_HEAD_MS
        if isinstance(intro_offset, (int, float)) and intro_offset > 0:
            skip_head_ms = max(skip_head_ms, int(intro_offset) + 1_500)
        try:
            frame_selection = select_expressive_cover_frame(
                media_path,
                workdir=cover_refs_dir,
                skip_head_ms=skip_head_ms,
                srt_path=(
                    Path(srt_value)
                    if isinstance(srt_value, str) and srt_value and Path(srt_value).is_file()
                    else None
                ),
            )
            cover_generation["reference_selection"] = frame_selection
        except Exception as exc:
            cover_generation["reference_selection"] = {
                "status": "FALLBACK_THUMBNAIL",
                "detail": f"{type(exc).__name__}: {exc}",
            }
    else:
        frame_selection = {
            "schema": "cover-frame-selection.v1",
            "status": (
                "HASH_BOUND_AUTHORITY_OVERRIDE"
                if reference_authority is not None
                else "OPERATOR_OVERRIDE"
            ),
            "best_ms": selected_override_ms,
            "candidates": [{"ms": selected_override_ms, "score": None}],
            "subject_confident": False,
            "authority_override": (
                dict(reference_authority) if reference_authority is not None else None
            ),
        }
        cover_generation["reference_selection"] = frame_selection
    ref_command = _cover_reference_command(
        media_path=media_path,
        reference_path=reference_path,
        override_ms=selected_override_ms,
        selected_ms=(int(frame_selection["best_ms"]) if frame_selection is not None else None),
    )
    completed = subprocess.run(ref_command, check=False, capture_output=True, text=True)
    if completed.returncode != 0 or not reference_path.is_file():
        cover_generation["reference_command"] = ref_command
        return (
            None,
            frame_selection,
            reference_authority,
            _blocked_ai_cover_result(
                cover_generation,
                ["CPA_AI_COVER_REQUIRED", "COVER_REFERENCE_EXTRACTION_FAILED"],
                completed.stderr[-500:] or "reference frame extraction failed",
            ),
        )
    # 竖屏判据量的是**已抽出的参考帧本身**，不是选帧回执：override / hash-bound
    # 重放手工拼的 frame_selection 里没有几何，而重放正是竖版事故的现场。
    cover_generation["reference_frame_size"] = read_source_frame_size(reference_path)
    if reference_authority is not None:
        actual_reference_sha256 = "sha256:" + _sha256(reference_path)
        expected_reference_sha256 = str(reference_authority["reference_png_sha256"])
        if str(reference_authority["source_sha256"]) not in (
            story_contract.get("source_media_sha256s") or []
        ):
            return (
                None,
                frame_selection,
                reference_authority,
                _blocked_ai_cover_result(
                    cover_generation,
                    ["COVER_REFERENCE_SOURCE_BINDING_MISSING"],
                    "the producer-verified source bytes do not match the cover authority",
                ),
            )
        if actual_reference_sha256 != expected_reference_sha256:
            return (
                None,
                frame_selection,
                reference_authority,
                _blocked_ai_cover_result(
                    cover_generation,
                    ["COVER_REFERENCE_AUTHORITY_HASH_MISMATCH"],
                    (f"expected {expected_reference_sha256}, got {actual_reference_sha256}"),
                ),
            )
    return reference_path, frame_selection, reference_authority, None


def _stage_cpa_redraw_cover(
    *,
    candidate_id: str,
    title: str,
    cover_text: str,
    story_contract: Mapping[str, object] | None,
    cover_refs_dir: Path,
    ai_dir: Path,
    covers_dir: Path,
    evidence_dir: Path,
    reference_path: Path,
    emote_library: EmoteLibrary,
    art_direction: LidoushaCoverArtDirection,
    cover_generation: dict[str, object],
    image_edit: Callable[..., dict[str, object]],
    final_participant_verifier: (Callable[..., Mapping[str, object]] | None),
    final_host_identity_verifier: (Callable[..., Mapping[str, object]] | None),
    base_url: str,
    api_key: str,
    full_text_cover_contract: Mapping[str, object] | None = None,
    demotion_detail: str | None = None,
) -> dict[str, object]:
    """Resolve optional emote direction and materialize the CPA redraw lane.

    ``demotion_detail`` 非空表示本次重绘是截图路线物化失败后的**显式降级**执行
    （非静默改道）：成功记录写 READY_DEGRADED 并携带失败细节，验证器要求
    screenshot_direct 的 BLOCKED 回执同时在场才放行。
    """








    emote_entry = None
    if art_direction.emote_id:
        entry = emote_library.get(art_direction.emote_id)
        resolved, resolve_detail = (
            resolve_emote_reference(
                entry,
                repo_root=ROOT,
                runtime_roots=emote_library.runtime_roots,
            )
            if entry is not None
            else (None, "EMOTE_ID_UNKNOWN")
        )
        emote_evidence: dict[str, object] = {
            "id": art_direction.emote_id,
            "label": entry.label if entry is not None else None,
            "mode": art_direction.emote_mode,
            "reason": art_direction.emote_reason,
        }
        if resolved is None:
            emote_evidence.update({"status": "BLOCKED_REFERENCE", "detail": resolve_detail})
            cover_generation["emote"] = emote_evidence
            cover_generation["art_direction"] = asdict(art_direction)
            record_cover_route_execution(
                cover_generation,
                actual_treatment=None,
                execution_status="BLOCKED",
                image_generation_attempted=False,
                image_generation_used=False,
                detail=str(resolve_detail),
            )
            return _blocked_ai_cover_result(
                cover_generation,
                ["EMOTE_REFERENCE_REQUIRED", str(resolve_detail)],
                str(resolve_detail),
            )
        else:
            try:
                if art_direction.emote_mode == "companion":
                    reference_path = compose_companion_reference(
                        reference_path,
                        resolved,
                        cover_refs_dir / f"{candidate_id}.cover-ref.with-emote.png",
                    )
                else:
                    reference_path = resolved
                emote_entry = entry
                emote_evidence.update(
                    {
                        "status": "EMOTE_REFERENCE_READY",
                        "hd_file": str(resolved),
                        "hd_sha256": "sha256:" + entry.hd_sha256,
                    }
                )
            except Exception as exc:
                detail = f"EMOTE_COMPANION_COMPOSE_FAILED: {type(exc).__name__}: {exc}"
                emote_evidence.update(
                    {
                        "status": "BLOCKED_REFERENCE",
                        "detail": detail,
                    }
                )
                cover_generation["emote"] = emote_evidence
                cover_generation["art_direction"] = asdict(art_direction)
                record_cover_route_execution(
                    cover_generation,
                    actual_treatment=None,
                    execution_status="BLOCKED",
                    image_generation_attempted=False,
                    image_generation_used=False,
                    detail=detail,
                )
                return _blocked_ai_cover_result(
                    cover_generation,
                    [
                        "EMOTE_REFERENCE_REQUIRED",
                        "EMOTE_COMPANION_COMPOSE_FAILED",
                    ],
                    detail,
                )
        cover_generation["emote"] = emote_evidence
    route = cover_generation.get("route_decision")
    redraw_scene_kind = (
        str(route.get("scene_kind") or "talk")
        if isinstance(route, Mapping)
        else "talk"
    )
    redraw_relationship_visual_required = (
        isinstance(route, Mapping)
        and route.get("relationship_visual_required") is True
    )
    style_binding = (
        ordinary_talk_redraw_style_binding(
            scene_kind=redraw_scene_kind,
            relationship_visual_required=redraw_relationship_visual_required,
            is_song=art_direction.is_song,
            hook_color=art_direction.hook_color,
        )
        if art_direction.emote_mode != "replace"
        else None
    )
    if (
        isinstance(style_binding, Mapping)
        and style_binding.get("status") == "READY"
        and isinstance(style_binding.get("text_palette"), Mapping)
    ):
        palette = style_binding["text_palette"]
        primary = palette.get("primary")
        secondary = palette.get("secondary")
        if (
            isinstance(primary, str)
            and isinstance(secondary, str)
            and secondary != primary
            and palette == ordinary_talk_redraw_text_palette(hook_color=primary)
        ):
            art_direction = dataclasses_replace(
                art_direction,
                hook_color=primary,
                secondary_fill=secondary,
            )
    # Record the final direction after the style palette is applied so the
    # renderer and the generation receipt share one deterministic recipe.
    cover_generation["art_direction"] = asdict(art_direction)
    if style_binding is not None:
        # Keep the exact profile bytes that the image prompt consumed.  A
        # registered but unreadable asset remains an explicit MISSING binding
        # so joint QC cannot silently downgrade it to an optional preference.
        cover_generation["art_direction_style"] = dict(style_binding)
        if style_binding.get("status") != "READY":
            return _blocked_ai_cover_result(
                cover_generation, ["COVER_STYLE_REFERENCE_UNAVAILABLE"],
                str(style_binding.get("reason_code") or "registered style asset is unavailable"),
            )

    ai_background_path = ai_dir / f"{candidate_id}.ai-bg.cpa-image-edit.png"
    request_path = evidence_dir / f"{candidate_id}.cover-cpa-request.redacted.json"
    response_path = evidence_dir / f"{candidate_id}.cover-cpa-response.redacted.json"
    cover_generation.update(
        {
            "method": "images.edit",
        }
    )
    generation_prompt = _cover_prompt(
        title=title,
        cover_text=cover_text,
        art_direction=art_direction,
        emote=emote_entry,
        scene_kind=redraw_scene_kind,
        relationship_visual_required=redraw_relationship_visual_required,
        style_binding=style_binding,
    ) + cover_relation_prompt(story_contract, scene_kind=redraw_scene_kind)
    generation_reference_path = reference_path
    if (
        not art_direction.emote_id
        and story_contract_requires_host_only_final(
            story_contract, scene_kind=redraw_scene_kind
        )
    ):
        composition = cover_generation.get("source_composition_verification")
        receipt = cover_generation.get("source_composition_receipt")
        try:
            if not isinstance(composition, Mapping) or not isinstance(receipt, Mapping):
                raise ValueError("SOURCE_COMPOSITION_REFERENCE_RECEIPT_MISSING")
            receipt_path = Path(str(receipt.get("path") or ""))
            if "sha256:" + _sha256(receipt_path) != receipt.get("sha256"):
                raise ValueError("SOURCE_COMPOSITION_REFERENCE_RECEIPT_DRIFT")
            if json.loads(receipt_path.read_text(encoding="utf-8")) != dict(composition):
                raise ValueError("SOURCE_COMPOSITION_REFERENCE_VERDICT_DRIFT")
            selection = cover_generation.get("reference_selection")
            if (
                not isinstance(selection, Mapping)
                or isinstance(selection.get("best_ms"), bool)
                or not isinstance(selection.get("best_ms"), int)
                or selection["best_ms"] < 0
            ):
                raise ValueError("SOURCE_COMPOSITION_REFERENCE_TIME_MISSING")
            generation_reference_path = (
                cover_refs_dir / f"{candidate_id}.cover-ref.host-identity.png"
            )
            transfer = extract_authority_source_crop(
                reference_path=reference_path,
                output_path=generation_reference_path,
                frame_ms=selection["best_ms"],
                verification=composition,
                verification_receipt_path=receipt_path,
                verification_receipt_sha256=str(receipt["sha256"]),
                identity_reference_only=True,
            )
            transfer_path = evidence_dir / f"{candidate_id}.redraw-identity-reference.json"
            _write_json_file(transfer_path, transfer)
            cover_generation["redraw_identity_reference"] = {
                **transfer,
                "output_path": str(generation_reference_path),
                "receipt_path": str(transfer_path),
                "receipt_sha256": "sha256:" + _sha256(transfer_path),
            }
            generation_prompt += (
                " The supplied image is the source-witnessed host region. "
                "Ignore any clipped background or other-character remnants at its edges. "
                "Preserve all visible outfit layers, collar, bow and head accessories "
                "from these pixels; do not invent hidden clothing."
            )
        except (OSError, ValueError, TypeError, KeyError) as exc:
            record_cover_route_execution(
                cover_generation, actual_treatment=None, execution_status="BLOCKED",
                image_generation_attempted=False, image_generation_used=False,
                detail=f"{type(exc).__name__}: {exc}",
            )
            return _blocked_ai_cover_result(
                cover_generation,
                ["CPA_REDRAW_IDENTITY_REFERENCE_INVALID"],
                f"{type(exc).__name__}: {exc}",
            )
    record_cover_route_execution(
        cover_generation,
        actual_treatment=None,
        execution_status="IMAGE_GENERATION_IN_PROGRESS",
        image_generation_attempted=True,
        image_generation_used=False,
    )
    try:
        style_reference = (
            style_binding.get("reference_image")
            if isinstance(style_binding, Mapping) else None
        )
        style_kwargs = (
            {"style_reference_paths": (Path(str(style_reference["path"])),)}
            if isinstance(style_reference, Mapping) else {}
        )
        cpa_result = image_edit(
            base_url=base_url,
            api_key=api_key,
            reference_path=generation_reference_path,
            output_path=ai_background_path,
            prompt=generation_prompt,
            request_path=request_path,
            response_path=response_path,
            **style_kwargs,
        )
        if style_kwargs:
            cover_generation["style_reference_images"] = cpa_result.get("style_reference_images", [])
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
        record_cover_route_execution(
            cover_generation,
            actual_treatment=None,
            execution_status="BLOCKED",
            image_generation_attempted=True,
            image_generation_used=False,
            detail=detail,
        )
        return _blocked_ai_cover_result(
            cover_generation,
            ["CPA_AI_COVER_REQUIRED", "CPA_IMAGE_EDIT_EXCEPTION"],
            detail,
        )
    cover_generation.update(
        {
            "reference_image": str(generation_reference_path),
            "reference_sha256": "sha256:" + _sha256(generation_reference_path),
            "request_path": str(request_path),
            "response_path": str(response_path),
            "attempted_models": list(cpa_result.get("attempted_models") or []),
            "model_fallback_used": bool(cpa_result.get("model_fallback_used")),
        }
    )
    if cpa_result.get("selected_model"):
        cover_generation["model"] = str(cpa_result["selected_model"])
    if cpa_result.get("status") != "AI_BACKGROUND_READY" or not ai_background_path.is_file():
        cover_generation["cpa_status"] = cpa_result.get("status")
        detail = str(cpa_result.get("detail") or "CPA image edit did not return an image")
        record_cover_route_execution(
            cover_generation,
            actual_treatment=None,
            execution_status="BLOCKED",
            image_generation_attempted=True,
            image_generation_used=False,
            detail=detail,
        )
        return _blocked_ai_cover_result(
            cover_generation,
            [
                "CPA_AI_COVER_REQUIRED",
                str(cpa_result.get("reason_code") or "CPA_IMAGE_EDIT_FAILED"),
            ],
            detail,
        )

    final_cover_path = covers_dir / f"{candidate_id}.ai-title.cover.png"
    # talk 的 mode=full 自动回退废除后，hash 绑定的 full-text contract
    # 是整句上封面的**唯一**合法通道；不把它穿透给 renderer 就等于把这条通道静默杀死
    # ——刚消灭「静默回退」，不能换来一个「静默不可能」。
    overlay = _overlay_cover_title(
        ai_background_path,
        final_cover_path,
        cover_text=cover_text,
        art_direction=art_direction,
        full_text_cover_contract=full_text_cover_contract,
    )
    cover_generation.update(
        {
            "method": "images.edit",
            "cover_origin": "AI_REDRAW",
            "image_generation_used": True,
            "ai_background": str(ai_background_path),
            "ai_background_sha256": "sha256:" + _sha256(ai_background_path),
            "final_cover": str(final_cover_path),
            "final_cover_sha256": "sha256:" + _sha256(final_cover_path),
            **overlay,
        }
    )
    route = cover_generation.get("route_decision")
    if isinstance(route, Mapping) and route.get("host_identity_required") is True:
        assert final_host_identity_verifier is not None

        def verify_current_final() -> None:
            try:
                witness = run_final_host_identity_witness(
                    final_host_identity_verifier,
                    final_cover_path=Path(str(cover_generation["final_cover"])),
                    final_cover_sha256=cover_generation["final_cover_sha256"],
                    reference_path=reference_path,
                    base_url=base_url,
                    api_key=api_key, cover_generation=cover_generation,
                )
                cover_generation["final_host_identity_verification"] = dict(witness)
                if 'title_occlusion_reflow' in cover_generation:
                    cover_generation['title_occlusion_reflow']['status'] = (
                        'PASS' if validate_final_host_identity_verification(cover_generation) else 'FAILED_CURRENT_IDENTITY')
            except Exception as exc:
                cover_generation["final_host_identity_verification"] = {
                    "status": "FAIL",
                    "reason_code": "HOST_IDENTITY_VERIFIER_EXCEPTION",
                    "detail": f"{type(exc).__name__}: {exc}",
                }

        verify_current_final()
        from .cover_title_reflow_binding import reflow_title_occlusion
        title_only = reflow_title_occlusion(cover_generation,
            output_path=covers_dir / f"{candidate_id}.ai-title.reflow.cover.png",
            full_text_cover_contract=full_text_cover_contract)
        if title_only and cover_generation['title_occlusion_reflow']['status'] != 'BLOCKED':
            verify_current_final()
        if not validate_final_host_identity_verification(
            cover_generation
        ) and not final_host_identity_witness_unavailable(cover_generation) and not title_only:
            first_verification = cover_generation.get("final_host_identity_verification")
            cover_generation["rejected_final_host_identity_verification"] = dict(
                first_verification if isinstance(first_verification, Mapping) else {}
            )
            retry_background = ai_dir / f"{candidate_id}.ai-bg.host-identity-retry.png"
            retry_final = covers_dir / f"{candidate_id}.ai-title.host-identity-retry.cover.png"
            retry_request = (
                evidence_dir / f"{candidate_id}.cover-cpa-host-identity-retry.request.redacted.json"
            )
            retry_response = (
                evidence_dir
                / f"{candidate_id}.cover-cpa-host-identity-retry.response.redacted.json"
            )
            retry_result: Mapping[str, object]
            try:
                retry_result = image_edit(
                    base_url=base_url,
                    api_key=api_key,
                    reference_path=generation_reference_path,
                    output_path=retry_background,
                    prompt=(
                        generation_prompt
                        + " FINAL-PIXEL QUALITY RETRY: the previous output failed "
                        "the independent identity or subject-prominence check. "
                        "Re-read visible source nameplates. The protagonist must "
                        f"be the person labelled {CHANNEL_PROFILE.display_name}; do not hybridize her with "
                        "any other participant, even if panda ears are added. "
                        f"Make {CHANNEL_PROFILE.prompt_name} a LARGE, clear, immediately dominant "
                        "head-and-shoulders subject who visibly carries the story "
                        "reaction. Never place her as a small lower-corner figure. "
                        "Remove vast dead space, meaningless solid-color/red bars, "
                        "decorative clutter, or unrelated elements; keep only the "
                        "intentional title zone and story-supporting visuals."
                    ),
                    request_path=retry_request,
                    response_path=retry_response,
                    **style_kwargs,
                )
            except Exception as exc:
                retry_result = {
                    "status": "FAILED",
                    "reason_code": "HOST_IDENTITY_RETRY_EXCEPTION",
                    "detail": f"{type(exc).__name__}: {exc}",
                }
            retry_ready = bool(
                retry_result.get("status") == "AI_BACKGROUND_READY" and retry_background.is_file()
            )
            cover_generation["host_identity_retry"] = {
                "schema_version": "lidousha-cover-host-identity-retry.v1",
                "attempted": True,
                "request_path": str(retry_request),
                "response_path": str(retry_response),
                "result_status": retry_result.get("status"),
                "reason_code": retry_result.get("reason_code"),
                "previous_request_path": str(request_path),
                "previous_response_path": str(response_path),
                "status": "GENERATED_PENDING_VERIFICATION" if retry_ready else "FAILED",
            }
            if retry_ready:
                # The retry owns the final pixels.  Its consumed references,
                # rather than the rejected first attempt's, must reach joint QC.
                if style_kwargs:
                    cover_generation["style_reference_images"] = retry_result.get(
                        "style_reference_images", []
                    )
                    cover_generation["host_identity_retry"]["style_reference_images"] = (
                        cover_generation["style_reference_images"]
                    )
                retry_overlay = _overlay_cover_title(
                    retry_background,
                    retry_final,
                    cover_text=cover_text,
                    art_direction=art_direction,
                    full_text_cover_contract=full_text_cover_contract,
                )
                cover_generation.update(
                    {
                        "ai_background": str(retry_background),
                        "ai_background_sha256": "sha256:" + _sha256(retry_background),
                        "final_cover": str(retry_final),
                        "final_cover_sha256": "sha256:" + _sha256(retry_final),
                        "identity_retry_request_path": str(retry_request),
                        "identity_retry_response_path": str(retry_response),
                        "request_path": str(retry_request),
                        "response_path": str(retry_response),
                        "attempted_models": list(retry_result.get("attempted_models") or []),
                        "model_fallback_used": bool(retry_result.get("model_fallback_used")),
                        **retry_overlay,
                    }
                )
                if retry_result.get("selected_model"):
                    cover_generation["model"] = str(retry_result["selected_model"])
                verify_current_final()
                retry_title_only = reflow_title_occlusion(cover_generation,
                    output_path=covers_dir / f"{candidate_id}.ai-title.host-identity-retry.reflow.cover.png",
                    full_text_cover_contract=full_text_cover_contract)
                if retry_title_only and cover_generation['title_occlusion_reflow']['status'] != 'BLOCKED':
                    verify_current_final()
                cover_generation["host_identity_retry"]["status"] = (
                    "PASS"
                    if validate_final_host_identity_verification(cover_generation)
                    else "FAILED_FINAL_IDENTITY"
                )
                final_cover_path = retry_final
                ai_background_path = retry_background
        final_cover_path = Path(str(cover_generation['final_cover']))
        if not validate_final_host_identity_verification(cover_generation):
            verification = cover_generation.get("final_host_identity_verification")
            detail = "AI cover final pixels do not have a PASS host identity verdict: " + str(
                (verification if isinstance(verification, Mapping) else {}).get("reason_code")
                or "VERIFICATION_MISSING"
            )
            record_cover_route_execution(
                cover_generation,
                actual_treatment=None,
                execution_status="BLOCKED",
                image_generation_attempted=True,
                image_generation_used=True,
                detail=detail,
            )
            return _blocked_ai_cover_result(
                cover_generation,
                ["COVER_FINAL_HOST_IDENTITY_UNVERIFIED"],
                detail,
            )
    relationship_visual_required = relationship_visual_safety_required(
        story_contract,
        route_decision=route,
    )
    final_participant_verification = None
    if relationship_visual_required:
        assert final_participant_verifier is not None
        try:
            final_participant_verification = dict(
                final_participant_verifier(
                    final_cover_path=final_cover_path,
                    final_cover_sha256=cover_generation["final_cover_sha256"],
                    required_participant_ids=list(route.get("required_participant_ids") or []),
                    source_reference_authority=(
                        story_contract.get("cover_reference_authority")
                        if isinstance(story_contract, Mapping)
                        else None
                    ),
                )
            )
        except Exception as exc:
            detail = f"final participant verification failed: {type(exc).__name__}: {exc}"
            record_cover_route_execution(
                cover_generation,
                actual_treatment=None,
                execution_status="BLOCKED",
                image_generation_attempted=True,
                image_generation_used=True,
                detail=detail,
            )
            return _blocked_ai_cover_result(
                cover_generation,
                ["RELATION_COVER_FINAL_PARTICIPANT_VERIFICATION_FAILED"],
                detail,
            )
        cover_generation["final_participant_verification"] = final_participant_verification
        if not validate_final_participant_verification(cover_generation):
            detail = (
                "AI redraw lacks a PASS verdict bound to the final cover hash "
                "that verifies every required participant"
            )
            record_cover_route_execution(
                cover_generation,
                actual_treatment=None,
                execution_status="BLOCKED",
                image_generation_attempted=True,
                image_generation_used=True,
                detail=detail,
            )
            return _blocked_ai_cover_result(
                cover_generation,
                ["RELATION_COVER_FINAL_PARTICIPANTS_UNVERIFIED"],
                detail,
            )
    record_cover_route_execution(
        cover_generation,
        actual_treatment="cpa_redraw",
        execution_status=("READY_DEGRADED" if demotion_detail else "READY"),
        image_generation_attempted=True,
        image_generation_used=True,
        detail=demotion_detail,
        final_participant_verification=final_participant_verification,
    )
    return {
        "status": "AI_COVER_READY",
        "reason_codes": [],
        "cover_path": str(final_cover_path),
        "cover_generation": cover_generation,
        "cover_sha256": "sha256:" + _sha256(final_cover_path),
        "ai_background_sha256": "sha256:" + _sha256(ai_background_path),
        "cover_reference_sha256": "sha256:" + _sha256(reference_path),
    }

def _build_lidousha_cover_route(
    *,
    cover_generation: dict[str, object],
    story_contract: object,
    title: str,
    cover_text: str,
    cover_mode: str,
    art_direction: LidoushaCoverArtDirection,
    punch_allowed: bool,
    frame_selection: Mapping[str, object] | None,
    reference_authority: Mapping[str, object] | None,
    source_composition_verification: Mapping[str, object] | None = None,
    full_text_cover_contract: Mapping[str, object] | None = None,
    enforce_final_host_identity: bool = False,
) -> tuple[str, dict[str, object]]:
    """Build the semantic-first route record before any cover materialization."""

    source_frame_size = cover_generation.get("reference_frame_size")
    required_participant_ids = story_participant_ids(story_contract)
    visible_participant_ids = source_visible_participant_ids(reference_authority)
    relationship_source_verified = bool(
        required_participant_ids and set(required_participant_ids) == set(visible_participant_ids)
    )
    verified_stream_frame = is_hash_bound_reference_authority(reference_authority)
    punch_review = art_direction.cover_punch_semantic_review
    punch_semantic_status = str(
        punch_review.get("status") if isinstance(punch_review, Mapping) else ""
    ).strip()
    thumbnail_text_requires_punch = punch_allowed and cover_text_requires_punch_for_thumbnail(
        cover_text
    )
    treatment, treatment_reason = _decide_cover_treatment(
        cover_mode=cover_mode,
        is_song=art_direction.is_song,
        punch_allowed=punch_allowed,
        frame_selection=frame_selection,
        verified_stream_frame=verified_stream_frame,
        relationship_visual_required=(relationship_visual_safety_required(story_contract)),
        relationship_source_verified=relationship_source_verified,
        thumbnail_text_requires_punch=thumbnail_text_requires_punch,
        punch_semantic_status=punch_semantic_status,
        source_composition_verification=source_composition_verification,
        source_frame_size=source_frame_size,
    )
    cover_generation["cover_treatment"] = {
        "treatment": treatment,
        "reason": treatment_reason,
    }
    candidates = frame_selection.get("candidates") if isinstance(frame_selection, Mapping) else None
    first_candidate = (
        candidates[0]
        if isinstance(candidates, list) and candidates and isinstance(candidates[0], Mapping)
        else {}
    )
    route = build_cover_route_decision(
        selected_treatment=treatment,
        selected_rationale=treatment_reason,
        story_contract=story_contract,
        reference_authority=reference_authority,
        title=title,
        cover_text=cover_text,
        decision_inputs={
            "cover_mode": cover_mode,
            "is_song": art_direction.is_song,
            "cover_punch_allowed": punch_allowed,
            "full_text_cover_contract": (full_text_cover_contract is not None),
            "frame_score": first_candidate.get("score"),
            "frame_emotion": first_candidate.get("emotion"),
            "subject_confident": (
                frame_selection.get("subject_confident")
                if isinstance(frame_selection, Mapping)
                else None
            ),
            "motion_dispersion_frac": (
                frame_selection.get("motion_dispersion_frac")
                if isinstance(frame_selection, Mapping)
                else None
            ),
            "verified_stream_frame": verified_stream_frame,
            **vertical_source_decision_inputs(source_frame_size, source_composition_verification),
            "thumbnail_text_requires_punch": thumbnail_text_requires_punch,
            "punch_semantic_status": punch_semantic_status,
            "reference_authority_id": (
                reference_authority.get("candidate_id") if reference_authority is not None else None
            ),
            # Final pixels own the public cover. A direct screenshot with Li
            # Dousha only as a tiny corner avatar is not a valid host cover.
            "host_identity_required": bool(enforce_final_host_identity),
            "source_composition_status": (
                source_composition_verification.get("status")
                if isinstance(source_composition_verification, Mapping)
                else "NOT_REQUIRED"
            ),
            "source_composition_schema_version": (
                source_composition_verification.get("schema_version")
                if isinstance(source_composition_verification, Mapping)
                else None
            ),
            "source_composition_witness_sha256": (
                source_composition_verification.get("witness_receipt_sha256")
                if isinstance(source_composition_verification, Mapping)
                else None
            ),
            "source_composition_redraw_recommended": (
                source_composition_recommends_redraw(source_composition_verification)
                if isinstance(source_composition_verification, Mapping)
                else None
            ),
        },
        # C9：拒绝理由必须带这一帧的真实判据（见证原文+分数+场景），不是模板。
        source_composition_verification=source_composition_verification,
    )
    cover_generation["route_decision"] = route
    record_cover_route_execution(
        cover_generation,
        actual_treatment=None,
        execution_status="PENDING",
        image_generation_attempted=False,
        image_generation_used=False,
    )
    return treatment, route


def _review_retained_failed_cover(*, artifact_root: Path, candidate_id: str,
                                 expected: Mapping, attempt: Path,
                                 prepare_qc_only: bool = False) -> dict:
    """Re-review one pinned rejected HOST_ONLY redraw, with no image call."""
    from .normal_cover_generation_checkpoint import retained_failed_generation, file_identity
    failure = os.environ.get('AUTOSLICE_REVIEW_COVER_FAILURE', '')
    digest = os.environ.get('AUTOSLICE_REVIEW_COVER_FAILURE_SHA256', '')
    if not failure or not digest:
        raise ValueError('COVER_REVIEW_ORIGIN_BINDING_INCOMPLETE')
    result = retained_failed_generation(path=Path(failure), expected_sha256=digest,
        expected=expected, artifact_root=artifact_root, candidate_id=candidate_id)
    generation = result['cover_generation']
    route = generation.get('route_decision') or {}
    story = generation.get('story_contract')
    if (not story_contract_requires_host_only_final(story)
            or route.get('host_identity_required') is not True
            or relationship_visual_safety_required(story, route_decision=route)):
        raise ValueError('COVER_REVIEW_SCOPE_UNSUPPORTED')
    old = generation.get('final_host_identity_verification') or {}
    reference = Path(str(old.get('reference_path') or ''))
    reference_identity = file_identity(reference)
    if reference_identity.get('sha256') != old.get('reference_sha256'):
        raise ValueError('COVER_REVIEW_REFERENCE_DRIFT')
    from .cover_title_reflow_binding import reflow_title_occlusion
    reflowed = reflow_title_occlusion(generation, output_path=attempt / 'retained-title-reflow.cover.png',
        full_text_cover_contract=expected.get('options', {}).get('full_text_cover_contract'))
    if reflowed and generation['title_occlusion_reflow']['status'] == 'BLOCKED':
        return _blocked_ai_cover_result(generation, ['COVER_FINAL_HOST_IDENTITY_UNVERIFIED'],
                                        'Retained background is valid; deterministic title cannot fit a measured clear area')
    base_url, api_key = _resolve_cover_cpa_credentials()
    current = verify_final_host_identity(
        final_cover_path=Path(generation['final_cover']),
        final_cover_sha256=generation['final_cover_sha256'], reference_path=reference,
        base_url=base_url, api_key=api_key, host_only_required=True,
        comparison_output_path=attempt / 'retained-source-final-comparison.png',
        **({'prepare_only': True} if prepare_qc_only else {}),
    )
    generation['previous_final_host_identity_verification'] = old
    generation['final_host_identity_verification'] = current
    generation['retained_pixel_review'] = {'new_image_calls': 0,
        'scope': 'CURRENT_HOST_ONLY_REFLOWED_FINAL_PIXELS' if reflowed else 'CURRENT_HOST_ONLY_FINAL_PIXELS',
        'original_failure_unchanged': True}
    if reflowed:
        generation['title_occlusion_reflow']['status'] = (
            'PENDING_CURRENT_IDENTITY' if current.get('status') == 'PENDING' else
            'PASS' if validate_final_host_identity_verification(generation) else 'FAILED_CURRENT_IDENTITY')
    if not validate_final_host_identity_verification(generation):
        return _blocked_ai_cover_result(generation, ['COVER_FINAL_HOST_IDENTITY_UNVERIFIED'],
                                        'Retained pixels did not pass the current independent identity review')
    generation['status'] = 'READY'
    generation['detail'] = 'Retained generated pixels passed current identity review; no new image generation'
    record_cover_route_execution(generation, actual_treatment='cpa_redraw', execution_status='READY',
        image_generation_attempted=True, image_generation_used=True)
    ready = {**result, 'status': 'AI_COVER_READY', 'reason_codes': [],
        'cover_path': generation['final_cover'], 'cover_sha256': generation['final_cover_sha256'],
        'ai_background_sha256': generation['ai_background_sha256'],
        'cover_reference_sha256': reference_identity['sha256']}
    return _enforce_final_talk_cover_thumbnail_gate(ready)


def _reuse_retained_successful_cover(*, artifact_root: Path, candidate_id: str, expected: Mapping) -> dict:
    """Consume a completed native pixel set through the current QC predicates."""
    from .normal_cover_generation_checkpoint import retained_generation
    result = retained_generation(path=Path(os.environ['AUTOSLICE_REUSE_COVER_RESULT']),
        expected_sha256=os.environ['AUTOSLICE_REUSE_COVER_RESULT_SHA256'], expected=expected,
        artifact_root=artifact_root, candidate_id=candidate_id, successful=True)
    generation = result['cover_generation']
    route = generation.get('route_decision') or {}
    if (route.get('host_identity_required') is True and not validate_final_host_identity_verification(generation)):
        raise ValueError('COVER_REUSE_CURRENT_IDENTITY_INVALID')
    if (relationship_visual_safety_required(generation.get('story_contract'), route_decision=route)
            and not validate_final_participant_verification(generation)):
        raise ValueError('COVER_REUSE_CURRENT_PARTICIPANTS_INVALID')
    return _enforce_final_talk_cover_thumbnail_gate(result)


def _stage_ai_cover(materialized_recut, *, media_path, candidate_id, title, cover_text, **kwargs):
    """Persist a complete generation before any downstream publish/package save."""
    from .normal_cover_generation_checkpoint import generation_identity, successful_generation

    story = materialized_recut.get("story_contract")
    inputs = editorial_input_binding(
        materialized_recut, candidate_id=candidate_id, title=title,
        selection_hook=str(story.get("selection_hook") or "") if isinstance(story, Mapping) else "",
        editorial_research=materialized_recut.get("editorial_research", story.get("editorial_research") if isinstance(story, Mapping) else None),
    )
    if bool(os.environ.get('AUTOSLICE_REVIEW_COVER_FAILURE')) != bool(os.environ.get('AUTOSLICE_REVIEW_COVER_FAILURE_SHA256')):
        return _blocked_ai_cover_result({'input_binding': inputs}, ['COVER_REVIEW_ORIGIN_BINDING_INCOMPLETE'],
                                        'Retained cover review requires the original path and SHA together')
    if (bool(os.environ.get('AUTOSLICE_REUSE_COVER_RESULT')) != bool(os.environ.get('AUTOSLICE_REUSE_COVER_RESULT_SHA256'))
            or (os.environ.get('AUTOSLICE_REUSE_COVER_RESULT') and os.environ.get('AUTOSLICE_REVIEW_COVER_FAILURE'))):
        return _blocked_ai_cover_result({'input_binding': inputs}, ['COVER_REUSE_ORIGIN_BINDING_INVALID'],
                                        'Choose exactly one complete retained-cover origin binding')
    options = {
        "run_ffmpeg": kwargs.get("run_ffmpeg"),
        "art_direction_llm_call": None, "image_edit": _cover_call_cpa_image_edit,
        "final_participant_verifier": None, "final_host_identity_verifier": None,
        "source_composition_verifier": None, "enforce_final_host_identity": False,
        "punch_allowed": False, "full_text_cover_contract": None,
        "diversity_slot": None, "fixed_options": FixedCoverStageOptions(),
        **kwargs,
    }
    artifact_root = kwargs.get("private_artifact_root") or materialized_artifact_root(materialized_recut, media_path)

    def identity():
        base_url, _ = _resolve_cover_cpa_credentials()
        provider = {
            "image_models": list(_cpa_image_model_candidates()),
            "image_endpoint_sha256": _sha256_text(base_url),
            "environment": {name: os.environ.get(name) for name in (
                "AUTOSLICE_COVER_REF_MS", "AUTOSLICE_FONTS_DIR", "LIDOUSHA_FONTS_DIR",
                "LIDOUSHA_COVER_FONT_BINDING", "LIDOUSHA_COVER_FONT", "LIDOUSHA_COVER_FONT_SHA256",
                "AUTOSLICE_EMOTE_DIR", "LIDOUSHA_SESSION_GAME_CONTEXT",
                "LIDOUSHA_DISABLE_SESSION_GAME_CONTEXT", "CPA_CHAT_MODEL", "CPA_CHAT_MODELS",
                "CPA_REASONING_EFFORT", "AUTOSLICE_FINAL_REVIEW_RUNTIME_ROOT",
                "AUTOSLICE_FINAL_REVIEW_SSH_HOST", "AUTOSLICE_BASE",
                "AGY_VISION_MODEL", "AGY_GEMINI_VISION_MODEL", "AGY_VISION_PRINT_TIMEOUT",
                "AGY_BIN", "AUTOSLICE_AGY_BIN", "AGY_REMOTE_BIN",
            )},
        }
        current_options = {**options, "resolved_cover_mode": resolved_cover_mode(
            options["fixed_options"], os.environ.get("AUTOSLICE_COVER_MODE", "").strip().lower(),
        )}
        if os.environ.get('AUTOSLICE_REVIEW_COVER_FAILURE') or os.environ.get('AUTOSLICE_REVIEW_COVER_FAILURE_SHA256'):
            current_options['retained_failure_sha256'] = os.environ.get('AUTOSLICE_REVIEW_COVER_FAILURE_SHA256', '').removeprefix('sha256:')
        if os.environ.get('AUTOSLICE_REUSE_COVER_RESULT'):
            current_options['retained_result_sha256'] = os.environ['AUTOSLICE_REUSE_COVER_RESULT_SHA256'].removeprefix('sha256:')
        current_story = materialized_recut.get("story_contract")
        current = editorial_input_binding(
            materialized_recut, candidate_id=candidate_id, title=title,
            selection_hook=str(current_story.get("selection_hook") or "") if isinstance(current_story, Mapping) else "",
            editorial_research=materialized_recut.get("editorial_research", current_story.get("editorial_research") if isinstance(current_story, Mapping) else None),
        )
        return generation_identity(
            record=materialized_recut, media_path=media_path, title=title,
            cover_text=cover_text, inputs=current, options=current_options,
            profile=CHANNEL_PROFILE, repo_root=ROOT, provider=provider,
            generator=_stage_ai_cover_unbound,
        )

    def produce(root):
        if os.environ.get('AUTOSLICE_REUSE_COVER_RESULT'):
            return _reuse_retained_successful_cover(artifact_root=Path(artifact_root), candidate_id=candidate_id,
                                                    expected=identity())
        if os.environ.get('AUTOSLICE_REVIEW_COVER_FAILURE') or os.environ.get('AUTOSLICE_REVIEW_COVER_FAILURE_SHA256'):
            return _review_retained_failed_cover(
                artifact_root=Path(kwargs.get('retained_artifact_root') or artifact_root), candidate_id=candidate_id,
                expected=identity(), attempt=root, prepare_qc_only=bool(kwargs.get('retained_prepare_qc_only')))
        return _bind_lidousha_cover_generation(
            materialized_recut, media_path=media_path, candidate_id=candidate_id,
            title=title, cover_text=cover_text, **{**kwargs, 'private_artifact_root': root})

    try:
        return successful_generation(
            artifact_root=Path(artifact_root), candidate_id=candidate_id, identity=identity,
            produce=produce,
        )
    except (OSError, ValueError, TypeError) as exc:
        return _blocked_ai_cover_result(
            {"input_binding": inputs}, ["COVER_GENERATION_CHECKPOINT_BLOCKED"], str(exc),
        )


def _bind_lidousha_cover_generation(materialized_recut, *, media_path, candidate_id, title, cover_text, **kwargs):
    """Freeze the normal director/image inputs and retain actual response identity."""
    story = materialized_recut.get("story_contract")
    inputs = editorial_input_binding(
        materialized_recut, candidate_id=candidate_id, title=title,
        selection_hook=str(story.get("selection_hook") or "") if isinstance(story, Mapping) else "",
        editorial_research=materialized_recut.get("editorial_research", story.get("editorial_research") if isinstance(story, Mapping) else None),
    )
    result = _stage_ai_cover_unbound(
        materialized_recut, media_path=media_path, candidate_id=candidate_id,
        title=title, cover_text=cover_text, **kwargs,
    )
    current = editorial_input_binding(
        materialized_recut, candidate_id=candidate_id, title=title,
        selection_hook=str(story.get("selection_hook") or "") if isinstance(story, Mapping) else "",
        editorial_research=materialized_recut.get("editorial_research", story.get("editorial_research") if isinstance(story, Mapping) else None),
    )
    generation = dict(result.get("cover_generation") or {})
    generation["input_binding"] = inputs
    local_direct = (
        generation.get("method") == "screenshot_direct"
        and generation.get("image_generation_used") is False
    )
    # Hash the request that produced the final pixels, including any identity
    # retry. The rejected first request remains in its existing history fields.
    for key in ("request", "response"):
        path_value = generation.get(key + "_path")
        path = Path(str(path_value or ""))
        if path_value and path.is_file() and not path.is_symlink():
            generation[key + "_sha256"] = "sha256:" + _sha256(path)
        elif result.get("status") == "AI_COVER_READY" and not local_direct:
            return _blocked_ai_cover_result(
                generation, ["CURRENT_EDITORIAL_NETWORK_EVIDENCE_MISSING"],
                f"final cover {key} file is missing or unsafe",
            )
    result = {**result, "cover_generation": generation}
    if current != inputs:
        return _blocked_ai_cover_result(generation, ["CURRENT_EDITORIAL_INPUT_DRIFT"], "editorial input changed during cover generation")
    return result


def _stage_ai_cover_unbound(
    materialized_recut: Mapping[str, object],
    *,
    media_path: Path,
    candidate_id: str,
    title: str,
    cover_text: str,
    run_ffmpeg: bool,
    art_direction_llm_call: LlmCall | None = None,
    image_edit: Callable[..., dict[str, object]] = _cover_call_cpa_image_edit,
    final_participant_verifier: (Callable[..., Mapping[str, object]] | None) = None,
    final_host_identity_verifier: (Callable[..., Mapping[str, object]] | None) = None,
    source_composition_verifier: (Callable[..., Mapping[str, object]] | None) = None,
    enforce_final_host_identity: bool = False,
    punch_allowed: bool = False,
    full_text_cover_contract: Mapping[str, object] | None = None,
    diversity_slot: int | None = None,
    private_artifact_root: Path | None = None,
    fixed_options: FixedCoverStageOptions = FixedCoverStageOptions(),
) -> dict[str, object]:
    # Song covers and frozen/manual repair contracts keep their own gates.
    # 70-cover: only the ordinary ``auto`` route runs the skill candidate
    # comparison; an explicitly forced AUTOSLICE_COVER_MODE
    # (screenshot/polish/cpa) keeps its documented forced-route contract.
    forced_environment_mode = resolved_cover_mode(
        fixed_options, os.environ.get("AUTOSLICE_COVER_MODE", "").strip().lower()
    )
    if (
        _lidousha_is_song_title(title)
        or fixed_options != FixedCoverStageOptions()
        or forced_environment_mode != "auto"
        or validate_full_text_cover_contract(full_text_cover_contract, cover_text=cover_text)
    ):
        return _stage_legacy_lidousha_ai_cover_unbound(
            materialized_recut, media_path=media_path, candidate_id=candidate_id,
            title=title, cover_text=cover_text, run_ffmpeg=run_ffmpeg,
            art_direction_llm_call=art_direction_llm_call, image_edit=image_edit,
            final_participant_verifier=final_participant_verifier,
            final_host_identity_verifier=final_host_identity_verifier,
            source_composition_verifier=source_composition_verifier,
            enforce_final_host_identity=enforce_final_host_identity,
            punch_allowed=punch_allowed, full_text_cover_contract=full_text_cover_contract,
            diversity_slot=diversity_slot, private_artifact_root=private_artifact_root,
            fixed_options=fixed_options,
        )
    cover_generation: dict[str, object] = {
        "workflow": LIDOUSHA_COVER_WORKFLOW,
        "method": "images.edit",
        "model": _cpa_image_model_candidates()[0],
        "image_gen_model": "cpa",
        "fallback_used": False,
        "model_fallback_used": False,
        "cover_text": cover_text,
        "cover_punch_allowed": punch_allowed,
        "cover_diversity_slot": diversity_slot,
        "title": title,
    }
    if validate_full_text_cover_contract(
        full_text_cover_contract,
        cover_text=cover_text,
    ):
        cover_generation["full_text_cover_contract"] = dict(full_text_cover_contract)
    story_contract = materialized_recut.get("story_contract")
    if isinstance(story_contract, Mapping):
        cover_generation["story_contract"] = cover_story_contract_binding(story_contract)




    cover_mode = resolved_cover_mode(fixed_options, os.environ.get("AUTOSLICE_COVER_MODE", "").strip().lower()) or "auto"
    cover_generation["cover_mode"] = cover_mode
    base_url, api_key = _resolve_cover_cpa_credentials()
    if (not base_url or not api_key) and cover_mode == "cpa":
        return _blocked_ai_cover_result(
            cover_generation,
            ["CPA_AI_COVER_REQUIRED", "CPA_CREDENTIALS_MISSING"],
            "CPA_BASE_URL/CPA_API_KEY missing; deterministic frame covers are not publish-grade",
        )
    if not run_ffmpeg:
        return _blocked_ai_cover_result(
            cover_generation,
            ["CPA_AI_COVER_REQUIRED", "COVER_REFERENCE_EXTRACTION_DISABLED"],
            "ffmpeg disabled, so no identity/reference frame can be extracted for CPA images.edit",
        )
    artifact_root = private_artifact_root or materialized_artifact_root(materialized_recut, media_path)
    cover_refs_dir = artifact_root / "cover_refs"
    ai_dir = artifact_root / "covers_ai_original"
    covers_dir = artifact_root / "covers"
    evidence_dir = artifact_root / "evidence"
    for directory in (cover_refs_dir, ai_dir, covers_dir, evidence_dir):
        directory.mkdir(parents=True, exist_ok=True)
    from .title_cover_staging import stage_title_cover_candidates, pick_final_srt_frames
    exact_srt, _ = _staged_final_srt_text_and_sha256(materialized_recut, ())
    try:
        # Font preflight precedes image/text spending and frame extraction.
        from .cover_private_font import resolve_private_cover_font
        resolve_private_cover_font()
        frames = pick_final_srt_frames(media_path, exact_srt or "", dict(materialized_recut), cover_refs_dir)
        cover_generation["reference_selection"] = {
            "policy": "lidousha-title-cover-final-srt-frames.v1",
            "best_ms": frames[0]["frame_ms"],
            "frames": [{key: str(value) if isinstance(value, Path) else value for key, value in row.items()} for row in frames],
            "media_sha256": "sha256:" + _sha256(media_path), "audio_used": False,
        }
        return stage_title_cover_candidates(
            record=dict(materialized_recut), candidate_id=candidate_id,
            title=title, cover_text=cover_text, final_srt=exact_srt or "",
            reference_path=frames[0]["path"], extra_frames=frames[1:],
            artifact_root=artifact_root, generation=cover_generation,
            llm_call=art_direction_llm_call, image_edit=image_edit,
            emote_library=load_emote_library(ROOT), repo_root=ROOT,
            history_root=CHANNEL_PROFILE.delivery_root_for(ROOT),
            base_url=base_url, api_key=api_key,
        )
    except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
        return _blocked_ai_cover_result(
            cover_generation, ["TITLE_COVER_SKILL_BLOCKED", str(exc).split(":")[0]],
            str(exc),
        )


def _stage_legacy_lidousha_ai_cover_unbound(
    materialized_recut: Mapping[str, object],
    *,
    media_path: Path,
    candidate_id: str,
    title: str,
    cover_text: str,
    run_ffmpeg: bool,
    art_direction_llm_call: LlmCall | None = None,
    image_edit: Callable[..., dict[str, object]] = _cover_call_cpa_image_edit,
    final_participant_verifier: (Callable[..., Mapping[str, object]] | None) = None,
    final_host_identity_verifier: (Callable[..., Mapping[str, object]] | None) = None,
    source_composition_verifier: (Callable[..., Mapping[str, object]] | None) = None,
    enforce_final_host_identity: bool = False,
    punch_allowed: bool = False,
    full_text_cover_contract: Mapping[str, object] | None = None,
    diversity_slot: int | None = None,
    private_artifact_root: Path | None = None,
    fixed_options: FixedCoverStageOptions = FixedCoverStageOptions(),
) -> dict[str, object]:
    cover_generation: dict[str, object] = {
        "workflow": LIDOUSHA_COVER_WORKFLOW,
        "method": "images.edit",
        "model": _cpa_image_model_candidates()[0],
        "image_gen_model": "cpa",
        "fallback_used": False,
        "model_fallback_used": False,
        "cover_text": cover_text,
        "cover_punch_allowed": punch_allowed,
        "cover_diversity_slot": diversity_slot,
        "title": title,
    }
    if validate_full_text_cover_contract(
        full_text_cover_contract,
        cover_text=cover_text,
    ):
        cover_generation["full_text_cover_contract"] = dict(full_text_cover_contract)
    story_contract = materialized_recut.get("story_contract")
    if isinstance(story_contract, Mapping):
        cover_generation["story_contract"] = cover_story_contract_binding(story_contract)




    cover_mode = resolved_cover_mode(fixed_options, os.environ.get("AUTOSLICE_COVER_MODE", "").strip().lower()) or "auto"
    cover_generation["cover_mode"] = cover_mode
    base_url, api_key = _resolve_cover_cpa_credentials()
    if (not base_url or not api_key) and cover_mode == "cpa":
        return _blocked_ai_cover_result(
            cover_generation,
            ["CPA_AI_COVER_REQUIRED", "CPA_CREDENTIALS_MISSING"],
            "CPA_BASE_URL/CPA_API_KEY missing; deterministic frame covers are not publish-grade",
        )
    if not run_ffmpeg:
        return _blocked_ai_cover_result(
            cover_generation,
            ["CPA_AI_COVER_REQUIRED", "COVER_REFERENCE_EXTRACTION_DISABLED"],
            "ffmpeg disabled, so no identity/reference frame can be extracted for CPA images.edit",
        )
    artifact_root = private_artifact_root or materialized_artifact_root(materialized_recut, media_path)
    cover_refs_dir = artifact_root / "cover_refs"
    ai_dir = artifact_root / "covers_ai_original"
    covers_dir = artifact_root / "covers"
    evidence_dir = artifact_root / "evidence"
    for directory in (cover_refs_dir, ai_dir, covers_dir, evidence_dir):
        directory.mkdir(parents=True, exist_ok=True)
    (
        reference_path,
        frame_selection,
        reference_authority,
        reference_block,
    ) = _prepare_lidousha_cover_reference(
        materialized_recut,
        media_path=media_path,
        candidate_id=candidate_id,
        story_contract=(story_contract if isinstance(story_contract, Mapping) else None),
        cover_refs_dir=cover_refs_dir,
        cover_generation=cover_generation,
    )
    if reference_block is not None:
        return reference_block
    assert reference_path is not None
    source_composition_verification: Mapping[str, object] | None = None
    if enforce_final_host_identity:
        active_source_composition_verifier = (
            source_composition_verifier or verify_source_composition
        )
        reference_sha256 = "sha256:" + _sha256(reference_path)
        source_composition_verification = run_source_composition_witness(
            active_source_composition_verifier,
            reference_path=reference_path,
            reference_sha256=reference_sha256,
            story_hook=(
                str(story_contract.get("selection_hook") or "")
                if isinstance(story_contract, Mapping)
                else ""
            ),
            title=title,
            base_url=base_url,
            api_key=api_key,
            frame_selection=frame_selection,
        )
        cover_generation["source_composition_verification"] = dict(source_composition_verification)
        if not validate_source_composition_verification(
            source_composition_verification,
            reference_sha256=reference_sha256,
        ):
            detail = str(
                source_composition_verification.get("detail")
                or source_composition_verification.get("reason_code")
                or "source-composition witness is unavailable or invalid"
            )
            return _blocked_ai_cover_result(
                cover_generation,
                [
                    "COVER_SOURCE_COMPOSITION_UNVERIFIED",
                    str(
                        source_composition_verification.get("reason_code")
                        or "SOURCE_COMPOSITION_VERDICT_INVALID"
                    ),
                ],
                detail,
            )
        source_composition_path = (
            evidence_dir / f"{candidate_id}.cover-source-composition-verification.json"
        )
        _write_json_file(
            source_composition_path,
            source_composition_verification,
        )
        cover_generation["source_composition_receipt"] = {
            "path": str(source_composition_path),
            "sha256": "sha256:" + _sha256(source_composition_path),
        }
    emote_library = load_emote_library(ROOT)
    director_scene_kind = source_composition_scene_kind(
        source_composition_verification
    )
    director_relationship_visual_required = relationship_visual_safety_required(
        story_contract
    )
    exact_srt_text, _ = _staged_final_srt_text_and_sha256(materialized_recut, ())
    director_builder = partial(
        _cover_art_direction,
        scene_kind=director_scene_kind,
        relationship_visual_required=director_relationship_visual_required,
        research_story_context=(dict(story_contract) if isinstance(story_contract, Mapping) else None),
        final_srt_text=exact_srt_text or None,
    )
    art_direction = fixed_art_direction(
        director_builder, candidate_id=candidate_id, title=title,
        cover_text=cover_text, llm_call=art_direction_llm_call, emote_library=emote_library,
        punch_allowed=punch_allowed, diversity_slot=diversity_slot,
        story_hook=(str(story_contract.get("selection_hook") or "") if isinstance(story_contract, Mapping) else ""),
        options=fixed_options,
    )
    if art_direction is None:
        return _blocked_ai_cover_result(
            cover_generation, ["COVER_APPROVED_PUNCH_RECEIPT_INVALID"],
            "fixed cover repair requires its canonical approved-punch receipt",
        )
    # 路由：语义/人物证据先行，几何只决定已经验真人物的构图处理。
    treatment, route = _build_lidousha_cover_route(
        cover_generation=cover_generation,
        story_contract=story_contract,
        title=title,
        cover_text=cover_text,
        cover_mode=cover_mode,
        art_direction=art_direction,
        punch_allowed=punch_allowed,
        full_text_cover_contract=(
            cover_generation.get("full_text_cover_contract")
            if isinstance(
                cover_generation.get("full_text_cover_contract"),
                Mapping,
            )
            else None
        ),
        frame_selection=frame_selection,
        reference_authority=reference_authority,
        source_composition_verification=source_composition_verification,
        enforce_final_host_identity=enforce_final_host_identity,
    )
    if (
        not art_direction.is_song
        and not art_direction.cover_punch
        and cover_text_requires_punch_for_thumbnail(cover_text)
        and "full_text_cover_contract" not in cover_generation
    ):
        detail = (
            "talk cover needs a CPA-reviewed 1-2 line punch before any image "
            "route can materialize; title authority and an empty/failed punch "
            "review do not authorize a long full-title cover"
        )
        cover_generation["art_direction"] = asdict(art_direction)
        record_cover_route_execution(
            cover_generation,
            actual_treatment=None,
            execution_status="BLOCKED",
            image_generation_attempted=False,
            image_generation_used=False,
            detail=detail,
        )
        return _blocked_ai_cover_result(
            cover_generation,
            [
                "COVER_PUNCH_REQUIRED_FOR_THUMBNAIL",
                "CPA_PUNCH_SEMANTIC_REVIEW_REQUIRED",
            ],
            detail,
        )
    if reference_authority is not None and treatment != reference_authority.get(
        "required_treatment"
    ):
        detail = (
            f"authority requires {reference_authority.get('required_treatment')}, "
            f"router selected {treatment}"
        )
        record_cover_route_execution(
            cover_generation,
            actual_treatment=None,
            execution_status="BLOCKED",
            image_generation_attempted=False,
            image_generation_used=False,
            detail=detail,
        )
        return _blocked_ai_cover_result(
            cover_generation,
            ["COVER_REFERENCE_REQUIRED_TREATMENT_NOT_SELECTED"],
            detail,
        )
    relationship_visual_required = relationship_visual_safety_required(
        story_contract,
        route_decision=route,
    )
    if relationship_visual_required and not (relationship_source_participants_verified(route)):
        detail = (
            "relationship cover requires a hash-bound source reference that "
            "visibly verifies every required participant before composition "
            "geometry or image generation can be considered"
        )
        record_cover_route_execution(
            cover_generation,
            actual_treatment=None,
            execution_status="BLOCKED",
            image_generation_attempted=False,
            image_generation_used=False,
            detail=detail,
        )
        return _blocked_ai_cover_result(
            cover_generation,
            ["RELATION_COVER_SOURCE_PARTICIPANTS_UNVERIFIED"],
            detail,
        )
    if isinstance(story_contract, Mapping):
        cover_generation["relation_cover_mode"] = (
            "VERIFIED_DUAL_STREAM_FRAME"
            if reference_authority is not None
            and treatment in ("screenshot_direct", "screenshot_polish")
            else "VERIFIED_STREAM_FRAME"
            if treatment in ("screenshot_direct", "screenshot_polish")
            and route.get("verified_stream_frame") is True
            else str(story_contract.get("cover_fallback_mode") or "HOST_ONLY_GENERIC")
        )
    demotion_detail: str | None = None
    if treatment in ("screenshot_direct", "screenshot_polish"):
        # The selected route is an authorization boundary: a materialization
        # failure never authorizes a SILENT redraw.  It is, however, positive
        # evidence that the screenshot lane is infeasible for this frame
        # (composition veto / crop failure), so we demote to cpa_redraw
        # EXPLICITLY: the screenshot BLOCKED receipt stays in the evidence,
        # the redraw runs through its own precondition gates below, and the
        # final execution records READY_DEGRADED with the failure detail —
        # the authorization chain shows exactly what happened.
        result = _stage_screenshot_direct_cover(
            media_path=media_path,
            candidate_id=candidate_id,
            cover_text=cover_text,
            art_direction=art_direction,
            frame_selection=frame_selection,
            reference_path=reference_path,
            ai_dir=ai_dir,
            covers_dir=covers_dir,
            evidence_dir=evidence_dir,
            cover_generation=cover_generation,
            polish=(treatment == "screenshot_polish"),
            image_edit=image_edit,
            final_participant_verifier=final_participant_verifier,
            final_host_identity_verifier=final_host_identity_verifier,
            base_url=base_url,
            api_key=api_key,
            full_text_cover_contract=full_text_cover_contract,
            identity_landmark_title_exclusion=(
                fixed_options.identity_landmark_title_exclusion
            ),
        )
        result = _enforce_final_talk_cover_thumbnail_gate(result)
        if "SCREENSHOT_ROUTE_MATERIALIZATION_FAILED" not in (result.get("reason_codes") or []):
            return result
        if fixed_options.require_screenshot_direct:
            return result
        screenshot_receipt = cover_generation.get("screenshot_direct")
        demotion_detail = f"demoted from {treatment}: " + str(
            (screenshot_receipt or {}).get("detail")
            if isinstance(screenshot_receipt, Mapping)
            else ""
        )
        cover_generation["route_demotion"] = {
            "schema_version": "cover-route-demotion.v1",
            "from_treatment": treatment,
            "to_treatment": "cpa_redraw",
            "reason_codes": list(result.get("reason_codes") or []),
            "detail": demotion_detail,
        }
    if route.get("host_identity_required") is True and final_host_identity_verifier is None:
        detail = (
            "cover requires a CPA-primary source/final host "
            "identity verifier bound to the final cover hash"
        )
        record_cover_route_execution(
            cover_generation,
            actual_treatment=None,
            execution_status="BLOCKED",
            image_generation_attempted=False,
            image_generation_used=False,
            detail=detail,
        )
        return _blocked_ai_cover_result(
            cover_generation,
            ["COVER_FINAL_HOST_IDENTITY_VERIFIER_REQUIRED"],
            detail,
        )
    if relationship_visual_required and final_participant_verifier is None:
        detail = (
            "relationship AI redraw requires an independent verifier bound "
            "to the final cover hash and every required participant"
        )
        record_cover_route_execution(
            cover_generation,
            actual_treatment=None,
            execution_status="BLOCKED",
            image_generation_attempted=False,
            image_generation_used=False,
            detail=detail,
        )
        return _blocked_ai_cover_result(
            cover_generation,
            ["RELATION_COVER_FINAL_PARTICIPANT_VERIFIER_REQUIRED"],
            detail,
        )
    if not base_url or not api_key:
        detail = "CPA_BASE_URL/CPA_API_KEY missing for the selected cpa_redraw route"
        record_cover_route_execution(
            cover_generation,
            actual_treatment=None,
            execution_status="BLOCKED",
            image_generation_attempted=False,
            image_generation_used=False,
            detail=detail,
        )
        return _blocked_ai_cover_result(
            cover_generation,
            ["CPA_AI_COVER_REQUIRED", "CPA_CREDENTIALS_MISSING"],
            detail,
        )

    redraw_result = _stage_cpa_redraw_cover(
        candidate_id=candidate_id,
        title=title,
        cover_text=cover_text,
        story_contract=(story_contract if isinstance(story_contract, Mapping) else None),
        cover_refs_dir=cover_refs_dir,
        ai_dir=ai_dir,
        covers_dir=covers_dir,
        evidence_dir=evidence_dir,
        reference_path=reference_path,
        emote_library=emote_library,
        art_direction=art_direction,
        cover_generation=cover_generation,
        image_edit=image_edit,
        final_participant_verifier=final_participant_verifier,
        final_host_identity_verifier=final_host_identity_verifier,
        base_url=base_url,
        api_key=api_key,
        full_text_cover_contract=full_text_cover_contract,
        demotion_detail=demotion_detail,
    )
    result = _degrade_unavailable_redraw_identity_to_direct(
        redraw_result=redraw_result,
        media_path=media_path,
        candidate_id=candidate_id,
        cover_text=cover_text,
        art_direction=art_direction,
        frame_selection=frame_selection,
        reference_path=reference_path,
        ai_dir=ai_dir,
        covers_dir=covers_dir,
        evidence_dir=evidence_dir,
        image_edit=image_edit,
        final_participant_verifier=final_participant_verifier,
        final_host_identity_verifier=final_host_identity_verifier,
        base_url=base_url,
        api_key=api_key,
        full_text_cover_contract=full_text_cover_contract,
    )
    return _enforce_final_talk_cover_thumbnail_gate(result)


def _cover_reference_command(
    *,
    media_path: Path,
    reference_path: Path,
    override_ms: int | None,
    selected_ms: int | None,
) -> list[str]:
    """封面参考帧的 ffmpeg 命令：人工点名帧 > 表现力选帧 > thumbnail 代表帧。"""

    base = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    at_ms = override_ms if override_ms is not None else selected_ms
    if at_ms is not None:
        return base + [
            "-ss",
            f"{at_ms / 1000:.3f}",
            "-i",
            str(media_path),
            "-vf",
            "scale=1920:-2",
            "-frames:v",
            "1",
            str(reference_path),
        ]
    return base + [
        "-i",
        str(media_path),
        "-vf",
        "thumbnail=120,scale=1920:-2",
        "-frames:v",
        "1",
        str(reference_path),
    ]


def _screenshot_base_and_crop(
    *,
    media_path: Path,
    candidate_id: str,
    ai_dir: Path,
    reference_path: Path,
    frame_selection: Mapping[str, object],
    art_direction,
    relationship_visual_required: bool,
    source_composition_verification: Mapping[str, object] | None = None,
    source_composition_receipt: Mapping[str, object] | None = None,
):
    """Materialize the screenshot base frame and its hash-bound crop proof."""

    if relationship_visual_required:
        # A relationship cover cannot inherit the reference frame's
        # participant verdict through a zoom crop.  Keep the exact,
        # hash-bound full reference and reserve a separate banner for text.
        art_direction = dataclasses_replace(
            art_direction,
            layout="banner",
        )
        screenshot_base = reference_path
        crop_evidence = {
            "schema": "cover-frame-transfer.v1",
            "status": "HASH_BOUND_FULL_FRAME",
            # This stays on the content timeline.  The hash-bound source
            # authority may also carry an absolute source_time_ms, but the
            # transfer proof must bind the exact frame selected from the
            # materialized clip just like the cropped screenshot route.
            "frame_ms": int(frame_selection["best_ms"]),
            "source_path": str(reference_path),
            "source_sha256": "sha256:" + _sha256(reference_path),
            "crop_applied": False,
            "zoom": 1.0,
        }
    else:
        screenshot_base = ai_dir / f"{candidate_id}.screenshot-base.png"
    # 裁切策略：运动几何分不开"皮套大身位"和竖版
    # 手游列（都窄而高），真正的脸部识别放大要等 CPA-primary 图像见证。v1 保守：
    # 默认 1.16x 顶部锚定——恰好裁掉底部烧录字幕带、微裁两侧，任何场景都
    # 安全；只有局部运动呈高置信单主体块时才 1.32x 锚定主体（宁欠勿错）。
    if not relationship_visual_required:
        if isinstance(source_composition_verification, Mapping):
            if not isinstance(source_composition_receipt, Mapping):
                raise ValueError("SOURCE_COMPOSITION_RECEIPT_MISSING")
            receipt_path = Path(str(source_composition_receipt.get("path") or ""))
            receipt_sha256 = str(source_composition_receipt.get("sha256") or "")
            if not receipt_path.is_file() or "sha256:" + _sha256(receipt_path) != receipt_sha256:
                raise ValueError("SOURCE_COMPOSITION_RECEIPT_HASH_MISMATCH")
            crop_evidence = extract_authority_source_crop_or_full_frame(
                reference_path=reference_path,
                output_path=screenshot_base,
                frame_ms=int(frame_selection["best_ms"]),
                verification=source_composition_verification,
                verification_receipt_path=receipt_path,
                verification_receipt_sha256=receipt_sha256,
            )
        else:
            confident = bool(frame_selection.get("subject_confident"))
            camera_window = (
                frame_selection.get("camera_window_bbox_frac") if not confident else None
            )
            crop_evidence = extract_zoomed_cover_frame(
                media_path,
                int(frame_selection["best_ms"]),
                screenshot_base,
                zoom=1.32 if confident else 1.16,
                anchor_x_frac=(
                    float(frame_selection["subject_anchor_x_frac"])
                    if confident and frame_selection.get("subject_anchor_x_frac") is not None
                    # 本频道版式皮套居中偏右、弹幕栏在左：右倾锚点让 1.16x 裁切
                    # 优先吃掉左侧弹幕栏。
                    else 0.58
                ),
                head_top_frac=(
                    float(frame_selection["subject_head_top_frac"])
                    if confident and frame_selection.get("subject_head_top_frac") is not None
                    else 0.0
                ),
                window_bbox_frac=camera_window,
            )
    return art_direction, screenshot_base, crop_evidence


def _stage_screenshot_direct_cover(
    *,
    media_path: Path,
    candidate_id: str,
    cover_text: str,
    art_direction,
    frame_selection: Mapping[str, object],
    reference_path: Path,
    ai_dir: Path,
    covers_dir: Path,
    cover_generation: dict[str, object],
    evidence_dir: Path | None = None,
    polish: bool = False,
    image_edit: Callable[..., dict[str, object]] | None = None,
    final_participant_verifier: (Callable[..., Mapping[str, object]] | None) = None,
    final_host_identity_verifier: (Callable[..., Mapping[str, object]] | None) = None,
    base_url: str = "",
    full_text_cover_contract: Mapping[str, object] | None = None,
    api_key: str = "",
    identity_landmark_title_exclusion: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """截图路线封面：直出或 +CPA 轻微调；物化失败原路线内阻断。
    表现力选帧的最佳帧 → 裁切（吃掉弹幕栏/字幕带）→ [polish：CPA 逐像素保真
    修图（清 UI 杂物+画质），失败显式降级直出] → 叠梗字。截图/裁切/叠字
    任一步失败都保留证据并 fail closed，绝不静默切换成全图 AI 重绘。
    """
    # 手定标题只锁投稿文字 authority，不决定视觉路线或封面全文。所有 talk
    # 标题有 CPA 短梗时都用 punch 版式；只有独立显式的 full-text-cover
    # contract 才能授权完整 cover_text，否则最终缩略图门会 fail closed。
    cover_generation.update(
        {
            "method": "screenshot_polish" if polish else "screenshot_direct",
            "model": "cpa" if polish else "none",
            "image_gen_model": "cpa" if polish else "none",
            "image_generation_used": False,
        }
    )
    try:
        route = cover_generation.get("route_decision")
        relationship_visual_required = relationship_visual_safety_required(
            cover_generation.get("story_contract"),
            route_decision=route,
        )
        art_direction, screenshot_base, crop_evidence = _screenshot_base_and_crop(
            media_path=media_path,
            candidate_id=candidate_id,
            ai_dir=ai_dir,
            reference_path=reference_path,
            frame_selection=frame_selection,
            art_direction=art_direction,
            relationship_visual_required=relationship_visual_required,
            source_composition_verification=(
                cover_generation.get("source_composition_verification")
                if isinstance(
                    cover_generation.get("source_composition_verification"),
                    Mapping,
                )
                else None
            ),
            source_composition_receipt=(
                cover_generation.get("source_composition_receipt")
                if isinstance(
                    cover_generation.get("source_composition_receipt"),
                    Mapping,
                )
                else None
            ),
        )
        # polish：CPA 保真修图（清 UI 杂物+画质），任何失败降级为直出。
        (
            overlay_source,
            method,
            selected_model,
            attempted_models,
            polish_attempted,
        ) = _materialize_screenshot_polish(
            screenshot_base=screenshot_base,
            candidate_id=candidate_id,
            ai_dir=ai_dir,
            evidence_dir=evidence_dir,
            polish=polish,
            image_edit=image_edit,
            base_url=base_url,
            api_key=api_key,
            cover_generation=cover_generation,
        )
        (
            poster_evidence,
            overlay,
            face_verification,
        ) = _compose_screenshot_cover_with_face_gate(
            overlay_source=overlay_source,
            crop_evidence=crop_evidence,
            candidate_id=candidate_id,
            ai_dir=ai_dir,
            covers_dir=covers_dir,
            cover_text=cover_text,
            art_direction=art_direction,
            relationship_visual_required=relationship_visual_required,
            method=method,
            base_url=base_url,
            api_key=api_key,
            verifier=_verify_polish_face_integrity,
            full_text_cover_contract=full_text_cover_contract,
            identity_landmark_title_exclusion=identity_landmark_title_exclusion,
        )
        (
            poster_evidence,
            overlay,
            face_verification,
            method,
            selected_model,
        ) = _degrade_rejected_polish_to_direct(
            full_text_cover_contract=full_text_cover_contract,
            poster_evidence=poster_evidence,
            overlay=overlay,
            face_verification=face_verification,
            method=method,
            selected_model=selected_model,
            screenshot_base=screenshot_base,
            crop_evidence=crop_evidence,
            candidate_id=candidate_id,
            ai_dir=ai_dir,
            covers_dir=covers_dir,
            cover_text=cover_text,
            art_direction=art_direction,
            relationship_visual_required=relationship_visual_required,
            base_url=base_url,
            api_key=api_key,
            cover_generation=cover_generation,
            verifier=_verify_polish_face_integrity,
        )
        poster_path = ai_dir / f"{candidate_id}.screenshot-poster.png"
        final_cover_path = covers_dir / f"{candidate_id}.screenshot-title.cover.png"
        if face_verification is not None:
            cover_generation["polish_face_verification"] = face_verification
        overlay_source = poster_path
        cover_generation["screenshot_graphic_poster"] = poster_evidence
        cover_generation["art_direction"] = asdict(art_direction)
        if art_direction.emote_id:
            cover_generation["emote"] = {"status": "IGNORED_SCREENSHOT_MODE"}
        cover_generation.update(
            {
                "method": method,
                "model": selected_model,
                "image_gen_model": selected_model,
                "cover_origin": (
                    "SOURCE_SCREENSHOT"
                    if method == "screenshot_direct"
                    else "SOURCE_SCREENSHOT_AI_POLISH"
                ),
                "image_generation_used": method == "screenshot_polish",
                "screenshot_frame": crop_evidence,
                "reference_image": str(reference_path),
                "reference_sha256": "sha256:" + _sha256(reference_path),
                "ai_background": str(overlay_source),
                "ai_background_sha256": "sha256:" + _sha256(overlay_source),
                "final_cover": str(final_cover_path),
                "final_cover_sha256": "sha256:" + _sha256(final_cover_path),
                "attempted_models": attempted_models,
                **overlay,
            }
        )
        if isinstance(route, Mapping) and route.get("host_identity_required") is True:
            host_identity_verification = run_final_host_identity_witness(
                final_host_identity_verifier,
                final_cover_path=final_cover_path,
                final_cover_sha256=cover_generation["final_cover_sha256"],
                reference_path=reference_path,
                base_url=base_url,
                api_key=api_key,
                cover_generation=cover_generation,
            )
            cover_generation["final_host_identity_verification"] = dict(host_identity_verification)
            if not validate_final_host_identity_verification(cover_generation):
                detail = "final cover pixels do not have a PASS host identity verdict: " + str(
                    host_identity_verification.get("reason_code") or "VERIFICATION_MISSING"
                )
                record_cover_route_execution(
                    cover_generation,
                    actual_treatment=None,
                    execution_status="BLOCKED",
                    image_generation_attempted=polish_attempted,
                    image_generation_used=True,
                    detail=detail,
                )
                return _blocked_ai_cover_result(
                    cover_generation,
                    ["COVER_FINAL_HOST_IDENTITY_UNVERIFIED"],
                    detail,
                )
        polish_face_detail = _polish_face_binding_failure(
            cover_generation, face_verification, method
        )
        if polish_face_detail is not None:
            record_cover_route_execution(
                cover_generation,
                actual_treatment=None,
                execution_status="BLOCKED",
                image_generation_attempted=polish_attempted,
                image_generation_used=True,
                detail=polish_face_detail,
            )
            return _blocked_ai_cover_result(
                cover_generation,
                ["COVER_POLISH_FACE_UNVERIFIED"],
                polish_face_detail,
            )
        final_participant_verification = None
        if relationship_visual_required:
            if method == "screenshot_direct":
                final_participant_verification = build_no_crop_participant_verification(
                    cover_generation
                )
            elif final_participant_verifier is not None:
                final_participant_verification = dict(
                    final_participant_verifier(
                        final_cover_path=final_cover_path,
                        final_cover_sha256=cover_generation["final_cover_sha256"],
                        required_participant_ids=list(route.get("required_participant_ids") or []),
                        source_reference_authority=(
                            cover_generation.get("story_contract", {}).get(
                                "cover_reference_authority"
                            )
                            if isinstance(
                                cover_generation.get("story_contract"),
                                Mapping,
                            )
                            else None
                        ),
                    )
                )
            if final_participant_verification is not None:
                cover_generation["final_participant_verification"] = final_participant_verification
            if not validate_final_participant_verification(cover_generation):
                detail = (
                    "relationship screenshot lacks a PASS final-pixel "
                    "participant verdict bound to the final cover hash"
                )
                record_cover_route_execution(
                    cover_generation,
                    actual_treatment=None,
                    execution_status="BLOCKED",
                    image_generation_attempted=polish_attempted,
                    image_generation_used=method == "screenshot_polish",
                    detail=detail,
                )
                return _blocked_ai_cover_result(
                    cover_generation,
                    ["RELATION_COVER_FINAL_PARTICIPANTS_UNVERIFIED"],
                    detail,
                )
        degraded = polish and method == "screenshot_direct"
        record_cover_route_execution(
            cover_generation,
            actual_treatment=method,
            execution_status="READY_DEGRADED" if degraded else "READY",
            image_generation_attempted=polish_attempted,
            image_generation_used=method == "screenshot_polish",
            final_participant_verification=final_participant_verification,
            detail=(
                str(
                    (
                        cover_generation.get("screenshot_polish")
                        if isinstance(cover_generation.get("screenshot_polish"), Mapping)
                        else {}
                    ).get("detail")
                    or "screenshot polish failed; direct source screenshot retained"
                )
                if degraded
                else None
            ),
        )
        return {
            "status": "AI_COVER_READY",
            "reason_codes": [],
            "cover_path": str(final_cover_path),
            "cover_generation": cover_generation,
            "cover_sha256": "sha256:" + _sha256(final_cover_path),
            "ai_background_sha256": "sha256:" + _sha256(overlay_source),
            "cover_reference_sha256": "sha256:" + _sha256(reference_path),
        }
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
        cover_generation["screenshot_direct"] = {
            "status": "BLOCKED",
            "reason_code": "SCREENSHOT_ROUTE_MATERIALIZATION_FAILED",
            "detail": detail,
        }
        route = cover_generation.get("route_decision")
        selected = str(route.get("selected_treatment") or "") if isinstance(route, Mapping) else ""
        record_cover_route_execution(
            cover_generation,
            actual_treatment=None,
            execution_status="BLOCKED",
            image_generation_attempted=bool(
                isinstance(cover_generation.get("screenshot_polish"), Mapping)
                and cover_generation["screenshot_polish"].get("image_generation_attempted")
            ),
            image_generation_used=False,
            detail=detail,
        )
        return _blocked_ai_cover_result(
            cover_generation,
            [
                "SCREENSHOT_ROUTE_MATERIALIZATION_FAILED",
                (
                    "SCREENSHOT_POLISH_ROUTE_FAILED"
                    if selected == "screenshot_polish"
                    else "SCREENSHOT_DIRECT_ROUTE_FAILED"
                ),
            ],
            detail,
        )


def _degrade_unavailable_redraw_identity_to_direct(
    *,
    redraw_result: dict[str, object],
    media_path: Path,
    candidate_id: str,
    cover_text: str,
    art_direction: LidoushaCoverArtDirection,
    frame_selection: Mapping[str, object],
    reference_path: Path,
    ai_dir: Path,
    covers_dir: Path,
    evidence_dir: Path,
    image_edit: Callable[..., dict[str, object]],
    final_participant_verifier: Callable[..., Mapping[str, object]] | None,
    final_host_identity_verifier: Callable[..., Mapping[str, object]] | None,
    base_url: str,
    api_key: str,
    full_text_cover_contract: Mapping[str, object] | None,
) -> dict[str, object]:
    """Keep source pixels when the independent redraw identity witness is down."""

    generation = redraw_result.get("cover_generation")
    if not isinstance(generation, dict) or not (
        redraw_result.get("status") == "BLOCKED_AI_COVER_REQUIRED"
        and final_host_identity_witness_unavailable(generation)
    ):
        return redraw_result
    source_composition = generation.get("source_composition_verification")
    if source_composition_recommends_redraw(source_composition):
        # The pre-generation v2 route is an authorization boundary.  A later
        # v3 witness outage cannot silently turn a source-composition redraw
        # mandate into screenshot pixels.
        return redraw_result
    verification = generation.get("final_host_identity_verification")
    generation["cpa_redraw"] = {
        "schema_version": "lidousha-cpa-redraw-degradation.v1",
        "status": "DEGRADED_TO_DIRECT_IDENTITY_WITNESS_UNAVAILABLE",
        "reason_code": (
            verification.get("reason_code")
            if isinstance(verification, Mapping)
            else "HOST_IDENTITY_WITNESS_UNAVAILABLE"
        ),
        "attempted_final_cover": generation.get("final_cover"),
        "attempted_final_cover_sha256": generation.get("final_cover_sha256"),
        "identity_verification": (dict(verification) if isinstance(verification, Mapping) else {}),
    }
    generation.pop("final_host_identity_verification", None)
    direct_result = _stage_screenshot_direct_cover(
        media_path=media_path,
        candidate_id=candidate_id,
        cover_text=cover_text,
        art_direction=art_direction,
        frame_selection=frame_selection,
        reference_path=reference_path,
        ai_dir=ai_dir,
        covers_dir=covers_dir,
        evidence_dir=evidence_dir,
        cover_generation=generation,
        polish=False,
        image_edit=image_edit,
        final_participant_verifier=final_participant_verifier,
        final_host_identity_verifier=final_host_identity_verifier,
        base_url=base_url,
        api_key=api_key,
        full_text_cover_contract=full_text_cover_contract,
    )
    if direct_result.get("status") != "AI_COVER_READY":
        return direct_result
    direct_generation = direct_result.get("cover_generation")
    if not isinstance(direct_generation, dict):
        return direct_result
    record_cover_route_execution(
        direct_generation,
        actual_treatment="screenshot_direct",
        execution_status="READY_DEGRADED",
        image_generation_attempted=True,
        image_generation_used=False,
        detail=(
            "CPA redraw identity witness was unavailable; retained a "
            "hash-bound source screenshot instead of unverified AI pixels"
        ),
        final_participant_verification=(
            direct_generation.get("final_participant_verification")
            if isinstance(
                direct_generation.get("final_participant_verification"),
                Mapping,
            )
            else None
        ),
    )
    direct_generation["status"] = "READY_DEGRADED"
    direct_generation["detail"] = (
        "CPA redraw identity witness was unavailable; retained a hash-bound "
        "source screenshot instead of unverified AI pixels"
    )
    return direct_result


def _enforce_final_talk_cover_thumbnail_gate(
    result: dict[str, object],
) -> dict[str, object]:
    """Fail a freshly materialized talk cover before it can become review-ready."""

    if result.get("status") != "AI_COVER_READY":
        return result
    generation = result.get("cover_generation")
    if not isinstance(generation, dict):
        return result
    cover_text = str(generation.get("cover_text") or "")
    violations = talk_cover_thumbnail_gate_violations(
        generation,
        cover_text=cover_text,
    )
    generation["thumbnail_text_gate"] = {
        "schema_version": "lidousha-cover-thumbnail-text-gate.v1",
        "status": "FAIL" if violations else "PASS",
        "max_physical_lines": COVER_THUMBNAIL_MAX_LINES,
        "max_line_em_width": PUNCH_LINE_MAX_EM,
        "rendered_lines": list(generation.get("rendered_lines") or []),
        "reason_codes": list(violations),
        "full_text_cover_contract_exemption": bool(
            violations == ()
            and generation.get("cover_text_mode") == "full"
            and validate_full_text_cover_contract(
                generation.get("full_text_cover_contract"),
                cover_text=cover_text,
            )
        ),
    }
    if not violations:
        return result
    detail = (
        "talk cover final text violates the universal 1-2 physical line / "
        f"{PUNCH_LINE_MAX_EM:g}em-per-line thumbnail contract"
    )
    record_cover_route_execution(
        generation,
        actual_treatment=None,
        execution_status="BLOCKED",
        image_generation_attempted=(generation.get("image_generation_attempted") is True),
        image_generation_used=(generation.get("image_generation_used") is True),
        detail=detail,
    )
    return _blocked_ai_cover_result(generation, violations, detail)


def _blocked_ai_cover_result(
    cover_generation: Mapping[str, object], reason_codes: Sequence[str], detail: str
) -> dict[str, object]:
    generation = {**dict(cover_generation), "status": "BLOCKED", "detail": detail}
    return {
        "status": "BLOCKED_AI_COVER_REQUIRED",
        "reason_codes": list(dict.fromkeys(reason_codes)),
        "cover_path": None,
        "cover_generation": generation,
    }


def _staged_final_srt_text_and_sha256(
    record: Mapping[str, object], cues: Sequence[SourceCue]
) -> tuple[str, str | None]:
    """Read the exact current subtitle bytes used by normal staging.

    The fallback is only for the existing cue-bound shadow fixtures. Production
    candidate projections require the regular subtitle artifact so their hash
    can be carried into the publish draft and same-stem tag consumer.
    """

    subtitle_path = record.get("subtitle_path")
    if isinstance(subtitle_path, str) and subtitle_path.strip():
        path = Path(subtitle_path)
        if path.is_file() and not path.is_symlink():
            try:
                raw = path.read_bytes()
                return raw.decode("utf-8"), "sha256:" + hashlib.sha256(raw).hexdigest()
            except (OSError, UnicodeError):
                pass
    fallback = "\n".join(cue.text for cue in cues if cue.text.strip())
    return fallback, None


# End of staging helpers.
