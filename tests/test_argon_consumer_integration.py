"""Public producer/ordinary adjudication with synthetic raw audio and providers."""

from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import socket
import struct
from types import SimpleNamespace
import wave

import pytest

from src.autoslice import argon_escalation_runtime as runtime
from src.autoslice.argon_escalation_contract import canonical, sha, strict_json
from src.autoslice.argon_escalation_witness import HttpResponse
from src.autoslice.final_review_auditor import adjudicate_context_finding
from src.autoslice.final_review_provider_budget import ContextAdjudicationBudget
from src.autoslice.semantic_retry_progress import SemanticAttemptRecorder
from src.autoslice.unresolved_findings_gate import unresolved_findings_problem


@pytest.fixture(autouse=True)
def offline(monkeypatch, request):
    violations = []

    def no_network(*a, **k):
        violations.append("external network")
        raise AssertionError("external network forbidden")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket.socket, "connect_ex", no_network)
    monkeypatch.setattr(socket, "getaddrinfo", no_network)
    original = Path.read_text

    def read(path, *a, **k):
        if str(path) in {"/opt/bilive/.env", str(Path.home() / "Project" / "vtuber-slice" / ".env")}:
            violations.append("real env")
            raise AssertionError("real .env forbidden")
        return original(path, *a, **k)

    monkeypatch.setattr(Path, "read_text", read)
    yield
    request.node.user_properties.append(("external_network_or_real_env_attempts", len(violations)))
    assert not violations


class NativeProvider:
    def __init__(self):
        self.calls = []
        self.task = None
        self.hearing = None
        self.post_status = 200
        self.post_error = None
        self.remote_state = "BATCH_STATE_SUCCEEDED"
        self.argon_text = "一百五十首"
        self.mutate = lambda value: value
        self.transports = []
        self.metadata_calls = []
        self.metadata_response = HttpResponse(200, canonical({
            "name": "models/gemini-4-argon", "displayName": "Gemini 4 Argon",
            "supportedGenerationMethods": ["generateContent", "batchGenerateContent"],
        }))

    def factory(self, resolver):
        parent = self

        class Transport:
            def model_metadata(self, *, timeout_s):
                assert resolver({}).startswith("fictional-")
                parent.metadata_calls.append({"method": "GET",
                    "path": "models/gemini-4-argon", "body": None})
                if isinstance(parent.metadata_response, Exception):
                    raise parent.metadata_response
                return parent.metadata_response

            def request(self, method, path, body, *, route, timeout_s):
                key = resolver(route)
                parent.transports.append(
                    {"ordinal": route["accepted_key_ordinal"], "tier": route["accepted_key_tier"]}
                )
                assert key.startswith("fictional-")
                # Real normal provider slot must be held at this exact seam.
                from src.autoslice.provider_slots import provider_slot, ProviderSlotTimeout

                with pytest.raises(ProviderSlotTimeout):
                    with provider_slot(Path(os.environ["AUTOSLICE_BASE"]), timeout_seconds=0.001):
                        pass
                return parent.request(method, path, body, route=route, timeout_s=timeout_s)

        return Transport()

    def op(self, state=None):
        state = state or self.remote_state
        op = {
            "name": "batches/consumer-001",
            "metadata": {
                "name": "batches/consumer-001",
                "model": "models/gemini-4-argon",
                "displayName": "argon-" + self.task,
                "state": state,
            },
            "done": state not in {"BATCH_STATE_PENDING", "BATCH_STATE_RUNNING"},
        }
        if state == "BATCH_STATE_SUCCEEDED":
            op["metadata"]["output"] = {
                "inlinedResponses": {
                    "inlinedResponses": [
                        {"metadata": {"key": self.task}, "response": self.payload()}
                    ]
                }
            }
        elif op["done"]:
            op["error"] = {"code": 13, "message": "synthetic terminal"}
        return self.mutate(op)

    def payload(self):
        return {
            "modelVersion": "gemini-4-argon",
            "candidates": [
                {
                    "finishReason": "STOP",
                    "content": {
                        "parts": [
                            {
                                "text": json.dumps(
                                    {**self.hearing, "transcript": self.argon_text},
                                    ensure_ascii=False,
                                )
                            }
                        ]
                    },
                }
            ],
            "usageMetadata": {"promptTokenCount": 10, "totalTokenCount": 20},
        }

    def request(self, method, path, body, *, route, timeout_s):
        self.calls.append({"method": method, "path": path, "body": body, "route": deepcopy(route)})
        if path.endswith(":batchGenerateContent"):
            full = json.loads(body)
            item = full["batch"]["inputConfig"]["requests"]["requests"][0]
            self.task = item["metadata"]["key"]
            prompt = item["request"]["contents"][0]["parts"][1]["text"]
            marker = json.loads(prompt[prompt.rfind("{") :])
            self.hearing = {
                "schema_version": "argon-blind-hearing.v1",
                **marker,
                "status": "OBSERVED",
                "transcript": self.argon_text,
                "uncertainties": [],
                "speech_role": "unknown",
                "reason": None,
            }
            if self.post_error:
                raise self.post_error
            if self.post_status != 200:
                return HttpResponse(self.post_status, b'{"error":"synthetic quota"}')
            value = self.op("BATCH_STATE_PENDING")
        elif path.endswith(":generateContent"):
            value = self.payload()
        elif path.startswith("batches?"):
            value = {"operations": [self.op()]}
        else:
            value = self.op()
        return HttpResponse(200, json.dumps(value, ensure_ascii=False, indent=2).encode())


