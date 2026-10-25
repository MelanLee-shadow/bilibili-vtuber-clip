"""G6 shared judgment: do two acoustic check requests ask the same audio question?

Identity sweep (G6) and the admin rulings of the same day: a needs-audio
gate or pending audio obligation asked under one request stays the obligation of a
later request (another round, lane, mode or clock) exactly when the question is
the same:

- the same current cue text and its base text hash;
- the same proposed text, where the gate's answer depends on the candidate
  (41 「原判断的恢复消费」: 「与当前文字、候选、物理目标、clip/source身份一致」);
- the same physical target on the source clock: local window plus its own
  offset (41: 「本地 offset 与整片 SRT 字节只是表示」);
- the same source binding: candidate id, recording SHA-256 set and, where the
  binding carries it, the piece clock (``pieces``: each piece's recording
  SHA-256 and source window).

Representation and mode fields never enter the question, on one clock or across
a source/delivery clock change (admin, G6 class ruling; 41 updated):
the mode flag ``require_complete_utterance_support`` (collection authority only;
a text change still needs the current judge under the current mode),
``evidence_id`` (whole-SRT hash + wording), cue ordinals, the local clock and the
whole clip-context record (``context_sha256``, draft SRT, speech-memory ledger).
They are returned as ``deltas`` so the caller records what differed.
"""

from __future__ import annotations

from typing import Any, Mapping

# Fields that are recorded when they differ but never decide the question.
RECORDED_DELTA_KEYS = (
    "require_complete_utterance_support",
    "evidence_id",
    "cue_indexes",
    "matched_start_ms",
    "matched_end_ms",
    "source_media_timeline_offset_ms",
    "context_start_ms",
    "context_end_ms",
    "request_sha256",
)
_CLIP_CONTEXT_DELTA_KEYS = (
    "schema_version",
    "context_sha256",
    "whole_clip_draft_srt_sha256",
    "speech_memory_ledger_sha256",
)


def physical_target(request: Mapping[str, Any]) -> tuple[int, int] | None:
    """Local matched window plus its own offset (the source-clock target)."""
    values = [request.get(key) for key in (
        "matched_start_ms", "matched_end_ms", "source_media_timeline_offset_ms")]
    if any(type(value) is not int for value in values):
        return None
    start, end, offset = values
    if start < 0 or end <= start or offset < 0:
        return None
    return start + offset, end + offset


def _bare(value: object) -> str:
    return str(value).removeprefix("sha256:").lower()


def piece_clock(binding: Mapping[str, Any]) -> tuple[tuple[str, int, int], ...] | None:
    """The binding's piece clock (recording SHA-256 + source window per piece), if carried."""
    pieces = binding.get("pieces")
    if pieces is None:
        return None
    if not isinstance(pieces, (list, tuple)):
        return (("INVALID", -1, -1),)
    clock = []
    for piece in pieces:
        if not isinstance(piece, Mapping):
            return (("INVALID", -1, -1),)
        start, end = piece.get("start_ms"), piece.get("end_ms")
        clock.append((_bare(piece.get("source_media_sha256")),
                      start if type(start) is int else -1, end if type(end) is int else -1))
    return tuple(clock)


def source_binding(request: Mapping[str, Any]) -> tuple[Any, ...] | None:
    """Candidate id, recording SHA-256 set and piece clock of the request's clip binding."""
    clip = request.get("clip_context_binding")
    if not isinstance(clip, Mapping):
        return None
    candidate = clip.get("candidate_id")
    shas = clip.get("source_media_sha256s")
    if not isinstance(candidate, str) or not candidate or not isinstance(shas, (list, tuple)) or not shas:
        return None
    return candidate, tuple(sorted(_bare(value) for value in shas)), piece_clock(clip)


def same_audio_question(
    old: Mapping[str, Any],
    new: Mapping[str, Any],
    *,
    compare_proposed: bool,
    strict_source: bool = False,
) -> tuple[bool, dict[str, Any]]:
    """Return (same question, recorded deltas) for two acoustic check requests.

    A cross-clock carry (different local representation of the same physical
    target) additionally requires both source bindings, exactly as the former
    cross-clock rule did; a same-representation pair keeps working without
    one.  When both bindings are present they must agree (candidate id,
    recording set, piece clock).  ``strict_source`` keeps a caller's former
    rule that a binding present on only one side is a different question.
    """
    if not isinstance(old, Mapping) or not isinstance(new, Mapping):
        return False, {"reason": "REQUEST_MISSING"}
    keys = ("current_cue", "base_text_sha256") + (("proposed_cue",) if compare_proposed else ())
    for key in keys:
        if old.get(key) != new.get(key):
            return False, {"reason": "QUESTION_TEXT_DIFFERS", "field": key}
    old_target, new_target = physical_target(old), physical_target(new)
    if old_target is None or old_target != new_target:
        return False, {"reason": "PHYSICAL_TARGET_DIFFERS",
                       "old": list(old_target or []), "new": list(new_target or [])}
    same_representation = all(old.get(key) == new.get(key) for key in (
        "matched_start_ms", "matched_end_ms", "source_media_timeline_offset_ms"))
    old_source, new_source = source_binding(old), source_binding(new)
    if strict_source and (old_source is None) != (new_source is None):
        return False, {"reason": "SOURCE_BINDING_DIFFERS"}
    if old_source is not None and new_source is not None:
        if old_source != new_source:
            return False, {"reason": "SOURCE_BINDING_DIFFERS"}
    elif not same_representation:
        return False, {"reason": "CROSS_CLOCK_SOURCE_BINDING_REQUIRED"}
    deltas: dict[str, Any] = {}
    for key in RECORDED_DELTA_KEYS:
        if old.get(key) != new.get(key):
            deltas[key] = [old.get(key), new.get(key)]
    old_clip = old.get("clip_context_binding") if isinstance(old.get("clip_context_binding"), Mapping) else {}
    new_clip = new.get("clip_context_binding") if isinstance(new.get("clip_context_binding"), Mapping) else {}
    for key in _CLIP_CONTEXT_DELTA_KEYS:
        if old_clip.get(key) != new_clip.get(key):
            deltas["clip_context_binding." + key] = [old_clip.get(key), new_clip.get(key)]
    if not compare_proposed and old.get("proposed_cue") != new.get("proposed_cue"):
        deltas["proposed_cue"] = [old.get("proposed_cue"), new.get("proposed_cue")]
    return True, deltas
