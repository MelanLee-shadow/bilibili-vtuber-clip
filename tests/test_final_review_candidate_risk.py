import copy
import hashlib
import json
from pathlib import Path

import pytest

from src.autoslice.final_review_auditor import audit_final_subtitles
from src.autoslice.final_review_candidate_risk import (
    RISK_PROMPT_MARKER, candidate_risk_assessment_valid,
    candidate_risk_parser_successor, unreviewed_postburn_candidate_cues,
)
from src.autoslice.final_review_contract import is_noncritical_disclosed
from src.autoslice.final_review_meaning_policy import POLICY_VERSION, bind_current_risk


def _text(*, critical=False):
    identity = "我是猫" if critical else "我不是猫"
    return ("1\n00:00:00,000 --> 00:00:01,000\n小鱼就是我，小雨也是我，" + identity
            + "\n\n2\n00:00:01,100 --> 00:00:02,000\n小雨不是猫\n")


def _nickname_equivalence():
    return {
        "status": "EQUIVALENT", "current_meaning": "小雨这个昵称所指的说话人不是猫。",
        "alternative_meaning": "换用小鱼这个昵称仍表示该说话人不是猫。",
        "shared_meaning": "同一个说话人不是猫。", "uncertain_detail": "本句采用小鱼或小雨哪个昵称字形。",
        "context_basis": "第1句明确小鱼和小雨都是说话人本人，第2句主语的不同昵称仍同指。",
        "evidence_cue_ids": [1, 2],
    }


def _proposal(*, source="小鱼", kind="self_ref"):
    return {"cue": 2, "kind": kind, "content_risk": "noncritical_uncertainty",
            "risk_basis": "第1句已确认两昵称同指，当前不是猫的命题不因昵称字形变化。",
            "meaning_equivalence": _nickname_equivalence(),
            "proposed_full_cue": source + "不是猫", "repair_class": "phonetic",
            "source_surface": source, "evidence_cue_ids": [1], "why": "核实主体指代"}


def _decision(risk):
    return {"findings": [{"cue": 2, "content_risk": risk,
        "content_effect": "context_supported_meaning_equivalence" if risk == "noncritical_uncertainty" else "concrete_meaning_or_fact_conflict",
        "risk_basis": "两昵称在第1句明确同指，整句命题相同" if risk == "noncritical_uncertainty" else "第1句明确我是猫，第2句不是猫与该身份事实冲突",
        "meaning_equivalence": _nickname_equivalence() if risk == "noncritical_uncertainty" else None,
        "evidence_cue_ids": [1, 2]}]}


@pytest.mark.parametrize("risk", ["factual_error", "noncritical_uncertainty"])
def test_source_candidate_and_content_risk_have_separate_bound_decisions(risk):
    prompts = []
    def llm(prompt):
        prompts.append(prompt)
        return json.dumps(_decision(risk) if RISK_PROMPT_MARKER in prompt else {"findings": [_proposal()]})
    row, = audit_final_subtitles(_text(critical=risk == "factual_error"), llm_call=llm, extract_json=json.loads)
    assert len(prompts) == 2
    assert row["proposed_full_cue"] == "小鱼不是猫"
    assert row["candidate_provenance"]["kind"] == "transcript_context"
    assert row["force_acoustic"] is True
    assert row["repair_class"] == "source_backed_entity"
    assert candidate_risk_assessment_valid(row)
    # Current contextual equivalence can disclose unchanged text; this never
    # supplies the separate typed authority needed to apply the candidate.
    assert is_noncritical_disclosed(row) is (risk == "noncritical_uncertainty")
    assert "exact_release_adjudication" not in row
    assert "合法第三者、昵称、引用" in prompts[-1]
    assert "不能证明听到或授权改字" in prompts[-1]


def test_rejected_candidate_still_requires_content_review_and_cannot_become_clean():
    raw = _proposal(source="小鹿")
    for key in ("content_risk", "risk_basis", "meaning_equivalence"):
        raw.pop(key)
    prompts = []
    def llm(prompt):
        prompts.append(prompt)
        if len(prompts) == 1:
            return json.dumps({"findings": [raw]})
        assert "本次只补齐上一轮漏报" in prompt
        return json.dumps(_decision("factual_error"))
    row, = audit_final_subtitles(_text(critical=True), llm_call=llm, extract_json=json.loads)
    assert len(prompts) == 2
    assert row["suggestion_rejected_reason"] == "ENTITY_REPAIR_REQUIRES_SOURCE_PROVENANCE"
    assert row["proposed_full_cue"] is None
    assert row["content_risk"] == "factual_error"
    assert not is_noncritical_disclosed(row)