def _synthetic_target_scope(prompt, needs_audio):
    """New low-level fixture response; never edit an archived model completion.

    This synthetic scene's only contrast is the final unit of 一百五十秒/首.
    Quote every actual observed unit, including dissent, without word timing.
    """
    marker = "\nSPAN_REVIEW_INPUT\n"
    if marker not in prompt:
        return {}
    block = json.loads(prompt.split(marker, 1)[1].split("\nEND_SPAN_REVIEW_INPUT\n", 1)[0])
    alignments = []
    for row in block["observations"]:
        text = row["text"]
        if row["kind"] == "blind_pinyin":
            units = text.split()
            left, right = len(units)-1, len(units)
            quote = units[-1]
        else:
            stripped = text.rstrip("。！？?! ")
            left, right = len(stripped)-1, len(stripped)
            quote = text[left:right]
        alignments.append({"observation_id":row["observation_id"], "span":[left,right], "quote":quote})
    return {"span_review":{
        "binding_sha256":block["binding"]["binding_sha256"],
        "request_sha256":block["request_sha256"],
        "context_basis":"Synthetic scene: final unit after one-hundred-fifty; each actual observed unit is retained.",
        "all_audio_questions_located":True, "target_needs_audio":needs_audio,
        "alignments":alignments, "audio_questions":[],
    }}


@pytest.fixture
def scene(tmp_path, monkeypatch):
    from src.autoslice import producer_text_pipeline as pipeline, entity_audio_verifier as ordinary
    from src.autoslice import native_context_witness

    # Test world: these ladder/consumer mechanics model a free key that serves
    # Argon, contrary to the provider tier table (the real table is
    # asserted in tests/test_argon_tier_routing.py).
    from src.autoslice import argon_model_capability as _capability
    monkeypatch.setattr(_capability, "TIER_INFERENCE", {"free": "AVAILABLE", "paid_backup": "AVAILABLE"})
    root = tmp_path.resolve()
    source = root / "source.wav"
    out = root / "ordinary"
    out.mkdir()
    with wave.open(str(source), "wb") as handle:
        handle.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
        handle.writeframes(b"".join(struct.pack("<h", i % 2000 - 1000) for i in range(48000)))
    srt = "1\n00:00:01,000 --> 00:00:02,000\n一百五十秒\n\n2\n00:00:03,000 --> 00:00:04,000\n还差两个\n"
    finding = {
        "cue_index": 1,
        "suspect": "秒",
        "suggestion": "首",
        "proposed_full_cue": "一百五十首",
        "repair_class": "phonetic",
        "why": "synthetic source-bound doubt",
    }
    context = {
        "candidate_id": "synthetic-consumer",
        "context_sha256": "a" * 64,
        "source_media_sha256": sha(source.read_bytes()),
        "pieces": [{"source_media_sha256": sha(source.read_bytes())}],
    }
    monkeypatch.setenv("AUTOSLICE_BASE", str(root / "runtime"))
    monkeypatch.setenv("AUTOSLICE_PROVIDER_CONCURRENCY", "1")
    monkeypatch.setenv("GEMINI_PAID_BACKUP_DEV_EXCEPTION", "0")
    monkeypatch.delenv("GEMINI_PAID_BACKUP_DAILY_CAP", raising=False)
    provider = NativeProvider()
    state = SimpleNamespace(
        ordinary_status="UNCERTAIN",
        ordinary_calls=0,
        prompts=[],
        after_choice="PROPOSED",
        after_candidate="PROPOSAL",
        diagnosis="reading_chat",
        source=source,
        out=out,
        srt=srt,
        finding=finding,
        context=context,
        provider=provider,
        root=root,
        credentials={"GEMINI_API_KEY": "fictional-free-one"},
        loader_calls=0,
        after_needs_audio=False,
        ordinary_choice="CURRENT",
        ordinary_model="gemini-3.8-flash",
    )

    def credentials():
        state.loader_calls += 1
        return dict(state.credentials)

    monkeypatch.setattr(runtime, "load_credentials", credentials)
    monkeypatch.setattr(runtime, "ArgonHttpTransport", provider.factory)

    def judge(prompt):
        state.prompts.append(prompt)
        if "## 新增 Argon 独立盲听证据" in prompt:
            if getattr(state, "after_error", None):
                raise state.after_error
            return json.dumps(
                {
                    "choice": state.after_choice,
                    "candidate_id": state.after_candidate,
                    "needs_audio": state.after_needs_audio,
                    "reason": "synthetic CPA decision after verified independent evidence",
                    "current_utterance_supported": state.after_choice == "CURRENT",
                    "current_utterance_support_reason": "synthetic current supported independently",
                    **_synthetic_target_scope(prompt, state.after_needs_audio),
                }
            )
        if "字幕文字闭集裁决" in prompt or "疑似拼音" in prompt or "最终文字法官" in prompt:
            answer = {
                "choice": state.ordinary_choice,
                "needs_audio": True,
                "reason": "synthetic local listening required",
                "current_utterance_supported": False,
                "current_utterance_support_reason": "not established",
            }
            if state.diagnosis:
                answer["argon_escalation"] = {
                    state.diagnosis: True,
                    "reason": "synthetic exact target diagnosis",
                }
            return json.dumps(answer)
        return json.dumps(
            {
                "status": "UNRESOLVED",
                "choice": "NEITHER",
                "needs_audio": True,
                "reason": "synthetic unresolved",
            }
        )

    judge.cpa_cache_identity = {"models": ["synthetic-cpa"], "effort": "high"}
    state.judge = judge

    def observe(**kwargs):
        state.ordinary_calls += 1
        job = kwargs["job_dir"]
        prompt, _, _ = ordinary._entity_observation_prompt(
            request=kwargs["request"],
            candidates=kwargs["candidates"],
            recording_date=kwargs["recording_date"],
            timely_context=kwargs["timely_context"],
        )
        prompt_path = job / "prompt.md"
        prompt_path.write_text(prompt)
        raw = {
            "schema_version": "subtitle-span-acoustic-witness.v1",
            "status": state.ordinary_status,
            "heard_pinyin": "yi bai wu shi shou",
            "target_audible": True,
            "uncertain_positions": [],
            "syllable_count": 5,
            "confidence": 0.8,
            "reason": "synthetic attempt " + str(state.ordinary_calls),
        }
        response = job / "verdict.raw.json"
        response.write_text(json.dumps(raw))
        return ordinary._EntityProviderOutcome(
            raw, "agy", state.ordinary_model, prompt_path, response, None, None, []
        )

    monkeypatch.setattr(ordinary, "_observe_entity_audio", observe)
    original_builder = ordinary.build_local_audio_entity_verifier

    def builder(**kwargs):
        kwargs.update(model="gemini-3.8-flash", agy_bin="/usr/bin/true")
        return original_builder(**kwargs)

    monkeypatch.setattr(ordinary, "build_local_audio_entity_verifier", builder)
    monkeypatch.setattr(
        native_context_witness,
        "build_independent_context_witnesses",
        lambda *, fallback, **_: fallback,
    )
    monkeypatch.setattr(pipeline, "_ensure_local_native_audio_budget", lambda *_: None)
    monkeypatch.setattr(pipeline, "witness_audio_locally_resolvable", lambda *_, **__: True)
    monkeypatch.setattr(pipeline, "load_referent_groups", lambda *_, **__: [])
    monkeypatch.setattr(pipeline, "_build_final_review_llm_call", lambda: judge)

    def build(enabled=True, **config):
        spec = {"date": "2026-10-16"}
        if enabled is not None:
            spec["argon_escalation"] = {"enabled": enabled, **config}
        ctx = pipeline._build_entity_verification_context(
            spec=spec,
            padded=source,
            padded_dur=6000,
            host="localhost",
            text_override_path=None,
            cid="synthetic-consumer",
            out_root=out,
            srt_text=srt,
            authoritative_chat=[],
            adapters=SimpleNamespace(
                profile_asset_file=lambda _: root / "missing-entities.json",
                topic_graph_disabled=lambda: True,
            ),
        )
        return ctx.verify_confusable_entity

    state.build = build
    state.verifier = build()

    def run(verifier=None, finding=None, **kwargs):
        return adjudicate_context_finding(
            state.srt,
            finding or state.finding,
            entity_verifier=verifier or state.verifier,
            clip_context=state.context,
            source_media_timeline_offset_ms=1000,
            judge_llm_call=state.judge,
            require_complete_utterance_support=True,
            **kwargs,
        )

    state.run = run
    return state


