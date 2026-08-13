from __future__ import annotations

import copy
import html
import json
from pathlib import Path

import pytest

import scripts.free_session_autoslice as runner
from src.autoslice.operator_processing_scope import (
    FAILED_PICK_RECOVERY_GRANT_SCHEMA,
    FAILED_PICK_RECOVERY_INTENT,
)
from src.autoslice.selection_scorecard import (
    normalize_selection_scorecard,
    selection_scorecard_is_valid,
)
from src.autoslice import semantic_evidence_scorecard_refresh as refresh


KNOWN_LOW_CANDIDATES = (
    "auto_221234_1349_1418",
    "auto_210624_656_909",
)


def _srt_block(index: int, start_seconds: int, text: str) -> str:
    start_minute, start_second = divmod(start_seconds, 60)
    end_minute, end_second = divmod(start_seconds + 2, 60)
    return (
        f"{index}\n"
        f"00:{start_minute:02d}:{start_second:02d},000 --> "
        f"00:{end_minute:02d}:{end_second:02d},000\n{text}\n"
    )


def _xml(entries: list[tuple[float, str]]) -> str:
    body = "\n".join(
        f'<d p="{offset:.3f},1,25,16777215,0,0,0,0">{html.escape(text)}</d>'
        for offset, text in entries
    )
    return f"<?xml version='1.0' encoding='utf-8'?><i>{body}</i>"


def _old_scorecard() -> dict[str, object]:
    card = normalize_selection_scorecard(
        {
            "tier": 2,
            "tier_basis": "personal_stance",
            "tier_reason": "旧评分未见动作请求弹幕",
            "tier_evidence_cues": [1, 2],
            "dimensions": {
                "lidousha_centrality": 4,
                "stance_intensity": 1,
                "audience_salience": 1,
                "relationship_interaction": 1,
                "persona_reversal": 1,
                "comedic_payoff": 2,
                "self_contained": 3,
            },
            "uncertainty_penalty": 8,
            "fatigue_penalty": 0,
        },
        start_cue=1,
        end_cue=2,
    )
    assert card is not None
    card["semantic_recall_chat_evidence"] = {
        "schema_version": "semantic-recall-chat-evidence.v1",
        "algorithm_id": "old-burst-samples.v0",
        "policy_sha256": "sha256:" + "0" * 64,
    }
    return card


def _provider_scorecard() -> dict[str, object]:
    return {
        "tier": 1,
        "tier_basis": "audience_driven_performance",
        "tier_reason": "观众动作请求、主播照做和后续可爱反馈形成完整互动链",
        "tier_evidence_cues": [1, 2],
        "dimensions": {
            "lidousha_centrality": 4,
            "stance_intensity": 2,
            "audience_salience": 4,
            "relationship_interaction": 4,
            "persona_reversal": 4,
            "comedic_payoff": 4,
            "self_contained": 4,
        },
        "uncertainty_penalty": 0,
        "fatigue_penalty": 0,
    }


def _grant(candidate_ids: list[str], *, schema: str = FAILED_PICK_RECOVERY_GRANT_SCHEMA) -> dict:
    grant = {
        "schema_version": schema,
        "grant_id": "ivan-20260813-refresh-known-89",
        "recording_date": "2026-08-09",
        "reason": "重评已存在的八月九日低分候选聊天证据",
        "candidate_ids": candidate_ids,
        "user_authorization": {
            "quote": "89的应该是单人直播，为什么分数都这么低呢？",
            "timestamp": "2026-08-13T15:00:00Z",
        },
        "expires_at": "2099-08-14T00:00:00Z",
    }
    if schema == FAILED_PICK_RECOVERY_GRANT_SCHEMA:
        grant["intent"] = FAILED_PICK_RECOVERY_INTENT
    return grant


