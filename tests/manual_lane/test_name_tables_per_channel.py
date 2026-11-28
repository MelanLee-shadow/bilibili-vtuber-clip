"""A channel's clips are written with its own names (owner 不愧是礼墨Sumi — 根据语境、根据这个写法…流水线也应该能做到).

- the name tables (proper_nouns.json, mishearing_pairs.json) are the active channel's, never another channel's;
- a full name made of a Chinese part and a Latin part is hinted when a recogniser wrote the Chinese part in loosely
  alike characters right beside the Latin part (or beside a syllable that begins it)."""
from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from src.autoslice.manual_lane import channel, name_match
from src.autoslice.manual_lane.lane_rules import rules

ROOT = Path(__file__).resolve().parents[2]
NAMES = [("礼墨Sumi", ("礼墨", "Sumi")), ("伊索尔Sol", ("Sol",)), ("李豆沙", ("小李",))]


def test_the_default_channel_keeps_its_tables_where_its_spelling_table_is():
    assert channel.name_table("proper_nouns.json") == ROOT / "assets/manual_lane/proper_nouns.json"
    assert channel.name_table("mishearing_pairs.json") == ROOT / "assets/manual_lane/mishearing_pairs.json"


def test_a_channel_without_a_spelling_table_reads_its_own_asset_directory(monkeypatch, tmp_path):
    other = dataclasses.replace(channel.profile(), spelling_table_path=None)
    monkeypatch.setattr(channel, "profile", lambda: other)
    monkeypatch.setattr(channel, "ASSETS", tmp_path / "assets/other")
    assert channel.name_table("proper_nouns.json") == tmp_path / "assets/other/proper_nouns.json"


@pytest.mark.parametrize("heard", ["不愧是你们苏菲", "不愧是你们苏", "不愧是你们Sumi", "不愧是你们 sumi"])
def test_the_chinese_part_heard_loosely_beside_the_latin_part_hints_the_full_name(heard):
    assert name_match.near_full_names(heard, NAMES) == ["礼墨Sumi"]


@pytest.mark.parametrize("heard", ["你们好", "你们说什么", "你们是谁", "不愧是礼墨Sumi", "李墨是谁"])
def test_the_loose_sound_alone_or_the_name_itself_is_no_hint(heard):
    assert name_match.near_full_names(heard, NAMES) == []


def test_another_member_of_the_class_a_colleague_written_chinese_then_latin():
    assert name_match.near_full_names("今天一所儿Sol也来了", NAMES) == ["伊索尔Sol"]


def test_the_hint_reaches_the_name_doubt_reasons():
    reasons = name_match.detection_reasons("不愧是你们苏菲", NAMES, {"near_name": True, "latin_unknown": False})
    assert any("近音全名" in r and "礼墨Sumi" in r for r in reasons)
    assert rules("detection")["near_name"] is True


@pytest.mark.parametrize("heard, name", [("我说看到阿娃娃在直播睡觉", "安晚awa"), ("苏米你来啦", "礼墨Sumi")])
def test_a_latin_only_name_sounded_out_in_chinese_characters_is_hinted(heard, name):
    # owner awawa 是安晚awa 的昵称 — three recognisers wrote 阿娃娃 (a-wa-wa) and nothing pointed at the name
    assert name in name_match.near_names(heard, [*NAMES, ("安晚awa", ("安晚", "awawa"))])


@pytest.mark.parametrize("heard", ["阿娃在吗", "你说什么呀", "苏醒了"])
def test_a_shorter_or_unrelated_run_does_not_sound_out_the_latin_name(heard):
    assert name_match.near_names(heard, [*NAMES, ("安晚awa", ("安晚", "awawa"))]) == []


@pytest.mark.parametrize("heard, name", [
    ("我跟东离说好了", "东爱璃"),
    ("找沙莉聊聊", "沙豆李"),
    ("明天看日房邹吧", "明日方舟"),
    ("民方奏新活动", "明日方舟"),
])
def test_dropped_syllable_with_blur_hints_three_or_more_syllable_names(heard, name):
    assert name_match.near_names(heard, [(name, ())]) == [name]


@pytest.mark.parametrize("heard, name", [
    ("刚才轩叔来了", "萱师傅"),
    ("提到玄素了", "萱师傅"),
    ("好像红欣来了", "红晓音"),
    ("这是衣索吗", "伊索尔"),
    ("今天戴丽不在", "东爱璃"),
    ("我跟东离说好了", "东爱璃"),
])
def test_fused_adjacent_syllables_with_blur_hint_names(heard, name):
    assert name_match.near_names(heard, [(name, ())]) == [name]


def test_short_names_and_split_han_names_do_not_get_drop_or_fuse_tolerance():
    assert name_match.near_names("起乐", [("绮罗", ())]) == []
    assert name_match.near_names("大", [("大璃", ())]) == []
    assert name_match.near_names("去哪", [("南町", ())]) == []
    assert name_match.near_names("问问老师", [("大N老师", ())]) == []
    assert name_match.near_names("七月再说", [("契约do爱", ())]) == []


