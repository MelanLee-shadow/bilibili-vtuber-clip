"""Offline complete-protocol acceptance for the normal relisten transport.

The WAV is synthetic and every model response is an explicit protocol stub.
These tests prove wiring, receipt consumption and guards, not listening quality.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
from types import SimpleNamespace
import wave

import pytest

from src.autoslice import context_assisted_audio as assisted
from src.autoslice import entity_audio_verifier as audio
from src.autoslice import final_review_auditor as auditor
from src.autoslice import semantic_audio_recovery as recovery
from src.autoslice import targeted_relisten as policy
from src.autoslice.acoustic_witness_adjudication import build_witness_request, judge_word_choice
from src.autoslice.native_context_witness import build_independent_context_witnesses

SRT = ("1\n00:00:00,500 --> 00:00:02,000\n豆沙问刚才是什么声音\n\n"
       "2\n00:00:02,000 --> 00:00:04,000\n我听到了，呜呜\n\n"
       "3\n00:00:04,000 --> 00:00:07,000\n刚才只是在哼唱，豆沙知道\n\n"
       "4\n00:00:07,000 --> 00:00:09,000\n问的是前半句还是尾音\n")
FINDING = {"cue_index": 2, "suspect": "呜呜", "suggestion": "哼唱",
           "proposed_full_cue": "我听到了，哼唱", "repair_class": "spoken_unit",
           "content_risk": "factual_error",
           "risk_basis": "把实际声音类型写成另一种声音会改变该句所指事实。"}

def seal_request(request):
    request = {k: v for k, v in request.items() if k != "request_sha256"}
    return {**request, "request_sha256": policy.digest(request)}

def planner_data(prompt):
    return next(json.loads(line) for line in prompt.splitlines()
                if line.startswith("{") and '"already_heard_ranges"' in line)

def observe_plan(first=1, last=2, *, mode="CANDIDATE_BLIND_AUDIO", purpose="FULL_QA", **extra):
    return {"decision": "OBSERVE", "provider": "gemini", "first_cue": first, "last_cue": last,
            "observation_class": mode, "purpose": purpose,
            "reason": "把提问与回答作为一块原音，区分句首遗漏和目标尾音。",
            "question": "目标前半句是否完整，尾部是词语还是无词义哼唱？", **extra}

def assisted_plan(first=1, last=2, refs=None, **extra):
    return observe_plan(first, last, mode=assisted.OBSERVATION_CLASS, purpose="SOURCED_CONTEXT",
        context_refs=refs or ["cue:1", "cue:3"], context_summary="相邻提问和回答讨论刚才的声音。",
        proper_names=[{"text": "豆沙", "source_ref": "cue:1"}], include_candidates=True, **extra)

def no_gain_plan(data):
    ids = [row["observation_id"] for row in data["already_heard_ranges"]]
    return {"decision": "NO_NEW_AUDIO", "reason": "音频保留前半句，尾部缺少可辨词音；已有完整问答与来源语境。",
            "no_gain": {"observation_ids": ids, "remaining_uncertainty": "尾部呜呜的字音不可辨。",
                "why_audio_cannot_discriminate": "已听完整问答和相邻回应，尾部仍只有持续发声，无新增词界可提取。",
                "alternatives": [
                    {"purpose": "WORD_BOUNDARY", "reason": "完整块覆盖尾音起止，重切没有缺失的词界。", "observation_ids": ids},
                    {"purpose": "FULL_QA", "reason": "提问与回答已在原音中覆盖，重复不增加可辨音节。", "observation_ids": ids}],
                "context_refs": ["cue:1", "cue:3"]}}

class OfflineTransport:
    def __init__(self):
        self.calls = []
        self.pinyin = "wo ting dao le ? ?"
        self.range_transcript = "完整提问。我听到了。"
        self.failure = None

    def __call__(self, argv, *, cwd, env, timeout, response_validator=None):
        directory = Path(cwd)
        clip = directory / "input.mp4"
        prompt = (directory / "prompt.md").read_text()
        # Intercept the real normal transport, after media preparation and
        # canonical dispatch admission, before any process or network send.
        streams = json.loads(subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type",
             "-of", "json", str(clip)], text=True))["streams"]
        assert any(row["codec_type"] == "audio" for row in streams)
        assert any("prompt.md" in str(arg) and "input.mp4" in str(arg) for arg in argv)
        call = {"audio_path": str(clip), "audio_sha256": audio._sha256(clip),
                "prompt_path": str(directory / "prompt.md"), "prompt": prompt,
                "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                "argv": list(map(str, argv)), "offline": True}
        self.calls.append(call)
        if self.failure:
            raise self.failure
        if "# Raw-audio dictation witness" in prompt:
            assert "豆沙" not in prompt and "呜呜" not in prompt and "哼唱" not in prompt
            tokens = self.pinyin.split()
            raw = {"schema_version": "subtitle-span-acoustic-witness.v1", "status": "OBSERVED",
                "target_audible": True, "heard_pinyin": self.pinyin,
                "uncertain_positions": [i for i, token in enumerate(tokens) if token == "?"],
                "syllable_count": len(tokens), "confidence": .72, "reason": "clear onset with uncertain trailing sounds"}
            call["class"] = "CANDIDATE_BLIND_AUDIO"
        elif "CONTEXT_ASSISTED_AUDIO" in prompt:
            data = next(json.loads(line) for line in prompt.splitlines() if line.startswith("{"))
            duration = data["audio_duration_ms"]
            refs = list(data["assistance"]["sources"])
            raw = {"schema_version": "context-assisted-audio-response.v1", "status": "UNCERTAIN",
                "target_audible": True, "heard": "我听到了", "full_transcript": "我听到了",
                "inferred": [{"text": "尾部可能是哼唱", "source_refs": refs}],
                "uncertain": [{"question": "尾音是否有词义", "audio_basis": "持续声不构成清楚词音"}],
                "segments": [{"segment": "complete_block", "start_ms": 0, "end_ms": duration,
                              "transcript": "我听到了"}], "audio_observations": []}
            call["class"] = assisted.OBSERVATION_CLASS
        else:
            duration = int(re.search(r"duration is (\d+) ms", prompt).group(1))
            assert "豆沙" not in prompt and "呜呜" not in prompt and "哼唱" not in prompt
            raw = {"status": "OBSERVED", "target_audible": True, "full_transcript": self.range_transcript,
                   "segments": [{"segment": "complete_block", "start_ms": 0, "end_ms": duration,
                                 "transcript": self.range_transcript}]}
            call["class"] = "CANDIDATE_BLIND_RANGE"
        raw_text = json.dumps(raw, ensure_ascii=False)
        (directory / "verdict.json").write_text(raw_text)
        call["raw_sha256"] = hashlib.sha256(raw_text.encode()).hexdigest()
        return SimpleNamespace(completed=subprocess.CompletedProcess(argv, 0, "offline protocol fixture", ""),
                               failure_category=None, launch_error_type=None)

@pytest.fixture
def rig(tmp_path, monkeypatch, request):
    artifact_root = os.environ.get("AUTOSLICE_RELISTEN_TEST_ARTIFACTS")
    if artifact_root:
        tmp_path = Path(artifact_root) / hashlib.sha256(request.node.nodeid.encode()).hexdigest()[:16]
        tmp_path.mkdir(parents=True, exist_ok=False)
    return build_offline_rig(tmp_path, monkeypatch)

def build_offline_rig(tmp_path, monkeypatch, *, srt=SRT, finding=FINDING,
                      offset_ms=0, source_duration_ms=10000):
    """Synthetic media with complete raw replies at the normal send boundary."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path / "base"))
    monkeypatch.delenv(audio.ENTITY_AUDIO_DISABLE_AGY_ENV, raising=False)
    source = tmp_path / "synthetic-source.wav"
    with wave.open(str(source), "wb") as out:
        out.setnchannels(1); out.setsampwidth(2); out.setframerate(16000)
        out.writeframes(b"".join(struct.pack("<h", int(2500 * math.sin(i / 17)))
                                for i in range(source_duration_ms * 16)))
    output = tmp_path / "out"
    output.mkdir()
    local = audio._LocalAudioVerifier(
        source_media=source, output_dir=output, recording_date="2026-10-19",
        source_duration_ms=source_duration_ms, source_sha256=audio._sha256(source),
        source_stat_binding=audio._source_stat_binding(source), binary="agy",
        model="offline-protocol-model", timeout="1s", timely_context="")
    verifier = build_independent_context_witnesses(fallback=local, source_media=source,
                                                  output_dir=output, providers=())
    transport = OfflineTransport()
    monkeypatch.setattr(audio.agy_gemini_client, "local_agy_available", lambda _: True)
    monkeypatch.setattr(audio.agy_gemini_client, "run_local_agy", transport)
    monkeypatch.setattr(audio, "agy_subprocess_env", lambda: {})
    monkeypatch.setattr(audio, "_run_gemini_web_if_enabled", lambda *a, **k: None)
    def no_api(**kwargs):
        raise AssertionError("OFFLINE_UNEXPECTED_API_FALLBACK")
    monkeypatch.setattr(audio, "_run_gemini_api_fallback", no_api)
    from src.autoslice.clip_context import build_clip_context
    from src.autoslice.speech_memory_ledger import SCHEMA_VERSION as MEMORY_SCHEMA
    memory = tmp_path / "empty-speech-memory.json"
    memory.write_text(json.dumps({"schema_version": MEMORY_SCHEMA, "entries": []}))
    context = build_clip_context(
        candidate_id="synthetic-protocol-case",
        spec={"date": "2026-10-19", "pieces": [{"start_ms": 0,
              "end_ms": source_duration_ms, "source_media_sha256": "sha256:" + local.source_sha256}]},
        draft_srt=srt, authoritative_chat=(), topic_resolution={}, session_topic_authorities=(),
        speech_memory_ledger_path=memory, source_truth_ledger_path=None)
    check = auditor.build_context_adjudication_request(srt, finding, clip_context=context,
        source_media_timeline_offset_ms=offset_ms)
    return SimpleNamespace(source=source, output=output, context=context, check=check,
                           local=local, verifier=verifier, transport=transport, tmp=tmp_path,
                           srt=srt, finding=deepcopy(finding), offset_ms=offset_ms)

