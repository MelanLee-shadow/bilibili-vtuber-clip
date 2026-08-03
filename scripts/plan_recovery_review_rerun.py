#!/usr/bin/env python3
"""Create a hash-bound, no-upload queue for rerunning CURRENT review talks.

This operator is deliberately separate from cron.  It reads one immutable
source recovery state and creates a new target recovery state exactly once;
the normal unattended runner owns every subsequent production transition.
"""

from __future__ import annotations

import argparse
import copy
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
SUPPORTED_SOURCE_RERUN_PLAN_SCHEMAS = frozenset(
    {
        "recovery-review-talk-rerun-plan.v5",
        "recovery-review-talk-rerun-plan.v6",
        "recovery-review-talk-rerun-plan.v7",
    }
)


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


def _bind_external_cpa_env(
    *, source_base: Path, target_base: Path
) -> dict[str, str]:
    """Expose the production CPA authority without copying its secret bytes.

    An isolated recovery base changes ``AUTOSLICE_BASE``, so the runner's
    default ``BASE/cpa.env`` lookup would otherwise become a false provider
    outage.  A create-only symlink preserves the external authority and keeps
    the recovery base free of copied credentials.
    """

    source = source_base / "cpa.env"
    target = target_base / "cpa.env"
    try:
        metadata = source.lstat()
    except OSError as exc:
        raise SystemExit(
            f"source CPA environment missing: {source}: {exc}"
        ) from exc
    if stat.S_ISLNK(metadata.st_mode):
        try:
            authority = source.resolve(strict=True)
            authority_metadata = authority.stat()
        except OSError as exc:
            raise SystemExit(
                f"source CPA environment symlink is invalid: {source}: {exc}"
            ) from exc
    else:
        authority = source
        authority_metadata = metadata
    if not stat.S_ISREG(authority_metadata.st_mode):
        raise SystemExit(
            "source CPA environment must resolve to a regular file: "
            f"{source}"
        )
    if target.exists() or target.is_symlink():
        raise SystemExit(f"target CPA environment already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        target.symlink_to(authority)
    except FileExistsError as exc:
        raise SystemExit(
            f"target CPA environment already exists: {target}"
        ) from exc
    return {
        "schema_version": "recovery-external-cpa-env-binding.v1",
        "status": "BOUND",
        "binding": "SYMLINK_EXTERNAL_AUTHORITY",
        "source_path": str(source),
        "resolved_authority_path": str(authority),
        "target_path": str(target),
    }


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
        "--project-single-published-repair",
        action="store_true",
        help=(
            "project exactly one CURRENT+COMPLIANT published delivery from an "
            "ordinary daily state or a valid exact recovery state into a new "
            "no-upload RECOVERY_REVIEW base before planning its full rerun; "
            "other source rows are evidence-only exclusions, not suppressions"
        ),
    )
    parser.add_argument(
        "--target-recordings-root",
        type=Path,
        help=(
            "existing regular recording tree used by the isolated target; "
            "allowed only with --project-single-published-repair"
        ),
    )
    parser.add_argument(
        "--suppress-candidate-id",
        action="append",
        default=[],
        dest="suppressed_candidate_ids",
        help="CURRENT candidate explicitly excluded by 维护者 instead of rerun",
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
        help="维护者 authority for choosing a replacement over the score baseline",
    )
    parser.add_argument(
        "--publication-authority-asset",
        type=Path,
        required=True,
        help=(
            "committed, deployable registry binding every queued candidate "
            "to its existing BV identity and reviewed title source"
        ),
    )
    parser.add_argument(
        "--expected-publication-authority-sha256",
        required=True,
        metavar="SHA256",
    )
    return parser


