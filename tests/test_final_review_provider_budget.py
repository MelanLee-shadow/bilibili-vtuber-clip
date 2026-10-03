import json

import pytest

from src.autoslice.acoustic_witness_adjudication import (
    build_witness_request,
    judge_word_choice,
)
from src.autoslice.final_review_auditor import (
    adjudicate_exact_release_findings,
    build_context_adjudication_request,
)
from src.autoslice.final_review_provider_budget import (
    ContextAdjudicationBudget,
    with_cpa_resource_pressure,
)


def _srt(*texts: str) -> str:
    blocks = []
    for index, text in enumerate(texts, start=1):
        blocks.append(
            f"{index}\n00:00:{index * 5:02d},000 --> 00:00:{index * 5 + 4:02d},000\n{text}"
        )
    return "\n\n".join(blocks) + "\n"


def _witness(request, heard):
    return {
        "schema_version": "subtitle-span-acoustic-witness.v1",
        "request_sha256": request["request_sha256"],
        "status": "OBSERVED",
        "target_audible": True,
        "heard_pinyin": heard,
        "uncertain_positions": [],
        "syllable_count": len(heard.split()),
        "confidence": 0.9,
        "reason": "test witness",
    }


def _identified(call):
    call.cpa_cache_identity = {"models": ["gpt-6-sol"], "effort": "medium"}
    return call


def _judge(choice, *, count=0, target=12):
    payload = {"choice": choice, "reason": "test"}
    if choice == "CURRENT":
        payload.update(
            current_utterance_supported=True,
            current_utterance_support_reason="Bound whole utterance is audible and coherent",
        )
    return with_cpa_resource_pressure(
        _identified(lambda _prompt: json.dumps(payload)), count, target,
    )


def _finding():
    return {
        "cue_index": 2,
        "suspect": "秒",
        "suggestion": "首",
        "proposed_full_cue": "一百五十首",
        "repair_class": "phonetic",
        "why": "source-bound test finding",
        "candidate_provenance": {
            "source_media_sha256": "sha256:" + "a" * 64,
            "source_window_ms": [5_000, 9_000],
        },
    }


def _budget_context(calls):
    def adjudicate_context(srt_text, finding, **_kwargs):
        calls.append((srt_text, dict(finding)))
        return "", {
            "schema_version": "subtitle-span-adjudication.v1",
            "status": "OBSERVED",
            "repaired": False,
            "decision_authority": "CPA_JUDGE",
        }

    return adjudicate_context


@pytest.mark.parametrize("effort, hit", [("medium", True), ("low", False)])
def test_budget_cache_replay_binds_actual_model_effort(tmp_path, monkeypatch, effort, hit):
    import src.autoslice.final_review_auditor as auditor

    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path))
    monkeypatch.setattr(auditor, "MAX_CONTEXT_ADJUDICATIONS", 0)
    source = _srt("一百五十秒")
    finding = {**_finding(), "cue_index": 1}
    request = build_context_adjudication_request(
        source, finding, require_complete_utterance_support=True
    )
    wr = build_witness_request(request)
    witness = {**_witness(wr, "yi bai wu shi miao"),
               "witness_protocol": "blind_pinyin", "served_from_cache": True}
    judge_word_choice(llm_call=_judge("CURRENT", count=1, target=0),
                      check_request=request, witness=witness)

    class Verifier:
        def __call__(self, _request):
            raise AssertionError("no fresh audio after cap")

        def probe_witness_cache(self, _request):
            return witness

    def forbidden(_prompt):
        raise AssertionError("no fresh CPA after cap")

    forbidden.cpa_cache_identity = {"models": ["gpt-6-sol"], "effort": effort}
    unresolved, resolved = adjudicate_exact_release_findings(
        source,
        [finding],
        entity_verifier=Verifier(),
        judge_llm_call=forbidden,
        provider_calls_allowed=False,
    )
    assert bool(resolved) is hit
    if not hit:
        assert (
            unresolved[0]["exact_release_adjudication"]["status"]
            == "SKIPPED_PROVIDER_DISABLED"
        )


