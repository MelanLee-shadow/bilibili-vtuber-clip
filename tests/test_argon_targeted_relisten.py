"""Argon increment: normal relisten entry, full synthetic native responses only."""
from copy import deepcopy
import json
import os
import hashlib
import subprocess
import wave
from types import SimpleNamespace
from pathlib import Path

import pytest

from src.autoslice import final_review_auditor as auditor
from src.autoslice import agy_gemini_client as common_agy
_REAL_RUN_LOCAL_AGY = common_agy.run_local_agy
from src.autoslice.argon_escalation_integration import build_argon_integration
from test_argon_consumer_integration import NativeProvider, offline  # network/secret guard
from test_targeted_relisten_public_contract import (
    SRT, FINDING, build_offline_rig, three_observations, public_judge, apply_save_and_cold_review,
)


@pytest.fixture
def tmp_path(tmp_path, request):
    root = os.environ.get("AUTOSLICE_RELISTEN_TEST_ARTIFACTS")
    if root:
        tmp_path = Path(root) / hashlib.sha256(request.node.nodeid.encode()).hexdigest()[:16]
        tmp_path.mkdir(parents=True, exist_ok=False)
    return tmp_path


def attach_argon(rig, monkeypatch, provider=None):
    from src.autoslice import argon_escalation_agy as native
    monkeypatch.setenv("AUTOSLICE_PROVIDER_CONCURRENCY", "1")
    monkeypatch.setattr(native, "availability", lambda: (str(rig.tmp / "absent-agy"), False))
    # Test world: these ladder/consumer mechanics model a free key that serves
    # Argon, contrary to the provider tier table (the real table is
    # asserted in tests/test_argon_tier_routing.py).
    from src.autoslice import argon_model_capability as _capability
    monkeypatch.setattr(_capability, "TIER_INFERENCE", {"free": "AVAILABLE", "paid_backup": "AVAILABLE"})
    provider = provider or NativeProvider()
    provider.argon_text = "我听到了，哼唱"
    integration = build_argon_integration(
        config={"enabled": True}, source_media=rig.source, source_duration_ms=10000,
        ordinary_output_dir=rig.output, ordinary_verifier=rig.local,
        runtime_root=rig.tmp / "base",
        credential_loader=lambda: {"GEMINI_API_KEY": "fictional-free-one"},
        transport_factory=provider.factory)
    rig.verifier.argon_escalation = integration
    return provider, integration


def run_public(rig, finding, judge):
    return auditor.adjudicate_context_finding(
        SRT, deepcopy(finding), entity_verifier=rig.verifier, clip_context=rig.context,
        judge_llm_call=judge, require_complete_utterance_support=True)


def test_three_real_distinct_observations_trigger_existing_argon_from_public_entry(tmp_path, monkeypatch):
    rig = build_offline_rig(tmp_path, monkeypatch)
    witness, check, finding, observations = three_observations(rig)
    provider, integration = attach_argon(rig, monkeypatch)
    before = len(rig.transport.calls)
    output, audit = run_public(rig, finding, public_judge(rig))
    assert len(rig.transport.calls) == before == 3
    assert len([c for c in provider.calls if c["method"] == "POST"]) == 1, {k: audit.get(k) for k in ("reason_code", "policy_branch", "semantic_audio_recovery")}
    assert audit["reason_code"] == "ARGON_ESCALATION_PENDING_OR_UNRESOLVED"
    assert output == SRT and audit["repaired"] is False
    trigger = json.loads((integration.session(check, finding).root / "trigger.json").read_text())
    assert trigger["schema_version"] == "argon-targeted-relisten-trigger.v1"
    assert trigger["recommendation"]["basis"] == "STRONGER_HEARING_MODEL_AFTER_THREE_VALID_OBSERVATIONS"
    assert trigger["recommendation"]["effective_distinct_observations"] == 3
    assert trigger["recommendation"]["requested_model"] == "gemini-4-argon"
    assert trigger["recommendation"]["disputed_codepoint_span"] == [5, 7]


@pytest.mark.parametrize("stop_after", [1, 2])
def test_argon_does_not_force_three_when_normal_audio_resolves(tmp_path, monkeypatch, stop_after):
    rig = build_offline_rig(tmp_path, monkeypatch)
    rig.transport.pinyin = "wo ting dao le wu wu"
    provider, _ = attach_argon(rig, monkeypatch)
    output, audit = run_public(rig, FINDING, public_judge(rig, stop_after=stop_after))
    assert len(rig.transport.calls) == stop_after
    assert not provider.calls
    assert audit["policy_branch"] == "JUDGE_KEEPS_CURRENT"
    assert output == SRT


