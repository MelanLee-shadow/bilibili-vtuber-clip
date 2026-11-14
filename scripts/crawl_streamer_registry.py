#!/usr/bin/env python3
"""Refresh the low-frequency official multi-organization streamer registry."""

from __future__ import annotations

import argparse
import datetime as dt
import http.client
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import urllib.error
import urllib.parse
from typing import Any, Callable, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.autoslice.channel_profile import load_channel_profile
from src.autoslice.psplive_roster_crawler import load_source_config as load_psplive_config
from src.autoslice.streamer_registry_crawler import (
    SNAPSHOT_TTL,
    StreamerRegistryError,
    build_snapshot,
    load_source_config,
    snapshot_json,
)
from src.autoslice.timely_term_crawler import (
    BILIBILI_USER_AGENT,
    BoundedHttpClient,
    CrawlError,
    HttpCache,
)


PROFILE = load_channel_profile(ROOT)
RETRY_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 2
BACKOFF_STATUS_CODES = frozenset({412, 429})
# Hard cap on announcement pages per run; more than this fails the source closed.
MAX_ANNOUNCEMENT_PAGES = 10
LAST_GOOD_SCHEMA = "vtuber-slice.streamer-registry-last-good.v1"
LAST_GOOD_SUFFIX = ".sources.json"


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument(
        "--config", type=Path, default=PROFILE.asset_file("streamer_registry_sources")
    )
    result.add_argument(
        "--cache-dir", type=Path, default=ROOT / ".cache/streamer-registry-crawler"
    )
    result.add_argument("--write", type=Path)
    result.add_argument("--now")
    return result


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _url(source: dict[str, object], page: int = 1) -> str:
    if source["kind"] != "virtuareal_bilibili_announcements":
        return str(source["api_url"])
    query = urllib.parse.urlencode(
        {
            "search_type": "video",
            "keyword": str(source["query"]),
            "page": page,
            "page_size": int(source["page_size"]),
            "order": "pubdate",
        }
    )
    return f"{source['endpoint']}?{query}"


def _fetch_with_retries(
    client: BoundedHttpClient,
    source: dict[str, object],
    *,
    page: int = 1,
    attempts: int = RETRY_ATTEMPTS,
    sleep: Callable[[float], None] = time.sleep,
):
    headers = {
        "User-Agent": (
            BILIBILI_USER_AGENT if "bilibili" in str(source["kind"]) else "Mozilla/5.0"
        ),
        "Referer": str(source["public_url"]),
    }
    for attempt in range(1, attempts + 1):
        try:
            return client.fetch(_url(source, page), headers=headers)
        except (CrawlError, OSError, urllib.error.URLError, http.client.IncompleteRead) as exc:
            if attempt == attempts:
                raise
            # Bilibili answers 412/429 when it is throttling; back off exponentially
            # so the next attempt is not sent into the same window.
            if getattr(exc, "code", None) in BACKOFF_STATUS_CODES:
                sleep(RETRY_BACKOFF_SECONDS * 2 ** (attempt - 1))
    raise AssertionError("unreachable bounded retry loop")


def _merge_announcement_rows(
    previous_rows: list[Any], current_rows: list[Any]
) -> list[Any]:
    """Union by BVID: a row once confirmed stays even if a later window omits it."""
    merged: dict[str, Any] = {}
    for row in [*previous_rows, *current_rows]:
        key = str(row.get("bvid") or "") if isinstance(row, Mapping) else ""
        merged[key or json.dumps(row, ensure_ascii=False, sort_keys=True)] = row
    return list(merged.values())