def test_exact_release_budget_counts_only_new_provider_adjudications(tmp_path, monkeypatch):
    """A strict witness+judge double hit may replay after the call cap."""

    import src.autoslice.final_review_auditor as auditor

    monkeypatch.setattr(auditor, "MAX_CONTEXT_ADJUDICATIONS", 1)
    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path))
    source = _srt("原句甲", "一百五十秒")
    findings = [
        {
            "cue_index": 1,
            "suspect": "原",
            "suggestion": "改",
            "proposed_full_cue": "改句甲",
            "repair_class": "phonetic",
        },
        _finding(),
    ]
    second_request = build_context_adjudication_request(
        source, findings[1], require_complete_utterance_support=True
    )
    second_witness_request = build_witness_request(second_request)
    second_witness = {
        **_witness(second_witness_request, "yi bai wu shi miao"),
        "witness_protocol": "blind_pinyin",
        "served_from_cache": True,
    }
    assert (
        judge_word_choice(
            llm_call=_judge("CURRENT", count=2, target=1),
            check_request=second_request,
            witness=second_witness,
        )["status"]
        == "JUDGED"
    )

    class CacheAwareVerifier:
        def __init__(self):
            self.provider_calls = []

        def __call__(self, request):
            self.provider_calls.append(request)
            return {
                **_witness(request, "yuan ju jia"),
                "witness_protocol": "blind_pinyin",
            }

        def probe_witness_cache(self, request):
            if request["cue_indexes"] != [2]:
                return None
            return {
                **_witness(request, "yi bai wu shi miao"),
                "witness_protocol": "blind_pinyin",
                "served_from_cache": True,
            }

    verifier = CacheAwareVerifier()
    judge_calls = []

    def judge_provider(prompt):
        judge_calls.append(prompt)
        return json.dumps({"choice": "CURRENT", "reason": "fresh row",
                           "current_utterance_supported": True,
                           "current_utterance_support_reason": "Bound whole utterance is supported"})

    unresolved, resolved = adjudicate_exact_release_findings(
        source,
        findings,
        entity_verifier=verifier,
        judge_llm_call=_identified(judge_provider),
    )

    assert unresolved == []
    assert len(resolved) == 2
    assert len(verifier.provider_calls) == 1
    assert len(judge_calls) == 2  # one text-first decision plus one acoustic decision
    replay = resolved[1]["exact_release_adjudication"]["provider_budget_replay"]
    assert replay["status"] == "PASS"
    assert replay["provider_call_count"] == 0
    assert replay["witness_cache_hit"] is True
    assert replay["judge_cache_hit"] is True
    pressure = resolved[1]["exact_release_adjudication"]["cpa_resource_pressure"]
    assert pressure["provider_calls_this_decision"] == 0


def test_second_pass_replays_twelve_cached_rows_then_adjudicates_remaining_four(
    tmp_path, monkeypatch
):
    """The retry keeps cap=12 while cached rows consume no fresh budget."""

    import src.autoslice.final_review_auditor as auditor

    monkeypatch.setattr(auditor, "MAX_CONTEXT_ADJUDICATIONS", 12)
    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path))
    source = _srt(*(["一百五十秒"] * 16))
    findings = [
        {
            **_finding(),
            "cue_index": cue_index,
        }
        for cue_index in range(1, 17)
    ]
    cached_witnesses = {}
    for finding in findings[:12]:
        request = build_context_adjudication_request(
        source, finding, require_complete_utterance_support=True
    )
        witness_request = build_witness_request(request)
        witness = {
            **_witness(witness_request, "yi bai wu shi miao"),
            "witness_protocol": "blind_pinyin",
            "served_from_cache": True,
        }
        cached_witnesses[tuple(witness_request["cue_indexes"])] = witness
        assert (
            judge_word_choice(
                llm_call=_judge("CURRENT"),
                check_request=request,
                witness=witness,
            )["status"]
            == "JUDGED"
        )

    class TwelveHitVerifier:
        def __init__(self):
            self.provider_calls = []

        def __call__(self, request):
            self.provider_calls.append(request)
            return {
                **_witness(request, "yi bai wu shi miao"),
                "witness_protocol": "blind_pinyin",
            }

        def probe_witness_cache(self, request):
            return cached_witnesses.get(tuple(request["cue_indexes"]))

    verifier = TwelveHitVerifier()
    judge_calls = []

    def judge_provider(prompt):
        judge_calls.append(prompt)
        return json.dumps({"choice": "CURRENT", "reason": "fresh row",
                           "current_utterance_supported": True,
                           "current_utterance_support_reason": "Bound whole utterance is supported"})

    unresolved, resolved = adjudicate_exact_release_findings(
        source,
        findings,
        entity_verifier=verifier,
        judge_llm_call=_identified(judge_provider),
    )

    assert unresolved == []
    assert len(resolved) == 16
    assert len(verifier.provider_calls) == 4
    assert len(judge_calls) == 8  # text-first and acoustic decisions for four new rows
    assert all(
        row["exact_release_adjudication"]["provider_budget_replay"]
        ["provider_call_count"]
        == 0
        for row in resolved[:12]
    )
    assert all(
        "provider_budget_replay" not in row["exact_release_adjudication"]
        for row in resolved[12:]
    )