def test_producer_pending_cold_recovery_reaches_real_cpa_prompt_and_typed_mutation(scene):
    before, first = scene.run()
    assert before == scene.srt and first.get("argon_escalation", {}).get("status") == "PENDING", (
        first
    )
    assert scene.ordinary_calls == 1
    resumed = scene.build()
    output, result = scene.run(resumed)
    assert result.get("mutation_authority", {}).get("status") == "PASS", result
    assert result["repaired"] and "一百五十首" in output and "还差两个" in output
    assert scene.ordinary_calls == 1
    assert [c["method"] for c in scene.provider.calls] == ["POST", "GET"]
    prompt = [p for p in scene.prompts if "## 新增 Argon 独立盲听证据" in p][-1]
    assert "synthetic attempt 1" in prompt and "一百五十首" in prompt
    assert result["request"]["argon_escalation_evidence"]
    recorder = SemanticAttemptRecorder(
        srt_text=scene.srt, finding=scene.finding, clip_context=scene.context, offset_ms=1000
    )
    latest = recorder._entries()[-1]
    assert any(
        row.get("kind") == "argon_acquisition"
        for row in latest["request"]["material"]["bound_judge_evidence"]
    )
    assert latest["request"]["prompt"] == prompt
    count = len(scene.prompts)
    output2, replay = scene.run(scene.build())
    assert output2 == output and replay["repaired"] and len(scene.prompts) == count


def test_default_off_and_explicit_provider_disabled_never_load_argon(scene):
    scene.diagnosis = None
    _, result = scene.run(scene.build(enabled=None))
    assert "argon_escalation" not in result and not scene.provider.calls and scene.loader_calls == 0
    budget = ContextAdjudicationBudget(
        limit=12,
        entity_verifier=scene.verifier,
        clip_context=scene.context,
        source_media_timeline_offset_ms=1000,
        judge_llm_call=scene.judge,
        screen_read_probe=None,
        adjudicate_context=adjudicate_context_finding,
        provider_calls_allowed=False,
    )
    result = budget.adjudicate(scene.srt, scene.finding)
    assert result["status"] == "SKIPPED_PROVIDER_DISABLED" and not scene.provider.calls


def test_wrong_argon_candidate_cannot_overrule_current_without_cpa(scene):
    scene.run()
    scene.provider.argon_text = "错误的模型提名"
    scene.after_choice = "CURRENT"
    output, result = scene.run(scene.build())
    assert output == scene.srt and not result["repaired"], result
    assert result["witness_judge"]["judge"]["choice"] == "CURRENT"
    assert "错误的模型提名" in scene.prompts[-1]


@pytest.mark.parametrize("diagnosis", ["reading_chat", "lyrics_reference_conflict"])
def test_only_actual_cpa_diagnosis_on_bound_hearing_triggers(scene, diagnosis):
    scene.diagnosis = diagnosis
    output, audit = scene.run()
    assert output == scene.srt and audit["argon_escalation"]["status"] == "PENDING", audit
    root = next((scene.root / "runtime/cache/argon-consumer").iterdir())
    recommendation = strict_json((root / "trigger.json").read_bytes())["recommendation"]
    assert recommendation["reasons"] == [
        "READING_CHAT" if diagnosis == "reading_chat" else "LYRICS_REFERENCE_CONFLICT"
    ]
    assert len(scene.provider.calls) == 1