def acquire_narrow(rig, check=None):
    check = check or rig.check
    witness = rig.verifier(build_witness_request(check))
    assert witness["status"] == "OBSERVED", witness
    return witness

def pending_finding(rig, check, witness, previous=None):
    raw = json.dumps({"choice": "CURRENT", "needs_audio": True,
        "reason": "前半句清楚，尾音仍缺少完整问答音频。",
        "current_utterance_supported": False,
        "current_utterance_support_reason": "尾音不可凭拼写下结论。"}, ensure_ascii=False)
    judgment = judge_word_choice(llm_call=lambda _: raw, check_request=check, witness=witness,
                                context_before=check.get("context_before", ""),
                                context_after=check.get("context_after", ""))
    assert judgment["needs_audio"] is True, judgment
    prior = {"schema_version": "subtitle-span-adjudication.v1", "status": "OBSERVED",
        "repaired": False, "timing_immutable": True, "decision_authority": "CPA_JUDGE",
        "witness_authority": "EVIDENCE_ONLY", "policy_branch": "JUDGE_REQUESTS_MORE_AUDIO",
        "request": check, "verdict": witness, "witness_judge": {"judge": judgment}}
    if previous:
        prior["prior_audio_adjudication"] = previous
    return {**deepcopy(rig.finding), "exact_release_adjudication": prior,
            "_prior_range_acoustic_observations": list(check.get("context_only_range_acoustic_observations", []))}

