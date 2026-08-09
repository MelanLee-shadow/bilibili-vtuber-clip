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
    Cue,
    parse_labelled_srt,
    sha256_file,
    validate_bound_speaker_override_document,
    write_srt,
)
from scripts.apply_subtitle_text_overrides import TextCue, parse_srt
from src.autoslice.speaker_common import (
    GUEST_SPEAKER,
    HOST_SPEAKER,
    SPEAKERS,
    SpeakerFinalizationError,
)
from src.autoslice.speaker_context import _reviewed_context_votes
from src.autoslice.text_baseline_guard import (
    BASELINE_CONTAINS_SPEAKER_LABEL_PREFIX,
    contains_speaker_label_prefix,
)


REVIEWED_SPEAKER_BASELINE_V1_SCHEMA = "reviewed-speaker-baseline.v1"
REVIEWED_SPEAKER_BASELINE_SCHEMA = "reviewed-speaker-baseline.v2"
TRUTH_SCHEMA = "ivan-speaker-truth-diff.v2"
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class ReviewedSpeakerBaseline:
    """Validated early-anchor inputs and their manifest-ready provenance."""

    anchor_labels: dict[int, str]
    machine_cues: tuple[int, ...]
    machine_labels: dict[int, str]
    frozen_automatic: tuple[Cue, ...] | None
    frozen_automatic_sha256: str | None
    evidence: dict[str, object]


@dataclass(frozen=True)
class SpeakerOverrideState:
    """One fully bound optional override and its pre-analysis authorities."""

    document: dict[str, object] | None
    reviewed_votes: dict[int, str]
    reviewed_baseline: ReviewedSpeakerBaseline | None
    expected_automatic_sha256: str


def build_fresh_automatic_labels(
    analysis: Mapping[str, object], cues: Sequence[TextCue]
) -> list[Cue]:
    """Convert analyzer decisions to the complete automatic cue grid."""

    decisions = analysis.get("decisions")
    if not isinstance(decisions, list) or len(decisions) != len(cues):
        raise SpeakerFinalizationError("speaker analyzer returned incomplete decisions")
    automatic = []
    for index, (text_cue, decision) in enumerate(zip(cues, decisions, strict=True), 1):
        if not isinstance(decision, Mapping) or decision.get("speaker") not in SPEAKERS:
            raise SpeakerFinalizationError(f"speaker decision {index} is invalid")
        automatic.append(
            Cue(
                source_index=index,
                start=text_cue.start,
                end=text_cue.end,
                speaker=str(decision["speaker"]),
                text=text_cue.text,
                decision_source=str(decision.get("decision_source") or "campp_audio"),
                note=(
                    f"margin={decision.get('margin')}"
                    if decision.get("margin") is not None
                    else None
                ),
            )
        )
    return automatic


def canonical_labelled_srt_sha256(cues: Sequence[Cue]) -> str:
    """Hash the exact canonical bytes emitted by ``write_srt``."""

    payload = "\n\n".join(
        f"{index}\n{cue.start} --> {cue.end}\n[{cue.speaker}] {cue.text}"
        for index, cue in enumerate(cues, 1)
    ) + "\n"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def materialize_reviewed_automatic_labels(
    fresh: list[Cue],
    *,
    work_dir: Path,
    baseline: ReviewedSpeakerBaseline | None,
) -> tuple[list[Cue], Path, Path | None]:
    """Persist fresh evidence and select the frozen v2 machine baseline."""

    frozen = baseline.frozen_automatic if baseline is not None else None
    fresh_path = (
        work_dir / "automatic-labelled.fresh.srt"
        if frozen is not None
        else work_dir / "automatic-labelled.srt"
    )
    write_srt(fresh, fresh_path)
    selected = list(frozen) if frozen is not None else fresh
    selected_path = work_dir / "automatic-labelled.srt"
    if frozen is not None:
        write_srt(selected, selected_path)
        if (
            baseline is None
            or baseline.frozen_automatic_sha256 != sha256_file(selected_path)
        ):
            raise SpeakerFinalizationError(
                "reviewed speaker frozen automatic canonical SHA-256 drift"
            )
    return selected, selected_path, fresh_path if frozen is not None else None