@pytest.mark.parametrize("drift", ("base", "proposed", "time"))
def test_exact_release_cache_replay_rejects_current_input_drift(tmp_path, monkeypatch, drift):
    import src.autoslice.final_review_auditor as auditor

    monkeypatch.setattr(auditor, "MAX_CONTEXT_ADJUDICATIONS", 0)
    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path))
    old_source = _srt("一百五十秒")
    old_finding = {**_finding(), "cue_index": 1}
    old_request = build_context_adjudication_request(
        old_source, old_finding, require_complete_utterance_support=True
    )
    old_witness_request = build_witness_request(old_request)
    old_witness = {
        **_witness(old_witness_request, "yi bai wu shi miao"),
        "witness_protocol": "blind_pinyin",
        "served_from_cache": True,
    }
    assert (
        judge_word_choice(
            llm_call=_judge("CURRENT"),
            check_request=old_request,
            witness=old_witness,
        )["status"]
        == "JUDGED"
    )

    source = old_source
    finding = dict(old_finding)
    if drift == "base":
        source = _srt("真的一百五十秒")
        finding["proposed_full_cue"] = "真的一百五十首"
    elif drift == "proposed":
        finding.update(suggestion="手", proposed_full_cue="一百五十手")
    else:
        source = old_source.replace(
            "00:00:05,000 --> 00:00:09,000",
            "00:00:06,000 --> 00:00:10,000",
        )

    class ProbeOnlyVerifier:
        def __call__(self, _request):
            raise AssertionError("budget-exhausted provider path ran")

        def probe_witness_cache(self, request):
            return {
                **_witness(request, "yi bai wu shi miao"),
                "witness_protocol": "blind_pinyin",
                "served_from_cache": True,
            }

    unresolved, resolved = adjudicate_exact_release_findings(
        source,
        [finding],
        entity_verifier=ProbeOnlyVerifier(),
        judge_llm_call=lambda _prompt: (_ for _ in ()).throw(
            AssertionError("budget-exhausted CPA path ran")
        ),
        provider_calls_allowed=False,
    )

    assert resolved == []
    assert (
        unresolved[0]["exact_release_adjudication"]["status"]
        == "SKIPPED_PROVIDER_DISABLED"
    )


@pytest.mark.parametrize("missing_cache", ("witness", "judge"))
def test_exhausted_budget_never_calls_on_partial_cache_hit(tmp_path, monkeypatch, missing_cache):
    import src.autoslice.final_review_auditor as auditor

    monkeypatch.setattr(auditor, "MAX_CONTEXT_ADJUDICATIONS", 0)
    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path))
    source = _srt("一百五十秒")
    finding = {**_finding(), "cue_index": 1}
    request = build_context_adjudication_request(
        source, finding, require_complete_utterance_support=True
    )
    witness_request = build_witness_request(request)
    witness = {
        **_witness(witness_request, "yi bai wu shi miao"),
        "witness_protocol": "blind_pinyin",
        "served_from_cache": True,
    }
    if missing_cache == "witness":
        assert (
            judge_word_choice(
                llm_call=_judge("CURRENT"),
                check_request=request,
                witness=witness,
            )["status"]
            == "JUDGED"
        )

    class PartialCacheVerifier:
        def __init__(self):
            self.provider_calls = 0

        def __call__(self, _request):
            self.provider_calls += 1
            raise AssertionError("budget-exhausted witness provider ran")

        def probe_witness_cache(self, current_request):
            if missing_cache == "witness":
                return None
            return {
                **_witness(current_request, "yi bai wu shi miao"),
                "witness_protocol": "blind_pinyin",
                "served_from_cache": True,
            }

    verifier = PartialCacheVerifier()
    judge_calls = []
    unresolved, resolved = adjudicate_exact_release_findings(
        source,
        [finding],
        entity_verifier=verifier,
        judge_llm_call=lambda _prompt: judge_calls.append(True),
        provider_calls_allowed=False,
    )

    assert resolved == []
    assert (
        unresolved[0]["exact_release_adjudication"]["status"]
        == "SKIPPED_PROVIDER_DISABLED"
    )
    assert verifier.provider_calls == 0
    assert judge_calls == []


