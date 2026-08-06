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

    with pytest.raises(StreamerRegistryError, match="too few official"):
        build_snapshot(
            payloads=payloads,
            config=config,
            legacy_psplive_config=legacy,
            generated_at=NOW,
        )


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
