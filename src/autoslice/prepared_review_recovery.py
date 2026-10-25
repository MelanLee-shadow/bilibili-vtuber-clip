"""Review-only recovery checks for retained producer prepared handles.

An old prepared handle can still be useful as private review input after the
runtime has moved to another deployment.  It cannot become a materialization
or commit capability: those paths continue to use the strict current
deployment preflight in :mod:`producer_delivery_transaction`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from src.autoslice.package_cover_network import _digest, _regular
from src.autoslice.producer_delivery_transaction import (
    PreparedDelivery,
    ProducerDeliveryTransactionError,
    _checked_artifacts,
    _deployment_repository,
    _read_document,
    _read_regular_bytes,
    deployment_authority_binding,
)
from src.autoslice.qixi_transaction_core import (
    RunnerCommitLease,
    require_runner_commit_lease,
)
from src.autoslice.runtime_code_lease import immutable_version


_COMMIT = re.compile(r"[0-9a-f]{40}\Z")


@dataclass(frozen=True, slots=True)
class PreparedReviewArtifacts:
    """The document and artifact checks made under one runner lease.

    ``checked`` is deliberately the raw artifact tuple returned by the
    transaction core.  It is review evidence only; callers must not pass it to
    materialization APIs as a fabricated ``PreparedDeliveryPreflight``.
    """

    document: dict[str, Any]
    checked: tuple[tuple[dict, Path, Path, str], ...]


def _review_error(message: str) -> ProducerDeliveryTransactionError:
    return ProducerDeliveryTransactionError(message)


def _load_authority(path: Path, *, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(_read_regular_bytes(path).decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _review_error(f"{label} is unreadable") from exc
    if not isinstance(payload, dict):
        raise _review_error(f"{label} must be a JSON object")
    return payload


def _authority_entries(
    authority: Mapping[str, Any], *, label: str,
) -> dict[str, tuple[int, str]]:
    raw_entries = authority.get("entries")
    if not isinstance(raw_entries, dict) or not raw_entries:
        raise _review_error(f"{label} authority assets changed")
    entries: dict[str, tuple[int, str]] = {}
    for relative, raw_entry in raw_entries.items():
        if not isinstance(relative, str) or not isinstance(raw_entry, Mapping):
            raise _review_error(f"{label} authority assets changed")
        expected_bytes = raw_entry.get("bytes")
        expected_sha = raw_entry.get("sha256")
        if (
            isinstance(expected_bytes, bool)
            or not isinstance(expected_bytes, int)
            or expected_bytes < 0
            or not isinstance(expected_sha, str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", expected_sha) is None
        ):
            raise _review_error(f"{label} authority assets changed")
        entries[relative] = (expected_bytes, expected_sha)
    return entries


def _check_authority_assets(
    repo: Path,
    entries: Mapping[str, tuple[int, str]],
    *,
    label: str,
) -> None:
    for relative, (expected_bytes, expected_sha) in entries.items():
        try:
            path = _regular(repo / relative, root=repo)
            matches = path.stat().st_size == expected_bytes and _digest(path) == expected_sha
        except (OSError, ValueError) as exc:
            raise _review_error(f"{label} authority asset bytes drift") from exc
        if not matches:
            raise _review_error(f"{label} authority asset bytes drift")


def validate_retained_deployment(
    document: Mapping[str, Any],
    *,
    runtime_root: Path,
    lease: RunnerCommitLease,
    current_package_audit: bool = False,
) -> None:
    """Validate both retained and current authority assets for review."""

    require_runner_commit_lease(lease, runtime_root=runtime_root)
    original = document.get("deployed_authority")
    if not isinstance(original, Mapping):
        raise _review_error("prepared deployed authority is invalid")
    commit = str(original.get("commit") or "")
    if _COMMIT.fullmatch(commit) is None:
        raise _review_error("prepared original deployment identity is invalid")

    old_version = runtime_root / "versions" / commit
    legacy_layout = not old_version.exists() and not old_version.is_symlink()
    worker_release = None
    old_repo = old_version / "repo"
    if legacy_layout:
        # Worker versions are named by their complete release digest, while
        # the handle binds the controller commit and exact authority seals.
        # Locate only real retained trees with those original seals; never
        # invent a commit alias or borrow the current symlink.
        matches = []
        versions = runtime_root / "versions"
        if versions.is_dir() and not versions.is_symlink():
            for version in sorted(versions.iterdir()):
                candidate = version / "repo"
                if (re.fullmatch(r"[0-9a-f]{64}", version.name) is None
                        or version.is_symlink() or candidate.is_symlink()
                        or not candidate.is_dir() or candidate.resolve() != candidate):
                    continue
                try:
                    marker = _regular(candidate / "DEPLOYED_COMMIT", root=candidate)
                    authority = _regular(candidate / "DEPLOYED_AUTHORITY_MANIFEST.json", root=candidate)
                    same = (_read_regular_bytes(marker).decode().strip() == commit
                            and _digest(marker) == original.get("commit_bytes_sha256")
                            and _digest(authority) == original.get("authority_manifest_sha256"))
                except (OSError, ValueError, UnicodeError, ProducerDeliveryTransactionError):
                    continue
                if same:
                    matches.append(candidate)
        if len(matches) > 1:
            raise _review_error("retained prepared worker deployment is ambiguous")
        if matches:
            old_repo = matches[0]
            worker_release = old_repo.parent.name
            legacy_layout = False
        else:
            old_repo = runtime_root / "repo"
    if legacy_layout:
        if old_repo.is_symlink() or not old_repo.is_dir() or old_repo.resolve() != old_repo:
            raise _review_error("retained prepared deployment is unavailable")
    else:
        try:
            retained = immutable_version(old_repo)
        except (OSError, RuntimeError, ValueError) as exc:
            raise _review_error("retained prepared deployment is unavailable") from exc
        if not retained:
            raise _review_error("retained prepared deployment is unavailable")

    try:
        old_path = _regular(
            old_repo / "DEPLOYED_AUTHORITY_MANIFEST.json",
            root=old_repo,
        )
        old_commit_path = _regular(old_repo / "DEPLOYED_COMMIT", root=old_repo)
    except (OSError, ValueError) as exc:
        raise _review_error("prepared review deployment seal is unavailable") from exc
    if (
        _digest(old_path) != original.get("authority_manifest_sha256")
        or _digest(old_commit_path) != original.get("commit_bytes_sha256")
        or _read_regular_bytes(old_commit_path).decode("utf-8").strip() != commit
    ):
        raise _review_error("prepared review deployment seal drifts")

    old = _load_authority(old_path, label="original prepared authority")
    if old.get("deployed_commit") != commit:
        raise _review_error("prepared review deployment seal drifts")
    old_entries = _authority_entries(old, label="prepared review")
    current_repo = _deployment_repository(runtime_root)
    try:
        current_path = _regular(
            current_repo / "DEPLOYED_AUTHORITY_MANIFEST.json",
            root=current_repo,
        )
        current_binding = deployment_authority_binding(runtime_root)
    except (OSError, ValueError) as exc:
        raise _review_error("current review deployment seal is unavailable") from exc

    if _digest(current_path) != current_binding["authority_manifest_sha256"]:
        raise _review_error("prepared review deployment seal drifts")
    current = _load_authority(current_path, label="current review authority")
    current_entries = _authority_entries(current, label="prepared review")
    if (old.get("entries") != current.get("entries") or old_entries != current_entries) and not (
        legacy_layout and current_package_audit
    ):
        raise _review_error("prepared review authority assets changed")
    _check_authority_assets(
        old_repo, old_entries, label="prepared review"
    )
    _check_authority_assets(
        current_repo, current_entries, label="prepared review"
    )
    if worker_release is not None:
        from src.autoslice.overflow_release import verify
        try:
            verify(old_repo, worker_release)
        except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
            raise _review_error("prepared review retained worker deployment bytes drift") from exc
    if legacy_layout:
        # The real pre-versioning tree is review provenance only. Reuse the
        # deployment verifier rather than copying it into an invented version
        # directory or treating its old seal as a current commit capability.
        from src.autoslice.overflow_install import verify_deployed

        try:
            verify_deployed(old_repo)
        except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
            raise _review_error("prepared review legacy deployment bytes drift") from exc


def historical_prepared_review(
    handle: PreparedDelivery,
    lease: RunnerCommitLease,
    *,
    current_package_audit: bool = False,
) -> PreparedReviewArtifacts:
    """Check a retained old prepare for private review under a live lease.

    The old deployment seal, current deployment seal, exact authority asset
    inventory, actual old/current asset bytes, and every private staged/source
    preimage are checked while the caller owns the runner commit lease.  No
    public target, journal, state, or commit authority is created. Only the
    complete current package auditor may independently replay evolved current
    policies after validating both legacy and current asset inventories.
    """

    require_runner_commit_lease(lease, runtime_root=handle.runtime_root)
    document = _read_document(handle)
    if document.get("upload_enabled") is not False or document.get("lane") not in {"talk", "song"}:
        raise _review_error("historical prepared review requires a private Talk or Song handle")
    validate_retained_deployment(
        document,
        runtime_root=handle.runtime_root,
        lease=lease,
        current_package_audit=current_package_audit,
    )
    checked = _checked_artifacts(
        document,
        runtime_root=handle.runtime_root,
        allow_installed=False,
    )
    return PreparedReviewArtifacts(document=document, checked=tuple(checked))


__all__ = [
    "PreparedReviewArtifacts",
    "historical_prepared_review",
    "validate_retained_deployment",
]