def test_repeated_real_uncertain_same_finding_only_and_cached_hearing_does_not_count_twice(scene):
    scene.diagnosis = None
    _, one = scene.run()
    assert not scene.provider.calls, one
    # Merely copying UNCERTAIN/NEITHER statuses into a finding has no effect.
    forged = {
        **scene.finding,
        "ordinary_attempts": [{"status": "UNCERTAIN"}, {"status": "NEITHER"}],
        "argon_escalation": {"reading_chat": True},
    }
    scene.run(finding=forged)
    assert not scene.provider.calls and scene.ordinary_calls == 1
    other = {
        **scene.finding,
        "cue_index": 2,
        "suspect": "个",
        "suggestion": "遍",
        "proposed_full_cue": "还差两遍",
    }
    scene.run(finding=other)
    assert not scene.provider.calls
    # Retain the second actual raw. Two UNCERTAIN receipts, changing a
    # proposal or replaying the same audio cannot replace three distinct
    # effective observations under the current relisten contract.
    new = {**scene.finding, "suggestion": "个", "proposed_full_cue": "一百五十个"}
    _, two = scene.run(finding=new)
    assert not two.get("argon_escalation") and not scene.provider.calls
    before = scene.ordinary_calls
    scene.run(finding=new)
    assert scene.ordinary_calls == before and not scene.provider.calls


def test_repeated_neither_requires_actual_cpa_raw_not_pasted_judge(scene):
    scene.diagnosis = None
    scene.ordinary_status = "OBSERVED"
    scene.ordinary_choice = "NEITHER"
    scene.run()
    assert not scene.provider.calls
    new = {**scene.finding, "suggestion": "个", "proposed_full_cue": "一百五十个"}
    _, result = scene.run(finding=new)

    # Two text decisions over one actual hearing are not three listens.
    assert not result.get("argon_escalation") and not scene.provider.calls
    assert scene.ordinary_calls == 1
    history = list((scene.root / "runtime/cache/argon-consumer").glob("*/ordinary-history/*.json"))
    assert history  # Original audio/CPA records are retained, not reset.


def test_non_38_or_unbound_ordinary_output_does_not_trigger(scene):
    scene.ordinary_model = "unverified-3.8-impostor"
    scene.run()
    assert not scene.provider.calls and scene.loader_calls == 0


@pytest.mark.parametrize(
    "decision,needs", [("NEITHER", False), ("CURRENT", True), ("PROPOSED", True)]
)
def test_cpa_still_unresolved_blocks_existing_release_gate(scene, decision, needs):
    scene.run()
    scene.after_choice = decision
    scene.after_needs_audio = needs
    output, audit = scene.run(scene.build())
    assert output == scene.srt and not audit["repaired"], audit
    record = {
        "unresolved_findings_disclosed": [{**scene.finding, "exact_release_adjudication": audit}]
    }
    assert unresolved_findings_problem(record) is not None
    assert "CLEAN" not in audit.values() and audit["mutation_authority"]["status"] == "NOT_APPLIED"
    count = len(scene.provider.calls)
    scene.run(scene.build())
    assert len(scene.provider.calls) == count


@pytest.mark.parametrize(
    "failure", ["missing_model", "wrong_model", "contradiction", "incomplete", "uncertain"]
)
def test_bad_or_uncertain_argon_keeps_normal_finding_unresolved(scene, failure):
    scene.run()

    def mutate(op):
        if "output" not in op["metadata"]:
            return op
        payload = op["metadata"]["output"]["inlinedResponses"]["inlinedResponses"][0]["response"]
        if failure == "missing_model":
            payload.pop("modelVersion")
        elif failure == "wrong_model":
            payload["modelVersion"] = "gemini-3.8-flash"
        elif failure == "contradiction":
            op["error"] = {"code": 7, "message": "synthetic contradictory response"}
        elif failure == "incomplete":
            payload["candidates"][0]["finishReason"] = "MAX_TOKENS"
        else:
            hearing = json.loads(payload["candidates"][0]["content"]["parts"][0]["text"])
            hearing.update(
                status="UNCERTAIN",
                transcript=None,
                reason="synthetic unclear",
                uncertainties=[
                    {
                        "start_ms": hearing["target_window_ms"][0],
                        "end_ms": hearing["target_window_ms"][1],
                        "reason": "unclear",
                    }
                ],
            )
            payload["candidates"][0]["content"]["parts"][0]["text"] = json.dumps(hearing)
        return op

    scene.provider.mutate = mutate
    count = len(scene.prompts)
    output, audit = scene.run(scene.build())
    assert output == scene.srt and not audit["repaired"] and len(scene.prompts) == count, audit
    assert audit["argon_escalation"]["job_name"] == "batches/consumer-001"
    assert unresolved_findings_problem(
        {"unresolved_findings_disclosed": [{**scene.finding, "exact_release_adjudication": audit}]}
    )
    scene.run(scene.build(allow_realtime_fallback=True))
    assert len([c for c in scene.provider.calls if c["method"] == "POST"]) == 1


@pytest.mark.parametrize(
    "drift", ["source", "context_source", "target", "native_response", "hearing_projection"]
)
def test_source_window_raw_and_projected_evidence_drift_rejected_before_cpa(scene, drift):
    from src.autoslice.argon_escalation_integration import validate_consumer_evidence

    scene.run()
    _, audit = scene.run(scene.build())
    request = deepcopy(audit["request"])
    envelope = request["argon_escalation_evidence"]
    count = len(scene.prompts)
    if drift == "source":
        scene.source.write_bytes(scene.source.read_bytes() + b"changed")
        output, result = scene.run()
        assert output == scene.srt and not result["repaired"] and len(scene.prompts) == count
        return
    if drift == "native_response":
        from src.autoslice.argon_escalation_witness import ArgonEscalationWitness

        engine = ArgonEscalationWitness(Path(envelope["engine_root"]))
        raw_result = strict_json(
            (
                engine.root / envelope["task_id"] / "blobs" / (envelope["result_sha256"] + ".bin")
            ).read_bytes()
        )
        file = (
            engine.root / envelope["task_id"] / "blobs" / (raw_result["response_sha256"] + ".bin")
        )
        file.write_bytes(file.read_bytes() + b"changed")
    elif drift == "target":
        request["matched_start_ms"] += 1
    elif drift == "context_source":
        request["context_end_ms"] -= 1
    else:
        request["argon_source_media_sha256"] = "f" * 64
    request.pop("request_sha256")
    request["request_sha256"] = sha(canonical(request))
    with pytest.raises(ValueError):
        validate_consumer_evidence(request, envelope)
    assert len(scene.prompts) == count


