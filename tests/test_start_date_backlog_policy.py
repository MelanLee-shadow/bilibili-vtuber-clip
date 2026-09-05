from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.autoslice.start_date_backlog_policy import start_date_is_terminal


def test_pending_lane_keeps_terminal_projection_unfinished(tmp_path: Path) -> None:
    state = tmp_path / "state.json"

    def state_path(_date: str) -> Path:
        return state

    for pending in (
        {"pending_talk": [{"cid": "fixture-talk"}]},
        {"pending_song": [{"cid": "fixture-song"}]},
    ):
        state.write_text(
            json.dumps({"status": "no_delivery", **pending}),
            encoding="utf-8",
        )
        assert not start_date_is_terminal("2099-01-01", state_path)

    state.write_text(
        json.dumps({"status": "no_delivery", "pending_talk": [], "pending_song": []}),
        encoding="utf-8",
    )
    assert start_date_is_terminal("2099-01-01", state_path)


def test_queued_cover_route_pick_keeps_terminal_date_in_backlog(tmp_path: Path) -> None:
    state = tmp_path / "state.json"

    def state_path(_date: str) -> Path:
        return state

    queued_cover_pick = {
        "candidate_id": "auto_210028_427_660",
        "status": "failed",
        "failure_kind": "cover_route_regeneration",
        "failure_recoverable": True,
        "cover_status": "SCREENSHOT_ROUTE_REGENERATION_QUEUED",
        "cover_route_regeneration_fingerprint": (
            "sha256:589d58bacd6ec846fbe0d19654e627e165db4181274d27cfe538405b47da7daa"
        ),
        "cover_route_regeneration_attempts": 1,
        "pipeline_fingerprint": "sha256:" + "a6be3000" + "0" * 56,
        "video_sha256": "sha256:" + "1" * 64,
        "subtitle_sha256": "sha256:" + "2" * 64,
    }
    state.write_text(
        json.dumps(
            {
                "status": "review_ready_with_failures",
                "pending_talk": [],
                "pending_song": [],
                "picks": [queued_cover_pick],
            }
        ),
        encoding="utf-8",
    )

    assert not start_date_is_terminal("2026-08-20", state_path)

    # This is the old projection: with the queued failed pick omitted, the
    # same empty-lane terminal state would have been hidden from maintenance.
    state.write_text(
        json.dumps(
            {
                "status": "review_ready_with_failures",
                "pending_talk": [],
                "pending_song": [],
                "picks": [],
            }
        ),
        encoding="utf-8",
    )
    assert start_date_is_terminal("2026-08-20", state_path)


@pytest.mark.parametrize(
    "changes",
    (
        {"failure_kind": "provider_transient"},
        {"failure_recoverable": False},
        {"cover_route_regeneration_fingerprint": None},
        {"cover_route_regeneration_attempts": 0},
        {"cover_route_regeneration_attempts": "bad"},
        {"cover_repair_exhausted": True, "failure_recoverable": False},
    ),
    ids=(
        "ordinary-recoverable-failure",
        "nonrecoverable-route-budget",
        "missing-route-fingerprint",
        "unspent-route-attempt",
        "malformed-route-attempt",
        "cover-repair-budget-exhausted",
    ),
)
def test_nonqueued_or_exhausted_cover_work_does_not_reopen_terminal_date(
    tmp_path: Path, changes: dict[str, object]
) -> None:
    state = tmp_path / "state.json"

    def state_path(_date: str) -> Path:
        return state

    pick = {
        "candidate_id": "cover-negative",
        "status": "failed",
        "failure_kind": "cover_route_regeneration",
        "failure_recoverable": True,
        "cover_status": "SCREENSHOT_ROUTE_REGENERATION_QUEUED",
        "cover_route_regeneration_fingerprint": "sha256:" + "a" * 64,
        "cover_route_regeneration_attempts": 1,
    }
    pick.update(changes)
    state.write_text(
        json.dumps(
            {
                "status": "review_ready_with_failures",
                "pending_talk": [],
                "pending_song": [],
                "picks": [pick],
            }
        ),
        encoding="utf-8",
    )

    assert start_date_is_terminal("2026-08-20", state_path)
