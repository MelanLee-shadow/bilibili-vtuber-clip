"""Organization-neutral, occurrence-neutral streamer registry crawler.

The registry answers only "which official entity/name exists?".  It is a
stable identity anchor for the separate daily community-name crawler; it is
never cue-occurrence evidence and never authorizes a subtitle rewrite.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import unicodedata
from pathlib import Path
from typing import Any, Mapping


SOURCE_SCHEMA = "vtuber-slice.streamer-registry-sources.v1"
SNAPSHOT_SCHEMA = "vtuber-slice.streamer-registry.v1"
OCCURRENCE_POLICY = "ENTITY_EXISTENCE_ONLY_NOT_CUE_OCCURRENCE_EVIDENCE"
_SPACE_URL_RX = re.compile(
    r"(?P<name>[^\s：:\n|]{1,64})[：:]\s*https?://space\.bilibili\.com/(?P<mid>[0-9]{1,32})"
)
_HTML_RX = re.compile(r"<[^>]{1,256}>")
_ASCII_SUFFIX_RX = re.compile(r"^(.+?)([A-Za-z][A-Za-z0-9_. -]{0,31})$")


class StreamerRegistryError(ValueError):
    pass


def _safe_atom(value: object, *, label: str, max_chars: int = 64) -> str:
    if not isinstance(value, str):
        raise StreamerRegistryError(f"{label} must be a string")
    text = unicodedata.normalize("NFKC", value).strip()
    if not text or len(text) > max_chars or "\n" in text or "\r" in text:
        raise StreamerRegistryError(f"{label} is not a bounded entity atom")
    lowered = text.casefold()
    if any(token in lowered for token in ("ignore ", "system prompt", "assistant:")):
        raise StreamerRegistryError(f"{label} is instruction-shaped")
    for char in text:
        if char in " _-&·.':":
            continue
        if unicodedata.category(char)[0] not in {"L", "N"}:
            raise StreamerRegistryError(f"{label} contains an unsafe character")
    return text


def _iso_timestamp(value: object, *, label: str) -> str:
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise StreamerRegistryError(f"{label} is not an ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise StreamerRegistryError(f"{label} must be timezone-aware")
    return parsed.isoformat(timespec="seconds")


def load_source_config(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StreamerRegistryError(f"cannot load streamer registry config: {exc}") from exc
    required = {"schema_version", "sources"}
    if (not isinstance(payload, dict) or not required <= set(payload)
            or set(payload) - required - {"reviewed_accounts"}):
        raise StreamerRegistryError("streamer registry config has unknown or missing fields")
    if payload.get("schema_version") != SOURCE_SCHEMA:
        raise StreamerRegistryError("unsupported streamer registry source schema")
    sources = payload.get("sources")
    if not isinstance(sources, list) or not sources:
        raise StreamerRegistryError("streamer registry sources are empty")
    source_ids: set[str] = set()
    for index, source in enumerate(sources):
        if not isinstance(source, dict):
            raise StreamerRegistryError(f"source {index} is not an object")
        source_id = _safe_atom(source.get("source_id"), label=f"source {index}.source_id")
        if source_id in source_ids:
            raise StreamerRegistryError("duplicate streamer registry source_id")
        source_ids.add(source_id)
        if source.get("kind") not in {
            "psplive_bilibili_video",
            "virtuareal_member_api",
            "virtuareal_bilibili_announcements",
        }:
            raise StreamerRegistryError(f"source {source_id} has unsupported kind")
    reviewed_account_records(payload)
    return payload


def reviewed_account_records(
    config: Mapping[str, Any], *, as_of: dt.datetime | None = None
) -> list[dict[str, Any]]:
    """Read bounded operator-reviewed facts without declaring a crawler refresh.

    This supplement is independent of the complete registry and its TTL/count
    gates. Recorded reactions are historical context, never live presence,
    community aliases, cue-occurrence evidence, or subtitle mutation authority.
    """
    records = config.get("reviewed_accounts", [])
    if not isinstance(records, list) or len(records) > 16:
        raise StreamerRegistryError("reviewed_accounts must contain at most 16 records")
    cutoff = as_of or dt.datetime.now(dt.timezone.utc)
    if cutoff.tzinfo is None or cutoff.utcoffset() is None:
        raise StreamerRegistryError("reviewed account as_of must be timezone-aware")
    result: list[dict[str, Any]] = []
    seen_mids: set[int] = set()
    for row in records:
        fields = {"canonical", "account_name", "official_mid", "homepage", "affiliation",
                  "verified_at", "profile_evidence", "historical_events"}
        if not isinstance(row, dict) or set(row) != fields:
            raise StreamerRegistryError("reviewed account has invalid fields")
        for key in ("canonical", "account_name", "affiliation"):
            _safe_atom(row[key], label=f"reviewed account {key}")
        mid = row["official_mid"]
        if type(mid) is not int or mid <= 0 or mid in seen_mids:
            raise StreamerRegistryError("reviewed account MID is invalid or duplicate")
        seen_mids.add(mid)
        if row["homepage"] != f"https://space.bilibili.com/{mid}":
            raise StreamerRegistryError("reviewed account homepage/MID mismatch")
        verified_at = dt.datetime.fromisoformat(
            _iso_timestamp(row["verified_at"], label="reviewed account verified_at")
        )
        evidence = row["profile_evidence"]
        if (not isinstance(evidence, dict)
                or set(evidence) != {"source_bvid", "page_state_sha256", "affiliation_excerpt"}
                or re.fullmatch(r"BV[0-9A-Za-z]{10}", str(evidence["source_bvid"])) is None
                or re.fullmatch(r"[0-9a-f]{64}", str(evidence["page_state_sha256"])) is None):
            raise StreamerRegistryError("reviewed account profile evidence is invalid")
        _safe_atom(evidence["affiliation_excerpt"], label="profile affiliation excerpt")
        events = row["historical_events"]
        if not isinstance(events, list) or not events or len(events) > 8:
            raise StreamerRegistryError("reviewed account historical events are invalid")
        event_ids: set[str] = set()
        eligible_events: list[dict[str, Any]] = []
        for event in events:
            event_fields = {"event_id", "relationship", "not_a_live_collaboration",
                            "recording_date", "source_bvid", "start_ms", "end_ms",
                            "reference_bvid", "reference_subject", "source_info_sha256"}
            if not isinstance(event, dict) or set(event) != event_fields:
                raise StreamerRegistryError("reviewed event has invalid fields")
            event_id = _safe_atom(event["event_id"], label="reviewed event id")
            if event_id in event_ids:
                raise StreamerRegistryError("duplicate reviewed event id")
            event_ids.add(event_id)
            _safe_atom(event["reference_subject"], label="reviewed event reference subject")
            if (event["relationship"] != "watching_and_commenting_on_recorded_clips"
                    or event["not_a_live_collaboration"] is not True):
                raise StreamerRegistryError("reviewed reaction cannot assert live collaboration")
            for key in ("source_bvid", "reference_bvid"):
                if re.fullmatch(r"BV[0-9A-Za-z]{10}", str(event[key])) is None:
                    raise StreamerRegistryError("reviewed event BVID is invalid")
            if re.fullmatch(r"[0-9a-f]{64}", str(event["source_info_sha256"])) is None:
                raise StreamerRegistryError("reviewed event source hash is invalid")
            if (type(event["start_ms"]) is not int or type(event["end_ms"]) is not int
                    or not 0 <= event["start_ms"] < event["end_ms"]):
                raise StreamerRegistryError("reviewed event interval is invalid")
            try:
                recording_date = dt.date.fromisoformat(event["recording_date"])
            except (TypeError, ValueError) as exc:
                raise StreamerRegistryError("reviewed event recording date is invalid") from exc
            if recording_date > verified_at.date():
                raise StreamerRegistryError("reviewed event postdates its verification")
            if recording_date <= cutoff.date():
                eligible_events.append(dict(event))
        # as_of is the production recording date, not the research date. An
        # eligible source replay also anchors this account's existence then;
        # a later profile review must not hide that historical evidence.
        if not eligible_events:
            continue
        result.append({
            "entity_id": f"bilibili:{mid}",
            "canonical": row["canonical"],
            "account_name": row["account_name"],
            "homepage": row["homepage"],
            "affiliation_as_observed": row["affiliation"],
            "verified_at": verified_at.isoformat(),
            "profile_evidence": dict(evidence),
            "historical_events": eligible_events,
        })
    return result


def reviewed_accounts_context(path: Path, *, as_of: dt.datetime | None = None) -> str:
    """Render reviewed source records for the existing glossary consumer."""
    records = reviewed_account_records(load_source_config(path), as_of=as_of)
    if not records:
        return ""
    lines = [
        "已核实主播资料与历史观看事件（人工核实的来源补充，非完整名单爬虫刷新；",
        "账号与事件按源录播日期提供；所属仅为核实日期观察到的账号简介，",
        "不回溯断言录播时所属或出道日期；历史观看/评论录播切片不代表实时联动。",
        "只供实体及话题候选，不证明当前句出现，也不授权改字或声称在场）:",
    ]
    lines.extend("- " + json.dumps(row, ensure_ascii=False, sort_keys=True) for row in records)
    return "\n".join(lines) + "\n"


def _source_by_kind(config: Mapping[str, Any], kind: str) -> Mapping[str, Any]:
    matches = [item for item in config["sources"] if item.get("kind") == kind]
    if len(matches) != 1:
        raise StreamerRegistryError(f"expected exactly one {kind} source")
    return matches[0]


def _surface_parts(surface: str) -> tuple[str, ...]:
    match = _ASCII_SUFFIX_RX.fullmatch(surface)
    if not match:
        return (surface,)
    left = match.group(1).strip(" _-")
    right = match.group(2).strip(" _-")
    return tuple(item for item in (surface, left, right) if item)


def _member_key(*, organization: str, surface: str, official_mid: int | None) -> str:
    if official_mid is not None:
        return f"bilibili:{official_mid}"
    digest = hashlib.sha256(
        f"{organization}\0{unicodedata.normalize('NFKC', surface).casefold()}".encode("utf-8")
    ).hexdigest()[:20]
    return f"{organization}:surface:{digest}"


def _add_member(
    members: dict[str, dict[str, Any]],
    *,
    organization: str,
    canonical: str,
    surfaces: list[str],
    aliases: list[str],
    official_mid: int | None,
    source: Mapping[str, Any],
) -> None:
    canonical = _safe_atom(canonical, label="member canonical")
    cleaned_surfaces = {
        _safe_atom(item, label=f"{canonical}.official_surface") for item in surfaces if item
    }
    cleaned_surfaces.add(canonical)
    cleaned_aliases = {
        _safe_atom(item, label=f"{canonical}.alias") for item in aliases if item
    }
    key = _member_key(
        organization=organization,
        surface=canonical,
        official_mid=official_mid,
    )
    row = members.setdefault(
        key,
        {
            "entity_id": key,
            "canonical": canonical,
            "official_surfaces": set(),
            "aliases": set(),
            "official_mid": official_mid,
            "affiliations": set(),
            "official_sources": {},
        },
    )
    # A Bilibili MID is the identity authority.  Prefer the more descriptive
    # official surface if a later announcement supplies it.
    if len(canonical) > len(row["canonical"]):
        row["canonical"] = canonical
    row["official_surfaces"].update(cleaned_surfaces)
    row["aliases"].update(cleaned_aliases)
    row["affiliations"].add(organization)
    source_url = str(source["url"])
    row["official_sources"][source_url] = dict(source)


def _parse_psplive(
    *,
    payload: Mapping[str, Any],
    source: Mapping[str, Any],
    legacy_config: Mapping[str, Any],
    members: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    from src.autoslice.psplive_roster_crawler import build_snapshot

    generated_at = dt.datetime(2000, 1, 1, tzinfo=dt.timezone.utc)
    snapshot = build_snapshot(
        api_payloads=[payload], config=legacy_config, generated_at=generated_at
    )
    provenance = snapshot["sources"][0]
    public_url = str(provenance["url"])
    for row in snapshot["members"]:
        _add_member(
            members,
            organization=str(source["organization_id"]),
            canonical=str(row["canonical"]),
            surfaces=[str(row["official_surface"])],
            aliases=list(row["aliases"]),
            official_mid=None,
            source={
                "source_id": str(source["source_id"]),
                "kind": str(source["kind"]),
                "url": public_url,
                "title": str(provenance["title"]),
            },
        )
    return {
        "source_id": str(source["source_id"]),
        "kind": str(source["kind"]),
        "url": public_url,
        "member_count": len(snapshot["members"]),
    }


def _parse_virtuareal_api(
    *,
    payload: Mapping[str, Any],
    source: Mapping[str, Any],
    members: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    rows = payload.get("content")
    if not isinstance(rows, list) or len(rows) < int(source["minimum_members"]):
        raise StreamerRegistryError("VirtuaReal member API returned an unexpectedly short list")
    accepted = 0
    for index, item in enumerate(rows):
        if not isinstance(item, Mapping):
            raise StreamerRegistryError(f"VirtuaReal member {index} is malformed")
        name_cn = _safe_atom(item.get("nameCn"), label=f"VirtuaReal member {index}.nameCn")
        name_en_raw = item.get("nameEn")
        name_en = _safe_atom(name_en_raw, label=f"VirtuaReal member {index}.nameEn") if name_en_raw else ""
        try:
            official_mid = int(str(item.get("bilibiliUID")))
        except ValueError as exc:
            raise StreamerRegistryError(f"VirtuaReal member {index} has invalid bilibiliUID") from exc
        project = _safe_atom(item.get("project"), label=f"VirtuaReal member {index}.project")
        full_surface = f"{name_cn}{name_en}" if name_en else name_cn
        _add_member(
            members,
            organization=str(source["organization_id"]),
            canonical=full_surface,
            surfaces=[name_cn, name_en, full_surface],
            aliases=[],
            official_mid=official_mid,
            source={
                "source_id": str(source["source_id"]),
                "kind": str(source["kind"]),
                "url": str(source["public_url"]),
                "project": project,
            },
        )
        accepted += 1
    return {
        "source_id": str(source["source_id"]),
        "kind": str(source["kind"]),
        "url": str(source["public_url"]),
        "member_count": accepted,
    }


def _parse_virtuareal_announcements(
    *,
    payload: Mapping[str, Any],
    source: Mapping[str, Any],
    generated_at: dt.datetime,
    members: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    if payload.get("code") != 0:
        raise StreamerRegistryError("VirtuaReal announcement search was not successful")
    rows = payload.get("data", {}).get("result")
    if not isinstance(rows, list):
        raise StreamerRegistryError("VirtuaReal announcement search result is malformed")
    owner_mid = int(source["owner_mid"])
    title_anchor = str(source["title_contains"])
    announcement_count = 0
    found_mids: set[int] = set()
    for row in rows:
        if (
            not isinstance(row, Mapping)
            or int(row.get("mid") or 0) != owner_mid
            or row.get("author") != source["owner_name"]
        ):
            continue
        title = _HTML_RX.sub("", str(row.get("title") or ""))
        if title_anchor not in title:
            continue
        try:
            published_at = dt.datetime.fromtimestamp(int(row.get("pubdate") or 0), dt.timezone.utc)
        except (OSError, OverflowError, ValueError):
            continue
        if published_at > generated_at.astimezone(dt.timezone.utc):
            continue
        bvid = str(row.get("bvid") or "")
        if not re.fullmatch(r"BV[0-9A-Za-z]{10}", bvid):
            continue
        announcement_count += 1
        public_url = f"https://www.bilibili.com/video/{bvid}/"
        for match in _SPACE_URL_RX.finditer(str(row.get("description") or "")):
            surface = _safe_atom(match.group("name"), label="VirtuaReal announcement member")
            official_mid = int(match.group("mid"))
            found_mids.add(official_mid)
            parts = _surface_parts(surface)
            _add_member(
                members,
                organization=str(source["organization_id"]),
                canonical=surface,
                surfaces=list(parts),
                aliases=[],
                official_mid=official_mid,
                source={
                    "source_id": str(source["source_id"]),
                    "kind": str(source["kind"]),
                    "url": public_url,
                    "title": title[:120],
                    "published_at": published_at.isoformat(timespec="seconds"),
                },
            )
    if announcement_count < int(source["minimum_announcements"]):
        raise StreamerRegistryError("too few official VirtuaReal announcements were verified")
    if len(found_mids) < int(source["minimum_members"]):
        raise StreamerRegistryError("too few VirtuaReal announcement members were parsed")
    return {
        "source_id": str(source["source_id"]),
        "kind": str(source["kind"]),
        "url": str(source["public_url"]),
        "announcement_count": announcement_count,
        "member_count": len(found_mids),
    }


def build_snapshot(
    *,
    payloads: Mapping[str, Mapping[str, Any]],
    config: Mapping[str, Any],
    legacy_psplive_config: Mapping[str, Any],
    generated_at: dt.datetime,
) -> dict[str, Any]:
    if generated_at.tzinfo is None or generated_at.utcoffset() is None:
        raise StreamerRegistryError("generated_at must be timezone-aware")
    members: dict[str, dict[str, Any]] = {}
    provenance: list[dict[str, Any]] = []
    for source in config["sources"]:
        source_id = str(source["source_id"])
        payload = payloads.get(source_id)
        if not isinstance(payload, Mapping):
            raise StreamerRegistryError(f"missing payload for {source_id}")
        kind = source["kind"]
        if kind == "psplive_bilibili_video":
            provenance.append(
                _parse_psplive(
                    payload=payload,
                    source=source,
                    legacy_config=legacy_psplive_config,
                    members=members,
                )
            )
        elif kind == "virtuareal_member_api":
            provenance.append(_parse_virtuareal_api(payload=payload, source=source, members=members))
        elif kind == "virtuareal_bilibili_announcements":
            provenance.append(
                _parse_virtuareal_announcements(
                    payload=payload,
                    source=source,
                    generated_at=generated_at,
                    members=members,
                )
            )
    rendered_members: list[dict[str, Any]] = []
    for row in members.values():
        canonical = row["canonical"]
        official_surfaces = sorted(
            row["official_surfaces"], key=lambda item: (item.casefold(), item)
        )
        aliases = sorted(
            (row["aliases"] - set(official_surfaces) - {canonical}),
            key=lambda item: (item.casefold(), item),
        )
        rendered_members.append(
            {
                "entity_id": row["entity_id"],
                "canonical": canonical,
                "official_surfaces": official_surfaces,
                "aliases": aliases,
                "official_mid": row["official_mid"],
                "affiliations": sorted(row["affiliations"]),
                "official_sources": sorted(
                    row["official_sources"].values(), key=lambda item: item["url"]
                )[:8],
            }
        )
    snapshot = {
        "schema_version": SNAPSHOT_SCHEMA,
        "generated_at": generated_at.isoformat(timespec="seconds"),
        "expires_at": (generated_at + dt.timedelta(days=35)).isoformat(timespec="seconds"),
        "status": "fresh",
        "authority": "official_organization_sources",
        "occurrence_policy": OCCURRENCE_POLICY,
        "sources": provenance,
        "members": sorted(
            rendered_members,
            key=lambda row: (row["canonical"].casefold(), row["entity_id"]),
        ),
    }
    return validate_snapshot(snapshot)


def validate_snapshot(payload: object, *, as_of: dt.datetime | None = None) -> dict[str, Any]:
    if not isinstance(payload, dict) or payload.get("schema_version") != SNAPSHOT_SCHEMA:
        raise StreamerRegistryError("unsupported streamer registry snapshot")
    if payload.get("occurrence_policy") != OCCURRENCE_POLICY:
        raise StreamerRegistryError("streamer registry occurrence-neutral policy is missing")
    if payload.get("status") not in {"fresh", "unconfigured"}:
        raise StreamerRegistryError("streamer registry status is invalid")
    members = payload.get("members")
    if not isinstance(members, list) or len(members) > 256:
        raise StreamerRegistryError("streamer registry member count is invalid")
    if payload.get("status") == "fresh" and len(members) < 20:
        raise StreamerRegistryError("fresh streamer registry is unexpectedly short")
    normalized: list[dict[str, Any]] = []
    entity_ids: set[str] = set()
    official_mids: set[int] = set()
    for index, row in enumerate(members):
        required = {
            "entity_id",
            "canonical",
            "official_surfaces",
            "aliases",
            "official_mid",
            "affiliations",
            "official_sources",
        }
        if not isinstance(row, dict) or set(row) != required:
            raise StreamerRegistryError(f"streamer registry member {index} has invalid fields")
        entity_id = _safe_atom(row["entity_id"], label=f"member {index}.entity_id")
        if entity_id in entity_ids:
            raise StreamerRegistryError("duplicate streamer registry entity_id")
        entity_ids.add(entity_id)
        official_mid = row["official_mid"]
        if official_mid is not None:
            if not isinstance(official_mid, int) or official_mid <= 0:
                raise StreamerRegistryError(f"member {index}.official_mid is invalid")
            if official_mid in official_mids:
                raise StreamerRegistryError("duplicate streamer registry official_mid")
            official_mids.add(official_mid)
        for key, limit in (("official_surfaces", 12), ("aliases", 24), ("affiliations", 8)):
            values = row[key]
            if not isinstance(values, list) or not values or len(values) > limit:
                if key == "aliases" and values == []:
                    continue
                raise StreamerRegistryError(f"member {index}.{key} is invalid")
            for value in values:
                _safe_atom(value, label=f"member {index}.{key}")
        sources = row["official_sources"]
        if not isinstance(sources, list) or not sources or len(sources) > 8:
            raise StreamerRegistryError(f"member {index}.official_sources is invalid")
        normalized.append(dict(row))
    if as_of is not None and payload.get("status") == "fresh":
        expires_at = dt.datetime.fromisoformat(str(payload.get("expires_at")))
        if expires_at.tzinfo is None or as_of > expires_at:
            raise StreamerRegistryError("streamer registry snapshot is expired")
    result = dict(payload)
    result["members"] = normalized
    return result


def snapshot_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def snapshot_sha256(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(snapshot_json(payload).encode("utf-8")).hexdigest()
