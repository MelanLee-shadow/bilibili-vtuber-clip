import datetime as dt
import json
from pathlib import Path
import urllib.parse

from src.autoslice.community_name_crawler import (
    canonical_json,
    crawl,
    empty_state,
    load_config,
    validate_state,
)
from src.autoslice.timely_term_crawler import CachedResponse, CrawlError


NOW = dt.datetime(2026, 8, 6, 2, tzinfo=dt.timezone.utc)


class FakeClient:
    def __init__(self, responses, *, max_requests=176):
        self.responses = list(responses)
        self.requests_made = 0
        self.max_requests = max_requests
        self.cache_hits = 0
        self.stale_hits = 0
        self.urls = []

    def fetch(self, url, **kwargs):
        del kwargs
        self.urls.append(url)
        self.requests_made += 1
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        if isinstance(response, CachedResponse):
            return response
        return CachedResponse(json.dumps(response).encode(), "application/json", NOW)


def _member(index, canonical, mid):
    return {
        "entity_id": f"bilibili:{mid}",
        "canonical": canonical,
        "official_surfaces": [canonical, canonical[:2]],
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


def _registry(members=None):
    members = list(members or [_member(1, "花礼Harei", 1048135385)])
    next_index = 100
    while len(members) < 20:
        members.append(_member(next_index, f"成员{next_index}Name", 900000 + next_index))
        next_index += 1
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


def _config(*, comments=0, candidates=0, history=0):
    config = load_config(Path("assets/lidousha/community_name_sources.v1.json"))
    config = json.loads(json.dumps(config))
    config["comment_discovery_video_limit"] = comments
    config["candidate_query_limit"] = candidates
    config["historical_member_limit"] = history
    config["request_interval_seconds"] = 0
    return config


def _search_payload(rows):
    return {"code": 0, "data": {"result": rows}}


def _video(index, *, canonical="花礼Harei", uploader=11, day="2026-08-01", surface=None):
    published = int(dt.datetime.fromisoformat(day + "T12:00:00+00:00").timestamp())
    suffix = f"，大家叫她{surface}" if surface else "，今天也很可爱"
    return {
        "bvid": f"BV1{index:09d}",
        "aid": index,
        "mid": uploader,
        "pubdate": published,
        "title": f"【{canonical}】切片{suffix}",
        "description": f"{canonical} 单人切片",
        "tag": f"{canonical},虚拟主播",
    }


def _reply_payload(specs):
    replies = []
    for index, (commenter_mid, day, message) in enumerate(specs, start=1):
        ctime = int(dt.datetime.fromisoformat(day + "T12:00:00+00:00").timestamp())
        replies.append(
            {
                "rpid": commenter_mid * 100 + index,
                "ctime": ctime,
                "member": {"mid": str(commenter_mid), "uname": "must-not-persist"},
                "content": {"message": message},
            }
        )
    return {"code": 0, "data": {"replies": replies}}


def _judge(surface, bvids, kind="alias_of"):
    return json.dumps(
        {
            "relations": [
                {
                    "entity_id": "bilibili:1048135385",
                    "surface": surface,
                    "relation_kind": kind,
                    "evidence_bvids": bvids,
                }
            ]
        },
        ensure_ascii=False,
    )


def test_comment_only_name_is_discovered_and_accepted_by_independent_commenters():
    videos = [_video(1, day="2026-08-01"), _video(2, uploader=22, day="2026-08-02")]
    client = FakeClient(
        [
            _search_payload(videos),
            _reply_payload([(2001, "2026-08-01", "鼠鼠好可爱"), (2002, "2026-08-01", "鼠鼠来了")]),
            _reply_payload([(2003, "2026-08-02", "今天也是鼠鼠"), (2004, "2026-08-02", "喜欢鼠鼠")]),
        ]
    )
    result = crawl(
        client=client,
        registry=_registry(),
        config=_config(comments=2),
        state=empty_state(),
        now=NOW,
        llm_call=lambda _: _judge("鼠鼠", ["BV1000000001", "BV1000000002"]),
        forced_entities=["花礼Harei"],
    )

    mapping = result["state"]["mappings"][0]
    assert mapping["status"] == "accepted"
    assert mapping["reason_codes"] == ["INDEPENDENT_COMMENTER_QUORUM_MET"]
    assert mapping["metadata_video_count"] == 0
    assert mapping["distinct_commenters"] == 4
    assert mapping["comment_video_count"] == 2
    assert result["snapshot"]["relations"][0]["surface"] == "鼠鼠"

    persisted = canonical_json(result["state"])
    assert "鼠鼠好可爱" not in persisted
    assert "must-not-persist" not in persisted
    assert '"commenter_mid"' not in persisted
    assert '"rpid"' not in persisted
    assert "2001" not in persisted


def test_official_video_comments_strengthen_target_context_without_becoming_official_self_evidence():
    official_mid = 1048135385
    videos = [_video(1, uploader=official_mid), _video(2, uploader=22)]
    client = FakeClient(
        [
            _search_payload(videos),
            _reply_payload([(2101, "2026-08-01", "鼠鼠你好")]),
            _reply_payload([(2102, "2026-08-01", "鼠鼠好耶"), (2103, "2026-08-01", "来看鼠鼠")]),
        ]
    )
    result = crawl(
        client=client,
        registry=_registry(),
        config=_config(comments=2),
        state=empty_state(),
        now=NOW,
        llm_call=lambda _: _judge("鼠鼠", ["BV1000000001", "BV1000000002"]),
        forced_entities=["花礼Harei"],
    )

    mapping = result["state"]["mappings"][0]
    assert mapping["status"] == "accepted"
    assert mapping["reason_codes"] == [
        "OFFICIAL_UPLOAD_COMMENT_CONTEXT_PLUS_COMMUNITY_QUORUM"
    ]
    assert "OFFICIAL_SELF" not in mapping["reason_codes"][0]


def test_same_commenter_across_videos_counts_once_and_cannot_form_quorum():
    videos = [_video(1), _video(2, uploader=22, day="2026-08-02")]
    client = FakeClient(
        [
            _search_payload(videos),
            _reply_payload([(2201, "2026-08-01", "鼠鼠")]),
            _reply_payload([(2201, "2026-08-02", "鼠鼠又来了")]),
        ]
    )
    result = crawl(
        client=client,
        registry=_registry(),
        config=_config(comments=2),
        state=empty_state(),
        now=NOW,
        llm_call=lambda _: _judge("鼠鼠", ["BV1000000001", "BV1000000002"]),
        forced_entities=["花礼Harei"],
    )
    mapping = result["state"]["mappings"][0]
    assert mapping["status"] == "candidate"
    assert mapping["distinct_commenters"] == 1


def test_multi_person_comment_without_target_anchor_cannot_bind_a_comment_name():
    dog = _member(2, "犬绒Mofu", 1125641408)
    video = _video(1)
    video["title"] = "【花礼Harei×犬绒Mofu】联动切片"
    video["description"] = "花礼Harei 和 犬绒Mofu 双人联动"
    client = FakeClient(
        [_search_payload([video]), _reply_payload([(2301, "2026-08-01", "鼠鼠好可爱")])]
    )
    result = crawl(
        client=client,
        registry=_registry([_member(1, "花礼Harei", 1048135385), dog]),
        config=_config(comments=1),
        state=empty_state(),
        now=NOW,
        llm_call=lambda _: _judge("鼠鼠", ["BV1000000001"]),
        forced_entities=["花礼Harei"],
    )
    assert result["state"]["mappings"] == []
    assert result["state"]["last_run"]["comment_stats"] == {}


def test_comment_api_failure_does_not_discard_metadata_discovery():
    video = _video(1, surface="鼠鼠")
    client = FakeClient([_search_payload([video]), CrawlError("comment endpoint unavailable")])
    result = crawl(
        client=client,
        registry=_registry(),
        config=_config(comments=1),
        state=empty_state(),
        now=NOW,
        llm_call=lambda _: _judge("鼠鼠", ["BV1000000001"]),
        forced_entities=["花礼Harei"],
    )
    mapping = result["state"]["mappings"][0]
    assert mapping["video_count"] == 1
    assert mapping["metadata_video_count"] == 1
    assert "comment:BV1000000001" in result["state"]["last_run"]["errors"]


def test_existing_candidate_accumulates_targeted_search_evidence_without_llm_reproposal():
    first = crawl(
        client=FakeClient([_search_payload([_video(1, surface="鼠鼠")])]),
        registry=_registry(),
        config=_config(),
        state=empty_state(),
        now=NOW,
        llm_call=lambda _: _judge("鼠鼠", ["BV1000000001"]),
        forced_entities=["花礼Harei"],
    )
    assert first["state"]["mappings"][0]["status"] == "candidate"
    expanded = [
        _video(10, uploader=11, day="2026-08-01", surface="鼠鼠"),
        _video(11, uploader=22, day="2026-08-02", surface="鼠鼠"),
        _video(12, uploader=33, day="2026-08-03", surface="鼠鼠"),
        _video(13, uploader=44, day="2026-08-04", surface="鼠鼠"),
    ]
    client = FakeClient([_search_payload([]), _search_payload(expanded)])
    second = crawl(
        client=client,
        registry=_registry(),
        config=_config(candidates=1),
        state=first["state"],
        now=NOW + dt.timedelta(days=1),
        llm_call=None,
        forced_entities=["花礼Harei"],
    )
    mapping = second["state"]["mappings"][0]
    assert mapping["status"] == "accepted"
    assert mapping["video_count"] == 5
    assert mapping["distinct_uploader_mids"] == 4
    candidate_query = urllib.parse.parse_qs(urllib.parse.urlsplit(client.urls[1]).query)
    assert candidate_query["keyword"] == ["花礼 鼠鼠"]


def test_every_registry_member_gets_a_fresh_daily_query():
    members = [
        _member(1, "花礼Harei", 1048135385),
        _member(2, "犬绒Mofu", 1125641408),
        _member(3, "李豆沙", 11223344),
    ]
    registry = _registry(members)
    client = FakeClient([_search_payload([]) for _ in registry["members"]])
    result = crawl(
        client=client,
        registry=registry,
        config=_config(),
        state=empty_state(),
        now=NOW,
        llm_call=None,
    )
    assert client.requests_made == 20
    assert len(result["state"]["last_run"]["selected_entities"]) == 20
    assert result["state"]["last_run"]["search_stats"]["fresh_observed"] == 20


def test_http_412_opens_search_circuit_instead_of_immediate_retry():
    members = [
        _member(1, "花礼Harei", 1048135385),
        _member(2, "犬绒Mofu", 1125641408),
        _member(3, "李豆沙", 11223344),
    ]
    client = FakeClient([{"code": -412, "message": "request blocked"}])
    result = crawl(
        client=client,
        registry=_registry(members),
        config=_config(),
        state=empty_state(),
        now=NOW,
        llm_call=None,
    )
    assert client.requests_made == 1
    assert result["full_failure"] is True
    assert result["state"]["last_run"]["search_stats"]["fresh_deferred"] == 19
    assert "412" in result["state"]["last_run"]["search_circuit"]


def test_legacy_comment_boolean_migrates_but_never_counts_as_commenter_quorum():
    legacy = empty_state()
    for key in ("comment_hash_salt", "candidate_cursor", "comment_cursor"):
        legacy.pop(key)
    legacy["schema_version"] = "vtuber-slice.community-name-state.v1"
    legacy["rule_version"] = "community-name-quorum.v1"
    legacy["mappings"] = [
        {
            "mapping_key": "a" * 64,
            "entity_id": "bilibili:1048135385",
            "canonical": "花礼Harei",
            "surface": "鼠鼠",
            "normalized_key": "鼠鼠",
            "relation_kind": "alias_of",
            "status": "candidate",
            "reason_codes": ["INSUFFICIENT_INDEPENDENT_EVIDENCE"],
            "first_seen_at": NOW.isoformat(),
            "last_seen_at": NOW.isoformat(),
            "accepted_at": None,
            "evidence": [
                {
                    "bvid": "BV1000000001",
                    "aid": 1,
                    "url": "https://www.bilibili.com/video/BV1000000001/",
                    "uploader_mid": 11,
                    "published_at": NOW.isoformat(),
                    "field_kinds": ["title"],
                    "strength": "strong",
                    "base_score": 3,
                    "comment_match": True,
                    "raw_sha256": "b" * 64,
                }
            ],
        }
    ]
    migrated = validate_state(legacy)
    evidence = migrated["mappings"][0]["evidence"][0]
    assert migrated["schema_version"] == "vtuber-slice.community-name-state.v2"
    assert evidence["legacy_comment_match"] is True
    assert evidence["comment_witnesses"] == []
    assert "comment_match" not in evidence
