"""Synthetic canaries for F16 judge evidence and F17 session surfaces."""

from __future__ import annotations

import ast
import hashlib
import inspect
import json
from pathlib import Path

import pytest

from src.autoslice.acoustic_witness_adjudication import adjudicate_with_witness
from src.autoslice.final_review_auditor import (
    adjudicate_context_finding,
    audit_final_subtitles,
    build_context_adjudication_request,
)
from src.autoslice.review_priority_candidates import (
    review_priority_candidate_counts,
    review_priority_candidates,
)
from src.autoslice.session_transcript_recurrence import (
    session_transcript_recurrence_candidates,
)
from src.autoslice.talk_lane import _compact_final_review_finding


def _srt(*texts: str) -> str:
    blocks = []
    for cue_index, cue_text in enumerate(texts, start=1):
        start = cue_index * 5
        blocks.append(
            f"{cue_index}\n00:00:{start:02d},000 --> 00:00:{start + 4:02d},000\n"
            f"{cue_text}"
        )
    return "\n\n".join(blocks) + "\n"


def _chat_context(surface: str) -> dict[str, object]:
    offsets = [5_500, 6_500, 10_500, 11_500, 12_500, 15_500, 16_500]
    return {
        "schema_version": "clip-context.v1",
        "structured_chat": [
            {
                "offset_ms": offset,
                "kind": "danmaku",
                "sender": f"viewer-{index}",
                "text": f"我听到的是{surface}",
                "source_event_id": f"event-{index}",
                "source_sha256": f"{index + 1:064x}",
            }
            for index, offset in enumerate(offsets)
        ],
    }


