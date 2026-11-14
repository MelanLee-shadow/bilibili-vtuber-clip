"""The hash-bound review receipt (upload-compatibility interface).

The receipt records who reviewed, the verdict, and the SHA-256 of every file
the verdict covers: finished media, member, record, the ``check`` result file
and the reviewer's free-form ``REVIEW.md``.  It does not judge the review
itself; how to review is the reviewer's job (see
``docs/pipeline/85-finished-media-review.md``).  Nothing here edits subtitles
or media.
"""

from __future__ import annotations

import datetime as dt
import shutil
from pathlib import Path
from typing import Any, Mapping

from .common import (
    AUTO_CHECKS_NAME,
    AUTO_CHECKS_SCHEMA,
    EVIDENCE_DIR,
    RECEIPT_SCHEMA,
    REVIEW_NAME,
    FmrError,
    file_sha,
    write_json,
)
from .member import member_copy_path, package_files, receipt_path, record_path

A_IDS = ("A1", "A2", "A3", "A4", "A4b", "A5", "A6", "A7", "A8", "A9", "A10", "A11", "A12", "A13")
SKIP_ALLOWED = {"A4b", "A13"}
VERDICTS = ("PASS", "FAIL")
REVIEWER_KINDS = ("AGENT", "维护者")


def auto_rows(auto: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    rows = auto.get("checks")
    out: dict[str, dict[str, Any]] = {}
    if isinstance(rows, list):
        for item in rows:
            if isinstance(item, dict) and isinstance(item.get("id"), str):
                out[item["id"]] = item
    return out


def auto_problems_and_findings(auto: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    """Structure problems (result file unusable) and findings (a check failed).

    Findings only feed ``findings.md`` for the reviewer; they never block the receipt.
    """
    problems: list[str] = []
    findings: list[str] = []
    if auto.get("schema_version") != AUTO_CHECKS_SCHEMA:
        return ["FMR_AUTO_CHECKS_SCHEMA_INVALID"], findings
    rows = auto_rows(auto)
    if set(rows) != set(A_IDS) or len(auto.get("checks") or []) != len(A_IDS):
        problems.append("FMR_AUTO_CHECKS_INCOMPLETE")
    for check_id, item in rows.items():
        status = item.get("status")
        if status in {"PASS", "FLAG"}:
            continue
        if status in {"SKIPPED_DISABLED", "SKIPPED_UNAVAILABLE"} and check_id in SKIP_ALLOWED:
            continue
        findings.append(f"{check_id}:{status}:" + ",".join(item.get("reason_codes") or []))
    return problems, findings


def bindings(root: Path, cid: str) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    paths = dict(package_files(root, cid))
    paths["member"] = member_copy_path(root, cid)
    paths["record"] = record_path(root, cid)
    for key, path in paths.items():
        out[key] = {"path": path.name, "sha256": file_sha(path)}
    return out


def install_review(root: Path, review: Path) -> Path:
    """Copy the reviewer's report into the package as ``REVIEW.md`` (free format, non-empty)."""
    review = Path(review)
    if review.is_symlink() or not review.is_file():
        raise FmrError("FMR_REVIEW_MISSING")
    if not review.read_text(encoding="utf-8").strip():
        raise FmrError("FMR_REVIEW_EMPTY")
    target = Path(root) / REVIEW_NAME
    if target.exists() and target.resolve() == review.resolve():
        return target
    if target.exists() or target.is_symlink():
        target.unlink()
    shutil.copyfile(review, target)
    return target


def build_receipt(
    root: Path,
    cid: str,
    member: Mapping[str, Any],
    *,
    review: Path,
    reviewer_id: str,
    verdict: str,
    reviewer_kind: str = "AGENT",
) -> dict[str, Any]:
    """Install ``REVIEW.md`` and build the receipt.  Raises FmrError on interface problems."""
    root = Path(root)
    if verdict not in VERDICTS:
        raise FmrError("FMR_VERDICT_INVALID")
    if reviewer_kind not in REVIEWER_KINDS or not str(reviewer_id or "").strip():
        raise FmrError("FMR_REVIEWER_INVALID")
    if reviewer_id == (member.get("maker") or {}).get("id"):
        raise FmrError("FMR_REVIEWER_IS_MAKER")
    auto_path = root / EVIDENCE_DIR / AUTO_CHECKS_NAME
    if not auto_path.is_file() or not record_path(root, cid).is_file():
        raise FmrError("FMR_CHECK_NOT_RUN: run `check` first")
    review_file = install_review(root, review)
    return {
        "schema_version": RECEIPT_SCHEMA,
        "candidate_id": cid,
        "recording_date": member["recording_date"],
        "title_sha256": file_sha(package_files(root, cid)["title"]),
        "maker": member["maker"],
        "reviewer": {"kind": reviewer_kind, "id": reviewer_id},
        "verdict": verdict,
        "bindings": bindings(root, cid),
        "auto_checks": {"path": f"{EVIDENCE_DIR}/{AUTO_CHECKS_NAME}", "sha256": file_sha(auto_path)},
        "review": {"path": review_file.name, "sha256": file_sha(review_file)},
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }


def write_receipt(root: Path, cid: str, receipt: Mapping[str, Any]) -> Path:
    path = receipt_path(root, cid)
    write_json(path, receipt)
    return path
