from __future__ import annotations

import copy
import json
from pathlib import Path

from src.autoslice import candidate_selection, host_occupancy, reporting
from src.autoslice.selection_metric_v2_shadow import (
    INPUT_SCHEMA_VERSION,
    INPUT_STATE_FIELD,
    POLICY_SHA256,
    RECEIPT_SCHEMA_VERSION,
    REPORT_FILENAME,
    SNAPSHOT_SCHEMA_VERSION,
    TOPIC_SCHEMA_VERSION,
    build_shadow_snapshot,
    canonical_sha256,
)
from src.autoslice.selection_scorecard import normalize_selection_scorecard
from src.autoslice.talk_quota_policy import TalkQuotaPolicy
from src.autoslice.surface_canon import CHANNEL_PROFILE


SESSION_ID = "live-20260809T200000+0800"


def _scorecard(*, centrality: int = 3, other: int = 3) -> dict:
    card = normalize_selection_scorecard(
        {
            "tier": 2,
            "tier_basis": "personal_stance",
            "tier_reason": "selection v2 shadow canary",
            "tier_evidence_cues": [1, 2],
            "dimensions": {
                "lidousha_centrality": centrality,
                "stance_intensity": other,
                "audience_salience": other,
                "relationship_interaction": other,
                "persona_reversal": other,
                "comedic_payoff": other,
                "self_contained": max(2, other),
            },
            "uncertainty_penalty": 0,
            "fatigue_penalty": 0,
        },
        start_cue=1,
        end_cue=2,
    )
    assert card is not None
    return card


def _candidate(
    candidate_id: str,
    *,
    start_ms: int,
    centrality: int = 3,
    other: int = 3,
) -> dict:
    return {
        "cid": candidate_id,
        "segment_path": f"/recordings/{candidate_id}.mp4",
        "start_ms": start_ms,
        "end_ms": start_ms + 90_000,
        "session_id": SESSION_ID,
        "confidence": 0.9,
        "hook": candidate_id,
        "selection_scorecard": _scorecard(
            centrality=centrality,
            other=other,
        ),
    }


def _host_report(candidate: dict) -> dict[str, object]:
    occupancy = {
        "state": host_occupancy.MULTI_VERIFIED,
        "host_speech_share": 0.8,
        "gates": {
            "classified_coverage": True,
            "unknown_share": True,
            "classified_speech_ms": True,
        },
        "reason_codes": ["SUSTAINED_NON_HOST_SPEECH"],
    }
    return {
        "schema_version": host_occupancy.SCHEMA_VERSION,
        "estimator_version": host_occupancy.ESTIMATOR_VERSION,
        "threshold_version": host_occupancy.THRESHOLD_VERSION,
        "config_hash": host_occupancy.CONFIG_HASH,
        "provisional_calibration": True,
        "candidate_id": candidate["cid"],
        "candidate_start_ms": candidate["start_ms"],
        "candidate_end_ms": candidate["end_ms"],
        "occupancy": occupancy,
        "context_occupancy": occupancy,
        "enrollment": {"source_sha256": "sha256:" + "e" * 64},
        "attribution_status": host_occupancy.map_attribution_status(occupancy),
        "requires_speaker_manual_review": False,
        "windows": [],
    }


def _shadow_input(candidate: dict, *, topic: str) -> dict[str, object]:
    return {
        "schema_version": INPUT_SCHEMA_VERSION,
        "candidate_id": candidate["cid"],
        "topic": {
            "schema_version": TOPIC_SCHEMA_VERSION,
            "fingerprint": canonical_sha256({"topic": topic}),
            "source_sha256": canonical_sha256(
                {"candidate_id": candidate["cid"], "topic": topic}
            ),
            "policy_version": "semantic-topic-fingerprint-canary.v1",
        },
        "host_occupancy": _host_report(candidate),
    }


def _state_with_inputs(*candidates: dict, topics: tuple[str, ...]) -> dict:
    assert len(candidates) == len(topics)
    return {
        "status": "processing",
        "pending_talk": list(candidates),
        "talk_backlog": [],
        "picks": [],
        "pending_song": [],
        "song_backlog": [],
        "songs": [],
        INPUT_STATE_FIELD: {
            candidate["cid"]: _shadow_input(candidate, topic=topic)
            for candidate, topic in zip(candidates, topics)
        },
    }


