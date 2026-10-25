import hashlib
import json
from pathlib import Path

import pytest

from src.autoslice.nested_media_caption_dedup import CaptionDedupError
from src.autoslice.producer_nested_caption import consume_nested_caption_policy
from src.autoslice.producer_nested_caption_observer import observe_nested_captions
from src.autoslice.producer_nested_external_audio import EXTERNAL_AUDIO_SCENE_SOURCE_ROLE
from src.autoslice import producer_nested_caption_observer as observer_module


def setup(tmp_path, count=2):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source fixture")
    subtitle = tmp_path / "final.srt"
    def time(seconds):
        return f"00:{seconds // 60:02d}:{seconds % 60:02d},000"
    subtitle.write_text("\n\n".join(
        f"{n}\n{time(n * 2)} --> {time(n * 2 + 1)}\n这是完整台词{n}"
        for n in range(1, count + 1)) + "\n")
    return dict(spec={}, candidate_id="new-reaction", source_media=source,
                source_start_ms=1000, source_end_ms=max(10000, count * 2000 + 3000), subtitle_path=subtitle,
                evidence_root=tmp_path, cache_root=tmp_path / "cache",
                extract_frame=lambda source, ms: f"jpeg-{ms}".encode())


def mock_probe(calls, scene="WATCHED_MEDIA", bad=False):
    def probe(paths, question):
        calls.append((paths, question))
        assert "这是完整台词" not in question
        ids = json.loads(question.splitlines()[-1])["frame_ids"]
        return {"status": "OBSERVED", "images": [
            {"image_path": str(path), "image_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            for path in paths], "answer": json.dumps({"frames": [
                {"frame_id": identity if not bad else "wrong", "scene": scene,
                 "captions": ["这是完整台词1"] if scene == "WATCHED_MEDIA" and 'scene_validation' not in question else []}
                for identity in ids]})}
    return probe


def test_observer_blind_bound_consumer_and_cache_replay(tmp_path):
    kwargs = setup(tmp_path)
    original = kwargs["subtitle_path"].read_bytes()
    calls = []
    spec = observe_nested_captions(**kwargs, probe=mock_probe(calls))
    assert len(calls) == 3
    evidence = json.loads(Path(spec["nested_media_caption_dedup"]["evidence_path"]).read_text())
    assert evidence["observations"][0]["frame_ms"] == 3500
    assert evidence["observations"][0]["host_repeat_or_overlap"] is None
    assert kwargs["subtitle_path"].read_bytes() == original
    replay = observe_nested_captions(**kwargs, probe=lambda *a: pytest.fail("replay dispatched"))
    assert replay == spec
    chat = {}
    output = consume_nested_caption_policy(**{k: kwargs[k] for k in (
        "candidate_id", "source_media", "source_start_ms", "source_end_ms", "subtitle_path", "evidence_root")},
        spec=spec, chat_authority_audit=chat)
    assert "这是完整台词1" not in output
    assert "这是完整台词2" in output
    archive = Path(chat["nested_media_caption_dedup"]["all_source_transcript_path"])
    assert archive.read_bytes() == original


def test_sampled_clear_scene_is_byte_identical_and_audited(tmp_path):
    kwargs = setup(tmp_path)
    original = kwargs["subtitle_path"].read_bytes()
    calls, audit = [], {}
    spec = observe_nested_captions(**kwargs, probe=mock_probe(calls, "NOT_WATCHED_MEDIA"), chat_authority_audit=audit)
    assert len(calls) == 1 and spec == {}
    assert kwargs["subtitle_path"].read_bytes() == original
    assert audit["nested_media_caption_observer"]["whole_scene_identity_proven"] is False


def test_audio_button_controls_are_preserved_without_playback_proof(tmp_path):
    kwargs = setup(tmp_path, count=2)
    kwargs["source_start_ms"] = 0
    kwargs["source_end_ms"] = 10000
    calls, audit = [], {}

    def probe(paths, question):
        receipt = mock_probe(calls, "NOT_WATCHED_MEDIA")(paths, question)
        parsed = json.loads(receipt["answer"])
        for row in parsed["frames"]:
            row["source_controls"] = {
                "visible_ui": ["喜欢你", "我讨厌你"],
                "button_under_cursor": "我讨厌你",
                "visible_site_or_url": None,
            }
        receipt["answer"] = json.dumps(parsed, ensure_ascii=False)
        return receipt

    effective = observe_nested_captions(**kwargs, probe=probe, chat_authority_audit=audit)

    assert effective.get("watched_media") is not True
    assert effective["nested_media_external_audio_scene"] is True
    evidence = json.loads(Path(effective["nested_media_caption_dedup"]["evidence_path"]).read_text())
    assert evidence["scene_type"] == "NOT_WATCHED_MEDIA_EXTERNAL_AUDIO_BUTTON"
    assert evidence["source_role"] == EXTERNAL_AUDIO_SCENE_SOURCE_ROLE
    assert evidence["observations"] == []
    assert evidence["scene_observations"][0]["scene"] == "NOT_WATCHED_MEDIA"
    assert evidence["scene_observations"][0]["source_controls"]["button_under_cursor"] == "我讨厌你"
    assert audit["nested_media_caption_observer"]["source_controls_present"] is True


