from __future__ import annotations

import copy
import datetime as dt
import json
from types import SimpleNamespace

import pytest

from scripts.gemini_slice_jingting import agy_prompt
from scripts.crawl_topic_entity_graph import parser as graph_crawler_parser
from scripts.crawl_topic_entity_graph import write_complete_graph
from scripts.run_full_session_selector_cpa_shadow import _cpa_correct_draft_cues
from src.autoslice.chat_authority import (
    ReferentEntity,
    ReferentGroup,
    apply_audio_entity_verification,
)
from src.autoslice.timely_term_crawler import CrawlError
from src.autoslice.topic_entity_crawler import (
    GraphCrawlResult,
    _search_score,
    _search_subjects,
    crawl_topic_entity_graph,
)
from src.autoslice.topic_entity_graph import (
    TopicEntityGraphError,
    TopicEvidence,
    build_scoped_topic_context,
    dynamic_referent_groups,
    merge_referent_groups,
    render_scoped_entity_context,
    resolve_topic_context,
    validate_topic_entity_graph,
)


def _source(url: str, date: str = "2026-07-01") -> dict[str, str]:
    return {"url": url, "published_at": date, "publisher": "structured fixture"}


def _graph() -> dict:
    return {
        "schema_version": "lidousha-topic-entity-graph.v1",
        "generator": "scripts/crawl_topic_entity_graph.py",
        "input_timely_terms_sha256": "f" * 64,
        "generated_at": "2026-07-12T12:00:00+00:00",
        "expires_at": "2026-07-19T12:00:00+00:00",
        "status": "fresh",
        "topics": [
            {
                "topic_id": "topic:bangdream",
                "canonical": "BanG Dream!",
                "aliases": ["邦多利"],
                "work_ids": ["work:mygo", "work:ave"],
                "active_from": "2026-01-01",
                "active_until": "2027-01-01",
                "sources": [_source("https://bgm.tv/subject/1")],
            },
            {
                "topic_id": "topic:other",
                "canonical": "Other Anime",
                "aliases": ["别的动画"],
                "work_ids": ["work:other"],
                "active_from": "2026-01-01",
                "active_until": "2027-01-01",
                "sources": [_source("https://bgm.tv/subject/9")],
            },
        ],
        "works": [
            {
                "work_id": "work:mygo",
                "canonical": "BanG Dream! It's MyGO!!!!!",
                "aliases": ["MyGO"],
                "topic_ids": ["topic:bangdream"],
                "entity_ids": ["character:taki", "character:sakiko", "character:tomori"],
                "aired_from": "2023-06-29",
                "sources": [_source("https://bgm.tv/subject/2", "2023-06-29")],
            },
            {
                "work_id": "work:ave",
                "canonical": "BanG Dream! Ave Mujica",
                "aliases": ["Ave Mujica"],
                "topic_ids": ["topic:bangdream"],
                "entity_ids": ["character:uika"],
                "aired_from": "2025-01-01",
                "sources": [_source("https://bgm.tv/subject/3", "2025-01-01")],
            },
            {
                "work_id": "work:other",
                "canonical": "Other Anime",
                "aliases": [],
                "topic_ids": ["topic:other"],
                "entity_ids": ["character:other"],
                "aired_from": "2025-01-01",
                "sources": [_source("https://bgm.tv/subject/9", "2025-01-01")],
            },
        ],
        "entities": [
            {
                "entity_id": "character:taki",
                "kind": "character",
                "canonical_zh": "椎名立希",
                "native_names": ["椎名立希"],
                "aliases": ["立希", "Rikki"],
                "readings": ["しいな たき", "Shiina Taki", "Taki"],
                "role": "MAIN",
                "work_ids": ["work:mygo"],
                "sources": [_source("https://bgm.tv/character/1", "2023-06-29")],
            },
            {
                "entity_id": "character:sakiko",
                "kind": "character",
                "canonical_zh": "丰川祥子",
                "native_names": ["豊川祥子"],
                "aliases": ["祥子", "Saki"],
                "readings": ["とがわ さきこ", "Togawa Sakiko", "Sakiko"],
                "role": "SUPPORTING",
                "work_ids": ["work:mygo"],
                "sources": [_source("https://bgm.tv/character/2", "2023-06-29")],
            },
            {
                "entity_id": "character:tomori",
                "kind": "character",
                "canonical_zh": "高松灯",
                "native_names": ["高松燈"],
                "aliases": ["灯"],
                "readings": ["たかまつ ともり", "Takamatsu Tomori"],
                "role": "MAIN",
                "work_ids": ["work:mygo"],
                "sources": [_source("https://bgm.tv/character/3", "2023-06-29")],
            },
            {
                "entity_id": "character:uika",
                "kind": "character",
                "canonical_zh": "三角初华",
                "native_names": ["三角初華"],
                "aliases": ["初华"],
                "readings": ["みすみ ういか", "Misumi Uika"],
                "role": "MAIN",
                "work_ids": ["work:ave"],
                "sources": [_source("https://bgm.tv/character/4", "2025-01-01")],
            },
            {
                "entity_id": "character:other",
                "kind": "character",
                "canonical_zh": "无关角色",
                "native_names": ["無関係"],
                "aliases": [],
                "readings": ["mukankei"],
                "role": "MAIN",
                "work_ids": ["work:other"],
                "sources": [_source("https://bgm.tv/character/9", "2025-01-01")],
            },
        ],
    }


