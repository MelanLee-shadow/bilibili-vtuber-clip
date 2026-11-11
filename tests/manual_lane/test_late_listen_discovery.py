"""A C judge may discover an unreviewed cue and send it through the ordinary evidence ladder."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from fakes import FakeCpa, FakeRemote, make_ctx, srt

from src.autoslice.manual_lane import stage_impl as si
from src.autoslice.manual_lane import parallel
from src.autoslice.manual_lane import variant_c
from src.autoslice.manual_lane.adapters import RemoteResult

RD = "/r/clip"


def _answer(cue_id, *, text="", requests=None, confidence="高", **extra):
    row = {"id": f"d{cue_id:03d}", "state": "ADOPT" if text else "KEEP", "final_text": text,
           "confidence": confidence, "affects_meaning": bool(text), "basis": "合成见证"}
    if requests is not None:
        row["listen_requests"] = requests
    row.update(extra)
    return row


def _scenario(tmp_path, monkeypatch, answers, *, fail_label=None, ladder="gemini", labels=None,
              review_ids=(1,), cue_count=4, statuses=None, texts=None):
    monkeypatch.setenv("MANUAL_LANE_ADJUDICATE_MODE", "C")
    monkeypatch.setenv("MANUAL_LANE_LADDER", ladder)
    responses = [json.dumps({"suspicious": [{"cue_id": cid, "reason": "合成疑点"} for cid in review_ids]},
                            ensure_ascii=False)]
    responses += [json.dumps(rows, ensure_ascii=False) for rows in answers]
    cpa = FakeCpa(responses)
    recorded = []
    calls = []

    def script(name, body, remote):
        if name == "s3-asr":
            return RemoteResult(0, "ASR_OK\n", "")
        if name.startswith("s3-asrwin-"):
            label = name.removeprefix("s3-asrwin-")
            calls.append(label)
            windows = json.loads(remote.files[f"{RD}/witness/win/windows-{label}.json"])
            records = [{"win": win["win"], "provider": provider, "status": "OK", "reused": False,
                        "excerpt_ms": 2000, "segments": [{"start_ms": 100, "end_ms": 900,
                                                          "text": f"识别第{win['cue_ids'][0]}句"}]}
                       for win in windows for provider in ("mai", "moss")]
            remote.files[f"{RD}/witness/win/result-windows-{label}.json"] = json.dumps(records).encode()
            return RemoteResult(0, f'WITNESS_WINDOWS_DONE {{"OK": {len(records)}}}\n', "")
        if name.startswith("s3-listen-"):
            label = name.removeprefix("s3-listen-")
            calls.append(label)
            windows = json.loads(remote.files[f"{RD}/listen/windows-{label}.json"])
            models = json.loads(remote.files[f"{RD}/listen/models-{label}.json"])
            if label == fail_label:
                return RemoteResult(1, "", "synthetic failure")
            for win in windows:
                for model in models:
                    cid = win["cue_ids"][0]
                    recorded.append({"win": win["win"], "model": model["name"], "slug": model["slug"],
                                     "status": (statuses or {}).get(label, "OK"),
                                     "lines": [f"合成听音第{cid}句"]})
            remote.files[f"{RD}/listen/out/all.json"] = json.dumps(recorded, ensure_ascii=False).encode()
            status = (statuses or {}).get(label, "OK")
            counts = json.dumps({status: len(windows) * len(models)})
            return RemoteResult(0, f"LISTEN_DONE 1 records 1 {counts}\n", "")
        return RemoteResult(0, "", "")

    remote = FakeRemote(on_script=script)
    text = (srt(cue_count, step=2.0) if texts is None else
            "\n\n".join(f"{i}\n{si.fmt_ts((i - 1) * 2)} --> {si.fmt_ts(i * 2)}\n{value}"
                        for i, value in enumerate(texts, 1)) + "\n")
    remote.files[f"{RD}/asr/bcut.srt"] = text.encode()
    remote.files[f"{RD}/asr/jianying.srt"] = text.encode()
    ctx = make_ctx(tmp_path, remote=remote, cpa=cpa, bcut_text=text, reviewer_labels=labels)
    return ctx, cpa, calls


def test_discovery_after_original_is_settled_reaches_real_tool_judge_and_writer(tmp_path, monkeypatch):
    request = {"cue_id": 2, "reason": "后文揭示另一句含糊"}
    ctx, cpa, calls = _scenario(tmp_path, monkeypatch, [[_answer(1, requests=[request])],
                                                        [_answer(2, text="合成听音第2句")]])
    out = si.step3_subtitles(ctx)
    assert out.ok, (out.reason, out.note)
    assert calls == ["C-L1", "C-L1-b2"]
    windows = json.loads((ctx.clip_dir / "listen/windows-C-L1-b2.json").read_text())
    assert [w["cue_ids"] for w in windows] == [[2]]
    prompt = cpa.prompts[cpa.calls.index("adjudicate-c-L1-b2-1")]
    assert "合成听音第2句" in prompt and "audio:38m@P0" in prompt
    assert "合成听音第2句" in (ctx.clip_dir / "subs/final.srt").read_text()
    assert any("后文揭示另一句含糊" in line for line in out.process)


def test_late_l2_discovery_starts_at_first_tier_and_deduplicates_across_levels(tmp_path, monkeypatch):
    def request(cid, reason):
        return {"cue_id": cid, "reason": reason}
    labelled = {"labels": [{"current": "句子4", "text": "人工冻结", "abs_start_ms": 16000,
                             "abs_end_ms": 18000}]}
    first_requests = [request(2, "发现第二句"), request(3, "发现第三句"), request(2, "同批重复"),
                      request(1, "已处理"), request(4, "已人工标注"), request(99, "不存在")]
    answers = [[_answer(1, confidence="低")], [_answer(1, requests=first_requests)],
               [_answer(2, text="合成听音第2句", requests=[request(3, "再次提名")]), _answer(3)],
               [_answer(2, text="合成听音第2句", requests=[request(2, "末级重复")])]]
    ctx, cpa, calls = _scenario(tmp_path, monkeypatch, answers, ladder="asr_first", labels=labelled)
    out = si.step3_subtitles(ctx)
    assert out.ok, (out.reason, out.note)
    assert calls == ["C-L1", "C-L2", "C-L1-b2", "C-L2-b2"]
    for label, expected in [("C-L1", [1]), ("C-L2", [1]), ("C-L1-b2", [2, 3]), ("C-L2-b2", [2])]:
        folder = "witness" if "L1" in label else "listen"
        windows = json.loads((ctx.clip_dir / folder / f"windows-{label}.json").read_text())
        assert [w["cue_ids"][0] for w in windows] == expected
        assert all(len(w["cue_ids"]) == 1 for w in windows)
    prompt = cpa.prompts[cpa.calls.index("adjudicate-c-L1-b2-1")]
    assert "【先前裁决工作稿】" in prompt and '"cue_id":1' in prompt
    assert "识别第2句" in prompt and "jianying" in prompt
    text = (ctx.clip_dir / "subs/final.srt").read_text()
    assert "合成听音第2句" in text and "人工冻结" in text and "句子3" in text
    process = "\n".join(out.process)
    assert process.count("采用：加入疑点流程首级") == 2
    assert process.count("忽略：已进入疑点流程") == 4
    assert "忽略：人工标注保护" in process and "忽略：不是本片 BCUT 编号" in process
    assert all(reason in process for reason in ("发现第二句", "发现第三句", "同批重复", "末级重复"))


def test_lookup_response_can_request_a_cue_and_prior_request_is_preserved(tmp_path, monkeypatch):
    def lookup(_ctx, doubts, rows, cache):
        fresh = []
        for doubt in doubts:
            query = rows[doubt["id"]].get("work_query")
            if query and query not in cache:
                cache[query] = {"query": query, "source": "synthetic", "results": [], "layers": []}
                doubt["work_lookup"] = cache[query]
                fresh.append(doubt["id"])
        return fresh

    monkeypatch.setattr(si, "_attach_work_lookups", lookup)
    req2 = {"cue_id": 2, "reason": "首轮提名"}
    req3 = {"cue_id": 3, "reason": "查证后提名"}
    answers = [[_answer(1, requests=[req2], work_query="合成作品")],
               [_answer(1, requests=[req3])], [_answer(2), _answer(3)]]
    ctx, cpa, calls = _scenario(tmp_path, monkeypatch, answers)
    out = si.step3_subtitles(ctx)
    assert out.ok, (out.reason, out.note)
    assert calls == ["C-L1", "C-L1-b2"]
    prompt = cpa.prompts[cpa.calls.index("adjudicate-c-L1-lookup-1")]
    assert '"work_lookup"' in prompt and '"first_pass"' in prompt and "合成作品" in prompt
    process = "\n".join(out.process)
    assert "首轮提名" in process and "查证后提名" in process
    assert len(list((ctx.clip_dir / "cpa").glob("adjudicate-c-L1*.completion.txt"))) == 3


def test_requests_from_separate_adjudication_batches_are_both_recorded(tmp_path, monkeypatch):
    monkeypatch.setattr(parallel, "limit", lambda _provider: 1)
    batch1 = [_answer(cid) for cid in range(1, 11)]
    batch1[0]["listen_requests"] = [{"cue_id": 12, "reason": "首批发现"}]
    batch2 = [_answer(11, requests=[{"cue_id": 12, "reason": "另一批重复"},
                                    {"cue_id": 13, "reason": "另一批发现"}])]
    texts = ["甲乙丙", "丁戊己", "庚辛壬", "癸子丑", "寅卯辰", "巳午未", "申酉戌",
             "亥天地", "玄黄宇", "宙洪荒", "日月盈", "昃辰宿", "列张寒", "来暑往"]
    ctx, cpa, calls = _scenario(tmp_path, monkeypatch, [batch1, batch2, [_answer(12), _answer(13)]],
                                review_ids=range(1, 12), texts=texts)
    out = si.step3_subtitles(ctx)
    assert out.ok, (out.reason, out.note)
    assert calls == ["C-L1", "C-L1-b2"]
    windows = json.loads((ctx.clip_dir / "listen/windows-C-L1-b2.json").read_text())
    assert [w["cue_ids"][0] for w in windows] == [12, 13]
    assert [c for c in cpa.calls if c.startswith("adjudicate")] == ["adjudicate-c-L1-1", "adjudicate-c-L1-2",
                                                                      "adjudicate-c-L1-b2-1"]
    process = "\n".join(out.process)
    assert "首批发现" in process and "另一批重复" in process and "另一批发现" in process
    assert process.count("采用：加入疑点流程首级") == 2 and "忽略：已进入疑点流程" in process


def test_error_and_quota_records_are_not_listening_evidence(tmp_path, monkeypatch):
    req = {"cue_id": 2, "reason": "需要新证据"}
    answers = [[_answer(1, requests=[req])], [_answer(2, text="假定写法")],
               [_answer(2, text="假定写法", confidence="低")]]
    ctx, cpa, calls = _scenario(tmp_path, monkeypatch, answers, statuses={"C-L1-b2": "ERROR", "C-L2": "QUOTA"})
    out = si.step3_subtitles(ctx)
    assert out.ok and calls == ["C-L1", "C-L1-b2", "C-L2"]
    d2 = next(d for d in json.loads((ctx.clip_dir / "subs/doubts.json").read_text()) if d["cue_id"] == 2)
    assert not any(key.startswith("audio:") for key in d2["readings"])
    assert d2["unsettled"] and d2["confidence"] == "低"
    prompt = cpa.prompts[cpa.calls.index("adjudicate-c-L2-1")]
    assert "合成听音第2句" not in prompt
    assert any("错误 1" in line for line in out.process)
    assert any("429/额度 1" in line for line in out.process)


def test_speaker_pass_uses_only_the_active_batch_and_distinct_receipt_labels(tmp_path, monkeypatch):
    seen = []

    def listen(_ctx, label, windows, models, *, prompt):
        seen.append((label, [w["cue_ids"][0] for w in windows], prompt))
        return []

    monkeypatch.setattr(si, "_listen_pass", listen)
    ctx = make_ctx(tmp_path, remote=FakeRemote(), cpa=FakeCpa([]))
    cues = si.parse_srt(srt(2))
    two_base = {w["cue_ids"][0]: w for w in si.listen_windows(cues, 70.0, prefix="S-")}
    det = SimpleNamespace(mixed={1: {}, 2: {}})
    esc = variant_c._Escalation(pending={1, 2})
    variant_c._two_speaker_pass(ctx, si, det, esc, two_base, [si.M38], {1})
    variant_c._two_speaker_pass(ctx, si, det, esc, two_base, [si.M38], {2}, suffix="-b2")
    assert seen == [("C-S", [1], "listen_two_speakers.md"), ("C-S-b2", [2], "listen_two_speakers.md")]


@pytest.mark.parametrize("bad", [None, {}, "2", 2, [None], [{}], [{"cue_id": True, "reason": "x"}],
                                  [{"cue_id": "2", "reason": "x"}], [{"cue_id": 2, "reason": " "}],
                                  [{"cue_id": 2, "reason": 1}],
                                  [{"cue_id": 2, "reason": "x", "tempo": 0.5}]])
def test_present_malformed_requests_use_the_existing_parse_failure(tmp_path, monkeypatch, bad):
    row = _answer(1)
    row["listen_requests"] = bad
    ctx, _cpa, calls = _scenario(tmp_path, monkeypatch, [[row]])
    out = si.step3_subtitles(ctx)
    assert not out.ok and out.reason == "CPA_RESPONSE_UNPARSEABLE"
    assert calls == ["C-L1"] and not (ctx.clip_dir / "subs/final.srt").exists()


def test_late_tool_failure_is_not_treated_as_a_heard_or_written_result(tmp_path, monkeypatch):
    req = {"cue_id": 2, "reason": "需要新证据"}
    ctx, cpa, calls = _scenario(tmp_path, monkeypatch, [[_answer(1, requests=[req])]], fail_label="C-L1-b2")
    out = si.step3_subtitles(ctx)
    assert not out.ok and out.reason == "LISTEN_TOOL_FAILED"
    assert calls == ["C-L1", "C-L1-b2"]
    assert "adjudicate-c-L1-b2-1" not in cpa.calls
    assert not (ctx.clip_dir / "subs/final.srt").exists()
    assert any("需要新证据" in line and "采用" in line for line in out.process)


@pytest.mark.parametrize("requests", [None, []])
def test_no_request_preserves_normal_trace(tmp_path, monkeypatch, requests):
    ctx, cpa, calls = _scenario(tmp_path, monkeypatch, [[_answer(1, requests=requests)]])
    out = si.step3_subtitles(ctx)
    assert out.ok and calls == ["C-L1"]
    assert cpa.calls == ["review-c", "adjudicate-c-L1-1"]
    assert not any("补听请求" in line for line in out.process)
