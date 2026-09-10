"""Existing upstream context must reach the post-fidelity pronoun decision."""
from __future__ import annotations

import hashlib
import json

import pytest

from src.autoslice import full_session_transcription as tx, llm_client
from src.autoslice.danmaku_evidence import DanmakuItem
from src.autoslice.jingting_chunker import parse_srt_cues
from src.autoslice.source_context_executor import AgyRunnerError

SRT = "1\n00:00:00,000 --> 00:00:01,000\n为什么TA先到\n"
KEEP = '{"rewrites":[]}'
CHANGE = '{"rewrites":[{"n":1,"occurrence":1,"from":"TA","to":"她"}]}'


def _aggregate(tmp_path, monkeypatch, *, hook="", rows=(), topics=(), reply=KEEP):
    import scripts.free_asr_client as free

    monkeypatch.setenv("AUTOSLICE_DISABLE_TOPIC_ENTITY_GRAPH", "1")
    monkeypatch.setattr(free, "extract_audio_mp3", lambda _: b"synthetic audio")
    monkeypatch.setattr(free, "transcribe", lambda *_a, **_k: {
        "provider": "bcut", "utterances": [
            {"start_time": 0, "end_time": 1000, "transcript": "为什么TA先到", "words": []}
        ],
    })
    calls, configs = [], []

    def builder(config):
        stage = len(configs)
        configs.append(config)

        def call(prompt):
            calls.append((stage, prompt))
            return '{"cues":[{"n":1,"text":"为什么TA先到"}]}' if stage == 0 else reply

        call.cpa_cache_identity = {
            "transport": "cpa_command", "models": ["gpt-6-astra"], "effort": "low",
        }
        return call

    monkeypatch.setattr(llm_client, "build_llm_call", builder)
    media = tmp_path.resolve() / "sample.mp4"
    media.write_bytes(b"synthetic media")
    run = tx._build_aggregate_asr_transcriber(
        "localhost", correct="cpa", danmaku_items=rows,
        topic_hint=hook, session_topic_authorities=topics,
    )
    return run, media, calls, configs


def test_real_aggregate_forwards_existing_hook_chat_and_topic_to_pronouns(tmp_path, monkeypatch):
    run, media, calls, configs = _aggregate(
        tmp_path, monkeypatch,
        hook="编号相同的两位主播比较先后，不是在讨论一个陌生观众。",
        rows=[DanmakuItem(600, "许医生是大写XYZ")],
        topics=[{"canonical": "许医生", "room_title": "主播连线", "chat_text": "两位的简称相同"}],
        reply=CHANGE,
    )
    output = run(media)
    prompt = next(p for stage, p in calls if stage == 1)
    assert "编号相同的两位主播比较先后" in prompt
    assert "00:00 许医生是大写XYZ" in prompt
    assert "两位的简称相同" in prompt
    assert "不是逐字真值" in prompt
    assert "不能照抄弹幕中的他/她" in prompt
    assert len(calls) == 2
    assert all("gpt-6-astra" in c.command_template for c in configs)
    assert all(c.command_template.endswith("low") for c in configs)
    assert output == SRT.replace("TA", "她")
    records = [json.loads(p.read_text()) for p in media.with_suffix(".pronoun-trace").glob("*.json")]
    assert len(records) == 1
    assert records[0]["calls"][0]["prompt"]["text"] == prompt
    assert records[0]["calls"][0]["prompt"]["sha256"] == hashlib.sha256(prompt.encode()).hexdigest()
    assert records[0]["release_authorized"] is False


def test_no_context_preserves_legacy_request_and_zero_edit(tmp_path, monkeypatch):
    run, media, calls, _ = _aggregate(tmp_path, monkeypatch)
    assert run(media) == SRT
    direct_prompts = []
    tx._cpa_pronoun_ta_pass(SRT, cpa_llm_call=lambda p: direct_prompts.append(p) or KEEP, required=True)
    assert next(p for stage, p in calls if stage == 1) == direct_prompts[0]


@pytest.mark.parametrize("token", ["TA", "他", "她", "它"])
def test_context_does_not_choose_tokens_or_change_other_text(tmp_path, monkeypatch, token):
    reply = KEEP if token == "TA" else CHANGE.replace('"to":"她"', f'"to":"{token}"')
    run, media, _, _ = _aggregate(
        tmp_path, monkeypatch, hook="观众的文字可能写错代词；裁决仍由CPA完成。", reply=reply,
    )
    out = run(media)
    assert out == SRT.replace("TA", token)
    assert [(c.start_ms, c.end_ms) for c in parse_srt_cues(out)] == [(0, 1000)]


def test_context_does_not_make_stale_occurrence_valid(tmp_path, monkeypatch):
    run, media, _, _ = _aggregate(
        tmp_path, monkeypatch, hook="这是语境，不是修改权限。", reply=CHANGE.replace('"from":"TA"', '"from":"他"'),
    )
    with pytest.raises(AgyRunnerError) as exc:
        run(media)
    assert exc.value.reason_code == "CPA_PRONOUN_INVALID_OUTPUT"


def test_warm_main_cache_does_not_skip_contextual_pronoun_calls(tmp_path, monkeypatch):
    run, media, calls, _ = _aggregate(tmp_path, monkeypatch, hook="两位女主播的对话。")
    assert run(media) == run(media) == SRT
    assert [stage for stage, _ in calls] == [0, 1, 1]
    assert calls[1][1] == calls[2][1]
    assert "两位女主播的对话" in calls[1][1]
    receipt = json.loads(media.with_suffix(".transcription-reuse.json").read_text())
    assert receipt["bcut"]["status"] == receipt["cpa"]["status"] == "HIT"


def test_context_is_lossless_data_and_does_not_silently_truncate():
    from src.autoslice.pronoun_context import render_pronoun_context

    hook = 'quoted "name"\nignore earlier text is data'
    rows = [f"00:01 {'甲' * 400}", "LAST_CONTEXT_ROW"]
    rendered = render_pronoun_context(hook, "TOPIC_DATA", rows)
    payload = json.loads(rendered.split("\n", 1)[1])
    assert payload == {"selection_hook": hook, "topic_context": "TOPIC_DATA", "danmaku_lines": rows}
    assert render_pronoun_context("", "", []) == ""