def reviewed_machine_replay_evidence(
    *,
    analysis: Mapping[str, object],
    selected: Sequence[Cue],
    selected_path: Path,
    fresh_path: Path,
    baseline: ReviewedSpeakerBaseline,
) -> dict[str, object]:
    """Disclose fresh-vs-frozen machine-cue drift without changing authority."""

    decisions = analysis["decisions"]
    if not isinstance(decisions, list):
        raise SpeakerFinalizationError("speaker analyzer returned incomplete decisions")
    drift = []
    for index in sorted(baseline.machine_labels):
        decision = decisions[index]
        if not isinstance(decision, Mapping):
            raise SpeakerFinalizationError("speaker analyzer returned invalid decisions")
        fresh_speaker = str(decision.get("speaker") or "")
        if fresh_speaker != selected[index].speaker:
            drift.append(
                {
                    "source_cue": index + 1,
                    "frozen_speaker": selected[index].speaker,
                    "fresh_speaker": fresh_speaker,
                }
            )
    return {
        "schema_version": "reviewed-machine-baseline-replay.v1",
        "status": "FROZEN_MACHINE_BASELINE_APPLIED",
        "frozen_automatic_srt_sha256": sha256_file(selected_path),
        "fresh_automatic_srt_sha256": sha256_file(fresh_path),
        "machine_cue_label_drift": drift,
    }


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


