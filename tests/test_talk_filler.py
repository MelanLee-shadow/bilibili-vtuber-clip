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


def test_reviewed_july18_talk05_plan_reproduces_all_three_jumps():
    plan = build_talk_filler_plan(
        start_ms=527_500,
        end_ms=695_400,
        cues=[],
        reviewed_removals=[
            {
                "start_ms": 559_670,
                "end_ms": 569_730,
                "reason": "gift_thanks",
                "authority": "ivan_reviewed_2026-07-18",
            },
            {
                "start_ms": 603_170,
                "end_ms": 611_530,
                "reason": "gift_thanks",
                "authority": "ivan_reviewed_2026-07-18",
            },
            {
                "start_ms": 638_270,
                "end_ms": 650_250,
                "reason": "gift_thanks",
                "authority": "ivan_reviewed_2026-07-18",
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
                "topic_relation": "incidental",
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
    assert plan["removals"][0]["source_start_ms"] == 10_000
    assert plan["removals"][0]["source_end_ms"] == 16_000
    assert plan["removals"][0]["left_retained_context"]["text"] == "前面的话题"
    assert plan["removals"][0]["right_retained_context"]["text"] == "继续刚才的话题"


def test_unrelated_aside_and_srt_mismatch_fail_back_to_contiguous(tmp_path: Path):
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
        "unrelated_aside_requires_later_semantic_authorizer"
    )


def test_piece_specs_preserve_order_and_only_add_outer_context():
    item = {
        "segment_path": "/recording/source.mp4",
        "seg_dur_ms": 100_000,
        "xml": "/recording/source.xml",
        "chat_jsonl": "/recording/source.jsonl",
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


def test_global_verifier_requires_every_invariant_to_be_explicitly_true():
    cues = [
        cue(1, 0, 10_000, "前半段"),
        cue(2, 20_000, 70_000, "后半段"),
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
        "meaning_or_stance_changed": False,
        "pass": True,
        "reason": "完整",
    }

    passed = verify_automatic_filler_plan(
        cues=cues,
        plan=plan,
        llm_call=lambda prompt: __import__("json").dumps(passed_payload),
    )
    assert passed["status"] == "PASS"

    missing_field = dict(passed_payload)
    missing_field.pop("referents_resolved")
    failed = verify_automatic_filler_plan(
        cues=cues,
        plan=plan,
        llm_call=lambda prompt: __import__("json").dumps(missing_field),
    )
    assert failed["status"] == "FAIL"


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
        spec={"candidate_id": "talk", "talk_filler_plan": plan},
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