def argon_judge(rig, *, needs_audio=False, choice="PROPOSED", complete_no_gain=True):
    fallback = public_judge(rig)
    prompts = []
    def judge(prompt):
        prompts.append(prompt)
        if prompt.startswith("# CPA 补听范围选择") and not complete_no_gain:
            return json.dumps({"decision":"NO_NEW_AUDIO", "reason":"合成负控：仅字符串，缺少穷尽证明。"})
        if "## 新增 Argon 独立盲听证据" not in prompt:
            return fallback(prompt)
        reply = {"choice": choice, "candidate_id": "PROPOSAL",
            "needs_audio": needs_audio, "reason": "合成协议：新原音观察支持目标尾部，保留清楚前半句。",
            "current_utterance_supported": choice == "CURRENT",
            "current_utterance_support_reason": "合成协议核当前完整句。"}
        if "SPAN_REVIEW_INPUT" in prompt:
            data = json.loads(prompt.split("SPAN_REVIEW_INPUT\n", 1)[1].split("\nEND_SPAN_REVIEW_INPUT", 1)[0])
            alignments = []
            for row in data["observations"]:
                units = row["text"].split() if row["kind"] == "blind_pinyin" else list(row["text"].rstrip("。！？?! "))
                left, right = len(units)-2, len(units)
                quote = " ".join(units[left:right]) if row["kind"] == "blind_pinyin" else row["text"][left:right]
                alignments.append({"observation_id": row["observation_id"], "span": [left,right], "quote": quote})
            reply["span_review"] = {"binding_sha256": data["binding"]["binding_sha256"],
                "request_sha256": data["request_sha256"], "context_basis": "合成协议仅裁目标尾部，清楚前缀不改。",
                "all_audio_questions_located": True, "target_needs_audio": needs_audio,
                "alignments": alignments, "audio_questions": []}
        return json.dumps(reply, ensure_ascii=False)
    judge.prompts = prompts
    judge.cpa_cache_identity = {"transport": "offline-protocol",
                                "models": ["offline-argon-cpa"], "effort": "high"}
    return judge


def install_native_agy(rig, monkeypatch, *, outcome="heard"):
    from src.autoslice import argon_escalation_agy as native
    from src.autoslice.argon_escalation_contract import sha, canonical
    calls = []
    monkeypatch.setattr(native, "availability", lambda: ("/offline-protocol/agy", True))
    def catalog(argv, **kwargs):
        assert argv == ["/offline-protocol/agy", "models"]
        assert not (Path(kwargs["cwd"]) / "agy-input").exists()
        return subprocess.CompletedProcess(argv, 0,
            b"gemini-4-argon\tGemini 4 Argon\n", b"Fetching available models...\n")
    monkeypatch.setattr(native, "execute_model_catalog", catalog)
    ordinary_runner = common_agy.run_local_agy
    def route_runner(argv, **kwargs):
        # Preserve the ordinary audio fixture; only the native adapter passes
        # its durable send boundary as command_runner.
        if kwargs.get("command_runner") is not None:
            return _REAL_RUN_LOCAL_AGY(argv, **kwargs)
        return ordinary_runner(argv, **kwargs)
    monkeypatch.setattr(common_agy, "run_local_agy", route_runner)
    def send(argv, **kwargs):
        directory = Path(kwargs["cwd"])
        prompt = (directory / "prompt.md").read_text()
        with wave.open(str(directory / "input.wav")) as handle:
            assert handle.getnframes() > 0 and handle.getnchannels() == 1
        assert argv[argv.index("--model")+1] == native.AGY_MODEL
        assert kwargs["umask"] == 0o077
        assert "豆沙" not in prompt and "呜呜" not in prompt and "哼唱" not in prompt
        from src.autoslice.provider_slots import provider_slot, ProviderSlotTimeout
        from tests.test_provider_slots_cross_process_admission import assert_route_held
        assert_route_held(rig.tmp / "base", 'agy', 'audio_video')
        calls.append({"argv": argv, "cwd": str(directory),
            "audio_sha256": sha((directory / "input.wav").read_bytes()),
            "prompt_sha256": sha(prompt.encode())})
        if outcome == "timeout":
            raise subprocess.TimeoutExpired(argv, 1, output="synthetic partial")
        if outcome == "unknown_rc":
            return subprocess.CompletedProcess(argv, 2, "", "synthetic unidentified failure")
        if outcome == "quota":
            return subprocess.CompletedProcess(argv, 1, "", "quota exhausted")
        marker = json.loads(prompt[prompt.rfind("{"):])
        raw = {"schema_version":"argon-blind-hearing.v1", **marker,
            "status":"OBSERVED", "transcript":"我听到了，哼唱", "uncertainties":[],
            "speech_role":"unknown", "reason":None}
        if outcome == "uncertain":
            raw.update(status="UNCERTAIN", transcript="我听到了",
                uncertainties=[{"start_ms":marker["target_window_ms"][0],
                    "end_ms":marker["target_window_ms"][1],"reason":"synthetic unresolved tail"}],
                reason="synthetic unresolved tail")
        path = directory / "hearing.json"
        if outcome == "bad_json":
            path.write_text("not a native hearing"); path.chmod(0o600)
            return subprocess.CompletedProcess(argv, 0, "", "")
        path.write_text(json.dumps(raw,ensure_ascii=False)); path.chmod(0o600)
        return subprocess.CompletedProcess(argv, 0, "native CLI wrote hearing.json", "")
    monkeypatch.setattr(native, "execute_command", send)
    return calls


