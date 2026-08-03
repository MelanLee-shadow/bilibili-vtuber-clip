from src.autoslice.self_reference_absorption import (
    absorb_host_self_references,
    host_phonetic_ratio,
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
        "HOST_SELF_REFERENCE_EQUAL_NAME_TEXT_RESOLUTION"
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
    assert host_phonetic_ratio("李杜莎") > host_phonetic_ratio("刘翔")
    assert host_phonetic_ratio("礼墨") < 0.58


def test_host_role_restatement_absorbs_unrelated_short_asr_phrase():
    payload = _srt(
        "官方要找流沙来玩这个游戏",
        "因为李杜莎切片破百万的",
        "这个，呃",
        "叫什么的",
        "做流量",
        "作为一个游戏里非常差的人",
    )

    repaired, audit = absorb_host_self_references(payload)

    assert "作为李豆沙" in repaired
    assert "做流量" not in repaired
    row = next(
        item
        for item in audit["repairs"]
        if item["authority"]
        == "HOST_SELF_REFERENCE_EQUAL_NAME_REPEAT_PLUS_LOCAL_SLOT"
    )
    assert row["prior_host_references"] >= 2
    assert row["next_cue"] == "作为一个游戏里非常差的人"


def test_host_role_restatement_requires_local_host_and_immediate_repeat():
    payload = _srt(
        "今天讨论怎么做流量",
        "做流量",
        "作为一个运营要先看数据",
    )
    unrelated_next = _srt(
        "官方要找李豆沙来玩这个游戏",
        "因为李豆沙很会玩",
        "做流量",
        "今天先看数据",
    )

    repaired, audit = absorb_host_self_references(payload)
    repaired_unrelated, audit_unrelated = absorb_host_self_references(unrelated_next)

    assert repaired == payload
    assert audit["status"] == "NO_MATCH"
    assert repaired_unrelated == unrelated_next
    assert audit_unrelated["status"] == "NO_MATCH"


def test_bcut_witness_restores_lidousha_without_alias_popularity_bias():
    final = _srt(
        "可以在弹幕发出你想让小李说的台词",
        "然后小李，小李就任选一个吧",
        "可以让小李任选吗",
        "欧不欧是可以由小李自己来决定吗",
    )
    bcut = _srt(
        "可以在弹幕发出你想让流沙说的台词",
        "然后流沙，流沙就任选一个吧",
        "可以让流沙任选吗",
        "欧不欧是可以留下来自己来代替吗",
    )

    repaired, audit = absorb_host_self_references(
        final,
        source_witness_srt=bcut,
    )

    assert "小李" not in repaired
    assert repaired.count("李豆沙") == 5
    assert audit["source_witness_available"] is True
    source_rows = [
        row
        for row in audit["repairs"]
        if row["authority"]
        == "HOST_SELF_REFERENCE_EQUAL_NAME_AUDIO_RESOLUTION"
    ]
    assert len(source_rows) == 5
    assert source_rows[-1]["evidence_mode"] == "LOCAL_REPEAT_CONSENSUS"
    assert source_rows[-1]["prior_consensus_votes"] >= 3


def test_bcut_witness_does_not_rewrite_real_xiaoli_or_ordinary_mentions():
    final = _srt(
        "可以在弹幕发出你想让小李说的台词",
        "小李今天看了MyGO",
        "礼墨说可以让小李任选吗",
    )
    bcut = _srt(
        "可以在弹幕发出你想让小李说的台词",
        "小李今天看了MyGO",
        "礼墨说可以让小李任选吗",
    )

    repaired, audit = absorb_host_self_references(
        final,
        source_witness_srt=bcut,
    )

    assert repaired == final
    assert audit["status"] == "NO_MATCH"


def test_optional_you_preposition_is_not_absorbed_into_name_slot():
    final = _srt(
        "官方要找李豆沙来玩这个游戏",
        "因为李豆沙很会玩",
        "欧不欧是可以由李豆沙自己来决定吗",
    )
    bcut = _srt(
        "官方要找流沙来玩这个游戏",
        "因为李杜莎很会玩",
        "欧不欧是可以留下来自己来代替吗",
    )

    repaired, audit = absorb_host_self_references(
        final,
        source_witness_srt=bcut,
    )

    assert repaired == final
    assert "可以李豆沙自己" not in repaired
    assert all(row["before"] != "由李豆沙" for row in audit["repairs"])


def test_name_arbitration_is_symmetric_when_bcut_says_xiaoli():
    final = _srt(
        "可以在弹幕发出你想让李豆沙说的台词",
        "然后李豆沙就任选一个吧",
    )
    bcut = _srt(
        "可以在弹幕发出你想让小李说的台词",
        "然后小李就任选一个吧",
    )

    repaired, audit = absorb_host_self_references(
        final,
        source_witness_srt=bcut,
    )

    assert "李豆沙" not in repaired
    assert repaired.count("小李") == 2
    assert {
        row["after"]
        for row in audit["repairs"]
        if row["authority"] == "HOST_SELF_REFERENCE_EQUAL_NAME_AUDIO_RESOLUTION"
    } == {"小李"}


def test_name_arbitration_preserves_dousha_as_an_equal_candidate():
    final = _srt("可以在弹幕发出你想让小李说的台词")
    bcut = _srt("可以在弹幕发出你想让豆沙说的台词")

    repaired, audit = absorb_host_self_references(
        final,
        source_witness_srt=bcut,
    )

    assert "想让豆沙说" in repaired
    assert audit["repairs"][0]["after"] == "豆沙"


def test_ambiguous_audio_has_no_candidate_order_tiebreak():
    final = _srt("可以在弹幕发出你想让小李说的台词")
    bcut = _srt("可以在弹幕发出你想让刘翔说的台词")

    repaired, audit = absorb_host_self_references(
        final,
        source_witness_srt=bcut,
    )

    assert repaired == final
    assert audit["status"] == "NO_MATCH"


def test_local_repeat_does_not_absorb_a_different_person():
    payload = _srt(
        "官方要找李豆沙来玩这个游戏",
        "因为李豆沙很会玩",
        "我想让刘翔说一句台词",
    )

    repaired, audit = absorb_host_self_references(payload)

    assert "刘翔" in repaired
    assert audit["status"] == "NO_MATCH"


def test_conflicting_names_in_one_audio_window_do_not_use_pattern_order():
    final = _srt("可以在弹幕发出你想让豆沙说的台词")
    bcut = _srt("先想让小李说一句，再让李豆沙任选")

    repaired, audit = absorb_host_self_references(
        final,
        source_witness_srt=bcut,
    )

    assert repaired == final
    assert audit["status"] == "NO_MATCH"


def test_bcut_witness_corrects_excluded_name_in_offline_call_slot():
    final = _srt("让礼墨线下叫kmx")
    bcut = _srt("让刘莎线下叫停了时")

    repaired, audit = absorb_host_self_references(
        final,
        source_witness_srt=bcut,
    )

    assert "让李豆沙线下叫kmx" in repaired
    assert audit["status"] == "APPLIED"
    assert audit["repairs"][0]["before"] == "礼墨"
    assert (
        audit["repairs"][0]["authority"]
        == "HOST_SELF_REFERENCE_EQUAL_NAME_AUDIO_RESOLUTION"
    )


def test_real_limo_in_offline_call_slot_is_not_absorbed():
    final = _srt("让礼墨线下叫kmx")

    repaired, audit = absorb_host_self_references(
        final,
        source_witness_srt=final,
    )

    assert repaired == final
    assert audit["status"] == "NO_MATCH"
