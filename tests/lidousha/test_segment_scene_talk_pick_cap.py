"""Synthetic F13 scene receipts and mutually exclusive quota accounting."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import scripts.session_autoslice as runner
import src.autoslice.session_discovery as session_discovery
from src.autoslice.segment_scene_context import resolve_segment_scene_context
from src.autoslice.selection_scorecard import normalize_selection_scorecard
from src.autoslice.talk_quota_policy import resolve_talk_quota_policy


COLLAPSED_SESSION = "live-20260808Tunknown"


def _scene(
    tmp_path: Path,
    *,
    clock: str,
    width: int | None,
    height: int | None,
    title: str,
) -> tuple[Path, dict[str, object]]:
    segment = tmp_path / f"22966160_20260808-{clock}.mp4"
    segment.write_bytes(b"synthetic segment")
    segment.with_suffix(".meta.json").write_text(
        json.dumps({"recorder": {"title": title}}, ensure_ascii=False),
        encoding="utf-8",
    )

    def probe(_segment: Path) -> dict[str, object]:
        if width is None or height is None:
            return {
                "status": "UNKNOWN",
                "width": None,
                "height": None,
                "orientation": "unknown",
                "reason_code": "SYNTHETIC_PROBE_UNAVAILABLE",
            }
        return {
            "status": "PASS",
            "width": width,
            "height": height,
            "orientation": "fixture-value-is-recomputed",
        }

    return segment, resolve_segment_scene_context(segment, dimension_probe=probe)


def _scorecard(score: float) -> dict[str, object]:
    total_penalty = 100.0 - score
    result = normalize_selection_scorecard(
        {
            "tier": 2,
            "tier_basis": "personal_stance",
            "tier_reason": "synthetic F13 quota fixture",
            "tier_evidence_cues": [1, 2],
            "dimensions": {
                "lidousha_centrality": 4,
                "stance_intensity": 4,
                "audience_salience": 4,
                "relationship_interaction": 4,
                "persona_reversal": 4,
                "comedic_payoff": 4,
                "self_contained": 4,
            },
            "uncertainty_penalty": min(15.0, total_penalty),
            "fatigue_penalty": max(0.0, total_penalty - 15.0),
        },
        start_cue=1,
        end_cue=2,
    )
    assert result is not None
    assert result["effective_score"] == pytest.approx(score)
    return result


def _candidate(
    cid: str,
    *,
    segment: Path,
    scene: dict[str, object],
    score: float = 100.0,
    hook: str | None = None,
) -> dict[str, object]:
    return {
        "cid": cid,
        "segment_path": str(segment),
        "segment_scene_context": scene,
        "session_id": COLLAPSED_SESSION,
        "start_ms": 10_000,
        "end_ms": 40_000,
        "confidence": 0.99,
        "hook": hook or cid,
        "selection_scorecard": _scorecard(score),
    }


def _state(*, picks: list[dict], pending: list[dict]) -> dict[str, object]:
    return {
        "picks": picks,
        "songs": [],
        "pending_song": [],
        "pending_talk": pending,
    }


def test_scene_receipt_is_stat_bound_cached_and_fail_closed(
    tmp_path: Path,
) -> None:
    event_segment, event = _scene(
        tmp_path,
        clock="20-01-30",
        width=1920,
        height=1080,
        title="李豆沙三周年 3D Live",
    )
    portrait_segment, portrait = _scene(
        tmp_path,
        clock="23-01-25",
        width=1080,
        height=1920,
        title="3DLive 后的生日杂谈",
    )
    _unknown_segment, unknown = _scene(
        tmp_path,
        clock="23-31-23",
        width=None,
        height=None,
        title="三周年 3D Live",
    )

    assert event["scene_kind"] == "event"
    assert portrait["scene_kind"] == "talk"
    assert portrait["decision_reason_code"] == "PORTRAIT_SEGMENT_NEVER_EVENT"
    assert unknown["scene_kind"] == "talk"

    calls = 0

    def must_not_reprobe(_segment: Path) -> dict[str, object]:
        nonlocal calls
        calls += 1
        raise AssertionError("stat-bound receipt should be reused")

    assert resolve_segment_scene_context(
        event_segment, cached=event, dimension_probe=must_not_reprobe
    ) == event
    assert calls == 0
    assert portrait_segment.name == portrait["source_segment"]


def test_unknown_probe_is_fail_closed_for_the_tick_but_retried_later(
    tmp_path: Path,
) -> None:
    segment, unknown = _scene(
        tmp_path,
        clock="22-31-30",
        width=None,
        height=None,
        title="三周年 3D Live",
    )
    calls = 0

    def recovered_probe(_segment: Path) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"status": "PASS", "width": 1920, "height": 1080}

    recovered = resolve_segment_scene_context(
        segment,
        cached=unknown,
        dimension_probe=recovered_probe,
    )

    assert unknown["scene_kind"] == "talk"
    assert calls == 1
    assert recovered["scene_kind"] == "event"
    assert recovered["media_probe"]["orientation"] == "landscape"


def test_f13_canary_event_fifteen_and_same_day_talk_five_are_independent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative canary: reverting event scope collapses this batch back to 5."""

    base = tmp_path / "autoslice"
    monkeypatch.setattr(runner, "BASE", base)
    event_segment, event = _scene(
        tmp_path,
        clock="20-01-30",
        width=1920,
        height=1080,
        title="三周年 3D Live",
    )
    talk_segment, talk = _scene(
        tmp_path,
        clock="23-01-25",
        width=1080,
        height=1920,
        title="深夜杂谈",
    )
    delivered_event = [
        {
            **_candidate(
                f"delivered-event-{index}", segment=event_segment, scene=event
            ),
            "status": "review_ready",
        }
        for index in range(5)
    ]
    named_backups = [
        _candidate(
            "event-backup-goodbye-lala",
            segment=event_segment,
            scene=event,
            score=90.2,
            hook="再见拉拉",
        ),
        _candidate(
            "event-backup-fulltime-envy",
            segment=event_segment,
            scene=event,
            score=89.2,
            hook="羡慕全职",
        ),
        _candidate(
            "event-backup-through-screen",
            segment=event_segment,
            scene=event,
            score=86.8,
            hook="穿越屏幕",
        ),
    ]
    ordinary_talk = [
        _candidate(f"portrait-talk-{index}", segment=talk_segment, scene=talk)
        for index in range(6)
    ]
    state = _state(
        picks=delivered_event,
        pending=[*named_backups, *ordinary_talk],
    )

    runner.prioritize(state)

    pending_ids = {row["cid"] for row in state["pending_talk"]}
    assert {row["cid"] for row in named_backups} <= pending_ids
    assert sum(cid.startswith("portrait-talk-") for cid in pending_ids) == 5
    assert len(state["pending_talk"]) == 8
    assert [row["cid"] for row in state["talk_backlog"]] == ["portrait-talk-5"]


