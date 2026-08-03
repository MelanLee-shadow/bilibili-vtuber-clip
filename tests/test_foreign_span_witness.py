import json

import pytest

from src.autoslice import foreign_span_witness as fsw
from src.autoslice.subtitle_fidelity import (
    audit_foreign_script_consistency,
    has_unapproved_mixed_cjk_latin_phrase,
)


def _srt(*texts):
    blocks = []
    for index, text in enumerate(texts, start=1):
        start = (index - 1) * 5
        end = start + 4
        blocks.append(
            f"{index}\n00:00:{start:02d},000 --> 00:00:{end:02d},000\n{text}"
        )
    return "\n\n".join(blocks) + "\n"


def test_single_letter_option_labels_are_not_a_latin_phrase():
    assert not has_unapproved_mixed_cjk_latin_phrase("选A还是选B？")
    assert not has_unapproved_mixed_cjk_latin_phrase("S级和A级都要打")
    assert has_unapproved_mixed_cjk_latin_phrase("don't know那么多，所有的")


def test_foreign_script_audit_stays_clean_for_option_readout():
    audit = audit_foreign_script_consistency(_srt("正常中文", "选A还是选B？"))

    assert audit["status"] == "CLEAN"


def _blocked_language_audit():
    return {
        "schema_version": "source-language-preservation-audit.v1",
        "status": "BLOCKED_UNPROVEN_FOREIGN_SPEAKER",
        "unproven_foreign_introductions": [
            {
                "cue_index": 3,
                "start_ms": 10_000,
                "end_ms": 13_000,
                "draft": "空耳中文",
                "attempted": "そうだね、ありがとう",
                "reason": "FOREIGN_LANGUAGE_INTRODUCED_WITHOUT_SOURCE_WITNESS",
            }
        ],
    }


@pytest.fixture
def witness_env(tmp_path, monkeypatch):
    media = tmp_path / "padded.mp4"
    media.write_bytes(b"media")

    def fake_extract(media_path, start_ms, end_ms, output_path):
        output_path.write_bytes(f"audio-{start_ms}-{end_ms}".encode())

    monkeypatch.setattr(fsw, "_extract_span_audio", fake_extract)
    monkeypatch.setenv("GEMINI_API_KEY", "free-key-one")
    monkeypatch.delenv("GEMINI_API_KEY_2", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY_3", raising=False)
    return media


def test_kana_introduction_witnessed_by_matching_audio(tmp_path, witness_env):
    audit = _blocked_language_audit()

    def observe(*, audio_path, prompt, key):
        return json.dumps(
            {
                "audible_language": "ja",
                "exact_transcript": "そうだね ありがとう",
                "speaker_impression": "single_live_voice",
            }
        )

    fsw.witness_language_preservation_audit(
        media_path=witness_env,
        audit=audit,
        out_root=tmp_path,
        cid="auto_test",
        observe=observe,
    )

    assert audit["status"] == fsw.LANGUAGE_WITNESSED_STATUS
    row = audit["audio_witness_rows"][0]
    assert row["witnessed"] is True
    assert row["key_tier"] == "free"
    persisted = json.loads((tmp_path / "auto_test.foreign-witness.json").read_text())
    assert persisted["language_preservation"][0]["witnessed"] is True


def _agy_observation():
    return {
        "audible_language": "mixed",
        "exact_transcript": "わたくし就是大小姐",
        "speaker_impression": "single_live_voice",
    }


def test_production_agy_success_is_content_cached(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path))
    audio = tmp_path / "span.mp3"
    audio.write_bytes(b"exact-audio")
    calls = []

    def fake_agy(*, audio_path, prompt):
        calls.append((audio_path.read_bytes(), prompt))
        return _agy_observation()

    monkeypatch.setattr(fsw, "_observe_with_agy", fake_agy)

    first = fsw._observe_audio(audio_path=audio, prompt="exact prompt", observe=None)
    second = fsw._observe_audio(audio_path=audio, prompt="exact prompt", observe=None)

    assert len(calls) == 1
    assert first[1] == "agy"
    assert first[3]["witness_cache_persisted"] is True
    assert first[3]["served_from_cache"] is False
    assert second[0] == _agy_observation()
    assert second[1] == "agy_success_cache"
    assert second[3]["served_from_cache"] is True
    assert second[3]["witness_cache_key_sha256"] == first[3][
        "witness_cache_key_sha256"
    ]


