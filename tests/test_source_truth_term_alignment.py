"""Bounded local-term discovery, durable replay, and source-owner rejection."""

from copy import deepcopy
import json

import pytest

from src.autoslice.source_subtitle_truth import apply_source_subtitle_truth
from src.autoslice.source_truth_term_alignment import (
    aligned_term_replacement,
    review_local_term_alignment,
)
from src.autoslice.producer_text_finalization import verify_chat_authority_final_surfaces


def _fixture(tmp_path, *, canonical="量子光栅", declared="量子光删", surface="量子光扇",
             base=None, decision="LOCATED"):
    base = base or f"我想看量、{surface}的结果"
    text = f"1\n00:00:00,000 --> 00:00:01,000\n先看实验装置\n\n2\n00:00:01,000 --> 00:00:02,000\n{base}\n"
    spec = {"candidate_id": "unseen-term", "pieces": [{
        "remote_media": "/recordings/experiment.flv",
        "source_media_sha256": "sha256:" + "a" * 64,
        "start_ms": 1000, "end_ms": 3000,
    }]}
    entry = {
        "truth_id": "instrument-term", "knowledge_type": "SOURCE_INTERVAL_TRUTH",
        "evidence_class": "HUMAN_CONFIRMED_LOCAL_TERM",
        "recording_basename": "experiment.flv", "source_media_sha256": "sha256:" + "a" * 64,
        "source_start_ms": 2000, "source_end_ms": 3000,
        "action": "replace_substring", "required": True,
        "replacements": [{"surface": declared, "canonical": canonical}],
        "required_text": canonical,
    }
    ledger = tmp_path / "ledger.json"
    ledger.write_text(json.dumps({"schema_version": "source-subtitle-truth-ledger.v1",
                                 "entries": [entry]}, ensure_ascii=False))
    start = base.index(surface)
    answer = {
        "decision": decision, "reason": "原人工条目所指的同一实验部件",
        "start": start, "end": start + len(surface), "surface": surface,
        "canonical": canonical, "proposed_full_cue": base.replace(surface, canonical, 1),
        "source_basis": "同一完整录制的指定源窗内唯一术语",
        "context_basis": "前句介绍实验装置，本句讨论该部件的结果",
        "evidence_cue_ids": [1, 2],
    }
    if decision == "UNRESOLVED":
        answer.update(start=None, end=None, surface="", canonical="", proposed_full_cue="")
    calls = []

    def llm(prompt):
        calls.append(prompt)
        return json.dumps(answer, ensure_ascii=False)

    llm.cpa_cache_identity = {"transport": "fixture", "models": ["fixture"], "effort": "fixture"}
    llm.provider_runtime_binding = {
        "provider_transport": "fixture", "provider_credential_source": "fixture",
    }
    llm.completed_review_root = tmp_path / "cache"
    _, audit = apply_source_subtitle_truth(text, spec=spec, durations=[2000], ledger_path=ledger)
    kwargs = dict(srt_text=text, spec=spec, durations=[2000], ledger_path=ledger, audit=audit,
                  clip_context={"topic": "实验部件", "whole_clip_draft_srt": text}, llm_call=llm)
    return kwargs, entry, calls, answer


def _replacement(proof, kwargs, entry=None, row=None):
    return aligned_term_replacement(
        proof, row or kwargs["audit"]["failures"][0], entry,
        ledger_sha256=kwargs["audit"]["ledger_sha256"], srt_text=kwargs["srt_text"],
        spec=kwargs["spec"], durations=kwargs["durations"],
    )


@pytest.mark.parametrize("vocabulary", [
    {},
    {"canonical": "星际巡航仪", "declared": "旧型巡航仪", "surface": "新型巡航仪"},
])
def test_generate_save_reopen_exact_scoped_replacement(tmp_path, vocabulary):
    kwargs, entry, calls, answer = _fixture(tmp_path, **vocabulary)
    original_ledger = kwargs["ledger_path"].read_bytes()
    reviews, ids = review_local_term_alignment(**kwargs)
    again, again_ids = review_local_term_alignment(**kwargs)
    assert len(calls) == len(reviews) == len(ids) == 1
    assert again == reviews and again_ids == ids
    assert len(list((tmp_path / "cache").rglob("*.json"))) == 1
    assert kwargs["ledger_path"].read_bytes() == original_ledger
    assert _replacement(reviews[0], kwargs, entry) == (
        answer["surface"], answer["canonical"], answer["proposed_full_cue"],
    )
    assert "量、" in answer["proposed_full_cue"]
    assert reviews[0]["request"]["original_entry"] == entry
    assert kwargs["audit"]["status"] == "FAILED"
    assert reviews[0]["answer"]["decision"] == "LOCATED"


