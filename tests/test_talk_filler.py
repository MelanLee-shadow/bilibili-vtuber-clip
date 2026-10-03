import json
from pathlib import Path
from types import SimpleNamespace

from src.autoslice.talk_filler import (
    bind_final_filler_audit_to_burn,
    build_piece_specs,
    build_talk_filler_plan,
    verify_automatic_filler_plan,
    write_final_filler_audit,
)


def cue(position: int, start_ms: int, end_ms: int, text: str):
    return SimpleNamespace(
        cue_id=f"cue-{position}",
        source_start_ms=start_ms,
        source_end_ms=end_ms,
        text=text,
    )


def test_reviewed_three_approved_jumps_plan_reproduces_all_three_jumps():
    plan = build_talk_filler_plan(
        start_ms=527_500,
        end_ms=695_400,
        cues=[],
        reviewed_removals=[
            {
                "start_ms": 559_670,
                "end_ms": 569_730,
                "reason": "gift_thanks",
                "authority": "user_reviewed_2026-07-18",
            },
            {
                "start_ms": 603_170,
                "end_ms": 611_530,
                "reason": "gift_thanks",
                "authority": "user_reviewed_2026-07-18",
            },
            {
                "start_ms": 638_270,
                "end_ms": 650_250,
                "reason": "gift_thanks",
                "authority": "user_reviewed_2026-07-18",
            },
        ],
    )

    assert plan["status"] == "active"
    assert plan["effective_duration_ms"] == 137_500
    assert [
        row["content_output_jump_ms"] for row in plan["removals"]
    ] == [32_170, 65_610, 92_350]
    assert plan["retained_intervals"] == [
        {"start_ms": 527_500, "end_ms": 559_670},
        {"start_ms": 569_730, "end_ms": 603_170},
        {"start_ms": 611_530, "end_ms": 638_270},
        {"start_ms": 650_250, "end_ms": 695_400},
    ]


def test_reviewed_removal_requires_explicit_nondefault_authority():
    for authority in (None, "", "user_reviewed", "cpa_semantic_pass", "auto_reviewed"):
        reviewed = {
            "start_ms": 10_000,
            "end_ms": 15_000,
            "reason": "gift_thanks",
        }
        if authority is not None:
            reviewed["authority"] = authority
        plan = build_talk_filler_plan(
            start_ms=0,
            end_ms=70_000,
            cues=[],
            reviewed_removals=[reviewed],
        )

        assert plan["status"] == "contiguous_fallback"
        assert plan["removals"] == []
        assert any(
            row.get("reason_code") == "CONSERVATIVE_POLICY_REQUIRES_USER_APPROVAL"
            for row in plan["rejected_proposals"]
        )


def test_automatic_gift_thanks_requires_bound_srt_and_safe_bridge(tmp_path: Path):
    srt = tmp_path / "source.srt"
    srt.write_text("bound transcript\n", encoding="utf-8")
    import hashlib

    digest = "sha256:" + hashlib.sha256(srt.read_bytes()).hexdigest()
    cues = [
        cue(1, 0, 10_000, "前面的话题"),
        cue(2, 10_500, 12_000, "谢谢阿甲的粉丝灯牌"),
        cue(3, 12_000, 14_000, "谢谢你呀"),
        cue(4, 16_000, 20_000, "继续刚才的话题"),
        cue(5, 20_500, 70_000, "把同一件事讲完"),
    ]

    plan = build_talk_filler_plan(
        start_ms=0,
        end_ms=70_000,
        cues=cues,
        proposals=[
            {
                "proposal_id": "gift-1",
                "mode": "remove_cues",
                "start_cue": 2,
                "end_cue": 3,
                "reason": "gift_thanks",
                "bridge_coherent": True,
                "bridge": "左右都在讲同一件事",
                "topic_relation": "unrelated",
                "contains_setup": False,
                "contains_cause": False,
                "contains_answer": False,
                "contains_punchline": False,
                "contains_referent_intro": False,
                "contains_correction": False,
                "contains_resolution": False,
                "later_dependency": False,
                "interaction_relevant": False,
                "meaning_or_stance_changed": False,
                "confidence": 0.98,
            }
        ],
        proposal_srt_sha256=digest,
        source_srt_path=srt,
    )

    assert plan["status"] == "active"
    assert plan["removals"][0]["source_start_ms"] == 10_500
    assert plan["removals"][0]["source_end_ms"] == 14_000
    assert plan["retained_intervals"] == [
        {"start_ms": 0, "end_ms": 10_500},
        {"start_ms": 14_000, "end_ms": 70_000},
    ]
    assert plan["removals"][0]["left_retained_context"]["text"] == "前面的话题"
    assert plan["removals"][0]["right_retained_context"]["text"] == "继续刚才的话题"


