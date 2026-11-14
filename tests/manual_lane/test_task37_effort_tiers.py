"""TASK-37 (U168): one thinking tier per CPA text call, chosen by what the call is for.

What these tests hold:

- every registered purpose gets its own tier from lane_rules `effort.purposes`, and the tier reaches the adapter;
- a purpose nobody registered still runs, at `effort.default`, and says so in PROCESS (never silent);
- the effort is in the cache key, so changing a purpose's tier cannot read an answer made at the old tier, while an
  unchanged tier still hits its entry (the pre-TASK-37 medium entries included);
- step 6's record list is in input order under completion-order jitter, 50 runs in a row.

No ssh, no network, no paid model: every call goes to a fake.
"""

from __future__ import annotations

import json
import random
import threading
import time
from pathlib import Path

import pytest
from fakes import (COVER_BRIEF, COVER_EMOTE, JUDGE_PASS, UNDERSTANDING_CALLS, FakeCpa, FakeRemote, fake_cover, is_judge, make_ctx,
                   rules_with, srt, understanding_answer)

from src.autoslice.manual_lane import stage_impl as si
from src.autoslice.manual_lane.adapters import CPA_JUDGE_EFFORT, CPA_JUDGE_MODEL, RemoteResult
from src.autoslice.manual_lane.lane_rules import rules
from src.autoslice.manual_lane.runner import run_clip
from src.autoslice.manual_lane.step_io import _ask_cpa, _cpa_cache_path, _effort_of

RD = "/r/clip"
TIERS = {"low", "medium", "high"}


# ----- the registry itself -------------------------------------------------------------------------------------
def _purposes() -> dict:
    return rules("effort")["purposes"]


def test_every_registered_purpose_names_a_tier_and_says_why():
    purposes = _purposes()
    assert purposes, "lane_rules effort.purposes is empty"
    for name, entry in purposes.items():
        assert entry["effort"] in TIERS, f"{name}: {entry['effort']} is not a tier"
        assert str(entry.get("reason") or "").strip(), f"{name}: no reason"
        assert "U168" in str(entry.get("source") or ""), f"{name}: no U168 source"


def test_the_registry_covers_every_call_site_by_purpose():
    """The set U168's grouping implies: self-check/judge/review think, plain confirmation does not, generation and
    per-sentence adjudication keep the tier TASK-6 was measured at."""
    purposes = _purposes()
    expected = {"boundary": "high", "adjudicate": "medium", "concat-check": "low", "speaker-review": "high",
                "title": "medium", "title-read": "medium", "title-merge": "high", "title-judge": "high",
                "cover-brief": "medium", "cover-emote": "medium", "cover-text": "low", "review": "high", "review-c": "high",
                "import-meaning": "low"}
    assert {name: purposes[name]["effort"] for name in expected} == expected


@pytest.mark.parametrize("purpose", ["boundary", "adjudicate", "concat-check", "speaker-review", "title",
                                     "title-read", "title-merge", "title-judge", "cover-brief", "cover-emote", "cover-text", "review", "review-c",
                                     "import-meaning"])
def test_each_registered_purpose_resolves_to_its_own_tier(purpose):
    effort, note = _effort_of(purpose)
    assert effort == _purposes()[purpose]["effort"]
    assert note == ""            # a registered purpose is never announced


def test_an_unregistered_purpose_keeps_the_default_tier_and_says_so():
    effort, note = _effort_of("a-purpose-nobody-registered")
    assert effort == rules("effort")["default"] == "medium"
    assert "没有在 lane_rules effort.purposes 登记" in note and "a-purpose-nobody-registered" in note


def test_a_call_with_no_purpose_at_all_keeps_the_default_tier_and_says_so():
    effort, note = _effort_of(None)
    assert effort == "medium"
    assert "没有写用途" in note


