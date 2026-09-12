"""Candidate-free MOSS/MAI observations for an already-scoped audio doubt.

This module is deliberately not a subtitle producer.  It consumes the same
physical witness geometry used by the existing blind-pinyin route, crops only
that target (with the existing bounded pad), and returns provider-native text
as EVIDENCE_ONLY.  The result never chooses a candidate or mutates text.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import stat
import subprocess
from typing import Any, Mapping

from src.autoslice.acoustic_witness_adjudication import WITNESS_REQUEST_SCHEMA
from src.autoslice.acoustic_witness_protocol import BLIND_PINYIN_PROTOCOL
from src.autoslice.diarized_transcription import DiarizedTranscriptionError
from src.autoslice.entity_audio_verifier import _crop_black_frame_audio, _prepare_audio_span
from src.autoslice.subtitle_audio_evidence import observe_secondary
from src.autoslice.supplement_audio_budget import BudgetExceeded, ensure_budget


SCHEMA_VERSION = "secondary-audio-witness-evidence.v1"
_FORBIDDEN_TEXT_KEYS = frozenset(
    {
        "candidate_entities",
        "current_cue",
        "proposed_cue",
        "matched_audio_text",
        "context_before",
        "context_after",
        "suspect",
        "replacement",
        "exact_text",
        "structured_chat_canonical",
    }
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _valid_request(request: Mapping[str, Any]) -> bool:
    if (
        request.get("schema_version") != WITNESS_REQUEST_SCHEMA
        or request.get("witness_protocol") != BLIND_PINYIN_PROTOCOL
        or any(key in request for key in _FORBIDDEN_TEXT_KEYS)
    ):
        return False
    request_sha = request.get("request_sha256")
    if not isinstance(request_sha, str) or len(request_sha) != 64:
        return False
    unsigned = dict(request)
    unsigned.pop("request_sha256", None)
    return _json_sha256(unsigned) == request_sha


def _extract_mp3(source: Path, output: Path) -> tuple[bool, str]:
    completed = subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "libmp3lame",
            "-b:a",
            "64k",
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=120,
    )
    if completed.returncode != 0 or not output.is_file() or output.is_symlink():
        return False, "SECONDARY_AUDIO_MP3_EXTRACTION_FAILED"
    info = output.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size <= 0:
        return False, "SECONDARY_AUDIO_MP3_EXTRACTION_FAILED"
    return True, ""


def _shift_segments(segments: object, *, offset_ms: int) -> list[dict[str, Any]]:
    shifted: list[dict[str, Any]] = []
    if not isinstance(segments, list):
        return shifted
    for raw in segments:
        if not isinstance(raw, Mapping):
            continue
        row = dict(raw)
        try:
            row["start_ms"] = int(raw["start_ms"]) + offset_ms
            row["end_ms"] = int(raw["end_ms"]) + offset_ms
        except (KeyError, TypeError, ValueError):
            continue
        words = []
        for word_raw in raw.get("words") or []:
            if not isinstance(word_raw, Mapping):
                continue
            word = dict(word_raw)
            try:
                word["start_ms"] = int(word_raw["start_ms"]) + offset_ms
                word["end_ms"] = int(word_raw["end_ms"]) + offset_ms
            except (KeyError, TypeError, ValueError):
                continue
            words.append(word)
        if "words" in raw:
            row["words"] = words
        shifted.append(row)
    return shifted


def build_secondary_audio_observer(
    *,
    source_media: Path,
    output_dir: Path,
    source_duration_ms: int,
    provider: str,
):
    """Return ``observe(witness_request)`` for one explicit secondary provider.

    ``provider`` is never selected from model output and never falls back to a
    different secondary provider.  A caller can construct another observer if
    it intentionally wants another independent source; both share the same
    source-scoped supplementary-audio budget in this process.
    """

    if provider not in {"mai", "moss"}:
        raise ValueError("secondary audio provider must be mai or moss")
    source_media = source_media.resolve(strict=True)
    output_dir = output_dir.resolve()
    info = source_media.lstat()
    if not stat.S_ISREG(info.st_mode) or source_media.is_symlink():
        raise ValueError("secondary audio source must be a regular non-symlink file")
    source_sha = _sha256(source_media)
    output_dir.mkdir(parents=True, exist_ok=True)
    if output_dir.is_symlink() or not output_dir.is_dir():
        raise ValueError("secondary audio output directory is invalid")
    ensure_budget(source_media)

    def observe(request: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(request, Mapping) or not _valid_request(request):
            return {
                "schema_version": SCHEMA_VERSION,
                "status": "INVALID",
                "reason_code": "SECONDARY_AUDIO_REQUEST_INVALID",
                "authority": "EVIDENCE_ONLY",
                "mutation_authorized": False,
            }
        raw_offset = request.get("source_media_timeline_offset_ms")
        if isinstance(raw_offset, bool) or not isinstance(raw_offset, int) or raw_offset < 0:
            return {
                "schema_version": SCHEMA_VERSION,
                "status": "INVALID",
                "reason_code": "SECONDARY_AUDIO_TIMELINE_INVALID",
                "authority": "EVIDENCE_ONLY",
                "mutation_authorized": False,
            }
        span = _prepare_audio_span(
            request=request,
            context_mode=True,
            source_media_timeline_offset_ms=raw_offset,
            source_duration_ms=source_duration_ms,
            witness_mode=True,
        )
        if span is None:
            return {
                "schema_version": SCHEMA_VERSION,
                "status": "INVALID",
                "reason_code": "SECONDARY_AUDIO_SPAN_INVALID",
                "authority": "EVIDENCE_ONLY",
                "mutation_authorized": False,
            }
        request_sha = str(request["request_sha256"])
        job = output_dir / "secondary_audio" / provider / request_sha[:20]
        job.mkdir(parents=True, exist_ok=True)
        crop = job / "input.mp4"
        ok, detail = _crop_black_frame_audio(
            source_media=source_media,
            audio_path=crop,
            start_ms=span.crop_start_ms,
            end_ms=span.crop_end_ms,
        )
        if not ok:
            return {
                "schema_version": SCHEMA_VERSION,
                "status": "UNAVAILABLE",
                "reason_code": "SECONDARY_AUDIO_CROP_FAILED",
                "detail": detail[-300:],
                "authority": "EVIDENCE_ONLY",
                "mutation_authorized": False,
            }
        audio = job / "input.mp3"
        ok, reason = _extract_mp3(crop, audio)
        if not ok:
            return {
                "schema_version": SCHEMA_VERSION,
                "status": "UNAVAILABLE",
                "reason_code": reason,
                "authority": "EVIDENCE_ONLY",
                "mutation_authorized": False,
            }
        audio_bytes = audio.read_bytes()
        try:
            evidence = observe_secondary(
                audio_bytes,
                media_path=crop,
                provider=provider,
                duration_ms=span.crop_end_ms - span.crop_start_ms,
                supplement_source=source_media,
                crop_start_ms=span.crop_start_ms,
                crop_end_ms=span.crop_end_ms,
            )
        except (DiarizedTranscriptionError, BudgetExceeded) as exc:
            return {
                "schema_version": SCHEMA_VERSION,
                "status": "UNAVAILABLE",
                "reason_code": getattr(exc, "reason_code", type(exc).__name__),
                "authority": "EVIDENCE_ONLY",
                "mutation_authorized": False,
            }
        shifted = _shift_segments(evidence.get("native_segments"), offset_ms=span.crop_start_ms)
        target_rows = [
            row for row in shifted
            if max(row["start_ms"], span.target_start_ms)
            < min(row["end_ms"], span.target_end_ms)
        ]
        result = {
            "schema_version": SCHEMA_VERSION,
            "status": "OBSERVED",
            "authority": "EVIDENCE_ONLY",
            "mutation_authorized": False,
            "candidate_exposure": "none",
            "witness_request_sha256": request_sha,
            "witness_protocol": BLIND_PINYIN_PROTOCOL,
            "provider": evidence.get("provider"),
            "model": evidence.get("model"),
            "source_media_sha256": source_sha,
            "source_crop_sha256": _sha256(crop),
            "secondary_audio_sha256": hashlib.sha256(audio_bytes).hexdigest(),
            "provider_input_audio_sha256": evidence.get("input_audio_sha256"),
            "provider_response_sha256": evidence.get("response_sha256"),
            "timeline_binding": dict(span.timeline_binding),
            "native_segments": shifted,
            "target_overlap_segments": target_rows,
            "served_from_cache": bool(evidence.get("served_from_cache")),
        }
        receipt = job / "secondary-evidence.json"
        receipt.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        result["receipt_path"] = str(receipt)
        result["receipt_sha256"] = _sha256(receipt)
        return result

    return observe