def test_audio_button_cursor_label_must_be_visible(tmp_path):
    kwargs = setup(tmp_path)

    def probe(paths, question):
        receipt = mock_probe([], "NOT_WATCHED_MEDIA")(paths, question)
        parsed = json.loads(receipt["answer"])
        parsed["frames"][0]["source_controls"] = {
            "visible_ui": ["喜欢你"],
            "button_under_cursor": "我讨厌你",
            "visible_site_or_url": None,
        }
        receipt["answer"] = json.dumps(parsed, ensure_ascii=False)
        return receipt

    with pytest.raises(CaptionDedupError, match="RESPONSE_INVALID"):
        observe_nested_captions(**kwargs, probe=probe)


@pytest.mark.parametrize("validation_scene", ["NOT_WATCHED_MEDIA", "UNKNOWN"])
def test_inconclusive_discovery_keeps_unknown_without_scene_claim(tmp_path, validation_scene):
    kwargs = setup(tmp_path, count=13)
    original = kwargs["subtitle_path"].read_bytes()
    calls, audit = [], {}

    def probe(paths, question):
        scene = validation_scene if 'scene_ownership_validation' in question else 'UNKNOWN'
        return mock_probe(calls, scene)(paths, question)

    spec = observe_nested_captions(**kwargs, probe=probe, chat_authority_audit=audit)
    assert spec == {}
    result = audit["nested_media_caption_observer"]
    assert all(row["scene"] == "UNKNOWN" for row in result["survey"])
    assert result["negative_scene_confirmed"] is (validation_scene == "NOT_WATCHED_MEDIA")
    assert len(result["scene_ownership_validation"]) == 13
    assert result["speaker_identity_proven"] is False
    assert result["whole_scene_identity_proven"] is False
    if validation_scene == "UNKNOWN":
        assert result["status"] == "SCENE_UNRESOLVED_KEEP_ALL"
        assert result["unknown_action"] == "KEEP"
        assert result["display_mutation_authorized"] is False
        assert result["unknown_cue_indexes"] == list(range(1, 14))
    else:
        assert result["status"] == "SAMPLED_NOT_WATCHED_MEDIA"
    output = consume_nested_caption_policy(**{k: kwargs[k] for k in (
        "candidate_id", "source_media", "source_start_ms", "source_end_ms", "subtitle_path", "evidence_root")},
        spec=spec, chat_authority_audit=audit)
    assert output.encode() == original
    assert kwargs["subtitle_path"].read_bytes() == original
    before = len(calls)
    replay_audit = {}
    assert observe_nested_captions(**kwargs, probe=lambda *a: pytest.fail("same scene request redispatched"),
                                   chat_authority_audit=replay_audit) == spec
    assert replay_audit["nested_media_caption_observer"] == result
    assert len(calls) == before


def test_brief_player_between_unknown_survey_frames_is_verified_without_reprobing(tmp_path):
    kwargs = setup(tmp_path, count=13)
    original = kwargs["subtitle_path"].read_bytes()
    calls, audit = [], {}

    def probe(paths, question):
        receipt = mock_probe(calls, "UNKNOWN")(paths, question)
        parsed = json.loads(receipt["answer"])
        for row in parsed["frames"]:
            if row["frame_id"] == "cue-2":
                row.update(scene="WATCHED_MEDIA", captions=(
                    [] if "scene_validation" in question else ["这是完整台词2"]))
        receipt["answer"] = json.dumps(parsed)
        return receipt

    spec = observe_nested_captions(**kwargs, probe=probe, chat_authority_audit=audit)
    assert len(calls) == 3  # survey, unseen frames, independent player validation
    assert kwargs["subtitle_path"].read_bytes() == original
    evidence = json.loads(Path(spec["nested_media_caption_dedup"]["evidence_path"]).read_text())
    assert len(evidence["scene_observations"]) == 13
    assert evidence["scene_observations"][0]["scene"] == "UNKNOWN"
    assert evidence["scene_observations"][1]["scene"] == "WATCHED_MEDIA"
    assert audit["nested_media_caption_observer"]["unknown_cue_indexes"] == [1, *range(3, 14)]
    assert audit["nested_media_caption_observer"]["inconclusive_survey_fallback"]["validation_frame_ids"] == ["cue-2"]
    replay = observe_nested_captions(**kwargs, probe=lambda *a: pytest.fail("same pixels redispatched"))
    assert replay == spec


def test_rejected_first_discovery_continues_to_unseen_player_frames(tmp_path):
    kwargs = setup(tmp_path, count=30)
    calls, audit = [], {}

    def probe(paths, question):
        receipt = mock_probe(calls, "UNKNOWN")(paths, question)
        parsed = json.loads(receipt["answer"])
        for row in parsed["frames"]:
            if row["frame_id"] in {"cue-2", "cue-20"}:
                if "scene_validation" not in question or row["frame_id"] == "cue-20":
                    row.update(scene="WATCHED_MEDIA", captions=[])
        receipt["answer"] = json.dumps(parsed)
        return receipt

    spec = observe_nested_captions(**kwargs, probe=probe, chat_authority_audit=audit)
    observed = audit["nested_media_caption_observer"]
    assert observed["inconclusive_survey_fallback"]["validation_frame_ids"] == ["cue-20"]
    assert observed["inconclusive_survey_fallback"]["rejected_discoveries"] == [
        [{"frame_id": "cue-2", "scene": "UNKNOWN", "captions": []}]]
    assert len(json.loads(Path(spec["nested_media_caption_dedup"]["evidence_path"]).read_text())["scene_observations"]) == 30
    assert observe_nested_captions(**kwargs, probe=lambda *a: pytest.fail("same pixels redispatched")) == spec


