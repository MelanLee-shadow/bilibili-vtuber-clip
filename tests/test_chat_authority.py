import copy
import hashlib
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from scripts.produce_slice_package import (
    _load_independent_chat_support_srts,
    _piece_chat_evidence,
    verify_chat_authority_final_surfaces,
)
from src.autoslice.chat_authority import (
    ChatEvidence,
    ReferentEntity,
    ReferentGroup,
    apply_audio_entity_verification,
    apply_authoritative_chat_evidence,
    build_human_text_entity_verifier,
    clip_opening_address_group,
    load_chat_jsonl,
    load_referent_groups,
    repetition_divergence_groups,
    normalize_code_switch_surfaces,
    recording_start_epoch_ms,
    reconcile_pending_text_overrides,
    reconcile_reviewed_text_override_conflicts,
    witness_disagreement_cues,
    introduced_term_cues,
    _strip_interjections_once,
)
from src.autoslice.jingting_chunker import parse_srt_cues
from src.autoslice.producer_chat_input import StructuredChatEvidenceError
from src.autoslice.chat_repair import (
    _aligned_span_replacements,
    _repair_sc_sender,
    _spoken_sender_alias,
)


def _srt(*texts: str) -> str:
    blocks = []
    for index, text in enumerate(texts, start=1):
        blocks.append(
            f"{index}\n00:00:{index * 5:02d},000 --> 00:00:{index * 5 + 4:02d},000\n{text}"
        )
    return "\n\n".join(blocks) + "\n"


def _audio_entity_verifier(canonical: str):
    def verify(request):
        return {
            "schema_version": "chat-entity-verdict.v1",
            "request_sha256": request["request_sha256"],
            "status": "RESOLVED",
            "canonical_entity": canonical,
            "authority_kind": "audio_forced_choice",
            "confidence": 0.97,
            "heard_syllables": canonical,
            "source_media_sha256": "a" * 64,
            "audio_clip_sha256": "b" * 64,
            "prompt_sha256": "c" * 64,
            "response_sha256": "d" * 64,
        }

    return verify


def test_interjection_stripping_preserves_authority_owned_duplicate_word():
    expected = "妈妈主人老公姐姐宝贝晚上好今天李出来的时候眼睛袅袅了"
    span = expected + "宝贝"

    assert (
        _strip_interjections_once(
            span,
            ["宝贝"],
            required_substring=expected,
        )
        == span
    )


def test_interjection_stripping_removes_the_occurrence_that_splits_authority():
    expected = "妈妈主人老公姐姐晚上好"
    span = "妈妈主人宝贝老公姐姐晚上好"

    assert (
        _strip_interjections_once(
            span,
            ["宝贝"],
            required_substring=expected,
        )
        == expected
    )


def test_interjection_stripping_searches_past_same_token_in_neighbor_cue():
    expected = "一般不是llnn吗"
    # Final verification windows include boundary-touching neighbour cues.
    # The first ``NN`` belongs to the neighbour; only the second one is the
    # declared interjection inside the authority span.
    span = "都是nnll一般不是nnllhhb或者nn吗对对对"

    repaired = _strip_interjections_once(
        span,
        [" NN", "HHB 或者 "],
        required_substring=expected,
    )

    assert expected in repaired
    assert repaired == "都是nnll一般不是llnn吗对对对"


DREAM_MUJICA_GROUP = ReferentGroup(
    (
        ReferentEntity("梦限大", ("梦限大", "梦现代"), ("meng xian da",)),
        ReferentEntity("Ave Mujica", ("Ave Mujica", "Mujica", "母鸡卡", "木子卡"), ("mujica",)),
    )
)
CHARACTER_GROUP = ReferentGroup(
    (
        ReferentEntity("立希", ("立希", "椎名立希"), ("li xi", "taki")),
        ReferentEntity("祥子", ("祥子", "丰川祥子", "saki"), ("xiang zi", "saki")),
    ),
    audio_verify_all_surfaces=True,
)


def test_v2_entity_config_keeps_aliases_under_one_canonical():
    groups = load_referent_groups(
        Path(__file__).resolve().parents[1] / "assets/lidousha/entity_confusables.json"
    )
    dream_group = next(
        group for group in groups if {entity.canonical for entity in group.entities} == {"梦限大", "Ave Mujica"}
    )
    mujica = next(entity for entity in dream_group.entities if entity.canonical == "Ave Mujica")

    assert {"Mujica", "母鸡卡", "木子卡"} <= set(mujica.surfaces)
    assert all(entity.canonical != "母鸡卡" for entity in dream_group.entities)


def test_japanese_code_switch_surface_is_canonicalized_without_timing_change():
    source = _srt("这种哇哭哇哭的感觉")

    output, audit = normalize_code_switch_surfaces(source)

    assert "这种wakuwaku的感觉" in output
    assert "00:00:05,000 --> 00:00:09,000" in output
    assert audit["repairs"][0]["authority"] == "lidousha-code-switch-canon.v1"


def test_ordinary_speech_audio_verifier_repairs_saki_to_lixi_entity_only():
    source = _srt("然后那个saki的高压的态度")

    output, audit = apply_audio_entity_verification(
        source,
        referent_groups=[CHARACTER_GROUP],
        entity_verifier=_audio_entity_verifier("立希"),
    )

    assert "然后那个立希的高压的态度" in output
    assert "saki" not in output
    assert audit["repairs"][0]["mode"] == "transcript_entity_only"
    assert audit["repairs"][0]["before"] == ["然后那个saki的高压的态度"]


def test_ordinary_speech_entity_verification_fails_closed_when_uncertain():
    source = _srt("然后那个祥子的高压的态度")

    output, audit = apply_audio_entity_verification(
        source,
        referent_groups=[CHARACTER_GROUP],
        entity_verifier=None,
    )

    assert output == source
    assert audit["status"] == "ENTITY_VERDICT_REQUIRED"
    assert audit["entity_verdict_required"][0]["cue_index"] == 1


KMX_GROUP = ReferentGroup(
    (
        ReferentEntity(
            "kmx",
            ("kmx", "kimo熊", "提莫熊", "提莫的熊"),
            ("ki mo xiong", "kimo xiong"),
        ),
        ReferentEntity("乒乓球", ("乒乓球",), ("ping pang qiu",)),
    ),
    audio_verify_all_surfaces=True,
    uncertain_keep_canonicals=("kmx",),
)


KMX_ASSOC_GROUP = ReferentGroup(
    KMX_GROUP.entities,
    audio_verify_all_surfaces=True,
    uncertain_keep_canonicals=("kmx",),
    association_core_chars=("熊",),
)


def test_context_association_recall_arbitrates_shattered_proper_noun():
    """2026-07-25 叹十七手实案：ASR 把 kmx(kimo熊) 打散成清单外新变体，
    字面召回必然失败；但 cue 里有关联词「坏熊」且 kmx 已在本片他处确认——
    语境关联召回把句首杂段送音频强裁，判 kmx 即改写。"""
    source = _srt("叹十七手里面的坏熊太多了", "kmx欺负我")

    output, audit = apply_audio_entity_verification(
        source,
        referent_groups=[KMX_ASSOC_GROUP],
        entity_verifier=_audio_entity_verifier("kmx"),
    )

    assert "kmx里面的坏熊太多了" in output
    assert "叹十七手" not in output
    repair = audit["repairs"][0]
    assert repair["transcript_surface"] == "叹十七手"
    assert repair["resolved_canonical"] == "kmx"


def test_context_association_recall_keeps_original_when_uncertain():
    """关联召回是额外召回：音频拿不准（无 verifier）= 保留原文，绝不阻塞。"""
    source = _srt("叹十七手里面的坏熊太多了", "kmx欺负我")

    output, audit = apply_audio_entity_verification(
        source,
        referent_groups=[KMX_ASSOC_GROUP],
        entity_verifier=None,
    )

    assert output == source
    assert audit.get("status") != "ENTITY_VERDICT_REQUIRED"
    assert any(
        row.get("reason_code") == "ENTITY_SURFACE_KEPT_ON_UNCERTAIN"
        for row in audit.get("confirmed") or []
    )


def test_context_association_needs_in_clip_confirmation():
    """无 clip 内 kmx 字面确认时关联召回不触发（防无语境乱裁）。"""
    source = _srt("叹十七手里面的坏熊太多了", "今天天气不错")

    output, audit = apply_audio_entity_verification(
        source,
        referent_groups=[KMX_ASSOC_GROUP],
        entity_verifier=_audio_entity_verifier("kmx"),
    )

    assert output == source
    assert not audit.get("repairs")


def test_double_canonical_occurrences_pass_without_slot_ambiguity():
    """2026-07-13 MUA 实案：「除了社恐kmx之外有社牛kmx」两处都是规范形 kmx，
    多槽位不构成歧义，不得 fail-closed 整条打回。"""
    source = _srt("除了社恐kmx之外有社牛kmx")
    output, audit = apply_audio_entity_verification(
        source,
        referent_groups=[KMX_GROUP],
        entity_verifier=None,
    )
    assert output == source
    assert audit["status"] != "ENTITY_VERDICT_REQUIRED", audit
    assert audit["confirmed"][0]["reason_code"] == "ENTITY_ALREADY_CANONICAL_EVERYWHERE"


def test_canonical_surface_kept_when_audio_uncertain_but_mishear_still_blocks():
    """方向性 fail-closed：文本已是 kmx 时 UNCERTAIN → 保留不阻塞；文本是
    疑似误听形「乒乓球」时 UNCERTAIN → 仍然阻塞待裁。"""
    def uncertain(request):
        return {
            "schema_version": "chat-entity-verdict.v1",
            "request_sha256": request["request_sha256"],
            "status": "UNCERTAIN",
            "reason_code": "ENTITY_AUDIO_UNCERTAIN",
        }

    canonical_src = _srt("kmx今天也来了")
    output, audit = apply_audio_entity_verification(
        canonical_src, referent_groups=[KMX_GROUP], entity_verifier=uncertain
    )
    assert output == canonical_src
    assert audit["status"] != "ENTITY_VERDICT_REQUIRED", audit
    assert audit["confirmed"][0]["reason_code"] == "ENTITY_CANONICAL_KEPT_ON_UNCERTAIN"

    mishear_src = _srt("突击一下乒乓球")
    output2, audit2 = apply_audio_entity_verification(
        mishear_src, referent_groups=[KMX_GROUP], entity_verifier=uncertain
    )
    assert output2 == mishear_src  # 不确定绝不改写
    assert audit2["status"] == "ENTITY_VERDICT_REQUIRED"


def test_exact_chat_and_semantic_text_agreement_skips_entity_audio_for_any_name():
    """结构化弹幕与语义精修文本逐字同意专名时，不限 kmx，后置声学模型
    都不得把两份一致文本证据一起推翻。"""
    calls = []

    def conflicting_verifier(request):
        calls.append(request)
        return _audio_entity_verifier("Ave Mujica")(request)

    source = _srt("梦限大最近很火")
    output, audit = apply_authoritative_chat_evidence(
        source,
        [ChatEvidence("danmaku", 0, "梦限大最近很火")],
        support_srt_texts=[source],
        referent_groups=[DREAM_MUJICA_GROUP],
        entity_verifier=conflicting_verifier,
    )

    assert "梦限大最近很火" in output
    assert "Ave Mujica" not in output
    assert calls == []
    assert any(
        row.get("reason_code")
        == "ENTITY_CANONICAL_CORROBORATED_BY_CHAT_AND_SEMANTIC_TEXT"
        for row in audit["entity_verdicts"]
    )


def test_exact_kmx_chat_and_registered_semantic_alias_skip_provider() -> None:
    calls = []

    def unavailable_verifier(request):
        calls.append(request)
        return _uncertain_verifier(request)

    source = _srt(
        "其实我有想到熊猫",
        "但是我想到提莫的熊",
        "会没人想到熊猫就想笑",
    )
    output, audit = apply_authoritative_chat_evidence(
        source,
        [
            ChatEvidence(
                "danmaku",
                0,
                "其实我有想到熊猫 但是我想到kmx会没人想到熊猫就想笑",
            )
        ],
        support_srt_texts=[source],
        referent_groups=[KMX_GROUP],
        entity_verifier=unavailable_verifier,
    )

    assert "kmx" in output
    assert "提莫的熊" not in output
    assert calls == []
    assert audit["status"] == "APPLIED_AND_VERIFIED"
    assert audit["entity_verdict_required"] == []
    assert any(
        row.get("reason_code")
        == "ENTITY_CANONICAL_CORROBORATED_BY_EXACT_CHAT_AND_REGISTERED_SEMANTIC_ALIAS"
        for row in audit["entity_verdicts"]
    )


def test_exact_kmx_chat_and_competing_ping_pong_semantic_text_still_block() -> None:
    source = _srt("我想到乒乓球", "会没人想到熊猫就想笑")
    output, audit = apply_authoritative_chat_evidence(
        source,
        [ChatEvidence("danmaku", 0, "我想到kmx会没人想到熊猫就想笑")],
        support_srt_texts=[source],
        referent_groups=[KMX_GROUP],
        entity_verifier=_uncertain_verifier,
    )

    assert output == source
    assert audit["status"] == "ENTITY_VERDICT_REQUIRED"
    assert audit["entity_verdict_required"][0]["matched_audio_text"] == (
        "我想到乒乓球会没人想到熊猫就想笑"
    )


def test_repeated_exact_chat_canonical_is_not_multi_entity_ambiguity() -> None:
    exact = "姐姐还是摸摸kmx吧，kmx不咬人还喜欢被敲"
    calls = []

    def unavailable_verifier(request):
        calls.append(request)
        return _uncertain_verifier(request)

    source = _srt(exact)
    output, audit = apply_authoritative_chat_evidence(
        source,
        [ChatEvidence("superchat", 0, exact)],
        support_srt_texts=[source],
        referent_groups=[KMX_GROUP],
        entity_verifier=unavailable_verifier,
    )

    assert output == source
    assert calls == []
    assert audit["entity_verdict_required"] == []
    assert any(
        row.get("reason_code")
        == "ENTITY_REPETITION_CORROBORATED_BY_CHAT_AND_SEMANTIC_TEXT"
        for row in audit["entity_verdicts"]
    )


def test_repeated_chat_entity_without_slot_mapping_fails_closed() -> None:
    exact = (
        "姐姐姐姐姐还是在直播间摸摸kmx吧，"
        "kmx不咬人还喜欢被敲（在公司说怪话好刺激）"
    )
    source = _srt(
        "姐姐姐姐姐还是在提问",
        "还是在直播间摸提问什么提问什么，不咬人",
        "还喜欢被敲。在公司说怪好刺激",
        "之前都下班再，再说是吧？",
    )

    output, audit = apply_authoritative_chat_evidence(
        source,
        [ChatEvidence("superchat", 0, exact)],
        support_srt_texts=[source],
        referent_groups=[KMX_GROUP],
        entity_verifier=_uncertain_verifier,
    )

    assert output == source
    assert audit["status"] == "ENTITY_VERDICT_REQUIRED"
    assert audit["entity_verdict_required"][0]["reason_code"] == (
        "REPEATED_CHAT_ENTITY_SLOTS_UNRESOLVED"
    )
    assert audit["entity_verdict_required"][0]["structured_chat_occurrence_count"] == 2
    assert audit["entity_verdict_required"][0]["semantic_text_occurrence_count"] == 0
    assert audit["read_aloud_arbitrations"] == []


OPENING_ADDRESS_GROUP = ReferentGroup(
    (
        ReferentEntity("大家", ("大家",), ("da jia",)),
        ReferentEntity("但是", ("但是",), ("dan shi",)),
    ),
    audio_verify_all_surfaces=True,
    uncertain_keep_canonicals=("大家",),
    positions=("clip_initial",),
)


def _uncertain_verifier(request):
    return {
        "schema_version": "chat-entity-verdict.v1",
        "request_sha256": request["request_sha256"],
        "status": "UNCERTAIN",
        "reason_code": "ENTITY_AUDIO_UNCERTAIN",
    }


def test_clip_initial_address_mishear_repaired_by_audio_forced_choice():
    """2026-07-10 彩排实案：片首「但是她们要提前去彩排了」实为开场称呼「大家」。
    软性原则曾漏修一次 → 位置门控组 + 音频强制二选一是确定性升级。"""
    source = _srt("但是她们要提前去彩排了", "但是我先挂一下")

    output, audit = apply_audio_entity_verification(
        source,
        referent_groups=[OPENING_ADDRESS_GROUP],
        entity_verifier=_audio_entity_verifier("大家"),
    )

    assert audit["status"] == "APPLIED_AND_VERIFIED", audit
    assert "大家她们要提前去彩排了" in output
    assert "但是我先挂一下" in output  # 位置门控组不管辖非片首 cue
    assert len(audit["repairs"]) == 1


