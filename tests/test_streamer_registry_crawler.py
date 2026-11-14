import datetime as dt
import json
from pathlib import Path

import pytest

from src.autoslice.psplive_roster_crawler import load_source_config as load_psplive_config
from src.autoslice.streamer_registry_crawler import (
    OCCURRENCE_POLICY,
    StreamerRegistryError,
    build_snapshot,
    load_source_config,
    validate_snapshot,
)
from src.autoslice.timely_term_crawler import CachedResponse, CrawlError


NOW = dt.datetime(2026, 8, 6, 1, 50, tzinfo=dt.timezone.utc)


def _config():
    config = load_source_config(Path("assets/lidousha/streamer_registry_sources.v1.json"))
    config = json.loads(json.dumps(config))
    api = next(row for row in config["sources"] if row["kind"] == "virtuareal_member_api")
    api["minimum_members"] = 2
    announcements = next(
        row
        for row in config["sources"]
        if row["kind"] == "virtuareal_bilibili_announcements"
    )
    announcements["minimum_announcements"] = 1
    announcements["minimum_members"] = 2
    return config


def _psp_payload(legacy):
    source = legacy["sources"][0]
    return {
        "code": 0,
        "data": {
            "owner": {"mid": source["owner_mid"], "name": source["owner_name"]},
            "title": "【2025新春】虚拟艺人团体psplive给大家拜年啦！",
            "desc": "参与人员\n" + "、".join(legacy["member_overrides"]),
        },
    }


def _payloads(config, legacy):
    ids = {row["kind"]: row["source_id"] for row in config["sources"]}
    published = int(dt.datetime(2025, 7, 31, 12, tzinfo=dt.timezone.utc).timestamp())
    return {
        ids["psplive_bilibili_video"]: _psp_payload(legacy),
        ids["virtuareal_member_api"]: {
            "content": [
                {
                    "nameCn": "旧成员",
                    "nameEn": "Legacy",
                    "bilibiliUID": "1001",
                    "project": "VP",
                },
                {
                    "nameCn": "花礼",
                    "nameEn": "Harei",
                    "bilibiliUID": "1048135385",
                    "project": "VP",
                },
            ]
        },
        ids["virtuareal_bilibili_announcements"]: {
            "code": 0,
            "data": {
                "result": [
                    {
                        "mid": 413748120,
                        "author": "VirtuaReal",
                        "title": "<em>VirtuaReal新成员加入</em>！",
                        "description": (
                            "花礼Harei：https://space.bilibili.com/1048135385\n"
                            "犬绒Mofu：https://space.bilibili.com/1125641408"
                        ),
                        "pubdate": published,
                        "bvid": "BV1RM8BzxEWD",
                    }
                ]
            },
        },
    }


def test_registry_merges_psplive_and_current_virtuareal_announcements_by_mid():
    config = _config()
    legacy = load_psplive_config(Path("assets/lidousha/psplive_roster_sources.v1.json"))
    snapshot = build_snapshot(
        payloads=_payloads(config, legacy),
        config=config,
        legacy_psplive_config=legacy,
        generated_at=NOW,
    )

    harei = next(row for row in snapshot["members"] if row["entity_id"] == "bilibili:1048135385")
    mofu = next(row for row in snapshot["members"] if row["entity_id"] == "bilibili:1125641408")
    lidousha = next(row for row in snapshot["members"] if row["canonical"] == "李豆沙")

    assert harei["canonical"] == "花礼Harei"
    assert set(harei["official_surfaces"]) >= {"花礼", "Harei", "花礼Harei"}
    assert len(harei["official_sources"]) == 2
    assert mofu["official_mid"] == 1125641408
    assert lidousha["affiliations"] == ["psplive"]
    assert snapshot["occurrence_policy"] == OCCURRENCE_POLICY
    assert snapshot["expires_at"] == "2026-09-10T01:50:00+00:00"


