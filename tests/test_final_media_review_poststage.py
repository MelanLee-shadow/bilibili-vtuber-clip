from __future__ import annotations

from pathlib import Path

import pytest

from src.autoslice import final_media_review_poststage as poststage
from src.autoslice.final_media_review_poststage import (
    FinalMediaReviewPoststageError,
    consume_configured_final_media_review,
)


CANDIDATE = "auto_200000_1_2"


def _package(tmp_path: Path) -> tuple[Path, Path, Path]:
    root = (tmp_path / "package").resolve()
    verification = root / "verification"
    verification.mkdir(parents=True)
    job = verification / f"{CANDIDATE}.final-media-review-job.json"
    state = verification / f"{CANDIDATE}.final-media-review-state.json"
    return root, job, state


def test_no_job_is_a_true_noop(tmp_path: Path):
    root, _job, state = _package(tmp_path)

    result = consume_configured_final_media_review(
        root, CANDIDATE, runtime_root=tmp_path / "runtime"
    )

    assert result == {
        "status": "NOT_CONFIGURED",
        "candidate_id": CANDIDATE,
        "provider_calls": 0,
        "package_state_written": False,
    }
    assert not state.exists()
    assert list((root / "verification").iterdir()) == []


def test_orphan_state_is_rejected(tmp_path: Path):
    root, _job, state = _package(tmp_path)
    state.write_text("{}\n", encoding="utf-8")

    with pytest.raises(
        FinalMediaReviewPoststageError,
        match="FINAL_MEDIA_REVIEW_POSTSTAGE_ORPHAN_STATE",
    ):
        consume_configured_final_media_review(root, CANDIDATE)


def test_capability_wait_stops_before_binding_and_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root, job, state = _package(tmp_path)
    job.write_text("{}\n", encoding="utf-8")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setattr(
        poststage,
        "resolve_or_materialize_review_job",
        lambda *_args, **_kwargs: {
            "active_job_path": str(job),
            "status": "INPUTS_ALREADY_COMPLETE",
        },
    )
    monkeypatch.setattr(
        poststage,
        "ensure_runtime_raw_av_capability",
        lambda *_args, **_kwargs: {
            "status": "RETRY_WAIT",
            "provider_calls": 1,
            "reason_code": "SENTINEL_PROVIDER_RATE_LIMITED",
        },
    )

    def unexpected(*_args, **_kwargs):
        raise AssertionError("binding/consumer must wait for capability")

    monkeypatch.setattr(
        poststage, "bind_review_job_to_runtime_capability", unexpected
    )
    monkeypatch.setattr(poststage, "consume_review_job", unexpected)

    result = consume_configured_final_media_review(
        root, CANDIDATE, runtime_root=runtime
    )

    assert result["status"] == "CAPABILITY_WAIT"
    assert result["provider_calls"] == 1
    assert result["package_state_written"] is False
    assert result["capability_binding"] is None
    assert not state.exists()


def test_capability_stopped_preserves_no_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root, job, state = _package(tmp_path)
    job.write_text("{}\n", encoding="utf-8")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setattr(
        poststage,
        "resolve_or_materialize_review_job",
        lambda *_args, **_kwargs: {
            "active_job_path": str(job),
            "status": "INPUTS_ALREADY_COMPLETE",
        },
    )
    monkeypatch.setattr(
        poststage,
        "ensure_runtime_raw_av_capability",
        lambda *_args, **_kwargs: {
            "status": "DISPATCH_AMBIGUOUS",
            "provider_calls": 0,
            "reason_code": "SENTINEL_DISPATCH_OUTCOME_UNKNOWN",
        },
    )

    result = consume_configured_final_media_review(
        root, CANDIDATE, runtime_root=runtime
    )

    assert result["status"] == "CAPABILITY_STOPPED"
    assert result["provider_calls"] is None
    assert result["package_state_written"] is False
    assert not state.exists()


