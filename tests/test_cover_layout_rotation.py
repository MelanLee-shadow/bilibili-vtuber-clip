"""The cover composition templates and their one selector (src/autoslice/cover_layout_rotation.py).

Owner,, the covers all looked the same.  18 of the 20 covers before had the
words on top: the lane's column turn was refused or skipped and fell back to top, and the skill renderer's `auto` layout
took the largest punchline, which is almost always the top's.  The channel's template table now says which compositions
exist; the selector passes over the ones the last covers and this stream's last covers used, the ones that do not apply,
and the ones that do not fit, and records why."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest
from PIL import Image, ImageChops

from src.autoslice import cover_layout_rotation as rotation
from src.autoslice.cover_style_rotation import REPO_TABLE as STYLE_TABLE

REPO = Path(__file__).resolve().parents[1]
TABLE_PATH = REPO / "assets/lidousha/cover_layout_templates.json"
TABLE = rotation.load_table(TABLE_PATH)
SOLO = {"people": "solo", "lines": ["打针不会哭的人", "无趣的大人"], "column_rows": True, "route": "screenshot",
        "fans_emote": False, "watching": False}
FONT = Path(os.environ.get("LIDOUSHA_COVER_FONT") or Path.home() / ".local/share/fonts/lidousha/RuiZiZhenYan.ttf")


def _fits(template):
    return {"punch": 220}


def _remote(name):
    from src.autoslice.manual_lane import stage_impl as si

    spec = importlib.util.spec_from_file_location(f"rotation_tested_{name[:-3]}", Path(si.REMOTE_TOOLS) / name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _lane_rules():
    from src.autoslice.manual_lane.lane_rules import rules

    return rules("cover")


def test_the_table_has_the_compositions_the_owner_and_the_peers_named():
    ids = {t["template_id"]: t for t in TABLE["templates"]}
    assert {"column-left", "column-right", "top-two", "diagonal", "bottom-banner", "closeup-left", "closeup-right",
            "panels-top", "panels-bottom", "panels-diagonal", "emote-collage", "reaction-inset"} <= set(ids)
    assert ids["column-left"]["text"] == "left" and ids["column-right"]["text"] == "right"
    assert all(ids[t]["rows"] == "column" for t in ("column-left", "column-right", "closeup-left", "closeup-right"))
    assert TABLE["recent_covers"] == 3 and TABLE["session_recent_covers"] == 2 and TABLE["punch_floor_px"] == 200


def test_every_channel_has_its_own_table_of_the_same_structure():
    # The public snapshot intentionally omits other channels and the private exporter's source template.
    private_source = (REPO / "scripts/export_oss_snapshot.py").is_file()
    channels = ["lidousha", "huali", "sumi", "sumire"] if private_source else ["lidousha"]
    tables = [rotation.load_table(REPO / f"assets/{c}/cover_layout_templates.json") for c in channels]
    tables.append(rotation.load_table(REPO / "assets/_template/cover_layout_templates.json"))
    if private_source:
        tables.append(rotation.load_table(REPO / "docs/oss/templates/cover_layout_templates.json"))
    shape = [(t["template_id"], t["text"], t["rows"], sorted(t["applies"])) for t in TABLE["templates"]]
    assert all([(t["template_id"], t["text"], t["rows"], sorted(t["applies"])) for t in table["templates"]] == shape
               for table in tables)
    for channel in [*channels, "_template"]:
        profile = json.loads((REPO / f"profiles/{channel}/profile.json").read_text(encoding="utf-8"))
        assert profile["assets"]["files"]["cover_layout_templates"] == "cover_layout_templates.json"


def test_consecutive_covers_never_repeat_the_last_ones():
    history: list[str] = []
    for n in range(12):
        _, selection = rotation.select(TABLE, SOLO, attempt=_fits, history=history, content_sha256=f"{n:064x}")
        assert selection["fallback"] is None
        assert selection["template_id"] not in history[-TABLE["recent_covers"]:]
        assert {s["template_id"] for s in selection["skipped"] if s["reason"] == "recent_used"} == set(history[-3:])
        history.append(selection["template_id"])
    assert len(set(history)) == 7                     # every template that applies to a solo cover with column rows


def test_the_streams_own_covers_are_passed_over_too():
    # the last three covers (any stream) and the last two of this stream: five templates are passed over
    history = ["top-two", "diagonal", "bottom-banner"]
    session = ["column-left", "column-right"]
    _, selection = rotation.select(TABLE, SOLO, attempt=_fits, history=history, session_history=session)
    assert selection["template_id"] in {"closeup-left", "closeup-right"}
    reasons = {s["template_id"]: s["reason"] for s in selection["skipped"]}
    assert reasons["column-left"] == "session_used" and reasons["top-two"] == "recent_used"


def test_collaboration_rotates_visual_families_and_excludes_both_adjacent_covers():
    facts = dict(SOLO, people="panels", people_count=3, guest_line=False)
    families = {t["template_id"]: t.get("family", t["template_id"]) for t in TABLE["templates"]}
    history, session = [], []
    for n in range(12):
        _, choice = rotation.select(TABLE, facts, attempt=_fits, history=history, session_history=session,
                                    content_sha256=f"{n:064x}")
        family = families[choice["template_id"]]
        assert not history or family != families[history[-1]]
        assert not session or family != families[session[-1]]
        history.append(choice["template_id"])
        session.append(choice["template_id"])
    assert len({families[t] for t in history}) >= 4
    # Three different positions of words over the same vertical strips are one visual family.
    assert len({families[t] for t in ("panels-top", "panels-bottom", "panels-diagonal")}) == 1
    fresh, _, skipped = rotation.order(TABLE, dict(facts, guest_line=True))
    assert not {"collab-portraits", "collab-stacked"}.intersection(t["template_id"] for t in fresh)
    assert any(s["template_id"] == "collab-portraits" and "嘉宾" in s["reason"] for s in skipped)


def test_two_concurrent_collaboration_selections_share_the_actual_batch_lock(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from types import SimpleNamespace

    from src.autoslice.manual_lane.step6_title import cover_batch_lock

    state = tmp_path / "task" / "state"
    state.mkdir(parents=True)
    ctx = SimpleNamespace(clip_dir=tmp_path / "task" / "clips" / "current",
                          state=SimpleNamespace(path=state / "task.json"))
    history_file = state / "chosen.json"
    start = Barrier(2)
    facts = dict(SOLO, people="panels", people_count=3, guest_line=False)
    families = {t["template_id"]: t.get("family", t["template_id"]) for t in TABLE["templates"]}

    def select_one(_):
        start.wait(timeout=10)
        with cover_batch_lock(ctx):
            history = json.loads(history_file.read_text()) if history_file.exists() else []
            _, choice = rotation.select(TABLE, facts, attempt=_fits, history=history, session_history=history)
            history_file.write_text(json.dumps([*history, choice["template_id"]]))
            return choice["template_id"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        selected = list(pool.map(select_one, range(2)))
    assert families[selected[0]] != families[selected[1]]


def test_sibling_tasks_share_cover_history_and_lock(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from types import SimpleNamespace

    from src.autoslice.manual_lane import channel, cover_scope
    from src.autoslice.manual_lane.step6_cover import _lane_covers
    from src.autoslice.manual_lane.step6_title import cover_batch_lock

    start = Barrier(2)
    monkeypatch.setattr(cover_scope, "layout", lambda: SimpleNamespace(work_root=str(tmp_path)))
    facts = dict(SOLO, people="panels", people_count=3, guest_line=False)
    families = {t["template_id"]: t.get("family", t["template_id"]) for t in TABLE["templates"]}

    def select_in_task(index):
        task = tmp_path / "lane" / f"task-{index}"
        state, clip = task / "state", task / "clips" / f"clip-{index}"
        state.mkdir(parents=True)
        (clip / "title").mkdir(parents=True)
        ctx = SimpleNamespace(clip_dir=clip, state=SimpleNamespace(path=state / f"clip-{index}.json"))
        start.wait(timeout=10)
        with cover_batch_lock(ctx):
            history = [r["template_id"] for r in _lane_covers(ctx)]
            _, selection = rotation.select(TABLE, facts, attempt=_fits, history=history, session_history=history)
            (clip / "title" / "candidates.json").write_text(json.dumps({"cover_route": {
                "method": "skill.candidates", "at": "2026-11-24T01:00:00Z", "recording_date": "2026-11-24",
                "peers": {"names": [channel.HOST]},
                "chosen": {"route": "screenshot", "template_id": selection["template_id"]},
            }}), encoding="utf-8")
            return selection["template_id"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        selected = list(pool.map(select_in_task, range(2)))
    assert families[selected[0]] != families[selected[1]]


def test_on_host_tasks_in_different_directories_share_durable_cover_history(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from types import SimpleNamespace

    from src.autoslice.manual_lane import cover_scope
    from src.autoslice.manual_lane.adapters import LocalRemote
    from src.autoslice.manual_lane.step6_cover import _lane_covers
    from src.autoslice.manual_lane.step6_title import cover_batch_lock

    work = tmp_path / "work"
    (work / "lane").mkdir(parents=True)
    monkeypatch.setattr(cover_scope, "layout", lambda: SimpleNamespace(work_root=str(work)))
    start = Barrier(2)
    facts = dict(SOLO, people="panels", people_count=3, guest_line=False)
    families = {t["template_id"]: t.get("family", t["template_id"]) for t in TABLE["templates"]}

    def choose(index):
        task = tmp_path / f"separate-{index}" / "task"
        state, clip = task / "state", task / "clips" / f"clip-{index}"
        state.mkdir(parents=True)
        clip.mkdir(parents=True)
        ctx = SimpleNamespace(clip_dir=clip, state=SimpleNamespace(path=state / "clip.json"),
                              adapters=SimpleNamespace(remote=LocalRemote(scripts_dir=str(task / "scripts"))))
        start.wait(timeout=10)
        with cover_batch_lock(ctx):
            history = [c["template_id"] for c in _lane_covers(ctx)]
            _, selected = rotation.select(TABLE, facts, attempt=_fits, history=history, session_history=history)
            # Record before candidates.json is written, including the crash window between those two writes.
            cover_scope.note(ctx, {"at": "2026-11-24T01:00:00Z", "recording_date": "2026-11-24",
                                  "chosen": {"route": "screenshot", "template_id": selected["template_id"]}})
            return selected["template_id"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        selected = list(pool.map(choose, range(2)))
    assert families[selected[0]] != families[selected[1]]


def test_a_template_that_does_not_apply_is_passed_over_with_why():
    no_rows = dict(SOLO, column_rows=False)
    fresh, _, skipped = rotation.order(TABLE, no_rows)
    assert {t["template_id"] for t in fresh} == {"top-two", "diagonal", "bottom-banner"}
    why = {s["template_id"]: s["reason"] for s in skipped}
    assert why["column-left"] == "not_applicable: 封面字没有拆成竖列的短行"
    assert why["panels-top"].startswith("not_applicable: 人物")
    long_punch = dict(SOLO, lines=["打针", "一二三四五六七八"])
    assert "diagonal" in {s["template_id"] for s in rotation.order(TABLE, long_punch)[2]}
    panels = dict(SOLO, people="panels")
    collab = {"collab-pair", "collab-portraits", "collab-stacked", "collab-dialogue"}
    assert {t["template_id"] for t in rotation.order(TABLE, panels)[0]} == {"panels-top", "panels-bottom", "panels-diagonal"} | collab
    # the split screen's diagonal is the diagonal's: two lines, a punchline of at most 7 characters
    assert {t["template_id"] for t in rotation.order(TABLE, dict(long_punch, people="panels"))[0]} == {"panels-top", "panels-bottom"} | collab
    fans = dict(SOLO, route="emote", fans_emote=True)
    assert [t["template_id"] for t in rotation.order(TABLE, fans)[0]] == ["emote-collage"]


def test_a_template_that_does_not_fit_falls_through_to_the_next_and_the_reason_is_kept():
    tried = []

    def attempt(template):
        tried.append(template["template_id"])
        if template["rows"] == "column":
            raise rotation.TemplateRefused("「怎么这么」最多只能放到 94px")
        if template["template_id"] == "diagonal":
            return {"punch": 160}                     # under the floor: passed over, never made with smaller words
        return {"punch": 214}

    result, selection = rotation.select(TABLE, SOLO, attempt=attempt, history=["top-two"])
    assert selection["template_id"] == "bottom-banner" and result["punch"] == 214 and selection["fallback"] is None
    reasons = {s["template_id"]: s["reason"] for s in selection["skipped"]}
    assert reasons["column-left"].startswith("does_not_fit: 「怎么这么」") and reasons["diagonal"] == "punch_below_floor: 160px < 200px"
    assert tried.index("bottom-banner") > tried.index("diagonal") and "top-two" not in tried
    assert "顺延" in rotation.describe(selection) and "bottom-banner" in rotation.describe(selection)


def test_a_recent_template_is_taken_only_when_nothing_else_fits_and_that_is_recorded():
    def attempt(template):
        if template["template_id"] != "top-two":
            raise rotation.TemplateRefused("NO_ROOM")
        return {"punch": 230}

    _, selection = rotation.select(TABLE, SOLO, attempt=attempt, history=["top-two"])
    assert selection["template_id"] == "top-two" and selection["fallback"] == "recent_repeat_forced"
    with pytest.raises(rotation.TemplateRefused, match="NO_TEMPLATE_FITS"):
        rotation.select(TABLE, SOLO, attempt=lambda t: (_ for _ in ()).throw(rotation.TemplateRefused("NO_ROOM")))


def test_when_no_template_reaches_the_floor_the_largest_punchline_is_taken():
    sizes = {"column-left": 170, "top-two": 190}

    def attempt(template):
        return {"punch": sizes.get(template["template_id"], 150)}

    result, selection = rotation.select(TABLE, SOLO, attempt=attempt)
    assert selection["template_id"] == "top-two" and result["punch"] == 190 and selection["fallback"] == "punch_below_floor"


def test_collaboration_does_not_force_a_punchline_under_85_percent_of_floor():
    facts = dict(SOLO, people="panels", people_count=3, guest_line=False)
    with pytest.raises(rotation.TemplateRefused, match="punch_below_floor"):
        rotation.select(TABLE, facts, attempt=lambda _: {"punch": 169})

    for punch in (170, 190):
        result, selection = rotation.select(TABLE, facts, attempt=lambda _, p=punch: {"punch": p})
        assert result["punch"] == punch and selection["fallback"] == "punch_below_floor"

    # Per-template floor (wordy panels-bottom = 170px): 144px < 85% stops, 145px >= 85% succeeds.
    wordy = dict(facts, wordy=True)

    def bottom_only(p):
        def attempt(template):
            if template["template_id"] != "panels-bottom":
                raise rotation.TemplateRefused("NO_ROOM")
            return {"punch": p}
        return attempt

    with pytest.raises(rotation.TemplateRefused, match="punch_below_floor"):
        rotation.select(TABLE, wordy, attempt=bottom_only(144))
    res_wordy, sel_wordy = rotation.select(TABLE, wordy, attempt=bottom_only(145))
    assert sel_wordy["template_id"] == "panels-bottom" and res_wordy["punch"] == 145 and sel_wordy["fallback"] == "punch_below_floor"


def test_pack_collaboration_witness_takes_85_percent_below_floor_before_stopping():
    facts = dict(SOLO, people="panels", people_count=3, guest_line=True, lines=["前面一句", "七个字嘉宾原话"])
    attempted = []

    def attempt(template):
        attempted.append(template["template_id"])
        if template["template_id"] in {"collab-pair", "collab-dialogue"}:
            return {"punch": 178}
        return {"punch": 210}

    result, selection = rotation.select(TABLE, facts, attempt=attempt, history=["panels-top"], session_history=["panels-top"])
    assert selection["template_id"] in {"collab-pair", "collab-dialogue"}
    assert result["punch"] == 178
    assert selection["fallback"] == "punch_below_floor"
    assert "collab-portraits" not in attempted and "collab-stacked" not in attempted
    assert not {"panels-top", "panels-bottom", "panels-diagonal"}.intersection(attempted)
    reasons = {s["template_id"]: s["reason"] for s in selection["skipped"]}
    assert reasons["collab-portraits"].startswith("not_applicable:") and "嘉宾" in reasons["collab-portraits"]
    assert reasons["collab-stacked"].startswith("not_applicable:") and "嘉宾" in reasons["collab-stacked"]
    assert reasons["panels-top"] == "adjacent_family_forbidden"
    assert reasons["panels-bottom"] == "adjacent_family_forbidden"
    assert reasons["panels-diagonal"] == "adjacent_family_forbidden"
    assert "punch_below_floor" in rotation.describe(selection)

    # Fresh families beat recent ones in tier 2a; higher punch wins across tiers; tier 1b beats tier 2a.
    _, sel_fresh = rotation.select(TABLE, facts, attempt=lambda _: {"punch": 178},
                                   history=["collab-pair", "panels-top"], session_history=["panels-top"])
    assert sel_fresh["template_id"] == "collab-dialogue" and sel_fresh["fallback"] == "punch_below_floor"

    _, sel_bigger = rotation.select(
        TABLE, facts,
        attempt=lambda t: {"punch": 190 if t["template_id"] == "collab-pair" else 178},
        history=["collab-pair", "panels-top"], session_history=["panels-top"],
    )
    assert sel_bigger["template_id"] == "collab-pair" and sel_bigger["fallback"] == "punch_below_floor+recent_repeat_forced"

    _, sel_tier1b = rotation.select(
        TABLE, facts,
        attempt=lambda t: {"punch": 200 if t["template_id"] == "collab-pair" else 190},
        history=["collab-pair", "panels-top"], session_history=["panels-top"],
    )
    assert sel_tier1b["template_id"] == "collab-pair" and sel_tier1b["fallback"] == "recent_repeat_forced"


def test_pack_collaboration_falls_back_to_adjacent_family_least_recently_used_variant_first():
    facts = dict(SOLO, people="panels", people_count=3, guest_line=True, lines=["前面一句", "七个字嘉宾原话"])

    def under_85_attempt(template):
        if template["template_id"] == "collab-pair":
            return {"punch": 168}
        if template["template_id"] == "collab-dialogue":
            return {"punch": 160}
        return {"punch": 210}

    # Subcase A: all hashes prefer unused variants (panels-bottom / panels-diagonal) over just-used panels-top.
    for i in range(len(TABLE["templates"])):
        _, selection = rotation.select(TABLE, facts, attempt=under_85_attempt,
                                       history=["panels-top"], session_history=["panels-top"],
                                       content_sha256=f"{i:064x}")
        assert selection["template_id"] in {"panels-bottom", "panels-diagonal"}
        assert selection["fallback"] == "adjacent_repeat_forced"
        assert "adjacent_repeat_forced" in rotation.describe(selection)

    # Subcase B: least recently used variant of the adjacent family comes first.
    _, sel_lru = rotation.select(
        TABLE, facts,
        attempt=lambda t: {"punch": 160 if t["template_id"] in {"collab-pair", "collab-dialogue"} else 210},
        history=["panels-bottom", "panels-diagonal", "panels-top"], session_history=["panels-top"],
    )
    assert sel_lru["template_id"] == "panels-bottom" and sel_lru["fallback"] == "adjacent_repeat_forced"

    # Subcase C: adjacent variant below its floor is skipped; next variant at or above floor is taken.
    _, sel_skip_below = rotation.select(
        TABLE, facts,
        attempt=lambda t: {"punch": 160 if t["template_id"] in {"collab-pair", "collab-dialogue"}
                           else 190 if t["template_id"] == "panels-bottom"
                           else 205 if t["template_id"] == "panels-diagonal" else 220},
        history=["panels-bottom", "panels-diagonal", "panels-top"], session_history=["panels-top"],
    )
    assert sel_skip_below["template_id"] == "panels-diagonal" and sel_skip_below["fallback"] == "adjacent_repeat_forced"

    # Subcase D: 95% adjacent fallback (reviewer reproduction): LRU variant >= 85% of floor wins.
    res_adj_below, sel_adj_below = rotation.select(
        TABLE, facts,
        attempt=lambda t: {"punch": 160 if t["template_id"] in {"collab-pair", "collab-dialogue"} else 190},
        history=["panels-bottom", "panels-diagonal", "panels-top"], session_history=["panels-top"],
    )
    assert sel_adj_below["template_id"] == "panels-bottom" and res_adj_below["punch"] == 190
    assert sel_adj_below["fallback"] == "adjacent_repeat_forced+punch_below_floor"
    assert sel_adj_below["punch_px"] == 190 and sel_adj_below["punch_floor_px"] == 200
    assert "panels-bottom" not in {s["template_id"] for s in sel_adj_below["skipped"]}
    desc_adj_below = rotation.describe(sel_adj_below)
    assert desc_adj_below.startswith("构图样板 panels-bottom（adjacent_repeat_forced+punch_below_floor: 190px < 200px）")
    assert "panels-bottom（" not in desc_adj_below.split("；顺延：", 1)[-1]

    # Subcase D2: LRU variant beats larger-punch recent variant in tier 2c.
    _, sel_adj_below_lru = rotation.select(
        TABLE, facts,
        attempt=lambda t: {"punch": 160 if t["template_id"] in {"collab-pair", "collab-dialogue"}
                           else 175 if t["template_id"] == "panels-bottom"
                           else 160 if t["template_id"] == "panels-diagonal" else 195},
        history=["panels-top"], session_history=["panels-top"],
    )
    assert sel_adj_below_lru["template_id"] == "panels-bottom"
    assert sel_adj_below_lru["fallback"] == "adjacent_repeat_forced+punch_below_floor"
    assert sel_adj_below_lru["punch_px"] == 175 and sel_adj_below_lru["punch_floor_px"] == 200

    # Subcase E: when non-adjacent and all adjacent variants are <85% of floor, NO_TEMPLATE_FITS raises.
    with pytest.raises(rotation.TemplateRefused, match="NO_TEMPLATE_FITS"):
        rotation.select(
            TABLE, facts,
            attempt=lambda t: {"punch": 160 if t["template_id"] in {"collab-pair", "collab-dialogue"} else 169},
            history=["panels-bottom", "panels-diagonal", "panels-top"], session_history=["panels-top"],
        )

    # Subcase F: per-template wordy floor in tier 2c (panels-bottom floor 170px: 145px succeeds, 144px stops).
    wordy_facts = dict(facts, wordy=True)
    res_wordy_adj, sel_wordy_adj = rotation.select(
        TABLE, wordy_facts,
        attempt=lambda t: {"punch": 145 if t["template_id"] == "panels-bottom" else 160},
        history=["panels-top"], session_history=["panels-top"],
    )
    assert sel_wordy_adj["template_id"] == "panels-bottom" and res_wordy_adj["punch"] == 145
    assert sel_wordy_adj["fallback"] == "adjacent_repeat_forced+punch_below_floor"
    assert sel_wordy_adj["punch_px"] == 145 and sel_wordy_adj["punch_floor_px"] == 170
    with pytest.raises(rotation.TemplateRefused, match="NO_TEMPLATE_FITS"):
        rotation.select(
            TABLE, wordy_facts,
            attempt=lambda t: {"punch": 144 if t["template_id"] == "panels-bottom" else 160},
            history=["panels-top"], session_history=["panels-top"],
        )


def test_pack_collaboration_still_raises_when_nothing_drawable_and_keeps_not_applicable():
    facts = dict(SOLO, people="panels", people_count=3, guest_line=True, lines=["前面一句", "七个字嘉宾原话"])

    def attempt(template):
        assert template["template_id"] not in {"collab-portraits", "collab-stacked"}
        raise rotation.TemplateRefused("NO_ROOM")

    with pytest.raises(rotation.TemplateRefused, match="NO_TEMPLATE_FITS"):
        rotation.select(TABLE, facts, attempt=attempt, history=["panels-top"], session_history=["panels-top"])


@pytest.mark.parametrize("people", ["solo", "together"])
def test_non_pack_below_floor_still_takes_under_85_percent(people):
    facts = dict(SOLO, people=people)
    result, selection = rotation.select(
        TABLE, facts, attempt=lambda t: {"punch": 160 if t["template_id"] == "top-two" else 150}
    )
    assert selection["template_id"] == "top-two"
    assert result["punch"] == 160
    assert selection["fallback"] == "punch_below_floor"


def test_selected_template_is_omitted_from_skipped_and_records_punch_and_floor():
    panels_facts = dict(SOLO, people="panels", people_count=3, guest_line=True, lines=["前面一句", "七个字嘉宾原话"])
    cases = [
        (
            panels_facts,
            ["panels-top"],
            ["panels-top"],
            lambda t: {"punch": 178 if t["template_id"] in {"collab-pair", "collab-dialogue"} else 210},
            "punch_below_floor",
            178,
            200,
        ),
        (
            SOLO,
            ["top-two"],
            [],
            lambda t: {"punch": 230} if t["template_id"] == "top-two" else (_ for _ in ()).throw(rotation.TemplateRefused("NO_ROOM")),
            "recent_repeat_forced",
            230,
            200,
        ),
        (
            panels_facts,
            ["panels-top"],
            ["panels-top"],
            lambda t: {"punch": 160 if t["template_id"] in {"collab-pair", "collab-dialogue"} else 210},
            "adjacent_repeat_forced",
            210,
            200,
        ),
        (
            panels_facts,
            ["panels-bottom", "panels-diagonal", "panels-top"],
            ["panels-top"],
            lambda t: {"punch": 160 if t["template_id"] in {"collab-pair", "collab-dialogue"} else 190},
            "adjacent_repeat_forced+punch_below_floor",
            190,
            200,
        ),
    ]
    for facts, history, session_history, attempt, fallback, punch, floor in cases:
        _, sel = rotation.select(TABLE, facts, attempt=attempt, history=history, session_history=session_history)
        tid = sel["template_id"]
        assert sel["fallback"] == fallback
        assert sel["punch_px"] == punch and sel["punch_floor_px"] == floor
        assert tid not in {s["template_id"] for s in sel["skipped"]}
        desc = rotation.describe(sel)
        expected_fb = f"{fallback}: {punch}px < {floor}px" if punch < floor else fallback
        assert desc.startswith(f"构图样板 {tid}（{expected_fb}）")
        if "；顺延：" in desc:
            assert f"{tid}（" not in desc.split("；顺延：", 1)[1]


def test_a_bad_table_is_refused_not_defaulted(tmp_path):
    bad = json.loads(TABLE_PATH.read_text(encoding="utf-8"))
    bad["templates"][0]["rows"] = "column"
    bad["templates"][0]["text"] = "top"
    path = tmp_path / "t.json"
    path.write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(rotation.CoverLayoutTableError):
        rotation.load_table(path)
    with pytest.raises(rotation.CoverLayoutTableError):
        rotation.load_table(tmp_path / "missing.json")


def test_the_window_frames_her_as_the_template_says_and_never_cuts_the_face_off_the_cover():
    t = {x["template_id"]: x for x in TABLE["templates"]}
    face = [800, 440, 1190, 690]
    for tid in ("column-left", "column-right", "closeup-left", "closeup-right", "top-two", "diagonal"):
        made, why = rotation.window(face, 0, [], t[tid]["framing"], pad_side=t[tid]["text"] if t[tid]["framing"].get("panel") else None)
        assert why is None, tid
        assert 0 <= made["face"][0] and made["face"][2] <= 1920 and 0 <= made["face"][1] and made["face"][3] <= 1080, tid
    big = rotation.window(face, 0, [], t["closeup-left"]["framing"], pad_side="left")[0]
    small = rotation.window(face, 0, [], t["column-left"]["framing"], pad_side="left")[0]
    assert big["face_px"] > 1.4 * small["face_px"]                  # the close-up is a close-up


# ----- the skill renderer: the same table and selector ------------------------------------------------------------------
@pytest.fixture
def renderer():
    path = Path(os.environ.get("MANUAL_LANE_COVER_SKILL_SCRIPTS") or
                Path.home() / ".claude/skills/lidousha-title-cover/scripts") / "render_cover.py"
    if not (path.is_file() and FONT.is_file()):
        pytest.skip("title-cover skill renderer or private font not present")
    spec = importlib.util.spec_from_file_location("layout_tested_renderer", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.install_table(module.load_styles(STYLE_TABLE))
    return module


def _frame(tmp_path):
    frame = tmp_path / "frame.png"
    im = Image.new("RGB", (1920, 1080), (90, 140, 200))
    im.paste((230, 200, 180), (760, 300, 1160, 700))                 # where her face is said to be
    im.save(frame)
    return frame


@pytest.mark.parametrize("anchor", [260, 1660])
def test_actual_text_and_shadow_pixels_stay_in_the_safe_zone(renderer, tmp_path, anchor):
    spec = {"id": "safe-pixels", "frame": str(_frame(tmp_path)), "crop": [0, 0, 1920],
            "face": [700, 600, 1100, 950], "layout": "top",
            "lines": [{"text": "安晚每天来？", "at": anchor}, {"text": "你不能每天来", "at": anchor}]}
    before = renderer.background(spec).convert("RGB")
    image, _, _ = renderer.render(spec, str(FONT))
    pixels = ImageChops.difference(before, image.convert("RGB")).getbbox()
    assert pixels is not None and pixels[0] >= 260 and pixels[2] <= 1660


def test_manual_pixel_audit_replays_glyphs_background_and_rejects_changed_pixels(renderer, tmp_path):
    audit = _remote("cover_pixel_audit.py")
    spec = {"id": "pixel-replay", "frame": str(_frame(tmp_path)), "crop": [0, 0, 1920],
            "face": [700, 600, 1100, 950], "layout": "top",
            "lines": [{"text": "安晚每天来"}, {"text": "不能每天都来"}]}
    img, rows, _ = renderer.render(spec, str(FONT))
    before, mask, receipt = audit.audit(renderer, spec, rows, str(FONT), img)
    assert before.size == img.size == mask.size == (1920, 1080)
    assert receipt["status"] == "PASS" and 260 <= receipt["text_pixel_bbox"][0] < receipt["text_pixel_bbox"][2] <= 1660
    changed = img.copy()
    changed.putpixel((10, 10), (0, 0, 0, 255))
    with pytest.raises(ValueError, match="PIXEL_AUDIT_RECOMPOSITION_MISMATCH"):
        audit.audit(renderer, spec, rows, str(FONT), changed)


def test_consecutive_skill_covers_change_composition_where_auto_never_did(renderer, tmp_path):
    """The old renderer's `auto` took the largest punchline: one layout for a picture, cover after cover (on the lane's
    frames the top: 18 of 20).  With the table the renderer gives four covers in a row four compositions, none repeating
    the one before.  On the old renderer this fails: render() took no template history and every cover was `auto`."""
    renderer.load_layouts(TABLE_PATH)
    spec = {"id": "c", "frame": str(_frame(tmp_path)), "face_src": [760, 300, 1160, 700], "head_top_src": 220,
            "lines": [{"text": "打针不会哭"}, {"text": "无趣大人"}],
            "column_rows": [{"text": "打针", "punch": False}, {"text": "不会哭", "punch": False},
                            {"text": "无趣", "punch": True}, {"text": "大人", "punch": True}]}
    history, layouts = [], []
    for _ in range(4):
        _, rec, selection = renderer.render(json.loads(json.dumps(spec)), str(FONT), template_history=history,
                                            session_history=history)
        history.append(selection["cover_layout"]["template_id"])
        layouts.append(rec[-1]["layout"])
        assert max(line["size"] for line in rec) >= 120              # the hard floor; the words were never made smaller
    assert all(a != b for a, b in zip(history, history[1:])) and len(set(history)) == 4
    assert len(set(layouts)) >= 2 and layouts != ["top"] * 4
    # what every cover was before: `auto` takes the largest punchline, the same layout for the same picture every time
    auto = dict(spec, layout="auto", crop=[0, 0, 1920], face=[760, 300, 1160, 700])
    auto.pop("face_src")
    assert len({renderer.render(json.loads(json.dumps(auto)), str(FONT))[1][-1]["layout"] for _ in range(3)}) == 1


def test_the_skill_renderer_without_a_table_and_without_a_layout_stops(renderer, tmp_path):
    renderer.LAYOUTS = None
    with pytest.raises(SystemExit, match="LIDOUSHA_COVER_LAYOUTS"):
        renderer.render({"id": "c", "bg": ["#ffffff", "#eeeeee"], "face": [700, 500, 1100, 900],
                         "lines": [{"text": "甲"}, {"text": "乙"}]}, str(FONT))


# ----- the lane: history from the real cover records ---------------------------------------------------------------------
def test_the_lane_reads_each_earlier_covers_template_from_its_record():
    from src.autoslice.manual_lane.step6_cover import legacy_template, route_run

    assert legacy_template({"route": "screenshot", "layout": "top", "composition_key": ["screenshot", "top", "picture"]}) == "top-two"
    assert legacy_template({"route": "screenshot", "layout": "right", "composition_key": ["screenshot", "right", "picture_panel"]}) == "column-right"
    assert legacy_template({"route": "cutout", "layout": "top", "composition_key": ["cutout", "top", "peers_equal"]}) == "panels-top"
    assert legacy_template({"route": "ai_sticker", "layout": None, "composition_key": ["ai_sticker", "x", "y"]}) is None
    covers = [{"route": "emote"}, {"route": "screenshot"}, {"route": "screenshot"}]
    assert route_run(covers, 2) == "screenshot" and route_run(covers[:2], 2) is None and route_run(covers, 0) is None


def test_a_forced_template_repeat_and_a_third_route_in_a_row_are_recorded_for_every_cover():
    from src.autoslice.manual_lane.step6_cover import cover_repeats

    plan = {"rotation": {"route_run": "screenshot"}}
    forced = {"route": "emote", "cover_layout": {"template_id": "top-two", "fallback": "recent_repeat_forced",
                                                 "skipped": [{"template_id": "column-left", "reason": "does_not_fit: x"}]}}
    assert "top-two" in cover_repeats(forced, plan)
    # every template that applies was recent: still a repeat, recorded (it only ranks; review)
    alike = {"route": "emote", "cover_layout": {"template_id": "panels-top", "fallback": "recent_repeat_forced",
                                                "skipped": [{"template_id": "panels-bottom", "reason": "recent_used"}]}}
    assert "适用的样板最近几张都用过" in cover_repeats(alike, plan)
    assert "screenshot" in cover_repeats({"route": "screenshot", "cover_layout": {}}, plan)
    assert cover_repeats({"route": "emote", "cover_layout": {"template_id": "column-left", "fallback": None}}, plan) is None
    # review: a cover of several people repeating panels-top five times left no record at all; it is a repeat
    # like any other (ranked behind, written to PROCESS), for the split screen and for people side by side
    split = {"route": "emote", "cover_layout": {"template_id": "panels-top", "fallback": "recent_repeat_forced",
                                                "facts": {"people": "panels"},
                                                "skipped": [{"template_id": "panels-bottom", "reason": "does_not_fit: x"}]}}
    assert "panels-top" in cover_repeats(split, plan)
    together = {"route": "emote", "cover_layout": dict(forced["cover_layout"], facts={"people": "together"})}
    assert "top-two" in cover_repeats(together, plan)


def test_cover_repeats_and_pick_handle_new_pack_fallbacks():
    from src.autoslice.manual_lane.cover_pick import candidate_record, rank
    from src.autoslice.manual_lane.step6_cover import cover_repeats

    plan = {"rotation": {"route_run": None}}
    adj = {"route": "screenshot", "cover_layout": {
        "template_id": "panels-bottom", "fallback": "adjacent_repeat_forced",
        "skipped": [{"template_id": "collab-pair", "reason": "punch_below_floor: 160px < 200px"},
                    {"template_id": "panels-top", "reason": "adjacent_family_forbidden"}],
    }}
    rep_adj = cover_repeats(adj, plan)
    assert rep_adj is not None and "panels-bottom" in rep_adj and "collab-pair" in rep_adj

    adj_below = {"route": "screenshot", "cover_layout": {
        "template_id": "panels-bottom", "fallback": "adjacent_repeat_forced+punch_below_floor",
        "skipped": [{"template_id": "collab-pair", "reason": "punch_below_floor: 160px < 200px"}],
    }}
    rep_adj_below = cover_repeats(adj_below, plan)
    assert rep_adj_below is not None and "panels-bottom" in rep_adj_below and "collab-pair" in rep_adj_below

    below_fresh = {"route": "screenshot", "cover_layout": {
        "template_id": "collab-pair", "fallback": "punch_below_floor",
        "skipped": [{"template_id": "collab-pair", "reason": "punch_below_floor: 178px < 200px"}],
    }}
    assert cover_repeats(below_fresh, plan) is None

    below_recent = {"route": "screenshot", "cover_layout": {
        "template_id": "collab-pair", "fallback": "punch_below_floor+recent_repeat_forced",
        "skipped": [{"template_id": "collab-pair", "reason": "punch_below_floor: 180px < 200px"},
                    {"template_id": "collab-dialogue", "reason": "does_not_fit: x"}],
    }}
    rep_recent = cover_repeats(below_recent, plan)
    assert rep_recent is not None and "collab-dialogue" in rep_recent and "collab-pair 这张放不下" not in rep_recent

    for row_in in (
        {"id": "a", "route": "screenshot", "status": "RENDERED", "punch_font_size": 178, "punch_floor_px": 200,
         "punch_below_floor": True, "cover_layout": below_fresh["cover_layout"],
         "cover_layout_note": "构图样板 collab-pair（punch_below_floor）", "score": {"status": "ELIGIBLE", "total": 10, "C": 2}},
        {"id": "b", "route": "screenshot", "status": "RENDERED", "punch_font_size": 210, "punch_floor_px": 200,
         "punch_below_floor": False, "cover_layout": adj["cover_layout"],
         "cover_layout_note": "构图样板 panels-bottom（adjacent_repeat_forced）", "repeat": rep_adj,
         "score": {"status": "ELIGIBLE", "total": 12, "C": 2}},
    ):
        rec = candidate_record(row_in)
        assert rec["punch_below_floor"] is row_in["punch_below_floor"]
        assert rec["cover_layout"] == row_in["cover_layout"]
        assert rec["cover_layout_note"] == row_in["cover_layout_note"]

    eligible_adj = {"id": "adj", "route": "screenshot", "face_status": "MEASURED", "face_px": 450,
                    "repeat": rep_adj, "score": {"status": "ELIGIBLE", "total": 14, "C": 2}}
    eligible_fresh = {"id": "fresh", "route": "emote", "face_status": "MEASURED", "face_px": 450,
                      "repeat": None, "score": {"status": "ELIGIBLE", "total": 10, "C": 2}}
    assert [r["id"] for r in rank([eligible_adj], repeats=lambda r: bool(r.get("repeat")), target_px=464)] == ["adj"]
    assert [r["id"] for r in rank([eligible_adj, eligible_fresh], repeats=lambda r: bool(r.get("repeat")), target_px=464)] == ["fresh", "adj"]


def _cut_emote(tmp_path, bottom):
    """A 400 px picture of a head-dominant figure whose alpha runs down to row `bottom` (399: the picture cuts her flat
    at its bottom, like an emote cut at the waist); her face is 200 px of its 300 px."""
    path = tmp_path / f"cut-{bottom}.png"
    im = Image.new("RGBA", (400, 400), (0, 0, 0, 0))
    im.paste((150, 160, 200, 255), (100, 100, 300, bottom + 1))
    im.save(path)
    return str(path), [130, 130, 270, 330]


def test_a_figure_cut_at_its_bottom_is_not_left_floating_inside_the_canvas(tmp_path):
    """review: the diagonal and banner templates place her face high and small; an emote cut at the waist then
    ended in a straight white-outlined edge across the middle of the cover (replay 10, emote, the punchline under it).
    The layout refuses that placement (the template is passed over); an uncut figure may stand anywhere."""
    layout = _remote("cover_layout.py")
    rules = {k: v for k, v in _lane_rules().items() if not (k.startswith("_") or k.endswith("_source") or k == "source")}
    renderer = type("R", (), {"cutout": staticmethod(lambda raw, mode: raw.convert("RGBA"))})
    diagonal = next(t for t in TABLE["templates"] if t["template_id"] == "diagonal")["framing"]
    cut, face = _cut_emote(tmp_path, 399)
    with pytest.raises(ValueError, match="FIGURE_CUT_ENDS_ABOVE_FRAME_BOTTOM"):
        layout.place_solo({"src": cut, "cutout": "alpha"}, face, renderer, rules, None, "right", diagonal["face_x_frac"],
                          diagonal["face_frac"], diagonal["face_top_frac"] * 1080)
    whole, face = _cut_emote(tmp_path, 380)                     # the same figure, whole: it may stand in the middle
    sticker, box, plan = layout.place_solo({"src": whole, "cutout": "alpha"}, face, renderer, rules, None, "right",
                                           diagonal["face_x_frac"], diagonal["face_frac"], diagonal["face_top_frac"] * 1080)
    assert sticker["y"] + sticker["h"] < 1080


def test_the_skill_renderer_refuses_a_template_that_leaves_a_bottom_cut_inside(renderer, tmp_path):
    renderer.load_layouts(TABLE_PATH)
    banner = next(t for t in TABLE["templates"] if t["template_id"] == "bottom-banner")["framing"]
    cut, face = _cut_emote(tmp_path, 399)
    with pytest.raises(renderer.LAYOUT.TemplateRefused, match="FIGURE_CUT_ENDS_ABOVE_FRAME_BOTTOM"):
        renderer._place_sticker({"src": cut, "cutout": "alpha", "face_src": face}, banner)


def test_the_lane_stops_before_it_sends_a_renderer_older_than_the_table(tmp_path):
    """review: the lane and the skill are installed apart; a renderer without the bottom layout stopped the
    media-host tool with a KeyError at the first bottom template.  The lane checks the renderer's LAYOUT_SCHEMA first."""
    from src.autoslice.manual_lane.step6_cover import renderer_outdated

    old = tmp_path / "render_cover.py"
    old.write_text("SCHEMA = 'lidousha-cover-style-rotation.v1'\n", encoding="utf-8")
    assert "LAYOUT_SCHEMA" in renderer_outdated(old) and "lidousha-title-cover" in renderer_outdated(old)
    old.write_text("LAYOUT_SCHEMA = 'cover-layout-templates.v0'\n", encoding="utf-8")
    assert "v0" in renderer_outdated(old)
    new = tmp_path / "render_new.py"
    new.write_text(f"LAYOUT_SCHEMA = '{rotation.SCHEMA}'\n", encoding="utf-8")
    assert renderer_outdated(new) is None
    skill = Path(os.environ.get("MANUAL_LANE_COVER_SKILL_SCRIPTS") or
                 Path.home() / ".claude/skills/lidousha-title-cover/scripts") / "render_cover.py"
    if skill.is_file():
        assert renderer_outdated(skill) is None, "the skill renderer the tests run against must declare the table's schema"