# ----- the tier reaches the call, the record and the cache key --------------------------------------------------
class EffortCpa:
    """Records (call_id, effort) per ask; answers with one JSON object."""

    def __init__(self, text: str):
        self.text = text
        self.seen: list[tuple[str, str]] = []
        self.output_budgets: list[int | None] = []
        self._lock = threading.Lock()

    def ask(self, *, call_id, prompt_text, model, effort, remote_dir, max_output_tokens=None):
        with self._lock:
            self.seen.append((call_id, effort))
            self.output_budgets.append(max_output_tokens)
        return {"call_id": call_id, "model": model, "effort": effort, "rc": 0, "elapsed_s": 0.1,
                "response_text": self.text, "stderr_tail": ""}


def _ctx(tmp_path, cpa):
    return make_ctx(tmp_path, remote=FakeRemote(), cpa=cpa, bcut_text=srt(3, step=2.0))


@pytest.mark.parametrize("purpose,expected", [("boundary", "high"), ("concat-check", "low"),
                                             ("title-judge", "high"), ("adjudicate", "medium")])
def test_the_registered_tier_is_the_one_the_adapter_is_asked_at(tmp_path, purpose, expected):
    cpa = EffortCpa('{"ok": true}')
    ctx = _ctx(tmp_path, cpa)
    record = _ask_cpa(ctx, f"{purpose}-0", "提示词", purpose=purpose)
    assert [effort for _call_id, effort in cpa.seen] == [expected]
    assert record["effort"] == expected            # the tier rides on the record
    assert record["purpose"] == purpose
    assert "effort_note" not in record


def test_registered_model_and_tier_both_select_the_call_and_its_cache(tmp_path, monkeypatch):
    from src.autoslice.manual_lane.lane_rules import RULES_FILE

    doc = json.loads(RULES_FILE.read_text(encoding="utf-8"))
    cpa = EffortCpa('{"ok": true}')
    ctx = _ctx(tmp_path, cpa)
    records = []
    for index, (model, effort, budget) in enumerate((("model-a", "high", 32768), ("model-b", "high", 32768),
                                                      ("model-b", "medium", 32768), ("model-b", "medium", 16000),
                                                      ("model-b", "medium", 16000))):
        doc['effort']['purposes']['subtitle-agent'].update(model=model, effort=effort, max_output_tokens=budget)
        path = tmp_path / f'rules-{index}.json'
        path.write_text(json.dumps(doc), encoding='utf-8')
        monkeypatch.setenv('MANUAL_LANE_RULES', str(path))
        records.append(_ask_cpa(ctx, f'agent-{index}', 'same prompt', purpose='subtitle-agent'))
    assert [(r['model'], r['effort'], r['max_output_tokens'], r.get('cached', False)) for r in records] == [
        ('model-a', 'high', 32768, False), ('model-b', 'high', 32768, False),
        ('model-b', 'medium', 32768, False), ('model-b', 'medium', 16000, False),
        ('model-b', 'medium', 16000, True)]
    assert len(cpa.seen) == 4
    assert cpa.output_budgets == [32768, 32768, 32768, 16000]


def test_an_unregistered_purpose_runs_at_the_default_tier_and_leaves_a_process_note(tmp_path):
    cpa = EffortCpa('{"ok": true}')
    ctx = _ctx(tmp_path, cpa)
    record = _ask_cpa(ctx, "mystery-0", "提示词", purpose="mystery")
    assert cpa.seen == [("mystery-0", CPA_JUDGE_EFFORT)]
    assert record["effort"] == CPA_JUDGE_EFFORT
    assert "effort_note" in record