def test_candidate_shaped_inputs_stay_unavailable_until_producers_are_wired() -> None:
    first = _candidate("auto_200000_100_190", start_ms=100_000)
    second = _candidate("auto_200000_200_290", start_ms=200_000)
    state = _state_with_inputs(first, second, topics=("同一话题", "同一话题"))
    before = copy.deepcopy(state)

    snapshot = build_shadow_snapshot(state)

    assert state == before
    assert snapshot["schema_version"] == SNAPSHOT_SCHEMA_VERSION
    assert snapshot["policy_sha256"] == POLICY_SHA256
    assert snapshot["summary"] == {
        "candidate_count": 2,
        "available_count": 0,
        "unavailable_count": 2,
    }
    for receipt in snapshot["candidates"]:
        assert receipt["schema_version"] == RECEIPT_SCHEMA_VERSION
        assert receipt["status"] == "UNAVAILABLE"
        assert receipt["metric_v1"]["status"] == "VALID"
        assert receipt["metric_v2"] is None
        assert "PRODUCTION_TOPIC_HOST_INPUT_PRODUCERS_NOT_WIRED" in receipt["reason_codes"]
        assert receipt["observed_inputs"]["session_topic_occurrences"] == 2
        assert receipt["bindings"]["policy_sha256"] == POLICY_SHA256
        assert all(
            str(receipt["bindings"][field]).startswith("sha256:")
            for field in (
                "candidate_input_sha256",
                "selection_scorecard_sha256",
                "shadow_input_sha256",
                "topic_input_sha256",
                "host_occupancy_input_sha256",
            )
        )
        assert receipt["decision_surfaces"] == {
            "decision_influence": False,
            "selection_authorized": False,
            "quota_authorized": False,
            "release_gate": False,
            "upload_authorized": False,
        }


def test_hook_event_key_and_nominal_scene_never_substitute_for_typed_evidence() -> None:
    candidate = _candidate("auto_200000_300_390", start_ms=300_000)
    candidate.update(
        {
            "event_key": "看起来像 topic",
            "topic_fingerprint": canonical_sha256("untyped topic"),
            "segment_scene_context": {"scene_kind": "talk"},
            "nominal_solo_stream": True,
        }
    )

    snapshot = build_shadow_snapshot({"pending_talk": [candidate]})
    receipt = snapshot["candidates"][0]

    assert receipt["status"] == "UNAVAILABLE"
    assert receipt["metric_v1"]["status"] == "VALID"
    assert receipt["metric_v2"] is None
    assert "SHADOW_INPUT_MISSING" in receipt["reason_codes"]
    assert receipt["observed_inputs"]["topic_fingerprint"] is None
    assert receipt["observed_inputs"]["host_attribution_status"] is None


def test_boundary_drift_and_incomplete_session_topic_coverage_are_unavailable() -> None:
    first = _candidate("auto_200000_400_490", start_ms=400_000)
    second = _candidate("auto_200000_500_590", start_ms=500_000)
    state = _state_with_inputs(first, second, topics=("一", "二"))
    del state[INPUT_STATE_FIELD][second["cid"]]
    state[INPUT_STATE_FIELD][first["cid"]]["host_occupancy"][
        "candidate_end_ms"
    ] += 1

    snapshot = build_shadow_snapshot(state)
    by_id = {row["candidate_id"]: row for row in snapshot["candidates"]}

    assert by_id[first["cid"]]["status"] == "UNAVAILABLE"
    assert "HOST_OCCUPANCY_CANDIDATE_END_MS_MISMATCH" in by_id[first["cid"]][
        "reason_codes"
    ]
    assert "SESSION_TOPIC_COVERAGE_INCOMPLETE" in by_id[first["cid"]][
        "reason_codes"
    ]
    assert by_id[second["cid"]]["status"] == "UNAVAILABLE"
    assert "SHADOW_INPUT_MISSING" in by_id[second["cid"]]["reason_codes"]


def test_current_row_wins_over_ordinary_superseded_attempt_history() -> None:
    current = _candidate("auto_200000_550_640", start_ms=550_000)
    state = _state_with_inputs(current, topics=("当前主题",))
    state["talk_superseded_attempts"] = [
        {
            "candidate_id": current["cid"],
            "status": "failed",
            "failure_kind": "subtitle_authority",
            "start_ms": current["start_ms"],
            "end_ms": current["end_ms"],
            "session_id": current["session_id"],
            # Historical attempts commonly predate scorecard persistence.
            "selection_scorecard": None,
        }
    ]

    snapshot = build_shadow_snapshot(state)

    assert snapshot["summary"] == {
        "candidate_count": 1,
        "available_count": 0,
        "unavailable_count": 1,
    }
    receipt = snapshot["candidates"][0]
    assert receipt["candidate_id"] == current["cid"]
    assert receipt["status"] == "UNAVAILABLE"
    assert "PRODUCTION_TOPIC_HOST_INPUT_PRODUCERS_NOT_WIRED" in receipt["reason_codes"]
    assert "CANDIDATE_METRIC_INPUT_CONFLICT" not in receipt["reason_codes"]


class _SelectionRunner:
    MAX_TALK_PICKS = 5
    MIN_TALK_CONFIDENCE = 0.8
    TALK_PER_SEGMENT_CAP = 2
    TALK_ATTEMPT_CAP = 10
    TALK_REPAIR_LIFETIME_RETRY_CAP = 6
    TALK_COVER_PENDING_STATUS = "cover_pending"
    DELIVERED_TALK_STATUSES = {"ok", "review_ready"}

    def __init__(self, base: Path) -> None:
        self.BASE = base

    @staticmethod
    def exclude_session_edge_bgm_candidates(_state: dict) -> None:
        return None

    @staticmethod
    def quarantine_overlapping_talk_candidates(_state: dict) -> None:
        return None

    @staticmethod
    def refill_songs(_state: dict) -> None:
        return None

    @staticmethod
    def _note_not_selected(_state: dict, _message: str) -> None:
        return None

    @staticmethod
    def log(_message: str) -> None:
        return None