def test_family_topic_routes_to_only_its_child_subgraph():
    graph = validate_topic_entity_graph(_graph())
    result = resolve_topic_context(
        graph,
        [TopicEvidence("selection_hook", "聊到邦多利角色")],
        recording_date="2026-07-12",
        graph_sha256="a" * 64,
    )
    assert result.status == "RESOLVED"
    assert set(result.selected_work_ids) == {"work:mygo", "work:ave"}
    assert "character:other" not in result.scoped_entity_ids
    assert {"character:taki", "character:uika"} <= set(result.scoped_entity_ids)

    scoped = build_scoped_topic_context(graph, result)
    assert scoped["graph_sha256"] == "a" * 64
    assert scoped["topics"][0]["canonical"] == "BanG Dream!"
    assert {row["canonical"] for row in scoped["works"]} == {
        "BanG Dream! It's MyGO!!!!!",
        "BanG Dream! Ave Mujica",
    }
    assert "无关角色" not in {
        row["canonical_zh"] for row in scoped["entities"]
    }
    assert all(row["sources"] for row in scoped["entities"])


def test_explicit_work_routes_without_sibling_character_leakage():
    graph = validate_topic_entity_graph(_graph())
    result = resolve_topic_context(
        graph,
        [TopicEvidence("transcript", "我最近在看MyGO")],
        recording_date="2026-07-12",
        graph_sha256="b" * 64,
    )
    assert result.status == "RESOLVED"
    assert result.selected_work_ids == ("work:mygo",)
    assert "character:uika" not in result.scoped_entity_ids


def test_related_unit_is_scoped_like_an_equal_proper_name():
    payload = _graph()
    payload["entities"].append(
        {
            "entity_id": "unit:sumimi",
            "kind": "unit",
            "canonical_zh": "sumimi",
            "native_names": ["sumimi"],
            "aliases": ["スミミ"],
            "readings": ["sumimi", "すみみ"],
            "role": "RELATED",
            "work_ids": [],
            "activation_work_ids": ["work:mygo", "work:ave"],
            "sources": [
                _source(
                    "https://anime.bang-dream.com/avemujica/character/uika/",
                    "2025-01-02",
                )
            ],
        }
    )
    payload["works"][0]["retrieval_entity_ids"] = ["unit:sumimi"]
    payload["works"][1]["retrieval_entity_ids"] = ["unit:sumimi"]
    graph = validate_topic_entity_graph(payload)

    result = resolve_topic_context(
        graph,
        [TopicEvidence("transcript", "前面一直在聊MyGO和Mujica")],
        recording_date="2026-07-12",
        graph_sha256="c" * 64,
    )
    context = render_scoped_entity_context(graph, result)

    assert "unit:sumimi" in result.scoped_entity_ids
    assert '"kind":"unit"' in context
    assert '"canonical_zh":"sumimi"' in context
    context = render_scoped_entity_context(graph, result)
    assert "椎名立希" in context
    assert "三角初华" not in context
    assert "structured fixture" not in context


def test_sibling_name_in_nearby_chat_cannot_override_explicit_spoken_work():
    graph = validate_topic_entity_graph(_graph())
    result = resolve_topic_context(
        graph,
        [
            TopicEvidence("transcript", "我最近在看MyGO，立希很好"),
            TopicEvidence("structured_chat", "Ave Mujica也不错"),
        ],
        recording_date="2026-07-12",
        graph_sha256="e" * 64,
    )
    assert result.selected_work_ids == ("work:mygo",)
    assert "character:taki" in result.scoped_entity_ids
    assert "character:uika" not in result.scoped_entity_ids


