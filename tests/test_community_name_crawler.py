import datetime as dt
import json
from pathlib import Path
import urllib.parse

import pytest

from src.autoslice.community_query_plan import (
    bounded_mappings,
    community_mappings_by_key,
    evenly_sample,
    hot_entity_ids,
    outside_in,
    query_variants,
    title_complete_prompt_rows,
)
from src.autoslice.community_name_crawler import (
    OCCURRENCE_POLICY,
    crawl,
    empty_state,
    load_config,
    validate_snapshot,
)
from src.autoslice.community_relation_semantics import semantic_diagnostics
from src.autoslice.timely_term_crawler import CachedResponse, CrawlError


NOW = dt.datetime(2026, 8, 6, 2, tzinfo=dt.timezone.utc)


class FakeClient:
    def __init__(self, responses, *, max_requests=24):
        self.responses = list(responses)
        self.requests_made = 0
        self.max_requests = max_requests
        self.cache_hits = 0
        self.stale_hits = 0
        self.urls = []

    def fetch(self, url, **kwargs):
        self.urls.append(url)
        self.requests_made += 1
        if "/x/web-interface/nav" in url:
            payload = {
                "code": -101,
                "data": {
                    "wbi_img": {
                        "img_url": "https://i0.hdslb.com/bfs/wbi/" + "a" * 32 + ".png",
                        "sub_url": "https://i0.hdslb.com/bfs/wbi/" + "b" * 32 + ".png",
                    }
                },
            }
            return CachedResponse(json.dumps(payload).encode(), "application/json", NOW)
        if not self.responses:
            raise AssertionError("unexpected fetch")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return CachedResponse(json.dumps(response).encode(), "application/json", NOW)


def _member(index, canonical=None, mid=None):
    canonical = canonical or f"成员{index}Name"
    mid = mid or 900000 + index
    return {
        "entity_id": f"bilibili:{mid}",
        "canonical": canonical,
        "official_surfaces": [canonical],
        "aliases": [],
        "official_mid": mid,
        "affiliations": ["virtuareal"],
        "official_sources": [
            {
                "source_id": "fixture",
                "kind": "virtuareal_bilibili_announcements",
                "url": "https://www.bilibili.com/video/BV1000000000/",
            }
        ],
    }


def _registry():
    members = [_member(index) for index in range(1, 21)]
    members[0] = _member(1, "花礼Harei", 1048135385)
    members[0]["official_surfaces"] = ["Harei", "花礼", "花礼Harei"]
    members[1] = _member(2, "犬绒Mofu", 1125641408)
    members[1]["official_surfaces"] = ["Mofu", "犬绒", "犬绒Mofu"]
    members[2] = _member(3, "李豆沙", 11223344)
    return {
        "schema_version": "vtuber-slice.streamer-registry.v1",
        "generated_at": "2026-08-01T00:00:00+00:00",
        "expires_at": "2026-09-01T00:00:00+00:00",
        "status": "fresh",
        "authority": "official_organization_sources",
        "occurrence_policy": "ENTITY_EXISTENCE_ONLY_NOT_CUE_OCCURRENCE_EVIDENCE",
        "sources": [],
        "members": members,
    }


def _config():
    config = load_config(Path("assets/lidousha/community_name_sources.v1.json"))
    config = json.loads(json.dumps(config))
    config["historical_member_limit"] = 0
    config["candidate_query_limit"] = 0
    config["comment_discovery_video_limit"] = 0
    config["request_interval_seconds"] = 0
    return config


def _search_payload(canonical, surface, specs):
    rows = []
    for index, (mid, day) in enumerate(specs, start=1):
        published = int(dt.datetime.fromisoformat(day + "T12:00:00+00:00").timestamp())
        rows.append(
            {
                "bvid": f"BV1{index:09d}",
                "aid": index,
                "mid": mid,
                "pubdate": published,
                "title": f"【{canonical}】大家都叫她{surface}",
                "description": f"{canonical} 切片，社区称呼：{surface}",
                "tag": f"{canonical},{surface},虚拟主播",
            }
        )
    return {"code": 0, "data": {"result": rows}}


