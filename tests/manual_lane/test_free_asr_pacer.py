"""U152 (公开规则11-02: 「剪映被限流可以等等。另外，也确实不应该发的太猛。」): the free ASR providers are paced by one
rule (assets/manual_lane/remote/free_asr_paced.py), with the pacing read from the profile's manual_lane.json.

A fake clock and sleep stand in for time: every call's start time is recorded, so the tests assert how many calls a
provider gets and how far apart they are.
"""

from __future__ import annotations

import importlib.util
import json
from itertools import pairwise
from pathlib import Path

import pytest

from src.autoslice.manual_lane import layout as lay
from src.autoslice.manual_lane.step3_subtitles import FREE_ASR_PROVIDERS, free_asr_body

TOOL = Path(__file__).resolve().parents[2] / "assets" / "manual_lane" / "remote" / "free_asr_paced.py"
spec = importlib.util.spec_from_file_location("free_asr_paced", TOOL)
fap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fap)


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _provider(clock: Clock, answers: list):
    """A provider that answers from `answers` in turn (an exception is raised) and records when each call started."""
    starts: list[float] = []

    def call():
        starts.append(clock.now)
        clock.now += 90          # one recognition takes a while
        answer = answers[min(len(starts), len(answers)) - 1]
        if isinstance(answer, Exception):
            raise answer
        return answer
    return call, starts


def _pacer(tmp_path: Path, clock: Clock, *, min_interval_s: float = 60, cooldown_s: float = 900):
    return fap.Pacer(tmp_path / "pace", min_interval_s=min_interval_s, cooldown_s=cooldown_s,
                     clock=clock.time, sleep=clock.sleep)


def test_consecutive_429s_are_sent_rate_limit_attempts_times_a_cooldown_apart_then_given_up(tmp_path):
    clock = Clock()
    call, starts = _provider(clock, [RuntimeError("jianying HTTP 429: Too Many Requests")])
    result, status = fap.run_provider("jianying", call, _pacer(tmp_path, clock), attempts=4, rate_limit_attempts=3,
                                      log=lambda _line: None)
    assert (result, status) == (None, "RATE_LIMITED")
    assert len(starts) == 3                                    # never more than rate_limit_attempts
    assert all(b - a >= 900 for a, b in pairwise(starts))   # each a full cooldown after the last
    # the provider's next call, from any clip, is still held off by the cooldown
    call2, starts2 = _provider(clock, ["ok"])
    assert fap.run_provider("jianying", call2, _pacer(tmp_path, clock), attempts=4, rate_limit_attempts=3,
                            log=lambda _line: None) == ("ok", "OK")
    assert starts2[0] - starts[-1] >= 900


def test_bcut_code_509_counts_as_a_rate_limit():
    assert fap.rate_limited(RuntimeError("bcut query failed: code -509 请求过于频繁"))
    assert fap.rate_limited(RuntimeError("HTTP Error 429"))
    assert not fap.rate_limited(RuntimeError("HTTP Error 500: upstream"))
    assert not fap.rate_limited(RuntimeError("task 14290 failed"))


def test_successful_calls_of_one_provider_are_at_least_the_minimum_interval_apart_across_clips(tmp_path):
    clock = Clock()
    starts_all: list[float] = []
    for _clip in range(3):              # three clips, each with its own pacer object (another process): one state
        call, starts = _provider(clock, ["ok"])
        assert fap.run_provider("bcut", call, _pacer(tmp_path, clock), attempts=4, rate_limit_attempts=3,
                                log=lambda _line: None) == ("ok", "OK")
        starts_all += starts
    assert all(b - (a + 90) >= 60 for a, b in pairwise(starts_all))   # from the end of one call to the next start
    # another provider is paced on its own: its first call does not wait for bcut
    call, starts = _provider(clock, ["ok"])
    before = clock.now
    fap.run_provider("jianying", call, _pacer(tmp_path, clock), attempts=4, rate_limit_attempts=3,
                     log=lambda _line: None)
    assert starts[0] == before


def test_other_failures_are_retried_attempts_times_then_failed(tmp_path):
    clock = Clock()
    call, starts = _provider(clock, [RuntimeError("HTTP Error 500: upstream")])
    result, status = fap.run_provider("bcut", call, _pacer(tmp_path, clock), attempts=4, rate_limit_attempts=3,
                                      log=lambda _line: None)
    assert (result, status) == (None, "FAILED") and len(starts) == 4
    assert all(b - a - 90 >= 60 for a, b in pairwise(starts))   # the minimum interval holds too
    steps = iter(clock.sleeps)
    assert all(step in steps for step in (10, 20, 30))      # the old shell loop's 10/20/30 s steps, in order