def test_gift_thanks_without_specific_gift_is_kept(tmp_path: Path):
    srt = tmp_path / "source.srt"
    srt.write_text("bound transcript\n", encoding="utf-8")
    import hashlib

    digest = "sha256:" + hashlib.sha256(srt.read_bytes()).hexdigest()
    plan = build_talk_filler_plan(
        start_ms=0,
        end_ms=70_000,
        cues=[
            cue(1, 0, 10_000, "主题开头"),
            cue(2, 11_000, 14_000, "谢谢你呀"),
            cue(3, 16_000, 70_000, "主题继续到结尾"),
        ],
        proposals=[
            {
                "proposal_id": "generic-thanks",
                "mode": "remove_cues",
                "start_cue": 2,
                "end_cue": 2,
                "reason": "gift_thanks",
                "bridge_coherent": True,
                "bridge": "两侧继续同一主题",
                "topic_relation": "unrelated",
                "contains_setup": False,
                "contains_cause": False,
                "contains_answer": False,
                "contains_punchline": False,
                "contains_referent_intro": False,
                "contains_correction": False,
                "contains_resolution": False,
                "later_dependency": False,
                "interaction_relevant": False,
                "meaning_or_stance_changed": False,
                "confidence": 0.99,
            }
        ],
        proposal_srt_sha256=digest,
        source_srt_path=srt,
    )
    assert plan["status"] == "contiguous_fallback"
    assert plan["removals"] == []
    assert plan["rejected_proposals"][0]["reason_code"] == (
        "gift_thanks_has_no_specific_gift_witness"
    )


def test_unsupported_generic_aside_is_kept_even_with_explicit_semantics(tmp_path: Path):
    srt = tmp_path / "source.srt"
    srt.write_text("actual\n", encoding="utf-8")
    cues = [
        cue(1, 0, 10_000, "开头"),
        cue(2, 11_000, 14_000, "另外一件事"),
        cue(3, 16_000, 70_000, "结尾"),
    ]
    proposal = {
        "mode": "remove_cues",
        "start_cue": 2,
        "end_cue": 2,
        "reason": "unrelated_aside",
        "bridge_coherent": True,
        "bridge": "模型认为可直连",
        "confidence": 0.99,
    }

    mismatch = build_talk_filler_plan(
        start_ms=0,
        end_ms=70_000,
        cues=cues,
        proposals=[proposal],
        proposal_srt_sha256="sha256:not-the-file",
        source_srt_path=srt,
    )
    assert mismatch["status"] == "contiguous_fallback_srt_binding_failed"

    import hashlib

    digest = "sha256:" + hashlib.sha256(srt.read_bytes()).hexdigest()
    unsupported = build_talk_filler_plan(
        start_ms=0,
        end_ms=70_000,
        cues=cues,
        proposals=[proposal],
        proposal_srt_sha256=digest,
        source_srt_path=srt,
    )
    assert unsupported["status"] == "contiguous_fallback"
    assert unsupported["rejected_proposals"][0]["reason_code"] == (
        "automatic_reason_not_allowed"
    )

    semantic_deny_fields = {
        "contains_setup": False,
        "contains_cause": False,
        "contains_answer": False,
        "contains_punchline": False,
        "contains_referent_intro": False,
        "contains_correction": False,
        "contains_resolution": False,
        "later_dependency": False,
        "interaction_relevant": False,
        "meaning_or_stance_changed": False,
    }
    approved = build_talk_filler_plan(
        start_ms=0,
        end_ms=70_000,
        cues=cues,
        proposals=[
            {
                **proposal,
                "topic_relation": "unrelated",
                **semantic_deny_fields,
            }
        ],
        proposal_srt_sha256=digest,
        source_srt_path=srt,
    )
    assert approved["status"] == "contiguous_fallback"
    assert approved["removals"] == []
    assert approved["rejected_proposals"][0]["reason_code"] == (
        "automatic_reason_not_allowed"
    )

    denied = build_talk_filler_plan(
        start_ms=0,
        end_ms=70_000,
        cues=cues,
        proposals=[
            {
                **proposal,
                "topic_relation": "incidental",
                **semantic_deny_fields,
            }
        ],
        proposal_srt_sha256=digest,
        source_srt_path=srt,
    )
    assert denied["status"] == "contiguous_fallback"
    assert denied["rejected_proposals"][0]["reason_code"] == (
        "automatic_reason_not_allowed"
    )


