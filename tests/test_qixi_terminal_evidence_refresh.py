from __future__ import annotations

import hashlib
import json
import copy
import base64
from contextlib import nullcontext

import pytest

from scripts import refresh_qixi_terminal_evidence as refresh_cli
from src.autoslice import qixi_terminal_evidence_refresh as refresh
from src.autoslice.boundary_endpoint_binding import (
    bind_final_semantic_endpoint,
    final_delivery_review_matches_srt,
)
from src.autoslice.boundary_semantic_review import cue_grid_sha256
from src.autoslice.jingting_chunker import parse_srt_cues
from src.autoslice.final_review_auditor import resolve_verified_source_truth_findings
from src.autoslice.review_package_ass_audit import audit_review_package_ass


def _srt(*lines: str) -> str:
    return "\n\n".join(
        f"{index}\n00:00:{index:02d},000 --> 00:00:{index:02d},900\n{text}"
        for index, text in enumerate(lines, start=1)
    ) + "\n"


def _sha(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def _correction(before: str, after: str) -> dict[str, object]:
    return {
        "before_srt_sha256": _sha(before),
        "after_srt_sha256": _sha(after),
        "set_line_operations": ["1=A", "3=C", "6=F", "27=AA"],
    }


def _source_boundary() -> dict[str, object]:
    return {
        "schema_version": "talk-boundary-semantic-review.v1",
        "status": "PASS",
        "review_scope": "source_full_window",
        "candidate_id": refresh.CANDIDATE_ID,
    }


def _verified_truth() -> dict[str, object]:
    return {
        "source_subtitle_truth_audit": {
            "status": "ALREADY_SATISFIED", "failures": [], "applied": [], "satisfied": [],
        }
    }


def _correction_pass() -> dict[str, object]:
    return {"status": "APPLIED", "findings": [], "applied_count": 0}


def _provenance() -> dict[str, object]:
    body = {"schema_version": "qixi-terminal-operator-truth-provenance.v1", "status": "PASS"}
    return {**body, "provenance_sha256": refresh._canonical_sha(body)}


def _sealed_human_truth_srt() -> tuple[dict[str, object], bytes, str, str]:
    path = refresh.ROOT / refresh.HUMAN_TRUTH_AUTHORITY_PATH
    payload = path.read_bytes()
    authority = json.loads(payload)
    before = (
        refresh.ROOT
        / "assets/lidousha/sealed_subtitle_corrections/auto_123655_771_844.pipeline-diagnostic.srt"
    ).read_text(encoding="utf-8")
    pieces = before.split("\n\n")
    for row in authority["correction"]["replacements"]:
        index = int(row["cue_index"]) - 1
        pieces[index] = pieces[index].replace("\n" + str(row["before"]), "\n" + str(row["after"]))
    return authority, payload, before, "\n\n".join(pieces)


def _source_boundary_audit() -> dict[str, object]:
    review = {
        "schema_version": "talk-boundary-semantic-review.v1",
        "status": "PASS",
        "review_scope": "source_full_window",
        "candidate_id": refresh.CANDIDATE_ID,
        "next_topic_separated": True,
        "next_topic_witness_valid": True,
        "request_sha256": "sha256:" + "a" * 64,
        "cue_grid_sha256": "sha256:" + "b" * 64,
        "final_endpoint_binding": {
            "schema_version": "talk-boundary-final-endpoint-binding.v1",
            "status": "PASS",
            "final_start_ms": 9_780,
            "final_end_ms": 82_670,
            "reason_codes": [],
        },
    }
    return {
        "final_start_ms": 9_780,
        "final_end_ms": 82_670,
        "boundary_semantic_review": review,
        "final_delivery_boundary_semantic_review": {"status": "PASS"},
    }


def _live_correction(before: str, after: str) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    operations = ["1=A", "3=C", "6=F", "27=AA"]
    burn = {"path": "/runtime/final.mp4", "sha256": "sha256:" + "b" * 64}
    live = {
        "schema_version": "human-subtitle-correction.v2",
        "stage_order": "human_text_then_speaker_then_burn",
        "corrected_at": "2026-08-21T10:38:16.502731+00:00",
        "candidate_id": refresh.CANDIDATE_ID,
        "before_srt_sha256": _sha(before)[7:],
        "after_srt_sha256": _sha(after)[7:],
        "replace_operations": [],
        "set_line_operations": operations,
        "refresh_only": False,
        "text_source": None,
        "text_source_sha256": None,
        "text_override": None,
        "text_override_sha256": None,
        "text_override_manifest": None,
        "text_override_manifest_sha256": None,
        "text_override_decision_output": None,
        "text_override_decision_output_sha256": None,
        "text_override_output": None,
        "text_override_output_sha256": None,
        "timing_source": None,
        "timing_source_sha256": None,
        "speaker_mode": "uniform_host",
        "speaker_manifest": None,
        "speaker_manifest_sha256": None,
        "burned_media": burn["path"],
        "burned_media_sha256": burn["sha256"][7:],
        "delivery_branding_authority": {
            "schema_version": "sealed-subtitle-correction-delivery-authority.v1",
            "authority_path": "/repo/authority.json",
            "authority_sha256": "sha256:" + "c" * 64,
            "authority_repository_seal": {
                "mode": "DEPLOYED_MANIFEST", "deployed_commit": "d" * 40,
                "relative_path": "assets/authority.json", "sha256": "sha256:" + "e" * 64,
            },
            "branding_intro": {
                "intro_id": "intro", "intro_media_sha256": "sha256:" + "f" * 64,
                "intro_offset_ms": 6200, "status": "PREPENDED",
            },
            "record_sha256": "sha256:" + "1" * 64,
            "publish_sha256": "sha256:" + "2" * 64,
            "burned_video_sha256": "sha256:" + "3" * 64,
        },
        "upload_enabled": False,
    }
    authority = {
        "before_srt_sha256": _sha(before), "after_srt_sha256": _sha(after),
        "set_line_operations": operations,
    }
    return live, authority, {"burn": burn}


def test_live_correction_bare_hex_normalizes_only_after_full_live_schema_binding() -> None:
    before = _srt(*[str(index) for index in range(1, 28)])
    after = _srt("A", "2", "C", "4", "5", "F", *[str(index) for index in range(7, 27)], "AA")
    live, authority, preimage = _live_correction(before, after)
    assert refresh._normalize_live_correction(
        live, authority_correction=authority, before_srt=before, final_srt=after,
        authority_preimage=preimage,
    ) == _correction(before, after)


@pytest.mark.parametrize(
    "mutate, expected",
    [
        (lambda value: value.__setitem__("before_srt_sha256", "sha256:" + str(value["before_srt_sha256"])), "CORRECTION_BEFORE_SHA_INVALID"),
        (lambda value: value.__setitem__("after_srt_sha256", "0" * 64), "CORRECTION_HASH_DRIFT"),
        (lambda value: value.__setitem__("replace_operations", ["1=A"]), "CORRECTION_SCHEMA_INVALID"),
        (lambda value: value.__setitem__("set_line_operations", ["1=A", "3=C", "6=F", "27=AA", "7=drift"]), "CORRECTION_HASH_DRIFT"),
    ],
)
def test_live_correction_rejects_prefix_confusion_hash_or_unlisted_operations(mutate, expected) -> None:
    before = _srt(*[str(index) for index in range(1, 28)])
    after = _srt("A", "2", "C", "4", "5", "F", *[str(index) for index in range(7, 27)], "AA")
    live, authority, preimage = _live_correction(before, after)
    mutate(live)
    with pytest.raises(refresh.QixiTerminalEvidenceRefreshError, match=expected):
        refresh._normalize_live_correction(
            live, authority_correction=authority, before_srt=before, final_srt=after,
            authority_preimage=preimage,
        )


def test_refresh_rejects_unlisted_text_and_timing_drift() -> None:
    before = _srt(*[str(index) for index in range(1, 28)])
    after = _srt("A", "2", "C", "4", "5", "F", *[str(index) for index in range(7, 27)], "AA")
    refresh._assert_text_and_grid_immutable(
        before_srt=before, final_srt=after, correction=_correction(before, after)
    )
    with pytest.raises(refresh.QixiTerminalEvidenceRefreshError, match="UNLISTED_TEXT_DRIFT"):
        refresh._assert_text_and_grid_immutable(
            before_srt=before,
            final_srt=after.replace("\n7\n00:00:07,000 --> 00:00:07,900\n7", "\n7\n00:00:07,000 --> 00:00:07,900\nDRIFT"),
            correction=_correction(before, after),
        )
    with pytest.raises(refresh.QixiTerminalEvidenceRefreshError, match="TIMING_DRIFT"):
        refresh._assert_text_and_grid_immutable(
            before_srt=before,
            final_srt=after.replace("00:00:02,900", "00:00:02,899", 1),
            correction=_correction(before, after),
        )


def test_sealed_human_truth_binds_four_replacements_and_two_assertions() -> None:
    authority, payload, before, final = _sealed_human_truth_srt()
    audit, provenance = refresh._human_truth_from_authority(
        authority=authority,
        authority_bytes=payload,
        runtime_authority_sha256="sha256:" + hashlib.sha256(payload).hexdigest(),
        before_srt=before,
        final_srt=final,
    )
    assert audit["status"] == "ALREADY_SATISFIED"
    assert [row["cue_indexes"][0] for row in audit["satisfied"]] == [1, 3, 5, 6, 14, 27]
    assert [row["cue_index"] for row in provenance["covered_cues"]] == [1, 3, 5, 6, 14, 27]
    assert provenance["authority_bytes_sha256"] == "sha256:" + hashlib.sha256(payload).hexdigest()


def test_human_truth_loader_requires_the_committed_asset_and_live_seal() -> None:
    authority, payload, before, final = _sealed_human_truth_srt()
    del authority
    audit, provenance = refresh._load_qixi_human_truth(
        repo_root=refresh.ROOT,
        live_correction={
            "delivery_branding_authority": {
                "authority_repository_seal": {
                    "sha256": "sha256:" + hashlib.sha256(payload).hexdigest(),
                }
            }
        },
        before_srt=before,
        final_srt=final,
    )
    assert audit["status"] == "ALREADY_SATISFIED"
    assert provenance["authority_path"] == str(refresh.HUMAN_TRUTH_AUTHORITY_PATH)
    with pytest.raises(refresh.QixiTerminalEvidenceRefreshError, match="HUMAN_TRUTH_DRIFT"):
        refresh._load_qixi_human_truth(
            repo_root=refresh.ROOT,
            live_correction={
                "delivery_branding_authority": {
                    "authority_repository_seal": {"sha256": "sha256:" + "0" * 64}
                }
            },
            before_srt=before,
            final_srt=final,
        )


@pytest.mark.parametrize("kind", ["hash", "replacement", "assertion", "timing", "other_cue"])
def test_sealed_human_truth_rejects_hash_text_or_timing_drift(kind: str) -> None:
    authority, payload, before, final = _sealed_human_truth_srt()
    broken = copy.deepcopy(authority)
    if kind == "hash":
        broken["correction"]["output_srt_sha256"] = "sha256:" + "0" * 64
    elif kind == "replacement":
        broken["correction"]["replacements"][0]["after"] = "漂移"
    elif kind == "assertion":
        broken["correction"]["assertions"][0]["text"] = "漂移"
    elif kind == "timing":
        broken["correction"]["replacements"][0]["start"] = "00:00:00,000"
    else:
        final = final.replace("感觉kmx比较多吧", "未列 cue 漂移")
    with pytest.raises(refresh.QixiTerminalEvidenceRefreshError, match="HUMAN_TRUTH"):
        refresh._human_truth_from_authority(
            authority=broken,
            authority_bytes=payload,
            runtime_authority_sha256="sha256:" + hashlib.sha256(payload).hexdigest(),
            before_srt=before,
            final_srt=final,
        )


def test_operator_assertion_truth_resolves_only_the_owned_provider_finding() -> None:
    authority, payload, before, final = _sealed_human_truth_srt()
    audit, _provenance = refresh._human_truth_from_authority(
        authority=authority,
        authority_bytes=payload,
        runtime_authority_sha256="sha256:" + hashlib.sha256(payload).hexdigest(),
        before_srt=before,
        final_srt=final,
    )
    cue_five = parse_srt_cues(final)[4]
    protected = {
        "cue_index": 5, "proposed_full_cue": "模型改写", "base_text_sha256": hashlib.sha256(cue_five.text.encode()).hexdigest(),
    }
    other = {"cue_index": 2, "proposed_full_cue": "模型改写", "base_text_sha256": hashlib.sha256(parse_srt_cues(final)[1].text.encode()).hexdigest()}
    pending, resolved = resolve_verified_source_truth_findings(
        final, [protected, other], source_truth_audit=audit, timeline_offset_ms=0
    )
    assert [row["cue_index"] for row in resolved] == [5]
    assert [row["cue_index"] for row in pending] == [2]


def test_source_full_window_rejects_relative_delivery_or_wrong_interval() -> None:
    boundary = _source_boundary_audit()
    assert refresh._require_qixi_source_boundary(boundary)["review_scope"] == "source_full_window"
    relative = copy.deepcopy(boundary)
    relative["boundary_semantic_review"] = relative["final_delivery_boundary_semantic_review"]
    wrong_interval = copy.deepcopy(boundary)
    wrong_interval["boundary_semantic_review"]["final_endpoint_binding"]["final_start_ms"] = 0
    for broken in (relative, wrong_interval):
        with pytest.raises(refresh.QixiTerminalEvidenceRefreshError, match="SOURCE_"):
            refresh._require_qixi_source_boundary(broken)


def test_terminal_review_requires_a_passed_raw_correction_pass() -> None:
    source = _source_boundary_audit()["boundary_semantic_review"]
    assert refresh._correction_pass_for_terminal_review(
        {"correction_pass": _correction_pass()}, source_boundary=source
    )["boundary_semantic_review"] == source
    with pytest.raises(refresh.QixiTerminalEvidenceRefreshError, match="CORRECTION_PASS"):
        refresh._correction_pass_for_terminal_review(
            {"correction_pass": {"status": "AUDITOR_UNAVAILABLE", "findings": [], "applied_count": 0}},
            source_boundary=source,
        )


def test_refresh_uses_new_final_and_boundary_receipts(monkeypatch) -> None:
    before = _srt(*[str(index) for index in range(1, 28)])
    after = _srt("A", "2", "C", "4", "5", "F", *[str(index) for index in range(7, 27)], "AA")
    old_chat = {
        "final_text_srt_sha256": _sha(before)[7:],
        "final_review_audit": {"old": "audit", "correction_pass": _correction_pass()},
    }
    boundary = {"status": "PASS", "request_sha256": "sha256:" + "a" * 64}
    fresh = {
        "schema_version": "final-review-audit.v1",
        "reviewed_srt_sha256": _sha(after),
        "boundary_semantic_review": boundary,
        "discovery": {"status": "COMPLETE"},
        "correction_mutation_authority": {
            "schema_version": "subtitle-correction-mutation-audit.v1", "status": "PASS"
        },
        "findings": [],
        "validated_finding_count": 0,
    }
    seen: dict[str, object] = {}
    monkeypatch.setattr(
        refresh,
        "exact_delivery_correction_audit",
        lambda **kwargs: seen.setdefault("boundary", kwargs) and {"boundary_semantic_review": boundary},
    )
    monkeypatch.setattr(
        refresh,
        "_run_exact_final_release_review",
        lambda **kwargs: seen.setdefault("final", kwargs) and fresh,
    )
    monkeypatch.setattr(refresh, "clip_context_prompt_text", lambda _context: "canonical context")
    monkeypatch.setattr(refresh, "validate_final_review_release", lambda audit, **_kwargs: audit)
    result = refresh.refresh_terminal_evidence(
        before_srt=before,
        final_srt=after,
        correction=_correction(before, after),
        old_chat_authority=old_chat,
        selection_hook="hook",
        selection_scorecard={},
        structured_context="context",
        clip_context={"context_sha256": "sha256:" + "e" * 64},
        source_boundary=_source_boundary(),
        verified_authority_audit=_verified_truth(),
        operator_truth_provenance=_provenance(),
        source_final_start_ms=9_780,
        source_final_end_ms=82_670,
        boundary_max_forward_ms=30_000,
        adapters=object(),
        authoritative_chat=(),
        final_review_llm=lambda _prompt: '{"findings":[]}',
        boundary_review_llm=lambda _prompt: '{"decision":"PASS"}',
    )
    assert seen["boundary"]["final_srt_text"] == after
    assert seen["boundary"]["correction_audit"] == {
        "status": "APPLIED",
        "findings": [],
        "applied_count": 0,
        "boundary_semantic_review": _source_boundary(),
    }
    assert seen["boundary"]["candidate_context"] == "canonical context"
    assert seen["final"]["correction_audit"]["boundary_semantic_review"] == boundary
    assert result["chat_authority"]["final_text_srt_sha256"] == _sha(after)[7:]
    assert {
        result["chat_authority"][key]
        for key in (
            "final_text_srt_sha256", "final_speaker_srt_sha256", "final_output_srt_sha256",
        )
    } == {_sha(after)[7:]}
    assert result["final_delivery_boundary_semantic_review"] == boundary
    assert result["chat_authority"]["final_review_audit"] == {
        **fresh, "qixi_operator_truth_provenance": _provenance(),
    }


def test_final_review_failure_preserves_reason_and_exposes_only_sanitized_diagnostic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = _srt(*[str(index) for index in range(1, 28)])
    after = _srt("A", "2", "C", "4", "5", "F", *[str(index) for index in range(7, 27)], "AA")
    old_chat = {
        "final_text_srt_sha256": _sha(before)[7:],
        "final_review_audit": {"old": "audit", "correction_pass": _correction_pass()},
    }
    private_provider_text = "prompt=do-not-render raw-response=cookie=not-a-cookie media=/private/final.mp4"
    boundary = {
        "status": "BLOCK",
        "reason_codes": [
            "BOUNDARY_NEXT_TOPIC_WITNESS_MISSING",
            "NEXT_TOPIC_SEPARATED_NOT_PROVEN",
            "TERMINAL_SOURCE_SEPARATION_WITNESS_BLOCK",
        ],
        "raw_provider_response": private_provider_text,
    }
    fresh = {
        "schema_version": "final-review-audit.v2",
        "reviewed_srt_sha256": _sha(after),
        "status": "FLAGGED",
        "release_gate": "BLOCK",
        "reason_codes": [
            "FINAL_REVIEW_CORRECTION_MUTATION_AUTHORITY_INVALID",
            private_provider_text,
        ],
        "boundary_semantic_review": boundary,
        "discovery": {
            "status": "COMPLETE",
            "reason_codes": ["SUBTITLE_SPEAKER_SRT_CHAT_HASH_MISMATCH"],
            "prompt": private_provider_text,
        },
        "findings": [
            {
                "reason_code": "SUBTITLE_SPEAKER_SRT_CHAT_HASH_MISMATCH",
                "cue_index": 3,
                "raw_response": private_provider_text,
            },
            {"reason_code": "FINAL_REVIEW_UNRESOLVED_FINDINGS", "cue_index": 5},
            {"reason_code": "FINAL_REVIEW_UNRESOLVED_FINDINGS", "cue_index": 6},
        ],
        "validated_finding_count": 3,
        "correction_mutation_authority": {
            "schema_version": "subtitle-correction-mutation-audit.v1",
            "status": "BLOCK",
            "failures": [
                {
                    "reason_code": "CORRECTION_DISCOVERY_INCOMPLETE",
                    "cue_index": 3,
                    "provider_response": private_provider_text,
                }
            ],
        },
    }
    monkeypatch.setattr(
        refresh,
        "exact_delivery_correction_audit",
        lambda **_kwargs: {"boundary_semantic_review": boundary},
    )
    monkeypatch.setattr(refresh, "_run_exact_final_release_review", lambda **_kwargs: fresh)
    monkeypatch.setattr(refresh, "clip_context_prompt_text", lambda _context: "canonical context")

    with pytest.raises(refresh.QixiTerminalEvidenceRefreshError) as raised:
        refresh.refresh_terminal_evidence(
            before_srt=before,
            final_srt=after,
            correction=_correction(before, after),
            old_chat_authority=old_chat,
            selection_hook="hook",
            selection_scorecard={},
            structured_context="context",
            clip_context={"context_sha256": "sha256:" + "e" * 64},
            source_boundary=_source_boundary(),
            verified_authority_audit=_verified_truth(),
            operator_truth_provenance=_provenance(),
            source_final_start_ms=9_780,
            source_final_end_ms=82_670,
            boundary_max_forward_ms=30_000,
            adapters=object(),
            authoritative_chat=(),
            final_review_llm=lambda _prompt: '{"findings":[]}',
            boundary_review_llm=lambda _prompt: '{"decision":"PASS"}',
        )

    error = raised.value
    assert error.reason_code == "QIXI_TERMINAL_REFRESH_FINAL_REVIEW_BLOCKED"
    assert error.underlying_reason_code == "FINAL_REVIEW_CORRECTION_MUTATION_AUTHORITY_INVALID"
    diagnostic = refresh.full_dry_run_failure_result(error)
    assert diagnostic["predicate"] == {
        "name": "final_review_contract",
        "status": "FAIL",
        "reason_code": "FINAL_REVIEW_CORRECTION_MUTATION_AUTHORITY_INVALID",
    }
    assert (
        diagnostic["final_review_reason_code"]
        == "FINAL_REVIEW_CORRECTION_MUTATION_AUTHORITY_INVALID"
    )
    assert diagnostic["reviewed_srt_sha256"] == _sha(after)
    assert diagnostic["reported_reviewed_srt_sha256"] == _sha(after)
    assert diagnostic["discovery"] == {
        "status": "COMPLETE",
        "finding_count": 3,
        "reason_codes": ["SUBTITLE_SPEAKER_SRT_CHAT_HASH_MISMATCH"],
    }
    assert diagnostic["findings"] == {
        "count": 3,
        "validated_count": 3,
        "reason_codes": [
            "FINAL_REVIEW_UNRESOLVED_FINDINGS",
            "SUBTITLE_SPEAKER_SRT_CHAT_HASH_MISMATCH",
        ],
        "cue_indices": [3, 5, 6],
    }
    assert diagnostic["correction_mutation_authority"] == {
        "status": "BLOCK",
        "failure_count": 1,
        "reason_codes": ["CORRECTION_DISCOVERY_INCOMPLETE"],
        "cue_indices": [3],
    }
    assert diagnostic["boundary"] == {
        "status": "BLOCK",
        "reason_codes": [
            "BOUNDARY_NEXT_TOPIC_WITNESS_MISSING",
            "NEXT_TOPIC_SEPARATED_NOT_PROVEN",
            "TERMINAL_SOURCE_SEPARATION_WITNESS_BLOCK",
        ],
    }
    assert diagnostic["terminal_write_predicates"] == [
        {"name": "target_write", "status": "NOT_ATTEMPTED"},
        {"name": "state_write", "status": "NOT_ATTEMPTED"},
        {"name": "journal_write", "status": "NOT_ATTEMPTED"},
        {"name": "terminal_private_stage", "status": "NOT_CREATED"},
    ]
    rendered = json.dumps(diagnostic, sort_keys=True)
    assert private_provider_text not in rendered
    assert "cookie=" not in rendered
    assert "final.mp4" not in rendered


def test_allowlist_drift_has_a_distinct_bounded_full_dry_run_predicate() -> None:
    diagnostic = refresh.full_dry_run_failure_result(
        refresh.QixiTerminalEvidenceRefreshError(
            "QIXI_TERMINAL_REFRESH_RECORD_ALLOWLIST_DRIFT"
        )
    )
    assert diagnostic["predicate"] == {
        "name": "allowed_mutations",
        "status": "FAIL",
        "reason_code": "QIXI_TERMINAL_REFRESH_RECORD_ALLOWLIST_DRIFT",
    }


def test_allowlist_subtree_contract_requires_every_root_and_no_prefix_escape() -> None:
    assert refresh._changed_within_allowlisted_subtrees(
        {
            "/boundary_audit/final_delivery_boundary_semantic_review/status",
            "/story_contract/boundary_semantic_review/request_sha256",
        },
        [
            "/boundary_audit/final_delivery_boundary_semantic_review",
            "/story_contract/boundary_semantic_review",
        ],
    )
    assert not refresh._changed_within_allowlisted_subtrees(
        {"/boundary_audit/final_delivery_boundary_semantic_review/status"},
        [
            "/boundary_audit/final_delivery_boundary_semantic_review",
            "/story_contract/boundary_semantic_review",
        ],
    )


def test_projection_uses_subtree_allowlist_and_preserves_absent_mirrors() -> None:
    old_record = {
        "boundary_audit": {"boundary_semantic_review": {"status": "PASS"}},
        "story_contract": {"boundary_semantic_review": {"status": "PASS"}},
        "artifact_hashes": {"chat_authority_audit_sha256": "sha256:" + "0" * 64},
        "chat_authority_audit_path": "auto_123655_771_844.chat-authority.json",
    }
    fresh_boundary = {
        "schema_version": "talk-boundary-semantic-review.v1", "status": "PASS",
        "request_sha256": "sha256:" + "a" * 64,
    }
    projected = refresh.project_evidence_mirrors(
        record=old_record,
        delivery_record=copy.deepcopy(old_record),
        publish={}, state={}, refreshed_chat_bytes=b"fresh-chat",
        final_delivery_boundary=fresh_boundary,
        allowed_mutations={
            "record": [
                "/artifact_hashes/chat_authority_audit_sha256",
                "/boundary_audit/final_delivery_boundary_semantic_review",
                "/story_contract/boundary_semantic_review",
            ],
            "delivery_record": [
                "/artifact_hashes/chat_authority_audit_sha256",
                "/boundary_audit/final_delivery_boundary_semantic_review",
                "/story_contract/boundary_semantic_review",
            ],
            "publish": [], "state": [],
        },
    )
    expected_hash = "sha256:" + hashlib.sha256(b"fresh-chat").hexdigest()
    assert projected["record"]["artifact_hashes"]["chat_authority_audit_sha256"] == expected_hash
    assert projected["delivery_record"] == projected["record"]
    assert projected["record"]["boundary_audit"]["final_delivery_boundary_semantic_review"] == fresh_boundary
    assert projected["record"]["story_contract"]["boundary_semantic_review"] == fresh_boundary
    assert projected["publish"] == {} and projected["state"] == {}
    assert not refresh._changed_within_allowlisted_subtrees(
        {"/story_contract/boundary_semantic_review_extra/status"},
        ["/story_contract/boundary_semantic_review"],
    )


def test_terminal_authority_declares_no_publish_or_state_story_mirror() -> None:
    authority = json.loads((refresh.ROOT / refresh.AUTHORITY_PATH).read_text(encoding="utf-8"))
    claimed = authority.pop("authority_sha256")
    assert authority["allowed_mutations"]["publish"] == []
    assert authority["allowed_mutations"]["state"] == []
    assert claimed == "sha256:4298f33bf342507a0b5e234ebaf1d2a63ca38dab8895cb10e011cca25de94f42"
    assert refresh._canonical_sha(authority) == claimed


def test_committed_empty_mirror_scopes_require_bytes_and_no_injection() -> None:
    story = {"boundary_semantic_review": {"status": "PASS"}}
    publish = {}
    state = {"picks": [{"candidate_id": refresh.CANDIDATE_ID, "title": "unchanged"}]}
    before_publish = json.dumps(publish, sort_keys=True).encode()
    before_state = json.dumps(state, sort_keys=True).encode()
    kwargs = {
        "allowed_mutations": {"publish": [], "state": []},
        "before_publish": before_publish,
        "after_publish": before_publish,
        "before_state": before_state,
        "after_state": before_state,
        "current_publish": publish,
        "current_state": state,
        "story": story,
    }
    refresh._verify_committed_optional_mirrors(**kwargs)

    injected_publish = {"story_contract": story}
    with pytest.raises(refresh.QixiTerminalEvidenceRefreshError, match="COMMITTED_CONTEXT_DRIFT"):
        refresh._verify_committed_optional_mirrors(
            **{**kwargs, "after_publish": json.dumps(injected_publish, sort_keys=True).encode(),
               "current_publish": injected_publish}
        )
    injected_state = {
        "picks": [{"candidate_id": refresh.CANDIDATE_ID, "story_contract": story}]
    }
    with pytest.raises(refresh.QixiTerminalEvidenceRefreshError, match="COMMITTED_CONTEXT_DRIFT"):
        refresh._verify_committed_optional_mirrors(
            **{**kwargs, "after_state": json.dumps(injected_state, sort_keys=True).encode(),
               "current_state": injected_state}
        )
    with pytest.raises(refresh.QixiTerminalEvidenceRefreshError, match="COMMITTED_CONTEXT_DRIFT"):
        refresh._verify_committed_optional_mirrors(
            **{**kwargs, "after_publish": b"{ }"}
        )


def test_committed_publish_mirror_requires_declared_existing_scope() -> None:
    story = {"boundary_semantic_review": {"status": "PASS"}}
    before_publish = json.dumps({"story_contract": story}, sort_keys=True).encode()
    state = {"picks": [{"candidate_id": refresh.CANDIDATE_ID}]}
    state_bytes = json.dumps(state, sort_keys=True).encode()
    refresh._verify_committed_optional_mirrors(
        allowed_mutations={
            "publish": ["/story_contract/boundary_semantic_review"], "state": [],
        },
        before_publish=before_publish,
        after_publish=before_publish,
        before_state=state_bytes,
        after_state=state_bytes,
        current_publish={"story_contract": story},
        current_state=state,
        story=story,
    )


def test_cli_full_dry_run_prints_sanitized_failure_without_writes(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    audit = {
        "status": "BLOCKED",
        "release_gate": "BLOCK",
        "reviewed_srt_sha256": "sha256:" + "a" * 64,
        "discovery": {"status": "COMPLETE"},
        "findings": [],
        "validated_finding_count": 0,
        "boundary_semantic_review": {"status": "BLOCK"},
    }
    failure = refresh.QixiTerminalEvidenceRefreshError(
        "QIXI_TERMINAL_REFRESH_FINAL_REVIEW_BLOCKED",
        underlying_reason_code="FINAL_REVIEW_RELEASE_GATE_BLOCKED",
        diagnostic_result=refresh._final_review_failure_diagnostic(
            final_audit=audit,
            expected_srt_sha256="sha256:" + "b" * 64,
            reason_code="FINAL_REVIEW_RELEASE_GATE_BLOCKED",
        ),
    )
    monkeypatch.setattr(
        refresh_cli, "load_authority", lambda _root: {"runtime_root": str(tmp_path)}
    )
    monkeypatch.setattr(refresh_cli, "validate_runtime", lambda _authority: {})
    monkeypatch.setattr(
        refresh_cli, "exclusive_runner_lock", lambda _root: nullcontext()
    )
    monkeypatch.setattr(
        refresh_cli, "build_staged_refresh", lambda **_kwargs: (_ for _ in ()).throw(failure)
    )
    monkeypatch.setattr(
        refresh_cli,
        "apply_projection",
        lambda **_kwargs: pytest.fail("failed full dry run must not apply a projection"),
    )

    assert refresh_cli.main(["--full-dry-run"]) == 2
    assert json.loads(capsys.readouterr().out) == refresh.full_dry_run_failure_result(failure)
    assert list(tmp_path.iterdir()) == []


def test_projection_dry_run_is_target_write_free(tmp_path) -> None:
    paths = {}
    before = {}
    for role in ("chat", "record", "delivery_record", "publish", "state"):
        path = tmp_path / f"{role}.json"
        payload = f"before-{role}".encode()
        path.write_bytes(payload)
        paths[role] = path
        before[role] = payload
    authority = {
        "authority_sha256": "sha256:" + "a" * 64,
        "preimage": {
            role: {
                "path": str(path),
                "sha256": "sha256:" + hashlib.sha256(before[role]).hexdigest(),
                "bytes": len(before[role]),
                "mode": 0o644,
            }
            for role, path in paths.items()
        },
    }
    after = {role: f"after-{role}".encode() for role in before}
    result = refresh.apply_projection(
        authority=authority, before=before, after=after, apply=False
    )
    assert result["status"] == "DRY_RUN_PASS"
    assert {role: path.read_bytes() for role, path in paths.items()} == before
    assert not (tmp_path / "qixi-terminal-evidence-refresh").exists()


def test_projection_apply_is_cas_and_create_only(tmp_path) -> None:
    paths, before = {}, {}
    for role in ("chat", "record", "delivery_record", "publish", "state"):
        path = tmp_path / f"{role}.json"
        payload = f"before-{role}".encode()
        path.write_bytes(payload)
        paths[role], before[role] = path, payload
    authority = {
        "authority_sha256": "sha256:" + "b" * 64,
        "preimage": {
            role: {
                "path": str(path), "sha256": "sha256:" + hashlib.sha256(before[role]).hexdigest(),
                "bytes": len(before[role]), "mode": 0o644,
            }
            for role, path in paths.items()
        },
    }
    after = {role: f"after-{role}".encode() for role in before}
    result = refresh.apply_projection(authority=authority, before=before, after=after, apply=True)
    assert result["status"] == "COMMITTED"
    assert {role: path.read_bytes() for role, path in paths.items()} == after
    assert refresh.apply_projection(authority=authority, before=before, after=after, apply=True)["status"] == "ALREADY_COMMITTED"


def test_projection_rolls_back_prior_targets_on_install_failure(tmp_path, monkeypatch) -> None:
    paths, before = {}, {}
    for role in ("chat", "record", "delivery_record", "publish", "state"):
        path = tmp_path / f"{role}.json"
        payload = f"before-{role}".encode()
        path.write_bytes(payload)
        paths[role], before[role] = path, payload
    authority = {
        "authority_sha256": "sha256:" + "c" * 64,
        "preimage": {
            role: {"path": str(path), "sha256": "sha256:" + hashlib.sha256(before[role]).hexdigest(), "bytes": len(before[role]), "mode": 0o644}
            for role, path in paths.items()
        },
    }
    after = {role: f"after-{role}".encode() for role in before}
    original = refresh._replace_exact
    calls = 0

    def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise refresh.QixiTerminalEvidenceRefreshError("injected")
        return original(*args, **kwargs)

    monkeypatch.setattr(refresh, "_replace_exact", fail_second)
    with pytest.raises(refresh.QixiTerminalEvidenceRefreshError, match="injected"):
        refresh.apply_projection(authority=authority, before=before, after=after, apply=True)
    assert {role: path.read_bytes() for role, path in paths.items()} == before


def test_projection_adopts_owned_stage_after_checkpoint_crash(tmp_path, monkeypatch) -> None:
    paths, before = {}, {}
    for role in ("chat", "record", "delivery_record", "publish", "state"):
        path = tmp_path / f"{role}.json"
        payload = f"before-{role}".encode()
        path.write_bytes(payload)
        paths[role], before[role] = path, payload
    authority = {
        "authority_sha256": "sha256:" + "d" * 64,
        "preimage": {role: {"path": str(path), "sha256": "sha256:" + hashlib.sha256(before[role]).hexdigest(), "bytes": len(before[role]), "mode": 0o644} for role, path in paths.items()},
    }
    after = {role: f"after-{role}".encode() for role in before}
    original = refresh._checkpoint
    calls = 0
    def crash_after_stage(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise KeyboardInterrupt("crash after staged owner")
        return original(*args, **kwargs)
    monkeypatch.setattr(refresh, "_checkpoint", crash_after_stage)
    with pytest.raises(KeyboardInterrupt, match="staged owner"):
        refresh.apply_projection(authority=authority, before=before, after=after, apply=True)
    monkeypatch.setattr(refresh, "_checkpoint", original)
    assert refresh.apply_projection(authority=authority, before=before, after=after, apply=True)["status"] == "COMMITTED"
    assert {role: path.read_bytes() for role, path in paths.items()} == after


def test_projection_recovers_crash_after_rename_before_install_checkpoint(tmp_path, monkeypatch) -> None:
    paths, before = {}, {}
    for role in ("chat", "record", "delivery_record", "publish", "state"):
        path = tmp_path / f"{role}.json"
        payload = f"before-{role}".encode()
        path.write_bytes(payload)
        paths[role], before[role] = path, payload
    authority = {"authority_sha256": "sha256:" + "f" * 64, "preimage": {
        role: {"path": str(path), "sha256": "sha256:" + hashlib.sha256(before[role]).hexdigest(), "bytes": len(before[role]), "mode": 0o644}
        for role, path in paths.items()
    }}
    after = {role: f"after-{role}".encode() for role in before}
    original = refresh._checkpoint

    def crash_after_rename(*args, **kwargs):
        if kwargs.get("phase") == "INSTALLED":
            raise KeyboardInterrupt("crash after rename")
        return original(*args, **kwargs)

    monkeypatch.setattr(refresh, "_checkpoint", crash_after_rename)
    with pytest.raises(KeyboardInterrupt, match="after rename"):
        refresh.apply_projection(authority=authority, before=before, after=after, apply=True)
    assert paths["chat"].read_bytes() == after["chat"]
    monkeypatch.setattr(refresh, "_checkpoint", original)
    assert refresh.apply_projection(authority=authority, before=before, after=after, apply=True)["status"] == "COMMITTED"
    assert {role: path.read_bytes() for role, path in paths.items()} == after


def test_projection_failure_persists_recovery_journal(tmp_path, monkeypatch) -> None:
    paths, before = {}, {}
    for role in ("chat", "record", "delivery_record", "publish", "state"):
        path = tmp_path / f"{role}.json"
        payload = f"before-{role}".encode()
        path.write_bytes(payload)
        paths[role], before[role] = path, payload
    authority = {"authority_sha256": "sha256:" + "e" * 64, "preimage": {
        role: {"path": str(path), "sha256": "sha256:" + hashlib.sha256(before[role]).hexdigest(), "bytes": len(before[role]), "mode": 0o644}
        for role, path in paths.items()
    }}
    monkeypatch.setattr(refresh, "create_staged_inode", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("stage fail")))
    with pytest.raises(RuntimeError, match="stage fail"):
        refresh.apply_projection(authority=authority, before=before, after={role: b"after" for role in before}, apply=True)
    root = refresh._refresh_root(authority)
    assert __import__("json").loads((root / "journal.json").read_text())["status"] == "ROLLED_BACK"


def test_projection_retains_rollback_required_when_reverse_rollback_fails(tmp_path, monkeypatch) -> None:
    paths, before = {}, {}
    for role in ("chat", "record", "delivery_record", "publish", "state"):
        path = tmp_path / f"{role}.json"
        payload = f"before-{role}".encode()
        path.write_bytes(payload)
        paths[role], before[role] = path, payload
    authority = {"authority_sha256": "sha256:" + "9" * 64, "preimage": {
        role: {"path": str(path), "sha256": "sha256:" + hashlib.sha256(before[role]).hexdigest(), "bytes": len(before[role]), "mode": 0o644}
        for role, path in paths.items()
    }}
    monkeypatch.setattr(refresh, "create_staged_inode", lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("stage fail")))
    monkeypatch.setattr(refresh, "_rollback_entries", lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("rollback fail")))
    with pytest.raises(RuntimeError, match="stage fail"):
        refresh.apply_projection(authority=authority, before=before, after={role: b"after" for role in before}, apply=True)
    journal = json.loads((refresh._refresh_root(authority) / "journal.json").read_text())
    assert journal["status"] == "ROLLBACK_REQUIRED"


