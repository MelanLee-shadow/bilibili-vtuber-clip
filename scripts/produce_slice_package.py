#!/usr/bin/env python3
"""Produce a finished Li Dousha talk-slice package from explicit window specs.

This is the standard "圈中候选→出成品" driver (Ivan 2026-07-04). Its core
contract is the TOPIC-CLOSURE boundary rule: a clip must end where the topic
lands, on a COMPLETE sentence — never mid-sentence, never mid-story.

Flow: remote accurate piece cuts (supports cross-segment stitching) → local
concat → fresh whole-window transcription (glossary+danmaku+screen text) →
sentence-snap the final end to a transcription cue boundary near the semantic
target (fail closed if none) → VAD boundary audit + deterministic red flags →
SELF-REPAIR loop (Ivan 2026-07-10: flags move the cut to the next verifiably
clean sentence end / pull the opening onto the straddled sentence; an
unrepairable boundary fails closed — no quarantine state) → final accurate cut
→ VAD-sanitized text-final subtitles → speaker finalization → colour ASS burn
→ title/cover staging → flat delivery copy to lidousha/<date>/.

Spec JSON:
{
  "candidate_id": "...",
  "date": "2026-07-02",
  "output_root": "reports/.../finals",
  "delivery_name": "买弹幕梗当场拆台",
  "selection_hook": "弹幕让李豆沙表演上下摇……", # selected main event; auto-title must retain it
  "given_title": null,                      # Ivan-given title is verbatim-final
  "lead_pad_ms": 300,
  "pieces": [                                # concatenated in order
    {"remote_media": "<abs path on free>", "start_ms": ..., "end_ms": ...,
     "danmaku_xml_local": "<local path>"}
  ],
  "semantic_end_ms": <absolute ms in the LAST piece's segment timeline>
}
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.run_auto_review_shadow_pipeline import (
    _accurate_reencode_recut_command,
    _burn_preview_subtitles,
    _sha256,
    _stage_publish_draft,
    _write_source_range_srt,
)
from scripts.run_full_session_selector_cpa_shadow import (
    _build_aggregate_asr_transcriber,
    _build_ssh_agy_transcribe_runner,
)
from scripts.apply_subtitle_text_overrides import apply_document as apply_text_override_document
from scripts.apply_speaker_turn_overrides import SPEAKER_SUBTITLE_STYLE_ID
from src.autoslice.branding_intro import BrandingIntroError, require_branding_intro
from src.autoslice.chat_authority import (
    ChatEvidence,
    apply_audio_entity_verification,
    apply_authoritative_chat_evidence,
    build_human_text_entity_verifier,
    load_chat_jsonl,
    load_referent_groups,
    normalize_code_switch_surfaces,
    normalize_chat_text,
    normalize_srt_payload_text,
    normalize_srt_payload_window,
    recording_start_epoch_ms,
    reconcile_pending_text_overrides,
    sanitize_chat_display_text,
)
from src.autoslice.danmaku_evidence import DanmakuItem, load_danmaku_xml
from src.autoslice.jingting_chunker import parse_srt_cues
from src.autoslice.llm_client import LlmConfig, build_llm_call
from src.autoslice.song_name_pin import pin_song_names_in_srt
from src.autoslice.review_evidence import SourceCue
from src.autoslice.speaker_finalizer import (
    SpeakerFinalizationError,
    finalize_fast_solo_subtitles,
    validate_speaker_review_manifest_document,
)
from src.autoslice.speaker_session_router import (
    FAST_SOLO,
    SpeakerRoutingError,
    segment_binding_sha256,
    verify_speaker_routing_claim,
    verify_speaker_routing_claim_for_candidate,
)
from src.autoslice.subtitle_timing_qa import build_ssh_silero_vad_provider, sanitize_cue_timing
from src.autoslice.subtitle_regression import verify_subtitle_regression_surfaces
from src.autoslice.topic_entity_graph import (
    TopicEvidence,
    dynamic_referent_groups,
    load_topic_entity_graph,
    merge_referent_groups,
    resolve_topic_context,
)

SNAP_BEFORE_MS = 6_000
SNAP_AFTER_MS = 9_000
DANMAKU_PRE_CONTEXT_MS = 30_000
SC_PRE_CONTEXT_MS = 900_000  # include SCs up to 15min before the clip: she CLEARS THE
                             # SC BACKLOG in batches, reading SCs minutes after they
                             # appeared (《想要成为真正的拉拉》SC was read ~4min later),
                             # so "recent" is not enough — content-match picks the right
                             # one out of the backlog, irrelevant ones are ignored.


def _load_superchats(jsonl_path: Path) -> list[tuple[int, str, str]]:
    """(video_relative_ms, sender_uname, message) for SUPER_CHAT events in a blrec
    .jsonl event log.  SUPER_CHAT carries the EXACT full sender uname (unlike gift
    events, whose uname is masked to 小***), so both the name and the on-screen
    text she reads/thanks are recoverable ground truth.  This compatibility
    helper retains earliest-event fallback for filename-less fixtures;
    production ``_piece_chat_evidence`` uses the segment filename as t=0.
    De-duplicates the CN/JPN twin events by message text."""
    return [
        (item.offset_ms, item.sender, item.text)
        for item in load_chat_jsonl(jsonl_path)
        if item.kind == "superchat"
    ]


def _piece_chat_evidence(piece: dict) -> list[ChatEvidence]:
    """Load ordinary danmaku and SC independently from their healthy source.

    A zero-byte XML no longer disables the sibling JSONL.  When XML is healthy
    it remains the ordinary-danmaku timeline authority; JSONL still supplies
    exact SC text and is the fallback for ordinary messages.
    """

    xml_value = piece.get("danmaku_xml_local")
    xml_path = Path(str(xml_value)) if xml_value else None
    jsonl_value = piece.get("chat_jsonl_local") or piece.get("superchat_jsonl_local")
    if not jsonl_value:
        jsonl_value = str(Path(piece["remote_media"]).with_suffix(".jsonl"))
    jsonl_path = Path(str(jsonl_value))
    segment_zero = recording_start_epoch_ms(piece["remote_media"])
    jsonl_items = load_chat_jsonl(jsonl_path, recording_start_ms=segment_zero)

    evidence: list[ChatEvidence] = []
    xml_healthy = bool(xml_path and xml_path.is_file() and xml_path.stat().st_size > 0)
    if xml_healthy and xml_path is not None:
        evidence.extend(
            ChatEvidence(
                "danmaku",
                item.offset_ms,
                item.text,
                source=str(xml_path),
                source_sha256=hashlib.sha256(xml_path.read_bytes()).hexdigest(),
            )
            for item in load_danmaku_xml(xml_path)
        )
    else:
        evidence.extend(item for item in jsonl_items if item.kind == "danmaku")
    evidence.extend(item for item in jsonl_items if item.kind == "superchat")
    return evidence


def _load_independent_chat_support_srts(media_path: Path) -> list[str]:
    """Load only transcripts that never saw chat or rendered video text.

    ``.agy_refined.srt`` is deliberately excluded: that pass receives the
    structured danmaku context and source frames, so using it to prove that the
    streamer read the same message would be circular.
    """

    raw_audio_asr = media_path.with_suffix(".asr_draft.srt")
    if not raw_audio_asr.is_file():
        return []
    return [raw_audio_asr.read_text(encoding="utf-8", errors="replace")]
START_SNAP_MS = 2_500
TAIL_PAD_MS = 400
NEXT_SPEECH_ISLAND_GUARD_MS = 100
LEAD_AIR_MS = 250
REFINE_IF_OFF_BY_MS = 2_500
REFINE_IF_CUE_LONGER_MS = 10_000


def snap_end_to_sentence(cue_ends_ms: list[int], target_ms: int) -> int | None:
    """Nearest transcription cue end to the semantic target — the cut must sit
    on a complete-sentence boundary or nowhere."""

    candidates = [end for end in cue_ends_ms if target_ms - SNAP_BEFORE_MS <= end <= target_ms + SNAP_AFTER_MS]
    if not candidates:
        return None
    return min(candidates, key=lambda end: abs(end - target_ms))


def snap_start_to_sentence(cue_starts_ms: list[int], target_ms: int) -> int | None:
    """Nearest sentence START to the intended opening — the clip must open on
    a complete sentence, with a little lead air, or nowhere."""

    candidates = [s for s in cue_starts_ms if abs(s - target_ms) <= START_SNAP_MS]
    if not candidates:
        return None
    return min(candidates, key=lambda s: abs(s - target_ms))


def needs_tail_refinement(cues, *, snapped_end: int | None, target_ms: int) -> bool:
    """A run-on cue straddling the target, or a snap far off target, means the
    coarse cue grid cannot place the closure — do a fine micro-pass."""

    if snapped_end is None or abs(snapped_end - target_ms) > REFINE_IF_OFF_BY_MS:
        return True
    straddling = next((c for c in cues if c.start_ms < target_ms < c.end_ms), None)
    return bool(straddling and (straddling.end_ms - straddling.start_ms) > REFINE_IF_CUE_LONGER_MS)


def boundary_audit(spans, *, start_ms: int, cut_ms: int, start_snapped: bool, end_snapped: bool) -> dict:
    """Both cuts must sit on sentence boundaries; the audio at each cut is
    recorded honestly (speech flowing through a sentence-boundary cut is
    allowed, an unsnapped cut is not)."""

    crossing = next((s for s in spans if s.start_ms < cut_ms < s.end_ms), None)
    verdict = "ok_sentence_boundary_cut"
    if not start_snapped:
        verdict = "start_not_on_sentence_boundary"
    elif not end_snapped:
        verdict = "end_not_on_sentence_boundary"
    return {
        "start_on_sentence_boundary": start_snapped,
        "end_on_sentence_boundary": end_snapped,
        "end_cut_inside_speech_island": bool(crossing),
        "end_island_continues_ms": (crossing.end_ms - cut_ms) if crossing else 0,
        "verdict": verdict,
    }


ISLAND_CONTINUES_FLAG_MS = 1_500


def _norm_cue_text(text: str) -> str:
    return "".join(str(text).split())


def boundary_red_flags(
    *,
    audit: dict,
    cues,
    sanitized,
    final_start_ms: int,
    final_end_ms: int,
    snapped_end_ms: int,
    closure_text: str,
) -> list[str]:
    """Deterministic boundary red flags (2026-07-09 external audit).

    A green boundary verdict only says both cuts SNAPPED to ASR cue boundaries;
    the real 7/6+7/9 deliveries showed that is not enough (cuts inside a still-
    running speech island, final SRT ending on a different sentence than the
    claimed closure, next-topic text flashing in the tail pad).  These checks
    need no LLM.  Policy (Ivan 2026-07-10): an unattended pipeline REPAIRS what
    its own auditors detect — flags drive the deterministic self-repair loop in
    main(); a clip that cannot be repaired fails closed (no delivery).  There
    is no deliver-and-ask-a-human quarantine state."""
    flags: list[str] = []
    continues_ms = int(audit.get("end_island_continues_ms") or 0)
    if continues_ms >= ISLAND_CONTINUES_FLAG_MS:
        flags.append(f"speech_continues_{continues_ms}ms_after_cut")
    if any(c.start_ms < final_start_ms - 50 and c.end_ms > final_start_ms + 300 for c in cues):
        flags.append("opens_mid_sentence")
    if any(snapped_end_ms <= c.start_ms < final_end_ms for c in cues):
        flags.append("next_sentence_enters_tail_pad")
    if sanitized and _norm_cue_text(sanitized[-1].text) != _norm_cue_text(closure_text):
        flags.append("closure_not_final_subtitle")
    return flags


# 7/10 incident: the first plausible closure ended at +25,030ms and the old
# 25,000ms hard edge excluded it before VAD/next-cue cleanliness could even be
# evaluated.  Keep the normal repair bounded to 30s.  A runner retry may raise
# that absolute-from-semantic-target ceiling once to 60s, but only alongside
# more source context and only for deterministic continuation evidence.
BOUNDARY_REPAIR_EXTEND_CAP_MS = 30_000
BOUNDARY_REPAIR_EXTEND_CAP_MAX_MS = 60_000
MAX_BOUNDARY_REPAIRS = 3


def adaptive_tail_cut(spans, *, snapped_end_ms: int, padded_dur_ms: int, cues=()) -> dict:
    """Keep normal tail air unless it would enter a distinct next VAD island.

    A sentence-end snap is already semantic closure evidence.  If VAD then
    observes a *new* island after that closure, consuming the island is not
    evidence that the closure was incomplete: it is usually the next turn or
    topic.  Leave a small guard before that island, but only when the silence
    gap is wide enough to distinguish two islands robustly.  A narrow gap is
    left to the existing continuation/open-loop repair checks.
    """

    nominal_end_ms = min(padded_dur_ms, snapped_end_ms + TAIL_PAD_MS)
    next_span = min(
        (span for span in spans if span.start_ms > snapped_end_ms),
        key=lambda span: span.start_ms,
        default=None,
    )
    next_cue = min(
        (cue for cue in cues if cue.start_ms >= snapped_end_ms),
        key=lambda cue: cue.start_ms,
        default=None,
    )
    gap_ms = next_span.start_ms - snapped_end_ms if next_span is not None else None
    final_end_ms = nominal_end_ms
    reason = None
    # Subtitle timing is the direct authority for whether text from the next
    # turn will flash in the disposable tail pad.  VAD can begin slightly
    # later than the ASR cue (7/12: cue=123470, island=123592), so a VAD-only
    # clamp still included 22ms of the next subtitle and failed its own audit.
    if next_cue is not None and next_cue.start_ms < nominal_end_ms:
        final_end_ms = max(
            snapped_end_ms,
            next_cue.start_ms - NEXT_SPEECH_ISLAND_GUARD_MS,
        )
        reason = "tail_clamped_before_next_subtitle"
    elif (
        next_span is not None
        and next_span.start_ms < nominal_end_ms
        and gap_ms is not None
        and gap_ms >= 2 * NEXT_SPEECH_ISLAND_GUARD_MS
    ):
        final_end_ms = min(nominal_end_ms, next_span.start_ms - NEXT_SPEECH_ISLAND_GUARD_MS)
        reason = "tail_clamped_before_next_speech_island"
    return {
        "nominal_end_ms": nominal_end_ms,
        "final_end_ms": final_end_ms,
        "tail_pad_ms": TAIL_PAD_MS,
        "next_speech_island_start_ms": next_span.start_ms if next_span is not None else None,
        "next_speech_island_gap_ms": gap_ms,
        "next_speech_island_guard_ms": NEXT_SPEECH_ISLAND_GUARD_MS,
        "next_subtitle_start_ms": next_cue.start_ms if next_cue is not None else None,
        "reason": reason,
    }


def tail_requires_forward_extension(cues, spans, *, snapped_end_ms: int, cut_ms: int) -> bool:
    """True only when speech already in progress crosses the proposed cut.

    A later, distinct VAD island is not a reason to swallow another topic.
    Forward repair is reserved for a continuation island that began before the
    snapped closure, or an ASR cue/open loop that visibly crosses the cut.
    """

    continuation_island = any(
        span.start_ms < snapped_end_ms and span.end_ms > cut_ms for span in spans
    )
    crossing_cue = any(
        cue.start_ms <= snapped_end_ms < cut_ms < cue.end_ms for cue in cues
    )
    return continuation_island or crossing_cue


def next_clean_closure(cues, spans, *, after_ms: int, padded_dur_ms: int,
                       cap_ms: int = BOUNDARY_REPAIR_EXTEND_CAP_MS,
                       search_origin_ms: int | None = None) -> int | None:
    """Earliest LATER sentence end whose cut raises no deterministic red flag:
    nothing starts inside its tail pad and no speech island runs
    ≥ ISLAND_CONTINUES_FLAG_MS past the cut.

    This is the unattended repair move (Ivan 2026-07-10): extend FORWARD to
    where the talk actually lands — never retract, which would drop the very
    content the pick was chosen for.  ``cap_ms`` is absolute from the original
    semantic target (``search_origin_ms``), so repeated repairs cannot ratchet
    the window forward indefinitely."""
    origin_ms = after_ms if search_origin_ms is None else search_origin_ms
    max_end_ms = min(padded_dur_ms, origin_ms + cap_ms)
    ends = sorted({c.end_ms for c in cues if after_ms < c.end_ms <= max_end_ms})
    for end in ends:
        cut = adaptive_tail_cut(
            spans,
            cues=cues,
            snapped_end_ms=end,
            padded_dur_ms=padded_dur_ms,
        )["final_end_ms"]
        if any(end <= c.start_ms < cut for c in cues):
            continue
        crossing = next((s for s in spans if s.start_ms < cut < s.end_ms), None)
        if crossing and crossing.end_ms - cut >= ISLAND_CONTINUES_FLAG_MS:
            continue
        return end
    return None


def repair_start_for_straddler(cues, *, final_start_ms: int) -> int | None:
    """Repair an ``opens_mid_sentence`` flag by opening on the straddling
    sentence's own start — include the whole sentence rather than slicing into
    it.  Returns that sentence's start_ms (the new snapped start; the caller
    re-derives final_start with lead air), or None when no cue straddles the
    opening."""
    straddler = next(
        (c for c in cues if c.start_ms < final_start_ms - 50 and c.end_ms > final_start_ms + 300),
        None,
    )
    if straddler is None:
        return None
    return straddler.start_ms


def run(cmd: list[str], *, timeout: int = 3600) -> None:
    completed = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=timeout)
    if completed.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed rc={completed.returncode}: {completed.stderr[-400:]}")


def ffprobe_duration_ms(path: Path) -> int:
    completed = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return int(float(completed.stdout.strip()) * 1000)


def _validated_burned_artifact(record: dict) -> Path:
    """Return the exact burn bound into the record, never a directory glob."""

    preview = record.get("burned_preview")
    if not isinstance(preview, dict) or preview.get("status") != "BURNED":
        raise RuntimeError(f"FINAL_SUBTITLE_BURN_NOT_READY: {preview}")
    value = preview.get("path")
    burned = Path(str(value)) if value else None
    if burned is None or not burned.is_file():
        raise RuntimeError(f"FINAL_SUBTITLE_BURN_MISSING: {value}")
    actual = "sha256:" + _sha256(burned)
    expected_preview = preview.get("burned_sha256")
    expected_record = (record.get("artifact_hashes") or {}).get("burned_video_sha256")
    if expected_preview != actual or expected_record != actual:
        raise RuntimeError(
            f"FINAL_SUBTITLE_BURN_HASH_MISMATCH: preview={expected_preview} "
            f"record={expected_record} actual={actual}"
        )
    return burned


def _resolved_optional_path(value: object, *, relative_to: Path) -> Path | None:
    if not isinstance(value, (str, Path)) or not str(value):
        return None
    path = Path(value)
    return path if path.is_absolute() else (relative_to / path).resolve()


RECUT_PROVENANCE_SCHEMA = "lidousha-speaker-recut-provenance.v1"


def _write_json_atomic(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _source_media_sha256(host: str, source: Path) -> tuple[str, str]:
    """Hash the exact source bytes on the execution host."""

    if host in {"localhost", "127.0.0.1", "::1"}:
        resolved = source.resolve(strict=True)
        return str(resolved), segment_binding_sha256(resolved)
    completed = subprocess.run(
        ["ssh", host, "sha256sum -- " + shlex.quote(str(source))],
        check=False,
        capture_output=True,
        text=True,
        timeout=3600,
    )
    match = re.fullmatch(r"([0-9a-f]{64})\s+.+\n?", completed.stdout)
    if completed.returncode != 0 or match is None:
        raise RuntimeError(f"SOURCE_HASH_FAILED: {source}: {completed.stderr[-400:]}")
    return str(source), match.group(1)


def _valid_cached_provenance(
    path: Path, *, expected_without_output_hash: dict, output: Path
) -> bool:
    if not path.is_file() or not output.is_file():
        return False
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    expected = {
        **expected_without_output_hash,
        "output_sha256": _sha256(output),
    }
    return document == expected


FAST_FRESH_DERIVATION_SCHEMA = "lidousha-speaker-fast-fresh-derivation.v1"


class FastMediaRollbackError(RuntimeError):
    """Raised when the original media cannot be proven restored."""


def _begin_fast_media_transaction(media_path: Path) -> dict[str, object]:
    """Move the original media to an invocation-owned same-filesystem backup."""

    parent = media_path.parent.resolve(strict=True)
    destination = media_path.absolute()
    if destination.parent.resolve(strict=True) != parent:
        raise SpeakerRoutingError("FAST media destination escapes its parent")
    if destination.exists() and (
        destination.is_symlink() or not destination.is_file()
    ):
        raise SpeakerRoutingError("FAST media destination is not a regular file")
    descriptor, backup_name = tempfile.mkstemp(
        prefix=f".{media_path.name}.fast-backup-", dir=parent
    )
    os.close(descriptor)
    backup = Path(backup_name).absolute()
    if (
        backup.parent != parent
        or backup.is_symlink()
        or backup.resolve(strict=True) != backup
    ):
        backup.unlink(missing_ok=True)
        raise SpeakerRoutingError("FAST backup path is not invocation-owned")
    original_existed = destination.is_file()
    original_sha256 = _sha256(destination) if original_existed else None
    try:
        if original_existed:
            os.replace(destination, backup)
            if _sha256(backup) != original_sha256:
                raise FastMediaRollbackError("FAST backup hash mismatch")
    except Exception as exc:
        if original_existed and backup.is_file():
            destination.unlink(missing_ok=True)
            os.replace(backup, destination)
            if _sha256(destination) != original_sha256:
                raise FastMediaRollbackError(
                    "FAST transaction initialization could not restore original media"
                ) from exc
        else:
            backup.unlink(missing_ok=True)
        raise
    return {
        "media_path": destination,
        "backup_path": backup,
        "original_existed": original_existed,
        "original_sha256": original_sha256,
    }


def _rollback_fast_media_transaction(
    transaction: dict[str, object],
    *,
    output_srt_path: Path,
    output_ass_path: Path,
    output_manifest_path: Path,
) -> None:
    """Remove partial FAST outputs and prove the original media is restored."""

    media_path = Path(str(transaction["media_path"])).absolute()
    backup_path = Path(str(transaction["backup_path"])).absolute()
    for output in (output_srt_path, output_ass_path, output_manifest_path):
        output.unlink(missing_ok=True)
    media_path.unlink(missing_ok=True)
    if transaction["original_existed"] is True:
        if not backup_path.is_file() or backup_path.is_symlink():
            raise FastMediaRollbackError("FAST original backup is unavailable")
        os.replace(backup_path, media_path)
        if _sha256(media_path) != transaction["original_sha256"]:
            raise FastMediaRollbackError("FAST original media hash was not restored")
    else:
        backup_path.unlink(missing_ok=True)
        if media_path.exists():
            raise FastMediaRollbackError("FAST installed media survived absent-original rollback")
    if backup_path.exists():
        raise FastMediaRollbackError("FAST backup survived rollback")


def _commit_fast_media_transaction(transaction: dict[str, object]) -> None:
    """Commit fresh media by deleting the verified original backup."""

    media_path = Path(str(transaction["media_path"])).absolute()
    backup_path = Path(str(transaction["backup_path"])).absolute()
    if not media_path.is_file() or media_path.is_symlink():
        raise SpeakerRoutingError("FAST committed media is missing")
    backup_path.unlink(missing_ok=False)
    if backup_path.exists():
        raise SpeakerRoutingError("FAST backup was not removed after commit")


def _validate_fast_transaction_outputs(
    *,
    manifest: object,
    media_path: Path,
    output_srt_path: Path,
    output_ass_path: Path,
    output_manifest_path: Path,
    fresh_derivation: dict[str, object],
) -> None:
    """Validate every installed FAST surface before deleting the backup."""

    if (
        not isinstance(manifest, dict)
        or manifest.get("status") != "READY"
        or manifest.get("production_ready") is not True
    ):
        raise SpeakerRoutingError("FAST renderer did not return production READY")
    if not all(
        path.is_file() and not path.is_symlink()
        for path in (output_srt_path, output_ass_path, output_manifest_path)
    ):
        raise SpeakerRoutingError("FAST renderer omitted a required output")
    loaded_manifest = json.loads(output_manifest_path.read_text(encoding="utf-8"))
    if loaded_manifest != manifest:
        raise SpeakerRoutingError("FAST manifest bytes differ from returned manifest")
    if (
        manifest.get("source_media_sha256") != _sha256(media_path)
        or manifest.get("output_review_srt_sha256") != _sha256(output_srt_path)
        or manifest.get("output_ass_sha256") != _sha256(output_ass_path)
        or manifest.get("fresh_fast_derivation") != fresh_derivation
        or fresh_derivation.get("output_sha256") != _sha256(media_path)
    ):
        raise SpeakerRoutingError("FAST output/derivation hash validation failed")


def _derive_fresh_fast_media(
    *,
    host: str,
    media_path: Path,
    claimed_segment_path: Path,
    expected_segment_sha256: str,
    final_source_start_ms: int,
    final_source_end_ms: int,
) -> dict[str, object]:
    """Freshly derive FAST media from the verified source, bypassing caches.

    The temporary output is invocation-owned and lives beside the destination,
    so the final ``os.replace`` is atomic.  Existing piece/padded/final bytes
    and their caller-writable provenance are never read here.
    """

    if host not in {"localhost", "127.0.0.1", "::1"}:
        raise SpeakerRoutingError("FAST fresh derivation requires a local sealed source")
    if final_source_start_ms < 0 or final_source_end_ms <= final_source_start_ms:
        raise SpeakerRoutingError("FAST fresh derivation interval is invalid")
    source = claimed_segment_path.resolve(strict=True)
    if source.is_symlink() or not source.is_file():
        raise SpeakerRoutingError("FAST claimed source is not a regular file")
    source_before = segment_binding_sha256(source)
    if source_before != expected_segment_sha256:
        raise SpeakerRoutingError("FAST claimed source hash drifted before derivation")
    media_path.parent.mkdir(parents=True, exist_ok=True)
    expected_duration_ms = final_source_end_ms - final_source_start_ms
    with tempfile.TemporaryDirectory(
        prefix=f".{media_path.name}.fast-derive-", dir=media_path.parent
    ) as temporary_dir:
        temporary_output = Path(temporary_dir) / "fresh-recut.mp4"
        command = _accurate_reencode_recut_command(
            source_video=source,
            output_media=temporary_output,
            start_ms=final_source_start_ms,
            duration_ms=expected_duration_ms,
        )
        try:
            run(command, timeout=3600)
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            raise SpeakerRoutingError(f"FAST fresh derivation command failed: {exc}") from exc
        if (
            not temporary_output.is_file()
            or temporary_output.is_symlink()
            or temporary_output.stat().st_size <= 0
        ):
            raise SpeakerRoutingError("FAST fresh derivation produced no regular media")
        source_after = segment_binding_sha256(source)
        if source_after != source_before:
            raise SpeakerRoutingError("FAST claimed source drifted during derivation")
        actual_duration_ms = ffprobe_duration_ms(temporary_output)
        duration_tolerance_ms = max(350, int(expected_duration_ms * 0.01))
        if (
            actual_duration_ms <= 0
            or abs(actual_duration_ms - expected_duration_ms) > duration_tolerance_ms
        ):
            raise SpeakerRoutingError(
                "FAST fresh derivation duration mismatch: "
                f"expected={expected_duration_ms} actual={actual_duration_ms}"
            )
        output_sha256 = _sha256(temporary_output)
        os.replace(temporary_output, media_path)
    resolved_output = media_path.resolve(strict=True)
    if _sha256(resolved_output) != output_sha256:
        raise SpeakerRoutingError("FAST fresh derivation changed during atomic install")
    return {
        "schema_version": FAST_FRESH_DERIVATION_SCHEMA,
        "method": "canonical_accurate_recut_direct_from_claimed_segment",
        "cache_reused": False,
        "source_path": str(source),
        "source_sha256": source_before,
        "absolute_source_start_ms": final_source_start_ms,
        "absolute_source_end_ms": final_source_end_ms,
        "expected_duration_ms": expected_duration_ms,
        "actual_duration_ms": actual_duration_ms,
        "output_path": str(resolved_output),
        "output_sha256": output_sha256,
    }


def _format_srt_timestamp(ms: int) -> str:
    hours, remainder = divmod(ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def _write_route_mixed_overlap_evidence(
    *,
    media_path: Path,
    text_srt_path: Path,
    work_dir: Path,
    claim: dict,
    verified_route: object,
) -> Path:
    """Turn provider-wide mixed/overlap evidence into a bound terminal review.

    The acoustic provider currently reports at candidate-window granularity.
    Therefore every final cue is conservatively affected.  Each row includes a
    real extracted cue-audio digest, and the downstream finalizer emits no SRT
    or ASS unless exact speaker overrides cover all affected cues.
    """

    candidate_result = getattr(verified_route, "candidate_result")
    evidence = candidate_result["evidence"]
    reasons = []
    if evidence.get("mixed_speaker_within_unit_detected"):
        reasons.append("CUE_MIXED_SPEAKER")
    if evidence.get("overlap_detected"):
        reasons.append("CUE_OVERLAPPING_SPEECH")
    if not reasons:
        raise SpeakerRoutingError("provider route has no mixed/overlap evidence")
    cues = [
        cue
        for cue in parse_srt_cues(text_srt_path.read_text(encoding="utf-8"))
        if cue.text.strip()
    ]
    if not cues:
        raise SpeakerRoutingError("mixed/overlap review has no final subtitle cues")
    work_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for source_cue, cue in enumerate(cues, start=1):
        audio_path = work_dir / f"mixed-review-cue-{source_cue:04d}.wav"
        run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-ss",
                f"{cue.start_ms / 1000:.3f}",
                "-i",
                str(media_path),
                "-t",
                f"{max(1, cue.end_ms - cue.start_ms) / 1000:.3f}",
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-c:a",
                "pcm_s16le",
                str(audio_path),
            ],
            timeout=300,
        )
        rows.append(
            {
                "source_cue": source_cue,
                "zero_based_index": source_cue - 1,
                "start": _format_srt_timestamp(cue.start_ms),
                "end": _format_srt_timestamp(cue.end_ms),
                "text": cue.text,
                "audio_sha256": _sha256(audio_path),
                "reason_codes": reasons,
                "provider_details": {
                    "cluster_count": max(
                        2, int(evidence.get("speaker_count_lower_bound") or 1)
                    ),
                    "overlap_detected": bool(evidence.get("overlap_detected")),
                    "audio_path": str(audio_path.resolve()),
                    "provider_audio_window_sha256": evidence["audio_window_sha256"],
                    "provider_acoustic_observation_sha256": evidence[
                        "acoustic_observation_sha256"
                    ],
                    "routing_claim_sha256": getattr(verified_route, "claim_sha256"),
                    "provider_evidence_sha256": getattr(
                        verified_route, "provider_evidence_sha256"
                    ),
                },
            }
        )
    provider = claim.get("provider") or {}
    config_sha = (((provider.get("artifacts") or {}).get("config") or {}).get("sha256"))
    output = work_dir / "provider-mixed-overlap-evidence.json"
    _write_json_atomic(
        output,
        {
            "schema_version": "lidousha-speaker-mixed-overlap-evidence.v1",
            "status": "REVIEW_REQUIRED",
            "source_media_sha256": _sha256(media_path),
            "text_final_srt_sha256": _sha256(text_srt_path),
            "provider": {
                "name": str(provider.get("name") or "sealed-acoustic-provider"),
                "config_sha256": str(config_sha or ""),
            },
            "review_required_cues": rows,
        },
    )
    return output


def verify_chat_authority_final_surfaces(
    audit: dict,
    *,
    final_text_srt: str,
    final_speaker_srt: str,
    delivery_start_ms: int,
    delivery_end_ms: int,
) -> bool:
    """Verify every in-delivery authority decision at its original time span."""

    decision_rows: list[tuple[str, dict, str]] = []
    decision_rows.extend(
        ("exact_read", row, str(row.get("exact_text") or ""))
        for row in audit.get("applied") or []
    )
    decision_rows.extend(
        ("sc_sender", row, str(row.get("after") or ""))
        for row in audit.get("sender_repairs") or []
    )
    decision_rows.extend(
        ("reply_coreference", row, str(row.get("after") or ""))
        for row in audit.get("coreference_repairs") or []
    )
    decision_rows.extend(
        (
            "entity_repair",
            row,
            str(row.get("structured_exact_text") or "")
            or "".join(str(value) for value in row.get("after") or []),
        )
        for row in audit.get("entity_repairs") or []
    )
    if any(
        row.get("reconciliation_status") != "APPLIED_AND_HASH_VERIFIED"
        for row in audit.get("pending_text_overrides") or []
    ):
        audit["final_verification_failure"] = "PENDING_TEXT_OVERRIDE_NOT_RECONCILED"
        return False
    required_rows: list[dict] = []
    for kind, row, expected_text in decision_rows:
        matched_start = int(row["matched_start_ms"])
        matched_end = int(row["matched_end_ms"])
        row["final_verification_kind"] = kind
        if matched_end <= delivery_start_ms or matched_start >= delivery_end_ms:
            row["final_verification_scope"] = "OUTSIDE_DELIVERY"
            continue
        row["final_verification_scope"] = "DELIVERY"
        relative_start = max(0, matched_start - delivery_start_ms)
        relative_end = min(delivery_end_ms - delivery_start_ms, matched_end - delivery_start_ms)
        expected_norm = normalize_chat_text(expected_text)
        text_window = normalize_srt_payload_window(
            final_text_srt, start_ms=relative_start, end_ms=relative_end
        )
        speaker_window = normalize_srt_payload_window(
            final_speaker_srt,
            start_ms=relative_start,
            end_ms=relative_end,
            strip_speaker_labels=True,
        )
        row["final_relative_start_ms"] = relative_start
        row["final_relative_end_ms"] = relative_end
        row["survived_final_text_srt"] = bool(expected_norm and expected_norm in text_window)
        row["survived_final_speaker_srt"] = bool(expected_norm and expected_norm in speaker_window)
        required_rows.append(row)
    audit["final_required_decision_count"] = len(required_rows)
    audit["final_outside_delivery_count"] = len(decision_rows) - len(required_rows)
    return all(
        row.get("survived_final_text_srt") and row.get("survived_final_speaker_srt")
        for row in required_rows
    )


def _rebase_remote_speaker_manifest(
    manifest: dict,
    *,
    host: str,
    media_path: Path,
    text_srt_path: Path,
    override_path: Path | None,
    source_session_anchor_path: Path | None = None,
    mixed_overlap_evidence_path: Path | None = None,
    output_srt_path: Path,
    output_ass_path: Path,
) -> dict:
    """Replace deleted /tmp paths while retaining their execution provenance."""

    rebased = dict(manifest)
    path_keys = (
        "source_media",
        "text_final_srt",
        "speaker_override",
        "source_session_anchor_manifest",
        "mixed_overlap_evidence",
        "output_review_srt",
        "output_ass",
    )
    rebased["runtime_host"] = host
    rebased["ephemeral_runtime_paths"] = {key: manifest.get(key) for key in path_keys}
    rebased.update(
        {
            "source_media": str(media_path.resolve()),
            "text_final_srt": str(text_srt_path.resolve()),
            "speaker_override": str(override_path.resolve()) if override_path is not None else None,
            "source_session_anchor_manifest": (
                str(source_session_anchor_path.resolve())
                if source_session_anchor_path is not None
                else None
            ),
            "mixed_overlap_evidence": (
                str(mixed_overlap_evidence_path.resolve())
                if mixed_overlap_evidence_path is not None
                else None
            ),
            "output_review_srt": str(output_srt_path.resolve()),
            "output_ass": str(output_ass_path.resolve()),
        }
    )
    return rebased


def run_speaker_finalizer(
    *,
    host: str,
    candidate_id: str,
    media_path: Path,
    text_srt_path: Path,
    output_srt_path: Path,
    output_ass_path: Path,
    output_manifest_path: Path,
    work_dir: Path,
    override_path: Path | None = None,
    source_session_anchor_path: Path | None = None,
    mixed_overlap_evidence_path: Path | None = None,
    speaker_python: Path = Path("/opt/bilive/autoslice/venv-diar/bin/python"),
    reference_dir: Path = Path("/opt/bilive/autoslice/voiceprints/lidousha"),
    model_dir: Path = Path("/opt/bilive/autoslice/models/campp"),
) -> dict:
    """Run the pinned speaker runtime locally on free or through a remote temp.

    The command consumes the already-final text SRT.  Outputs are accepted only
    when the manifest says READY and every returned artifact hash matches.
    """

    safe_cid = re.sub(r"[^A-Za-z0-9_.-]+", "_", candidate_id)[:80]
    local_host = host in {"localhost", "127.0.0.1", "::1"}
    profile = ROOT / "assets" / "lidousha" / "voiceprint_profile.v1.json"
    output_srt_path.parent.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)
    # Clear stale results before reading any runtime prerequisite.  A missing
    # profile used to raise above this cleanup and leave an old speaker-final
    # sidecar that looked newer than the current text attempt.
    for stale in (output_srt_path, output_ass_path, output_manifest_path):
        stale.unlink(missing_ok=True)
    frozen_inputs = {
        "source_media_sha256": _sha256(media_path),
        "text_final_srt_sha256": _sha256(text_srt_path),
        "profile_sha256": _sha256(profile),
        "speaker_override_sha256": _sha256(override_path) if override_path is not None else None,
        "source_session_anchor_manifest_sha256": (
            _sha256(source_session_anchor_path)
            if source_session_anchor_path is not None
            else None
        ),
        "mixed_overlap_evidence_sha256": (
            _sha256(mixed_overlap_evidence_path)
            if mixed_overlap_evidence_path is not None
            else None
        ),
    }

    def valid_review_manifest(document: object) -> bool:
        try:
            validate_speaker_review_manifest_document(
                document,
                expected_media_sha256=str(frozen_inputs["source_media_sha256"]),
                expected_text_sha256=str(frozen_inputs["text_final_srt_sha256"]),
            )
        except SpeakerFinalizationError:
            return False
        return True

    if local_host:
        command = [
            str(speaker_python), "-m", "src.autoslice.speaker_finalizer",
            "--candidate-id", candidate_id,
            "--media", str(media_path), "--text-srt", str(text_srt_path),
            "--profile", str(profile), "--reference-dir", str(reference_dir),
            "--model-dir", str(model_dir), "--output-srt", str(output_srt_path),
            "--output-ass", str(output_ass_path), "--output-manifest", str(output_manifest_path),
            "--work-dir", str(work_dir),
        ]
        if override_path is not None:
            command.extend(["--overrides", str(override_path)])
        if source_session_anchor_path is not None:
            command.extend(["--source-session-anchors", str(source_session_anchor_path)])
        if mixed_overlap_evidence_path is not None:
            command.extend(["--mixed-overlap-evidence", str(mixed_overlap_evidence_path)])
        completed = subprocess.run(
            command, cwd=str(ROOT), check=False, capture_output=True, text=True, timeout=1800
        )
    else:
        remote_dir = f"/tmp/autoslice-speaker-{safe_cid}-{os.getpid()}"
        remote_media = f"{remote_dir}/media.mp4"
        remote_srt = f"{remote_dir}/text-final.srt"
        remote_output_srt = f"{remote_dir}/speaker-final.srt"
        remote_output_ass = f"{remote_dir}/speaker-final.ass"
        remote_manifest = f"{remote_dir}/speaker-final.json"
        remote_override = f"{remote_dir}/overrides.json"
        remote_session_anchors = f"{remote_dir}/source-session-anchors.json"
        remote_mixed_overlap = f"{remote_dir}/mixed-overlap-evidence.json"
        run(["ssh", host, f"rm -rf {shlex.quote(remote_dir)} && mkdir -p {shlex.quote(remote_dir)}/work"], timeout=120)
        try:
            run(["scp", "-q", str(media_path), str(text_srt_path), f"{host}:{remote_dir}/"], timeout=1800)
            # scp preserves local basenames, normalize to fixed remote names.
            remote_setup = (
                f"mv {shlex.quote(remote_dir + '/' + media_path.name)} {shlex.quote(remote_media)}; "
                f"mv {shlex.quote(remote_dir + '/' + text_srt_path.name)} {shlex.quote(remote_srt)}"
            )
            run(["ssh", host, remote_setup], timeout=120)
            if override_path is not None:
                run(["scp", "-q", str(override_path), f"{host}:{remote_override}"], timeout=120)
            if source_session_anchor_path is not None:
                run(
                    ["scp", "-q", str(source_session_anchor_path), f"{host}:{remote_session_anchors}"],
                    timeout=120,
                )
            if mixed_overlap_evidence_path is not None:
                run(
                    ["scp", "-q", str(mixed_overlap_evidence_path), f"{host}:{remote_mixed_overlap}"],
                    timeout=120,
                )
            remote_command = [
                str(speaker_python), "-m", "src.autoslice.speaker_finalizer",
                "--candidate-id", candidate_id,
                "--media", remote_media, "--text-srt", remote_srt,
                "--profile", "/opt/bilive/autoslice/repo/assets/lidousha/voiceprint_profile.v1.json",
                "--reference-dir", str(reference_dir), "--model-dir", str(model_dir),
                "--output-srt", remote_output_srt, "--output-ass", remote_output_ass,
                "--output-manifest", remote_manifest, "--work-dir", f"{remote_dir}/work",
            ]
            if override_path is not None:
                remote_command.extend(["--overrides", remote_override])
            if source_session_anchor_path is not None:
                remote_command.extend(["--source-session-anchors", remote_session_anchors])
            if mixed_overlap_evidence_path is not None:
                remote_command.extend(["--mixed-overlap-evidence", remote_mixed_overlap])
            shell_command = "cd /opt/bilive/autoslice/repo && " + " ".join(
                shlex.quote(part) for part in remote_command
            )
            completed = subprocess.run(
                ["ssh", host, shell_command], check=False, capture_output=True, text=True, timeout=1800
            )
            if completed.returncode == 0:
                for remote_source, local_target in (
                    (remote_output_srt, output_srt_path),
                    (remote_output_ass, output_ass_path),
                    (remote_manifest, output_manifest_path),
                ):
                    run(["scp", "-q", f"{host}:{remote_source}", str(local_target)], timeout=600)
            else:
                # The CLI writes a compact BLOCKED manifest before returning
                # non-zero.  Preserve it so callers see the real fail-closed
                # reason instead of an unrelated tail of ModelScope warnings.
                subprocess.run(
                    ["scp", "-q", f"{host}:{remote_manifest}", str(output_manifest_path)],
                    check=False,
                    timeout=120,
                )
        finally:
            subprocess.run(["ssh", host, f"rm -rf {shlex.quote(remote_dir)}"], check=False, timeout=120)
    if completed.returncode != 0:
        if output_manifest_path.is_file():
            try:
                blocked_manifest = json.loads(output_manifest_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                blocked_manifest = {}
            if (
                blocked_manifest.get("status") == "SPEAKER_REVIEW_REQUIRED"
                and blocked_manifest.get("reason")
                and valid_review_manifest(blocked_manifest)
            ):
                raise RuntimeError(
                    f"SPEAKER_REVIEW_REQUIRED: {blocked_manifest['reason']}"
                )
            if blocked_manifest.get("status") == "BLOCKED" and blocked_manifest.get("reason"):
                raise RuntimeError(
                    f"SPEAKER_FINALIZATION_BLOCKED: {blocked_manifest['reason']}"
                )
        raise RuntimeError(
            "SPEAKER_FINALIZATION_FAILED: " + (completed.stderr or completed.stdout)[-1200:]
        )
    if not output_manifest_path.is_file():
        raise RuntimeError("SPEAKER_FINALIZATION_FAILED: READY process omitted its manifest")
    manifest = json.loads(output_manifest_path.read_text(encoding="utf-8"))
    if not local_host:
        manifest = _rebase_remote_speaker_manifest(
            manifest,
            host=host,
            media_path=media_path,
            text_srt_path=text_srt_path,
            override_path=override_path,
            source_session_anchor_path=source_session_anchor_path,
            mixed_overlap_evidence_path=mixed_overlap_evidence_path,
            output_srt_path=output_srt_path,
            output_ass_path=output_ass_path,
        )
        output_manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    if manifest.get("status") == "SPEAKER_REVIEW_REQUIRED":
        if valid_review_manifest(manifest):
            raise RuntimeError(f"SPEAKER_REVIEW_REQUIRED: {manifest.get('reason')}")
        raise RuntimeError("SPEAKER_FINALIZATION_BLOCKED: invalid speaker review evidence")
    if manifest.get("status") != "READY" or manifest.get("production_ready") is not True:
        raise RuntimeError(f"SPEAKER_FINALIZATION_BLOCKED: {manifest.get('reason')}")
    expected = {
        output_srt_path: manifest.get("output_review_srt_sha256"),
        output_ass_path: manifest.get("output_ass_sha256"),
    }
    for path, digest in expected.items():
        if not path.is_file() or digest != _sha256(path):
            raise RuntimeError(f"SPEAKER_FINALIZATION_HASH_MISMATCH: {path}")
    if manifest.get("source_media_sha256") != frozen_inputs["source_media_sha256"]:
        raise RuntimeError("SPEAKER_FINALIZATION_MEDIA_BINDING_MISMATCH")
    if manifest.get("text_final_srt_sha256") != frozen_inputs["text_final_srt_sha256"]:
        raise RuntimeError("SPEAKER_FINALIZATION_TEXT_BINDING_MISMATCH")
    if manifest.get("profile_sha256") != frozen_inputs["profile_sha256"]:
        raise RuntimeError("SPEAKER_FINALIZATION_PROFILE_BINDING_MISMATCH")
    if manifest.get("speaker_override_sha256") != frozen_inputs["speaker_override_sha256"]:
        raise RuntimeError("SPEAKER_FINALIZATION_OVERRIDE_BINDING_MISMATCH")
    if (
        manifest.get("source_session_anchor_manifest_sha256")
        != frozen_inputs["source_session_anchor_manifest_sha256"]
    ):
        raise RuntimeError("SPEAKER_FINALIZATION_SOURCE_SESSION_BINDING_MISMATCH")
    if (
        manifest.get("mixed_overlap_evidence_sha256")
        != frozen_inputs["mixed_overlap_evidence_sha256"]
    ):
        raise RuntimeError("SPEAKER_FINALIZATION_MIXED_OVERLAP_BINDING_MISMATCH")
    current_inputs = {
        "source_media_sha256": _sha256(media_path),
        "text_final_srt_sha256": _sha256(text_srt_path),
        "profile_sha256": _sha256(profile),
        "speaker_override_sha256": _sha256(override_path) if override_path is not None else None,
        "source_session_anchor_manifest_sha256": (
            _sha256(source_session_anchor_path)
            if source_session_anchor_path is not None
            else None
        ),
        "mixed_overlap_evidence_sha256": (
            _sha256(mixed_overlap_evidence_path)
            if mixed_overlap_evidence_path is not None
            else None
        ),
    }
    if current_inputs != frozen_inputs:
        raise RuntimeError("SPEAKER_FINALIZATION_INPUT_DRIFT")
    return manifest


def _default_speaker_mode() -> str:
    """Resolve the effective default speaker mode from the environment.

    argparse does not validate defaults against ``choices``, so an unknown
    env value must fail toward the standing uniform_host policy instead of
    silently reaching the finalizer dispatch.
    """

    mode = os.environ.get("AUTOSLICE_SPEAKER_MODE", "uniform_host")
    return mode if mode in ("uniform_host", "required", "auto") else "uniform_host"


def run_producer_speaker_finalization(
    *,
    speaker_mode: str,
    host: str,
    candidate_id: str,
    media_path: Path,
    text_srt_path: Path,
    output_srt_path: Path,
    output_ass_path: Path,
    output_manifest_path: Path,
    work_dir: Path,
    spec: dict,
    spec_parent: Path,
    override_path: Path | None,
    source_session_anchor_path: Path | None,
    mixed_overlap_evidence_path: Path | None,
    speaker_python: Path,
    final_source_start_ms: int | None,
    final_source_end_ms: int | None,
) -> dict:
    """Dispatch a verified FAST route or the existing binary finalizer.

    Any missing, malformed, stale, or uncertain routing authority falls back
    to the full finalizer.  The FAST branch is unavailable when candidate-local
    override/session/mixed evidence exists, so it cannot bypass stronger
    authority or a review gate.
    """

    if speaker_mode == "uniform_host":
        raise ValueError(
            "uniform_host mode must never dispatch speaker finalization; "
            "the caller skips this step entirely"
        )
    fallback_reason = "SPEAKER_MODE_REQUIRED"
    if speaker_mode == "auto":
        fallback_reason = "ROUTING_CLAIM_MISSING"
        claim_path = _resolved_optional_path(
            spec.get("speaker_routing_claim"), relative_to=spec_parent
        )
        expected_claim_sha256 = str(
            spec.get("speaker_routing_claim_sha256") or ""
        ).removeprefix("sha256:")
        candidate = spec.get("speaker_routing_candidate")
        if claim_path is not None and expected_claim_sha256:
            try:
                if _sha256(claim_path) != expected_claim_sha256:
                    raise SpeakerRoutingError("routing claim bytes drifted")
                claim = json.loads(claim_path.read_text(encoding="utf-8"))
                if not isinstance(claim, dict):
                    raise SpeakerRoutingError("routing claim must be an object")
                if claim.get("decision") != FAST_SOLO:
                    reasons = claim.get("reason_codes")
                    fallback_reason = (
                        "ROUTER_REQUIRED:"
                        + ",".join(str(value) for value in reasons)
                        if isinstance(reasons, list) and reasons
                        else "ROUTER_REQUIRED"
                    )
                    if isinstance(candidate, dict):
                        verified_binary = verify_speaker_routing_claim_for_candidate(
                            claim,
                            claim_path=claim_path,
                            expected_claim_sha256=expected_claim_sha256,
                            expected_date=str(spec.get("date") or ""),
                            expected_candidate_id=candidate_id,
                            expected_segment_binding_sha256=str(
                                candidate.get("segment_binding_sha256") or ""
                            ),
                            expected_start_ms=int(candidate["start_ms"]),
                            expected_end_ms=int(candidate["end_ms"]),
                            expected_pipeline_fingerprint=str(
                                candidate.get("pipeline_fingerprint") or ""
                            ),
                            expected_segment_path=Path(str(candidate["segment_path"])),
                            expected_segment_stat_signature=candidate[
                                "segment_stat_signature"
                            ],
                            expected_bcut_srt_path=Path(
                                str(candidate["bcut_srt_path"])
                            ),
                            expected_bcut_srt_sha256=str(
                                candidate.get("bcut_srt_sha256") or ""
                            ),
                        )
                        result = verified_binary.candidate_result
                        if (
                            result.get("mixed_or_overlap_detected") is True
                            and mixed_overlap_evidence_path is None
                        ):
                            mixed_overlap_evidence_path = (
                                _write_route_mixed_overlap_evidence(
                                    media_path=media_path,
                                    text_srt_path=text_srt_path,
                                    work_dir=work_dir,
                                    claim=claim,
                                    verified_route=verified_binary,
                                )
                            )
                            fallback_reason = "ROUTER_MIXED_OVERLAP_REVIEW_REQUIRED"
                elif any(
                    path is not None
                    for path in (
                        override_path,
                        source_session_anchor_path,
                        mixed_overlap_evidence_path,
                    )
                ):
                    fallback_reason = "CANDIDATE_SPEAKER_AUTHORITY_REQUIRES_BINARY"
                elif not isinstance(candidate, dict):
                    fallback_reason = "ROUTING_CANDIDATE_BINDING_MISSING"
                elif (
                    final_source_start_ms is None
                    or final_source_end_ms is None
                    or not int(candidate["start_ms"])
                    <= final_source_start_ms
                    < final_source_end_ms
                    <= int(candidate["end_ms"])
                ):
                    fallback_reason = "FINAL_RECUT_OUTSIDE_ROUTING_COVERAGE"
                else:
                    verified = verify_speaker_routing_claim(
                        claim,
                        claim_path=claim_path,
                        expected_claim_sha256=expected_claim_sha256,
                        expected_date=str(spec.get("date") or ""),
                        expected_candidate_id=candidate_id,
                        expected_segment_binding_sha256=str(
                            candidate.get("segment_binding_sha256") or ""
                        ),
                        expected_start_ms=int(candidate["start_ms"]),
                        expected_end_ms=int(candidate["end_ms"]),
                        expected_pipeline_fingerprint=str(
                            candidate.get("pipeline_fingerprint") or ""
                        ),
                        expected_segment_path=Path(str(candidate["segment_path"])),
                        expected_segment_stat_signature=candidate["segment_stat_signature"],
                        expected_bcut_srt_path=Path(str(candidate["bcut_srt_path"])),
                        expected_bcut_srt_sha256=str(
                            candidate.get("bcut_srt_sha256") or ""
                        ),
                    )
                    transaction = _begin_fast_media_transaction(media_path)
                    try:
                        fresh_derivation = _derive_fresh_fast_media(
                            host=host,
                            media_path=media_path,
                            claimed_segment_path=Path(str(candidate["segment_path"])),
                            expected_segment_sha256=str(
                                candidate.get("segment_binding_sha256") or ""
                            ),
                            final_source_start_ms=final_source_start_ms,
                            final_source_end_ms=final_source_end_ms,
                        )
                        fast_manifest = finalize_fast_solo_subtitles(
                            media_path=media_path,
                            text_srt_path=text_srt_path,
                            output_srt_path=output_srt_path,
                            output_ass_path=output_ass_path,
                            output_manifest_path=output_manifest_path,
                            candidate_id=candidate_id,
                            routing_claim_path=claim_path,
                            verified_route=verified,
                            fresh_derivation=fresh_derivation,
                        )
                        _validate_fast_transaction_outputs(
                            manifest=fast_manifest,
                            media_path=media_path,
                            output_srt_path=output_srt_path,
                            output_ass_path=output_ass_path,
                            output_manifest_path=output_manifest_path,
                            fresh_derivation=fresh_derivation,
                        )
                        _commit_fast_media_transaction(transaction)
                    except Exception as fast_exc:
                        try:
                            _rollback_fast_media_transaction(
                                transaction,
                                output_srt_path=output_srt_path,
                                output_ass_path=output_ass_path,
                                output_manifest_path=output_manifest_path,
                            )
                        except Exception as rollback_exc:
                            raise FastMediaRollbackError(
                                "FAST failed and original media rollback could not be verified"
                            ) from rollback_exc
                        raise SpeakerRoutingError(
                            f"FAST transaction rolled back: {type(fast_exc).__name__}: {fast_exc}"
                        ) from fast_exc
                    return fast_manifest
            except (
                KeyError,
                OSError,
                TypeError,
                ValueError,
                json.JSONDecodeError,
                SpeakerFinalizationError,
                SpeakerRoutingError,
            ) as exc:
                fallback_reason = f"ROUTING_UNCERTAIN:{type(exc).__name__}:{exc}"

    manifest = run_speaker_finalizer(
        host=host,
        candidate_id=candidate_id,
        media_path=media_path,
        text_srt_path=text_srt_path,
        output_srt_path=output_srt_path,
        output_ass_path=output_ass_path,
        output_manifest_path=output_manifest_path,
        work_dir=work_dir,
        override_path=override_path,
        source_session_anchor_path=source_session_anchor_path,
        mixed_overlap_evidence_path=mixed_overlap_evidence_path,
        speaker_python=speaker_python,
    )
    manifest["speaker_routing"] = {
        "requested_mode": speaker_mode,
        "decision": "RUN_BINARY_FINALIZER",
        "reason": fallback_reason,
    }
    output_manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--ssh-host", default="free")
    parser.add_argument(
        "--substrate",
        choices=("aggregate_asr", "agy_fresh"),
        default="aggregate_asr",
        help="subtitle substrate: aggregate_asr = free ASR (bcut/jianying, accurate ms timeline) + text-only correction (default); agy_fresh = legacy agy whole-window transcription.",
    )
    parser.add_argument(
        "--speaker-mode",
        choices=("uniform_host", "required", "auto"),
        default=_default_speaker_mode(),
        help=(
            "uniform_host = no speaker separation: every cue keeps the single host "
            "(李豆沙) style and speaker uncertainty can never reject a delivery "
            "(Ivan 2026-07-13 data-accumulation policy; evidence capture stays passive); "
            "required = always run binary finalizer; auto = verified FAST_SOLO else binary fallback"
        ),
    )
    parser.add_argument("--subtitle-text-overrides", type=Path, help="hash-bound human text decisions applied before speaker inference")
    parser.add_argument(
        "--subtitle-regression",
        type=Path,
        help="candidate-scoped final subtitle truth gate evaluated before burn/delivery",
    )
    parser.add_argument("--speaker-overrides", type=Path, help="hash-bound reviewed turn/split/overlap decisions applied after automatic speaker inference")
    parser.add_argument(
        "--speaker-source-session-anchors",
        type=Path,
        help="hash-bound high-gate Li Dousha anchors from the same source recording",
    )
    parser.add_argument(
        "--speaker-mixed-overlap-evidence",
        type=Path,
        help="hash-bound provider evidence that mixed/overlap cues require review",
    )
    parser.add_argument(
        "--speaker-python",
        type=Path,
        default=Path(os.environ.get("AUTOSLICE_SPEAKER_PYTHON", "/opt/bilive/autoslice/venv-diar/bin/python")),
    )
    parser.add_argument(
        "--correct",
        choices=("bcut_agy_cpa", "cpa", "agy", "none"),
        default="bcut_agy_cpa",
        help="correction: bcut_agy_cpa = BCUT draft + AGY refine (hears audio) + CPA reconcile (default, best quality); cpa = CPA text-only (fast, blind to audio); agy = AGY refine only; none = raw BCUT.",
    )
    parser.add_argument(
        "--screen-text",
        action="store_true",
        help="cpa correct only: also feed agy-extracted on-screen text (superchat cards/titles) to CPA. Off by default — the glossary is the reliable authority for known names; agy vision on stylized cards is unreliable and can override the glossary. Use only when a clip's meaning hinges on on-screen text NOT yet in the glossary.",
    )
    parser.add_argument(
        "--reuse-cover",
        action="store_true",
        help="subtitle-only re-run: keep the EXISTING delivered cover, skip the AI cover (art-direction LLM + gpt-image-2 ~90s/clip). Title still regenerates. Use when re-correcting subtitles on an already-covered clip.",
    )
    args = parser.parse_args(argv)
    # Mandatory delivery intro (Ivan 2026-07-12): resolve before any expensive
    # stage so an unavailable intro fails the run instead of a late delivery.
    try:
        branding_intro = require_branding_intro(ROOT)
    except BrandingIntroError as exc:
        raise SystemExit(f"BRANDING_INTRO_UNAVAILABLE: {exc}")
    spec = json.loads(args.spec.read_text(encoding="utf-8"))
    repair_cap_raw = spec.get("boundary_repair_extend_cap_ms", BOUNDARY_REPAIR_EXTEND_CAP_MS)
    if isinstance(repair_cap_raw, bool) or not isinstance(repair_cap_raw, int):
        raise ValueError("boundary_repair_extend_cap_ms must be an integer")
    if not BOUNDARY_REPAIR_EXTEND_CAP_MS <= repair_cap_raw <= BOUNDARY_REPAIR_EXTEND_CAP_MAX_MS:
        raise ValueError(
            "boundary_repair_extend_cap_ms must stay within "
            f"{BOUNDARY_REPAIR_EXTEND_CAP_MS}..{BOUNDARY_REPAIR_EXTEND_CAP_MAX_MS}"
        )
    boundary_repair_extend_cap_ms = repair_cap_raw
    truth_mode = os.environ.get("AUTOSLICE_HUMAN_TRUTH_MODE", "delivery").strip().lower()
    if truth_mode not in {"delivery", "withheld"}:
        raise ValueError("AUTOSLICE_HUMAN_TRUTH_MODE must be delivery or withheld")
    spec_truth_mode = str(spec.get("human_truth_mode") or truth_mode).strip().lower()
    if spec_truth_mode != truth_mode:
        raise ValueError("spec human_truth_mode does not match the process truth-isolation mode")
    if truth_mode == "withheld":
        leaked_inputs = [
            name
            for name, value in (
                ("--subtitle-text-overrides", args.subtitle_text_overrides),
                ("--subtitle-regression", args.subtitle_regression),
                ("--speaker-overrides", args.speaker_overrides),
                ("spec.subtitle_text_overrides", spec.get("subtitle_text_overrides")),
                ("spec.subtitle_regression", spec.get("subtitle_regression")),
                ("spec.speaker_overrides", spec.get("speaker_overrides")),
            )
            if value is not None
        ]
        if leaked_inputs:
            raise ValueError(
                "blind subtitle generation refuses human-truth inputs: " + ", ".join(leaked_inputs)
            )
    # Time-sensitive terminology must be evaluated as of the recording date,
    # never the processing date.  This prevents future-news leakage when an old
    # stream is repaired later.
    if isinstance(spec.get("date"), str):
        os.environ["LIDOUSHA_TERM_AS_OF"] = spec["date"]

    cid = spec["candidate_id"]
    out_root = Path(spec["output_root"]) / cid
    out_root.mkdir(parents=True, exist_ok=True)
    host = args.ssh_host
    text_override_path = args.subtitle_text_overrides or _resolved_optional_path(
        spec.get("subtitle_text_overrides"), relative_to=args.spec.parent
    )
    subtitle_regression_path = args.subtitle_regression or _resolved_optional_path(
        spec.get("subtitle_regression"), relative_to=args.spec.parent
    )

    # 1. Remote accurate piece cuts (production encode params), pull local.
    piece_paths: list[Path] = []
    piece_provenance_rows: list[dict] = []
    for index, piece in enumerate(spec["pieces"]):
        local = out_root / f"piece_{index}_{piece['start_ms']}_{piece['end_ms']}.mp4"
        source_path, source_sha256 = _source_media_sha256(
            host, Path(piece["remote_media"])
        )
        piece_provenance_path = local.with_suffix(".provenance.json")
        expected_piece = {
            "source_path": source_path,
            "source_sha256": source_sha256,
            "start_ms": int(piece["start_ms"]),
            "end_ms": int(piece["end_ms"]),
            "output_path": str(local.resolve()),
        }
        if not _valid_cached_provenance(
            piece_provenance_path,
            expected_without_output_hash=expected_piece,
            output=local,
        ):
            local.unlink(missing_ok=True)
            piece_provenance_path.unlink(missing_ok=True)
            remote_tmp = f"/tmp/produce_{cid}_{index}.mp4"
            cmd = _accurate_reencode_recut_command(
                source_video=Path(piece["remote_media"]),
                output_media=Path(remote_tmp),
                start_ms=piece["start_ms"],
                duration_ms=piece["end_ms"] - piece["start_ms"],
            )
            run(["ssh", host, " ".join(shlex.quote(str(part)) for part in cmd)], timeout=3600)
            run(["scp", "-q", f"{host}:{remote_tmp}", str(local)], timeout=1800)
            run(["ssh", host, f"rm -f {shlex.quote(remote_tmp)}"], timeout=60)
            if _source_media_sha256(host, Path(piece["remote_media"])) != (
                source_path,
                source_sha256,
            ):
                local.unlink(missing_ok=True)
                raise RuntimeError("SOURCE_MEDIA_DRIFT_DURING_PIECE_RECUT")
            _write_json_atomic(
                piece_provenance_path,
                {**expected_piece, "output_sha256": _sha256(local)},
            )
        piece_paths.append(local)
        piece_provenance_rows.append(
            json.loads(piece_provenance_path.read_text(encoding="utf-8"))
        )
    durations = [ffprobe_duration_ms(p) for p in piece_paths]

    padded = out_root / f"padded_{spec['pieces'][0]['start_ms']}_{spec['pieces'][-1]['end_ms']}.mp4"
    padded_provenance_path = padded.with_suffix(".provenance.json")
    expected_padded = {
        "inputs": [
            {"path": str(path.resolve()), "sha256": _sha256(path)}
            for path in piece_paths
        ],
        "output_path": str(padded.resolve()),
    }
    padded_cache_valid = _valid_cached_provenance(
        padded_provenance_path,
        expected_without_output_hash=expected_padded,
        output=padded,
    )
    if not padded_cache_valid:
        padded.unlink(missing_ok=True)
        padded_provenance_path.unlink(missing_ok=True)
    if len(piece_paths) == 1:
        if not padded.exists():
            run(["cp", str(piece_paths[0]), str(padded)])
    elif not padded.exists():
        concat_list = out_root / "concat.txt"
        concat_list.write_text("".join(f"file '{p.resolve()}'\n" for p in piece_paths), encoding="utf-8")
        run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
             "-i", str(concat_list), "-c", "copy", str(padded)])
    if not padded_cache_valid:
        _write_json_atomic(
            padded_provenance_path,
            {**expected_padded, "output_sha256": _sha256(padded)},
        )
    padded_dur = ffprobe_duration_ms(padded)

    # 2. Danmaku + on-screen SUPER_CHATs merged onto the concat timeline.
    merged: list[DanmakuItem] = []
    authoritative_chat: list[ChatEvidence] = []
    offset = 0
    for piece, dur in zip(spec["pieces"], durations):
        for item in _piece_chat_evidence(piece):
            rel = item.offset_ms - piece["start_ms"]
            pre_context = SC_PRE_CONTEXT_MS if item.kind == "superchat" else DANMAKU_PRE_CONTEXT_MS
            if not (-pre_context <= rel <= dur + 1_000):
                continue
            marker = f"·{item.sender}" if item.sender else ""
            if item.kind == "superchat":
                prefix = "【SC此前" if rel < 0 else "【SC"
            else:
                prefix = "【弹幕此前" if rel < 0 else "【弹幕"
            label = f"{prefix}{marker}】{sanitize_chat_display_text(item.text)}"
            merged.append(DanmakuItem(offset_ms=offset + max(0, rel), text=label))
            authoritative_chat.append(
                ChatEvidence(
                    item.kind,
                    offset + rel,
                    item.text,
                    item.sender,
                    item.source,
                    item.source_sha256,
                    item.source_event_id,
                )
            )
        offset += dur
    merged.sort(key=lambda item: item.offset_ms)

    # 3. Fresh transcription of the padded window.
    song_name_candidates = [
        str(title).strip()
        for title in (spec.get("song_name_candidates") or [])
        if isinstance(title, str) and str(title).strip()
    ]
    if args.substrate == "aggregate_asr":
        transcriber = _build_aggregate_asr_transcriber(
            host,
            danmaku_items=merged or None,
            window_start_ms=0,
            source_video=padded,
            correct=args.correct,
            screen_text=args.screen_text,
            recording_date=str(spec.get("date") or ""),
            topic_hint=str(spec.get("selection_hook") or ""),
            song_name_candidates=song_name_candidates,
        )
    else:
        transcriber = _build_ssh_agy_transcribe_runner(host, danmaku_items=merged or None, window_start_ms=0)
    vad = build_ssh_silero_vad_provider(host)
    spans = vad(padded, 0, padded_dur)
    srt_text = transcriber(padded, [(s.start_ms, s.end_ms) for s in spans])
    srt_text, code_switch_audit = normalize_code_switch_surfaces(srt_text)
    support_srts = _load_independent_chat_support_srts(padded)
    human_entity_verifier = (
        build_human_text_entity_verifier(text_override_path, candidate_id=cid)
        if text_override_path is not None
        else None
    )
    audio_entity_verifier = None
    if host in {"localhost", "127.0.0.1"}:
        from src.autoslice.entity_audio_verifier import build_local_audio_entity_verifier

        audio_entity_verifier = build_local_audio_entity_verifier(
            source_media=padded,
            output_dir=out_root,
            recording_date=str(spec.get("date") or ""),
            source_duration_ms=padded_dur,
        )

    def verify_confusable_entity(request):
        if human_entity_verifier is not None:
            verdict = human_entity_verifier(request)
            if verdict is not None:
                return verdict
        if audio_entity_verifier is not None:
            return audio_entity_verifier(request)
        return None

    static_referent_groups = load_referent_groups(
        ROOT / "assets" / "lidousha" / "entity_confusables.json"
    )
    dynamic_groups = []
    topic_resolution_audit: dict[str, object] = {
        "schema_version": "topic-resolution.v1",
        "status": "NO_GRAPH",
        "recording_date": str(spec.get("date") or ""),
        "selected_topic_ids": [],
        "selected_work_ids": [],
        "scoped_entity_ids": [],
        "evidence": [],
    }
    if os.environ.get("LIDOUSHA_DISABLE_TOPIC_ENTITY_GRAPH") != "1":
        graph_path = Path(
            os.environ.get("LIDOUSHA_TOPIC_ENTITY_GRAPH")
            or ROOT / "assets" / "lidousha" / "topic_entity_graph.json"
        )
        if graph_path.is_file() and not graph_path.is_symlink():
            try:
                graph, graph_sha = load_topic_entity_graph(
                    graph_path,
                    expected_sha256=os.environ.get("LIDOUSHA_TOPIC_ENTITY_GRAPH_SHA256", ""),
                )
                if dt.datetime.now(dt.timezone.utc) <= dt.datetime.fromisoformat(
                    graph["expires_at"]
                ):
                    topic_evidence = [TopicEvidence("transcript", srt_text)]
                    selection_hook = str(spec.get("selection_hook") or "")
                    if selection_hook:
                        topic_evidence.append(TopicEvidence("selection_hook", selection_hook))
                    if authoritative_chat:
                        topic_evidence.append(
                            TopicEvidence(
                                "structured_chat",
                                "\n".join(item.text for item in authoritative_chat),
                            )
                        )
                    resolution = resolve_topic_context(
                        graph,
                        topic_evidence,
                        recording_date=str(spec.get("date") or ""),
                        graph_sha256=graph_sha,
                    )
                    topic_resolution_audit = resolution.as_dict()
                    dynamic_groups = dynamic_referent_groups(graph, resolution, srt_text)
                else:
                    topic_resolution_audit["status"] = "GRAPH_EXPIRED"
                    topic_resolution_audit["graph_sha256"] = graph_sha
            except (OSError, ValueError) as exc:
                topic_resolution_audit["status"] = "GRAPH_INVALID"
                topic_resolution_audit["error"] = f"{type(exc).__name__}: {exc}"
    topic_resolution_path = out_root / f"{cid}.topic-resolution.json"
    topic_resolution_path.write_text(
        json.dumps(topic_resolution_audit, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    referent_groups = merge_referent_groups(static_referent_groups, dynamic_groups)
    srt_text, chat_authority_audit = apply_authoritative_chat_evidence(
        srt_text,
        authoritative_chat,
        support_srt_texts=support_srts,
        referent_groups=referent_groups,
        entity_verifier=verify_confusable_entity,
    )
    handled_entity_cues = {
        int(index)
        for key in ("applied", "pending_text_overrides", "entity_repairs", "coreference_repairs")
        for row in chat_authority_audit.get(key) or []
        for index in (
            row.get("cue_indexes")
            or ([row.get("cue_index")] if row.get("cue_index") is not None else [])
        )
    }
    srt_text, transcript_entity_audit = apply_audio_entity_verification(
        srt_text,
        referent_groups=referent_groups,
        entity_verifier=verify_confusable_entity,
        excluded_cue_indexes=handled_entity_cues,
    )
    chat_authority_audit["transcript_entity_audit"] = transcript_entity_audit
    chat_authority_audit["code_switch_surface_audit"] = code_switch_audit
    chat_authority_audit.setdefault("entity_repairs", []).extend(
        transcript_entity_audit.get("repairs") or []
    )
    chat_authority_audit["post_transcript_entity_output_srt_sha256"] = hashlib.sha256(
        srt_text.encode("utf-8")
    ).hexdigest()
    chat_authority_path = out_root / f"{cid}.chat-authority.json"
    chat_authority_path.write_text(
        json.dumps(chat_authority_audit, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if (
        chat_authority_audit["status"]
        in {"FAILED", "ENTITY_VERDICT_REQUIRED", "SC_SENDER_VERDICT_REQUIRED"}
        or transcript_entity_audit["status"] == "ENTITY_VERDICT_REQUIRED"
    ):
        raise SystemExit(f"CHAT_AUTHORITY_FINALIZATION_FAILED: {chat_authority_path}")
    # Deterministic song-name pin (Ivan 2026-07-13): belt over the LLM prompt
    # context above.  A talk cue that signals a song mention (下一首/点歌/想唱/…)
    # gets its trailing mention span fuzzy-matched against machine-evidence
    # candidates (screen songlist + 点歌 + known-songs) and, on a strong match,
    # rewritten to 《title》.  A cue with no intent phrase or a weak match is
    # never touched — see src/autoslice/song_name_pin.py.
    if song_name_candidates:
        srt_text, song_name_pin_audit = pin_song_names_in_srt(
            srt_text, candidates=song_name_candidates
        )
        song_name_pin_path = out_root / f"{cid}.song-name-pin.json"
        song_name_pin_path.write_text(
            json.dumps(song_name_pin_audit, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if song_name_pin_audit.get("replacements"):
            print(
                f"{cid}: pinned {len(song_name_pin_audit['replacements'])} "
                "song name(s) from screen-songlist/点歌 evidence"
            )
    (out_root / "padded.fresh.srt").write_text(srt_text, encoding="utf-8")
    cues = [c for c in parse_srt_cues(srt_text) if c.text.strip()]
    if len(cues) < 3:
        raise SystemExit("FRESH_TRANSCRIPTION_TOO_SPARSE")

    # 4a. Sentence-snap the START (the clip must open on a sentence).
    first_piece = spec["pieces"][0]
    target_start_rel = spec.get("semantic_start_ms", first_piece["start_ms"]) - first_piece["start_ms"]
    snapped_start = snap_start_to_sentence([c.start_ms for c in cues], target_start_rel)
    final_start = max(0, (snapped_start if snapped_start is not None else target_start_rel) - LEAD_AIR_MS)

    # 4b. Sentence-snap the END; a run-on cue near the closure triggers a
    #     fine-grained micro re-transcription of the tail so the closure
    #     sentence gets its own boundary.
    last_piece = spec["pieces"][-1]
    target_rel = sum(durations[:-1]) + (spec["semantic_end_ms"] - last_piece["start_ms"])
    snapped = snap_end_to_sentence([c.end_ms for c in cues], target_rel)
    refinement_used = False
    if needs_tail_refinement(cues, snapped_end=snapped, target_ms=target_rel):
        refinement_used = True
        refine_start = max(0, target_rel - 20_000)
        refine_end = min(padded_dur, target_rel + 15_000)
        tail_clip = out_root / "tail_refine.mp4"
        run(_accurate_reencode_recut_command(source_video=padded, output_media=tail_clip, start_ms=refine_start, duration_ms=refine_end - refine_start))
        tail_srt = transcriber(tail_clip, None)
        (out_root / "tail_refine.fresh.srt").write_text(tail_srt, encoding="utf-8")
        fine = [c for c in parse_srt_cues(tail_srt) if c.text.strip()]
        fine_lifted = [
            type(c)(index=c.index, start_ms=c.start_ms + refine_start, end_ms=c.end_ms + refine_start, text=c.text)
            for c in fine
        ]
        snapped = snap_end_to_sentence([c.end_ms for c in fine_lifted], target_rel)
        if snapped is not None:
            # Splice: fine cues replace coarse cues inside the refined window.
            cues = [c for c in cues if c.end_ms <= refine_start or c.start_ms >= refine_end] + fine_lifted
            cues.sort(key=lambda c: c.start_ms)
    if snapped is None:
        raise SystemExit(
            f"NO_SENTENCE_BOUNDARY_NEAR_TARGET: target={target_rel}ms; nearest cue ends="
            f"{sorted((c.end_ms for c in cues), key=lambda e: abs(e - target_rel))[:3]}"
        )
    closure_cue = next(c for c in cues if c.end_ms == snapped)

    # 4c. Boundary self-repair loop (Ivan 2026-07-10): unattended means the
    # pipeline FIXES what its own deterministic auditors detect — there is no
    # deliver-and-ask-a-human quarantine state.  Pure computation (no ffmpeg,
    # no LLM): each pass re-audits; a flagged end moves FORWARD to the next
    # verifiably clean sentence end, a straddled opening moves back onto that
    # sentence's own start.  Unrepairable clips fail closed BEFORE any media
    # is cut.
    audit_path = out_root / f"{cid}.boundary_audit.json"
    boundary_repairs: list[dict] = []
    recorded_tail_clamps: set[tuple[int, int]] = set()
    while True:
        tail_adjustment = adaptive_tail_cut(
            spans,
            cues=cues,
            snapped_end_ms=snapped,
            padded_dur_ms=padded_dur,
        )
        final_end = tail_adjustment["final_end_ms"]
        clamp_key = (snapped, final_end)
        if tail_adjustment["reason"] and clamp_key not in recorded_tail_clamps:
            boundary_repairs.append(
                {
                    "reason": tail_adjustment["reason"],
                    "snapped_end_ms": snapped,
                    "nominal_final_end_ms": tail_adjustment["nominal_end_ms"],
                    "final_end_ms": final_end,
                    "next_speech_island_start_ms": tail_adjustment["next_speech_island_start_ms"],
                    "next_speech_island_guard_ms": tail_adjustment["next_speech_island_guard_ms"],
                }
            )
            recorded_tail_clamps.add(clamp_key)
        audit = boundary_audit(
            spans,
            start_ms=final_start,
            cut_ms=final_end,
            start_snapped=snapped_start is not None,
            end_snapped=True,
        )
        audit.update(
            {
                "semantic_start_target_rel_ms": target_start_rel,
                "snapped_sentence_start_ms": snapped_start,
                "final_start_ms": final_start,
                "opening_sentence": next((c.text for c in cues if c.start_ms == snapped_start), None),
                "semantic_target_rel_ms": target_rel,
                "snapped_sentence_end_ms": snapped,
                "final_end_ms": final_end,
                "closure_sentence": closure_cue.text,
                "tail_adjustment": tail_adjustment,
                "tail_refinement_used": refinement_used,
                "boundary_repair_search_origin_ms": target_rel,
                "boundary_repair_extend_cap_ms": boundary_repair_extend_cap_ms,
                "boundary_repair_max_end_ms": min(
                    padded_dur, target_rel + boundary_repair_extend_cap_ms
                ),
                "boundary_repairs": boundary_repairs,
            }
        )
        audit_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if audit["verdict"] != "ok_sentence_boundary_cut":
            raise SystemExit(f"BOUNDARY_AUDIT_FAILED: {json.dumps(audit, ensure_ascii=False)}")
        source_cues = [
            SourceCue(f"fresh_{i:04d}", max(c.start_ms, final_start), min(c.end_ms, final_end), c.text.strip(), "zh", "speech", 1.0)
            for i, c in enumerate(cues, start=1)
            if c.start_ms < final_end and c.end_ms > final_start
        ]
        sanitized, timing_qa = sanitize_cue_timing(source_cues, spans, window_start_ms=final_start, window_end_ms=final_end)
        red_flags = boundary_red_flags(
            audit=audit,
            cues=cues,
            sanitized=sanitized,
            final_start_ms=final_start,
            final_end_ms=final_end,
            snapped_end_ms=snapped,
            closure_text=closure_cue.text,
        )
        if not red_flags:
            break
        forward_extension_eligible = tail_requires_forward_extension(
            cues, spans, snapped_end_ms=snapped, cut_ms=final_end
        )
        audit["forward_extension_eligible"] = forward_extension_eligible
        repair: dict = {}
        flagged_repair_count = sum(1 for item in boundary_repairs if "flags" in item)
        if flagged_repair_count < MAX_BOUNDARY_REPAIRS:
            if "opens_mid_sentence" in red_flags:
                new_snap_start = repair_start_for_straddler(cues, final_start_ms=final_start)
                if new_snap_start is not None and new_snap_start != snapped_start:
                    repair["snapped_start_ms"] = new_snap_start
            if any(flag != "opens_mid_sentence" for flag in red_flags) and forward_extension_eligible:
                new_end = next_clean_closure(
                    cues,
                    spans,
                    after_ms=snapped,
                    padded_dur_ms=padded_dur,
                    cap_ms=boundary_repair_extend_cap_ms,
                    search_origin_ms=target_rel,
                )
                if new_end is not None:
                    repair["snapped_end_ms"] = new_end
        if not repair:
            audit["red_flags"] = red_flags
            retry_scope = "same_topic_continues" if forward_extension_eligible else "none"
            audit["boundary_context_retry_scope"] = retry_scope
            audit_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            raise SystemExit(
                f"BOUNDARY_UNREPAIRABLE: {','.join(red_flags)} after {flagged_repair_count} repair(s); "
                f"snapped={snapped}ms target={target_rel}ms "
                f"extend_cap={boundary_repair_extend_cap_ms}ms retry_scope={retry_scope}"
            )
        boundary_repairs.append({"flags": red_flags, **repair})
        if "snapped_start_ms" in repair:
            snapped_start = repair["snapped_start_ms"]
            final_start = max(0, snapped_start - LEAD_AIR_MS)
        if "snapped_end_ms" in repair:
            snapped = repair["snapped_end_ms"]
            closure_cue = next(c for c in cues if c.end_ms == snapped)
    audit["red_flags"] = []
    audit_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # 5. Final accurate cut + VAD-sanitized subtitles rebased to the cut.
    recut_dir = out_root / "replacement_recuts"
    recut_dir.mkdir(exist_ok=True)
    media_path = recut_dir / f"{cid}.recut.mp4"
    run(_accurate_reencode_recut_command(source_video=padded, output_media=media_path, start_ms=final_start, duration_ms=final_end - final_start))
    recut_provenance_path = media_path.with_suffix(".provenance.json")
    _write_json_atomic(
        recut_provenance_path,
        {
            "schema_version": RECUT_PROVENANCE_SCHEMA,
            "source_piece": piece_provenance_rows[0] if len(piece_provenance_rows) == 1 else None,
            "padded": json.loads(padded_provenance_path.read_text(encoding="utf-8")),
            "final_recut": {
                "source_path": str(padded.resolve()),
                "source_sha256": _sha256(padded),
                "start_ms": final_start,
                "end_ms": final_end,
                "absolute_source_start_ms": (
                    int(spec["pieces"][0]["start_ms"]) + final_start
                    if len(spec["pieces"]) == 1
                    else None
                ),
                "absolute_source_end_ms": (
                    int(spec["pieces"][0]["start_ms"]) + final_end
                    if len(spec["pieces"]) == 1
                    else None
                ),
                "output_path": str(media_path.resolve()),
                "output_sha256": _sha256(media_path),
            },
        },
    )
    subtitle_path = media_path.with_suffix(".srt")
    text_manifest_path: Path | None = None
    text_manifest: dict | None = None
    if text_override_path is not None:
        automatic_text_path = media_path.with_suffix(".automatic-text.srt")
        _write_source_range_srt(sanitized, final_start, final_end, automatic_text_path)
        text_manifest_path = media_path.with_suffix(".text-finalization.json")
        text_manifest = apply_text_override_document(
            automatic_text_path, text_override_path, subtitle_path, text_manifest_path
        )
    else:
        _write_source_range_srt(sanitized, final_start, final_end, subtitle_path)
    (recut_dir / f"{cid}.recut.timing_qa.json").write_text(
        json.dumps(timing_qa, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    speaker_manifest: dict | None = None
    speaker_review_srt: Path | None = None
    speaker_ass: Path | None = None
    speaker_manifest_path: Path | None = None
    if args.speaker_mode in {"required", "auto"}:
        speaker_review_srt = media_path.with_suffix(".speaker-final.srt")
        speaker_ass = media_path.with_suffix(".speaker-final.ass")
        speaker_manifest_path = media_path.with_suffix(".speaker-final.json")
        speaker_override_path = args.speaker_overrides or _resolved_optional_path(
            spec.get("speaker_overrides"), relative_to=args.spec.parent
        )
        source_session_anchor_path = (
            args.speaker_source_session_anchors
            or _resolved_optional_path(
                spec.get("speaker_source_session_anchors"), relative_to=args.spec.parent
            )
        )
        mixed_overlap_evidence_path = (
            args.speaker_mixed_overlap_evidence
            or _resolved_optional_path(
                spec.get("speaker_mixed_overlap_evidence"),
                relative_to=args.spec.parent,
            )
        )
        speaker_manifest = run_producer_speaker_finalization(
            speaker_mode=args.speaker_mode,
            host=host,
            candidate_id=cid,
            media_path=media_path,
            text_srt_path=subtitle_path,
            output_srt_path=speaker_review_srt,
            output_ass_path=speaker_ass,
            output_manifest_path=speaker_manifest_path,
            work_dir=recut_dir / f"{cid}.speaker-work",
            override_path=speaker_override_path,
            source_session_anchor_path=source_session_anchor_path,
            mixed_overlap_evidence_path=mixed_overlap_evidence_path,
            speaker_python=args.speaker_python,
            spec=spec,
            spec_parent=args.spec.parent,
            final_source_start_ms=(
                int(spec["pieces"][0]["start_ms"]) + final_start
                if len(spec.get("pieces") or []) == 1
                else None
            ),
            final_source_end_ms=(
                int(spec["pieces"][0]["start_ms"]) + final_end
                if len(spec.get("pieces") or []) == 1
                else None
            ),
        )

    # Generative correction, a human override, timing sanitation, or speaker
    # rendering must never silently undo a structured-source lock.  Recheck the
    # exact text against both final subtitle surfaces and bind their hashes into
    # the append-only authority audit before the burn.
    final_text = subtitle_path.read_text(encoding="utf-8", errors="replace")
    final_speaker_text = (
        speaker_review_srt.read_text(encoding="utf-8", errors="replace")
        if speaker_review_srt is not None and speaker_review_srt.is_file()
        else final_text
    )
    pending_override_ok = reconcile_pending_text_overrides(
        chat_authority_audit,
        text_manifest,
        delivery_start_ms=final_start,
    )
    final_authority_ok = pending_override_ok and verify_chat_authority_final_surfaces(
        chat_authority_audit,
        final_text_srt=final_text,
        final_speaker_srt=final_speaker_text,
        delivery_start_ms=final_start,
        delivery_end_ms=final_end,
    )
    chat_authority_audit.update(
        {
            "final_status": "FINAL_ARTIFACTS_VERIFIED" if final_authority_ok else "FINAL_ARTIFACTS_FAILED",
            "final_text_srt_path": str(subtitle_path),
            "final_text_srt_sha256": hashlib.sha256(final_text.encode("utf-8")).hexdigest(),
            "final_speaker_srt_path": str(speaker_review_srt) if speaker_review_srt is not None else None,
            "final_speaker_srt_sha256": hashlib.sha256(final_speaker_text.encode("utf-8")).hexdigest(),
            "speaker_ass_path": str(speaker_ass) if speaker_ass is not None else None,
            "speaker_ass_sha256": _sha256(speaker_ass) if speaker_ass is not None else None,
            "speaker_manifest_sha256": _sha256(speaker_manifest_path) if speaker_manifest_path is not None else None,
        }
    )
    chat_authority_path.write_text(
        json.dumps(chat_authority_audit, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if not final_authority_ok:
        raise SystemExit(f"CHAT_AUTHORITY_FINAL_ARTIFACT_FAILED: {chat_authority_path}")

    subtitle_regression_audit_path: Path | None = None
    subtitle_regression_audit: dict | None = None
    if subtitle_regression_path is not None:
        subtitle_regression_audit = verify_subtitle_regression_surfaces(
            subtitle_regression_path,
            candidate_id=cid,
            final_text_srt=final_text,
            final_speaker_srt=final_speaker_text,
        )
        subtitle_regression_audit_path = recut_dir / f"{cid}.subtitle-regression.json"
        subtitle_regression_audit_path.write_text(
            json.dumps(subtitle_regression_audit, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if subtitle_regression_audit["status"] != "PASS":
            raise SystemExit(
                f"SUBTITLE_REGRESSION_FAILED: {subtitle_regression_audit_path}"
            )

    record: dict = {
        "status": "MATERIALIZED",
        "media_path": str(media_path),
        "subtitle_path": str(subtitle_path),
        "subtitle_source": f"{args.substrate}+{args.correct}+pronoun+text_final",
        "start_ms": 0,
        "end_ms": final_end - final_start,
        "duration_ms": final_end - final_start,
        "artifact_hashes": {
            "video_sha256": "sha256:" + _sha256(media_path),
            "subtitle_sha256": "sha256:" + _sha256(subtitle_path),
            **({"ass_sha256": "sha256:" + _sha256(speaker_ass)} if speaker_ass is not None else {}),
            **({"speaker_review_srt_sha256": "sha256:" + _sha256(speaker_review_srt)} if speaker_review_srt is not None else {}),
            "chat_authority_audit_sha256": "sha256:" + _sha256(chat_authority_path),
            **(
                {
                    "subtitle_regression_audit_sha256": "sha256:"
                    + _sha256(subtitle_regression_audit_path)
                }
                if subtitle_regression_audit_path is not None
                else {}
            ),
        },
        "chat_authority_audit_path": str(chat_authority_path),
        "subtitle_regression_audit_path": (
            str(subtitle_regression_audit_path)
            if subtitle_regression_audit_path is not None
            else None
        ),
        "subtitle_regression": subtitle_regression_audit,
        "text_finalization_manifest_path": str(text_manifest_path) if text_manifest_path is not None else None,
        "speaker_mode": args.speaker_mode,
        "speaker_review_srt_path": str(speaker_review_srt) if speaker_review_srt is not None else None,
        "subtitle_ass_path": str(speaker_ass) if speaker_ass is not None else None,
        "subtitle_style": SPEAKER_SUBTITLE_STYLE_ID if speaker_ass is not None else "lidousha-final-sapphire72",
        "speaker_finalization_manifest_path": str(speaker_manifest_path) if speaker_manifest_path is not None else None,
        "speaker_finalization_manifest_sha256": ("sha256:" + _sha256(speaker_manifest_path)) if speaker_manifest_path is not None else None,
        "speaker_finalization": speaker_manifest,
        "subtitle_timing_qa": timing_qa,
        "boundary_audit": audit,
    }
    record = _burn_preview_subtitles(record, run_ffmpeg=True, branding_intro=branding_intro)
    if not isinstance(record.get("burned_preview"), dict) or record["burned_preview"].get("status") != "BURNED":
        raise SystemExit(f"FINAL_SUBTITLE_BURN_FAILED: {record.get('burned_preview')}")
    burned = _validated_burned_artifact(record)
    chat_authority_audit["burn_binding"] = {
        "burned_media_path": str(burned),
        "burned_media_sha256": _sha256(burned),
        "ass_path": str(speaker_ass) if speaker_ass is not None else None,
        "ass_sha256": _sha256(speaker_ass) if speaker_ass is not None else None,
    }
    chat_authority_path.write_text(
        json.dumps(chat_authority_audit, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    record["artifact_hashes"]["chat_authority_audit_sha256"] = "sha256:" + _sha256(chat_authority_path)

    # 6. Title (Ivan-given verbatim, else style-asset LLM) + cover + delivery.
    given_title = spec.get("given_title")
    # Title LLM only when no manual title (iron rule: manual titles pass through
    # untouched). Cover art-direction LLM ALWAYS runs (Ivan 2026-07-04): even with
    # a hand-given title the cover still benefits from persona-fit expression /
    # layout / background; it is fail-open, so it never blocks.
    # Per-stage CPA chains (2026-07-10, Ivan): title is a single brand-critical
    # short call → gpt-5.6-sol at high effort; art direction is a structured
    # pick with a known good shape, deterministic fallback and judge guardrails
    # → gpt-5.6-luna at medium (the doc-exact luna lane).  Both fall back
    # 5.5 → 5.4.
    title_llm = None
    if not given_title:
        title_llm = build_llm_call(
            LlmConfig(transport="command", command_template="bash scripts/llm_via_cpa.sh {prompt_file} {completion_file} 'gpt-5.6-sol gpt-5.5 gpt-5.4' high", timeout_seconds=180.0)
        )
    art_direction_llm = None if args.reuse_cover else build_llm_call(
        LlmConfig(transport="command", command_template="bash scripts/llm_via_cpa.sh {prompt_file} {completion_file} 'gpt-5.6-luna gpt-5.5 gpt-5.4' medium", timeout_seconds=180.0)
    )
    final_title_cues = [
        SourceCue(
            f"text_final_{index:04d}", cue.start_ms, cue.end_ms, cue.text.strip(),
            "zh", "speech", 1.0,
        )
        for index, cue in enumerate(parse_srt_cues(subtitle_path.read_text(encoding="utf-8")), start=1)
        if cue.text.strip()
    ]
    record = _stage_publish_draft(
        record,
        candidate_id=cid,
        title=given_title or cid,
        cues=final_title_cues,
        run_ffmpeg=True,
        title_llm_call=title_llm,
        art_direction_llm_call=art_direction_llm,
        skip_cover=args.reuse_cover,
        selection_hook=str(spec.get("selection_hook") or ""),
    )
    staging = record.get("publish_staging") or {}
    record_path = recut_dir / f"{cid}.record.json"
    with record_path.open("w", encoding="utf-8") as handle:
        json.dump(record, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")

    if str(staging.get("title_authority_status") or "").startswith("UNRESOLVED"):
        # The standalone producer is also a supported entry point.  Never
        # materialize delivery bytes and rely on the unattended runner to
        # notice and delete them afterward.  The publish/record evidence above
        # remains available for classification and bounded retry.
        raise SystemExit(
            f"TITLE_AUTHORITY_UNRESOLVED: {staging.get('title_authority_error') or 'unknown'}"
        )

    delivery = ROOT / "lidousha" / spec["date"]
    delivery.mkdir(parents=True, exist_ok=True)
    name = spec.get("delivery_name") or cid
    # Old sapphire renders may coexist in replacement_recuts; copy only the
    # exact hash-bound speaker burn made by this run.
    burned = _validated_burned_artifact(record)
    run(["cp", str(burned), str(delivery / f"{name}.mp4")])
    run(["cp", str(subtitle_path), str(delivery / f"{name}.srt")])
    for source, suffix in (
        (speaker_review_srt, ".speaker.srt"),
        (speaker_ass, ".speaker.ass"),
        (speaker_manifest_path, ".speaker.json"),
        (chat_authority_path, ".chat-authority.json"),
        (subtitle_regression_audit_path, ".subtitle-regression.json"),
        (text_manifest_path, ".text-finalization.json"),
        (record_path, ".record.json"),
    ):
        if source is not None and source.is_file():
            run(["cp", str(source), str(delivery / f"{name}{suffix}")])
    cover = staging.get("cover_path")
    if cover and Path(cover).is_file():
        run(["cp", str(cover), str(delivery / f"{name}.cover.png")])

    print(json.dumps(
        {
            "candidate_id": cid,
            "final_end_ms": final_end,
            "closure_sentence": closure_cue.text,
            "boundary_verdict": audit["verdict"],
            "red_flags": red_flags,
            "boundary_repairs": boundary_repairs,
            "timing_qa": timing_qa.get("counts"),
            "cover_status": staging.get("cover_status"),
            "title": staging.get("title"),
            "delivery": str(delivery / f"{name}.mp4"),
            "subtitle": str(delivery / f"{name}.srt"),
            "speaker_subtitle": str(delivery / f"{name}.speaker.srt") if speaker_review_srt else None,
            "speaker_ass": str(delivery / f"{name}.speaker.ass") if speaker_ass else None,
            "speaker_status": speaker_manifest.get("status") if speaker_manifest else "OFF",
            "subtitle_regression_status": (
                subtitle_regression_audit.get("status")
                if subtitle_regression_audit is not None
                else "NOT_CONFIGURED"
            ),
        },
        ensure_ascii=False,
        indent=2,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