def test_housekeeping_with_no_topic_content_can_be_removed(tmp_path: Path):
    srt = tmp_path / "source.srt"
    srt.write_text("bound transcript\n", encoding="utf-8")
    import hashlib

    digest = "sha256:" + hashlib.sha256(srt.read_bytes()).hexdigest()
    deny = {
        "contains_setup": False,
        "contains_cause": False,
        "contains_answer": False,
        "contains_punchline": False,
        "contains_referent_intro": False,
        "contains_correction": False,
        "contains_resolution": False,
        "later_dependency": False,
        "interaction_relevant": False,
        "meaning_or_stance_changed": False,
    }
    plan = build_talk_filler_plan(
        start_ms=0,
        end_ms=70_000,
        cues=[
            cue(1, 0, 10_000, "主题开头"),
            cue(2, 11_000, 14_000, "我去喝口水回来"),
            cue(3, 16_000, 70_000, "主题继续到结尾"),
        ],
        proposals=[
            {
                "proposal_id": "water",
                "mode": "remove_cues",
                "start_cue": 2,
                "end_cue": 2,
                "reason": "housekeeping",
                "bridge_coherent": True,
                "bridge": "两侧继续同一主题",
                "topic_relation": "unrelated",
                **deny,
                "confidence": 0.99,
            }
        ],
        proposal_srt_sha256=digest,
        source_srt_path=srt,
    )
    assert plan["status"] == "active"
    assert plan["removals"][0]["reason"] == "housekeeping"


def test_housekeeping_common_water_and_toilet_phrases_are_recognized(tmp_path: Path):
    srt = tmp_path / "source.srt"
    srt.write_text("bound transcript\n", encoding="utf-8")
    import hashlib

    digest = "sha256:" + hashlib.sha256(srt.read_bytes()).hexdigest()
    deny = {
        "contains_setup": False,
        "contains_cause": False,
        "contains_answer": False,
        "contains_punchline": False,
        "contains_referent_intro": False,
        "contains_correction": False,
        "contains_resolution": False,
        "later_dependency": False,
        "interaction_relevant": False,
        "meaning_or_stance_changed": False,
    }
    for index, phrase in enumerate(("我去喝杯水", "我去喝一杯水", "我去上个厕所")):
        plan = build_talk_filler_plan(
            start_ms=0,
            end_ms=70_000,
            cues=[
                cue(1, 0, 10_000, "主题开头"),
                cue(2, 11_000, 14_000, phrase),
                cue(3, 16_000, 70_000, "主题继续到结尾"),
            ],
            proposals=[
                {
                    "proposal_id": f"housekeeping-{index}",
                    "mode": "remove_cues",
                    "start_cue": 2,
                    "end_cue": 2,
                    "reason": "housekeeping",
                    "bridge_coherent": True,
                    "bridge": "两侧继续同一主题",
                    "topic_relation": "unrelated",
                    **deny,
                    "confidence": 0.99,
                }
            ],
            proposal_srt_sha256=digest,
            source_srt_path=srt,
        )
        assert plan["status"] == "active"
        assert plan["removals"][0]["reason"] == "housekeeping"


def test_unrelated_sc_without_bound_original_evidence_is_kept(tmp_path: Path):
    srt = tmp_path / "source.srt"
    srt.write_text("bound transcript\n", encoding="utf-8")
    import hashlib

    digest = "sha256:" + hashlib.sha256(srt.read_bytes()).hexdigest()
    deny = {
        "contains_setup": False,
        "contains_cause": False,
        "contains_answer": False,
        "contains_punchline": False,
        "contains_referent_intro": False,
        "contains_correction": False,
        "contains_resolution": False,
        "later_dependency": False,
        "interaction_relevant": False,
        "meaning_or_stance_changed": False,
    }
    plan = build_talk_filler_plan(
        start_ms=0,
        end_ms=70_000,
        cues=[
            cue(1, 0, 10_000, "主题开头"),
            cue(2, 11_000, 14_000, "请问能不能聊一下别的事"),
            cue(3, 16_000, 70_000, "主题继续到结尾"),
        ],
        proposals=[
            {
                "proposal_id": "sc-without-source",
                "mode": "remove_cues",
                "start_cue": 2,
                "end_cue": 2,
                "reason": "unrelated_sc",
                "bridge_coherent": True,
                "bridge": "两侧继续同一主题",
                "topic_relation": "unrelated",
                **deny,
                "confidence": 0.99,
            }
        ],
        proposal_srt_sha256=digest,
        source_srt_path=srt,
    )
    assert plan["status"] == "contiguous_fallback"
    assert plan["removals"] == []
    assert plan["rejected_proposals"][0]["reason_code"] == (
        "structured_chat_binding_missing"
    )


