from __future__ import annotations

import hashlib
import json
import stat
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.autoslice import publication_reconciliation as reconciliation
from src.autoslice import fastlane_c1_technical_receipt
from src.autoslice.batch_terminal_state import project_terminal_batch_state
from src.autoslice.candidate_selection import exact_talk_contract_closure
from src.autoslice.reporting import _current_compliant_delivery
from src.autoslice import same_bv_live_verification
from src.autoslice.surface_canon import CHANNEL_PROFILE
from src.autoslice.repository_asset_authority import (
    build_deployed_authority_manifest,
)


DATE = "2026-07-29"
CANDIDATE = "auto_225056_814_887"
BVID = "BV1FBGw6uE6i"
TITLE = f"{CHANNEL_PROFILE.talk_title_prefix}公开后状态必须收敛"
DESCRIPTION = "https://live.bilibili.com/\n测试简介"
TAGS = [CHANNEL_PROFILE.display_name, "虚拟主播", "直播切片"]


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _entry(path: Path) -> dict:
    return {
        "path": str(path.resolve()),
        "sha256": _sha(path),
        "bytes": path.stat().st_size,
    }


def _new_bv_fixture(
    tmp_path: Path,
    *,
    candidate: str = CANDIDATE,
    bvid: str = BVID,
    date: str = DATE,
    base: Path | None = None,
    write_state: bool = True,
    aid: int = 1001,
    published_cid: int = 2002,
) -> dict:
    base = base or (tmp_path / "autoslice")
    package = base / "out" / date / candidate / "final"
    package.mkdir(parents=True)
    video = package / "clip.mp4"
    cover = package / "clip.cover.png"
    record = package / "clip.record.json"
    manifest_path = package / "clip.upload_manifest.json"
    public_path = package / "clip.public_verify.json"
    season_path = package / "clip.season_verify.json"
    uploaded_path = package / "clip.uploaded.json"
    registry_path = tmp_path / "publication_registry.v1.json"
    state_path = base / "state" / f"{date}.json"
    video.write_bytes(b"video")
    cover.write_bytes(b"cover")
    _write_json(
        record,
        {
            "story_contract": {"candidate_id": candidate},
            "delivery_candidate_id": candidate,
        },
    )
    manifest = {
        "manifest_version": 3,
        "title": TITLE,
        "description": DESCRIPTION,
        "tags": TAGS,
        "video": {"path": str(video), "sha256": _sha(video)},
        "cover": {"path": str(cover), "sha256": _sha(cover)},
        "season": {"lane": "talk", "season_title": "小主切片"},
        "package_attestation": {
            "package_root": str(package.resolve()),
            "record": _entry(record),
        },
    }
    _write_json(manifest_path, manifest)
    public = {
        "schema_version": "authorized-upload-public-verify.v2",
        "status": "VERIFIED_PUBLIC",
        "bvid": bvid,
        "manifest_title": TITLE,
        "expected": {
            "title": TITLE,
            "description": DESCRIPTION,
            "tags": TAGS,
            "tid": 21,
            "copyright": 2,
            "source": "https://live.bilibili.com/",
        },
        "public_view": {
            "code": 0,
            "state": 0,
            "aid": aid,
            "cid": published_cid,
            "title": TITLE,
            "desc": DESCRIPTION,
            "tid": 21,
            "copyright": 2,
            "is_season_display": True,
        },
        "public_tags": TAGS,
        "member_archive": {
            "aid": aid,
            "bvid": bvid,
            "title": TITLE,
            "desc": DESCRIPTION,
            "tag": ",".join(TAGS),
            "tid": 21,
            "copyright": 2,
            "source": "https://live.bilibili.com/",
        },
        "section_api": {
            "code": 0,
            "episode_match_count": 1,
            "episode_titles": [TITLE],
        },
        "problems": [],
        "verified_at": "2026-07-29T12:00:00+00:00",
    }
    season = {
        "schema_version": "authorized-upload-season-verify.v1",
        "status": "IN_SEASON_PUBLIC",
        "bvid": bvid,
        "title": TITLE,
        "aid": aid,
        "cid": published_cid,
        "verified_at": "2026-07-29T12:00:00+00:00",
    }
    _write_json(public_path, public)
    _write_json(season_path, season)
    uploaded = {
        "schema_version": "authorized-upload-result.v3",
        "status": "VERIFIED_PUBLIC",
        "bvid": bvid,
        "aid": aid,
        "cid": published_cid,
        "title": TITLE,
        "video_sha256": manifest["video"]["sha256"],
        "cover_sha256": manifest["cover"]["sha256"],
        "manifest": str(manifest_path.resolve()),
        "manifest_sha256": _sha(manifest_path),
        "public_verify": str(public_path.resolve()),
        "public_verify_sha256": _sha(public_path),
        "uploaded_at": "2026-07-29T12:01:00+00:00",
    }
    _write_json(uploaded_path, uploaded)
    _write_json(
        registry_path,
        {"schema_version": "publication-registry.v1", "entries": []},
    )
    if write_state:
        _write_json(
            state_path,
            {
                "status": "review_ready_with_failures",
                "picks": [
                    {
                        "candidate_id": candidate,
                        "cid": candidate,
                        "status": "candidate_rejected",
                        "rc": 1,
                        "failure_kind": "subtitle_authority",
                    }
                ],
                "songs": [],
            },
        )
    return {
        "base": base,
        "candidate": candidate,
        "bvid": bvid,
        "date": date,
        "manifest": manifest,
        "manifest_path": manifest_path,
        "public": public,
        "public_path": public_path,
        "season": season,
        "season_path": season_path,
        "uploaded": uploaded,
        "uploaded_path": uploaded_path,
        "registry_path": registry_path,
        "state_path": state_path,
    }


def _reconcile_new(fixture: dict, *, at: str = "2026-07-29T12:01:00+00:00"):
    return reconciliation.reconcile_new_bv_publication(
        manifest=fixture["manifest"],
        manifest_path=fixture["manifest_path"],
        bvid=fixture.get("bvid", BVID),
        public_verify_path=fixture["public_path"],
        season_verify_path=fixture["season_path"],
        uploaded_path=fixture["uploaded_path"],
        autoslice_base=fixture["base"],
        registry_path=fixture["registry_path"],
        reconciled_at=at,
    )