def _installed_as(tmp_path, shape: str) -> Path:
    """A skill named some-title-cover in one of the two shapes it is installed in, its renderer older than the table:
    a repository checkout (<skills>/some-title-cover/scripts), or a versioned install as
    scripts/deploy_oci3_title_skill.py install makes it (<parent>/some-title-cover -> versions/<content hash>, with
    SOURCE.json), reached through a link the way a CI run directory reaches it (skills -> the install's parent)."""
    if shape == "checkout":
        scripts = tmp_path / "repo/skills/some-title-cover/scripts"
        scripts.mkdir(parents=True)
    else:
        version = tmp_path / "title-skills/abc/versions" / ("9" * 64)
        (version / "scripts").mkdir(parents=True)
        (version / "SOURCE.json").write_text(json.dumps({"schema_version": "title-skill-source.v1", "commit": "c" * 40,
                                                         "files": {}}), encoding="utf-8")
        (tmp_path / "title-skills/abc/some-title-cover").symlink_to(Path("versions") / ("9" * 64))
        (tmp_path / "run").mkdir()
        (tmp_path / "run/skills").symlink_to(tmp_path / "title-skills/abc")
        scripts = tmp_path / "run/skills/some-title-cover/scripts"
    (scripts / "render_cover.py").write_text("LAYOUT_SCHEMA = 'cover-layout-templates.v0'\n", encoding="utf-8")
    return scripts


