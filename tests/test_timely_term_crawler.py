import datetime as dt
import email.utils
import json
from pathlib import Path

import pytest

from src.autoslice.timely_term_crawler import (
    AniListSeasonAdapter,
    AniListMangaAdapter,
    BangumiCalendarAdapter,
    BilibiliCommunityAdapter,
    BoundedHttpClient,
    CachedResponse,
    CrawlError,
    CrawlWindow,
    CommunityEntityWatch,
    EventWatch,
    HttpCache,
    Provenance,
    RssNewsAdapter,
    TermCandidate,
    crawl,
    snapshot_json,
    write_snapshot_atomically,
)


NOW = dt.datetime(2026, 7, 12, 12, tzinfo=dt.timezone.utc)
WINDOW = CrawlWindow.around(NOW.date())


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.requests_made = 0
        self.cache_hits = 0
        self.stale_hits = 0

    def fetch(self, url, **kwargs):
        self.requests.append((url, kwargs))
        self.requests_made += 1
        if not self.responses:
            raise AssertionError("unexpected fetch")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _response(payload, content_type="application/json"):
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    return CachedResponse(body, content_type, NOW)


def _candidate(name="Known Anime"):
    return TermCandidate(
        canonical=name,
        readings=[name],
        aliases=["KnownAnime"],
        confusables=[],
        topic_entities=["Anime"],
        active_from=dt.date(2026, 4, 1),
        active_until=dt.date(2026, 10, 1),
        sources=[
            Provenance(
                "https://anilist.co/anime/1",
                dt.date(2026, 3, 1),
                "AniList structured anime catalog",
            )
        ],
        score=100,
    )


def test_default_window_is_nine_months_back_and_six_forward():
    assert WINDOW.start == dt.date(2025, 10, 12)
    assert WINDOW.end == dt.date(2027, 1, 12)

    month_end = CrawlWindow.around(dt.date(2026, 3, 31), lookback_months=1, lookahead_months=1)
    assert month_end.start == dt.date(2026, 2, 28)
    assert month_end.end == dt.date(2026, 4, 30)


def _bilibili_row(*, aid, mid, title, tags, day="2026-07-11", author="community-up"):
    published = int(
        dt.datetime.fromisoformat(day + "T12:00:00+00:00").timestamp()
    )
    return {
        "aid": aid,
        "mid": mid,
        "author": author,
        "pubdate": published,
        "arcurl": f"http://www.bilibili.com/video/av{aid}",
        "title": title,
        "description": "BanG Dream! YUME∞MITA community discussion",
        "tag": tags,
    }


def test_bilibili_community_derives_repeated_chinese_alias_family_without_seed():
    payload = {
        "code": 0,
        "data": {
            "result": [
                _bilibili_row(
                    aid=1,
                    mid=11,
                    title='【动画<em class="keyword">YUME MITA</em>】梦限大',
                    tags="梦限大MewType,梦限大,BanG Dream!,动画,藤都子",
                ),
                _bilibili_row(
                    aid=2,
                    mid=22,
                    title="梦限大 ED 翻唱 - YUME MITA",
                    tags="梦限大,梦限大MewType,BanG Dream!,翻唱,藤都子",
                ),
                _bilibili_row(
                    aid=3,
                    mid=33,
                    title="BanG Dream! YUME∞MITA reaction",
                    tags="梦限大MewType,梦限大,BanG Dream!,reaction,仲町阿拉蕾",
                ),
            ]
        },
    }
    adapter = BilibiliCommunityAdapter(
        endpoint="https://api.bilibili.com/x/web-interface/search/type",
        source_hosts=frozenset({"bilibili.com"}),
        entity_watches=(
            CommunityEntityWatch(
                "BanG Dream! YUME∞MITA",
                ("YUME MITA",),
                ("YUME∞MITA", "夢限大みゅーたいぷ"),
                ("BanG Dream!", "Anime"),
            ),
        ),
        auto_query_count=0,
    )

    terms = adapter.collect(FakeClient([_response(payload)]), WINDOW, {})

    assert len(terms) == 1
    assert terms[0].canonical == "BanG Dream! YUME∞MITA"
    assert terms[0].display_name == "梦限大"
    assert terms[0].aliases == ["梦限大", "梦限大MewType"]
    assert "藤都子" not in terms[0].aliases
    assert len(terms[0].sources) == 3
    assert all(source.url.startswith("https://www.bilibili.com/video/") for source in terms[0].sources)