def _install_deployed_registry_authority(fixture: dict) -> Path:
    """Move the fixture registry below a manifest-bound deployed repo tree."""

    repo = fixture["base"].parent / "repo"
    registry = repo / "assets" / "lidousha" / "publication_registry.v1.json"
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_bytes(fixture["registry_path"].read_bytes())
    fixture["registry_path"].unlink()

    commit = "a" * 40
    (repo / "DEPLOYED_COMMIT").write_text(commit + "\n", encoding="utf-8")
    tree_entries = {
        "assets": {
            "type": "dir",
            "mode": stat.S_IMODE((repo / "assets").stat().st_mode),
        },
        "assets/lidousha": {
            "type": "dir",
            "mode": stat.S_IMODE(registry.parent.stat().st_mode),
        },
        "assets/lidousha/publication_registry.v1.json": {
            "type": "file",
            "mode": stat.S_IMODE(registry.stat().st_mode),
            "sha256": _sha(registry),
        },
    }
    tree_sha256 = hashlib.sha256(
        json.dumps(
            tree_entries,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    _write_json(
        repo / "DEPLOYED_MANIFEST.json",
        {
            "schema_version": "oci3-shadow-autoslice-repo-manifest.v1",
            "commit": commit,
            "entries": tree_entries,
            "tree_sha256": tree_sha256,
        },
    )
    authority = build_deployed_authority_manifest(
        repo_root=repo,
        deployed_commit=commit,
        relative_paths=[registry.relative_to(repo)],
    )
    _write_json(repo / "DEPLOYED_AUTHORITY_MANIFEST.json", authority)
    fixture["registry_path"] = registry
    return repo


def test_recording_date_prefers_canonical_package_layout_inside_recovery(
    tmp_path: Path,
) -> None:
    package = (
        tmp_path
        / "recovery"
        / "2026-08-09"
        / CANDIDATE
        / "repo"
        / "lidousha"
        / DATE
    )
    record = package / "clip.record.json"
    _write_json(
        record,
        {
            "story_contract": {"candidate_id": CANDIDATE},
            "delivery_candidate_id": CANDIDATE,
        },
    )
    manifest = {
        "package_attestation": {
            "package_root": str(package.resolve()),
            "record": _entry(record),
        }
    }

    assert reconciliation._candidate_and_date(manifest) == (  # noqa: SLF001
        CANDIDATE,
        DATE,
    )


def test_recording_date_rejects_conflicting_canonical_package_layouts(
    tmp_path: Path,
) -> None:
    package = (
        tmp_path
        / "out"
        / "2026-07-28"
        / CANDIDATE
        / "lidousha"
        / DATE
    )
    record = package / "clip.record.json"
    _write_json(
        record,
        {
            "story_contract": {"candidate_id": CANDIDATE},
            "delivery_candidate_id": CANDIDATE,
        },
    )
    manifest = {
        "package_attestation": {
            "package_root": str(package.resolve()),
            "record": _entry(record),
        }
    }

    with pytest.raises(
        reconciliation.PublicationReconciliationError,
        match="conflicting canonical recording dates",
    ):
        reconciliation._candidate_and_date(manifest)  # noqa: SLF001


def test_recording_date_qixi_portable_clone_uses_attested_review_date(
    tmp_path: Path,
) -> None:
    package = (
        tmp_path
        / "qixi-cover-successor-portable-v3"
        / "2026-08-24"
        / f"{CANDIDATE}-root-reviewed-20260824T123852Z"
        / "replacement_recuts"
    )
    record = package / f"{CANDIDATE}.record.json"
    review = package / "review_manifest.json"
    _write_json(record, {"story_contract": {"candidate_id": CANDIDATE}})
    _write_json(review, {"candidate_id": CANDIDATE, "date": DATE})
    manifest = {
        "package_attestation": {
            "package_root": str(package.resolve()),
            "record": _entry(record),
            "review_manifest": _entry(review),
        }
    }

    assert reconciliation._candidate_and_date(manifest) == (  # noqa: SLF001
        CANDIDATE,
        DATE,
    )


@pytest.mark.parametrize(
    ("review", "message"),
    [
        ({"candidate_id": "other", "date": DATE}, "candidate differs"),
        ({"candidate_id": CANDIDATE, "date": "not-a-date"}, "single valid"),
        (
            {
                "candidate_id": CANDIDATE,
                "date": DATE,
                "recording_date": "2026-07-28",
            },
            "single valid",
        ),
    ],
)
def test_recording_date_portable_clone_rejects_invalid_attested_review_manifest(
    tmp_path: Path, review: dict, message: str
) -> None:
    package = tmp_path / "portable" / "2026-08-24" / CANDIDATE
    record = package / f"{CANDIDATE}.record.json"
    review_path = package / "review_manifest.json"
    _write_json(record, {"story_contract": {"candidate_id": CANDIDATE}})
    _write_json(review_path, review)
    manifest = {
        "package_attestation": {
            "package_root": str(package.resolve()),
            "record": _entry(record),
            "review_manifest": _entry(review_path),
        }
    }

    with pytest.raises(reconciliation.PublicationReconciliationError, match=message):
        reconciliation._candidate_and_date(manifest)  # noqa: SLF001


def test_recording_date_portable_clone_rejects_review_manifest_hash_drift(
    tmp_path: Path,
) -> None:
    package = tmp_path / "portable" / "2026-08-24" / CANDIDATE
    record = package / f"{CANDIDATE}.record.json"
    review = package / "review_manifest.json"
    _write_json(record, {"story_contract": {"candidate_id": CANDIDATE}})
    _write_json(review, {"candidate_id": CANDIDATE, "date": DATE})
    manifest = {
        "package_attestation": {
            "package_root": str(package.resolve()),
            "record": _entry(record),
            "review_manifest": _entry(review),
        }
    }
    _write_json(review, {"candidate_id": CANDIDATE, "date": "2026-07-28"})

    with pytest.raises(
        reconciliation.PublicationReconciliationError, match="hash/bytes drifted"
    ):
        reconciliation._candidate_and_date(manifest)  # noqa: SLF001


def test_recording_date_legacy_wrapper_fallback_without_review_manifest(
    tmp_path: Path,
) -> None:
    package = tmp_path / "legacy" / DATE / CANDIDATE
    record = package / f"{CANDIDATE}.record.json"
    _write_json(record, {"story_contract": {"candidate_id": CANDIDATE}})
    manifest = {
        "package_attestation": {
            "package_root": str(package.resolve()),
            "record": _entry(record),
        }
    }

    assert reconciliation._candidate_and_date(manifest) == (  # noqa: SLF001
        CANDIDATE,
        DATE,
    )


def _c1_manifest_without_record() -> dict:
    title = "【李豆沙】经小李判断，薇欧拉对阿拉蕾就是铁暗恋！"
    tags = [
        "李豆沙", "虚拟主播", "虚拟UP主", "直播切片", "梦限大",
        "夢限大みゅーたいぷ", "BanG Dream", "邦多利", "磕CP", "上头",
    ]
    return {
        "manifest_version": 3,
        "schema_version": "authorized-upload-manifest.v3",
        "title": title,
        "tags": tags,
        "package_attestation": {
            "package_root": "/private/c1-formal-package",
            "review_manifest": {},
            "package_audit": {},
            "c1_technical_receipt": {},
        },
        "recovery_publication_authority": {
            "schema_version": "fastlane-c1-authorized-same-bv-projection.v1",
            "candidate_id": "auto_173005_934_1166",
            "recording_date": "2026-08-11",
            "bvid": "BV1os8q61Eya",
            "aid": 117132650155234,
            "cid": 41126267272,
            "title": title,
            "tags": tags,
            "same_bv_only": True,
        },
    }


def test_c1_reconciliation_resolves_missing_record_after_receipt_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = _c1_manifest_without_record()
    replayed: list[object] = []

    def validate(value: object) -> None:
        replayed.append(value)

    monkeypatch.setattr(
        reconciliation.fastlane_c1_technical_receipt,
        "validate_authorized_projection_manifest",
        validate,
    )
    assert reconciliation._candidate_and_date(manifest) == (  # noqa: SLF001
        "auto_173005_934_1166", "2026-08-11"
    )
    assert replayed == [manifest]


@pytest.mark.parametrize(
    "drift", ["receipt", "audit", "artifact", "authority", "title", "tags", "date", "candidate"]
)
def test_c1_reconciliation_rejects_any_failed_receipt_replay(
    monkeypatch: pytest.MonkeyPatch, drift: str
) -> None:
    manifest = _c1_manifest_without_record()

    def reject(_value: object) -> None:
        raise fastlane_c1_technical_receipt.C1TechnicalReceiptError(drift)

    monkeypatch.setattr(
        reconciliation.fastlane_c1_technical_receipt,
        "validate_authorized_projection_manifest",
        reject,
    )
    with pytest.raises(
        reconciliation.PublicationReconciliationError,
        match="C1 manifest cannot resolve publication candidate/date",
    ):
        reconciliation._candidate_and_date(manifest)  # noqa: SLF001


def test_generic_missing_record_keeps_original_error() -> None:
    with pytest.raises(
        reconciliation.PublicationReconciliationError,
        match="manifest record binding is missing",
    ):
        reconciliation._candidate_and_date({"package_attestation": {}})  # noqa: SLF001


def _exact_c2_manifest(tmp_path: Path) -> tuple[dict, Path, Path]:
    bridge = reconciliation.fastlane_c2_authorized_upload.bridge
    package = tmp_path / "portable-wrapper" / "package"
    record_path = package / bridge.RECORD_NAME
    review_path = package / "review_manifest.json"
    record = {
        "schema_version": "lidousha-c2-release-record.v1",
        "candidate_id": bridge.CID,
        "recording_date": bridge.DATE,
        "upload_allowed": False,
        "artifact_hashes": {},
        "c2_tag_generation_receipt": {},
        "legacy_execution_contract": {},
        "publish_staging": {"title": bridge.TITLE},
        "upload_tags": {},
    }
    review = {
        "schema_version": bridge.SCHEMA,
        "candidate_id": bridge.CID,
        "recording_date": bridge.DATE,
        "title": bridge.TITLE,
        "scope": "C2_NAMED_FASTLANE_NEW_BV_ONLY",
    }
    _write_json(record_path, record)
    _write_json(review_path, review)
    return (
        {
            "package_attestation": {
                "package_root": str(package.resolve()),
                "record": _entry(record_path),
                "review_manifest": _entry(review_path),
            }
        },
        record_path,
        review_path,
    )


def test_exact_verified_c2_uses_bridge_candidate_and_attested_review_date(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest, _record_path, _review_path = _exact_c2_manifest(tmp_path)
    bridge = reconciliation.fastlane_c2_authorized_upload.bridge
    audit_roots: list[Path] = []
    monkeypatch.setattr(
        bridge,
        "audit_fastlane_c2_release_package",
        lambda root: audit_roots.append(root) or [],
    )

    assert reconciliation._candidate_and_date(manifest) == (  # noqa: SLF001
        bridge.CID,
        bridge.DATE,
    )
    assert audit_roots == [Path(manifest["package_attestation"]["package_root"])]


def test_generic_top_level_candidate_only_remains_rejected(tmp_path: Path) -> None:
    package = tmp_path / "generic" / DATE
    record_path = package / "clip.record.json"
    _write_json(record_path, {"candidate_id": CANDIDATE})
    manifest = {
        "package_attestation": {
            "package_root": str(package.resolve()),
            "record": _entry(record_path),
        }
    }

    with pytest.raises(
        reconciliation.PublicationReconciliationError,
        match="no publication candidate_id",
    ):
        reconciliation._candidate_and_date(manifest)  # noqa: SLF001


@pytest.mark.parametrize("drift", ["record", "review", "package_root"])
def test_c2_bridge_candidate_resolution_rejects_identity_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, drift: str
) -> None:
    manifest, record_path, review_path = _exact_c2_manifest(tmp_path)
    bridge = reconciliation.fastlane_c2_authorized_upload.bridge
    monkeypatch.setattr(bridge, "audit_fastlane_c2_release_package", lambda _root: [])
    if drift == "record":
        payload = json.loads(record_path.read_text(encoding="utf-8"))
        payload["candidate_id"] = "other"
        _write_json(record_path, payload)
        manifest["package_attestation"]["record"] = _entry(record_path)
    elif drift == "review":
        payload = json.loads(review_path.read_text(encoding="utf-8"))
        payload["recording_date"] = "2026-08-12"
        _write_json(review_path, payload)
        manifest["package_attestation"]["review_manifest"] = _entry(review_path)
    else:
        manifest["package_attestation"]["package_root"] = str(
            tmp_path / "wrong-package"
        )

    with pytest.raises(reconciliation.PublicationReconciliationError):
        reconciliation._candidate_and_date(manifest)  # noqa: SLF001


def test_c2_rejects_external_or_symlinked_manifest_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest, record_path, _review_path = _exact_c2_manifest(tmp_path)
    bridge = reconciliation.fastlane_c2_authorized_upload.bridge
    monkeypatch.setattr(bridge, "audit_fastlane_c2_release_package", lambda _root: [])
    external = tmp_path / "external.record.json"
    external.write_bytes(record_path.read_bytes())
    manifest["package_attestation"]["record"] = _entry(external)

    with pytest.raises(
        reconciliation.PublicationReconciliationError,
        match="not the verified package record",
    ):
        reconciliation._candidate_and_date(manifest)  # noqa: SLF001

    # A symlinked attestation must fail by the same physical-member binding.
    linked = tmp_path / "linked.record.json"
    linked.symlink_to(record_path)
    manifest["package_attestation"]["record"] = {
        "path": str(linked),
        "sha256": _sha(record_path),
        "bytes": record_path.stat().st_size,
    }
    with pytest.raises(
        reconciliation.PublicationReconciliationError,
        match="not the verified package record",
    ):
        reconciliation._candidate_and_date(manifest)  # noqa: SLF001


@pytest.mark.parametrize(
    ("record", "expected"),
    [
        (
            {
                "story_contract": {"candidate_id": CANDIDATE},
                "delivery_candidate_id": "delivery-must-not-override-story",
            },
            CANDIDATE,
        ),
        ({"delivery_candidate_id": CANDIDATE}, CANDIDATE),
    ],
)
def test_generic_story_and_delivery_candidate_resolution_is_unchanged(
    tmp_path: Path, record: dict, expected: str
) -> None:
    package = tmp_path / "generic" / DATE
    record_path = package / "clip.record.json"
    _write_json(record_path, record)
    manifest = {
        "package_attestation": {
            "package_root": str(package.resolve()),
            "record": _entry(record_path),
        }
    }

    assert reconciliation._candidate_and_date(manifest) == (  # noqa: SLF001
        expected,
        DATE,
    )


@pytest.mark.parametrize("target", ["record", "review"])
def test_c2_rejects_record_or_review_hash_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    manifest, record_path, review_path = _exact_c2_manifest(tmp_path)
    bridge = reconciliation.fastlane_c2_authorized_upload.bridge
    monkeypatch.setattr(bridge, "audit_fastlane_c2_release_package", lambda _root: [])
    if target == "record":
        record_path.write_text("{}\n", encoding="utf-8")
    else:
        review_path.write_text(
            review_path.read_text(encoding="utf-8") + "\n",
            encoding="utf-8",
        )
    with pytest.raises(
        reconciliation.PublicationReconciliationError, match="hash/bytes drifted"
    ):
        reconciliation._candidate_and_date(manifest)  # noqa: SLF001


def test_new_bv_public_closure_reconciles_registry_and_failed_runner_row(
    tmp_path: Path,
) -> None:
    fixture = _new_bv_fixture(tmp_path)
    result = _reconcile_new(fixture)

    assert result["status"] == "VERIFIED_PUBLIC"
    state = json.loads(fixture["state_path"].read_text(encoding="utf-8"))
    row = state["picks"][0]
    assert state["status"] == "published"
    assert state["publication_closure"]["published_candidate_ids"] == [CANDIDATE]
    assert row["status"] == "published"
    assert row["prepublication_status"] == "candidate_rejected"
    assert row["cid"] == CANDIDATE
    assert row["published_cid"] == 2002
    assert row["failure_kind"] == "subtitle_authority"
    assert reconciliation.publication_row_is_verified(row)

    registry = json.loads(
        fixture["registry_path"].read_text(encoding="utf-8")
    )
    assert registry["entries"][0]["bvid"] == BVID
    runtime = json.loads(
        reconciliation.runtime_registry_path(fixture["base"]).read_text(
            encoding="utf-8"
        )
    )
    runtime_row = runtime["entries"][0]
    reconciliation.validate_runtime_registry_entry(runtime_row)


@pytest.mark.parametrize("write_state", [True, False])
def test_deployed_registry_uses_runtime_overlay_and_preserves_static_asset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    write_state: bool,
) -> None:
    fixture = _new_bv_fixture(tmp_path, write_state=write_state)
    repo = _install_deployed_registry_authority(fixture)
    static_before = fixture["registry_path"].read_bytes()
    static_mode_before = stat.S_IMODE(fixture["registry_path"].stat().st_mode)
    fixture_base = fixture["base"]

    result = _reconcile_new(fixture)

    assert result["registry_projection_mode"] == "DEPLOYED_RUNTIME_OVERLAY"
    assert result["registry_projection"]["mode"] == "DEPLOYED_RUNTIME_OVERLAY"
    assert result["registry_projection"]["relative_path"] == (
        "assets/lidousha/publication_registry.v1.json"
    )
    assert result["registry_readback"] == {
        "path": str(fixture["registry_path"].resolve()),
        "sha256": _sha(fixture["registry_path"]),
        "bytes": fixture["registry_path"].stat().st_size,
    }
    assert fixture["registry_path"].read_bytes() == static_before
    assert stat.S_IMODE(fixture["registry_path"].stat().st_mode) == static_mode_before

    runtime = json.loads(
        reconciliation.runtime_registry_path(fixture_base).read_text(
            encoding="utf-8"
        )
    )
    assert runtime["entries"][0]["bvid"] == BVID

    # The real read path uses the canonical default with the runtime overlay;
    # a custom explicit path remains an isolated static snapshot by contract.
    from src.autoslice import publication_registry

    monkeypatch.setattr(
        publication_registry, "DEFAULT_REGISTRY_PATH", fixture["registry_path"]
    )
    monkeypatch.setenv("AUTOSLICE_BASE", str(fixture_base))
    effective = publication_registry.load_publication_registry()
    assert any(
        row.get("candidate_id") == CANDIDATE
        and row.get("status") == "published"
        and row.get("bvid") == BVID
        for row in effective["entries"]
    )

    if write_state:
        assert result["state_paths"] == [str(fixture["state_path"].resolve())]
    else:
        assert result["state_paths"] == []
        assert result["day_state_status"] == "UNKNOWN"
        assert not fixture["state_path"].exists()
    assert repo.joinpath("DEPLOYED_COMMIT").is_file()


def test_deployed_registry_reconciliation_is_idempotent(
    tmp_path: Path,
) -> None:
    fixture = _new_bv_fixture(tmp_path)
    _install_deployed_registry_authority(fixture)
    static_before = fixture["registry_path"].read_bytes()

    first = _reconcile_new(fixture)
    runtime_path = reconciliation.runtime_registry_path(fixture["base"])
    runtime_before = runtime_path.read_bytes()
    state_before = fixture["state_path"].read_bytes()
    second = _reconcile_new(fixture)

    assert first["registry_projection_mode"] == "DEPLOYED_RUNTIME_OVERLAY"
    assert second["registry_projection_mode"] == "DEPLOYED_RUNTIME_OVERLAY"
    assert second["registry_readback"] == first["registry_readback"]
    assert fixture["registry_path"].read_bytes() == static_before
    assert runtime_path.read_bytes() == runtime_before
    assert fixture["state_path"].read_bytes() == state_before


@pytest.mark.parametrize(
    "marker",
    ["DEPLOYED_COMMIT", "DEPLOYED_AUTHORITY_MANIFEST.json"],
)
def test_deployed_registry_marker_drift_fails_before_projection_writes(
    tmp_path: Path,
    marker: str,
) -> None:
    fixture = _new_bv_fixture(tmp_path)
    repo = _install_deployed_registry_authority(fixture)
    (repo / marker).write_text("drift\n", encoding="utf-8")
    static_before = fixture["registry_path"].read_bytes()
    state_before = fixture["state_path"].read_bytes()

    with pytest.raises(
        reconciliation.PublicationReconciliationError,
        match="deployed publication registry",
    ):
        _reconcile_new(fixture)

    assert fixture["registry_path"].read_bytes() == static_before
    assert fixture["state_path"].read_bytes() == state_before
    assert not reconciliation.runtime_registry_path(fixture["base"]).exists()
    assert not reconciliation.publication_recovery_sidecar_path(
        fixture["base"], DATE
    ).exists()


def test_deployed_registry_asset_drift_fails_before_runtime_write(
    tmp_path: Path,
) -> None:
    fixture = _new_bv_fixture(tmp_path)
    repo = _install_deployed_registry_authority(fixture)
    registry = fixture["registry_path"]
    registry.write_bytes(registry.read_bytes() + b" \n")
    static_before = registry.read_bytes()
    state_before = fixture["state_path"].read_bytes()

    with pytest.raises(
        reconciliation.PublicationReconciliationError,
        match="deployed publication registry authority",
    ):
        _reconcile_new(fixture)

    assert registry.read_bytes() == static_before
    assert fixture["state_path"].read_bytes() == state_before
    assert not reconciliation.runtime_registry_path(fixture["base"]).exists()
    assert repo.joinpath("DEPLOYED_AUTHORITY_MANIFEST.json").is_file()


def test_tree_only_deployed_marker_does_not_fall_back_to_static_mutation(
    tmp_path: Path,
) -> None:
    fixture = _new_bv_fixture(tmp_path)
    repo = _install_deployed_registry_authority(fixture)
    (repo / "DEPLOYED_COMMIT").unlink()
    (repo / "DEPLOYED_AUTHORITY_MANIFEST.json").unlink()
    static_before = fixture["registry_path"].read_bytes()
    state_before = fixture["state_path"].read_bytes()

    with pytest.raises(
        reconciliation.PublicationReconciliationError,
        match="deployed publication registry marker",
    ):
        _reconcile_new(fixture)

    assert fixture["registry_path"].read_bytes() == static_before
    assert fixture["state_path"].read_bytes() == state_before
    assert not reconciliation.runtime_registry_path(fixture["base"]).exists()


def test_deployed_registry_symlink_fails_before_runtime_write(
    tmp_path: Path,
) -> None:
    fixture = _new_bv_fixture(tmp_path)
    _install_deployed_registry_authority(fixture)
    registry = fixture["registry_path"]
    target = registry.with_name("registry-target.json")
    target.write_bytes(registry.read_bytes())
    registry.unlink()
    registry.symlink_to(target)
    state_before = fixture["state_path"].read_bytes()

    with pytest.raises(
        reconciliation.PublicationReconciliationError,
        match="deployed publication registry authority",
    ):
        _reconcile_new(fixture)

    assert fixture["state_path"].read_bytes() == state_before
    assert not reconciliation.runtime_registry_path(fixture["base"]).exists()


@pytest.mark.parametrize("api_retry", [False, True])
def test_new_bv_reconciliation_is_idempotent_after_valid_sidecar_refresh(
    tmp_path: Path,
    api_retry: bool,
) -> None:
    fixture = _new_bv_fixture(tmp_path)
    fixture["season"].update(season_add_code=0, season_add_message="OK")
    _write_json(fixture["season_path"], fixture["season"])
    _reconcile_new(fixture)
    authority = reconciliation.authority_sidecar_path(
        fixture["manifest_path"]
    )
    authority_bytes = authority.read_bytes()

    fixture["public"]["verified_at"] = "2026-07-29T12:05:00+00:00"
    fixture["season"]["verified_at"] = "2026-07-29T12:05:00+00:00"
    if api_retry:
        fixture["public"]["public_tags"] = list(reversed(TAGS))
        fixture["season"].update(
            season_add_code=20080, season_add_message="当前稿件已存在在合集中"
        )
    _write_json(fixture["public_path"], fixture["public"])
    _write_json(fixture["season_path"], fixture["season"])
    fixture["uploaded"]["uploaded_at"] = "2026-07-29T12:05:00+00:00"
    fixture["uploaded"]["public_verify_sha256"] = _sha(
        fixture["public_path"]
    )
    _write_json(fixture["uploaded_path"], fixture["uploaded"])

    _reconcile_new(fixture, at="2026-07-29T12:05:00+00:00")
    assert authority.read_bytes() == authority_bytes
    state = json.loads(fixture["state_path"].read_text(encoding="utf-8"))
    assert (
        state["picks"][0]["publication_reconciliation"]["reconciled_at"]
        == "2026-07-29T12:01:00+00:00"
    )


@pytest.mark.parametrize(
    "conflict", ["tag", "duplicate_tag", "section", "season_error", "season_message"]
)
def test_new_bv_reconciliation_api_retry_still_rejects_evidence_conflicts(
    tmp_path: Path, conflict: str,
) -> None:
    fixture = _new_bv_fixture(tmp_path)
    fixture["season"].update(
        season_add_code=0, season_add_message="OK", section_id=123
    )
    _write_json(fixture["season_path"], fixture["season"])
    _reconcile_new(fixture)
    before_registry = fixture["registry_path"].read_bytes()
    before_state = fixture["state_path"].read_bytes()
    if conflict == "tag":
        fixture["public"]["public_tags"] = [*TAGS, "extra"]
    elif conflict == "duplicate_tag":
        fixture["public"]["public_tags"] = [*TAGS, TAGS[0]]
    elif conflict == "section":
        fixture["season"]["section_id"] = 456
    elif conflict == "season_message":
        fixture["season"]["season_add_message"] = "unexpected response"
    else:
        fixture["season"].update(season_add_code=-400, season_add_message="error")
    _write_json(fixture["public_path"], fixture["public"])
    _write_json(fixture["season_path"], fixture["season"])
    fixture["uploaded"]["public_verify_sha256"] = _sha(fixture["public_path"])
    _write_json(fixture["uploaded_path"], fixture["uploaded"])
    with pytest.raises(reconciliation.PublicationReconciliationError):
        _reconcile_new(fixture)
    assert fixture["registry_path"].read_bytes() == before_registry
    assert fixture["state_path"].read_bytes() == before_state


def test_new_bv_weak_public_proof_cannot_write_registry_or_state(
    tmp_path: Path,
) -> None:
    fixture = _new_bv_fixture(tmp_path)
    before_registry = fixture["registry_path"].read_bytes()
    before_state = fixture["state_path"].read_bytes()
    fixture["public"]["status"] = "PUBLIC_VERIFY_FAILED"
    _write_json(fixture["public_path"], fixture["public"])
    fixture["uploaded"]["public_verify_sha256"] = _sha(
        fixture["public_path"]
    )
    _write_json(fixture["uploaded_path"], fixture["uploaded"])

    with pytest.raises(reconciliation.PublicationReconciliationError):
        _reconcile_new(fixture)
    assert fixture["registry_path"].read_bytes() == before_registry
    assert fixture["state_path"].read_bytes() == before_state
    assert not reconciliation.runtime_registry_path(fixture["base"]).exists()


def test_conflicting_published_bvid_fails_before_projection(tmp_path: Path) -> None:
    fixture = _new_bv_fixture(tmp_path)
    conflicting = {
        "schema_version": "publication-registry.v1",
        "entries": [
            {
                "candidate_id": CANDIDATE,
                "recording_date": DATE,
                "status": "published",
                "bvid": "BV1DIFFERENT",
            }
        ],
    }
    _write_json(fixture["registry_path"], conflicting)
    before_state = fixture["state_path"].read_bytes()

    with pytest.raises(
        reconciliation.PublicationReconciliationError,
        match="BVID conflict",
    ):
        _reconcile_new(fixture)
    assert fixture["state_path"].read_bytes() == before_state
    assert not reconciliation.runtime_registry_path(fixture["base"]).exists()


def test_same_bv_completed_authority_reconciles_without_ledger(
    tmp_path: Path,
) -> None:
    fixture = _new_bv_fixture(tmp_path)
    _write_json(
        fixture["registry_path"],
        {
            "schema_version": "publication-registry.v1",
            "entries": [
                {
                    "candidate_id": CANDIDATE,
                    "recording_date": DATE,
                    "status": "published",
                    "bvid": BVID,
                    "cid": 100,
                }
            ],
        },
    )
    completed_path = fixture["manifest_path"].parent / "completed.json"
    completed = {
        "schema_version": "same-bv-repair-completed.v1",
        "status": "VERIFIED_FRESH_LIVE",
        "rc": 0,
        "verified_at": "2026-07-29T13:00:00+00:00",
        "candidate_id": CANDIDATE,
        "bvid": BVID,
        "aid": 1001,
        "new_cid": 3003,
        "manifest": {
            "path": str(fixture["manifest_path"].resolve()),
            "sha256": _sha(fixture["manifest_path"]),
        },
        "live_snapshot": {
            "creator": {
                "available": True,
                "bvid": BVID,
                "aid": 1001,
                "videos": [{"cid": 3003}],
            },
            "public": {
                "available": True,
                "bvid": BVID,
                "aid": 1001,
                "state": 0,
                "cid": 3003,
            },
            "section": {
                "available": True,
                "matches": [
                    {"bvid": BVID, "aid": 1001, "cid": 3003}
                ],
            },
        },
    }
    _write_json(completed_path, completed)

    result = reconciliation.reconcile_same_bv_publication(
        completed_path=completed_path,
        manifest=fixture["manifest"],
        manifest_path=fixture["manifest_path"],
        autoslice_base=fixture["base"],
        registry_path=fixture["registry_path"],
        reconciled_at=completed["verified_at"],
    )
    assert result["status"] == "VERIFIED_SAME_BV"
    state = json.loads(fixture["state_path"].read_text(encoding="utf-8"))
    assert state["picks"][0]["published_cid"] == 3003
    registry = json.loads(
        fixture["registry_path"].read_text(encoding="utf-8")
    )
    assert registry["entries"][0]["cid"] == 100
    assert registry["entries"][0]["bvid"] == BVID


def test_runtime_overlay_replays_verified_publication_into_state(
    tmp_path: Path,
) -> None:
    fixture = _new_bv_fixture(tmp_path)
    _reconcile_new(fixture)
    state = {
        "status": "review_ready_with_failures",
        "picks": [
            {
                "candidate_id": CANDIDATE,
                "cid": CANDIDATE,
                "status": "candidate_rejected",
                "rc": 1,
            }
        ],
        "songs": [],
    }
    assert reconciliation.apply_runtime_publications_to_state(
        date=DATE,
        state=state,
        autoslice_base=fixture["base"],
    )
    assert state["status"] == "published"
    assert state["picks"][0]["status"] == "published"


def test_verified_publication_is_terminal_for_exact_runner_and_reporting(
    tmp_path: Path,
) -> None:
    authority = tmp_path / "authority.json"
    _write_json(authority, {"status": "VERIFIED_PUBLIC"})
    publication = {
        "schema_version": "publication-reconciliation.v1",
        "status": "VERIFIED_PUBLIC",
        "candidate_id": CANDIDATE,
        "recording_date": DATE,
        "bvid": BVID,
        "authority": _entry(authority),
    }
    row = {
        "candidate_id": CANDIDATE,
        "cid": CANDIDATE,
        "status": "published",
        "bvid": BVID,
        "prepublication_status": "candidate_rejected",
        "publication_reconciliation": publication,
    }
    state = {
        "run_mode": "RECOVERY_REVIEW",
        "upload_allowed": False,
        "talk_selection_contract": {
            "schema_version": "talk-selection-contract.v1",
            "mode": "EXACT_CANDIDATE_SET_NO_BACKFILL",
            "authority": "维护者 selected exact recovery set",
            "source_state_sha256": "sha256:" + "1" * 64,
            "candidate_ids": [CANDIDATE],
        },
        "picks": [row],
        "songs": [],
        "pending_talk": [],
        "pending_song": [],
    }
    closure = exact_talk_contract_closure(state)
    assert closure["status"] == "COMPLETE"
    assert closure["rows"][0]["disposition"] == (
        "CURRENT_COMPLIANT_DELIVERY"
    )
    assert _current_compliant_delivery(row)

    result = project_terminal_batch_state(
        state,
        delivered_talk_statuses={"review_ready"},
        talk_failure_statuses={"failed"},
        cover_pending_status="cover_pending",
        exact_closure=closure,
        retry_epoch=None,
    )
    assert state["status"] == "published"
    assert result["delivered_talk"] == [row]


def test_same_bv_completed_sidecar_can_resume_only_local_reconciliation(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "manifest.json"
    _write_json(manifest_path, {"manifest_version": 3})
    plan_path = tmp_path / "plan.json"
    _write_json(plan_path, {})
    journal = tmp_path / "journal.jsonl"
    journal.write_text("journal\n", encoding="utf-8")
    out = tmp_path / "completed.json"
    snapshot = {
        "creator": {"videos": [{"cid": 3003}]},
        "public": {"cid": 3003},
        "section": {"matches": [{"cid": 3003}]},
    }
    plan = {
        "plan_id": "plan-1",
        "bvid": BVID,
        "manifest": {
            "path": str(manifest_path),
            "sha256": _sha(manifest_path),
        },
        "replacement": {},
        "season": {"section_id": 9110001},
        "recovery_publication_authority": {
            "candidate_id": CANDIDATE,
            "aid": 1001,
        },
    }
    row = {
        "state": "VERIFIED",
        "seq": 9,
        "at": "2026-07-29T12:00:00+00:00",
        "row_sha256": "row-sha",
        "details": {"live_snapshot": snapshot},
    }

    class Adapter:
        def observe(self, _bvid, _section_id):
            return snapshot

    @contextmanager
    def lock(_path):
        yield

    times = iter(
        ["2026-07-29T12:01:00+00:00", "2026-07-29T12:02:00+00:00"]
    )
    attempts = 0

    def reconcile_completed(**_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise reconciliation.PublicationReconciliationError(
                "temporary state write failure"
            )
        return {"status": "VERIFIED_SAME_BV"}

    def create_sidecar(path, payload):
        _write_json(path, payload)

    args = SimpleNamespace(
        plan=str(plan_path),
        journal=str(journal),
        out=str(out),
        lock=str(tmp_path / "upload.lock"),
        cookie_json=str(tmp_path / "cookie.json"),
        biliup_cookie_json=str(tmp_path / "biliup.json"),
    )
    kwargs = {
        "default_lock": tmp_path / "default.lock",
        "exclusive_lock": lock,
        "load_repair_manifest": lambda _path: (
            plan,
            {"manifest_version": 3},
            [],
        ),
        "status_reader": lambda **_kwargs: SimpleNamespace(state="VERIFIED"),
        "journal_entries": lambda *_args: [row],
        "adapter_factory": lambda *_args: Adapter(),
        "create_sidecar": create_sidecar,
        "sha256_file": _sha,
        "now": lambda: next(times),
        "observation_unavailable": RuntimeError,
        "snapshots_equal": lambda left, right: left == right,
        "reconcile_completed": reconcile_completed,
        "reconciliation_error": reconciliation.PublicationReconciliationError,
    }
    assert (
        same_bv_live_verification.run_repair_verify_live(args, **kwargs) == 6
    )
    first_bytes = out.read_bytes()
    assert (
        same_bv_live_verification.run_repair_verify_live(args, **kwargs) == 0
    )
    assert out.read_bytes() == first_bytes
    assert attempts == 2


def test_runner_read_state_persists_runtime_publication_overlay(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import scripts.session_autoslice as runner

    base = tmp_path / "autoslice"
    authority = tmp_path / "authority.json"
    _write_json(
        authority,
        {
            "schema_version": (
                "new-bv-publication-reconciliation-authority.v1"
            ),
            "status": "VERIFIED_PUBLIC",
            "candidate_id": CANDIDATE,
            "recording_date": DATE,
            "bvid": BVID,
            "aid": 1001,
            "cid": 2002,
        },
    )
    publication = {
        "schema_version": "publication-reconciliation.v1",
        "status": "VERIFIED_PUBLIC",
        "candidate_id": CANDIDATE,
        "recording_date": DATE,
        "bvid": BVID,
        "aid": 1001,
        "cid": 2002,
        "authority": _entry(authority),
        "reconciled_at": "2026-07-29T12:01:00+00:00",
    }
    _write_json(
        reconciliation.runtime_registry_path(base),
        {
            "schema_version": "publication-reconciliation-registry.v1",
            "entries": [
                {
                    "candidate_id": CANDIDATE,
                    "recording_date": DATE,
                    "status": "published",
                    "bvid": BVID,
                    "publication_reconciliation": publication,
                }
            ],
        },
    )
    state_path = base / "state" / f"{DATE}.json"
    _write_json(
        state_path,
        {
            "status": "review_ready_with_failures",
            "picks": [
                {
                    "candidate_id": CANDIDATE,
                    "cid": CANDIDATE,
                    "status": "candidate_rejected",
                    "rc": 1,
                }
            ],
            "songs": [],
        },
    )
    monkeypatch.setattr(runner, "BASE", base)

    state = runner.read_state(DATE)
    assert state["status"] == "published"
    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert persisted["picks"][0]["status"] == "published"


def test_missing_state_writes_entries_only_publication_recovery_projection(
    tmp_path: Path,
) -> None:
    fixture = _new_bv_fixture(tmp_path)
    fixture["state_path"].unlink()

    result = _reconcile_new(fixture)

    sidecar = reconciliation.publication_recovery_sidecar_path(
        fixture["base"], DATE
    )
    assert result["status"] == "VERIFIED_PUBLIC"
    assert result["state_paths"] == []
    assert result["publication_recovery_paths"] == [str(sidecar.resolve())]
    assert result["candidate_projection_status"] == "PUBLISHED_TARGET_ONLY"
    assert result["day_state_status"] == "UNKNOWN"
    assert not fixture["state_path"].exists()
    assert not fixture["state_path"].with_suffix(".json.bak").exists()

    projection = json.loads(sidecar.read_text(encoding="utf-8"))
    assert projection["schema_version"] == "publication-recovery-projection.v1"
    assert projection["projection_kind"] == "PUBLISHED_TARGET_ONLY"
    assert projection["recording_date"] == DATE
    assert projection["original_state_status"] == "MISSING"
    assert projection["original_candidate_set"] == "UNKNOWN"
    assert projection["day_completion"] == "UNKNOWN"
    assert set(projection) == {
        "schema_version",
        "projection_kind",
        "recording_date",
        "original_state_status",
        "original_candidate_set",
        "day_completion",
        "entries",
    }
    entry = projection["entries"][0]
    assert entry["candidate_id"] == CANDIDATE
    assert entry["cid"] == CANDIDATE
    assert entry["rc"] == 0
    assert isinstance(entry["published_cid"], int)
    assert entry["published_cid"] == 2002
    assert entry["bvid"] == BVID
    assert entry["publication_reconciliation"]["cid"] == 2002
    assert not any(
        key in projection
        for key in (
            "picks",
            "songs",
            "pending_talk",
            "pending_song",
            "publication_closure",
            "prepublication_status",
        )
    )


def test_missing_state_second_candidate_merges_and_rerun_is_idempotent(
    tmp_path: Path,
) -> None:
    first = _new_bv_fixture(tmp_path)
    first["state_path"].unlink()
    second = _new_bv_fixture(
        tmp_path,
        candidate="auto_225057_900_980",
        bvid="BV1SECOND0001",
        base=first["base"],
        write_state=False,
        aid=1002,
        published_cid=3003,
    )

    _reconcile_new(first)
    _reconcile_new(second)
    sidecar = reconciliation.publication_recovery_sidecar_path(
        first["base"], DATE
    )
    before = sidecar.read_bytes()
    projection = json.loads(before)
    assert [entry["candidate_id"] for entry in projection["entries"]] == [
        CANDIDATE,
        second["candidate"],
    ]
    assert {entry["published_cid"] for entry in projection["entries"]} == {
        2002,
        3003,
    }
    assert not first["state_path"].exists()
    assert not second["state_path"].exists()

    _reconcile_new(second, at="2026-07-29T13:00:00+00:00")
    assert sidecar.read_bytes() == before


@pytest.mark.parametrize("duplicate", [False, True])
def test_existing_state_without_unique_publication_target_is_rejected(
    tmp_path: Path, duplicate: bool
) -> None:
    fixture = _new_bv_fixture(tmp_path)
    state = json.loads(fixture["state_path"].read_text(encoding="utf-8"))
    if duplicate:
        state["songs"].append(dict(state["picks"][0]))
    else:
        state["picks"][0]["candidate_id"] = "other-candidate"
        state["picks"][0]["cid"] = "other-candidate"
    _write_json(fixture["state_path"], state)
    before_registry = fixture["registry_path"].read_bytes()

    with pytest.raises(
        reconciliation.PublicationReconciliationError,
        match="daily state does not contain exactly one published candidate",
    ):
        _reconcile_new(fixture)
    assert fixture["registry_path"].read_bytes() == before_registry
    assert not reconciliation.runtime_registry_path(fixture["base"]).exists()
    assert not reconciliation.publication_recovery_sidecar_path(
        fixture["base"], DATE
    ).exists()


def test_missing_state_sidecar_authority_drift_is_rejected_before_registry_write(
    tmp_path: Path,
) -> None:
    fixture = _new_bv_fixture(tmp_path)
    fixture["state_path"].unlink()
    _reconcile_new(fixture)
    sidecar = reconciliation.publication_recovery_sidecar_path(
        fixture["base"], DATE
    )
    projection = json.loads(sidecar.read_text(encoding="utf-8"))
    projection["entries"][0]["bvid"] = "BV1DRIFTED001"
    _write_json(sidecar, projection)
    before_registry = fixture["registry_path"].read_bytes()
    before_runtime = reconciliation.runtime_registry_path(
        fixture["base"]
    ).read_bytes()

    with pytest.raises(
        reconciliation.PublicationReconciliationError,
        match="mapping conflicts|authority/publication tuple conflicts|row is not a verified projection",
    ):
        _reconcile_new(fixture)
    assert fixture["registry_path"].read_bytes() == before_registry
    assert (
        reconciliation.runtime_registry_path(fixture["base"]).read_bytes()
        == before_runtime
    )


def test_missing_state_weak_public_proof_writes_no_projection(
    tmp_path: Path,
) -> None:
    fixture = _new_bv_fixture(tmp_path)
    fixture["state_path"].unlink()
    before_registry = fixture["registry_path"].read_bytes()
    fixture["public"]["status"] = "PUBLIC_VERIFY_FAILED"
    _write_json(fixture["public_path"], fixture["public"])
    fixture["uploaded"]["public_verify_sha256"] = _sha(fixture["public_path"])
    _write_json(fixture["uploaded_path"], fixture["uploaded"])

    with pytest.raises(reconciliation.PublicationReconciliationError):
        _reconcile_new(fixture)
    assert fixture["registry_path"].read_bytes() == before_registry
    assert not reconciliation.runtime_registry_path(fixture["base"]).exists()
    assert not reconciliation.publication_recovery_sidecar_path(
        fixture["base"], DATE
    ).exists()
    assert not fixture["state_path"].exists()


@pytest.mark.parametrize("backup_kind", ["regular", "directory", "dangling_symlink"])
def test_missing_primary_with_backup_requires_real_backup_restore(
    tmp_path: Path, backup_kind: str,
) -> None:
    fixture = _new_bv_fixture(tmp_path)
    backup = fixture["state_path"].with_suffix(".json.bak")
    backup.parent.mkdir(parents=True, exist_ok=True)
    if backup_kind == "regular":
        backup.write_bytes(fixture["state_path"].read_bytes())
    elif backup_kind == "directory":
        backup.mkdir()
    else:
        backup.symlink_to(backup.parent / "missing-backup-target")
    fixture["state_path"].unlink()
    before_registry = fixture["registry_path"].read_bytes()

    with pytest.raises(
        reconciliation.PublicationReconciliationError,
        match=r"primary is missing but \.bak exists",
    ):
        _reconcile_new(fixture)
    assert fixture["registry_path"].read_bytes() == before_registry
    assert not reconciliation.runtime_registry_path(fixture["base"]).exists()
    assert not reconciliation.publication_recovery_sidecar_path(
        fixture["base"], DATE
    ).exists()


def test_runner_missing_state_returns_in_memory_block_then_overlays_restored_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import scripts.session_autoslice as runner

    fixture = _new_bv_fixture(tmp_path)
    fixture["state_path"].unlink()
    _reconcile_new(fixture)
    monkeypatch.setattr(runner, "BASE", fixture["base"])

    blocked = runner.read_state(DATE)
    assert blocked["status"] == "publication_reconciliation_blocked"
    assert blocked["original_state_status"] == "MISSING"
    assert blocked["original_candidate_set"] == "UNKNOWN"
    assert blocked["day_completion"] == "UNKNOWN"
    assert blocked["state_paths"] == []
    assert "original_state_status=MISSING" in blocked[
        "publication_reconciliation_error"
    ]
    assert not fixture["state_path"].exists()

    _write_json(
        fixture["state_path"],
        {
            "status": "review_ready_with_failures",
            "picks": [
                {
                    "candidate_id": CANDIDATE,
                    "cid": CANDIDATE,
                    "status": "candidate_rejected",
                    "rc": 1,
                }
            ],
            "songs": [],
        },
    )
    restored = runner.read_state(DATE)
    assert restored["status"] == "published"
    assert restored["picks"][0]["published_cid"] == 2002
    assert restored["picks"][0]["cid"] == CANDIDATE


def test_runner_missing_primary_with_unusable_backup_stays_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import scripts.session_autoslice as runner

    fixture = _new_bv_fixture(tmp_path)
    backup = fixture["state_path"].with_suffix(".json.bak")
    fixture["state_path"].unlink()
    backup.write_text("{not-json", encoding="utf-8")
    monkeypatch.setattr(runner, "BASE", fixture["base"])

    blocked = runner.read_state(DATE)

    assert blocked["status"] == "state_corrupt_blocked"
    assert "primary is missing" in blocked["state_error"]
    assert fixture["state_path"].with_suffix(".json.bak").exists()


def test_publication_recovery_sidecar_does_not_make_date_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import scripts.session_autoslice as runner

    fixture = _new_bv_fixture(tmp_path)
    fixture["state_path"].unlink()
    _reconcile_new(fixture)
    monkeypatch.setattr(runner, "BASE", fixture["base"])

    assert not runner._start_date_is_terminal(DATE)  # noqa: SLF001


def _repair_missing_state_fixture(tmp_path: Path) -> dict:
    fixture = _new_bv_fixture(
        tmp_path, write_state=False, date="2099-01-02",
        candidate="fixture_missing_state_repair", bvid="BV1TEST000001",
        aid=6000001, published_cid=7000001,
    )
    _install_deployed_registry_authority(fixture)
    _reconcile_new(fixture)
    manifest_path = fixture["manifest_path"].with_name("repair-manifest.json")
    _write_json(manifest_path, fixture["manifest"])
    old_cid, new_cid = 7000001, 7000002
    aid, bvid = 6000001, fixture["bvid"]

    def snapshot(cid):
        return {
            "creator": {"available": True, "bvid": bvid, "aid": aid,
                        "videos": [{"cid": cid}]},
            "public": {"available": True, "bvid": bvid, "aid": aid,
                       "cid": cid, "state": 0},
            "section": {"available": True, "section_id": 9110001,
                        "matches": [{"bvid": bvid, "aid": aid, "cid": cid}]},
        }

    plan_path = manifest_path.with_name("repair.plan.json")
    plan = {
        "schema_version": "same-bv-repair-plan.v2",
        "plan_id": "fixture-repair-continuity",
        "bvid": bvid,
        "manifest": {"path": str(manifest_path), "sha256": _sha(manifest_path)},
        "replacement": {"video": fixture["manifest"]["video"],
                        "cover": fixture["manifest"]["cover"]},
        "recovery_publication_authority": {
            "schema_version": "recovery-same-bv-publication-authority.v1",
            "candidate_id": fixture["candidate"], "bvid": bvid, "aid": aid,
            "cid": old_cid,
        },
        "before": snapshot(old_cid),
    }
    _write_json(plan_path, plan)
    completed_path = manifest_path.with_name("repair.completed.json")
    completed = {
        "schema_version": "same-bv-repair-completed.v1",
        "status": "VERIFIED_FRESH_LIVE", "rc": 0, "remote_mutation": False,
        "candidate_id": fixture["candidate"], "bvid": bvid, "aid": aid,
        "new_cid": new_cid, "verified_at": "2099-01-03T10:25:33+0000",
        "manifest": plan["manifest"], "replacement": plan["replacement"],
        "plan": {"path": str(plan_path), "sha256": _sha(plan_path),
                 "plan_id": plan["plan_id"]},
        "live_snapshot": snapshot(new_cid),
    }
    _write_json(completed_path, completed)
    fixture.update(
        repair_manifest_path=manifest_path, completed_path=completed_path,
        completed=completed, plan=plan, plan_path=plan_path,
        recovery_path=reconciliation.publication_recovery_sidecar_path(
            fixture["base"], fixture["date"]
        ),
    )
    return fixture


def _reconcile_missing_state_repair(fixture):
    return reconciliation.reconcile_same_bv_publication(
        completed_path=fixture["completed_path"],
        manifest=fixture["manifest"],
        manifest_path=fixture["repair_manifest_path"],
        autoslice_base=fixture["base"],
        registry_path=fixture["registry_path"],
        reconciled_at=fixture["completed"]["verified_at"],
    )


def test_verified_same_bv_replaces_missing_state_projection_and_resumes(tmp_path):
    fixture = _repair_missing_state_fixture(tmp_path)
    static_before = fixture["registry_path"].read_bytes()
    before = json.loads(fixture["recovery_path"].read_text())
    result = _reconcile_missing_state_repair(fixture)
    assert result["status"] == "VERIFIED_SAME_BV"
    assert result["day_state_status"] == "UNKNOWN"
    after = json.loads(fixture["recovery_path"].read_text())
    assert {k: v for k, v in before.items() if k != "entries"} == {
        k: v for k, v in after.items() if k != "entries"
    }
    row = after["entries"][0]
    assert row["published_cid"] == 7000002
    assert row["cid"] == fixture["candidate"]
    assert row["publication_reconciliation"]["status"] == "VERIFIED_SAME_BV"
    assert row["publication_reconciliation"]["authority"]["sha256"] == _sha(
        fixture["completed_path"]
    )
    assert fixture["registry_path"].read_bytes() == static_before
    assert not fixture["state_path"].exists()
    assert not fixture["state_path"].with_suffix(".json.bak").exists()
    sidecar_before = fixture["recovery_path"].read_bytes()
    _reconcile_missing_state_repair(fixture)
    assert fixture["recovery_path"].read_bytes() == sidecar_before
    runtime = json.loads(
        reconciliation.runtime_registry_path(fixture["base"]).read_text()
    )
    assert runtime["entries"][0]["publication_reconciliation"]["cid"] == 7000002


@pytest.mark.parametrize("defect", [
    "before_public_cid", "before_creator_cid", "before_section_cid",
    "before_bvid", "before_aid", "authority_candidate", "authority_bvid",
    "plan_id", "plan_hash", "plan_manifest", "unverified_completion",
    "completion_candidate", "completion_new_cid", "completion_bvid",
])
def test_missing_state_repair_rejects_unproven_transition_before_any_write(tmp_path, defect):
    fixture = _repair_missing_state_fixture(tmp_path)
    plan, completed = fixture["plan"], fixture["completed"]
    if defect == "before_public_cid":
        plan["before"]["public"]["cid"] += 1
    elif defect == "before_creator_cid":
        plan["before"]["creator"]["videos"][0]["cid"] += 1
    elif defect == "before_section_cid":
        plan["before"]["section"]["matches"][0]["cid"] += 1
    elif defect in {"before_bvid", "before_aid"}:
        key = defect.removeprefix("before_")
        for row in (plan["before"]["creator"], plan["before"]["public"],
                    plan["before"]["section"]["matches"][0]):
            row[key] = "BV_WRONG" if key == "bvid" else 9999
    elif defect in {"authority_candidate", "authority_bvid"}:
        key = "candidate_id" if defect.endswith("candidate") else "bvid"
        plan["recovery_publication_authority"][key] = "WRONG"
    elif defect == "plan_id":
        completed["plan"]["plan_id"] = "different-plan"
    elif defect == "plan_manifest":
        plan["manifest"] = {**plan["manifest"], "sha256": "0" * 64}
    elif defect == "unverified_completion":
        completed["status"] = "PUBLIC_PENDING"
    elif defect == "completion_candidate":
        completed["candidate_id"] = "wrong-candidate"
    elif defect == "completion_new_cid":
        completed["new_cid"] += 1
    elif defect == "completion_bvid":
        completed["bvid"] = "BV_WRONG"
    _write_json(fixture["plan_path"], plan)
    completed["plan"]["sha256"] = (
        "0" * 64 if defect == "plan_hash" else _sha(fixture["plan_path"])
    )
    _write_json(fixture["completed_path"], completed)
    paths = [fixture["recovery_path"], fixture["registry_path"],
             reconciliation.runtime_registry_path(fixture["base"])]
    before = {path: path.read_bytes() for path in paths}
    with pytest.raises(reconciliation.PublicationReconciliationError):
        _reconcile_missing_state_repair(fixture)
    assert {path: path.read_bytes() for path in paths} == before
    assert not fixture["state_path"].exists()
    assert not fixture["state_path"].with_suffix(".json.bak").exists()


def test_missing_state_repair_preserves_unrelated_rows_and_allows_next_repair(tmp_path):
    fixture = _repair_missing_state_fixture(tmp_path)
    other = _new_bv_fixture(
        tmp_path / "other", candidate="another-candidate", bvid="BV_OTHER",
        base=fixture["base"], date=fixture["date"], write_state=False,
    )
    other["registry_path"] = fixture["registry_path"]
    _reconcile_new(other)
    old_projection = json.loads(fixture["recovery_path"].read_text())
    other_before = next(r for r in old_projection["entries"]
                        if r["candidate_id"] == "another-candidate")
    _reconcile_missing_state_repair(fixture)
    completed = json.loads(fixture["completed_path"].read_text())
    plan = json.loads(fixture["plan_path"].read_text())
    plan["plan_id"] = "second-verified-repair"
    plan["before"] = json.loads(json.dumps(completed["live_snapshot"]))
    new_cid = completed["new_cid"] + 1
    completed["new_cid"] = new_cid
    completed["live_snapshot"]["creator"]["videos"][0]["cid"] = new_cid
    completed["live_snapshot"]["public"]["cid"] = new_cid
    completed["live_snapshot"]["section"]["matches"][0]["cid"] = new_cid
    plan_path = fixture["plan_path"].with_name("second.plan.json")
    completed_path = fixture["completed_path"].with_name("second.completed.json")
    _write_json(plan_path, plan)
    completed["plan"] = {"path": str(plan_path), "sha256": _sha(plan_path),
                         "plan_id": plan["plan_id"]}
    _write_json(completed_path, completed)
    fixture.update(completed_path=completed_path, completed=completed)
    _reconcile_missing_state_repair(fixture)
    result = json.loads(fixture["recovery_path"].read_text())
    assert next(r for r in result["entries"]
                if r["candidate_id"] == "another-candidate") == other_before
    assert next(r for r in result["entries"]
                if r["candidate_id"] == fixture["candidate"])["published_cid"] == new_cid
    assert result["day_completion"] == result["original_candidate_set"] == "UNKNOWN"
    assert not fixture["state_path"].exists()


def test_missing_state_repair_resumes_after_runtime_write(tmp_path, monkeypatch):
    fixture = _repair_missing_state_fixture(tmp_path)
    upsert = reconciliation._upsert_publication_recovery_sidecar
    before = fixture["recovery_path"].read_bytes()

    def fail_sidecar(*args, **kwargs):
        raise OSError("simulated stop after registry write")

    monkeypatch.setattr(reconciliation, "_upsert_publication_recovery_sidecar", fail_sidecar)
    with pytest.raises(OSError, match="simulated stop"):
        _reconcile_missing_state_repair(fixture)
    assert fixture["recovery_path"].read_bytes() == before
    runtime = json.loads(
        reconciliation.runtime_registry_path(fixture["base"]).read_text()
    )
    assert runtime["entries"][0]["publication_reconciliation"]["cid"] == 7000002
    monkeypatch.setattr(reconciliation, "_upsert_publication_recovery_sidecar", upsert)
    _reconcile_missing_state_repair(fixture)
    assert json.loads(fixture["recovery_path"].read_text())["entries"][0]["published_cid"] == 7000002
    assert not fixture["state_path"].exists()


def test_original_review_uses_registered_item_identity_not_missing_top_level(tmp_path, monkeypatch):
    from src.autoslice import original_patch_package

    package = tmp_path / "portable-original"
    record = package / "record.json"
    review_path = package / "review_manifest.json"
    authority = {"candidate_id": CANDIDATE, "recording_date": DATE, "bvid": "BV1X9Yn6aEjs"}
    _write_json(
        record,
        {
            "story_contract": {"candidate_id": CANDIDATE},
            "recovery_publication_authority": authority,
        },
    )
    review = {
        "schema_version": original_patch_package.MANIFEST_SCHEMA,
        "date": DATE,
        "items": [{"candidate_id": CANDIDATE, "recovery_publication_authority": authority}],
    }
    _write_json(review_path, review)
    manifest = {
        "title": "synthetic",
        "recovery_publication_authority": authority,
        "package_attestation": {
            "package_root": str(package),
            "record": _entry(record),
            "review_manifest": _entry(review_path),
        },
    }
    checked = []

    def validate(value, **kwargs):
        checked.append((value, kwargs))
        assert value == authority
        return authority

    monkeypatch.setattr(original_patch_package, "validate_publication", validate)
    assert reconciliation._candidate_and_date(manifest) == (CANDIDATE, DATE)
    assert len(checked) == 1
    review["items"][0]["candidate_id"] = "other"
    _write_json(review_path, review)
    manifest["package_attestation"]["review_manifest"] = _entry(review_path)
    with pytest.raises(reconciliation.PublicationReconciliationError, match="candidate/authority"):
        reconciliation._candidate_and_date(manifest)
    review["items"][0]["candidate_id"] = CANDIDATE
    review["date"] = "2026-09-09"
    _write_json(review_path, review)
    manifest["package_attestation"]["review_manifest"] = _entry(review_path)
    with pytest.raises(
        reconciliation.PublicationReconciliationError, match="recording date differs"
    ):
        reconciliation._candidate_and_date(manifest)


@pytest.mark.parametrize("valid", [True, False])
def test_original_repair_reconciles_missing_state_only_with_registered_authority(tmp_path, monkeypatch, valid):
    from src.autoslice import original_patch_package

    fixture = _repair_missing_state_fixture(tmp_path)
    authority = dict(fixture["plan"]["recovery_publication_authority"])
    authority["schema_version"] = "original-fastlane-authorized-same-bv.v1"
    fixture["manifest"]["recovery_publication_authority"] = authority
    fixture["plan"]["recovery_publication_authority"] = authority
    _write_json(fixture["repair_manifest_path"], fixture["manifest"])
    fixture["plan"]["manifest"]["sha256"] = _sha(fixture["repair_manifest_path"])
    _write_json(fixture["plan_path"], fixture["plan"])
    fixture["completed"]["plan"]["sha256"] = _sha(fixture["plan_path"])
    _write_json(fixture["completed_path"], fixture["completed"])
    checked = []

    def validate(value, **kwargs):
        checked.append(kwargs)
        if not valid:
            raise original_patch_package.OriginalPackageError("unregistered target")
        assert value == authority
        assert kwargs["candidate_id"] == fixture["candidate"]
        return authority

    monkeypatch.setattr(original_patch_package, "validate_publication", validate)
    preserved = fixture["recovery_path"].read_bytes()
    if valid:
        result = _reconcile_missing_state_repair(fixture)
        assert result["status"] == "VERIFIED_SAME_BV"
        row = json.loads(fixture["recovery_path"].read_text())["entries"][0]
        assert row["published_cid"] == fixture["completed"]["new_cid"]
        assert row["cid"] == fixture["candidate"]
        assert not fixture["state_path"].exists()
        after = fixture["recovery_path"].read_bytes()
        _reconcile_missing_state_repair(fixture)
        assert fixture["recovery_path"].read_bytes() == after
    else:
        with pytest.raises(reconciliation.PublicationReconciliationError):
            _reconcile_missing_state_repair(fixture)
        assert fixture["recovery_path"].read_bytes() == preserved
    assert checked


def test_original_successor_authority_must_equal_manifest(monkeypatch):
    from src.autoslice import original_patch_package

    def unexpected(*args, **kwargs):
        pytest.fail("mismatched manifest must not reach authority loader")

    monkeypatch.setattr(original_patch_package, "validate_publication", unexpected)
    assert not reconciliation._repair_successor_authority_valid(
        {"schema_version": "original-fastlane-authorized-same-bv.v1"}, {}, {}
    )
    assert not reconciliation._repair_successor_authority_valid(
        {"schema_version": "unrecognized-self-approved"}, {}, {}
    )
