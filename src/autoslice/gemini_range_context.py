"""Candidate-blind Gemini complete-block context observations.

This module is the ordinary Gemini counterpart to the native range witness.
The provider hears one exact semantic block and returns one nominal
``complete_block`` interval.  The interval is an envelope for a whole block;
it is deliberately not a word-timing alignment and it never becomes a narrow
single-cue witness or subtitle mutation authority.

The provider transport remains :mod:`entity_audio_verifier`: this module only
does the range contract, exact media preparation, source-bound artifacts and
the existing source-scoped audio budget.  In particular, no candidate text or
subtitle text is sent to Gemini.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any, NotRequired, TypedDict, cast

from src.autoslice.acoustic_witness_adjudication import build_witness_request
from src.autoslice.evidence_identity import recorded_source_stat_matches
from src.autoslice.entity_audio_verifier import (
    _LocalAudioVerifier,
    _crop_black_frame_audio,
    _entity_api_model,
    _observe_entity_audio,
    _sha256,
    _source_stat_binding,
)
from src.autoslice.entity_audio_gemini_web import _provider_provenance
from src.autoslice.independent_context_adjudication import (
    RANGE_ACOUSTIC_CONTEXT_SCHEMA,
    _range_cue_rows,
    _range_media_source_binding,
    validate_range_response_protocol,
    validate_segmented_range_context_observation,
)
from src.autoslice.native_audio_budget_receipt import (
    exclusive_native_audio_budget,
    resume_native_audio_budget,
)
from src.autoslice.native_context_witness import _prepare_request
from src.autoslice.native_foreign_witness import _extract_exact_mp3, _persist_budget_receipt
from src.autoslice.supplement_audio_budget import ensure_budget
from scripts.gemini_slice_jingting import strip_markdown_fence


RANGE_REQUEST_SCHEMA = "gemini-semantic-range-transcript-request.v1"
_ACQUISITION_SCHEMA = "gemini-semantic-range-acquisition.v1"
_EXECUTION_SCHEMA = "gemini-semantic-range-transcript-execution.v1"
_PROMPT_CONTRACT = "gemini-semantic-complete-block.v2"
_HEX_SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")
_ALLOWED_PROMPT_REQUEST_KEYS = frozenset(
    {"schema_version", "target_audio_start_ms", "target_audio_end_ms", "timeline_binding"}
)
_FORBIDDEN_LEXICAL_KEYS = frozenset(
    {
        "candidate",
        "candidate_id",
        "candidate_text",
        "canonical_entity",
        "current_cue",
        "proposed_cue",
        "replacement",
        "transcript",
        "text",
        "words",
        "lexical",
        "subtitle",
        "srt",
        "cue_text",
    }
)


class _RangeInputInvalid(ValueError):
    """A typed range failure that occurred before provider dispatch."""

    def __init__(self, reason_code: str, detail: str = "") -> None:
        super().__init__(detail or reason_code)
        self.reason_code = reason_code
        self.detail = detail


class _DispatchPersistenceFailed(RuntimeError):
    """The source budget reservation could not be durably recorded."""


DISPATCH_CLAIM_DIR = "native-dispatch-claims"
DISPATCH_CLAIM_SCHEMA = "native-dispatch-request-claim.v1"
SOFT_PRESSURE_ATTEMPTS = 3
SOFT_PRESSURE_NO_NEW_REASON = "GEMINI_RANGE_SOFT_PRESSURE_NO_NEW_REASON"
RESEND_REASONS = {
    "DISPATCHED": "RECOVERABLE_INTERRUPTION",  # outcome unknown and not retrievable
    "FAILED": "TRANSPORT_FAILURE",
}


class _PriorRequestUnknown(RuntimeError):
    """Soft pressure is reached for this route/window without a new reason."""

    def __init__(self, reason_code: str, detail: str) -> None:
        super().__init__(detail)
        self.reason_code = reason_code
        self.detail = detail


def _range_request_identity(
    *, provider: str, model: str, source_sha256: str, start_ms: int, end_ms: int,
    prompt_sha256: str, input_audio_sha256: str,
) -> str:
    """Request identity: route, model, source bytes, window, prompt and input bytes.

    The output directory is not part of it (41:112; 40 claim rule).
    """
    return _digest_json({
        "schema_version": DISPATCH_CLAIM_SCHEMA, "provider": provider, "model": model,
        "source_media_sha256": source_sha256, "start_ms": start_ms, "end_ms": end_ms,
        "prompt_sha256": prompt_sha256, "input_audio_sha256": input_audio_sha256,
    })


def _claim_directories(receipt_path: Path, output_dir: Path) -> list[Path]:
    """The account's own claim store, its registered parents and this root."""
    account = Path(receipt_path).absolute().parent
    roots = [account, Path(output_dir).absolute()]
    manifest = account / "parent.json"
    if manifest.is_file() and not manifest.is_symlink():
        try:
            parents = json.loads(manifest.read_text(encoding="utf-8")).get("parents") or []
        except (OSError, UnicodeError, ValueError, AttributeError):
            parents = []
        roots.extend(Path(str(row.get("path"))) for row in parents
                     if isinstance(row, Mapping) and row.get("path"))
    return list(dict.fromkeys(root / DISPATCH_CLAIM_DIR for root in roots))


def _read_dispatch_claims(directories: list[Path], *, source_sha256: str) -> dict[int, dict[str, Mapping[str, Any]]]:
    """Map attempt_id -> {request identity: claim} from valid, self-consistent claims."""
    claimed: dict[int, dict[str, Mapping[str, Any]]] = {}
    for directory in directories:
        if not directory.is_dir() or directory.is_symlink():
            continue
        for path in sorted(directory.glob("*.json")):
            try:
                _ensure_regular(path)
                claim = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, ValueError, _RangeInputInvalid):
                continue
            if not isinstance(claim, Mapping) or claim.get("schema_version") != DISPATCH_CLAIM_SCHEMA:
                continue
            try:
                identity = _range_request_identity(
                    provider=str(claim["provider"]), model=str(claim["model"]),
                    source_sha256=str(claim["source_media_sha256"]),
                    start_ms=int(claim["start_ms"]), end_ms=int(claim["end_ms"]),
                    prompt_sha256=str(claim["prompt_sha256"]),
                    input_audio_sha256=str(claim["input_audio_sha256"]),
                )
            except (KeyError, TypeError, ValueError):
                continue
            if (identity != claim.get("request_identity_sha256")
                    or claim.get("source_media_sha256") != source_sha256
                    or type(claim.get("attempt_id")) is not int
                    or path.name != f"{identity}.{claim['attempt_id']}.json"):
                continue
            claimed.setdefault(int(claim["attempt_id"]), {})[identity] = dict(claim)
    return claimed


def _pending_rows(budget: Any, *, provider: str, model: str, start_ms: int, end_ms: int) -> list[Mapping[str, Any]]:
    return [
        row for row in budget.snapshot()["attempts"]
        if row.get("status") == "DISPATCHED" and row.get("provider") == provider
        and row.get("model") == model and (row.get("start_ms"), row.get("end_ms")) == (start_ms, end_ms)
    ]


def _resend_basis(
    budget: Any, *, directories: list[Path], source_sha256: str, provider: str,
    model: str, start_ms: int, end_ms: int, identity: str,
) -> dict[str, Any]:
    """40 (维护者): retrieve first; if impossible, a bounded re-send.

    Earlier attempts of this provider/model/exact window stay as they are and
    count toward the cumulative soft pressure of three. Below it the ordinary
    entry dispatches normally. At or beyond it, a dispatch continues only for
    a recoverable interruption (pending, not retrievable), a real transport
    failure, or a request no earlier claimed attempt carried; the basis is
    recorded with the new attempt. Otherwise nothing is sent and the refusal
    is recorded.
    """
    prior = [
        row for row in budget.snapshot()["attempts"]
        if row.get("provider") == provider and row.get("model") == model
        and (row.get("start_ms"), row.get("end_ms")) == (start_ms, end_ms)
    ]
    basis: dict[str, Any] = {"prior_attempt_ids": [int(row["attempt_id"]) for row in prior],
                             "soft_pressure_attempts": SOFT_PRESSURE_ATTEMPTS,
                             "soft_pressure_exceeded": len(prior) >= SOFT_PRESSURE_ATTEMPTS}
    if prior:
        basis["reason"] = RESEND_REASONS.get(str(prior[-1].get("status")))
        claimed = _read_dispatch_claims(directories, source_sha256=source_sha256)
        if basis["reason"] is None and all(
                int(row["attempt_id"]) in claimed and identity not in claimed[int(row["attempt_id"])]
                for row in prior):
            basis["reason"] = "DISTINCT_REQUEST"
        if basis["reason"] is None:
            inherited = (_inherited_range_cache_unavailable(
                account=directories[0].parent, source_sha256=source_sha256,
                start_ms=start_ms, end_ms=end_ms, provider=provider, model=model)
                if directories else None)
            completed = [a for a in prior if a.get('status') in {'OBSERVED', 'TEXT_UNLOCATED'}]
            if inherited is not None and completed and all(a in inherited['attempts'] for a in completed):
                basis.update(reason='NATIVE_CACHE_REACQUISITION_REQUIRED',
                    unavailable_acquisition_attempt_ids=[int(a['attempt_id'])
                        for a in inherited['attempts'] if a in prior])
        if basis["reason"] is None:
            unavailable = []
            for row in prior:
                claim = claimed.get(int(row["attempt_id"]), {}).get(identity)
                run_dir = claim.get("run_dir") if claim else None
                if isinstance(run_dir, str):
                    metadata = _read_json_path(Path(run_dir) / "acquisition.json")
                    if (metadata and metadata.get("source_media_sha256") == source_sha256
                            and metadata.get("provider") == provider and metadata.get("model") == model
                            and metadata.get("source_target_ms") == [start_ms, end_ms]
                            and _unavailable_acquisition_media(metadata)):
                        unavailable.append(int(row["attempt_id"]))
            if unavailable:
                basis.update(reason="BOUND_MEDIA_REACQUISITION_REQUIRED",
                             unavailable_acquisition_attempt_ids=unavailable)
    if basis["soft_pressure_exceeded"] and basis.get("reason") is None:
        raise _PriorRequestUnknown(
            SOFT_PRESSURE_NO_NEW_REASON,
            f"{len(prior)} attempts on this provider/model/window and no new reason to continue",
        )
    return basis

