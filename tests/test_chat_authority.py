import copy
import hashlib
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from scripts.produce_slice_package import (
    _load_independent_chat_support_srts,
    _piece_chat_evidence,
    verify_chat_authority_final_surfaces,
)
from src.autoslice.chat_authority import (
    ChatEvidence,
    ReferentEntity,
    ReferentGroup,
    apply_audio_entity_verification,
    apply_authoritative_chat_evidence,
    build_human_text_entity_verifier,
    load_chat_jsonl,
    load_referent_groups,
    normalize_code_switch_surfaces,
    recording_start_epoch_ms,
    reconcile_pending_text_overrides,
)
from src.autoslice.jingting_chunker import parse_srt_cues


def _srt(*texts: str) -> str:
    blocks = []
    for index, text in enumerate(texts, start=1):
        blocks.append(
            f"{index}\n00:00:{index * 5:02d},000 --> 00:00:{index * 5 + 4:02d},000\n{text}"
        )
    return "\n\n".join(blocks) + "\n"


def _audio_entity_verifier(canonical: str):
    def verify(request):
        return {
            "schema_version": "chat-entity-verdict.v1",
            "request_sha256": request["request_sha256"],
            "status": "RESOLVED",
            "canonical_entity": canonical,
            "authority_kind": "audio_forced_choice",
            "confidence": 0.97,
            "heard_syllables": canonical,
            "source_media_sha256": "a" * 64,
            "audio_clip_sha256": "b" * 64,
            "prompt_sha256": "c" * 64,
            "response_sha256": "d" * 64,
        }

    return verify


DREAM_MUJICA_GROUP = ReferentGroup(
    (
        ReferentEntity("梦限大", ("梦限大", "梦现代"), ("meng xian da",)),
        ReferentEntity("Ave Mujica", ("Ave Mujica", "Mujica", "母鸡卡", "木子卡"), ("mujica",)),
    )
)
CHARACTER_GROUP = ReferentGroup(
    (
        ReferentEntity("立希", ("立希", "椎名立希"), ("li xi", "taki")),
        ReferentEntity("祥子", ("祥子", "丰川祥子", "saki"), ("xiang zi", "saki")),
    ),
    audio_verify_all_surfaces=True,
)


def test_v2_entity_config_keeps_aliases_under_one_canonical():
    groups = load_referent_groups(
        Path(__file__).resolve().parents[1] / "assets/lidousha/entity_confusables.json"
    )
    dream_group = next(
        group for group in groups if {entity.canonical for entity in group.entities} == {"梦限大", "Ave Mujica"}
    )
    mujica = next(entity for entity in dream_group.entities if entity.canonical == "Ave Mujica")

    assert {"Mujica", "母鸡卡", "木子卡"} <= set(mujica.surfaces)
    assert all(entity.canonical != "母鸡卡" for entity in dream_group.entities)


def test_japanese_code_switch_surface_is_canonicalized_without_timing_change():
    source = _srt("这种哇哭哇哭的感觉")

    output, audit = normalize_code_switch_surfaces(source)

    assert "这种wakuwaku的感觉" in output
    assert "00:00:05,000 --> 00:00:09,000" in output
    assert audit["repairs"][0]["authority"] == "lidousha-code-switch-canon.v1"


def test_ordinary_speech_audio_verifier_repairs_saki_to_lixi_entity_only():
    source = _srt("然后那个saki的高压的态度")

    output, audit = apply_audio_entity_verification(
        source,
        referent_groups=[CHARACTER_GROUP],
        entity_verifier=_audio_entity_verifier("立希"),
    )

    assert "然后那个立希的高压的态度" in output
    assert "saki" not in output
    assert audit["repairs"][0]["mode"] == "transcript_entity_only"
    assert audit["repairs"][0]["before"] == ["然后那个saki的高压的态度"]


def test_ordinary_speech_entity_verification_fails_closed_when_uncertain():
    source = _srt("然后那个祥子的高压的态度")

    output, audit = apply_audio_entity_verification(
        source,
        referent_groups=[CHARACTER_GROUP],
        entity_verifier=None,
    )

    assert output == source
    assert audit["status"] == "ENTITY_VERDICT_REQUIRED"
    assert audit["entity_verdict_required"][0]["cue_index"] == 1


