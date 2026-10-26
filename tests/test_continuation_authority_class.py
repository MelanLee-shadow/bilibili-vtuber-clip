"""Cold continuation reads exactly the states the ordinary writers persist.

Class (four-slice rerun): a producer cold start validates the
previous attempt's authority.  Every case here produces that authority with
the real writer, persists it, and feeds the persisted bytes to the real cold
reader: the interrupted exact-final self-heal commit, a multi-stage audio
language discovery, repeated media digests in one validation, and the
recovery sub-stage's native audio account.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import time
from types import SimpleNamespace

import pytest

from src.autoslice import producer_package_finalization as finalization
from src.autoslice import producer_text_pipeline as pipeline
from src.autoslice.final_review_meaning_policy import POLICY_VERSION as MEANING_POLICY_VERSION

ORIGINAL = "这怎么怎么是 TTT 的头像"
REPAIRED = "这怎么怎么是 ttt15 的头像"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _boundary() -> dict:
    return {
        "schema_version": "talk-boundary-semantic-review.v1", "status": "PASS",
        "review_scope": "final_delivery", "reason_codes": [],
        "request_sha256": "sha256:" + "d" * 64, "cue_grid_sha256": "sha256:" + "e" * 64,
        "source_separation_witness": {
            "schema_version": "talk-boundary-source-separation-witness.v1", "status": "PASS",
            "source_review_sha256": "sha256:" + "a" * 64, "source_request_sha256": "sha256:" + "b" * 64,
            "source_cue_grid_sha256": "sha256:" + "c" * 64, "source_final_start_ms": 0,
            "source_final_end_ms": 2_400, "reason_codes": [],
        },
        "final_endpoint_binding": {
            "schema_version": "talk-boundary-final-endpoint-binding.v1", "status": "PASS",
            "recommended_end_cue_index": 2, "recommended_end_ms": 2_000,
            "final_closure_cue_index": 2, "final_snapped_end_ms": 2_000, "final_end_ms": 2_400,
            "semantic_cue_grid_sha256": "sha256:" + "e" * 64,
            "final_cue_grid_sha256": "sha256:" + "e" * 64, "reason_codes": [],
        },
    }


def _review(text: str, repaired: str = REPAIRED) -> dict:
    common = {
        "schema_version": "final-review-audit.v2", "meaning_policy": MEANING_POLICY_VERSION,
        "reviewed_srt_sha256": "sha256:" + _sha(text), "boundary_semantic_review": _boundary(),
        "correction_mutation_authority": {
            "schema_version": "subtitle-correction-mutation-audit.v1", "status": "PASS",
            "applied_count": 0, "validated_mutation_count": 0, "failures": []},
        "correction_pass": {"findings": []},
        "discovery": {"status": "COMPLETE", "explicit_empty_findings": ORIGINAL not in text},
    }
    if ORIGINAL not in text:
        return {**common, "status": "CLEAN", "release_gate": "PASS", "reason_codes": [],
                "findings": [], "validated_finding_count": 0}
    base = _sha(ORIGINAL)
    finding = {
        "cue_index": 1, "base_text_sha256": base, "proposed_full_cue": repaired,
        "exact_release_adjudication": {
            "schema_version": "subtitle-span-adjudication.v1", "status": "OBSERVED",
            "decision_authority": "CPA_JUDGE", "repaired": True, "timing_immutable": True,
            "mutation_authority": {"schema_version": "subtitle-correction-mutation-authority.v1",
                                   "status": "PASS",
                                   "basis": "CPA_ACOUSTIC_PRONUNCIATION_DISAMBIGUATION"},
            "request": {"schema_version": "subtitle-span-acoustic-check-request.v1",
                        "request_sha256": "f" * 64, "base_text_sha256": base,
                        "current_cue": ORIGINAL, "proposed_cue": repaired},
            "witness_judge": {"judge": {"schema_version": "acoustic-witness-adjudication.v1",
                                        "status": "JUDGED", "choice": "PROPOSED",
                                        "check_request_sha256": "f" * 64}},
        },
    }
    return {**common, "status": "FLAGGED", "release_gate": "BLOCK",
            "reason_codes": ["FINAL_REVIEW_UNRESOLVED_FINDINGS"], "findings": [finding],
            "validated_finding_count": 1}


def _interrupted_writer_state(tmp_path: Path, repaired: str = REPAIRED, restart=None):
    """The real gate commits one self-heal pass, then is killed during its next review.

    ``restart`` (a callable: what changed before the restart, e.g. a newly
    deployed profile rule) runs the ordinary gate once more and kills it at
    review start.
    """
    subtitle = tmp_path / "candidate.recut.srt"
    subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\n" + ORIGINAL
                        + "\n\n2\n00:00:01,100 --> 00:00:02,000\n完整收束\n", encoding="utf-8")
    chat_path = tmp_path / "candidate.chat-authority.json"
    calls: list[str] = []

    def exact_review(text, _authority, _offset, _end):
        calls.append(text)
        if len(calls) >= 2:
            raise SystemExit("KILLED_DURING_SUCCESSOR_REVIEW")
        return _review(text, repaired)

    def unused(*_args, **_kwargs):
        raise AssertionError("unrelated adapter called")

    chat = {"schema_version": "chat-authority-audit.v2", "mutation_authorized": False,
            "entity_repairs": [], "final_output_srt_sha256": _sha(subtitle.read_text())}
    adapters = finalization.ProducerFinalizationAdapters(
        accurate_recut_command=unused, run_command=unused, write_source_range_srt=unused,
        apply_text_override_document=unused, run_speaker_finalization=unused,
        burn_preview_subtitles=unused, stage_publish_draft=unused, generate_upload_tags=unused,
        delivery_root=unused, run_exact_final_review=exact_review)
    for attempt in range(2 if restart else 1):
        if attempt:
            restart()
        with pytest.raises(SystemExit, match="KILLED_DURING_SUCCESSOR_REVIEW"):
            finalization._run_exact_final_review_gate(
                cid="candidate", out_root=tmp_path, final_start=0, final_end=2_400,
                recut=finalization.FinalRecutArtifacts(
                    recut_dir=tmp_path, media_path=tmp_path / "candidate.recut.mp4",
                    subtitle_path=subtitle, text_manifest_path=None, text_manifest=None,
                    redelivery_baseline_audit_path=None, redelivery_baseline_audit=None),
                chat_authority_audit=chat, chat_authority_path=chat_path, adapters=adapters)
        chat = pipeline.review_from_bytes(chat_path.read_bytes(), directory=tmp_path)
    persisted = pipeline.review_from_bytes(chat_path.read_bytes(), directory=tmp_path)
    text = subtitle.read_text(encoding="utf-8")
    return persisted, text, {"candidate_id": "candidate"}


def _cold_read(chat, text, context):
    return pipeline._continuation_validate_existing_authority(
        chat, srt_text=text, srt_sha256=_sha(text), clip_context=context, candidate_id="candidate")


def test_interrupted_self_heal_commit_is_resumed_as_review_input(tmp_path):
    chat, text, context = _interrupted_writer_state(tmp_path)
    # The writer's own persisted state: review bound to the terminal pass input.
    receipt = chat["exact_final_cpa_self_heal"]
    assert REPAIRED in text and receipt["status"] == "REVIEW_PENDING"
    assert chat["final_review_audit"]["reviewed_srt_sha256"] == receipt["passes"][-1]["input_srt_sha256"]
    assert chat["final_output_srt_sha256"] == _sha(text)
    before = copy.deepcopy(chat)
    result = _cold_read(chat, text, context)
    assert chat == before
    assert result["release_gate"] == "BLOCK"
    assert result["reviewed_srt_sha256"] == receipt["passes"][-1]["input_srt_sha256"]
    # Never a release (or completed review) of the active bytes.
    with pytest.raises(pipeline.FinalReviewContractError):
        pipeline.validate_final_review_release(result, expected_srt_sha256="sha256:" + _sha(text))


def test_restart_under_a_newer_display_rule_is_the_same_member(tmp_path, monkeypatch):
    # New member: the restart runs a newer deployment whose profile registers a
    # caption display spelling for the repaired word. The restarted ordinary
    # gate applies it as a typed successor and is killed again; the persisted
    # review still covers the terminal pass input.
    import dataclasses
    from src.autoslice import surface_canon

    def newer_deployment():
        template = surface_canon._CAPTION_DISPLAY_SURFACE_RULES[0]
        monkeypatch.setattr(surface_canon, "_CAPTION_DISPLAY_SURFACE_RULES", (
            *surface_canon._CAPTION_DISPLAY_SURFACE_RULES,
            dataclasses.replace(template, surface="ttt15", canonical="TTT15")))

    chat, text, context = _interrupted_writer_state(tmp_path, restart=newer_deployment)
    receipt = chat["exact_final_cpa_self_heal"]
    assert "这怎么怎么是 TTT15 的头像" in text and receipt.get("caption_display_successors")
    assert chat["final_review_audit"]["reviewed_srt_sha256"] == receipt["passes"][-1]["input_srt_sha256"]
    assert receipt["passes"][-1]["output_srt_sha256"] != "sha256:" + _sha(text)
    assert _cold_read(chat, text, context)["release_gate"] == "BLOCK"


@pytest.mark.parametrize("drift", [
    "review_bound_elsewhere", "chain_tampered",
    "released_predecessor", "run_completed", "active_bytes_edited",
])
def test_interrupted_commit_keeps_every_other_binding(tmp_path, drift):
    chat, text, context = _interrupted_writer_state(tmp_path)
    receipt, review = chat["exact_final_cpa_self_heal"], chat["final_review_audit"]
    if drift == "review_bound_elsewhere":
        review["reviewed_srt_sha256"] = "sha256:" + "0" * 64
    elif drift == "chain_tampered":
        receipt["passes"][-1]["output_srt_sha256"] = "sha256:" + "1" * 64
    elif drift == "released_predecessor":
        review.update(status="CLEAN", release_gate="PASS", reason_codes=[], findings=[],
                      validated_finding_count=0)
    elif drift == "run_completed":
        receipt["status"] = "PASS"
    else:
        text = text.replace("完整收束", "完整收束了")
        chat["final_output_srt_sha256"] = _sha(text)
    with pytest.raises(pipeline.CurrentTextContinuationError):
        _cold_read(chat, text, context)


def test_preflight_audit_reports_every_subcheck_and_raises_the_same_first_failure(tmp_path):
    from src.autoslice import continuation_subchecks

    chat, text, context = _interrupted_writer_state(tmp_path)
    chat["mutation_authorized"] = True  # first failure
    chat["exact_final_cpa_self_heal"]["passes"][-1]["output_srt_sha256"] = "sha256:" + "1" * 64
    with pytest.raises(pipeline.CurrentTextContinuationError) as fail_fast:
        _cold_read(chat, text, context)
    with continuation_subchecks.audit() as rows:
        with pytest.raises(pipeline.CurrentTextContinuationError) as audited:
            _cold_read(chat, text, context)
    assert str(audited.value) == str(fail_fast.value) == "CURRENT_CHAT_AUTHORITY_MUTATION_INVALID"
    status = {row["check"]: row["status"] for row in rows}
    assert status["chat_authority.mutation_flag"] == "FAIL"
    assert status["self_heal.chain"] == "FAIL"  # a later, independent item is still evaluated
    assert status["final_review.release_or_pending"] == "PASS"
    assert all(isinstance(row["elapsed_s"], float) for row in rows)


# --- multi-stage audio language discovery (real writer) --------------------

INPUT = (
    "1\n00:00:00,000 --> 00:00:01,000\n先听这个\n\n"
    "2\n00:00:01,000 --> 00:00:02,500\n他说明他在给你飞的高\n\n"
    "3\n00:00:03,000 --> 00:00:04,660\n完全现场口令说\n"
)
SPOKEN = {2: "メンタル的にきついですか", 3: "完全に無双状態"}


def _discovery_authority(tmp_path, monkeypatch, *, input_srt=INPUT, spoken=SPOKEN):
    from src.autoslice import audio_language_discovery as discovery
    from src.autoslice import foreign_span_witness as foreign

    monkeypatch.delenv("AUTOSLICE_BASE", raising=False)
    monkeypatch.setenv("AUTOSLICE_CPA_JUDGE_CACHE", "0")
    media = tmp_path / "source.mp4"
    media.write_bytes(b"source-identity-only-no-media-processing")
    cues = {int(cue.index): cue for cue in pipeline.parse_srt_cues(input_srt)}

    def witness_rows(*, rows, **_kwargs):
        return [{"cue_index": row["cue_index"], "start_ms": row["start_ms"], "end_ms": row["end_ms"],
                 "mode": "verbatim", "witnessed": False, "audible_language": "ja",
                 "exact_transcript": spoken.get(row["cue_index"], row.get("text") or cues[row["cue_index"]].text),
                 "speaker_impression": "single_live_voice",
                 "provider": "agy", "audio_sha256": "a" * 64} for row in rows]

    def judge(_prompt):
        return json.dumps({"choice": "PROPOSED", "candidate_id": "PROPOSAL", "needs_audio": False,
                           "reason": "The supplied observation supports this candidate."})

    monkeypatch.setattr(foreign, "_witness_rows", witness_rows)
    authority = {"applied": [], "entity_repairs": []}
    output, audit = discovery.repair_audio_language_candidates(
        media_path=media, srt_text=input_srt, out_root=tmp_path, cid="candidate", llm_call=judge,
        authority=authority, source_srt=input_srt,
        scan_call=lambda **_: {"status": "OBSERVED", "semantic_anomalies": [], "speech_turns": [],
                               "foreign_spans": [{"start_ms": cues[i].start_ms, "end_ms": cues[i].end_ms,
                                                  "language": "ja"} for i in spoken]})
    authority["audio_language_discovery_audit"] = audit
    path = tmp_path / "authority.json"
    path.write_text(json.dumps(authority, ensure_ascii=False), encoding="utf-8")
    return json.loads(path.read_text(encoding="utf-8")), output, discovery


def test_every_owner_of_a_multi_stage_discovery_replays_on_its_own_generation(tmp_path, monkeypatch):
    from src.autoslice.final_source_language_owner import validate_final_cpa_owner

    authority, output, _discovery = _discovery_authority(tmp_path, monkeypatch)
    stages = authority["audio_language_discovery_audit"]["adjudications"]
    assert sum(1 for stage in stages if stage.get("applied_count")) == 2
    assert all(SPOKEN[i] in output for i in SPOKEN)
    for index, row in enumerate(authority["entity_repairs"]):
        assert row["mode"] == "final_foreign_script_cpa_adjudication"
        saved = copy.deepcopy(authority)
        validate_final_cpa_owner(row, row_index=index, authority=authority, srt_text=output)
        assert authority == saved
    from src.autoslice.final_source_language_owner import discovery_generations
    generations = discovery_generations(authority["audio_language_discovery_audit"])
    assert generations[0][1]["input_srt_sha256"] == _sha(INPUT)
    assert generations[0][1]["output_srt_sha256"] == generations[1][1]["input_srt_sha256"]


@pytest.mark.parametrize("damage", ["middle_digest", "reordered", "input_digest", "unrecorded_change"])
def test_multi_stage_generation_must_be_authenticated_at_every_stage(tmp_path, monkeypatch, damage):
    from src.autoslice.final_source_language_owner import validate_final_cpa_owner

    authority, output, discovery = _discovery_authority(tmp_path, monkeypatch)
    audit = authority["audio_language_discovery_audit"]
    applied = [stage for stage in audit["adjudications"] if stage.get("applied_count")]
    if damage == "middle_digest":
        applied[0]["output_srt_sha256"] = "0" * 64
    elif damage == "reordered":
        first, second = (audit["adjudications"].index(stage) for stage in applied)
        audit["adjudications"][first], audit["adjudications"][second] = applied[1], applied[0]
    elif damage == "input_digest":
        audit["input_srt_sha256"] = "f" * 64
    else:
        audit["output_srt"] = audit["output_srt"].replace("先听这个", "先听那个")
        audit["output_srt_sha256"] = _sha(audit["output_srt"])
    audit["discovery_sha256"] = discovery._audit_sha(audit)
    with pytest.raises(ValueError):
        for index, row in enumerate(authority["entity_repairs"]):
            validate_final_cpa_owner(row, row_index=index, authority=authority, srt_text=output)


# --- one digest per file placement within cold validation ----------------

def test_cold_validators_hash_each_unchanged_file_once(tmp_path, monkeypatch):
    from src.autoslice import evidence_identity
    from src.autoslice import independent_context_adjudication as context

    media = tmp_path / "source.flv"
    media.write_bytes(b"x" * (3 << 20))
    # A newly written file is racy; establish a cold input before counting reads.
    time.sleep(3 * evidence_identity._RACY_WINDOW_NS / 1_000_000_000)
    reads = []
    real = evidence_identity.hashlib

    class Counting:
        @staticmethod
        def sha256(*args):
            reads.append(1)
            return real.sha256(*args)

    monkeypatch.setattr(evidence_identity, "hashlib", Counting)
    digests = {context._file_sha256(str(media)) for _ in range(50)}
    digests |= {pipeline._continuation_sha256_path(media, label="CURRENT_PADDED_MEDIA") for _ in range(50)}
    assert digests == {real.sha256(media.read_bytes()).hexdigest()}
    assert len(reads) == 1
    media.write_bytes(b"y" * (3 << 20 + 1))  # a changed placement is hashed again
    assert context._file_sha256(str(media)) == real.sha256(media.read_bytes()).hexdigest()
    assert len(reads) == 2


# --- recovery sub-stage shares the candidate's native audio account ----------

def test_text_recovery_uses_the_candidate_audio_account_not_its_own_directory(tmp_path, monkeypatch):
    built = []

    class Stop(Exception):
        pass

    def context_builder(**kwargs):
        built.append(kwargs["out_root"])
        return SimpleNamespace(verify_confusable_entity=lambda _request: None)

    def gate(**_kwargs):
        raise Stop()

    out_root = tmp_path / "out"
    current = "1\n00:00:00,250 --> 00:00:03,070\n谢谢 Alice 的 SC!\n"
    srt = tmp_path / "current.srt"
    srt.write_text(current, encoding="utf-8")
    padded = tmp_path / "padded.mp4"
    padded.write_bytes(b"padded-fixture")
    context = tmp_path / "context.json"
    context.write_text("{}", encoding="utf-8")
    review = {"schema_version": "final-review-audit.v2", "status": "FLAGGED", "release_gate": "BLOCK",
              "findings": [{"cue_index": 1}], "reviewed_srt_sha256": "sha256:" + _sha(current),
              "boundary_semantic_review": {"review_scope": "final_delivery"}}
    chat = {"schema_version": "chat-authority-audit.v2", "entity_repairs": [], "final_review_audit": review,
            "final_output_srt_sha256": _sha(current)}
    chat_path = tmp_path / "chat.json"
    chat_path.write_text(json.dumps(chat), encoding="utf-8")
    cues = pipeline.parse_srt_cues(current)
    projection = {"source_first_start_ms": 250, "source_last_end_ms": 3_070}
    monkeypatch.setattr(pipeline, "_continuation_source_clock_reference", lambda *a, **k: (None, None))
    monkeypatch.setattr(pipeline, "_continuation_validate_existing_authority", lambda *a, **k: review)
    monkeypatch.setattr(pipeline, "validate_clip_context", lambda value, **kwargs: value)
    monkeypatch.setattr(pipeline, "_continuation_project_delivery_to_source", lambda *a, **k: (cues, dict(projection)))
    monkeypatch.setattr(pipeline, "_bind_projection_recording_clock", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "_continuation_source_boundary_cues", lambda **k: (cues, {"status": "PASS"}))
    monkeypatch.setattr(pipeline, "_continuation_source_context_draft_binding", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "_continuation_chat_from_context", lambda value: ())
    monkeypatch.setattr(pipeline, "ledger_required_owner_contracts", lambda **kwargs: [])
    monkeypatch.setattr(pipeline, "_redelivery_baseline_boundary_owner", lambda *a, **k: [])
    monkeypatch.setattr(pipeline, "freeze_required_boundary_owner_contract", lambda **kwargs: (0, {}))
    monkeypatch.setattr(pipeline, "_build_entity_verification_context", context_builder)
    monkeypatch.setattr(pipeline, "_build_exact_final_review_callback", lambda **kwargs: (lambda *a: None))
    monkeypatch.setattr(pipeline, "build_env_screen_read_probe", lambda *a, **k: None)
    monkeypatch.setattr(finalization, "_run_exact_final_review_gate", gate)
    continuation = pipeline.CurrentTextContinuation(
        current_srt_path=srt, chat_authority_path=chat_path, clip_context_path=context,
        padded_sha256="sha256:" + hashlib.sha256(padded.read_bytes()).hexdigest(),
        source_media_sha256s=("sha256:" + "a" * 64,), spans=[SimpleNamespace(start_ms=0, end_ms=100)],
        transcriber=lambda *_args: "")
    adapters = SimpleNamespace(profile_asset_file=lambda name: tmp_path / name)
    with pytest.raises(Stop):
        pipeline._run_current_text_continuation(
            spec={"date": "2026-10-04", "human_truth_mode": "withheld",
                  "pieces": [{"source_media_sha256": "sha256:" + "a" * 64}]},
            durations=[100_000], padded=padded, padded_dur=100_000, host="localhost",
            cid="candidate", out_root=out_root, adapters=adapters, continuation=continuation)
    assert built == [out_root]
    assert (out_root / "current-text-recovery").is_dir()  # review artifacts stay in the sub-stage


# --- an unrecoverable cold read is a retryable engineering state -------------

def test_runner_classifies_cold_continuation_failure_as_engineering_not_content():
    from scripts import session_autoslice as talk_lane  # the runner's own entry

    tail = (
        "Traceback (most recent call last):\n"
        '  File "producer_text_pipeline.py", line 4464, in _continuation_validate_existing_authority\n'
        "src.autoslice.producer_text_pipeline.CurrentTextContinuationError: FINAL_REVIEW_SRT_BINDING_MISMATCH\n"
    )
    result = talk_lane.classify_talk_failure(tail)
    assert (result["failure_kind"], result["failure_stage"]) == ("pipeline_engineering", "continuation_authority")
    assert result["failure_recoverable"] is True
    assert result["failure_evidence"]["reason_code"] == "FINAL_REVIEW_SRT_BINDING_MISMATCH"
    assert result["failure_evidence"]["new_model_call_authorized"] is False
    verdict = talk_lane.classify_talk_failure(
        "FINAL_REVIEW_RELEASE_BLOCKED: FINAL_REVIEW_UNRESOLVED_FINDINGS: /out/x.chat-authority.json\n")
    assert verdict["failure_stage"] != "continuation_authority"


def test_identical_persisted_judge_content_replays_once_per_cold_read(monkeypatch):
    from src.autoslice import span_phoneme_resolution as spr
    from src.autoslice.continuation_subchecks import cold_read_session

    bare = {"cue": "这怎么怎么是 TTT 的头像"}
    unsigned = {"schema_version": "span-judge-compilation.v1", "compiler_revision": "test-revision",
                "request": {}, "witness": {}, "judge_sha256": spr.digest(bare)}
    outcome = {**unsigned, "outcome_sha256": spr.digest(unsigned)}
    node = {**bare, "span_compilation": outcome}
    calls = []

    def replay(_request, _witness, _bare):
        calls.append(1)
        return {"span_compilation": copy.deepcopy(outcome)}

    monkeypatch.setattr(spr, "compile_judge_scope", replay)
    spr.validate_persisted_judge_scopes({"a": node})
    spr.validate_persisted_judge_scopes({"a": node})
    assert len(calls) == 1  # v5 retains successful complete-input checks per process
    with cold_read_session():
        spr.validate_persisted_judge_scopes({"a": node})
        spr.validate_persisted_judge_scopes({"b": [copy.deepcopy(node)]})
    assert len(calls) == 1  # a cold-read session cannot invalidate unchanged evidence
    # A different immutable input still reaches the original compiler; failed
    # checks never become successful memo entries, even within one cold read.
    changed_bare = {"cue": bare["cue"] + " changed"}
    changed_unsigned = {**unsigned, "judge_sha256": spr.digest(changed_bare)}
    changed_outcome = {**changed_unsigned, "outcome_sha256": spr.digest(changed_unsigned)}
    changed = {**changed_bare, "span_compilation": changed_outcome}
    def reject(*_args):
        calls.append(1)
        return {"span_compilation": {}}
    monkeypatch.setattr(spr, "compile_judge_scope", reject)
    with cold_read_session():
        for _ in range(2):  # a failed replay is never remembered
            with pytest.raises(ValueError, match="SPAN_COMPILATION_REPLAY_DRIFT"):
                spr.validate_persisted_judge_scopes({"a": changed})
    assert len(calls) == 3


# --- an unfinished review stage persisted by the gate (姐姐位 post-run) -------

ENGINEERING_ROW = {
    "cue_index": 1, "kind": "context", "origin_span_obligation": True,
    "lifecycle": {"schema_version": "finding-lifecycle.v1", "state": "CONTRACT_ERROR",
                  "release_action": "RECOVER_ENGINEERING", "reason_code": "CONSUMER_REJECTED",
                  "recovery": "REVALIDATE_ORIGINAL_ANSWER_AFTER_CONSUMER_REPAIR",
                  "new_model_call_authorized": False, "terminal": True,
                  "detail": {"status": "CONTRACT_ERROR", "prior_evidence_retained": True,
                             "reason_code": "ASSISTED_AUDIO_SOURCE_BINDING_REQUIRED"}},
}


def _unfinished_stage(tmp_path, *, findings, codes):
    from tests.test_producer_current_text_continuation import _discovery_pending_checkpoint, _srt

    current, context, digest, clock = _discovery_pending_checkpoint(tmp_path)
    current["final_review_audit"].update(status="FLAGGED", reason_codes=codes, findings=findings,
                                         validated_finding_count=len(findings))
    return dict(chat_authority=current, srt_text=_srt(), srt_sha256=digest, clip_context=context,
                candidate_id="relationship-candidate", source_clock_review=clock)


def test_unfinished_review_stage_with_pending_engineering_rows_is_review_input(tmp_path):
    from src.autoslice.finding_lifecycle import release_reason_codes

    codes = ["CANDIDATE_PRONOUN_PROVIDER_OR_JSON_UNAVAILABLE",
             *release_reason_codes([ENGINEERING_ROW], terminal=False)]
    arguments = _unfinished_stage(tmp_path, findings=[copy.deepcopy(ENGINEERING_ROW)], codes=codes)
    chat = arguments.pop("chat_authority")
    review = pipeline._continuation_validate_existing_authority(chat, **arguments)
    assert review["discovery"]["status"] == "AUDITOR_UNAVAILABLE" and review["release_gate"] == "BLOCK"


@pytest.mark.parametrize("drift", ["unaccounted_row", "content_block", "foreign_reason", "no_stage_code"])
def test_unfinished_review_stage_still_rejects_everything_else(tmp_path, drift):
    from src.autoslice.finding_lifecycle import CONTENT_BLOCK, release_reason_codes

    row = copy.deepcopy(ENGINEERING_ROW)
    codes = ["CANDIDATE_PRONOUN_PROVIDER_OR_JSON_UNAVAILABLE", *release_reason_codes([row], terminal=False)]
    if drift == "unaccounted_row":
        codes = codes[:1]
    elif drift == "content_block":
        codes.append(CONTENT_BLOCK)
    elif drift == "foreign_reason":
        codes.append("SOMETHING_ELSE_FAILED")
    else:
        codes = codes[1:]
    arguments = _unfinished_stage(tmp_path, findings=[row], codes=codes)
    chat = arguments.pop("chat_authority")
    with pytest.raises(pipeline.CurrentTextContinuationError):
        pipeline._continuation_validate_existing_authority(chat, **arguments)


# --- pending delivery input and historical language clocks -----------------

LANGUAGE_INPUT = (
    "1\n00:00:10,000 --> 00:00:11,000\n先听这个\n\n"
    "2\n00:00:11,000 --> 00:00:12,500\n他说明他在给你飞的高\n\n"
    "3\n00:00:13,000 --> 00:00:14,660\n完全现场口令说\n\n"
    "4\n00:00:15,000 --> 00:00:16,500\n现在还有一句\n"
)
LANGUAGE_SPOKEN = {**SPOKEN, 4: "もう一度聞いてください"}


def _language_owner_checkpoint(tmp_path, monkeypatch, *, surface="foreign_script"):
    """Real language writer -> saved clock/current files -> cold continuation."""
    if surface == "foreign_script":
        language, source, _ = _discovery_authority(
            tmp_path, monkeypatch, input_srt=LANGUAGE_INPUT, spoken=LANGUAGE_SPOKEN)
    else:
        from src.autoslice import foreign_span_witness as foreign
        from src.autoslice.final_source_language_owner import register_final_source_language_cpa_repairs

        monkeypatch.delenv("AUTOSLICE_BASE", raising=False)
        monkeypatch.setenv("AUTOSLICE_CPA_JUDGE_CACHE", "0")
        media = tmp_path / "source.mp4"
        media.write_bytes(b"source-identity-only-no-media-processing")
        cues = {int(cue.index): cue for cue in pipeline.parse_srt_cues(LANGUAGE_INPUT)}
        observations = [{"cue_index": i, "start_ms": cues[i].start_ms, "end_ms": cues[i].end_ms,
                         "mode": "verbatim", "witnessed": False, "audible_language": "ja",
                         "exact_transcript": text, "speaker_impression": "single_live_voice",
                         "provider": "agy", "audio_sha256": "a" * 64}
                        for i, text in LANGUAGE_SPOKEN.items()]
        monkeypatch.setattr(foreign, "_witness_rows", lambda **_: copy.deepcopy(observations))
        source, audit = foreign.adjudicate_language_preservation_audit(
            media_path=media, srt_text=LANGUAGE_INPUT, out_root=tmp_path, cid="candidate",
            source_srt=LANGUAGE_INPUT,
            llm_call=lambda _: json.dumps({"choice": "PROPOSED", "candidate_id": "PROPOSAL",
                "needs_audio": False, "reason": "The supplied observation supports this candidate."}),
            audit={"status": "BLOCKED_UNPROVEN_FOREIGN_SPEAKER", "unproven_foreign_introductions": [
                {"cue_index": i, "start_ms": cues[i].start_ms, "end_ms": cues[i].end_ms,
                 "draft": cues[i].text, "attempted": cues[i].text} for i in LANGUAGE_SPOKEN]})
        audit.update(input_srt=LANGUAGE_INPUT, input_srt_sha256=_sha(LANGUAGE_INPUT), output_srt=source)
        language = {"applied": [], "entity_repairs": [], "final_source_language_preservation_audit": audit}
        register_final_source_language_cpa_repairs(
            language, input_srt=LANGUAGE_INPUT, output_srt=source, source_language_audit=audit)
    assert len(language["entity_repairs"]) == 3
    # A real cut moves all four source cues ten seconds onto the delivery clock.
    delivery = pipeline._render_cues_to_srt([
        type(cue)(index=cue.index, start_ms=cue.start_ms - 10_000,
                  end_ms=cue.end_ms - 10_000, text=cue.text)
        for cue in pipeline.parse_srt_cues(source)])
    grid = pipeline.cue_grid_sha256(pipeline.parse_srt_cues(delivery))
    review = _review(delivery)
    boundary = review["boundary_semantic_review"]
    boundary.update(candidate_id="candidate", cue_grid_sha256=grid)
    boundary["final_endpoint_binding"].update(
        final_start_ms=0, final_end_ms=6_500, recommended_end_cue_index=4,
        final_closure_cue_index=4, recommended_end_ms=6_500, final_snapped_end_ms=6_500,
        semantic_cue_grid_sha256=grid, final_cue_grid_sha256=grid)
    boundary["source_separation_witness"].update(source_final_start_ms=10_000, source_final_end_ms=16_500)
    reference = {**language, "schema_version": "chat-authority-audit.v2", "mutation_authorized": False,
                 "final_output_srt_sha256": _sha(delivery), "final_review_audit": review,
                 "post_transcript_entity_output_srt": source,
                 "post_transcript_entity_output_srt_sha256": _sha(source),
                 "frozen_boundary_owner_contract": {"owner_set_sha256": "sha256:" + "8" * 64}}
    clock_path = tmp_path / "language-clock.json"
    clock_path.write_text(json.dumps(reference, ensure_ascii=False), encoding="utf-8")
    current = copy.deepcopy(reference)
    current["final_review_audit"].update(
        status="AUDITOR_UNAVAILABLE", release_gate="BLOCK",
        reason_codes=["FINAL_REVIEW_PROVIDER_OR_JSON_UNAVAILABLE"],
        discovery={"status": "AUDITOR_UNAVAILABLE", "detail": "LlmCallError"},
        boundary_semantic_review={"schema_version": "talk-boundary-semantic-review.v1",
            "status": "BLOCK", "reason_codes": ["CURRENT_TEXT_REQUIRES_SOURCE_REVIEW"]})
    current_path = tmp_path / "language-current.json"
    current_path.write_text(json.dumps(current, ensure_ascii=False), encoding="utf-8")
    continuation = SimpleNamespace(source_clock_authority_path=clock_path,
        expected_source_clock_authority_sha256="sha256:" + hashlib.sha256(clock_path.read_bytes()).hexdigest())
    context = {"candidate_id": "candidate"}
    return json.loads(current_path.read_text()), delivery, context, continuation, current_path


def _read_language_checkpoint(chat, text, context, continuation):
    clock, _ = pipeline._continuation_source_clock_reference(
        continuation, chat_authority=chat, srt_text=text, srt_sha256=_sha(text),
        clip_context=context, candidate_id="candidate")
    return pipeline._continuation_validate_existing_authority(
        chat, srt_text=text, srt_sha256=_sha(text), clip_context=context,
        candidate_id="candidate", source_clock_review=clock)


@pytest.mark.parametrize("surface", ["foreign_script", "source_language"])
def test_pending_delivery_replays_three_language_owners_without_promoting_clock(tmp_path, monkeypatch, surface):
    chat, text, context, continuation, current_path = _language_owner_checkpoint(
        tmp_path, monkeypatch, surface=surface)
    paths = [current_path, continuation.source_clock_authority_path]
    before = [path.read_bytes() for path in paths]
    saved = copy.deepcopy(chat)
    result = _read_language_checkpoint(chat, text, context, continuation)
    assert result == saved["final_review_audit"]
    assert chat == saved and [path.read_bytes() for path in paths] == before
    assert result["release_gate"] == "BLOCK"
    assert result["discovery"]["status"] == "AUDITOR_UNAVAILABLE"
    with pytest.raises(pipeline.FinalReviewContractError, match="DISCOVERY_INCOMPLETE"):
        pipeline.validate_final_review_release(result, expected_srt_sha256="sha256:" + _sha(text))


@pytest.mark.parametrize("surface", ["foreign_script", "source_language"])
@pytest.mark.parametrize("damage", ["no_clock", "clock_sha", "candidate", "source_binding", "owner_binding",
                                  "clock_grid", "owner_generation", "current_text", "owner_words"])
def test_pending_language_owners_keep_clock_and_text_rejection_guards(tmp_path, monkeypatch, surface, damage):
    chat, text, context, continuation, _ = _language_owner_checkpoint(tmp_path, monkeypatch, surface=surface)
    path = continuation.source_clock_authority_path
    reference = json.loads(path.read_text())
    if damage == "no_clock":
        continuation.source_clock_authority_path = None
        continuation.expected_source_clock_authority_sha256 = None
    elif damage == "clock_sha":
        path.write_bytes(path.read_bytes() + b" ")
    elif damage == "candidate":
        reference["final_review_audit"]["boundary_semantic_review"]["candidate_id"] = "another-candidate"
    elif damage == "source_binding":
        chat["post_transcript_entity_output_srt_sha256"] = "0" * 64
    elif damage == "owner_binding":
        chat["frozen_boundary_owner_contract"]["owner_set_sha256"] = "sha256:" + "0" * 64
    elif damage == "clock_grid":
        reference["final_review_audit"]["boundary_semantic_review"]["final_endpoint_binding"]["final_cue_grid_sha256"] = "sha256:" + "0" * 64
    elif damage == "owner_generation":
        if surface == "foreign_script":
            from src.autoslice.audio_language_discovery import _audit_sha
            audit = chat["audio_language_discovery_audit"]
            audit["adjudications"][0]["output_srt_sha256"] = "0" * 64
            audit["discovery_sha256"] = _audit_sha(audit)
        else:
            chat["final_source_language_preservation_audit"]["input_srt_sha256"] = "0" * 64
    elif damage == "current_text":
        text = text.replace(LANGUAGE_SPOKEN[4], "別のことば")
        chat["final_output_srt_sha256"] = _sha(text)
        chat["final_review_audit"]["reviewed_srt_sha256"] = "sha256:" + _sha(text)
    else:
        chat["entity_repairs"][1]["after"] = ["別のことば"]
    if damage in {"candidate", "clock_grid"}:
        path.write_text(json.dumps(reference, ensure_ascii=False), encoding="utf-8")
        continuation.expected_source_clock_authority_sha256 = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    before = copy.deepcopy(chat)
    with pytest.raises(pipeline.CurrentTextContinuationError):
        _read_language_checkpoint(chat, text, context, continuation)
    assert chat == before


@pytest.mark.parametrize("surface", ["foreign_script", "source_language"])
def test_normal_delivery_uses_current_geometry_even_when_an_unused_clock_is_supplied(tmp_path, monkeypatch, surface):
    chat, text, context, continuation, _ = _language_owner_checkpoint(tmp_path, monkeypatch, surface=surface)
    reference = json.loads(continuation.source_clock_authority_path.read_text())
    result = pipeline._continuation_validate_existing_authority(
        reference, srt_text=text, srt_sha256=_sha(text), clip_context=context,
        candidate_id="candidate", source_clock_review={"boundary_semantic_review": {}})
    assert result == reference["final_review_audit"]
    assert result["release_gate"] == "PASS"


@pytest.mark.parametrize("surface", ["foreign_script", "source_language"])
def test_source_full_window_language_owners_keep_their_source_domain(tmp_path, monkeypatch, surface):
    chat, _, context, _, _ = _language_owner_checkpoint(tmp_path, monkeypatch, surface=surface)
    source = chat["post_transcript_entity_output_srt"]
    grid = pipeline.cue_grid_sha256(pipeline.parse_srt_cues(source))
    review = chat["final_review_audit"]
    review["boundary_semantic_review"] = {
        "schema_version": "talk-boundary-semantic-review.v1", "status": "BLOCK",
        "candidate_id": "candidate", "review_scope": "source_full_window",
        "request_sha256": "sha256:" + "c" * 64, "cue_grid_sha256": grid,
        "current_delivery_text_evidence": {"role": "CURRENT_TEXT_PROPOSAL_NOT_FINAL_CLEAN",
            "delivery_cue_grid_sha256": grid, "projected_source_cue_grid_sha256": grid,
            "source_cue_grid_sha256": grid}}
    review["current_delivery_source_projection"] = {
        "schema_version": "delivery-to-source-timeline-projection.v1", "status": "PASS",
        "delivery_srt_cue_grid_sha256": grid, "projected_source_cue_grid_sha256": grid}
    chat["final_output_srt_sha256"] = _sha(source)
    review["reviewed_srt_sha256"] = "sha256:" + _sha(source)
    # Valid geometry deliberately maps to a different physical source interval.
    # source_full_window must not project its already-source-domain owners.
    clock = _review(source)
    before = copy.deepcopy(chat)
    result = pipeline._continuation_validate_existing_authority(
        chat, srt_text=source, srt_sha256=_sha(source), clip_context=context,
        candidate_id="candidate", source_clock_review=clock)
    assert result == review and chat == before
    with pytest.raises(pipeline.FinalReviewContractError, match="DISCOVERY_INCOMPLETE"):
        pipeline.validate_final_review_release(result, expected_srt_sha256="sha256:" + _sha(source))