def _candidate(
    tmp_path: Path,
    *,
    candidate_id: str,
    segment_tag: str,
    start_ms: int,
    end_ms: int,
    hook: str,
    request: str,
) -> dict[str, object]:
    segment = tmp_path / f"22966160_{segment_tag}.mp4"
    segment.write_bytes((candidate_id + " source").encode())
    srt = tmp_path / f"{segment.stem}.bcut.srt"
    first_second = start_ms // 1000 + 1
    second_second = min(end_ms // 1000 - 3, first_second + 20)
    srt.write_text(
        _srt_block(1, first_second, "她看到动作要求，问这样可以吗")
        + "\n"
        + _srt_block(2, second_second, "她照做之后笑着说腿要抽筋了"),
        encoding="utf-8",
    )
    xml = tmp_path / f"{segment.stem}.xml"
    xml.write_text(
        _xml(
            [
                (first_second - 2.0, request),
                (second_second + 1.0, "可爱捏"),
                (second_second + 2.0, "可爱捏"),
            ]
        ),
        encoding="utf-8",
    )
    return {
        "cid": candidate_id,
        "lane": "semantic_recall_sharded",
        "hook": hook,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "segment_path": str(segment),
        "bcut_srt_path": str(srt),
        "xml": str(xml),
        "selection_scorecard": _old_scorecard(),
        "session_id": "session-89",
    }


def _two_candidate_state(tmp_path: Path) -> dict:
    first = _candidate(
        tmp_path,
        candidate_id=KNOWN_LOW_CANDIDATES[0],
        segment_tag="20260809-22-12-34",
        start_ms=1_349_000,
        end_ms=1_418_000,
        hook="观众要求她正面趴地晃腿撒娇，她现场照做",
        request="正面趴着地上双手撑脸，然后小腿乱晃这种动作可以吗？",
    )
    second = _candidate(
        tmp_path,
        candidate_id=KNOWN_LOW_CANDIDATES[1],
        segment_tag="20260809-21-06-24",
        start_ms=656_000,
        end_ms=909_000,
        hook="挑战唱百首时被要求趴地，她照做后抽筋",
        request="能不能趴在地上双手撑着脸，小腿晃一晃？",
    )
    return {
        "operator_processing_scope": _grant(list(KNOWN_LOW_CANDIDATES)),
        "segments_done": ["22966160_20260809-22-12-34", "22966160_20260809-21-06-24"],
        "pending_talk": [],
        "talk_backlog": [first, second],
        "picks": [],
        "pending_song": [],
        "songs": [],
    }


def _good_llm(prompts: list[str]):
    def call(prompt: str) -> str:
        prompts.append(prompt)
        return json.dumps(
            {"status": "SUPPORTED", "selection_scorecard": _provider_scorecard()},
            ensure_ascii=False,
        )

    return call


def test_refreshes_two_known_20260809_candidates_without_rediscovery(tmp_path: Path) -> None:
    state = _two_candidate_state(tmp_path)
    original_done = list(state["segments_done"])
    original_bindings = {
        row["cid"]: (row["hook"], row["start_ms"], row["end_ms"]) for row in state["talk_backlog"]
    }
    prompts: list[str] = []

    result = refresh.refresh_operator_scoped_chat_scorecards(
        "2026-08-09", state, llm_call=_good_llm(prompts)
    )

    assert result == 2
    assert len(prompts) == 2
    assert state["segments_done"] == original_done
    assert state["pending_talk"] == []
    assert state["picks"] == []
    for row in state["talk_backlog"]:
        candidate_id = row["cid"]
        assert (row["hook"], row["start_ms"], row["end_ms"]) == original_bindings[candidate_id]
        assert selection_scorecard_is_valid(row["selection_scorecard"])
        provenance = row["selection_scorecard"]["semantic_recall_chat_evidence"]
        assert provenance["policy_sha256"] == refresh.SEMANTIC_CHAT_POLICY_SHA256
        assert provenance["source_sha256"].startswith("sha256:")
        assert provenance["evidence_sha256"].startswith("sha256:")
        receipt = row[refresh.ROW_RECEIPT_KEY]
        assert receipt["status"] == "REFRESHED"
        assert receipt["old_scorecard_sha256"] != receipt["new_scorecard_sha256"]
        assert receipt["provider_contract_sha256"] == refresh.PROVIDER_CONTRACT_SHA256
        assert receipt["scope_grant_sha256"].startswith("sha256:")
    assert state[refresh.REFRESH_STATE_KEY]["status"] == "COMPLETE"
    assert (
        state[refresh.REFRESH_STATE_KEY]["missing_never_recalled_candidates_outside_scope"] is True
    )
    assert all("固定边界" in prompt and "固定 hook" in prompt for prompt in prompts)


def test_current_refresh_is_idempotent_and_never_calls_provider_again(tmp_path: Path) -> None:
    state = _two_candidate_state(tmp_path)
    assert (
        refresh.refresh_operator_scoped_chat_scorecards("2026-08-09", state, llm_call=_good_llm([]))
        == 2
    )
    snapshot = copy.deepcopy(state["talk_backlog"])

    def forbidden(_prompt: str) -> str:
        raise AssertionError("current provenance must not call provider")

    assert (
        refresh.refresh_operator_scoped_chat_scorecards("2026-08-09", state, llm_call=forbidden)
        == 0
    )
    assert state["talk_backlog"] == snapshot
    assert state[refresh.REFRESH_STATE_KEY]["current_candidate_ids"] == sorted(KNOWN_LOW_CANDIDATES)


def test_provider_failure_parks_old_card_and_exhausts_per_input_retry(tmp_path: Path) -> None:
    state = _two_candidate_state(tmp_path)
    state["operator_processing_scope"] = _grant([KNOWN_LOW_CANDIDATES[0]])
    state["talk_backlog"] = state["talk_backlog"][:1]
    old_card = copy.deepcopy(state["talk_backlog"][0]["selection_scorecard"])
    calls = 0

    def unavailable(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        raise LlmCallErrorForTest

    class LlmCallErrorForTest(RuntimeError):
        pass

    for expected_status in ("RETRY_WAIT", "RETRY_WAIT", "BLOCKED_RETRY_EXHAUSTED"):
        assert (
            refresh.refresh_operator_scoped_chat_scorecards(
                "2026-08-09", state, llm_call=unavailable
            )
            == -1
        )
        row = state["talk_backlog"][0]
        assert row["selection_scorecard"] == old_card
        assert row[refresh.ROW_RECEIPT_KEY]["status"] == expected_status
    assert state["status"] == "semantic_chat_scorecard_refresh_blocked"
    assert (
        refresh.refresh_operator_scoped_chat_scorecards("2026-08-09", state, llm_call=unavailable)
        == -1
    )
    assert calls == refresh.MAX_ATTEMPTS_PER_INPUT


def test_xml_drift_during_provider_call_fails_closed_without_card_mutation(tmp_path: Path) -> None:
    state = _two_candidate_state(tmp_path)
    state["operator_processing_scope"] = _grant([KNOWN_LOW_CANDIDATES[0]])
    state["talk_backlog"] = state["talk_backlog"][:1]
    row = state["talk_backlog"][0]
    old_card = copy.deepcopy(row["selection_scorecard"])
    xml = Path(str(row["xml"]))

    def drifting(_prompt: str) -> str:
        xml.write_text(_xml([(1_350.0, "源已经漂移，可以重新做吗？")]), encoding="utf-8")
        return json.dumps(
            {"status": "SUPPORTED", "selection_scorecard": _provider_scorecard()},
            ensure_ascii=False,
        )

    assert (
        refresh.refresh_operator_scoped_chat_scorecards("2026-08-09", state, llm_call=drifting)
        == -1
    )
    assert row["selection_scorecard"] == old_card
    assert row[refresh.ROW_RECEIPT_KEY]["reason_code"] == "REFRESH_INPUT_BINDING_DRIFT"
    assert state["talk_backlog"] == [row]


def test_missing_chat_source_blocks_before_provider_and_before_production(tmp_path: Path) -> None:
    state = _two_candidate_state(tmp_path)
    state["operator_processing_scope"] = _grant([KNOWN_LOW_CANDIDATES[0]])
    state["talk_backlog"] = state["talk_backlog"][:1]
    Path(str(state["talk_backlog"][0]["xml"])).unlink()

    assert refresh.operator_scoped_chat_refresh_needed("2026-08-09", state) is True
    assert (
        refresh.refresh_operator_scoped_chat_scorecards(
            "2026-08-09",
            state,
            llm_call=lambda _prompt: pytest.fail("missing source must block before provider"),
        )
        == -1
    )
    receipt = state["talk_backlog"][0][refresh.ROW_RECEIPT_KEY]
    assert receipt["reason_code"] == "CHAT_SOURCE_UNAVAILABLE"
    assert receipt["status"] == "RETRY_WAIT"


def test_failed_pick_is_never_rewritten_in_place(tmp_path: Path) -> None:
    queued_state = _two_candidate_state(tmp_path)
    failed = queued_state["talk_backlog"][0]
    failed.update({"status": "failed", "failure_recoverable": True})
    state = {
        "operator_processing_scope": _grant([KNOWN_LOW_CANDIDATES[0]]),
        "pending_talk": [],
        "talk_backlog": [],
        "picks": [failed],
    }
    before = copy.deepcopy(failed)

    assert refresh.operator_scoped_chat_refresh_needed("2026-08-09", state) is False
    assert (
        refresh.refresh_operator_scoped_chat_scorecards(
            "2026-08-09",
            state,
            llm_call=lambda _prompt: pytest.fail("picks are outside this lane"),
        )
        == 0
    )
    assert state["picks"] == [before]
    assert refresh.ROW_RECEIPT_KEY not in state["picks"][0]


def test_v1_or_current_date_scope_cannot_enter_historical_refresh(tmp_path: Path) -> None:
    state = _two_candidate_state(tmp_path)
    state["operator_processing_scope"] = _grant(
        list(KNOWN_LOW_CANDIDATES), schema="operator-processing-scope-grant.v1"
    )
    before = copy.deepcopy(state)

    assert (
        refresh.refresh_operator_scoped_chat_scorecards(
            "2026-08-09",
            state,
            llm_call=lambda _prompt: pytest.fail("v1 does not authorize this lane"),
        )
        == 0
    )
    assert state == before

    current = _two_candidate_state(tmp_path)
    current["operator_processing_scope"]["recording_date"] = "2099-08-14"
    assert (
        refresh.refresh_operator_scoped_chat_scorecards(
            "2099-08-14",
            current,
            llm_call=lambda _prompt: pytest.fail("non-historical date is outside scope"),
        )
        == 0
    )


def test_quota_full_backlog_still_sets_runner_work_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _two_candidate_state(tmp_path)
    state["operator_processing_scope"] = _grant([KNOWN_LOW_CANDIDATES[0]])
    state["talk_backlog"] = state["talk_backlog"][:1]
    monkeypatch.setattr(runner, "list_segments", lambda _date: [])
    monkeypatch.setattr(runner, "backlog_has_eligible_session_work", lambda _state: False)
    monkeypatch.setattr(runner, "cover_repair_needed", lambda _date, _row: False)

    has_new, has_pending, needs_cover = refresh.runner_date_work_flags(
        "2026-08-09", state, automatic_maintenance=True
    )

    assert has_new is False
    assert has_pending is True
    assert needs_cover is False