def test_budget_normal_entry_does_not_replay_old_pending_in_memory(scene):
    budget = ContextAdjudicationBudget(
        limit=12,
        entity_verifier=scene.verifier,
        clip_context=scene.context,
        source_media_timeline_offset_ms=1000,
        judge_llm_call=scene.judge,
        screen_read_probe=None,
        adjudicate_context=adjudicate_context_finding,
    )
    first = budget.adjudicate(scene.srt, scene.finding)
    second = budget.adjudicate(scene.srt, scene.finding)
    assert first["argon_escalation"]["status"] == "PENDING"
    assert second["repaired"] and second["mutation_authority"]["status"] == "PASS", second
    assert [c["method"] for c in scene.provider.calls] == ["POST", "GET"]


def test_submission_unknown_cold_recovery_uses_original_key_no_post(scene):
    scene.provider.post_error = TimeoutError("synthetic")
    _, first = scene.run()
    assert first["argon_escalation"]["status"] == "SUBMIT_UNKNOWN"
    output, result = scene.run(scene.build())
    assert result["repaired"], result
    assert [c["method"] for c in scene.provider.calls] == ["POST", "GET", "GET"]
    assert all(r["ordinal"] == 1 for r in scene.provider.transports)


def test_changed_original_credential_never_rotates_or_resubmits_unknown(scene):
    scene.provider.post_error = TimeoutError("synthetic")
    scene.run()
    scene.credentials["GEMINI_API_KEY"] = "fictional-different"
    _, result = scene.run(scene.build())
    assert not result["repaired"]
    assert len(scene.provider.calls) == 1


def test_free_quota_next_key_is_bounded_and_all_old_accounts_retained(scene):
    scene.credentials["GEMINI_API_KEY_2"] = "fictional-free-two"
    scene.provider.post_status = 429
    _, one = scene.run()
    assert one["argon_escalation"]["status"] == "FREE_QUOTA_NEXT_ROUTE_PENDING"
    assert len(scene.provider.calls) == 1
    scene.provider.post_status = 200
    _, two = scene.run(scene.build())
    assert two["argon_escalation"]["status"] == "PENDING"
    assert [c["route"]["accepted_key_ordinal"] for c in scene.provider.calls] == [1, 2]
    assert len(two["argon_escalation"]["prior_key_attempts"]) == 1
    _, three = scene.run(scene.build())
    assert three["repaired"] and len(scene.provider.calls) == 3


@pytest.mark.parametrize(
    "allow_paid,dev,cap,expected",
    [
        (False, True, None, False),
        (True, False, None, False),
        (True, True, "0", True),  # Production ignores fee caps; grant scope is tested separately.
        (True, True, None, True),
    ],
)
def test_paid_policy_uses_real_temporary_gate_and_record_not_hearing_counts(
    scene, monkeypatch, allow_paid, dev, cap, expected
):
    from src.autoslice import gemini_backup_policy as paid

    scene.credentials["GEMINI_KEY_BACKUP"] = "fictional-paid"
    monkeypatch.setenv("GEMINI_PAID_BACKUP_DEV_EXCEPTION", "1" if dev else "0")
    if cap is not None:
        monkeypatch.setenv("GEMINI_PAID_BACKUP_DAILY_CAP", cap)
    scene.provider.post_status = 429
    scene.run(scene.build(allow_paid=allow_paid))
    task = scene.provider.task
    scene.provider.post_status = 200
    _, result = scene.run(scene.build(allow_paid=allow_paid))
    assert paid.free_chain_strikes(task) == 1  # Actual complete quota chain, not two unclear ears.
    assert paid.paid_calls_today() == int(expected)
    assert len([c for c in scene.provider.calls if c["method"] == "POST"]) == 1 + int(expected)
    if expected:
        assert result["argon_escalation"]["status"] == "PENDING"
        route = scene.provider.calls[-1]["route"]
        assert paid.valid_paid_policy_stamp(
            route["paid_backup_policy"],
            expected_item_key=task,
            expected_purpose=paid.CANDIDATE_BLIND_AUDIO_WITNESS_PURPOSE,
        )
        _, done = scene.run(scene.build(allow_paid=True))
        assert done["repaired"] and paid.paid_calls_today() == 1
    else:
        assert not result["repaired"]
        scene.run(scene.build(allow_paid=allow_paid))
        assert paid.paid_calls_today() == 0 and len(scene.provider.calls) == 1


def test_argon_response_is_full_blind_bytes_and_no_synthetic_key_in_artifacts(scene):
    import base64
    import io

    scene.run()
    scene.run(scene.build())
    body = json.loads(scene.provider.calls[0]["body"])
    parts = body["batch"]["inputConfig"]["requests"]["requests"][0]["request"]["contents"][0][
        "parts"
    ]
    prompt = parts[1]["text"]
    assert "CURRENT" not in prompt and "PROPOSED" not in prompt and "一百五十" not in prompt
    assert "歌词答案" not in prompt
    data = base64.b64decode(parts[0]["inlineData"]["data"], validate=True)
    with wave.open(io.BytesIO(data), "rb") as reader:
        assert reader.getnframes() == 32000  # Complete source 1.5..5.5 seconds, not target-only.
    for path in (scene.root / "runtime").rglob("*.json"):
        assert b"fictional-" not in path.read_bytes()