def _read_back_prior_request(
    budget: Any, *, verifier: Any, receipt_path: Path, directories: list[Path],
    geometry: Mapping[str, Any], prompt: str, prompt_sha: str, input_audio_sha: str,
) -> dict[str, Any] | None:
    """Read the original stored result of the same pending request and seal it.

    Only the owner's own complete acquisition record in the claimed original
    run directory counts; nothing is inferred for a pending row without it.
    """
    start_ms = int(geometry["source_target_start_ms"])
    end_ms = int(geometry["source_target_end_ms"])
    for provider, model in sorted(_provider_model_pairs(verifier)):
        identity = _range_request_identity(
            provider=provider, model=model, source_sha256=str(verifier.source_sha256),
            start_ms=start_ms, end_ms=end_ms, prompt_sha256=prompt_sha,
            input_audio_sha256=input_audio_sha,
        )
        with budget.lock, exclusive_native_audio_budget(Path(receipt_path).absolute()):
            resume_native_audio_budget(
                source_media=verifier.source_media, source_media_sha256=verifier.source_sha256,
                receipt_path=Path(receipt_path).absolute(), require_existing=True,
            )
            pending = _pending_rows(budget, provider=provider, model=model,
                                    start_ms=start_ms, end_ms=end_ms)
            if not pending:
                continue
            claimed = _read_dispatch_claims(directories, source_sha256=str(verifier.source_sha256))
            for row in pending:
                claim = claimed.get(int(row["attempt_id"]), {}).get(identity)
                run_dir = claim.get("run_dir") if claim else None
                if not isinstance(run_dir, str):
                    continue
                metadata = _cache_metadata_valid(
                    Path(run_dir) / "acquisition.json", verifier=verifier, geometry=geometry,
                    prompt=prompt, prompt_sha=prompt_sha,
                )
                if metadata is None or (metadata.get("provider"), metadata.get("model")) != (provider, model):
                    continue
                error = metadata.get("response_error")
                budget.finish_attempt(
                    int(row["attempt_id"]),
                    status="RESPONSE_REJECTED" if error else "OBSERVED",
                    reason_code=str(error) if error else None,
                )
                _persist_budget_receipt(
                    source_media=verifier.source_media, source_sha256=verifier.source_sha256,
                    receipt_path=Path(receipt_path).absolute(),
                )
                return metadata
    return None


def _digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _digest_json(value: Mapping[str, Any]) -> str:
    return _digest_bytes(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    )


def _safe_detail(value: object, *, limit: int = 300) -> str:
    text = " ".join(str(value or "").replace("\x00", " ").split())
    text = re.sub(
        r"(?i)(api[_ -]?key|authorization|bearer|token|secret|password)\s*[:=]\s*\S+",
        r"\1=<redacted>",
        text,
    )
    return text[:limit]


def _is_int(value: object) -> bool:
    return type(value) is int


def _ensure_regular(path: Path, *, allow_missing: bool = False) -> None:
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise _RangeInputInvalid("GEMINI_RANGE_SYMLINK_PATH", str(path))
    if not path.exists():
        if allow_missing:
            return
        raise _RangeInputInvalid("GEMINI_RANGE_PATH_MISSING", str(path))
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise _RangeInputInvalid("GEMINI_RANGE_PATH_NOT_REGULAR", str(path))


