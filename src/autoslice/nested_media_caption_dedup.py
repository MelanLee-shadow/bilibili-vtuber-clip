"""Derive a display subtitle track from verified watched-media caption observations.

Display duplication and acoustic speaker identity are different questions. This
consumer can suppress a complete, synchronized visual duplicate under the user's
watched-media policy without claiming that a voiceprint proved the host silent.
Partial matches, chat/UI text, short interjections and positive host/overlap
observations never authorize deletion. The original transcript is retained by
its caller for analysis and provenance; all normal release gates still apply.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path

from src.autoslice.subtitle_audio_correspondence import normalize_text, parse_timed_srt

SCHEMA = "watched-media-caption-display-evidence.v1"
POLICY = "synchronized-whole-cue-visual-dedup.v1"
AUDIT_SCHEMA = "watched-media-caption-display-dedup.v1"
MIN_DISTINCTIVE_CHARS = 4


class CaptionDedupError(ValueError):
    """Evidence is malformed or no longer describes the current source/text."""


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_bytes(path: Path, expected: object, code: str) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise CaptionDedupError(code + "_UNAVAILABLE")
    before = path.stat()
    data = path.read_bytes()
    after = path.stat()
    def identity(stat):
        return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
    if identity(before) != identity(after):
        raise CaptionDedupError(code + "_CHANGED_DURING_READ")
    if not isinstance(expected, str) or _sha(data) != expected.removeprefix("sha256:"):
        raise CaptionDedupError(code + "_HASH_MISMATCH")
    return data


def _integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _time(ms: int) -> str:
    h, r = divmod(ms, 3_600_000)
    m, r = divmod(r, 60_000)
    s, r = divmod(r, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{r:03d}"


def derive_display_track(
    *, transcript: str, observations: Sequence[Mapping[str, object]],
    source_start_ms: int = 0,
) -> tuple[str, dict[str, object]]:
    """Pure policy: exact normalized whole cue, at its own source time only.

    A caption at another time cannot delete repeated words said by the host.
    No similarity threshold, substring deletion or proportional retiming is used.
    """
    cues = parse_timed_srt(transcript, label="caption-dedup transcript")
    cue_by_id = {int(cue.index): cue for cue in cues}
    if len(cue_by_id) != len(cues) or not _integer(source_start_ms) or source_start_ms < 0:
        raise CaptionDedupError("CAPTION_DEDUP_CUE_OR_TIMELINE_INVALID")
    matched: dict[int, list[str]] = {}
    protected: set[int] = set()
    pending: set[int] = set()
    ignored: list[dict[str, object]] = []
    seen: set[str] = set()
    for observation in observations:
        if not isinstance(observation, Mapping):
            raise CaptionDedupError("CAPTION_DEDUP_OBSERVATION_INVALID")
        identity = observation.get("observation_id")
        index = observation.get("cue_index")
        frame_ms = observation.get("frame_ms")
        text = observation.get("visible_text")
        if (not isinstance(identity, str) or not identity or identity in seen
                or not _integer(index) or index not in cue_by_id
                or not _integer(frame_ms) or frame_ms < 0 or not isinstance(text, str)):
            raise CaptionDedupError("CAPTION_DEDUP_OBSERVATION_INVALID")
        seen.add(identity)
        cue = cue_by_id[index]
        overlap = observation.get("host_repeat_or_overlap")
        if overlap not in (None, True, False) or (overlap is not None and not isinstance(overlap, bool)):
            raise CaptionDedupError("CAPTION_DEDUP_OVERLAP_STATE_INVALID")
        if overlap is True:
            protected.add(index)
        # Reject self-generated subtitles even if their strings are identical.
        if (observation.get("pipeline_burned_subtitle") is not False
                or observation.get("role") != "WATCHED_MEDIA_CAPTION"):
            ignored.append({"observation_id": identity, "reason": "NOT_INDEPENDENT_SOURCE_CAPTION"})
            continue
        if not cue.start_ms + source_start_ms <= frame_ms < cue.end_ms + source_start_ms:
            ignored.append({"observation_id": identity, "reason": "CAPTION_OUTSIDE_CUE_TIME"})
            continue
        wanted, visible = normalize_text(cue.text), normalize_text(text)
        if len(wanted) < MIN_DISTINCTIVE_CHARS:
            pending.add(index)
            continue
        if wanted == visible:
            matched.setdefault(index, []).append(identity)
        elif visible and (visible in wanted or wanted in visible):
            pending.add(index)  # Mixed/partial cue: do not remove the host remainder.
    dropped = sorted(set(matched) - protected)
    if not dropped:
        output = transcript  # Preserve original raw bytes for a true no-op.
    else:
        kept = [cue for cue in cues if int(cue.index) not in dropped]
        output = "\n\n".join(
            f"{n}\n{_time(cue.start_ms)} --> {_time(cue.end_ms)}\n{cue.text}"
            for n, cue in enumerate(kept, 1)
        ) + ("\n" if kept else "")
    audit = {
        "schema_version": AUDIT_SCHEMA, "policy": POLICY,
        "status": "APPLIED_DISPLAY_DEDUP" if dropped else "UNCHANGED",
        "input_transcript_sha256": _sha(transcript.encode("utf-8")),
        "output_display_srt_sha256": _sha(output.encode("utf-8")),
        "dropped_input_cue_indexes": dropped,
        "retained_input_cue_indexes": [int(cue.index) for cue in cues if int(cue.index) not in dropped],
        "protected_host_or_overlap_cue_indexes": sorted(protected),
        "partial_or_short_match_cue_indexes": sorted(pending - set(dropped)),
        "matched_observations": {str(k): v for k, v in matched.items()},
        "ignored_observations": ignored,
        "speaker_identity_proven": False, "no_caption_means_host": False,
        "transcript_text_rewritten": False, "retained_cue_times_changed": False,
        "publication_authority": False,
    }
    return output, audit


def apply_bound_display_policy(
    *, config: object, candidate_id: str, source_media: Path,
    transcript: str, source_start_ms: int, source_end_ms: int,
    evidence_root: Path,
) -> tuple[str, dict[str, object] | None]:
    """Production materialization seam; verify current inputs before filtering.

    None keeps ordinary non-watched-media consumers unchanged. A declared mode
    with missing or stale evidence fails closed instead of silently burning the
    entire mixed transcript. The producer's current source interval is binding.
    """
    if config is None:
        return transcript, None
    if not isinstance(config, Mapping) or config.get("policy") != POLICY:
        raise CaptionDedupError("CAPTION_DEDUP_CONFIG_INVALID")
    value = config.get("evidence_path")
    if not isinstance(value, str) or not value:
        raise CaptionDedupError("CAPTION_DEDUP_EVIDENCE_REQUIRED")
    path = Path(value)
    if not path.is_absolute():
        path = evidence_root / path
    raw = _file_bytes(path, config.get("evidence_sha256"), "CAPTION_DEDUP_EVIDENCE")
    try:
        evidence = json.loads(raw)
    except (UnicodeError, ValueError) as exc:
        raise CaptionDedupError("CAPTION_DEDUP_EVIDENCE_INVALID") from exc
    if not isinstance(evidence, Mapping) or (
        evidence.get("schema_version") != SCHEMA
        or evidence.get("candidate_id") != candidate_id
        or evidence.get("source_role") != "WATCHED_MEDIA"
        or evidence.get("source_interval_ms") != [source_start_ms, source_end_ms]
        or evidence.get("input_transcript_sha256") != _sha(transcript.encode("utf-8"))
        or evidence.get("policy") != POLICY
        or not isinstance(evidence.get("observations"), list)
    ):
        raise CaptionDedupError("CAPTION_DEDUP_EVIDENCE_BINDING_MISMATCH")
    _file_bytes(source_media, evidence.get("source_media_sha256"), "CAPTION_DEDUP_SOURCE")
    for observation in evidence["observations"]:
        if not isinstance(observation, Mapping):
            raise CaptionDedupError("CAPTION_DEDUP_OBSERVATION_INVALID")
        frame_value = observation.get("frame_path")
        if not isinstance(frame_value, str) or not frame_value:
            raise CaptionDedupError("CAPTION_DEDUP_FRAME_REQUIRED")
        frame = Path(frame_value)
        if not frame.is_absolute():
            frame = path.parent / frame
        _file_bytes(frame, observation.get("frame_sha256"), "CAPTION_DEDUP_FRAME")
    output, audit = derive_display_track(
        transcript=transcript, observations=evidence["observations"], source_start_ms=source_start_ms,
    )
    audit.update(
        candidate_id=candidate_id, evidence_path=str(path), evidence_sha256=_sha(raw),
        source_media_sha256=evidence["source_media_sha256"],
        source_interval_ms=[source_start_ms, source_end_ms],
    )
    return output, audit
