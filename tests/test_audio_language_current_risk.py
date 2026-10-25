"""Discovery -> saved authority -> ordinary current risk, with no real provider."""
import hashlib
import json
from copy import deepcopy

import pytest

from src.autoslice import audio_language_discovery as discovery
from src.autoslice import foreign_span_witness as foreign
from src.autoslice import producer_text_pipeline as pipeline
from src.autoslice import final_review_auditor as auditor
from src.autoslice import producer_package_finalization as finalization
from tests.test_audio_language_discovery import srt, scan
from tests.test_producer_text_pipeline_final_review import _correction_pass, _adapters


def _ordinary_saved_discovery(tmp_path, monkeypatch, text):
    media = tmp_path / "padded.mp4"
    media.write_bytes(b"exact shared producer media")
    media_sha = "sha256:" + hashlib.sha256(media.read_bytes()).hexdigest()
    monkeypatch.setattr(discovery, "scan_audio_languages", lambda **_kwargs:
                        scan(anomalies=[{"cue_index": 2, "reason": "audio/text semantic conflict"}]))
    monkeypatch.setattr(foreign, "_witness_rows", lambda **kwargs: [
        {**row, "exact_transcript": "", "audible_language": "uncertain",
         "audio_sha256": "a" * 64, "failure": "local observation unresolved"}
        for row in kwargs["rows"]])
    def no_call(_prompt):
        pytest.fail("unusable observation dispatched a proposal")
    monkeypatch.setattr(pipeline, "_build_final_review_llm_call", lambda: no_call)
    authority = {"status": "PASS", "source_truth_preview_receipts": {
        stage: {"schema_version": "source-truth-deterministic-preview.v1", "ledger_sha256": None}
        for stage in ("pre_entity_arbitration", "pre_correction_review")}}
    result = pipeline._finalize_text_evidence(
        spec={"pieces": []}, durations=[], srt_text=text, chat_authority_audit=authority,
        transcript_entity_audit={"status": "PASS"}, referent_groups=[], final_review_audit={},
        song_name_candidates=[], known_songs_path=tmp_path / "songs.json", session_topic_authorities=(),
        source_language_witness_srt=text, text_override_path=None, source_truth_ledger_path=None,
        out_root=tmp_path, cid="different-source", padded=media)
    assert result.srt_text == text
    assert authority["audio_language_discovery_audit"]["status"] == "PENDING_CURRENT_RISK"
    assert all("content_risk" not in row for row in authority["audio_language_discovery_audit"]["disclosures"])
    # Reopen the actual normal producer's saved authority, rather than feeding
    # a fabricated discovery directly to the repaired consumer.
    saved = json.loads(result.chat_authority_path.read_bytes())
    assert saved["audio_language_discovery_audit"] == json.loads(
        (tmp_path / "different-source.audio-language-discovery.json").read_bytes())
    return saved, media_sha


@pytest.mark.parametrize("risk,words", [
    ("meaning_reversal", "他没有碰过那笔钱"),
    ("factual_error", "这台设备确实用了三十年"),
    ("potential_harm", "他说那个人在故意伤害别人"),
    ("noncritical_uncertainty", "谢谢小宇的灯牌"),
    ("noncritical_uncertainty", "嗯那个我们接着讲"),
])
def test_new_saved_discovery_reaches_exact_and_postburn_risk(tmp_path, monkeypatch, risk, words):
    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path))
    source = srt("前句", words, "后句")
    saved, media_sha = _ordinary_saved_discovery(tmp_path, monkeypatch, source)
    before = deepcopy(saved)
    # Final recut removes the first two seconds. The old cue2 is current cue1;
    # no intro duration is added here, and no old cue index is reused.
    current = srt(words, "后句")
    context, nominees = discovery.current_language_discovery_context(
        current, saved, source_media_sha256=media_sha, timeline_offset_ms=2000)
    assert [row["cue"] for row in nominees] == [1]
    assert nominees[0]["candidate_provenance"]["source_window_ms"] == [2000, 3000]
    prompts, acoustics = [], []
    def review(prompt):
        prompts.append(prompt)
        if len(prompts) == 1:
            return '{"findings":[]}'  # Independent R1 omits this new discovery.
        assert "audio/text semantic conflict" in prompt or "local observation unresolved" in prompt
        return json.dumps({"findings": [{"cue": 1, "content_risk": risk,
                                         "risk_basis": "当前句的具体冲突" if risk != "noncritical_uncertainty"
                                         else "名字或犹疑未知，未发现原意、事实或伤害错误"}]})
    def unresolved(text, finding, **_kwargs):
        acoustics.append(finding)
        return text, {"status": "UNCERTAIN", "repaired": False}
    monkeypatch.setattr(auditor, "adjudicate_context_finding", unresolved)
    monkeypatch.setattr(pipeline, "clip_context_prompt_text", lambda _: "")
    monkeypatch.setattr(pipeline, "discover_candidate_pronoun_findings", lambda *a, **k: ([], {}))
    exact = pipeline._run_exact_final_release_review(
        srt_text=current, correction_audit=_correction_pass(), adapters=_adapters(),
        authoritative_chat=(), selection_hook="", clip_context={}, verified_authority_audit=saved,
        source_media_sha256=media_sha, timeline_offset_ms=2000,
        final_review_llm=review, pronoun_audit_llm=lambda _: '{}')
    assert len(prompts) == 2
    assert exact["release_gate"] == ("PASS" if risk == "noncritical_uncertainty" else "BLOCK"), exact
    assert len(acoustics) == (0 if risk == "noncritical_uncertainty" else 1)
    # The very same saved evidence enters the postburn consumer after reopen.
    prompts.clear()
    rows = auditor.audit_final_subtitles(
        current, llm_call=review, extract_json=json.loads, candidate_context_text=context,
        extra_raw_findings=nominees, prioritize_extra_raw_findings=True)
    blocked, disclosed = finalization._postburn_current_risk_partition(rows)
    assert bool(blocked) is (risk != "noncritical_uncertainty")
    assert bool(disclosed) is (risk == "noncritical_uncertainty")
    assert saved == before


