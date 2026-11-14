"""Post-review wide listening through the ordinary runner and focused CPA path."""

from __future__ import annotations

import ast
import hashlib
import json
import subprocess

import pytest
from test_review_doubt_authority import _answer, _focused, _row, _runner

from src.autoslice.manual_lane import doubt_policy, stage_package as sp, step3_evidence as evidence
from src.autoslice.manual_lane.runner import run_clip


def _cue(cid, start, end):
    return {"id": cid, "start_s": start, "end_s": end, "text": f"句子{cid}"}


def _providers(monkeypatch):
    calls = []

    def asr(_ctx, label, windows, providers, *, stats_out):
        calls.append((label, tuple(providers), [w["audio"] for w in windows]))
        stats_out.update(requested=len(windows) * len(providers), new=len(windows) * len(providers))
        return [{"win": w["win"], "provider": p, "status": "OK", "reused": False,
                 "segments": [{"start_ms": 100, "end_ms": 500, "text": f"整窗{p}"}]}
                for w in windows for p in providers]

    def listen(_ctx, label, windows, models, *, prompt, stats):
        calls.append((label, tuple(m["slug"] for m in models), [w["audio"] for w in windows]))
        assert prompt == "listen_wide.md"
        stats.update(requested=len(windows), new=len(windows))
        return [{"win": w["win"], "model": m["name"], "slug": m["slug"], "status": "OK",
                 "lines": [{"start_s": 0.3, "text": f"整窗{m['slug']}"}]}
                for w in windows for m in models]

    monkeypatch.setattr(evidence, "_asr_window_pass", asr)
    monkeypatch.setattr(evidence, "_listen_pass", listen)
    return calls


def test_window_clipping_seams_no_chaining_and_identical_sharing():
    cues = [_cue(1, .2, 1), _cue(2, 2, 4), _cue(3, 5, 6), _cue(4, 9.5, 10.1),
            _cue(5, 10.5, 11.5), _cue(6, 17, 18)]
    windows = evidence.wide_listen_windows(cues, {2, 3, 4, 5}, 20, [(0, 10), (10, 20)])
    assert [(w["audio"], w["cue_ids"]) for w in windows] == [
        ([0.0, 7.0], [2]), ([2.0, 10], [3]), ([5.0, 10], [4]), ([10, 18.0], [5])]
    same = evidence.wide_listen_windows([_cue(1, 0, 1), _cue(2, 1, 2)], {1, 2}, 2, [(0, 2)])
    assert len(same) == 1 and same[0]["cue_ids"] == [1, 2]
    with pytest.raises(ValueError, match="no compilation piece"):
        evidence.wide_listen_windows(cues, {6}, 20, [(0, 10)])


def test_no_hold_has_no_new_listening(tmp_path, monkeypatch):
    calls = _providers(monkeypatch)
    ctx, steps, cpa, _, _ = _runner(tmp_path, monkeypatch, _focused())
    assert run_clip(ctx, steps).stopped_at is None
    assert calls == [] and cpa.calls == ["review"] and "wide_held" not in ctx.state.data["review"]


@pytest.mark.parametrize(("first_remains", "second_remains", "expected"), [
    (False, False, ["review", "review-wide-held-1"]),
    (True, False, ["review", "review-wide-held-1", "review-wide-held-2"]),
    (True, True, ["review", "review-wide-held-1", "review-wide-held-2"]),
])
def test_review_rounds_stop_at_resolution_or_two(tmp_path, monkeypatch, first_remains, second_remains, expected):
    calls = _providers(monkeypatch)
    initial = _focused(affects=True)
    ctx, steps, cpa, _, fmr = _runner(tmp_path, monkeypatch, initial)
    cpa.responses.extend([_focused(affects=first_remains), _focused(affects=second_remains)])
    report = run_clip(ctx, steps)
    assert report.stopped_at is None, (report.reason, (ctx.clip_dir / "error-step9.txt").read_text()
                                               if (ctx.clip_dir / "error-step9.txt").exists() else "")
    assert cpa.calls == expected
    assert [c[1] for c in calls] == ([('mai', 'moss'), ('38m',), ('argon',)]
                                     if first_remains else [('mai', 'moss'), ('38m',)])
    if first_remains:
        assert calls[-1][2] == calls[0][2]
    assert doubt_policy.is_hold(_row(ctx)) is (first_remains and second_remains)
    assert ("attest" in fmr) is not (first_remains and second_remains)
    before = ctx.clip_dir / "listen/wide-held/before"
    assert (before / "subs/final.srt").is_file() and (before / "subs/doubts.json").is_file()
    assert "复审后宽窗汇总" in (ctx.clip_dir / "PROCESS.md").read_text()


def test_readings_are_per_doubt_context_and_do_not_enter_votes(tmp_path, monkeypatch):
    calls = _providers(monkeypatch)
    decisions = [{'id': did, 'confidence': '低', 'affects_meaning': True, 'reason': '未定'}
                 for did in ('d493', 'd982')]
    ctx, steps, cpa, _, _ = _runner(tmp_path, monkeypatch, _answer(decisions))
    path = ctx.clip_dir / "subs/doubts.json"
    rows = json.loads(path.read_text())
    rows[0]["readings"] = {"ordinary": "普通读法"}
    rows.append({"id": "d982", "start_s": 3.0, "end_s": 5.0, "state": "ADOPT", "final_text": "木舟等雨停",
                 "confidence": "低", "affects_meaning": True, "readings": {"ordinary": "另一普通读法"}})
    path.write_text(json.dumps(rows, ensure_ascii=False))
    cpa.responses.extend([_answer([{**d, 'confidence': '高'} for d in decisions])])
    assert run_clip(ctx, steps).stopped_at is None
    prompt = (ctx.clip_dir / "cpa/review-wide-held-1.prompt.txt").read_text()
    material = json.loads(prompt.split("## 加长窗口的新听写\n", 1)[1].split("\n", 1)[1])
    assert {b["id"] for b in material} == {"d493", "d982"}
    assert all({r["channel"] for r in b["readings"]} == {"mai", "moss", "38m"} for b in material)
    assert all("window_s" in b and "target_s" in b for b in material)
    for block in material:
        mai = next(r for r in block["readings"] if r["channel"] == "mai")
        assert mai["segments"][0]["start_s"] == pytest.approx(block["window_s"][0] + 0.1)
    assert len(calls[0][2]) == 2
    after = json.loads(path.read_text())
    assert [r["readings"] for r in after] == [{"ordinary": "普通读法"}, {"ordinary": "另一普通读法"}]
    assert not any("整窗" in json.dumps(r, ensure_ascii=False) for r in after)


