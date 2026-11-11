"""TASK-9: steps 8-9 after the abstraction repair.  The review is one CPA reading whose fixes are adopted in place;
the only interception is doubt_policy.HOLD_RULE (定不下且影响意思), shared with step 3; cues are named by their final.srt
number; final.srt and speaker.srt are edited together, so an import or the step-4 speakers survive a fix; release
subtitles never overlap; the cover's vision calls are in the step's record.

Most cases go through the ordinary path (run_clip or step 9 on a prepared package) with answers that the code before
TASK-9 would also accept, so the same check fails there (scratchpad t9 guard run) and passes here."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from itertools import pairwise

import pytest
from fakes import UNDERSTANDING_CALLS, FakeCpa, FakeRemote, make_ctx, serial_order
from test_pipeline_fake_e2e import ScriptedCpa, _fake_fmr, _remote

from src.autoslice.manual_lane import doubt_policy, normalize, speaker_v8
from src.autoslice.manual_lane import stage_impl as si
from src.autoslice.manual_lane import stage_package as sp
from src.autoslice.manual_lane.adapters import RemoteResult
from src.autoslice.manual_lane.runner import Step, StepOutcome, run_clip
from src.autoslice.manual_lane.stages import STEPS
from src.autoslice.manual_lane.state import StateError
from src.autoslice.subtitle_validation import validate_srt_text

TITLE = json.dumps({"candidates": [{"title": "【李豆沙】半分钟后她破防了", "on_stage": ["李豆沙"], "punchline_person": "她破防了",
                                    "swap_check": {"holds_with_other_host": False, "reason": "测试"}}],
                    "recommended": 0, "cover_lines": ["破防了", "半分钟"]}, ensure_ascii=False)
BOUNDARY = json.dumps({"verdict": "ACCEPT", "start_cue": 6, "end_cue": 40, "reason": "完整"})
GUEST, HOST = speaker_v8.GUEST_LABEL, speaker_v8.HOST
RD = "/r/clip"
SUBS_REWIND = ["burn", "tags", "package", "review"]     # what a subtitle fix rebuilds (no speaker, no title)
TITLE_REWIND = ["tags", "package", "review"]
PKG_REWIND = ("package", "review")


@pytest.fixture(autouse=True)
def _isolate_review_adoption_from_post_review_listening(monkeypatch):
    """Retain these adoption/recovery controls independently of the wide-listening runner tests."""
    monkeypatch.setattr(sp, "_finish_or_wide", lambda ctx, record, process:
                        sp._finish(ctx, cpa=record["cpa"], process=process))


def _review(fixes=None, holds=None, verdict="FAIL", doubt_decisions=None):
    """An answer both the old and the new step 9 can read (the old one wants verdict/findings, the new fixes/holds)."""
    return json.dumps({"verdict": verdict, "findings": [{"item": "1", "severity": "BLOCK", "evidence": "字幕",
                                                         "text": "错字"}] if verdict == "FAIL" else [],
                       "fixes": dict({"subtitles": [], "title": None, "tags": None}, **(fixes or {})),
                       "holds": holds or [], "doubt_decisions": doubt_decisions or [],
                       "rechoose": [], "review_md": "复审"}, ensure_ascii=False)


def _run(tmp_path, monkeypatch, cpa, *, before_run=None):
    monkeypatch.setattr(sp, "_fmr", _fake_fmr)
    monkeypatch.setattr(sp, "description_text", lambda t, p=None: "简介")
    remote = _remote()
    ctx = make_ctx(tmp_path, remote=remote, cpa=cpa)
    if before_run:
        before_run(ctx)
    return ctx, remote, run_clip(ctx, STEPS)


def _nonadj(cpa):
    return [c for c in serial_order(cpa.calls) if not c.startswith("adjudicate")]


def _cues(ctx, rel):
    """Cues with the `[名字] ` label split off into `speaker` (parsed here, independent of the code under test)."""
    out = []
    for c in si.parse_srt((ctx.clip_dir / rel).read_text(encoding="utf-8")):
        m = re.match(r"^\[([^\]]+)\]\s+(.*)$", c["text"])
        out.append(dict(c, speaker=m.group(1) if m else None, text=m.group(2) if m else c["text"]))
    return out


# ----- problem 2 / 7: one review, its fixes adopted, only the invalidated steps rebuilt ------------------------
def test_a_subtitle_fix_is_adopted_once_and_only_burn_tags_and_package_are_rebuilt(tmp_path, monkeypatch):
    cpa = ScriptedCpa({"boundary": BOUNDARY, "title": [TITLE],
                       "review": [_review({"subtitles": [{"cue_id": 2, "text": "改后的句子"}]})]})
    ctx, remote, report = _run(tmp_path, monkeypatch, cpa)
    assert report.stopped_at is None, (report.stopped_at, report.reason)
    assert report.rewinds == SUBS_REWIND
    assert _nonadj(cpa) == ["boundary", *UNDERSTANDING_CALLS, "title", "title-judge-r0-1", "title-judge-r0-2", "cover-brief", "review"]          # the review and the title are asked once
    names = [n for n, _ in remote.scripts]
    assert names.count("s4-speaker-v8") == 1 and names.count("s5-burn") == 2 and names.count("s7-tags") == 2
    assert _cues(ctx, "subs/final.srt")[1]["text"] == "改后的句子"
    assert _cues(ctx, "speaker/speaker.srt")[1]["text"] == "改后的句子"    # both files from the same cue list
    process = (ctx.clip_dir / "PROCESS.md").read_text(encoding="utf-8")
    assert "FMR seal" in process and "未再次询问 CPA" in process
    assert [c["call_id"] for c in ctx.state.step("review")["cpa"]] == ["review"]   # the call stays on record
    assert "字幕 1 条" in (ctx.clip_dir / "pkg" / "REVIEW.md").read_text(encoding="utf-8")


def test_reviewed_tags_survive_a_subtitle_rewind_and_new_generated_tags_are_kept(tmp_path, monkeypatch):
    cpa = ScriptedCpa({"boundary": BOUNDARY, "title": [TITLE],
                       "review": [_review({"subtitles": [{"cue_id": 2, "text": "改后的句子"}],
                                            "tags": ["李豆沙", "保留"]})]})
    monkeypatch.setattr(sp, "_fmr", _fake_fmr)
    monkeypatch.setattr(sp, "description_text", lambda t, p=None: "简介")
    remote = _remote()
    original = remote.on_script
    calls = []

    def generate(name, body, adapter):
        if name == "s7-tags":
            calls.append(name)
            final = (["李豆沙", "星汐Seki", "保留"] if len(calls) == 1 else
                     ["李豆沙", "星汐Seki", "新内容"])
            adapter.files[f"{RD}/tags/tags.json"] = json.dumps(
                {"status": "OK", "final_tags": final}, ensure_ascii=False).encode()
            return RemoteResult(0, "", "")
        return original(name, body, adapter)

    remote.on_script = generate
    ctx = make_ctx(tmp_path, remote=remote, cpa=cpa)
    report = run_clip(ctx, STEPS)
    assert report.stopped_at is None and report.rewinds == SUBS_REWIND
    tags = json.loads((ctx.clip_dir / "pkg/tags.json").read_text(encoding="utf-8"))
    assert tags["final_tags"] == ["李豆沙", "保留", "新内容"]
    assert tags["final_tag_line"] == "李豆沙,保留,新内容"
    assert _nonadj(cpa).count("review") == 1
    later = sp.apply_fixes(ctx, {"fixes": {"tags": ["李豆沙", "保留", "星汐Seki"]}})
    assert later["tags"] is True
    assert si.step7_tags(ctx).ok and sp.step8_package(ctx).ok
    tags = json.loads((ctx.clip_dir / "pkg/tags.json").read_text(encoding="utf-8"))["final_tags"]
    assert "星汐Seki" in tags and "保留" in tags


def test_review_no_tag_change_keeps_reviewed_tags_during_subtitle_rewind(tmp_path, monkeypatch):
    cpa = ScriptedCpa({"boundary": BOUNDARY, "title": [TITLE],
                       "review": [_review({"subtitles": [{"cue_id": 2, "text": "改后的句子"}], "tags": None})]})
    monkeypatch.setattr(sp, "_fmr", _fake_fmr)
    monkeypatch.setattr(sp, "description_text", lambda t, p=None: "简介")
    remote = _remote()
    original, calls = remote.on_script, []

    def generate(name, body, adapter):
        if name == "s7-tags":
            calls.append(name)
            tags = ["李豆沙", "保留"] if len(calls) == 1 else ["李豆沙", "新内容"]
            adapter.files[f"{RD}/tags/tags.json"] = json.dumps({"status": "OK", "final_tags": tags},
                                                                ensure_ascii=False).encode()
            return RemoteResult(0, "", "")
        return original(name, body, adapter)

    remote.on_script = generate
    ctx = make_ctx(tmp_path, remote=remote, cpa=cpa)
    assert run_clip(ctx, STEPS).stopped_at is None and len(calls) == 2
    final = json.loads((ctx.clip_dir / "pkg/tags.json").read_text(encoding="utf-8"))["final_tags"]
    assert final == ["李豆沙", "保留", "新内容"]


def test_a_title_fix_rebuilds_only_the_tags_and_the_package(tmp_path, monkeypatch):
    cpa = ScriptedCpa({"boundary": BOUNDARY, "title": [TITLE],
                       "review": [_review({"title": "【李豆沙】她半分钟后真的破防了"})]})   # a title the publish gate passes
    ctx, _, report = _run(tmp_path, monkeypatch, cpa)
    assert report.stopped_at is None and report.rewinds == TITLE_REWIND
    assert _nonadj(cpa) == ["boundary", *UNDERSTANDING_CALLS, "title", "title-judge-r0-1", "title-judge-r0-2", "cover-brief", "review",
                            "review-title-judge-1", "review-title-judge-2"]   # the review's title goes to the judge too
    assert (ctx.clip_dir / "title/title.txt").read_text(encoding="utf-8").strip() == "【李豆沙】她半分钟后真的破防了"


def test_a_title_fix_that_breaks_reviewers_rule_is_not_adopted_and_nothing_loops(tmp_path, monkeypatch):
    cpa = ScriptedCpa({"boundary": BOUNDARY, "title": [TITLE],
                       "review": [_review({"title": "【李豆沙】这段直接把人笑哭"})]})
    ctx, _, report = _run(tmp_path, monkeypatch, cpa)
    assert report.stopped_at is None and report.rewinds == []
    assert "直接" not in (ctx.clip_dir / "title/title.txt").read_text(encoding="utf-8")
    assert "未采用：title breaks" in (ctx.clip_dir / "PROCESS.md").read_text(encoding="utf-8")


def test_the_old_revision_counter_does_not_stop_a_clip(tmp_path, monkeypatch):
    # 692 (TASK-8): a revisions=2 left from earlier rounds made step 9 stop before applying anything
    cpa = ScriptedCpa({"boundary": BOUNDARY, "title": [TITLE],
                       "review": [_review({"subtitles": [{"cue_id": 3, "text": "又一次改"}]})]})
    ctx, _, report = _run(tmp_path, monkeypatch, cpa, before_run=lambda c: c.state.data.update(revisions=2))
    assert report.stopped_at is None, report.reason
    assert _cues(ctx, "subs/final.srt")[2]["text"] == "又一次改" and "revisions" not in ctx.state.data


# ----- problem 3: the fix reaches the subtitles (no join rule removes its words) ------------------------------
def _package(tmp_path, monkeypatch, review_answer, *, final, doubts, speaker=None, session=None):
    """A clip at step 9 (package and FMR directory in place) with the given subtitles, doubts and review answer."""
    cpa = FakeCpa([review_answer])
    ctx = make_ctx(tmp_path, remote=FakeRemote(on_script=lambda n, b, r: RemoteResult(0, "", "")), cpa=cpa,
                   session=session)
    c = ctx.clip_dir
    for rel, body in (("subs/final.srt", final), ("subs/doubts.json", json.dumps(doubts, ensure_ascii=False)),
                      ("title/title.txt", "【李豆沙】标题\n"), ("tags/tags.json", json.dumps({"final_tags": ["李豆沙"]})),
                      ("pkg/final.srt", final), ("pkg/title.txt", "【李豆沙】标题\n"), ("pkg/description.txt", "简介\n"),
                      ("pkg/tags.json", json.dumps({"final_tags": ["李豆沙"]})), ("asr/bcut.srt", final)):
        (c / rel).parent.mkdir(parents=True, exist_ok=True)
        (c / rel).write_text(body, encoding="utf-8")
    if speaker is not None:
        (c / "speaker").mkdir(parents=True, exist_ok=True)
        (c / "speaker/speaker.srt").write_text(speaker, encoding="utf-8")
    sp.package_dir(ctx).mkdir(parents=True, exist_ok=True)
    (sp.package_dir(ctx) / "findings.md").write_text("F", encoding="utf-8")
    calls = []
    monkeypatch.setattr(sp, "_fmr", lambda *a: (calls.append(a[0]), subprocess.CompletedProcess(a, 0, "", ""))[1])
    return ctx, cpa, calls


def _srt(rows):
    return "\n".join(f"{i}\n{si.fmt_ts(a)} --> {si.fmt_ts(b)}\n{t}\n" for i, (a, b, t) in enumerate(rows, 1))


def test_focused_review_without_tag_field_does_not_claim_tag_approval(tmp_path, monkeypatch):
    ctx, _, _ = _package(tmp_path, monkeypatch, _review(), final=_srt([(0, 2, "你好")]), doubts=[])
    sp.apply_fixes(ctx, {"fixes": {"subtitles": []}})
    assert "reviewed_tags" not in ctx.state.data


class _CompleteCpa(FakeCpa):
    """Synthetic terminal receipt in the same complete/rc shape as the real provider."""

    def ask(self, **kwargs):
        answer = super().ask(**kwargs)
        answer["complete"] = answer["rc"] == 0
        return answer


class _NoSecondCpa:
    def __init__(self):
        self.calls = []

    def ask(self, **kwargs):
        self.calls.append(kwargs["call_id"])
        raise AssertionError("saved review must never call a provider again")


def _ordinary_saved_review(tmp_path, monkeypatch):
    """Create the old unsuccessful adoption through ordinary runner/step9, then remove only its derived report."""
    target = "周六周一纸鸢在窗边"
    stale = "周6周一纸鸢在窗边"
    answer = _review({"subtitles": [{"cue_id": 1, "text": target}]}, verdict="PASS")
    final = _srt([(0, 2, stale), (3, 5, "海风停下了")])
    speaker = _srt([(0, 2, f"[{HOST}] {stale}"), (3, 5, f"[{HOST}] 海风停下了")])
    doubts = [{"id": "k001", "start_s": 0.0, "end_s": 2.0, "state": "KEEP", "final_text": stale,
               "confidence": "高", "affects_meaning": False, "basis": "合成原句"},
              {"id": "d999", "start_s": 3.0, "end_s": 5.0, "state": "ADOPT", "final_text": "海风停下了",
               "confidence": "低", "affects_meaning": True, "basis": "合成待定"}]
    ctx, _, fmr_calls = _package(tmp_path, monkeypatch, answer, final=final, doubts=doubts, speaker=speaker)
    cpa = _CompleteCpa([answer])
    ctx.adapters.cpa = cpa

    def stage(name):
        def run(c):
            if name == "package":
                shutil.copyfile(c.clip_dir / "subs/final.srt", c.clip_dir / "pkg/final.srt")
            p = c.clip_dir / f"{name}.synthetic-product"
            p.write_text(name, encoding="utf-8")
            return StepOutcome(True, products={name: str(p)})
        return run

    steps = [Step(5, "burn", "burn", stage("burn")), Step(7, "tags", "tags", stage("tags")),
             Step(8, "package", "package", stage("package")), Step(9, "review", "review", sp.step9_review)]
    ordinary_numerals = normalize.numerals

    def old_writer(text, *, yuri=False):
        if text == target:
            return stale, []
        return ordinary_numerals(text, yuri=yuri)

    with monkeypatch.context() as m:
        m.setattr(normalize, "numerals", old_writer)
        first = run_clip(ctx, steps)
    assert first.stopped_at is None and first.rewinds == SUBS_REWIND
    assert cpa.calls == ["review"] and not fmr_calls
    assert _cues(ctx, "subs/final.srt")[0]["text"] == stale
    original = dict(ctx.state.data["review"]["cpa"][0])
    cache = sp.Path(original["cache_path"])
    payload = json.loads(cache.read_text(encoding="utf-8"))
    assert payload["complete"] is True and "call_id" not in payload and "http_log" not in payload
    response = sp.Path(original["file"] + ".completion.txt")
    prompt = sp.Path(original["file"] + ".prompt.txt")
    kept = {p: p.read_bytes() for p in (cache, response, prompt)}
    (ctx.clip_dir / "pkg/REVIEW.md").rename(tmp_path / "saved-review-preimage.md")
    (ctx.clip_dir / "pkg/description.txt").write_text("合成材料更新，不能触发新请求", encoding="utf-8")
    never = _NoSecondCpa()
    ctx.adapters.cpa = never
    return ctx, steps, never, cache, kept, original, target, stale, fmr_calls


def test_missing_derived_review_replays_saved_terminal_through_ordinary_runner(tmp_path, monkeypatch):
    ctx, steps, never, cache, kept, original, target, stale, fmr_calls = _ordinary_saved_review(tmp_path, monkeypatch)
    clip_before = dict(ctx.state.data["clip"])
    result = run_clip(ctx, steps)
    assert result.stopped_at is None, (result.reason, ctx.state.step("review"))
    assert result.rewinds == SUBS_REWIND and never.calls == []
    assert _cues(ctx, "subs/final.srt")[0]["text"] == target != stale
    assert _cues(ctx, "speaker/speaker.srt")[0]["text"] == target
    row = json.loads((ctx.clip_dir / "subs/doubts.json").read_text(encoding="utf-8"))[0]
    assert row["state"] == "ADOPT" and row["final_text"] == target
    assert ctx.state.data["clip"] == clip_before and not fmr_calls
    replay = ctx.state.step("review")["cpa"][0]
    assert replay["cached"] is True
    assert all(replay[k] == original[k] for k in ("call_id", "file", "cache_path", "purpose"))
    assert "未再次请求；新补材料未进入原复审" in (ctx.clip_dir / "PROCESS.md").read_text(encoding="utf-8")
    assert all(p.read_bytes() == data for p, data in kept.items())
    assert cache.exists() and "未封存" in ctx.state.step("review")["note"]


def test_missing_other_derived_review_product_reuses_saved_terminal(tmp_path, monkeypatch):
    answer = _review(verdict="PASS")
    ctx, _, _ = _package(tmp_path, monkeypatch, answer, final=_srt([(0, 2, "纸船靠岸")]), doubts=[])
    cpa = _CompleteCpa([answer])
    ctx.adapters.cpa = cpa

    def fmr(*args):
        if args[0] == "seal":
            (sp.package_dir(ctx) / "review_manifest.json").write_text("{}", encoding="utf-8")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(sp, "_fmr", fmr)
    steps = [Step(9, "review", "review", sp.step9_review)]
    assert run_clip(ctx, steps).stopped_at is None and cpa.calls == ["review"]
    manifest = sp.Path(ctx.state.step("review")["products"]["review_manifest"])
    manifest.rename(tmp_path / "saved-manifest-preimage.json")
    assert (ctx.clip_dir / "pkg/REVIEW.md").is_file()
    (ctx.clip_dir / "pkg/description.txt").write_text("另一份合成材料", encoding="utf-8")
    never = _NoSecondCpa()
    ctx.adapters.cpa = never
    result = run_clip(ctx, steps)
    assert result.stopped_at is None and never.calls == []
    assert manifest.is_file() and ctx.state.step("review")["cpa"][0]["cached"] is True


@pytest.mark.parametrize("broken", ["missing", "invalid_json", "not_object", "missing_complete", "incomplete",
                                    "missing_rc", "false_rc", "failed_rc", "missing_body", "truncated_body"])
def test_missing_or_bad_saved_review_stops_without_provider_or_cache_removal(tmp_path, monkeypatch, broken):
    ctx, steps, never, cache, _, _, _, stale, _ = _ordinary_saved_review(tmp_path, monkeypatch)
    payload = json.loads(cache.read_text(encoding="utf-8"))
    if broken == "missing":
        cache.unlink()
    elif broken == "invalid_json":
        cache.write_text("{", encoding="utf-8")
    elif broken == "not_object":
        cache.write_text("[]", encoding="utf-8")
    else:
        if broken == "missing_complete":
            payload.pop("complete")
        elif broken == "incomplete":
            payload["complete"] = False
        elif broken == "missing_rc":
            payload.pop("rc")
        elif broken == "false_rc":
            payload["rc"] = False
        elif broken == "failed_rc":
            payload["rc"] = 1
        elif broken == "missing_body":
            payload.pop("response_text")
        elif broken == "truncated_body":
            payload["response_text"] = '{"fixes":'
        cache.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    raw = cache.read_bytes() if cache.exists() else None
    result = run_clip(ctx, steps)
    assert result.stopped_at == 9 and result.reason in {"CPA_CALL_FAILED", "CPA_RESPONSE_UNPARSEABLE"}
    assert never.calls == [] and (cache.read_bytes() if cache.exists() else None) == raw
    assert _cues(ctx, "subs/final.srt")[0]["text"] == stale


def test_a_fix_that_restores_a_restart_reaches_the_subtitles(tmp_path, monkeypatch):
    # 15_229 (TASK-8): the fix 「我们最近由于…」 after 「我们最最…」 was cut back to 「近由于…」 by the join rule
    final = _srt([(20.93, 21.89, "我们最最…"), (21.89, 27.31, "近由于这个小圆剧场版回天的出现")])
    ctx, _, _ = _package(tmp_path, monkeypatch,
                         _review({"subtitles": [{"cue_id": 2, "text": "我们最近由于这个小圆剧场版回天的出现"}]}),
                         final=final, doubts=[])
    sp.step9_review(ctx)
    assert "".join(c["text"] for c in _cues(ctx, "subs/final.srt")[1:]).replace("\n", "").startswith("我们最近由于")


# ----- problem 4: one numbering (final.srt) for the review, its fixes and the package ---------------------------
def test_a_doubt_is_named_by_its_final_number_not_its_bcut_id():
    bcut = [{"id": 1, "start_s": 0.0, "end_s": 2.0, "text": "甲"}, {"id": 2, "start_s": 2.0, "end_s": 3.0, "text": "哈哈"},
            {"id": 3, "start_s": 3.0, "end_s": 5.0, "text": "宫婉受弃走后疯狂追源"}]
    final = [{"start_s": 0.0, "end_s": 2.0, "text": "甲"}, {"start_s": 3.0, "end_s": 5.0, "text": "宫婉受弃走后疯狂追源"}]
    doubts = [{"id": "d001", "cue_id": 1}, {"id": "d003", "cue_id": 3}]   # the laughter cue 2 is not subtitled
    from src.autoslice.manual_lane import subtitle_track

    assert subtitle_track.doubt_finals(doubts, final, bcut, 10_000) == {"d001": [1], "d003": [2]}
    # an imported interjection inside a longer sentence (111_330 imp-B86x): after sequencing, its own line is its own
    rows = [{"id": "a", "start_s": 0.0, "end_s": 4.0}, {"id": "b", "start_s": 2.0, "end_s": 2.6}]
    seq = [{"start_s": 0.0, "end_s": 2.0, "text": "主句"}, {"start_s": 2.0, "end_s": 2.6, "text": "插话"}]
    assert subtitle_track.doubt_finals(rows, seq, [], 0) == {"a": [1], "b": [2]}


def test_the_review_sees_doubts_in_final_numbering_and_the_package_uses_it(tmp_path, monkeypatch):
    final = _srt([(0.0, 2.0, "甲"), (3.0, 5.0, "受在上的那种")])
    bcut = _srt([(0.0, 2.0, "甲"), (2.0, 3.0, "哈哈"), (3.0, 5.0, "受在上的那种")])
    doubts = [{"id": "d003", "cue_id": 3, "state": "FIT_CONTEXT", "final_text": "受在上的那种", "confidence": "高",
               "affects_meaning": True, "source": "CPA", "basis": "拟合"}]
    ctx, cpa, _ = _package(tmp_path, monkeypatch, _review(verdict="PASS"), final=final, doubts=doubts)
    (ctx.clip_dir / "asr/bcut.srt").write_text(bcut, encoding="utf-8")
    sp.step9_review(ctx)
    assert '"final_cues":[2]' in re.sub(r"\s+", "", cpa.prompts[0])
    member = sp._member(ctx, files={}, cover_lines=["甲"], dedup={}, cut_local=ctx.clip_dir / "x", duration_ms=1,
                        selection_hook=ctx.state.data["clip"]["hook"])
    assert member["doubts"][0]["cue_index"] == 2


# ----- problem 1: one definition of 定不下且影响意思, shared by step 3, the package and the review -------------------
def test_a_held_doubt_is_shown_as_held_and_the_package_is_not_sealed(tmp_path, monkeypatch):
    doubts = [{"id": "d001", "cue_id": 1, "state": "FIT_CONTEXT", "final_text": "周次那种", "confidence": "低",
               "affects_meaning": True, "source": "CPA", "basis": "简称未查到"}]
    ctx, cpa, calls = _package(tmp_path, monkeypatch, _review(verdict="PASS"), final=_srt([(0, 2, "周次那种")]),
                               doubts=doubts)
    out = sp.step9_review(ctx)
    assert "seal" not in calls and "attest" not in calls
    assert out.ok and "未封存" in out.note
    assert '"held": true' in cpa.prompts[0] and "周次那种" in cpa.prompts[0]
    assert "doubt_decisions" in cpa.prompts[0]
    assert doubt_policy.HOLD_RULE in cpa.prompts[0]
    assert sp.grade_of(doubts[0]) == "D"


def test_a_held_sentence_the_review_writes_a_fix_for_is_decided_and_the_doubt_table_follows(tmp_path, monkeypatch):
    doubts = [{"id": "d001", "cue_id": 1, "state": "FIT_CONTEXT", "final_text": "周四那种", "confidence": "低",
               "affects_meaning": True, "source": "CPA", "basis": "简称未查到"}]
    decision = {"id": "d001", "confidence": "高", "affects_meaning": True, "reason": "合成语境已确认"}
    ctx, _, _ = _package(tmp_path, monkeypatch, _review({"subtitles": [{"cue_id": 1, "text": "周次那种"}]},
                                                        doubt_decisions=[decision]),
                         final=_srt([(0, 2, "周四那种")]), doubts=doubts)
    out = sp.step9_review(ctx)
    row = json.loads((ctx.clip_dir / "subs/doubts.json").read_text(encoding="utf-8"))[0]
    assert row["final_text"] == "周次那种" and not doubt_policy.is_hold(row) and out.rewind == tuple(SUBS_REWIND)


def test_a_low_confidence_doubt_that_does_not_change_the_meaning_is_adopted_and_sealed(tmp_path, monkeypatch):
    doubts = [{"id": "d001", "cue_id": 1, "state": "FIT_CONTEXT", "final_text": "嗯", "confidence": "低",
               "affects_meaning": False, "source": "CPA", "basis": "语气词"}]
    ctx, _, calls = _package(tmp_path, monkeypatch, _review(verdict="PASS"), final=_srt([(0, 2, "嗯")]), doubts=doubts)
    sp.step9_review(ctx)
    assert calls == ["attest", "seal"]


def test_a_review_hold_uses_the_same_rule_and_keeps_the_package_unsealed(tmp_path, monkeypatch):
    ctx, _, calls = _package(tmp_path, monkeypatch,
                             _review(holds=[{"cue_id": 1, "reason": "两种读法意思相反"}], verdict="FAIL"),
                             final=_srt([(0, 2, "甲"), (2, 4, "乙")]), doubts=[])
    out = sp.step9_review(ctx)
    held = doubt_policy.holds(json.loads((ctx.clip_dir / "subs/doubts.json").read_text(encoding="utf-8")))
    assert [h["final_text"] for h in held] == ["甲"] and out.rewind == PKG_REWIND
    assert "attest" not in calls


def test_the_hold_rule_treats_an_unknown_meaning_effect_as_affecting_and_a_decided_row_as_free():
    assert doubt_policy.is_hold({"confidence": "低", "affects_meaning": None})
    assert doubt_policy.is_hold({"state": "UNRESOLVED", "affects_meaning": True})
    assert not doubt_policy.is_hold({"confidence": "低", "affects_meaning": False})
    assert not doubt_policy.is_hold({"confidence": "高", "affects_meaning": True})


# ----- problem 6: a linked clip's review sees and fixes the speakers -------------------------------------------
LINKED = {"recording_date": "2026-10-24", "speaker_mode": "uniform_host", "enablement_evidence": None,
          "participants": [["李豆沙"], ["雾深Girimi"]], "own_uploader_mids": [1]}


def test_a_linked_review_is_told_the_speakers_and_its_speaker_fix_is_applied(tmp_path, monkeypatch):
    final = _srt([(0, 2, "我是雾深我想举手"), (2, 4, "我也要用气泡音叫你宝贝吗")])
    speaker = _srt([(0, 2, f"[{HOST}] 我是雾深我想举手"), (2, 4, f"[{HOST}] 我也要用气泡音叫你宝贝吗")])
    fix = {"cue_id": 1, "speaker": GUEST, "evidence": "自称", "quote": "我是雾深"}
    ctx, cpa, _ = _package(tmp_path, monkeypatch, _review({"speakers": [fix]}),
                           final=final, doubts=[], speaker=speaker, session=LINKED)
    out = sp.step9_review(ctx)
    assert "uniform_host" not in cpa.prompts[0] and f"[{GUEST}]" in cpa.prompts[0] and f"[{HOST}] 我是雾深" in cpa.prompts[0]
    assert [c["speaker"] for c in _cues(ctx, "speaker/speaker.srt")] == [GUEST, HOST]
    assert out.rewind == tuple(SUBS_REWIND)


def test_a_review_speaker_fix_without_content_evidence_leaves_the_acoustic_label(tmp_path, monkeypatch):
    # TASK-18: the review relabelled on who asks / how someone is addressed; the step-4 label is the default
    final = _srt([(0, 2, "这就是李豆沙"), (2, 4, "快点让小李说话")])
    speaker = _srt([(0, 2, f"[{HOST}] 这就是李豆沙"), (2, 4, f"[{HOST}] 快点让小李说话")])
    fixes = [{"cue_id": 1, "speaker": GUEST, "reason": "用第三人称评论李豆沙"},
             {"cue_id": 2, "speaker": GUEST, "evidence": "只能出自一方", "quote": "快点让小李说话"}]
    ctx, cpa, _ = _package(tmp_path, monkeypatch, _review({"speakers": fixes}),
                           final=final, doubts=[], speaker=speaker, session=LINKED)
    sp.step9_review(ctx)
    assert "谁在提问谁在回答" in cpa.prompts[0] and "不能据此改" in cpa.prompts[0] and "念题" not in cpa.prompts[0]
    assert [c["speaker"] for c in _cues(ctx, "speaker/speaker.srt")] == [HOST, HOST]


def test_the_review_is_given_the_same_pronoun_rule_as_the_adjudication_and_old_findings_are_kept(tmp_path, monkeypatch):
    # TASK-9: four adopted fixes flipped 她 to 他 under a review prompt that did not carry SUB-23
    ctx, cpa, _ = _package(tmp_path, monkeypatch, _review(verdict="FAIL"), final=_srt([(0, 2, "甲")]), doubts=[])
    sp.step9_review(ctx)
    adjudicate = si._render("subtitle_adjudicate.md", doubts="[]", context="", participants="", confusions="")
    assert si.PRONOUN_RULE in cpa.prompts[0] and si.PRONOUN_RULE in adjudicate
    assert "（旧格式 BLOCK）错字" in (ctx.clip_dir / "pkg" / "REVIEW.md").read_text(encoding="utf-8")


def test_the_guest_label_is_the_one_the_lane_checks():
    from src.autoslice.branding_intro import CHANNEL_PROFILE

    assert (CHANNEL_PROFILE.host_speaker_label, CHANNEL_PROFILE.guest_speaker_label) == (HOST, GUEST)


# ----- problem 5: an imported push is a formal input and survives a review fix ----------------------------------
def _push(tmp_path, clip):
    src = tmp_path / "push"
    rows = [(0.0, 1.14, "雾深Girimi", "我想举手"), (1.14, 3.24, "李豆沙", "我想看李豆沙演霸总"),
            (2.80, 3.20, "雾深Girimi", "嗯"),            # an interjection overlapping the sentence before (r2 style)
            (4.22, 6.62, "李豆沙", "我也要用气泡音叫你宝贝吗")]
    (src / "subs").mkdir(parents=True)
    (src / "speaker").mkdir(parents=True)
    (src / "boundary").mkdir(parents=True)
    (src / "subs/final.srt").write_text(_srt([(a, b, t) for a, b, _, t in rows]), encoding="utf-8")
    (src / "speaker/speaker.srt").write_text(_srt([(a, b, f"[{s}] {t}") for a, b, s, t in rows]), encoding="utf-8")
    head = "no\tid\tstart\tend\tspeaker\ttext\ttext_conf\tspeaker_conf\talt\tbasis\tbcut_text\n"
    body = "".join(f"{i}\tB{i}\t{a}\t{b}\t{s}\t{t}\t{'低' if i == 2 else '高'}\t高\t\t依据\t{t}\n"
                   for i, (a, b, s, t) in enumerate(rows, 1))
    (src / "subs/review.tsv").write_text(head + body, encoding="utf-8")
    (src / "boundary/decision.json").write_text(json.dumps({"adopted": {"start_ms": clip["start_ms"],
                                                                        "end_ms": clip["end_ms"]}}), encoding="utf-8")
    return src


def test_an_imported_push_runs_through_the_ordinary_steps_and_survives_a_fix(tmp_path, monkeypatch):
    meaning = json.dumps([{"id": "imp-B2", "affects_meaning": True, "reason": "两种读法对象不同"}], ensure_ascii=False)
    cpa = ScriptedCpa({"boundary": BOUNDARY, "title": [TITLE], "import": [meaning],
                       "review": [_review({"subtitles": [{"cue_id": 1, "text": "我想，举手"}]})]})

    def imp(ctx):
        si.record_import(ctx.state, ctx.clip_dir, _push(tmp_path, ctx.state.data["clip"]), source="push-test",
                         authority="U130")
    ctx, remote, report = _run(tmp_path, monkeypatch, cpa, before_run=imp)
    assert report.stopped_at is None, (report.stopped_at, report.reason)
    names = [n for n, _ in remote.scripts]
    assert not any(n.startswith(("s3-listen", "s4-speaker")) for n in names)      # nothing heard, no v8
    final = _cues(ctx, "subs/final.srt")
    speakers = _cues(ctx, "speaker/speaker.srt")
    assert [c["text"] for c in final] == ["我想，举手", "我想看李豆沙演霸总", "嗯", "我也要用气泡音叫你宝贝吗"]
    assert [c["speaker"] for c in speakers] == [GUEST, HOST, GUEST, HOST]           # imported, canonical label
    assert "SRT_CUE_OVERLAP" not in json.dumps(validate_srt_text((ctx.clip_dir / "subs/final.srt").read_text()))
    doubts = json.loads((ctx.clip_dir / "subs/doubts.json").read_text(encoding="utf-8"))
    assert [d["id"] for d in doubt_policy.holds(doubts)] == ["imp-B2"]               # low, and CPA: changes the meaning
    assert _nonadj(cpa).count("import-meaning") == 1                                # asked once, only for the low row
    assert "未封存" in ctx.state.step("review")["note"]


def test_an_imported_low_row_that_does_not_change_the_meaning_is_not_held(tmp_path):
    answer = json.dumps([{"id": "imp-B2", "affects_meaning": False, "reason": "只差语气词"}], ensure_ascii=False)
    ctx = make_ctx(tmp_path, remote=None, cpa=FakeCpa([answer]))
    si.record_import(ctx.state, ctx.clip_dir, _push(tmp_path, ctx.state.data["clip"]), source="push-test",
                     authority="U130")
    out = si.step3_subtitles(ctx)
    assert out.ok and [c["call_id"] for c in out.cpa] == ["import-meaning"]
    assert '"id": "imp-B2"' in ctx.adapters.cpa.prompts[0] and '"id": "imp-B1"' not in ctx.adapters.cpa.prompts[0]
    doubts = json.loads((ctx.clip_dir / "subs/doubts.json").read_text(encoding="utf-8"))
    assert doubt_policy.holds(doubts) == []


def test_an_import_made_on_another_cut_is_refused(tmp_path):
    ctx = make_ctx(tmp_path, remote=None, cpa=None)
    src = _push(tmp_path, {"start_ms": 1, "end_ms": 2})
    with pytest.raises(StateError, match="another cut"):
        si.record_import(ctx.state, ctx.clip_dir, src, source="push-test", authority="U130")


# ----- problem 9: release subtitles never overlap -----------------------------------------------------------
def test_two_people_at_once_become_two_consecutive_lines_with_their_words():
    cues = [{"start_s": 50.50, "end_s": 52.20, "text": "好看，好看", "speaker": HOST},
            {"start_s": 51.12, "end_s": 55.20, "text": "我的弹幕说感觉像穷鬼", "speaker": HOST},
            {"start_s": 69.08, "end_s": 69.72, "text": "雾深Girimi来演", "speaker": HOST},
            {"start_s": 69.48, "end_s": 70.56, "text": "要我演吗", "speaker": GUEST}]
    out = normalize.release_shape(cues)[0]
    assert [c["text"] for c in out] == [c["text"] for c in cues] and [c["speaker"] for c in out] == [HOST, HOST, HOST, GUEST]
    assert all(a["end_s"] <= b["start_s"] for a, b in pairwise(out))
    assert out[1]["start_s"] == 51.12                        # the later line keeps its own start when it can
    srt_text = si.format_srt(out)
    codes = {e["code"] for e in validate_srt_text(srt_text)["errors"]}
    assert not codes & {"SRT_CUE_OVERLAP", "SRT_CUE_TOO_SHORT", "SRT_START_ORDER_INVALID"}


def test_a_short_reply_between_two_touching_lines_borrows_time_from_its_neighbour():
    # 111_330 r2: 「对」 02.82-03.03 (0.21 s) with the next line starting at 03.03
    out = normalize.release_shape([{"start_s": 61.64, "end_s": 62.80, "text": "这样子啊"},
                                   {"start_s": 62.82, "end_s": 63.03, "text": "对"},
                                   {"start_s": 63.03, "end_s": 64.15, "text": "那有1吗"}])[0]
    assert all(c["end_s"] - c["start_s"] >= 0.3 - 1e-9 for c in out)
    assert all(a["end_s"] <= b["start_s"] + 1e-9 for a, b in pairwise(out))
    assert out[1]["start_s"] == 62.82 and out[0] == {"start_s": 61.64, "end_s": 62.80, "text": "这样子啊"}


def test_no_line_is_pushed_past_the_last_end_the_producer_wrote():
    # the media end is not known to the shaping: a short last line takes its time from before, not after the end
    out = normalize.release_shape([{"start_s": 0.0, "end_s": 9.9, "text": "最后一句前面"},
                                   {"start_s": 9.9, "end_s": 10.0, "text": "嗯"}])[0]
    assert out[-1]["end_s"] == 10.0 and out[-1]["end_s"] - out[-1]["start_s"] >= 0.3 - 1e-9
    out = normalize.release_shape([{"start_s": 8.0, "end_s": 10.0, "text": "主句"},
                                   {"start_s": 9.95, "end_s": 10.0, "text": "插话"}])[0]
    assert max(c["end_s"] for c in out) == 10.0 and out[0]["end_s"] == out[1]["start_s"]


def test_a_near_simultaneous_reply_shares_the_time_by_text_length():
    out = normalize.release_shape([{"start_s": 16.31, "end_s": 17.95, "text": "你能想象这个"},
                                   {"start_s": 16.32, "end_s": 17.95, "text": "no，no"}])[0]
    assert out[0]["end_s"] == out[1]["start_s"] and out[0]["end_s"] - out[0]["start_s"] >= 0.3
    assert out[1]["end_s"] - out[1]["start_s"] >= 0.3


# ----- problem 8: what a step keeps about its rebuild is dropped when something else runs ----------------------
def test_a_rewind_record_is_dropped_when_a_step_outside_the_rebuild_runs(tmp_path):
    seen = []

    def make(key, rewind=()):
        def run(ctx):
            seen.append((key, dict(ctx.state.data.get("rewind") or {})))
            out = StepOutcome(True, rewind=rewind if not any(k == key for k, _ in seen[:-1]) else ())
            return out
        return Step(len(seen), key, key, run)
    ctx = make_ctx(tmp_path, remote=None, cpa=None)
    steps = (Step(1, "a", "a", make("a").run), Step(2, "b", "b", make("b").run),
             Step(3, "c", "c", make("c", rewind=("b", "c")).run))
    run_clip(ctx, steps)
    assert seen[-1] == ("c", {"by": "c", "steps": ["b", "c"]})       # the asking step sees its own rebuild
    assert "rewind" not in ctx.state.data
    ctx.state.data["rewind"] = {"by": "c", "steps": ["b", "c"]}
    ctx.state.data["steps"].clear()
    seen.clear()
    run_clip(ctx, steps)
    assert seen[0] == ("a", {})                       # step a is outside the rebuild: the record is dropped


# ----- problem 3 (step 3 side): a repeat at a join is a doubt for CPA, never a silent deletion -------------------
def test_a_join_repeat_is_sent_to_the_adjudicator_and_its_words_stay(tmp_path, monkeypatch):
    from test_evidence_attach import ReviewCpa
    from test_evidence_attach import _srt as srt_rows

    monkeypatch.setenv("MANUAL_LANE_ADJUDICATE_MODE", "C")
    rows = [(0.0, 2.0, "甲乙丙丁戊"), (2.1, 4.0, "丙丁戊己庚"), (8.0, 10.0, "子丑寅卯")]

    def script(name, body, remote):
        if name == "s3-asr":
            return RemoteResult(0, "ASR_OK\n", "")
        if name.startswith("s3-listen-"):
            remote.files[f"{RD}/listen/out/all.json"] = b"[]"
            return RemoteResult(0, 'LISTEN_DONE 1 records 1 {"OK": 1}\n', "")
        return RemoteResult(0, "", "")
    remote = FakeRemote(on_script=script)
    remote.files[f"{RD}/asr/bcut.srt"] = srt_rows(rows).encode()
    remote.files[f"{RD}/asr/jianying.srt"] = srt_rows(rows).encode()
    ctx = make_ctx(tmp_path, remote=remote, cpa=ReviewCpa(), bcut_text=srt_rows(rows))
    assert si.step3_subtitles(ctx).ok
    doubts = {d["cue_id"]: d for d in json.loads((ctx.clip_dir / "subs/doubts.json").read_text(encoding="utf-8"))}
    assert any(r.startswith("相接处重复") for r in doubts[2]["reasons"])
    assert "丙丁戊己庚" in (ctx.clip_dir / "subs/final.srt").read_text(encoding="utf-8")


# ----- problem 10: the cover's vision calls are in the step's record ----------------------------------------------
def test_the_cover_vision_calls_are_recorded(tmp_path):
    ctx = make_ctx(tmp_path, remote=None, cpa=None)
    (ctx.clip_dir / "cover").mkdir(parents=True)
    (ctx.clip_dir / "cover/frames.json").write_text(json.dumps({"chosen": 4.0, "calls": [
        {"kind": "clean", "t": 0.0, "model": "gpt-6.1-sol", "rc": 0, "elapsed_s": 2.0},
        {"kind": "clean", "t": 4.0, "model": "gpt-6.1-sol", "rc": 0, "elapsed_s": 2.0},
        {"kind": "boxes", "t": 4.0, "model": "gpt-6.1-sol", "rc": 0, "elapsed_s": 3.0}]}), encoding="utf-8")
    records, line = si.cover_vision_calls(ctx, 1)
    assert len(records) == 3 and all(r["kind"] == "vision" for r in records) and "判帧 2，取框 1" in line


# ----- verbatim import and the push's own doubt table (the trim command's way into step 3) ----------------------------
def _verbatim_push(tmp_path, clip, *, doubts=None, tsv_conf="高"):
    src = tmp_path / "vpush"
    rows = [(0.0, 2.5, "李豆沙", "哈哈哈第一句\n第二行"), (2.5, 5.0, "雾深Girimi", "第二句 一二三"),
            (5.0, 5.2, "李豆沙", "啊"), (5.1, 8.0, "李豆沙", "第四句，和第三句时间重叠")]
    for d in ("subs", "speaker", "boundary"):
        (src / d).mkdir(parents=True, exist_ok=True)
    (src / "subs/final.srt").write_text(_srt([(a, b, t) for a, b, _, t in rows]), encoding="utf-8")
    (src / "speaker/speaker.srt").write_text(_srt([(a, b, f"[{s}] {t}") for a, b, s, t in rows]), encoding="utf-8")
    head = "no\tid\tstart\tend\tspeaker\ttext\ttext_conf\tspeaker_conf\talt\tbasis\tbcut_text\n"
    (src / "subs/review.tsv").write_text(head + f"1\tB1\t0.0\t2.5\t李豆沙\t甲\t{tsv_conf}\t高\t\t依据\t乙\n",
                                          encoding="utf-8")
    if doubts is not None:
        (src / "subs/doubts.json").write_text(json.dumps(doubts, ensure_ascii=False), encoding="utf-8")
    return src


def test_a_verbatim_import_writes_the_pushed_text_unchanged_and_a_plain_import_still_rewrites_it(tmp_path):
    plain_ctx = make_ctx(tmp_path / "plain", remote=None, cpa=FakeCpa([]))
    verbatim_ctx = make_ctx(tmp_path / "verbatim", remote=None, cpa=FakeCpa([]))      # any CPA call would pop nothing
    push = _verbatim_push(tmp_path, verbatim_ctx.state.data["clip"])
    wanted = (push / "subs/final.srt").read_bytes()
    si.record_import(plain_ctx.state, plain_ctx.clip_dir, push, source="push-test", authority="U130")
    rec = si.record_import(verbatim_ctx.state, verbatim_ctx.clip_dir, push, source="push-test", authority="U130",
                           verbatim=True)
    assert rec["verbatim"] is True and "verbatim" not in plain_ctx.state.data["imported"]
    plain = si.step3_subtitles(plain_ctx)
    assert plain.ok, (plain.reason, plain.note)
    assert (plain_ctx.clip_dir / "subs/final.srt").read_bytes() != wanted          # laughter, line break, overlap rewritten
    out = si.step3_subtitles(verbatim_ctx)
    assert out.ok and out.cpa == [] and verbatim_ctx.adapters.cpa.calls == []
    assert (verbatim_ctx.clip_dir / "subs/final.srt").read_bytes() == wanted
    assert si.step4_speaker(verbatim_ctx).ok
    assert [c["speaker"] for c in _cues(verbatim_ctx, "speaker/speaker.srt")] == ["李豆沙", "雾深Girimi", "李豆沙", "李豆沙"]
    assert [c["text"] for c in _cues(verbatim_ctx, "speaker/speaker.srt")][0] == "哈哈哈第一句 第二行"


def test_a_push_that_brings_its_doubt_table_is_not_read_from_the_tsv_and_asks_nothing(tmp_path):
    held = {"id": "trim-d005", "cue_id": None, "start_s": 0.0, "end_s": 2.5, "current": "乙", "state": "KEEP",
            "final_text": "甲", "confidence": "低", "affects_meaning": True, "source": "trim", "basis": "带来的依据",
            "alternatives": ["乙"], "speaker": None, "speaker_confidence": None}
    free = dict(held, id="trim-d006", confidence="高", affects_meaning=False)
    for verbatim in (True, False):
        ctx = make_ctx(tmp_path / str(verbatim), remote=None, cpa=FakeCpa([]))
        push = _verbatim_push(tmp_path / str(verbatim), ctx.state.data["clip"], doubts=[held, free], tsv_conf="低")
        si.record_import(ctx.state, ctx.clip_dir, push, source="push-test", authority="U130", verbatim=verbatim)
        out = si.step3_subtitles(ctx)
        assert out.ok and ctx.adapters.cpa.calls == []                           # the tsv's 低 row would have asked
        assert json.loads((ctx.clip_dir / "subs/doubts.json").read_text(encoding="utf-8")) == [held, free]
        assert [d["id"] for d in doubt_policy.holds([held, free])] == ["trim-d005"]


def test_a_push_doubt_table_with_an_unsettled_meaning_effect_is_refused(tmp_path):
    ctx = make_ctx(tmp_path, remote=None, cpa=FakeCpa([]))
    row = {"id": "x1", "confidence": "低", "affects_meaning": None}
    push = _verbatim_push(tmp_path, ctx.state.data["clip"], doubts=[row])
    si.record_import(ctx.state, ctx.clip_dir, push, source="push-test", authority="U130", verbatim=True)
    out = si.step3_subtitles(ctx)
    assert not out.ok and out.reason == "IMPORT_DOUBTS_INVALID" and ctx.adapters.cpa.calls == []
