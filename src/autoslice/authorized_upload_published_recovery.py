"""Authorized-upload binding for materialized published recovery packages."""

from __future__ import annotations

import json
from pathlib import Path

from src.autoslice.published_recovery_package_contract import (
    validate_materialized_published_recovery_package,
)


def attach_manifest_attestation(manifest: dict, package_root: Path) -> list[str]:
    if not list(package_root.glob("*.published-recovery-preflight.json")):
        return []
    try:
        entry = validate_materialized_published_recovery_package(package_root)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        return [f"published recovery package rejected: {exc}"]
    attestation = manifest.get("package_attestation")
    if not isinstance(attestation, dict):
        return ["manifest v3 has no package_attestation object"]
    attestation["published_recovery_package"] = entry
    return []


def attestation_problems(
    attestation: dict,
    package_root: Path,
    *,
    live_authority_recheck: bool,
) -> list[str]:
    preflights = list(package_root.glob("*.published-recovery-preflight.json"))
    entry = attestation.get("published_recovery_package")
    if not preflights:
        return (
            ["published recovery package attestation is extraneous"]
            if entry is not None else []
        )
    if not isinstance(entry, dict):
        return ["package_attestation.published_recovery_package is missing"]
    try:
        expected = validate_materialized_published_recovery_package(
            package_root,
            live_authority_recheck=live_authority_recheck,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        return [f"published recovery package rejected: {exc}"]
    if entry != expected:
        return ["package_attestation.published_recovery_package drifts"]
    return []
