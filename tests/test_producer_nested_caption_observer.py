import hashlib
import json
from pathlib import Path

import pytest

from src.autoslice.nested_media_caption_dedup import CaptionDedupError
from src.autoslice.producer_nested_caption import consume_nested_caption_policy
from src.autoslice.producer_nested_caption_observer import observe_nested_captions
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
                 "captions": ["这是完整台词1"] if scene == "WATCHED_MEDIA" else []}
                for identity in ids]})}
    return probe


def test_observer_blind_bound_consumer_and_cache_replay(tmp_path):
    kwargs = setup(tmp_path)
    original = kwargs["subtitle_path"].read_bytes()
    calls = []
    spec = observe_nested_captions(**kwargs, probe=mock_probe(calls))
    assert len(calls) == 2
    evidence = json.loads(Path(spec["nested_media_caption_dedup"]["evidence_path"]).read_text())
    assert evidence["observations"][0]["frame_ms"] == 3500
    assert evidence["observations"][0]["host_repeat_or_overlap"] is None
    assert kwargs["subtitle_path"].read_bytes() == original
    replay = observe_nested_captions(**kwargs, probe=lambda *a: pytest.fail("replay dispatched"))
    assert replay == spec
    output = consume_nested_caption_policy(**{k: kwargs[k] for k in (
        "candidate_id", "source_media", "source_start_ms", "source_end_ms", "subtitle_path", "evidence_root")}, spec=spec)
    assert "这是完整台词1" not in output
    assert "这是完整台词2" in output
    assert kwargs["subtitle_path"].with_suffix(".all-source-transcript.txt").read_bytes() == original


def test_sampled_clear_scene_is_byte_identical_and_audited(tmp_path):
    kwargs = setup(tmp_path)
    original = kwargs["subtitle_path"].read_bytes()
    calls, audit = [], {}
    spec = observe_nested_captions(**kwargs, probe=mock_probe(calls, "NOT_WATCHED_MEDIA"), chat_authority_audit=audit)
    assert len(calls) == 1 and spec == {}
    assert kwargs["subtitle_path"].read_bytes() == original
    assert audit["nested_media_caption_observer"]["whole_scene_identity_proven"] is False


@pytest.mark.parametrize("scene,bad,code", [
    ("UNKNOWN", False, "SCENE_UNKNOWN"), ("WATCHED_MEDIA", True, "RESPONSE_INVALID")])
def test_unknown_or_malformed_blocks_without_text_mutation(tmp_path, scene, bad, code):
    kwargs = setup(tmp_path)
    original = kwargs["subtitle_path"].read_bytes()
    calls = []
    with pytest.raises(CaptionDedupError, match=code):
        observe_nested_captions(**kwargs, probe=mock_probe(calls, scene, bad))
    assert kwargs["subtitle_path"].read_bytes() == original
    with pytest.raises(CaptionDedupError):
        observe_nested_captions(**kwargs, probe=lambda *a: pytest.fail("failed request redispatched"))


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


def test_69_cues_seven_calls_and_cap_fail_closed(tmp_path):
    kwargs = setup(tmp_path, 69)
    calls = []
    observe_nested_captions(**kwargs, probe=mock_probe(calls))
    assert len(calls) == 7
    observe_nested_captions(**kwargs, probe=lambda *a: pytest.fail("69 cue replay dispatched"))
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    kwargs = setup(fresh, 73)
    calls = []
    with pytest.raises(CaptionDedupError, match="CUE_CAP_EXCEEDED"):
        observe_nested_captions(**kwargs, probe=mock_probe(calls))
    assert len(calls) == 1


def test_new_bound_text_can_continue_after_soft_target_without_reset(tmp_path):
    kwargs = setup(tmp_path, 69)
    calls, audit = [], {}
    observe_nested_captions(**kwargs, probe=mock_probe(calls), chat_authority_audit=audit)
    state_path = kwargs["cache_root"] / "new-reaction" / "observer.json"
    previous = json.loads(state_path.read_text())
    assert previous["calls_consumed"] == 7
    kwargs["subtitle_path"].write_text(kwargs["subtitle_path"].read_text().replace("台词1", "台词X"))
    observe_nested_captions(**kwargs, probe=mock_probe(calls), chat_authority_audit=audit)
    current = json.loads(state_path.read_text())
    assert current["calls_consumed"] == 14
    assert all(current["batches"][key] == row for key, row in previous["batches"].items())
    assert audit["nested_media_caption_observer"]["cpa_resource_pressure"] == {
        "policy": "SOFT_DISTINCT_REQUESTS_CACHE_FIRST",
        "soft_call_target": 7,
        "charged_calls": 14,
        "over_soft_target": True,
    }
    observe_nested_captions(**kwargs, probe=lambda *a: pytest.fail("same request resent"))
    assert json.loads(state_path.read_text())["calls_consumed"] == 14


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
    assert audit["nested_media_caption_observer"]["calls_consumed"] == 2
