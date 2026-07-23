#!/usr/bin/env python3
"""Bridge official BililiveRecorder output into the autoslice source contract.

The recorder owns live capture and writes closed FLV/XML pairs.  This adapter
owns only the boundary to the existing autoslice system:

* publish a small, recorder-neutral, freshness-bound status document;
* remux closed FLV files to MP4 without deleting or overwriting source media;
* reconstruct Bilibili event JSONL from BililiveRecorder XML ``raw`` fields;
* publish MP4 only after validation, using a same-directory atomic rename.

It deliberately refuses to finalize anything while the room is streaming or
recording, and refuses to guess when the recorder API is unavailable.
"""

from __future__ import annotations

import argparse
import base64
import errno
import fcntl
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import threading
import time
from typing import Any, Iterable
import urllib.error
import urllib.request
import uuid
import xml.etree.ElementTree as ET


SCHEMA_VERSION = "bililive-recorder-adapter.v1"
STATUS_SCHEMA_VERSION = "recorder-neutral-status.v1"
STATE_SCHEMA_VERSION = "bililive-recorder-adapter-state.v1"
BACKEND = "BililiveRecorder"
QUALITY_PRIORITY = ("avc10000", "avc400", "avc250")
FILENAME_RX_TEMPLATE = r"^{room}_(?P<stamp>20\d{{6}}-\d{{2}}-\d{{2}}-\d{{2}})\.flv$"
GRAPHQL_ROOM_QUERY = """
query AdapterRoomStatus($roomId: Int!) {
  room(roomId: $roomId) {
    objectId
    name
    title
    recording
    streaming
    danmakuConnected
    ioStats {
      streamHost
      networkBytesDownloaded
      networkMbps
      diskBytesWritten
      diskMBps
    }
    recordingStats {
      currentFileSize
      totalInputBytes
      totalOutputBytes
    }
  }
}
""".strip()
EVENT_COMMANDS = {
    "d": "DANMU_MSG",
    "sc": "SUPER_CHAT_MESSAGE",
    "gift": "SEND_GIFT",
    "guard": "GUARD_BUY",
}
WEBHOOK_EVENT_TYPES = {
    "SessionStarted",
    "SessionEnded",
    "FileOpening",
    "FileClosed",
    "StreamStarted",
    "StreamEnded",
}


class AdapterError(RuntimeError):
    """A fail-closed adapter error safe to surface in status/log output."""