@pytest.mark.parametrize("scene,bad,code", [
    ("UNKNOWN", False, "SCENE_UNKNOWN"), ("WATCHED_MEDIA", True, "RESPONSE_INVALID")])
def test_declared_watched_unknown_or_malformed_blocks_without_text_mutation(tmp_path, scene, bad, code):
    kwargs = setup(tmp_path)
    kwargs["spec"] = {"watched_media": True}
    original = kwargs["subtitle_path"].read_bytes()
    calls = []
    with pytest.raises(CaptionDedupError, match=code):
        observe_nested_captions(**kwargs, probe=mock_probe(calls, scene, bad))
    assert kwargs["subtitle_path"].read_bytes() == original
    with pytest.raises(CaptionDedupError):
        observe_nested_captions(**kwargs, probe=lambda *a: pytest.fail("failed request redispatched"))


@pytest.mark.parametrize(
    ("variant", "expected_reason"),
    [
        ("invalid_frame", "CAPTION_OBSERVER_RESPONSE_INVALID"),
        ("unknown_caption", "CAPTION_OBSERVER_RESPONSE_INVALID"),
        ("unavailable", "CAPTION_OBSERVER_PROVIDER_UNAVAILABLE"),
    ],
)
def test_failed_batch_retains_probe_receipt_and_reason_without_retry(
    tmp_path, variant, expected_reason,
):
    kwargs = setup(tmp_path)
    calls = []
    receipts = []

    def probe(paths, question):
        receipt = mock_probe(calls)(paths, question)
        if variant == "unavailable":
            receipt["status"] = "UNAVAILABLE"
            receipt["reason_code"] = "REAL_PROVIDER_UNAVAILABLE"
        else:
            parsed = json.loads(receipt["answer"])
            if variant == "invalid_frame":
                parsed["frames"][0]["frame_id"] = "wrong-frame"
            else:
                parsed["frames"][0].update(scene="UNKNOWN", captions=["illegal caption"])
            receipt["answer"] = json.dumps(parsed, ensure_ascii=False)
        receipts.append(json.loads(json.dumps(receipt)))
        return receipt

    with pytest.raises(CaptionDedupError, match=expected_reason):
        observe_nested_captions(**kwargs, probe=probe)

    state = json.loads((kwargs["cache_root"] / "new-reaction" / "observer.json").read_text())
    failed = next(iter(state["batches"].values()))
    assert failed["status"] == "FAILED"
    assert failed["error_type"] == "CaptionDedupError"
    assert failed["reason_code"] == expected_reason
    assert failed["receipt"] == receipts[0]
    if variant == "unavailable":
        assert failed["receipt"]["status"] == "UNAVAILABLE"
        assert failed["receipt"]["reason_code"] == "REAL_PROVIDER_UNAVAILABLE"

    with pytest.raises(CaptionDedupError, match="AMBIGUOUS"):
        observe_nested_captions(
            **kwargs,
            probe=lambda *args: pytest.fail("failed receipt was redispatched"),
        )
    assert len(calls) == 1


def test_confirmed_507_resumes_after_backoff_without_refund_or_receipt_loss(tmp_path, monkeypatch):
    kwargs = setup(tmp_path)
    clock, calls = [1000.0], []
    monkeypatch.setattr(observer_module.time, 'time', lambda: clock[0])
    def failed(paths, question):
        receipt = mock_probe(calls)(paths, question)
        receipt.update(status='UNAVAILABLE', provider='cpa', reason_code='VISION_CALL_FAILED',
                       error='HTTPError: HTTP Error 507: Insufficient Storage')
        return receipt
    with pytest.raises(CaptionDedupError, match='PROVIDER_UNAVAILABLE'):
        observe_nested_captions(**kwargs, probe=failed)
    path = kwargs['cache_root'] / 'new-reaction' / 'observer.json'
    original = json.loads(path.read_text())
    key, failure = next(iter(original['batches'].items()))
    clock[0] = 1100.0
    with pytest.raises(CaptionDedupError, match='RETRY_BACKOFF'):
        observe_nested_captions(**kwargs, probe=lambda *a: pytest.fail('retry before deadline'))
    scheduled = json.loads(path.read_text())['batches'][key]
    assert scheduled['infra_retry']['retry_not_before_epoch'] == 1900.0
    assert scheduled['receipt'] == failure['receipt']
    clock[0] = 1900.0
    observe_nested_captions(**kwargs, probe=mock_probe(calls))
    state = json.loads(path.read_text())
    assert state['calls_consumed'] == len(calls) and len(calls) >= 2
    assert state['batches'][key]['status'] == 'COMPLETE'
    previous, = state['batches'][key]['prior_attempts']
    assert all(previous[k] == v for k, v in failure.items())
    assert previous['receipt']['status'] == 'UNAVAILABLE'
    before = path.read_bytes()
    observe_nested_captions(**kwargs, probe=lambda *a: pytest.fail('completed result must replay'))
    assert path.read_bytes() == before