def test_future_announcement_is_not_allowed_to_predeclare_members():
    config = _config()
    legacy = load_psplive_config(Path("assets/lidousha/psplive_roster_sources.v1.json"))
    payloads = _payloads(config, legacy)
    announcement_id = next(
        row["source_id"]
        for row in config["sources"]
        if row["kind"] == "virtuareal_bilibili_announcements"
    )
    payloads[announcement_id]["data"]["result"][0]["pubdate"] = int(
        (NOW + dt.timedelta(days=1)).timestamp()
    )

    # Per-source isolation: the future-dated announcement is not counted, so its
    # source is unavailable (no last-good here) and it predeclares no member.
    snapshot = build_snapshot(
        payloads=payloads,
        config=config,
        legacy_psplive_config=legacy,
        generated_at=NOW,
    )

    provenance = {row["source_id"]: row for row in snapshot["sources"]}
    assert provenance[announcement_id]["status"] == "unavailable"
    assert "too few official" in provenance[announcement_id]["reason"]
    assert all(row["entity_id"] != "bilibili:1125641408" for row in snapshot["members"])


def test_registry_rejects_occurrence_authority_drift():
    config = _config()
    legacy = load_psplive_config(Path("assets/lidousha/psplive_roster_sources.v1.json"))
    snapshot = build_snapshot(
        payloads=_payloads(config, legacy),
        config=config,
        legacy_psplive_config=legacy,
        generated_at=NOW,
    )
    snapshot["occurrence_policy"] = "ALL_NAMES_ALWAYS_WIN"

    with pytest.raises(StreamerRegistryError, match="occurrence-neutral"):
        validate_snapshot(snapshot)


def test_registry_cli_retry_is_bounded_and_uses_full_bilibili_user_agent():
    from scripts.crawl_streamer_registry import _fetch_with_retries

    class Client:
        def __init__(self):
            self.calls = []

        def fetch(self, url, *, headers):
            self.calls.append((url, headers))
            if len(self.calls) < 3:
                raise CrawlError("HTTP 412")
            return CachedResponse(b"{}", "application/json", NOW)

    source = next(
        row
        for row in _config()["sources"]
        if row["kind"] == "virtuareal_bilibili_announcements"
    )
    client = Client()
    response = _fetch_with_retries(client, source, attempts=3)

    assert response.body == b"{}"
    assert len(client.calls) == 3
    assert "Chrome/" in client.calls[0][1]["User-Agent"]


HAREI_BVID = "BV1uW421R79X"
HAREI_PUBDATE = int(dt.datetime(2024, 7, 11, tzinfo=dt.timezone.utc).timestamp())


def _announcement_row(bvid, *, description, pubdate=HAREI_PUBDATE):
    return {
        "mid": 413748120,
        "author": "VirtuaReal",
        "title": "<em>VirtuaReal新成员加入</em>！",
        "description": description,
        "pubdate": pubdate,
        "bvid": bvid,
    }


def _harei_row():
    return _announcement_row(
        HAREI_BVID,
        description=(
            "花礼Harei：https://space.bilibili.com/1048135385\n"
            "犬绒Mofu：https://space.bilibili.com/1125641408"
        ),
    )


def _source_id(config, kind):
    return next(row["source_id"] for row in config["sources"] if row["kind"] == kind)


def _with_bulk_members(payloads, config, count=20):
    """Pad the VirtuaReal member API so a fresh snapshot clears its 20-member floor."""
    api_id = _source_id(config, "virtuareal_member_api")
    payloads[api_id]["content"].extend(
        {"nameCn": f"测试成员{index}", "nameEn": "", "bilibiliUID": str(2000 + index), "project": "VP"}
        for index in range(count)
    )
    return payloads