@pytest.mark.parametrize("damage", ["media", "hash", "srt", "window", "offset"])
def test_saved_discovery_rejects_drift_without_audio_dispatch(tmp_path, monkeypatch, damage):
    saved, media_sha = _ordinary_saved_discovery(tmp_path, monkeypatch, srt("上句", "未知声音", "下句"))
    audit = saved["audio_language_discovery_audit"]
    if damage == "media":
        media_sha = "sha256:" + "b" * 64
    elif damage == "hash":
        audit["audio_witness_rows"][0]["exact_transcript"] = "invented"
    elif damage == "srt":
        audit["output_srt"] += "different"
        audit["discovery_sha256"] = discovery._audit_sha(audit)
    elif damage == "window":
        audit["pending_current_risk"][0]["start_ms"] += 1
        audit["discovery_sha256"] = discovery._audit_sha(audit)
    with pytest.raises(ValueError, match="DISCOVERY_.*DRIFT"):
        discovery.current_language_discovery_context(
            srt("未知声音"), saved, source_media_sha256=media_sha,
            timeline_offset_ms=-1 if damage == "offset" else 2000)


def test_unchanged_other_cues_and_valid_replacement_keep_discovery_question(tmp_path, monkeypatch):
    saved, media_sha = _ordinary_saved_discovery(tmp_path, monkeypatch, srt("上句", "原来未知", "下句"))
    # A later legitimate text owner may split, join or replace this source
    # interval. Discovery asks current risk about the new words, never replays
    # an obsolete proposal or treats different text as proof of resolution.
    current = srt("已由正式真值恢复的当前句", "其他句变化")
    _, rows = discovery.current_language_discovery_context(
        current, saved, source_media_sha256=media_sha, timeline_offset_ms=2000)
    assert rows[0]["suspect"] == "已由正式真值恢复的当前句"
    assert rows[0]["proposed_full_cue"] is None
    assert discovery.current_language_discovery_context(
        srt("完全在已裁掉窗口之外"), saved, source_media_sha256=media_sha,
        timeline_offset_ms=9000) == ("", [])
    assert discovery.current_language_discovery_context(srt("无该发现"), {}, source_media_sha256=None) == ("", [])


