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
from pathlib import Path
import re
from typing import Any, Mapping


REGISTRY_SCHEMA_VERSION = "candidate-reviewed-subtitle-baseline.v1"
BASELINE_SCHEMA_VERSIONS = frozenset(
    {"subtitle-redelivery-baseline.v1", "subtitle-redelivery-baseline.v2"}
)
BASELINE_MODE = "preserve_text_outside_source_truth"
_CANDIDATE_ID_RX = re.compile(r"[A-Za-z0-9_-]{1,96}\Z")
_SHA256_RX = re.compile(r"(?:sha256:)?([0-9a-f]{64})\Z")


class ReviewedSubtitleBaselineRegistryError(ValueError):
    """The canonical baseline asset is present but cannot be trusted."""


@dataclass(frozen=True)
class ReviewedSubtitleBaseline:
    config: dict[str, Any]
    manifest_path: Path
    baseline_path: Path

    @property
    def fingerprint_paths(self) -> tuple[Path, Path]:
        return self.manifest_path, self.baseline_path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _regular_canonical_file(path: Path, *, root: Path, label: str) -> Path:
    if path.is_symlink() or not path.is_file():
        raise ReviewedSubtitleBaselineRegistryError(
            f"{label} must be a regular non-symlink file"
        )
    resolved = path.resolve()
    if resolved.parent != root.resolve():
        raise ReviewedSubtitleBaselineRegistryError(
            f"{label} escapes its canonical asset root"
        )
    return resolved


def _required_text(value: object, *, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ReviewedSubtitleBaselineRegistryError(f"{label} is required")
    return text


def load_candidate_reviewed_subtitle_baseline(
    root: Path,
    candidate_id: str,
) -> ReviewedSubtitleBaseline | None:
    """Load one candidate baseline, returning ``None`` only when absent."""

    if not _CANDIDATE_ID_RX.fullmatch(str(candidate_id or "")):
        raise ReviewedSubtitleBaselineRegistryError(
            "unsafe candidate id for reviewed subtitle baseline"
        )
    root = root.resolve()
    manifest = root / f"{candidate_id}.subtitle-baseline.v1.json"
    if not manifest.exists():
        return None
    manifest = _regular_canonical_file(
        manifest, root=root, label="candidate subtitle baseline manifest"
    )
    try:
        document = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
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
        raise ReviewedSubtitleBaselineRegistryError(
            "baseline path must be a sibling filename"
        )
    baseline = _regular_canonical_file(
        root / relative,
        root=root,
        label="candidate reviewed subtitle baseline",
    )
    expected_match = _SHA256_RX.fullmatch(str(document.get("sha256") or ""))
    if expected_match is None:
        raise ReviewedSubtitleBaselineRegistryError(
            "candidate subtitle baseline sha256 is invalid"
        )
    actual_sha256 = _sha256(baseline)
    if actual_sha256 != expected_match.group(1):
        raise ReviewedSubtitleBaselineRegistryError(
            "candidate subtitle baseline sha256 does not match"
        )
    _required_text(document.get("authority"), label="baseline authority")

    if schema_version == "subtitle-redelivery-baseline.v2":
        basename = _required_text(
            document.get("source_recording_basename"),
            label="baseline source recording basename",
        )
        if Path(basename).name != basename:
            raise ReviewedSubtitleBaselineRegistryError(
                "baseline source recording basename must not contain a path"
            )
        if _SHA256_RX.fullmatch(str(document.get("source_sha256") or "")) is None:
            raise ReviewedSubtitleBaselineRegistryError(
                "baseline source sha256 is invalid"
            )
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

    config = dict(document)
    config.pop("registry_schema_version", None)
    config.pop("candidate_id", None)
    config["path"] = str(baseline)
    config["sha256"] = actual_sha256
    return ReviewedSubtitleBaseline(
        config=config,
        manifest_path=manifest,
        baseline_path=baseline,
    )
