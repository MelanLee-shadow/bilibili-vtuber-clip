"""Acquire hash-bound recordings from a profile-registered official map.

The existing external-audio resolver is intentionally cached-only.  This
module is the small producer-side seam that may populate that cache when a
normal profile supplies a verified public map.  Pixel controls only shortlist
one exact button; they never establish playback or a speaker role.
"""
from __future__ import annotations

from src.autoslice.evidence_identity import file_sha256 as _evidence_file_sha256
import hashlib
import json
import os
import subprocess
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

from src.autoslice.package_import import atomic_write_json, exclusive_lock
from src.autoslice.producer_nested_external_audio import normalize_source_controls


SOURCE_MAP_SCHEMA = "nested-external-audio-source-map.v1"
ACQUISITION_RESULT_SCHEMA = "nested-external-audio-acquisition-result.v1"
CATALOG_SCHEMA = "nested-external-audio-catalog.v1"
_MAX_AUDIO_BYTES = 64 * 1024 * 1024
_DOWNLOAD_TIMEOUT_SECONDS = 30
_SHA256_LENGTH = 64


class ExternalAudioAcquisitionError(ValueError):
    """A map, cache binding, or one-shot acquisition cannot be trusted."""

    def __init__(self, reason_code: str, **details: object) -> None:
        self.reason_code = reason_code
        self.details = details
        super().__init__(reason_code)


def _sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha(path: Path) -> str:
    digest = _evidence_file_sha256(path)
    return digest


def _prefixed_sha(value: str) -> str:
    return "sha256:" + value