def test_clip_initial_group_ignores_mid_cue_and_later_cues():
    """位置门 = 片首 cue 且句首槽位：句中「但是」与后续 cue 一律不触发裁决。"""
    calls: list[dict] = []

    def counting(request):
        calls.append(dict(request))
        return None

    source = _srt("我觉得但是话说回来", "但是大家听我说")

    output, audit = apply_audio_entity_verification(
        source, referent_groups=[OPENING_ADDRESS_GROUP], entity_verifier=counting
    )

    assert output == source
    assert audit["status"] == "NO_ENTITY", audit
    assert calls == []


def test_clip_initial_uncertain_keeps_dajia_but_blocks_danshi():
    kept_src = _srt("大家早上好")
    output, audit = apply_audio_entity_verification(
        kept_src, referent_groups=[OPENING_ADDRESS_GROUP], entity_verifier=_uncertain_verifier
    )
    assert output == kept_src
    assert audit["status"] != "ENTITY_VERDICT_REQUIRED", audit
    assert audit["confirmed"][0]["reason_code"] == "ENTITY_CANONICAL_KEPT_ON_UNCERTAIN"

    blocked_src = _srt("但是她们要提前去彩排了")
    output2, audit2 = apply_audio_entity_verification(
        blocked_src, referent_groups=[OPENING_ADDRESS_GROUP], entity_verifier=_uncertain_verifier
    )
    assert output2 == blocked_src
    assert audit2["status"] == "ENTITY_VERDICT_REQUIRED"


def test_positioned_groups_never_enter_chat_evidence_path():
    """弹幕/SC 里「但是/大家」是高频普通词——位置门控组不得在 chat 证据
    路径制造实体槽位或裁决要求。"""
    source = _srt("但是大家都在等她回来")

    output, audit = apply_authoritative_chat_evidence(
        source,
        [ChatEvidence("danmaku", 0, "但是大家都在等她回来")],
        referent_groups=[OPENING_ADDRESS_GROUP],
        entity_verifier=_uncertain_verifier,
    )

    assert audit.get("entity_verdict_required") in ([], None)
    assert "但是大家都在等她回来" in output


def test_mishear_surface_rescue_uncertain_keeps_resolved_rewrites():
    """2026-07-14 理论上/留下→李豆沙、苏人→素惹类：已知误听面触发音频
    二选一；真句（留下来/理论上）UNCERTAIN 保留绝不阻塞，音频确证才改写。"""
    rescue = ReferentGroup(
        (
            ReferentEntity("李豆沙", ("李豆沙", "留下", "理论上"), ("li dou sha",)),
            ReferentEntity("小李", ("小李",), ("xiao li",)),
        ),
        uncertain_keep_canonicals=("李豆沙", "小李"),
        positions=("transcript_only",),
        uncertain_keep_surfaces=("留下", "理论上"),
    )
    source = _srt("理论上会打个电话", "大家可以留下来看看")

    # 音频拿不准 → 两个误听面都保留原文，绝不阻塞
    output, audit = apply_audio_entity_verification(
        source, referent_groups=[rescue], entity_verifier=_uncertain_verifier
    )
    assert output == source
    assert audit["status"] != "ENTITY_VERDICT_REQUIRED", audit
    assert all(
        row["reason_code"] == "ENTITY_SURFACE_KEPT_ON_UNCERTAIN"
        for row in audit["confirmed"]
    )

    # 音频确证听到「李豆沙」→ 改写
    misheard = _srt("理论上要被关一辈子直播间")
    output2, audit2 = apply_audio_entity_verification(
        misheard, referent_groups=[rescue], entity_verifier=_audio_entity_verifier("李豆沙")
    )
    assert "李豆沙要被关一辈子直播间" in output2
    assert audit2["status"] == "APPLIED_AND_VERIFIED", audit2

    # 无误听面的普通句零成本：不触发任何裁决
    calls: list[int] = []

    def counting(request):
        calls.append(1)
        return None

    plain = _srt("小李今天想吃火锅", "李豆沙说好")
    output3, audit3 = apply_audio_entity_verification(
        plain, referent_groups=[rescue], entity_verifier=counting
    )
    assert output3 == plain
    assert calls == []
    assert audit3["status"] == "NO_ENTITY"


def test_loader_parses_uncertain_keep_surfaces_from_asset():
    groups = load_referent_groups(
        Path(__file__).resolve().parents[1] / "assets/lidousha/entity_confusables.json"
    )
    rescue = next(
        g for g in groups
        if "留下" in {s for e in g.entities for s in e.surfaces}
    )
    sure = next(
        g for g in groups if {e.canonical for e in g.entities} == {"素惹", "素人"}
    )

    assert {"留下", "理论上", "小雨"} <= set(rescue.uncertain_keep_surfaces)
    assert rescue.positions == ("transcript_only",)
    assert set(sure.uncertain_keep_surfaces) == {"苏人", "苏惹"}


def test_single_char_surfaces_never_form_slots():
    """2026-07-14 乐队番案：话题图组的单字面「灯」把「粉丝灯牌」命中成
    高松灯候选并阻塞整条——实体面最短两字。"""
    group = ReferentGroup(
        (
            ReferentEntity("高松灯", ("高松灯", "灯"), ("takamatsu tomori",)),
            ReferentEntity("千早爱音", ("千早爱音",), ("anon",)),
        ),
        audio_verify_all_surfaces=True,
    )
    calls: list[int] = []

    def counting(request):
        calls.append(1)
        return None

    source = _srt("谢谢猴桃酷拉我的粉丝灯牌")
    output, audit = apply_audio_entity_verification(
        source, referent_groups=[group], entity_verifier=counting
    )

    assert output == source
    assert audit["status"] == "NO_ENTITY", audit
    assert calls == []


def test_static_group_canonicals_keep_on_uncertain_after_kmx_generalization():
    """kmx 先例推广回归：文本已是「梦限大」「恋死」等规范形时，供应商断供
    (UNCERTAIN) 不得阻塞；误听面（梦现代）UNCERTAIN 照旧阻塞。"""
    groups = load_referent_groups(
        Path(__file__).resolve().parents[1] / "assets/lidousha/entity_confusables.json"
    )
    dream = next(
        g for g in groups if {e.canonical for e in g.entities} == {"梦限大", "Ave Mujica"}
    )

    canonical_src = _srt("但是我确实很想跟大家看梦限大")
    output, audit = apply_audio_entity_verification(
        canonical_src, referent_groups=[dream], entity_verifier=_uncertain_verifier
    )
    assert output == canonical_src
    assert audit["status"] != "ENTITY_VERDICT_REQUIRED", audit

    mishear_src = _srt("怎么有人说有梦现代的风险")
    output2, audit2 = apply_audio_entity_verification(
        mishear_src, referent_groups=[dream], entity_verifier=_uncertain_verifier
    )
    assert output2 == mishear_src
    assert audit2["status"] == "ENTITY_VERDICT_REQUIRED"


def test_auditor_pair_adjudication_semantics_via_engine():
    """审片员自定夺（Ivan 2026-07-14）：非同音建议交黑帧二选一——RESOLVED=
    建议→改写目标 cue；UNCERTAIN→双向保留不阻塞；其他 cue 不受影响。"""
    import dataclasses as _dc

    pair = ReferentGroup(
        (
            ReferentEntity("罗莎", ("罗莎",), ()),
            ReferentEntity("豆沙", ("豆沙",), ()),
        ),
        audio_verify_all_surfaces=True,
        uncertain_keep_canonicals=("罗莎", "豆沙"),
        positions=("transcript_only",),
    )
    source = _srt("让罗莎把kmx扛起来", "罗莎在别的句子里")

    output, audit = apply_audio_entity_verification(
        source,
        referent_groups=[_dc.replace(pair, positions=("transcript_only",))],
        entity_verifier=_audio_entity_verifier("豆沙"),
        excluded_cue_indexes={2},
    )
    assert "让豆沙把kmx扛起来" in output
    assert "罗莎在别的句子里" in output  # 只裁目标 cue
    assert audit["status"] == "APPLIED_AND_VERIFIED", audit

    output2, audit2 = apply_audio_entity_verification(
        source,
        referent_groups=[pair],
        entity_verifier=_uncertain_verifier,
        excluded_cue_indexes={2},
    )
    assert output2 == source
    assert audit2["status"] != "ENTITY_VERDICT_REQUIRED", audit2


def test_introduced_term_compiler_flags_injections_with_aligned_spans():
    """2026-07-14 乐队番实案抽象：记录修正层把钦定词(Ave Mujica/睦睦)
    注入 draft 没有的位置及对应 draft 片段；单字对齐段向左扩。"""
    draft = _srt("不需要会打鼓的梦", "月月不是算妈妈吗", "正常句子")
    final = _srt("不需要会打鼓的，Ave Mujica", "睦睦不是算妈妈吗", "正常句子")

    rows = introduced_term_cues(draft, final, {"Ave Mujica", "睦睦", "梦限大"})

    by_term = {row["term"]: row for row in rows}
    assert "Ave Mujica" in by_term
    assert by_term["Ave Mujica"]["cue_index"] == 1
    assert "梦" in by_term["Ave Mujica"]["draft_span"]
    assert len(by_term["Ave Mujica"]["draft_span"]) >= 2  # 单字左扩
    assert "睦睦" in by_term
    assert by_term["睦睦"]["draft_span"] == "月月"
    # draft 本来就有该词的不算注入
    assert introduced_term_cues(final, final, {"Ave Mujica", "睦睦"}) == []


def test_multi_group_same_cue_arbitrated_independently():
    """2026-07-14 梦限大坏女人案 cue31「海铃的假哭和那个にゃむち的」：一 cue
    命中多个不同实体组不构成歧义，各组各自单槽正常裁。"""
    hailing = ReferentGroup(
        (
            ReferentEntity("海铃", ("海铃", "海玲"), ("hai ling",)),
            ReferentEntity("海底", ("海底",), ("hai di",)),
        ),
        audio_verify_all_surfaces=True,
    )
    nyamu = ReferentGroup(
        (
            ReferentEntity("にゃむ", ("にゃむ", "娘木"), ("nya mu",)),
            ReferentEntity("尼亚", ("尼亚",), ("ni ya",)),
        ),
        audio_verify_all_surfaces=True,
    )
    calls: list[str] = []

    def verifier(request):
        canonical = request["candidate_entities"][0]["canonical"]
        calls.append(canonical)
        return {
            "schema_version": "chat-entity-verdict.v1",
            "request_sha256": request["request_sha256"],
            "status": "RESOLVED",
            "canonical_entity": request["transcript_canonical"],
            "authority_kind": "audio_forced_choice",
            "confidence": 0.95,
            "heard_syllables": "clear",
            "source_media_sha256": "a" * 64,
            "audio_clip_sha256": "b" * 64,
            "prompt_sha256": "c" * 64,
            "response_sha256": "d" * 64,
        }

    source = _srt("海铃的假哭和那个にゃむ的")
    output, audit = apply_audio_entity_verification(
        source, referent_groups=[hailing, nyamu], entity_verifier=verifier
    )

    assert audit["status"] != "ENTITY_VERDICT_REQUIRED", audit
    assert output == source  # 双双确认为本组规范形，无改写
    assert len(calls) == 2  # 两组各自独立裁决


def test_same_group_double_canonical_passes_without_keep_membership():
    """2026-07-14 乐队番案 cue28「限大，梦限大直接…」类：同组规范形复数出现
    且无误听形 = 无可改写，直接放行——不再要求 uncertain_keep 成员资格。"""
    group = ReferentGroup(
        DREAM_MUJICA_GROUP.entities, audio_verify_all_surfaces=True
    )
    source = _srt("梦限大，梦限大直接给大家推出一个究极坏女人")

    output, audit = apply_audio_entity_verification(
        source, referent_groups=[group], entity_verifier=None
    )

    assert output == source
    assert audit["status"] != "ENTITY_VERDICT_REQUIRED", audit
    assert audit["confirmed"][0]["reason_code"] == "ENTITY_ALREADY_CANONICAL_EVERYWHERE"


def test_alias_group_multi_character_cue_passes_without_arbitration():
    """2026-07-14 梦限大 cue31 案：「海铃的假哭和那个祥子的」命中话题图组
    两个角色别名（surface≠全名 canonical 是图组的构造常态）——别名组多槽位
    =一句提多个角色，无可改写直接放行；静态误听面组不受此宽免。"""
    graph_group = ReferentGroup(
        (
            ReferentEntity("八幡海铃", ("八幡海铃", "海铃"), ("yahata umiri",)),
            ReferentEntity("丰川祥子", ("丰川祥子", "祥子"), ("togawa sakiko",)),
        ),
        audio_verify_all_surfaces=True,
        alias_surfaces=True,
    )
    calls: list[int] = []

    def counting(request):
        calls.append(1)
        return None

    source = _srt("然后海铃的假哭和那个祥子的")
    output, audit = apply_audio_entity_verification(
        source, referent_groups=[graph_group], entity_verifier=counting
    )

    assert output == source
    assert audit["status"] != "ENTITY_VERDICT_REQUIRED", audit
    assert audit["confirmed"][0]["reason_code"] == "ENTITY_ALREADY_CANONICAL_EVERYWHERE"
    assert calls == []


def test_same_group_multi_occurrence_with_mishear_still_blocks():
    source = _srt("母鸡卡还是梦限大我分不清")

    output, audit = apply_audio_entity_verification(
        source, referent_groups=[DREAM_MUJICA_GROUP], entity_verifier=None
    )

    assert output == source
    assert audit["status"] == "ENTITY_VERDICT_REQUIRED"
    assert audit["entity_verdict_required"][0]["reason_code"] == "TRANSCRIPT_ENTITY_SLOT_AMBIGUOUS"


WD_GROUP = ReferentGroup(
    (
        ReferentEntity("李豆沙", ("李豆沙",), ("li dou sha",)),
        ReferentEntity("小李", ("小李",), ("xiao li",)),
    ),
    audio_verify_all_surfaces=True,
    uncertain_keep_canonicals=("李豆沙", "小李"),
    positions=("witness_disagreement",),
)


def test_witness_disagreement_compiler_flags_introduced_forms_only():
    """2026-07-14 生日结婚实案：BCUT 逐字证人听成「留下」，带弹幕上下文的
    二听把它写成「小李」——引入形态才可疑；两侧都在场的形态不打扰。"""
    draft = _srt("所以你是想看留下跟别人亲亲", "小李小李抱抱李", "李豆沙又要直播了")
    final = _srt("所以你是想看小李跟别人亲亲", "小李小李抱抱李", "李豆沙又要直播了")

    assert witness_disagreement_cues(draft, final, WD_GROUP) == [1]
    # 时轴对不上的 cue 宁缺毋滥
    shifted = "1\n00:01:39,000 --> 00:01:43,000\n完全另一个时间轴的句子\n"
    assert witness_disagreement_cues(shifted, final, WD_GROUP) == []


def test_witness_disagreement_arbitration_rewrites_only_suspicious_cues():
    import dataclasses as _dc

    draft = _srt("所以你是想看留下跟别人亲亲", "还是想要留下直播", "小李小李抱抱李")
    final = _srt("所以你是想看小李跟别人亲亲", "还是想要小李直播", "小李小李抱抱李")
    suspicious = witness_disagreement_cues(draft, final, WD_GROUP)
    assert suspicious == [1, 2]
    excluded = ({1, 2, 3} - set(suspicious)) | {3}

    output, audit = apply_audio_entity_verification(
        final,
        referent_groups=[_dc.replace(WD_GROUP, positions=("transcript_only",))],
        entity_verifier=_audio_entity_verifier("李豆沙"),
        excluded_cue_indexes=excluded,
    )

    assert audit["status"] == "APPLIED_AND_VERIFIED", audit
    assert "所以你是想看李豆沙跟别人亲亲" in output
    assert "还是想要李豆沙直播" in output
    assert "小李小李抱抱李" in output  # 弹幕逐字 cue 被排除，不复审

    # UNCERTAIN 双向保留：两个形态都在默认可信方列表，绝不阻塞。
    output2, audit2 = apply_audio_entity_verification(
        final,
        referent_groups=[_dc.replace(WD_GROUP, positions=("transcript_only",))],
        entity_verifier=_uncertain_verifier,
        excluded_cue_indexes=excluded,
    )
    assert output2 == final
    assert audit2["status"] != "ENTITY_VERDICT_REQUIRED", audit2


def test_self_reference_wd_group_loads_from_asset():
    groups = load_referent_groups(
        Path(__file__).resolve().parents[1] / "assets/lidousha/entity_confusables.json"
    )
    group = next(
        g for g in groups if {e.canonical for e in g.entities} == {"李豆沙", "小李"}
    )

    assert group.positions == ("witness_disagreement",)
    assert set(group.uncertain_keep_canonicals) == {"李豆沙", "小李"}


def test_loader_parses_known_positions_and_drops_unknown(tmp_path):
    doc = {
        "schema_version": "lidousha-referent-groups.v2",
        "groups": [
            {
                "audio_verify_all_surfaces": True,
                "positions": ["clip_initial", "transcript_only", "made_up"],
                "entities": [
                    {"canonical": "甲", "surfaces": [], "readings": ["jia"]},
                    {"canonical": "乙", "surfaces": [], "readings": ["yi"]},
                ],
            }
        ],
    }
    path = tmp_path / "groups.json"
    path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")

    groups = load_referent_groups(path)

    assert groups[0].positions == ("clip_initial", "transcript_only")


