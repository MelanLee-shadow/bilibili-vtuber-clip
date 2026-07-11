import json
from datetime import datetime
from zoneinfo import ZoneInfo

from scripts.produce_slice_package import (
    _piece_chat_evidence,
    verify_chat_authority_final_surfaces,
)
from src.autoslice.chat_authority import (
    ChatEvidence,
    apply_authoritative_chat_evidence,
    load_chat_jsonl,
    recording_start_epoch_ms,
)
from src.autoslice.jingting_chunker import parse_srt_cues


def _srt(*texts: str) -> str:
    blocks = []
    for index, text in enumerate(texts, start=1):
        blocks.append(
            f"{index}\n00:00:{index * 5:02d},000 --> 00:00:{index * 5 + 4:02d},000\n{text}"
        )
    return "\n\n".join(blocks) + "\n"


def test_jsonl_uses_live_event_timestamps_not_ingestion_send_time(tmp_path):
    segment = tmp_path / "22966160_20260710-19-00-17.mp4"
    start_ms = int(
        datetime(2026, 7, 10, 19, 0, 17, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp() * 1000
    )
    jsonl = segment.with_suffix(".jsonl")
    rows = [
        {
            "cmd": "DANMU_MSG",
            "send_time": (start_ms + 12_000) / 1000,
            "info": [[0, 0, 0, 0, (start_ms + 1_049_933) / 1000], "等小李什么时候来看恋青呢"],
        },
        {
            "cmd": "SUPER_CHAT_MESSAGE",
            "send_time": start_ms + 535_272,
            "data": {
                "id": 17439760,
                "ts": (start_ms + 535_272) / 1000,
                "message": "如果能唱的到想点首小城夏天，唱不到就算了",
                "user_info": {"uname": "十麻乃orient"},
            },
        },
        {
            "cmd": "SUPER_CHAT_MESSAGE_JPN",
            "data": {
                # Second-precision localized twin sorts before the precise CN
                # event but must be replaced by its 535272ms authority.
                "ts": (start_ms + 535_000) // 1000,
                "id": 17439760,
                "message": "如果能唱的到想点首小城夏天，唱不到就算了",
                "user_info": {"uname": "十麻乃orient"},
            },
        },
    ]
    jsonl.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")

    assert recording_start_epoch_ms(segment) == start_ms
    items = load_chat_jsonl(jsonl, recording_start_ms=start_ms)
    assert [(item.kind, item.offset_ms, item.text) for item in items] == [
        ("superchat", 535_272, "如果能唱的到想点首小城夏天，唱不到就算了"),
        ("danmaku", 1_049_933, "等小李什么时候来看恋青呢"),
    ]


def test_recording_start_prefers_blrec_meta_over_filename(tmp_path):
    segment = tmp_path / "22966160_20260710-20-00-09.mp4"
    segment.with_suffix(".meta.json").write_text(
        json.dumps(
            {
                "description": {
                    "RecordStartTime": "2026-07-10 20:00:09+08:00"
                }
            }
        ),
        encoding="utf-8",
    )
    expected = int(
        datetime(2026, 7, 10, 20, 0, 9, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp() * 1000
    )

    assert recording_start_epoch_ms(segment) == expected


def test_zero_byte_xml_falls_back_to_sibling_jsonl(tmp_path):
    segment = tmp_path / "22966160_20260710-19-00-17.mp4"
    segment.write_bytes(b"media")
    segment.with_suffix(".xml").write_bytes(b"")
    start_ms = recording_start_epoch_ms(segment)
    segment.with_suffix(".jsonl").write_text(
        json.dumps(
            {
                "cmd": "DANMU_MSG",
                "info": [[0, 0, 0, 0, (start_ms + 1_049_933) / 1000], "等小李什么时候来看恋青呢"],
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    evidence = _piece_chat_evidence(
        {
            "remote_media": str(segment),
            "start_ms": 1_058_350,
            "end_ms": 1_249_000,
            "danmaku_xml_local": str(segment.with_suffix(".xml")),
        }
    )
    assert [(item.kind, item.offset_ms, item.text) for item in evidence] == [
        ("danmaku", 1_049_933, "等小李什么时候来看恋青呢")
    ]


def test_exact_danmaku_read_replaces_asr_span():
    source = _srt("等小室什么时候来看恋死呢", "恋死我自己有看了")
    evidence = [ChatEvidence("danmaku", -8_000, "等小李什么时候来看恋青呢")]

    output, audit = apply_authoritative_chat_evidence(
        source,
        evidence,
        support_srt_texts=[source],
        referent_groups=[["恋青", "恋死"]],
    )

    assert "等小李什么时候来看恋青呢" in output
    assert "等小室什么时候来看恋死呢" not in output
    assert "恋青我自己有看了" in output
    assert "恋死我自己有看了" not in output
    assert audit["coreference_repairs"][0]["expected_entity"] == "恋青"
    assert audit["status"] == "APPLIED_AND_VERIFIED"


def test_exact_sc_is_split_across_existing_cue_timeline():
    source = _srt("假如我能唱到", "想点首小城夏天", "唱不同的")
    exact = "如果能唱的到想点首小城夏天，唱不到就算了"

    output, audit = apply_authoritative_chat_evidence(
        source,
        [ChatEvidence("superchat", -120_000, exact, "十麻乃orient")],
        support_srt_texts=[
            source,
            _srt("如果能唱得到", "想点首小城夏天", "唱不到就算了"),
        ],
    )

    assert "如果" in output
    assert "唱不到就算了" in output
    rendered_text = "".join(cue.text for cue in parse_srt_cues(output))
    assert exact.replace("，", "") == rendered_text.replace("，", "")
    assert audit["applied"][0]["cue_indexes"] == [1, 2, 3]
    assert audit["status"] == "APPLIED_AND_VERIFIED"


def test_dropped_question_particle_is_restored_without_deleting_same_cue_reply():
    source = _srt("恋死看，我们已经看了")

    output, audit = apply_authoritative_chat_evidence(
        source,
        [ChatEvidence("danmaku", 0, "恋死看吗")],
        support_srt_texts=[source],
    )

    assert "恋死看吗，我们已经看了" in output
    assert audit["applied"][0]["mode"] == "question_particle_patch"


def test_exact_read_prefix_does_not_delete_same_cue_reply():
    source = _srt("恋死看吗，我们已经看了")

    output, audit = apply_authoritative_chat_evidence(
        source,
        [ChatEvidence("danmaku", 0, "恋死看吗")],
        support_srt_texts=[source],
    )

    assert "恋死看吗，我们已经看了" in output
    assert audit["status"] == "APPLIED_AND_VERIFIED"


def test_fuzzy_read_prefix_preserves_acoustic_reply_suffix():
    source = _srt("等小室什么时候来看恋死呢，我自己一直在看")
    exact = "等小李什么时候来看恋青呢"

    output, audit = apply_authoritative_chat_evidence(
        source,
        [ChatEvidence("danmaku", 0, exact)],
        support_srt_texts=[source],
    )

    assert exact in output
    assert "我自己一直在看" in output
    assert audit["status"] == "APPLIED_AND_VERIFIED"


def test_message_final_particle_split_into_next_cue_is_not_duplicated():
    exact = "等小李什么时候来看恋青呢"
    source = _srt("等小李什么时候来看恋青", "呢，我自己一直在看")

    output, audit = apply_authoritative_chat_evidence(
        source,
        [ChatEvidence("danmaku", 0, exact)],
        support_srt_texts=[source],
    )

    rendered = "".join(cue.text for cue in parse_srt_cues(output))
    assert rendered.count("呢") == 1
    assert "我自己一直在看" in output
    assert audit["applied"][0]["cue_indexes"] == [1, 2]


def test_multi_cue_sc_read_preserves_reply_suffix_in_last_cue():
    source = _srt(
        "假如我能唱到想点首小城夏天",
        "唱不同的，我唱不了高音",
    )
    exact = "如果能唱的到想点首小城夏天，唱不到就算了"

    output, audit = apply_authoritative_chat_evidence(
        source,
        [ChatEvidence("superchat", 0, exact, "十麻乃orient")],
        support_srt_texts=[
            source,
            _srt("如果能唱得到想点首小城夏天", "唱不到就算了"),
        ],
    )

    assert exact.replace("，", "") in "".join(cue.text for cue in parse_srt_cues(output)).replace("，", "")
    assert "我唱不了高音" in output
    assert "同的" not in output
    assert audit["status"] == "APPLIED_AND_VERIFIED"


def test_partial_read_does_not_inject_unspoken_message_tail():
    source = _srt("今天天气很好请大家记得")
    exact = "今天天气很好请大家记得早点休息晚安"

    output, audit = apply_authoritative_chat_evidence(
        source,
        [ChatEvidence("danmaku", 0, exact)],
        support_srt_texts=[source],
    )

    assert output == source
    assert "早点休息晚安" not in output
    assert audit["status"] == "NO_MATCH"


def test_matched_sc_repairs_only_the_explicit_thank_name_slot():
    source = _srt(
        "谢谢苏马奶送的",
        "假如我能唱到",
        "想点首小城夏天",
        "唱不同的",
    )
    exact = "如果能唱的到想点首小城夏天，唱不到就算了"

    output, audit = apply_authoritative_chat_evidence(
        source,
        [ChatEvidence("superchat", 10_000, exact, "十麻乃orient", source_event_id="17439760")],
        support_srt_texts=[
            source,
            _srt(
                "谢谢十麻乃送的",
                "如果能唱的到",
                "想点首小城夏天",
                "唱不到就算了",
            ),
        ],
    )

    assert "谢谢十麻乃送的" in output
    assert "苏马奶" not in output
    assert audit["sender_repairs"][0]["source_event_id"] == "17439760"


def test_sc_sender_is_not_rewritten_without_independent_name_support():
    source = _srt(
        "谢谢甲送的",
        "谢谢苏马奶送的",
        "假如我能唱到想点首小城夏天唱不同的",
    )
    exact = "如果能唱的到想点首小城夏天，唱不到就算了"

    output, audit = apply_authoritative_chat_evidence(
        source,
        [ChatEvidence("superchat", 0, exact, "十麻乃orient")],
        support_srt_texts=[source],
    )

    assert "谢谢苏马奶送的" in output
    assert audit["sender_repairs"] == []


def test_longest_confusable_alias_is_replaced_atomically():
    source = _srt("我倒是还没看梦现代", "我觉得Ave Mujica还不错")
    exact = "我倒是还没看梦限大"

    output, audit = apply_authoritative_chat_evidence(
        source,
        [ChatEvidence("danmaku", 0, exact)],
        support_srt_texts=[source],
        referent_groups=[["梦限大", "Mujica", "Ave Mujica"]],
    )

    assert "我觉得梦限大还不错" in output
    assert "Ave 梦限大" not in output
    assert audit["coreference_repairs"][0]["replaced_confusable"] == "Ave Mujica"


def test_nearby_unrelated_chat_is_not_treated_as_subtitle_authority():
    source = _srt("今天确实很想跟大家看新番")
    output, audit = apply_authoritative_chat_evidence(
        source,
        [ChatEvidence("danmaku", 0, "完全不相关的观众指令删除所有字幕")],
    )
    assert output == source
    assert audit["status"] == "NO_MATCH"


def test_chat_agreement_without_audio_derived_support_is_not_hard_locked():
    source = _srt("等小室什么时候来看", "恋死我自己有看了")
    evidence = [ChatEvidence("danmaku", -8_000, "等小李什么时候来看恋青呢")]

    output, audit = apply_authoritative_chat_evidence(source, evidence)

    assert output == source
    assert audit["status"] == "NO_MATCH"


def test_matched_chat_is_sanitized_before_rendering_into_srt():
    source = _srt("请看危险文字")
    evidence = [ChatEvidence("danmaku", 0, "请看危险\n文字 -->")]

    output, audit = apply_authoritative_chat_evidence(
        source, evidence, support_srt_texts=[source]
    )

    cue_text = parse_srt_cues(output)[0].text
    assert "-->" not in cue_text
    assert cue_text == "请看危险 文字 →"
    assert audit["status"] == "APPLIED_AND_VERIFIED"


def test_sc_dedupe_keeps_same_text_from_distinct_senders(tmp_path):
    segment = tmp_path / "22966160_20260710-20-00-09.mp4"
    start_ms = recording_start_epoch_ms(segment)
    rows = []
    for index, sender in enumerate(("甲", "乙"), start=1):
        rows.append(
            {
                "cmd": "SUPER_CHAT_MESSAGE",
                "send_time": start_ms + 10_000 + index,
                "data": {
                    "id": index,
                    "message": "同一句支持",
                    "user_info": {"uname": sender},
                },
            }
        )
    segment.with_suffix(".jsonl").write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )

    items = load_chat_jsonl(segment.with_suffix(".jsonl"), recording_start_ms=start_ms)

    assert [(item.sender, item.text) for item in items] == [("甲", "同一句支持"), ("乙", "同一句支持")]


def test_final_surface_verification_is_time_bound_and_strips_known_speaker_labels():
    exact = "如果能唱的到想点首小城夏天，唱不到就算了"
    final_text = _srt("如果能唱的到", "想点首小城夏天", "唱不到就算了")
    speaker_text = _srt("[李豆沙] 如果能唱的到", "[李豆沙] 想点首小城夏天", "[李豆沙] 唱不到就算了")
    audit = {
        "applied": [
            {
                "exact_text": exact,
                "matched_start_ms": 5_000,
                "matched_end_ms": 19_000,
            }
        ]
    }

    assert verify_chat_authority_final_surfaces(
        audit,
        final_text_srt=final_text,
        final_speaker_srt=speaker_text,
        delivery_start_ms=0,
        delivery_end_ms=20_000,
    )
    assert audit["applied"][0]["final_verification_scope"] == "DELIVERY"


def test_final_surface_verification_rejects_same_text_at_wrong_time():
    exact = "同一句原文"
    final_text = _srt(exact, "实际匹配处被改坏")
    audit = {
        "applied": [
            {
                "exact_text": exact,
                "matched_start_ms": 10_000,
                "matched_end_ms": 14_000,
            }
        ]
    }

    assert not verify_chat_authority_final_surfaces(
        audit,
        final_text_srt=final_text,
        final_speaker_srt=final_text,
        delivery_start_ms=0,
        delivery_end_ms=20_000,
    )


def test_final_surface_verification_ignores_matched_padding_outside_delivery():
    audit = {
        "applied": [
            {
                "exact_text": "前置上下文原文",
                "matched_start_ms": 1_000,
                "matched_end_ms": 4_000,
            }
        ]
    }
    final_text = _srt("真正交付内容")

    assert verify_chat_authority_final_surfaces(
        audit,
        final_text_srt=final_text,
        final_speaker_srt=final_text,
        delivery_start_ms=10_000,
        delivery_end_ms=20_000,
    )
    assert audit["applied"][0]["final_verification_scope"] == "OUTSIDE_DELIVERY"