def test_structured_chat_routes_work_only_when_no_direct_work_matches():
    graph = validate_topic_entity_graph(_graph())
    result = resolve_topic_context(
        graph,
        [
            TopicEvidence("transcript", "她们都挺有意思"),
            TopicEvidence("structured_chat", "Ave Mujica也不错"),
        ],
        recording_date="2026-07-12",
        graph_sha256="f" * 64,
    )
    assert result.selected_work_ids == ("work:ave",)
    assert "character:uika" in result.scoped_entity_ids


def test_dynamic_audio_group_compares_lixi_with_topic_siblings():
    graph = validate_topic_entity_graph(_graph())
    result = resolve_topic_context(
        graph,
        [TopicEvidence("selection_hook", "MyGO角色讨论")],
        recording_date="2026-07-12",
        graph_sha256="c" * 64,
    )
    groups = dynamic_referent_groups(
        graph,
        result,
        "1\n00:00:00,000 --> 00:00:02,000\n然后那个立希的高压态度\n",
    )
    assert len(groups) == 1
    canonicals = {entity.canonical for entity in groups[0].entities}
    assert {"椎名立希", "丰川祥子"} <= canonicals
    assert "三角初华" not in canonicals
    assert groups[0].audio_verify_all_surfaces is True


def test_dynamic_full_name_group_supersedes_overlapping_static_short_name_group():
    graph = validate_topic_entity_graph(_graph())
    resolution = resolve_topic_context(
        graph,
        [TopicEvidence("selection_hook", "MyGO角色讨论")],
        recording_date="2026-07-12",
        graph_sha256="d" * 64,
    )
    dynamic = dynamic_referent_groups(
        graph,
        resolution,
        "1\n00:00:00,000 --> 00:00:02,000\n然后那个立希的高压态度\n",
    )
    static = [
        ReferentGroup(
            (
                ReferentEntity("立希", ("立希", "椎名立希"), ("lixi", "taki")),
                ReferentEntity("祥子", ("祥子", "丰川祥子", "Saki"), ("xiangzi", "saki")),
            ),
            audio_verify_all_surfaces=True,
        )
    ]
    merged = merge_referent_groups(static, dynamic)
    assert merged == dynamic

    calls = []

    def verify(request):
        calls.append(request)
        return {
            "schema_version": "chat-entity-verdict.v1",
            "request_sha256": request["request_sha256"],
            "status": "RESOLVED",
            "canonical_entity": "椎名立希",
            "authority_kind": "audio_forced_choice",
            "source_media_sha256": "a" * 64,
            "audio_clip_sha256": "b" * 64,
            "prompt_sha256": "c" * 64,
            "response_sha256": "d" * 64,
            "confidence": 0.95,
        }

    output, audit = apply_audio_entity_verification(
        "1\n00:00:00,000 --> 00:00:02,000\n然后那个立希的高压态度\n",
        referent_groups=merged,
        entity_verifier=verify,
    )
    assert len(calls) == 1
    assert audit["status"] == "VERIFIED"
    # A correct spoken short name remains short; the graph verifies identity
    # and spelling without expanding words that were not spoken.
    assert "立希" in output


def test_dangling_graph_edge_fails_closed():
    graph = _graph()
    graph["works"][0]["entity_ids"].append("character:missing")
    with pytest.raises(TopicEntityGraphError, match="dangling"):
        validate_topic_entity_graph(graph)


def test_non_reciprocal_graph_edge_fails_closed():
    graph = _graph()
    graph["entities"][0]["work_ids"] = []
    with pytest.raises(TopicEntityGraphError, match="non-reciprocal entity edge"):
        validate_topic_entity_graph(graph)


def _timely_snapshot() -> dict:
    return {
        "schema_version": "lidousha-timely-terms.v1",
        "generated_at": "2026-07-12T12:00:00+00:00",
        "expires_at": "2026-07-14T12:00:00+00:00",
        "status": "fresh",
        "terms": [
            {
                "canonical": "Current Franchise",
                "readings": ["Current Work"],
                "aliases": ["当前企划"],
                "confusables": [],
                "topic_entities": ["Anime", "Current Work"],
                "active_from": "2026-01-01",
                "active_until": "2027-01-01",
                "sources": [_source("https://anilist.co/anime/1")],
            }
        ],
    }