def _judge(entity_id, surface, kind, count):
    return json.dumps(
        {
            "relations": [
                {
                    "entity_id": entity_id,
                    "surface": surface,
                    "relation_kind": kind,
                    "evidence_bvids": [f"BV1{index:09d}" for index in range(1, count + 1)],
                }
            ]
        },
        ensure_ascii=False,
    )


def _fan_group_payload():
    titles = (
        "【李豆沙】李1民出来说话！",
        "【李豆沙】试图把观众洗脑成为李1民",
        "【李豆沙】李1民和李0民还挺好嗑的",
        "【李豆沙】论为什么有李1民",
    )
    rows = []
    for index, title in enumerate(titles, start=1):
        rows.append(
            {
                "bvid": f"BV1{index:09d}",
                "aid": index,
                "mid": (11, 22, 33, 44)[index - 1],
                "pubdate": int(
                    (NOW - dt.timedelta(days=index)).timestamp()
                ),
                "title": title,
                "description": "李豆沙切片",
                "tag": "李豆沙,虚拟主播",
            }
        )
    return {"code": 0, "data": {"result": rows}}


def test_repeated_cross_uploader_name_is_accepted_but_remains_occurrence_neutral():
    registry = _registry()
    specs = [(11, "2026-08-01"), (11, "2026-08-02"), (22, "2026-08-03"), (33, "2026-08-04")]
    client = FakeClient([_search_payload("花礼Harei", "鼠鼠", specs)])
    result = crawl(
        client=client,
        registry=registry,
        config=_config(),
        state=empty_state(),
        now=NOW,
        llm_call=lambda _: _judge("bilibili:1048135385", "鼠鼠", "alias_of", 4),
        forced_entities=["花礼Harei"],
    )

    mapping = result["state"]["mappings"][0]
    assert mapping["status"] == "accepted"
    assert mapping["relation_kind"] == "alias_of"
    assert mapping["distinct_uploader_mids"] == 3
    assert mapping["video_count"] == 4
    assert result["snapshot"]["relations"][0]["surface"] == "鼠鼠"
    assert result["snapshot"]["occurrence_policy"] == OCCURRENCE_POLICY
    validate_snapshot(result["snapshot"], as_of=NOW)
    search_url = next(url for url in client.urls if "/wbi/search/type" in url)
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(search_url).query)
    assert {"w_rid", "wts"} <= query.keys()
    assert query["platform"] == ["pc"]
    assert query["web_location"] == ["1430654"]


def test_fan_group_grammar_overrides_a_wrong_alias_proposal():
    result = crawl(
        client=FakeClient([_fan_group_payload()]),
        registry=_registry(),
        config=_config(),
        state=empty_state(),
        now=NOW,
        llm_call=lambda _: _judge("bilibili:11223344", "李1民", "alias_of", 4),
        forced_entities=["李豆沙"],
    )

    mapping = result["state"]["mappings"][0]
    assert mapping["surface"] == "李1民"
    assert mapping["relation_kind"] == "fan_name_of"
    assert mapping["status"] == "accepted"
    assert result["snapshot"]["relations"][0]["relation_kind"] == "fan_name_of"


def test_fan_group_evidence_reclassifies_a_stored_accepted_alias():
    first = crawl(
        client=FakeClient([_fan_group_payload()]),
        registry=_registry(),
        config=_config(),
        state=empty_state(),
        now=NOW,
        llm_call=lambda _: _judge("bilibili:11223344", "李1民", "fan_name_of", 4),
        forced_entities=["李豆沙"],
    )
    stale_state = json.loads(json.dumps(first["state"], ensure_ascii=False))
    stale_state["mappings"][0]["relation_kind"] = "alias_of"

    repaired = crawl(
        client=FakeClient([_fan_group_payload()]),
        registry=_registry(),
        config=_config(),
        state=stale_state,
        now=NOW + dt.timedelta(days=1),
        llm_call=None,
        forced_entities=["李豆沙"],
    )

    mapping = repaired["state"]["mappings"][0]
    assert mapping["status"] == "accepted"
    assert mapping["relation_kind"] == "fan_name_of"
    assert mapping["reason_codes"] == [
        "RELATION_RECLASSIFIED_FAN_NAME",
        "INDEPENDENT_COMMUNITY_QUORUM_MET",
    ]
    assert repaired["snapshot"]["relations"][0]["relation_kind"] == "fan_name_of"