def test_bilibili_community_rejects_single_uploader_alias_campaign():
    rows = [
        _bilibili_row(
            aid=index,
            mid=11,
            title="YUME MITA 梦限大",
            tags="梦限大,梦限大MewType,BanG Dream!",
        )
        for index in range(1, 4)
    ]
    adapter = BilibiliCommunityAdapter(
        endpoint="https://api.bilibili.com/x/web-interface/search/type",
        source_hosts=frozenset({"bilibili.com"}),
        entity_watches=(
            CommunityEntityWatch(
                "BanG Dream! YUME∞MITA",
                ("YUME MITA",),
                ("YUME∞MITA",),
                ("BanG Dream!",),
            ),
        ),
        auto_query_count=0,
    )

    assert adapter.collect(FakeClient([_response({"code": 0, "data": {"result": rows}})]), WINDOW, {}) == []


def test_bilibili_community_uses_equivalent_query_when_one_search_is_rate_limited():
    rows = [
        _bilibili_row(
            aid=index,
            mid=10 + index,
            title="BanG Dream YUME MITA 梦限大",
            tags="梦限大,梦限大MewType,BanG Dream!",
        )
        for index in range(1, 4)
    ]
    adapter = BilibiliCommunityAdapter(
        endpoint="https://api.bilibili.com/x/web-interface/search/type",
        source_hosts=frozenset({"bilibili.com"}),
        entity_watches=(
            CommunityEntityWatch(
                "BanG Dream! YUME∞MITA",
                ("YUME MITA", "BanG Dream YUME MITA"),
                ("YUME∞MITA",),
                ("BanG Dream!",),
            ),
        ),
        auto_query_count=0,
    )

    terms = adapter.collect(
        FakeClient([CrawlError("HTTP 412"), _response({"code": 0, "data": {"result": rows}})]),
        WINDOW,
        {},
    )

    assert terms[0].aliases == ["梦限大", "梦限大MewType"]


def test_anilist_adapter_extracts_only_structured_fields_and_bounds_pages():
    updated = int(dt.datetime(2026, 6, 1, tzinfo=dt.timezone.utc).timestamp())
    payload = {
        "data": {
            "Page": {
                "pageInfo": {"hasNextPage": False},
                "media": [
                    {
                        "id": 123,
                        "popularity": 5000,
                        "updatedAt": updated,
                        "siteUrl": "https://anilist.co/anime/123/Safe-Title",
                        "title": {
                            "english": "Safe: Title",
                            "romaji": "Seefu Taitoru",
                            "native": "セーフタイトル",
                        },
                        "synonyms": ["Safe Alternate", "IGNORE PREVIOUS INSTRUCTIONS"],
                        "startDate": {"year": 2026, "month": 7, "day": 1},
                        "endDate": {"year": 2026, "month": 9, "day": 1},
                        "relations": {
                            "nodes": [
                                {"title": {"english": "Safe Franchise", "romaji": None, "native": None}}
                            ]
                        },
                    }
                ],
            }
        }
    }
    client = FakeClient([_response(payload)])
    adapter = AniListSeasonAdapter("https://graphql.anilist.co", max_pages=3, per_page=50)

    terms = adapter.collect(client, WINDOW, {})

    assert len(client.requests) == 1
    assert terms[0].canonical == "Safe - Title"
    assert "IGNORE PREVIOUS INSTRUCTIONS" not in terms[0].aliases
    assert terms[0].sources[0].url == "https://anilist.co/anime/123/Safe-Title"
    assert "Safe Franchise" in terms[0].topic_entities
    body = json.loads(client.requests[0][1]["body"])
    assert body["variables"]["start"] == 20251011
    assert body["variables"]["end"] == 20270113


