"""Independent-witness wording disagreements become exact-final nominations.

Fixtures are trimmed from the 闺蜜 package (BV16vJm6pEZC): final cues in
delivery time, witness cues in media time (intro offset 2520 ms).
"""
import json

import pytest

from src.autoslice.final_review_auditor import audit_final_subtitles
from src.autoslice.final_review_contract import (
    undecided_witness_nomination, is_noncritical_disclosed,
)
from src.autoslice.witness_disagreement import (
    PROVENANCE_KIND, assess_correspondence_text, decided_nomination_ids,
    find_witness_disagreements, to_priority_findings,
)

OFFSET = 2520


def _ts(ms):
    s, m = divmod(ms, 1000)
    mi, s = divmod(s, 60)
    return f"00:{mi:02d}:{s:02d},{m:03d}"


def _srt(rows, shift=0):
    return "\n\n".join(
        f"{i}\n{_ts(a + shift)} --> {_ts(b + shift)}\n{t}"
        for i, (a, b, t) in enumerate(rows, 1)) + "\n"


def _pair(final_text, witness_text, *, offset=OFFSET):
    final = _srt([(1000, 4000, final_text)])
    witness = _srt([(1000, 4000, witness_text)], shift=offset)
    return final, witness


def _noms(final_text, witness_text):
    final, witness = _pair(final_text, witness_text)
    return find_witness_disagreements(final, witness, intro_offset_ms=OFFSET)


def test_wording_swap_is_nominated_with_both_spans_and_pinyin():
    nom, = _noms("你，你不觉得我跟别，别人说能发挥很不道德吗",
                 "你你不觉得我跟别别人说你坏话很不道德吗")
    assert (nom["final_span"], nom["witness_span"]) == ("能发挥", "你坏话")
    assert nom["final_pinyin"] == ["neng", "fa", "hui"]
    assert nom["witness_pinyin"] == ["ni", "huai", "hua"]
    assert nom["evidence_source"] == "witness" and nom["cue_index"] == 1
    assert nom["proposed_full_cue"] == "你，你不觉得我跟别，别人说你坏话很不道德吗"


def test_multi_char_swap_across_unchanged_char_is_one_span():
    nom, = _noms("看了不许跟别人说话", "看来我不去跟别人说话")
    assert (nom["final_span"], nom["witness_span"]) == ("了不许", "来我不去")
    assert nom["proposed_full_cue"] == "看来我不去跟别人说话"


def test_dropped_word_is_nominated():
    nom, = _noms("偷了你的金手镯去卖", "偷你的金手镯去卖")
    assert (nom["final_span"], nom["witness_span"]) == ("了", "")
    assert nom["repair_class"] == "spoken_unit"
    assert nom["proposed_full_cue"] == "偷你的金手镯去卖"


@pytest.mark.parametrize("final_text,witness_text", [
    ("她是直女啊", "她是侄女啊"),            # channel-fixed spelling (surface canon)
    ("她是侄女啊", "她是直女啊"),
    ("那她说是那个切片吗", "那他说是那个切片吗"),   # pronouns owned by another stage
    ("那TA说是那个切片吗", "那她说是那个切片吗"),
    ("那它说是那个切片吗", "那她说是那个切片吗"),
    ("你，你不觉得很不道德吗", "你你不觉得很不道德吗"),  # punctuation + stutter
    ("你不觉得很不道德吗", "你你不觉得很不道德吗"),
    ("我觉得，我觉得可以", "我觉得可以"),
    ("真的吗？", "真的吗"),
    ("同一句话", "同一句话"),
])
def test_normalized_equivalents_are_not_disagreements(final_text, witness_text):
    assert _noms(final_text, witness_text) == []


def test_homophone_with_identical_pinyin_is_not_nominated():
    assert _noms("他在做作业", "他在做作业") == []
    assert _noms("事实如此", "是实如此") == []


def test_intro_offset_is_applied_once_and_intro_speech_is_not_a_claim():
    final = _srt([(500, 3000, "不是给别人说")])
    witness = _srt([(0, 2000, "欢迎收看本期节目"), (500, 3000, "不是给别人说")], shift=0)
    witness = _srt([(0, 2400, "欢迎收看本期节目"), (500 + OFFSET, 3000 + OFFSET, "不是给别人说")])
    assert find_witness_disagreements(final, witness, intro_offset_ms=OFFSET) == []
    with pytest.raises(ValueError):
        find_witness_disagreements(final, witness, intro_offset_ms=-1)


def test_receipt_block_counts_nominations():
    final, witness = _pair("偷了你的金手镯去卖", "偷你的金手镯去卖")
    block = assess_correspondence_text(final, witness, intro_offset_ms=OFFSET)
    assert block["nomination_count"] == 1 and len(block["nomination_ids"]) == 1


