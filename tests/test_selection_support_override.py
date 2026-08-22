"""Fail-closed canaries for the terminal selection-support-only authority."""

from __future__ import annotations

import json
import os
import hashlib
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.autoslice import semantic_evidence_scorecard_refresh as refresh
from src.autoslice import selection_support_override as override
from scripts import authorize_selection_support_override as authorize_cli
from src.autoslice.historical_fastlane_authority import _sha
from src.autoslice.producer_delivery_transaction import deployment_authority_binding
from src.autoslice.publication_readiness import (
    NEEDS_IVAN_TRUTH,
    READY_TO_PREPARE,
    build_readiness_graph,
)
from src.autoslice.repository_asset_authority import DEPLOYED_AUTHORITY_MANIFEST_SCHEMA, _canonical_sha256
from src.autoslice.runner_state_writeback import state_bytes


DATE = "2026-08-14"
CID = "auto_123036_727_785"
NOW = datetime(2026, 8, 22, 16, 30, tzinfo=timezone.utc)


def _write(path: Path, payload: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    os.chmod(path, mode)


def _identity(path: Path, *, include_hash: bool) -> dict[str, object]:
    info = path.lstat()
    value: dict[str, object] = {
        "path": str(path), "size_bytes": info.st_size, "mtime_ns": info.st_mtime_ns,
        "ctime_ns": info.st_ctime_ns, "device": info.st_dev, "inode": info.st_ino,
        "mode": info.st_mode & 0o777,
    }
    if include_hash:
        value["sha256"] = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    return value


def _terminal(media: Path, bcut: Path, xml: Path) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": "semantic-evidence-scorecard-refresh-receipt.v1",
        "candidate_id": CID,
        "status": "BLOCKED_TERMINAL",
        "reason_code": "REFRESH_HOOK_UNSUPPORTED",
        "attempts": [
            {"attempt_number": number, "outcome": "UNSUPPORTED", "reason_code": "REFRESH_HOOK_UNSUPPORTED"}
            for number in (1, 2, 3)
        ],
        "prompt_excerpt": "no bounded interaction chain",
        "input_provenance": {
            "source_media": _identity(media, include_hash=False),
            "bcut_srt": _identity(bcut, include_hash=True),
            "semantic_chat": {"schema_version": "semantic-recall-chat-evidence.v1", "window_start_ms": 727160, "window_end_ms": 785950, "evidence_sha256": "sha256:" + "b" * 64},
            "semantic_chat_source": _identity(xml, include_hash=True),
        },
    }
    value["receipt_sha256"] = _canonical_sha256(value)
    return value


def _row(media: Path, bcut: Path, xml: Path) -> dict[str, object]:
    return {
        "cid": CID,
        "lane": "semantic_recall",
        "hook": "弹幕刚喊温柔姐姐想睡脚边，李豆沙立刻拒绝并表示要一脚踹开，随后又用冰美式和瑞幸太淡暴露冷酷口味。",
        "start_ms": 1000,
        "end_ms": 30000,
        "segment_path": str(media),
        "bcut_srt_path": str(bcut),
        "xml": str(xml),
        "selection_scorecard": {
            "semantic_recall_chat_evidence": {"schema_version": "semantic-recall-chat-evidence.v1", "window_start_ms": 0, "window_end_ms": 1803150, "evidence_sha256": "sha256:" + "c" * 64},
            "tier": 2,
        },
        "semantic_evidence_scorecard_refresh": _terminal(media, bcut, xml),
    }