def test_bound_latin_object_candidate_uses_cited_text_without_automatic_replacement():
    text = ("1\n00:00:00,000 --> 00:00:01,000\n这里讨论的是 menu\n\n"
            "2\n00:00:01,100 --> 00:00:02,000\n美女更好\n")
    raw = _proposal(source="menu", kind="context")
    raw.update(proposed_full_cue="menu更好", content_risk="factual_error",
               risk_basis="本段讨论菜单对象，当前句却变成了对女性的评价。")
    raw.pop("meaning_equivalence")
    row, = audit_final_subtitles(text, llm_call=lambda p: json.dumps(
        _decision("factual_error") if RISK_PROMPT_MARKER in p else {"findings": [raw]}), extract_json=json.loads)
    assert row["candidate_provenance"]["surface"] == "menu"
    assert row["proposed_full_cue"] == "menu更好" and row["force_acoustic"]
    assert row["content_risk"] == "factual_error"
    assert "美女更好" in text


def test_concrete_conflict_cannot_be_labeled_noncritical_in_completed_risk_receipt():
    bad = _decision("noncritical_uncertainty")
    bad["findings"][0]["content_effect"] = "concrete_meaning_or_fact_conflict"
    with pytest.raises(ValueError, match="RISK_REVIEW_INVALID"):
        audit_final_subtitles(_text(), llm_call=lambda p: json.dumps(
            bad if RISK_PROMPT_MARKER in p else {"findings": [_proposal()]}), extract_json=json.loads)


@pytest.mark.parametrize("drift", ["base", "proposal", "risk", "srt"])
def test_noncritical_candidate_assessment_rejects_identity_drift(drift):
    row, = audit_final_subtitles(_text(), llm_call=lambda p: json.dumps(
        _decision("noncritical_uncertainty") if RISK_PROMPT_MARKER in p else {"findings": [_proposal()]}), extract_json=json.loads)
    if drift == "srt":
        assert not candidate_risk_assessment_valid(row, srt_sha256="sha256:" + "b" * 64)
    else:
        row[{"base": "base_text_sha256", "proposal": "proposed_full_cue", "risk": "risk_basis"}[drift]] = "tamper"
        assert not is_noncritical_disclosed(row)


def test_saved_false_clean_postburn_remains_readable_but_is_blocked_by_current_policy():
    old = {"cue_index": 2, "kind": "self_ref", "content_risk": "noncritical_uncertainty",
           "risk_basis": "疑似主体漂移，候选缺少来源", "suggestion_rejected_reason": "ENTITY_REPAIR_REQUIRES_SOURCE_PROVENANCE"}
    record = {"postburn_subtitle_audio_review": {"status": "CLEAN", "discovered_findings": [old]}}
    before = copy.deepcopy(record)
    assert unreviewed_postburn_candidate_cues(record) == [2]
    from src.autoslice.producer_package_finalization import _postburn_current_risk_partition
    assert record == before and _postburn_current_risk_partition([old]) == ([old], [])
    assert not candidate_risk_parser_successor([old], [{**old, "cue_index": 3}])
    critical = {"cue_index": 3, "kind": "context", "content_risk": "meaning_reversal"}
    assert unreviewed_postburn_candidate_cues({"postburn_subtitle_audio_review": {
        "status": "CLEAN", "discovered_findings": [critical]}}) == [3]


def test_context_supported_same_reference_nickname_can_be_disclosed():
    raw = {"cue": 2, "kind": "context", "repair_class": "disclosure_only",
           "proposed_full_cue": None, "why": "同指昵称字形未知", "content_risk": "noncritical_uncertainty",
           "risk_basis": "第1句明确两昵称同指，整句不是猫的含义不变。",
           "meaning_equivalence": _nickname_equivalence()}
    prompts = []
    row, = audit_final_subtitles(_text(), llm_call=lambda p: prompts.append(p) or json.dumps({"findings": [raw]}), extract_json=json.loads)
    assert len(prompts) == 1 and is_noncritical_disclosed(row)