def test_confirmed_http_failure_backoff_grows_and_legacy_clock_is_conservative(tmp_path, monkeypatch):
    kwargs = setup(tmp_path)
    clock, calls = [1000.0], []
    monkeypatch.setattr(observer_module.time, 'time', lambda: clock[0])
    def failed(paths, question):
        receipt = mock_probe(calls)(paths, question)
        receipt.update(status='UNAVAILABLE', provider='cpa', reason_code='VISION_CALL_FAILED',
            provider_status_code=507, dispatch_outcome='HTTP_ERROR_RESPONSE', error='original server error')
        return receipt
    with pytest.raises(CaptionDedupError):
        observe_nested_captions(**kwargs, probe=failed)
    path = kwargs['cache_root'] / 'new-reaction' / 'observer.json'
    state = json.loads(path.read_text())
    key, claim = next(iter(state['batches'].items()))
    del claim['failed_at_epoch']  # Real legacy cache has no dispatch clock.
    path.write_text(json.dumps(state))
    import os
    os.utime(path, (1000.0, 1000.0))
    clock[0] = 1900.0
    with pytest.raises(CaptionDedupError, match='PROVIDER_UNAVAILABLE'):
        observe_nested_captions(**kwargs, probe=failed)
    state = json.loads(path.read_text())
    assert state['batches'][key]['prior_attempts'][0]['infra_retry']['failure_time_source'] == 'legacy_cache_mtime'
    clock[0] = 3699.0
    with pytest.raises(CaptionDedupError, match='RETRY_BACKOFF'):
        observe_nested_captions(**kwargs, probe=lambda *a: pytest.fail('second backoff is thirty minutes'))
    assert json.loads(path.read_text())['batches'][key]['infra_retry']['retry_not_before_epoch'] == 3700.0
    assert len(calls) == 2


@pytest.mark.parametrize('error', ['TimeoutError: read timed out', 'HTTPError: HTTP Error 401: Unauthorized',
                                  'HTTPError: HTTP Error 400: Bad Request'])
def test_unknown_or_rejected_visual_dispatch_is_not_retried_as_infrastructure(tmp_path, error):
    kwargs = setup(tmp_path)
    def failed(paths, question):
        receipt = mock_probe([])(paths, question)
        receipt.update(status='UNAVAILABLE', provider='cpa', reason_code='VISION_CALL_FAILED', error=error)
        return receipt
    with pytest.raises(CaptionDedupError):
        observe_nested_captions(**kwargs, probe=failed)
    with pytest.raises(CaptionDedupError, match='AMBIGUOUS'):
        observe_nested_captions(**kwargs, probe=lambda *a: pytest.fail('unsafe redispatch'))


@pytest.mark.parametrize('reason', ['CAPTION_OBSERVER_PROVIDER_UNAVAILABLE: HTTP 507', 'CAPTION_OBSERVER_PROVIDER_RETRY_BACKOFF'])
def test_visual_provider_wait_stays_in_ordinary_infrastructure_lane(reason):
    from scripts import session_autoslice as runner
    result = runner.classify_talk_failure('CaptionDedupError: ' + reason)
    assert result['failure_kind'] == 'provider_transient'
    assert result['failure_stage'] == 'caption_observer'
    assert result['failure_recoverable'] is True


def test_ambiguous_dispatch_is_not_refunded_or_resent(tmp_path):
    kwargs = setup(tmp_path)
    def interrupted(*args):
        raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        observe_nested_captions(**kwargs, probe=interrupted)
    with pytest.raises(CaptionDedupError, match="AMBIGUOUS"):
        observe_nested_captions(**kwargs, probe=lambda *a: pytest.fail("ambiguous dispatch resent"))
    state = json.loads((kwargs["cache_root"] / "new-reaction" / "observer.json").read_text())
    assert state["calls_consumed"] == 1


def test_prebound_manual_evidence_skips_all_observation(tmp_path):
    kwargs = setup(tmp_path)
    kwargs["spec"] = {"watched_media": True, "nested_media_caption_dedup": {"existing": True}}
    assert observe_nested_captions(**kwargs, probe=lambda *a: pytest.fail("manual evidence reprobed")) == kwargs["spec"]


def test_declared_scene_cannot_be_silently_downgraded(tmp_path):
    kwargs = setup(tmp_path)
    kwargs["spec"] = {"watched_media": True}
    with pytest.raises(CaptionDedupError, match="DECLARED_SCENE_CONFLICT"):
        observe_nested_captions(**kwargs, probe=mock_probe([], "NOT_WATCHED_MEDIA"))


def test_changed_transcript_uses_same_budget(tmp_path):
    kwargs = setup(tmp_path)
    observe_nested_captions(**kwargs, probe=mock_probe([], "NOT_WATCHED_MEDIA"))
    kwargs["subtitle_path"].write_text(kwargs["subtitle_path"].read_text().replace("台词1", "台词X"))
    observe_nested_captions(**kwargs, probe=mock_probe([], "NOT_WATCHED_MEDIA"))
    state = json.loads((kwargs["cache_root"] / "new-reaction" / "observer.json").read_text())
    assert state["calls_consumed"] == 2


