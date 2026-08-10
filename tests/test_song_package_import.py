"""歌切评审包跨主机导入：证据链、两处落地、songs 绑定与 talk 零变化。

源包用**真正的评审包 builder** 现造（``tests.test_build_lidousha_song_review_manifest``
的 fixture），不是手糊 JSON：导入器认的就是那个 builder 的信封，两边分家会
在这里直接红。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import scripts.build_lidousha_song_review_manifest as builder
from src.autoslice import package_import as pi
from src.autoslice import song_package_import as spi
from src.autoslice.publication_reconciliation import project_publication_closure
from src.autoslice.publication_registry import REGISTRY_SCHEMA
from tests.test_build_lidousha_song_review_manifest import (
    BASENAME,
    CANDIDATE_ID,
    _build,
    _fixture,
)


DATE = "2026-07-25"
BOUND_AT = "2026-08-10T18:00:00Z"


def _registry(tmp_path: Path, status: str | None = "hold_pending_review") -> Path:
    entries = []
    if status is not None:
        row = {
            "candidate_id": CANDIDATE_ID,
            "recording_date": DATE,
            "status": status,
        }
        if status == "published":
            row["bvid"] = "BV1BJGc6aEWf"
        entries.append(row)
    path = tmp_path / "publication_registry.v1.json"
    path.write_text(
        json.dumps(
            {"schema_version": REGISTRY_SCHEMA, "entries": entries},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def _source_package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """One genuine review package, plus the producing host's package audit."""

    fx = _fixture(tmp_path)
    _build(fx, monkeypatch)
    package = fx["package"]
    (package / "package_audit.json").write_text(
        json.dumps(
            {
                "schema_version": "lidousha-review-package-audit.v2",
                "passed": True,
                "root": "/home/ivan/Project/vtuber-reproduce/wsl-song-88/"
                f"review_packages/{DATE}/{CANDIDATE_ID}-r1",
                "issues": [],
                "issue_count": 0,
                "blocking_issue_count": 0,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return package


def _destinations(tmp_path: Path) -> tuple[Path, Path]:
    return (
        tmp_path / "free" / "review_packages" / DATE / f"{CANDIDATE_ID}-r1",
        tmp_path / "free" / "repo" / "lidousha" / DATE,
    )


def _plan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> spi.SongImportPlan:
    package = _source_package(tmp_path, monkeypatch)
    package_root, delivery_root = _destinations(tmp_path)
    return spi.plan_song_import(
        source_package_dir=package,
        destination_package_root=package_root,
        destination_delivery_root=delivery_root,
        candidate_id=CANDIDATE_ID,
    )


def _before_state() -> dict:
    return {
        "status": "published_with_failures",
        "picks": [{"candidate_id": "auto_1", "status": "failed", "rc": 3}],
        "songs": [],
        "pending_talk": [],
        "pending_song": [],
    }


def test_role_suffix_table_matches_the_review_package_builder() -> None:
    """平价锚：导入器认的角色表就是产这个包的 builder 的角色表。"""

    assert spi.SONG_ROLE_SUFFIXES == builder.REQUIRED_ROLES


def test_song_package_plans_lands_and_binds_to_songs_not_picks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(tmp_path, monkeypatch)

    # 计划：包内全部（除 package_audit.json）+ 扁平交付 12 件，manifest 最后。
    assert plan.documents.stem == BASENAME
    assert plan.documents.candidate_id == CANDIDATE_ID
    assert plan.documents.source_candidate_id != CANDIDATE_ID
    assert "package_audit.json" in plan.skipped
    assert plan.copies[-1].destination == plan.destination_delivery_manifest
    package_names = {
        item.destination.name
        for item in plan.copies
        if item.role == "review_package"
    }
    assert "review_manifest.json" in package_names
    assert "package_audit.json" not in package_names

    receipt = pi.execute_copy(plan, apply=True)
    assert receipt["divergent_destination_count"] == 0
    assert not plan.destination_package_audit.exists()
    assert spi.verify_landed_song_delivery(plan)["delivery_manifest"]

    after, detail = spi.build_bound_song_state(
        _before_state(),
        plan=plan,
        date=DATE,
        bound_at=BOUND_AT,
        project_closure=project_publication_closure,
        registry_path=_registry(tmp_path),
    )

    assert detail["created_song_row"] is True
    assert after["picks"] == _before_state()["picks"]
    assert [row["candidate_id"] for row in after["songs"]] == [CANDIDATE_ID]
    row = after["songs"][0]
    assert row["status"] == "review_ready" and row["rc"] == 0
    assert row["lane"] == "song"
    assert row["delivered"] == str(plan.delivery_path("video"))
    assert row["cover_path"] == str(plan.delivery_path("cover"))
    assert row["external_song_package_import"]["review_package_root"] == str(
        plan.destination_package_root
    )
    # 冻结件里的生产机路径是被声明的出处，不是本机定位符。
    assert row["external_song_package_import"]["frozen_locator_authority"] == (
        "producing_host"
    )
    # locator-bearing 生产机文档不进 free 的运行期 state。
    assert "cover_generation" not in row
    assert "selector_summary_path" not in row


def test_imported_song_is_not_uploadable_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(tmp_path, monkeypatch)
    pi.execute_copy(plan, apply=True)

    after, _ = spi.build_bound_song_state(
        _before_state(),
        plan=plan,
        date=DATE,
        bound_at=BOUND_AT,
        project_closure=project_publication_closure,
        registry_path=_registry(tmp_path),
    )

    row = after["songs"][0]
    assert row["delivery_upload_enabled"] is False
    assert json.loads(
        plan.destination_delivery_manifest.read_text(encoding="utf-8")
    )["upload_enabled"] is False
    review = json.loads(
        plan.destination_review_manifest.read_text(encoding="utf-8")
    )
    assert review["upload_allowed"] is False
    assert review["status"] == (
        "finished_review_package_no_upload_pending_human_review"
    )


def test_landed_bytes_are_byte_identical_to_the_producing_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(tmp_path, monkeypatch)
    pi.execute_copy(plan, apply=True)

    for role in spi.SONG_ROLE_SUFFIXES:
        landed = plan.destination_package_root / (
            BASENAME + spi.SONG_ROLE_SUFFIXES[role]
        )
        assert pi.sha256_file(landed) == plan.documents.role_sha256[role]
        assert (
            pi.sha256_file(plan.delivery_path(role))
            == plan.documents.role_sha256[role]
        )


@pytest.mark.parametrize(
    ("role", "code"),
    [
        ("host_vocal_proof", "SONG_ARTIFACT_SHA_DRIFT"),
        ("lyrics_alignment_report", "SONG_ARTIFACT_SHA_DRIFT"),
        ("recut_manifest", "SONG_ARTIFACT_SHA_DRIFT"),
        ("video", "SONG_ARTIFACT_SHA_DRIFT"),
    ],
)
def test_sha_drift_in_any_song_proof_is_a_typed_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, role: str, code: str
) -> None:
    package = _source_package(tmp_path, monkeypatch)
    (package / f"{BASENAME}{spi.SONG_ROLE_SUFFIXES[role]}").write_bytes(
        b"tampered"
    )
    package_root, delivery_root = _destinations(tmp_path)

    with pytest.raises(pi.PackageImportError) as excinfo:
        spi.plan_song_import(
            source_package_dir=package,
            destination_package_root=package_root,
            destination_delivery_root=delivery_root,
            candidate_id=CANDIDATE_ID,
        )
    assert excinfo.value.code == code


@pytest.mark.parametrize(
    "role", ["host_vocal_proof", "lyrics_alignment_report", "recut_manifest"]
)
def test_missing_song_proof_is_a_typed_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    package = _source_package(tmp_path, monkeypatch)
    (package / f"{BASENAME}{spi.SONG_ROLE_SUFFIXES[role]}").unlink()
    package_root, delivery_root = _destinations(tmp_path)

    with pytest.raises(pi.PackageImportError) as excinfo:
        spi.plan_song_import(
            source_package_dir=package,
            destination_package_root=package_root,
            destination_delivery_root=delivery_root,
            candidate_id=CANDIDATE_ID,
        )
    assert excinfo.value.code == "SOURCE_FILE_MISSING"


def test_unready_completion_proof_is_a_typed_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = _source_package(tmp_path, monkeypatch)
    review_path = package / "review_manifest.json"
    review = json.loads(review_path.read_text(encoding="utf-8"))
    review["delivery_authority"]["song_completion_evidence"][
        "host_vocal_status"
    ] = "BLOCKED"
    review_path.write_text(
        json.dumps(review, ensure_ascii=False), encoding="utf-8"
    )
    package_root, delivery_root = _destinations(tmp_path)

    with pytest.raises(pi.PackageImportError) as excinfo:
        spi.plan_song_import(
            source_package_dir=package,
            destination_package_root=package_root,
            destination_delivery_root=delivery_root,
            candidate_id=CANDIDATE_ID,
        )
    assert excinfo.value.code == "SONG_COMPLETION_EVIDENCE_INVALID"


def test_delivery_manifest_hash_drift_breaks_the_authority_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = _source_package(tmp_path, monkeypatch)
    manifest = package / f"{BASENAME}.delivery.manifest.json"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["absent_artifacts"] = {"unused": {"status": "ABSENT"}}
    manifest.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    package_root, delivery_root = _destinations(tmp_path)

    with pytest.raises(pi.PackageImportError) as excinfo:
        spi.plan_song_import(
            source_package_dir=package,
            destination_package_root=package_root,
            destination_delivery_root=delivery_root,
            candidate_id=CANDIDATE_ID,
        )
    assert excinfo.value.code == "SONG_DELIVERY_AUTHORITY_INVALID"


def test_symlinked_source_artifact_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = _source_package(tmp_path, monkeypatch)
    video = package / f"{BASENAME}.mp4"
    payload = video.read_bytes()
    elsewhere = tmp_path / "elsewhere.mp4"
    elsewhere.write_bytes(payload)
    video.unlink()
    video.symlink_to(elsewhere)
    package_root, delivery_root = _destinations(tmp_path)

    with pytest.raises(pi.PackageImportError) as excinfo:
        spi.plan_song_import(
            source_package_dir=package,
            destination_package_root=package_root,
            destination_delivery_root=delivery_root,
            candidate_id=CANDIDATE_ID,
        )
    assert excinfo.value.code == "UNSAFE_PATH_SYMLINK"


def test_landed_delivery_drift_is_caught_before_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(tmp_path, monkeypatch)
    pi.execute_copy(plan, apply=True)
    plan.delivery_path("cover").write_bytes(b"overwritten")

    with pytest.raises(pi.PackageImportError) as excinfo:
        spi.verify_landed_song_delivery(plan)
    assert excinfo.value.code == "LANDED_ARTIFACT_SHA_DRIFT"


def test_bind_refuses_while_generic_cover_maintenance_is_unblocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(tmp_path, monkeypatch)

    with pytest.raises(pi.PackageImportError) as excinfo:
        spi.build_bound_song_state(
            _before_state(),
            plan=plan,
            date=DATE,
            bound_at=BOUND_AT,
            project_closure=project_publication_closure,
            registry_path=_registry(tmp_path, status=None),
        )
    assert excinfo.value.code == "SONG_COVER_MAINTENANCE_UNBLOCKED"
    assert "publication_registry" in excinfo.value.hint


def test_bind_refuses_a_processing_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(tmp_path, monkeypatch)
    state = _before_state()
    state["status"] = "processing"

    with pytest.raises(pi.PackageImportError) as excinfo:
        spi.build_bound_song_state(
            state,
            plan=plan,
            date=DATE,
            bound_at=BOUND_AT,
            project_closure=project_publication_closure,
            registry_path=_registry(tmp_path),
        )
    assert excinfo.value.code == "BATCH_STATUS_NOT_REVIEWABLE"


def test_bind_refuses_a_still_queued_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(tmp_path, monkeypatch)
    state = _before_state()
    state["pending_song"] = [{"cid": CANDIDATE_ID}]

    with pytest.raises(pi.PackageImportError) as excinfo:
        spi.build_bound_song_state(
            state,
            plan=plan,
            date=DATE,
            bound_at=BOUND_AT,
            project_closure=project_publication_closure,
            registry_path=_registry(tmp_path),
        )
    assert excinfo.value.code == "CANDIDATE_STILL_QUEUED"


def test_native_row_is_never_overwritten_in_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(tmp_path, monkeypatch)
    state = _before_state()
    state["songs"] = [
        {
            "candidate_id": CANDIDATE_ID,
            "status": "blocked",
            "rc": 0,
            "start_ms": 1090340,
            "end_ms": 1352380,
            "hook": "生日舞台",
            "reason_codes": ["SONG_NOT_LIDOUSHA_SINGING"],
        }
    ]

    with pytest.raises(pi.PackageImportError) as excinfo:
        spi.build_bound_song_state(
            state,
            plan=plan,
            date=DATE,
            bound_at=BOUND_AT,
            project_closure=project_publication_closure,
            registry_path=_registry(tmp_path),
        )
    assert excinfo.value.code == "SONG_ROW_NOT_REBINDABLE"

    after, detail = spi.build_bound_song_state(
        state,
        plan=plan,
        date=DATE,
        bound_at=BOUND_AT,
        project_closure=project_publication_closure,
        allow_supersede_blocked=True,
        registry_path=_registry(tmp_path),
    )

    assert detail["superseded_existing_row"] is True
    assert after["song_superseded_attempts"][0]["superseded_row"] == state["songs"][0]
    assert after["song_superseded_attempts"][0]["status"] == "blocked"
    row = after["songs"][0]
    assert row["status"] == "review_ready"
    # 场次窗口是本机自己的选题证据，逐字带走，不从包里的重剪偏移重算。
    assert (row["start_ms"], row["end_ms"]) == (1090340, 1352380)
    assert detail["carried_selection_keys"] == ["end_ms", "hook", "start_ms"]


def _published_pick(tmp_path: Path) -> dict:
    """One genuinely verified published pick, so the closure is applicable."""

    authority = tmp_path / "authority.json"
    authority.write_text("{}", encoding="utf-8")
    return {
        "candidate_id": "auto_published",
        "status": "published",
        "bvid": "BV1zzz",
        "publication_reconciliation": {
            "schema_version": "publication-reconciliation.v1",
            "status": "VERIFIED_PUBLIC",
            "candidate_id": "auto_published",
            "bvid": "BV1zzz",
            "authority": {
                "path": str(authority),
                "sha256": pi.sha256_file(authority),
                "bytes": authority.stat().st_size,
            },
        },
    }


def test_closed_day_import_lands_the_song_as_ready_unpublished(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """8/8 的真实形状：那天已经收官（published_with_failures），成品在别的机器上。

    谈话那份白名单会在这里直接拒绝——它镜像的是 free 现跑的日评审 manifest
    builder；歌切的评审 manifest 在 free 上永远重建不了，``authorized_upload``
    也不读 state，所以歌切按真实消费方另立白名单，并用闭包投影兜住结果。
    """

    plan = _plan(tmp_path, monkeypatch)
    state = _before_state()
    state["picks"] = [_published_pick(tmp_path), {"candidate_id": "x", "status": "failed"}]
    assert state["status"] not in pi.REVIEWABLE_BATCH_STATUSES
    assert state["status"] in spi.REVIEWABLE_SONG_BATCH_STATUSES

    after, detail = spi.build_bound_song_state(
        state,
        plan=plan,
        date=DATE,
        bound_at=BOUND_AT,
        project_closure=project_publication_closure,
        registry_path=_registry(tmp_path),
    )

    assert detail["closure_before"]["status"] == "published_with_failures"
    assert detail["closure_after"]["status"] == "ready_unpublished_with_failures"
    assert CANDIDATE_ID in detail["closure_after"]["ready_unpublished_candidate_ids"]
    assert detail["closure_after"]["published_candidate_ids"] == ["auto_published"]
    assert after["songs"][0]["delivery_upload_enabled"] is False


def test_rebinding_the_same_package_converges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(tmp_path, monkeypatch)
    registry = _registry(tmp_path)
    once, _ = spi.build_bound_song_state(
        _before_state(),
        plan=plan,
        date=DATE,
        bound_at=BOUND_AT,
        project_closure=project_publication_closure,
        registry_path=registry,
    )
    twice, detail = spi.build_bound_song_state(
        once,
        plan=plan,
        date=DATE,
        bound_at="2026-08-11T09:00:00Z",
        project_closure=project_publication_closure,
        registry_path=registry,
    )

    assert detail["state_changed"] is False
    assert twice["songs"] == once["songs"]


def test_already_published_candidate_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(tmp_path, monkeypatch)
    state = _before_state()
    state["songs"] = [
        {"candidate_id": CANDIDATE_ID, "status": "published", "rc": 0}
    ]
    # 真"已发布"要一整条 reconciliation 权威链才成立；这里用闭包投影 seam
    # 直接给出那个结论，检的是导入面对已发布件必须拒绝改绑。
    published_closure = lambda _state: {  # noqa: E731
        "schema_version": "daily-publication-closure.v1",
        "status": "published",
        "published_candidate_ids": [CANDIDATE_ID],
        "ready_unpublished_candidate_ids": [],
        "unresolved_candidate_ids": [],
    }

    with pytest.raises(pi.PackageImportError) as excinfo:
        spi.build_bound_song_state(
            state,
            plan=plan,
            date=DATE,
            bound_at=BOUND_AT,
            project_closure=published_closure,
            allow_supersede_blocked=True,
            registry_path=_registry(tmp_path, status="published"),
        )
    assert excinfo.value.code == "CANDIDATE_ALREADY_PUBLISHED"


def test_upload_gate_role_keys_stay_in_parity(tmp_path: Path) -> None:
    """上传面认哪些角色，导入面就必须搬哪些角色（``record`` 是同一份的别名）。"""

    from scripts import authorized_upload

    upload_roles = set(authorized_upload.VERIFIED_SONG_REVIEW_PATH_KEYS)
    covered = set(spi.SONG_ROLE_SUFFIXES) | {"delivery_manifest", "record"}
    assert upload_roles <= covered
    assert authorized_upload.VERIFIED_SONG_REVIEW_SCHEMA == (
        spi.SONG_REVIEW_MANIFEST_SCHEMA
    )
    assert authorized_upload.VERIFIED_SONG_DELIVERY_SCHEMA == (
        spi.VERIFIED_SONG_DELIVERY_SCHEMA
    )


def test_cli_dry_run_writes_no_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import scripts.import_external_song_package as cli

    package = _source_package(tmp_path, monkeypatch)
    base = tmp_path / "free"
    state_path = base / "state" / f"{DATE}.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(json.dumps(_before_state()), encoding="utf-8")

    receipt, code = cli.run_song_import(
        source=package,
        date=DATE,
        candidate_id=CANDIDATE_ID,
        base=base,
        state_path=state_path,
        runner_lock=base / "runner.lock",
        apply=False,
        supersede_existing_row=False,
        registry_path=_registry(tmp_path),
    )

    assert code == 0
    assert receipt["status"] == "DRY_RUN_OK"
    steps = {row["step"]: row for row in receipt["steps"]}
    assert steps["PREFLIGHT"]["status"] == "PASS"
    assert steps["PREFLIGHT"]["stem"] == BASENAME
    assert steps["COPY"]["status"] == "DRY_RUN"
    assert not (base / "review_packages").exists()
    assert not (base / "repo").exists()
    assert json.loads(state_path.read_text(encoding="utf-8")) == _before_state()


def test_cli_refuses_without_a_registry_hold_and_writes_a_typed_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import scripts.import_external_song_package as cli

    package = _source_package(tmp_path, monkeypatch)
    base = tmp_path / "free"
    state_path = base / "state" / f"{DATE}.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(json.dumps(_before_state()), encoding="utf-8")

    receipt, code = cli.run_song_import(
        source=package,
        date=DATE,
        candidate_id=CANDIDATE_ID,
        base=base,
        state_path=state_path,
        runner_lock=base / "runner.lock",
        apply=True,
        supersede_existing_row=False,
        registry_path=_registry(tmp_path, status=None),
    )

    assert code == 2
    assert receipt["status"] == "REFUSED"
    refusals = [row for row in receipt["steps"] if row["status"] == "REFUSE"]
    assert refusals[0]["code"] == "SONG_COVER_MAINTENANCE_UNBLOCKED"
    # 被拒时一个字节都没搬。
    assert not (base / "review_packages").exists()
    assert not (base / "repo").exists()
    assert "STATE_BIND" in receipt["not_reached"]


def test_talk_importer_refuses_a_song_review_package_by_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = _source_package(tmp_path, monkeypatch)
    package_root, _delivery_root = _destinations(tmp_path)

    with pytest.raises(pi.PackageImportError) as excinfo:
        pi.plan_import(
            source_package_dir=package,
            destination_package_root=package_root,
            destination_repo_root=tmp_path / "repo",
            candidate_id=CANDIDATE_ID,
        )
    assert excinfo.value.code == "LANE_NOT_SUPPORTED"
    assert "song_package_import" in excinfo.value.hint


def test_talk_directory_without_a_song_manifest_is_untouched(
    tmp_path: Path,
) -> None:
    """talk 零变化：没有歌切 manifest 的目录照旧走原来的失败路径。"""

    empty = tmp_path / "replacement_recuts"
    empty.mkdir()
    spi.refuse_if_song_review_package(empty)  # 不抛

    with pytest.raises(pi.PackageImportError) as excinfo:
        pi.plan_import(
            source_package_dir=empty,
            destination_package_root=tmp_path / "dest",
            destination_repo_root=tmp_path / "repo",
            candidate_id="auto_1",
        )
    assert excinfo.value.code == "SOURCE_FILE_MISSING"
