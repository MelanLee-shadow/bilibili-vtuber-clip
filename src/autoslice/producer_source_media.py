"""Hash-bound source-piece cutting and concatenation for the producer."""

from __future__ import annotations

import json
import os
import re
import shlex
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from src.autoslice.producer_media import (
    _source_media_sha256,
    _valid_cached_provenance,
    _write_json_atomic,
    ffprobe_duration_ms,
    run,
)
from src.autoslice.recut_materialization import _accurate_reencode_recut_command
from src.autoslice.redelivery_boundary_projection import (
    AUTHORITY_CONFIG_KEY,
    build_terminal_projection_authority,
)
from src.autoslice.reviewed_exact_source_interval import (
    RUNTIME_CONFIG_KEY as EXACT_INTERVAL_RUNTIME_CONFIG_KEY,
    ReviewedExactSourceIntervalError,
    compile_authority_from_spec as compile_exact_interval_authority,
)
from src.autoslice.shadow_review import _sha256
from src.autoslice.jingting_remote_runner import is_local_host


MULTI_PIECE_MATERIALIZATION = "independent-decoded-audio-copyts.v1"


def _padded_name(pieces: list[dict]) -> str:
    suffix = ".decoded-audio-v1" if len(pieces) > 1 else ""
    return f"padded_{pieces[0]['start_ms']}_{pieces[-1]['end_ms']}{suffix}.mp4"


def _concat_piece_command(
    piece_paths: list[Path], durations: list[int], concat_list: Path, output: Path,
) -> list[str]:
    # The concat demuxer loses each AAC packet's priming skip metadata. Decode
    # audio independently before joining it; keep video PTS rather than shifting
    # them by the demuxer's negative AAC start time.
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-copyts",
               "-f", "concat", "-safe", "0", "-i", str(concat_list)]
    filters = []
    for index, (path, duration) in enumerate(zip(piece_paths, durations, strict=True)):
        command.extend(["-i", str(path)])
        seconds = f"{duration / 1000:.3f}"
        filters.append(
            f"[{index + 1}:a:0]apad=whole_dur={seconds},atrim=duration={seconds},"
            f"asetpts=PTS-STARTPTS[a{index}]"
        )
    inputs = "".join(f"[a{index}]" for index in range(len(piece_paths)))
    filters.append(f"{inputs}concat=n={len(piece_paths)}:v=0:a=1[a]")
    return command + ["-filter_complex", ";".join(filters), "-map", "0:v:0", "-map", "[a]",
                      "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", str(output)]


def _bind_piece_source_media_sha256(
    piece: dict,
    *,
    source_sha256: str,
) -> str:
    """Bind a producer piece to the exact source bytes that were inspected.

    Candidate construction only knows a source path.  The producer is the first
    layer that hashes the bytes on the execution host, so it owns this binding.
    Downstream source-truth aliases must consume this verified value instead of
    trusting an optional caller declaration.
    """

    binding = f"sha256:{source_sha256}"
    declared = piece.get("source_media_sha256")
    if declared is not None and declared != binding:
        raise RuntimeError(
            f"SOURCE_MEDIA_DECLARED_SHA256_MISMATCH: declared={declared!r} actual={binding}"
        )
    piece["source_media_sha256"] = binding
    return binding


@dataclass(frozen=True)
class PreparedSourceMedia:
    durations: list[int]
    padded: Path
    padded_duration_ms: int
    padded_provenance_path: Path
    piece_provenance_rows: list[dict]