def _prepare_exact_triple_cache(tmp_path, *, store_second_judge=True):
    from src.autoslice.acoustic_witness_adjudication import build_witness_request
    from src.autoslice.exact_source_transcript_contract import (
        _canonical_sha256,
        build_exact_source_transcript_request,
        seal_exact_source_transcript_observation,
    )
    from src.autoslice.exact_source_transcript_provider import (
        rebuild_candidate_from_exact_source_transcript,
    )
    from src.autoslice.final_review_auditor import _derive_single_span_edit

    source = _srt("难听难听，对吧")
    finding = {
        "cue_index": 1,
        "suspect": "难听难听",
        "suggestion": "南町nightin",
        "proposed_full_cue": "南町nightin，对吧",
        "repair_class": "phonetic",
    }
    clip_context = {
        "schema_version": "clip-context.v1",
        "candidate_id": "auto_213135_806_1068",
        "context_sha256": "sha256:" + "d" * 64,
        "whole_clip_draft_srt_sha256": "sha256:" + "e" * 64,
    }
    initial = build_context_adjudication_request(
        source, finding, clip_context=clip_context,
        require_complete_utterance_support=True,
    )
    witness_request = build_witness_request(initial)
    timeline = {
        "schema_version": "subtitle-audio-timeline-binding.v1",
        "source_media_timeline_offset_ms": 0,
        "delivery_local": {
            "target_start_ms": 5_000,
            "target_end_ms": 9_000,
            "context_start_ms": 4_500,
            "context_end_ms": 9_500,
        },
        "source_media": {
            "target_start_ms": 5_000,
            "target_end_ms": 9_000,
            "crop_start_ms": 4_600,
            "crop_end_ms": 9_400,
        },
    }
    witness = {
        **_witness(witness_request, "yao jiu jiu tian dui ba"),
        "witness_protocol": "blind_pinyin",
        "served_from_cache": True,
        "source_media_sha256": "a" * 64,
        "audio_clip_sha256": "b" * 64,
        "prompt_sha256": "1" * 64,
        "response_sha256": "2" * 64,
        "audio_start_ms": 4_600,
        "audio_end_ms": 9_400,
        "timeline_binding": timeline,
    }
    assert judge_word_choice(
        llm_call=_judge("NEITHER", count=1, target=0),
        check_request=initial, witness=witness
    )["status"] == "JUDGED"
    physical = build_exact_source_transcript_request(witness_request)
    observation = seal_exact_source_transcript_observation(
        request=physical,
        exact_transcript="nineteen nineteen，对吧",
        audible_language="mixed",
        source_media_sha256="a" * 64,
        audio_clip_sha256="b" * 64,
        provider="agy",
        model="Gemini 3.6 Flash (High)",
        response_sha256="c" * 64,
        timeline_binding=timeline,
    )
    cached_observation = dict(observation)
    cached_observation["served_from_cache"] = True
    cached_observation.pop("observation_sha256")
    cached_observation["observation_sha256"] = _canonical_sha256(
        cached_observation
    )
    rebuilt, _handoff = rebuild_candidate_from_exact_source_transcript(
        finding=finding,
        initial_check_request=initial,
        witness_request=witness_request,
        witness=witness,
        srt_text=source,
        clip_context=clip_context,
        provider=lambda _request: cached_observation,
        derive_single_span_edit=_derive_single_span_edit,
    )
    assert rebuilt is not None
    if store_second_judge:
        rebuilt_request = build_context_adjudication_request(
            source, rebuilt, clip_context=clip_context,
            require_complete_utterance_support=True,
        )
        assert judge_word_choice(
            llm_call=_judge("PROPOSED", count=1, target=0),
            check_request=rebuilt_request,
            witness=witness,
        )["status"] == "JUDGED"
    return source, finding, clip_context, witness, cached_observation