def _resolve_repo_input(
    file_input: Mapping[str, object],
    *,
    repo_root: Path,
    label: str,
) -> tuple[Path, str]:
    if set(file_input) != {"path", "sha256"}:
        raise SpeakerFinalizationError(
            f"{label} must contain exactly path/sha256"
        )
    raw_path = _required_text(
        file_input.get("path"), label=f"{label}.path"
    )
    relative = Path(raw_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise SpeakerFinalizationError(
            f"{label}.path must be repository-relative"
        )
    root = repo_root.resolve(strict=True)
    candidate = root / relative
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise SpeakerFinalizationError(
            f"{label} is unavailable"
        ) from exc
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise SpeakerFinalizationError(f"{label} escapes the repository")
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise SpeakerFinalizationError(
                f"{label} must not traverse symlinks"
            )
    expected_sha = str(file_input.get("sha256") or "")
    if not SHA256_RE.fullmatch(expected_sha):
        raise SpeakerFinalizationError(f"{label}.sha256 must be a SHA-256 digest")
    actual_sha = _sha256(resolved)
    if actual_sha != expected_sha:
        raise SpeakerFinalizationError(f"{label} hash drift")
    return resolved, actual_sha


def _resolve_truth_input(
    truth_input: Mapping[str, object],
    *,
    repo_root: Path,
    candidate_id: str,
) -> tuple[Path, str]:
    resolved, actual_sha = _resolve_repo_input(
        truth_input,
        repo_root=repo_root,
        label="reviewed speaker truth input",
    )
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


def _resolve_automatic_input(
    automatic_input: Mapping[str, object],
    *,
    repo_root: Path,
    cues: Sequence[object],
    expected_sha256: str,
) -> tuple[Path, str, tuple[Cue, ...]]:
    resolved, actual_sha = _resolve_repo_input(
        automatic_input,
        repo_root=repo_root,
        label="reviewed speaker automatic input",
    )
    if actual_sha != expected_sha256:
        raise SpeakerFinalizationError(
            "reviewed speaker automatic input does not match source_srt_sha256"
        )
    automatic = tuple(parse_labelled_srt(resolved))
    if len(automatic) != len(cues):
        raise SpeakerFinalizationError("reviewed speaker automatic cue count drift")
    for position, (frozen, current) in enumerate(
        zip(automatic, cues, strict=True), start=1
    ):
        if frozen.source_index != position:
            raise SpeakerFinalizationError(
                "reviewed speaker automatic cue indices are not contiguous"
            )
        for field in ("start", "end", "text"):
            if getattr(frozen, field) != getattr(current, field, None):
                raise SpeakerFinalizationError(
                    f"reviewed speaker automatic cue {position} {field} drift"
                )
    return resolved, actual_sha, automatic


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
    schema = raw.get("schema_version")
    expected_keys = {
        "schema_version",
        "authority",
        "truth_input",
        "cue_count",
        "anchor_source_cues",
        "machine_cues",
    }
    if schema == REVIEWED_SPEAKER_BASELINE_SCHEMA:
        expected_keys.add("automatic_input")
    if set(raw) != expected_keys:
        raise SpeakerFinalizationError(
            "reviewed_speaker_baseline fields are incomplete or unsupported"
        )
    if schema not in {
        REVIEWED_SPEAKER_BASELINE_V1_SCHEMA,
        REVIEWED_SPEAKER_BASELINE_SCHEMA,
    }:
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
    frozen_automatic: tuple[Cue, ...] | None = None
    automatic_path: Path | None = None
    automatic_sha = str(document.get("source_srt_sha256") or "")
    if schema == REVIEWED_SPEAKER_BASELINE_SCHEMA:
        if not SHA256_RE.fullmatch(automatic_sha):
            raise SpeakerFinalizationError(
                "reviewed speaker source_srt_sha256 is invalid"
            )
        automatic_input = raw.get("automatic_input")
        if not isinstance(automatic_input, Mapping):
            raise SpeakerFinalizationError(
                "reviewed speaker automatic_input must be an object"
            )
        automatic_path, automatic_sha, frozen_automatic = _resolve_automatic_input(
            automatic_input,
            repo_root=repo_root or REPO_ROOT,
            cues=cues,
            expected_sha256=automatic_sha,
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
        expected_machine_keys = {
            "source_cue", "expect", "reason", "arbitration"
        }
        if schema == REVIEWED_SPEAKER_BASELINE_SCHEMA:
            expected_machine_keys.add("speaker")
        if set(row) != expected_machine_keys:
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
        if (
            schema == REVIEWED_SPEAKER_BASELINE_SCHEMA
            and row.get("speaker") not in {HOST_SPEAKER, GUEST_SPEAKER}
        ):
            raise SpeakerFinalizationError(
                f"reviewed speaker machine cue {cue_number} speaker is invalid"
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
        if (
            frozen_automatic is not None
            and frozen_automatic[cue_number - 1].speaker != row.get("speaker")
        ):
            raise SpeakerFinalizationError(
                f"reviewed speaker machine cue {cue_number} frozen label drift"
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
        "schema_version": schema,
        "authority": authority,
        "truth_input": str(truth_path),
        "truth_input_sha256": truth_sha,
        "cue_count": cue_count,
        "reviewed_cue_count": len(override_rows),
        "machine_cues": sorted(machine_rows),
        "machine_labels": {
            str(number): str(machine_rows[number]["speaker"])
            for number in sorted(machine_rows)
            if "speaker" in machine_rows[number]
        },
        "source_automatic_srt_sha256": automatic_sha,
        "anchor_source_cues": anchor_numbers,
    }
    if automatic_path is not None:
        evidence["automatic_input"] = str(automatic_path)
    return ReviewedSpeakerBaseline(
        anchor_labels={number - 1: HOST_SPEAKER for number in anchor_numbers},
        machine_cues=tuple(sorted(machine_rows)),
        machine_labels={
            number - 1: str(row["speaker"])
            for number, row in machine_rows.items()
            if "speaker" in row
        },
        frozen_automatic=frozen_automatic,
        frozen_automatic_sha256=(
            automatic_sha if frozen_automatic is not None else None
        ),
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
