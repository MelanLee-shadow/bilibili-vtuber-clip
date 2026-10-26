"""Provider-free source-fact successor for B2 E422's exact private v7 package.

The v7 outer-display subtitle intentionally removes watched-source dialogue that
was already visible inside the watched video.  Public title/hook support must
therefore replay the prior, hash-bound nested-source attribution rather than ask
an unscoped reviewer to rediscover those facts in the reduced host-display SRT.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
from typing import Any

from src.autoslice.final_review_contract import validate_final_review_release
from src.autoslice.jingting_chunker import parse_srt_cues


SCHEMA_VERSION = "b2-e422-v7-source-fact-successor.v1"
DECISION = "KEEP_VIA_E421_V7_NESTED_SOURCE_AND_E422_ENDPOINT"
CANDIDATE_ID = "auto_120029_1409_1578"
TARGET_BVID = "BV18dac6iEvJ"
OBSERVED_PREDECESSOR_CID = 42165076036
TITLE = "【李豆沙】打嗝视频里怎么还藏着雷公电母？！"
SELECTION_HOOK = (
    "李豆沙点开“神秘可爱小动静”观看打嗝视频，片中从可爱嗝声聊到像打雷的声音，"
    "之后又比较鼓脸与方脸。"
)

PARENT_SOURCE_FACT_RECEIPT_SHA256 = (
    "sha256:ba6050a02492b8ad892d058e3b480f0e645a6e98ae69a773175ecddda49fcd37"
)
PARENT_STORY_CONTRACT_SHA256 = (
    "sha256:79688269794b91f7469dde2aa7ff33d3b25d17e01a7e4f3c385c9465de540dfa"
)
PARENT_SRT_SHA256 = (
    "sha256:f10409d4ebe626a15ca3414dab41b1efcc0faee8bd805ee848d7967046f2047a"
)
CURRENT_SRT_SHA256 = (
    "sha256:a0856249250c46623468f5b7735027c7007efa0f88c6b838c5837fe65fdac57f"
)
CURRENT_ASS_SHA256 = (
    "sha256:8867bbdb5b79635dc839828ee9ed23664c291b81804c8db29defa47f3807b5c1"
)
CURRENT_VIDEO_SHA256 = (
    "sha256:2c309f6a23846ef7d5ed45513b73455fd55b093f1b269caf04e35d87a93ac498"
)
CURRENT_TRANSCRIPT_SHA256 = (
    "sha256:4fd5cc0684e73d6230054b404a87e2802c8004c4ccf05eef7d64ddb22fcb71f6"
)
CURRENT_STORY_PRE_SOURCE_FILE_SHA256 = (
    "sha256:bc8809243da68a620e6c53e37d63f048b0228ea9b3462ce4549a99355c4485c0"
)
CURRENT_STORY_PRE_SOURCE_CANONICAL_SHA256 = (
    "sha256:a81c3fc164af4d3daf6e4164d7128ef588a1d63086b9305fa6295c7b08365570"
)
NESTED_ATTRIBUTION_SHA256 = (
    "sha256:68b9da6d95f97bfca226fa3f196b173269b2b59ade8712cb0e6148bd94b91d02"
)
SPEAKER_EVIDENCE_SHA256 = (
    "sha256:7ad0672052878d79d0ad5f24e41ee57a6a8c401596b8954beb078ca45b39c989"
)

_EVIDENCE_SHA256 = {
    "e421_result": (
        "sha256:f910eeadf48027abc7879083ca76f47f033ebf197628eaa553bee2c682c5a3e6"
    ),
    "e421_review": (
        "sha256:2e0957ceb96555c2eb0d6e5dfdfc08216700f5affc133ffe3d79a402d59862bd"
    ),
    "e421_matrix": (
        "sha256:695aaa30cef0df4326dc797cd24b4079b94e1219e5c6254eabd681c566267e99"
    ),
    "e422_final_review": (
        "sha256:a8899a7fff24c9c19875f57674249587f7b5d77e611bcbe077556abf79f04fc4"
    ),
    "e422_boundary": (
        "sha256:032166e5a0818b25d6284f127ec4a3aa2a1b32768e4b1d49e3d56139f5010529"
    ),
    "e422_result": (
        "sha256:80b515d2ab84ddcb20024e7d07c72be34fc0960b9088b214fe2931e9974bd415"
    ),
}

_REQUIRED_CURRENT_SURFACES = (
    "看露蒂丝和瓜咪打嗝",
    "打不赢哦，她是打这嗝",
    "为什么是方的？太奇怪了",
)
_NESTED_SOURCE_SURFACES = {
    45: "我打嗝跟打雷似的",
    46: "雷公电母来了有感觉吗",
}


class B2E422SourceFactSuccessorError(ValueError):
    def __init__(self, reason_code: str, detail: str | None = None) -> None:
        self.reason_code = reason_code
        super().__init__(detail or reason_code)


def _fail(reason_code: str, detail: str | None = None) -> None:
    raise B2E422SourceFactSuccessorError(reason_code, detail)


def _bytes_sha256(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _canonical_sha256(value: object) -> str:
    return _bytes_sha256(_canonical_bytes(value))


def _seal(document: Mapping[str, object]) -> dict[str, object]:
    sealed = deepcopy(dict(document))
    sealed.pop("receipt_sha256", None)
    sealed["receipt_sha256"] = _canonical_sha256(sealed)
    return sealed


def _receipt_is_sealed(document: Mapping[str, object]) -> bool:
    body = deepcopy(dict(document))
    declared = body.pop("receipt_sha256", None)
    return isinstance(declared, str) and _canonical_sha256(body) == declared


def _read_bytes(value: str | bytes | Path, reason_code: str) -> bytes:
    if isinstance(value, Path):
        if value.is_symlink() or not value.is_file():
            _fail(reason_code)
        return value.read_bytes()
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8")
    _fail(reason_code)
    raise AssertionError("unreachable")


def _parse_json(raw: bytes, reason_code: str) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        _fail(reason_code, str(exc))
    if not isinstance(value, dict):
        _fail(reason_code)
    return value


def _transcript(raw: bytes) -> str:
    try:
        text = raw.decode("utf-8-sig", errors="strict")
        cues = parse_srt_cues(text)
    except (UnicodeError, ValueError) as exc:
        _fail("B2_E422_SOURCE_FACT_SRT_INVALID", str(exc))
    return "\n".join(cue.text.strip() for cue in cues if cue.text.strip())


def _normalized(value: str) -> str:
    return "".join(char for char in value if not char.isspace())


def _story_without_source_fact(story_contract: Mapping[str, object]) -> dict[str, object]:
    stripped = deepcopy(dict(story_contract))
    stripped.pop("source_fact_review", None)
    return stripped


def _validate_parent_source_fact(
    *,
    parent_receipt: Mapping[str, object],
    parent_story_contract: Mapping[str, object],
    parent_srt_raw: bytes,
    selection_hook: str,
    title: str,
    clip_context_prompt: str,
    selection_scorecard: object,
    speaker_evidence: object,
) -> None:
    if parent_receipt.get("receipt_sha256") != PARENT_SOURCE_FACT_RECEIPT_SHA256:
        _fail("B2_E422_PARENT_SOURCE_FACT_RECEIPT_DRIFT")
    if _canonical_sha256(parent_story_contract) != PARENT_STORY_CONTRACT_SHA256:
        _fail("B2_E422_PARENT_STORY_CONTRACT_DRIFT")
    if parent_story_contract.get("source_fact_review") != parent_receipt:
        _fail("B2_E422_PARENT_STORY_SOURCE_FACT_BINDING_DRIFT")
    if _bytes_sha256(parent_srt_raw) != PARENT_SRT_SHA256:
        _fail("B2_E422_PARENT_SRT_DRIFT")

    from src.autoslice.source_fact_review import validate_source_fact_review

    with tempfile.TemporaryDirectory(prefix="b2-e422-parent-source-fact-") as directory:
        parent_path = Path(directory) / "parent.srt"
        parent_path.write_bytes(parent_srt_raw)
        if not validate_source_fact_review(
            parent_receipt,
            selection_hook=selection_hook,
            title=title,
            final_transcript=_transcript(parent_srt_raw),
            clip_context_prompt=clip_context_prompt,
            selection_scorecard=selection_scorecard,
            candidate_id=CANDIDATE_ID,
            final_reviewed_srt_path=parent_path,
            speaker_evidence=speaker_evidence,
            story_contract=parent_story_contract,
        ):
            _fail("B2_E422_PARENT_SOURCE_FACT_RECEIPT_INVALID")


def _nested_source_rows(
    parent_receipt: Mapping[str, object],
) -> list[dict[str, object]]:
    raw_b64 = parent_receipt.get("attribution_raw_b64")
    if not isinstance(raw_b64, str):
        _fail("B2_E422_PARENT_ATTRIBUTION_MISSING")
    try:
        raw = base64.b64decode(raw_b64, validate=True)
    except ValueError as exc:
        _fail("B2_E422_PARENT_ATTRIBUTION_INVALID", str(exc))
    if _bytes_sha256(raw) != NESTED_ATTRIBUTION_SHA256:
        _fail("B2_E422_PARENT_ATTRIBUTION_DRIFT")
    attribution = _parse_json(raw, "B2_E422_PARENT_ATTRIBUTION_INVALID")
    decisions = attribution.get("decisions")
    if not isinstance(decisions, list):
        _fail("B2_E422_PARENT_ATTRIBUTION_INVALID")
    selected: list[dict[str, object]] = []
    for original_cue, expected_surface in _NESTED_SOURCE_SURFACES.items():
        matches = [
            row
            for row in decisions
            if isinstance(row, Mapping) and row.get("original_cue") == original_cue
        ]
        if len(matches) != 1:
            _fail("B2_E422_NESTED_SOURCE_EVIDENCE_MISSING")
        row = dict(matches[0])
        basis = row.get("basis")
        if not (
            row.get("decision") == "REMOVE_SOURCE_MEDIA"
            and row.get("host_text") is None
            and row.get("source_text") == expected_surface
            and isinstance(basis, list)
            and "source_frame_embedded_caption" in basis
            and "exact_BVID_audio_alignment" in basis
            and any(
                isinstance(item, str)
                and item.startswith("reference_cancellation_correlation_")
                for item in basis
            )
        ):
            _fail("B2_E422_NESTED_SOURCE_EVIDENCE_INVALID")
        selected.append(row)
    return selected


def _validate_e421_result(value: Mapping[str, object]) -> None:
    artifacts = value.get("artifacts")
    coverage = value.get("coverage")
    release = value.get("formal_release_gates")
    if not (
        value.get("schema_version") == "b2-e421-private-v7-result.v1"
        and value.get("status")
        == "PASS_PRIVATE_V7_FINAL_TEXT_AUDIO_CORRESPONDENCE_AND_OUTER_DISPLAY_FORMAL_PACKAGE_PENDING_NO_UPLOAD"
        and value.get("candidate_id") == CANDIDATE_ID
        and value.get("target_bvid") == TARGET_BVID
        and value.get("observed_public_cid") == OBSERVED_PREDECESSOR_CID
        and isinstance(artifacts, Mapping)
        and isinstance(coverage, Mapping)
        and isinstance(release, Mapping)
    ):
        _fail("B2_E422_E421_RESULT_INVALID")
    expected_artifacts = {
        "srt": CURRENT_SRT_SHA256,
        "ass": CURRENT_ASS_SHA256,
        "video": CURRENT_VIDEO_SHA256,
        "final_55_cue_matrix": _EVIDENCE_SHA256["e421_matrix"],
    }
    for key, expected in expected_artifacts.items():
        row = artifacts.get(key)
        if not (
            isinstance(row, Mapping)
            and "sha256:" + str(row.get("sha256") or "").removeprefix("sha256:")
            == expected
        ):
            _fail("B2_E422_E421_ARTIFACT_BINDING_DRIFT", key)
    if not (
        coverage.get("original_v2_cues_accounted") == 55
        and coverage.get("final_outer_display_cues") == 29
        and coverage.get("visible_source_only_duplicate_outer_clauses_remaining") == 0
        and coverage.get("approved_ending_preserved") is True
        and coverage.get("unknown_and_overlap_preserved") is True
        and release.get("formal_review_package_built") is False
        and release.get("upload_performed") is False
        and release.get("new_bvid_created") is False
    ):
        _fail("B2_E422_E421_RESULT_SCOPE_INVALID")


def _validate_e421_review(value: Mapping[str, object]) -> None:
    verdict = value.get("content_verdict")
    publication = value.get("publication")
    if not (
        value.get("schema_version") == "b2-e421-review-result.v1"
        and value.get("status")
        == "PASS_PRIVATE_V7_CONTENT_AND_FINAL_OUTER_DISPLAY_FORMAL_PACKAGE_AND_SAME_BV_PUBLICATION_PENDING"
        and value.get("candidate_id") == CANDIDATE_ID
        and value.get("target_bvid") == TARGET_BVID
        and value.get("observed_public_cid") == OBSERVED_PREDECESSOR_CID
        and isinstance(verdict, Mapping)
        and isinstance(publication, Mapping)
        and verdict.get("content_condition_satisfied_for_private_v7") is True
        and verdict.get("complete_55_cue_actual_media_binding") is True
        and verdict.get("approved_ending_preserved") is True
        and verdict.get("visible_source_only_duplicate_outer_clauses_remaining") == 0
        and publication.get("private_content_condition_satisfied") is True
        and publication.get("same_bv_conditionally_authorized_by_user") is True
        and publication.get("upload_performed") is False
    ):
        _fail("B2_E422_E421_REVIEW_INVALID")


def _validate_e421_matrix(value: Mapping[str, object]) -> dict[str, object]:
    rows = value.get("rows")
    summary = value.get("summary")
    if not (
        value.get("schema_version")
        == "b2-e421-full-55-cue-final-text-audiovisual-disposition-matrix.v4"
        and value.get("candidate_id") == CANDIDATE_ID
        and value.get("target_bvid") == TARGET_BVID
        and value.get("row_count") == 55
        and isinstance(rows, list)
        and len(rows) == 55
        and isinstance(summary, Mapping)
        and summary.get("all_55_accounted") is True
        and summary.get("approved_ending_preserved") is True
        and summary.get("source_only_visible_duplicate_outer_clauses_remaining_by_decision")
        == 0
        and summary.get("unknown_and_overlap_preserved") is True
        and summary.get("e414_dependency") is False
    ):
        _fail("B2_E422_E421_MATRIX_INVALID")

    def one(original_cue: int) -> Mapping[str, object]:
        matches = [
            row
            for row in rows
            if isinstance(row, Mapping) and row.get("original_cue") == original_cue
        ]
        if len(matches) != 1:
            _fail("B2_E422_E421_MATRIX_ROW_MISSING", str(original_cue))
        return matches[0]

    removed = one(60)
    ending = one(64)
    if not (
        removed.get("semantic_final_text") == "我鼓起来脸是个方的"
        and removed.get("final_audio_correspondence") == "SOURCE_ONLY_CORRESPONDENCE"
        and removed.get("final_display_action") == "REMOVE_DUPLICATE_DISPLAY_ONLY"
        and removed.get("outer_display_segments") == []
    ):
        _fail("B2_E422_E421_SOURCE_ONLY_REMOVAL_INVALID")
    ending_segments = ending.get("outer_display_segments")
    if not (
        ending.get("semantic_final_text") == "为什么是方的？太奇怪了"
        and ending.get("final_audio_correspondence") == "MATERIAL_NON_SOURCE_RESIDUAL"
        and ending.get("final_display_action") == "KEEP"
        and isinstance(ending_segments, list)
        and len(ending_segments) == 1
        and isinstance(ending_segments[0], Mapping)
        and ending_segments[0].get("text") == "为什么是方的？太奇怪了"
    ):
        _fail("B2_E422_E421_APPROVED_ENDING_INVALID")
    return {
        "removed_source_only_original_cue": 60,
        "removed_source_only_surface": "我鼓起来脸是个方的",
        "retained_material_residual_original_cue": 64,
        "retained_approved_ending": "为什么是方的？太奇怪了",
    }


def _validate_e422_evidence(
    *,
    final_review: Mapping[str, object],
    boundary: Mapping[str, object],
    result: Mapping[str, object],
) -> None:
    try:
        validated = validate_final_review_release(
            final_review,
            expected_srt_sha256=CURRENT_SRT_SHA256,
        )
    except ValueError as exc:
        _fail("B2_E422_FINAL_REVIEW_INVALID", str(exc))
    if validated != dict(final_review):
        _fail("B2_E422_FINAL_REVIEW_MUTATED")
    accepted = final_review.get("source_content_acceptance")
    if not (
        isinstance(accepted, Mapping)
        and isinstance(accepted.get("e421_result"), Mapping)
        and accepted["e421_result"].get("sha256") == _EVIDENCE_SHA256["e421_result"]
        and isinstance(accepted.get("e421_review"), Mapping)
        and accepted["e421_review"].get("sha256") == _EVIDENCE_SHA256["e421_review"]
        and isinstance(accepted.get("final_55_cue_matrix"), Mapping)
        and accepted["final_55_cue_matrix"].get("sha256")
        == _EVIDENCE_SHA256["e421_matrix"]
    ):
        _fail("B2_E422_FINAL_REVIEW_E421_BINDING_DRIFT")
    if not (
        boundary.get("manual_end_mode") == "human_explicit_endpoint_v1"
        and boundary.get("final_start_ms") == 9_750
        and boundary.get("final_end_ms") == 161_840
        and boundary.get("snapped_sentence_end_ms") == 161_440
        and boundary.get("closure_sentence") == "为什么是方的？太奇怪了"
    ):
        _fail("B2_E422_BOUNDARY_AUDIT_INVALID")
    validators = result.get("validators")
    claims = result.get("claims")
    if not (
        result.get("schema_version") == "b2-e422-boundary-final-review-result.v2"
        and result.get("status")
        == "PASS_USER_EXPLICIT_COMPLETE_ENDPOINT_WITH_DISCLOSED_SAME_TOPIC_TAIL"
        and result.get("candidate_id") == CANDIDATE_ID
        and result.get("target_bvid") == TARGET_BVID
        and isinstance(validators, Mapping)
        and validators.get("source_boundary_review") is True
        and validators.get("final_delivery_boundary_review") is True
        and validators.get("generic_final_review_release") is True
        and validators.get("package_boundary_contract") is True
        and validators.get("issue_count") == 0
        and isinstance(claims, Mapping)
        and claims.get("next_topic_transition") is False
        and claims.get("same_topic_tail_disclosed") is True
        and claims.get("v7_text_or_timing_mutated") is False
    ):
        _fail("B2_E422_BOUNDARY_RESULT_INVALID")


def _build_from_bound_inputs(
    *,
    parent_receipt: Mapping[str, object],
    parent_story_contract: Mapping[str, object],
    parent_srt_raw: bytes,
    current_srt_raw: bytes,
    story_pre_source_raw: bytes,
    evidence_raw: Mapping[str, bytes],
    selection_hook: str,
    title: str,
    clip_context_prompt: str,
    selection_scorecard: object,
    speaker_evidence: object,
    story_contract: Mapping[str, object],
) -> dict[str, object]:
    if selection_hook != SELECTION_HOOK or title != TITLE:
        _fail("B2_E422_PUBLIC_SURFACE_DRIFT")
    if _canonical_sha256(speaker_evidence) != SPEAKER_EVIDENCE_SHA256:
        _fail("B2_E422_SPEAKER_EVIDENCE_DRIFT")
    if _bytes_sha256(current_srt_raw) != CURRENT_SRT_SHA256:
        _fail("B2_E422_CURRENT_SRT_DRIFT")
    current_transcript = _transcript(current_srt_raw)
    if _bytes_sha256(current_transcript.encode("utf-8")) != CURRENT_TRANSCRIPT_SHA256:
        _fail("B2_E422_CURRENT_TRANSCRIPT_DRIFT")

    _validate_parent_source_fact(
        parent_receipt=parent_receipt,
        parent_story_contract=parent_story_contract,
        parent_srt_raw=parent_srt_raw,
        selection_hook=selection_hook,
        title=title,
        clip_context_prompt=clip_context_prompt,
        selection_scorecard=selection_scorecard,
        speaker_evidence=speaker_evidence,
    )
    nested_rows = _nested_source_rows(parent_receipt)

    if _bytes_sha256(story_pre_source_raw) != CURRENT_STORY_PRE_SOURCE_FILE_SHA256:
        _fail("B2_E422_STORY_PRE_SOURCE_FILE_DRIFT")
    story_pre_source = _parse_json(
        story_pre_source_raw,
        "B2_E422_STORY_PRE_SOURCE_INVALID",
    )
    if _canonical_sha256(story_pre_source) != CURRENT_STORY_PRE_SOURCE_CANONICAL_SHA256:
        _fail("B2_E422_STORY_PRE_SOURCE_CANONICAL_DRIFT")
    if story_pre_source != _story_without_source_fact(story_contract):
        _fail("B2_E422_STORY_PRE_SOURCE_BINDING_DRIFT")
    if not (
        story_pre_source.get("candidate_id") == CANDIDATE_ID
        and story_pre_source.get("selection_hook") == selection_hook
        and story_pre_source.get("transcript_sha256") == CURRENT_TRANSCRIPT_SHA256
        and story_pre_source.get("normalized_transcript") == current_transcript
        and story_pre_source.get("selection_scorecard") == selection_scorecard
        and story_pre_source.get("clip_context_prompt") == clip_context_prompt
        and "source_fact_review" not in story_pre_source
    ):
        _fail("B2_E422_STORY_PRE_SOURCE_SCOPE_INVALID")

    if set(evidence_raw) != set(_EVIDENCE_SHA256):
        _fail("B2_E422_EVIDENCE_SET_INVALID")
    evidence: dict[str, dict[str, Any]] = {}
    for key, expected_sha in _EVIDENCE_SHA256.items():
        raw = evidence_raw[key]
        if _bytes_sha256(raw) != expected_sha:
            _fail("B2_E422_EVIDENCE_DRIFT", key)
        evidence[key] = _parse_json(raw, "B2_E422_EVIDENCE_INVALID")

    _validate_e421_result(evidence["e421_result"])
    _validate_e421_review(evidence["e421_review"])
    disposition = _validate_e421_matrix(evidence["e421_matrix"])
    _validate_e422_evidence(
        final_review=evidence["e422_final_review"],
        boundary=evidence["e422_boundary"],
        result=evidence["e422_result"],
    )

    current_normalized = _normalized(current_transcript)
    current_checks = [
        {
            "surface": surface,
            "surface_sha256": _bytes_sha256(surface.encode("utf-8")),
            "present": _normalized(surface) in current_normalized,
        }
        for surface in _REQUIRED_CURRENT_SURFACES
    ]
    if not all(row["present"] is True for row in current_checks):
        _fail("B2_E422_CURRENT_CONTEXT_EVIDENCE_MISSING")
    excluded_checks = [
        {
            "surface": surface,
            "surface_sha256": _bytes_sha256(surface.encode("utf-8")),
            "absent_from_outer_display": _normalized(surface) not in current_normalized,
        }
        for surface in _NESTED_SOURCE_SURFACES.values()
    ]
    if not all(row["absent_from_outer_display"] is True for row in excluded_checks):
        _fail("B2_E422_NESTED_SOURCE_DIALOGUE_REINSERTED")
    if not (
        parent_receipt.get("final_selection_hook") == selection_hook
        and parent_receipt.get("final_title") == title
        and "雷公电母" in title
        and "像打雷的声音" in selection_hook
        and "片中" in selection_hook
    ):
        _fail("B2_E422_PARENT_PUBLIC_SURFACE_DRIFT")

    receipt: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "status": "PASS",
        "decision": DECISION,
        "candidate_id": CANDIDATE_ID,
        "target_bvid": TARGET_BVID,
        "observed_predecessor_cid": OBSERVED_PREDECESSOR_CID,
        "original_selection_hook": selection_hook,
        "original_title": title,
        "final_selection_hook": selection_hook,
        "final_title": title,
        "parent_receipt": deepcopy(dict(parent_receipt)),
        "parent_receipt_sha256": PARENT_SOURCE_FACT_RECEIPT_SHA256,
        "parent_story_contract": deepcopy(dict(parent_story_contract)),
        "parent_story_contract_sha256": PARENT_STORY_CONTRACT_SHA256,
        "parent_srt_raw_b64": base64.b64encode(parent_srt_raw).decode("ascii"),
        "parent_srt_sha256": PARENT_SRT_SHA256,
        "current_srt_raw_b64": base64.b64encode(current_srt_raw).decode("ascii"),
        "current_srt_sha256": CURRENT_SRT_SHA256,
        "current_transcript_sha256": CURRENT_TRANSCRIPT_SHA256,
        "current_ass_sha256": CURRENT_ASS_SHA256,
        "current_video_sha256": CURRENT_VIDEO_SHA256,
        "story_contract_pre_source_fact_raw_b64": base64.b64encode(
            story_pre_source_raw
        ).decode("ascii"),
        "story_contract_pre_source_fact_file_sha256": (
            CURRENT_STORY_PRE_SOURCE_FILE_SHA256
        ),
        "story_contract_pre_source_fact_canonical_sha256": (
            CURRENT_STORY_PRE_SOURCE_CANONICAL_SHA256
        ),
        "evidence_raw_b64": {
            key: base64.b64encode(evidence_raw[key]).decode("ascii")
            for key in sorted(evidence_raw)
        },
        "evidence_sha256": dict(_EVIDENCE_SHA256),
        "nested_source_evidence": nested_rows,
        "nested_source_attribution_sha256": NESTED_ATTRIBUTION_SHA256,
        "e421_disposition_evidence": disposition,
        "current_transcript_evidence_checks": current_checks,
        "nested_source_outer_display_exclusion_checks": excluded_checks,
        "source_attribution_scope": (
            "WATCHED_SOURCE_MEDIA_FACTS_SUPPORT_DERIVED_PUBLIC_TEXT_ONLY;"
            "DOES_NOT_REINSERT_SOURCE_DIALOGUE_INTO_HOST_SUBTITLES"
        ),
        "speaker_evidence": deepcopy(speaker_evidence),
        "speaker_evidence_sha256": SPEAKER_EVIDENCE_SHA256,
        "clip_context_prompt_sha256": _bytes_sha256(
            clip_context_prompt.encode("utf-8")
        ),
        "selection_scorecard_sha256": _canonical_sha256(selection_scorecard),
        "public_surfaces_changed": False,
        "changed_surfaces": [],
        "provider_calls": 0,
        "paid_calls": 0,
        "new_fact_votes": 0,
        "new_audio_calls": 0,
        "asr_calls": 0,
        "render_calls": 0,
        "image_generation_calls": 0,
        "production_state_mutations": 0,
        "upload_calls": 0,
        "same_bv_only": True,
        "new_bvid_allowed": False,
        "private_package_only": True,
        "production_replacement_authorized": False,
        "upload_authorized": False,
        "summary": (
            "The unchanged title/hook are supported by the exact prior nested-source "
            "attribution for thunder/雷公电母 plus the current v7 context and approved "
            "ending. Source-only dialogue remains absent from the outer host-display SRT."
        ),
    }
    return _seal(receipt)


def build_b2_e422_source_fact_successor(
    *,
    parent_receipt: Mapping[str, object],
    parent_story_contract: Mapping[str, object],
    parent_srt: str | bytes | Path,
    current_srt: str | bytes | Path,
    story_contract_pre_source_fact: str | bytes | Path,
    evidence: Mapping[str, str | bytes | Path],
    selection_hook: str,
    title: str,
    clip_context_prompt: str,
    selection_scorecard: object,
    speaker_evidence: object,
    story_contract: Mapping[str, object],
) -> dict[str, object]:
    return _build_from_bound_inputs(
        parent_receipt=parent_receipt,
        parent_story_contract=parent_story_contract,
        parent_srt_raw=_read_bytes(parent_srt, "B2_E422_PARENT_SRT_UNAVAILABLE"),
        current_srt_raw=_read_bytes(current_srt, "B2_E422_CURRENT_SRT_UNAVAILABLE"),
        story_pre_source_raw=_read_bytes(
            story_contract_pre_source_fact,
            "B2_E422_STORY_PRE_SOURCE_UNAVAILABLE",
        ),
        evidence_raw={
            key: _read_bytes(value, "B2_E422_EVIDENCE_UNAVAILABLE")
            for key, value in evidence.items()
        },
        selection_hook=selection_hook,
        title=title,
        clip_context_prompt=clip_context_prompt,
        selection_scorecard=selection_scorecard,
        speaker_evidence=speaker_evidence,
        story_contract=story_contract,
    )


def validate_b2_e422_source_fact_successor(
    review: object,
    *,
    selection_hook: str,
    title: str,
    final_transcript: str,
    clip_context_prompt: str,
    selection_scorecard: object = None,
    candidate_id: str | None = None,
    final_reviewed_srt_path: Path | None = None,
    speaker_evidence: object = None,
    story_contract: Mapping[str, object] | None = None,
    **_unused: object,
) -> bool:
    if not isinstance(review, Mapping) or review.get("schema_version") != SCHEMA_VERSION:
        return False
    document = deepcopy(dict(review))
    if not _receipt_is_sealed(document):
        return False
    if (
        candidate_id != CANDIDATE_ID
        or final_reviewed_srt_path is None
        or story_contract is None
        or final_reviewed_srt_path.is_symlink()
        or not final_reviewed_srt_path.is_file()
    ):
        return False
    try:
        current_raw = final_reviewed_srt_path.read_bytes()
        parent_raw = base64.b64decode(
            str(document["parent_srt_raw_b64"]), validate=True
        )
        embedded_current = base64.b64decode(
            str(document["current_srt_raw_b64"]), validate=True
        )
        story_raw = base64.b64decode(
            str(document["story_contract_pre_source_fact_raw_b64"]),
            validate=True,
        )
        raw_map = document["evidence_raw_b64"]
        if not isinstance(raw_map, Mapping):
            return False
        evidence_raw = {
            str(key): base64.b64decode(str(value), validate=True)
            for key, value in raw_map.items()
        }
        parent_receipt = document["parent_receipt"]
        parent_story = document["parent_story_contract"]
        if not isinstance(parent_receipt, Mapping) or not isinstance(
            parent_story, Mapping
        ):
            return False
        expected = _build_from_bound_inputs(
            parent_receipt=parent_receipt,
            parent_story_contract=parent_story,
            parent_srt_raw=parent_raw,
            current_srt_raw=current_raw,
            story_pre_source_raw=story_raw,
            evidence_raw=evidence_raw,
            selection_hook=selection_hook,
            title=title,
            clip_context_prompt=clip_context_prompt,
            selection_scorecard=selection_scorecard,
            speaker_evidence=speaker_evidence,
            story_contract=story_contract,
        )
    except (KeyError, OSError, TypeError, ValueError):
        return False
    return bool(
        embedded_current == current_raw
        and final_transcript == _transcript(current_raw)
        and document == expected
    )


def validate_from_scope(scope: Mapping[str, object]) -> bool | None:
    review = scope.get("review")
    if not isinstance(review, Mapping) or review.get("schema_version") != SCHEMA_VERSION:
        return None
    return validate_b2_e422_source_fact_successor(
        review,
        selection_hook=str(scope.get("selection_hook") or ""),
        title=str(scope.get("title") or ""),
        final_transcript=str(scope.get("final_transcript") or ""),
        clip_context_prompt=str(scope.get("clip_context_prompt") or ""),
        selection_scorecard=scope.get("selection_scorecard"),
        candidate_id=(
            str(scope.get("candidate_id"))
            if scope.get("candidate_id") is not None
            else None
        ),
        final_reviewed_srt_path=(
            Path(scope["final_reviewed_srt_path"])
            if scope.get("final_reviewed_srt_path") is not None
            else None
        ),
        speaker_evidence=scope.get("speaker_evidence"),
        story_contract=(
            scope.get("story_contract")
            if isinstance(scope.get("story_contract"), Mapping)
            else None
        ),
    )
