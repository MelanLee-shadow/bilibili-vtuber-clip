from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
try:
    from enum import StrEnum
except ImportError:  # Python 3.10 on the free/bilive container.
    from enum import Enum

    class StrEnum(str, Enum):
        pass
from typing import Iterable, Mapping, Sequence


REQUIRED_PUBLISH_ARTIFACT_KEYS: tuple[str, ...] = (
    "video_sha256",
    "draft_subtitle_sha256",
    "jingting_subtitle_sha256",
    "jingting_manifest_sha256",
    "cover_sha256",
    "publish_json_sha256",
)

# Typed provenance lanes.  A jingting manifest is valid evidence when it says
# HONESTLY which layer produced the refined subtitle — not only when that layer
# happened to be agy.  Ivan 2026-07-19（项目 memory）：AGY 订阅 / 免费 key /
# 付费 backup 是同一个 Gemini 模型的配额顺序，「按 provider 层拒证据的门 = 过度
# 限制」，处方是「任一层证据有效 + 按层钉模型串」。Ivan 2026-08-10：「需要调用
# AGY->gemini 这条链的，全都复用一种接口才好」/「CPA请求失败的逻辑是积极重试，
# 而不是判候选死，毕竟这跟候选没有关系啊」。
JINGTING_LANE_AGY = "agy"
JINGTING_LANE_GEMINI_API_FALLBACK = "gemini_api_fallback"
JINGTING_LANE_SONG_LRC_BYPASS = "song_lrc_bypass"
JINGTING_LANE_UNKNOWN = "unknown"

# ``source_context_executor`` deliberately skips agy refinement for songs: the
# independently fetched LRC plus audio alignment is the subtitle authority, so a
# talk-style multimodal rewrite is both redundant and an avoidable provider
# dependency.  That bypass writes this exact self-attesting triple.
SONG_LRC_BYPASS_PROVIDER = "source_draft_context"
SONG_LRC_BYPASS_AUTHORITY_SCOPE = "proof_context_only_external_lrc_required"
GEMINI_API_FALLBACK_PROVIDER = "gemini_api"


class DecisionAction(StrEnum):
    AUTO_UPLOAD = "AUTO_UPLOAD"
    AUTO_RECUT = "AUTO_RECUT"
    DROP = "DROP"
    BLOCK = "BLOCK"
    RETRY = "RETRY"


@dataclass(frozen=True)
class JingtingProvenance:
    """Machine-readable proof of WHICH layer produced the jingting subtitle.

    ``refinement_required``/``subtitle_authority_scope`` are the song lane's
    self-attestation that agy refinement was deliberately skipped.  Dropping
    them (as this dataclass used to) makes a designed bypass indistinguishable
    from an unauthorized provider substitution — the 2026-08-07 root cause.
    """

    manifest_present: bool = False
    provider: str | None = None
    agy_rc: int | None = None
    model: str | None = None
    provider_fallback_used: bool | None = None
    refinement_required: bool | None = None
    subtitle_authority_scope: str | None = None

    @classmethod
    def from_manifest(cls, manifest: Mapping[str, object] | None) -> "JingtingProvenance":
        if manifest is None:
            return cls(manifest_present=False)
        return cls(
            manifest_present=True,
            provider=_optional_str(manifest.get("provider")),
            agy_rc=_optional_int(manifest.get("agy_rc")),
            model=_optional_str(manifest.get("model")),
            provider_fallback_used=_optional_bool(manifest.get("provider_fallback_used")),
            refinement_required=_optional_bool(manifest.get("refinement_required")),
            subtitle_authority_scope=_optional_str(manifest.get("subtitle_authority_scope")),
        )

    def lane(self) -> str:
        """Which typed provenance lane this manifest claims, if any."""

        if (
            self.provider == SONG_LRC_BYPASS_PROVIDER
            and self.refinement_required is False
            and self.subtitle_authority_scope == SONG_LRC_BYPASS_AUTHORITY_SCOPE
        ):
            return JINGTING_LANE_SONG_LRC_BYPASS
        if self.provider == GEMINI_API_FALLBACK_PROVIDER:
            return JINGTING_LANE_GEMINI_API_FALLBACK
        if self.provider == JINGTING_LANE_AGY:
            return JINGTING_LANE_AGY
        return JINGTING_LANE_UNKNOWN

    def to_metadata(self) -> dict[str, object]:
        return {**asdict(self), "lane": self.lane()}


