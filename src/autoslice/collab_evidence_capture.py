"""Fail-closed, unlabelled evidence capture for future collab sessions.

This module deliberately does not infer speaker identity.  Cheap metadata may
only decide whether an unlabelled evidence package is worth collecting.  The
package and its queue are explicitly barred from training and upload until a
separate, human-reviewed authority promotes them.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import signal
import shutil
import subprocess
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence

from src.autoslice.jingting_chunker import parse_srt_cues


SCHEMA_VERSION = "collab-evidence-capture.v1"
QUEUE_SCHEMA_VERSION = "collab-evidence-queue.v1"
ALERT_SCHEMA_VERSION = "collab-evidence-quota-alert.v1"
WORKER_REQUEST_SCHEMA_VERSION = "collab-evidence-worker-request.v1"
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
CAPTURE_ID_RE = re.compile(r"[0-9a-f]{64}\Z")
MAX_CUES = 120
DEFAULT_QUOTA = 5
MIN_CAPTURE_DATE = "2026-07-13"
WORKER_WALL_SECONDS = 900

# These are deliberately semantic families, not a bag of repeated keywords.
# Text is used only to decide whether to capture unlabelled evidence; it never
# becomes a speaker label, prediction, or routing decision.
TEXT_SIGNAL_PATTERNS: dict[str, re.Pattern[str]] = {
    "EXPLICIT_COLLAB": re.compile(r"联动|一起直播"),
    "LIVE_CONNECTION": re.compile(r"连麦|连线"),
    "GUEST_ROLE": re.compile(r"嘉宾"),
}
ALLOWED_TEXT_SIGNAL_CLASSES = frozenset(TEXT_SIGNAL_PATTERNS)


class CollabEvidenceCaptureError(RuntimeError):
    pass


@dataclass(frozen=True)
class TriggerEvaluation:
    triggered: bool
    reason_codes: tuple[str, ...]
    text_signal_classes: tuple[str, ...]
    routing_candidate_ids: tuple[str, ...]


def validate_opaque_trigger(trigger: TriggerEvaluation) -> TriggerEvaluation:
    """Validate the only trigger shape allowed across the worker boundary."""

    reason_codes = tuple(trigger.reason_codes)
    signal_classes = tuple(trigger.text_signal_classes)
    if (
        trigger.triggered is not True
        or trigger.routing_candidate_ids
        or not all(isinstance(value, str) for value in reason_codes)
        or not all(isinstance(value, str) for value in signal_classes)
        or signal_classes != tuple(sorted(set(signal_classes)))
        or any(value not in ALLOWED_TEXT_SIGNAL_CLASSES for value in signal_classes)
    ):
        raise CollabEvidenceCaptureError("worker trigger override is not opaque/safe")

    expected_reason_codes: list[str] = []
    if "PROVIDER_CAPTURE_TRIGGER" in reason_codes:
        expected_reason_codes.append("PROVIDER_CAPTURE_TRIGGER")
    if signal_classes:
        if len(signal_classes) < 2:
            raise CollabEvidenceCaptureError("worker trigger text evidence is insufficient")
        expected_reason_codes.append(
            "TEXT_SIGNAL_CLASSES:" + ",".join(signal_classes)
        )
    if not expected_reason_codes or reason_codes != tuple(expected_reason_codes):
        raise CollabEvidenceCaptureError("worker trigger reason codes are inconsistent")
    return trigger


def _candidate_id(candidate: Mapping[str, object]) -> str:
    return str(candidate.get("cid") or candidate.get("candidate_id") or "").strip()


def evaluate_trigger(
    routing_claim: Mapping[str, object] | None,
    candidates: Sequence[Mapping[str, object]],
) -> TriggerEvaluation:
    """Return a pure capture decision without opening or statting any path.

    ``routing_claim`` must already have passed the speaker router's provider
    validation in the caller.  Here we additionally require provider-backed
    result shape, and only MULTI_SPEAKER/UNCERTAIN can trigger capture.  The
    fallback RUN_BINARY_FINALIZER decision by itself is not evidence.
    """

    candidate_ids = {_candidate_id(item) for item in candidates}
    candidate_ids.discard("")
    routing_hits: list[tuple[str, str]] = []
    if isinstance(routing_claim, Mapping):
        digest = str(routing_claim.get("provider_evidence_sha256") or "")
        results = routing_claim.get("candidate_results")
        provider_backed = (
            routing_claim.get("status") == "READY"
            and SHA256_RE.fullmatch(digest) is not None
            and isinstance(routing_claim.get("provider"), Mapping)
            and isinstance(results, list)
        )
        if provider_backed:
            for raw in results:
                if not isinstance(raw, Mapping):
                    continue
                cid = str(raw.get("candidate_id") or "")
                verdict = str(raw.get("verdict") or "")
                if cid in candidate_ids and verdict in {"MULTI_SPEAKER", "UNCERTAIN"}:
                    routing_hits.append((cid, verdict))

    signal_classes: set[str] = set()
    for candidate in candidates:
        # Do not inspect xml/chat_jsonl/danmaku or copy source text into output.
        text = "\n".join(
            str(candidate.get(field) or "") for field in ("hook", "preview")
        )
        for signal_class, pattern in TEXT_SIGNAL_PATTERNS.items():
            if pattern.search(text):
                signal_classes.add(signal_class)

    # Never persist provider verdicts or candidate identity in an unlabelled
    # package.  The already-verified claim may only yield one opaque session
    # trigger bit.
    persisted_signal_classes = (
        tuple(sorted(signal_classes)) if len(signal_classes) >= 2 else ()
    )
    reason_codes = ["PROVIDER_CAPTURE_TRIGGER"] if routing_hits else []
    if len(signal_classes) >= 2:
        reason_codes.append(
            "TEXT_SIGNAL_CLASSES:" + ",".join(sorted(signal_classes))
        )
    triggered = bool(routing_hits) or len(signal_classes) >= 2
    return TriggerEvaluation(
        triggered=triggered,
        reason_codes=tuple(reason_codes),
        text_signal_classes=persisted_signal_classes,
        routing_candidate_ids=(),
    )


def _stat_signature(path: Path) -> tuple[int, int, int, int, int]:
    stat = path.stat()
    return (
        int(stat.st_dev),
        int(stat.st_ino),
        int(stat.st_size),
        int(stat.st_mtime_ns),
        int(stat.st_ctime_ns),
    )


def _strict_regular_path(value: object, *, field: str) -> Path:
    path = Path(str(value or ""))
    if not path.is_absolute():
        raise CollabEvidenceCaptureError(f"{field} must be absolute")
    absolute = path.absolute()
    if absolute.is_symlink():
        raise CollabEvidenceCaptureError(f"{field} must not be a symlink")
    try:
        resolved = absolute.resolve(strict=True)
    except OSError as exc:
        raise CollabEvidenceCaptureError(f"{field} is unavailable: {exc}") from exc
    if resolved != absolute or not resolved.is_file():
        raise CollabEvidenceCaptureError(
            f"{field} must be a canonical regular file without symlink traversal"
        )
    return resolved


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ffprobe_duration_ms(path: Path) -> int:
    try:
        completed = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "csv=p=0",
                str(path),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CollabEvidenceCaptureError(f"ffprobe failed: {exc}") from exc
    try:
        duration_ms = int(float(completed.stdout.strip()) * 1000)
    except ValueError as exc:
        raise CollabEvidenceCaptureError("ffprobe returned no usable duration") from exc
    if completed.returncode != 0 or duration_ms <= 0:
        raise CollabEvidenceCaptureError("source media duration is invalid")
    return duration_ms


def ffmpeg_extract_cue(
    source: Path, start_ms: int, end_ms: int, destination: Path
) -> None:
    duration_ms = end_ms - start_ms
    if start_ms < 0 or duration_ms <= 0:
        raise CollabEvidenceCaptureError("collab evidence cue interval is invalid")
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        completed = subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-ss",
                f"{start_ms / 1000:.3f}",
                "-i",
                str(source),
                "-t",
                f"{duration_ms / 1000:.3f}",
                "-map",
                "0:a:0",
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-c:a",
                "pcm_s16le",
                str(destination),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=min(60, max(15, duration_ms // 1000 * 2 + 10)),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        destination.unlink(missing_ok=True)
        raise CollabEvidenceCaptureError(f"collab evidence ffmpeg failed: {exc}") from exc
    if completed.returncode != 0:
        destination.unlink(missing_ok=True)
        raise CollabEvidenceCaptureError(
            f"collab evidence ffmpeg failed rc={completed.returncode}: "
            f"{completed.stderr[-300:]}"
        )


def _validate_pcm16_mono_16k(path: Path, *, expected_duration_ms: int) -> dict[str, int]:
    try:
        with wave.open(str(path), "rb") as source:
            channels = source.getnchannels()
            sample_width = source.getsampwidth()
            sample_rate = source.getframerate()
            frame_count = source.getnframes()
            compression = source.getcomptype()
    except (OSError, EOFError, wave.Error) as exc:
        raise CollabEvidenceCaptureError(f"cue WAV is invalid: {exc}") from exc
    if (
        channels != 1
        or sample_width != 2
        or sample_rate != 16_000
        or frame_count <= 0
        or compression != "NONE"
    ):
        raise CollabEvidenceCaptureError("cue WAV must be PCM16 mono 16 kHz")
    actual_duration_ms = round(frame_count * 1000 / sample_rate)
    tolerance_ms = max(250, round(expected_duration_ms * 0.05))
    if abs(actual_duration_ms - expected_duration_ms) > tolerance_ms:
        raise CollabEvidenceCaptureError("cue WAV duration does not match requested interval")
    return {
        "channels": channels,
        "sample_width_bytes": sample_width,
        "sample_rate_hz": sample_rate,
        "frame_count": frame_count,
        "duration_ms": actual_duration_ms,
    }


def _canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _safe_root(path: Path, *, field: str) -> Path:
    """Create a root only after its nearest existing parent is canonical."""

    absolute = path.absolute()
    cursor = absolute
    while not cursor.exists():
        if cursor.parent == cursor:
            raise CollabEvidenceCaptureError(f"{field} has no existing parent")
        cursor = cursor.parent
    if cursor.is_symlink() or cursor.resolve(strict=True) != cursor:
        raise CollabEvidenceCaptureError(f"{field} must not traverse symlinks")
    absolute.mkdir(parents=True, exist_ok=True)
    if absolute.is_symlink() or absolute.resolve(strict=True) != absolute:
        raise CollabEvidenceCaptureError(f"{field} must not traverse symlinks")
    return absolute


def _manifest_integrity_sha256(document: Mapping[str, object]) -> str:
    unsigned = {
        key: value
        for key, value in document.items()
        if key != "manifest_integrity_sha256"
    }
    return hashlib.sha256(_canonical_json_bytes(unsigned)).hexdigest()


def _manifest_identity(document: Mapping[str, object]) -> dict[str, object]:
    trigger = document.get("trigger")
    cues = document.get("cues")
    if not isinstance(trigger, Mapping) or not isinstance(cues, list):
        raise CollabEvidenceCaptureError("capture manifest trigger/cues are invalid")
    identity_cues: list[dict[str, object]] = []
    for cue in cues:
        if not isinstance(cue, Mapping):
            raise CollabEvidenceCaptureError("capture manifest cue is invalid")
        identity_cues.append(
            {
                "source_cue": cue.get("source_cue"),
                "segment_path": cue.get("segment_path"),
                "segment_sha256": cue.get("segment_sha256"),
                "bcut_srt_path": cue.get("bcut_srt_path"),
                "bcut_srt_sha256": cue.get("bcut_srt_sha256"),
                "start_ms": cue.get("start_ms"),
                "end_ms": cue.get("end_ms"),
                "source_duration_ms": cue.get("source_duration_ms"),
                "text_sha256": cue.get("text_sha256"),
                "sampling_stratum": cue.get("sampling_stratum"),
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "date": document.get("date"),
        "trigger_reason_codes": trigger.get("reason_codes"),
        "cues": identity_cues,
    }


def _cue_binding_sha256(capture_id: str, cue: Mapping[str, object]) -> str:
    binding = {
        "capture_id": capture_id,
        "source_cue": cue.get("source_cue"),
        "segment_sha256": cue.get("segment_sha256"),
        "bcut_srt_sha256": cue.get("bcut_srt_sha256"),
        "start_ms": cue.get("start_ms"),
        "end_ms": cue.get("end_ms"),
        "source_duration_ms": cue.get("source_duration_ms"),
        "text_sha256": cue.get("text_sha256"),
        "sampling_stratum": cue.get("sampling_stratum"),
    }
    return hashlib.sha256(_canonical_json_bytes(binding)).hexdigest()


def _ensure_canonical_directory(root: Path, path: Path) -> Path:
    """Create ``path`` beneath ``root`` without accepting symlink indirection."""

    path.mkdir(parents=True, exist_ok=True)
    absolute = path.absolute()
    try:
        resolved = absolute.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise CollabEvidenceCaptureError("capture directory escapes its root") from exc
    if resolved != absolute or not resolved.is_dir():
        raise CollabEvidenceCaptureError("capture directory must not traverse symlinks")
    return resolved


def _overlaps_song(
    candidate: Mapping[str, object], song_intervals: Iterable[Mapping[str, object]]
) -> bool:
    segment = Path(str(candidate.get("segment_path") or "")).name
    start = candidate.get("start_ms")
    end = candidate.get("end_ms")
    if (
        not segment
        or isinstance(start, bool)
        or not isinstance(start, int)
        or isinstance(end, bool)
        or not isinstance(end, int)
    ):
        return False
    for interval in song_intervals:
        other_segment = Path(str(interval.get("segment_path") or "")).name
        other_start = interval.get("start_ms")
        other_end = interval.get("end_ms")
        if (
            other_segment == segment
            and isinstance(other_start, int)
            and not isinstance(other_start, bool)
            and isinstance(other_end, int)
            and not isinstance(other_end, bool)
            and max(start, other_start) < min(end, other_end)
        ):
            return True
    return False


def _prepare_sources(
    candidates: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    prepared: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    ordered = sorted(
        candidates,
        key=lambda row: (
            str(row.get("segment_path") or ""),
            str(row.get("bcut_srt_path") or ""),
        ),
    )
    for raw in ordered:
        identity = (
            str(raw.get("segment_path") or ""),
            str(raw.get("bcut_srt_path") or ""),
        )
        if not all(identity):
            raise CollabEvidenceCaptureError("candidate source/SRT path is missing")
        if identity in seen:
            continue
        seen.add(identity)
        prepared.append(
            {
                "segment_path": raw.get("segment_path"),
                "bcut_srt_path": raw.get("bcut_srt_path"),
            }
        )
    return prepared


def _duration_stratum(duration_ms: int) -> str:
    if duration_ms < 1_500:
        return "short"
    if duration_ms < 5_000:
        return "medium"
    return "long"


def _evenly_sample(rows: Sequence[dict[str, object]], limit: int) -> list[dict[str, object]]:
    if limit <= 0 or not rows:
        return []
    if len(rows) <= limit:
        return list(rows)
    if limit == 1:
        return [rows[len(rows) // 2]]
    return [rows[(index * (len(rows) - 1)) // (limit - 1)] for index in range(limit)]


def _sample_cues(rows: Sequence[dict[str, object]]) -> list[dict[str, object]]:
    """Sample short/medium/long cues across the full sealed session timeline."""

    ordered = sorted(
        rows,
        key=lambda row: (
            str(row["segment_path"]),
            int(row["start_ms"]),
            int(row["source_cue"]),
        ),
    )
    targets = {"short": 30, "medium": 60, "long": 30}
    selected: list[dict[str, object]] = []
    for stratum in ("short", "medium", "long"):
        pool = [row for row in ordered if row["sampling_stratum"] == stratum]
        selected.extend(_evenly_sample(pool, targets[stratum]))
    selected_ids = {
        (str(row["segment_path"]), int(row["source_cue"])) for row in selected
    }
    if len(selected) < MAX_CUES:
        remainder = [
            row
            for row in ordered
            if (str(row["segment_path"]), int(row["source_cue"])) not in selected_ids
        ]
        selected.extend(_evenly_sample(remainder, MAX_CUES - len(selected)))
    return sorted(
        selected[:MAX_CUES],
        key=lambda row: (
            str(row["segment_path"]),
            int(row["start_ms"]),
            int(row["source_cue"]),
        ),
    )


def _manifest_is_unlabelled(document: Mapping[str, object]) -> bool:
    cues = document.get("cues")
    return (
        document.get("schema_version") == SCHEMA_VERSION
        and document.get("status") == "UNLABELLED_EVIDENCE_ONLY"
        and document.get("labels_present") is False
        and document.get("predictions_present") is False
        and document.get("training") is False
        and document.get("upload") is False
        and document.get("authorized_for_training") is False
        and document.get("authorized_for_upload") is False
        and document.get("confirmed_collab_session") is False
        and document.get("training_ready") is False
        and isinstance(cues, list)
        and document.get("cue_count") == len(cues)
        and all(
            isinstance(cue, Mapping)
            and cue.get("label") is None
            and cue.get("prediction") is None
            for cue in cues
        )
    )


def rebuild_queue(
    base_dir: Path,
    *,
    quota: int = DEFAULT_QUOTA,
    alert_dir: Path | None = None,
) -> dict[str, object]:
    """Rebuild a candidate-session queue from verified unlabelled packages."""

    if isinstance(quota, bool) or not isinstance(quota, int) or quota <= 0:
        raise CollabEvidenceCaptureError("quota must be a positive integer")
    root = _safe_root(base_dir, field="capture root")

    entries_by_id: dict[str, dict[str, object]] = {}
    capture_id_by_date: dict[str, str] = {}
    sessions_root = root / "sessions"
    if sessions_root.is_dir():
        _ensure_canonical_directory(root, sessions_root)
        for manifest_path in sorted(sessions_root.glob("*/*/manifest.json")):
            absolute_manifest = manifest_path.absolute()
            if (
                absolute_manifest.is_symlink()
                or not absolute_manifest.is_file()
                or absolute_manifest.resolve(strict=True) != absolute_manifest
            ):
                raise CollabEvidenceCaptureError("queue manifest must be a regular file")
            try:
                document = json.loads(absolute_manifest.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise CollabEvidenceCaptureError(f"invalid capture manifest: {exc}") from exc
            if not isinstance(document, Mapping):
                raise CollabEvidenceCaptureError("queue manifest must be an object")
            if document.get("manifest_integrity_sha256") != _manifest_integrity_sha256(document):
                raise CollabEvidenceCaptureError("capture manifest integrity mismatch")
            if not _manifest_is_unlabelled(document):
                raise CollabEvidenceCaptureError("queue manifest is not sealed unlabelled evidence")
            capture_id = str(document.get("capture_id") or "")
            date = str(document.get("date") or "")
            if (
                not CAPTURE_ID_RE.fullmatch(capture_id)
                or not DATE_RE.fullmatch(date)
                or absolute_manifest.parent.name != capture_id
                or absolute_manifest.parent.parent.name != date
            ):
                raise CollabEvidenceCaptureError("capture manifest path/identity mismatch")
            expected_capture_id = hashlib.sha256(
                _canonical_json_bytes(_manifest_identity(document))
            ).hexdigest()
            if expected_capture_id != capture_id:
                raise CollabEvidenceCaptureError("capture manifest identity mismatch")
            cues = document.get("cues")
            if (
                not isinstance(cues, list)
                or not cues
                or len(cues) > MAX_CUES
                or document.get("cue_count") != len(cues)
            ):
                raise CollabEvidenceCaptureError("capture manifest cue inventory is invalid")
            for cue in cues:
                if not isinstance(cue, Mapping):
                    raise CollabEvidenceCaptureError("capture manifest cue is invalid")
                cue_id = str(cue.get("cue_id") or "")
                if cue_id != _cue_binding_sha256(capture_id, cue):
                    raise CollabEvidenceCaptureError("capture cue binding mismatch")
                if cue.get("label") is not None or cue.get("prediction") is not None:
                    raise CollabEvidenceCaptureError("capture cue contains a label/prediction")
                relative_audio = str(cue.get("audio_path") or "")
                if relative_audio != f"{cue_id}.wav":
                    raise CollabEvidenceCaptureError("capture cue audio path is not canonical")
                audio_path = (absolute_manifest.parent / relative_audio).absolute()
                start_ms = cue.get("start_ms")
                end_ms = cue.get("end_ms")
                source_duration_ms = cue.get("source_duration_ms")
                if (
                    isinstance(start_ms, bool)
                    or not isinstance(start_ms, int)
                    or isinstance(end_ms, bool)
                    or not isinstance(end_ms, int)
                    or isinstance(source_duration_ms, bool)
                    or not isinstance(source_duration_ms, int)
                    or start_ms < 0
                    or end_ms <= start_ms
                    or end_ms > source_duration_ms
                ):
                    raise CollabEvidenceCaptureError("capture cue interval is out of source bounds")
                if (
                    audio_path.is_symlink()
                    or not audio_path.is_file()
                    or audio_path.resolve(strict=True).parent != absolute_manifest.parent
                    or _sha256_file(audio_path) != cue.get("audio_sha256")
                ):
                    raise CollabEvidenceCaptureError("capture cue audio binding mismatch")
                observed_format = _validate_pcm16_mono_16k(
                    audio_path, expected_duration_ms=end_ms - start_ms
                )
                if observed_format != cue.get("audio_format"):
                    raise CollabEvidenceCaptureError("capture cue audio format binding mismatch")
            # July 9 and the already-used July 12 corpus remain development-only.
            if date < MIN_CAPTURE_DATE:
                continue
            prior_capture_id = capture_id_by_date.get(date)
            if prior_capture_id is not None and prior_capture_id != capture_id:
                raise CollabEvidenceCaptureError(
                    "multiple capture packages claim the same livestream date"
                )
            capture_id_by_date[date] = capture_id
            entry = {
                "capture_id": capture_id,
                "date": date,
                "cue_count": document.get("cue_count"),
                "manifest_path": str(absolute_manifest),
                "manifest_sha256": _sha256_file(absolute_manifest),
                "labels_present": False,
                "predictions_present": False,
                "training": False,
                "upload": False,
            }
            existing = entries_by_id.get(capture_id)
            if existing is not None and existing != entry:
                raise CollabEvidenceCaptureError("capture_id collision in queue")
            entries_by_id[capture_id] = entry

    entries = sorted(
        entries_by_id.values(),
        key=lambda row: (str(row["date"]), str(row["capture_id"])),
    )
    cycle_id = hashlib.sha256(
        f"{MIN_CAPTURE_DATE}:{quota}".encode("utf-8")
    ).hexdigest()[:16]
    queue = {
        "schema_version": QUEUE_SCHEMA_VERSION,
        "cycle_id": cycle_id,
        "minimum_capture_date": MIN_CAPTURE_DATE,
        "candidate_session_quota": quota,
        "candidate_session_quota_reached": len(entries) >= quota,
        "candidate_session_count": len(entries),
        "confirmed_collab_session_count": 0,
        "labels_present": False,
        "predictions_present": False,
        "training": False,
        "upload": False,
        "training_ready": False,
        "entries": entries,
    }
    queue_dir = _ensure_canonical_directory(root, root / "queue")
    _atomic_write(queue_dir / "queue.v1.json", _canonical_json_bytes(queue))

    if queue["candidate_session_quota_reached"]:
        alerts = _safe_root(alert_dir or (root / "alerts"), field="alert root")
        alert_path = (
            alerts / f"ALERT_COLLAB_EVIDENCE_CANDIDATE_QUOTA_{cycle_id}.txt"
        )
        payload = _canonical_json_bytes(
            {
                "schema_version": ALERT_SCHEMA_VERSION,
                "cycle_id": cycle_id,
                "candidate_session_quota": quota,
                "candidate_session_count_at_first_alert": len(entries),
                "confirmed_collab_session_count": 0,
                "labels_present": False,
                "predictions_present": False,
                "training": False,
                "upload": False,
                "training_ready": False,
            }
        )
        try:
            descriptor = os.open(
                alert_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError:
            pass
        else:
            try:
                with os.fdopen(descriptor, "wb") as sink:
                    sink.write(payload)
                    sink.flush()
                    os.fsync(sink.fileno())
            except Exception:
                alert_path.unlink(missing_ok=True)
                raise
    return queue


def capture_session(
    date: str,
    candidates: Sequence[Mapping[str, object]],
    *,
    routing_claim: Mapping[str, object] | None,
    song_intervals: Sequence[Mapping[str, object]] = (),
    base_dir: Path,
    extractor: Callable[[Path, int, int, Path], None],
    hash_file: Callable[[Path], str] = _sha256_file,
    duration_probe: Callable[[Path], int] = ffprobe_duration_ms,
    quota: int = DEFAULT_QUOTA,
    alert_dir: Path | None = None,
    trigger_override: TriggerEvaluation | None = None,
) -> dict[str, object]:
    """Capture one unlabelled collab evidence session transactionally.

    The trigger is evaluated before *any* filesystem operation or injected
    callback.  This makes the overwhelmingly common solo/no-trigger path a
    true zero-media-I/O path.
    """

    trigger = trigger_override or evaluate_trigger(routing_claim, candidates)
    if trigger_override is not None:
        validate_opaque_trigger(trigger)
    if not trigger.triggered:
        return {"status": "NO_TRIGGER", "trigger": trigger}
    if not DATE_RE.fullmatch(str(date)):
        raise CollabEvidenceCaptureError("date must be strict YYYY-MM-DD")

    prepared_sources = _prepare_sources(candidates)
    if not prepared_sources:
        return {"status": "NO_ELIGIBLE_CUES", "trigger": trigger}

    root = _safe_root(base_dir, field="capture root")

    bound_sources: list[dict[str, object]] = []
    file_authority: dict[Path, dict[str, object]] = {}
    for candidate in prepared_sources:
        source = _strict_regular_path(candidate["segment_path"], field="segment_path")
        srt = _strict_regular_path(candidate["bcut_srt_path"], field="bcut_srt_path")
        for path in (source, srt):
            if path not in file_authority:
                before = _stat_signature(path)
                digest = str(hash_file(path))
                after = _stat_signature(path)
                if before != after or not SHA256_RE.fullmatch(digest):
                    raise CollabEvidenceCaptureError("source/SRT changed while initial hashing")
                file_authority[path] = {"stat": before, "sha256": digest}
        source_duration_ms = int(duration_probe(source))
        if source_duration_ms <= 0:
            raise CollabEvidenceCaptureError("source media duration is invalid")
        bound_sources.append(
            {
                "segment_path": source,
                "bcut_srt_path": srt,
                "source_duration_ms": source_duration_ms,
            }
        )

    all_cues: list[dict[str, object]] = []
    for source_row in bound_sources:
        source = source_row["segment_path"]
        srt = source_row["bcut_srt_path"]
        source_duration_ms = int(source_row["source_duration_ms"])
        try:
            parsed = parse_srt_cues(srt.read_text(encoding="utf-8"))
        except (OSError, UnicodeError) as exc:
            raise CollabEvidenceCaptureError(f"cannot parse capture SRT: {exc}") from exc
        for source_cue, cue in enumerate(parsed, 1):
            duration_ms = cue.end_ms - cue.start_ms
            if (
                duration_ms < 300
                or duration_ms > 15_000
                or cue.start_ms < 0
                or cue.end_ms > source_duration_ms
            ):
                continue
            row: dict[str, object] = {
                "source_cue": source_cue,
                "segment_path": source,
                "bcut_srt_path": srt,
                "start_ms": cue.start_ms,
                "end_ms": cue.end_ms,
                "source_duration_ms": source_duration_ms,
                "text_sha256": hashlib.sha256(cue.text.encode("utf-8")).hexdigest(),
                "sampling_stratum": _duration_stratum(duration_ms),
            }
            if _overlaps_song(row, song_intervals):
                continue
            all_cues.append(row)
    bound = _sample_cues(all_cues)
    if not bound:
        return {"status": "NO_ELIGIBLE_CUES", "trigger": trigger}

    identity = {
        "schema_version": SCHEMA_VERSION,
        "date": date,
        "trigger_reason_codes": list(trigger.reason_codes),
        "cues": [
            {
                "source_cue": row["source_cue"],
                "segment_path": str(row["segment_path"]),
                "segment_sha256": file_authority[row["segment_path"]]["sha256"],
                "bcut_srt_path": str(row["bcut_srt_path"]),
                "bcut_srt_sha256": file_authority[row["bcut_srt_path"]]["sha256"],
                "start_ms": row["start_ms"],
                "end_ms": row["end_ms"],
                "source_duration_ms": row["source_duration_ms"],
                "text_sha256": row["text_sha256"],
                "sampling_stratum": row["sampling_stratum"],
            }
            for row in bound
        ],
    }
    capture_id = hashlib.sha256(_canonical_json_bytes(identity)).hexdigest()
    final_dir = root / "sessions" / date / capture_id
    if final_dir.is_dir():
        queue = rebuild_queue(root, quota=quota, alert_dir=alert_dir)
        return {
            "status": "ALREADY_CAPTURED",
            "capture_id": capture_id,
            "manifest_path": str(final_dir / "manifest.json"),
            "queue": queue,
            "trigger": trigger,
        }

    staging_parent = _ensure_canonical_directory(root, root / ".staging")
    staging = Path(tempfile.mkdtemp(prefix=f"{capture_id}.", dir=staging_parent))
    published = False
    try:
        cue_documents: list[dict[str, object]] = []
        for row in bound:
            cue_document: dict[str, object] = {
                "source_cue": row["source_cue"],
                "segment_path": str(row["segment_path"]),
                "segment_sha256": file_authority[row["segment_path"]]["sha256"],
                "bcut_srt_path": str(row["bcut_srt_path"]),
                "bcut_srt_sha256": file_authority[row["bcut_srt_path"]]["sha256"],
                "start_ms": row["start_ms"],
                "end_ms": row["end_ms"],
                "source_duration_ms": row["source_duration_ms"],
                "text_sha256": row["text_sha256"],
                "sampling_stratum": row["sampling_stratum"],
            }
            cue_id = _cue_binding_sha256(capture_id, cue_document)
            audio_path = staging / f"{cue_id}.wav"
            extractor(
                row["segment_path"],
                int(row["start_ms"]),
                int(row["end_ms"]),
                audio_path,
            )
            if audio_path.is_symlink() or not audio_path.is_file() or audio_path.stat().st_size <= 0:
                raise CollabEvidenceCaptureError("extractor did not create a regular non-empty cue")
            if audio_path.resolve(strict=True).parent != staging.resolve(strict=True):
                raise CollabEvidenceCaptureError("extractor output escaped staging")
            audio_format = _validate_pcm16_mono_16k(
                audio_path,
                expected_duration_ms=int(row["end_ms"]) - int(row["start_ms"]),
            )
            audio_sha = str(hash_file(audio_path))
            if not SHA256_RE.fullmatch(audio_sha):
                raise CollabEvidenceCaptureError("extractor output hash is invalid")
            cue_documents.append(
                {
                    "cue_id": cue_id,
                    **cue_document,
                    "audio_path": audio_path.name,
                    "audio_sha256": audio_sha,
                    "audio_format": audio_format,
                    "label": None,
                    "prediction": None,
                }
            )

        for path, authority in file_authority.items():
            before = authority["stat"]
            if _stat_signature(path) != before:
                raise CollabEvidenceCaptureError("source/SRT stat changed during extraction")
            digest = str(hash_file(path))
            if digest != authority["sha256"] or _stat_signature(path) != before:
                raise CollabEvidenceCaptureError("source/SRT changed during final hashing")

        manifest = {
            "schema_version": SCHEMA_VERSION,
            "capture_id": capture_id,
            "date": date,
            "status": "UNLABELLED_EVIDENCE_ONLY",
            "labels_present": False,
            "predictions_present": False,
            "training": False,
            "upload": False,
            "authorized_for_training": False,
            "authorized_for_upload": False,
            "confirmed_collab_session": False,
            "training_ready": False,
            "trigger": {
                "reason_codes": list(trigger.reason_codes),
                "text_signal_classes": list(trigger.text_signal_classes),
                "routing_candidate_ids": list(trigger.routing_candidate_ids),
                "text_used_for_identity_or_routing": False,
            },
            "sampling": {
                "source": "sealed_session_bcut_srt_cues",
                "max_cues": MAX_CUES,
                "duration_strata": {"short": 30, "medium": 60, "long": 30},
                "minimum_cue_ms": 300,
                "maximum_cue_ms": 15_000,
                "song_intervals_filtered": True,
                "ground_truth_quota_claimed": False,
            },
            "cue_count": len(cue_documents),
            "cues": cue_documents,
        }
        manifest["manifest_integrity_sha256"] = _manifest_integrity_sha256(manifest)
        _atomic_write(staging / "manifest.json", _canonical_json_bytes(manifest))
        _ensure_canonical_directory(root, final_dir.parent)
        try:
            os.replace(staging, final_dir)
        except OSError:
            if final_dir.is_dir():
                shutil.rmtree(staging, ignore_errors=True)
                queue = rebuild_queue(root, quota=quota, alert_dir=alert_dir)
                return {
                    "status": "ALREADY_CAPTURED",
                    "capture_id": capture_id,
                    "manifest_path": str(final_dir / "manifest.json"),
                    "queue": queue,
                    "trigger": trigger,
                }
            else:
                raise
        published = True
        queue = rebuild_queue(root, quota=quota, alert_dir=alert_dir)
        return {
            "status": "CAPTURED",
            "capture_id": capture_id,
            "manifest_path": str(final_dir / "manifest.json"),
            "cue_count": len(cue_documents),
            "queue": queue,
            "trigger": trigger,
        }
    except Exception:
        if published:
            shutil.rmtree(final_dir, ignore_errors=True)
            try:
                rebuild_queue(root, quota=quota, alert_dir=alert_dir)
            except Exception:
                pass
        raise
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def validate_worker_request_document(document: object) -> dict[str, object]:
    """Validate a worker request without opening any referenced media path."""

    if not isinstance(document, dict) or set(document) != {
        "schema_version",
        "date",
        "candidates",
        "song_intervals",
        "trigger",
    }:
        raise CollabEvidenceCaptureError("worker request schema is invalid")
    if document.get("schema_version") != WORKER_REQUEST_SCHEMA_VERSION:
        raise CollabEvidenceCaptureError("worker request version is invalid")
    if not DATE_RE.fullmatch(str(document.get("date") or "")):
        raise CollabEvidenceCaptureError("worker request date is invalid")
    candidates = document.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise CollabEvidenceCaptureError("worker request candidates are missing")
    candidate_sources: set[str] = set()
    for candidate in candidates:
        if not isinstance(candidate, dict) or set(candidate) != {
            "segment_path",
            "bcut_srt_path",
        }:
            raise CollabEvidenceCaptureError("worker candidate schema is invalid")
        for field in ("segment_path", "bcut_srt_path"):
            value = candidate[field]
            if not isinstance(value, str) or not value or not Path(value).is_absolute():
                raise CollabEvidenceCaptureError(
                    f"worker candidate {field} must be a non-empty absolute path"
                )
        candidate_sources.add(candidate["segment_path"])
    intervals = document.get("song_intervals")
    if not isinstance(intervals, list):
        raise CollabEvidenceCaptureError("worker song intervals are invalid")
    for interval in intervals:
        if not isinstance(interval, dict) or set(interval) != {
            "segment_path",
            "start_ms",
            "end_ms",
        }:
            raise CollabEvidenceCaptureError("worker song interval schema is invalid")
        source = interval["segment_path"]
        start_ms = interval["start_ms"]
        end_ms = interval["end_ms"]
        if (
            not isinstance(source, str)
            or not source
            or not Path(source).is_absolute()
            or source not in candidate_sources
            or isinstance(start_ms, bool)
            or not isinstance(start_ms, int)
            or isinstance(end_ms, bool)
            or not isinstance(end_ms, int)
            or start_ms < 0
            or start_ms >= end_ms
        ):
            raise CollabEvidenceCaptureError("worker song interval authority is invalid")
    trigger = document.get("trigger")
    if not isinstance(trigger, dict) or set(trigger) != {
        "reason_codes",
        "text_signal_classes",
    }:
        raise CollabEvidenceCaptureError("worker trigger schema is invalid")
    if not isinstance(trigger["reason_codes"], list) or not all(
        isinstance(value, str) for value in trigger["reason_codes"]
    ):
        raise CollabEvidenceCaptureError("worker trigger reason codes are invalid")
    if not isinstance(trigger["text_signal_classes"], list) or not all(
        isinstance(value, str) for value in trigger["text_signal_classes"]
    ):
        raise CollabEvidenceCaptureError("worker trigger text classes are invalid")
    validate_opaque_trigger(
        TriggerEvaluation(
            triggered=True,
            reason_codes=tuple(trigger["reason_codes"]),
            text_signal_classes=tuple(trigger["text_signal_classes"]),
            routing_candidate_ids=(),
        )
    )
    return document


def _worker_request(path: Path) -> dict[str, object]:
    request_path = _strict_regular_path(path, field="worker request")
    try:
        document = json.loads(request_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CollabEvidenceCaptureError(f"worker request is invalid: {exc}") from exc
    return validate_worker_request_document(document)


def _serializable_result(result: Mapping[str, object]) -> dict[str, object]:
    output = {key: value for key, value in result.items() if key != "trigger"}
    trigger = result.get("trigger")
    if isinstance(trigger, TriggerEvaluation):
        output["trigger"] = {
            "triggered": trigger.triggered,
            "reason_codes": list(trigger.reason_codes),
            "text_signal_classes": list(trigger.text_signal_classes),
            "routing_candidate_ids": [],
        }
    return output


def run_worker_request(
    request_path: Path,
    *,
    base_dir: Path,
    alert_dir: Path,
) -> dict[str, object]:
    root = _safe_root(base_dir, field="capture root")
    requests_root = _ensure_canonical_directory(root, root / "requests")
    request_path = _strict_regular_path(request_path, field="worker request")
    if request_path.parent != requests_root:
        raise CollabEvidenceCaptureError("worker request escapes request root")
    document = _worker_request(request_path)
    trigger_document = document["trigger"]
    assert isinstance(trigger_document, dict)
    trigger = TriggerEvaluation(
        triggered=True,
        reason_codes=tuple(trigger_document["reason_codes"]),
        text_signal_classes=tuple(trigger_document["text_signal_classes"]),
        routing_candidate_ids=(),
    )
    lock_path = request_path.with_suffix(".lock")
    with lock_path.open("a+b") as lock_handle:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "WORKER_ALREADY_RUNNING"}

        def alarm_handler(_signum, _frame):
            raise TimeoutError("collab evidence worker wall deadline exceeded")

        previous_handler = signal.signal(signal.SIGALRM, alarm_handler)
        signal.alarm(WORKER_WALL_SECONDS)
        try:
            result = capture_session(
                str(document["date"]),
                document["candidates"],
                routing_claim=None,
                song_intervals=document["song_intervals"],
                base_dir=root,
                alert_dir=alert_dir,
                extractor=ffmpeg_extract_cue,
                duration_probe=ffprobe_duration_ms,
                trigger_override=trigger,
            )
            serializable = _serializable_result(result)
            serializable["worker_status"] = "COMPLETE"
        except Exception as exc:  # keep durable failure outside production state
            serializable = {
                "status": "CAPTURE_FAILED",
                "worker_status": "FAILED",
                "error": f"{type(exc).__name__}: {exc}"[:1000],
                "labels_present": False,
                "predictions_present": False,
                "training_ready": False,
                "upload_authorized": False,
            }
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, previous_handler)
        _atomic_write(
            request_path.with_suffix(".result.json"),
            _canonical_json_bytes(serializable),
        )
        return serializable


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--base-dir", type=Path, required=True)
    parser.add_argument("--alert-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    result = run_worker_request(
        args.request,
        base_dir=args.base_dir,
        alert_dir=args.alert_dir,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("worker_status") != "FAILED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
