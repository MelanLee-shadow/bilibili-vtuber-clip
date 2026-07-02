from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from src.autoslice.review_evidence import ReviewEvidence


CPA_SEMANTIC_REVIEW_SCHEMA_VERSION = "cpa-semantic-review-response.v1"


class CPASemanticReviewError(ValueError):
    def __init__(self, reason_code: str, message: str):
        super().__init__(message)
        self.reason_code = reason_code


@dataclass(frozen=True)
class CPASemanticReviewResponse:
    candidate_id: str
    release_ready: bool
    semantic_complete: bool
    terminology_ok: bool
    reason_codes: tuple[str, ...] = ()
    required_fixes: tuple[str, ...] = ()
    title_hook_score: float | None = None
    context_dependency_score: float | None = None
    evidence: Mapping[str, object] | None = None
    schema_version: str = CPA_SEMANTIC_REVIEW_SCHEMA_VERSION

    @classmethod
    def from_mapping(cls, data: Mapping[str, object], *, expected_candidate_id: str | None = None) -> "CPASemanticReviewResponse":
        schema_version = data.get("schema_version")
        if schema_version != CPA_SEMANTIC_REVIEW_SCHEMA_VERSION:
            raise CPASemanticReviewError(
                "CPA_SEMANTIC_QA_SCHEMA_MISMATCH",
                f"Expected schema_version={CPA_SEMANTIC_REVIEW_SCHEMA_VERSION!r}, got {schema_version!r}",
            )
        candidate_id = data.get("candidate_id")
        if not isinstance(candidate_id, str) or not candidate_id:
            raise CPASemanticReviewError("CPA_SEMANTIC_QA_SCHEMA_MISMATCH", "candidate_id must be a non-empty string")
        if expected_candidate_id is not None and candidate_id != expected_candidate_id:
            raise CPASemanticReviewError(
                "CPA_SEMANTIC_QA_CANDIDATE_MISMATCH",
                f"Expected candidate_id={expected_candidate_id!r}, got {candidate_id!r}",
            )
        release_ready = _required_bool(data, "release_ready")
        semantic_complete = _required_bool(data, "semantic_complete")
        terminology_ok = _required_bool(data, "terminology_ok")
        reason_codes = _string_tuple(data.get("reason_codes"), field_name="reason_codes")
        required_fixes = _string_tuple(data.get("required_fixes"), field_name="required_fixes")
        evidence = data.get("evidence")
        if evidence is not None and not isinstance(evidence, Mapping):
            raise CPASemanticReviewError("CPA_SEMANTIC_QA_SCHEMA_MISMATCH", "evidence must be an object when present")
        return cls(
            candidate_id=candidate_id,
            release_ready=release_ready,
            semantic_complete=semantic_complete,
            terminology_ok=terminology_ok,
            title_hook_score=_optional_float(data.get("title_hook_score")),
            context_dependency_score=_optional_float(data.get("context_dependency_score")),
            reason_codes=reason_codes,
            required_fixes=required_fixes,
            evidence=dict(evidence or {}),
        )

    @property
    def passed(self) -> bool:
        return self.release_ready and self.semantic_complete and self.terminology_ok and not self.reason_codes

    def primary_reason_code(self) -> str | None:
        if self.reason_codes:
            return self.reason_codes[0]
        if not self.terminology_ok:
            return "TERMINOLOGY_QA_FAILED"
        if not self.semantic_complete:
            return "CPA_SEMANTIC_INCOMPLETE"
        if not self.release_ready:
            return "CPA_SEMANTIC_NOT_RELEASE_READY"
        return None

    def to_manifest(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "candidate_id": self.candidate_id,
            "release_ready": self.release_ready,
            "semantic_complete": self.semantic_complete,
            "terminology_ok": self.terminology_ok,
            "title_hook_score": self.title_hook_score,
            "context_dependency_score": self.context_dependency_score,
            "reason_codes": list(self.reason_codes),
            "required_fixes": list(self.required_fixes),
            "evidence": dict(self.evidence or {}),
        }


def load_cpa_semantic_review_response(path: Path | str, *, expected_candidate_id: str | None = None) -> CPASemanticReviewResponse:
    response_path = Path(path)
    if not response_path.is_file():
        raise CPASemanticReviewError("CPA_SEMANTIC_QA_MISSING", f"CPA semantic review response missing: {response_path}")
    try:
        payload = json.loads(response_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CPASemanticReviewError("CPA_SEMANTIC_QA_INVALID_JSON", f"Invalid CPA semantic review JSON: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise CPASemanticReviewError("CPA_SEMANTIC_QA_SCHEMA_MISMATCH", "CPA response root must be an object")
    return CPASemanticReviewResponse.from_mapping(payload, expected_candidate_id=expected_candidate_id)


def apply_cpa_semantic_review(
    evidence: ReviewEvidence,
    response: CPASemanticReviewResponse,
    *,
    response_path: Path | str,
) -> ReviewEvidence:
    reason_code = response.primary_reason_code()
    check: dict[str, object] = {
        "code": "CPA_SEMANTIC_QA",
        "pass": response.passed,
        "severity": "PASS" if response.passed else "BLOCK",
        "evidence": {
            "response_path": str(response_path),
            "semantic_complete": response.semantic_complete,
            "terminology_ok": response.terminology_ok,
            "title_hook_score": response.title_hook_score,
            "context_dependency_score": response.context_dependency_score,
            "required_fixes": list(response.required_fixes),
            "cpa_evidence": dict(response.evidence or {}),
        },
    }
    if reason_code:
        check["reason_code"] = reason_code

    metadata = dict(evidence.metadata)
    metadata["cpa_semantic_review"] = {
        "response_path": str(response_path),
        "schema_version": response.schema_version,
        "release_ready": response.release_ready,
        "semantic_complete": response.semantic_complete,
        "terminology_ok": response.terminology_ok,
        "reason_codes": list(response.reason_codes),
    }
    evidence_gaps = list(evidence.evidence_gaps)
    if reason_code and reason_code not in evidence_gaps:
        evidence_gaps.append(reason_code)
    updates: dict[str, object] = {
        "checks": tuple(list(evidence.checks) + [check]),
        "metadata": metadata,
        "evidence_gaps": tuple(evidence_gaps),
    }
    if response.semantic_complete is False:
        updates["open_loop_count"] = max(1, evidence.open_loop_count or 0)
    return evidence.__class__(**{**evidence.__dict__, **updates})


def _required_bool(data: Mapping[str, object], field_name: str) -> bool:
    value = data.get(field_name)
    if not isinstance(value, bool):
        raise CPASemanticReviewError("CPA_SEMANTIC_QA_SCHEMA_MISMATCH", f"{field_name} must be boolean")
    return value


def _string_tuple(value: object, *, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise CPASemanticReviewError("CPA_SEMANTIC_QA_SCHEMA_MISMATCH", f"{field_name} must be an array of strings")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise CPASemanticReviewError("CPA_SEMANTIC_QA_SCHEMA_MISMATCH", f"{field_name} must be an array of strings")
        result.append(item)
    return tuple(result)


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CPASemanticReviewError("CPA_SEMANTIC_QA_SCHEMA_MISMATCH", "score fields must be numbers when present")
    return float(value)
