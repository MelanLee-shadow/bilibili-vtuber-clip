"""封面梗字判官的有界重试回归。

合并 ft-a8600994 时的改动说明：本文件来自 ft 分支,写时该分支的
``SCHEMA_VERSION`` 还是 v1、判官只有三项布尔门,故 fixture 里硬编码了
``...review.v1`` 且不带 ``no_fabricated_fact``。主线 3301e4e 已按 公开规则
裁定把 schema 抬到 v2 并新增 fail-closed 的反编造门
``no_fabricated_fact``。合并后取更严那侧(v2 四项布尔),因此这里把 fixture
的 schema 改成引用模块常量 ``SCHEMA_VERSION``、并给每个 payload 补上
``no_fabricated_fact``(一律 True,好让每条用例只在它自己的目标字段上判别)。
门与阈值一字未动,改的只是 fixture 的时代。
"""

import copy
import json
from pathlib import Path

from src.autoslice.cover_punch_semantics import (
    SCHEMA_VERSION,
    _validated_extractive_punch,
    review_cover_punch_semantics,
    validate_cover_punch_semantic_review,
)


def test_cover_punch_semantic_review_retries_one_fragmentary_rejection():
    cover_text = (
        "打歌服召唤不出来，被拆成“打她、她是哥、她服了”，"
        "公主抱又扯到脐带，宝贝妈妈老公主人齐了"
    )
    prompts: list[str] = []

    def judge(prompt: str) -> str:
        prompts.append(prompt)
        if len(prompts) == 1:
            return json.dumps(
                {
                    "schema_version": SCHEMA_VERSION,
                    "status": "REJECT",
                    "final_punch": None,
                    "stranger_can_infer_event": True,
                    "contains_concrete_subject": True,
                    "contains_action_or_conflict": True,
                    "no_fabricated_fact": True,
                    "story_summary": "公主抱荒诞地牵扯到脐带。",
                    "click_motivation": "想知道这段关系梗如何失控。",
                },
                ensure_ascii=False,
            )
        assert "唯一一次有界重试" in prompt
        return json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "status": "REVISE",
                "final_punch": {"main": "公主抱又扯到脐带", "sub": None},
                "stranger_can_infer_event": True,
                "contains_concrete_subject": True,
                "contains_action_or_conflict": True,
                "no_fabricated_fact": True,
                "story_summary": "公主抱荒诞地牵扯到脐带。",
                "click_motivation": "想知道这段关系梗如何失控。",
            },
            ensure_ascii=False,
        )

    reviewed, proof = review_cover_punch_semantics(
        title="【李豆沙】" + cover_text,
        cover_text=cover_text,
        story_hook="嘉宾把打歌服拆成谐音梗并一路闹到公主抱和脐带。",
        punch=("打歌服", "她服了"),
        llm_call=judge,
    )

    assert reviewed == ("公主抱又扯到脐带",)
    assert proof["status"] == "REVISED"
    assert proof["attempt_count"] == 2
    assert [row["status"] for row in proof["attempts"]] == [
        "REJECTED",
        "ACCEPTED",
    ]


def test_cover_punch_semantic_review_stops_after_one_retry():
    calls = 0

    def reject(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        return json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "status": "REJECT",
                "final_punch": None,
                "stranger_can_infer_event": False,
                "contains_concrete_subject": False,
                "contains_action_or_conflict": False,
                "no_fabricated_fact": True,
                "story_summary": "没有完整事件。",
                "click_motivation": "不足以形成点击动机。",
            },
            ensure_ascii=False,
        )

    reviewed, proof = review_cover_punch_semantics(
        title="【李豆沙】碎片标题",
        cover_text="碎片标题",
        story_hook="",
        punch=("碎片标题",),
        llm_call=reject,
    )

    assert reviewed == ()
    assert proof["status"] == "FAILED"
    assert proof["attempt_count"] == 2
    assert calls == 2


def test_frozen_background_review_rejects_long_valid_semantics_before_render():
    from src.autoslice.cover_generation import _fit_cover_punch_lines

    prompts = []
    font = Path(__file__).resolve().parents[1] / "assets/lidousha/fonts/ZCOOLKuaiLe-Regular.ttf"

    def fits_zone(value, text):
        lines = _validated_extractive_punch(value, text)
        if not lines:
            return ()
        fitted = _fit_cover_punch_lines(
            lines, zone=(260, 66, 960, 1014), font_path=font,
            hook_rgb=(255, 92, 138), base_fill=(255, 198, 41), max_size=360,
        )
        return lines if fitted[0]["size"] >= 120 else ()

    def judge(prompt):
        prompts.append(prompt)
        assert "700px" in prompt and "120px" in prompt
        short = len(prompts) == 2
        return json.dumps({
            "schema_version": SCHEMA_VERSION,
            "status": "PASS" if short else "REVISE",
            "final_punch": {"main": "游戏对白" if short else "游戏：抛下我结婚？", "sub": "别先结婚" if short else "小李：有点太暧昧"},
            **{key: True for key in ("stranger_can_infer_event", "contains_concrete_subject", "contains_action_or_conflict", "no_fabricated_fact")},
            "story_summary": "游戏对白担心对方先去结婚。",
            "click_motivation": "想看游戏对白为何这么暧昧。",
        }, ensure_ascii=False)

    text = "游戏对白问对方会不会抛下自己先去结婚，小李评价有点太暧昧。"
    lines, proof = review_cover_punch_semantics(
        title=text, cover_text=text, story_hook=text,
        punch=("游戏对白", "别先结婚"), llm_call=judge, punch_validator=fits_zone,
        render_constraint="固定背景只有700px文字区；主字至少120px，不能换区或拆行。",
    )
    assert lines == ("游戏对白", "别先结婚")
    assert [row["status"] for row in proof["attempts"]] == ["REJECTED", "ACCEPTED"]
    assert "可以忠实抽取、缩写或改写" in prompts[-1]
    assert validate_cover_punch_semantic_review(proof, rendered_lines=list(lines), cover_text=text, story_hook=text)


