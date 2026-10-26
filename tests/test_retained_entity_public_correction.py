"""Exercise the ordinary correction parser/router before consuming retained doubts.

Only provider boundaries are stubbed. No completed adjudication is supplied to
the gate; risk, nomination, proposal, witness and CPA receipts are built by the
normal implementation from raw responses.
"""
from copy import deepcopy
import hashlib
import json
import socket

import pytest

from src.autoslice import producer_text_pipeline as pipeline
from src.autoslice.speech_memory_ledger import SCHEMA_VERSION as SPEECH_MEMORY_SCHEMA
from src.autoslice.retained_reviewed_draft import (
    RetainedReviewedDraftError, validate_pending_entity_checkpoint, retained_entity_handoff_findings,
)
from tests.test_span_phoneme_resolution import Observers
from tests.test_producer_text_pipeline_final_review import _adapters
from tests.test_retained_entity_input_recovery import CURRENT, _pending, _consume
from src.autoslice.retained_reviewed_draft import pending_entity_review_findings


@pytest.fixture(autouse=True)
def offline_contract(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path / "runtime"))

    def no_network(*_args, **_kwargs):
        pytest.fail("Public correction contract must use only the normal stub providers")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)


def _normal_review(tmp_path, monkeypatch, retained, context, *, candidate, final_audio=False,
                   final_choice="CURRENT", supported=True, risk_mode="complete", runner=None):
    memory = tmp_path / "speech-memory.json"
    memory.write_text(json.dumps({"schema_version": SPEECH_MEMORY_SCHEMA, "entries": []}))
    context = pipeline.build_clip_context(
        candidate_id=context["candidate_id"],
        spec={"date": context["recording_date"], "pieces": context["pieces"],
              "selection_hook": "Synthetic fixture: wen zi is one named item, spelled 文字 or 文子; retain that same item."},
        draft_srt=CURRENT, authoritative_chat=(), topic_resolution={},
        session_topic_authorities=(), speech_memory_ledger_path=memory,
    )
    events = []
    witness_requests = []
    observer = Observers(heard="bao liu wen zi")

    def cpa(prompt):
        if not events:
            events.append("discovery")
            # A complete empty discovery must not erase the priority nomination.
            return json.dumps({"findings": []})
        if "本次不授权改字、删字或听音调度" in prompt:
            events.append("rebuilt_risk" if "CORRECTION_REBUILT_CURRENT_RISK" in prompt else "current_risk")
            reply = {"findings": [{
                "cue": 1, "content_risk": "noncritical_uncertainty",
                "risk_basis": "The fixture names one item; only its spelling is uncertain.",
                "meaning_equivalence": {
                    "status": "EQUIVALENT",
                    "current_meaning": "Retain the fixture named item wen zi.",
                    "alternative_meaning": "Retain the same fixture named item wen zi.",
                    "shared_meaning": "The same retain action and the same named object.",
                    "uncertain_detail": "Only the spelling of the fixture item name.",
                    "context_basis": "The whole-clip selection hook binds both spellings to one fixture item.",
                    "evidence_cue_ids": [1],
                },
            }]}
            if "CORRECTION_REBUILT_CURRENT_RISK" in prompt:
                if risk_mode == "empty":
                    reply["findings"] = []
                elif risk_mode == "critical":
                    reply["findings"][0].update(content_risk="meaning_reversal", meaning_equivalence=None)
                elif risk_mode == "invalid_equivalence":
                    reply["findings"][0].pop("meaning_equivalence")
            return json.dumps(reply)
        if "# 字幕缺失候选重建" in prompt:
            events.append("proposal")
            return json.dumps({
                "status": "PROPOSED" if candidate else "UNRESOLVED",
                "proposed_cue": "保留文子" if candidate else "",
                "topic_summary": "Retain the same named fixture item.",
                "needs_audio": False, "reason": "A bounded fixture proposal, without authority.",
            }, ensure_ascii=False)
        if "# 字幕缺失候选最终收敛" in prompt:
            events.append("convergence")
            return json.dumps({"decision": "UNRESOLVED", "replacement_text": "",
                               "needs_audio": True, "reason": "A current listen is required."})
        if "TEXT_FIRST" in prompt:
            events.append("text_first")
            return json.dumps({"choice": "CURRENT", "needs_audio": True,
                               "reason": "The retained question still needs actual audio."})
        assert "CURRENT" in prompt, "Unexpected provider phase"
        events.append("audio_judge")
        reply = {
            "choice": final_choice, "needs_audio": final_audio,
            "reason": "Decision after the bound candidate-blind observation.",
            "current_utterance_supported": supported,
            "current_utterance_support_reason": "The complete current fixture utterance is audible.",
        }
        if "\nSPAN_REVIEW_INPUT\n" in prompt:
            data = json.loads(prompt.split("\nSPAN_REVIEW_INPUT\n", 1)[1].split(
                "\nEND_SPAN_REVIEW_INPUT\n", 1)[0])
            reply["span_review"] = {
                "binding_sha256": data["binding"]["binding_sha256"],
                "request_sha256": data["request_sha256"],
                "context_basis": "The observed fixture syllable supports the current item name.",
                "all_audio_questions_located": True,
                "target_needs_audio": final_audio,
                "alignments": [{"observation_id": row["observation_id"],
                                "span": [3, 4], "quote": "zi"}
                               for row in data["observations"] if row["kind"] == "blind_pinyin"],
                "audio_questions": [],
            }
        return json.dumps(reply)

    def witness(request):
        events.append("witness")
        witness_requests.append(deepcopy(request))
        assert "proposed_cue" not in request and "current_cue" not in request
        return observer(request)

    raw_calls = []

    def recorded_cpa(prompt):
        raw = cpa(prompt)
        raw_calls.append({"phase": events[-1], "raw_completion": raw,
                          "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest()})
        (tmp_path / "provider-replies.json").write_text(json.dumps(raw_calls, ensure_ascii=False, indent=2))
        return raw

    monkeypatch.setattr(pipeline, "_build_final_review_llm_call", lambda: recorded_cpa)
    if runner is None:
        output, review = pipeline._run_final_review(
            srt_text=CURRENT, chat_authority_audit={}, handled_entity_cues=set(),
            verify_confusable_entity=witness, adapters=_adapters(), clip_context=context,
            priority_raw_findings=pending_entity_review_findings(retained, srt_text=CURRENT),
            initial_provider_adjudication_count=50,
        )
    else:
        output, review = runner(context, witness)
    return output, review, events, witness_requests


@pytest.mark.parametrize("candidate", [False, True])
def test_ordinary_correction_consumes_current_audio_and_cold_replays(
    tmp_path, monkeypatch, candidate,
):
    retained, original, context = _pending(tmp_path)
    before = deepcopy(original)
    output, review, events, witnesses = _normal_review(
        tmp_path, monkeypatch, retained, context, candidate=candidate,
    )
    (tmp_path / "public-correction-review.json").write_text(
        json.dumps({"review": review, "events": events}, ensure_ascii=False, indent=2))
    assert review["status"] != "AUDITOR_UNAVAILABLE", review
    assert events[:2] == ["discovery", "current_risk"]
    assert "proposal" in events and len(witnesses) == 1
    assert events.index("witness") < events.index("audio_judge")
    assert events.count("rebuilt_risk") == int(candidate)
    authority = _consume(tmp_path, retained, review, output=output)
    validate_pending_entity_checkpoint(
        retained, {"chat_authority": json.loads(json.dumps(authority))})
    assert authority["retained_entity_review"]["status"] == "CURRENT_REVIEW_COMPLETED"
    assert authority["retained_entity_review"]["release_authorized"] is False
    assert original == before


@pytest.mark.parametrize("options", [
    {"final_audio": True}, {"final_choice": "NEITHER"}, {"supported": False},
])
def test_ordinary_correction_does_not_discharge_unresolved_audio(tmp_path, monkeypatch, options):
    retained, _, context = _pending(tmp_path)
    output, review, events, _ = _normal_review(
        tmp_path, monkeypatch, retained, context, candidate=False, **options)
    assert "current_risk" in events and "witness" in events
    if options.get("supported") is False:
        # A claimed KEEP without its required support is not a valid completion.
        with pytest.raises(RetainedReviewedDraftError, match="PENDING_ENTITY_CURRENT_REVIEW_REQUIRED"):
            _consume(tmp_path, retained, review, output=output)
        return
    # F7: an actual current unresolved adjudication proceeds to G as an
    # obligation; it is not discharged by correction or granted release.
    authority = _consume(tmp_path, retained, review, output=output)
    validate_pending_entity_checkpoint(retained, {"chat_authority": authority})
    pointer = authority["retained_entity_review"]
    assert pointer["carried_to_exact_final_count"] == 1 and pointer["release_authorized"] is False
    carried = retained_entity_handoff_findings(output, authority)
    assert len(carried) == 1
    assert carried[0]["candidate_provenance"]["handoff_reason_codes"][0] == "CURRENT_REVIEW_UNRESOLVED_CARRIED"


@pytest.mark.parametrize("risk_mode", ["empty", "critical", "invalid_equivalence"])
def test_rebuilt_candidate_requires_a_new_complete_risk_without_losing_usage(
    tmp_path, monkeypatch, risk_mode,
):
    retained, _, context = _pending(tmp_path)
    output, review, events, witnesses = _normal_review(
        tmp_path, monkeypatch, retained, context, candidate=True, risk_mode=risk_mode)
    assert events[-1] == "rebuilt_risk" and len(witnesses) == 1
    assert review["cpa_adjudication_usage"]["provider_adjudication_count"] == 51
    row = review["findings"][0]
    assert row["context_audio_adjudication"]["verdict"]["status"] == "OBSERVED"
    refresh = row["rebuilt_current_risk_review"]
    assert refresh["completions"] and refresh["release_authorized"] is False
    if risk_mode == "critical":
        assert row["content_risk"] == "meaning_reversal"
        assert row["prior_current_content_risks"][0]["content_risk"] == "noncritical_uncertainty"
    else:
        assert review["status"] == "AUDITOR_UNAVAILABLE" and review["release_gate"] == "BLOCK"
    with pytest.raises(RetainedReviewedDraftError, match="PENDING_ENTITY_CURRENT_REVIEW_REQUIRED"):
        _consume(tmp_path, retained, review, output=output)


@pytest.mark.parametrize("change", ["raw_choice", "source", "clock", "candidate", "witness", "other_entity", "empty_clean"])
def test_normal_generated_receipt_rejects_corruption(tmp_path, monkeypatch, change):
    retained, _, context = _pending(tmp_path)
    output, review, _, _ = _normal_review(tmp_path, monkeypatch, retained, context, candidate=False)
    row = review["findings"][0]
    audit = row["context_audio_adjudication"]
    request = audit["request"]
    judge = audit["witness_judge"]["judge"]
    if change == "raw_choice":
        raw = json.loads(judge["span_review_raw_completion"])
        raw["choice"] = "NEITHER"
        judge["span_review_raw_completion"] = json.dumps(raw)
        judge["completion_sha256"] = hashlib.sha256(judge["span_review_raw_completion"].encode()).hexdigest()
    elif change == "source":
        request["clip_context_binding"]["source_media_sha256s"] = ["sha256:" + "b" * 64]
    elif change == "clock":
        request["source_media_timeline_offset_ms"] = 29950
    elif change == "candidate":
        request["clip_context_binding"]["candidate_id"] = "another-candidate"
    elif change == "witness":
        audit["verdict"]["request_sha256"] = "c" * 64
    elif change == "other_entity":
        request["suspect"] = "保留"
    else:
        review.update(status="CLEAN", findings=[])
    if change in {"source", "clock", "candidate", "other_entity"}:
        request["request_sha256"] = hashlib.sha256(json.dumps(
            {k: v for k, v in request.items() if k != "request_sha256"},
            ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    with pytest.raises(RetainedReviewedDraftError, match="PENDING_ENTITY_CURRENT_REVIEW_REQUIRED"):
        _consume(tmp_path, retained, review, output=output)


def test_normal_completed_checkpoint_cannot_be_rehashed_to_empty_clean(tmp_path, monkeypatch):
    retained, _, context = _pending(tmp_path)
    output, review, _, _ = _normal_review(tmp_path, monkeypatch, retained, context, candidate=True)
    authority = _consume(tmp_path, retained, review, output=output)
    reference = authority["retained_entity_review"]
    from pathlib import Path
    path = Path(reference["path"])
    saved = json.loads(path.read_text())
    saved["normal_correction_review"].update(status="CLEAN", findings=[])
    path.write_text(json.dumps(saved))
    reference["sha256"] = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(RetainedReviewedDraftError, match="PENDING_ENTITY"):
        validate_pending_entity_checkpoint(retained, {"chat_authority": authority})


@pytest.mark.parametrize("candidate", [False, True])
@pytest.mark.parametrize("final_audio", [False, True])
def test_producer_entry_consumes_pending_before_materialization(
    tmp_path, monkeypatch, candidate, final_audio,
):
    """Public orchestration uses the same admitted question and raw providers.

    Upstream media and retained-loader IO are isolated fixtures. Nomination,
    correction parsing/routing, candidate bootstrap, witness/CPA consumption
    and the checkpoint gate all run unchanged. Stop at the later materializer;
    this test does not grant final content or release acceptance.
    """
    from dataclasses import replace
    from types import SimpleNamespace
    from src.autoslice import retained_reviewed_draft as retained_module

    retained, original, context = _pending(tmp_path)
    loaded = {**retained, "srt_text": CURRENT, "parent_authority": deepcopy(original)}
    loaded["parent_authority"]["cpa_adjudication_usage"] = {"provider_adjudication_count": 50}
    before = deepcopy(original)
    padded = tmp_path / "padded.mp4"
    padded.write_bytes(b"isolated synthetic fixture media")
    out_root = tmp_path / "producer"
    out_root.mkdir()
    reached = []

    class MaterializationReached(Exception):
        pass

    def before_materialization(**kwargs):
        reached.append(kwargs)
        raise MaterializationReached

    monkeypatch.setattr(retained_module, "load_retained_reviewed_draft", lambda **_kwargs: loaded)
    monkeypatch.setattr(pipeline, "_collect_timeline_chat", lambda *_args: ([], []))
    monkeypatch.setattr(pipeline, "build_env_screen_read_probe", lambda *_args: None)
    monkeypatch.setattr(pipeline, "build_ssh_silero_vad_provider",
                        lambda _host: lambda *_args: [SimpleNamespace(start_ms=10, end_ms=30)])
    monkeypatch.setattr(pipeline, "_apply_entity_authority", lambda **kwargs:
                        pipeline.EntityAuthorityResult(kwargs["srt_text"], {}, {}, set()))
    monkeypatch.setattr(pipeline, "_finalize_text_evidence", before_materialization)

    def runner(full_context, witness):
        monkeypatch.setattr(pipeline, "_build_entity_verification_context", lambda **_kwargs:
                            pipeline.EntityVerificationContext(witness, [], {}))
        adapters = replace(_adapters(), load_term_boundary_surfaces=lambda _spec: [],
                           profile_asset_file=lambda name: tmp_path / (
                               "speech-memory.json" if name == "speech_memory_ledger" else name))
        arguments = dict(
            spec={"date": context["recording_date"], "human_truth_mode": "withheld",
                  "pieces": context["pieces"], "retained_current_text": {"isolated_fixture": True},
                  "selection_hook": full_context["selection_hook"]},
            durations=[326540], padded=padded, padded_dur=326540, host="fixture",
            text_override_path=None, cid="candidate-1", out_root=out_root,
            substrate="aggregate_asr", correct="cpa", screen_text=False, adapters=adapters,
        )
        with pytest.raises(MaterializationReached):
            pipeline.run_text_pipeline(**arguments)
        assert len(reached) == 1
        authority = reached[0]["chat_authority_audit"]
        assert authority["retained_entity_review"]["status"] == "CURRENT_REVIEW_COMPLETED"
        assert authority["retained_entity_review"]["release_authorized"] is False
        carried = retained_entity_handoff_findings(reached[0]["srt_text"], authority)
        assert len(carried) == int(final_audio)
        assert authority["cpa_adjudication_usage"]["provider_adjudication_count"] == 51
        validate_pending_entity_checkpoint(loaded, {"chat_authority": json.loads(json.dumps(authority))})
        return reached[0]["srt_text"], reached[0]["final_review_audit"]

    output, review, events, witnesses = _normal_review(
        tmp_path, monkeypatch, retained, context, candidate=candidate,
        final_audio=final_audio, runner=runner)
    assert output == CURRENT and "proposal" in events and witnesses
    assert original == before


@pytest.mark.parametrize("damage", [None, "missing_completion", "rehash_empty_clean", "raw_audio_pending"])
def test_public_cold_continuation_checks_saved_consumption_before_resume(
    tmp_path, monkeypatch, damage,
):
    """Snapshot IO supplies serialized evidence; the public cold gate stays live."""
    from src.autoslice import retained_reviewed_draft as retained_module

    retained, _, context = _pending(tmp_path)
    output, review, events, _ = _normal_review(
        tmp_path, monkeypatch, retained, context, candidate=True)
    authority = _consume(tmp_path, retained, review, output=output)
    from pathlib import Path
    reference = authority["retained_entity_review"]
    if damage == "missing_completion":
        authority.pop("retained_entity_review")
    elif damage is not None:
        path = Path(reference["path"])
        saved = json.loads(path.read_text())
        if damage == "rehash_empty_clean":
            saved["normal_correction_review"].update(status="CLEAN", findings=[])
        else:
            judge = saved["normal_correction_review"]["findings"][0]["context_audio_adjudication"]["witness_judge"]["judge"]
            raw = json.loads(judge["span_review_raw_completion"])
            raw["needs_audio"] = True
            judge["span_review_raw_completion"] = json.dumps(raw)
            judge["completion_sha256"] = hashlib.sha256(judge["span_review_raw_completion"].encode()).hexdigest()
        path.write_text(json.dumps(saved))
        reference["sha256"] = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    cold = tmp_path / "cold"
    cold.mkdir()
    snapshot_path = cold / "candidate-1.current-text-checkpoint.json"
    snapshot_path.write_text(json.dumps({"chat_authority": authority}))
    padded = tmp_path / "cold-padded.mp4"
    padded.write_bytes(b"isolated media fixture")
    loaded = {**retained, "srt_text": CURRENT}
    resumed = []
    sentinel = object()
    monkeypatch.setattr(retained_module, "load_retained_reviewed_draft", lambda **_kwargs: loaded)
    monkeypatch.setattr(pipeline, "_collect_timeline_chat", lambda *_args: ([], []))
    monkeypatch.setattr(pipeline, "_automatic_current_checkpoint_snapshot", lambda **_kwargs:
                        json.loads(snapshot_path.read_text()))
    monkeypatch.setattr(pipeline, "_automatic_current_text_checkpoint", lambda **_kwargs: sentinel)

    def resume(**kwargs):
        resumed.append(kwargs)
        return sentinel

    monkeypatch.setattr(pipeline, "_run_current_text_continuation", resume)
    provider_events_before = list(events)
    arguments = dict(
        spec={"date": context["recording_date"], "human_truth_mode": "withheld",
              "pieces": context["pieces"], "retained_current_text": {"isolated_fixture": True}},
        durations=[326540], padded=padded, padded_dur=326540, host="fixture",
        text_override_path=None, cid="candidate-1", out_root=cold,
        substrate="aggregate_asr", correct="cpa", screen_text=False, adapters=_adapters(),
    )
    if damage:
        with pytest.raises(RetainedReviewedDraftError, match="PENDING_ENTITY"):
            pipeline.run_text_pipeline(**arguments)
        assert resumed == []
    else:
        assert pipeline.run_text_pipeline(**arguments) is sentinel
        assert len(resumed) == 1 and resumed[0]["continuation"] is sentinel
    assert events == provider_events_before