def test_native_agy_is_first_and_preserves_native_identity(tmp_path, monkeypatch):
    rig = build_offline_rig(tmp_path, monkeypatch)
    rig.transport.pinyin = "wo ting dao le heng chang"
    _, check, finding, _ = three_observations(rig)
    # Normal exact-review findings bind the current cue before typed apply.
    finding = {**finding, "base_text_sha256": check["base_text_sha256"]}
    provider, integration = attach_argon(rig, monkeypatch)
    calls = install_native_agy(rig, monkeypatch)
    output, audit = run_public(rig, finding, argon_judge(rig))
    (rig.output / "argon-public-audit.json").write_text(json.dumps(audit, ensure_ascii=False))
    assert len(calls) == 1 and not provider.calls
    assert audit["argon_escalation"]["status"] == "OBSERVED", audit.get("reason_code")
    session = integration.session(check, finding)
    assert session.resume() and session.envelope
    from src.autoslice.argon_escalation_integration import validate_consumer_evidence
    result = validate_consumer_evidence(session.mark_request(check), session.envelope)
    assert result["mode"] == "agy" and result["provider"] == "agy"
    assert result["model_version"] is None
    assert result["selected_model_label"] == "Gemini 4 Argon"
    assert result["usage_receipt"]["actual_charge_usd"] is None
    assert len(calls) == 1 and not provider.calls
    assert output == SRT.replace("我听到了，呜呜", "我听到了，哼唱")
    assert audit["repaired"] is True, {k:audit.get(k) for k in ("reason_code","policy_branch","mutation_authority")}
    apply_save_and_cold_review(rig, [{**finding, "exact_release_adjudication": audit}],
                               expected_literal_transcription=None)
    # Rehashing a different current question cannot make this old trigger current.
    # (G6, admin the whole-film SRT bytes are representation and only
    # a recorded delta -- tests/test_identity_g6_audio_question.py; a
    # different current text of the target cue is a different audio question.)
    from src.autoslice.argon_escalation_contract import canonical, sha
    drift = deepcopy(session.mark_request(check))
    drift["whole_clip_current_srt"] = drift["whole_clip_current_srt"].replace(
        drift["current_cue"], drift["current_cue"] + "啊")
    drift["current_cue"] = drift["current_cue"] + "啊"
    drift["base_text_sha256"] = sha(drift["current_cue"].encode())
    drift.pop("request_sha256")
    drift["request_sha256"] = sha(canonical(drift))
    with pytest.raises(ValueError, match="ARGON_TARGETED_CURRENT_CONTEXT_DRIFT"):
        validate_consumer_evidence(drift, session.envelope)
    assert len(calls) == 1 and not provider.calls


def test_unknown_native_agy_keeps_original_reservation_and_does_not_fallback(tmp_path, monkeypatch):
    rig = build_offline_rig(tmp_path, monkeypatch)
    _, check, finding, _ = three_observations(rig)
    provider, integration = attach_argon(rig, monkeypatch)
    calls = install_native_agy(rig, monkeypatch, outcome="timeout")
    _, first = run_public(rig, finding, public_judge(rig))
    # New integration object must read the original ambiguous native ledger.
    attach_argon(rig, monkeypatch, provider)
    _, second = run_public(rig, finding, public_judge(rig))
    assert len(calls) == 1 and not provider.calls
    assert first["argon_escalation"]["status"] == second["argon_escalation"]["status"] == "UNKNOWN"
    assert first["repaired"] is second["repaired"] is False
    histories = second["argon_escalation"]["accounting"]["histories"]
    assert next(r for r in histories if r["mode"] == "agy")["state"] == "SUBMIT_AMBIGUOUS"
    assert second["argon_escalation"]["dispatches_this_advance"] == 0


def test_api_ready_reuses_original_task_and_cpa_consumes_once(tmp_path, monkeypatch):
    rig = build_offline_rig(tmp_path, monkeypatch)
    rig.transport.pinyin = "wo ting dao le heng chang"
    _, check, finding, _ = three_observations(rig)
    provider, integration = attach_argon(rig, monkeypatch)
    judge = argon_judge(rig, needs_audio=True, choice="NEITHER", complete_no_gain=False)
    _, pending = run_public(rig, finding, judge)
    assert pending["argon_escalation"]["status"] != "OBSERVED"
    _, unresolved = run_public(rig, finding, judge)
    assert unresolved["argon_escalation"]["status"] == "OBSERVED"
    assert unresolved["repaired"] is False
    before = len(judge.prompts)
    _, replay = run_public(rig, finding, judge)
    assert replay["repaired"] is False
    assert len(judge.prompts) == before, "Same Argon acquisition must not buy another text judgment"
    assert len([c for c in provider.calls if c["method"] == "POST"]) == 1
    assert len(rig.transport.calls) == 3
    assert replay["argon_escalation"]["new_usable_observations_this_advance"] == 0


def test_argon_proposal_cannot_force_edit_against_current_cpa(tmp_path, monkeypatch):
    rig = build_offline_rig(tmp_path, monkeypatch)
    rig.transport.pinyin = "wo ting dao le wu wu"
    _, check, finding, _ = three_observations(rig)
    provider, integration = attach_argon(rig, monkeypatch)
    calls = install_native_agy(rig, monkeypatch)
    output, audit = run_public(rig, finding, argon_judge(rig, choice="CURRENT"))
    assert len(calls) == 1
    assert output == SRT and audit["repaired"] is False
    assert (audit.get("mutation_authority") or {}).get("status") != "PASS"


