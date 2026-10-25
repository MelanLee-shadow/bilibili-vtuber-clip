"""Normal producer -> actual adjudicator -> persisted repair -> cold gate."""

from copy import deepcopy
import json
import socket
import subprocess
import sys

import pytest

from src.autoslice import producer_text_pipeline as pipeline
from src.autoslice import producer_package_finalization as finalizer
from src.autoslice.final_review_contract import (
    FinalReviewContractError,
    validate_final_review_release,
)
from src.autoslice.final_review_auditor import audit_correction_mutation_authority
from src.autoslice.review_evidence_store import load_review
from src.autoslice.span_finding_obligations import (
    pending_findings,
    derive_obligations,
    enforce_review,
    validate_release_obligations,
)
from src.autoslice.span_phoneme_resolution import FIELD, POLICY
from tests.test_span_phoneme_resolution import Observers, model_for, run_case, srt, sha
from tests.test_producer_text_pipeline_final_review import (
    _adapters,
    _correction_pass,
    _literal_review_llm,
)


@pytest.fixture(autouse=True)
def offline(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path / "runtime"))

    def forbidden(*args, **kwargs):
        pytest.fail("Producer scope fixtures cannot reach network")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def boundary_correction():
    correction = _correction_pass()
    boundary = correction["boundary_semantic_review"]
    boundary["source_separation_witness"]["source_final_end_ms"] = 4000
    endpoint = boundary["final_endpoint_binding"]
    endpoint.update(
        recommended_end_cue_index=1,
        recommended_end_ms=4000,
        final_closure_cue_index=1,
        final_snapped_end_ms=4000,
        final_end_ms=4000,
    )
    return correction


def clip_context():
    from src.autoslice.clip_context import _canonical_json_sha256

    source = srt("你想，甲0呀")
    context = {
        "schema_version": "lidousha-clip-context.v1",
        "candidate_id": "span-test",
        "recording_date": "2026-10-16",
        "mutation_authorized": False,
        "whole_clip_draft_srt": source,
        "whole_clip_draft_srt_sha256": "sha256:" + sha(source),
        "pieces": [{"source_media_sha256": "sha256:" + "a" * 64, "start_ms": 0, "end_ms": 4000}],
        "retrieval_budget": {"whole_clip_transcript_truncated": False},
    }
    context["context_sha256"] = _canonical_json_sha256(context)
    return context


def actual_producer_review(
    text,
    authority,
    verifier,
    *,
    hide_after_repair=True,
    snapshots=None,
    target_only=False,
    discover=True,
):
    word_model = model_for(
        target_quote="零",
        blind_quote="ling",
        question_span=[4, 5] if target_only else [0, 2],
        outside=not target_only,
    )
    raw = []
    if "甲0" in text and discover:
        raw = [
            {
                "cue": 1,
                "kind": "context",
                "repair_class": "phonetic",
                "suspect": "甲0",
                "suggestion": "甲零",
                "source_surface": "甲零",
                "span_start_codepoint": 3,
                "span_end_codepoint": 5,
                "proposed_full_cue": "你想，甲零呀",
                "why": "Synthetic target-name spelling is uncertain.",
                "content_risk": "meaning_reversal",
                "risk_basis": "Synthetic spelling question must be judged.",
            }
        ]

    def model(prompt):
        if "\nSPAN_REVIEW_INPUT\n" in prompt or "候选排序与这个补听决定分别填写" in prompt:
            return word_model(prompt)
        if "字幕缺失候选" in prompt or "字幕坏闭集" in prompt:
            return json.dumps(
                {
                    "status": "UNRESOLVED",
                    "decision": "UNRESOLVED",
                    "needs_audio": True,
                    "proposed_cue": "",
                    "replacement_text": "",
                    "reason": "Unresolved prefix remains.",
                }
            )
        return json.dumps({"findings": raw}, ensure_ascii=False)

    audit = pipeline._run_exact_final_release_review(
        srt_text=text,
        correction_audit=boundary_correction(),
        adapters=_adapters(),
        authoritative_chat=(),
        selection_hook="",
        clip_context=clip_context(),
        verify_confusable_entity=verifier,
        verified_authority_audit=authority,
        final_review_llm=_literal_review_llm(model),
        pronoun_audit_llm=lambda _: '{"occurrences": []}',
    )
    if snapshots is not None:
        snapshots.append(deepcopy(audit))
    return audit


