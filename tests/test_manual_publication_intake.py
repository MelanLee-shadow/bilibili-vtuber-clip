from copy import deepcopy
import hashlib
import json

import pytest

from src.autoslice.manual_publication_intake import (
    SCHEMA, plan_manual_publication_intake, verified_manual_inputs,
)
from src.autoslice.publication_state_projection import PublicationReconciliationError


def fixture():
    state = {"date": "2026-08-25", "status": "paused_cpa_down", "picks": [], "songs": [],
             "pending_talk": [{"cid": "other", "kept": True},
                              {"cid": "candidate", "segment_path": "/offline/recording.mp4",
                               "start_ms": 1000, "end_ms": 4000, "retry_count": 3}],
             "pending_song": [{"cid": "song-kept"}], "talk_backlog": [],
             "song_backlog": [], "operator_scope": {"unchanged": True}}
    publication = {"schema_version": "publication-reconciliation.v1", "status": "VERIFIED_PUBLIC",
                   "candidate_id": "candidate", "recording_date": "2026-08-25",
                   "bvid": "BV1example", "aid": 123, "cid": 456, "title": "title",
                   "authority": {"path": "/receipt.json", "sha256": "a" * 64, "bytes": 4},
                   "reconciled_at": "2026-09-21T00:00:00Z"}
    values = {"publication": publication, "recording_basename": "recording.mp4",
              "source_interval_ms": [900, 4100], "source_media_sha256": "b" * 64,
              "manifest": {"path": "/manifest.json", "sha256": "c" * 64},
              "record": {"path": "/record.json", "sha256": "d" * 64},
              "review_manifest": {"path": "/review.json"}, "provenance": {"path": "/prov.json"},
              "video_path": "/video.mp4", "subtitle_path": "/subtitle.srt",
              "cover_path": "/cover.png", "video_sha256": "e" * 64, "cover_sha256": "f" * 64}
    return state, values


def test_exact_queue_move_keeps_pause_original_evidence_and_unrelated_rows():
    state, values = fixture()
    original = deepcopy(state)
    after, result = plan_manual_publication_intake(state, values)
    assert state == original
    assert after["status"] == "paused_cpa_down"
    assert after["pending_talk"] == [original["pending_talk"][0]]
    assert len(after["picks"]) == 1
    row = after["picks"][0]
    assert row["status"] == "published" and row["published_cid"] == 456
    assert row["cid"] == row["candidate_id"] == "candidate"
    assert row["retry_count"] == 3
    receipt = row["manual_publication_intake"]
    assert receipt["schema_version"] == SCHEMA
    assert receipt["original_queue_row"] == original["pending_talk"][1]
    assert receipt["original_queue_row_sha256"] == hashlib.sha256(json.dumps(
        receipt["original_queue_row"], ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode()).hexdigest()
    assert receipt["prepublication_quality_synthesized"] is False
    assert result["state_changed"]
    for key in set(state) - {"picks", "pending_talk", "publication_closure"}:
        assert after[key] == state[key]


def test_same_publication_replay_is_byte_semantically_idempotent():
    state, values = fixture()
    after, _ = plan_manual_publication_intake(state, values)
    replay, result = plan_manual_publication_intake(after, values)
    assert replay == after and result["state_changed"] is False


@pytest.mark.parametrize("mode", ["source", "start", "end", "date", "unverified", "bvid", "absent", "duplicate", "song", "backlog"])
def test_conflicts_refuse_without_mutating_state(mode):
    state, values = fixture()
    row = state["pending_talk"][1]
    if mode == "source":
        row["segment_path"] = "/offline/different.mp4"
    elif mode == "start":
        row["start_ms"] = 800
    elif mode == "end":
        row["end_ms"] = 4200
    elif mode == "date":
        state["date"] = "2026-08-26"
    elif mode == "unverified":
        values["publication"]["status"] = "PENDING"
    elif mode == "bvid":
        row["bvid"] = "BV1different"
    elif mode == "absent":
        state["pending_talk"].pop()
    elif mode == "duplicate":
        state["talk_backlog"].append(deepcopy(row))
    elif mode == "song":
        state["pending_song"].append(state["pending_talk"].pop())
    elif mode == "backlog":
        state["talk_backlog"].append(state["pending_talk"].pop())
    before = deepcopy(state)
    with pytest.raises(PublicationReconciliationError):
        plan_manual_publication_intake(state, values)
    assert state == before


def test_existing_published_row_different_manifest_refused():
    state, values = fixture()
    after, _ = plan_manual_publication_intake(state, values)
    values["manifest"]["sha256"] = "0" * 64
    with pytest.raises(PublicationReconciliationError):
        plan_manual_publication_intake(after, values)


def test_native_manifest_errors_are_never_turned_into_intake(monkeypatch, tmp_path):
    from scripts import authorized_upload
    monkeypatch.setattr(authorized_upload, "load_and_verify", lambda *a, **k: ({}, ["invalid"]))
    with pytest.raises(PublicationReconciliationError, match="native validation"):
        verified_manual_inputs(tmp_path / "missing.json", "BV1example")


def test_missing_public_authority_refuses_even_when_manifest_parses(monkeypatch, tmp_path):
    from scripts import authorized_upload
    monkeypatch.setattr(authorized_upload, "load_and_verify", lambda *a, **k: ({"season": {"lane": "talk"}}, []))
    with pytest.raises(PublicationReconciliationError):
        verified_manual_inputs(tmp_path / "missing.json", "BV1example")


def test_applied_check_does_not_treat_queued_row_as_completed(monkeypatch, tmp_path):
    from scripts import authorized_upload
    from src.autoslice import manual_publication_intake as module
    state, _ = fixture()
    monkeypatch.setattr(authorized_upload, "load_and_verify", lambda *a, **k: ({}, []))
    monkeypatch.setattr(module.reconciliation, "_candidate_and_date", lambda m: ("candidate", "2026-08-25"))
    assert not module.already_applied_manual_intake(state, tmp_path / "manifest.json", "BV1example")


def test_applied_check_different_bvid_refuses_before_any_projection(monkeypatch, tmp_path):
    from scripts import authorized_upload
    from src.autoslice import manual_publication_intake as module
    state, values = fixture()
    state, _ = module.plan_manual_publication_intake(state, values)
    monkeypatch.setattr(authorized_upload, "load_and_verify", lambda *a, **k: ({}, []))
    monkeypatch.setattr(module.reconciliation, "_candidate_and_date", lambda m: ("candidate", "2026-08-25"))
    before = deepcopy(state)
    with pytest.raises(PublicationReconciliationError):
        module.already_applied_manual_intake(state, tmp_path / "manifest.json", "BV1different")
    assert state == before