def test_interrupted_adoption_reuses_all_requests_and_rebuilds_once(tmp_path, monkeypatch):
    calls = _providers(monkeypatch)
    ctx, steps, cpa, _, _ = _runner(tmp_path, monkeypatch, _focused(affects=True))
    cpa.responses.append(_focused(fixes={"subtitles": [{"cue_id": 1, "text": "纸伞落在南边"}]}))
    original = sp.apply_fixes
    interrupted = False

    def crash_after_wide(c, answer):
        nonlocal interrupted
        result = original(c, answer)
        if answer.get("fixes", {}).get("subtitles") and not interrupted:
            interrupted = True
            raise RuntimeError("synthetic interruption after file writes")
        return result

    monkeypatch.setattr(sp, "apply_fixes", crash_after_wide)
    first = run_clip(ctx, steps)
    assert first.stopped_at == 9 and interrupted
    original_call = next(c for c in ctx.state.data["review"]["cpa"] if c["call_id"] == "review-wide-held-1")
    assert not original_call.get("cached")
    resumed = run_clip(ctx, steps)
    assert resumed.stopped_at is None and len(calls) == 2
    assert cpa.calls == ["review", "review-wide-held-1"]
    assert "纸伞落在南边" in (ctx.clip_dir / "subs/final.srt").read_text()
    assert len(ctx.state.data["review"]["wide_held"]["rounds"]) == 1
    saved_call = next(c for c in ctx.state.data["review"]["cpa"] if c["call_id"] == "review-wide-held-1")
    assert not saved_call.get("cached")
    assert not ctx.state.data["review"]["wide_held"]["rounds"][0]["cpa_cached"]
    assert run_clip(ctx, steps).stopped_at is None and len(calls) == 2 and len(cpa.calls) == 2


def test_rebuild_after_first_round_keeps_the_same_window_for_argon(tmp_path, monkeypatch):
    calls = _providers(monkeypatch)
    ctx, steps, cpa, _, _ = _runner(tmp_path, monkeypatch, _focused(affects=True))
    cpa.responses.extend([_focused(affects=True, fixes={"subtitles": [{"cue_id": 1, "text": "纸伞落在南边"}]}),
                          _focused(affects=False)])
    report = run_clip(ctx, steps)
    assert report.stopped_at is None and not doubt_policy.is_hold(_row(ctx))
    assert cpa.calls == ["review", "review-wide-held-1", "review-wide-held-2"]
    assert calls[0][2] == calls[2][2] and len(ctx.state.data["review"]["wide_held"]["rounds"]) == 2
    assert report.rewinds.count("burn") == 1


def test_failed_window_tool_preserves_failure_and_does_not_ask_cpa(tmp_path, monkeypatch):
    ctx, steps, cpa, _, _ = _runner(tmp_path, monkeypatch, _focused(affects=True))
    monkeypatch.setattr(evidence, "_asr_window_pass", lambda *_args, **_kwargs: None)
    report = run_clip(ctx, steps)
    assert report.stopped_at == 9 and report.reason == "ASR_WINDOW_TOOL_FAILED"
    assert cpa.calls == ["review"] and doubt_policy.is_hold(_row(ctx))


def test_ordinary_step3_ladder_windows_and_cache_keys_equal_base():
    sha = "f2e01effb5a8091a79176e7ddeeeda53b3f1a10e"
    for path in ("src/autoslice/manual_lane/suspicion.py", "src/autoslice/manual_lane/variant_c.py",
                 "src/autoslice/manual_lane/step3_adjudicate.py"):
        original = subprocess.run(["git", "show", f"{sha}:{path}"], check=True, capture_output=True).stdout
        assert original == __import__("pathlib").Path(path).read_bytes()
    source = subprocess.run(["git", "show", f"{sha}:src/autoslice/manual_lane/step3_evidence.py"],
                            check=True, capture_output=True, text=True).stdout
    node = next(n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name == "listen_windows")
    namespace = {"LISTEN_MARGIN_S": 1.5}
    exec("from __future__ import annotations\n" + ast.get_source_segment(source, node), namespace)  # noqa: S102
    cues = [_cue(1, .2, 1.1), _cue(2, 2.2, 4.2), _cue(3, 8, 9.5)]
    old, new = namespace["listen_windows"](cues, 10), evidence.listen_windows(cues, 10)
    assert old == new
    from assets.manual_lane.remote.listen_windows import key_for, prompt_for
    prompt = (evidence.PROMPTS / "listen_blind.md").read_text()
    for a, b in zip(old, new, strict=True):
        for model in evidence.LISTEN_MODELS:
            keys = [key_for(w["audio"], w["tempo"], model["name"], model["slug"], prompt_for(w, prompt))
                    for w in (a, b)]
            assert hashlib.sha256(keys[0].encode()).digest() == hashlib.sha256(keys[1].encode()).digest()