def test_final_owner_exact_transition_and_pure_trim_reindex(tmp_path):
    kwargs, _, _, answer = _fixture(tmp_path)
    reviews, _ = review_local_term_alignment(**kwargs)
    original_row = kwargs["audit"]["failures"][0]
    row = deepcopy(original_row)
    row["resolved_target_projection"]["cues"][0]["after_text"] = answer["proposed_full_cue"]
    assert aligned_term_replacement(reviews[0], row) is not None
    kwargs["srt_text"] = kwargs["srt_text"].replace(answer["surface"], answer["canonical"])
    assert _replacement(reviews[0], kwargs, row=row) is not None
    # Final-stage exact transition is accepted; arbitrary surrounding edits are not.
    row["resolved_target_projection"]["cues"][0]["after_text"] += "呀"
    assert aligned_term_replacement(reviews[0], row) is None
    row = deepcopy(original_row)
    row["cue_indexes"] = [1]
    row["local_windows"] = [{"start_ms": 0, "end_ms": 1000}]
    cue = row["resolved_target_projection"]["cues"][0]
    cue.update(cue_index=1, start_ms=0, end_ms=1000)
    kwargs["srt_text"] = f"1\n00:00:00,000 --> 00:00:01,000\n{cue['before_text']}\n"
    kwargs["spec"]["pieces"][0]["start_ms"] = 2000
    kwargs["durations"] = [1000]
    assert _replacement(reviews[0], kwargs, row=row) is not None


def test_unresolved_saved_and_reopened_without_authority(tmp_path):
    kwargs, _, calls, _ = _fixture(tmp_path, decision="UNRESOLVED")
    reviews, ids = review_local_term_alignment(**kwargs)
    again, again_ids = review_local_term_alignment(**kwargs)
    assert len(calls) == 1 and reviews == again and ids == again_ids
    assert _replacement(reviews[0], kwargs) is None


@pytest.mark.parametrize('damage',[None,'proof','surrounding','speaker','unknown'])
def test_ordinary_writer_replay_and_final_owner_consume_complete_term_proof(tmp_path,damage):
    kwargs, _, calls, answer = _fixture(tmp_path,decision='UNRESOLVED' if damage=='unknown' else 'LOCATED')
    reviews, _ = review_local_term_alignment(**kwargs)
    again, _ = review_local_term_alignment(**kwargs)
    assert len(calls) == 1 and reviews == again
    output,audit = apply_source_subtitle_truth(kwargs['srt_text'],spec=kwargs['spec'],
        durations=kwargs['durations'],ledger_path=kwargs['ledger_path'],local_term_reviews=reviews)
    if damage == 'unknown':
        assert output == kwargs['srt_text'] and audit['status'] == 'FAILED'
        return
    assert output == kwargs['srt_text'].replace(answer['surface'],answer['canonical'])
    assert audit['status'] == 'APPLIED'
    assert '量、' in output
    authority={'source_subtitle_truth_audit':audit}
    speaker=output
    if damage=='proof':
        audit['applied'][0]['local_term_alignment']['raw_response'] += 'drift'
    elif damage=='surrounding':
        output=output.replace('我想看量、','我决定看量、')
    elif damage=='speaker':
        speaker=speaker.replace(answer['canonical'],answer['surface'])
    assert verify_chat_authority_final_surfaces(authority,final_text_srt=output,
        final_speaker_srt=speaker,delivery_start_ms=0,delivery_end_ms=2000) is (damage is None)


@pytest.mark.parametrize("field", ["provider_runtime_binding", "cpa_cache_identity",
                                  "completed_review_root", "unbound_credential"])
def test_unbound_provider_never_dispatches(tmp_path, monkeypatch, field):
    kwargs, _, calls, _ = _fixture(tmp_path)
    monkeypatch.delenv("AUTOSLICE_FINAL_REVIEW_RUNTIME_ROOT", raising=False)
    monkeypatch.delenv("AUTOSLICE_BASE", raising=False)
    if field == "unbound_credential":
        kwargs["llm_call"].provider_runtime_binding["provider_credential_source"] = "UNBOUND"
    else:
        delattr(kwargs["llm_call"], field)
    with pytest.raises(ValueError, match="CHECKPOINT_UNBOUND"):
        review_local_term_alignment(**kwargs)
    assert calls == []


@pytest.mark.parametrize("failure", ["canonical", "anchor", "overlong", "whole_cue",
                                    "nonunique", "stutter_loss", "span", "basis", "unknown"])
