"""终审结转闭环契约（2026-07-25 7/24 六条死循环案）。"""

from __future__ import annotations

import hashlib
import json

from src.autoslice.final_review_carryover import (
    carryover_path,
    load_final_review_carryover,
    persist_final_review_carryover,
)
from src.autoslice.final_review_auditor import audit_final_subtitles


def _audit(findings):
    return {"schema_version": "final-review-audit.v2", "findings": findings}


def test_persist_keeps_only_acoustically_confirmed_repairs(tmp_path):
    path = carryover_path(tmp_path, "auto_x")
    count = persist_final_review_carryover(
        path,
        _audit(
            [
                {
                    "cue_index": 32,
                    "base_text_sha256": "a" * 64,
                    "kind": "context",
                    "suspect": "悄悄",
                    "replacement": "敲敲",
                    "proposed_full_cue": "就去敲敲那些结晶，看看",
                    "why": "语境应是敲结晶",
                    "exact_release_adjudication": {"repaired": True},
                },
                {
                    "cue_index": 52,
                    "kind": "context",
                    "suspect": "刮",
                    "proposed_full_cue": "还能稍微刮一点",
                    "why": "keep-current",
                    "exact_release_adjudication": {"repaired": False},
                },
            ]
        ),
    )
    assert count == 1
    rows = load_final_review_carryover(path)
    assert len(rows) == 1
    row = rows[0]
    assert row["cue"] == 32
    assert row["base_text_sha256"] == "a" * 64
    assert row["suspect"] == "悄悄"
    assert "终审结转" in row["why"]


def test_persist_empty_removes_stale_file(tmp_path):
    path = carryover_path(tmp_path, "auto_x")
    path.write_text("{}", encoding="utf-8")
    assert persist_final_review_carryover(path, _audit([])) == 0
    assert not path.exists()
    assert load_final_review_carryover(path) == []


def test_provider_unavailable_preserves_unconsumed_prior_carryover(tmp_path):
    path = carryover_path(tmp_path, "auto_x")
    prior = {
        "schema_version": "final-review-carryover.v1",
        "findings": [
            {
                "cue": 27,
                "suspect": "早上",
                "proposed_full_cue": "谢谢如果世上没有早……",
            }
        ],
    }
    path.write_text(json.dumps(prior, ensure_ascii=False), encoding="utf-8")
    before = path.read_bytes()

    count = persist_final_review_carryover(
        path,
        {
            **_audit([]),
            "status": "AUDITOR_UNAVAILABLE",
            "correction_pass": {
                "status": "AUDITOR_UNAVAILABLE",
                "discovery": {"status": "AUDITOR_UNAVAILABLE"},
            },
        },
    )

    assert count == 1
    assert path.read_bytes() == before