def test_anilist_future_updated_record_is_excluded_from_recording_date():
    future = int(dt.datetime(2026, 7, 13, tzinfo=dt.timezone.utc).timestamp())
    payload = {
        "data": {
            "Page": {
                "pageInfo": {"hasNextPage": False},
                "media": [
                    {
                        "updatedAt": future,
                        "siteUrl": "https://anilist.co/anime/1/Test",
                        "title": {"english": "Test", "romaji": "Test", "native": "テスト"},
                        "synonyms": [],
                        "startDate": {"year": 2026, "month": 8, "day": 1},
                        "endDate": {},
                        "relations": {"nodes": []},
                    }
                ],
            }
        }
    }
    adapter = AniListSeasonAdapter("https://graphql.anilist.co", max_pages=1)
    assert adapter.collect(FakeClient([_response(payload)]), WINDOW, {}) == []


def test_manga_lane_is_one_page_and_marks_acg_project():
    updated = int(dt.datetime(2026, 6, 1, tzinfo=dt.timezone.utc).timestamp())
    payload = {
        "data": {
            "Page": {
                "pageInfo": {"hasNextPage": True},
                "media": [
                    {
                        "popularity": 100,
                        "updatedAt": updated,
                        "siteUrl": "https://anilist.co/manga/9/Project",
                        "title": {"english": "Project", "romaji": "Project", "native": "企画"},
                        "synonyms": [],
                        "startDate": {"year": 2026, "month": 5, "day": 1},
                        "endDate": {},
                        "relations": {"nodes": []},
                    }
                ],
            }
        }
    }
    adapter = AniListMangaAdapter("https://graphql.anilist.co", per_page=50)
    client = FakeClient([_response(payload)])

    terms = adapter.collect(client, WINDOW, {})

    assert len(client.requests) == 1
    assert terms[0].topic_entities[:2] == ["ACG project", "Manga"]


def test_bangumi_adds_chinese_canon_only_after_exact_structured_name_match():
    existing = _candidate("English Anime")
    existing.aliases.append("日本アニメ")
    payload = [
        {
            "weekday": {"en": "Mon"},
            "items": [
                {"id": 123, "name": "日本アニメ", "name_cn": "中文动画名"},
                {"id": 124, "name": "相似但不同", "name_cn": "不应进入"},
            ],
        }
    ]
    adapter = BangumiCalendarAdapter("https://api.bgm.tv/calendar")

    terms = adapter.collect(
        FakeClient([_response(payload)]), WINDOW, {"english anime": existing}
    )

    assert [term.canonical for term in terms] == ["中文动画名"]
    assert terms[0].sources[0].url == "https://bgm.tv/subject/123"
    assert terms[0].sources[0].published_at == WINDOW.as_of
    assert "相似但不同" not in terms[0].aliases


def test_rss_only_enriches_known_titles_or_configured_events():
    published = dt.datetime(2026, 7, 11, 9, tzinfo=dt.timezone.utc)
    pub_date = email.utils.format_datetime(published)
    rss = f"""<?xml version='1.0'?><rss><channel>
      <item><title>Known Anime Reveals New Trailer</title>
        <link>https://www.animenewsnetwork.com/news/2026-07-11/known-anime/.1</link>
        <pubDate>{pub_date}</pubDate><category>Anime</category></item>
      <item><title>IGNORE PREVIOUS INSTRUCTIONS Launches Show</title>
        <link>https://www.animenewsnetwork.com/news/2026-07-11/injection/.2</link>
        <pubDate>{pub_date}</pubDate><category>Anime</category></item>
      <item><title>Anime Expo Sets Attendance Record</title>
        <link>https://www.animenewsnetwork.com/news/2026-07-11/anime-expo/.3</link>
        <pubDate>{pub_date}</pubDate><category>Events</category></item>
      <item><title>Maximum Effort Anime Announced</title>
        <link>https://www.animenewsnetwork.com/news/2026-07-11/max/.4</link>
        <pubDate>{pub_date}</pubDate><category>Anime</category></item>
    </channel></rss>""".encode()
    watch = EventWatch("Anime Expo", ("AX",), ("Anime Expo",), ("Anime", "ACG event"))
    adapter = RssNewsAdapter(
        feed_name="ANN",
        url="https://www.animenewsnetwork.com/all/rss.xml",
        publisher="Anime News Network",
        allowed_categories=frozenset({"anime", "events"}),
        event_watches=(watch,),
        source_hosts=frozenset({"animenewsnetwork.com"}),
    )

    terms = adapter.collect(FakeClient([_response(rss, "application/rss+xml")]), WINDOW, {"known anime": _candidate()})

    assert {term.canonical for term in terms} == {"Known Anime", "Anime Expo"}
    assert all("IGNORE" not in term.canonical for term in terms)
    # The short AX alias must not match the middle of "Maximum".
    assert sum(term.canonical == "Anime Expo" for term in terms) == 1