def test_cover_punch_retry_rejects_candidate_inserted_whitespace():
    cover_text = "公主抱又扯到脐带"

    def spaced(_prompt: str) -> str:
        return json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "status": "REVISE",
                "final_punch": {"main": "公主抱 又扯到脐带", "sub": None},
                "stranger_can_infer_event": True,
                "contains_concrete_subject": True,
                "contains_action_or_conflict": True,
                "no_fabricated_fact": True,
                "story_summary": "公主抱荒诞地牵扯到脐带。",
                "click_motivation": "想知道这段关系梗如何失控。",
            },
            ensure_ascii=False,
        )

    reviewed, proof = review_cover_punch_semantics(
        title="【李豆沙】" + cover_text,
        cover_text=cover_text,
        story_hook=cover_text,
        punch=("公主抱",),
        llm_call=spaced,
    )

    assert reviewed == ()
    assert proof["status"] == "FAILED"
    assert proof["attempt_count"] == 2


def test_cover_punch_retry_does_not_cross_bind_after_terminal_exception():
    calls = 0

    def reject_then_raise(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("provider unavailable")
        return json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "status": "REJECT",
                "final_punch": None,
                "stranger_can_infer_event": False,
                "contains_concrete_subject": False,
                "contains_action_or_conflict": False,
                "no_fabricated_fact": True,
                "story_summary": "没有完整事件。",
                "click_motivation": "不足以形成点击动机。",
            },
            ensure_ascii=False,
        )

    _reviewed, proof = review_cover_punch_semantics(
        title="【李豆沙】碎片标题",
        cover_text="碎片标题",
        story_hook="",
        punch=("碎片标题",),
        llm_call=reject_then_raise,
    )

    assert proof["status"] == "FAILED"
    assert proof["reason_code"] == "CPA_TEXT_REVIEW_CALL_FAILED"
    assert proof["request_sha256"] == proof["attempts"][-1]["request_sha256"]
    assert "response_sha256" not in proof
    assert proof["attempts"][0]["response_sha256"]


def test_cover_punch_validator_binds_current_retry_lineage():
    cover_text = "公主抱又扯到脐带"
    story_hook = "公主抱荒诞地牵扯到脐带。"

    def accept(_prompt: str) -> str:
        return json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "status": "PASS",
                "final_punch": {"main": cover_text, "sub": None},
                "stranger_can_infer_event": True,
                "contains_concrete_subject": True,
                "contains_action_or_conflict": True,
                "no_fabricated_fact": True,
                "story_summary": "公主抱荒诞地牵扯到脐带。",
                "click_motivation": "想知道这段关系梗如何失控。",
            },
            ensure_ascii=False,
        )

    reviewed, proof = review_cover_punch_semantics(
        title="【李豆沙】" + cover_text,
        cover_text=cover_text,
        story_hook=story_hook,
        punch=(cover_text,),
        llm_call=accept,
    )
    lines = list(reviewed)
    assert validate_cover_punch_semantic_review(
        proof,
        rendered_lines=lines,
        cover_text=cover_text,
        story_hook=story_hook,
    )
    for mutate in (
        lambda value: value.update(attempt_count=999),
        lambda value: value.update(attempts=[]),
        lambda value: (value.pop("attempt_count"), value.pop("attempts")),
        lambda value: value.update(request_sha256="0" * 64),
        lambda value: value.update(response_sha256="0" * 64),
    ):
        tampered = copy.deepcopy(proof)
        mutate(tampered)
        assert not validate_cover_punch_semantic_review(
            tampered,
            rendered_lines=lines,
            cover_text=cover_text,
            story_hook=story_hook,
        )

    downgraded = copy.deepcopy(proof)
    downgraded.pop("attempt_count")
    downgraded.pop("attempts")
    assert not validate_cover_punch_semantic_review(
        downgraded,
        rendered_lines=lines,
        cover_text=cover_text,
        story_hook=story_hook,
        allow_legacy_retryless=True,
    )


