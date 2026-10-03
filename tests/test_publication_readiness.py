from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from src.autoslice import publication_readiness
from src.autoslice.final_media_review_inputs import (
    ASSESSMENT_SCHEMA_VERSION,
    JOB_SCHEMA_VERSION,
    RESULT_SCHEMA_VERSION,
    STATE_SCHEMA_VERSION,
    canonical_sha256,
)
from src.autoslice.publication_readiness import (
    CODE_DEFECT,
    NEEDS_REVIEWER_TRUTH,
    NEEDS_PROVIDER,
    READY_TO_PREPARE,
    READY_FOR_SERIAL_UPLOAD,
    STATE_DRIFT,
    build_readiness_graph,
)


def _sha(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _write(path: Path, payload: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return _sha(payload)


def _attested_record(path: Path) -> dict[str, object]:
    payload = path.read_bytes()
    return {
        "path": str(path),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "bytes": len(payload),
    }


def _registry(*entries: dict[str, object]) -> dict[str, object]:
    return {"schema_version": "publication-registry.v1", "entries": list(entries)}


def _package(runtime: Path, date: str, candidate: str, *, source_fact: bool = True, qc: bool = True, nested_burn: bool = False) -> tuple[Path, Path]:
    root = runtime / "out" / date / candidate / "replacement_recuts"
    main, srt, ass, burn, cover = (root / f"{candidate}.recut.mp4", root / f"{candidate}.recut.srt", root / f"{candidate}.recut.ass", root / f"{candidate}.burn.mp4", root / "covers" / f"{candidate}.cover.png")
    hashes = {"video_sha256": _write(main, b"video"), "subtitle_sha256": _write(srt, b"srt"), "ass_sha256": _write(ass, b"ass"), "burned_video_sha256": _write(burn, b"burn"), "cover_sha256": _write(cover, b"cover")}
    publish = root / f"{candidate}.publish.json"
    record = root / f"{candidate}.record.json"
    record_doc = {"candidate_id": candidate, "recording_date": date, "media_path": str(main), "subtitle_path": str(srt), "subtitle_ass_path": str(ass), "burned_video_path": str(burn), "artifact_hashes": hashes, "story_contract": {"source_fact_review": {"status": "PASS"} if source_fact else {}}}
    if nested_burn:
        record_doc.pop("burned_video_path")
        record_doc["burned_preview"] = {"burned_path": str(burn), "burned_sha256": hashes["burned_video_sha256"]}
    record.write_text(json.dumps(record_doc))
    title = f"【李豆沙】{candidate}"
    publish.write_text(json.dumps({"candidate_id": candidate, "title": title, "artifact_hashes": hashes, "cover_generation": {"final_cover": str(cover), "final_cover_sha256": hashes["cover_sha256"]}}))
    if qc:
        (root / f"{candidate}.title-cover-joint-qc.json").write_text(json.dumps({"schema_version": "lidousha-title-cover-joint-qc.v1", "candidate_id": candidate, "title": title, "title_sha256": _sha(title.encode()), "cover_path": str(cover), "cover_sha256": hashes["cover_sha256"], "preferred_provider": "cpa", "selected_provider": "cpa", "witness": {"schema_version": "cpa-frame-witness.v1", "provider": "cpa", "model": "fixture", "status": "OBSERVED", "image_path": str(cover), "image_sha256": hashes["cover_sha256"]}, "verdict": {"lidousha_primary": True, "thumbnail_readable": True, "single_clear_hook": True, "text_overcrowded": False, "title_cover_aligned": True, "physical_text_line_count": 1, "unrelated_or_misleading_elements": [], "pass": True}, "status": "PASS", "pass": True}))
    (root / f"{candidate}.review-manifest.json").write_text(json.dumps({"status": "PASS"}))
    (root / f"{candidate}.package-audit.json").write_text(json.dumps({"status": "PASS"}))
    return root, record


def _write_final_media_sidecars(
    root: Path,
    candidate: str,
    *,
    status: str | None,
    result_status: str | None = None,
) -> tuple[Path, Path | None]:
    verification = root / "verification"
    verification.mkdir(parents=True, exist_ok=True)
    job_path = verification / f"{candidate}.final-media-review-job.json"
    job = {
        "schema_version": JOB_SCHEMA_VERSION,
        "candidate_id": candidate,
        "requirements": {"content_review_required": True},
    }
    job_path.write_text(json.dumps(job), encoding="utf-8")
    if status is None:
        return job_path, None

    record = json.loads((root / f"{candidate}.record.json").read_text())
    burned = Path(record["burned_video_path"])
    video_sha256 = hashlib.sha256(burned.read_bytes()).hexdigest()
    asset_manifest_sha256 = "d" * 64
    assessment = {
        "schema_version": ASSESSMENT_SCHEMA_VERSION,
        "candidate_id": candidate,
        "content_review_status": "UNASSESSED",
        "asset_manifest": {"sha256": asset_manifest_sha256},
        "exact_media_clock": {"source_video_sha256": video_sha256},
    }
    result = None
    if result_status is not None:
        result = {
            "schema_version": RESULT_SCHEMA_VERSION,
            "candidate_id": candidate,
            "source_video_sha256": video_sha256,
            "asset_manifest_sha256": asset_manifest_sha256,
            "status": result_status,
            "content_review_status": result_status,
            "observations": ["synthetic exact final-media result"],
        }
    state = {
        "schema_version": STATE_SCHEMA_VERSION,
        "candidate_id": candidate,
        "binding_sha256": "a" * 64,
        "job_path": str(job_path.resolve()),
        "job_sha256": hashlib.sha256(job_path.read_bytes()).hexdigest(),
        "source_video_sha256": video_sha256,
        "asset_manifest_sha256": asset_manifest_sha256,
        "content_review_required": True,
        "attempt_count": 1 if result is not None else 0,
        "updated_at": "2026-09-30T12:00:00Z",
        "next_attempt_at": None,
        "assessment": assessment,
        "runtime_binding": None,
        "provider_diagnostics": {},
        "result": result,
        "reason_codes": (
            []
            if result_status == "PASS"
            else ["FINAL_MEDIA_REVIEW_INPUT_OR_CONTENT_BLOCKED"]
        ),
        "status": status,
    }
    state["state_sha256"] = canonical_sha256(state)
    state_path = verification / f"{candidate}.final-media-review-state.json"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    return job_path, state_path


def _runtime(tmp_path: Path) -> tuple[Path, Path]:
    repo, runtime = tmp_path / "repo", tmp_path / "runtime"
    (repo / "assets/lidousha").mkdir(parents=True)
    (runtime / "state").mkdir(parents=True)
    (runtime / "reports").mkdir()
    (runtime / "reports/upload_ledger.jsonl").write_text("")
    return repo, runtime


def _rows(graph: dict[str, object]) -> dict[str, dict[str, object]]:
    return {str(row["candidate_id"]): row for row in graph["rows"]}  # type: ignore[index]


def test_production_shape_talk_song_hold_and_published_are_independent(tmp_path: Path) -> None:
    repo, runtime = _runtime(tmp_path)
    _package(runtime, "2026-08-20", "talk")
    _package(runtime, "2026-08-20", "song")
    (runtime / "state/2026-08-20.json").write_text(json.dumps({"picks": [{"candidate_id": "talk", "status": "review_ready", "rc": 0, "bundle_lifecycle": "CURRENT", "bundle_compliance": "COMPLIANT"}], "songs": [{"candidate_id": "song", "status": "review_ready", "rc": 0}]}))
    graph = build_readiness_graph(repository_root=repo, runtime_root=runtime, registry_loader=lambda *_a, **_k: _registry({"candidate_id": "held", "recording_date": "2026-08-21", "status": "hold_pending_review"}, {"candidate_id": "public", "recording_date": "2026-08-18", "status": "published"}))
    rows = _rows(graph)
    assert rows["talk"]["category"] == READY_TO_PREPARE
    assert rows["song"]["category"] == READY_TO_PREPARE
    assert rows["talk"]["lane"] == "talk"
    assert rows["talk"]["source_collection"] == "picks"
    assert rows["song"]["lane"] == "song"
    assert rows["song"]["source_collection"] == "songs"
    assert rows["held"]["category"] == NEEDS_REVIEWER_TRUTH
    assert graph["excluded_published"] == [{"candidate_id": "public", "recording_date": "2026-08-18", "reason_code": "PUBLISHED_EXCLUDED"}]


def test_pending_and_backlog_collections_are_read_only_preparation_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, runtime = _runtime(tmp_path)
    state = {
        "pending_talk": [{"cid": "talk-ready"}],
        "talk_backlog": [
            {"cid": "talk-provider", "failure_stage": "source_fact_provider"},
            {"candidate_id": "talk-truth", "title_authority_error": "维护者 title needed"},
        ],
        "pending_song": [{"cid": "song-ready"}],
        "song_backlog": [{"candidate_id": "song-provider", "failure_stage": "cover_provider"}],
        "song_selection_backlog": [{"cid": "song-truth", "rejection_reason": "human review required"}],
        "picks": [],
        "songs": [],
    }
    (runtime / "state/2026-08-20.json").write_text(json.dumps(state))
    from src.autoslice import publication_readiness

    monkeypatch.setattr(
        publication_readiness,
        "_inspect_package",
        lambda *_args: (_ for _ in ()).throw(AssertionError("unpackaged candidate inspected")),
    )
    rows = _rows(
        build_readiness_graph(
            repository_root=repo,
            runtime_root=runtime,
            registry_loader=lambda *_a, **_k: _registry(),
        )
    )
    assert rows["talk-ready"]["category"] == READY_TO_PREPARE
    assert rows["talk-provider"]["category"] == NEEDS_PROVIDER
    assert rows["talk-truth"]["category"] == NEEDS_REVIEWER_TRUTH
    assert rows["song-ready"]["category"] == READY_TO_PREPARE
    assert rows["song-provider"]["category"] == NEEDS_PROVIDER
    assert rows["song-truth"]["category"] == NEEDS_REVIEWER_TRUTH
    assert rows["talk-ready"]["lane"] == "talk"
    assert rows["talk-ready"]["source_collection"] == "pending_talk"
    assert rows["song-ready"]["lane"] == "song"
    assert rows["song-ready"]["source_collection"] == "pending_song"
    assert all("PACKAGE_ROOT_MISSING" not in row["reason_codes"] for row in rows.values())


def test_scoped_graph_does_not_inspect_unrelated_dates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, runtime = _runtime(tmp_path)
    (runtime / "state/2026-08-14.json").write_text(json.dumps({
        "picks": [{"candidate_id": "wanted", "status": "review_ready", "rc": 0}],
    }))
    (runtime / "state/2026-08-15.json").write_text(json.dumps({
        "picks": [{"candidate_id": "unrelated", "status": "review_ready", "rc": 0}],
    }))
    from src.autoslice import publication_readiness

    inspected: list[tuple[str, str]] = []
    original = publication_readiness._inspect_package

    def inspect(root, candidate_id, date):
        inspected.append((candidate_id, date))
        assert (candidate_id, date) == ("wanted", "2026-08-14")
        return original(root, candidate_id, date)

    monkeypatch.setattr(publication_readiness, "_inspect_package", inspect)
    graph = build_readiness_graph(
        repository_root=repo, runtime_root=runtime,
        recording_dates=frozenset({"2026-08-14"}), candidate_ids=frozenset({"wanted"}),
        registry_loader=lambda *_a, **_k: _registry(),
    )
    assert set(_rows(graph)) == {"wanted"}
    assert inspected == [("wanted", "2026-08-14")]


def test_consistent_candidate_sources_merge_but_conflicts_are_state_drift(tmp_path: Path) -> None:
    repo, runtime = _runtime(tmp_path)
    state = {
        "pending_talk": [{"cid": "merged", "status": "queued"}, {"cid": "conflict", "opaque": "one"}],
        "talk_backlog": [
            {"candidate_id": "merged", "status": "queued"},
            {"candidate_id": "conflict", "opaque": "two"},
        ],
    }
    (runtime / "state/2026-08-20.json").write_text(json.dumps(state))
    rows = _rows(
        build_readiness_graph(
            repository_root=repo,
            runtime_root=runtime,
            registry_loader=lambda *_a, **_k: _registry(),
        )
    )
    assert rows["merged"]["category"] == READY_TO_PREPARE
    assert rows["merged"]["source_collections"] == ["pending_talk", "talk_backlog"]
    assert len(rows["merged"]["state_sources"]) == 2
    assert rows["conflict"]["category"] == STATE_DRIFT
    assert "STATE_CANDIDATE_CONFLICT" in rows["conflict"]["reason_codes"]


def test_invalid_active_collection_is_a_fail_closed_graph_blocker(tmp_path: Path) -> None:
    repo, runtime = _runtime(tmp_path)
    (runtime / "state/2026-08-20.json").write_text(json.dumps({"pending_song": {"cid": "bad"}}))
    graph = build_readiness_graph(
        repository_root=repo,
        runtime_root=runtime,
        registry_loader=lambda *_a, **_k: _registry(),
    )
    assert {item["code"] for item in graph["graph_blockers"]} == {"STATE_COLLECTION_INVALID"}


def test_rejected_or_historical_collections_do_not_expand_publish_candidates(tmp_path: Path) -> None:
    repo, runtime = _runtime(tmp_path)
    (runtime / "state/2026-08-20.json").write_text(
        json.dumps(
            {
                "talk_below_confidence_threshold": [{"candidate_id": "rejected"}],
                "talk_superseded_attempts": [{"candidate_id": "historical"}],
            }
        )
    )
    graph = build_readiness_graph(
        repository_root=repo,
        runtime_root=runtime,
        registry_loader=lambda *_a, **_k: _registry(),
    )
    assert graph["rows"] == []


def test_daily_final_media_job_without_state_blocks_readiness(
    tmp_path: Path,
) -> None:
    repo, runtime = _runtime(tmp_path)
    root, _record = _package(runtime, "2026-08-20", "media-pending")
    job, _state = _write_final_media_sidecars(
        root, "media-pending", status=None
    )
    (runtime / "state/2026-08-20.json").write_text(
        json.dumps(
            {
                "picks": [
                    {
                        "candidate_id": "media-pending",
                        "status": "review_ready",
                        "rc": 0,
                    }
                ]
            }
        )
    )

    row = _rows(
        build_readiness_graph(
            repository_root=repo,
            runtime_root=runtime,
            registry_loader=lambda *_a, **_k: _registry(),
        )
    )["media-pending"]

    assert row["category"] == NEEDS_PROVIDER
    assert "FINAL_MEDIA_REVIEW_UNRESOLVED" in row["reason_codes"]
    assert row["package_dependencies"]["final_media_review_job"] == str(job)
    assert "final_media_review_state" not in row["package_dependencies"]


@pytest.mark.parametrize(
    "status",
    ["BLOCKED_INPUT", "WAITING_CAPABILITY", "RETRY_WAIT", "FAILED"],
)
def test_daily_unresolved_final_media_state_blocks_readiness(
    tmp_path: Path,
    status: str,
) -> None:
    repo, runtime = _runtime(tmp_path)
    root, _record = _package(runtime, "2026-08-20", "media-unresolved")
    _write_final_media_sidecars(root, "media-unresolved", status=status)
    (runtime / "state/2026-08-20.json").write_text(
        json.dumps(
            {
                "picks": [
                    {
                        "candidate_id": "media-unresolved",
                        "status": "review_ready",
                        "rc": 0,
                    }
                ]
            }
        )
    )

    row = _rows(
        build_readiness_graph(
            repository_root=repo,
            runtime_root=runtime,
            registry_loader=lambda *_a, **_k: _registry(),
        )
    )["media-unresolved"]

    assert row["category"] == NEEDS_PROVIDER
    assert "FINAL_MEDIA_REVIEW_UNRESOLVED" in row["reason_codes"]


def test_daily_final_media_block_is_not_ready_to_prepare(
    tmp_path: Path,
) -> None:
    repo, runtime = _runtime(tmp_path)
    root, _record = _package(runtime, "2026-08-20", "media-block")
    _write_final_media_sidecars(
        root,
        "media-block",
        status="COMPLETE",
        result_status="BLOCK",
    )
    (runtime / "state/2026-08-20.json").write_text(
        json.dumps(
            {
                "picks": [
                    {
                        "candidate_id": "media-block",
                        "status": "review_ready",
                        "rc": 0,
                    }
                ]
            }
        )
    )

    row = _rows(
        build_readiness_graph(
            repository_root=repo,
            runtime_root=runtime,
            registry_loader=lambda *_a, **_k: _registry(),
        )
    )["media-block"]

    assert row["category"] == STATE_DRIFT
    assert "FINAL_MEDIA_REVIEW_CONTENT_BLOCKED" in row["reason_codes"]


def test_daily_final_media_pass_preserves_normal_readiness(
    tmp_path: Path,
) -> None:
    repo, runtime = _runtime(tmp_path)
    root, _record = _package(runtime, "2026-08-20", "media-pass")
    job, state = _write_final_media_sidecars(
        root,
        "media-pass",
        status="COMPLETE",
        result_status="PASS",
    )
    assert state is not None
    (runtime / "state/2026-08-20.json").write_text(
        json.dumps(
            {
                "picks": [
                    {
                        "candidate_id": "media-pass",
                        "status": "review_ready",
                        "rc": 0,
                    }
                ]
            }
        )
    )

    row = _rows(
        build_readiness_graph(
            repository_root=repo,
            runtime_root=runtime,
            registry_loader=lambda *_a, **_k: _registry(),
        )
    )["media-pass"]

    assert row["category"] == READY_TO_PREPARE
    assert row["package_dependencies"]["final_media_review_job"] == str(job)
    assert row["package_dependencies"]["final_media_review_state"] == str(state)
    assert row["package_dependencies"]["final_media_review_active_job"] == str(job)


def test_daily_final_media_ignores_historical_sidecar_preimages(
    tmp_path: Path,
) -> None:
    repo, runtime = _runtime(tmp_path)
    root, _record = _package(runtime, "2026-08-20", "media-history")
    _write_final_media_sidecars(
        root,
        "media-history",
        status="COMPLETE",
        result_status="PASS",
    )
    history = root / "history/e570/verification"
    history.mkdir(parents=True)
    (history / "media-history.final-media-review-job.json").write_text("{}")
    (history / "media-history.final-media-review-state.json").write_text("{}")
    (runtime / "state/2026-08-20.json").write_text(
        json.dumps(
            {
                "picks": [
                    {
                        "candidate_id": "media-history",
                        "status": "review_ready",
                        "rc": 0,
                    }
                ]
            }
        )
    )

    row = _rows(
        build_readiness_graph(
            repository_root=repo,
            runtime_root=runtime,
            registry_loader=lambda *_a, **_k: _registry(),
        )
    )["media-history"]

    assert row["category"] == READY_TO_PREPARE
    assert not set(row["reason_codes"]) & {
        "FINAL_MEDIA_REVIEW_STATE_INVALID",
        "FINAL_MEDIA_REVIEW_UNRESOLVED",
    }


def test_daily_final_media_job_binding_drift_is_state_drift(
    tmp_path: Path,
) -> None:
    repo, runtime = _runtime(tmp_path)
    root, _record = _package(runtime, "2026-08-20", "media-drift")
    job, _state = _write_final_media_sidecars(
        root,
        "media-drift",
        status="COMPLETE",
        result_status="PASS",
    )
    job.write_text('{"changed":true}', encoding="utf-8")
    (runtime / "state/2026-08-20.json").write_text(
        json.dumps(
            {
                "picks": [
                    {
                        "candidate_id": "media-drift",
                        "status": "review_ready",
                        "rc": 0,
                    }
                ]
            }
        )
    )

    row = _rows(
        build_readiness_graph(
            repository_root=repo,
            runtime_root=runtime,
            registry_loader=lambda *_a, **_k: _registry(),
        )
    )["media-drift"]

    assert row["category"] == STATE_DRIFT
    assert "FINAL_MEDIA_REVIEW_STATE_INVALID" in row["reason_codes"]


def test_unresolved_final_media_sidecar_prevents_serial_upload_readiness(
    tmp_path: Path,
) -> None:
    repo, runtime = _runtime(tmp_path)
    root, record = _package(runtime, "2026-08-20", "media-serial")
    (root / "media-serial.upload_manifest.json").write_text("{}")
    _write_final_media_sidecars(root, "media-serial", status=None)
    (runtime / "state/2026-08-20.json").write_text(
        json.dumps(
            {
                "picks": [
                    {
                        "candidate_id": "media-serial",
                        "status": "review_ready",
                        "rc": 0,
                    }
                ]
            }
        )
    )

    row = _rows(
        build_readiness_graph(
            repository_root=repo,
            runtime_root=runtime,
            registry_loader=lambda *_a, **_k: _registry(),
            manifest_loader=lambda *_a, **_k: (
                {
                    "candidate_id": "media-serial",
                    "recording_date": "2026-08-20",
                    "video": {"sha256": "sha256:video"},
                    "package_attestation": {
                        "record": _attested_record(record)
                    },
                },
                [],
            ),
            ledger_checker=lambda *_a: (None, None, []),
        )
    )["media-serial"]

    assert row["category"] == NEEDS_PROVIDER
    assert row["category"] != READY_FOR_SERIAL_UPLOAD
    assert "FINAL_MEDIA_REVIEW_UNRESOLVED" in row["reason_codes"]


def test_missing_truth_provider_and_typed_code_use_real_fields(tmp_path: Path) -> None:
    repo, runtime = _runtime(tmp_path)
    _package(runtime, "2026-08-20", "provider", source_fact=False)
    _package(runtime, "2026-08-20", "truth")
    _package(runtime, "2026-08-20", "defect")
    (runtime / "state/2026-08-20.json").write_text(json.dumps({"picks": [{"candidate_id": "provider", "status": "review_ready", "rc": 0}, {"candidate_id": "truth", "status": "review_ready", "rc": 0, "title_authority_error": "exact 维护者 title required"}, {"candidate_id": "defect", "status": "review_ready", "rc": 0, "failure_stage": "runtime_code_defect"}]}))
    rows = _rows(build_readiness_graph(repository_root=repo, runtime_root=runtime, registry_loader=lambda *_a, **_k: _registry()))
    assert rows["provider"]["category"] == NEEDS_PROVIDER
    assert rows["truth"]["category"] == NEEDS_REVIEWER_TRUTH
    assert rows["defect"]["category"] == CODE_DEFECT


def test_symlink_and_hash_drift_are_state_drift(tmp_path: Path) -> None:
    repo, runtime = _runtime(tmp_path)
    root, record = _package(runtime, "2026-08-20", "drift")
    record_doc = json.loads(record.read_text())
    Path(record_doc["subtitle_path"]).write_bytes(b"tampered")
    (runtime / "state/2026-08-20.json").write_text(json.dumps({"picks": [{"candidate_id": "drift", "status": "review_ready", "rc": 0}]}))
    rows = _rows(build_readiness_graph(repository_root=repo, runtime_root=runtime, registry_loader=lambda *_a, **_k: _registry()))
    assert rows["drift"]["category"] == STATE_DRIFT
    assert "PACKAGE_ARTIFACT_HASH_DRIFT" in rows["drift"]["reason_codes"]
    target = root / "elsewhere"
    target.write_bytes(b"x")
    Path(record_doc["subtitle_path"]).unlink()
    Path(record_doc["subtitle_path"]).symlink_to(target)
    rows = _rows(build_readiness_graph(repository_root=repo, runtime_root=runtime, registry_loader=lambda *_a, **_k: _registry()))
    assert rows["drift"]["category"] == STATE_DRIFT


def test_nested_burn_and_real_joint_qc_are_accepted_but_escaped_paths_fail(tmp_path: Path) -> None:
    repo, runtime = _runtime(tmp_path)
    root, record = _package(runtime, "2026-08-20", "nested", nested_burn=True)
    (runtime / "state/2026-08-20.json").write_text(json.dumps({"picks": [{"candidate_id": "nested", "status": "review_ready", "rc": 0}]}))
    rows = _rows(build_readiness_graph(repository_root=repo, runtime_root=runtime, registry_loader=lambda *_a, **_k: _registry()))
    assert rows["nested"]["category"] == READY_TO_PREPARE
    escaped = tmp_path / "outside.srt"
    escaped.write_bytes(b"srt")
    document = json.loads(record.read_text())
    document["subtitle_path"] = str(escaped)
    record.write_text(json.dumps(document))
    rows = _rows(build_readiness_graph(repository_root=repo, runtime_root=runtime, registry_loader=lambda *_a, **_k: _registry()))
    assert rows["nested"]["category"] == STATE_DRIFT


def _legacy_same_stem_review_package(
    tmp_path: Path,
) -> tuple[Path, Path, Path, Path, str]:
    root = tmp_path / "legacy-same-stem-package"
    root.mkdir()
    candidate_id = "legacy-candidate"
    title = "【李豆沙】legacy same-stem package"
    video = root / f"{candidate_id}.mp4"
    cover = root / f"{candidate_id}.cover.png"
    record = root / f"{candidate_id}.record.json"
    publish = root / f"{candidate_id}.publish.json"
    video.write_bytes(b"legacy-video")
    cover.write_bytes(b"legacy-cover")
    record.write_text("{}", encoding="utf-8")
    publish.write_text(
        json.dumps(
            {
                "candidate_id": candidate_id,
                "title": title,
                "cover_generation": {
                    "final_cover": str(cover),
                    "final_cover_sha256": hashlib.sha256(
                        cover.read_bytes()
                    ).hexdigest(),
                },
            }
        ),
        encoding="utf-8",
    )
    (root / "review_manifest.json").write_text(
        json.dumps(
            {
                "candidate_id": candidate_id,
                "items": [
                    {
                        "candidate_id": candidate_id,
                        "title": title,
                        "video": video.name,
                        "cover": cover.name,
                        "record": record.name,
                        "publish_json": publish.name,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return root, video, cover, record, title


def test_schema_less_same_stem_review_manifest_resolves_final_cover(
    tmp_path: Path,
) -> None:
    root, video, cover, record, title = _legacy_same_stem_review_package(
        tmp_path
    )
    publish = root / "legacy-candidate.publish.json"

    resolved = publication_readiness._final_review_cover(
        root,
        candidate_id="legacy-candidate",
        title=title,
        record_path=record,
        publish_path=publish,
        burned_path=str(video),
        generated_cover=cover,
    )

    assert resolved == cover.resolve()


@pytest.mark.parametrize(
    "mutation",
    [
        "unsupported_schema",
        "candidate_drift",
        "publish_binding_drift",
        "title_drift",
    ],
)
def test_schema_less_same_stem_review_manifest_remains_fail_closed(
    tmp_path: Path,
    mutation: str,
) -> None:
    root, video, cover, record, title = _legacy_same_stem_review_package(
        tmp_path
    )
    publish = root / "legacy-candidate.publish.json"
    manifest_path = root / "review_manifest.json"
    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    item = document["items"][0]
    if mutation == "unsupported_schema":
        document["schema_version"] = "unknown-review-schema.v1"
    elif mutation == "candidate_drift":
        item["candidate_id"] = "other-candidate"
    elif mutation == "publish_binding_drift":
        wrong = root / "wrong.publish.json"
        wrong.write_text("{}", encoding="utf-8")
        item["publish_json"] = wrong.name
    elif mutation == "title_drift":
        item["title"] = "【李豆沙】different title"
    manifest_path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ValueError):
        publication_readiness._final_review_cover(
            root,
            candidate_id="legacy-candidate",
            title=title,
            record_path=record,
            publish_path=publish,
            burned_path=str(video),
            generated_cover=cover,
        )


def test_canonical_record_requires_every_candidate_mirror_to_match(
    tmp_path: Path,
) -> None:
    repo, runtime = _runtime(tmp_path)
    root, record = _package(runtime, "2026-08-20", "mirrors")
    mirror = root / "mirrors.recut.burned.record.json"
    mirror.write_bytes(record.read_bytes())
    (runtime / "state/2026-08-20.json").write_text(
        json.dumps({"picks": [{"candidate_id": "mirrors", "status": "review_ready", "rc": 0}]})
    )
    rows = _rows(
        build_readiness_graph(
            repository_root=repo,
            runtime_root=runtime,
            registry_loader=lambda *_a, **_k: _registry(),
        )
    )
    assert rows["mirrors"]["category"] == READY_TO_PREPARE
    from src.autoslice.publication_readiness import _canonical_record_file

    assert _canonical_record_file(root, "mirrors") == record

    mirror.write_bytes(b'{"different":true}')
    row = _rows(
        build_readiness_graph(
            repository_root=repo,
            runtime_root=runtime,
            registry_loader=lambda *_a, **_k: _registry(),
        )
    )["mirrors"]
    assert row["category"] == STATE_DRIFT
    assert "PACKAGE_RECORD_MISSING_OR_AMBIGUOUS" in row["reason_codes"]
    mirror.write_bytes(record.read_bytes())

    legacy = runtime / "out/2026-08-20/legacy/replacement_recuts"
    legacy.mkdir(parents=True)
    (legacy / "left.record.json").write_bytes(record.read_bytes())
    (legacy / "right.record.json").write_bytes(b"{\"different\":true}")
    state = json.loads((runtime / "state/2026-08-20.json").read_text())
    state["picks"].append({"candidate_id": "legacy", "status": "review_ready", "rc": 0})
    (runtime / "state/2026-08-20.json").write_text(json.dumps(state))
    rows = _rows(
        build_readiness_graph(
            repository_root=repo,
            runtime_root=runtime,
            registry_loader=lambda *_a, **_k: _registry(),
        )
    )
    assert rows["legacy"]["category"] == STATE_DRIFT
    assert "PACKAGE_RECORD_MISSING_OR_AMBIGUOUS" in rows["legacy"]["reason_codes"]


def test_manifest_declared_delivery_record_is_not_a_candidate_record_mirror(tmp_path: Path) -> None:
    repo, runtime = _runtime(tmp_path)
    root, record = _package(runtime, "2026-08-20", "delivery-role")
    record_doc = json.loads(record.read_text())
    publish = root / "delivery-role.publish.json"
    record_doc["publish_staging"] = {"publish_json_path": str(publish)}
    record.write_text(json.dumps(record_doc))
    delivery_record = root / "delivery-role.recut.burned.record.json"
    delivery_doc = dict(record_doc)
    delivery_doc["delivery_only_evidence"] = "historical materialization receipt"
    delivery_record.write_text(json.dumps(delivery_doc))
    (root / "delivery-role.review-manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "lidousha-daily-review-manifest.v1",
                "candidate_id": "delivery-role",
                "items": [
                    {
                        "candidate_id": "delivery-role",
                        "evidence_json": record.name,
                        "record": delivery_record.name,
                        "publish_json": publish.name,
                        "sha256": {"evidence_json": hashlib.sha256(delivery_record.read_bytes()).hexdigest()},
                    }
                ]
            }
        )
    )
    (runtime / "state/2026-08-20.json").write_text(
        json.dumps({"picks": [{"candidate_id": "delivery-role", "status": "review_ready", "rc": 0}]})
    )
    row = _rows(
        build_readiness_graph(
            repository_root=repo,
            runtime_root=runtime,
            registry_loader=lambda *_a, **_k: _registry(),
        )
    )["delivery-role"]
    assert row["category"] == READY_TO_PREPARE

    manifest = root / "delivery-role.review-manifest.json"
    document = json.loads(manifest.read_text())
    document["schema_version"] = "untrusted-review-manifest.v1"
    manifest.write_text(json.dumps(document))
    row = _rows(
        build_readiness_graph(
            repository_root=repo,
            runtime_root=runtime,
            registry_loader=lambda *_a, **_k: _registry(),
        )
    )["delivery-role"]
    assert row["category"] == STATE_DRIFT
    assert "PACKAGE_RECORD_MISSING_OR_AMBIGUOUS" in row["reason_codes"]

    document["schema_version"] = "lidousha-daily-review-manifest.v1"
    document["items"][0]["sha256"]["evidence_json"] = "0" * 64
    manifest.write_text(json.dumps(document))
    row = _rows(
        build_readiness_graph(
            repository_root=repo,
            runtime_root=runtime,
            registry_loader=lambda *_a, **_k: _registry(),
        )
    )["delivery-role"]
    assert row["category"] == STATE_DRIFT
    assert "PACKAGE_RECORD_MISSING_OR_AMBIGUOUS" in row["reason_codes"]


def test_multiple_canonical_record_names_are_ambiguous_even_when_byte_identical(
    tmp_path: Path,
) -> None:
    repo, runtime = _runtime(tmp_path)
    root, record = _package(runtime, "2026-08-20", "duplicate")
    duplicate = root / "nested/duplicate.record.json"
    duplicate.parent.mkdir()
    duplicate.write_bytes(record.read_bytes())
    (runtime / "state/2026-08-20.json").write_text(
        json.dumps({"picks": [{"candidate_id": "duplicate", "status": "review_ready", "rc": 0}]})
    )
    row = _rows(
        build_readiness_graph(
            repository_root=repo,
            runtime_root=runtime,
            registry_loader=lambda *_a, **_k: _registry(),
        )
    )["duplicate"]
    assert row["category"] == STATE_DRIFT
    assert "PACKAGE_RECORD_MISSING_OR_AMBIGUOUS" in row["reason_codes"]


def test_parent_symlink_and_candidate_exception_are_local_drift(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, runtime = _runtime(tmp_path)
    root, _record = _package(runtime, "2026-08-20", "safe")
    (runtime / "state/2026-08-20.json").write_text(json.dumps({"picks": [{"candidate_id": "safe", "status": "review_ready", "rc": 0}]}))
    linked = runtime / "out" / "2026-08-20" / "linked"
    linked.symlink_to(root)
    state = json.loads((runtime / "state/2026-08-20.json").read_text())
    state["picks"].append({"candidate_id": "linked", "status": "review_ready", "rc": 0})
    (runtime / "state/2026-08-20.json").write_text(json.dumps(state))
    rows = _rows(build_readiness_graph(repository_root=repo, runtime_root=runtime, registry_loader=lambda *_a, **_k: _registry()))
    assert rows["linked"]["category"] == STATE_DRIFT
    from src.autoslice import publication_readiness

    original = publication_readiness._inspect_package
    monkeypatch.setattr(
        publication_readiness,
        "_inspect_package",
        lambda root, candidate, date: (_ for _ in ()).throw(RuntimeError("race"))
        if candidate == "safe"
        else original(root, candidate, date),
    )
    rows = _rows(build_readiness_graph(repository_root=repo, runtime_root=runtime, registry_loader=lambda *_a, **_k: _registry()))
    assert rows["safe"]["category"] == STATE_DRIFT
    assert "PACKAGE_INSPECTION_EXCEPTION" in rows["safe"]["reason_codes"]
    assert rows["linked"]["category"] == STATE_DRIFT


def test_serial_requires_conventional_manifest_attested_record_and_clean_ledger(tmp_path: Path) -> None:
    repo, runtime = _runtime(tmp_path)
    root, record = _package(runtime, "2026-08-20", "serial")
    manifest = root / "serial.upload_manifest.json"
    manifest.write_text("{}")
    (runtime / "state/2026-08-20.json").write_text(json.dumps({"picks": [{"candidate_id": "serial", "status": "review_ready", "rc": 0}]}))
    def loader(_path: Path, **_kwargs: object) -> tuple[dict | None, list[str]]:
        return {"video": {"sha256": "sha256:video"}, "package_attestation": {"record": _attested_record(record)}}, []
    rows = _rows(build_readiness_graph(repository_root=repo, runtime_root=runtime, registry_loader=lambda *_a, **_k: _registry(), manifest_loader=loader, ledger_checker=lambda *_a: (None, None, [])))
    assert rows["serial"]["category"] == READY_FOR_SERIAL_UPLOAD
    assert rows["serial"]["package_dependencies"]["upload_manifest"] == str(manifest)
    rows = _rows(build_readiness_graph(repository_root=repo, runtime_root=runtime, registry_loader=lambda *_a, **_k: _registry(), manifest_loader=lambda *_a, **_k: ({"video": {"sha256": "sha256:video"}, "package_attestation": {"record": {"path": "wrong"}}}, []), ledger_checker=lambda *_a: (None, None, [])))
    assert rows["serial"]["category"] == STATE_DRIFT
    rows = _rows(build_readiness_graph(repository_root=repo, runtime_root=runtime, registry_loader=lambda *_a, **_k: _registry(), manifest_loader=lambda *_a, **_k: ({"candidate_id": "other", "recording_date": "2026-08-21", "video": {"sha256": "sha256:video"}, "package_attestation": {"record": _attested_record(record)}}, []), ledger_checker=lambda *_a: (None, None, [])))
    assert rows["serial"]["category"] == STATE_DRIFT
    assert "UPLOAD_MANIFEST_IDENTITY_DRIFT" in rows["serial"]["reason_codes"]


def test_serial_accepts_byte_bound_same_stem_record_but_rejects_drift(
    tmp_path: Path,
) -> None:
    repo, runtime = _runtime(tmp_path)
    root, record = _package(runtime, "2026-08-20", "serial")
    publish = root / "serial.publish.json"
    record_doc = json.loads(record.read_text())
    record_doc["publish_staging"] = {"publish_json_path": str(publish)}
    record.write_text(json.dumps(record_doc))
    delivery_record = root / "serial.recut.record.json"
    delivery_record.write_bytes(record.read_bytes())
    review_manifest = root / "serial.review-manifest.json"

    def bind_delivery_record(payload: bytes) -> None:
        delivery_record.write_bytes(payload)
        review_manifest.write_text(
            json.dumps(
                {
                    "schema_version": "lidousha-daily-review-manifest.v1",
                    "candidate_id": "serial",
                    "items": [
                        {
                            "candidate_id": "serial",
                            "evidence_json": record.name,
                            "record": delivery_record.name,
                            "publish_json": publish.name,
                            "sha256": {
                                "evidence_json": hashlib.sha256(payload).hexdigest(),
                            },
                        }
                    ],
                }
            )
        )

    bind_delivery_record(delivery_record.read_bytes())
    (root / "serial.upload_manifest.json").write_text("{}")
    (runtime / "state/2026-08-20.json").write_text(
        json.dumps({"picks": [{"candidate_id": "serial", "status": "review_ready", "rc": 0}]})
    )
    initial = delivery_record.read_bytes()

    def manifest_for(
        payload: bytes, *, declared_sha: str | None = None
    ) -> tuple[dict[str, object], list[str]]:
        return (
            {
                "manifest_version": 3,
                "schema_version": "authorized-upload-manifest.v3",
                "candidate_id": "serial",
                "recording_date": "2026-08-20",
                "video": {"sha256": "sha256:video"},
                "package_attestation": {
                    "record": {
                        "path": str(delivery_record),
                        "sha256": declared_sha or hashlib.sha256(payload).hexdigest(),
                        "bytes": len(payload),
                    }
                },
            },
            [],
        )

    rows = _rows(
        build_readiness_graph(
            repository_root=repo,
            runtime_root=runtime,
            registry_loader=lambda *_a, **_k: _registry(),
            manifest_loader=lambda *_a, **_k: manifest_for(initial),
            ledger_checker=lambda *_a: (None, None, []),
        )
    )
    assert rows["serial"]["category"] == READY_FOR_SERIAL_UPLOAD

    for invalid_sha in (_sha(initial), "g" * 64):
        rows = _rows(
            build_readiness_graph(
                repository_root=repo,
                runtime_root=runtime,
                registry_loader=lambda *_a, **_k: _registry(),
                manifest_loader=lambda *_a, invalid_sha=invalid_sha, **_k: manifest_for(
                    initial, declared_sha=invalid_sha
                ),
                ledger_checker=lambda *_a: (None, None, []),
            )
        )
        assert rows["serial"]["category"] == STATE_DRIFT
        assert "UPLOAD_MANIFEST_IDENTITY_DRIFT" in rows["serial"]["reason_codes"]

    drifted = b'{"same-stem":"drift"}'
    bind_delivery_record(drifted)
    rows = _rows(
        build_readiness_graph(
            repository_root=repo,
            runtime_root=runtime,
            registry_loader=lambda *_a, **_k: _registry(),
            manifest_loader=lambda *_a, **_k: manifest_for(initial),
            ledger_checker=lambda *_a: (None, None, []),
        )
    )
    assert rows["serial"]["category"] == STATE_DRIFT
    assert "UPLOAD_MANIFEST_IDENTITY_DRIFT" in rows["serial"]["reason_codes"]

    rows = _rows(
        build_readiness_graph(
            repository_root=repo,
            runtime_root=runtime,
            registry_loader=lambda *_a, **_k: _registry(),
            manifest_loader=lambda *_a, **_k: manifest_for(drifted),
            ledger_checker=lambda *_a: (None, None, []),
        )
    )
    assert rows["serial"]["category"] == STATE_DRIFT
    assert "UPLOAD_MANIFEST_IDENTITY_DRIFT" in rows["serial"]["reason_codes"]


def test_serial_rejects_missing_local_audit_and_uploaded_registry_disagreement(tmp_path: Path) -> None:
    repo, runtime = _runtime(tmp_path)
    root, record = _package(runtime, "2026-08-20", "serial")
    (root / "serial.upload_manifest.json").write_text("{}")
    (root / "serial.package-audit.json").unlink()
    (runtime / "state/2026-08-20.json").write_text(json.dumps({"picks": [{"candidate_id": "serial", "status": "review_ready", "rc": 0}]}))
    def loader(*_args: object, **_kwargs: object) -> tuple[dict[str, object], list[str]]:
        return {"video": {"sha256": "sha256:video"}, "package_attestation": {"record": _attested_record(record)}}, []
    rows = _rows(build_readiness_graph(repository_root=repo, runtime_root=runtime, registry_loader=lambda *_a, **_k: _registry(), manifest_loader=loader, ledger_checker=lambda *_a: (None, None, [])))
    assert rows["serial"]["category"] == STATE_DRIFT
    _write(root / "serial.package-audit.json", b'{"status":"PASS"}')
    rows = _rows(build_readiness_graph(repository_root=repo, runtime_root=runtime, registry_loader=lambda *_a, **_k: _registry(), manifest_loader=loader, ledger_checker=lambda *_a: ("uploaded", {}, [])))
    assert rows["serial"]["category"] == STATE_DRIFT
    assert "UPLOAD_STATE_REGISTRY_DRIFT" in rows["serial"]["reason_codes"]


def test_unresolved_ledger_blocks_only_serial_and_bad_state_does_not_hide_ready(tmp_path: Path) -> None:
    repo, runtime = _runtime(tmp_path)
    root, record = _package(runtime, "2026-08-20", "ready")
    _package(runtime, "2026-08-20", "other")
    (root / "ready.upload_manifest.json").write_text("{}")
    (runtime / "state/2026-08-19.json").write_text("{bad")
    (runtime / "state/2026-08-20.json").write_text(json.dumps({"picks": [{"candidate_id": "ready", "status": "review_ready", "rc": 0}, {"candidate_id": "other", "status": "review_ready", "rc": 0}]}))
    def loader(_path: Path, **_kwargs: object) -> tuple[dict | None, list[str]]:
        return {"video": {"sha256": "sha256:video"}, "package_attestation": {"record": _attested_record(record)}}, []
    graph = build_readiness_graph(repository_root=repo, runtime_root=runtime, registry_loader=lambda *_a, **_k: _registry(), manifest_loader=loader, ledger_checker=lambda *_a: ("unresolved", None, ["STARTED"]))
    rows = _rows(graph)
    assert rows["ready"]["category"] == READY_TO_PREPARE
    assert "LEDGER_SERIAL_BLOCKED" in rows["ready"]["reason_codes"]
    assert rows["other"]["category"] == READY_TO_PREPARE
    assert {item["code"] for item in graph["graph_blockers"]} == {"STATE_FILE_INVALID"}


def test_invalid_registry_forbids_serial_but_keeps_local_prepare(tmp_path: Path) -> None:
    repo, runtime = _runtime(tmp_path)
    root, record = _package(runtime, "2026-08-20", "serial")
    (root / "serial.upload_manifest.json").write_text("{}")
    (runtime / "state/2026-08-20.json").write_text(json.dumps({"picks": [{"candidate_id": "serial", "status": "review_ready", "rc": 0}]}))
    def loader(*_args: object, **_kwargs: object) -> tuple[dict[str, object], list[str]]:
        return {"video": {"sha256": "sha256:video"}, "package_attestation": {"record": _attested_record(record)}}, []
    graph = build_readiness_graph(repository_root=repo, runtime_root=runtime, registry_loader=lambda *_a, **_k: (_ for _ in ()).throw(ValueError("bad registry")), manifest_loader=loader, ledger_checker=lambda *_a: (None, None, []))
    row = _rows(graph)["serial"]
    assert row["category"] == READY_TO_PREPARE
    assert "REGISTRY_SERIAL_BLOCKED" in row["reason_codes"]
    assert {item["code"] for item in graph["graph_blockers"]} == {"PUBLICATION_REGISTRY_INVALID"}


def test_manifest_and_ledger_exceptions_are_candidate_local(tmp_path: Path) -> None:
    repo, runtime = _runtime(tmp_path)
    roots = {candidate: _package(runtime, "2026-08-20", candidate) for candidate in ("good", "manifest-bad", "ledger-bad")}
    for candidate, (root, _record) in roots.items():
        (root / f"{candidate}.upload_manifest.json").write_text("{}")
    (runtime / "state/2026-08-20.json").write_text(json.dumps({"picks": [{"candidate_id": candidate, "status": "review_ready", "rc": 0} for candidate in roots]}))
    def loader(path: Path, **_kwargs: object) -> tuple[dict[str, object], list[str]]:
        if "manifest-bad" in path.name:
            raise RuntimeError("manifest race")
        candidate = "ledger-bad" if "ledger-bad" in path.name else "good"
        return {"candidate_id": candidate, "recording_date": "2026-08-20", "video": {"sha256": "sha256:" + candidate}, "package_attestation": {"record": _attested_record(roots[candidate][1])}}, []
    def checker(_ledger: Path, sha: str) -> tuple[str | None, dict | None, list[str]]:
        if sha.endswith("ledger-bad"):
            raise RuntimeError("ledger race")
        return None, None, []
    rows = _rows(build_readiness_graph(repository_root=repo, runtime_root=runtime, registry_loader=lambda *_a, **_k: _registry(), manifest_loader=loader, ledger_checker=checker))
    assert rows["good"]["category"] == READY_FOR_SERIAL_UPLOAD
    assert rows["manifest-bad"]["category"] == STATE_DRIFT
    assert rows["ledger-bad"]["category"] == STATE_DRIFT
    assert "PACKAGE_MANIFEST_OR_LEDGER_EXCEPTION" in rows["manifest-bad"]["reason_codes"]
    assert "PACKAGE_MANIFEST_OR_LEDGER_EXCEPTION" in rows["ledger-bad"]["reason_codes"]


def test_readiness_is_read_only_and_never_calls_provider_or_network(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, runtime = _runtime(tmp_path)
    _package(runtime, "2026-08-20", "safe")
    state = runtime / "state/2026-08-20.json"
    state.write_text(json.dumps({"picks": [{"candidate_id": "safe", "status": "review_ready", "rc": 0}]}))
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    monkeypatch.setattr("src.autoslice.publication_readiness.authorized_upload.load_and_verify", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("network/provider")))
    build_readiness_graph(repository_root=repo, runtime_root=runtime, registry_loader=lambda *_a, **_k: _registry())
    assert before == {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}


def test_fixed_cli_bootstraps_from_non_repo_cwd_without_runtime_writes(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    (runtime / "state").mkdir(parents=True)
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    script = Path(__file__).resolve().parents[1] / "scripts/publication_readiness_graph.py"
    result = subprocess.run(
        [sys.executable, str(script)],
        cwd=tmp_path,
        env={**os.environ, "AUTOSLICE_BASE": str(runtime), "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    graph = json.loads(result.stdout)
    assert graph["schema_version"] == "publication-readiness-graph.v1"
    assert all(row["dependencies"] == ["publication_registry"] for row in graph["rows"])
    assert before == {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}


def test_uniform_host_nested_ass_locator_is_read_only(tmp_path: Path) -> None:
    """Uniform-host renders declare ASS in burned_preview, not speaker ASS."""
    repo, runtime = _runtime(tmp_path)
    root, record = _package(runtime, "2026-08-20", "uniform", nested_burn=True)
    doc = json.loads(record.read_text())
    expected_ass = doc["subtitle_ass_path"]
    doc["subtitle_ass_path"] = None
    doc["burned_preview"].update(status="BURNED", ass_path=expected_ass)
    record.write_text(json.dumps(doc))
    state = runtime / "state/2026-08-20.json"
    state.write_text(json.dumps({"picks": [{"candidate_id": "uniform", "status": "review_ready", "rc": 0}]}))
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    rows = _rows(build_readiness_graph(
        repository_root=repo, runtime_root=runtime,
        registry_loader=lambda *_a, **_k: _registry(),
    ))
    assert rows["uniform"]["category"] == READY_TO_PREPARE
    assert rows["uniform"]["package_dependencies"]["subtitle_ass"] == expected_ass
    assert {p: p.read_bytes() for p in before} == before


@pytest.mark.parametrize("fault", ["different_bytes", "escaped", "symlink", "missing", "invalid_locator"])
def test_nested_ass_locator_still_rejects_drift(tmp_path: Path, fault: str) -> None:
    repo, runtime = _runtime(tmp_path)
    root, record = _package(runtime, "2026-08-20", "bad-ass", nested_burn=True)
    doc = json.loads(record.read_text())
    ass = Path(doc["subtitle_ass_path"])
    # An explicit top-level locator does not permit a conflicting nested one.
    if fault == "different_bytes":
        other = root / "different.ass"
        other.write_bytes(b"wrong subtitle rendering")
        nested: object = str(other)
    elif fault == "escaped":
        other = tmp_path / "outside.ass"
        other.write_bytes(ass.read_bytes())
        nested = str(other)
    elif fault == "symlink":
        other = root / "linked.ass"
        other.symlink_to(ass)
        nested = str(other)
    elif fault == "missing":
        nested = str(root / "missing.ass")
    else:
        nested = 123
    doc["burned_preview"].update(status="BURNED", ass_path=nested)
    record.write_text(json.dumps(doc))
    (runtime / "state/2026-08-20.json").write_text(json.dumps({"picks": [{"candidate_id": "bad-ass", "status": "review_ready", "rc": 0}]}))
    rows = _rows(build_readiness_graph(
        repository_root=repo, runtime_root=runtime,
        registry_loader=lambda *_a, **_k: _registry(),
    ))
    assert rows["bad-ass"]["category"] == STATE_DRIFT
    assert set(rows["bad-ass"]["reason_codes"]) & {
        "PACKAGE_ARTIFACT_HASH_DRIFT", "PACKAGE_ARTIFACT_FILE_INVALID", "PACKAGE_ARTIFACT_LOCATOR_MISSING"
    }


@pytest.mark.parametrize("status", [None, "DRY_RUN", "FAILED"])
def test_nested_ass_fallback_requires_burned_status(tmp_path: Path, status: object) -> None:
    repo, runtime = _runtime(tmp_path)
    root, record = _package(runtime, "2026-08-20", "unburned", nested_burn=True)
    doc = json.loads(record.read_text())
    doc["burned_preview"].update(ass_path=doc["subtitle_ass_path"], status=status)
    doc["subtitle_ass_path"] = None
    record.write_text(json.dumps(doc))
    (runtime / "state/2026-08-20.json").write_text(json.dumps({"picks": [{"candidate_id": "unburned", "status": "review_ready", "rc": 0}]}))
    rows = _rows(build_readiness_graph(
        repository_root=repo, runtime_root=runtime,
        registry_loader=lambda *_a, **_k: _registry(),
    ))
    assert rows["unburned"]["category"] == STATE_DRIFT
    assert "PACKAGE_ARTIFACT_LOCATOR_MISSING" in rows["unburned"]["reason_codes"]

def _review_package_item(
    candidate: str, *, record_name: str
) -> dict[str, object]:
    return {
        "candidate_id": candidate,
        "title": f"【李豆沙】{candidate}",
        "record": record_name,
        "publish_json": f"{candidate}.publish.json",
        "video": f"{candidate}.recut.mp4",
        "mp4": f"{candidate}.recut.mp4",
        "subtitle_srt": f"{candidate}.recut.srt",
        "speaker_srt": f"{candidate}.recut.srt",
        "ass_path": f"{candidate}.recut.ass",
        "cover": f"covers/{candidate}.cover.png",
    }


def test_review_package_manifest_selects_current_record_over_history_and_lineage(
    tmp_path: Path,
) -> None:
    repo, runtime = _runtime(tmp_path)
    root, original = _package(runtime, "2026-08-20", "recovery-current")
    current = root / "recovery-current.recut.burned.record.json"
    original.rename(current)
    history = root / "history/a8/old.record.json"
    lineage = root / "lineage/e1/older.record.json"
    _write(history, b'{"historical":true}')
    _write(lineage, b'{"lineage":true}')
    (root / "review_manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "lidousha-review-package.v1",
                "status": "finished_review_package_no_upload_pending_human_review",
                "upload_allowed": False,
                "exact_candidate_ids": ["recovery-current"],
                "items": [
                    _review_package_item(
                        "recovery-current", record_name=current.name
                    )
                ],
            }
        )
    )
    from src.autoslice.publication_readiness import _canonical_record_file

    assert _canonical_record_file(root, "recovery-current") == current
    reasons, _dependencies, selected = publication_readiness._inspect_package(
        root, "recovery-current", "2026-08-20"
    )
    assert selected == current
    assert "PACKAGE_RECORD_MISSING_OR_AMBIGUOUS" not in reasons


def test_review_package_manifest_record_binding_is_fail_closed(
    tmp_path: Path,
) -> None:
    _repo, runtime = _runtime(tmp_path)
    root, record = _package(runtime, "2026-08-20", "recovery-invalid")
    manifest = root / "review_manifest.json"
    document = {
        "schema_version": "lidousha-review-package.v1",
        "status": "finished_review_package_no_upload_pending_human_review",
        "upload_allowed": False,
        "exact_candidate_ids": ["recovery-invalid"],
        "items": [
            _review_package_item(
                "recovery-invalid", record_name=record.name
            )
        ],
    }
    manifest.write_text(json.dumps(document))
    from src.autoslice.publication_readiness import _canonical_record_file

    assert _canonical_record_file(root, "recovery-invalid") == record

    document["items"][0]["record"] = "history/old.record.json"
    _write(root / "history/old.record.json", record.read_bytes())
    manifest.write_text(json.dumps(document))
    assert _canonical_record_file(root, "recovery-invalid") is None

    document["items"][0]["record"] = record.name
    document["exact_candidate_ids"] = ["other"]
    manifest.write_text(json.dumps(document))
    assert _canonical_record_file(root, "recovery-invalid") is None


def test_review_package_without_current_mechanical_receipt_is_blocked(
    tmp_path: Path,
) -> None:
    _repo, runtime = _runtime(tmp_path)
    root, record = _package(runtime, "2026-08-20", "mechanical-missing")
    (root / "review_manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "lidousha-review-package.v1",
                "status": "finished_review_package_no_upload_pending_human_review",
                "upload_allowed": False,
                "exact_candidate_ids": ["mechanical-missing"],
                "items": [
                    _review_package_item(
                        "mechanical-missing", record_name=record.name
                    )
                ],
            }
        )
    )

    reasons, dependencies, _selected = publication_readiness._inspect_package(
        root, "mechanical-missing", "2026-08-20"
    )

    assert "MECHANICAL_DELIVERY_REVIEW_MISSING" in reasons
    assert "mechanical_delivery_review" not in dependencies


def test_review_package_replays_one_current_mechanical_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _repo, runtime = _runtime(tmp_path)
    root, record = _package(runtime, "2026-08-20", "mechanical-current")
    (root / "review_manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "lidousha-review-package.v1",
                "status": "finished_review_package_no_upload_pending_human_review",
                "upload_allowed": False,
                "exact_candidate_ids": ["mechanical-current"],
                "items": [
                    _review_package_item(
                        "mechanical-current", record_name=record.name
                    )
                ],
            }
        )
    )
    receipt = root / "verification/mechanical-delivery-review.json"
    receipt.parent.mkdir(exist_ok=True)
    receipt.write_text(json.dumps({"fixture": "mechanical"}))
    from src.autoslice import mechanical_delivery_review

    calls = []

    def replay(value, package_root):
        calls.append((value, package_root))
        return dict(value)

    monkeypatch.setattr(
        mechanical_delivery_review, "validate_saved_mechanical_receipt", replay
    )

    reasons, dependencies, _selected = publication_readiness._inspect_package(
        root, "mechanical-current", "2026-08-20"
    )

    assert "MECHANICAL_DELIVERY_REVIEW_MISSING" not in reasons
    assert "MECHANICAL_DELIVERY_REVIEW_INVALID" not in reasons
    assert dependencies["mechanical_delivery_review"] == str(receipt)
    assert calls == [({"fixture": "mechanical"}, root)]


def test_review_package_duplicate_or_invalid_mechanical_receipt_is_blocked(
    tmp_path: Path,
) -> None:
    _repo, runtime = _runtime(tmp_path)
    root, record = _package(runtime, "2026-08-20", "mechanical-invalid")
    (root / "review_manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "lidousha-review-package.v1",
                "status": "finished_review_package_no_upload_pending_human_review",
                "upload_allowed": False,
                "exact_candidate_ids": ["mechanical-invalid"],
                "items": [
                    _review_package_item(
                        "mechanical-invalid", record_name=record.name
                    )
                ],
            }
        )
    )
    verification = root / "verification"
    verification.mkdir(exist_ok=True)
    (verification / "mechanical-delivery-review.json").write_text("{}")
    (verification / "mechanical-invalid.mechanical-delivery-review.json").write_text(
        "{}"
    )

    reasons, dependencies, _selected = publication_readiness._inspect_package(
        root, "mechanical-invalid", "2026-08-20"
    )

    assert "MECHANICAL_DELIVERY_REVIEW_INVALID" in reasons
    assert "mechanical_delivery_review" not in dependencies

def test_mechanical_receipt_reason_is_state_drift_not_ready() -> None:
    assert publication_readiness._category(
        {"MECHANICAL_DELIVERY_REVIEW_MISSING"}, serial=False
    ) == STATE_DRIFT
    assert publication_readiness._category(
        {"MECHANICAL_DELIVERY_REVIEW_INVALID"}, serial=False
    ) == STATE_DRIFT
