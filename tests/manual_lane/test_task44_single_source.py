"""TASK-44: independent-family additions, ordinary evidence -> CPA -> writer."""
from __future__ import annotations

import json

import pytest
from fakes import FakeRemote, IdCpa, make_ctx, rules_with
from test_evidence_attach import RD, _srt

from src.autoslice.manual_lane import doubt_policy, evidence_attach
from src.autoslice.manual_lane import stage_impl as si
from src.autoslice.manual_lane.step3_adjudicate import _adjudicate_all
from src.autoslice.manual_lane.step3_doubts import doubt_entry, evidence_extra

MAIN = "今天讨论天气"
EXTRA = "请记得带上雨伞"


def _view(cues, lines):
    att = evidence_attach.own_witness_lines(cues, lines)
    jy = evidence_attach.attach_asr(cues, [], source="jianying")
    ev = si._evidence_for_cue(cues[0], 0, cues, jy, att, lines, [])
    return doubt_entry(cues[0], {"start_ms": 0}, ev, differing=[], lead=evidence_extra(ev))


def _cues(text=MAIN):
    return [{"id": 1, "start_s": 0.0, "end_s": 3.0, "text": text}]


@pytest.mark.parametrize("source", ["asr:mai", "asr:moss", "jianying", "audio:38m@P0"])
def test_one_family_large_addition_counts_as_one_source_not_as_corroboration(source):
    current = MAIN * 8
    text = current + EXTRA
    readings = {source: text}
    if source.startswith("asr:"):
        readings[source + "@win"] = text
    row = next(c for c in doubt_policy.candidates(readings, current) if source in c["sources"])
    family = "asr:jianying" if source == "jianying" else doubt_policy.witness_family(source)
    assert row["families"] == [family] and row["supporters"] == 1
    assert not doubt_policy.maintenance(readings, current)["maintained"]


def test_mai_and_gemini_support_addition_even_when_the_extension_dominates_the_reading():
    text = MAIN + EXTRA * 3
    rows = doubt_policy.candidates({"asr:mai": text, "audio:38m@P0": text}, MAIN)
    row = next(c for c in rows if c["text"] == text)
    assert row["families"] == ["asr:mai", "gemini"]
    assert doubt_policy.maintenance({"asr:mai": text, "audio:38m@P0": text}, MAIN)["maintained"]


def test_asr_notes_and_speaker_segments_do_not_multiply_their_vendor_or_require_retention(tmp_path):
    lines = {"asr:mai": [{"abs_start_s": 0.0, "end_s": 3.0, "text": MAIN, "speaker": "main"},
                         {"abs_start_s": 1.0, "end_s": 2.0, "text": EXTRA, "speaker": "guest"}]}
    d = _view(_cues(), lines)
    assert EXTRA in str(d["asr_notes"]) and EXTRA in str(d["speaker_segments"])
    assert not d["maintenance"]["maintained"]
    mai_rows = [c for c in d["candidates"] if "asr:mai" in c["families"]]
    assert len(mai_rows) == 1 and mai_rows[0]["supporters"] == 1
    cpa = IdCpa()
    ctx = make_ctx(tmp_path, remote=FakeRemote(), cpa=cpa)
    _, failure = _adjudicate_all(ctx, [d], "1 0-3 " + MAIN, [], tag="synthetic")
    assert failure is None
    prompt = cpa.prompts[0]
    assert "听写或上下文支持时保留" not in prompt
    assert "另一人的话" in prompt and "不能删掉" not in prompt
    assert "不止一家" in prompt


def test_genuine_repeat_is_not_a_novel_addition_and_cpa_final_still_stands(tmp_path):
    current = "出门带伞出门带伞"
    rows = doubt_policy.candidates({"asr:mai": "出门带伞"}, current)
    by = {row["text"]: row for row in rows}
    assert by[current]["families"] == ["current"] and by["出门带伞"]["families"] == ["asr:mai"]
    # U107/U108: even a single-source candidate is evidence, not a post-CPA veto.
    ctx = make_ctx(tmp_path, remote=FakeRemote(), cpa=IdCpa())
    decision = doubt_policy.decide({"state": "ADOPT", "final_text": MAIN + EXTRA}, MAIN, maintained=False)
    final, _ = si._final_cues(_cues(), [dict(decision, cue_id=1)])
    assert "".join(c["text"] for c in final).replace(" ", "") == MAIN + EXTRA
    assert ctx.adapters.cpa.calls == []


def test_shared_prefix_does_not_launder_a_single_vendor_tail_into_two_votes():
    shared = MAIN + "我们下午去买东西"
    readings = {"asr:mai": shared + EXTRA, "asr:moss": shared, "audio:38m@P0": shared}
    rows = doubt_policy.candidates(readings, MAIN)
    by = {c["text"]: c for c in rows}
    assert by[shared + EXTRA]["families"] == ["asr:mai"]
    assert by[shared]["families"] == ["asr:moss", "gemini"]


