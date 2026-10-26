"""Range relisten after an interrupted, outcome-unknown dispatch (40, 维护者).

Rule (40-subtitle-text 「硬退出后该预留仍保留，但不再把'结果未知'作为永久停工理由……
先尝试取回结果，无法取回则有界重发」「旧未知 attempt 保留 DISPATCHED，新尝试独立追加」
「同 provider/model/精确窗口三次累计尝试是软压力提示；只在真实传输失败、可恢复中断
或新增语义疑点时继续」): the range owner first reads back the original stored
result of the same request (identified by route, model, source bytes, window,
prompt and input bytes, never by output directory); if nothing is retrievable it
re-sends through the same ordinary entry and appends a new attempt; the earlier
row is never cleared or rewritten; at the soft pressure of three attempts on
the same provider/model/window it continues only with a new reason, otherwise
it records the refusal.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from src.autoslice import gemini_range_context as gemini
from tests.test_gemini_range_context import _check_request, _range_request, _verifier

SRT = "1\n00:00:01,000 --> 00:00:03,000\nspoken words\n\n"


class _Killed(BaseException):
    """The process is stopped after the provider was reserved and called."""


def _raw(request):
    duration = request["target_audio_end_ms"] - request["target_audio_start_ms"]
    return {"status": "OBSERVED", "target_audible": True, "full_transcript": "heard complete block",
            "segments": [{"segment": "complete_block", "start_ms": 0, "end_ms": duration,
                          "transcript": "heard complete block"}]}


def _world(tmp_path, monkeypatch):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source-media")
    account = tmp_path / "account"
    account.mkdir()
    receipt = account / "native-audio-budget.json"
    monkeypatch.setattr(gemini, "_extract_exact_mp3",
                        lambda src, out, start, end: out.write_bytes(f"mp3:{start}:{end}".encode()))
    monkeypatch.setattr(gemini, "_crop_black_frame_audio",
                        lambda **kw: (kw["audio_path"].write_bytes(b"black-frame-mp4") or True, ""))
    calls = {"dispatched": 0, "provider_body": 0}

    def provider(*, kill=False, pair=("agy", "test-agy-model")):
        def observe(**kwargs):
            kwargs["on_provider_dispatch"](*pair)
            calls["dispatched"] += 1
            prompt_path = kwargs["job_dir"] / "prompt.md"
            prompt_path.write_text(gemini.range_transcript_prompt(kwargs["request"]), encoding="utf-8")
            if kill:
                raise _Killed()
            calls["provider_body"] += 1
            raw = _raw(kwargs["request"])
            response = kwargs["job_dir"] / "verdict.raw.json"
            response.write_text(json.dumps(raw), encoding="utf-8")
            return SimpleNamespace(observed=raw, provider=pair[0], model=pair[1],
                                   prompt_path=prompt_path, response_path=response,
                                   provider_failures=[])
        monkeypatch.setattr(gemini, "_observe_entity_audio", observe)

    def run(root, *, start=1_000, model="test-agy-model"):
        output = tmp_path / root
        output.mkdir(exist_ok=True)
        verifier = _verifier(source, output)
        verifier.model = model
        check = _check_request(source, SRT)
        broad = _range_request(check)
        if start != 1_000:
            broad = {**broad, "matched_start_ms": start, "context_start_ms": start}
            broad.pop("request_sha256")
            broad["request_sha256"] = gemini._digest_json(broad)
        return gemini.observe_gemini_range_context(
            verifier=verifier, check_request=check, range_witness_request=broad,
            budget_receipt_path=receipt)

    def budget():
        return json.loads(receipt.read_text(encoding="utf-8"))["budget"]

    def claims():
        return [json.loads(path.read_text()) for path in sorted(
            (account / gemini.DISPATCH_CLAIM_DIR).glob("*.json"))]

    return SimpleNamespace(provider=provider, run=run, budget=budget, calls=calls,
                           claims=claims, source=source, receipt=receipt)


def _killed_first_root(world):
    world.provider(kill=True)
    with pytest.raises(_Killed):
        world.run("killed-root")
    rows = world.budget()["attempts"]
    assert [row["status"] for row in rows] == ["DISPATCHED"]
    return rows


def test_unretrievable_interrupted_request_is_resent_once_and_appended(tmp_path, monkeypatch):
    world = _world(tmp_path, monkeypatch)
    before = _killed_first_root(world)
    world.provider()

    result = world.run("new-root")

    assert result[0]["status"] == "OBSERVED", result[0].get("reason_code")
    assert world.calls["provider_body"] == 1
    rows = world.budget()["attempts"]
    assert rows[0] == before[0]  # the interrupted row is kept exactly as it was
    assert [row["status"] for row in rows] == ["DISPATCHED", "OBSERVED"]
    resent = world.claims()[-1]
    assert resent["attempt_id"] == 2
    assert resent["resend_basis"]["reason"] == "RECOVERABLE_INTERRUPTION"
    assert resent["resend_basis"]["prior_attempt_ids"] == [1]


def test_original_stored_result_is_read_back_and_sealed_without_dispatch(tmp_path, monkeypatch):
    world = _world(tmp_path, monkeypatch)
    real_finish = gemini._finish_attempts

    def killed_before_sealing(**kwargs):
        raise _Killed()  # acquisition stored, terminal row not yet written

    monkeypatch.setattr(gemini, "_finish_attempts", killed_before_sealing)
    world.provider()
    with pytest.raises(_Killed):
        world.run("killed-root")
    assert [row["status"] for row in world.budget()["attempts"]] == ["DISPATCHED"]
    monkeypatch.setattr(gemini, "_finish_attempts", real_finish)
    dispatched = world.calls["dispatched"]

    result = world.run("new-root")

    assert result[0]["status"] == "OBSERVED"
    assert result[0]["served_from_cache"] is True
    assert world.calls["dispatched"] == dispatched  # retrieved, not re-sent
    assert [row["status"] for row in world.budget()["attempts"]] == ["OBSERVED"]


def _three_prior(world, *, last_status):
    """Three earlier attempts on the same provider/model/window (no claims)."""
    from src.autoslice.native_audio_budget_receipt import persist_native_audio_budget_receipt
    from src.autoslice.supplement_audio_budget import get_budget
    import hashlib

    world.provider()
    world.run("seed-root", start=900)  # registers the shared account
    budget = get_budget(world.source)
    statuses = ["RESPONSE_REJECTED", "RESPONSE_REJECTED", last_status]
    for status in statuses:
        attempt = budget.consume("agy", "test-agy-model", 1_100, 3_100)
        if status != "DISPATCHED":
            budget.finish_attempt(attempt, status=status, reason_code="SYNTHETIC")
    persist_native_audio_budget_receipt(
        source_media=world.source,
        source_media_sha256=hashlib.sha256(world.source.read_bytes()).hexdigest(),
        receipt_path=world.receipt)
    return world.budget()["attempts"]


def test_soft_pressure_without_a_new_reason_is_not_sent_and_is_recorded(tmp_path, monkeypatch):
    world = _world(tmp_path, monkeypatch)
    before = _three_prior(world, last_status="RESPONSE_REJECTED")
    sent = world.calls["provider_body"]

    result = world.run("new-root")

    assert result[0]["status"] == "UNCERTAIN"
    assert result[0]["reason_code"] == gemini.SOFT_PRESSURE_NO_NEW_REASON
    assert world.calls["provider_body"] == sent
    budget = world.budget()
    assert budget["attempts"] == before
    assert budget["refusals"][-1]["reason_code"] == gemini.SOFT_PRESSURE_NO_NEW_REASON


def test_soft_pressure_with_a_recoverable_interruption_continues_and_records_it(tmp_path, monkeypatch):
    world = _world(tmp_path, monkeypatch)
    before = _three_prior(world, last_status="DISPATCHED")

    result = world.run("new-root")

    assert result[0]["status"] == "OBSERVED", result[0].get("reason_code")
    rows = world.budget()["attempts"]
    assert rows[:len(before)] == before and rows[-1]["status"] == "OBSERVED"
    basis = world.claims()[-1]["resend_basis"]
    assert basis["soft_pressure_exceeded"] is True and basis["reason"] == "RECOVERABLE_INTERRUPTION"


@pytest.mark.parametrize("change", ["window", "model"])
def test_distinct_window_or_model_is_dispatched_normally(tmp_path, monkeypatch, change):
    world = _world(tmp_path, monkeypatch)
    _killed_first_root(world)
    kwargs = {}
    if change == "window":
        kwargs["start"] = 900
        world.provider()
    else:
        kwargs["model"] = "other-agy-model"
        world.provider(pair=("agy", "other-agy-model"))

    result = world.run("new-root", **kwargs)

    assert result[0]["status"] == "OBSERVED", result[0].get("reason_code")
    assert [row["status"] for row in world.budget()["attempts"]] == ["DISPATCHED", "OBSERVED"]
    assert "reason" not in world.claims()[-1]["resend_basis"]


def test_same_root_cache_is_unchanged(tmp_path, monkeypatch):
    world = _world(tmp_path, monkeypatch)
    world.provider()
    first = world.run("root")
    second = world.run("root")

    assert first[0]["status"] == "OBSERVED" and second[0]["served_from_cache"] is True
    assert world.calls["provider_body"] == 1
    assert [row["status"] for row in world.budget()["attempts"]] == ["OBSERVED"]


def test_baseline_unretrievable_resend_appends_and_keeps_the_old_row(tmp_path, monkeypatch):
    """The sister case without any new API: the second root re-sends (bounded)
    because the first root's request cannot be retrieved; it is a new appended
    attempt and the interrupted row is unchanged."""
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source-media")
    receipt = tmp_path / "account" / "native-audio-budget.json"
    receipt.parent.mkdir()
    monkeypatch.setattr(gemini, "_extract_exact_mp3",
                        lambda src, out, start, end: out.write_bytes(f"mp3:{start}:{end}".encode()))
    monkeypatch.setattr(gemini, "_crop_black_frame_audio",
                        lambda **kw: (kw["audio_path"].write_bytes(b"black-frame-mp4") or True, ""))
    sends = []

    def observe(**kwargs):
        kwargs["on_provider_dispatch"]("agy", "test-agy-model")
        sends.append(kwargs["job_dir"].parent.parent.parent.parent.parent.name)
        raise _Killed()

    monkeypatch.setattr(gemini, "_observe_entity_audio", observe)
    check = _check_request(source, SRT)
    snapshots = []
    for root in ("killed-root", "new-root"):
        output = tmp_path / root
        output.mkdir()
        with pytest.raises(_Killed):
            gemini.observe_gemini_range_context(
                verifier=_verifier(source, output), check_request=check,
                range_witness_request=_range_request(check), budget_receipt_path=receipt)
        snapshots.append(json.loads(receipt.read_text(encoding="utf-8"))["budget"]["attempts"])

    assert sends == ["killed-root", "new-root"]
    assert snapshots[1][0] == snapshots[0][0]
    assert [row["status"] for row in snapshots[1]] == ["DISPATCHED", "DISPATCHED"]
