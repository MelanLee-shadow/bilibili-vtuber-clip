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