def _bare_sha(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.removeprefix("sha256:").lower()
    if len(value) != _SHA256_LENGTH or any(c not in "0123456789abcdef" for c in value):
        return None
    return value


def _same_sha(left: object, right: object) -> bool:
    left_sha, right_sha = _bare_sha(left), _bare_sha(right)
    return left_sha is not None and right_sha is not None and left_sha == right_sha


def _regular_path(value: Path, *, label: str) -> Path:
    path = Path(value).expanduser().absolute()
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ExternalAudioAcquisitionError(f"{label}_UNAVAILABLE") from exc
    if path.is_symlink() or resolved.is_symlink() or not resolved.is_file():
        raise ExternalAudioAcquisitionError(f"{label}_UNAVAILABLE")
    return resolved


def _read_json(path: Path, *, code: str) -> Mapping[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ExternalAudioAcquisitionError(code) from exc
    if not isinstance(value, Mapping):
        raise ExternalAudioAcquisitionError(code)
    return value


def _official_url(value: object, *, code: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ExternalAudioAcquisitionError(code)
    url = value.strip()
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise ExternalAudioAcquisitionError(code)
    return url


def _url_identity(value: object, *, code: str) -> str:
    """Normalize only URL spelling that cannot change the requested route."""

    url = _official_url(value, code=code)
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ExternalAudioAcquisitionError(code) from exc
    if host is None:
        raise ExternalAudioAcquisitionError(code)
    host = host.lower()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    # HTTPS's default port is equivalent; every path/query/fragment byte stays
    # part of the identity so a redirect cannot silently select another asset.
    netloc = host if port in (None, 443) else f"{host}:{port}"
    return urlunsplit((parsed.scheme.lower(), netloc, parsed.path or "/", parsed.query, parsed.fragment))


def _source_map(path_value: Path) -> tuple[Path, str, Mapping[str, object]]:
    path = _regular_path(path_value, label="EXTERNAL_AUDIO_SOURCE_MAP")
    map_sha = _sha(path)
    document = _read_json(path, code="EXTERNAL_AUDIO_SOURCE_MAP_INVALID")
    if document.get("schema_version") not in {None, SOURCE_MAP_SCHEMA}:
        raise ExternalAudioAcquisitionError("EXTERNAL_AUDIO_SOURCE_MAP_SCHEMA_INVALID")
    page_url = _official_url(
        document.get("source_url") or document.get("source_page_url"),
        code="EXTERNAL_AUDIO_SOURCE_MAP_SOURCE_URL_INVALID",
    )
    if document.get("page_sha256") is not None and _bare_sha(document.get("page_sha256")) is None:
        raise ExternalAudioAcquisitionError("EXTERNAL_AUDIO_SOURCE_MAP_PAGE_HASH_INVALID")
    buttons = document.get("buttons")
    if not isinstance(buttons, list) or not buttons:
        raise ExternalAudioAcquisitionError("EXTERNAL_AUDIO_SOURCE_MAP_BUTTONS_INVALID")
    if document.get("button_count") is not None and document.get("button_count") != len(buttons):
        raise ExternalAudioAcquisitionError("EXTERNAL_AUDIO_SOURCE_MAP_BUTTON_COUNT_MISMATCH")
    page_host = urlsplit(page_url).hostname
    seen_labels: set[str] = set()
    for button in buttons:
        if not isinstance(button, Mapping):
            raise ExternalAudioAcquisitionError("EXTERNAL_AUDIO_SOURCE_MAP_BUTTON_INVALID")
        label = button.get("label")
        if not isinstance(label, str) or not label.strip() or label in seen_labels:
            raise ExternalAudioAcquisitionError("EXTERNAL_AUDIO_SOURCE_MAP_LABEL_NOT_UNIQUE")
        seen_labels.add(label)
        route = _official_url(
            button.get("url") or button.get("data_audio"),
            code="EXTERNAL_AUDIO_SOURCE_MAP_ROUTE_INVALID",
        )
        relative_route = button.get("data_audio")
        if isinstance(relative_route, str) and relative_route.strip():
            if urlsplit(route).path != relative_route.strip():
                raise ExternalAudioAcquisitionError("EXTERNAL_AUDIO_SOURCE_MAP_ROUTE_DRIFT")
        if page_host and urlsplit(route).hostname != page_host:
            raise ExternalAudioAcquisitionError("EXTERNAL_AUDIO_SOURCE_MAP_ROUTE_HOST_MISMATCH")
    return path, map_sha, document


def _button_for_controls(
    source_controls: Mapping[str, object],
    *,
    cue_text: object,
    buttons: list[Mapping[str, object]],
) -> tuple[str, str, str]:
    try:
        controls = normalize_source_controls(source_controls)
    except (TypeError, ValueError) as exc:
        raise ExternalAudioAcquisitionError("EXTERNAL_AUDIO_SOURCE_CONTROLS_INVALID") from exc
    if controls is None:
        raise ExternalAudioAcquisitionError("EXTERNAL_AUDIO_SOURCE_CONTROLS_MISSING")
    visible = controls.get("visible_ui")
    if not isinstance(visible, list):
        raise ExternalAudioAcquisitionError("EXTERNAL_AUDIO_SOURCE_CONTROLS_INVALID")
    labels = {button["label"] for button in buttons if isinstance(button.get("label"), str)}
    cue = cue_text.strip() if isinstance(cue_text, str) else ""
    if cue and cue in visible and cue in labels:
        return cue, "CURRENT_CUE_EXACT_VISIBLE_LABEL", "cue_text"
    cursor = controls.get("button_under_cursor")
    if isinstance(cursor, str) and cursor in labels:
        return cursor, "CURSOR_LABEL", "cursor"
    candidates = sorted(set(visible) & labels)
    if len(candidates) != 1:
        code = (
            "EXTERNAL_AUDIO_SOURCE_LABEL_AMBIGUOUS"
            if len(candidates) > 1
            else "EXTERNAL_AUDIO_SOURCE_LABEL_UNAVAILABLE"
        )
        raise ExternalAudioAcquisitionError(code)
    return candidates[0], "CURRENT_VISIBLE_LABEL_UNALIGNED_VOICE_REFERENCE", "visible_ui"


def _route_record(
    route_dir: Path,
    *,
    map_path: Path,
    map_sha: str,
    label: str,
    url: str,
) -> tuple[Path, Mapping[str, object]] | None:
    record_path = route_dir / "record.json"
    if not record_path.is_file() or record_path.is_symlink():
        return None
    record = _read_json(record_path, code="EXTERNAL_AUDIO_CACHE_RECORD_INVALID")
    if (
        record.get("schema_version") != ACQUISITION_RESULT_SCHEMA
        or record.get("label") != label
        or record.get("source_uri") != url
        or not _same_sha(record.get("source_map_sha256"), map_sha)
    ):
        raise ExternalAudioAcquisitionError("EXTERNAL_AUDIO_CACHE_BINDING_MISMATCH")
    raw_path = record.get("audio_path")
    if not isinstance(raw_path, str) or not raw_path:
        raise ExternalAudioAcquisitionError("EXTERNAL_AUDIO_CACHE_AUDIO_PATH_MISSING")
    audio_path = Path(raw_path).expanduser()
    if audio_path.is_symlink():
        raise ExternalAudioAcquisitionError("EXTERNAL_AUDIO_CACHE_AUDIO_UNAVAILABLE")
    try:
        audio_path = audio_path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ExternalAudioAcquisitionError("EXTERNAL_AUDIO_CACHE_AUDIO_UNAVAILABLE") from exc
    try:
        audio_path.relative_to(route_dir.resolve())
    except ValueError as exc:
        raise ExternalAudioAcquisitionError("EXTERNAL_AUDIO_CACHE_AUDIO_OUTSIDE_ROUTE") from exc
    expected = record.get("audio_sha256")
    actual = _sha(audio_path)
    if not _same_sha(expected, actual):
        raise ExternalAudioAcquisitionError("EXTERNAL_AUDIO_CACHE_AUDIO_HASH_MISMATCH")
    decode = route_dir / "decode.json"
    if not decode.is_file() or decode.is_symlink():
        raise ExternalAudioAcquisitionError("EXTERNAL_AUDIO_CACHE_DECODE_RECEIPT_MISSING")
    decode_doc = _read_json(decode, code="EXTERNAL_AUDIO_CACHE_DECODE_RECEIPT_INVALID")
    if decode_doc.get("status") != "COMPLETE" or decode_doc.get("audio_sha256") != _prefixed_sha(actual):
        raise ExternalAudioAcquisitionError("EXTERNAL_AUDIO_CACHE_DECODE_RECEIPT_MISMATCH")
    return audio_path, record


def _catalog_for_record(
    route_dir: Path,
    *,
    map_path: Path,
    map_sha: str,
    label: str,
    url: str,
    audio_path: Path,
    audio_sha: str,
) -> dict[str, object]:
    result = {
        "schema_version": ACQUISITION_RESULT_SCHEMA,
        "source_map_path": str(map_path),
        "source_map_sha256": _prefixed_sha(map_sha),
        "source_role_authority": False,
        "audio": [{
            "label": label,
            "path": str(audio_path),
            "url": url,
            "source_uri": url,
            "audio_sha256": _prefixed_sha(audio_sha),
            "file_sha256": _prefixed_sha(audio_sha),
            "status": "READY",
        }],
    }
    result_path = route_dir / "result.json"
    result_sha = atomic_write_json(result_path, result)
    catalog = {
        "schema_version": CATALOG_SCHEMA,
        "source_map_path": str(map_path),
        "source_map_sha256": _prefixed_sha(map_sha),
        "result_path": str(result_path),
        "result_sha256": _prefixed_sha(result_sha),
        "source_role_authority": False,
    }
    catalog_path = route_dir / "catalog.json"
    catalog_sha = atomic_write_json(catalog_path, catalog)
    return {
        "catalog_path": str(catalog_path),
        "catalog_sha256": _prefixed_sha(catalog_sha),
        "catalog_value": f"{catalog_path}#sha256:{catalog_sha}",
        "result_path": str(result_path),
        "result_sha256": _prefixed_sha(result_sha),
    }


def _decode_audio(audio_path: Path, route_dir: Path) -> tuple[dict[str, object], str]:
    """Require both an audio stream probe and a complete decoder pass."""
    ffprobe_command = [
        "ffprobe", "-v", "error", "-show_streams", "-of", "json", str(audio_path),
    ]
    ffmpeg_command = [
        "ffmpeg", "-v", "error", "-i", str(audio_path), "-map", "0:a:0",
        "-f", "null", "-",
    ]
    try:
        probe = subprocess.run(
            ffprobe_command, check=True, capture_output=True, text=True,
        )
        probe_document = json.loads(probe.stdout or "{}")
        streams = probe_document.get("streams") if isinstance(probe_document, Mapping) else None
        if not isinstance(streams, list) or not any(
            isinstance(stream, Mapping) and stream.get("codec_type") == "audio"
            for stream in streams
        ):
            raise ExternalAudioAcquisitionError("EXTERNAL_AUDIO_NO_AUDIO_STREAM")
        subprocess.run(ffmpeg_command, check=True, capture_output=True, text=True)
    except ExternalAudioAcquisitionError:
        raise
    except (OSError, subprocess.SubprocessError, UnicodeError, json.JSONDecodeError) as exc:
        raise ExternalAudioAcquisitionError(
            "EXTERNAL_AUDIO_DECODE_FAILED", error_type=type(exc).__name__
        ) from exc
    receipt = {
        "schema_version": "nested-external-audio-decode-receipt.v1",
        "status": "COMPLETE",
        "audio_path": str(audio_path),
        "audio_sha256": _prefixed_sha(_sha(audio_path)),
        "ffprobe": {"command": ffprobe_command, "audio_stream_verified": True},
        "ffmpeg": {"command": ffmpeg_command, "complete_decode_verified": True},
    }
    receipt_path = route_dir / "decode.json"
    receipt_sha = atomic_write_json(receipt_path, receipt)
    return receipt, receipt_sha


def _failure(
    route_dir: Path,
    *,
    reason_code: str,
    map_path: Path,
    map_sha: str,
    label: str | None = None,
    url: str | None = None,
    **details: object,
) -> dict[str, object]:
    receipt = {
        "schema_version": "nested-external-audio-acquisition-failure.v1",
        "status": "UNKNOWN_KEEP",
        "reason_code": reason_code,
        "source_map_path": str(map_path),
        "source_map_sha256": _prefixed_sha(map_sha),
        "label": label,
        "source_uri": url,
        "attempts": 1 if details.get("attempted_download") else 0,
        **details,
    }
    receipt_sha = atomic_write_json(route_dir / "failure.json", receipt)
    return {
        "status": "UNKNOWN_KEEP",
        "authority": "EVIDENCE_ONLY",
        "reason_code": reason_code,
        "source_role_authority": False,
        "source_map_path": str(map_path),
        "source_map_sha256": _prefixed_sha(map_sha),
        "acquisition_receipt_path": str(route_dir / "failure.json"),
        "acquisition_receipt_sha256": _prefixed_sha(receipt_sha),
        **({"label": label} if label is not None else {}),
        **({"source_uri": url} if url is not None else {}),
    }


def _path_present(path: Path) -> bool:
    """Treat broken symlinks and directories as occupied cache paths too."""

    return path.exists() or path.is_symlink()


def _prepare_route_locked(
    route_dir: Path,
    *,
    map_path: Path,
    map_sha: str,
    label: str,
    route_url: str,
    selection_basis: str,
    selection_hint: str,
) -> dict[str, object]:
    """Read, acquire, and publish one route while its route lock is held."""

    def failure(reason_code: str, **details: object) -> dict[str, object]:
        result = _failure(
            route_dir,
            reason_code=reason_code,
            map_path=map_path,
            map_sha=map_sha,
            label=label,
            url=route_url,
            selection_basis=selection_basis,
            **details,
        )
        result["selection_basis"] = selection_basis
        return result

    failure_path = route_dir / "failure.json"
    if failure_path.is_file() and not failure_path.is_symlink():
        previous = _read_json(failure_path, code="EXTERNAL_AUDIO_FAILURE_RECEIPT_INVALID")
        if (
            previous.get("source_uri") == route_url
            and _same_sha(previous.get("source_map_sha256"), map_sha)
            and previous.get("label") == label
        ):
            failure_sha = _sha(failure_path)
            return {
                "status": "UNKNOWN_KEEP",
                "authority": "EVIDENCE_ONLY",
                "reason_code": str(previous.get("reason_code") or "EXTERNAL_AUDIO_PREVIOUS_FAILURE"),
                "label": label,
                "selection_basis": selection_basis,
                "source_role_authority": False,
                "source_map_path": str(map_path),
                "source_map_sha256": _prefixed_sha(map_sha),
                "acquisition_receipt_path": str(failure_path),
                "acquisition_receipt_sha256": _prefixed_sha(failure_sha),
            }

    cached = _route_record(
        route_dir, map_path=map_path, map_sha=map_sha,
        label=label, url=route_url,
    )
    if cached is not None:
        audio_path, record = cached
        audio_sha = _bare_sha(record.get("audio_sha256"))
        assert audio_sha is not None
        catalog = _catalog_for_record(
            route_dir, map_path=map_path, map_sha=map_sha,
            label=label, url=route_url, audio_path=audio_path,
            audio_sha=audio_sha,
        )
        acquisition = {
            "status": "CACHE_HIT",
            "selection_hint": selection_hint,
            "source_map_path": str(map_path),
            "source_map_sha256": _prefixed_sha(map_sha),
        }
        return {
            "status": "READY",
            "authority": "EVIDENCE_ONLY",
            "label": label,
            "selection_basis": selection_basis,
            "source_role_authority": False,
            "source_map_path": str(map_path),
            "source_map_sha256": _prefixed_sha(map_sha),
            "acquisition": acquisition,
            **catalog,
        }

    part_path = route_dir / "audio.mp3.part"
    audio_path = route_dir / "audio.mp3"
    # A record-less byte is an unbound artifact.  It is never overwritten or
    # removed; the route remains UNKNOWN_KEEP until an operator binds it.
    if _path_present(audio_path) or _path_present(part_path):
        return failure(
            "EXTERNAL_AUDIO_CACHE_UNBOUND_AUDIO_PRESENT"
            if _path_present(audio_path)
            else "EXTERNAL_AUDIO_CACHE_UNBOUND_PART_PRESENT",
            existing_audio_path=str(audio_path) if _path_present(audio_path) else None,
            existing_part_path=str(part_path) if _path_present(part_path) else None,
            attempted_download=False,
        )

    request = Request(route_url, headers={"User-Agent": "vtuber-slice-external-audio/1"})
    response_status: int | None = None
    effective_url = route_url
    content_type: str | None = None
    try:
        with urlopen(request, timeout=_DOWNLOAD_TIMEOUT_SECONDS) as response:
            response_status = getattr(response, "status", None)
            effective_url = str(getattr(response, "geturl", lambda: route_url)())
            content_type = (
                response.headers.get("Content-Type")
                if getattr(response, "headers", None)
                else None
            )
            if isinstance(response_status, int) and response_status >= 400:
                raise ExternalAudioAcquisitionError("EXTERNAL_AUDIO_HTTP_STATUS_FAILED")
            if _url_identity(effective_url, code="EXTERNAL_AUDIO_REDIRECT_URL_INVALID") != _url_identity(
                route_url, code="EXTERNAL_AUDIO_SOURCE_MAP_ROUTE_INVALID"
            ):
                raise ExternalAudioAcquisitionError("EXTERNAL_AUDIO_REDIRECT_URL_MISMATCH")
            try:
                with part_path.open("xb") as handle:
                    total = 0
                    while True:
                        block = response.read(1 << 20)
                        if not block:
                            break
                        total += len(block)
                        if total > _MAX_AUDIO_BYTES:
                            raise ExternalAudioAcquisitionError("EXTERNAL_AUDIO_DOWNLOAD_TOO_LARGE")
                        handle.write(block)
                    handle.flush()
                    os.fsync(handle.fileno())
            except FileExistsError:
                return failure(
                    "EXTERNAL_AUDIO_CACHE_UNBOUND_PART_PRESENT",
                    existing_part_path=str(part_path),
                    attempted_download=False,
                    effective_url=effective_url,
                    response_status=response_status,
                    content_type=content_type,
                )
        if _path_present(audio_path):
            return failure(
                "EXTERNAL_AUDIO_CACHE_UNBOUND_AUDIO_PRESENT",
                existing_audio_path=str(audio_path),
                existing_part_path=str(part_path) if _path_present(part_path) else None,
                attempted_download=True,
                effective_url=effective_url,
                response_status=response_status,
                content_type=content_type,
            )
        os.replace(part_path, audio_path)
    except ExternalAudioAcquisitionError as exc:
        return failure(
            exc.reason_code,
            attempted_download=True,
            effective_url=effective_url,
            response_status=response_status,
            content_type=content_type,
        )
    except (OSError, TimeoutError, ValueError) as exc:
        return failure(
            "EXTERNAL_AUDIO_DOWNLOAD_FAILED",
            attempted_download=True,
            error_type=type(exc).__name__,
            effective_url=effective_url,
            response_status=response_status,
            content_type=content_type,
        )
    download_receipt = {
        "schema_version": "nested-external-audio-download-receipt.v1",
        "status": "COMPLETE",
        "label": label,
        "source_uri": route_url,
        "effective_url": effective_url,
        "response_status": response_status,
        "content_type": content_type,
        "bytes": audio_path.stat().st_size,
        "audio_sha256": _prefixed_sha(_sha(audio_path)),
        "source_map_path": str(map_path),
        "source_map_sha256": _prefixed_sha(map_sha),
        "attempts": 1,
    }
    download_sha = atomic_write_json(route_dir / "download.json", download_receipt)
    try:
        _decode_audio(audio_path, route_dir)
    except ExternalAudioAcquisitionError as exc:
        return failure(
            exc.reason_code,
            attempted_download=True,
            download_receipt_path=str(route_dir / "download.json"),
            download_receipt_sha256=_prefixed_sha(download_sha),
            error_type=exc.details.get("error_type"),
        )
    audio_sha = _sha(audio_path)
    record = {
        "schema_version": ACQUISITION_RESULT_SCHEMA,
        "label": label,
        "source_uri": route_url,
        "audio_path": str(audio_path),
        "audio_sha256": _prefixed_sha(audio_sha),
        "source_map_path": str(map_path),
        "source_map_sha256": _prefixed_sha(map_sha),
        "download_receipt_path": str(route_dir / "download.json"),
        "download_receipt_sha256": _prefixed_sha(download_sha),
    }
    atomic_write_json(route_dir / "record.json", record)
    catalog = _catalog_for_record(
        route_dir, map_path=map_path, map_sha=map_sha,
        label=label, url=route_url, audio_path=audio_path,
        audio_sha=audio_sha,
    )
    acquisition = {
        "status": "DOWNLOADED",
        "selection_hint": selection_hint,
        "source_map_path": str(map_path),
        "source_map_sha256": _prefixed_sha(map_sha),
        "download_receipt_path": str(route_dir / "download.json"),
        "download_receipt_sha256": _prefixed_sha(download_sha),
    }
    return {
        "status": "READY",
        "authority": "EVIDENCE_ONLY",
        "label": label,
        "selection_basis": selection_basis,
        "source_role_authority": False,
        "source_map_path": str(map_path),
        "source_map_sha256": _prefixed_sha(map_sha),
        "acquisition": acquisition,
        **catalog,
    }


def prepare_external_audio_reference(
    source_controls: Mapping[str, object], *,
    source_map_path: Path | None,
    cache_root: Path | None,
    cue_text: object = None,
) -> dict[str, object]:
    """Return a resolver-ready catalog, using one exact route at most once."""
    map_path: Path | None = None
    map_sha: str | None = None
    route_dir: Path | None = None
    label: str | None = None
    route_url: str | None = None
    try:
        if source_map_path is None:
            raise ExternalAudioAcquisitionError("EXTERNAL_AUDIO_SOURCE_MAP_UNCONFIGURED")
        if cache_root is None:
            raise ExternalAudioAcquisitionError("EXTERNAL_AUDIO_CACHE_UNCONFIGURED")
        map_path, map_sha, map_data = _source_map(Path(source_map_path))
        cache = Path(cache_root).expanduser().absolute()
        if cache.exists() and cache.is_symlink():
            raise ExternalAudioAcquisitionError("EXTERNAL_AUDIO_CACHE_UNAVAILABLE")
        cache.mkdir(parents=True, exist_ok=True)
        buttons_raw = map_data.get("buttons")
        assert isinstance(buttons_raw, list)
        buttons = [button for button in buttons_raw if isinstance(button, Mapping)]
        label, selection_basis, selection_hint = _button_for_controls(
            source_controls, cue_text=cue_text, buttons=buttons,
        )
        button = next(button for button in buttons if button.get("label") == label)
        route_url = _official_url(
            button.get("url") or button.get("data_audio"),
            code="EXTERNAL_AUDIO_SOURCE_MAP_ROUTE_INVALID",
        )
        map_cache_dir = cache / map_sha
        if map_cache_dir.exists() and map_cache_dir.is_symlink():
            raise ExternalAudioAcquisitionError("EXTERNAL_AUDIO_CACHE_UNAVAILABLE")
        map_cache_dir.mkdir(parents=True, exist_ok=True)
        route_dir = map_cache_dir / _sha_bytes(route_url.encode("utf-8"))
        if route_dir.exists() and route_dir.is_symlink():
            raise ExternalAudioAcquisitionError("EXTERNAL_AUDIO_CACHE_UNAVAILABLE")
        route_dir.mkdir(parents=True, exist_ok=True)
        with exclusive_lock(route_dir / "route.lock", label="external audio route acquisition"):
            return _prepare_route_locked(
                route_dir,
                map_path=map_path,
                map_sha=map_sha,
                label=label,
                route_url=route_url,
                selection_basis=selection_basis,
                selection_hint=selection_hint,
            )
    except ExternalAudioAcquisitionError as exc:
        if map_path is not None and map_sha is not None and route_dir is not None:
            return _failure(
                route_dir, reason_code=exc.reason_code,
                map_path=map_path, map_sha=map_sha,
                label=label, url=route_url, **exc.details,
            )
        return {
            "status": "UNKNOWN_KEEP", "authority": "EVIDENCE_ONLY",
            "reason_code": exc.reason_code, "source_role_authority": False,
            **exc.details,
        }
    except (OSError, TypeError, ValueError) as exc:
        if map_path is not None and map_sha is not None and route_dir is not None:
            return _failure(
                route_dir, reason_code=type(exc).__name__,
                map_path=map_path, map_sha=map_sha,
                label=label, url=route_url,
            )
        return {
            "status": "UNKNOWN_KEEP", "authority": "EVIDENCE_ONLY",
            "reason_code": type(exc).__name__, "source_role_authority": False,
        }


__all__ = [
    "ACQUISITION_RESULT_SCHEMA",
    "CATALOG_SCHEMA",
    "SOURCE_MAP_SCHEMA",
    "ExternalAudioAcquisitionError",
    "prepare_external_audio_reference",
]