def _project_single_published_repair_state(
    state: dict,
    *,
    candidate_id: str,
    source_state_sha256: str,
    delivered_statuses: set[str] | frozenset[str],
) -> dict:
    """Isolate one published daily delivery without mutating its source state.

    Ordinary successful packages intentionally stay stable across broad
    pipeline changes.  A reported one-clip incident still needs a whole-clip
    rerun, but treating every other CURRENT delivery as user-suppressed changes
    their lifecycle semantics.  This projection creates a separate exact
    recovery base containing only the named delivery and records every excluded
    active row for audit.
    """

    if (
        not isinstance(state, dict)
        or state.get("upload_allowed") is not False
        or FINGERPRINT_RX.fullmatch(source_state_sha256) is None
    ):
        raise SystemExit(
            "single published repair requires a no-upload source state"
        )
    selection_contract = state.get("talk_selection_contract")
    rerun_plan = state.get("delivery_rerun_plan")
    source_contract_sha256 = None
    if selection_contract is None and rerun_plan is None:
        source_state_kind = "ORDINARY_NO_UPLOAD"
    elif (
        state.get("run_mode") == "RECOVERY_REVIEW"
        and isinstance(selection_contract, dict)
        and selection_contract.get("schema_version")
        == "talk-selection-contract.v1"
        and selection_contract.get("mode")
        == "EXACT_CANDIDATE_SET_NO_BACKFILL"
        and isinstance(selection_contract.get("candidate_ids"), list)
        and candidate_id in selection_contract["candidate_ids"]
        and len(selection_contract["candidate_ids"])
        == len(set(selection_contract["candidate_ids"]))
        and isinstance(rerun_plan, dict)
        and rerun_plan.get("schema_version")
        in SUPPORTED_SOURCE_RERUN_PLAN_SCHEMAS
        and rerun_plan.get("talk_selection_contract")
        == selection_contract
    ):
        source_state_kind = "EXACT_RECOVERY_REVIEW"
        source_contract_sha256 = _sha256(
            json.dumps(
                selection_contract,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
    else:
        raise SystemExit(
            "single published repair source recovery contract is invalid"
        )
    picks = state.get("picks")
    pending = state.get("pending_talk")
    if not isinstance(picks, list) or not isinstance(pending, list):
        raise SystemExit("single published repair source talk state is invalid")

    matching = [
        row
        for row in picks
        if isinstance(row, dict)
        and str(row.get("candidate_id") or row.get("cid") or "")
        == candidate_id
        and row.get("status") in delivered_statuses
        and row.get("bundle_lifecycle") == "CURRENT"
        and row.get("bundle_compliance") == "COMPLIANT"
        and row.get("rc") == 0
    ]
    if len(matching) != 1:
        raise SystemExit(
            "single published repair requires exactly one CURRENT+COMPLIANT "
            f"delivery: {candidate_id}"
        )
    pending_ids = sorted(
        {
            str(row.get("candidate_id") or row.get("cid") or "")
            for row in pending
            if isinstance(row, dict)
            and str(row.get("candidate_id") or row.get("cid") or "")
        }
    )
    if candidate_id in pending_ids:
        raise SystemExit(
            "single published repair candidate is already pending in source state"
        )

    projected = copy.deepcopy(state)
    excluded_pick_ids = sorted(
        {
            str(row.get("candidate_id") or row.get("cid") or "")
            for row in picks
            if isinstance(row, dict)
            and str(row.get("candidate_id") or row.get("cid") or "")
            and str(row.get("candidate_id") or row.get("cid") or "")
            != candidate_id
        }
    )
    projected["run_mode"] = "RECOVERY_REVIEW"
    projected["upload_allowed"] = False
    projected["status"] = "single_published_repair_projected"
    projected["picks"] = [copy.deepcopy(matching[0])]
    projected["pending_talk"] = []
    projected["talk_backlog"] = []
    projected["talk_superseded_attempts"] = [
        copy.deepcopy(row)
        for row in state.get("talk_superseded_attempts", [])
        if isinstance(row, dict)
        and str(row.get("candidate_id") or row.get("cid") or "")
        == candidate_id
    ]
    for key in (
        "songs",
        "pending_song",
        "song_backlog",
        "song_superseded_attempts",
        "song_selection_backlog",
    ):
        projected[key] = []
    for key in (
        "exact_talk_contract_closure",
        "talk_selection_contract",
        "delivery_rerun_plan",
        "next_retry_at",
        "next_retry_at_epoch",
    ):
        projected.pop(key, None)
    projected["single_published_repair_projection"] = {
        "schema_version": "single-published-talk-repair-projection.v1",
        "candidate_id": candidate_id,
        "source_state_sha256": source_state_sha256,
        "source_run_mode": state.get("run_mode"),
        "source_state_kind": source_state_kind,
        "source_talk_selection_contract_sha256": source_contract_sha256,
        "excluded_pick_candidate_ids": excluded_pick_ids,
        "excluded_pending_candidate_ids": pending_ids,
        "excluded_rows_disposition": "SOURCE_STATE_UNCHANGED_OUTSIDE_REPAIR_TARGET",
    }
    return projected


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


def _load_recovery_publication_contract(
    *,
    queued_candidate_ids: set[str],
    publication_asset: Path,
    expected_publication_authority_sha256: str,
    repo_root: Path,
) -> tuple[dict[str, dict[str, object]], dict[str, int], str]:
    from src.autoslice.recovery_title_authority import (
        RecoveryTitleAuthorityError,
        build_recovery_publication_authorities,
    )

    try:
        authorities = build_recovery_publication_authorities(
            candidate_ids=queued_candidate_ids,
            registry_path=(
                publication_asset
                if publication_asset.is_absolute()
                else repo_root / publication_asset
            ),
            expected_registry_sha256=(
                expected_publication_authority_sha256
            ),
            require_exact_candidate_set=True,
            repo_root=repo_root,
        )
    except RecoveryTitleAuthorityError as exc:
        raise SystemExit(str(exc)) from exc
    given_end_ms_by_candidate = {
        candidate_id: int(authority["required_given_end_ms"])
        for candidate_id, authority in authorities.items()
    }
    given_end_authorities = {
        str(authority["registry_authority"])
        for authority in authorities.values()
    }
    if len(given_end_authorities) != 1:
        raise SystemExit("RECOVERY_PUBLICATION_GIVEN_END_AUTHORITY_INVALID")
    return (
        authorities,
        given_end_ms_by_candidate,
        next(iter(given_end_authorities)),
    )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", args.date) is None:
        raise SystemExit("invalid --date")
    for label, value in (
        ("source state", args.expected_source_state_sha256),
        ("old pipeline", args.expected_old_fingerprint),
        (
            "publication authority",
            args.expected_publication_authority_sha256,
        ),
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
    if args.target_recordings_root is not None and (
        not args.project_single_published_repair
    ):
        raise SystemExit(
            "--target-recordings-root requires "
            "--project-single-published-repair"
        )
    target_recordings_root = (
        args.target_recordings_root.resolve()
        if args.target_recordings_root is not None
        else target_base / "recordings"
    )
    if (
        not target_recordings_root.is_absolute()
        or not target_recordings_root.is_dir()
        or target_recordings_root.is_symlink()
    ):
        raise SystemExit(
            f"target recordings root must be a regular directory: "
            f"{target_recordings_root}"
        )
    if args.project_single_published_repair:
        visible_recording_dates = sorted(
            entry.name
            for entry in target_recordings_root.iterdir()
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", entry.name)
            and entry.is_dir()
        )
        if visible_recording_dates != [args.date]:
            raise SystemExit(
                "single published repair target recordings root must expose "
                f"only {args.date}; observed {visible_recording_dates}"
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
    os.environ["AUTOSLICE_REC_ROOT"] = str(target_recordings_root)
    from scripts import session_autoslice as runner
    from src.autoslice.delivery_recovery import (
        RecoveryReviewRerunError,
        plan_current_talk_recovery_rerun,
    )

    if args.project_single_published_repair:
        if (
            len(args.candidate_ids) != 1
            or args.suppressed_candidate_ids
            or args.replacement_candidate_ids
            or args.suppression_authority
            or args.replacement_selection_authority
        ):
            raise SystemExit(
                "--project-single-published-repair requires exactly one "
                "--candidate-id and forbids suppression/replacement options"
            )
        state = _project_single_published_repair_state(
            state,
            candidate_id=args.candidate_ids[0],
            source_state_sha256=args.expected_source_state_sha256,
            delivered_statuses=runner.DELIVERED_TALK_STATUSES,
        )

    queued_candidate_ids = set(args.candidate_ids) | set(
        args.replacement_candidate_ids
    )
    (
        recovery_publication_authorities,
        given_end_ms_by_candidate,
        given_end_authority,
    ) = _load_recovery_publication_contract(
        queued_candidate_ids=queued_candidate_ids,
        publication_asset=args.publication_authority_asset,
        expected_publication_authority_sha256=(
            args.expected_publication_authority_sha256
        ),
        repo_root=repo_root,
    )
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
            given_end_authority=given_end_authority,
            recovery_publication_authorities_by_candidate=(
                recovery_publication_authorities
            ),
        )
    except RecoveryReviewRerunError as exc:
        raise SystemExit(str(exc)) from exc
    if runner.BASE.resolve() != target_base:
        raise SystemExit("runner did not bind the target recovery base")
    state["updated_at"] = time.strftime(
        "%Y-%m-%dT%H:%M:%S%z", time.localtime()
    )
    external_cpa_env = None
    if args.project_single_published_repair:
        external_cpa_env = _bind_external_cpa_env(
            source_base=source_base,
            target_base=target_base,
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
        "target_recordings_root": str(target_recordings_root),
        "target_state_path": str(target_state_path),
        "target_state_sha256": _sha256(target_bytes),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if external_cpa_env is not None:
        receipt["external_cpa_env"] = external_cpa_env
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