def test_absent_sentinel_runtime_stops_before_binding_and_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root, job, state = _package(tmp_path)
    job.write_text("{}\n", encoding="utf-8")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setattr(
        poststage,
        "resolve_or_materialize_review_job",
        lambda *_args, **_kwargs: {
            "active_job_path": str(job),
            "status": "INPUTS_ALREADY_COMPLETE",
        },
    )
    monkeypatch.setattr(
        poststage,
        "ensure_runtime_raw_av_capability",
        lambda *_args, **_kwargs: {
            "status": "SENTINEL_RUNTIME_ABSENT",
            "runner_called": False,
            "provider_calls": 0,
        },
    )

    def unexpected(*_args, **_kwargs):
        raise AssertionError("binding/consumer must stop when sentinel runtime is absent")

    monkeypatch.setattr(
        poststage, "bind_review_job_to_runtime_capability", unexpected
    )
    monkeypatch.setattr(poststage, "consume_review_job", unexpected)

    result = consume_configured_final_media_review(
        root, CANDIDATE, runtime_root=runtime
    )

    assert result["status"] == "CAPABILITY_STOPPED"
    assert result["provider_calls"] == 0
    assert result["package_state_written"] is False
    assert result["capability_binding"] is None
    assert not state.exists()


def test_ambiguous_previous_dispatch_preserves_unknown_provider_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root, job, state = _package(tmp_path)
    job.write_text("{}\n", encoding="utf-8")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setattr(
        poststage,
        "resolve_or_materialize_review_job",
        lambda *_args, **_kwargs: {
            "active_job_path": str(job),
            "status": "INPUTS_ALREADY_COMPLETE",
        },
    )
    monkeypatch.setattr(
        poststage,
        "ensure_runtime_raw_av_capability",
        lambda *_args, **_kwargs: {
            "status": "RAW_AV_CAPABILITY_PRESENT",
            "provider_calls": 0,
        },
    )
    monkeypatch.setattr(
        poststage,
        "bind_review_job_to_runtime_capability",
        lambda *_args, **_kwargs: {
            "status": "BOUND_RUNTIME_RAW_AV_CAPABILITY",
            "active_job_path": str(job),
        },
    )

    def ambiguous(*_args, **_kwargs):
        state.write_text("{}\n", encoding="utf-8")
        return {
            "state": {
                "status": "DISPATCHING",
                "assessment": {"content_review_status": "UNASSESSED"},
            },
            "cache_reused": True,
            "provider_called": None,
            "provider_call_status": "AMBIGUOUS_PREVIOUS_DISPATCH",
        }

    monkeypatch.setattr(poststage, "consume_review_job", ambiguous)
    result = consume_configured_final_media_review(
        root, CANDIDATE, runtime_root=runtime
    )

    assert result["status"] == "CONSUMED"
    assert result["consumer_state_status"] == "DISPATCHING"
    assert result["provider_calls"] is None
    assert result["consumer"]["provider_call_status"] == (
        "AMBIGUOUS_PREVIOUS_DISPATCH"
    )
    assert result["package_state_written"] is True