@dataclass(frozen=True)
class ProvenanceCheck:
    code: str
    passed: bool
    severity: str
    reason_code: str | None = None
    evidence: Mapping[str, object] = field(default_factory=dict)

    def to_manifest_check(self) -> dict[str, object]:
        data: dict[str, object] = {
            "code": self.code,
            "pass": self.passed,
            "severity": self.severity,
            "evidence": dict(self.evidence),
        }
        if self.reason_code is not None:
            data["reason_code"] = self.reason_code
        return data


@dataclass(frozen=True)
class CandidateReview:
    candidate_id: str
    jingting_done: bool = False
    # A complete hash-bound LRC timeline is the final subtitle authority for
    # songs.  AGY text polishing remains mandatory for talk, but its provider
    # outage must not block the independent song proof chain.
    verified_song_lrc_authority: bool = False
    release_ready: bool | None = None
    review_required_findings: Sequence[str] | None = None
    foreground_song_overlap_seconds: float | None = None
    song_complete: bool | None = None
    lyrics_alignment_ready: bool | None = None
    start_boundary_score: float | None = None
    end_boundary_score: float | None = None
    standalone_score: float | None = None
    payoff_score: float | None = None
    open_loop_count: int | None = None
    editorial_score: float | None = None
    duplicate_similarity: float | None = None
    subtitle_alignment_p95_ms: float | None = None
    actual_cut_error_ms: float | None = None
    recut_attempt: int = 0
    max_recut_attempts: int = 2
    jingting_provenance: JingtingProvenance | None = None


@dataclass(frozen=True)
class ReviewDecision:
    action: DecisionAction
    reason_codes: tuple[str, ...] = ()
    score: float = 0.0