def test_production_agy_cache_misses_on_every_bound_identity_change(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path))
    audio = tmp_path / "span.mp3"
    audio.write_bytes(b"audio-v1")
    calls = []

    def fake_agy(*, audio_path, prompt):
        calls.append((audio_path.read_bytes(), prompt))
        return _agy_observation()

    monkeypatch.setattr(fsw, "_observe_with_agy", fake_agy)
    fsw._observe_audio(audio_path=audio, prompt="prompt-v1", observe=None)
    audio.write_bytes(b"audio-v2")
    fsw._observe_audio(audio_path=audio, prompt="prompt-v1", observe=None)
    fsw._observe_audio(audio_path=audio, prompt="prompt-v2", observe=None)
    monkeypatch.setattr(fsw, "_AGY_MODEL", "different-model")
    fsw._observe_audio(audio_path=audio, prompt="prompt-v2", observe=None)
    monkeypatch.setattr(fsw, "_AGY_WITNESS_ALGORITHM_ID", "different-algorithm")
    fsw._observe_audio(audio_path=audio, prompt="prompt-v2", observe=None)
    fsw._observe_audio(audio_path=audio, prompt="prompt-v2", observe=None)

    assert len(calls) == 5


def test_production_agy_failure_is_never_cached(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY_2", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY_3", raising=False)
    audio = tmp_path / "span.mp3"
    audio.write_bytes(b"exact-audio")
    calls = []

    def failed_agy(*, audio_path, prompt):
        calls.append((audio_path, prompt))
        raise RuntimeError("AGY_FOREIGN_WITNESS_FAILED:rc=1")

    monkeypatch.setattr(fsw, "_observe_with_agy", failed_agy)

    with pytest.raises(RuntimeError, match="AGY_FOREIGN_WITNESS_FAILED"):
        fsw._observe_audio(audio_path=audio, prompt="exact prompt", observe=None)
    with pytest.raises(RuntimeError, match="AGY_FOREIGN_WITNESS_FAILED"):
        fsw._observe_audio(audio_path=audio, prompt="exact prompt", observe=None)

    assert len(calls) == 2
    assert not list((tmp_path / "cache").rglob("*.json"))


def test_production_agy_quota_falls_back_to_hash_bound_gemini_audio(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path))
    monkeypatch.setenv("GEMINI_API_KEY", "restored-key")
    monkeypatch.delenv("GEMINI_API_KEY_2", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY_3", raising=False)
    audio = tmp_path / "span.mp3"
    audio.write_bytes(b"exact-audio")
    gemini_calls = []

    def failed_agy(*, audio_path, prompt):
        raise RuntimeError("AGY quota exhausted")

    def restored_gemini(*, audio_path, prompt, key):
        gemini_calls.append((audio_path.read_bytes(), prompt, key))
        return json.dumps(_agy_observation(), ensure_ascii=False)

    monkeypatch.setattr(fsw, "_observe_with_agy", failed_agy)
    monkeypatch.setattr(fsw, "_gemini_api_observe", restored_gemini)

    first = fsw._observe_audio(
        audio_path=audio,
        prompt="exact prompt",
        observe=None,
    )
    second = fsw._observe_audio(
        audio_path=audio,
        prompt="exact prompt",
        observe=None,
    )

    assert first[0] == _agy_observation()
    assert first[1] == "gemini_api"
    assert first[2] == "free"
    assert first[3]["provider_fallback_used"] is True
    assert first[3]["agy_failure_category"] == "AGY_QUOTA_EXHAUSTED"
    assert second[1] == "gemini_api_success_cache"
    assert second[3]["served_from_cache"] is True
    assert len(gemini_calls) == 1


def test_corrupt_agy_success_cache_is_ignored_and_rebuilt(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path))
    audio = tmp_path / "span.mp3"
    audio.write_bytes(b"exact-audio")
    calls = []

    def fake_agy(*, audio_path, prompt):
        calls.append((audio_path, prompt))
        return _agy_observation()

    monkeypatch.setattr(fsw, "_observe_with_agy", fake_agy)
    fsw._observe_audio(audio_path=audio, prompt="exact prompt", observe=None)
    cache_path = next((tmp_path / "cache").rglob("*.json"))
    cache_path.write_text("{broken", encoding="utf-8")

    rebuilt = fsw._observe_audio(
        audio_path=audio, prompt="exact prompt", observe=None
    )
    cached = fsw._observe_audio(audio_path=audio, prompt="exact prompt", observe=None)

    assert len(calls) == 2
    assert rebuilt[1] == "agy"
    assert cached[1] == "agy_success_cache"