def test_incomplete_correction_merges_prior_and_new_exact_carryover(tmp_path):
    path = carryover_path(tmp_path, "auto_x")
    path.write_text(
        json.dumps(
            {
                "schema_version": "final-review-carryover.v1",
                "findings": [
                    {
                        "cue": 27,
                        "suspect": "早上",
                        "proposed_full_cue": "谢谢如果世上没有早……",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    count = persist_final_review_carryover(
        path,
        {
            **_audit(
                [
                    {
                        "cue_index": 31,
                        "suspect": "粉丝灯牌",
                        "proposed_full_cue": "如果世上没有早起的粉丝团灯牌",
                        "exact_release_adjudication": {"repaired": True},
                    }
                ]
            ),
            "correction_pass": {
                "status": "AUDITOR_UNAVAILABLE",
                "discovery": {"status": "AUDITOR_UNAVAILABLE"},
            },
            "correction_mutation_authority": {
                "status": "BLOCK",
                "failures": [
                    {"reason_code": "CORRECTION_DISCOVERY_INCOMPLETE"}
                ],
            },
        },
    )

    assert count == 2
    rows = load_final_review_carryover(path)
    assert {(row["cue"], row["suspect"]) for row in rows} == {
        (27, "早上"),
        (31, "粉丝灯牌"),
    }


def test_glossary_insertion_carryover_preserves_raw_provenance(tmp_path):
    path = carryover_path(tmp_path, "auto_909")
    count = persist_final_review_carryover(
        path,
        _audit(
            [
                {
                    "cue_index": 2,
                    "kind": "entity",
                    "suspect": "",
                    "suggestion": "团",
                    "proposed_full_cue": "如果世上没有早起的粉丝团灯牌",
                    "repair_class": "source_backed_entity",
                    "candidate_provenance": {
                        "kind": "glossary",
                        "surface": "粉丝团灯牌",
                    },
                    "why": "平台固定礼物名漏字",
                    "exact_release_adjudication": {"repaired": True},
                }
            ]
        ),
    )

    assert count == 1
    rows = load_final_review_carryover(path)
    assert rows[0]["replacement"] == "团"
    assert rows[0]["source_surface"] == "粉丝团灯牌"

    findings = audit_final_subtitles(
        "1\n00:00:00,000 --> 00:00:01,000\n谢谢\n\n"
        "2\n00:00:01,000 --> 00:00:03,000\n"
        "如果世上没有早起的粉丝灯牌\n",
        llm_call=lambda _prompt: '{"findings":[]}',
        extract_json=lambda value: json.loads(value),
        glossary_text="- 粉丝团灯牌",
        extra_raw_findings=rows,
    )
    assert len(findings) == 1
    assert findings[0]["suspect"] == ""
    assert findings[0]["suggestion"] == "团"
    assert findings[0]["candidate_provenance"] == {
        "kind": "glossary",
        "surface": "粉丝团灯牌",
    }


def test_carryover_recovers_cpa_target_hidden_by_large_delta_rejection(tmp_path):
    path = carryover_path(tmp_path, "auto_909")
    current = "就是面部的时候没有什么制作这个机体"
    proposed = "就是制作这个机体"
    count = persist_final_review_carryover(
        path,
        _audit(
            [
                {
                    "cue_index": 11,
                    "base_text_sha256": hashlib.sha256(
                        current.encode("utf-8")
                    ).hexdigest(),
                    "kind": "context",
                    "suspect": "面部的时候没有什么",
                    "proposed_full_cue": None,
                    "suggestion_rejected_reason": "EDIT_LENGTH_DELTA_TOO_LARGE",
                    "exact_release_adjudication": {
                        "schema_version": "subtitle-span-adjudication.v1",
                        "status": "OBSERVED",
                        "decision_authority": "CPA_JUDGE",
                        "repaired": True,
                        "timing_immutable": True,
                        "mutation_authority": {
                            "schema_version": (
                                "subtitle-correction-mutation-authority.v1"
                            ),
                            "status": "PASS",
                        },
                        "request": {
                            "schema_version": (
                                "subtitle-span-acoustic-check-request.v1"
                            ),
                            "current_cue": current,
                            "proposed_cue": proposed,
                            "matched_start_ms": 0,
                            "matched_end_ms": 1_000,
                        },
                        "witness_judge": {
                            "judge": {
                                "status": "JUDGED",
                                "choice": "PROPOSED",
                            }
                        },
                    },
                }
            ]
        ),
    )

    assert count == 1
    row = load_final_review_carryover(path)[0]
    assert row["proposed_full_cue"] == proposed
    assert row["exact_release_adjudication"]["request"][
        "proposed_cue"
    ] == proposed


def test_clean_exact_scan_preserves_unconsumed_remapped_carryover(tmp_path):
    path = carryover_path(tmp_path, "auto_909")
    prior = {
        "schema_version": "final-review-carryover.v1",
        "findings": [
            {
                "cue": 11,
                "base_text_sha256": "a" * 64,
                "kind": "context",
                "suspect": "面部的时候没有什么",
                "proposed_full_cue": "就是制作这个机体",
            }
        ],
    }
    path.write_text(json.dumps(prior, ensure_ascii=False), encoding="utf-8")
    clean_exact = {
        **_audit([]),
        "status": "CLEAN",
        "correction_pass": {
            "findings": [
                {
                    "cue_index": 15,
                    "base_text_sha256": "a" * 64,
                    "kind": "context",
                    "suspect": "面部的时候没有什么",
                    "proposed_full_cue": "就是制作这个机体",
                    "carryover_replay_remap": {
                        "schema_version": "final-review-carryover-remap.v1",
                        "status": "PASS",
                        "basis": "base_text_sha256",
                    },
                    "routed": "disclosure",
                }
            ]
        },
    }

    assert persist_final_review_carryover(path, clean_exact) == 1
    assert load_final_review_carryover(path) == prior["findings"]


def test_carryover_not_shadowed_by_different_empty_span_proposal(tmp_path):
    path = carryover_path(tmp_path, "auto_909")
    path.write_text(
        json.dumps(
            {
                "schema_version": "final-review-carryover.v1",
                "findings": [
                    {
                        "cue": 1,
                        "kind": "entity",
                        "suspect": "",
                        "replacement": "团",
                        "proposed_full_cue": "粉丝团灯牌",
                        "repair_class": "source_backed_entity",
                        "source_surface": "粉丝团灯牌",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    findings = audit_final_subtitles(
        "1\n00:00:00,000 --> 00:00:01,000\n粉丝灯牌\n",
        llm_call=lambda _prompt: json.dumps(
            {
                "findings": [
                    {
                        "cue": 1,
                        "kind": "entity",
                        "suspect": "",
                        "replacement": "牌",
                        "proposed_full_cue": "粉丝牌灯牌",
                        "repair_class": "source_backed_entity",
                        "source_surface": "粉丝牌灯牌",
                    }
                ]
            },
            ensure_ascii=False,
        ),
        extract_json=lambda value: json.loads(value),
        glossary_text="- 粉丝团灯牌\n- 粉丝牌灯牌",
        extra_raw_findings=load_final_review_carryover(path),
    )
    assert {
        row["proposed_full_cue"] for row in findings
    } == {"粉丝团灯牌", "粉丝牌灯牌"}


def test_carryover_replays_by_text_hash_when_padded_cue_index_shifted():
    base_text = "所以剩一点给我"
    findings = audit_final_subtitles(
        "1\n00:00:00,000 --> 00:00:01,000\n前置一\n\n"
        "2\n00:00:01,000 --> 00:00:02,000\n前置二\n\n"
        f"3\n00:00:02,000 --> 00:00:03,000\n{base_text}\n\n"
        "4\n00:00:03,000 --> 00:00:04,000\n后文\n",
        llm_call=lambda _prompt: '{"findings":[]}',
        extract_json=json.loads,
        extra_raw_findings=[
            {
                "cue": 1,
                "base_text_sha256": hashlib.sha256(
                    base_text.encode("utf-8")
                ).hexdigest(),
                "kind": "context",
                "suspect": "剩一点给",
                "replacement": "顺便带",
                "proposed_full_cue": "所以顺便带我",
                "repair_class": "phonetic",
                "evidence_cue_ids": [2],
            }
        ],
    )

    assert findings[0]["cue_index"] == 3
    assert findings[0]["evidence_cue_ids"] == [4]
    assert findings[0]["carryover_replay_remap"] == {
        "schema_version": "final-review-carryover-remap.v1",
        "status": "PASS",
        "basis": "base_text_sha256",
        "from_cue": 1,
        "to_cue": 3,
        "evidence_delta": 2,
    }


def test_legacy_carryover_replays_only_when_suspect_owner_is_unique():
    source = (
        "1\n00:00:00,000 --> 00:00:01,000\n前置\n\n"
        "2\n00:00:01,000 --> 00:00:02,000\n所以剩一点给我\n"
    )
    row = {
        "cue": 1,
        "kind": "context",
        "suspect": "剩一点给",
        "replacement": "顺便带",
        "proposed_full_cue": "所以顺便带我",
        "repair_class": "phonetic",
    }
    findings = audit_final_subtitles(
        source,
        llm_call=lambda _prompt: '{"findings":[]}',
        extract_json=json.loads,
        extra_raw_findings=[row],
    )
    assert findings[0]["cue_index"] == 2
    assert (
        findings[0]["carryover_replay_remap"]["basis"]
        == "unique_legacy_suspect"
    )

    ambiguous = source + (
        "\n3\n00:00:02,000 --> 00:00:03,000\n又说剩一点给我\n"
    )
    assert audit_final_subtitles(
        ambiguous,
        llm_call=lambda _prompt: '{"findings":[]}',
        extract_json=json.loads,
        extra_raw_findings=[row],
    ) == []


def test_load_rejects_malformed_payload(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("not json", encoding="utf-8")
    assert load_final_review_carryover(path) == []
    path.write_text(json.dumps({"schema_version": "other", "findings": [{}]}), encoding="utf-8")
    assert load_final_review_carryover(path) == []
