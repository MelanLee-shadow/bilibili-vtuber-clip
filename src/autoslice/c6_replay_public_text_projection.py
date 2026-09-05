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
# These are production pins from the clean 981 projection.  This module is
# deliberately candidate-specific; keeping the pins here prevents a package
# stage from accepting a receipt that merely has a valid shape and seal.
EXPECTED_PERSISTED_STORY_SHA256 = "sha256:94b52484061524ce7d3865d9a8939b84773fcb87015b14287aa4e9f6794dcfd6"
EXPECTED_FRESH_STORY_SHA256 = "sha256:e002cc5732ffb261274d255754c3ed6e6e17c95982ff4d9d86cd40c54a5c3a22"
# The package finalizer appends these independently audited downstream
# surfaces after the replay receipt is sealed.  The receipt keeps binding the
# complete pre-downstream StoryContract above; this digest binds the exact
# candidate-specific runtime projection used by package-time consumers.
EXPECTED_C6_RUNTIME_FRESH_STORY_PROJECTION_SHA256 = "sha256:b3397c38bddaa4fa4915eb596e6a6ec2d60905d3d539aa928c116833e2a4c612"
EXPECTED_AUTHORITY_SHA256 = "sha256:7395daf13d25062e9de5ed7de3b292ecdb085c7c729876735bb4aa5399e50d45"
EXPECTED_BASELINE_SHA256 = "sha256:3cfcf2954c1d32a078e99aaff5585213b5edbbedd6db8f87d62097d06775ea77"
EXPECTED_DECISION_LEDGER_SHA256 = "sha256:ecef045ba999ca94f3afa58a57562ce728fd004e9dc0d03cec949e0f27f737f3"
EXPECTED_TRUTH_DIFF_SHA256 = "sha256:ee662f130cb91537db8dd471cddc777318e30179ed7b2c6e8fe7f203a3f23522"
EXPECTED_PIPELINE_SRT_SHA256 = "sha256:a3ac0efb39d5348f8e8e756712de9a18a6aa87123b0ccbd8fb9bd68c54afb8b1"
EXPECTED_OLD_TRANSCRIPT_SHA256 = "sha256:0933dd7c516c5dbc2a7d0dbe3c3aeefea059055c120d697dc8697914a936806e"
EXPECTED_FRESH_TRANSCRIPT_SHA256 = "sha256:c00924ce0e63e9ee25c87d77256eaf7c24ad29588c0c22188d958eba4827a0c2"
EXPECTED_CLIP_CONTEXT_SHA256 = "sha256:edf26d3d7cf0eeea0e94c8d3a0006b7a323441ed11bdb701818bb65da619100a"
EXPECTED_CLIP_CONTEXT_PROMPT_SHA256 = "sha256:ff0a633cdd9eb853d25278b24d71e939cfe35fac00d94c51617295164f07c4d8"
EXPECTED_INPUT_HOOK_SHA256 = "sha256:4abced4f63049a3dc91ffc5d703c1ab99c17894d26cd8bc1abddca60d28636fd"
EXPECTED_RESOLVED_HOOK_SHA256 = "sha256:2565d16fa8b4063fb1b92e9f143a4a3221c1b8f64cb69a72592186c48939ff24"
EXPECTED_TITLE_SHA256 = "sha256:420247526abe5962ad12024fbbf0a503c55bc4bedf3524eaa6e7422bdbb88885"
EXPECTED_COVER_LINES_SHA256 = "sha256:fa948444200ed7399e5305f61ea8255a0c2dab06a3ed78e2395a481b09d95fd9"
_RECEIPT_FIELDS = frozenset(
    {
        "schema_version", "status", "result_type", "candidate_id", "recording_date",
        "root_public_text_authority_sha256", "root_authority_consumption_sha256",
        "old_story_contract_sha256", "fresh_story_contract_sha256", "clip_context_sha256",
        "baseline_sha256", "decision_ledger_sha256", "truth_diff_sha256",
        "pipeline_srt_sha256", "old_transcript_sha256", "fresh_transcript_sha256",
        "fresh_artifact_hashes", "allowed_canonical_diff_paths", "cue17_transition",
        "subtitle_text_mutation_authorized", "speaker_label_mutation_authorized",
        "upload_authorized", "registry_hold_released", "receipt_sha256",
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


def _receipt_seal(receipt: Mapping[str, object]) -> str:
    unsigned = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    return _sha256_json(unsigned)


def _normalized_fresh_story(story: Mapping[str, object]) -> dict[str, object]:
    normalized = dict(story)
    normalized.pop("source_fact_review", None)
    return normalized


def _c6_runtime_fresh_story_projection(
    story: Mapping[str, object],
) -> dict[str, object]:
    projected = dict(story)
    for field in (
        "source_fact_review",
        "cover_output_audits",
        "boundary_semantic_review",
    ):
        projected.pop(field, None)
    return projected


def _preserve_persisted_story_boundary(
    *, old_story: Mapping[str, object], fresh_story: Mapping[str, object]
) -> dict[str, object]:
    boundary = old_story.get("boundary_semantic_review")
    if (
        not isinstance(boundary, Mapping)
        or boundary.get("candidate_id") != CANDIDATE_ID
        or boundary.get("review_scope") != "final_delivery"
    ):
        raise C6ReplayPublicTextProjectionError(
            "C6_OLD_STORY_BOUNDARY_REVIEW_INVALID"
        )
    normalized = dict(fresh_story)
    normalized["boundary_semantic_review"] = copy.deepcopy(dict(boundary))
    return normalized


def _root_consumption(authority: object) -> dict[str, object]:
    """Rebuild the root reviewed consumption hash without trusting old story bytes."""

    return {
        "schema_version": "candidate-public-text-surface-consumption.v1",
        "status": "CONSUMED",
        "candidate_id": CANDIDATE_ID,
        "authority_sha256": EXPECTED_AUTHORITY_SHA256,
        "algorithm_id": "exact-candidate-root-reviewed-public-surface-resolution.v1",
        "clip_context_sha256": EXPECTED_CLIP_CONTEXT_SHA256,
        "clip_context_prompt_sha256": EXPECTED_CLIP_CONTEXT_PROMPT_SHA256,
        "selected_interval": {"absolute_start_ms": 753000, "absolute_end_ms": 816000},
        "input_selection_hook_sha256": EXPECTED_INPUT_HOOK_SHA256,
        "observed_selection_hook_sha256": EXPECTED_INPUT_HOOK_SHA256,
        "resolved_selection_hook": authority.resolved_selection_hook,
        "resolved_title": authority.resolved_title,
        "resolved_cover_lines": list(authority.resolved_cover_lines),
        "subtitle_text_mutation_authorized": False,
        "speaker_label_mutation_authorized": False,
        "upload_authorized": False,
        "registry_hold_released": False,
        "user_authorization": dict(authority.user_authorization),
        "decision_authorization": dict(authority.decision_authorization or {}),
    }


def _validate_receipt_static(
    receipt: object,
    *,
    fresh_story: Mapping[str, object],
    authority: object,
    old_story: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Validate the complete C6 receipt identity shared by both consumers."""

    if not isinstance(receipt, Mapping) or set(receipt) != _RECEIPT_FIELDS:
        raise C6ReplayPublicTextProjectionError("C6_DERIVED_RECEIPT_INVALID")
    if (
        receipt.get("schema_version") != SCHEMA_VERSION
        or receipt.get("status") != "CONSUMED"
        or receipt.get("result_type") != "DERIVED_REPLAY_CONSUMPTION"
    ):
        raise C6ReplayPublicTextProjectionError("C6_DERIVED_RECEIPT_INVALID")
    if receipt.get("receipt_sha256") != _receipt_seal(receipt):
        raise C6ReplayPublicTextProjectionError("C6_DERIVED_RECEIPT_SELF_SEAL_INVALID")
    expected_bindings = {
        "candidate_id": CANDIDATE_ID,
        "recording_date": RECORDING_DATE,
        "root_public_text_authority_sha256": EXPECTED_AUTHORITY_SHA256,
        "clip_context_sha256": EXPECTED_CLIP_CONTEXT_SHA256,
        "old_story_contract_sha256": EXPECTED_PERSISTED_STORY_SHA256,
        "fresh_story_contract_sha256": EXPECTED_FRESH_STORY_SHA256,
        "baseline_sha256": EXPECTED_BASELINE_SHA256,
        "decision_ledger_sha256": EXPECTED_DECISION_LEDGER_SHA256,
        "truth_diff_sha256": EXPECTED_TRUTH_DIFF_SHA256,
        "pipeline_srt_sha256": EXPECTED_PIPELINE_SRT_SHA256,
        "old_transcript_sha256": EXPECTED_OLD_TRANSCRIPT_SHA256,
        "fresh_transcript_sha256": EXPECTED_FRESH_TRANSCRIPT_SHA256,
    }
    if any(receipt.get(key) != value for key, value in expected_bindings.items()):
        raise C6ReplayPublicTextProjectionError("C6_DERIVED_RECEIPT_BINDING_DRIFT")
    if (
        getattr(authority, "authority_sha256", None) != EXPECTED_AUTHORITY_SHA256
        or getattr(authority, "clip_context_sha256", None) != EXPECTED_CLIP_CONTEXT_SHA256
        or getattr(authority, "resolved_selection_hook", None) is None
        or _sha256_text(str(authority.resolved_selection_hook)) != EXPECTED_RESOLVED_HOOK_SHA256
        or _sha256_text(str(getattr(authority, "resolved_title", ""))) != EXPECTED_TITLE_SHA256
        or _sha256_json(list(getattr(authority, "resolved_cover_lines", ()))) != EXPECTED_COVER_LINES_SHA256
    ):
        raise C6ReplayPublicTextProjectionError("C6_DERIVED_RECEIPT_AUTHORITY_DRIFT")
    runtime_projection = _c6_runtime_fresh_story_projection(fresh_story)
    if (
        _sha256_json(runtime_projection) != EXPECTED_C6_RUNTIME_FRESH_STORY_PROJECTION_SHA256
        or fresh_story.get("candidate_id") != CANDIDATE_ID
        or fresh_story.get("selection_hook") != getattr(authority, "resolved_selection_hook", None)
    ):
        raise C6ReplayPublicTextProjectionError("C6_DERIVED_RECEIPT_STORY_DRIFT")
    artifacts = receipt.get("fresh_artifact_hashes")
    if not isinstance(artifacts, Mapping) or set(artifacts) != {
        "story_contract_sha256", "selection_hook_sha256", "title_sha256",
        "cover_lines_sha256", "reviewed_baseline_sha256",
    } or artifacts != {
        "story_contract_sha256": EXPECTED_FRESH_STORY_SHA256,
        "selection_hook_sha256": EXPECTED_RESOLVED_HOOK_SHA256,
        "title_sha256": EXPECTED_TITLE_SHA256,
        "cover_lines_sha256": EXPECTED_COVER_LINES_SHA256,
        "reviewed_baseline_sha256": EXPECTED_BASELINE_SHA256,
    }:
        raise C6ReplayPublicTextProjectionError("C6_DERIVED_RECEIPT_ARTIFACT_DRIFT")
    if receipt.get("allowed_canonical_diff_paths") != sorted(ALLOWED_DIFF_PATHS):
        raise C6ReplayPublicTextProjectionError("C6_DERIVED_RECEIPT_DIFF_DRIFT")
    if receipt.get("cue17_transition") != {
        "cue": 17, "before": EXPECTED_OLD_CUE17, "after": EXPECTED_NEW_CUE17,
    }:
        raise C6ReplayPublicTextProjectionError("C6_DERIVED_RECEIPT_CUE17_DRIFT")
    for key in (
        "subtitle_text_mutation_authorized", "speaker_label_mutation_authorized",
        "upload_authorized", "registry_hold_released",
    ):
        if receipt.get(key) is not False:
            raise C6ReplayPublicTextProjectionError("C6_DERIVED_RECEIPT_SCOPE_DRIFT")
    if old_story is not None:
        if _sha256_json(dict(old_story)) != EXPECTED_PERSISTED_STORY_SHA256:
            raise C6ReplayPublicTextProjectionError("C6_DERIVED_RECEIPT_OLD_STORY_DRIFT")
        from src.autoslice.candidate_public_text_surface_authority import (
            consume_candidate_public_text_surface_authority,
        )
        try:
            root_consumption = consume_candidate_public_text_surface_authority(
                authority, candidate_id=CANDIDATE_ID,
                selection_hook=authority.input_selection_hook, story_contract=old_story,
            )
        except CandidatePublicTextSurfaceAuthorityError as exc:
            raise C6ReplayPublicTextProjectionError(
                "C6_DERIVED_RECEIPT_AUTHORITY_CONSUMPTION_INVALID"
            ) from exc
        if receipt.get("root_authority_consumption_sha256") != _sha256_json(root_consumption):
            raise C6ReplayPublicTextProjectionError("C6_DERIVED_RECEIPT_AUTHORITY_CONSUMPTION_DRIFT")
    else:
        if receipt.get("root_authority_consumption_sha256") != _sha256_json(_root_consumption(authority)):
            raise C6ReplayPublicTextProjectionError("C6_DERIVED_RECEIPT_AUTHORITY_CONSUMPTION_DRIFT")
    return dict(receipt)


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
        _read_sealed(
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
        ReviewedSubtitleBaselineRegistryError,
        load_candidate_reviewed_subtitle_baseline,
    )
    try:
        canonical = load_candidate_reviewed_subtitle_baseline(
            root / RELATIVE_BASELINE_MANIFEST.parent,
            CANDIDATE_ID,
            repo_root=root,
        )
    except (OSError, KeyError, TypeError, ValueError, ReviewedSubtitleBaselineRegistryError) as exc:
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
    if (
        expected_baseline != EXPECTED_BASELINE_SHA256
        or expected_ledger != EXPECTED_DECISION_LEDGER_SHA256
        or expected_diff != EXPECTED_TRUTH_DIFF_SHA256
        or expected_pipeline != EXPECTED_PIPELINE_SRT_SHA256
    ):
        raise C6ReplayPublicTextProjectionError("C6_BASELINE_AUTHORITY_DRIFT")
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
    pipeline_sha = _sha256_bytes(pipeline_path.read_bytes())
    baseline_sha = _sha256_bytes(baseline_path.read_bytes())
    if baseline_sha != declared or baseline_sha != EXPECTED_BASELINE_SHA256:
        raise C6ReplayPublicTextProjectionError("C6_BASELINE_DIGEST_DRIFT")
    if pipeline_sha != EXPECTED_PIPELINE_SRT_SHA256:
        raise C6ReplayPublicTextProjectionError("C6_PIPELINE_DIAGNOSTIC_DIGEST_DRIFT")
    if transcript_old != EXPECTED_OLD_TRANSCRIPT_SHA256 or transcript_new != EXPECTED_FRESH_TRANSCRIPT_SHA256:
        raise C6ReplayPublicTextProjectionError("C6_TRANSCRIPT_DIGEST_DRIFT")
    return transcript_old, transcript_new, pipeline_sha, baseline_sha


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
    receipt: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "status": "CONSUMED",
        "result_type": "DERIVED_REPLAY_CONSUMPTION",
        "candidate_id": CANDIDATE_ID,
        "recording_date": RECORDING_DATE,
        "root_public_text_authority_sha256": authority.authority_sha256,
        "root_authority_consumption_sha256": _sha256_json(root_authority_consumption),
        "old_story_contract_sha256": _sha256_json(dict(old_story)),
        "fresh_story_contract_sha256": _sha256_json(_normalized_fresh_story(fresh_story)),
        "clip_context_sha256": authority.clip_context_sha256,
        "baseline_sha256": baseline_sha,
        "decision_ledger_sha256": "sha256:" + str(baseline_config["operator_truth_lanes"]["decision_ledger"]["sha256"]),
        "truth_diff_sha256": "sha256:" + str(baseline_config["operator_truth_lanes"]["diff_receipt"]["sha256"]),
        "pipeline_srt_sha256": pipeline_sha,
        "old_transcript_sha256": transcript_old,
        "fresh_transcript_sha256": transcript_new,
        "fresh_artifact_hashes": {
            "story_contract_sha256": _sha256_json(_normalized_fresh_story(fresh_story)),
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
    receipt["receipt_sha256"] = _receipt_seal(receipt)
    return C6ReplayPublicTextConsumption(receipt=receipt)


@dataclass(frozen=True, slots=True)
class C6ReplayPublicTextConsumption:
    """Immutable typed result carrying the derived receipt into staging."""

    receipt: dict[str, object]

    def as_dict(self) -> dict[str, object]:
        return copy.deepcopy(self.receipt)


def validate_c6_replay_public_text_consumption_for_fresh_story(
    receipt: object, *, fresh_story: Mapping[str, object], authority: object
) -> dict[str, object]:
    # The source-fact provider may append its review after this pre-provider
    # gate.  All projection-bound fields, including the old-side evidence
    # digests, remain statically pinned and self-sealed here.
    return _validate_receipt_static(
        receipt, fresh_story=fresh_story, authority=authority,
    )


def validate_c6_replay_public_text_consumption(
    receipt: object, *, old_story: Mapping[str, object], fresh_story: Mapping[str, object], authority: object
) -> dict[str, object]:
    return _validate_receipt_static(
        receipt, old_story=old_story, fresh_story=fresh_story, authority=authority,
    )


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
        fresh_dict = _preserve_persisted_story_boundary(
            old_story=story_contract, fresh_story=fresh
        )
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
