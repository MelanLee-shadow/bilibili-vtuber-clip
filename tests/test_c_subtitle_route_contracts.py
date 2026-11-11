"""New ordinary hearing/ownership controls, using synthetic low-level observations."""
from copy import deepcopy
import json
import socket

import pytest

from scripts.subtitle_regression_eval import normal_off_samples, main
from src.autoslice.acoustic_witness_adjudication import adjudicate_with_witness, build_witness_request
from src.autoslice.final_review_auditor import build_context_adjudication_request
from src.autoslice.span_phoneme_resolution import bind_runtime_request, validate_persisted_judge_scopes
from src.autoslice.span_finding_obligations import pending_findings, enforce_review, validate_release_obligations
from tests.test_span_phoneme_resolution import Observers, srt, sha
from tests.test_span_producer_persistence import applied_history


@pytest.fixture(autouse=True)
def offline(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path / "runtime"))
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Synthetic route controls cannot call any provider")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def hearing(choice, needs_audio):
    current = "两个电阻要并联。"
    finding = {
        "cue_index": 1, "suspect": "并联", "suggestion": "串联",
        "repair_class": "phonetic", "proposed_full_cue": "两个电阻要串联。",
        "base_text_sha256": sha(current),
    }
    observer = Observers(heard="liang ge dian zu yao " + ("bing lian" if choice == "CURRENT" else "chuan lian"))
    request = build_context_adjudication_request(srt(current), finding, require_complete_utterance_support=True)
    request = bind_runtime_request(request, observer)
    witness = observer(build_witness_request(request))
    reply = {
        "choice": choice, "reason": "Synthetic definitive whole-cue hearing.",
        "current_utterance_supported": choice == "CURRENT",
        "current_utterance_support_reason": "The full current reading is supported." if choice == "CURRENT" else "The proposed reading is supported.",
    }
    if needs_audio is not None:
        reply["needs_audio"] = needs_audio
    result = adjudicate_with_witness(check_request=request, witness=witness,
                                    llm_call=lambda _prompt: json.dumps(reply))
    return result, reply


@pytest.mark.parametrize("choice", ["CURRENT", "PROPOSED"])
@pytest.mark.parametrize("needs_audio", [None, False])
def test_definitive_ordinary_hearing_survives_negative_span_diagnostic(choice, needs_audio):
    (repaired, branch, audit), reply = hearing(choice, needs_audio)
    assert repaired is (choice == "PROPOSED")
    assert branch == ("WITNESS_JUDGE_APPLY_PROPOSED" if repaired else "JUDGE_KEEPS_CURRENT")
    assert audit["span_termination_blocked_reason"] == "SPAN_LOCALIZATION_RESULT_REQUIRED"
    assert "span_context_resolution" not in audit
    judge = audit["judge"]
    assert json.loads(judge["span_review_raw_completion"]) == reply
    assert judge["needs_audio_requested"] is False
    # Missing scope remains an immutable negative result, including on cold read.
    cold = json.loads(json.dumps(audit))
    assert validate_persisted_judge_scopes(cold) == 1
    cold["judge"].pop("span_compilation")
    with pytest.raises(ValueError, match="SPAN_COMPILATION_REQUIRED"):
        validate_persisted_judge_scopes(cold)


@pytest.mark.parametrize("choice", ["CURRENT", "PROPOSED"])
def test_explicit_more_audio_is_not_closed_by_legacy_hearing_or_missing_scope(choice):
    (repaired, _branch, audit), reply = hearing(choice, True)
    assert repaired is False
    assert audit["judge"]["needs_audio_requested"] is True
    assert json.loads(audit["judge"]["span_review_raw_completion"]) == reply
    assert "span_context_resolution" not in audit
    assert audit["span_termination_blocked_reason"] == "SPAN_LOCALIZATION_RESULT_REQUIRED"


@pytest.mark.parametrize("end", [2, 3, 4])
def test_actual_outside_question_boundary_survives_typed_persistence_and_empty_review(end):
    # Vary the actual model question independently of the minimal edit. In
    # particular end=4 retains the unchanged character adjacent to that edit.
    text, authority, _ = applied_history(question_span=(0, end))
    cold = json.loads(json.dumps(authority))
    pending = pending_findings(text, cold)
    assert len(pending) == 1
    assert [pending[0]["span_start_codepoint"], pending[0]["span_end_codepoint"]] == [0, end]
    assert pending[0]["suspect"] == "你想，甲零呀"[:end]
    attempted = {
        "schema_version": "final-review-audit.v2", "reviewed_srt_sha256": "sha256:" + sha(text),
        "status": "CLEAN", "release_gate": "PASS", "findings": [], "validated_finding_count": 0,
    }
    final = enforce_review(text, cold, attempted)
    assert final["release_gate"] == "BLOCK"
    with pytest.raises(ValueError, match="FINAL_REVIEW_FINDING_REVIEW_PENDING"):
        validate_release_obligations(final)