@pytest.mark.parametrize("tamper", ["journal", "receipt", "after"])
def test_committed_projection_replay_rejects_tampered_receipt_journal_or_after_bytes(tmp_path, tamper) -> None:
    paths, before = {}, {}
    for role in ("chat", "record", "delivery_record", "publish", "state"):
        path = tmp_path / f"{role}.json"
        payload = f"before-{role}".encode()
        path.write_bytes(payload)
        paths[role], before[role] = path, payload
    authority = {"authority_sha256": "sha256:" + "8" * 64, "preimage": {
        role: {"path": str(path), "sha256": "sha256:" + hashlib.sha256(before[role]).hexdigest(), "bytes": len(before[role]), "mode": 0o644}
        for role, path in paths.items()
    }}
    after = {role: f"after-{role}".encode() for role in before}
    refresh.apply_projection(authority=authority, before=before, after=after, apply=True)
    root = refresh._refresh_root(authority)
    if tamper == "journal":
        (root / "journal.json").write_bytes(b"{}")
    elif tamper == "receipt":
        (root / "receipt.json").write_bytes(b"{}")
    else:
        paths["chat"].write_bytes(b"tampered")
    with pytest.raises(refresh.QixiTerminalEvidenceRefreshError, match="QIXI_TERMINAL_REFRESH"):
        refresh.apply_projection(authority=authority, before=before, after=after, apply=True)


