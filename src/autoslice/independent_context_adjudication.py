"""Collect target-bound independent ASR candidates for the ordinary CPA judge."""
from __future__ import annotations

from .review_errors import ContractRejection

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any

from src.autoslice.acoustic_witness_adjudication import (
    build_witness_request,
    valid_witness_evidence,
)
from src.autoslice.acoustic_witness_protocol import (
    BLIND_PINYIN_PROTOCOL,
    CANDIDATE_BLIND_TRANSCRIPT_PROTOCOL,
)
from src.autoslice.jingting_chunker import parse_srt_cues
from src.autoslice.subtitle_audio_evidence import CONTRACT as SECONDARY_RESPONSE_CONTRACT, _digest


SECONDARY_AUDIO_EVIDENCE_SCHEMA = "secondary-audio-witness-evidence.v1"
SECONDARY_AUDIO_TIMELINE_SCHEMA = "subtitle-audio-timeline-binding.v1"
RANGE_ACOUSTIC_CONTEXT_SCHEMA = "range-acoustic-context-observation.v1"
RANGE_ACOUSTIC_CONTEXT_CARRYOVER_SCHEMA = "range-acoustic-context-carryover.v1"
SECONDARY_AUDIO_CONTEXT_SCHEMA = "secondary-audio-context-observation.v1"
_SECONDARY_PROVIDERS = frozenset({"mai", "moss"})
_SECONDARY_SEAMS = ("observe_secondary_audio_evidence", "observe_secondary_witnesses")
_RECEIPT_FIELDS = (
    "schema_version", "status", "authority", "mutation_authorized",
    "candidate_exposure", "witness_request_sha256", "witness_protocol",
    "provider", "model", "source_media_sha256", "source_crop_sha256",
    "secondary_audio_sha256", "provider_input_audio_sha256",
    "provider_response_sha256", "timeline_binding", "native_segments",
    "target_overlap_segments", "served_from_cache",
)
_FORBIDDEN_REQUEST_KEYS = frozenset({
    "candidate_id", "candidate_entities", "current_cue", "proposed_cue",
    "matched_audio_text", "context_before", "context_after", "suspect",
    "replacement", "exact_text", "canonical_entity", "rewritten_text",
})

_RANGE_ARTIFACT_PATHS = {
    "input": ("input_path", "request_path"),
    "result": ("result_path",),
    "verdict": ("verdict_path",),
    "manifest": ("manifest_path", "receipt_path"),
    "response": ("response_path", "provider_response_path"),
    "prompt": ("prompt_path", "provider_prompt_path"),
    "source_media": ("source_media_path",),
    "reviewed_srt": ("reviewed_srt_path", "srt_path"),
    "audio_clip": ("audio_clip_path", "audio_path", "input_audio_path"),
}

_RECUT_PROVENANCE_SCHEMA = "lidousha-speaker-recut-provenance.v1"
_PADDED_CONTEXT_PROVENANCE_NAME = "padded-context.provenance.json"


def _sha256(value: object) -> str | None:
    if isinstance(value, str):
        value = value.removeprefix("sha256:")
    if not isinstance(value, str) or len(value) != 64:
        return None
    return value if all(c in "0123456789abcdefABCDEF" for c in value) else None


def _file_sha256(path_value: object) -> str | None:
    if not isinstance(path_value, str) or not path_value:
        return None
    try:
        path = Path(path_value)
        if path.is_symlink() or not path.is_file():
            return None
        # One in-process digest per file placement (evidence_identity): cold
        # replay revalidates every observation's source binding, and the same
        # recording must not be re-read once per observation.
        from src.autoslice.evidence_identity import regular_file_sha256
        result = regular_file_sha256(path)
        if result is None:
            return None
        from src.autoslice.media_copy_provenance import record_source_read
        record_source_read(path, result)
        return result
    except (OSError, ValueError):
        return None


def _read_json(path_value: object) -> Mapping[str, Any] | None:
    if not isinstance(path_value, str) or not path_value:
        return None
    try:
        path = Path(path_value)
        if path.is_symlink() or not path.is_file():
            return None
        raw = path.read_bytes()
        value = json.loads(raw)
        from src.autoslice.media_copy_provenance import record_source_read
        record_source_read(path, hashlib.sha256(raw).hexdigest())
    except (OSError, UnicodeError, ValueError):
        return None
    return value if isinstance(value, Mapping) else None


def _same_digest(left: object, right: object) -> bool:
    left_digest, right_digest = _sha256(left), _sha256(right)
    return bool(
        left_digest is not None
        and right_digest is not None
        and left_digest.lower() == right_digest.lower()
    )