def source_piece_clock(
    rows: list[dict], durations: list[int], *, padded_sha256: str,
) -> dict:
    """Expose the normal byte-bound cuts as a piecewise recording clock.

    Keep measured concat offsets. Encoder/mux tail rounding does not extend
    the declared original recording window or create source content.
    """
    if len(rows) != len(durations) or not rows:
        raise RuntimeError("SOURCE_PIECE_CLOCK_COUNT_INVALID")
    clock = []
    offset = 0
    for index, (row, duration) in enumerate(zip(rows, durations, strict=True)):
        start, end = row.get("start_ms"), row.get("end_ms")
        if any(type(value) is not int for value in (start, end, duration)) or not 0 <= start < end or duration <= 0:
            raise RuntimeError("SOURCE_PIECE_CLOCK_INTERVAL_INVALID")
        source_sha = _retained_hash(row.get("source_sha256"), "SOURCE_PIECE_CLOCK")
        if row.get("source_media_binding") != "sha256:" + source_sha:
            raise RuntimeError("SOURCE_PIECE_CLOCK_SOURCE_BINDING_INVALID")
        clock.append({
            "piece_index": index, "recording_basename": Path(row["source_path"]).name,
            "recording_sha256": "sha256:" + source_sha,
            "recording_start_ms": start, "recording_end_ms": end,
            "padded_start_ms": offset, "padded_end_ms": offset + duration,
            "mapped_padded_end_ms": offset + min(duration, end - start),
            "piece_sha256": "sha256:" + _retained_hash(row.get("output_sha256"), "SOURCE_PIECE_CLOCK_OUTPUT"),
        })
        offset += duration
    return {"time_domain": "padded_local", "root_time_domain": "recording_absolute_ms",
            "padded_sha256": "sha256:" + _retained_hash(padded_sha256, "SOURCE_PIECE_CLOCK_PADDED"),
            "pieces": clock}


def _source_root_is_unavailable(error: BaseException) -> bool:
    text = str(error)
    return (
        "SOURCE_RECORDING_ROOT_UNAVAILABLE:" in text
        or "Transport endpoint is not connected" in text
        or "State not recoverable" in text
    )


