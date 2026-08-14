from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from src.autoslice import producer_chat_input, producer_text_pipeline
from src.autoslice.isolated_source_read import (
    IsolatedSourceReadError,
    read_source_bytes_isolated,
)
from src.autoslice.producer_chat_input import StructuredChatEvidenceError


def _bound_piece(tmp_path: Path) -> tuple[dict[str, object], Path, Path, bytes, bytes]:
    start_ms = 1_750_000_000_000
    jsonl = tmp_path / "cloudfs" / "recording.jsonl"
    xml = tmp_path / "cloudfs" / "recording.xml"
    jsonl.parent.mkdir()
    jsonl_payload = (
        json.dumps(
            {
                "cmd": "SUPER_CHAT_MESSAGE",
                "send_time": start_ms + 5_000,
                "data": {
                    "id": 42,
                    "message": "隔离读取",
                    "user_info": {"uname": "证人"},
                },
            },
            ensure_ascii=False,
        )
        + "\n"
    ).encode()
    xml_payload = (
        b"<?xml version='1.0' encoding='utf-8'?><i>"
        b'<d p="4.000,1,25,16777215,0,0,0,0">xml authority</d></i>'
    )
    jsonl.write_bytes(jsonl_payload)
    xml.write_bytes(xml_payload)
    return (
        {
            "remote_media": str(tmp_path / "cloudfs" / "recording.mp4"),
            "start_ms": 0,
            "end_ms": 10_000,
            "danmaku_xml_local": str(xml),
            "chat_jsonl_local": str(jsonl),
            "chat_jsonl_sha256": "sha256:" + hashlib.sha256(jsonl_payload).hexdigest(),
            "chat_origin_epoch_ms": start_ms,
            "chat_timeline_offset_ms": 37,
            "structured_chat_required": True,
        },
        jsonl,
        xml,
        jsonl_payload,
        xml_payload,
    )


def test_bound_jsonl_and_xml_are_each_isolated_once_without_cloudfs_reopen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    piece, jsonl, xml, jsonl_payload, xml_payload = _bound_piece(tmp_path)
    source_paths = {jsonl, xml}
    calls: list[Path] = []

    def counted_isolated_read(path: Path | str):
        source = Path(path)
        calls.append(source)
        return read_source_bytes_isolated(source, spool_root=tmp_path / "spool")

    monkeypatch.setattr(
        producer_chat_input,
        "read_source_bytes_isolated",
        counted_isolated_read,
        raising=False,
    )
    original_read_bytes = Path.read_bytes
    original_read_text = Path.read_text

    def refuse_source_read_bytes(path: Path) -> bytes:
        if path in source_paths:
            raise AssertionError(f"CloudFS source reopened with read_bytes: {path}")
        return original_read_bytes(path)

    def refuse_source_read_text(path: Path, *args, **kwargs) -> str:
        if path in source_paths:
            raise AssertionError(f"CloudFS source reopened with read_text: {path}")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_bytes", refuse_source_read_bytes)
    monkeypatch.setattr(Path, "read_text", refuse_source_read_text)

    evidence = producer_chat_input._piece_chat_evidence(piece)

    assert calls == [jsonl, xml]
    by_kind = {item.kind: item for item in evidence}
    assert by_kind["superchat"].source == str(jsonl)
    assert by_kind["superchat"].source_sha256 == hashlib.sha256(jsonl_payload).hexdigest()
    assert by_kind["superchat"].offset_ms == 5_037
    assert by_kind["danmaku"].source == str(xml)
    assert by_kind["danmaku"].source_sha256 == hashlib.sha256(xml_payload).hexdigest()


@pytest.mark.parametrize(
    ("source_kind", "reason_code"),
    [
        ("jsonl", "STRUCTURED_CHAT_BINDING_SOURCE_READ_TIMEOUT"),
        ("xml", "DANMAKU_XML_SOURCE_READ_TIMEOUT"),
    ],
)
def test_source_timeout_is_typed_before_transcriber_or_ffmpeg(
    source_kind: str,
    reason_code: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    piece, jsonl, xml, _jsonl_payload, _xml_payload = _bound_piece(tmp_path)
    timed_out_path = jsonl if source_kind == "jsonl" else xml
    if source_kind == "xml":
        piece.pop("chat_jsonl_local")
        piece.pop("chat_jsonl_sha256")
        piece["structured_chat_required"] = False

    def timed_out(path: Path | str):
        if Path(path) == timed_out_path:
            raise IsolatedSourceReadError(
                "SOURCE_READ_TIMEOUT",
                {
                    "source_path": str(timed_out_path),
                    "reason_code": "SOURCE_READ_TIMEOUT",
                },
            )
        return read_source_bytes_isolated(path, spool_root=tmp_path / "spool")

    downstream_calls: list[str] = []

    def downstream_bomb(*_args, **_kwargs):
        downstream_calls.append("called")
        raise AssertionError("provider/ffmpeg stage must not start")

    monkeypatch.setattr(
        producer_chat_input,
        "read_source_bytes_isolated",
        timed_out,
        raising=False,
    )
    monkeypatch.setattr(producer_text_pipeline, "build_env_screen_read_probe", downstream_bomb)
    monkeypatch.setattr(producer_text_pipeline, "_transcribe_draft", downstream_bomb)

    with pytest.raises(StructuredChatEvidenceError) as failure:
        producer_text_pipeline.run_text_pipeline(
            spec={"pieces": [piece]},
            durations=[10_000],
            padded=tmp_path / "padded.mp4",
            padded_dur=10_000,
            host="free",
            text_override_path=None,
            cid="auto_test",
            out_root=tmp_path / "out",
            substrate="agy",
            correct="agy",
            screen_text=False,
            adapters=object(),  # collection must fail before an adapter is inspected
        )

    assert failure.value.reason_code == reason_code
    assert failure.value.evidence["isolated_read"]["reason_code"] == "SOURCE_READ_TIMEOUT"
    assert downstream_calls == []
