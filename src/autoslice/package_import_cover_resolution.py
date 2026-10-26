"""Resolve a package-contained cover from the native HOST_ONLY locator form."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping

from src.autoslice.cover_host_identity_gate import HOST_ONLY_SCHEMA_VERSION
from src.autoslice.failed_pick_import import PackageImportError
from src.autoslice.host_only_v4_package_binding import read_package_file_once

DigestParser = Callable[..., str]
BytesDigester = Callable[[bytes], str]


def resolve_package_internal_cover(
    package_root: Path,
    cover_generation: Mapping[str, Any],
    *,
    declared_digest: DigestParser,
    sha256_bytes: BytesDigester,
) -> Path:
    """Return only a hash-bound path contained under ``package_root``.

    Native HOST_ONLY receipts may preserve an absolute producer label. The label is
    never opened: it selects an established relative package alias, which is
    read with no-follow directory descriptors and verified against the frozen
    final-cover digest.
    """

    if cover_generation.get("method") == "skill.candidates":
        from .package_import_candidate_cover import package_candidate_cover
        try:
            return package_candidate_cover(package_root, cover_generation)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise PackageImportError("DECLARED_ARTIFACT_SHA_DRIFT", str(exc)) from exc
    verification = cover_generation.get("final_host_identity_verification")
    if isinstance(verification, Mapping) and verification.get("schema_version") == (
        HOST_ONLY_SCHEMA_VERSION
    ):
        try:
            locator = verification.get("final_cover_path")
            if isinstance(locator, str) and PurePosixPath(locator).is_absolute():
                declared = cover_generation.get("final_cover")
                if not isinstance(declared, str) or not declared:
                    raise ValueError("HOST_ONLY generation final cover locator is missing")
                receipt_path = PurePosixPath(locator)
                declared_path = PurePosixPath(declared)
                # A package projector can replace an external design locator
                # with the logical replacement_recuts source namespace. That
                # namespace maps to the flat package root. Historical receipts
                # which themselves named replacement_recuts retain the older
                # nested-alias proof, while read_bound_package independently
                # verifies the flat same-stem delivery copy.
                locator = (
                    declared_path.name
                    if (
                        declared_path.parent.name == "replacement_recuts"
                        and receipt_path.parent.name != "replacement_recuts"
                    )
                    else (PurePosixPath(declared_path.parent.name) / declared_path.name).as_posix()
                )
            relative, payload = read_package_file_once(
                package_root,
                locator,
                label="HOST_ONLY package cover",
            )
            expected = declared_digest(
                cover_generation.get("final_cover_sha256"), label="HOST_ONLY cover hash"
            )
            if sha256_bytes(payload) != expected:
                raise ValueError("HOST_ONLY relative cover hash drift")
        except (OSError, ValueError, RuntimeError) as exc:
            raise PackageImportError("DECLARED_ARTIFACT_SHA_DRIFT", str(exc)) from exc
        return package_root / relative

    declared = cover_generation.get("final_cover")
    if not isinstance(declared, str) or not declared:
        raise PackageImportError(
            "PACKAGE_DOCUMENT_INVALID",
            "publish.cover_generation lacks final_cover",
        )
    path = PurePosixPath(declared)
    return package_root / path.parent.name / path.name