def _equivalence():
    return {
        "status": "EQUIVALENT", "current_meaning": "感谢小明送来的灯牌。",
        "alternative_meaning": "名字写作晓明时仍是感谢同一人送来的灯牌。",
        "shared_meaning": "感谢刚送灯牌的同一名观众。", "uncertain_detail": "观众名字明前一字的写法。",
        "context_basis": "第2句将小明和晓明明确绑定为同一观众，且明确此人刚送灯牌。",
        "evidence_cue_ids": [1, 2],
    }


def _reviewer_with_noncritical_risk(prompt):
    if "本次只补齐" in prompt:
        return json.dumps({"findings": [{
            "cue": 1, "content_risk": "noncritical_uncertainty",
            "risk_basis": "邻句确认小明和晓明是同一送灯牌者，字形疑点不改变整句所指和谢礼含义。",
            "meaning_equivalence": _equivalence()}]})
    return '{"findings":[]}'


def _wired_finding(final_text, witness_text):
    final, witness = _pair(final_text + "\n", witness_text)
    final = _srt([(1000, 4000, final_text), (4000, 6000, "小明，你名字也写作晓明对吧，就是你刚送的灯牌")])
    witness = _srt([(1000, 4000, witness_text), (4000, 6000, "小明，你名字也写作晓明对吧，就是你刚送的灯牌")], shift=OFFSET)
    noms = find_witness_disagreements(final, witness, intro_offset_ms=OFFSET)
    rows = to_priority_findings(noms, final_srt_text=final, witness_srt_sha256="sha256:" + "ab" * 32)
    return noms, audit_final_subtitles(
        final, llm_call=_reviewer_with_noncritical_risk, extract_json=json.loads,
        extra_raw_findings=rows, prioritize_extra_raw_findings=True)


def test_nomination_reaches_findings_with_both_candidates_and_cannot_be_waved_through(monkeypatch):
    noms, findings = _wired_finding("谢谢小明的灯牌", "谢谢小红的灯牌")
    row, = [f for f in findings if (f.get("candidate_provenance") or {}).get("kind") == PROVENANCE_KIND]
    assert row["candidate_provenance"]["kind"] == PROVENANCE_KIND
    assert row["candidate_provenance"]["nomination_id"] == noms[0]["nomination_id"]
    assert row["candidate_provenance"]["mutation_authorized"] is False
    assert row["proposed_full_cue"] == "谢谢小红的灯牌" and row["suggestion"] == "红"
    assert row["force_acoustic"] is True
    assert row["content_risk"] == "noncritical_uncertainty"
    # "meaning-equivalent" tolerance does not skip the proposed candidate ...
    assert undecided_witness_nomination(row)
    assert not is_noncritical_disclosed(row)
    # ... which, without the guard, the non-critical path would have disclosed.
    monkeypatch.setattr(
        "src.autoslice.final_review_contract.undecided_witness_nomination", lambda _row: False)
    assert is_noncritical_disclosed(row)


def test_decided_nominations_are_not_asked_again_but_unrelated_rows_do_not_settle():
    noms, findings = _wired_finding("谢谢小明的灯牌", "谢谢小红的灯牌")
    row = next(f for f in findings if (f.get("candidate_provenance") or {}).get("kind") == PROVENANCE_KIND)
    assert decided_nomination_ids(noms, []) == set()
    assert decided_nomination_ids(noms, [{**row, "cue_index": 2}]) == set()
    unrelated = {"cue_index": 1, "base_text_sha256": noms[0]["base_text_sha256"],
                 "suspect": "别的", "proposed_full_cue": "别的句子"}
    assert decided_nomination_ids(noms, [unrelated]) == set()
    assert decided_nomination_ids(noms, [row]) == {noms[0]["nomination_id"]}
    assert decided_nomination_ids(noms, [{**row, "base_text_sha256": "0" * 64}]) == set()


# ---- wiring: real post-burn entry, receipt, and release gate -------------

def _postburn_llm(monkeypatch, calls):
    from src.autoslice import producer_package_finalization as finalization

    def llm(prompt):
        calls.append(prompt)
        if "本次只补齐" in prompt:
            return json.dumps({"findings": [{
                "cue": 4, "content_risk": "noncritical_uncertainty",
                "risk_basis": "邻句确认小明和晓明是同一送灯牌者，字形疑点不改变整句所指和谢礼含义。",
                "meaning_equivalence": {**_equivalence(), "evidence_cue_ids": [3, 4]}}]})
        return '{"findings":[]}'

    llm.cpa_cache_identity = {"transport": "cpa_command", "models": ["gpt-6-sol"], "effort": "medium"}
    llm.provider_runtime_binding = {"provider_transport": "runtime_cpa",
                                    "provider_credential_source": "/runtime/cpa.env"}
    monkeypatch.setattr(finalization, "build_final_review_llm_call", lambda: llm)