def test_old_preburn_noncritical_pronoun_scalar_requires_current_meaning_review():
    row = {"kind": "context", "repair_class": "phonetic", "cue_index": 18,
           "suggestion_rejected_reason": "LATIN_SCRIPT_REPAIR_REQUIRES_SOURCE_PROVENANCE",
           "content_risk": "noncritical_uncertainty",
           "risk_basis": "他们学校泛指校方，未断言成员均为男性"}
    before = copy.deepcopy(row)
    assert not is_noncritical_disclosed(row)
    assert row == before
    assert unreviewed_postburn_candidate_cues({"final_review_audit": {
        "unresolved_findings_disclosed": [row]}}) == []


@pytest.mark.parametrize("drift", [None, "srt", "transport", "typed_decision"])
def test_postburn_disclosure_is_not_fed_back_as_new_exact_evidence(tmp_path, drift):
    from src.autoslice import producer_package_finalization as finalization

    text = _text()
    srt_sha = "sha256:" + hashlib.sha256(text.encode()).hexdigest()
    row = {"cue_index": 2, "base_text_sha256": hashlib.sha256("小雨不是猫".encode()).hexdigest(),
           "content_risk": "noncritical_uncertainty", "risk_basis": "第1句明确两昵称同指，整句含义不变。"}
    row["current_content_risk"] = bind_current_risk(
        row, srt_text=text, cue_count=2, prompt=text, meaning_equivalence=_nickname_equivalence(),
    )
    original = copy.deepcopy(row)
    if drift == "typed_decision":
        row["exact_release_adjudication"] = {
            "status": "OBSERVED", "policy_branch": "JUDGE_KEEPS_CURRENT",
            "repaired": False, "timing_immutable": True,
            "mutation_authority": {"status": "NOT_APPLIED"},
        }
    request = {
        "meaning_policy": POLICY_VERSION,
        "cpa_cache_identity": {"transport": "fixture", "models": ["fixture-model"], "effort": "fixture"},
        "provider_runtime_binding": {"provider_transport": "fixture"},
        "final_srt_sha256": srt_sha,
    }
    request["request_sha256"] = finalization._postburn_request_sha256(request)
    call = {"request_sha256": request["request_sha256"], "dispatch_status": "COMPLETED"}
    for field, value in (("prompt", text), ("response", json.dumps({"findings": [_proposal()]}))):
        path = tmp_path / (field + ".txt")
        path.write_text(value)
        call[field + "_path"] = str(path)
        call[field + "_sha256"] = "sha256:" + hashlib.sha256(value.encode()).hexdigest()
    if drift == "transport":
        call["response_sha256"] = "sha256:" + "f" * 64
    review = {"unresolved_findings_disclosed": [row], "postburn_subtitle_audio_review": {
        "schema_version": "postburn-subtitle-audio-review.v1",
        "meaning_policy": POLICY_VERSION, "cpa_request": request, "cpa_calls": [call], "findings": [],
        "reviewed_srt_sha256": srt_sha if drift != "srt" else "sha256:" + "b" * 64,
        "unresolved_findings_disclosed": [original]}}
    assert finalization._postburn_cached_review_complete(review["postburn_subtitle_audio_review"]) is (drift != "transport")
    evidence = finalization._current_postburn_resolution_evidence(exact_review=review, reviewed_srt_sha256=srt_sha)
    if drift is None:
        assert evidence is None
    else:
        assert evidence["resolved_findings"] == [row]
    assert review["postburn_subtitle_audio_review"]["unresolved_findings_disclosed"] == [original]