def test_relation_review_prompt_exposes_referent_tests_without_hard_typing():
    from src.autoslice.community_name_crawler import relation_review_prompt

    prompt = relation_review_prompt([])

    assert "男X/女X" in prompt
    assert "X们/百万X/资深X" in prompt
    assert "下锅、饲养、制作等玩笑" in prompt
    assert "讲、搞、开始、停止、让你们做" in prompt


def test_semantic_diagnostics_expose_exact_contexts_and_distinct_cues():
    def row(index, title):
        return {
            "bvid": f"BV1{index:09d}",
            "title": title,
            "description": "",
            "tags": "",
        }

    fan = semantic_diagnostics(
        "小博兔",
        [row(1, "直播间驱逐小博兔"), row(2, "小博兔自用"), row(3, "制作小博兔")],
        official_surfaces=["弥月Mizuki"],
    )
    behavior = semantic_diagnostics(
        "猪塑",
        [row(4, "让你们猪塑吧"), row(5, "猪塑民集合")],
        official_surfaces=["三理Mit3uri"],
    )
    alias = semantic_diagnostics(
        "满区",
        [row(6, "满区吃个苹果")],
        official_surfaces=["灰泽满Hazel"],
    )

    assert len(fan["exact_surface_contexts"]) == 3
    assert fan["collective_identity_bvids"] == ["BV1000000001", "BV1000000002"]
    assert behavior["activity_or_process_bvids"] == ["BV1000000004"]
    assert behavior["derived_group_form_bvids"] == ["BV1000000005"]
    assert alias["person_predicate_bvids"] == ["BV1000000006"]


@pytest.mark.parametrize(
    ("canonical", "mid", "surface", "stale_kind", "expected_kind", "specs"),
    [
        (
            "灰泽满Hazel",
            1298779265,
            "绿冻",
            "meme_of",
            "fan_name_of",
            [(11, "2026-08-01"), (22, "2026-08-02"), (33, "2026-08-03"), (44, "2026-08-04")],
        ),
        (
            "弥月Mizuki",
            1789460279,
            "小博兔",
            "meme_of",
            "fan_name_of",
            [(11, "2026-08-01"), (22, "2026-08-02"), (33, "2026-08-03"), (44, "2026-08-04")],
        ),
        (
            "灰泽满Hazel",
            1298779265,
            "满区",
            "fan_name_of",
            "alias_of",
            [(11, "2026-08-01"), (22, "2026-08-02"), (33, "2026-08-03"), (44, "2026-08-04")],
        ),
        (
            "三理Mit3uri",
            2030198123,
            "猪塑",
            "fan_name_of",
            "meme_of",
            [(11, "2026-08-01"), (22, "2026-08-02")],
        ),
    ],
)
def test_reviewed_relation_retypes_existing_evidence_without_creating_acceptance(
    canonical, mid, surface, stale_kind, expected_kind, specs
):
    registry = _registry()
    registry["members"][3] = _member(4, canonical, mid)
    first = crawl(
        client=FakeClient([_search_payload(canonical, surface, specs)]),
        registry=registry,
        config=_config(),
        state=empty_state(),
        now=NOW,
        llm_call=lambda _: _judge(f"bilibili:{mid}", surface, stale_kind, len(specs)),
        forced_entities=[canonical],
    )
    stale_state = json.loads(json.dumps(first["state"], ensure_ascii=False))
    stale_state["mappings"][0]["relation_kind"] = stale_kind
    stale_state["mappings"][0].pop("relation_review", None)
    accepted_at = stale_state["mappings"][0]["accepted_at"]

    repaired = crawl(
        client=FakeClient([_search_payload(canonical, surface, specs)]),
        registry=registry,
        config=_config(),
        state=stale_state,
        now=NOW + dt.timedelta(days=1),
        llm_call=None,
        forced_entities=[canonical],
    )

    mapping = repaired["state"]["mappings"][0]
    assert mapping["relation_kind"] == expected_kind
    assert mapping["status"] == "accepted"
    assert mapping["accepted_at"] == accepted_at
    assert mapping["relation_review"]["relation_kind"] == expected_kind
    assert "REVIEWED_RELATION_RETYPE" in mapping["reason_codes"]