def test_ready_capability_binds_and_consumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root, job, state = _package(tmp_path)
    job.write_text("{}\n", encoding="utf-8")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    calls: list[str] = []
    monkeypatch.setattr(
        poststage,
        "resolve_or_materialize_review_job",
        lambda *_args, **_kwargs: {
            "active_job_path": str(job),
            "status": "INPUTS_ALREADY_COMPLETE",
        },
    )
    monkeypatch.setattr(
        poststage,
        "ensure_runtime_raw_av_capability",
        lambda *_args, **_kwargs: {
            "status": "RAW_AV_CAPABILITY_PRESENT",
            "provider_calls": 0,
        },
    )

    def bind(*_args, **_kwargs):
        calls.append("bind")
        return {
            "status": "BOUND_RUNTIME_RAW_AV_CAPABILITY",
            "active_job_path": str(job),
        }

    def consume(*_args, **_kwargs):
        calls.append("consume")
        state.write_text("{}\n", encoding="utf-8")
        return {
            "state": {
                "status": "COMPLETE",
                "assessment": {"content_review_status": "UNASSESSED"},
                "result": {"content_review_status": "PASS"},
            },
            "cache_reused": False,
            "provider_called": True,
            "provider_call_status": "RESPONSE_RECEIVED",
        }

    monkeypatch.setattr(
        poststage, "bind_review_job_to_runtime_capability", bind
    )
    monkeypatch.setattr(poststage, "consume_review_job", consume)

    result = consume_configured_final_media_review(
        root, CANDIDATE, runtime_root=runtime
    )

    assert calls == ["bind", "consume"]
    assert result["status"] == "CONSUMED"
    assert result["consumer_state_status"] == "COMPLETE"
    assert result["consumer_content_review_status"] == "PASS"
    assert result["provider_calls"] == 1
    assert result["package_state_written"] is True


def test_job_symlink_is_rejected(tmp_path: Path):
    root, job, _state = _package(tmp_path)
    outside = tmp_path / "outside.json"
    outside.write_text("{}\n", encoding="utf-8")
    job.symlink_to(outside)

    with pytest.raises(
        FinalMediaReviewPoststageError,
        match="FINAL_MEDIA_REVIEW_POSTSTAGE_PATH_INVALID",
    ):
        consume_configured_final_media_review(root, CANDIDATE)