def test_cover_punch_validator_rejects_tampered_failed_attempt_response_hash():
    cover_text = "公主抱又扯到脐带"
    calls = 0

    def malformed_then_accept(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            return "not json"
        return json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "status": "PASS",
                "final_punch": {"main": cover_text, "sub": None},
                "stranger_can_infer_event": True,
                "contains_concrete_subject": True,
                "contains_action_or_conflict": True,
                "no_fabricated_fact": True,
                "story_summary": "公主抱荒诞地牵扯到脐带。",
                "click_motivation": "想知道这段关系梗如何失控。",
            },
            ensure_ascii=False,
        )

    reviewed, proof = review_cover_punch_semantics(
        title="【李豆沙】" + cover_text,
        cover_text=cover_text,
        story_hook=cover_text,
        punch=(cover_text,),
        llm_call=malformed_then_accept,
    )
    assert validate_cover_punch_semantic_review(
        proof,
        rendered_lines=list(reviewed),
        cover_text=cover_text,
        story_hook=cover_text,
    )
    proof["attempts"][0]["response_sha256"] = "not-a-digest"
    assert not validate_cover_punch_semantic_review(
        proof,
        rendered_lines=list(reviewed),
        cover_text=cover_text,
        story_hook=cover_text,
    )


def test_cover_punch_rejects_all_non_physical_line_separators_from_source():
    for separator in ("\x0b", "\x0c", "\x85", "\u2028", "\u2029"):
        cover_text = f"公主抱{separator}又扯到脐带"
        assert not _validated_extractive_punch(
            {"main": cover_text, "sub": None}, cover_text
        )


def test_cover_punch_preserves_source_line_boundary_before_quoted_statement():
    proposal = {"main": "播放自己的录音", "sub": "赞同又反对"}
    for separator in ("\n", "\r\n", "\u2028", "\u2029"):
        cover_text = f"播放自己的录音{separator}“赞同”和“反对”交替"
        assert _validated_extractive_punch(proposal, cover_text) == (
            "播放自己的录音", "赞同又反对",
        )
    # Whitespace on the same source line still cannot hide a cut object.
    for separator in ("", " ", "\t"):
        assert not _validated_extractive_punch(
            {"main": "永久保留", "sub": None},
            f"永久保留{separator}“完整表情”",
        )


def test_cover_punch_accepts_source_backed_stance_hook_without_action_flag():
    cover_text = "主播称赞这套角色好看却声明自己完全不代入"
    story_hook = "主播称赞这套角色好看，却声明自己完全不代入。"

    def accept(_prompt: str) -> str:
        return json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "status": "PASS",
                "final_punch": {"main": "角色好看", "sub": "完全不代入"},
                "stranger_can_infer_event": True,
                "contains_concrete_subject": True,
                "contains_action_or_conflict": False,
                "no_fabricated_fact": True,
                "story_summary": "主播称赞角色好看，却声明自己不代入。",
                "click_motivation": "这个偏好与立场的反差让人想点开。",
            },
            ensure_ascii=False,
        )

    reviewed, proof = review_cover_punch_semantics(
        title="【李豆沙】" + cover_text,
        cover_text=cover_text,
        story_hook=story_hook,
        punch=("角色好看", "完全不代入"),
        llm_call=accept,
    )

    assert reviewed == ("角色好看", "完全不代入")
    assert proof["contains_action_or_conflict"] is False
    assert validate_cover_punch_semantic_review(
        proof,
        rendered_lines=list(reviewed),
        cover_text=cover_text,
        story_hook=story_hook,
    )

    missing_action = copy.deepcopy(proof)
    missing_action.pop("contains_action_or_conflict")
    assert not validate_cover_punch_semantic_review(
        missing_action,
        rendered_lines=list(reviewed),
        cover_text=cover_text,
        story_hook=story_hook,
    )


def test_cover_punch_accepts_colon_without_dropping_semantic_proof():
    cover_text = "主播称赞这套角色好看却声明自己完全不代入"
    punch = ("角色：好看", "完全:不代入")

    def accept(_prompt: str) -> str:
        return json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "status": "PASS",
                "final_punch": {"main": punch[0], "sub": punch[1]},
                "stranger_can_infer_event": True,
                "contains_concrete_subject": True,
                "contains_action_or_conflict": False,
                "no_fabricated_fact": True,
                "story_summary": "主播称赞角色好看，却声明自己不代入。",
                "click_motivation": "这个偏好与立场的反差让人想点开。",
            },
            ensure_ascii=False,
        )

    def permissive_shape(_value: object, _cover_text: str) -> tuple[str, ...]:
        return punch

    reviewed, proof = review_cover_punch_semantics(
        title="【李豆沙】" + cover_text,
        cover_text=cover_text,
        story_hook=cover_text,
        punch=punch,
        llm_call=accept,
        punch_validator=permissive_shape,
    )

    assert reviewed == punch
    assert proof["status"] == "PASS"
    assert validate_cover_punch_semantic_review(
        proof, rendered_lines=list(punch), cover_text=cover_text, story_hook=cover_text,
    )