def test_registry_412_backoff_is_exponential_between_attempts():
    import urllib.error

    from scripts.crawl_streamer_registry import _fetch_with_retries

    class Client:
        def __init__(self):
            self.calls = 0

        def fetch(self, url, *, headers):
            self.calls += 1
            if self.calls <= 2:
                raise urllib.error.HTTPError(url, 412, "Precondition Failed", None, None)
            return CachedResponse(b"{}", "application/json", NOW)

    source = next(row for row in _config()["sources"] if row["kind"] == "virtuareal_bilibili_announcements")
    sleeps = []
    response = _fetch_with_retries(Client(), source, sleep=sleeps.append)

    assert response.body == b"{}"
    assert sleeps == [2, 4]


def test_failed_source_reuses_last_good_payload_and_is_marked_stale():
    config = _config()
    legacy = load_psplive_config(Path("assets/lidousha/psplive_roster_sources.v1.json"))
    payloads = _payloads(config, legacy)
    announcement_id = _source_id(config, "virtuareal_bilibili_announcements")
    last_good = {
        announcement_id: {
            "fetched_at": "2026-08-01T01:50:00+00:00",
            "payload": payloads.pop(announcement_id),
        }
    }

    snapshot = build_snapshot(
        payloads=payloads,
        config=config,
        legacy_psplive_config=legacy,
        generated_at=NOW,
        previous=last_good,
    )

    provenance = {row["source_id"]: row for row in snapshot["sources"]}
    assert snapshot["status"] == "fresh"
    assert provenance[announcement_id]["status"] == "stale"
    assert provenance[announcement_id]["fetched_at"] == "2026-08-01T01:50:00+00:00"
    harei = next(row for row in snapshot["members"] if row["entity_id"] == "bilibili:1048135385")
    assert "virtuareal" in harei["affiliations"]


def test_source_without_last_good_is_unavailable_and_snapshot_still_gated():
    config = _config()
    legacy = load_psplive_config(Path("assets/lidousha/psplive_roster_sources.v1.json"))
    payloads = _with_bulk_members(_payloads(config, legacy), config)
    announcement_id = _source_id(config, "virtuareal_bilibili_announcements")
    payloads.pop(announcement_id)

    snapshot = build_snapshot(
        payloads=payloads,
        config=config,
        legacy_psplive_config=legacy,
        generated_at=NOW,
        previous=None,
    )

    provenance = {row["source_id"]: row for row in snapshot["sources"]}
    assert provenance[announcement_id]["status"] == "unavailable"
    # 1125641408 is named only by the announcement source, so it must be absent.
    assert all(row["entity_id"] != "bilibili:1125641408" for row in snapshot["members"])
    assert any(row["entity_id"] == "bilibili:2000" for row in snapshot["members"])


def test_registry_fails_closed_when_no_source_can_be_refreshed():
    config = _config()
    legacy = load_psplive_config(Path("assets/lidousha/psplive_roster_sources.v1.json"))

    with pytest.raises(StreamerRegistryError, match="no official source"):
        build_snapshot(
            payloads={},
            config=config,
            legacy_psplive_config=legacy,
            generated_at=NOW,
            previous=None,
        )


def test_fresh_payload_that_fails_parsing_falls_back_and_rolls_back_partial_members():
    config = _config()
    legacy = load_psplive_config(Path("assets/lidousha/psplive_roster_sources.v1.json"))
    payloads = _payloads(config, legacy)
    announcement_id = _source_id(config, "virtuareal_bilibili_announcements")
    good = payloads[announcement_id]
    # One counted row naming a new member only: found_mids (1) < minimum_members (2) fails
    # after the row has already been added, so the partial member must be rolled back.
    payloads[announcement_id] = {
        "code": 0,
        "data": {"result": [_announcement_row(
            "BV1ROLLBACK01",
            description="新人甲：https://space.bilibili.com/3003",
        )]},
    }
    last_good = {announcement_id: {"fetched_at": "2026-08-01T01:50:00+00:00", "payload": good}}

    snapshot = build_snapshot(
        payloads=payloads,
        config=config,
        legacy_psplive_config=legacy,
        generated_at=NOW,
        previous=last_good,
    )

    provenance = {row["source_id"]: row for row in snapshot["sources"]}
    assert provenance[announcement_id]["status"] == "stale"
    assert all(row["entity_id"] != "bilibili:3003" for row in snapshot["members"])
    assert any(row["entity_id"] == "bilibili:1048135385" for row in snapshot["members"])


