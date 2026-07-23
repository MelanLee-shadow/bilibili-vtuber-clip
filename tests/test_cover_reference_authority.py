import json
from pathlib import Path

import pytest

from src.autoslice.cover_reference_authority import (
    CoverReferenceAuthorityError,
    load_candidate_cover_reference,
)
from src.autoslice.story_contract import (
    build_story_contract,
    cover_relation_prompt,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


def _relation():
    return {
        "state": "CONFIRMED",
        "participants": [
            {"canonical_id": "lidousha", "display_name": "李豆沙"},
            {"canonical_id": "nancho", "display_name": "南町"},
        ],
    }


def test_committed_chair_reference_is_hash_bound_and_survives_recut_suffix():
    reference = load_candidate_cover_reference(
        "auto_193450_1573_1672r4",
        ledger_path=(
            REPO_ROOT
            / "assets/lidousha/cover_reference_overrides.v1.json"
        ),
    )

    assert reference is not None
    assert reference["content_time_ms"] == 21_500
    assert reference["visible_participant_ids"] == ["lidousha", "nancho"]
    assert reference["required_treatment"] == "screenshot_direct"
    assert str(reference["ledger_sha256"]).startswith("sha256:")

    contract = build_story_contract(
        candidate_id="auto_193450_1573_1672",
        selection_hook="搭档把大椅子让给李豆沙",
        transcript_text="她把大椅子给我了，像被李豆沙霸凌。",
        selection_scorecard={"status": "VALID"},
        session_relation_authority=_relation(),
        cover_reference_authority=reference,
        source_media_sha256s=[reference["source_sha256"]],
    )
    assert contract["cover_counterpart_reference_available"] is True
    assert contract["cover_fallback_mode"] == "VERIFIED_DUAL_STREAM_FRAME"
    assert contract["source_media_sha256s"] == [reference["source_sha256"]]
    prompt = cover_relation_prompt(contract)
    assert "hash-bound source frame" in prompt
    assert "Preserve both" in prompt
    assert "HOST-ONLY" not in prompt


def test_reference_visible_participants_must_belong_to_story_contract():
    contract = build_story_contract(
        candidate_id="x",
        selection_hook="联动",
        transcript_text="文本",
        selection_scorecard={"status": "VALID"},
        session_relation_authority=_relation(),
        cover_reference_authority={
            "visible_participant_ids": ["lidousha", "unknown_guest"]
        },
    )
    assert contract["cover_counterpart_reference_available"] is False
    assert contract["cover_fallback_mode"] == "HOST_ONLY_RELATION_EXPLICIT"


def test_duplicate_reference_rows_fail_closed(tmp_path):
    row = {
        "candidate_id": "candidate",
        "content_time_ms": 1,
        "source_time_ms": 2,
        "source_sha256": "sha256:" + "1" * 64,
        "reference_png_sha256": "sha256:" + "2" * 64,
        "visible_participant_ids": ["lidousha", "nancho"],
        "required_treatment": "screenshot_direct",
        "authority": "reviewed",
    }
    path = tmp_path / "cover-references.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "lidousha-cover-reference-overrides.v1",
                "overrides": [row, dict(row)],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(CoverReferenceAuthorityError, match="AMBIGUOUS"):
        load_candidate_cover_reference("candidate", ledger_path=path)