def _load_hash_bound_cached_piece(
    *,
    piece: dict,
    local: Path,
    provenance_path: Path,
    host: str,
) -> dict | None:
    """Return an exact cached cut when only the source root is unavailable."""

    if host not in {"localhost", "127.0.0.1", "::1"}:
        return None
    try:
        document = json.loads(provenance_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(document, dict):
        return None

    source_path = str(piece["remote_media"])
    source_sha256 = document.get("source_sha256")
    if (
        document.get("source_path") != source_path
        or not isinstance(source_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", source_sha256) is None
        or document.get("source_media_binding") != f"sha256:{source_sha256}"
    ):
        return None
    expected_piece = {
        "source_path": source_path,
        "source_sha256": source_sha256,
        "source_media_binding": f"sha256:{source_sha256}",
        "start_ms": int(piece["start_ms"]),
        "end_ms": int(piece["end_ms"]),
        "output_path": str(local.resolve()),
    }
    if not _valid_cached_provenance(
        provenance_path,
        expected_without_output_hash=expected_piece,
        output=local,
    ):
        return None
    return document


def _retained_hash(value: object, label: str) -> str:
    """Return a strict bare SHA-256 from a retained-material declaration."""

    if not isinstance(value, str):
        raise RuntimeError(f"{label}_HASH_INVALID")
    match = re.fullmatch(r"(?:sha256:)?([0-9a-fA-F]{64})", value)
    if match is None:
        raise RuntimeError(f"{label}_HASH_INVALID")
    return match.group(1).lower()


def _retained_interval(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RuntimeError(f"{label}_INVALID")
    return value


def _retained_regular_file(value: object, label: str) -> Path:
    """Resolve an existing regular file without accepting a symlink."""

    if not isinstance(value, str) or not value:
        raise RuntimeError(f"{label}_PATH_INVALID")
    path = Path(value)
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"{label}_MISSING")
    return path


def _retained_json(path: Path, label: str) -> tuple[bytes, dict]:
    raw = path.read_bytes()
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{label}_INVALID") from exc
    if not isinstance(document, dict):
        raise RuntimeError(f"{label}_INVALID")
    return raw, document


def _copy_retained_sidecar(raw: bytes, destination: Path, label: str) -> None:
    """Create a new output sidecar while preserving its original bytes."""

    if destination.exists() or destination.is_symlink():
        if destination.is_symlink() or not destination.is_file():
            raise RuntimeError(f"{label}_DESTINATION_INVALID")
        if destination.read_bytes() != raw:
            raise RuntimeError(f"{label}_DESTINATION_DRIFT")
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with destination.open("xb") as handle:
            handle.write(raw)
    except FileExistsError:
        if destination.is_symlink() or not destination.is_file() or destination.read_bytes() != raw:
            raise RuntimeError(f"{label}_DESTINATION_DRIFT")


def _prepare_retained_source_media(
    *,
    spec: dict,
    cid: str,
    out_root: Path,
    host: str,
    spec_parent: Path,
) -> PreparedSourceMedia | None:
    """Reuse a validated retained materialization through the ordinary consumer.

    The retained declaration is an input to the normal source-media consumer, not
    an authority that bypasses its cache/provenance checks.  All validation is
    completed before anything is created below ``out_root``.
    """

    retained = spec.get("retained_current_text")
    source_input = spec.get("retained_source_input")
    if retained is not None and source_input is not None:
        raise RuntimeError("RETAINED_SOURCE_INPUT_CONFLICT")
    if source_input is not None:
        if (not isinstance(source_input, dict)
                or source_input.get("schema_version") not in {"retained-source-input.v1", "retained-source-checkpoint-input.v1"}
                or source_input.get("candidate_id") != cid):
            raise RuntimeError("RETAINED_SOURCE_INPUT_INVALID")
        retained = source_input
    if retained is None:
        return None
    if not isinstance(retained, dict):
        raise RuntimeError("RETAINED_CURRENT_TEXT_INVALID")
    if not is_local_host(host):
        raise RuntimeError("RETAINED_SOURCE_CACHE_REQUIRES_LOCAL_HOST")

    padded_sha = _retained_hash(retained.get("padded_sha256"), "RETAINED_PADDED")
    parent_sha = _retained_hash(
        retained.get("parent_materialization_sha256"),
        "RETAINED_PARENT_MATERIALIZATION",
    )
    if padded_sha != parent_sha:
        raise RuntimeError("RETAINED_PADDED_HASH_MISMATCH")

    padded = _retained_regular_file(
        retained.get("parent_materialization_path"),
        "RETAINED_PARENT_MATERIALIZATION",
    )
    padded_provenance = _retained_regular_file(
        retained.get("parent_materialization_provenance_path"),
        "RETAINED_PARENT_PROVENANCE",
    )
    actual_padded_sha = _sha256(padded)
    if actual_padded_sha != padded_sha:
        raise RuntimeError("RETAINED_PARENT_MATERIALIZATION_HASH_MISMATCH")
    _provenance_raw, padded_document = _retained_json(
        padded_provenance,
        "RETAINED_PARENT_PROVENANCE",
    )
    declared_provenance_sha = _retained_hash(
        retained.get("parent_materialization_provenance_sha256"),
        "RETAINED_PARENT_PROVENANCE",
    )
    if _sha256(padded_provenance) != declared_provenance_sha:
        raise RuntimeError("RETAINED_PARENT_PROVENANCE_HASH_MISMATCH")
    if padded_provenance.parent.resolve() != padded.parent.resolve():
        raise RuntimeError("RETAINED_PARENT_PROVENANCE_PARENT_MISMATCH")
    pieces = spec.get("pieces")
    if not isinstance(pieces, list) or not pieces:
        raise RuntimeError("RETAINED_PARENT_PROVENANCE_INPUTS_INVALID")
    if not all(isinstance(piece, dict) for piece in pieces):
        raise RuntimeError("RETAINED_PIECE_SPEC_INVALID")
    for piece in pieces:
        try:
            _retained_interval(piece["start_ms"], "RETAINED_PIECE_START_MS")
            _retained_interval(piece["end_ms"], "RETAINED_PIECE_END_MS")
        except KeyError as exc:
            raise RuntimeError("RETAINED_PIECE_INTERVAL_INVALID") from exc
    if len(pieces) > 1 and padded_document.get("materialization") != MULTI_PIECE_MATERIALIZATION:
        raise RuntimeError("RETAINED_MULTI_PIECE_CLOCK_MATERIALIZATION_UNVERIFIED")
    if padded.name != _padded_name(pieces):
        raise RuntimeError("RETAINED_PARENT_MATERIALIZATION_NAME_MISMATCH")
    if padded_document.get("output_path") != str(padded.resolve()):
        raise RuntimeError("RETAINED_PARENT_PROVENANCE_OUTPUT_MISMATCH")
    if _retained_hash(padded_document.get("output_sha256"), "RETAINED_PARENT_PROVENANCE_OUTPUT") != padded_sha:
        raise RuntimeError("RETAINED_PARENT_PROVENANCE_OUTPUT_HASH_MISMATCH")

    parent_inputs = padded_document.get("inputs")
    if not isinstance(parent_inputs, list):
        raise RuntimeError("RETAINED_PARENT_PROVENANCE_INPUTS_INVALID")
    if len(parent_inputs) != len(pieces):
        raise RuntimeError("RETAINED_PARENT_PROVENANCE_INPUTS_MISMATCH")
    declared_source_shas = retained.get("source_media_sha256s")
    if declared_source_shas is not None:
        if not isinstance(declared_source_shas, list) or len(declared_source_shas) != len(pieces):
            raise RuntimeError("RETAINED_SOURCE_SHA256S_INVALID")

    sidecars: list[tuple[bytes, dict, Path, Path, str]] = []
    for index, piece in enumerate(pieces):
        if not isinstance(piece, dict):
            raise RuntimeError("RETAINED_PIECE_SPEC_INVALID")
        try:
            start_ms = _retained_interval(piece["start_ms"], "RETAINED_PIECE_START_MS")
            end_ms = _retained_interval(piece["end_ms"], "RETAINED_PIECE_END_MS")
        except KeyError as exc:
            raise RuntimeError("RETAINED_PIECE_INTERVAL_INVALID") from exc
        if end_ms <= start_ms:
            raise RuntimeError("RETAINED_PIECE_INTERVAL_INVALID")
        remote_media = piece.get("remote_media")
        if not isinstance(remote_media, str) or not remote_media:
            raise RuntimeError("RETAINED_PIECE_SOURCE_INVALID")
        source_path = Path(remote_media)
        expected_source_path = str(source_path)
        sidecar_path = padded.parent / f"piece_{index}_{start_ms}_{end_ms}.provenance.json"
        sidecar_path = _retained_regular_file(
            str(sidecar_path),
            "RETAINED_PIECE_PROVENANCE",
        )
        sidecar_raw, sidecar = _retained_json(sidecar_path, "RETAINED_PIECE_PROVENANCE")
        if sidecar.get("source_path") != expected_source_path:
            raise RuntimeError("RETAINED_PIECE_SOURCE_PATH_MISMATCH")
        source_sha = _retained_hash(sidecar.get("source_sha256"), "RETAINED_PIECE_SOURCE")
        if _retained_hash(sidecar.get("source_media_binding"), "RETAINED_PIECE_SOURCE_BINDING") != source_sha:
            raise RuntimeError("RETAINED_PIECE_SOURCE_BINDING_MISMATCH")
        if declared_source_shas is not None and _retained_hash(
            declared_source_shas[index],
            "RETAINED_SOURCE_SHA256S",
        ) != source_sha:
            raise RuntimeError("RETAINED_SOURCE_SHA256S_MISMATCH")
        declared_piece_sha = piece.get("source_media_sha256")
        if declared_piece_sha is not None and _retained_hash(declared_piece_sha, "RETAINED_PIECE_SPEC_SOURCE") != source_sha:
            raise RuntimeError("RETAINED_PIECE_SOURCE_SHA256_MISMATCH")
        actual_source_path, actual_source_sha = _source_media_sha256(host, source_path)
        if actual_source_path != expected_source_path:
            raise RuntimeError("RETAINED_PIECE_SOURCE_PATH_MISMATCH")
        if actual_source_sha != source_sha:
            raise RuntimeError("RETAINED_PIECE_SOURCE_SHA256_MISMATCH")
        if sidecar.get("start_ms") != start_ms or sidecar.get("end_ms") != end_ms:
            raise RuntimeError("RETAINED_PIECE_INTERVAL_MISMATCH")
        output_path = _retained_regular_file(sidecar.get("output_path"), "RETAINED_PIECE_OUTPUT")
        output_sha = _retained_hash(sidecar.get("output_sha256"), "RETAINED_PIECE_OUTPUT")
        if output_path.parent.resolve() != padded.parent.resolve():
            raise RuntimeError("RETAINED_PIECE_OUTPUT_PARENT_MISMATCH")
        if output_path.name != f"piece_{index}_{start_ms}_{end_ms}.mp4":
            raise RuntimeError("RETAINED_PIECE_OUTPUT_NAME_MISMATCH")
        if _sha256(output_path) != output_sha:
            raise RuntimeError("RETAINED_PIECE_OUTPUT_HASH_MISMATCH")
        expected_piece = {
            "source_path": expected_source_path,
            "source_sha256": source_sha,
            "source_media_binding": f"sha256:{source_sha}",
            "start_ms": start_ms,
            "end_ms": end_ms,
            "output_path": str(output_path.resolve()),
        }
        if not _valid_cached_provenance(
            sidecar_path,
            expected_without_output_hash=expected_piece,
            output=output_path,
        ):
            raise RuntimeError("RETAINED_PIECE_PROVENANCE_INVALID")
        parent_input = parent_inputs[index]
        if not isinstance(parent_input, dict):
            raise RuntimeError("RETAINED_PARENT_PROVENANCE_INPUT_INVALID")
        if parent_input.get("path") != str(output_path.resolve()):
            raise RuntimeError("RETAINED_PARENT_PROVENANCE_INPUT_PATH_MISMATCH")
        if _retained_hash(parent_input.get("sha256"), "RETAINED_PARENT_PROVENANCE_INPUT") != output_sha:
            raise RuntimeError("RETAINED_PARENT_PROVENANCE_INPUT_HASH_MISMATCH")
        piece["source_media_sha256"] = f"sha256:{source_sha}"
        sidecars.append((sidecar_raw, sidecar, sidecar_path, output_path, output_sha))

    expected_padded = {
        "inputs": [
            {"path": str(output_path.resolve()), "sha256": output_sha}
            for _, _, _, output_path, output_sha in sidecars
        ],
        "output_path": str(padded.resolve()),
    }
    if len(pieces) > 1:
        expected_padded.update(
            materialization=MULTI_PIECE_MATERIALIZATION,
            piece_durations_ms=[ffprobe_duration_ms(row[3]) for row in sidecars],
        )
    if not _valid_cached_provenance(
        padded_provenance,
        expected_without_output_hash=expected_padded,
        output=padded,
    ):
        raise RuntimeError("RETAINED_PARENT_PROVENANCE_INVALID")

    # The normal consumer must see the same pieces and cache parent, but must not
    # recurse into this retained branch.  It remains responsible for returning
    # the actual PreparedSourceMedia object and its usual projection checks.
    normal_spec = dict(spec)
    normal_spec.pop("retained_current_text", None)
    normal_spec.pop("retained_source_input", None)
    normal_spec["pieces"] = [dict(piece) for piece in pieces]
    if isinstance(spec.get("subtitle_redelivery_baseline"), dict):
        normal_spec["subtitle_redelivery_baseline"] = dict(
            spec["subtitle_redelivery_baseline"]
        )
    prepared = prepare_source_media(
        spec=normal_spec,
        cid=cid,
        out_root=padded.parent,
        host=host,
        spec_parent=spec_parent,
    )
    if prepared.padded.resolve() != padded.resolve():
        raise RuntimeError("RETAINED_NORMAL_CONSUMER_PADDED_PATH_MISMATCH")
    if _sha256(prepared.padded) != padded_sha:
        raise RuntimeError("RETAINED_NORMAL_CONSUMER_PADDED_HASH_MISMATCH")
    if prepared.padded_provenance_path.resolve() != padded_provenance.resolve():
        raise RuntimeError("RETAINED_NORMAL_CONSUMER_PROVENANCE_PATH_MISMATCH")
    declared_duration = retained.get("parent_materialization_duration_ms")
    if declared_duration is not None:
        try:
            declared_duration = int(declared_duration)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("RETAINED_PARENT_DURATION_INVALID") from exc
        if prepared.padded_duration_ms != declared_duration:
            # Source intervals retain their logical millisecond clock.  A
            # hash-bound mux can finish within one measured video frame of
            # that clock; use the same stream-aware check as normal resume
            # instead of rewriting the declared interval or measured duration.
            from src.autoslice.producer_text_pipeline import (
                _source_piece_container_clock_matches,
            )

            if not _source_piece_container_clock_matches(
                prepared.padded, declared_duration, prepared.padded_duration_ms
            ):
                raise RuntimeError("RETAINED_PARENT_DURATION_MISMATCH")
    if len(prepared.piece_provenance_rows) != len(sidecars):
        raise RuntimeError("RETAINED_NORMAL_CONSUMER_PIECES_MISMATCH")
    for row, (_, _, _, output_path, output_sha) in zip(prepared.piece_provenance_rows, sidecars):
        if row.get("output_path") != str(output_path.resolve()):
            raise RuntimeError("RETAINED_NORMAL_CONSUMER_PIECE_PATH_MISMATCH")
        if _retained_hash(row.get("output_sha256"), "RETAINED_NORMAL_CONSUMER_PIECE") != output_sha:
            raise RuntimeError("RETAINED_NORMAL_CONSUMER_PIECE_HASH_MISMATCH")

    # ``prepare_source_media`` normally mutates the caller's spec with the
    # exact-interval or terminal-projection runtime authority.  The temporary
    # recursion-safe copy must not hide that ordinary result from downstream
    # text processing.
    if EXACT_INTERVAL_RUNTIME_CONFIG_KEY in normal_spec:
        spec[EXACT_INTERVAL_RUNTIME_CONFIG_KEY] = normal_spec[
            EXACT_INTERVAL_RUNTIME_CONFIG_KEY
        ]
    if isinstance(normal_spec.get("subtitle_redelivery_baseline"), dict):
        spec["subtitle_redelivery_baseline"] = normal_spec[
            "subtitle_redelivery_baseline"
        ]
    spec["source_piece_clock"] = normal_spec["source_piece_clock"]

    for index, (raw, _, _, _, _) in enumerate(sidecars):
        start_ms = int(pieces[index]["start_ms"])
        end_ms = int(pieces[index]["end_ms"])
        _copy_retained_sidecar(
            raw,
            out_root / f"piece_{index}_{start_ms}_{end_ms}.provenance.json",
            "RETAINED_OUTPUT_PIECE_PROVENANCE",
        )
    return prepared


def _new_local_recut_temp(output: Path) -> tuple[Path, tuple[int, int]]:
    """Reserve a private same-directory temp for one native local recut."""

    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.produce-",
        suffix=".mp4",
        dir=str(output.parent),
    )
    os.fchmod(fd, 0o600)
    metadata = os.fstat(fd)
    os.close(fd)
    return Path(temporary_name), (metadata.st_dev, metadata.st_ino)


def _cleanup_local_recut_temp(
    temporary: Path,
    identity: tuple[int, int],
    *,
    expected_sha256: str | None = None,
) -> None:
    """Delete only an unchanged temp owned by this recut invocation."""

    try:
        metadata = temporary.lstat()
    except OSError:
        return
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or (metadata.st_dev, metadata.st_ino) != identity
    ):
        return
    if expected_sha256 is not None:
        try:
            if _sha256(temporary) != expected_sha256:
                return
        except OSError:
            return
    try:
        temporary.unlink()
    except OSError:
        return