def test_a_rate_limit_then_an_answer_is_ok(tmp_path):
    clock = Clock()
    call, starts = _provider(clock, [RuntimeError("HTTP 429"), "ok"])
    assert fap.run_provider("jianying", call, _pacer(tmp_path, clock), attempts=4, rate_limit_attempts=3,
                            log=lambda _line: None) == ("ok", "OK")
    assert len(starts) == 2 and starts[1] - starts[0] >= 900


def test_the_step_sends_the_profile_pacing_and_waits_long_enough_for_the_cooldowns():
    pace = lay.layout().free_asr
    body, timeout = free_asr_body("/r/c")
    assert (f"{pace.min_interval_s:g} {pace.rate_limit_cooldown_s:g} {pace.attempts} {pace.rate_limit_attempts} "
            + " ".join(FREE_ASR_PROVIDERS)) in body
    assert lay.layout().free_asr_state_dir in body
    assert timeout >= pace.rate_limit_attempts * pace.rate_limit_cooldown_s
    # conservative defaults (U152): at least a minute between calls, a long cooldown, few rate-limited tries
    assert pace.min_interval_s >= 60 and pace.rate_limit_cooldown_s >= 600 and pace.rate_limit_attempts <= 3


def test_layout_values_come_from_the_profile_and_the_environment_overrides_them(tmp_path):
    env = {"MANUAL_LANE_FREE_ASR_MIN_INTERVAL_S": "120", "MANUAL_LANE_WORK_ROOT": "/w", "MANUAL_LANE_ROOM_ID": "1"}
    got = lay.load_layout(env, path=lay.REPO_ROOT / "profiles" / "lidousha" / "manual_lane.json")
    assert got.free_asr.min_interval_s == 120 and got.work_root == "/w"
    assert got.session_root("2026-10-24") == "/w/2026-10-24"   # a session's directory is derived, never a default
    broken = tmp_path / "manual_lane.json"
    broken.write_text('{"schema": "manual-lane.runtime-layout.v1"}', encoding="utf-8")
    with pytest.raises(lay.LayoutError, match="remote_host is not set"):
        lay.load_layout({"MANUAL_LANE_ROOM_ID": "1"}, path=broken)


def test_search_mcp_url_is_optional_in_the_profile_and_overridden_by_the_environment(tmp_path):
    profile = lay.REPO_ROOT / "profiles" / "lidousha" / "manual_lane.json"
    doc = json.loads(profile.read_text(encoding="utf-8"))
    env = {"MANUAL_LANE_ROOM_ID": "1"}
    assert lay.load_layout(env, path=profile).search_mcp_url == doc["search_mcp_url"] != ""
    assert lay.load_layout({**env, "MANUAL_LANE_SEARCH_MCP_URL": "http://o.test/mcp"}, path=profile).search_mcp_url == \
        "http://o.test/mcp"
    bare = tmp_path / "manual_lane.json"                       # another channel's profile without the key
    bare.write_text(json.dumps({k: v for k, v in doc.items() if k != "search_mcp_url"}), encoding="utf-8")
    got = lay.load_layout(env, path=bare)
    assert got.search_mcp_url == "" and got.work_root == doc["work_root"] and got.remote_host == doc["remote_host"]


def test_shared_assets_are_derived_from_work_root_not_a_dated_task_directory():
    profile = lay.REPO_ROOT / "profiles" / "lidousha" / "manual_lane.json"
    got = lay.load_layout({"MANUAL_LANE_ROOM_ID": "1"}, path=profile)
    assert got.intro_dir == f"{got.work_root}/assets/intro"
    assert got.cover_font == f"{got.work_root}/assets/fonts/RuiZiZhenYan.ttf"
    for path in (got.intro_dir, got.cover_font):
        assert "supercut" not in path and not any(part[:2] == "20" and part[2:4].isdigit() for part in path.split("/"))
    moved = lay.load_layout({"MANUAL_LANE_ROOM_ID": "1", "MANUAL_LANE_WORK_ROOT": "/w"}, path=profile)
    assert (moved.intro_dir, moved.cover_font) == ("/w/assets/intro", "/w/assets/fonts/RuiZiZhenYan.ttf")
    pinned = lay.load_layout({"MANUAL_LANE_ROOM_ID": "1", "MANUAL_LANE_INTRO_DIR": "/i", "MANUAL_LANE_COVER_FONT": "/f.ttf"},
                             path=profile)
    assert (pinned.intro_dir, pinned.cover_font) == ("/i", "/f.ttf")
