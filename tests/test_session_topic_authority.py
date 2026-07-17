from pathlib import Path

from src.autoslice.chat_authority import normalize_code_switch_surfaces
from src.autoslice.session_topic_authority import (
    absorb_session_topic_entities,
    discover_session_topic_authorities,
)


def _srt(*texts: str) -> str:
    return "\n\n".join(
        f"{index}\n00:00:{index:02d},000 --> 00:00:{index + 1:02d},000\n{text}"
        for index, text in enumerate(texts, start=1)
    ) + "\n"


def test_full_session_title_chat_authority_absorbs_all_close_name_variants(
    tmp_path: Path,
):
    xml = tmp_path / "session.xml"
    xml.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<i>
  <metadata><room_title>一起仗剑传说！</room_title></metadata>
  <d p="1717.786,1,25">我们杖剑传说是这种游戏吗</d>
</i>
""",
        encoding="utf-8",
    )
    spec = {"pieces": [{"danmaku_xml_local": str(xml)}]}

    authorities = discover_session_topic_authorities(spec)

    assert [row["canonical"] for row in authorities] == ["杖剑传说"]
    repaired, audit = absorb_session_topic_entities(
        _srt(
            "为什么这个藏剑传说官方要找我",
            "能不能把战争传说玩成Galgame",
            "战舰传说和钻剑传说都应该是同一个名字",
            "谢谢杖剑传说老师送的私人飞机，不是钻戒传送",
        ),
        authorities,
    )
    assert repaired.count("杖剑传说") == 6
    assert not any(
        wrong in repaired
        for wrong in (
            "藏剑传说",
            "战争传说",
            "战舰传说",
            "钻剑传说",
            "钻戒传送",
        )
    )
    assert "谢谢杖剑传说老师送的私人飞机，不是杖剑传说" in repaired
    assert all(
        malformed not in repaired
        for malformed in ("杖剑传说说", "杖剑传说说玩")
    )
    assert audit["status"] == "APPLIED"
    assert {row["authority"] for row in audit["repairs"]} == {
        "ROOM_TITLE_PLUS_FULL_SESSION_STRUCTURED_CHAT"
    }


def test_session_topic_phonetic_absorption_does_not_match_unrelated_chinese():
    repaired, audit = absorb_session_topic_entities(
        _srt("妹妹提升好感度", "今晚大家一起聊天"),
        [{"canonical": "杖剑传说"}],
    )

    assert "妹妹提升好感度" in repaired
    assert "今晚大家一起聊天" in repaired
    assert audit["status"] == "CLEAN"


def test_room_title_without_differing_chat_spelling_is_not_auto_authority(
    tmp_path: Path,
):
    xml = tmp_path / "session.xml"
    xml.write_text(
        """<i>
  <metadata><room_title>今天晚上大家聊天</room_title></metadata>
  <d p="1.0,1,25">今天晚上大家聊天</d>
</i>""",
        encoding="utf-8",
    )

    assert discover_session_topic_authorities(
        {"pieces": [{"danmaku_xml_local": str(xml)}]}
    ) == ()


def test_galgame_code_switch_variants_are_canonicalized():
    normalized, audit = normalize_code_switch_surfaces(
        _srt("把它玩成 gala game", "换成嘎啦 game")
    )

    assert normalized.count("Galgame") == 2
    assert "gala game" not in normalized
    assert "嘎啦 game" not in normalized
    assert audit["status"] == "APPLIED"


def test_confirmed_kimo_xiong_written_surfaces_are_canonicalized_to_kmx():
    normalized, audit = normalize_code_switch_surfaces(
        _srt("kimo熊系喜欢吗", "Kimo熊在打灰", "基默熊出现了")
    )

    assert normalized.count("kmx") == 3
    assert all(surface not in normalized for surface in ("kimo熊", "Kimo熊", "基默熊"))
    assert audit["status"] == "APPLIED"
