from src.autoslice.self_reference_absorption import (
    absorb_host_self_references,
    lidousha_phonetic_ratio,
)


def _srt(*texts: str) -> str:
    return "\n\n".join(
        f"{index}\n00:00:{index * 3:02d},000 --> 00:00:{index * 3 + 2:02d},000\n{text}"
        for index, text in enumerate(texts, start=1)
    ) + "\n"


def test_host_self_reference_slots_absorb_phonetically_close_wrong_names():
    payload = _srt(
        "官方要找流沙来玩这个游戏",
        "因为李杜莎切夸夸克的",
        "那我就用流沙的方式把这游戏玩",
        "官方能同意留下把杖剑传说完成吗",
        "官方给了留下一个挑战",
        "留下是什么游戏苦手",
    )

    repaired, audit = absorb_host_self_references(payload)

    assert repaired.count("李豆沙") == 6
    assert all(surface not in repaired for surface in ("流沙", "李杜莎", "留下"))
    assert audit["status"] == "APPLIED"
    assert {row["authority"] for row in audit["repairs"]} == {
        "HOST_SELF_REFERENCE_GRAMMAR_PLUS_PHONETIC_ABSORPTION"
    }


def test_phonetic_absorption_does_not_turn_other_people_or_generic_words_into_host():
    payload = _srt(
        "这台词可以由礼墨来定",
        "我想让刘翔来玩这个游戏",
        "官方同意留下来继续直播",
    )

    repaired, audit = absorb_host_self_references(payload)

    assert repaired == payload
    assert audit["status"] == "NO_MATCH"
    assert lidousha_phonetic_ratio("李杜莎") > lidousha_phonetic_ratio("刘翔")
    assert lidousha_phonetic_ratio("礼墨") < 0.58
