"""Unit tests for the C2-only authorized-upload authority resolver."""
from __future__ import annotations

import json

from src.autoslice import fastlane_c2_authorized_upload as c2_upload


def _write_c2_identity(root, *, record_schema="lidousha-c2-release-record.v1"):
    root.mkdir(exist_ok=True)
    bridge = c2_upload.bridge
    record = {
        "schema_version": record_schema,
        "candidate_id": bridge.CID,
        "recording_date": bridge.DATE,
        "upload_allowed": False,
        "artifact_hashes": {},
        "publish_staging": {"title": bridge.TITLE},
        "legacy_execution_contract": {},
        "upload_tags": {},
        "c2_tag_generation_receipt": {},
    }
    review = {
        "schema_version": bridge.SCHEMA,
        "candidate_id": bridge.CID,
        "recording_date": bridge.DATE,
        "title": bridge.TITLE,
        "scope": "C2_NAMED_FASTLANE_NEW_BV_ONLY",
    }
    (root / bridge.RECORD_NAME).write_text(
        json.dumps(record, ensure_ascii=False), encoding="utf-8"
    )
    (root / "review_manifest.json").write_text(
        json.dumps(review, ensure_ascii=False), encoding="utf-8"
    )
    return record


def test_resolver_accepts_only_a_passing_exact_c2_bridge(tmp_path, monkeypatch):
    record = _write_c2_identity(tmp_path)
    seen = []
    monkeypatch.setattr(
        c2_upload.bridge,
        "audit_fastlane_c2_release_package",
        lambda root: seen.append(root) or [],
    )

    assert c2_upload.verified_c2_release_candidate_id(tmp_path, record) == c2_upload.bridge.CID
    assert seen == [tmp_path]


def test_resolver_rejects_bridge_exception(tmp_path, monkeypatch):
    record = _write_c2_identity(tmp_path)
    monkeypatch.setattr(
        c2_upload.bridge,
        "audit_fastlane_c2_release_package",
        lambda _root: (_ for _ in ()).throw(OSError("unreadable")),
    )

    assert c2_upload.verified_c2_release_candidate_id(tmp_path, record) is None


def test_resolver_rejects_record_from_a_different_root(tmp_path, monkeypatch):
    expected_root = tmp_path / "expected"
    other_root = tmp_path / "other"
    record = _write_c2_identity(expected_root)
    other_record = _write_c2_identity(other_root)
    other_record["recording_date"] = "2026-08-14"
    (other_root / c2_upload.bridge.RECORD_NAME).write_text(
        json.dumps(other_record, ensure_ascii=False), encoding="utf-8"
    )
    monkeypatch.setattr(c2_upload.bridge, "audit_fastlane_c2_release_package", lambda _root: [])

    assert c2_upload.verified_c2_release_candidate_id(other_root, record) is None


def test_resolver_rejects_wrong_c2_record_schema(tmp_path, monkeypatch):
    record = _write_c2_identity(tmp_path, record_schema="lidousha-c2-release-record.v0")
    monkeypatch.setattr(c2_upload.bridge, "audit_fastlane_c2_release_package", lambda _root: [])

    assert c2_upload.verified_c2_release_candidate_id(tmp_path, record) is None


def test_resolver_rejects_ordinary_talk_record(tmp_path, monkeypatch):
    record = {
        "schema_version": "lidousha-record.v1",
        "candidate_id": "ordinary-talk",
        "recording_date": "2026-08-13",
        "upload_allowed": False,
        "publish_staging": {"title": "ordinary"},
    }
    monkeypatch.setattr(
        c2_upload.bridge,
        "audit_fastlane_c2_release_package",
        lambda _root: (_ for _ in ()).throw(AssertionError("must not replay C2")),
    )

    assert c2_upload.verified_c2_release_candidate_id(tmp_path, record) is None
    assert c2_upload.candidate_id_from_record(tmp_path, record) == ""


def test_generic_candidate_resolution_does_not_call_the_c2_bridge(tmp_path, monkeypatch):
    record = {"story_contract": {"candidate_id": "ordinary-talk"}}
    monkeypatch.setattr(
        c2_upload,
        "verified_c2_release_candidate_id",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not resolve C2")),
    )

    assert c2_upload.candidate_id_from_record(tmp_path, record) == "ordinary-talk"