def test_long_watched_scene_continues_beyond_soft_target_and_replays(tmp_path):
    kwargs = setup(tmp_path, 69)
    calls = []
    observe_nested_captions(**kwargs, probe=mock_probe(calls))
    assert len(calls) == 8
    observe_nested_captions(**kwargs, probe=lambda *a: pytest.fail("69 cue replay dispatched"))
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    kwargs = setup(fresh, 73)
    calls, audit = [], {}
    original = kwargs["subtitle_path"].read_bytes()
    spec = observe_nested_captions(**kwargs, probe=mock_probe(calls), chat_authority_audit=audit)
    assert len(calls) == 9
    assert audit["nested_media_caption_observer"]["cue_frames"] == 73
    assert audit["nested_media_caption_observer"]["cpa_resource_pressure"]["over_soft_target"] is True
    assert kwargs["subtitle_path"].read_bytes() == original
    replay = observe_nested_captions(**kwargs, probe=lambda *a: pytest.fail("long scene replay dispatched"))
    assert replay == spec


def test_new_bound_text_can_continue_after_soft_target_without_reset(tmp_path):
    kwargs = setup(tmp_path, 69)
    calls, audit = [], {}
    observe_nested_captions(**kwargs, probe=mock_probe(calls), chat_authority_audit=audit)
    state_path = kwargs["cache_root"] / "new-reaction" / "observer.json"
    previous = json.loads(state_path.read_text())
    assert previous["calls_consumed"] == 8
    kwargs["subtitle_path"].write_text(kwargs["subtitle_path"].read_text().replace("台词1", "台词X"))
    observe_nested_captions(**kwargs, probe=mock_probe(calls), chat_authority_audit=audit)
    current = json.loads(state_path.read_text())
    assert current["calls_consumed"] == 16
    assert all(current["batches"][key] == row for key, row in previous["batches"].items())
    assert audit["nested_media_caption_observer"]["cpa_resource_pressure"] == {
        "policy": "SOFT_DISTINCT_REQUESTS_CACHE_FIRST",
        "soft_call_target": 7,
        "charged_calls": 16,
        "over_soft_target": True,
    }
    observe_nested_captions(**kwargs, probe=lambda *a: pytest.fail("same request resent"))
    assert json.loads(state_path.read_text())["calls_consumed"] == 16


def test_unknown_cue_in_watched_scene_stays_in_display(tmp_path):
    kwargs = setup(tmp_path)
    def probe(paths, question):
        receipt = mock_probe([])(paths, question)
        rows = json.loads(receipt["answer"])["frames"]
        if 'visible_caption_read' in question:
            rows[1].update(scene="UNKNOWN", captions=[])
        receipt["answer"] = json.dumps({"frames": rows})
        return receipt
    audit = {}
    spec = observe_nested_captions(**kwargs, probe=probe, chat_authority_audit=audit)
    output = consume_nested_caption_policy(**{k: kwargs[k] for k in (
        "candidate_id", "source_media", "source_start_ms", "source_end_ms", "subtitle_path", "evidence_root")}, spec=spec)
    assert "这是完整台词1" not in output
    assert "这是完整台词2" in output
    assert audit["nested_media_caption_observer"]["unknown_cue_indexes"] == [2]
    assert audit["nested_media_caption_observer"]["unknown_action"] == "KEEP"


def test_updated_prompt_replays_exact_old_requests_without_new_calls(tmp_path, monkeypatch):
    kwargs = setup(tmp_path)
    first = observe_nested_captions(**kwargs, probe=mock_probe([]))
    monkeypatch.setattr(observer_module, "PROMPT", observer_module.PROMPT + "new wording")
    audit = {}
    replay = observe_nested_captions(**kwargs, probe=lambda *a: pytest.fail("old request recharged"), chat_authority_audit=audit)
    assert replay == first
    assert audit["nested_media_caption_observer"]["replayed_previous_prompt"] is True
    assert audit["nested_media_caption_observer"]["calls_consumed"] == 3


def test_game_scene_rejects_positive_caption_survey_without_deletion(tmp_path):
    kwargs = setup(tmp_path)
    original = kwargs["subtitle_path"].read_bytes()
    calls, audit = [], {}

    def probe(paths, question):
        scene = "NOT_WATCHED_MEDIA" if 'scene_validation' in question else "WATCHED_MEDIA"
        return mock_probe(calls, scene)(paths, question)

    spec = observe_nested_captions(**kwargs, probe=probe, chat_authority_audit=audit)
    assert spec == {}
    assert len(calls) == 2
    assert kwargs["subtitle_path"].read_bytes() == original
    assert audit["nested_media_caption_observer"]["survey_positive_rejected"] is True
    assert audit["nested_media_caption_observer"]["status"] == "SAMPLED_NOT_WATCHED_MEDIA"
    replay = observe_nested_captions(**kwargs, probe=lambda *a: pytest.fail("game recheck redispatched"))
    assert replay == spec


