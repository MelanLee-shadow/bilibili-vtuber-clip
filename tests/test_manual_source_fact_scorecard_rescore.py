from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts.manual_source_fact_scorecard_rescore import (
    CPA_CALLER_TIMEOUT_SECONDS,
    CPA_COMMAND_TEMPLATE,
    CPA_MODEL,
    CPA_PROVIDER,
    CPA_TRANSPORT,
    EXECUTION_AUTHORITY_ENV,
    EXECUTION_AUTHORITY_VALUE,
    ManualScorecardRescoreError,
    _canonical_sha256,
    run_manual_rescore,
)
from src.autoslice.selection_scorecard import (
    normalize_selection_scorecard,
    selection_scorecard_is_valid,
)
from src.autoslice.source_fact_review import review_and_repair_source_facts


CANDIDATE_ID = "auto_100000_1_3"
ORIGINAL_HOOK = "技能已经被命名成李姐拉拉。"
ORIGINAL_TITLE = "【李豆沙】技能已经被命名成李姐拉拉"
CORRECTED_HOOK = "弹幕提议把技能叫李姐拉拉，主播随即拒绝。"
CORRECTED_TITLE = "【李豆沙】弹幕提议把技能叫李姐拉拉，主播随即拒绝"
FINAL_TRANSCRIPT = "技能可以叫李姐拉拉吗\n不行，我这个应该叫李姐网"