def test_terminal_predecessor_replay_uses_the_sealed_before_chat(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Terminal after-chat bytes must not replace predecessor receipt evidence."""

    sealed_chat, terminal_chat = b'{"predecessor":true}\n', b'{"terminal":true}\n'
    entries = [
        {
            "role": role,
            "before_bytes_b64": base64.b64encode(
                sealed_chat if role == "chat" else f"before-{role}".encode()
            ).decode(),
            "after_bytes_b64": base64.b64encode(
                terminal_chat if role == "chat" else f"after-{role}".encode()
            ).decode(),
        }
        for role in ("chat", "record", "delivery_record", "publish", "state")
    ]
    journal = {
        "schema_version": "synthetic",
        "status": "COMMITTED",
        "candidate_id": refresh.CANDIDATE_ID,
        "recording_date": refresh.RECORDING_DATE,
        "authority_sha256": "sha256:" + "a" * 64,
        "entries": entries,
        "matrix": refresh._terminal_matrix(),
        "journal_sha256": "sha256:" + "b" * 64,
    }
    journal_root = tmp_path / "terminal"
    journal_root.mkdir()
    (journal_root / "journal.json").write_bytes(refresh._json_bytes(journal))
    (journal_root / "receipt.json").write_bytes(refresh._json_bytes(refresh._receipt_for(journal)))
    predecessor = {}
    for role in ("journal", "receipt"):
        path = tmp_path / f"predecessor-{role}.json"
        payload = b"sealed predecessor " + role.encode()
        path.write_bytes(payload)
        predecessor[role] = {
            "path": str(path),
            "bytes": len(payload),
            "sha256": "sha256:" + hashlib.sha256(payload).hexdigest(),
        }
    public_asset = tmp_path / "public-authority.json"
    public_asset.write_text("{}", encoding="utf-8")
    authority = {
        "authority_sha256": journal["authority_sha256"],
        "predecessor_recovery": predecessor,
        "preimage": {"record": {"path": str(tmp_path / "record.json")}},
    }
    monkeypatch.setattr(refresh, "load_authority", lambda _root: authority)
    monkeypatch.setattr(refresh, "_refresh_root", lambda _authority: journal_root)
    monkeypatch.setattr(refresh, "_validate_journal", lambda *_args, **_kwargs: {})
    from src.autoslice import qixi_operator_exact_title_source_fact as source_fact
    from src.autoslice import qixi_post_correction_public_artifact_recovery as recovery
    from src.autoslice import qixi_post_correction_public_surface as public_surface

    monkeypatch.setattr(
        source_fact,
        "load_authority",
        lambda *_args, **_kwargs: type(
            "TitleAuthority", (),
            {"document": {"source_binding": {"public_surface_authority": {"relative_path": public_asset.name}}}}
        )(),
    )
    monkeypatch.setattr(public_surface, "validate_authority", lambda _value: {"public": True})
    observed: dict[str, object] = {}

    def capture(_document, **kwargs):
        observed.update(kwargs)
        raise ValueError("stop after predecessor replay")

    monkeypatch.setattr(recovery, "validate_committed_successor", capture)
    with pytest.raises(refresh.QixiTerminalEvidenceRefreshError, match="PREDECESSOR_INVALID"):
        refresh.validate_committed_refresh(repo_root=tmp_path)
    assert observed == {
        "sealed_chat_authority_bytes": sealed_chat,
        "allow_terminal_successor": True,
    }


def test_refreshed_boundary_receipt_matches_the_exact_final_grid(tmp_path) -> None:
    srt_path = tmp_path / "final.srt"
    text = _srt("第一句", "终句")
    srt_path.write_text(text, encoding="utf-8")
    cues = parse_srt_cues(text)
    review, reasons = bind_final_semantic_endpoint(
        semantic_review={
            "status": "PASS", "request_sha256": "sha256:" + "d" * 64,
            "cue_grid_sha256": cue_grid_sha256(cues),
            "recommended_end_cue_index": 2, "recommended_end_ms": 2_900,
        },
        cues=cues, closure_cue=cues[-1], snapped_end_ms=2_900, final_start_ms=0, final_end_ms=2_900,
    )
    assert reasons == []
    assert final_delivery_review_matches_srt(review, srt_path)
    srt_path.write_text(text.replace("终句", "漂移"), encoding="utf-8")
    assert not final_delivery_review_matches_srt(review, srt_path)


def test_refreshed_chat_hash_clears_uniform_host_speaker_hash_block(tmp_path) -> None:
    srt = tmp_path / "final.srt"
    srt.write_text(_srt("第一句", "终句"), encoding="utf-8")
    ass = tmp_path / "final.ass"
    ass.write_text(
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        "Dialogue: 0,0:00:01.00,0:00:01.90,Default,,0,0,0,,第一句\n"
        "Dialogue: 0,0:00:02.00,0:00:02.90,Default,,0,0,0,,终句\n",
        encoding="utf-8",
    )
    srt_hash = "sha256:" + hashlib.sha256(srt.read_bytes()).hexdigest()
    item = {"ass_path": ass.name, "ass_sha256": "sha256:" + hashlib.sha256(ass.read_bytes()).hexdigest(), "speaker_srt": srt.name, "speaker_srt_sha256": srt_hash}
    record = {"artifact_hashes": {"ass_sha256": item["ass_sha256"]}}
    stale = {"speaker_ass_path": None, "speaker_ass_sha256": None, "final_text_srt_sha256": "0" * 64, "final_speaker_srt_sha256": "0" * 64}
    assert "SUBTITLE_SPEAKER_SRT_CHAT_HASH_MISMATCH" in {
        issue.code for issue in audit_review_package_ass(root=tmp_path, item=item, portable_required=True, max_visual_lines=2, max_visual_line_chars=28, record=record, chat_authority=stale).issues
    }
    fresh = dict(stale, final_text_srt_sha256=srt_hash[7:], final_speaker_srt_sha256=srt_hash[7:])
    assert "SUBTITLE_SPEAKER_SRT_CHAT_HASH_MISMATCH" not in {
        issue.code for issue in audit_review_package_ass(root=tmp_path, item=item, portable_required=True, max_visual_lines=2, max_visual_line_chars=28, record=record, chat_authority=fresh).issues
    }
