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


def _judge(choice):
    return lambda _prompt: json.dumps({"choice": choice, "reason": "test"})


def _finding():
    return {
        "cue_index": 2,
        "suspect": "秒",
        "suggestion": "首",
        "proposed_full_cue": "一百五十首",
        "repair_class": "phonetic",
    }


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
    second_request = build_context_adjudication_request(source, findings[1])
    second_witness_request = build_witness_request(second_request)
    second_witness = {
        **_witness(second_witness_request, "yi bai wu shi miao"),
        "witness_protocol": "blind_pinyin",
        "served_from_cache": True,
    }
    assert (
        judge_word_choice(
            llm_call=_judge("CURRENT"),
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
        return json.dumps({"choice": "CURRENT", "reason": "fresh row"})

    unresolved, resolved = adjudicate_exact_release_findings(
        source,
        findings,
        entity_verifier=verifier,
        judge_llm_call=judge_provider,
    )

    assert unresolved == []
    assert len(resolved) == 2
    assert len(verifier.provider_calls) == 1
    assert len(judge_calls) == 1
    replay = resolved[1]["exact_release_adjudication"]["provider_budget_replay"]
    assert replay["status"] == "PASS"
    assert replay["provider_call_count"] == 0
    assert replay["witness_cache_hit"] is True
    assert replay["judge_cache_hit"] is True


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
        request = build_context_adjudication_request(source, finding)
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
        return json.dumps({"choice": "CURRENT", "reason": "fresh row"})

    unresolved, resolved = adjudicate_exact_release_findings(
        source,
        findings,
        entity_verifier=verifier,
        judge_llm_call=judge_provider,
    )

    assert unresolved == []
    assert len(resolved) == 16
    assert len(verifier.provider_calls) == 4
    assert len(judge_calls) == 4
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
    old_request = build_context_adjudication_request(old_source, old_finding)
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
    )

    assert resolved == []
    assert unresolved[0]["exact_release_adjudication"]["status"] == ("SKIPPED_BUDGET")


@pytest.mark.parametrize("missing_cache", ("witness", "judge"))
def test_exhausted_budget_never_calls_on_partial_cache_hit(tmp_path, monkeypatch, missing_cache):
    import src.autoslice.final_review_auditor as auditor

    monkeypatch.setattr(auditor, "MAX_CONTEXT_ADJUDICATIONS", 0)
    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path))
    source = _srt("一百五十秒")
    finding = {**_finding(), "cue_index": 1}
    request = build_context_adjudication_request(source, finding)
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
    )

    assert resolved == []
    assert unresolved[0]["exact_release_adjudication"]["status"] == ("SKIPPED_BUDGET")
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
        source, finding, clip_context=clip_context
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
        llm_call=_judge("NEITHER"), check_request=initial, witness=witness
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
            source, rebuilt, clip_context=clip_context
        )
        assert judge_word_choice(
            llm_call=_judge("PROPOSED"),
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
        judge_llm_call=lambda _prompt: (_ for _ in ()).throw(
            AssertionError("budget-exhausted CPA provider ran")
        ),
    )

    assert resolved == [] and len(unresolved) == 1
    adjudication = unresolved[0]["exact_release_adjudication"]
    assert adjudication["repaired"] is True
    replay = adjudication["provider_budget_replay"]
    assert replay["provider_call_count"] == 0
    assert replay["witness_cache_hit"] is True
    assert replay["exact_source_transcript_cache_hit"] is True
    assert replay["judge_cache_hit"] is True


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
        judge_llm_call=lambda _prompt: (_ for _ in ()).throw(
            AssertionError("budget-exhausted CPA provider ran")
        ),
    )
    assert resolved == []
    assert unresolved[0]["exact_release_adjudication"]["status"] == "SKIPPED_BUDGET"