def acquire_range(rig, finding, check, plan):
    calls = []
    def judge(prompt):
        calls.append(prompt)
        return json.dumps(plan(planner_data(prompt)) if callable(plan) else plan, ensure_ascii=False)
    output, audit = recovery.recover_semantic_audio_range(
        srt_text=rig.srt, finding=finding, check_request=check,
        entity_verifier=rig.verifier, llm_call=judge)
    return output, audit, calls

def three_observations(rig):
    witness = acquire_narrow(rig)
    check = rig.check
    finding = pending_finding(rig, check, witness)
    output, first, _ = acquire_range(rig, finding, check, observe_plan())
    assert first["status"] == "OBSERVED", first.get("reason_code")
    check = auditor.build_context_adjudication_request(SRT, output, clip_context=rig.context)
    finding = pending_finding(rig, check, witness, previous=finding["exact_release_adjudication"])
    finding["exact_release_adjudication"]["semantic_audio_recovery"] = first
    output, second, _ = acquire_range(rig, finding, check, assisted_plan())
    assert second["status"] == "OBSERVED", second.get("reason_code")
    check = auditor.build_context_adjudication_request(SRT, output, clip_context=rig.context)
    finding = pending_finding(rig, check, witness, previous=finding["exact_release_adjudication"])
    finding["exact_release_adjudication"]["semantic_audio_recovery"] = second
    return witness, check, finding, [first, second]

def test_normal_transport_narrow_reopens_audio_prompt_and_raw(rig):
    witness = acquire_narrow(rig)
    projection = policy.evidence_projection(rig.check, witness=witness)
    assert projection["effective_distinct_observations"] == 1
    assert len(rig.transport.calls) == 1
    from src.autoslice.entity_witness_cache_probe import validate_relisten_witness_artifacts
    validate_relisten_witness_artifacts(request=build_witness_request(rig.check), witness=witness)
    Path(witness["relisten_artifacts"]["response_path"]).write_text("{}")
    after = policy.evidence_projection(rig.check, witness=witness)
    assert after["effective_distinct_observations"] == 0
    assert "RELISTEN_NARROW_RESPONSE_DRIFT" in str(after["rejected_observations"])

def test_three_distinct_audio_inputs_and_assisted_material_reach_normal_transport(rig):
    witness, check, finding, audits = three_observations(rig)
    assert len(rig.transport.calls) == 3
    assert [row["class"] for row in rig.transport.calls] == [
        "CANDIDATE_BLIND_AUDIO", "CANDIDATE_BLIND_RANGE", assisted.OBSERVATION_CLASS]
    assert [row["new_effective_observations_this_attempt"] for row in audits] == [1, 1]
    assert audits[-1]["observation_projection_after"]["effective_distinct_observations"] == 3
    prompt = rig.transport.calls[-1]["prompt"]
    assert "豆沙" in prompt  # Content is passed, not merely source IDs.
    assert "呜呜" in prompt and "哼唱" in prompt
    assert "actual audio takes precedence" in prompt and "Mark uncertainty" in prompt
    assisted_row = check["context_only_range_acoustic_observations"][-1]
    assert assisted_row["independent_witness"] is False
    assert assisted_row["audio_partition"]["inferred"]
    assert len({row["prompt_sha256"] for row in rig.transport.calls}) == 3

def test_no_gain_proof_is_separate_from_original_needs_audio_and_reopens_files(rig):
    witness, check, finding, audits = three_observations(rig)
    original = deepcopy(finding)
    _, audit, _ = acquire_range(rig, finding, check, no_gain_plan)
    assert audit["status"] == "CONTEXT_REVIEW", audit.get("reason_code")
    assert len(rig.transport.calls) == 3 and audit["provider_calls_this_attempt"] == 0
    assert finding == original
    proof = audit["no_gain_proof"]
    policy.validate_no_gain(proof, audit["terminal_check_request"])
    altered = deepcopy(proof)
    altered["projection"]["effective_distinct_observations"] += 1
    altered["proof_sha256"] = policy.digest({k: v for k, v in altered.items() if k != "proof_sha256"})
    with pytest.raises(ValueError, match="RELISTEN_NO_GAIN_OBSERVATION_DRIFT"):
        policy.validate_no_gain(altered, audit["terminal_check_request"])
    Path(witness["relisten_artifacts"]["response_path"]).write_text("{}")
    with pytest.raises(ValueError, match="RELISTEN_NO_GAIN_OBSERVATION_DRIFT"):
        policy.validate_no_gain(proof, audit["terminal_check_request"])

def test_partial_clear_audio_cannot_be_changed_by_context(rig):
    witness = acquire_narrow(rig)
    policy.protect_clear_audio(rig.check, witness, "我听到了，哼唱")
    with pytest.raises(ValueError, match="EDITORIAL_CLEAR_AUDIO_CONFLICT"):
        policy.protect_clear_audio(rig.check, witness, "你听到了，哼唱")

def test_fully_clear_audio_cannot_be_changed_by_context(rig):
    rig.transport.pinyin = "wo ting dao le wu wu"
    witness = acquire_narrow(rig)
    with pytest.raises(ValueError, match="EDITORIAL_CLEAR_AUDIO_CONFLICT"):
        policy.protect_clear_audio(rig.check, witness, "我听到了，哼唱")