def _write_f16_fixture(
    tmp_path: Path, *, current_is_draft: bool
) -> tuple[str, dict[str, object], dict[str, object]]:
    draft_target = "她要找会打歌服的"
    hallucinated = "她要找会打高尔夫的"
    raw_srt = _srt("先看看衣柜", draft_target, "旁边也挂着打歌服")
    current_srt = _srt(
        "先看看衣柜",
        draft_target if current_is_draft else hallucinated,
        "旁边也挂着打歌服",
    )
    padded = tmp_path / "synthetic.mp4"
    padded.with_suffix(".asr_draft.srt").write_text(raw_srt, encoding="utf-8")
    padded.with_suffix(".fidelity-audit.json").write_text(
        json.dumps(
            {
                "schema_version": "subtitle-fidelity-audit.v2",
                "reverted": [
                    {
                        "cue_index": 2,
                        "draft": draft_target,
                        "attempted": hallucinated,
                        "kept": draft_target,
                        "violations": [
                            {
                                "op": "replace",
                                "draft_span": "打歌服",
                                "final_span": "打高尔夫",
                                "reason": "REPLACE_UNWITNESSED",
                            }
                        ],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    candidates = review_priority_candidates(padded, current_srt)
    kept_candidates = [
        row
        for row in candidates
        if (row.get("candidate_provenance") or {}).get("kind")
        == "draft_fidelity_kept"
    ]
    assert len(kept_candidates) == 1
    return current_srt, kept_candidates[0], _chat_context("打歌服")


def _normalize(
    srt_text: str,
    candidate: dict[str, object],
    *,
    clip_context: dict[str, object] | None = None,
) -> dict[str, object]:
    findings = audit_final_subtitles(
        srt_text,
        llm_call=lambda _prompt: json.dumps({"findings": []}),
        extract_json=json.loads,
        candidate_context=clip_context,
        extra_raw_findings=[candidate],
        prioritize_extra_raw_findings=True,
    )
    assert len(findings) == 1
    return findings[0]


def _observed(request: dict[str, object], heard_pinyin: str) -> dict[str, object]:
    serialized = json.dumps(request, ensure_ascii=False)
    assert "current_cue" not in serialized
    assert "proposed_cue" not in serialized
    return {
        "schema_version": "subtitle-span-acoustic-witness.v1",
        "request_sha256": request["request_sha256"],
        "status": "OBSERVED",
        "target_audible": True,
        "heard_pinyin": heard_pinyin,
        "uncertain_positions": [],
        "syllable_count": len(heard_pinyin.split()),
        "confidence": 0.95,
    }


def _assert_evidence_digest(evidence: dict[str, object]) -> None:
    payload = dict(evidence)
    observed = payload.pop("evidence_sha256")
    expected = "sha256:" + hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    assert observed == expected


def test_f16_canary_fresh_hallucination_loses_to_draft_and_chat(tmp_path):
    current_srt, candidate, clip_context = _write_f16_fixture(
        tmp_path, current_is_draft=False
    )
    assert candidate["proposed_full_cue"] == "她要找会打歌服的"
    assert candidate["candidate_origin"] == "draft_fidelity_kept"
    finding = _normalize(current_srt, candidate, clip_context=clip_context)
    evidence = build_context_adjudication_request(
        current_srt, finding, clip_context=clip_context
    )["closed_set_structured_evidence"]

    assert evidence["draft_fidelity"] == {
        "draft_fidelity_kept": True,
        "favored_candidate": "PROPOSED",
        "current_similarity": pytest.approx(0.705882),
        "proposed_similarity": 1.0,
        "kept_text_sha256": candidate["candidate_provenance"]["kept_text_sha256"],
    }
    assert evidence["neighbor_lexical_hits"]["cue_ids"] == [3]
    assert evidence["structured_chat_binding"]["bound_event_count"] == 7
    assert evidence["structured_chat_binding"]["evidence_cue_count"] == 3
    _assert_evidence_digest(evidence)

    def evidence_sensitive_judge(prompt: str) -> str:
        complete = all(
            token in prompt
            for token in (
                '"draft_fidelity_kept": true',
                '"favored_candidate": "PROPOSED"',
                '"surface": "打歌服"',
                '"bound_event_count": 7',
                '"cue_ids": [3]',
            )
        )
        return json.dumps({"choice": "PROPOSED" if complete else "CURRENT"})

    output, receipt = adjudicate_context_finding(
        current_srt,
        finding,
        entity_verifier=lambda request: _observed(
            request, "ta yao zhao hui da ge fu de"
        ),
        clip_context=clip_context,
        judge_llm_call=evidence_sensitive_judge,
    )

    assert "她要找会打歌服的" in output
    assert "她要找会打高尔夫的" not in output
    assert receipt["repaired"] is True
    assert receipt["request"]["closed_set_structured_evidence"] == evidence
    assert receipt["witness_judge"]["judge"]["closed_set_structured_evidence"] == evidence


def test_f16_judge_keeps_current_receipt_retains_recomputable_triplet(tmp_path):
    current_srt, candidate, clip_context = _write_f16_fixture(
        tmp_path, current_is_draft=True
    )
    assert candidate["proposed_full_cue"] == "她要找会打高尔夫的"
    finding = _normalize(current_srt, candidate, clip_context=clip_context)

    output, receipt = adjudicate_context_finding(
        current_srt,
        finding,
        entity_verifier=lambda request: _observed(
            request, "ta yao zhao hui da ge fu de"
        ),
        clip_context=clip_context,
        judge_llm_call=lambda _prompt: json.dumps({"choice": "CURRENT"}),
    )

    evidence = receipt["request"]["closed_set_structured_evidence"]
    assert output == current_srt
    assert receipt["policy_branch"] == "JUDGE_KEEPS_CURRENT"
    assert evidence["draft_fidelity"]["favored_candidate"] == "CURRENT"
    assert evidence["draft_fidelity"]["current_similarity"] == 1.0
    assert evidence["neighbor_lexical_hits"]["cue_ids"] == [3]
    assert evidence["structured_chat_binding"]["evidence_cue_count"] == 3
    assert receipt["witness_judge"]["closed_set_structured_evidence"] == evidence
    assert receipt["witness_judge"]["judge"]["closed_set_structured_evidence"] == evidence
    _assert_evidence_digest(evidence)


def test_f16_kept_current_signal_is_inherited_by_fresh_same_cue_proposal(tmp_path):
    current_srt, candidate, clip_context = _write_f16_fixture(
        tmp_path, current_is_draft=True
    )
    fresh_hallucination = "她要找会打高尔夫球的"
    findings = audit_final_subtitles(
        current_srt,
        llm_call=lambda _prompt: json.dumps(
            {
                "findings": [
                    {
                        "cue": 2,
                        "kind": "context",
                        "proposed_full_cue": fresh_hallucination,
                        "repair_class": "phonetic",
                        "why": "合成 fresh 常识化候选",
                    }
                ]
            },
            ensure_ascii=False,
        ),
        extract_json=json.loads,
        candidate_context=clip_context,
        extra_raw_findings=[candidate],
        prioritize_extra_raw_findings=True,
    )
    fresh = next(
        finding
        for finding in findings
        if finding.get("proposed_full_cue") == fresh_hallucination
    )

    assert fresh["draft_fidelity_kept_provenance"]["kept_candidate"] == "CURRENT"
    evidence = build_context_adjudication_request(
        current_srt, fresh, clip_context=clip_context
    )["closed_set_structured_evidence"]
    assert evidence["draft_fidelity"]["draft_fidelity_kept"] is True
    assert evidence["draft_fidelity"]["favored_candidate"] == "CURRENT"
    assert evidence["neighbor_lexical_hits"]["cue_ids"] == [3]
    assert evidence["structured_chat_binding"]["bound_event_count"] == 7


def test_f16_keep_existing_receipt_binds_neighbor_and_chat_counts():
    source = _srt("大家都说这么娇羞", "这么羞羞", "旁边又说娇羞")
    finding = {
        "cue_index": 2,
        "suspect": "羞羞",
        "suggestion": None,
        "proposed_full_cue": None,
        "repair_class": "disclosure_only",
        "evidence_cue_ids": [1, 3],
        "candidate_provenance": {
            "kind": "structured_chat_bound",
            "surface": "娇羞",
            "source_sha256": "a" * 64,
            "source_event_id": "synthetic-chat-event",
        },
        "why": "合成缺失候选",
    }
    prompts: list[str] = []

    def cpa(prompt: str) -> str:
        prompts.append(prompt)
        if "# 字幕缺失候选重建" in prompt:
            return json.dumps(
                {"status": "UNRESOLVED", "proposed_cue": "", "reason": "不猜"},
                ensure_ascii=False,
            )
        complete = all(
            token in prompt
            for token in (
                '"surface": "娇羞"',
                '"cue_ids": [1, 3]',
                '"evidence_cue_count": 2',
            )
        )
        if complete:
            return json.dumps(
                {"decision": "KEEP_EXISTING", "replacement_text": "", "reason": "证据闭集"},
                ensure_ascii=False,
            )
        return json.dumps(
            {
                "decision": "REPLACE_WITH_EXACT_TEXT",
                "replacement_text": "这么常识",
                "reason": "缺证据时故意走错侧",
            },
            ensure_ascii=False,
        )

    output, receipt = adjudicate_context_finding(
        source,
        finding,
        entity_verifier=lambda request: pytest.fail(
            f"KEEP_EXISTING must not request audio: {request}"
        ),
        judge_llm_call=cpa,
    )

    evidence = receipt["cpa_missing_proposal_convergence"][
        "closed_set_structured_evidence"
    ]
    assert output == source
    assert receipt["policy_branch"] == "JUDGE_KEEPS_CURRENT"
    assert receipt["request"]["closed_set_structured_evidence"] == evidence
    assert evidence["draft_fidelity"]["draft_fidelity_kept"] is False
    assert evidence["neighbor_lexical_hits"]["cue_count"] == 2
    assert evidence["structured_chat_binding"]["evidence_cue_count"] == 2
    assert len(prompts) == 2
    _assert_evidence_digest(evidence)


def _f17_pair() -> tuple[str, str]:
    return (
        _srt("开场", "妈妈说猪人来了", "又是猪人呀", "结束"),
        _srt("开场", "妈妈说主人来了", "又是主人呀", "结束"),
    )


def test_f17_canary_production_assembler_emits_session_only_candidates(tmp_path):
    raw_srt, current_srt = _f17_pair()
    padded = tmp_path / "session.mp4"
    padded.with_suffix(".asr_draft.srt").write_text(raw_srt, encoding="utf-8")

    candidates = review_priority_candidates(padded, current_srt)
    recurrence = [
        row
        for row in candidates
        if (row.get("candidate_provenance") or {}).get("kind")
        == "session_transcript_recurrence"
    ]

    assert [row["cue"] for row in recurrence] == [2, 3]
    assert review_priority_candidate_counts(candidates) == {
        "fidelity_candidate_count": 0,
        "session_transcript_recurrence_candidate_count": 2,
    }
    assert session_transcript_recurrence_candidates(
        raw_draft_srt=raw_srt, current_srt=current_srt, enabled=False
    ) == []
    for row in recurrence:
        provenance = row["candidate_provenance"]
        assert provenance["scope"] == "session"
        assert provenance["surface"] == "猪人"
        assert provenance["occurrence_cue_count"] == 2
        assert [position["cue_index"] for position in provenance["occurrence_positions"]] == [2, 3]
        assert provenance["mutation_authorized"] is False
        assert provenance["global_glossary_authorized"] is False


@pytest.mark.parametrize(
    ("raw_srt", "current_srt"),
    [
        (
            _srt("开场", "妈妈说猪人来了", "只是普通话"),
            _srt("开场", "妈妈说主人来了", "只是普通话"),
        ),
        (
            _srt("猪人来了", "二", "三", "四", "五", "六", "七", "又是猪人"),
            _srt("主人来了", "二", "三", "四", "五", "六", "七", "又是主人"),
        ),
        (
            _srt("开场", "妈妈说猪人来了", "又是猪人呀"),
            _srt("开场", "妈妈说主人来了", "又是猪人呀").replace(
                "00:00:10,000 --> 00:00:14,000",
                "00:00:10,100 --> 00:00:14,100",
            ),
        ),
    ],
    ids=["single-occurrence", "outside-window", "timing-mismatch"],
)
def test_f17_recurrence_rejects_unbound_evidence(raw_srt: str, current_srt: str):
    assert session_transcript_recurrence_candidates(
        raw_draft_srt=raw_srt, current_srt=current_srt
    ) == []


def test_f17_each_target_requires_observed_audio_then_closed_set_judge():
    raw_srt, current_srt = _f17_pair()
    candidate = session_transcript_recurrence_candidates(
        raw_draft_srt=raw_srt, current_srt=current_srt
    )[0]
    finding = _normalize(current_srt, candidate)
    request = build_context_adjudication_request(current_srt, finding)
    uncertain = {
        "schema_version": "subtitle-span-acoustic-witness.v1",
        "witness_protocol": "blind_pinyin",
        "status": "UNCERTAIN",
        "reason_code": "SYNTHETIC_AUDIO_UNAVAILABLE",
    }

    repaired, branch, gate_audit = adjudicate_with_witness(
        check_request=request,
        witness=uncertain,
        llm_call=lambda prompt: pytest.fail(f"judge must be gated: {prompt}"),
    )
    assert repaired is False
    assert branch == "SESSION_RECURRENCE_TARGET_ACOUSTIC_WITNESS_REQUIRED"
    assert gate_audit["session_transcript_recurrence_acoustic_gate"]["status"] == "BLOCK"

    output, receipt = adjudicate_context_finding(
        current_srt,
        finding,
        entity_verifier=lambda witness_request: _observed(
            witness_request, "ma ma shuo zhu ren lai le"
        ),
        judge_llm_call=lambda _prompt: json.dumps({"choice": "PROPOSED"}),
    )
    assert "妈妈说猪人来了" in output
    assert receipt["repaired"] is True
    assert receipt["request"]["candidate_provenance"]["occurrence_positions"] == (
        candidate["candidate_provenance"]["occurrence_positions"]
    )
    assert receipt["witness_judge"]["session_transcript_recurrence_acoustic_gate"]["status"] == "PASS"


def test_f17_receipt_compactor_keeps_occurrence_positions():
    raw_srt, current_srt = _f17_pair()
    candidate = session_transcript_recurrence_candidates(
        raw_draft_srt=raw_srt, current_srt=current_srt
    )[0]
    finding = _normalize(current_srt, candidate)

    compact = _compact_final_review_finding(finding)

    assert compact is not None
    assert compact["candidate_provenance"]["kind"] == "session_transcript_recurrence"
    assert compact["candidate_provenance"]["occurrence_positions"] == (
        candidate["candidate_provenance"]["occurrence_positions"]
    )


def test_f17_module_has_no_global_glossary_or_file_write_surface():
    import src.autoslice.session_transcript_recurrence as recurrence_module

    source = Path(recurrence_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_modules = {
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    imported_modules.update(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )
    called_attributes = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    parameters = inspect.signature(
        session_transcript_recurrence_candidates
    ).parameters

    assert not any("term_authority" in module for module in imported_modules)
    assert not {"write_text", "write_bytes", "touch", "unlink", "mkdir"} & called_attributes
    assert not {"glossary", "glossary_path", "profile", "profile_path"} & set(parameters)
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "open"
        for node in ast.walk(tree)
    )