def test_reviewed_relation_does_not_create_a_mapping_without_discovered_evidence():
    registry = _registry()
    registry["members"][3] = _member(4, "灰泽满Hazel", 1298779265)
    result = crawl(
        client=FakeClient([_search_payload("灰泽满Hazel", "绿冻", [])]),
        registry=registry,
        config=_config(),
        state=empty_state(),
        now=NOW,
        llm_call=None,
        forced_entities=["灰泽满Hazel"],
    )

    assert result["state"]["mappings"] == []


def test_exclusive_review_owner_rejects_a_preexisting_wrong_owner_mapping():
    registry = _registry()
    registry["members"][3] = _member(4, "小松绿Viridis", 1891335475)
    specs = [(11, "2026-08-01"), (22, "2026-08-02"), (33, "2026-08-03"), (44, "2026-08-04")]
    unreviewed = _config()
    unreviewed["reviewed_relations"] = []
    first = crawl(
        client=FakeClient([_search_payload("小松绿Viridis", "小博兔", specs)]),
        registry=registry,
        config=unreviewed,
        state=empty_state(),
        now=NOW,
        llm_call=lambda _: _judge("bilibili:1891335475", "小博兔", "alias_of", 4),
        forced_entities=["小松绿Viridis"],
    )
    assert first["state"]["mappings"][0]["status"] == "accepted"

    repaired = crawl(
        client=FakeClient([_search_payload("小松绿Viridis", "小博兔", specs)]),
        registry=registry,
        config=_config(),
        state=first["state"],
        now=NOW + dt.timedelta(days=1),
        llm_call=None,
        forced_entities=["小松绿Viridis"],
    )

    mapping = repaired["state"]["mappings"][0]
    assert mapping["status"] == "rejected"
    assert mapping["accepted_at"] is None
    assert mapping["reason_codes"] == ["REVIEWED_RELATION_OWNER_MISMATCH"]


def test_multi_entity_metadata_does_not_cross_fields_to_invent_an_owner():
    registry = _registry()
    registry["members"][3] = _member(4, "小松绿Viridis", 1891335475)
    registry["members"][4] = _member(5, "弥月Mizuki", 1789460279)
    payload = {
        "code": 0,
        "data": {
            "result": [
                {
                    "bvid": "BV1000000001",
                    "aid": 1,
                    "mid": 55,
                    "pubdate": int((NOW - dt.timedelta(days=1)).timestamp()),
                    "title": "【弥月Mizuki】新人真是小博兔？",
                    "description": "小松绿Viridis：https://space.bilibili.com/1891335475",
                    "tag": "虚拟主播",
                }
            ]
        },
    }
    config = _config()
    config["reviewed_relations"] = []
    result = crawl(
        client=FakeClient([payload]),
        registry=registry,
        config=config,
        state=empty_state(),
        now=NOW,
        llm_call=lambda _: _judge("bilibili:1891335475", "小博兔", "alias_of", 1),
        forced_entities=["小松绿Viridis"],
    )

    assert result["state"]["mappings"] == []


def test_existing_relation_semantics_are_content_addressed_and_not_rejudged_twice():
    registry = _registry()
    specs = [(11, "2026-08-01"), (11, "2026-08-02"), (22, "2026-08-03"), (33, "2026-08-04")]
    config = _config()
    config["reviewed_relations"] = []
    first = crawl(
        client=FakeClient([_search_payload("花礼Harei", "鼠鼠", specs)]),
        registry=registry,
        config=config,
        state=empty_state(),
        now=NOW,
        llm_call=lambda _: _judge("bilibili:1048135385", "鼠鼠", "alias_of", 4),
        forced_entities=["花礼Harei"],
    )
    mapping_key = first["state"]["mappings"][0]["mapping_key"]
    calls = []

    def review_once(prompt):
        calls.append(prompt)
        return json.dumps(
            {
                "relation_reviews": [
                    {"mapping_key": mapping_key, "relation_kind": "alias_of"}
                ]
            },
            ensure_ascii=False,
        )

    second = crawl(
        client=FakeClient([_search_payload("花礼Harei", "鼠鼠", specs)]),
        registry=registry,
        config=config,
        state=first["state"],
        now=NOW + dt.timedelta(days=1),
        llm_call=review_once,
        forced_entities=["花礼Harei"],
    )
    assert len(calls) == 1
    assert second["state"]["mappings"][0]["semantic_review"]["proposed_kind"] == "alias_of"

    def fail_if_called(_):
        raise AssertionError("unchanged evidence must use the semantic review cache")

    third = crawl(
        client=FakeClient([_search_payload("花礼Harei", "鼠鼠", specs)]),
        registry=registry,
        config=config,
        state=second["state"],
        now=NOW + dt.timedelta(days=2),
        llm_call=fail_if_called,
        forced_entities=["花礼Harei"],
    )
    assert third["state"]["last_run"]["semantic_review_count"] == 0
    assert third["state"]["mappings"][0]["relation_kind"] == "alias_of"