def test_assisted_first_request_is_rejected_before_transport(rig):
    finding = pending_finding(rig, rig.check, {
        "schema_version": "subtitle-span-acoustic-witness.v1", "status": "UNCERTAIN",
        "witness_protocol": "blind_pinyin", "request_sha256": build_witness_request(rig.check)["request_sha256"],
        "reason_code": "NO_WITNESS"})
    _, audit, _ = acquire_range(rig, finding, rig.check, assisted_plan())
    assert audit["status"] == "BLOCK"
    assert audit["reason_code"] == "ASSISTED_FIRST_OBSERVATION_MUST_BE_BLIND"
    assert not rig.transport.calls


def editorial_answer():
    return {"status": "RESOLVED", "mode": "faithful_paraphrase",
            "edit_start": 5, "edit_end": 7, "replacement": "哼唱",
            "reliable_fragments": [{"text": "我听到了", "basis": "盲听前四个音节清楚"}],
            "evidence_refs": ["audio", "context_after"], "meaning_basis": "相邻回应明确讨论哼唱，保留清楚前半句。",
            "no_new_facts": True, "literal_uncertainty": "尾部原字不确定，按已有语境表达声音类型。",
            "reason": "显式语境推断，只处理尾部不清楚的呈现。"}

def public_judge(rig, *, stop_after=None, terminal_keep=False):
    prompts = []
    def judge(prompt):
        prompts.append(prompt)
        if prompt.startswith('# 字幕材料穷尽终裁 v1'):
            assert '"needs_audio"' not in prompt
            if terminal_keep:
                return json.dumps({'choice': 'CURRENT', 'grade': 'C', 'keep_route': 'C_MEANING_EQUIVALENT', 'evidence_decisive': False,
                    'context_can_decide': True, 'reason': '尾音写法不影响完整语境中的所指。',
                    'comparison_text': '我听到了，呜呜', 'evidence_refs': ['audio', 'context_after'],
                    'meaning_equivalence': {'status': 'EQUIVALENT', 'current_meaning': '听到了声音',
                        'alternative_meaning': '听到了哼唱般的声音', 'shared_meaning': '听到了那个声音',
                        'uncertain_detail': '尾音字形', 'context_basis': '后一句解释哼唱。', 'evidence_cue_ids': [2, 3]}}, ensure_ascii=False)
            return json.dumps({**editorial_answer(), 'choice': 'CONTEXT_FIT', 'grade': 'B',
                'evidence_decisive': True, 'context_can_decide': True,
                'comparison_text': '我听到了，哼唱'}, ensure_ascii=False)
        if prompt.startswith("# CPA 补听范围选择"):
            data = planner_data(prompt)
            count = data["effective_distinct_observations"]
            plan = observe_plan() if count < 2 else assisted_plan() if count < 3 else no_gain_plan(data)
            return json.dumps(plan, ensure_ascii=False)
        if '"audio_gain_termination"' in prompt and '"reliable_fragments"' in prompt:
            return json.dumps(editorial_answer(), ensure_ascii=False)
        done = stop_after is not None and len(rig.transport.calls) >= stop_after
        reply = {"choice": "CURRENT", "needs_audio": not done,
            "reason": "已听原音支持当前整句。" if done else "尾部未决，需不同语义范围的实际音频。",
            "current_utterance_supported": done,
            "current_utterance_support_reason": "全部原音与当前文字相容。" if done else "尾音不足。"}
        if "SPAN_REVIEW_INPUT" in prompt:
            data = json.loads(prompt.split("SPAN_REVIEW_INPUT\n", 1)[1]
                              .split("\nEND_SPAN_REVIEW_INPUT", 1)[0])
            binding = data["binding"]
            reply["span_review"] = {
                "binding_sha256": binding["binding_sha256"], "request_sha256": data["request_sha256"],
                "context_basis": "仅核对当前目标尾部；清楚前半句保持原话。",
                "all_audio_questions_located": done, "target_needs_audio": not done,
                "alignments": [
                    {"observation_id": row["observation_id"], "span": [4, 6],
                     "quote": " ".join(row["text"].split()[4:6])}
                    for row in data["observations"] if row["kind"] == "blind_pinyin"],
                "audio_questions": [] if done else [
                    {"codepoint_span": binding["codepoint_span"], "reason": "尾音仍不确定。",
                     "observation_ids": [row["observation_id"] for row in data["observations"]]}]}
            if not done:
                reply["span_review"].update(status="UNLOCATABLE", unlocatable_reason="目标尾部原音无法定位成可靠词音。")
        return json.dumps(reply, ensure_ascii=False)
    judge.prompts = prompts
    return judge

@pytest.mark.parametrize("stop_after", [1, 2])
def test_public_entry_stops_when_resolved_after_one_or_two_audio_observations(rig, stop_after):
    rig.transport.pinyin = "wo ting dao le wu wu"
    judge = public_judge(rig, stop_after=stop_after)
    output, audit = auditor.adjudicate_context_finding(
        SRT, deepcopy(FINDING), entity_verifier=rig.verifier, clip_context=rig.context,
        judge_llm_call=judge, require_complete_utterance_support=True)
    assert len(rig.transport.calls) == stop_after, (audit.get("reason_code"), audit.get("policy_branch"))
    assert output == SRT and audit["repaired"] is False
    assert audit["policy_branch"] == "JUDGE_KEEPS_CURRENT", audit.get("reason_code")
    assert len([p for p in judge.prompts if p.startswith("# CPA 补听范围选择")]) == stop_after - 1

