"""Daily song grants through ordinary selection, reservations, recovery and reports."""

import ast
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from scripts import session_autoslice as runner
from src.autoslice import candidate_selection, reporting, talk_quota_authority as authority


def _candidate(date, index, *, session_hour=21):
    return {
        "cid": f"song_{index}",
        "segment_path": f"/recordings/{date}/22966160_{date.replace('-', '')}-{session_hour:02d}-00-00.mp4",
        "session_id": f"live-{date.replace('-', '')}T{session_hour:02d}0000",
        "anchor_start_ms": index * 60_000,
        "anchor_end_ms": (index + 1) * 60_000,
        "danmaku": 100 - index,
    }


@pytest.mark.parametrize("date,cap", [("2026-10-21", 2), ("2026-10-20", 1), ("2026-10-22", 1)])
def test_daily_reservations_and_third_song_rejection(date, cap):
    candidates = [_candidate(date, index, session_hour=21 + index) for index in range(3)]
    state = {"pending_song": deepcopy(candidates), "song_backlog": [], "songs": []}
    assert runner.song_delivery_budget(state) == cap
    runner.refill_songs(state)
    assert [row["cid"] for row in state["pending_song"]] == [row["cid"] for row in candidates[:cap]]
    for index in range(cap):
        reserved = state["pending_song"].pop(0)
        state["songs"].append({**reserved, "status": "blocked", "verified_delivery_pending_commit": True})
        # The next broadcast shares the same daily reservations.
        assert runner.song_delivery_budget(state, candidates[-1]["session_id"]) == cap - index - 1
    runner.refill_songs(state)
    assert state["pending_song"] == []
    assert [row["cid"] for row in state["song_backlog"]] == [row["cid"] for row in candidates[cap:]]
    assert runner.song_delivery_budget(state) == 0


@pytest.mark.parametrize("representation", ["inventory", "inventory_midnight", "segments", "pending", "record", "unknown_start"])
def test_date_comes_from_existing_discovery_and_persisted_state(tmp_path, representation):
    row = _candidate("2026-10-21", 0)
    if representation == "inventory":
        state = {"source_integrity": {"date_dir": "/recordings/2026-10-21"}}
    elif representation == "inventory_midnight":
        state = {
            "source_integrity": {"date_dir": "/recordings/2026-10-21"},
            "pending_song": [_candidate("2026-10-22", 0)],
        }
    elif representation == "segments":
        # A stream starting before midnight still uses the recording segment's date.
        state = {"segment_sessions": {Path(row["segment_path"]).stem: "live-20261020T230000"}}
    elif representation == "pending":
        state = {"pending_song": [row]}
    elif representation == "unknown_start":
        state = {"pending_song": [{"session_id": "live-20261021Tunknown"}]}
    else:
        state = {"songs": [{**row, "status": "blocked"}]}
    path = tmp_path / "state.json"
    path.write_text(json.dumps(state), encoding="utf-8")
    assert runner.song_delivery_budget(json.loads(path.read_text(encoding="utf-8"))) == 2


def test_ambiguous_or_missing_date_and_invalid_authority_keep_default(tmp_path, monkeypatch):
    mixed = {"pending_song": [_candidate("2026-10-21", 0), _candidate("2026-10-22", 1)]}
    assert runner.song_delivery_budget(mixed, "live-20261021T210000") == 1
    assert runner.song_delivery_budget({}) == 1
    state = {"pending_song": [_candidate("2026-10-21", 0)]}
    monkeypatch.setattr(authority, "DEFAULT_AUTHORITY_PATH", tmp_path / "absent.json")
    assert runner.song_delivery_budget(state) == 1
    document = authority.load_talk_quota_policy_authority(Path(__file__).resolve().parents[1] / "assets/lidousha/talk_quota_policy_authority.v1.json")
    duplicate = deepcopy(document["entries"][-1])
    duplicate["entry_id"] += "-duplicate"
    document["entries"].append(duplicate)
    path = tmp_path / "duplicate.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    monkeypatch.setattr(authority, "DEFAULT_AUTHORITY_PATH", path)
    assert runner.song_delivery_budget(state) == 1


def _reservation(base, date, index):
    row = _candidate(date, index)
    cid = row.pop("cid")
    inner = f"inner_{cid}"
    path = base / "out" / date / cid / "attempt" / "summary.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"records": [{"candidate_id": inner}]}), encoding="utf-8")
    record = {
        **row, "candidate_id": cid, "status": "blocked", "rc": 0,
        "title": f"【李豆沙】豆沙歌，《{cid}》",
        "reason_codes": ["SONG_DELIVERY_ATOMIC_COPY_FAILED"],
        "verified_delivery_pending_commit": True,
        "selector_summary_path": str(path),
        "selector_summary_sha256": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
        "selector_record_candidate_id": inner,
    }
    record["song_delivery_recovery_authority"] = runner._song_delivery_recovery_authority(
        date=date, outer_candidate_id=cid, summary_path=str(path),
        summary_sha256=record["selector_summary_sha256"], source_candidate_id=inner,
        title=record["title"],
    )
    return record