def _runtime(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "runtime"
    (root / "state").mkdir(parents=True)
    (root / "repo" / "docs/reviews").mkdir(parents=True)
    _write(root / "repo" / "DEPLOYED_COMMIT", b"1" * 40 + b"\n")
    manifest_body = {
        "schema_version": DEPLOYED_AUTHORITY_MANIFEST_SCHEMA,
        "deployed_commit": "1" * 40,
        "entries": {"assets/x": {"bytes": 1, "sha256": "sha256:" + "2" * 64}},
    }
    manifest = {**manifest_body, "manifest_sha256": _canonical_sha256(manifest_body)}
    _write(root / "repo" / "DEPLOYED_AUTHORITY_MANIFEST.json", json.dumps(manifest).encode())
    ruling = root / "repo" / "docs/reviews/2026-08-19-ivan-review-batch-rulings.md"
    _write(
        ruling,
        "| #7 | auto_123036_727_785 | 修 |\n以上我说的所有内容修复后都可以走快车道上传\n".encode(),
        0o644,
    )
    source = tmp_path / "source"
    media, bcut, xml = source / "live.mp4", source / "clip.srt", source / "chat.xml"
    _write(media, b"media")
    _write(bcut, b"1\n00:00:00,000 --> 00:00:01,000\ntext\n")
    _write(xml, b"<i></i>")
    state = {
        "operator_processing_scope": {
            "schema_version": "operator-processing-scope-grant.v2",
            "grant_id": "scope-7", "recording_date": DATE, "reason": "historical named failed picks",
            "candidate_ids": [CID],
            "user_authorization": {"quote": "Ivan explicitly approved this historical repair", "timestamp": "2026-08-19T00:08:52.249Z"},
            "expires_at": "2026-08-22T20:00:00Z", "intent": "RECOVER_NAMED_FAILED_PICKS",
        },
        "pending_talk": [_row(media, bcut, xml)],
    }
    state_path = root / "state" / f"{DATE}.json"
    _write(state_path, state_bytes(state))
    return root, ruling


def _prepared(root: Path, ruling: Path) -> tuple[Path, dict[str, object]]:
    before = (root / "state" / f"{DATE}.json").read_bytes()
    return override.prepare_selection_support_override(
        runtime_root=root,
        date=DATE,
        candidate_id=CID,
        nonce="selection-support-20260822-r7",
        expires_at="2026-08-22T18:00:00Z",
        expected_state_sha256=_sha(before),
        expected_authority=deployment_authority_binding(root),
        authorization={
            "source_path": str(ruling),
            "source_sha256": _sha(ruling.read_bytes()),
            "event_id": "0df2296b-500a-4681-ab5e-6fb46dc39579",
            "user_id": "555195ed-ec18-418d-a311-558f7e54291f",
            "timestamp": "2026-08-19T00:08:52.249Z",
            "scope": "SELECTION_SUPPORT_ONLY",
        },
        now=NOW,
    )


def test_exact_terminal_is_sealed_then_consumed_without_changing_scorecard_or_verdict(tmp_path: Path):
    root, ruling = _runtime(tmp_path)
    path, document = _prepared(root, ruling)
    before = json.loads((root / "state" / f"{DATE}.json").read_text())
    old_card = deepcopy(before["pending_talk"][0]["selection_scorecard"])
    old_terminal = deepcopy(before["pending_talk"][0]["semantic_evidence_scorecard_refresh"])
    assert old_terminal["input_provenance"]["semantic_chat"] != old_card["semantic_recall_chat_evidence"]
    override.create_or_apply_selection_support_override(path, document, runtime_root=root, now=NOW)
    state = json.loads((root / "state" / f"{DATE}.json").read_text())
    row = state["pending_talk"][0]
    assert path.stat().st_mode & 0o777 == 0o600
    sealed = json.loads(path.read_text())
    assert sealed["status"] == "COMMITTED"
    assert "inode" not in sealed["current_source_binding"]["source_media"]
    assert "inode" not in sealed["current_source_binding"]["semantic_chat_source"]
    assert "inode" in sealed["current_source_binding"]["bcut_srt"]
    assert override.selection_support_override_applies(row, state=state, date=DATE, runtime_root=root, now=NOW)
    consumed = override.consume_in_memory(row, state=state, date=DATE, runtime_root=root, now=NOW)
    assert consumed["status"] == "CONSUMED_SELECTION_SUPPORT_ONLY"
    assert row["selection_scorecard"] == old_card
    assert row["semantic_evidence_scorecard_refresh"] == old_terminal
    assert not override.terminal_selection_support_blocked(row, CID, state=state, date=DATE, runtime_root=root)
    with pytest.raises(override.SelectionSupportOverrideError, match="UNUSABLE"):
        override.consume_in_memory(row, state=state, date=DATE, runtime_root=root, now=NOW)


def test_drifted_terminal_or_source_authorization_refuses_without_state_write(tmp_path: Path):
    root, ruling = _runtime(tmp_path)
    path, document = _prepared(root, ruling)
    state_path = root / "state" / f"{DATE}.json"
    before = state_path.read_bytes()
    state = json.loads(before)
    state["pending_talk"][0]["semantic_evidence_scorecard_refresh"]["attempts"][2]["outcome"] = "REFRESHED"
    _write(state_path, state_bytes(state))
    with pytest.raises(override.SelectionSupportOverrideError, match="(CANDIDATE|STATE)_DRIFT"):
        override.create_or_apply_selection_support_override(path, document, runtime_root=root, now=NOW)
    assert not path.exists()
    root, ruling = _runtime(tmp_path / "source")
    ruling.write_text("forged")
    with pytest.raises(override.SelectionSupportOverrideError, match="AUTHORIZATION_INVALID"):
        _prepared(root, ruling)


def test_tamper_replay_wrong_reason_and_deployed_drift_fail_closed(tmp_path: Path):
    root, ruling = _runtime(tmp_path)
    path, document = _prepared(root, ruling)
    override.create_or_apply_selection_support_override(path, document, runtime_root=root, now=NOW)
    state_path = root / "state" / f"{DATE}.json"
    state_before = state_path.read_bytes()
    broken = json.loads(path.read_text())
    broken["candidate_id"] = "auto_forged"
    _write(path, json.dumps(broken).encode())
    state = json.loads(state_before)
    row = state["pending_talk"][0]
    assert not override.selection_support_override_applies(row, state=state, date=DATE, runtime_root=root, now=NOW)
    with pytest.raises(override.SelectionSupportOverrideError, match="SELECTION_SUPPORT_TERMINAL_BLOCKED"):
        override.historical_preflight(state, date=DATE, candidate_ids=(CID,), runtime_root=root, now=NOW)
    assert state_path.read_bytes() == state_before
    root, ruling = _runtime(tmp_path / "reason")
    state_path = root / "state" / f"{DATE}.json"
    state = json.loads(state_path.read_text())
    receipt = state["pending_talk"][0]["semantic_evidence_scorecard_refresh"]
    receipt["reason_code"] = "REFRESH_CALIBRATION_REJECTED"
    receipt["receipt_sha256"] = _canonical_sha256({key: value for key, value in receipt.items() if key != "receipt_sha256"})
    _write(state_path, state_bytes(state))
    with pytest.raises(override.SelectionSupportOverrideError, match="TERMINAL_INVALID"):
        _prepared(root, ruling)
    root, ruling = _runtime(tmp_path / "deploy")
    with pytest.raises(override.SelectionSupportOverrideError, match="DEPLOYMENT_DRIFT"):
        override.prepare_selection_support_override(
            runtime_root=root, date=DATE, candidate_id=CID, nonce="selection-support-20260822-r7",
            expires_at="2026-08-22T18:00:00Z", expected_state_sha256=_sha((root / "state" / f"{DATE}.json").read_bytes()),
            expected_authority={"commit": "0" * 40}, authorization={
                "source_path": str(ruling), "source_sha256": _sha(ruling.read_bytes()),
                "event_id": "0df2296b-500a-4681-ab5e-6fb46dc39579", "user_id": "555195ed-ec18-418d-a311-558f7e54291f",
                "timestamp": "2026-08-19T00:08:52.249Z", "scope": "SELECTION_SUPPORT_ONLY",
            }, now=NOW,
        )


@pytest.mark.parametrize("role", ("bcut_srt", "semantic_chat_source"))
def test_post_commit_control_source_drift_blocks_consumption(tmp_path: Path, role: str):
    root, ruling = _runtime(tmp_path)
    path, document = _prepared(root, ruling)
    override.create_or_apply_selection_support_override(path, document, runtime_root=root, now=NOW)
    state = json.loads((root / "state" / f"{DATE}.json").read_text())
    source = Path(state["pending_talk"][0]["semantic_evidence_scorecard_refresh"]["input_provenance"][role]["path"])
    source.write_bytes(source.read_bytes() + b" drift")
    row = state["pending_talk"][0]
    assert not override.selection_support_override_applies(row, state=state, date=DATE, runtime_root=root, now=NOW)
    with pytest.raises(override.SelectionSupportOverrideError, match="SELECTION_SUPPORT_TERMINAL_BLOCKED"):
        override.historical_preflight(state, date=DATE, candidate_ids=(CID,), runtime_root=root, now=NOW)


def test_wrong_cid_expiry_and_committed_prepared_ancestry_tamper_fail_closed(tmp_path: Path):
    root, ruling = _runtime(tmp_path)
    with pytest.raises(override.SelectionSupportOverrideError, match="ARGUMENT_INVALID"):
        override.prepare_selection_support_override(
            runtime_root=root, date=DATE, candidate_id="auto_113028_1602_1698",
            nonce="selection-support-20260822-r7", expires_at="2026-08-22T18:00:00Z",
            expected_state_sha256=_sha((root / "state" / f"{DATE}.json").read_bytes()),
            expected_authority=deployment_authority_binding(root), authorization={
                "source_path": str(ruling), "source_sha256": _sha(ruling.read_bytes()),
                "event_id": "0df2296b-500a-4681-ab5e-6fb46dc39579", "user_id": "555195ed-ec18-418d-a311-558f7e54291f",
                "timestamp": "2026-08-19T00:08:52.249Z", "scope": "SELECTION_SUPPORT_ONLY",
            }, now=NOW,
        )
    with pytest.raises(override.SelectionSupportOverrideError, match="EXPIRY_INVALID"):
        override.prepare_selection_support_override(
            runtime_root=root, date=DATE, candidate_id=CID, nonce="selection-support-20260822-r7",
            expires_at="2026-08-22T16:29:00Z", expected_state_sha256=_sha((root / "state" / f"{DATE}.json").read_bytes()),
            expected_authority=deployment_authority_binding(root), authorization={
                "source_path": str(ruling), "source_sha256": _sha(ruling.read_bytes()),
                "event_id": "0df2296b-500a-4681-ab5e-6fb46dc39579", "user_id": "555195ed-ec18-418d-a311-558f7e54291f",
                "timestamp": "2026-08-19T00:08:52.249Z", "scope": "SELECTION_SUPPORT_ONLY",
            }, now=NOW,
        )
    path, document = _prepared(root, ruling)
    override.create_or_apply_selection_support_override(path, document, runtime_root=root, now=NOW)
    committed = json.loads(path.read_text())
    committed["prepared_receipt_sha256"] = "sha256:" + "0" * 64
    committed["receipt_sha256"] = _canonical_sha256({key: value for key, value in committed.items() if key != "receipt_sha256"})
    _write(path, json.dumps(committed).encode())
    with pytest.raises(override.SelectionSupportOverrideError, match="RECEIPT_INVALID"):
        override._safe_document(path)


def test_apply_cli_emits_committed_safe_readback(monkeypatch, capsys, tmp_path: Path):
    path = tmp_path / "receipt.json"
    prepared = {"status": "PREPARED"}
    committed = {"status": "COMMITTED", "receipt_sha256": "sha256:" + "1" * 64}
    monkeypatch.setattr(authorize_cli, "deployment_authority_binding", lambda _root: {"commit": "c", "authority_manifest_sha256": "m"})
    monkeypatch.setattr(authorize_cli, "prepare_selection_support_override", lambda **_kwargs: (path, prepared))
    monkeypatch.setattr(authorize_cli, "create_or_apply_selection_support_override", lambda *_args, **_kwargs: path)
    monkeypatch.setattr(authorize_cli, "_safe_document", lambda _path: committed)
    assert authorize_cli.main([
        "--runtime-root", str(tmp_path), "--date", DATE, "--candidate-id", CID,
        "--nonce", "selection-support-20260822-r7", "--expires-at", "2099-01-01T00:00:00Z",
        "--expected-state-sha256", "sha256:x", "--expected-deployed-commit", "c",
        "--expected-authority-manifest-sha256", "m", "--authorization-source-path", "/x",
        "--authorization-source-sha256", "sha256:y", "--authorization-event-id", "e",
        "--authorization-user-id", "u", "--authorization-timestamp", "t", "--apply",
    ]) == 0
    assert json.loads(capsys.readouterr().out)["authority"] == committed


def test_semantic_refresh_consumes_after_settlement_and_preserves_terminal_receipt(tmp_path: Path):
    root, ruling = _runtime(tmp_path)
    path, document = _prepared(root, ruling)
    override.create_or_apply_selection_support_override(path, document, runtime_root=root, now=NOW)
    state = json.loads((root / "state" / f"{DATE}.json").read_text())
    terminal = deepcopy(state["pending_talk"][0]["semantic_evidence_scorecard_refresh"])
    assert refresh.refresh_operator_scoped_chat_scorecards(DATE, state, llm_call=lambda _prompt: pytest.fail("provider called"), now=NOW, runtime_root=root) == 0
    row = state["pending_talk"][0]
    assert row["semantic_evidence_scorecard_refresh"] == terminal
    assert state[override.STATE_KEY][CID]["status"] == "CONSUMED"
    assert state["selection_support_override_consumption_run"]["status"] == "COMPLETE_SELECTION_SUPPORT_ONLY"


def test_staged_override_is_not_consumed_when_another_refresh_remains_blocked(tmp_path: Path):
    root, ruling = _runtime(tmp_path)
    path, document = _prepared(root, ruling)
    override.create_or_apply_selection_support_override(path, document, runtime_root=root, now=NOW)
    state = json.loads((root / "state" / f"{DATE}.json").read_text())
    other = deepcopy(state["pending_talk"][0])
    other["cid"] = "auto_113028_1602_1698"
    other["segment_path"] = "/missing/media.mp4"
    other.pop("semantic_evidence_scorecard_refresh")
    state["pending_talk"].append(other)
    state["operator_processing_scope"]["candidate_ids"] = [CID, other["cid"]]
    assert refresh.refresh_operator_scoped_chat_scorecards(DATE, state, llm_call=lambda _prompt: pytest.fail("provider called"), now=NOW, runtime_root=root) == -1
    assert state[override.STATE_KEY][CID]["status"] == "STAGED"


def test_consumed_marker_with_source_drift_becomes_terminal_again(tmp_path: Path):
    root, ruling = _runtime(tmp_path)
    path, document = _prepared(root, ruling)
    override.create_or_apply_selection_support_override(path, document, runtime_root=root, now=NOW)
    state = json.loads((root / "state" / f"{DATE}.json").read_text())
    row = state["pending_talk"][0]
    override.consume_in_memory(row, state=state, date=DATE, runtime_root=root, now=NOW)
    xml = Path(row["semantic_evidence_scorecard_refresh"]["input_provenance"]["semantic_chat_source"]["path"])
    xml.write_bytes(b"drift")
    assert override.terminal_selection_support_blocked(row, CID, state=state, date=DATE, runtime_root=root)


@pytest.mark.parametrize("field, value", (("hook", "drifted hook"), ("end_ms", 30001)))
def test_consumed_marker_with_candidate_binding_drift_becomes_terminal_again(tmp_path: Path, field: str, value: object):
    root, ruling = _runtime(tmp_path)
    path, document = _prepared(root, ruling)
    override.create_or_apply_selection_support_override(path, document, runtime_root=root, now=NOW)
    state = json.loads((root / "state" / f"{DATE}.json").read_text())
    row = state["pending_talk"][0]
    override.consume_in_memory(row, state=state, date=DATE, runtime_root=root, now=NOW)
    row[field] = value
    assert override.terminal_selection_support_blocked(row, CID, state=state, date=DATE, runtime_root=root)


def test_consumed_marker_or_private_namespace_drift_becomes_terminal_again(tmp_path: Path):
    root, ruling = _runtime(tmp_path)
    path, document = _prepared(root, ruling)
    override.create_or_apply_selection_support_override(path, document, runtime_root=root, now=NOW)
    state = json.loads((root / "state" / f"{DATE}.json").read_text())
    row = state["pending_talk"][0]
    override.consume_in_memory(row, state=state, date=DATE, runtime_root=root, now=NOW)
    state[override.STATE_KEY][CID]["schema_version"] = "forged"
    assert override.terminal_selection_support_blocked(row, CID, state=state, date=DATE, runtime_root=root)
    state[override.STATE_KEY][CID]["schema_version"] = override.SCHEMA
    os.chmod(root / override.NAMESPACE, 0o755)
    assert override.terminal_selection_support_blocked(row, CID, state=state, date=DATE, runtime_root=root)


@pytest.mark.parametrize("mutation", ("receipt_sha256", "status", "attempt"))
def test_malformed_terminal_like_receipt_remains_a_prestart_truth_block(tmp_path: Path, mutation: str):
    root, ruling = _runtime(tmp_path)
    path, document = _prepared(root, ruling)
    override.create_or_apply_selection_support_override(path, document, runtime_root=root, now=NOW)
    state = json.loads((root / "state" / f"{DATE}.json").read_text())
    row = state["pending_talk"][0]
    receipt = row["semantic_evidence_scorecard_refresh"]
    if mutation == "receipt_sha256":
        receipt["receipt_sha256"] = "sha256:" + "0" * 64
    elif mutation == "status":
        receipt["status"] = "BLOCKED_TERMINAL"
        receipt["reason_code"] = "WRONG_REASON"
        receipt["receipt_sha256"] = _canonical_sha256({key: value for key, value in receipt.items() if key != "receipt_sha256"})
    else:
        receipt["attempts"][2]["outcome"] = "REFRESHED"
        receipt["receipt_sha256"] = _canonical_sha256({key: value for key, value in receipt.items() if key != "receipt_sha256"})
    assert override.terminal_selection_support_blocked(row, CID, state=state, date=DATE, runtime_root=root)
    with pytest.raises(override.SelectionSupportOverrideError, match="SELECTION_SUPPORT_TERMINAL_BLOCKED"):
        override.historical_preflight(state, date=DATE, candidate_ids=(CID,), runtime_root=root, now=NOW)


def test_raw_malformed_terminal_without_marker_is_still_blocked(tmp_path: Path):
    root, _ruling = _runtime(tmp_path)
    state = json.loads((root / "state" / f"{DATE}.json").read_text())
    row = state["pending_talk"][0]
    row["semantic_evidence_scorecard_refresh"]["receipt_sha256"] = "sha256:" + "0" * 64
    assert override.terminal_selection_support_blocked(row, CID, state=state, date=DATE, runtime_root=root)


def test_preflight_and_readiness_surface_terminal_truth_before_cover_provider(tmp_path: Path):
    root, ruling = _runtime(tmp_path)
    state = json.loads((root / "state" / f"{DATE}.json").read_text())
    with pytest.raises(override.SelectionSupportOverrideError, match="SELECTION_SUPPORT_TERMINAL_BLOCKED"):
        override.historical_preflight(state, date=DATE, candidate_ids=(CID,), runtime_root=root, now=NOW)
    path, document = _prepared(root, ruling)
    override.create_or_apply_selection_support_override(path, document, runtime_root=root, now=NOW)
    state = json.loads((root / "state" / f"{DATE}.json").read_text())
    override.historical_preflight(state, date=DATE, candidate_ids=(CID,), runtime_root=root, now=NOW)


def test_readiness_does_not_hide_terminal_selection_support_behind_cover_qc(tmp_path: Path):
    root, ruling = _runtime(tmp_path)
    state_path = root / "state" / f"{DATE}.json"
    state = json.loads(state_path.read_text())
    ready = deepcopy(state["pending_talk"][0])
    ready["cid"] = "auto_113028_1602_1698"
    ready.pop("semantic_evidence_scorecard_refresh")
    state["pending_talk"].append(ready)
    _write(state_path, state_bytes(state))
    graph = build_readiness_graph(
        repository_root=root / "repo",
        runtime_root=root,
        registry_loader=lambda *_args, **_kwargs: {"entries": []},
        ledger_reader=lambda _path: ([], []),
        ledger_checker=lambda *_args: (None, None, []),
    )
    row = next(item for item in graph["rows"] if item["candidate_id"] == CID)
    assert row["category"] == NEEDS_IVAN_TRUTH
    assert "SELECTION_SUPPORT_TERMINAL_BLOCKED" in row["reason_codes"]
    sibling = next(item for item in graph["rows"] if item["candidate_id"] == "auto_113028_1602_1698")
    assert sibling["category"] == READY_TO_PREPARE
    path, document = _prepared(root, ruling)
    override.create_or_apply_selection_support_override(path, document, runtime_root=root, now=NOW)
    graph = build_readiness_graph(
        repository_root=root / "repo",
        runtime_root=root,
        registry_loader=lambda *_args, **_kwargs: {"entries": []},
        ledger_reader=lambda _path: ([], []),
        ledger_checker=lambda *_args: (None, None, []),
    )
    row = next(item for item in graph["rows"] if item["candidate_id"] == CID)
    assert row["category"] == READY_TO_PREPARE
    assert "SELECTION_SUPPORT_TERMINAL_BLOCKED" not in row["reason_codes"]