def test_public_entry_no_gain_returns_existing_typed_editorial_authority(rig):
    judge = public_judge(rig)
    unresolved, resolved = auditor.adjudicate_exact_release_findings(
        SRT, [deepcopy(FINDING)], entity_verifier=rig.verifier, clip_context=rig.context,
        judge_llm_call=judge)
    assert not resolved and len(unresolved) == 1
    audit = unresolved[0]["exact_release_adjudication"]
    (rig.output / "public-audit.json").write_text(json.dumps(audit, ensure_ascii=False))
    assert audit.get("repaired") is True, (audit.get("reason_code"), audit.get("policy_branch"),
        audit.get("semantic_audio_recovery", {}).get("reason_code"),
        audit.get("semantic_audio_recovery", {}).get("terminal_reason_code"))
    assert audit["contextual_terminal_handoff"]["status"] == "TYPED_EDITORIAL_AUTHORITY"
    assert audit["semantic_audio_recovery"]["status"] == "CONTEXT_REVIEW"
    assert len(rig.transport.calls) == 3
    assert audit["contextual_terminal_handoff"]["literal_transcription"] is False
    apply_save_and_cold_review(rig, unresolved)


def apply_save_and_cold_review(rig, unresolved, *, expected_literal_transcription=False,
                               correction_control="historical_invalid"):
    """Use normal application/durable consumers; keep the historical negative."""
    assert correction_control in {"historical_invalid", "normal_generated"}
    from src.autoslice.producer_package_finalization import (
        _apply_exact_final_cpa_repairs, _register_exact_final_cpa_repairs)
    from src.autoslice.exact_final_cpa_history import build_run_audit
    from src.autoslice.final_review_contract import _validate_exact_final_cpa_self_heal
    def sha(value):
        return "sha256:" + hashlib.sha256(value.encode()).hexdigest()
    output, repairs = _apply_exact_final_cpa_repairs(
        rig.srt, {"findings": unresolved}, clip_context=rig.context)
    assert output == rig.srt.replace("我听到了，呜呜", "我听到了，哼唱")
    assert len(repairs) == 1
    if expected_literal_transcription is not None:
        assert repairs[0]["literal_transcription"] is expected_literal_transcription
    assert [line for line in output.splitlines() if "-->" in line] == [
        line for line in rig.srt.splitlines() if "-->" in line]
    passes = [{"schema_version": "exact-final-cpa-self-heal-pass.v1", "pass_index": 1,
               "input_srt_sha256": sha(rig.srt), "output_srt_sha256": sha(output), "repairs": repairs}]
    authority = {"exact_final_cpa_self_heal": build_run_audit(passes, [])}
    _register_exact_final_cpa_repairs(authority, repairs, delivery_start_ms=rig.offset_ms)
    _validate_exact_final_cpa_self_heal(authority, expected_srt_sha256=sha(output), allow_pending=True)
    with pytest.raises(ValueError):
        _validate_exact_final_cpa_self_heal(authority, expected_srt_sha256=sha(output))
    (rig.output / "saved.srt").write_text(output)
    (rig.output / "saved-authority.json").write_text(json.dumps(authority, ensure_ascii=False))
    (rig.output / "saved-context.json").write_text(json.dumps(rig.context, ensure_ascii=False))
    script = r'''
import hashlib, json, os, pathlib, sys
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch
def deny_network(event, args):
    if event in {"socket.connect", "socket.connect_ex", "socket.getaddrinfo"}:
        raise RuntimeError("OFFLINE_COLD_NETWORK_FORBIDDEN")
    if event == "subprocess.Popen" and pathlib.Path(str(args[0])).name in {
            "agy", "curl", "wget", "ssh", "gemini", "codex", "claude"}:
        raise RuntimeError("OFFLINE_COLD_PROVIDER_PROCESS_FORBIDDEN")
sys.addaudithook(deny_network)
from src.autoslice.final_review_contract import (
    _validate_exact_final_cpa_self_heal, validate_final_review_release)
from src.autoslice.producer_text_pipeline import (
    _continuation_validate_exact_final_owner, _run_exact_final_release_review)
root = pathlib.Path(sys.argv[1])
srt = (root / "saved.srt").read_text()
authority = json.loads((root / "saved-authority.json").read_text())
context = json.loads((root / "saved-context.json").read_text())
current_sha = "sha256:" + hashlib.sha256(srt.encode()).hexdigest()
_validate_exact_final_cpa_self_heal(authority, expected_srt_sha256=current_sha, allow_pending=True)
_continuation_validate_exact_final_owner(authority["entity_repairs"][0],
                                        row_index=0, chat_authority=authority)
prompts = []
def text_reply(prompt):
    prompts.append(prompt)
    return '{"findings": []}'
def no_audio(*args, **kwargs):
    raise AssertionError("COLD_REVIEW_MUST_NOT_DISPATCH_AUDIO")
assert len(authority["entity_repairs"]) == 1
assert os.getpid() != int(sys.argv[3])
control = sys.argv[2]
correction = {"applied_count": 0, "findings": []}
correction_sha = None
if control == "normal_generated":
    from src.autoslice import producer_text_pipeline as pipeline
    from src.autoslice.final_review_auditor import audit_correction_mutation_authority
    with patch.object(pipeline, "_build_final_review_llm_call", return_value=text_reply):
        corrected, correction = pipeline._run_final_review(
            srt_text=srt, chat_authority_audit=deepcopy(authority),
            handled_entity_cues=set(), verify_confusable_entity=no_audio,
            adapters=SimpleNamespace(review_glossary=lambda: ""),
            authoritative_chat=(), clip_context=context,
            source_media_sha256=context["pieces"][0]["source_media_sha256"])
    assert corrected == srt
    assert correction["status"] == "CLEAN" and correction["applied_count"] == 0
    assert correction["findings"] == []
    assert "boundary_semantic_review" not in correction
    raw_correction = json.dumps(correction, ensure_ascii=False).encode()
    (root / "cold-generated-correction.json").write_bytes(raw_correction)
    correction = json.loads((root / "cold-generated-correction.json").read_bytes())
    correction_sha = "sha256:" + hashlib.sha256(raw_correction).hexdigest()
    mutation = audit_correction_mutation_authority(correction)
    assert mutation["status"] == "PASS" and mutation["failures"] == []
    assert mutation["validated_mutation_count"] == 0
    _continuation_validate_exact_final_owner(authority["entity_repairs"][0],
                                            row_index=0, chat_authority=authority)
review = _run_exact_final_release_review(
    srt_text=srt, correction_audit=correction,
    adapters=SimpleNamespace(review_glossary=lambda: ""),
    authoritative_chat=(), selection_hook="", clip_context=context,
    verified_authority_audit=authority, verify_confusable_entity=no_audio,
    final_review_llm=text_reply, pronoun_audit_llm=text_reply,
    source_media_sha256=context["pieces"][0]["source_media_sha256"])
(root / "cold-current-review.json").write_text(json.dumps(review, ensure_ascii=False))
(root / "cold-current-review-prompts.json").write_text(json.dumps(prompts, ensure_ascii=False))
assert review["reviewed_srt_sha256"] == current_sha
assert review["discovery"]["status"] == "COMPLETE", review.get("reason_codes")
assert prompts and any("我听到了，哼唱" in prompt for prompt in prompts)
assert review["release_gate"] == "BLOCK"
assert "FINAL_REVIEW_BOUNDARY_SEMANTIC_BLOCKED" in review["reason_codes"]
if control == "normal_generated":
    assert review["correction_pass"] == correction
    assert review["correction_mutation_authority"]["status"] == "PASS"
    assert review["correction_mutation_authority"]["failures"] == []
    assert set(review["reason_codes"]) == {"FINAL_REVIEW_BOUNDARY_SEMANTIC_BLOCKED"}
else:
    assert "FINAL_REVIEW_CORRECTION_MUTATION_AUTHORITY_INVALID" in review["reason_codes"]
rejected = False
try:
    validate_final_review_release(review, expected_srt_sha256=current_sha)
except ValueError:
    rejected = True
assert rejected, "Missing boundary must never become G PASS"
assert authority["exact_final_cpa_self_heal"]["status"] == "REVIEW_PENDING"
print(json.dumps({"cold_owner_validated": True, "current_srt_sha256": current_sha,
    "whole_review_discovery": review["discovery"]["status"], "gate": review["release_gate"],
    "gate_rejected_missing_boundary": rejected, "external_real_provider_calls": 0,
    "correction_control": control, "correction_sha256": correction_sha,
    "correction_mutation_status": review["correction_mutation_authority"]["status"],
    "registered_typed_repairs": len(authority["entity_repairs"]),
    "parent_pid": int(sys.argv[3]), "child_pid": os.getpid(),
    "reason_codes": review["reason_codes"]}))
'''
    completed = subprocess.run([sys.executable, "-B", "-c", script, str(rig.output),
                                correction_control, str(os.getpid())],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=60)
    (rig.output / "cold-process.json").write_text(json.dumps({
        "returncode": completed.returncode, "stdout": completed.stdout, "stderr": completed.stderr}))
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["cold_owner_validated"] is True
    assert len(rig.transport.calls) == 3