def test_genuine_new_argon_material_crosses_existing_three_unresolved_pressure_once(scene):
    scene.run()

    def old_answer(prompt):
        return '{"choice":"NEITHER","needs_audio":true}'

    old_answer.cpa_cache_identity = scene.judge.cpa_cache_identity
    recorder = SemanticAttemptRecorder(
        srt_text=scene.srt, finding=scene.finding, clip_context=scene.context, offset_ms=1000
    )
    wrapped = recorder.wrap(old_answer)
    wrapped("prior actual question one")
    wrapped("prior actual question two")
    scene.after_choice = "NEITHER"
    _, result = scene.run(scene.build())
    assert result["witness_judge"]["judge"]["choice"] == "NEITHER", result
    records = recorder._entries()
    offered = [
        row
        for row in records
        if any(
            item.get("kind") == "argon_acquisition"
            for item in row["request"]["material"].get("bound_judge_evidence", [])
        )
    ]
    assert len(offered) == 1
    assert offered[0]["continuation_reason"]["prior_unresolved_answers"] >= 3
    assert "bound_judge_evidence" in offered[0]["continuation_reason"]["changed_material_fields"]
    assert offered[0]["request"]["continuation_plan"]
    count = len(scene.prompts)
    scene.run(scene.build())
    assert len(scene.prompts) == count


@pytest.mark.parametrize("state", ["ERROR", "OUTCOME_UNKNOWN", "STARTED"])
def test_argon_offered_identity_survives_cpa_error_or_interruption(scene, monkeypatch, state):
    from src.autoslice import semantic_retry_progress as recording

    scene.run()
    if state in {"ERROR", "OUTCOME_UNKNOWN"}:
        scene.after_error = RuntimeError("synthetic error")
    else:
        original = recording._atomic

        def atomic(path, value):
            original(path, value)
            if value.get("status") == "STARTED" and any(
                i.get("kind") == "argon_acquisition"
                for i in value["request"]["material"].get("bound_judge_evidence", [])
            ):
                raise KeyboardInterrupt()

        monkeypatch.setattr(recording, "_atomic", atomic)
    if state in {"ERROR", "OUTCOME_UNKNOWN"}:
        scene.run(scene.build())
    else:
        with pytest.raises(KeyboardInterrupt):
            scene.run(scene.build())
    recorder = SemanticAttemptRecorder(
        srt_text=scene.srt, finding=scene.finding, clip_context=scene.context, offset_ms=1000
    )
    offered = [
        row
        for row in recorder._entries()
        if any(
            i.get("kind") == "argon_acquisition"
            for i in row["request"]["material"].get("bound_judge_evidence", [])
        )
    ]
    if state == "ERROR":
        # The current writer calls generic transport exceptions OUTCOME_UNKNOWN.
        # Retain that real generated input, then supply an explicit legacy ERROR
        # fixture on the SAME request/material for the historical reader case.
        assert offered and offered[0]["status"] == "OUTCOME_UNKNOWN"
        (scene.root / "original-unknown-cpa.json").write_bytes(canonical(offered[0]))
        legacy = {**offered[0], "status": "ERROR"}
        recording._atomic(recorder.directory / (legacy["request_sha256"] + ".json"), legacy)
        offered = [legacy]
    assert offered and offered[0]["status"] == state
    before = [canonical(row) for row in offered]
    scene.after_error = None
    if state == "STARTED":
        monkeypatch.setattr(recording, "_atomic", original)
    count = len(scene.prompts)
    output, result = scene.run(scene.build())
    assert output == scene.srt and not result["repaired"] and len(scene.prompts) == count
    assert [
        canonical(row)
        for row in recorder._entries()
        if any(
            i.get("kind") == "argon_acquisition"
            for i in row["request"]["material"].get("bound_judge_evidence", [])
        )
    ] == before
    assert len([r for r in scene.provider.calls if r["method"] == "POST"]) == 1


def test_relocated_wrapper_is_same_material_and_semantic_stop_not_another_cpa_vote(scene):
    import shutil
    from src.autoslice.argon_escalation_integration import (
        bind_evidence,
        material_identity,
        transcript_witness,
    )
    from src.autoslice.acoustic_witness_adjudication import judge_word_choice

    scene.run()
    scene.after_choice = "NEITHER"
    _, audit = scene.run(scene.build())
    request = audit["request"]
    original = request["argon_escalation_evidence"]
    relocated = deepcopy(original)
    new_engine = scene.root / "relocated-engine"
    shutil.copytree(original["engine_root"], new_engine)
    relocated["engine_root"] = str(new_engine)
    relocated.pop("envelope_sha256")
    relocated["envelope_sha256"] = sha(canonical(relocated))
    other = deepcopy(request)
    other["proposed_candidates"] = [
        row
        for row in other["proposed_candidates"]
        if row["source"].get("kind") != "argon_target_transcript"
    ]
    other = bind_evidence(other, relocated)
    assert material_identity(request, original) == material_identity(other, relocated)
    recorder = SemanticAttemptRecorder(
        srt_text=scene.srt, finding=scene.finding, clip_context=scene.context, offset_ms=1000
    )
    call = recorder.wrap(scene.judge)
    count = len(scene.prompts)
    result = judge_word_choice(
        check_request=other, witness=transcript_witness(other, relocated), llm_call=call
    )
    assert result["reason_code"] == "JUDGE_NO_NEW_EVIDENCE", result
    assert len(scene.prompts) == count and recorder.plan is None


def test_provider_slot_refusal_has_no_remote_submission_and_can_resume_normally(scene):
    from src.autoslice.provider_slots import provider_slot

    with provider_slot(scene.root / "runtime", timeout_seconds=0.1):
        _, first = scene.run(scene.build(slot_wait_s=0.01))
    assert not first["repaired"] and not scene.provider.calls
    _, second = scene.run(scene.build(slot_wait_s=0.01))
    assert second["argon_escalation"]["status"] == "PENDING", second
    assert len(scene.provider.calls) == 1