def test_positive_scene_with_black_unknown_frame_keeps_unknown_cue_evidence(tmp_path):
    kwargs = setup(tmp_path)
    calls = []
    base = mock_probe(calls)

    def partly_unknown(paths, question):
        receipt = base(paths, question)
        parsed = json.loads(receipt["answer"])
        if "scene_validation" in question or "visible_caption_read" in question:
            parsed["frames"][-1].update(scene="UNKNOWN", captions=[])
        receipt["answer"] = json.dumps(parsed)
        return receipt

    spec = observe_nested_captions(**kwargs, probe=partly_unknown)
    evidence = json.loads(Path(spec["nested_media_caption_dedup"]["evidence_path"]).read_text())
    assert spec["watched_media"] is True
    assert evidence["scene_observations"][-1]["scene"] == "UNKNOWN"
    assert all(row["cue_index"] != 2 for row in evidence["observations"])
    result = json.loads((kwargs["cache_root"] / "new-reaction" / "RESULT.json").read_text())
    assert result["unknown_cue_indexes"] == [2]
    assert result["whole_scene_identity_proven"] is False


def test_current_recut_with_same_srt_does_not_overwrite_padded_evidence(tmp_path):
    kwargs = setup(tmp_path)
    first = observe_nested_captions(**kwargs, probe=mock_probe([]))
    old_path = Path(first["nested_media_caption_dedup"]["evidence_path"])
    old_bytes = old_path.read_bytes()
    kwargs["source_media"] = tmp_path / "different-recut.mp4"
    kwargs["source_media"].write_bytes(b"new recut with same transcript")
    second = observe_nested_captions(**kwargs, probe=mock_probe([]))
    current_path = Path(second["nested_media_caption_dedup"]["evidence_path"])
    assert current_path != old_path and old_path.read_bytes() == old_bytes
    assert json.loads(current_path.read_text())["source_media_sha256"] != json.loads(old_bytes)["source_media_sha256"]


@pytest.mark.parametrize("controls", [None, {
    "visible_ui": ["喜欢你"], "button_under_cursor": None, "visible_site_or_url": None,
}])
def test_focus_reuses_sampled_control_inspection_without_new_request(tmp_path, controls):
    kwargs = setup(tmp_path, count=2)
    calls, audit = [], {}

    def probe(paths, question):
        receipt = mock_probe(calls, "NOT_WATCHED_MEDIA")(paths, question)
        parsed = json.loads(receipt["answer"])
        for row in parsed["frames"]:
            row["source_controls"] = controls
        receipt["answer"] = json.dumps(parsed)
        return receipt

    result = observe_nested_captions(
        **kwargs, focus_cue_indexes=[1], probe=probe,
        chat_authority_audit=audit,
    )

    assert bool(result.get("nested_media_external_audio_scene")) == (controls is not None)
    assert len(calls) == 1
    state = json.loads((kwargs["cache_root"] / "new-reaction" / "observer.json").read_text())
    phases = [json.loads(row["request"]["question"].splitlines()[-1])["phase"]
              for row in state["batches"].values()]
    assert phases == ["scene_survey"]
    assert audit["nested_media_caption_observer"]["focus_cue_indexes"] == [1]
    assert audit["nested_media_caption_observer"]["focus_observations"][0]["cue_index"] == 1
    assert audit["nested_media_caption_observer"]["focus_observations"][0]["source_controls"] == controls

    assert observe_nested_captions(
        **kwargs, focus_cue_indexes=[1],
        probe=lambda *args: pytest.fail("inspected controls redispatched"),
    ) == result


