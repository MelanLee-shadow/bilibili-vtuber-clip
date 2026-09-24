"""Read-only diagnostics for recorded Jev responses, never an acceptance gate.

Mirror the existing experiment's numeric rules, including its strict sum
threshold. Separating a row's error from collateral request rejection neither
normalizes probabilities nor authorizes a partial response or subtitle edit.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
import math
from typing import Any

LEGACY_SUM_TOLERANCE = 0.0001
LEGACY_ARGMAX_TOLERANCE = 1e-9


def _probability(value: Any) -> bool:
    # Checking the range first also handles arbitrarily large JSON integers.
    return type(value) in (int, float) and 0 <= value <= 1 and math.isfinite(value)


def _row(question: Any, answer: Any, *, present: bool) -> dict[str, Any]:
    issues: list[str] = []
    result: dict[str, Any] = {
        "answer_present": present,
        "issues": issues,
        "probability_sum": None,
        "probability_sum_decimal": None,
        "distribution_detail": None,
        "choice": answer.get("choice")
        if isinstance(answer, Mapping) and isinstance(answer.get("choice"), str)
        else None,
        "automatic_action_eligible": False,
    }
    if not present:
        issues.append("ANSWER_COVERAGE_INVALID")
    elif not isinstance(question, Mapping) or not isinstance(answer, Mapping):
        issues.append("ANSWER_TYPE_INVALID")
    elif answer.get("type") != question.get("type"):
        issues.append("ANSWER_TYPE_INVALID")
    elif question.get("type") == "noul":
        if not _probability(answer.get("noul")):
            issues.append("NOUL_INVALID")
    elif question.get("type") != "choice":
        issues.append("UNIMPLEMENTED_PRIMITIVE")
    else:
        ps, criteria = answer.get("probabilities"), question.get("criteria")
        if (
            not isinstance(ps, Mapping)
            or not isinstance(criteria, Mapping)
            or not criteria
            or set(ps) != set(criteria)
        ):
            issues.append("OPTION_COVERAGE_INVALID")
        elif not all(_probability(value) for value in ps.values()):
            issues.append("DISTRIBUTION_INVALID")
            result["distribution_detail"] = "NONFINITE_OUT_OF_RANGE_OR_NONNUMERIC"
        else:
            total = sum(ps.values())
            result["probability_sum"] = total
            result["probability_sum_decimal"] = str(
                sum((Decimal(str(v)) for v in ps.values()), Decimal(0))
            )
            if abs(total - 1) > LEGACY_SUM_TOLERANCE:
                issues.append("DISTRIBUTION_INVALID")
                result["distribution_detail"] = "SUM_OUTSIDE_LEGACY_TOLERANCE"
            choice = answer.get("choice")
            if (
                not isinstance(choice, str)
                or choice not in ps
                or ps[choice] + LEGACY_ARGMAX_TOLERANCE < max(ps.values())
            ):
                issues.append("ARGMAX_INVALID")
        if not _probability(answer.get("confidence")):
            issues.append("CONFIDENCE_INVALID")
    result["individually_matches_numeric_contract"] = not issues
    return result


def diagnose_response(request: Any, response: Any, *, expected_model: str) -> dict[str, Any]:
    """Report every requested answer while preserving the whole-request failure.

    `expected_model` must come from the caller's frozen experiment configuration.
    This function does not interpret source facts, labels, or model confidence as
    truth. Returned numeric diagnostics never authorize automatic consumption.
    """
    if not isinstance(expected_model, str) or not expected_model.strip():
        raise ValueError("EXPECTED_MODEL_REQUIRED")
    global_issues: list[str] = []
    if (
        not isinstance(request, Mapping)
        or not isinstance(request.get("questions"), Mapping)
        or not request["questions"]
    ):
        global_issues.append("REQUEST_INPUT_INVALID")
        questions = {}
    else:
        questions = request["questions"]
    if not isinstance(response, Mapping):
        global_issues.append("RESPONSE_ROOT_INVALID")
        response = {}
    if response.get("model") != expected_model:
        global_issues.append("MODEL_ID_MISMATCH")
    answers = response.get("answers")
    if not isinstance(answers, Mapping):
        answers = {}
        global_issues.append("ANSWER_COVERAGE_INVALID")
    elif set(answers) != set(questions):
        global_issues.append("ANSWER_COVERAGE_INVALID")
    usage = response.get("usage")
    if not isinstance(usage, Mapping) or any(
        type(usage.get(k)) is not int or usage[k] < 0 for k in ("input_tokens", "output_tokens")
    ):
        global_issues.append("USAGE_INVALID")
    rows = {
        key: _row(question, answers.get(key), present=key in answers)
        for key, question in questions.items()
    }
    numeric_pass = not global_issues and all(
        row["individually_matches_numeric_contract"] for row in rows.values()
    )
    valid_count = sum(row["individually_matches_numeric_contract"] for row in rows.values())
    for row in rows.values():
        row["would_be_dropped_with_request"] = (
            not numeric_pass and row["individually_matches_numeric_contract"]
        )
    return {
        "schema_version": "jev-response-diagnostic.v1",
        "status": "DIAGNOSTIC_ONLY",
        "expected_model": expected_model,
        "request_model": request.get("model") if isinstance(request, Mapping) else None,
        "response_model": response.get("model") if isinstance(response.get("model"), str) else None,
        "global_issues": global_issues,
        "missing_question_ids": sorted(set(questions) - set(answers)),
        "unexpected_question_ids": sorted(set(answers) - set(questions)),
        "answers": rows,
        "strict_request_numeric_contract_pass": numeric_pass,
        "legacy_sum_tolerance_unchanged": LEGACY_SUM_TOLERANCE,
        "counts": {
            "requested_answers": len(questions),
            "returned_answers": len(answers),
            "individually_matches_numeric_contract": valid_count,
            "individual_numeric_or_coverage_failures": len(rows) - valid_count,
            "otherwise_valid_answers_dropped_with_request": valid_count if not numeric_pass else 0,
        },
        "raw_probabilities_normalized": False,
        "semantic_correctness": "UNASSESSED",
        "mutation_authorized": False,
        "release_authorized": False,
    }