@pytest.mark.parametrize("shape", ["checkout", "versioned"])
def test_a_stale_renderer_says_which_skill_and_version_to_install_however_the_skill_is_installed(tmp_path, monkeypatch,
                                                                                                    shape):
    """Integration 11-25: on the hosts the skill is a versioned install, its configured directory resolves to
    versions/<content hash>, and the stop told the operator to install "the 2a9e0d... skill".  The name is read from
    the path as configured, the installed version from the install's SOURCE.json, the version wanted from the repo's pin."""
    from src.autoslice.manual_lane import layout as layout_mod
    from src.autoslice.manual_lane import step6_cover

    scripts = _installed_as(tmp_path, shape)
    configured = os.path.relpath(scripts, REPO)          # relative, as the profiles write it: the layout resolves it
    lane = layout_mod.load_layout({**os.environ, "MANUAL_LANE_COVER_SKILL_SCRIPTS": configured})
    monkeypatch.setattr(step6_cover, "layout", lambda: lane)
    assert lane.cover_skill_name == "some-title-cover"
    assert ("9" * 64 in str(lane.cover_skill_scripts)) is (shape == "versioned")     # resolved, it is the hash
    why = step6_cover.renderer_outdated(scripts / "render_cover.py")
    assert "先装新版 some-title-cover 技能" in why and "9" * 64 + " 技能" not in why
    assert configured in why
    if (REPO / "scripts/export_oss_snapshot.py").is_file():
        pin = json.loads((REPO / "config/title_skill_source.json").read_text(encoding="utf-8"))["agent_toolkit_commit"]
        assert pin in why and "config/title_skill_source.json" in why
    else:
        assert "本仓库没有钉技能提交" in why
    assert ("那里装的是提交 " + "c" * 40 in why) is (shape == "versioned")
    assert ("没有 SOURCE.json" in why) is (shape == "checkout")

