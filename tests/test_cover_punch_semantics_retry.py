import copy
import json

from src.autoslice.cover_punch_semantics import (
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
                    "schema_version": "lidousha-cover-punch-semantic-review.v1",
                    "status": "REJECT",
                    "final_punch": None,
                    "stranger_can_infer_event": True,
                    "contains_concrete_subject": True,
                    "contains_action_or_conflict": True,
                    "story_summary": "公主抱荒诞地牵扯到脐带。",
                    "click_motivation": "想知道这段关系梗如何失控。",
                },
                ensure_ascii=False,
            )
        assert "唯一一次有界重试" in prompt
        return json.dumps(
            {
                "schema_version": "lidousha-cover-punch-semantic-review.v1",
                "status": "REVISE",
                "final_punch": {"main": "公主抱又扯到脐带", "sub": None},
                "stranger_can_infer_event": True,
                "contains_concrete_subject": True,
                "contains_action_or_conflict": True,
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
                "schema_version": "lidousha-cover-punch-semantic-review.v1",
                "status": "REJECT",
                "final_punch": None,
                "stranger_can_infer_event": False,
                "contains_concrete_subject": False,
                "contains_action_or_conflict": False,
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


def test_cover_punch_retry_rejects_candidate_inserted_whitespace():
    cover_text = "公主抱又扯到脐带"

    def spaced(_prompt: str) -> str:
        return json.dumps(
            {
                "schema_version": "lidousha-cover-punch-semantic-review.v1",
                "status": "REVISE",
                "final_punch": {"main": "公主抱 又扯到脐带", "sub": None},
                "stranger_can_infer_event": True,
                "contains_concrete_subject": True,
                "contains_action_or_conflict": True,
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
                "schema_version": "lidousha-cover-punch-semantic-review.v1",
                "status": "REJECT",
                "final_punch": None,
                "stranger_can_infer_event": False,
                "contains_concrete_subject": False,
                "contains_action_or_conflict": False,
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
                "schema_version": "lidousha-cover-punch-semantic-review.v1",
                "status": "PASS",
                "final_punch": {"main": cover_text, "sub": None},
                "stranger_can_infer_event": True,
                "contains_concrete_subject": True,
                "contains_action_or_conflict": True,
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
                "schema_version": "lidousha-cover-punch-semantic-review.v1",
                "status": "PASS",
                "final_punch": {"main": cover_text, "sub": None},
                "stranger_can_infer_event": True,
                "contains_concrete_subject": True,
                "contains_action_or_conflict": True,
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