@pytest.mark.parametrize("risk", ["meaning_reversal", "factual_error", "potential_harm", "noncritical_uncertainty"])
def test_real_postburn_entry_consumes_saved_discovery_and_replays_completed_risk(tmp_path, monkeypatch, risk):
    from tests.test_final_subtitle_audio_gate import _postburn_record, _postburn_chat_audit, _postburn_adapters
    record, subtitle, media = _postburn_record(tmp_path)
    saved, media_sha = _ordinary_saved_discovery(tmp_path, monkeypatch, subtitle.read_text())
    audit = _postburn_chat_audit(subtitle)
    preburn = deepcopy(audit["final_review_audit"])
    audit["audio_language_discovery_audit"] = saved["audio_language_discovery_audit"]
    calls = []
    _postburn_adapters(monkeypatch, "", calls)
    identity = finalization.build_final_review_llm_call()
    def llm(prompt):
        calls.append(prompt)
        if len(calls) == 1:
            return '{"findings":[]}'
        return json.dumps({"findings": [{"cue": 2, "content_risk": risk,
                                         "risk_basis": "当前句具体原意、事实或伤害冲突" if risk != "noncritical_uncertainty"
                                         else "正常未知而无具体原意、事实或伤害风险"}]})
    llm.cpa_cache_identity = identity.cpa_cache_identity
    llm.provider_runtime_binding = identity.provider_runtime_binding
    monkeypatch.setattr(finalization, "build_final_review_llm_call", lambda: llm)
    kwargs = dict(
        cid="candidate", out_root=tmp_path,
        recut=finalization.FinalRecutArtifacts(tmp_path, media, subtitle, None, None),
        record=record, chat_authority_audit=audit, chat_authority_path=tmp_path / "current-chat.json",
        source_evidence_spec={"source_piece_clock": {"padded_sha256": media_sha}},
    )
    protected = subtitle.read_bytes(), media.read_bytes(), deepcopy(audit["audio_language_discovery_audit"])
    for replay in (False, True):
        audit["final_review_audit"] = deepcopy(preburn)
        if replay:
            # Ordinary exact continuation restores its input PASS while
            # retaining the completed postburn receipt as historical evidence.
            audit["final_review_audit"]["postburn_subtitle_audio_review"] = deepcopy(
                audit["postburn_subtitle_audio_review"])
        if risk == "noncritical_uncertainty":
            finalization._run_postburn_subtitle_audio_review(**kwargs)
        else:
            with pytest.raises(SystemExit, match="POSTBURN_SUBTITLE_AUDIO_REVIEW_BLOCKED"):
                finalization._run_postburn_subtitle_audio_review(**kwargs)
        receipt = audit["postburn_subtitle_audio_review"]
        assert receipt["release_gate"] == ("PASS" if risk == "noncritical_uncertainty" else "BLOCK")
        assert receipt["cache_reused"] is replay
        assert len(calls) == 2
    assert (subtitle.read_bytes(), media.read_bytes(), audit["audio_language_discovery_audit"]) == protected


@pytest.mark.parametrize("all_critical", [False, True])
def test_discovered_critical_cue_cannot_fall_off_ordinary_action_cap(tmp_path, monkeypatch, all_critical):
    count = auditor.MAX_FINDINGS + 3
    text = srt(*(f"当前第{index}句" for index in range(1, count + 1)))
    media = tmp_path / "source.mp4"
    media.write_bytes(b"another source")
    monkeypatch.setattr(foreign, "_witness_rows", lambda **kwargs: [
        {**row, "exact_transcript": "", "failure": "unresolved"} for row in kwargs["rows"]])
    discovery.repair_audio_language_candidates(
        media_path=media, srt_text=text, out_root=tmp_path, cid="longer", authority={}, llm_call=None,
        scan_call=lambda **_: scan(anomalies=[{"cue_index": i, "reason": "semantic anomaly"}
                                            for i in range(1, count + 1)]))
    saved = {"audio_language_discovery_audit": json.loads((tmp_path / "longer.audio-language-discovery.json").read_bytes())}
    context, rows = discovery.current_language_discovery_context(
        text, saved, source_media_sha256="sha256:" + hashlib.sha256(media.read_bytes()).hexdigest())
    calls = []
    def risk(prompt):
        calls.append(prompt)
        if len(calls) == 1:
            return '{"findings":[]}'
        return json.dumps({"findings": [{"cue": index,
            "content_risk": "factual_error" if all_critical or index == count else "noncritical_uncertainty",
            "risk_basis": "当前具体事实矛盾" if all_critical or index == count else "仅未知，无具体冲突"}
            for index in range(1, count + 1)]})
    kwargs = dict(llm_call=risk, extract_json=json.loads, candidate_context_text=context,
                  extra_raw_findings=rows, prioritize_extra_raw_findings=True)
    if all_critical:
        with pytest.raises(auditor.FinalReviewAuditError, match="LANGUAGE_CRITICAL_AGENDA_INCOMPLETE"):
            auditor.audit_final_subtitles(text, **kwargs)
    else:
        findings = auditor.audit_final_subtitles(text, **kwargs)
        assert findings[0]["cue_index"] == count
        assert findings[0]["content_risk"] == "factual_error"
    assert len(calls) == 2