def test_focus_detail_does_not_reuse_legacy_1280_request(tmp_path):
    kwargs = setup(tmp_path, count=13)
    calls, audit = [], {}
    focus_extract_calls = []
    original_srt = kwargs["subtitle_path"].read_bytes()

    def extract_focus_frame(source, ms):
        focus_extract_calls.append(ms)
        return f"focus-controls-{ms}".encode()

    def probe(paths, question):
        receipt = mock_probe(calls, "NOT_WATCHED_MEDIA")(paths, question)
        metadata = json.loads(question.splitlines()[-1])
        if metadata["phase"] == "scene_focus":
            parsed = json.loads(receipt["answer"])
            for row in parsed["frames"]:
                row["source_controls"] = {
                    "visible_ui": ["喜欢你"],
                    "button_under_cursor": "喜欢你",
                    "visible_site_or_url": None,
                }
            receipt["answer"] = json.dumps(parsed, ensure_ascii=False)
        return receipt

    result = observe_nested_captions(
        **kwargs, focus_cue_indexes=[7], probe=probe,
        extract_focus_frame=extract_focus_frame, chat_authority_audit=audit,
    )
    assert len(calls) == 2
    assert focus_extract_calls == [15500]
    state_path = kwargs["cache_root"] / "new-reaction" / "observer.json"
    state = json.loads(state_path.read_text())

    def phase(row):
        return json.loads(row["request"]["question"].splitlines()[-1])["phase"]

    survey_key, survey_entry = next(
        (key, row) for key, row in state["batches"].items() if phase(row) == "scene_survey"
    )
    focus_key, focus_entry = next(
        (key, row) for key, row in state["batches"].items() if phase(row) == "scene_focus"
    )
    survey_request = json.loads(json.dumps(survey_entry["request"]))
    high_row = focus_entry["request"]["frames"][0]
    assert "focus-controls-1920-v1" in high_row["frame_path"]
    assert high_row["frame_path"] != survey_request["frames"][0]["frame_path"]
    assert high_row["frame_variant"] == "focus-controls-1920-v1"

    # Leave an old 1280 focus receipt in the ledger.  The new detail-bound
    # request must not accept it as the current focus observation.
    legacy_entry = json.loads(json.dumps(focus_entry))
    legacy_row = json.loads(json.dumps(high_row))
    legacy_path = kwargs["cache_root"] / "new-reaction" / "frames" / (
        f"{hashlib.sha256(kwargs['source_media'].read_bytes()).hexdigest()}-15500.jpg"
    )
    legacy_path.write_bytes(b"legacy-1280-focus")
    legacy_row.pop("frame_variant")
    legacy_row["frame_path"] = str(legacy_path.absolute())
    legacy_row["frame_sha256"] = hashlib.sha256(legacy_path.read_bytes()).hexdigest()
    legacy_entry["request"]["frames"] = [legacy_row]
    legacy_entry["receipt"]["images"] = [{
        "image_path": legacy_row["frame_path"],
        "image_sha256": legacy_row["frame_sha256"],
    }]
    legacy_key = observer_module._sha(
        json.dumps(legacy_entry["request"], sort_keys=True).encode()
    )
    del state["batches"][focus_key]
    state["batches"][legacy_key] = legacy_entry
    state_path.write_text(json.dumps(state, ensure_ascii=False, sort_keys=True))

    replay_audit = {}
    replay = observe_nested_captions(
        **kwargs, focus_cue_indexes=[7], probe=probe,
        extract_focus_frame=extract_focus_frame, chat_authority_audit=replay_audit,
    )
    assert len(calls) == 3
    assert focus_extract_calls == [15500]
    replay_focus = replay_audit["nested_media_caption_observer"]["focus_observations"][0]
    assert replay_focus["frame_path"] == high_row["frame_path"]
    assert replay_focus["frame_sha256"] == high_row["frame_sha256"]
    assert kwargs["subtitle_path"].read_bytes() == original_srt
    assert json.loads(state_path.read_text())["batches"][survey_key]["request"] == survey_request

    assert observe_nested_captions(
        **kwargs, focus_cue_indexes=[7],
        probe=lambda *args: pytest.fail("same high-resolution focus request redispatched"),
        extract_focus_frame=extract_focus_frame,
    ) == replay


def test_control_binding_uses_the_receipt_frame_resolution(tmp_path):
    kwargs = setup(tmp_path, count=13)
    calls = []

    def probe(paths, question):
        metadata = json.loads(question.splitlines()[-1])
        phase = metadata["phase"]
        if phase == "scene_survey":
            receipt = mock_probe(calls, "NOT_WATCHED_MEDIA")(paths, question)
            parsed = json.loads(receipt["answer"])
            # Make cue 1 establish a watched discovery so cue 8 is checked
            # again by scene_validation on its original survey pixels.
            parsed["frames"][0]["scene"] = "WATCHED_MEDIA"
        elif phase == "scene_focus":
            receipt = mock_probe(calls, "NOT_WATCHED_MEDIA")(paths, question)
            parsed = json.loads(receipt["answer"])
            for row in parsed["frames"]:
                row["source_controls"] = {
                    "visible_ui": ["高分辨率候选"],
                    "button_under_cursor": "高分辨率候选",
                    "visible_site_or_url": None,
                }
        else:
            receipt = mock_probe(calls, "NOT_WATCHED_MEDIA")(paths, question)
            parsed = json.loads(receipt["answer"])
            for row in parsed["frames"]:
                if row["frame_id"] == "cue-8":
                    row["source_controls"] = {
                        "visible_ui": ["survey-validation"],
                        "button_under_cursor": "survey-validation",
                        "visible_site_or_url": None,
                    }
        receipt["answer"] = json.dumps(parsed, ensure_ascii=False)
        return receipt

    result = observe_nested_captions(
        **kwargs, focus_cue_indexes=[8], probe=probe,
    )
    evidence = json.loads(Path(result["nested_media_caption_dedup"]["evidence_path"]).read_text())
    cue8 = next(row for row in evidence["scene_observations"] if row["cue_index"] == 8)
    assert cue8["source_controls"]["button_under_cursor"] == "survey-validation"
    assert "focus-controls-1920-v1" not in cue8["frame_path"]
    assert cue8["frame_sha256"] == hashlib.sha256(Path(cue8["frame_path"]).read_bytes()).hexdigest()
    phases = [json.loads(question.splitlines()[-1])["phase"] for _, question in calls]
    assert phases == ["scene_survey", "scene_focus", "scene_validation"]