def test_event_extra_slots_use_existing_eighty_five_score_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = tmp_path / "autoslice"
    monkeypatch.setattr(runner, "BASE", base)
    segment, scene = _scene(
        tmp_path,
        clock="21-01-30",
        width=1920,
        height=1080,
        title="生日回 3D直播",
    )
    delivered = [
        {
            **_candidate(f"delivered-{index}", segment=segment, scene=scene),
            "status": "review_ready",
        }
        for index in range(5)
    ]
    at_gate = _candidate("event-score-85", segment=segment, scene=scene, score=85.0)
    below_gate = _candidate(
        "event-score-84.99", segment=segment, scene=scene, score=84.99
    )
    state = _state(picks=delivered, pending=[below_gate, at_gate])

    runner.prioritize(state)

    assert [row["cid"] for row in state["pending_talk"]] == ["event-score-85"]
    assert [row["cid"] for row in state["talk_backlog"]] == ["event-score-84.99"]


def test_event_scope_stops_at_fifteen_total_slots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = tmp_path / "autoslice"
    monkeypatch.setattr(runner, "BASE", base)
    segment, scene = _scene(
        tmp_path,
        clock="21-31-30",
        width=1920,
        height=1080,
        title="周年纪念 3D Live",
    )
    delivered = [
        {
            **_candidate(f"delivered-{index}", segment=segment, scene=scene),
            "status": "review_ready",
        }
        for index in range(5)
    ]
    candidates = [
        _candidate(
            f"event-extra-{index:02d}",
            segment=segment,
            scene=scene,
            score=95.0 - index / 10,
        )
        for index in range(11)
    ]
    state = _state(picks=delivered, pending=candidates)

    runner.prioritize(state)

    assert len(state["pending_talk"]) == 10
    assert len(delivered) + len(state["pending_talk"]) == 15
    assert [row["cid"] for row in state["talk_backlog"]] == ["event-extra-10"]