def test_announcement_fetch_follows_every_page_and_keeps_confirmed_rows():
    from urllib.parse import parse_qs, urlsplit

    from scripts.crawl_streamer_registry import _fetch_source_payload

    source = next(row for row in _config()["sources"] if row["kind"] == "virtuareal_bilibili_announcements")
    pages = {
        1: {"code": 0, "data": {"numPages": 2, "result": [
            _announcement_row("BV1PAGEONE01", description="甲：https://space.bilibili.com/3001")]}},
        2: {"code": 0, "data": {"numPages": 2, "result": [_harei_row()]}},
    }

    class PagedClient:
        def __init__(self):
            self.urls = []

        def fetch(self, url, *, headers):
            self.urls.append(url)
            page = int(parse_qs(urlsplit(url).query)["page"][0])
            return CachedResponse(json.dumps(pages[page]).encode(), "application/json", NOW)

    client = PagedClient()
    payload = _fetch_source_payload(client, source, sleep=lambda _seconds: None)

    bvids = [row["bvid"] for row in payload["data"]["result"]]
    assert bvids == ["BV1PAGEONE01", HAREI_BVID]
    assert len(client.urls) == 2


def test_announcement_window_drop_does_not_remove_previously_confirmed_rows():
    from scripts.crawl_streamer_registry import _fetch_source_payload

    source = next(row for row in _config()["sources"] if row["kind"] == "virtuareal_bilibili_announcements")
    previous = {"code": 0, "data": {"numPages": 1, "result": [_harei_row()]}}
    current = {"code": 0, "data": {"numPages": 1, "result": [_announcement_row(
        "BV1NEWONE001", description="甲：https://space.bilibili.com/3001")]}}

    class Client:
        def fetch(self, url, *, headers):
            return CachedResponse(json.dumps(current).encode(), "application/json", NOW)

    payload = _fetch_source_payload(Client(), source, previous_payload=previous, sleep=lambda _s: None)

    bvids = {row["bvid"] for row in payload["data"]["result"]}
    assert bvids == {HAREI_BVID, "BV1NEWONE001"}


def test_announcement_page_budget_fails_closed_before_fetching_all_pages():
    from scripts.crawl_streamer_registry import MAX_ANNOUNCEMENT_PAGES, _fetch_source_payload

    source = next(row for row in _config()["sources"] if row["kind"] == "virtuareal_bilibili_announcements")

    class Client:
        def __init__(self):
            self.calls = 0

        def fetch(self, url, *, headers):
            self.calls += 1
            body = {"code": 0, "data": {"numPages": MAX_ANNOUNCEMENT_PAGES + 1, "result": []}}
            return CachedResponse(json.dumps(body).encode(), "application/json", NOW)

    client = Client()
    with pytest.raises(StreamerRegistryError, match="page"):
        _fetch_source_payload(client, source, sleep=lambda _s: None)
    assert client.calls == 1


def test_last_good_store_round_trips_and_drops_entries_past_snapshot_expiry(tmp_path):
    from scripts.crawl_streamer_registry import _load_last_good, _save_last_good

    path = tmp_path / "streamer_registry.sources.json"
    fresh = {"fetched_at": "2026-08-04T01:50:00+00:00", "payload": {"code": 0}}
    expired = {"fetched_at": "2026-06-01T01:50:00+00:00", "payload": {"code": 0}}
    _save_last_good(path, {"kept": fresh, "dropped": expired})

    assert set(_load_last_good(path, NOW)) == {"kept"}
    assert _load_last_good(tmp_path / "missing.json", NOW) == {}
    path.write_text("not json", encoding="utf-8")
    assert _load_last_good(path, NOW) == {}