def test_production_quota_routes_use_original_paid_ledger_without_cap(tmp_path, monkeypatch):
    from src.autoslice import gemini_backup_policy as paid
    rig = build_offline_rig(tmp_path, monkeypatch)
    rig.transport.pinyin = "wo ting dao le heng chang"
    _, check, finding, _ = three_observations(rig)
    provider, integration = attach_argon(rig, monkeypatch)
    integration.runtime.config["allow_paid"] = True
    integration.runtime.loader = lambda: {"GEMINI_API_KEY":"fictional-free-one",
        "GEMINI_API_KEY_2":"fictional-free-two", "GEMINI_KEY_BACKUP":"fictional-paid"}
    calls = install_native_agy(rig, monkeypatch, outcome="quota")
    monkeypatch.setenv("GEMINI_PAID_BACKUP_DAILY_CAP", "1")
    monkeypatch.setenv("GEMINI_PAID_BACKUP_DEV_EXCEPTION", "0")
    provider.post_status = 429
    judge = argon_judge(rig)
    _, first = run_public(rig, finding, judge)
    assert len(calls) == 1 and len(provider.calls) == 1
    task_id = first["argon_escalation"]["task_id"]
    # Isolated policy fixture: three historical full-chain strikes and a
    # pre-existing paid use. These are not real historical provider charges.
    for _ in range(3):
        paid.record_free_chain_failure(task_id)
    old = paid.record_paid_use(task_id, purpose=paid.CANDIDATE_BLIND_AUDIO_WITNESS_PURPOSE)
    assert old["daily_cap"] == 1
    assert paid.paid_attempt_allowed(task_id, key_configured=True)[0] is False
    _, second = run_public(rig, finding, judge)
    assert second["argon_escalation"]["status"] == "FREE_QUOTA_NEXT_ROUTE_PENDING"
    provider.post_status = 200
    _, third = run_public(rig, finding, judge)
    assert [c["route"]["accepted_key_tier"] for c in provider.calls] == ["free","free","paid_backup"]
    stamp = provider.calls[-1]["route"]["paid_backup_policy"]
    assert stamp["daily_cap"] is None and stamp["calls_today_before"] == 1
    assert stamp["free_chain_strikes"] >= 3
    # Main's saved-gate rule: once the native job completes, the public run
    # consumes the latest saved gate rather than the pre-upgrade one.
    output, final = run_public(rig, {**finding, "exact_release_adjudication": third}, judge)
    assert final["argon_escalation"]["status"] == "OBSERVED"
    assert len(calls) == 1
    assert final["argon_escalation"]["new_usable_observations_this_advance"] == 1
    assert final["argon_escalation"]["dispatches_this_advance"] == 0
    assert len(final["argon_escalation"]["prior_key_attempts"]) == 2
    assert paid.paid_calls_today() == 2
    assert output == SRT.replace("我听到了，呜呜","我听到了，哼唱")
    root = integration.session(check,finding).root / "batch"
    route = json.loads((root / "routing-policy.json").read_text())
    assert route["scope"] == "production" and route["comparison"] is False
    # Cold scope change is rejected before either transport can run.
    integration.runtime.config["comparison_plan"] = str(tmp_path / "old-five-dollar-plan.json")
    integration.runtime.config["comparison_grant"] = str(tmp_path / "old-five-dollar-grant.json")
    before = len(provider.calls)
    _, rejected = run_public(rig, finding, judge)
    assert rejected["detail"] == "ARGON_ROUTING_POLICY_DRIFT"
    assert len(provider.calls) == before and len(calls) == 1
    assert json.loads((root / "routing-policy.json").read_text()) == route


@pytest.mark.parametrize("outcome", ["unknown_rc", "uncertain", "bad_json"])
def test_native_non_observed_is_not_automatic_api_permission(tmp_path, monkeypatch, outcome):
    rig = build_offline_rig(tmp_path, monkeypatch)
    _, check, finding, _ = three_observations(rig)
    provider, integration = attach_argon(rig, monkeypatch)
    calls = install_native_agy(rig, monkeypatch, outcome=outcome)
    _, audit = run_public(rig, finding, public_judge(rig))
    assert len(calls) == 1 and not provider.calls
    if outcome == "uncertain":
        # Admin round-2 rule (SUB-04 -> SUB-18): an uncertain Argon
        # hearing is a terminal Argon state, so after three distinct ordinary
        # observations the target reaches the existing no-gain editorial
        # terminal (typed authority, disclosed, apply still required). It is
        # still never permission for the API route.
        handoff = audit["contextual_terminal_handoff"]
        assert handoff["audio_resolution"] == "UNRESOLVED_BY_AUDIO" and handoff["apply_required"] is True
        assert handoff["material_exhaustion"]["argon"] == {"state": "TERMINAL", "status": "UNCERTAIN", "reason": None}
    else:
        assert audit["repaired"] is False
    assert audit["argon_escalation"]["status"] in {"UNKNOWN","UNCERTAIN","RESPONSE_REJECTED"}


def test_fresh_public_flow_acquires_three_then_upgrades_without_manual_history(tmp_path, monkeypatch):
    from src.autoslice import semantic_audio_recovery
    real_recover = semantic_audio_recovery.recover_semantic_audio_range
    trace = []
    def record_recovery(**kwargs):
        result, audit = real_recover(**kwargs)
        if audit:
            trace.append({k: audit.get(k) for k in ("status", "reason_code",
                "execution_status", "provider_calls_this_attempt",
                "new_effective_observations_this_attempt",
                "original_request_sha256", "current_request_sha256")})
            trace[-1]["before_count"] = (audit.get("observation_projection_before") or {}).get("effective_distinct_observations")
            trace[-1]["after_count"] = (audit.get("observation_projection_after") or {}).get("effective_distinct_observations")
            (rig.output / "fresh-public-recovery-trace.json").write_text(json.dumps(trace,ensure_ascii=False))
        return result, audit
    monkeypatch.setattr(semantic_audio_recovery,"recover_semantic_audio_range",record_recovery)
    real_validate = auditor.validate_range_acoustic_observation
    range_validation = []
    def record_validation(*args, **kwargs):
        value, reason = real_validate(*args, **kwargs)
        check = args[1] if len(args)>1 else kwargs["current_request"]
        row = args[2] if len(args)>2 else kwargs["observation"]
        if row.get("observation_class") == "CONTEXT_ASSISTED_AUDIO":
            range_validation.append({"accepted":value is not None,"reason":reason,
                "check_sealed":"request_sha256" in check,
                "prior_narrow_count":len(check.get("prior_acoustic_observations",[]))})
            (rig.output / "fresh-assisted-validation.json").write_text(json.dumps(range_validation))
        return value, reason
    monkeypatch.setattr(auditor,"validate_range_acoustic_observation",record_validation)
    rig = build_offline_rig(tmp_path, monkeypatch)
    rig.transport.pinyin = "wo ting dao le heng chang"
    provider, integration = attach_argon(rig, monkeypatch)
    calls = install_native_agy(rig, monkeypatch)
    judge = argon_judge(rig)
    output, audit = run_public(rig, FINDING, judge)
    (rig.output / "fresh-public-audit.json").write_text(json.dumps(audit,ensure_ascii=False))
    assert len(rig.transport.calls) == 3, {"audit":audit.get("reason_code"),"trace":trace}
    assert [r["class"] for r in rig.transport.calls] == [
        "CANDIDATE_BLIND_AUDIO", "CANDIDATE_BLIND_RANGE", "CONTEXT_ASSISTED_AUDIO"]
    assert len(calls) == 1 and not provider.calls
    assisted_cpa = [p for p in judge.prompts if "CONTEXT_ASSISTED_AUDIO" in p
                    and not p.startswith("# CPA 补听范围选择")
                    and "## 新增 Argon 独立盲听证据" not in p]
    assert assisted_cpa and all(token in assisted_cpa[-1]
                               for token in ("heard", "inferred", "uncertain"))
    assert audit["argon_escalation"]["status"] == "OBSERVED"
    assert audit["repaired"] is True
    assert output == SRT.replace("我听到了，呜呜", "我听到了，哼唱")


