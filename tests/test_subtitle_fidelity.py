from types import SimpleNamespace

from src.autoslice.subtitle_fidelity import (
    apply_numeric_fact_provenance_guard,
    apply_subtitle_fidelity_guard,
    digit_reading_equivalent,
)


def _srt(*texts: str) -> str:
    blocks = []
    for index, text in enumerate(texts, start=1):
        blocks.append(
            f"{index}\n00:00:{index * 5:02d},000 --> 00:00:{index * 5 + 4:02d},000\n{text}"
        )
    return "\n\n".join(blocks) + "\n"


def test_number_paraphrase_reverts_to_draft_without_witness():
    """2026-07-11 实案：BCUT「190」被修正层转述成「一米九」。一九零是梗，
    无证人时必须忠实原文。"""
    draft = _srt("就是她小时候被那个190粉毛花臂抢了三部手机")
    final = _srt("就是她小时候被那个一米九粉毛花臂抢了三部手机")

    guarded, audit = apply_subtitle_fidelity_guard(draft, final, agy_srt=None, sanctioned=())

    assert "190" in guarded
    assert "一米九" not in guarded
    assert audit["status"] == "APPLIED"
    assert audit["reverted"][0]["violations"][0]["reason"] == "REPLACE_UNWITNESSED"


def test_digit_reading_rewrite_passes():
    draft = _srt("就是她被那个190粉毛抢了手机")
    final = _srt("就是她被那个一九零粉毛抢了手机")

    guarded, audit = apply_subtitle_fidelity_guard(draft, final, agy_srt=None, sanctioned=())

    assert "一九零" in guarded
    assert audit["status"] == "CLEAN"
    assert digit_reading_equivalent("190", "幺九零")
    assert not digit_reading_equivalent("190", "一米九")


def test_unwitnessed_name_guess_reverts_but_agy_witness_passes():
    """2026-07-11 生日结婚实案：BCUT 误听「留下」，AGY 缺席时 CPA 猜成
    「小李」= 无证改写必须回退；AGY 在场且听到「李豆沙」则采信。"""
    draft = _srt("所以你是想看留下跟别人亲亲")
    guessed = _srt("所以你是想看小李跟别人亲亲")

    guarded, audit = apply_subtitle_fidelity_guard(draft, guessed, agy_srt=None, sanctioned=())
    assert "留下" in guarded
    assert "小李" not in guarded
    assert audit["reverted_count"] == 1

    agy = _srt("所以你是想看李豆沙跟别人亲亲")
    adopted = _srt("所以你是想看李豆沙跟别人亲亲")
    guarded2, audit2 = apply_subtitle_fidelity_guard(draft, adopted, agy_srt=agy, sanctioned=())
    assert "李豆沙" in guarded2
    assert audit2["status"] == "CLEAN"


def test_sanctioned_table_and_homophone_sets_pass():
    draft = _srt("我是直女啊", "他今天来了吗")
    final = _srt("我是侄女啊", "她今天来了嘛")

    guarded, audit = apply_subtitle_fidelity_guard(
        draft, final, agy_srt=None, sanctioned=(("直女", "侄女"),)
    )

    assert "侄女" in guarded
    assert "她今天来了嘛" in guarded
    assert audit["status"] == "CLEAN"


def test_hallucination_drop_and_small_particle_trim_allowed():
    draft = _srt("虫儿飞虫儿飞", "就是呢那个她们要去彩排了")
    final = _srt("", "就是那个她们要去彩排了")

    guarded, audit = apply_subtitle_fidelity_guard(draft, final, agy_srt=None, sanctioned=())

    assert audit["hallucination_drops"][0]["cue_index"] == 1
    assert "虫儿飞" not in guarded.split("\n\n")[0].split("\n")[-1]
    assert audit["status"] == "CLEAN"


def test_pinyin_homophone_respell_passes_without_witness():
    """2026-07-14 五年之约冤杀案回归：季下→记下(jì同音)、小丽→小李(lǐ同音)
    是声学保真的合法重拼，AGY 缺席也必须放行；转述(一米九)与非同音实体换写
    (留下→小李)仍回退。"""
    draft = _srt("欢迎季下", "小丽只是恰好处在一个", "就是她被那个190粉毛抢了手机")
    final = _srt("欢迎记下", "小李只是恰好处在一个", "就是她被那个一米九粉毛抢了手机")

    guarded, audit = apply_subtitle_fidelity_guard(draft, final, agy_srt=None, sanctioned=())

    assert "欢迎记下" in guarded
    assert "小李只是恰好处在一个" in guarded
    assert "一米九" not in guarded  # 转述仍被回退
    assert audit["reverted_count"] == 1


def test_cue_count_mismatch_skips_guard():
    draft = _srt("一句")
    final = _srt("一句", "多出来的")

    guarded, audit = apply_subtitle_fidelity_guard(draft, final, agy_srt=None, sanctioned=())

    assert audit["status"] == "SKIPPED_CUE_COUNT_MISMATCH"
    assert guarded == final


def test_numeric_fact_introduced_by_semantic_lane_without_source_is_reverted():
    draft = _srt("所以在这里喂，哈哈")
    final = _srt("所以在这里为李豆沙做0.4")

    guarded, audit = apply_numeric_fact_provenance_guard(draft, final)

    assert "0.4" not in guarded
    assert "所以在这里喂，哈哈" in guarded
    assert audit["status"] == "REVERTED_UNPROVEN_NUMERIC_FACT"
    assert audit["reverted"][0]["unsupported"][0]["token"] == "0.4"


def test_numeric_fact_survives_when_same_time_structured_chat_contains_it():
    draft = _srt("抽卡惩罚就做这个")
    final = _srt("抽卡惩罚就为礼墨做0.6")
    evidence = [
        SimpleNamespace(
            offset_ms=5_500,
            text="为礼墨做0.6",
            kind="danmaku",
        )
    ]

    guarded, audit = apply_numeric_fact_provenance_guard(
        draft, final, structured_evidence=evidence
    )

    assert "为礼墨做0.6" in guarded
    assert audit["status"] == "CLEAN"