def _write_immutable(path: Path, data: bytes) -> None:
    """Create an artifact once, tolerating an identical replay only."""

    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise OSError(f"range artifact path contains a symlink: {path}")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise OSError(f"range artifact is a symlink: {path}")
    if path.exists():
        _ensure_regular(path)
        if path.read_bytes() != data:
            raise OSError(f"range artifact already contains different bytes: {path}")
        return
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        try:
            path.unlink()
        except OSError:
            pass
        raise


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    _write_immutable(
        path,
        (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(
            "utf-8"
        ),
    )


def _file_sha(path: Path) -> str:
    _ensure_regular(path)
    return _sha256(path)


def _path_digest(path: object) -> str | None:
    if not isinstance(path, str) or not path:
        return None
    try:
        value = Path(path)
        _ensure_regular(value)
        return _sha256(value)
    except (OSError, ValueError, _RangeInputInvalid):
        return None


def _record(path: Path) -> dict[str, str]:
    return {"path": str(path), "sha256": _file_sha(path)}


def _path_value(value: object) -> Path | None:
    if isinstance(value, (str, os.PathLike)):
        try:
            return Path(value)
        except (TypeError, ValueError):
            return None
    return None


def _provider_model_pairs(verifier: _LocalAudioVerifier) -> set[tuple[str, str]]:
    pairs = {("agy", str(verifier.model))}
    api_model = _entity_api_model()
    if isinstance(api_model, str) and api_model:
        pairs.add(("gemini_api", api_model))
    return pairs


def _validate_timeline_binding(value: object) -> None:
    """Reject lexical payloads nested in the candidate-free prompt request."""

    if not isinstance(value, Mapping):
        raise _RangeInputInvalid("GEMINI_RANGE_TIMELINE_BINDING_INVALID")
    for key, child in value.items():
        if not isinstance(key, str):
            raise _RangeInputInvalid("GEMINI_RANGE_TIMELINE_BINDING_INVALID")
        lowered = key.lower()
        if any(token in lowered for token in _FORBIDDEN_LEXICAL_KEYS):
            raise _RangeInputInvalid("GEMINI_RANGE_CANDIDATE_EXPOSURE")
        if isinstance(child, Mapping):
            _validate_timeline_binding(child)
        elif isinstance(child, list):
            for item in child:
                if isinstance(item, Mapping):
                    _validate_timeline_binding(item)
                elif isinstance(item, (str, int, float, bool)) or item is None:
                    continue
                else:
                    raise _RangeInputInvalid("GEMINI_RANGE_TIMELINE_BINDING_INVALID")
        elif isinstance(child, (str, int, float, bool)) or child is None:
            # Schema labels and role names are allowed; no free-form sentence
            # is copied into the provider prompt.
            continue
        else:
            raise _RangeInputInvalid("GEMINI_RANGE_TIMELINE_BINDING_INVALID")


def range_transcript_prompt(request: Mapping[str, Any]) -> str:
    """Build the fixed complete-block Gemini prompt.

    Only geometry and a structural timeline binding are accepted.  The
    provider is asked to report one complete semantic block, with one nominal
    interval covering the attached clip.  It is never asked for word timing,
    translation, or a candidate choice.
    """

    if not isinstance(request, Mapping) or set(request) != _ALLOWED_PROMPT_REQUEST_KEYS:
        raise ValueError("Gemini range prompt request must contain geometry only")
    if request.get("schema_version") != RANGE_REQUEST_SCHEMA:
        raise ValueError("Gemini range prompt schema is invalid")
    start = request.get("target_audio_start_ms")
    end = request.get("target_audio_end_ms")
    if not _is_int(start) or not _is_int(end) or start < 0 or end <= start:
        raise ValueError("Gemini range prompt geometry is invalid")
    _validate_timeline_binding(request.get("timeline_binding"))
    duration = end - start
    return f"""# Candidate-blind complete semantic-block transcript

Listen to the entire attached exact audio block. The frames are black and
contain no text. Use only the audio in the attachment. Report the spoken
content that is actually audible in the complete block; do not translate,
guess, normalize, or complete missing words. Do not use subtitles, metadata,
viewer chat, or any candidate wording.

The attached block corresponds to target audio {start} ms through {end} ms;
its duration is {duration} ms. The block is the unit of evidence. The declared
segment boundary is nominal and covers the whole block; it is not a word or
token timing. Do not split the block into word timings.

Reply with exactly one JSON object and no markdown fences or surrounding text:
{{"status":"OBSERVED"|"UNCERTAIN","target_audible":true|false,"full_transcript":"", "segments":[{{"segment":"complete_block","start_ms":0,"end_ms":{duration},"transcript":""}}], "audio_observations":[{{"kind":"environment_sound|nonsemantic_vocalization","status":"OBSERVED|UNCERTAIN","start_ms":0,"end_ms":{duration},"description":""}}]}}

Use status UNCERTAIN when the block cannot be reliably heard. Keep
full_transcript and the complete_block transcript faithful to the audio, with
no translation or invented content. Keep meaningful lyrics and language switches;
leave interrupted words incomplete. Put only linguistic content in transcripts.
Separately report actually heard environmental sounds or nonsemantic vocalizations
in audio_observations, or [] if none. Describe the sound without inventing object
identity (an impact alone does not prove a cup), actions, words or speaker identity.
These event intervals are approximate crop-local locations, not word timestamps.
Do not call unclear foreign speech or lyrics meaningless humming. Use UNCERTAIN
for a doubtful observation. target_audible describes audible sound, not whether
speech exists; transcripts may be empty when only nonlinguistic sounds are heard.
"""


def _request_uncertain(
    *,
    candidate_id: str | None,
    provider: str,
    model: str,
    reason_code: str,
    detail: str,
    geometry: Mapping[str, Any] | None = None,
    artifact_bindings: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "schema_version": RANGE_ACOUSTIC_CONTEXT_SCHEMA,
        "status": "UNCERTAIN",
        "authority": "EVIDENCE_ONLY",
        "mutation_authorized": False,
        "candidate_blind": True,
        "candidate_id": candidate_id,
        "provider": provider,
        "model": model,
        "evidence_scope": "RANGE_CONTEXT_ONLY",
        "transcript_scope": "RANGE_WITHOUT_SINGLE_CUE_TIMESTAMPS",
        "context_only": True,
        "reason_code": reason_code,
        "reason_detail": _safe_detail(detail),
        "verdict": {
            "exact_transcript": "",
            "original_provider_response": {},
        },
    }
    if geometry:
        row.update(
            range={
                "cue_indexes": list(geometry.get("cue_indexes") or []),
                "matched_start_ms": geometry.get("matched_start_ms"),
                "matched_end_ms": geometry.get("matched_end_ms"),
            },
            source_window={
                "crop_start_ms": geometry.get("crop_start_ms"),
                "crop_end_ms": geometry.get("crop_end_ms"),
            },
        )
    if artifact_bindings:
        row["artifact_bindings"] = dict(artifact_bindings)
    return row


def _validate_source_and_geometry(
    *, verifier: _LocalAudioVerifier,
    check_request: Mapping[str, Any],
    range_witness_request: Mapping[str, Any],
) -> tuple[dict[str, Any], Any, Any, str, list[dict[str, Any]]]:
    """Validate all source/window/SRT bindings before media/provider work."""

    if not isinstance(check_request, Mapping) or not isinstance(range_witness_request, Mapping):
        raise _RangeInputInvalid("GEMINI_RANGE_REQUEST_INVALID")
    declared_check_hash = check_request.get("request_sha256")
    if declared_check_hash is not None:
        normalized_check_hash = str(declared_check_hash).removeprefix("sha256:")
        check_payload = dict(check_request)
        check_payload.pop("request_sha256", None)
        if (
            not _HEX_SHA256.fullmatch(normalized_check_hash)
            or _digest_json(check_payload) != normalized_check_hash
        ):
            raise _RangeInputInvalid("GEMINI_RANGE_CURRENT_REQUEST_HASH_INVALID")
    try:
        current_request = build_witness_request(check_request)
        current = _prepare_request(current_request)
        broad = _prepare_request(range_witness_request)
    except Exception as exc:
        raise _RangeInputInvalid(
            getattr(exc, "reason_code", "GEMINI_RANGE_REQUEST_INVALID"),
            getattr(exc, "detail", str(exc)),
        ) from exc
    if broad.request.get("schema_version") != current.request.get("schema_version"):
        raise _RangeInputInvalid("GEMINI_RANGE_REQUEST_SCHEMA_INVALID")
    indexes = broad.request.get("cue_indexes")
    current_indexes = current.request.get("cue_indexes")
    if (
        not isinstance(indexes, list)
        or not indexes
        or any(type(value) is not int for value in indexes)
        or indexes != list(range(indexes[0], indexes[-1] + 1))
        or not isinstance(current_indexes, list)
        or len(current_indexes) != 1
        or current_indexes[0] not in indexes
    ):
        raise _RangeInputInvalid("GEMINI_RANGE_CUE_INDEXES_INVALID")
    if broad.source_offset_ms != current.source_offset_ms:
        raise _RangeInputInvalid("GEMINI_RANGE_SOURCE_OFFSET_DRIFT")
    if not (
        broad.target_start_ms <= current.target_start_ms
        < current.target_end_ms
        <= broad.target_end_ms
    ):
        raise _RangeInputInvalid("GEMINI_RANGE_CURRENT_TARGET_OUTSIDE_BLOCK")

    srt_text = check_request.get("whole_clip_current_srt")
    if not isinstance(srt_text, str) or not srt_text:
        raise _RangeInputInvalid("GEMINI_RANGE_CURRENT_SRT_MISSING")
    rows = _range_cue_rows(
        srt_text,
        list(indexes),
        broad.target_start_ms,
        broad.target_end_ms,
        allow_boundary_gaps=True,
    )
    current_rows = _range_cue_rows(
        srt_text,
        list(current_indexes),
        current.target_start_ms,
        current.target_end_ms,
        allow_boundary_gaps=False,
    )
    if rows is None or current_rows is None or current_rows[0].get("coverage") != "FULL":
        raise _RangeInputInvalid("GEMINI_RANGE_SRT_GEOMETRY_INVALID")

    source = Path(verifier.source_media)
    try:
        _ensure_regular(source)
        stat_before = _source_stat_binding(source)
        source_sha = _sha256(source)
        stat_after = _source_stat_binding(source)
    except Exception as exc:
        raise _RangeInputInvalid("GEMINI_RANGE_SOURCE_UNAVAILABLE", str(exc)) from exc
    if stat_before != stat_after or tuple(stat_before) != tuple(verifier.source_stat_binding):
        raise _RangeInputInvalid("GEMINI_RANGE_SOURCE_STAT_DRIFT")
    if source_sha != verifier.source_sha256:
        raise _RangeInputInvalid("GEMINI_RANGE_SOURCE_HASH_DRIFT")
    if not isinstance(verifier.source_duration_ms, int) or verifier.source_duration_ms <= 0:
        raise _RangeInputInvalid("GEMINI_RANGE_SOURCE_DURATION_INVALID")
    if broad.physical_end_ms > verifier.source_duration_ms:
        raise _RangeInputInvalid("GEMINI_RANGE_SOURCE_WINDOW_OUT_OF_BOUNDS")
    if _range_media_source_binding(check_request, str(source), source_sha) is None:
        raise _RangeInputInvalid("GEMINI_RANGE_CURRENT_SOURCE_OR_CANDIDATE_DRIFT")
    clip = check_request.get("clip_context_binding")
    candidate_id = clip.get("candidate_id") if isinstance(clip, Mapping) else None
    if not isinstance(candidate_id, str) or not candidate_id:
        raise _RangeInputInvalid("GEMINI_RANGE_CANDIDATE_BINDING_MISSING")
    for key in ("whole_clip_current_srt_sha256", "current_srt_sha256", "reviewed_srt_sha256"):
        declared = check_request.get(key)
        if declared is not None and str(declared).removeprefix("sha256:") != _digest_bytes(
            srt_text.encode("utf-8")
        ):
            raise _RangeInputInvalid("GEMINI_RANGE_CURRENT_SRT_HASH_DRIFT")

    geometry = {
        "candidate_id": candidate_id,
        "cue_indexes": list(indexes),
        "current_cue_indexes": list(current_indexes),
        "matched_start_ms": broad.target_start_ms,
        "matched_end_ms": broad.target_end_ms,
        "source_target_start_ms": broad.physical_start_ms,
        "source_target_end_ms": broad.physical_end_ms,
        "source_offset_ms": broad.source_offset_ms,
        "crop_start_ms": broad.physical_start_ms,
        "crop_end_ms": broad.physical_end_ms,
    }
    return geometry, current, broad, srt_text, rows


def _blind_request(geometry: Mapping[str, Any]) -> dict[str, Any]:
    duration = int(geometry["crop_end_ms"] - geometry["crop_start_ms"])
    request = {
        "schema_version": RANGE_REQUEST_SCHEMA,
        "target_audio_start_ms": 0,
        "target_audio_end_ms": duration,
        "timeline_binding": {
            "schema_version": "gemini-semantic-range-timeline-binding.v1",
            "source_media_timeline_offset_ms": int(geometry["source_offset_ms"]),
            "delivery_target_ms": [int(geometry["matched_start_ms"]), int(geometry["matched_end_ms"])],
            "source_target_ms": [int(geometry["source_target_start_ms"]), int(geometry["source_target_end_ms"])],
            "source_crop_ms": [int(geometry["crop_start_ms"]), int(geometry["crop_end_ms"])],
            "delivery_crop_ms": [
                int(geometry["crop_start_ms"] - geometry["source_offset_ms"]),
                int(geometry["crop_end_ms"] - geometry["source_offset_ms"]),
            ],
        },
    }
    # Run the public prompt validator before any media/provider operation.
    range_transcript_prompt(request)
    return request


def _acquisition_root(verifier: _LocalAudioVerifier, geometry: Mapping[str, Any], prompt_sha: str) -> Path:
    return (
        Path(verifier.output_dir)
        / "gemini-range-acquisition"
        / str(verifier.source_sha256)[:20]
        / f"{geometry['source_target_start_ms']}-{geometry['source_target_end_ms']}"
        / prompt_sha
    )


def _bound_media_digest(record: Mapping) -> str | None:
    from .bound_media_recovery import BoundMediaUnavailable, resolve_bound_media
    try:
        path = resolve_bound_media(Path(record["path"]), record["sha256"])
        return _path_digest(str(path))
    except (BoundMediaUnavailable, KeyError, TypeError, ValueError):
        return None


def _unavailable_acquisition_media(metadata: Mapping) -> bool:
    """Only an intact acquisition with absent derived bytes may be recollected."""
    unsigned = dict(metadata)
    if unsigned.pop("metadata_sha256", None) != _digest_json(unsigned):
        return False
    paths = metadata.get("paths")
    if not isinstance(paths, Mapping):
        return False
    if not {"audio_source", "audio_clip", "actual_provider_audio", "rendered_audio_clip",
            "prompt", "raw_response", "provider_stdout", "execution"}.issubset(paths):
        return False
    missing = False
    for record in paths.values():
        if not isinstance(record, Mapping) or not isinstance(record.get("path"), str):
            return False
        expected = record.get("sha256")
        if (not isinstance(expected, str) or len(expected) != 64
                or any(c not in "0123456789abcdef" for c in expected)):
            return False
        path = Path(record["path"])
        if path.is_symlink():
            return False
        if _path_digest(str(path)) == record.get("sha256"):
            continue
        if path.exists() or path.suffix not in {".mp4", ".mp3", ".wav"}:
            return False
        if _bound_media_digest(record) != record.get("sha256"):
            missing = True
    return missing


def _cache_metadata_valid(
    path: Path,
    *,
    verifier: _LocalAudioVerifier,
    geometry: Mapping[str, Any],
    prompt: str | None,
    prompt_sha: str,
    unavailable: set[str] | None = None,
    inherited_missing: set[str] | None = None,
    resolved: dict[str, Path] | None = None,
    expected_pair: tuple[str, str] | None = None,
    original_contract: bool = False,
) -> dict[str, Any] | None:
    try:
        _ensure_regular(path)
        metadata = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, _RangeInputInvalid):
        return None
    if not isinstance(metadata, Mapping):
        return None
    metadata_without_digest = dict(metadata)
    stored_metadata_digest = metadata_without_digest.pop("metadata_sha256", None)
    if (
        not isinstance(stored_metadata_digest, str)
        or _digest_json(metadata_without_digest) != stored_metadata_digest
    ):
        return None
    if original_contract:
        from .context_assisted_audio import PROMPT_CONTRACT as ASSISTED_PROMPT_CONTRACT
        contract_matches = (
            metadata.get('prompt_contract') == _PROMPT_CONTRACT
            and metadata.get('candidate_blind') is True) or (
            metadata.get('prompt_contract') == ASSISTED_PROMPT_CONTRACT
            and metadata.get('candidate_blind') is False)
    else:
        contract_matches = (
            metadata.get('prompt_contract') == geometry.get('prompt_contract', _PROMPT_CONTRACT)
            and metadata.get('candidate_blind') is (geometry.get('context_assistance') is None))
    if (
        metadata.get("schema_version") != _ACQUISITION_SCHEMA
        or (metadata.get("provider"), metadata.get("model")) not in (
            {expected_pair} if expected_pair is not None else _provider_model_pairs(verifier))
        # Content identity: the verifier hashed the current, stat-stable
        # source; the recorded inode/ctime is placement, not evidence identity.
        or metadata.get("source_media_sha256") != verifier.source_sha256
        or not recorded_source_stat_matches(
            metadata.get("source_stat_binding"), verifier.source_stat_binding)
        or metadata.get("source_target_ms")
        != [geometry["source_target_start_ms"], geometry["source_target_end_ms"]]
        or metadata.get("prompt_sha256") != prompt_sha
        or not contract_matches
        or metadata.get("status") not in {"OBSERVED", "UNCERTAIN"}
    ):
        return None
    paths = metadata.get("paths")
    if not isinstance(paths, Mapping):
        return None
    required = (
        "audio_source", "audio_clip", "actual_provider_audio", "rendered_audio_clip",
        "prompt", "raw_response", "provider_stdout", "execution",
    )
    missing: set[str] = set()
    for role in required:
        record = paths.get(role)
        if not isinstance(record, Mapping) or not isinstance(record.get("path"), str):
            return None
        expected = record.get('sha256')
        if not isinstance(expected, str) or _HEX_SHA256.fullmatch(expected) is None:
            return None
        candidate = Path(record['path'])
        try:
            _ensure_regular(candidate, allow_missing=True)
        except (OSError, _RangeInputInvalid):
            return None
        actual = (_bound_media_digest(record) if role in {
            "audio_source", "audio_clip", "actual_provider_audio", "rendered_audio_clip"
        } else _path_digest(record["path"]))
        if resolved is not None and actual == expected and not candidate.exists():
            from .bound_media_recovery import resolve_bound_media
            resolved[str(candidate)] = resolve_bound_media(candidate, expected)
        if actual is None and inherited_missing is not None and not candidate.exists():
            # Only the inheritance inventory (or its validated manifest reader)
            # may classify absent non-media artifacts. No replacement bytes or
            # reconstructed protocol record is created.
            missing.add(role)
            inherited_missing.add(role)
            actual = expected
        if actual is None and unavailable is not None and role in {
            "audio_source", "audio_clip", "actual_provider_audio", "rendered_audio_clip"
        }:
            from .bound_media_recovery import _no_links
            candidate = Path(record["path"])
            expected = record.get("sha256")
            if (not candidate.exists() and _no_links(candidate)
                    and isinstance(expected, str) and len(expected) == 64
                    and all(c in "0123456789abcdef" for c in expected)):
                # Diagnostic validation only. All other checks below must pass
                # before _find_cache may classify this as a collection miss.
                actual = expected
                unavailable.add(role)
        if actual is None or actual != record.get("sha256"):
            return None
    if paths["audio_clip"] != paths["actual_provider_audio"]:
        return None
    if metadata.get("actual_provider_audio") != dict(paths["actual_provider_audio"]):
        return None
    if metadata.get("rendered_audio_clip") != dict(paths["rendered_audio_clip"]):
        return None
    provider_audio = str(paths["actual_provider_audio"]["path"])
    rendered_audio = str(paths["rendered_audio_clip"]["path"])
    # AGY receives the black-frame MP4; the API ladder receives the MP3 it
    # derives from that MP4.  Keep both bindings so a replay cannot silently
    # substitute one physical input for the other.
    if metadata.get("provider") == "gemini_api":
        if Path(provider_audio).name != "input.gemini-api.mp3":
            return None
    elif Path(provider_audio).name != "input.mp4":
        return None
    if Path(rendered_audio).name != "input.mp4":
        return None
    prompt_path = Path(paths["prompt"]["path"])
    if 'prompt' not in missing:
        try:
            if prompt_path.read_text(encoding="utf-8") != prompt:
                return None
        except (OSError, UnicodeError):
            return None
    execution = _read_json_path(Path(paths["execution"]["path"]))
    if 'execution' not in missing and not isinstance(execution, Mapping):
        return None
    if isinstance(execution, Mapping):
        if execution.get("provider") != metadata.get("provider") or execution.get("model") != metadata.get("model"):
            return None
        provider_audio_record = execution.get("provider_audio")
        if not isinstance(provider_audio_record, Mapping) or dict(provider_audio_record) != dict(paths["actual_provider_audio"]):
            return None
        if execution.get("response_source") != metadata.get("response_source"):
            return None
        if execution.get("provider_route") != metadata.get("provider_route", {}):
            return None
        if execution.get("provider_provenance") != metadata.get("provider_provenance", {}):
            return None
        response_record = execution.get("response")
        stdout_record = execution.get("stdout")
        for record, role in ((response_record, "raw_response"), (stdout_record, "provider_stdout")):
            if not isinstance(record, Mapping) or paths[role] != dict(record):
                return None
    raw = _read_json_path(Path(paths["raw_response"]["path"]))
    if 'raw_response' not in missing and metadata.get("raw_is_json") is True and not isinstance(raw, Mapping):
        return None
    response_source = metadata.get("response_source")
    if response_source is not None:
        if not isinstance(response_source, Mapping):
            return None
        source_path = response_source.get("path")
        source_sha = response_source.get("sha256")
        if not isinstance(source_path, str) or not isinstance(source_sha, str) or _HEX_SHA256.fullmatch(source_sha) is None:
            return None
        try:
            _ensure_regular(Path(source_path), allow_missing=True)
        except (OSError, _RangeInputInvalid):
            return None
        source_missing = inherited_missing is not None and not Path(source_path).exists()
        if source_missing:
            inherited_missing.add('response_source')
        elif _path_digest(source_path) != source_sha:
            return None
        if metadata.get("raw_is_json") is True and not source_missing:
            source_raw = _read_transport_json(Path(source_path))
            if not isinstance(source_raw, Mapping) or ('raw_response' not in missing and source_raw != raw):
                return None
    elif metadata.get("raw_is_json") is True:
        # A successful parsed response must retain the original transport
        # bytes; an observed mapping alone cannot become an authority.
        return None
    return dict(metadata)


