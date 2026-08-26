"""Fail-closed, candidate-private revival input for Ivan's C7b ruling."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[2]
_CID = "auto_130040_201_255"
_DATE = "2026-08-14"
_DIR = _ROOT / "assets/lidousha/fastlane_c7b_private"
_AUTHORITY = _DIR / f"{_CID}.private-authority.v1.json"
_CLOSURE = _DIR / f"{_CID}.freeze-closure.v1.json"
_SRT = _DIR / f"{_CID}.reviewed.srt"
_PREDECESSOR = _DIR / "predecessor" / f"{_CID}.recut.srt"
_SRT_SHA = "e07e2e23d2eacebbeb95a4700c7fa4005fb4342a432c28d0adde1d4e68e98457"
_TITLE = "李姐也是脑控大师，但即使被脑控仍然信不了李1是怎么回事呢"


class C7bPrivateAuthorityError(ValueError):
    """The isolated C7b source ruling cannot be replayed safely."""


def _bytes(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise C7bPrivateAuthorityError("C7B_PRIVATE_REGULAR_FILE_REQUIRED")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise C7bPrivateAuthorityError("C7B_PRIVATE_AUTHORITY_UNAVAILABLE") from exc


def _read(path: Path) -> dict[str, object]:
    try:
        value = json.loads(_bytes(path).decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise C7bPrivateAuthorityError("C7B_PRIVATE_AUTHORITY_UNAVAILABLE") from exc
    if not isinstance(value, dict):
        raise C7bPrivateAuthorityError("C7B_PRIVATE_AUTHORITY_INVALID")
    return value


def build_private_revival() -> dict[str, object]:
    """Return exact C7b text/title inputs, never a delivery or upload request."""

    authority, closure = _read(_AUTHORITY), _read(_CLOSURE)
    try:
        srt_bytes, predecessor_bytes = _bytes(_SRT), _bytes(_PREDECESSOR)
    except C7bPrivateAuthorityError:
        raise
    if not (
        authority.get("candidate_id") == closure.get("candidate_id") == _CID
        and authority.get("recording_date") == _DATE
        and authority.get("title") == _TITLE
        and authority.get("reviewed_subtitle", {}).get("sha256") == _SRT_SHA
        and hashlib.sha256(srt_bytes).hexdigest() == _SRT_SHA
        and closure.get("reviewed_srt_sha256") == _SRT_SHA
        and closure.get("changed_ordinals") == [10, 18, 19, 20]
        and closure.get("frozen_ordinal_count") == 32
        and closure.get("operator_drop_count") == 0
        and hashlib.sha256(predecessor_bytes).hexdigest() == "810a0f17e658184029543910dc8e1c22e1166f03edf7ca90afb82943b761bd94"
    ):
        raise C7bPrivateAuthorityError("C7B_PRIVATE_BINDING_DRIFT")
    return {
        "schema_version": "fastlane-c7b-private-revival-input.v1",
        "candidate_id": _CID,
        "recording_date": _DATE,
        "reviewed_srt": {
            "path": _SRT.relative_to(_ROOT).as_posix(),
            "sha256": "sha256:" + _SRT_SHA,
            "bytes": srt_bytes,
            "changed_ordinals": [10, 18, 19, 20],
            "frozen_ordinal_count": 32,
        },
        "title": _TITLE,
        "content_boundary": {
            "status": "REVIVED_CANDIDATE_PRIVATE_ONLY",
            "basis": "IVAN_LINE947_ROW_7B_SUPERSEDES_HISTORICAL_REJECTION",
            "boundary_change": False,
        },
        "cover": {"mode": "CARRY_EXISTING_LIVE_COVER_ONLY", "sha256": "sha256:df3df8ae3902d7d0118c08f7d1c8aee0d577ed2cd12d4a87e62c747421b2a96f"},
        "capabilities": {key: False for key in ("provider", "state", "ssh", "deploy", "upload")},
    }


def apply_replay_carry(spec: dict[str, object], *, candidate_id: str, recording_date: str, baseline_sha256: str) -> dict[str, object]:
    """Add C7b's sealed title/cover carry, leaving every other replay unchanged."""

    if (candidate_id, recording_date) != (_CID, _DATE):
        return spec
    revival = build_private_revival()
    if baseline_sha256.removeprefix("sha256:") != _SRT_SHA:
        raise C7bPrivateAuthorityError("C7B_PRIVATE_BASELINE_DRIFT")
    result = dict(spec)
    if result.get("given_title") is not None or result.get("published_cover_carry") is not None:
        raise C7bPrivateAuthorityError("C7B_PRIVATE_REPLAY_SURFACE_DRIFT")
    result["given_title"] = revival["title"]
    result["published_cover_carry"] = revival["cover"]
    return result