def strong_fourth(check, projection):
    plan = observe_plan(1, 3, purpose="FULL_QA")
    info = policy.input_information(source_window=[500, 7000], cue_indexes=[1, 2, 3])
    plan["continuation"] = {
        "physical_target": policy.physical_target(check), "disputed_codepoint_span": [5, 7],
        "disputed_units": ["呜"],
        "prior_observation_ids": [r["observation_id"] for r in projection["observations"]],
        "missing_evidence": "呜的尾部缺少可辨词义边界；此前源音未把完整后续回答当目标块听取。",
        "input_delta": policy._delta(projection, info),
        "expected_gain": "新增后续回答的实际音频，以有词尾或连续哼声区分两种声音解释。",
        "discrimination": [
            {"audible_result": "呜尾部存在可重复辅音词界", "supports": "SPOKEN_WORD", "excludes": "CONTINUOUS_TONE"},
            {"audible_result": "呜尾部持续元音且后续回答称哼声", "supports": "CONTINUOUS_TONE", "excludes": "SPOKEN_WORD"}],
        "verification": "保存新增块的实际raw、原音源窗与声学不确定项，由既有裁决核对是否获得可辨词界。",
        "failure_exit": "CONTEXT_REVIEW_OR_UNRESOLVED"}
    return plan

@pytest.mark.parametrize("mode", ["generic", "contradictory", "concrete"])
def test_fourth_observation_requires_a_concrete_discriminating_plan(rig, mode):
    witness, check, finding, audits = three_observations(rig)
    projection = policy.evidence_projection(check, witness=witness)
    assert projection["effective_distinct_observations"] == 3
    plan = strong_fourth(check, projection)
    if mode == "generic":
        plan["continuation"] = {"expected_gain": "再听一下"}
    elif mode == "contradictory":
        for item in plan["continuation"]["discrimination"]:
            item["excludes"] = item["supports"]
    _, audit, _ = acquire_range(rig, finding, check, plan)
    if mode == "concrete":
        assert audit["status"] == "OBSERVED", audit.get("reason_code")
        assert len(rig.transport.calls) == 4
        assert audit["new_effective_observations_this_attempt"] == 1
    else:
        assert audit["status"] == "CONTEXT_REVIEW"
        assert audit["execution_status"] == "REJECTED_BEFORE_DISPATCH"
        assert len(rig.transport.calls) == 3
        policy.validate_no_gain(audit['no_gain_proof'], audit['terminal_check_request'])
        assert audit['no_gain_proof']['plan'] == plan