class _FakeClient:
    requests_made = 0
    cache_hits = 0
    stale_hits = 0

    def fetch(self, url, *, method="GET", body=None, content_type="", headers=None):
        del content_type, headers
        self.requests_made += 1
        if method == "POST":
            payload = {
                "data": [
                    {
                        "id": 42,
                        "type": 2,
                        "name": "Current Work",
                        "name_cn": "当前作品",
                        "collection": {"collect": 100},
                    }
                ]
            }
        else:
            payload = {
                "id": 42,
                "name": "Current Work",
                "name_cn": "当前作品",
                "air_date": "2026-04-01",
                "crt": [
                    {
                        "id": 7,
                        "name": "椎名立希",
                        "name_cn": "椎名立希",
                        "role_name": "主角",
                        "info": {
                            "name_cn": "椎名立希",
                            "alias": [
                                {"key": "jp", "value": "椎名 立希"},
                                {"key": "kana", "value": "しいな たき"},
                                {"key": "romaji", "value": "Shiina Taki"},
                                {"key": "nickname", "value": "Rikki、立希队长"},
                            ],
                        },
                    }
                ],
            }
        return SimpleNamespace(body=json.dumps(payload, ensure_ascii=False).encode())


def test_crawler_builds_chinese_name_and_short_reading_from_structured_subject():
    result = crawl_topic_entity_graph(
        client=_FakeClient(),
        timely_snapshot=_timely_snapshot(),
        input_timely_terms_sha256="e" * 64,
        generated_at=dt.datetime(2026, 7, 12, 12, tzinfo=dt.timezone.utc),
        max_topics=1,
        max_queries=3,
        max_works_per_topic=1,
    )
    entity = result.graph["entities"][0]
    work = result.graph["works"][0]
    assert entity["canonical_zh"] == "椎名立希"
    assert "立希" in entity["aliases"]
    assert {"Rikki", "立希队长"} <= set(entity["aliases"])
    assert "Taki" in entity["readings"]
    assert "Current Work" in work["aliases"]


def test_crawler_attaches_reviewed_unit_seed_to_exact_matching_work():
    seeds = {
        "schema_version": "lidousha-related-entity-seeds.v1",
        "entities": [
            {
                "entity_id": "official:bandori:unit:sumimi",
                "kind": "unit",
                "canonical_zh": "sumimi",
                "native_names": ["sumimi"],
                "aliases": ["スミミ"],
                "readings": ["sumimi", "すみみ"],
                "role": "RELATED",
                "work_surfaces": ["Current Work"],
                "sources": [
                    _source(
                        "https://anime.bang-dream.com/avemujica/character/uika/",
                        "2025-01-02",
                    )
                ],
            }
        ],
    }

    result = crawl_topic_entity_graph(
        client=_FakeClient(),
        timely_snapshot=_timely_snapshot(),
        input_timely_terms_sha256="e" * 64,
        generated_at=dt.datetime(2026, 7, 12, 12, tzinfo=dt.timezone.utc),
        max_topics=1,
        max_queries=3,
        max_works_per_topic=1,
        related_entity_seeds=seeds,
    )

    unit = next(
        entity for entity in result.graph["entities"] if entity["kind"] == "unit"
    )
    assert unit["canonical_zh"] == "sumimi"
    assert unit["work_ids"] == []
    assert unit["entity_id"] not in result.graph["works"][0]["entity_ids"]
    assert unit["entity_id"] in result.graph["works"][0]["retrieval_entity_ids"]


@pytest.mark.parametrize(
    "source_url",
    [
        "https://www.animenewsnetwork.com/news/2026-07-11/current-anime/.1",
        "https://anime.bang-dream.com/avemujica/",
        "https://bushiroad.com/events/current-anime",
        "https://www.tv-tokyo.co.jp/anime/current/",
    ],
)
def test_crawler_graph_accepts_every_machine_timely_source_family(source_url):
    result = crawl_topic_entity_graph(
        client=_BreadthFakeClient({"Current Work": 42}),
        timely_snapshot=_snapshot(_term("Current Work", source_url=source_url)),
        input_timely_terms_sha256="a" * 64,
        generated_at=dt.datetime(2026, 7, 12, 12, tzinfo=dt.timezone.utc),
        max_topics=1,
        max_queries=1,
        max_works_per_topic=1,
    )

    assert result.graph["topics"][0]["sources"][0]["url"] == source_url


