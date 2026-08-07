"""Bounded selection-rescore lane state machine (2026-08-07 狍哥案修复).

``docs/reviews/2026-08-07-source-fact-rescore-design.md`` §2-§5.  When
source-fact repair edits a candidate's hook and the old selection scorecard
is judged INCOMPATIBLE, this module carries that fact from the standalone
producer subprocess (``producer_package_finalization.py``) across the
process boundary to the unattended runner (``talk_lane.py`` /
``delivery_recovery.py``), and owns the one-shot-per-repaired-hook fingerprint
bookkeeping that keeps the rescore lane bounded instead of an infinite spin.

Deliberately a standalone module rather than growing the two module-debt-
ledger files it is called from (``producer_package_finalization.py``,
``delivery_recovery.py``): both are frozen at their 2026-07-31 line counts
and any thin call site there is the only budget spent against that ledger.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Mapping


RESCORE_RECEIPT_SCHEMA = "source-fact-rescore.v1"
_SIDECAR_GLOB = "replacement_recuts/*.source-fact-rescore.json"
# 一次修正 hook 一次追改；第三个不同 hook 说明 review 本身在震荡，交终态。
SOURCE_FACT_RESCORE_CAP = 2


def _sha256_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sidecar_path(recut_dir: Path, candidate_id: str) -> Path:
    return recut_dir / f"{candidate_id}.source-fact-rescore.json"


def classify_source_fact_review_marker(source_fact_review: object) -> str:
    """Pick the ``SystemExit`` marker for a non-passing source-fact receipt.

    Pure/testable counterpart of ``producer_package_finalization.py``'s
    marker-selection branch (design §3.2, 2026-08-07 狍哥案修复):

    - ``SOURCE_FACT_REPAIRED_HOOK_SCORECARD_STALE`` → the bounded rescore
      lane, never the terminal EXHAUSTED wording (only one review pass ran).
    - provider-unavailable/call-failed → the pre-existing infra-wait marker.
    - a real 5-pass ``REPAIR_EXHAUSTED`` decision → keeps the EXHAUSTED word.
    - everything else (REPAIR_CYCLE, title-authority block, first-pass
      invalid shape, missing receipt) → a neutral "unresolved" marker; the
      EXHAUSTED word is reserved for genuine pass exhaustion, not a catch-all.
    """

    reason = (
        str(source_fact_review.get("reason_code") or "unknown")
        if isinstance(source_fact_review, Mapping)
        else "missing_receipt"
    )
    if reason == "SOURCE_FACT_REPAIRED_HOOK_SCORECARD_STALE":
        return "SOURCE_FACT_REPAIRED_RESCORE_REQUIRED"
    if reason in {"CPA_TEXT_REVIEW_UNAVAILABLE", "CPA_TEXT_REVIEW_CALL_FAILED"}:
        return "SOURCE_FACT_REVIEW_INFRA_UNRESOLVED"
    if (
        isinstance(source_fact_review, Mapping)
        and source_fact_review.get("decision") == "REPAIR_EXHAUSTED"
    ):
        return "SOURCE_FACT_REPAIR_EXHAUSTED"
    return "SOURCE_FACT_REVIEW_UNRESOLVED"


def write_pending_rescore_sidecar(
    recut_dir: Path,
    *,
    candidate_id: str,
    source_fact_review: Mapping[str, object],
) -> Path:
    """Persist the PENDING rescore receipt where the runner can read it back.

    Raises ``ValueError`` if the review receipt lacks the ``rescore_candidate``
    block a REPAIR_SCORECARD_STALE decision must always carry (file 1 of this
    fix) — a defensive fail-closed check, not an expected path.
    """

    block = source_fact_review.get("rescore_candidate")
    if (
        source_fact_review.get("decision") != "REPAIR_SCORECARD_STALE"
        or not isinstance(block, Mapping)
        or not isinstance(block.get("repaired_selection_hook"), str)
        or not isinstance(block.get("repaired_selection_hook_sha256"), str)
    ):
        raise ValueError("SOURCE_FACT_RESCORE_CANDIDATE_BLOCK_MISSING_OR_INVALID")
    receipt = {
        "schema_version": RESCORE_RECEIPT_SCHEMA,
        "candidate_id": candidate_id,
        "review_receipt_sha256": source_fact_review.get("receipt_sha256"),
        "repaired_hook": block.get("repaired_selection_hook"),
        "repaired_hook_sha256": block.get("repaired_selection_hook_sha256"),
        "repaired_title": block.get("repaired_title"),
        "repaired_title_sha256": block.get("repaired_title_sha256"),
        "stale_scorecard_sha256": block.get("stale_selection_scorecard_sha256"),
        "stale_reason": (
            str(
                (block.get("selection_scorecard_review") or {}).get("reason")
                or ""
            )
            if isinstance(block.get("selection_scorecard_review"), Mapping)
            else ""
        ),
        "attempts": [],
        "status": "PENDING",
    }
    path = _sidecar_path(recut_dir, candidate_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)
    return path


def _read_sidecar(work_dir: Path, *, candidate_id: str) -> dict[str, object] | None:
    for path in sorted(work_dir.glob(_SIDECAR_GLOB)):
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if (
            isinstance(document, dict)
            and document.get("schema_version") == RESCORE_RECEIPT_SCHEMA
            and document.get("candidate_id") == candidate_id
            and isinstance(document.get("repaired_hook"), str)
            and isinstance(document.get("repaired_hook_sha256"), str)
            and document["repaired_hook_sha256"]
            == _sha256_text(document["repaired_hook"])
        ):
            return document
    return None


def rescore_fingerprint(failure_fingerprint: str, repaired_hook_sha256: str) -> str:
    """One fingerprint per (this failure identity, this repaired hook).

    A different repaired hook (review oscillated to a new proposal) earns a
    fresh budget slot instead of being silently blocked by the previous
    hook's consumption record; the same hook retried again does not.
    """

    return "sha256:" + hashlib.sha256(
        f"{failure_fingerprint}\0{repaired_hook_sha256}".encode("utf-8")
    ).hexdigest()


def attach_pending_rescore_receipt(
    work_dir: Path,
    *,
    candidate_id: str,
    failure_fingerprint: str,
) -> dict[str, object] | None:
    """Read the finalization-side sidecar and bind it to this pick's failure.

    Returns ``None`` when no sidecar exists (finalization failed before
    writing one, or this failure was not a scorecard-stale rescore case) so
    the caller can leave the pick without a ``source_fact_rescore`` receipt.
    """

    receipt = _read_sidecar(work_dir, candidate_id=candidate_id)
    if receipt is None:
        return None
    return {
        **receipt,
        "rescore_fingerprint": rescore_fingerprint(
            failure_fingerprint, receipt["repaired_hook_sha256"]
        ),
    }


def unconsumed_rescore_fingerprint(record: Mapping[str, object]) -> str | None:
    """Return the fingerprint eligible for one bounded requeue, or ``None``.

    Mirrors ``_unconsumed_final_review_carryover`` in ``delivery_recovery.py``
    (same consumed-ledger pattern, ``rescore_consumed_fingerprints`` instead
    of ``final_review_carryover_consumed_fingerprints``), capped at
    ``SOURCE_FACT_RESCORE_CAP`` instead of the carryover lane's own cap.
    """

    receipt = record.get("source_fact_rescore")
    if (
        record.get("status") != "failed"
        or record.get("failure_kind") != "selection_rescore"
        or record.get("failure_recoverable") is not True
        or not isinstance(receipt, Mapping)
        or receipt.get("schema_version") != RESCORE_RECEIPT_SCHEMA
        or receipt.get("status") != "PENDING"
    ):
        return None
    fingerprint = receipt.get("rescore_fingerprint")
    if not isinstance(fingerprint, str) or not fingerprint:
        return None
    consumed = record.get("rescore_consumed_fingerprints")
    consumed_fingerprints = (
        [value for value in consumed if isinstance(value, str)]
        if isinstance(consumed, list)
        else []
    )
    if (
        fingerprint in consumed_fingerprints
        or len(consumed_fingerprints) >= SOURCE_FACT_RESCORE_CAP
    ):
        return None
    return fingerprint
