"""Replay real legacy route labels without inventing screenshot history."""

import json
import pytest
from src.autoslice.title_cover_history import recent_choices, source_session_key


@pytest.mark.parametrize("route,emote", [("cpa_redraw", ""), ("cpa_redraw", "09"), (None, "")])
def test_legacy_images_edit_history_keeps_actual_ai_route(tmp_path, route, emote):
    record = {
        "candidate_id": "previous",
        "recording_date": "2026-10-18",
        "created_at": "2026-10-18T12:00:00Z",
        "publish_staging": {
            "cover_status": "AI_COVER_READY",
            "cover_generation": {
                "method": "images.edit",
                "route_decision": {"actual_treatment": route},
                "art_direction": {"emote_id": emote},
            },
        },
    }
    path = tmp_path / "previous.record.json"
    path.write_text(json.dumps(record))
    history = recent_choices(tmp_path, source_session_key(record), "next")
    assert history["previous_route"] == "ai_scene"
    assert history["source_session_main_emotes"] == (["emote:09"] if emote else [])
    assert history["inputs"][0]["order_basis"] == "RECORD_CREATED_AT"