def _term(
    canonical: str,
    *,
    active_from: str = "2026-01-01",
    topic_entities: list[str] | None = None,
    community: bool = False,
    source_url: str = "https://anilist.co/anime/1",
) -> dict:
    source = _source(source_url)
    if source_url.startswith("https://bgm.tv/subject/"):
        source["publisher"] = "Bangumi structured fixture"
    if community:
        source["publisher"] = "Bilibili community fixture"
    return {
        "canonical": canonical,
        "readings": [canonical],
        "aliases": [],
        "confusables": [],
        "topic_entities": ["Anime", *(topic_entities or [])],
        "active_from": active_from,
        "active_until": "2027-01-01",
        "sources": [source],
    }


def _snapshot(*terms: dict) -> dict:
    return {
        "schema_version": "lidousha-timely-terms.v1",
        "generated_at": "2026-07-12T12:00:00+00:00",
        "expires_at": "2026-07-14T12:00:00+00:00",
        "status": "fresh",
        "terms": list(terms),
    }


class _BreadthFakeClient:
    requests_made = 0
    cache_hits = 0
    stale_hits = 0

    def __init__(self, search_ids: dict[str, int | None], *, empty_cast_ids: set[int] | None = None):
        self.search_ids = search_ids
        self.empty_cast_ids = empty_cast_ids or set()
        self.search_queries: list[str] = []

    def fetch(self, url, *, method="GET", body=None, content_type="", headers=None):
        del content_type, headers
        self.requests_made += 1
        if method == "POST":
            query = json.loads(body)["keyword"]
            self.search_queries.append(query)
            subject_id = self.search_ids.get(query)
            rows = []
            if subject_id is not None:
                rows.append(
                    {
                        "id": subject_id,
                        "type": 2,
                        "name": query,
                        "name_cn": query,
                        "collection": {"collect": 100},
                    }
                )
            payload = {"data": rows}
        else:
            subject_id = int(url.split("/subject/")[1].split("?")[0])
            characters = []
            if subject_id not in self.empty_cast_ids:
                characters = [
                    {
                        "id": 1000 + subject_id,
                        "name": f"角色{subject_id}",
                        "name_cn": f"角色{subject_id}",
                        "role_name": "主角",
                        "info": {"name_cn": f"角色{subject_id}", "alias": []},
                    }
                ]
            payload = {
                "id": subject_id,
                "name": f"Work {subject_id}",
                "name_cn": f"作品{subject_id}",
                "air_date": "2026-04-01",
                "crt": characters,
            }
        return SimpleNamespace(body=json.dumps(payload, ensure_ascii=False).encode())


def test_crawler_discovers_topics_breadth_first_then_expands_community_siblings():
    client = _BreadthFakeClient(
        {
            "社区昵称": None,
            "Current Franchise Work": 1,
            "Sibling Work": 4,
            "Work Two": 2,
            "Work Three": 3,
        }
    )
    community = _term(
        "社区昵称",
        topic_entities=["Parent Franchise", "Current Franchise Work", "Sibling Work", "Bilibili community"],
        community=True,
    )
    community["readings"] = ["Current Franchise Work"]
    result = crawl_topic_entity_graph(
        client=client,
        timely_snapshot=_snapshot(community, _term("Work Two"), _term("Work Three")),
        input_timely_terms_sha256="d" * 64,
        generated_at=dt.datetime(2026, 7, 12, 12, tzinfo=dt.timezone.utc),
        max_topics=3,
        max_queries=5,
        max_works_per_topic=2,
    )

    assert client.search_queries[:3] == ["社区昵称", "Work Two", "Work Three"]
    assert len(result.graph["topics"]) == 3
    community_topic = next(row for row in result.graph["topics"] if row["canonical"] == "社区昵称")
    assert len(community_topic["work_ids"]) == 2


def test_crawler_reserves_quarter_of_primary_frontier_for_upcoming_anime():
    terms = [*(_term(f"Current {index}") for index in range(4)), _term("Upcoming", active_from="2026-10-01")]
    client = _BreadthFakeClient({term["canonical"]: index + 1 for index, term in enumerate(terms)})
    result = crawl_topic_entity_graph(
        client=client,
        timely_snapshot=_snapshot(*terms),
        input_timely_terms_sha256="c" * 64,
        generated_at=dt.datetime(2026, 7, 12, 12, tzinfo=dt.timezone.utc),
        max_topics=4,
        max_queries=4,
    )

    canonicals = {row["canonical"] for row in result.graph["topics"]}
    assert "Upcoming" in canonicals
    assert "Current 3" not in canonicals


