"""Fail-closed reviewed speaker baselines for delivery-time finalization.

The ordinary speaker override remains the final wording/style authority.  This
module only lets a reviewed subset supply trusted in-clip HOST anchors before
CAM++ analysis, so explicitly uncovered cues still receive the normal machine
decision.  The baseline envelope binds the complete final cue grid and the
operator truth input that produced it.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Mapping, Sequence

from scripts.apply_speaker_turn_overrides import (
    sha256_file,
    validate_bound_speaker_override_document,
)
from scripts.apply_subtitle_text_overrides import parse_srt
from src.autoslice.speaker_common import HOST_SPEAKER, SpeakerFinalizationError
from src.autoslice.speaker_context import _reviewed_context_votes
from src.autoslice.text_baseline_guard import (
    BASELINE_CONTAINS_SPEAKER_LABEL_PREFIX,
    contains_speaker_label_prefix,
)


REVIEWED_SPEAKER_BASELINE_SCHEMA = "reviewed-speaker-baseline.v1"
TRUTH_SCHEMA = "ivan-speaker-truth-diff.v2"
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class ReviewedSpeakerBaseline:
    """Validated early-anchor inputs and their manifest-ready provenance."""

    anchor_labels: dict[int, str]
    machine_cues: tuple[int, ...]
    evidence: dict[str, object]


@dataclass(frozen=True)
class SpeakerOverrideState:
    """One fully bound optional override and its pre-analysis authorities."""

    document: dict[str, object] | None
    reviewed_votes: dict[int, str]
    reviewed_baseline: ReviewedSpeakerBaseline | None
    expected_automatic_sha256: str


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _required_text(value: object, *, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise SpeakerFinalizationError(f"{label} must be non-empty")
    return text


def _positive_cue_number(value: object, *, label: str, cue_count: int) -> int:
    if isinstance(value, bool):
        raise SpeakerFinalizationError(f"{label} must be an integer cue number")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise SpeakerFinalizationError(
            f"{label} must be an integer cue number"
        ) from exc
    if str(value).strip() != str(number) or not 1 <= number <= cue_count:
        raise SpeakerFinalizationError(f"{label} is out of range: {value!r}")
    return number


def _expected_cue(row: Mapping[str, object], *, label: str) -> Mapping[str, object]:
    expected = row.get("expect")
    if not isinstance(expected, Mapping):
        raise SpeakerFinalizationError(f"{label}.expect must be an object")
    if set(expected) != {"start", "end", "text"}:
        raise SpeakerFinalizationError(
            f"{label}.expect must contain exactly start/end/text"
        )
    return expected


def _assert_expect_matches(
    expected: Mapping[str, object],
    cue: object,
    *,
    label: str,
) -> None:
    for field in ("start", "end", "text"):
        actual = getattr(cue, field, None)
        if expected.get(field) != actual:
            raise SpeakerFinalizationError(
                f"{label} {field} drift: expected {expected.get(field)!r}, got {actual!r}"
            )


def _resolve_truth_input(
    truth_input: Mapping[str, object],
    *,
    repo_root: Path,
    candidate_id: str,
) -> tuple[Path, str]:
    if set(truth_input) != {"path", "sha256"}:
        raise SpeakerFinalizationError(
            "reviewed speaker truth_input must contain exactly path/sha256"
        )
    raw_path = _required_text(
        truth_input.get("path"), label="reviewed speaker truth_input.path"
    )
    relative = Path(raw_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise SpeakerFinalizationError(
            "reviewed speaker truth_input.path must be repository-relative"
        )
    root = repo_root.resolve(strict=True)
    candidate = root / relative
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise SpeakerFinalizationError(
            "reviewed speaker truth input is unavailable"
        ) from exc
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise SpeakerFinalizationError(
            "reviewed speaker truth input escapes the repository"
        )
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise SpeakerFinalizationError(
                "reviewed speaker truth input must not traverse symlinks"
            )
    expected_sha = str(truth_input.get("sha256") or "")
    if not SHA256_RE.fullmatch(expected_sha):
        raise SpeakerFinalizationError(
            "reviewed speaker truth_input.sha256 must be a SHA-256 digest"
        )
    actual_sha = _sha256(resolved)
    if actual_sha != expected_sha:
        raise SpeakerFinalizationError("reviewed speaker truth input hash drift")
    try:
        truth = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SpeakerFinalizationError(
            "reviewed speaker truth input is invalid JSON"
        ) from exc
    if not isinstance(truth, Mapping) or truth.get("schema") != TRUTH_SCHEMA:
        raise SpeakerFinalizationError("reviewed speaker truth schema is unsupported")
    if truth.get("candidate_id") != candidate_id:
        raise SpeakerFinalizationError("reviewed speaker truth candidate mismatch")
    return resolved, actual_sha


def _validated_override_rows(
    document: Mapping[str, object],
    *,
    cues: Sequence[object],
    authority: str,
) -> dict[int, Mapping[str, object]]:
    raw = document.get("overrides")
    if not isinstance(raw, list):
        raise SpeakerFinalizationError("speaker overrides must be a list")
    by_cue: dict[int, Mapping[str, object]] = {}
    for position, row in enumerate(raw, start=1):
        if not isinstance(row, Mapping):
            raise SpeakerFinalizationError(
                f"speaker override row {position} must be an object"
            )
        cue_number = _positive_cue_number(
            row.get("source_cue"),
            label=f"speaker override row {position}.source_cue",
            cue_count=len(cues),
        )
        if cue_number in by_cue:
            raise SpeakerFinalizationError(
                f"duplicate speaker override cue: {cue_number}"
            )
        if _required_text(
            row.get("authority"), label=f"speaker override cue {cue_number}.authority"
        ) != authority:
            raise SpeakerFinalizationError(
                f"speaker override cue {cue_number} authority mismatch"
            )
        expected = _expected_cue(row, label=f"speaker override cue {cue_number}")
        _assert_expect_matches(
            expected,
            cues[cue_number - 1],
            label=f"speaker override cue {cue_number}",
        )
        by_cue[cue_number] = row
    return by_cue


def load_reviewed_speaker_baseline(
    document: Mapping[str, object],
    *,
    candidate_id: str,
    cues: Sequence[object],
    repo_root: Path | None = None,
) -> ReviewedSpeakerBaseline | None:
    """Validate the optional baseline envelope and return zero-based anchors.

    The baseline must partition the complete current cue grid between reviewed
    overrides and explicit machine-owned cues.  It therefore cannot silently
    turn an omitted cue into GUEST or reuse truth after any text/timing drift.
    """

    raw = document.get("reviewed_speaker_baseline")
    if raw is None:
        return None
    if contains_speaker_label_prefix(
        str(getattr(cue, "text", "")) for cue in cues
    ):
        raise SpeakerFinalizationError(
            BASELINE_CONTAINS_SPEAKER_LABEL_PREFIX
        )
    if not isinstance(raw, Mapping):
        raise SpeakerFinalizationError(
            "reviewed_speaker_baseline must be an object"
        )
    expected_keys = {
        "schema_version",
        "authority",
        "truth_input",
        "cue_count",
        "anchor_source_cues",
        "machine_cues",
    }
    if set(raw) != expected_keys:
        raise SpeakerFinalizationError(
            "reviewed_speaker_baseline fields are incomplete or unsupported"
        )
    if raw.get("schema_version") != REVIEWED_SPEAKER_BASELINE_SCHEMA:
        raise SpeakerFinalizationError("reviewed speaker baseline schema is unsupported")
    authority = _required_text(
        raw.get("authority"), label="reviewed speaker baseline authority"
    )
    cue_count = raw.get("cue_count")
    if isinstance(cue_count, bool) or not isinstance(cue_count, int):
        raise SpeakerFinalizationError("reviewed speaker baseline cue_count must be an integer")
    if cue_count != len(cues) or cue_count <= 0:
        raise SpeakerFinalizationError("reviewed speaker baseline cue count drift")
    truth_input = raw.get("truth_input")
    if not isinstance(truth_input, Mapping):
        raise SpeakerFinalizationError("reviewed speaker truth_input must be an object")
    truth_path, truth_sha = _resolve_truth_input(
        truth_input,
        repo_root=repo_root or REPO_ROOT,
        candidate_id=candidate_id,
    )
    override_rows = _validated_override_rows(
        document,
        cues=cues,
        authority=authority,
    )

    machine_raw = raw.get("machine_cues")
    if not isinstance(machine_raw, list):
        raise SpeakerFinalizationError("reviewed speaker machine_cues must be a list")
    machine_rows: dict[int, Mapping[str, object]] = {}
    for position, row in enumerate(machine_raw, start=1):
        if not isinstance(row, Mapping):
            raise SpeakerFinalizationError(
                f"reviewed speaker machine cue {position} must be an object"
            )
        if set(row) != {"source_cue", "expect", "reason", "arbitration"}:
            raise SpeakerFinalizationError(
                f"reviewed speaker machine cue {position} fields are invalid"
            )
        cue_number = _positive_cue_number(
            row.get("source_cue"),
            label=f"reviewed speaker machine cue {position}.source_cue",
            cue_count=cue_count,
        )
        if cue_number in machine_rows or cue_number in override_rows:
            raise SpeakerFinalizationError(
                f"reviewed speaker cue ownership is duplicated: {cue_number}"
            )
        _required_text(
            row.get("reason"), label=f"reviewed speaker machine cue {cue_number}.reason"
        )
        arbitration = row.get("arbitration")
        if not isinstance(arbitration, Mapping):
            raise SpeakerFinalizationError(
                f"reviewed speaker machine cue {cue_number}.arbitration must be an object"
            )
        if arbitration.get("human_voice_observed") is not True:
            raise SpeakerFinalizationError(
                f"reviewed speaker machine cue {cue_number} lacks positive voice arbitration"
            )
        receipt_sha = str(arbitration.get("receipt_sha256") or "")
        if not SHA256_RE.fullmatch(receipt_sha):
            raise SpeakerFinalizationError(
                f"reviewed speaker machine cue {cue_number} arbitration receipt hash is invalid"
            )
        expected = _expected_cue(
            row, label=f"reviewed speaker machine cue {cue_number}"
        )
        _assert_expect_matches(
            expected,
            cues[cue_number - 1],
            label=f"reviewed speaker machine cue {cue_number}",
        )
        machine_rows[cue_number] = row

    expected_partition = set(range(1, cue_count + 1))
    actual_partition = set(override_rows) | set(machine_rows)
    if actual_partition != expected_partition:
        missing = sorted(expected_partition - actual_partition)
        extra = sorted(actual_partition - expected_partition)
        raise SpeakerFinalizationError(
            f"reviewed speaker cue partition drift: missing={missing}, extra={extra}"
        )

    anchors_raw = raw.get("anchor_source_cues")
    if not isinstance(anchors_raw, list):
        raise SpeakerFinalizationError(
            "reviewed speaker anchor_source_cues must be a list"
        )
    anchor_numbers: list[int] = []
    for position, value in enumerate(anchors_raw, start=1):
        cue_number = _positive_cue_number(
            value,
            label=f"reviewed speaker anchor {position}",
            cue_count=cue_count,
        )
        if cue_number in anchor_numbers:
            raise SpeakerFinalizationError(
                f"duplicate reviewed speaker anchor: {cue_number}"
            )
        row = override_rows.get(cue_number)
        if row is None or str(row.get("action") or "replace") != "replace":
            raise SpeakerFinalizationError(
                f"reviewed speaker anchor {cue_number} is not a reviewed replacement"
            )
        segments = row.get("segments")
        if not isinstance(segments, list) or len(segments) != 1:
            raise SpeakerFinalizationError(
                f"reviewed speaker anchor {cue_number} must be a nonmixed full cue"
            )
        segment = segments[0]
        expected = row["expect"]
        if (
            not isinstance(segment, Mapping)
            or segment.get("speaker") != HOST_SPEAKER
            or segment.get("start") != expected.get("start")
            or segment.get("end") != expected.get("end")
            or segment.get("text") != expected.get("text")
            or row.get("overlays") not in (None, [])
        ):
            raise SpeakerFinalizationError(
                f"reviewed speaker anchor {cue_number} is not an exact full-cue {HOST_SPEAKER} row"
            )
        anchor_numbers.append(cue_number)
    if len(anchor_numbers) < 2:
        raise SpeakerFinalizationError(
            "reviewed speaker baseline requires at least two exact host anchors"
        )

    evidence: dict[str, object] = {
        "schema_version": REVIEWED_SPEAKER_BASELINE_SCHEMA,
        "authority": authority,
        "truth_input": str(truth_path),
        "truth_input_sha256": truth_sha,
        "cue_count": cue_count,
        "reviewed_cue_count": len(override_rows),
        "machine_cues": sorted(machine_rows),
        "anchor_source_cues": anchor_numbers,
    }
    return ReviewedSpeakerBaseline(
        anchor_labels={number - 1: HOST_SPEAKER for number in anchor_numbers},
        machine_cues=tuple(sorted(machine_rows)),
        evidence=evidence,
    )


def load_speaker_override_state(
    override_path: Path | None,
    *,
    candidate_id: str | None,
    media_path: Path,
    text_srt_path: Path,
    cue_count: int,
) -> SpeakerOverrideState:
    """Validate one optional human override against current frozen inputs."""

    if override_path is None:
        return SpeakerOverrideState(None, {}, None, "")
    loaded = json.loads(override_path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise SpeakerFinalizationError("speaker override document must be an object")
    expected_text = str(loaded.get("text_final_srt_sha256") or "")
    expected_automatic = str(loaded.get("source_srt_sha256") or "")
    expected_media = str(loaded.get("source_media_sha256") or "")
    actual_text = sha256_file(text_srt_path)
    actual_media = sha256_file(media_path)
    if not expected_media:
        raise SpeakerFinalizationError("speaker override is missing source_media_sha256")
    if expected_media != actual_media:
        raise SpeakerFinalizationError(
            f"speaker override media hash mismatch: expected {expected_media!r}, "
            f"got {actual_media!r}"
        )
    if expected_text and expected_text != actual_text:
        raise SpeakerFinalizationError(
            f"speaker override text-final hash mismatch: expected {expected_text!r}, "
            f"got {actual_text!r}"
        )
    if not str(candidate_id or "").strip():
        raise SpeakerFinalizationError(
            "candidate_id is required when a speaker override is present"
        )
    try:
        validate_bound_speaker_override_document(
            override_path,
            candidate_id=str(candidate_id),
            expected_source_media_sha256=actual_media,
            expected_text_final_srt_sha256=actual_text,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise SpeakerFinalizationError(
            f"speaker override authority binding failed: {exc}"
        ) from exc
    return SpeakerOverrideState(
        document=loaded,
        reviewed_votes=_reviewed_context_votes(loaded, cue_count=cue_count),
        reviewed_baseline=load_reviewed_speaker_baseline(
            loaded,
            candidate_id=str(candidate_id),
            cues=parse_srt(text_srt_path),
        ),
        expected_automatic_sha256=expected_automatic,
    )
