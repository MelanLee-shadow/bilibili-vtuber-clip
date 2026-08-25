from __future__ import annotations

import hashlib
import json
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


def _new_bv_fixture(tmp_path: Path) -> dict:
    base = tmp_path / "autoslice"
    package = base / "out" / DATE / CANDIDATE / "final"
    package.mkdir(parents=True)
    video = package / "clip.mp4"
    cover = package / "clip.cover.png"
    record = package / "clip.record.json"
    manifest_path = package / "clip.upload_manifest.json"
    public_path = package / "clip.public_verify.json"
    season_path = package / "clip.season_verify.json"
    uploaded_path = package / "clip.uploaded.json"
    registry_path = tmp_path / "publication_registry.v1.json"
    state_path = base / "state" / f"{DATE}.json"
    video.write_bytes(b"video")
    cover.write_bytes(b"cover")
    _write_json(
        record,
        {
            "story_contract": {"candidate_id": CANDIDATE},
            "delivery_candidate_id": CANDIDATE,
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
        "bvid": BVID,
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
            "aid": 1001,
            "cid": 2002,
            "title": TITLE,
            "desc": DESCRIPTION,
            "tid": 21,
            "copyright": 2,
            "is_season_display": True,
        },
        "public_tags": TAGS,
        "member_archive": {
            "aid": 1001,
            "bvid": BVID,
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
        "bvid": BVID,
        "title": TITLE,
        "aid": 1001,
        "cid": 2002,
        "verified_at": "2026-07-29T12:00:00+00:00",
    }
    _write_json(public_path, public)
    _write_json(season_path, season)
    uploaded = {
        "schema_version": "authorized-upload-result.v3",
        "status": "VERIFIED_PUBLIC",
        "bvid": BVID,
        "aid": 1001,
        "cid": 2002,
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
                    "failure_kind": "subtitle_authority",
                }
            ],
            "songs": [],
        },
    )
    return {
        "base": base,
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
        bvid=BVID,
        public_verify_path=fixture["public_path"],
        season_verify_path=fixture["season_path"],
        uploaded_path=fixture["uploaded_path"],
        autoslice_base=fixture["base"],
        registry_path=fixture["registry_path"],
        reconciled_at=at,
    )


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


def test_new_bv_reconciliation_is_idempotent_after_valid_sidecar_refresh(
    tmp_path: Path,
) -> None:
    fixture = _new_bv_fixture(tmp_path)
    _reconcile_new(fixture)
    authority = reconciliation.authority_sidecar_path(
        fixture["manifest_path"]
    )
    authority_bytes = authority.read_bytes()

    fixture["public"]["verified_at"] = "2026-07-29T12:05:00+00:00"
    fixture["season"]["verified_at"] = "2026-07-29T12:05:00+00:00"
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
            "authority": "Ivan selected exact recovery set",
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
    import scripts.free_session_autoslice as runner

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