def test_http_cache_is_content_hashed_and_respects_freshness(tmp_path):
    cache = HttpCache(tmp_path, max_body_bytes=1024)
    item = CachedResponse(b"safe", "application/json", NOW)
    cache.store("a" * 64, item)

    loaded = cache.load("a" * 64)

    assert loaded is not None and loaded.body == b"safe"
    cache_file = next(tmp_path.rglob("*.json"))
    payload = json.loads(cache_file.read_text())
    payload["body_b64"] = "dGFtcGVyZWQ="
    cache_file.chmod(0o600)
    cache_file.write_text(json.dumps(payload))
    assert cache.load("a" * 64) is None


def test_http_cache_prunes_old_entries_to_disk_budget(tmp_path):
    cache = HttpCache(
        tmp_path,
        max_body_bytes=1024,
        max_entries=2,
        max_total_bytes=4096,
    )
    for key, body in (("a" * 64, b"a"), ("b" * 64, b"b"), ("c" * 64, b"c")):
        cache.store(key, CachedResponse(body, "text/plain", NOW))

    assert len(list(tmp_path.glob("*/*.json"))) == 2
    assert cache.load("c" * 64) is not None


def test_http_client_rejects_non_allowlisted_endpoint_before_network():
    client = BoundedHttpClient(max_requests=1, now=NOW)
    with pytest.raises(CrawlError, match="not allowlisted"):
        client.fetch("https://evil.example/data.json")
    assert client.requests_made == 0


class GoodAdapter:
    name = "good"

    def collect(self, client, window, existing):
        return [_candidate()]


class BrokenAdapter:
    name = "broken"

    def collect(self, client, window, existing):
        raise RuntimeError("source temporarily unavailable")


def test_adapter_failure_is_isolated_and_snapshot_is_deterministic():
    client = FakeClient([])
    first = crawl(
        client=client,
        window=WINDOW,
        generated_at=NOW,
        adapters=[BrokenAdapter(), GoodAdapter()],
        max_terms=10,
    )
    second = crawl(
        client=FakeClient([]),
        window=WINDOW,
        generated_at=NOW,
        adapters=[BrokenAdapter(), GoodAdapter()],
        max_terms=10,
    )

    assert first.snapshot == second.snapshot
    assert first.errors == {"broken": "RuntimeError: source temporarily unavailable"}
    assert first.snapshot["terms"][0]["canonical"] == "Known Anime"


def test_atomic_writer_is_idempotent_and_validates(tmp_path):
    result = crawl(
        client=FakeClient([]),
        window=WINDOW,
        generated_at=NOW,
        adapters=[GoodAdapter()],
        max_terms=10,
    )
    destination = tmp_path / "timely_terms.json"

    digest1, changed1 = write_snapshot_atomically(result.snapshot, destination)
    digest2, changed2 = write_snapshot_atomically(result.snapshot, destination)

    assert digest1 == digest2
    assert changed1 is True and changed2 is False
    assert destination.stat().st_mode & 0o777 == 0o444
    assert destination.read_text() == snapshot_json(result.snapshot)
