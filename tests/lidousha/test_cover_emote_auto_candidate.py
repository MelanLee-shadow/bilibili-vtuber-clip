"""CVR-02 (维护者 10/19): official emotes are default candidates, compared with the other routes by score.

The planner may omit an emote row. The staging step then appends one deterministic emote candidate
(manifest keyword match, recent main emotes excluded, 25 added as kmx's layer). It never blocks: an
unmatched hook, a full list, or an existing emote row only leaves a recorded reason.
"""

from __future__ import annotations

import pytest

from src.autoslice import title_cover_staging as staging
from src.autoslice.cover_emote import EmoteEntry, EmoteLibrary, pick_emote_for_text
from src.autoslice.title_cover_candidates import score_candidate


def _entry(emote_id: str, label: str, use_when: str, subject: str = "lidousha") -> EmoteEntry:
    return EmoteEntry(
        id=emote_id,
        label=label,
        subject=subject,
        file=f"emote/{emote_id}.png",
        hd_file=f"hd/{emote_id}.png",
        hd_sha256="0" * 64,
        baked_text="",
        description="",
        use_when=use_when,
    )


LIBRARY = EmoteLibrary(
    enabled=True,
    entries=(
        _entry("05", "被吓到", "突发惊吓/被吓一跳/愣住"),
        _entry("13", "得意", "得意得逞/嘚瑟/计划通"),
        _entry("25", "只是熊猫", "熊猫头梗/装无辜“我只是熊猫”/代画粉丝kmx(kimo熊)", "panda_creature"),
    ),
)

LINES = ["被吓到了", "快滚"]
FINAL_SRT = (
    "1\n00:00:01,000 --> 00:00:02,000\n被吓到了\n\n"
    "2\n00:00:02,000 --> 00:00:03,000\nkmx：快滚！\n"
)
RECENT_EMPTY = {
    "last_five_main_emotes": [],
    "source_session_main_emotes": [],
    "previous_route": None,
}


def _asset(kind: str, **extra) -> dict:
    return {"path": "x", "sha256": "0" * 64, "kind": kind, **extra}


def _assets() -> dict:
    return {
        "frame": _asset("stream_frame", participant="李豆沙"),
        "emote:05": _asset("official_emote", official_id="05", mask="m", mask_sha256="0" * 64),
        "emote:25": _asset("official_emote", official_id="25", mask="m", mask_sha256="0" * 64),
    }


def _plan(names: list[str], *, candidates: list[dict] | None = None, quotes: dict | None = None) -> dict:
    quotes = quotes or {}
    return {
        "participants": [{"name": name, "quote": quotes.get(name, "被吓到了")} for name in names],
        "cover_punch": {"main": "被吓到了", "sub": "快滚"},
        "candidates": candidates
        if candidates is not None
        else [
            {"id": "shot", "route": "screenshot", "source_ids": ["frame"], "reason": "截图"},
            {"id": "cut", "route": "cutout", "source_ids": ["frame"], "reason": "抠图"},
        ],
    }


def test_pick_matches_manifest_phrase_in_manifest_order():
    emote_id, reason = pick_emote_for_text("刚进去就被吓一跳", LIBRARY)
    assert emote_id == "05"
    assert reason.startswith("MATCHED: ")


def test_panda_creature_is_never_the_main_emote():
    emote_id, reason = pick_emote_for_text("熊猫头梗 我只是熊猫", LIBRARY)
    assert emote_id is None
    assert reason.startswith("NO_EMOTION_MATCH")


def test_excluded_recent_main_emote_is_skipped():
    assert pick_emote_for_text("得意得逞", LIBRARY, excluded={"13"})[0] is None


def test_no_match_returns_recorded_reason_not_exception():
    emote_id, reason = pick_emote_for_text("今天吃饭", LIBRARY)
    assert emote_id is None
    assert reason.startswith("NO_EMOTION_MATCH")


def test_adds_emote_candidate_with_kmx_layer_and_validates():
    plan = _plan(["李豆沙", "kmx"], quotes={"kmx": "kmx：快滚！"})
    assets = _assets()
    outcome = staging._add_auto_emote_candidate(
        plan, lines=LINES, assets=assets, recent=RECENT_EMPTY, library=LIBRARY, names=["李豆沙", "kmx"]
    )
    assert outcome["status"] == "ADDED"
    row = plan["candidates"][-1]
    assert row["route"] == "emote"
    assert row["main_emote"] == "emote:05"
    assert row["source_ids"] == ["emote:05", "emote:25"]
    names, _lines = staging._validate_plan(plan, FINAL_SRT, assets, RECENT_EMPTY)
    assert names == ["李豆沙", "kmx"]


