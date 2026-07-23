#!/usr/bin/env python3
"""Apply hash-bound reviewed title/cover corrections, then repair only selected covers.

The workflow is deliberately narrower than a producer rerun: it transactionally
invalidates stale active cover authority, preserves the old immutable generation
as evidence, and invokes the existing approved cover-only lane for exactly the
plan's candidates.  It never calls ASR, speaker, selector, title, upload, or
general talk production.
"""

from __future__ import annotations

import argparse
import copy
import fcntl
import hashlib
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.authorized_upload import (  # noqa: E402
    DEFAULT_UPLOAD_LOCK,
    UploadLockBusy,
    exclusive_upload_lock,
)
from scripts.free_session_autoslice import (  # noqa: E402
    BASE,
    _active_cover_documents,
    _atomic_write_bytes_file,
    _atomic_write_json_file,
    _cover_binding_valid,
    _json_file_bytes,
    delivered_paths,
    pipeline_fingerprint,
    read_state,
    repair_covers,
    write_reports,
    write_state,
)
from scripts.run_auto_review_shadow_pipeline import _lidousha_cover_text  # noqa: E402


PLAN_SCHEMA = "reviewed-cover-repair-plan.v1"
TRANSACTION_SCHEMA = "reviewed-cover-invalidation-transaction.v1"
SAFE_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,96}")
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
SHA_RE = re.compile(r"(?:sha256:)?([0-9a-f]{64})")


class ReviewedCoverRepairError(RuntimeError):
    pass


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalized_sha256(value: object) -> str:
    match = SHA_RE.fullmatch(str(value or ""))
    if match is None:
        raise ReviewedCoverRepairError(f"invalid SHA256 value: {value!r}")
    return match.group(1)


