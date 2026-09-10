"""Keep prior CPA repair evidence when a later native review starts a new run."""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import hashlib
import json


HISTORY_KEY = "retained_previous_runs"
RUN_SCHEMA = "retained-exact-final-cpa-self-heal-run.v1"


def _sha(value: object) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True,
        separators=(",", ":"),
    ).encode()).hexdigest()


def retained_runs(previous: object) -> list[dict[str, object]]:
    """Flatten only completed runs; their final SRT remains historical."""
    if not isinstance(previous, Mapping):
        return []
    retained = previous.get(HISTORY_KEY, [])
    if not isinstance(retained, list):
        raise ValueError("EXACT_FINAL_CPA_HISTORY_INVALID")
    result = deepcopy(retained)
    if previous.get("status") == "PASS":
        body = {str(k): deepcopy(v) for k, v in previous.items() if k != HISTORY_KEY}
        entry = {"schema_version": RUN_SCHEMA, "audit": body, "audit_sha256": _sha(body)}
        if not any(row == entry for row in result):
            result.append(entry)
    # Validate through the same reader used by package acceptance.
    repair_history({"passes": [], HISTORY_KEY: result})
    return result


def repair_history(current: Mapping[str, object]) -> list[Mapping[str, object]]:
    """Return actual receipts; reject a malformed or rehashed partial history."""
    runs: list[Mapping[str, object]] = [current]
    history = current.get(HISTORY_KEY, [])
    if not isinstance(history, list):
        raise ValueError("EXACT_FINAL_CPA_HISTORY_INVALID")
    seen: set[str] = set()
    for entry in history:
        if not isinstance(entry, Mapping) or set(entry) != {"schema_version", "audit", "audit_sha256"}:
            raise ValueError("EXACT_FINAL_CPA_HISTORY_INVALID")
        audit = entry["audit"]
        digest = entry["audit_sha256"]
        if not (
            entry["schema_version"] == RUN_SCHEMA
            and isinstance(audit, Mapping)
            and audit.get("schema_version") == "exact-final-cpa-self-heal-audit.v1"
            and audit.get("status") == "PASS"
            and isinstance(audit.get("final_srt_sha256"), str)
            and len(audit["final_srt_sha256"]) == 71
            and HISTORY_KEY not in audit
            and digest == _sha(audit)
            and digest not in seen
        ):
            raise ValueError("EXACT_FINAL_CPA_HISTORY_INVALID")
        seen.add(str(digest))
        runs.append(audit)
    receipts: list[Mapping[str, object]] = []
    for run in runs:
        passes = run.get("passes")
        if not isinstance(passes, list):
            raise ValueError("EXACT_FINAL_CPA_HISTORY_INVALID")
        for row in passes:
            repairs = row.get("repairs") if isinstance(row, Mapping) else None
            if not isinstance(repairs, list) or any(not isinstance(r, Mapping) for r in repairs):
                raise ValueError("EXACT_FINAL_CPA_HISTORY_INVALID")
            receipts.extend(repairs)
    return receipts


def build_run_audit(
    passes: list, previous_runs: list, *, final_srt_sha256: str | None = None,
) -> dict:
    """Build current-run state while preserving independently completed runs."""
    return {
        "schema_version": "exact-final-cpa-self-heal-audit.v1",
        "status": "PASS" if final_srt_sha256 is not None else "REVIEW_PENDING",
        "passes": passes,
        **({"final_srt_sha256": final_srt_sha256} if final_srt_sha256 is not None else {}),
        **({HISTORY_KEY: previous_runs} if previous_runs else {}),
    }
