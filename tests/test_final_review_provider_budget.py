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