def _file_sha(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _stale_scorecard() -> dict[str, object]:
    card = normalize_selection_scorecard(
        {
            "tier": 2,
            "tier_basis": "personal_stance",
            "tier_reason": "旧 hook 下的态度表达",
            "tier_evidence_cues": [1, 2],
            "dimensions": {
                "lidousha_centrality": 4,
                "stance_intensity": 3,
                "audience_salience": 2,
                "relationship_interaction": 2,
                "persona_reversal": 2,
                "comedic_payoff": 3,
                "self_contained": 4,
            },
            "uncertainty_penalty": 1,
            "fatigue_penalty": 0,
        },
        start_cue=1,
        end_cue=2,
    )
    assert card is not None
    return card


def _stale_receipt(scorecard: dict[str, object]) -> dict[str, object]:
    def cpa(_prompt: str) -> str:
        return json.dumps(
            {
                "schema_version": "lidousha-source-fact-review.v1",
                "status": "REPAIR",
                "final_selection_hook": CORRECTED_HOOK,
                "final_title": CORRECTED_TITLE,
                "supported_by": ["final_transcript"],
                "changed_surfaces": [
                    {
                        "artifact": "selection_hook",
                        "before": "已经被命名",
                        "after": "提议把技能叫",
                        "reason": "字幕是提议且主播随后拒绝。",
                        "evidence": [
                            "技能可以叫李姐拉拉吗",
                            "不行，我这个应该叫李姐网",
                        ],
                    },
                    {
                        "artifact": "title",
                        "before": "已经被命名成李姐拉拉",
                        "after": "提议把技能叫李姐拉拉，主播随即拒绝",
                        "reason": "标题同样需要保留提议与拒绝的模态。",
                        "evidence": [
                            "技能可以叫李姐拉拉吗",
                            "不行，我这个应该叫李姐网",
                        ],
                    },
                ],
                "addressee_attribution": [],
                "selection_scorecard_review": {
                    "status": "INCOMPATIBLE",
                    "reason": "旧评分卡基于已经完成命名的错误事实。",
                },
                "summary": "最终字幕支持提议后被拒绝。",
            },
            ensure_ascii=False,
        )

    receipt = review_and_repair_source_facts(
        selection_hook=ORIGINAL_HOOK,
        title=ORIGINAL_TITLE,
        final_transcript=FINAL_TRANSCRIPT,
        clip_context_prompt="",
        selection_scorecard=scorecard,
        llm_call=cpa,
    )
    assert receipt["decision"] == "REPAIR_SCORECARD_STALE"
    return receipt


def _provider_payload() -> str:
    return json.dumps(
        {
            "status": "SUPPORTED",
            "selection_scorecard": {
                "tier": 2,
                "tier_basis": "personal_stance",
                "tier_reason": "弹幕提出命名，主播立即否决并给出自己的命名。",
                "tier_evidence_cues": [1, 2],
                "dimensions": {
                    "lidousha_centrality": 4,
                    "stance_intensity": 3,
                    "audience_salience": 2,
                    "relationship_interaction": 2,
                    "persona_reversal": 3,
                    "comedic_payoff": 3,
                    "self_contained": 4,
                },
                "uncertainty_penalty": 0,
                "fatigue_penalty": 0,
            },
        },
        ensure_ascii=False,
    )


def _fixture(tmp_path: Path) -> dict[str, object]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    reviewed = tmp_path / "reviewed-final.srt"
    reviewed.write_text(
        """1
00:00:00,000 --> 00:00:01,000
技能可以叫李姐拉拉吗

2
00:00:01,100 --> 00:00:02,500
不行，我这个应该叫李姐网
""",
        encoding="utf-8",
    )
    scorecard = tmp_path / "stale-scorecard.json"
    _write_json(scorecard, _stale_scorecard())
    receipt = tmp_path / "stale-source-fact.json"
    _write_json(receipt, _stale_receipt(json.loads(scorecard.read_text(encoding="utf-8"))))
    provider = tmp_path / "provider.json"
    _write_json(
        provider,
        {
            "schema_version": "manual-source-fact-rescore-provider.v1",
            "provider": CPA_PROVIDER,
            "model": CPA_MODEL,
            "llm_config": {
                "transport": CPA_TRANSPORT,
                "command_template": CPA_COMMAND_TEMPLATE,
                "timeout_seconds": CPA_CALLER_TIMEOUT_SECONDS,
            },
        },
    )
    return {
        "candidate_id": CANDIDATE_ID,
        "reviewed_final_srt": reviewed,
        "reviewed_final_srt_sha256": _file_sha(reviewed),
        "corrected_hook": CORRECTED_HOOK,
        "source_fact_receipt": receipt,
        "source_fact_receipt_sha256": _file_sha(receipt),
        "stale_scorecard": scorecard,
        "stale_scorecard_sha256": _file_sha(scorecard),
        "source_start_ms": 10_000,
        "source_end_ms": 13_000,
        "provider_config": provider,
        "provider_config_sha256": _file_sha(provider),
        "output": tmp_path / "rescored.json",
    }


def test_default_is_hash_bound_validation_without_provider_or_write(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    calls = 0

    def provider(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        return _provider_payload()

    preflight = run_manual_rescore(**fixture, llm_call=provider)

    assert preflight["status"] == "VALIDATED_NO_PROVIDER_CALL"
    assert preflight["input_sha256"] == _canonical_sha256(preflight["input_bindings"])
    assert preflight["input_bindings"]["reviewed_final_transcript_sha256"] == (
        "sha256:" + hashlib.sha256(FINAL_TRANSCRIPT.encode("utf-8")).hexdigest()
    )
    assert calls == 0
    assert not fixture["output"].exists()


def test_executes_once_and_writes_create_only_calibrated_receipt(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    calls = 0

    def provider(prompt: str) -> str:
        nonlocal calls
        calls += 1
        assert CORRECTED_HOOK in prompt
        assert "技能可以叫李姐拉拉吗" in prompt
        return _provider_payload()

    receipt = run_manual_rescore(
        **fixture,
        execute_provider_call=True,
        environ={EXECUTION_AUTHORITY_ENV: EXECUTION_AUTHORITY_VALUE},
        llm_call=provider,
    )

    assert calls == 1
    assert receipt["schema_version"] == "source-fact-rescore-scorecard.v1"
    assert receipt["status"] == "RESCORED"
    assert selection_scorecard_is_valid(receipt["selection_scorecard"])
    assert receipt["selection_scorecard_sha256"] == _canonical_sha256(
        receipt["selection_scorecard"]
    )
    unhashed = dict(receipt)
    output_sha = unhashed.pop("output_sha256")
    assert output_sha == _canonical_sha256(unhashed)
    assert json.loads(fixture["output"].read_text(encoding="utf-8")) == receipt


def test_rejects_stale_input_drift_before_provider_call(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    calls = 0
    fixture["source_fact_receipt"].write_text("{}\n", encoding="utf-8")

    def provider(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        return _provider_payload()

    with pytest.raises(ManualScorecardRescoreError, match="bytes drifted"):
        run_manual_rescore(
            **fixture,
            execute_provider_call=True,
            environ={EXECUTION_AUTHORITY_ENV: EXECUTION_AUTHORITY_VALUE},
            llm_call=provider,
        )
    assert calls == 0
    assert not fixture["output"].exists()


def test_rejects_existing_output_before_provider_call(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    fixture["output"].write_text("do not replace\n", encoding="utf-8")
    calls = 0

    def provider(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        return _provider_payload()

    with pytest.raises(ManualScorecardRescoreError, match="create-only"):
        run_manual_rescore(
            **fixture,
            execute_provider_call=True,
            environ={EXECUTION_AUTHORITY_ENV: EXECUTION_AUTHORITY_VALUE},
            llm_call=provider,
        )
    assert calls == 0
    assert fixture["output"].read_text(encoding="utf-8") == "do not replace\n"


def test_rejects_missing_execution_authority_before_provider_call(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    calls = 0

    def provider(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        return _provider_payload()

    with pytest.raises(ManualScorecardRescoreError, match=EXECUTION_AUTHORITY_ENV):
        run_manual_rescore(
            **fixture,
            execute_provider_call=True,
            environ={},
            llm_call=provider,
        )
    assert calls == 0
    assert not fixture["output"].exists()


@pytest.mark.parametrize(
    ("drift", "expected_error"),
    [
        ({"timeout_seconds": 180}, "timeout must be exactly 600s"),
        ({"timeout_seconds": 601}, "timeout must be exactly 600s"),
        ({"transport": "direct"}, "command transport contract"),
        (
            {
                "command_template": (
                    "bash scripts/llm_via_cpa.sh {prompt_file} {completion_file} "
                    "'{model}' max"
                )
            },
            "command template drifted",
        ),
        ({"model": "gpt-5.5"}, "model must be the exact"),
    ],
)
def test_rejects_cpa_provider_contract_drift_before_call(
    tmp_path: Path,
    drift: dict[str, object],
    expected_error: str,
) -> None:
    fixture = _fixture(tmp_path)
    config_path = fixture["provider_config"]
    config = json.loads(config_path.read_text(encoding="utf-8"))
    if "model" in drift:
        config["model"] = drift["model"]
    for key in ("timeout_seconds", "transport", "command_template"):
        if key in drift:
            config["llm_config"][key] = drift[key]
    _write_json(config_path, config)
    # Rebind the mutated file itself so this test reaches the semantic CPA
    # contract guard instead of stopping at the outer file-byte hash guard.
    fixture["provider_config_sha256"] = _file_sha(config_path)
    calls = 0

    def provider(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        return _provider_payload()

    with pytest.raises(ManualScorecardRescoreError, match=expected_error):
        run_manual_rescore(
            **fixture,
            execute_provider_call=True,
            environ={EXECUTION_AUTHORITY_ENV: EXECUTION_AUTHORITY_VALUE},
            llm_call=provider,
        )
    assert calls == 0
    assert not fixture["output"].exists()


def test_rejects_input_drift_during_call_without_writing_receipt(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)

    def provider(_prompt: str) -> str:
        fixture["reviewed_final_srt"].write_text("drifted\n", encoding="utf-8")
        return _provider_payload()

    with pytest.raises(ManualScorecardRescoreError, match="after validation"):
        run_manual_rescore(
            **fixture,
            execute_provider_call=True,
            environ={EXECUTION_AUTHORITY_ENV: EXECUTION_AUTHORITY_VALUE},
            llm_call=provider,
        )
    assert not fixture["output"].exists()


def test_rejects_receipt_srt_or_corrected_hook_mismatch_before_provider(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    calls = 0

    def provider(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        return _provider_payload()

    fixture["corrected_hook"] = "另一个没有绑定的修订 hook"
    with pytest.raises(ManualScorecardRescoreError, match="corrected hook"):
        run_manual_rescore(
            **fixture,
            execute_provider_call=True,
            environ={EXECUTION_AUTHORITY_ENV: EXECUTION_AUTHORITY_VALUE},
            llm_call=provider,
        )
    assert calls == 0

    fixture = _fixture(tmp_path / "transcript")
    reviewed = fixture["reviewed_final_srt"]
    reviewed.write_text(
        reviewed.read_text(encoding="utf-8").replace("不行", "可以"),
        encoding="utf-8",
    )
    fixture["reviewed_final_srt_sha256"] = _file_sha(reviewed)
    with pytest.raises(ManualScorecardRescoreError, match="does not match"):
        run_manual_rescore(
            **fixture,
            execute_provider_call=True,
            environ={EXECUTION_AUTHORITY_ENV: EXECUTION_AUTHORITY_VALUE},
            llm_call=provider,
        )
    assert calls == 0