@pytest.mark.parametrize("heard, names", [
    ("你是谁呀", [("露蒂丝", ())]),
    ("想带你出去玩", [("东爱璃", ())]),
    ("双马尾很好看", [("马有利", ())]),
    ("新加坡国立大学", [("李豆沙", ())]),
    ("好好听听这首歌", [("汀汀汀", ())]),
    ("跟马有利打到一起", [("马有利", ()), ("李豆沙", ())]),
    ("第四本刚出", [("露蒂丝", ())]),
    ("建议去看看", [("三剪一", ())]),
    ("我跟东爱璃说", [("东爱璃", ())]),
])
def test_guards_suppress_common_words_edge_drops_and_written_overlaps(heard, names):
    assert name_match.near_names(heard, names) == []


def test_entries_loads_three_plus_han_roster_and_shared_terms_and_reaches_detection(monkeypatch, tmp_path):
    roster = tmp_path / "roster.json"
    roster.write_text(
        '{"member_overrides": {"m1": {"canonical": "萱萱卡娅", "aliases": ["Kaya", "萱师傅"]}, '
        '"m2": {"canonical": "亦枝", "aliases": ["这是亦枝YY", "YY"]}}}',
        encoding="utf-8",
    )
    shared = tmp_path / "nodes.jsonl"
    shared.write_text(
        '{"id": "term:三剪一", "kind": "term", "names": {"display": "三剪一"}, '
        '"aliases": ["3剪1"], "status": "accepted"}\n'
        '{"id": "term:旧词", "kind": "term", "names": {"display": "旧词已退"}, '
        '"aliases": [], "status": "retired"}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(name_match, "ROSTER_SOURCES", roster)
    monkeypatch.setattr(name_match, "SHARED_NODES", shared)
    name_match._roster_entries.cache_clear()
    name_match._shared_entries.cache_clear()
    try:
        loaded = name_match.entries([["萱萱卡娅", "小卡"], ["连线"]])
        by_name = dict(loaded)
        assert "萱师傅" in by_name["萱萱卡娅"]
        assert "小卡" in by_name["萱萱卡娅"]
        assert "亦枝" not in by_name
        assert "这是亦枝YY" in by_name
        assert "三剪一" in by_name
        assert "旧词已退" not in by_name
        assert name_match.near_names("刚才轩叔来了", loaded) == ["萱萱卡娅"]
        reasons = name_match.detection_reasons("刚才轩叔来了", loaded, {"near_name": True, "latin_unknown": False})
        assert any("近音名字" in r and "萱萱卡娅" in r for r in reasons)

        monkeypatch.setattr(name_match, "ROSTER_SOURCES", None)
        monkeypatch.setattr(name_match, "SHARED_NODES", tmp_path / "missing.jsonl")
        name_match._roster_entries.cache_clear()
        name_match._shared_entries.cache_clear()
        fallback = name_match.entries([])
        assert "萱萱卡娅" not in dict(fallback)
    finally:
        name_match._roster_entries.cache_clear()
        name_match._shared_entries.cache_clear()


def test_written_name_at_one_position_does_not_suppress_candidate_elsewhere(tmp_path, monkeypatch):
    # Ruling 3: position-based coverage in both near_names passes
    assert name_match.near_names("东爱璃来了", [("东爱璃", ())]) == []
    assert name_match.near_names("东爱璃来了，后来东离走了", [("东爱璃", ())]) == ["东爱璃"]

    shared = tmp_path / "nodes.jsonl"
    shared.write_text(
        '{"id": "yizhi", "kind": "term", "names": {"display": "这是亦枝YY"}, "aliases": ["亦枝"]}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(name_match, "ROSTER_SOURCES", None)
    monkeypatch.setattr(name_match, "SHARED_NODES", shared)
    loaded = name_match.entries([["义之"]])
    assert name_match.near_names("这是亦枝YY来了", loaded) == []
    assert name_match.near_names("这是亦枝YY来了，后来又谈到了亦枝", loaded) == ["义之"]
    assert name_match.near_names("你豆沙", [("李豆沙", ("豆沙",))]) == ["李豆沙"]


def test_weak_sources_full_length_homophones_guard_common_and_repeated_char_spans(tmp_path, monkeypatch):
    # Ruling 4a: roster/shared full-length homophones use common-word & single-repeated-char guards; table/participant unchanged
    roster = tmp_path / "roster.json"
    roster.write_text(
        '{"member_overrides": {"m1": {"canonical": "廷亭庭", "aliases": []}, '
        '"m2": {"canonical": "可不石", "aliases": []}}}',
        encoding="utf-8",
    )
    shared = tmp_path / "nodes.jsonl"
    shared.write_text(
        '{"id": "s1", "kind": "term", "names": {"display": "三剪一"}, "aliases": []}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(name_match, "ROSTER_SOURCES", roster)
    monkeypatch.setattr(name_match, "SHARED_NODES", shared)
    loaded = name_match.entries([])
    assert "廷亭庭" not in name_match.near_names("等一下停停停", loaded)
    assert "可不石" not in name_match.near_names("这可不是吗", loaded)
    assert "三剪一" in name_match.near_names("这叫三减一", loaded)
    # Table/participant homophone rule is unchanged even when spanning common words
    assert name_match.near_names("这可不是吗", [("可不石", ())]) == ["可不石"]


def test_overlapping_roster_entry_merges_into_participant_keeping_participant_name(tmp_path, monkeypatch):
    # Ruling 6: sharing any spelling with a table/participant row merges 3+ Han forms into that row
    roster = tmp_path / "roster.json"
    roster.write_text(
        '{"member_overrides": {"kaya": {"canonical": "萱萱卡娅", "aliases": ["萱师傅", "萱萱", "萱萱卡娅Kaya"]}}}',
        encoding="utf-8",
    )
    monkeypatch.setattr(name_match, "ROSTER_SOURCES", roster)
    monkeypatch.setattr(name_match, "SHARED_NODES", tmp_path / "missing.jsonl")
    loaded = name_match.entries([["萱师傅"]])
    by_name = dict(loaded)
    assert "萱师傅" in by_name
    assert "萱萱卡娅" not in by_name
    assert "萱萱卡娅" in by_name["萱师傅"]
    assert "萱萱卡娅Kaya" in by_name["萱师傅"]
    assert "萱萱" not in by_name["萱师傅"]
    assert name_match.entry_source(loaded, "萱师傅") == "participant"
    assert name_match.near_names("欢迎宣萱卡雅", loaded) == ["萱师傅"]
    reasons = name_match.detection_reasons("欢迎宣萱卡雅", loaded, {"near_name": True, "latin_unknown": False})
    assert any(r.startswith("近音名字：") and "萱师傅" in r for r in reasons)


def test_roster_and_shared_latin_forms_do_not_hide_unknown_latin(tmp_path, monkeypatch):
    # Ruling 7: unknown_latin / unknown_line only consider table + participants as known Latin
    roster = tmp_path / "roster.json"
    roster.write_text(
        '{"member_overrides": {"kaya": {"canonical": "萱萱卡娅", "aliases": ["Kaya", "萱萱卡娅Kaya"]}}}',
        encoding="utf-8",
    )
    shared = tmp_path / "nodes.jsonl"
    shared.write_text(
        '{"id": "n1", "kind": "term", "names": {"display": "这是亦枝YY"}, "aliases": ["YYDS"]}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(name_match, "ROSTER_SOURCES", roster)
    monkeypatch.setattr(name_match, "SHARED_NODES", shared)
    loaded = name_match.entries([])
    assert "萱萱卡娅" in dict(loaded) and "这是亦枝YY" in dict(loaded)
    assert name_match.unknown_latin("这是Kaya和YY和YYDS", loaded) == ["Kaya", "YY", "YYDS"]
    assert "Kaya" in name_match.unknown_line(["这是Kaya"], [], [])
    with_participant = name_match.entries([["萱师傅", "Kaya"]])
    assert "Kaya" not in name_match.unknown_latin("这是Kaya", with_participant)


def test_malformed_roster_or_shared_raises_value_error_naming_file_and_creates_late(tmp_path, monkeypatch):
    # Ruling 8: wrong shape raises ValueError naming the file; missing files are not cached as empty forever
    bad_roster = tmp_path / "bad_roster.json"
    bad_roster.write_text('{"member_overrides": ["not-a-dict"]}', encoding="utf-8")
    monkeypatch.setattr(name_match, "ROSTER_SOURCES", bad_roster)
    monkeypatch.setattr(name_match, "SHARED_NODES", tmp_path / "missing.jsonl")
    with pytest.raises(ValueError, match="bad_roster\\.json"):
        name_match.entries([])

    bad_shared = tmp_path / "bad_nodes.jsonl"
    bad_shared.write_text('["not-an-object"]\n', encoding="utf-8")
    monkeypatch.setattr(name_match, "ROSTER_SOURCES", None)
    monkeypatch.setattr(name_match, "SHARED_NODES", bad_shared)
    with pytest.raises(ValueError, match="bad_nodes\\.jsonl"):
        name_match.entries([])

    late_shared = tmp_path / "late_nodes.jsonl"
    monkeypatch.setattr(name_match, "SHARED_NODES", late_shared)
    assert "晚来新词" not in dict(name_match.entries([]))
    late_shared.write_text(
        '{"id": "late", "kind": "term", "names": {"display": "晚来新词"}, "aliases": []}\n',
        encoding="utf-8",
    )
    assert "晚来新词" in dict(name_match.entries([]))


def test_default_roster_path_only_ignores_channel_profile_error(monkeypatch):
    # Ruling 8: catch ChannelProfileError, never AttributeError or unrelated KeyError/RuntimeError
    from src.autoslice.channel_profile import ChannelProfileError

    class _MissingAssetProfile:
        def asset_file(self, _key):
            raise ChannelProfileError("no roster")

    monkeypatch.setattr(channel, "profile", lambda: _MissingAssetProfile())
    assert name_match._default_roster_path() is None

    class _BrokenProfile:
        def asset_file(self, _key):
            raise KeyError("unexpected bug")

    monkeypatch.setattr(channel, "profile", lambda: _BrokenProfile())
    with pytest.raises(KeyError):
        name_match._default_roster_path()