def applied_history(*, question_span=(0, 3)):
    # State the synthetic audible question independently of edit minimization.
    # A different actual question remains distinct and must survive unchanged.
    output, adjudication, _, _, finding = run_case(
        model=model_for(question_span=list(question_span))
    )
    row = {**finding, "exact_release_adjudication": adjudication}
    produced, repairs = finalizer._apply_exact_final_cpa_repairs(
        srt("你想，甲0呀"), {"findings": [row]}
    )
    assert produced == output and len(repairs) == 1
    history = {
        "schema_version": "exact-final-cpa-self-heal-audit.v1",
        "status": "REVIEW_PENDING",
        "passes": [
            {
                "schema_version": "exact-final-cpa-self-heal-pass.v1",
                "pass_index": 1,
                "input_srt_sha256": "sha256:" + sha(srt("你想，甲0呀")),
                "output_srt_sha256": "sha256:" + sha(output),
                "repairs": repairs,
            }
        ],
    }
    return produced, {"exact_final_cpa_self_heal": history}, repairs


def test_mutation_consumer_accepts_new_resolution_but_not_relabelled_or_stale_evidence():
    _, adj, _, _, finding = run_case()
    record = {
        "status": "APPLIED",
        "applied_count": 1,
        "findings": [
            {
                **finding,
                "routed": "context_audio_adjudicated_fix",
                "context_audio_adjudication": adj,
            }
        ],
    }
    assert audit_correction_mutation_authority(record)["status"] == "PASS"
    for damage in ("outside", "request", "needs_audio", "missing"):
        changed = deepcopy(record)
        audit = changed["findings"][0]["context_audio_adjudication"]
        if damage == "outside":
            audit[FIELD]["outside_findings"] = []
        elif damage == "request":
            audit["request"]["matched_start_ms"] += 1
        elif damage == "needs_audio":
            audit["witness_judge"]["judge"]["needs_audio_requested"] = False
        else:
            audit.pop(FIELD)
        assert audit_correction_mutation_authority(changed)["status"] == "BLOCK"


def test_persisted_outside_question_survives_cold_empty_review_and_real_release_gate(tmp_path):
    text, authority, repairs = applied_history()
    path = tmp_path / "authority.json"
    path.write_text(json.dumps(authority, ensure_ascii=False))
    recovered = json.loads(path.read_text())
    pending = pending_findings(text, recovered)
    assert len(pending) == 1 and pending[0]["suspect"] == "你想，"
    assert pending[0]["span_start_codepoint"] == 0 and pending[0]["span_end_codepoint"] == 3
    assert pending[0]["origin_span_obligation"]["source"]["questions"]
    attempted = {
        "schema_version": "final-review-audit.v2",
        "reviewed_srt_sha256": "sha256:" + sha(text),
        "status": "CLEAN",
        "release_gate": "PASS",
        "findings": [],
        "validated_finding_count": 0,
    }
    audit = enforce_review(text, recovered, attempted)
    assert audit["release_gate"] == "BLOCK" and len(audit["findings"]) == 1
    with pytest.raises(ValueError, match="FINAL_REVIEW_FINDING_REVIEW_PENDING"):
        validate_release_obligations(audit)
    authority["exact_final_cpa_self_heal"]["passes"][0]["repairs"][0][FIELD][
        "outside_findings"
    ] = []
    with pytest.raises(ValueError):
        pending_findings(text, authority)


def test_ordinary_pipeline_retains_split_question_when_discovery_returns_empty():
    text, authority, _ = applied_history()
    audit = actual_producer_review(text, authority, Observers())
    assert audit["release_gate"] == "BLOCK"
    assert any(row.get("origin_span_obligation") for row in audit["findings"]), audit.get(
        "discovery"
    )