def test_exact_chat_owned_cue_is_excluded_from_second_entity_verdict():
    source = _srt("等小李什么时候来看恋青呢")
    group = ReferentGroup(
        (ReferentEntity("恋青", ("恋青",)), ReferentEntity("恋死", ("恋死",))),
        audio_verify_all_surfaces=True,
    )

    output, audit = apply_audio_entity_verification(
        source,
        referent_groups=[group],
        entity_verifier=None,
        excluded_cue_indexes=[1],
    )

    assert output == source
    assert audit["status"] == "NO_ENTITY"


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
        entity_verifier=_audio_entity_verifier("恋青"),
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


def test_matched_sc_body_makes_platform_sender_authoritative_for_thank_name_slot():
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

    assert "谢谢十麻乃送的" in output
    assert "苏马奶" not in output
    assert audit["sender_repairs"][0]["alignment_basis"] == (
        "matched-superchat-body-plus-platform-sender.v1"
    )


def test_duplicate_real_sc_body_with_different_senders_fails_closed():
    exact = "如果能唱的到想点首小城夏天，唱不到就算了"
    source = _srt("谢谢错名送的", "如果能唱的到想点首小城夏天唱不到就算了")

    output, audit = apply_authoritative_chat_evidence(
        source,
        [
            ChatEvidence("superchat", 0, exact, "甲", source_event_id="evt-a"),
            ChatEvidence("superchat", 0, exact, "乙", source_event_id="evt-b"),
        ],
        support_srt_texts=[source],
    )

    assert "谢谢错名送的" in output
    assert audit["sender_repairs"] == []
    assert audit["status"] == "SC_SENDER_VERDICT_REQUIRED"
    assert audit["sender_verdict_required"][0]["reason_code"] == (
        "DUPLICATE_SC_BODY_SENDER_AMBIGUOUS"
    )


def test_duplicate_sc_body_without_thank_name_does_not_create_sender_block():
    exact = "如果能唱的到想点首小城夏天，唱不到就算了"
    source = _srt("如果能唱的到想点首小城夏天唱不到就算了")

    _output, audit = apply_authoritative_chat_evidence(
        source,
        [
            ChatEvidence("superchat", 0, exact, "甲", source_event_id="evt-a"),
            ChatEvidence("superchat", 0, exact, "乙", source_event_id="evt-b"),
        ],
        support_srt_texts=[source],
    )

    assert audit["sender_verdict_required"] == []
    assert audit["status"] == "APPLIED_AND_VERIFIED"


def test_chat_read_support_excludes_chat_conditioned_agy_refinement(tmp_path):
    media = tmp_path / "padded.mp4"
    media.write_bytes(b"media")
    media.with_suffix(".asr_draft.srt").write_text(_srt("raw audio only"), encoding="utf-8")
    media.with_suffix(".agy_refined.srt").write_text(
        _srt("copied exact structured chat"), encoding="utf-8"
    )

    support = _load_independent_chat_support_srts(media)

    assert len(support) == 1
    assert "raw audio only" in support[0]
    assert "copied exact structured chat" not in support[0]


def test_longest_confusable_alias_is_replaced_atomically():
    source = _srt("我倒是还没看梦现代", "我觉得Ave Mujica还不错")
    exact = "我倒是还没看梦限大"

    output, audit = apply_authoritative_chat_evidence(
        source,
        [ChatEvidence("danmaku", 0, exact)],
        support_srt_texts=[source],
        referent_groups=[DREAM_MUJICA_GROUP],
        entity_verifier=_audio_entity_verifier("梦限大"),
    )

    assert "我觉得梦限大还不错" in output
    assert "Ave 梦限大" not in output
    assert audit["coreference_repairs"][0]["replaced_confusable"] == "Ave Mujica"


def test_confusable_entity_in_nearby_chat_cannot_overwrite_audio_refined_name():
    source = _srt(
        "还没看",
        "怎么有人说有梦限大的风险",
        "我还没有看完整的",
    )
    exact = "还没看，怎么有人说有母鸡卡的风险"

    output, audit = apply_authoritative_chat_evidence(
        source,
        [ChatEvidence("danmaku", 0, exact)],
        # Even a raw ASR agreeing with the nearby chat is only a text proxy;
        # it cannot erase a different proper name chosen by the refined audio.
        support_srt_texts=[_srt("还没看", "怎么有人说有母鸡卡的风险")],
        referent_groups=[DREAM_MUJICA_GROUP],
    )

    assert output == source
    assert audit["status"] == "ENTITY_VERDICT_REQUIRED"
    assert audit["applied"] == []
    assert audit["entity_verdict_required"][0]["matched_audio_text"] == (
        "还没看怎么有人说有梦限大的风险"
    )
    assert audit["entity_verdict_required"][0]["reason_code"] == "ENTITY_VERDICT_REQUIRED"


