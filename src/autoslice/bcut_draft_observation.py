"""Reuse one immutable BCUT draft observation in the ordinary CPA route.

The producer normally cuts a hash-bound source piece before it starts the text
pipeline.  This module accepts a previously observed BCUT context only when
that observation, the source media, the request receipt, and the actual
producer piece all still bind to the same bytes and intervals.  The result is
an ASR draft observation, never a transcription-cache hit or final text
authority.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, NoReturn

from scripts.free_asr_client import to_srt
from src.autoslice.jingting_chunker import parse_srt_cues


SCHEMA_VERSION = "bcut-source-draft-observation.v1"
REUSE_RECEIPT_SCHEMA = "bcut-source-draft-observation-reuse.v1"
C20_REQUEST_SCHEMA = "c20-ordinary-source-asr-input-v02.v1"
C20_AUDIO_INPUT_SCHEMA = "c20-ordinary-source-asr-audio-input-v02.v1"
C20_AUDIO_RESULT_SCHEMA = "c20-ordinary-source-asr-audio-result-v02.v1"
C20_ASR_RESULT_SCHEMA = "c20-ordinary-source-asr-result-v02.v1"
MAX_RAW_BYTES = 32_000_000
MAX_UTTERANCES = 100_000
MAX_TRANSCRIPT_BYTES = 1_000_000
_BINDING_NAMES = ("source_media", "raw_response", "source_audio", "request_receipt")


class BcutDraftObservationError(ValueError):
    """The explicit observation cannot be safely reused."""


@dataclass(frozen=True)
class BcutDraftObservation:
    """The shifted, lossless draft and its producer-side reuse receipt."""

    srt_text: str
    receipt: dict[str, object]


def _fail(message: str) -> NoReturn:
    raise BcutDraftObservationError(message)


def _sha256_text(value: object, *, label: str) -> str:
    if not isinstance(value, str):
        _fail(f"{label} must be a sha256 string")
    normalized = value.removeprefix("sha256:").lower()
    if len(normalized) != 64 or any(c not in "0123456789abcdef" for c in normalized):
        _fail(f"{label} is not a sha256 digest")
    return normalized


def _require_mapping(value: object, *, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        _fail(f"{label} must be an object")
    return value


def _require_interval(value: object, *, label: str) -> tuple[int, int]:
    if (
        not isinstance(value, (list, tuple))
        or len(value) != 2
        or type(value[0]) is not int
        or type(value[1]) is not int
        or value[0] < 0
        or value[1] <= value[0]
    ):
        _fail(f"{label} must be a non-empty [start_ms, end_ms) interval")
    return int(value[0]), int(value[1])


def _reject_symlink_ancestors(path: Path) -> None:
    current = path
    while current != current.parent:
        try:
            if current.is_symlink():
                _fail(f"{path} has a symlink component")
        except OSError as exc:
            _fail(f"cannot inspect {path}: {exc}")
        current = current.parent


def _bound_path(binding: Mapping[str, object], *, label: str) -> tuple[Path, str, int]:
    raw_path = binding.get("path")
    if not isinstance(raw_path, str) or not raw_path or not Path(raw_path).is_absolute():
        _fail(f"{label}.path must be an absolute path")
    path = Path(raw_path)
    expected_sha = _sha256_text(binding.get("sha256"), label=f"{label}.sha256")
    expected_bytes = binding.get("bytes")
    if type(expected_bytes) is not int or expected_bytes <= 0:
        _fail(f"{label}.bytes must be a positive integer")
    return path, expected_sha, expected_bytes


def _read_bound_file(
    binding: Mapping[str, object],
    *,
    label: str,
    max_bytes: int | None = None,
    keep_bytes: bool = False,
) -> tuple[Path, str, int, bytes | None]:
    """Read and hash one immutable regular file in a single open."""

    path, expected_sha, expected_bytes = _bound_path(binding, label=label)
    if max_bytes is not None and expected_bytes > max_bytes:
        _fail(f"{label} exceeds the {max_bytes}-byte limit")
    _reject_symlink_ancestors(path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        _fail(f"{label} cannot be opened: {exc}")
    digest = hashlib.sha256()
    chunks: list[bytes] = []
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink < 1:
            _fail(f"{label} is not a regular file")
        if before.st_size != expected_bytes:
            _fail(f"{label} byte count drift")
        remaining = expected_bytes
        while remaining:
            chunk = os.read(fd, min(1024 * 1024, remaining))
            if not chunk:
                _fail(f"{label} ended before its bound byte count")
            digest.update(chunk)
            if keep_bytes:
                chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(fd)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            _fail(f"{label} changed while it was read")
    except OSError as exc:
        _fail(f"{label} could not be read: {exc}")
    finally:
        os.close(fd)
    actual_sha = digest.hexdigest()
    if actual_sha != expected_sha:
        _fail(f"{label} sha256 drift")
    return path, actual_sha, expected_bytes, b"".join(chunks) if keep_bytes else None


def _read_hashed_file(
    path: Path,
    expected_sha: str,
    *,
    label: str,
    max_bytes: int | None = None,
    keep_bytes: bool = False,
) -> tuple[Path, str, int, bytes | None]:
    """Read a typed companion file whose schema carries no byte-count field."""

    if not path.is_absolute():
        _fail(f"{label}.path must be an absolute path")
    _reject_symlink_ancestors(path)
    try:
        expected_bytes = path.stat().st_size
    except OSError as exc:
        _fail(f"{label} cannot be stat'ed: {exc}")
    return _read_bound_file(
        {
            "path": str(path),
            "sha256": expected_sha,
            "bytes": expected_bytes,
        },
        label=label,
        max_bytes=max_bytes,
        keep_bytes=keep_bytes,
    )


def _read_hashed_json(
    path: Path,
    expected_sha: str,
    *,
    label: str,
) -> tuple[dict[str, object], str, int]:
    _, actual_sha, size, payload = _read_hashed_file(
        path,
        expected_sha,
        label=label,
        max_bytes=MAX_RAW_BYTES,
        keep_bytes=True,
    )
    assert payload is not None
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        _fail(f"{label} JSON is invalid: {exc}")
    return dict(_require_mapping(document, label=label)), actual_sha, size


def _absolute_path(value: object, *, label: str) -> Path:
    if not isinstance(value, str) or not value or not Path(value).is_absolute():
        _fail(f"{label} must be an absolute path")
    return Path(value).absolute()


def _path_equal(left: object, right: Path) -> bool:
    if not isinstance(left, (str, Path)) or not Path(left).is_absolute():
        return False
    return Path(left).absolute() == right.absolute()


def _binding_document(
    value: object, *, label: str
) -> tuple[Path, str, int, dict[str, object]]:
    binding = _require_mapping(value, label=label)
    path, digest, size = _bound_path(binding, label=label)
    return path, digest, size, {
        "path": str(path),
        "sha256": f"sha256:{digest}",
        "bytes": size,
    }


def _pick(document: Mapping[str, object], *keys: str) -> object | None:
    for key in keys:
        if key in document:
            return document[key]
    return None


def _receipt_sha_with_legacy(
    document: Mapping[str, object],
    *,
    primary: str,
    legacy: str,
    nested: str,
) -> str:
    values: list[str] = []
    for key in (primary, legacy):
        if key in document:
            values.append(_sha256_text(document[key], label=f"request.{key}"))
    child = document.get(nested)
    if isinstance(child, Mapping) and "sha256" in child:
        values.append(_sha256_text(child["sha256"], label=f"request.{nested}.sha256"))
    if not values:
        _fail(f"request receipt has no {primary}")
    if len(set(values)) != 1:
        _fail(f"request receipt {primary} aliases conflict")
    return values[0]


def _invocation_option(argv: list[str], option: str, *, label: str) -> str:
    positions = [index for index, value in enumerate(argv) if value == option]
    if len(positions) != 1:
        _fail(f"{label} must contain one {option} option")
    position = positions[0]
    if position + 1 >= len(argv) or not isinstance(argv[position + 1], str):
        _fail(f"{label} {option} value is missing")
    value = argv[position + 1]
    if not value or value.startswith("-"):
        _fail(f"{label} {option} value is invalid")
    return value


def _parse_bcut_invocation(value: object, *, label: str) -> dict[str, object]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        _fail(f"request receipt {label} must be an argv list")
    script_positions = [
        index
        for index, item in enumerate(value)
        if Path(item).name == "free_asr_client.py"
    ]
    if len(script_positions) != 1:
        _fail(f"request receipt {label} must invoke one free_asr_client.py")
    script_index = script_positions[0]
    script_path = Path(value[script_index])
    if not script_path.is_absolute() or script_path.parent.name != "scripts":
        _fail(f"request receipt {label} free_asr_client.py path is invalid")
    if script_index + 1 >= len(value):
        _fail(f"request receipt {label} input audio is missing")
    input_path = Path(value[script_index + 1])
    if not input_path.is_absolute() or input_path.name.startswith("-"):
        _fail(f"request receipt {label} input audio path is invalid")
    provider_positions = [index for index, item in enumerate(value) if item == "--provider"]
    if len(provider_positions) != 1:
        _fail(f"request receipt {label} must contain one --provider bcut")
    provider_index = provider_positions[0]
    if (
        provider_index <= script_index + 1
        or provider_index + 1 >= len(value)
        or value[provider_index + 1] != "bcut"
    ):
        _fail(f"request receipt {label} provider command is not bcut")
    json_output = Path(_invocation_option(value, "--json", label=label))
    srt_output = Path(_invocation_option(value, "--srt", label=label))
    if not json_output.is_absolute() or not srt_output.is_absolute():
        _fail(f"request receipt {label} output paths must be absolute")
    if len({input_path.absolute(), json_output.absolute(), srt_output.absolute()}) != 3:
        _fail(f"request receipt {label} input and output paths are ambiguous")
    if any(
        index <= script_index + 1
        for index in (
            value.index("--json"),
            value.index("--srt"),
        )
    ):
        _fail(f"request receipt {label} output options occur before input audio")
    return {
        "provider": "bcut",
        "script_path": script_path.absolute(),
        "audio_path": input_path.absolute(),
        "raw_output_path": json_output.absolute(),
        "srt_output_path": srt_output.absolute(),
    }


def _validate_explicit_invocation_fields(
    document: Mapping[str, object],
    *,
    derived: Mapping[str, object],
) -> None:
    provider = document.get("provider")
    if provider is not None and provider != "bcut":
        _fail("request receipt explicit provider conflicts with bcut invocation")
    for key in ("audio_path", "source_audio_path", "input_audio_path"):
        if key in document and not _path_equal(document[key], derived["audio_path"]):
            _fail(f"request receipt explicit {key} conflicts with argv input")
    for key in ("raw_output", "raw_response_path"):
        if key in document and not _path_equal(document[key], derived["raw_output_path"]):
            _fail(f"request receipt explicit {key} conflicts with argv output")
    for key, derived_key in (
        ("source_audio", "audio_path"),
        ("input_audio", "audio_path"),
    ):
        child = document.get(key)
        if isinstance(child, Mapping) and "path" in child:
            if not _path_equal(child["path"], derived[derived_key]):
                _fail(f"request receipt explicit {key}.path conflicts with argv input")
    for key in ("raw_response", "output"):
        child = document.get(key)
        if isinstance(child, Mapping) and "path" in child:
            if not _path_equal(child["path"], derived["raw_output_path"]):
                _fail(f"request receipt explicit {key}.path conflicts with argv output")


def _parse_c20_invocation(
    value: object,
    *,
    label: str,
    client_path: Path,
) -> dict[str, object]:
    if isinstance(value, str):
        if label != "command":
            _fail(f"request receipt {label} must be an argv list")
        try:
            tokens = shlex.split(value, posix=True)
        except ValueError as exc:
            _fail(f"request receipt {label} is not valid shell syntax: {exc}")
    elif isinstance(value, list) and all(isinstance(item, str) for item in value):
        if label != "argv":
            _fail(f"request receipt {label} must be a command string")
        tokens = list(value)
    else:
        _fail(f"request receipt {label} has the wrong type")

    if len(tokens) != 11:
        _fail(f"request receipt {label} has an unsupported c20 invocation")
    interpreter = Path(tokens[0])
    script = Path(tokens[1])
    audio = Path(tokens[2])
    srt = Path(tokens[6])
    raw = Path(tokens[8])
    if (
        not interpreter.is_absolute()
        or not interpreter.name.startswith("python")
        or script != client_path
        or tokens[3:5] != ["--provider", "auto"]
        or tokens[5] != "--srt"
        or tokens[7] != "--json"
        or tokens[9:] != ["--offset-ms", "0"]
        or not audio.is_absolute()
        or not srt.is_absolute()
        or not raw.is_absolute()
    ):
        _fail(f"request receipt {label} does not match the c20 auto invocation")
    if len({audio.absolute(), srt.absolute(), raw.absolute()}) != 3:
        _fail(f"request receipt {label} input and output paths are ambiguous")
    return {
        "tokens": tokens,
        "interpreter_path": interpreter.absolute(),
        "script_path": script.absolute(),
        "audio_path": audio.absolute(),
        "srt_output_path": srt.absolute(),
        "raw_output_path": raw.absolute(),
        "offset_ms": 0,
    }


def _validate_c20_source_companions(
    *,
    source_path: Path,
    source_sha: str,
    source_bytes: int,
    source_input_sha: str,
    source_pts_sha: str,
    source_xml_sha: str,
    full_av_decode_sha: str,
) -> dict[str, object]:
    """Read the exact V02 source evidence documents and bind their contents.

    The c20 request carries hashes for these four typed documents, but a hash
    string alone does not prove that the bytes at the retained source sibling
    paths are the documents that produced the request.  The source media's
    parent is the authoritative run root for this schema; companion aliases
    from another directory are deliberately not accepted.
    """

    run_root = source_path.parent
    projection_root = run_root / "projection"
    source_input_path = run_root / "SOURCE-INPUT-V02.json"
    source_pts_path = projection_root / "SOURCE-PTS-BINDING-V02.json"
    source_xml_path = projection_root / "XML-CLOCK-PROJECTION-V02.json"
    full_av_path = projection_root / "DECODE-RESULT-V02.json"

    source_input, actual_source_input_sha, source_input_bytes = _read_hashed_json(
        source_input_path,
        source_input_sha,
        label="c20 source input",
    )
    if (
        source_input.get("schema_version") != "c20-ordinary-source-intake-input-v02.v1"
        or source_input.get("status") != "V02_VALID_READY_FOR_ASR"
    ):
        _fail("c20 source input is not a valid V02 ASR substrate")
    derived_media = _require_mapping(source_input.get("derived_media"), label="c20 source input.derived_media")
    projected_xml = _require_mapping(source_input.get("projected_xml"), label="c20 source input.projected_xml")
    if (
        derived_media.get("path") != str(source_path)
        or _sha256_text(derived_media.get("sha256"), label="c20 source input.derived_media.sha256")
        != source_sha
        or derived_media.get("size_bytes") != source_bytes
        or source_input.get("pts_binding_v02") != "projection/SOURCE-PTS-BINDING-V02.json"
        or projected_xml.get("path") != str(projection_root / "source.local-clock-v02.xml")
    ):
        _fail("c20 source input does not bind the derived source media")
    projected_xml_sha = _sha256_text(
        projected_xml.get("sha256"), label="c20 source input.projected_xml.sha256"
    )
    projected_xml_bytes = projected_xml.get("size_bytes")
    if projected_xml_bytes is not None and (
        type(projected_xml_bytes) is not int or projected_xml_bytes <= 0
    ):
        _fail("c20 source input projected XML size is invalid")
    _, actual_projected_xml_sha, actual_projected_xml_bytes, _ = _read_hashed_file(
        Path(str(projected_xml["path"])),
        projected_xml_sha,
        label="c20 projected XML",
        max_bytes=MAX_RAW_BYTES,
    )
    if projected_xml_bytes is not None and actual_projected_xml_bytes != projected_xml_bytes:
        _fail("c20 projected XML byte count drift")

    source_pts, actual_source_pts_sha, source_pts_bytes = _read_hashed_json(
        source_pts_path,
        source_pts_sha,
        label="c20 source PTS binding",
    )
    if (
        source_pts.get("schema_version") != "c20-ordinary-source-intake-pts-binding-v02.v1"
        or source_pts.get("status") != "VALID_BINDING_V02"
        or _sha256_text(
            source_pts.get("source_input_v02_sha256"),
            label="c20 source PTS binding.source_input_v02_sha256",
        )
        != source_input_sha
    ):
        _fail("c20 source PTS binding is not bound to the V02 source input")
    if (
        source_pts.get("derived_media") != str(source_path)
        or _sha256_text(source_pts.get("derived_media_sha256"), label="c20 source PTS binding.derived_media_sha256")
        != source_sha
        or source_pts.get("derived_media_size_bytes") != source_bytes
    ):
        _fail("c20 source PTS binding media drift")
    audio_clock = _require_mapping(source_pts.get("audio_clock"), label="c20 source PTS binding.audio_clock")
    audio_range = audio_clock.get("observed_range_ms")
    if (
        type(audio_clock.get("offset_ms_used_for_xml_projection")) is not int
        or not isinstance(audio_range, list)
        or len(audio_range) != 2
        or any(type(value) not in (int, float) or isinstance(value, bool) for value in audio_range)
    ):
        _fail("c20 source PTS binding clock evidence is malformed")
    packet_probe = _require_mapping(
        source_pts.get("derived_packet_probe"),
        label="c20 source PTS binding.derived_packet_probe",
    )
    if (
        packet_probe.get("rc") != 0
        or type(packet_probe.get("packet_count")) is not int
        or packet_probe["packet_count"] <= 0
        or packet_probe.get("data_hash_count") != packet_probe["packet_count"]
    ):
        _fail("c20 source PTS binding packet evidence is not successful")

    source_xml, actual_source_xml_sha, source_xml_bytes = _read_hashed_json(
        source_xml_path,
        source_xml_sha,
        label="c20 XML clock projection",
    )
    if (
        source_xml.get("schema_version") != "source-intake-xml-clock-projection-v02.v1"
        or source_xml.get("status") != "VALID_XML_PROJECTION_V02"
        or source_xml.get("xml_parse") != "PASS"
        or source_xml.get("actual_pts_receipt_sha256") is None
        or _sha256_text(
            source_xml.get("actual_pts_receipt_sha256"),
            label="c20 XML clock projection.actual_pts_receipt_sha256",
        )
        != source_pts_sha
    ):
        _fail("c20 XML clock projection is not bound to the V02 PTS receipt")
    output_xml_path = _absolute_path(
        source_xml.get("output_path_oci3"), label="c20 XML clock projection.output_path_oci3"
    )
    if (
        output_xml_path != Path(str(projected_xml["path"])).absolute()
        or _sha256_text(source_xml.get("output_sha256"), label="c20 XML clock projection.output_sha256")
        != projected_xml_sha
        or source_xml.get("output_size_bytes") != actual_projected_xml_bytes
        or source_xml.get("audio_parent_minus_local_ms")
        != audio_clock.get("offset_ms_used_for_xml_projection")
        or source_xml.get("audio_rounding_range_ms") != audio_range
        or source_xml.get("xml_parse") != "PASS"
    ):
        _fail("c20 XML clock projection clock or output drift")
    video_offset = source_pts.get("video_offset_ms_median")
    if source_xml.get("video_parent_minus_local_ms") != video_offset:
        _fail("c20 XML clock projection video offset drift")

    full_av, actual_full_av_sha, full_av_bytes = _read_hashed_json(
        full_av_path,
        full_av_decode_sha,
        label="c20 full AV decode result",
    )
    if (
        full_av.get("schema_version") != "c20-ordinary-source-full-av-decode-result-v02.v1"
        or full_av.get("status") != "DECODE_RC0_BOUND_TO_V02_MEDIA"
        or full_av.get("observed_rc") != 0
        or _sha256_text(
            full_av.get("source_input_v02_sha256"),
            label="c20 full AV decode result.source_input_v02_sha256",
        )
        != source_input_sha
        or _sha256_text(full_av.get("media_sha256"), label="c20 full AV decode result.media_sha256")
        != source_sha
        or full_av.get("media_size_bytes") != source_bytes
    ):
        _fail("c20 full AV decode result does not bind successful source decode")

    return {
        "source_input": {
            "path": str(source_input_path),
            "sha256": f"sha256:{actual_source_input_sha}",
            "bytes": source_input_bytes,
        },
        "source_pts_binding": {
            "path": str(source_pts_path),
            "sha256": f"sha256:{actual_source_pts_sha}",
            "bytes": source_pts_bytes,
            "audio_offset_ms": audio_clock["offset_ms_used_for_xml_projection"],
            "audio_observed_range_ms": list(audio_range),
            "video_offset_ms": video_offset,
            "packet_count": packet_probe["packet_count"],
        },
        "source_xml_projection": {
            "path": str(source_xml_path),
            "sha256": f"sha256:{actual_source_xml_sha}",
            "bytes": source_xml_bytes,
            "output_path": str(output_xml_path),
            "output_sha256": f"sha256:{actual_projected_xml_sha}",
            "output_bytes": actual_projected_xml_bytes,
        },
        "full_av_decode": {
            "path": str(full_av_path),
            "sha256": f"sha256:{actual_full_av_sha}",
            "bytes": full_av_bytes,
            "status": full_av["status"],
            "observed_rc": full_av["observed_rc"],
        },
    }


def _validate_c20_extraction_chain(
    *,
    request_path: Path,
    source_path: Path,
    source_sha: str,
    source_bytes: int,
    source_audio_path: Path,
    source_audio_sha: str,
    source_audio_bytes: int,
    client_path: Path,
    interpreter_path: Path,
    client_sha: str,
    source_input_sha: str,
    source_pts_sha: str,
    source_xml_sha: str,
    full_av_decode_sha: str,
    extract_result_sha: str,
) -> dict[str, object]:
    extract_result_path = request_path.with_name("EXTRACT-RESULT.json")
    extract_result, actual_extract_sha, _ = _read_hashed_json(
        extract_result_path,
        extract_result_sha,
        label="c20 extraction result",
    )
    if (
        extract_result.get("schema_version") != C20_AUDIO_RESULT_SCHEMA
        or extract_result.get("status") != "REQUEST_AUDIO_SAVED"
        or extract_result.get("provider_dispatch_started") is not False
    ):
        _fail("c20 extraction result is not a frozen pre-dispatch receipt")

    input_ref = extract_result.get("input_path")
    if input_ref is not None and input_ref != "asr-auto/EXTRACT-INPUT.json":
        _fail("c20 extraction result input path is not the typed companion")
    extract_input_path = request_path.with_name("EXTRACT-INPUT.json")
    extract_input_sha = _sha256_text(
        extract_result.get("source_input_v02_sha256"),
        label="c20 extraction result.source_input_v02_sha256",
    )
    extract_input, actual_extract_input_sha, _ = _read_hashed_json(
        extract_input_path,
        extract_input_sha,
        label="c20 extraction input",
    )
    if actual_extract_input_sha != extract_input_sha:
        _fail("c20 extraction input hash drift")
    if (
        extract_input.get("schema_version") != C20_AUDIO_INPUT_SCHEMA
        or extract_input.get("status") != "READY_TO_EXTRACT_AUDIO"
        or extract_input.get("provider") != "auto"
        or extract_input.get("public_or_upload") is not False
        or extract_input.get("selection_or_production") is not False
        or extract_input.get("provider_dispatches_before_launch") != 0
        or extract_input.get("offset_ms") != 0
        or extract_input.get("source_media_path") != str(source_path)
        or _sha256_text(extract_input.get("source_media_sha256"), label="c20 extraction input.source_media_sha256")
        != source_sha
        or extract_input.get("source_media_size_bytes") != source_bytes
        or extract_input.get("output_mp3_path") != str(source_audio_path)
        or extract_input.get("extractor_function")
        != "scripts.free_asr_client.extract_audio_mp3"
        or extract_input.get("extractor_signature") != "(media: Path) -> bytes"
        or _sha256_text(extract_input.get("extractor_sha256"), label="c20 extraction input.extractor_sha256")
        != client_sha
        or _sha256_text(
            extract_input.get("source_input_v02_sha256"),
            label="c20 extraction input.source_input_v02_sha256",
        )
        != source_input_sha
        or _sha256_text(
            extract_input.get("source_pts_binding_v02_sha256"),
            label="c20 extraction input.source_pts_binding_v02_sha256",
        )
        != source_pts_sha
        or _sha256_text(
            extract_input.get("source_xml_projection_v02_sha256"),
            label="c20 extraction input.source_xml_projection_v02_sha256",
        )
        != source_xml_sha
        or _sha256_text(
            extract_input.get("full_av_decode_v02_sha256"),
            label="c20 extraction input.full_av_decode_v02_sha256",
        )
        != full_av_decode_sha
    ):
        _fail("c20 extraction input does not bind the source or extractor")
    extractor_module = extract_input.get("extractor_module")
    if extractor_module != str(client_path):
        _fail("c20 extraction input extractor module drift")
    command = extract_input.get("command")
    if (
        not isinstance(command, list)
        or len(command) != 3
        or any(not isinstance(item, str) for item in command)
        or Path(command[0]).absolute() != interpreter_path
        or command[1] != "-c"
        or "extract_audio_mp3" not in command[2]
        or str(source_path) not in command[2]
        or str(source_audio_path) not in command[2]
    ):
        _fail("c20 extraction input command does not bind source audio")

    if (
        extract_result.get("output_mp3_path") != str(source_audio_path)
        or _sha256_text(extract_result.get("output_mp3_sha256"), label="c20 extraction result.output_mp3_sha256")
        != source_audio_sha
        or extract_result.get("output_mp3_size_bytes") != source_audio_bytes
        or _sha256_text(extract_result.get("extractor_sha256"), label="c20 extraction result.extractor_sha256")
        != client_sha
        or extract_result.get("provider_dispatch_started") is not False
        or extract_result.get("status") != "REQUEST_AUDIO_SAVED"
    ):
        _fail("c20 extraction result does not bind the request MP3")
    return {
        "schema_version": C20_AUDIO_RESULT_SCHEMA,
        "path": str(extract_result_path),
        "sha256": f"sha256:{actual_extract_sha}",
        "input_path": str(extract_input_path),
        "input_sha256": f"sha256:{actual_extract_input_sha}",
    }


def _validate_c20_request_receipt(
    document: Mapping[str, object],
    *,
    receipt_binding: Mapping[str, object],
    request_path: Path,
    request_sha: str,
    request_bytes: int,
    source_path: Path,
    source_sha: str,
    source_bytes: int,
    source_audio_path: Path,
    source_audio_sha: str,
    source_audio_bytes: int,
    raw_response_path: Path,
    raw_response_sha: str,
    observation: Mapping[str, object],
    context: tuple[int, int],
    source_offset_ms: int,
    raw_document: Mapping[str, object],
) -> dict[str, object]:
    if document.get("schema_version") != C20_REQUEST_SCHEMA:
        _fail("unsupported c20 request receipt schema")
    if (
        document.get("status") != "READY_TO_LAUNCH"
        or document.get("provider_requested") != "auto"
        or document.get("auto_chain") != ["bcut", "jianying"]
        or document.get("provider_dispatches_before_launch") != 0
        or document.get("public_or_upload") is not False
        or document.get("selection_or_production") is not False
        or document.get("offset_ms") != 0
        or source_offset_ms != 0
    ):
        _fail("c20 request receipt pre-dispatch fields drift")
    preflight = _require_mapping(document.get("target_preflight"), label="c20 request.target_preflight")
    if preflight.get("srt") != "ABSENT" or preflight.get("raw_json") != "ABSENT":
        _fail("c20 request receipt target preflight is not pre-dispatch")

    request_source_sha = _sha256_text(
        document.get("source_media_sha256"), label="c20 request.source_media_sha256"
    )
    if (
        document.get("source_media_path") != str(source_path)
        or request_source_sha != source_sha
        or (
            "source_media_size_bytes" in document
            and document.get("source_media_size_bytes") != source_bytes
        )
    ):
        _fail("c20 request receipt source media binding drift")
    request_source_input_sha = _sha256_text(
        document.get("source_input_v02_sha256"), label="c20 request.source_input_v02_sha256"
    )
    request_pts_sha = _sha256_text(
        document.get("source_pts_binding_v02_sha256"),
        label="c20 request.source_pts_binding_v02_sha256",
    )
    request_xml_sha = _sha256_text(
        document.get("source_xml_projection_v02_sha256"),
        label="c20 request.source_xml_projection_v02_sha256",
    )
    request_full_av_decode_sha = _sha256_text(
        document.get("full_av_decode_v02_sha256"),
        label="c20 request.full_av_decode_v02_sha256",
    )
    for key, expected in (
        ("source_input_v02_sha256", request_source_input_sha),
        ("source_pts_binding_v02_sha256", request_pts_sha),
        ("source_xml_projection_v02_sha256", request_xml_sha),
    ):
        observed = _sha256_text(observation.get(key), label=f"observation.{key}")
        if observed != expected:
            _fail(f"c20 observation {key} drift")
    if "full_av_decode_v02_sha256" in observation:
        observed_full_av_decode_sha = _sha256_text(
            observation["full_av_decode_v02_sha256"],
            label="observation.full_av_decode_v02_sha256",
        )
        if observed_full_av_decode_sha != request_full_av_decode_sha:
            _fail("c20 observation full_av_decode_v02_sha256 drift")

    source_companions = _validate_c20_source_companions(
        source_path=source_path,
        source_sha=source_sha,
        source_bytes=source_bytes,
        source_input_sha=request_source_input_sha,
        source_pts_sha=request_pts_sha,
        source_xml_sha=request_xml_sha,
        full_av_decode_sha=request_full_av_decode_sha,
    )

    client_path = _absolute_path(document.get("client_path"), label="c20 request.client_path")
    client_sha = _sha256_text(document.get("client_sha256"), label="c20 request.client_sha256")
    request_mp3_path = _absolute_path(
        document.get("request_mp3_path"), label="c20 request.request_mp3_path"
    )
    request_mp3_sha = _sha256_text(
        document.get("request_mp3_sha256"), label="c20 request.request_mp3_sha256"
    )
    if (
        request_mp3_path != source_audio_path
        or request_mp3_sha != source_audio_sha
        or document.get("request_mp3_size_bytes") != source_audio_bytes
    ):
        _fail("c20 request receipt MP3 binding drift")
    extract_result_sha = _sha256_text(
        document.get("request_audio_receipt_sha256"),
        label="c20 request.request_audio_receipt_sha256",
    )
    if observation.get("source_offset_ms") != 0:
        _fail("c20 observation source offset drift")

    invocation = _parse_c20_invocation(document.get("argv"), label="argv", client_path=client_path)
    command_invocation = _parse_c20_invocation(
        document.get("command"), label="command", client_path=client_path
    )
    for key in ("interpreter_path", "script_path", "audio_path", "srt_output_path", "raw_output_path", "offset_ms"):
        if invocation[key] != command_invocation[key]:
            _fail(f"c20 request argv and command conflict at {key}")
    if (
        invocation["audio_path"] != source_audio_path
        or invocation["raw_output_path"] != raw_response_path
        or invocation["srt_output_path"] != _absolute_path(
            document.get("srt_output_path"), label="c20 request.srt_output_path"
        )
        or invocation["raw_output_path"] != _absolute_path(
            document.get("raw_json_output_path"), label="c20 request.raw_json_output_path"
        )
    ):
        _fail("c20 request invocation output binding drift")

    if (
        receipt_binding.get("bytes") != request_bytes
        or _sha256_text(receipt_binding.get("sha256"), label="request_receipt.sha256") != request_sha
        or _absolute_path(receipt_binding.get("path"), label="request_receipt.path") != request_path
        or not isinstance(receipt_binding.get("result_path"), str)
        or not isinstance(receipt_binding.get("result_sha256"), str)
        or not isinstance(receipt_binding.get("extract_result_sha256"), str)
    ):
        _fail("c20 request receipt binding metadata is incomplete")
    result_path = _absolute_path(receipt_binding["result_path"], label="request_receipt.result_path")
    expected_result_path = request_path.with_name("ASR-RESULT.json")
    if result_path != expected_result_path:
        _fail("c20 request result path is not the typed companion")
    result_sha = _sha256_text(
        receipt_binding["result_sha256"], label="request_receipt.result_sha256"
    )
    if _sha256_text(receipt_binding["extract_result_sha256"], label="request_receipt.extract_result_sha256") != extract_result_sha:
        _fail("c20 request extraction receipt hash drift")

    extraction = _validate_c20_extraction_chain(
        request_path=request_path,
        source_path=source_path,
        source_sha=source_sha,
        source_bytes=source_bytes,
        source_audio_path=source_audio_path,
        source_audio_sha=source_audio_sha,
        source_audio_bytes=source_audio_bytes,
        client_path=client_path,
        interpreter_path=invocation["interpreter_path"],
        client_sha=client_sha,
        source_input_sha=request_source_input_sha,
        source_pts_sha=request_pts_sha,
        source_xml_sha=request_xml_sha,
        full_av_decode_sha=request_full_av_decode_sha,
        extract_result_sha=extract_result_sha,
    )
    result, actual_result_sha, _ = _read_hashed_json(
        result_path,
        result_sha,
        label="c20 ASR result",
    )
    if (
        result.get("schema_version") != C20_ASR_RESULT_SCHEMA
        or result.get("status") != "SUCCEEDED_DRAFT_ASR"
        or result.get("provider_requested") != "auto"
        or result.get("auto_chain") != ["bcut", "jianying"]
        or result.get("provider_result") != "bcut"
        or result.get("offset_ms") != 0
        or result.get("source_media_path") != str(source_path)
        or _sha256_text(result.get("source_media_sha256"), label="c20 ASR result.source_media_sha256")
        != source_sha
        or result.get("request_mp3_path") != str(source_audio_path)
        or _sha256_text(result.get("request_mp3_sha256"), label="c20 ASR result.request_mp3_sha256")
        != source_audio_sha
        or result.get("request_mp3_size_bytes") != source_audio_bytes
        or _sha256_text(result.get("client_sha256"), label="c20 ASR result.client_sha256") != client_sha
        or result.get("client_path") != str(client_path)
        or _sha256_text(result.get("source_input_v02_sha256"), label="c20 ASR result.source_input_v02_sha256")
        != request_sha
        or _sha256_text(
            result.get("source_pts_binding_v02_sha256"),
            label="c20 ASR result.source_pts_binding_v02_sha256",
        )
        != request_pts_sha
        or _sha256_text(
            result.get("source_xml_projection_v02_sha256"),
            label="c20 ASR result.source_xml_projection_v02_sha256",
        )
        != request_xml_sha
        or _sha256_text(result.get("audio_extract_result_sha256"), label="c20 ASR result.audio_extract_result_sha256")
        != extract_result_sha
        or result.get("raw_json_path") != str(raw_response_path)
        or _sha256_text(result.get("raw_json_sha256"), label="c20 ASR result.raw_json_sha256")
        != raw_response_sha
        or result.get("source_clock")
        != "derived local audio clock bound by exact packet SHA/PTS; offset-ms CLI=0"
    ):
        _fail("c20 ASR result source/request binding drift")
    dispatches = result.get("provider_dispatches")
    if (dispatches != {"bcut": 1, "jianying": 0, "kuaishou": 0}
            or any(type(value) is not int for value in dispatches.values())):
        _fail("c20 ASR result provider dispatch evidence drift")
    provider_evidence = _require_mapping(
        result.get("actual_provider_evidence"), label="c20 ASR result.actual_provider_evidence"
    )
    task_id = result.get("bcut_task_id")
    task_created_line = provider_evidence.get("stdout_task_created_line")
    if (
        provider_evidence.get("raw_provider") != "bcut"
        or not isinstance(task_id, str)
        or not task_id
        or not isinstance(task_created_line, str)
        or task_created_line != f"[bcut] task created: {task_id}"
    ):
        _fail("c20 ASR result provider evidence is missing")
    _sha256_text(provider_evidence.get("stdout_sha256"), label="c20 ASR result.stdout_sha256")

    srt_document = _require_mapping(observation.get("srt"), label="observation.srt")
    srt_path = _absolute_path(srt_document.get("path"), label="observation.srt.path")
    srt_sha = _sha256_text(srt_document.get("sha256"), label="observation.srt.sha256")
    if (
        srt_path != invocation["srt_output_path"]
        or result.get("srt_path") != str(srt_path)
        or _sha256_text(result.get("srt_sha256"), label="c20 ASR result.srt_sha256") != srt_sha
    ):
        _fail("c20 ASR result SRT binding drift")
    srt_path, _, _, srt_payload = _read_hashed_file(
        srt_path,
        srt_sha,
        label="c20 source SRT",
        max_bytes=MAX_RAW_BYTES,
        keep_bytes=True,
    )
    assert srt_payload is not None
    try:
        srt_text = srt_payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        _fail(f"c20 source SRT is not UTF-8: {exc}")
    if srt_text != to_srt(raw_document, offset_ms=0):
        _fail("c20 source SRT does not match the bound raw response")
    try:
        srt_cues = parse_srt_cues(srt_text)
    except ValueError as exc:
        _fail(f"c20 source SRT is invalid: {exc}")
    utterances = raw_document.get("utterances")
    if not isinstance(utterances, list) or not utterances:
        _fail("c20 raw response has no utterance list")
    if (
        type(srt_document.get("utterance_count")) is not int
        or srt_document["utterance_count"] != len(utterances)
        or result.get("utterance_count") != len(utterances)
        or len(srt_cues) != len(utterances)
    ):
        _fail("c20 SRT utterance count drift")
    if (
        not isinstance(utterances[0], Mapping)
        or not isinstance(utterances[-1], Mapping)
        or type(utterances[0].get("start_time")) is not int
        or type(utterances[0].get("end_time")) is not int
        or type(utterances[-1].get("start_time")) is not int
        or type(utterances[-1].get("end_time")) is not int
    ):
        _fail("c20 raw first/last utterance is malformed")
    result_first = _require_interval(result.get("first_utterance_ms"), label="c20 ASR result.first_utterance_ms")
    result_last = _require_interval(result.get("last_utterance_ms"), label="c20 ASR result.last_utterance_ms")
    if result_first != (
        int(utterances[0]["start_time"]),
        int(utterances[0]["end_time"]),
    ) or result_last != (
        int(utterances[-1]["start_time"]),
        int(utterances[-1]["end_time"]),
    ):
        _fail("c20 ASR result first/last cue drift")
    if result_first[0] < context[0] or result_last[1] > context[1]:
        _fail("c20 ASR result cue clock is outside the observed context")
    return {
        "schema_version": C20_REQUEST_SCHEMA,
        "request_path": str(request_path),
        "request_sha256": f"sha256:{request_sha}",
        "request_result_path": str(result_path),
        "request_result_sha256": f"sha256:{actual_result_sha}",
        "request_audio_result": extraction,
        "source_srt_path": str(srt_path),
        "source_srt_sha256": f"sha256:{srt_sha}",
        "source_companions": source_companions,
        "source_clock": result.get("source_clock"),
        "provider_dispatches": dict(dispatches),
    }


def _validate_request_receipt(
    document: Mapping[str, object],
    *,
    source_path: Path,
    source_sha: str,
    source_audio_path: Path,
    source_audio_sha: str,
    raw_response_path: Path,
    context: tuple[int, int],
    source_offset_ms: int,
) -> dict[str, object] | None:
    request_source_sha = _receipt_sha_with_legacy(
        document,
        primary="source_media_sha256",
        legacy="source_sha256",
        nested="source_media",
    )
    if request_source_sha != source_sha:
        _fail("request receipt source media hash drift")
    source_path_values = [
        document[key]
        for key in ("source_media_path", "source_path")
        if key in document
    ]
    if source_path_values and any(
        not _path_equal(value, source_path) for value in source_path_values
    ):
        _fail("request receipt source media path drift")
    request_audio_sha = _receipt_sha_with_legacy(
        document,
        primary="source_audio_sha256",
        legacy="audio_sha256",
        nested="source_audio",
    )
    if request_audio_sha != source_audio_sha:
        _fail("request receipt source audio hash drift")

    context_values = [
        _require_interval(document[key], label=f"request.{key}")
        for key in ("source_context_ms", "source_window_ms")
        if key in document
    ]
    finite_local = document.get("srt_timestamp_domain") == "finite_local_ms"
    projection = None
    if finite_local:
        global_context = _require_interval(
            document.get("source_context_global_ms"), label="request.source_context_global_ms"
        )
        if (
            document.get("global_mapping") != "source_ms = finite_local_ms + source_offset_ms"
            or document.get("local_srt_timestamps_are_offset_by_adapter") is not False
            or context != (0, global_context[1] - global_context[0])
            or source_offset_ms != 0
        ):
            _fail("request receipt finite-local clock projection drift")
        projection = {
            "srt_timestamp_domain": "finite_local_ms",
            "source_context_global_ms": list(global_context),
            "global_source_origin_ms": global_context[0],
            "consumer_source_context_ms": list(context),
            "consumer_source_offset_ms": 0,
            "authority": "COORDINATE_PROJECTION_ONLY",
        }
    if not context_values and not finite_local:
        _fail("request receipt has no source context interval")
    if context_values and (len(set(context_values)) != 1 or context_values[0] != context):
        _fail("request receipt source context drift")
    offset_values = [
        document[key]
        for key in ("source_offset_ms", "global_offset_ms")
        if key in document
    ]
    if (
        not offset_values
        or any(type(value) is not int for value in offset_values)
        or len(set(offset_values)) != 1
        or offset_values[0] != (projection["global_source_origin_ms"] if projection else source_offset_ms)
    ):
        _fail("request receipt source offset drift")

    invocations = []
    if "argv" in document:
        invocations.append(_parse_bcut_invocation(document["argv"], label="argv"))
    if "command" in document:
        invocations.append(_parse_bcut_invocation(document["command"], label="command"))
    if not invocations:
        _fail("request receipt has neither argv nor command")
    if finite_local:
        for key in ("argv", "command"):
            if key in document and _invocation_option(document[key], "--offset-ms", label=key) != "0":
                _fail("request receipt finite-local invocation offset drift")
    derived = invocations[0]
    for other in invocations[1:]:
        for key in ("provider", "script_path", "audio_path", "raw_output_path", "srt_output_path"):
            if other[key] != derived[key]:
                _fail(f"request receipt argv and command conflict at {key}")
    _validate_explicit_invocation_fields(document, derived=derived)
    if not _path_equal(derived["audio_path"], source_audio_path):
        _fail("request receipt input audio path drift")
    if not _path_equal(derived["raw_output_path"], raw_response_path):
        _fail("request receipt raw output path drift")
    return projection


def _validate_producer_piece(
    *,
    producer_spec: Mapping[str, object],
    padded: Path,
    source_path: Path,
    source_sha: str,
    source_interval: tuple[int, int],
) -> dict[str, object]:
    pieces = producer_spec.get("pieces")
    if not isinstance(pieces, list) or len(pieces) != 1 or not isinstance(pieces[0], Mapping):
        _fail("BCUT observation requires exactly one normal producer piece")
    piece = pieces[0]
    piece_path_value = piece.get("remote_media")
    if not _path_equal(piece_path_value, source_path):
        _fail("producer piece source path drift")
    if (
        type(piece.get("start_ms")) is not int
        or type(piece.get("end_ms")) is not int
        or (int(piece["start_ms"]), int(piece["end_ms"])) != source_interval
    ):
        _fail("producer piece interval drift")
    declared_sha = piece.get("source_media_sha256")
    if declared_sha is not None and _sha256_text(declared_sha, label="piece.source_media_sha256") != source_sha:
        _fail("producer piece source hash drift")

    start_ms, end_ms = source_interval
    expected_piece_path = padded.parent / f"piece_0_{start_ms}_{end_ms}.mp4"
    expected_piece_receipt = expected_piece_path.with_suffix(".provenance.json")
    _reject_symlink_ancestors(expected_piece_receipt)
    _reject_symlink_ancestors(expected_piece_path)
    try:
        piece_receipt = json.loads(expected_piece_receipt.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _fail(f"piece provenance cannot be read: {exc}")
    piece_receipt = _require_mapping(piece_receipt, label="piece provenance")
    if (
        piece_receipt.get("source_path") != str(source_path)
        or _sha256_text(piece_receipt.get("source_sha256"), label="piece.source_sha256") != source_sha
        or piece_receipt.get("source_media_binding") != f"sha256:{source_sha}"
        or piece_receipt.get("start_ms") != start_ms
        or piece_receipt.get("end_ms") != end_ms
        or not _path_equal(piece_receipt.get("output_path"), expected_piece_path)
    ):
        _fail("piece provenance does not bind the observation source")
    _, piece_sha, piece_bytes, _ = _read_bound_file(
        {
            "path": str(expected_piece_path),
            "sha256": piece_receipt.get("output_sha256"),
            "bytes": expected_piece_path.stat().st_size,
        },
        label="producer piece",
    )
    if piece_sha != _sha256_text(piece_receipt.get("output_sha256"), label="piece.output_sha256"):
        _fail("piece output hash drift")

    padded_receipt_path = padded.with_suffix(".provenance.json")
    _reject_symlink_ancestors(padded_receipt_path)
    _reject_symlink_ancestors(padded)
    try:
        padded_document = json.loads(padded_receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _fail(f"padded provenance cannot be read: {exc}")
    padded_document = _require_mapping(padded_document, label="padded provenance")
    inputs = padded_document.get("inputs")
    if (
        not isinstance(inputs, list)
        or len(inputs) != 1
        or not isinstance(inputs[0], Mapping)
        or not _path_equal(inputs[0].get("path"), expected_piece_path)
        or _sha256_text(inputs[0].get("sha256"), label="padded.inputs.sha256") != piece_sha
        or not _path_equal(padded_document.get("output_path"), padded)
    ):
        _fail("padded provenance does not bind the one-piece producer output")
    padded_sha = _sha256_text(padded_document.get("output_sha256"), label="padded.output_sha256")
    _, actual_padded_sha, padded_bytes, _ = _read_bound_file(
        {"path": str(padded), "sha256": padded_sha, "bytes": padded.stat().st_size},
        label="padded media",
    )
    if actual_padded_sha != piece_sha or padded_bytes != piece_bytes:
        _fail("one-piece padded media is not the exact producer piece")
    return {
        "source_interval_ms": [start_ms, end_ms],
        "piece_path": str(expected_piece_path),
        "piece_sha256": f"sha256:{piece_sha}",
        "piece_bytes": piece_bytes,
        "padded_path": str(padded),
        "padded_sha256": f"sha256:{actual_padded_sha}",
        "padded_bytes": padded_bytes,
        "piece_provenance_path": str(expected_piece_receipt),
        "padded_provenance_path": str(padded_receipt_path),
    }


def _write_reuse_receipt(path: Path, document: dict[str, object]) -> None:
    _reject_symlink_ancestors(path)
    payload = (json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    if path.exists():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
            _fail("BCUT reuse receipt path conflict")
        return
    temporary: Path | None = None
    try:
        fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temporary = Path(name)
        with os.fdopen(fd, "wb") as out:
            out.write(payload)
            out.flush()
            os.fsync(out.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        temporary = None
    except OSError as exc:
        _fail(f"cannot write BCUT reuse receipt: {exc}")
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def load_bcut_draft_observation(
    observation: Mapping[str, object],
    *,
    padded: Path,
    producer_spec: Mapping[str, object],
    padded_duration_ms: int | None = None,
) -> BcutDraftObservation:
    """Validate and shift one explicit BCUT observation onto a padded piece."""

    observation = _require_mapping(observation, label="bcut_draft_observation")
    producer_spec = _require_mapping(producer_spec, label="producer_spec")
    if observation.get("schema_version") != SCHEMA_VERSION:
        _fail("unsupported BCUT draft observation schema")
    if observation.get("provider") != "bcut":
        _fail("BCUT draft observation provider is not bcut")
    candidate_id = observation.get("candidate_id")
    producer_id = producer_spec.get("id")
    if (
        not isinstance(candidate_id, str)
        or not candidate_id.strip()
        or not isinstance(producer_id, str)
        or not producer_id.strip()
        or candidate_id != producer_id
    ):
        _fail("BCUT observation candidate_id does not match producer_spec.id")
    source_interval = _require_interval(observation.get("source_interval_ms"), label="source_interval_ms")
    source_context = _require_interval(observation.get("source_context_ms"), label="source_context_ms")
    source_offset = observation.get("source_offset_ms")
    if type(source_offset) is not int:
        _fail("source_offset_ms must be an integer")
    if source_interval[0] < source_context[0] or source_interval[1] > source_context[1]:
        _fail("source interval is outside the observed source context")
    bindings: dict[str, dict[str, object]] = {}
    bound: dict[str, tuple[Path, str, int, bytes | None]] = {}
    for name in _BINDING_NAMES:
        path, digest, size, normalized = _binding_document(
            observation.get(name), label=name
        )
        bindings[name] = normalized
        bound[name] = _read_bound_file(
            {"path": str(path), "sha256": digest, "bytes": size},
            label=name,
            max_bytes=MAX_RAW_BYTES if name in {"raw_response", "request_receipt"} else None,
            keep_bytes=name in {"raw_response", "request_receipt"},
        )

    source_path, source_sha, source_bytes, _ = bound["source_media"]
    raw_path, raw_sha, _, raw_payload = bound["raw_response"]
    source_audio_path, source_audio_sha, source_audio_bytes, _ = bound["source_audio"]
    request_path, request_sha, request_bytes, request_payload = bound["request_receipt"]
    request_binding = _require_mapping(
        observation.get("request_receipt"), label="request_receipt"
    )
    assert raw_payload is not None and request_payload is not None

    try:
        raw_document = json.loads(raw_payload.decode("utf-8"))
        request_document = json.loads(request_payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        _fail(f"BCUT observation JSON is invalid: {exc}")
    raw_document = _require_mapping(raw_document, label="raw BCUT response")
    request_document = _require_mapping(request_document, label="BCUT request receipt")
    if raw_document.get("provider") not in (None, "bcut"):
        _fail("raw BCUT response provider is not bcut")
    request_receipt_compatibility = None
    if request_document.get("schema_version") == C20_REQUEST_SCHEMA:
        request_receipt_compatibility = _validate_c20_request_receipt(
            request_document,
            receipt_binding=request_binding,
            request_path=request_path,
            request_sha=request_sha,
            request_bytes=request_bytes,
            source_path=source_path,
            source_sha=source_sha,
            source_bytes=source_bytes,
            source_audio_path=source_audio_path,
            source_audio_sha=source_audio_sha,
            source_audio_bytes=source_audio_bytes,
            raw_response_path=raw_path,
            raw_response_sha=raw_sha,
            observation=observation,
            context=source_context,
            source_offset_ms=source_offset,
            raw_document=raw_document,
        )
        request_clock_projection = None
    else:
        request_clock_projection = _validate_request_receipt(
            request_document,
            source_path=source_path,
            source_sha=source_sha,
            source_audio_path=source_audio_path,
            source_audio_sha=source_audio_sha,
            raw_response_path=raw_path,
            context=source_context,
            source_offset_ms=source_offset,
        )

    utterances = raw_document.get("utterances")
    if not isinstance(utterances, list) or not utterances or len(utterances) > MAX_UTTERANCES:
        _fail("raw BCUT response has no bounded utterance list")
    selected: list[dict[str, object]] = []
    word_timings_present = False
    observed_count = len(utterances)
    for index, row in enumerate(utterances):
        if not isinstance(row, Mapping):
            _fail(f"raw utterance {index} is not an object")
        start = row.get("start_time")
        end = row.get("end_time")
        transcript = row.get("transcript")
        if (
            type(start) is not int
            or type(end) is not int
            or start < 0
            or end <= start
            or not isinstance(transcript, str)
            or not transcript.strip()
            or len(transcript.encode("utf-8")) > MAX_TRANSCRIPT_BYTES
        ):
            _fail(f"raw utterance {index} is malformed")
        absolute_start = start + source_offset
        absolute_end = end + source_offset
        if absolute_start < source_context[0] or absolute_end > source_context[1]:
            _fail(f"raw utterance {index} falls outside source context")
        if row.get("words"):
            word_timings_present = True
        if absolute_end <= source_interval[0] or absolute_start >= source_interval[1]:
            continue
        if absolute_start < source_interval[0] or absolute_end > source_interval[1]:
            _fail(f"raw utterance {index} crosses the producer piece boundary")
        selected.append(dict(row))
    if not selected:
        _fail("raw BCUT response has no complete cues in source interval")

    shift_ms = source_offset - source_interval[0]
    shifted = {"utterances": selected}
    draft_srt = to_srt(shifted, offset_ms=shift_ms)
    cues = parse_srt_cues(draft_srt)
    padded = Path(padded).absolute()
    provenance = _validate_producer_piece(
        producer_spec=producer_spec,
        padded=padded,
        source_path=source_path,
        source_sha=source_sha,
        source_interval=source_interval,
    )
    expected_duration = source_interval[1] - source_interval[0]
    clock_rounding = False
    if padded_duration_ms is not None:
        if type(padded_duration_ms) is not int or padded_duration_ms <= 0:
            _fail("padded_duration_ms must be a positive integer")
        if expected_duration != padded_duration_ms:
            from src.autoslice.producer_text_pipeline import _source_piece_container_clock_matches

            clock_rounding = _source_piece_container_clock_matches(
                padded, expected_duration, padded_duration_ms,
                allow_audio_packet_rounding=True,
                source_path=source_path,
                source_interval_ms=source_interval,
                required_end_ms=max((cue.end_ms for cue in cues), default=0),
            )
            if not clock_rounding:
                _fail("source interval differs from actual padded stream coverage")
        expected_duration = padded_duration_ms
    if len(cues) != len(selected) or any(
        cue.start_ms < 0 or cue.end_ms > expected_duration or cue.end_ms <= cue.start_ms
        for cue in cues
    ):
        _fail("derived BCUT SRT has invalid padded-domain timing")

    receipt: dict[str, object] = {
        "schema_version": REUSE_RECEIPT_SCHEMA,
        "status": "REUSED",
        "candidate_id": observation.get("candidate_id"),
        "provider": "bcut",
        "authority": "ASR_DRAFT_OBSERVATION_NOT_FINAL_TEXT",
        "asr_calls": 0,
        "provider_calls": 0,
        "observed_source_context_ms": list(source_context),
        "observed_source_offset_ms": source_offset,
        "source_interval_ms": list(source_interval),
        "padded_local_interval_ms": [0, expected_duration],
        "derived_timestamp_shift_ms": shift_ms,
        "word_timing_shift_ms": shift_ms,
        "observed_raw_utterance_count": observed_count,
        "selected_cue_count": len(selected),
        "word_timings_present": word_timings_present,
        "bindings": bindings,
        "raw_response_path": str(raw_path),
        "raw_response_sha256": f"sha256:{raw_sha}",
        "request_receipt_path": str(request_path),
        "request_receipt_sha256": f"sha256:{request_sha}",
        "source_audio_path": str(source_audio_path),
        "source_audio_sha256": f"sha256:{source_audio_sha}",
        "source_media_path": str(source_path),
        "source_media_sha256": f"sha256:{source_sha}",
        "producer_provenance": provenance,
    }
    if clock_rounding:
        receipt["container_clock"] = {
            "validation": "source-aware-measured-stream-quantum",
            "nominal_padded_duration_ms": source_interval[1] - source_interval[0],
            "actual_padded_duration_ms": expected_duration,
            "required_cue_end_ms": max(cue.end_ms for cue in cues),
        }
    if request_receipt_compatibility is not None:
        receipt["request_receipt_compatibility"] = request_receipt_compatibility
    if request_clock_projection is not None:
        receipt["request_clock_projection"] = request_clock_projection
    receipt_path = padded.with_suffix(".bcut-draft-observation-reuse.json")
    receipt["receipt_path"] = str(receipt_path)
    _write_reuse_receipt(receipt_path, receipt)
    return BcutDraftObservation(srt_text=draft_srt, receipt=receipt)


__all__ = [
    "BcutDraftObservation",
    "BcutDraftObservationError",
    "SCHEMA_VERSION",
    "REUSE_RECEIPT_SCHEMA",
    "load_bcut_draft_observation",
]