def test_resolved_game_policy_has_priority_without_stacking(
    tmp_path: Path,
) -> None:
    segment, scene = _scene(
        tmp_path,
        clock="20-31-30",
        width=1920,
        height=1080,
        title="三周年 3D Live",
    )
    state_root = tmp_path / "state"
    context_path = state_root / "session_game_context" / "2026-08-08.json"
    context_path.parent.mkdir(parents=True)
    context_path.write_text(
        json.dumps(
            {
                "schema_version": "session-game-context.v1",
                "occurrence_policy": "GAME_TERM_EXISTS_NOT_CUE_OCCURRENCE_OR_MUTATION_AUTHORITY",
                "recording_date": "2026-08-08",
                "status": "RESOLVED",
                "glossary_sha256": "sha256:" + "a" * 64,
                "input_inventory_sha256": "synthetic",
                "qualifying_game_ids": ["fixture"],
                "game": {
                    "game_id": "fixture",
                    "canonical": "合成游戏",
                    "aliases": [],
                    "terms": [{"surface": "合成术语", "kind": "role"}],
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    policy = resolve_talk_quota_policy(
        _candidate("game-priority", segment=segment, scene=scene),
        state_root=state_root,
    )

    # 优先级不变：RESOLVED 游戏日的 event 形状段仍留在 GAME scope。数字则来自
    # 按日期授权：8/8 的条目 scope=event，绝不漏给 game scope；game scope 这天
    # 没有条目 → fail-closed 回落 5 席 / 无额外席，而不是继承事件 lane 的 15/85。
    assert (policy.kind, policy.cap, policy.extra_slot_min_score) == (
        "game",
        5,
        None,
    )
    assert policy.policy_source == "asset:default_policy"


def test_session_annotation_persists_scene_receipt_on_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    segment, scene = _scene(
        tmp_path,
        clock="20-01-30",
        width=1920,
        height=1080,
        title="三周年 3D Live",
    )
    monkeypatch.setattr(runner, "list_segments", lambda _date: [segment])
    monkeypatch.setattr(runner, "find_danmaku_xml", lambda _segment: None)
    monkeypatch.setattr(
        session_discovery,
        "resolve_segment_scene_context",
        lambda *_args, **_kwargs: scene,
    )
    state = {
        "pending_talk": [{"cid": "fixture", "segment_path": str(segment)}],
        "picks": [{"candidate_id": "done", "segment": segment.name}],
    }

    assert runner.annotate_state_sessions("2026-08-08", state) is True
    assert state["segment_scene_contexts"][segment.stem] == scene
    assert state["pending_talk"][0]["segment_scene_context"] == scene
    assert state["picks"][0]["segment_scene_context"] == scene


def test_failed_pick_requeue_preserves_scene_receipt_and_quota_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    date = "2026-08-08"
    base = tmp_path / "autoslice"
    rec_root = tmp_path / "recordings"
    date_dir = rec_root / date
    date_dir.mkdir(parents=True)
    event_segment, event_scene = _scene(
        date_dir,
        clock="21-01-31",
        width=1920,
        height=1080,
        title="三周年 3D Live",
    )
    talk_segment, talk_scene = _scene(
        date_dir,
        clock="23-01-25",
        width=1080,
        height=1920,
        title="深夜杂谈",
    )
    monkeypatch.setattr(runner, "BASE", base)
    monkeypatch.setattr(runner, "REC_ROOT", rec_root)
    monkeypatch.setattr(
        runner, "talk_pipeline_fingerprint", lambda _cid: "sha256:new"
    )
    monkeypatch.setattr(
        runner,
        "talk_failure_recovery_fingerprint",
        lambda _kind, _cid: "sha256:new-recovery",
    )
    monkeypatch.setattr(runner, "ffprobe_ms", lambda _path: 900_000)
    monkeypatch.setattr(runner, "find_danmaku_xml", lambda _path: None)
    monkeypatch.setattr(runner, "find_chat_jsonl", lambda _path: None)

    def failed(
        candidate_id: str, segment: Path, scene: dict[str, object]
    ) -> dict[str, object]:
        return {
            "candidate_id": candidate_id,
            "segment": segment.name,
            "segment_scene_context": scene,
            "session_id": COLLAPSED_SESSION,
            "start_ms": 100_000,
            "end_ms": 200_000,
            "status": "failed",
            "failure_kind": "runtime_prerequisite",
            "failure_recoverable": True,
            "failure_recovery_fingerprint": "sha256:old-recovery",
            "pipeline_fingerprint": "sha256:old",
            "hook": candidate_id,
            "confidence": 0.99,
            "selection_scorecard": _scorecard(90.0),
        }

    state = {
        "pending_talk": [],
        "picks": [
            failed("event-retry", event_segment, event_scene),
            failed("talk-retry", talk_segment, talk_scene),
        ],
    }
    expected_scene_bytes = {
        "event-retry": json.dumps(
            event_scene, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode(),
        "talk-retry": json.dumps(
            talk_scene, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode(),
    }

    assert runner.requeue_recoverable_talks(date, state) == 2

    pending = {row["cid"]: row for row in state["pending_talk"]}
    policies = {
        candidate_id: resolve_talk_quota_policy(row, state_root=base / "state")
        for candidate_id, row in pending.items()
    }
    for candidate_id, row in pending.items():
        assert (
            json.dumps(
                row["segment_scene_context"],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            == expected_scene_bytes[candidate_id]
        )
    assert (
        policies["event-retry"].kind,
        policies["event-retry"].cap,
        policies["event-retry"].extra_slot_min_score,
        policies["event-retry"].policy_source,
    ) == ("event", 15, 85.0, "asset:2026-08-08-event-3dlive")
    assert (
        policies["talk-retry"].kind,
        policies["talk-retry"].cap,
        policies["talk-retry"].extra_slot_min_score,
        policies["talk-retry"].policy_source,
    ) == ("talk", 5, None, "asset:default_policy")


def test_talk_spec_persists_hash_bound_segment_speaker_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    date = "2026-08-08"
    base = tmp_path / "autoslice"
    repo = tmp_path / "repo"
    (base / "logs").mkdir(parents=True)
    repo.mkdir()
    segment, scene = _scene(
        tmp_path,
        clock="23-01-25",
        width=1080,
        height=1920,
        title="深夜杂谈",
    )
    monkeypatch.setattr(runner, "BASE", base)
    monkeypatch.setattr(runner, "REPO_ROOT", repo)
    monkeypatch.setattr(runner, "child_env_for_date", lambda _date: {})
    monkeypatch.setattr(
        runner, "talk_pipeline_fingerprint", lambda _cid: "sha256:synthetic"
    )

    class Completed:
        returncode = 0

    def fake_run(_command, **kwargs):
        kwargs["stdout"].write('{"red_flags": [], "boundary_repairs": []}\n')
        kwargs["stdout"].flush()
        return Completed()

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    candidate_id = "auto_230125_1157_1229"
    result = runner.produce_talk(
        date,
        _candidate(
            candidate_id,
            segment=segment,
            scene=scene,
            score=90.2,
            hook="竖屏单人杂谈",
        )
        | {
            "start_ms": 1_157_000,
            "end_ms": 1_229_000,
            "seg_dur_ms": 1_800_000,
            "lane": "semantic_recall_sharded",
        },
    )

    spec = json.loads(
        (base / "out" / date / f"spec_{candidate_id}.json").read_text(
            encoding="utf-8"
        )
    )
    context_path = Path(spec["speaker_session_context"])
    assert context_path.is_file()
    assert spec["speaker_session_context_sha256"] == (
        "sha256:" + hashlib.sha256(context_path.read_bytes()).hexdigest()
    )
    context = json.loads(context_path.read_text(encoding="utf-8"))
    assert context["source_segment"] == segment.name
    assert context["segment_scene_context"]["media_probe"]["orientation"] == (
        "portrait"
    )
    assert context["source_piece_count"] == 1
    assert result["session_id"] == COLLAPSED_SESSION
    assert result["segment_scene_context"] == scene