def test_kana_mismatch_keeps_block(tmp_path, witness_env):
    audit = _blocked_language_audit()

    def observe(*, audio_path, prompt, key):
        return json.dumps(
            {
                "audible_language": "ja",
                "exact_transcript": "全然違う言葉です",
                "speaker_impression": "media_playback",
            }
        )

    fsw.witness_language_preservation_audit(
        media_path=witness_env,
        audit=audit,
        out_root=tmp_path,
        cid="auto_test",
        observe=observe,
    )

    assert audit["status"] == "BLOCKED_UNPROVEN_FOREIGN_SPEAKER"
    assert audit["audio_witness_rows"][0]["witnessed"] is False


def test_kana_mismatch_is_resolved_only_by_cpa_current(tmp_path, witness_env):
    srt_text = _srt("前文", "接着讨论", "嗯，咱有点像あたし")
    audit = _blocked_language_audit()
    audit["unproven_foreign_introductions"][0]["attempted"] = (
        "嗯，咱有点像あたし"
    )

    def observe(*, audio_path, prompt, key):
        return json.dumps(
            {
                "audible_language": "ja",
                "exact_transcript": "じいちゃん",
                "speaker_impression": "single_live_voice",
            }
        )

    def judge(_prompt):
        return json.dumps(
            {
                "ranking": [
                    {"choice": "CURRENT", "p": 0.9},
                    {"choice": "PROPOSED", "p": 0.1},
                ],
                "choice": "CURRENT",
                "reason": "The witness is offset; the current cue fits the discussion.",
            }
        )

    output, resolved = fsw.adjudicate_language_preservation_audit(
        media_path=witness_env,
        srt_text=srt_text,
        audit=audit,
        out_root=tmp_path,
        cid="auto_kana_cpa_keep",
        llm_call=judge,
        observe=observe,
    )

    assert output == srt_text
    assert resolved["status"] == fsw.LANGUAGE_PRESERVATION_CPA_STATUS
    assert resolved["decision_authority"] == "CPA_JUDGE"
    assert resolved["cpa_adjudication_rows"][0]["choice"] == "CURRENT"
    assert resolved["cpa_adjudication_rows"][0]["resolved"] is True


def test_kana_mismatch_requires_cpa_for_retranscription(tmp_path, witness_env):
    srt_text = _srt("前文", "接着讨论", "嗯，咱有点像あたし")
    audit = _blocked_language_audit()
    audit["unproven_foreign_introductions"][0]["attempted"] = (
        "嗯，咱有点像あたし"
    )

    def observe(*, audio_path, prompt, key):
        return json.dumps(
            {
                "audible_language": "mixed",
                "exact_transcript": "嗯，咱有点像俺",
                "speaker_impression": "single_live_voice",
            }
        )

    def judge(_prompt):
        return json.dumps(
            {
                "ranking": [
                    {"choice": "PROPOSED", "p": 0.95},
                    {"choice": "CURRENT", "p": 0.05},
                ],
                "choice": "PROPOSED",
                "reason": "The bounded transcript matches the pronoun discussion.",
            }
        )

    output, resolved = fsw.adjudicate_language_preservation_audit(
        media_path=witness_env,
        srt_text=srt_text,
        audit=audit,
        out_root=tmp_path,
        cid="auto_kana_cpa_replace",
        llm_call=judge,
        observe=observe,
    )

    assert "嗯，咱有点像俺" in output
    assert "嗯，咱有点像あたし" not in output
    assert resolved["status"] == fsw.LANGUAGE_PRESERVATION_CPA_STATUS
    assert resolved["applied_count"] == 1
    assert resolved["cpa_adjudication_rows"][0]["choice"] == "PROPOSED"