def test_actual_finalizer_applies_target_but_writes_unresolved_prefix_and_cold_replay_blocks(
    tmp_path,
):
    path = tmp_path / "clip.srt"
    path.write_text(srt("你想，甲0呀"))
    authority = {}
    authority_path = tmp_path / "authority.json"
    verifier = Observers()
    snapshots = []

    def review(text, previous, start, end):
        return actual_producer_review(text, previous, verifier, snapshots=snapshots)

    with pytest.raises(SystemExit, match="FINAL_REVIEW_RELEASE_BLOCKED"):
        finalizer._run_exact_final_review_gate(
            cid="span-test",
            out_root=tmp_path,
            final_start=0,
            final_end=4000,
            recut=finalizer.CurrentTextReviewArtifacts(subtitle_path=path),
            chat_authority_audit=authority,
            chat_authority_path=authority_path,
            run_exact_final_review=review,
            clip_context=clip_context(),
        )
    assert path.read_text() == srt("你想，甲零呀"), [
        (s.get("reason_codes"), len(s.get("findings", []))) for s in snapshots
    ]
    saved = load_review(authority_path)
    pending = pending_findings(path.read_text(), saved)
    assert pending and pending[0]["suspect"] == "你想"
    raw_repair = saved["exact_final_cpa_self_heal"]["passes"][0]["repairs"][0]
    assert raw_repair["judge"]["needs_audio_requested"] is True
    assert raw_repair["policy_branch"] == POLICY
    assert raw_repair[FIELD]["outside_findings"]
    before = path.read_bytes()
    with pytest.raises(SystemExit, match="FINAL_REVIEW_RELEASE_BLOCKED"):
        finalizer._run_exact_final_review_gate(
            cid="span-test",
            out_root=tmp_path,
            final_start=0,
            final_end=4000,
            recut=finalizer.CurrentTextReviewArtifacts(subtitle_path=path),
            chat_authority_audit=saved,
            chat_authority_path=authority_path,
            run_exact_final_review=review,
            clip_context=clip_context(),
        )
    assert path.read_bytes() == before
    final = load_review(authority_path)["final_review_audit"]
    with pytest.raises(FinalReviewContractError):
        validate_final_review_release(final, expected_srt_sha256="sha256:" + sha(before))
    # A fresh interpreter has no in-memory adjudicator/closure objects. It
    # must reconstruct the pending finding and the real gate from saved bytes.
    cold = subprocess.run(
        [
            sys.executable, "-B", "-c",
            "from pathlib import Path; import json,sys; "
            "from src.autoslice.span_finding_obligations import pending_findings; "
            "from src.autoslice.final_review_contract import validate_final_review_release; "
            "from src.autoslice.review_evidence_store import load_review; "
            "text=Path(sys.argv[1]).read_text(); authority=load_review(Path(sys.argv[2])); "
            "pending=pending_findings(text,authority); assert pending; "
            "print('COLD_PENDING',len(pending),flush=True); "
            "validate_final_review_release(authority['final_review_audit'])",
            str(path), str(authority_path),
        ],
        text=True, capture_output=True, timeout=30, check=False,
    )
    assert cold.returncode != 0 and "COLD_PENDING" in cold.stdout
    assert "FINAL_REVIEW_ENGINEERING_FAILURE" in cold.stderr  # open after the terminal stage: engineering, not content
    assert path.read_bytes() == before


def test_independent_prefix_can_be_resolved_by_another_real_typed_adjudication():
    from src.autoslice.final_review_auditor import adjudicate_context_finding

    text, authority, repairs = applied_history()
    question = pending_findings(text, authority)[0]
    finding = {**question, "suggestion": "你很想，", "proposed_full_cue": "你很想，甲零呀"}
    verifier = Observers(
        heard="ni hen xiang jia ling ya",
        transcripts={"mai": "你很想，甲零呀", "moss": "你很想，甲零呀"},
    )

    def model(prompt):
        # Text first cannot close a specifically audible outstanding question.
        needs = "未调用音频证人" in prompt or "text_first" in prompt or "CURRENT_TEXT" in prompt
        if "\nSPAN_REVIEW_INPUT\n" in prompt:
            data = json.loads(
                prompt.split("\nSPAN_REVIEW_INPUT\n", 1)[1].split("\nEND_SPAN_REVIEW_INPUT\n", 1)[0]
            )
            needs = not data["observations"]
        return json.dumps(
            {
                "choice": "PROPOSED",
                "candidate_id": "PROPOSAL",
                "needs_audio": needs,
                "reason": "New target-bound audio resolves the independent prefix.",
            }
        )

    output, adjudication = adjudicate_context_finding(
        text,
        finding,
        entity_verifier=verifier,
        judge_llm_call=model,
        require_complete_utterance_support=True,
    )
    assert output == srt("你很想，甲零呀") and adjudication["repaired"]
    finding["exact_release_adjudication"] = adjudication
    produced, next_repairs = finalizer._apply_exact_final_cpa_repairs(text, {"findings": [finding]})
    assert produced == output and next_repairs
    all_repairs = [*repairs, *next_repairs]
    states = derive_obligations(all_repairs)
    assert len(states) == 1 and states[0]["status"] == "RESOLVED"
    next_history = deepcopy(authority)
    next_history["exact_final_cpa_self_heal"]["passes"].append(
        {
            "schema_version": "exact-final-cpa-self-heal-pass.v1",
            "pass_index": 2,
            "input_srt_sha256": "sha256:" + sha(text),
            "output_srt_sha256": "sha256:" + sha(output),
            "repairs": next_repairs,
        }
    )
    audit = {
        "schema_version": "final-review-audit.v2",
        "reviewed_srt_sha256": "sha256:" + sha(output),
        "status": "CLEAN",
        "release_gate": "PASS",
        "findings": [],
        "validated_finding_count": 0,
    }
    enforce_review(output, next_history, audit)
    validate_release_obligations(audit)
    assert audit["release_gate"] == "PASS" and not audit["findings"]