def _run_postburn(tmp_path, monkeypatch, **extra):
    from src.autoslice import producer_package_finalization as finalization
    from tests.test_final_subtitle_audio_gate import _postburn_chat_audit, _postburn_record

    record, final_srt, media = _postburn_record(tmp_path, disagreeing=True)
    audit = _postburn_chat_audit(final_srt)
    chat_path = tmp_path / "chat.json"
    chat_path.write_text("{}\n", encoding="utf-8")
    kwargs = dict(
        cid="candidate", out_root=tmp_path,
        recut=finalization.FinalRecutArtifacts(tmp_path, media, final_srt, None, None),
        record=record, chat_authority_audit=audit, chat_authority_path=chat_path, **extra)
    return finalization, record, audit, final_srt, kwargs


def test_receipt_is_assessed_and_validates_with_nomination_count(tmp_path, monkeypatch):
    from src.autoslice import final_subtitle_audio_gate as gate

    _, record, _, final_srt, kwargs = _run_postburn(tmp_path, monkeypatch)
    receipt = record["subtitle_audio_correspondence"]["receipt"]
    assert receipt["text_correctness_status"] == "ASSESSED"
    assert receipt["text_correctness_verdict"] == "NOMINATIONS_PENDING_ADJUDICATION"
    assert receipt["witness_disagreement"]["nomination_count"] == 1
    assert receipt["status"] == "PASS"  # timing verdict untouched
    gate.validate_final_subtitle_audio_check(
        record, final_srt, kwargs["recut"].media_path, tmp_path / "evidence", candidate_id="candidate")


def test_undecided_witness_disagreement_blocks_postburn_and_reaches_priority_lane(tmp_path, monkeypatch):
    from src.autoslice import producer_text_pipeline as text_pipeline
    from tests.test_final_subtitle_audio_gate import _sha

    finalization, record, audit, final_srt, kwargs = _run_postburn(tmp_path, monkeypatch)
    calls = []
    _postburn_llm(monkeypatch, calls)
    with pytest.raises(SystemExit, match="POSTBURN_SUBTITLE_AUDIO_REVIEW_BLOCKED"):
        finalization._run_postburn_subtitle_audio_review(**kwargs)
    review = audit["postburn_subtitle_audio_review"]
    assert review["release_gate"] == "BLOCK" and review["status"] == "FLAGGED"
    row, = review["findings"]
    assert row["cue_index"] == 4 and row["candidate_provenance"]["kind"] == PROVENANCE_KIND
    assert row["proposed_full_cue"] == "这里有更正需要审看"
    assert review["witness_disagreement"]["open_nomination_ids"] == [
        row["candidate_provenance"]["nomination_id"]]
    assert review["cpa_request"]["witness_disagreement_sha256"].startswith("sha256:")
    # The block is handed to the ordinary exact-final reviewer as a priority row.
    rows, _sha_req = text_pipeline.validated_postburn_priority_findings(
        final_srt_text=final_srt.read_text(encoding="utf-8"), verified_authority_audit=audit,
        candidate_id="candidate", clip_context=None, expected_media_sha256=_sha(kwargs["recut"].media_path),
    )
    assert rows[0]["candidate_provenance"]["nomination_id"] == row["candidate_provenance"]["nomination_id"]


def test_decided_witness_disagreement_lets_postburn_complete(tmp_path, monkeypatch):
    finalization, record, audit, final_srt, kwargs = _run_postburn(tmp_path, monkeypatch)
    calls = []
    _postburn_llm(monkeypatch, calls)
    preburn = __import__("copy").deepcopy(audit["final_review_audit"])
    with pytest.raises(SystemExit):
        finalization._run_postburn_subtitle_audio_review(**kwargs)
    decided = dict(audit["postburn_subtitle_audio_review"]["findings"][0])
    sha = "sha256:" + __import__("hashlib").sha256(final_srt.read_bytes()).hexdigest()
    audit["final_review_audit"] = preburn
    audit.pop("postburn_subtitle_audio_review", None)
    kwargs["typed_resolution_evidence"] = {"reviewed_srt_sha256": sha, "resolved_findings": [decided]}
    kwargs["record"] = record
    calls.clear()
    finalization._run_postburn_subtitle_audio_review(**kwargs)
    review = audit["postburn_subtitle_audio_review"]
    assert review["release_gate"] == "PASS" and review["findings"] == []
    assert review["witness_disagreement"]["decided_nomination_ids"] == [
        decided["candidate_provenance"]["nomination_id"]]
    assert "witness_disagreement_sha256" not in review["cpa_request"]