def test_audio_forced_choice_rejects_exact_chat_and_repairs_only_entity_slot():
    source = _srt("还没看", "怎么有人说有母鸡卡的风险", "我还没有看完整的")
    exact = "还没看，怎么有人说有母鸡卡的风险"

    output, audit = apply_authoritative_chat_evidence(
        source,
        [ChatEvidence("danmaku", 0, exact)],
        support_srt_texts=[source],
        referent_groups=[DREAM_MUJICA_GROUP],
        entity_verifier=_audio_entity_verifier("梦限大"),
    )

    assert "怎么有人说有梦限大的风险" in output
    assert "还没看，怎么有人说" not in output
    assert audit["applied"] == []
    assert audit["entity_repairs"][0]["mode"] == "entity_only"
    assert audit["entity_repairs"][0]["replaced_surface"] == "母鸡卡"
    assert audit["superseded_chat_proposals"][0]["reason_code"] == (
        "EXACT_CHAT_REJECTED_BY_AUDIO_ENTITY_VERDICT"
    )
    assert audit["status"] == "APPLIED_AND_VERIFIED"


def test_hash_bound_ivan_override_supersedes_chat_and_survives_final_verifier(tmp_path):
    source = _srt("还没看", "怎么有人说有母鸡卡的风险")
    final = source.replace("母鸡卡", "梦限大")
    evidence = ChatEvidence("danmaku", 0, "还没看，怎么有人说有母鸡卡的风险")
    document = {
        "schema_version": 1,
        "candidate_id": "auto_test",
        "source_srt_sha256": hashlib.sha256(source.encode()).hexdigest(),
        "text_final_srt_sha256": hashlib.sha256(final.encode()).hexdigest(),
        "chat_entity_verdicts": [
            {
                "evidence_id": evidence.evidence_id,
                "canonical_entity": "梦限大",
                "authority": "Ivan direct correction",
            }
        ],
        "overrides": [
            {
                "source_cue": 2,
                "expect": {
                    "start": "00:00:10,000",
                    "end": "00:00:14,000",
                    "text": "怎么有人说有母鸡卡的风险",
                },
                "text": "怎么有人说有梦限大的风险",
                "authority": "Ivan direct correction",
                "supersedes_chat_evidence_id": evidence.evidence_id,
            }
        ],
    }
    document_path = tmp_path / "auto_test.text.v1.json"
    document_path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    verifier = build_human_text_entity_verifier(document_path, candidate_id="auto_test")

    output, audit = apply_authoritative_chat_evidence(
        source,
        [evidence],
        support_srt_texts=[source],
        referent_groups=[DREAM_MUJICA_GROUP],
        entity_verifier=verifier,
    )

    assert output == source
    assert audit["status"] == "PENDING_TEXT_OVERRIDE"
    assert audit["applied"] == []
    manifest = {
        "status": "READY",
        "override_document_sha256": hashlib.sha256(document_path.read_bytes()).hexdigest(),
        "source_srt_sha256": hashlib.sha256(source.encode()).hexdigest(),
        "output_srt_sha256": hashlib.sha256(final.encode()).hexdigest(),
        "decisions": [
            {
                "source": {
                    "source_index": 2,
                    "start": "00:00:10,000",
                    "end": "00:00:14,000",
                    "text": "怎么有人说有母鸡卡的风险",
                },
                "output_text": "怎么有人说有梦限大的风险",
                "supersedes_chat_evidence_id": evidence.evidence_id,
            }
        ],
    }
    drifted = copy.deepcopy(audit)
    assert not reconcile_pending_text_overrides(
        drifted,
        {**manifest, "source_srt_sha256": "0" * 64},
        delivery_start_ms=0,
    )

    assert reconcile_pending_text_overrides(audit, manifest, delivery_start_ms=0)
    assert audit["entity_repairs"][0]["mode"] == "entity_only_human_text_override"
    assert verify_chat_authority_final_surfaces(
        audit,
        final_text_srt=final,
        final_speaker_srt=final,
        delivery_start_ms=0,
        delivery_end_ms=20_000,
    )