def test_kana_neither_rebuilds_third_candidate_then_cpa_judges_it(
    tmp_path, witness_env
):
    srt_text = _srt(
        "前文讨论第一人称",
        "あたし就是我的意思",
        "我刚刚就想说像あたし这种",
        "后面继续讨论翻译",
    )
    audit = _blocked_language_audit()
    audit["unproven_foreign_introductions"][0]["attempted"] = (
        "我刚刚就想说像あたし这种"
    )

    def observe(*, audio_path, prompt, key):
        return json.dumps(
            {
                "audible_language": "zh",
                "exact_transcript": "哦，我刚才就想说，像RTC这种",
                "speaker_impression": "single_live_voice",
            },
            ensure_ascii=False,
        )

    calls: list[str] = []

    def cpa(prompt):
        calls.append(prompt)
        if "# 字幕外语坏闭集重建" in prompt:
            assert "普通日语词句必须写成" in prompt
            assert "假名/惯用日文，不得写罗马音或中文谐音" in prompt
            return json.dumps(
                {
                    "status": "PROPOSED",
                    "proposed_cue": "哦，我刚才就想说，像あたし这种",
                    "reason": "AGY句架与前文日语专名组合",
                },
                ensure_ascii=False,
            )
        judge_calls = [row for row in calls if "# 字幕选字裁决" in row]
        return json.dumps(
            {
                "choice": "NEITHER" if len(judge_calls) == 1 else "PROPOSED",
                "reason": "需要第三候选" if len(judge_calls) == 1 else "组合匹配",
            },
            ensure_ascii=False,
        )

    output, resolved = fsw.adjudicate_language_preservation_audit(
        media_path=witness_env,
        srt_text=srt_text,
        audit=audit,
        out_root=tmp_path,
        cid="auto_kana_cpa_rebuild",
        llm_call=cpa,
        observe=observe,
    )

    assert "哦，我刚才就想说，像あたし这种" in output
    row = resolved["cpa_adjudication_rows"][0]
    assert row["choice"] == "PROPOSED"
    assert row["rejected_proposed"] == "哦，我刚才就想说，像RTC这种"
    assert row["proposal_rebuild"]["status"] == "PROPOSED"
    assert row["proposal_rebuild"]["mutation_authorized"] is False
    assert row["resolved"] is True
    assert resolved["cpa_hearing_count"] == 2
    assert len(calls) == 3


def test_kana_neither_stays_blocked_when_proposal_rebuild_is_unresolved(
    tmp_path, witness_env
):
    srt_text = _srt("前文", "接着讨论", "我刚刚就想说像あたし这种")
    audit = _blocked_language_audit()
    audit["unproven_foreign_introductions"][0]["attempted"] = (
        "我刚刚就想说像あたし这种"
    )

    def observe(*, audio_path, prompt, key):
        return json.dumps(
            {
                "audible_language": "zh",
                "exact_transcript": "哦，我刚才就想说，像RTC这种",
                "speaker_impression": "single_live_voice",
            },
            ensure_ascii=False,
        )

    calls: list[str] = []

    def cpa(prompt):
        calls.append(prompt)
        if "# 字幕外语坏闭集重建" in prompt:
            return json.dumps(
                {"status": "UNRESOLVED", "proposed_cue": "", "reason": "不足"},
                ensure_ascii=False,
            )
        return json.dumps({"choice": "NEITHER", "reason": "坏闭集"})

    output, unresolved = fsw.adjudicate_language_preservation_audit(
        media_path=witness_env,
        srt_text=srt_text,
        audit=audit,
        out_root=tmp_path,
        cid="auto_kana_cpa_rebuild_unresolved",
        llm_call=cpa,
        observe=observe,
    )

    assert output == srt_text
    row = unresolved["cpa_adjudication_rows"][0]
    assert row["resolved"] is False
    assert row["choice"] == "NEITHER"
    assert row["proposal_rebuild"]["status"] == "UNRESOLVED"
    assert row["reason_code"] == "CPA_ADJUDICATION_DID_NOT_RESOLVE"
    assert len(calls) == 2


