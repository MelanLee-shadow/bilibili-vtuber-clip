#!/usr/bin/env python3
"""Create-only rebind of one source spec to a receipt-backed scorecard.

The source spec and rescore receipt are immutable inputs.  This command never
edits either one and never overwrites output paths.  It emits a new spec with a
self-contained provenance block plus a deterministic rebind receipt.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
from copy import deepcopy
from pathlib import Path
from typing import Mapping, Sequence

from src.autoslice.jingting_chunker import parse_srt_cues
from src.autoslice.source_fact_rescore_provenance import (
    PROVENANCE_FIELD,
    SPEC_REBIND_RECEIPT_SCHEMA,
    SourceFactRescoreProvenanceError,
    build_rescore_provenance,
    bytes_sha256,
    canonical_sha256,
    load_committed_correction_authority,
    normalize_sha256,
    validate_rebound_spec_provenance,
)


ROOT = Path(__file__).resolve().parents[1]


class SourceFactRescoreBindError(RuntimeError):
    """The immutable rescore spec rebind could not be proven safe."""


def _regular_file(path: Path, *, label: str) -> tuple[Path, bytes]:
    absolute = path.absolute()
    try:
        metadata = absolute.lstat()
        resolved = absolute.resolve(strict=True)
    except OSError as exc:
        raise SourceFactRescoreBindError(f"{label} is missing: {absolute}") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or absolute.is_symlink()
        or resolved != absolute
    ):
        raise SourceFactRescoreBindError(
            f"{label} must be a regular non-symlink path: {absolute}"
        )
    return resolved, resolved.read_bytes()


def _bound_json(
    path: Path,
    expected_sha256: str,
    *,
    label: str,
) -> tuple[Path, bytes, dict[str, object]]:
    resolved, raw = _regular_file(path, label=label)
    expected = normalize_sha256(expected_sha256, label=f"expected {label} SHA-256")
    observed = bytes_sha256(raw)
    if observed != expected:
        raise SourceFactRescoreBindError(
            f"{label} bytes drifted: expected {expected}, observed {observed}"
        )
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise SourceFactRescoreBindError(f"{label} is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise SourceFactRescoreBindError(f"{label} must contain one JSON object")
    return resolved, raw, value


def _output_target(path: Path, *, label: str) -> Path:
    absolute = path.absolute()
    try:
        absolute.lstat()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise SourceFactRescoreBindError(f"{label} cannot be inspected") from exc
    else:
        raise SourceFactRescoreBindError(f"{label} already exists (create-only)")
    parent = absolute.parent
    try:
        metadata = parent.lstat()
        resolved_parent = parent.resolve(strict=True)
    except OSError as exc:
        raise SourceFactRescoreBindError(f"{label} parent is missing") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or parent.is_symlink()
        or resolved_parent != parent
    ):
        raise SourceFactRescoreBindError(
            f"{label} parent must be an existing non-symlink directory"
        )
    return absolute


def _json_bytes(value: Mapping[str, object]) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            indent=2,
        )
        + "\n"
    ).encode("utf-8")


def _create_file(path: Path, payload: bytes, *, label: str) -> None:
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise SourceFactRescoreBindError(
            f"{label} already exists (create-only): {path}"
        ) from exc
    try:
        with os.fdopen(descriptor, "wb") as sink:
            written = sink.write(payload)
            sink.flush()
            os.fsync(sink.fileno())
        if written != len(payload):
            raise SourceFactRescoreBindError(f"short write while creating {label}")
        parent_descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent_descriptor)
        finally:
            os.close(parent_descriptor)
    except Exception:
        # Leave a partial create-only artifact in place as failure evidence.
        raise


def _verify_reviewed_srt_asset(
    *,
    repo_root: Path,
    authority: Mapping[str, object],
) -> None:
    reviewed = authority.get("reviewed_final_srt")
    assert isinstance(reviewed, Mapping)
    path = repo_root / str(reviewed["repo_path"])
    _resolved, raw = _regular_file(path, label="committed reviewed final SRT")
    if bytes_sha256(raw) != reviewed.get("sha256"):
        raise SourceFactRescoreBindError("committed reviewed final SRT bytes drifted")
    try:
        cues = parse_srt_cues(raw.decode("utf-8"))
    except UnicodeError as exc:
        raise SourceFactRescoreBindError(
            "committed reviewed final SRT is not UTF-8"
        ) from exc
    if len(cues) != reviewed.get("cue_count"):
        raise SourceFactRescoreBindError(
            "committed reviewed final SRT cue count drifted"
        )


def bind_rescored_spec(
    *,
    source_spec: Path,
    source_spec_sha256: str,
    rescore_receipt: Path,
    rescore_receipt_sha256: str,
    correction_authority: Path,
    correction_authority_sha256: str,
    output_spec: Path,
    output_rebind_receipt: Path,
    repo_root: Path = ROOT,
) -> tuple[dict[str, object], dict[str, object]]:
    """Validate every input and create a rebound spec plus audit receipt."""

    source_path, source_raw, spec = _bound_json(
        source_spec, source_spec_sha256, label="source spec"
    )
    receipt_path, receipt_raw, receipt = _bound_json(
        rescore_receipt, rescore_receipt_sha256, label="rescore receipt"
    )
    authority_path, authority_raw, authority_from_file = _bound_json(
        correction_authority,
        correction_authority_sha256,
        label="correction authority",
    )
    candidate_id = str(spec.get("candidate_id") or "")
    try:
        authority, committed_path, committed_file_sha = (
            load_committed_correction_authority(
                repo_root=repo_root,
                candidate_id=candidate_id,
            )
        )
    except (OSError, SourceFactRescoreProvenanceError) as exc:
        raise SourceFactRescoreBindError(str(exc)) from exc
    if (
        authority_path != committed_path
        or bytes_sha256(authority_raw) != committed_file_sha
        or authority_from_file != authority
    ):
        raise SourceFactRescoreBindError(
            "correction authority is not the committed candidate asset"
        )
    _verify_reviewed_srt_asset(repo_root=repo_root, authority=authority)
    if PROVENANCE_FIELD in spec:
        raise SourceFactRescoreBindError("source spec is already rebound")

    try:
        provenance = build_rescore_provenance(
            source_spec=spec,
            source_spec_file_sha256=bytes_sha256(source_raw),
            authority=authority,
            authority_file_sha256=committed_file_sha,
            receipt=receipt,
            receipt_file_sha256=bytes_sha256(receipt_raw),
        )
    except SourceFactRescoreProvenanceError as exc:
        raise SourceFactRescoreBindError(str(exc)) from exc

    rebound_spec = deepcopy(spec)
    rebound_spec["selection_scorecard"] = deepcopy(receipt["selection_scorecard"])
    rebound_spec[PROVENANCE_FIELD] = provenance
    try:
        validate_rebound_spec_provenance(rebound_spec, repo_root=repo_root)
    except SourceFactRescoreProvenanceError as exc:
        raise SourceFactRescoreBindError(str(exc)) from exc
    rebound_spec_bytes = _json_bytes(rebound_spec)

    rebind_receipt: dict[str, object] = {
        "schema_version": SPEC_REBIND_RECEIPT_SCHEMA,
        "status": "REBOUND_CREATE_ONLY",
        "candidate_id": candidate_id,
        "source_spec": {
            "path": str(source_path),
            "file_sha256": bytes_sha256(source_raw),
            "canonical_sha256": canonical_sha256(spec),
        },
        "rescore_receipt": {
            "path": str(receipt_path),
            "file_sha256": bytes_sha256(receipt_raw),
            "output_sha256": receipt["output_sha256"],
        },
        "correction_authority": {
            "repo_path": provenance["correction_authority_repo_path"],
            "file_sha256": committed_file_sha,
            "authority_sha256": authority["authority_sha256"],
        },
        "rebound_spec": {
            "path": str(output_spec.absolute()),
            "file_sha256": bytes_sha256(rebound_spec_bytes),
            "canonical_sha256": canonical_sha256(rebound_spec),
        },
        "old_selection_scorecard_sha256": authority[
            "stale_selection_scorecard_sha256"
        ],
        "new_selection_scorecard_sha256": receipt[
            "selection_scorecard_sha256"
        ],
        "rescore_provenance_sha256": provenance["provenance_sha256"],
        "authority": {
            "runner_state_mutated": False,
            "publication_manifest": False,
        },
    }
    rebind_receipt["receipt_sha256"] = canonical_sha256(rebind_receipt)
    rebind_receipt_bytes = _json_bytes(rebind_receipt)

    output_spec_path = _output_target(output_spec, label="output spec")
    output_receipt_path = _output_target(
        output_rebind_receipt, label="output rebind receipt"
    )
    if output_spec_path == output_receipt_path:
        raise SourceFactRescoreBindError("output spec and receipt paths must differ")
    # Receipt last: its success status must never exist unless the exact spec
    # it describes was durably created first.  A crash/race can leave a
    # provenance-bearing spec without the convenience receipt, but never a
    # false success receipt for an absent or attacker-owned spec path.
    _create_file(output_spec_path, rebound_spec_bytes, label="rebound spec")
    _create_file(output_receipt_path, rebind_receipt_bytes, label="rebind receipt")
    return rebound_spec, rebind_receipt


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-spec", type=Path, required=True)
    parser.add_argument("--source-spec-sha256", required=True)
    parser.add_argument("--rescore-receipt", type=Path, required=True)
    parser.add_argument("--rescore-receipt-sha256", required=True)
    parser.add_argument("--correction-authority", type=Path, required=True)
    parser.add_argument("--correction-authority-sha256", required=True)
    parser.add_argument("--output-spec", type=Path, required=True)
    parser.add_argument("--output-rebind-receipt", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        rebound, receipt = bind_rescored_spec(
            source_spec=args.source_spec,
            source_spec_sha256=args.source_spec_sha256,
            rescore_receipt=args.rescore_receipt,
            rescore_receipt_sha256=args.rescore_receipt_sha256,
            correction_authority=args.correction_authority,
            correction_authority_sha256=args.correction_authority_sha256,
            output_spec=args.output_spec,
            output_rebind_receipt=args.output_rebind_receipt,
        )
    except SourceFactRescoreBindError as exc:
        raise SystemExit(str(exc)) from exc
    print(
        json.dumps(
            {
                "status": "REBOUND_CREATE_ONLY",
                "candidate_id": rebound["candidate_id"],
                "output_spec": str(args.output_spec.absolute()),
                "output_spec_sha256": receipt["rebound_spec"]["file_sha256"],
                "rebind_receipt": str(args.output_rebind_receipt.absolute()),
                "rebind_receipt_sha256": receipt["receipt_sha256"],
            },
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
