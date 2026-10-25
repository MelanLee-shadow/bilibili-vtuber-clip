from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from src.autoslice import producer_text_pipeline
from src.autoslice.chat_authority import recording_start_epoch_ms
from src.autoslice.clip_context import build_clip_context
from src.autoslice.producer_chat_input import (
    StructuredChatEvidenceError,
    _piece_chat_evidence,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
MEMORY_LEDGER = REPO_ROOT / "assets/lidousha/speech_memory_ledger.v1.json"


@pytest.mark.parametrize('kind', ['danmaku', 'superchat'])
def test_xml_chat_writer_and_continuation_share_content_identity(tmp_path, monkeypatch, kind):
    from src.autoslice.chat_authority import apply_authoritative_chat_evidence
    from src.autoslice.jingting_chunker import parse_srt_cues
    text = '小豆老公；； 直女不是你老公'
    canonical = '小豆老公；； 侄女不是你老公'
    xml = tmp_path / '22966160_20261024-19-31-06.xml'
    event = (f'<d p="1.000,1,25,16777215,0,0,0,0">{text}</d>' if kind == 'danmaku'
             else f'<sc ts="1.000" user="读者">{text}</sc>')
    xml.write_bytes(_xml_payload(event))
    piece = {**_piece(xml), 'start_ms': 0, 'end_ms': 30000}
    spec = {'pieces': [piece]}
    draft = '1\n00:00:05,000 --> 00:00:09,000\n小豆老公侄女不是你老公\n\n'
    support = draft.replace('小豆老公侄女不是你老公', canonical)
    _, chat = producer_text_pipeline._collect_timeline_chat(spec, [30000])
    output, audit = apply_authoritative_chat_evidence(draft, chat, support_srt_texts=[support])
    assert audit['applied'], audit
    row = audit['applied'][0]
    assert row['exact_text'] == chat[0].text == canonical
    assert row['evidence_id'] == chat[0].evidence_id
    kwargs = {'srt_text': output, 'clip_context': {'whole_clip_draft_srt': draft},
              'authoritative_chat': chat, 'independent_support_srts': [support]}
    producer_text_pipeline._validate_source_stage_chat_reads(audit['applied'], **kwargs)
    assert parse_srt_cues(output)[0].text == canonical
    # Restore the actual old responsible collector mechanism. The writer's
    # existing spelling projection then disagrees at the real continuation gate.
    with monkeypatch.context() as old:
        old.setattr(producer_text_pipeline, 'prepare_chat_evidence', list)
        _, raw_chat = producer_text_pipeline._collect_timeline_chat(spec, [30000])
    with pytest.raises(producer_text_pipeline.CurrentTextContinuationError,
                       match='CURRENT_SOURCE_STAGE_APPLIED_UNSUPPORTED'):
        producer_text_pipeline._validate_source_stage_chat_reads(
            audit['applied'], **{**kwargs, 'authoritative_chat': raw_chat})
    # Source-byte drift must still fail even when normalized words and IDs stay
    # identical. Restoring an equivalent spelling is not authority to rebind SHA.
    xml.write_bytes(_xml_payload(event.replace('直女', '侄女')))
    _, drifted = producer_text_pipeline._collect_timeline_chat(spec, [30000])
    if kind == 'danmaku':
        assert drifted[0].evidence_id == chat[0].evidence_id
    # XML SC event IDs also bind source bytes, so that lane rejects at identity.
    reason = ('CURRENT_SOURCE_STAGE_APPLIED_CHAT_OR_TEXT_DRIFT' if kind == 'danmaku'
              else 'CURRENT_SOURCE_STAGE_APPLIED_UNSUPPORTED')
    with pytest.raises(producer_text_pipeline.CurrentTextContinuationError, match=reason):
        producer_text_pipeline._validate_source_stage_chat_reads(
            audit['applied'], **{**kwargs, 'authoritative_chat': drifted})


def _xml_payload(*rows: str) -> bytes:
    return ("<?xml version='1.0' encoding='utf-8'?><i>" + "".join(rows) + "</i>").encode("utf-8")


def _piece(xml: Path, *, remote_media: Path | None = None) -> dict[str, object]:
    media = remote_media or xml.with_suffix(".mp4")
    return {
        "remote_media": str(media),
        "start_ms": 1_531_330,
        "end_ms": 1_831_330,
        "danmaku_xml_local": str(xml),
    }


def test_xml_only_superchat_reaches_timeline_and_clip_context(tmp_path: Path) -> None:
    xml = tmp_path / "22966160_20261006-19-01-03.xml"
    payload = _xml_payload(
        '<d p="1731.000,1,25,16777215,0,0,0,0">普通弹幕</d>',
        '<sc ts="1732.200" user="大黑鼠头胎轴二胎满" uid="485070" raw="ignored">'
        "小李你这种情况简直就是张无忌，因为对谁都不想伤害，最后伤害所有人"
        "</sc>",
    )
    xml.write_bytes(payload)
    piece = _piece(xml)
    xml_sha256 = hashlib.sha256(payload).hexdigest()

    evidence = _piece_chat_evidence(piece)
    sc = next(item for item in evidence if item.kind == "superchat")
    assert sc.offset_ms == 1_732_200
    assert sc.sender == "大黑鼠头胎轴二胎满"
    assert sc.source == str(xml)
    assert sc.source_sha256 == xml_sha256
    assert sc.source_event_id.startswith("xml-sc-")
    assert sc.source_event_id == next(
        item.source_event_id for item in _piece_chat_evidence(piece) if item.kind == "superchat"
    )

    merged, authoritative = producer_text_pipeline._collect_timeline_chat(
        {"pieces": [piece]}, [300_000]
    )
    timeline_sc = next(item for item in authoritative if item.kind == "superchat")
    assert timeline_sc.offset_ms == 200_870
    assert timeline_sc.sender == sc.sender
    assert timeline_sc.source == str(xml)
    assert timeline_sc.source_sha256 == xml_sha256
    assert timeline_sc.source_event_id == sc.source_event_id
    assert any(item.text.startswith("【SC·大黑鼠头胎轴二胎满】") for item in merged)

    context = build_clip_context(
        candidate_id="xml-only-sc-test",
        spec={
            "date": "2026-10-06",
            "selection_hook": "测试",
            "pieces": [piece],
        },
        draft_srt="1\n00:00:00,000 --> 00:00:01,000\n测试\n\n",
        authoritative_chat=authoritative,
        topic_resolution={"status": "UNKNOWN"},
        session_topic_authorities=(),
        speech_memory_ledger_path=MEMORY_LEDGER,
    )
    context_sc = next(row for row in context["structured_chat"] if row["kind"] == "superchat")
    assert context_sc["offset_ms"] == 200_870
    assert context_sc["sender"] == "大黑鼠头胎轴二胎满"
    assert context_sc["source"] == str(xml)
    assert context_sc["source_sha256"] == xml_sha256
    assert context_sc["source_event_id"] == sc.source_event_id


def test_jsonl_superchat_has_priority_over_xml_fallback_and_xml_danmaku_remains(
    tmp_path: Path,
) -> None:
    media = tmp_path / "22966160_20261006-19-01-03.mp4"
    xml = media.with_suffix(".xml")
    payload = _xml_payload(
        '<d p="1.000,1,25,16777215,0,0,0,0">保留弹幕</d>',
        '<sc ts="1.100" user="xml-sender">XML SC 不重复</sc>',
    )
    xml.write_bytes(payload)
    start_ms = recording_start_epoch_ms(media)
    assert start_ms is not None
    jsonl = media.with_suffix(".jsonl")
    jsonl.write_text(
        json.dumps(
            {
                "cmd": "SUPER_CHAT_MESSAGE",
                "send_time": start_ms + 1_100,
                "data": {
                    "id": 42,
                    "message": "JSONL SC 优先",
                    "user_info": {"uname": "jsonl-sender"},
                },
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    evidence = _piece_chat_evidence(_piece(xml, remote_media=media))
    assert [(item.kind, item.text, item.sender) for item in evidence] == [
        ("danmaku", "保留弹幕", ""),
        ("superchat", "JSONL SC 优先", "jsonl-sender"),
    ]
    assert evidence[-1].source == str(jsonl)
    assert evidence[-1].source_event_id == "42"


def test_xml_superchat_skips_bad_clock_and_empty_body_but_keeps_danmaku(
    tmp_path: Path,
) -> None:
    xml = tmp_path / "recording.xml"
    xml.write_bytes(
        _xml_payload(
            '<d p="2.000,1,25,16777215,0,0,0,0">普通弹幕</d>',
            '<sc ts="bad" user="bad-ts">不应出现</sc>',
            '<sc ts="NaN" user="nan-ts">不应出现</sc>',
            '<sc ts="-1" user="negative">不应出现</sc>',
            '<sc ts="1.000" user="empty"></sc>',
            '<sc ts="1.500" user="valid">有效正文</sc>',
        )
    )

    evidence = _piece_chat_evidence(_piece(xml))
    assert [(item.kind, item.offset_ms, item.text) for item in evidence] == [
        ("danmaku", 2_000, "普通弹幕"),
        ("superchat", 1_500, "有效正文"),
    ]


def test_malformed_xml_retains_typed_failure(tmp_path: Path) -> None:
    xml = tmp_path / "malformed.xml"
    xml.write_text("<i><sc ts='1.0'>未闭合", encoding="utf-8")

    with pytest.raises(StructuredChatEvidenceError, match="DANMAKU_XML_PARSE_FAILED"):
        _piece_chat_evidence(_piece(xml))
