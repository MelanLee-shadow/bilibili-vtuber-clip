from copy import deepcopy

import pytest

from src.autoslice.detached_manual_publication_intake import (
    SCHEMA,
    DetachedPublicationIntakeError,
    plan_detached_publication_intake,
)


def _entry(path: str, letter: str) -> dict[str, object]:
    return {"path": path, "sha256": letter * 64, "bytes": 10}


def fixture() -> tuple[dict, dict]:
    authority = _entry("/receipts/public-authority.json", "a")
    publication = {
        "schema_version": "publication-reconciliation.v1",
        "status": "VERIFIED_PUBLIC",
        "candidate_id": "auto_121012_796_992",
        "recording_date": "2026-08-26",
        "bvid": "BV1seHr62Etj",
        "aid": 117380718007741,
        "cid": 42446947834,
        "title": "dream",
        "authority": authority,
        "reconciled_at": "2026-10-03T00:00:00Z",
    }
    source_binding = {
        "source_path": "/source/auto_121012_796_992.source-window.v4.mp4",
        "source_basename": "auto_121012_796_992.source-window.v4.mp4",
        "source_media_sha256": "b" * 64,
        "source_interval_ms": [29950, 275110],
        "final_recut_path": "/package/auto_121012_796_992.recut.mp4",
        "final_recut_sha256": "e" * 64,
        "final_recut_interval_ms": [29950, 275110],
        "media_path": "/package/auto_121012_796_992.recut.mp4",
        "media_sha256": "e" * 64,
        "record_interval_ms": [0, 245160],
    }
    values = {
        "publication": publication,
        "publication_authority": deepcopy(authority),
        "candidate_id": publication["candidate_id"],
        "recording_date": publication["recording_date"],
        "manifest": _entry("/package/manifest.json", "c"),
        "record": _entry("/package/record.json", "d"),
        "review_manifest": _entry("/package/review.json", "f"),
        "provenance": _entry("/package/recut.provenance.json", "1"),
        "source_binding": source_binding,
        "validated_source_binding": deepcopy(source_binding),
        "source_interval_ms": [29950, 275110],
        "source_media_sha256": "b" * 64,
        "record_interval_ms": [0, 245160],
        "final_recut_interval_ms": [29950, 275110],
        "manual_package_validated": True,
        "source_provenance_validated": True,
        "detached_source": True,
        "prepublication_verdict_synthesized": False,
        "selection_scorecard": {"effective_score": 66.5, "status": "VALID"},
        "session_relation_authority": None,
        "video_path": "/package/auto_121012_796_992.recut.mp4",
        "subtitle_path": "/package/auto_121012_796_992.srt",
        "video_sha256": "e" * 64,
        "cover_path": "/package/cover.png",
        "cover_sha256": "9" * 64,
    }
    state = {
        "date": "2026-08-26",
        "status": "publication_in_progress",
        "HOLD": {"reason": "keep"},
        "budget": {"provider_calls": 0},
        "picks": [{"candidate_id": "existing-pick", "status": "published"}],
        "songs": [{"cid": "existing-song"}],
        "pending_talk": [{"cid": "other-talk"}],
        "talk_backlog": [{"cid": "other-backlog"}],
        "pending_song": [{"cid": "other-pending-song"}],
        "song_backlog": [{"cid": "other-song-backlog"}],
        "unrelated": {"nested": [1, 2, {"keep": True}]},
    }
    return state, values


def test_missing_candidate_projects_one_published_pick_without_verdict_or_side_effects():
    state, values = fixture()
    before = deepcopy(state)
    after, plan = plan_detached_publication_intake(
        state, values, state_sha256="0" * 64, created_at="2026-10-04T00:00:00Z"
    )

    assert state == before
    assert plan["status"] == "PLANNED"
    row = after["picks"][-1]
    assert row["candidate_id"] == values["candidate_id"]
    assert row["status"] == "published"
    assert row["bvid"] == values["publication"]["bvid"]
    assert row["source_path"] == values["source_binding"]["source_path"]
    assert row["source_interval_ms"] == values["source_interval_ms"]
    assert "review_ready" not in row
    assert "quality" not in row
    receipt = row["detached_manual_publication"]
    assert receipt["schema_version"] == SCHEMA
    assert receipt["prepublication_verdict_synthesized"] is False
    assert receipt["publication_permission_granted"] is False
    assert receipt["state_before_sha256"] == "0" * 64
    for key in set(state) - {"picks"}:
        assert after[key] == state[key]


def test_identical_detached_receipt_is_idempotent_and_does_not_duplicate():
    state, values = fixture()
    first, _ = plan_detached_publication_intake(
        state, values, state_sha256="1" * 64, created_at="2026-10-04T00:00:00Z"
    )
    replay, plan = plan_detached_publication_intake(
        first, values, state_sha256="2" * 64, created_at="2026-10-04T01:00:00Z"
    )
    assert replay == first
    assert plan["status"] == "ALREADY_APPLIED"
    assert plan["state_changed"] is False
    assert len(replay["picks"]) == len(first["picks"]) == 2


def test_conflicting_bvid_is_rejected_without_mutation():
    state, values = fixture()
    state["talk_backlog"].append({"candidate_id": "other", "bvid": values["publication"]["bvid"]})
    before = deepcopy(state)
    with pytest.raises(DetachedPublicationIntakeError, match="BVID_CONFLICT"):
        plan_detached_publication_intake(state, values, state_sha256="3" * 64)
    assert state == before


def test_duplicate_candidate_and_non_manual_inputs_are_rejected():
    state, values = fixture()
    state["pending_talk"].extend(
        [
            {"candidate_id": values["candidate_id"]},
            {"candidate_id": values["candidate_id"]},
        ]
    )
    with pytest.raises(DetachedPublicationIntakeError, match="DUPLICATE_CANDIDATE"):
        plan_detached_publication_intake(state, values, state_sha256="4" * 64)

    clean_state, non_manual = fixture()
    non_manual.pop("manual_package_validated")
    with pytest.raises(DetachedPublicationIntakeError, match="NON_MANUAL_PACKAGE"):
        plan_detached_publication_intake(clean_state, non_manual, state_sha256="5" * 64)


def test_drifted_source_binding_is_rejected():
    state, values = fixture()
    values["source_binding"]["source_media_sha256"] = "6" * 64
    with pytest.raises(DetachedPublicationIntakeError, match="SOURCE_BINDING_INVALID"):
        plan_detached_publication_intake(state, values, state_sha256="7" * 64)


def test_same_source_and_overlapping_interval_is_a_delivery_conflict():
    state, values = fixture()
    state["talk_backlog"].append(
        {
            "candidate_id": "other-delivery",
            "source_path": values["source_binding"]["source_path"],
            "source_interval_ms": [100000, 120000],
        }
    )
    with pytest.raises(DetachedPublicationIntakeError, match="SOURCE_DELIVERY_CONFLICT"):
        plan_detached_publication_intake(state, values, state_sha256="8" * 64)


def test_changed_existing_detached_receipt_is_not_treated_as_replay():
    state, values = fixture()
    first, _ = plan_detached_publication_intake(state, values, state_sha256="9" * 64)
    first["picks"][-1]["detached_manual_publication"]["publication_permission_granted"] = True
    with pytest.raises(DetachedPublicationIntakeError, match="EXISTING_CANDIDATE_CONFLICT"):
        plan_detached_publication_intake(first, values, state_sha256="a" * 64)
