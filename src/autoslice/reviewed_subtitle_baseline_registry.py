"""Candidate-scoped, hash-bound reviewed subtitle baseline discovery.

Reviewed subtitle text is human truth, independent of whether a cover is kept
or regenerated.  The runner discovers one canonical manifest per candidate
and injects its validated producer baseline config.  Both the manifest and the
SRT bytes are returned as fingerprint inputs so an edit wakes only that
candidate.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Mapping

from src.autoslice.redelivery_boundary_projection import (
    PROJECTION_MODE,
    PROJECTION_MODE_CONFIG_KEY,
)
from src.autoslice.repository_asset_authority import (
    RepositoryAssetAuthorityError,
    require_repository_asset_authority,
)
from src.autoslice.reviewed_exact_source_interval import (
    REFERENCE_CONFIG_KEY as EXACT_INTERVAL_REFERENCE_CONFIG_KEY,
    REFERENCE_SCHEMA_VERSION as EXACT_INTERVAL_REFERENCE_SCHEMA_VERSION,
    ReviewedExactSourceIntervalError,
    validate_runtime_authority,
)


REGISTRY_SCHEMA_VERSION = "candidate-reviewed-subtitle-baseline.v1"
BASELINE_SCHEMA_VERSIONS = frozenset(
    {"subtitle-redelivery-baseline.v1", "subtitle-redelivery-baseline.v2"}
)
BASELINE_MODE = "preserve_text_outside_source_truth"
_CANDIDATE_ID_RX = re.compile(r"[A-Za-z0-9_-]{1,96}\Z")
_SHA256_RX = re.compile(r"(?:sha256:)?([0-9a-f]{64})\Z")
_REPO_ROOT = Path(__file__).resolve().parents[2]
_BASELINE_REPO_DIRECTORY = Path("assets/lidousha/reviewed_subtitle_baselines")
_EXACT_INTERVAL_REPO_DIRECTORY = Path("assets/lidousha/reviewed_exact_source_intervals")


class ReviewedSubtitleBaselineRegistryError(ValueError):
    """The canonical baseline asset is present but cannot be trusted."""


@dataclass(frozen=True)
class ReviewedSubtitleBaseline:
    config: dict[str, Any]
    manifest_path: Path
    baseline_path: Path
    exact_interval_authority_path: Path | None = None
    exact_interval_authority: dict[str, object] | None = None

    @property
    def fingerprint_paths(self) -> tuple[Path, ...]:
        return tuple(
            path
            for path in (
                self.manifest_path,
                self.baseline_path,
                self.exact_interval_authority_path,
            )
            if path is not None
        )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _regular_canonical_file(path: Path, *, root: Path, label: str) -> Path:
    if path.is_symlink() or not path.is_file():
        raise ReviewedSubtitleBaselineRegistryError(f"{label} must be a regular non-symlink file")
    resolved = path.resolve()
    if resolved.parent != root.resolve():
        raise ReviewedSubtitleBaselineRegistryError(f"{label} escapes its canonical asset root")
    return resolved


def _lexical_absolute(path: Path) -> Path:
    """Make a path absolute without resolving any symlink component."""

    return Path(os.path.abspath(os.fspath(path)))


def _require_trusted_exact_asset_roots(
    root: Path,
    *,
    repo_root: Path,
) -> tuple[Path, Path]:
    """Bind exact replay assets to this process's explicit repository root.

    Resolving ``root`` before establishing this relationship would let a
    canonical-looking profile directory symlink to a second Git checkout (or a
    forged deployed tree) and borrow that tree's authority.  Compare lexical
    paths first, then reject every symlink below the trusted repository root.
    The repository authority loader repeats the component check while binding
    the exact bytes, closing a subsequent path-swap race.
    """

    declared_repo_root = _lexical_absolute(repo_root)
    declared_baseline_root = _lexical_absolute(root)
    expected_baseline_root = declared_repo_root / _BASELINE_REPO_DIRECTORY
    expected_authority_root = declared_repo_root / _EXACT_INTERVAL_REPO_DIRECTORY
    if declared_baseline_root != expected_baseline_root:
        raise ReviewedSubtitleBaselineRegistryError(
            "reviewed exact interval baseline root is not canonical for the trusted repository"
        )
    try:
        if declared_repo_root.is_symlink():
            raise ReviewedSubtitleBaselineRegistryError(
                "trusted repository root must not be a symlink"
            )
        resolved_repo_root = declared_repo_root.resolve(strict=True)
        if resolved_repo_root != declared_repo_root:
            raise ReviewedSubtitleBaselineRegistryError(
                "trusted repository root contains a symlink"
            )
        for relative in (
            _BASELINE_REPO_DIRECTORY,
            _EXACT_INTERVAL_REPO_DIRECTORY,
        ):
            cursor = declared_repo_root
            for part in relative.parts:
                cursor = cursor / part
                if cursor.is_symlink():
                    raise ReviewedSubtitleBaselineRegistryError(
                        "reviewed exact interval asset path contains a parent symlink"
                    )
            resolved = cursor.resolve(strict=True)
            if resolved != resolved_repo_root / relative:
                raise ReviewedSubtitleBaselineRegistryError(
                    "reviewed exact interval asset path escapes the trusted repository"
                )
    except ReviewedSubtitleBaselineRegistryError:
        raise
    except (OSError, ValueError) as exc:
        raise ReviewedSubtitleBaselineRegistryError(
            "reviewed exact interval trusted repository path is unavailable"
        ) from exc
    return declared_repo_root, expected_authority_root


def _required_text(value: object, *, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ReviewedSubtitleBaselineRegistryError(f"{label} is required")
    return text


def load_candidate_reviewed_subtitle_baseline(
    root: Path,
    candidate_id: str,
    *,
    repo_root: Path = _REPO_ROOT,
) -> ReviewedSubtitleBaseline | None:
    """Load one candidate baseline, returning ``None`` only when absent."""

    if not _CANDIDATE_ID_RX.fullmatch(str(candidate_id or "")):
        raise ReviewedSubtitleBaselineRegistryError(
            "unsafe candidate id for reviewed subtitle baseline"
        )
    root = _lexical_absolute(root)
    manifest = root / f"{candidate_id}.subtitle-baseline.v1.json"
    if not manifest.exists():
        return None
    manifest = _regular_canonical_file(
        manifest, root=root, label="candidate subtitle baseline manifest"
    )
    try:
        manifest_bytes = manifest.read_bytes()
        document = json.loads(manifest_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReviewedSubtitleBaselineRegistryError(
            "candidate subtitle baseline manifest is invalid JSON"
        ) from exc
    if not isinstance(document, Mapping):
        raise ReviewedSubtitleBaselineRegistryError(
            "candidate subtitle baseline manifest must be an object"
        )
    if document.get("registry_schema_version") != REGISTRY_SCHEMA_VERSION:
        raise ReviewedSubtitleBaselineRegistryError(
            "candidate subtitle baseline registry schema is unsupported"
        )
    if document.get("candidate_id") != candidate_id:
        raise ReviewedSubtitleBaselineRegistryError(
            "candidate subtitle baseline id does not match its canonical filename"
        )
    schema_version = document.get("schema_version")
    if schema_version not in BASELINE_SCHEMA_VERSIONS:
        raise ReviewedSubtitleBaselineRegistryError(
            "candidate subtitle baseline producer schema is unsupported"
        )
    if document.get("mode") != BASELINE_MODE:
        raise ReviewedSubtitleBaselineRegistryError(
            "candidate subtitle baseline mode is unsupported"
        )
    raw_path = _required_text(document.get("path"), label="baseline path")
    relative = Path(raw_path)
    if relative.is_absolute() or len(relative.parts) != 1 or relative.name != raw_path:
        raise ReviewedSubtitleBaselineRegistryError("baseline path must be a sibling filename")
    baseline = _regular_canonical_file(
        root / relative,
        root=root,
        label="candidate reviewed subtitle baseline",
    )
    expected_match = _SHA256_RX.fullmatch(str(document.get("sha256") or ""))
    if expected_match is None:
        raise ReviewedSubtitleBaselineRegistryError("candidate subtitle baseline sha256 is invalid")
    actual_sha256 = _sha256(baseline)
    if actual_sha256 != expected_match.group(1):
        raise ReviewedSubtitleBaselineRegistryError(
            "candidate subtitle baseline sha256 does not match"
        )
    _required_text(document.get("authority"), label="baseline authority")

    if schema_version == "subtitle-redelivery-baseline.v2":
        if not isinstance(document.get("exact_interval_replay", False), bool):
            raise ReviewedSubtitleBaselineRegistryError(
                "baseline exact interval replay flag must be boolean"
            )
        basename = _required_text(
            document.get("source_recording_basename"),
            label="baseline source recording basename",
        )
        if Path(basename).name != basename:
            raise ReviewedSubtitleBaselineRegistryError(
                "baseline source recording basename must not contain a path"
            )
        if _SHA256_RX.fullmatch(str(document.get("source_sha256") or "")) is None:
            raise ReviewedSubtitleBaselineRegistryError("baseline source sha256 is invalid")
        start = document.get("absolute_source_start_ms")
        end = document.get("absolute_source_end_ms")
        if (
            isinstance(start, bool)
            or not isinstance(start, int)
            or isinstance(end, bool)
            or not isinstance(end, int)
            or start < 0
            or end <= start
        ):
            raise ReviewedSubtitleBaselineRegistryError(
                "baseline absolute source interval is invalid"
            )
        projection_mode = document.get(PROJECTION_MODE_CONFIG_KEY)
        if projection_mode is not None and projection_mode != PROJECTION_MODE:
            raise ReviewedSubtitleBaselineRegistryError(
                "baseline terminal projection mode is unsupported"
            )
        if projection_mode is not None and document.get("exact_interval_replay") is not True:
            raise ReviewedSubtitleBaselineRegistryError(
                "baseline terminal projection requires exact interval replay"
            )

    exact_interval_authority_path: Path | None = None
    exact_interval_authority: dict[str, object] | None = None
    exact_interval_reference = document.get(EXACT_INTERVAL_REFERENCE_CONFIG_KEY)
    if exact_interval_reference is not None:
        if schema_version != "subtitle-redelivery-baseline.v2":
            raise ReviewedSubtitleBaselineRegistryError(
                "reviewed exact interval authority requires baseline v2"
            )
        if not isinstance(exact_interval_reference, Mapping):
            raise ReviewedSubtitleBaselineRegistryError(
                "reviewed exact interval authority reference is invalid"
            )
        if (
            exact_interval_reference.get("schema_version")
            != EXACT_INTERVAL_REFERENCE_SCHEMA_VERSION
        ):
            raise ReviewedSubtitleBaselineRegistryError(
                "reviewed exact interval authority reference schema is unsupported"
            )
        repo_relative = _required_text(
            exact_interval_reference.get("path"),
            label="reviewed exact interval authority path",
        )
        expected_repo_relative = (
            "assets/lidousha/reviewed_exact_source_intervals/"
            f"{candidate_id}.reviewed-exact-source-interval.v1.json"
        )
        if repo_relative != expected_repo_relative:
            raise ReviewedSubtitleBaselineRegistryError(
                "reviewed exact interval authority path is not canonical"
            )
        trusted_repo_root, authority_root = _require_trusted_exact_asset_roots(
            root,
            repo_root=repo_root,
        )
        try:
            manifest_relative = manifest.relative_to(trusted_repo_root)
        except ValueError as exc:
            raise ReviewedSubtitleBaselineRegistryError(
                "reviewed exact interval manifest escapes the trusted repository"
            ) from exc
        exact_interval_authority_path = _regular_canonical_file(
            trusted_repo_root / repo_relative,
            root=authority_root,
            label="reviewed exact interval authority",
        )
        expected_authority_match = _SHA256_RX.fullmatch(
            str(exact_interval_reference.get("sha256") or "")
        )
        if expected_authority_match is None:
            raise ReviewedSubtitleBaselineRegistryError(
                "reviewed exact interval authority sha256 is invalid"
            )
        try:
            require_repository_asset_authority(
                repo_root=trusted_repo_root,
                relative_path=manifest_relative,
                observed_bytes=manifest_bytes,
            )
            authority_bytes = exact_interval_authority_path.read_bytes()
            require_repository_asset_authority(
                repo_root=trusted_repo_root,
                relative_path=Path(repo_relative),
                observed_bytes=authority_bytes,
            )
        except (OSError, ValueError, RepositoryAssetAuthorityError) as exc:
            raise ReviewedSubtitleBaselineRegistryError(
                "reviewed exact interval authority is not sealed by the active repository"
            ) from exc
        if hashlib.sha256(authority_bytes).hexdigest() != expected_authority_match.group(1):
            raise ReviewedSubtitleBaselineRegistryError(
                "reviewed exact interval authority sha256 does not match"
            )
        try:
            authority_value = json.loads(authority_bytes.decode("utf-8"))
            exact_interval_authority = validate_runtime_authority(authority_value)
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            ReviewedExactSourceIntervalError,
        ) as exc:
            raise ReviewedSubtitleBaselineRegistryError(
                "reviewed exact interval authority document is invalid"
            ) from exc
        if exact_interval_authority.get("candidate_id") != candidate_id:
            raise ReviewedSubtitleBaselineRegistryError(
                "reviewed exact interval authority candidate does not match"
            )

    config = dict(document)
    config.pop("registry_schema_version", None)
    config.pop("candidate_id", None)
    config["path"] = str(baseline)
    config["sha256"] = actual_sha256
    if exact_interval_authority_path is not None:
        config[EXACT_INTERVAL_REFERENCE_CONFIG_KEY] = {
            **dict(config[EXACT_INTERVAL_REFERENCE_CONFIG_KEY]),
            "path": str(exact_interval_authority_path),
        }
    return ReviewedSubtitleBaseline(
        config=config,
        manifest_path=manifest,
        baseline_path=baseline,
        exact_interval_authority_path=exact_interval_authority_path,
        exact_interval_authority=exact_interval_authority,
    )