def test_new_normal_off_scope_is_independent_of_all_scoring_answers():
    package = {"candidate_id": "new-synthetic-package", "media_sha256": "a" * 64}
    cases = [{"id": "new-window", "body_window_ms": [2000, 3000],
              "expected_text": "SCORING ONLY A", "accepted_reference_text": "SCORING ONLY B"}]
    cues = [{"index": i + 1, "start_ms": i * 1000, "end_ms": (i + 1) * 1000, "text": "CURRENT"}
            for i in range(6)]
    probes = normal_off_samples(package, cases, cues)
    changed = deepcopy(cases)
    changed[0].update(expected_text="OTHER ANSWER", accepted_reference_text="OTHER REFERENCE")
    assert probes == normal_off_samples(package, changed, cues)
    assert probes == [{"case_id": "new-window", "candidate_id": "new-synthetic-package",
                       "source_media_sha256": "a" * 64, "target_source_ms": [2000, 3000],
                       "context_source_ms": [0, 5000]}]
    assert "SCORING" not in json.dumps(probes)


@pytest.mark.parametrize("window", [[3000, 2000], [-1, 2000], [True, 2000], [7000, 8000]])
def test_invalid_normal_off_window_is_rejected_before_observation(window):
    with pytest.raises(ValueError):
        normal_off_samples({"candidate_id": "new", "media_sha256": "a" * 64},
                           [{"id": "new", "body_window_ms": window}],
                           [{"start_ms": 1000, "end_ms": 2000}])


def test_normal_off_cannot_borrow_argon_plan_or_grant(tmp_path):
    with pytest.raises(SystemExit) as stopped:
        main(["--normal-off", "--comparison-plan", str(tmp_path / "unread-plan")])
    assert stopped.value.code == 2


@pytest.mark.parametrize("needs_audio", [False, True])
def test_first_ordinary_judge_uses_runtime_binding_and_is_consumed_once(needs_audio):
    from src.autoslice.final_review_auditor import adjudicate_context_finding
    from tests.test_span_phoneme_resolution import SOURCE_SHA

    text = "两个电阻要串联。"
    finding = {"cue_index": 1, "suspect": "串联", "suggestion": "并联",
               "repair_class": "phonetic", "base_text_sha256": sha(text),
               "proposed_full_cue": "两个电阻要并联。"}
    observer = Observers(heard="liang ge dian zu yao chuan lian",
                        transcripts={"mai": text, "moss": text})
    prompts = []
    def model(prompt):
        if "\nSPAN_REVIEW_INPUT\n" not in prompt:
            # CURRENT still needs the distinct full-context closure contract.
            return json.dumps({"decision": "UNRESOLVED", "needs_audio": True,
                               "replacement_text": "", "reason": "No full-context closure supplied."})
        prompts.append(prompt)
        data = json.loads(prompt.split("\nSPAN_REVIEW_INPUT\n", 1)[1]
                          .split("\nEND_SPAN_REVIEW_INPUT\n", 1)[0])
        return json.dumps({
            "choice": "CURRENT", "needs_audio": needs_audio,
            "reason": "Synthetic source-bound decision, with its audio request preserved.",
            "current_utterance_supported": not needs_audio,
            "current_utterance_support_reason": "Current is supported." if not needs_audio else "More audio is required.",
            "span_review": {
                "binding_sha256": data["binding"]["binding_sha256"],
                "request_sha256": data["request_sha256"], "status": "UNLOCATABLE",
                "unlocatable_reason": "This fixture does not claim a target alignment.",
                "all_audio_questions_located": False, "target_needs_audio": needs_audio,
            },
        })
    output, audit = adjudicate_context_finding(
        srt(text), finding, entity_verifier=observer, judge_llm_call=model,
        require_complete_utterance_support=True,
    )
    history = audit["executed_span_judgments"]
    first = history["outcomes"][history["expected_executions"][0]["execution_id"]]
    original = first["span_compilation"]["request"]
    # The first raw decision must retain its actual runtime source and routes.
    # Retagging a pre-runtime completion after observing audio is not a fix.
    assert original["span_termination_source_sha256"] == SOURCE_SHA
    assert original["span_termination_inventory"] == list(observer.span_termination_ordinary_routes)
    assert json.loads(first["span_review_raw_completion"])["needs_audio"] is needs_audio
    assert validate_persisted_judge_scopes(json.loads(json.dumps(audit))) >= 1
    assert output == srt(text)
    assert not audit["repaired"]
    if needs_audio:
        assert observer.calls
        assert "span_context_resolution" not in audit
    else:
        # A real text-first terminal decision is consumed, without submitting
        # the same question again merely because runtime metadata was added.
        assert len(prompts) == 1
        assert not observer.calls