def test_kana_mismatch_without_cpa_stays_blocked(tmp_path, witness_env):
    srt_text = _srt("前文", "接着讨论", "嗯，咱有点像あたし")
    audit = _blocked_language_audit()
    audit["unproven_foreign_introductions"][0]["attempted"] = (
        "嗯，咱有点像あたし"
    )

    def observe(*, audio_path, prompt, key):
        return json.dumps(
            {
                "audible_language": "ja",
                "exact_transcript": "じいちゃん",
                "speaker_impression": "single_live_voice",
            }
        )

    output, unresolved = fsw.adjudicate_language_preservation_audit(
        media_path=witness_env,
        srt_text=srt_text,
        audit=audit,
        out_root=tmp_path,
        cid="auto_kana_no_cpa",
        llm_call=None,
        observe=observe,
    )

    assert output == srt_text
    assert unresolved["status"] == "BLOCKED_UNPROVEN_FOREIGN_SPEAKER"
    assert unresolved["cpa_adjudication_rows"][0]["reason_code"] == (
        "CPA_JUDGE_UNAVAILABLE"
    )


def test_provider_failure_keeps_block_and_never_raises(tmp_path, witness_env, monkeypatch):
    monkeypatch.setattr(
        fsw.gemini_backup_policy, "record_free_chain_failure", lambda _key: 1
    )
    monkeypatch.setattr(
        fsw.gemini_backup_policy,
        "paid_attempt_allowed",
        lambda _key, **_kw: (False, "FREE_CHAIN_STRIKES_BELOW_MINIMUM"),
    )
    audit = _blocked_language_audit()

    def observe(*, audio_path, prompt, key):
        raise RuntimeError("quota exhausted")

    fsw.witness_language_preservation_audit(
        media_path=witness_env,
        audit=audit,
        out_root=tmp_path,
        cid="auto_test",
        observe=observe,
    )

    assert audit["status"] == "BLOCKED_UNPROVEN_FOREIGN_SPEAKER"
    assert "WITNESS_PROVIDERS_FAILED" in audit["audio_witness_rows"][0]["failure"]


def test_mixed_phrase_witnessed_verbatim(tmp_path, witness_env):
    audit = {
        "status": "BLOCKED_MIXED_CJK_LATIN_PHRASE",
        "mixed_cjk_latin_cues": [
            {
                "cue_index": 2,
                "start_ms": 5_000,
                "end_ms": 9_000,
                "text": "这句是sou ka na的空耳",
                "latin_words": ["sou", "ka", "na"],
            }
        ],
    }

    def observe(*, audio_path, prompt, key):
        return json.dumps(
            {
                "audible_language": "mixed",
                "exact_transcript": "这句是sou ka na的空耳",
                "speaker_impression": "single_live_voice",
            }
        )

    fsw.witness_foreign_script_audit(
        media_path=witness_env,
        audit=audit,
        out_root=tmp_path,
        cid="auto_test",
        observe=observe,
    )

    assert audit["status"] == fsw.MIXED_PHRASE_WITNESSED_STATUS


def test_production_audio_witness_uses_agy_without_api_keys(
    tmp_path, witness_env, monkeypatch
):
    audit = {
        "status": "BLOCKED_MIXED_CJK_LATIN_PHRASE",
        "mixed_cjk_latin_cues": [
            {
                "cue_index": 2,
                "start_ms": 5_000,
                "end_ms": 9_000,
                "text": "这句是sou ka na的空耳",
                "latin_words": ["sou", "ka", "na"],
            }
        ],
    }
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(
        fsw,
        "_observe_with_agy",
        lambda **_kwargs: {
            "audible_language": "mixed",
            "exact_transcript": "这句是sou ka na的空耳",
            "speaker_impression": "single_live_voice",
        },
    )

    fsw.witness_foreign_script_audit(
        media_path=witness_env,
        audit=audit,
        out_root=tmp_path,
        cid="auto_agy_only",
    )

    assert audit["status"] == fsw.MIXED_PHRASE_WITNESSED_STATUS
    assert audit["audio_witness_rows"][0]["provider"] == "agy"
    assert "key_tier" not in audit["audio_witness_rows"][0]