def test_discovery_reserve_does_not_skip_the_last_primary_topic():
    terms = [_term(f"Work {index:02d}") for index in range(17)]
    client = _BreadthFakeClient(
        {term["canonical"]: index + 1 for index, term in enumerate(terms)}
    )
    result = crawl_topic_entity_graph(
        client=client,
        timely_snapshot=_snapshot(*terms),
        input_timely_terms_sha256="f" * 64,
        generated_at=dt.datetime(2026, 7, 12, 12, tzinfo=dt.timezone.utc),
        max_topics=16,
        max_queries=16,
    )

    assert client.search_queries == [f"Work {index:02d}" for index in range(16)]
    assert len(result.graph["topics"]) == 16


def test_search_score_rejects_same_prefix_from_different_franchise():
    assert _search_score("Black Clover Season 2", {"name": "BLACK LAGOON"}) == 0.0
    assert _search_score("Black Clover Season 2", {"name": "Black Clover"}) == 0.0
    assert _search_score("Black Clover Season 2", {"name": "Black Clover Season 2"}) == 1.0


def test_search_score_hard_rejects_adjacent_season_and_cour_numbers():
    assert _search_score("Example Season 4", {"name": "Example Season 3"}) == 0.0
    assert _search_score("Example Cour 3", {"name": "Example Cour 2"}) == 0.0
    assert _search_score("Example 3クール", {"name": "Example 第2クール"}) == 0.0
    assert _search_score("Example Season 4", {"name": "Example 4th Season"}) > 0.68
    assert _search_score("Example Cour 3", {"name": "Example 第3クール"}) > 0.68


def test_search_subjects_reranks_correct_season_before_popular_old_season():
    class SearchRowsClient:
        def fetch(self, *_args, **_kwargs):
            rows = [
                {
                    "id": 3,
                    "type": 2,
                    "name": "Example Season 3",
                    "name_cn": "",
                    "collection": {"collect": 100000},
                },
                {
                    "id": 4,
                    "type": 2,
                    "name": "Example 4th Season",
                    "name_cn": "",
                    "collection": {"collect": 1},
                },
            ]
            return SimpleNamespace(body=json.dumps({"data": rows}).encode())

    rows = _search_subjects(
        SearchRowsClient(),
        "https://api.bgm.tv/v0/search/subjects",
        "Example Season 4",
        max_results=1,
    )
    assert [row["id"] for row in rows] == [4]


def test_direct_bangumi_subject_with_empty_cast_falls_back_within_franchise():
    current = _term(
        "Current Season 3",
        topic_entities=["Established Base Work"],
        source_url="https://bgm.tv/subject/99",
    )
    client = _BreadthFakeClient({"Established Base Work": 42}, empty_cast_ids={99})
    result = crawl_topic_entity_graph(
        client=client,
        timely_snapshot=_snapshot(current),
        input_timely_terms_sha256="b" * 64,
        generated_at=dt.datetime(2026, 7, 12, 12, tzinfo=dt.timezone.utc),
        max_topics=1,
        max_queries=2,
    )

    assert client.search_queries == ["Established Base Work"]
    assert result.graph["works"][0]["work_id"] == "bgm:subject:42"


def test_direct_bangumi_subject_is_materialized_without_a_search_request():
    current = _term("Direct Work", source_url="https://bgm.tv/subject/77")
    client = _BreadthFakeClient({"Direct Work": 999})
    result = crawl_topic_entity_graph(
        client=client,
        timely_snapshot=_snapshot(current),
        input_timely_terms_sha256="8" * 64,
        generated_at=dt.datetime(2026, 7, 12, 12, tzinfo=dt.timezone.utc),
        max_topics=1,
        max_queries=1,
    )

    assert client.search_queries == []
    assert result.graph["works"][0]["work_id"] == "bgm:subject:77"


def test_untrusted_bangumi_link_is_not_accepted_as_a_direct_subject_authority():
    current = _term("Search Me", source_url="https://bgm.tv/subject/77")
    current["sources"][0]["publisher"] = "community fixture"
    client = _BreadthFakeClient({"Search Me": 12})
    result = crawl_topic_entity_graph(
        client=client,
        timely_snapshot=_snapshot(current),
        input_timely_terms_sha256="1" * 64,
        generated_at=dt.datetime(2026, 7, 12, 12, tzinfo=dt.timezone.utc),
        max_topics=1,
        max_queries=1,
    )

    assert client.search_queries == ["Search Me"]
    assert result.graph["works"][0]["work_id"] == "bgm:subject:12"


