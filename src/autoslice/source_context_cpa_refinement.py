"""Text-only CPA refinement for ordinary source-context jobs.

The source-context lane has two deliberately separate transports.  An
explicit AGY caller may provide an audio observation; the ordinary talk
default is a numbered text correction over the existing draft.  This module
keeps that distinction visible in the result and validates every byte-bound
input before the executor can mark the context ready.
"""

from __future__ import annotations

from src.autoslice.structured_output import call_json

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping

from src.autoslice.jingting_chunker import parse_srt_cues
from src.autoslice.llm_client import cached_completion, completion_identity


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    from .evidence_identity import file_sha256
    return file_sha256(path)


def _timing_grid_sha256(cues: list[Any]) -> str:
    payload = [
        {"start_ms": int(cue.start_ms), "end_ms": int(cue.end_ms)}
        for cue in cues
    ]
    return _sha256_bytes(
        (json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()
    )


def _model_name(call: Any) -> str | None:
    identity = getattr(call, "cpa_cache_identity", None)
    if isinstance(identity, Mapping):
        models = identity.get("models")
        if isinstance(models, (list, tuple)) and models and isinstance(models[0], str):
            return models[0]
    return None


def _write_private(path: Path, payload: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = payload.encode("utf-8") if isinstance(payload, str) else payload
    if path.exists() and path.read_bytes() != raw:
        raise ValueError(f"artifact path already contains different bytes: {path}")
    if not path.exists():
        path.write_bytes(raw)


def _cache_completion(media_path: Path, cache: Any) -> str | None:
    """Recover the original completion after a validated CPA cache hit."""

    try:
        audit_path = media_path.with_suffix(".transcription-reuse.json")
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
        row = audit.get("cpa")
        if not isinstance(row, Mapping) or not row.get("served_from_cache"):
            return None
        key = row.get("cache_key")
        if not isinstance(key, str) or not key:
            return None
        entry_path = Path(cache.root) / "cpa" / f"{key}.json"
        entry = json.loads(entry_path.read_text(encoding="utf-8"))
        completion = entry.get("payload")
        if not isinstance(completion, str):
            return None
        return cached_completion(completion, entry.get('identity', {}).get('model_identity') or {})
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None


def _cache_row(media_path: Path) -> Mapping[str, object] | None:
    try:
        audit = json.loads(media_path.with_suffix(".transcription-reuse.json").read_text(encoding="utf-8"))
        row = audit.get("cpa")
        return row if isinstance(row, Mapping) else None
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None


class _RecordingResume:
    """Small adapter that preserves the existing cache/review contract."""

    def __init__(self, cache: Any, capture: dict[str, Any], llm_call: Any):
        self._cache = cache
        self._capture = capture
        self._llm_call = llm_call

    def review(self, *, prompt: str, source_srt: str, cue_count: int, llm_call: Any, validate: Any):
        self._capture["cache_prompt"] = prompt
        return self._cache.review(
            prompt=prompt,
            source_srt=source_srt,
            cue_count=cue_count,
            llm_call=self._llm_call,
            validate=validate,
        )


def _completion_deleted_indexes(completion: str) -> tuple[int, ...]:
    from src.autoslice.llm_client import extract_json_object

    payload = extract_json_object(completion)
    rows = payload.get("cues")
    if not isinstance(rows, list):
        raise ValueError("CPA completion has no cues list")
    deleted: list[int] = []
    for row in rows:
        if not isinstance(row, Mapping) or type(row.get("n")) is not int or not isinstance(row.get("text"), str):
            raise ValueError("CPA completion cue row is malformed")
        if not row["text"].strip():
            deleted.append(row["n"])
    return tuple(sorted(deleted))


def run_source_context_cpa(
    media_path: Path,
    draft_srt_path: Path,
    output_srt_path: Path,
    *,
    topic_entity_context: str = "",
    screen_text_lines=None,
    danmaku_lines=(),
):
    """Run the normal text-only source-context correction.

    The function intentionally returns the executor's existing
    ``AgyExecutionResult`` shape for compatibility.  It never sends audio to
    CPA and never edits the draft or media files.
    """

    from src.autoslice.full_session_transcription import (
        _build_full_session_cpa_llm_call,
        _cpa_correct_draft_cues,
    )
    from src.autoslice.source_context_executor import AgyExecutionResult, AgyRunnerError

    if not media_path.is_file() or not draft_srt_path.is_file():
        raise AgyRunnerError("CPA_SOURCE_CONTEXT_INPUT_MISSING", "CPA source-context input is missing")
    media_sha256 = _sha256_file(media_path)
    draft_text = draft_srt_path.read_text(encoding="utf-8-sig")
    draft_sha256 = _sha256_file(draft_srt_path)
    output_srt_path.parent.mkdir(parents=True, exist_ok=True)

    host = os.environ.get("AUTOSLICE_FINAL_REVIEW_SSH_HOST", "localhost")
    try:
        cpa_call = _build_full_session_cpa_llm_call(host=host, effort="low")
    except Exception as exc:
        raise AgyRunnerError("CPA_CORRECTION_UNAVAILABLE", "CPA source-context binding could not be built") from exc

    identity = getattr(cpa_call, "cpa_cache_identity", None)
    if not isinstance(identity, Mapping):
        identity = None
    model = _model_name(cpa_call)
    capture: dict[str, Any] = {"completion": None, "provider_prompt": None}
    request_path = output_srt_path.with_suffix(".cpa.request.json")
    prompt_path = output_srt_path.with_suffix(".cpa.prompt.txt")
    completion_path = output_srt_path.with_suffix(".cpa.response.txt")

    def record_prompt(prompt: str) -> str:
        sent_prompt = prompt
        capture["provider_prompt"] = sent_prompt
        provider_prompt_sha256 = _sha256_bytes(sent_prompt.encode("utf-8"))
        request_id = "cpa-source-context-" + _sha256_bytes(
            json.dumps(
                {
                    "source_media_sha256": media_sha256,
                    "draft_srt_sha256": draft_sha256,
                    "prompt_sha256": provider_prompt_sha256,
                    "model": model,
                    "effort": "low",
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        )[:32]
        capture["request_id"] = request_id
        _write_private(prompt_path, sent_prompt)
        _write_private(
            request_path,
            json.dumps(
                {
                    "schema_version": "source-context-cpa-request.v1",
                    "provider": "cpa",
                    "request_id": request_id,
                    "request_id_kind": "content_bound_hash",
                    "model": model,
                    "effort": "low",
                    "source_media_sha256": media_sha256,
                    "draft_srt_sha256": draft_sha256,
                    "prompt_sha256": provider_prompt_sha256,
                    "prompt_path": str(prompt_path),
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )
        return sent_prompt

    def recording_call(prompt: str) -> str:
        sent_prompt = record_prompt(prompt)
        completion = call_json(cpa_call, sent_prompt)
        capture["completion"] = completion
        return completion

    if identity is not None:
        recording_call.cpa_cache_identity = dict(identity)  # type: ignore[attr-defined]
    recording_call.provider = "cpa"  # type: ignore[attr-defined]
    from .completed_review_checkpoint import inherit_ruling_context
    inherit_ruling_context(recording_call, cpa_call)

    resume = None
    cache = None
    try:
        # This is local cache identity material only; it is not an ASR call.
        from scripts.free_asr_client import extract_audio_mp3
        from src.autoslice.transcription_stage_cache import TranscriptionStageCache

        cache = TranscriptionStageCache(media_path, extract_audio_mp3(media_path))
        resume = _RecordingResume(cache, capture, recording_call)
    except Exception:
        # Invalid/test media has no cacheable audio.  The text-only CPA path
        # remains usable and will fail closed if the actual provider is absent.
        cache = None
        resume = None

    try:
        refined_text = _cpa_correct_draft_cues(
            draft_text,
            danmaku_lines=danmaku_lines,
            cpa_llm_call=recording_call,
            screen_text_lines=screen_text_lines,
            topic_entity_context=topic_entity_context,
            song_name_candidates=(),
            resume=resume,
        )
        completion = capture.get("completion")
        if not isinstance(completion, str):
            completion = _cache_completion(media_path, cache) if cache is not None else None
        if not isinstance(completion, str) or not completion.strip():
            raise ValueError("CPA completion was not recoverable")
        capture["completion"] = completion
        _write_private(completion_path, completion)
        deleted_cue_indexes = _completion_deleted_indexes(completion)
        output_srt_path.write_text(refined_text, encoding="utf-8")
    except AgyRunnerError:
        completion = capture.get("completion")
        if isinstance(completion, str) and completion:
            _write_private(completion_path, completion)
        raise
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        completion = capture.get("completion")
        if isinstance(completion, str) and completion:
            _write_private(completion_path, completion)
        raise AgyRunnerError("CPA_CORRECTION_INVALID_OUTPUT", "CPA source-context correction was not usable") from exc
    except Exception as exc:
        completion = capture.get("completion")
        if isinstance(completion, str) and completion:
            _write_private(completion_path, completion)
        raise AgyRunnerError("CPA_CORRECTION_UNAVAILABLE", "CPA source-context correction was unavailable") from exc

    refined_sha256 = _sha256_file(output_srt_path)
    draft_cues = parse_srt_cues(draft_text)
    refined_cues = parse_srt_cues(refined_text)
    timing_grid_sha256 = _timing_grid_sha256(refined_cues)
    prompt = capture.get("provider_prompt")
    if not isinstance(prompt, str):
        # A cache hit still has the full cache identity prompt, but it was not
        # sent to a provider.  Keep a truthful artifact for the cache path.
        prompt = str(capture.get("cache_prompt") or "")
        capture["provider_prompt"] = prompt
        _write_private(prompt_path, prompt)
        record_prompt(str(capture.get("cache_prompt") or ""))
    prompt_sha256 = _sha256_bytes(prompt.encode("utf-8"))
    completion_sha256 = _sha256_file(completion_path)
    request_id = capture.get("request_id")
    if not isinstance(request_id, str):
        record_prompt(str(capture.get("cache_prompt") or ""))
        request_id = str(capture["request_id"])
    if len(refined_cues) != len(draft_cues) - len(deleted_cue_indexes):
        raise AgyRunnerError("CPA_CORRECTION_INVALID_OUTPUT", "CPA output timing grid is incomplete")
    cache_row = _cache_row(media_path)
    completion_source = "cache" if cache_row and cache_row.get("served_from_cache") else "provider"
    cache_key = cache_row.get("cache_key") if cache_row else None
    if not isinstance(cache_key, str):
        cache_key = None

    return AgyExecutionResult(
        provider="cpa",
        model=getattr(completion, 'answering_model', None) or model,
        requested_model=model,
        agy_rc=None,
        provider_fallback_used=False,
        provider_request_id=request_id,
        requested_provider="cpa",
        executed_provider="cpa",
        source_media_sha256=media_sha256,
        draft_srt_sha256=draft_sha256,
        refined_srt_sha256=refined_sha256,
        timing_validated=True,
        audio_input_attested=False,
        prompt_sha256=prompt_sha256,
        completion_sha256=completion_sha256,
        prompt_path=str(prompt_path),
        completion_path=str(completion_path),
        request_path=str(request_path),
        request_id_kind="content_bound_hash",
        cpa_cache_identity=completion_identity(identity, completion),
        deleted_cue_indexes=deleted_cue_indexes,
        timing_grid_sha256=timing_grid_sha256,
        completion_source=completion_source,
        cpa_cache_key=cache_key,
    )


def validate_cpa_source_context_result(
    result: Any,
    *,
    context_media_path: Path,
    context_draft_srt_path: Path,
    context_refined_srt_path: Path,
) -> tuple[str, ...]:
    """Validate a CPA result against the exact context files in the executor."""

    reasons: list[str] = []
    if str(getattr(result, "provider", "")) != "cpa":
        reasons.append("CPA_PROVIDER_MISMATCH")
    if str(getattr(result, "requested_provider", "")) != "cpa":
        reasons.append("CPA_REQUESTED_PROVIDER_MISMATCH")
    if str(getattr(result, "executed_provider", "")) != "cpa":
        reasons.append("CPA_EXECUTED_PROVIDER_MISMATCH")
    if not isinstance(getattr(result, "model", None), str) or not result.model.strip():
        reasons.append("CPA_MODEL_MISSING")
    requested_model = getattr(result, 'requested_model', None)
    if requested_model is not None and (not isinstance(requested_model, str) or not requested_model.strip()):
        reasons.append('CPA_REQUESTED_MODEL_INVALID')
    identity = getattr(result, "cpa_cache_identity", None)
    if isinstance(identity, Mapping):
        models = identity.get("models")
        if isinstance(models, (list, tuple)) and models and result.model != models[0]:
            reasons.append("CPA_MODEL_IDENTITY_MISMATCH")
    if not isinstance(getattr(result, "provider_request_id", None), str) or not result.provider_request_id.strip():
        reasons.append("CPA_REQUEST_ID_MISSING")
    if getattr(result, "request_id_kind", None) != "content_bound_hash":
        reasons.append("CPA_REQUEST_ID_KIND_MISSING")
    if getattr(result, "timing_validated", None) is not True:
        reasons.append("CPA_TIMING_UNVALIDATED")
    if getattr(result, "audio_input_attested", None) is not False:
        reasons.append("CPA_AUDIO_ATTESTATION_FORBIDDEN")

    paths = {
        "source_media_sha256": context_media_path,
        "draft_srt_sha256": context_draft_srt_path,
        "refined_srt_sha256": context_refined_srt_path,
    }
    for field, path in paths.items():
        declared = getattr(result, field, None)
        if not isinstance(declared, str) or not path.is_file():
            reasons.append(f"CPA_{field.upper()}_MISSING")
        elif declared != _sha256_file(path):
            reasons.append(f"CPA_{field.upper()}_MISMATCH")

    for field in ("prompt_path", "completion_path", "request_path"):
        raw_path = getattr(result, field, None)
        path = Path(raw_path) if isinstance(raw_path, str) else None
        if path is None or not path.is_file():
            reasons.append(f"CPA_{field.upper()}_MISSING")
    for field, path_field in (("prompt_sha256", "prompt_path"), ("completion_sha256", "completion_path")):
        raw_path = getattr(result, path_field, None)
        declared = getattr(result, field, None)
        if isinstance(raw_path, str) and Path(raw_path).is_file() and declared != _sha256_file(Path(raw_path)):
            reasons.append(f"CPA_{field.upper()}_MISMATCH")

    request_path = getattr(result, "request_path", None)
    if isinstance(request_path, str) and Path(request_path).is_file():
        try:
            request = json.loads(Path(request_path).read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            request = None
        if not isinstance(request, Mapping):
            reasons.append("CPA_REQUEST_ARTIFACT_INVALID")
        else:
            expected_request_fields = {
                "provider": "cpa",
                "request_id": getattr(result, "provider_request_id", None),
                "model": requested_model if requested_model is not None else getattr(result, "model", None),
                "source_media_sha256": getattr(result, "source_media_sha256", None),
                "draft_srt_sha256": getattr(result, "draft_srt_sha256", None),
                "prompt_sha256": getattr(result, "prompt_sha256", None),
            }
            for field, expected in expected_request_fields.items():
                if expected is not None and request.get(field) != expected:
                    reasons.append(f"CPA_REQUEST_{field.upper()}_MISMATCH")

    deleted_raw = getattr(result, "deleted_cue_indexes", ())
    deleted = tuple(deleted_raw) if isinstance(deleted_raw, (tuple, list)) else ()
    if len(set(deleted)) != len(deleted) or any(type(index) is not int for index in deleted):
        reasons.append("CPA_DELETED_CUE_INDEXES_INVALID")

    draft_cues = []
    refined_cues = []
    if context_draft_srt_path.is_file() and context_refined_srt_path.is_file():
        draft_cues = parse_srt_cues(context_draft_srt_path.read_text(encoding="utf-8-sig"))
        refined_cues = parse_srt_cues(context_refined_srt_path.read_text(encoding="utf-8-sig"))
        if any(index < 1 or index > len(draft_cues) for index in deleted):
            reasons.append("CPA_DELETED_CUE_INDEX_OUT_OF_RANGE")
        expected = [cue for number, cue in enumerate(draft_cues, start=1) if number not in deleted]
        if len(expected) != len(refined_cues):
            reasons.append("CPA_TIMING_GRID_COUNT_MISMATCH")
        else:
            for expected_cue, refined_cue in zip(expected, refined_cues):
                if (expected_cue.start_ms, expected_cue.end_ms) != (refined_cue.start_ms, refined_cue.end_ms):
                    reasons.append("CPA_TIMING_GRID_MISMATCH")
                    break
        timing_grid_sha256 = _timing_grid_sha256(refined_cues)
        if getattr(result, "timing_grid_sha256", None) != timing_grid_sha256:
            reasons.append("CPA_TIMING_GRID_SHA256_MISMATCH")

        completion_path = getattr(result, "completion_path", None)
        if isinstance(completion_path, str) and Path(completion_path).is_file():
            try:
                from src.autoslice.llm_client import extract_json_object

                completion = extract_json_object(Path(completion_path).read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                completion = None
            rows = completion.get("cues") if isinstance(completion, Mapping) else None
            if not isinstance(rows, list):
                reasons.append("CPA_COMPLETION_ARTIFACT_INVALID")
            else:
                corrected: dict[int, str] = {}
                if len(rows) != len(draft_cues):
                    reasons.append("CPA_COMPLETION_CUE_SET_INVALID")
                for row in rows:
                    if (
                        not isinstance(row, Mapping)
                        or type(row.get("n")) is not int
                        or not isinstance(row.get("text"), str)
                        or row["n"] in corrected
                    ):
                        reasons.append("CPA_COMPLETION_CUE_SET_INVALID")
                        continue
                    corrected[row["n"]] = row["text"]
                if set(corrected) != set(range(1, len(draft_cues) + 1)):
                    reasons.append("CPA_COMPLETION_CUE_SET_INVALID")
                completion_deleted = tuple(
                    sorted(
                        row["n"]
                        for row in rows
                        if isinstance(row, Mapping)
                        and type(row.get("n")) is int
                        and isinstance(row.get("text"), str)
                        and not row["text"].strip()
                    )
                )
                if completion_deleted != tuple(sorted(deleted)):
                    reasons.append("CPA_DELETED_CUE_DECLARATION_MISMATCH")
                if set(corrected) == set(range(1, len(draft_cues) + 1)):
                    from src.autoslice.subtitle_draft_preparation import _render_complete_cpa_review

                    expected_text = _render_complete_cpa_review(
                        draft_cues,
                        corrected,
                        context_draft_srt_path.read_text(encoding="utf-8-sig"),
                    )
                    expected_cues = parse_srt_cues(expected_text)
                    actual_pairs = [
                        (cue.start_ms, cue.end_ms, cue.text) for cue in refined_cues
                    ]
                    expected_pairs = [
                        (cue.start_ms, cue.end_ms, cue.text) for cue in expected_cues
                    ]
                    if actual_pairs != expected_pairs:
                        reasons.append("CPA_REFINED_TEXT_MISMATCH")
    elif not draft_cues:
        reasons.append("CPA_DRAFT_PARSE_FAILED")

    return tuple(dict.fromkeys(reasons))
