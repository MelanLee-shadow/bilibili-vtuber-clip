#!/usr/bin/env python3
"""Create a hash-bound, no-upload queue for rerunning CURRENT review talks.

This operator is deliberately separate from cron.  It reads one immutable
source recovery state and creates a new target recovery state exactly once;
the normal unattended runner owns every subsequent production transition.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


FINGERPRINT_RX = re.compile(r"sha256:[0-9a-f]{64}")


def _regular_file_bytes(path: Path, *, label: str) -> bytes:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise SystemExit(f"{label} missing: {path}: {exc}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise SystemExit(f"{label} must be a regular non-symlink file: {path}")
    return path.read_bytes()


def _sha256(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _atomic_create(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.plan-{os.getpid()}-{time.time_ns()}"
    )
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as sink:
            sink.write(payload)
            sink.flush()
            os.fsync(sink.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError as exc:
            raise SystemExit(f"target already exists: {path}") from exc
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-base", type=Path, required=True)
    parser.add_argument("--target-base", type=Path, required=True)
    parser.add_argument("--date", required=True)
    parser.add_argument("--expected-source-state-sha256", required=True)
    parser.add_argument("--expected-old-fingerprint", required=True)
    parser.add_argument("--expected-new-fingerprint")
    parser.add_argument(
        "--expected-new-fingerprint-by-candidate",
        action="append",
        default=[],
        metavar="CANDIDATE_ID=SHA256",
        help=(
            "candidate-scoped current fingerprint; repeat for every queued "
            "candidate when fingerprints differ"
        ),
    )
    parser.add_argument(
        "--candidate-id", action="append", required=True, dest="candidate_ids"
    )
    parser.add_argument(
        "--suppress-candidate-id",
        action="append",
        default=[],
        dest="suppressed_candidate_ids",
        help="CURRENT candidate explicitly excluded by Ivan instead of rerun",
    )
    parser.add_argument(
        "--replacement-candidate-id",
        action="append",
        default=[],
        dest="replacement_candidate_ids",
        help="validated talk_backlog candidate promoted into the recovery queue",
    )
    parser.add_argument("--suppression-authority")
    parser.add_argument(
        "--replacement-selection-authority",
        help="Ivan authority for choosing a replacement over the score baseline",
    )
    parser.add_argument(
        "--given-end-ms",
        action="append",
        default=[],
        metavar="CANDIDATE_ID=ABSOLUTE_MS",
        help=(
            "human-reviewed source-timeline lower-bound end; must be >= the "
            "candidate end; repeat per candidate"
        ),
    )
    parser.add_argument("--given-end-authority")
    parser.add_argument(
        "--public-title-evidence",
        action="append",
        default=[],
        metavar="CANDIDATE_ID=REPO_RELATIVE_PATH",
        help=(
            "hash-bound authorized-upload-public-verify.v2 receipt for an "
            "already-published same-BV title; repeat per candidate"
        ),
    )
    parser.add_argument(
        "--expected-public-title-evidence-sha256",
        action="append",
        default=[],
        metavar="CANDIDATE_ID=SHA256",
    )
    return parser


def _given_end_overrides(values: list[str]) -> dict[str, int]:
    parsed: dict[str, int] = {}
    for value in values:
        candidate_id, separator, raw_ms = str(value).partition("=")
        if (
            separator != "="
            or re.fullmatch(r"[A-Za-z0-9_-]{1,96}", candidate_id) is None
            or not raw_ms.isdigit()
            or candidate_id in parsed
        ):
            raise SystemExit(f"invalid --given-end-ms: {value}")
        parsed[candidate_id] = int(raw_ms)
    return parsed


def _fingerprint_overrides(values: list[str]) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for value in values:
        candidate_id, separator, fingerprint = str(value).partition("=")
        if (
            separator != "="
            or re.fullmatch(r"[A-Za-z0-9_-]{1,96}", candidate_id) is None
            or FINGERPRINT_RX.fullmatch(fingerprint) is None
            or candidate_id in parsed
        ):
            raise SystemExit(
                f"invalid --expected-new-fingerprint-by-candidate: {value}"
            )
        parsed[candidate_id] = fingerprint
    return parsed


def _candidate_path_overrides(values: list[str]) -> dict[str, Path]:
    parsed: dict[str, Path] = {}
    for value in values:
        candidate_id, separator, raw_path = str(value).partition("=")
        if (
            separator != "="
            or re.fullmatch(r"[A-Za-z0-9_-]{1,96}", candidate_id) is None
            or not raw_path
            or candidate_id in parsed
        ):
            raise SystemExit(f"invalid candidate evidence path: {value}")
        parsed[candidate_id] = Path(raw_path)
    return parsed


def _candidate_sha256_overrides(values: list[str]) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for value in values:
        candidate_id, separator, sha256 = str(value).partition("=")
        if (
            separator != "="
            or re.fullmatch(r"[A-Za-z0-9_-]{1,96}", candidate_id) is None
            or FINGERPRINT_RX.fullmatch(sha256) is None
            or candidate_id in parsed
        ):
            raise SystemExit(f"invalid candidate evidence sha256: {value}")
        parsed[candidate_id] = sha256
    return parsed


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", args.date) is None:
        raise SystemExit("invalid --date")
    for label, value in (
        ("source state", args.expected_source_state_sha256),
        ("old pipeline", args.expected_old_fingerprint),
    ):
        if FINGERPRINT_RX.fullmatch(value) is None:
            raise SystemExit(f"invalid {label} fingerprint")
    if args.expected_new_fingerprint is not None and (
        FINGERPRINT_RX.fullmatch(args.expected_new_fingerprint) is None
    ):
        raise SystemExit("invalid new pipeline fingerprint")
    new_fingerprints_by_candidate = _fingerprint_overrides(
        args.expected_new_fingerprint_by_candidate
    )
    if bool(args.expected_new_fingerprint) == bool(
        new_fingerprints_by_candidate
    ):
        raise SystemExit(
            "provide exactly one of --expected-new-fingerprint or "
            "--expected-new-fingerprint-by-candidate"
        )

    source_base = args.source_base.resolve()
    target_base = args.target_base.resolve()
    if source_base == target_base:
        raise SystemExit("source and target recovery bases must differ")
    repo_root = Path(__file__).resolve().parents[1]
    expected_target_repo = target_base / "repo"
    if not expected_target_repo.is_dir() or (
        expected_target_repo.resolve() != repo_root.resolve()
    ):
        raise SystemExit(
            "operator must run from the exact target recovery repo"
        )
    for manifest in (
        source_base / "AUTO_UPLOAD",
        source_base / "repo" / "AUTO_UPLOAD",
        target_base / "AUTO_UPLOAD",
        target_base / "repo" / "AUTO_UPLOAD",
    ):
        if manifest.exists() or manifest.is_symlink():
            raise SystemExit(f"AUTO_UPLOAD is forbidden in recovery rerun: {manifest}")

    source_state_path = source_base / "state" / f"{args.date}.json"
    source_bytes = _regular_file_bytes(
        source_state_path, label="source recovery state"
    )
    actual_source_sha256 = _sha256(source_bytes)
    if actual_source_sha256 != args.expected_source_state_sha256:
        raise SystemExit(
            "source state hash mismatch: "
            f"expected {args.expected_source_state_sha256}, got {actual_source_sha256}"
        )
    try:
        state = json.loads(source_bytes.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"source recovery state is invalid JSON: {exc}") from exc
    if not isinstance(state, dict):
        raise SystemExit("source recovery state must be an object")

    os.environ["AUTOSLICE_BASE"] = str(target_base)
    os.environ["AUTOSLICE_REC_ROOT"] = str(target_base / "recordings")
    from scripts import free_session_autoslice as runner
    from src.autoslice.delivery_recovery import (
        RecoveryReviewRerunError,
        plan_current_talk_recovery_rerun,
    )

    given_end_ms_by_candidate = _given_end_overrides(args.given_end_ms)
    public_title_evidence = _candidate_path_overrides(
        args.public_title_evidence
    )
    public_title_evidence_sha256 = _candidate_sha256_overrides(
        args.expected_public_title_evidence_sha256
    )
    if set(public_title_evidence) != set(public_title_evidence_sha256):
        raise SystemExit(
            "public title evidence paths and hashes must name the same candidates"
        )
    from src.autoslice.recovery_title_authority import (
        RecoveryTitleAuthorityError,
        build_recovery_title_authority,
    )

    recovery_title_authorities: dict[str, dict[str, object]] = {}
    try:
        for candidate_id, evidence_path in public_title_evidence.items():
            recovery_title_authorities[candidate_id] = (
                build_recovery_title_authority(
                    candidate_id=candidate_id,
                    evidence_path=(
                        evidence_path
                        if evidence_path.is_absolute()
                        else repo_root / evidence_path
                    ),
                    expected_evidence_sha256=(
                        public_title_evidence_sha256[candidate_id]
                    ),
                    repo_root=repo_root,
                )
            )
    except RecoveryTitleAuthorityError as exc:
        raise SystemExit(str(exc)) from exc
    try:
        plan = plan_current_talk_recovery_rerun(
            args.date,
            state,
            candidate_ids=args.candidate_ids,
            expected_source_state_sha256=args.expected_source_state_sha256,
            expected_old_fingerprint=args.expected_old_fingerprint,
            expected_new_fingerprint=args.expected_new_fingerprint,
            expected_new_fingerprints_by_candidate=(
                new_fingerprints_by_candidate
            ),
            suppressed_candidate_ids=args.suppressed_candidate_ids,
            replacement_candidate_ids=args.replacement_candidate_ids,
            user_suppression_authority=args.suppression_authority,
            replacement_selection_authority=(
                args.replacement_selection_authority
            ),
            given_end_ms_by_candidate=given_end_ms_by_candidate,
            given_end_authority=args.given_end_authority,
            recovery_title_authorities_by_candidate=(
                recovery_title_authorities
            ),
        )
    except RecoveryReviewRerunError as exc:
        raise SystemExit(str(exc)) from exc
    if runner.BASE.resolve() != target_base:
        raise SystemExit("runner did not bind the target recovery base")
    state["updated_at"] = time.strftime(
        "%Y-%m-%dT%H:%M:%S%z", time.localtime()
    )
    target_state_path = target_base / "state" / f"{args.date}.json"
    target_bytes = (
        json.dumps(state, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    )
    _atomic_create(target_state_path, target_bytes)
    receipt = {
        **plan,
        "source_base": str(source_base),
        "target_base": str(target_base),
        "target_state_path": str(target_state_path),
        "target_state_sha256": _sha256(target_bytes),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    receipt_path = (
        target_base
        / "reports"
        / f"recovery-review-rerun-plan-{args.date}.json"
    )
    _atomic_create(
        receipt_path,
        json.dumps(
            receipt, ensure_ascii=False, indent=2, sort_keys=True
        ).encode("utf-8")
        + b"\n",
    )
    print(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