def test_the_process_file_shows_the_tier_of_every_call_and_the_unregistered_warning(tmp_path):
    """PROCESS is the human record: a step's calls print model, rc, elapsed and now the tier; an unregistered call
    also prints the warning line, so a new call site cannot sit on the default tier unnoticed."""
    from src.autoslice.manual_lane.runner import Step, StepOutcome

    cpa = EffortCpa('{"ok": true}')
    ctx = _ctx(tmp_path, cpa)

    def step_run(c):
        registered = _ask_cpa(c, "review-0", "提示词一", purpose="review")
        unknown = _ask_cpa(c, "mystery-0", "提示词二", purpose="mystery")
        return StepOutcome(True, cpa=[registered, unknown],
                           process=[line for rec in (registered, unknown) if rec.get("effort_note")
                                    for line in [rec["effort_note"]]])
    run_clip(ctx, [Step(1, "one", "一步", step_run)])
    text = (ctx.clip_dir / "PROCESS.md").read_text(encoding="utf-8")
    assert "`review-0`" in text and "`mystery-0`" in text
    assert "档位 high" in text and "档位 medium" in text
    assert "没有在 lane_rules effort.purposes 登记" in text


# ----- cache keys ------------------------------------------------------------------------------------------------
def test_the_cache_key_changes_with_the_tier(tmp_path):
    ctx = _ctx(tmp_path, EffortCpa("{}"))
    low = _cpa_cache_path(ctx, "提示词", 0, "low")
    medium = _cpa_cache_path(ctx, "提示词", 0, "medium")
    high = _cpa_cache_path(ctx, "提示词", 0, "high")
    assert len({low, medium, high}) == 3


def test_a_tier_change_does_not_read_the_answer_made_at_the_old_tier(tmp_path, monkeypatch):
    """The same purpose, re-tiered: the old answer must not be served, and the call has to go out again."""
    cpa = EffortCpa('{"ok": true}')
    ctx = _ctx(tmp_path, cpa)
    first = _ask_cpa(ctx, "review-0", "提示词", purpose="review")
    assert first["effort"] == "high" and len(cpa.seen) == 1
    assert Path(first["cache_path"]).exists()

    rules_with(tmp_path, monkeypatch, effort={"default": "medium",
                                              "purposes": {**_purposes(), "review": {"effort": "medium",
                                                                                     "reason": "测试",
                                                                                     "source": "U168"}}})
    second = _ask_cpa(ctx, "review-0", "提示词", purpose="review")
    assert second["effort"] == "medium"
    assert len(cpa.seen) == 2                     # a new call, not the cached high answer
    assert not second.get("cached")
    assert second["cache_path"] != first["cache_path"]


def test_the_same_tier_still_reads_its_own_cached_answer(tmp_path):
    cpa = EffortCpa('{"ok": true}')
    ctx = _ctx(tmp_path, cpa)
    _ask_cpa(ctx, "review-0", "提示词", purpose="review")
    again = _ask_cpa(ctx, "review-1", "提示词", purpose="review")
    assert len(cpa.seen) == 1 and again.get("cached") is True


def test_a_pre_task37_medium_entry_still_matches(tmp_path):
    """Entries written before this task were keyed (model, medium, prompt) with no `run` tail.  A purpose that still
    asks for medium must keep reading them, or every rerun of a finished clip would spend the money again."""
    import hashlib

    ctx = _ctx(tmp_path, EffortCpa('{"ok": true}'))
    legacy_parts = [CPA_JUDGE_MODEL, "medium", "提示词"]
    legacy_key = hashlib.sha256(json.dumps(legacy_parts, ensure_ascii=False).encode("utf-8")).hexdigest()
    legacy = ctx.clip_dir.parent.parent / "cache" / "cpa" / f"{legacy_key}.json"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(json.dumps({"model": CPA_JUDGE_MODEL, "effort": "medium", "rc": 0,
                                  "response_text": '{"ok": true}', "complete": True}), encoding="utf-8")
    record = _ask_cpa(ctx, "adjudicate-p1-1", "提示词", purpose="adjudicate")
    assert record.get("cached") is True and record["effort"] == "medium"


