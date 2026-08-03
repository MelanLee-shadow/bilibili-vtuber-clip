"""Source-backed PSPLive roster crawler and prompt-safe snapshot validation.

The roster is occurrence-neutral entity data.  Being listed proves only that a
name/alias exists; it never proves that a particular subtitle cue contains it.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping


SOURCE_SCHEMA = "lidousha-psplive-roster-sources.v1"
SNAPSHOT_SCHEMA = "lidousha-psplive-roster.v1"
_SAFE_NAME = re.compile(r"^[0-9A-Za-z\u3400-\u9fff _&.-]{1,64}$")


class PspliveRosterError(ValueError):
    pass


def _safe_name(value: object, *, label: str) -> str:
    if not isinstance(value, str):
        raise PspliveRosterError(f"{label} must be a string")
    text = value.strip()
    if not _SAFE_NAME.fullmatch(text) or any(token in text.lower() for token in ("ignore ", "system prompt")):
        raise PspliveRosterError(f"{label} is not a safe entity atom")
    return text


def load_source_config(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PspliveRosterError(f"cannot load roster source config: {exc}") from exc
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version", "sources", "member_overrides"
    }:
        raise PspliveRosterError("roster source config has unknown or missing fields")
    if payload["schema_version"] != SOURCE_SCHEMA:
        raise PspliveRosterError("unsupported roster source schema")
    sources = payload["sources"]
    overrides = payload["member_overrides"]
    if not isinstance(sources, list) or not sources or not isinstance(overrides, dict):
        raise PspliveRosterError("roster sources/overrides have invalid shape")
    return payload


def _participants_from_description(description: object, marker: str) -> list[str]:
    if not isinstance(description, str):
        raise PspliveRosterError("official video description is missing")
    lines = [line.strip() for line in description.splitlines() if line.strip()]
    try:
        marker_index = next(index for index, line in enumerate(lines) if marker in line)
    except StopIteration as exc:
        raise PspliveRosterError("participant marker missing from official description") from exc
    participant_text = "、".join(lines[marker_index + 1 :])
    values = [_safe_name(value, label="official participant") for value in participant_text.split("、") if value.strip()]
    if len(values) != len(set(values)):
        raise PspliveRosterError("official participant list contains duplicates")
    return values


def build_snapshot(
    *,
    api_payloads: list[Mapping[str, Any]],
    config: Mapping[str, Any],
    generated_at: dt.datetime,
) -> dict[str, Any]:
    if generated_at.tzinfo is None or generated_at.utcoffset() is None:
        raise PspliveRosterError("generated_at must be timezone-aware")
    configured_sources = config["sources"]
    if len(api_payloads) != len(configured_sources):
        raise PspliveRosterError("source response count mismatch")
    seen_raw: set[str] = set()
    provenance: list[dict[str, Any]] = []
    for source, payload in zip(configured_sources, api_payloads):
        try:
            data = payload["data"]
            owner = data["owner"]
        except (KeyError, TypeError) as exc:
            raise PspliveRosterError("Bilibili roster response is malformed") from exc
        if payload.get("code") != 0:
            raise PspliveRosterError("Bilibili roster response is not successful")
        if int(owner.get("mid", 0)) != int(source["owner_mid"]) or owner.get("name") != source["owner_name"]:
            raise PspliveRosterError("official roster owner identity mismatch")
        title = str(data.get("title") or "")
        if str(source["title_contains"]) not in title:
            raise PspliveRosterError("official roster title anchor mismatch")
        participants = _participants_from_description(data.get("desc"), str(source["participant_marker"]))
        if len(participants) < int(source["minimum_participants"]):
            raise PspliveRosterError("official roster participant list is unexpectedly short")
        seen_raw.update(participants)
        provenance.append(
            {
                "url": str(source["public_url"]),
                "owner_mid": int(source["owner_mid"]),
                "owner_name": str(source["owner_name"]),
                "title": title[:120],
                "participant_count": len(participants),
            }
        )
    overrides = config["member_overrides"]
    missing = sorted(set(overrides) - seen_raw)
    if missing:
        raise PspliveRosterError(f"configured roster members missing from official source: {missing}")
    members: list[dict[str, Any]] = []
    for raw in sorted(seen_raw):
        override = overrides.get(raw)
        if not isinstance(override, dict):
            # New official names remain visible instead of silently disappearing.
            members.append({"canonical": raw, "aliases": [], "official_surface": raw})
            continue
        canonical = _safe_name(override.get("canonical"), label=f"{raw}.canonical")
        aliases = [
            _safe_name(value, label=f"{raw}.alias")
            for value in override.get("aliases", [])
        ]
        members.append(
            {
                "canonical": canonical,
                "aliases": sorted(set(aliases + [raw]) - {canonical}, key=str.casefold),
                "official_surface": raw,
            }
        )
    snapshot = {
        "schema_version": SNAPSHOT_SCHEMA,
        "generated_at": generated_at.isoformat(timespec="seconds"),
        "expires_at": (generated_at + dt.timedelta(days=8)).isoformat(timespec="seconds"),
        "status": "fresh",
        "authority": "official_bilibili_roster_crawler",
        "occurrence_policy": "ENTITY_EXISTENCE_ONLY_NOT_CUE_OCCURRENCE_EVIDENCE",
        "sources": provenance,
        "members": sorted(members, key=lambda row: (row["canonical"].casefold(), row["canonical"])),
    }
    return validate_snapshot(snapshot)


def validate_snapshot(payload: object, *, as_of: dt.datetime | None = None) -> dict[str, Any]:
    if not isinstance(payload, dict) or payload.get("schema_version") != SNAPSHOT_SCHEMA:
        raise PspliveRosterError("unsupported PSPLive roster snapshot")
    if payload.get("occurrence_policy") != "ENTITY_EXISTENCE_ONLY_NOT_CUE_OCCURRENCE_EVIDENCE":
        raise PspliveRosterError("roster occurrence-neutral policy is missing")
    members = payload.get("members")
    if not isinstance(members, list) or not 20 <= len(members) <= 128:
        raise PspliveRosterError("roster member count is invalid")
    normalized: list[dict[str, Any]] = []
    canonicals: set[str] = set()
    for index, row in enumerate(members):
        if not isinstance(row, dict) or set(row) != {"canonical", "aliases", "official_surface"}:
            raise PspliveRosterError(f"member {index} has invalid fields")
        canonical = _safe_name(row["canonical"], label=f"member {index}.canonical")
        aliases_raw = row["aliases"]
        if not isinstance(aliases_raw, list) or len(aliases_raw) > 16:
            raise PspliveRosterError(f"member {index}.aliases is invalid")
        aliases = [_safe_name(value, label=f"member {index}.alias") for value in aliases_raw]
        key = canonical.casefold()
        if key in canonicals:
            raise PspliveRosterError("duplicate roster canonical")
        canonicals.add(key)
        normalized.append(
            {
                "canonical": canonical,
                "aliases": list(dict.fromkeys(aliases)),
                "official_surface": _safe_name(row["official_surface"], label=f"member {index}.official_surface"),
            }
        )
    if as_of is not None:
        expires_at = dt.datetime.fromisoformat(str(payload.get("expires_at")))
        if expires_at.tzinfo is None or as_of > expires_at:
            raise PspliveRosterError("PSPLive roster snapshot is expired")
    result = dict(payload)
    result["members"] = normalized
    return result


def snapshot_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def snapshot_sha256(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(snapshot_json(payload).encode("utf-8")).hexdigest()