def test_guest_participant_adds_stream_frame_layer():
    plan = _plan(["李豆沙", "礼墨"], quotes={"礼墨": "被吓到了"})
    outcome = staging._add_auto_emote_candidate(
        plan, lines=LINES, assets=_assets(), recent=RECENT_EMPTY, library=LIBRARY, names=["李豆沙", "礼墨"]
    )
    assert outcome["status"] == "ADDED"
    assert plan["candidates"][-1]["source_ids"] == ["emote:05", "frame"]


def test_present_guest_scores_people_two_for_emote_candidate():
    verdict = {
        key: {"pass": True, "reason": "图像理由"}
        for key in ("clean_layers", "identity", "source_faithful", "no_fabrication")
    }
    verdict["people"] = {"expected": ["李豆沙", "礼墨"], "present": ["李豆沙", "礼墨"], "reason": "图像理由"}
    verdict["hook_fit"] = {"score": 2, "reason": "图像理由"}
    verdict["face"] = [600, 200, 1000, 700]
    verdict["keep"] = []
    row = {
        "route": "emote",
        "status": "RENDERED",
        "verdict": verdict,
        "punch_font_size": 210,
        "main_emote": "emote:05",
        "render": {"measured_lines": []},
    }
    score = score_candidate(row, participants=["李豆沙", "礼墨"], recent=RECENT_EMPTY)
    assert score["status"] == "ELIGIBLE"
    assert score["P"] == 2


def test_existing_emote_row_is_left_alone():
    existing = {"id": "emo", "route": "emote", "source_ids": ["emote:13"], "main_emote": "emote:13", "reason": "r"}
    plan = _plan(
        ["李豆沙"],
        candidates=[existing, {"id": "shot", "route": "screenshot", "source_ids": ["frame"], "reason": "截图"}],
    )
    before = list(plan["candidates"])
    outcome = staging._add_auto_emote_candidate(
        plan, lines=LINES, assets=_assets(), recent=RECENT_EMPTY, library=LIBRARY, names=["李豆沙"]
    )
    assert outcome["status"] == "PLAN_ALREADY_HAS_EMOTE"
    assert plan["candidates"] == before


def test_full_plan_records_limit_without_raising():
    four = [{"id": f"c{i}", "route": "screenshot", "source_ids": ["frame"], "reason": "r"} for i in range(4)]
    plan = _plan(["李豆沙"], candidates=four)
    outcome = staging._add_auto_emote_candidate(
        plan, lines=LINES, assets=_assets(), recent=RECENT_EMPTY, library=LIBRARY, names=["李豆沙"]
    )
    assert outcome == {"status": "SKIPPED", "reason": "CANDIDATE_LIMIT_REACHED"}
    assert len(plan["candidates"]) == 4


def test_unmatched_hook_records_reason_and_keeps_plan_valid():
    plan = _plan(["李豆沙"], quotes={"李豆沙": "今天吃饭"})
    outcome = staging._add_auto_emote_candidate(
        plan, lines=["今天吃饭", "好饱"], assets=_assets(), recent=RECENT_EMPTY, library=LIBRARY, names=["李豆沙"]
    )
    assert outcome["status"] == "SKIPPED"
    assert outcome["reason"].startswith("NO_EMOTION_MATCH")
    assert plan["emote_candidate"] == outcome
    assert len(plan["candidates"]) == 2


def test_recent_main_emote_is_excluded_before_matching():
    recent = {**RECENT_EMPTY, "last_five_main_emotes": ["emote:05"]}
    plan = _plan(["李豆沙"])
    outcome = staging._add_auto_emote_candidate(
        plan, lines=LINES, assets=_assets(), recent=recent, library=LIBRARY, names=["李豆沙"]
    )
    assert outcome["status"] == "SKIPPED"
    assert len(plan["candidates"]) == 2


def test_missing_main_emote_asset_records_unavailable():
    assets = _assets()
    assets.pop("emote:05")
    plan = _plan(["李豆沙"])
    outcome = staging._add_auto_emote_candidate(
        plan, lines=LINES, assets=assets, recent=RECENT_EMPTY, library=LIBRARY, names=["李豆沙"]
    )
    assert outcome == {"status": "SKIPPED", "reason": "EMOTE_ASSET_UNAVAILABLE"}


def test_missing_kmx_layer_asset_still_adds_host_only_emote():
    assets = _assets()
    assets.pop("emote:25")
    plan = _plan(["李豆沙", "kmx"])
    outcome = staging._add_auto_emote_candidate(
        plan, lines=LINES, assets=assets, recent=RECENT_EMPTY, library=LIBRARY, names=["李豆沙", "kmx"]
    )
    assert outcome["status"] == "ADDED"
    assert plan["candidates"][-1]["source_ids"] == ["emote:05"]