def test_exhausted_budget_replays_exact_source_triple_cache(tmp_path, monkeypatch):
    import src.autoslice.final_review_auditor as auditor

    monkeypatch.setattr(auditor, "MAX_CONTEXT_ADJUDICATIONS", 0)
    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path))
    source, finding, clip_context, witness, exact_observation = (
        _prepare_exact_triple_cache(tmp_path)
    )

    class TripleCacheVerifier:
        def __call__(self, _request):
            raise AssertionError("budget-exhausted witness provider ran")

        def probe_witness_cache(self, _request):
            return witness

        def exact_source_transcript(self, _request):
            raise AssertionError("budget-exhausted exact provider ran")

        def probe_exact_source_transcript_cache(self, _request):
            return exact_observation

    unresolved, resolved = adjudicate_exact_release_findings(
        source,
        [finding],
        entity_verifier=TripleCacheVerifier(),
        clip_context=clip_context,
        judge_llm_call=_identified(lambda _prompt: (_ for _ in ()).throw(
            AssertionError("budget-exhausted CPA provider ran")
        )),
    )

    assert resolved == [] and len(unresolved) == 1
    adjudication = unresolved[0]["exact_release_adjudication"]
    assert adjudication["repaired"] is True
    replay = adjudication["provider_budget_replay"]
    assert replay["provider_call_count"] == 0
    assert replay["witness_cache_hit"] is True
    assert replay["exact_source_transcript_cache_hit"] is True
    assert replay["judge_cache_hit"] is True
    assert adjudication["cpa_resource_pressure"]["provider_calls_this_decision"] == 0


@pytest.mark.parametrize("missing", ("exact", "second_judge"))
def test_exact_triple_cache_partial_hit_never_crosses_exhausted_budget(
    tmp_path, monkeypatch, missing
):
    import src.autoslice.final_review_auditor as auditor

    monkeypatch.setattr(auditor, "MAX_CONTEXT_ADJUDICATIONS", 0)
    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path))
    source, finding, clip_context, witness, exact_observation = (
        _prepare_exact_triple_cache(
            tmp_path, store_second_judge=missing != "second_judge"
        )
    )

    class PartialTripleVerifier:
        def __call__(self, _request):
            raise AssertionError("budget-exhausted witness provider ran")

        def probe_witness_cache(self, _request):
            return witness

        def exact_source_transcript(self, _request):
            raise AssertionError("budget-exhausted exact provider ran")

        def probe_exact_source_transcript_cache(self, _request):
            return None if missing == "exact" else exact_observation

    unresolved, resolved = adjudicate_exact_release_findings(
        source,
        [finding],
        entity_verifier=PartialTripleVerifier(),
        clip_context=clip_context,
        judge_llm_call=_identified(lambda _prompt: (_ for _ in ()).throw(
            AssertionError("budget-exhausted CPA provider ran")
        )),
        provider_calls_allowed=False,
    )
    assert resolved == []
    assert (
        unresolved[0]["exact_release_adjudication"]["status"]
        == "SKIPPED_PROVIDER_DISABLED"
    )


def test_soft_threshold_does_not_drop_new_findings_after_twelve():
    calls = []
    budget = ContextAdjudicationBudget(
        limit=12,
        entity_verifier=None,
        clip_context={"source_media_sha256": "sha256:" + "b" * 64},
        source_media_timeline_offset_ms=0,
        judge_llm_call=None,
        screen_read_probe=None,
        adjudicate_context=_budget_context(calls),
    )

    results = [
        budget.adjudicate(
            _srt(f"原句{index}"),
            {
                **_finding(),
                "cue_index": index,
                "why": f"new finding {index}",
            },
        )
        for index in range(1, 17)
    ]

    assert len(calls) == 16
    assert budget.provider_adjudication_count == 16
    assert all(result["status"] == "OBSERVED" for result in results)
    assert results[11]["cpa_resource_pressure"] == {
        "provider_adjudication_count": 12,
        "soft_limit": 12,
        "soft_limit_exceeded": False,
        "provider_calls_this_decision": 1,
        "reason_code": "WITHIN_SOFT_LIMIT",
    }
    assert results[12]["cpa_resource_pressure"] == {
        "provider_adjudication_count": 13,
        "soft_limit": 12,
        "soft_limit_exceeded": True,
        "provider_calls_this_decision": 1,
        "reason_code": "SOFT_LIMIT_EXCEEDED_CONTINUED",
    }