def test_expired_batch_explicit_realtime_uses_normal_cpa_no_duplicate_batch(scene):
    scene.provider.remote_state = "BATCH_STATE_EXPIRED"
    scene.run()
    _, failed = scene.run(scene.build())
    assert failed["argon_escalation"]["status"] == "FAILED"
    _, held = scene.run(scene.build())
    assert not held["repaired"] and len(scene.provider.calls) == 2
    _, fixed = scene.run(scene.build(allow_realtime_fallback=True))
    assert fixed["repaired"], fixed
    assert [c["path"].split(":")[-1] for c in scene.provider.calls if c["method"] == "POST"] == [
        "batchGenerateContent",
        "generateContent",
    ]


def test_cross_process_resume_enters_same_normal_public_adjudicator(scene):
    import subprocess
    import sys

    scene.run()
    data = {
        "source": str(scene.source),
        "ordinary_root": str(scene.out),
        "base": str(scene.root / "runtime"),
        "srt": scene.srt,
        "finding": scene.finding,
        "context": scene.context,
        "task": scene.provider.task,
        "hearing": scene.provider.hearing,
    }
    inputs = scene.root / "child-input.json"
    inputs.write_bytes(canonical(data))
    script = scene.root / "child-resume.py"
    script.write_text("""import json,sys,socket,os
from pathlib import Path
for name in ('connect','connect_ex'):
 setattr(socket.socket,name,lambda *a,**k: (_ for _ in ()).throw(AssertionError('external forbidden')))
socket.getaddrinfo=lambda *a,**k: (_ for _ in ()).throw(AssertionError('external forbidden'))
sys.path.insert(0,str(Path.cwd()/'tests'))
from test_argon_consumer_integration import NativeProvider, _synthetic_target_scope
from src.autoslice.argon_escalation_integration import build_argon_integration
from src.autoslice.final_review_auditor import adjudicate_context_finding
x=json.loads(Path(sys.argv[1]).read_text())
p=NativeProvider();p.task=x['task'];p.hearing=x['hearing']
def ordinary(request):raise AssertionError('No repeated ordinary hearing permitted')
ordinary.argon_escalation=build_argon_integration(config={'enabled':True},source_media=Path(x['source']),
 source_duration_ms=6000,ordinary_output_dir=Path(x['ordinary_root']),runtime_root=Path(x['base']),
 credential_loader=lambda:{'GEMINI_API_KEY':'fictional-free-one'},transport_factory=p.factory)
def cpa(prompt):
 assert '## 新增 Argon 独立盲听证据' in prompt and 'synthetic attempt 1' in prompt
 return json.dumps({'choice':'PROPOSED','candidate_id':'PROPOSAL','needs_audio':False,'reason':'synthetic child CPA',**_synthetic_target_scope(prompt,False)})
cpa.cpa_cache_identity={'models':['synthetic-cpa'],'effort':'high'}
output,audit=adjudicate_context_finding(x['srt'],x['finding'],entity_verifier=ordinary,clip_context=x['context'],
 source_media_timeline_offset_ms=1000,judge_llm_call=cpa,require_complete_utterance_support=True)
assert audit['repaired'] and audit['mutation_authority']['status']=='PASS',audit
assert [r['method'] for r in p.calls]==['GET']
print(json.dumps({'repaired':True,'typed_status':audit['mutation_authority']['status'],
 'methods':[r['method'] for r in p.calls],'real_provider_calls':0}))
""")
    done = subprocess.run(
        [sys.executable, str(script), str(inputs)],
        capture_output=True,
        text=True,
        timeout=45,
        env={**os.environ, "PYTHONPATH": str(Path.cwd()), "PYTHONDONTWRITEBYTECODE": "1"},
    )
    (scene.root / "child.log").write_text(done.stdout + done.stderr)
    assert done.returncode == 0, done.stdout + done.stderr
    assert json.loads(done.stdout)["methods"] == ["GET"]


@pytest.mark.parametrize("field", ["recommendation", "finding"])
def test_normal_consumer_recomputes_trigger_not_just_file_presence(scene, field):
    from src.autoslice.argon_escalation_integration import validate_consumer_evidence

    scene.run()
    _, audit = scene.run(scene.build())
    envelope = audit["request"]["argon_escalation_evidence"]
    path = Path(envelope["consumer_root"]) / "trigger.json"
    trigger = json.loads(path.read_bytes())
    if field == "recommendation":
        trigger["recommendation"]["matched_unresolved_count"] = 500
    else:
        trigger["finding"]["finding_id"] = "f" * 64
    path.write_bytes(canonical(trigger))
    with pytest.raises(ValueError):
        validate_consumer_evidence(audit["request"], envelope)
    count = len(scene.prompts)
    output, rejected = scene.run(scene.build())
    assert output == scene.srt and not rejected["repaired"] and len(scene.prompts) == count


def test_argon_alternative_selected_then_rebound_through_normal_typed_judge(scene):
    scene.run()
    scene.provider.argon_text = "一百五十个"
    original = scene.judge
    argon_calls = []

    def judge(prompt):
        if "## 新增 Argon 独立盲听证据" in prompt:
            argon_calls.append(prompt)
            return json.dumps(
                {
                    "choice": "PROPOSED",
                    "candidate_id": "SOURCE_1" if len(argon_calls) == 1 else "PROPOSAL",
                    "needs_audio": False,
                    "reason": "synthetic explicit alternative selection",
                    **_synthetic_target_scope(prompt, False),
                }
            )
        return original(prompt)

    judge.cpa_cache_identity = original.cpa_cache_identity
    scene.judge = judge
    output, audit = scene.run(scene.build())
    assert audit["repaired"] and audit["mutation_authority"]["status"] == "PASS", audit
    assert "一百五十个" in output and "一百五十首" not in output
    assert len(argon_calls) == 2
    assert audit["request"]["proposed_cue"] == "一百五十个"
    assert audit["witness_judge"]["judge"]["selected_candidate_text"] == "一百五十个"
    assert audit["request"]["argon_escalation_evidence"]
    assert scene.ordinary_calls == 1 and [x["method"] for x in scene.provider.calls] == [
        "POST",
        "GET",
    ]


