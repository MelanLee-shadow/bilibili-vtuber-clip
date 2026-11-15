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
