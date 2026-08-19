"""One-shot retry authority for exact-final provider-budget exhaustion.

This lane does not reinterpret an unresolved finding as repaired.  It only
recognizes the mechanical case where an otherwise valid current audit used
its complete provider-call allowance and every remaining active finding was
skipped solely because that allowance was exhausted.  A later pass can replay
the already cached witness/judge pairs at zero provider cost and spend the
unchanged cap on the remaining findings.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Collection, Mapping
from pathlib import Path


RETRY_SCHEMA = "final-review-provider-budget-retry.v1"
LEDGER_SCHEMA = "final-review-provider-budget-retry-ledger.v1"
LEDGER_FIELD = "final_review_provider_budget_retry_ledger"
FAILURE_STAGE = "final_review_provider_budget"

LEDGER_STATE_ABSENT = "ABSENT"
LEDGER_STATE_EMPTY = "EMPTY"
LEDGER_STATE_CONSUMED = "CONSUMED"
LEDGER_STATE_INVALID = "INVALID"

_SHA256_RX = re.compile(r"sha256:[0-9a-f]{64}\Z")
_CANDIDATE_RX = re.compile(r"[A-Za-z0-9_-]{1,96}\Z")
_SKIPPED_KEYS = frozenset(
    {
        "schema_version",
        "status",
        "repaired",
        "provider_adjudication_count",
        "provider_adjudication_budget",
    }
)
_RETRY_KEYS = frozenset(
    {
        "schema_version",
        "status",
        "candidate_id",
        "active_finding_count",
        "raw_validated_finding_count",
        "resolved_finding_count",
        "disclosed_finding_count",
        "provider_adjudication_count",
        "provider_adjudication_budget",
        "reviewed_srt_file",
        "reviewed_srt_sha256",
        "reviewed_srt_size_bytes",
        "prior_adjudication_sha256s",
        "active_finding_sha256s",
        "retry_fingerprint",
    }
)


def _plain_int(value: object, *, minimum: int = 0) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        return None
    return value


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _retry_basis(retry: Mapping[str, object]) -> dict[str, object]:
    return {
        key: retry[key]
        for key in sorted(_RETRY_KEYS - {"retry_fingerprint"})
    }


def _valid_correction_authority(audit: Mapping[str, object]) -> bool:
    authority = audit.get("correction_mutation_authority")
    return bool(
        isinstance(authority, Mapping)
        and authority.get("schema_version")
        == "subtitle-correction-mutation-audit.v1"
        and authority.get("status") == "PASS"
        and authority.get("failures") == []
    )


def build_provider_budget_retry_evidence(
    audit: Mapping[str, object],
    *,
    candidate_id: str,
    contract_reason_code: str,
    reviewed_srt_path: Path,
) -> dict[str, object] | None:
    """Return a hash-bound retry token, or ``None`` on any mixed/malformed case."""

    findings = audit.get("findings")
    resolved = audit.get("resolved_findings")
    disclosed = audit.get("unresolved_findings_disclosed")
    discovery = audit.get("discovery")
    boundary = audit.get("boundary_semantic_review")
    carryover_count = audit.get("carryover_persisted_count")
    validated_count = _plain_int(audit.get("validated_finding_count"), minimum=1)
    if not (
        _CANDIDATE_RX.fullmatch(candidate_id)
        and contract_reason_code == "FINAL_REVIEW_UNRESOLVED_FINDINGS"
        and audit.get("schema_version") == "final-review-audit.v2"
        and audit.get("status") == "FLAGGED"
        and audit.get("release_gate") == "BLOCK"
        and audit.get("reason_codes") == ["FINAL_REVIEW_UNRESOLVED_FINDINGS"]
        and (
            carryover_count is None
            or _plain_int(carryover_count, minimum=0) == 0
        )
        and isinstance(findings, list)
        and bool(findings)
        and validated_count == len(findings)
        and isinstance(resolved, list)
        and isinstance(disclosed, list)
        and isinstance(discovery, Mapping)
        and discovery.get("status") == "COMPLETE"
        and discovery.get("explicit_empty_findings") is False
        and isinstance(boundary, Mapping)
        and boundary.get("status") == "PASS"
        and _valid_correction_authority(audit)
    ):
        return None

    raw_count = _plain_int(
        discovery.get("raw_validated_finding_count"), minimum=1
    )
    resolved_count = _plain_int(
        discovery.get("resolved_finding_count"), minimum=0
    )
    if not (
        raw_count == len(findings) + len(resolved) + len(disclosed)
        and resolved_count == len(resolved)
    ):
        return None

    prior_rows = [*resolved, *disclosed]
    prior_digests: list[str] = []
    for prior in prior_rows:
        adjudication = (
            prior.get("exact_release_adjudication")
            if isinstance(prior, Mapping)
            else None
        )
        if not (
            isinstance(adjudication, Mapping)
            and adjudication.get("schema_version")
            == "subtitle-span-adjudication.v1"
            and adjudication.get("status") == "OBSERVED"
            and adjudication.get("repaired") is False
        ):
            return None
        prior_digests.append(_canonical_sha256(prior))
    if len(set(prior_digests)) != len(prior_digests):
        return None

    provider_counts: set[int] = set()
    provider_budgets: set[int] = set()
    finding_digests: list[str] = []
    for finding in findings:
        adjudication = (
            finding.get("exact_release_adjudication")
            if isinstance(finding, Mapping)
            else None
        )
        if not (
            isinstance(adjudication, Mapping)
            and set(adjudication) == _SKIPPED_KEYS
            and adjudication.get("schema_version")
            == "subtitle-span-adjudication.v1"
            and adjudication.get("status") == "SKIPPED_BUDGET"
            and adjudication.get("repaired") is False
        ):
            return None
        provider_count = _plain_int(
            adjudication.get("provider_adjudication_count"), minimum=1
        )
        provider_budget = _plain_int(
            adjudication.get("provider_adjudication_budget"), minimum=1
        )
        if provider_count is None or provider_budget is None:
            return None
        provider_counts.add(provider_count)
        provider_budgets.add(provider_budget)
        finding_digests.append(_canonical_sha256(finding))
    if (
        len(provider_counts) != 1
        or len(provider_budgets) != 1
        or len(set(finding_digests)) != len(finding_digests)
    ):
        return None
    provider_count = next(iter(provider_counts))
    provider_budget = next(iter(provider_budgets))
    if not (
        provider_count == provider_budget
        and provider_count == len(prior_rows)
        and raw_count == provider_count + validated_count
    ):
        return None

    reviewed_sha256 = str(audit.get("reviewed_srt_sha256") or "")
    try:
        if reviewed_srt_path.is_symlink() or not reviewed_srt_path.is_file():
            return None
        reviewed_bytes = reviewed_srt_path.read_bytes()
    except OSError:
        return None
    if (
        _SHA256_RX.fullmatch(reviewed_sha256) is None
        or "sha256:" + hashlib.sha256(reviewed_bytes).hexdigest()
        != reviewed_sha256
    ):
        return None

    retry: dict[str, object] = {
        "schema_version": RETRY_SCHEMA,
        "status": "ELIGIBLE",
        "candidate_id": candidate_id,
        "active_finding_count": validated_count,
        "raw_validated_finding_count": raw_count,
        "resolved_finding_count": resolved_count,
        "disclosed_finding_count": len(disclosed),
        "provider_adjudication_count": provider_count,
        "provider_adjudication_budget": provider_budget,
        "reviewed_srt_file": (
            f"replacement_recuts/{candidate_id}.recut.srt"
        ),
        "reviewed_srt_sha256": reviewed_sha256,
        "reviewed_srt_size_bytes": len(reviewed_bytes),
        "prior_adjudication_sha256s": prior_digests,
        "active_finding_sha256s": finding_digests,
    }
    retry["retry_fingerprint"] = _canonical_sha256(_retry_basis(retry))
    return retry


def valid_retry_evidence(
    value: object, *, candidate_id: str
) -> dict[str, object] | None:
    if not (
        isinstance(value, Mapping)
        and set(value) == _RETRY_KEYS
        and value.get("schema_version") == RETRY_SCHEMA
        and value.get("status") == "ELIGIBLE"
        and value.get("candidate_id") == candidate_id
        and _CANDIDATE_RX.fullmatch(candidate_id)
        and _plain_int(value.get("active_finding_count"), minimum=1)
        is not None
        and _plain_int(value.get("raw_validated_finding_count"), minimum=1)
        is not None
        and _plain_int(value.get("resolved_finding_count"), minimum=0)
        is not None
        and _plain_int(value.get("disclosed_finding_count"), minimum=0)
        is not None
        and _plain_int(value.get("provider_adjudication_count"), minimum=1)
        is not None
        and _plain_int(value.get("provider_adjudication_budget"), minimum=1)
        is not None
        and value.get("provider_adjudication_count")
        == value.get("provider_adjudication_budget")
        == value.get("resolved_finding_count")
        + value.get("disclosed_finding_count")
        and value.get("raw_validated_finding_count")
        == value.get("active_finding_count")
        + value.get("resolved_finding_count")
        + value.get("disclosed_finding_count")
        and value.get("reviewed_srt_file")
        == f"replacement_recuts/{candidate_id}.recut.srt"
        and _SHA256_RX.fullmatch(str(value.get("reviewed_srt_sha256") or ""))
        and _plain_int(value.get("reviewed_srt_size_bytes"), minimum=1)
        is not None
        and isinstance(value.get("prior_adjudication_sha256s"), list)
        and len(value["prior_adjudication_sha256s"])
        == value.get("provider_adjudication_count")
        and all(
            isinstance(digest, str) and _SHA256_RX.fullmatch(digest)
            for digest in value["prior_adjudication_sha256s"]
        )
        and len(set(value["prior_adjudication_sha256s"]))
        == len(value["prior_adjudication_sha256s"])
        and isinstance(value.get("active_finding_sha256s"), list)
        and len(value["active_finding_sha256s"])
        == value.get("active_finding_count")
        and all(
            isinstance(digest, str) and _SHA256_RX.fullmatch(digest)
            for digest in value["active_finding_sha256s"]
        )
        and len(set(value["active_finding_sha256s"]))
        == len(value["active_finding_sha256s"])
        and value.get("retry_fingerprint")
        == _canonical_sha256(_retry_basis(value))
    ):
        return None
    return dict(value)


def _valid_ledger(value: object) -> list[dict[str, str]] | None:
    if value is None:
        return []
    if not (
        isinstance(value, Mapping)
        and set(value) == {"schema_version", "entries"}
        and value.get("schema_version") == LEDGER_SCHEMA
        and isinstance(value.get("entries"), list)
    ):
        return None
    entries: list[dict[str, str]] = []
    identities: set[tuple[str, str]] = set()
    for row in value["entries"]:
        if not (
            isinstance(row, Mapping)
            and set(row) == {"candidate_id", "retry_fingerprint"}
            and _CANDIDATE_RX.fullmatch(str(row.get("candidate_id") or ""))
            and _SHA256_RX.fullmatch(str(row.get("retry_fingerprint") or ""))
        ):
            return None
        identity = (str(row["candidate_id"]), str(row["retry_fingerprint"]))
        if identity in identities:
            return None
        identities.add(identity)
        entries.append(
            {"candidate_id": identity[0], "retry_fingerprint": identity[1]}
        )
    return entries if len(entries) <= 1 else None


def resolve_provider_budget_retry_ledger_history(
    record: Mapping[str, object],
    *,
    candidate_id: str,
    history_records: Collection[object] = (),
) -> tuple[str, dict[str, object] | None]:
    """Resolve one canonical consumed ledger across current and superseded rows."""

    if isinstance(history_records, (str, bytes, Mapping)):
        return LEDGER_STATE_INVALID, None
    matching_history: list[Mapping[str, object]] = []
    for row in history_records:
        if not isinstance(row, Mapping) or LEDGER_FIELD not in row:
            continue
        ledger_value = row.get(LEDGER_FIELD)
        # Legacy null/empty history is not a consumed authority.  Any nonempty
        # claim, however, must validate before outer-CID filtering; otherwise a
        # row can hide a target ledger behind a foreign or missing outer CID.
        if ledger_value is None:
            continue
        entries = _valid_ledger(ledger_value)
        if entries is None:
            return LEDGER_STATE_INVALID, None
        if not entries:
            continue
        outer_candidate_id = str(
            row.get("candidate_id") or row.get("cid") or ""
        )
        if (
            _CANDIDATE_RX.fullmatch(outer_candidate_id) is None
            or any(
                entry["candidate_id"] != outer_candidate_id
                for entry in entries
            )
        ):
            return LEDGER_STATE_INVALID, None
        if outer_candidate_id == candidate_id:
            matching_history.append(row)
    saw_empty = False
    canonical: dict[str, object] | None = None
    for row_index, row in enumerate((record, *matching_history)):
        if LEDGER_FIELD not in row:
            continue
        ledger_value = row.get(LEDGER_FIELD)
        entries = _valid_ledger(ledger_value)
        if ledger_value is None or entries is None or any(
            entry["candidate_id"] != candidate_id for entry in entries
        ):
            return LEDGER_STATE_INVALID, None
        if not entries:
            if row_index > 0:
                return LEDGER_STATE_INVALID, None
            saw_empty = True
            continue
        ledger: dict[str, object] = {
            "schema_version": LEDGER_SCHEMA,
            "entries": [dict(entry) for entry in entries],
        }
        if canonical is not None and canonical != ledger:
            return LEDGER_STATE_INVALID, None
        canonical = ledger
    if canonical is not None and saw_empty:
        return LEDGER_STATE_INVALID, None
    if canonical is not None:
        return LEDGER_STATE_CONSUMED, canonical
    if saw_empty:
        return LEDGER_STATE_EMPTY, None
    return LEDGER_STATE_ABSENT, None


def carry_validated_active_provider_budget_ledger(
    record: Mapping[str, object],
    result: dict[str, object],
    *,
    candidate_id: str,
) -> dict[str, object]:
    """Carry one strict active ledger, or type an invalid claim fail-closed.

    Production has no access to superseded history.  Queue construction must
    therefore restore the canonical candidate ledger into the active row, and
    every producer result validates that active value before persisting it.
    """

    ledger_state, ledger = resolve_provider_budget_retry_ledger_history(
        record,
        candidate_id=candidate_id,
    )
    result.pop(LEDGER_FIELD, None)
    if ledger_state == LEDGER_STATE_CONSUMED and ledger is not None:
        result[LEDGER_FIELD] = copy.deepcopy(ledger)
        return result
    if ledger_state != LEDGER_STATE_INVALID:
        # Absent and structurally valid empty ledgers carry no consumed token.
        return result

    reason_code = "FINAL_REVIEW_PROVIDER_BUDGET_LEDGER_INVALID"
    prior_reason_codes = result.get("reason_codes")
    if isinstance(prior_reason_codes, str):
        reason_codes = [prior_reason_codes] if prior_reason_codes else []
    elif isinstance(prior_reason_codes, list):
        reason_codes = [str(code) for code in prior_reason_codes if str(code)]
    else:
        reason_codes = []
    if reason_code not in reason_codes:
        reason_codes.append(reason_code)
    result.update(
        {
            "candidate_id": candidate_id,
            "status": "candidate_rejected",
            "failure_kind": "pipeline_contract",
            "failure_stage": "final_review_provider_budget_ledger",
            "failure_recoverable": False,
            "rejection_reason": "final_review_provider_budget_ledger_invalid",
            "reason_codes": reason_codes,
        }
    )
    evidence = result.get("failure_evidence")
    normalized_evidence = dict(evidence) if isinstance(evidence, Mapping) else {}
    normalized_evidence["provider_budget_ledger_validation"] = {
        "schema_version": "final-review-provider-budget-ledger-validation.v1",
        "status": "BLOCK",
        "candidate_id": candidate_id,
        "reason_code": reason_code,
    }
    result["failure_evidence"] = normalized_evidence
    return result


def preserved_provider_budget_retry_ledger(
    record: Mapping[str, object],
    *,
    candidate_id: str,
    history_records: Collection[object] = (),
) -> dict[str, object] | None:
    """Deep-copy the one consumed candidate ledger across later retries."""

    status, ledger = resolve_provider_budget_retry_ledger_history(
        record,
        candidate_id=candidate_id,
        history_records=history_records,
    )
    return ledger if status == LEDGER_STATE_CONSUMED else None


def unconsumed_provider_budget_retry(
    record: Mapping[str, object],
    *,
    candidate_id: str,
    history_records: Collection[object] = (),
) -> dict[str, object] | None:
    evidence = record.get("failure_evidence")
    retry = valid_retry_evidence(
        evidence.get("provider_budget_retry")
        if isinstance(evidence, Mapping)
        else None,
        candidate_id=candidate_id,
    )
    ledger_state, _ = resolve_provider_budget_retry_ledger_history(
        record,
        candidate_id=candidate_id,
        history_records=history_records,
    )
    failure_fingerprint = str(record.get("failure_fingerprint") or "")
    if not (
        retry is not None
        and ledger_state in {LEDGER_STATE_ABSENT, LEDGER_STATE_EMPTY}
        and record.get("status") in {"failed", "candidate_rejected"}
        and record.get("failure_kind") == "subtitle_authority"
        and record.get("failure_stage") == FAILURE_STAGE
        and record.get("failure_recoverable") is True
        and _SHA256_RX.fullmatch(failure_fingerprint)
    ):
        return None
    # This is one candidate-level mechanical retry, not a budget that can be
    # refreshed by producing a different finding digest on each failed pass.
    return retry


def provider_budget_retry_claimed(record: Mapping[str, object]) -> bool:
    """Whether a record claims this route and must not fall through generic retry."""

    evidence = record.get("failure_evidence")
    return bool(
        record.get("failure_stage") == FAILURE_STAGE
        or (
            isinstance(evidence, Mapping)
            and "provider_budget_retry" in evidence
        )
    )


def consumed_provider_budget_retry_ledger(
    record: Mapping[str, object],
    *,
    candidate_id: str,
    retry: Mapping[str, object],
    history_records: Collection[object] = (),
) -> dict[str, object] | None:
    validated = valid_retry_evidence(retry, candidate_id=candidate_id)
    ledger_state, _ = resolve_provider_budget_retry_ledger_history(
        record,
        candidate_id=candidate_id,
        history_records=history_records,
    )
    if (
        validated is None
        or ledger_state not in {LEDGER_STATE_ABSENT, LEDGER_STATE_EMPTY}
        or unconsumed_provider_budget_retry(
            record,
            candidate_id=candidate_id,
            history_records=history_records,
        )
        != validated
    ):
        return None
    identity = {
        "candidate_id": candidate_id,
        "retry_fingerprint": str(validated["retry_fingerprint"]),
    }
    return {
        "schema_version": LEDGER_SCHEMA,
        "entries": [identity],
    }