def test_initial_cumulative_count_is_preserved_and_new_finding_is_adjudicated():
    calls = []
    budget = ContextAdjudicationBudget(
        limit=12,
        initial_provider_adjudication_count=12,
        entity_verifier=None,
        clip_context=None,
        source_media_timeline_offset_ms=0,
        judge_llm_call=None,
        screen_read_probe=None,
        adjudicate_context=_budget_context(calls),
    )

    result = budget.adjudicate(_srt("新疑点"), _finding())

    assert len(calls) == 1
    assert budget.provider_adjudication_count == 13
    assert result["status"] == "OBSERVED"
    assert result["cpa_resource_pressure"]["provider_adjudication_count"] == 13
    assert result["cpa_resource_pressure"]["soft_limit_exceeded"] is True


def test_same_input_replays_original_result_without_repeating_provider():
    calls = []
    budget = ContextAdjudicationBudget(
        limit=12,
        entity_verifier=None,
        clip_context={"source_media_sha256": "sha256:" + "c" * 64},
        source_media_timeline_offset_ms=0,
        judge_llm_call=None,
        screen_read_probe=None,
        adjudicate_context=lambda *_args, **_kwargs: (
            calls.append(True)
            or "",
            {
                "schema_version": "subtitle-span-adjudication.v1",
                "status": "UNCERTAIN",
                "repaired": False,
            },
        ),
    )
    finding = _finding()
    first = budget.adjudicate(_srt("一百五十秒"), finding)
    repeated = budget.adjudicate(
        _srt("一百五十秒"),
        {
            **finding,
            "_transient_attempt": 2,
            "exact_release_adjudication": {"status": "OLD_AUDIT"},
            "cpa_resource_pressure": {"provider_adjudication_count": 999},
        },
    )

    assert calls == [True]
    assert first["status"] == repeated["status"] == "UNCERTAIN"
    assert repeated["repaired"] is False
    assert repeated["same_input_replay"]["provider_call_count"] == 0
    assert repeated["cpa_resource_pressure"]["provider_calls_this_decision"] == 0


def test_changed_source_proposed_text_or_timeline_gets_fresh_admission():
    calls = []
    budget = ContextAdjudicationBudget(
        limit=12,
        entity_verifier=None,
        clip_context={"source_media_sha256": "sha256:" + "d" * 64},
        source_media_timeline_offset_ms=0,
        judge_llm_call=None,
        screen_read_probe=None,
        adjudicate_context=_budget_context(calls),
    )
    source = _srt("一百五十秒")
    finding = _finding()
    budget.adjudicate(source, finding)
    budget.adjudicate(_srt("真的一百五十秒"), finding)
    budget.adjudicate(source, {**finding, "suggestion": "手", "proposed_full_cue": "一百五十手"})
    budget.source_media_timeline_offset_ms = 100
    budget.adjudicate(source, finding)

    assert len(calls) == 4
    assert budget.provider_adjudication_count == 4


def test_provider_disabled_cache_miss_never_calls_provider_and_stays_unresolved():
    calls = []
    budget = ContextAdjudicationBudget(
        limit=12,
        provider_calls_allowed=False,
        entity_verifier=None,
        clip_context=None,
        source_media_timeline_offset_ms=0,
        judge_llm_call=None,
        screen_read_probe=None,
        adjudicate_context=lambda *_args, **_kwargs: (
            calls.append(True)
            or "",
            {"status": "OBSERVED", "repaired": True},
        ),
    )

    first = budget.adjudicate(_srt("离线"), _finding())
    repeated = budget.adjudicate(_srt("离线"), _finding())

    assert calls == []
    assert budget.provider_adjudication_count == 0
    assert first["status"] == repeated["status"] == "SKIPPED_PROVIDER_DISABLED"
    assert first["repaired"] is repeated["repaired"] is False
    assert repeated["same_input_replay"]["original_status"] == (
        "SKIPPED_PROVIDER_DISABLED"
    )
    assert repeated["cpa_resource_pressure"]["reason_code"] == "SAME_INPUT_REPLAY"