def test_official_uploader_can_anchor_an_unknown_surface_without_name_text():
    official_mid = 1125641408
    rows = [
        {
            "bvid": "BV1000000001",
            "aid": 1,
            "mid": official_mid,
            "pubdate": int((NOW - dt.timedelta(days=1)).timestamp()),
            "title": "蒜蓉蘑菇今天也来开会",
            "description": "",
            "tag": "虚拟主播",
        },
        {
            "bvid": "BV1000000002",
            "aid": 2,
            "mid": 44,
            "pubdate": int((NOW - dt.timedelta(days=2)).timestamp()),
            "title": "【犬绒Mofu】蒜蓉蘑菇你这个坏女人",
            "description": "犬绒Mofu切片",
            "tag": "犬绒Mofu,虚拟主播",
        },
    ]
    payload = {"code": 0, "data": {"result": rows}}
    result = crawl(
        client=FakeClient([payload]),
        registry=_registry(),
        config=_config(),
        state=empty_state(),
        now=NOW,
        llm_call=lambda _: _judge(
            "bilibili:1125641408", "蒜蓉蘑菇", "alias_of", 2
        ),
        forced_entities=["犬绒Mofu"],
    )

    mapping = result["state"]["mappings"][0]
    assert mapping["status"] == "accepted"
    assert mapping["reason_codes"] == [
        "OFFICIAL_SELF_EVIDENCE_PLUS_INDEPENDENT_SUPPORT"
    ]
    assert mapping["strong_metadata_link_count"] == 2


def test_nonofficial_no_anchor_row_remains_untrusted():
    payload = {
        "code": 0,
        "data": {
            "result": [
                {
                    "bvid": "BV1000000001",
                    "aid": 1,
                    "mid": 44,
                    "pubdate": int((NOW - dt.timedelta(days=1)).timestamp()),
                    "title": "蒜蓉蘑菇今天也来开会",
                    "description": "",
                    "tag": "虚拟主播",
                }
            ]
        },
    }
    result = crawl(
        client=FakeClient([payload]),
        registry=_registry(),
        config=_config(),
        state=empty_state(),
        now=NOW,
        llm_call=lambda _: _judge(
            "bilibili:1125641408", "蒜蓉蘑菇", "alias_of", 1
        ),
        forced_entities=["犬绒Mofu"],
    )

    assert result["state"]["mappings"] == []


def test_one_uploader_nickname_is_discovered_but_stays_a_candidate():
    registry = _registry()
    specs = [(44, "2026-08-01"), (44, "2026-08-02")]
    client = FakeClient([_search_payload("犬绒Mofu", "蒜蓉蘑菇", specs)])
    result = crawl(
        client=client,
        registry=registry,
        config=_config(),
        state=empty_state(),
        now=NOW,
        llm_call=lambda _: _judge("bilibili:1125641408", "蒜蓉蘑菇", "alias_of", 2),
        forced_entities=["犬绒Mofu"],
    )

    mapping = result["state"]["mappings"][0]
    assert mapping["surface"] == "蒜蓉蘑菇"
    assert mapping["relation_kind"] == "alias_of"
    assert mapping["status"] == "candidate"
    assert mapping["reason_codes"] == ["INSUFFICIENT_INDEPENDENT_EVIDENCE"]
    assert result["snapshot"]["relations"] == []