def test_unrelated_sc_uses_hash_bound_superchat_witness(tmp_path: Path):
    srt = tmp_path / "source.srt"
    srt.write_text("bound transcript\n", encoding="utf-8")
    origin = 1_700_000_000_000
    jsonl = tmp_path / "source.jsonl"
    jsonl.write_text(
        json.dumps(
            {
                "cmd": "SUPER_CHAT_MESSAGE",
                "send_time": origin + 12_000,
                "data": {
                    "id": "sc-1",
                    "message": "请问能不能聊一下别的事",
                    "user_info": {"uname": "viewer"},
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    import hashlib

    digest = "sha256:" + hashlib.sha256(srt.read_bytes()).hexdigest()
    chat_digest = "sha256:" + hashlib.sha256(jsonl.read_bytes()).hexdigest()
    deny = {
        "contains_setup": False,
        "contains_cause": False,
        "contains_answer": False,
        "contains_punchline": False,
        "contains_referent_intro": False,
        "contains_correction": False,
        "contains_resolution": False,
        "later_dependency": False,
        "interaction_relevant": False,
        "meaning_or_stance_changed": False,
    }
    plan = build_talk_filler_plan(
        start_ms=0,
        end_ms=70_000,
        cues=[
            cue(1, 0, 10_000, "主题开头"),
            cue(2, 11_000, 14_000, "请问能不能聊一下别的事"),
            cue(3, 16_000, 70_000, "主题继续到结尾"),
        ],
        proposals=[
            {
                "proposal_id": "sc-with-source",
                "mode": "remove_cues",
                "start_cue": 2,
                "end_cue": 2,
                "reason": "unrelated_sc",
                "bridge_coherent": True,
                "bridge": "两侧继续同一主题",
                "topic_relation": "unrelated",
                **deny,
                "confidence": 0.99,
            }
        ],
        proposal_srt_sha256=digest,
        source_srt_path=srt,
        structured_chat_binding={
            "chat_jsonl": str(jsonl),
            "chat_jsonl_sha256": chat_digest,
            "chat_origin_epoch_ms": origin,
            "chat_timeline_offset_ms": 0,
            "structured_chat_required": True,
            "chat_binding_status": "BOUND_DIRECT",
            "chat_binding_authority": "test",
        },
    )
    assert plan["status"] == "active"
    assert plan["removals"][0]["structured_chat_witness"][0]["text"].startswith(
        "请问能不能"
    )
    assert plan["removals"][0]["structured_chat_witness"][0][
        "removed_text_match"
    ] is True


def test_unrelated_sc_nearby_but_not_read_in_removed_text_is_kept(tmp_path: Path):
    srt = tmp_path / "source.srt"
    srt.write_text("bound transcript\n", encoding="utf-8")
    origin = 1_700_000_000_000
    jsonl = tmp_path / "source.jsonl"
    jsonl.write_text(
        json.dumps(
            {
                "cmd": "SUPER_CHAT_MESSAGE",
                "send_time": origin + 12_000,
                "data": {
                    "id": "sc-nearby",
                    "message": "请问能不能聊一下别的事",
                    "user_info": {"uname": "viewer"},
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    import hashlib

    digest = "sha256:" + hashlib.sha256(srt.read_bytes()).hexdigest()
    chat_digest = "sha256:" + hashlib.sha256(jsonl.read_bytes()).hexdigest()
    deny = {
        "contains_setup": False,
        "contains_cause": False,
        "contains_answer": False,
        "contains_punchline": False,
        "contains_referent_intro": False,
        "contains_correction": False,
        "contains_resolution": False,
        "later_dependency": False,
        "interaction_relevant": False,
        "meaning_or_stance_changed": False,
    }
    plan = build_talk_filler_plan(
        start_ms=0,
        end_ms=70_000,
        cues=[
            cue(1, 0, 10_000, "主题开头"),
            cue(2, 11_000, 14_000, "我读到另一个问题"),
            cue(3, 16_000, 70_000, "主题继续到结尾"),
        ],
        proposals=[
            {
                "proposal_id": "sc-nearby-not-read",
                "mode": "remove_cues",
                "start_cue": 2,
                "end_cue": 2,
                "reason": "unrelated_sc",
                "bridge_coherent": True,
                "bridge": "两侧继续同一主题",
                "topic_relation": "unrelated",
                **deny,
                "confidence": 0.99,
            }
        ],
        proposal_srt_sha256=digest,
        source_srt_path=srt,
        structured_chat_binding={
            "chat_jsonl": str(jsonl),
            "chat_jsonl_sha256": chat_digest,
            "chat_origin_epoch_ms": origin,
            "chat_timeline_offset_ms": 0,
            "structured_chat_required": True,
            "chat_binding_status": "BOUND_DIRECT",
            "chat_binding_authority": "test",
        },
    )
    assert plan["status"] == "contiguous_fallback"
    assert plan["removals"] == []
    assert plan["rejected_proposals"][0]["reason_code"] == (
        "structured_chat_sc_not_read_in_removed_text"
    )


def test_automatic_trim_at_or_below_60500ms_falls_back_to_contiguous(tmp_path: Path):
    srt = tmp_path / "source.srt"
    srt.write_text("bound transcript\n", encoding="utf-8")
    import hashlib

    digest = "sha256:" + hashlib.sha256(srt.read_bytes()).hexdigest()
    deny = {
        "contains_setup": False,
        "contains_cause": False,
        "contains_answer": False,
        "contains_punchline": False,
        "contains_referent_intro": False,
        "contains_correction": False,
        "contains_resolution": False,
        "later_dependency": False,
        "interaction_relevant": False,
        "meaning_or_stance_changed": False,
    }
    plan = build_talk_filler_plan(
        start_ms=0,
        end_ms=63_500,
        cues=[
            cue(1, 0, 10_000, "主题开头"),
            cue(2, 11_000, 14_000, "谢谢阿甲的粉丝灯牌"),
            cue(3, 16_000, 63_500, "主题继续到结尾"),
        ],
        proposals=[
            {
                "proposal_id": "short-after-trim",
                "mode": "remove_cues",
                "start_cue": 2,
                "end_cue": 2,
                "reason": "gift_thanks",
                "bridge_coherent": True,
                "bridge": "两侧继续同一主题",
                "topic_relation": "unrelated",
                **deny,
                "confidence": 0.99,
            }
        ],
        proposal_srt_sha256=digest,
        source_srt_path=srt,
    )
    assert plan["status"] == "contiguous_fallback"
    assert plan["effective_duration_ms"] == 63_500
    assert any(
        row["reason_code"] == "EFFECTIVE_DURATION_NOT_OVER_60S"
        for row in plan["rejected_proposals"]
    )


def test_housekeeping_still_requires_existing_global_semantic_pass():
    cues = [
        cue(1, 0, 10_000, "主题开始，先说明问题"),
        cue(2, 20_000, 25_000, "完全无关的插话"),
        cue(3, 30_000, 70_000, "主题继续并完成笑点"),
    ]
    plan = {
        "retained_intervals": [
            {"start_ms": 0, "end_ms": 10_000},
            {"start_ms": 25_000, "end_ms": 70_000},
        ],
        "removals": [
            {
                "proposal_id": "aside",
                "source_start_ms": 10_000,
                "source_end_ms": 25_000,
                "authorization_kind": "automatic",
                "reason": "housekeeping",
            }
        ],
    }
    complete = {
        "topic_complete": True,
        "cause_answer_chain_complete": True,
        "referents_resolved": True,
        "corrections_preserved": True,
        "punchline_preserved": True,
        "closing_resolution_preserved": True,
        "audience_interactions_consistent": True,
        "audience_feedback_preserved": True,
        "meaningful_repeats_preserved": True,
        "meaning_or_stance_changed": False,
        "pass": True,
        "reason": "删除后主题、笑点和衔接完整",
    }

    passed = verify_automatic_filler_plan(
        cues=cues,
        plan=plan,
        llm_call=lambda prompt: json.dumps(complete, ensure_ascii=False),
    )
    assert passed["status"] == "PASS"

    denied = dict(complete)
    denied["referents_resolved"] = False
    denied["pass"] = False
    failed = verify_automatic_filler_plan(
        cues=cues,
        plan=plan,
        llm_call=lambda prompt: json.dumps(denied, ensure_ascii=False),
    )
    assert failed["status"] == "FAIL"


def test_global_verifier_prompt_contains_original_source_and_removed_cues():
    cues = [
        cue(1, 0, 5_000, "主题开头和上下文"),
        cue(2, 5_000, 10_000, "相关SC实质回答：我来解释这个主题"),
        cue(3, 10_000, 15_000, "笑点铺垫：先这样再反转"),
        cue(4, 15_000, 40_000, "保留的主题收束和笑点结论"),
    ]
    plan = {
        "source_start_ms": 0,
        "source_end_ms": 40_000,
        "retained_intervals": [
            {"start_ms": 0, "end_ms": 5_000},
            {"start_ms": 15_000, "end_ms": 40_000},
        ],
        "removals": [
            {
                "proposal_id": "aside",
                "source_start_ms": 5_000,
                "source_end_ms": 15_000,
                "reason": "housekeeping",
                "authorization_kind": "automatic",
            }
        ],
    }
    payload = {
        "topic_complete": True,
        "cause_answer_chain_complete": True,
        "referents_resolved": True,
        "corrections_preserved": True,
        "punchline_preserved": True,
        "closing_resolution_preserved": True,
        "audience_interactions_consistent": True,
        "audience_feedback_preserved": True,
        "meaningful_repeats_preserved": True,
        "meaning_or_stance_changed": False,
        "pass": True,
        "reason": "完整",
    }
    prompts: list[str] = []

    result = verify_automatic_filler_plan(
        cues=cues,
        plan=plan,
        llm_call=lambda prompt: prompts.append(prompt)
        or json.dumps(payload, ensure_ascii=False),
    )

    assert result["status"] == "PASS"
    assert len(prompts) == 1
    prompt = prompts[0]
    assert "<SOURCE 0-40000ms>" in prompt
    assert "保留的主题收束和笑点结论" in prompt
    removed_start = prompt.index("<REMOVED aside 5000-15000ms")
    removed_end = prompt.index("</REMOVED>", removed_start)
    removed_text = prompt[removed_start:removed_end]
    assert "相关SC实质回答" in removed_text
    assert "笑点铺垫" in removed_text
    assert "原始完整选中区间" in prompt
    assert "固定删除区间及删除前原话" in prompt
    assert "观众反馈是否完整保留" in prompt
    assert "audience_feedback_preserved" in prompt
    assert "有意义的重复回应" in prompt
    assert "meaningful_repeats_preserved" in prompt
    assert "removed_text_match=true" in prompt


def test_automatic_missing_original_text_never_reaches_model():
    plan = {
        "source_start_ms": 0,
        "source_end_ms": 70_000,
        "retained_intervals": [
            {"start_ms": 0, "end_ms": 10_000},
            {"start_ms": 25_000, "end_ms": 70_000},
        ],
        "removals": [{
            "proposal_id": "missing-original",
            "source_start_ms": 10_000,
            "source_end_ms": 25_000,
            "authorization_kind": "automatic",
            "reason": "housekeeping",
        }],
    }
    def forbidden_call(prompt):
        raise AssertionError("missing original text must fail before model dispatch")
    result = verify_automatic_filler_plan(
        cues=[cue(1, 0, 10_000, "保留主题开头"), cue(2, 30_000, 70_000, "保留主题结尾")],
        plan=plan,
        llm_call=forbidden_call,
    )
    assert result == {"status": "FAIL", "reason_code": "REMOVED_TRANSCRIPT_MISSING"}


def test_piece_specs_preserve_order_and_only_add_outer_context():
    item = {
        "segment_path": "/recording/source.mp4",
        "seg_dur_ms": 100_000,
        "xml": "/recording/source.xml",
        "chat_jsonl": "/recording/source.jsonl",
        "chat_jsonl_sha256": "sha256:" + "a" * 64,
        "chat_origin_epoch_ms": 1_750_000_000_000,
        "chat_timeline_offset_ms": 37,
        "structured_chat_required": True,
        "chat_source_alias_id": "official-replay-alias",
        "chat_canonical_recording_basename": "canonical.mp4",
        "chat_binding_status": "BOUND_SOURCE_ALIAS",
        "chat_binding_authority": "hash-bound test authority",
    }
    plan = {
        "retained_intervals": [
            {"start_ms": 10_000, "end_ms": 30_000},
            {"start_ms": 40_000, "end_ms": 80_000},
        ]
    }

    pieces = build_piece_specs(item=item, plan=plan, pre_ms=5_000, post_ms=7_000)

    assert [(row["start_ms"], row["end_ms"]) for row in pieces] == [
        (5_000, 30_000),
        (40_000, 87_000),
    ]
    assert all(row["remote_media"] == "/recording/source.mp4" for row in pieces)
    assert all(
        row["chat_jsonl_local"] == "/recording/source.jsonl"
        for row in pieces
    )
    for row in pieces:
        assert row["chat_jsonl_sha256"] == "sha256:" + "a" * 64
        assert row["chat_origin_epoch_ms"] == 1_750_000_000_000
        assert row["chat_timeline_offset_ms"] == 37
        assert row["structured_chat_required"] is True
        assert row["chat_source_alias_id"] == "official-replay-alias"
        assert row["chat_canonical_recording_basename"] == "canonical.mp4"
        assert row["chat_binding_status"] == "BOUND_SOURCE_ALIAS"
        assert row["chat_binding_authority"] == "hash-bound test authority"


def test_piece_specs_preserve_explicit_optional_absent_chat_state():
    pieces = build_piece_specs(
        item={
            "segment_path": "/recording/legacy.mp4",
            "seg_dur_ms": 20_000,
            "chat_jsonl": None,
            "structured_chat_required": False,
            "chat_binding_status": "OPTIONAL_ABSENT",
        },
        plan={
            "retained_intervals": [
                {"start_ms": 1_000, "end_ms": 10_000},
            ]
        },
        pre_ms=0,
        post_ms=0,
    )

    assert pieces[0]["structured_chat_required"] is False
    assert pieces[0]["chat_binding_status"] == "OPTIONAL_ABSENT"
    assert "chat_jsonl_local" not in pieces[0]


def test_global_verifier_requires_every_invariant_to_be_explicitly_true():
    cues = [
        cue(1, 0, 10_000, "前半段"),
        cue(2, 12_000, 15_000, "可删除的事务话语"),
        cue(3, 20_000, 70_000, "后半段"),
    ]
    plan = {
        "retained_intervals": [
            {"start_ms": 0, "end_ms": 10_000},
            {"start_ms": 20_000, "end_ms": 70_000},
        ],
        "removals": [
            {
                "proposal_id": "jump",
                "source_start_ms": 10_000,
                "source_end_ms": 20_000,
                "authorization_kind": "automatic",
            }
        ],
    }
    passed_payload = {
        "topic_complete": True,
        "cause_answer_chain_complete": True,
        "referents_resolved": True,
        "corrections_preserved": True,
        "punchline_preserved": True,
        "closing_resolution_preserved": True,
        "audience_interactions_consistent": True,
        "audience_feedback_preserved": True,
        "meaningful_repeats_preserved": True,
        "meaning_or_stance_changed": False,
        "pass": True,
        "reason": "完整",
    }
    captured_prompts: list[str] = []

    passed = verify_automatic_filler_plan(
        cues=cues,
        plan=plan,
        llm_call=lambda prompt: captured_prompts.append(prompt)
        or __import__("json").dumps(passed_payload),
    )
    assert passed["status"] == "PASS"
    assert "<SOURCE 0-70000ms>" in captured_prompts[0]

    missing_field = dict(passed_payload)
    missing_field.pop("audience_feedback_preserved")
    failed = verify_automatic_filler_plan(
        cues=cues,
        plan=plan,
        llm_call=lambda prompt: __import__("json").dumps(missing_field),
    )
    assert failed["status"] == "FAIL"
    false_field = dict(passed_payload)
    false_field["audience_feedback_preserved"] = False
    failed_false = verify_automatic_filler_plan(
        cues=cues,
        plan=plan,
        llm_call=lambda prompt: __import__("json").dumps(false_field),
    )
    assert failed_false["status"] == "FAIL"


def test_final_audit_records_content_and_delivered_jump_times(tmp_path: Path):
    plan = {
        "status": "active",
        "source_srt_sha256": "sha256:srt",
        "retained_intervals": [
            {"start_ms": 10_000, "end_ms": 30_000},
            {"start_ms": 40_000, "end_ms": 80_000},
        ],
        "removals": [
            {
                "proposal_id": "jump",
                "source_start_ms": 30_000,
                "source_end_ms": 40_000,
            }
        ],
        "rejected_proposals": [],
        "policy": {},
    }
    path = write_final_filler_audit(
        spec={
            "candidate_id": "talk",
            "talk_filler_plan": plan,
            "pieces": [
                {"remote_media": "seg1.mp4", "start_ms": 10_000, "end_ms": 30_000},
                {"remote_media": "seg1.mp4", "start_ms": 40_000, "end_ms": 80_000},
            ],
        },
        durations=[25_000, 47_000],
        final_start_ms=4_000,
        final_end_ms=70_000,
        piece_provenance_rows=[
            {"output_sha256": "left"},
            {"output_sha256": "right"},
        ],
        branding_intro={"video": {"duration_ms": 5_754}},
        output_path=tmp_path / "audit.json",
    )

    assert path is not None
    audit = json.loads(path.read_text(encoding="utf-8"))
    jump = audit["removals"][0]
    assert jump["actual_concat_jump_ms"] == 25_000
    assert jump["final_content_output_jump_ms"] == 21_000
    assert jump["delivered_output_jump_ms"] == 26_754


def test_final_audit_excludes_trailing_boundary_witness_reserve_piece(tmp_path: Path):
    """A cross-segment reserve piece (talk_lane's widened-context retry) must
    never be treated as one of the removal-mapped content pieces: the
    ``len(durations) == len(removals) + 1`` invariant is about content-piece
    gaps only, and the jump math must be byte-identical to the no-reserve
    case."""

    plan = {
        "status": "active",
        "source_srt_sha256": "sha256:srt",
        "retained_intervals": [
            {"start_ms": 10_000, "end_ms": 30_000},
            {"start_ms": 40_000, "end_ms": 80_000},
        ],
        "removals": [
            {
                "proposal_id": "jump",
                "source_start_ms": 30_000,
                "source_end_ms": 40_000,
            }
        ],
        "rejected_proposals": [],
        "policy": {},
    }
    path = write_final_filler_audit(
        spec={
            "candidate_id": "talk",
            "talk_filler_plan": plan,
            "pieces": [
                {"remote_media": "seg1.mp4", "start_ms": 10_000, "end_ms": 30_000},
                {"remote_media": "seg1.mp4", "start_ms": 40_000, "end_ms": 80_000},
                {
                    "remote_media": "seg2.mp4",
                    "start_ms": 0,
                    "end_ms": 88_000,
                    "piece_role": "boundary_witness_reserve",
                },
            ],
        },
        # One extra duration/provenance entry for the reserve piece, trailing
        # the two content pieces exactly as talk_lane appends it.
        durations=[25_000, 47_000, 88_000],
        final_start_ms=4_000,
        final_end_ms=70_000,
        piece_provenance_rows=[
            {"output_sha256": "left"},
            {"output_sha256": "right"},
            {"output_sha256": "reserve"},
        ],
        branding_intro={"video": {"duration_ms": 5_754}},
        output_path=tmp_path / "audit.json",
    )

    assert path is not None
    audit = json.loads(path.read_text(encoding="utf-8"))
    jump = audit["removals"][0]
    assert jump["actual_concat_jump_ms"] == 25_000
    assert jump["final_content_output_jump_ms"] == 21_000
    assert jump["delivered_output_jump_ms"] == 26_754
    assert jump["right_piece_output_sha256"] == "right"
    # The reserve piece's own provenance is still disclosed in full...
    assert audit["piece_provenance"] == [
        {"output_sha256": "left"},
        {"output_sha256": "right"},
        {"output_sha256": "reserve"},
    ]
    # ...but never surfaces as a removal-mapping endpoint.
    assert "reserve" not in {
        row.get("left_piece_output_sha256") for row in audit["removals"]
    }
    assert "reserve" not in {
        row.get("right_piece_output_sha256") for row in audit["removals"]
    }


def test_final_audit_rebinds_jump_times_to_actual_burned_intro(tmp_path: Path):
    audit_path = tmp_path / "audit.json"
    audit_path.write_text(
        json.dumps(
            {
                "schema_version": "talk-filler-audit.v1",
                "status": "FINALIZED",
                "branding_intro_offset_ms": 0,
                "removals": [
                    {
                        "final_content_output_jump_ms": 21_000,
                        "delivered_output_jump_ms": 21_000,
                        "survives_final_boundary": True,
                    },
                    {
                        "final_content_output_jump_ms": -500,
                        "delivered_output_jump_ms": None,
                        "survives_final_boundary": False,
                    },
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    bind_final_filler_audit_to_burn(
        audit_path=audit_path,
        burned_preview={
            "status": "BURNED",
            "branding_intro": {"status": "PREPENDED", "intro_offset_ms": 5_749},
        },
    )

    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    assert audit["branding_intro_offset_ms"] == 5_749
    assert audit["removals"][0]["delivered_output_jump_ms"] == 26_749
    assert audit["removals"][1]["delivered_output_jump_ms"] is None


def test_merge_gap_removal_requires_explicit_user_approval():
    """旧 selector merge_gap 输入不能绕过连续内容和用户批准门。"""
    plan = build_talk_filler_plan(
        start_ms=0,
        end_ms=660_000,
        cues=[],
        merge_gap_removals=[
            {"start_ms": 60_000, "end_ms": 565_000, "event_key": "甲甲称呼串"}
        ],
    )

    assert plan["status"] == "contiguous_fallback"
    assert plan["removals"] == []
    assert plan["retained_intervals"] == [{"start_ms": 0, "end_ms": 660_000}]
    assert plan["effective_duration_ms"] == 660_000
    assert any(
        row.get("reason_code") == "CONSERVATIVE_POLICY_REQUIRES_USER_APPROVAL"
        for row in plan["rejected_proposals"]
    )


def test_merge_gap_over_cap_is_rejected():
    from src.autoslice.talk_filler import MAX_MERGE_GAP_MS, build_talk_filler_plan

    plan = build_talk_filler_plan(
        start_ms=0,
        end_ms=1_500_000,
        cues=[],
        merge_gap_removals=[
            {"start_ms": 60_000, "end_ms": 60_000 + MAX_MERGE_GAP_MS + 1}
        ],
    )

    assert not [
        row
        for row in (plan.get("removals") or [])
        if row.get("authorization_kind") == "merge_gap"
    ]
    assert plan["status"] == "contiguous_fallback"
    assert plan["retained_intervals"] == [{"start_ms": 0, "end_ms": 1_500_000}]
    assert any(
        row.get("reason_code") == "CONSERVATIVE_POLICY_REQUIRES_USER_APPROVAL"
        for row in plan.get("rejected_proposals") or []
    )
