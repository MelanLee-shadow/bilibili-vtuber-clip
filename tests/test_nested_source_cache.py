from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from src.autoslice.nested_source_cache import (
    probe_cached_caption_source_candidates,
    read_cached_caption_source_candidate,
    read_cached_nested_source,
)
from src.autoslice.producer_nested_source_locator import _canonical_sha, locate_nested_source
from src.autoslice.producer_nested_acoustic import observe_prepared_nested_acoustics
from tests.test_producer_nested_acoustic import _fake_pcm_tools, _prepared


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _save(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")
    return {"path": str(path), "sha256": _sha(path), "bytes": path.stat().st_size}


def _cache(tmp_path, inputs):
    bvid, cid = "BVTEST123456", 123456
    root = tmp_path / "cache"
    folder = root / bvid / str(cid)
    folder.mkdir(parents=True)
    page = f"https://www.bilibili.com/video/{bvid}/"
    media = Path(inputs["independent_source_media_path"])
    metadata = _save(folder / "source.info.json", {
        "id": bvid, "extractor_key": "BiliBili", "webpage_url": page,
        "title": "Cached watched source", "duration": 125,
    })
    receipt = _save(folder / "DOWNLOAD-RESULT.json", {
        "status": "PASS_EXACT_BVID_DOWNLOADED", "bvid": bvid, "webpage_url": page,
        # Original paths can belong to the download host; actual content is bound.
        "media": {"path": "/old/source.mp4", "sha256": _sha(media), "bytes": media.stat().st_size},
        "info": metadata,
    })
    log = folder / "YTDLP.stdout.txt"
    log.write_text(f"[BiliBili] {bvid}: Downloading video formats for cid {cid}\n", encoding="utf-8")
    entry = {
        "bvid": bvid, "cid": cid, "webpage_url": page,
        "media": {"path": str(media), "sha256": _sha(media), "bytes": media.stat().st_size},
        "metadata": metadata, "download_receipt": receipt,
        "download_log": {"path": str(log), "sha256": _sha(log)},
    }
    path = folder / "source.json"
    _save(path, entry)
    locator = {
        "candidate_id": "candidate", "status": "EVIDENCE_ONLY", "action": "KEEP",
        "reason_code": "PUBLIC_SOURCE_CANDIDATE_BOUND", "candidate": {"bvid": bvid, "cid": cid},
        "source_media_sha256": inputs["outer_media_sha256"],
        "final_srt_sha256": inputs["final_srt_sha256"],
    }
    return root, path, entry, locator


def _read(root, locator, inputs):
    return read_cached_nested_source(
        cache_root=root, locator=locator, candidate_id="candidate",
        source_media=inputs["outer_media_path"], subtitle_path=inputs["final_srt_path"],
    )


def _caption_discovery(
    tmp_path, inputs, *, cache_candidate="BVTEST123456", cache_root=None,
    cache_candidate_index=0, uncached_title="Second watched source",
):
    tmp_path.mkdir(parents=True, exist_ok=True)
    evidence = tmp_path / "caption-evidence.json"
    frame = Path(inputs["caption_frame_path"])
    source = Path(inputs["outer_media_path"])
    subtitle = Path(inputs["final_srt_path"])
    def raw_sha(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()
    evidence.write_text(
        json.dumps(
            {
                "schema_version": "nested-media-caption-dedup.v1",
                "candidate_id": "caption-discovery",
                "source_role": "WATCHED_MEDIA",
                "source_media_sha256": raw_sha(source),
                "source_interval_ms": [0, 4000],
                "input_transcript_sha256": raw_sha(subtitle),
                "observations": [
                    {
                        "observation_id": "cue-1-caption-0",
                        "frame_id": "cue-1",
                        "frame_ms": 150,
                        "frame_path": str(frame),
                        "frame_sha256": raw_sha(frame),
                        "role": "WATCHED_MEDIA_CAPTION",
                        "caption_present": True,
                        "visible_text": "cached watched source",
                        "pipeline_burned_subtitle": False,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    rows = [
        {"bvid": cache_candidate, "title": "Cached watched source", "duration": 125},
        {"bvid": "BV1KwHi6PEKE", "title": uncached_title, "duration": 126},
        {"bvid": "BV1RGbr6jEhv", "title": "Third watched source", "duration": 127},
        {"bvid": "BV1u5411n7HV", "title": "Fourth watched source", "duration": 128},
    ]
    rows.insert(0, rows.pop(cache_candidate_index))

    def search(_query):
        return {"rows": rows, "_source_locator_raw_response": rows,
                "_source_locator_response_sha256": _canonical_sha(rows)}

    def metadata(bvid):
        return {"code": 0, "data": {"bvid": bvid, "cid": 123456,
                "title": next(row["title"] for row in rows if row["bvid"] == bvid),
                "duration": 125}}

    spec = {
        "watched_media": True,
        "nested_media_caption_dedup": {
            "evidence_path": str(evidence),
            "evidence_sha256": raw_sha(evidence),
        },
    }
    return locate_nested_source(
        spec=spec,
        candidate_id="caption-discovery",
        source_media=source,
        source_start_ms=0,
        source_end_ms=4000,
        subtitle_path=subtitle,
        evidence_root=tmp_path,
        search_candidates=search,
        fetch_metadata=metadata,
        caption_candidate_discovery=True,
        cached_source_probe=(
            lambda candidates: probe_cached_caption_source_candidates(
                cache_root=cache_root, candidates=candidates,
            )
        ) if cache_root is not None else None,
        source_cache_root=cache_root,
    )


def test_download_source_can_relocate_without_rebinding_original_receipt(tmp_path):
    inputs, _, _ = _prepared(tmp_path)
    root, _, _, locator = _cache(tmp_path, inputs)
    result = _read(root, locator, inputs)
    assert result["independent_source_media_path"] == str(inputs["independent_source_media_path"])
    assert result["independent_source_media_sha256"] == inputs["independent_source_media_sha256"]
    assert result["source_cache_binding"]["source_role_authority"] is False


def test_source_fact_keeps_locator_id_for_later_same_bytes_consumer(tmp_path):
    inputs, _, _ = _prepared(tmp_path)
    root, _, _, locator = _cache(tmp_path, inputs)
    locator["candidate_id"] = "whole-scene-source-discovery"
    result = _read(root, locator, inputs)
    binding = result["source_cache_binding"]
    assert binding["locator_candidate_id"] == "whole-scene-source-discovery"
    assert binding["consumer_candidate_id"] == "candidate"


def test_caption_discovery_uses_first_cached_candidate_without_claiming_source_role(tmp_path):
    inputs, _, _ = _prepared(tmp_path)
    root, _, _, _ = _cache(tmp_path, inputs)
    locator = _caption_discovery(tmp_path, inputs)
    locator = {
        "status": "UNKNOWN",
        "reason_code": "OUTER_SOURCE_HINT_EMPTY",
        "primary_locator_result": {"status": "UNKNOWN"},
        "caption_candidate_discovery": locator,
    }
    result = read_cached_caption_source_candidate(
        cache_root=root,
        locator=locator,
        candidate_id="caption-consumer",
        source_media=inputs["outer_media_path"],
        subtitle_path=inputs["final_srt_path"],
    )
    assert result["status"] == "CANDIDATE_CACHE_AVAILABLE"
    assert result["selection_policy"] == "FIRST_AVAILABLE_PUBLIC_SEARCH_CANDIDATE"
    assert result["uniqueness_claim"] is False
    assert result["source_role_authority"] is False
    assert result["locator_candidate_id"] == "caption-discovery"
    assert result["consumer_candidate_id"] == "caption-consumer"
    assert result["source_cache_binding"]["bvid"] == "BVTEST123456"
    assert result["source_cache_binding"]["cid"] == 123456
    assert result["independent_source_media_path"] == str(inputs["independent_source_media_path"])
    assert result["independent_source_media_sha256"] == inputs["independent_source_media_sha256"]


def test_caption_discovery_accepts_prefixed_hashes_without_relaxing_byte_binding(tmp_path):
    inputs, _, _ = _prepared(tmp_path)
    root, _, _, _ = _cache(tmp_path, inputs)
    locator = _caption_discovery(tmp_path / "prefixed", inputs)
    evidence_path = Path(locator["caption_evidence_path"])
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    evidence["source_media_sha256"] = "sha256:" + evidence["source_media_sha256"]
    evidence["input_transcript_sha256"] = "sha256:" + evidence["input_transcript_sha256"]
    evidence["observations"][0]["frame_sha256"] = "sha256:" + evidence["observations"][0]["frame_sha256"]
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")

    locator["source_media_sha256"] = "sha256:" + locator["source_media_sha256"]
    locator["final_srt_sha256"] = "sha256:" + locator["final_srt_sha256"]
    locator["caption_evidence_sha256"] = "sha256:" + _sha(evidence_path)
    locator["frame"]["frame_sha256"] = "sha256:" + locator["frame"]["frame_sha256"]
    discovery = locator["caption_candidate_discovery"]
    discovery["caption_frame_sha256"] = "sha256:" + discovery["caption_frame_sha256"]
    discovery["caption_text_sha256"] = "sha256:" + discovery["caption_text_sha256"]

    source_frame = dict(locator["frame"])
    result = read_cached_caption_source_candidate(
        cache_root=root,
        locator=locator,
        candidate_id="caption-consumer",
        source_media=inputs["outer_media_path"],
        subtitle_path=inputs["final_srt_path"],
        source_frame=source_frame,
    )

    assert result["status"] == "CANDIDATE_CACHE_AVAILABLE"
    assert result["source_role_authority"] is False


def test_cache_aware_discovery_prioritizes_verified_cache_without_expanding_metadata_batch(tmp_path):
    inputs, _, _ = _prepared(tmp_path)
    root, _, _, _ = _cache(tmp_path, inputs)
    calls = []
    locator = _caption_discovery(
        tmp_path / "cache-first",
        inputs,
        cache_root=root,
        cache_candidate_index=1,
    )
    for row in locator["caption_candidate_discovery"]["metadata_candidates"]:
        calls.append(row["bvid"])

    discovery = locator["caption_candidate_discovery"]
    probes = discovery["cache_candidate_probes"]
    metadata_rows = discovery["metadata_candidates"]
    assert [row["search_index"] for row in discovery["search_candidate_order"]] == list(range(4))
    assert probes[1]["status"] == "CACHE_VALIDATED"
    assert probes[1]["cache_identity"]["bvid"] == "BVTEST123456"
    assert [row["search_index"] for row in metadata_rows] == [1, 0, 2]
    assert [row["selection_reason"] for row in metadata_rows] == [
        "VERIFIED_CACHE_FIRST", "SEARCH_RESPONSE_ORDER_FILL", "SEARCH_RESPONSE_ORDER_FILL",
    ]
    assert calls == ["BVTEST123456", "BV1KwHi6PEKE", "BV1RGbr6jEhv"]

    consumer = read_cached_caption_source_candidate(
        cache_root=root,
        locator=locator,
        candidate_id="caption-consumer",
        source_media=inputs["outer_media_path"],
        subtitle_path=inputs["final_srt_path"],
    )
    assert consumer["status"] == "CANDIDATE_CACHE_AVAILABLE"
    assert consumer["search_index"] == 1
    assert consumer["selection_reason"] == "VERIFIED_CACHE_FIRST"
    assert consumer["cache_validation"]["status"] == "CACHE_VALIDATED"
    assert consumer["source_role_authority"] is False


@pytest.mark.parametrize("title", [
    "Discussion     please share your thoughts",
    "Discussion\t\nplease share your thoughts",
    "<em>Discussion</em> &amp; comments",
])
def test_uncached_metadata_title_uses_producer_normalization_without_rebinding(tmp_path, title):
    inputs, _, _ = _prepared(tmp_path)
    root, _, _, _ = _cache(tmp_path, inputs)
    locator = _caption_discovery(
        tmp_path / "normalized-title", inputs, cache_root=root,
        cache_candidate_index=1, uncached_title=title,
    )
    before = copy.deepcopy(locator)
    consumer = read_cached_caption_source_candidate(
        cache_root=root, locator=locator, candidate_id="caption-consumer",
        source_media=inputs["outer_media_path"], subtitle_path=inputs["final_srt_path"],
    )
    assert consumer["status"] == "CANDIDATE_CACHE_AVAILABLE"
    assert consumer["source_cache_binding"]["bvid"] == "BVTEST123456"
    assert consumer["source_role_authority"] is False
    assert locator == before

    # Even an equivalent title cannot replace the original response bytes.
    row = locator["caption_candidate_discovery"]["metadata_candidates"][1]
    row["response"]["data"]["title"] += " "
    with pytest.raises(ValueError, match="CAPTION_DISCOVERY_METADATA_RESPONSE_DRIFT"):
        read_cached_caption_source_candidate(
            cache_root=root, locator=locator, candidate_id="caption-consumer",
            source_media=inputs["outer_media_path"], subtitle_path=inputs["final_srt_path"],
        )


def test_cache_aware_discovery_missing_cache_keeps_original_order(tmp_path):
    inputs, _, _ = _prepared(tmp_path)
    locator = _caption_discovery(
        tmp_path / "cache-missing",
        inputs,
        cache_root=tmp_path / "missing-cache",
        cache_candidate_index=1,
    )
    discovery = locator["caption_candidate_discovery"]
    assert [row["search_index"] for row in discovery["metadata_candidates"]] == [0, 1, 2]
    assert all(
        row["status"] == "CACHE_UNAVAILABLE"
        for row in discovery["cache_candidate_probes"]
    )
    assert all(
        row["selection_reason"] == "SEARCH_RESPONSE_ORDER_FILL"
        for row in discovery["metadata_candidates"]
    )


def test_caption_discovery_cache_candidate_rejects_current_binding_or_raw_response_drift(tmp_path):
    inputs, _, _ = _prepared(tmp_path)
    root, _, _, _ = _cache(tmp_path, inputs)
    locator = _caption_discovery(tmp_path, inputs)
    locator["caption_candidate_discovery"]["search_response"] = []
    with pytest.raises(ValueError, match="CAPTION_DISCOVERY_SEARCH_RESPONSE_DRIFT"):
        read_cached_caption_source_candidate(
            cache_root=root,
            locator=locator,
            candidate_id="caption-consumer",
            source_media=inputs["outer_media_path"],
            subtitle_path=inputs["final_srt_path"],
        )

    locator = _caption_discovery(tmp_path / "frame-drift", inputs)
    frame = dict(locator["frame"])
    frame["frame_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="CAPTION_DISCOVERY_FRAME_DRIFT"):
        read_cached_caption_source_candidate(
            cache_root=root,
            locator=locator,
            candidate_id="caption-consumer",
            source_media=inputs["outer_media_path"],
            subtitle_path=inputs["final_srt_path"],
            source_frame=frame,
        )


def test_caption_discovery_without_cache_returns_candidate_fact_only(tmp_path):
    inputs, _, _ = _prepared(tmp_path)
    locator = _caption_discovery(tmp_path, inputs)
    result = read_cached_caption_source_candidate(
        cache_root=tmp_path / "missing-cache",
        locator=locator,
        candidate_id="caption-consumer",
        source_media=inputs["outer_media_path"],
        subtitle_path=inputs["final_srt_path"],
    )
    assert result["status"] == "CANDIDATE_CACHE_UNAVAILABLE"
    assert result["source_role_authority"] is False
    assert result["selection_policy"] == "FIRST_AVAILABLE_PUBLIC_SEARCH_CANDIDATE"


@pytest.mark.parametrize("drift", ["media", "wrong_download", "cid", "locator_srt", "metadata_identity"])
def test_source_origin_drift_is_rejected_before_consumption(tmp_path, drift):
    inputs, _, _ = _prepared(tmp_path)
    root, entry_path, entry, locator = _cache(tmp_path, inputs)
    if drift == "media":
        Path(inputs["independent_source_media_path"]).write_bytes(b"changed-media")
    elif drift == "locator_srt":
        locator["final_srt_sha256"] = "0" * 64
    elif drift == "cid":
        log = Path(entry["download_log"]["path"])
        log.write_text("[BiliBili] BVTEST123456: Downloading video formats for cid 654321\n")
        entry["download_log"]["sha256"] = _sha(log)
        _save(entry_path, entry)
    else:
        receipt_path = Path(entry["download_receipt"]["path"])
        receipt = json.loads(receipt_path.read_text())
        if drift == "wrong_download":
            receipt["media"]["sha256"] = "0" * 64
        else:
            metadata_path = Path(entry["metadata"]["path"])
            metadata = json.loads(metadata_path.read_text())
            metadata["id"] = "BVOTHER12345"
            entry["metadata"] = _save(metadata_path, metadata)
            receipt["info"] = entry["metadata"]
        entry["download_receipt"] = _save(receipt_path, receipt)
        _save(entry_path, entry)
    with pytest.raises(ValueError):
        _read(root, locator, inputs)


def test_normal_consumer_resolves_source_while_preserving_input_spec(tmp_path):
    inputs, callbacks, spec = _prepared(tmp_path)
    root, _, _, locator = _cache(tmp_path, inputs)
    spec["nested_media_source_locator"] = locator
    observation = spec["nested_media_acoustic"]["observations"][0]
    del observation["independent_source_media_path"]
    del observation["independent_source_media_sha256"]
    original = copy.deepcopy(spec)
    result = observe_prepared_nested_acoustics(
        spec=spec, candidate_id="candidate", source_media=inputs["outer_media_path"],
        subtitle_path=inputs["final_srt_path"], evidence_root=inputs["evidence_root"],
        callbacks=callbacks, source_cache_root=root,
    )
    row = result["observations"][0]
    assert row["source_cache_binding"]["cid"] == 123456
    assert row["observer_result"]["status"] == "PASS"
    assert spec == original
    assert result["subtitle_mutations"] == 0


@pytest.mark.parametrize("drift", ["locator_srt", "mapping_cid"])
def test_missing_source_cache_keeps_subtitle_without_dispatch(tmp_path, drift):
    inputs, callbacks, spec = _prepared(tmp_path)
    root, _, _, locator = _cache(tmp_path, inputs)
    spec["nested_media_source_locator"] = locator
    observation = spec["nested_media_acoustic"]["observations"][0]
    if drift == "locator_srt":
        locator["final_srt_sha256"] = "0" * 64
    else:
        mapping_path = Path(observation["mapping_receipt_path"])
        mapping = json.loads(mapping_path.read_text())
        mapping["media"]["inner"]["cid"] = "654321"
        _save(mapping_path, mapping)
        observation["mapping_receipt_sha256"] = "sha256:" + _sha(mapping_path)
    del observation["independent_source_media_path"]
    del observation["independent_source_media_sha256"]
    def unavailable(*_args, **_kwargs):
        raise AssertionError("source drift must not dispatch a provider")
    callbacks.observe_audio = callbacks.cpa_judge = unavailable
    result = observe_prepared_nested_acoustics(
        spec=spec, candidate_id="candidate", source_media=inputs["outer_media_path"],
        subtitle_path=inputs["final_srt_path"], evidence_root=inputs["evidence_root"],
        callbacks=callbacks, source_cache_root=root,
    )
    assert result["observations"][0]["label"] == "unknown"
    assert result["observations"][0]["action"] == "KEEP"
    assert result["observations"][0]["reason_code"] == "NESTED_SOURCE_CACHE_UNAVAILABLE"


def _automatic_inputs(tmp_path):
    inputs, callbacks, spec = _prepared(tmp_path)
    root, _, _, locator = _cache(tmp_path, inputs)
    spec["nested_media_source_locator"] = locator
    observation = spec["nested_media_acoustic"]["observations"][0]
    mapping = {"path": observation["mapping_receipt_path"], "sha256": observation["mapping_receipt_sha256"]}
    for field in ("independent_source_media_path", "independent_source_media_sha256",
                  "mapping_receipt_path", "mapping_receipt_sha256", "source_audio_path",
                  "source_audio_sha256", "target_audio_path", "target_audio_sha256"):
        del observation[field]
    return inputs, callbacks, spec, root, mapping


def test_normal_missing_mapping_and_audio_prepares_from_verified_source(tmp_path, monkeypatch):
    inputs, callbacks, spec, root, mapping = _automatic_inputs(tmp_path)
    original = copy.deepcopy(spec)
    measurements = []
    def prepare(**kwargs):
        measurements.append(kwargs)
        assert kwargs["source_identity"] == {"bvid": "BVTEST123456", "cid": 123456}
        receipt_path = Path(mapping["path"])
        receipt = json.loads(receipt_path.read_text())
        receipt["current_cue_scope"] = kwargs["current_cue_scope"]
        receipt["mapping_scope"] = "CURRENT_CUE_REVERSE_NCC"
        receipt_path.write_text(json.dumps(receipt, sort_keys=True))
        mapping["sha256"] = "sha256:" + hashlib.sha256(receipt_path.read_bytes()).hexdigest()
        return {"decision": "MAPPING_CANDIDATE_BOUND", "mapping_receipt": mapping}
    monkeypatch.setattr("src.autoslice.nested_source_audio_alignment.prepare_nested_source_mapping", prepare)
    _fake_pcm_tools(monkeypatch)
    result = observe_prepared_nested_acoustics(
        spec=spec, candidate_id="candidate", source_media=inputs["outer_media_path"],
        subtitle_path=inputs["final_srt_path"], evidence_root=inputs["evidence_root"],
        callbacks=callbacks, source_cache_root=root,
    )
    row = result["observations"][0]
    assert len(measurements) == 1
    assert row["audio_preparation"]["status"] == "PREPARED"
    assert row["observer_result"]["status"] == "PASS"
    assert spec == original
    assert result["subtitle_mutations"] == 0


@pytest.mark.parametrize("failure", ["stale_srt", "stale_frame", "unsupported_mapping", "partial_mapping", "existing_audio"])
def test_automatic_mapping_failure_never_dispatches_role_provider(tmp_path, monkeypatch, failure):
    inputs, callbacks, spec, root, mapping = _automatic_inputs(tmp_path)
    observation = spec["nested_media_acoustic"]["observations"][0]
    measurements = []
    def prepare(**kwargs):
        measurements.append(kwargs)
        return {"decision": "UNKNOWN_KEEP"}
    monkeypatch.setattr("src.autoslice.nested_source_audio_alignment.prepare_nested_source_mapping", prepare)
    if failure == "stale_srt":
        observation["final_srt_sha256"] = "0" * 64
    elif failure == "stale_frame":
        observation["caption_frame_sha256"] = "0" * 64
    elif failure == "partial_mapping":
        observation["mapping_receipt_path"] = mapping["path"]
    elif failure == "existing_audio":
        observation["source_audio_path"] = inputs["source_audio_path"]
    def unavailable(*_args, **_kwargs):
        raise AssertionError("unbound evidence must not dispatch a role provider")
    callbacks.observe_audio = callbacks.cpa_judge = unavailable
    result = observe_prepared_nested_acoustics(
        spec=spec, candidate_id="candidate", source_media=inputs["outer_media_path"],
        subtitle_path=inputs["final_srt_path"], evidence_root=inputs["evidence_root"],
        callbacks=callbacks, source_cache_root=root,
    )
    row = result["observations"][0]
    assert row["label"] == "unknown" and row["action"] == "KEEP"
    assert len(measurements) == (1 if failure == "unsupported_mapping" else 0)