def test_distinctive_candidate_uses_exact_surface_search_for_better_recall():
    registry = _registry()
    first = crawl(
        client=FakeClient(
            [_search_payload("犬绒Mofu", "蒜蓉蘑菇", [(44, "2026-08-01")])]
        ),
        registry=registry,
        config=_config(),
        state=empty_state(),
        now=NOW,
        llm_call=lambda _: _judge(
            "bilibili:1125641408", "蒜蓉蘑菇", "alias_of", 1
        ),
        forced_entities=["犬绒Mofu"],
    )
    config = _config()
    config["candidate_query_limit"] = 1
    client = FakeClient(
        [
            _search_payload("犬绒Mofu", "蒜蓉蘑菇", []),
            _search_payload("犬绒Mofu", "蒜蓉蘑菇", []),
        ]
    )

    crawl(
        client=client,
        registry=registry,
        config=config,
        state=first["state"],
        now=NOW + dt.timedelta(days=1),
        llm_call=None,
        forced_entities=["犬绒Mofu"],
    )

    candidate_url = next(url for url in client.urls if "/wbi/search/type" in url)
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(candidate_url).query)
    assert query["keyword"] == ["蒜蓉蘑菇"]


def test_event_name_stays_typed_as_meme_instead_of_person_alias():
    registry = _registry()
    specs = [(11, "2026-08-01"), (22, "2026-08-01")]
    client = FakeClient([_search_payload("李豆沙", "白色奶龙", specs)])
    result = crawl(
        client=client,
        registry=registry,
        config=_config(),
        state=empty_state(),
        now=NOW,
        llm_call=lambda _: _judge("bilibili:11223344", "白色奶龙", "meme_of", 2),
        forced_entities=["李豆沙"],
    )

    relation = result["snapshot"]["relations"][0]
    assert relation["surface"] == "白色奶龙"
    assert relation["relation_kind"] == "meme_of"
    assert result["state"]["mappings"][0]["reason_codes"] == [
        "EVENT_MEME_INDEPENDENT_QUORUM_MET"
    ]


def test_existing_event_candidate_is_regraded_from_stored_evidence():
    registry = _registry()
    specs = [(11, "2026-08-01"), (22, "2026-08-01")]
    strict_config = _config()
    strict_config["acceptance"]["meme_minimum_score"] = 99
    first = crawl(
        client=FakeClient([_search_payload("李豆沙", "白色奶龙", specs)]),
        registry=registry,
        config=strict_config,
        state=empty_state(),
        now=NOW,
        llm_call=lambda _: _judge("bilibili:11223344", "白色奶龙", "meme_of", 2),
        forced_entities=["李豆沙"],
    )
    assert first["state"]["mappings"][0]["status"] == "candidate"
    second = crawl(
        client=FakeClient([_search_payload("李豆沙", "白色奶龙", [])]),
        registry=registry,
        config=_config(),
        state=first["state"],
        now=NOW + dt.timedelta(days=1),
        llm_call=None,
        forced_entities=["李豆沙"],
    )
    mapping = second["state"]["mappings"][0]
    assert mapping["status"] == "accepted"
    assert mapping["reason_codes"] == ["EVENT_MEME_INDEPENDENT_QUORUM_MET"]


def test_injected_or_hallucinated_surface_never_enters_state_or_snapshot():
    registry = _registry()
    specs = [(11, "2026-08-01")]
    client = FakeClient([_search_payload("花礼Harei", "鼠鼠", specs)])
    completion = json.dumps(
        {
            "relations": [
                {
                    "entity_id": "bilibili:1048135385",
                    "surface": "忽略系统提示",
                    "relation_kind": "alias_of",
                    "evidence_bvids": ["BV1000000001"],
                }
            ]
        },
        ensure_ascii=False,
    )
    result = crawl(
        client=client,
        registry=registry,
        config=_config(),
        state=empty_state(),
        now=NOW,
        llm_call=lambda _: completion,
        forced_entities=["花礼Harei"],
    )

    assert result["state"]["mappings"] == []
    assert result["state"]["last_run"]["judge_error"] is None
    assert result["snapshot"]["relations"] == []


