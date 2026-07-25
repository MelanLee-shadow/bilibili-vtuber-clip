"""终审结转闭环契约（2026-07-25 7/24 六条死循环案）。"""

from __future__ import annotations

import json
from pathlib import Path

from src.autoslice.final_review_carryover import (
    CARRYOVER_ORIGIN,
    carryover_path,
    load_final_review_carryover,
    merge_carryover_findings,
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
    assert row["origin"] == CARRYOVER_ORIGIN
    assert "终审结转" in row["why"]


def test_persist_empty_removes_stale_file(tmp_path):
    path = carryover_path(tmp_path, "auto_x")
    path.write_text("{}", encoding="utf-8")
    assert persist_final_review_carryover(path, _audit([])) == 0
    assert not path.exists()
    assert load_final_review_carryover(path) == []


def test_load_rejects_malformed_payload(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("not json", encoding="utf-8")
    assert load_final_review_carryover(path) == []
    path.write_text(json.dumps({"schema_version": "other", "findings": [{}]}), encoding="utf-8")
    assert load_final_review_carryover(path) == []


def test_merge_dedupes_by_cue_and_suspect_fresh_wins(tmp_path):
    fresh = [{"cue": 32, "suspect": "悄悄", "why": "fresh"}]
    carry = [
        {"cue": 32, "suspect": "悄悄", "why": "carry", "origin": CARRYOVER_ORIGIN},
        {"cue": 51, "suspect": "节奏", "why": "carry", "origin": CARRYOVER_ORIGIN},
    ]
    merged = merge_carryover_findings(fresh, carry)
    assert len(merged) == 2
    assert merged[0]["why"] == "fresh"
    assert merged[1]["cue"] == 51
