from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Mapping, Sequence

from src.autoslice.review_evidence import ReviewEvidence


CPA_SEMANTIC_QA_REQUEST_SCHEMA_VERSION = "cpa-semantic-review-request.v1"
CPA_SEMANTIC_QA_RESPONSE_SCHEMA_VERSION = "cpa-semantic-review-response.v1"
DEFAULT_CHECKS_REQUESTED: tuple[str, ...] = (
    "semantic_completeness",
    "context_dependency",
    "terminology_correctness",
    "title_hook_quality",
    "unsafe_upload_risk",
    "viewer_context_completeness",
)
CONNECTIVE_PREFIXES: tuple[str, ...] = ("然后", "所以", "但是", "因为", "结果", "接着", "后来", "而且", "不过")
ALLOWED_REQUEST_CHECKS = frozenset(DEFAULT_CHECKS_REQUESTED)
COMPLETE_SONG_SEMANTIC_WAIVED_REASONS = frozenset(
    (
        "CPA_SEMANTIC_INCOMPLETE",
        "CONTEXT_DEPENDENCY_HIGH",
        "NOT_INTERESTING",
        "CPA_RELEASE_NOT_READY",
        "CPA_REQUIRED_FIXES_MISSING",
        "CPA_REASON_CODES_MISSING",
        "VIEWER_CONTEXT_INCOMPLETE",
    )
)
# Backward-compatible alias for callers/tests that used the more verbose name.
COMPLETE_SONG_SEMANTIC_WAIVED_REASON_CODES = COMPLETE_SONG_SEMANTIC_WAIVED_REASONS


@dataclass(frozen=True)
class CpaSemanticSourceRef:
    video_path: str
    srt_path: str
    start_ms: int
    end_ms: int
    source_cues_path: str | None = None
    review_evidence_path: str | None = None

    def to_dict(self) -> dict[str, object]:
        data: dict[str, object] = {
            "video_path": self.video_path,
            "srt_path": self.srt_path,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
        }
        if self.source_cues_path is not None:
            data["source_cues_path"] = self.source_cues_path
        if self.review_evidence_path is not None:
            data["review_evidence_path"] = self.review_evidence_path
        return data


@dataclass(frozen=True)
class CpaTerminologyContext:
    schema_version: str = "lidousha-terminology.v1"
    applied_terms: Sequence[str] = ()
    evidence_paths: Sequence[str] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "applied_terms": list(self.applied_terms),
            "evidence_paths": list(self.evidence_paths),
        }


@dataclass(frozen=True)
class CpaSemanticQaRequest:
    candidate_id: str
    room_id: str
    source: CpaSemanticSourceRef
    candidate_text: str
    normalized_text: str
    response_path: str
    request_path: str | None = None
    terminology: CpaTerminologyContext = field(default_factory=CpaTerminologyContext)
    checks_requested: Sequence[str] = DEFAULT_CHECKS_REQUESTED
    metadata: Mapping[str, object] = field(default_factory=dict)
    schema_version: str = CPA_SEMANTIC_QA_REQUEST_SCHEMA_VERSION

    def to_dict(self) -> dict[str, object]:
        artifact_paths = {
            "response_path": self.response_path,
            "request_path": self.request_path,
        }
        return {
            "schema_version": self.schema_version,
            "candidate_id": self.candidate_id,
            "room_id": self.room_id,
            "source": self.source.to_dict(),
            "candidate_text": self.candidate_text,
            "normalized_text": self.normalized_text,
            "terminology": self.terminology.to_dict(),
            "checks_requested": list(dict.fromkeys(self.checks_requested)),
            "artifact_paths": artifact_paths,
            "metadata": dict(self.metadata),
        }

    def canonical_bytes(self) -> bytes:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")

    def canonical_sha256(self) -> str:
        return "sha256:" + hashlib.sha256(self.canonical_bytes()).hexdigest()