def load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.removeprefix("export ").split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def atomic_write_bytes(path: Path, payload: bytes, *, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with tmp.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def atomic_write_json(path: Path, payload: dict[str, Any], *, mode: int = 0o644) -> None:
    encoded = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    atomic_write_bytes(path, encoded, mode=mode)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _webhook_record_relative_path(relative_path: str, *, room_id: int) -> str:
    path = PurePosixPath(relative_path)
    parts = path.parts
    if path.is_absolute() or ".." in parts:
        raise AdapterError("webhook RelativePath is not a safe relative path")
    if len(parts) != 4 or parts[0:2] != ("Videos", str(room_id)):
        raise AdapterError("webhook RelativePath is outside the configured room root")
    date_name, file_name = parts[2], parts[3]
    if not re.fullmatch(r"20\d{2}-\d{2}-\d{2}", date_name):
        raise AdapterError("webhook RelativePath has an invalid date directory")
    filename_rx = re.compile(FILENAME_RX_TEMPLATE.format(room=re.escape(str(room_id))))
    if not filename_rx.fullmatch(file_name):
        raise AdapterError("webhook RelativePath has an unexpected recorder filename")
    return f"{date_name}/{file_name}"


def _validate_webhook_timestamp(value: Any, *, field: str) -> str:
    """Validate the seven-digit ISO timestamps emitted by .NET DateTimeOffset."""
    raw = str(value or "")
    match = re.fullmatch(
        r"(?P<head>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})"
        r"(?P<fraction>\.\d{1,7})?"
        r"(?P<zone>Z|[+-]\d{2}:\d{2})",
        raw,
    )
    if match is None:
        raise AdapterError(f"webhook {field} is invalid")
    fraction = match.group("fraction") or ""
    if fraction:
        fraction = "." + fraction[1:7].ljust(6, "0")
    zone = "+00:00" if match.group("zone") == "Z" else match.group("zone")
    try:
        datetime.fromisoformat(f"{match.group('head')}{fraction}{zone}")
    except ValueError as exc:
        raise AdapterError(f"webhook {field} is invalid") from exc
    return raw


def validate_webhook_event(payload: Any, *, room_id: int) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise AdapterError("webhook body must be a JSON object")
    event_type = str(payload.get("EventType") or "")
    if event_type not in WEBHOOK_EVENT_TYPES:
        raise AdapterError(f"unsupported webhook EventType: {event_type!r}")
    event_id = str(payload.get("EventId") or "")
    try:
        uuid.UUID(event_id)
    except (ValueError, AttributeError) as exc:
        raise AdapterError("webhook EventId is not a UUID") from exc
    event_timestamp = _validate_webhook_timestamp(
        payload.get("EventTimestamp"),
        field="EventTimestamp",
    )
    data = payload.get("EventData")
    if not isinstance(data, dict):
        raise AdapterError("webhook EventData must be an object")
    try:
        observed_room = int(data.get("RoomId"))
    except (TypeError, ValueError) as exc:
        raise AdapterError("webhook EventData.RoomId is invalid") from exc
    if observed_room != room_id:
        raise AdapterError("webhook event is for a different room")

    record_relative_path = None
    if event_type in {"FileOpening", "FileClosed"}:
        record_relative_path = _webhook_record_relative_path(
            str(data.get("RelativePath") or ""),
            room_id=room_id,
        )
        if not str(data.get("SessionId") or ""):
            raise AdapterError("file webhook lacks SessionId")
        _validate_webhook_timestamp(data.get("FileOpenTime"), field="FileOpenTime")
    if event_type == "FileClosed":
        try:
            file_size = int(data["FileSize"])
            duration = float(data["Duration"])
        except (KeyError, TypeError, ValueError) as exc:
            raise AdapterError("FileClosed has invalid size or duration") from exc
        if file_size <= 0 or duration <= 0:
            raise AdapterError("FileClosed has non-positive size or duration")
        _validate_webhook_timestamp(data.get("FileCloseTime"), field="FileCloseTime")

    return {
        "event_id": event_id,
        "event_type": event_type,
        "event_timestamp": event_timestamp,
        "event_data": data,
        "record_relative_path": record_relative_path,
    }


def append_webhook_event(
    journal_path: Path,
    payload: Any,
    *,
    room_id: int,
) -> None:
    validate_webhook_event(payload, room_id=room_id)
    encoded = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    if len(encoded) > 256 * 1024:
        raise AdapterError("webhook event exceeds 256 KiB")
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    if journal_path.is_symlink():
        raise AdapterError("webhook journal must not be a symlink")
    fd = os.open(journal_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.chmod(journal_path, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        view = memoryview(encoded)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def reconcile_webhook_journal(
    journal_path: Path,
    state: dict[str, Any],
    *,
    room_id: int,
) -> bool:
    try:
        stat = journal_path.stat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise AdapterError(f"webhook journal is unreadable: {exc}") from exc
    if journal_path.is_symlink() or not journal_path.is_file():
        raise AdapterError("webhook journal is not a regular file")
    if stat.st_size > 128 * 1024 * 1024:
        raise AdapterError("webhook journal exceeds 128 MiB; compact before continuing")

    seen = state.setdefault("webhook_event_ids", {})
    files = state.setdefault("webhook_files", {})
    if not isinstance(seen, dict) or not isinstance(files, dict):
        raise AdapterError("webhook ledger in adapter state is malformed")
    changed = False
    try:
        with journal_path.open("r", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
            try:
                lines = handle.read().splitlines()
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except (OSError, UnicodeError) as exc:
        raise AdapterError(f"webhook journal cannot be decoded: {exc}") from exc
    for line_number, line in enumerate(lines, start=1):
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise AdapterError(
                f"webhook journal line {line_number} is invalid JSON"
            ) from exc
        normalized = validate_webhook_event(payload, room_id=room_id)
        digest = hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        event_id = normalized["event_id"]
        previous = seen.get(event_id)
        if previous is not None:
            if not isinstance(previous, dict) or previous.get("sha256") != digest:
                raise AdapterError(f"webhook EventId payload conflict: {event_id}")
            continue

        event_type = normalized["event_type"]
        data = normalized["event_data"]
        record_relative = normalized["record_relative_path"]
        if event_type in {"FileOpening", "FileClosed"} and record_relative:
            row = files.setdefault(record_relative, {})
            if not isinstance(row, dict):
                raise AdapterError(f"webhook file ledger is malformed: {record_relative}")
            session_id = str(data.get("SessionId") or "")
            previous_session = str(row.get("session_id") or "")
            if previous_session and previous_session != session_id:
                raise AdapterError(f"recorder path reused by another session: {record_relative}")
            row["session_id"] = session_id
            row["file_open_time"] = str(data.get("FileOpenTime") or "")
            if event_type == "FileOpening":
                row["opening_event_id"] = event_id
                if row.get("status") != "CLOSED":
                    row["status"] = "OPEN"
            else:
                close_identity = {
                    "file_size": int(data["FileSize"]),
                    "duration": float(data["Duration"]),
                    "file_close_time": str(data.get("FileCloseTime") or ""),
                }
                if row.get("status") == "CLOSED":
                    for key, value in close_identity.items():
                        if row.get(key) != value:
                            raise AdapterError(
                                f"conflicting FileClosed evidence: {record_relative}"
                            )
                row.update(close_identity)
                row["closing_event_id"] = event_id
                row["status"] = "CLOSED"
        state["last_webhook_event"] = {
            "event_id": event_id,
            "event_type": event_type,
            "event_timestamp": normalized["event_timestamp"],
        }
        seen[event_id] = {"sha256": digest, "event_type": event_type}
        changed = True
    return changed


def _basic_auth_header(username: str, password: str) -> str:
    encoded = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {encoded}"


def query_room_status(
    endpoint: str,
    room_id: int,
    *,
    username: str = "",
    password: str = "",
    timeout_seconds: float = 15.0,
) -> dict[str, Any]:
    body = json.dumps(
        {"query": GRAPHQL_ROOM_QUERY, "variables": {"roomId": room_id}},
        separators=(",", ":"),
    ).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if username or password:
        headers["Authorization"] = _basic_auth_header(username, password)
    request = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except (OSError, urllib.error.URLError, ValueError) as exc:
        raise AdapterError(f"recorder GraphQL unavailable: {type(exc).__name__}: {exc}") from exc
    errors = payload.get("errors") if isinstance(payload, dict) else None
    if errors:
        first = errors[0] if isinstance(errors, list) and errors else errors
        raise AdapterError(f"recorder GraphQL error: {str(first)[:300]}")
    room = (payload.get("data") or {}).get("room") if isinstance(payload, dict) else None
    if not isinstance(room, dict):
        raise AdapterError(f"room {room_id} is not configured in BililiveRecorder")
    return room


def probe_cookie_health(
    config_path: Path,
    state: dict[str, Any],
    *,
    now_epoch: float,
    refresh_seconds: int = 6 * 60 * 60,
    timeout_seconds: float = 20.0,
) -> tuple[dict[str, Any], bool]:
    """Check login without ever returning or logging the Cookie itself."""

    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
        cookie = str(
            ((((config.get("global") or {}).get("Cookie") or {}).get("Value")) or "")
        ).strip()
    except (OSError, ValueError, AttributeError):
        cookie = ""
    cookie_sha256 = hashlib.sha256(cookie.encode("utf-8")).hexdigest() if cookie else ""
    cached = state.get("cookie_health")
    if isinstance(cached, dict):
        try:
            fresh = now_epoch - float(cached["checked_at_epoch"]) < refresh_seconds
        except (KeyError, TypeError, ValueError):
            fresh = False
        if fresh and cached.get("cookie_sha256") == cookie_sha256:
            return dict(cached), False
    result: dict[str, Any] = {
        "configured": bool(cookie),
        "login_valid": False if not cookie else None,
        "checked_at_epoch": now_epoch,
        "checked_at": datetime.fromtimestamp(now_epoch, tz=timezone.utc).isoformat(),
        "cookie_sha256": cookie_sha256,
        "api_code": None,
        "error": None,
    }
    if cookie:
        request = urllib.request.Request(
            "https://api.bilibili.com/x/web-interface/nav",
            headers={"Cookie": cookie, "User-Agent": "Mozilla/5.0"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8", "replace"))
            data = payload.get("data") if isinstance(payload, dict) else None
            result["api_code"] = payload.get("code") if isinstance(payload, dict) else None
            result["login_valid"] = bool(
                isinstance(data, dict) and data.get("isLogin") is True
            )
        except (OSError, urllib.error.URLError, ValueError) as exc:
            result["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
    state["cookie_health"] = result
    return result, True


def _run(
    command: list[str],
    *,
    timeout_seconds: int,
) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AdapterError(f"command failed to run: {command[0]}: {type(exc).__name__}: {exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()[-1200:]
        raise AdapterError(f"{command[0]} exited {completed.returncode}: {detail}")
    return completed


def probe_media(path: Path, *, ffprobe_bin: str = "ffprobe") -> dict[str, Any]:
    completed = _run(
        [
            ffprobe_bin,
            "-v",
            "error",
            "-show_entries",
            "format=duration,size:stream=index,codec_type,codec_name,width,height",
            "-of",
            "json",
            str(path),
        ],
        timeout_seconds=120,
    )
    try:
        payload = json.loads(completed.stdout)
        streams = payload.get("streams") or []
        media_format = payload.get("format") or {}
        duration = float(media_format.get("duration") or 0)
        size = int(media_format.get("size") or path.stat().st_size)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise AdapterError(f"invalid ffprobe result for {path.name}: {exc}") from exc
    video = next((row for row in streams if row.get("codec_type") == "video"), None)
    audio = next((row for row in streams if row.get("codec_type") == "audio"), None)
    if not isinstance(video, dict) or not isinstance(audio, dict):
        raise AdapterError(f"{path.name} must contain both video and audio streams")
    if duration <= 0 or size <= 0:
        raise AdapterError(f"{path.name} has invalid duration/size: {duration}/{size}")
    return {
        "duration_seconds": duration,
        "size_bytes": size,
        "video_codec": video.get("codec_name"),
        "audio_codec": audio.get("codec_name"),
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
    }


def probe_stream_shape(path: Path, *, ffprobe_bin: str = "ffprobe") -> dict[str, Any]:
    """Read only the opening seconds of an active FLV for objective quality telemetry."""

    completed = _run(
        [
            ffprobe_bin,
            "-v",
            "error",
            "-read_intervals",
            "%+2",
            "-show_entries",
            "stream=codec_type,codec_name,width,height",
            "-of",
            "json",
            str(path),
        ],
        timeout_seconds=30,
    )
    try:
        streams = (json.loads(completed.stdout) or {}).get("streams") or []
    except (TypeError, json.JSONDecodeError) as exc:
        raise AdapterError(f"invalid active-source ffprobe result for {path.name}: {exc}") from exc
    video = next((row for row in streams if row.get("codec_type") == "video"), None)
    audio = next((row for row in streams if row.get("codec_type") == "audio"), None)
    if not isinstance(video, dict):
        raise AdapterError(f"{path.name} active probe found no video stream")
    return {
        "video_codec": video.get("codec_name"),
        "audio_codec": audio.get("codec_name") if isinstance(audio, dict) else None,
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
    }


def packet_scan(path: Path, *, ffmpeg_bin: str = "ffmpeg") -> None:
    _run(
        [
            ffmpeg_bin,
            "-nostdin",
            "-v",
            "error",
            "-xerror",
            "-i",
            str(path),
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
        timeout_seconds=6 * 60 * 60,
    )


def _xml_record_info(root: ET.Element) -> dict[str, str]:
    node = root.find("BililiveRecorderRecordInfo")
    if node is None:
        return {}
    return {str(key): str(value) for key, value in node.attrib.items()}


def _start_epoch_seconds(record_info: dict[str, str]) -> float | None:
    raw = record_info.get("start_time")
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.timestamp()


def xml_to_jsonl(xml_path: Path) -> tuple[bytes, dict[str, str], int]:
    try:
        root = ET.parse(xml_path).getroot()
    except (OSError, ET.ParseError) as exc:
        raise AdapterError(f"invalid BililiveRecorder XML {xml_path.name}: {exc}") from exc
    record_info = _xml_record_info(root)
    if root.find("BililiveRecorder") is None or not record_info:
        raise AdapterError(f"{xml_path.name} is not an official BililiveRecorder XML sidecar")
    lines: list[str] = []
    for element in root:
        command = EVENT_COMMANDS.get(element.tag)
        if command is None:
            continue
        raw_value = element.attrib.get("raw")
        if not raw_value:
            raise AdapterError(
                f"{xml_path.name} event <{element.tag}> lacks required raw evidence"
            )
        try:
            raw: Any = json.loads(raw_value)
        except json.JSONDecodeError as exc:
            raise AdapterError(
                f"{xml_path.name} event <{element.tag}> has invalid raw JSON"
            ) from exc
        if element.tag == "d" and isinstance(raw, list):
            payload = {"cmd": command, "info": raw}
        elif element.tag != "d" and isinstance(raw, dict):
            payload = {"cmd": command, "data": raw}
        else:
            expected = "array" if element.tag == "d" else "object"
            raise AdapterError(
                f"{xml_path.name} event <{element.tag}> raw must be a JSON {expected}"
            )
        lines.append(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    data = (("\n".join(lines) + "\n") if lines else "").encode("utf-8")
    return data, record_info, len(lines)


def _fsync_directory(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        try:
            os.fsync(fd)
        except OSError as exc:
            if exc.errno not in {errno.EINVAL, errno.ENOTSUP, errno.EROFS}:
                raise
    finally:
        os.close(fd)


def _publish_path_noreplace(staged: Path, target: Path) -> str:
    """Atomically expose staged bytes without silently replacing target.

    CloudDrive's FUSE mount rejects hard links and RENAME_NOREPLACE.  Prefer a
    hard-link publish where supported; otherwise serialize compliant writers
    with an exclusive reservation directory and use same-directory rename.
    """

    try:
        os.link(staged, target)
        staged.unlink()
        _fsync_directory(target.parent)
        return "hardlink_noreplace"
    except FileExistsError as exc:
        raise AdapterError(f"target conflict; refusing overwrite: {target.name}") from exc
    except OSError as exc:
        if exc.errno not in {
            errno.EPERM,
            errno.EACCES,
            errno.EOPNOTSUPP,
            errno.ENOTSUP,
            errno.EXDEV,
            errno.EINVAL,
        }:
            raise AdapterError(f"atomic publish failed for {target.name}: {exc}") from exc

    reservation = target.with_name(f".{target.name}.publish-reservation")
    try:
        reservation.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise AdapterError(f"publish reservation conflict: {target.name}") from exc
    try:
        if target.exists():
            raise AdapterError(f"target conflict; refusing overwrite: {target.name}")
        os.rename(staged, target)
        _fsync_directory(target.parent)
        return "reserved_atomic_rename"
    finally:
        try:
            reservation.rmdir()
        except FileNotFoundError:
            pass


def _write_if_absent_or_equal(path: Path, payload: bytes, *, mode: int = 0o644) -> None:
    if path.exists():
        try:
            current = path.read_bytes()
        except OSError as exc:
            raise AdapterError(f"cannot read existing sidecar {path.name}: {exc}") from exc
        if current != payload:
            raise AdapterError(f"refusing to overwrite non-matching existing sidecar {path.name}")
        return
    staging = path.parent / ".brec-adapter-staging"
    staging.mkdir(mode=0o700, exist_ok=True)
    tmp = staging / f"{uuid.uuid4().hex}.{path.name}"
    try:
        with tmp.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        _publish_path_noreplace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def _meta_payload(
    source_flv: Path,
    xml_path: Path,
    record_info: dict[str, str],
    *,
    event_count: int,
) -> dict[str, Any]:
    source_stat = source_flv.stat()
    return {
        "schema_version": SCHEMA_VERSION,
        "description": {"RecordStartTime": record_info.get("start_time")},
        "segment_start_time": record_info.get("start_time"),
        "event_evidence": "bililiverecorder_xml_raw_subset",
        "recorder": {
            "backend": BACKEND,
            "room_id": record_info.get("roomid"),
            "room_name": record_info.get("name"),
            "title": record_info.get("title"),
            "event_count": event_count,
        },
        "source": {
            "flv": source_flv.name,
            "xml": xml_path.name,
            "size_bytes": source_stat.st_size,
            "mtime_ns": source_stat.st_mtime_ns,
            "delete_source": "never",
        },
    }


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(
        "utf-8"
    )


def finalize_recording(
    source_flv: Path,
    *,
    ffmpeg_bin: str = "ffmpeg",
    ffprobe_bin: str = "ffprobe",
    existing_ledger: dict[str, Any] | None = None,
) -> dict[str, Any]:
    xml_path = source_flv.with_suffix(".xml")
    if not xml_path.is_file():
        raise AdapterError(f"closed recording lacks XML sidecar: {source_flv.name}")
    jsonl_bytes, record_info, event_count = xml_to_jsonl(xml_path)
    target_mp4 = source_flv.with_suffix(".mp4")
    staging_dir = target_mp4.parent / ".brec-adapter-staging"
    staging_dir.mkdir(mode=0o700, exist_ok=True)
    tmp_mp4 = staging_dir / f"{uuid.uuid4().hex}.mp4"
    jsonl_path = source_flv.with_suffix(".jsonl")
    meta_path = source_flv.with_suffix(".meta.json")

    if target_mp4.exists():
        source_stat = source_flv.stat()
        if not isinstance(existing_ledger, dict):
            raise AdapterError(
                f"target exists without adapter ledger; refusing implicit success: {target_mp4.name}"
            )
        if (
            int(existing_ledger.get("source_size") or -1) != source_stat.st_size
            or int(existing_ledger.get("source_mtime_ns") or -1) != source_stat.st_mtime_ns
        ):
            raise AdapterError(f"source changed after finalization: {source_flv.name}")
        expected_sha = str(existing_ledger.get("target_sha256") or "")
        if not expected_sha or sha256_file(target_mp4) != expected_sha:
            raise AdapterError(f"target hash conflicts with adapter ledger: {target_mp4.name}")
        media = probe_media(target_mp4, ffprobe_bin=ffprobe_bin)
        _write_if_absent_or_equal(jsonl_path, jsonl_bytes)
        _write_if_absent_or_equal(
            meta_path,
            _json_bytes(
                _meta_payload(
                    source_flv,
                    xml_path,
                    record_info,
                    event_count=event_count,
                )
            ),
        )
        return {
            "source": str(source_flv),
            "target": str(target_mp4),
            "status": "already_finalized",
            "event_count": event_count,
            "media": media,
            "target_sha256": expected_sha,
            "publish_method": "existing_ledger_verified",
        }

    try:
        try:
            tmp_mp4.unlink()
        except FileNotFoundError:
            pass
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
                str(source_flv),
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
                str(tmp_mp4),
            ],
            timeout_seconds=6 * 60 * 60,
        )
        media = probe_media(tmp_mp4, ffprobe_bin=ffprobe_bin)
        packet_scan(tmp_mp4, ffmpeg_bin=ffmpeg_bin)
        with tmp_mp4.open("rb") as handle:
            os.fsync(handle.fileno())
        target_sha256 = sha256_file(tmp_mp4)
        _write_if_absent_or_equal(jsonl_path, jsonl_bytes)
        _write_if_absent_or_equal(
            meta_path,
            _json_bytes(
                _meta_payload(
                    source_flv,
                    xml_path,
                    record_info,
                    event_count=event_count,
                )
            ),
        )
        publish_method = _publish_path_noreplace(tmp_mp4, target_mp4)
    finally:
        try:
            tmp_mp4.unlink()
        except FileNotFoundError:
            pass
    return {
        "source": str(source_flv),
        "target": str(target_mp4),
        "status": "finalized",
        "event_count": event_count,
        "media": media,
        "target_sha256": target_sha256,
        "publish_method": publish_method,
    }


def load_or_initialize_state(
    path: Path,
    *,
    now_epoch: float,
    managed_since_epoch: float | None = None,
) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        payload = {
            "schema_version": STATE_SCHEMA_VERSION,
            "managed_since_epoch": float(
                managed_since_epoch if managed_since_epoch is not None else now_epoch
            ),
            "finalized": {},
        }
        atomic_write_json(path, payload, mode=0o600)
        return payload
    except (OSError, ValueError) as exc:
        raise AdapterError(f"adapter state is unreadable; refusing reset: {exc}") from exc
    if payload.get("schema_version") != STATE_SCHEMA_VERSION:
        raise AdapterError("adapter state schema mismatch; refusing reset")
    if not isinstance(payload.get("finalized"), dict):
        raise AdapterError("adapter state finalized ledger is malformed")
    try:
        float(payload["managed_since_epoch"])
    except (KeyError, TypeError, ValueError) as exc:
        raise AdapterError("adapter state lacks managed_since_epoch") from exc
    return payload


def _is_official_xml(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            prefix = handle.read(128 * 1024)
    except OSError:
        return False
    return b"<BililiveRecorder" in prefix and b"<BililiveRecorderRecordInfo" in prefix


def discover_managed_flvs(
    record_root: Path,
    *,
    room_id: int,
    managed_since_epoch: float,
) -> list[Path]:
    filename_rx = re.compile(FILENAME_RX_TEMPLATE.format(room=re.escape(str(room_id))))
    candidates: list[Path] = []
    try:
        date_dirs = sorted(
            path
            for path in record_root.iterdir()
            if path.is_dir() and re.fullmatch(r"20\d{2}-\d{2}-\d{2}", path.name)
        )
    except OSError as exc:
        raise AdapterError(f"recording root unreadable: {record_root}: {exc}") from exc
    for date_dir in date_dirs:
        try:
            flvs: Iterable[Path] = date_dir.glob(f"{room_id}_*.flv")
            for flv in flvs:
                if flv.parent != date_dir or not filename_rx.fullmatch(flv.name):
                    continue
                try:
                    new_enough = flv.stat().st_mtime >= managed_since_epoch
                except OSError:
                    continue
                if new_enough or _is_official_xml(flv.with_suffix(".xml")):
                    candidates.append(flv)
        except OSError:
            continue
    return sorted(set(candidates))


def _newest_source_probe(
    record_root: Path,
    room_id: int,
    *,
    ffprobe_bin: str = "ffprobe",
) -> dict[str, Any] | None:
    try:
        sources = sorted(
            record_root.glob(f"20??-??-??/{room_id}_*.flv"),
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        )
    except OSError:
        return None
    if not sources:
        return None
    source = sources[0]
    try:
        stat = source.stat()
    except OSError:
        return None
    result: dict[str, Any] = {
        "path": str(source),
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }
    if stat.st_size >= 256 * 1024:
        try:
            result["media"] = probe_stream_shape(source, ffprobe_bin=ffprobe_bin)
        except AdapterError as exc:
            result["media_probe_error"] = str(exc)[:300]
    return result


def build_status(
    *,
    room_id: int,
    room: dict[str, Any] | None,
    now_epoch: float,
    record_root: Path,
    error: str | None = None,
    finalizing: bool = False,
    finalized: list[dict[str, Any]] | None = None,
    finalize_errors: list[dict[str, str]] | None = None,
    ffprobe_bin: str = "ffprobe",
    cookie_health: dict[str, Any] | None = None,
    inactive_grace_remaining_seconds: int | None = None,
) -> dict[str, Any]:
    io_stats = (room or {}).get("ioStats") or {}
    recording_stats = (room or {}).get("recordingStats") or {}
    streaming = bool((room or {}).get("streaming")) if room is not None else None
    recording = bool((room or {}).get("recording")) if room is not None else None
    current_size = int(recording_stats.get("currentFileSize") or 0)
    total_input_bytes = int(recording_stats.get("totalInputBytes") or 0)
    total_output_bytes = int(recording_stats.get("totalOutputBytes") or 0)
    newest = _newest_source_probe(record_root, room_id, ffprobe_bin=ffprobe_bin)
    if newest and current_size <= 0 and recording:
        current_size = int(newest["size_bytes"])
    network_mbps = float(io_stats.get("networkMbps") or 0.0)
    service_reachable = room is not None and error is None
    effective_error = error
    if effective_error is None and finalize_errors:
        effective_error = f"{len(finalize_errors)} closed recording(s) failed finalization"
    live_status = None if not service_reachable else int(bool(streaming or recording))
    public_cookie_health = (
        {
            key: value
            for key, value in cookie_health.items()
            if key != "cookie_sha256"
        }
        if isinstance(cookie_health, dict)
        else None
    )
    return {
        "schema_version": STATUS_SCHEMA_VERSION,
        "backend": BACKEND,
        "generated_at": datetime.fromtimestamp(now_epoch, tz=timezone.utc).isoformat(),
        "generated_at_epoch": now_epoch,
        "room_id": str(room_id),
        "service_reachable": service_reachable,
        "live_status": live_status,
        "streaming": streaming,
        "recording": recording,
        "danmaku_connected": (
            bool((room or {}).get("danmakuConnected")) if room is not None else None
        ),
        "running_status": (
            "recording"
            if recording
            else "streaming"
            if streaming
            else "finalizing"
            if finalizing
            else "idle"
            if service_reachable
            else "unknown"
        ),
        "rec_total": current_size if service_reachable else None,
        "total_input_bytes": total_input_bytes if service_reachable else None,
        "total_output_bytes": total_output_bytes if service_reachable else None,
        "rec_rate": int(network_mbps * 1_000_000 / 8) if service_reachable else None,
        "stream_host": io_stats.get("streamHost") if service_reachable else None,
        "real_stream_format": "flv-standard",
        "requested_quality_priority": list(QUALITY_PRIORITY),
        "requested_quality_number": 10000,
        "actual_quality_number": None,
        "bilibili_cookie": public_cookie_health,
        "recording_path": newest.get("path") if newest and recording else None,
        "latest_source": newest,
        "finalizing": bool(finalizing),
        "inactive_grace_remaining_seconds": inactive_grace_remaining_seconds,
        "finalized": finalized or [],
        "finalize_errors": finalize_errors or [],
        "error": effective_error,
    }


def run_once(args: argparse.Namespace) -> int:
    now_epoch = time.time()
    state = load_or_initialize_state(
        args.state_path,
        now_epoch=now_epoch,
        managed_since_epoch=args.managed_since_epoch,
    )
    try:
        webhook_changed = reconcile_webhook_journal(
            args.webhook_journal,
            state,
            room_id=args.room,
        )
    except AdapterError as exc:
        status = build_status(
            room_id=args.room,
            room=None,
            now_epoch=time.time(),
            record_root=args.record_root,
            error=str(exc),
            ffprobe_bin=args.ffprobe,
        )
        atomic_write_json(args.status_path, status, mode=0o644)
        print(json.dumps(status, ensure_ascii=False))
        return 2
    if webhook_changed:
        atomic_write_json(args.state_path, state, mode=0o600)
    cookie_health, cookie_health_changed = probe_cookie_health(
        args.recorder_config,
        state,
        now_epoch=now_epoch,
        refresh_seconds=args.cookie_health_refresh,
        timeout_seconds=args.cookie_health_timeout,
    )
    if cookie_health_changed:
        atomic_write_json(args.state_path, state, mode=0o600)
    env = load_env_file(args.env_file)
    username = env.get("BREC_HTTP_BASIC_USER", "")
    password = env.get("BREC_HTTP_BASIC_PASS", "")
    try:
        room = query_room_status(
            args.endpoint,
            args.room,
            username=username,
            password=password,
            timeout_seconds=args.api_timeout,
        )
    except AdapterError as exc:
        state_changed = False
        for key in ("inactive_since_epoch", "last_room_status_epoch"):
            if state.pop(key, None) is not None:
                state_changed = True
        if state_changed:
            atomic_write_json(args.state_path, state, mode=0o600)
        status = build_status(
            room_id=args.room,
            room=None,
            now_epoch=time.time(),
            record_root=args.record_root,
            error=str(exc),
            ffprobe_bin=args.ffprobe,
            cookie_health=cookie_health,
        )
        atomic_write_json(args.status_path, status, mode=0o644)
        print(json.dumps(status, ensure_ascii=False))
        return 2

    observed_epoch = time.time()
    try:
        last_room_status_epoch = float(state["last_room_status_epoch"])
        status_continuous = (
            0 <= observed_epoch - last_room_status_epoch
            <= args.status_continuity_max_gap_seconds
        )
    except (KeyError, TypeError, ValueError):
        status_continuous = False
    state["last_room_status_epoch"] = observed_epoch

    active = bool(room.get("streaming") or room.get("recording"))
    finalized: list[dict[str, Any]] = []
    finalize_errors: list[dict[str, str]] = []
    if active:
        state.pop("inactive_since_epoch", None)
        atomic_write_json(args.state_path, state, mode=0o600)
        status = build_status(
            room_id=args.room,
            room=room,
            now_epoch=time.time(),
            record_root=args.record_root,
            ffprobe_bin=args.ffprobe,
            cookie_health=cookie_health,
        )
        atomic_write_json(args.status_path, status, mode=0o644)
        print(json.dumps(status, ensure_ascii=False))
        return 0

    if not status_continuous:
        state.pop("inactive_since_epoch", None)
    try:
        inactive_since = float(state["inactive_since_epoch"])
        if inactive_since > observed_epoch + 60:
            raise ValueError("future inactivity timestamp")
    except (KeyError, TypeError, ValueError):
        inactive_since = observed_epoch
        state["inactive_since_epoch"] = inactive_since
    atomic_write_json(args.state_path, state, mode=0o600)
    inactive_age = max(0.0, observed_epoch - inactive_since)
    grace_remaining = max(0.0, args.inactive_grace_seconds - inactive_age)
    if grace_remaining > 0:
        status = build_status(
            room_id=args.room,
            room=room,
            now_epoch=time.time(),
            record_root=args.record_root,
            finalizing=True,
            ffprobe_bin=args.ffprobe,
            cookie_health=cookie_health,
            inactive_grace_remaining_seconds=math.ceil(grace_remaining),
        )
        atomic_write_json(args.status_path, status, mode=0o644)
        print(json.dumps(status, ensure_ascii=False))
        return 0

    candidates = discover_managed_flvs(
        args.record_root,
        room_id=args.room,
        managed_since_epoch=float(state["managed_since_epoch"]),
    )
    closed_files = state.get("webhook_files") or {}
    eligible: list[Path] = []
    for source in candidates:
        relative = str(source.relative_to(args.record_root))
        evidence = closed_files.get(relative) if isinstance(closed_files, dict) else None
        if not isinstance(evidence, dict) or evidence.get("status") != "CLOSED":
            finalize_errors.append(
                {
                    "source": str(source),
                    "error": "source lacks BililiveRecorder FileClosed evidence",
                }
            )
            continue
        try:
            source_size = source.stat().st_size
        except OSError as exc:
            finalize_errors.append({"source": str(source), "error": str(exc)[:1200]})
            continue
        if source_size != int(evidence.get("file_size") or -1):
            finalize_errors.append(
                {
                    "source": str(source),
                    "error": (
                        "source size does not match BililiveRecorder FileClosed "
                        f"({source_size} != {evidence.get('file_size')})"
                    ),
                }
            )
            continue
        eligible.append(source)

    if isinstance(closed_files, dict):
        for relative, evidence in closed_files.items():
            if not isinstance(evidence, dict):
                continue
            source = args.record_root / relative
            if evidence.get("status") == "OPEN":
                finalize_errors.append(
                    {
                        "source": str(source),
                        "error": "FileOpening has no matching FileClosed event",
                    }
                )
            elif evidence.get("status") == "CLOSED" and not source.is_file():
                finalize_errors.append(
                    {
                        "source": str(source),
                        "error": "FileClosed event points to a missing source file",
                    }
                )

    pending = [
        source
        for source in eligible
        if str(source.relative_to(args.record_root)) not in state["finalized"]
        or not source.with_suffix(".mp4").is_file()
    ]
    if pending or finalize_errors:
        in_progress = build_status(
            room_id=args.room,
            room=room,
            now_epoch=time.time(),
            record_root=args.record_root,
            finalizing=True,
            ffprobe_bin=args.ffprobe,
            cookie_health=cookie_health,
        )
        atomic_write_json(args.status_path, in_progress, mode=0o644)

    for source in pending[: args.max_finalize]:
        relative = str(source.relative_to(args.record_root))
        try:
            result = finalize_recording(
                source,
                ffmpeg_bin=args.ffmpeg,
                ffprobe_bin=args.ffprobe,
                existing_ledger=state["finalized"].get(relative),
            )
            finalized.append(result)
            state["finalized"][relative] = {
                "source_size": source.stat().st_size,
                "source_mtime_ns": source.stat().st_mtime_ns,
                "target": str(source.with_suffix(".mp4")),
                "target_sha256": result["target_sha256"],
                "finalized_at": datetime.now(timezone.utc).isoformat(),
            }
            atomic_write_json(args.state_path, state, mode=0o600)
        except (AdapterError, OSError) as exc:
            finalize_errors.append({"source": str(source), "error": str(exc)[:1200]})

    status = build_status(
        room_id=args.room,
        room=room,
        now_epoch=time.time(),
        record_root=args.record_root,
        finalizing=len(pending) > args.max_finalize,
        finalized=finalized,
        finalize_errors=finalize_errors,
        ffprobe_bin=args.ffprobe,
        cookie_health=cookie_health,
    )
    atomic_write_json(args.status_path, status, mode=0o644)
    print(json.dumps(status, ensure_ascii=False))
    return 1 if finalize_errors else 0


def _make_webhook_handler(
    *,
    room_id: int,
    journal_path: Path,
) -> type[BaseHTTPRequestHandler]:
    class WebhookHandler(BaseHTTPRequestHandler):
        server_version = "BililiveRecorderAdapterWebhook/1"
        protocol_version = "HTTP/1.1"

        def _empty_response(self, status: int) -> None:
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()

        def do_POST(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler API
            if self.path != "/webhook":
                self._empty_response(404)
                return
            try:
                content_length = int(self.headers.get("Content-Length") or "")
            except ValueError:
                self._empty_response(411)
                return
            if content_length < 2 or content_length > 256 * 1024:
                self._empty_response(413)
                return
            payload: Any = None
            try:
                payload = json.loads(self.rfile.read(content_length))
                append_webhook_event(
                    journal_path,
                    payload,
                    room_id=room_id,
                )
            except (AdapterError, json.JSONDecodeError, OSError) as exc:
                event_type = payload.get("EventType") if isinstance(payload, dict) else None
                event_data = payload.get("EventData") if isinstance(payload, dict) else None
                relative = (
                    event_data.get("RelativePath")
                    if isinstance(event_data, dict)
                    else None
                )
                event_timestamp = (
                    payload.get("EventTimestamp") if isinstance(payload, dict) else None
                )
                print(
                    "webhook rejected "
                    f"type={event_type!r} timestamp={str(event_timestamp)[:80]!r} "
                    f"relative={str(relative)[:240]!r}: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
                self._empty_response(400)
                return
            self._empty_response(204)

        def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler API
            self._empty_response(404)

        def log_message(self, _format: str, *args: Any) -> None:
            return

    return WebhookHandler


def serve_adapter(args: argparse.Namespace) -> int:
    stop_event = threading.Event()

    def worker() -> None:
        while not stop_event.is_set():
            started = time.monotonic()
            try:
                run_once(args)
            except Exception as exc:  # noqa: BLE001 — daemon must publish failure state
                message = f"adapter worker failure: {type(exc).__name__}: {str(exc)[:500]}"
                try:
                    status = build_status(
                        room_id=args.room,
                        room=None,
                        now_epoch=time.time(),
                        record_root=args.record_root,
                        error=message,
                        ffprobe_bin=args.ffprobe,
                    )
                    atomic_write_json(args.status_path, status, mode=0o644)
                except Exception:
                    pass
                print(message, file=sys.stderr, flush=True)
            elapsed = time.monotonic() - started
            stop_event.wait(max(1.0, args.poll_interval - elapsed))

    handler = _make_webhook_handler(
        room_id=args.room,
        journal_path=args.webhook_journal,
    )
    server = ThreadingHTTPServer((args.webhook_bind, args.webhook_port), handler)
    thread = threading.Thread(target=worker, name="adapter-reconcile", daemon=True)
    thread.start()
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        server.server_close()
        thread.join(timeout=5)
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true", help="run one status/finalization pass")
    mode.add_argument(
        "--serve",
        action="store_true",
        help="serve Webhook v2 and periodically reconcile/finalize",
    )
    parser.add_argument("--room", type=int, default=22966160)
    parser.add_argument(
        "--endpoint",
        default="http://127.0.0.1:23566/graphql",
        help="localhost BililiveRecorder GraphQL endpoint",
    )
    parser.add_argument(
        "--record-root",
        type=Path,
        default=Path(
            "/root/clouddrive2/CloudNAS/CloudDrive/123云盘/live-streaming/22966160"
        ),
    )
    parser.add_argument(
        "--status-path",
        type=Path,
        default=Path("/opt/bilive/recording/status.json"),
    )
    parser.add_argument(
        "--state-path",
        type=Path,
        default=Path("/opt/bilive/recording/adapter-state.json"),
    )
    parser.add_argument(
        "--webhook-journal",
        type=Path,
        default=Path("/opt/bilive/recording/webhook-events.jsonl"),
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path("/opt/bilive/recorder.env"),
    )
    parser.add_argument(
        "--recorder-config",
        type=Path,
        default=Path("/opt/bilive/bililive-recorder/config.json"),
    )
    parser.add_argument("--cookie-health-refresh", type=int, default=6 * 60 * 60)
    parser.add_argument("--cookie-health-timeout", type=float, default=20.0)
    parser.add_argument("--managed-since-epoch", type=float)
    parser.add_argument("--api-timeout", type=float, default=15.0)
    parser.add_argument(
        "--inactive-grace-seconds",
        type=int,
        default=360,
        help="require this much continuously inactive time before finalization",
    )
    parser.add_argument(
        "--status-continuity-max-gap-seconds",
        type=int,
        default=150,
        help="reset inactivity grace when healthy recorder polls are too far apart",
    )
    parser.add_argument("--max-finalize", type=int, default=8)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    parser.add_argument("--webhook-bind", default="127.0.0.1")
    parser.add_argument("--webhook-port", type=int, default=18080)
    parser.add_argument("--poll-interval", type=float, default=60.0)
    args = parser.parse_args(argv)
    if args.max_finalize < 1:
        parser.error("--max-finalize must be positive")
    if args.inactive_grace_seconds < 0:
        parser.error("--inactive-grace-seconds must be non-negative")
    if args.status_continuity_max_gap_seconds < 1:
        parser.error("--status-continuity-max-gap-seconds must be positive")
    if not (1 <= args.webhook_port <= 65535):
        parser.error("--webhook-port is out of range")
    if args.poll_interval < 5:
        parser.error("--poll-interval must be at least 5 seconds")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    return serve_adapter(args) if args.serve else run_once(args)


if __name__ == "__main__":
    sys.exit(main())