def _read_json_path(path: Path) -> Mapping[str, Any] | None:
    try:
        _ensure_regular(path)
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, _RangeInputInvalid):
        return None
    return value if isinstance(value, Mapping) else None


def _read_transport_json(path: Path) -> Mapping[str, Any] | None:
    """Parse the provider's response bytes using the transport's fence rule."""

    try:
        _ensure_regular(path)
        parsed = json.loads(strip_markdown_fence(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeError, ValueError, _RangeInputInvalid):
        return None
    return parsed if isinstance(parsed, Mapping) else None


def _safe_provider_provenance(outcome: Any) -> dict[str, Any]:
    """Copy only provider provenance actually returned by the transport."""

    try:
        return dict(_provider_provenance(outcome))
    except (AttributeError, TypeError, ValueError):
        # Unit seams often use a small SimpleNamespace rather than the private
        # transport dataclass.  Preserve any fields that seam provided without
        # fabricating values for fields it did not return.
        return {
            key: value
            for key in (
                "provider_receipt_sha256",
                "provider_model_label",
                "provider_model_status",
                "provider_input_sha256",
            )
            if (value := getattr(outcome, key, None))
        }


def _provider_route_metadata(outcome: Any, provider: str) -> dict[str, Any]:
    """Persist API ladder route facts only when the transport supplied them."""

    if provider != "gemini_api":
        return {}
    route: dict[str, Any] = {}
    configured_key_count = getattr(outcome, "configured_key_count", None)
    if type(configured_key_count) is int:
        route["configured_key_count"] = configured_key_count
    accepted_key_tier = getattr(outcome, "accepted_key_tier", None)
    if isinstance(accepted_key_tier, str) and accepted_key_tier:
        route["accepted_key_tier"] = accepted_key_tier
    accepted_key_ordinal = getattr(outcome, "accepted_key_ordinal", None)
    if type(accepted_key_ordinal) is int:
        route["accepted_key_ordinal"] = accepted_key_ordinal
    paid_policy_stamp = getattr(outcome, "paid_policy_stamp", None)
    if isinstance(paid_policy_stamp, Mapping):
        route["paid_policy_stamp"] = dict(paid_policy_stamp)
    return route


class _InheritedRangeAttempt(TypedDict):
    attempt_id: int
    provider: str
    model: str
    start_ms: int
    end_ms: int


class _InheritedRangeCache(TypedDict):
    attempts: list[_InheritedRangeAttempt]
    unusable_acquisitions: NotRequired[dict[str, str]]


def _inherited_range_cache_unavailable(
    *, account: Path, source_sha256: str, start_ms: int, end_ms: int,
    provider: str | None = None, model: str | None = None,
) -> _InheritedRangeCache | None:
    from .prepared_native_audio_recovery import _read, _unavailable_cache
    from .native_audio_budget_receipt import _validate_existing, _validate_budget_history
    manifest_path = account / 'parent.json'
    if not manifest_path.exists() and not manifest_path.is_symlink():
        return None
    manifest = _read(manifest_path)
    key = str(Path('gemini-range-acquisition') / source_sha256[:20] / f'{start_ms}-{end_ms}')
    if key not in manifest.get('unusable_caches', {}):
        return None
    receipt = _read(account / 'native-audio-budget.json')
    _validate_existing(receipt, source_media_sha256=source_sha256)
    _validate_budget_history(receipt['budget'])
    record = _unavailable_cache(root=account, relative=key, source_sha=source_sha256,
        budget=receipt['budget'], manifest=manifest)
    if record is None:
        return None
    if any(a['start_ms'] != start_ms or a['end_ms'] != end_ms for a in record['attempts']):
        raise ValueError('PREPARED_NATIVE_UNAVAILABLE_CACHE_IDENTITY_MISSING')
    if provider is not None and not any((a['provider'], a['model']) == (provider, model)
                                      for a in record['attempts']):
        return None
    return cast(_InheritedRangeCache, record)


def _find_cache(
    *, verifier: _LocalAudioVerifier, geometry: Mapping[str, Any], prompt: str, prompt_sha: str,
    receipt_path: Path | None = None,
) -> dict[str, Any] | None:
    inherited = (_inherited_range_cache_unavailable(account=receipt_path.absolute().parent,
        source_sha256=verifier.source_sha256, start_ms=int(geometry['source_target_start_ms']),
        end_ms=int(geometry['source_target_end_ms'])) if receipt_path is not None else None)
    root = _acquisition_root(verifier, geometry, prompt_sha)
    if root.is_symlink():
        raise _RangeInputInvalid("GEMINI_RANGE_CACHE_INVALID", f"acquisition root is a symlink: {root}")
    if not root.is_dir():
        return None
    for path in sorted(root.glob("*/acquisition.json")):
        # A cache entry is an immutable provider acquisition.  Once an entry
        # for the current source/window/prompt and current provider identity
        # exists, a damaged artifact is a terminal local failure; silently
        # ignoring it would send the same audio to a provider again after a
        # tamper event.  A different configured model remains an independent
        # cache identity and is allowed to miss.
        try:
            _ensure_regular(path)
            raw_metadata = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError, _RangeInputInvalid) as exc:
            raise _RangeInputInvalid(
                "GEMINI_RANGE_CACHE_INVALID",
                f"acquisition metadata is unreadable: {path}: {exc}",
            ) from exc
        if not isinstance(raw_metadata, Mapping):
            raise _RangeInputInvalid(
                "GEMINI_RANGE_CACHE_INVALID",
                f"acquisition metadata is not an object: {path}",
            )
        without_digest = dict(raw_metadata)
        stored_digest = without_digest.pop("metadata_sha256", None)
        if stored_digest != _digest_json(without_digest):
            raise _RangeInputInvalid(
                "GEMINI_RANGE_CACHE_INVALID",
                f"acquisition metadata digest failed validation: {path}",
            )
        identity = (raw_metadata.get("provider"), raw_metadata.get("model"))
        if identity not in _provider_model_pairs(verifier):
            continue
        metadata = _cache_metadata_valid(
            path,
            verifier=verifier,
            geometry=geometry,
            prompt=prompt,
            prompt_sha=prompt_sha,
        )
        if metadata is None:
            eligible = (inherited is not None and any(
                (a['provider'], a['model']) == identity for a in inherited['attempts'])
                and inherited.get('unusable_acquisitions', {}).get(str(path)) == 'sha256:' + _sha256(path))
            inherited_missing: set[str] | None = set() if eligible else None
            unavailable: set[str] = set()
            intact_without_media = _cache_metadata_valid(path, verifier=verifier,
                geometry=geometry, prompt=prompt, prompt_sha=prompt_sha, unavailable=unavailable,
                inherited_missing=inherited_missing)
            if intact_without_media is not None and (unavailable or inherited_missing):
                continue  # keep the old acquisition; ordinary collection writes a new run
            raise _RangeInputInvalid(
                "GEMINI_RANGE_CACHE_INVALID",
                f"current provider/model acquisition artifacts failed validation: {path}",
            )
        return metadata
    return None