OPENING_CONFIG = {
    "connectives": [
        {"surface": "但是", "readings": ["dan shi"]},
        {"surface": "就是", "readings": ["jiu shi"]},
    ],
    "addresses": [
        {"canonical": "大家", "readings": ["da jia"]},
        {"canonical": "各位", "readings": ["ge wei"]},
    ],
    "reason": "test",
}


def test_clip_opening_builder_compiles_connective_opening_into_group():
    """位置先验编译器：任何连词开场都可疑，不再逐词对建表。"""
    group = clip_opening_address_group(_srt("但是她们要提前去彩排了"), OPENING_CONFIG)

    assert group is not None
    assert {entity.canonical for entity in group.entities} == {"但是", "大家", "各位"}
    assert group.positions == ("clip_initial",)
    # UNCERTAIN（含供应商故障）保留连词原文、绝不阻塞：连词开场是合法高频口语。
    assert group.uncertain_keep_canonicals == ("但是",)

    assert clip_opening_address_group(_srt("大家早上好"), OPENING_CONFIG) is None
    assert clip_opening_address_group(_srt("我觉得但是这个说法不对"), OPENING_CONFIG) is None
    assert clip_opening_address_group(_srt("但是她们来了"), None) is None


def test_clip_opening_group_uncertain_never_blocks_production():
    """通用版与静态词对的关键差异：配额断供时 UNCERTAIN 不得阻塞整条产线。"""
    source = _srt("然后她们要提前去彩排了")
    config = {
        "connectives": [{"surface": "然后", "readings": ["ran hou"]}],
        "addresses": OPENING_CONFIG["addresses"],
    }
    group = clip_opening_address_group(source, config)
    assert group is not None

    output, audit = apply_audio_entity_verification(
        source, referent_groups=[group], entity_verifier=_uncertain_verifier
    )

    assert output == source
    assert audit["status"] != "ENTITY_VERDICT_REQUIRED", audit
    assert audit["confirmed"][0]["reason_code"] == "ENTITY_CANONICAL_KEPT_ON_UNCERTAIN"


def test_clip_opening_end_to_end_repairs_via_shared_engine():
    source = _srt("但是她们要提前去彩排了", "但是我先挂一下")
    group = clip_opening_address_group(source, OPENING_CONFIG)

    output, audit = apply_audio_entity_verification(
        source, referent_groups=[group], entity_verifier=_audio_entity_verifier("大家")
    )

    assert audit["status"] == "APPLIED_AND_VERIFIED", audit
    assert "大家她们要提前去彩排了" in output
    assert "但是我先挂一下" in output


def test_repetition_divergence_compiles_single_span_pairs_only():
    """重复一致性编译器：无词表，自动覆盖未见过的实例（睡衣/素颜只是实例）。"""
    source = _srt(
        "今天穿的是睡衣哦",
        "今天穿的是素颜哦",
        "谢谢晚照",
        "谢谢利安",
        "完全不一样的一句话在这里",
    )

    groups = repetition_divergence_groups(source)

    assert len(groups) == 1
    assert {entity.canonical for entity in groups[0].entities} == {"睡衣", "素颜"}
    assert groups[0].positions == ("transcript_only",)
    # UNCERTAIN 双向保留：两个变体都在默认可信方列表。
    assert set(groups[0].uncertain_keep_canonicals) == {"睡衣", "素颜"}
    # 短句（谢谢A/谢谢B）低于最小长度不成组——真·答谢复读不受干扰。


def test_repetition_divergence_end_to_end_audio_winner_takes_both():
    source = _srt("今天穿的是睡衣哦", "今天穿的是素颜哦")
    groups = repetition_divergence_groups(source)

    output, audit = apply_audio_entity_verification(
        source, referent_groups=groups, entity_verifier=_audio_entity_verifier("睡衣")
    )

    assert audit["status"] == "APPLIED_AND_VERIFIED", audit
    assert output.count("睡衣") == 2
    assert "素颜" not in output
    assert len(audit["repairs"]) == 1


def test_repetition_divergence_uncertain_keeps_both_without_blocking():
    source = _srt("今天穿的是睡衣哦", "今天穿的是素颜哦")
    groups = repetition_divergence_groups(source)

    output, audit = apply_audio_entity_verification(
        source, referent_groups=groups, entity_verifier=_uncertain_verifier
    )

    assert output == source
    assert audit["status"] != "ENTITY_VERDICT_REQUIRED", audit


def test_exact_chat_owned_cue_is_excluded_from_second_entity_verdict():
    source = _srt("等小李什么时候来看恋青呢")
    group = ReferentGroup(
        (ReferentEntity("恋青", ("恋青",)), ReferentEntity("恋死", ("恋死",))),
        audio_verify_all_surfaces=True,
    )

    output, audit = apply_audio_entity_verification(
        source,
        referent_groups=[group],
        entity_verifier=None,
        excluded_cue_indexes=[1],
    )

    assert output == source
    assert audit["status"] == "NO_ENTITY"


