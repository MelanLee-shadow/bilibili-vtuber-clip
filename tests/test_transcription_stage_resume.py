"""Success-only retry checkpoints through the actual ordinary transcriber.

All replies below are synthetic fixtures, never measured recognition quality.
"""

import copy
import json

import pytest

from src.autoslice import full_session_transcription as tx
from src.autoslice import llm_client
from src.autoslice.source_context_executor import AgyRunnerError
import scripts.free_asr_client as free

TEXT = "不是不是十五"
DRAFT = "1\n00:00:00,000 --> 00:00:01,000\n" + TEXT + "\n"
ASR = {
    "provider": "bcut",
    "elapsed_s": 0.1,
    "utterances": [{"start_time": 0, "end_time": 1000, "transcript": TEXT, "words": []}],
}


def fixture(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOSLICE_DISABLE_TOPIC_ENTITY_GRAPH", "1")
    media = tmp_path / "input.mp4"
    media.write_bytes(b"media")
    counters = {"asr": 0, "cpa": 0, "fidelity": 0}
    config = {
        "audio": b"mp3",
        "answer": json.dumps({"cues": [{"n": 1, "text": TEXT}]}),
        "model": "gpt-6-sol",
        "effort": "low",
        "identity": True,
        "result": copy.deepcopy(ASR),
    }
    monkeypatch.setattr(free, "extract_audio_mp3", lambda _: config["audio"])

    def asr(*_a, **_k):
        counters["asr"] += 1
        return copy.deepcopy(config["result"])

    def cpa(_):
        counters["cpa"] += 1
        reply = config["answer"]
        if isinstance(reply, Exception):
            raise reply
        return reply

    def builder(_):
        cpa.cpa_cache_identity = (
            {"models": [config["model"]], "effort": config["effort"]}
            if config["identity"]
            else None
        )
        return cpa

    original = tx.apply_subtitle_fidelity_guard

    def fidelity(*a, **k):
        counters["fidelity"] += 1
        return original(*a, **k)

    monkeypatch.setattr(free, "transcribe", asr)
    monkeypatch.setattr(llm_client, "build_llm_call", builder)
    monkeypatch.setattr(tx, "apply_subtitle_fidelity_guard", fidelity)

    def run(**kwargs):
        return tx._build_aggregate_asr_transcriber("localhost", correct="cpa", **kwargs)(media)

    return media, counters, config, run


def test_retry_reuses_bcut_and_complete_cpa_but_reexecutes_guards(tmp_path, monkeypatch):
    media, calls, _, run = fixture(tmp_path, monkeypatch)
    assert run() == DRAFT
    assert run() == DRAFT
    assert calls == {"asr": 1, "cpa": 1, "fidelity": 2}
    receipt = json.loads(media.with_suffix(".transcription-reuse.json").read_text())
    assert receipt["bcut"]["served_from_cache"] is True
    assert receipt["cpa"]["served_from_cache"] is True
    assert receipt["final_release_approved"] is False


@pytest.mark.parametrize("change", ["audio", "model", "effort", "context", "milliseconds"])
def test_changed_request_cannot_reuse_old_cpa(tmp_path, monkeypatch, change):
    media, calls, config, run = fixture(tmp_path, monkeypatch)
    run()
    args = {}
    if change == "audio":
        config["audio"] = b"different audio"
    elif change in ("model", "effort"):
        config[change] = "gpt-6-other" if change == "model" else "medium"
    elif change == "context":
        args = {
            "session_topic_authorities": [
                {"canonical": "原词", "room_title": "新语境", "chat_text": "文字来源"}
            ]
        }
    else:
        config["audio"] = b"different audio and subsecond timing"
        config["result"]["utterances"][0]["start_time"] = 1
    run(**args)
    assert calls["cpa"] == 2
    assert calls["asr"] == (2 if change in ("audio", "milliseconds") else 1)


@pytest.mark.parametrize(
    "answer", ["{}", '{"cues":[]}', llm_client.LlmCallError("temporary service outage")]
)
def test_failed_cpa_does_not_discard_successful_bcut_or_cache_failure(
    tmp_path, monkeypatch, answer
):
    _, calls, config, run = fixture(tmp_path, monkeypatch)
    config["answer"] = answer
    with pytest.raises(AgyRunnerError):
        run()
    config["answer"] = json.dumps({"cues": [{"n": 1, "text": TEXT}]})
    assert run() == DRAFT
    assert run() == DRAFT
    assert calls["asr"] == 1 and calls["cpa"] == 2


def test_unknown_model_identity_never_reuses_cpa(tmp_path, monkeypatch):
    _, calls, config, run = fixture(tmp_path, monkeypatch)
    config["identity"] = False
    run()
    run()
    assert calls["asr"] == 1 and calls["cpa"] == 2


def test_existing_unsealed_srt_is_not_a_success_checkpoint(tmp_path, monkeypatch):
    media, calls, _, run = fixture(tmp_path, monkeypatch)
    media.with_suffix(".cpa-reviewed.srt").write_text(DRAFT)
    media.with_suffix(".asr_draft.srt").write_text(DRAFT)
    run()
    assert calls["asr"] == calls["cpa"] == 1


def test_non_bcut_fallback_is_not_cached_as_bcut(tmp_path, monkeypatch):
    media, calls, config, run = fixture(tmp_path, monkeypatch)
    config["result"]["provider"] = "jianying"
    run()
    run()
    assert calls["asr"] == 2
    source = json.loads(media.with_suffix(".asr-source.json").read_text())
    assert source["provider"] == "jianying"


def test_cached_reply_is_reparsed_by_current_schema(tmp_path, monkeypatch):
    media, calls, _, run = fixture(tmp_path, monkeypatch)
    run()
    # Corrupt the cache; never accept its original output hash at face value.
    entries = list((media.parent / ".transcription-stage-cache").rglob("*.json"))
    assert len(entries) == 2
    cpa = next(p for p in entries if p.parent.name == "cpa")
    old = cpa.read_bytes()
    data = json.loads(old)
    data["payload"] = '{"cues":[]}'
    cpa.write_text(json.dumps(data))
    assert run() == DRAFT
    assert calls["cpa"] == 2


def test_cache_directory_symlink_does_not_redirect_writes(tmp_path, monkeypatch):
    media, calls, _, run = fixture(tmp_path, monkeypatch)
    outside = tmp_path / "outside"
    outside.mkdir()
    (media.parent / ".transcription-stage-cache").symlink_to(outside, target_is_directory=True)
    assert run() == DRAFT
    assert run() == DRAFT
    assert calls["asr"] == calls["cpa"] == 2
    assert list(outside.iterdir()) == []


def test_same_prompt_but_millisecond_grid_change_invalidates_review(tmp_path, monkeypatch):
    from src.autoslice.transcription_stage_cache import TranscriptionStageCache
    from src.autoslice.subtitle_draft_preparation import _required_cpa_cues

    calls = []

    def call(_):
        calls.append(1)
        return json.dumps({"cues": [{"n": 1, "text": TEXT}]})

    call.cpa_cache_identity = {"models": ["gpt-6-sol"], "effort": "low"}
    cache = TranscriptionStageCache(tmp_path / "input.mp4", b"same audio")
    for source in (DRAFT, DRAFT, DRAFT.replace("00:00:00,000", "00:00:00,001")):
        assert _required_cpa_cues(
            "same prompt rounded to seconds", call, 1, resume=cache, source_srt=source
        ) == {1: TEXT}
    assert len(calls) == 2


def test_rehashed_invalid_cached_schema_still_rejected(tmp_path, monkeypatch):
    from src.autoslice.transcription_stage_cache import _digest

    media, calls, _, run = fixture(tmp_path, monkeypatch)
    run()
    path = next((media.parent / ".transcription-stage-cache/cpa").glob("*.json"))
    value = json.loads(path.read_text())
    value["payload"] = '{"cues":[]}'
    value["payload_sha256"] = _digest(value["payload"])
    path.write_text(json.dumps(value))
    saved = path.read_bytes()
    assert run() == DRAFT
    assert calls["cpa"] == 2
    assert path.read_bytes() == saved  # preserve bad evidence instead of silently rewriting it


def test_concurrent_bcut_users_share_one_real_dispatch(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    import time
    from src.autoslice.transcription_stage_cache import TranscriptionStageCache

    calls = []

    def fetch():
        calls.append(1)
        time.sleep(0.05)
        return copy.deepcopy(ASR)

    def run(_):
        return TranscriptionStageCache(tmp_path / "input.mp4", b"same").bcut(fetch)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outputs = list(pool.map(run, range(2)))
    assert outputs == [ASR, ASR] and calls == [1]


def test_failed_bcut_is_not_a_cached_empty_success(tmp_path):
    from src.autoslice.transcription_stage_cache import TranscriptionStageCache

    cache = TranscriptionStageCache(tmp_path / "input.mp4", b"audio")
    failed = {"provider": "bcut", "utterances": []}
    assert cache.bcut(lambda: failed) == failed
    assert not list((tmp_path / ".transcription-stage-cache/bcut").glob("*.json"))
    assert cache.bcut(lambda: copy.deepcopy(ASR)) == ASR
