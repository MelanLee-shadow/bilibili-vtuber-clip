"""Hash-bound source-piece cutting and concatenation for the producer."""

from __future__ import annotations

import json
import re
import shlex
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
from src.autoslice.shadow_review import _sha256


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
            "SOURCE_MEDIA_DECLARED_SHA256_MISMATCH: "
            f"declared={declared!r} actual={binding}"
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
    _bind_piece_source_media_sha256(piece, source_sha256=source_sha256)
    return {
        **document,
        "source_revalidation_status": "HASH_BOUND_CACHE_SOURCE_ROOT_UNAVAILABLE",
    }


def prepare_source_media(
    *,
    spec: dict,
    cid: str,
    out_root: Path,
    host: str,
) -> PreparedSourceMedia:
    piece_paths: list[Path] = []
    piece_provenance_rows: list[dict] = []
    for index, piece in enumerate(spec["pieces"]):
        local = out_root / f"piece_{index}_{piece['start_ms']}_{piece['end_ms']}.mp4"
        piece_provenance_path = local.with_suffix(".provenance.json")
        cached_piece: dict | None = None
        try:
            source_path, source_sha256 = _source_media_sha256(
                host, Path(piece["remote_media"])
            )
        except (OSError, RuntimeError) as exc:
            if _source_root_is_unavailable(exc):
                cached_piece = _load_hash_bound_cached_piece(
                    piece=piece,
                    local=local,
                    provenance_path=piece_provenance_path,
                    host=host,
                )
            if cached_piece is None:
                raise
            piece_paths.append(local)
            piece_provenance_rows.append(cached_piece)
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
    return PreparedSourceMedia(
        durations=durations,
        padded=padded,
        padded_duration_ms=padded_dur,
        padded_provenance_path=padded_provenance_path,
        piece_provenance_rows=piece_provenance_rows,
    )