def test_invalid_location_never_saved(tmp_path, failure):
    setup = {}
    if failure == "nonunique":
        setup["base"] = "量子光扇和量子光扇"
    elif failure == "whole_cue":
        setup["base"] = "量子光扇"
    elif failure == "overlong":
        setup["surface"] = "量子光扇设备"
    kwargs, _, _, answer = _fixture(tmp_path, **setup)
    if failure == "canonical":
        answer["canonical"] = "量子光盘"
    elif failure == "anchor":
        answer.update(start=0, end=2, surface="我想")
        base = kwargs["audit"]["failures"][0]["resolved_target_projection"]["cues"][0]["before_text"]
        answer["proposed_full_cue"] = answer["canonical"] + base[2:]
    elif failure == "stutter_loss":
        answer["proposed_full_cue"] = answer["proposed_full_cue"].replace("量、", "")
    elif failure == "span":
        answer["start"] += 1
    elif failure == "basis":
        answer["context_basis"] = ""
    elif failure == "unknown":
        answer["decision"] = "EQUIVALENT"
    with pytest.raises(ValueError, match="ALIGNMENT_.*INVALID"):
        review_local_term_alignment(**kwargs)
    retained = [json.loads(path.read_bytes()) for path in (tmp_path / "cache").rglob("*.json")]
    assert retained and all(row.get("schema_version") == "completed-review-response.v1" for row in retained)


@pytest.mark.parametrize("drift", ["source", "window", "context", "current_text", "raw",
                                  "entry", "supplied_entry", "provider", "parser", "shell",
                                  "request_context", "row_window", "ledger"])
def test_replay_rejects_drift_and_forged_shell(tmp_path, drift):
    kwargs, entry, _, _ = _fixture(tmp_path)
    reviews, _ = review_local_term_alignment(**kwargs)
    proof = deepcopy(reviews[0])
    if drift == "source":
        kwargs["spec"]["pieces"][0]["source_media_sha256"] = "sha256:" + "b" * 64
    elif drift == "window":
        kwargs["spec"]["pieces"][0]["start_ms"] += 10
    elif drift == "context":
        kwargs["srt_text"] = kwargs["srt_text"].replace("先看实验装置", "先看其他装置")
    elif drift == "current_text":
        kwargs["srt_text"] = kwargs["srt_text"].replace("我想看", "他想看")
    elif drift == "raw":
        proof["raw_response"] += "changed"
    elif drift == "entry":
        proof["request"]["original_entry"]["authority"] = "invented"
    elif drift == "supplied_entry":
        entry["authority"] = "invented"
    elif drift == "provider":
        proof["checkpoint_binding"]["provider"]["runtime"]["provider_credential_source"] = "UNBOUND"
    elif drift == "parser":
        proof["checkpoint_binding"]["parser_sha256"] = "f" * 64
    elif drift == "request_context":
        proof["request"]["clip_context"]["topic"] = "另一话题"
    elif drift == "row_window":
        kwargs["audit"]["failures"][0]["local_windows"][0]["start_ms"] += 10
        kwargs["audit"]["failures"][0]["local_windows"][0]["end_ms"] += 10
    elif drift == "ledger":
        kwargs["audit"]["ledger_sha256"] = "sha256:" + "c" * 64
    else:
        proof = {"status": "PASS", "decision": "LOCATED", "surface": "量子光扇"}
    assert _replacement(proof, kwargs, entry) is None


@pytest.mark.parametrize("failure", ["no_anchor", "alias", "partial_coverage", "two_cues",
                                    "changed_cue", "ledger", "mention", "wrong_evidence"])
def test_ineligible_question_never_dispatches(tmp_path, failure):
    kwargs, entry, calls, _ = _fixture(tmp_path, declared="误词" if failure == "no_anchor" else "量子光删")
    row = kwargs["audit"]["failures"][0]
    if failure == "alias":
        row["source_aliases"] = [{"alias_id": "unbound"}]
    elif failure == "partial_coverage":
        row["source_coverage_ms"] = 900
    elif failure == "two_cues":
        row["resolved_target_projection"]["cues"].append(
            deepcopy(row["resolved_target_projection"]["cues"][0]))
    elif failure == "changed_cue":
        row["resolved_target_projection"]["cues"][0]["before_text"] += "别的"
    elif failure == "ledger":
        kwargs["audit"]["ledger_sha256"] = "sha256:" + "b" * 64
    elif failure in {"mention", "wrong_evidence"}:
        if failure == "mention":
            entry["mention_postconditions"] = []
        else:
            entry["evidence_class"] = "MODEL_SUGGESTION"
        kwargs["ledger_path"].write_text(json.dumps(
            {"schema_version": "source-subtitle-truth-ledger.v1", "entries": [entry]}))
        # Bind the changed human entry into the audit without invoking governance.
        from src.autoslice.final_review_meaning_policy import digest
        import hashlib
        kwargs["audit"]["ledger_sha256"] = "sha256:" + hashlib.sha256(
            kwargs["ledger_path"].read_bytes()).hexdigest()
        row["entry_sha256"] = digest(entry)
    assert review_local_term_alignment(**kwargs) == ([], [])
    assert calls == []