def test_jsonl_uses_live_event_timestamps_not_ingestion_send_time(tmp_path):
    segment = tmp_path / "22966160_20260710-19-00-17.mp4"
    start_ms = int(
        datetime(2026, 7, 10, 19, 0, 17, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp() * 1000
    )
    jsonl = segment.with_suffix(".jsonl")
    rows = [
        {
            "cmd": "DANMU_MSG",
            "send_time": (start_ms + 12_000) / 1000,
            "info": [[0, 0, 0, 0, (start_ms + 1_049_933) / 1000], "等小李什么时候来看恋青呢"],
        },
        {
            "cmd": "SUPER_CHAT_MESSAGE",
            "send_time": start_ms + 535_272,
            "data": {
                "id": 17439760,
                "ts": (start_ms + 535_272) / 1000,
                "message": "如果能唱的到想点首小城夏天，唱不到就算了",
                "user_info": {"uname": "十麻乃orient"},
            },
        },
        {
            "cmd": "SUPER_CHAT_MESSAGE_JPN",
            "data": {
                # Second-precision localized twin sorts before the precise CN
                # event but must be replaced by its 535272ms authority.
                "ts": (start_ms + 535_000) // 1000,
                "id": 17439760,
                "message": "如果能唱的到想点首小城夏天，唱不到就算了",
                "user_info": {"uname": "十麻乃orient"},
            },
        },
    ]
    jsonl.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")

    assert recording_start_epoch_ms(segment) == start_ms
    items = load_chat_jsonl(jsonl, recording_start_ms=start_ms)
    assert [(item.kind, item.offset_ms, item.text) for item in items] == [
        ("superchat", 535_272, "如果能唱的到想点首小城夏天，唱不到就算了"),
        ("danmaku", 1_049_933, "等小李什么时候来看恋青呢"),
    ]


def test_recording_start_prefers_blrec_meta_over_filename(tmp_path):
    segment = tmp_path / "22966160_20260710-20-00-09.mp4"
    segment.with_suffix(".meta.json").write_text(
        json.dumps(
            {
                "description": {
                    "RecordStartTime": "2026-07-10 20:00:09+08:00"
                }
            }
        ),
        encoding="utf-8",
    )
    expected = int(
        datetime(2026, 7, 10, 20, 0, 9, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp() * 1000
    )

    assert recording_start_epoch_ms(segment) == expected


def test_zero_byte_xml_falls_back_to_sibling_jsonl(tmp_path):
    segment = tmp_path / "22966160_20260710-19-00-17.mp4"
    segment.write_bytes(b"media")
    segment.with_suffix(".xml").write_bytes(b"")
    start_ms = recording_start_epoch_ms(segment)
    segment.with_suffix(".jsonl").write_text(
        json.dumps(
            {
                "cmd": "DANMU_MSG",
                "info": [[0, 0, 0, 0, (start_ms + 1_049_933) / 1000], "等小李什么时候来看恋青呢"],
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    evidence = _piece_chat_evidence(
        {
            "remote_media": str(segment),
            "start_ms": 1_058_350,
            "end_ms": 1_249_000,
            "danmaku_xml_local": str(segment.with_suffix(".xml")),
        }
    )
    assert [(item.kind, item.offset_ms, item.text) for item in evidence] == [
        ("danmaku", 1_049_933, "等小李什么时候来看恋青呢")
    ]


def test_healthy_xml_is_hashed_and_kept_as_danmaku_authority(tmp_path):
    segment = tmp_path / "22966160_20260710-19-00-17.mp4"
    segment.write_bytes(b"media")
    xml = segment.with_suffix(".xml")
    xml.write_text(
        "<?xml version='1.0' encoding='utf-8'?><i>"
        '<d p="4.487,1,25,16777215,1782961247649,0,ea71f718,578340245">恋青</d>'
        "</i>",
        encoding="utf-8",
    )
    segment.with_suffix(".jsonl").write_text("", encoding="utf-8")

    evidence = _piece_chat_evidence(
        {
            "remote_media": str(segment),
            "danmaku_xml_local": str(xml),
        }
    )

    assert [(item.kind, item.offset_ms, item.text) for item in evidence] == [
        ("danmaku", 4_487, "恋青")
    ]
    assert evidence[0].source == str(xml)
    assert evidence[0].source_sha256 == hashlib.sha256(xml.read_bytes()).hexdigest()


def test_hash_bound_chat_uses_jsonl_origin_and_declared_alias_offset(tmp_path):
    remote = tmp_path / "22966160_20260722-19-34-50.mp4"
    remote.write_bytes(b"official replay")
    jsonl = tmp_path / "22966160_20260722-19-35-15.jsonl"
    canonical_start_ms = int(
        datetime(
            2026,
            7,
            22,
            19,
            35,
            15,
            tzinfo=ZoneInfo("Asia/Shanghai"),
        ).timestamp()
        * 1000
    )
    jsonl.write_text(
        json.dumps(
            {
                "cmd": "DANMU_MSG",
                "info": [
                    [0, 0, 0, 0, (canonical_start_ms + 60_000) / 1000],
                    "南町nightin",
                ],
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    digest = "sha256:" + hashlib.sha256(jsonl.read_bytes()).hexdigest()

    evidence = _piece_chat_evidence(
        {
            "remote_media": str(remote),
            "chat_jsonl_local": str(jsonl),
            "chat_jsonl_sha256": digest,
            "chat_origin_epoch_ms": canonical_start_ms,
            "chat_timeline_offset_ms": 37,
            "structured_chat_required": True,
            "chat_binding_status": "BOUND_SOURCE_ALIAS",
        }
    )

    assert [(item.kind, item.offset_ms, item.text) for item in evidence] == [
        ("danmaku", 60_037, "南町nightin")
    ]


def test_hash_bound_chat_rejects_missing_or_drifted_sidecar(tmp_path):
    missing = tmp_path / "missing.jsonl"
    with pytest.raises(
        StructuredChatEvidenceError,
        match="STRUCTURED_CHAT_BINDING_PATH_MISSING",
    ):
        _piece_chat_evidence(
            {
                "remote_media": str(tmp_path / "source.mp4"),
                "chat_jsonl_local": str(missing),
                "chat_jsonl_sha256": "sha256:" + "a" * 64,
                "chat_origin_epoch_ms": 1_750_000_000_000,
                "chat_timeline_offset_ms": 0,
                "structured_chat_required": True,
            }
        )

    jsonl = tmp_path / "22966160_20260722-19-35-15.jsonl"
    jsonl.write_text(
        '{"cmd":"DANMU_MSG","info":[[0,0,0,0,1750000000],"证据"]}\n',
        encoding="utf-8",
    )
    with pytest.raises(
        StructuredChatEvidenceError,
        match="STRUCTURED_CHAT_BINDING_SHA256_MISMATCH",
    ):
        _piece_chat_evidence(
            {
                "remote_media": str(tmp_path / "source.mp4"),
                "chat_jsonl_local": str(jsonl),
                "chat_jsonl_sha256": "sha256:" + "b" * 64,
                "chat_origin_epoch_ms": 1_750_000_000_000,
                "chat_timeline_offset_ms": 0,
                "structured_chat_required": True,
            }
        )


def test_hash_bound_chat_classifies_unavailable_source_root(tmp_path):
    unavailable = tmp_path / "unmounted" / "date" / "recording.jsonl"
    with pytest.raises(
        StructuredChatEvidenceError,
        match="SOURCE_RECORDING_ROOT_UNAVAILABLE",
    ):
        _piece_chat_evidence(
            {
                "remote_media": str(unavailable.with_suffix(".mp4")),
                "chat_jsonl_local": str(unavailable),
                "chat_jsonl_sha256": "sha256:" + "a" * 64,
                "chat_origin_epoch_ms": 1_750_000_000_000,
                "chat_timeline_offset_ms": 0,
                "structured_chat_required": True,
            }
        )


def test_optional_legacy_piece_without_chat_sidecar_stays_compatible(tmp_path):
    assert (
        _piece_chat_evidence(
            {
                "remote_media": str(tmp_path / "legacy.mp4"),
                "structured_chat_required": False,
                "chat_binding_status": "OPTIONAL_ABSENT",
            }
        )
        == []
    )


def test_exact_danmaku_read_replaces_asr_span():
    source = _srt("等小室什么时候来看恋死呢", "恋死我自己有看了")
    evidence = [ChatEvidence("danmaku", -8_000, "等小李什么时候来看恋青呢")]

    output, audit = apply_authoritative_chat_evidence(
        source,
        evidence,
        support_srt_texts=[source],
        referent_groups=[["恋青", "恋死"]],
        entity_verifier=_audio_entity_verifier("恋青"),
    )

    assert "等小李什么时候来看恋青呢" in output
    assert "等小室什么时候来看恋死呢" not in output
    assert "恋青我自己有看了" in output
    assert "恋死我自己有看了" not in output
    assert audit["coreference_repairs"][0]["expected_entity"] == "恋青"
    assert audit["status"] == "APPLIED_AND_VERIFIED"


def test_exact_sc_is_split_across_existing_cue_timeline():
    source = _srt("假如我能唱到", "想点首小城夏天", "唱不同的")
    exact = "如果能唱的到想点首小城夏天，唱不到就算了"

    output, audit = apply_authoritative_chat_evidence(
        source,
        [ChatEvidence("superchat", -120_000, exact, "十麻乃orient")],
        support_srt_texts=[
            source,
            _srt("如果能唱得到", "想点首小城夏天", "唱不到就算了"),
        ],
    )

    assert "如果" in output
    assert "唱不到就算了" in output
    rendered_text = "".join(cue.text for cue in parse_srt_cues(output))
    assert exact.replace("，", "") == rendered_text.replace("，", "")
    assert audit["applied"][0]["cue_indexes"] == [1, 2, 3]
    assert audit["status"] == "APPLIED_AND_VERIFIED"


def test_dropped_question_particle_is_restored_without_deleting_same_cue_reply():
    source = _srt("恋死看，我们已经看了")

    output, audit = apply_authoritative_chat_evidence(
        source,
        [ChatEvidence("danmaku", 0, "恋死看吗")],
        support_srt_texts=[source],
    )

    assert "恋死看吗，我们已经看了" in output
    assert audit["applied"][0]["mode"] == "question_particle_patch"


def test_exact_read_prefix_does_not_delete_same_cue_reply():
    source = _srt("恋死看吗，我们已经看了")

    output, audit = apply_authoritative_chat_evidence(
        source,
        [ChatEvidence("danmaku", 0, "恋死看吗")],
        support_srt_texts=[source],
    )

    assert "恋死看吗，我们已经看了" in output
    assert audit["status"] == "APPLIED_AND_VERIFIED"


def test_fuzzy_read_prefix_preserves_acoustic_reply_suffix():
    source = _srt("等小室什么时候来看恋死呢，我自己一直在看")
    exact = "等小李什么时候来看恋青呢"

    output, audit = apply_authoritative_chat_evidence(
        source,
        [ChatEvidence("danmaku", 0, exact)],
        support_srt_texts=[source],
    )

    assert exact in output
    assert "我自己一直在看" in output
    assert audit["status"] == "APPLIED_AND_VERIFIED"


def test_message_final_particle_split_into_next_cue_is_not_duplicated():
    exact = "等小李什么时候来看恋青呢"
    source = _srt("等小李什么时候来看恋青", "呢，我自己一直在看")

    output, audit = apply_authoritative_chat_evidence(
        source,
        [ChatEvidence("danmaku", 0, exact)],
        support_srt_texts=[source],
    )

    rendered = "".join(cue.text for cue in parse_srt_cues(output))
    assert rendered.count("呢") == 1
    assert "我自己一直在看" in output
    assert audit["applied"][0]["cue_indexes"] == [1, 2]


def test_multi_cue_sc_read_preserves_reply_suffix_in_last_cue():
    source = _srt(
        "假如我能唱到想点首小城夏天",
        "唱不同的，我唱不了高音",
    )
    exact = "如果能唱的到想点首小城夏天，唱不到就算了"

    output, audit = apply_authoritative_chat_evidence(
        source,
        [ChatEvidence("superchat", 0, exact, "十麻乃orient")],
        support_srt_texts=[
            source,
            _srt("如果能唱得到想点首小城夏天", "唱不到就算了"),
        ],
    )

    assert exact.replace("，", "") in "".join(cue.text for cue in parse_srt_cues(output)).replace("，", "")
    assert "我唱不了高音" in output
    assert "同的" not in output
    assert audit["status"] == "APPLIED_AND_VERIFIED"


def test_partial_read_does_not_inject_unspoken_message_tail():
    source = _srt("今天天气很好请大家记得")
    exact = "今天天气很好请大家记得早点休息晚安"

    output, audit = apply_authoritative_chat_evidence(
        source,
        [ChatEvidence("danmaku", 0, exact)],
        support_srt_texts=[source],
    )

    assert output == source
    assert "早点休息晚安" not in output
    assert audit["status"] == "NO_MATCH"


def test_sc_read_does_not_duplicate_spoken_prefix_or_eat_followup():
    """2026-07-11 乐队番实案：SC=「好冷的笑话，另外姐姐姐姐组乐队吗，我会打退堂鼓」。
    整段覆盖曾把上一条 cue 已说过的「好冷的笑话」重复注入 span 头、把 span 尾
    cue 主播自己的「退堂鼓算什么」覆盖成「退堂鼓」、还切出「，我会打」这种
    闭标点开头的 cue。对齐拼接三个都必须修掉。"""
    exact = "好冷的笑话，另外姐姐姐姐组乐队吗，我会打退堂鼓"
    source = _srt(
        "谢谢谢谢寒-歌的钢镚，好冷的笑话",
        "另外姐姐姐姐组乐队吗",
        "我会打退堂鼓",
        "退堂鼓算什么",
    )

    output, audit = apply_authoritative_chat_evidence(
        source,
        [ChatEvidence("superchat", -101_240, exact, "寒-歌")],
        support_srt_texts=[source],
    )

    texts = [cue.text for cue in parse_srt_cues(output)]
    joined = "".join(texts)
    assert joined.count("好冷的笑话") == 1, texts
    assert "退堂鼓算什么" in joined, texts
    assert "另外姐姐姐姐组乐队吗" in joined, texts
    assert not any(t.startswith(("，", ",")) for t in texts), texts
    assert audit["applied"], audit["status"]
    assert audit["applied"][0]["span_alignment"] is not None
    # 2026-07-13 生产实况：内部自检曾要求 authority 全文 ∈ 跨度 → 弃置重复头
    # 的对齐拼接被自己判 FAILED、整条成品打回。status 必须绿。
    assert audit["applied"][0]["survived"] is True
    assert audit["status"] == "APPLIED_AND_VERIFIED", audit["status"]


def test_danmaku_near_miss_is_arbitrated_by_audio_and_restored():
    """2026-07-11 实案：弹幕「乐队不是需要妈妈吗」被 ASR 写成「立希不是算妈妈吗」
    （score 0.582 / coverage 0.556 / common 5 —— 三道 exact_span 门各差一点，而
    "独立转写支持"来自同一个听错的 ASR 家族）。音频二选一 RESOLVED=弹幕原文
    时逐字修复。"""
    danmaku = "乐队不是需要妈妈吗"
    source = _srt("立希不是算妈妈吗", "当然也算妈妈")

    output, audit = apply_authoritative_chat_evidence(
        source,
        [ChatEvidence("danmaku", 0, danmaku)],
        support_srt_texts=[source],
        entity_verifier=_audio_entity_verifier(danmaku),
    )

    texts = [cue.text for cue in parse_srt_cues(output)]
    assert texts[0] == "乐队不是需要妈妈吗", texts
    assert texts[1] == "当然也算妈妈"
    assert audit["read_aloud_arbitrations"][0]["outcome"] == "authority_confirmed_by_audio"
    assert audit["applied"][0]["alignment_basis"] == "raw-audio-forced-choice.v1"


def test_high_confidence_danmaku_near_copy_uses_bounded_audio_arbitration():
    exact = "soyo就是妈"
    source = _srt("soyo是真妈", "已经超越妈感")

    output, audit = apply_authoritative_chat_evidence(
        source,
        [ChatEvidence("danmaku", 0, exact)],
        support_srt_texts=[],
        entity_verifier=_audio_entity_verifier(exact),
    )

    texts = [cue.text for cue in parse_srt_cues(output)]
    assert texts == ["soyo就是妈", "已经超越妈感"]
    row = audit["read_aloud_arbitrations"][0]
    assert row["outcome"] == "authority_confirmed_by_audio"
    assert row["owner_eligible"] is True
    assert row["whole_line_exact_copy_gate"]["proof_basis"] == (
        "hash_bound_full_span_audio_verdict"
    )
    applied = audit["applied"][0]
    assert applied["owner_eligible"] is True
    assert applied["whole_line_exact_copy_gate"] == row["whole_line_exact_copy_gate"]
    assert audit["status"] == "APPLIED_AND_VERIFIED"


def test_danmaku_near_miss_rejected_by_audio_keeps_asr_text():
    danmaku = "乐队不是需要妈妈吗"
    source = _srt("立希不是算妈妈吗")

    output, audit = apply_authoritative_chat_evidence(
        source,
        [ChatEvidence("danmaku", 0, danmaku)],
        entity_verifier=_audio_entity_verifier("立希不是算妈妈吗"),
    )

    assert parse_srt_cues(output)[0].text == "立希不是算妈妈吗"
    assert audit["read_aloud_arbitrations"][0]["outcome"] == "acoustic_span_confirmed_by_audio"
    assert any(
        row.get("reason_code") == "EXACT_CHAT_REJECTED_BY_READ_ALOUD_AUDIO"
        for row in audit["superseded_chat_proposals"]
    )


def test_danmaku_near_miss_without_verifier_or_uncertain_never_changes_text():
    danmaku = "乐队不是需要妈妈吗"
    source = _srt("立希不是算妈妈吗")

    output, audit = apply_authoritative_chat_evidence(
        source, [ChatEvidence("danmaku", 0, danmaku)]
    )
    assert parse_srt_cues(output)[0].text == "立希不是算妈妈吗"
    assert audit["read_aloud_arbitrations"] == []

    def uncertain(request):
        return {
            "schema_version": "chat-entity-verdict.v1",
            "request_sha256": request["request_sha256"],
            "status": "UNCERTAIN",
            "reason_code": "ENTITY_AUDIO_UNCERTAIN",
        }

    output, audit = apply_authoritative_chat_evidence(
        source, [ChatEvidence("danmaku", 0, danmaku)], entity_verifier=uncertain
    )
    assert parse_srt_cues(output)[0].text == "立希不是算妈妈吗"
    assert audit["read_aloud_arbitrations"][0]["outcome"] == "uncertain_no_change"


def test_matched_sc_repairs_only_the_explicit_thank_name_slot():
    source = _srt(
        "谢谢苏马奶送的",
        "假如我能唱到",
        "想点首小城夏天",
        "唱不同的",
    )
    exact = "如果能唱的到想点首小城夏天，唱不到就算了"

    output, audit = apply_authoritative_chat_evidence(
        source,
        # 发送先于首个念读 cue ≥4s：满足念读因果下界（发送+2s 前不可能开念）
        [ChatEvidence("superchat", 6_000, exact, "十麻乃orient", source_event_id="17439760")],
        support_srt_texts=[
            source,
            _srt(
                "谢谢十麻乃送的",
                "如果能唱的到",
                "想点首小城夏天",
                "唱不到就算了",
            ),
        ],
    )

    assert "谢谢十麻乃送的" in output
    assert "苏马奶" not in output
    assert audit["sender_repairs"][0]["source_event_id"] == "17439760"


def test_structured_sender_alias_preserves_punctuation_and_mixed_scripts():
    assert _spoken_sender_alias("寒-歌") == "寒-歌"
    assert _spoken_sender_alias("小凑るう子") == "小凑るう子"
    # This known display tag remains non-spoken for backward compatibility.
    assert _spoken_sender_alias("十麻乃orient") == "十麻乃"

    assert _repair_sc_sender("谢谢韩歌的钢镚", "寒-歌") == "谢谢寒-歌的钢镚"
    assert _repair_sc_sender("谢谢小路路口的钢镚", "小凑るう子") == (
        "谢谢小凑るう子的钢镚"
    )


def test_sc_sender_slot_covers_double_eye_gangbeng_action_without_eating_it():
    assert _repair_sc_sender("谢谢野菊的双目钢镚", "野橘未霜") == (
        "谢谢野橘未霜的双目钢镚"
    )
    assert _repair_sc_sender("谢谢波浪的光棒", "步汪汪") == "谢谢步汪汪的光棒"


def test_sc_body_splice_preserves_preceding_thank_sender_action_head():
    result = _aligned_span_replacements(
        "姐姐大人，晚上好，晚上好",
        ["谢谢波浪的光棒，姐大人", "晚上好，晚上好"],
    )

    assert result is not None
    replacements, alignment = result
    joined = "".join(replacements)
    assert joined.startswith("谢谢波浪的光棒")
    assert "姐姐大人，晚上好，晚上好" in joined
    assert alignment["preserved_thank_sender_action_head"].startswith(
        "谢谢波浪的光棒"
    )


def test_guard_buy_repairs_thank_sender_after_232140ms():
    source = (
        "1\n"
        "00:03:52,140 --> 00:03:55,000\n"
        "谢谢刚刚PANJA的舰长\n"
    )

    output, audit = apply_authoritative_chat_evidence(
        source,
        [
            ChatEvidence(
                "guard",
                0,
                "舰长",
                "panoja",
                source_event_id="guard-101",
            )
        ],
    )

    assert "谢谢刚刚panoja的舰长" in output
    assert audit["status"] == "APPLIED_AND_VERIFIED"
    assert audit["sender_repairs"][0]["alignment_basis"] == (
        "guard-buy-plus-thank-action-anchor.v1"
    )
    assert audit["sender_repairs"][0]["delay_ms"] == 232_140


def test_unique_guard_event_restores_full_mixed_script_sender_after_severe_asr_miss():
    source = "1\n00:00:50,000 --> 00:00:53,000\n谢谢小路路口的舰长\n"

    output, audit = apply_authoritative_chat_evidence(
        source,
        [
            ChatEvidence(
                "guard",
                10_000,
                "舰长",
                "小凑るう子",
                source_event_id="guard-102",
            )
        ],
    )

    assert "谢谢小凑るう子的舰长" in output
    assert audit["sender_repairs"][0]["name_match_strength"] == 1


def test_guard_buy_ambiguous_sender_fails_closed():
    source = (
        "1\n"
        "00:03:52,140 --> 00:03:55,000\n"
        "谢谢刚刚听不清的舰长\n"
    )

    output, audit = apply_authoritative_chat_evidence(
        source,
        [
            ChatEvidence("guard", 0, "舰长", "甲", source_event_id="guard-a"),
            ChatEvidence(
                "guard",
                1_000,
                "舰长",
                "乙",
                source_event_id="guard-b",
            ),
        ],
    )

    assert output == source
    assert audit["status"] == "SC_SENDER_VERDICT_REQUIRED"
    assert audit["sender_repairs"] == []
    assert audit["sender_verdict_required"][0]["reason_code"] == (
        "GUARD_BUY_SENDER_AMBIGUOUS"
    )
    assert audit["sender_verdict_required"][0]["candidate_spoken_senders"] == [
        "乙",
        "甲",
    ]


def test_matched_sc_body_makes_platform_sender_authoritative_for_thank_name_slot():
    source = _srt(
        "谢谢甲送的",
        "谢谢苏马奶送的",
        "假如我能唱到想点首小城夏天唱不同的",
    )
    exact = "如果能唱的到想点首小城夏天，唱不到就算了"

    output, audit = apply_authoritative_chat_evidence(
        source,
        [ChatEvidence("superchat", 0, exact, "十麻乃orient")],
        support_srt_texts=[source],
    )

    assert "谢谢十麻乃送的" in output
    assert "苏马奶" not in output
    assert audit["sender_repairs"][0]["alignment_basis"] == (
        "matched-superchat-body-plus-platform-sender.v1"
    )


def test_matched_sc_body_repairs_same_cue_sender_only_with_action_anchor():
    exact = "得了一种听到“是侄女”就想笑的病"
    source = _srt("左乃苏恰得了一种听到“是侄女”就想笑的病")

    output, audit = apply_authoritative_chat_evidence(
        source,
        [
            ChatEvidence(
                "superchat",
                0,
                exact,
                "十麻乃orient",
                source_event_id="sc-shimanao",
            )
        ],
        support_srt_texts=[source],
    )

    assert "十麻乃SC" in output
    assert "orient" not in output
    assert audit["sender_repairs"][0]["alignment_basis"] == (
        "matched-superchat-body-plus-action-anchor.v1"
    )


def test_matched_sc_body_does_not_invent_sender_without_spoken_action_anchor():
    exact = "得了一种听到“是侄女”就想笑的病"
    source = _srt("左乃说得了一种听到“是侄女”就想笑的病")

    output, audit = apply_authoritative_chat_evidence(
        source,
        [ChatEvidence("superchat", 0, exact, "十麻乃orient")],
        support_srt_texts=[source],
    )

    assert "十麻乃" not in output
    assert audit["sender_repairs"] == []


def test_duplicate_real_sc_body_with_different_senders_fails_closed():
    exact = "如果能唱的到想点首小城夏天，唱不到就算了"
    source = _srt("谢谢错名送的", "如果能唱的到想点首小城夏天唱不到就算了")

    output, audit = apply_authoritative_chat_evidence(
        source,
        [
            ChatEvidence("superchat", 0, exact, "甲", source_event_id="evt-a"),
            ChatEvidence("superchat", 0, exact, "乙", source_event_id="evt-b"),
        ],
        support_srt_texts=[source],
    )

    assert "谢谢错名送的" in output
    assert audit["sender_repairs"] == []
    assert audit["status"] == "SC_SENDER_VERDICT_REQUIRED"
    assert audit["sender_verdict_required"][0]["reason_code"] == (
        "DUPLICATE_SC_BODY_SENDER_AMBIGUOUS"
    )


def test_duplicate_sc_body_without_thank_name_does_not_create_sender_block():
    exact = "如果能唱的到想点首小城夏天，唱不到就算了"
    source = _srt("如果能唱的到想点首小城夏天唱不到就算了")

    _output, audit = apply_authoritative_chat_evidence(
        source,
        [
            ChatEvidence("superchat", 0, exact, "甲", source_event_id="evt-a"),
            ChatEvidence("superchat", 0, exact, "乙", source_event_id="evt-b"),
        ],
        support_srt_texts=[source],
    )

    assert audit["sender_verdict_required"] == []
    assert audit["status"] == "APPLIED_AND_VERIFIED"


def test_jsonl_yields_gift_events_with_masked_sender_and_unmasked_gift_name(tmp_path):
    """真实 2026-07-10 案例：SEND_GIFT 把赠送者昵称脱敏成"有***"(uid 0)，
    但 giftName 字段本身不脱敏——加载时必须把 giftName 当结构化证据文本，
    赠送者名原样保留（不可复原，交给 gift_repairs 只做首字核对）。"""
    segment = tmp_path / "22966160_20260710-19-00-17.mp4"
    start_ms = int(
        datetime(2026, 7, 10, 19, 0, 17, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp() * 1000
    )
    jsonl = segment.with_suffix(".jsonl")
    rows = [
        {
            "cmd": "SEND_GIFT",
            "send_time": (start_ms + 60_000) / 1000,
            "data": {
                "uname": "有***",
                "uid": 0,
                "giftName": "流星雨",
                "num": 1,
                "action": "投喂",
            },
        },
        {
            "cmd": "COMBO_SEND",
            "send_time": (start_ms + 65_000) / 1000,
            "data": {
                "uname": "有***",
                "uid": 0,
                "giftName": "流星雨",
                "combo_num": 3,
            },
        },
    ]
    jsonl.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")

    items = load_chat_jsonl(jsonl, recording_start_ms=start_ms)

    assert [(item.kind, item.offset_ms, item.text, item.sender) for item in items] == [
        ("gift", 60_000, "流星雨", "有***"),
        ("gift", 65_000, "流星雨", "有***"),
    ]


def test_jsonl_yields_guard_buy_from_event_time_and_full_structured_fields(tmp_path):
    """GUARD_BUY's data.start_time is the event clock; top-level send_time may
    be a later recorder-ingestion clock and must not move the thanks window."""

    start_ms = int(
        datetime(2026, 7, 22, 19, 34, 50, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
        * 1000
    )
    jsonl = tmp_path / "22966160_20260722-19-34-50.jsonl"
    jsonl.write_text(
        json.dumps(
            {
                "cmd": "GUARD_BUY",
                "send_time": (start_ms + 900_000) / 1000,
                "data": {
                    "start_time": (start_ms + 10_000) / 1000,
                    "username": "小凑るう子",
                    "gift_name": "舰长",
                    "guard_level": 3,
                    "uid": 101,
                },
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    items = load_chat_jsonl(jsonl, recording_start_ms=start_ms)

    assert [
        (
            item.kind,
            item.offset_ms,
            item.text,
            item.sender,
            item.source_event_id,
        )
        for item in items
    ] == [("guard", 10_000, "舰长", "小凑るう子", "101")]

    piece_items = _piece_chat_evidence(
        {
            "remote_media": str(
                tmp_path / "22966160_20260722-19-34-50.mp4"
            ),
            "chat_jsonl_local": str(jsonl),
        }
    )
    assert [
        (item.kind, item.offset_ms, item.text, item.sender)
        for item in piece_items
    ] == [("guard", 10_000, "舰长", "小凑るう子")]


def test_guard_buy_missing_identity_or_level_fails_closed(tmp_path):
    start_ms = int(
        datetime(2026, 7, 22, 19, 34, 50, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
        * 1000
    )
    jsonl = tmp_path / "22966160_20260722-19-34-50.jsonl"
    rows = [
        {
            "cmd": "GUARD_BUY",
            "data": {
                "start_time": (start_ms + 10_000) / 1000,
                "username": "无UID",
                "gift_name": "舰长",
                "guard_level": 3,
            },
        },
        {
            "cmd": "GUARD_BUY",
            "data": {
                "start_time": (start_ms + 20_000) / 1000,
                "username": "无等级",
                "gift_name": "舰长",
                "uid": 102,
            },
        },
        {
            "cmd": "GUARD_BUY",
            "send_time": (start_ms + 30_000) / 1000,
            "data": {
                "username": "无事件时间",
                "gift_name": "舰长",
                "guard_level": 3,
                "uid": 103,
            },
        },
    ]
    jsonl.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )

    assert load_chat_jsonl(jsonl, recording_start_ms=start_ms) == []


def test_gift_repair_replaces_asr_garbled_tail_on_resolved_verdict():
    """真实案例：她念读被 ASR 听成「谢谢有人看到你的人鱼」，结构化 SEND_GIFT
    的 giftName 是「流星雨」。RESOLVED>=0.80 的音频二选一才把 TAIL 换成
    giftName，且是这一处最小文本替换。"""
    source = _srt("谢谢有人看到你的人鱼")

    output, audit = apply_authoritative_chat_evidence(
        source,
        [ChatEvidence("gift", 0, "流星雨", sender="有***")],
        entity_verifier=_audio_entity_verifier("流星雨"),
    )

    assert parse_srt_cues(output)[0].text == "谢谢有人看到你的流星雨"
    row = audit["gift_repairs"][0]
    assert row["outcome"] == "gift_name_repaired"
    assert row["gift_name"] == "流星雨"
    assert row["asr_tail"] == "人鱼"
    assert row["after"] == "谢谢有人看到你的流星雨"
    assert row["sender_first_char_match"] is True


def test_gift_repair_uncertain_verdict_never_changes_text():
    source = _srt("谢谢有人看到你的人鱼")

    def uncertain(request):
        return {
            "schema_version": "chat-entity-verdict.v1",
            "request_sha256": request["request_sha256"],
            "status": "UNCERTAIN",
            "reason_code": "GIFT_NAME_AUDIO_UNCERTAIN",
        }

    output, audit = apply_authoritative_chat_evidence(
        source,
        [ChatEvidence("gift", 0, "流星雨", sender="有***")],
        entity_verifier=uncertain,
    )

    assert output == source
    assert audit["gift_repairs"][0]["outcome"] == "uncertain_no_change"


def test_gift_repair_skips_tail_that_matches_a_genuine_sc_or_danmaku_read():
    """TAIL 若真实出现在别的 SC 正文/弹幕原文里，说明那是一次真实朗读，不是
    ASR 听错礼物名——绝不能被礼物名覆盖掉。"""
    source = _srt("谢谢有人看到你的人鱼")

    output, audit = apply_authoritative_chat_evidence(
        source,
        [
            ChatEvidence("gift", 0, "流星雨", sender="有***"),
            ChatEvidence("superchat", 1_000, "我也喜欢那条人鱼", "路人甲"),
        ],
        entity_verifier=_audio_entity_verifier("流星雨"),
    )

    assert output == source
    assert audit["gift_repairs"] == []


def test_gift_repair_respects_pre_context_window_gating():
    """谢意 cue 必须落在 [gift_time-5s, gift_time+120s] 之内才会被考虑；太晚
    出现的"谢谢...的X" cue 与这次礼物无关，不应被仲裁触碰。"""
    source = _srt("谢谢有人看到你的人鱼")

    output, audit = apply_authoritative_chat_evidence(
        source,
        [ChatEvidence("gift", 200_000, "流星雨", sender="有***")],
        entity_verifier=_audio_entity_verifier("流星雨"),
    )

    assert output == source
    assert audit["gift_repairs"] == []


def test_gift_kind_never_triggers_exact_span_replacement_of_whole_cues():
    """kind="gift" 绝不能进入弹幕/SC 的 exact-read 精确跨度替换通路——即便
    giftName 恰好逐字等于某条 cue，也必须只走 gift_repairs 这条窄路径。"""
    source = _srt("好高兴收到超多的流星雨呀")

    output, audit = apply_authoritative_chat_evidence(
        source,
        [ChatEvidence("gift", -1_000, "好高兴收到超多的流星雨呀", sender="有***")],
        entity_verifier=_audio_entity_verifier("好高兴收到超多的流星雨呀"),
    )

    assert output == source
    assert audit["applied"] == []
    assert audit["gift_repairs"] == []


def test_chat_read_support_excludes_chat_conditioned_agy_refinement(tmp_path):
    media = tmp_path / "padded.mp4"
    media.write_bytes(b"media")
    media.with_suffix(".asr_draft.srt").write_text(_srt("raw audio only"), encoding="utf-8")
    media.with_suffix(".agy_refined.srt").write_text(
        _srt("copied exact structured chat"), encoding="utf-8"
    )

    support = _load_independent_chat_support_srts(media)

    assert len(support) == 1
    assert "raw audio only" in support[0]
    assert "copied exact structured chat" not in support[0]


def test_longest_confusable_alias_is_replaced_atomically():
    source = _srt("我倒是还没看梦现代", "我觉得Ave Mujica还不错")
    exact = "我倒是还没看梦限大"

    output, audit = apply_authoritative_chat_evidence(
        source,
        [ChatEvidence("danmaku", 0, exact)],
        support_srt_texts=[source],
        referent_groups=[DREAM_MUJICA_GROUP],
        entity_verifier=_audio_entity_verifier("梦限大"),
    )

    assert "我觉得梦限大还不错" in output
    assert "Ave 梦限大" not in output
    assert audit["coreference_repairs"][0]["replaced_confusable"] == "Ave Mujica"


def test_confusable_entity_in_nearby_chat_cannot_overwrite_audio_refined_name():
    source = _srt(
        "还没看",
        "怎么有人说有梦限大的风险",
        "我还没有看完整的",
    )
    exact = "还没看，怎么有人说有母鸡卡的风险"

    output, audit = apply_authoritative_chat_evidence(
        source,
        [ChatEvidence("danmaku", 0, exact)],
        # Even a raw ASR agreeing with the nearby chat is only a text proxy;
        # it cannot erase a different proper name chosen by the refined audio.
        support_srt_texts=[_srt("还没看", "怎么有人说有母鸡卡的风险")],
        referent_groups=[DREAM_MUJICA_GROUP],
    )

    assert output == source
    assert audit["status"] == "ENTITY_VERDICT_REQUIRED"
    assert audit["applied"] == []
    assert audit["entity_verdict_required"][0]["matched_audio_text"] == (
        "还没看怎么有人说有梦限大的风险"
    )
    assert audit["entity_verdict_required"][0]["reason_code"] == "ENTITY_VERDICT_REQUIRED"


def test_audio_forced_choice_rejects_exact_chat_and_repairs_only_entity_slot():
    source = _srt("还没看", "怎么有人说有母鸡卡的风险", "我还没有看完整的")
    exact = "还没看，怎么有人说有母鸡卡的风险"

    output, audit = apply_authoritative_chat_evidence(
        source,
        [ChatEvidence("danmaku", 0, exact)],
        support_srt_texts=[source],
        referent_groups=[DREAM_MUJICA_GROUP],
        entity_verifier=_audio_entity_verifier("梦限大"),
    )

    assert "怎么有人说有梦限大的风险" in output
    assert "还没看，怎么有人说" not in output
    assert audit["applied"] == []
    assert audit["entity_repairs"][0]["mode"] == "entity_only"
    assert audit["entity_repairs"][0]["replaced_surface"] == "母鸡卡"
    assert audit["superseded_chat_proposals"][0]["reason_code"] == (
        "EXACT_CHAT_REJECTED_BY_AUDIO_ENTITY_VERDICT"
    )
    assert audit["status"] == "APPLIED_AND_VERIFIED"


def test_hash_bound_ivan_override_supersedes_chat_and_survives_final_verifier(tmp_path):
    source = _srt("还没看", "怎么有人说有母鸡卡的风险")
    final = source.replace("母鸡卡", "梦限大")
    evidence = ChatEvidence("danmaku", 0, "还没看，怎么有人说有母鸡卡的风险")
    document = {
        "schema_version": 1,
        "candidate_id": "auto_test",
        "source_srt_sha256": hashlib.sha256(source.encode()).hexdigest(),
        "text_final_srt_sha256": hashlib.sha256(final.encode()).hexdigest(),
        "chat_entity_verdicts": [
            {
                "evidence_id": evidence.evidence_id,
                "canonical_entity": "梦限大",
                "authority": "Ivan direct correction",
            }
        ],
        "overrides": [
            {
                "source_cue": 2,
                "expect": {
                    "start": "00:00:10,000",
                    "end": "00:00:14,000",
                    "text": "怎么有人说有母鸡卡的风险",
                },
                "text": "怎么有人说有梦限大的风险",
                "authority": "Ivan direct correction",
                "supersedes_chat_evidence_id": evidence.evidence_id,
            }
        ],
    }
    document_path = tmp_path / "auto_test.text.v1.json"
    document_path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    verifier = build_human_text_entity_verifier(document_path, candidate_id="auto_test")

    output, audit = apply_authoritative_chat_evidence(
        source,
        [evidence],
        support_srt_texts=[source],
        referent_groups=[DREAM_MUJICA_GROUP],
        entity_verifier=verifier,
    )

    assert output == source
    assert audit["status"] == "PENDING_TEXT_OVERRIDE"
    assert audit["applied"] == []
    manifest = {
        "status": "READY",
        "override_document_sha256": hashlib.sha256(document_path.read_bytes()).hexdigest(),
        "source_srt_sha256": hashlib.sha256(source.encode()).hexdigest(),
        "output_srt_sha256": hashlib.sha256(final.encode()).hexdigest(),
        "decisions": [
            {
                "source": {
                    "source_index": 2,
                    "start": "00:00:10,000",
                    "end": "00:00:14,000",
                    "text": "怎么有人说有母鸡卡的风险",
                },
                "output_text": "怎么有人说有梦限大的风险",
                "supersedes_chat_evidence_id": evidence.evidence_id,
            }
        ],
    }
    drifted = copy.deepcopy(audit)
    assert not reconcile_pending_text_overrides(
        drifted,
        {**manifest, "source_srt_sha256": "0" * 64},
        delivery_start_ms=0,
    )

    assert reconcile_pending_text_overrides(audit, manifest, delivery_start_ms=0)
    assert audit["entity_repairs"][0]["mode"] == "entity_only_human_text_override"
    assert verify_chat_authority_final_surfaces(
        audit,
        final_text_srt=final,
        final_speaker_srt=final,
        delivery_start_ms=0,
        delivery_end_ms=20_000,
    )


@pytest.mark.parametrize("override_schema_version", [2, 3])
def test_cue_bound_ivan_override_supersedes_chat_and_reconciles_witnesses(
    tmp_path, override_schema_version
):
    source = _srt("还没看", "怎么有人说有母鸡卡的风险")
    final = source.replace("母鸡卡", "梦限大")
    evidence = ChatEvidence("danmaku", 0, "还没看，怎么有人说有母鸡卡的风险")
    source_witness = hashlib.sha256(b"reviewed-source-cues").hexdigest()
    decision_witness = hashlib.sha256(b"reviewed-output-decisions").hexdigest()
    document = {
        "schema_version": override_schema_version,
        "candidate_id": "auto_test",
        "source_cue_count": 2,
        "source_cue_witness_sha256": source_witness,
        "decision_output_witness_sha256": decision_witness,
        "chat_entity_verdicts": [
            {
                "evidence_id": evidence.evidence_id,
                "canonical_entity": "梦限大",
                "authority": "Ivan direct correction",
            }
        ],
        "overrides": [
            {
                "source_cue": 2,
                "expect": {
                    "start": "00:00:10,000",
                    "end": "00:00:14,000",
                    "text": "怎么有人说有母鸡卡的风险",
                },
                "text": "怎么有人说有梦限大的风险",
                "authority": "Ivan direct correction",
                "supersedes_chat_evidence_id": evidence.evidence_id,
            }
        ],
    }
    document_path = tmp_path / "auto_test.text.v1.json"
    document_path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    verifier = build_human_text_entity_verifier(document_path, candidate_id="auto_test")

    output, audit = apply_authoritative_chat_evidence(
        source,
        [evidence],
        support_srt_texts=[source],
        referent_groups=[DREAM_MUJICA_GROUP],
        entity_verifier=verifier,
    )

    assert output == source
    assert audit["status"] == "PENDING_TEXT_OVERRIDE"
    manifest = {
        "status": "READY",
        "override_schema_version": override_schema_version,
        "override_document": str(document_path),
        "override_document_sha256": hashlib.sha256(document_path.read_bytes()).hexdigest(),
        "source_srt_sha256": hashlib.sha256(source.encode()).hexdigest(),
        "output_srt_sha256": hashlib.sha256(final.encode()).hexdigest(),
        "source_cue_witness_sha256": source_witness,
        "decision_output_witness_sha256": decision_witness,
        "decisions": [
            {
                "source": {
                    "source_index": 2,
                    "start": "00:00:10,000",
                    "end": "00:00:14,000",
                    "text": "怎么有人说有母鸡卡的风险",
                },
                "output_text": "怎么有人说有梦限大的风险",
                "supersedes_chat_evidence_id": evidence.evidence_id,
            }
        ],
    }
    assert reconcile_pending_text_overrides(audit, manifest, delivery_start_ms=0)
    assert audit["entity_repairs"][0]["mode"] == "entity_only_human_text_override"
    assert verify_chat_authority_final_surfaces(
        audit,
        final_text_srt=final,
        final_speaker_srt=final,
        delivery_start_ms=0,
        delivery_end_ms=20_000,
    )


def test_hash_bound_ivan_entity_verdict_can_reuse_exact_chat_scaffold(tmp_path):
    source = _srt("还没看", "怎么有人说是Mujica的风险")
    final = _srt("还没看", "怎么有人说有梦限大的风险")
    evidence = ChatEvidence("danmaku", 0, "还没看，怎么有人说有母鸡卡的风险")
    document = {
        "schema_version": 1,
        "candidate_id": "auto_test",
        "source_srt_sha256": hashlib.sha256(source.encode()).hexdigest(),
        "text_final_srt_sha256": hashlib.sha256(final.encode()).hexdigest(),
        "chat_entity_verdicts": [
            {
                "evidence_id": evidence.evidence_id,
                "canonical_entity": "梦限大",
                "authority": "Ivan direct correction",
            }
        ],
        "overrides": [
            {
                "source_cue": 2,
                "expect": {
                    "start": "00:00:10,000",
                    "end": "00:00:14,000",
                    "text": "怎么有人说是Mujica的风险",
                },
                "text": "怎么有人说有梦限大的风险",
                "authority": "exact chat scaffold plus Ivan entity verdict",
                "supersedes_chat_evidence_id": evidence.evidence_id,
            }
        ],
    }
    document_path = tmp_path / "auto_test.text.v1.json"
    document_path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    document_hash = hashlib.sha256(document_path.read_bytes()).hexdigest()
    audit = {
        "status": "PENDING_TEXT_OVERRIDE",
        "pending_text_overrides": [
            {
                "evidence_id": evidence.evidence_id,
                "exact_text": evidence.text,
                "matched_start_ms": 5_000,
                "matched_end_ms": 14_000,
                "request": {
                    "candidate_entities": [
                        {
                            "canonical": entity.canonical,
                            "surfaces": list(entity.surfaces),
                            "readings": list(entity.readings),
                        }
                        for entity in DREAM_MUJICA_GROUP.entities
                    ]
                },
                "verdict": {
                    "request_sha256": "request-hash",
                    "override_document_sha256": document_hash,
                    "source_srt_sha256": hashlib.sha256(source.encode()).hexdigest(),
                    "text_final_srt_sha256": hashlib.sha256(final.encode()).hexdigest(),
                    "canonical_entity": "梦限大",
                },
            }
        ],
        "entity_verdicts": [
            {
                "evidence_id": evidence.evidence_id,
                "verdict": {
                    "request_sha256": "request-hash",
                    "canonical_entity": "梦限大",
                },
            }
        ],
        "entity_repairs": [],
    }
    manifest = {
        "status": "READY",
        "override_document": str(document_path),
        "override_document_sha256": document_hash,
        "source_srt_sha256": hashlib.sha256(source.encode()).hexdigest(),
        "output_srt_sha256": hashlib.sha256(final.encode()).hexdigest(),
        "decisions": [
            {
                "source": {
                    "source_index": 2,
                    "start": "00:00:10,000",
                    "end": "00:00:14,000",
                    "text": "怎么有人说是Mujica的风险",
                },
                "output_text": "怎么有人说有梦限大的风险",
                "supersedes_chat_evidence_id": evidence.evidence_id,
            }
        ],
    }
    arbitrary = copy.deepcopy(audit)
    arbitrary_manifest = copy.deepcopy(manifest)
    arbitrary_manifest["decisions"][0]["output_text"] = "今天完全不相关但提到梦限大"
    assert not reconcile_pending_text_overrides(
        arbitrary,
        arbitrary_manifest,
        delivery_start_ms=0,
    )
    audit["pending_text_overrides"][0]["verdict"].update(
        {
            "authority_kind": "ivan_text_override",
            "defer_to_text_override": True,
            "candidate_id": "auto_test",
            "override_document_sha256": "0" * 64,
            "source_srt_sha256": "1" * 64,
            "text_final_srt_sha256": "2" * 64,
        }
    )
    assert reconcile_pending_text_overrides(audit, manifest, delivery_start_ms=0)
    assert audit["pending_text_overrides"][0]["verdict_rebinding"]["status"] == (
        "UNCHANGED_ENTITY_REBOUND_TO_FROZEN_SOURCE"
    )
    assert audit["entity_verdicts"][0]["verdict"]["override_document_sha256"] == (
        document_hash
    )
    assert audit["entity_repairs"][0]["mode"] == (
        "chat_scaffold_plus_human_entity_override"
    )
    assert audit["entity_repairs"][0]["structured_exact_text"] == (
        "还没看，怎么有人说有梦限大的风险"
    )
    assert verify_chat_authority_final_surfaces(
        audit,
        final_text_srt=final,
        final_speaker_srt=final,
        delivery_start_ms=0,
        delivery_end_ms=20_000,
    )
    missing_prefix = _srt("前缀被丢了", "怎么有人说有梦限大的风险")
    assert not verify_chat_authority_final_surfaces(
        copy.deepcopy(audit),
        final_text_srt=missing_prefix,
        final_speaker_srt=missing_prefix,
        delivery_start_ms=0,
        delivery_end_ms=20_000,
    )


def test_reviewed_text_override_supersedes_conflicting_exact_chat_read():
    delivery_start_ms = 712_000
    final = (
        "1\n"
        "00:00:47,790 --> 00:00:49,590\n"
        "有没有李豆沙女友喜欢吗？\n"
    )
    audit = {
        "applied": [
            {
                "evidence_id": "chat-evidence",
                "exact_text": "有没有礼墨女友",
                "matched_start_ms": delivery_start_ms + 47_790,
                "matched_end_ms": delivery_start_ms + 49_590,
            }
        ]
    }
    manifest = {
        "status": "READY",
        "override_document_sha256": "a" * 64,
        "source_srt_sha256": "b" * 64,
        "output_srt_sha256": "c" * 64,
        "decisions": [
            {
                "source": {
                    "source_index": 12,
                    "start": "00:00:47,790",
                    "end": "00:00:49,590",
                    "text": "有没有礼墨女友喜欢吗？",
                },
                "output_text": "有没有李豆沙女友喜欢吗？",
                "authority": "reviewed event semantics",
                "reason": "correct the impossible homophone",
            }
        ],
    }

    assert not verify_chat_authority_final_surfaces(
        copy.deepcopy(audit),
        final_text_srt=final,
        final_speaker_srt=final,
        delivery_start_ms=delivery_start_ms,
        delivery_end_ms=delivery_start_ms + 60_000,
    )
    assert (
        reconcile_reviewed_text_override_conflicts(
            audit,
            manifest,
            delivery_start_ms=delivery_start_ms,
        )
        == 1
    )
    assert audit["applied"][0]["reconciliation"]["reason_code"] == (
        "REVIEWED_TEXT_OVERRIDE_SUPERSEDES_CHAT_READ"
    )
    assert audit["superseded_chat_proposals"][-1]["reviewed_output_text"] == (
        "有没有李豆沙女友喜欢吗？"
    )
    assert verify_chat_authority_final_surfaces(
        audit,
        final_text_srt=final,
        final_speaker_srt=final,
        delivery_start_ms=delivery_start_ms,
        delivery_end_ms=delivery_start_ms + 60_000,
    )


def test_unrelated_reviewed_text_override_does_not_supersede_chat_read():
    delivery_start_ms = 712_000
    final = (
        "1\n"
        "00:00:47,790 --> 00:00:49,590\n"
        "有没有李豆沙女友喜欢吗？\n"
    )
    audit = {
        "applied": [
            {
                "evidence_id": "chat-evidence",
                "exact_text": "有没有礼墨女友",
                "matched_start_ms": delivery_start_ms + 47_790,
                "matched_end_ms": delivery_start_ms + 49_590,
            }
        ]
    }
    manifest = {
        "status": "READY",
        "override_document_sha256": "a" * 64,
        "source_srt_sha256": "b" * 64,
        "output_srt_sha256": "c" * 64,
        "decisions": [
            {
                "source": {
                    "source_index": 3,
                    "start": "00:00:10,000",
                    "end": "00:00:12,000",
                    "text": "完全无关的一句",
                },
                "output_text": "另一句无关文本",
                "authority": "reviewed source",
                "reason": "unrelated correction",
            }
        ],
    }

    assert (
        reconcile_reviewed_text_override_conflicts(
            audit,
            manifest,
            delivery_start_ms=delivery_start_ms,
        )
        == 0
    )
    assert "reconciliation" not in audit["applied"][0]
    assert not verify_chat_authority_final_surfaces(
        audit,
        final_text_srt=final,
        final_speaker_srt=final,
        delivery_start_ms=delivery_start_ms,
        delivery_end_ms=delivery_start_ms + 60_000,
    )


def test_hard_meme_surface_zhinv_is_always_canonicalized():
    """Ivan 2026-07-13 铁律：这是梗，所有「直女」一律写成「侄女」（无例外），
    与 code-switch 同机制、authority 分表可审计。"""
    from src.autoslice.chat_authority import canonicalize_hard_surfaces

    source = _srt("我是直女", "全场都在鼓掌")
    output, audit = normalize_code_switch_surfaces(source)
    cues = parse_srt_cues(output)
    assert cues[0].text == "我是侄女"
    assert cues[1].text == "全场都在鼓掌"
    repair = audit["repairs"][0]
    assert repair["replacements"][0]["authority"] == "lidousha-hard-meme-canon.v1"
    # 标题/封面兜底走同一张表
    assert canonicalize_hard_surfaces("李豆沙坚称自己是直女，回忆大舞台") == (
        "李豆沙坚称自己是侄女，回忆大舞台"
    )


def test_only_zhinv_rule_is_unbypassable_at_final_surface():
    from src.autoslice.chat_authority import canonicalize_hard_meme_surfaces

    output, repairs = canonicalize_hard_meme_surfaces(
        "直女在看难崩小视频，还说哇库哇库"
    )
    assert output == "侄女在看难崩小视频，还说哇库哇库"
    assert [(row["surface"], row["canonical"]) for row in repairs] == [
        ("直女", "侄女")
    ]


def test_expected_value_canon_reasserts_limo_after_mutable_stages():
    from src.autoslice.chat_authority import normalize_expected_value_surfaces

    source = _srt("切，那就差林墨没吃了", "下一句")
    output, audit = normalize_expected_value_surfaces(source)

    assert parse_srt_cues(output)[0].text == "切，那就差礼墨没吃了"
    assert audit["status"] == "APPLIED"
    assert audit["decision_authority"] == "EXPECTED_VALUE_CANON"
    assert audit["repairs"][0]["replacements"][0] == {
        "surface": "林墨",
        "canonical": "礼墨",
        "authority": "lidousha-expected-value-canon.v1",
        "count": 1,
    }


def test_expected_value_canon_repairs_all_listed_shadouli_mentions():
    from src.autoslice.chat_authority import normalize_expected_value_surfaces

    source = _srt(
        "然后看下下斗里的队伍",
        "发一支持下斗里的队伍",
    )
    output, audit = normalize_expected_value_surfaces(source)

    assert [cue.text for cue in parse_srt_cues(output)] == [
        "然后看下沙豆李的队伍",
        "发一支持沙豆李的队伍",
    ]
    assert audit["status"] == "APPLIED"
    assert sum(
        replacement["count"]
        for repair in audit["repairs"]
        for replacement in repair["replacements"]
        if replacement["canonical"] == "沙豆李"
    ) == 2


def test_expected_value_canon_stops_when_mishear_surface_becomes_registered(
    monkeypatch,
):
    from src.autoslice.chat_authority import normalize_expected_value_surfaces
    from src.autoslice import term_authority

    monkeypatch.setattr(
        term_authority,
        "registered_terms",
        lambda: frozenset({"林墨", "礼墨"}),
    )
    source = _srt("今天林墨也在", "下一句")
    output, audit = normalize_expected_value_surfaces(source)

    assert output == source
    assert audit["status"] == "NO_CHANGE"
    assert audit["eligible_rule_count"] == audit["rule_count"] - 1
    assert audit["repairs"] == []
    assert audit["registered_name_conflicts"] == [
        {
            "surface": "林墨",
            "canonical": "礼墨",
            "authority": "lidousha-expected-value-canon.v1",
            "count": 1,
            "routed": "CPA_REQUIRED",
            "registered_name_conflict": True,
        }
    ]


def test_final_surface_gate_rejects_human_override_that_reintroduces_zhinv():
    bad = _srt("人工裁决又写回直女")
    audit: dict = {}
    assert not verify_chat_authority_final_surfaces(
        audit,
        final_text_srt=bad,
        final_speaker_srt=bad,
        delivery_start_ms=0,
        delivery_end_ms=10_000,
    )
    assert audit["final_verification_failure"] == (
        "UNBYPASSABLE_HARD_MEME_SURFACE_PRESENT"
    )

    good = _srt("人工裁决最终仍是侄女")
    assert verify_chat_authority_final_surfaces(
        {},
        final_text_srt=good,
        final_speaker_srt=good,
        delivery_start_ms=0,
        delivery_end_ms=10_000,
    )


def test_sc_read_with_mid_read_interjection_is_preserved():
    """2026-07-10 伊依 SC 实案：她念半句 SC → 回应「谢谢你」→ 继续念完。
    对齐拼接必须保留中途插话，不得因整段覆盖而删掉。"""
    exact = "李姐动车被取消了，被困在别的城市我想回家"
    source = _srt(
        "李姐动车被取消了",
        "谢谢你",
        "被困在别的城市我想回家",
    )
    output, audit = apply_authoritative_chat_evidence(
        source,
        [ChatEvidence("superchat", -30_000, exact, "小猪状态")],
        support_srt_texts=[source],
    )
    joined = "".join(cue.text for cue in parse_srt_cues(output))
    assert "谢谢你" in joined, joined
    assert "被困在别的城市我想回家" in joined
    assert audit["status"] == "APPLIED_AND_VERIFIED", audit["status"]


def test_sc_emote_placeholders_are_stripped_from_subtitle_splice():
    """SC 原文的表情占位（；；串）和生僻区颜文字不进字幕。"""
    from src.autoslice.chat_authority import _strip_unrenderable_for_subtitle

    assert _strip_unrenderable_for_subtitle("李姐；；动车被取消了；；我想回家；；") == (
        "李姐，动车被取消了，我想回家"
    )
    assert _strip_unrenderable_for_subtitle("把你关在房间里ᗜ𖥦ᗜ") == "把你关在房间里"

    exact = "妈妈；；今天也要加油哦；；"
    source = _srt("妈妈今天也要加油哦")
    output, _audit = apply_authoritative_chat_evidence(
        source,
        [ChatEvidence("superchat", 0, exact, "十麻乃orient")],
        support_srt_texts=[source],
    )
    joined = "".join(cue.text for cue in parse_srt_cues(output))
    assert "；；" not in joined, joined


def test_sender_anchored_sc_near_miss_goes_to_audio_arbitration():
    """2026-07-10 十麻乃两案：字幕已有「谢谢十麻乃…」答谢锚点时，后面严重
    听岔的 SC 念读（文本相似度低于弹幕门槛）也送音频二选一；RESOLVED=SC 原文
    则逐字修复。"""
    exact = "李李被突击了"
    source = _srt(
        "谢谢十麻乃的醒目留言",
        "里里被吐击了吗",
    )
    output, audit = apply_authoritative_chat_evidence(
        source,
        [ChatEvidence("superchat", 0, exact, "十麻乃orient")],
        support_srt_texts=[source],
        entity_verifier=_audio_entity_verifier(exact),
    )
    texts = [cue.text for cue in parse_srt_cues(output)]
    assert texts[1] == "李李被突击了", texts
    row = audit["read_aloud_arbitrations"][0]
    assert row["sender_anchored"] is True
    assert row["outcome"] == "authority_confirmed_by_audio"
    assert row["whole_line_exact_copy_gate"]["proof_basis"] == (
        "hash_bound_full_span_audio_verdict"
    )


def test_partial_sc_context_verdict_cannot_inject_unspoken_message_remainder():
    """2026-07-22 火锅实案：0.4 coverage 只覆盖 SC 的少量词槽。

    “她在念这条 SC”的上下文判断不能把未听见的其余正文整句写进字幕；只有
    hash-bound 且逐字听出整句的音频 verdict，或近完整的转写跨度，才有权做
    whole-line exact copy。具体昵称可继续由 entity/source-truth 槽位修复。
    """

    exact = "大N老师能别躲在后面拿烟头偷偷烫我麻麻吗；；"
    source = _srt(
        "谢谢邪恶守宫的SC",
        "大N老师拿烟头烫的好",
    )

    def context_only_verifier(request):
        return {
            "schema_version": "chat-entity-verdict.v1",
            "request_sha256": request["request_sha256"],
            "status": "RESOLVED",
            "canonical_entity": exact,
            "confidence": 0.93,
            "reason_code": "READ_ALOUD_CONFIRMED_BY_CONTEXT",
            "verifier_id": "lidousha-cpa-read-aloud-context-v1",
            "prompt_sha256": "sha256:" + "a" * 64,
            "completion_sha256": "sha256:" + "b" * 64,
        }

    output, audit = apply_authoritative_chat_evidence(
        source,
        [ChatEvidence("superchat", 0, exact, "邪恶守宫")],
        support_srt_texts=[source],
        entity_verifier=context_only_verifier,
    )

    assert output == source
    assert "能别躲在后面" not in output
    row = audit["read_aloud_arbitrations"][0]
    assert row["coverage"] == 0.4
    assert row["outcome"] == "partial_evidence_no_whole_line_copy"
    assert row["whole_line_exact_copy_gate"]["status"] == "BLOCKED_PARTIAL_EVIDENCE"
    assert row["whole_line_exact_copy_gate"]["proof_basis"] == "partial_evidence"
    rejected = audit["superseded_chat_proposals"][0]
    assert rejected["reason_code"] == (
        "PARTIAL_CHAT_EVIDENCE_CANNOT_AUTHORIZE_WHOLE_LINE_COPY"
    )
    assert rejected["allowed_followup"] == "entity_or_source_truth_slot_only"


def test_direct_support_missing_prefix_cannot_authorize_whole_sc_copy():
    source = _srt("躲在后面大N老师拿烟头烫我麻麻")
    exact = "不要躲在后面大N老师拿烟头烫我麻麻"
    evidence = ChatEvidence(
        "superchat",
        0,
        exact,
        "missing-prefix-sender",
        source_event_id="sc://missing-prefix",
    )

    output, audit = apply_authoritative_chat_evidence(
        source,
        [evidence],
        support_srt_texts=[source],
    )

    assert output == source
    assert audit["applied"] == []
    rejected = next(
        row
        for row in audit["superseded_chat_proposals"]
        if row["evidence_id"] == evidence.evidence_id
    )
    gate = rejected["whole_line_exact_copy_gate"]
    assert gate["status"] == "BLOCKED_PARTIAL_EVIDENCE"
    assert gate["owner_eligible"] is False
    support = gate["independent_supports"][0]
    assert support["owner_eligible"] is False
    assert support["unsupported_authority_head"] == "不要"
    assert support["unsupported_authority_tail"] == ""
    assert support["unsupported_authority_interior"] == []
    for key in (
        "score",
        "coverage",
        "precision",
        "extent",
        "common",
        "common_chars",
    ):
        assert key in support


def test_direct_near_complete_independent_support_gets_typed_owner_receipt():
    exact = "soyo就是妈"
    source = _srt("soyo是真妈")
    independent = _srt(exact)

    output, audit = apply_authoritative_chat_evidence(
        source,
        [ChatEvidence("danmaku", 0, exact)],
        support_srt_texts=[independent],
    )

    assert parse_srt_cues(output)[0].text == exact
    row = audit["applied"][0]
    assert row["owner_eligible"] is True
    gate = row["whole_line_exact_copy_gate"]
    assert gate["status"] == "PASS"
    assert gate["proof_basis"] == "near_complete_independent_transcript"
    assert gate["independent_owner_support_count"] == 1
    support = row["audio_transcript_supports"][0]
    assert support["owner_eligible"] is True
    assert support["unsupported_authority_head"] == ""
    assert support["unsupported_authority_tail"] == ""
    assert support["unsupported_authority_interior"] == []


def test_boundary_borrowed_tail_char_is_not_whole_line_support():
    """2026-07-22 1863 实案：SC 原文尾字「了」她没念，独立转录（BCUT）连写
    到下一句「哎，现在几点了」，SequenceMatcher 让 authority 尾「了」借到
    下一句的同形字，把非逐字朗读伪判成 near-complete。边界孤立小块跨
    ≥3 字 observed 插入必须剥离。"""
    from src.autoslice.read_aloud_arbitration import (
        typed_whole_line_support_receipt,
    )

    receipt = typed_whole_line_support_receipt(
        "什么要请大N老师吃火锅，给你们加盘素菜，终于和大N见面了",
        "为什么要请大老师吃火锅给你们加盘素菜终于和大人见面哎，现在几点了",
        score=0.9,
        coverage=0.92,
        precision=0.75,
        common_chars=24,
        support_kind="independent_transcript",
    )
    assert receipt["borrowed_boundary_blocks_stripped"] == ["了"]
    assert receipt["unsupported_authority_tail"] == "了"
    assert receipt["owner_eligible"] is False


def test_non_verbatim_sc_read_does_not_overwrite_reviewed_wording():
    """同一实案端到端：她念 SC 时加「为」、没念尾「了」——整行逐字改写
    不许 applied，字幕保持她实际说的话。"""
    spoken = (
        "为什么要请大N老师吃火锅",
        "给你们加盘素菜",
        "终于和大N见面",
        "哎现在几点了",
    )
    bcut = (
        "为什么要请大老师吃火锅",
        "给你们加盘素菜",
        "终于和大人见面",
        "哎现在几点了",
    )
    exact = "什么要请大N老师吃火锅，给你们加盘素菜，终于和大N见面了"
    output, audit = apply_authoritative_chat_evidence(
        _srt(*spoken),
        [ChatEvidence("superchat", -448_869, exact, "南町家的星耀")],
        support_srt_texts=[_srt(*bcut)],
    )
    texts = [cue.text for cue in parse_srt_cues(output)]
    assert texts[0] == "为什么要请大N老师吃火锅", texts
    assert exact.replace("，", "") not in "".join(texts)
    assert not [
        row
        for row in audit["applied"]
        if row.get("exact_text") == exact
        and row.get("whole_line_exact_copy_gate", {}).get("status") == "PASS"
        and row.get("whole_line_exact_copy_gate", {}).get("proof_basis")
        == "near_complete_independent_transcript"
    ]


def test_sc_thread_danmaku_reply_is_verbatim_authority():
    """2026-07-13 利安/无马懿 实案：观众 SC 提问（诸葛亮谜题）后，同一人用
    普通弹幕接龙谜底「无马懿，无马懿」（弹幕名被打码成 -***）。她念这条弹幕
    时谐音梗必然听写错（吾马已无马矣）且任何转写家族都一致听错（support
    结构性缺席）——线程+时间窗即逐字权威。"""
    source = _srt("庆功宴上诸葛亮说了什么", "吾马已无马矣", "谁听得懂立语")
    output, audit = apply_authoritative_chat_evidence(
        source,
        [
            ChatEvidence("superchat", -60_000, "诸葛亮空城计之后庆功宴上说了什么", "-利安-"),
            ChatEvidence("danmaku", 2_000, "无马懿，无马懿", "-***"),
        ],
    )
    texts = [cue.text for cue in parse_srt_cues(output)]
    assert texts[1] == "无马懿，无马懿", texts
    row = next(r for r in audit["applied"] if r["exact_text"] == "无马懿，无马懿")
    assert row["thread_anchored"] is True
    assert row["owner_eligible"] is True
    assert row["whole_line_exact_copy_gate"]["proof_basis"] == "strong_thread_anchor"
    assert audit["status"] == "APPLIED_AND_VERIFIED", audit["status"]


def test_unrelated_masked_danmaku_gets_no_thread_privilege():
    """掩码首字不匹配（或无 SC 前情）的弹幕不享受线程豁免：不改字幕。"""
    source = _srt("吾马已无马矣")
    output, audit = apply_authoritative_chat_evidence(
        source,
        [
            ChatEvidence("superchat", -60_000, "诸葛亮空城计之后庆功宴上说了什么", "-利安-"),
            ChatEvidence("danmaku", 2_000, "无马懿，无马懿", "K***"),
        ],
    )
    assert parse_srt_cues(output)[0].text == "吾马已无马矣"
    assert audit["applied"] == []


def test_spoken_repeat_after_read_survives():
    """2026-07-10 戴上眼罩 实案：念完弹幕后她复读片段再回应——复读 cue 绝不能
    被念读替换吞掉。"""
    source = _srt("李豆沙戴上眼罩挑战", "戴上眼罩", "是的")
    output, audit = apply_authoritative_chat_evidence(
        source,
        [ChatEvidence("danmaku", 0, "李豆沙戴上眼罩挑战")],
        support_srt_texts=[source],
    )
    texts = [cue.text for cue in parse_srt_cues(output)]
    assert texts[1] == "戴上眼罩", texts
    assert texts[2] == "是的"


def test_hard_meme_rule_applies_to_quoted_danmaku_evidence_too():
    """Ivan 铁律覆盖证据入口：观众弹幕原文写「直女」时，逐字注入前先回正，
    不允许 verbatim 权威把已规范化的字幕改回直女。"""
    source = _srt("弹幕说以前不是零是侄女")
    output, audit = apply_authoritative_chat_evidence(
        source,
        [ChatEvidence("danmaku", 0, "以前不是零，是直女")],
        support_srt_texts=[source],
    )
    joined = "".join(cue.text for cue in parse_srt_cues(output))
    assert "直女" not in joined, joined
    assert "侄女" in joined


def test_sc_read_with_address_prefix_already_spoken_survives_self_check():
    """2026-07-13 冷笑话成片实况：SC=「妈妈可以帮我宣传一下…」，称呼「妈妈」
    她在上一句已带出，拼接弃置后内部自检必须仍判 APPLIED_AND_VERIFIED。"""
    exact = "妈妈可以帮我宣传一下，我带去萤火虫的无料吗感觉可能发不完"
    source = _srt(
        "谢谢492的光波妈妈",
        "可以帮我宣传一下",
        "我带去萤火虫的物料吗",
        "感觉可能发不完好",
    )
    output, audit = apply_authoritative_chat_evidence(
        source,
        [ChatEvidence("superchat", -20_000, exact, "492")],
        support_srt_texts=[source],
    )
    joined = "".join(cue.text for cue in parse_srt_cues(output))
    assert "无料" in joined, joined  # SC 原文替换生效（物料→无料）
    assert audit["status"] == "APPLIED_AND_VERIFIED", (
        audit["status"],
        [(r.get("survived"), r.get("span_alignment")) for r in audit["applied"]],
    )
    assert all(row["survived"] for row in audit["applied"])


def test_aligned_sc_keeps_current_reply_prefix_when_authority_head_was_previous():
    """2026-07-22：SC 问句跨上一 cue，内部 gap 不能倒灌进当前回答。

    authority 的「住她家了」已在上一 cue 念过；若按当前 span 的两个匹配块
    机械补 gap，会把「当然不是啦，确实要，要小心……」篡成自相矛盾的
    「确实要住她家了」。当前回答和口吃都必须保留。
    """
    from src.autoslice.chat_repair import _aligned_span_replacements

    result = _aligned_span_replacements(
        "那你今晚不是要住她家了，要小心小n老师啊，另外你俩的cp叫啥",
        [
            "当然不是啦",
            "确实要，要小心小鹅老师啊",
            "另外你俩CP叫啥",
        ],
        prev_context="那你今晚不是要住她家了",
    )

    assert result is not None
    replacements, alignment = result
    joined = "".join(replacements)
    assert joined.startswith("当然不是啦确实要，要小心")
    assert "确实要住她家了" not in joined
    assert joined.count("住她家了") == 0
    assert alignment["context_owned_internal_authority_gap"] == "住她家了，"
    assert (
        alignment["dropped_duplicate_authority_head"]
        == "那你今晚不是要住她家了，"
    )
    assert alignment["preserved_span_head"] == "当然不是啦确实要，"


def test_context_owned_internal_gap_survives_full_chat_authority_self_check():
    """完整 apply 路径必须既去重，又把分布在前 cue + 当前 span 的全文验绿。"""
    exact = "那你今晚不是要住她家了，要小心小n老师啊，另外你俩的cp叫啥"
    source = _srt(
        "那你今晚不是要住她家了",
        "当然不是啦",
        "确实要，要小心小鹅老师啊",
        "另外你俩CP叫啥",
    )

    output, audit = apply_authoritative_chat_evidence(
        source,
        [ChatEvidence("superchat", 0, exact, "南町nightin")],
        support_srt_texts=[source],
    )

    texts = [cue.text for cue in parse_srt_cues(output)]
    assert "".join(texts).count("住她家了") == 1
    assert "".join(texts[1:]).startswith("当然不是啦确实要，要小心")
    assert audit["applied"][0]["survived"] is True
    assert audit["status"] == "APPLIED_AND_VERIFIED"


def test_context_owned_gap_rebase_rejects_fuzzy_prefix_missing_polarity():
    """高覆盖不能替代 exact prefix；漏掉“不是”会反转 SC 原意。"""
    exact = "那你今晚不是要住她家了，要小心小n老师啊"
    source = _srt(
        "那你今晚要住她家了",
        "当然不是啦",
        "确实要，要小心小鹅老师啊",
    )

    output, audit = apply_authoritative_chat_evidence(
        source,
        [ChatEvidence("superchat", 0, exact, "南町nightin")],
        support_srt_texts=[source],
    )

    joined = "".join(cue.text for cue in parse_srt_cues(output))
    assert "那你今晚不是要住她家了" in joined
    assert "那你今晚要住她家了" not in joined
    assert audit["status"] == "APPLIED_AND_VERIFIED"


def test_aligned_sc_does_not_drop_real_missing_gap_for_incidental_nl_repeat():
    """相邻句偶然出现短 N/L token，不足以声明整个 authority 前缀已念过。"""
    from src.autoslice.chat_repair import _aligned_span_replacements

    result = _aligned_span_replacements(
        "一般不是LLNN吗",
        ["一般不是NN吗"],
        prev_context="刚才说的是NNLL",
    )

    assert result is not None
    replacements, alignment = result
    assert replacements == ["一般不是LLNN吗"]
    assert "context_owned_internal_authority_gap" not in alignment


def test_aligned_sc_preserves_nl_repetition_and_mid_read_interjections():
    """N/L 公式本身会复读；无 authority 缺字时不得删错相同 token。"""
    from src.autoslice.chat_repair import _aligned_span_replacements

    before = ["都是NNLL一般不是", "NNLLHHB或者NN吗"]
    result = _aligned_span_replacements(
        "都是NNLL一般不是LLNN吗",
        before,
        prev_context="前一句也提过NNLL",
    )

    assert result is not None
    replacements, alignment = result
    assert replacements == before
    assert alignment["preserved_span_interjections"] == ["NN", "HHB或者"]


def test_aligned_sc_deduplicates_only_surplus_title_close_at_splice():
    from src.autoslice.chat_repair import _aligned_span_replacements

    title = "《躲在屏幕后面抽烟的二人》"
    result = _aligned_span_replacements(
        title,
        [title + "》这个名字需要这么长吗"],
    )

    assert result is not None
    replacements, alignment = result
    assert replacements == [title + "这个名字需要这么长吗"]
    assert alignment["deduplicated_title_close_at_splice"] == 2

    nested = _aligned_span_replacements("《A《B》", ["《A《B》》后续"])
    assert nested is not None
    nested_replacements, nested_alignment = nested
    assert nested_replacements == ["《A《B》》后续"]
    assert nested_alignment["deduplicated_title_close_at_splice"] == 1


def test_dropped_head_claim_must_hold_or_self_check_fails_closed():
    """弃置声明撒谎（相邻句里其实没有那个头）时，内部自检必须 FAILED。"""
    from src.autoslice import chat_authority as ca

    row = {
        "exact_text": "妈妈可以帮我宣传一下我带去萤火虫的无料吗",
        "cue_indexes": [2],
        "span_alignment": {"dropped_duplicate_authority_head": "妈妈"},
    }
    texts = ["完全无关的上一句", "可以帮我宣传一下我带去萤火虫的无料吗", "后一句"]
    cues = ca.parse_srt_cues(_srt(*texts))
    # 直接复算 survived 判定路径：头在相邻上下文不存在 → False
    span_text = texts[1]
    expected = ca.normalize_chat_text(row["exact_text"])
    head = ca.normalize_chat_text("妈妈")
    assert expected.startswith(head)
    assert not ca._fragment_spoken_in("妈妈", texts[0])
    del cues, span_text  # 判定语义由上面两条断言钉住


def test_final_surface_verifier_understands_aligned_splice_dropped_head():
    """2026-07-13 生产实况回归：对齐拼接声明「authority 头她已在上一句说过，
    不重复注入」后，终验器必须在跨度窗口验剩余部分 + 在相邻上下文验被弃置
    的头；此前它坚持全文进跨度窗口，导致 4 条成品 CHAT_AUTHORITY_FINALIZATION_FAILED。"""
    final = _srt(
        "谢谢谢谢寒-歌的钢镚，好冷的笑话",
        "另外姐姐姐姐组乐队吗",
        "我会打退堂鼓",
        "退堂鼓算什么",
    )
    def make_audit():
        return {
            "applied": [
                {
                    "exact_text": "好冷的笑话，另外姐姐姐姐组乐队吗，我会打退堂鼓",
                    "matched_start_ms": 10_000,
                    "matched_end_ms": 24_000,
                    "span_alignment": {
                        "dropped_duplicate_authority_head": "好冷的笑话，",
                        "preserved_span_tail": "退堂鼓算什么",
                    },
                }
            ]
        }

    assert verify_chat_authority_final_surfaces(
        make_audit(),
        final_text_srt=final,
        final_speaker_srt=final,
        delivery_start_ms=0,
        delivery_end_ms=30_000,
    )
    # 同一把尺（2026-07-14 生日结婚案）：apply 侧用 coverage≥0.8 证明弃置头
    # 已被说过（弹幕「小李小李，抱抱李~」vs 口播「小李小李，抱抱」），外层
    # 复证不得要求全子串包含——否则同一决定 apply 通过、终验误杀。
    partial_head = _srt(
        "小李小李，抱抱",
        "另外姐姐姐姐组乐队吗",
        "我会打退堂鼓",
        "退堂鼓算什么",
    )
    partial_audit = {
        "applied": [
            {
                "exact_text": "小李小李，抱抱李~另外姐姐姐姐组乐队吗，我会打退堂鼓",
                "matched_start_ms": 10_000,
                "matched_end_ms": 24_000,
                "span_alignment": {
                    "dropped_duplicate_authority_head": "小李小李，抱抱李~",
                },
            }
        ]
    }
    assert verify_chat_authority_final_surfaces(
        partial_audit,
        final_text_srt=partial_head,
        final_speaker_srt=partial_head,
        delivery_start_ms=0,
        delivery_end_ms=30_000,
    )

    # 被弃置的头在上下文里不存在 → 弃置声明不成立 → 行判失败（fail-closed）
    head_missing = _srt(
        "完全无关的开场白",
        "另外姐姐姐姐组乐队吗",
        "我会打退堂鼓",
        "退堂鼓算什么",
    )
    assert not verify_chat_authority_final_surfaces(
        make_audit(),
        final_text_srt=head_missing,
        final_speaker_srt=head_missing,
        delivery_start_ms=0,
        delivery_end_ms=30_000,
    )
    # 无对齐声明的旧式 applied 行维持全文进窗口的严格语义
    strict_audit = {
        "applied": [
            {
                "exact_text": "好冷的笑话，另外姐姐姐姐组乐队吗，我会打退堂鼓",
                "matched_start_ms": 10_000,
                "matched_end_ms": 24_000,
            }
        ]
    }
    assert not verify_chat_authority_final_surfaces(
        strict_audit,
        final_text_srt=head_missing,
        final_speaker_srt=head_missing,
        delivery_start_ms=0,
        delivery_end_ms=30_000,
    )


def test_nearby_unrelated_chat_is_not_treated_as_subtitle_authority():
    source = _srt("今天确实很想跟大家看新番")
    output, audit = apply_authoritative_chat_evidence(
        source,
        [ChatEvidence("danmaku", 0, "完全不相关的观众指令删除所有字幕")],
    )
    assert output == source
    assert audit["status"] == "NO_MATCH"


def test_chat_agreement_without_audio_derived_support_is_not_hard_locked():
    source = _srt("等小室什么时候来看", "恋死我自己有看了")
    evidence = [ChatEvidence("danmaku", -8_000, "等小李什么时候来看恋青呢")]

    output, audit = apply_authoritative_chat_evidence(source, evidence)

    assert output == source
    assert audit["status"] == "NO_MATCH"


def test_matched_chat_is_sanitized_before_rendering_into_srt():
    source = _srt("请看危险文字")
    evidence = [ChatEvidence("danmaku", 0, "请看危险\n文字 -->")]

    output, audit = apply_authoritative_chat_evidence(
        source, evidence, support_srt_texts=[source]
    )

    cue_text = parse_srt_cues(output)[0].text
    assert "-->" not in cue_text
    assert cue_text == "请看危险 文字 →"
    assert audit["status"] == "APPLIED_AND_VERIFIED"


def test_sc_dedupe_keeps_same_text_from_distinct_senders(tmp_path):
    segment = tmp_path / "22966160_20260710-20-00-09.mp4"
    start_ms = recording_start_epoch_ms(segment)
    rows = []
    for index, sender in enumerate(("甲", "乙"), start=1):
        rows.append(
            {
                "cmd": "SUPER_CHAT_MESSAGE",
                "send_time": start_ms + 10_000 + index,
                "data": {
                    "id": index,
                    "message": "同一句支持",
                    "user_info": {"uname": sender},
                },
            }
        )
    segment.with_suffix(".jsonl").write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )

    items = load_chat_jsonl(segment.with_suffix(".jsonl"), recording_start_ms=start_ms)

    assert [(item.sender, item.text) for item in items] == [("甲", "同一句支持"), ("乙", "同一句支持")]


def test_final_surface_verification_is_time_bound_and_strips_known_speaker_labels():
    exact = "如果能唱的到想点首小城夏天，唱不到就算了"
    final_text = _srt("如果能唱的到", "想点首小城夏天", "唱不到就算了")
    speaker_text = _srt("[李豆沙] 如果能唱的到", "[李豆沙] 想点首小城夏天", "[李豆沙] 唱不到就算了")
    audit = {
        "applied": [
            {
                "exact_text": exact,
                "matched_start_ms": 5_000,
                "matched_end_ms": 19_000,
            }
        ]
    }

    assert verify_chat_authority_final_surfaces(
        audit,
        final_text_srt=final_text,
        final_speaker_srt=speaker_text,
        delivery_start_ms=0,
        delivery_end_ms=20_000,
    )
    assert audit["applied"][0]["final_verification_scope"] == "DELIVERY"


def test_sc_sender_final_verification_owns_only_the_narrow_sender_slot():
    final_text = _srt(
        "十麻乃SC啊，得了一种听到“是侄女”就想笑的病"
    )
    row = {
        "after": "十麻乃SC得了一种听到\"是侄女\"就想笑的病",
        "spoken_sender": "十麻乃",
        "alignment_basis": "matched-superchat-body-plus-action-anchor.v1",
        "matched_start_ms": 5_000,
        "matched_end_ms": 9_000,
    }
    audit = {"sender_repairs": [row]}

    assert verify_chat_authority_final_surfaces(
        audit,
        final_text_srt=final_text,
        final_speaker_srt=final_text,
        delivery_start_ms=0,
        delivery_end_ms=10_000,
    )
    assert row["final_verification_kind"] == "sc_sender"

    missing_sender = _srt("某人SC啊，得了一种听到侄女就想笑的病")
    assert not verify_chat_authority_final_surfaces(
        {"sender_repairs": [dict(row)]},
        final_text_srt=missing_sender,
        final_speaker_srt=missing_sender,
        delivery_start_ms=0,
        delivery_end_ms=10_000,
    )


def test_entity_final_verification_owns_only_its_repaired_span():
    final_text = _srt(
        "“李姐是侄女”，好多人笑出声这个事情呢"
    )
    row = {
        "mode": "final_review_context_adjudication",
        "before": ["“李姐是子女”，好的人笑出声这个事情呢，"],
        "after": ["“李姐是侄女”，好的人笑出声这个事情呢，"],
        "structured_exact_text": "“李姐是侄女”，好的人笑出声这个事情呢，",
        "matched_start_ms": 5_000,
        "matched_end_ms": 9_000,
    }
    audit = {"entity_repairs": [row]}

    assert verify_chat_authority_final_surfaces(
        audit,
        final_text_srt=final_text,
        final_speaker_srt=final_text,
        delivery_start_ms=0,
        delivery_end_ms=10_000,
    )
    assert row["final_verification_kind"] == "entity_repair"

    missing_repair = _srt("“李姐是子女”，好多人笑出声这个事情呢")
    assert not verify_chat_authority_final_surfaces(
        {"entity_repairs": [dict(row)]},
        final_text_srt=missing_repair,
        final_speaker_srt=missing_repair,
        delivery_start_ms=0,
        delivery_end_ms=10_000,
    )


def _authorized_drop_cue_row() -> dict:
    return {
        "mode": "final_review_context_adjudication",
        "repair_class": "acoustic_drop_cue",
        "decision_authority": "CPA_JUDGE",
        "policy_branch": "CPA_JUDGE_APPLY_INAUDIBLE_DROP_CUE",
        "mutation_authority": {
            "schema_version": (
                "subtitle-correction-mutation-authority.v1"
            ),
            "status": "PASS",
            "basis": "CPA_ACOUSTIC_PRONUNCIATION_DISAMBIGUATION",
        },
        "before": ["咳咳咳"],
        "after": [""],
        "structured_exact_text": "",
        "matched_start_ms": 5_000,
        "matched_end_ms": 9_000,
    }


def test_authorized_drop_cue_verifies_empty_final_window():
    final_text = "1\n00:00:10,000 --> 00:00:14,000\n保留的下一句\n"
    row = _authorized_drop_cue_row()

    assert verify_chat_authority_final_surfaces(
        {"entity_repairs": [row]},
        final_text_srt=final_text,
        final_speaker_srt=final_text,
        delivery_start_ms=0,
        delivery_end_ms=15_000,
    )
    assert row["final_drop_cue_empty_text_window"] is True
    assert row["final_drop_cue_empty_speaker_window"] is True


def test_drop_cue_without_typed_mutation_receipt_stays_fail_closed():
    final_text = "1\n00:00:10,000 --> 00:00:14,000\n保留的下一句\n"
    row = _authorized_drop_cue_row()
    row.pop("mutation_authority")

    assert not verify_chat_authority_final_surfaces(
        {"entity_repairs": [row]},
        final_text_srt=final_text,
        final_speaker_srt=final_text,
        delivery_start_ms=0,
        delivery_end_ms=15_000,
    )


def test_authorized_drop_cue_rejects_surviving_text_in_owned_window():
    final_text = _srt("咳咳咳", "保留的下一句")
    row = _authorized_drop_cue_row()

    assert not verify_chat_authority_final_surfaces(
        {"entity_repairs": [row]},
        final_text_srt=final_text,
        final_speaker_srt=final_text,
        delivery_start_ms=0,
        delivery_end_ms=15_000,
    )
    assert row["final_drop_cue_empty_text_window"] is False


def test_final_surface_verification_rejects_same_text_at_wrong_time():
    exact = "同一句原文"
    final_text = _srt(exact, "实际匹配处被改坏")
    audit = {
        "applied": [
            {
                "exact_text": exact,
                "matched_start_ms": 10_000,
                "matched_end_ms": 14_000,
            }
        ]
    }

    assert not verify_chat_authority_final_surfaces(
        audit,
        final_text_srt=final_text,
        final_speaker_srt=final_text,
        delivery_start_ms=0,
        delivery_end_ms=20_000,
    )


def test_final_surface_verification_ignores_matched_padding_outside_delivery():
    audit = {
        "applied": [
            {
                "exact_text": "前置上下文原文",
                "matched_start_ms": 1_000,
                "matched_end_ms": 4_000,
            }
        ]
    }
    final_text = _srt("真正交付内容")

    assert verify_chat_authority_final_surfaces(
        audit,
        final_text_srt=final_text,
        final_speaker_srt=final_text,
        delivery_start_ms=10_000,
        delivery_end_ms=20_000,
    )
    assert audit["applied"][0]["final_verification_scope"] == "OUTSIDE_DELIVERY"


def test_final_surface_verification_ignores_ten_ms_boundary_sliver():
    audit = {
        "applied": [
            {
                "exact_text": "沧月第一首结束叫的，我第二首结束叫的",
                "matched_start_ms": 6_880,
                "matched_end_ms": 9_760,
            }
        ]
    }
    final_text = _srt("真正交付内容")

    assert verify_chat_authority_final_surfaces(
        audit,
        final_text_srt=final_text,
        final_speaker_srt=final_text,
        delivery_start_ms=9_750,
        delivery_end_ms=251_680,
    )
    row = audit["applied"][0]
    assert row["final_verification_scope"] == "OUTSIDE_DELIVERY"
    assert row["final_delivery_overlap_ms"] == 10
    assert row["final_verification_scope_reason"] == (
        "BOUNDARY_SLIVER_BELOW_MEANINGFUL_AUDIO_THRESHOLD"
    )


def test_final_surface_verification_edge_crossing_head_pad_sliver_exempt():
    """850_940 案（2026-07-27）：1640ms 判决窗跨交付头缘，overlap 恰为
    250ms 片头 pad 常数、ratio 0.152——跨边缘几何工件必须算 sliver，
    ratio 腿只留给整体在交付内的短行。"""

    audit = {
        "applied": [
            {
                "exact_text": "又可以和大家见面了耶",
                "matched_start_ms": 8_360,
                "matched_end_ms": 10_000,
            }
        ]
    }
    final_text = _srt("交付从后面开始的内容")

    assert verify_chat_authority_final_surfaces(
        audit,
        final_text_srt=final_text,
        final_speaker_srt=final_text,
        delivery_start_ms=9_750,
        delivery_end_ms=100_000,
    )
    row = audit["applied"][0]
    assert row["final_verification_scope"] == "OUTSIDE_DELIVERY"
    assert row["final_delivery_overlap_ms"] == 250
    assert row["final_verification_scope_reason"] == (
        "BOUNDARY_SLIVER_BELOW_MEANINGFUL_AUDIO_THRESHOLD"
    )


def test_final_surface_verification_tiny_contained_row_still_required():
    """窗口整体在交付内（不跨边缘）的 240ms 微行不是几何工件——必须存活，
    文本缺失照旧判失败。"""

    audit = {
        "applied": [
            {
                "exact_text": "微小但完整在内的修复",
                "matched_start_ms": 10_000,
                "matched_end_ms": 10_240,
            }
        ]
    }
    final_text = _srt("交付字幕没有这句")

    assert not verify_chat_authority_final_surfaces(
        audit,
        final_text_srt=final_text,
        final_speaker_srt=final_text,
        delivery_start_ms=9_750,
        delivery_end_ms=100_000,
    )
    assert audit["applied"][0]["final_verification_scope"] == "DELIVERY"


def test_final_surface_verification_keeps_meaningful_boundary_overlap_strict():
    audit = {
        "applied": [
            {
                "exact_text": "跨边界仍在交付内的整句",
                "matched_start_ms": 9_700,
                "matched_end_ms": 10_700,
            }
        ]
    }
    final_text = _srt("交付字幕没有这句")

    assert not verify_chat_authority_final_surfaces(
        audit,
        final_text_srt=final_text,
        final_speaker_srt=final_text,
        delivery_start_ms=9_750,
        delivery_end_ms=20_000,
    )
    assert audit["applied"][0]["final_verification_scope"] == "DELIVERY"


def test_time_anchored_thanks_sender_copies_platform_name():
    """1209 唐琳韵案（Ivan 2026-07-27 复制人名令）：她只答谢不念 SC 正文，
    体锚失败——事件时刻 + 90s 窗 + 读音门（tao-lin≈tang-lin-yun）直接复制
    平台精确名，不再硬听。"""

    source = _srt(
        "随便聊点别的",
        "谢谢桃林的钢镚",
        "接着刚才的话题说",
    )
    output, audit = apply_authoritative_chat_evidence(
        source,
        [
            ChatEvidence(
                "superchat",
                2_000,
                "感觉豆沙用礼墨的麦更加吵闹了，是我的错觉吗",
                "唐琳韵",
                source_event_id="sc-tang",
            )
        ],
    )

    assert "谢谢唐琳韵的钢镚" in output
    assert "桃林" not in output
    rows = [
        row
        for row in audit["sender_repairs"]
        if row.get("alignment_basis") == "time-anchored-platform-sender.v1"
    ]
    assert rows and rows[0]["sender"] == "唐琳韵"
    assert rows[0]["source_event_id"] == "sc-tang"


def test_time_anchored_thanks_never_guesses_between_two_senders():
    source = _srt("谢谢桃林的钢镚")
    output, audit = apply_authoritative_chat_evidence(
        source,
        [
            ChatEvidence("superchat", 1_000, "正文甲", "唐琳韵", source_event_id="a"),
            ChatEvidence("superchat", 2_000, "正文乙", "淘琳", source_event_id="b"),
        ],
    )

    assert "谢谢桃林的钢镚" in output  # 不落刀
    verdicts = [
        row
        for row in audit["sender_verdict_required"]
        if row.get("reason_code") == "TIME_ANCHORED_SENDER_AMBIGUOUS"
    ]
    assert verdicts and set(verdicts[0]["candidate_event_ids"]) == {"a", "b"}


def test_time_anchored_thanks_ignores_incompatible_names():
    source = _srt("谢谢张三丰的钢镚")
    output, _audit = apply_authoritative_chat_evidence(
        source,
        [ChatEvidence("superchat", 1_000, "正文", "唐琳韵", source_event_id="a")],
    )
    assert "谢谢张三丰的钢镚" in output


def test_named_thanks_record_coverage_disclosure_states():
    """自审计面（Ivan 2026-07-27 复盘令）：「有记录没用上」不许静默。
    三态锚死：无事件=RECORD_CHANNEL_ABSENT；有事件名不相容=
    RECORD_PRESENT_NAME_INCOMPATIBLE；已修 cue 不出行。"""

    # 无任何带名事件 → ABSENT
    source = _srt("谢谢桃林的钢镚")
    _out, audit = apply_authoritative_chat_evidence(source, [])
    rows = audit["thanks_record_coverage"]
    assert rows and rows[0]["status"] == "RECORD_CHANNEL_ABSENT"
    assert rows[0]["heard_name"] == "桃林"

    # 有事件但两门都不认 → INCOMPATIBLE（张三丰≠唐琳韵）
    _out, audit = apply_authoritative_chat_evidence(
        _srt("谢谢张三丰的钢镚"),
        [ChatEvidence("superchat", 1_000, "正文", "唐琳韵", source_event_id="a")],
    )
    rows = audit["thanks_record_coverage"]
    assert rows and rows[0]["status"] == "RECORD_PRESENT_NAME_INCOMPATIBLE"
    assert "唐琳韵" in rows[0]["nearby_event_senders"]

    # 时间锚修掉的 cue 不再出披露行（修复行即披露）
    _out, audit = apply_authoritative_chat_evidence(
        _srt("谢谢桃林的钢镚"),
        [ChatEvidence("superchat", 1_000, "正文", "唐琳韵", source_event_id="a")],
    )
    assert audit["thanks_record_coverage"] == []


def test_time_anchored_thanks_respects_causal_floor():
    """物理不可能约束（Ivan 2026-07-27 追问）：事件到开口至少 2s——
    同帧/1s 内开始的感谢线不可能在谢这单，绝不匹配（舰长锚同款纪律）。"""

    # cue1 起点 5_000ms；事件 4_500ms → delta 500ms < 2s 因果下界
    source = _srt("谢谢桃林的钢镚")
    output, audit = apply_authoritative_chat_evidence(
        source,
        [ChatEvidence("superchat", 4_500, "正文", "唐琳韵", source_event_id="a")],
    )
    assert "谢谢桃林的钢镚" in output  # 未改
    assert not [
        row
        for row in audit["sender_repairs"]
        if row.get("alignment_basis") == "time-anchored-platform-sender.v1"
    ]
    # 覆盖审计同一因果窗：该事件不算 nearby → RECORD_CHANNEL_ABSENT
    rows = audit["thanks_record_coverage"]
    assert rows and rows[0]["status"] == "RECORD_CHANNEL_ABSENT"