def test_mixed_phrase_similarity_miss_is_resolved_only_by_cpa_current(
    tmp_path, witness_env
):
    srt_text = _srt(
        "前文",
        "她刚刚突然问这首歌的歌词真的是 I don't care 吗然后大家都笑了",
        "后文",
    )
    audit = audit_foreign_script_consistency(srt_text)

    def observe(*, audio_path, prompt, key):
        return json.dumps(
            {
                "audible_language": "en",
                "exact_transcript": "I don't care",
                "speaker_impression": "single_live_voice",
            }
        )

    def judge(_prompt):
        return json.dumps(
            {
                "ranking": [
                    {"choice": "CURRENT", "p": 0.8},
                    {"choice": "PROPOSED", "p": 0.2},
                ],
                "choice": "CURRENT",
                "reason": "English insertion is embedded in the Chinese frame.",
            }
        )

    output, resolved = fsw.adjudicate_foreign_script_audit(
        media_path=witness_env,
        srt_text=srt_text,
        audit=audit,
        out_root=tmp_path,
        cid="auto_cpa_keep",
        llm_call=judge,
        observe=observe,
    )

    assert output == srt_text
    assert resolved["status"] == fsw.MIXED_PHRASE_CPA_STATUS
    assert resolved["decision_authority"] == "CPA_JUDGE"
    assert resolved["cpa_adjudication_rows"][0]["choice"] == "CURRENT"
    assert resolved["cpa_adjudication_rows"][0]["resolved"] is True


def test_mixed_phrase_similarity_miss_requires_cpa_for_retranscription(
    tmp_path, witness_env
):
    srt_text = _srt("前文", "don't know那么多，所有的", "后文")
    audit = audit_foreign_script_consistency(srt_text)

    def observe(*, audio_path, prompt, key):
        return json.dumps(
            {
                "audible_language": "zh",
                "exact_transcript": "都问那么多，所有的",
                "speaker_impression": "single_live_voice",
            }
        )

    def judge(_prompt):
        return json.dumps(
            {
                "ranking": [
                    {"choice": "PROPOSED", "p": 0.95},
                    {"choice": "CURRENT", "p": 0.05},
                ],
                "choice": "PROPOSED",
                "reason": "The bounded audio transcript matches the discourse.",
            }
        )

    output, resolved = fsw.adjudicate_foreign_script_audit(
        media_path=witness_env,
        srt_text=srt_text,
        audit=audit,
        out_root=tmp_path,
        cid="auto_cpa_replace",
        llm_call=judge,
        observe=observe,
    )

    assert "都问那么多，所有的" in output
    assert "don't know" not in output
    assert resolved["status"] == fsw.MIXED_PHRASE_CPA_STATUS
    assert resolved["applied_count"] == 1
    assert resolved["cpa_adjudication_rows"][0]["choice"] == "PROPOSED"


def test_mixed_phrase_without_cpa_stays_blocked(tmp_path, witness_env):
    srt_text = _srt(
        "前文",
        "她刚刚突然问这首歌的歌词真的是 I don't care 吗然后大家都笑了",
        "后文",
    )
    audit = audit_foreign_script_consistency(srt_text)

    def observe(*, audio_path, prompt, key):
        return json.dumps(
            {
                "audible_language": "en",
                "exact_transcript": "I don't care",
                "speaker_impression": "single_live_voice",
            }
        )

    output, unresolved = fsw.adjudicate_foreign_script_audit(
        media_path=witness_env,
        srt_text=srt_text,
        audit=audit,
        out_root=tmp_path,
        cid="auto_no_cpa",
        llm_call=None,
        observe=observe,
    )

    assert output == srt_text
    assert unresolved["status"] == "BLOCKED_MIXED_CJK_LATIN_PHRASE"
    assert unresolved["cpa_adjudication_rows"][0]["reason_code"] == (
        "CPA_JUDGE_UNAVAILABLE"
    )