def _prepare_budget(verifier: _LocalAudioVerifier, receipt_path: Path):
    receipt_path = Path(receipt_path).absolute()
    budget = ensure_budget(verifier.source_media)
    with budget.lock, exclusive_native_audio_budget(receipt_path):
        resume_native_audio_budget(
            source_media=verifier.source_media,
            source_media_sha256=verifier.source_sha256,
            receipt_path=receipt_path,
            require_existing=receipt_path.exists(),
        )
        _persist_budget_receipt(
            source_media=verifier.source_media,
            source_sha256=verifier.source_sha256,
            receipt_path=receipt_path,
        )
    return budget


def _record_cache_hit(
    *, budget: Any, verifier: _LocalAudioVerifier, receipt_path: Path, metadata: Mapping[str, Any], geometry: Mapping[str, Any]
) -> None:
    with budget.lock, exclusive_native_audio_budget(receipt_path):
        resume_native_audio_budget(
            source_media=verifier.source_media,
            source_media_sha256=verifier.source_sha256,
            receipt_path=receipt_path,
            require_existing=True,
        )
        budget.record_cache_hit(
            provider=str(metadata["provider"]),
            model=str(metadata["model"]),
            start_ms=int(geometry["source_target_start_ms"]),
            end_ms=int(geometry["source_target_end_ms"]),
        )
        _persist_budget_receipt(
            source_media=verifier.source_media,
            source_sha256=verifier.source_sha256,
            receipt_path=receipt_path,
        )


def _finish_attempts(
    *, attempts: list[dict[str, Any]], budget: Any, verifier: _LocalAudioVerifier,
    receipt_path: Path, accepted: bool, observed: bool, response_rejected: bool = False,
    reason: str,
) -> None:
    if not attempts:
        return
    with budget.lock, exclusive_native_audio_budget(receipt_path):
        resume_native_audio_budget(
            source_media=verifier.source_media,
            source_media_sha256=verifier.source_sha256,
            receipt_path=receipt_path,
            require_existing=True,
        )
        for index, attempt in enumerate(attempts):
            if accepted and observed and index == len(attempts) - 1:
                status = "OBSERVED"
                detail = None
            elif response_rejected and index == len(attempts) - 1:
                status = "RESPONSE_REJECTED"
                detail = reason or "GEMINI_RANGE_RESPONSE_INVALID"
            else:
                status = "FAILED"
                detail = reason or "GEMINI_RANGE_PROVIDER_FAILED"
            budget.finish_attempt(int(attempt["attempt_id"]), status=status, reason_code=detail)
        _persist_budget_receipt(
            source_media=verifier.source_media,
            source_sha256=verifier.source_sha256,
            receipt_path=receipt_path,
        )


def _valid_provider_response(value: object, duration_ms: int) -> tuple[bool, str]:
    _, _, protocol_error = validate_range_response_protocol(
        value,
        duration_ms,
        segments_local_ms={"complete_block": [0, duration_ms]},
        target_local_ms=[0, duration_ms],
    )
    reasons = {
        "MISSING": "GEMINI_RANGE_RESPONSE_MISSING",
        "STATUS_INVALID": "GEMINI_RANGE_RESPONSE_STATUS_INVALID",
        "AUDIO_FLAG_INVALID": "GEMINI_RANGE_RESPONSE_AUDIO_FLAG_INVALID",
        "TRANSCRIPT_INVALID": "GEMINI_RANGE_RESPONSE_TRANSCRIPT_INVALID",
        "SEGMENTS_INVALID": "GEMINI_RANGE_RESPONSE_SEGMENTS_INVALID",
        "AUDIO_OBSERVATIONS_INVALID": "GEMINI_RANGE_AUDIO_OBSERVATIONS_INVALID",
        "GEOMETRY_INVALID": "GEMINI_RANGE_RESPONSE_GEOMETRY_INVALID",
        "EVIDENCE_EMPTY": "GEMINI_RANGE_RESPONSE_EVIDENCE_EMPTY",
    }
    if protocol_error is not None:
        return False, reasons.get(protocol_error, "GEMINI_RANGE_RESPONSE_INVALID")
    return True, ""


