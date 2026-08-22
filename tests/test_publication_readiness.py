from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from src.autoslice.publication_readiness import (
    CODE_DEFECT,
    NEEDS_IVAN_TRUTH,
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
    assert rows["held"]["category"] == NEEDS_IVAN_TRUTH
    assert graph["excluded_published"] == [{"candidate_id": "public", "recording_date": "2026-08-18", "reason_code": "PUBLISHED_EXCLUDED"}]


def test_pending_and_backlog_collections_are_read_only_preparation_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, runtime = _runtime(tmp_path)
    state = {
        "pending_talk": [{"cid": "talk-ready"}],
        "talk_backlog": [
            {"cid": "talk-provider", "failure_stage": "source_fact_provider"},
            {"candidate_id": "talk-truth", "title_authority_error": "Ivan title needed"},
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
    assert rows["talk-truth"]["category"] == NEEDS_IVAN_TRUTH
    assert rows["song-ready"]["category"] == READY_TO_PREPARE
    assert rows["song-provider"]["category"] == NEEDS_PROVIDER
    assert rows["song-truth"]["category"] == NEEDS_IVAN_TRUTH
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


def test_missing_truth_provider_and_typed_code_use_real_fields(tmp_path: Path) -> None:
    repo, runtime = _runtime(tmp_path)
    _package(runtime, "2026-08-20", "provider", source_fact=False)
    _package(runtime, "2026-08-20", "truth")
    _package(runtime, "2026-08-20", "defect")
    (runtime / "state/2026-08-20.json").write_text(json.dumps({"picks": [{"candidate_id": "provider", "status": "review_ready", "rc": 0}, {"candidate_id": "truth", "status": "review_ready", "rc": 0, "title_authority_error": "exact Ivan title required"}, {"candidate_id": "defect", "status": "review_ready", "rc": 0, "failure_stage": "runtime_code_defect"}]}))
    rows = _rows(build_readiness_graph(repository_root=repo, runtime_root=runtime, registry_loader=lambda *_a, **_k: _registry()))
    assert rows["provider"]["category"] == NEEDS_PROVIDER
    assert rows["truth"]["category"] == NEEDS_IVAN_TRUTH
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
