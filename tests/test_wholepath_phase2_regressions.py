"""Phase-2 whole-path guard fixes: one rule-asserted regression per FALSE/POLICY check.

Every fixture is synthetic and offline. No test injects a ground-truth answer;
each assertion is the governing rule itself (docs/pipeline/41-semantic-repair.md):

* 41:1489-1492  same source/target/context/clock keeps an existing hearing;
                 source, window or clock drift is still rejected (K01, K02).
* 41:48-56      the only retained original that counts as resolved is an audio
                 ruling of KEEP_CURRENT (K27).
* 41:1540-1541  native occupation/failures/cache are never refunded or reset and
                 usable observations are reused first (K29, K07).
* 41:1540       an outcome that cannot be checked stays UNKNOWN; evidence must be
                 durable before judging it (K19).
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest


# --------------------------------------------------------------------- K01

def _padded_rig(tmp_path, monkeypatch, *, recording_override=None):
    from src.autoslice.clip_context import build_clip_context
    from src.autoslice.final_review_auditor import build_context_adjudication_request
    from tests.test_independent_context_adjudication import _padded_recording_provenance
    from tests.test_targeted_relisten_public_contract import build_offline_rig

    rig = build_offline_rig(tmp_path, monkeypatch)
    recording_sha, *_ = _padded_recording_provenance(tmp_path, rig.source)
    declared = recording_override or recording_sha
    rig.context = build_clip_context(
        candidate_id=rig.context["candidate_id"],
        spec={"date": "2026-10-19", "pieces": [{"start_ms": 438350, "end_ms": 617760,
              "source_media_sha256": "sha256:" + declared}]},
        draft_srt=rig.srt, authoritative_chat=(), topic_resolution={},
        session_topic_authorities=(),
        speech_memory_ledger_path=tmp_path / "empty-speech-memory.json",
        source_truth_ledger_path=None)
    rig.check = build_context_adjudication_request(
        rig.srt, rig.finding, clip_context=rig.context, source_media_timeline_offset_ms=0)
    return rig, recording_sha


def test_k01_blind_witness_on_padded_media_of_the_clip_recording_is_catalogued(tmp_path, monkeypatch):
    from src.autoslice.span_phoneme_resolution import observation_catalogue
    from src.autoslice.targeted_relisten import evidence_projection
    from tests.test_targeted_relisten_public_contract import acquire_narrow

    rig, recording_sha = _padded_rig(tmp_path, monkeypatch)
    witness = acquire_narrow(rig)
    padded_sha = hashlib.sha256(rig.source.read_bytes()).hexdigest()
    assert padded_sha != recording_sha
    assert witness["source_media_sha256"].removeprefix("sha256:") == padded_sha

    rows, invalid = observation_catalogue(rig.check, witness)
    assert invalid == []
    assert [row["kind"] for row in rows] == ["blind_pinyin"]
    # The relisten consumer counts the same reopened witness, not a new hearing.
    projection = evidence_projection(rig.check, witness=witness, ranges=[])
    assert projection["effective_distinct_observations"] == 1
    assert projection["rejected_observations"] == []


def test_k01_blind_witness_bound_to_another_recording_is_still_source_drift(tmp_path, monkeypatch):
    from src.autoslice.span_phoneme_resolution import observation_catalogue
    from tests.test_targeted_relisten_public_contract import acquire_narrow

    rig, _ = _padded_rig(tmp_path, monkeypatch, recording_override="f" * 64)
    witness = acquire_narrow(rig)
    rows, invalid = observation_catalogue(rig.check, witness)
    assert rows == []
    assert [row["reason_code"] for row in invalid] == ["SPAN_BLIND_SOURCE_MEDIA_DRIFT"]


# --------------------------------------------------------------------- K02

def _seven_cue_source(texts=None):
    from tests.test_independent_context_adjudication import _srt
    return _srt(*(texts or ["第一句", "第二句", "当前句", "第四句", "第五句", "第六句", "窗外句"]))


def _legacy_range(tmp_path, source):
    from src.autoslice.final_review_auditor import build_context_adjudication_request
    from tests.test_independent_context_adjudication import _range_artifacts, _range_clip_context

    finding = {"cue_index": 3, "suspect": "当前", "suggestion": "目前",
               "proposed_full_cue": "目前句", "repair_class": "phonetic"}
    request = build_context_adjudication_request(
        source, finding, clip_context=_range_clip_context(), source_media_timeline_offset_ms=100)
    observation, _broad, _window = _range_artifacts(tmp_path, source, request)
    return finding, observation


def _current_request(source, finding):
    from src.autoslice.final_review_auditor import build_context_adjudication_request
    from tests.test_independent_context_adjudication import _range_clip_context

    return build_context_adjudication_request(
        source, finding, clip_context=_range_clip_context(), source_media_timeline_offset_ms=100)


def test_k02_text_only_revision_keeps_a_subtitle_text_free_range_with_current_texts(tmp_path):
    from src.autoslice.independent_context_adjudication import validate_range_acoustic_observation

    original = _seven_cue_source()
    finding, observation = _legacy_range(tmp_path, original)
    # Inside (cue 2) and outside (cue 7) the window: the blind prompt held no
    # subtitle text, so neither revision is context drift (41:1489-1492).
    revised = original.replace("第二句", "第二版").replace("窗外句", "窗外改句")
    valid, reason = validate_range_acoustic_observation(
        revised, _current_request(revised, finding), observation)
    assert valid is not None, reason
    assert valid["status"] == "OBSERVED" and valid["mutation_authorized"] is False
    assert [row["text"] for row in valid["current_cue_texts"]] == [
        "第一句", "第二版", "当前句", "第四句", "第五句", "第六句"]
    assert [row["text"] for row in valid["cue_texts"]] == [
        "第一句", "第二句", "当前句", "第四句", "第五句", "第六句"]


@pytest.mark.parametrize("mutation", ["cue_time", "cue_count"])
def test_k02_cue_grid_change_still_rejects(tmp_path, mutation):
    from src.autoslice.independent_context_adjudication import validate_range_acoustic_observation

    original = _seven_cue_source()
    finding, observation = _legacy_range(tmp_path, original)
    if mutation == "cue_time":
        revised = original.replace("00:00:35,000 --> 00:00:39,000", "00:00:35,000 --> 00:00:38,000")
    else:
        revised = original + "\n8\n00:00:45,000 --> 00:00:48,000\n新增句\n"
    valid, reason = validate_range_acoustic_observation(
        revised, _current_request(revised, finding), observation)
    assert valid is None
    assert reason == "RANGE_ACOUSTIC_SRT_FILE_DRIFT"


def test_k02_exposure_is_checked_on_the_actual_prompt_not_the_saved_flag(tmp_path):
    from src.autoslice.final_review_auditor import build_context_adjudication_request
    from src.autoslice.independent_context_adjudication import (
        adapt_range_acoustic_context_observation, validate_range_acoustic_observation,
    )
    from tests.test_independent_context_adjudication import _range_artifacts, _range_clip_context

    texts = ["第一句话", "第二句话", "当前句话", "第四句话", "第五句话", "第六句话"]
    from tests.test_independent_context_adjudication import _srt
    original = _srt(*texts)
    finding = {"cue_index": 3, "suspect": "当前", "suggestion": "目前",
               "proposed_full_cue": "目前句话", "repair_class": "phonetic"}
    request = build_context_adjudication_request(
        original, finding, clip_context=_range_clip_context(), source_media_timeline_offset_ms=100)
    # The saved input still claims candidate_blind=True; the prompt says otherwise.
    observation, _broad, _window = _range_artifacts(
        tmp_path, original, request, prompt_text="参考字幕：第二句话")
    revised = original.replace("第五句话", "第五版话")
    current = _current_request(revised, finding)
    strict, reason = validate_range_acoustic_observation(revised, current, observation)
    assert strict is None and reason == "RANGE_ACOUSTIC_SRT_FILE_DRIFT"
    carried, carry_reason = adapt_range_acoustic_context_observation(revised, current, observation)
    assert carried is not None, carry_reason
    assert carried["status"] == "CONTEXT_ONLY"
    assert [row["text"] for row in carried["current_cue_texts"]][4] == "第五版话"


_FOUR_CUE_SRT = (
    "1\n00:00:05,000 --> 00:00:09,000\n第一句\n\n"
    "2\n00:00:10,000 --> 00:00:14,000\n当前句\n\n"
    "3\n00:00:15,000 --> 00:00:19,000\n第三句\n\n"
    "4\n00:00:25,000 --> 00:00:28,000\n窗外句\n"
)


def _range_case(tmp_path, monkeypatch, provider):
    from src.autoslice import native_context_witness as native
    from src.autoslice import independent_context_adjudication as independent
    from tests.test_final_recut_range_source_binding import install_gemini, package, sha
    from tests.test_native_context_witness import _install_range_writer, _range_requests

    _final, recording, padded, _ = package(tmp_path, 1)
    check, broad = _range_requests(sha(recording))
    check["whole_clip_current_srt"] = _FOUR_CUE_SRT
    out = tmp_path / "out"
    out.mkdir()
    calls = []
    if provider == "gemini":
        observe = install_gemini(monkeypatch, padded, out, calls)
    else:
        _install_range_writer(monkeypatch, padded, tmp_path / "cache", dispatches=calls)
        wrapper = native.build_independent_context_witnesses(
            fallback=lambda _: pytest.fail("range must not manufacture a narrow witness"),
            source_media=padded, output_dir=out, providers=(provider,),
            native_observation_cache_root=tmp_path / "cache")
        observe = lambda a, b: wrapper.observe_range_context(check_request=a, range_witness_request=b)
    raw = observe(check, broad)[0]
    valid, reason = independent.validate_range_acoustic_observation(_FOUR_CUE_SRT, check, raw)
    assert valid is not None, reason
    return check, raw, calls


@pytest.mark.parametrize("provider", ["gemini", "mai", "moss"])
def test_k02_segmented_and_native_ranges_follow_cue_grid_not_whole_srt_bytes(
        tmp_path, monkeypatch, provider):
    from src.autoslice.independent_context_adjudication import validate_range_acoustic_observation

    check, raw, calls = _range_case(tmp_path, monkeypatch, provider)
    dispatched = len(calls)
    for revised in (_FOUR_CUE_SRT.replace("窗外句", "窗外改句"),
                    _FOUR_CUE_SRT.replace("第一句", "第一版")):
        valid, reason = validate_range_acoustic_observation(
            revised, {**check, "whole_clip_current_srt": revised}, raw)
        assert valid is not None, reason
        assert valid["mutation_authorized"] is False
        current = [row["text"] for row in valid["current_cue_texts"]]
        assert current and all(text in revised for text in current)
    retimed = _FOUR_CUE_SRT.replace("00:00:25,000 --> 00:00:28,000", "00:00:24,000 --> 00:00:28,000")
    rejected, code = validate_range_acoustic_observation(
        retimed, {**check, "whole_clip_current_srt": retimed}, raw)
    assert rejected is None
    assert code in {"RANGE_ACOUSTIC_SRT_FILE_DRIFT", "NATIVE_RANGE_SOURCE_OR_SRT_DRIFT"}
    assert len(calls) == dispatched  # validation is a read-only consumer


# --------------------------------------------------------------------- K27

def _meaning_reversal_keep_current(**adjudication_overrides):
    from src.autoslice.acoustic_witness_adjudication import build_witness_request
    from src.autoslice.final_review_auditor import build_context_adjudication_request

    source = "1\n00:00:01,000 --> 00:00:03,000\n我不喜欢这个\n"
    request = build_context_adjudication_request(
        source,
        {"cue_index": 1, "suspect": "不喜欢", "suggestion": "喜欢",
         "proposed_full_cue": "我喜欢这个", "repair_class": "phonetic"},
        require_complete_utterance_support=True,
    )
    witness_request = build_witness_request(request)
    adjudication = {
        "status": "OBSERVED", "policy_branch": "JUDGE_KEEPS_CURRENT", "repaired": False,
        "timing_immutable": True, "request": request,
        "witness_judge": {
            "witness_request_sha256": witness_request["request_sha256"],
            "judge": {"status": "JUDGED", "choice": "CURRENT",
                      "check_request_sha256": request["request_sha256"],
                      "current_utterance_supported": True,
                      "current_utterance_support_reason": "整句原话在音频中完整可闻。"},
        },
        "mutation_authority": {"status": "NOT_APPLIED"},
    }
    adjudication.update(adjudication_overrides)
    return {"cue_index": 1, "content_risk": "meaning_reversal",
            "risk_basis": "否定词的有无改变整句态度。",
            "exact_release_adjudication": adjudication}


def test_k27_complete_listening_keep_current_resolves_a_meaning_reversal_row():
    from src.autoslice.final_review_contract import is_audio_decided_keep_current
    from src.autoslice.final_review_meaning_policy import current_meaning_equivalent

    row = _meaning_reversal_keep_current()
    assert current_meaning_equivalent(row) is False
    assert is_audio_decided_keep_current(row) is True


@pytest.mark.parametrize("damage", [
    "uncertain_keep", "support_missing", "support_false", "legacy_request_less",
    "other_keep_branch", "not_observed", "applied", "timing_mutable",
])
def test_k27_incomplete_or_non_listening_keep_stays_unresolved(damage):
    from src.autoslice.acoustic_witness_adjudication import REQUIRE_COMPLETE_UTTERANCE_SUPPORT
    from src.autoslice.final_review_contract import is_audio_decided_keep_current

    row = _meaning_reversal_keep_current()
    adjudication = row["exact_release_adjudication"]
    judge = adjudication["witness_judge"]["judge"]
    if damage == "uncertain_keep":
        adjudication["policy_branch"] = "JUDGE_UNCERTAIN_KEEP_CURRENT"
    elif damage == "support_missing":
        judge.pop("current_utterance_supported")
        judge.pop("current_utterance_support_reason")
    elif damage == "support_false":
        judge["current_utterance_supported"] = False
    elif damage == "legacy_request_less":
        adjudication.pop("request")
    elif damage == "other_keep_branch":
        adjudication["policy_branch"] = "TARGET_INAUDIBLE_KEEP_CURRENT"
    elif damage == "not_observed":
        adjudication["status"] = "UNCERTAIN"
    elif damage == "applied":
        adjudication["mutation_authority"] = {"status": "APPLIED"}
    else:
        adjudication["timing_immutable"] = False
    if damage == "legacy_request_less":
        assert REQUIRE_COMPLETE_UTTERANCE_SUPPORT not in adjudication
    assert is_audio_decided_keep_current(row) is False


def test_k27_noncritical_candidate_risk_requirement_is_kept():
    from src.autoslice.final_review_contract import is_audio_decided_keep_current

    row = _meaning_reversal_keep_current()
    # A source-backed entity candidate on a noncritical row still needs its
    # own candidate risk assessment; the listening ruling does not replace it.
    row.update(content_risk="noncritical_uncertainty", candidate_content_risk_required=True,
               repair_class="source_backed_entity",
               candidate_provenance={"source": "chat", "text": "喜欢"})
    assert is_audio_decided_keep_current(row) is False
    plain = _meaning_reversal_keep_current()
    plain.update(content_risk="noncritical_uncertainty")
    assert is_audio_decided_keep_current(plain) is True


# --------------------------------------------------------------------- K29

def test_k29_completed_non_partial_parent_native_account_is_an_exact_prefix(tmp_path, monkeypatch):
    from src.autoslice.native_audio_budget_receipt import persist_native_audio_budget_receipt
    from src.autoslice.supplement_audio_budget import get_budget, start_budget
    from tests.test_retained_reviewed_draft import _fixture, _load

    spec, out, padded, _ = _fixture(tmp_path)
    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path / "runtime"))
    source_sha = hashlib.sha256(padded.read_bytes()).hexdigest()
    budget = start_budget(padded)
    for start in (0, 50, 120):
        token = budget.consume("mai", "MAI-Transcribe-2", start, start + 20)
        budget.finish_attempt(token, status="FAILED", reason_code="FIXTURE_OFFLINE")
    parent_receipt = out / "native-audio-budget.json"
    persist_native_audio_budget_receipt(
        source_media=padded, source_media_sha256=source_sha, receipt_path=parent_receipt)
    parent_bytes = parent_receipt.read_bytes()
    start_budget(padded)  # a fresh producer process has no in-memory account

    result = _load(spec, out, padded)

    assert "partial_evidence" not in result
    native = result["retained_native"]
    snapshot = get_budget(padded).snapshot()
    assert [row["attempt_id"] for row in snapshot["attempts"]] == [1, 2, 3]
    assert [row["status"] for row in snapshot["attempts"]] == ["FAILED"] * 3
    assert native.budget_receipt == (
        tmp_path / "runtime" / ".native-audio-resume" / "candidate-1" / source_sha
        / "native-audio-budget.json")
    assert json.loads(native.budget_receipt.read_bytes())["budget"]["attempts"] == snapshot["attempts"]
    assert get_budget(padded).consume("mai", "MAI-Transcribe-2", 160, 180) == 4
    assert parent_receipt.read_bytes() == parent_bytes
    assert result["retained_native_parent_output_root"] == str(out)
    assert "retained_native_account" not in result["proof"]  # proof identity is re-checked


# --------------------------------------------------------------------- K07

def _store_entry(verifier_module, output_dir, clip_sha):
    job_dir = output_dir / "entity_verdicts" / "job"
    job_dir.mkdir(parents=True)
    prompt_path = job_dir / "prompt.md"
    response_path = job_dir / "response.json"
    prompt_path.write_text("candidate-free prompt", encoding="utf-8")
    observed = {
        "schema_version": verifier_module.WITNESS_SCHEMA, "status": "OBSERVED",
        "target_audible": True, "heard_pinyin": "yuan shi ting xie",
        "uncertain_positions": [], "syllable_count": 4, "confidence": 0.95, "reason": "clear",
    }
    response_path.write_text(json.dumps(observed), encoding="utf-8")
    outcome = verifier_module._EntityProviderOutcome(
        observed=observed, provider="agy", model=verifier_module.ENTITY_AUDIO_MODEL,
        prompt_path=prompt_path, response_path=response_path, accepted_key_tier=None,
        paid_policy_stamp=None, provider_failures=[])
    identity = verifier_module._sha256(prompt_path)
    verifier_module._store_witness_acoustic_cache(
        output_dir=output_dir, clip_sha256=clip_sha, observed=observed, outcome=outcome,
        prompt_identity_sha256=identity)
    return identity


@pytest.mark.parametrize("family", ["runtime_base", "retained_parent"])
def test_k07_private_run_reuses_runtime_or_retained_parent_witness_cache(tmp_path, monkeypatch, family):
    from src.autoslice import entity_audio_verifier as verifier_module

    clip_sha = "a" * 64
    base = tmp_path / "base"
    if family == "runtime_base":
        producer = base / "out" / "2026-10-11" / "auto_case"
        monkeypatch.setenv("AUTOSLICE_BASE", str(base))
        prior_roots = ()
    else:
        producer = base / "private-runs" / "repair" / "release-v4" / "output" / "auto_case"
        monkeypatch.delenv("AUTOSLICE_BASE", raising=False)
        prior_roots = (producer,)
    identity = _store_entry(verifier_module, producer, clip_sha)
    private = base / "private-runs" / "phase" / "normal-v1" / "output" / "auto_case"
    job_dir = private / "entity_verdicts" / "job"
    job_dir.mkdir(parents=True)
    served = verifier_module._serve_witness_acoustic_cache(
        output_dir=private, clip_sha256=clip_sha, job_dir=job_dir,
        expected_model=verifier_module.ENTITY_AUDIO_MODEL,
        expected_prompt_identity_sha256=identity, prior_roots=prior_roots)
    assert served is not None and served.served_from_cache is True
    assert served.observed["heard_pinyin"] == "yuan shi ting xie"


def test_k07_without_runtime_base_or_declared_parent_the_cache_stays_isolated(tmp_path, monkeypatch):
    from src.autoslice import entity_audio_verifier as verifier_module

    monkeypatch.delenv("AUTOSLICE_BASE", raising=False)
    clip_sha = "a" * 64
    base = tmp_path / "base"
    identity = _store_entry(verifier_module, base / "out" / "2026-10-11" / "auto_case", clip_sha)
    private = base / "private-runs" / "phase" / "normal-v1" / "output" / "auto_case"
    job_dir = private / "entity_verdicts" / "job"
    job_dir.mkdir(parents=True)
    assert verifier_module._serve_witness_acoustic_cache(
        output_dir=private, clip_sha256=clip_sha, job_dir=job_dir,
        expected_model=verifier_module.ENTITY_AUDIO_MODEL,
        expected_prompt_identity_sha256=identity) is None


# --------------------------------------------------------------------- K19

def test_k19_rejected_sentinel_reply_keeps_raw_and_usage_before_judging(tmp_path):
    import scripts.final_media_review_gemini_api_adapter as adapter
    from tests.test_final_media_review_gemini_api_adapter import FakeFileRunner, _sentinel_fixture

    root, script, request, response, media, prompt, environment = _sentinel_fixture(tmp_path)
    fake = FakeFileRunner(["The answer is 12345678"])
    with pytest.raises(adapter.AdapterError, match="not exactly eight digits"):
        adapter.run_sentinel(request_path=request, response_path=response, input_path=media,
                             prompt_path=prompt, environment=environment, file_runner=fake,
                             adapter_path=script)
    assert not response.exists()  # the verdict is still a rejection
    saved, = root.glob("final-media-review-gemini-api-runs/*/*/sentinel-provider-outcome.json")
    outcome = json.loads(saved.read_text())
    assert outcome["raw_response_text"] == "The answer is 12345678"
    assert outcome["provider_usage"]["http_request_count"] == 4
    assert outcome["provider_usage"]["paid_backup_used"] is False


def test_k19_unparseable_final_review_keeps_both_modalities_raw_and_usage(tmp_path):
    import scripts.final_media_review_gemini_api_adapter as adapter
    from tests.test_final_media_review_gemini_api_adapter import (
        FakeFileRunner, _final_fixture, _review,
    )

    root, script, request, response, _video, _audio, environment = _final_fixture(tmp_path)
    fake = FakeFileRunner(["not json at all", _review("PASS", "audio-whole")])
    with pytest.raises(adapter.AdapterError, match="not JSON"):
        adapter.run_final_review(request_path=request, response_path=response,
                                 environment=environment, file_runner=fake, adapter_path=script)
    assert not response.exists()
    run, = root.glob("final-media-review-gemini-api-runs/*/*")
    video = json.loads((run / "continuous_source_video-provider-outcome.json").read_text())
    audio = json.loads((run / "raw_audio-provider-outcome.json").read_text())
    assert video["raw_response_text"] == "not json at all"
    assert audio["provider_usage"]["mime_type"] == "audio/wav"
    assert len(fake.calls) == 2


# ------------------------------------------------------------------- COST-01

def _nested_entry(tmp_path, monkeypatch, generate):
    """Normal nested acoustic entry with offline credentials and transport seams."""
    import scripts.session_autoslice as runtime
    import src.autoslice.agy_gemini_client as gemini
    import src.autoslice.nested_media_acoustic_runtime as acoustic_runtime
    import src.autoslice.producer_final_review_transport as final_transport
    import src.autoslice.producer_nested_acoustic as producer
    from types import SimpleNamespace

    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path / "base"))
    monkeypatch.delenv("GEMINI_PAID_BACKUP_DEV_EXCEPTION", raising=False)
    monkeypatch.delenv("GEMINI_PAID_BACKUP_DAILY_CAP", raising=False)
    monkeypatch.setattr(runtime, "load_gemini_credentials", lambda: {
        "GEMINI_API_KEY": "free-1", "GEMINI_API_KEY_2": "free-2",
        "GEMINI_API_KEY_3": "free-3", "GEMINI_KEY_BACKUP": "paid-key"})

    def cpa(_prompt):
        return "unused"
    cpa.cpa_cache_identity = {"transport": "fixture.cpa", "models": ["fixture"], "effort": "medium"}
    monkeypatch.setattr(final_transport, "build_final_review_llm_call", lambda **_kwargs: cpa)
    monkeypatch.setattr(gemini, "generate_content", generate)
    captured = {}
    monkeypatch.setattr(acoustic_runtime, "build_runtime_acoustic_callbacks",
                        lambda **kwargs: captured.update(kwargs) or SimpleNamespace())
    monkeypatch.setattr(producer, "observe_prepared_nested_acoustics",
                        lambda **kwargs: captured.update(kwargs) or {})
    producer.build_runtime_nested_acoustic_observer(tmp_path)(spec={"nested_media_acoustic": {}})
    captured["callbacks_factory"](descriptor={}, candidate_id="candidate",
                                  observation_id="normal", evidence_root=tmp_path)
    return captured


def _http(status):
    import urllib.error
    return urllib.error.HTTPError("https://example.invalid", status, "failure", {}, None)


def _paid_ledger(tmp_path):
    rows = []
    for path in (tmp_path / "base" / "state" / "gemini-paid-backup").glob("usage-*.jsonl"):
        rows.extend(json.loads(line) for line in path.read_text().splitlines() if line.strip())
    return rows


def test_cost01_all_free_routes_429_admit_the_paid_key_in_the_same_round(tmp_path, monkeypatch):
    calls = []

    def generate(*, key, **_kwargs):
        calls.append(key)
        if key.startswith("free-"):
            raise _http(429)
        return "paid response"

    captured = _nested_entry(tmp_path, monkeypatch, generate)
    assert captured["audio_generate"](prompt="p", inline_data=b"a") == "paid response"
    assert calls == ["free-1", "free-2", "free-3", "paid-key"]
    trace = captured["audio_attempts"]()
    paid = trace[-1]
    assert paid["key_tier"] == "paid_backup" and paid["status"] == "RESPONDED"
    assert paid["key_ordinal"] == 4 and paid["gate_reason"].startswith("QUOTA_FASTPATH:")
    assert "paid-key" not in json.dumps(trace) and "free-1" not in json.dumps(trace)
    ledger = _paid_ledger(tmp_path)
    assert len(ledger) == 1 and ledger[0]["purpose"] == "nested_media_acoustic"
    assert ledger[0]["admission"].startswith("QUOTA_FASTPATH:")
    # No cooldown: the next window may use the paid key again, ledgered again;
    # the exhausted free routes are not re-dispatched.
    assert captured["audio_generate"](prompt="q", inline_data=b"b") == "paid response"
    assert calls[4:] == ["paid-key"]
    assert len(_paid_ledger(tmp_path)) == 2


def test_cost01_non_quota_free_failure_does_not_reach_the_paid_key(tmp_path, monkeypatch):
    calls = []

    def generate(*, key, **_kwargs):
        calls.append(key)
        raise _http(503)

    captured = _nested_entry(tmp_path, monkeypatch, generate)
    with pytest.raises(Exception):
        captured["audio_generate"](prompt="p", inline_data=b"a")
    assert calls == ["free-1"]
    assert _paid_ledger(tmp_path) == []


def test_cost01_paid_429_keeps_unknown_and_stops_redispatch(tmp_path, monkeypatch):
    calls = []

    def generate(*, key, **_kwargs):
        calls.append(key)
        raise _http(429)

    captured = _nested_entry(tmp_path, monkeypatch, generate)
    with pytest.raises(Exception):
        captured["audio_generate"](prompt="p", inline_data=b"a")
    assert calls == ["free-1", "free-2", "free-3", "paid-key"]
    assert captured["audio_attempts"]()[-1]["status"] == "FAILED"
    # Later windows stay unknown/KEEP without dispatching the same failure.
    with pytest.raises(Exception):
        captured["audio_generate"](prompt="q", inline_data=b"b")
    assert calls == ["free-1", "free-2", "free-3", "paid-key"]
    assert len(_paid_ledger(tmp_path)) == 1


def test_cost01_no_paid_credential_keeps_the_free_only_outcome(tmp_path, monkeypatch):
    import scripts.session_autoslice as runtime
    calls = []

    def generate(*, key, **_kwargs):
        calls.append(key)
        raise _http(429)

    captured = _nested_entry(tmp_path, monkeypatch, generate)
    monkeypatch.setattr(runtime, "load_gemini_credentials", lambda: {})
    with pytest.raises(Exception):
        captured["audio_generate"](prompt="p", inline_data=b"a")
    assert "paid-key" in calls  # the entry was built with a paid credential
    calls.clear()
    # A fresh entry without a configured paid key never dispatches it.
    monkeypatch.setattr(runtime, "load_gemini_credentials", lambda: {
        "GEMINI_API_KEY": "free-1", "GEMINI_API_KEY_2": "free-2", "GEMINI_API_KEY_3": "free-3"})
    import src.autoslice.producer_nested_acoustic as producer
    import src.autoslice.nested_media_acoustic_runtime as acoustic_runtime
    from types import SimpleNamespace
    second = {}
    monkeypatch.setattr(acoustic_runtime, "build_runtime_acoustic_callbacks",
                        lambda **kwargs: second.update(kwargs) or SimpleNamespace())
    monkeypatch.setattr(producer, "observe_prepared_nested_acoustics",
                        lambda **kwargs: second.update(kwargs) or {})
    producer.build_runtime_nested_acoustic_observer(tmp_path)(spec={"nested_media_acoustic": {}})
    second["callbacks_factory"](descriptor={}, candidate_id="candidate",
                                observation_id="normal", evidence_root=tmp_path)
    with pytest.raises(Exception):
        second["audio_generate"](prompt="p", inline_data=b"a")
    assert calls == ["free-1", "free-2", "free-3"]
    assert second["audio_attempts"]()[-1]["gate_reason"].endswith("PAID_KEY_NOT_CONFIGURED")


# ------------------------------------------------- evidence identity (inode)

def _restore_copy(path):
    """Byte-identical restore: new inode/ctime, restored mtime (copy2)."""
    import os
    import shutil
    backup = path.with_name(path.name + ".backup")
    shutil.copy2(path, backup)
    before = path.stat()
    path.unlink()
    shutil.copy2(backup, path)
    backup.unlink()
    after = path.stat()
    assert after.st_ino != before.st_ino or after.st_ctime_ns != before.st_ctime_ns
    assert after.st_mtime_ns == before.st_mtime_ns
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))


def _flip_one_byte_keep_attributes(path):
    import os
    info = path.stat()
    raw = bytearray(path.read_bytes())
    raw[len(raw) // 2] ^= 0x01
    path.write_bytes(bytes(raw))
    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))


def test_identity_relisten_witness_survives_byte_identical_restore(tmp_path, monkeypatch):
    from src.autoslice.entity_witness_cache_probe import validate_relisten_witness_artifacts
    from src.autoslice.acoustic_witness_adjudication import build_witness_request
    from tests.test_targeted_relisten_public_contract import acquire_narrow

    rig, _ = _padded_rig(tmp_path, monkeypatch)
    witness = acquire_narrow(rig)
    request = build_witness_request(rig.check)
    _restore_copy(rig.source)
    assert validate_relisten_witness_artifacts(request=request, witness=witness)
    _flip_one_byte_keep_attributes(rig.source)
    with pytest.raises(ValueError, match="RELISTEN_NARROW_SOURCE_DRIFT"):
        validate_relisten_witness_artifacts(request=request, witness=witness)


def test_identity_retained_history_copy_receipt_survives_restore(tmp_path):
    from src.autoslice import retained_reviewed_draft as retained_reader
    from tests.test_retained_preparation_checkpoint import _history_copy_fixture, without_provider_dispatch

    source, path, raw, requested = _history_copy_fixture(tmp_path)
    retained_reader._write_history(path, requested, label="PENDING_ENTITY_INPUT")
    history = source / "candidate.parent_srt"
    _restore_copy(history)
    _restore_copy(path.parent / history.name)
    with without_provider_dispatch():
        retained_reader._write_history(path, requested, label="PENDING_ENTITY_INPUT")
    _flip_one_byte_keep_attributes(history)
    with without_provider_dispatch(), pytest.raises((ValueError, OSError)):
        retained_reader._write_history(path, requested, label="PENDING_ENTITY_INPUT")


def test_identity_helpers_ignore_placement_but_bind_content():
    from src.autoslice.evidence_identity import content_view, recorded_source_stat_matches

    recorded = [1, 100, 0o100644, 0, 10, 555, 999]
    restored = [2, 200, 0o100644, 1001, 10, 555, 1234]
    assert recorded_source_stat_matches(recorded, restored)
    assert not recorded_source_stat_matches(recorded, [2, 200, 0o100644, 0, 11, 555, 1234])
    assert not recorded_source_stat_matches(recorded, [2, 200, 0o100644, 0, 10, 556, 1234])
    row = {"path": "/x", "sha256": "a" * 64, "stat": {"st_dev": 1, "st_ino": 2, "st_size": 3,
                                                     "st_mtime_ns": 4, "st_ctime_ns": 5}}
    moved = {**row, "stat": {**row["stat"], "st_dev": 9, "st_ino": 8, "st_ctime_ns": 7}}
    assert content_view(row) == content_view(moved)
    assert content_view(row) != content_view({**row, "sha256": "b" * 64})


def test_persisted_stat_bindings_are_compared_only_through_the_shared_helper():
    """Structural guard: no evidence reader compares a recorded stat binding by equality."""
    import re
    root = Path(__file__).resolve().parents[1]
    offenders = []
    pattern = re.compile(
        r"""(get\(\s*["']source_stat_binding["']\s*\)|\[\s*["']source_stat_binding["']\s*\])\s*(==|!=)"""
        r"""|(==|!=)\s*\S*get\(\s*["']source_stat_binding["']\s*\)""")
    for path in list((root / "src").rglob("*.py")) + list((root / "scripts").rglob("*.py")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line):
                offenders.append(f"{path.relative_to(root)}:{number}")
    assert offenders == []


# ---------------------------------------- shared identity helpers (tier 2)

_ANCESTRY_CALLERS = {
    "src/autoslice/span_phoneme_resolution.py": "_blind_source_is_clip_recording",
    "src/autoslice/targeted_relisten.py": "evidence_projection",
    "src/autoslice/context_assisted_audio.py": "validate_prior_blind",
    "src/autoslice/argon_targeted_relisten.py": "_recommendation",
}
_CONSUMED_TEXT_CALLERS = {
    "src/autoslice/independent_context_adjudication.py": (
        "_validate_range_acoustic_observation",
        "validate_native_range_context_observation",
        "validate_segmented_range_context_observation",
    ),
}


def _function_source(rel, name):
    import ast
    root = Path(__file__).resolve().parents[1]
    tree = ast.parse((root / rel).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment((root / rel).read_text(encoding="utf-8"), node)
    raise AssertionError(f"{rel}:{name} missing")


@pytest.mark.parametrize("rel,name", sorted(_ANCESTRY_CALLERS.items()))
def test_same_source_checks_use_the_shared_validated_ancestry(rel, name):
    """41 : padded/recut vs recording only via validated_media_recordings."""
    if rel == 'src/autoslice/context_assisted_audio.py' and name == 'validate_prior_blind':
        # Assistance consumes the catalogue's verified recording ancestry;
        # it must not rebuild a second ancestry predicate after projection.
        assert 'observation_catalogue' in _function_source(rel, name)
        assert '_blind_source_is_clip_recording' in _function_source(
            'src/autoslice/span_phoneme_resolution.py', 'observation_catalogue')
        assert 'validated_media_recordings' in _function_source(
            'src/autoslice/span_phoneme_resolution.py', '_blind_source_is_clip_recording')
        return
    assert "validated_media_recordings" in _function_source(rel, name)


@pytest.mark.parametrize("name", _CONSUMED_TEXT_CALLERS["src/autoslice/independent_context_adjudication.py"])
def test_range_validators_use_the_shared_consumed_content_rule(name):
    source = _function_source("src/autoslice/independent_context_adjudication.py", name)
    assert "subtitle_text_free_observation_survives" in source or "same_cue_grid" in source


def test_budget_ledger_sources_use_the_shared_validated_ancestry():
    source = _function_source("src/autoslice/targeted_relisten.py", "validate_budget_binding")
    assert "_ledger_source_is_clip_recording" in source
    assert "validated_media_recordings" in _function_source(
        "src/autoslice/targeted_relisten.py", "_ledger_source_is_clip_recording")


def test_native_ledger_on_padded_media_is_the_clip_recordings_ledger(tmp_path):
    from src.autoslice.targeted_relisten import _ledger_source_is_clip_recording
    from tests.test_independent_context_adjudication import _padded_recording_provenance

    padded = tmp_path / "padded.mp4"
    padded.write_bytes(b"padded media bytes")
    padded_sha = hashlib.sha256(padded.read_bytes()).hexdigest()
    recording_sha, *_ = _padded_recording_provenance(tmp_path, padded)
    assert _ledger_source_is_clip_recording(padded_sha, str(padded), {recording_sha})
    assert _ledger_source_is_clip_recording(recording_sha, None, {recording_sha})
    assert not _ledger_source_is_clip_recording(padded_sha, str(padded), {"f" * 64})


# ------------------------------------------- xiamen published-recall wiring

def test_published_recall_successor_inputs_are_derived_without_operator_choice(tmp_path):
    import importlib.util
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "build_first_publication_recall_spec", root / "scripts/build_first_publication_recall_spec.py")
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)

    output_root = tmp_path / "run" / "output"
    checkpoint = output_root / "semantictalk_1_2" / "semantictalk_1_2.source-text-checkpoint.json"
    checkpoint.parent.mkdir(parents=True)
    checkpoint.write_text("{}")
    parent = tmp_path / "run" / "SPEC.json"
    parent.write_text(json.dumps({"candidate_id": "semantictalk_1_2", "output_root": str(output_root)}))
    derived = builder.derive_successor_inputs(parent)
    assert derived["source_checkpoint"] == checkpoint
    assert derived["output_root"].parent.parent == output_root.parent
    assert not derived["output_root"].exists() and derived["output_spec"].name.startswith("SPEC-published-recall-")
    assert builder.derive_successor_inputs(parent) == derived  # deterministic
    checkpoint.unlink()
    with pytest.raises(ValueError, match="SOURCE_CHECKPOINT_MISSING"):
        builder.derive_successor_inputs(parent)


# ------------------------------------- C legacy: completed retained + entity

def test_completed_retained_review_with_pending_entity_findings_needs_its_saved_decision(tmp_path, monkeypatch):
    """run_text_pipeline: a completed retained review is replayed (zero calls) and the
    pending entity questions are still checked against that restored review; a
    restored review that lacks the decision cannot settle them (41:48)."""
    import copy as _copy
    from src.autoslice.retained_reviewed_draft import RetainedReviewedDraftError
    from tests.test_retained_entity_input_recovery import _consume, _native_current_review, _pending

    monkeypatch.delenv("AUTOSLICE_BASE", raising=False)
    retained, _original, context = _pending(tmp_path)
    saved = _native_current_review(context)
    restored = _copy.deepcopy(saved)  # what consume_completed_retained_review returns
    authority = _consume(tmp_path / "with-decision", retained, restored)
    assert authority["retained_entity_review"]["status"] == "CURRENT_REVIEW_COMPLETED"
    empty = {**_copy.deepcopy(saved), "findings": [], "status": "CLEAN"}
    with pytest.raises(RetainedReviewedDraftError, match="PENDING_ENTITY"):
        _consume(tmp_path / "without-decision", retained, empty)
    # The pipeline order this test stands for: the completed branch replays the
    # saved review, then the unconditional require_pending_entity_review runs.
    source = (Path(__file__).resolve().parents[1] / "src/autoslice/producer_text_pipeline.py").read_text()
    replay = source.index("reviewed_srt, final_review_audit = consume_completed_retained_review(")
    gate = source.index("    require_pending_entity_review(\n        retained_reviewed_draft,", replay)
    assert "if completed_retained_review is not None:" in source[replay - 120:replay]
    assert source[replay:gate].count("\n    require_pending_entity_review(") == 0


# ---------------------------- tier-2 identity scan: durable stat comparisons

def _restore_in_place(path: Path, payload: bytes | None = None):
    """A byte-identical (or changed) restore: new inode, same size/mtime."""
    import os
    info = os.stat(path)
    data = path.read_bytes() if payload is None else payload
    staged = path.with_name(path.name + ".restored")
    staged.write_bytes(data)
    os.utime(staged, ns=(info.st_atime_ns, info.st_mtime_ns))
    os.replace(staged, path)
    assert os.stat(path).st_ino != info.st_ino


def test_identity_speaker_routing_claim_survives_restore_of_its_segment(tmp_path):
    from src.autoslice.speaker_session_router import SpeakerRoutingError, generate_speaker_routing
    from tests.test_speaker_session_router import _provider_fixture, _request_fixture, _verify
    request_path, candidate = _request_fixture(tmp_path)
    provider_path = _provider_fixture(tmp_path, request_path, candidate)
    claim_path = tmp_path / "claim.json"
    claim = generate_speaker_routing(request_path=request_path, output_path=claim_path,
                                     provider_evidence_path=provider_path)
    segment = Path(str(candidate["segment_path"]))
    _restore_in_place(segment)
    assert _verify(claim, claim_path, candidate).candidate_id == "candidate-1"
    _restore_in_place(segment, b"sealed source segmenX")  # same size and mtime, other bytes
    with pytest.raises(SpeakerRoutingError, match="binding mismatch|drifted"):
        _verify(claim, claim_path, candidate)


def test_identity_historical_fastlane_preimage_ignores_placement_not_content(monkeypatch):
    from src.autoslice import historical_fastlane_authority as fast
    entries = [
        {"relative_path": ".", "type": "directory", "device": 1, "inode": 2, "mode": 0o755,
         "mtime_ns": 10, "ctime_ns": 11},
        {"relative_path": "a.flv", "type": "regular", "sha256": "a" * 64, "size": 3,
         "device": 1, "inode": 3, "mode": 0o644, "mtime_ns": 12, "ctime_ns": 13},
    ]
    document = {"recording_root": "/r", "date": "2026-10-01", "room_id": "1", "entries": entries,
                "inventory_sha256": fast._json_sha(entries), "inventory_audit_sha256": "x",
                "adapter_state_path": "/s"}

    def observed_with(**changes):
        moved = [dict(e, device=9, inode=e["inode"] + 100, ctime_ns=99) for e in entries]
        moved[1].update(changes)
        return dict(document, entries=moved, inventory_sha256=fast._json_sha(moved))

    kwargs = dict(recording_root=Path("/r"), date="2026-10-01", room_id="1",
                  adapter_state_path=Path("/s"), now=None)
    monkeypatch.setattr(fast, "_source_binding", lambda *a, **k: observed_with())
    fast._validate_source_binding(document, **kwargs)
    for change in ({"sha256": "b" * 64}, {"size": 4}, {"mtime_ns": 14}):
        monkeypatch.setattr(fast, "_source_binding", lambda *a, _c=change, **k: observed_with(**_c))
        with pytest.raises(fast.HistoricalFastlaneAuthorityError, match="PREIMAGE_DRIFT"):
            fast._validate_source_binding(document, **kwargs)
    forged = dict(document, inventory_sha256="0" * 64)
    monkeypatch.setattr(fast, "_source_binding", lambda *a, **k: observed_with())
    with pytest.raises(fast.HistoricalFastlaneAuthorityError, match="PREIMAGE_DRIFT"):
        fast._validate_source_binding(forged, **kwargs)


# ------------------------------- K21: read-aloud reuses complete responses

def _bound_cpa_call(tmp_path, calls, *, models=("gpt-fixture",)):
    def llm_call(prompt):
        calls.append(prompt)
        return json.dumps({"is_read_aloud": False, "confidence": 0.99})
    llm_call.cpa_cache_identity = {"transport": "cpa_command", "models": list(models), "effort": "low"}
    llm_call.provider_runtime_binding = {"provider_transport": "command",
                                         "provider_credential_source": "FIXTURE_BOUND"}
    llm_call.completed_review_root = tmp_path / "completed-reviews"
    return llm_call


def test_k21_read_aloud_same_binding_reuses_the_completed_response(tmp_path):
    from src.autoslice import read_aloud_llm_verifier as module
    from tests.test_read_aloud_llm_verifier import _request
    calls = []
    first = module.build_cpa_read_aloud_verifier(_bound_cpa_call(tmp_path, calls))(_request())
    again = module.build_cpa_read_aloud_verifier(_bound_cpa_call(tmp_path, calls))(_request())
    assert len(calls) == 1 and again == first
    assert first["reason_code"] == "READ_ALOUD_REJECTED_BY_CONTEXT"
    other_model = module.build_cpa_read_aloud_verifier(
        _bound_cpa_call(tmp_path, calls, models=("gpt-other",)))(_request())
    assert len(calls) == 2 and other_model["canonical_entity"] == first["canonical_entity"]
    module.build_cpa_read_aloud_verifier(_bound_cpa_call(tmp_path, calls))(
        _request(context_after="不是黑色"))
    assert len(calls) == 3  # another question is another prompt


def test_k21_read_aloud_without_bound_provider_dispatches_as_before(tmp_path):
    from src.autoslice import read_aloud_llm_verifier as module
    from tests.test_read_aloud_llm_verifier import _request
    calls = []

    def unbound(prompt):
        calls.append(prompt)
        return json.dumps({"is_read_aloud": False, "confidence": 0.99})
    for _ in range(2):
        module.build_cpa_read_aloud_verifier(unbound)(_request())
    assert len(calls) == 2 and not (tmp_path / "completed-reviews").exists()


def test_k21_read_aloud_damaged_checkpoint_is_a_provider_failure_not_a_verdict(tmp_path):
    from src.autoslice import read_aloud_llm_verifier as module
    from tests.test_read_aloud_llm_verifier import _request
    calls = []
    module.build_cpa_read_aloud_verifier(_bound_cpa_call(tmp_path, calls))(_request())
    saved, = (tmp_path / "completed-reviews").rglob("*.json")
    saved.write_text(saved.read_text().replace("0.99", "0.98"))
    audio = []

    def witness(request):
        audio.append(request)
        return {"schema_version": "subtitle-span-acoustic-witness.v1",
                "request_sha256": request["request_sha256"], "status": "UNCERTAIN"}
    verdict = module.build_cpa_read_aloud_verifier(
        _bound_cpa_call(tmp_path, calls), next_verifier=witness)(_request())
    # The drifted bytes are neither served nor is the same prompt re-bought; the
    # existing provider-failure route (witness, then closed choice) takes over.
    assert calls.count(calls[0]) == 1 and len(audio) == 1
    assert verdict is None or verdict.get("reason_code") != "READ_ALOUD_REJECTED_BY_CONTEXT"


# ------------- C legacy forward: pronoun discovery keeps its complete reply

def _bound(llm, tmp_path, models=("gpt-fixture",)):
    llm.cpa_cache_identity = {"transport": "cpa_command", "models": list(models), "effort": "low"}
    llm.provider_runtime_binding = {"provider_transport": "command",
                                    "provider_credential_source": "FIXTURE_BOUND"}
    llm.completed_review_root = tmp_path / "completed-reviews"
    return llm


def test_pronoun_discovery_successor_reuses_the_interrupted_runs_complete_reply(tmp_path):
    """R2 #4 class: an interruption after a complete pronoun reply no longer leaves
    only a raw exchange; the successor's identical discovery consumes it (0 calls)."""
    from tests.test_pronoun_referent_consistency import Scripted, d, run, srt
    source = srt("她来了")
    first = _bound(Scripted([d("cue-1-occurrence-1", "她", "她", "HOST")]), tmp_path)
    findings, audit = run(source, first)
    successor = _bound(Scripted(), tmp_path)  # any dispatch would pop from an empty script
    again, replay = run(source, successor)
    assert len(first.prompts) == 1 and successor.prompts == []
    assert (again, replay["completion_sha256"], replay["decisions"]) == (
        findings, audit["completion_sha256"], audit["decisions"])
    other = _bound(Scripted([d("cue-1-occurrence-1", "她", "她", "HOST")]), tmp_path, models=("gpt-other",))
    run(source, other)
    assert len(other.prompts) == 1  # another provider binding is another question


def test_pronoun_discovery_bad_raw_is_retained_without_new_vote_or_authority(tmp_path):
    from src.autoslice import pronoun_consistency as native
    from src.autoslice.llm_client import extract_json_object
    from tests.test_pronoun_referent_consistency import CONTEXT, POLICY, Scripted, d, run, srt
    source = srt("她来了")
    first = _bound(Scripted("not json", [d("cue-1-occurrence-1", "她", "她", "HOST")]), tmp_path)
    for _ in range(2):
        with pytest.raises(native.CandidatePronounAuditError) as error:
            native.discover_candidate_pronoun_findings(
                source, policy_text=POLICY, candidate_context_text=CONTEXT, llm_call=first,
                extract_json=extract_json_object)
        assert error.value.failure_layer == 'CONTRACT_ERROR'
    assert len(first.prompts) == 1
    saved = list((tmp_path / 'completed-reviews').rglob('*.json'))
    assert len(saved) == 1 and json.loads(saved[0].read_text())['response'] == 'not json'
    plain = Scripted([d("cue-1-occurrence-1", "她", "她", "HOST")])
    run(source, plain)
    assert len(plain.prompts) == 1  # unbound callers dispatch as before


def test_k21_read_aloud_unbuildable_checkpoint_binding_dispatches_as_before(tmp_path, monkeypatch):
    from src.autoslice import completed_review_checkpoint as crc
    from src.autoslice import read_aloud_llm_verifier as module
    from tests.test_read_aloud_llm_verifier import _request

    def broken(*_a, **_k):
        raise TypeError("binding not serializable")
    monkeypatch.setattr(crc.CompletedReviewCheckpoint, "for_call", classmethod(lambda cls, **k: broken()))
    calls = []
    verdict = module.build_cpa_read_aloud_verifier(_bound_cpa_call(tmp_path, calls))(_request())
    assert len(calls) == 1 and verdict["reason_code"] == "READ_ALOUD_REJECTED_BY_CONTEXT"