def _make_execution(
    *, provider: str, model: str, raw_path: Path, stdout_path: Path,
    provider_audio: Mapping[str, Any], provider_route: Mapping[str, Any],
    provider_provenance: Mapping[str, Any], response_source: Mapping[str, Any] | None,
    observed: object,
    response_error: str | None, provider_failure: str | None,
    provider_failures: list[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    successful_transport = isinstance(observed, Mapping)
    return {
        "schema_version": _EXECUTION_SCHEMA,
        "status": "COMPLETED" if successful_transport else "FAILED",
        "returncode": 0 if successful_transport else 1,
        "provider": provider,
        "model": model,
        "provider_audio": dict(provider_audio),
        "response": _record(raw_path),
        "stdout": _record(stdout_path),
        "response_source": dict(response_source) if response_source is not None else None,
        "provider_route": dict(provider_route),
        "provider_provenance": dict(provider_provenance),
        "failure_category": provider_failure,
        "provider_failures": [dict(row) for row in (provider_failures or []) if isinstance(row, Mapping)],
        "adapter_exception": None,
        "response_parse_error": response_error,
        "srt_mutation": False,
        "publication": "NOT_REQUESTED",
    }


def _store_provider_acquisition(
    *, verifier: _LocalAudioVerifier, geometry: Mapping[str, Any], prompt: str,
    prompt_sha: str, outcome: Any, run_dir: Path, observed: object,
    response_error: str | None, provider_failure: str | None,
) -> dict[str, Any]:
    provider = str(getattr(outcome, "provider", "gemini_api") or "gemini_api")
    model = str(getattr(outcome, "model", _entity_api_model()) or _entity_api_model())
    prompt_source = _path_value(getattr(outcome, "prompt_path", None))
    if prompt_source is not None and prompt_source.is_file() and not prompt_source.is_symlink():
        prompt_path = prompt_source
    else:
        prompt_path = run_dir / "prompt.md"
        _write_immutable(prompt_path, prompt.encode("utf-8"))
    if prompt_path.read_text(encoding="utf-8") != prompt:
        raise _RangeInputInvalid("GEMINI_RANGE_PROMPT_ARTIFACT_DRIFT")

    response_source = _path_value(getattr(outcome, "response_path", None))
    raw_path: Path
    raw_mapping = observed if isinstance(observed, Mapping) else None
    transport_response_error: str | None = None
    response_source_record: dict[str, str] | None = None
    if response_source is not None:
        try:
            _ensure_regular(response_source)
        except (OSError, _RangeInputInvalid) as exc:
            if raw_mapping is not None:
                raise _RangeInputInvalid(
                    "GEMINI_RANGE_RESPONSE_SOURCE_MISSING", str(exc)
                ) from exc
            response_source = None
        else:
            response_source_record = _record(response_source)
            if raw_mapping is not None:
                # The parsed provider response is authoritative only when it
                # can be reproduced from the actual transport bytes.  A
                # fenced JSON response gets a normalized *derived* artifact
                # for the existing validator, while the original path/hash
                # remains bound in ``response_source`` below.
                source_mapping = _read_transport_json(response_source)
                if source_mapping != raw_mapping:
                    # Keep the actual transport bytes as a terminal rejected
                    # acquisition.  Do not synthesize a replacement response
                    # from ``observed`` and do not let this drift become CPA
                    # authority on a later cache read.
                    transport_response_error = "GEMINI_RANGE_RESPONSE_SOURCE_DRIFT"
                    raw_mapping = None
                    raw_path = response_source
                else:
                    try:
                        direct_mapping = json.loads(response_source.read_text(encoding="utf-8"))
                    except (OSError, UnicodeError, ValueError):
                        direct_mapping = None
                    if isinstance(direct_mapping, Mapping) and direct_mapping == raw_mapping:
                        raw_path = response_source
                    else:
                        raw_path = run_dir / "raw-response.normalized.json"
                        _write_json(raw_path, dict(raw_mapping))
            else:
                raw_path = response_source
    else:
        raw_path = run_dir / "raw-response.json"
        if raw_mapping is not None:
            raise _RangeInputInvalid(
                "GEMINI_RANGE_RESPONSE_SOURCE_MISSING",
                "provider returned parsed JSON without its transport response path",
            )
        else:
            _write_immutable(raw_path, b"")

    rendered_audio_path = run_dir / "input.mp4"
    provider_audio_path = (
        run_dir / "input.gemini-api.mp3"
        if provider == "gemini_api"
        else rendered_audio_path
    )
    try:
        _ensure_regular(rendered_audio_path)
        _ensure_regular(provider_audio_path)
    except (OSError, _RangeInputInvalid) as exc:
        raise _RangeInputInvalid("GEMINI_RANGE_PROVIDER_AUDIO_MISSING", str(exc)) from exc
    provider_audio_record = _record(provider_audio_path)
    rendered_audio_record = _record(rendered_audio_path)
    provider_route = _provider_route_metadata(outcome, provider)
    provider_provenance = _safe_provider_provenance(outcome)

    stdout_path = run_dir / "provider.stdout"
    for candidate in (
        run_dir / "agy.stdout",
        run_dir / "gemini.stdout",
        _path_value(getattr(outcome, "stdout_path", None)),
    ):
        if candidate is not None and candidate.is_file() and not candidate.is_symlink():
            stdout_path = candidate
            break
    if not stdout_path.is_file():
        _write_immutable(stdout_path, b"")

    execution = _make_execution(
        provider=provider, model=model, raw_path=raw_path, stdout_path=stdout_path,
        provider_audio=provider_audio_record, provider_route=provider_route,
        provider_provenance=provider_provenance, response_source=response_source_record,
        observed=observed,
        response_error=transport_response_error or response_error,
        provider_failure=provider_failure,
        provider_failures=getattr(outcome, "provider_failures", None),
    )
    execution_path = run_dir / "execution.json"
    _write_json(execution_path, execution)
    paths = {
        "audio_source": _record(run_dir / "input.mp3"),
        "audio_clip": dict(provider_audio_record),
        "actual_provider_audio": dict(provider_audio_record),
        "rendered_audio_clip": dict(rendered_audio_record),
        "prompt": _record(prompt_path),
        "raw_response": _record(raw_path),
        "provider_stdout": _record(stdout_path),
        "execution": _record(execution_path),
    }
    metadata = {
        "schema_version": _ACQUISITION_SCHEMA,
        "prompt_contract": geometry.get("prompt_contract", _PROMPT_CONTRACT),
        "prompt_sha256": prompt_sha,
        "source_media": str(Path(verifier.source_media).resolve()),
        "source_media_sha256": verifier.source_sha256,
        "source_stat_binding": list(verifier.source_stat_binding),
        "source_target_ms": [geometry["source_target_start_ms"], geometry["source_target_end_ms"]],
        "provider": provider,
        "model": model,
        "status": (
            str(observed.get("status"))
            if isinstance(observed, Mapping)
            and observed.get("status") in {"OBSERVED", "UNCERTAIN"}
            else "UNCERTAIN"
        ),
        "raw_status": observed.get("status") if isinstance(observed, Mapping) else None,
        "candidate_blind": geometry.get("context_assistance") is None,
        "raw_is_json": isinstance(raw_mapping, Mapping),
        "response_error": transport_response_error or response_error,
        "provider_failure": provider_failure,
        "response_source": response_source_record,
        "provider_route": provider_route,
        "provider_provenance": provider_provenance,
        "actual_provider_audio": dict(provider_audio_record),
        "rendered_audio_clip": dict(rendered_audio_record),
        "provider_failures": [
            dict(row)
            for row in (getattr(outcome, "provider_failures", None) or [])
            if isinstance(row, Mapping)
        ],
        "paths": paths,
    }
    metadata["metadata_sha256"] = _digest_json(metadata)
    _write_json(run_dir / "acquisition.json", metadata)
    return metadata


def _materialize_current_observation(
    *, verifier: _LocalAudioVerifier, check_request: Mapping[str, Any], geometry: Mapping[str, Any],
    srt_text: str, rows: list[dict[str, Any]], blind_request: Mapping[str, Any],
    prompt: str, metadata: Mapping[str, Any], current_request_sha: str,
    served_from_cache: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    paths = metadata["paths"]
    candidate_digest = _digest_bytes(str(geometry["candidate_id"]).encode())[:16]
    srt_sha = _digest_bytes(srt_text.encode("utf-8"))
    acquisition_id = Path(paths["execution"]["path"]).parent.name
    duration = int(geometry["crop_end_ms"] - geometry["crop_start_ms"])
    input_doc = {
        "schema_version": blind_request["schema_version"],
        "candidate_id": geometry["candidate_id"],
        "provider": metadata["provider"],
        "model": metadata["model"],
        "candidate_blind": geometry.get("context_assistance") is None,
        "candidate_text_in_provider_prompt": geometry.get("context_assistance") is not None,
        **({"observation_class": "CONTEXT_ASSISTED_AUDIO", "independent_witness": False,
            "assistance": geometry["context_assistance"],
            "prior_blind_ranges": geometry.get("prior_blind_ranges", []),
            "prior_blind_narrow": geometry.get("prior_blind_narrow", [])}
           if geometry.get("context_assistance") is not None else {}),
        "srt_mutation": False,
        "publication": "NOT_REQUESTED",
        "cue_indexes": list(geometry["cue_indexes"]),
        "delivery_target_ms": [geometry["matched_start_ms"], geometry["matched_end_ms"]],
        "source_media_timeline_offset_ms": geometry["source_offset_ms"],
        "source_target_ms": [geometry["source_target_start_ms"], geometry["source_target_end_ms"]],
        "source_crop_ms": [geometry["crop_start_ms"], geometry["crop_end_ms"]],
        "delivery_crop_ms": [
            geometry["crop_start_ms"] - geometry["source_offset_ms"],
            geometry["crop_end_ms"] - geometry["source_offset_ms"],
        ],
        "target_local_ms": [0, duration],
        "segments_local_ms": {"complete_block": [0, duration]},
        "source_media": {"path": str(verifier.source_media), "sha256": verifier.source_sha256},
        "audio_clip": dict(paths["audio_clip"]),
        "actual_provider_audio": dict(paths["actual_provider_audio"]),
        "rendered_audio_clip": dict(paths["rendered_audio_clip"]),
        "prompt": dict(paths["prompt"]),
        "range_request": dict(blind_request),
    }
    # 41（证据身份＝内容，不认路径/目录）：同一缓存采集可被不同 finding 以不同
    # assistance／先验盲听／范围请求投影；目录按投影内容寻址，各自落盘不冲突，
    # 同内容重放仍落同一目录并按字节核对。
    projection_digest = _digest_json(input_doc)[:16]
    current_dir = (
        Path(verifier.output_dir)
        / "gemini-range-context"
        / current_request_sha[:20]
        / srt_sha[:20]
        / candidate_digest
        / acquisition_id
        / projection_digest
    )
    current_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    reviewed_srt = current_dir / "reviewed.srt"
    _write_immutable(reviewed_srt, srt_text.encode("utf-8"))
    input_doc["reviewed_srt"] = _record(reviewed_srt)
    input_path = current_dir / "segmented_input.json"
    _write_json(input_path, input_doc)
    raw_path = Path(paths["raw_response"]["path"])
    stdout_path = Path(paths["provider_stdout"]["path"])
    execution_source = _read_json_path(Path(paths["execution"]["path"])) or {}
    execution = dict(execution_source)
    execution["response"] = _record(raw_path)
    execution["stdout"] = _record(stdout_path)
    execution_path = current_dir / "execution.json"
    _write_json(execution_path, execution)
    bindings = {
        "segmented_input_path": str(input_path),
        "segmented_input_sha256": _file_sha(input_path),
        "execution_path": str(execution_path),
        "execution_sha256": _file_sha(execution_path),
        "raw_response_path": str(raw_path),
        "raw_response_sha256": _file_sha(raw_path),
        "provider_stdout_path": str(stdout_path),
        "provider_stdout_sha256": _file_sha(stdout_path),
        "prompt_path": str(paths["prompt"]["path"]),
        "prompt_sha256": _file_sha(Path(paths["prompt"]["path"])),
        "audio_clip_path": str(paths["audio_clip"]["path"]),
        "audio_clip_sha256": _bound_media_digest(paths["audio_clip"]),
        "source_media_path": str(verifier.source_media),
        "source_media_sha256": verifier.source_sha256,
        "reviewed_srt_path": str(reviewed_srt),
        "reviewed_srt_sha256": _file_sha(reviewed_srt),
    }
    observation = {"artifact_bindings": bindings}
    return observation, input_doc


def _augment_uncertain(
    row: Mapping[str, Any], *, geometry: Mapping[str, Any], input_doc: Mapping[str, Any],
    bindings: Mapping[str, Any], reason_code: str, detail: str,
) -> dict[str, Any]:
    result = dict(row)
    result.update(
        schema_version=RANGE_ACOUSTIC_CONTEXT_SCHEMA,
        status="UNCERTAIN",
        authority="EVIDENCE_ONLY",
        mutation_authorized=False,
        candidate_blind=True,
        candidate_id=geometry["candidate_id"],
        provider=input_doc.get("provider"),
        model=input_doc.get("model"),
        evidence_scope="RANGE_CONTEXT_ONLY",
        transcript_scope="RANGE_WITHOUT_SINGLE_CUE_TIMESTAMPS",
        context_only=True,
        reason_code=reason_code,
        reason_detail=_safe_detail(detail),
        artifact_bindings=dict(bindings),
        range={
            "cue_indexes": list(geometry["cue_indexes"]),
            "matched_start_ms": geometry["matched_start_ms"],
            "matched_end_ms": geometry["matched_end_ms"],
        },
        source_window={
            "crop_start_ms": geometry["crop_start_ms"],
            "crop_end_ms": geometry["crop_end_ms"],
        },
        verdict={"exact_transcript": "", "original_provider_response": {}},
    )
    return result


def observe_gemini_range_context(
    *,
    verifier: _LocalAudioVerifier,
    check_request: Mapping[str, Any],
    range_witness_request: Mapping[str, Any],
    budget_receipt_path: Path,
    context_assistance: Mapping[str, Any] | None = None,
) -> list[Mapping[str, Any]]:
    """Observe one exact complete block through the ordinary Gemini ladder."""

    candidate_id: str | None = None
    provider_hint = "gemini_api"
    model_hint = str(getattr(verifier, "model", "") or "")
    geometry: dict[str, Any] | None = None
    attempts: list[dict[str, Any]] = []
    dispatch_persist_failed = False
    attempts_finished = False
    budget = None
    try:
        geometry, _current, _broad, srt_text, rows = _validate_source_and_geometry(
            verifier=verifier, check_request=check_request, range_witness_request=range_witness_request
        )
        candidate_id = str(geometry["candidate_id"])
        blind = _blind_request(geometry)
        if context_assistance is not None:
            from src.autoslice import context_assisted_audio as assisted
            assistance = assisted.validate_assistance(context_assistance, check_request)
            prior_ids = assisted.validate_prior_blind(check_request)
            if not set(assistance["prior_blind_observation_ids"]).issubset(prior_ids):
                raise _RangeInputInvalid("ASSISTED_AUDIO_BLIND_PREREQUISITE_DRIFT")
            geometry.update(context_assistance=assistance, prompt_contract=assisted.PROMPT_CONTRACT,
                            prior_blind_ranges=[row for row in check_request.get("context_only_range_acoustic_observations", [])
                                                if row.get("candidate_blind") is True],
                            prior_blind_narrow=check_request.get("prior_acoustic_observations", []))
            blind = {**blind, "schema_version": assisted.REQUEST_SCHEMA, "assistance": assistance}
            prompt = assisted.prompt(blind)
        else:
            prompt = range_transcript_prompt(blind)
        prompt_sha = _digest_bytes(prompt.encode("utf-8"))
        current_request = build_witness_request(check_request)
        current_request_sha = str(current_request["request_sha256"])
        budget = _prepare_budget(verifier, Path(budget_receipt_path))

        cached = _find_cache(
            verifier=verifier, geometry=geometry, prompt=prompt, prompt_sha=prompt_sha,
            receipt_path=Path(budget_receipt_path),
        )
        if cached is not None:
            try:
                _record_cache_hit(
                    budget=budget,
                    verifier=verifier,
                    receipt_path=Path(budget_receipt_path).absolute(),
                    metadata=cached,
                    geometry=geometry,
                )
            except Exception as exc:
                return [
                    _request_uncertain(
                        candidate_id=candidate_id,
                        provider=str(cached.get("provider", provider_hint)),
                        model=str(cached.get("model", model_hint)),
                        reason_code="GEMINI_RANGE_CACHE_LEDGER_PERSIST_FAILED",
                        detail=str(exc),
                        geometry=geometry,
                    )
                ]
            observation, input_doc = _materialize_current_observation(
                verifier=verifier, check_request=check_request, geometry=geometry,
                srt_text=srt_text, rows=rows, blind_request=blind, prompt=prompt,
                metadata=cached, current_request_sha=current_request_sha,
                served_from_cache=True,
            )
            validated, reason = validate_segmented_range_context_observation(
                srt_text, check_request, observation
            )
            if validated is not None:
                validated["served_from_cache"] = True
                return [validated]
            return [
                _augment_uncertain(
                    {}, geometry=geometry, input_doc=input_doc,
                    bindings=observation["artifact_bindings"],
                    reason_code=(
                        str(cached.get("response_error"))
                        if cached.get("response_error")
                        else reason or "GEMINI_RANGE_CACHED_OBSERVATION_INVALID"
                    ),
                    detail="cached acquisition was immutable but current validation failed",
                )
            ]

        acquisition_root = _acquisition_root(verifier, geometry, prompt_sha)
        run_dir = acquisition_root / f"run-{uuid.uuid4().hex}"
        run_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
        mp3_path = run_dir / "input.mp3"
        mp4_path = run_dir / "input.mp4"
        _extract_exact_mp3(
            Path(verifier.source_media), mp3_path,
            int(geometry["source_target_start_ms"]), int(geometry["source_target_end_ms"]),
        )
        cropped, crop_detail = _crop_black_frame_audio(
            source_media=Path(verifier.source_media), audio_path=mp4_path,
            start_ms=int(geometry["source_target_start_ms"]),
            end_ms=int(geometry["source_target_end_ms"]),
        )
        if not cropped:
            raise _RangeInputInvalid("GEMINI_RANGE_AUDIO_CROP_FAILED", crop_detail)
        # Extraction is a source read.  Rebind the physical media immediately
        # before the first provider reservation so a replacement recording
        # cannot be charged or sent under the old request identity.
        if (
            _source_stat_binding(Path(verifier.source_media))
            != tuple(verifier.source_stat_binding)
            or _sha256(Path(verifier.source_media)) != verifier.source_sha256
        ):
            raise _RangeInputInvalid("GEMINI_RANGE_SOURCE_DRIFT_BEFORE_PROVIDER")

        input_audio_sha = _file_sha(mp3_path)
        claim_directories = _claim_directories(Path(budget_receipt_path), Path(verifier.output_dir))
        original = _read_back_prior_request(
            budget, verifier=verifier, receipt_path=Path(budget_receipt_path),
            directories=claim_directories, geometry=geometry, prompt=prompt,
            prompt_sha=prompt_sha, input_audio_sha=input_audio_sha,
        )
        if original is not None:
            observation, input_doc = _materialize_current_observation(
                verifier=verifier, check_request=check_request, geometry=geometry,
                srt_text=srt_text, rows=rows, blind_request=blind, prompt=prompt,
                metadata=original, current_request_sha=current_request_sha,
                served_from_cache=True,
            )
            validated, reason = validate_segmented_range_context_observation(
                srt_text, check_request, observation
            )
            if validated is not None:
                validated["served_from_cache"] = True
                return [validated]
            return [
                _augment_uncertain(
                    {}, geometry=geometry, input_doc=input_doc,
                    bindings=observation["artifact_bindings"],
                    reason_code=(str(original.get("response_error")) if original.get("response_error")
                                 else reason or "GEMINI_RANGE_CACHED_OBSERVATION_INVALID"),
                    detail="original acquisition was read back but current validation failed",
                )
            ]

        def on_provider_dispatch(provider: str, model: str) -> None:
            nonlocal dispatch_persist_failed
            start_ms = int(geometry["source_target_start_ms"])
            end_ms = int(geometry["source_target_end_ms"])
            identity = _range_request_identity(
                provider=provider, model=model, source_sha256=str(verifier.source_sha256),
                start_ms=start_ms, end_ms=end_ms, prompt_sha256=prompt_sha,
                input_audio_sha256=input_audio_sha,
            )
            try:
                with budget.lock, exclusive_native_audio_budget(Path(budget_receipt_path).absolute()):
                    resume_native_audio_budget(
                        source_media=verifier.source_media,
                        source_media_sha256=verifier.source_sha256,
                        receipt_path=Path(budget_receipt_path).absolute(),
                        require_existing=True,
                    )
                    try:
                        basis = _resend_basis(
                            budget, directories=claim_directories,
                            source_sha256=str(verifier.source_sha256), provider=provider,
                            model=model, start_ms=start_ms, end_ms=end_ms, identity=identity,
                        )
                    except _PriorRequestUnknown as refusal:
                        budget.record_refusal(provider=provider, model=model, start_ms=start_ms,
                                              end_ms=end_ms, reason_code=refusal.reason_code)
                        _persist_budget_receipt(
                            source_media=verifier.source_media,
                            source_sha256=verifier.source_sha256,
                            receipt_path=Path(budget_receipt_path).absolute(),
                        )
                        raise
                    attempt_id = budget.consume(provider, model, start_ms, end_ms)
                    attempts.append({"attempt_id": attempt_id, "provider": provider, "model": model})
                    _persist_budget_receipt(
                        source_media=verifier.source_media,
                        source_sha256=verifier.source_sha256,
                        receipt_path=Path(budget_receipt_path).absolute(),
                    )
                    # The request identity is recorded beside the account it
                    # occupies, so every later output root sees the claim.
                    _write_json(
                        claim_directories[0] / f"{identity}.{attempt_id}.json",
                        {"schema_version": DISPATCH_CLAIM_SCHEMA,
                         "request_identity_sha256": identity, "attempt_id": attempt_id,
                         "provider": provider, "model": model,
                         "source_media_sha256": str(verifier.source_sha256),
                         "start_ms": start_ms, "end_ms": end_ms,
                         "prompt_sha256": prompt_sha, "input_audio_sha256": input_audio_sha,
                         # Diagnostic locator of the original run, not identity.
                         "run_dir": str(run_dir), "resend_basis": basis},
                    )
            except _PriorRequestUnknown:
                raise
            except Exception as exc:
                dispatch_persist_failed = True
                raise _DispatchPersistenceFailed(str(exc)) from exc

        outcome = _observe_entity_audio(
            request=blind,
            candidates=[],
            audio_path=mp4_path,
            job_dir=run_dir,
            recording_date=str(getattr(verifier, "recording_date", "")),
            timely_context="",
            binary=str(getattr(verifier, "binary", "")),
            model=str(getattr(verifier, "model", "")),
            timeout=str(getattr(verifier, "timeout", "")),
            agy_quota_circuit=verifier.agy_quota_circuit,
            web_unavailable=verifier.web_unavailable,
            on_provider_dispatch=on_provider_dispatch,
        )
        provider_hint = str(getattr(outcome, "provider", provider_hint) or provider_hint)
        model_hint = str(getattr(outcome, "model", model_hint) or model_hint)
        observed = getattr(outcome, "observed", None)
        shaped = observed
        if context_assistance is not None:
            # Assisted replies carry textual hearing notes, not sound events.
            shaped, _notes = assisted.split_text_notes(observed)
        valid_response, response_error = _valid_provider_response(shaped, int(geometry["crop_end_ms"] - geometry["crop_start_ms"]))
        if context_assistance is not None:
            try:
                assisted.validate_response(observed, context_assistance)
                # A typed, useful uncertainty is an observation too. It must
                # still have the same legal segment/audio protocol.
                if response_error == "GEMINI_RANGE_RESPONSE_EVIDENCE_EMPTY" and observed.get("target_audible") is True:
                    valid_response, response_error = True, ""
            except (ValueError, TypeError, KeyError) as exc:
                valid_response, response_error = False, str(exc)
        provider_failure = None
        failures = getattr(outcome, "provider_failures", None)
        if not valid_response:
            response_error = response_error or "GEMINI_RANGE_RESPONSE_INVALID"
        if not isinstance(observed, Mapping):
            if isinstance(failures, list) and failures:
                provider_failure = _safe_detail(failures[-1].get("category", "GEMINI_RANGE_PROVIDER_FAILED"))
            else:
                provider_failure = "GEMINI_RANGE_PROVIDER_FAILED"
        metadata = _store_provider_acquisition(
            verifier=verifier, geometry=geometry, prompt=prompt, prompt_sha=prompt_sha,
            outcome=outcome, run_dir=run_dir, observed=observed,
            response_error=None if valid_response else response_error,
            provider_failure=provider_failure,
        )
        stored_response_error = metadata.get("response_error")
        if isinstance(stored_response_error, str) and stored_response_error:
            valid_response = False
            response_error = stored_response_error
        if dispatch_persist_failed:
            raise _DispatchPersistenceFailed("provider dispatch reservation persistence failed")
        response_rejected = bool(
            isinstance(failures, list)
            and any(
                "INVALID_OUTPUT" in str(row.get("category", ""))
                or "INVALID_JSON" in str(row.get("category", ""))
                for row in failures
                if isinstance(row, Mapping)
            )
        ) or (isinstance(observed, Mapping) and not valid_response)
        _finish_attempts(
            attempts=attempts, budget=budget, verifier=verifier,
            receipt_path=Path(budget_receipt_path).absolute(),
            accepted=valid_response, observed=isinstance(observed, Mapping),
            response_rejected=response_rejected,
            reason=response_error or provider_failure or "",
        )
        attempts_finished = True
        observation, input_doc = _materialize_current_observation(
            verifier=verifier, check_request=check_request, geometry=geometry,
            srt_text=srt_text, rows=rows, blind_request=blind, prompt=prompt,
            metadata=metadata, current_request_sha=current_request_sha,
            served_from_cache=False,
        )
        validated, reason = validate_segmented_range_context_observation(
            srt_text, check_request, observation
        )
        if validated is not None:
            return [validated]
        return [
            _augment_uncertain(
                {}, geometry=geometry, input_doc=input_doc,
                bindings=observation["artifact_bindings"],
                reason_code=response_error or provider_failure or reason or "GEMINI_RANGE_OBSERVATION_INVALID",
                detail=response_error or provider_failure or "range observation failed current validation",
            )
        ]
    except _PriorRequestUnknown as exc:
        # Not dispatched: earlier attempts keep their rows; the refusal is recorded.
        # Attempts of an earlier ladder step in this call did run and failed.
        if budget is not None and attempts and not attempts_finished:
            try:
                _finish_attempts(
                    attempts=attempts, budget=budget, verifier=verifier,
                    receipt_path=Path(budget_receipt_path).absolute(),
                    accepted=False, observed=False, reason="GEMINI_RANGE_PROVIDER_FAILED",
                )
            except Exception:
                pass
        return [
            _request_uncertain(
                candidate_id=candidate_id,
                provider=provider_hint,
                model=model_hint,
                reason_code=exc.reason_code,
                detail=exc.detail,
                geometry=geometry,
            )
        ]
    except _DispatchPersistenceFailed as exc:
        if budget is not None and attempts and not dispatch_persist_failed and not attempts_finished:
            try:
                _finish_attempts(
                    attempts=attempts, budget=budget, verifier=verifier,
                    receipt_path=Path(budget_receipt_path).absolute(),
                    accepted=False, observed=False, reason="GEMINI_RANGE_BUDGET_PERSIST_FAILED",
                )
            except Exception:
                pass
        return [
            _request_uncertain(
                candidate_id=candidate_id,
                provider=provider_hint,
                model=model_hint,
                reason_code="GEMINI_RANGE_BUDGET_PERSIST_FAILED",
                detail=str(exc),
                geometry=geometry,
            )
        ]
    except Exception as exc:
        reason = getattr(exc, "reason_code", None) or "GEMINI_RANGE_OBSERVATION_FAILED"
        detail = getattr(exc, "detail", None) or str(exc)
        if budget is not None and attempts and not dispatch_persist_failed and not attempts_finished:
            try:
                _finish_attempts(
                    attempts=attempts, budget=budget, verifier=verifier,
                    receipt_path=Path(budget_receipt_path).absolute(),
                    accepted=False, observed=False, response_rejected=True,
                    reason=str(reason),
                )
            except Exception:
                # Leave the canonical receipt pending when its terminal write
                # cannot be persisted; a later resume may seal the same
                # attempt, but this invocation cannot claim authority.
                pass
        return [
            _request_uncertain(
                candidate_id=candidate_id,
                provider=provider_hint,
                model=model_hint,
                reason_code=str(reason),
                detail=str(detail),
                geometry=geometry,
            )
        ]


__all__ = ["RANGE_REQUEST_SCHEMA", "range_transcript_prompt", "observe_gemini_range_context"]