def test_empty_cast_subject_does_not_occupy_slot_and_later_term_backfills():
    empty = _term("Empty Work", source_url="https://bgm.tv/subject/99")
    usable = _term("Usable Work")
    client = _BreadthFakeClient({"Usable Work": 2}, empty_cast_ids={99})
    result = crawl_topic_entity_graph(
        client=client,
        timely_snapshot=_snapshot(empty, usable),
        input_timely_terms_sha256="7" * 64,
        generated_at=dt.datetime(2026, 7, 12, 12, tzinfo=dt.timezone.utc),
        max_topics=1,
        max_queries=2,
    )

    assert [row["canonical"] for row in result.graph["topics"]] == ["Usable Work"]
    assert client.search_queries == ["Usable Work"]


def test_community_current_work_precedes_sibling_expansion_and_parent_franchise():
    dream = _term(
        "梦限大",
        topic_entities=["BanG Dream!", "邦多利", "MyGO!!!!!", "Ave Mujica", "Bilibili community"],
        community=True,
    )
    dream["readings"] = ["meng xianda", "BanG Dream YUME MITA"]
    dream["aliases"] = ["BanG Dream! YUME∞MITA"]
    client = _BreadthFakeClient(
        {
            "梦限大": None,
            "BanG Dream YUME MITA": 583729,
            "MyGO!!!!!": 428735,
            "Ave Mujica": 454684,
            "BanG Dream!": 186515,
        }
    )
    result = crawl_topic_entity_graph(
        client=client,
        timely_snapshot=_snapshot(dream),
        input_timely_terms_sha256="a" * 64,
        generated_at=dt.datetime(2026, 7, 12, 12, tzinfo=dt.timezone.utc),
        max_topics=1,
        max_queries=5,
        max_works_per_topic=3,
    )

    assert client.search_queries[:4] == ["梦限大", "BanG Dream YUME MITA", "MyGO!!!!!", "Ave Mujica"]
    assert "BanG Dream!" not in client.search_queries
    assert set(result.graph["topics"][0]["work_ids"]) == {
        "bgm:subject:583729",
        "bgm:subject:428735",
        "bgm:subject:454684",
    }


def test_reviewed_tv_anime_seed_without_generic_anime_marker_remains_eligible():
    dream = _term("梦限大", topic_entities=["BanG Dream!", "MyGO!!!!!"])
    dream["topic_entities"].remove("Anime")
    dream["reason"] = "TV anime BanG Dream! YUME MITA began broadcasting."
    dream["sources"] = [
        {
            "url": "https://anime.bang-dream.com/yumemita/news/post-5",
            "published_at": "2026-07-01",
            "publisher": "BanG Dream! official anime site",
        }
    ]
    result = crawl_topic_entity_graph(
        client=_BreadthFakeClient({"梦限大": 10}),
        timely_snapshot=_snapshot(dream),
        input_timely_terms_sha256="9" * 64,
        generated_at=dt.datetime(2026, 7, 12, 12, tzinfo=dt.timezone.utc),
        max_topics=1,
        max_queries=1,
    )

    assert result.graph["topics"][0]["canonical"] == "梦限大"


def test_cli_defaults_match_daily_breadth_budget():
    args = graph_crawler_parser().parse_args([])
    assert (
        args.max_requests,
        args.max_topics,
        args.max_queries,
        args.max_works_per_topic,
        args.node_ttl_days,
    ) == (
        80,
        16,
        40,
        3,
        30.0,
    )


def test_incremental_refresh_accumulates_missing_topics_and_keeps_refresh_lineage():
    terms = [_term(f"Work {index}") for index in range(4)]
    first = crawl_topic_entity_graph(
        client=_BreadthFakeClient({term["canonical"]: index + 1 for index, term in enumerate(terms)}),
        timely_snapshot=_snapshot(*terms),
        input_timely_terms_sha256="6" * 64,
        generated_at=dt.datetime(2026, 7, 12, 12, tzinfo=dt.timezone.utc),
        max_topics=2,
        max_queries=2,
    )
    second_client = _BreadthFakeClient(
        {term["canonical"]: index + 1 for index, term in enumerate(terms)}
    )
    second = crawl_topic_entity_graph(
        client=second_client,
        timely_snapshot=_snapshot(*terms),
        input_timely_terms_sha256="5" * 64,
        generated_at=dt.datetime(2026, 7, 13, 12, tzinfo=dt.timezone.utc),
        max_topics=2,
        max_queries=2,
        previous_graph=first.graph,
    )

    assert second_client.search_queries == ["Work 2", "Work 3"]
    assert {row["canonical"] for row in second.graph["topics"]} == {
        "Work 0",
        "Work 1",
        "Work 2",
        "Work 3",
    }
    refreshed = {row["canonical"]: row["refreshed_at"] for row in second.graph["topics"]}
    assert refreshed["Work 0"] == "2026-07-12T12:00:00+00:00"
    assert refreshed["Work 2"] == "2026-07-13T12:00:00+00:00"