def test_existing_third_observation_is_consumed_before_any_argon_upgrade(tmp_path, monkeypatch):
    rig = build_offline_rig(tmp_path, monkeypatch)
    rig.transport.pinyin = "wo ting dao le wu wu"
    _, check, finding, _ = three_observations(rig)
    # The third raw exists, while this original gate has consumed only two.
    earlier = deepcopy(finding)
    earlier["exact_release_adjudication"] = finding["exact_release_adjudication"]["prior_audio_adjudication"]
    assert len(earlier["exact_release_adjudication"]["request"]["context_only_range_acoustic_observations"]) == 1
    assert len(earlier["_prior_range_acoustic_observations"]) == 2
    provider, integration = attach_argon(rig, monkeypatch)
    calls = install_native_agy(rig, monkeypatch)
    output, audit = run_public(rig, earlier, public_judge(rig, stop_after=3))
    assert output == SRT and audit["policy_branch"] == "JUDGE_KEEPS_CURRENT"
    assert len(rig.transport.calls) == 3 and not calls and not provider.calls
    assert not (integration.session(check, finding).root / "trigger.json").exists()


def test_native_dispatch_reservation_failure_cannot_start_process(tmp_path, monkeypatch):
    from src.autoslice.content_bound_request_ledger import ContentBoundRequestLedger
    rig = build_offline_rig(tmp_path, monkeypatch)
    _, check, finding, _ = three_observations(rig)
    provider, integration = attach_argon(rig, monkeypatch)
    calls = install_native_agy(rig, monkeypatch)
    transition = ContentBoundRequestLedger.transition
    def fail_reservation(self, state, *args, **kwargs):
        if state == "SUBMIT_DISPATCHED" and kwargs.get("event") == "ARGON_AGY_SEND_RESERVED":
            raise OSError("synthetic durable reservation failed")
        return transition(self, state, *args, **kwargs)
    monkeypatch.setattr(ContentBoundRequestLedger, "transition", fail_reservation)
    output, audit = run_public(rig, finding, public_judge(rig))
    assert output == SRT and not calls and not provider.calls
    assert audit["argon_escalation"]["status"] == "ADMISSION_PENDING"
    agy = next(r for r in audit["argon_escalation"]["accounting"]["histories"] if r["mode"] == "agy")
    assert agy["state"] == "PREPARED"
    assert audit["argon_escalation"]["dispatches_this_advance"] == 0


def test_argon_unresolved_no_gain_reaches_typed_editor_and_cold_current_review(tmp_path, monkeypatch):
    rig = build_offline_rig(tmp_path, monkeypatch)
    _, check, finding, _ = three_observations(rig)
    provider, integration = attach_argon(rig, monkeypatch)
    calls = install_native_agy(rig, monkeypatch)
    output, audit = run_public(rig, finding, argon_judge(rig, needs_audio=True, choice="NEITHER"))
    (rig.output / "argon-no-gain-audit.json").write_text(json.dumps(audit,ensure_ascii=False))
    assert len(calls) == 1 and not provider.calls and len(rig.transport.calls) == 3
    assert audit["contextual_terminal_handoff"]["apply_required"] is True, audit.get("semantic_audio_recovery")
    assert audit["prior_audio_adjudication"]["witness_judge"]["judge"]["needs_audio"] is True
    proof = audit["semantic_audio_recovery"]["no_gain_proof"]
    assert any(r["receipt_kind"] == "argon_native_transcript" for r in proof["projection"]["observations"])
    assert audit["semantic_audio_recovery"]["consumed_argon_acquisition"]
    assert output == SRT  # Existing finalizer must perform the actual typed edit.
    apply_save_and_cold_review(rig, [{**finding, "exact_release_adjudication":audit}])