@dataclass(frozen=True)
class CpaSemanticQaResponse:
    candidate_id: str
    request_sha256: str
    release_ready: bool
    semantic_complete: bool
    terminology_ok: bool
    title_hook_score: float
    context_dependency_score: float
    unsafe_upload_risk_score: float
    reason_codes: Sequence[str]
    required_fixes: Sequence[str]
    summary: str
    response_path: str
    request_path: str | None = None
    provider: str = "mock-local-cpa"
    provider_request_id: str | None = None
    terminology_findings: Sequence[Mapping[str, object]] = ()
    semantic_findings: Sequence[str] = ()
    metadata: Mapping[str, object] = field(default_factory=dict)
    schema_version: str = CPA_SEMANTIC_QA_RESPONSE_SCHEMA_VERSION

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "candidate_id": self.candidate_id,
            "request_sha256": self.request_sha256,
            "release_ready": self.release_ready,
            "semantic_complete": self.semantic_complete,
            "terminology_ok": self.terminology_ok,
            "title_hook_score": self.title_hook_score,
            "context_dependency_score": self.context_dependency_score,
            "unsafe_upload_risk_score": self.unsafe_upload_risk_score,
            "reason_codes": list(dict.fromkeys(self.reason_codes)),
            "required_fixes": list(dict.fromkeys(self.required_fixes)),
            "provider": {
                "name": self.provider,
                "request_id": self.provider_request_id,
            },
            "artifact_paths": {
                "request_path": self.request_path,
                "response_path": self.response_path,
            },
            "evidence": {
                "summary": self.summary,
                "terminology_findings": [dict(item) for item in self.terminology_findings],
                "semantic_findings": list(self.semantic_findings),
            },
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class CpaSemanticQaEvaluation:
    candidate_id: str
    passed: bool
    reason_codes: tuple[str, ...]
    check: Mapping[str, object]
    metadata: Mapping[str, object]
    response: Mapping[str, object] | None = None


def validate_cpa_semantic_request_payload(payload: Mapping[str, object]) -> tuple[str, ...]:
    reasons: list[str] = []
    if str(payload.get("schema_version") or "") != CPA_SEMANTIC_QA_REQUEST_SCHEMA_VERSION:
        reasons.append("CPA_SEMANTIC_QA_REQUEST_SCHEMA_MISMATCH")
    if not _non_empty_str(payload.get("candidate_id")):
        reasons.append("CPA_SEMANTIC_QA_REQUEST_CANDIDATE_ID_MISSING")
    if not _non_empty_str(payload.get("room_id")):
        reasons.append("CPA_SEMANTIC_QA_REQUEST_ROOM_ID_MISSING")

    source = _mapping(payload.get("source"))
    if not source:
        reasons.append("CPA_SEMANTIC_QA_REQUEST_SOURCE_MISSING")
    else:
        if not _non_empty_str(source.get("video_path")):
            reasons.append("CPA_SEMANTIC_QA_REQUEST_VIDEO_PATH_MISSING")
        if not _non_empty_str(source.get("srt_path")):
            reasons.append("CPA_SEMANTIC_QA_REQUEST_SRT_PATH_MISSING")
        start_ms = _optional_int(source.get("start_ms"))
        end_ms = _optional_int(source.get("end_ms"))
        if start_ms is None:
            reasons.append("CPA_SEMANTIC_QA_REQUEST_START_MS_MISSING")
        if end_ms is None:
            reasons.append("CPA_SEMANTIC_QA_REQUEST_END_MS_MISSING")
        if start_ms is not None and end_ms is not None and end_ms <= start_ms:
            reasons.append("CPA_SEMANTIC_QA_REQUEST_RANGE_INVALID")

    if not _non_empty_str(payload.get("candidate_text")):
        reasons.append("CPA_SEMANTIC_QA_REQUEST_TEXT_MISSING")
    if not _non_empty_str(payload.get("normalized_text")):
        reasons.append("CPA_SEMANTIC_QA_REQUEST_NORMALIZED_TEXT_MISSING")

    checks_requested = payload.get("checks_requested")
    if not isinstance(checks_requested, Sequence) or isinstance(checks_requested, (str, bytes)) or not checks_requested:
        reasons.append("CPA_SEMANTIC_QA_REQUEST_CHECKS_MISSING")
    else:
        invalid = [str(item) for item in checks_requested if str(item) not in ALLOWED_REQUEST_CHECKS]
        if invalid:
            reasons.append("CPA_SEMANTIC_QA_REQUEST_CHECK_UNKNOWN")

    artifact_paths = _mapping(payload.get("artifact_paths"))
    if not artifact_paths or not _non_empty_str(artifact_paths.get("response_path")):
        reasons.append("CPA_SEMANTIC_QA_REQUEST_RESPONSE_PATH_MISSING")
    return tuple(dict.fromkeys(reasons))