@dataclass(frozen=True)
class AutoReviewManifest:
    candidate_id: str
    decision: ReviewDecision
    schema_version: str = "slice-auto-review.v1"
    artifacts: Mapping[str, str] = field(default_factory=dict)
    jingting_provenance: JingtingProvenance | None = None
    checks: Sequence[Mapping[str, object]] = field(default_factory=tuple)
    metadata: Mapping[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        provenance_checks = evaluate_jingting_provenance(self.jingting_provenance)
        provenance_reason_codes = _provenance_reason_codes(provenance_checks)
        reason_codes = tuple(dict.fromkeys(self.decision.reason_codes + provenance_reason_codes))
        checks = [dict(check) for check in self.checks]
        checks.extend(check.to_manifest_check() for check in provenance_checks)
        metadata = dict(self.metadata)
        metadata["jingting_provenance"] = (
            self.jingting_provenance.to_metadata() if self.jingting_provenance is not None else None
        )
        return {
            "schema_version": self.schema_version,
            "candidate_id": self.candidate_id,
            "decision": {
                "action": self.decision.action.value,
                "reason_codes": list(reason_codes),
                "score": self.decision.score,
            },
            "artifacts": dict(self.artifacts),
            "checks": checks,
            "metadata": metadata,
        }


def auto_review_manifest_sha256(manifest: AutoReviewManifest) -> str:
    payload = json.dumps(manifest.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def is_publish_gate_satisfied(
    *,
    jingting_done: bool,
    manifest: AutoReviewManifest | None,
    expected_artifacts: Mapping[str, str] | None = None,
) -> bool:
    """Return whether publish/upload preparation may proceed.

    This is the local release gate that future uploader code should call before
    preparing publish payloads.  It intentionally treats ``*.jingting.done`` as
    necessary-but-not-sufficient: only a v1 auto-review manifest with an
    ``AUTO_UPLOAD`` decision, no residual reason codes, and matching current
    artifact hashes satisfies the gate.
    """

    if not jingting_done or manifest is None:
        return False
    if manifest.schema_version != "slice-auto-review.v1":
        return False
    if manifest.decision.action != DecisionAction.AUTO_UPLOAD:
        return False
    if manifest.decision.reason_codes:
        return False
    if _provenance_reason_codes(evaluate_jingting_provenance(manifest.jingting_provenance)):
        return False

    expected_artifacts = dict(expected_artifacts or {})
    for artifact_name in REQUIRED_PUBLISH_ARTIFACT_KEYS:
        expected_hash = expected_artifacts.get(artifact_name)
        if not expected_hash:
            return False
        if manifest.artifacts.get(artifact_name) != expected_hash:
            return False

    return True


def review_candidate(candidate: CandidateReview) -> ReviewDecision:
    reasons: list[str] = []
    score = _decision_score(candidate)

    if not candidate.jingting_done and not candidate.verified_song_lrc_authority:
        return ReviewDecision(DecisionAction.RETRY, ("JINGTING_PENDING",), score)

    if not candidate.verified_song_lrc_authority:
        reasons.extend(
            _provenance_reason_codes(evaluate_jingting_provenance(candidate.jingting_provenance))
        )
    reasons.extend(_provenance_reason_codes(evaluate_required_evidence(candidate)))

    if (
        not candidate.verified_song_lrc_authority
        and (candidate.review_required_findings or candidate.release_ready is False)
    ):
        reasons.append("JINGTING_REVIEW_REQUIRED")

    if candidate.foreground_song_overlap_seconds is not None and candidate.foreground_song_overlap_seconds > 5.0:
        if candidate.song_complete is False:
            reasons.append("SONG_PARTIAL")
        if candidate.lyrics_alignment_ready is False:
            reasons.append("LYRICS_ALIGNMENT_REQUIRED")

    if candidate.subtitle_alignment_p95_ms is not None and candidate.subtitle_alignment_p95_ms > 800.0:
        reasons.append("SUBTITLE_ALIGNMENT_BAD")

    hard_block_prefixes = (
        "JINGTING_PROVENANCE_MISSING",
        "JINGTING_PROVIDER_NOT_AGY",
        "JINGTING_AGY_FAILED",
        "JINGTING_MODEL_MISSING",
        # A bypass manifest that claims a model string is lying about which
        # layer ran.  Provenance dishonesty is a BLOCK, never a retry.
        "JINGTING_BYPASS_MODEL_UNEXPECTED",
        "JINGTING_PROVIDER_FALLBACK_USED",
        "JINGTING_PROVIDER_FALLBACK_UNKNOWN",
        "RELEASE_READY_MISSING",
        "REVIEW_REQUIRED_FINDINGS_MISSING",
        "FOREGROUND_SONG_OVERLAP_MISSING",
        "SONG_COMPLETENESS_MISSING",
        "LYRICS_ALIGNMENT_MISSING",
        "START_BOUNDARY_MISSING",
        "END_BOUNDARY_MISSING",
        "STANDALONE_MISSING",
        "OPEN_LOOP_EVIDENCE_MISSING",
        "PAYOFF_MISSING_EVIDENCE",
        "EDITORIAL_SCORE_MISSING",
        "DUPLICATE_SIMILARITY_MISSING",
        "SUBTITLE_ALIGNMENT_MISSING",
        "ACTUAL_CUT_ERROR_MISSING",
        "JINGTING_REVIEW_REQUIRED",
        "SONG_PARTIAL",
        "LYRICS_ALIGNMENT_REQUIRED",
        "SUBTITLE_ALIGNMENT_BAD",
    )
    hard_block_reasons = [reason for reason in reasons if reason in hard_block_prefixes]
    if hard_block_reasons:
        return ReviewDecision(DecisionAction.BLOCK, tuple(dict.fromkeys(reasons)), score)

    recut_reasons: list[str] = []
    if candidate.actual_cut_error_ms is not None and candidate.actual_cut_error_ms > 100.0:
        recut_reasons.append("ACTUAL_CUT_ERROR_HIGH")
    if candidate.subtitle_alignment_p95_ms is not None and candidate.subtitle_alignment_p95_ms > 350.0:
        recut_reasons.append("SUBTITLE_ALIGNMENT_RETRY")
    if candidate.start_boundary_score is not None and candidate.start_boundary_score < 0.92:
        recut_reasons.append("START_BOUNDARY_LOW")
    if candidate.end_boundary_score is not None and candidate.end_boundary_score < 0.95:
        recut_reasons.append("END_BOUNDARY_LOW")
    if candidate.standalone_score is not None and candidate.standalone_score < 0.90:
        recut_reasons.append("STANDALONE_LOW")
    if candidate.open_loop_count is not None and candidate.open_loop_count > 0:
        recut_reasons.append("OPEN_LOOPS_PRESENT")

    if recut_reasons:
        if candidate.recut_attempt < candidate.max_recut_attempts:
            return ReviewDecision(DecisionAction.AUTO_RECUT, tuple(recut_reasons), score)
        if any(reason in recut_reasons for reason in ("ACTUAL_CUT_ERROR_HIGH", "SUBTITLE_ALIGNMENT_RETRY")):
            return ReviewDecision(
                DecisionAction.BLOCK,
                tuple(recut_reasons + ["RECUT_BUDGET_EXHAUSTED"]),
                score,
            )
        return ReviewDecision(
            DecisionAction.DROP,
            tuple(recut_reasons + ["RECUT_BUDGET_EXHAUSTED"]),
            score,
        )

    drop_reasons: list[str] = []
    if candidate.payoff_score is not None and candidate.payoff_score < 0.90:
        drop_reasons.append("PAYOFF_MISSING")
    if candidate.editorial_score is not None and candidate.editorial_score < 82.0:
        drop_reasons.append("EDITORIAL_SCORE_LOW")
    if candidate.duplicate_similarity is not None and candidate.duplicate_similarity >= 0.90:
        drop_reasons.append("DUPLICATE")

    if drop_reasons:
        return ReviewDecision(DecisionAction.DROP, tuple(drop_reasons), score)

    return ReviewDecision(DecisionAction.AUTO_UPLOAD, (), score)


def select_auto_uploadable(
    candidates: Iterable[CandidateReview], *, max_count: int
) -> list[CandidateReview]:
    """Return up to max_count release-ready candidates, never padding to fill quota."""

    if max_count <= 0:
        return []

    uploadable = [
        candidate
        for candidate in candidates
        if review_candidate(candidate).action == DecisionAction.AUTO_UPLOAD
    ]
    uploadable.sort(key=lambda item: _decision_score(item), reverse=True)
    return uploadable[:max_count]


def evaluate_required_evidence(candidate: CandidateReview) -> tuple[ProvenanceCheck, ...]:
    """Return fail-closed checks for content, duplicate, editorial, subtitle, and PTS evidence.

    AUTO_UPLOAD requires explicit evidence for every local-only analyzer gate.  A
    missing/unknown value is represented as a failed BLOCK check rather than an
    optimistic default.
    """

    return (
        _evidence_check(
            "RELEASE_READY_RECORDED",
            candidate.release_ready is not None,
            "RELEASE_READY_MISSING",
            {"release_ready": candidate.release_ready},
        ),
        _evidence_check(
            "REVIEW_REQUIRED_FINDINGS_RECORDED",
            candidate.review_required_findings is not None,
            "REVIEW_REQUIRED_FINDINGS_MISSING",
            {"review_required_findings": candidate.review_required_findings},
        ),
        _evidence_check(
            "FOREGROUND_SONG_OVERLAP_RECORDED",
            candidate.foreground_song_overlap_seconds is not None,
            "FOREGROUND_SONG_OVERLAP_MISSING",
            {"foreground_song_overlap_seconds": candidate.foreground_song_overlap_seconds},
        ),
        _evidence_check(
            "SONG_COMPLETENESS_RECORDED",
            candidate.song_complete is not None,
            "SONG_COMPLETENESS_MISSING",
            {"song_complete": candidate.song_complete},
        ),
        _evidence_check(
            "LYRICS_ALIGNMENT_RECORDED",
            candidate.lyrics_alignment_ready is not None,
            "LYRICS_ALIGNMENT_MISSING",
            {"lyrics_alignment_ready": candidate.lyrics_alignment_ready},
        ),
        _evidence_check(
            "START_BOUNDARY_RECORDED",
            candidate.start_boundary_score is not None,
            "START_BOUNDARY_MISSING",
            {"start_boundary_score": candidate.start_boundary_score},
        ),
        _evidence_check(
            "END_BOUNDARY_RECORDED",
            candidate.end_boundary_score is not None,
            "END_BOUNDARY_MISSING",
            {"end_boundary_score": candidate.end_boundary_score},
        ),
        _evidence_check(
            "STANDALONE_RECORDED",
            candidate.standalone_score is not None,
            "STANDALONE_MISSING",
            {"standalone_score": candidate.standalone_score},
        ),
        _evidence_check(
            "OPEN_LOOP_EVIDENCE_RECORDED",
            candidate.open_loop_count is not None,
            "OPEN_LOOP_EVIDENCE_MISSING",
            {"open_loop_count": candidate.open_loop_count},
        ),
        _evidence_check(
            "PAYOFF_RECORDED",
            candidate.payoff_score is not None,
            "PAYOFF_MISSING_EVIDENCE",
            {"payoff_score": candidate.payoff_score},
        ),
        _evidence_check(
            "EDITORIAL_SCORE_RECORDED",
            candidate.editorial_score is not None,
            "EDITORIAL_SCORE_MISSING",
            {"editorial_score": candidate.editorial_score},
        ),
        _evidence_check(
            "DUPLICATE_SIMILARITY_RECORDED",
            candidate.duplicate_similarity is not None,
            "DUPLICATE_SIMILARITY_MISSING",
            {"duplicate_similarity": candidate.duplicate_similarity},
        ),
        _evidence_check(
            "SUBTITLE_ALIGNMENT_RECORDED",
            candidate.subtitle_alignment_p95_ms is not None,
            "SUBTITLE_ALIGNMENT_MISSING",
            {"subtitle_alignment_p95_ms": candidate.subtitle_alignment_p95_ms},
        ),
        _evidence_check(
            "ACTUAL_CUT_ERROR_RECORDED",
            candidate.actual_cut_error_ms is not None,
            "ACTUAL_CUT_ERROR_MISSING",
            {"actual_cut_error_ms": candidate.actual_cut_error_ms},
        ),
    )


def evaluate_jingting_provenance(provenance: JingtingProvenance | None) -> tuple[ProvenanceCheck, ...]:
    """Fail-closed provenance checks, evaluated per typed lane.

    Every lane still has to prove the SAME four things — an execution outcome,
    an honest model string (or an honest declaration that no model ran), an
    honest fallback flag, and a recognized provider.  What changed on
    2026-08-10 is that "recognized provider" is no longer a synonym for
    ``agy``: a fully self-attesting song-LRC bypass and a fully self-attesting
    Gemini API failover are evidence, not violations.  An INCOMPLETE
    self-attestation blocks exactly as before.
    """

    if provenance is None:
        provenance = JingtingProvenance(manifest_present=False)

    lane = provenance.lane()
    provider_accepted = lane != JINGTING_LANE_UNKNOWN

    if lane == JINGTING_LANE_SONG_LRC_BYPASS:
        # No model ran, so claiming one is a dishonest manifest — the mirror
        # image of, not an instance of, JINGTING_MODEL_MISSING.
        model_passed = provenance.model is None
        model_reason = None if model_passed else "JINGTING_BYPASS_MODEL_UNEXPECTED"
        execution_succeeded = provenance.agy_rc == 0
        fallback_accepted = provenance.provider_fallback_used is False
    elif lane == JINGTING_LANE_GEMINI_API_FALLBACK:
        # 按层钉模型串: the failover must name the model it actually ran and
        # must record that the agy leg was attempted and how it exited.
        model_passed = bool(provenance.model)
        model_reason = None if model_passed else "JINGTING_MODEL_MISSING"
        execution_succeeded = provenance.agy_rc is not None
        fallback_accepted = provenance.provider_fallback_used is True
    else:
        model_passed = bool(provenance.model)
        model_reason = None if model_passed else "JINGTING_MODEL_MISSING"
        execution_succeeded = provenance.agy_rc == 0
        fallback_accepted = provenance.provider_fallback_used is False

    lane_evidence = {
        "lane": lane,
        "provider": provenance.provider,
        "refinement_required": provenance.refinement_required,
        "subtitle_authority_scope": provenance.subtitle_authority_scope,
    }
    return (
        ProvenanceCheck(
            code="JINGTING_MANIFEST_PRESENT",
            passed=provenance.manifest_present,
            severity="BLOCK",
            reason_code=None if provenance.manifest_present else "JINGTING_PROVENANCE_MISSING",
            evidence={"manifest_present": provenance.manifest_present},
        ),
        ProvenanceCheck(
            code="JINGTING_PROVIDER_AGY",
            passed=provider_accepted,
            severity="BLOCK",
            reason_code=None if provider_accepted else "JINGTING_PROVIDER_NOT_AGY",
            evidence=lane_evidence,
        ),
        ProvenanceCheck(
            code="JINGTING_AGY_SUCCESS",
            passed=execution_succeeded,
            severity="BLOCK",
            reason_code=None if execution_succeeded else "JINGTING_AGY_FAILED",
            evidence={"agy_rc": provenance.agy_rc, "lane": lane},
        ),
        ProvenanceCheck(
            code="JINGTING_MODEL_RECORDED",
            passed=model_passed,
            severity="BLOCK",
            reason_code=model_reason,
            evidence={"model": provenance.model, "lane": lane},
        ),
        ProvenanceCheck(
            code="JINGTING_PROVIDER_FALLBACK_NOT_USED",
            passed=fallback_accepted,
            severity="BLOCK",
            reason_code=(
                None
                if fallback_accepted
                else _fallback_reason_code(provenance.provider_fallback_used, lane=lane)
            ),
            evidence={"provider_fallback_used": provenance.provider_fallback_used, "lane": lane},
        ),
    )


def _provenance_reason_codes(checks: Sequence[ProvenanceCheck]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(check.reason_code for check in checks if not check.passed and check.reason_code))


def _evidence_check(code: str, passed: bool, reason_code: str, evidence: Mapping[str, object]) -> ProvenanceCheck:
    return ProvenanceCheck(
        code=code,
        passed=passed,
        severity="BLOCK",
        reason_code=None if passed else reason_code,
        evidence=evidence,
    )


def _decision_score(candidate: CandidateReview) -> float:
    return candidate.editorial_score if candidate.editorial_score is not None else 0.0


def _fallback_reason_code(
    provider_fallback_used: bool | None, *, lane: str = JINGTING_LANE_AGY
) -> str | None:
    if lane == JINGTING_LANE_GEMINI_API_FALLBACK:
        # This lane REQUIRES the flag to be True.  Anything else means the
        # manifest did not honestly say which quota layer ran.
        return None if provider_fallback_used is True else "JINGTING_PROVIDER_FALLBACK_UNKNOWN"
    if provider_fallback_used is False:
        return None
    if provider_fallback_used is True:
        return "JINGTING_PROVIDER_FALLBACK_USED"
    return "JINGTING_PROVIDER_FALLBACK_UNKNOWN"


def _optional_str(value: object) -> str | None:
    if isinstance(value, str):
        return value
    return None


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _optional_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    return None