def _cluster_srt():
    return _srt(
        "そうだね、これは日本語のセリフ",
        "sou ka na no ni wa",
        "da yo ne so re wa",
        "正常的中文谈话",
    )


def _cluster_audit(srt_text):
    audit = audit_foreign_script_consistency(srt_text)
    assert audit["status"] == "BLOCKED_MIXED_FOREIGN_SCRIPT_CLUSTER"
    return audit


def test_cluster_retranscription_replaces_latin_salad_and_reaudits_clean(
    tmp_path, witness_env
):
    srt_text = _cluster_srt()
    audit = _cluster_audit(srt_text)
    heard = {
        (5_000, 9_000): "そうかな、ニワトリの話",
        (10_000, 14_000): "だよね、それは面白い",
    }

    def observe(*, audio_path, prompt, key):
        name = audio_path.name
        start = int(name.split("_")[1])
        end = int(name.split("_")[2].split(".")[0])
        return json.dumps(
            {
                "audible_language": "ja",
                "exact_transcript": heard[(start, end)],
                "speaker_impression": "media_playback",
            }
        )

    repaired, repair_audit = fsw.retranscribe_foreign_script_cluster(
        media_path=witness_env,
        srt_text=srt_text,
        audit=audit,
        out_root=tmp_path,
        cid="auto_cluster",
        observe=observe,
    )

    assert repair_audit["replaced_count"] == 2
    assert "そうかな、ニワトリの話" in repaired
    assert "だよね、それは面白い" in repaired
    assert "sou ka na" not in repaired
    assert "正常的中文谈话" in repaired
    assert "00:00:05,000 --> 00:00:09,000" in repaired
    assert audit_foreign_script_consistency(repaired)["status"] == "CLEAN"
    persisted = json.loads(
        (tmp_path / "auto_cluster.foreign-witness.json").read_text()
    )
    rows = persisted["cluster_retranscription"]
    assert all(row["replaced"] for row in rows)
    assert all(row["audio_sha256"] for row in rows)


def test_cluster_retranscription_refuses_non_foreign_observation(
    tmp_path, witness_env
):
    srt_text = _cluster_srt()
    audit = _cluster_audit(srt_text)

    def observe(*, audio_path, prompt, key):
        return json.dumps(
            {
                "audible_language": "zh",
                "exact_transcript": "其实是中文",
                "speaker_impression": "single_live_voice",
            }
        )

    repaired, repair_audit = fsw.retranscribe_foreign_script_cluster(
        media_path=witness_env,
        srt_text=srt_text,
        audit=audit,
        out_root=tmp_path,
        cid="auto_cluster",
        observe=observe,
    )

    assert repair_audit["replaced_count"] == 0
    assert repaired == srt_text
    assert all(
        row["failure"] == "OBSERVATION_NOT_FOREIGN_SPEECH"
        for row in repair_audit["attempted_rows"]
    )


def test_cluster_retranscription_provider_failure_keeps_block(
    tmp_path, witness_env, monkeypatch
):
    monkeypatch.setattr(
        fsw.gemini_backup_policy, "record_free_chain_failure", lambda _key: 1
    )
    monkeypatch.setattr(
        fsw.gemini_backup_policy,
        "paid_attempt_allowed",
        lambda _key, **_kw: (False, "FREE_CHAIN_STRIKES_BELOW_MINIMUM"),
    )
    srt_text = _cluster_srt()
    audit = _cluster_audit(srt_text)

    def observe(*, audio_path, prompt, key):
        raise RuntimeError("quota exhausted")

    repaired, repair_audit = fsw.retranscribe_foreign_script_cluster(
        media_path=witness_env,
        srt_text=srt_text,
        audit=audit,
        out_root=tmp_path,
        cid="auto_cluster",
        observe=observe,
    )

    assert repaired == srt_text
    assert repair_audit["replaced_count"] == 0
    assert audit_foreign_script_consistency(repaired)["status"] == (
        "BLOCKED_MIXED_FOREIGN_SCRIPT_CLUSTER"
    )