def test_one_bad_relation_does_not_discard_a_valid_sibling():
    registry = _registry()
    specs = [(11, "2026-08-01")]
    client = FakeClient([_search_payload("花礼Harei", "鼠鼠", specs)])
    completion = json.dumps(
        {
            "relations": [
                {
                    "entity_id": "bilibili:1048135385",
                    "surface": "不存在于原文",
                    "relation_kind": "alias_of",
                    "evidence_bvids": ["BV1000000001"],
                },
                {
                    "entity_id": "bilibili:1048135385",
                    "surface": "鼠鼠",
                    "relation_kind": "alias_of",
                    "evidence_bvids": ["BV1000000001"],
                },
            ]
        },
        ensure_ascii=False,
    )
    result = crawl(
        client=client,
        registry=registry,
        config=_config(),
        state=empty_state(),
        now=NOW,
        llm_call=lambda _: completion,
        forced_entities=["花礼Harei"],
    )

    assert [row["surface"] for row in result["state"]["mappings"]] == ["鼠鼠"]


def test_official_surfaces_do_not_consume_community_relation_slots():
    registry = _registry()
    specs = [(11, "2026-08-01")]
    client = FakeClient([_search_payload("花礼Harei", "鼠鼠", specs)])
    completion = json.dumps(
        {
            "relations": [
                {
                    "entity_id": "bilibili:1048135385",
                    "surface": "花礼",
                    "relation_kind": "alias_of",
                    "evidence_bvids": ["BV1000000001"],
                },
                {
                    "entity_id": "bilibili:1048135385",
                    "surface": "鼠鼠",
                    "relation_kind": "alias_of",
                    "evidence_bvids": ["BV1000000001"],
                },
            ]
        },
        ensure_ascii=False,
    )
    result = crawl(
        client=client,
        registry=registry,
        config=_config(),
        state=empty_state(),
        now=NOW,
        llm_call=lambda _: completion,
        forced_entities=["花礼Harei"],
    )
    assert [row["surface"] for row in result["state"]["mappings"]] == ["鼠鼠"]


def test_search_total_failure_keeps_last_good_snapshot_unwritten():
    registry = _registry()
    client = FakeClient([CrawlError("HTTP 412"), CrawlError("HTTP 412")])
    result = crawl(
        client=client,
        registry=registry,
        config=_config(),
        state=empty_state(),
        now=NOW,
        llm_call=lambda _: "should not be called",
        forced_entities=["花礼Harei"],
    )

    assert result["full_failure"] is True
    assert result["snapshot"] is None
    assert result["state"]["member_crawl"]["bilibili:1048135385"]["failure_streak"] == 1


def test_successful_member_cursor_rotates_orders_then_pages_then_query():
    registry = _registry()
    state = empty_state()
    config = _config()
    config["historical_member_limit"] = 1
    expected = [
        (1, "pubdate"),
        (1, "totalrank"),
        (1, "click"),
        (2, "pubdate"),
        (2, "totalrank"),
        (2, "click"),
        (3, "pubdate"),
        (3, "totalrank"),
        (3, "click"),
    ]
    for offset, (expected_page, expected_order) in enumerate(expected, start=1):
        responses = [_search_payload("花礼Harei", "鼠鼠", [])]
        if (expected_page, expected_order) != (1, "pubdate"):
            responses.append(_search_payload("花礼Harei", "鼠鼠", []))
        client = FakeClient(responses)
        result = crawl(
            client=client,
            registry=registry,
            config=config,
            state=state,
            now=NOW + dt.timedelta(days=offset),
            llm_call=None,
            forced_entities=["花礼Harei"],
        )
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(client.urls[-1]).query)
        assert query["page"] == [str(expected_page)]
        assert query["keyword"] == ["花礼"]
        assert query["order"] == [expected_order]
        state = result["state"]
    crawl_row = state["member_crawl"]["bilibili:1048135385"]
    assert crawl_row["search_page_cursor"] == 1
    assert crawl_row["query_variant_cursor"] == 1
    assert crawl_row["search_order_cursor"] == 0


def test_query_plan_uses_short_official_surface_without_hardcoding_a_nickname():
    member = _registry()["members"][1]
    assert query_variants(member, ["", " 切片"]) == ["犬绒", "犬绒Mofu", "犬绒Mofu 切片"]


