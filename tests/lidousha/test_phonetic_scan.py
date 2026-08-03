"""拼音混淆候选发现层 + 短语级重复分歧编译器（欠账 #0/#5）。

夹具全部来自 五件套审片实案：这两条 lane 的存在意义就是把
当晚人工审片抓到的 kmx 变体（皮毛熊/Q我熊/K头小）、奶P（卖批/奶皮子）、
抱/帮、零的人/零个人 变成确定性候选发现。阈值改动必须过这组回归。
"""

from __future__ import annotations

import pytest

from src.autoslice.chat_evidence import ReferentEntity, ReferentGroup
from src.autoslice.phonetic_scan import (
    glossary_phonetic_candidate_groups,
    phrase_repetition_divergence_groups,
)


def _srt(*lines: str) -> str:
    blocks = []
    for index, text in enumerate(lines):
        start = index * 3
        blocks.append(
            f"{index + 1}\n"
            f"00:00:{start:02d},000 --> 00:00:{start + 2:02d},500\n"
            f"{text}"
        )
    return "\n\n".join(blocks) + "\n"


def _groups() -> list[ReferentGroup]:
    return [
        ReferentGroup(
            (
                ReferentEntity("kmx", ("kmx", "kimo熊"), ("ki mo xiong", "kimo xiong")),
                ReferentEntity("乒乓球", ("乒乓球",), ("ping pang qiu",)),
            ),
            audio_verify_all_surfaces=True,
        ),
        ReferentGroup(
            (
                ReferentEntity("奶P", ("奶P",), ("nai pi",)),
                ReferentEntity("奶瓶", ("奶瓶",), ("nai ping",)),
            ),
            audio_verify_all_surfaces=True,
        ),
        ReferentGroup(
            (
                ReferentEntity(
                    "ありがとう",
                    ("ありがとう",),
                    ("a li ga dou", "a ri ga tou", "li gen duo"),
                ),
            ),
            audio_verify_all_surfaces=True,
        ),
        ReferentGroup(
            (
                ReferentEntity("李豆沙", (), ("li dou sha", "lidousha")),
                ReferentEntity("小李", (), ("xiao li", "xiaoli")),
            ),
            audio_verify_all_surfaces=True,
        ),
    ]


def _phonetic_pairs(srt_text: str) -> set[tuple[str, str]]:
    return {
        (group.entities[0].canonical, group.entities[1].canonical)
        for group in glossary_phonetic_candidate_groups(
            srt_text, _groups(), protected_faces=frozenset()
        )
    }


class TestGlossaryPhoneticCandidates:
    def test_catches_kmx_variants_from_20260718(self) -> None:
        """皮毛熊/Q我熊/K头小（她精心设计 1:49/3:26 案）必须进候选。"""

        srt = _srt("皮毛熊是猪，皮毛熊是猪", "你不是31级Q我熊吗", "K头小是猪")
        pairs = _phonetic_pairs(srt)
        surfaces = {surface for canonical, surface in pairs if canonical == "kmx"}
        assert "皮毛熊" in surfaces
        assert "Q我熊" in surfaces
        assert "K头小" in surfaces

    def test_catches_naip_variants(self) -> None:
        """卖批/奶皮子（她精心设计 0:21/0:27 案）必须进 奶P 候选。"""

        srt = _srt("有人说零个人喊熊猫卖批", "怎么是奶皮子也是最多的吗")
        pairs = _phonetic_pairs(srt)
        surfaces = {surface for canonical, surface in pairs if canonical == "奶P"}
        assert any("卖批" in s for s in surfaces)
        assert any("奶皮" in s for s in surfaces)

    def test_catches_arigatou_as_lingengduo(self) -> None:
        """林更多（七星 2:06 案，ありがとう 误听）必须进候选。"""

        srt = _srt("谢谢你呀，林更多", "林更多收到了")
        pairs = _phonetic_pairs(srt)
        assert ("ありがとう", "林更多") in pairs

    def test_rejects_common_word_noise(self) -> None:
        """高频词（有人/件事/谢谢）不许撞短实体——0.65/0.80 短实体门校准。"""

        srt = _srt("我看到有人说这件事情", "谢谢大家夸我")
        pairs = _phonetic_pairs(srt)
        flagged = {surface for _canonical, surface in pairs}
        assert "有人" not in flagged
        assert "件事" not in flagged
        assert "谢谢" not in flagged

    def test_registered_faces_are_skipped(self) -> None:
        """已注册面（kimo熊）走精确匹配通道，扫描器不再重复编组。"""

        srt = _srt("kimo熊真哭啦")
        pairs = _phonetic_pairs(srt)
        assert ("kmx", "kimo熊") not in pairs

    def test_cap_keeps_highest_scores(self) -> None:
        """cap 截断按分数排序：低分噪声不许饿死高分真命中。"""

        srt = _srt(
            "皮毛熊是猪",
            "谢谢你呀，林更多",
            "怎么是奶皮子也是最多的吗",
            "你不是31级Q我熊吗",
            "K头头是猪，K头小是猪",
        )
        groups = glossary_phonetic_candidate_groups(
            srt, _groups(), max_groups=3, protected_faces=frozenset()
        )
        assert len(groups) == 3
        surfaces = {group.entities[1].canonical for group in groups}
        # 奶皮/林更多 分数在 0.83+，必须在前三里。
        assert any("奶皮" in s for s in surfaces)
        assert "林更多" in surfaces

    def test_uncertain_keeps_both_sides(self) -> None:
        """发现≠裁决：UNCERTAIN 双向保留、绝不阻塞交付。"""

        srt = _srt("皮毛熊是猪")
        groups = glossary_phonetic_candidate_groups(
            srt, _groups(), protected_faces=frozenset()
        )
        target = next(g for g in groups if g.entities[1].canonical == "皮毛熊")
        assert set(target.uncertain_keep_canonicals) == {"kmx", "皮毛熊"}
        assert "phonetic_candidate" in target.positions
        assert target.audio_verify_all_surfaces