def write_cpa_semantic_request_artifact(request: CpaSemanticQaRequest, path: Path) -> CpaSemanticQaRequest:
    actual = replace(request, request_path=str(path))
    payload = _request_payload_with_expected_hash(actual)
    errors = validate_cpa_semantic_request_payload(payload)
    if errors:
        raise ValueError(", ".join(errors))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return actual


def _request_payload_with_expected_hash(request: CpaSemanticQaRequest) -> dict[str, object]:
    payload = request.to_dict()
    # Convenience for external CPA scripts: echo this exact value in the
    # response `request_sha256`.  The canonical hash intentionally excludes
    # this helper field; load_request_artifact ignores it and recomputes from
    # the stable request fields.
    payload["request_sha256"] = request.canonical_sha256()
    payload["response_contract"] = {"expected_request_sha256": request.canonical_sha256()}
    return payload


def write_cpa_semantic_response_artifact(response: CpaSemanticQaResponse, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(response.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_request_artifact(path: Path) -> CpaSemanticQaRequest:
    payload = _load_json(path)
    errors = validate_cpa_semantic_request_payload(payload)
    if errors:
        raise ValueError(", ".join(errors))

    source = _mapping(payload["source"])
    terminology = _mapping(payload.get("terminology"))
    artifact_paths = _mapping(payload.get("artifact_paths"))
    return CpaSemanticQaRequest(
        candidate_id=str(payload["candidate_id"]),
        room_id=str(payload["room_id"]),
        source=CpaSemanticSourceRef(
            video_path=str(source["video_path"]),
            srt_path=str(source["srt_path"]),
            start_ms=int(source["start_ms"]),
            end_ms=int(source["end_ms"]),
            source_cues_path=_optional_str(source.get("source_cues_path")),
            review_evidence_path=_optional_str(source.get("review_evidence_path")),
        ),
        candidate_text=str(payload["candidate_text"]),
        normalized_text=str(payload["normalized_text"]),
        response_path=str(artifact_paths["response_path"]),
        request_path=_optional_str(artifact_paths.get("request_path")) or str(path),
        terminology=CpaTerminologyContext(
            schema_version=str(terminology.get("schema_version") or "lidousha-terminology.v1"),
            applied_terms=tuple(str(item) for item in _sequence(terminology.get("applied_terms"))),
            evidence_paths=tuple(str(item) for item in _sequence(terminology.get("evidence_paths"))),
        ),
        checks_requested=tuple(str(item) for item in _sequence(payload.get("checks_requested"))),
        metadata=dict(_mapping(payload.get("metadata"))),
    )


def validate_cpa_semantic_response_payload(
    payload: Mapping[str, object],
    *,
    request: CpaSemanticQaRequest,
    response_path: Path | None = None,
) -> tuple[str, ...]:
    reasons: list[str] = []
    if str(payload.get("schema_version") or "") != CPA_SEMANTIC_QA_RESPONSE_SCHEMA_VERSION:
        reasons.append("CPA_SEMANTIC_QA_SCHEMA_MISMATCH")
    if str(payload.get("candidate_id") or "") != request.candidate_id:
        reasons.append("CPA_SEMANTIC_QA_CANDIDATE_MISMATCH")
    if str(payload.get("request_sha256") or "") != request.canonical_sha256():
        reasons.append("CPA_SEMANTIC_QA_REQUEST_SHA256_MISMATCH")

    release_ready = payload.get("release_ready")
    semantic_complete = payload.get("semantic_complete")
    terminology_ok = payload.get("terminology_ok")
    if not isinstance(release_ready, bool):
        reasons.append("CPA_SEMANTIC_QA_RELEASE_READY_INVALID")
    if not isinstance(semantic_complete, bool):
        reasons.append("CPA_SEMANTIC_QA_SEMANTIC_COMPLETE_INVALID")
    if not isinstance(terminology_ok, bool):
        reasons.append("CPA_SEMANTIC_QA_TERMINOLOGY_OK_INVALID")

    for field_name in ("title_hook_score", "context_dependency_score", "unsafe_upload_risk_score"):
        value = payload.get(field_name)
        if not isinstance(value, (int, float)):
            reasons.append(f"CPA_SEMANTIC_QA_{field_name.upper()}_INVALID")
            continue
        if float(value) < 0.0 or float(value) > 1.0:
            reasons.append(f"CPA_SEMANTIC_QA_{field_name.upper()}_OUT_OF_RANGE")

    if not _non_empty_str(_mapping(payload.get("evidence")).get("summary")):
        reasons.append("CPA_SEMANTIC_QA_SUMMARY_MISSING")

    artifact_paths = _mapping(payload.get("artifact_paths"))
    if not artifact_paths:
        reasons.append("CPA_SEMANTIC_QA_ARTIFACT_PATHS_MISSING")
    else:
        if str(artifact_paths.get("request_path") or "") != str(request.request_path or ""):
            reasons.append("CPA_SEMANTIC_QA_REQUEST_PATH_MISMATCH")
        expected_response_path = str(response_path or request.response_path)
        if str(artifact_paths.get("response_path") or "") != expected_response_path:
            reasons.append("CPA_SEMANTIC_QA_RESPONSE_PATH_MISMATCH")

    raw_reason_codes = payload.get("reason_codes")
    if not isinstance(raw_reason_codes, Sequence) or isinstance(raw_reason_codes, (str, bytes)):
        reasons.append("CPA_SEMANTIC_QA_REASON_CODES_INVALID")
    raw_required_fixes = payload.get("required_fixes")
    if not isinstance(raw_required_fixes, Sequence) or isinstance(raw_required_fixes, (str, bytes)):
        reasons.append("CPA_SEMANTIC_QA_REQUIRED_FIXES_INVALID")

    return tuple(dict.fromkeys(reasons))


def evaluate_cpa_semantic_response_artifact(request: CpaSemanticQaRequest, response_path: Path) -> CpaSemanticQaEvaluation:
    if not response_path.is_file():
        return _evaluation_failure(
            candidate_id=request.candidate_id,
            reason_codes=("CPA_SEMANTIC_QA_MISSING",),
            request=request,
            response_path=response_path,
            response=None,
        )

    try:
        payload = _load_json(response_path)
    except json.JSONDecodeError:
        return _evaluation_failure(
            candidate_id=request.candidate_id,
            reason_codes=("CPA_SEMANTIC_QA_INVALID_JSON",),
            request=request,
            response_path=response_path,
            response=None,
        )

    validation_errors = validate_cpa_semantic_response_payload(payload, request=request, response_path=response_path)
    if validation_errors:
        return _evaluation_failure(
            candidate_id=request.candidate_id,
            reason_codes=validation_errors,
            request=request,
            response_path=response_path,
            response=payload,
        )

    reason_codes = _derive_response_reason_codes(payload)
    passed = not reason_codes
    check = {
        "code": "CPA_SEMANTIC_QA",
        "pass": passed,
        "severity": "PASS" if passed else "BLOCK",
        "reason_codes": list(reason_codes),
        "evidence": {
            "request_path": str(request.request_path),
            "response_path": str(response_path),
            "request_sha256": request.canonical_sha256(),
            "release_ready": payload["release_ready"],
            "semantic_complete": payload["semantic_complete"],
            "terminology_ok": payload["terminology_ok"],
            "title_hook_score": float(payload["title_hook_score"]),
            "context_dependency_score": float(payload["context_dependency_score"]),
            "unsafe_upload_risk_score": float(payload["unsafe_upload_risk_score"]),
            "summary": _mapping(payload.get("evidence")).get("summary"),
            "provider": _mapping(payload.get("provider")).get("name"),
            "viewer_context": dict(_mapping(_mapping(payload.get("metadata")).get("viewer_context"))),
        },
    }
    metadata = {
        "request_path": str(request.request_path),
        "response_path": str(response_path),
        "request_sha256": request.canonical_sha256(),
        "response_reason_codes": list(reason_codes),
        "response_provider": _mapping(payload.get("provider")),
        "response_evidence": _mapping(payload.get("evidence")),
    }
    return CpaSemanticQaEvaluation(
        candidate_id=request.candidate_id,
        passed=passed,
        reason_codes=tuple(reason_codes),
        check=check,
        metadata=metadata,
        response=payload,
    )


def apply_cpa_semantic_qa_to_review_evidence(
    evidence: ReviewEvidence,
    evaluation: CpaSemanticQaEvaluation,
) -> ReviewEvidence:
    evaluation = _apply_complete_song_semantic_policy(evidence, evaluation)
    checks = tuple(evidence.checks) + (dict(evaluation.check),)
    metadata = dict(evidence.metadata)
    metadata["cpa_semantic_qa"] = dict(evaluation.metadata)
    evidence_gaps = tuple(dict.fromkeys(tuple(evidence.evidence_gaps) + tuple(evaluation.reason_codes)))
    return replace(evidence, checks=checks, metadata=metadata, evidence_gaps=evidence_gaps)


def _apply_complete_song_semantic_policy(
    evidence: ReviewEvidence,
    evaluation: CpaSemanticQaEvaluation,
) -> CpaSemanticQaEvaluation:
    """Verified complete songs are form-gated, not "boring chat" gated.

    CPA still owns terminology and upload-risk checks, but once the machine
    evidence proves a foreground song is complete and lyric-aligned, semantic
    reasons that only say "not interesting", "context-dependent", or generic
    "not release ready" must not block the slice.  This encodes Ivan's product
    rule: a complete song slice is not classified as boring/no-hook content.
    """

    if not _complete_song_ready(evidence):
        return evaluation
    original_reasons = tuple(evaluation.reason_codes)
    waived = tuple(reason for reason in original_reasons if reason in COMPLETE_SONG_SEMANTIC_WAIVED_REASONS)
    if not waived:
        return evaluation
    remaining = tuple(reason for reason in original_reasons if reason not in COMPLETE_SONG_SEMANTIC_WAIVED_REASONS)

    check = dict(evaluation.check)
    check["pass"] = not remaining
    check["severity"] = "PASS" if not remaining else "BLOCK"
    check["reason_codes"] = list(remaining)
    check_evidence = dict(_mapping(check.get("evidence")))
    check_evidence["complete_song_policy"] = {
        "applied": True,
        "waived_reason_codes": list(waived),
        "remaining_reason_codes": list(remaining),
        "basis": _complete_song_policy_basis(evidence),
        "rules": _complete_song_policy_rules(),
    }
    check["evidence"] = check_evidence

    metadata = dict(evaluation.metadata)
    metadata["raw_response_reason_codes"] = list(original_reasons)
    metadata["response_reason_codes"] = list(remaining)
    metadata["complete_song_policy"] = {
        "applied": True,
        "waived_reason_codes": list(waived),
        "remaining_reason_codes": list(remaining),
        "basis": _complete_song_policy_basis(evidence),
        "rules": _complete_song_policy_rules(),
    }
    return replace(evaluation, passed=not remaining, reason_codes=remaining, check=check, metadata=metadata)


def _complete_song_ready(evidence: ReviewEvidence) -> bool:
    return (
        evidence.foreground_song_overlap_seconds is not None
        and evidence.foreground_song_overlap_seconds > 5.0
        and evidence.song_complete is True
        and evidence.lyrics_alignment_ready is True
    )


def _complete_song_policy_basis(evidence: ReviewEvidence) -> dict[str, object]:
    return {
        "foreground_song_overlap_seconds": evidence.foreground_song_overlap_seconds,
        "song_complete": evidence.song_complete,
        "lyrics_alignment_ready": evidence.lyrics_alignment_ready,
    }


def _complete_song_policy_rules() -> dict[str, object]:
    return {"complete_song_semantic_waived_reasons": sorted(COMPLETE_SONG_SEMANTIC_WAIVED_REASONS)}


def build_mock_cpa_response(request: CpaSemanticQaRequest) -> CpaSemanticQaResponse:
    normalized = request.normalized_text.strip()
    semantic_findings: list[str] = []
    terminology_findings: list[Mapping[str, object]] = []
    reason_codes: list[str] = []
    required_fixes: list[str] = []

    semantic_complete = True
    if normalized.startswith(CONNECTIVE_PREFIXES) or normalized.endswith(("然后", "结果", "后面", "等等")):
        semantic_complete = False
        semantic_findings.append("candidate starts/ends like an unresolved fragment")
        reason_codes.append("CPA_SEMANTIC_INCOMPLETE")
        required_fixes.append("expand candidate to include setup and closure")

    context_dependency_score = 0.18
    if normalized.startswith(CONNECTIVE_PREFIXES):
        context_dependency_score = 0.72
    elif any(token in normalized[:12] for token in ("这个", "那个", "她", "他")):
        context_dependency_score = 0.55
    if context_dependency_score > 0.45:
        reason_codes.append("CONTEXT_DEPENDENCY_HIGH")

    terminology_ok = True
    if "天不熊" in request.candidate_text and "kmx" not in normalized:
        terminology_ok = False
        terminology_findings.append(
            {
                "term": "kmx",
                "issue": "alias unresolved",
                "observed": "天不熊",
            }
        )
        reason_codes.append("TERMINOLOGY_QA_FAILED")
        required_fixes.append("normalize 天不熊/kimo熊 to kmx using approved lexicon")

    title_hook_score = 0.86 if any(token in normalized for token in ("笑", "哭", "绷", "赢", "骗", "反转", "kmx")) else 0.62
    unsafe_upload_risk_score = 0.10

    release_ready = semantic_complete and terminology_ok and context_dependency_score <= 0.45 and unsafe_upload_risk_score <= 0.35
    summary = "mock-local CPA semantic QA pass" if release_ready else "mock-local CPA semantic QA found blocking semantic issues"
    if not release_ready and not reason_codes:
        reason_codes.append("CPA_RELEASE_NOT_READY")

    return CpaSemanticQaResponse(
        candidate_id=request.candidate_id,
        request_sha256=request.canonical_sha256(),
        release_ready=release_ready,
        semantic_complete=semantic_complete,
        terminology_ok=terminology_ok,
        title_hook_score=title_hook_score,
        context_dependency_score=context_dependency_score,
        unsafe_upload_risk_score=unsafe_upload_risk_score,
        reason_codes=tuple(dict.fromkeys(reason_codes)),
        required_fixes=tuple(dict.fromkeys(required_fixes)),
        summary=summary,
        request_path=request.request_path,
        response_path=request.response_path,
        terminology_findings=tuple(terminology_findings),
        semantic_findings=tuple(semantic_findings),
        metadata={"mode": "mock-local"},
    )


def _derive_response_reason_codes(payload: Mapping[str, object]) -> tuple[str, ...]:
    reasons = [str(item) for item in _sequence(payload.get("reason_codes")) if str(item)]
    required_fixes = [str(item) for item in _sequence(payload.get("required_fixes")) if str(item)]

    semantic_complete = bool(payload.get("semantic_complete"))
    terminology_ok = bool(payload.get("terminology_ok"))
    release_ready = bool(payload.get("release_ready"))
    context_dependency_score = float(payload.get("context_dependency_score"))
    unsafe_upload_risk_score = float(payload.get("unsafe_upload_risk_score"))

    if not semantic_complete:
        reasons.append("CPA_SEMANTIC_INCOMPLETE")
    if not terminology_ok:
        reasons.append("TERMINOLOGY_QA_FAILED")
    if context_dependency_score > 0.45:
        reasons.append("CONTEXT_DEPENDENCY_HIGH")
    if unsafe_upload_risk_score > 0.35:
        reasons.append("CPA_UNSAFE_UPLOAD_RISK_HIGH")
    if not release_ready:
        reasons.append("CPA_RELEASE_NOT_READY")
    if not release_ready and not required_fixes:
        reasons.append("CPA_REQUIRED_FIXES_MISSING")
    if not release_ready and not _sequence(payload.get("reason_codes")):
        reasons.append("CPA_REASON_CODES_MISSING")
    return tuple(dict.fromkeys(reasons))


def _evaluation_failure(
    *,
    candidate_id: str,
    reason_codes: Sequence[str],
    request: CpaSemanticQaRequest,
    response_path: Path,
    response: Mapping[str, object] | None,
) -> CpaSemanticQaEvaluation:
    check = {
        "code": "CPA_SEMANTIC_QA",
        "pass": False,
        "severity": "BLOCK",
        "reason_codes": list(dict.fromkeys(reason_codes)),
        "evidence": {
            "request_path": str(request.request_path),
            "response_path": str(response_path),
            "request_sha256": request.canonical_sha256(),
        },
    }
    metadata = {
        "request_path": str(request.request_path),
        "response_path": str(response_path),
        "request_sha256": request.canonical_sha256(),
        "response_reason_codes": list(dict.fromkeys(reason_codes)),
    }
    return CpaSemanticQaEvaluation(
        candidate_id=candidate_id,
        passed=False,
        reason_codes=tuple(dict.fromkeys(reason_codes)),
        check=check,
        metadata=metadata,
        response=response,
    )


def _load_json(path: Path) -> Mapping[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("JSON payload must be an object")
    return payload


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: object) -> Sequence[object]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return value
    return ()


def _non_empty_str(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value != "" else None


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None
