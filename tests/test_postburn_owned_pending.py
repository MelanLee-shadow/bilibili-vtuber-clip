import hashlib
import pytest

from src.autoslice import producer_package_finalization as finalization
from src.autoslice.chat_authority_ownership import (
    chat_authority_owned_cue_indexes,
    chat_authority_owned_cue_intervals,
    suppress_chat_authority_owned_self_heal_findings,
)
from src.autoslice.final_review_auditor import build_context_adjudication_request
from src.autoslice.retained_json_graph import JsonValue


_SRT = "1\n00:00:00,250 --> 00:00:02,850\n主包还没忘记nus之梦\n"
_CURRENT = "主包还没忘记nus之梦"
_PROPOSED = "妈妈主播还没忘记nus之梦"
_BASE_SHA = hashlib.sha256(_CURRENT.encode("utf-8")).hexdigest()


def _postburn_finding() -> dict[str, JsonValue]:
    return {
        "cue_index": 1,
        "base_text_sha256": _BASE_SHA,
        "kind": "context",
        "suspect": "主包",
        "suggestion": "妈妈主播",
        "proposed_full_cue": _PROPOSED,
        "repair_class": "phonetic",
        "candidate_provenance": {
            "kind": "subtitle_audio_witness_disagreement",
            "nomination_id": "saved-postburn-nomination",
            "mutation_authorized": False,
            "evidence_source": "witness",
            "current_srt_sha256": "sha256:" + hashlib.sha256(_SRT.encode()).hexdigest(),
        },
    }


def _ordinary_finding() -> dict[str, JsonValue]:
    return {
        "cue_index": 1,
        "base_text_sha256": _BASE_SHA,
        "kind": "context",
        "suspect": "主包",
        "suggestion": "主播",
        "proposed_full_cue": "主播还没忘记nus之梦",
        "repair_class": "phonetic",
    }


def _authority() -> dict[str, JsonValue]:
    return {
        "applied": [
            {
                "mode": "exact_span",
                "matched_start_ms": 9_730,
                "matched_end_ms": 12_330,
            }
        ]
    }


def test_postburn_witness_row_is_pending_but_ordinary_owned_row_is_suppressed():
    authority = _authority()
    assert chat_authority_owned_cue_intervals(
        _SRT, authority, delivery_start_ms=9_480
    ) == {1: (250, 2_850)}
    assert chat_authority_owned_cue_indexes(
        _SRT, authority, delivery_start_ms=9_480
    ) == frozenset({1})

    postburn = _postburn_finding()
    ordinary = _ordinary_finding()
    audit = {
        "status": "FLAGGED",
        "release_gate": "BLOCK",
        "reason_codes": ["POSTBURN_SUBTITLE_AUDIO_CPA_FINDINGS"],
        "findings": [postburn, ordinary],
        "validated_finding_count": 2,
    }

    receipts = suppress_chat_authority_owned_self_heal_findings(
        _SRT, audit, authority, 9_480
    )

    assert audit["findings"] == [postburn]
    assert audit["validated_finding_count"] == 1
    assert audit["status"] == "FLAGGED"
    assert audit["release_gate"] == "BLOCK"
    assert len(receipts) == 2
    pending = next(row for row in receipts if row.get("status") == "PENDING")
    assert pending["finding_preserved"] is True
    assert pending["mutation_authorized"] is False
    assert pending["pending_consumer"] == "exact_final_terminal_funnel"
    ordinary_receipt = next(row for row in receipts if row.get("status") != "PENDING")
    assert ordinary_receipt["reason_code"] == (
        "CHAT_AUTHORITY_OWNED_CUE_SELF_HEAL_SUPPRESSED"
    )
    assert ordinary_receipt["owned_interval"] == {
        "start_ms": 250,
        "end_ms": 2_850,
    }


def test_postburn_disclosure_without_provenance_uses_bound_receipt_identity():
    disclosure = _postburn_finding()
    disclosure.update(
        proposed_full_cue=None,
        suggestion=None,
        repair_class="disclosure_only",
        candidate_provenance=None,
    )
    receipt = {
        "schema_version": "postburn-subtitle-audio-review.v1",
        "status": "FLAGGED",
        "release_gate": "BLOCK",
        "reviewed_srt_sha256": "sha256:" + hashlib.sha256(_SRT.encode()).hexdigest(),
        "findings": [disclosure],
    }
    authority = {**_authority(), "postburn_subtitle_audio_review": receipt}
    audit = {
        "status": "FLAGGED",
        "release_gate": "BLOCK",
        "findings": [dict(disclosure)],
        "validated_finding_count": 1,
    }

    receipts = suppress_chat_authority_owned_self_heal_findings(
        _SRT, audit, authority, 9_480
    )

    assert audit["findings"] == [disclosure]
    assert len(receipts) == 1
    assert receipts[0]["status"] == "PENDING"
    assert receipts[0]["finding_preserved"] is True