@pytest.mark.parametrize("focus_index", [1, 7])
def test_focus_inspects_missing_controls_and_preserves_survey_cache(tmp_path, focus_index):
    kwargs = setup(tmp_path, count=13)
    calls, audit = [], {}

    def probe(paths, question):
        receipt = mock_probe(calls, "NOT_WATCHED_MEDIA")(paths, question)
        metadata = json.loads(question.splitlines()[-1])
        if metadata["phase"] == "scene_focus":
            parsed = json.loads(receipt["answer"])
            for row in parsed["frames"]:
                row["source_controls"] = {
                    "visible_ui": ["喜欢你", "我讨厌你"],
                    "button_under_cursor": "我讨厌你",
                    "visible_site_or_url": None,
                }
            receipt["answer"] = json.dumps(parsed, ensure_ascii=False)
        return receipt

    result = observe_nested_captions(
        **kwargs, focus_cue_indexes=[focus_index], probe=probe, chat_authority_audit=audit,
    )

    assert result["nested_media_external_audio_scene"] is True
    evidence = json.loads(Path(result["nested_media_caption_dedup"]["evidence_path"]).read_text())
    assert [row["cue_index"] for row in evidence["scene_observations"]] == [focus_index]
    assert evidence["scene_observations"][0]["source_controls"]["button_under_cursor"] == "我讨厌你"
    assert result.get("watched_media") is not True
    assert len(calls) == 2
    state = json.loads((kwargs["cache_root"] / "new-reaction" / "observer.json").read_text())
    survey_request = next(
        row["request"] for row in state["batches"].values()
        if json.loads(row["request"]["question"].splitlines()[-1])["phase"] == "scene_survey"
    )
    evidence_bytes = Path(result["nested_media_caption_dedup"]["evidence_path"]).read_bytes()
    evidence_sha = result["nested_media_caption_dedup"]["evidence_sha256"]
    phases = [json.loads(row["request"]["question"].splitlines()[-1])["phase"]
              for row in state["batches"].values()]
    assert sorted(phases) == ["scene_focus", "scene_survey"]
    assert audit["nested_media_caption_observer"]["focus_observations"][0]["cue_index"] == focus_index

    replay = observe_nested_captions(
        **kwargs, focus_cue_indexes=[focus_index],
        probe=lambda *args: pytest.fail("same survey/focus request redispatched"),
    )
    assert replay == result
    assert len(calls) == 2
    replay_state = json.loads((kwargs["cache_root"] / "new-reaction" / "observer.json").read_text())
    replay_survey_request = next(
        row["request"] for row in replay_state["batches"].values()
        if json.loads(row["request"]["question"].splitlines()[-1])["phase"] == "scene_survey"
    )
    assert replay_survey_request == survey_request
    replay_evidence_path = Path(replay["nested_media_caption_dedup"]["evidence_path"])
    assert replay_evidence_path.read_bytes() == evidence_bytes
    assert replay["nested_media_caption_dedup"]["evidence_sha256"] == evidence_sha


def test_focus_unknown_keeps_transcript_and_does_not_authorize_caption_dedup(tmp_path):
    kwargs = setup(tmp_path, count=13)
    original = kwargs["subtitle_path"].read_bytes()
    calls, audit = [], {}

    def probe(paths, question):
        receipt = mock_probe(calls, "NOT_WATCHED_MEDIA")(paths, question)
        metadata = json.loads(question.splitlines()[-1])
        if metadata["phase"] == "scene_focus":
            parsed = json.loads(receipt["answer"])
            for row in parsed["frames"]:
                row.update(scene="UNKNOWN", captions=[], source_controls=None)
            receipt["answer"] = json.dumps(parsed)
        return receipt

    result = observe_nested_captions(
        **kwargs, focus_cue_indexes=[7], probe=probe, chat_authority_audit=audit,
    )

    assert result == {}
    assert kwargs["subtitle_path"].read_bytes() == original
    observed = audit["nested_media_caption_observer"]
    assert observed["focus_unknown_cue_indexes"] == [7]
    assert observed["status"] == "SAMPLED_NOT_WATCHED_MEDIA"


@pytest.mark.parametrize("focus_index", [1, 7])
def test_focus_rejects_omitted_controls_without_repeating_saved_survey(tmp_path, focus_index):
    kwargs = setup(tmp_path, count=13)
    original = kwargs["subtitle_path"].read_bytes()
    observe_nested_captions(**kwargs, probe=mock_probe([], "NOT_WATCHED_MEDIA"))
    state_path = kwargs["cache_root"] / "new-reaction" / "observer.json"
    before = json.loads(state_path.read_text())
    calls = []
    with pytest.raises(CaptionDedupError, match="CAPTION_OBSERVER_RESPONSE_INVALID"):
        observe_nested_captions(
            **kwargs, focus_cue_indexes=[focus_index], probe=mock_probe(calls, "NOT_WATCHED_MEDIA"),
        )
    after = json.loads(state_path.read_text())
    assert len(calls) == 1
    assert after["calls_consumed"] == before["calls_consumed"] + 1
    assert all(after["batches"][key] == row for key, row in before["batches"].items())
    assert kwargs["subtitle_path"].read_bytes() == original


@pytest.mark.parametrize("focus_cue_indexes", [[0], [99], ["7"], [True], [7, 7]])
def test_focus_rejects_invalid_cue_ids(tmp_path, focus_cue_indexes):
    kwargs = setup(tmp_path, count=13)
    with pytest.raises(CaptionDedupError, match="FOCUS_CUES_INVALID"):
        observe_nested_captions(
            **kwargs, focus_cue_indexes=focus_cue_indexes,
            probe=lambda *args: pytest.fail("invalid focus dispatched"),
        )