class TestPhraseRepetitionDivergence:
    def test_catches_bang_bao_from_flower_basket(self) -> None:
        """抱/帮（看花篮 0:22 案）：跨句短语重现一字近音差。"""

        srt = _srt(
            "那个，就大家这次萤火虫不是帮小李准备了花篮吗",
            "非常非常感谢！",
            "非常非常感谢大家",
            "就是抱小李准备那个花了",
        )
        groups = phrase_repetition_divergence_groups(srt)
        pair = {
            frozenset((g.entities[0].canonical, g.entities[1].canonical))
            for g in groups
        }
        assert frozenset(("是帮小李准备", "是抱小李准备")) in pair

    def test_catches_ling_ge_ren(self) -> None:
        """零的人/零个人（她精心设计 0:09 案）。"""

        srt = _srt(
            "我看到有人说零的人喊熊猫，是什么意思呢",
            "在小李说熊猫之前",
            "有人，有人说零个人喊熊猫",
        )
        groups = phrase_repetition_divergence_groups(srt)
        assert any(
            {"零" if False else g.entities[0].canonical, g.entities[1].canonical}
            == {"人说零的人喊", "人说零个人喊"}
            or {g.entities[0].canonical, g.entities[1].canonical}
            == {"说零的人喊熊", "说零个人喊熊"}
            for g in groups
        )

    def test_longest_gram_wins_no_duplicate_slots(self) -> None:
        """同一分歧字位只编一组（6-gram 命中后 5-gram 不再重复）。"""

        srt = _srt(
            "不是帮小李准备了花篮吗",
            "就是抱小李准备那个花了",
        )
        groups = phrase_repetition_divergence_groups(srt)
        diff_pairs = [
            (g.entities[0].canonical, g.entities[1].canonical) for g in groups
        ]
        bang_bao = [p for p in diff_pairs if "帮" in p[0] + p[1] and "抱" in p[0] + p[1]]
        assert len(bang_bao) == 1

    def test_particle_only_divergence_skipped(self) -> None:
        """就太好了/就太好啦：双方都是语气词的分歧不编组。"""

        srt = _srt("那就太好啦", "开心就太好了")
        groups = phrase_repetition_divergence_groups(srt)
        assert not groups

    def test_protected_slot_not_grouped(self) -> None:
        """分歧字位落在钦定词面（侄女）里时让位给词表权威。"""

        srt = _srt(
            "“李姐是侄女”，好多人笑出声这个事情呢",
            "李姐是世界上最可爱的姐姐",
        )
        groups = phrase_repetition_divergence_groups(srt)
        for group in groups:
            for entity in group.entities:
                assert "侄" not in entity.canonical

    def test_phonetically_distant_slots_skipped(self) -> None:
        """姐姐/妈妈枚举复读（jie/ma 远音）不编组——真枚举不是分歧。"""

        srt = _srt("和最可爱的姐姐", "和最可爱的妈妈", "和最可爱的主人")
        groups = phrase_repetition_divergence_groups(srt)
        assert not groups


@pytest.mark.parametrize(
    "reading,window,expected",
    [
        (("ki mo xiong",), "皮毛熊", True),
        (("ki mo xiong",), "李说熊", False),
        (("li dou sha",), "里头她", True),
        (("li dou sha",), "李姐", False),
    ],
)
def test_alignment_calibration(reading, window, expected) -> None:
    """阈值校准的最小回归：调阈值前先过这几对。"""

    srt = _srt(f"{window}在这里")
    groups = glossary_phonetic_candidate_groups(
        srt,
        [
            ReferentGroup(
                (ReferentEntity("目标", ("目标",), reading),),
                audio_verify_all_surfaces=True,
            )
        ],
        protected_faces=frozenset(),
    )
    hit = any(g.entities[1].canonical == window for g in groups)
    assert hit is expected


def test_reduplicated_registered_face_not_windowed() -> None:
    """叠名守卫：滑窗骑在注册面
    「小李」的出现区间上（中段「李小」）不许成为其他实体的候选。"""

    srt = _srt("小李小李，你能教教我怎么样")
    groups = [
        ReferentGroup(
            (
                ReferentEntity("立希", ("立希",), ("li xi",)),
                ReferentEntity("祥子", ("祥子",), ("xiang zi",)),
            ),
            audio_verify_all_surfaces=True,
        ),
        ReferentGroup(
            (
                ReferentEntity("李豆沙", (), ("li dou sha",)),
                ReferentEntity("小李", ("小李",), ("xiao li",)),
            ),
            audio_verify_all_surfaces=True,
        ),
    ]
    found = glossary_phonetic_candidate_groups(
        srt, groups, protected_faces=frozenset()
    )
    surfaces = {g.entities[1].canonical for g in found}
    assert "李小" not in surfaces
    assert not any("立希" == g.entities[0].canonical for g in found)
