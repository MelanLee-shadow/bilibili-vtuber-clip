"""Authorized-upload binding for materialized published recovery packages."""

from __future__ import annotations

from src.autoslice.evidence_identity import file_sha256 as _evidence_file_sha256
import json
from pathlib import Path

from src.autoslice.published_recovery_package_contract import (
    validate_materialized_published_recovery_package,
)


def uploaded_sidecar_path(manifest_path: Path) -> Path:
    """Use the producer's exact locator; never guess a compatible alias."""
    name = manifest_path.name
    stem = name[:-len(".upload_manifest.json")] if name.endswith(".upload_manifest.json") else name
    return manifest_path.parent / (stem + ".uploaded.json")


def verified_publication_snapshot(
    manifest: dict, manifest_path: Path, bvid: str, *,
    uploaded_path: Path, season_path: Path,
) -> tuple[dict, dict, dict] | None:
    """Replay the historical closure without changing its source bytes."""
    if not uploaded_path.exists() and not uploaded_path.is_symlink():
        return None
    from src.autoslice.publication_reconciliation import _validate_new_public_evidence

    def read(path: Path) -> dict:
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"historical publication file is unsafe or missing: {path}")
        value = json.loads(path.read_bytes())
        if not isinstance(value, dict):
            raise ValueError("historical publication file is not an object")
        return value

    uploaded = read(uploaded_path)
    # The original uploaded file may refer to a separately saved proof. Resolve
    # the actual frozen source rather than assuming a mutable latest locator.
    bound_public = Path(str(uploaded.get("public_verify") or ""))
    public = read(bound_public)
    if _evidence_file_sha256(bound_public) != uploaded.get("public_verify_sha256"):
        raise ValueError("historical public proof SHA drift")
    season = read(season_path)
    _validate_new_public_evidence(
        manifest=manifest, manifest_path=manifest_path, bvid=bvid,
        public_verify_path=bound_public, season_verify_path=season_path,
        uploaded_path=uploaded_path,
    )
    return uploaded, public, season

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
