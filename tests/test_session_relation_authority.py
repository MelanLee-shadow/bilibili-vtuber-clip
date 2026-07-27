from pathlib import Path

from src.autoslice.session_relation_authority import resolve_session_relation
from src.autoslice.story_contract import (
    audit_story_artifact,
    build_story_contract,
    canonicalize_relation_summary,
    cover_relation_prompt,
)
from src.autoslice.producer_package_finalization import _audit_story_bound_cover


REPO_ROOT = Path(__file__).resolve().parents[1]


def _authority() -> dict[str, object]:
    authority = resolve_session_relation(
        ledger_path=REPO_ROOT / "assets/lidousha/session_relation_ledger.v1.json",
        date="2026-07-22",
        recording_path="/recovery/22966160_20260722-19-34-50.mp4",
        source_sha256=(
            "sha256:"
            "0eb2778dc53e5eabbccae089e5db92d3fb3662d90e1dd2ddbe7765436718989a"
        ),
    )
    assert authority is not None
    return authority


def test_committed_relation_is_independent_of_no_trigger_sidecar() -> None:
    authority = _authority()
    assert authority["state"] == "CONFIRMED"
    assert [row["display_name"] for row in authority["participants"]] == [
        "李豆沙",
        "南町",
    ]


def test_generated_hook_canonicalizes_name_slot_but_not_common_phrase() -> None:
    authority = _authority()
    assert canonicalize_relation_summary(
        "被追问为什么请大恩吃火锅，最后承认最喜欢大恩",
        session_relation_authority=authority,
    ) == "被追问为什么请南町吃火锅，最后承认最喜欢南町"
    assert canonicalize_relation_summary(
        "滴水之恩涌泉相报，这是大恩大德",
        session_relation_authority=authority,
    ) == "滴水之恩涌泉相报，这是大恩大德"


def test_story_contract_rejects_cross_artifact_nancho_outlier() -> None:
    authority = _authority()
    contract = build_story_contract(
        candidate_id="c1",
        selection_hook="南町当面追问李豆沙",
        transcript_text="大N老师问她为什么",
        selection_scorecard=None,
        session_relation_authority=authority,
    )
    bad = audit_story_artifact(
        "【李豆沙】大卫老师当面追问",
        story_contract=contract,
        artifact_kind="title",
    )
    assert bad["status"] == "FAIL"
    assert {row["reason_code"] for row in bad["violations"]} == {
        "NANCHO_ALIAS_UNRESOLVED",
        "REQUIRED_NANCHO_ENTITY_MISSING_FROM_TITLE",
    }
    good = audit_story_artifact(
        "【李豆沙】南町当面追问最喜欢",
        story_contract=contract,
        artifact_kind="title",
    )
    assert good["status"] == "PASS"


def test_relation_claim_fails_when_authority_is_unknown() -> None:
    contract = build_story_contract(
        candidate_id="c2",
        selection_hook="普通聊天",
        transcript_text="普通聊天",
        selection_scorecard=None,
        session_relation_authority=None,
    )
    audit = audit_story_artifact(
        "【李豆沙】和南町联动",
        story_contract=contract,
        artifact_kind="title",
    )
    assert audit["status"] == "FAIL"
    assert audit["violations"][0]["reason_code"] == "UNCONFIRMED_RELATION_CLAIM"


def test_subtitle_verbatim_relation_words_are_not_claims() -> None:
    """1533 案（2026-07-27）：她口播「看看联动这边」是逐字实录——源语
    保真高于关系权威，字幕工件不受 relation-claim 门拦截；生成物（hook/
    标题）照旧上个测试锚死。"""

    contract = build_story_contract(
        candidate_id="c2b",
        selection_hook="普通聊天",
        transcript_text="看看联动这边",
        selection_scorecard=None,
        session_relation_authority=None,
    )
    audit = audit_story_artifact(
        "看看联动这边",
        story_contract=contract,
        artifact_kind="subtitle",
    )
    assert audit["status"] == "PASS"
    assert audit["violations"] == []


def test_confirmed_relation_cover_prompt_discloses_host_only_fallback() -> None:
    contract = build_story_contract(
        candidate_id="c3",
        selection_hook="南町与李豆沙联动",
        transcript_text="大N老师问她为什么",
        selection_scorecard=None,
        session_relation_authority=_authority(),
    )
    prompt = cover_relation_prompt(contract)
    assert "confirmed live collaboration" in prompt
    assert "HOST-ONLY" in prompt
    assert "do NOT invent" in prompt
    assert cover_relation_prompt(None) == ""


def test_runtime_cover_story_gate_catches_rendered_alias_drift() -> None:
    contract = build_story_contract(
        candidate_id="c4",
        selection_hook="南町当面追问李豆沙",
        transcript_text="大N老师问她为什么",
        selection_scorecard=None,
        session_relation_authority=_authority(),
    )
    binding = {
        key: contract[key]
        for key in (
            "schema_version",
            "relation_state",
            "participants",
            "source_media_sha256s",
            "cover_fallback_mode",
        )
    }
    codes, audits = _audit_story_bound_cover(
        {
            "cover_text": "南町当面追问",
            "cover_generation": {
                "cover_text": "南町当面追问",
                "rendered_lines": ["大恩当面追问"],
                "story_contract": binding,
            },
        },
        contract,
    )
    assert "NANCHO_ALIAS_UNRESOLVED" in codes
    assert len(audits) == 2