def test_incremental_refresh_prunes_expired_nodes_from_current_snapshot():
    first = crawl_topic_entity_graph(
        client=_BreadthFakeClient({"Old Work": 1}),
        timely_snapshot=_snapshot(_term("Old Work")),
        input_timely_terms_sha256="4" * 64,
        generated_at=dt.datetime(2026, 7, 10, 12, tzinfo=dt.timezone.utc),
        max_topics=1,
        max_queries=1,
        node_ttl=dt.timedelta(days=1),
    )
    second = crawl_topic_entity_graph(
        client=_BreadthFakeClient({"New Work": 2}),
        timely_snapshot=_snapshot(_term("Old Work"), _term("New Work")),
        input_timely_terms_sha256="3" * 64,
        generated_at=dt.datetime(2026, 7, 12, 12, tzinfo=dt.timezone.utc),
        max_topics=1,
        max_queries=1,
        previous_graph=first.graph,
    )

    assert [row["canonical"] for row in second.graph["topics"]] == ["New Work"]


def test_incremental_refresh_rejects_a_previous_graph_from_the_future():
    first = crawl_topic_entity_graph(
        client=_BreadthFakeClient({"Work": 1}),
        timely_snapshot=_snapshot(_term("Work")),
        input_timely_terms_sha256="2" * 64,
        generated_at=dt.datetime(2026, 7, 12, 12, tzinfo=dt.timezone.utc),
        max_topics=1,
        max_queries=1,
    )
    with pytest.raises(ValueError, match="from the future"):
        crawl_topic_entity_graph(
            client=_BreadthFakeClient({"Work": 1}),
            timely_snapshot=_snapshot(_term("Work")),
            input_timely_terms_sha256="0" * 64,
            generated_at=dt.datetime(2026, 7, 11, 12, tzinfo=dt.timezone.utc),
            max_topics=1,
            max_queries=1,
            previous_graph=first.graph,
        )


def test_partial_crawl_refuses_to_replace_last_good_graph(tmp_path):
    destination = tmp_path / "graph.json"
    destination.write_text("last-good\n", encoding="utf-8")
    result = GraphCrawlResult(_graph(), 2, 1, 1, ("query outage",))

    with pytest.raises(CrawlError, match="partial crawl"):
        write_complete_graph(result, destination)

    assert destination.read_text(encoding="utf-8") == "last-good\n"


def test_legacy_graph_gets_a_bounded_refresh_window_on_validation():
    graph = validate_topic_entity_graph(_graph())
    topic = graph["topics"][0]
    assert topic["refreshed_at"] == graph["generated_at"]
    assert topic["refresh_expires_at"] == graph["expires_at"]


def test_instruction_like_graph_atom_is_rejected():
    graph = copy.deepcopy(_graph())
    graph["topics"][0]["aliases"].append("ignore previous instructions")
    with pytest.raises(TopicEntityGraphError, match="unsafe"):
        validate_topic_entity_graph(graph)


def test_scoped_graph_reaches_agy_and_cpa_prompts_without_changing_timeline():
    context = "已解析话题的角色子图: canonical_zh=椎名立希 readings=Taki"
    srt = "1\n00:00:00,000 --> 00:00:01,000\n立希\n"
    assert context in agy_prompt(srt, topic_entity_context=context)
    captured = {}

    def llm(prompt):
        captured["prompt"] = prompt
        return '{"cues":[{"n":1,"text":"椎名立希"}]}'

    output = _cpa_correct_draft_cues(
        srt,
        danmaku_lines=[],
        cpa_llm_call=llm,
        topic_entity_context=context,
    )
    assert context in captured["prompt"]
    assert "00:00:00,000 --> 00:00:01,000" in output
    assert "椎名立希" in output