def test_shadow_cannot_change_v1_order_quota_bytes_or_release_gate(
    tmp_path: Path, monkeypatch
) -> None:
    # A wins v1 through centrality, while B wins the v2 broad path.  If the
    # shadow leaked into ranking, this cap=1 canary would flip candidates.
    candidate_a = _candidate(
        "auto_200000_600_690", start_ms=600_000, centrality=4, other=3
    )
    candidate_b = _candidate(
        "auto_200000_700_790", start_ms=700_000, centrality=0, other=4
    )
    base_state = _state_with_inputs(
        candidate_a, candidate_b, topics=("候选A", "候选B")
    )
    shadow_state = copy.deepcopy(base_state)
    control_state = copy.deepcopy(base_state)
    control_state.pop(INPUT_STATE_FIELD)

    snapshot = build_shadow_snapshot(shadow_state)
    by_id = {row["candidate_id"]: row for row in snapshot["candidates"]}
    assert (
        by_id[candidate_a["cid"]]["metric_v1"]["effective_score"]
        > by_id[candidate_b["cid"]]["metric_v1"]["effective_score"]
    )
    assert by_id[candidate_a["cid"]]["metric_v2"] is None
    assert by_id[candidate_b["cid"]]["metric_v2"] is None

    policy = TalkQuotaPolicy(
        kind="talk",
        scope_key=f"talk:{SESSION_ID}",
        cap=1,
        extra_slot_min_score=None,
        recording_date="2026-08-09",
        policy_source="canary:v1-only",
    )
    monkeypatch.setattr(candidate_selection, "_runner", _SelectionRunner(tmp_path))
    monkeypatch.setattr(candidate_selection, "_talk_quota_policy", lambda _item: policy)
    monkeypatch.setattr(
        candidate_selection,
        "hold_published_topic_collision_reviews",
        lambda _state: None,
    )
    monkeypatch.setattr(
        candidate_selection,
        "hold_talk_outside_operator_scope",
        lambda _state, frozen_candidate_ids=None: [],
    )
    monkeypatch.setattr(
        candidate_selection,
        "release_operator_scope_held_talk",
        lambda _state, _held: None,
    )

    candidate_selection.prioritize(control_state)
    candidate_selection.prioritize(shadow_state)

    assert [row["cid"] for row in shadow_state["pending_talk"]] == [
        candidate_a["cid"]
    ]
    assert [row["cid"] for row in shadow_state["pending_talk"]] == [
        row["cid"] for row in control_state["pending_talk"]
    ]
    assert [row["cid"] for row in shadow_state["talk_backlog"]] == [
        row["cid"] for row in control_state["talk_backlog"]
    ]
    quota_bytes = lambda value: json.dumps(  # noqa: E731
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    assert quota_bytes(shadow_state["talk_quota_policy_disclosure"]) == quota_bytes(
        control_state["talk_quota_policy_disclosure"]
    )
    assert all(
        row["decision_surfaces"]["release_gate"] is False
        and row["decision_surfaces"]["upload_authorized"] is False
        for row in snapshot["candidates"]
    )


class _ReportingRunner:
    DELIVERED_TALK_STATUSES = {"ok", "review_ready"}
    MAX_SONGS_PER_SESSION = 1
    PROFILE_DISPLAY_NAME = CHANNEL_PROFILE.display_name

    def __init__(self, root: Path) -> None:
        self.BASE = root
        self._delivery = root / "delivery"

    def profile_delivery_root(self) -> Path:
        return self._delivery

    @staticmethod
    def safe_name(text: str, fallback: str) -> str:
        return text or fallback


def test_historical_current_report_persists_json_and_renders_v1_v2(
    tmp_path: Path, monkeypatch
) -> None:
    first = _candidate("auto_200000_800_890", start_ms=800_000)
    second = _candidate("auto_200000_900_990", start_ms=900_000)
    state = _state_with_inputs(first, second, topics=("一", "二"))
    before = copy.deepcopy(state)
    monkeypatch.setattr(reporting, "_runner", _ReportingRunner(tmp_path))

    reporting.write_reports("2026-08-09", state)

    assert state == before
    sidecar_path = tmp_path / "delivery" / "2026-08-09" / REPORT_FILENAME
    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    assert sidecar["schema_version"] == SNAPSHOT_SCHEMA_VERSION
    assert sidecar["summary"] == {
        "candidate_count": 2,
        "available_count": 0,
        "unavailable_count": 2,
    }
    markdown = (sidecar_path.parent / "AUTOSLICE_SUMMARY.md").read_text(
        encoding="utf-8"
    )
    assert "选片 v1 / v2 影子对照（仅观察）" in markdown
    assert "v1 仍是唯一的选片、排序与配额口径" in markdown
    assert "UNAVAILABLE: PRODUCTION_TOPIC_HOST_INPUT_PRODUCERS_NOT_WIRED" in markdown
    latest = (tmp_path / "reports" / "latest.md").read_text(encoding="utf-8")
    assert "不参与选片、配额、发布或上传门" in latest