def test_clips_made_at_once_choose_their_covers_one_after_another(tmp_path):
    """review: a run makes its clips in threads at once (multi_run); four clips chose their covers within a
    minute of each other, each read the same history (the cover records already written) and would take the same first
    template.  Step 6 holds the batch's cover lock from reading the history to writing the cover record, so the next
    clip sees the one before it."""
    import inspect
    import threading
    import time
    from types import SimpleNamespace

    from src.autoslice.manual_lane import step6_title
    from src.autoslice.manual_lane.step6_cover import _lane_covers

    task = tmp_path / "task"
    (task / "state").mkdir(parents=True)
    taken: list[str] = []

    def clip(cid: str) -> None:
        clip_dir = task / "clips" / cid
        (clip_dir / "title").mkdir(parents=True)
        ctx = SimpleNamespace(clip_dir=clip_dir, state=SimpleNamespace(path=task / "state" / f"{cid}.json"))
        with step6_title.cover_batch_lock(ctx):
            history = [c["template_id"] for c in _lane_covers(ctx)]
            fresh, _recent, _ = rotation.order(TABLE, SOLO, history, history)
            time.sleep(0.2)                                          # the cover being made (minutes in production)
            chosen = {"route": "screenshot", "template_id": fresh[0]["template_id"]}
            (clip_dir / "title/candidates.json").write_text(json.dumps({"cover_route": {
                "method": "skill.candidates", "at": f"2026-11-25T08:{len(taken):02d}:00Z", "chosen": chosen}}), encoding="utf-8")
            taken.append(chosen["template_id"])

    threads = [threading.Thread(target=clip, args=(f"c{i}",)) for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(set(taken)) == 3
    # and step 6 does make the cover under that lock: the plan (which reads the history) is inside _cover_held
    assert "with cover_batch_lock(ctx):" in inspect.getsource(step6_title.step6_title_cover)
    assert "cover_plan(ctx, cover_lines, brief, names)" in inspect.getsource(step6_title._cover_held)
    assert "cover_plan(" not in inspect.getsource(step6_title.step6_title_cover)


def test_a_cut_out_whose_placement_one_template_refuses_tries_the_next():
    """replay: the cut-out reports its layers' failure under PEOPLE_LAYERS_UNAVAILABLE, a failure no template
    changes; a bottom cut left inside the canvas is the template's, so the next template is tried (the route was dropped
    after its first template)."""
    from types import SimpleNamespace

    compose = _remote("cover_compose.py")

    class Run:
        def __init__(self, failure):
            self.column, self.job, self.table = SimpleNamespace(template=None, rows=[]), {}, TABLE
            self.specs, self.failed, self.rows, self.layouts, self.pack, self.guest_boxes = [], [], [], {}, [], []
            self.lines = [{"text": "打针不会哭"}, {"text": "无趣大人"}]
            self.first, self.failure = None, failure

        def fail(self, route, code):
            self.failed.append({"id": route, "route": route, "failure": code})

        def draw(self, route, spec):
            return {"status": "RENDERED", "punch_font_size": 220, "route": route}

        def judge_row(self, row, *item):
            return row

        def make(self):
            self.first = self.first or self.column.template["template_id"]
            if self.column.template["template_id"] == self.first:
                self.fail("cutout", self.failure)
            else:
                self.specs.append(("cutout", {}))

    run = Run("PEOPLE_LAYERS_UNAVAILABLE:FIGURE_CUT_ENDS_ABOVE_FRAME_BOTTOM")
    compose.compose(run, "cutout", run.make)
    taken = run.rows[0]["cover_layout"]
    assert taken["template_id"] != run.first
    assert {"template_id": run.first, "reason": "does_not_fit: PEOPLE_LAYERS_UNAVAILABLE:FIGURE_CUT_ENDS_ABOVE_FRAME_BOTTOM"} in taken["skipped"]
    dead = Run("PEOPLE_LAYERS_UNAVAILABLE")                  # no layers at all: no template helps, the route fails at once
    compose.compose(dead, "cutout", dead.make)
    assert not dead.rows and dead.layouts["cutout"]["tried"] == [dead.first]