def test_preexisting_native_output_is_not_a_new_response_or_api_permission(tmp_path, monkeypatch):
    from src.autoslice import argon_escalation_agy as native
    rig = build_offline_rig(tmp_path, monkeypatch)
    _, check, finding, _ = three_observations(rig)
    provider, integration = attach_argon(rig, monkeypatch)
    calls = install_native_agy(rig, monkeypatch)
    original_directory = native.store.directory
    stale = []
    def prepare_directory(path):
        value = original_directory(path)
        if Path(path).name == "agy-input":
            output = Path(path) / "hearing.json"
            output.write_text("synthetic stale response")
            output.chmod(0o600)
            stale.append(output)
        return value
    monkeypatch.setattr(native.store, "directory", prepare_directory)
    output, audit = run_public(rig, finding, public_judge(rig))
    assert output == SRT and not calls and not provider.calls
    assert audit["reason_code"] == "ARGON_INTEGRATION_REJECTED"
    assert audit["detail"] == "ARGON_AGY_PREEXISTING_RESPONSE"
    assert stale and stale[0].read_text() == "synthetic stale response"


def test_assisted_cpa_unknown_stays_on_original_issue_without_argon_or_relisten(tmp_path, monkeypatch):
    rig = build_offline_rig(tmp_path, monkeypatch)
    provider, integration = attach_argon(rig, monkeypatch)
    calls = install_native_agy(rig, monkeypatch)
    fallback = public_judge(rig)
    attempts, all_judge_inputs = [], []
    def judge(prompt):
        all_judge_inputs.append(hashlib.sha256(prompt.encode()).hexdigest())
        if (len(rig.transport.calls) == 3
                and rig.transport.calls[-1]["class"] == "CONTEXT_ASSISTED_AUDIO"
                and "CONTEXT_ASSISTED_AUDIO" in prompt
                and not prompt.startswith("# CPA 补听范围选择")):
            attempts.append(hashlib.sha256(prompt.encode()).hexdigest())
            raise ConnectionError("synthetic CPA sent; terminal response unknown")
        return fallback(prompt)
    judge.cpa_cache_identity = {"transport":"offline-protocol",
                               "models":["offline-assisted-unknown"],"effort":"high"}
    _, first = run_public(rig, FINDING, judge)
    assert len(attempts) == 1 and len(rig.transport.calls) == 3
    original_records = {}
    for path in (rig.tmp / "base/cache/semantic-cpa-attempts").glob("*/*.json"):
        row = json.loads(path.read_text())
        if row["status"] == "OUTCOME_UNKNOWN":
            original_records[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
    assert original_records
    before_judgments = len(all_judge_inputs)
    original_attempts = set(attempts)
    _, resumed = run_public(rig, FINDING, judge)
    (rig.output / "assisted-cpa-unknown-audit.json").write_text(json.dumps({
        "first":first, "resumed":resumed, "original_unknown_records":original_records,
        "all_judge_inputs":all_judge_inputs, "before_resume_judgments":before_judgments,
        "audio_calls":len(rig.transport.calls), "argon_calls":len(calls)}))
    assert len(attempts) == len(set(attempts)) and len(rig.transport.calls) == 3
    # Admin ruling (K22 per request, 41「不完整 STARTED 保留未知，不盲重发。」):
    # the UNKNOWN assisted request is never re-sent (attempts stays 1); other
    # routes of the same target may continue with new, different questions.
    new_inputs = all_judge_inputs[before_judgments:]
    assert not set(new_inputs) & original_attempts
    assert not calls and not provider.calls and resumed["repaired"] is False
    # The normal resume can stop at an earlier existing recorder gate; the
    # original UNKNOWN row, no resends and no false terminal are authoritative.
    assert not resumed.get("contextual_terminal_handoff")
    assert {path:hashlib.sha256(Path(path).read_bytes()).hexdigest()
            for path in original_records} == original_records


def test_argon_flow_assisted_observation_reaches_cpa_through_main_recorder_plan(tmp_path, monkeypatch):
    """Positive control for the integration decision to keep only main's planner.

    Argon's parallel with_assisted_consumption_plan was removed. The assisted
    range observation acquired on the Argon path must still be planned by
    main's recorder-history consumer and reach the actual CPA prompt.
    """
    from src.autoslice import semantic_audio_recovery, targeted_relisten
    real_plan = targeted_relisten.with_unconsumed_assisted_plan
    planned = []
    def record_plan(**kwargs):
        value = real_plan(**kwargs)
        planned.append({"input_plan": "continuation_plan" in kwargs["finding"], "output": deepcopy(value)})
        return value
    monkeypatch.setattr(targeted_relisten, "with_unconsumed_assisted_plan", record_plan)
    real_recover = semantic_audio_recovery.recover_semantic_audio_range
    recovered = []
    def record_recovery(**kwargs):
        result, audit = real_recover(**kwargs)
        recovered.append(deepcopy(result))
        return result, audit
    monkeypatch.setattr(semantic_audio_recovery, "recover_semantic_audio_range", record_recovery)
    rig = build_offline_rig(tmp_path, monkeypatch)
    rig.transport.pinyin = "wo ting dao le heng chang"
    provider, integration = attach_argon(rig, monkeypatch)
    calls = install_native_agy(rig, monkeypatch)
    judge = argon_judge(rig)
    output, audit = run_public(rig, FINDING, judge)
    assert [r["class"] for r in rig.transport.calls] == [
        "CANDIDATE_BLIND_AUDIO", "CANDIDATE_BLIND_RANGE", "CONTEXT_ASSISTED_AUDIO"]
    # No recovery result carries the removed Argon-side planner.
    assert not any((row.get("semantic_retry_evidence_basis") or {}).get("status")
                   == "VALIDATED_ASSISTED_OBSERVATION_NOT_IN_CURRENT_GATE" for row in recovered)
    plans = [row["output"] for row in planned if not row["input_plan"]
             and (row["output"].get("semantic_retry_evidence_basis") or {}).get("status")
             == "VALIDATED_ASSISTED_INFORMATION_NOT_IN_RECORDED_TARGET_REQUESTS"]
    assert plans, planned
    plan = plans[0]
    assert plan["continuation_plan"]["action"] == "expand_context"
    refs = plan["semantic_retry_evidence_basis"]["observations"]
    assert refs and all(ref["input_information"]["context_source_sha256s"] for ref in refs)
    # The plan reaches an actual CPA call through the durable recorder: the
    # recorded request carries main's plan, and its prompt carries the
    # assisted observation's heard/inferred/uncertain receipt.
    rows = [json.loads(path.read_text())
            for path in (rig.tmp / "base/cache/semantic-cpa-attempts").glob("*/*.json")]
    consumed = [row for row in rows
                if row["request"].get("continuation_plan") == plan["continuation_plan"]]
    assert consumed and any(row["status"] == "COMPLETED" for row in consumed), [
        (row["status"], row["request"].get("continuation_plan")) for row in rows]
    assert any("CONTEXT_ASSISTED_AUDIO" in row["request"]["prompt"]
               and all(token in row["request"]["prompt"] for token in ("heard", "inferred", "uncertain"))
               for row in consumed)
    assert len(calls) == 1 and not provider.calls
    assert audit["argon_escalation"]["status"] == "OBSERVED"
    assert audit["repaired"] is True
    assert output == SRT.replace("我听到了，呜呜", "我听到了，哼唱")


def test_resumed_terminal_uncertain_argon_reaches_the_same_no_gain_terminal(tmp_path, monkeypatch):
    """41 (SUB-04 -> SUB-18): an uncertain Argon hearing is a terminal Argon
    state. A later run that resumes the same retained task (trigger already
    registered) must continue to the existing no-gain/context closure exactly
    as the first run did, not report the finished task as pending (OCI3
    e9c50824 sister cue16: Argon UNCERTAIN -> ARGON_ESCALATION_PENDING_OR_UNRESOLVED).
    """
    rig = build_offline_rig(tmp_path, monkeypatch)
    _, check, finding, _ = three_observations(rig)
    provider, integration = attach_argon(rig, monkeypatch)
    calls = install_native_agy(rig, monkeypatch, outcome="uncertain")
    _, first = run_public(rig, finding, public_judge(rig))
    assert first["contextual_terminal_handoff"]["material_exhaustion"]["argon"]["status"] == "UNCERTAIN"
    assert (integration.session(check, finding).root / "trigger.json").exists()
    # A later run: new integration/session objects over the same retained task.
    _, integration = attach_argon(rig, monkeypatch, provider=provider)
    _, resumed = run_public(rig, finding, public_judge(rig))
    assert len(calls) == 1 and not provider.calls  # Terminal task is read, never re-sent.
    assert resumed.get("reason_code") != "ARGON_ESCALATION_PENDING_OR_UNRESOLVED"
    handoff = resumed["contextual_terminal_handoff"]
    assert handoff["audio_resolution"] == "UNRESOLVED_BY_AUDIO" and handoff["apply_required"] is True
    assert handoff["material_exhaustion"]["argon"]["state"] == "TERMINAL"
    assert handoff["material_exhaustion"]["argon"]["status"] == "UNCERTAIN"
    assert resumed["argon_escalation"]["status"] == "UNCERTAIN"


def test_argon_credential_failure_blocks_for_an_operator_instead_of_closing(tmp_path, monkeypatch):
    """Admin ruling (41 SUB-04 -> SUB-18): a 401/403 credential or
    configuration failure (or an INVALID_ARGUMENT request body) is not
    exhausted hearing. The target stays blocked with an infrastructure reason
    and never reaches the context closure; a later run does not re-send.
    """
    rig = build_offline_rig(tmp_path, monkeypatch)
    _, check, finding, _ = three_observations(rig)
    provider, integration = attach_argon(rig, monkeypatch)
    provider.post_status = 401
    _, first = run_public(rig, finding, public_judge(rig))
    recovery_audit = first["semantic_audio_recovery"]
    assert recovery_audit["reason_code"] == "ARGON_INFRASTRUCTURE_BLOCKED"
    assert recovery_audit["argon_state"]["state"] == "INFRASTRUCTURE_BLOCKED"
    assert recovery_audit["infrastructure_reason"] == "SUBMIT_HTTP_REJECTED"
    assert "contextual_terminal_handoff" not in first and first["repaired"] is False
    _, integration = attach_argon(rig, monkeypatch, provider=provider)
    _, later = run_public(rig, finding, public_judge(rig))
    assert later["reason_code"] == "ARGON_INFRASTRUCTURE_BLOCKED" and later["operator_action_required"] is True
    assert "contextual_terminal_handoff" not in later
    assert len([c for c in provider.calls if c["method"] == "POST"]) == 1  # Never re-sent.


def test_every_route_refusing_with_zero_inference_is_structural_and_may_close(tmp_path, monkeypatch):
    """Admin ruling every route definitively refusing with zero
    inference is structural unavailability (reason recorded); the 41 no-gain
    closure is then reachable like any Argon terminal state."""
    from src.autoslice import argon_escalation_agy as native
    from src.autoslice.argon_escalation_integration import build_argon_integration
    rig = build_offline_rig(tmp_path, monkeypatch)
    _, check, finding, _ = three_observations(rig)
    monkeypatch.setenv("AUTOSLICE_PROVIDER_CONCURRENCY", "1")
    monkeypatch.setenv("GEMINI_PAID_BACKUP_DEV_EXCEPTION", "0")
    monkeypatch.setattr(native, "availability", lambda: (str(rig.tmp / "absent-agy"), False))
    provider = NativeProvider()
    provider.post_status = 429  # The real tier table closes free; the paid route refuses too.
    rig.verifier.argon_escalation = build_argon_integration(
        config={"enabled": True, "allow_paid": True}, source_media=rig.source, source_duration_ms=10000,
        ordinary_output_dir=rig.output, ordinary_verifier=rig.local, runtime_root=rig.tmp / "base",
        credential_loader=lambda: {"GEMINI_API_KEY": "fictional-free-one", "GEMINI_KEY_BACKUP": "fictional-paid"},
        transport_factory=provider.factory)
    _, audit = run_public(rig, finding, public_judge(rig))
    posts = [c for c in provider.calls if c["method"] == "POST"]
    assert [c["route"]["accepted_key_tier"] for c in posts] == ["paid_backup"]
    argon = audit["semantic_audio_recovery"]["argon_escalation"]
    assert argon["status"] == "FAILED" and argon["route_refusal"]["class"] == "ROUTE_UNSERVABLE"
    handoff = audit["contextual_terminal_handoff"]
    assert handoff["material_exhaustion"]["argon"]["state"] == "STRUCTURALLY_UNAVAILABLE"
    assert handoff["audio_resolution"] == "UNRESOLVED_BY_AUDIO" and handoff["apply_required"] is True


def _inrun_retrieval_scene(tmp_path, monkeypatch, *, done_at_get):
    """A batch this exact-final round dispatched; GET n returns SUCCEEDED (None: never)."""
    rig = build_offline_rig(tmp_path, monkeypatch)
    _, check, finding, _ = three_observations(rig)
    provider, integration = attach_argon(rig, monkeypatch)
    gets, real = [], provider.request

    def request(method, path, body, *, route, timeout_s):
        if method != "POST":
            gets.append(path)
            provider.remote_state = ("BATCH_STATE_SUCCEEDED" if done_at_get and len(gets) >= done_at_get
                                     else "BATCH_STATE_RUNNING")
        return real(method, path, body, route=route, timeout_s=timeout_s)
    provider.request = request
    clock, sleeps = [0.0], []

    def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds
    monkeypatch.setattr(auditor, "_argon_retrieval_clock", lambda: clock[0])
    monkeypatch.setattr(auditor, "_argon_retrieval_sleep", sleep)
    return rig, finding, provider, gets, sleeps


def test_inrun_argon_retrieval_closes_a_batch_finished_at_the_second_get(tmp_path, monkeypatch, capsys):
    """Admin ruling (Argon plan A): after the round, a batch this
    run dispatched is read back (one GET per 60 s, at most 6 min, no new
    inference); once retrieved, the finding is adjudicated again on the
    normal path within the same run."""
    rig, finding, provider, gets, sleeps = _inrun_retrieval_scene(tmp_path, monkeypatch, done_at_get=2)
    unresolved, resolved = auditor.adjudicate_exact_release_findings(
        SRT, [deepcopy(finding)], entity_verifier=rig.verifier, clip_context=rig.context,
        judge_llm_call=argon_judge(rig, needs_audio=True, choice="NEITHER"))
    rows = [*unresolved, *resolved]
    assert len(rows) == 1
    audit = rows[0]["exact_release_adjudication"]
    # Left PENDING within this run: the retained hearing came back and the
    # finding was judged again on the normal path (what the next run would do).
    assert audit.get("reason_code") != "ARGON_ESCALATION_PENDING_OR_UNRESOLVED"
    assert audit["argon_escalation"]["status"] == "OBSERVED"
    assert audit["argon_escalation"]["dispatches_this_advance"] == 0
    assert audit["argon_escalation"]["accounting"]["inference_reservations"] == 1
    assert sleeps == [auditor.ARGON_INRUN_RETRIEVAL_POLL_S] * 2 and len(gets) == 2
    assert len([c for c in provider.calls if c["method"] == "POST"]) == 1  # No new inference.
    log = capsys.readouterr().err
    assert "START exact_final_review.argon_inrun_retrieval" in log and "max_wait_s=360" in log
    assert "RESULT exact_final_review.argon_inrun_retrieval" in log and "retrieved=1" in log


def test_inrun_argon_retrieval_leaves_an_unfinished_batch_pending_without_new_inference(tmp_path, monkeypatch):
    rig, finding, provider, gets, sleeps = _inrun_retrieval_scene(tmp_path, monkeypatch, done_at_get=None)
    unresolved, resolved = auditor.adjudicate_exact_release_findings(
        SRT, [deepcopy(finding)], entity_verifier=rig.verifier, clip_context=rig.context,
        judge_llm_call=argon_judge(rig, needs_audio=True, choice="NEITHER"))
    assert not resolved and len(unresolved) == 1
    audit = unresolved[0]["exact_release_adjudication"]
    assert audit["reason_code"] == "ARGON_ESCALATION_PENDING_OR_UNRESOLVED"
    assert audit["argon_escalation"]["status"] == "PENDING"
    assert sum(sleeps) <= auditor.ARGON_INRUN_RETRIEVAL_MAX_WAIT_S
    assert sleeps == [auditor.ARGON_INRUN_RETRIEVAL_POLL_S] * 6 and len(gets) == 6
    assert len([c for c in provider.calls if c["method"] == "POST"]) == 1  # No new inference.
    assert audit["argon_escalation"]["accounting"]["inference_reservations"] == 1
