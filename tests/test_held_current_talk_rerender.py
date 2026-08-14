"""Focused no-upload transaction tests for the held CURRENT Talk rerender lane."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

import scripts.free_session_autoslice as runner
from src.autoslice import held_current_talk_rerender as held
from src.autoslice import historical_failed_talk_scope
from src.autoslice.final_review_provider_budget_retry import (
    LEDGER_FIELD as FINAL_REVIEW_PROVIDER_BUDGET_LEDGER_FIELD,
    LEDGER_SCHEMA as FINAL_REVIEW_PROVIDER_BUDGET_LEDGER_SCHEMA,
)
from src.autoslice.operator_processing_scope import (
    HELD_CURRENT_RERENDER_GRANT_SCHEMA,
    HELD_CURRENT_RERENDER_INTENT,
    STATE_KEY,
)
from src.autoslice.repository_asset_authority import (
    RepositoryAssetAuthority,
    RepositoryAssetAuthorityError,
)


DATE = "2026-08-07"
CID = "auto_210739_1142_1436"
GRANT_ID = "2026-08-13-rerender-held-current-210739"
OLD_FP = "sha256:" + "1" * 64
NEW_FP = "sha256:" + "2" * 64


def _grant() -> dict:
    return {
        "schema_version": HELD_CURRENT_RERENDER_GRANT_SCHEMA,
        "grant_id": GRANT_ID,
        "recording_date": DATE,
        "reason": "南町专名流水线修复后，只重出这一条 held CURRENT 审片包。",
        "candidate_ids": [CID],
        "user_authorization": {
            "quote": "任何时候优先修复流水线；如果能给我 review 就 review。",
            "timestamp": "2026-08-13T11:00:00Z",
        },
        "expires_at": "2099-08-14T11:00:00Z",
        "intent": HELD_CURRENT_RERENDER_INTENT,
        "upload_allowed": False,
    }


@pytest.fixture()
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    repo = tmp_path / "repo"
    registry = repo / "assets" / "lidousha" / "publication_registry.v1.json"
    registry.parent.mkdir(parents=True)
    registry.write_text(
        json.dumps(
            {
                "schema_version": "publication-registry.v1",
                "authority": "test committed hold",
                "entries": [
                    {
                        "candidate_id": CID,
                        "recording_date": DATE,
                        "status": "hold_pending_review",
                        "note": "no upload until Ivan releases",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    base = tmp_path / "autoslice"
    rec_root = tmp_path / "recordings"
    segment = rec_root / DATE / "22966160_20260807-21-07-39.mp4"
    segment.parent.mkdir(parents=True)
    segment.write_bytes(b"source")
    bcut = base / "cache" / DATE / f"{segment.stem}.bcut.srt"
    bcut.parent.mkdir(parents=True)
    bcut.write_text("1\n00:00:00,000 --> 00:00:01,000\n南町\n", encoding="utf-8")
    chat_binding = {
        "structured_chat_required": True,
        "chat_binding_status": "BOUND_SOURCE_ALIAS",
        "chat_jsonl": str(segment.with_suffix(".jsonl")),
        "chat_jsonl_sha256": "sha256:" + "c" * 64,
        "chat_origin_epoch_ms": 1_786_099_659_000,
        "chat_timeline_offset_ms": 0,
        "chat_binding_authority": "source-alias-ledger.v1",
    }
    monkeypatch.setattr(runner, "REPO_ROOT", repo)
    monkeypatch.setattr(runner, "BASE", base)
    monkeypatch.setattr(runner, "REC_ROOT", rec_root)
    monkeypatch.setattr(runner, "ffprobe_ms", lambda _path: 2_000_000)
    monkeypatch.setattr(runner, "find_danmaku_xml", lambda _path: None)
    monkeypatch.setattr(
        runner,
        "resolve_structured_chat_binding",
        lambda *_a, **_k: dict(chat_binding),
    )
    monkeypatch.setattr(runner, "talk_pipeline_fingerprint", lambda _cid: NEW_FP)
    monkeypatch.setattr(held.publication_registry, "DEFAULT_REGISTRY_PATH", registry)
    monkeypatch.setattr(
        held,
        "require_repository_asset_authority",
        lambda **_k: RepositoryAssetAuthority(
            mode="GIT_HEAD",
            commit="a" * 40,
            relative_path="assets/lidousha/publication_registry.v1.json",
            file_sha256="sha256:" + "b" * 64,
        ),
    )
    current = {
        "candidate_id": CID,
        "status": "review_ready",
        "bundle_lifecycle": "CURRENT",
        "bundle_compliance": "COMPLIANT",
        "pipeline_fingerprint": OLD_FP,
        "segment": segment.name,
        "start_ms": 1_142_160,
        "end_ms": 1_437_390,
        "hook": "想抱团却被拒绝，认南町为大哥",
        "selection_scorecard": None,
        "session_id": "live-20260807T210739+0800",
    }
    state = {
        "run_mode": "PRODUCTION",
        "upload_allowed": False,
        "picks": [current],
        "pending_talk": [],
        "talk_backlog": [{"cid": "auto_unrelated_talk"}],
        "talk_superseded_attempts": [],
        "pending_song": [{"cid": "song_parked"}],
        "song_backlog": [{"cid": "song_backlog_parked"}],
        "song_selection_backlog": [],
        "songs": [],
        STATE_KEY: _grant(),
    }
    return {
        "state": state,
        "registry": registry,
        "segment": segment,
        "bcut": bcut,
    }


def test_transaction_supersedes_current_and_queues_exact_selected_repair(harness):
    state = harness["state"]
    registry_preimage = harness["registry"].read_bytes()
    song_preimage = copy.deepcopy(
        (state["pending_song"], state["song_backlog"], state["songs"])
    )
    inspection = held.inspect_named_held_current_talk_rerender(
        DATE, state, candidate_id=CID, grant_id=GRANT_ID
    )
    assert inspection.outcome == held.OUTSTANDING_CURRENT
    assert inspection.queue_item is not None
    assert "given_title" not in inspection.queue_item
    assert "recovery_publication_authority" not in inspection.queue_item

    assert held.requeue_named_held_current_talk_for_review(
        DATE, state, candidate_ids=[CID], grant_id=GRANT_ID
    ) == 1

    assert state["picks"] == []
    assert [row["cid"] for row in state["pending_talk"]] == [CID]
    queued = state["pending_talk"][0]
    assert queued["selected_repair"] is True
    assert queued["retry_reason"] == held.RETRY_REASON
    assert queued["operator_scope_grant_id"] == GRANT_ID
    archive = state["talk_superseded_attempts"][0]
    assert archive["bundle_lifecycle"] == "SUPERSEDED"
    assert archive["bundle_compliance"] == "STALE_PIPELINE"
    assert archive["superseded_by"] == NEW_FP
    assert harness["registry"].read_bytes() == registry_preimage
    assert (state["pending_song"], state["song_backlog"], state["songs"]) == song_preimage
    assert [row["cid"] for row in state["talk_backlog"]] == ["auto_unrelated_talk"]
    queued_check = held.inspect_named_held_current_talk_rerender(
        DATE, state, candidate_id=CID, grant_id=GRANT_ID
    )
    assert queued_check.outcome == held.OUTSTANDING_QUEUED


def test_transaction_restores_provider_budget_ledger_to_queue_and_archive(
    harness,
):
    state = harness["state"]
    ledger = {
        "schema_version": FINAL_REVIEW_PROVIDER_BUDGET_LEDGER_SCHEMA,
        "entries": [
            {
                "candidate_id": CID,
                "retry_fingerprint": "sha256:" + "7" * 64,
            }
        ],
    }
    state["talk_superseded_attempts"] = [
        {
            "candidate_id": CID,
            FINAL_REVIEW_PROVIDER_BUDGET_LEDGER_FIELD: ledger,
        }
    ]

    inspection = held.inspect_named_held_current_talk_rerender(
        DATE, state, candidate_id=CID, grant_id=GRANT_ID
    )

    assert inspection.outcome == held.OUTSTANDING_CURRENT
    assert inspection.queue_item is not None
    assert inspection.archived_record is not None
    assert inspection.queue_item[FINAL_REVIEW_PROVIDER_BUDGET_LEDGER_FIELD] == ledger
    assert inspection.archived_record[FINAL_REVIEW_PROVIDER_BUDGET_LEDGER_FIELD] == ledger
    assert inspection.queue_item[FINAL_REVIEW_PROVIDER_BUDGET_LEDGER_FIELD] is not ledger
    assert inspection.archived_record[FINAL_REVIEW_PROVIDER_BUDGET_LEDGER_FIELD] is not ledger
    assert (
        inspection.archived_record[FINAL_REVIEW_PROVIDER_BUDGET_LEDGER_FIELD]
        is not inspection.queue_item[FINAL_REVIEW_PROVIDER_BUDGET_LEDGER_FIELD]
    )

    assert held.requeue_named_held_current_talk_for_review(
        DATE, state, candidate_ids=[CID], grant_id=GRANT_ID
    ) == 1
    assert state["pending_talk"][0][FINAL_REVIEW_PROVIDER_BUDGET_LEDGER_FIELD] == ledger
    assert state["talk_superseded_attempts"][-1][
        FINAL_REVIEW_PROVIDER_BUDGET_LEDGER_FIELD
    ] == ledger


def test_malformed_provider_budget_history_blocks_held_current_rerender(harness):
    state = harness["state"]
    state["talk_superseded_attempts"] = [
        {
            "candidate_id": CID,
            FINAL_REVIEW_PROVIDER_BUDGET_LEDGER_FIELD: {
                "schema_version": FINAL_REVIEW_PROVIDER_BUDGET_LEDGER_SCHEMA,
                "entries": [{"candidate_id": CID}],
            },
        }
    ]
    preimage = copy.deepcopy(state)

    inspection = held.inspect_named_held_current_talk_rerender(
        DATE, state, candidate_id=CID, grant_id=GRANT_ID
    )

    assert inspection.outcome == held.BLOCKED
    assert "RECOVERY_RERUN_PROVIDER_BUDGET_LEDGER_INVALID" in inspection.reason_code
    assert state == preimage


@pytest.mark.parametrize("fault", ["missing_bcut", "duplicate_active"])
def test_missing_evidence_or_duplicate_active_row_blocks_without_mutation(
    harness, fault
):
    state = harness["state"]
    if fault == "missing_bcut":
        harness["bcut"].unlink()
    else:
        state["pending_talk"].append(copy.deepcopy(state["picks"][0]))
    preimage = copy.deepcopy(state)

    inspection = held.inspect_named_held_current_talk_rerender(
        DATE, state, candidate_id=CID, grant_id=GRANT_ID
    )

    assert inspection.outcome == held.BLOCKED
    assert state == preimage
    with pytest.raises(held.HeldCurrentTalkRerenderError):
        held.requeue_named_held_current_talk_for_review(
            DATE, state, candidate_ids=[CID], grant_id=GRANT_ID
        )
    assert state == preimage


def test_unsealed_or_non_hold_registry_blocks_without_state_mutation(
    harness, monkeypatch
):
    state = harness["state"]
    preimage = copy.deepcopy(state)
    monkeypatch.setattr(
        held,
        "require_repository_asset_authority",
        lambda **_k: (_ for _ in ()).throw(
            RepositoryAssetAuthorityError("worktree bytes differ from HEAD")
        ),
    )
    inspection = held.inspect_named_held_current_talk_rerender(
        DATE, state, candidate_id=CID, grant_id=GRANT_ID
    )
    assert inspection.outcome == held.BLOCKED
    assert inspection.reason_code.startswith("HELD_CURRENT_REGISTRY_UNSEALED")
    assert state == preimage

    monkeypatch.undo()


def test_unchanged_fingerprint_converges_without_requeue_evidence(harness, monkeypatch):
    state = harness["state"]
    monkeypatch.setattr(runner, "talk_pipeline_fingerprint", lambda _cid: OLD_FP)
    harness["bcut"].unlink()
    preimage = copy.deepcopy(state)

    inspection = held.inspect_named_held_current_talk_rerender(
        DATE, state, candidate_id=CID, grant_id=GRANT_ID
    )

    assert inspection.outcome == held.CONVERGED
    assert inspection.reason_code == "HELD_CURRENT_PIPELINE_ALREADY_CURRENT"
    assert state == preimage


@pytest.mark.parametrize("field", ["given_title", "recovery_publication_authority"])
def test_publication_or_given_title_authority_is_forbidden(harness, field):
    state = harness["state"]
    state["picks"][0][field] = (
        "forbidden title"
        if field == "given_title"
        else {"schema_version": "some-publication-authority"}
    )
    preimage = copy.deepcopy(state)

    inspection = held.inspect_named_held_current_talk_rerender(
        DATE, state, candidate_id=CID, grant_id=GRANT_ID
    )

    assert inspection.outcome == held.BLOCKED
    assert inspection.reason_code == "HELD_CURRENT_PUBLICATION_AUTHORITY_FORBIDDEN"
    assert state == preimage


def test_historical_scope_wiring_requeues_target_and_never_touches_song_queues(
    harness, monkeypatch
):
    state = harness["state"]
    song_preimage = copy.deepcopy(
        (state["pending_song"], state["song_backlog"], state["songs"])
    )
    frozen = historical_failed_talk_scope.freeze(state, date=DATE)
    assert frozen == (CID,)
    monkeypatch.setattr(
        historical_failed_talk_scope,
        "maintain_delivery_recovery_scope",
        lambda *_a, **_k: (0, 0, 0, 0, False),
    )

    result = historical_failed_talk_scope.maintain(
        DATE,
        state,
        automatic_maintenance=True,
        candidate_ids=frozen,
    )

    assert result == (0, 1, 0, 0, False)
    assert [row["cid"] for row in state["pending_talk"]] == [CID]
    assert (state["pending_song"], state["song_backlog"], state["songs"]) == song_preimage