def _run_local_piece_recut(
    *,
    host: str,
    source_path: Path,
    expected_source_path: str,
    source_sha256: str,
    output: Path,
    start_ms: int,
    duration_ms: int,
) -> str:
    """Re-encode one piece locally, publishing only after source/hash checks."""

    temporary, identity = _new_local_recut_temp(output)
    temporary_sha256: str | None = None
    try:
        command = _accurate_reencode_recut_command(
            source_video=source_path,
            output_media=temporary,
            start_ms=start_ms,
            duration_ms=duration_ms,
        )
        completed = run(command, timeout=3600)
        if hasattr(completed, "returncode") and completed.returncode != 0:
            raise RuntimeError(
                f"{command[0]} failed rc={completed.returncode}: "
                f"{getattr(completed, 'stderr', '')[-400:]}"
            )
        try:
            metadata = temporary.lstat()
        except OSError as exc:
            raise RuntimeError("SOURCE_MEDIA_RECUT_OUTPUT_MISSING") from exc
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size <= 0
            or (metadata.st_dev, metadata.st_ino) != identity
        ):
            raise RuntimeError("SOURCE_MEDIA_RECUT_OUTPUT_INVALID")
        temporary_sha256 = _sha256(temporary)
        post_source_path, post_sha256 = _source_media_sha256(host, source_path)
        if (post_source_path, post_sha256) != (expected_source_path, source_sha256):
            raise RuntimeError("SOURCE_MEDIA_DRIFT_DURING_PIECE_RECUT")
        os.replace(temporary, output)
        try:
            _local_output_metadata = output.lstat()
        except OSError as exc:
            raise RuntimeError("SOURCE_MEDIA_RECUT_OUTPUT_MISSING") from exc
        if (
            stat.S_ISLNK(_local_output_metadata.st_mode)
            or not stat.S_ISREG(_local_output_metadata.st_mode)
            or _sha256(output) != temporary_sha256
        ):
            raise RuntimeError("SOURCE_MEDIA_RECUT_OUTPUT_HASH_MISMATCH")
        return temporary_sha256
    finally:
        _cleanup_local_recut_temp(
            temporary,
            identity,
            expected_sha256=temporary_sha256,
        )


