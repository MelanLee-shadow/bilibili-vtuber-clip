"""The one place texts are compared by their letters (text_overlap)."""

from __future__ import annotations

import re
from pathlib import Path

from src.autoslice.manual_lane import text_overlap

LANE = Path(__file__).resolve().parents[2] / "src/autoslice/manual_lane"


def test_letters_and_key_keep_only_letters_and_digits() -> None:
    assert text_overlap.letters("谢谢 SAVIOR-in-the_dark 的灯牌！×2") == "谢谢SAVIORinthedark的灯牌2"
    assert text_overlap.key("谢谢 SAVIOR-in-the_dark 的灯牌！×2") == "谢谢saviorinthedark的灯牌2"
    assert text_overlap.key("") == "" and text_overlap.key("，。… \n") == ""


def test_key_is_what_the_removed_pattern_gave() -> None:
    for text in ("Hello, World_1!", "她说：“kmx，晚上好～”", "ありがとう！ thank_you 100%", "A——B……C", "１２３ ｆｕｌｌ"):
        assert text_overlap.key(text) == re.sub(r"[\W_]+", "", text).lower()


def test_shared_counts_letters_found_in_order() -> None:
    assert text_overlap.shared("谢谢你呀", "谢谢你ありがとう") == 3
    assert text_overlap.shared("甲乙丙丁", "甲x乙丙y丁") == 4
    assert text_overlap.shared("甲乙丙丁", "甲x乙丙y丁", 2) == 2          # single letters are chance
    assert text_overlap.shared("", "甲") == 0 and text_overlap.shared("甲", "") == 0


def test_share_is_of_the_first_text() -> None:
    assert text_overlap.share("谢谢你", "谢谢你ありがとう") == 1.0
    assert text_overlap.share("谢谢你呀", "谢谢你") == 0.75
    assert text_overlap.share("", "谢谢") == 1.0 and text_overlap.share("", "谢谢", empty=0.0) == 0.0
    assert text_overlap.share("大家好", "", empty=0.0) == 0.0


def test_no_step_keeps_its_own_copy() -> None:
    """A module that strips a text to its letters itself is a second implementation: it calls text_overlap."""
    own = re.compile(r"if ch\.isalnum\(\)\)(\.lower\(\))?$|re\.sub\(r\"\[\\W_\]\+\", \"\", ")
    found = [f"{path.name}:{n}" for path in sorted(LANE.glob("*.py")) if path.name not in ("text_overlap.py", "title_history.py")
             for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1) if own.search(line.strip())]
    assert found == []
