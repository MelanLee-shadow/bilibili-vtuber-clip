"""Fail-closed recovery of finalized legacy HLS recordings into runner MP4s."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any, Sequence


class LegacyHlsRecoveryError(RuntimeError):
    """A finalized legacy recording could not be safely recovered."""


def _run(command: Sequence[str], *, timeout: int) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            list(command),
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise LegacyHlsRecoveryError(
            f"LEGACY_HLS_RECOVERY_COMMAND_FAILED:{type(exc).__name__}"
        ) from exc


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _playlist_media_name(playlist: Path, raw_media: Path) -> None:
    try:
        text = playlist.read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeError) as exc:
        raise LegacyHlsRecoveryError(
            "LEGACY_HLS_PLAYLIST_UNREADABLE"
        ) from exc
    if "#EXT-X-ENDLIST" not in text:
        raise LegacyHlsRecoveryError("LEGACY_HLS_PLAYLIST_NOT_FINALIZED")
    media_uris = {
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.startswith("#")
    }
    media_uris.update(
        match.group(1)
        for match in re.finditer(r'URI="([^"]+)"', text)
    )
    if media_uris != {raw_media.name}:
        raise LegacyHlsRecoveryError(
            "LEGACY_HLS_PLAYLIST_EXTERNAL_MEDIA_FORBIDDEN"
        )


def _probe(path: Path, *, ffprobe_bin: str) -> dict[str, Any]:
    completed = _run(
        [
            ffprobe_bin,
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=codec_type,codec_name,width,height",
            "-of",
            "json",
            str(path),
        ],
        timeout=120,
    )
    try:
        payload = json.loads(completed.stdout)
        streams = payload.get("streams") or []
        duration = float((payload.get("format") or {}).get("duration") or 0)
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise LegacyHlsRecoveryError("LEGACY_HLS_PROBE_INVALID") from exc
    video = next(
        (row for row in streams if row.get("codec_type") == "video"),
        None,
    )
    audio = next(
        (row for row in streams if row.get("codec_type") == "audio"),
        None,
    )
    if not isinstance(video, dict) or not isinstance(audio, dict) or duration <= 0:
        raise LegacyHlsRecoveryError("LEGACY_HLS_MEDIA_STREAMS_INVALID")
    return {
        "duration_seconds": duration,
        "video_codec": video.get("codec_name"),
        "audio_codec": audio.get("codec_name"),
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
    }


def _publish_noreplace(staged: Path, target: Path) -> str:
    try:
        os.link(staged, target)
        staged.unlink()
        return "hardlink_noreplace"
    except FileExistsError as exc:
        raise LegacyHlsRecoveryError(
            "LEGACY_HLS_TARGET_CONFLICT"
        ) from exc
    except OSError as exc:
        if exc.errno not in {
            errno.EPERM,
            errno.EACCES,
            errno.EOPNOTSUPP,
            errno.ENOTSUP,
            errno.EXDEV,
            errno.EINVAL,
        }:
            raise LegacyHlsRecoveryError(
                "LEGACY_HLS_ATOMIC_PUBLISH_FAILED"
            ) from exc
    reservation = target.with_name(f".{target.name}.publish-reservation")
    try:
        reservation.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise LegacyHlsRecoveryError(
            "LEGACY_HLS_PUBLISH_RESERVATION_CONFLICT"
        ) from exc
    try:
        if target.exists():
            raise LegacyHlsRecoveryError("LEGACY_HLS_TARGET_CONFLICT")
        os.rename(staged, target)
        return "reserved_atomic_rename"
    finally:
        try:
            reservation.rmdir()
        except FileNotFoundError:
            pass


def _write_receipt(path: Path, payload: dict[str, Any]) -> None:
    encoded = (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    if path.exists():
        if path.read_bytes() != encoded:
            raise LegacyHlsRecoveryError("LEGACY_HLS_RECEIPT_CONFLICT")
        return
    fd, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        _publish_noreplace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def recover_finalized_legacy_hls(
    date_dir: Path,
    *,
    room_id: str,
    ffmpeg_bin: str = "ffmpeg",
    ffprobe_bin: str = "ffprobe",
) -> list[dict[str, Any]]:
    """Recover every finalized same-stem m3u8/m4s pair with no MP4 sibling."""

    receipts: list[dict[str, Any]] = []
    for playlist in sorted(date_dir.glob(f"{room_id}_*.m3u8")):
        raw_media = playlist.with_suffix(".m4s")
        target = playlist.with_suffix(".mp4")
        if target.exists() or not raw_media.is_file():
            continue
        _playlist_media_name(playlist, raw_media)
        staging_dir = date_dir / ".autoslice-hls-staging"
        staging_dir.mkdir(mode=0o700, exist_ok=True)
        staged = staging_dir / f"{playlist.stem}.{uuid.uuid4().hex}.mp4"
        try:
            _run(
                [
                    ffmpeg_bin,
                    "-nostdin",
                    "-hide_banner",
                    "-n",
                    "-loglevel",
                    "warning",
                    "-fflags",
                    "+genpts",
                    "-i",
                    str(playlist),
                    "-map",
                    "0:v:0",
                    "-map",
                    "0:a:0",
                    "-c",
                    "copy",
                    "-avoid_negative_ts",
                    "make_zero",
                    "-movflags",
                    "+faststart",
                    "-f",
                    "mp4",
                    str(staged),
                ],
                timeout=6 * 60 * 60,
            )
            media = _probe(staged, ffprobe_bin=ffprobe_bin)
            _run(
                [
                    ffmpeg_bin,
                    "-nostdin",
                    "-v",
                    "error",
                    "-xerror",
                    "-i",
                    str(staged),
                    "-map",
                    "0:v:0",
                    "-map",
                    "0:a:0",
                    "-c",
                    "copy",
                    "-f",
                    "null",
                    "-",
                ],
                timeout=6 * 60 * 60,
            )
            target_sha256 = _sha256(staged)
            publish_method = _publish_noreplace(staged, target)
            receipt = {
                "schema_version": "legacy-hls-recovery.v1",
                "status": "RECOVERED",
                "playlist": str(playlist),
                "playlist_size": playlist.stat().st_size,
                "raw_media": str(raw_media),
                "raw_media_size": raw_media.stat().st_size,
                "target": str(target),
                "target_sha256": target_sha256,
                "publish_method": publish_method,
                "media": media,
            }
            _write_receipt(
                target.with_suffix(".legacy-hls-recovery.json"),
                receipt,
            )
            receipts.append(receipt)
        finally:
            staged.unlink(missing_ok=True)
    return receipts
