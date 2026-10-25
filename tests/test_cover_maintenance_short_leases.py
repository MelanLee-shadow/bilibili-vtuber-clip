"""D driver orchestration; exact C CAS/journal replay is an integration probe."""
from copy import deepcopy
from pathlib import Path

import pytest

import scripts.session_autoslice as runner
from src.autoslice import cover_maintenance
from src.autoslice.runner_state_writeback import RunnerStateWritebackError


@pytest.mark.parametrize("outcome", ["success", "stale_candidate", "source_drift", "operator_stop"])
def test_charge_is_persisted_before_unlocked_provider_and_independent_tail_runs(tmp_path, monkeypatch, outcome):
    monkeypatch.setattr(runner, "BASE", tmp_path / "runtime")
    monkeypatch.setattr(runner, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(runner, "pipeline_fingerprint", lambda: "sha256:" + "a" * 64)
    monkeypatch.setattr(runner, "cover_ref_for", lambda *_: None)
    monkeypatch.setattr(runner, "profile_tool", lambda *_: tmp_path / "tool.py")
    monkeypatch.setattr(runner, "child_env_for_date", lambda *_: {})
    monkeypatch.setattr(runner, "log", lambda *_: None)
    stop = [False]
    monkeypatch.setattr(runner, "producer_stop_requested", lambda: stop[0])
    monkeypatch.setattr(cover_maintenance, "cover_maintenance_block_reason", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_roll_forward_prepared_cover_transactions", lambda *_: False)
    monkeypatch.setattr(runner, "_recover_committed_cover_binding", lambda *_: False)
    monkeypatch.setattr(runner, "_refresh_cover_repair_budget", lambda *_: None)
    monkeypatch.setattr(runner, "cover_repair_needed", lambda _date, rec: not rec.get("bound"))
    monkeypatch.setattr(runner, "_cover_authority_preflight", lambda *_: None)
    monkeypatch.setattr(runner, "write_state", lambda *_: pytest.fail("short path wrote state outside its callback"))
    (runner.BASE / "logs").mkdir(parents=True)
    paths = {}
    records = []
    for candidate in ("first", "second"):
        media = tmp_path / f"{candidate}.mp4"
        media.write_bytes(b"media")
        paths[candidate] = media, tmp_path / f"{candidate}.cover.png"
        records.append({"candidate_id": candidate, "title": candidate, "status": "review_ready", "cover_status": "BLOCKED_AI_COVER_REQUIRED"})
    state = {"picks": records, "songs": []}
    monkeypatch.setattr(runner, "delivered_paths", lambda _date, rec: paths[rec["candidate_id"]])
    locked = [False]
    persisted = {}
    calls, binds = [], []

    def transition(*, candidate_id, expected_record, transition):
        live = next(row for row in state["picks"] if row["candidate_id"] == candidate_id)
        assert live == expected_record
        if outcome == "stale_candidate" and candidate_id == "first" and candidate_id in persisted:
            raise RunnerStateWritebackError("COVER_STATE_TRANSITION_CANDIDATE_DRIFT")
        locked[0] = True
        try:
            transition(live)
            persisted[candidate_id] = deepcopy(live)
            return deepcopy(live)
        finally:
            locked[0] = False

    def provider(command, **kwargs):
        assert not locked[0]
        candidate = command[command.index("--candidate-id") + 1]
        assert persisted[candidate]["cover_repair_attempts"] == 1
        assert persisted[candidate]["cover_repair_lifetime_attempts"] == 1
        calls.append(candidate)
        output = Path(command[command.index("--out") + 1])
        output.write_bytes(b"generated")
        if outcome == "source_drift" and candidate == "first":
            paths[candidate][0].write_bytes(b"new source")
        if outcome == "operator_stop" and candidate == "first":
            stop[0] = True
        return type("Completed", (), {"returncode": 0})()

    def bind(_date, rec, _media, _cover, _generated):
        assert locked[0]
        binds.append(rec["candidate_id"])
        rec.update(bound=True, cover_status="REPAIRED_AI_COVER")

    monkeypatch.setattr(cover_maintenance.subprocess, "run", provider)
    monkeypatch.setattr(runner, "_bind_repaired_cover", bind)
    cover_maintenance.repair_covers("2026-10-13", state, state_transition=transition)
    if outcome == "operator_stop":
        assert calls == ["first"]
        assert binds == ["first"]
        assert "cover_repair_attempts" not in state["picks"][1]
        assert "bound" not in state["picks"][1]
        assert state["picks"][0]["bound"] is True
        return
    assert calls == ["first", "second"]
    assert binds == (["first", "second"] if outcome == "success" else ["second"])
    assert state["picks"][1]["bound"] is True
    assert all(row["cover_repair_attempts"] == 1 for row in state["picks"])
