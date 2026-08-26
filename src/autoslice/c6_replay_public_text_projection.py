"""C6-only public-text composition proof for reviewed-baseline replay.

This module is a derived consumption lane, not a new text authority.  It first
consumes the existing v1 public-text authority against the persisted record,
then proves that the canonical StoryContract rebuilt from the sealed reviewed
baseline differs only at the explicitly recorded replay projection.  The
normal resolver remains strict for every other caller.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.autoslice.candidate_public_text_surface_authority import (
    CandidatePublicTextSurfaceAuthorityError,
)
from src.autoslice.repository_asset_authority import (
    RepositoryAssetAuthorityError,
    require_repository_asset_authority,
)


CANDIDATE_ID = "auto_120032_753_816"
RECORDING_DATE = "2026-08-14"
SCHEMA_VERSION = "c6-replay-public-text-projection-consumption.v1"
RELATIVE_PUBLIC_TEXT_AUTHORITY = Path(
    "assets/lidousha/candidate_public_text_surface_authorities/"
    f"{CANDIDATE_ID}.public-text-surface-authority.v1.json"
)
RELATIVE_BASELINE_MANIFEST = Path(
    "assets/lidousha/reviewed_subtitle_baselines/"
    f"{CANDIDATE_ID}.subtitle-baseline.v1.json"
)

# This is the complete canonical projection observed on clean 981.  Any other
# path, including any title/cover/clip/entity/boundary/scorecard metadata,
# rejects closed.  ``cover_output_audits`` is absent from a freshly rebuilt
# StoryContract and is therefore explicitly represented as a deletion.
ALLOWED_DIFF_PATHS = frozenset(
    {
        "/selection_hook",
        "/selection_hook_sha256",
        "/transcript_sha256",
        "/source_fact_review",
        "/input_audits/0/text_sha256",
        "/input_audits/1/text_sha256",
        "/cover_output_audits",
    }
)
EXPECTED_OLD_CUE17 = "哦，是昨天的视频，搞忘了"
EXPECTED_NEW_CUE17 = "哦，是昨天的视频。へぇ、なるほどね。"
_RECEIPT_FIELDS = frozenset(
    {
        "schema_version", "status", "result_type", "candidate_id", "recording_date",
        "root_public_text_authority_sha256", "root_authority_consumption_sha256",
        "old_story_contract_sha256", "fresh_story_contract_sha256", "clip_context_sha256",
        "baseline_sha256", "decision_ledger_sha256", "truth_diff_sha256",
        "pipeline_srt_sha256", "old_transcript_sha256", "fresh_transcript_sha256",
        "fresh_artifact_hashes", "allowed_canonical_diff_paths", "cue17_transition",
        "subtitle_text_mutation_authorized", "speaker_label_mutation_authorized",
        "upload_authorized", "registry_hold_released",
    }
)


class C6ReplayPublicTextProjectionError(CandidatePublicTextSurfaceAuthorityError):
    """The C6-derived public-text consumption proof is unavailable."""


def _sha256_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_json(value: object) -> str:
    return "sha256:" + hashlib.sha256(
        json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _diff_paths(old: object, new: object, path: str = "") -> set[str]:
    if isinstance(old, Mapping) and isinstance(new, Mapping):
        result: set[str] = set()
        for key in sorted(set(old) | set(new), key=str):
            child = f"{path}/{key}" if path else f"/{key}"
            if key not in old or key not in new:
                result.add(child)
            else:
                result.update(_diff_paths(old[key], new[key], child))
        return result
    if isinstance(old, list) and isinstance(new, list):
        result = set()
        for index in range(max(len(old), len(new))):
            child = f"{path}/{index}"
            if index >= len(old) or index >= len(new):
                result.add(child)
            else:
                result.update(_diff_paths(old[index], new[index], child))
        return result
    return set() if old == new else {path or "/"}


def _read_sealed(path: Path, *, root: Path, expected_sha256: str, label: str) -> bytes:
    try:
        if path.is_symlink() or not path.is_file():
            raise C6ReplayPublicTextProjectionError(f"C6_{label}_AUTHORITY_MISSING")
        raw = path.read_bytes()
        if _sha256_bytes(raw) != expected_sha256:
            raise C6ReplayPublicTextProjectionError(f"C6_{label}_DIGEST_DRIFT")
        require_repository_asset_authority(
            repo_root=root, relative_path=path.relative_to(root), observed_bytes=raw
        )
        return raw
    except (OSError, ValueError, RepositoryAssetAuthorityError) as exc:
        if isinstance(exc, C6ReplayPublicTextProjectionError):
            raise
        raise C6ReplayPublicTextProjectionError(f"C6_{label}_AUTHORITY_INVALID") from exc


def _sealed_baseline(plan: object, *, root: Path) -> tuple[dict[str, object], Path, Path, Path, Path]:
    baseline = getattr(plan, "baseline", None)
    if baseline is None:
        raise C6ReplayPublicTextProjectionError("C6_BASELINE_AUTHORITY_MISSING")
    manifest_path = getattr(baseline, "manifest_path", None)
    baseline_path = getattr(baseline, "baseline_path", None)
    config = getattr(baseline, "config", None)
    if not isinstance(manifest_path, Path) or not isinstance(baseline_path, Path) or not isinstance(config, Mapping):
        raise C6ReplayPublicTextProjectionError("C6_BASELINE_AUTHORITY_INVALID")
    try:
        manifest_raw = _read_sealed(
            manifest_path, root=root,
            expected_sha256=_sha256_bytes(manifest_path.read_bytes()), label="BASELINE_MANIFEST",
        )
    except OSError as exc:
        raise C6ReplayPublicTextProjectionError("C6_BASELINE_AUTHORITY_MISSING") from exc
    # Re-read the canonical registry rather than trusting a plan's self-asserted
    # config.  The registry binds every lane file through the repository
    # authority manifest.
    if manifest_path != root / RELATIVE_BASELINE_MANIFEST:
        raise C6ReplayPublicTextProjectionError("C6_BASELINE_PATH_DRIFT")
    from src.autoslice.reviewed_subtitle_baseline_registry import (
        load_candidate_reviewed_subtitle_baseline,
    )
    try:
        canonical = load_candidate_reviewed_subtitle_baseline(
            root / RELATIVE_BASELINE_MANIFEST.parent,
            CANDIDATE_ID,
            repo_root=root,
        )
    except Exception as exc:
        raise C6ReplayPublicTextProjectionError("C6_BASELINE_AUTHORITY_INVALID") from exc
    if canonical is None or canonical.config != dict(config) or canonical.manifest_path != manifest_path or canonical.baseline_path != baseline_path:
        raise C6ReplayPublicTextProjectionError("C6_BASELINE_AUTHORITY_DRIFT")
    try:
        lanes = config["operator_truth_lanes"]
        ledger_decl = lanes["decision_ledger"]
        diff_decl = lanes["diff_receipt"]
        pipeline_decl = lanes["pipeline_diagnostic"]
        ledger_path = manifest_path.parent / str(ledger_decl["path"])
        diff_path = manifest_path.parent / str(diff_decl["path"])
        pipeline_path = manifest_path.parent / str(pipeline_decl["path"])
        expected_baseline = str(config["sha256"])
        expected_ledger = "sha256:" + str(ledger_decl["sha256"])
        expected_diff = "sha256:" + str(diff_decl["sha256"])
        expected_pipeline = "sha256:" + str(pipeline_decl["sha256"])
    except (KeyError, TypeError, ValueError) as exc:
        raise C6ReplayPublicTextProjectionError("C6_BASELINE_AUTHORITY_INVALID") from exc
    if not expected_baseline.startswith("sha256:"):
        expected_baseline = "sha256:" + expected_baseline
    if _sha256_bytes(baseline_path.read_bytes()) != expected_baseline:
        raise C6ReplayPublicTextProjectionError("C6_BASELINE_DIGEST_DRIFT")
    _read_sealed(baseline_path, root=root, expected_sha256=expected_baseline, label="BASELINE")
    _read_sealed(ledger_path, root=root, expected_sha256=expected_ledger, label="DECISION_LEDGER")
    _read_sealed(diff_path, root=root, expected_sha256=expected_diff, label="TRUTH_DIFF")
    _read_sealed(pipeline_path, root=root, expected_sha256=expected_pipeline, label="PIPELINE_DIAGNOSTIC")
    return dict(config), baseline_path, pipeline_path, ledger_path, diff_path


def _parse_srt_texts(raw: bytes) -> list[tuple[int, int, int, str]]:
    from src.autoslice.jingting_chunker import parse_srt_cues

    try:
        cues = parse_srt_cues(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise C6ReplayPublicTextProjectionError("C6_TRUTH_LANE_SRT_INVALID") from exc
    return [(int(cue.index), int(cue.start_ms), int(cue.end_ms), str(cue.text).strip()) for cue in cues]


def _validate_cue_projection(*, pipeline_path: Path, baseline_path: Path, config: Mapping[str, object]) -> tuple[str, str, str, str]:
    pipeline = _parse_srt_texts(pipeline_path.read_bytes())
    reviewed = _parse_srt_texts(baseline_path.read_bytes())
    if len(pipeline) != 22 or len(reviewed) != 22:
        raise C6ReplayPublicTextProjectionError("C6_CUE_GRID_INVALID")
    changed: list[int] = []
    for ordinal, (old, new) in enumerate(zip(pipeline, reviewed, strict=True), start=1):
        if old[:3] != new[:3] or old[0] != ordinal or new[0] != ordinal:
            raise C6ReplayPublicTextProjectionError("C6_CUE_ORDER_OR_TIMING_DRIFT")
        if old[3] != new[3]:
            changed.append(old[0])
    if changed != [17] or pipeline[16][3] != EXPECTED_OLD_CUE17 or reviewed[16][3] != EXPECTED_NEW_CUE17:
        raise C6ReplayPublicTextProjectionError("C6_CUE17_PROJECTION_INVALID")
    transcript_old = _sha256_text("\n".join(row[3] for row in pipeline))
    transcript_new = _sha256_text("\n".join(row[3] for row in reviewed))
    declared = str(config.get("sha256") or "")
    if not declared.startswith("sha256:"):
        declared = "sha256:" + declared
    if _sha256_bytes(baseline_path.read_bytes()) != declared:
        raise C6ReplayPublicTextProjectionError("C6_BASELINE_DIGEST_DRIFT")
    return transcript_old, transcript_new, _sha256_bytes(pipeline_path.read_bytes()), _sha256_bytes(baseline_path.read_bytes())


def _validate_story_diff(*, old_story: Mapping[str, object], fresh_story: Mapping[str, object], authority: object, old_transcript: str, new_transcript: str) -> set[str]:
    if fresh_story.get("candidate_id") != CANDIDATE_ID or fresh_story.get("selection_hook") != authority.resolved_selection_hook:
        raise C6ReplayPublicTextProjectionError("C6_FRESH_STORY_PUBLIC_SURFACE_DRIFT")
    if fresh_story.get("transcript_sha256") != new_transcript:
        raise C6ReplayPublicTextProjectionError("C6_FRESH_TRANSCRIPT_DIGEST_DRIFT")
    if old_story.get("transcript_sha256") != old_transcript:
        raise C6ReplayPublicTextProjectionError("C6_OLD_TRANSCRIPT_DIGEST_DRIFT")
    for key in ("source_media_sha256s", "clip_context_binding", "clip_context_prompt", "session_relation_authority", "participants", "selection_scorecard", "boundary_semantic_review", "cover_reference_authority", "human_boundary_authority", "relation_state", "required_entity_ids", "nancho_accepted_surfaces", "relation_claim_allowed", "cover_counterpart_reference_available", "cover_fallback_mode"):
        if old_story.get(key) != fresh_story.get(key):
            raise C6ReplayPublicTextProjectionError(f"C6_STORY_METADATA_DRIFT:{key}")
    paths = _diff_paths(old_story, fresh_story)
    if not paths.issubset(ALLOWED_DIFF_PATHS):
        raise C6ReplayPublicTextProjectionError("C6_STORY_CANONICAL_DIFF_NOT_ALLOWLISTED")
    required = {"/selection_hook", "/selection_hook_sha256", "/transcript_sha256", "/input_audits/0/text_sha256", "/input_audits/1/text_sha256"}
    if not required.issubset(paths):
        raise C6ReplayPublicTextProjectionError("C6_STORY_CANONICAL_DIFF_INCOMPLETE")
    if "/source_fact_review" not in paths and "source_fact_review" in old_story:
        raise C6ReplayPublicTextProjectionError("C6_STALE_SOURCE_FACT_REVIEW_SURVIVED")
    if fresh_story.get("selection_hook_sha256") != _sha256_text(str(fresh_story["selection_hook"])):
        raise C6ReplayPublicTextProjectionError("C6_FRESH_HOOK_DIGEST_DRIFT")
    audits = fresh_story.get("input_audits")
    if not isinstance(audits, list) or len(audits) != 2 or audits[0].get("text_sha256") != _sha256_text(str(fresh_story["selection_hook"])) or audits[1].get("text_sha256") != new_transcript:
        raise C6ReplayPublicTextProjectionError("C6_FRESH_INPUT_AUDIT_DRIFT")
    return paths


def _build_receipt(*, plan: object, authority: object, old_story: Mapping[str, object], fresh_story: Mapping[str, object], baseline_config: Mapping[str, object], baseline_path: Path, pipeline_path: Path, ledger_path: Path, diff_path: Path, root_authority_consumption: Mapping[str, object], diff_paths: set[str], transcript_old: str, transcript_new: str, pipeline_sha: str, baseline_sha: str) -> "C6ReplayPublicTextConsumption":
    return C6ReplayPublicTextConsumption(
        receipt={
            "schema_version": SCHEMA_VERSION,
            "status": "CONSUMED",
            "result_type": "DERIVED_REPLAY_CONSUMPTION",
            "candidate_id": CANDIDATE_ID,
            "recording_date": RECORDING_DATE,
            "root_public_text_authority_sha256": authority.authority_sha256,
            "root_authority_consumption_sha256": _sha256_json(root_authority_consumption),
            "old_story_contract_sha256": _sha256_json(dict(old_story)),
            "fresh_story_contract_sha256": _sha256_json(dict(fresh_story)),
            "clip_context_sha256": authority.clip_context_sha256,
            "baseline_sha256": baseline_sha,
            "decision_ledger_sha256": "sha256:" + str(baseline_config["operator_truth_lanes"]["decision_ledger"]["sha256"]),
            "truth_diff_sha256": "sha256:" + str(baseline_config["operator_truth_lanes"]["diff_receipt"]["sha256"]),
            "pipeline_srt_sha256": pipeline_sha,
            "old_transcript_sha256": transcript_old,
            "fresh_transcript_sha256": transcript_new,
            "fresh_artifact_hashes": {
                "story_contract_sha256": _sha256_json(dict(fresh_story)),
                "selection_hook_sha256": _sha256_text(str(fresh_story["selection_hook"])),
                "title_sha256": _sha256_text(authority.resolved_title),
                "cover_lines_sha256": _sha256_json(list(authority.resolved_cover_lines)),
                "reviewed_baseline_sha256": baseline_sha,
            },
            "allowed_canonical_diff_paths": sorted(diff_paths),
            "cue17_transition": {"cue": 17, "before": EXPECTED_OLD_CUE17, "after": EXPECTED_NEW_CUE17},
            "subtitle_text_mutation_authorized": False,
            "speaker_label_mutation_authorized": False,
            "upload_authorized": False,
            "registry_hold_released": False,
        }
    )


@dataclass(frozen=True, slots=True)
class C6ReplayPublicTextConsumption:
    """Immutable typed result carrying the derived receipt into staging."""

    receipt: dict[str, object]

    def as_dict(self) -> dict[str, object]:
        return copy.deepcopy(self.receipt)


def validate_c6_replay_public_text_consumption_for_fresh_story(
    receipt: object, *, fresh_story: Mapping[str, object], authority: object
) -> dict[str, object]:
    if not isinstance(receipt, Mapping) or set(receipt) != _RECEIPT_FIELDS or receipt.get("schema_version") != SCHEMA_VERSION or receipt.get("status") != "CONSUMED" or receipt.get("result_type") != "DERIVED_REPLAY_CONSUMPTION":
        raise C6ReplayPublicTextProjectionError("C6_DERIVED_RECEIPT_INVALID")
    if receipt.get("candidate_id") != CANDIDATE_ID or receipt.get("recording_date") != RECORDING_DATE or receipt.get("root_public_text_authority_sha256") != authority.authority_sha256 or receipt.get("clip_context_sha256") != authority.clip_context_sha256:
        raise C6ReplayPublicTextProjectionError("C6_DERIVED_RECEIPT_BINDING_DRIFT")
    if receipt.get("cue17_transition") != {"cue": 17, "before": EXPECTED_OLD_CUE17, "after": EXPECTED_NEW_CUE17}:
        raise C6ReplayPublicTextProjectionError("C6_DERIVED_RECEIPT_CUE17_DRIFT")
    normalized_story = dict(fresh_story)
    # The source-fact provider is allowed to append its fresh review after this
    # pre-provider receipt is consumed.  That review is not part of the
    # public-text projection identity and is validated by its own gate.
    normalized_story.pop("source_fact_review", None)
    if receipt.get("fresh_story_contract_sha256") != _sha256_json(normalized_story):
        raise C6ReplayPublicTextProjectionError("C6_DERIVED_RECEIPT_STORY_DRIFT")
    artifacts = receipt.get("fresh_artifact_hashes")
    if not isinstance(artifacts, Mapping) or artifacts.get("story_contract_sha256") != receipt.get("fresh_story_contract_sha256") or artifacts.get("selection_hook_sha256") != _sha256_text(str(fresh_story.get("selection_hook") or "")) or artifacts.get("title_sha256") != _sha256_text(authority.resolved_title) or artifacts.get("cover_lines_sha256") != _sha256_json(list(authority.resolved_cover_lines)):
        raise C6ReplayPublicTextProjectionError("C6_DERIVED_RECEIPT_ARTIFACT_DRIFT")
    expected_paths = receipt.get("allowed_canonical_diff_paths")
    if expected_paths != sorted(ALLOWED_DIFF_PATHS):
        raise C6ReplayPublicTextProjectionError("C6_DERIVED_RECEIPT_DIFF_DRIFT")
    for key in ("subtitle_text_mutation_authorized", "speaker_label_mutation_authorized", "upload_authorized", "registry_hold_released"):
        if receipt.get(key) is not False:
            raise C6ReplayPublicTextProjectionError("C6_DERIVED_RECEIPT_SCOPE_DRIFT")
    if fresh_story.get("selection_hook") != authority.resolved_selection_hook:
        raise C6ReplayPublicTextProjectionError("C6_DERIVED_RECEIPT_PUBLIC_SURFACE_DRIFT")
    return dict(receipt)


def validate_c6_replay_public_text_consumption(
    receipt: object, *, old_story: Mapping[str, object], fresh_story: Mapping[str, object], authority: object
) -> dict[str, object]:
    if not isinstance(receipt, Mapping) or set(receipt) != _RECEIPT_FIELDS or receipt.get("schema_version") != SCHEMA_VERSION or receipt.get("status") != "CONSUMED" or receipt.get("result_type") != "DERIVED_REPLAY_CONSUMPTION":
        raise C6ReplayPublicTextProjectionError("C6_DERIVED_RECEIPT_INVALID")
    if receipt.get("candidate_id") != CANDIDATE_ID or receipt.get("recording_date") != RECORDING_DATE or receipt.get("root_public_text_authority_sha256") != authority.authority_sha256:
        raise C6ReplayPublicTextProjectionError("C6_DERIVED_RECEIPT_BINDING_DRIFT")
    normalized_fresh = dict(fresh_story)
    normalized_fresh.pop("source_fact_review", None)
    if receipt.get("old_story_contract_sha256") != _sha256_json(dict(old_story)) or receipt.get("fresh_story_contract_sha256") != _sha256_json(normalized_fresh):
        raise C6ReplayPublicTextProjectionError("C6_DERIVED_RECEIPT_STORY_DRIFT")
    from src.autoslice.candidate_public_text_surface_authority import consume_candidate_public_text_surface_authority
    root_consumption = consume_candidate_public_text_surface_authority(
        authority, candidate_id=CANDIDATE_ID,
        selection_hook=authority.input_selection_hook, story_contract=old_story,
    )
    if receipt.get("root_authority_consumption_sha256") != _sha256_json(root_consumption):
        raise C6ReplayPublicTextProjectionError("C6_DERIVED_RECEIPT_AUTHORITY_CONSUMPTION_DRIFT")
    expected_paths = receipt.get("allowed_canonical_diff_paths")
    if expected_paths != sorted(ALLOWED_DIFF_PATHS if "/cover_output_audits" in old_story else ALLOWED_DIFF_PATHS - {"/cover_output_audits"}):
        raise C6ReplayPublicTextProjectionError("C6_DERIVED_RECEIPT_DIFF_DRIFT")
    for key in ("subtitle_text_mutation_authorized", "speaker_label_mutation_authorized", "upload_authorized", "registry_hold_released"):
        if receipt.get(key) is not False:
            raise C6ReplayPublicTextProjectionError("C6_DERIVED_RECEIPT_SCOPE_DRIFT")
    return dict(receipt)


def build_c6_replay_public_text_resolver(*, plan: object, root: Path) -> Callable[..., object]:
    """Return the C6 hook, or fail closed when C6 authority is incomplete."""

    if getattr(plan, "candidate_id", None) != CANDIDATE_ID or getattr(plan, "date", None) != RECORDING_DATE:
        raise C6ReplayPublicTextProjectionError("C6_REPLAY_PLAN_IDENTITY_INVALID")
    baseline_config, baseline_path, pipeline_path, ledger_path, diff_path = _sealed_baseline(plan, root=root)
    from src.autoslice.candidate_public_text_surface_authority import (
        CandidatePublicTextSurfaceAuthorityError,
        load_candidate_public_text_surface_authority,
        consume_candidate_public_text_surface_authority,
    )

    try:
        authority = load_candidate_public_text_surface_authority(CANDIDATE_ID, root=root)
    except CandidatePublicTextSurfaceAuthorityError as exc:
        raise C6ReplayPublicTextProjectionError("C6_ROOT_PUBLIC_TEXT_AUTHORITY_INVALID") from exc
    if authority is None or not authority.is_root_reviewed_resolution:
        raise C6ReplayPublicTextProjectionError("C6_ROOT_PUBLIC_TEXT_AUTHORITY_MISSING")
    transcript_old, transcript_new, pipeline_sha, baseline_sha = _validate_cue_projection(
        pipeline_path=pipeline_path, baseline_path=baseline_path, config=baseline_config
    )

    def resolve(*, authority: object, candidate_id: str, selection_hook: str, story_contract: object, story_contract_rebuilder: Callable[[str], dict[str, object]] | None, **_unused: object) -> object:
        if candidate_id != CANDIDATE_ID or not isinstance(story_contract, Mapping) or selection_hook != authority.input_selection_hook or story_contract.get("selection_hook") != authority.input_selection_hook:
            raise C6ReplayPublicTextProjectionError("C6_OLD_PUBLIC_TEXT_BINDING_INVALID")
        try:
            root_consumption = consume_candidate_public_text_surface_authority(
                authority, candidate_id=CANDIDATE_ID, selection_hook=selection_hook, story_contract=story_contract
            )
        except CandidatePublicTextSurfaceAuthorityError as exc:
            raise C6ReplayPublicTextProjectionError("C6_OLD_PUBLIC_TEXT_AUTHORITY_INVALID") from exc
        if story_contract_rebuilder is None:
            raise C6ReplayPublicTextProjectionError("C6_STORY_CONTRACT_REBUILDER_REQUIRED")
        try:
            fresh = story_contract_rebuilder(authority.resolved_selection_hook)
        except Exception as exc:
            raise C6ReplayPublicTextProjectionError("C6_FRESH_STORY_REBUILD_FAILED") from exc
        if not isinstance(fresh, Mapping):
            raise C6ReplayPublicTextProjectionError("C6_FRESH_STORY_CONTRACT_INVALID")
        fresh_dict = dict(fresh)
        paths = _validate_story_diff(old_story=story_contract, fresh_story=fresh_dict, authority=authority, old_transcript=transcript_old, new_transcript=transcript_new)
        consumption = _build_receipt(plan=plan, authority=authority, old_story=story_contract, fresh_story=fresh_dict, baseline_config=baseline_config, baseline_path=baseline_path, pipeline_path=pipeline_path, ledger_path=ledger_path, diff_path=diff_path, root_authority_consumption=root_consumption, diff_paths=paths, transcript_old=transcript_old, transcript_new=transcript_new, pipeline_sha=pipeline_sha, baseline_sha=baseline_sha)
        return _staging_resolution(authority, fresh_dict, consumption.as_dict())

    return resolve


def _staging_resolution(authority: object, story: dict[str, object], receipt: dict[str, object]) -> object:
    from src.autoslice.candidate_public_text_surface_authority import CandidatePublicTextStagingResolution

    return CandidatePublicTextStagingResolution(
        selection_hook=authority.resolved_selection_hook,
        title=authority.resolved_title,
        story_contract=story,
        consumption=receipt,
        title_source=authority.title_source,
    )


# Keep a named alias useful to tests and to the replay adapter.
resolve_c6_replay_public_text_staging = build_c6_replay_public_text_resolver