# ----- the call sites ask at their own tier ---------------------------------------------------------------------
def test_step6_title_generation_and_its_judge_run_at_different_tiers(tmp_path):
    tiers: dict[str, str] = {}
    answers = json.dumps({"candidates": [{"title": "【李豆沙】半分钟后她破防了", "on_stage": ["李豆沙"],
                                          "punchline_person": "她破防了",
                                          "swap_check": {"holds_with_other_host": False, "reason": "测试"}}],
                          "recommended": 0, "cover_lines": ["破防了", "半分钟"]}, ensure_ascii=False)

    class Tiers(FakeCpa):
        def ask(self, *, call_id, prompt_text, model, effort, remote_dir):
            tiers[call_id] = effort
            return super().ask(call_id=call_id, prompt_text=prompt_text, model=model, effort=effort,
                               remote_dir=remote_dir)

    def script(name, body, remote):
        if name.startswith("s6-cover-job"):
            fake_cover(remote, RD)
        return RemoteResult(0, "", "")
    ctx = make_ctx(tmp_path, remote=FakeRemote(on_script=script), cpa=Tiers([answers, JUDGE_PASS]))
    (ctx.clip_dir / "subs").mkdir(parents=True, exist_ok=True)
    (ctx.clip_dir / "subs" / "final.srt").write_text(srt(40), encoding="utf-8")
    out = si.step6_title_cover(ctx)
    assert out.ok, out.note
    assert tiers["title"] == "medium"                     # generation
    assert tiers["title-read-1"] == tiers["title-read-2"] == "medium"   # reading the clip is a generation too
    assert tiers["title-merge"] == "high"                 # the merge checks the two readings against the subtitles
    assert tiers["title-judge-r0-1"] == tiers["title-judge-r0-2"] == "high"   # the judge
    assert tiers["cover-brief"] == "medium"           # the cover brief is a generation, not a check
    recorded = {c["call_id"]: c.get("effort") for c in out.cpa}
    assert recorded["title"] == "medium" and recorded["title-judge-r0-1"] == "high"
    # the cover's vision calls (cover-judge-*, cover-image-*) are made by pick_frame.py on OCI3, not through _ask_cpa:
    # they are not text calls and carry no thinking tier
    assert all(v is None for k, v in recorded.items() if k.startswith(("cover-judge", "cover-image")))


def test_the_splice_guard_asks_at_the_low_tier(tmp_path):
    """concat-check is a confirmation: it only asks whether the chosen text is two candidates joined."""
    from test_splice import _doubt, _row, SPLICED

    seen: list[str] = []

    class Tiers(FakeCpa):
        def ask(self, *, call_id, prompt_text, model, effort, remote_dir):
            seen.append(effort)
            return super().ask(call_id=call_id, prompt_text=prompt_text, model=model, effort=effort,
                               remote_dir=remote_dir)

    clean = json.dumps([{"id": "d010", "state": "ADOPT", "final_text": "是真的杀呀", "affects_meaning": True,
                         "basis": "单一候选"}], ensure_ascii=False)
    ctx = make_ctx(tmp_path, remote=None, cpa=Tiers([clean]))
    si.resolve_concatenation(ctx, _doubt(), _row(SPLICED), call_prefix="concat-d010", context="1 00:00:00 x")
    assert seen == ["low"]


# ----- record order under completion-order jitter ----------------------------------------------------------------
class JitterCpa:
    """A fake whose calls finish in a random order (the delay is drawn per call), so completion order differs from
    input order inside a run.  `arrival` keeps when a thread reached the fake, for the "the race really happened"
    assertion."""

    def __init__(self, answers: dict[str, str], delay=(0.0, 0.012)):
        self.answers = answers
        self.delay = delay
        self.arrival: list[str] = []
        self._lock = threading.Lock()

    def ask(self, *, call_id, prompt_text, model, effort, remote_dir):
        if self.delay[1] > 0:
            time.sleep(random.uniform(*self.delay))
        with self._lock:
            self.arrival.append(call_id)
        if call_id == "cover-brief":
            text = COVER_BRIEF
        elif call_id == "cover-emote":
            text = COVER_EMOTE
        elif is_judge(call_id):
            text = JUDGE_PASS
        else:
            text = understanding_answer(call_id) or self.answers["title"]
        return {"call_id": call_id, "model": model, "effort": effort, "rc": 0, "elapsed_s": 0.1,
                "response_text": text, "stderr_tail": ""}