def _fetch_source_payload(
    client: BoundedHttpClient,
    source: dict[str, object],
    *,
    previous_payload: Mapping[str, Any] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Return one source payload; announcement search is read across every page."""
    if source["kind"] != "virtuareal_bilibili_announcements":
        response = _fetch_with_retries(client, source, sleep=sleep)
        return json.loads(response.body)
    collected: list[Any] = []
    page = 1
    total_pages: int | None = None
    while total_pages is None or page <= total_pages:
        response = _fetch_with_retries(client, source, page=page, sleep=sleep)
        body = json.loads(response.body)
        data = body.get("data") if isinstance(body, Mapping) else None
        if not isinstance(body, Mapping) or body.get("code") != 0 or not isinstance(data, Mapping):
            raise StreamerRegistryError("VirtuaReal announcement search was not successful")
        total_pages = int(data.get("numPages") or 0)
        if total_pages > MAX_ANNOUNCEMENT_PAGES:
            raise StreamerRegistryError(
                f"VirtuaReal announcement search has {total_pages} pages, "
                f"over the {MAX_ANNOUNCEMENT_PAGES}-page budget"
            )
        rows = data.get("result")
        if not isinstance(rows, list):
            raise StreamerRegistryError("VirtuaReal announcement search result is malformed")
        collected.extend(rows)
        page += 1
    previous_rows: list[Any] = []
    if isinstance(previous_payload, Mapping):
        previous_data = previous_payload.get("data")
        if isinstance(previous_data, Mapping) and isinstance(previous_data.get("result"), list):
            previous_rows = previous_data["result"]
    return {"code": 0, "data": {"result": _merge_announcement_rows(previous_rows, collected)}}


def _within_ttl(fetched_at: object, now: dt.datetime) -> bool:
    try:
        parsed = dt.datetime.fromisoformat(str(fetched_at))
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed <= now and now - parsed <= SNAPSHOT_TTL


def _load_last_good(path: Path | None, now: dt.datetime) -> dict[str, dict[str, Any]]:
    """Per-source last successful payloads that are still inside the snapshot TTL."""
    if path is None:
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict) or payload.get("schema_version") != LAST_GOOD_SCHEMA:
        return {}
    entries = payload.get("sources")
    if not isinstance(entries, dict):
        return {}
    kept: dict[str, dict[str, Any]] = {}
    for source_id, entry in entries.items():
        if (
            isinstance(entry, Mapping)
            and isinstance(entry.get("payload"), Mapping)
            and _within_ttl(entry.get("fetched_at"), now)
        ):
            kept[str(source_id)] = {"fetched_at": str(entry["fetched_at"]), "payload": dict(entry["payload"])}
    return kept


def _save_last_good(path: Path, entries: Mapping[str, Mapping[str, Any]]) -> None:
    text = json.dumps(
        {"schema_version": LAST_GOOD_SCHEMA, "sources": dict(sorted(entries.items()))},
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"
    _atomic_write(path, text)


def _next_last_good(
    snapshot: Mapping[str, Any],
    payloads: Mapping[str, Mapping[str, Any]],
    previous: Mapping[str, Mapping[str, Any]],
    now: dt.datetime,
) -> dict[str, dict[str, Any]]:
    entries: dict[str, dict[str, Any]] = {}
    for row in snapshot["sources"]:
        source_id = str(row["source_id"])
        if row["status"] == "fresh":
            entries[source_id] = {
                "fetched_at": now.isoformat(timespec="seconds"),
                "payload": dict(payloads[source_id]),
            }
        elif row["status"] == "stale" and source_id in previous:
            entries[source_id] = dict(previous[source_id])
    return entries


def _request_budget(config: Mapping[str, Any]) -> int:
    # Every source may use its retry attempts; the announcement search may page.
    return RETRY_ATTEMPTS * (len(config["sources"]) + MAX_ANNOUNCEMENT_PAGES) + 2


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    now = (
        dt.datetime.fromisoformat(args.now.replace("Z", "+00:00"))
        if args.now
        else dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    )
    try:
        if now.tzinfo is None or now.utcoffset() is None:
            raise StreamerRegistryError("--now must include a timezone")
        config = load_source_config(args.config)
        psplive_source = next(
            source for source in config["sources"] if source["kind"] == "psplive_bilibili_video"
        )
        legacy_path = (args.config.parent / str(psplive_source["legacy_config"])).resolve()
        legacy_config = load_psplive_config(legacy_path)
        store_path = args.write.with_name(args.write.name + LAST_GOOD_SUFFIX) if args.write else None
        previous = _load_last_good(store_path, now)
        client = BoundedHttpClient(
            allowed_hosts=frozenset({"api.bilibili.com", "bilibili.com", "vrp.live"}),
            max_requests=_request_budget(config),
            max_response_bytes=1024 * 1024,
            timeout_seconds=15,
            cache=HttpCache(args.cache_dir, max_body_bytes=1024 * 1024),
            cache_ttl=dt.timedelta(days=6),
            now=now,
        )
        payloads: dict[str, dict[str, Any]] = {}
        failures: dict[str, str] = {}
        for source in config["sources"]:
            source_id = str(source["source_id"])
            last_good = previous.get(source_id)
            try:
                payloads[source_id] = _fetch_source_payload(
                    client,
                    source,
                    previous_payload=last_good["payload"] if last_good else None,
                )
            except (CrawlError, OSError, ValueError, KeyError, http.client.IncompleteRead,
                    StreamerRegistryError) as exc:
                # Isolated: this source falls back to its last-good payload in build_snapshot.
                failures[source_id] = f"{type(exc).__name__}: {exc}"
        snapshot = build_snapshot(
            payloads=payloads,
            config=config,
            legacy_psplive_config=legacy_config,
            generated_at=now,
            previous=previous,
        )
        rendered = snapshot_json(snapshot)
        if args.write:
            _atomic_write(args.write, rendered)
            _save_last_good(store_path, _next_last_good(snapshot, payloads, previous, now))
        else:
            sys.stdout.write(rendered)
        print(
            json.dumps(
                {
                    "members": len(snapshot["members"]),
                    "organizations": sorted(
                        {group for row in snapshot["members"] for group in row["affiliations"]}
                    ),
                    "destination": str(args.write) if args.write else None,
                    "network_requests": client.requests_made,
                    "bootstrap_requests": client.bootstrap_requests,
                    "cache_hits": client.cache_hits,
                    "stale_cache_hits": client.stale_hits,
                    "source_status": {
                        str(row["source_id"]): row["status"] for row in snapshot["sources"]
                    },
                    "source_failures": failures,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 0
    except (OSError, ValueError, KeyError, json.JSONDecodeError, CrawlError,
            StreamerRegistryError) as exc:
        print(f"streamer registry crawler failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
