"""Provider-free exact-final closure for the clean C6 replay.

This is a deliberately small, candidate/date-specific adapter.  It is built
only after ``stage_replay`` and consumes the stage manifest, the clean plan,
the committed C6 acceptance, and the old record-bound authority.  It never
calls a provider and never supplies a fallback reviewer.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Callable

CANDIDATE_ID = "auto_120032_753_816"
RECORDING_DATE = "2026-08-14"
REVIEWED_SRT_SHA256 = "sha256:3cfcf2954c1d32a078e99aaff5585213b5edbbedd6db8f87d62097d06775ea77"
PIPELINE_SRT_SHA256 = "sha256:a3ac0efb39d5348f8e8e756712de9a18a6aa87123b0ccbd8fb9bd68c54afb8b1"
DECISION_LEDGER_SHA256 = "sha256:ecef045ba999ca94f3afa58a57562ce728fd004e9dc0d03cec949e0f27f737f3"
TRUTH_DIFF_SHA256 = "sha256:ee662f130cb91537db8dd471cddc777318e30179ed7b2c6e8fe7f203a3f23522"
RECORD_SHA256 = "sha256:04b64d9a1e19810948fa0f6cca90d132a4464cc518cdbb26ace9ed0f0876222f"
# These are intentionally different roles.  ``ReplayPlan.expected_video_sha256``
# is read from the old record's ``artifact_hashes.video_sha256`` and therefore
# identifies the unburned recut used to reproduce the old record.  The C6
# authority pins the already-frozen burned delivery; the private stage retains
# the replay-plan pin until downstream finalization produces that delivery.
REPLAY_PLAN_EXPECTED_VIDEO_SHA256 = "sha256:ba1eeec5f8904cbef91594ed3dc28e7bec9766d719e5e0e1c4ef34d429e71736"
FROZEN_DELIVERY_VIDEO_SHA256 = "sha256:982e0fea91ef222302ec5cb30a965d0222164a87f4f3a3bc53e445048919de35"
CHAT_SHA256 = "sha256:fb7c0bb21ef6e775c22ebc7c75c83d09e6500d8623d80d35c9b2cb27ef0cd3a9"
BOUNDARY_AUDIT_SHA256 = "sha256:5231b6319b2bfa827ce3b404da7fcf3a7c33ea226314a0cfefde8195b1c5aec1"
AUTHORITY_SELF_HASH = "sha256:7d38e745cc75330dbc324a395ef3192693c672709451a18c2c6ff3cfd3f45eea"
STAGE_JSON_SHA256 = "sha256:b23b2a7997a00c0f1a6ace98b797cf37091914e47435d7284e0aaf31de412116"
# This is the source's sealed logical identity.  It is deliberately not
# substituted for a byte hash of a separately located padded file.
SOURCE_MEDIA_IDENTITY = "sha256:0f0770a9426c48fee5457cf9639a77e5a477f41ec806f4e4789e19c336e1aae5"

# The plan callback is the source/padded-local record interval.  The reviewed
# baseline itself is DELIVERY_LOCAL [0, 62860] inside that interval.
SOURCE_LOCAL_START_MS = 9_770
SOURCE_LOCAL_END_MS = 72_630
DELIVERY_LOCAL_START_MS = 0
DELIVERY_LOCAL_END_MS = 62_860
ABSOLUTE_SOURCE_START_MS = 753_540
ABSOLUTE_SOURCE_END_MS = 816_400
EXPECTED_CUE_COUNT = 22
OLD_CUE17 = "哦，是昨天的视频，搞忘了"
NEW_CUE17 = "哦，是昨天的视频。へぇ、なるほどね。"


class C6ExactFinalBoundaryAdapterError(RuntimeError):
    """The provider-free C6 closure cannot be constructed safely."""


def _sha_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _canonical(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _fail(code: str) -> None:
    raise C6ExactFinalBoundaryAdapterError(code)


def _binding(path: Path, expected: str, *, label: str) -> Any:
    from src.autoslice.reviewed_baseline_replay import ReviewedBaselineReplayError, regular_binding

    try:
        binding = regular_binding(path, label=label)
    except ReviewedBaselineReplayError as exc:
        raise C6ExactFinalBoundaryAdapterError(f"C6_EXACT_{label}_UNAVAILABLE") from exc
    if binding.sha256 != expected:
        _fail(f"C6_EXACT_{label}_HASH_MISMATCH")
    return binding


def _load_json(path: Path, expected: str, *, label: str) -> dict[str, object]:
    _binding(path, expected, label=label)
    try:
        value = json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise C6ExactFinalBoundaryAdapterError(f"C6_EXACT_{label}_INVALID") from exc
    if not isinstance(value, dict):
        _fail(f"C6_EXACT_{label}_INVALID")
    return value


def _rows(raw: bytes, *, label: str) -> list[tuple[int, int, int, str]]:
    from src.autoslice.jingting_chunker import parse_srt_cues

    try:
        cues = parse_srt_cues(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise C6ExactFinalBoundaryAdapterError(f"C6_EXACT_{label}_INVALID") from exc
    result = [(int(cue.index), int(cue.start_ms), int(cue.end_ms), str(cue.text)) for cue in cues]
    if len(result) != EXPECTED_CUE_COUNT:
        _fail(f"C6_EXACT_{label}_CUE_COUNT_MISMATCH")
    return result


def _validate_authority() -> dict[str, object]:
    from src.autoslice.c6_exhaustive_boundary_authority import load_accepted_authority

    authority = load_accepted_authority()
    if not isinstance(authority, dict) or authority.get("canonical_self_hash") != AUTHORITY_SELF_HASH:
        _fail("C6_EXACT_ACCEPTED_AUTHORITY_INVALID")
    binding = authority.get("frozen_delivery_binding")
    if not isinstance(binding, Mapping) or binding.get("candidate_media_sha256") != FROZEN_DELIVERY_VIDEO_SHA256:
        _fail("C6_EXACT_ACCEPTED_AUTHORITY_BINDING_INVALID")
    if (
        authority.get("candidate_id") != CANDIDATE_ID
        or authority.get("recording_date") != RECORDING_DATE
        or authority.get("accepted") is not True
        or authority.get("boundary_change") is not False
        or authority.get("provider_authorized") is not False
        or authority.get("upload_authorized") is not False
    ):
        _fail("C6_EXACT_ACCEPTED_AUTHORITY_SCOPE_INVALID")
    interval = binding.get("source_interval")
    if interval != {"absolute_start_ms": 753000, "absolute_end_ms": 816000}:
        _fail("C6_EXACT_ACCEPTED_AUTHORITY_INTERVAL_INVALID")
    return authority


def _validate_plan_identity(plan: object) -> None:
    """Validate the replay input without conflating it with delivery bytes."""

    if (
        getattr(plan, "candidate_id", None) != CANDIDATE_ID
        or getattr(plan, "date", None) != RECORDING_DATE
        or getattr(plan, "local_start_ms", None) != SOURCE_LOCAL_START_MS
        or getattr(plan, "local_end_ms", None) != SOURCE_LOCAL_END_MS
        or getattr(plan, "expected_video_sha256", None) != REPLAY_PLAN_EXPECTED_VIDEO_SHA256
    ):
        _fail("C6_EXACT_PLAN_COORDINATE_OR_IDENTITY_DRIFT")


def _validate_plan_paths(plan: object) -> None:
    """Retain the build-plan path topology as an identity invariant."""

    package_root = getattr(plan, "package_root", None)
    record_path = getattr(plan, "record_path", None)
    padded_path = getattr(plan, "padded_path", None)
    expected_record = (
        Path(package_root) / "replacement_recuts" / f"{CANDIDATE_ID}.record.json"
        if isinstance(package_root, Path) else None
    )
    if (
        not isinstance(package_root, Path)
        or not isinstance(record_path, Path)
        or not isinstance(padded_path, Path)
        or record_path != expected_record
        or padded_path.parent != package_root
    ):
        _fail("C6_EXACT_PLAN_PATH_OR_IDENTITY_DRIFT")


def _validate_stage(plan: object, stage: Path) -> tuple[dict[str, object], bytes]:
    """Validate the frozen delivery manifest independently of recut identity.

    The private stage's ``expected_video_sha256``/``artifacts.video`` fields
    are the unburned recut produced by ``stage_replay``.  They must be compared
    with the replay plan identity; the separate frozen delivery role is checked
    by the accepted C6 authority and downstream package gates.
    """

    _validate_plan_identity(plan)
    _validate_plan_paths(plan)
    stage = Path(stage)
    document = _load_json(stage / "stage.json", STAGE_JSON_SHA256, label="STAGE_MANIFEST")
    unsigned = dict(document)
    declared = unsigned.pop("stage_sha256", None)
    if not isinstance(declared, str) or declared != _sha_bytes(_canonical(unsigned)):
        _fail("C6_EXACT_STAGE_SELF_SEAL_INVALID")
    if (
        document.get("schema_version") != "reviewed-baseline-replay-stage.v1"
        or document.get("candidate_id") != CANDIDATE_ID
        or document.get("date") != RECORDING_DATE
        or document.get("record_sha256") != RECORD_SHA256
        or document.get("expected_video_sha256") != REPLAY_PLAN_EXPECTED_VIDEO_SHA256
        or document.get("baseline_sha256") != REVIEWED_SRT_SHA256
        or document.get("upload_allowed") is not False
    ):
        _fail("C6_EXACT_STAGE_BINDING_DRIFT")
    artifacts = document.get("artifacts")
    if not isinstance(artifacts, Mapping) or artifacts.get("video") != REPLAY_PLAN_EXPECTED_VIDEO_SHA256 or artifacts.get("subtitle") != REVIEWED_SRT_SHA256:
        _fail("C6_EXACT_STAGE_ARTIFACT_DRIFT")
    reviewed_path = stage / "reviewed.srt"
    _binding(reviewed_path, REVIEWED_SRT_SHA256, label="REVIEWED_SRT")
    return document, reviewed_path.read_bytes()


def _validate_old_authority(plan: object) -> tuple[dict[str, object], dict[str, object]]:
    package_root = Path(getattr(plan, "package_root", ""))
    _load_json(
        package_root / f"{CANDIDATE_ID}.boundary_audit.json",
        BOUNDARY_AUDIT_SHA256,
        label="BOUNDARY_AUDIT",
    )
    record_path = Path(getattr(plan, "record_path", ""))
    record = _load_json(record_path, RECORD_SHA256, label="OLD_RECORD")
    boundary = record.get("boundary_audit")
    if not isinstance(boundary, Mapping):
        _fail("C6_EXACT_BOUNDARY_AUDIT_INVALID")
    if (
        record.get("candidate_id") != CANDIDATE_ID
        or record.get("date") not in {None, RECORDING_DATE}
        or record.get("duration_ms") != DELIVERY_LOCAL_END_MS - DELIVERY_LOCAL_START_MS
        or boundary.get("final_start_ms") != SOURCE_LOCAL_START_MS
        or boundary.get("final_end_ms") != SOURCE_LOCAL_END_MS
    ):
        _fail("C6_EXACT_OLD_RECORD_BINDING_DRIFT")
    review = boundary.get("final_delivery_boundary_semantic_review")
    if not isinstance(review, Mapping) or review.get("review_scope") != "final_delivery" or review.get("status") != "PASS":
        _fail("C6_EXACT_FINAL_BOUNDARY_REVIEW_INVALID")
    endpoint = review.get("final_endpoint_binding")
    if (
        not isinstance(endpoint, Mapping)
        or endpoint.get("final_start_ms") != DELIVERY_LOCAL_START_MS
        or endpoint.get("final_end_ms") != DELIVERY_LOCAL_END_MS
        or endpoint.get("reason_codes") != []
    ):
        _fail("C6_EXACT_FINAL_BOUNDARY_COORDINATE_DRIFT")
    return record, dict(review)


def _validate_truth_lanes(plan: object) -> None:
    baseline = getattr(plan, "baseline", None)
    config = getattr(baseline, "config", None)
    manifest = getattr(baseline, "manifest_path", None)
    baseline_path = getattr(baseline, "baseline_path", None)
    if not isinstance(config, Mapping) or not isinstance(manifest, Path) or not isinstance(baseline_path, Path):
        _fail("C6_EXACT_BASELINE_INVALID")
    if (
        config.get("sha256") != REVIEWED_SRT_SHA256.removeprefix("sha256:")
        or config.get("absolute_source_start_ms") != ABSOLUTE_SOURCE_START_MS
        or config.get("absolute_source_end_ms") != ABSOLUTE_SOURCE_END_MS
        or config.get("source_sha256") != SOURCE_MEDIA_IDENTITY.removeprefix("sha256:")
        or config.get("time_domain") != "DELIVERY_LOCAL"
    ):
        _fail("C6_EXACT_BASELINE_BINDING_DRIFT")
    _binding(baseline_path, REVIEWED_SRT_SHA256, label="BASELINE")
    lanes = config.get("operator_truth_lanes")
    if not isinstance(lanes, Mapping):
        _fail("C6_EXACT_TRUTH_LANES_INVALID")
    for key, expected, label in (
        ("pipeline_diagnostic", PIPELINE_SRT_SHA256, "PIPELINE"),
        ("decision_ledger", DECISION_LEDGER_SHA256, "LEDGER"),
        ("diff_receipt", TRUTH_DIFF_SHA256, "TRUTH_DIFF"),
    ):
        declaration = lanes.get(key)
        if not isinstance(declaration, Mapping) or declaration.get("sha256") != expected.removeprefix("sha256:"):
            _fail(f"C6_EXACT_{label}_DECLARATION_DRIFT")
        path = manifest.parent / str(declaration.get("path") or "")
        _binding(path, expected, label=label)


def _chat_authority(plan: object) -> dict[str, object]:
    path = Path(getattr(plan, "package_root")) / f"{CANDIDATE_ID}.chat-authority.json"
    chat = _load_json(path, CHAT_SHA256, label="CHAT_AUTHORITY")
    if (
        chat.get("schema_version") != "chat-authority-audit.v2"
        or chat.get("status") not in {"NO_MATCH", "APPLIED_AND_VERIFIED"}
        or chat.get("final_status") != "FINAL_ARTIFACTS_VERIFIED"
    ):
        _fail("C6_EXACT_CHAT_AUTHORITY_STATUS_INVALID")
    review = chat.get("final_review_audit")
    if not isinstance(review, Mapping) or review.get("schema_version") != "final-review-audit.v2" or review.get("status") != "CLEAN" or review.get("reviewed_srt_sha256") != REVIEWED_SRT_SHA256:
        _fail("C6_EXACT_CHAT_FINAL_REVIEW_BINDING_INVALID")
    return chat


def _compare_projection(pipeline: bytes, reviewed: bytes) -> str:
    old_rows = _rows(pipeline, label="PIPELINE_SRT")
    new_rows = _rows(reviewed, label="REVIEWED_SRT")
    changed: list[int] = []
    for ordinal, (old, new) in enumerate(zip(old_rows, new_rows, strict=True), start=1):
        if old[:3] != new[:3] or old[0] != ordinal or new[0] != ordinal:
            _fail("C6_EXACT_CUE_TIMING_OR_ORDER_DRIFT")
        if old[3] != new[3]:
            changed.append(ordinal)
    if changed != [17] or old_rows[16][3] != OLD_CUE17 or new_rows[16][3] != NEW_CUE17:
        _fail("C6_EXACT_CUE17_PROJECTION_DRIFT")
    if new_rows[0][1] < DELIVERY_LOCAL_START_MS or new_rows[-1][2] > DELIVERY_LOCAL_END_MS:
        _fail("C6_EXACT_DELIVERY_GRID_DRIFT")
    from src.autoslice.boundary_semantic_review import cue_grid_sha256

    try:
        from src.autoslice.jingting_chunker import parse_srt_cues
        reviewed_cues = parse_srt_cues(reviewed.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise C6ExactFinalBoundaryAdapterError("C6_EXACT_REVIEWED_SRT_INVALID") from exc
    return cue_grid_sha256(reviewed_cues)


def _audit(*, reviewed_sha: str, boundary_review: Mapping[str, object], grid_sha: str) -> dict[str, object]:
    boundary = copy.deepcopy(dict(boundary_review))
    endpoint = boundary.get("final_endpoint_binding")
    if isinstance(endpoint, Mapping):
        endpoint = dict(endpoint)
        endpoint["semantic_cue_grid_sha256"] = grid_sha
        endpoint["final_cue_grid_sha256"] = grid_sha
        boundary["final_endpoint_binding"] = endpoint
    boundary["cue_grid_sha256"] = grid_sha
    audit: dict[str, object] = {
        "schema_version": "final-review-audit.v2",
        "status": "CLEAN",
        "release_gate": "PASS",
        "reviewed_srt_sha256": reviewed_sha,
        "discovery": {"status": "COMPLETE", "provider_called": False},
        "correction_mutation_authority": {"schema_version": "subtitle-correction-mutation-audit.v1", "status": "PASS", "timing_immutable": True},
        "findings": [],
        "validated_finding_count": 0,
        "boundary_semantic_review": boundary,
        "c6_exact_final_boundary": {
            "schema_version": "c6-exact-final-boundary-audit.v1",
            "candidate_id": CANDIDATE_ID,
            "recording_date": RECORDING_DATE,
            "reviewed_srt_sha256": REVIEWED_SRT_SHA256,
            "pipeline_srt_sha256": PIPELINE_SRT_SHA256,
            "record_sha256": RECORD_SHA256,
            "boundary_audit_sha256": BOUNDARY_AUDIT_SHA256,
            "chat_authority_sha256": CHAT_SHA256,
            "accepted_authority_self_hash": AUTHORITY_SELF_HASH,
            "source_local_callback": {"start_ms": SOURCE_LOCAL_START_MS, "end_ms": SOURCE_LOCAL_END_MS},
            "delivery_local_grid": {"start_ms": DELIVERY_LOCAL_START_MS, "end_ms": DELIVERY_LOCAL_END_MS},
            "cue_count": EXPECTED_CUE_COUNT,
            "changed_cues": [17],
            "provider_called": False,
            "upload_allowed": False,
        },
    }
    try:
        from src.autoslice.final_review_contract import validate_final_review_release

        validate_final_review_release(audit, expected_srt_sha256=reviewed_sha)
    except Exception as exc:
        raise C6ExactFinalBoundaryAdapterError("C6_EXACT_FINAL_REVIEW_AUDIT_INVALID") from exc
    return audit


def build_c6_exact_final_reviewer(*, plan: object, stage: Path) -> Callable[[str, Mapping[str, object], int, int], dict[str, object]]:
    """Verify the post-stage clean C6 closure, then return a pure reviewer."""

    _validate_authority()
    _validate_truth_lanes(plan)
    _stage_document, staged_reviewed = _validate_stage(plan, stage)
    _record, boundary_review = _validate_old_authority(plan)
    chat = _chat_authority(plan)
    pipeline_path = Path(getattr(plan.baseline, "manifest_path")).parent / f"{CANDIDATE_ID}.pipeline-diagnostic.srt"
    pipeline = _binding(pipeline_path, PIPELINE_SRT_SHA256, label="PIPELINE").path.read_bytes()
    if _sha_bytes(staged_reviewed) != REVIEWED_SRT_SHA256:
        _fail("C6_EXACT_STAGE_REVIEWED_SRT_DRIFT")
    grid_sha = _compare_projection(pipeline, staged_reviewed)
    expected_chat_review = copy.deepcopy(chat["final_review_audit"])
    frozen_audit = _audit(reviewed_sha=REVIEWED_SRT_SHA256, boundary_review=boundary_review, grid_sha=grid_sha)
    if expected_chat_review.get("reviewed_srt_sha256") != REVIEWED_SRT_SHA256:
        _fail("C6_EXACT_CHAT_REVIEW_SRT_DRIFT")

    def review(final_srt_text: str, verified_authority_audit: Mapping[str, object], timeline_offset_ms: int, source_final_end_ms: int) -> dict[str, object]:
        if (
            not isinstance(verified_authority_audit, Mapping)
            or verified_authority_audit.get("schema_version") != "chat-authority-audit.v2"
            or verified_authority_audit.get("status") not in {"NO_MATCH", "APPLIED_AND_VERIFIED"}
            or verified_authority_audit.get("final_status") != "FINAL_ARTIFACTS_VERIFIED"
        ):
            _fail("C6_EXACT_CHAT_AUTHORITY_CALLBACK_INVALID")
        if timeline_offset_ms != SOURCE_LOCAL_START_MS or source_final_end_ms != SOURCE_LOCAL_END_MS:
            _fail("C6_EXACT_CALLBACK_COORDINATE_DRIFT")
        if final_srt_text.encode("utf-8") != staged_reviewed:
            _fail("C6_EXACT_FINAL_SRT_BYTES_MISMATCH")
        return copy.deepcopy(frozen_audit)

    return review
