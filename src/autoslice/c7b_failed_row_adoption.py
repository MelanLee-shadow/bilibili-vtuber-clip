"""The single sealed C7b failed-row adoption exception.

This module is deliberately narrower than every generic revive/import path.  It
only waives the source-state class mismatch for the one C7b row; all replay,
artifact, QC, and state-last CAS gates remain owned by the normal replay lane.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Mapping, Sequence
from pathlib import Path

from src.autoslice.repository_asset_authority import (
    RepositoryAssetAuthority,
    require_repository_asset_authority,
)

SCHEMA = "c7b-failed-row-adoption-receipt.v1"
CANDIDATE_ID = "auto_130040_201_255"
RECORDING_DATE = "2026-08-14"
RECEIPT_RELATIVE_PATH = Path(
    "assets/lidousha/fastlane_c7b_private/"
    "auto_130040_201_255.failed-row-adoption-receipt.v1.json"
)

class C7bFailedRowAdoptionError(ValueError):
    """The exact C7b failed-row adoption receipt cannot be consumed."""


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def canonical_sha256(value: object) -> str:
    return "sha256:" + hashlib.sha256(canonical_bytes(value)).hexdigest()


def row_fingerprint(row: Mapping[str, object]) -> str:
    """Fingerprint the complete row, not a selected failure subset."""
    return canonical_sha256(row)


def _sha(value: object, *, label: str) -> str:
    if not isinstance(value, str) or len(value.removeprefix("sha256:")) != 64:
        raise C7bFailedRowAdoptionError(f"C7B_ADOPTION_{label}_INVALID")
    raw = value.removeprefix("sha256:")
    if any(char not in "0123456789abcdef" for char in raw):
        raise C7bFailedRowAdoptionError(f"C7B_ADOPTION_{label}_INVALID")
    return "sha256:" + raw


def _mapping(value: object, *, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise C7bFailedRowAdoptionError(f"C7B_ADOPTION_{label}_INVALID")
    return value


_PRIVATE_AUTHORITY_RELATIVE = Path(
    "assets/lidousha/fastlane_c7b_private/"
    "auto_130040_201_255.private-authority.v1.json"
)
_CLOSURE_RELATIVE = Path(
    "assets/lidousha/fastlane_c7b_private/"
    "auto_130040_201_255.freeze-closure.v1.json"
)
_DECISION_RELATIVE = Path(
    "assets/lidousha/fastlane_c7b_private/"
    "auto_130040_201_255.decisions.v3.json"
)
_DIFF_RELATIVE = Path(
    "assets/lidousha/fastlane_c7b_private/"
    "auto_130040_201_255.diff.json"
)
_SOURCE_RECONCILIATION_RELATIVE = Path(
    "assets/lidousha/fastlane_c7b_private/"
    "auto_130040_201_255.source-media-reconciliation-authority.v1.json"
)
_PIPELINE_RELATIVE = Path(
    "assets/lidousha/fastlane_c7b_private/"
    "auto_130040_201_255.pipeline-diagnostic.srt"
)
_PREDECESSOR_RELATIVE = Path(
    "assets/lidousha/fastlane_c7b_private/predecessor/"
    "auto_130040_201_255.recut.srt"
)
_TARGET_RELATIVE = Path(
    "assets/lidousha/fastlane_c7b_private/"
    "auto_130040_201_255.reviewed.srt"
)
_BASELINE_MANIFEST_RELATIVE = Path(
    "assets/lidousha/reviewed_subtitle_baselines/"
    "auto_130040_201_255.subtitle-baseline.v1.json"
)
_BASELINE_TARGET_RELATIVE = Path(
    "assets/lidousha/reviewed_subtitle_baselines/"
    "auto_130040_201_255.reviewed.srt"
)
_BASELINE_DECISION_RELATIVE = Path(
    "assets/lidousha/reviewed_subtitle_baselines/"
    "auto_130040_201_255.operator-decisions.v3.json"
)
_BASELINE_DIFF_RELATIVE = Path(
    "assets/lidousha/reviewed_subtitle_baselines/"
    "auto_130040_201_255.operator-truth-diff.v2.json"
)
_BASELINE_PIPELINE_RELATIVE = Path(
    "assets/lidousha/reviewed_subtitle_baselines/"
    "auto_130040_201_255.pipeline-diagnostic.srt"
)
_RULING_RELATIVE = Path("docs/reviews/2026-08-19-ivan-review-batch-rulings.md")

_C7B_BASELINE_SHA256 = "sha256:e07e2e23d2eacebbeb95a4700c7fa4005fb4342a432c28d0adde1d4e68e98457"
_C7B_PRIVATE_AUTHORITY_SHA256 = "sha256:bf46c6d6ae743bfb7488c3387b79e5eb187182915284ecb3856569f9552b7d77"
_C7B_RECEIPT_SELF_SHA256 = "sha256:e8e29bf1466ed31035d66e9df4f1cea80f61e921b2142398e863f98e80d34db8"
_C7B_OPERATOR_AUTHORITY = {
    "kind": "IVAN_OPERATOR",
    "evidence_ref": "docs/reviews/2026-08-19-ivan-review-batch-rulings.md#line947-row7b",
}


def _safe_chain_bytes(*, repo_root: Path, relative: Path, label: str) -> bytes:
    """Read one fixed chain file through one stable, no-follow descriptor."""
    try:
        root = repo_root.resolve(strict=True)
    except OSError as exc:
        raise C7bFailedRowAdoptionError(f"C7B_ADOPTION_{label}_UNAVAILABLE") from exc
    if repo_root.is_symlink() or not root.is_dir() or relative.is_absolute() or ".." in relative.parts:
        raise C7bFailedRowAdoptionError(f"C7B_ADOPTION_{label}_PATH_INVALID")
    path = root / relative
    cursor = root
    try:
        for part in relative.parts:
            cursor = cursor / part
            item = os.lstat(cursor)
            if stat.S_ISLNK(item.st_mode) or not stat.S_ISDIR(item.st_mode) and cursor != path:
                raise C7bFailedRowAdoptionError(f"C7B_ADOPTION_{label}_PATH_INVALID")
        before = os.lstat(path)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or not stat.S_IMODE(before.st_mode) & 0o400 or stat.S_IMODE(before.st_mode) & 0o022:
            raise C7bFailedRowAdoptionError(f"C7B_ADOPTION_{label}_PERMISSION_INVALID")
        identity = (before.st_dev, before.st_ino, stat.S_IMODE(before.st_mode), before.st_size, before.st_mtime_ns, before.st_ctime_ns)
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
    except C7bFailedRowAdoptionError:
        raise
    except OSError as exc:
        raise C7bFailedRowAdoptionError(f"C7B_ADOPTION_{label}_UNAVAILABLE") from exc
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1 or (
            opened.st_dev, opened.st_ino, stat.S_IMODE(opened.st_mode), opened.st_size,
            opened.st_mtime_ns, opened.st_ctime_ns
        ) != identity:
            raise C7bFailedRowAdoptionError(f"C7B_ADOPTION_{label}_DRIFT")
        chunks: list[bytes] = []
        while chunk := os.read(fd, 1024 * 1024):
            chunks.append(chunk)
        after_fd = os.fstat(fd)
    except OSError as exc:
        raise C7bFailedRowAdoptionError(f"C7B_ADOPTION_{label}_DRIFT") from exc
    finally:
        os.close(fd)
    try:
        after = os.lstat(path)
    except OSError as exc:
        raise C7bFailedRowAdoptionError(f"C7B_ADOPTION_{label}_DRIFT") from exc
    after_identity = (after.st_dev, after.st_ino, stat.S_IMODE(after.st_mode), after.st_size, after.st_mtime_ns, after.st_ctime_ns)
    if after_identity != identity or (
        after_fd.st_dev, after_fd.st_ino, stat.S_IMODE(after_fd.st_mode), after_fd.st_size,
        after_fd.st_mtime_ns, after_fd.st_ctime_ns
    ) != identity:
        raise C7bFailedRowAdoptionError(f"C7B_ADOPTION_{label}_DRIFT")
    return b"".join(chunks)


def _chain_json(*, repo_root: Path, relative: Path, label: str, expected_sha256: str | None = None) -> dict[str, object]:
    raw = _safe_chain_bytes(repo_root=repo_root, relative=relative, label=label)
    if expected_sha256 is not None and "sha256:" + hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise C7bFailedRowAdoptionError(f"C7B_ADOPTION_{label}_HASH_DRIFT")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise C7bFailedRowAdoptionError(f"C7B_ADOPTION_{label}_INVALID") from exc
    if not isinstance(value, dict):
        raise C7bFailedRowAdoptionError(f"C7B_ADOPTION_{label}_INVALID")
    return value


def _chain_digest(*, repo_root: Path, relative: Path, label: str) -> str:
    raw = _safe_chain_bytes(repo_root=repo_root, relative=relative, label=label)
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _sealed_c7b_chain(*, repo_root: Path, config: Mapping[str, object], baseline: Sequence[object], candidate_id: str | None, recording_date: str | None) -> dict[str, object]:
    if (candidate_id, recording_date) != (CANDIDATE_ID, RECORDING_DATE):
        raise C7bFailedRowAdoptionError("C7B_ADOPTION_IDENTITY_DRIFT")
    if config.get("schema_version") != "subtitle-redelivery-baseline.v2" or config.get("mode") != "preserve_text_outside_source_truth" or config.get("exact_interval_replay") is not True:
        raise C7bFailedRowAdoptionError("C7B_ADOPTION_BASELINE_INVALID")
    if config.get("sha256") != _C7B_BASELINE_SHA256.removeprefix("sha256:") or config.get("source_recording_basename") != "22966160_20260814-13-00-40.mp4" or config.get("source_sha256") != "660f609ca46cf9b6a5d7618df290ffd8cf32e54677813be343067719d8616b54" or config.get("absolute_source_start_ms") != 191190 or config.get("absolute_source_end_ms") != 303140:
        raise C7bFailedRowAdoptionError("C7B_ADOPTION_BASELINE_INVALID")
    pin = config.get("operator_text_full_ownership")
    if not isinstance(pin, Mapping) or pin != {
        "schema_version": "operator-reviewed-text-full-ownership-pin.v3",
        "baseline_sha256": _C7B_BASELINE_SHA256.removeprefix("sha256:"),
        "pipeline_srt_sha256": "810a0f17e658184029543910dc8e1c22e1166f03edf7ca90afb82943b761bd94",
        "decision_ledger_sha256": "f91c50c410f265b472e32f4f7f57696876e75b0c82d2e527b62292cfa3ff0aca",
        "diagnostic_diff_sha256": "ea5b986c58dcd8ae70b6b947c41943ba563419623b1ae08b89f8a9d52a8f7e09",
        "operator_authority": _C7B_OPERATOR_AUTHORITY,
        "source_cue_count": 36, "release_cue_count": 36, "changed_cue_count": 4,
        "operator_exact_text_cue_count": 4, "operator_unchanged_freeze_cue_count": 32,
        "operator_drop_cue_count": 0, "speaker_authority": "NOT_CLAIMED_TEXT_ONLY",
    }:
        raise C7bFailedRowAdoptionError("C7B_ADOPTION_BASELINE_INVALID")
    receipt, _seal = load_c7b_failed_row_adoption_receipt(repo_root=repo_root)
    if receipt["canonical_self_sha256"] != _C7B_RECEIPT_SELF_SHA256:
        raise C7bFailedRowAdoptionError("C7B_ADOPTION_RECEIPT_IDENTITY_DRIFT")
    chain = receipt["authority_chain"]
    if not isinstance(chain, Mapping) or chain.get("private_authority_sha256") != _C7B_PRIVATE_AUTHORITY_SHA256 or chain.get("freeze_closure_sha256") != "sha256:916eb1f10ff4e8a30db0b4b0164583c4de55fa4d9a785f2b2e905c060b9398e6" or chain.get("decision_ledger_sha256") != "sha256:f91c50c410f265b472e32f4f7f57696876e75b0c82d2e527b62292cfa3ff0aca" or chain.get("truth_diff_sha256") != "sha256:ea5b986c58dcd8ae70b6b947c41943ba563419623b1ae08b89f8a9d52a8f7e09" or chain.get("source_reconciliation_sha256") != "sha256:c6c07f47c007736c8c85ec5691c6c63eb503ab2bea4188e2dfbc6cc1cee75a97":
        raise C7bFailedRowAdoptionError("C7B_ADOPTION_AUTHORITY_CHAIN_INVALID")
    files = {
        "PRIVATE_AUTHORITY": (_PRIVATE_AUTHORITY_RELATIVE, chain["private_authority_sha256"]),
        "FREEZE_CLOSURE": (_CLOSURE_RELATIVE, chain["freeze_closure_sha256"]),
        "DECISION": (_DECISION_RELATIVE, chain["decision_ledger_sha256"]),
        "DIFF": (_DIFF_RELATIVE, chain["truth_diff_sha256"]),
        "SOURCE_RECONCILIATION": (_SOURCE_RECONCILIATION_RELATIVE, chain["source_reconciliation_sha256"]),
        "PIPELINE": (_PIPELINE_RELATIVE, "sha256:810a0f17e658184029543910dc8e1c22e1166f03edf7ca90afb82943b761bd94"),
        "PREDECESSOR": (_PREDECESSOR_RELATIVE, "sha256:810a0f17e658184029543910dc8e1c22e1166f03edf7ca90afb82943b761bd94"),
        "TARGET": (_TARGET_RELATIVE, _C7B_BASELINE_SHA256),
        "BASELINE_MANIFEST": (_BASELINE_MANIFEST_RELATIVE, "sha256:030b8f71f89a4ec4b9decb17103ed455132c64a5921943957f9faf685f7c5ade"),
        "BASELINE_TARGET": (_BASELINE_TARGET_RELATIVE, _C7B_BASELINE_SHA256),
        "BASELINE_DECISION": (_BASELINE_DECISION_RELATIVE, chain["decision_ledger_sha256"]),
        "BASELINE_DIFF": (_BASELINE_DIFF_RELATIVE, chain["truth_diff_sha256"]),
        "BASELINE_PIPELINE": (_BASELINE_PIPELINE_RELATIVE, "sha256:810a0f17e658184029543910dc8e1c22e1166f03edf7ca90afb82943b761bd94"),
    }
    loaded = {label: _chain_json(repo_root=repo_root, relative=path, label=label, expected_sha256=digest) if label not in {"PIPELINE", "PREDECESSOR", "TARGET", "BASELINE_TARGET", "BASELINE_PIPELINE"} else _chain_digest(repo_root=repo_root, relative=path, label=label) for label, (path, digest) in files.items()}
    if loaded["PIPELINE"] != files["PIPELINE"][1] or loaded["PREDECESSOR"] != files["PREDECESSOR"][1] or loaded["TARGET"] != _C7B_BASELINE_SHA256 or loaded["BASELINE_TARGET"] != _C7B_BASELINE_SHA256 or loaded["BASELINE_PIPELINE"] != files["BASELINE_PIPELINE"][1]:
        raise C7bFailedRowAdoptionError("C7B_ADOPTION_SOURCE_HASH_DRIFT")
    private = loaded["PRIVATE_AUTHORITY"]
    closure = loaded["FREEZE_CLOSURE"]
    ledger = loaded["DECISION"]
    diff = loaded["DIFF"]
    source = loaded["SOURCE_RECONCILIATION"]
    manifest = loaded["BASELINE_MANIFEST"]
    reviewed_binding = private.get("reviewed_subtitle")
    if (
        not isinstance(reviewed_binding, Mapping)
        or private.get("schema_version") != "fastlane-candidate-private-authority.v1"
        or private.get("candidate_id") != CANDIDATE_ID
        or private.get("recording_date") != RECORDING_DATE
        or reviewed_binding.get("sha256") != _C7B_BASELINE_SHA256.removeprefix("sha256:")
        or private.get("title") != "李姐也是脑控大师，但即使被脑控仍然信不了李1是怎么回事呢"
    ):
        raise C7bFailedRowAdoptionError("C7B_ADOPTION_PRIVATE_AUTHORITY_INVALID")
    unsigned = dict(closure)
    declared = unsigned.pop("canonical_self_sha256", None)
    if closure.get("schema_version") != "fastlane-c7b-freeze-closure.v1" or closure.get("candidate_id") != CANDIDATE_ID or declared != hashlib.sha256(canonical_bytes(unsigned)).hexdigest():
        raise C7bFailedRowAdoptionError("C7B_ADOPTION_FREEZE_CLOSURE_INVALID")
    if ledger.get("schema_version") != "operator-reviewed-subtitle-decisions.v3" or ledger.get("candidate_id") != CANDIDATE_ID or ledger.get("report_scope") != "EXHAUSTIVE" or ledger.get("operator_authority") != _C7B_OPERATOR_AUTHORITY or not isinstance(ledger.get("cue_decisions"), list) or len(ledger["cue_decisions"]) != 36:
        raise C7bFailedRowAdoptionError("C7B_ADOPTION_DECISION_INVALID")
    if diff.get("schema_version") != "operator-reviewed-subtitle-truth-diff.v2" or diff.get("candidate_id") != CANDIDATE_ID or diff.get("pipeline_srt_sha256") != "810a0f17e658184029543910dc8e1c22e1166f03edf7ca90afb82943b761bd94" or diff.get("release_truth_srt_sha256") != _C7B_BASELINE_SHA256.removeprefix("sha256:") or not isinstance(diff.get("rows"), list) or len(diff["rows"]) != 36:
        raise C7bFailedRowAdoptionError("C7B_ADOPTION_TRUTH_DIFF_INVALID")
    if source.get("schema_version") != "c7b-source-media-reconciliation-authority.v1" or source.get("candidate_id") != CANDIDATE_ID or source.get("recording_date") != RECORDING_DATE or source.get("upload_allowed") is not False:
        raise C7bFailedRowAdoptionError("C7B_ADOPTION_SOURCE_RECONCILIATION_INVALID")
    if manifest.get("registry_schema_version") != "candidate-reviewed-subtitle-baseline.v1" or manifest.get("candidate_id") != CANDIDATE_ID or manifest.get("sha256") != _C7B_BASELINE_SHA256.removeprefix("sha256:"):
        raise C7bFailedRowAdoptionError("C7B_ADOPTION_BASELINE_MANIFEST_INVALID")
    ruling = _safe_chain_bytes(repo_root=repo_root, relative=_RULING_RELATIVE, label="RULING").decode("utf-8")
    if "| 7b |" not in ruling or CANDIDATE_ID not in ruling or "这个切片非常好啊" not in ruling:
        raise C7bFailedRowAdoptionError("C7B_ADOPTION_RULING_INVALID")
    if len(baseline) != 36:
        raise C7bFailedRowAdoptionError("C7B_ADOPTION_TARGET_INVALID")
    return dict(_C7B_OPERATOR_AUTHORITY)


def resolve_c7b_operator_authority(*, config: Mapping[str, object], baseline: Sequence[object], spec_parent: Path, candidate_id: str | None, recording_date: str | None) -> dict[str, object]:
    """Resolve mapping authority only through the complete exact C7b chain."""
    try:
        resolved_spec_parent = spec_parent.resolve(strict=True)
    except OSError as exc:
        raise C7bFailedRowAdoptionError("C7B_ADOPTION_REPO_UNAVAILABLE") from exc
    if resolved_spec_parent.name != "reviewed_subtitle_baselines":
        raise C7bFailedRowAdoptionError("C7B_ADOPTION_SCOPE_INVALID")
    assets_dir = resolved_spec_parent.parents[1]
    if assets_dir.name != "assets" or assets_dir.is_symlink():
        raise C7bFailedRowAdoptionError("C7B_ADOPTION_SCOPE_INVALID")
    # ``assets`` must be the repository's direct child; no alternate checkout
    # or symlinked authority root is accepted.
    repo_root = assets_dir.parent
    return _sealed_c7b_chain(repo_root=repo_root, config=config, baseline=baseline, candidate_id=candidate_id, recording_date=recording_date)


def load_c7b_failed_row_adoption_receipt(
    *, repo_root: Path,
) -> tuple[dict[str, object], RepositoryAssetAuthority]:
    """Load and prove the checked-in receipt is current repository authority."""
    payload = _safe_chain_bytes(
        repo_root=repo_root, relative=RECEIPT_RELATIVE_PATH, label="RECEIPT"
    )
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise C7bFailedRowAdoptionError("C7B_ADOPTION_RECEIPT_INVALID") from exc
    if not isinstance(value, dict):
        raise C7bFailedRowAdoptionError("C7B_ADOPTION_RECEIPT_INVALID")
    try:
        seal = require_repository_asset_authority(
            repo_root=repo_root,
            relative_path=RECEIPT_RELATIVE_PATH,
            observed_bytes=payload,
        )
    except Exception as exc:
        raise C7bFailedRowAdoptionError("C7B_ADOPTION_RECEIPT_UNSEALED") from exc
    unsigned = dict(value)
    declared = unsigned.pop("canonical_self_sha256", None)
    if declared != canonical_sha256(unsigned):
        raise C7bFailedRowAdoptionError("C7B_ADOPTION_RECEIPT_SELF_SEAL_DRIFT")
    _validate_receipt(value)
    return value, seal


def _validate_receipt(value: Mapping[str, object]) -> None:
    required = {
        "schema_version", "candidate_id", "recording_date", "row_selector",
        "live_row_fingerprint", "failure_tuple", "attempt_generation",
        "source_collection", "predecessor", "ivan_ruling", "target",
        "authority_chain", "permissions", "canonical_self_sha256",
    }
    if set(value) != required or value.get("schema_version") != SCHEMA:
        raise C7bFailedRowAdoptionError("C7B_ADOPTION_RECEIPT_SCHEMA_INVALID")
    if value.get("candidate_id") != CANDIDATE_ID or value.get("recording_date") != RECORDING_DATE:
        raise C7bFailedRowAdoptionError("C7B_ADOPTION_RECEIPT_IDENTITY_DRIFT")
    selector = _mapping(value.get("row_selector"), label="ROW_SELECTOR")
    if dict(selector) != {"collection": "picks", "index": 5, "candidate_id": CANDIDATE_ID}:
        raise C7bFailedRowAdoptionError("C7B_ADOPTION_ROW_SELECTOR_INVALID")
    fingerprint = _mapping(value.get("live_row_fingerprint"), label="ROW_FINGERPRINT")
    if set(fingerprint) != {"algorithm", "canonicalization", "sha256"} or fingerprint.get("algorithm") != "sha256" or fingerprint.get("canonicalization") != "json_sort_keys_utf8_no_whitespace":
        raise C7bFailedRowAdoptionError("C7B_ADOPTION_ROW_FINGERPRINT_INVALID")
    _sha(fingerprint.get("sha256"), label="ROW_FINGERPRINT")
    failure = _mapping(value.get("failure_tuple"), label="FAILURE_TUPLE")
    expected_failure = {
        "status": "failed", "rc": 1, "failure_kind": "content_boundary",
        "failure_stage": "final_review_boundary_semantic", "failure_recoverable": False,
        "failure_fingerprint": "sha256:ba8e802ba67bd284207fd706a758924afc122577398b038eff6fbb1197dfa606",
        "failure_recovery_fingerprint": "sha256:270cd93e86e9612d4d3e3060b3cd300d3ce2c6f3a82b66f3fa99d64694a16caf",
    }
    if dict(failure) != expected_failure:
        raise C7bFailedRowAdoptionError("C7B_ADOPTION_FAILURE_TUPLE_INVALID")
    attempts = _mapping(value.get("attempt_generation"), label="ATTEMPT_GENERATION")
    expected_attempts = {
        "cover_route_regeneration_attempts": 1,
        "cover_route_regeneration_fingerprint": "sha256:4d7a8d95549b0385d3ed8917b684b1d42087bfb2dde0318f3eaea663e2e83918",
        "cover_generation_sha256": "sha256:" + "0" * 64,
    }
    # The full generation object is already covered by the complete row hash;
    # the receipt stores its digest as an explicit anti-omission witness.
    if set(attempts) != set(expected_attempts) or attempts.get("cover_route_regeneration_attempts") != 1 or attempts.get("cover_route_regeneration_fingerprint") != expected_attempts["cover_route_regeneration_fingerprint"] or attempts.get("cover_generation_sha256") != "sha256:7ef2ae295d7ab612bf5ee0f74af3571bab880dc31f6b8e9a9fe3398197aead60":
        raise C7bFailedRowAdoptionError("C7B_ADOPTION_ATTEMPT_GENERATION_INVALID")
    _sha(attempts.get("cover_generation_sha256"), label="COVER_GENERATION")
    source = _mapping(value.get("source_collection"), label="SOURCE_COLLECTION")
    if dict(source) != {
        "basename": "22966160_20260814-13-00-40.mp4",
        "sha256": "sha256:660f609ca46cf9b6a5d7618df290ffd8cf32e54677813be343067719d8616b54",
        "absolute_interval_ms": [191190, 303140],
    }:
        raise C7bFailedRowAdoptionError("C7B_ADOPTION_SOURCE_COLLECTION_INVALID")
    predecessor = _mapping(value.get("predecessor"), label="PREDECESSOR")
    expected_predecessor = {
        "record_sha256": "sha256:b875d8ddedaa47971249e3af057b218f45e62fb002ddcf6c9733cd38d5bfd8f5",
        "video_sha256": "sha256:09c42e6cab8b35891f7b307dcfd4e23323073f30915ffc36bcbe2f33acdb1798",
        "srt_sha256": "sha256:810a0f17e658184029543910dc8e1c22e1166f03edf7ca90afb82943b761bd94",
        "source_media_sha256": "sha256:660f609ca46cf9b6a5d7618df290ffd8cf32e54677813be343067719d8616b54",
        "padded_source_sha256": "sha256:5b06a7bb19e22c0a8c368c83ee83170ff068b04d32fd07cf246859e8f09014f0",
    }
    if dict(predecessor) != expected_predecessor:
        raise C7bFailedRowAdoptionError("C7B_ADOPTION_PREDECESSOR_INVALID")
    ruling = _mapping(value.get("ivan_ruling"), label="IVAN_RULING")
    if dict(ruling) != {
        "path": "docs/reviews/2026-08-19-ivan-review-batch-rulings.md",
        "ref": "line947-row7b",
    }:
        raise C7bFailedRowAdoptionError("C7B_ADOPTION_RULING_INVALID")
    target = _mapping(value.get("target"), label="TARGET")
    if dict(target) != {
        "reviewed_srt_sha256": "sha256:e07e2e23d2eacebbeb95a4700c7fa4005fb4342a432c28d0adde1d4e68e98457",
        "title": "李姐也是脑控大师，但即使被脑控仍然信不了李1是怎么回事呢",
        "changed_cue_ordinals": [10, 18, 19, 20],
        "frozen_cue_count": 32,
        "operator_drop_count": 0,
    }:
        raise C7bFailedRowAdoptionError("C7B_ADOPTION_TARGET_INVALID")
    chain = _mapping(value.get("authority_chain"), label="AUTHORITY_CHAIN")
    expected_chain = {
        "private_authority_sha256": "sha256:bf46c6d6ae743bfb7488c3387b79e5eb187182915284ecb3856569f9552b7d77",
        "freeze_closure_sha256": "sha256:916eb1f10ff4e8a30db0b4b0164583c4de55fa4d9a785f2b2e905c060b9398e6",
        "decision_ledger_sha256": "sha256:f91c50c410f265b472e32f4f7f57696876e75b0c82d2e527b62292cfa3ff0aca",
        "truth_diff_sha256": "sha256:ea5b986c58dcd8ae70b6b947c41943ba563419623b1ae08b89f8a9d52a8f7e09",
        "source_reconciliation_sha256": "sha256:c6c07f47c007736c8c85ec5691c6c63eb503ab2bea4188e2dfbc6cc1cee75a97",
    }
    if dict(chain) != expected_chain:
        raise C7bFailedRowAdoptionError("C7B_ADOPTION_AUTHORITY_CHAIN_INVALID")
    permissions = _mapping(value.get("permissions"), label="PERMISSIONS")
    if dict(permissions) != {"provider_allowed": False, "upload_allowed": False, "state_write_allowed": False, "source_state_class_waiver": True}:
        raise C7bFailedRowAdoptionError("C7B_ADOPTION_PERMISSIONS_INVALID")


def validate_c7b_failed_row_adoption(
    *, repo_root: Path, state: Mapping[str, object], state_date: str,
) -> tuple[dict[str, object], RepositoryAssetAuthority]:
    """Validate the one live row before the replay after-image is projected."""
    receipt, seal = load_c7b_failed_row_adoption_receipt(repo_root=repo_root)
    if state_date != RECORDING_DATE:
        raise C7bFailedRowAdoptionError("C7B_ADOPTION_STATE_DATE_DRIFT")
    picks = state.get("picks")
    if not isinstance(picks, list) or len(picks) <= 5:
        raise C7bFailedRowAdoptionError("C7B_ADOPTION_PICKS_INVALID")
    row = picks[5]
    if not isinstance(row, Mapping):
        raise C7bFailedRowAdoptionError("C7B_ADOPTION_ROW_INVALID")
    selector = receipt["row_selector"]
    assert isinstance(selector, Mapping)
    if dict(selector) != {"collection": "picks", "index": 5, "candidate_id": CANDIDATE_ID}:
        raise C7bFailedRowAdoptionError("C7B_ADOPTION_ROW_SELECTOR_DRIFT")
    if row.get("candidate_id") != CANDIDATE_ID or row.get("status") != "failed":
        raise C7bFailedRowAdoptionError("C7B_ADOPTION_ROW_STATE_DRIFT")
    if row_fingerprint(row) != receipt["live_row_fingerprint"]["sha256"]:
        raise C7bFailedRowAdoptionError("C7B_ADOPTION_ROW_FINGERPRINT_DRIFT")
    failure = receipt["failure_tuple"]
    assert isinstance(failure, Mapping)
    if any(row.get(key) != value for key, value in failure.items()):
        raise C7bFailedRowAdoptionError("C7B_ADOPTION_ROW_FAILURE_DRIFT")
    generation = receipt["attempt_generation"]
    assert isinstance(generation, Mapping)
    if row.get("cover_route_regeneration_attempts") != generation["cover_route_regeneration_attempts"] or row.get("cover_route_regeneration_fingerprint") != generation["cover_route_regeneration_fingerprint"]:
        raise C7bFailedRowAdoptionError("C7B_ADOPTION_ROW_ATTEMPT_DRIFT")
    return receipt, seal