def test_chat_owned_protection_blocks_postburn_proposal_before_and_after_terminal():
    request = build_context_adjudication_request(
        _SRT,
        {
            "cue_index": 1,
            "suspect": "主包",
            "suggestion": "妈妈主播",
            "proposed_full_cue": _PROPOSED,
            "repair_class": "phonetic",
        },
    )
    finding = {
        **_postburn_finding(),
        "exact_release_adjudication": {
            "schema_version": "subtitle-span-adjudication.v1",
            "status": "OBSERVED",
            "decision_authority": "CPA_JUDGE",
            "repaired": True,
            "timing_immutable": True,
            "mutation_authority": {
                "schema_version": "subtitle-correction-mutation-authority.v1",
                "status": "PASS",
            },
            "request": request,
            "witness_judge": {
                "judge": {"status": "JUDGED", "choice": "PROPOSED"}
            },
        },
    }
    protected = chat_authority_owned_cue_indexes(
        _SRT, _authority(), delivery_start_ms=9_480
    )

    unprotected, repairs = finalization._apply_exact_final_cpa_repairs(
        _SRT, {"findings": [dict(finding)]}
    )
    assert _PROPOSED in unprotected
    assert len(repairs) == 1

    for _terminal_pass in range(2):
        audit = {"findings": [dict(finding)]}
        protected_text, protected_repairs = finalization._apply_exact_final_cpa_repairs(
            _SRT, audit, protected_cue_indexes=protected
        )
        assert protected_text == _SRT
        assert protected_repairs == []
        assert audit["findings"][0]["human_literal_proposal_veto"][
            "mutation_authorized"
        ] is False


@pytest.mark.parametrize("case", ["stale_provenance", "stale_receipt", "unrelated_row", "malformed_proposal"])
def test_only_current_matching_postburn_rows_are_retained(case):
    finding = _ordinary_finding()
    authority = _authority()
    if case == "stale_provenance":
        finding = _postburn_finding()
        finding["candidate_provenance"]["current_srt_sha256"] = "sha256:" + "0" * 64
    elif case in {"stale_receipt", "unrelated_row"}:
        authority["postburn_subtitle_audio_review"] = {
            "schema_version": "postburn-subtitle-audio-review.v1",
            "status": "FLAGGED", "release_gate": "BLOCK",
            "reviewed_srt_sha256": "sha256:" + (
                "0" * 64 if case == "stale_receipt" else hashlib.sha256(_SRT.encode()).hexdigest()),
            "findings": [finding if case == "stale_receipt" else _postburn_finding()],
        }
    else:
        finding["proposed_full_cue"] = ["invalid"]
    audit = {"status": "FLAGGED", "release_gate": "BLOCK", "findings": [finding],
             "validated_finding_count": 1, "reason_codes": []}
    receipts = suppress_chat_authority_owned_self_heal_findings(_SRT, audit, authority, 9_480)
    assert audit["findings"] == []
    assert len(receipts) == 1 and receipts[0].get("finding_preserved") is not True


def test_ordinary_gate_passes_same_chat_protection_before_and_after_terminal(tmp_path, monkeypatch):
    from src.autoslice import span_finding_obligations
    from src.autoslice import exact_final_terminal_funnel as funnel
    subtitle = tmp_path / "candidate.srt"
    subtitle.write_text(_SRT)
    audit = {"status": "FLAGGED", "release_gate": "BLOCK", "findings": [_postburn_finding()],
             "validated_finding_count": 1, "reason_codes": ["POSTBURN_SUBTITLE_AUDIO_CPA_FINDINGS"]}
    protected_calls, terminal_rows = [], []
    monkeypatch.setattr(span_finding_obligations, "enforce_review", lambda text, authority, audit, **kwargs: audit)
    monkeypatch.setattr(finalization, "persist_review_audit", lambda *args: None)
    monkeypatch.setattr(finalization, "collect_exact_final_convergence_memos", lambda *args: [])
    monkeypatch.setattr(funnel, "review_pass_receipt", lambda **kwargs: {})
    monkeypatch.setattr(funnel, "unresolved_review_rows", lambda audit: audit["findings"])
    def apply(text, audit, **kwargs):
        protected_calls.append(kwargs["protected_cue_indexes"])
        return text, []
    def terminal(text, audit, **kwargs):
        terminal_rows.extend(audit["findings"])
        return audit
    class BoundaryObserved(Exception):
        pass
    def validate(*args, **kwargs):
        raise BoundaryObserved
    monkeypatch.setattr(finalization, "_apply_exact_final_cpa_repairs", apply)
    monkeypatch.setattr(funnel, "terminalize_review", terminal)
    monkeypatch.setattr(finalization, "validate_final_review_release", validate)
    with pytest.raises(BoundaryObserved):
        finalization._run_exact_final_review_gate(
            cid="candidate", out_root=tmp_path, final_start=9_480, final_end=12_330,
            recut=finalization.CurrentTextReviewArtifacts(subtitle),
            chat_authority_audit=_authority(), chat_authority_path=tmp_path / "authority.json",
            run_exact_final_review=lambda *_: audit)
    assert terminal_rows == [_postburn_finding()]
    assert protected_calls == [frozenset({1}), frozenset({1})]
    assert subtitle.read_text() == _SRT
