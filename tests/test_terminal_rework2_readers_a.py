"""Compressed review readers for the first H migration batch."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest


def _compact_chat(tmp_path: Path) -> tuple[dict, Path, bytes]:
    from src.autoslice.review_evidence_store import persist_document

    review = {
        "schema_version": "final-review-audit.v2",
        "status": "CLEAN",
        "findings": [],
        "request": {"payload": "compressed reader evidence" * 2_000},
    }
    document = {
        "schema_version": "chat-authority-audit.v2",
        "candidate_id": "reader-fixture",
        "speaker_manifest_sha256": "a" * 64,
        "final_review_audit": review,
    }
    path = tmp_path / "reader-fixture.chat-authority.json"
    persist_document(path, document)
    raw = path.read_bytes()
    assert b"review-evidence-reference.v1" in raw
    return document, path, raw


@pytest.mark.parametrize(
    "name, reader",
    [
        (
            "b2-caption",
            lambda raw, path: __import__(
                "src.autoslice.b2_caption_formal_successor",
                fromlist=["_load_json_bytes"],
            )._load_json_bytes(
                raw,
                reason_code="TEST_REVIEW",
                review=True,
                directory=path.parent,
            ),
        ),
        (
            "b2-current-delivery",
            lambda raw, path: __import__(
                "src.autoslice.b2_current_delivery_refresh", fromlist=["_json"]
            )._json(path, label="chat", review=True),
        ),
        (
            "b2-record-publish",
            lambda raw, path: __import__(
                "src.autoslice.b2_record_publish_mirror_successor",
                fromlist=["_read_json"],
            )._read_json(
                path,
                root=path.parent,
                label="chat",
                review=True,
            ),
        ),
        (
            "b2-v7",
            lambda raw, path: __import__(
                "src.autoslice.b2_v7_recovery_successor",
                fromlist=["_decode_role_document"],
            )._decode_role_document(
                raw, role="chat_authority", directory=path.parent
            ),
        ),
        (
            "e353",
            lambda raw, path: __import__(
                "src.autoslice.e353_content_formal_successor", fromlist=["_json"]
            )._json(
                raw,
                "TEST_REVIEW",
                review=True,
                directory=path.parent,
            ),
        ),
        (
            "package-import",
            lambda raw, path: __import__(
                "src.autoslice.package_import", fromlist=["load_json_document"]
            ).load_json_document(path, label="chat", review=True)[0],
        ),
        (
            "package-relocation",
            lambda raw, path: __import__(
                "src.autoslice.package_relocation",
                fromlist=["_chat_speaker_manifest_digest"],
            )._chat_speaker_manifest_digest(raw, directory=path.parent),
        ),
        (
            "qixi-finalization",
            lambda raw, path: __import__(
                "src.autoslice.qixi_corrected_package_finalization",
                fromlist=["_load_object"],
            )._load_object(path, label="chat", review=True),
        ),
        (
            "qixi-terminal-refresh",
            lambda raw, path: __import__(
                "src.autoslice.qixi_terminal_evidence_refresh",
                fromlist=["_json_document"],
            )._json_document(
                raw,
                "CHAT",
                review=True,
                directory=path.parent,
            ),
        ),
    ],
)
def test_each_first_h_reader_expands_compact_wire_roundtrip(
    name: str, reader, tmp_path: Path
) -> None:
    document, path, raw = _compact_chat(tmp_path)
    loaded = reader(raw, path)
    if name == "package-relocation":
        assert loaded == "a" * 64
    else:
        assert loaded == document, name
    assert path.read_bytes() == raw
    assert hashlib.sha256(path.read_bytes()).hexdigest() == hashlib.sha256(raw).hexdigest()


def test_first_h_reader_defaults_keep_ordinary_json_semantics(tmp_path: Path) -> None:
    ordinary = {"kind": "ordinary", "value": [1, 2, 3]}
    path = tmp_path / "ordinary.json"
    path.write_text('{"kind":"ordinary","value":[1,2,3]}\n', encoding="utf-8")

    from src.autoslice.b2_current_delivery_refresh import _json
    from src.autoslice.qixi_terminal_evidence_refresh import _json_document

    assert _json(path, label="ordinary") == ordinary
    assert _json_document(path.read_bytes(), "ORDINARY") == ordinary
