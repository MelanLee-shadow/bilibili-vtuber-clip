"""Media execution, provenance, and rollback transactions for the producer."""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import tempfile
from pathlib import Path
from typing import Callable

from .channel_profile import load_channel_profile
from .recut_materialization import _accurate_reencode_recut_command
from .shadow_review import _sha256
from .speaker_session_router import SpeakerRoutingError, segment_binding_sha256

ROOT = Path(__file__).resolve().parents[2]
CHANNEL_PROFILE = load_channel_profile(ROOT)


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

RECUT_PROVENANCE_SCHEMA = (
    f"{CHANNEL_PROFILE.profile_id}-speaker-recut-provenance.v1"
)

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

FAST_FRESH_DERIVATION_SCHEMA = (
    f"{CHANNEL_PROFILE.profile_id}-speaker-fast-fresh-derivation.v1"
)

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
    accurate_command_builder: Callable[..., list[str]] = _accurate_reencode_recut_command,
    run_command: Callable[..., None] = run,
    duration_probe: Callable[[Path], int] = ffprobe_duration_ms,
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
        command = accurate_command_builder(
            source_video=source,
            output_media=temporary_output,
            start_ms=final_source_start_ms,
            duration_ms=expected_duration_ms,
        )
        try:
            run_command(command, timeout=3600)
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
        actual_duration_ms = duration_probe(temporary_output)
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