def _read_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ReviewedCoverRepairError(f"{label} is unreadable: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ReviewedCoverRepairError(f"{label} must be a JSON object: {path}")
    return value


def _canonical_record_sha256(record: Mapping[str, Any]) -> str:
    return _sha256_bytes(
        json.dumps(
            record,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def _record_map(state: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    found: dict[str, list[dict[str, Any]]] = {}
    for lane in ("picks", "songs"):
        for record in state.get(lane, []) or []:
            if isinstance(record, dict):
                found.setdefault(str(record.get("candidate_id") or ""), []).append(record)
    duplicates = {candidate_id: len(rows) for candidate_id, rows in found.items() if len(rows) != 1}
    if duplicates:
        raise ReviewedCoverRepairError(f"duplicate state candidate ids: {duplicates}")
    return {candidate_id: rows[0] for candidate_id, rows in found.items()}


def load_plan(path: Path) -> tuple[dict[str, Any], str]:
    if path.is_symlink():
        raise ReviewedCoverRepairError("reviewed cover plan may not be a symlink")
    resolved = path.resolve(strict=True)
    try:
        if not resolved.is_relative_to(ROOT.resolve()):
            raise ReviewedCoverRepairError("reviewed cover plan escapes the deployed repo")
    except OSError as exc:
        raise ReviewedCoverRepairError(f"reviewed cover plan path is invalid: {exc}") from exc
    raw = resolved.read_bytes()
    plan = json.loads(raw)
    if not isinstance(plan, dict):
        raise ReviewedCoverRepairError("reviewed cover plan must be a JSON object")
    if (
        plan.get("do_not_execute") is True
        or plan.get("artifact_lifecycle") == "HISTORICAL_EVIDENCE_ONLY"
    ):
        raise ReviewedCoverRepairError(
            "reviewed cover plan is historical evidence only and may not be executed"
        )
    if plan.get("schema_version") != PLAN_SCHEMA:
        raise ReviewedCoverRepairError(f"plan schema must be {PLAN_SCHEMA}")
    date = str(plan.get("date") or "")
    if DATE_RE.fullmatch(date) is None or plan.get("upload_enabled") is not False:
        raise ReviewedCoverRepairError("plan date/upload boundary is invalid")
    invalidations = plan.get("invalidations")
    repair_candidates = plan.get("repair_candidates")
    if not isinstance(invalidations, list) or not invalidations:
        raise ReviewedCoverRepairError("plan requires at least one invalidation")
    if not isinstance(repair_candidates, list) or not repair_candidates:
        raise ReviewedCoverRepairError("plan requires selected repair candidates")
    invalidation_ids = [str(row.get("candidate_id") or "") for row in invalidations if isinstance(row, dict)]
    repair_ids = [str(row.get("candidate_id") or "") for row in repair_candidates if isinstance(row, dict)]
    if (
        len(invalidation_ids) != len(invalidations)
        or len(repair_ids) != len(repair_candidates)
        or len(set(invalidation_ids)) != len(invalidation_ids)
        or len(set(repair_ids)) != len(repair_ids)
        or any(SAFE_ID_RE.fullmatch(value) is None for value in (*invalidation_ids, *repair_ids))
        or not set(invalidation_ids).issubset(repair_ids)
    ):
        raise ReviewedCoverRepairError("plan candidate ids are invalid, duplicate, or inconsistent")
    for row in repair_candidates:
        _normalized_sha256(row.get("expected_state_record_sha256"))
    for row in invalidations:
        if not all(isinstance(row.get(key), str) and row.get(key) for key in ("expected_title", "title")):
            raise ReviewedCoverRepairError("invalidation title authority is incomplete")
        expected_cover_text = row.get("expected_cover_text")
        if expected_cover_text is not None and (
            not isinstance(expected_cover_text, str) or not expected_cover_text.strip()
        ):
            raise ReviewedCoverRepairError("expected_cover_text must be non-empty")
        if row.get("require_rendered_text_exact") is True and not isinstance(
            expected_cover_text, str
        ):
            raise ReviewedCoverRepairError(
                "exact reviewed cover repair requires expected_cover_text"
            )
        diversity_slot = row.get("cover_diversity_slot")
        if diversity_slot is not None and (
            not isinstance(diversity_slot, int)
            or isinstance(diversity_slot, bool)
            or not 0 <= diversity_slot <= 5
        ):
            raise ReviewedCoverRepairError("cover_diversity_slot must be an integer from 0 through 5")
        media = row.get("media")
        cover = row.get("cover")
        documents = row.get("documents")
        if not isinstance(media, dict) or not isinstance(cover, dict) or not isinstance(documents, list) or len(documents) != 3:
            raise ReviewedCoverRepairError("invalidation artifact envelope is incomplete")
        _normalized_sha256(media.get("sha256"))
        _normalized_sha256(cover.get("sha256"))
        for document in documents:
            if not isinstance(document, dict) or not isinstance(document.get("path"), str):
                raise ReviewedCoverRepairError("invalid active document entry")
            _normalized_sha256(document.get("sha256"))
    ledger = plan.get("upload_ledger")
    if not isinstance(ledger, dict) or Path(str(ledger.get("path") or "")) != BASE / "reports/upload_ledger.jsonl":
        raise ReviewedCoverRepairError("plan must bind the production upload ledger")
    _normalized_sha256(ledger.get("sha256"))
    return plan, _sha256_bytes(raw)


def _plan_binding(record: Mapping[str, Any], plan_sha256: str) -> Mapping[str, Any] | None:
    value = record.get("reviewed_cover_repair")
    return value if isinstance(value, Mapping) and value.get("plan_sha256") == plan_sha256 else None


def _assert_ledger(plan: Mapping[str, Any]) -> None:
    ledger = Path(str(plan["upload_ledger"]["path"]))
    if ledger.is_symlink() or not ledger.is_file() or _sha256_file(ledger) != _normalized_sha256(plan["upload_ledger"]["sha256"]):
        raise ReviewedCoverRepairError("upload ledger changed during reviewed cover repair")


def _invalidate_document(
    document: Mapping[str, Any],
    *,
    title: str,
    cover_text: str | None = None,
) -> dict[str, Any]:
    updated = copy.deepcopy(dict(document))
    hashes = updated.get("artifact_hashes")
    if not isinstance(hashes, dict):
        raise ReviewedCoverRepairError("active document artifact_hashes is missing")
    hashes.pop("cover_sha256", None)
    updated.pop("cover_repair_binding", None)
    cover_text = cover_text if isinstance(cover_text, str) else _lidousha_cover_text(title)
    views: list[dict[str, Any]] = []
    if updated.get("schema_version") == "shadow-publish-draft.v1":
        views.append(updated)
    staging = updated.get("publish_staging")
    if isinstance(staging, dict):
        views.append(staging)
    if not views:
        raise ReviewedCoverRepairError("active document has no publish view")
    for view in views:
        reasons = [str(value) for value in view.get("reason_codes") or []]
        if "REVIEWED_COVER_REPLACEMENT_REQUIRED" not in reasons:
            reasons.append("REVIEWED_COVER_REPLACEMENT_REQUIRED")
        view.update(
            {
                "title": title,
                "title_source": "job_title",
                "title_authority_status": "RESOLVED_MANUAL",
                "title_authority_error": None,
                "title_policy_violations": [],
                "cover_text": cover_text,
                "cover_status": "BLOCKED_AI_COVER_REQUIRED",
                "reason_codes": reasons,
                "upload_enabled": False,
            }
        )
        view.pop("cover_path", None)
        view.pop("cover_generation", None)
        view.pop("cover_repair_binding", None)
    return updated


def _validated_invalidation_documents(
    date: str,
    row: Mapping[str, Any],
    record: Mapping[str, Any],
) -> list[tuple[Path, bytes]]:
    candidate_id = str(row["candidate_id"])
    if record.get("title") != row.get("expected_title"):
        raise ReviewedCoverRepairError(f"{candidate_id} title drifted before invalidation")
    paths = delivered_paths(date, dict(record))
    if paths is None:
        raise ReviewedCoverRepairError(f"{candidate_id} has no active delivery")
    mp4, cover = paths
    media_entry = row["media"]
    cover_entry = row["cover"]
    if (
        mp4.resolve(strict=True) != Path(str(media_entry["path"])).resolve(strict=True)
        or cover.resolve(strict=True) != Path(str(cover_entry["path"])).resolve(strict=True)
        or mp4.is_symlink()
        or cover.is_symlink()
        or _sha256_file(mp4) != _normalized_sha256(media_entry["sha256"])
        or _sha256_file(cover) != _normalized_sha256(cover_entry["sha256"])
    ):
        raise ReviewedCoverRepairError(f"{candidate_id} media/cover authority drifted")
    media_sha = "sha256:" + _sha256_file(mp4)
    active = _active_cover_documents(
        date=date,
        candidate_id=candidate_id,
        title=str(row["expected_title"]),
        mp4=mp4,
        media_sha256=media_sha,
    )
    expected = {
        Path(str(entry["path"])).resolve(strict=True): _normalized_sha256(entry["sha256"])
        for entry in row["documents"]
    }
    actual = {path.resolve(strict=True): document for path, document in active}
    if set(actual) != set(expected):
        raise ReviewedCoverRepairError(f"{candidate_id} active document set drifted")
    intended: list[tuple[Path, bytes]] = []
    for path, document in actual.items():
        if path.is_symlink() or _sha256_file(path) != expected[path]:
            raise ReviewedCoverRepairError(f"{candidate_id} active document hash drifted: {path}")
        intended.append(
            (
                path,
                _json_file_bytes(
                    _invalidate_document(
                        document,
                        title=str(row["title"]),
                        cover_text=(
                            str(row["expected_cover_text"])
                            if isinstance(row.get("expected_cover_text"), str)
                            else None
                        ),
                    )
                ),
            )
        )
    return intended


def _prepare_transaction(
    *,
    plan: Mapping[str, Any],
    plan_sha256: str,
    state: Mapping[str, Any],
    records: Mapping[str, dict[str, Any]],
    transaction_path: Path,
    code_fingerprint: str,
) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    intended_root = transaction_path.parent / "intended"
    for row in plan["invalidations"]:
        candidate_id = str(row["candidate_id"])
        if _plan_binding(records[candidate_id], plan_sha256) is not None:
            raise ReviewedCoverRepairError("bound state exists without its invalidation journal")
        for index, (target, payload) in enumerate(
            _validated_invalidation_documents(str(plan["date"]), row, records[candidate_id])
        ):
            blob = intended_root / f"{candidate_id}-{index:02d}.bin"
            _atomic_write_bytes_file(blob, payload)
            entries.append(
                {
                    "candidate_id": candidate_id,
                    "target": str(target),
                    "intended_blob": str(blob),
                    "intended_sha256": _sha256_bytes(payload),
                }
            )
    journal = {
        "schema_version": TRANSACTION_SCHEMA,
        "status": "PREPARED",
        "date": plan["date"],
        "plan_sha256": plan_sha256,
        "code_fingerprint": code_fingerprint,
        "entries": entries,
        "upload_enabled": False,
        "prepared_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    _atomic_write_json_file(transaction_path, journal)
    return journal


def _commit_transaction(
    *,
    journal_path: Path,
    journal: dict[str, Any],
    plan: Mapping[str, Any],
    plan_sha256: str,
    code_fingerprint: str,
    state_is_bound: bool,
) -> dict[str, Any]:
    if (
        journal.get("schema_version") != TRANSACTION_SCHEMA
        or journal.get("date") != plan.get("date")
        or journal.get("plan_sha256") != plan_sha256
        or journal.get("code_fingerprint") != code_fingerprint
        or journal.get("upload_enabled") is not False
        or journal.get("status") not in {"PREPARED", "COMMITTED", "FINALIZED"}
    ):
        raise ReviewedCoverRepairError("reviewed cover invalidation journal drifted")
    expected_targets = {
        Path(str(entry["path"])).resolve(strict=True)
        for row in plan["invalidations"]
        for entry in row["documents"]
    }
    allowed_roots = {
        (ROOT / "lidousha" / str(plan["date"])).resolve(strict=True),
        *(
            (BASE / "out" / str(plan["date"]) / str(row["candidate_id"])).resolve(
                strict=True
            )
            for row in plan["invalidations"]
        ),
    }
    if any(
        not any(target.is_relative_to(root) for root in allowed_roots)
        for target in expected_targets
    ):
        raise ReviewedCoverRepairError("invalidation plan target escapes active roots")
    validated: list[tuple[Path, bytes, str]] = []
    seen: set[Path] = set()
    for entry in journal.get("entries") or []:
        target = Path(str(entry.get("target") or ""))
        blob = Path(str(entry.get("intended_blob") or ""))
        expected = _normalized_sha256(entry.get("intended_sha256"))
        try:
            target_resolved = target.resolve(strict=False)
            blob_resolved = blob.resolve(strict=True)
        except OSError as exc:
            raise ReviewedCoverRepairError(f"invalidation journal path is invalid: {exc}") from exc
        if (
            target_resolved not in expected_targets
            or target_resolved in seen
            or target.is_symlink()
            or blob.is_symlink()
            or not blob_resolved.is_relative_to(journal_path.parent.resolve(strict=True))
            or _sha256_file(blob_resolved) != expected
        ):
            raise ReviewedCoverRepairError(f"invalid invalidation transaction entry: {target}")
        seen.add(target_resolved)
        validated.append((target, blob_resolved.read_bytes(), expected))
    if seen != expected_targets:
        raise ReviewedCoverRepairError("invalidation journal target set is incomplete")
    if journal["status"] == "PREPARED":
        for target, payload, expected in validated:
            _atomic_write_bytes_file(target, payload)
            if _sha256_file(target) != expected:
                raise ReviewedCoverRepairError(f"invalidation transaction verify failed: {target}")
        journal["status"] = "COMMITTED"
        journal["committed_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        _atomic_write_json_file(journal_path, journal)
    elif not state_is_bound:
        for target, _payload, expected in validated:
            if not target.is_file() or _sha256_file(target) != expected:
                raise ReviewedCoverRepairError(
                    "committed invalidation drifted before state reconciliation"
                )
    return journal


_COVER_STATE_FIELDS = (
    "cover_status",
    "cover_path",
    "cover_sha256",
    "cover_generation_path",
    "cover_generation_sha256",
    "cover_binding_path",
    "cover_binding_sha256",
    "cover_transaction_path",
    "cover_transaction_status",
    "cover_integrity_status",
)


def _invalidate_state_record(
    record: dict[str, Any],
    *,
    row: Mapping[str, Any],
    plan_sha256: str,
) -> None:
    history = record.setdefault("superseded_cover_authorities", [])
    if not isinstance(history, list):
        raise ReviewedCoverRepairError("superseded cover authority history is invalid")
    if not any(isinstance(item, dict) and item.get("plan_sha256") == plan_sha256 for item in history):
        history.append(
            {
                "schema_version": "superseded-cover-authority.v1",
                "plan_sha256": plan_sha256,
                "title": record.get("title"),
                **{key: record.get(key) for key in _COVER_STATE_FIELDS},
                "superseded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
        )
    record["title"] = str(row["title"])
    if row.get("cover_diversity_slot") is not None:
        record["cover_diversity_slot"] = int(row["cover_diversity_slot"])
    record["cover_status"] = "BLOCKED_AI_COVER_REQUIRED"
    for key in (
        "cover_path",
        "cover_sha256",
        "cover_generation",
        "cover_generation_path",
        "cover_generation_sha256",
        "cover_binding_path",
        "cover_binding_sha256",
        "cover_transaction_path",
        "cover_transaction_status",
        "cover_integrity_status",
        "cover_authority_preflight_error",
        "cover_repair_recovered_at",
    ):
        record.pop(key, None)
    summary = record.get("summary")
    if isinstance(summary, dict):
        summary["title"] = record["title"]
        summary["cover_status"] = "BLOCKED_AI_COVER_REQUIRED"
        for key in ("cover_path", "cover_sha256", "cover_binding_path", "cover_binding_sha256"):
            summary.pop(key, None)


def _selected_bindings_valid(
    date: str,
    records: Mapping[str, dict[str, Any]],
    candidate_ids: set[str],
) -> bool:
    for candidate_id in candidate_ids:
        record = records[candidate_id]
        paths = delivered_paths(date, record)
        if paths is None or not _cover_binding_valid(date, record, *paths):
            return False
    return True


def _verify_expected_cover_text(plan: Mapping[str, Any], records: Mapping[str, dict[str, Any]]) -> None:
    for row in plan["invalidations"]:
        expected = row.get("expected_cover_text")
        if not isinstance(expected, str):
            continue
        record = records[str(row["candidate_id"])]
        generation = record.get("cover_generation")
        if not isinstance(generation, dict) or generation.get("cover_text") != expected:
            raise ReviewedCoverRepairError(f"{row['candidate_id']} generated cover text drifted")
        if row.get("require_rendered_text_exact") is True:
            lines = generation.get("rendered_lines")
            if (
                not isinstance(lines, list)
                or "".join(str(value) for value in lines) != expected.replace("\n", "")
                or (expected.endswith("？") and not str(lines[-1]).endswith("？"))
            ):
                raise ReviewedCoverRepairError(f"{row['candidate_id']} rendered punctuation drifted")


def run(plan_path: Path) -> dict[str, Any]:
    plan, plan_sha256 = load_plan(plan_path)
    date = str(plan["date"])
    _assert_ledger(plan)
    state = read_state(date)
    records = _record_map(state)
    repair_rows = {str(row["candidate_id"]): row for row in plan["repair_candidates"]}
    repair_ids = set(repair_rows)
    missing = repair_ids - set(records)
    if missing:
        raise ReviewedCoverRepairError(f"selected repair candidates are missing: {sorted(missing)}")
    for candidate_id, row in repair_rows.items():
        if _plan_binding(records[candidate_id], plan_sha256) is None and (
            _canonical_record_sha256(records[candidate_id])
            != _normalized_sha256(row["expected_state_record_sha256"])
        ):
            raise ReviewedCoverRepairError(f"{candidate_id} state authority drifted")

    code_fingerprint = pipeline_fingerprint()
    transaction_root = BASE / "out" / date / "reviewed_cover_repairs" / plan_sha256[:16]
    transaction_scope = (BASE / "out" / date).resolve(strict=True)
    transaction_root.parent.mkdir(parents=True, exist_ok=True)
    if transaction_root.is_symlink():
        raise ReviewedCoverRepairError("reviewed cover transaction root may not be a symlink")
    transaction_root.mkdir(parents=True, exist_ok=True)
    if not transaction_root.resolve(strict=True).is_relative_to(transaction_scope):
        raise ReviewedCoverRepairError("reviewed cover transaction root escapes the date scope")
    transaction_path = transaction_root / "invalidation-transaction.json"
    if transaction_path.is_symlink() or (
        transaction_path.exists() and not transaction_path.is_file()
    ):
        raise ReviewedCoverRepairError("reviewed cover transaction journal path is invalid")
    state_is_bound = all(
        _plan_binding(records[candidate_id], plan_sha256) is not None
        for candidate_id in repair_ids
    )
    if transaction_path.is_file():
        journal = _read_json(transaction_path, label="reviewed cover invalidation journal")
    else:
        journal = _prepare_transaction(
            plan=plan,
            plan_sha256=plan_sha256,
            state=state,
            records=records,
            transaction_path=transaction_path,
            code_fingerprint=code_fingerprint,
        )
    journal = _commit_transaction(
        journal_path=transaction_path,
        journal=journal,
        plan=plan,
        plan_sha256=plan_sha256,
        code_fingerprint=code_fingerprint,
        state_is_bound=state_is_bound,
    )

    invalidation_rows = {str(row["candidate_id"]): row for row in plan["invalidations"]}
    state_changed = False
    for candidate_id in repair_ids:
        record = records[candidate_id]
        if _plan_binding(record, plan_sha256) is not None:
            continue
        if candidate_id in invalidation_rows:
            _invalidate_state_record(
                record,
                row=invalidation_rows[candidate_id],
                plan_sha256=plan_sha256,
            )
            status = "INVALIDATED"
        else:
            status = "PENDING_COVER_REPAIR"
        record["reviewed_cover_repair"] = {
            "schema_version": PLAN_SCHEMA,
            "status": status,
            "plan_path": str(plan_path.resolve()),
            "plan_sha256": plan_sha256,
            "transaction_path": str(transaction_path),
            "upload_enabled": False,
        }
        state_changed = True
    if state_changed:
        write_state(date, state)

    _assert_ledger(plan)
    if not _selected_bindings_valid(date, records, repair_ids):
        repair_covers(
            date,
            state,
            candidate_ids=repair_ids,
            expected_cover_texts={
                str(row["candidate_id"]): str(row["expected_cover_text"])
                for row in plan["invalidations"]
                if isinstance(row.get("expected_cover_text"), str)
            },
        )
    if not _selected_bindings_valid(date, records, repair_ids):
        failed = {
            candidate_id: records[candidate_id].get("cover_status")
            for candidate_id in repair_ids
            if not (
                delivered_paths(date, records[candidate_id])
                and _cover_binding_valid(
                    date,
                    records[candidate_id],
                    *delivered_paths(date, records[candidate_id]),
                )
            )
        }
        raise ReviewedCoverRepairError(f"selected cover repair did not finish: {failed}")
    _verify_expected_cover_text(plan, records)
    _assert_ledger(plan)

    finalized_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    summary: dict[str, Any] = {}
    for candidate_id in sorted(repair_ids):
        record = records[candidate_id]
        binding = dict(record["reviewed_cover_repair"])
        binding.update(
            {
                "status": "FINALIZED",
                "cover_path": record.get("cover_path"),
                "cover_sha256": record.get("cover_sha256"),
                "cover_binding_path": record.get("cover_binding_path"),
                "cover_binding_sha256": record.get("cover_binding_sha256"),
                "finalized_at": finalized_at,
            }
        )
        record["reviewed_cover_repair"] = binding
        if record.get("delivered"):
            record["cover_release_gate_satisfied"] = True
        summary[candidate_id] = {
            "title": record.get("title"),
            "cover_path": record.get("cover_path"),
            "cover_sha256": record.get("cover_sha256"),
            "cover_binding_sha256": record.get("cover_binding_sha256"),
        }
    write_state(date, state)
    write_reports(date, state)
    journal["status"] = "FINALIZED"
    journal["finalized_at"] = finalized_at
    _atomic_write_json_file(transaction_path, journal)
    _assert_ledger(plan)
    if not (BASE / "DISABLED").is_file():
        raise ReviewedCoverRepairError("DISABLED disappeared during reviewed cover repair")
    return {
        "date": date,
        "plan_sha256": plan_sha256,
        "transaction_path": str(transaction_path),
        "upload_enabled": False,
        "candidates": summary,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    args = parser.parse_args(argv)
    if not (BASE / "DISABLED").is_file():
        raise ReviewedCoverRepairError("DISABLED must exist for reviewed cover repair")
    runner_lock = BASE / "runner.lock"
    runner_lock.parent.mkdir(parents=True, exist_ok=True)
    with runner_lock.open("a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ReviewedCoverRepairError("runner.lock is busy") from exc
        try:
            with exclusive_upload_lock(DEFAULT_UPLOAD_LOCK):
                if not (BASE / "DISABLED").is_file():
                    raise ReviewedCoverRepairError(
                        "DISABLED disappeared before reviewed cover repair acquired its locks"
                    )
                result = run(args.plan)
        except UploadLockBusy as exc:
            raise ReviewedCoverRepairError("upload.lock is busy") from exc
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