def test_full_producer_release_can_close_target_only_without_rewriting_raw_needs_audio(tmp_path):
    path = tmp_path / "clip.srt"
    path.write_text(srt("你想，甲0呀"))
    authority = {}
    authority_path = tmp_path / "authority.json"
    verifier = Observers(transcripts={"mai": "你想，甲零呀", "moss": "你想，甲零呀"})
    snapshots = []

    def review(text, previous, start, end):
        return actual_producer_review(
            text, previous, verifier, snapshots=snapshots, target_only=True
        )

    audit = finalizer._run_exact_final_review_gate(
        cid="span-test",
        out_root=tmp_path,
        final_start=0,
        final_end=4000,
        recut=finalizer.CurrentTextReviewArtifacts(subtitle_path=path),
        chat_authority_audit=authority,
        chat_authority_path=authority_path,
        run_exact_final_review=review,
        clip_context=clip_context(),
    )
    assert path.read_text() == srt("你想，甲零呀")
    assert audit["release_gate"] == "PASS"
    saved = load_review(authority_path)
    final = saved["final_review_audit"]
    validate_final_review_release(final, expected_srt_sha256="sha256:" + sha(path.read_bytes()))
    repair = saved["exact_final_cpa_self_heal"]["passes"][0]["repairs"][0]
    assert repair[FIELD]["outside_findings"] == []
    assert repair["judge"]["needs_audio_requested"] is True
    assert repair[FIELD]["resolution"] == "CONTEXT_PHONEME_FIT"
    assert final["span_obligation_review"]["states"] == []


def test_unapplied_target_still_persists_its_separate_prefix_question(tmp_path):
    path = tmp_path / "clip.srt"
    path.write_text(srt("你想，甲0呀"))
    authority = {}
    authority_path = tmp_path / "authority.json"
    verifier = Observers(transcripts={"mai": "你想，甲零呀", "moss": "你想，甲蓝呀"})

    def review(text, previous, start, end):
        return actual_producer_review(text, previous, verifier)

    with pytest.raises(SystemExit, match="FINAL_REVIEW_RELEASE_BLOCKED"):
        finalizer._run_exact_final_review_gate(
            cid="span-test",
            out_root=tmp_path,
            final_start=0,
            final_end=4000,
            recut=finalizer.CurrentTextReviewArtifacts(subtitle_path=path),
            chat_authority_audit=authority,
            chat_authority_path=authority_path,
            run_exact_final_review=review,
            clip_context=clip_context(),
        )
    assert path.read_text() == srt("你想，甲0呀")
    saved = load_review(authority_path)
    packets = saved["span_unapplied_questions"]
    assert packets and packets[0]["packet"]["status"] == "UNRESOLVED"
    assert packets[0]["packet"]["mutation_authorized"] is False
    pending = pending_findings(path.read_text(), saved)
    assert any(row["suspect"] == "你想" for row in pending)
    cold = actual_producer_review(path.read_text(), saved, verifier, discover=False)
    enforce_review(path.read_text(), saved, cold)
    assert cold["release_gate"] == "BLOCK"
    assert any(row.get("origin_span_obligation") for row in cold["findings"])
    with pytest.raises(FinalReviewContractError):
        validate_final_review_release(cold, expected_srt_sha256="sha256:" + sha(path.read_bytes()))
    altered = json.loads(json.dumps(saved))
    altered["span_unapplied_questions"][0]["repair_prefix_sha256"] = "a" * 64
    with pytest.raises(ValueError):
        pending_findings(path.read_text(), altered)