def test_text_first_proposal_is_not_redispatched_for_runtime_metadata_only():
    from src.autoslice.final_review_auditor import adjudicate_context_finding
    text, proposed = "两个电阻要串联。", "两个电阻要并联。"
    finding = {"cue_index": 1, "suspect": "串联", "suggestion": "并联",
               "repair_class": "phonetic", "base_text_sha256": sha(text),
               "proposed_full_cue": proposed}
    observer = Observers()
    calls = []
    def model(prompt):
        calls.append(prompt)
        return json.dumps({"choice": "PROPOSED", "needs_audio": False,
                           "reason": "Synthetic terminal text decision."})
    output, audit = adjudicate_context_finding(
        srt(text), finding, entity_verifier=observer, judge_llm_call=model,
        require_complete_utterance_support=True,
    )
    assert output == srt(proposed)
    assert audit["repaired"] is True
    assert not observer.calls
    assert len(calls) == 1, "Runtime metadata alone must not resubmit a completed word judgment"
    assert validate_persisted_judge_scopes(json.loads(json.dumps(audit))) >= 1


def test_unlocatable_question_can_acquire_configured_native_routes_after_cache_miss():
    from tests.test_span_phoneme_resolution import model_for, run_case
    class EmptyCache(Observers):
        def observe_cached_independent_witnesses(self, _target):
            return []
    observer = EmptyCache()
    original_model = model_for()
    def unlocatable(prompt):
        raw = json.loads(original_model(prompt))
        if isinstance(raw.get("span_review"), dict):
            raw["span_review"].update(
                status="UNLOCATABLE", unlocatable_reason="A real synthetic request needs another ear.",
                all_audio_questions_located=False, alignments=[],
            )
        return json.dumps(raw)
    output, audit, *_ = run_case(verifier=observer, model=unlocatable)
    assert sum(name == "independent" for name, _ in observer.calls) == 1
    assert audit["independent_asr"]["routes_attempted"] == 2
    assert audit["independent_asr"]["status"] == "OBSERVED"
    assert output == srt("你想，甲0呀")
    assert not audit["repaired"] and "span_context_resolution" not in audit
    assert audit["witness_judge"]["judge"]["needs_audio_requested"] is True
    assert validate_persisted_judge_scopes(json.loads(json.dumps(audit))) >= 1


def test_read_only_independent_cache_miss_never_dispatches_or_exhausts_routes():
    from src.autoslice.independent_context_adjudication import independent_asr_candidates
    class ReadOnly(Observers):
        def observe_cached_independent_witnesses(self, _target):
            return []
        def observe_independent_witnesses(self, _target):
            raise AssertionError("An explicitly read-only recovery cannot submit audio")
    current = "两个电阻要串联。"
    request = build_context_adjudication_request(
        srt(current), {"cue_index": 1, "suspect": "串联", "suggestion": "并联",
                       "repair_class": "phonetic", "base_text_sha256": sha(current),
                       "proposed_full_cue": "两个电阻要并联。"},
    )
    unchanged, witness, audit = independent_asr_candidates(request, ReadOnly(), cached_only=True)
    assert unchanged == request and witness is None
    assert audit["reason_code"] == "INDEPENDENT_ASR_CACHE_MISS"
    assert audit["routes_attempted"] == 0 and audit["routes_exhausted"] is False


def test_private_normal_source_survives_external_hardlink_metadata_but_rejects_local_drift(tmp_path):
    import os
    from scripts.subtitle_regression_eval import stage_normal_source_media
    from src.autoslice.native_context_witness import build_independent_context_witnesses
    original = tmp_path / "original.mp4"
    original.write_bytes(b"Synthetic media bytes for source identity; no provider is called.")
    expected = sha(original.read_bytes())
    before = original.stat()
    out = tmp_path / "private"
    out.mkdir()
    staged = stage_normal_source_media(original, out, expected)
    assert staged.read_bytes() == original.read_bytes()
    assert not os.path.samefile(staged, original)
    after = original.stat()
    assert (before.st_ino, before.st_mtime_ns, before.st_ctime_ns) == (
        after.st_ino, after.st_mtime_ns, after.st_ctime_ns)
    observer = build_independent_context_witnesses(
        lambda _: {}, staged, out / "native", ("mai", "moss"))
    shared = build_independent_context_witnesses(
        lambda _: {}, original, out / "shared-control", ("mai", "moss"))
    assert observer.span_termination_source_identity() == expected
    assert shared.span_termination_source_identity() == expected
    os.link(original, tmp_path / "another-reader.mp4")
    assert shared.span_termination_source_identity() is None
    assert observer.span_termination_source_identity() == expected
    # The original strict native guard is unchanged for the actual input.
    os.utime(staged, ns=(staged.stat().st_atime_ns, staged.stat().st_mtime_ns + 1))
    assert observer.span_termination_source_identity() is None


@pytest.mark.parametrize("damage", ["bytes", "symlink"])
def test_private_normal_source_never_overwrites_existing_bad_input(tmp_path, damage):
    from scripts.subtitle_regression_eval import stage_normal_source_media
    original = tmp_path / "original.mp4"
    original.write_bytes(b"original")
    out = tmp_path / "private"
    out.mkdir()
    target = out / "normal-off-source.mp4"
    if damage == "bytes":
        target.write_bytes(b"bad existing evidence")
    else:
        target.symlink_to(original)
    with pytest.raises(ValueError):
        stage_normal_source_media(original, out, sha(b"original"))
    assert original.read_bytes() == b"original"
    assert target.is_symlink() if damage == "symlink" else target.read_bytes() == b"bad existing evidence"
