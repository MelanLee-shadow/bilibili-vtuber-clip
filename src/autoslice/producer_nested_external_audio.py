"""Evidence-only discovery of cached audio-button recordings.

The visual observer can establish that a button label is visible.  This module
only joins that pixel fact to an already cached, hash-bound official recording;
it never decides that the button was pressed or that the recording is playing.
"""
from __future__ import annotations

from src.autoslice.evidence_identity import file_sha256 as _evidence_file_sha256
import json
import os
from collections.abc import Mapping
from pathlib import Path


CATALOG_ENV = "AUTOSLICE_NESTED_EXTERNAL_AUDIO_CATALOG"
EXTERNAL_AUDIO_SCENE_SOURCE_ROLE = "NOT_WATCHED_MEDIA_EXTERNAL_AUDIO"
_HEX_SHA256_LENGTH = 64


class ExternalAudioCatalogError(ValueError):
    """A private external-audio catalog cannot be used as evidence."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


def _bare_sha(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.removeprefix("sha256:").lower()
    if len(value) != _HEX_SHA256_LENGTH or any(char not in "0123456789abcdef" for char in value):
        return None
    return value


def _sha(path: Path) -> str:
    digest = _evidence_file_sha256(path)
    return digest


def _same_sha(left: object, right: object) -> bool:
    left_sha, right_sha = _bare_sha(left), _bare_sha(right)
    return left_sha is not None and left_sha == right_sha


def normalize_source_controls(value: object) -> dict[str, object] | None:
    """Validate the small, pixel-only source-control contract."""
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("SOURCE_CONTROLS_INVALID")
    visible = value.get("visible_ui", [])
    if not isinstance(visible, list) or len(visible) > 128:
        raise ValueError("SOURCE_CONTROLS_VISIBLE_UI_INVALID")
    labels: list[str] = []
    for label in visible:
        if not isinstance(label, str) or not label or len(label) > 300:
            raise ValueError("SOURCE_CONTROLS_VISIBLE_UI_INVALID")
        if label not in labels:
            labels.append(label)
    under_cursor = value.get("button_under_cursor")
    if under_cursor is not None and (
        not isinstance(under_cursor, str) or not under_cursor or len(under_cursor) > 300
        or under_cursor not in labels
    ):
        raise ValueError("SOURCE_CONTROLS_CURSOR_LABEL_INVALID")
    site_or_url = value.get("visible_site_or_url")
    if site_or_url is not None and (
        not isinstance(site_or_url, str) or not site_or_url or len(site_or_url) > 2000
    ):
        raise ValueError("SOURCE_CONTROLS_SITE_INVALID")
    return {
        "visible_ui": labels,
        "button_under_cursor": under_cursor,
        "visible_site_or_url": site_or_url,
    }


def _catalog_reference(value: object, *, base: Path, code: str) -> tuple[Path, str | None]:
    if isinstance(value, Mapping):
        raw_path, expected = value.get("path"), value.get("sha256")
    else:
        raw_path, expected = value, None
    if not isinstance(raw_path, str) or not raw_path:
        raise ExternalAudioCatalogError(code)
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = base / path
    try:
        path = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ExternalAudioCatalogError(code) from exc
    if path.is_symlink() or not path.is_file():
        raise ExternalAudioCatalogError(code)
    actual = _sha(path)
    if expected is not None and not _same_sha(expected, actual):
        raise ExternalAudioCatalogError(f"{code}_HASH_MISMATCH")
    return path, actual


def _read_json(path: Path, code: str) -> Mapping[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ExternalAudioCatalogError(code) from exc
    if not isinstance(value, Mapping):
        raise ExternalAudioCatalogError(code)
    return value


def _split_catalog_env(value: object) -> tuple[Path, str | None]:
    if not isinstance(value, str) or not value.strip():
        raise ExternalAudioCatalogError("EXTERNAL_AUDIO_CATALOG_UNCONFIGURED")
    text = value.strip()
    marker = "#sha256:"
    if marker in text:
        path_text, expected = text.rsplit(marker, 1)
        if not path_text or _bare_sha(expected) is None:
            raise ExternalAudioCatalogError("EXTERNAL_AUDIO_CATALOG_REFERENCE_INVALID")
        return Path(path_text).expanduser(), expected
    return Path(text).expanduser(), None


def _load_ref(value: object, *, base: Path, path_code: str, json_code: str) -> tuple[Path, Mapping[str, object]]:
    path, _ = _catalog_reference(value, base=base, code=path_code)
    return path, _read_json(path, json_code)


def _record_values(value: object):
    if isinstance(value, Mapping):
        yield value
        for child in value.values():
            yield from _record_values(child)
    elif isinstance(value, list):
        for child in value:
            yield from _record_values(child)


def _label(record: Mapping[str, object]) -> str | None:
    for key in ("label", "button_label", "name", "title"):
        value = record.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _audio_path(record: Mapping[str, object], *, base: Path) -> tuple[Path, str]:
    path_value = None
    for key in ("cached_audio_path", "audio_path", "private_path", "file_path", "path", "file"):
        candidate = record.get(key)
        if isinstance(candidate, str) and candidate:
            path_value = candidate
            break
    if path_value is None:
        raise ExternalAudioCatalogError("EXTERNAL_AUDIO_AUDIO_PATH_MISSING")
    expected = None
    for key in ("file_sha256", "audio_sha256", "sha256", "source_sha256"):
        if record.get(key) is not None:
            expected = record.get(key)
            break
    if _bare_sha(expected) is None:
        raise ExternalAudioCatalogError("EXTERNAL_AUDIO_AUDIO_HASH_MISSING")
    path, actual = _catalog_reference(path_value, base=base, code="EXTERNAL_AUDIO_AUDIO_UNAVAILABLE")
    if not _same_sha(expected, actual):
        raise ExternalAudioCatalogError("EXTERNAL_AUDIO_AUDIO_HASH_MISMATCH")
    return path, actual


def _source_uri(record: Mapping[str, object], *, fallback: object = None) -> str | None:
    for key in ("source_uri", "url", "data_audio", "source_url"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return fallback.strip() if isinstance(fallback, str) and fallback.strip() else None


def _load_catalog(value: object) -> tuple[
    Path, Mapping[str, object], object | None, Mapping[str, object] | None, Path
]:
    path, expected = _split_catalog_env(value)
    try:
        path = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ExternalAudioCatalogError("EXTERNAL_AUDIO_CATALOG_UNAVAILABLE") from exc
    if path.is_symlink() or not path.is_file():
        raise ExternalAudioCatalogError("EXTERNAL_AUDIO_CATALOG_UNAVAILABLE")
    actual = _sha(path)
    if expected is not None and not _same_sha(expected, actual):
        raise ExternalAudioCatalogError("EXTERNAL_AUDIO_CATALOG_HASH_MISMATCH")
    catalog = _read_json(path, "EXTERNAL_AUDIO_CATALOG_INVALID")
    map_data: Mapping[str, object] = catalog
    audio_data: object | None = None
    html_data: Mapping[str, object] | None = None
    audio_base = path.parent
    map_value = next((catalog.get(key) for key in ("button_map", "live_button_map", "map") if key in catalog), None)
    if map_value is None and catalog.get("source_map_path") is not None:
        map_value = {
            "path": catalog.get("source_map_path"),
            "sha256": catalog.get("source_map_sha256"),
        }
    if isinstance(map_value, Mapping) and "path" not in map_value:
        map_data = map_value
    elif map_value is not None:
        _, map_data = _load_ref(map_value, base=path.parent, path_code="EXTERNAL_AUDIO_MAP_UNAVAILABLE", json_code="EXTERNAL_AUDIO_MAP_INVALID")
    audio_value = next((catalog.get(key) for key in ("audio_result", "audio_index", "result", "audio") if key in catalog), None)
    if audio_value is None and catalog.get("result_path") is not None:
        audio_value = {
            "path": catalog.get("result_path"),
            "sha256": catalog.get("result_sha256"),
        }
    if audio_value is not None:
        if isinstance(audio_value, Mapping) and any(key in audio_value for key in ("path", "sha256")):
            result_path, audio_data = _load_ref(audio_value, base=path.parent, path_code="EXTERNAL_AUDIO_RESULT_UNAVAILABLE", json_code="EXTERNAL_AUDIO_RESULT_INVALID")
            audio_base = result_path.parent
        elif isinstance(audio_value, Mapping):
            audio_data = audio_value
        elif isinstance(audio_value, list):
            audio_data = audio_value
    html_value = next((catalog.get(key) for key in ("source_html", "html") if key in catalog), None)
    if html_value is None and catalog.get("source_html_path") is not None:
        html_value = {
            "path": catalog.get("source_html_path"),
            "sha256": catalog.get("source_html_sha256"),
        }
    if html_value is not None:
        if isinstance(html_value, Mapping) and any(key in html_value for key in ("path", "sha256")):
            html_path, html_sha = _catalog_reference(
                html_value, base=path.parent, code="EXTERNAL_AUDIO_HTML_UNAVAILABLE"
            )
            html_data = {"path": str(html_path), "sha256": "sha256:" + html_sha}
        elif isinstance(html_value, str):
            html_path, html_sha = _catalog_reference(
                html_value, base=path.parent, code="EXTERNAL_AUDIO_HTML_UNAVAILABLE"
            )
            html_data = {"path": str(html_path), "sha256": "sha256:" + html_sha}
        elif isinstance(html_value, Mapping):
            html_data = html_value
    # Direct LIVE-BUTTON-MAP files carry a page hash.  A wrapper can also pin
    # the page hash through its source_html reference; no URL is trusted alone.
    if html_data is not None and isinstance(html_data.get("path"), str):
        _catalog_reference(html_data, base=path.parent, code="EXTERNAL_AUDIO_HTML_UNAVAILABLE")
    return path, map_data, audio_data, html_data, audio_base


def resolve_external_audio_reference(
    source_controls: Mapping[str, object], *, catalog_value: object = None,
    cue_text: object = None, unaligned_visible_reference: bool = False,
) -> dict[str, object]:
    """Find one cached recording; cue text and cursor are only search hints."""
    try:
        controls = normalize_source_controls(source_controls)
        if controls is None:
            raise ExternalAudioCatalogError("EXTERNAL_AUDIO_SOURCE_CONTROLS_MISSING")
        visible = controls.get("visible_ui")
        label = controls.get("button_under_cursor")
        selection_basis = "CURSOR_LABEL"
        # A cursor may move while a previously selected recording is playing.
        # An exact current-cue/visible-label match can shortlist that recording,
        # but cannot establish either the utterance or the speaker's identity.
        if isinstance(cue_text, str) and isinstance(visible, list) and cue_text.strip() in visible:
            label = cue_text.strip()
            selection_basis = "CURRENT_CUE_EXACT_VISIBLE_LABEL"
        missing_label = not isinstance(label, str) or not isinstance(visible, list) or label not in visible
        if missing_label and not unaligned_visible_reference:
            raise ExternalAudioCatalogError("EXTERNAL_AUDIO_CURSOR_LABEL_UNAVAILABLE")
        configured = catalog_value if catalog_value is not None else os.environ.get(CATALOG_ENV)
        catalog_path, map_data, audio_data, _html_data, audio_base = _load_catalog(configured)
        map_source_url = map_data.get("source_url")
        audio_records = list(_record_values(audio_data)) if audio_data is not None else []
        audio_records.extend(
            record for record in _record_values(map_data.get("audio", []))
            if record not in audio_records
        )
        if missing_label:
            # A visible, independently cached recording can supply a voice
            # sample even when no button explains the current utterance.  This
            # is deliberately independent of cue text and playback.  A byte or
            # URL failure below remains a rejection, not a reason to try another
            # recording until one passes.
            cached_labels = {_label(record) for record in audio_records}
            mapped_labels = {_label(record) for record in _record_values(map_data.get("buttons", []))}
            eligible = sorted(set(visible or []) & cached_labels & mapped_labels)
            if not eligible:
                raise ExternalAudioCatalogError("EXTERNAL_AUDIO_VISIBLE_REFERENCE_UNAVAILABLE")
            label = eligible[0]
            selection_basis = "CURRENT_VISIBLE_LABEL_UNALIGNED_VOICE_REFERENCE"
        buttons = [record for record in _record_values(map_data.get("buttons", [])) if _label(record) == label]
        if len(buttons) != 1:
            raise ExternalAudioCatalogError("EXTERNAL_AUDIO_BUTTON_LABEL_UNAVAILABLE")
        button = buttons[0]
        button_audio = _source_uri(button)
        label_matches = [record for record in audio_records if _label(record) == label]
        if button_audio:
            if any(
                (record_audio := _source_uri(record)) is not None
                and record_audio != button_audio
                for record in label_matches
            ):
                raise ExternalAudioCatalogError("EXTERNAL_AUDIO_CACHED_URL_MISMATCH")
            matches = [
                record for record in audio_records
                if _source_uri(record) == button_audio
            ]
        else:
            matches = label_matches
        if len(matches) != 1:
            raise ExternalAudioCatalogError("EXTERNAL_AUDIO_CACHED_RECORD_UNAVAILABLE")
        audio_path, audio_sha = _audio_path(matches[0], base=audio_base)
        source_uri = _source_uri(button, fallback=_source_uri(matches[0], fallback=map_source_url))
        if source_uri is None:
            raise ExternalAudioCatalogError("EXTERNAL_AUDIO_SOURCE_URI_MISSING")
        return {
            "status": "READY",
            "authority": "EVIDENCE_ONLY",
            "label": label,
            "selection_basis": selection_basis,
            **({"reference_only": True} if missing_label else {}),
            "source_media_path": str(audio_path),
            "source_media_sha256": "sha256:" + audio_sha,
            "source_cache_binding": {
                "kind": "OFFICIAL_EXTERNAL_AUDIO",
                "source_uri": source_uri,
                "source_sha256": "sha256:" + audio_sha,
            },
            "source_controls": controls,
            "catalog_path": str(catalog_path),
            "catalog_sha256": "sha256:" + _sha(catalog_path),
            "audio_playback_proven": False,
            "source_identity_proven": False,
            "player_identity_proven": False,
            "source_role_authority": False,
        }
    except ExternalAudioCatalogError as exc:
        return {"status": "UNKNOWN_KEEP", "authority": "EVIDENCE_ONLY", "reason_code": exc.reason_code}
    except (OSError, TypeError, ValueError) as exc:
        return {"status": "UNKNOWN_KEEP", "authority": "EVIDENCE_ONLY", "reason_code": type(exc).__name__}