def test_production_wrapper_preserves_noop_when_admission_is_not_required(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    root, _job, state = _package(tmp_path)
    monkeypatch.setattr(
        poststage,
        "bootstrap_production_final_media_review_job",
        lambda _root, _candidate: {
            "status": "NOT_REQUIRED",
            "reason_code": "NO_PROJECTED_DURATION_BINDING",
            "provider_calls": 0,
            "created": False,
            "job_path": None,
        },
    )

    result = poststage.bootstrap_and_consume_production_final_media_review(
        root, CANDIDATE, runtime_root=tmp_path / "runtime"
    )

    assert result["status"] == "NOT_CONFIGURED"
    assert result["job_bootstrap"]["reason_code"] == (
        "NO_PROJECTED_DURATION_BINDING"
    )
    assert result["provider_calls"] == 0
    assert result["package_state_written"] is False
    assert not state.exists()


def test_production_wrapper_bootstraps_before_normal_consumer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    root, job, _state = _package(tmp_path)
    calls: list[str] = []

    def bootstrap(_root: Path, _candidate: str) -> dict[str, object]:
        calls.append("bootstrap")
        job.write_text("{}\n", encoding="utf-8")
        return {
            "status": "BOOTSTRAPPED_FINAL_MEDIA_REVIEW_JOB",
            "created": True,
            "job_path": str(job),
            "provider_calls": 0,
        }

    def consume(*_args, **_kwargs) -> dict[str, object]:
        calls.append("consume")
        return {
            "status": "CONSUMED",
            "candidate_id": CANDIDATE,
            "provider_calls": 0,
            "package_state_written": True,
        }

    monkeypatch.setattr(
        poststage, "bootstrap_production_final_media_review_job", bootstrap
    )
    monkeypatch.setattr(
        poststage, "consume_configured_final_media_review", consume
    )

    result = poststage.bootstrap_and_consume_production_final_media_review(
        root, CANDIDATE, runtime_root=tmp_path / "runtime"
    )

    assert calls == ["bootstrap", "consume"]
    assert result["status"] == "CONSUMED"
    assert result["job_bootstrap"]["created"] is True


def test_production_wrapper_converts_admission_error_to_poststage_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    root, _job, _state = _package(tmp_path)

    def reject(_root: Path, _candidate: str) -> dict[str, object]:
        from src.autoslice.production_final_media_review_admission import (
            ProductionFinalMediaReviewAdmissionError,
        )

        raise ProductionFinalMediaReviewAdmissionError(
            "FINAL_MEDIA_REVIEW_PRODUCTION_DURATION_BINDING_MISMATCH",
            "synthetic drift",
        )

    monkeypatch.setattr(
        poststage, "bootstrap_production_final_media_review_job", reject
    )

    with pytest.raises(
        FinalMediaReviewPoststageError,
        match="FINAL_MEDIA_REVIEW_PRODUCTION_DURATION_BINDING_MISMATCH",
    ):
        poststage.bootstrap_and_consume_production_final_media_review(
            root, CANDIDATE
        )


def test_current_contract_refresh_precedes_capability_binding_and_consumer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    root, job, state = _package(tmp_path)
    job.write_text('{"stale":true}\n', encoding="utf-8")
    refreshed = (
        root
        / "verification"
        / "final-media-review-contracts"
        / CANDIDATE
        / "refresh"
        / "review-job.successor.json"
    )
    refreshed.parent.mkdir(parents=True)
    refreshed.write_text('{"current":true}\n', encoding="utf-8")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    calls: list[str] = []
    points = [
        {
            "point_id": "final-clock",
            "final_video_start_ms": 250,
            "final_video_end_ms": 1_750,
            "expectation": "current final-video interval",
        }
    ]
    monkeypatch.setattr(
        poststage,
        "resolve_or_materialize_review_job",
        lambda *_args, **_kwargs: {
            "active_job_path": str(job),
            "status": "INPUTS_ALREADY_COMPLETE",
        },
    )
    monkeypatch.setattr(
        poststage,
        "_current_review_contract",
        lambda candidate_id: (
            "sha256:" + "2" * 64,
            points,
        )
        if candidate_id == CANDIDATE
        else None,
    )

    def refresh(source_job, **kwargs):
        calls.append("refresh")
        assert Path(source_job) == job
        assert Path(kwargs["allowed_root"]) == root
        assert kwargs["review_contract_sha256"] == "sha256:" + "2" * 64
        assert kwargs["review_points"] == points
        return {
            "status": "REFRESHED_REVIEW_CONTRACT",
            "active_job_path": str(refreshed),
            "receipt_path": str(refreshed.with_name("contract-refresh-receipt.json")),
            "provider_calls": 0,
        }

    monkeypatch.setattr(poststage, "refresh_review_job_contract", refresh)
    monkeypatch.setattr(
        poststage,
        "ensure_runtime_raw_av_capability",
        lambda *_args, **_kwargs: {
            "status": "RAW_AV_CAPABILITY_PRESENT",
            "provider_calls": 0,
        },
    )

    def bind(active_job, **_kwargs):
        calls.append("bind")
        assert Path(active_job) == refreshed
        return {
            "status": "BOUND_RUNTIME_RAW_AV_CAPABILITY",
            "active_job_path": str(refreshed),
        }

    def consume(active_job, *_args, **_kwargs):
        calls.append("consume")
        assert Path(active_job) == refreshed
        state.write_text("{}\n", encoding="utf-8")
        return {
            "state": {
                "status": "COMPLETE",
                "assessment": {"content_review_status": "UNASSESSED"},
                "result": {"content_review_status": "PASS"},
            },
            "cache_reused": False,
            "provider_called": True,
            "provider_call_status": "RESPONSE_RECEIVED",
        }

    monkeypatch.setattr(poststage, "bind_review_job_to_runtime_capability", bind)
    monkeypatch.setattr(poststage, "consume_review_job", consume)

    result = consume_configured_final_media_review(
        root,
        CANDIDATE,
        runtime_root=runtime,
    )

    assert calls == ["refresh", "bind", "consume"]
    assert result["active_job_path"] == str(refreshed)
    assert result["contract_refresh"]["status"] == (
        "REFRESHED_REVIEW_CONTRACT"
    )
    assert result["consumer_content_review_status"] == "PASS"