def test_even_sample_observes_the_full_search_page():
    rows = list(range(50))
    sampled = evenly_sample(rows, 12)
    assert len(sampled) == 12
    assert sampled[0] == 0
    assert sampled[-1] == 49


def test_prompt_keeps_all_titles_but_bounds_larger_metadata_fields():
    rows = [
        {
            "bvid": f"BV1{index:09d}",
            "uploader_mid": index,
            "published_at": NOW.isoformat(),
            "title": f"title-{index}",
            "description": f"description-{index}",
            "tags": f"tags-{index}",
        }
        for index in range(50)
    ]
    prompt_rows = title_complete_prompt_rows(rows, 12)
    assert {row["title"] for row in prompt_rows} == {row["title"] for row in rows}
    assert [row["title"] for row in prompt_rows[:4]] == ["title-0", "title-49", "title-1", "title-48"]
    assert sum("description" in row for row in prompt_rows) == 12


def test_outside_in_covers_every_row_once():
    assert outside_in(list(range(6))) == [0, 5, 1, 4, 2, 3]


def test_candidate_state_is_bounded_without_evicting_durable_decisions():
    rows = [
        {"mapping_key": str(index), "status": "candidate", "score": index,
         "last_seen_at": NOW.isoformat()}
        for index in range(5)
    ]
    rows.append({"mapping_key": "accepted", "status": "accepted", "score": 0})
    kept = bounded_mappings(rows, transient_limit=2)
    assert {row["mapping_key"] for row in kept} == {"accepted", "3", "4"}


def test_hot_rotation_counts_entities_instead_of_candidate_surfaces():
    rows = [
        {"entity_id": "flower", "status": "candidate", "score": 12},
        {"entity_id": "flower", "status": "candidate", "score": 10},
        {"entity_id": "dog", "status": "candidate", "score": 8},
    ]
    assert hot_entity_ids(rows, {"flower", "dog"}, 2) == ["flower", "dog"]


def test_existing_official_surface_rows_are_removed_from_community_state():
    members = {"flower": _registry()["members"][0]}
    rows = [
        {"mapping_key": "official", "entity_id": "flower", "surface": "花礼"},
        {"mapping_key": "community", "entity_id": "flower", "surface": "鼠鼠"},
    ]
    kept = community_mappings_by_key(rows, members)
    assert set(kept) == {"community"}


def test_prompt_context_keeps_official_and_community_authority_separate(tmp_path, monkeypatch):
    from scripts import gemini_slice_jingting as jingting

    registry = _registry()
    registry_path = tmp_path / "streamer_registry.json"
    registry_path.write_text(json.dumps(registry, ensure_ascii=False), encoding="utf-8")
    community_path = tmp_path / "community_names.json"
    community_path.write_text(
        json.dumps(
            {
                "schema_version": "vtuber-slice.community-names.v1",
                "generated_at": "2026-08-06T02:00:00+00:00",
                "expires_at": "2026-09-05T02:00:00+00:00",
                "status": "fresh",
                "registry_sha256": "fixture",
                "occurrence_policy": OCCURRENCE_POLICY,
                "relations": [
                    {
                        "entity_id": "bilibili:11223344",
                        "canonical": "李豆沙",
                        "surface": "白色奶龙",
                        "relation_kind": "meme_of",
                        "first_seen_at": "2026-08-01T00:00:00+00:00",
                        "last_seen_at": "2026-08-06T00:00:00+00:00",
                        "accepted_at": "2026-08-06T00:00:00+00:00",
                        "evidence_summary": {
                            "score": 9,
                            "video_count": 4,
                            "distinct_uploader_mids": 3,
                            "distinct_days": 3,
                        },
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(jingting, "STREAMER_REGISTRY_PATHS", (str(registry_path),))
    monkeypatch.setattr(jingting, "COMMUNITY_NAMES_PATHS", (str(community_path),))

    official = jingting.streamer_registry_context(as_of=NOW)
    community = jingting.community_names_context(as_of=NOW)

    assert "VirtuaReal" in official
    assert "花礼Harei" in official
    assert "绝不证明本句提到它" in official
    assert '"relation_kind":"meme_of"' in community
    assert "白色奶龙" in community
    assert "不是官方 roster" in community
    assert "机械改字权限" in community