@pytest.mark.parametrize(
    "kwargs",
    ({"limit": -1}, {"limit": 0, "initial_provider_adjudication_count": -1}),
)
def test_budget_rejects_negative_counters(kwargs):
    with pytest.raises(ValueError):
        ContextAdjudicationBudget(
            entity_verifier=None,
            clip_context=None,
            source_media_timeline_offset_ms=0,
            judge_llm_call=None,
            screen_read_probe=None,
            adjudicate_context=_budget_context([]),
            **kwargs,
        )


def test_soft_pressure_reaches_cpa_text_first_without_an_audio_provider():
    source = _srt("这首歌还欠着呢", "还没有歌杂呢", "之后再唱")
    prompts = []

    def judge(prompt):
        prompts.append(prompt)
        assert "TEXT_FIRST" in prompt
        assert "单片软资源压力" in prompt
        assert '\"pressure_multiple\": 1' in prompt
        return json.dumps({"choice": "PROPOSED", "needs_audio": False,
                           "reason": "语境支持歌债"}, ensure_ascii=False)

    unresolved, resolved = adjudicate_exact_release_findings(
        source, [{"cue_index": 2, "suspect": "歌杂", "suggestion": "歌债",
                  "proposed_full_cue": "还没有歌债呢", "repair_class": "phonetic"}],
        entity_verifier=None, judge_llm_call=_identified(judge),
        initial_provider_adjudication_count=12,
    )
    assert len(prompts) == 1 and not resolved
    decision = unresolved[0]["exact_release_adjudication"]
    assert decision["repaired"] is True
    assert decision["mutation_authority"]["status"] == "PASS"
    assert decision["cpa_resource_pressure"]["provider_adjudication_count"] == 13


def test_new_prior_acoustic_evidence_is_not_treated_as_unchanged_input():
    calls = []
    budget = ContextAdjudicationBudget(
        limit=12, entity_verifier=None, clip_context=None,
        source_media_timeline_offset_ms=0, judge_llm_call=None,
        screen_read_probe=None, adjudicate_context=_budget_context(calls),
    )
    source = _srt("一百五十秒")
    budget.adjudicate(source, _finding())
    budget.adjudicate(source, {**_finding(), "_prior_acoustic_observations": [
        {"request": {"request_sha256": "a" * 64}, "verdict": {"heard_pinyin": "yi bai wu shi miao"}}
    ]})
    assert len(calls) == 2


def test_soft_pressure_cached_judge_survives_counter_only_resume(tmp_path, monkeypatch):
    from src.autoslice.final_review_auditor import adjudicate_context_finding

    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path))
    source = _srt("一百五十秒")
    finding = {**_finding(), "cue_index": 1}
    calls = []

    def judge(prompt):
        calls.append(prompt)
        return json.dumps({"choice": "CURRENT", "needs_audio": True,
                           "reason": "bound original audio supports seconds"})

    class CachedWitness:
        def __call__(self, request):
            return self.probe_witness_cache(request)

        def probe_witness_cache(self, request):
            return {**_witness(request, "yi bai wu shi miao"),
                    "witness_protocol": "blind_pinyin", "served_from_cache": True}

    def budget(prior):
        return ContextAdjudicationBudget(
            limit=12, initial_provider_adjudication_count=prior,
            entity_verifier=CachedWitness(), clip_context=None,
            source_media_timeline_offset_ms=0, judge_llm_call=_identified(judge),
            screen_read_probe=None, adjudicate_context=adjudicate_context_finding,
        )

    first = budget(12).adjudicate(source, finding)
    assert first["policy_branch"] == "JUDGE_KEEPS_CURRENT"
    assert calls
    before_resume = len(calls)
    resumed = budget(13).adjudicate(source, finding)
    assert len(calls) == before_resume
    assert resumed["cpa_resource_pressure"]["provider_calls_this_decision"] == 0
    assert resumed["cpa_resource_pressure"]["provider_adjudication_count"] == 13


def test_soft_pressure_grows_without_changing_every_counter():
    from src.autoslice.final_review_provider_budget import with_cpa_resource_pressure

    call = _identified(lambda _prompt: "")
    pressures = [with_cpa_resource_pressure(call, count, 12).cpa_resource_pressure
                 for count in (13, 14, 24, 48, 96, 200)]
    assert pressures[0] == pressures[1]
    assert [row["pressure_multiple"] for row in pressures] == [1, 1, 2, 4, 8, 8]
