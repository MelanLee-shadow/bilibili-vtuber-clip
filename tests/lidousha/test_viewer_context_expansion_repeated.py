import json
from types import SimpleNamespace

import pytest

from scripts import run_full_session_selector_cpa_shadow as shadow
from src.autoslice.auto_review import DecisionAction
from src.autoslice.boundary_resolver import AnchorCandidate, BoundaryResolution
from src.autoslice.full_session_candidate_selector import FullSessionCandidate
from src.autoslice.review_evidence import SourceCue


@pytest.mark.parametrize("final_ok", [True, False])
def test_ordinary_review_repeats_each_available_context_expansion(tmp_path, monkeypatch, final_ok):
    cues = (
        SourceCue("1", 0, 4_000, "前情"),
        SourceCue("2", 10_000, 14_000, "触发"),
        SourceCue("3", 20_000, 24_000, "回应"),
    )
    cid = "semantictalk_20000_24000"
    candidate = FullSessionCandidate(
        anchor=AnchorCandidate(candidate_id=cid, anchor_start_ms=20_000, anchor_end_ms=24_000),
        boundary=BoundaryResolution(candidate_id=cid, action=DecisionAction.AUTO_RECUT,
            resolved_start_ms=20_000, resolved_end_ms=24_000, start_boundary_score=0.9,
            end_boundary_score=0.9, reason_codes=(), next_start_ms=20_000, next_end_ms=24_000),
        cues=cues[2:], text_preview="回应", content_type_hint="talk",
    )
    args = SimpleNamespace(output_dir=tmp_path, cpa_command="unused", room_id="22966160",
        source_video=tmp_path / "source.mp4", source_srt=tmp_path / "source.srt",
        agy_ssh_host=None, copy_draft_context=False, agy_audio_lrc_align=False,
        no_ffmpeg=True, song_hint_llm_command=None, song_lrc_query=(), burn_preview=False,
        publish_staging=False, title_llm_command=None, cover_art_direction_llm_command=None)
    calls = []

    def fake_cpa(**kwargs):
        calls.append((kwargs["start_ms"], kwargs["candidate_text"]))
        ok = final_ok and len(calls) == 3
        kwargs["response_json"].write_text(json.dumps({"metadata": {"viewer_context": {
            "viewer_context_ok": ok, "expand_before_ms": 10_000 if len(calls) < 3 else 0,
            "expand_after_ms": 0}}}))

    def fake_shadow(**kwargs):
        response = json.loads(open(kwargs["source_context_job"]["cpa_semantic_response_path"]).read())
        action = "AUTO_UPLOAD" if response["metadata"]["viewer_context"]["viewer_context_ok"] else "BLOCK"
        return {"records": [{"decision_action": action}]}

    monkeypatch.setattr(shadow, "_run_cpa_script", fake_cpa)
    monkeypatch.setattr(shadow, "run_shadow_pipeline", fake_shadow)
    record, _ = shadow._review_one_selected_candidate(
        args=args, original_candidate=candidate, lexicon=None, duplicate_corpus=[], lrc_provider=None,
        cues=cues, source_duration_ms=24_000, selector_stage="semantic_recall", danmaku_items=[],
        host_vocal_prover=None, branding_intro=None, canonical_lrc_result=None,
        canonical_lrc_provenance_path=None,
    )
    assert [start for start, _text in calls] == [20_000, 10_000, 0]
    assert calls[-1][1].splitlines() == ["前情", "触发", "回应"]
    assert record["viewer_context_expansion"]["original_start_ms"] == 20_000
    assert record["viewer_context_expansion"]["expanded_start_ms"] == 0
    assert record["decision_action"] == ("AUTO_UPLOAD" if final_ok else "BLOCK")
