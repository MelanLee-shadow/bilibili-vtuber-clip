"""Reuse a verified subtitle-audio witness for a same-audio successor.

The normal final subtitle-audio capture path obtains a fresh BCUT witness.  A
same-BV successor may change subtitle pixels while retaining the exact source
audio.  This module is the narrow, explicit path for that case:

* validate the complete parent gate closure;
* extract the successor's audio with the existing default FFmpeg extractor;
* prefer an exact extracted-MP3 hash match with the parent's recorded hash;
* when only historical encoder bytes drift, require the current extractor to
  produce identical parent/successor MP3 bytes and require both complete native
  PCM and the mono-16-kHz BCUT-input PCM to be byte-identical;
* carry the parent's raw BCUT result and witness SRT byte-for-byte;
* write new provenance and correspondence envelopes bound to the successor;
* run the existing native validator on the returned successor record.

There is deliberately no transcriber adapter here.  ``_extract_audio`` is a
private test-only seam; production calls omit it and use
``final_subtitle_audio_gate._default_extract_audio``.  Provider identity in
the new envelopes identifies the source of the reused witness.  The explicit
``audio_reuse`` object records that this run made zero new provider/ASR calls.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from src.autoslice import final_subtitle_audio_gate as gate


REUSE_SCHEMA_VERSION = "final-subtitle-audio-audio-reuse.v1"
PCM_IDENTITY_SCHEMA_VERSION = "final-subtitle-audio-decoded-pcm-identity.v1"
RECORD_FILENAME = "AUDIO-VERIFIED-RECORD.json"
PROVIDER_NAME = gate.PROVIDER_NAME
PROVIDER_ROUTE = "reused-existing-parent-bcut-receipt"


class FinalSubtitleAudioReuseError(ValueError):
    """A parent witness cannot be safely reused for the successor."""

    def __init__(self, reason_code: str, detail: str = "") -> None:
        self.reason_code = reason_code
        self.detail = detail
        message = reason_code if not detail else f"{reason_code}: {detail}"
        super().__init__(message)


@dataclass(frozen=True)
class FinalSubtitleAudioReuseResult:
    """Artifacts and receipts returned by :func:`reuse_final_subtitle_audio_check`."""

    record: dict[str, Any]
    parent_receipt: dict[str, Any]
    current_receipt: dict[str, Any]
    audio_sha256: str
    parent_audio_sha256: str
    artifact_paths: dict[str, Path]
    record_path: Path
    record_sha256: str

    @property
    def status(self) -> str:
        return str(self.current_receipt.get("status") or "")


def _hash_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _sha256(path: str | Path) -> str:
    try:
        return gate.sha256_file(path)
    except (OSError, ValueError) as exc:
        raise FinalSubtitleAudioReuseError("AUDIO_REUSE_INPUT_UNAVAILABLE", str(exc)) from exc


def _ffmpeg_pcm_sha256(media: Path, *, mono_16khz: bool) -> str:
    """Hash the complete decoded PCM stream without retaining audio bytes."""

    command = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-v",
        "error",
        "-i",
        str(media),
        "-map",
        "0:a:0",
        "-vn",
    ]
    if mono_16khz:
        command.extend(["-ac", "1", "-ar", "16000"])
    command.extend(["-c:a", "pcm_s16le", "-f", "hash", "-hash", "sha256", "-"])
    try:
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=900,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise FinalSubtitleAudioReuseError("AUDIO_REUSE_PCM_DECODE_FAILED", str(exc)) from exc
    output = completed.stdout.strip()
    if (
        completed.returncode != 0
        or completed.stderr.strip()
        or re.fullmatch(r"SHA256=[a-f0-9]{64}", output) is None
    ):
        raise FinalSubtitleAudioReuseError(
            "AUDIO_REUSE_PCM_DECODE_FAILED",
            f"rc={completed.returncode} stderr={completed.stderr[-500:]!r} stdout={output!r}",
        )
    return "sha256:" + output.split("=", 1)[1]


def _decoded_pcm_identity(media: Path) -> dict[str, str]:
    return {
        "schema_version": PCM_IDENTITY_SCHEMA_VERSION,
        "native_pcm_s16le_sha256": _ffmpeg_pcm_sha256(media, mono_16khz=False),
        "bcut_input_mono_16000_pcm_s16le_sha256": _ffmpeg_pcm_sha256(
            media, mono_16khz=True
        ),
    }


def _validated_pcm_identity(value: object, *, label: str) -> dict[str, str]:
    if not isinstance(value, Mapping) or value.get("schema_version") != PCM_IDENTITY_SCHEMA_VERSION:
        raise FinalSubtitleAudioReuseError("AUDIO_REUSE_PCM_IDENTITY_INVALID", label)
    return {
        "schema_version": PCM_IDENTITY_SCHEMA_VERSION,
        "native_pcm_s16le_sha256": _canonical_sha(
            value.get("native_pcm_s16le_sha256"), label=f"{label}_native_pcm"
        ),
        "bcut_input_mono_16000_pcm_s16le_sha256": _canonical_sha(
            value.get("bcut_input_mono_16000_pcm_s16le_sha256"),
            label=f"{label}_bcut_input_pcm",
        ),
    }


def _regular_file(value: str | Path, *, label: str) -> Path:
    try:
        return gate._regular_file(value, label=label)
    except gate.FinalSubtitleAudioGateError as exc:
        raise FinalSubtitleAudioReuseError("AUDIO_REUSE_INPUT_UNAVAILABLE", str(exc)) from exc


def _regular_directory(value: str | Path, *, label: str, must_exist: bool = True) -> Path:
    candidate = Path(value)
    if candidate.is_symlink():
        raise FinalSubtitleAudioReuseError("AUDIO_REUSE_DIRECTORY_INVALID", f"{label} is a symlink: {candidate}")
    if must_exist:
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise FinalSubtitleAudioReuseError("AUDIO_REUSE_DIRECTORY_UNAVAILABLE", f"{label}: {candidate}: {exc}") from exc
        if not resolved.is_dir():
            raise FinalSubtitleAudioReuseError("AUDIO_REUSE_DIRECTORY_INVALID", f"{label} is not a directory: {candidate}")
        return resolved
    if candidate.exists() and not candidate.is_dir():
        raise FinalSubtitleAudioReuseError("AUDIO_REUSE_DIRECTORY_INVALID", f"{label} is not a directory: {candidate}")
    parent = candidate.parent
    try:
        resolved_parent = parent.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise FinalSubtitleAudioReuseError("AUDIO_REUSE_DIRECTORY_UNAVAILABLE", f"parent of {label}: {exc}") from exc
    return resolved_parent / candidate.name


def _read_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise FinalSubtitleAudioReuseError("AUDIO_REUSE_JSON_INVALID", f"{label}: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise FinalSubtitleAudioReuseError("AUDIO_REUSE_JSON_INVALID", f"{label} must be an object")
    return value


def _canonical_sha(value: object, *, label: str) -> str:
    try:
        return gate._canonical_sha(value, label=label)
    except gate.FinalSubtitleAudioGateError as exc:
        raise FinalSubtitleAudioReuseError("AUDIO_REUSE_HASH_INVALID", str(exc)) from exc


def _safe_candidate_id(parent_record: Mapping[str, object], current_record: Mapping[str, object]) -> str:
    parent_id = parent_record.get("candidate_id")
    current_id = current_record.get("candidate_id")
    if not isinstance(parent_id, str) or not isinstance(current_id, str) or parent_id != current_id:
        raise FinalSubtitleAudioReuseError(
            "AUDIO_REUSE_CANDIDATE_ID_MISMATCH",
            f"parent={parent_id!r} current={current_id!r}",
        )
    try:
        return gate._safe_candidate_id(parent_id)
    except gate.FinalSubtitleAudioGateError as exc:
        raise FinalSubtitleAudioReuseError("AUDIO_REUSE_CANDIDATE_ID_INVALID", str(exc)) from exc


def _artifact_paths(directory: Path, candidate_id: str) -> dict[str, Path]:
    try:
        return gate._artifact_paths(directory, candidate_id)
    except gate.FinalSubtitleAudioGateError as exc:
        raise FinalSubtitleAudioReuseError("AUDIO_REUSE_ARTIFACT_PATH_INVALID", str(exc)) from exc


def _load_record_hash(
    record: Mapping[str, object],
    source: str | Path | None,
    *,
    label: str,
) -> str:
    """Hash an optional record source and ensure it matches the supplied mapping."""

    if source is None:
        try:
            payload = (json.dumps(dict(record), ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise FinalSubtitleAudioReuseError("AUDIO_REUSE_RECORD_INVALID", f"{label}: {exc}") from exc
        return _hash_bytes(payload)
    path = _regular_file(source, label=label)
    observed = _read_json(path, label=label)
    if observed != dict(record):
        raise FinalSubtitleAudioReuseError(
            "AUDIO_REUSE_RECORD_SOURCE_DRIFT",
            f"{label} bytes do not equal the supplied record mapping: {path}",
        )
    return _sha256(path)


def _record_audio_sha(record: Mapping[str, object], *, label: str) -> str:
    evidence = record.get("subtitle_audio_correspondence")
    if not isinstance(evidence, Mapping):
        raise FinalSubtitleAudioReuseError("AUDIO_REUSE_PARENT_EVIDENCE_MISSING", f"{label} has no subtitle_audio_correspondence")
    audio = _canonical_sha(evidence.get("audio_sha256"), label=f"{label}_audio")
    artifacts = record.get("artifact_hashes")
    if isinstance(artifacts, Mapping) and artifacts.get("subtitle_audio_extracted_audio_sha256") is not None:
        artifact_audio = _canonical_sha(
            artifacts.get("subtitle_audio_extracted_audio_sha256"),
            label=f"{label}_artifact_audio",
        )
        if artifact_audio != audio:
            raise FinalSubtitleAudioReuseError(
                "AUDIO_REUSE_PARENT_AUDIO_HASH_DRIFT",
                f"record evidence={audio} artifact_hashes={artifact_audio}",
            )
    return audio


def _receipt_path_rewrite(value: object, *, source_root: Path, destination_root: Path) -> object:
    if isinstance(value, Mapping):
        return {
            str(key): _receipt_path_rewrite(item, source_root=source_root, destination_root=destination_root)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            _receipt_path_rewrite(item, source_root=source_root, destination_root=destination_root)
            for item in value
        ]
    if isinstance(value, str):
        source_text = str(source_root)
        if value == source_text:
            return str(destination_root)
        prefix = source_text + os.sep
        if value.startswith(prefix):
            return str(destination_root / value[len(prefix):])
    return value


def _write_once(path: Path, payload: bytes) -> None:
    """Write an invocation-owned artifact without overwriting different bytes."""

    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file():
            raise FinalSubtitleAudioReuseError("AUDIO_REUSE_DESTINATION_DRIFT", f"not a regular file: {path}")
        try:
            existing = path.read_bytes()
        except OSError as exc:
            raise FinalSubtitleAudioReuseError("AUDIO_REUSE_DESTINATION_READ_FAILED", str(exc)) from exc
        if existing != payload:
            raise FinalSubtitleAudioReuseError("AUDIO_REUSE_DESTINATION_DRIFT", f"different bytes already exist: {path}")
        return
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        raise FinalSubtitleAudioReuseError("AUDIO_REUSE_DESTINATION_WRITE_FAILED", f"{path}: {exc}") from exc
    finally:
        temporary.unlink(missing_ok=True)


def _copy_source_receipt(source: Path, destination: Path) -> bytes:
    try:
        payload = source.read_bytes()
    except OSError as exc:
        raise FinalSubtitleAudioReuseError("AUDIO_REUSE_SOURCE_RECEIPT_READ_FAILED", f"{source}: {exc}") from exc
    _write_once(destination, payload)
    return payload


def _validate_parent_source(
    parent_record: Mapping[str, object],
    *,
    parent_final_srt: Path,
    parent_evidence_dir: Path,
    candidate_id: str,
) -> tuple[dict[str, Any], dict[str, Path], str, dict[str, Any], dict[str, Any]]:
    """Validate the parent natively and return its source receipt metadata."""

    parent_evidence = parent_record.get("subtitle_audio_correspondence")
    if not isinstance(parent_evidence, Mapping):
        raise FinalSubtitleAudioReuseError("AUDIO_REUSE_PARENT_EVIDENCE_MISSING")
    paths = _artifact_paths(parent_evidence_dir, candidate_id)
    for key, path in paths.items():
        paths[key] = _regular_file(path, label=f"parent_{key}")

    try:
        parent_media = gate._validated_burned_artifact(dict(parent_record))
    except Exception as exc:
        raise FinalSubtitleAudioReuseError("AUDIO_REUSE_PARENT_BURN_INVALID", str(exc)) from exc
    parent_media = _regular_file(parent_media, label="parent_burned_media")
    parent_final_srt = _regular_file(parent_final_srt, label="parent_final_srt")
    try:
        parent_receipt = gate.validate_final_subtitle_audio_check(
            parent_record,
            final_srt=parent_final_srt,
            actual_media=parent_media,
            package_root=parent_evidence_dir,
            candidate_id=candidate_id,
        )
    except (ValueError, OSError) as exc:
        raise FinalSubtitleAudioReuseError("AUDIO_REUSE_PARENT_NATIVE_VALIDATION_FAILED", str(exc)) from exc
    if parent_receipt.get("status") != "PASS" or parent_receipt.get("timing_status") != "PASS":
        raise FinalSubtitleAudioReuseError(
            "AUDIO_REUSE_PARENT_NATIVE_VALIDATION_BLOCKED",
            f"status={parent_receipt.get('status')!r} timing_status={parent_receipt.get('timing_status')!r}",
        )

    parent_audio = _record_audio_sha(parent_record, label="parent")
    envelope = _read_json(paths["correspondence"], label="parent correspondence")
    provenance = _read_json(paths["provenance"], label="parent provenance")
    envelope_audio = _canonical_sha(envelope.get("audio_sha256"), label="parent_envelope_audio")
    provenance_audio = _canonical_sha(provenance.get("audio_sha256"), label="parent_provenance_audio")
    if envelope_audio != parent_audio or provenance_audio != parent_audio:
        raise FinalSubtitleAudioReuseError(
            "AUDIO_REUSE_PARENT_AUDIO_HASH_DRIFT",
            f"record={parent_audio} envelope={envelope_audio} provenance={provenance_audio}",
        )
    if envelope.get("provider") != PROVIDER_NAME or provenance.get("provider") != PROVIDER_NAME:
        raise FinalSubtitleAudioReuseError("AUDIO_REUSE_PARENT_PROVIDER_INVALID")

    source_hashes = {
        "actual_media_sha256": _sha256(parent_media),
        "final_srt_sha256": _sha256(parent_final_srt),
        "audio_sha256": parent_audio,
        "witness_srt_sha256": _sha256(paths["witness_srt"]),
        "raw_result_sha256": _sha256(paths["raw_result"]),
        "provenance_sha256": _sha256(paths["provenance"]),
        "correspondence_sha256": _sha256(paths["correspondence"]),
    }
    # Native validation already binds these fields; retain explicit checks here
    # so the lineage records exactly what was accepted as the source.
    if _canonical_sha(envelope.get("actual_media_sha256"), label="parent_envelope_media") != source_hashes["actual_media_sha256"]:
        raise FinalSubtitleAudioReuseError("AUDIO_REUSE_PARENT_MEDIA_HASH_DRIFT")
    if _canonical_sha(envelope.get("final_srt_sha256"), label="parent_envelope_srt") != source_hashes["final_srt_sha256"]:
        raise FinalSubtitleAudioReuseError("AUDIO_REUSE_PARENT_SRT_HASH_DRIFT")
    return parent_receipt, paths, parent_audio, source_hashes, {"envelope": envelope, "provenance": provenance}


def _write_json_bytes(document: Mapping[str, object]) -> bytes:
    return (json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _prove_audio_reuse_identity(
    *,
    parent_media: Path,
    current_media: Path,
    parent_audio: str,
    candidate_id: str,
    temp_parent: Path,
    extractor: Callable[[Path, Path], None],
    custom_extractor: bool,
    decoded_audio_identity: Callable[[Path], Mapping[str, object]] | None,
) -> tuple[str, str, dict[str, object]]:
    """Prove byte identity or the existing extractor/PCM equivalence contract."""
    parent_replay_audio: str | None = None
    parent_pcm_identity: dict[str, str] | None = None
    current_pcm_identity: dict[str, str] | None = None
    try:
        with tempfile.TemporaryDirectory(prefix=f".{candidate_id}.audio-reuse-extract-", dir=temp_parent) as temp_name:
            temp_root = Path(temp_name)
            extracted = temp_root / "successor.mp3"
            extractor(current_media, extracted)
            extracted = _regular_file(extracted, label="successor_extracted_audio")
            current_audio = _sha256(extracted)
            if current_audio != parent_audio:
                # A custom extractor without a matching PCM seam is a unit-test
                # mismatch, never implicit permission to take the production
                # fallback.  Production omits both private seams.
                if custom_extractor and decoded_audio_identity is None:
                    raise FinalSubtitleAudioReuseError(
                        "AUDIO_REUSE_AUDIO_HASH_MISMATCH",
                        f"parent_recorded={parent_audio} successor_extracted={current_audio}",
                    )
                replayed_parent = temp_root / "parent-replay.mp3"
                extractor(parent_media, replayed_parent)
                replayed_parent = _regular_file(
                    replayed_parent, label="parent_replayed_extracted_audio"
                )
                parent_replay_audio = _sha256(replayed_parent)
                if parent_replay_audio != current_audio:
                    raise FinalSubtitleAudioReuseError(
                        "AUDIO_REUSE_CURRENT_EXTRACTOR_REPLAY_MISMATCH",
                        f"parent_replayed={parent_replay_audio} successor_extracted={current_audio}",
                    )
                identity_reader = decoded_audio_identity or _decoded_pcm_identity
                parent_pcm_identity = _validated_pcm_identity(
                    identity_reader(parent_media), label="parent"
                )
                current_pcm_identity = _validated_pcm_identity(
                    identity_reader(current_media), label="current"
                )
                if parent_pcm_identity != current_pcm_identity:
                    raise FinalSubtitleAudioReuseError(
                        "AUDIO_REUSE_DECODED_PCM_MISMATCH",
                        f"parent={parent_pcm_identity} current={current_pcm_identity}",
                    )
    except FinalSubtitleAudioReuseError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise FinalSubtitleAudioReuseError("AUDIO_REUSE_EXTRACTION_FAILED", str(exc)) from exc

    exact_recorded_audio_match = current_audio == parent_audio
    reuse_mode = (
        "EXACT_EXTRACTED_AUDIO_HASH_REUSE"
        if exact_recorded_audio_match
        else "CURRENT_EXTRACTOR_REPLAY_AND_DECODED_PCM_EQUIVALENCE_REUSE"
    )
    proof: dict[str, object] = {
        "extractor": "src.autoslice.final_subtitle_audio_gate._default_extract_audio",
        "successor_audio_bytes_hashed": True,
        "recorded_parent_audio_hash_match": exact_recorded_audio_match,
    }
    if not exact_recorded_audio_match:
        assert parent_replay_audio is not None
        assert parent_pcm_identity is not None
        assert current_pcm_identity is not None
        proof["current_extractor_replay"] = {
            "parent_audio_sha256": parent_replay_audio,
            "current_audio_sha256": current_audio,
            "byte_identical": parent_replay_audio == current_audio,
        }
        proof["decoded_pcm_equivalence"] = {
            "schema_version": "final-subtitle-audio-decoded-pcm-equivalence.v1",
            "status": "PASS",
            "parent": parent_pcm_identity,
            "current": current_pcm_identity,
            "native_pcm_byte_identical": (
                parent_pcm_identity["native_pcm_s16le_sha256"]
                == current_pcm_identity["native_pcm_s16le_sha256"]
            ),
            "bcut_input_pcm_byte_identical": (
                parent_pcm_identity["bcut_input_mono_16000_pcm_s16le_sha256"]
                == current_pcm_identity["bcut_input_mono_16000_pcm_s16le_sha256"]
            ),
        }

    return current_audio, reuse_mode, proof


def reuse_final_subtitle_audio_check(
    parent_record: Mapping[str, object],
    current_record: Mapping[str, object],
    *,
    parent_final_srt: str | Path,
    current_final_srt: str | Path,
    parent_evidence_dir: str | Path,
    output_dir: str | Path,
    parent_record_path: str | Path | None = None,
    current_record_path: str | Path | None = None,
    _extract_audio: Callable[[Path, Path], None] | None = None,
    _decoded_audio_identity: Callable[[Path], Mapping[str, object]] | None = None,
) -> FinalSubtitleAudioReuseResult:
    """Build and validate a hash-bound same-audio successor evidence record.

    ``parent_record`` and ``current_record`` are read-only mappings.  The
    current mapping is deep-copied before its audio evidence is replaced.  The
    helper writes only the four canonical gate artifacts and
    ``AUDIO-VERIFIED-RECORD.json`` below ``output_dir``.  A supplied record
    path is checked byte-for-byte against its mapping and is used in lineage;
    callers should pass the actual parent/current record paths for durable
    provenance.

    The private ``_extract_audio`` and ``_decoded_audio_identity`` arguments
    exist solely for unit tests.  In production they must be omitted so the
    default extractor and full FFmpeg PCM decoders perform the identity proof.
    No argument can inject a transcriber.
    """

    if not isinstance(parent_record, Mapping) or not isinstance(current_record, Mapping):
        raise FinalSubtitleAudioReuseError("AUDIO_REUSE_RECORD_INVALID", "records must be mappings")
    candidate_id = _safe_candidate_id(parent_record, current_record)
    parent_srt = _regular_file(parent_final_srt, label="parent_final_srt")
    current_srt = _regular_file(current_final_srt, label="current_final_srt")
    parent_dir = _regular_directory(parent_evidence_dir, label="parent_evidence_dir")
    output = _regular_directory(output_dir, label="output_dir", must_exist=False)
    current_copy = copy.deepcopy(dict(current_record))

    parent_record_sha = _load_record_hash(parent_record, parent_record_path, label="parent_record")
    current_record_sha = _load_record_hash(current_record, current_record_path, label="current_record")
    parent_receipt, parent_paths, parent_audio, parent_source_hashes, _parent_source_docs = _validate_parent_source(
        parent_record,
        parent_final_srt=parent_srt,
        parent_evidence_dir=parent_dir,
        candidate_id=candidate_id,
    )
    try:
        parent_media = gate._validated_burned_artifact(dict(parent_record))
    except Exception as exc:
        raise FinalSubtitleAudioReuseError("AUDIO_REUSE_PARENT_BURN_INVALID", str(exc)) from exc
    parent_media = _regular_file(parent_media, label="parent_burned_media")

    try:
        current_media = gate._validated_burned_artifact(current_copy)
    except Exception as exc:
        raise FinalSubtitleAudioReuseError("AUDIO_REUSE_CURRENT_BURN_INVALID", str(exc)) from exc
    current_media = _regular_file(current_media, label="current_burned_media")
    try:
        parent_intro_ms = gate._intro_offset_ms(parent_record)
        current_intro_ms = gate._intro_offset_ms(current_copy)
    except gate.FinalSubtitleAudioGateError as exc:
        raise FinalSubtitleAudioReuseError("AUDIO_REUSE_INTRO_OFFSET_INVALID", str(exc)) from exc
    if parent_intro_ms != current_intro_ms:
        raise FinalSubtitleAudioReuseError(
            "AUDIO_REUSE_INTRO_OFFSET_MISMATCH",
            f"parent={parent_intro_ms} current={current_intro_ms}",
        )

    current_media_sha = _sha256(current_media)
    current_srt_sha = _sha256(current_srt)
    parent_evidence = parent_record.get("subtitle_audio_correspondence")
    assert isinstance(parent_evidence, Mapping)
    parent_actual_media_sha = _canonical_sha(parent_evidence.get("actual_media_sha256"), label="parent_record_media")
    if parent_actual_media_sha != parent_source_hashes["actual_media_sha256"]:
        raise FinalSubtitleAudioReuseError("AUDIO_REUSE_PARENT_MEDIA_HASH_DRIFT")

    # The default extractor is the existing production boundary.  No
    # transcriber is called: the parent's raw/witness files are reused below.
    extractor = _extract_audio or gate._default_extract_audio
    temp_parent = output.parent
    if not temp_parent.is_dir() or temp_parent.is_symlink():
        raise FinalSubtitleAudioReuseError("AUDIO_REUSE_DIRECTORY_UNAVAILABLE", str(temp_parent))
    current_audio, reuse_mode, proof = _prove_audio_reuse_identity(
        parent_media=parent_media,
        current_media=current_media,
        parent_audio=parent_audio,
        candidate_id=candidate_id,
        temp_parent=temp_parent,
        extractor=extractor,
        custom_extractor=_extract_audio is not None,
        decoded_audio_identity=_decoded_audio_identity,
    )

    audio_reuse = {
        "schema_version": REUSE_SCHEMA_VERSION,
        "mode": reuse_mode,
        "source_provider": PROVIDER_NAME,
        "provider_route": PROVIDER_ROUTE,
        "new_provider_calls": 0,
        "new_asr_calls": 0,
        "subtitle_text_editing": "none",
        "parent": {
            "record_sha256": parent_record_sha,
            "actual_media_sha256": parent_source_hashes["actual_media_sha256"],
            "final_srt_sha256": parent_source_hashes["final_srt_sha256"],
            "audio_sha256": parent_audio,
            "witness_srt_sha256": parent_source_hashes["witness_srt_sha256"],
            "raw_result_sha256": parent_source_hashes["raw_result_sha256"],
            "provenance_sha256": parent_source_hashes["provenance_sha256"],
            "correspondence_sha256": parent_source_hashes["correspondence_sha256"],
        },
        "current": {
            "record_sha256": current_record_sha,
            "actual_media_sha256": current_media_sha,
            "final_srt_sha256": current_srt_sha,
            "audio_sha256": current_audio,
            "intro_offset_ms": current_intro_ms,
        },
        "proof": proof,
    }

    artifact_paths = _artifact_paths(output, candidate_id)
    record_path = output / RECORD_FILENAME
    # Stage every successor artifact outside the destination directory.  This
    # keeps a failed correspondence or native replay from leaving a partial
    # report, while still allowing the destination to be an existing exact run.
    try:
        with tempfile.TemporaryDirectory(prefix=f".{candidate_id}.audio-reuse-", dir=temp_parent) as stage_name:
            stage = Path(stage_name)
            stage_paths = _artifact_paths(stage, candidate_id)
            witness_bytes = _copy_source_receipt(parent_paths["witness_srt"], stage_paths["witness_srt"])
            raw_bytes = _copy_source_receipt(parent_paths["raw_result"], stage_paths["raw_result"])
            witness_sha = _hash_bytes(witness_bytes)
            raw_sha = _hash_bytes(raw_bytes)

            provenance = {
                "schema_version": gate.PROVENANCE_SCHEMA_VERSION,
                "candidate_id": candidate_id,
                "actual_media_sha256": current_media_sha,
                "witness_srt_sha256": witness_sha,
                "audio_sha256": current_audio,
                "audio_format": "mp3",
                "provider": PROVIDER_NAME,
                "provider_route": PROVIDER_ROUTE,
                "audio_reuse": audio_reuse,
                "timebase": {
                    "unit": "ms",
                    "final_srt": "delivery_local_ms",
                    "witness_srt": "actual_media_local_ms",
                    "intro_offset_application": "add_once_to_final_srt",
                    "declared_intro_offset_ms": current_intro_ms,
                },
            }
            _write_once(stage_paths["provenance"], _write_json_bytes(provenance))

            try:
                current_receipt_stage = gate.check_subtitle_audio_correspondence(
                    current_srt,
                    current_media,
                    stage_paths["witness_srt"],
                    stage_paths["provenance"],
                    intro_offset_ms=current_intro_ms,
                )
            except gate.CorrespondenceInputError as exc:
                raise FinalSubtitleAudioReuseError(
                    "AUDIO_REUSE_CURRENT_CORRESPONDENCE_INPUT_INVALID", str(exc)
                ) from exc
            if current_receipt_stage.get("status") != "PASS" or current_receipt_stage.get("timing_status") != "PASS":
                raise FinalSubtitleAudioReuseError(
                    "AUDIO_REUSE_CURRENT_CORRESPONDENCE_BLOCKED",
                    ",".join(str(code) for code in current_receipt_stage.get("reason_codes") or ["unknown"]),
                )
            current_receipt = _receipt_path_rewrite(
                current_receipt_stage,
                source_root=stage,
                destination_root=output,
            )
            assert isinstance(current_receipt, dict)
            envelope = {
                "schema_version": gate.GATE_SCHEMA_VERSION,
                "candidate_id": candidate_id,
                "provider": PROVIDER_NAME,
                "provider_route": PROVIDER_ROUTE,
                "actual_media_sha256": current_media_sha,
                "final_srt_sha256": current_srt_sha,
                "witness_srt_sha256": witness_sha,
                "audio_sha256": current_audio,
                "raw_result_sha256": raw_sha,
                "intro_offset_ms": current_intro_ms,
                "audio_reuse": audio_reuse,
                "receipt": current_receipt,
            }
            _write_once(stage_paths["correspondence"], _write_json_bytes(envelope))

            evidence = {
                "schema_version": gate.GATE_SCHEMA_VERSION,
                "candidate_id": candidate_id,
                "output_dir": str(output),
                "provider": PROVIDER_NAME,
                "provider_route": PROVIDER_ROUTE,
                "status": current_receipt.get("status"),
                "timing_status": current_receipt.get("timing_status"),
                "text_correctness_status": current_receipt.get("text_correctness_status"),
                "reason_codes": list(current_receipt.get("reason_codes") or []),
                "actual_media_sha256": current_media_sha,
                "final_srt_sha256": current_srt_sha,
                "witness_srt_sha256": witness_sha,
                "audio_sha256": current_audio,
                "raw_result_sha256": raw_sha,
                "intro_offset_ms": current_intro_ms,
                "cache_reused": True,
                "audio_reuse": audio_reuse,
                "witness_srt_path": str(artifact_paths["witness_srt"]),
                "provenance_path": str(artifact_paths["provenance"]),
                "correspondence_path": str(artifact_paths["correspondence"]),
                "raw_result_path": str(artifact_paths["raw_result"]),
                "receipt": current_receipt,
                "receipt_sha256": _hash_bytes(_write_json_bytes(current_receipt)),
            }
            current_copy["subtitle_audio_correspondence"] = evidence
            hashes = current_copy.setdefault("artifact_hashes", {})
            if isinstance(hashes, dict):
                hashes.update(
                    {
                        "subtitle_audio_witness_srt_sha256": _sha256(stage_paths["witness_srt"]),
                        "subtitle_audio_provenance_sha256": _sha256(stage_paths["provenance"]),
                        "subtitle_audio_correspondence_sha256": _sha256(stage_paths["correspondence"]),
                        "subtitle_audio_bcut_raw_sha256": _sha256(stage_paths["raw_result"]),
                        "subtitle_audio_extracted_audio_sha256": current_audio,
                    }
                )

            # Replay the existing native gate while all staged evidence is
            # available.  The package root controls canonical file selection;
            # evidence locators remain final and relocatable by design.
            try:
                gate.validate_final_subtitle_audio_check(
                    current_copy,
                    final_srt=current_srt,
                    actual_media=current_media,
                    package_root=stage,
                    candidate_id=candidate_id,
                )
            except (ValueError, OSError) as exc:
                raise FinalSubtitleAudioReuseError("AUDIO_REUSE_STAGED_NATIVE_VALIDATION_FAILED", str(exc)) from exc

            staged_record_bytes = _write_json_bytes(current_copy)
            staged_record = stage / RECORD_FILENAME
            _write_once(staged_record, staged_record_bytes)

            output.mkdir(parents=True, exist_ok=True)
            if output.is_symlink() or not output.is_dir():
                raise FinalSubtitleAudioReuseError("AUDIO_REUSE_DIRECTORY_INVALID", str(output))
            for key, stage_path in stage_paths.items():
                destination = artifact_paths[key]
                _write_once(destination, stage_path.read_bytes())
            _write_once(record_path, staged_record_bytes)
    except FinalSubtitleAudioReuseError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise FinalSubtitleAudioReuseError("AUDIO_REUSE_WRITE_FAILED", str(exc)) from exc

    # Validate the actual destination after commit, using exactly the record
    # returned to the caller.  This is the acceptance boundary for the helper.
    try:
        current_receipt_final = gate.validate_final_subtitle_audio_check(
            current_copy,
            final_srt=current_srt,
            actual_media=current_media,
            package_root=output,
            candidate_id=candidate_id,
        )
    except (ValueError, OSError) as exc:
        raise FinalSubtitleAudioReuseError("AUDIO_REUSE_CURRENT_NATIVE_VALIDATION_FAILED", str(exc)) from exc
    if current_receipt_final.get("status") != "PASS" or current_receipt_final.get("timing_status") != "PASS":
        raise FinalSubtitleAudioReuseError("AUDIO_REUSE_CURRENT_NATIVE_VALIDATION_BLOCKED")

    try:
        record_sha = _sha256(record_path)
    except FinalSubtitleAudioReuseError:
        raise
    return FinalSubtitleAudioReuseResult(
        record=current_copy,
        parent_receipt=parent_receipt,
        current_receipt=current_receipt_final,
        audio_sha256=current_audio,
        parent_audio_sha256=parent_audio,
        artifact_paths=artifact_paths,
        record_path=record_path,
        record_sha256=record_sha,
    )


# A descriptive alias for callers that prefer the operation name over the
# gate-shaped name.  Both names point to the same narrow implementation.
reuse_existing_bcut_audio = reuse_final_subtitle_audio_check


__all__ = [
    "FinalSubtitleAudioReuseError",
    "FinalSubtitleAudioReuseResult",
    "reuse_existing_bcut_audio",
    "reuse_final_subtitle_audio_check",
]
