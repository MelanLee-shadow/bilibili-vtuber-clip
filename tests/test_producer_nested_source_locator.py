"""Focused tests for candidate-only watched-player source discovery."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from src.autoslice import producer_package_finalization as finalization
from src.autoslice.producer_nested_source_locator import (
    _candidate_matches,
    _canonical_sha,
    _capture_transport_response,
    _normal_candidate,
    _search_query_from_title,
    build_runtime_nested_source_locator,
    locate_nested_source,
)


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def test_search_query_uses_first_specific_clause_without_author_prefix():
    title = "【Host】Outer watched page, trailing context"

    assert _search_query_from_title(title) == "Outer watched page"
    assert _search_query_from_title("【Host】短词，A sufficiently specific phrase") == (
        "A sufficiently specific phrase"
    )
    assert _search_query_from_title("【Host】短词") is None


def test_complete_twenty_row_response_is_saved_with_reproducible_digest():
    rows = [{"bvid": f"fixture-{n}", "title": f"Result {n}"} for n in range(20)]
    payload, digest = _capture_transport_response({
        "rows": rows, "_source_locator_raw_response": rows,
        "_source_locator_response_sha256": _canonical_sha(rows),
    })
    assert payload == rows
    assert digest == _canonical_sha(payload)


@pytest.mark.parametrize("kind", ["search", "metadata"])
def test_transport_digest_drift_cannot_bind_candidate(tmp_path, kind):
    spec, source, subtitle, frame = _bound_inputs(tmp_path)
    rows = [{"bvid": "BV1D38C6UEyN", "title": "Outer watched page"}]
    metadata = {"code": 0, "data": {"bvid": "BV1D38C6UEyN", "cid": 456,
                 "title": "Outer watched page", "duration": 125}}
    def wrap(payload, stage):
        return {"rows": payload, "_source_locator_raw_response": payload,
                "_source_locator_response_sha256": "0" * 64 if stage == kind else _canonical_sha(payload)}
    result = locate_nested_source(
        spec=spec, candidate_id="watched-fixture", source_media=source,
        source_start_ms=0, source_end_ms=4000, subtitle_path=subtitle,
        evidence_root=tmp_path,
        visual_probe=lambda *_: {"status": "OBSERVED", "frame_ms": 2000,
            "frame_sha256": _sha(frame.read_bytes()), "answer": json.dumps({
                "player_visible": True, "title_candidates": ["Outer watched page"],
                "url_candidates": [], "duration_candidates": []})},
        search_candidates=lambda *_: wrap(rows, "search"),
        fetch_metadata=lambda *_: {**metadata, **wrap(metadata, "metadata")},
    )
    assert result["status"] == "UNKNOWN"
    assert "candidate" not in result
    assert result["mutation_authorized"] is False


def _bound_inputs(tmp_path: Path) -> tuple[dict, Path, Path, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    source = tmp_path / "outer.mp4"
    source.write_bytes(b"outer-media-fixture")
    subtitle = tmp_path / "final.srt"
    subtitle.write_text("1\n00:00:01,000 --> 00:00:03,000\nvisible text\n", encoding="utf-8")
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"bound-frame-jpeg")
    evidence = tmp_path / "caption-evidence.json"
    document = {
        "schema_version": "nested-media-caption-dedup.v1",
        "candidate_id": "watched-fixture",
        "source_role": "WATCHED_MEDIA",
        "source_media_sha256": _sha(source.read_bytes()),
        "source_interval_ms": [0, 4000],
        "input_transcript_sha256": _sha(subtitle.read_bytes()),
        "observations": [
            {
                "observation_id": "cue-1-caption-0",
                "frame_id": "cue-1",
                "frame_ms": 2000,
                "frame_path": str(frame),
                "frame_sha256": _sha(frame.read_bytes()),
                "role": "WATCHED_MEDIA_CAPTION",
            }
        ],
    }
    evidence.write_text(json.dumps(document), encoding="utf-8")
    spec = {
        "watched_media": True,
        "nested_media_caption_dedup": {
            "evidence_path": str(evidence),
            "evidence_sha256": _sha(evidence.read_bytes()),
        },
    }
    return spec, source, subtitle, frame


def _caption_bound_inputs(tmp_path: Path, *, text: str = "被亲一下就害羞得不行的人"):
    spec, source, subtitle, frame = _bound_inputs(tmp_path)
    evidence = Path(spec["nested_media_caption_dedup"]["evidence_path"])
    document = json.loads(evidence.read_text(encoding="utf-8"))
    document["observations"][0].update(
        {
            "caption_present": True,
            "visible_text": text,
            "pipeline_burned_subtitle": False,
        }
    )
    evidence.write_text(json.dumps(document), encoding="utf-8")
    spec["nested_media_caption_dedup"]["evidence_sha256"] = _sha(evidence.read_bytes())
    return spec, source, subtitle, frame


@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("drift", [None, "source_media_sha256", "frame_sha256"])
def test_normal_scene_digest_prefixes_preserve_byte_binding(tmp_path, explicit, drift):
    from src.autoslice.producer_nested_source_locator import (
        NestedSourceLocatorError,
        _load_bound_frame,
    )

    spec, source, subtitle, frame = _bound_inputs(tmp_path)
    evidence = Path(spec["nested_media_caption_dedup"]["evidence_path"])
    document = json.loads(evidence.read_text())
    for key in ("source_media_sha256", "input_transcript_sha256"):
        document[key] = "sha256:" + document[key]
    document["observations"][0]["frame_sha256"] = "sha256:" + _sha(frame.read_bytes())
    if explicit:
        spec["nested_media_source_frame"] = {
            "candidate_id": "watched-fixture",
            "source_media_sha256": "sha256:" + _sha(source.read_bytes()),
            "final_srt_sha256": "sha256:" + _sha(subtitle.read_bytes()),
            "source_interval_ms": [0, 4000],
            "source_role": "WATCHED_MEDIA",
            "frame_path": str(frame), "frame_ms": 2000,
            "frame_sha256": "sha256:" + _sha(frame.read_bytes()),
        }
    if drift:
        if explicit:
            spec["nested_media_source_frame"][drift] = "sha256:" + "0" * 64
        elif drift == "source_media_sha256":
            document[drift] = "sha256:" + "0" * 64
        else:
            document["observations"][0][drift] = "sha256:" + "0" * 64
    evidence.write_text(json.dumps(document), encoding="utf-8")
    spec["nested_media_caption_dedup"]["evidence_sha256"] = "sha256:" + _sha(evidence.read_bytes())
    kwargs = dict(spec=spec, candidate_id="watched-fixture", source_media=source,
                  source_start_ms=0, source_end_ms=4000, subtitle_path=subtitle,
                  evidence_root=tmp_path)
    if drift:
        with pytest.raises(NestedSourceLocatorError):
            _load_bound_frame(**kwargs)
    else:
        bound = _load_bound_frame(**kwargs)
        assert bound["frame"]["frame_sha256"] == _sha(frame.read_bytes())
        assert bound["source_media_sha256"] == _sha(source.read_bytes())


def test_caption_candidate_discovery_uses_bound_caption_without_vision_and_replays(tmp_path):
    spec, source, subtitle, frame = _caption_bound_inputs(tmp_path)
    search_calls = []
    metadata_calls = []
    rows = [
        {"bvid": "BV1D38C6UEyN", "title": "Cached watched source", "duration": 125},
        {"bvid": "BV1KwHi6PEKE", "title": "Second watched source", "duration": 126},
        {"bvid": "BV1RGbr6jEhv", "title": "Third watched source", "duration": 127},
        {"bvid": "BV1u5411n7HV", "title": "Fourth watched source", "duration": 128},
    ]

    def search(query):
        search_calls.append(query)
        return {"rows": rows, "_source_locator_raw_response": rows,
                "_source_locator_response_sha256": _canonical_sha(rows)}

    def metadata(bvid):
        metadata_calls.append(bvid)
        return {"code": 0, "data": {"bvid": bvid, "cid": 456, "title": next(
            row["title"] for row in rows if row["bvid"] == bvid
        ), "duration": 125}}

    def forbidden_visual(*_args):
        raise AssertionError("caption fallback must not dispatch vision")

    kwargs = {
        "spec": spec,
        "candidate_id": "watched-fixture",
        "source_media": source,
        "source_start_ms": 0,
        "source_end_ms": 4000,
        "subtitle_path": subtitle,
        "evidence_root": tmp_path,
        "visual_probe": forbidden_visual,
        "search_candidates": search,
        "fetch_metadata": metadata,
        "search_identity": {"transport": "bounded-test"},
        "metadata_identity": {"transport": "metadata-test"},
        "caption_candidate_discovery": True,
    }
    audit = {}
    result = locate_nested_source(**kwargs, chat_authority_audit=audit)
    assert result["status"] == "CANDIDATE_DISCOVERY_ONLY"
    assert result["reason_code"] == "CANDIDATE_DISCOVERY_ONLY"
    assert result["mutation_authorized"] is False
    assert result["source_role_authority"] is False
    discovery = result["caption_candidate_discovery"]
    assert discovery["caption_observation_id"] == "cue-1-caption-0"
    assert discovery["search_query"] == "被亲一下就害羞得不行的人"
    assert len(discovery["metadata_candidates"]) == 3
    assert search_calls == ["被亲一下就害羞得不行的人"]
    assert metadata_calls == [row["bvid"] for row in rows[:3]]
    assert audit["nested_media_source_locator"] == result

    replay = locate_nested_source(
        **{
            **kwargs,
            "search_candidates": lambda *_: (_ for _ in ()).throw(
                AssertionError("search replay")
            ),
            "fetch_metadata": lambda *_: (_ for _ in ()).throw(
                AssertionError("metadata replay")
            ),
        }
    )
    assert replay["cache_replayed"] is True
    assert replay["request_sha256"] == result["request_sha256"]


def test_caption_discovery_keeps_scene_and_caption_observation_ids_distinct(tmp_path):
    spec, source, subtitle, frame = _caption_bound_inputs(tmp_path)
    evidence = Path(spec["nested_media_caption_dedup"]["evidence_path"])
    document = json.loads(evidence.read_text(encoding="utf-8"))
    document["observations"][0]["frame_sha256"] = "sha256:" + _sha(frame.read_bytes())
    evidence.write_text(json.dumps(document), encoding="utf-8")
    spec["nested_media_caption_dedup"]["evidence_sha256"] = "sha256:" + _sha(
        evidence.read_bytes()
    )
    spec["nested_media_source_frame"] = {
        "candidate_id": "watched-fixture",
        "source_media_sha256": "sha256:" + _sha(source.read_bytes()),
        "final_srt_sha256": "sha256:" + _sha(subtitle.read_bytes()),
        "source_interval_ms": [0, 4000],
        "source_role": "WATCHED_MEDIA",
        "frame_path": str(frame),
        "frame_ms": 2000,
        "frame_sha256": "sha256:" + _sha(frame.read_bytes()),
        "observation_id": "scene-1-cue-1",
    }

    result = locate_nested_source(
        spec=spec,
        candidate_id="watched-fixture",
        source_media=source,
        source_start_ms=0,
        source_end_ms=4000,
        subtitle_path=subtitle,
        evidence_root=tmp_path,
        caption_candidate_discovery=True,
    )

    assert result["status"] == "CANDIDATE_DISCOVERY_ONLY"
    assert result["reason_code"] == "CAPTION_CANDIDATE_SEARCH_UNAVAILABLE"
    assert result["frame"]["observation_id"] == "scene-1-cue-1"
    assert result["caption_observation"]["observation_id"] == "cue-1-caption-0"
    assert result["caption_observation"]["frame_sha256"] == _sha(frame.read_bytes())


def test_mixed_caption_search_keeps_visible_alias_and_full_evidence(tmp_path):
    spec, source, subtitle, _frame = _caption_bound_inputs(tmp_path)
    evidence = Path(spec["nested_media_caption_dedup"]["evidence_path"])
    document = json.loads(evidence.read_text(encoding="utf-8"))
    caption = "老是有人说 lmsm以前"
    document["observations"][0]["visible_text"] = caption
    evidence.write_text(json.dumps(document), encoding="utf-8")
    spec["nested_media_caption_dedup"]["evidence_sha256"] = _sha(evidence.read_bytes())
    queries = []
    result = locate_nested_source(
        spec=spec, candidate_id="watched-fixture", source_media=source,
        source_start_ms=0, source_end_ms=4000, subtitle_path=subtitle,
        evidence_root=tmp_path, caption_candidate_discovery=True,
        search_candidates=lambda query: queries.append(query) or [],
    )
    assert queries == ["lmsm"]
    assert result["caption_observation"]["visible_text"] == caption
    assert result["status"] == "CANDIDATE_DISCOVERY_ONLY"
    assert result["source_role_authority"] is False
    assert result["mutation_authorized"] is False


def test_normal_empty_chrome_attaches_caption_candidate_without_second_vision(tmp_path):
    spec, source, subtitle, frame = _caption_bound_inputs(tmp_path)
    visual_calls = []
    search_calls = []
    metadata_calls = []
    rows = [{"bvid": "BV1D38C6UEyN", "title": "Watched caption source", "duration": 125}]

    def visual(media, frame_ms, _question):
        visual_calls.append((Path(media), frame_ms))
        return {
            "status": "OBSERVED",
            "frame_ms": frame_ms,
            "frame_sha256": _sha(frame.read_bytes()),
            "answer": json.dumps({
                "player_visible": True,
                "title_candidates": [],
                "url_candidates": [],
                "duration_candidates": [],
            }),
        }

    def search(query):
        search_calls.append(query)
        return {
            "rows": rows,
            "_source_locator_raw_response": rows,
            "_source_locator_response_sha256": _canonical_sha(rows),
        }

    def metadata(bvid):
        metadata_calls.append(bvid)
        payload = {
            "code": 0,
            "data": {"bvid": bvid, "cid": 456, "title": rows[0]["title"], "duration": 125},
        }
        return {
            **payload,
            "_source_locator_raw_response": payload,
            "_source_locator_response_sha256": _canonical_sha(payload),
        }

    result = locate_nested_source(
        spec=spec,
        candidate_id="watched-fixture",
        source_media=source,
        source_start_ms=0,
        source_end_ms=4000,
        subtitle_path=subtitle,
        evidence_root=tmp_path,
        visual_probe=visual,
        search_candidates=search,
        fetch_metadata=metadata,
        search_identity={"status": "BOUND"},
        metadata_identity={"status": "BOUND"},
    )

    assert result["status"] == "UNKNOWN"
    assert result["reason_code"] == "OUTER_SOURCE_HINT_EMPTY"
    assert result["primary_locator_result"] == {
        key: value for key, value in result.items() if key not in {
            "primary_locator_result", "caption_candidate_discovery",
        }
    }
    discovery = result["caption_candidate_discovery"]
    assert discovery["status"] == "CANDIDATE_DISCOVERY_ONLY"
    assert discovery["action"] == "KEEP"
    assert discovery["source_role_authority"] is False
    assert discovery["caption_candidate_discovery"]["caption_observation_id"] == "cue-1-caption-0"
    assert visual_calls == [(source, 2000)]
    assert search_calls == ["被亲一下就害羞得不行的人"]
    assert metadata_calls == [rows[0]["bvid"]]


@pytest.mark.parametrize(
    "change",
    [
        {"role": "GAME_UI"},
        {"pipeline_burned_subtitle": True},
        {"visible_text": "alias"},
    ],
)
def test_caption_candidate_discovery_rejects_non_caption_or_weak_text(tmp_path, change):
    spec, source, subtitle, _frame = _caption_bound_inputs(tmp_path)
    evidence = Path(spec["nested_media_caption_dedup"]["evidence_path"])
    document = json.loads(evidence.read_text(encoding="utf-8"))
    document["observations"][0].update(change)
    evidence.write_text(json.dumps(document), encoding="utf-8")
    spec["nested_media_caption_dedup"]["evidence_sha256"] = _sha(evidence.read_bytes())
    calls = []
    result = locate_nested_source(
        spec=spec,
        candidate_id="watched-fixture",
        source_media=source,
        source_start_ms=0,
        source_end_ms=4000,
        subtitle_path=subtitle,
        evidence_root=tmp_path,
        search_candidates=lambda query: calls.append(query),
        caption_candidate_discovery=True,
    )
    assert result["status"] == "CANDIDATE_DISCOVERY_ONLY"
    assert result["source_role_authority"] is False
    assert calls == []


def test_search_duration_string_and_numeric_duration_normalize_without_error():
    search_row = _normal_candidate(
        {
            "bvid": "BV1D38C6UEyN",
            "title": "Outer watched page",
            "arcurl": "https://www.bilibili.com/video/BV1D38C6UEyN",
            "duration": "4:27",
        }
    )
    numeric_row = _normal_candidate(
        {
            "bvid": "BV1KwHi6PEKE",
            "title": "Numeric duration fixture",
            "arcurl": "https://www.bilibili.com/video/BV1KwHi6PEKE",
            "duration": 125,
        }
    )

    assert search_row is not None
    assert search_row["duration_seconds"] == 267
    assert numeric_row is not None
    assert numeric_row["duration_seconds"] == 125


def test_candidate_matching_rejects_short_shared_generic_title():
    matches = _candidate_matches(
        [{"bvid": "BV1RGbr6jEhv", "title": "打嗝"}],
        ["【莉蔻】看露蒂丝和瓜妹打嗝，笑完彻底进不去sp了，下周还要和瓜妹联动呢！"],
    )

    assert matches == []


def test_candidate_matching_allows_small_ocr_drift_with_title_coverage():
    clue = "【莉蔻】看露蒂丝和瓜妹打嗝，笑完彻底进不去sp了，下周还要和瓜妹联动呢！"
    matches = _candidate_matches(
        [
            {
                "bvid": "BV1D38C6UEyN",
                "title": "【莉蔻】看露蒂丝和瓜咪打嗝，笑完彻底进不去sp了，下周还要和瓜妹联动呢！",
            }
        ],
        [clue],
    )

    assert [item["bvid"] for item in matches] == ["BV1D38C6UEyN"]


def test_visible_title_without_matching_search_result_keeps_without_metadata(tmp_path):
    spec, source, subtitle, frame = _bound_inputs(tmp_path)
    clue = "【莉蔻】看露蒂丝和瓜妹打嗝，笑完彻底进不去sp了，下周还要和瓜妹联动呢！"
    result = locate_nested_source(
        spec=spec,
        candidate_id="watched-fixture",
        source_media=source,
        source_start_ms=0,
        source_end_ms=4000,
        subtitle_path=subtitle,
        evidence_root=tmp_path,
        visual_probe=lambda *_: {
            "status": "OBSERVED",
            "frame_ms": 2000,
            "frame_sha256": _sha(frame.read_bytes()),
            "answer": json.dumps({
                "player_visible": True,
                "title_candidates": [clue],
                "url_candidates": [],
                "duration_candidates": [],
            }),
        },
        search_candidates=lambda _: [{"bvid": "BV1RGbr6jEhv", "title": "打嗝"}],
        fetch_metadata=lambda _: (_ for _ in ()).throw(AssertionError("wrong-source metadata")),
    )

    assert result["visual_observation"]["title_candidates"] == [clue]
    assert result["reason_code"] == "SOURCE_SEARCH_NO_TITLE_MATCH"
    assert result["status"] == "UNKNOWN"
    assert result["action"] == "KEEP"
    assert result["search_candidates"] == []


def test_search_uses_first_specific_clause_but_matches_full_title(tmp_path):
    spec, source, subtitle, frame = _bound_inputs(tmp_path)
    clue = "【Host】Outer watched page, trailing context"
    calls = {"search": 0, "metadata": 0}

    def search(query):
        calls["search"] += 1
        assert query == "Outer watched page"
        rows = [{"bvid": "BV1D38C6UEyN", "title": clue}]
        return {
            "rows": rows,
            "_source_locator_response_sha256": _canonical_sha(rows),
            "_source_locator_raw_response": rows,
        }

    def metadata(bvid):
        calls["metadata"] += 1
        assert bvid == "BV1D38C6UEyN"
        payload = {
            "code": 0,
            "data": {
                "bvid": bvid,
                "cid": 456,
                "title": "【Host】 Outer watched page ，trailing context！",
                "duration": 125,
            },
        }
        return {
            **payload,
            "_source_locator_response_sha256": _canonical_sha(payload),
            "_source_locator_raw_response": payload,
        }

    result = locate_nested_source(
        spec=spec,
        candidate_id="watched-fixture",
        source_media=source,
        source_start_ms=0,
        source_end_ms=4000,
        subtitle_path=subtitle,
        evidence_root=tmp_path,
        visual_probe=lambda *_: {
            "status": "OBSERVED",
            "frame_ms": 2000,
            "frame_sha256": _sha(frame.read_bytes()),
            "answer": json.dumps(
                {
                    "player_visible": True,
                    "title_candidates": [clue],
                    "url_candidates": [],
                    "duration_candidates": [],
                }
            ),
        },
        search_candidates=search,
        fetch_metadata=metadata,
    )

    assert result["status"] == "EVIDENCE_ONLY"
    assert result["search_query"] == "Outer watched page"
    assert result["candidate"]["title"] == "【Host】 Outer watched page ，trailing context！"
    assert calls == {"search": 1, "metadata": 1}


def test_unique_public_candidate_is_evidence_only_and_replays(tmp_path):
    spec, source, subtitle, frame = _bound_inputs(tmp_path)
    calls = {"visual": 0, "search": 0, "metadata": 0}

    def visual(media, ms, question):
        calls["visual"] += 1
        assert media == source and ms == 2000
        assert "subtitles" in question and "prior answers" in question
        return {
            "status": "OBSERVED",
            "media_path": str(media),
            "frame_ms": ms,
            "frame_sha256": _sha(frame.read_bytes()),
            "prompt_sha256": "prompt-hash",
            "response_sha256": "response-hash",
            "answer": json.dumps(
                {
                    "player_visible": True,
                    "title_candidates": ["Outer watched page"],
                    "url_candidates": [],
                    "duration_candidates": ["2:05"],
                }
            ),
        }

    def search(query):
        calls["search"] += 1
        assert query == "Outer watched page"
        rows = [{"bvid": "BV1D38C6UEyN", "title": "Outer watched page"}]
        return {
            "rows": rows,
            "_source_locator_response_sha256": _canonical_sha(rows),
            "_source_locator_raw_response": rows,
        }

    def metadata(bvid):
        calls["metadata"] += 1
        assert bvid == "BV1D38C6UEyN"
        payload = {
            "code": 0,
            "data": {
                "bvid": bvid,
                "aid": 123,
                "cid": 456,
                "title": "Outer watched page",
                "duration": 125,
            },
        }
        return {
            **payload,
            "_source_locator_response_sha256": _canonical_sha(payload),
            "_source_locator_raw_response": payload,
        }

    result = locate_nested_source(
        spec=spec,
        candidate_id="watched-fixture",
        source_media=source,
        source_start_ms=0,
        source_end_ms=4000,
        subtitle_path=subtitle,
        evidence_root=tmp_path,
        visual_probe=visual,
        search_candidates=search,
        fetch_metadata=metadata,
        visual_identity={"transport": "fixture-visual"},
        search_identity={"transport": "bounded-search-fixture"},
        metadata_identity={"transport": "public-view-fixture"},
    )
    assert result["status"] == "EVIDENCE_ONLY"
    assert result["action"] == "KEEP"
    assert result["mutation_authorized"] is False
    assert result["source_role_authority"] is False
    assert result["candidate"] == {
        "bvid": "BV1D38C6UEyN",
        "aid": 123,
        "cid": 456,
        "title": "Outer watched page",
        "duration_seconds": 125,
    }
    assert result["search_response"] == [{"bvid": "BV1D38C6UEyN", "title": "Outer watched page"}]
    assert result["metadata_response"]["data"]["cid"] == 456
    assert "answer" in result["visual_receipt"]
    assert result["visual_receipt"]["answer"].startswith("{")
    assert calls == {"visual": 1, "search": 1, "metadata": 1}
    replay = locate_nested_source(
        spec=spec,
        candidate_id="watched-fixture",
        source_media=source,
        source_start_ms=0,
        source_end_ms=4000,
        subtitle_path=subtitle,
        evidence_root=tmp_path,
        visual_probe=lambda *args: (_ for _ in ()).throw(AssertionError("visual replay")),
        search_candidates=lambda *_: (_ for _ in ()).throw(AssertionError("search replay")),
        fetch_metadata=lambda *_: (_ for _ in ()).throw(AssertionError("metadata replay")),
        visual_identity={"transport": "fixture-visual"},
        search_identity={"transport": "bounded-search-fixture"},
        metadata_identity={"transport": "public-view-fixture"},
    )
    assert replay["cache_replayed"] is True
    assert replay["candidate"] == result["candidate"]

    cache_path = next((tmp_path / "nested-source-locator/watched-fixture").glob("*.json"))
    cached = json.loads(cache_path.read_text())
    cached["result"]["search_response"] = []
    cache_path.write_text(json.dumps(cached))
    corrupt_replay = locate_nested_source(
        spec=spec, candidate_id="watched-fixture", source_media=source,
        source_start_ms=0, source_end_ms=4000, subtitle_path=subtitle,
        evidence_root=tmp_path,
        visual_probe=lambda *args: (_ for _ in ()).throw(AssertionError("visual replay")),
        search_candidates=lambda *_: (_ for _ in ()).throw(AssertionError("search replay")),
        fetch_metadata=lambda *_: (_ for _ in ()).throw(AssertionError("metadata replay")),
        visual_identity={"transport": "fixture-visual"},
        search_identity={"transport": "bounded-search-fixture"},
        metadata_identity={"transport": "public-view-fixture"},
    )
    assert corrupt_replay["status"] == "UNKNOWN"
    assert corrupt_replay["reason_code"] == "LOCATOR_CACHE_RESPONSE_HASH_DRIFT"
    assert "candidate" not in corrupt_replay


def test_explicit_current_source_frame_precedes_caption_observation(tmp_path):
    spec, source, subtitle, _caption_frame = _bound_inputs(tmp_path)
    _add_later_frame(tmp_path, spec)
    source_frame = tmp_path / "current-source-frame.jpg"
    source_frame.write_bytes(b"current-unburned-source-frame")
    spec["nested_media_source_frame"] = {
        "candidate_id": "watched-fixture",
        "source_media_sha256": _sha(source.read_bytes()),
        "final_srt_sha256": _sha(subtitle.read_bytes()),
        "source_interval_ms": [0, 4000],
        "source_role": "WATCHED_MEDIA",
        "frame_path": str(source_frame),
        "frame_ms": 3500,
        "frame_sha256": _sha(source_frame.read_bytes()),
        "frame_id": "current-source-frame",
        "observation_id": "capture-1",
    }
    seen = []

    def visual(media, ms, _question):
        seen.append((media, ms))
        return {
            "status": "OBSERVED",
            "frame_ms": ms,
            "frame_sha256": _sha(source_frame.read_bytes()),
            "answer": json.dumps(
                {
                    "player_visible": True,
                    "title_candidates": [],
                    "url_candidates": [],
                    "duration_candidates": [],
                }
            ),
        }

    result = locate_nested_source(
        spec=spec,
        candidate_id="watched-fixture",
        source_media=source,
        source_start_ms=0,
        source_end_ms=4000,
        subtitle_path=subtitle,
        evidence_root=tmp_path,
        visual_probe=visual,
    )
    assert result["status"] == "UNKNOWN"
    assert result["frame_selection"] == "EXPLICIT_CURRENT_SOURCE_FRAME"
    assert result["frame_ms"] == 3500
    assert result["frame_sha256"] == _sha(source_frame.read_bytes())
    assert seen == [(source, 3500)]


def _add_later_frame(tmp_path, spec, *, same_pixels=False):
    evidence = Path(spec["nested_media_caption_dedup"]["evidence_path"])
    document = json.loads(evidence.read_text())
    frame = tmp_path / "later-frame.jpg"
    frame.write_bytes(b"bound-frame-jpeg" if same_pixels else b"later-readable-chrome")
    document["observations"].append({
        "observation_id": "cue-2-caption-0", "frame_id": "cue-2", "frame_ms": 3500,
        "frame_path": str(frame), "frame_sha256": _sha(frame.read_bytes()),
        "role": "WATCHED_MEDIA_CAPTION",
    })
    evidence.write_text(json.dumps(document))
    spec["nested_media_caption_dedup"]["evidence_sha256"] = _sha(evidence.read_bytes())
    return frame


def test_empty_first_chrome_uses_later_bound_frame_and_replays_both_caches(tmp_path):
    spec, source, subtitle, first = _bound_inputs(tmp_path)
    later = _add_later_frame(tmp_path, spec)
    calls = []
    def visual(_media, ms, _question):
        calls.append(ms)
        return {"status": "OBSERVED", "frame_ms": ms,
                "frame_sha256": _sha((first if ms == 2000 else later).read_bytes()),
                "answer": json.dumps({"player_visible": True,
                    "title_candidates": [] if ms == 2000 else ["Outer watched page"],
                    "url_candidates": [], "duration_candidates": []})}
    kwargs = dict(spec=spec, candidate_id="watched-fixture", source_media=source,
        source_start_ms=0, source_end_ms=4000, subtitle_path=subtitle,
        evidence_root=tmp_path, visual_probe=visual,
        search_candidates=lambda _: [{"bvid": "BV1D38C6UEyN", "title": "Outer watched page"}],
        fetch_metadata=lambda _: {"code": 0, "data": {"bvid": "BV1D38C6UEyN", "cid": 456,
            "title": "Outer watched page", "duration": 125}})
    audit = {}
    result = locate_nested_source(**kwargs, chat_authority_audit=audit)
    assert calls == [2000, 3500]
    assert result["reason_code"] == "PUBLIC_SOURCE_CANDIDATE_BOUND"
    assert result["frame_ms"] == 3500
    assert result["frame_selection"] == "CURRENT_BOUND_FRAMES_AFTER_EMPTY_CHROME"
    assert result["source_match_scope"] == "OBSERVED_FRAME_ONLY"
    assert result["source_role_authority"] is False
    assert result["action"] == "KEEP"
    assert [x["reason_code"] for x in result["frame_attempts"]] == ["OUTER_SOURCE_HINT_EMPTY", "PUBLIC_SOURCE_CANDIDATE_BOUND"]
    assert audit["nested_media_source_locator"] == result
    replay = locate_nested_source(**kwargs)
    assert calls == [2000, 3500]
    assert replay["cache_replayed"] is True
    assert replay["candidate"] == result["candidate"]


@pytest.mark.parametrize("same_pixels,unavailable,expected", [(True, False, [2000]), (False, True, [2000])])
def test_later_frame_does_not_duplicate_pixels_or_retry_provider_failure(tmp_path, same_pixels, unavailable, expected):
    spec, source, subtitle, first = _bound_inputs(tmp_path)
    _add_later_frame(tmp_path, spec, same_pixels=same_pixels)
    calls = []
    def visual(_media, ms, _question):
        calls.append(ms)
        if unavailable:
            return {"status": "UNAVAILABLE"}
        return {"status": "OBSERVED", "frame_ms": ms, "frame_sha256": _sha(first.read_bytes()),
            "answer": json.dumps({"player_visible": True, "title_candidates": [],
                                  "url_candidates": [], "duration_candidates": []})}
    result = locate_nested_source(spec=spec, candidate_id="watched-fixture", source_media=source,
        source_start_ms=0, source_end_ms=4000, subtitle_path=subtitle, evidence_root=tmp_path,
        visual_probe=visual)
    assert calls == expected
    assert result["status"] == "UNKNOWN"
    assert result["action"] == "KEEP"


def test_explicit_current_source_frame_drift_fails_closed_without_fallback(tmp_path):
    spec, source, subtitle, caption_frame = _bound_inputs(tmp_path)
    spec["nested_media_source_frame"] = {
        "candidate_id": "watched-fixture",
        "source_media_sha256": _sha(source.read_bytes()),
        "final_srt_sha256": _sha(subtitle.read_bytes()),
        "source_interval_ms": [0, 4000],
        "frame_path": str(tmp_path / "missing-current-frame.jpg"),
        "frame_ms": 3500,
        "frame_sha256": "stale-frame-hash",
    }
    calls = []
    result = locate_nested_source(
        spec=spec,
        candidate_id="watched-fixture",
        source_media=source,
        source_start_ms=0,
        source_end_ms=4000,
        subtitle_path=subtitle,
        evidence_root=tmp_path,
        visual_probe=lambda *args: calls.append(args),
    )
    assert result["status"] == "UNKNOWN"
    assert result["action"] == "KEEP"
    assert result["reason_code"].startswith("SOURCE_LOCATOR_INPUT_")
    assert calls == []
    assert result.get("frame_sha256") != _sha(caption_frame.read_bytes())


def test_nonwatched_scene_and_binding_drift_never_dispatch(tmp_path):
    spec, source, subtitle, _frame = _bound_inputs(tmp_path)
    calls = []
    spec["watched_media"] = False
    result = locate_nested_source(
        spec=spec,
        candidate_id="watched-fixture",
        source_media=source,
        source_start_ms=0,
        source_end_ms=4000,
        subtitle_path=subtitle,
        evidence_root=tmp_path,
        visual_probe=lambda *args: calls.append("visual"),
        search_candidates=lambda *_: calls.append("search"),
        fetch_metadata=lambda *_: calls.append("metadata"),
    )
    assert result["status"] == "UNKNOWN"
    assert result["action"] == "KEEP"
    assert calls == []

    spec, source, subtitle, _frame = _bound_inputs(tmp_path / "drift")
    subtitle.write_text(subtitle.read_text(encoding="utf-8").replace("visible", "changed"), encoding="utf-8")
    result = locate_nested_source(
        spec=spec,
        candidate_id="watched-fixture",
        source_media=source,
        source_start_ms=0,
        source_end_ms=4000,
        subtitle_path=subtitle,
        evidence_root=tmp_path / "drift",
        visual_probe=lambda *args: calls.append("visual"),
        search_candidates=lambda *_: calls.append("search"),
        fetch_metadata=lambda *_: calls.append("metadata"),
    )
    assert result["status"] == "UNKNOWN"
    assert calls == []


def test_ambiguous_candidates_and_visual_failure_keep_without_metadata(tmp_path):
    spec, source, subtitle, frame = _bound_inputs(tmp_path)
    metadata_calls = []

    def visual(_media, _ms, _question):
        return {
            "status": "OBSERVED",
            "frame_sha256": _sha(frame.read_bytes()),
            "answer": json.dumps(
                {
                    "player_visible": True,
                    "title_candidates": ["Outer watched page"],
                    "url_candidates": [],
                    "duration_candidates": [],
                }
            ),
        }

    result = locate_nested_source(
        spec=spec,
        candidate_id="watched-fixture",
        source_media=source,
        source_start_ms=0,
        source_end_ms=4000,
        subtitle_path=subtitle,
        evidence_root=tmp_path,
        visual_probe=visual,
        search_candidates=lambda _query: [
            {"bvid": "BV1D38C6UEyN", "title": "Outer watched page"},
            {"bvid": "BV1D38C6UEyM", "title": "Outer watched page"},
        ],
        fetch_metadata=lambda bvid: metadata_calls.append(bvid),
    )
    assert result["status"] == "UNKNOWN"
    assert result["reason_code"] == "AMBIGUOUS_SOURCE_CANDIDATES"
    assert metadata_calls == []


def _adapters(tmp_path, locator=None):
    def unused(*args, **kwargs):
        raise AssertionError("not part of locator hook")

    return finalization.ProducerFinalizationAdapters(
        accurate_recut_command=unused,
        run_command=unused,
        write_source_range_srt=unused,
        apply_text_override_document=unused,
        run_speaker_finalization=unused,
        burn_preview_subtitles=unused,
        stage_publish_draft=unused,
        generate_upload_tags=unused,
        delivery_root=lambda: tmp_path,
        locate_nested_source=locator,
    )


def test_finalizer_hook_only_runs_for_bound_watched_scene(tmp_path):
    calls = []

    def locator(**kwargs):
        calls.append(kwargs)
        return {
            "schema_version": "nested-media-source-locator.v1",
            "status": "EVIDENCE_ONLY",
            "action": "KEEP",
            "mutation_authorized": True,
            "source_role_authority": True,
        }

    spec = {
        "watched_media": True,
        "nested_media_caption_dedup": {"evidence_path": "evidence.json"},
    }
    audit = {}
    effective = finalization._run_nested_source_locator(
        spec=spec,
        caption_spec=spec,
        adapters=_adapters(tmp_path, locator),
        cid="watched-fixture",
        padded=tmp_path / "outer.mp4",
        final_start=0,
        final_end=4000,
        subtitle_path=tmp_path / "final.srt",
        evidence_root=tmp_path,
        chat_authority_audit=audit,
    )
    assert len(calls) == 1
    assert effective["nested_media_source_locator"]["status"] == "EVIDENCE_ONLY"
    assert spec["nested_media_source_locator"]["mutation_authorized"] is False
    assert spec["nested_media_source_locator"]["source_role_authority"] is False
    assert "nested_media_acoustic" not in spec
    assert audit["nested_media_source_locator"]["action"] == "KEEP"

    calls.clear()
    ordinary = {"watched_media": False}
    effective = finalization._run_nested_source_locator(
        spec=ordinary,
        caption_spec=ordinary,
        adapters=_adapters(tmp_path, locator),
        cid="ordinary",
        padded=tmp_path / "outer.mp4",
        final_start=0,
        final_end=4000,
        subtitle_path=tmp_path / "final.srt",
        evidence_root=tmp_path,
        chat_authority_audit={},
    )
    assert calls == []
    assert effective == ordinary


def test_finalizer_hook_rejects_injected_authority_result(tmp_path):
    def locator(**kwargs):
        return {"status": "PASS", "action": "REPLACE", "mutation_authorized": True}

    spec = {
        "watched_media": True,
        "nested_media_caption_dedup": {"evidence_path": "evidence.json"},
    }
    audit = {}
    effective = finalization._run_nested_source_locator(
        spec=spec,
        caption_spec=spec,
        adapters=_adapters(tmp_path, locator),
        cid="watched-fixture",
        padded=tmp_path / "outer.mp4",
        final_start=0,
        final_end=4000,
        subtitle_path=tmp_path / "final.srt",
        evidence_root=tmp_path,
        chat_authority_audit=audit,
    )
    result = effective["nested_media_source_locator"]
    assert result["status"] == "UNKNOWN"
    assert result["action"] == "KEEP"
    assert result["mutation_authorized"] is False
    assert result["source_role_authority"] is False


def test_runtime_factory_wires_existing_clients_without_network_in_preflight(tmp_path):
    locator = build_runtime_nested_source_locator(
        tmp_path,
        repo_root=Path(__file__).parents[1],
        community_config_path=Path(__file__).parents[1]
        / "assets/lidousha/community_name_sources.v1.json",
        cache_root=tmp_path / "http-cache",
        offline=True,
    )
    result = locator(
        spec={"watched_media": False},
        candidate_id="offline-fixture",
        source_media=tmp_path / "missing.mp4",
        source_start_ms=0,
        source_end_ms=1000,
        subtitle_path=tmp_path / "missing.srt",
        evidence_root=tmp_path,
    )
    assert result["status"] == "UNKNOWN"
    assert result["reason_code"] == "SOURCE_LOCATOR_INPUT_NESTEDSOURCELOCATORERROR"


def test_runtime_factory_offline_disables_visual_cpa_and_bili_callbacks(tmp_path, monkeypatch):
    spec, source, subtitle, _frame = _bound_inputs(tmp_path)
    visual_calls = []
    metadata_builder_calls = []

    def forbidden_metadata_builder(*args, **kwargs):
        metadata_builder_calls.append((args, kwargs))
        raise AssertionError("offline locator must not load the login cookie")

    def forbidden_visual(*args, **kwargs):
        visual_calls.append((args, kwargs))
        raise AssertionError("offline locator must not dispatch CPA")

    monkeypatch.setattr(
        "scripts.authorized_upload._build_season_http", forbidden_metadata_builder
    )
    monkeypatch.setattr(
        "src.autoslice.cpa_frame_witness.frame_vision_probe", forbidden_visual
    )
    locator = build_runtime_nested_source_locator(
        tmp_path,
        repo_root=Path(__file__).parents[1],
        community_config_path=Path(__file__).parents[1]
        / "assets/lidousha/community_name_sources.v1.json",
        cache_root=tmp_path / "http-cache",
        offline=True,
    )
    result = locator(
        spec=spec,
        candidate_id="watched-fixture",
        source_media=source,
        source_start_ms=0,
        source_end_ms=4000,
        subtitle_path=subtitle,
        evidence_root=tmp_path,
    )
    assert result["status"] == "UNKNOWN"
    assert result["reason_code"] == "VISUAL_PROBE_UNAVAILABLE"
    assert visual_calls == []
    assert metadata_builder_calls == []


def test_runtime_factory_uses_existing_default_source_cache_root(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    source_cache = runtime / "cache" / "nested-sources" / "BV1D38C6UEyN" / "456"
    source_cache.mkdir(parents=True)
    (source_cache / "source.json").write_text("{}", encoding="utf-8")
    captured = {}

    def fake_builder(**kwargs):
        captured.update(kwargs)
        return lambda **_ignored: {"status": "UNKNOWN"}

    monkeypatch.setattr(
        "src.autoslice.producer_nested_source_locator.build_nested_source_locator",
        fake_builder,
    )
    build_runtime_nested_source_locator(
        runtime,
        repo_root=Path(__file__).parents[1],
        community_config_path=Path(__file__).parents[1]
        / "assets/lidousha/community_name_sources.v1.json",
        cache_root=tmp_path / "http-cache",
        offline=True,
    )

    assert captured["source_cache_root"] == source_cache.parents[1]
    assert captured["cached_source_probe"] is not None


def test_runtime_factory_empty_default_source_cache_keeps_v12_shape(tmp_path, monkeypatch):
    captured = {}

    def fake_builder(**kwargs):
        captured.update(kwargs)
        return lambda **_ignored: {"status": "UNKNOWN"}

    monkeypatch.setattr(
        "src.autoslice.producer_nested_source_locator.build_nested_source_locator",
        fake_builder,
    )
    build_runtime_nested_source_locator(
        tmp_path / "runtime",
        repo_root=Path(__file__).parents[1],
        community_config_path=Path(__file__).parents[1]
        / "assets/lidousha/community_name_sources.v1.json",
        cache_root=tmp_path / "http-cache",
        offline=True,
    )

    assert captured["source_cache_root"] is None
    assert captured["cached_source_probe"] is None


def test_runtime_factory_uses_normal_login_view_client_for_metadata(
    tmp_path, monkeypatch
):
    spec, source, subtitle, frame = _bound_inputs(tmp_path)
    import scripts.authorized_upload as authorized_upload
    from src.autoslice import community_comment_discovery, community_name_crawler
    from src.autoslice import llm_client

    calls = {}

    def fake_frame_probe(media, ms, question, *, model="gpt-6-sol", **kwargs):
        calls["visual"] = (media, ms, question, model, kwargs)
        return {
            "status": "OBSERVED",
            "frame_ms": ms,
            "frame_sha256": _sha(frame.read_bytes()),
            "answer": json.dumps(
                {
                    "player_visible": True,
                    "title_candidates": ["Outer watched page"],
                    "url_candidates": [],
                    "duration_candidates": [],
                }
            ),
        }

    def fake_runtime_environment(_runtime):
        return {"CPA_BASE_URL": "https://cpa.invalid", "CPA_API_KEY": "test-only"}

    def fake_wbi(_client, *, endpoint):
        calls["wbi"] = endpoint
        return "mixin-key", True

    def fake_search(
        _client,
        *,
        endpoint,
        query,
        order,
        page,
        max_results,
        mixin_key,
        signed_at,
    ):
        calls["search"] = {
            "endpoint": endpoint,
            "query": query,
            "order": order,
            "page": page,
            "max_results": max_results,
            "mixin_key": mixin_key,
        }
        return ([{"bvid": "BV1D38C6UEyN", "title": "Outer watched page"}], True)

    cookie_path = tmp_path / "app-cookie.json"

    def fake_build_season_http(path):
        calls["cookie_path"] = path

        def read_view(url):
            calls["metadata_url"] = url
            assert url == (
                "https://api.bilibili.com/x/web-interface/view?bvid=BV1D38C6UEyN"
            )
            return {
                "code": 0,
                "data": {
                    "bvid": "BV1D38C6UEyN",
                    "aid": 123,
                    "cid": 456,
                    "title": "Outer watched page",
                    "duration": 125,
                },
            }

        return read_view, "csrf-is-not-persisted"

    monkeypatch.setattr(
        "src.autoslice.cpa_frame_witness.frame_vision_probe", fake_frame_probe
    )
    monkeypatch.setattr(
        llm_client, "runtime_cpa_command_environment", fake_runtime_environment
    )
    monkeypatch.setattr(
        community_comment_discovery, "fetch_wbi_mixin_key", fake_wbi
    )
    monkeypatch.setattr(community_name_crawler, "_search_rows", fake_search)
    monkeypatch.setattr(authorized_upload, "DEFAULT_COOKIE_JSON", cookie_path)
    monkeypatch.setattr(
        authorized_upload, "_build_season_http", fake_build_season_http
    )

    locator = build_runtime_nested_source_locator(
        tmp_path,
        repo_root=Path(__file__).parents[1],
        community_config_path=Path(__file__).parents[1]
        / "assets/lidousha/community_name_sources.v1.json",
        cache_root=tmp_path / "http-cache",
    )
    result = locator(
        spec=spec,
        candidate_id="watched-fixture",
        source_media=source,
        source_start_ms=0,
        source_end_ms=4000,
        subtitle_path=subtitle,
        evidence_root=tmp_path / "locator-evidence",
    )

    assert result["status"] == "EVIDENCE_ONLY"
    assert result["candidate"]["cid"] == 456
    assert calls["cookie_path"] == cookie_path
    assert calls["metadata_url"].startswith("https://api.bilibili.com/")
    assert calls["search"]["query"] == "Outer watched page"
    assert "csrf-is-not-persisted" not in json.dumps(result, ensure_ascii=False)