@pytest.mark.parametrize("date,cap", [("2026-10-21", 2), ("2026-10-22", 1)])
def test_recovery_consumes_the_same_dated_cap_and_preserves_bindings(tmp_path, monkeypatch, date, cap):
    monkeypatch.setattr(runner, "BASE", tmp_path)
    calls = []

    def commit(**kwargs):
        calls.append(kwargs["delivery_candidate_id"])
        return {"delivered": f"/delivery/{calls[-1]}.mp4", "delivery_upload_enabled": False}

    monkeypatch.setattr(runner, "_commit_verified_song_package", commit)
    state = {"songs": [_reservation(tmp_path, date, index) for index in range(3)]}
    # State/summary authority still rejects tampering before packaging.
    state["songs"][0]["title"] += "drift"
    assert runner.recover_bound_song_deliveries(date, state) == cap
    assert calls == [f"song_{index}" for index in range(1, cap + 1)]
    assert state["songs"][0]["verified_delivery_pending_commit"] is True
    assert not state["songs"][0].get("delivered")
    assert runner.song_delivery_budget(state) == 0
    assert runner.recover_bound_song_deliveries(date, state) == 0


def _report(tmp_path, monkeypatch, date, cap):
    monkeypatch.setattr(runner, "BASE", tmp_path)
    monkeypatch.setattr(runner, "profile_delivery_root", lambda: tmp_path / "delivery")
    reporting.write_reports(date, {"songs": [], "picks": [], "status": "no_delivery"})
    summary = (tmp_path / "delivery" / date / "AUTOSLICE_SUMMARY.md").read_text(encoding="utf-8")
    assert f"歌切（每日至多 {cap} 个、同日多场共享" in summary


@pytest.mark.parametrize("date,cap", [("2026-10-21", 2), ("2026-10-22", 1)])
def test_report_and_budget_ignore_the_legacy_alias(tmp_path, monkeypatch, date, cap):
    monkeypatch.setattr(runner, "MAX_SONGS_PER_DATE", 99)
    assert runner.song_delivery_budget({"pending_song": [_candidate(date, 0)]}) == cap
    _report(tmp_path, monkeypatch, date, cap)


def test_new_dated_grant_flows_from_asset_through_all_consumers(tmp_path, monkeypatch):
    document = authority.load_talk_quota_policy_authority()
    date, cap = "2027-01-03", 3
    document["entries"].append({
        "entry_id": "test-new-date-song", "recording_date": date, "scope": "song",
        "cap": cap, "extra_slot_min_score": None,
        "authority": "Test fixture only: a new dated song authorization, never production authority.",
    })
    path = tmp_path / "authority.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    monkeypatch.setattr(authority, "DEFAULT_AUTHORITY_PATH", path)
    state = {"pending_song": [_candidate(date, index) for index in range(4)]}
    runner.refill_songs(state)
    assert len(state["pending_song"]) == cap
    assert len(state["song_backlog"]) == 1
    assert runner.song_delivery_budget(state) == cap
    monkeypatch.setattr(runner, "BASE", tmp_path)
    monkeypatch.setattr(runner, "_commit_verified_song_package", lambda **kwargs: {
        "delivered": f"/delivery/{kwargs['delivery_candidate_id']}.mp4", "delivery_upload_enabled": False,
    })
    reservations = {"songs": [_reservation(tmp_path, date, index) for index in range(4)]}
    assert runner.recover_bound_song_deliveries(date, reservations) == cap
    _report(tmp_path, monkeypatch, date, cap)
    assert authority.resolve_quota_grant("2027-01-04", "song").cap == 1


def test_ast_guard_keeps_daily_song_numbers_in_the_authority_only():
    root = Path(__file__).resolve().parents[1]
    daily_names = {"MAX_SONGS_PER_DATE", "MAX_SONGS_PER_SESSION", "BASE_SONG_PICK_CAP"}
    definitions = []
    uses = []
    for folder in (root / "src", root / "scripts"):
        for path in folder.rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            if not any(name in source for name in daily_names):
                continue
            for node in ast.walk(ast.parse(source)):
                if isinstance(node, ast.Name) and node.id in daily_names and isinstance(node.ctx, ast.Load):
                    uses.append((path.relative_to(root).as_posix(), node.id))
                if isinstance(node, ast.Attribute) and node.attr in daily_names:
                    pytest.fail(f"daily song policy bypass: {path}:{node.lineno}")
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name) and target.id in daily_names:
                            definitions.append((path.relative_to(root).as_posix(), target.id, node.value))
    literal_definitions = [(path, name) for path, name, value in definitions if isinstance(value, ast.Constant)]
    assert literal_definitions == [("src/autoslice/talk_quota_authority.py", "BASE_SONG_PICK_CAP")]
    assert len(definitions) == 3
    assert all(path in {"scripts/session_autoslice.py", "src/autoslice/talk_quota_authority.py"} for path, _ in uses)
    alias = next(value for _, name, value in definitions if name == "MAX_SONGS_PER_DATE")
    assert isinstance(alias, ast.Attribute) and alias.attr == "cap"
    assert isinstance(alias.value, ast.Call)
    assert alias.value.args[0].value is None
    assert alias.value.args[1].value == "song"
