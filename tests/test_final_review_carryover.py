"""终审结转闭环契约（2026-07-25 7/24 六条死循环案）。"""

from __future__ import annotations

import json
from pathlib import Path

from src.autoslice.final_review_carryover import (
    carryover_path,
    load_final_review_carryover,
    persist_final_review_carryover,
)


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


def test_load_rejects_malformed_payload(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("not json", encoding="utf-8")
    assert load_final_review_carryover(path) == []
    path.write_text(json.dumps({"schema_version": "other", "findings": [{}]}), encoding="utf-8")
    assert load_final_review_carryover(path) == []