def _source_context_media_binding(
    current_request: Mapping[str, Any],
    media_path: str,
    media_sha: str,
    provenance_path: Path,
    provenance: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Validate the explicit source-context padding provenance.

    The ordinary producer writes ``<padded>.provenance.json`` with ``inputs``
    and per-piece sidecars.  The source-context stage has a different,
    explicit one-piece envelope: ``padded-context.provenance.json`` binds the
    materialized context bytes to the immutable recording and its absolute
    interval.  Keep this adapter narrow and fail closed; it must never turn a
    similarly named or incomplete JSON file into a recording identity.
    """
    if provenance.get("schema_version") != _RECUT_PROVENANCE_SCHEMA:
        return None
    declared_padded_path = provenance.get("padded_path")
    if not isinstance(declared_padded_path, str) or Path(declared_padded_path).is_symlink():
        return None
    if not _path_matches(declared_padded_path, media_path):
        return None
    actual_media_sha = _file_sha256(media_path)
    if actual_media_sha is None or not _same_digest(actual_media_sha, media_sha):
        return None
    if not _same_digest(provenance.get("padded_sha256"), actual_media_sha):
        return None

    source_path_value = provenance.get("source_path")
    if not isinstance(source_path_value, str) or Path(source_path_value).is_symlink():
        return None
    source_sha = _file_sha256(source_path_value)
    if source_sha is None or not _same_digest(provenance.get("source_sha256"), source_sha):
        return None
    clip = current_request.get("clip_context_binding")
    sources = clip.get("source_media_sha256s") if isinstance(clip, Mapping) else None
    if not isinstance(sources, list) or not any(
        _same_digest(value, source_sha) for value in sources
    ):
        return None

    start_ms = provenance.get("source_start_ms")
    end_ms = provenance.get("source_end_ms")
    padded_duration_ms = provenance.get("padded_duration_ms")
    if (
        type(start_ms) is not int
        or type(end_ms) is not int
        or not 0 <= start_ms < end_ms
        or type(padded_duration_ms) is not int
        or padded_duration_ms != end_ms - start_ms
    ):
        return None
    for key in ("parent_full_hash_recomputed", "parent_full_decode_repeated"):
        if type(provenance.get(key)) is not bool:
            return None

    rows = provenance.get("piece_provenance_rows")
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], Mapping):
        return None
    row = rows[0]
    piece_id = row.get("piece_id")
    if not isinstance(piece_id, str) or not piece_id:
        return None
    if (
        not isinstance(row.get("source_path"), str)
        or Path(row["source_path"]).is_symlink()
        or not _path_matches(row.get("source_path"), source_path_value)
        or not _same_digest(row.get("source_sha256"), source_sha)
        or row.get("source_start_ms") != start_ms
        or row.get("source_end_ms") != end_ms
        or row.get("start_ms") != start_ms
        or row.get("end_ms") != end_ms
        or row.get("duration_ms") != end_ms - start_ms
        or not isinstance(row.get("materialized_path"), str)
        or Path(row["materialized_path"]).is_symlink()
        or not _path_matches(row.get("materialized_path"), media_path)
        or not _same_digest(row.get("materialized_sha256"), actual_media_sha)
    ):
        return None

    provenance_sha = _file_sha256(str(provenance_path))
    if provenance_sha is None:
        return None
    return {
        "role": "PRODUCER_PADDED_MEDIA_FROM_RECORDING",
        "media_sha256": actual_media_sha,
        "recording_sha256": source_sha,
        "recording_start_ms": start_ms,
        "recording_end_ms": end_ms,
        "padded_provenance_path": str(provenance_path),
        "padded_provenance_sha256": provenance_sha,
        "piece_count": 1,
        "piece_id": piece_id,
        "piece_source_interval_ms": [start_ms, end_ms],
    }



def _final_recut_media_binding(
    current_request: Mapping[str, Any], media_path: str, media_sha: str,
    provenance_path: Path, provenance: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Replay the normal final-recut writer's source chain, not a padded envelope.

    The same schema name is used for two distinct producer representations.
    A final recut contains the original padded/piece documents and an extra
    crop edge. Validate those documents against their original sidecars and
    actual bytes before accepting the delivered media as range evidence.
    This is source identity only: no subtitle, audio receipt or clock is moved.
    """
    final = provenance.get("final_recut")
    padded = provenance.get("padded")
    pieces = provenance.get("source_piece")
    pieces = [pieces] if isinstance(pieces, Mapping) else pieces
    if (not isinstance(final, Mapping) or not isinstance(padded, Mapping)
            or not isinstance(pieces, list) or not pieces
            or not all(isinstance(row, Mapping) for row in pieces)
            or not _path_matches(final.get("output_path"), media_path)
            or not _same_digest(final.get("output_sha256"), media_sha)
            or not _same_digest(_file_sha256(media_path), media_sha)):
        return None
    parent_path = final.get("source_path")
    if (not isinstance(parent_path, str) or Path(parent_path).is_symlink()
            or _path_matches(parent_path, media_path)):
        return None
    parent_sha = _file_sha256(parent_path)
    parent_proof_path = Path(parent_path).with_suffix(".provenance.json")
    parent_proof = _read_json(str(parent_proof_path))
    # Normal final recuts are made from the producer's padded concatenation.
    # Do not recurse into an arbitrary final-recut chain or a cyclic sidecar.
    if (parent_sha is None or not _same_digest(final.get("source_sha256"), parent_sha)
            or not isinstance(parent_proof, Mapping) or parent_proof != padded
            or "final_recut" in parent_proof
            or not _path_matches(padded.get("output_path"), parent_path)
            or not _same_digest(padded.get("output_sha256"), parent_sha)
            or not isinstance(padded.get("inputs"), list)
            or len(padded["inputs"]) != len(pieces)):
        return None
    for item, piece in zip(padded["inputs"], pieces, strict=True):
        if not isinstance(item, Mapping) or not isinstance(item.get("path"), str):
            return None
        actual_piece = _read_json(str(Path(item["path"]).with_suffix(".provenance.json")))
        if actual_piece is None:
            return None
        # prepare_source_media annotates a reused piece in memory without
        # rewriting its immutable sidecar. This status is not source identity
        # or proof of current availability; the actual source bytes below are
        # still mandatory even for SOURCE_ROOT_UNAVAILABLE historical rows.
        metadata_key = "source_revalidation_status"
        known_statuses = (None, "HASH_BOUND_CACHE_REUSED_SOURCE_PATH_PRESENT",
                          "HASH_BOUND_CACHE_SOURCE_ROOT_UNAVAILABLE")
        if (piece.get(metadata_key) not in known_statuses
                or actual_piece.get(metadata_key) not in known_statuses
                or {k: v for k, v in piece.items() if k != metadata_key}
                != {k: v for k, v in actual_piece.items() if k != metadata_key}):
            return None
    # This internal projection validates a parent identity; it is never sent
    # to a provider or persisted as a rebound check/witness request. The outer
    # entry already checked the caller's explicit delivered-media declaration.
    parent_request = {**current_request, "source_media_sha256": parent_sha}
    parent = _range_media_source_binding(parent_request, parent_path, parent_sha)
    if parent is None or parent.get("role") not in {
        "PRODUCER_PADDED_MEDIA_FROM_RECORDING", "PRODUCER_PADDED_MEDIA_FROM_RECORDING_CHAIN",
    }:
        return None
    start, end = final.get("start_ms"), final.get("end_ms")
    if (type(start) is not int or type(end) is not int or not 0 <= start < end
            or any(type(row.get("start_ms")) is not int or type(row.get("end_ms")) is not int
                   for row in pieces)
            or end > sum(row["end_ms"] - row["start_ms"] for row in pieces)):
        return None
    absolute_start = final.get("absolute_source_start_ms")
    absolute_end = final.get("absolute_source_end_ms")
    if len(pieces) == 1:
        if (type(absolute_start) is not int or type(absolute_end) is not int
                or (absolute_start, absolute_end)
                != (pieces[0]["start_ms"] + start, pieces[0]["start_ms"] + end)):
            return None
    elif absolute_start is not None or absolute_end is not None:
        return None  # A multi-piece recut has no single absolute recording clock.
    proof_sha = _file_sha256(str(provenance_path))
    if proof_sha is None:
        return None
    return {"role": "PRODUCER_FINAL_RECUT_FROM_RECORDING", "media_sha256": media_sha,
            "parent_media_sha256": parent_sha, "final_recut_interval_ms": [start, end],
            "final_recut_provenance_path": str(provenance_path),
            "final_recut_provenance_sha256": proof_sha,
            "parent_media_source_binding": parent,
            "absolute_source_start_ms": absolute_start, "absolute_source_end_ms": absolute_end}


def _range_media_source_binding(
    current_request: Mapping[str, Any], media_path: str, media_sha: str,
) -> dict[str, Any] | None:
    """Keep recording identity separate from a producer's actual audio media.

    A saved range can use the padded recut while clip context keeps the original
    recording. Reuse the ordinary producer's existing provenance files and
    actual bytes; a declaration of two hashes alone cannot establish this link.
    """
    clip = current_request.get("clip_context_binding")
    sources = clip.get("source_media_sha256s") if isinstance(clip, Mapping) else None
    if not isinstance(sources, list) or not sources:
        return None
    requested_sha = current_request.get("source_media_sha256")
    if requested_sha is not None and not any(
        _same_digest(requested_sha, value) for value in [media_sha, *sources]
    ):
        return None
    padded_path = Path(media_path).with_suffix(".provenance.json")
    legacy_present = padded_path.exists() or padded_path.is_symlink()
    padded = _read_json(str(padded_path))
    from src.autoslice.media_copy_provenance import SCHEMA as COPY_SCHEMA, replay_copy_binding
    if isinstance(padded, Mapping) and padded.get("schema_version") == COPY_SCHEMA:
        return replay_copy_binding(current_request, media_path, media_sha, padded_path, padded)
    if any(_same_digest(value, media_sha) for value in sources):
        return {"role": "DIRECT_RECORDING_MEDIA", "media_sha256": media_sha}
    if isinstance(padded, Mapping) and padded.get("schema_version") == _RECUT_PROVENANCE_SCHEMA:
        if "final_recut" in padded:
            return _final_recut_media_binding(
                current_request, media_path, media_sha, padded_path, padded,
            )
        # A same-stem source-context sidecar is still an explicit source
        # envelope; validate it with the narrow adapter below.  If it is
        # malformed, return UNKNOWN rather than trying another sibling.
        return _source_context_media_binding(
            current_request, media_path, media_sha, padded_path, padded,
        )
    if not legacy_present:
        # The source-context stage records one immutable envelope beside its
        # ``source-context.mp4`` under this fixed name.  Do not scan the
        # directory or fall back when an existing same-stem sidecar is bad.
        explicit_path = Path(media_path).parent / _PADDED_CONTEXT_PROVENANCE_NAME
        explicit = _read_json(str(explicit_path))
        if isinstance(explicit, Mapping):
            return _source_context_media_binding(
                current_request, media_path, media_sha, explicit_path, explicit,
            )
        return None
    if (
        not isinstance(padded, Mapping)
        or not _path_matches(padded.get("output_path"), media_path)
        or not _same_digest(padded.get("output_sha256"), media_sha)
        or not isinstance(padded.get("inputs"), list)
        or not padded["inputs"]
    ):
        return None

    pieces: list[dict[str, Any]] = []
    seen_piece_paths: set[str] = set()
    seen_piece_inodes: set[tuple[int, int]] = set()
    previous_end_by_source: dict[str, int] = {}
    for input_row in padded["inputs"]:
        if not isinstance(input_row, Mapping) or not isinstance(input_row.get("path"), str):
            return None
        piece_path = Path(input_row["path"])
        # _file_sha256 rejects links, but check the path before resolving it so
        # a symlink cannot pass the provenance path comparison below.
        if piece_path.is_symlink() or not piece_path.is_file():
            return None
        piece_key = str(piece_path)
        try:
            piece_stat = piece_path.stat()
        except OSError:
            return None
        piece_identity = str(piece_path.resolve())
        inode_identity = (piece_stat.st_dev, piece_stat.st_ino)
        if piece_identity in seen_piece_paths or inode_identity in seen_piece_inodes:
            return None
        seen_piece_paths.add(piece_identity)
        seen_piece_inodes.add(inode_identity)
        piece_sha = _file_sha256(piece_key)
        if piece_sha is None or not _same_digest(input_row.get("sha256"), piece_sha):
            return None

        piece_provenance_path = piece_path.with_suffix(".provenance.json")
        piece = _read_json(str(piece_provenance_path))
        if (
            not isinstance(piece, Mapping)
            or not _path_matches(piece.get("output_path"), piece_key)
            or not _same_digest(piece.get("output_sha256"), piece_sha)
            or type(piece.get("start_ms")) is not int
            or type(piece.get("end_ms")) is not int
            or not 0 <= piece["start_ms"] < piece["end_ms"]
        ):
            return None

        source_path = piece.get("source_path")
        source_sha = _file_sha256(source_path)
        piece_provenance_sha = _file_sha256(str(piece_provenance_path))
        if (
            source_sha is None
            or piece_provenance_sha is None
            or not all(
                _same_digest(piece.get(key), source_sha)
                for key in ("source_sha256", "source_media_binding")
            )
            or not any(_same_digest(value, source_sha) for value in sources)
        ):
            return None
        previous_end = previous_end_by_source.get(source_sha)
        if previous_end is not None and piece["start_ms"] < previous_end:
            return None
        previous_end_by_source[source_sha] = piece["end_ms"]
        pieces.append(
            {
                "piece_path": piece_key,
                "piece_sha256": piece_sha,
                "piece_provenance_path": str(piece_provenance_path),
                "piece_provenance_sha256": piece_provenance_sha,
                "recording_sha256": source_sha,
                "recording_start_ms": piece["start_ms"],
                "recording_end_ms": piece["end_ms"],
            }
        )

    padded_sha = _file_sha256(str(padded_path))
    if padded_sha is None:
        return None
    if len(pieces) == 1:
        piece = pieces[0]
        # Preserve the original single-piece contract.  A single producer
        # piece must still be the actual padded bytes, as before this chain
        # support was added.
        if not _same_digest(piece["piece_sha256"], media_sha):
            return None
        return {
            "role": "PRODUCER_PADDED_MEDIA_FROM_RECORDING",
            "media_sha256": media_sha,
            "recording_sha256": piece["recording_sha256"],
            "recording_start_ms": piece["recording_start_ms"],
            "recording_end_ms": piece["recording_end_ms"],
            "padded_provenance_path": str(padded_path),
            "padded_provenance_sha256": padded_sha,
            "piece_provenance_path": piece["piece_provenance_path"],
            "piece_provenance_sha256": piece["piece_provenance_sha256"],
        }

    # A concat/recut can contain more than one producer piece.  Keep the
    # ordered intervals and each recording identity explicit; do not claim the
    # concatenated bytes are themselves a recording or silently bridge gaps.
    return {
        "role": "PRODUCER_PADDED_MEDIA_FROM_RECORDING_CHAIN",
        "media_sha256": media_sha,
        "piece_count": len(pieces),
        "pieces": pieces,
        "recording_sha256s": [piece["recording_sha256"] for piece in pieces],
        "padded_provenance_path": str(padded_path),
        "padded_provenance_sha256": padded_sha,
    }


def _range_bindings(observation: Mapping[str, Any]) -> Mapping[str, Any]:
    nested = observation.get("artifact_bindings")
    return nested if isinstance(nested, Mapping) else observation


def _range_path(
    bindings: Mapping[str, Any], role: str, manifest: Mapping[str, Any] | None = None
) -> str | None:
    for name in _RANGE_ARTIFACT_PATHS[role]:
        value = bindings.get(name)
        if isinstance(value, str) and value:
            return value
    if isinstance(manifest, Mapping):
        manifest_names = {
            "source_media": ("source_media",),
            "audio_clip": ("audio_clip",),
        }
        for name in manifest_names.get(role, ()):
            value = manifest.get(name)
            if isinstance(value, str) and value:
                return value
    return None


def _path_matches(left: object, right: object) -> bool:
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    try:
        return Path(left).resolve() == Path(right).resolve()
    except (OSError, RuntimeError, ValueError):
        return False


def _cue_grid(srt_text: str) -> list[tuple[int, int, bool]]:
    return [(cue.start_ms, cue.end_ms, bool(cue.text.strip())) for cue in parse_srt_cues(srt_text)]


def same_cue_grid(original_srt: str, current_srt: str) -> bool:
    """Whole-clip cue clock identity: same cues, same times, same empty slots."""
    return _cue_grid(original_srt) == _cue_grid(current_srt)


def same_physical_cue_grid(original_srt: str, original_offset_ms: object,
                           current_srt: str, current_offset_ms: object) -> bool:
    """The same cue grid on the source clock (local time + its own offset).

    A padded-clock SRT (offset 0) and the delivery SRT (offset 9800) of one
    clip are one physical grid; a local-millisecond comparison of the two is a
    clock-representation artefact, not an identity difference (41 共同核当前
    请求、物理源、源/交付时钟).
    """
    if type(original_offset_ms) is not int or type(current_offset_ms) is not int:
        return False
    def physical(srt, offset):
        return [(start + offset, end + offset, filled) for start, end, filled in _cue_grid(srt)]
    return physical(original_srt, original_offset_ms) == physical(current_srt, current_offset_ms)


_SRT_TIMING_LINE = re.compile(
    r"^(\s*)(\d{2,}):(\d{2}):(\d{2})[,.](\d{1,3})(\s*-->\s*)(\d{2,}):(\d{2}):(\d{2})[,.](\d{1,3})(.*)$")


def _shift_srt_clock(srt_text: str, delta_ms: int) -> str | None:
    """Re-time only the timing lines; every text byte stays the current text."""

    def stamp(ms: int) -> str:
        seconds, millis = divmod(ms, 1000)
        minutes, sec = divmod(seconds, 60)
        hours, minute = divmod(minutes, 60)
        return f"{hours:02d}:{minute:02d}:{sec:02d},{millis:03d}"

    lines = []
    for line in srt_text.split("\n"):
        match = _SRT_TIMING_LINE.match(line)
        if match is None:
            lines.append(line)
            continue
        lead, h1, m1, s1, f1, arrow, h2, m2, s2, f2, tail = match.groups()
        start = ((int(h1) * 60 + int(m1)) * 60 + int(s1)) * 1000 + int(f1.ljust(3, "0")) + delta_ms
        end = ((int(h2) * 60 + int(m2)) * 60 + int(s2)) * 1000 + int(f2.ljust(3, "0")) + delta_ms
        if start < 0 or end < 0:
            return None
        lines.append(f"{lead}{stamp(start)}{arrow}{stamp(end)}{tail}")
    return "\n".join(lines)


def _clock_rebased_view(srt_text, current_request, observation):
    """A validation view of the current request in the observation's own clock.

    The same physical cue grid under another local clock (padded 0 vs delivery
    9800) is one target (41 源/交付时钟); the integer original offset is read
    from the observation's own bound input document. With identical cue texts
    the view carries the SRT bytes its receipt bound. With a text-only
    revision it carries the CURRENT text re-timed into the observation clock,
    so the strict validator applies its ordinary text-revision rules
    (provider-input exposure, assistance re-check) to the current words. Every
    original artifact is re-checked in that view; the physical source windows
    it returns are clock-independent. Nothing is rewritten.
    """
    if not isinstance(current_request, Mapping) or not isinstance(observation, Mapping):
        return None
    bindings = _range_bindings(observation)
    input_doc = None
    for name in ("segmented_input_path", *(_RANGE_ARTIFACT_PATHS["input"])):
        if isinstance(bindings.get(name), str):
            input_doc = _read_json(bindings[name])
            if input_doc is not None:
                break
    original_offset = (input_doc or {}).get("source_media_timeline_offset_ms")
    current_offset = current_request.get("source_media_timeline_offset_ms")
    reviewed_path = _range_path(bindings, "reviewed_srt")
    if (type(original_offset) is not int or type(current_offset) is not int
            or original_offset == current_offset or not isinstance(reviewed_path, str)):
        return None
    try:
        path = Path(reviewed_path)
        if path.is_symlink() or not path.is_file():
            return None
        reviewed = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    def texts(srt):
        return [cue.text for cue in parse_srt_cues(srt)]

    if not same_physical_cue_grid(reviewed, original_offset, srt_text, current_offset):
        return None
    delta = current_offset - original_offset
    texts_identical = texts(reviewed) == texts(srt_text)
    view_srt = reviewed if texts_identical else _shift_srt_clock(srt_text, delta)
    if view_srt is None or not same_cue_grid(reviewed, view_srt):
        return None
    view = dict(current_request)
    view.pop("request_sha256", None)
    view.update(whole_clip_current_srt=view_srt, source_media_timeline_offset_ms=original_offset)
    for key in ("matched_start_ms", "matched_end_ms", "context_start_ms", "context_end_ms"):
        if type(view.get(key)) is int:
            view[key] = view[key] + delta
    view["request_sha256"] = hashlib.sha256(json.dumps(
        view, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return view_srt, view, {"schema_version": "range-observation-clock-rebase.v1",
                            "original_offset_ms": original_offset, "current_offset_ms": current_offset,
                            "physical_cue_grid_identical": True, "cue_texts_identical": texts_identical,
                            "authority": "EVIDENCE_ONLY", "mutation_authorized": False}


def window_cue_texts(srt_text: str, cue_indexes: object) -> list[str]:
    """Texts of window cues (1-based, non-empty numbering as in ``_range_cue_rows``)."""
    cues = [cue.text for cue in parse_srt_cues(srt_text) if cue.text.strip()]
    if not isinstance(cue_indexes, (list, tuple)):
        return []
    return [cues[index - 1] for index in cue_indexes
            if type(index) is int and 1 <= index <= len(cues)]


def provider_input_exposes_cue_text(
    provider_input: str, srt_texts: tuple[str, ...], cue_indexes: object,
) -> bool:
    """Actual exposure check on the bytes a provider received.

    A self-declared ``candidate_blind`` flag is not evidence. Any window cue
    text (of any supplied SRT version, >= 4 characters, the established
    segmented-range threshold) found in the provider input means the
    observation consumed subtitle text.
    """
    return any(len(text) >= 4 and text in provider_input
               for srt in srt_texts for text in window_cue_texts(srt, cue_indexes))


def subtitle_text_free_observation_survives(
    original_srt: str, current_srt: str, provider_input: str, cue_indexes: object,
) -> bool:
    """41:1489-1492: same source/target/clock keeps an existing blind hearing.

    An observation whose provider input contained no subtitle text consumed
    no subtitle text, so a later text-only SRT revision is not context drift.
    Only the whole-clip cue grid (count, times, empty slots) must be
    identical; source/window/clock drift is still rejected by the caller.
    An observation whose input exposed window subtitle text keeps the strict
    rejection, so its carryover stays the only way to reuse it.
    """
    if original_srt == current_srt:
        return True
    return (same_cue_grid(original_srt, current_srt)
            and not provider_input_exposes_cue_text(
                provider_input, (original_srt, current_srt), cue_indexes))


def text_revision_view(
    original_srt: str, current_srt: str, rows_for, *, current_rows,
) -> dict[str, Any]:
    """Current text for CPA readers after a text-only revision.

    ``cue_texts`` keeps the observation's original record; ``current_cue_texts``
    is derived from the current SRT at the identical grid. Old subtitle text is
    never presented as current text (41:454-458 carryover contract).
    """
    if original_srt == current_srt:
        return {}
    return {
        "cue_texts": rows_for(original_srt),
        "current_cue_texts": current_rows,
        "original_reviewed_srt_sha256": hashlib.sha256(original_srt.encode("utf-8")).hexdigest(),
        "current_srt_sha256": hashlib.sha256(current_srt.encode("utf-8")).hexdigest(),
        "text_revision": "TEXT_ONLY_REVISION_AT_IDENTICAL_CUE_GRID",
    }


def _reviewed_srt_text(path: object) -> str | None:
    try:
        target = Path(str(path))
        if target.is_symlink() or not target.is_file():
            return None
        # Exact bytes (no newline translation): hashes stay those of the file.
        return target.read_bytes().decode("utf-8")
    except (OSError, UnicodeError, ValueError):
        return None


def _range_cue_rows(
    srt_text: str, cue_indexes: list[int], start_ms: int, end_ms: int,
    *, allow_boundary_gaps: bool = False,
) -> list[dict[str, Any]] | None:
    cues = [cue for cue in parse_srt_cues(srt_text) if cue.text.strip()]
    if not cue_indexes or cue_indexes != list(range(cue_indexes[0], cue_indexes[-1] + 1)):
        return None
    if cue_indexes[0] < 1 or cue_indexes[-1] > len(cues):
        return None
    selected = [cues[index - 1] for index in cue_indexes]
    starts_in_first = selected[0].start_ms <= start_ms < selected[0].end_ms
    ends_in_last = selected[-1].start_ms < end_ms <= selected[-1].end_ms
    # A sealed native crop may start/end in the silence beside an outer cue.
    # Keep the original declaration and expose the missing coverage explicitly;
    # never pretend that the excluded cue was heard or change the request.
    if allow_boundary_gaps and len(selected) > 1:
        starts_in_first |= selected[0].end_ms <= start_ms <= selected[1].start_ms
        ends_in_last |= selected[-2].end_ms <= end_ms <= selected[-1].start_ms
    if allow_boundary_gaps:
        # A complete semantic crop can also include the text-free margin
        # *outside* its first/last declared cue. Do not require an artificial
        # cut at the subtitle boundary, or absorb an undeclared neighboring cue.
        previous_end = cues[cue_indexes[0] - 2].end_ms if cue_indexes[0] > 1 else min(0, start_ms)
        next_start = cues[cue_indexes[-1]].start_ms if cue_indexes[-1] < len(cues) else None
        starts_in_first |= previous_end <= start_ms <= selected[0].start_ms
        ends_in_last |= selected[-1].end_ms <= end_ms and (next_start is None or end_ms <= next_start)
    if not (starts_in_first and ends_in_last):
        return None
    if any(
        cue.start_ms < start_ms or cue.end_ms > end_ms
        for cue in selected[1:-1]
    ):
        return None
    rows = []
    for index, cue in zip(cue_indexes, selected, strict=True):
        covered_start = max(cue.start_ms, start_ms)
        covered_end = min(cue.end_ms, end_ms)
        no_coverage = covered_end <= covered_start
        if no_coverage and not (
            allow_boundary_gaps and index in (cue_indexes[0], cue_indexes[-1])
        ):
            return None
        rows.append({
            "cue_index": index,
            "start_ms": cue.start_ms,
            "end_ms": cue.end_ms,
            "text": cue.text,
            "text_sha256": hashlib.sha256(cue.text.encode("utf-8")).hexdigest(),
            "covered_start_ms": None if no_coverage else covered_start,
            "covered_end_ms": None if no_coverage else covered_end,
            "coverage": (
                "NONE" if no_coverage else "FULL"
                if covered_start == cue.start_ms and covered_end == cue.end_ms
                else "PARTIAL"
            ),
            **({"boundary_gap_ms": max(start_ms - cue.end_ms, cue.start_ms - end_ms, 0)}
               if no_coverage else {}),
        })
    return rows


def validate_range_acoustic_observation(srt_text, current_request, observation):
    validated, reason = _validate_range_acoustic_observation(srt_text, current_request, observation)
    if validated is None:
        # Same physical target heard under another local clock (padded vs
        # delivery): re-validate the original receipt in its own clock.
        rebased = _clock_rebased_view(srt_text, current_request, observation)
        if rebased is not None:
            view_srt, view_request, rebase = rebased
            again, _ = _validate_range_acoustic_observation(view_srt, view_request, observation)
            if again is not None:
                validated, reason = {**again, "clock_rebase": rebase}, None
    if validated is not None and isinstance(observation, Mapping):
        # Retain the acquisition plan as evidence metadata. Only the relisten
        # policy revalidates it; it grants no transcript/edit/independence right.
        if observation.get("targeted_relisten_input") is not None:
            validated = {**validated, "targeted_relisten_input": deepcopy(observation["targeted_relisten_input"])}
        if observation.get("served_from_cache") is True:
            validated = {**validated, "served_from_cache": True}
    return validated, reason


def _validate_range_acoustic_observation(
    srt_text: str,
    current_request: Mapping[str, Any],
    observation: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    """Validate a saved candidate-blind *range* witness as CPA context only.

    The range witness has its own candidate-free request hash and externally
    bound artifacts.  It is deliberately not converted into the single-cue
    witness used by the normal mutation path.  A successful return therefore
    carries the range request/verdict and cue map, while callers must continue
    to build the ordinary single-cue request and witness separately.
    """

    if not isinstance(current_request, Mapping) or not isinstance(observation, Mapping):
        return None, "RANGE_ACOUSTIC_OBSERVATION_INVALID"
    from src.autoslice.native_acoustic_feature_context import (
        TONE_CONTEXT_SCHEMA,
        validate_tone_context_observation,
    )

    if observation.get("schema_version") == TONE_CONTEXT_SCHEMA:
        # Tone/contour packets are a separate evidence domain.  The adapter
        # validates the original request, observation, execution/stdout and
        # media bytes, then returns PARTIAL_CONTEXT without manufacturing a
        # pinyin or transcript witness.  Keep this dispatch before the normal
        # witness-request construction so the old range contracts remain
        # unchanged.
        return validate_tone_context_observation(srt_text, current_request, observation)
    if observation.get("schema_version") == "mapped-source-audio-context.v1":
        from src.autoslice.mapped_source_context import validate_mapped_source_context

        return validate_mapped_source_context(srt_text, current_request, observation)
    if _range_bindings(observation).get("segmented_input_path"):
        return validate_segmented_range_context_observation(srt_text, current_request, observation)
    if _range_bindings(observation).get("native_envelope_path"):
        return validate_native_range_context_observation(
            srt_text, current_request, observation,
        )
    try:
        narrow_witness = build_witness_request(current_request)
    except (KeyError, TypeError, ValueError):
        return None, "RANGE_ACOUSTIC_CURRENT_TARGET_INVALID"
    if current_request.get("whole_clip_current_srt") != srt_text:
        return None, "RANGE_ACOUSTIC_SRT_REQUEST_DRIFT"
    bindings = _range_bindings(observation)
    # Load the manifest first because source/audio paths may be recorded there;
    # every path is still required to resolve to a real, non-symlink artifact.
    manifest_path = _range_path(bindings, "manifest")
    manifest = _read_json(manifest_path)
    if manifest is None:
        return None, "RANGE_ACOUSTIC_MANIFEST_MISSING"

    paths = {
        role: _range_path(bindings, role, manifest)
        for role in _RANGE_ARTIFACT_PATHS
    }
    # Request/result/verdict/manifest, source, reviewed SRT, audio, prompt and
    # raw response are all part of the saved provider receipt.  Missing any of
    # them makes the evidence UNKNOWN instead of a cache shortcut.
    if any(paths[role] is None for role in paths):
        return None, "RANGE_ACOUSTIC_ARTIFACT_BINDING_MISSING"
    docs = {
        role: _read_json(paths[role])
        for role in ("input", "result", "verdict", "manifest", "response")
    }
    if any(not isinstance(value, Mapping) for value in docs.values()):
        return None, "RANGE_ACOUSTIC_ARTIFACT_JSON_INVALID"
    input_doc = docs["input"]
    result_doc = docs["result"]
    verdict = docs["verdict"]
    manifest_doc = docs["manifest"]
    raw_response = docs["response"]
    assert isinstance(input_doc, Mapping)
    assert isinstance(result_doc, Mapping)
    assert isinstance(verdict, Mapping)
    assert isinstance(manifest_doc, Mapping)
    assert isinstance(raw_response, Mapping)

    actual_hashes = {
        role: _file_sha256(path)
        for role, path in paths.items()
    }
    if any(value is None for value in actual_hashes.values()):
        return None, "RANGE_ACOUSTIC_ARTIFACT_MISSING"
    if (
        not _path_matches(manifest_doc.get("source_media"), paths["source_media"])
        or not _path_matches(manifest_doc.get("audio_clip"), paths["audio_clip"])
        or not _path_matches(input_doc.get("source_media_path"), paths["source_media"])
        or not _path_matches(input_doc.get("reviewed_srt_path"), paths["reviewed_srt"])
        or (
            result_doc.get("verdict_path") is not None
            and not _path_matches(result_doc.get("verdict_path"), paths["verdict"])
        )
    ):
        return None, "RANGE_ACOUSTIC_ARTIFACT_PATH_DRIFT"
    source_sha = actual_hashes["source_media"]
    reviewed_srt_sha = actual_hashes["reviewed_srt"]
    assert source_sha is not None and reviewed_srt_sha is not None
    current_srt_sha = hashlib.sha256(srt_text.encode("utf-8")).hexdigest()
    reviewed_text = srt_text
    if reviewed_srt_sha != current_srt_sha:
        # The provider prompt is read and checked here; the saved
        # candidate-blind flag alone does not establish exposure.
        reviewed_text = _reviewed_srt_text(paths["reviewed_srt"])
        window_indexes = (input_doc.get("witness_request") or {}).get("cue_indexes") if isinstance(
            input_doc.get("witness_request"), Mapping) else None
        prompt_text = _reviewed_srt_text(paths["prompt"])
        if (reviewed_text is None or prompt_text is None
                or not subtitle_text_free_observation_survives(
                    reviewed_text, srt_text, prompt_text, window_indexes)):
            return None, "RANGE_ACOUSTIC_SRT_FILE_DRIFT"
    if not _same_digest(input_doc.get("reviewed_srt_sha256"), reviewed_srt_sha):
        return None, "RANGE_ACOUSTIC_SRT_BINDING_DRIFT"
    if not _same_digest(input_doc.get("source_media_sha256"), source_sha):
        return None, "RANGE_ACOUSTIC_SOURCE_FILE_DRIFT"
    for document in (result_doc, verdict, manifest_doc):
        declared = document.get("source_media_sha256")
        if declared is not None and not _same_digest(declared, source_sha):
            return None, "RANGE_ACOUSTIC_SOURCE_BINDING_DRIFT"

    broad_request = input_doc.get("witness_request")
    if not isinstance(broad_request, Mapping):
        return None, "RANGE_ACOUSTIC_REQUEST_MISSING"
    broad_request = dict(broad_request)
    try:
        rebuilt_witness_request = build_witness_request(broad_request)
    except (KeyError, TypeError, ValueError):
        return None, "RANGE_ACOUSTIC_REQUEST_INVALID"
    if rebuilt_witness_request != broad_request:
        return None, "RANGE_ACOUSTIC_REQUEST_NOT_CANONICAL"
    request_sha = rebuilt_witness_request["request_sha256"]
    for declared in (
        input_doc.get("witness_request_sha256"),
        result_doc.get("request_sha256"),
        verdict.get("request_sha256"),
        manifest_doc.get("request_sha256"),
    ):
        if declared is not None and not _same_digest(declared, request_sha):
            return None, "RANGE_ACOUSTIC_REQUEST_HASH_DRIFT"
    request_payload_sha = manifest_doc.get("request_payload_sha256")
    if request_payload_sha is not None and not _same_digest(
        request_payload_sha, _digest(broad_request)
    ):
        return None, "RANGE_ACOUSTIC_REQUEST_PAYLOAD_DRIFT"
    if any(key in broad_request for key in _FORBIDDEN_REQUEST_KEYS):
        return None, "RANGE_ACOUSTIC_REQUEST_EXPOSED_TEXT"
    if input_doc.get("candidate_blind") is not True or input_doc.get(
        "candidate_text_in_request"
    ) is not False:
        return None, "RANGE_ACOUSTIC_CANDIDATE_EXPOSURE"
    if result_doc.get("candidate_blind") is False:
        return None, "RANGE_ACOUSTIC_CANDIDATE_EXPOSURE"
    if any(key in verdict for key in _FORBIDDEN_REQUEST_KEYS):
        return None, "RANGE_ACOUSTIC_VERDICT_EXPOSED_TEXT"
    if any(key in raw_response for key in _FORBIDDEN_REQUEST_KEYS):
        return None, "RANGE_ACOUSTIC_RESPONSE_EXPOSED_TEXT"
    if (
        result_doc.get("srt_mutation") is not False
        or result_doc.get("publication") != "NOT_REQUESTED"
    ):
        return None, "RANGE_ACOUSTIC_RESULT_MUTATION_OR_PUBLICATION"
    for document in (input_doc, result_doc, verdict, manifest_doc):
        if "mutation_authorized" in document and document.get("mutation_authorized") is not False:
            return None, "RANGE_ACOUSTIC_MUTATION_AUTHORITY"

    cue_indexes = broad_request.get("cue_indexes")
    if isinstance(cue_indexes, tuple):
        cue_indexes = list(cue_indexes)
    if not isinstance(cue_indexes, list) or any(
        type(index) is not int for index in cue_indexes
    ):
        return None, "RANGE_ACOUSTIC_CUE_RANGE_INVALID"
    matched_start = broad_request.get("matched_start_ms")
    matched_end = broad_request.get("matched_end_ms")
    context_start = broad_request.get("context_start_ms")
    context_end = broad_request.get("context_end_ms")
    offset = broad_request.get("source_media_timeline_offset_ms")
    if (
        any(
            type(value) is not int
            for value in (matched_start, matched_end, context_start, context_end, offset)
        )
        or matched_start < 0
        or matched_end <= matched_start
        or context_start > matched_start
        or context_end < matched_end
        or offset < 0
    ):
        return None, "RANGE_ACOUSTIC_GEOMETRY_INVALID"
    delivery_window = input_doc.get("delivery_window")
    if (
        not isinstance(delivery_window, Mapping)
        or delivery_window.get("cue_indexes") != cue_indexes
        or delivery_window.get("start_ms") != matched_start
        or delivery_window.get("end_ms") != matched_end
        or input_doc.get("source_media_timeline_offset_ms") != offset
        or result_doc.get("source_media_timeline_offset_ms") != offset
    ):
        return None, "RANGE_ACOUSTIC_INPUT_GEOMETRY_DRIFT"
    cue_rows = _range_cue_rows(srt_text, cue_indexes, matched_start, matched_end)
    if cue_rows is None:
        return None, "RANGE_ACOUSTIC_CUE_BOUNDARY_DRIFT"
    current_indexes = current_request.get("cue_indexes")
    if not isinstance(current_indexes, list) or len(current_indexes) != 1:
        return None, "RANGE_ACOUSTIC_CURRENT_TARGET_INVALID"
    current_index = current_indexes[0]
    if type(current_index) is not int or current_index not in cue_indexes:
        return None, "RANGE_ACOUSTIC_CURRENT_OUTSIDE_RANGE"
    current_start = current_request.get("matched_start_ms")
    current_end = current_request.get("matched_end_ms")
    if type(current_start) is not int or type(current_end) is not int:
        return None, "RANGE_ACOUSTIC_CURRENT_TARGET_INVALID"
    if (
        current_request.get("source_media_timeline_offset_ms") != offset
        or current_start < matched_start
        or current_end > matched_end
    ):
        return None, "RANGE_ACOUSTIC_CURRENT_OUTSIDE_RANGE"
    current_binding = current_request.get("clip_context_binding")
    if not isinstance(current_binding, Mapping):
        return None, "RANGE_ACOUSTIC_CURRENT_SOURCE_IDENTITY_MISSING"
    current_candidate_id = current_binding.get("candidate_id")
    if not isinstance(current_candidate_id, str) or not current_candidate_id:
        return None, "RANGE_ACOUSTIC_CURRENT_CANDIDATE_IDENTITY_MISSING"
    if current_candidate_id != input_doc.get("candidate_id"):
        return None, "RANGE_ACOUSTIC_CURRENT_CANDIDATE_DRIFT"
    declared_sources = current_binding.get("source_media_sha256s")
    if not isinstance(declared_sources, list) or not declared_sources:
        return None, "RANGE_ACOUSTIC_CURRENT_SOURCE_IDENTITY_MISSING"
    media_source_binding = _range_media_source_binding(
        current_request, paths["source_media"], source_sha,
    )
    if media_source_binding is None:
        return None, "RANGE_ACOUSTIC_CURRENT_SOURCE_DRIFT"

    source_window = input_doc.get("source_window")
    result_window = result_doc.get("source_window")
    if not isinstance(source_window, Mapping) or not isinstance(result_window, Mapping):
        return None, "RANGE_ACOUSTIC_SOURCE_WINDOW_MISSING"
    target_start = matched_start + offset
    target_end = matched_end + offset
    if (
        source_window.get("target_start_ms") != target_start
        or source_window.get("target_end_ms") != target_end
        or result_window != source_window
    ):
        return None, "RANGE_ACOUSTIC_SOURCE_TARGET_DRIFT"
    crop_start = source_window.get("crop_start_ms")
    crop_end = source_window.get("crop_end_ms")
    if (
        type(crop_start) is not int
        or type(crop_end) is not int
        or crop_start > target_start
        or crop_end < target_end
    ):
        return None, "RANGE_ACOUSTIC_CROP_BINDING_DRIFT"
    timeline = verdict.get("timeline_binding")
    manifest_timeline = manifest_doc.get("timeline_binding")
    if not isinstance(timeline, Mapping) or not isinstance(manifest_timeline, Mapping):
        return None, "RANGE_ACOUSTIC_TIMELINE_BINDING_MISSING"
    if timeline != manifest_timeline:
        return None, "RANGE_ACOUSTIC_TIMELINE_BINDING_DRIFT"
    delivery = timeline.get("delivery_local")
    source_media = timeline.get("source_media")
    if (
        not isinstance(delivery, Mapping)
        or not isinstance(source_media, Mapping)
        or delivery.get("target_start_ms") != matched_start
        or delivery.get("target_end_ms") != matched_end
        or delivery.get("context_start_ms") != context_start
        or delivery.get("context_end_ms") != context_end
        or timeline.get("source_media_timeline_offset_ms") != offset
        or source_media.get("target_start_ms") != target_start
        or source_media.get("target_end_ms") != target_end
        or source_media.get("crop_start_ms") != crop_start
        or source_media.get("crop_end_ms") != crop_end
    ):
        return None, "RANGE_ACOUSTIC_TIMELINE_GEOMETRY_DRIFT"
    if (
        verdict.get("audio_start_ms") != crop_start
        or verdict.get("audio_end_ms") != crop_end
        or verdict.get("audio_clip_sha256") != manifest_doc.get("audio_clip_sha256")
        or verdict.get("audio_clip_sha256") != actual_hashes["audio_clip"]
    ):
        return None, "RANGE_ACOUSTIC_AUDIO_BINDING_DRIFT"

    # Prompt/response files are part of the provider receipt, not optional
    # labels.  Their hashes must agree with the manifest and the saved verdict.
    if (
        manifest_doc.get("prompt_sha256") != actual_hashes["prompt"]
        or manifest_doc.get("response_sha256") != actual_hashes["response"]
        or verdict.get("prompt_sha256") != actual_hashes["prompt"]
        or verdict.get("response_sha256") != actual_hashes["response"]
    ):
        return None, "RANGE_ACOUSTIC_PROVIDER_ARTIFACT_DRIFT"
    for key in (
        "schema_version",
        "status",
        "target_audible",
        "heard_pinyin",
        "uncertain_positions",
        "syllable_count",
        "confidence",
        "reason",
    ):
        if key in raw_response and raw_response.get(key) != verdict.get(key):
            # The Gemini receipt preserves the provider's raw syllable count
            # and records a separately derived count in the wrapper when the
            # provider explicitly marked the two counts as mismatched.  This
            # is a bounded receipt normalization, not permission to alter the
            # heard pinyin or any geometry/binding field.
            if not (
                key == "syllable_count"
                and verdict.get("self_count_mismatch") is True
                and type(raw_response.get(key)) is int
                and type(verdict.get(key)) is int
                and raw_response.get(key) > 0
                and verdict.get(key) > 0
            ):
                return None, "RANGE_ACOUSTIC_RESPONSE_CONTENT_DRIFT"
    if verdict.get("schema_version") != "subtitle-span-acoustic-witness.v1":
        return None, "RANGE_ACOUSTIC_VERDICT_SCHEMA_INVALID"
    if verdict.get("witness_protocol") != BLIND_PINYIN_PROTOCOL:
        return None, "RANGE_ACOUSTIC_VERDICT_PROTOCOL_INVALID"
    if verdict.get("status") not in {"OBSERVED", "UNCERTAIN"}:
        return None, "RANGE_ACOUSTIC_VERDICT_STATUS_INVALID"
    if verdict.get("status") == "OBSERVED" and not isinstance(verdict.get("target_audible"), bool):
        return None, "RANGE_ACOUSTIC_VERDICT_AUDIO_FLAG_INVALID"

    provider = result_doc.get("provider") or input_doc.get("provider")
    model = result_doc.get("model") or input_doc.get("model")
    if provider != input_doc.get("provider") or model != input_doc.get("model"):
        return None, "RANGE_ACOUSTIC_PROVIDER_IDENTITY_DRIFT"
    if verdict.get("provider") not in {None, provider} or verdict.get("model") not in {None, model}:
        return None, "RANGE_ACOUSTIC_PROVIDER_IDENTITY_DRIFT"

    result_verdict = result_doc.get("verdict")
    manifest_verdict = manifest_doc.get("verdict")
    if (
        isinstance(result_verdict, Mapping) and dict(result_verdict) != dict(verdict)
    ) or (
        isinstance(manifest_verdict, Mapping) and dict(manifest_verdict) != dict(verdict)
    ):
        return None, "RANGE_ACOUSTIC_VERDICT_FILE_DRIFT"
    for role in ("input", "result", "verdict", "manifest"):
        declared = bindings.get(f"{role}_sha256", observation.get(f"{role}_sha256"))
        if declared is not None and not _same_digest(declared, actual_hashes[role]):
            return None, "RANGE_ACOUSTIC_RECEIPT_FILE_DRIFT"

    return {
        "schema_version": RANGE_ACOUSTIC_CONTEXT_SCHEMA,
        "status": verdict.get("status"),
        "authority": "EVIDENCE_ONLY",
        "mutation_authorized": False,
        "candidate_blind": True,
        "candidate_id": input_doc.get("candidate_id"),
        "provider": provider,
        "model": model,
        "witness_request": broad_request,
        "witness_request_sha256": request_sha,
        "verdict": dict(verdict),
        "range": {
            "cue_indexes": list(cue_indexes),
            "matched_start_ms": matched_start,
            "matched_end_ms": matched_end,
            "context_start_ms": context_start,
            "context_end_ms": context_end,
            "source_media_timeline_offset_ms": offset,
        },
        "cue_texts": cue_rows,
        **text_revision_view(
            reviewed_text, srt_text,
            lambda srt: _range_cue_rows(srt, cue_indexes, matched_start, matched_end),
            current_rows=cue_rows),
        "source_window": {
            "target_start_ms": target_start,
            "target_end_ms": target_end,
            "crop_start_ms": crop_start,
            "crop_end_ms": crop_end,
        },
        "current_cue_index": current_index,
        "single_cue_witness_request_sha256": narrow_witness["request_sha256"],
        "media_source_binding": media_source_binding,
        "artifact_bindings": {
            **{f"{role}_path": str(path) for role, path in paths.items()},
            "input_sha256": actual_hashes["input"],
            "result_sha256": actual_hashes["result"],
            "verdict_file_sha256": actual_hashes["verdict"],
            "manifest_sha256": actual_hashes["manifest"],
            "source_media_sha256": source_sha,
            "reviewed_srt_sha256": reviewed_srt_sha,
            "audio_clip_sha256": actual_hashes["audio_clip"],
            "prompt_sha256": actual_hashes["prompt"],
            "response_sha256": actual_hashes["response"],
        },
        "evidence_scope": "RANGE_CONTEXT_ONLY",
        "transcript_scope": "RANGE_WITHOUT_SINGLE_CUE_TIMESTAMPS",
    }, None


def validate_native_range_context_observation(
    srt_text: str,
    current_request: Mapping[str, Any],
    observation: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    """Read a saved native ASR range as context, retaining its original request.

    Reuse the native writer's validators and provider receipt.  Whole-range
    text never becomes a narrow witness or acquires invented word timestamps.
    """
    from src.autoslice.local_asr_target_evidence import exact_target_evidence
    from src.autoslice.native_context_witness import (
        _native_transcript_witness,
        _prepare_request,
    )

    bindings = _range_bindings(observation)
    roles = (
        "input", "native_envelope", "provider_receipt", "audio_clip",
        "source_media", "reviewed_srt", "raw_response",
    )
    paths = {role: bindings.get(f"{role}_path") for role in roles}
    hashes = {role: _file_sha256(path) for role, path in paths.items()}
    if any(value is None for value in hashes.values()) or any(
        not _same_digest(bindings.get(f"{role}_sha256"), hashes[role])
        for role in roles
    ):
        return None, "NATIVE_RANGE_ARTIFACT_BINDING_INVALID"
    input_doc = _read_json(paths["input"])
    envelope = _read_json(paths["native_envelope"])
    receipt = _read_json(paths["provider_receipt"])
    if any(value is None for value in (input_doc, envelope, receipt)):
        return None, "NATIVE_RANGE_ARTIFACT_JSON_INVALID"
    assert input_doc is not None and envelope is not None and receipt is not None
    reviewed_text = srt_text
    srt_text_free_survives = hashes["reviewed_srt"] == hashlib.sha256(srt_text.encode()).hexdigest()
    if not srt_text_free_survives:
        # Native ASR receives audio plus this geometry-only request; check
        # those actual provider inputs, not the saved candidate flags.
        reviewed_text = _reviewed_srt_text(paths["reviewed_srt"])
        window = input_doc.get("witness_request")
        provider_input = json.dumps(
            [input_doc.get("witness_request"), envelope.get("request")], ensure_ascii=False, sort_keys=True)
        srt_text_free_survives = bool(
            reviewed_text is not None and isinstance(window, Mapping)
            and subtitle_text_free_observation_survives(
                reviewed_text, srt_text, provider_input, window.get("cue_indexes"))
        )
    if (
        not srt_text_free_survives
        or current_request.get("whole_clip_current_srt") != srt_text
        or input_doc.get("reviewed_srt_sha256") != hashes["reviewed_srt"]
        or input_doc.get("source_media_sha256") != hashes["source_media"]
        or not _path_matches(input_doc.get("source_media_path"), paths["source_media"])
        or not _path_matches(input_doc.get("reviewed_srt_path"), paths["reviewed_srt"])
    ):
        return None, "NATIVE_RANGE_SOURCE_OR_SRT_DRIFT"
    broad = input_doc.get("witness_request")
    try:
        prepared = _prepare_request(broad)
        narrow = build_witness_request(current_request)
    except (TypeError, ValueError, KeyError, AttributeError):
        return None, "NATIVE_RANGE_REQUEST_INVALID"
    if (
        envelope.get("schema_version") != "native-context-witness-envelope.v1"
        or envelope.get("request") != broad
        or envelope.get("request_sha256") != prepared.request_sha256
        or input_doc.get("witness_request_sha256") != prepared.request_sha256
        or input_doc.get("candidate_blind") is not True
        or input_doc.get("candidate_text_in_request") is not False
        or input_doc.get("srt_mutation") is not False
        or input_doc.get("publication") != "NOT_REQUESTED"
    ):
        return None, "NATIVE_RANGE_REQUEST_OR_AUTHORITY_DRIFT"
    indexes = broad["cue_indexes"]
    if any(type(index) is not int for index in indexes):
        return None, "NATIVE_RANGE_CUE_INDEXES_INVALID"
    rows = _range_cue_rows(
        srt_text, indexes, prepared.target_start_ms, prepared.target_end_ms,
        allow_boundary_gaps=True,
    )
    target_indexes = current_request.get("cue_indexes")
    clip_binding = current_request.get("clip_context_binding")
    if (
        rows is None or not isinstance(target_indexes, list) or len(target_indexes) != 1
        or target_indexes[0] not in indexes
        or (any(row["coverage"] == "NONE" for row in rows)
            and next(row for row in rows if row["cue_index"] == target_indexes[0])["coverage"] != "FULL")
        or narrow["source_media_timeline_offset_ms"] != prepared.source_offset_ms
        or narrow["matched_start_ms"] < prepared.target_start_ms
        or narrow["matched_end_ms"] > prepared.target_end_ms
        or prepared.request["context_start_ms"] > prepared.target_start_ms
        or prepared.request["context_end_ms"] < prepared.target_end_ms
        or not isinstance(clip_binding, Mapping)
        or not input_doc.get("candidate_id")
        or clip_binding.get("candidate_id") != input_doc["candidate_id"]
        or input_doc.get("source_media_timeline_offset_ms") != prepared.source_offset_ms
        or input_doc.get("delivery_window") != {
            "cue_indexes": indexes, "start_ms": prepared.target_start_ms,
            "end_ms": prepared.target_end_ms,
        }
        or envelope.get("source_media_sha256") != hashes["source_media"]
        or envelope.get("audio_start_ms") != prepared.physical_start_ms
        or envelope.get("audio_end_ms") != prepared.physical_end_ms
    ):
        return None, "NATIVE_RANGE_GEOMETRY_OR_CANDIDATE_DRIFT"
    media_source_binding = _range_media_source_binding(
        current_request, paths["source_media"], hashes["source_media"],
    )
    if media_source_binding is None:
        return None, "NATIVE_RANGE_CURRENT_SOURCE_DRIFT"
    metadata, binding = receipt.get("evidence"), receipt.get("binding")
    native = envelope.get("native_observation")
    if not all(isinstance(value, Mapping) for value in (metadata, binding, native)):
        return None, "NATIVE_RANGE_PROVIDER_RECEIPT_INVALID"
    # A rejected native timeline does not discard a bound whole-crop
    # transcript.  The normal exact-crop writer preserves MOSS text this way;
    # expose it only as range context, without inventing cue/word timestamps.
    unlocated_provider_text = (
        metadata.get("provider") == "moss"
        and metadata.get("status") == "TEXT_UNLOCATED"
        and metadata.get("evidence_scope") == "exact_cue"
        and metadata.get("native_segments") == []
        and metadata.get("one_track_srt_eligible") is False
        and isinstance(metadata.get("raw_response"), Mapping)
        and isinstance(metadata["raw_response"].get("text"), str)
        and bool(metadata["raw_response"]["text"].strip())
        and native.get("transcript_basis") == "provider_full_crop_text"
        and native.get("transcript_timeline_available") is False
    )
    if (
        receipt.get("evidence_sha256") != _digest(metadata)
        # response_sha256 binds the original HTTP bytes, whose whitespace and
        # key order need not match the separately saved parsed JSON artifact.
        or not isinstance(metadata.get("raw_response"), Mapping)
        or _read_json(paths["raw_response"]) != metadata.get("raw_response")
        or metadata.get("candidate_exposure") != "none"
        or (metadata.get("status") != "OK" and not unlocated_provider_text)
        or binding.get("candidate_exposure") != "none"
        or binding.get("contract") != "candidate-free-native-secondary-v1"
        or any(binding.get(key) != metadata.get(key) for key in (
            "provider", "model", "input_audio_sha256",
        ))
        or binding.get("duration_ms") != prepared.physical_end_ms - prepared.physical_start_ms
        or metadata.get("input_audio_sha256") != hashes["audio_clip"]
    ):
        return None, "NATIVE_RANGE_PROVIDER_ARTIFACT_DRIFT"
    try:
        rebuilt = exact_target_evidence(
            metadata, audio=Path(paths["audio_clip"]).read_bytes(),
            source_sha256=hashes["source_media"], start_ms=prepared.physical_start_ms,
            end_ms=prepared.physical_end_ms,
            prefer_provider_text=native.get("transcript_basis") == "provider_full_crop_text",
        )
        if any(native.get(key) != value for key, value in rebuilt.items() if key != "receipt_sha256"):
            return None, "NATIVE_RANGE_TRANSCRIPT_RECEIPT_DRIFT"
        witness = _native_transcript_witness(
            request=broad, prepared=prepared, native_observation=native,
            source_sha256=hashes["source_media"], provider=metadata["provider"],
        )
    except (OSError, KeyError, TypeError, ValueError):
        return None, "NATIVE_RANGE_PROVIDER_RECEIPT_INVALID"
    if witness.get("status") != "OBSERVED" or witness != envelope.get("witness"):
        return None, "NATIVE_RANGE_WITNESS_RECEIPT_DRIFT"
    word_overlap = None
    if metadata["provider"] == "mai" and any(
        row.get("words_available") is True for row in native.get("native_segments", [])
    ):
        from src.autoslice.mai_transcription import MaiTranscriptionError, _parse_evidence_response
        from src.autoslice.subtitle_audio_evidence import evidence_table

        # Reparse the bound provider response before exposing word timing.
        # A rehashed local metadata edit cannot invent provider timestamps.
        try:
            parsed, _, _ = _parse_evidence_response(
                dict(metadata["raw_response"]),
                duration_ms=prepared.physical_end_ms - prepared.physical_start_ms,
            )
        except (MaiTranscriptionError, TypeError, ValueError):
            return None, "NATIVE_RANGE_WORD_TIMELINE_INVALID"
        if parsed != native["native_segments"]:
            return None, "NATIVE_RANGE_WORD_TIMELINE_DRIFT"
        delivery_origin = prepared.physical_start_ms - prepared.source_offset_ms
        shifted = deepcopy(parsed)
        for segment in shifted:
            segment["start_ms"] += delivery_origin
            segment["end_ms"] += delivery_origin
            for word in segment.get("words") or []:
                word["start_ms"] += delivery_origin
                word["end_ms"] += delivery_origin
        word_overlap = {
            "authority": "EVIDENCE_ONLY", "mutation_authorized": False,
            "coordinate_system": "delivery_clip_ms",
            "physical_crop_ms": [prepared.physical_start_ms, prepared.physical_end_ms],
            "source_media_timeline_offset_ms": prepared.source_offset_ms,
            "crop_local_to_delivery_offset_ms": delivery_origin,
            "table": evidence_table(srt_text, {**native, "native_segments": shifted}),
        }
    return {
        "schema_version": RANGE_ACOUSTIC_CONTEXT_SCHEMA, "status": "OBSERVED",
        "authority": "EVIDENCE_ONLY", "mutation_authorized": False,
        "candidate_blind": True, "candidate_id": input_doc["candidate_id"],
        "provider": witness["provider"], "model": witness["model"],
        "witness_request": dict(broad), "witness_request_sha256": prepared.request_sha256,
        "verdict": witness, "cue_texts": rows,
        **text_revision_view(
            reviewed_text, srt_text,
            lambda srt: _range_cue_rows(srt, indexes, prepared.target_start_ms,
                                        prepared.target_end_ms, allow_boundary_gaps=True),
            current_rows=rows),
        "current_cue_index": target_indexes[0],
        "single_cue_witness_request_sha256": narrow["request_sha256"],
        "media_source_binding": media_source_binding,
        "artifact_bindings": {
            **hashes, **{f"{role}_path": str(path) for role, path in paths.items()},
            **{f"{role}_sha256": value for role, value in hashes.items()},
        }, "evidence_scope": "RANGE_CONTEXT_ONLY",
        "transcript_scope": ("RANGE_WITH_NATIVE_WORD_OVERLAP" if word_overlap
                             else "RANGE_WITHOUT_SINGLE_CUE_TIMESTAMPS"),
        **({"native_word_overlap_evidence": word_overlap} if word_overlap else {}),
    }, None


AUDIO_OBSERVATION_POLICY = (
    "Nonlexical observations are fallible context evidence, not transcript words, "
    "word timing, speaker identity or mutation authority. Event times are approximate "
    "locations within the bound audio crop, not speech-unit boundaries. An impact "
    "does not by itself prove a cup, drinking, an exact word, negation or a name. "
    "Preserve reliable lyrics, language switches and interrupted speech; do not "
    "complete missing words or treat quoted chat as the speaker's own position."
)


def validated_audio_observations(raw: Mapping[str, Any], duration_ms: int) -> list[dict[str, Any]] | None:
    """Read optional sound observations from the same immutable provider response.

    Missing means the older transcript-only protocol; malformed or out-of-crop
    observations are rejected, never promoted into text or exact word timing.
    """
    rows = raw.get("audio_observations", [])
    if not isinstance(rows, list):
        return None
    result = []
    for row in rows:
        if (not isinstance(row, Mapping)
            or row.get("kind") not in {"environment_sound", "nonsemantic_vocalization"}
            or row.get("status") not in {"OBSERVED", "UNCERTAIN"}
            or type(row.get("start_ms")) is not int or type(row.get("end_ms")) is not int
            or not 0 <= row["start_ms"] < row["end_ms"] <= duration_ms
            or not isinstance(row.get("description"), str) or not row["description"].strip()
            or set(row) != {"kind", "status", "start_ms", "end_ms", "description"}):
            return None
        result.append(dict(row))
    return result


def validate_range_response_protocol(
    raw: object,
    duration_ms: int,
    *,
    segments_local_ms: Mapping[str, Any] | None,
    target_local_ms: list[int] | tuple[int, int] | None,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]] | None, str | None]:
    """Validate the shared raw complete-range response protocol.

    The provider writer and the cold receipt reader must agree on the same
    response semantics.  In particular, ``UNCERTAIN`` is a completed hearing
    even when the provider could not recover any text or sound observation.
    ``OBSERVED`` still needs an audible target and at least one textual or
    sound observation.  Geometry is checked against the caller's immutable
    segment layout and local target, so this helper does not grant any
    authority to provider-declared timestamps.

    The final return item is a compact reason token.  Callers map it to their
    existing producer or consumer diagnostic namespace without duplicating the
    protocol predicates.
    """
    if not isinstance(raw, Mapping):
        return None, None, "MISSING"
    if raw.get("status") not in {"OBSERVED", "UNCERTAIN"}:
        return None, None, "STATUS_INVALID"
    if not isinstance(raw.get("target_audible"), bool):
        return None, None, "AUDIO_FLAG_INVALID"
    if not isinstance(raw.get("full_transcript"), str):
        return None, None, "TRANSCRIPT_INVALID"
    segments = raw.get("segments")
    if not isinstance(segments, list) or not segments:
        return None, None, "SEGMENTS_INVALID"
    audio_observations = validated_audio_observations(raw, duration_ms)
    if audio_observations is None:
        return None, None, "AUDIO_OBSERVATIONS_INVALID"

    if not isinstance(segments_local_ms, Mapping):
        return None, None, "GEOMETRY_INVALID"
    if not isinstance(target_local_ms, (list, tuple)) or len(target_local_ms) != 2:
        return None, None, "GEOMETRY_INVALID"
    if any(type(value) is not int for value in target_local_ms):
        return None, None, "GEOMETRY_INVALID"
    local_start, local_end = target_local_ms
    if local_start < 0 or local_end <= local_start or local_end > duration_ms:
        return None, None, "GEOMETRY_INVALID"
    if len(segments) != len(segments_local_ms):
        return None, None, "GEOMETRY_INVALID"

    cursor = local_start
    seen: set[str] = set()
    for row in segments:
        if (
            not isinstance(row, Mapping)
            or not isinstance(row.get("transcript"), str)
            or not isinstance(row.get("segment"), str)
            or row["segment"] in seen
            or row["segment"] not in segments_local_ms
            or type(row.get("start_ms")) is not int
            or type(row.get("end_ms")) is not int
            or row["start_ms"] != cursor
            or row["end_ms"] <= cursor
        ):
            return None, None, "GEOMETRY_INVALID"
        expected = segments_local_ms[row["segment"]]
        if (
            not isinstance(expected, (list, tuple))
            or len(expected) != 2
            or any(type(value) is not int for value in expected)
            or [row["start_ms"], row["end_ms"]] != list(expected)
        ):
            return None, None, "GEOMETRY_INVALID"
        seen.add(row["segment"])
        cursor = row["end_ms"]
    if cursor != local_end or len(seen) != len(segments_local_ms):
        return None, None, "GEOMETRY_INVALID"

    # Only a wholly empty UNCERTAIN reply is an attempt without evidence.
    # Partial/inconsistent text or a claimed transcript of inaudible audio
    # keeps the old producer rejection; it cannot become lexical support.
    segment_text = any(row['transcript'].strip() for row in segments)
    full_text = bool(raw['full_transcript'].strip())
    empty_uncertain = (raw['status'] == 'UNCERTAIN' and not audio_observations
        and not full_text and not segment_text)
    if not empty_uncertain and (
        raw.get('target_audible') is not True
        or (not audio_observations and (not full_text or not segment_text))
    ):
        return None, None, 'EVIDENCE_EMPTY'
    return dict(raw), audio_observations, None


def validate_segmented_range_context_observation(
    srt_text: str, current_request: Mapping[str, Any], observation: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    """Read saved blind segmented transcripts without fabricating a pinyin witness.

    The original transport input, prompt, stdout and parsed response remain
    immutable. Nominal segment boundaries are not provider word timestamps.
    This observation only supplies wider context to the existing CPA consumer.
    """
    bindings = _range_bindings(observation)
    roles = ("segmented_input", "execution", "raw_response", "provider_stdout",
             "prompt", "audio_clip", "source_media", "reviewed_srt")
    paths = {role: bindings.get(f"{role}_path") for role in roles}
    hashes = {role: _file_sha256(path) for role, path in paths.items()}
    if any(value is None for value in hashes.values()) or any(
        not _same_digest(bindings.get(f"{role}_sha256"), hashes[role]) for role in roles
    ):
        return None, "SEGMENTED_RANGE_ARTIFACT_BINDING_INVALID"
    input_doc = _read_json(paths["segmented_input"])
    execution = _read_json(paths["execution"])
    raw = _read_json(paths["raw_response"])
    # File-producing transports can print a Markdown summary to stdout.
    # Keep that original log byte-bound; parse the execution-bound response
    # file rather than requiring the terminal summary to duplicate its JSON.
    if any(doc is None for doc in (input_doc, execution, raw)):
        return None, "SEGMENTED_RANGE_ARTIFACT_JSON_INVALID"
    assert input_doc is not None and execution is not None and raw is not None
    assisted_mode = input_doc.get("schema_version") == "context-assisted-audio-request.v1"
    reviewed_text = srt_text
    if hashes["reviewed_srt"] != hashlib.sha256(srt_text.encode()).hexdigest():
        reviewed_text = _reviewed_srt_text(paths["reviewed_srt"])
        segmented_prompt = _reviewed_srt_text(paths["prompt"])
        # Context-assisted prompts render subtitle text by design; the
        # rendered sources are rechecked one by one against the current
        # catalogue by validate_assistance below. A blind segmented prompt
        # must actually contain no window subtitle text.
        survives = bool(reviewed_text is not None and segmented_prompt is not None and (
            same_cue_grid(reviewed_text, srt_text) if assisted_mode
            else subtitle_text_free_observation_survives(
                reviewed_text, srt_text, segmented_prompt, input_doc.get("cue_indexes"))
        ))
        if not survives:
            return None, "RANGE_ACOUSTIC_SRT_FILE_DRIFT"
    if current_request.get("whole_clip_current_srt") != srt_text:
        return None, "RANGE_ACOUSTIC_SRT_FILE_DRIFT"
    for role in ("source_media", "reviewed_srt", "audio_clip", "prompt"):
        record = input_doc.get(role)
        if (not isinstance(record, Mapping) or not _path_matches(record.get("path"), paths[role])
            or not _same_digest(record.get("sha256"), hashes[role])):
            return None, "SEGMENTED_RANGE_INPUT_ARTIFACT_DRIFT"
    for name, role in (("response", "raw_response"), ("stdout", "provider_stdout")):
        record = execution.get(name)
        if (not isinstance(record, Mapping) or not _path_matches(record.get("path"), paths[role])
            or not _same_digest(record.get("sha256"), hashes[role])):
            return None, "SEGMENTED_RANGE_TRANSPORT_ARTIFACT_DRIFT"
    if (execution.get("returncode") != 0 or execution.get("status") != "COMPLETED"
        or execution.get("failure_category") is not None or execution.get("adapter_exception") is not None
        or execution.get("response_parse_error") is not None
        or execution.get("provider") != input_doc.get("provider")
        or execution.get("model") != input_doc.get("model")
        or not isinstance(input_doc.get("provider"), str) or not input_doc["provider"]
        or not isinstance(input_doc.get("model"), str) or not input_doc["model"]
        or input_doc.get("candidate_blind") is not (not assisted_mode)
        or input_doc.get("candidate_text_in_provider_prompt") is not assisted_mode
        or any(doc.get("srt_mutation") is not False or doc.get("publication") != "NOT_REQUESTED"
               for doc in (input_doc, execution))):
        return None, "SEGMENTED_RANGE_TRANSPORT_OR_AUTHORITY_INVALID"
    clip = current_request.get("clip_context_binding")
    media_binding = _range_media_source_binding(current_request, paths["source_media"], hashes["source_media"])
    if (not isinstance(clip, Mapping) or not input_doc.get("candidate_id")
        or clip.get("candidate_id") != input_doc["candidate_id"] or media_binding is None):
        return None, "SEGMENTED_RANGE_CURRENT_SOURCE_OR_CANDIDATE_DRIFT"
    target = input_doc.get("delivery_target_ms")
    crop = input_doc.get("source_crop_ms")
    indexes = input_doc.get("cue_indexes")
    offset = input_doc.get("source_media_timeline_offset_ms")
    if (not isinstance(target, list) or len(target) != 2 or not isinstance(crop, list) or len(crop) != 2
        or any(type(x) is not int for x in [*target, *crop, offset])
        or not isinstance(indexes, list) or any(type(x) is not int for x in indexes)
        or offset < 0 or target[0] + offset < 0 or target[1] <= target[0]
        or crop[0] < 0 or crop[0] > target[0] + offset or crop[1] < target[1] + offset
        or input_doc.get("source_target_ms") != [x + offset for x in target]
        or input_doc.get("delivery_crop_ms") != [x - offset for x in crop]):
        return None, "SEGMENTED_RANGE_GEOMETRY_INVALID"
    rows = _range_cue_rows(srt_text, indexes, *target, allow_boundary_gaps=True)
    current_indexes = current_request.get("cue_indexes")
    if (rows is None or not isinstance(current_indexes, list) or len(current_indexes) != 1
        or current_indexes[0] not in indexes
        or next(row for row in rows if row["cue_index"] == current_indexes[0])["coverage"] != "FULL"
        or current_request.get("source_media_timeline_offset_ms") != offset
        or type(current_request.get("matched_start_ms")) is not int
        or type(current_request.get("matched_end_ms")) is not int
        or not target[0] <= current_request["matched_start_ms"] < current_request["matched_end_ms"] <= target[1]):
        return None, "SEGMENTED_RANGE_CURRENT_TARGET_DRIFT"
    declared = input_doc.get("segments_local_ms")
    sound_raw = raw
    if assisted_mode:
        from src.autoslice import context_assisted_audio as assisted
        sound_raw, _notes = assisted.split_text_notes(raw)
    local_target = [x + offset - crop[0] for x in target]
    if input_doc.get("target_local_ms") != local_target:
        return None, "SEGMENTED_RANGE_GEOMETRY_INVALID"
    _, audio_observations, protocol_error = validate_range_response_protocol(
        sound_raw,
        crop[1] - crop[0],
        segments_local_ms=declared,
        target_local_ms=local_target,
    )
    if protocol_error is not None:
        response_reasons = {
            "MISSING": "SEGMENTED_RANGE_RESPONSE_INVALID",
            "STATUS_INVALID": "SEGMENTED_RANGE_RESPONSE_INVALID",
            "AUDIO_FLAG_INVALID": "SEGMENTED_RANGE_RESPONSE_INVALID",
            "TRANSCRIPT_INVALID": "SEGMENTED_RANGE_RESPONSE_INVALID",
            "SEGMENTS_INVALID": "SEGMENTED_RANGE_RESPONSE_INVALID",
            "AUDIO_OBSERVATIONS_INVALID": "SEGMENTED_RANGE_AUDIO_OBSERVATIONS_INVALID",
            "GEOMETRY_INVALID": "SEGMENTED_RANGE_RESPONSE_GEOMETRY_DRIFT",
            "EVIDENCE_EMPTY": "SEGMENTED_RANGE_RESPONSE_EVIDENCE_EMPTY",
        }
        return None, response_reasons.get(protocol_error, "SEGMENTED_RANGE_RESPONSE_INVALID")
    assert audio_observations is not None
    segments = raw["segments"]
    try:
        prompt = Path(paths["prompt"]).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None, "SEGMENTED_RANGE_PROMPT_UNREADABLE"
    assisted_fields = {}
    if assisted_mode:
        from src.autoslice import context_assisted_audio as assisted
        try:
            assistance = assisted.validate_assistance(
                input_doc.get("assistance"), current_request, original_srt=reviewed_text)
            if (input_doc.get("observation_class") != assisted.OBSERVATION_CLASS
                    or input_doc.get("independent_witness") is not False
                    or input_doc.get("range_request", {}).get("assistance") != assistance
                    or not assisted.prompt_matches(input_doc["range_request"], prompt)):
                raise ContractRejection("ASSISTED_AUDIO_PROMPT_BINDING_DRIFT")
            # Validate the saved blind prerequisites against the same current
            # source and clock. A flag, old blind cache key or ID is not proof.
            proof_check = {**current_request, "prior_acoustic_observations": input_doc.get("prior_blind_narrow", [])}
            ids = assisted.validate_prior_blind(proof_check, input_doc.get("prior_blind_ranges", []))
            if not set(assistance["prior_blind_observation_ids"]).issubset(ids):
                raise ContractRejection("ASSISTED_AUDIO_BLIND_PREREQUISITE_DRIFT")
            partition = assisted.validate_response(raw, assistance)
            assisted_fields = {
                "schema_version": assisted.OBSERVATION_SCHEMA,
                "observation_class": assisted.OBSERVATION_CLASS, "candidate_blind": False,
                "independent_witness": False, "assistance": assistance, "audio_partition": partition}
        except (ValueError, TypeError, KeyError, OSError) as exc:
            return None, str(exc)
    elif provider_input_exposes_cue_text(prompt, (srt_text, reviewed_text), indexes):
        return None, "SEGMENTED_RANGE_CANDIDATE_EXPOSURE"
    return {
        "schema_version": RANGE_ACOUSTIC_CONTEXT_SCHEMA, "status": raw["status"],
        "authority": "EVIDENCE_ONLY", "mutation_authorized": False, "candidate_blind": True,
        **assisted_fields,
        "candidate_id": input_doc["candidate_id"], "provider": input_doc["provider"], "model": input_doc["model"],
        "range": {"cue_indexes": indexes, "matched_start_ms": target[0], "matched_end_ms": target[1]},
        "source_window": {"crop_start_ms": crop[0], "crop_end_ms": crop[1]}, "cue_texts": rows,
        **text_revision_view(
            reviewed_text, srt_text,
            lambda srt: _range_cue_rows(srt, indexes, *target, allow_boundary_gaps=True),
            current_rows=rows),
        "current_cue_index": current_indexes[0], "media_source_binding": media_binding,
        "verdict": {"exact_transcript": raw["full_transcript"], "original_provider_response": dict(raw),
                    **({"audio_observations": audio_observations,
                        "audio_observation_policy": AUDIO_OBSERVATION_POLICY,
                        "audio_observation_time_domain": "audio_crop_local_ms"}
                       if audio_observations else {})},
        "artifact_bindings": dict(bindings), "original_input_sha256": hashes["segmented_input"],
        "evidence_scope": "RANGE_CONTEXT_ONLY", "transcript_scope": "RANGE_WITHOUT_SINGLE_CUE_TIMESTAMPS",
        "context_only": True,
    }, None


def adapt_range_acoustic_context_observation(
    current_srt: str,
    current_request: Mapping[str, Any],
    observation: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    """Carry a valid old range witness across a text-only SRT revision.

    The strict validator above remains the authority for the original receipt.
    This adapter only changes the validation view's full-SRT text to the bytes
    actually bound by the old receipt, then checks that the current SRT keeps
    every cue geometry in the old range.  The returned row is context evidence
    and cannot authorize a current single-cue witness or subtitle mutation.
    """

    if not isinstance(current_srt, str) or not isinstance(current_request, Mapping):
        return None, "RANGE_ACOUSTIC_CONTEXT_INPUT_INVALID"
    if not isinstance(observation, Mapping):
        return None, "RANGE_ACOUSTIC_CONTEXT_OBSERVATION_INVALID"
    if current_request.get("whole_clip_current_srt") != current_srt:
        return None, "RANGE_ACOUSTIC_CONTEXT_SRT_REQUEST_DRIFT"
    bindings = _range_bindings(observation)
    # A validated range row carries the immutable current-target anchor while
    # a raw artifact observation carries the file paths.  Keep both views in
    # the local validation copy when they arrive together; never mutate the
    # caller's observation or its cached receipt.
    validation_observation: Mapping[str, Any] = observation
    validation_bindings = bindings
    if bindings is not observation:
        merged_bindings = dict(bindings)
        for role_names in _RANGE_ARTIFACT_PATHS.values():
            for name in role_names:
                if name not in merged_bindings and observation.get(name):
                    merged_bindings[name] = observation[name]
        validation_observation = dict(observation)
        validation_observation["artifact_bindings"] = merged_bindings
        validation_bindings = merged_bindings
    reviewed_srt_path = _range_path(validation_bindings, "reviewed_srt")
    if not isinstance(reviewed_srt_path, str) or not reviewed_srt_path:
        return None, "RANGE_ACOUSTIC_CONTEXT_REVIEWED_SRT_MISSING"
    path = Path(reviewed_srt_path)
    try:
        if path.is_symlink() or not path.is_file():
            return None, "RANGE_ACOUSTIC_CONTEXT_REVIEWED_SRT_MISSING"
        original_srt = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None, "RANGE_ACOUSTIC_CONTEXT_REVIEWED_SRT_UNREADABLE"
    original_srt_sha = _file_sha256(reviewed_srt_path)
    current_srt_sha = hashlib.sha256(current_srt.encode("utf-8")).hexdigest()
    if original_srt_sha is None:
        return None, "RANGE_ACOUSTIC_CONTEXT_REVIEWED_SRT_MISSING"
    if original_srt_sha != current_srt_sha:
        input_path = validation_bindings.get("segmented_input_path")
        input_doc = _read_json(input_path) if isinstance(input_path, str) else None
        if ((input_doc or {}).get("schema_version") == "context-assisted-audio-request.v1"
                or observation.get("observation_class") == "CONTEXT_ASSISTED_AUDIO"):
            return None, "ASSISTED_AUDIO_CURRENT_CONTEXT_DRIFT"
    if original_srt_sha == current_srt_sha:
        return None, "RANGE_ACOUSTIC_CONTEXT_SRT_UNCHANGED"

    # Keep every current target/source/candidate field while replacing only
    # the full-SRT authority view required by the strict old-receipt checker.
    original_validation_request = dict(current_request)
    original_validation_request["whole_clip_current_srt"] = original_srt
    validated, reason = validate_range_acoustic_observation(
        original_srt, original_validation_request, validation_observation
    )
    if validated is None:
        return None, f"RANGE_ACOUSTIC_CONTEXT_ORIGINAL_{reason or 'INVALID'}"

    range_binding = validated.get("range")
    if not isinstance(range_binding, Mapping):
        # Native validation returns the immutable witness request and cue rows,
        # while the older file-based validator also returns a normalized
        # ``range`` mapping.  Derive that mapping only from a native result
        # that has already passed the strict receipt/source/SRT/geometry checks.
        # Keep the derivation local; never rewrite the saved envelope or receipt.
        if not bindings.get("native_envelope_path"):
            return None, "RANGE_ACOUSTIC_CONTEXT_RANGE_BINDING_MISSING"
        witness_request = validated.get("witness_request")
        if not isinstance(witness_request, Mapping):
            return None, "RANGE_ACOUSTIC_CONTEXT_RANGE_BINDING_MISSING"
        derived_range = {
            key: witness_request.get(key)
            for key in (
                "cue_indexes", "matched_start_ms", "matched_end_ms",
                "context_start_ms", "context_end_ms",
                "source_media_timeline_offset_ms",
            )
        }
        cue_indexes = derived_range["cue_indexes"]
        if (
            not isinstance(cue_indexes, list)
            or not cue_indexes
            or any(type(index) is not int for index in cue_indexes)
            or cue_indexes != list(range(cue_indexes[0], cue_indexes[-1] + 1))
            or any(
                type(derived_range[key]) is not int
                for key in derived_range
                if key != "cue_indexes"
            )
        ):
            return None, "RANGE_ACOUSTIC_CONTEXT_RANGE_BINDING_INVALID"
        validated = dict(validated)
        validated["range"] = derived_range
        range_binding = derived_range
    cue_indexes = range_binding.get("cue_indexes")
    matched_start = range_binding.get("matched_start_ms")
    matched_end = range_binding.get("matched_end_ms")
    if (
        not isinstance(cue_indexes, list)
        or not all(type(index) is int for index in cue_indexes)
        or type(matched_start) is not int
        or type(matched_end) is not int
    ):
        return None, "RANGE_ACOUSTIC_CONTEXT_RANGE_BINDING_INVALID"
    original_rows = validated.get("cue_texts")
    current_rows = _range_cue_rows(
        current_srt, cue_indexes, matched_start, matched_end,
        allow_boundary_gaps=bool(bindings.get("native_envelope_path")),
    )
    if not isinstance(original_rows, list) or current_rows is None:
        return None, "RANGE_ACOUSTIC_CONTEXT_RANGE_GEOMETRY_DRIFT"
    geometry_keys = (
        "cue_index", "start_ms", "end_ms", "covered_start_ms",
        "covered_end_ms", "coverage",
    )
    if [
        {key: row.get(key) for key in geometry_keys}
        for row in original_rows
        if isinstance(row, Mapping)
    ] != [
        {key: row.get(key) for key in geometry_keys}
        for row in current_rows
    ]:
        return None, "RANGE_ACOUSTIC_CONTEXT_RANGE_GEOMETRY_DRIFT"

    prior_target = observation.get("current_target")
    if not isinstance(prior_target, Mapping):
        prior_target = observation.get("target")
    prior_index = observation.get("current_cue_index")
    prior_start = observation.get("current_matched_start_ms")
    prior_end = observation.get("current_matched_end_ms")
    if isinstance(prior_target, Mapping):
        prior_index = prior_target.get("cue_index", prior_index)
        prior_start = prior_target.get("start_ms", prior_start)
        prior_end = prior_target.get("end_ms", prior_end)
        if any(
            type(value) is not int
            for value in (prior_index, prior_start, prior_end)
        ):
            return None, "RANGE_ACOUSTIC_CONTEXT_CURRENT_TARGET_INVALID"
    if (
        type(prior_index) is int
        and type(prior_start) is int
        and type(prior_end) is int
    ):
        if (
            current_request.get("cue_indexes") != [prior_index]
            or current_request.get("matched_start_ms") != prior_start
            or current_request.get("matched_end_ms") != prior_end
        ):
            return None, "RANGE_ACOUSTIC_CONTEXT_CURRENT_TARGET_DRIFT"

    current_narrow = build_witness_request(current_request)
    original_range = deepcopy(validated)
    carryover = deepcopy(validated)
    carryover.update(
        {
            "schema_version": RANGE_ACOUSTIC_CONTEXT_CARRYOVER_SCHEMA,
            "status": "CONTEXT_ONLY",
            "authority": "EVIDENCE_ONLY",
            "mutation_authorized": False,
            "context_only": True,
            "original_status": validated.get("status"),
            "original_reviewed_srt_sha256": original_srt_sha,
            "current_srt_sha256": current_srt_sha,
            "original_witness_request": deepcopy(
                validated.get("witness_request")
            ),
            "original_witness_request_sha256": validated.get(
                "witness_request_sha256"
            ),
            "current_narrow_witness_request": current_narrow,
            "current_narrow_witness_request_sha256": current_narrow[
                "request_sha256"
            ],
            "original_range_observation": original_range,
            # Original cue_texts belong to the immutable audio receipt.  CPA
            # also needs the current text at the same verified geometry so a
            # repaired neighbour is not mistaken for the old candidate again.
            "current_cue_texts": current_rows,
            "evidence_scope": "RANGE_CONTEXT_ONLY_SRT_CARRYOVER",
        }
    )
    return carryover, None


def _path(binding: Mapping[str, Any], *names: str) -> object:
    for name in names:
        if binding.get(name):
            return binding[name]
    return None


def _valid_interval(value: object) -> tuple[int, int] | None:
    if not isinstance(value, Mapping):
        return None
    start, end = value.get("start_ms"), value.get("end_ms")
    if any((isinstance(item, bool) or not isinstance(item, int)) for item in (start, end)):
        return None
    if start < 0 or end <= start:
        return None
    return start, end


def _shift_segments(value: object, offset_ms: int) -> list[dict[str, Any]] | None:
    if not isinstance(value, list):
        return None
    shifted: list[dict[str, Any]] = []
    for raw in value:
        if not isinstance(raw, Mapping):
            return None
        interval = _valid_interval(raw)
        if interval is None:
            return None
        row = dict(raw)
        row["start_ms"], row["end_ms"] = interval[0] + offset_ms, interval[1] + offset_ms
        if "words" in raw:
            words = raw.get("words")
            if not isinstance(words, list):
                return None
            shifted_words: list[dict[str, Any]] = []
            for word in words:
                if not isinstance(word, Mapping) or _valid_interval(word) is None:
                    return None
                item = dict(word)
                item["start_ms"] += offset_ms
                item["end_ms"] += offset_ms
                shifted_words.append(item)
            row["words"] = shifted_words
        shifted.append(row)
    return shifted


def _payload(
    value: object, artifact_bindings: Mapping[str, Any] | None
) -> tuple[list[object], Mapping[str, Any] | None]:
    if isinstance(value, Mapping):
        nested = value.get("artifact_bindings")
        bindings = nested if isinstance(nested, Mapping) else artifact_bindings
        providers = value.get("providers")
        if isinstance(providers, Mapping):
            return list(providers.values()), bindings
        if isinstance(providers, (list, tuple)):
            return list(providers), bindings
        return [value], bindings
    if isinstance(value, (list, tuple)):
        return list(value), artifact_bindings
    return [], artifact_bindings


def _load_artifact_context(
    target: Mapping[str, Any], bindings: Mapping[str, Any] | None
) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(bindings, Mapping):
        return None, "SECONDARY_AUDIO_ARTIFACT_BINDING_MISSING"
    request_path = bindings.get("request_path")
    source_path = bindings.get("source_media_path")
    request_doc = _read_json(request_path)
    source_sha = _file_sha256(source_path)
    if request_doc is None or source_sha is None:
        return None, "SECONDARY_AUDIO_ARTIFACT_MISSING"
    request_value = request_doc.get("witness_request", request_doc)
    if not isinstance(request_value, Mapping) or dict(request_value) != dict(target):
        return None, "SECONDARY_AUDIO_REQUEST_FILE_DRIFT"
    if any(key in request_value for key in _FORBIDDEN_REQUEST_KEYS):
        return None, "SECONDARY_AUDIO_REQUEST_EXPOSED_TEXT"
    declared_source = request_doc.get("source_media_sha256")
    if _sha256(declared_source) is None:
        return None, "SECONDARY_AUDIO_SOURCE_BINDING_MISSING"
    if _sha256(declared_source) != source_sha:
        return None, "SECONDARY_AUDIO_SOURCE_FILE_DRIFT"
    expected_target = request_doc.get("expected_source_target_ms")
    physical_start = target["matched_start_ms"] + target["source_media_timeline_offset_ms"]
    physical_end = target["matched_end_ms"] + target["source_media_timeline_offset_ms"]
    if expected_target is None:
        return None, "SECONDARY_AUDIO_SOURCE_TARGET_BINDING_MISSING"
    if expected_target != [physical_start, physical_end]:
        return None, "SECONDARY_AUDIO_SOURCE_TARGET_FILE_DRIFT"
    providers = bindings.get("providers")
    if not isinstance(providers, Mapping):
        return None, "SECONDARY_AUDIO_PROVIDER_BINDING_MISSING"
    return {
        "request_path": str(request_path),
        "source_media_path": str(source_path),
        "source_media_sha256": source_sha,
        "providers": providers,
    }, None


def _verify_provider_artifacts(
    row: Mapping[str, Any],
    provider_binding: object,
    context: Mapping[str, Any],
    *,
    target: Mapping[str, Any],
    source_start: int,
    source_end: int,
) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(provider_binding, Mapping):
        return None, "SECONDARY_AUDIO_PROVIDER_BINDING_MISSING"
    provider = row.get("provider")
    receipt_path = _path(provider_binding, "receipt_path")
    receipt_sha = _sha256(_path(provider_binding, "receipt_sha256"))
    receipt = _read_json(receipt_path)
    actual_receipt_sha = _file_sha256(receipt_path)
    if receipt is None or actual_receipt_sha is None or receipt_sha != actual_receipt_sha:
        return None, "SECONDARY_AUDIO_RECEIPT_ARTIFACT_DRIFT"
    if row.get("receipt_sha256") is not None and _sha256(row.get("receipt_sha256")) != receipt_sha:
        return None, "SECONDARY_AUDIO_RECEIPT_SHA_DRIFT"
    for key in _RECEIPT_FIELDS:
        if key not in row or key not in receipt or row[key] != receipt[key]:
            return None, "SECONDARY_AUDIO_RECEIPT_CONTENT_DRIFT"
    if receipt.get("source_media_sha256") != context["source_media_sha256"]:
        return None, "SECONDARY_AUDIO_SOURCE_FILE_DRIFT"
    if receipt.get("witness_request_sha256") != target["request_sha256"]:
        return None, "SECONDARY_AUDIO_REQUEST_DRIFT"

    crop_path = _path(provider_binding, "crop_path", "source_crop_path")
    audio_path = _path(provider_binding, "audio_path", "secondary_audio_path", "input_audio_path")
    response_path = _path(provider_binding, "response_path", "provider_response_path")
    response_file_sha = _sha256(_path(provider_binding, "response_file_sha256"))
    crop_sha = _file_sha256(crop_path)
    audio_sha = _file_sha256(audio_path)
    response_file_actual = _file_sha256(response_path)
    if (
        crop_sha is None
        or audio_sha is None
        or response_file_actual is None
        or response_file_sha is None
        or response_file_actual != response_file_sha
        or crop_sha != receipt.get("source_crop_sha256")
        or audio_sha != receipt.get("secondary_audio_sha256")
        or audio_sha != receipt.get("provider_input_audio_sha256")
    ):
        return None, "SECONDARY_AUDIO_MEDIA_ARTIFACT_DRIFT"
    response_doc = _read_json(response_path)
    response_evidence = response_doc.get("evidence") if response_doc else None
    if not isinstance(response_evidence, Mapping):
        return None, "SECONDARY_AUDIO_RESPONSE_ARTIFACT_MISSING"
    if response_doc.get("evidence_sha256") != _digest(response_evidence):
        return None, "SECONDARY_AUDIO_RESPONSE_FILE_DRIFT"
    response_binding = response_doc.get("binding")
    if not isinstance(response_binding, Mapping):
        return None, "SECONDARY_AUDIO_RESPONSE_BINDING_MISSING"
    if (
        response_binding.get("contract") != SECONDARY_RESPONSE_CONTRACT
        or response_binding.get("provider") != provider
        or response_binding.get("model") != row.get("model")
        or response_binding.get("input_audio_sha256") != audio_sha
        or response_evidence.get("contract") != SECONDARY_RESPONSE_CONTRACT
        or response_evidence.get("candidate_exposure") != "none"
        or response_evidence.get("provider") != provider
        or response_evidence.get("model") != row.get("model")
        or response_evidence.get("input_audio_sha256") != audio_sha
        or response_evidence.get("response_sha256") != receipt.get("provider_response_sha256")
        or _sha256(response_evidence.get("response_sha256")) is None
        or not isinstance(response_evidence.get("raw_response"), Mapping)
    ):
        return None, "SECONDARY_AUDIO_RESPONSE_CONTENT_DRIFT"
    timeline_binding = receipt.get("timeline_binding")
    source_media_binding = (
        timeline_binding.get("source_media")
        if isinstance(timeline_binding, Mapping)
        else None
    )
    if not isinstance(source_media_binding, Mapping):
        return None, "SECONDARY_AUDIO_TIMELINE_ARTIFACT_DRIFT"
    crop_start = source_media_binding.get("crop_start_ms")
    if isinstance(crop_start, bool) or not isinstance(crop_start, int):
        return None, "SECONDARY_AUDIO_TIMELINE_ARTIFACT_DRIFT"
    shifted_native = _shift_segments(response_evidence.get("native_segments"), crop_start)
    if shifted_native != receipt.get("native_segments"):
        return None, "SECONDARY_AUDIO_NATIVE_RESPONSE_DRIFT"
    if shifted_native is None:
        return None, "SECONDARY_AUDIO_RESPONSE_SEGMENTS_MISSING"
    expected_overlap = [
        segment
        for segment in shifted_native
        if max(segment["start_ms"], source_start) < min(segment["end_ms"], source_end)
    ]
    if expected_overlap != receipt.get("target_overlap_segments"):
        return None, "SECONDARY_AUDIO_OVERLAP_RESPONSE_DRIFT"
    return {
        "receipt": receipt,
        "receipt_path": str(receipt_path),
        "receipt_sha256": receipt_sha,
        "request_path": context["request_path"],
        "source_media_path": context["source_media_path"],
        "source_media_sha256": context["source_media_sha256"],
        "crop_path": str(crop_path),
        "audio_path": str(audio_path),
        "response_path": str(response_path),
        "response_file_sha256": response_file_sha,
    }, None


def _secondary_candidate(
    target: Mapping[str, Any],
    evidence: Mapping[str, Any],
    artifact: Mapping[str, Any],
    *,
    source_start: int,
    source_end: int,
) -> tuple[dict[str, Any] | None, str | None]:
    if evidence.get("schema_version") != SECONDARY_AUDIO_EVIDENCE_SCHEMA:
        return None, "SECONDARY_AUDIO_SCHEMA"
    if (
        evidence.get("status") != "OBSERVED"
        or evidence.get("authority") != "EVIDENCE_ONLY"
        or evidence.get("mutation_authorized") is not False
        or evidence.get("candidate_exposure") != "none"
        or evidence.get("witness_protocol") != BLIND_PINYIN_PROTOCOL
    ):
        return None, "SECONDARY_AUDIO_AUTHORITY_OR_PROTOCOL"
    if evidence.get("witness_request_sha256") != target["request_sha256"]:
        return None, "SECONDARY_AUDIO_REQUEST_DRIFT"
    provider, model = evidence.get("provider"), evidence.get("model")
    if provider not in _SECONDARY_PROVIDERS or not isinstance(model, str) or not model.strip():
        return None, "SECONDARY_AUDIO_PROVIDER"
    if evidence.get("source_media_sha256") != artifact.get("source_media_sha256"):
        return None, "SECONDARY_AUDIO_SOURCE_FILE_DRIFT"
    binding = evidence.get("timeline_binding")
    delivery = binding.get("delivery_local") if isinstance(binding, Mapping) else None
    source_binding = binding.get("source_media") if isinstance(binding, Mapping) else None
    if not isinstance(binding, Mapping) or binding.get("schema_version") != SECONDARY_AUDIO_TIMELINE_SCHEMA:
        return None, "SECONDARY_AUDIO_TIMELINE_SCHEMA"
    if (
        binding.get("source_media_timeline_offset_ms") != target["source_media_timeline_offset_ms"]
        or not isinstance(delivery, Mapping)
        or delivery.get("target_start_ms") != target["matched_start_ms"]
        or delivery.get("target_end_ms") != target["matched_end_ms"]
        or delivery.get("context_start_ms") != target["context_start_ms"]
        or delivery.get("context_end_ms") != target["context_end_ms"]
        or not isinstance(source_binding, Mapping)
        or source_binding.get("target_start_ms") != source_start
        or source_binding.get("target_end_ms") != source_end
    ):
        return None, "SECONDARY_AUDIO_TIMELINE_DRIFT"
    crop_start, crop_end = source_binding.get("crop_start_ms"), source_binding.get("crop_end_ms")
    if (
        isinstance(crop_start, bool)
        or isinstance(crop_end, bool)
        or not isinstance(crop_start, int)
        or not isinstance(crop_end, int)
        or not crop_start <= source_start <= source_end <= crop_end
    ):
        return None, "SECONDARY_AUDIO_TARGET_OUTSIDE_CROP"
    segments = evidence.get("target_overlap_segments")
    native_segments = evidence.get("native_segments")
    if not isinstance(segments, list) or not isinstance(native_segments, list) or not segments:
        return None, "SECONDARY_AUDIO_SEGMENTS_MISSING"
    native_keys = {_digest(item) for item in native_segments if isinstance(item, Mapping)}
    if any(not isinstance(item, Mapping) or _digest(item) not in native_keys for item in segments):
        return None, "SECONDARY_AUDIO_SEGMENT_RESPONSE_DRIFT"
    text_parts: list[str] = []
    transcript_scope = "TARGET_OVERLAP_SEGMENT"
    for segment in segments:
        interval = _valid_interval(segment)
        if interval is None or max(interval[0], source_start) >= min(interval[1], source_end):
            return None, "SECONDARY_AUDIO_NO_TARGET_OVERLAP"
        words = segment.get("words")
        if isinstance(words, list) and words:
            selected: list[str] = []
            for word in words:
                word_interval = _valid_interval(word)
                if word_interval is None or not isinstance(word.get("text"), str):
                    return None, "SECONDARY_AUDIO_WORD_INVALID"
                if max(word_interval[0], source_start) < min(word_interval[1], source_end):
                    selected.append(word["text"])
            if not selected:
                return None, "SECONDARY_AUDIO_NO_TARGET_WORDS"
            text_parts.append("".join(selected))
            transcript_scope = "TARGET_WORD_OVERLAP"
        elif isinstance(segment.get("text"), str) and segment["text"].strip():
            text_parts.append(segment["text"].strip())
        else:
            return None, "SECONDARY_AUDIO_SEGMENT_TEXT"
    transcript = "".join(text_parts).strip()
    if not transcript or "\n" in transcript or "\r" in transcript:
        return None, "SECONDARY_AUDIO_TRANSCRIPT"
    canonical = {
        "schema_version": "subtitle-span-acoustic-witness.v1",
        "witness_protocol": CANDIDATE_BLIND_TRANSCRIPT_PROTOCOL,
        "request_sha256": target["request_sha256"],
        "status": "OBSERVED",
        "target_audible": True,
        "exact_transcript": transcript,
        "candidate_exposure": "none",
        "authority": "EVIDENCE_ONLY",
        "mutation_authorized": False,
        "provider": provider,
        "model": model,
        "source_media_sha256": artifact["source_media_sha256"],
        "audio_start_ms": source_start,
        "audio_end_ms": source_end,
        "source_crop_sha256": artifact["receipt"]["source_crop_sha256"],
        "secondary_audio_sha256": artifact["receipt"]["secondary_audio_sha256"],
        "provider_input_audio_sha256": artifact["receipt"]["provider_input_audio_sha256"],
        "provider_response_sha256": artifact["receipt"]["provider_response_sha256"],
        "receipt_sha256": artifact["receipt_sha256"],
        "evidence_scope": "SECONDARY_AUDIO_TARGET_OVERLAP",
        "transcript_scope": transcript_scope,
        "audio_geometry_scope": "TARGET_WITHIN_PADDED_CROP",
        "crop_is_exact_target": False,
        "target_start_ms": source_start,
        "target_end_ms": source_end,
        "crop_start_ms": crop_start,
        "crop_end_ms": crop_end,
        "secondary_audio_evidence": dict(evidence),
        "artifact_provenance": dict(artifact),
    }
    return canonical, None


def _adapt_secondary_rows(
    target: Mapping[str, Any],
    rows: list[object],
    bindings: Mapping[str, Any] | None,
    *,
    source_start: int,
    source_end: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    context, context_error = _load_artifact_context(target, bindings)
    if context is None:
        return [], [{"status": "REJECTED", "reason_code": context_error}]
    provider_bindings = context["providers"]
    accepted: list[dict[str, Any]] = []
    rejections: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            rejections.append({"status": "REJECTED", "index": index, "reason_code": "NON_OBJECT_RESULT"})
            continue
        if row.get("status") != "OBSERVED":
            rejections.append({"status": "SKIPPED", "index": index, "reason_code": "SECONDARY_AUDIO_NOT_OBSERVED"})
            continue
        provider = row.get("provider")
        provider_binding = provider_bindings.get(provider) if isinstance(provider_bindings, Mapping) else None
        artifact, reason = _verify_provider_artifacts(
            row,
            provider_binding,
            context,
            target=target,
            source_start=source_start,
            source_end=source_end,
        )
        if artifact is None:
            rejections.append({"status": "REJECTED", "index": index, "provider": provider, "reason_code": reason})
            continue
        candidate, reason = _secondary_candidate(
            target,
            row,
            artifact,
            source_start=source_start,
            source_end=source_end,
        )
        if candidate is None:
            rejections.append({"status": "REJECTED", "index": index, "provider": provider, "reason_code": reason})
            continue
        identity = (candidate["provider"], candidate["model"])
        if identity in seen:
            rejections.append({"status": "REJECTED", "index": index, "provider": provider, "reason_code": "SECONDARY_AUDIO_DUPLICATE_ROUTE"})
            continue
        seen.add(identity)
        accepted.append(candidate)
    if any(item.get("status") == "REJECTED" for item in rejections):
        return [], rejections
    return accepted, rejections


def adapt_secondary_audio_evidence(
    request: Mapping[str, Any],
    evidence: object,
    *,
    artifact_bindings: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Adapt only externally hash-bound padded receipts into CPA candidates."""

    target = build_witness_request(request)
    rows, bindings = _payload(evidence, artifact_bindings)
    source_start = target["matched_start_ms"] + target["source_media_timeline_offset_ms"]
    source_end = target["matched_end_ms"] + target["source_media_timeline_offset_ms"]
    accepted, _ = _adapt_secondary_rows(
        target, rows, bindings, source_start=source_start, source_end=source_end
    )
    return accepted


def adapt_secondary_audio_context_observation(
    current_request: Mapping[str, Any],
    observation: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Expose a validated secondary receipt as prior context only.

    A persisted secondary receipt has its own candidate-blind witness request.
    A later finding can have the same physical cue/window while a different
    ``evidence_id`` (and therefore a different witness request hash).  The
    receipt must be verified against its own immutable request first; it is
    then carried into the later request as a prior observation.  It never
    becomes the later request's witness or mutation authority.

    ``observation`` accepts ``request`` or ``source_request`` (a canonical
    witness request, or a request-binding document containing
    ``witness_request``), plus ``evidence``/``payload`` and optional
    ``artifact_bindings``.  This is intentionally narrower than a general
    cache rebind: physical geometry, source identity, and the complete native
    artifact chain must still pass ``adapt_secondary_audio_evidence``.
    """

    if not isinstance(current_request, Mapping) or not isinstance(observation, Mapping):
        return []
    source_request = observation.get("request") or observation.get("source_request")
    if not isinstance(source_request, Mapping):
        return []
    if isinstance(source_request.get("witness_request"), Mapping):
        source_request = source_request["witness_request"]
    evidence = observation.get("evidence") or observation.get("payload")
    if evidence is None and (
        "providers" in observation or "artifact_bindings" in observation
    ):
        evidence = observation
    artifact_bindings = observation.get("artifact_bindings")
    if artifact_bindings is None and isinstance(evidence, Mapping):
        artifact_bindings = evidence.get("artifact_bindings")
    if not isinstance(evidence, (Mapping, list, tuple)):
        return []

    try:
        current_target = build_witness_request(current_request)
        source_target = build_witness_request(source_request)
    except (KeyError, TypeError, ValueError):
        return []
    if dict(source_target) != dict(source_request):
        return []
    geometry_keys = (
        "cue_indexes",
        "matched_start_ms",
        "matched_end_ms",
        "context_start_ms",
        "context_end_ms",
        "source_media_timeline_offset_ms",
    )
    if any(source_target.get(key) != current_target.get(key) for key in geometry_keys):
        return []

    # The new request must carry an independently bound current source.  Do
    # not infer this identity from the old receipt's path or self-reported
    # candidate metadata.
    current_binding = current_request.get("clip_context_binding")
    if not isinstance(current_binding, Mapping):
        return []
    current_candidate_id = current_binding.get("candidate_id")
    if not isinstance(current_candidate_id, str) or not current_candidate_id:
        return []
    current_sources = current_binding.get("source_media_sha256s")
    if not isinstance(current_sources, list) or not current_sources:
        return []
    accepted = adapt_secondary_audio_evidence(
        source_target,
        evidence,
        artifact_bindings=(
            artifact_bindings if isinstance(artifact_bindings, Mapping) else None
        ),
    )
    if not accepted:
        return []
    source_sha = accepted[0].get("source_media_sha256")
    if not any(_same_digest(source_sha, value) for value in current_sources):
        return []
    source_start = source_target["matched_start_ms"] + source_target[
        "source_media_timeline_offset_ms"
    ]
    source_end = source_target["matched_end_ms"] + source_target[
        "source_media_timeline_offset_ms"
    ]
    normalized: list[dict[str, Any]] = []
    for candidate in accepted:
        if (
            candidate.get("request_sha256") != source_target["request_sha256"]
            or candidate.get("authority") != "EVIDENCE_ONLY"
            or candidate.get("mutation_authorized") is not False
            or candidate.get("candidate_exposure") != "none"
            or candidate.get("crop_is_exact_target") is not False
            or candidate.get("target_start_ms") != source_start
            or candidate.get("target_end_ms") != source_end
            or not _same_digest(candidate.get("source_media_sha256"), source_sha)
        ):
            return []
        normalized.append(
            {
                "schema_version": SECONDARY_AUDIO_CONTEXT_SCHEMA,
                "status": candidate.get("status"),
                "authority": "EVIDENCE_ONLY",
                "mutation_authorized": False,
                "context_only": True,
                "candidate_blind": True,
                "current_request_sha256": current_target["request_sha256"],
                "source_request_sha256": source_target["request_sha256"],
                "current_candidate_id": current_candidate_id,
                "request": dict(source_target),
                "verdict": dict(candidate),
                "provider": candidate.get("provider"),
                "model": candidate.get("model"),
                "source_media_sha256": candidate.get("source_media_sha256"),
                "source_target_geometry_ms": [source_start, source_end],
                "crop_geometry_ms": [
                    candidate.get("crop_start_ms"),
                    candidate.get("crop_end_ms"),
                ],
                "crop_is_exact_target": False,
                "evidence_scope": candidate.get("evidence_scope"),
                "transcript_scope": candidate.get("transcript_scope"),
                "audio_geometry_scope": candidate.get("audio_geometry_scope"),
                "artifact_provenance": candidate.get("artifact_provenance"),
            }
        )
    return normalized


def bind_request(request: Mapping[str, Any], **fields: Any) -> dict[str, Any]:
    bound = {**request, **fields}
    bound.pop("request_sha256", None)
    bound["request_sha256"] = hashlib.sha256(json.dumps(
        bound, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    return bound


def _secondary_seam(verifier: object) -> object | None:
    for name in _SECONDARY_SEAMS:
        candidate = getattr(verifier, name, None)
        if callable(candidate) or isinstance(candidate, (Mapping, list, tuple)):
            return candidate
    return None


def _route_observation_completed(row: Mapping[str, Any]) -> bool:
    """A configuration/transport failure is not an exhausted ASR observation."""
    if row.get("status") == "OBSERVED":
        return True
    native = row.get("native_observation")
    if row.get("status") != "UNCERTAIN" or not isinstance(native, Mapping):
        return False
    unsigned = dict(native)
    receipt_sha = unsigned.pop("receipt_sha256", None)
    return bool(
        native.get("status") in {"OBSERVED", "NO_SPEECH_REPORTED"}
        and native.get("authority") == "EVIDENCE_ONLY"
        and native.get("mutation_authorized") is False
        and native.get("candidate_exposure") == "none"
        and native.get("provider") == row.get("provider")
        and native.get("model") == row.get("model")
        and native.get("source_media_sha256") == row.get("source_media_sha256")
        and native.get("target_start_ms") == row.get("audio_start_ms")
        and native.get("target_end_ms") == row.get("audio_end_ms")
        and native.get("crop_is_exact_target") is True
        and _sha256(native.get("input_audio_sha256")) is not None
        and _sha256(native.get("response_sha256")) is not None
        and receipt_sha == _digest(unsigned)
    )


def independent_asr_candidates(
    request: Mapping[str, Any], verifier: Callable | None, *, cached_only: bool = False,
) -> tuple[dict[str, Any], dict[str, Any] | None, dict[str, Any]]:
    """Retain real independent routes without voting or text mutation."""

    observe = getattr(verifier, "observe_cached_independent_witnesses" if cached_only else "observe_independent_witnesses", None)
    secondary_source = None if cached_only else _secondary_seam(verifier)
    audit: dict[str, Any] = {"status": "UNAVAILABLE", "mutation_authorized": False,
                             "observations": [], "routes_attempted": 0, "routes_exhausted": False}
    if not callable(observe) and secondary_source is None:
        audit["reason_code"] = "INDEPENDENT_ASR_NOT_CONNECTED"
        return dict(request), None, audit
    target = build_witness_request(request)
    start = target["matched_start_ms"] + target["source_media_timeline_offset_ms"]
    end = target["matched_end_ms"] + target["source_media_timeline_offset_ms"]
    observations_recorded = False
    if secondary_source is not None:
        try:
            payload = secondary_source(target) if callable(secondary_source) else secondary_source
        except Exception as exc:
            audit.update(reason_code="SECONDARY_AUDIO_EXECUTION_FAILED", error_type=type(exc).__name__)
            return dict(request), None, audit
        rows, bindings = _payload(payload, None)
        if rows:
            audit["secondary_audio_adapter"] = SECONDARY_AUDIO_EVIDENCE_SCHEMA
            audit["observations"].extend(
                dict(row) if isinstance(row, Mapping) else {"status": "INVALID", "raw_type": type(row).__name__}
                for row in rows
            )
            accepted, rejections = _adapt_secondary_rows(
                target, rows, bindings, source_start=start, source_end=end
            )
            if rejections:
                audit["secondary_rejections"] = rejections
            if accepted:
                rows = accepted
                observations_recorded = True
            if any(item.get("status") == "REJECTED" for item in rejections):
                audit.update(
                    status="UNRESOLVED",
                    routes_exhausted=False,
                    reason_code=rejections[0].get("reason_code", "SECONDARY_AUDIO_NO_USABLE_TRANSCRIPT"),
                )
                return dict(request), None, audit
        elif not callable(observe):
            audit.update(status="UNRESOLVED", routes_exhausted=False, reason_code="SECONDARY_AUDIO_NO_CONFIGURED_ROUTES")
            return dict(request), None, audit
    if not callable(observe) and not observations_recorded:
        audit["reason_code"] = "INDEPENDENT_ASR_NOT_CONNECTED"
        return dict(request), None, audit
    try:
        if not observations_recorded:
            rows = observe(target)
    except Exception as exc:
        audit.update(reason_code="INDEPENDENT_ASR_EXECUTION_FAILED", error_type=type(exc).__name__)
        return dict(request), None, audit
    if not isinstance(rows, list) or not rows:
        audit["reason_code"] = ("INDEPENDENT_ASR_CACHE_MISS" if cached_only
                                else "INDEPENDENT_ASR_NO_CONFIGURED_ROUTES")
        return dict(request), None, audit
    accepted: list[dict[str, Any]] = []
    source_sha = None
    seen: set[tuple[str, str]] = set()
    for raw in rows:
        if not isinstance(raw, Mapping):
            if not observations_recorded:
                audit["observations"].append({"status": "INVALID", "reason_code": "NON_OBJECT_RESULT"})
            continue
        row = dict(raw)
        if not observations_recorded:
            audit["observations"].append(row)
        identity = (str(row.get("provider") or ""), str(row.get("model") or ""))
        digest = row.get("source_media_sha256")
        valid = (
            valid_witness_evidence(row, request_sha256=target["request_sha256"])
            and row.get("witness_protocol") == CANDIDATE_BLIND_TRANSCRIPT_PROTOCOL
            and row.get("candidate_exposure") == "none"
            and row.get("authority") == "EVIDENCE_ONLY"
            and row.get("mutation_authorized") is False
            and row.get("audio_start_ms") == start
            and row.get("audio_end_ms") == end
            and isinstance(digest, str)
            and len(digest) == 64
            and row.get("status") == "OBSERVED"
            and row.get("target_audible") is True
            and isinstance(row.get("exact_transcript"), str)
            and row["exact_transcript"].strip()
            and "\n" not in row["exact_transcript"]
            and "\r" not in row["exact_transcript"]
            and identity not in seen
            and all(identity)
        )
        if valid and (source_sha is None or source_sha == digest):
            source_sha = digest
            accepted.append(row)
            seen.add(identity)
    audit["routes_attempted"] = len(rows)
    source_digests = {
        row.get("source_media_sha256")
        for row in rows
        if isinstance(row, Mapping)
        and isinstance(row.get("source_media_sha256"), str)
        and len(row["source_media_sha256"]) == 64
    }
    if len(source_digests) > 1:
        audit.update(status="UNRESOLVED", routes_exhausted=False, reason_code="INDEPENDENT_ASR_SOURCE_CONFLICT")
        return dict(request), None, audit
    audit["routes_exhausted"] = all(
        isinstance(row, Mapping)
        and row.get("provider") in _SECONDARY_PROVIDERS
        and _route_observation_completed(row)
        and row.get("request_sha256") == target["request_sha256"]
        and row.get("audio_start_ms") == start
        and row.get("audio_end_ms") == end
        and isinstance(row.get("source_media_sha256"), str)
        for row in rows
    )
    if not accepted:
        audit.update(status="UNRESOLVED", reason_code="INDEPENDENT_ASR_NO_USABLE_TRANSCRIPT")
        return dict(request), None, audit
    candidates = [{"text": row["exact_transcript"].strip(), "source": row} for row in accepted]
    augmented = bind_request(request, proposed_candidates=candidates)
    witness = dict(accepted[0])
    witness["request_sha256"] = build_witness_request(augmented)["request_sha256"]
    witness["independent_observations"] = audit["observations"]
    audit.update(
        status="OBSERVED", candidate_count=len(candidates),
        accepted_observations=accepted,
        # G1 generator: the candidate-free request these routes were heard for
        # travels with them, so a later request (another candidate, wording or
        # evidence_id) projects them against their OWN request, not its hash.
        witness_request=dict(target),
    )
    return augmented, witness, audit
