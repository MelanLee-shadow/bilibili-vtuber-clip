"""Capture and validate the timing of a burned talk subtitle track.

The gate is deliberately timing-only.  It asks the free BCUT client for a
fresh witness from the exact burned media, then delegates correspondence
classification to :mod:`subtitle_audio_correspondence`.  No provider output
is used to edit subtitle text.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from scripts.free_asr_client import to_srt as _bcut_to_srt
from scripts.free_asr_client import transcribe_bcut as _transcribe_bcut

from src.autoslice.producer_media import _validated_burned_artifact
from src.autoslice.subtitle_audio_correspondence import (
    CorrespondenceInputError,
    check_subtitle_audio_correspondence,
    sha256_file,
)


GATE_SCHEMA_VERSION = "final-subtitle-audio-gate.v1"
PROVENANCE_SCHEMA_VERSION = "subtitle-audio-correspondence-provenance.v1"
PROVIDER_NAME = "bcut"
_CANDIDATE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_PATH_KEYS = {
    "path",
    "audio_path",
    "final_srt_path",
    "actual_media_path",
    "witness_srt_path",
    "provenance_path",
    "raw_result_path",
    "correspondence_path",
}


class FinalSubtitleAudioGateError(ValueError):
    """A burned talk package cannot pass the final subtitle-audio gate."""


@dataclass(frozen=True)
class FinalSubtitleAudioGateAdapters:
    """Injectable seams for tests; production defaults are ffmpeg and BCUT."""

    extract_audio: Callable[[Path, Path], None]
    transcribe_bcut: Callable[[bytes], dict[str, Any]]
    to_srt: Callable[[dict[str, Any]], str]


def _default_extract_audio(media: Path, output: Path) -> None:
    # Caller input is data, never FFmpeg keyboard commands (q can exit with rc=0).
    completed = subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(media),
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
        stdin=subprocess.DEVNULL,
        check=False,
        capture_output=True,
        text=True,
        timeout=900,
    )
    if completed.returncode != 0 or not output.is_file():
        raise FinalSubtitleAudioGateError(
            "FINAL_SUBTITLE_AUDIO_FFMPEG_FAILED: "
            f"rc={completed.returncode}: {completed.stderr[-800:]}"
        )

    _validate_extracted_audio_media(output)


def _validate_extracted_audio_media(output: Path) -> None:
    """Require decodable mono MP3, not speech or a provider's success claim.

    This is the extraction boundary only. Positive frames do not prove complete
    source coverage or correct subtitle text; those remain independent checks.
    Silence is valid. The short AV witness validator cannot be used
    here because it deliberately requires a separate black-video stream.
    """
    try:
        audio = _regular_file(output, label="FINAL_SUBTITLE_AUDIO_EXTRACTED")
        if audio.stat().st_size == 0:
            raise ValueError("empty extraction")
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-count_frames", "-show_entries",
             "stream=codec_type,codec_name,duration,sample_rate,channels,nb_read_frames",
             "-of", "json", str(audio)],
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=900, check=False,
        )
        if probe.returncode != 0 or probe.stderr.strip():
            raise ValueError("decode probe failed")
        payload = json.loads(probe.stdout)
        streams = payload.get("streams") if isinstance(payload, dict) else None
        if not isinstance(streams, list) or len(streams) != 1:
            raise ValueError("extraction stream count mismatch")
        stream = streams[0]
        if not isinstance(stream, dict):
            raise ValueError("malformed extraction stream")
        duration = float(stream["duration"])
        if (stream.get("codec_type") != "audio" or stream.get("codec_name") != "mp3"
                or int(stream["sample_rate"]) != 16_000 or int(stream["channels"]) != 1
                or int(stream["nb_read_frames"]) <= 0
                or not math.isfinite(duration) or duration <= 0):
            raise ValueError("extraction has no valid decoded audio")
    except (OSError, ValueError, TypeError, KeyError, OverflowError, subprocess.TimeoutExpired) as exc:
        # Keep the existing extraction failure class; do not retain a tool's
        # arbitrary output or turn unusable bytes into a provider retry.
        raise FinalSubtitleAudioGateError(
            "FINAL_SUBTITLE_AUDIO_FFMPEG_FAILED: extracted audio decode validation failed"
        ) from exc


_DEFAULT_ADAPTERS = FinalSubtitleAudioGateAdapters(
    extract_audio=_default_extract_audio,
    transcribe_bcut=_transcribe_bcut,
    to_srt=_bcut_to_srt,
)


def _sha256(path: Path) -> str:
    return sha256_file(path)


def _regular_file(value: object, *, label: str) -> Path:
    path = Path(str(value)) if isinstance(value, (str, Path)) else Path("")
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise FinalSubtitleAudioGateError(f"{label}_UNAVAILABLE: {path}: {exc}") from exc
    if path.is_symlink() or not resolved.is_file():
        raise FinalSubtitleAudioGateError(f"{label}_NOT_REGULAR_FILE: {path}")
    return resolved


def _safe_candidate_id(candidate_id: str) -> str:
    if not isinstance(candidate_id, str) or not _CANDIDATE_ID_RE.fullmatch(candidate_id):
        raise FinalSubtitleAudioGateError(
            "FINAL_SUBTITLE_AUDIO_CANDIDATE_ID_INVALID: expected a simple file stem"
        )
    return candidate_id


def _artifact_paths(output_dir: Path, candidate_id: str) -> dict[str, Path]:
    stem = _safe_candidate_id(candidate_id)
    return _artifact_paths_for_stem(output_dir, stem)


def _artifact_paths_for_stem(output_dir: Path, stem: str) -> dict[str, Path]:
    """Build canonical evidence paths for a safe filename stem.

    Capture uses the restricted candidate id.  Delivery can rename the SRT
    basename (including to a Unicode title), so validation uses the supplied
    final SRT stem after checking that it cannot escape the package root.
    """

    if not isinstance(stem, str) or not stem or stem in {".", ".."}:
        raise FinalSubtitleAudioGateError("FINAL_SUBTITLE_AUDIO_PACKAGE_STEM_INVALID")
    if Path(stem).name != stem or any(part in {"", ".", ".."} for part in Path(stem).parts):
        raise FinalSubtitleAudioGateError("FINAL_SUBTITLE_AUDIO_PACKAGE_STEM_INVALID")
    return {
        "witness_srt": output_dir / f"{stem}.subtitle-audio-witness.srt",
        "provenance": output_dir / f"{stem}.subtitle-audio-provenance.json",
        "correspondence": output_dir / f"{stem}.subtitle-audio-correspondence.json",
        "raw_result": output_dir / f"{stem}.subtitle-audio-bcut.raw.json",
    }


def subtitle_audio_artifact_paths(
    record: Mapping[str, object], *, candidate_id: str | None = None
) -> tuple[Path, ...]:
    """Return the four canonical evidence files bound by a captured record."""

    evidence = record.get("subtitle_audio_correspondence")
    if not isinstance(evidence, Mapping):
        return ()
    stem = candidate_id or str(evidence.get("candidate_id") or "")
    if not stem:
        return ()
    output = evidence.get("output_dir")
    if not isinstance(output, str) or not output:
        paths = {
            key: evidence.get(f"{key}_path")
            for key in ("witness_srt", "provenance", "correspondence", "raw_result")
        }
        return tuple(Path(str(value)) for value in paths.values() if value)
    return tuple(_artifact_paths(Path(output), stem).values())


def _intro_offset_ms(record: Mapping[str, object]) -> int:
    preview = record.get("burned_preview")
    if not isinstance(preview, Mapping):
        raise FinalSubtitleAudioGateError("FINAL_SUBTITLE_AUDIO_BURN_BINDING_MISSING")
    branding = preview.get("branding_intro")
    if branding is None:
        return 0
    if not isinstance(branding, Mapping):
        raise FinalSubtitleAudioGateError("FINAL_SUBTITLE_AUDIO_BRANDING_INTRO_INVALID")
    value = branding.get("intro_offset_ms")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise FinalSubtitleAudioGateError(
            "FINAL_SUBTITLE_AUDIO_INTRO_OFFSET_INVALID: expected non-negative integer ms"
        )
    return value


def _write_bytes_atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_json_atomic(path: Path, document: Mapping[str, object]) -> None:
    _write_bytes_atomic(
        path,
        (json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
            "utf-8"
        ),
    )


def _read_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise FinalSubtitleAudioGateError(f"{label}_JSON_INVALID: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise FinalSubtitleAudioGateError(f"{label}_JSON_NOT_OBJECT: {path}")
    return value


def _canonical_sha(value: object, *, label: str) -> str:
    if not isinstance(value, str):
        raise FinalSubtitleAudioGateError(f"{label}_SHA256_MISSING")
    value = value.strip().lower()
    if value.startswith("sha256:"):
        value = value[7:]
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise FinalSubtitleAudioGateError(f"{label}_SHA256_INVALID")
    return "sha256:" + value


def _semantic(value: object) -> object:
    """Drop only relocatable path values before receipt comparison."""

    if isinstance(value, Mapping):
        return {
            str(key): _semantic(item)
            for key, item in value.items()
            if str(key) not in _PATH_KEYS and not str(key).endswith("_path")
        }
    if isinstance(value, list):
        return [_semantic(item) for item in value]
    return value


def _expected_receipt(evidence: Mapping[str, object]) -> Mapping[str, object]:
    receipt = evidence.get("receipt")
    if not isinstance(receipt, Mapping):
        raise FinalSubtitleAudioGateError("FINAL_SUBTITLE_AUDIO_RECEIPT_MISSING")
    return receipt


def _resolve_evidence_paths(
    record: Mapping[str, object],
    *,
    candidate_id: str | None,
    package_dir: Path | None,
    package_stem: str | None = None,
) -> tuple[str, dict[str, Path]]:
    evidence = record.get("subtitle_audio_correspondence")
    if not isinstance(evidence, Mapping):
        raise FinalSubtitleAudioGateError("FINAL_SUBTITLE_AUDIO_RECORD_BINDING_MISSING")
    stem = _safe_candidate_id(
        candidate_id or str(evidence.get("candidate_id") or record.get("candidate_id") or "")
    )
    if package_dir is not None:
        package_paths = _artifact_paths_for_stem(package_dir, package_stem or stem)
        if not all(path.is_file() and not path.is_symlink() for path in package_paths.values()):
            raise FinalSubtitleAudioGateError(
                "FINAL_SUBTITLE_AUDIO_PACKAGE_EVIDENCE_MISSING: "
                f"expected canonical evidence for {stem!r} inside {package_dir}"
            )
        return stem, package_paths
    paths = {
        key: evidence.get(f"{key}_path")
        for key in ("witness_srt", "provenance", "correspondence", "raw_result")
    }
    if not all(isinstance(value, str) and value for value in paths.values()):
        raise FinalSubtitleAudioGateError(
            "FINAL_SUBTITLE_AUDIO_EVIDENCE_PATHS_MISSING: package copy or original paths required"
        )
    return stem, {key: _regular_file(value, label=f"subtitle_audio_{key}") for key, value in paths.items()}


def _validate_artifacts(
    record: Mapping[str, object],
    *,
    actual_media: Path,
    final_srt: Path,
    package_dir: Path | None = None,
    candidate_id: str | None = None,
    package_stem: str | None = None,
    require_pass: bool,
) -> dict[str, Any]:
    expected_intro = _intro_offset_ms(record)
    stem, paths = _resolve_evidence_paths(
        record,
        candidate_id=candidate_id,
        package_dir=package_dir,
        package_stem=(package_stem or final_srt.stem) if package_dir is not None else None,
    )
    media_sha256 = _sha256(actual_media)
    final_sha256 = _sha256(final_srt)
    evidence = record["subtitle_audio_correspondence"]
    assert isinstance(evidence, Mapping)
    envelope = _read_json(paths["correspondence"], label="subtitle_audio_correspondence")
    if envelope.get("schema_version") != GATE_SCHEMA_VERSION:
        raise FinalSubtitleAudioGateError("FINAL_SUBTITLE_AUDIO_CORRESPONDENCE_SCHEMA_UNSUPPORTED")
    if envelope.get("candidate_id") != stem:
        raise FinalSubtitleAudioGateError("FINAL_SUBTITLE_AUDIO_CANDIDATE_ID_DRIFT")
    if envelope.get("provider") != PROVIDER_NAME:
        raise FinalSubtitleAudioGateError("FINAL_SUBTITLE_AUDIO_PROVIDER_INVALID")
    if _canonical_sha(envelope.get("actual_media_sha256"), label="actual_media") != media_sha256:
        raise FinalSubtitleAudioGateError(
            "FINAL_SUBTITLE_AUDIO_MEDIA_HASH_DRIFT: "
            f"bound={envelope.get('actual_media_sha256')} observed={media_sha256}"
        )
    if _canonical_sha(envelope.get("final_srt_sha256"), label="final_srt") != final_sha256:
        raise FinalSubtitleAudioGateError("FINAL_SUBTITLE_AUDIO_FINAL_SRT_HASH_DRIFT")
    if _canonical_sha(envelope.get("witness_srt_sha256"), label="witness_srt") != _sha256(paths["witness_srt"]):
        raise FinalSubtitleAudioGateError("FINAL_SUBTITLE_AUDIO_WITNESS_HASH_DRIFT")
    if envelope.get("intro_offset_ms") != expected_intro:
        raise FinalSubtitleAudioGateError("FINAL_SUBTITLE_AUDIO_ENVELOPE_INTRO_OFFSET_DRIFT")

    for key, observed, label in (
        ("actual_media_sha256", media_sha256, "record_media"),
        ("final_srt_sha256", final_sha256, "record_final_srt"),
    ):
        declared = evidence.get(key)
        if declared is not None and _canonical_sha(declared, label=label) != observed:
            raise FinalSubtitleAudioGateError(
                f"FINAL_SUBTITLE_AUDIO_RECORD_{key.upper()}_DRIFT"
            )

    provenance = _read_json(paths["provenance"], label="subtitle_audio_provenance")
    if provenance.get("schema_version") != PROVENANCE_SCHEMA_VERSION:
        raise FinalSubtitleAudioGateError("FINAL_SUBTITLE_AUDIO_PROVENANCE_SCHEMA_UNSUPPORTED")
    if _canonical_sha(provenance.get("actual_media_sha256"), label="provenance_media") != media_sha256:
        raise FinalSubtitleAudioGateError("FINAL_SUBTITLE_AUDIO_PROVENANCE_MEDIA_HASH_DRIFT")
    if provenance.get("provider") != PROVIDER_NAME:
        raise FinalSubtitleAudioGateError("FINAL_SUBTITLE_AUDIO_PROVENANCE_PROVIDER_INVALID")
    envelope_audio_sha256 = _canonical_sha(envelope.get("audio_sha256"), label="audio")
    provenance_audio_sha256 = _canonical_sha(provenance.get("audio_sha256"), label="provenance_audio")
    if envelope_audio_sha256 != provenance_audio_sha256:
        raise FinalSubtitleAudioGateError("FINAL_SUBTITLE_AUDIO_AUDIO_HASH_BINDING_DRIFT")
    timebase = provenance.get("timebase")
    expected_timebase = {
        "unit": "ms",
        "final_srt": "delivery_local_ms",
        "witness_srt": "actual_media_local_ms",
        "intro_offset_application": "add_once_to_final_srt",
    }
    if not isinstance(timebase, Mapping):
        raise FinalSubtitleAudioGateError(
            "FINAL_SUBTITLE_AUDIO_INTRO_OFFSET_DRIFT: "
            f"expected={expected_intro} provenance={timebase!r}"
        )
    for key, expected in expected_timebase.items():
        if timebase.get(key) != expected:
            raise FinalSubtitleAudioGateError(
                f"FINAL_SUBTITLE_AUDIO_PROVENANCE_TIMEBASE_INVALID: {key}"
            )
    if timebase.get("declared_intro_offset_ms") != expected_intro:
        raise FinalSubtitleAudioGateError(
            "FINAL_SUBTITLE_AUDIO_INTRO_OFFSET_DRIFT: "
            f"expected={expected_intro} provenance={timebase!r}"
        )

    witness_sha256 = _sha256(paths["witness_srt"])
    if _canonical_sha(provenance.get("witness_srt_sha256"), label="provenance_witness") != witness_sha256:
        raise FinalSubtitleAudioGateError("FINAL_SUBTITLE_AUDIO_PROVENANCE_WITNESS_HASH_DRIFT")
    raw_sha256 = _sha256(paths["raw_result"])
    if _canonical_sha(envelope.get("raw_result_sha256"), label="raw_result") != raw_sha256:
        raise FinalSubtitleAudioGateError("FINAL_SUBTITLE_AUDIO_RAW_RESULT_HASH_DRIFT")
    try:
        raw_result = _read_json(paths["raw_result"], label="subtitle_audio_bcut_raw")
        derived_witness: bytes | None = None
        if derived_witness is None:
            derived_witness = _bcut_to_srt(raw_result).encode("utf-8")
    except FinalSubtitleAudioGateError:
        raise
    except Exception as exc:
        raise FinalSubtitleAudioGateError(
            "FINAL_SUBTITLE_AUDIO_RAW_RESULT_TO_SRT_FAILED: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    try:
        witness_bytes = paths["witness_srt"].read_bytes()
    except OSError as exc:
        raise FinalSubtitleAudioGateError(
            f"FINAL_SUBTITLE_AUDIO_WITNESS_READ_FAILED: {exc}"
        ) from exc
    if derived_witness != witness_bytes:
        raise FinalSubtitleAudioGateError(
            "FINAL_SUBTITLE_AUDIO_WITNESS_DERIVATION_DRIFT: "
            "canonical BCUT raw result does not reproduce witness SRT"
        )

    evidence_witness = evidence.get("witness_srt_sha256")
    if evidence_witness is not None and _canonical_sha(
        evidence_witness, label="record_witness"
    ) != witness_sha256:
        raise FinalSubtitleAudioGateError("FINAL_SUBTITLE_AUDIO_RECORD_WITNESS_HASH_DRIFT")
    evidence_raw = evidence.get("raw_result_sha256")
    if evidence_raw is not None and _canonical_sha(
        evidence_raw, label="record_raw_result"
    ) != raw_sha256:
        raise FinalSubtitleAudioGateError("FINAL_SUBTITLE_AUDIO_RECORD_RAW_RESULT_HASH_DRIFT")
    evidence_audio = evidence.get("audio_sha256")
    if evidence_audio is not None and _canonical_sha(
        evidence_audio, label="record_audio"
    ) != envelope_audio_sha256:
        raise FinalSubtitleAudioGateError("FINAL_SUBTITLE_AUDIO_RECORD_AUDIO_HASH_DRIFT")

    try:
        receipt = check_subtitle_audio_correspondence(
            final_srt,
            actual_media,
            paths["witness_srt"],
            paths["provenance"],
            intro_offset_ms=expected_intro,
        )
    except CorrespondenceInputError as exc:
        raise FinalSubtitleAudioGateError(
            f"FINAL_SUBTITLE_AUDIO_CORRESPONDENCE_INPUT_INVALID: {exc}"
        ) from exc
    stored_receipt = _expected_receipt(envelope)
    if _semantic(stored_receipt) != _semantic(receipt):
        raise FinalSubtitleAudioGateError(
            "FINAL_SUBTITLE_AUDIO_RECEIPT_DRIFT: deterministic receipt changed"
        )
    for key in ("status", "timing_status", "text_correctness_status"):
        declared = evidence.get(key)
        if declared is not None and declared != receipt.get(key):
            raise FinalSubtitleAudioGateError(
                f"FINAL_SUBTITLE_AUDIO_RECORD_{key.upper()}_DRIFT"
            )
    if evidence.get("candidate_id") not in (None, stem):
        raise FinalSubtitleAudioGateError("FINAL_SUBTITLE_AUDIO_RECORD_CANDIDATE_ID_DRIFT")
    if evidence.get("actual_media_sha256") not in (None, media_sha256):
        raise FinalSubtitleAudioGateError("FINAL_SUBTITLE_AUDIO_RECORD_MEDIA_HASH_DRIFT")
    if evidence.get("intro_offset_ms") not in (None, expected_intro):
        raise FinalSubtitleAudioGateError("FINAL_SUBTITLE_AUDIO_RECORD_INTRO_OFFSET_DRIFT")
    if require_pass and receipt.get("status") != "PASS":
        raise FinalSubtitleAudioGateError(
            "FINAL_SUBTITLE_AUDIO_TIMING_BLOCK: "
            + ",".join(str(code) for code in receipt.get("reason_codes") or ["unknown"])
        )
    return receipt


def _record_evidence(
    *,
    record: dict[str, Any],
    candidate_id: str,
    output_dir: Path,
    paths: Mapping[str, Path],
    media_sha256: str,
    final_sha256: str,
    audio_sha256: str,
    raw_sha256: str,
    receipt: Mapping[str, object],
    cache_reused: bool,
) -> dict[str, Any]:
    witness_sha256 = _sha256(paths["witness_srt"])
    provenance_sha256 = _sha256(paths["provenance"])
    correspondence_sha256 = _sha256(paths["correspondence"])
    evidence = {
        "schema_version": GATE_SCHEMA_VERSION,
        "candidate_id": candidate_id,
        "output_dir": str(output_dir),
        "provider": PROVIDER_NAME,
        "provider_route": "scripts.free_asr_client.transcribe_bcut",
        "status": receipt.get("status"),
        "timing_status": receipt.get("timing_status"),
        "text_correctness_status": receipt.get("text_correctness_status"),
        "reason_codes": list(receipt.get("reason_codes") or []),
        "actual_media_sha256": media_sha256,
        "final_srt_sha256": final_sha256,
        "witness_srt_sha256": witness_sha256,
        "audio_sha256": audio_sha256,
        "raw_result_sha256": raw_sha256,
        "intro_offset_ms": receipt.get("timebase", {}).get("intro_offset_ms") if isinstance(receipt.get("timebase"), Mapping) else None,
        "cache_reused": cache_reused,
        "witness_srt_path": str(paths["witness_srt"]),
        "provenance_path": str(paths["provenance"]),
        "correspondence_path": str(paths["correspondence"]),
        "raw_result_path": str(paths["raw_result"]),
        "receipt": dict(receipt),
        "receipt_sha256": "sha256:" + hashlib.sha256(
            (json.dumps(receipt, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        ).hexdigest(),
    }
    record["subtitle_audio_correspondence"] = evidence
    hashes = record.setdefault("artifact_hashes", {})
    if isinstance(hashes, dict):
        hashes.update(
            {
                "subtitle_audio_witness_srt_sha256": witness_sha256,
                "subtitle_audio_provenance_sha256": provenance_sha256,
                "subtitle_audio_correspondence_sha256": correspondence_sha256,
                "subtitle_audio_bcut_raw_sha256": raw_sha256,
                "subtitle_audio_extracted_audio_sha256": audio_sha256,
            }
        )
    return record


def _load_cache(
    record: dict[str, Any],
    *,
    candidate_id: str,
    final_srt: Path,
    actual_media: Path,
    output_dir: Path,
    expected_intro: int,
) -> dict[str, Any] | None:
    paths = _artifact_paths(output_dir, candidate_id)
    if not all(path.is_file() and not path.is_symlink() for path in paths.values()):
        return None
    try:
        envelope = _read_json(paths["correspondence"], label="cached subtitle_audio_correspondence")
        if envelope.get("candidate_id") != candidate_id or envelope.get("provider") != PROVIDER_NAME:
            return None
        if _canonical_sha(envelope.get("actual_media_sha256"), label="cached_media") != _sha256(actual_media):
            return None
        if _canonical_sha(envelope.get("final_srt_sha256"), label="cached_final_srt") != _sha256(final_srt):
            return None
        if envelope.get("intro_offset_ms") != expected_intro:
            return None
        _canonical_sha(envelope.get("audio_sha256"), label="cached_audio")
        cached_record = dict(record)
        cached_record["subtitle_audio_correspondence"] = {
            "schema_version": GATE_SCHEMA_VERSION,
            "candidate_id": candidate_id,
            "output_dir": str(output_dir),
            "actual_media_sha256": envelope.get("actual_media_sha256"),
            "final_srt_sha256": envelope.get("final_srt_sha256"),
            "intro_offset_ms": envelope.get("intro_offset_ms"),
            "receipt": envelope.get("receipt"),
        }
        receipt = _validate_artifacts(
            cached_record,
            actual_media=actual_media,
            final_srt=final_srt,
            package_dir=output_dir,
            candidate_id=candidate_id,
            package_stem=candidate_id,
            require_pass=False,
        )
    except (FinalSubtitleAudioGateError, OSError, ValueError):
        return None
    return {"paths": paths, "envelope": envelope, "receipt": receipt}


def capture_final_subtitle_audio_check(
    record: dict[str, Any],
    final_srt: str | Path,
    output_dir: str | Path,
    candidate_id: str,
    *,
    adapters: FinalSubtitleAudioGateAdapters | None = None,
) -> dict[str, Any]:
    """Capture or safely resume the exact burned-media timing witness.

    The returned object is the same record mapping with a bound
    ``subtitle_audio_correspondence`` entry.  A timing ``BLOCK`` is retained
    in that entry so the producer can persist evidence before refusing stage;
    media/provider failures raise :class:`FinalSubtitleAudioGateError`.
    """

    candidate_id = _safe_candidate_id(candidate_id)
    final_path = _regular_file(final_srt, label="FINAL_SUBTITLE_AUDIO_FINAL_SRT")
    try:
        burned = _validated_burned_artifact(record)
    except Exception as exc:  # the producer media helper supplies the detail
        raise FinalSubtitleAudioGateError(
            f"FINAL_SUBTITLE_AUDIO_BURN_BINDING_INVALID: {exc}"
        ) from exc
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    if output.is_symlink() or not output.is_dir():
        raise FinalSubtitleAudioGateError("FINAL_SUBTITLE_AUDIO_OUTPUT_DIR_INVALID")
    expected_intro = _intro_offset_ms(record)
    cached = _load_cache(
        record,
        candidate_id=candidate_id,
        final_srt=final_path,
        actual_media=burned,
        output_dir=output,
        expected_intro=expected_intro,
    )
    if cached is not None:
        return _record_evidence(
            record=record,
            candidate_id=candidate_id,
            output_dir=output,
            paths=cached["paths"],
            media_sha256=_sha256(burned),
            final_sha256=_sha256(final_path),
            audio_sha256=str(cached["envelope"].get("audio_sha256")),
            raw_sha256=_sha256(cached["paths"]["raw_result"]),
            receipt=cached["receipt"],
            cache_reused=True,
        )

    selected = adapters or _DEFAULT_ADAPTERS
    paths = _artifact_paths(output, candidate_id)
    media_sha256 = _sha256(burned)
    final_sha256 = _sha256(final_path)
    with tempfile.TemporaryDirectory(prefix=f".{candidate_id}.subtitle-audio-", dir=output) as temp_name:
        audio_path = Path(temp_name) / "input.mp3"
        try:
            selected.extract_audio(burned, audio_path)
            audio_path = _regular_file(audio_path, label="FINAL_SUBTITLE_AUDIO_EXTRACTED_AUDIO")
            audio_bytes = audio_path.read_bytes()
        except FinalSubtitleAudioGateError:
            raise
        except (OSError, RuntimeError) as exc:
            raise FinalSubtitleAudioGateError(
                f"FINAL_SUBTITLE_AUDIO_EXTRACTION_FAILED: {exc}"
            ) from exc
    audio_sha256 = "sha256:" + hashlib.sha256(audio_bytes).hexdigest()
    try:
        raw_result = selected.transcribe_bcut(audio_bytes)
    except Exception as exc:  # provider errors must fail the producer gate
        raise FinalSubtitleAudioGateError(
            f"FINAL_SUBTITLE_AUDIO_BCUT_FAILED: {type(exc).__name__}: {exc}"
        ) from exc
    if not isinstance(raw_result, dict):
        raise FinalSubtitleAudioGateError("FINAL_SUBTITLE_AUDIO_BCUT_RESULT_INVALID")
    try:
        witness_text = selected.to_srt(raw_result)
    except Exception as exc:
        raise FinalSubtitleAudioGateError(
            f"FINAL_SUBTITLE_AUDIO_BCUT_TO_SRT_FAILED: {type(exc).__name__}: {exc}"
        ) from exc
    if not isinstance(witness_text, str):
        raise FinalSubtitleAudioGateError("FINAL_SUBTITLE_AUDIO_WITNESS_SRT_INVALID")
    raw_bytes = (json.dumps(raw_result, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    raw_sha256 = "sha256:" + hashlib.sha256(raw_bytes).hexdigest()
    witness_bytes = witness_text.encode("utf-8")
    witness_sha256 = "sha256:" + hashlib.sha256(witness_bytes).hexdigest()
    provenance = {
        "schema_version": PROVENANCE_SCHEMA_VERSION,
        "candidate_id": candidate_id,
        "actual_media_sha256": media_sha256,
        "witness_srt_sha256": witness_sha256,
        "audio_sha256": audio_sha256,
        "audio_format": "mp3",
        "provider": PROVIDER_NAME,
        "provider_route": "scripts.free_asr_client.transcribe_bcut",
        "timebase": {
            "unit": "ms",
            "final_srt": "delivery_local_ms",
            "witness_srt": "actual_media_local_ms",
            "intro_offset_application": "add_once_to_final_srt",
            "declared_intro_offset_ms": expected_intro,
        },
    }
    _write_bytes_atomic(paths["raw_result"], raw_bytes)
    _write_bytes_atomic(paths["witness_srt"], witness_bytes)
    _write_json_atomic(paths["provenance"], provenance)
    try:
        receipt = check_subtitle_audio_correspondence(
            final_path,
            burned,
            paths["witness_srt"],
            paths["provenance"],
            intro_offset_ms=expected_intro,
        )
    except CorrespondenceInputError as exc:
        receipt = {
            "schema_version": "final-subtitle-audio-correspondence-error.v1",
            "status": "BLOCK",
            "timing_status": "BLOCK",
            "text_correctness_status": "UNASSESSED",
            "reason_codes": [exc.reason_code],
            "error": exc.detail,
        }
    envelope = {
        "schema_version": GATE_SCHEMA_VERSION,
        "candidate_id": candidate_id,
        "provider": PROVIDER_NAME,
        "provider_route": "scripts.free_asr_client.transcribe_bcut",
        "actual_media_sha256": media_sha256,
        "final_srt_sha256": final_sha256,
        "witness_srt_sha256": witness_sha256,
        "audio_sha256": audio_sha256,
        "raw_result_sha256": raw_sha256,
        "intro_offset_ms": expected_intro,
        "receipt": receipt,
    }
    _write_json_atomic(paths["correspondence"], envelope)
    _record_evidence(
        record=record,
        candidate_id=candidate_id,
        output_dir=output,
        paths=paths,
        media_sha256=media_sha256,
        final_sha256=final_sha256,
        audio_sha256=audio_sha256,
        raw_sha256=raw_sha256,
        receipt=receipt,
        cache_reused=False,
    )
    return record


def validate_final_subtitle_audio_check(
    record: Mapping[str, object],
    final_srt: str | Path,
    actual_media: str | Path,
    package_root: str | Path | None = None,
    *,
    candidate_id: str | None = None,
) -> dict[str, Any]:
    """Re-run only deterministic validation; never calls BCUT or ffmpeg."""

    media_path = _regular_file(actual_media, label="FINAL_SUBTITLE_AUDIO_ACTUAL_MEDIA")
    final_path = _regular_file(final_srt, label="FINAL_SUBTITLE_AUDIO_FINAL_SRT")
    explicit_package = package_root is not None
    package = Path(package_root) if explicit_package else None
    package_stem: str | None = (
        str(candidate_id) if explicit_package and candidate_id else final_path.stem
    ) if explicit_package else None
    if package is None:
        evidence = record.get("subtitle_audio_correspondence")
        if isinstance(evidence, Mapping):
            raw_output = evidence.get("output_dir")
            if isinstance(raw_output, str) and raw_output:
                package = Path(raw_output)
                package_stem = str(evidence.get("candidate_id") or "") or None
    return _validate_artifacts(
        record,
        actual_media=media_path,
        final_srt=final_path,
        package_dir=package,
        candidate_id=candidate_id,
        package_stem=package_stem,
        require_pass=True,
    )


def require_final_subtitle_audio_check(
    record: dict, subtitle_path: Path, output_dir: Path, candidate_id: str,
    *, capture: Callable | None = None,
) -> dict:
    """Capture the final burn and stop the producer on missing or failed timing."""
    capture = capture or capture_final_subtitle_audio_check
    try:
        gated_record = capture(
            record,
            subtitle_path,
            output_dir,
            candidate_id,
        )
    except FinalSubtitleAudioGateError as exc:
        raise SystemExit(f"FINAL_SUBTITLE_AUDIO_CHECK_FAILED: {exc}") from exc
    except ValueError as exc:
        raise SystemExit(f"FINAL_SUBTITLE_AUDIO_CHECK_FAILED: {exc}") from exc
    if not isinstance(gated_record, dict):
        raise SystemExit(
            "FINAL_SUBTITLE_AUDIO_CHECK_FAILED: callback must return a record object"
        )
    record = gated_record
    correspondence = record.get("subtitle_audio_correspondence")
    if not isinstance(correspondence, Mapping):
        raise SystemExit(
            "FINAL_SUBTITLE_AUDIO_CHECK_FAILED: subtitle_audio_correspondence missing"
        )
    status = correspondence.get("status")
    timing_status = correspondence.get("timing_status")
    if status != "PASS" or timing_status != "PASS":
        reasons = correspondence.get("reason_codes")
        if not isinstance(reasons, list):
            receipt = correspondence.get("receipt")
            reasons = receipt.get("reason_codes") if isinstance(receipt, Mapping) else None
        raise SystemExit(
            "FINAL_SUBTITLE_AUDIO_CHECK_BLOCKED: "
            f"status={status!r} timing_status={timing_status!r} "
            f"reasons={reasons or ['unknown']}"
        )
    return record


def copy_subtitle_audio_artifacts(
    record: dict, *, candidate_id: str, delivery: Path, name: str,
    run_command: Callable,
) -> None:
    """Keep the current burn timing evidence portable with the final delivery."""
    for source in subtitle_audio_artifact_paths(record, candidate_id=candidate_id):
        if source.is_file() and not source.is_symlink():
            prefix = candidate_id
            if not source.name.startswith(prefix):
                raise SystemExit(
                    f"FINAL_SUBTITLE_AUDIO_ARTIFACT_NAME_INVALID: {source.name}"
                )
            run_command(
                [
                    "cp",
                    str(source),
                    str(delivery / f"{name}{source.name[len(prefix):]}"),
                ]
            )
