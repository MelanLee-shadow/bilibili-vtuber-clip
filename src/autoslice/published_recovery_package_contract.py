"""OSS stub: private published-recovery authority is unavailable."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


# Keep the ordinary package module importable without copying the private
# registry path or its digest.  A real recovery package remains fail-closed.
REGISTRY_REPO_PATH = "assets/_template/recovery_publication_authority.v1.json"
REGISTRY_SHA256 = ""
STATE_AUTHORITY_REPO_PATH = "assets/_template/published_recovery_state_authority.v1.json"
STATE_AUTHORITY_SHA256 = ""


@dataclass(frozen=True, slots=True)
class PublishedRecoveryPreflightIssue:
    code: str
    path: Path
    detail: str


class PublishedRecoveryContractError(ValueError):
    """The private published-recovery authority is not in OSS."""


def _unavailable(*_args, **_kwargs):
    raise PublishedRecoveryContractError(
        "PUBLISHED_RECOVERY_PRIVATE_AUTHORITY_UNAVAILABLE"
    )


def audit_published_recovery_preflight(
    root: Path,
    _recovery_publication_authorities: Mapping[str, Mapping[str, object]],
) -> list[PublishedRecoveryPreflightIssue]:
    evidence = sorted(
        list(root.glob("*.published-recovery-preflight.json"))
        + list(root.glob("*.published-recovery-package-receipt.json"))
    )
    if not evidence:
        return []
    return [
        PublishedRecoveryPreflightIssue(
            "PUBLISHED_RECOVERY_PRIVATE_AUTHORITY_UNAVAILABLE",
            evidence[0],
            "private published-recovery authority is unavailable in an OSS snapshot",
        )
    ]


def audit_published_recovery_manifest_binding(
    root: Path,
    recovery_publication_authorities: Mapping[str, Mapping[str, object]],
    _items: object,
) -> list[PublishedRecoveryPreflightIssue]:
    return audit_published_recovery_preflight(root, recovery_publication_authorities)


load_published_recovery_state_authority = _unavailable
build_published_recovery_package_receipt = _unavailable
validate_published_recovery_package_receipt = _unavailable
validate_materialized_published_recovery_package = _unavailable
