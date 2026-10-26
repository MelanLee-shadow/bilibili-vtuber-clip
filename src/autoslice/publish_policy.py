"""New-archive policy and immutable existing-BV policy, with separate authority."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path

NEW_UPLOAD_TID = 27
COPYRIGHT = 2
SOURCE = "https://live.bilibili.com/"
FIELDS = ("tid", "copyright", "source")
ROOT = Path(__file__).resolve().parents[2]


def new_upload_policy() -> dict:
    return {"tid": NEW_UPLOAD_TID, "copyright": COPYRIGHT, "source": SOURCE}


def checked_policy(value: object) -> dict:
    if (
        not isinstance(value, Mapping)
        or set(value) != set(FIELDS)
        or type(value.get("tid")) is not int
        or value["tid"] <= 0
        or type(value.get("copyright")) is not int
        or value["copyright"] not in {1, 2}
        or not isinstance(value.get("source"), str)
        or (value["copyright"] == 2 and not value["source"].strip())
    ):
        raise ValueError("PUBLISH_POLICY_SOURCE_MISSING_OR_INVALID")
    return dict(value)


def _read_proof(path: Path, expected_sha: object) -> dict:
    if path.is_symlink() or not path.is_file():
        raise ValueError("PUBLISHED_POLICY_PROOF_MISSING")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != str(expected_sha or "").removeprefix("sha256:"):
        raise ValueError("PUBLISHED_POLICY_PROOF_HASH_DRIFT")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("PUBLISHED_POLICY_PROOF_INVALID")
    return value


def _proof_policy(proof: Mapping, authority: Mapping) -> dict:
    """Take category/copyright from both surfaces and source from Creator."""
    if "member_archive" in proof:
        creator, public = proof.get("member_archive") or {}, proof.get("public_view") or {}
        if (
            proof.get("bvid") != authority["bvid"]
            or creator.get("bvid") != authority["bvid"]
            or creator.get("aid") != authority["aid"]
            or public.get("aid") != authority["aid"]
            or public.get("cid") != authority["cid"]
        ):
            raise ValueError("PUBLISHED_POLICY_PROOF_IDENTITY_DRIFT")
    else:
        creator_surface, public_surface = proof.get("creator") or {}, proof.get("public") or {}
        if (
            creator_surface.get("bvid") != authority["bvid"]
            or public_surface.get("bvid") != authority["bvid"]
            or creator_surface.get("aid") != authority["aid"]
            or public_surface.get("aid") != authority["aid"]
            or creator_surface.get("available") is not True
            or public_surface.get("available") is not True
        ):
            raise ValueError("PUBLISHED_POLICY_PROOF_IDENTITY_DRIFT")
        creator, public = (
            creator_surface.get("metadata") or {},
            public_surface.get("metadata") or {},
        )
    policy = checked_policy({field: creator.get(field) for field in FIELDS})
    if any(public.get(field) != policy[field] for field in ("tid", "copyright")):
        raise ValueError("PUBLISHED_POLICY_PUBLIC_CREATOR_DISAGREE")
    return policy


def manifest_policy(manifest: Mapping, *, repo_root: Path = ROOT) -> dict:
    """Derive policy only after validating the exact publication authority."""
    authority = manifest.get("recovery_publication_authority")
    if authority is None:
        return new_upload_policy()
    from .recovery_title_authority import validate_recovery_publication_authority

    authority = validate_recovery_publication_authority(
        authority,
        candidate_id=str(authority.get("candidate_id") or ""),
        expected_final_title=str(manifest.get("title") or ""),
        repo_root=repo_root,
    )
    schema = authority["schema_version"]
    if schema == "new-bv-same-bv-publication-authority.v1":
        from .new_bv_repair_authority import replay_first_publication_manifest

        return checked_policy(replay_first_publication_manifest(authority).get("publish_policy"))
    if schema == "fastlane-c1-authorized-same-bv-projection.v1":
        from .fastlane_c1_technical_receipt import frozen_publish_policy

        return frozen_publish_policy()
    if schema == "original-fastlane-authorized-same-bv.v1":
        from .original_patch_package import sealed_asset, within

        candidate = authority["candidate_id"]
        target, _ = sealed_asset(repo_root, candidate, ".original-repair-target.v1.json")
        closure, _ = sealed_asset(repo_root, candidate, ".original-delivery-closure.v1.json")
        package = Path(str((manifest.get("package_attestation") or {}).get("package_root") or ""))
        if not package.is_absolute():
            raise ValueError("PUBLISHED_POLICY_PACKAGE_MISSING")
        proof = _read_proof(
            within(package, closure["evidence"]["public_before"]), target["public_snapshot_sha256"]
        )
    else:
        relative = Path(str(authority.get("source_public_verify_repo_path") or ""))
        if relative.is_absolute() or ".." in relative.parts or not str(relative):
            raise ValueError("PUBLISHED_POLICY_PROOF_PATH_INVALID")
        path = repo_root / relative
        if not path.resolve().is_relative_to(repo_root.resolve()):
            raise ValueError("PUBLISHED_POLICY_PROOF_PATH_INVALID")
        proof = _read_proof(path, authority.get("source_public_verify_sha256"))
    return _proof_policy(proof, authority)


def policy_problems(
    manifest: Mapping, *, ordinary_upload: bool = False, proven_historical: bool = False
) -> list[str]:
    try:
        actual = checked_policy(manifest.get("publish_policy"))
        if ordinary_upload:
            if manifest.get("recovery_publication_authority") is not None:
                return ["EXISTING_BV_POLICY_NOT_NEW_UPLOAD"]
            expected = new_upload_policy()
        elif proven_historical and manifest.get("recovery_publication_authority") is None:
            # The caller must replay exact posted ledger/publication bytes first.
            expected = actual
        else:
            expected = manifest_policy(manifest)
        if actual != expected:
            return ["manifest v3 publish_policy is invalid: PUBLISH_POLICY_LANE_DRIFT"]
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return [f"manifest v3 publish_policy is invalid: {exc}"]
    return []


def preserved_policy_problems(target: Mapping, before: Mapping) -> list[str]:
    """Used at plan creation AND replay against the frozen before snapshot."""
    creator = (before.get("creator") or {}).get("metadata") or {}
    public = (before.get("public") or {}).get("metadata") or {}
    try:
        expected = checked_policy({field: creator.get(field) for field in FIELDS})
    except ValueError as exc:
        return [str(exc)]
    problems = []
    for field in FIELDS:
        if target.get(field) != expected[field]:
            problems.append("SAME_BV_" + field.upper() + "_MUST_PRESERVE_ORIGINAL")
    if any(public.get(field) != expected[field] for field in ("tid", "copyright")):
        problems.append("SAME_BV_PUBLIC_CREATOR_POLICY_DISAGREE")
    return problems