TITLE = json.dumps({"candidates": [{"title": "【李豆沙】半分钟后她破防了", "on_stage": ["李豆沙"],
                                    "punchline_person": "她破防了",
                                    "swap_check": {"holds_with_other_host": False, "reason": "测试"}}],
                    "recommended": 0, "cover_lines": ["破防了", "半分钟"]}, ensure_ascii=False)


def _title_ctx(tmp_path, cpa):
    def script(name, body, remote):
        if name.startswith("s6-cover-job"):
            fake_cover(remote, RD)
        return RemoteResult(0, "", "")
    ctx = make_ctx(tmp_path, remote=FakeRemote(on_script=script), cpa=cpa)
    (ctx.clip_dir / "subs").mkdir(parents=True, exist_ok=True)
    (ctx.clip_dir / "subs" / "final.srt").write_text(srt(40), encoding="utf-8")
    return ctx


def test_the_e2e_fake_log_is_in_input_order_50_runs_in_a_row(tmp_path):
    """The flake TASK-35 saw, as a guard (measured on 9abd2888f: this test fails there, 1 failed / 50 e2e runs).

    test_pipeline_fake_e2e.py asserts on its fake's call log.  That log used to be a raw append, i.e. completion
    order; step 6 asks its two judge runs at once (TASK-31), so on some runs it read -r0-2 before -r0-1 and the e2e
    test failed.  Measured on 9abd2888f: the LANE's own record list was 0/200 out of order — the contract TASK-31
    states was never broken; what raced was the instrument.  The fake now reads its log through `serial_order`, and
    this test holds that over 50 runs with a per-call random delay, so the two judge runs really do finish in a
    varying order inside the 50."""
    from test_pipeline_fake_e2e import ScriptedCpa

    class JitterCpa(ScriptedCpa):
        def ask(self, *, call_id, prompt_text, model, effort, remote_dir):
            time.sleep(random.uniform(0.0, 0.012))
            return super().ask(call_id=call_id, prompt_text=prompt_text, model=model, effort=effort,
                               remote_dir=remote_dir)

    want = [*UNDERSTANDING_CALLS, "title", "title-judge-r0-1", "title-judge-r0-2", "cover-brief", "cover-emote"]
    seen = set()
    for i in range(50):
        cpa = JitterCpa({"title": TITLE})
        ctx = _title_ctx(tmp_path / f"run{i}", cpa)
        out = si.step6_title_cover(ctx)
        assert out.ok, out.note
        seen.add(tuple(cpa.calls))
    assert seen == {tuple(want)}, f"the fake's call log came back in completion order: {seen}"


def test_the_lane_record_list_is_in_input_order_50_runs_in_a_row(tmp_path):
    """The lane's own contract, independently of any fake: `out.cpa` (the list a step records) is in input order
    whatever finishes first.  Measured on 9abd2888f: held 0/200 — this test guards the contract, not the flake."""
    seen = set()
    for i in range(50):
        cpa = JitterCpa({"title": TITLE})
        ctx = _title_ctx(tmp_path / f"lane{i}", cpa)
        out = si.step6_title_cover(ctx)
        assert out.ok, out.note
        seen.add(tuple(c["call_id"] for c in out.cpa if c["call_id"].startswith("title")))
    assert seen == {(*UNDERSTANDING_CALLS, "title", "title-judge-r0-1", "title-judge-r0-2")}, f"record order varied: {seen}"