def prepare_source_media(
    *,
    spec: dict,
    cid: str,
    out_root: Path,
    host: str,
    spec_parent: Path | None = None,
) -> PreparedSourceMedia:
    retained_prepared = _prepare_retained_source_media(
        spec=spec,
        cid=cid,
        out_root=out_root,
        host=host,
        spec_parent=spec_parent or Path.cwd(),
    )
    if retained_prepared is not None:
        if spec.get("source_witness_extension") is not None:
            from src.autoslice.source_checkpoint_parent import validate_source_checkpoint_parent
            from src.autoslice.source_witness_extension import prepare_extension
            validate_source_checkpoint_parent(spec, runtime=True)
            prepare_extension(spec, host=host, spec_parent=spec_parent or Path.cwd())
        spec["source_piece_clock"] = source_piece_clock(
            retained_prepared.piece_provenance_rows, retained_prepared.durations,
            padded_sha256=_sha256(retained_prepared.padded),
        )
        return retained_prepared
    piece_paths: list[Path] = []
    piece_provenance_rows: list[dict] = []
    for index, piece in enumerate(spec["pieces"]):
        local = out_root / f"piece_{index}_{piece['start_ms']}_{piece['end_ms']}.mp4"
        piece_provenance_path = local.with_suffix(".provenance.json")
        cached_piece = _load_hash_bound_cached_piece(
            piece=piece,
            local=local,
            provenance_path=piece_provenance_path,
            host=host,
        )
        if cached_piece is not None:
            try:
                cached_source_present = Path(piece["remote_media"]).is_file()
            except OSError:
                cached_source_present = False
            if cached_source_present:
                source_sha256 = str(cached_piece["source_sha256"])
                _bind_piece_source_media_sha256(
                    piece,
                    source_sha256=source_sha256,
                )
                piece_paths.append(local)
                piece_provenance_rows.append(
                    {
                        **cached_piece,
                        "source_revalidation_status": (
                            "HASH_BOUND_CACHE_REUSED_SOURCE_PATH_PRESENT"
                        ),
                    }
                )
                continue
        try:
            source_path, source_sha256 = _source_media_sha256(host, Path(piece["remote_media"]))
        except (OSError, RuntimeError) as exc:
            cache_allowed = False
            if _source_root_is_unavailable(exc):
                if cached_piece is not None:
                    _bind_piece_source_media_sha256(
                        piece,
                        source_sha256=str(cached_piece["source_sha256"]),
                    )
                    cache_allowed = True
            if not cache_allowed:
                raise
            piece_paths.append(local)
            piece_provenance_rows.append(
                {
                    **cached_piece,
                    "source_revalidation_status": ("HASH_BOUND_CACHE_SOURCE_ROOT_UNAVAILABLE"),
                }
            )
            continue
        source_media_binding = _bind_piece_source_media_sha256(
            piece,
            source_sha256=source_sha256,
        )
        expected_piece = {
            "source_path": source_path,
            "source_sha256": source_sha256,
            "source_media_binding": source_media_binding,
            "start_ms": int(piece["start_ms"]),
            "end_ms": int(piece["end_ms"]),
            "output_path": str(local.resolve()),
        }
        if not _valid_cached_provenance(
            piece_provenance_path,
            expected_without_output_hash=expected_piece,
            output=local,
        ):
            if is_local_host(host):
                _run_local_piece_recut(
                    host=host,
                    source_path=Path(piece["remote_media"]),
                    expected_source_path=source_path,
                    source_sha256=source_sha256,
                    output=local,
                    start_ms=piece["start_ms"],
                    duration_ms=piece["end_ms"] - piece["start_ms"],
                )
            else:
                local.unlink(missing_ok=True)
                piece_provenance_path.unlink(missing_ok=True)
                remote_tmp = f"/tmp/produce_{cid}_{index}.mp4"
                cmd = _accurate_reencode_recut_command(
                    source_video=Path(piece["remote_media"]),
                    output_media=Path(remote_tmp),
                    start_ms=piece["start_ms"],
                    duration_ms=piece["end_ms"] - piece["start_ms"],
                )
                run(
                    [
                        "ssh",
                        host,
                        " ".join(shlex.quote(str(part)) for part in cmd),
                    ],
                    timeout=3600,
                )
                run(
                    ["scp", "-q", f"{host}:{remote_tmp}", str(local)],
                    timeout=1800,
                )
                run(
                    ["ssh", host, f"rm -f {shlex.quote(remote_tmp)}"],
                    timeout=60,
                )
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
        piece_provenance_rows.append(json.loads(piece_provenance_path.read_text(encoding="utf-8")))
    durations = [ffprobe_duration_ms(p) for p in piece_paths]

    padded = out_root / _padded_name(spec["pieces"])
    padded_provenance_path = padded.with_suffix(".provenance.json")
    expected_padded = {
        "inputs": [{"path": str(path.resolve()), "sha256": _sha256(path)} for path in piece_paths],
        "output_path": str(padded.resolve()),
    }
    if len(piece_paths) > 1:
        expected_padded.update(
            materialization=MULTI_PIECE_MATERIALIZATION,
            piece_durations_ms=durations,
        )
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
        concat_list.write_text(
            "".join(f"file '{p.resolve()}'\nduration {duration / 1000:.3f}\n"
                    for p, duration in zip(piece_paths, durations, strict=True)),
            encoding="utf-8",
        )
        run(_concat_piece_command(piece_paths, durations, concat_list, padded))
    if not padded_cache_valid:
        _write_json_atomic(
            padded_provenance_path,
            {**expected_padded, "output_sha256": _sha256(padded)},
        )
    padded_dur = ffprobe_duration_ms(padded)
    try:
        exact_interval_authority = compile_exact_interval_authority(
            spec=spec,
            piece_provenance_rows=piece_provenance_rows,
        )
    except ReviewedExactSourceIntervalError as exc:
        # A declared exact authority is exclusive.  Never let a malformed or
        # stale grant fall through to the fresh-grid terminal projection.
        raise RuntimeError(str(exc)) from exc
    if exact_interval_authority is not None:
        spec[EXACT_INTERVAL_RUNTIME_CONFIG_KEY] = exact_interval_authority
        baseline = spec.get("subtitle_redelivery_baseline")
        if isinstance(baseline, dict):
            baseline.pop(AUTHORITY_CONFIG_KEY, None)
    else:
        terminal_projection = build_terminal_projection_authority(
            spec=spec,
            piece_provenance_rows=piece_provenance_rows,
            spec_parent=spec_parent or Path.cwd(),
        )
        if terminal_projection is not None:
            spec["subtitle_redelivery_baseline"][AUTHORITY_CONFIG_KEY] = terminal_projection
    spec["source_piece_clock"] = source_piece_clock(
        piece_provenance_rows, durations, padded_sha256=_sha256(padded),
    )
    return PreparedSourceMedia(
        durations=durations,
        padded=padded,
        padded_duration_ms=padded_dur,
        padded_provenance_path=padded_provenance_path,
        piece_provenance_rows=piece_provenance_rows,
    )