def test_postburn_missing_risk_is_completed_then_raw_calls_are_reused(tmp_path, monkeypatch):
    from tests.test_final_subtitle_audio_gate import _postburn_record, _postburn_chat_audit, _postburn_adapters
    from src.autoslice import producer_package_finalization as finalization

    record, srt, media = _postburn_record(tmp_path)
    raw = {"findings": [{"cue": 4, "kind": "context", "repair_class": "phonetic",
                         "proposed_full_cue": "这里有更正需要审看", "source_surface": "更正",
                         "evidence_cue_ids": [4], "why": "成片听写与当前字幕的主题术语不一致"}]}
    decision = {"findings": [{"cue": 4, "content_risk": "factual_error",
                "risk_basis": "当前字幕称有乱码，成片听写称有更正；两者指向不同问题，需继续裁决。",
                "evidence_cue_ids": [4]}]}
    calls = []
    def llm(prompt):
        calls.append(prompt)
        return json.dumps(raw if len(calls) == 1 else decision)
    # Reuse the standard fake transport identity while varying its raw answer.
    _postburn_adapters(monkeypatch, "", calls=[])
    identity = finalization.build_final_review_llm_call()
    llm.cpa_cache_identity = identity.cpa_cache_identity
    llm.provider_runtime_binding = identity.provider_runtime_binding
    monkeypatch.setattr(finalization, "build_final_review_llm_call", lambda: llm)
    audit = _postburn_chat_audit(srt)
    audit["final_review_audit"]["meaning_policy"] = POLICY_VERSION
    preburn_review = copy.deepcopy(audit["final_review_audit"])
    kwargs = dict(cid="candidate", out_root=tmp_path,
        recut=finalization.FinalRecutArtifacts(tmp_path, media, srt, None, None),
        record=record, chat_authority_audit=audit, chat_authority_path=tmp_path / "candidate.chat-authority.json")
    before = srt.read_bytes(), media.read_bytes()
    with pytest.raises(SystemExit, match="POSTBURN_SUBTITLE_AUDIO_REVIEW_BLOCKED"):
        finalization._run_postburn_subtitle_audio_review(**kwargs)
    prior = copy.deepcopy(audit["postburn_subtitle_audio_review"])
    original_response = prior["cpa_calls"][0]["response_path"]
    original_sha = hashlib.sha256(Path(original_response).read_bytes()).hexdigest()
    assert json.loads(Path(original_response).read_text()) == raw
    for pass_index in range(2):
        audit["final_review_audit"] = copy.deepcopy(preburn_review)
        with pytest.raises(SystemExit, match="POSTBURN_SUBTITLE_AUDIO_REVIEW_BLOCKED") as blocked:
            finalization._run_postburn_subtitle_audio_review(**kwargs)
        current = audit["postburn_subtitle_audio_review"]
        assert current["findings"], (pass_index, {k:(prior['cpa_request'].get(k),v) for k,v in current['cpa_request'].items() if prior['cpa_request'].get(k)!=v}, repr(blocked.value.__cause__))
        assert current["findings"][0]["content_risk"] == "factual_error"
        assert len(current["cpa_calls"]) == 2
        assert len(calls) == 2  # initial discovery + one risk call, then reentry reuses both
        assert current["cache_reused"] is True
    assert hashlib.sha256(Path(original_response).read_bytes()).hexdigest() == original_sha
    assert (srt.read_bytes(), media.read_bytes()) == before
    # A later risk child can finish before its parent stops. Recover only the
    # final matching call and revalidate every earlier completed observation.
    review = copy.deepcopy(current)
    call = review["cpa_calls"][-1]
    files = []
    for name, key in [("prompt.txt", "prompt_path"), ("completion.txt", "response_path")]:
        path = Path(call[key])
        data = path.read_bytes()
        files.append({"source": "/native/bridge/" + name, "preserved": str(path),
                      "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)})
    execution = {"status": "INTERRUPTED", "output_root": str(tmp_path),
        "terminal_materials": {"srt": {"sha256": hashlib.sha256(srt.read_bytes()).hexdigest()},
                               "video": {"sha256": hashlib.sha256(media.read_bytes()).hexdigest()}},
        "interruption": {"provider_child_completed_before_parent_stop": True,
            "raw_completion_unconsumed": {"outcome": "CHILD_COMPLETED_RAW_UNCONSUMED_PARENT_INTERRUPTED",
                "raw_wait_exit_code": 0, "child_not_signalled": True, "pidfd_exit_ready": True, "files": files}}}
    path = tmp_path / "completed-risk-execution.json"
    path.write_text(json.dumps(execution))
    monkeypatch.setenv("AUTOSLICE_COMPLETED_CPA_EXECUTION", str(path))
    monkeypatch.setenv("AUTOSLICE_COMPLETED_CPA_EXECUTION_SHA256", hashlib.sha256(path.read_bytes()).hexdigest())
    review.update(status="RUNNING", release_gate="BLOCK")
    call.update(dispatch_status="RUNNING")
    call.pop("response_path")
    call.pop("response_sha256")
    recovered = finalization._recover_completed_postburn_transport(review, tmp_path)
    assert recovered["cpa_calls"][0] == review["cpa_calls"][0]
    assert recovered["cpa_calls"][-1]["dispatch_status"] == "COMPLETED"
    assert finalization._postburn_cached_review_complete(recovered)
    damaged = copy.deepcopy(review)
    damaged["cpa_calls"][0]["response_sha256"] = "sha256:" + "f" * 64
    with pytest.raises(ValueError, match="HASH_DRIFT"):
        finalization._recover_completed_postburn_transport(damaged, tmp_path)