def test_existing_unconsumed_observation_reuse_does_not_increment_new_audio(rig):
    witness = acquire_narrow(rig)
    finding = pending_finding(rig, rig.check, witness)
    output, first, _ = acquire_range(rig, finding, rig.check, observe_plan())
    before = len(rig.transport.calls)
    reused, audit, prompts = acquire_range(rig, output, rig.check,
                                           lambda _: pytest.fail("bound reuse needs no plan"))
    assert audit["status"] == "OBSERVED" and audit["served_from_cache"] is True
    assert audit["new_effective_observations_this_attempt"] == 0
    assert audit["provider_calls_this_attempt"] == 0 and not prompts
    assert len(rig.transport.calls) == before

def test_assisted_rewording_with_one_millisecond_padding_is_not_new_information(rig):
    _, check, finding, _ = three_observations(rig)
    _, audit, _ = acquire_range(rig, finding, check, assisted_plan(before_ms=1))
    assert audit["status"] == "CONTEXT_REVIEW" and audit["provider_calls_this_attempt"] == 0
    policy.validate_no_gain(audit['no_gain_proof'], audit['terminal_check_request'])
    assert len(rig.transport.calls) == 3

def test_not_dispatched_and_failed_audio_do_not_increment_effective_observations(rig):
    witness = acquire_narrow(rig)
    finding = pending_finding(rig, rig.check, witness)
    original = rig.verifier.observe_range_context
    def missing(**kwargs):
        return [{"status": "NOT_DISPATCHED", "reason_code": "OFFLINE_MISSING_DEPENDENCY"}]
    missing.available_providers = ("gemini",)
    rig.verifier.observe_range_context = missing
    _, audit, _ = acquire_range(rig, finding, rig.check, observe_plan())
    assert audit["execution_status"] == "NOT_DISPATCHED" and audit["provider_calls_this_attempt"] == 0
    assert audit["new_effective_observations_this_attempt"] == 0 and len(rig.transport.calls) == 1
    assert "OFFLINE_MISSING_DEPENDENCY" in str(audit["native_observations"])
    rig.verifier.observe_range_context = original
    rig.transport.failure = RuntimeError("OFFLINE_TRANSPORT_FAILED_AFTER_DISPATCH")
    _, failed, _ = acquire_range(rig, finding, rig.check, observe_plan())
    assert failed["status"] == "BLOCK"
    assert failed["new_effective_observations_this_attempt"] == 0
    assert len(rig.transport.calls) == 2
    assert failed["provider_calls_this_attempt"] == 1
    assert failed["provider_dispatches"][0]["status"] in {"FAILED", "RESPONSE_REJECTED", "DISPATCHED"}

def test_post_dispatch_ledger_read_failure_is_unknown_not_zero(rig):
    witness = acquire_narrow(rig)
    finding = pending_finding(rig, rig.check, witness)
    original = rig.verifier.relisten_budget_state
    reads = []
    def fail_after():
        reads.append(1)
        if len(reads) > 1:
            raise OSError(5, "OFFLINE_LEDGER_READ_FAILED")
        return original()
    rig.verifier.relisten_budget_state = fail_after
    _, audit, _ = acquire_range(rig, finding, rig.check, observe_plan())
    assert len(rig.transport.calls) == 2
    assert audit["execution_status"] == "UNKNOWN"
    assert audit["provider_calls_this_attempt"] is None
    assert "OFFLINE_LEDGER_READ_FAILED" in audit["reason_detail"]
    assert audit["new_effective_observations_this_attempt"] == 0
    # Read the original result; never retry the audio request.
    assert original()["receipt"]["budget"]["attempts"][-1]["status"] == "OBSERVED"

@pytest.mark.parametrize("field", ["context", "source_clock", "response"])
def test_assisted_cache_rejects_context_source_clock_and_raw_drift(rig, field):
    _, check, _, _ = three_observations(rig)
    row = check["context_only_range_acoustic_observations"][-1]
    altered = deepcopy(check)
    if field == "context":
        altered["whole_clip_current_srt"] = SRT.replace("豆沙问", "另一人问")
    elif field == "source_clock":
        altered["source_media_timeline_offset_ms"] += 1
    else:
        Path(row["artifact_bindings"]["raw_response_path"]).write_text("{}")
    from src.autoslice.independent_context_adjudication import validate_range_acoustic_observation
    validated, reason = validate_range_acoustic_observation(altered["whole_clip_current_srt"], altered, row)
    assert validated is None and reason
    assert len(rig.transport.calls) == 3


def test_failure_before_audio_adapter_can_resume_same_unexecuted_plan(rig):
    witness = acquire_narrow(rig)
    finding = pending_finding(rig, rig.check, witness)
    owner_state = rig.verifier.relisten_budget_state
    def unreadable():
        raise OSError(5, "OFFLINE_PRE_DISPATCH_LEDGER_READ_FAILED")
    rig.verifier.relisten_budget_state = unreadable
    _, failed, _ = acquire_range(rig, finding, rig.check, observe_plan())
    assert failed["execution_status"] == "NOT_DISPATCHED"
    assert failed["audio_adapter_called"] is False
    assert failed["provider_calls_this_attempt"] == 0 and len(rig.transport.calls) == 1
    original = deepcopy(failed)
    finding["exact_release_adjudication"]["semantic_audio_recovery"] = failed
    rig.verifier.relisten_budget_state = owner_state
    _, resumed, _ = acquire_range(rig, finding, rig.check, observe_plan())
    assert resumed["status"] == "OBSERVED", resumed.get("reason_code")
    assert resumed["new_effective_observations_this_attempt"] == 1
    assert len(rig.transport.calls) == 2
    assert failed == original


