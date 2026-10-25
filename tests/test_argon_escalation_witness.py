"""Public-entry offline contracts, not an acoustic benchmark or model truth."""

from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
import io
import json
import os
import socket
import struct
import subprocess
import threading
import urllib.request
import wave

import pytest

from src.autoslice import argon_escalation_storage as storage
from src.autoslice.argon_escalation_contract import (
    AudioSpec,
    ContractError,
    Finding,
    HEARING_SCHEMA,
    KeyPolicy,
    MODEL,
    OrdinaryAttempt,
    Pricing,
    canonical,
    recommend_escalation,
    sha,
)
from src.autoslice.argon_escalation_witness import ArgonEscalationWitness, HttpResponse
from src.autoslice.content_bound_request_ledger import ContentBoundRequestLedger
from src.autoslice.gemini_backup_policy import CANDIDATE_BLIND_AUDIO_WITNESS_PURPOSE


@pytest.fixture(autouse=True)
def no_network_or_credentials(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Offline suite attempted real network/credential access")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    original = os._Environ.__getitem__

    def guarded(self, key):
        if key.startswith("GEMINI_"):
            forbidden()
        return original(self, key)

    monkeypatch.setattr(os._Environ, "__getitem__", guarded)


@pytest.fixture
def case(tmp_path):
    root = tmp_path.resolve()
    source = root / "synthetic.wav"
    # Nonzero, deterministic PCM provides an exact byte oracle, not words.
    pcm = b"".join(struct.pack("<h", i - 12000) for i in range(24000))
    with wave.open(str(source), "wb") as handle:
        handle.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
        handle.writeframes(pcm)
    finding = Finding(sha(source.read_bytes()), (11000, 12000), "finding-A")
    spec = AudioSpec(source, finding, (10500, 12500), source_origin_ms=10000)
    engine = ArgonEscalationWitness(root / "witness")
    handle = engine.prepare(spec)
    return engine, spec, handle, pcm


class FakeProvider:
    def __init__(self, handle):
        self.handle = handle
        self.calls = []
        self.post_error = None
        self.get_error = None
        self.sync_error = None
        self.post_status = 200
        self.state = "BATCH_STATE_SUCCEEDED"
        self.submit_terminal = False
        self.mutate = lambda op: op
        self.pages = None
        self.name = "batches/offline-001"
        self.payload = self.hearing_payload()

    def hearing_payload(self):
        b = self.handle["binding"]
        heard = {
            "schema_version": HEARING_SCHEMA,
            "binding_sha256": sha(canonical(b)),
            "target_window_ms": b["target_audio_ms"],
            "context_window_ms": b["context_audio_ms"],
            "status": "OBSERVED",
            "transcript": "synthetic test candidate, not ground truth",
            "uncertainties": [],
            "speech_role": "unknown",
            "reason": None,
        }
        return {
            "modelVersion": MODEL,
            "candidates": [
                {
                    "finishReason": "STOP",
                    "content": {"parts": [{"text": canonical(heard).decode()}]},
                }
            ],
            "usageMetadata": {
                "promptTokenCount": 100,
                "totalTokenCount": 150,
                "thoughtsTokenCount": 30,
                "candidatesTokenCount": 20,
            },
        }

    def op(self, state=None):
        state = state or self.state
        op = {
            "name": self.name,
            "metadata": {
                "name": self.name,
                "model": f"models/{MODEL}",
                "displayName": "argon-" + self.handle["task_id"],
                "state": state,
            },
            "done": state not in {"BATCH_STATE_PENDING", "BATCH_STATE_RUNNING"},
        }
        if state == "BATCH_STATE_SUCCEEDED":
            op["response"] = {
                "inlinedResponses": {
                    "inlinedResponses": [
                        {
                            "metadata": {"key": self.handle["task_id"]},
                            "response": deepcopy(self.payload),
                        }
                    ]
                }
            }
        elif op["done"]:
            op["error"] = {"code": 13, "message": "synthetic terminal error"}
        return self.mutate(op)

    def request(self, method, path, body, *, route, timeout_s):
        self.calls.append(
            {
                "method": method,
                "path": path,
                "body": body,
                "route": deepcopy(route),
                "timeout_s": timeout_s,
            }
        )
        if path.endswith(":batchGenerateContent"):
            assert method == "POST"
            if self.post_error:
                raise self.post_error
            if self.post_status != 200:
                return HttpResponse(self.post_status, b'{"error":{"code":403}}')
            obj = self.op(None if self.submit_terminal else "BATCH_STATE_PENDING")
        elif path.endswith(":generateContent"):
            assert method == "POST"
            if self.sync_error:
                raise self.sync_error
            obj = self.payload
        elif path.startswith("batches?"):
            assert method == "GET" and body is None
            obj = self.pages.pop(0) if self.pages is not None else {"operations": [self.op()]}
        else:
            assert method == "GET" and body is None and path.startswith("batches/")
            if self.get_error:
                raise self.get_error
            obj = self.op()
        # Noncanonical wire formatting checks actual raw-byte receipt hashing.
        return HttpResponse(200, json.dumps(obj, ensure_ascii=False, indent=2).encode())

    @property
    def posts(self):
        return [c for c in self.calls if c["method"] == "POST"]


def advance(case, provider, **kwargs):
    engine, spec, handle, _ = case
    return engine.advance(
        handle["task_id"],
        expected=spec,
        provider=provider,
        key_policy=kwargs.pop("key_policy", KeyPolicy(3)),
        **kwargs,
    )


def finish(case, provider, **kwargs):
    result = advance(case, provider, **kwargs)
    if result["status"] == "PENDING":
        result = advance(case, provider, **kwargs)
    return result


def test_batch_success_full_bytes_blind_clock_and_cached_resume(case):
    engine, spec, handle, pcm = case
    fake = FakeProvider(handle)
    receipt = finish(case, fake)
    assert receipt["status"] == "OBSERVED"
    assert receipt["finding_resolved"] is receipt["release_authorized"] is False
    assert receipt["requires_cpa"] and receipt["model_version"] == MODEL
    assert len(fake.posts) == 1
    full = json.loads(fake.posts[0]["body"])
    inner = full["batch"]["inputConfig"]["requests"]["requests"][0]["request"]
    parts = inner["contents"][0]["parts"]
    audio = base64.b64decode(parts[0]["inlineData"]["data"], validate=True)
    assert parts[0]["inlineData"]["mimeType"] == "audio/wav"
    assert sha(audio) == handle["binding"]["input_audio_sha256"]
    assert len(audio) == handle["binding"]["input_audio_bytes"]
    with wave.open(io.BytesIO(audio), "rb") as reader:
        assert reader.getnframes() == 16000
        assert reader.readframes(16000) == pcm[4000 * 2 : 20000 * 2]
    assert handle["binding"]["target_audio_ms"] == [500, 1500]
    assert handle["binding"]["clock_mapping"]["source_ms_equals_audio_ms_plus"] == 10500
    assert "CURRENT" not in parts[1]["text"] and "PROPOSED" not in parts[1]["text"]
    from src.autoslice.argon_escalation_contract import structured_argon_request
    assert receipt['request_sha256'] == handle['batch_request_sha256']
    assert fake.posts[0]['body'] == structured_argon_request(engine._body(handle['task_id'],
        engine._load(handle['task_id'], spec)[2], 'batch'))
    assert any(json.loads(path.read_text()).get('wire_request_sha256') == sha(fake.posts[0]['body'])
        for path in (engine.root / handle['task_id'] / 'calls').glob('*.start.json'))
    assert receipt["usage_receipt"]["estimated_usd"] is None
    assert receipt["usage_receipt"]["cost_status"] == "UNKNOWN"
    assert receipt["accounting"]["inference_reservations"] == 1
    assert receipt["accounting"]["http_attempts"] == 2
    calls = len(fake.calls)
    restored = advance(case, fake)
    assert restored["result_sha256"] == receipt["result_sha256"]
    assert len(fake.calls) == calls
    engine.validate_evidence(handle["task_id"], receipt, expected=spec)
    assert engine.prepare(spec)["task_id"] == handle["task_id"]


def test_finding_text_never_leaks_into_blind_prompt(case):
    engine, spec, _, _ = case
    poison = "CURRENT=old PROPOSED=new reference lyrics=secret phrase"
    spec = replace(spec, finding=replace(spec.finding, finding_id=poison))
    handle = engine.prepare(spec)
    fake = FakeProvider(handle)
    engine.advance(handle["task_id"], expected=spec, provider=fake, key_policy=KeyPolicy(1))
    wire = fake.posts[0]["body"].decode()
    assert poison not in wire and "secret phrase" not in wire and "CURRENT" not in wire


def test_repeated_uncertain_same_source_window_finding_only(case):
    _, spec, _, _ = case
    f = spec.finding
    one = OrdinaryAttempt(f, "a", "UNCERTAIN", "a" * 64)
    two = OrdinaryAttempt(f, "b", "NEITHER", "b" * 64)
    others = [
        replace(two, finding=replace(f, finding_id="other")),
        replace(two, finding=replace(f, source_media_sha256="c" * 64)),
        replace(two, finding=replace(f, target_source_ms=(11001, 12000))),
    ]
    assert not recommend_escalation(f, [one, one, *others])["recommend_upgrade"]
    result = recommend_escalation(f, [one, two, two, *others])
    assert result["matched_unresolved_count"] == 2 and result["excluded_other_findings"] == 3
    assert result["recommend_upgrade"] and result["count_is_truth"] is False
    assert recommend_escalation(f, reading_chat=True)["reasons"] == ["READING_CHAT"]
    assert recommend_escalation(f, lyrics_reference_conflict=True)["recommend_upgrade"]
    with pytest.raises(ContractError, match="ATTEMPT_IDENTITY_CONFLICT"):
        recommend_escalation(f, [one, replace(one, receipt_sha256="d" * 64)])


def test_same_receipt_does_not_count_as_two_attempts(case):
    f = case[1].finding
    result = recommend_escalation(
        f,
        [
            OrdinaryAttempt(f, "a", "UNCERTAIN", "a" * 64),
            OrdinaryAttempt(f, "b", "NEITHER", "a" * 64),
        ],
    )
    assert not result["recommend_upgrade"]


def test_unknown_submit_recovers_remote_once_without_resubmission(case):
    fake = FakeProvider(case[2])
    fake.post_error = TimeoutError("not persisted")
    first = advance(case, fake)
    assert first["status"] == "SUBMIT_UNKNOWN" and first["job_name"] is None
    recovered = advance(case, fake)
    assert recovered["status"] == "OBSERVED" and len(fake.posts) == 1
    assert any(c["path"].startswith("batches?") for c in fake.calls)
    assert recovered["job_name"] == fake.name
    assert recovered["accounting"]["inference_reservations"] == 1


@pytest.mark.parametrize(
    "scenario,reason",
    [
        ("none", "RECOVERY_NO_MATCH"),
        ("duplicate", "RECOVERY_MULTIPLE_MATCHES"),
        ("pagination", "RECOVERY_PAGINATION_INCOMPLETE"),
        ("unreachable", "RECOVERY_LIST_INVALID"),
    ],
)
def test_unknown_recovery_cannot_guess_or_resend(case, scenario, reason):
    fake = FakeProvider(case[2])
    fake.post_error = TimeoutError()
    advance(case, fake)
    if scenario == "none":
        fake.pages = [{"operations": []}]
    elif scenario == "duplicate":
        other = fake.op()
        other["name"] = other["metadata"]["name"] = "batches/other"
        fake.pages = [{"operations": [fake.op(), other]}]
    elif scenario == "pagination":
        fake.pages = [{"operations": [fake.op()], "nextPageToken": "another"}]
    else:
        fake.pages = [{"operations": [fake.op()], "unreachable": ["unknown-scope"]}]
    result = advance(case, fake, allow_realtime_fallback=True, max_recovery_pages=1)
    assert result["status"] == "SUBMIT_UNKNOWN" and result["reason"] == reason
    assert len(fake.posts) == 1 and result["usage_receipt"]["estimated_usd"] is None


def test_recovery_get_cannot_switch_to_another_remote_job(case):
    fake = FakeProvider(case[2])
    fake.post_error = TimeoutError()
    advance(case, fake)
    fake.pages = [{"operations": [fake.op()]}]
    fake.name = "batches/different-get-identity"
    result = advance(case, fake)
    assert result["status"] == "READBACK_REJECTED"
    assert result["reason"] == "REMOTE_JOB_IDENTITY_MISMATCH"
    assert len(fake.posts) == 1


def test_timeout_retains_job_and_no_realtime_until_terminal_and_opt_in(case):
    fake = FakeProvider(case[2])
    fake.state = "BATCH_STATE_RUNNING"
    result = advance(case, fake, max_batch_wait_s=1, now=lambda: 100)
    assert result["status"] == "PENDING"
    result = advance(
        case, fake, max_batch_wait_s=99999, now=lambda: 102, allow_realtime_fallback=True
    )
    assert result["status"] == "WAIT_TIMEOUT" and result["job_name"] == fake.name
    assert len(fake.posts) == 1  # Restart cannot extend the original wait deadline.
    fake.state = "BATCH_STATE_EXPIRED"
    assert advance(case, fake)["status"] == "FAILED"
    assert advance(case, fake)["mode"] == "batch"  # No implicit fallback.
    result = advance(case, fake, allow_realtime_fallback=True)
    assert result["mode"] == "realtime" and result["status"] == "OBSERVED"
    assert len(fake.posts) == 2 and result["accounting"]["inference_reservations"] == 2
    assert result["accounting"]["histories"][0]["job_name"] == fake.name
    assert result["accounting"]["actual_charge_usd"] is None
    assert fake.posts[1]["body"] == canonical(
        json.loads(fake.posts[0]["body"])["batch"]["inputConfig"]["requests"]["requests"][0][
            "request"
        ]
    )


def test_realtime_unknown_never_reposts(case):
    fake = FakeProvider(case[2])
    fake.state = "BATCH_STATE_FAILED"
    finish(case, fake)
    fake.sync_error = TimeoutError()
    assert advance(case, fake, allow_realtime_fallback=True)["status"] == "SUBMIT_UNKNOWN"
    result = advance(case, fake, allow_realtime_fallback=True)
    assert result["reason"] == "REALTIME_OUTCOME_UNKNOWN" and len(fake.posts) == 2


@pytest.mark.parametrize("status", [400, 401, 403, 429])
def test_http_rejection_never_falls_back_or_retries(case, status):
    fake = FakeProvider(case[2])
    fake.post_status = status
    assert advance(case, fake)["status"] == "FAILED"
    assert advance(case, fake, allow_realtime_fallback=True)["mode"] == "batch"
    assert len(fake.posts) == 1


@pytest.mark.parametrize("status", [202, 302, 408, 409, 500, 503])
def test_unexpected_submit_status_is_unknown_not_confirmed_failure(case, status):
    fake = FakeProvider(case[2])
    fake.post_status = status
    assert advance(case, fake)["status"] == "SUBMIT_UNKNOWN"
    assert len(fake.posts) == 1


def test_get_failure_retains_job_and_can_resume(case):
    fake = FakeProvider(case[2])
    advance(case, fake)
    fake.get_error = TimeoutError()
    result = advance(case, fake)
    assert result["status"] == "READBACK_UNKNOWN" and result["job_name"] == fake.name
    fake.get_error = None
    assert advance(case, fake)["status"] == "OBSERVED" and len(fake.posts) == 1


@pytest.mark.parametrize(
    "mutation,reason",
    [
        ("missing_model", "RETURNED_MODEL_IDENTITY_MISMATCH_OR_MISSING"),
        ("wrong_model", "RETURNED_MODEL_IDENTITY_MISMATCH_OR_MISSING"),
        ("nonstring_model", "RETURNED_MODEL_IDENTITY_MISMATCH_OR_MISSING"),
        ("truncated", "RESPONSE_NOT_COMPLETE"),
        ("candidates", "RESPONSE_CANDIDATES_INCOMPLETE"),
        ("empty_text", "INVALID_JSON"),
        ("bad_clock", "HEARING_CLOCK_MISMATCH"),
        ("bad_hash", "HEARING_BINDING_MISMATCH"),
        ("extra_answer", "HEARING_SCHEMA_INVALID"),
        ("float_clock", "CLOCK_WINDOW_INVALID"),
        ("duplicate_json", "INVALID_JSON"),
    ],
)
def test_bad_model_response_rejected_and_failure_survives_resume(case, mutation, reason):
    fake = FakeProvider(case[2])
    payload = fake.payload
    if mutation == "missing_model":
        payload.pop("modelVersion")
    elif mutation == "wrong_model":
        payload["modelVersion"] = "gemini-3.8-flash"
    elif mutation == "nonstring_model":
        payload["modelVersion"] = 123
    elif mutation == "truncated":
        payload["candidates"][0]["finishReason"] = "MAX_TOKENS"
    elif mutation == "candidates":
        payload["candidates"] = []
    else:
        part = payload["candidates"][0]["content"]["parts"][0]
        heard = json.loads(part["text"])
        if mutation == "empty_text":
            part["text"] = ""
        elif mutation == "duplicate_json":
            part["text"] = '{"status":"OBSERVED","status":"ERROR"}'
        else:
            if mutation == "bad_clock":
                heard["target_window_ms"][0] += 1
            elif mutation == "bad_hash":
                heard["binding_sha256"] = "f" * 64
            elif mutation == "extra_answer":
                heard["current"] = "do not accept"
            else:
                heard["target_window_ms"][0] = float(heard["target_window_ms"][0])
            part["text"] = canonical(heard).decode()
    receipt = finish(case, fake)
    assert receipt["status"] == "RESPONSE_REJECTED" and receipt["reason"] == reason
    assert receipt["response_sha256"] and receipt["hearing"] is None
    resumed = advance(case, fake, allow_realtime_fallback=True)
    assert resumed["status"] == "RESPONSE_REJECTED" and len(fake.posts) == 1


@pytest.mark.parametrize("status", ["UNCERTAIN", "NO_SPEECH", "ERROR"])
def test_unknown_no_speech_and_media_errors_are_not_truth_or_release(case, status):
    fake = FakeProvider(case[2])
    part = fake.payload["candidates"][0]["content"]["parts"][0]
    heard = json.loads(part["text"])
    heard.update(status=status, transcript=None, reason="synthetic unresolved input")
    if status == "UNCERTAIN":
        heard["uncertainties"] = [{"start_ms": 500, "end_ms": 1500, "reason": "unclear"}]
    part["text"] = canonical(heard).decode()
    receipt = finish(case, fake)
    assert receipt["status"] == status and receipt["hearing"]["reason"]
    assert not receipt["finding_resolved"] and not receipt["release_authorized"]
    assert advance(case, fake)["status"] == status


@pytest.mark.parametrize(
    "mutation", ["key", "missing_rows", "duplicate_rows", "item_error", "not_done"]
)
def test_native_batch_envelope_must_be_complete_and_key_bound(case, mutation):
    fake = FakeProvider(case[2])
    advance(case, fake)

    def mutate(op):
        if mutation == "not_done":
            op["done"] = False
        elif mutation == "missing_rows":
            op["response"] = {}
        else:
            rows = op["response"]["inlinedResponses"]["inlinedResponses"]
            if mutation == "key":
                rows[0]["metadata"]["key"] = "wrong"
            elif mutation == "duplicate_rows":
                rows.append(deepcopy(rows[0]))
            else:
                rows[0] = {"metadata": {"key": case[2]["task_id"]}, "error": {"code": 13}}
        return op

    fake.mutate = mutate
    receipt = advance(case, fake)
    assert receipt["status"] == "RESPONSE_REJECTED"
    assert advance(case, fake)["status"] == "RESPONSE_REJECTED"
    assert len(fake.posts) == 1


@pytest.mark.parametrize(
    "field", ["source", "target", "finding", "context", "origin", "float_origin"]
)
def test_resume_rejects_wrong_expected_binding_before_provider(case, field):
    engine, spec, handle, _ = case
    fake = FakeProvider(handle)
    if field == "source":
        other = replace(spec, finding=replace(spec.finding, source_media_sha256="f" * 64))
    elif field == "target":
        other = replace(spec, finding=replace(spec.finding, target_source_ms=(11001, 12000)))
    elif field == "finding":
        other = replace(spec, finding=replace(spec.finding, finding_id="other"))
    elif field == "context":
        other = replace(spec, context_source_ms=(10499, 12500))
    elif field == "origin":
        other = replace(spec, source_origin_ms=9999)
    else:
        other = replace(spec, source_origin_ms=10000.0)
    with pytest.raises(ContractError):
        engine.advance(handle["task_id"], expected=other, provider=fake, key_policy=KeyPolicy(1))
    assert not fake.calls


@pytest.mark.parametrize("what", ["source", "audio", "request", "manifest"])
def test_corrupt_bytes_rejected_before_provider(case, what):
    engine, spec, handle, _ = case
    task = engine.root / handle["task_id"]
    if what == "source":
        path = spec.source_path
    elif what == "audio":
        path = task / "blobs" / (handle["binding"]["input_audio_sha256"] + ".bin")
    elif what == "request":
        path = task / "blobs" / (handle["inner_request_sha256"] + ".bin")
    else:
        path = task / "manifest.json"
    path.write_bytes(path.read_bytes() + b" ")
    fake = FakeProvider(handle)
    with pytest.raises(ContractError):
        advance(case, fake)
    assert not fake.calls


@pytest.mark.parametrize(
    "field",
    [
        "model_version",
        "request_sha256",
        "response_sha256",
        "hearing",
        "release_authorized",
        "usage_receipt",
    ],
)
def test_consumer_replays_raw_not_just_a_success_flag(case, field):
    engine, spec, handle, _ = case
    receipt = finish(case, FakeProvider(handle))
    bad = deepcopy(receipt)
    if field == "hearing":
        bad[field]["transcript"] = "changed after provider"
    elif field == "release_authorized":
        bad[field] = True
    elif field == "usage_receipt":
        bad[field]["estimated_usd"] = 0
    elif field == "response_sha256":
        bad[field] = handle["inner_request_sha256"]
    else:
        bad[field] = "f" * 64
    with pytest.raises(ContractError):
        engine.validate_evidence(handle["task_id"], bad, expected=spec)


def test_no_context_or_wrong_source_prepare_is_rejected(case):
    engine, spec, _, _ = case
    with pytest.raises(ContractError, match="AUDIO_CONTEXT_MISSING"):
        engine.prepare(replace(spec, context_source_ms=spec.finding.target_source_ms))
    with pytest.raises(ContractError, match="SOURCE_SHA_MISMATCH"):
        engine.prepare(replace(spec, finding=replace(spec.finding, source_media_sha256="f" * 64)))


def test_content_dedupe_does_not_depend_on_local_source_filename(case):
    engine, spec, handle, _ = case
    copy = spec.source_path.with_name("same-bytes-renamed.wav")
    copy.write_bytes(spec.source_path.read_bytes())
    assert engine.prepare(replace(spec, source_path=copy))["task_id"] == handle["task_id"]


def test_secret_free_free_first_and_price_unknown_or_explicit_estimate(case):
    fake = FakeProvider(case[2])
    pricing = Pricing(MODEL, 2, 10, 0.5, "synthetic-test-rates-not-current-market-prices")
    result = finish(case, fake, key_policy=KeyPolicy(3, (1,)), pricing=pricing)
    assert fake.posts[0]["route"]["accepted_key_ordinal"] == 2
    assert fake.posts[0]["route"]["accepted_key_tier"] == "free"
    assert result["usage_receipt"]["estimated_usd"] == pytest.approx(0.00035)
    assert result["usage_receipt"]["usage"]["thoughtsTokenCount"] == 30
    assert result["usage_receipt"]["actual_charge_usd"] is None


def paid_policy(task_id, *, strikes=3, calls_before=0):
    stamp = {
        "key_tier": "paid_backup",
        "mode": "strict",
        "item_key": task_id,
        "free_chain_strikes": strikes,
        "calls_today_before": calls_before,
        "daily_cap": 10,
        "recorded_at": "2026-10-16T12:00:00+00:00",
        "purpose": CANDIDATE_BLIND_AUDIO_WITNESS_PURPOSE,
    }
    return KeyPolicy(3, (1, 2, 3), True, stamp)


def test_paid_route_requires_exact_shared_policy_and_is_accounted(case):
    fake = FakeProvider(case[2])
    with pytest.raises(ContractError, match="PAID_POLICY_REQUIRED"):
        advance(case, fake, key_policy=paid_policy(case[2]["task_id"], strikes=2))
    assert not fake.calls
    result = finish(case, fake, key_policy=paid_policy(case[2]["task_id"]))
    assert result["status"] == "OBSERVED"
    route = fake.posts[0]["route"]
    assert route["accepted_key_ordinal"] == 4 and route["accepted_key_tier"] == "paid_backup"
    assert result["accounting"]["histories"][0]["events"][1]["metadata"]["route"] == route
    assert "GEMINI_KEY_BACKUP" not in canonical(result).decode()


def test_existing_paid_stamp_cannot_be_reused_for_second_physical_call(case):
    fake = FakeProvider(case[2])
    fake.state = "BATCH_STATE_EXPIRED"
    policy = paid_policy(case[2]["task_id"])
    finish(case, fake, key_policy=policy)
    with pytest.raises(ContractError, match="PAID_STAMP_ALREADY_RESERVED"):
        advance(case, fake, key_policy=policy, allow_realtime_fallback=True)
    assert len(fake.posts) == 1
    assert (
        advance(
            case,
            fake,
            key_policy=paid_policy(case[2]["task_id"], calls_before=1),
            allow_realtime_fallback=True,
        )["status"]
        == "OBSERVED"
    )


@pytest.mark.parametrize(
    "usage",
    [
        None,
        {},
        {"promptTokenCount": 100},
        {"promptTokenCount": True, "totalTokenCount": 100},
        {"promptTokenCount": 100, "totalTokenCount": 50},
    ],
)
def test_missing_or_invalid_usage_is_unknown_not_zero(case, usage):
    fake = FakeProvider(case[2])
    fake.payload["usageMetadata"] = usage
    result = finish(case, fake, pricing=Pricing(MODEL, 2, 10, 0.5, "test-only"))
    assert result["usage_receipt"]["estimated_usd"] is None
    assert result["usage_receipt"]["cost_status"] == "UNKNOWN"


def test_crash_during_submit_recovers_without_repost(case):
    fake = FakeProvider(case[2])
    fake.post_error = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        advance(case, fake)
    result = advance(case, fake)
    assert result["status"] == "OBSERVED" and len(fake.posts) == 1
    assert result["accounting"]["http_attempts"] == 3


@pytest.mark.parametrize("crash_state", ["PROCESSING", "COMPLETED"])
def test_crash_after_response_persisted_resumes_locally(case, monkeypatch, crash_state):
    fake = FakeProvider(case[2])
    original = ContentBoundRequestLedger.transition
    crashed = False

    def transition(self, state, **kwargs):
        nonlocal crashed
        if state == crash_state and not crashed:
            crashed = True
            raise KeyboardInterrupt()
        return original(self, state, **kwargs)

    monkeypatch.setattr(ContentBoundRequestLedger, "transition", transition)
    with pytest.raises(KeyboardInterrupt):
        finish(case, fake)
    calls = len(fake.calls)
    result = advance(case, fake)
    assert len(fake.calls) == calls
    if result["status"] == "PENDING":
        result = advance(case, fake)
    assert result["status"] == "OBSERVED" and len(fake.posts) == 1


def test_two_writers_cannot_dispatch_the_same_request(case):
    engine, spec, handle, _ = case
    fake = FakeProvider(handle)
    entered, release = threading.Event(), threading.Event()
    original = fake.request

    def request(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    fake.request = request
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(advance, case, fake)
        assert entered.wait(5)
        try:
            other = ArgonEscalationWitness(engine.root)
            with pytest.raises(BlockingIOError):
                other.advance(
                    handle["task_id"], expected=spec, provider=fake, key_policy=KeyPolicy(1)
                )
        finally:
            release.set()
        assert first.result()["status"] == "PENDING"
    assert len(fake.posts) == 1


def test_finite_polling_and_no_implicit_policy(case):
    engine, spec, handle, _ = case
    fake = FakeProvider(handle)
    result = engine.advance(handle["task_id"], expected=spec, provider=fake)
    assert result["status"] == "NOT_DISPATCHED" and not fake.calls
    sleeps = []
    result = engine.poll(
        handle["task_id"],
        expected=spec,
        provider=fake,
        key_policy=KeyPolicy(1),
        max_polls=2,
        interval_s=0,
        sleep=sleeps.append,
    )
    assert result["status"] == "OBSERVED" and sleeps == [0]


def test_real_local_container_decode_no_provider_experiment(case):
    engine, spec, _, _ = case
    # Synthetic PCM -> local lossless container -> real FFmpeg extraction. No API.
    media = spec.source_path.with_suffix(".mka")
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-nostdin",
            "-i",
            str(spec.source_path),
            "-c:a",
            "flac",
            str(media),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    adapted = replace(
        spec,
        source_path=media,
        finding=replace(spec.finding, source_media_sha256=sha(media.read_bytes())),
    )
    handle = engine.prepare(adapted)
    fake = FakeProvider(handle)
    result = engine.poll(
        handle["task_id"],
        expected=adapted,
        provider=fake,
        key_policy=KeyPolicy(1),
        max_polls=2,
        interval_s=0,
    )
    assert result["status"] == "OBSERVED"
    assert handle["binding"]["extraction"] == "ffmpeg-contiguous-pcm16-stereo-16k.v1"
    assert handle["binding"]["sample_count"] == 32000


def test_recovered_job_survives_failure_of_its_first_get(case):
    fake = FakeProvider(case[2])
    fake.post_error = TimeoutError()
    fake.get_error = TimeoutError()
    advance(case, fake)
    result = advance(case, fake)
    assert result["status"] == "READBACK_UNKNOWN" and result["job_name"] == fake.name
    before = sum(c["path"].startswith("batches?") for c in fake.calls)
    fake.get_error = None
    assert advance(case, fake)["status"] == "OBSERVED"
    assert sum(c["path"].startswith("batches?") for c in fake.calls) == before
    assert len(fake.posts) == 1


def test_malformed_native_state_retained_as_rejected_readback(case):
    fake = FakeProvider(case[2])
    advance(case, fake)

    def mutate(op):
        op["metadata"]["state"] = {"unexpected": "object"}
        return op

    fake.mutate = mutate
    result = advance(case, fake)
    assert result["status"] == "RESPONSE_REJECTED" and result["reason"] == "REMOTE_STATE_INVALID"
    assert result["response_sha256"] and result["job_name"] == fake.name


@pytest.mark.parametrize("shape", ["list", "metadata_output"])
def test_native_inline_variants_from_research_protocol(case, shape):
    fake = FakeProvider(case[2])
    advance(case, fake)

    def mutate(op):
        if shape == "list":
            op["response"]["inlinedResponses"] = op["response"]["inlinedResponses"][
                "inlinedResponses"
            ]
        else:
            op["metadata"]["output"] = op.pop("response")
        return op

    fake.mutate = mutate
    assert advance(case, fake)["status"] == "OBSERVED"


def test_source_outside_declared_clock_is_typed_rejection(case):
    engine, spec, _, _ = case
    other = replace(
        spec,
        context_source_ms=(18500, 20500),
        finding=replace(spec.finding, target_source_ms=(19000, 20000)),
    )
    with pytest.raises(ContractError, match="CONTEXT_OUTSIDE_SOURCE"):
        engine.prepare(other)


def test_truncated_audio_bytes_never_padded_to_expected_duration(case):
    engine, spec, _, _ = case
    spec.source_path.write_bytes(spec.source_path.read_bytes()[:-16000])
    changed = replace(
        spec, finding=replace(spec.finding, source_media_sha256=sha(spec.source_path.read_bytes()))
    )
    with pytest.raises(ContractError, match="AUDIO_DECODE_INCOMPLETE"):
        engine.prepare(changed)


@pytest.mark.parametrize(
    "field,value",
    [("thoughtsTokenCount", -1), ("candidatesTokenCount", 1000), ("thoughtsTokenCount", "30")],
)
def test_inconsistent_native_token_details_do_not_create_a_known_cost(case, field, value):
    fake = FakeProvider(case[2])
    fake.payload["usageMetadata"][field] = value
    receipt = finish(case, fake, pricing=Pricing(MODEL, 2, 10, 0.5, "test-only"))
    assert receipt["status"] == "OBSERVED"
    assert receipt["usage_receipt"]["estimated_usd"] is None
    assert receipt["usage_receipt"]["usage_status"] == "UNKNOWN"


def test_crash_between_raw_blob_and_outcome_does_not_resubmit(case, monkeypatch):
    fake = FakeProvider(case[2])
    original = storage.put_json
    crashed = False

    def put(path, value):
        nonlocal crashed
        if path.name.endswith(".outcome.json") and not crashed:
            crashed = True
            raise KeyboardInterrupt()
        original(path, value)

    monkeypatch.setattr(storage, "put_json", put)
    with pytest.raises(KeyboardInterrupt):
        advance(case, fake)
    assert advance(case, fake)["status"] == "OBSERVED"
    assert len(fake.posts) == 1


def test_exact_digital_silence_cannot_be_accepted_as_spoken_words(case):
    engine, spec, _, _ = case
    with wave.open(str(spec.source_path), "wb") as handle:
        handle.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
        handle.writeframes(b"\x00" * 48000)
    silent = replace(
        spec, finding=replace(spec.finding, source_media_sha256=sha(spec.source_path.read_bytes()))
    )
    handle = engine.prepare(silent)
    assert handle["binding"]["target_pcm_is_digital_silence"]
    fake = FakeProvider(handle)
    receipt = engine.poll(
        handle["task_id"],
        expected=silent,
        provider=fake,
        key_policy=KeyPolicy(1),
        max_polls=2,
        interval_s=0,
    )
    assert receipt["status"] == "RESPONSE_REJECTED"
    assert receipt["reason"] == "DIGITAL_SILENCE_CONTRADICTS_TRANSCRIPT"
    assert receipt["hearing"] is None and receipt["response_sha256"]
    assert (
        engine.advance(handle["task_id"], expected=silent, provider=fake)["status"]
        == "RESPONSE_REJECTED"
    )


def test_same_receipt_cannot_hide_conflicting_attempt_identity(case):
    f = case[1].finding
    with pytest.raises(ContractError, match="ATTEMPT_IDENTITY_CONFLICT"):
        recommend_escalation(
            f,
            [
                OrdinaryAttempt(f, "a", "UNCERTAIN", "a" * 64),
                OrdinaryAttempt(f, "b", "UNCERTAIN", "a" * 64),
                OrdinaryAttempt(f, "b", "NEITHER", "b" * 64),
            ],
        )


def test_authorized_realtime_unknown_remains_visible_with_default_resume_policy(case):
    fake = FakeProvider(case[2])
    fake.state = "BATCH_STATE_EXPIRED"
    finish(case, fake)
    fake.sync_error = TimeoutError()
    advance(case, fake, allow_realtime_fallback=True)
    result = advance(case, fake)
    assert result["mode"] == "realtime" and result["status"] == "SUBMIT_UNKNOWN"
    assert len(fake.posts) == 2
    proof = result["accounting"]["histories"][1]["events"][1]["metadata"]["fallback_policy"]
    assert proof["explicit_realtime_permission"] is True
    assert proof["batch_job_name"] == fake.name and proof["batch_terminal_response_sha256"]


def test_batch_permission_denial_is_not_a_realtime_fallback_permission(case):
    fake = FakeProvider(case[2])
    fake.state = "BATCH_STATE_FAILED"

    def mutate(op):
        if "error" in op:
            op["error"]["code"] = 7
        return op

    fake.mutate = mutate
    finish(case, fake)
    result = advance(case, fake, allow_realtime_fallback=True)
    assert result["mode"] == "batch" and result["status"] == "FAILED" and len(fake.posts) == 1


@pytest.mark.parametrize("shape", ["multiple_audio_streams", "surround_downmix"])
def test_container_audio_must_not_silently_drop_tracks_or_channels(case, shape):
    engine, spec, _, _ = case
    media = spec.source_path.with_name(shape + ".mka")
    command = ["ffmpeg", "-v", "error", "-nostdin", "-i", str(spec.source_path)]
    command += (
        ["-map", "0:a:0", "-map", "0:a:0"] if shape == "multiple_audio_streams" else ["-ac", "6"]
    )
    subprocess.run(
        command + ["-c:a", "flac", str(media)], check=True, capture_output=True, timeout=30
    )
    adapted = replace(
        spec,
        source_path=media,
        finding=replace(spec.finding, source_media_sha256=sha(media.read_bytes())),
    )
    with pytest.raises(ContractError, match="SOURCE_AUDIO_LAYOUT_UNSUPPORTED"):
        engine.prepare(adapted)


@pytest.mark.parametrize("location", ["response", "metadata.output"])
@pytest.mark.parametrize("entry", ["poll", "submit", "unknown_recovery"])
@pytest.mark.parametrize("conflict", ["operation_error", "failed_output", "running_output"])
def test_operation_contradiction_public_lifecycle(case, location, entry, conflict):
    """A conflicting native result is never hearing or permission to send again."""
    engine, spec, handle, _ = case
    fake = FakeProvider(handle)
    if entry == "unknown_recovery":
        fake.post_error = TimeoutError()
        assert advance(case, fake)["status"] == "SUBMIT_UNKNOWN"
    elif entry == "poll":
        assert advance(case, fake)["status"] == "PENDING"
    else:
        fake.submit_terminal = True

    def mutate(op):
        if location == "metadata.output":
            op["metadata"]["output"] = op.pop("response")
        if conflict == "operation_error":
            op["error"] = {"code": 7, "message": "synthetic operation error"}
        elif conflict == "failed_output":
            op["metadata"]["state"] = "BATCH_STATE_FAILED"
        else:
            op["metadata"]["state"] = "BATCH_STATE_RUNNING"
            op["done"] = False
        return op

    fake.mutate = mutate
    result = advance(case, fake)
    assert result["status"] == "RESPONSE_REJECTED"
    assert result["reason"] and result["hearing"] is None
    assert result["job_name"] == fake.name
    assert storage.get_blob(engine.root / handle["task_id"], result["response_sha256"])
    count = len(fake.calls)
    resumed = advance(case, fake, allow_realtime_fallback=True)
    assert resumed["status"] == "RESPONSE_REJECTED" and resumed["mode"] == "batch"
    assert len(fake.posts) == 1 and len(fake.calls) == count
    consumed = engine.validate_evidence(handle["task_id"], result, expected=spec)
    assert consumed["status"] == "RESPONSE_REJECTED" and not consumed["finding_resolved"]


@pytest.mark.parametrize("variant", ["success_error_no_output", "pending_done", "invalid_error"])
def test_operation_state_error_cannot_grant_realtime_fallback(case, variant):
    fake = FakeProvider(case[2])
    advance(case, fake)

    def mutate(op):
        op.pop("response")
        if variant == "success_error_no_output":
            op["error"] = {"code": 13, "message": "synthetic internal error"}
        elif variant == "pending_done":
            op["metadata"]["state"] = "BATCH_STATE_PENDING"
        else:
            op["metadata"]["state"] = "BATCH_STATE_EXPIRED"
            op["error"] = None
        return op

    fake.mutate = mutate
    result = advance(case, fake)
    assert result["status"] == "RESPONSE_REJECTED"
    assert advance(case, fake, allow_realtime_fallback=True)["status"] == "RESPONSE_REJECTED"
    assert len(fake.posts) == 1


@pytest.mark.parametrize("location", ["response", "metadata.output", "both_identical"])
def test_valid_native_output_remains_supported_through_consumer(case, location):
    engine, spec, handle, _ = case
    fake = FakeProvider(handle)
    advance(case, fake)

    def mutate(op):
        if location == "metadata.output":
            op["metadata"]["output"] = op.pop("response")
        elif location == "both_identical":
            op["metadata"]["output"] = deepcopy(op["response"])
        return op

    fake.mutate = mutate
    result = advance(case, fake)
    assert result["status"] == "OBSERVED" and result["reason"] is None
    assert (
        engine.validate_evidence(handle["task_id"], result, expected=spec)["status"] == "OBSERVED"
    )
    assert advance(case, fake)["result_sha256"] == result["result_sha256"]
    assert len(fake.posts) == 1


@pytest.mark.parametrize("field,value", [("state", {}), ("done", "true")])
def test_invalid_native_status_still_retains_valid_job_identity(case, field, value):
    fake = FakeProvider(case[2])
    fake.submit_terminal = True

    def mutate(op):
        if field == "state":
            op["metadata"][field] = value
        else:
            op[field] = value
        return op

    fake.mutate = mutate
    result = advance(case, fake)
    assert result["status"] == "RESPONSE_REJECTED" and result["job_name"] == fake.name
    assert result["response_sha256"] and result["hearing"] is None
    assert advance(case, fake, allow_realtime_fallback=True)["status"] == "RESPONSE_REJECTED"
    assert len(fake.posts) == 1


@pytest.mark.parametrize("location", ["response", "metadata.output"])
@pytest.mark.parametrize("conflict", ["unknown_state", "inline_and_file"])
def test_additional_native_output_conflicts_through_public_consumer(case, location, conflict):
    engine, spec, handle, _ = case
    fake = FakeProvider(handle)
    advance(case, fake)

    def mutate(op):
        output = op["response"]
        if location == "metadata.output":
            op["metadata"]["output"] = op.pop("response")
        if conflict == "unknown_state":
            op["metadata"]["state"] = "BATCH_STATE_NEW_UNKNOWN"
        else:
            output["responsesFile"] = "files/unrequested-output"
        return op

    fake.mutate = mutate
    result = advance(case, fake)
    assert result["status"] == "RESPONSE_REJECTED" and len(fake.posts) == 1
    assert (
        engine.validate_evidence(handle["task_id"], result, expected=spec)["status"]
        == "RESPONSE_REJECTED"
    )


@pytest.mark.parametrize("inline_location", ["response", "metadata.output"])
def test_conflicting_output_kinds_across_native_locations_are_rejected(case, inline_location):
    engine, spec, handle, _ = case
    fake = FakeProvider(handle)
    advance(case, fake)

    def mutate(op):
        if inline_location == "metadata.output":
            op["metadata"]["output"] = op.pop("response")
            op["response"] = {"responsesFile": "files/not-the-inline-result"}
        else:
            op["metadata"]["output"] = {"responsesFile": "files/not-the-inline-result"}
        return op

    fake.mutate = mutate
    result = advance(case, fake)
    assert result["status"] == "RESPONSE_REJECTED"
    assert (
        engine.validate_evidence(handle["task_id"], result, expected=spec)["status"]
        == "RESPONSE_REJECTED"
    )
    assert advance(case, fake, allow_realtime_fallback=True)["status"] == "RESPONSE_REJECTED"
    assert len(fake.posts) == 1