def test_threshold_is_configured_and_short_local_changes_remain_candidates(tmp_path, monkeypatch):
    rules_with(tmp_path, monkeypatch, candidates={"single_source_min_chars": 8})
    text = MAIN + "顺便买面包"
    assert any("asr:mai" in c["families"] for c in doubt_policy.candidates({"asr:mai": text}, MAIN))
    assert any("asr:mai" in c["families"] for c in doubt_policy.candidates({"asr:mai": "今天讨论下雨"}, MAIN))


@pytest.mark.parametrize("source", ["jianying", "asr:mai", "asr:moss", "audio:38m@P0"])
def test_all_text_entries_use_the_existing_neighbour_join(source):
    cues = [{"id": 1, "start_s": 0.0, "end_s": 2.0, "text": MAIN},
            {"id": 2, "start_s": 2.0, "end_s": 4.0, "text": EXTRA}]
    # A single timed utterance is already sliced at the normal attachment boundary.
    lines = {"asr:moss": [{"abs_start_s": 0.0, "end_s": 4.0, "text": MAIN + EXTRA}]}
    att = evidence_attach.own_witness_lines(cues, lines)
    jy = evidence_attach.attach_asr(cues, [{"start_s": 0.0, "end_s": 4.0, "text": MAIN + EXTRA}], source="jianying")
    # An independently timed window duplicates its neighbour. Existing join decides once, not an invented veto.
    lines[source] = [{"abs_start_s": 0.0, "end_s": 4.0, "text": MAIN + EXTRA, "owners": [1]}]
    ev = si._evidence_for_cue(cues[0], 0, cues, jy, att, lines, [])
    assert EXTRA not in ev["readings"].get(source, "")


def test_ordinary_subtitle_path_exposes_single_family_evidence_without_false_corroboration(tmp_path, monkeypatch):
    from src.autoslice.manual_lane.adapters import RemoteResult

    monkeypatch.setenv("MANUAL_LANE_ADJUDICATE_MODE", "C")
    monkeypatch.setenv("MANUAL_LANE_WHOLE_WITNESS", "mai")
    monkeypatch.setenv("MANUAL_LANE_LADDER", "asr_first")

    def script(name, body, remote):
        if name.startswith("s3-asrwin-"):
            label = name.removeprefix("s3-asrwin-")
            remote.files[f"{RD}/witness/win/result-windows-{label}.json"] = b"[]"
            return RemoteResult(0, 'WITNESS_WINDOWS_DONE {}\n', "")
        if name.startswith("s3-listen-"):
            remote.files[f"{RD}/listen/out/all.json"] = b"[]"
            return RemoteResult(0, 'LISTEN_DONE 0 records 0 {}\n', "")
        return RemoteResult(0, "ASR_OK\n" if name == "s3-asr" else "", "")

    class InspectCpa(IdCpa):
        def ask(self, **kw):
            if kw["call_id"] == "review-c":
                return {"rc": 0, "response_text": '{"suspicious": [{"cue_id": 1, "reason": "合成疑点"}]}'}
            if kw["call_id"].startswith("adjudicate-"):
                tail = kw["prompt_text"].split("输入疑点（含候选读法及其支持来源、各来源读法与弹幕线索）：", 1)[1]
                rows = json.JSONDecoder().raw_decode(tail.lstrip())[0]
                d = next(r for r in rows if r["cue_id"] == 1)
                assert not d["maintenance"]["maintained"]
                mai_rows = [c for c in d["candidates"] if "asr:mai" in c["families"]]
                assert len(mai_rows) == 1 and mai_rows[0]["supporters"] == 1
                assert EXTRA in str(d["asr_notes"]) and EXTRA in str(d["speaker_segments"])
            return super().ask(**kw)

    remote = FakeRemote(script)
    for source in ("bcut", "jianying"):
        remote.files[f"{RD}/asr/{source}.srt"] = _srt([(0, 3, MAIN)]).encode()
    remote.files[f"{RD}/witness/out/mai.json"] = json.dumps({"status": "OK", "segments": [
        {"start_ms": 0, "end_ms": 3000, "text": MAIN, "speaker": "main"},
        {"start_ms": 1000, "end_ms": 2000, "text": EXTRA, "speaker": "guest"}]}).encode()
    ctx = make_ctx(tmp_path, remote=remote, cpa=InspectCpa(), bcut_text=_srt([(0, 3, MAIN)]))
    result = si.step3_subtitles(ctx)
    assert result.ok, (result.reason, result.note)
    assert EXTRA not in (ctx.clip_dir / "subs/final.srt").read_text()