def test_paid_realtime_uses_new_real_shared_stamp_and_never_renews_old_use(scene, monkeypatch):
    from src.autoslice import gemini_backup_policy as paid

    scene.credentials["GEMINI_KEY_BACKUP"] = "fictional-paid"
    monkeypatch.setenv("GEMINI_PAID_BACKUP_DEV_EXCEPTION", "1")
    scene.provider.post_status = 429
    scene.run(scene.build(allow_paid=True))
    scene.provider.post_status = 200
    scene.run(scene.build(allow_paid=True))
    scene.provider.remote_state = "BATCH_STATE_EXPIRED"
    _, expired = scene.run(scene.build(allow_paid=True))
    assert expired["argon_escalation"]["status"] == "FAILED"
    output, audit = scene.run(scene.build(allow_paid=True, allow_realtime_fallback=True))
    assert audit["repaired"], audit
    assert paid.paid_calls_today() == 2
    posts = [x for x in scene.provider.calls if x["method"] == "POST"]
    assert len(posts) == 3
    assert posts[1]["route"]["paid_backup_policy"] != posts[2]["route"]["paid_backup_policy"]
    scene.run(scene.build(allow_paid=True, allow_realtime_fallback=True))
    assert (
        paid.paid_calls_today() == 2
        and len([x for x in scene.provider.calls if x["method"] == "POST"]) == 3
    )


def test_completed_native_cache_does_not_need_provider_slot_or_credentials(scene):
    from src.autoslice.provider_slots import provider_slot

    scene.run()
    output, audit = scene.run(scene.build())
    count = scene.loader_calls
    with provider_slot(scene.root / "runtime", timeout_seconds=0.1):
        repeated, again = scene.run(scene.build(slot_wait_s=0.01))
    assert repeated == output and again["repaired"], again
    assert scene.loader_calls == count and len(scene.provider.calls) == 2


def test_normal_finalizer_persists_replayable_argon_binding_and_preserves_human_veto(scene):
    from src.autoslice.producer_package_finalization import _apply_exact_final_cpa_repairs
    from src.autoslice.exact_final_witness_authority import valid_acoustic_witness_binding
    from src.autoslice.deferred_same_cue_resolution import _staged_owner
    from src.autoslice.producer_text_pipeline import _continuation_validate_existing_repair

    scene.run()
    output, adjudication = scene.run(scene.build())
    row = {
        **scene.finding,
        "base_text_sha256": sha("一百五十秒".encode()),
        "context_audio_adjudication": adjudication,
    }
    final, repairs = _apply_exact_final_cpa_repairs(
        scene.srt,
        {"findings": [row]},
        clip_context=scene.context,
        adjudication_stage="context_audio_adjudication",
    )
    assert final == output and len(repairs) == 1
    binding = repairs[0]["acoustic_witness_binding"]
    assert valid_acoustic_witness_binding(binding, proposed="一百五十首", window=(1000, 2000))
    assert binding["witness"]["argon_escalation_evidence"]
    staged = _staged_owner(1, adjudication)
    assert staged["acoustic_witness_binding"] == binding
    _continuation_validate_existing_repair(staged, srt_text=scene.srt, clip_context=scene.context)
    kept, blocked = _apply_exact_final_cpa_repairs(
        scene.srt,
        {"findings": [row]},
        clip_context=scene.context,
        adjudication_stage="context_audio_adjudication",
        protected_cue_indexes=frozenset({1}),
    )
    assert kept == scene.srt and not blocked
    assert row["human_literal_proposal_veto"]["mutation_authorized"] is False


def test_normal_typed_consumer_rejects_native_raw_drift_after_cpa_has_completed(scene):
    from src.autoslice.producer_package_finalization import _apply_exact_final_cpa_repairs
    from src.autoslice.final_review_carryover import adjudicated_proposed_full_cue
    from src.autoslice.final_review_auditor import audit_correction_mutation_authority

    scene.run()
    output, adjudication = scene.run(scene.build())
    row = {
        **scene.finding,
        "base_text_sha256": sha("一百五十秒".encode()),
        "routed": "context_audio_adjudicated_fix",
        "context_audio_adjudication": adjudication,
    }
    envelope = adjudication["request"]["argon_escalation_evidence"]
    task = Path(envelope["engine_root"]) / envelope["task_id"]
    result = strict_json((task / "blobs" / (envelope["result_sha256"] + ".bin")).read_bytes())
    raw = task / "blobs" / (result["response_sha256"] + ".bin")
    raw.write_bytes(raw.read_bytes() + b"drift")
    assert (
        adjudicated_proposed_full_cue(
            row,
            srt_text=scene.srt,
            clip_context=scene.context,
            adjudication_stage="context_audio_adjudication",
        )
        is None
    )
    final, repairs = _apply_exact_final_cpa_repairs(
        scene.srt,
        {"findings": [row]},
        clip_context=scene.context,
        adjudication_stage="context_audio_adjudication",
    )
    assert final == scene.srt and repairs == []
    report = audit_correction_mutation_authority(
        {"status": "APPLIED", "applied_count": 1, "findings": [row]}
    )
    assert report["status"] == "BLOCK"


@pytest.mark.parametrize("prior,allowed", [(2, False), (3, True)])
def test_strict_paid_gate_uses_round_start_strikes_not_current_just_added_strike(
    scene, prior, allowed
):
    from src.autoslice import gemini_backup_policy as paid

    scene.credentials["GEMINI_KEY_BACKUP"] = "fictional-paid"
    scene.provider.post_status = 429
    scene.run(scene.build(allow_paid=True))
    task = scene.provider.task
    for _ in range(prior):
        paid.record_free_chain_failure(task)
    scene.provider.post_status = 200
    _, audit = scene.run(scene.build(allow_paid=True))
    assert paid.free_chain_strikes(task) == prior + 1
    assert paid.paid_calls_today() == int(allowed)
    assert len(scene.provider.calls) == 1 + int(allowed)
    if allowed:
        assert audit["argon_escalation"]["status"] == "PENDING"
    else:
        assert not audit["repaired"]