def test_unknown_dispatch_plan_is_not_reissued_after_reading_original_terminal(rig):
    witness = acquire_narrow(rig)
    finding = pending_finding(rig, rig.check, witness)
    original_state = rig.verifier.relisten_budget_state
    reads = []
    def post_dispatch_failure():
        reads.append(1)
        if len(reads) > 1:
            raise OSError(5, "OFFLINE_POST_DISPATCH_LEDGER_READ_FAILED")
        return original_state()
    rig.verifier.relisten_budget_state = post_dispatch_failure
    _, unknown, _ = acquire_range(rig, finding, rig.check, observe_plan())
    assert unknown["execution_status"] == "UNKNOWN" and unknown["provider_calls_this_attempt"] is None
    assert len(rig.transport.calls) == 2
    original = deepcopy(unknown)
    rig.verifier.relisten_budget_state = original_state
    assert original_state()["receipt"]["budget"]["attempts"][-1]["status"] == "OBSERVED"
    finding["exact_release_adjudication"]["semantic_audio_recovery"] = unknown
    _, retry, _ = acquire_range(rig, finding, rig.check, observe_plan())
    assert retry["status"] == "BLOCK"
    assert retry["reason_code"] == "RELISTEN_REPEATED_INPUT_NO_INFORMATION_GAIN"
    assert len(rig.transport.calls) == 2 and unknown == original


def test_early_no_gain_reopens_actual_unstarted_owner_without_creating_a_ledger(rig):
    witness = acquire_narrow(rig)
    finding = pending_finding(rig, rig.check, witness)
    def plan(data):
        result = no_gain_plan(data)
        result["reason"] = "目标窄窗已含完整起止；词音缺失处没有可进一步裁出的信息。"
        result["no_gain"]["why_audio_cannot_discriminate"] = "完整目标起止已听，尾部无可辨词音；此样例继续裁窗无增益。"
        result["no_gain"]["alternatives"] = [
            {"purpose": "WORD_BOUNDARY", "reason": "窄窗已完整覆盖目标起止，重编码或轻微扩窗不增加词界。",
             "observation_ids": result["no_gain"]["observation_ids"]},
            {"purpose": "SOURCED_CONTEXT", "reason": "已有相邻字幕仅供语境推断，不能创造缺失的可听词音。",
             "observation_ids": result["no_gain"]["observation_ids"]}]
        return result
    _, audit, _ = acquire_range(rig, finding, rig.check, plan)




    assert audit["status"] == "BLOCK" and audit["reason_code"] == "RELISTEN_NO_GAIN_MATERIAL_NOT_EXHAUSTED"
    assert len(rig.transport.calls) == 1 and "no_gain_proof" not in audit
    # The unstarted-owner binding contract below is unchanged; exercise it on the
    # same planner material as a proof without the new exhaustion field.
    proof = policy.compile_no_gain(
        check=audit["consumer_check_request"], recovery=audit["history_audio_recovery"],
        plan=audit["plan"], prompt=audit["prompt"], raw=audit["raw_completion"],
        projection=audit["observation_projection_before"],
        budget_binding=rig.verifier.relisten_budget_state(), original_request=rig.check)
    check = audit["consumer_check_request"]
    assert len(rig.transport.calls) == 1
    assert proof["projection"]["effective_distinct_observations"] == 1
    assert proof["budget_binding"]["receipt"] is None
    owner_path = Path(proof["budget_binding"]["receipt_path"])
    assert not owner_path.exists()
    policy.validate_no_gain(proof, check)
    assert not owner_path.exists()
    for mutation in ("locator", "memory"):
        corrupted = deepcopy(proof)
        if mutation == "locator":
            corrupted["budget_binding"]["receipt_path"] = str(rig.tmp / "unrelated-missing.json")
        else:
            corrupted["budget_binding"]["unstarted_owner_state"]["memory_snapshot"] = {
                "revision": 2, "attempts": [{"status": "OBSERVED"}]}
        corrupted["proof_sha256"] = policy.digest({
            key: value for key, value in corrupted.items() if key != "proof_sha256"})
        with pytest.raises((ValueError, RuntimeError), match="RELISTEN_NATIVE_OWNER"):
            policy.validate_no_gain(corrupted, check)
    state = proof["budget_binding"]["unstarted_owner_state"]
    legacy = Path(state["output_dir"]) / "mai" / "native-audio-budget.json"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text("{}")
    with pytest.raises(ValueError, match="RELISTEN_NATIVE_OWNER_RECEIPT_PRESENT_OR_UNSAFE"):
        policy.validate_no_gain(proof, check)
    assert legacy.read_text() == "{}" and not owner_path.exists()








@pytest.mark.parametrize("heard,proposed,accepted", [
    ("ta shuo ta shuo", "他说他说メンタル", False),
    ("ta shuo ? ? wo zhi dao", "他说メンタル的に我知道", True),
    ("ta shuo ? ? wo zhi dao", "他说メンタル的に我不知道", False),
    ("ta shuo ? wo zhi dao", "他说我知道", True),
])
def test_unclear_foreign_span_cannot_hide_a_clear_suffix_or_create_uncertainty(
        rig, heard, proposed, accepted):
    rig.transport.pinyin = heard
    witness = acquire_narrow(rig)
    if accepted:
        policy.protect_clear_audio(rig.check, witness, proposed)
    else:
        with pytest.raises(ValueError, match="EDITORIAL_CLEAR_AUDIO_CONFLICT"):
            policy.protect_clear_audio(rig.check, witness, proposed)
