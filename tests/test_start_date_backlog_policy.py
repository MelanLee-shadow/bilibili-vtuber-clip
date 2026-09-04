from __future__ import annotations

import json
from pathlib import Path

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
