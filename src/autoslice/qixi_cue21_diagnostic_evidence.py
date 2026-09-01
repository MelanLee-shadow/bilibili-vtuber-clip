"""Strictly validate the retained, diagnostic-only Qixi cue-21 evidence."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Mapping


SCHEMA = "qixi-cue21-canonical-provider-evidence.v1"
PROMPT = "这是一次独立的盲听。请只根据所附音频转写，不要搜索、不要使用外部知识，也不要按任何可能的作品名或候选词猜测。音频来自原录像 415.110–427.510 秒；目标语句是其中 5.000–7.400 秒。请给出逐字目标语句，并同时给出前后完整语境。只输出 JSON：context_transcript, target_transcript, uncertainties, confidence_0_to_1。"
EXTRACTION_COMMAND = "ffmpeg -ss 415.110 -to 427.510 -i SOURCE -vn -ac 1 -ar 16000 -c:a pcm_s16le -f wav OUTPUT"
FFMPEG_VERSION = "7.1.5-0+deb13u1"


class QixiCue21DiagnosticEvidenceError(ValueError):
    """A retained diagnostic artifact cannot be replayed safely."""


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _contained(root: Path, relative: object, *, label: str) -> Path:
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise QixiCue21DiagnosticEvidenceError(f"{label} path is invalid")
    if root.is_symlink() or not root.is_dir():
        raise QixiCue21DiagnosticEvidenceError("evidence root must be a non-symlink directory")
    candidate = root / relative
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root.resolve(strict=True))
    except (OSError, ValueError):
        raise QixiCue21DiagnosticEvidenceError(f"{label} escapes evidence root") from None
    current = candidate
    while current != root:
        if current.is_symlink():
            raise QixiCue21DiagnosticEvidenceError(f"{label} must not traverse a symlink")
        current = current.parent
    if not resolved.is_file() or resolved.is_symlink():
        raise QixiCue21DiagnosticEvidenceError(f"{label} must be a regular file")
    return resolved


def _artifact(root: Path, value: object, *, label: str) -> Path:
    if not isinstance(value, Mapping) or set(value) != {"path", "sha256", "bytes"}:
        raise QixiCue21DiagnosticEvidenceError(f"{label} shape is invalid")
    path = _contained(root, value.get("path"), label=label)
    if not isinstance(value.get("bytes"), int) or value["bytes"] != path.stat().st_size:
        raise QixiCue21DiagnosticEvidenceError(f"{label} bytes drift")
    if not isinstance(value.get("sha256"), str) or value["sha256"] != _sha(path):
        raise QixiCue21DiagnosticEvidenceError(f"{label} sha256 drift")
    return path


def validate_qixi_cue21_diagnostic_evidence(
    descriptor: object,
    *,
    evidence_root: Path,
    candidate_id: str,
    source_basename: str,
    source_sha256: str,
    cue_start_ms: int,
    cue_end_ms: int,
    absolute_source_start_ms: int,
) -> None:
    """Rehash every retained artifact without deriving any release text."""

    if not isinstance(descriptor, Mapping) or set(descriptor) != {"path", "sha256", "reason"}:
        raise QixiCue21DiagnosticEvidenceError("diagnostic evidence descriptor is invalid")
    if not isinstance(descriptor.get("reason"), str) or not descriptor["reason"].strip():
        raise QixiCue21DiagnosticEvidenceError("diagnostic evidence reason is invalid")
    if candidate_id != "auto_113022_354_496":
        raise QixiCue21DiagnosticEvidenceError("diagnostic evidence candidate binding drift")
    path = _contained(evidence_root, descriptor.get("path"), label="diagnostic evidence")
    if not isinstance(descriptor.get("sha256"), str) or descriptor["sha256"] != _sha(path):
        raise QixiCue21DiagnosticEvidenceError("diagnostic evidence sha256 drift")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise QixiCue21DiagnosticEvidenceError("diagnostic evidence JSON is invalid") from exc
    expected = {"schema_version", "status", "source_recording", "extraction", "prompt", "prompt_sha256", "provider_requests", "raw_responses", "release_decision"}
    if not isinstance(document, Mapping) or set(document) != expected or document.get("schema_version") != SCHEMA or document.get("status") != "UNRESOLVED_NO_RELEASE_TEXT" or document.get("release_decision") != "NO_INTERSECTION; REVIEWER_EXACT_TEXT_REQUIRED":
        raise QixiCue21DiagnosticEvidenceError("diagnostic evidence schema is invalid")
    source = document.get("source_recording")
    expected_source = {"basename": source_basename, "sha256": source_sha256, "bytes": 1168095423, "absolute_context_start_ms": 415110, "absolute_context_end_ms": 427510, "target_start_ms": absolute_source_start_ms + cue_start_ms, "target_end_ms": absolute_source_start_ms + cue_end_ms}
    if source != expected_source:
        raise QixiCue21DiagnosticEvidenceError("diagnostic evidence source binding drift")
    if document.get("prompt") != PROMPT or document.get("prompt_sha256") != hashlib.sha256(PROMPT.encode()).hexdigest():
        raise QixiCue21DiagnosticEvidenceError("diagnostic evidence prompt drift")
    extraction = document.get("extraction")
    if (
        not isinstance(extraction, Mapping)
        or set(extraction) != {"command", "ffmpeg_version", "context_wav", "target_wav", "target_mp3"}
        or extraction.get("command") != EXTRACTION_COMMAND
        or extraction.get("ffmpeg_version") != FFMPEG_VERSION
    ):
        raise QixiCue21DiagnosticEvidenceError("diagnostic evidence extraction shape is invalid")
    for key in ("context_wav", "target_wav", "target_mp3"):
        _artifact(path.parent, extraction.get(key), label=key)
    requests = document.get("provider_requests")
    if requests != {"policy": "ONE_REQUEST_PER_MODEL_NO_RETRY", "models": ["gemini-3.7-flash", "gemini-3.6-flash"]}:
        raise QixiCue21DiagnosticEvidenceError("diagnostic evidence request policy drift")
    responses = document.get("raw_responses")
    if not isinstance(responses, list) or len(responses) != 2:
        raise QixiCue21DiagnosticEvidenceError("diagnostic evidence raw responses are invalid")
    for response, model in zip(responses, requests["models"], strict=True):
        if not isinstance(response, Mapping) or set(response) != {"model", "path", "sha256", "bytes", "target_transcript", "confidence_0_to_1"} or response.get("model") != model:
            raise QixiCue21DiagnosticEvidenceError("diagnostic evidence raw response shape is invalid")
        raw = _artifact(path.parent, {key: response[key] for key in ("path", "sha256", "bytes")}, label=model)
        try:
            payload = json.loads(raw.read_text(encoding="utf-8"))
            if payload["modelVersion"] != model:
                raise ValueError("raw modelVersion drift")
            text = payload["candidates"][0]["content"]["parts"][0]["text"]
            parsed = json.loads(text)
        except (KeyError, TypeError, ValueError, UnicodeDecodeError) as exc:
            raise QixiCue21DiagnosticEvidenceError("diagnostic evidence raw response is unparsable") from exc
        if parsed.get("target_transcript") != response.get("target_transcript") or parsed.get("confidence_0_to_1") != response.get("confidence_0_to_1"):
            raise QixiCue21DiagnosticEvidenceError("diagnostic evidence raw response drift")