def test_hash_bound_ivan_entity_verdict_can_reuse_exact_chat_scaffold(tmp_path):
    source = _srt("还没看", "怎么有人说是Mujica的风险")
    final = _srt("还没看", "怎么有人说有梦限大的风险")
    evidence = ChatEvidence("danmaku", 0, "还没看，怎么有人说有母鸡卡的风险")
    document = {
        "schema_version": 1,
        "candidate_id": "auto_test",
        "source_srt_sha256": hashlib.sha256(source.encode()).hexdigest(),
        "text_final_srt_sha256": hashlib.sha256(final.encode()).hexdigest(),
        "chat_entity_verdicts": [
            {
                "evidence_id": evidence.evidence_id,
                "canonical_entity": "梦限大",
                "authority": "Ivan direct correction",
            }
        ],
        "overrides": [
            {
                "source_cue": 2,
                "expect": {
                    "start": "00:00:10,000",
                    "end": "00:00:14,000",
                    "text": "怎么有人说是Mujica的风险",
                },
                "text": "怎么有人说有梦限大的风险",
                "authority": "exact chat scaffold plus Ivan entity verdict",
                "supersedes_chat_evidence_id": evidence.evidence_id,
            }
        ],
    }
    document_path = tmp_path / "auto_test.text.v1.json"
    document_path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    document_hash = hashlib.sha256(document_path.read_bytes()).hexdigest()
    audit = {
        "status": "PENDING_TEXT_OVERRIDE",
        "pending_text_overrides": [
            {
                "evidence_id": evidence.evidence_id,
                "exact_text": evidence.text,
                "matched_start_ms": 5_000,
                "matched_end_ms": 14_000,
                "request": {
                    "candidate_entities": [
                        {
                            "canonical": entity.canonical,
                            "surfaces": list(entity.surfaces),
                            "readings": list(entity.readings),
                        }
                        for entity in DREAM_MUJICA_GROUP.entities
                    ]
                },
                "verdict": {
                    "request_sha256": "request-hash",
                    "override_document_sha256": document_hash,
                    "source_srt_sha256": hashlib.sha256(source.encode()).hexdigest(),
                    "text_final_srt_sha256": hashlib.sha256(final.encode()).hexdigest(),
                    "canonical_entity": "梦限大",
                },
            }
        ],
        "entity_verdicts": [
            {
                "evidence_id": evidence.evidence_id,
                "verdict": {
                    "request_sha256": "request-hash",
                    "canonical_entity": "梦限大",
                },
            }
        ],
        "entity_repairs": [],
    }
    manifest = {
        "status": "READY",
        "override_document": str(document_path),
        "override_document_sha256": document_hash,
        "source_srt_sha256": hashlib.sha256(source.encode()).hexdigest(),
        "output_srt_sha256": hashlib.sha256(final.encode()).hexdigest(),
        "decisions": [
            {
                "source": {
                    "source_index": 2,
                    "start": "00:00:10,000",
                    "end": "00:00:14,000",
                    "text": "怎么有人说是Mujica的风险",
                },
                "output_text": "怎么有人说有梦限大的风险",
                "supersedes_chat_evidence_id": evidence.evidence_id,
            }
        ],
    }
    arbitrary = copy.deepcopy(audit)
    arbitrary_manifest = copy.deepcopy(manifest)
    arbitrary_manifest["decisions"][0]["output_text"] = "今天完全不相关但提到梦限大"
    assert not reconcile_pending_text_overrides(
        arbitrary,
        arbitrary_manifest,
        delivery_start_ms=0,
    )
    audit["pending_text_overrides"][0]["verdict"].update(
        {
            "authority_kind": "ivan_text_override",
            "defer_to_text_override": True,
            "candidate_id": "auto_test",
            "override_document_sha256": "0" * 64,
            "source_srt_sha256": "1" * 64,
            "text_final_srt_sha256": "2" * 64,
        }
    )
    assert reconcile_pending_text_overrides(audit, manifest, delivery_start_ms=0)
    assert audit["pending_text_overrides"][0]["verdict_rebinding"]["status"] == (
        "UNCHANGED_ENTITY_REBOUND_TO_FROZEN_SOURCE"
    )
    assert audit["entity_verdicts"][0]["verdict"]["override_document_sha256"] == (
        document_hash
    )
    assert audit["entity_repairs"][0]["mode"] == (
        "chat_scaffold_plus_human_entity_override"
    )
    assert audit["entity_repairs"][0]["structured_exact_text"] == (
        "还没看，怎么有人说有梦限大的风险"
    )
    assert verify_chat_authority_final_surfaces(
        audit,
        final_text_srt=final,
        final_speaker_srt=final,
        delivery_start_ms=0,
        delivery_end_ms=20_000,
    )
    missing_prefix = _srt("前缀被丢了", "怎么有人说有梦限大的风险")
    assert not verify_chat_authority_final_surfaces(
        copy.deepcopy(audit),
        final_text_srt=missing_prefix,
        final_speaker_srt=missing_prefix,
        delivery_start_ms=0,
        delivery_end_ms=20_000,
    )


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
