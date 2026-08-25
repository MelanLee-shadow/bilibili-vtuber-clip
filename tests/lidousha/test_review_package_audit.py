from __future__ import annotations

import json
import hashlib
from pathlib import Path

from PIL import Image
import pytest

import scripts.audit_lidousha_review_package as review_package_audit

from scripts.audit_lidousha_review_package import (
    _audit_policy_fingerprint,
    _add_issue,
    _audit_story_bound_cover,
    _contained_package_artifact,
    audit_package,
)
from src.autoslice.review_package_owner_audit import (
    audit_source_truth_owner_attestations,
)
from src.autoslice.boundary_semantic_review import (
    cue_grid_sha256,
    semantic_review_sha256,
)
from src.autoslice.clip_context import build_clip_context
from src.autoslice.cover_route_evidence import (
    build_cover_route_decision,
    record_cover_route_execution,
)
from src.autoslice.cover_text_pixel_evidence import (
    materialize_rendered_text_pixel_evidence,
)
from src.autoslice.cover_title_rendering import (
    SCHEMA_VERSION as COVER_TITLE_RENDER_SPEC_SCHEMA,
    render_title_layer,
    sha256_file,
)
from src.autoslice.selection_scorecard import normalize_selection_scorecard
from src.autoslice.jingting_chunker import parse_srt_cues
from src.autoslice.review_package_ass_audit import audit_review_package_ass
from src.autoslice.review_package_boundary_contract import (
    _audit_redelivery_time_domain,
    audit_boundary_contract,
)
from src.autoslice.producer_boundary_owner_contract import (
    freeze_required_boundary_owner_contract,
    frozen_boundary_owner_contract_sha256,
)
from src.autoslice.qixi_terminal_reconciliation import (
    reconcile_terminal_baseline_replay_supersessions,
)
from src.autoslice.recovery_title_authority import (
    build_recovery_publication_authorities,
    expected_recovery_publish_title,
)
from src.autoslice.source_subtitle_truth import (
    candidate_boundary_owner_scope,
)
from src.autoslice.source_fact_review import review_and_repair_source_facts
from src.autoslice.story_contract import build_story_contract


REPO_ROOT = Path(__file__).resolve().parents[2]
COVER_FONT = REPO_ROOT / "assets/lidousha/fonts/ZCOOLKuaiLe-Regular.ttf"


@pytest.mark.parametrize(
    ("stem", "bad_start", "bad_end", "good_start", "good_end"),
    [
        ("auto_113028_1271_1328", 1_261_170, 1_376_550, 1_270_920, 1_328_694),
        ("auto_113028_1602_1698", 1_592_760, 1_746_900, 1_602_510, 1_699_300),
    ],
)
def test_package_audit_rejects_old_delivery_local_double_projection(
    tmp_path: Path,
    stem: str,
    bad_start: int,
    bad_end: int,
    good_start: int,
    good_end: int,
) -> None:
    baseline = (
        REPO_ROOT
        / "assets/lidousha/reviewed_subtitle_baselines"
        / f"{stem}.reviewed.srt"
    )
    subtitle = tmp_path / f"{stem}.srt"
    subtitle.write_text(
        "1\n00:00:00,000 --> 00:00:00,330\nold double projection\n",
        encoding="utf-8",
    )
    record = {
        "redelivery_baseline": {
            "current_source_interval": {
                "absolute_source_start_ms": bad_start,
                "absolute_source_end_ms": bad_end,
            }
        }
    }
    issues: list[dict] = []
    _audit_redelivery_time_domain(
        issue_adder=lambda rows, code, **fields: rows.append({"code": code, **fields}),
        issues=issues,
        stem=stem,
        record_path=tmp_path / f"{stem}.record.json",
        subtitle_path=subtitle,
        record=record,
    )
    assert {issue["code"] for issue in issues} == {
        "REDELIVERY_BASELINE_DELIVERY_INTERVAL_MISMATCH",
        "REDELIVERY_BASELINE_DELIVERY_LOCAL_SUBTITLE_MISMATCH",
    }

    subtitle.write_bytes(baseline.read_bytes())
    record["redelivery_baseline"]["current_source_interval"] = {
        "absolute_source_start_ms": good_start,
        "absolute_source_end_ms": good_end,
    }
    clean: list[dict] = []
    _audit_redelivery_time_domain(
        issue_adder=lambda rows, code, **fields: rows.append({"code": code, **fields}),
        issues=clean,
        stem=stem,
        record_path=tmp_path / f"{stem}.record.json",
        subtitle_path=subtitle,
        record=record,
    )
    assert clean == []


def test_contained_package_artifact_rejects_symlinked_parent(
    tmp_path: Path,
) -> None:
    root = tmp_path / "package"
    root.mkdir()
    real = root / "real"
    real.mkdir()
    (real / "speaker.json").write_text("{}\n", encoding="utf-8")
    (root / "linked").symlink_to(real, target_is_directory=True)

    with pytest.raises(ValueError, match="not package-contained"):
        _contained_package_artifact(
            root,
            "linked/speaker.json",
            label="speaker finalization manifest",
        )


def test_contained_package_artifact_never_reads_existing_absolute_source(
    tmp_path: Path,
) -> None:
    root = tmp_path / "package"
    root.mkdir()
    external = tmp_path / "producer" / "speaker.json"
    external.parent.mkdir()
    external.write_text('{"source":"external"}\n', encoding="utf-8")

    with pytest.raises(ValueError, match="not package-contained"):
        _contained_package_artifact(
            root,
            str(external),
            label="speaker finalization manifest",
        )

    packaged = root / external.name
    packaged.write_text('{"source":"package"}\n', encoding="utf-8")
    assert _contained_package_artifact(
        root,
        str(external),
        label="speaker finalization manifest",
    ) == packaged


def test_package_audit_rejects_814_shaped_full_title_thumbnail() -> None:
    cover_text = (
        "SC称转发佐伯沙弥香生日信息能拿菲尔兹奖，小李追问“我也磕原点组怎么没有”，难道磕错了？"
    )
    issues: list[dict] = []
    _audit_story_bound_cover(
        issues=issues,
        stem="auto_225056_814_887",
        record_path=Path("auto_225056_814_887.record.json"),
        record={
            "publish_staging": {"cover_text": cover_text},
            "cover_generation": {
                "cover_text": cover_text,
                "cover_text_mode": "full",
                "rendered_lines": [
                    "SC称转发",
                    "佐伯沙弥香生",
                    "日信息能拿",
                    "菲尔兹奖，小",
                    "李追问“我",
                    "也磕原点组怎",
                    "么没有”，",
                    "难道磕错了？",
                ],
                "art_direction": {
                    "cover_punch_semantic_review": {
                        "status": "FAILED",
                        "reason_code": "CPA_PUNCH_SEMANTIC_REVIEW_REJECTED",
                    }
                },
            },
        },
        story_contract={"selection_hook": "转发生日信息能拿菲尔兹奖，小李追问为何自己没有。"},
        required=True,
    )

    codes = {issue["code"] for issue in issues}
    assert "COVER_PUNCH_REQUIRED_FOR_THUMBNAIL" in codes
    assert "COVER_THUMBNAIL_TEXT_UNREADABLE" in codes


def test_package_audit_rejects_exact_964_manual_three_line_double_hook() -> None:
    cover_text = "长沙人李豆沙亲自打假“长沙大香肠”，话还没说完，弹幕又提议把技能叫“李姐拉拉”"
    issues: list[dict] = []
    _audit_story_bound_cover(
        issues=issues,
        stem="auto_152944_964_1091",
        record_path=Path("auto_152944_964_1091.record.json"),
        record={
            "publish_staging": {
                "title_source": "ivan_manual_override",
                "cover_text": cover_text,
            },
            "cover_generation": {
                "cover_text": cover_text,
                "cover_text_mode": "full",
                "rendered_lines": [
                    "长沙人李豆沙亲自打假",
                    "“长沙大香肠”，话还没说完，弹幕",
                    "又提议把技能叫“李姐拉拉”",
                ],
                "cover_punch_allowed": False,
                "art_direction": {
                    "is_song": False,
                    "cover_punch_semantic_review": {},
                },
            },
        },
        story_contract={"selection_hook": "长沙人李豆沙打假长沙大香肠。"},
        required=True,
    )

    codes = {issue["code"] for issue in issues}
    assert "COVER_PUNCH_REQUIRED_FOR_THUMBNAIL" in codes
    assert "COVER_THUMBNAIL_TEXT_UNREADABLE" in codes
    assert "COVER_FULL_TEXT_CONTRACT_MISSING_OR_INVALID" in codes


def _source_fact_keep_receipt(
    *,
    hook: str,
    title: str,
    transcript: str,
    context: str,
    scorecard: object,
) -> dict:
    return review_and_repair_source_facts(
        selection_hook=hook,
        title=title,
        final_transcript=transcript,
        clip_context_prompt=context,
        selection_scorecard=scorecard,
        llm_call=lambda _prompt: json.dumps(
            {
                "schema_version": "lidousha-source-fact-review.v1",
                "status": "KEEP",
                "final_selection_hook": hook,
                "final_title": title,
                "supported_by": [
                    "final_transcript",
                    "same_clip_context",
                ],
                "changed_surfaces": [],
                # F12：判项必填；无说话人转写 -> UNVERIFIABLE 车道，留空数组。
                "addressee_attribution": [],
                "selection_scorecard_review": {
                    "status": "NOT_NEEDED",
                    "reason": "selection hook remains unchanged",
                },
                "summary": "最终字幕和同片上下文支持现有派生事实。",
            },
            ensure_ascii=False,
        ),
    )


def _source_truth_audit_fixture(
    *,
    applied: list[dict] | None = None,
    satisfied: list[dict] | None = None,
    failures: list[dict] | None = None,
    status: str | None = None,
) -> dict:
    applied_rows = applied or []
    satisfied_rows = satisfied or []
    failure_rows = failures or []
    if status is None:
        status = (
            "FAILED"
            if failure_rows
            else "APPLIED"
            if applied_rows
            else "ALREADY_SATISFIED"
            if satisfied_rows
            else "NO_RELEVANT_INTERVAL"
        )
    ledger = REPO_ROOT / "assets/lidousha/subtitle_truth_ledger.v1.json"
    return {
        "schema_version": "source-subtitle-truth-audit.v1",
        "status": status,
        "ledger_path": str(ledger),
        "ledger_sha256": ("sha256:" + hashlib.sha256(ledger.read_bytes()).hexdigest()),
        "applied": applied_rows,
        "satisfied": satisfied_rows,
        "failures": failure_rows,
    }


def test_optional_source_truth_does_not_require_final_owner_attestation():
    issues: list[dict] = []
    chat = {
        "source_subtitle_truth_audit": _source_truth_audit_fixture(
            applied=[
                {
                    "truth_id": "optional-mop-up",
                    "required": False,
                }
            ],
        )
    }

    audit_source_truth_owner_attestations(
        issue_adder=_add_issue,
        issues=issues,
        stem="optional",
        chat_authority_path=None,
        chat_authority=chat,
        record_path=None,
        record={},
    )

    assert "SOURCE_TRUTH_FINAL_OWNER_ATTESTATION_MISSING" not in {issue["code"] for issue in issues}

    chat["source_subtitle_truth_audit"]["applied"][0]["required"] = True
    required_issues: list[dict] = []
    audit_source_truth_owner_attestations(
        issue_adder=_add_issue,
        issues=required_issues,
        stem="required",
        chat_authority_path=None,
        chat_authority=chat,
        record_path=None,
        record={},
    )
    assert "SOURCE_TRUTH_FINAL_OWNER_ATTESTATION_MISSING" in {
        issue["code"] for issue in required_issues
    }


def _frozen_owner_fixture(
    *,
    owners: list[dict] | None = None,
    candidate_id: str = "owner-audit-fixture",
) -> dict:
    spec = {
        "candidate_id": candidate_id,
        "semantic_start_ms": 0,
        "semantic_end_ms": 4_000,
        "pieces": [{"start_ms": 0, "end_ms": 10_000}],
        "boundary_repair_extend_cap_ms": 30_000,
    }
    durations = [10_000]
    owner_scope = candidate_boundary_owner_scope(
        spec=spec,
        durations=durations,
    )
    normalized_owners = []
    for owner in owners or []:
        normalized = dict(owner)
        if normalized.get("owner_kind") == "source_subtitle_truth":
            normalized.setdefault(
                "owner_scope_sha256",
                owner_scope["scope_sha256"],
            )
        normalized_owners.append(normalized)
    chat: dict = {}
    freeze_required_boundary_owner_contract(
        spec=spec,
        durations=durations,
        chat_authority_audit=chat,
        required_boundary_owners=normalized_owners,
    )
    return chat["frozen_boundary_owner_contract"]


def _owner_attestation_codes(
    *,
    truth_rows: list[dict],
    frozen: dict,
    baseline_applied: bool = False,
    record_candidate_id: str | None = None,
    truth_owner_overrides: dict | None = None,
    source_truth_audit_override: dict | None = None,
    chat_rows_by_key: dict[str, list[dict]] | None = None,
    qixi_terminal_projection_authority: dict | None = None,
    delivery_start_ms: int | None = None,
) -> set[str]:
    classified_truth_rows = []
    final_delivery_rows = 0
    context_only_rows = 0
    straddling_rows = 0
    required_windows = 0
    for original in truth_rows:
        row = dict(original)
        windows = row["local_windows"]
        fully_inside = all(
            0 <= window["start_ms"] and window["end_ms"] <= 4_000 for window in windows
        )
        fully_outside = all(
            window["end_ms"] <= 0 or window["start_ms"] >= 4_000 for window in windows
        )
        if fully_inside:
            final_delivery_rows += 1
            required_windows += len(windows)
            row["final_owner_scope"] = "FINAL_DELIVERY"
            row["final_owner_verified"] = True
            row["final_owner_windows"] = [
                {
                    **window,
                    "text_ok": True,
                    "speaker_ok": True,
                }
                for window in windows
            ]
            row["final_owner_contract"] = {
                "text_ok": True,
                "speaker_ok": True,
            }
        elif fully_outside:
            context_only_rows += 1
            row["final_owner_scope"] = "CONTEXT_ONLY_OUTSIDE_FINAL_DELIVERY"
            row["final_owner_scope_reason"] = (
                "ALL_EFFECTIVE_OWNER_WINDOWS_OUTSIDE_HALF_OPEN_FINAL_INTERVAL"
            )
            row["final_owner_verified"] = False
            row["final_owner_windows"] = [
                {
                    **window,
                    "delivery_relation": (
                        "OUTSIDE_AFTER_FINAL_DELIVERY"
                        if window["start_ms"] >= 4_000
                        else "OUTSIDE_BEFORE_FINAL_DELIVERY"
                    ),
                }
                for window in windows
            ]
        else:
            straddling_rows += 1
            row["final_owner_scope"] = "STRADDLES_FINAL_DELIVERY"
            row["final_owner_verified"] = False
            row["final_owner_windows"] = [dict(window) for window in windows]
        classified_truth_rows.append(row)
    truth_owner = {
        "status": "PASS",
        "final_delivery_interval": {
            "timeline": "padded_source_local_ms",
            "interval_semantics": "half_open",
            "start_ms": 0,
            "end_ms": 4_000,
        },
        "required_truth_row_count": (final_delivery_rows + straddling_rows),
        "required_truth_ids": [
            str(row.get("truth_id") or "")
            for row in classified_truth_rows
            if row.get("final_owner_scope") in {"FINAL_DELIVERY", "STRADDLES_FINAL_DELIVERY"}
        ],
        "context_only_truth_row_count": context_only_rows,
        "context_only_truth_ids": [
            str(row.get("truth_id") or "")
            for row in classified_truth_rows
            if row.get("final_owner_scope") == "CONTEXT_ONLY_OUTSIDE_FINAL_DELIVERY"
        ],
        "context_only_truth_evidence": [
            {
                "truth_id": str(row.get("truth_id") or ""),
                "boundary_role": str(row.get("boundary_role") or ""),
                "final_owner_scope": row.get("final_owner_scope"),
                "final_owner_windows": row.get("final_owner_windows"),
            }
            for row in classified_truth_rows
            if row.get("final_owner_scope") == "CONTEXT_ONLY_OUTSIDE_FINAL_DELIVERY"
        ],
        "optional_truth_row_count": 0,
        "straddling_truth_row_count": straddling_rows,
        "required_window_count": required_windows,
        "failures": [],
    }
    truth_owner.update(truth_owner_overrides or {})
    chat = {
        "source_subtitle_truth_audit": (
            source_truth_audit_override
            if source_truth_audit_override is not None
            else _source_truth_audit_fixture(
                applied=classified_truth_rows,
            )
        ),
        "final_source_truth_owner_verification": truth_owner,
        "final_required_legacy_decision_count": 0,
        "final_required_source_truth_owner_count": required_windows,
        "final_required_redelivery_baseline_owner_count": (1 if baseline_applied else 0),
        "final_required_decision_count": (required_windows + (1 if baseline_applied else 0)),
        "frozen_boundary_owner_contract": frozen,
        "final_boundary_required_exclusion_count": 0,
    }
    chat.update(chat_rows_by_key or {})
    if baseline_applied:
        chat["redelivery_subtitle_baseline_audit"] = {
            "status": "APPLIED",
        }
        chat["final_redelivery_baseline_owner_verification"] = {
            "status": "PASS",
            "required_mapping_count": 1,
        }
    owners = frozen["owners"]
    record = {
        "boundary_audit": {
            "frozen_required_boundary_owner_count": len(owners),
            "frozen_required_boundary_owners": owners,
            "required_boundary_owner_verification": {
                "status": "PASS",
                "failures": [],
            },
            "delivery_coverage_verification": {
                "status": "PASS",
                "failure": None,
            },
        }
    }
    if delivery_start_ms is not None:
        record["boundary_audit"]["final_start_ms"] = delivery_start_ms
    if record_candidate_id is not None:
        record["story_contract"] = {
            "candidate_id": record_candidate_id,
        }
    issues: list[dict] = []
    audit_source_truth_owner_attestations(
        issue_adder=_add_issue,
        issues=issues,
        stem="owner-audit",
        chat_authority_path=None,
        chat_authority=chat,
        record_path=None,
        record=record,
        qixi_terminal_projection_authority=qixi_terminal_projection_authority,
    )
    return {issue["code"] for issue in issues}


def _truth_row(
    truth_id: str,
    *,
    start_ms: int,
    end_ms: int,
    boundary_role: str = "story_content",
) -> dict:
    return {
        "truth_id": truth_id,
        "required": True,
        "boundary_role": boundary_role,
        "source_start_ms": 100_000 + start_ms,
        "source_end_ms": 100_000 + end_ms,
        "local_windows": [{"start_ms": start_ms, "end_ms": end_ms}],
    }


def _truth_owner(row: dict) -> dict:
    return {
        "owner_kind": "source_subtitle_truth",
        "owner_id": row["truth_id"],
        "required": True,
        "source_start_ms": row["source_start_ms"],
        "source_end_ms": row["source_end_ms"],
        "local_windows": row["local_windows"],
    }


def _story_owner(
    owner_kind: str,
    owner_id: str,
    *,
    start_ms: int,
    end_ms: int,
) -> dict:
    return {
        "owner_kind": owner_kind,
        "owner_id": owner_id,
        "required": True,
        "local_windows": [{"start_ms": start_ms, "end_ms": end_ms}],
    }


def test_context_truth_remains_required_text_but_not_boundary_owner():
    context_truth = _truth_row(
        "later-candidate-truth",
        start_ms=6_000,
        end_ms=7_000,
    )

    codes = _owner_attestation_codes(
        truth_rows=[context_truth],
        frozen=_frozen_owner_fixture(),
    )

    assert "FROZEN_BOUNDARY_OWNER_CONTRACT_MISSING_OR_INVALID" not in codes
    assert "SOURCE_TRUTH_FINAL_OWNER_ATTESTATION_MISSING" not in codes
    assert "FINAL_AUTHORITY_DECISION_COVERAGE_EMPTY" not in codes


def test_candidate_scope_truth_requires_exact_frozen_owner():
    story_truth = _truth_row(
        "current-story-truth",
        start_ms=1_000,
        end_ms=2_000,
    )
    valid_frozen = _frozen_owner_fixture(owners=[_truth_owner(story_truth)])
    assert "FROZEN_BOUNDARY_OWNER_CONTRACT_MISSING_OR_INVALID" not in (
        _owner_attestation_codes(
            truth_rows=[story_truth],
            frozen=valid_frozen,
        )
    )

    missing_frozen = _frozen_owner_fixture()
    assert "FROZEN_BOUNDARY_OWNER_CONTRACT_MISSING_OR_INVALID" in (
        _owner_attestation_codes(
            truth_rows=[story_truth],
            frozen=missing_frozen,
        )
    )


def test_context_or_straddling_truth_cannot_forge_boundary_owner():
    context_truth = _truth_row(
        "later-candidate-truth",
        start_ms=6_000,
        end_ms=7_000,
    )
    context_owner = _frozen_owner_fixture(owners=[_truth_owner(context_truth)])
    assert "FROZEN_BOUNDARY_OWNER_CONTRACT_MISSING_OR_INVALID" in (
        _owner_attestation_codes(
            truth_rows=[context_truth],
            frozen=context_owner,
        )
    )

    straddling_truth = _truth_row(
        "straddling-truth",
        start_ms=3_500,
        end_ms=4_500,
    )
    assert "FROZEN_BOUNDARY_OWNER_CONTRACT_MISSING_OR_INVALID" in (
        _owner_attestation_codes(
            truth_rows=[straddling_truth],
            frozen=_frozen_owner_fixture(),
        )
    )


def test_owner_scope_lead_tolerance_matches_producer_arithmetic():
    from src.autoslice.review_package_owner_audit import (
        _candidate_owner_scope,
    )

    spec = {
        "candidate_id": "lead-tol-fixture",
        "semantic_start_ms": 10_000,
        "semantic_end_ms": 40_000,
        "pieces": [{"start_ms": 0, "end_ms": 100_000}],
        "boundary_repair_extend_cap_ms": 30_000,
    }
    scope = candidate_boundary_owner_scope(spec=spec, durations=[100_000])
    assert scope["story_start_ms"] == 9_500  # 10_000 - 500 tolerance
    frozen = {
        "owner_eligibility_scope": scope,
        "story_start_ms": scope["story_start_ms"],
        "story_end_ms": scope["story_end_ms"],
    }

    valid, story_start, _, _ = _candidate_owner_scope(frozen=frozen, record={})
    assert valid and story_start == 9_500

    inflated = dict(scope)
    inflated["lead_tolerance_ms"] = 600
    inflated["story_start_ms"] = 9_400
    assert not _candidate_owner_scope(
        frozen={
            "owner_eligibility_scope": inflated,
            "story_start_ms": 9_400,
            "story_end_ms": scope["story_end_ms"],
        },
        record={},
    )[0]

    forged = dict(scope)
    forged["semantic_story_start_ms"] = 9_000
    assert not _candidate_owner_scope(
        frozen={
            "owner_eligibility_scope": forged,
            "story_start_ms": scope["story_start_ms"],
            "story_end_ms": scope["story_end_ms"],
        },
        record={},
    )[0]


def test_ledger_progression_equivalence_gates_on_package_overlap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import src.autoslice.review_package_owner_audit as owner_audit

    ledger_path = tmp_path / "subtitle_truth_ledger.v1.json"

    def write_ledger(entries: list[dict]) -> str:
        ledger_path.write_text(
            json.dumps(
                {"entries": entries, "source_aliases": []},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return "sha256:" + hashlib.sha256(ledger_path.read_bytes()).hexdigest()

    def entry(truth_id: str, start: int, end: int, **extra) -> dict:
        return {
            "truth_id": truth_id,
            "recording_basename": "rec_a.mp4",
            "source_start_ms": start,
            "source_end_ms": end,
            **extra,
        }

    monkeypatch.setattr(owner_audit, "SOURCE_TRUTH_LEDGER_PATH", ledger_path)
    recorded_sha = write_ledger([entry("seen-truth", 1_000, 2_000)])
    provenance = {
        "source_piece": {
            "source_path": "/host/rec_a.mp4",
            "start_ms": 0,
            "end_ms": 10_000,
        }
    }
    audit_base = {
        "schema_version": "source-subtitle-truth-audit.v1",
        "status": "APPLIED",
        "applied": [
            {
                "truth_id": "seen-truth",
                "required": True,
                "boundary_role": "story_content",
                "local_windows": [{"start_ms": 1_000, "end_ms": 2_000}],
            }
        ],
        "satisfied": [],
        "failures": [],
        "ledger_path": str(ledger_path),
        "ledger_sha256": recorded_sha,
    }

    # ledger grows with a non-overlapping entry -> equivalent, still valid
    write_ledger(
        [
            entry("seen-truth", 1_000, 2_000),
            entry("other-clip-truth", 50_000, 60_000),
        ]
    )
    assert owner_audit._source_truth_audit_valid(audit_base, provenance=provenance)

    # a new entry overlapping this package that the audit never saw -> stale
    write_ledger(
        [
            entry("seen-truth", 1_000, 2_000),
            entry("new-overlapping-truth", 3_000, 4_000),
        ]
    )
    assert not owner_audit._source_truth_audit_valid(audit_base, provenance=provenance)

    # superseded revisions of an overlapping entry do not force a rerun
    write_ledger(
        [
            entry("seen-truth", 1_000, 2_000),
            entry(
                "retired-truth",
                3_000,
                4_000,
                assertion_state="SUPERSEDED",
            ),
        ]
    )
    assert owner_audit._source_truth_audit_valid(audit_base, provenance=provenance)

    # without provenance the progression cannot be scoped -> stale
    write_ledger(
        [
            entry("seen-truth", 1_000, 2_000),
            entry("other-clip-truth", 50_000, 60_000),
        ]
    )
    assert not owner_audit._source_truth_audit_valid(audit_base, provenance=None)


def test_reviewed_baseline_is_text_authority_not_boundary_owner():
    no_boundary_owner = _frozen_owner_fixture()
    assert "FROZEN_BOUNDARY_OWNER_CONTRACT_MISSING_OR_INVALID" not in (
        _owner_attestation_codes(
            truth_rows=[],
            frozen=no_boundary_owner,
            baseline_applied=True,
        )
    )

    forged_baseline_owner = _frozen_owner_fixture(
        owners=[
            {
                "owner_kind": "reviewed_redelivery_baseline",
                "owner_id": "exact-reviewed-interval",
                "required": True,
                "local_windows": [{"start_ms": 0, "end_ms": 4_000}],
            }
        ]
    )
    assert "FROZEN_BOUNDARY_OWNER_CONTRACT_MISSING_OR_INVALID" in (
        _owner_attestation_codes(
            truth_rows=[],
            frozen=forged_baseline_owner,
            baseline_applied=True,
        )
    )


def test_story_owner_is_recomputed_from_candidate_scope_and_exact_window():
    row = {
        "finding_id": "inside-exact-read",
        "matched_start_ms": 1_000,
        "matched_end_ms": 2_000,
        "owner_eligible": True,
        "boundary_required": True,
        "boundary_owner_id": "inside-exact-read",
    }
    valid_frozen = _frozen_owner_fixture(
        owners=[
            _story_owner(
                "exact_read",
                "inside-exact-read",
                start_ms=1_000,
                end_ms=2_000,
            )
        ]
    )
    valid_codes = _owner_attestation_codes(
        truth_rows=[],
        frozen=valid_frozen,
        chat_rows_by_key={"applied": [row]},
    )
    assert "FROZEN_BOUNDARY_OWNER_CONTRACT_MISSING_OR_INVALID" not in (valid_codes)

    hidden_required = dict(row)
    hidden_required["boundary_required"] = False
    hidden_required.pop("boundary_owner_id")
    hidden_codes = _owner_attestation_codes(
        truth_rows=[],
        frozen=_frozen_owner_fixture(),
        chat_rows_by_key={"applied": [hidden_required]},
    )
    assert "FROZEN_BOUNDARY_OWNER_CONTRACT_MISSING_OR_INVALID" in (hidden_codes)

    shortened_frozen = _frozen_owner_fixture(
        owners=[
            _story_owner(
                "exact_read",
                "inside-exact-read",
                start_ms=1_000,
                end_ms=1_500,
            )
        ]
    )
    shortened_codes = _owner_attestation_codes(
        truth_rows=[],
        frozen=shortened_frozen,
        chat_rows_by_key={"applied": [row]},
    )
    assert "FROZEN_BOUNDARY_OWNER_CONTRACT_MISSING_OR_INVALID" in (shortened_codes)


def test_narrow_story_owner_uses_its_typed_slot_without_exact_read_gate():
    row = {
        "verdict_id": "sender-slot",
        "matched_start_ms": 1_200,
        "matched_end_ms": 1_800,
        "boundary_required": True,
        "boundary_owner_id": "sender-slot",
    }
    frozen = _frozen_owner_fixture(
        owners=[
            _story_owner(
                "sc_sender",
                "sender-slot",
                start_ms=1_200,
                end_ms=1_800,
            )
        ]
    )

    codes = _owner_attestation_codes(
        truth_rows=[],
        frozen=frozen,
        chat_rows_by_key={"sender_repairs": [row]},
    )

    assert "FROZEN_BOUNDARY_OWNER_CONTRACT_MISSING_OR_INVALID" not in codes


def test_exact_final_surface_owner_does_not_reopen_frozen_boundary_set():
    repair = {
        "schema_version": "exact-final-cpa-self-heal.v1",
        "cue_index": 1,
        "matched_start_ms": 1_000,
        "matched_end_ms": 2_000,
        "before": "旧字",
        "after": "新字",
        "decision_authority": "CPA_JUDGE",
        "timing_immutable": True,
        "mutation_authority": {
            "schema_version": "subtitle-correction-mutation-authority.v1",
            "status": "PASS",
        },
    }
    repair_sha256 = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(
                repair,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
    )
    row = {
        "mode": "exact_final_cpa_self_heal",
        "decision_authority": "CPA_JUDGE",
        "mutation_authority": repair["mutation_authority"],
        "matched_start_ms": 1_000,
        "matched_end_ms": 2_000,
        "structured_exact_text": "新字",
        "timing_immutable": True,
        "boundary_required": False,
        "boundary_owner_rejection": ("POST_BOUNDARY_FREEZE_FINAL_SURFACE_OWNER"),
        "exact_final_repair_sha256": repair_sha256,
    }
    registrations = [
        {
            "schema_version": "exact-final-cpa-surface-registration.v1",
            "status": "REGISTERED",
            "exact_final_repair_sha256": repair_sha256,
            "owner_entity_repair_index": 0,
            "superseded_entity_repair_indexes": [],
        }
    ]
    self_heal = {
        "schema_version": "exact-final-cpa-self-heal-audit.v1",
        "status": "PASS",
        "passes": [{"pass_index": 1, "repairs": [repair]}],
    }
    chat_rows = {
        "entity_repairs": [row],
        "exact_final_cpa_surface_registrations": registrations,
        "exact_final_cpa_self_heal": self_heal,
    }

    codes = _owner_attestation_codes(
        truth_rows=[],
        frozen=_frozen_owner_fixture(),
        chat_rows_by_key=chat_rows,
    )
    assert "FROZEN_BOUNDARY_OWNER_CONTRACT_MISSING_OR_INVALID" not in codes

    missing_registration = dict(chat_rows)
    missing_registration["exact_final_cpa_surface_registrations"] = []
    invalid_codes = _owner_attestation_codes(
        truth_rows=[],
        frozen=_frozen_owner_fixture(),
        chat_rows_by_key=missing_registration,
    )
    assert "FROZEN_BOUNDARY_OWNER_CONTRACT_MISSING_OR_INVALID" in invalid_codes


def test_exact_final_supersession_preserves_prior_frozen_boundary_owner():
    repair = {
        "schema_version": "exact-final-cpa-self-heal.v1",
        "cue_index": 1,
        "matched_start_ms": 1_000,
        "matched_end_ms": 2_000,
        "before": "旧字",
        "after": "新字",
        "decision_authority": "CPA_JUDGE",
        "timing_immutable": True,
        "mutation_authority": {
            "schema_version": "subtitle-correction-mutation-authority.v1",
            "status": "PASS",
        },
    }
    repair_sha256 = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(
                repair,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
    )
    predecessor = {
        "mode": "final_review_context_adjudication",
        "matched_start_ms": 1_000,
        "matched_end_ms": 2_000,
        "structured_exact_text": "旧字",
        "boundary_required": True,
        "boundary_owner_id": "entity_repair:1:1000:2000",
        "reconciliation": {
            "schema_version": "exact-final-cpa-supersession.v1",
            "status": "SUPERSEDED_BY_EXACT_FINAL_CPA",
            "exact_final_repair_sha256": repair_sha256,
            "before_sha256": "sha256:" + hashlib.sha256(b"\xe6\x97\xa7\xe5\xad\x97").hexdigest(),
            "after_sha256": "sha256:" + hashlib.sha256(b"\xe6\x96\xb0\xe5\xad\x97").hexdigest(),
            "timing_immutable": True,
        },
    }
    successor = {
        "mode": "exact_final_cpa_self_heal",
        "decision_authority": "CPA_JUDGE",
        "mutation_authority": repair["mutation_authority"],
        "matched_start_ms": 1_000,
        "matched_end_ms": 2_000,
        "structured_exact_text": "新字",
        "timing_immutable": True,
        "boundary_required": False,
        "boundary_owner_rejection": ("POST_BOUNDARY_FREEZE_FINAL_SURFACE_OWNER"),
        "exact_final_repair_sha256": repair_sha256,
    }
    registration = {
        "schema_version": "exact-final-cpa-surface-registration.v1",
        "status": "REGISTERED",
        "exact_final_repair_sha256": repair_sha256,
        "owner_entity_repair_index": 1,
        "superseded_entity_repair_indexes": [0],
    }
    chat_rows = {
        "entity_repairs": [predecessor, successor],
        "exact_final_cpa_surface_registrations": [registration],
        "exact_final_cpa_self_heal": {
            "schema_version": "exact-final-cpa-self-heal-audit.v1",
            "status": "PASS",
            "passes": [{"pass_index": 1, "repairs": [repair]}],
        },
    }
    frozen = _frozen_owner_fixture(
        owners=[
            _story_owner(
                "entity_repair",
                "entity_repair:1:1000:2000",
                start_ms=1_000,
                end_ms=2_000,
            )
        ]
    )

    codes = _owner_attestation_codes(
        truth_rows=[],
        frozen=frozen,
        chat_rows_by_key=chat_rows,
    )
    assert "FROZEN_BOUNDARY_OWNER_CONTRACT_MISSING_OR_INVALID" not in codes

    registration["owner_entity_repair_index"] = 0
    invalid_codes = _owner_attestation_codes(
        truth_rows=[],
        frozen=frozen,
        chat_rows_by_key=chat_rows,
    )
    assert "FROZEN_BOUNDARY_OWNER_CONTRACT_MISSING_OR_INVALID" in invalid_codes


@pytest.mark.parametrize("drift", (None, "authority", "status", "cue", "owner"))
def test_manifest_bound_qixi_terminal_replay_preserves_only_exact_frozen_owner(
    drift: str,
) -> None:
    """A typed terminal replay cannot erase a frozen exact-read owner by itself."""

    terminal_authority = {"authority_sha256": "sha256:" + "a" * 64}
    row = {
        "finding_id": "qixi-terminal-read",
        "matched_start_ms": 1_000,
        "matched_end_ms": 2_000,
        "boundary_required": True,
        "boundary_owner_id": "qixi-terminal-read",
        "owner_eligible": True,
        "exact_text": "旧字幕",
        "final_verification_scope": "SUPERSEDED_BY_REDELIVERY_BASELINE",
        "final_verification_scope_reason": (
            "BOUNDARY_OWNER_REVERTED_BY_VERIFIED_BASELINE_REPLAY"
        ),
        "final_redelivery_baseline_revert": {
            "before_payload": "旧字幕",
            "after_payload": "新字幕",
            "baseline_cue_indexes": [1],
        },
    }
    baseline = {
        "mappings": [
            {
                "baseline_cue_index": 1,
                "start_ms": 1_000,
                "end_ms": 2_000,
                "text": "新字幕",
                "final_owner_verified": True,
            }
        ]
    }
    chat_rows: dict[str, object] = {
        "applied": [row],
        "redelivery_subtitle_baseline_audit": baseline,
    }
    reconcile_terminal_baseline_replay_supersessions(
        chat_rows,
        terminal_projection_authority=terminal_authority,
        delivery_start_ms=0,
    )
    reconciliation = row["reconciliation"]
    assert isinstance(reconciliation, dict)
    if drift == "authority":
        reconciliation["terminal_projection_authority_sha256"] = "sha256:" + "b" * 64
    elif drift == "status":
        reconciliation["status"] = "FORGED"
    elif drift == "cue":
        reconciliation["baseline_cue_indexes"] = [2]
    elif drift == "owner":
        row["boundary_owner_id"] = "forged-owner"

    frozen = _frozen_owner_fixture(
        owners=[
            _story_owner(
                "exact_read", "qixi-terminal-read", start_ms=1_000, end_ms=2_000
            )
        ]
    )
    codes = _owner_attestation_codes(
        truth_rows=[],
        frozen=frozen,
        chat_rows_by_key=chat_rows,
        qixi_terminal_projection_authority=terminal_authority,
        delivery_start_ms=0,
    )
    expected_missing = drift is not None
    assert (
        "FROZEN_BOUNDARY_OWNER_CONTRACT_MISSING_OR_INVALID" in codes
    ) is expected_missing


@pytest.mark.parametrize(
    "row",
    [
        {
            "finding_id": "outside-read",
            "matched_start_ms": 6_000,
            "matched_end_ms": 7_000,
            "owner_eligible": True,
            "boundary_required": False,
            "boundary_owner_rejection": ("OUTSIDE_IMMUTABLE_STORY_SCOPE"),
        },
        {
            "finding_id": "straddling-read",
            "matched_start_ms": 3_500,
            "matched_end_ms": 4_500,
            "owner_eligible": True,
            "boundary_required": False,
            "boundary_owner_rejection": ("STRADDLES_IMMUTABLE_STORY_SCOPE"),
        },
        {
            "finding_id": "unsupported-read",
            "matched_start_ms": 1_000,
            "matched_end_ms": 2_000,
            "owner_eligible": False,
            "boundary_required": False,
            "boundary_owner_rejection": ("EXACT_READ_SUPPORT_NOT_OWNER_ELIGIBLE"),
        },
    ],
)
def test_noneligible_story_rows_require_typed_rejection(row: dict):
    valid_codes = _owner_attestation_codes(
        truth_rows=[],
        frozen=_frozen_owner_fixture(),
        chat_rows_by_key={"applied": [row]},
    )
    assert "FROZEN_BOUNDARY_OWNER_CONTRACT_MISSING_OR_INVALID" not in (valid_codes)

    missing_reason = dict(row)
    missing_reason.pop("boundary_owner_rejection")
    invalid_codes = _owner_attestation_codes(
        truth_rows=[],
        frozen=_frozen_owner_fixture(),
        chat_rows_by_key={"applied": [missing_reason]},
    )
    assert "FROZEN_BOUNDARY_OWNER_CONTRACT_MISSING_OR_INVALID" in (invalid_codes)


def test_retry_owner_scope_receipt_must_match_current_frozen_contract():
    frozen = _frozen_owner_fixture()
    frozen["boundary_retry_owner_contract_verification"] = {
        "status": "PASS",
        "expected_contract_sha256": "sha256:" + "1" * 64,
        "owner_set_sha256": "sha256:" + "2" * 64,
        "owner_eligibility_scope_sha256": frozen["owner_eligibility_scope"]["scope_sha256"],
    }
    frozen["contract_sha256"] = frozen_boundary_owner_contract_sha256(frozen)

    assert "FROZEN_BOUNDARY_OWNER_CONTRACT_MISSING_OR_INVALID" in (
        _owner_attestation_codes(
            truth_rows=[],
            frozen=frozen,
        )
    )


def test_context_only_final_owner_receipt_counts_cannot_be_forged():
    context_truth = _truth_row(
        "later-candidate-truth",
        start_ms=6_000,
        end_ms=7_000,
    )

    codes = _owner_attestation_codes(
        truth_rows=[context_truth],
        frozen=_frozen_owner_fixture(),
        truth_owner_overrides={
            "required_truth_row_count": 1,
            "context_only_truth_row_count": 0,
            "required_window_count": 1,
        },
    )

    assert "SOURCE_TRUTH_FINAL_OWNER_ATTESTATION_MISSING" in codes


@pytest.mark.parametrize(
    ("truth", "overrides"),
    [
        (
            _truth_row(
                "inside-story",
                start_ms=1_000,
                end_ms=2_000,
            ),
            {"required_truth_ids": ["forged-id"]},
        ),
        (
            _truth_row(
                "outside-story",
                start_ms=6_000,
                end_ms=7_000,
            ),
            {"context_only_truth_ids": ["forged-id"]},
        ),
        (
            _truth_row(
                "outside-evidence",
                start_ms=6_000,
                end_ms=7_000,
            ),
            {"context_only_truth_evidence": []},
        ),
    ],
)
def test_final_owner_receipt_ids_and_context_evidence_are_exact(
    truth: dict,
    overrides: dict,
):
    codes = _owner_attestation_codes(
        truth_rows=[truth],
        frozen=(
            _frozen_owner_fixture(owners=[_truth_owner(truth)])
            if truth["local_windows"][0]["end_ms"] <= 4_000
            else _frozen_owner_fixture()
        ),
        truth_owner_overrides=overrides,
    )

    assert "SOURCE_TRUTH_FINAL_OWNER_ATTESTATION_MISSING" in codes


@pytest.mark.parametrize(
    "mutation",
    ["schema", "status", "ledger", "failure", "missing_applied_row"],
)
def test_source_truth_audit_must_bind_current_successful_ledger(
    mutation: str,
):
    audit = _source_truth_audit_fixture()
    if mutation == "schema":
        audit["schema_version"] = "source-subtitle-truth-audit.v0"
    elif mutation == "status":
        audit["status"] = "FAILED"
    elif mutation == "ledger":
        audit["ledger_sha256"] = "sha256:" + "0" * 64
    elif mutation == "failure":
        audit["status"] = "FAILED"
        audit["failures"] = [
            {
                "truth_id": "required-failure",
                "required": True,
            }
        ]
    else:
        audit["status"] = "APPLIED"

    codes = _owner_attestation_codes(
        truth_rows=[],
        frozen=_frozen_owner_fixture(),
        source_truth_audit_override=audit,
    )

    assert "SOURCE_TRUTH_AUDIT_MISSING_OR_INVALID" in codes


def test_owner_scope_candidate_id_must_match_packaged_story_contract():
    frozen = _frozen_owner_fixture(candidate_id="candidate-a")

    assert "FROZEN_BOUNDARY_OWNER_CONTRACT_MISSING_OR_INVALID" in (
        _owner_attestation_codes(
            truth_rows=[],
            frozen=frozen,
            record_candidate_id="candidate-b",
        )
    )


@pytest.mark.parametrize(
    "relative_path",
    [
        "src/autoslice/producer_boundary_owner_contract.py",
        "src/autoslice/source_subtitle_truth.py",
        "src/autoslice/producer_text_finalization.py",
        "src/autoslice/expected_value_canon_supersession.py",
        "src/autoslice/addressee_attribution.py",
        "src/autoslice/review_package_portable_evidence.py",
        "src/autoslice/candidate_entity_projection.py",
        "src/autoslice/reviewed_subtitle_baseline_registry.py",
        "src/autoslice/review_package_owner_audit.py",
        "assets/lidousha/subtitle_truth_ledger.v1.json",
    ],
)
def test_owner_policy_sources_invalidate_policy_fingerprint(
    monkeypatch,
    relative_path: str,
):
    baseline = _audit_policy_fingerprint()
    target = (REPO_ROOT / relative_path).resolve()
    original_read_bytes = Path.read_bytes

    def read_bytes_with_target_drift(path: Path) -> bytes:
        payload = original_read_bytes(path)
        if path.resolve() == target:
            return payload + b"\n# policy drift"
        return payload

    monkeypatch.setattr(Path, "read_bytes", read_bytes_with_target_drift)

    assert _audit_policy_fingerprint() != baseline


def _materialize_test_title(
    *,
    pre_overlay: Path,
    final_cover: Path,
    rendered_text: str,
    font_size: int = 120,
) -> dict[str, object]:
    split_at = max(1, len(rendered_text) // 2)
    line_texts = [
        rendered_text[:split_at],
        rendered_text[split_at:],
    ]
    render_spec: dict[str, object] = {
        "schema_version": COVER_TITLE_RENDER_SPEC_SCHEMA,
        "font_file_name": COVER_FONT.name,
        "font_file_sha256": sha256_file(COVER_FONT),
        "font_face_index": 0,
        "layer_size": [1200, 420],
        "rendered_layer_size": [1200, 420],
        "angle_degrees": 0,
        "lines": [
            {
                "x": 20,
                "y": 20 + index * 190,
                "font_size": font_size,
                "segment_pad": 10,
                "segments": [{"text": line_text, "fill": [255, 198, 41]}],
                "outlines": [
                    {"width": 10, "color": [18, 36, 79]},
                    {"width": 5, "color": [255, 255, 255]},
                ],
            }
            for index, line_text in enumerate(line_texts)
        ],
    }
    layer = render_title_layer(render_spec, font_path=COVER_FONT)
    with Image.open(pre_overlay) as source:
        final_image = source.convert("RGB")
    final_image.paste(layer, (360, 100), layer)
    final_image.save(final_cover)
    return materialize_rendered_text_pixel_evidence(
        final_cover_path=final_cover,
        pre_overlay_path=pre_overlay,
        font_path=COVER_FONT,
        render_spec=render_spec,
        paste_xy=(360, 100),
        font_size=font_size,
        rendered_text=rendered_text,
    )


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _passing_boundary_review(
    candidate_id: str,
    *,
    end_ms: int,
    review_scope: str = "source_full_window",
    source_review: dict[str, object] | None = None,
    cue_grid_digest: str | None = None,
    closure_text: str | None = None,
) -> dict[str, object]:
    is_final_delivery = review_scope == "final_delivery"
    request_sha256 = "sha256:" + ("b" if is_final_delivery else "d") * 64
    grid_sha256 = cue_grid_digest or "sha256:" + ("c" if is_final_delivery else "e") * 64
    source_separation_witness = None
    if is_final_delivery:
        assert source_review is not None
        source_endpoint = source_review["final_endpoint_binding"]
        assert isinstance(source_endpoint, dict)
        source_separation_witness = {
            "schema_version": ("talk-boundary-source-separation-witness.v1"),
            "status": "PASS",
            "source_review_sha256": semantic_review_sha256(source_review),
            "source_request_sha256": source_review["request_sha256"],
            "source_cue_grid_sha256": source_review["cue_grid_sha256"],
            "source_recommended_end_ms": source_review["recommended_end_ms"],
            "source_final_start_ms": source_endpoint["final_start_ms"],
            "source_final_end_ms": source_endpoint["final_end_ms"],
            "reason_codes": [],
        }
    return {
        "schema_version": "talk-boundary-semantic-review.v1",
        "status": "PASS",
        "review_scope": review_scope,
        "candidate_id": candidate_id,
        "request_sha256": request_sha256,
        "target_ms": end_ms,
        "target_cue_index": 1,
        "recommended_end_cue_index": 1,
        "recommended_end_ms": end_ms,
        "syntax_complete": True,
        "story_closed": True,
        "next_topic_separated": True,
        "content_anchor_covered": True,
        "selector_story_witness": {
            "status": "PASS",
            "independence_group": "semantic-llm:gpt-5.6",
        },
        "evidence_cue_indexes": [1],
        "reviewer_independence_group": "semantic-llm:gpt-5.6",
        "semantic_independence_groups": ["semantic-llm:gpt-5.6"],
        "independent_semantic_vote_count": 1,
        "correlated_reviewer_disclosure": True,
        "reason_codes": [],
        "cue_grid_sha256": grid_sha256,
        "next_topic_witness_valid": True,
        "source_separation_witness": source_separation_witness,
        "final_endpoint_binding": {
            "schema_version": "talk-boundary-final-endpoint-binding.v1",
            "status": "PASS",
            "semantic_request_sha256": request_sha256,
            "recommended_end_cue_index": 1,
            "recommended_end_ms": end_ms,
            "final_closure_cue_index": 1,
            "final_snapped_end_ms": end_ms,
            "final_start_ms": 0,
            "final_end_ms": end_ms,
            "semantic_cue_grid_sha256": grid_sha256,
            "final_cue_grid_sha256": grid_sha256,
            "closure_text_sha256": (
                "sha256:" + hashlib.sha256(closure_text.encode("utf-8")).hexdigest()
                if closure_text is not None
                else "sha256:" + "f" * 64
            ),
            "reason_codes": [],
        },
    }


def test_exact_source_pin_boundary_authority_survives_package_audit(
    tmp_path: Path,
):
    stem = "auto_193450_1863_2056"
    closure_end_ms = 3_600
    exact_media_end_ms = 4_000
    subtitle = _write(
        tmp_path / f"{stem}.srt",
        "1\n00:00:00,000 --> 00:00:03,600\n就是刚认识暂时不太熟啊\n",
    )
    source_review = _passing_boundary_review(
        stem,
        end_ms=closure_end_ms,
    )
    source_review["final_endpoint_binding"]["final_end_ms"] = exact_media_end_ms
    final_cues = parse_srt_cues(subtitle.read_text(encoding="utf-8"))
    final_review = _passing_boundary_review(
        stem,
        end_ms=closure_end_ms,
        review_scope="final_delivery",
        source_review=source_review,
        cue_grid_digest=cue_grid_sha256(final_cues),
        closure_text=final_cues[-1].text,
    )
    final_review["final_endpoint_binding"]["final_end_ms"] = exact_media_end_ms
    issues: list[dict] = []
    human_authority = "Pro source cue 911 exact endpoint"
    record = {
        "recovery_publication_authority": {
            "boundary_end_mode": "exact_source_pin",
        },
        "boundary_audit": {
            "boundary_authority": ("human_source_exact_pin_plus_semantic_review"),
            "manual_end_authority": human_authority,
            "manual_end_mode": "exact_source_pin",
            "boundary_semantic_review": source_review,
            "final_delivery_boundary_semantic_review": final_review,
            "final_start_ms": 0,
            "snapped_sentence_end_ms": closure_end_ms,
            "final_end_ms": exact_media_end_ms,
            "boundary_selection_lower_bound_ms": closure_end_ms,
            "delivery_coverage_lower_bound_ms": exact_media_end_ms,
            "tail_pad_coverage_bridge": {
                "status": "USED",
                "closure_lower_bound_ms": closure_end_ms,
                "delivery_lower_bound_ms": exact_media_end_ms,
                "maximum_tail_pad_ms": 400,
            },
            "delivery_coverage_verification": {
                "status": "PASS",
                "failure": None,
            },
            "frozen_required_boundary_owner_count": 0,
            "frozen_required_boundary_owners": [],
            "required_boundary_owner_verification": {
                "status": "PASS",
                "failures": [],
            },
        },
    }

    audit_boundary_contract(
        issue_adder=lambda rows, code, **fields: rows.append({"code": code, **fields}),
        issues=issues,
        stem=stem,
        record_path=tmp_path / f"{stem}.record.json",
        subtitle_path=subtitle,
        exact_final_review={
            "boundary_semantic_review": final_review,
        },
        record=record,
        story_contract={
            "human_boundary_authority": human_authority,
            "boundary_semantic_review": final_review,
        },
        required=True,
        is_song=False,
    )

    assert "HUMAN_BOUNDARY_AUTHORITY_DRIFT" not in {issue["code"] for issue in issues}
    record["boundary_audit"]["manual_end_mode"] = "semantic_lower_bound"
    drift_issues: list[dict] = []
    audit_boundary_contract(
        issue_adder=lambda rows, code, **fields: rows.append({"code": code, **fields}),
        issues=drift_issues,
        stem=stem,
        record_path=tmp_path / f"{stem}.record.json",
        subtitle_path=subtitle,
        exact_final_review={
            "boundary_semantic_review": final_review,
        },
        record=record,
        story_contract={
            "human_boundary_authority": human_authority,
            "boundary_semantic_review": final_review,
        },
        required=True,
        is_song=False,
    )
    assert "HUMAN_BOUNDARY_AUTHORITY_DRIFT" in {issue["code"] for issue in drift_issues}
    record["boundary_audit"]["manual_end_mode"] = "exact_source_pin"
    record["boundary_audit"]["final_end_ms"] = exact_media_end_ms + 200
    late_end_issues: list[dict] = []
    audit_boundary_contract(
        issue_adder=lambda rows, code, **fields: rows.append({"code": code, **fields}),
        issues=late_end_issues,
        stem=stem,
        record_path=tmp_path / f"{stem}.record.json",
        subtitle_path=subtitle,
        exact_final_review={
            "boundary_semantic_review": final_review,
        },
        record=record,
        story_contract={
            "human_boundary_authority": human_authority,
            "boundary_semantic_review": final_review,
        },
        required=True,
        is_song=False,
    )
    assert "BOUNDARY_DELIVERY_COVERAGE_INVALID" in {issue["code"] for issue in late_end_issues}


# 2026-08-09 auto_200130_1722_1792「对食」快车道包的真实形状：Ivan 复核区间
# 终点钉在 67760，但新鲜栅格上最后一个闭合 cue 收在 67670，下一话题 cue
# (36) 跨过终点。producer 用 typed reviewed_exact_interval_terminal_projection
# 桥接这 90ms（帽 250ms）。验收面必须认这条 typed 桥，且只在桥自洽时才把
# tail-pad 桥的 closure 下界从复核终点放宽到 snapped 闭合点。
_TP_REVIEWED_END_MS = 67_760
_TP_SNAPPED_END_MS = 67_670
_TP_FINAL_START_MS = 9_630
_TP_DRIFT_MS = 90
_TP_DRIFT_CAP_MS = 250


def _terminal_projection_binding(
    *,
    authority_sha256: str,
    grid_sha256: str,
    closure_text_sha256: str,
) -> dict[str, object]:
    return {
        "kind": "reviewed_exact_interval_terminal_projection",
        "cue_index": 35,
        "cue_start_ms": 64_440,
        "cue_end_ms": _TP_SNAPPED_END_MS,
        "cue_text_sha256": closure_text_sha256,
        "reviewed_endpoint_ms": _TP_REVIEWED_END_MS,
        "terminal_drift_ms": _TP_DRIFT_MS,
        "max_terminal_drift_ms": _TP_DRIFT_CAP_MS,
        "crossing_witness_cue_index": 36,
        "crossing_witness_start_ms": _TP_SNAPPED_END_MS,
        "crossing_witness_end_ms": 69_210,
        "crossing_witness_text_sha256": "sha256:" + "9" * 64,
        "authority_sha256": authority_sha256,
        "cue_grid_sha256": grid_sha256,
    }


def _terminal_projection_case(tmp_path: Path) -> dict[str, object]:
    """Build the 1722-shaped source/final reviews plus its boundary audit."""

    stem = "auto_200130_1722_1792.recut"
    delivered_duration_ms = _TP_REVIEWED_END_MS - _TP_FINAL_START_MS
    subtitle = _write(
        tmp_path / f"{stem}.srt",
        "1\n00:00:00,000 --> 00:00:58,130\n那我们就先这样啊\n",
    )
    source_review = _passing_boundary_review(
        "auto_200130_1722_1792",
        end_ms=_TP_REVIEWED_END_MS,
    )
    grid_sha256 = str(source_review["cue_grid_sha256"])
    authority_sha256 = "sha256:" + "7" * 64
    closure_text_sha256 = "sha256:" + "f" * 64
    binding = _terminal_projection_binding(
        authority_sha256=authority_sha256,
        grid_sha256=grid_sha256,
        closure_text_sha256=closure_text_sha256,
    )
    source_review["recommended_end_cue_index"] = 35
    source_review["evidence_cue_indexes"] = [28, 29, 35, 36]
    source_review["boundary_search_scope"] = {
        "boundary_end_mode": "semantic_lower_bound",
        "minimum_recommended_end_ms": _TP_REVIEWED_END_MS,
        "max_recommended_end_ms": _TP_REVIEWED_END_MS,
        "reviewed_exact_interval_projection": {
            "schema_version": ("reviewed-exact-interval-terminal-projection-scope.v1"),
            "authority_sha256": authority_sha256,
            "reviewed_endpoint_ms": _TP_REVIEWED_END_MS,
            "max_terminal_drift_ms": _TP_DRIFT_CAP_MS,
        },
    }
    source_review["recommendation_relaxations"] = [binding]
    source_review["selected_terminal_projection_binding"] = binding
    source_review["final_endpoint_binding"].update(
        {
            "recommended_end_cue_index": 35,
            "final_closure_cue_index": 35,
            "final_snapped_end_ms": _TP_SNAPPED_END_MS,
            "final_start_ms": _TP_FINAL_START_MS,
            "final_end_ms": _TP_REVIEWED_END_MS,
            "closure_text_sha256": closure_text_sha256,
        }
    )
    final_cues = parse_srt_cues(subtitle.read_text(encoding="utf-8"))
    final_review = _passing_boundary_review(
        "auto_200130_1722_1792",
        end_ms=delivered_duration_ms,
        review_scope="final_delivery",
        source_review=source_review,
        cue_grid_digest=cue_grid_sha256(final_cues),
        closure_text=final_cues[-1].text,
    )
    record = {
        "boundary_audit": {
            "boundary_authority": ("correlated_semantic_review_plus_deterministic_guards"),
            "boundary_semantic_review": source_review,
            "final_delivery_boundary_semantic_review": final_review,
            "final_start_ms": _TP_FINAL_START_MS,
            "final_end_ms": _TP_REVIEWED_END_MS,
            "snapped_sentence_end_ms": _TP_SNAPPED_END_MS,
            "delivery_coverage_lower_bound_ms": _TP_REVIEWED_END_MS,
            "tail_pad_coverage_bridge": {
                "status": "USED",
                "closure_lower_bound_ms": _TP_SNAPPED_END_MS,
                "delivery_lower_bound_ms": _TP_REVIEWED_END_MS,
                "maximum_tail_pad_ms": 400,
            },
            "delivery_coverage_verification": {
                "status": "PASS",
                "failure": None,
            },
        }
    }
    return {
        "stem": stem,
        "subtitle": subtitle,
        "record": record,
        "source_review": source_review,
        "final_review": final_review,
        "binding": binding,
    }


def _terminal_projection_issue_codes(
    case: dict[str, object],
    tmp_path: Path,
) -> set[str]:
    issues: list[dict] = []
    final_review = case["final_review"]
    audit_boundary_contract(
        issue_adder=lambda rows, code, **fields: rows.append({"code": code, **fields}),
        issues=issues,
        stem=str(case["stem"]),
        record_path=tmp_path / f"{case['stem']}.record.json",
        subtitle_path=case["subtitle"],
        exact_final_review={"boundary_semantic_review": final_review},
        record=case["record"],
        story_contract={"boundary_semantic_review": final_review},
        required=True,
        is_song=False,
    )
    return {issue["code"] for issue in issues}


def test_reviewed_terminal_projection_passes_the_1722_shaped_boundary_gate(
    tmp_path: Path,
):
    case = _terminal_projection_case(tmp_path)

    assert _terminal_projection_issue_codes(case, tmp_path) == set()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        # 漂移超帽：桥只在 reviewed 时序容差内成立。
        ("terminal_drift_ms", _TP_DRIFT_CAP_MS + 1),
        # 桥自称的帽与 scope 授权的帽必须一致。
        ("max_terminal_drift_ms", _TP_DRIFT_CAP_MS + 1),
        # 跨越 cue 必须紧贴闭合 cue 收尾，不能留缝。
        ("crossing_witness_start_ms", _TP_SNAPPED_END_MS - 10),
        # 跨越 cue 必须真的跨过复核终点。
        ("crossing_witness_end_ms", _TP_REVIEWED_END_MS - 10),
        # 桥必须绑到 scope 里那一份 authority。
        ("authority_sha256", "sha256:" + "1" * 64),
        # 桥必须绑到复核时那一张栅格。
        ("cue_grid_sha256", "sha256:" + "2" * 64),
        # 闭合 cue 必须就是 endpoint binding 认定的收尾 cue。
        ("cue_index", 34),
    ],
)
def test_reviewed_terminal_projection_is_fail_closed(
    tmp_path: Path,
    field: str,
    value: object,
):
    case = _terminal_projection_case(tmp_path)
    case["binding"][field] = value

    assert _terminal_projection_issue_codes(case, tmp_path) >= {
        "BOUNDARY_SEMANTIC_REVIEW_NOT_PASS",
        "BOUNDARY_RECOMMENDED_END_NOT_MATERIALIZED",
        "BOUNDARY_DELIVERY_COVERAGE_INVALID",
    }


def test_reviewed_terminal_projection_needs_the_crossing_cue_as_evidence(
    tmp_path: Path,
):
    case = _terminal_projection_case(tmp_path)
    case["source_review"]["evidence_cue_indexes"] = [28, 29, 35]

    assert _terminal_projection_issue_codes(case, tmp_path) >= {
        "BOUNDARY_SEMANTIC_REVIEW_NOT_PASS",
        "BOUNDARY_RECOMMENDED_END_NOT_MATERIALIZED",
        "BOUNDARY_DELIVERY_COVERAGE_INVALID",
    }


def test_reviewed_terminal_projection_does_not_relax_a_plain_short_closure(
    tmp_path: Path,
):
    """没有 typed 桥时，closure 下界照旧钉在复核终点（本次移植不削弱既有谓词）。"""

    case = _terminal_projection_case(tmp_path)
    source_review = case["source_review"]
    source_review.pop("selected_terminal_projection_binding")
    source_review["recommendation_relaxations"] = []
    scope = source_review["boundary_search_scope"]
    scope.pop("reviewed_exact_interval_projection")

    assert _terminal_projection_issue_codes(case, tmp_path) >= {
        "BOUNDARY_SEMANTIC_REVIEW_NOT_PASS",
        "BOUNDARY_RECOMMENDED_END_NOT_MATERIALIZED",
        "BOUNDARY_DELIVERY_COVERAGE_INVALID",
    }


def _minimal_package(tmp_path: Path) -> Path:
    root = tmp_path / "pkg"
    (root / "subtitles").mkdir(parents=True)
    (root / "ass").mkdir()
    (root / "publish").mkdir()
    (root / "evidence").mkdir()
    (root / "covers").mkdir()

    stem = "447s_hybrid_22966160_2026-06-29-22-05-02"
    _write(
        root / "subtitles" / f"{stem}.normalized.zh.srt",
        """1
00:00:00,000 --> 00:00:10,000
却说不出你欣赏我哪一
种表情

2
00:00:52,560 --> 00:01:22,540
词曲 陈绮贞
""",
    )
    _write(
        root / "ass" / f"{stem}.sapphire48.ass",
        r"""[Script Info]
ScriptType: v4.00+

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.00,0:00:10.00,Default,,0,0,0,,却说不出你欣赏我哪一\N种表情
Dialogue: 0,0:00:52.56,0:01:22.54,Default,,0,0,0,,词曲 陈绮贞
""",
    )
    _write(root / "publish" / f"{stem}.title.txt", "【李豆沙】唱着唱着突然卡住：像在KTV录的？\n")
    _write(
        root / "publish" / f"{stem}.publish.json",
        json.dumps({"title": "唱着唱着突然卡住：像在KTV录的？"}, ensure_ascii=False),
    )
    _write(
        root / "evidence" / f"{stem}.evidence.json",
        json.dumps(
            {
                "summary": "后半段是直播/伴奏事故，包含歌词：却说不出你欣赏我哪一种表情。",
                "transcript_excerpt": "却说不出你欣赏我哪一种表情 词曲 陈绮贞 怎么还卡了一下",
            },
            ensure_ascii=False,
        ),
    )
    _write(root / "covers" / f"{stem}.cover.png", "fake")
    (root / "review_manifest.json").write_text(
        json.dumps(
            {
                "status": "finished",
                "items": [
                    {
                        "stem": stem,
                        "title": "【李豆沙】唱着唱着突然卡住：像在KTV录的？",
                        "source_srt": f"/app/Videos/.../{stem}.jingting.srt",
                        "subtitle_srt": str(root / "subtitles" / f"{stem}.normalized.zh.srt"),
                        "ass_path": str(root / "ass" / f"{stem}.sapphire48.ass"),
                        "publish_json": str(root / "publish" / f"{stem}.publish.json"),
                        "title_txt": str(root / "publish" / f"{stem}.title.txt"),
                        "cover": str(root / "covers" / f"{stem}.cover.png"),
                        "evidence_json": str(root / "evidence" / f"{stem}.evidence.json"),
                        "cover_generation": "deterministic title + burned-frame cover",
                        "cover_regenerated_from_burn_frame": True,
                    }
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return root


def test_audit_blocks_song_package_without_lyric_alignment_ai_cover_and_title_sync(tmp_path: Path):
    root = _minimal_package(tmp_path)

    result = audit_package(root)

    codes = {issue["code"] for issue in result["issues"]}
    assert result["passed"] is False
    assert "SONG_LYRIC_SOURCE_MISSING" in codes
    assert "SONG_ALIGNMENT_REPORT_MISSING" in codes
    assert "SONG_CATALOG_TITLE_NOT_EXACT" in codes
    assert "PUBLISH_TITLE_TXT_MISMATCH" in codes
    assert "AI_COVER_EVIDENCE_MISSING" in codes
    assert "COVER_FALLBACK_NOT_FINISHED" in codes
    assert "SUBTITLE_LONG_STATIC_CUE" in codes


def test_current_song_bypasses_talk_only_chat_authority_gate(
    tmp_path: Path,
):
    root = tmp_path / "song"
    root.mkdir()
    (root / "review_manifest.json").write_text(
        json.dumps(
            {
                "date": "2026-07-25",
                "story_contract_required": True,
                "run_mode": "PRODUCTION_REVIEW",
                "upload_allowed": False,
                "items": [
                    {
                        "stem": "song-without-chat-authority",
                        "classification": "Song",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = audit_package(root)

    assert "CHAT_AUTHORITY_AUDIT_MISSING_OR_INVALID" not in {
        issue["code"] for issue in result["issues"]
    }


def test_current_talk_still_requires_chat_authority(
    tmp_path: Path,
):
    root = tmp_path / "talk"
    root.mkdir()
    (root / "review_manifest.json").write_text(
        json.dumps(
            {
                "date": "2026-07-25",
                "story_contract_required": True,
                "run_mode": "PRODUCTION_REVIEW",
                "upload_allowed": False,
                "items": [
                    {
                        "stem": "talk-without-chat-authority",
                        "classification": "Talk",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = audit_package(root)

    assert "CHAT_AUTHORITY_AUDIT_MISSING_OR_INVALID" in {
        issue["code"] for issue in result["issues"]
    }


def _current_talk_portable_evidence_package(
    tmp_path: Path,
) -> tuple[Path, dict[str, Path]]:
    root = tmp_path / "current-talk-portable-evidence"
    evidence = root / "evidence"
    evidence.mkdir(parents=True)
    paths = {
        "record": evidence / "candidate.record-evidence.json",
        "chat_authority": evidence / "candidate.chat-evidence.json",
        "clip_context": evidence / "candidate.context-evidence.json",
    }
    for path in paths.values():
        path.write_text("{}\n", encoding="utf-8")
    (root / "review_manifest.json").write_text(
        json.dumps(
            {
                "date": "2026-08-09",
                "story_contract_required": True,
                "run_mode": "PRODUCTION_REVIEW",
                "upload_allowed": False,
                "items": [
                    {
                        "stem": "current-talk-portable-evidence",
                        "classification": "Talk",
                        **{key: path.relative_to(root).as_posix() for key, path in paths.items()},
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return root, paths


_MANDATORY_PORTABLE_ITEM_PATH_CODES = {
    "record": "RECORD_PATH_MISSING_OR_NONPORTABLE",
    "chat_authority": "CHAT_AUTHORITY_PATH_MISSING_OR_NONPORTABLE",
    "clip_context": "CLIP_CONTEXT_PATH_MISSING_OR_NONPORTABLE",
}


@pytest.mark.parametrize(
    ("item_key", "issue_code"),
    tuple(_MANDATORY_PORTABLE_ITEM_PATH_CODES.items()),
)
@pytest.mark.parametrize(
    "invalid_shape",
    ("parent_traversal", "terminal_symlink", "ancestor_symlink"),
)
def test_current_talk_rejects_nonportable_mandatory_item_evidence(
    tmp_path: Path,
    item_key: str,
    issue_code: str,
    invalid_shape: str,
) -> None:
    root, paths = _current_talk_portable_evidence_package(tmp_path)
    manifest_path = root / "review_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    if invalid_shape == "parent_traversal":
        outside = tmp_path / f"outside-{item_key}.json"
        outside.write_text(paths[item_key].read_text(encoding="utf-8"), encoding="utf-8")
        manifest["items"][0][item_key] = f"../{outside.name}"
    elif invalid_shape == "terminal_symlink":
        link = root / f"linked-{item_key}.json"
        link.symlink_to(paths[item_key])
        manifest["items"][0][item_key] = link.relative_to(root).as_posix()
    else:
        linked_parent = root / f"linked-{item_key}-parent"
        linked_parent.symlink_to(paths[item_key].parent, target_is_directory=True)
        manifest["items"][0][item_key] = (
            linked_parent.relative_to(root) / paths[item_key].name
        ).as_posix()

    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

    result = audit_package(root)

    assert issue_code in {issue["code"] for issue in result["issues"]}


def test_current_talk_audited_inputs_bind_accepted_mandatory_evidence(
    tmp_path: Path,
) -> None:
    root, paths = _current_talk_portable_evidence_package(tmp_path)

    result = audit_package(root)

    codes = {issue["code"] for issue in result["issues"]}
    assert not codes.intersection(_MANDATORY_PORTABLE_ITEM_PATH_CODES.values())
    audited_paths = {row["path"] for row in result["audited_inputs"]}
    assert {path.relative_to(root).as_posix() for path in paths.values()} <= audited_paths


def test_audit_flags_ass_visual_line_count_and_length(tmp_path: Path):
    root = tmp_path / "pkg"
    stem = "138s_semantic_22966160_2026-06-29-22-35-01"
    _write(
        root / "ass" / f"{stem}.sapphire48.ass",
        r"""[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.00,0:00:20.00,Default,,0,0,0,,剩女是一个组织对剩女而且这一整行明显超过十八个字\\N是一个组织我于是准备\\N把你挂在这你刚好可以\\N跟这个白色背景融为一\\N体你就挂在这吧你作为\\N一副挂画就挂在这直播\\N间你的这个百合厨的属\\N性也非常符合本质
""",
    )
    (root / "review_manifest.json").write_text(
        json.dumps(
            {
                "status": "finished",
                "items": [
                    {
                        "stem": stem,
                        "title": "【李豆沙】测试",
                        "ass_path": str(root / "ass" / f"{stem}.sapphire48.ass"),
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = audit_package(root)

    codes = {issue["code"] for issue in result["issues"]}
    assert "SUBTITLE_ASS_TOO_MANY_VISUAL_LINES" in codes
    assert "SUBTITLE_ASS_LINE_TOO_LONG" in codes


def test_recovery_public_title_authority_is_bound_across_package_surfaces(
    tmp_path: Path,
) -> None:
    candidate_id = "auto_193450_1475_1543"
    authority = build_recovery_publication_authorities(
        candidate_ids={candidate_id},
        registry_path=(REPO_ROOT / "assets/lidousha/recovery_publication_authority.v1.json"),
        expected_registry_sha256=(
            "sha256:0bbb26c63c30b1e30af13e33d5513c49aa10b98afa8730ee9761f59865317e30"
        ),
    )[candidate_id]
    title = expected_recovery_publish_title(authority)
    root = tmp_path / "pkg"
    root.mkdir()
    stem = "public-title"
    publish = root / f"{stem}.publish.json"
    publish.write_text(
        json.dumps(
            {
                "title": title,
                "recovery_publication_authority": authority,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    record = {
        "recovery_publication_authority": authority,
        "publish_staging": {
            "title": title,
            "recovery_publication_authority": authority,
        },
        "artifact_hashes": {
            "publish_draft_sha256": ("sha256:" + hashlib.sha256(publish.read_bytes()).hexdigest())
        },
    }
    record_path = root / f"{stem}.record.json"
    record_path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    manifest = {
        "status": "finished",
        "items": [
            {
                "stem": stem,
                "candidate_id": candidate_id,
                "title": title,
                "record": record_path.name,
                "publish_json": publish.name,
                "recovery_publication_authority": authority,
            }
        ],
    }
    manifest_path = root / "review_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

    clean_codes = {issue["code"] for issue in audit_package(root)["issues"]}
    assert not any(code.startswith("RECOVERY_PUBLICATION") for code in clean_codes)

    del manifest["items"][0]["recovery_publication_authority"]
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    drift_codes = {issue["code"] for issue in audit_package(root)["issues"]}
    assert "RECOVERY_PUBLICATION_AUTHORITY_SURFACE_MISSING" in (drift_codes)


def test_current_review_ass_is_portable_hash_bound_and_fail_closed(
    tmp_path: Path,
) -> None:
    root = tmp_path / "pkg"
    root.mkdir()
    stem = "portable-talk"
    speaker_srt = _write(
        root / f"{stem}.speaker.srt",
        "1\n00:00:00,000 --> 00:00:01,000\n[李豆沙] 正常字幕\n",
    )
    ass = _write(
        root / f"{stem}.speaker.ass",
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, "
        "MarginV, Effect, Text\n"
        "Dialogue: 0,0:00:00.00,0:00:01.00,LDS,,0,0,0,,正常字幕\n",
    )
    manifest_path = root / "review_manifest.json"
    manifest = {
        "date": "2026-07-22",
        "run_mode": "RECOVERY_REVIEW",
        "upload_allowed": False,
        "items": [
            {
                "stem": stem,
                "speaker_srt": speaker_srt.name,
                "speaker_srt_sha256": "sha256:"
                + hashlib.sha256(speaker_srt.read_bytes()).hexdigest(),
                "ass_path": f"/missing/remote/{ass.name}",
                "ass_sha256": "sha256:" + hashlib.sha256(ass.read_bytes()).hexdigest(),
            }
        ],
    }

    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    remote_path_result = audit_package(root)
    assert "SUBTITLE_ASS_PATH_MISSING_OR_NONPORTABLE" in {
        issue["code"] for issue in remote_path_result["issues"]
    }

    manifest["items"][0]["ass_path"] = "missing.speaker.ass"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    missing_result = audit_package(root)
    assert "SUBTITLE_ASS_PATH_MISSING_OR_NONPORTABLE" in {
        issue["code"] for issue in missing_result["issues"]
    }

    manifest["items"][0]["ass_path"] = ass.name
    del manifest["items"][0]["ass_sha256"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    missing_hash_result = audit_package(root)
    assert "SUBTITLE_ASS_HASH_MISSING_OR_INVALID" in {
        issue["code"] for issue in missing_hash_result["issues"]
    }

    manifest["items"][0]["ass_sha256"] = "sha256:" + "0" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    hash_result = audit_package(root)
    assert "SUBTITLE_ASS_HASH_MISMATCH" in {issue["code"] for issue in hash_result["issues"]}

    ass.write_text("[Events]\n", encoding="utf-8")
    manifest["items"][0]["ass_sha256"] = "sha256:" + hashlib.sha256(ass.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    dialogue_result = audit_package(root)
    dialogue_codes = {issue["code"] for issue in dialogue_result["issues"]}
    assert "SUBTITLE_ASS_DIALOGUE_MISSING" in dialogue_codes
    assert "SUBTITLE_ASS_HASH_MISMATCH" not in dialogue_codes


def test_current_review_ass_rejects_package_internal_symlink(
    tmp_path: Path,
) -> None:
    root = tmp_path / "pkg"
    root.mkdir()
    speaker_srt = _write(
        root / "portable-talk.speaker.srt",
        "1\n00:00:00,000 --> 00:00:01,000\n[李豆沙] 正常字幕\n",
    )
    target = _write(
        root / "real.speaker.ass",
        "[Events]\nDialogue: 0,0:00:00.00,0:00:01.00,LDS,,0,0,0,,正常字幕\n",
    )
    linked = root / "linked.speaker.ass"
    linked.symlink_to(target.name)
    (root / "review_manifest.json").write_text(
        json.dumps(
            {
                "date": "2026-07-22",
                "run_mode": "RECOVERY_REVIEW",
                "upload_allowed": False,
                "items": [
                    {
                        "stem": "portable-talk",
                        "speaker_srt": speaker_srt.name,
                        "speaker_srt_sha256": "sha256:"
                        + hashlib.sha256(speaker_srt.read_bytes()).hexdigest(),
                        "ass_path": linked.name,
                        "ass_sha256": "sha256:" + hashlib.sha256(target.read_bytes()).hexdigest(),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = audit_package(root)

    assert "SUBTITLE_ASS_PATH_MISSING_OR_NONPORTABLE" in {
        issue["code"] for issue in result["issues"]
    }


def test_current_review_ass_replays_all_speaker_events_even_after_hash_rebinding(
    tmp_path: Path,
) -> None:
    root = tmp_path / "pkg"
    root.mkdir()
    speaker_srt = _write(
        root / "talk.speaker.srt",
        "1\n00:00:00,000 --> 00:00:01,000\n[李豆沙] 第一句\n\n"
        "2\n00:00:01,000 --> 00:00:02,000\n[连线] 第二句\n",
    )
    ass = root / "talk.speaker.ass"
    valid_events = (
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, "
        "MarginV, Effect, Text\n"
        "Dialogue: 0,0:00:00.00,0:00:01.00,LDS,,0,0,0,,第一句\n"
        "Dialogue: 0,0:00:01.00,0:00:02.00,GUEST,,0,0,0,,第二句\n"
    )

    def audit_with_rebound_hashes(ass_text: str):
        ass.write_text(ass_text, encoding="utf-8")
        ass_hash = "sha256:" + hashlib.sha256(ass.read_bytes()).hexdigest()
        srt_hash = "sha256:" + hashlib.sha256(speaker_srt.read_bytes()).hexdigest()
        return audit_review_package_ass(
            root=root,
            item={
                "ass_path": ass.name,
                "ass_sha256": ass_hash,
                "speaker_srt": speaker_srt.name,
                "speaker_srt_sha256": srt_hash,
            },
            portable_required=True,
            max_visual_lines=2,
            max_visual_line_chars=28,
            record={"artifact_hashes": {"ass_sha256": ass_hash}},
            chat_authority={
                "speaker_ass_sha256": ass_hash,
                "final_speaker_srt_sha256": srt_hash,
            },
        )

    assert audit_with_rebound_hashes(valid_events).issues == ()

    cases = {
        "SUBTITLE_ASS_SPEAKER_SRT_TEXT_MISMATCH": valid_events.replace("第一句\n", "伪造文字\n", 1),
        "SUBTITLE_ASS_SPEAKER_SRT_TIMELINE_MISMATCH": valid_events.replace(
            "0:00:01.00,0:00:02.00", "0:00:01.10,0:00:02.00"
        ),
        "SUBTITLE_ASS_SPEAKER_SRT_STYLE_MISMATCH": valid_events.replace(
            "0:00:02.00,GUEST", "0:00:02.00,LDS"
        ),
        "SUBTITLE_ASS_SPEAKER_SRT_EVENT_COUNT_MISMATCH": (valid_events.rsplit("Dialogue:", 1)[0]),
    }
    for expected_code, tampered_ass in cases.items():
        codes = {issue.code for issue in audit_with_rebound_hashes(tampered_ass).issues}
        assert expected_code in codes


def _uniform_daily_package(tmp_path: Path):
    """Daily uniform_host package: unlabeled text SRT doubles as speaker SRT."""

    root = tmp_path / "pkg"
    root.mkdir()
    srt = _write(
        root / "talk.recut.srt",
        "1\n00:00:00,000 --> 00:00:01,000\n第一句\n\n2\n00:00:01,000 --> 00:00:02,000\n第二句\n",
    )
    ass = _write(
        root / "talk.final.ass",
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, "
        "MarginV, Effect, Text\n"
        "Dialogue: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,第一句\n"
        "Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,第二句\n",
    )
    srt_hash = "sha256:" + hashlib.sha256(srt.read_bytes()).hexdigest()
    ass_hash = "sha256:" + hashlib.sha256(ass.read_bytes()).hexdigest()
    item = {
        "ass_path": ass.name,
        "ass_sha256": ass_hash,
        "speaker_srt": srt.name,
        "speaker_srt_sha256": srt_hash,
    }
    record = {"artifact_hashes": {"ass_sha256": ass_hash}}
    chat_authority = {
        "speaker_ass_path": None,
        "speaker_ass_sha256": None,
        "final_text_srt_sha256": srt_hash.removeprefix("sha256:"),
        "final_speaker_srt_sha256": srt_hash.removeprefix("sha256:"),
    }
    return root, item, record, chat_authority, ass


def test_uniform_host_fallback_package_passes_full_parity(tmp_path: Path):
    root, item, record, chat_authority, _ = _uniform_daily_package(tmp_path)

    result = audit_review_package_ass(
        root=root,
        item=item,
        portable_required=True,
        max_visual_lines=2,
        max_visual_line_chars=28,
        record=record,
        chat_authority=chat_authority,
    )

    assert result.issues == ()


def test_uniform_host_fallback_still_replays_event_parity(tmp_path: Path):
    root, item, record, chat_authority, ass = _uniform_daily_package(tmp_path)
    tampered = ass.read_text(encoding="utf-8").replace("第二句\n", "伪造\n")
    ass.write_text(tampered, encoding="utf-8")
    item = dict(item)
    item["ass_sha256"] = "sha256:" + hashlib.sha256(ass.read_bytes()).hexdigest()
    record = {"artifact_hashes": {"ass_sha256": item["ass_sha256"]}}

    result = audit_review_package_ass(
        root=root,
        item=item,
        portable_required=True,
        max_visual_lines=2,
        max_visual_line_chars=28,
        record=record,
        chat_authority=chat_authority,
    )

    assert "SUBTITLE_ASS_SPEAKER_SRT_TEXT_MISMATCH" in {issue.code for issue in result.issues}


def test_recovery_package_cannot_borrow_uniform_fallback(tmp_path: Path):
    """A real speaker_ass_sha256 in chat authority keeps the strict path."""

    root, item, record, chat_authority, _ = _uniform_daily_package(tmp_path)
    chat_authority = dict(chat_authority)
    chat_authority["speaker_ass_sha256"] = "sha256:" + "0" * 64

    result = audit_review_package_ass(
        root=root,
        item=item,
        portable_required=True,
        max_visual_lines=2,
        max_visual_line_chars=28,
        record=record,
        chat_authority=chat_authority,
    )

    codes = {issue.code for issue in result.issues}
    # strict lane: unlabeled cues and the chat ass hash both fail again
    assert "SUBTITLE_SPEAKER_SRT_INVALID" in codes
    assert "SUBTITLE_ASS_CHAT_HASH_MISMATCH" in codes


def test_uniform_fallback_requires_speaker_text_hash_identity(tmp_path: Path):
    root, item, record, chat_authority, _ = _uniform_daily_package(tmp_path)
    chat_authority = dict(chat_authority)
    chat_authority["final_speaker_srt_sha256"] = "1" * 64

    result = audit_review_package_ass(
        root=root,
        item=item,
        portable_required=True,
        max_visual_lines=2,
        max_visual_line_chars=28,
        record=record,
        chat_authority=chat_authority,
    )

    assert "SUBTITLE_SPEAKER_SRT_INVALID" in {issue.code for issue in result.issues}


def test_audit_does_not_flag_ai_cover_dict_when_fallback_used_false(tmp_path: Path):
    root = tmp_path / "pkg"
    stem = "447s_travel_meaning_redone_20260629"
    srt = _write(
        root / "subtitles" / f"{stem}.srt",
        "1\n00:00:00,000 --> 00:00:06,000\n却说不出你欣赏我哪一种表情\n",
    )
    ass = _write(
        root / "ass" / f"{stem}.ass",
        "[Events]\nDialogue: 0,0:00:00.00,0:00:06.00,Default,,0,0,0,,却说不出你欣赏我哪一种表情\n",
    )
    title = "【李豆沙】豆沙歌，《旅行的意义》"
    publish = _write(
        root / "publish" / f"{stem}.publish.json", json.dumps({"title": title}, ensure_ascii=False)
    )
    title_txt = _write(root / "publish" / f"{stem}.title.txt", title + "\n")
    evidence = _write(
        root / "evidence" / f"{stem}.evidence.json",
        json.dumps(
            {
                "classification": "song",
                "lyrics_alignment": {"lyric_source_url": "https://example.test/lrc"},
            },
            ensure_ascii=False,
        ),
    )
    ai_bg = root / "covers_ai_original" / f"{stem}.ai-bg.png"
    ai_bg.parent.mkdir(parents=True, exist_ok=True)
    ai_bg.write_bytes(b"fake-ai-bg")
    cover = root / "covers" / f"{stem}.cover.png"
    cover.parent.mkdir(parents=True, exist_ok=True)
    cover.write_bytes(b"fake-cover")
    (root / "review_manifest.json").write_text(
        json.dumps(
            {
                "items": [
                    {
                        "stem": stem,
                        "classification": "song",
                        "source_srt": str(srt),
                        "subtitle_srt": str(srt),
                        "ass_path": str(ass),
                        "publish_json": str(publish),
                        "title_txt": str(title_txt),
                        "cover": str(cover),
                        "evidence_json": str(evidence),
                        "lyrics_alignment_report": str(evidence),
                        "ai_cover_generated": True,
                        "source_ai_background": str(ai_bg),
                        "cover_generation": {
                            "workflow": "cpa-openai-compatible-image-edit-cover",
                            "method": "images.edit",
                            "model": "gpt-image-2",
                            "image_gen_model": "cpa",
                            "fallback_used": False,
                            "ai_background": str(ai_bg),
                            "ai_background_sha256": "sha256:"
                            + hashlib.sha256(ai_bg.read_bytes()).hexdigest(),
                            "final_cover": str(cover),
                            "final_cover_sha256": "sha256:"
                            + hashlib.sha256(cover.read_bytes()).hexdigest(),
                            "attempted_models": ["gpt-image-2"],
                        },
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = audit_package(root)

    assert result["passed"] is True
    assert result["issues"] == []


def test_audit_replays_typed_manual_same_bv_receipt_and_rejects_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "pkg"
    root.mkdir()
    cover = _write(root / "cover.png", "cover")
    evidence = _write(root / "evidence.json", json.dumps({"classification": "talk"}))
    subtitle = _write(root / "subtitle.srt", "1\n00:00:00,000 --> 00:00:01,000\n歌\n")
    publish_title = "【李豆沙】测试歌《七夕安排》，甜甜苦苦全都说清楚"
    publish = _write(root / "publish.json", json.dumps({"title": publish_title}))
    title = _write(root / "title.txt", publish_title + "\n")
    ai_background = _write(root / "ai-bg.png", "background")
    receipt_path = root / "qixi-corrected-package-finalization.json"
    inner = {
        "schema_version": "manual-corrected-same-bv.v1",
        "candidate_id": "typed-qixi",
        "recovery_publication_authority": None,
        "approved_burned_video_sha256": "sha256:" + "1" * 64,
        "approved_subtitle_sha256": "sha256:" + "2" * 64,
        "approved_cover_sha256": "sha256:" + "3" * 64,
    }
    outer = {"manual_corrected_same_bv": inner}
    receipt_path.write_text(json.dumps(outer), encoding="utf-8")
    generation = {
        "workflow": "cpa-openai-compatible-image-edit-cover",
        "method": "images.edit",
        "model": "gpt-image-2",
        "image_gen_model": "cpa",
        "fallback_used": False,
        "final_cover": str(cover),
        "final_cover_sha256": "sha256:" + hashlib.sha256(cover.read_bytes()).hexdigest(),
        "ai_background": str(ai_background),
        "ai_background_sha256": "sha256:" + hashlib.sha256(ai_background.read_bytes()).hexdigest(),
        "attempted_models": ["gpt-image-2"],
    }
    manifest = {
        "items": [
            {
                "stem": "typed-qixi",
                "candidate_id": "typed-qixi",
                "classification": "talk",
                "source_srt": str(subtitle),
                "subtitle_srt": str(subtitle),
                "publish_json": str(publish),
                "title_txt": str(title),
                "cover": str(cover),
                "evidence_json": str(evidence),
                "lyrics_alignment_report": str(evidence),
                "ai_cover_generated": True,
                "source_ai_background": str(ai_background),
                "cover_generation": generation,
                "manual_corrected_same_bv": inner,
                "manual_corrected_same_bv_receipt": receipt_path.name,
                "manual_corrected_same_bv_receipt_sha256": "sha256:"
                + hashlib.sha256(receipt_path.read_bytes()).hexdigest(),
            }
        ]
    }
    (root / "review_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(
        review_package_audit,
        "validate_manifest_bound_applied_receipt",
        lambda item, *, candidate_id, package_root, repo_root=None: True,
    )

    passed = audit_package(root)
    assert passed["passed"] is True, passed["issues"]
    def reject_typed(*_args, **_kwargs):
        raise review_package_audit.QixiCorrectedPackageError("fixture receipt drift")

    monkeypatch.setattr(
        review_package_audit,
        "validate_manifest_bound_applied_receipt",
        reject_typed,
    )
    receipt_path.write_text("{}\n", encoding="utf-8")
    drifted = audit_package(root)
    assert "MANUAL_CORRECTED_SAME_BV_RECEIPT_INVALID" in {
        issue["code"] for issue in drifted["issues"]
    }


def test_audit_accepts_hashed_screenshot_cover_without_ai_evidence(tmp_path: Path):
    root = tmp_path / "pkg"
    root.mkdir()
    cover = _write(root / "covers" / "talk.cover.png", "screenshot-cover")
    generation = {
        "method": "screenshot_direct",
        "reference_selection": {"best_ms": 1_000},
        "screenshot_frame": {"frame_ms": 1_000},
        "reference_image": "/remote/cover_refs/talk.cover-ref.png",
        "reference_sha256": "sha256:" + "2" * 64,
        "final_cover": str(cover),
        "final_cover_sha256": "sha256:" + hashlib.sha256(cover.read_bytes()).hexdigest(),
        "rendered_lines": ["真实联动画面"],
        "route_decision": {
            "schema_version": "lidousha-cover-route-decision.v1",
            "selected_treatment": "screenshot_direct",
            "reason": "hash-bound frame contains both participants",
        },
    }
    (root / "review_manifest.json").write_text(
        json.dumps(
            {
                "status": "finished",
                "items": [
                    {
                        "stem": "talk",
                        "title": "【李豆沙】真实联动画面",
                        "cover": str(cover),
                        "cover_generation": generation,
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = audit_package(root)

    assert result["passed"] is True
    assert result["issues"] == []


def test_audit_accepts_cpa_redraw_safely_degraded_to_direct_screenshot(
    tmp_path: Path,
):
    root = tmp_path / "pkg"
    root.mkdir()
    cover = _write(root / "covers" / "talk.cover.png", "screenshot-cover")
    generation = {
        "title": "【李豆沙】真实画面安全降级",
        "cover_text": "真实画面安全降级",
        "method": "screenshot_direct",
        "cover_origin": "SOURCE_SCREENSHOT",
        "reference_selection": {"best_ms": 1_000},
        "screenshot_frame": {"frame_ms": 1_000},
        "reference_image": "/remote/cover_refs/talk.cover-ref.png",
        "reference_sha256": "sha256:" + "2" * 64,
        "final_cover": str(cover),
        "final_cover_sha256": "sha256:" + hashlib.sha256(cover.read_bytes()).hexdigest(),
        "rendered_lines": ["真实画面安全降级"],
    }
    generation["route_decision"] = build_cover_route_decision(
        selected_treatment="cpa_redraw",
        selected_rationale="fixture initially requires a redraw",
        story_contract={},
        reference_authority=None,
        decision_inputs={"cover_mode": "auto"},
        title=generation["title"],
        cover_text=generation["cover_text"],
    )
    generation["cpa_redraw"] = {
        "status": "DEGRADED_TO_DIRECT_IDENTITY_WITNESS_UNAVAILABLE",
    }
    record_cover_route_execution(
        generation,
        actual_treatment="screenshot_direct",
        execution_status="READY_DEGRADED",
        image_generation_attempted=True,
        image_generation_used=False,
        detail="unverified generated pixels were discarded",
    )
    (root / "review_manifest.json").write_text(
        json.dumps(
            {
                "status": "finished",
                "items": [
                    {
                        "stem": "talk",
                        "title": generation["title"],
                        "cover": str(cover),
                        "cover_generation": generation,
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = audit_package(root)

    assert result["passed"] is True
    assert result["issues"] == []


@pytest.mark.parametrize(
    ("method", "frame_ms", "best_ms"),
    [
        ("screenshot_direct", None, 1_000),
        ("screenshot_direct", 1_000, None),
        ("screenshot_direct", True, 1_000),
        ("screenshot_direct", 1_000, False),
        ("screenshot_direct", -1, -1),
        ("screenshot_direct", 999, 1_000),
        ("screenshot_polish", 999, 1_000),
    ],
)
def test_audit_rejects_unbound_screenshot_frame_time(
    tmp_path: Path,
    method: str,
    frame_ms: object,
    best_ms: object,
):
    root = tmp_path / "pkg"
    root.mkdir()
    cover = _write(root / "talk.cover.png", "screenshot-cover")
    generation = {
        "method": method,
        "reference_selection": {"best_ms": best_ms},
        "screenshot_frame": {"frame_ms": frame_ms},
        "reference_image": "/remote/cover_refs/talk.cover-ref.png",
        "reference_sha256": "sha256:" + "2" * 64,
        "final_cover": str(cover),
        "final_cover_sha256": "sha256:" + hashlib.sha256(cover.read_bytes()).hexdigest(),
        "rendered_lines": ["真实联动画面"],
        "route_decision": {
            "schema_version": "lidousha-cover-route-decision.v1",
            "selected_treatment": method,
            "reason": "hash-bound frame contains both participants",
        },
    }
    (root / "review_manifest.json").write_text(
        json.dumps(
            {
                "status": "finished",
                "items": [
                    {
                        "stem": "talk",
                        "title": "【李豆沙】真实联动画面",
                        "cover": str(cover),
                        "cover_generation": generation,
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = audit_package(root)

    assert "SCREENSHOT_COVER_EVIDENCE_MISSING" in {issue["code"] for issue in result["issues"]}


def test_audit_accepts_portable_delivered_cover_with_record_hash(tmp_path: Path):
    root = tmp_path / "pkg"
    root.mkdir()
    delivered_cover = _write(
        root / "covers" / "human-readable-title.cover.png",
        "portable-screenshot-cover",
    )
    final_hash = "sha256:" + hashlib.sha256(delivered_cover.read_bytes()).hexdigest()
    generation = {
        "method": "screenshot_direct",
        "reference_selection": {"best_ms": 1_000},
        "screenshot_frame": {"frame_ms": 1_000},
        "reference_image": "/remote/cover_refs/auto.cover-ref.png",
        "reference_sha256": "sha256:" + "2" * 64,
        "final_cover": "/opt/runtime/covers/auto.screenshot-title.cover.png",
        "final_cover_sha256": final_hash,
        "rendered_lines": ["真实联动画面"],
        "route_decision": {
            "schema_version": "lidousha-cover-route-decision.v1",
            "selected_treatment": "screenshot_direct",
            "reason": "hash-bound frame contains both participants",
        },
    }
    (root / "review_manifest.json").write_text(
        json.dumps(
            {
                "status": "finished",
                "items": [
                    {
                        "stem": "talk",
                        "title": "【李豆沙】真实联动画面",
                        "cover": str(delivered_cover),
                        "cover_generation": generation,
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = audit_package(root)

    assert result["passed"] is True
    assert result["issues"] == []


def test_audit_rejects_portable_delivered_cover_with_wrong_bytes(tmp_path: Path):
    root = tmp_path / "pkg"
    root.mkdir()
    delivered_cover = _write(
        root / "covers" / "human-readable-title.cover.png",
        "wrong-cover-bytes",
    )
    generation = {
        "method": "screenshot_direct",
        "reference_selection": {"best_ms": 1_000},
        "screenshot_frame": {"frame_ms": 1_000},
        "reference_image": "/remote/cover_refs/auto.cover-ref.png",
        "reference_sha256": "sha256:" + "2" * 64,
        "final_cover": "/opt/runtime/covers/auto.screenshot-title.cover.png",
        "final_cover_sha256": "sha256:" + hashlib.sha256(b"expected-cover-bytes").hexdigest(),
        "rendered_lines": ["真实联动画面"],
        "route_decision": {
            "schema_version": "lidousha-cover-route-decision.v1",
            "selected_treatment": "screenshot_direct",
            "reason": "hash-bound frame contains both participants",
        },
    }
    (root / "review_manifest.json").write_text(
        json.dumps(
            {
                "status": "finished",
                "items": [
                    {
                        "stem": "talk",
                        "title": "【李豆沙】真实联动画面",
                        "cover": str(delivered_cover),
                        "cover_generation": generation,
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = audit_package(root)

    assert result["passed"] is False
    assert {issue["code"] for issue in result["issues"]} == {"SCREENSHOT_COVER_EVIDENCE_MISSING"}


def test_audit_rejects_cpa_model_defaults_without_materialized_ai(tmp_path: Path):
    root = tmp_path / "pkg"
    root.mkdir()
    (root / "review_manifest.json").write_text(
        json.dumps(
            {
                "status": "finished",
                "items": [
                    {
                        "stem": "talk",
                        "cover_generation": {
                            "method": "images.edit",
                            "model": "gpt-image-2",
                            "image_gen_model": "cpa",
                            "fallback_used": False,
                        },
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = audit_package(root)

    assert result["passed"] is False
    assert {issue["code"] for issue in result["issues"]} == {"AI_COVER_EVIDENCE_MISSING"}


def test_audit_blocks_known_too_small_talk_cover_title(tmp_path: Path):
    root = tmp_path / "pkg"
    root.mkdir()
    ai_bg = _write(root / "covers_ai_original" / "talk.ai-bg.png", "ai")
    cover = _write(root / "covers" / "talk.cover.png", "cover")
    record = root / "talk.record.json"
    record.write_text(
        json.dumps(
            {
                "cover_generation": {
                    "method": "images.edit",
                    "model": "gpt-image-2",
                    "fallback_used": False,
                    "font_size": 91,
                    "ai_background": str(ai_bg),
                    "ai_background_sha256": "sha256:"
                    + hashlib.sha256(ai_bg.read_bytes()).hexdigest(),
                    "final_cover": str(cover),
                    "final_cover_sha256": "sha256:"
                    + hashlib.sha256(cover.read_bytes()).hexdigest(),
                    "attempted_models": ["gpt-image-2"],
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (root / "review_manifest.json").write_text(
        json.dumps(
            {
                "status": "finished_review_package_no_upload_pending_human_review",
                "items": [
                    {
                        "stem": "talk",
                        "title": "【李豆沙】南町当面追问",
                        "record": record.name,
                        "ai_cover_generated": True,
                        "cover_generation": {
                            "method": "images.edit",
                            "model": "gpt-image-2",
                            "fallback_used": False,
                        },
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = audit_package(root)

    assert result["passed"] is False
    assert {issue["code"] for issue in result["issues"]} == {"COVER_TITLE_TOO_SMALL"}


def test_audit_accepts_explicit_bounded_sapphire72_visual_contract(tmp_path: Path):
    root = tmp_path / "pkg"
    stem = "autoslice-talk"
    line = "一二三四五六七八九十甲乙丙丁戊己庚辛壬癸子丑寅卯"
    assert 18 < len(line) <= 28
    srt = _write(
        root / f"{stem}.srt",
        f"1\n00:00:00,000 --> 00:00:06,000\n{line}\n",
    )
    ass = _write(
        root / f"{stem}.ass",
        f"[Events]\nDialogue: 0,0:00:00.00,0:00:06.00,Default,,0,0,0,,{line}\n",
    )
    cover = _write(root / "covers" / f"{stem}.cover.png", "cover")
    (root / "review_manifest.json").write_text(
        json.dumps(
            {
                "status": "finished_review_package_no_upload_pending_human_review",
                "subtitle_visual_contract": {
                    "profile": "autoslice-sapphire72",
                    "max_visual_lines": 2,
                    "max_chars_per_line": 28,
                },
                "items": [
                    {
                        "stem": stem,
                        "title": "【李豆沙】测试",
                        "subtitle_srt": str(srt),
                        "ass_path": str(ass),
                        "ai_cover_generated": True,
                        "cover": str(cover),
                        "cover_generation": {
                            "method": "screenshot_direct",
                            "reference_selection": {"best_ms": 1_000},
                            "screenshot_frame": {"frame_ms": 1_000},
                            "reference_image": "source-bound-frame.png",
                            "reference_sha256": "sha256:" + "1" * 64,
                            "final_cover": str(cover),
                            "final_cover_sha256": "sha256:"
                            + hashlib.sha256(cover.read_bytes()).hexdigest(),
                            "rendered_lines": ["测试"],
                            "route_decision": {
                                "schema_version": "lidousha-cover-route-decision.v1",
                                "selected_treatment": "screenshot_direct",
                                "reason": "fixture preserves a strong real moment",
                            },
                        },
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = audit_package(root)

    assert result["passed"] is True
    assert result["issues"] == []


def test_audit_rejects_visual_contract_looser_than_renderer(tmp_path: Path):
    root = tmp_path / "pkg"
    root.mkdir()
    (root / "review_manifest.json").write_text(
        json.dumps(
            {
                "status": "finished_review_package_no_upload_pending_human_review",
                "subtitle_visual_contract": {
                    "max_visual_lines": 3,
                    "max_chars_per_line": 40,
                },
                "items": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = audit_package(root)

    assert result["passed"] is False
    assert {issue["code"] for issue in result["issues"]} == {"SUBTITLE_VISUAL_CONTRACT_INVALID"}


def test_story_contract_package_rejects_subtitle_drift_and_unresolved_nancho_alias(
    tmp_path: Path,
):
    root = tmp_path / "pkg"
    root.mkdir()
    stem = "nancho-collab"
    transcript = "南町nightin说非常亚撒西"
    srt = _write(
        root / f"{stem}.srt",
        f"1\n00:00:00,000 --> 00:00:04,000\n{transcript}\n",
    )
    speaker_srt = _write(
        root / f"{stem}.speaker.srt",
        f"1\n00:00:00,000 --> 00:00:04,000\n[李豆沙] {transcript}\n",
    )
    speaker_ass = _write(
        root / f"{stem}.speaker.ass",
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, "
        "MarginV, Effect, Text\n"
        f"Dialogue: 0,0:00:00.00,0:00:04.00,LDS,,0,0,0,,{transcript}\n",
    )
    speaker_ass_sha256 = "sha256:" + hashlib.sha256(speaker_ass.read_bytes()).hexdigest()
    speaker_srt_sha256 = "sha256:" + hashlib.sha256(speaker_srt.read_bytes()).hexdigest()
    scorecard = normalize_selection_scorecard(
        {
            "tier": 1,
            "tier_basis": "relationship_chain",
            "tier_reason": "联动关系与反转",
            "tier_evidence_cues": [1, 2],
            "dimensions": {
                "lidousha_centrality": 4,
                "stance_intensity": 3,
                "audience_salience": 4,
                "relationship_interaction": 4,
                "persona_reversal": 3,
                "comedic_payoff": 3,
                "self_contained": 4,
            },
            "uncertainty_penalty": 0,
            "fatigue_penalty": 0,
        },
        start_cue=1,
        end_cue=2,
    )
    assert scorecard is not None
    source_sha256 = "sha256:" + "1" * 64
    cover_reference_authority = {
        "candidate_id": stem,
        "content_time_ms": 1_000,
        "source_time_ms": 1_000,
        "source_sha256": source_sha256,
        "reference_png_sha256": "sha256:" + "2" * 64,
        "visible_participant_ids": ["lidousha", "nancho"],
        "required_treatment": "cpa_redraw",
        "authority": "fixture reviewed source frame",
    }
    clip_context = build_clip_context(
        candidate_id=stem,
        spec={
            "date": "2026-07-22",
            "selection_hook": "南町当面追问李豆沙最喜欢谁",
            "pieces": [
                {
                    "start_ms": 0,
                    "end_ms": 4_000,
                    "remote_media": "/recordings/collab.mp4",
                    "source_media_sha256": source_sha256,
                }
            ],
            "session_relation_authority": {
                "state": "CONFIRMED",
                "relation_id": "20260722-lidousha-nancho-live-collaboration",
            },
        },
        draft_srt=srt.read_text(encoding="utf-8"),
        authoritative_chat=(),
        topic_resolution={"status": "NO_GRAPH"},
        session_topic_authorities=(),
        speech_memory_ledger_path=(
            Path(__file__).resolve().parents[2] / "assets/lidousha/speech_memory_ledger.v1.json"
        ),
    )
    clip_context_path = root / f"{stem}.clip-context.json"
    clip_context_path.write_text(
        json.dumps(clip_context, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    source_boundary_review = _passing_boundary_review(
        stem,
        end_ms=4_000,
    )
    final_cues = parse_srt_cues(srt.read_bytes().decode("utf-8"))
    final_boundary_review = _passing_boundary_review(
        stem,
        end_ms=4_000,
        review_scope="final_delivery",
        source_review=source_boundary_review,
        cue_grid_digest=cue_grid_sha256(final_cues),
        closure_text=final_cues[-1].text,
    )
    contract = build_story_contract(
        candidate_id=stem,
        selection_hook="南町当面追问李豆沙最喜欢谁",
        transcript_text=transcript,
        selection_scorecard=scorecard,
        session_relation_authority={
            "state": "CONFIRMED",
            "participants": ["lidousha", "nancho"],
        },
        source_media_sha256s=[source_sha256],
        cover_reference_authority=cover_reference_authority,
        clip_context=clip_context,
        recording_date="2026-07-22",
        boundary_semantic_review=final_boundary_review,
        human_boundary_authority="fixture source-reviewed closure",
    )
    publish_title = "【李豆沙】南町当面追问最最最最喜欢"
    source_fact_review = _source_fact_keep_receipt(
        hook=str(contract["selection_hook"]),
        title=publish_title,
        transcript=transcript,
        context=str(contract["clip_context_prompt"]),
        scorecard=scorecard,
    )
    contract["source_fact_review"] = source_fact_review
    record = root / f"{stem}.record.json"
    publish = root / f"{stem}.publish.json"
    publish.write_text(
        json.dumps(
            {
                "title": publish_title,
                "source_fact_review": source_fact_review,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    ai_bg = root / "covers_ai_original" / f"{stem}.ai-bg.png"
    ai_bg.parent.mkdir(parents=True)
    Image.new("RGB", (1920, 1080), (244, 238, 220)).save(ai_bg)
    cover = root / "covers" / f"{stem}.cover.png"
    cover.parent.mkdir(parents=True)
    cover_text = "南町当面追问最最最最喜欢"
    rendered_text_pixels = _materialize_test_title(
        pre_overlay=ai_bg,
        final_cover=cover,
        rendered_text=cover_text,
    )
    cover_binding = {
        "schema_version": contract["schema_version"],
        "selection_hook": contract["selection_hook"],
        "relation_state": contract["relation_state"],
        "participants": contract["participants"],
        "cover_counterpart_reference_available": contract["cover_counterpart_reference_available"],
        "cover_reference_authority": contract["cover_reference_authority"],
        "source_media_sha256s": contract["source_media_sha256s"],
        "clip_context_binding": contract["clip_context_binding"],
        "boundary_semantic_review": contract["boundary_semantic_review"],
        "human_boundary_authority": contract["human_boundary_authority"],
        "cover_fallback_mode": contract["cover_fallback_mode"],
    }
    final_cover_sha256 = "sha256:" + hashlib.sha256(cover.read_bytes()).hexdigest()
    generation = {
        "title": "【李豆沙】南町当面追问最最最最喜欢",
        "cover_text": cover_text,
        "rendered_lines": ["南町当面追问", "最最最最喜欢"],
        "rendered_text_pixels": rendered_text_pixels,
        "font_size": 120,
        "font_selection": {
            "font": COVER_FONT.name,
            "glyph_risk": [],
        },
        "angle_degrees": 0,
        "method": "images.edit",
        "cover_origin": "AI_REDRAW",
        "model": "gpt-image-2",
        "attempted_models": ["gpt-image-2"],
        "ai_background": str(ai_bg),
        "ai_background_sha256": "sha256:" + hashlib.sha256(ai_bg.read_bytes()).hexdigest(),
        "pre_overlay_path": str(ai_bg),
        "pre_overlay_sha256": rendered_text_pixels["pre_overlay_sha256"],
        "overlay_position": rendered_text_pixels["overlay_position"],
        "text_backing": "outline",
        "scrim": False,
        "final_cover": str(cover),
        "final_cover_sha256": final_cover_sha256,
        "reference_sha256": cover_reference_authority["reference_png_sha256"],
        "reference_authority": cover_reference_authority,
        "story_contract": cover_binding,
    }
    generation["route_decision"] = build_cover_route_decision(
        selected_treatment="cpa_redraw",
        selected_rationale=("fixture redraw with independent final-pixel verification"),
        story_contract=cover_binding,
        reference_authority=cover_reference_authority,
        decision_inputs={"cover_mode": "cpa"},
        title=generation["title"],
        cover_text=cover_text,
    )
    final_participant_verification = {
        "schema_version": ("lidousha-cover-final-participant-verification.v1"),
        "status": "PASS",
        "authority": "fixture independent final-pixel review",
        "final_cover_sha256": final_cover_sha256,
        "visible_participant_ids": ["lidousha", "nancho"],
    }
    record_cover_route_execution(
        generation,
        actual_treatment="cpa_redraw",
        execution_status="READY",
        image_generation_attempted=True,
        image_generation_used=True,
        final_participant_verification=final_participant_verification,
    )
    chat_authority = root / f"{stem}.chat-authority.json"
    final_srt_sha256 = (
        "sha256:" + hashlib.sha256(srt.read_text(encoding="utf-8").encode("utf-8")).hexdigest()
    )
    chat_authority.write_text(
        json.dumps(
            {
                "schema_version": "fixture-chat-authority.v1",
                "status": "PASS",
                "source_subtitle_truth_audit": (_source_truth_audit_fixture()),
                "speaker_ass_sha256": speaker_ass_sha256,
                "final_speaker_srt_sha256": speaker_srt_sha256,
                "frozen_boundary_owner_contract": (_frozen_owner_fixture(candidate_id=stem)),
                "final_boundary_required_exclusion_count": 0,
                "final_review_audit": {
                    "schema_version": "final-review-audit.v2",
                    "status": "CLEAN",
                    "release_gate": "PASS",
                    "reviewed_srt_sha256": final_srt_sha256,
                    "discovery": {
                        "status": "COMPLETE",
                        "explicit_empty_findings": True,
                    },
                    "correction_mutation_authority": {
                        "schema_version": ("subtitle-correction-mutation-audit.v1"),
                        "status": "PASS",
                        "applied_count": 0,
                        "validated_mutation_count": 0,
                        "failures": [],
                    },
                    "findings": [],
                    "validated_finding_count": 0,
                    "boundary_semantic_review": final_boundary_review,
                },
            }
        ),
        encoding="utf-8",
    )
    record.write_text(
        json.dumps(
            {
                "speaker_mode": "uniform_host",
                "story_contract": contract,
                "boundary_audit": {
                    "boundary_authority": (
                        "human_source_reviewed_lower_bound_plus_semantic_review"
                    ),
                    "manual_end_authority": "fixture source-reviewed closure",
                    "manual_end_mode": "semantic_lower_bound",
                    "boundary_semantic_review": source_boundary_review,
                    "final_delivery_boundary_semantic_review": (final_boundary_review),
                    "final_start_ms": 0,
                    "snapped_sentence_end_ms": 4_000,
                    "final_end_ms": 4_000,
                    "boundary_selection_lower_bound_ms": 4_000,
                    "delivery_coverage_lower_bound_ms": 4_000,
                    "tail_pad_coverage_bridge": {
                        "status": "NOT_NEEDED",
                        "closure_lower_bound_ms": 4_000,
                        "delivery_lower_bound_ms": 4_000,
                        "maximum_tail_pad_ms": 400,
                    },
                    "delivery_coverage_verification": {
                        "status": "PASS",
                        "failure": None,
                    },
                    "frozen_required_boundary_owner_count": 0,
                    "frozen_required_boundary_owners": [],
                    "required_boundary_owner_verification": {
                        "status": "PASS",
                        "failures": [],
                    },
                },
                "clip_context_path": clip_context_path.name,
                "clip_context_payload_sha256": clip_context["context_sha256"],
                "artifact_hashes": {
                    "clip_context_file_sha256": "sha256:"
                    + hashlib.sha256(clip_context_path.read_bytes()).hexdigest(),
                    "chat_authority_audit_sha256": "sha256:"
                    + hashlib.sha256(chat_authority.read_bytes()).hexdigest(),
                    "subtitle_sha256": final_srt_sha256,
                    "ass_sha256": speaker_ass_sha256,
                },
                "publish_staging": {
                    "title": publish_title,
                    "source_fact_review": source_fact_review,
                    "cover_text": cover_text,
                    "cover_generation": generation,
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    recorded_generation = json.loads(record.read_text(encoding="utf-8"))["publish_staging"][
        "cover_generation"
    ]
    (root / "review_manifest.json").write_text(
        json.dumps(
            {
                "date": "2026-07-22",
                "status": "finished_review_package_no_upload_pending_human_review",
                "story_contract_required": True,
                "run_mode": "RECOVERY_REVIEW",
                "upload_allowed": False,
                "cover_route_attestations": [
                    {
                        "candidate_id": stem,
                        "reference_sha256": recorded_generation.get("reference_sha256"),
                        "final_cover_sha256": recorded_generation.get("final_cover_sha256"),
                        "method": recorded_generation.get("method"),
                        "route_decision": recorded_generation.get("route_decision"),
                        "reference_authority": recorded_generation.get("reference_authority"),
                    }
                ],
                "items": [
                    {
                        "stem": stem,
                        "candidate_id": stem,
                        "title": publish_title,
                        "publish_json": publish.name,
                        "subtitle_srt": srt.name,
                        "speaker_srt": speaker_srt.name,
                        "speaker_srt_sha256": speaker_srt_sha256,
                        "ass_path": speaker_ass.name,
                        "ass_sha256": speaker_ass_sha256,
                        "record": record.name,
                        "chat_authority": chat_authority.name,
                        "clip_context": clip_context_path.name,
                        "cover": cover.relative_to(root).as_posix(),
                        "cover_title_mask": Path(rendered_text_pixels["mask_path"])
                        .relative_to(root)
                        .as_posix(),
                        "cover_pre_overlay": ai_bg.relative_to(root).as_posix(),
                        "cover_route_background": ai_bg.relative_to(root).as_posix(),
                        "source_ai_background": ai_bg.relative_to(root).as_posix(),
                        "ai_cover_generated": True,
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    initial_audit = audit_package(root)
    assert initial_audit["passed"] is True, json.dumps(
        initial_audit["issues"], ensure_ascii=False, indent=2
    )
    portable_record_text = record.read_text(encoding="utf-8")
    valid_speaker_srt = speaker_srt.read_bytes()
    speaker_srt.write_bytes(b"present but malformed speaker evidence\n")
    speaker_manifest = root / f"{stem}.speaker-finalization.json"
    speaker_manifest.write_text("{}\n", encoding="utf-8")
    invalid_speaker_record = json.loads(portable_record_text)
    invalid_speaker_record["speaker_mode"] = "required"
    invalid_speaker_record["speaker_review_srt_path"] = "/producer-only/" + speaker_srt.name
    invalid_speaker_record["speaker_finalization_manifest_path"] = (
        "/producer-only/" + speaker_manifest.name
    )
    invalid_speaker_record["speaker_finalization_manifest_sha256"] = (
        "sha256:" + hashlib.sha256(speaker_manifest.read_bytes()).hexdigest()
    )
    invalid_speaker_record["speaker_finalization"] = {}
    invalid_speaker_record["artifact_hashes"]["speaker_review_srt_sha256"] = (
        "sha256:" + hashlib.sha256(speaker_srt.read_bytes()).hexdigest()
    )
    record.write_text(
        json.dumps(invalid_speaker_record, ensure_ascii=False),
        encoding="utf-8",
    )
    invalid_speaker_audit = audit_package(root)
    assert invalid_speaker_audit["passed"] is False
    assert "SOURCE_FACT_SPEAKER_EVIDENCE_REJECTED" in {
        row["code"] for row in invalid_speaker_audit["issues"]
    }
    record.write_text(portable_record_text, encoding="utf-8")
    speaker_srt.write_bytes(valid_speaker_srt)
    stale_external_cover = tmp_path / "stale-external-cover.png"
    stale_external_background = tmp_path / "stale-external-background.png"
    Image.new("RGB", (1920, 1080), (1, 2, 3)).save(stale_external_cover)
    Image.new("RGB", (1920, 1080), (4, 5, 6)).save(stale_external_background)
    portable_record = json.loads(portable_record_text)
    portable_generation = portable_record["publish_staging"]["cover_generation"]
    portable_generation["final_cover"] = str(stale_external_cover)
    portable_generation["ai_background"] = str(stale_external_background)
    record.write_text(
        json.dumps(portable_record, ensure_ascii=False),
        encoding="utf-8",
    )
    portable_audit = audit_package(root)
    assert portable_audit["passed"] is True, json.dumps(
        portable_audit["issues"], ensure_ascii=False, indent=2
    )
    record.write_text(portable_record_text, encoding="utf-8")

    valid_chat_authority = chat_authority.read_text(encoding="utf-8")
    valid_record_before_final_review_tamper = record.read_text(encoding="utf-8")
    tampered_chat = json.loads(valid_chat_authority)
    tampered_chat["final_review_audit"]["status"] = "AUDITOR_UNAVAILABLE"
    tampered_chat["final_review_audit"]["release_gate"] = "BLOCK"
    tampered_chat["final_review_audit"]["discovery"] = {"status": "AUDITOR_UNAVAILABLE"}
    chat_authority.write_text(
        json.dumps(tampered_chat, ensure_ascii=False),
        encoding="utf-8",
    )
    rebound_record = json.loads(record.read_text(encoding="utf-8"))
    rebound_record["artifact_hashes"]["chat_authority_audit_sha256"] = (
        "sha256:" + hashlib.sha256(chat_authority.read_bytes()).hexdigest()
    )
    record.write_text(
        json.dumps(rebound_record, ensure_ascii=False),
        encoding="utf-8",
    )
    final_review_result = audit_package(root)
    assert final_review_result["passed"] is False
    assert "FINAL_REVIEW_DISCOVERY_INCOMPLETE" in {
        row["code"] for row in final_review_result["issues"]
    }
    chat_authority.write_text(valid_chat_authority, encoding="utf-8")
    record.write_text(
        valid_record_before_final_review_tamper,
        encoding="utf-8",
    )

    # A producer cannot make required source truth disappear by forging a
    # smaller frozen-boundary contract that happens to agree with the final
    # boundary audit.  The package auditor independently binds required truth
    # IDs to source_subtitle_truth owners.
    missing_owner_chat = json.loads(valid_chat_authority)
    missing_owner_chat["source_subtitle_truth_audit"] = {
        "status": "SATISFIED",
        "applied": [],
        "satisfied": [
            {
                "truth_id": "fixture-required-truth-r1",
                "required": True,
                "action": "replace_substring",
                "required_text": "南町nightin",
                "local_windows": [{"start_ms": 0, "end_ms": 4_000}],
            }
        ],
        "failures": [],
    }
    missing_owner_chat["final_source_truth_owner_verification"] = {
        "status": "PASS",
        "required_window_count": 1,
        "failures": [],
    }
    missing_owner_chat["final_required_decision_count"] = 1
    chat_authority.write_text(
        json.dumps(missing_owner_chat, ensure_ascii=False),
        encoding="utf-8",
    )
    missing_owner_record = json.loads(valid_record_before_final_review_tamper)
    missing_owner_record["artifact_hashes"]["chat_authority_audit_sha256"] = (
        "sha256:" + hashlib.sha256(chat_authority.read_bytes()).hexdigest()
    )
    record.write_text(
        json.dumps(missing_owner_record, ensure_ascii=False),
        encoding="utf-8",
    )
    missing_owner_result = audit_package(root)
    assert "FROZEN_BOUNDARY_OWNER_CONTRACT_MISSING_OR_INVALID" in {
        row["code"] for row in missing_owner_result["issues"]
    }
    chat_authority.write_text(valid_chat_authority, encoding="utf-8")
    record.write_text(
        valid_record_before_final_review_tamper,
        encoding="utf-8",
    )

    prompt_tampered_record = json.loads(valid_record_before_final_review_tamper)
    prompt_tampered_record["story_contract"]["clip_context_prompt"] += "\nforged prompt"
    record.write_text(
        json.dumps(prompt_tampered_record, ensure_ascii=False),
        encoding="utf-8",
    )
    prompt_binding_result = audit_package(root)
    assert "CLIP_CONTEXT_PROMPT_BINDING_DRIFT" in {
        row["code"] for row in prompt_binding_result["issues"]
    }
    record.write_text(
        valid_record_before_final_review_tamper,
        encoding="utf-8",
    )

    manifest_path = root / "review_manifest.json"
    valid_record = record.read_text(encoding="utf-8")
    valid_manifest = manifest_path.read_text(encoding="utf-8")
    valid_srt_bytes = srt.read_bytes()
    srt.write_bytes(valid_srt_bytes.replace(b"\n", b"\r\n"))
    newline_drift_result = audit_package(root)
    newline_drift_codes = {issue["code"] for issue in newline_drift_result["issues"]}
    assert "SUBTITLE_RECORD_HASH_MISMATCH" in newline_drift_codes
    assert "FINAL_REVIEW_SRT_BINDING_MISMATCH" in newline_drift_codes
    assert "STORY_CONTRACT_SUBTITLE_HASH_DRIFT" not in newline_drift_codes
    srt.write_bytes(valid_srt_bytes)

    # Updating every raw-byte declaration is not authority to reuse an old
    # semantic receipt for a different final cue grid.
    srt.write_bytes(
        valid_srt_bytes.replace(
            b"00:00:04,000",
            b"00:00:03,900",
        )
    )
    drifted_srt_sha256 = "sha256:" + hashlib.sha256(srt.read_bytes()).hexdigest()
    drifted_grid_chat = json.loads(valid_chat_authority)
    drifted_grid_chat["final_review_audit"]["reviewed_srt_sha256"] = drifted_srt_sha256
    chat_authority.write_text(
        json.dumps(drifted_grid_chat, ensure_ascii=False),
        encoding="utf-8",
    )
    drifted_grid_record = json.loads(valid_record)
    drifted_grid_record["artifact_hashes"]["subtitle_sha256"] = drifted_srt_sha256
    drifted_grid_record["artifact_hashes"]["chat_authority_audit_sha256"] = (
        "sha256:" + hashlib.sha256(chat_authority.read_bytes()).hexdigest()
    )
    record.write_text(
        json.dumps(drifted_grid_record, ensure_ascii=False),
        encoding="utf-8",
    )
    cue_grid_drift_result = audit_package(root)
    cue_grid_drift_codes = {issue["code"] for issue in cue_grid_drift_result["issues"]}
    assert "BOUNDARY_FINAL_DELIVERY_CUE_GRID_MISMATCH" in (cue_grid_drift_codes)
    assert "SUBTITLE_RECORD_HASH_MISMATCH" not in cue_grid_drift_codes
    assert "FINAL_REVIEW_SRT_BINDING_MISMATCH" not in cue_grid_drift_codes
    srt.write_bytes(valid_srt_bytes)
    chat_authority.write_text(valid_chat_authority, encoding="utf-8")
    record.write_text(valid_record, encoding="utf-8")

    valid_cover_bytes = cover.read_bytes()
    Image.open(ai_bg).save(cover)
    no_text_pixels = dict(rendered_text_pixels)
    no_text_pixels["final_cover_sha256"] = (
        "sha256:" + hashlib.sha256(cover.read_bytes()).hexdigest()
    )
    with (
        Image.open(cover) as no_text_cover,
        Image.open(Path(rendered_text_pixels["mask_path"])) as title_mask,
    ):
        rgb = no_text_cover.convert("RGB")
        masked = Image.composite(
            rgb,
            Image.new("RGB", rgb.size),
            title_mask.convert("L"),
        )
    no_text_pixels["masked_final_pixels_sha256"] = (
        "sha256:" + hashlib.sha256(masked.tobytes()).hexdigest()
    )
    no_text_record = json.loads(valid_record)
    no_text_generation = no_text_record["publish_staging"]["cover_generation"]
    no_text_generation["final_cover_sha256"] = no_text_pixels["final_cover_sha256"]
    no_text_generation["rendered_text_pixels"] = no_text_pixels
    no_text_generation["final_participant_verification"]["final_cover_sha256"] = no_text_pixels[
        "final_cover_sha256"
    ]
    record.write_text(
        json.dumps(no_text_record, ensure_ascii=False),
        encoding="utf-8",
    )
    no_text_manifest = json.loads(valid_manifest)
    no_text_attestation = no_text_manifest["cover_route_attestations"][0]
    no_text_attestation["final_cover_sha256"] = no_text_pixels["final_cover_sha256"]
    no_text_attestation["route_decision"] = no_text_generation["route_decision"]
    manifest_path.write_text(
        json.dumps(no_text_manifest, ensure_ascii=False),
        encoding="utf-8",
    )
    no_text_result = audit_package(root)
    assert "COVER_RENDERED_TEXT_PIXEL_ARTIFACT_MISMATCH" in {
        issue["code"] for issue in no_text_result["issues"]
    }
    cover.write_bytes(valid_cover_bytes)
    record.write_text(valid_record, encoding="utf-8")
    manifest_path.write_text(valid_manifest, encoding="utf-8")

    missing_cover_attestations = json.loads(valid_manifest)
    missing_cover_attestations.pop("cover_route_attestations")
    manifest_path.write_text(
        json.dumps(missing_cover_attestations, ensure_ascii=False),
        encoding="utf-8",
    )
    missing_cover_result = audit_package(root)
    missing_cover_codes = {issue["code"] for issue in missing_cover_result["issues"]}
    assert "MANIFEST_COVER_ATTESTATIONS_MISSING" in missing_cover_codes
    assert "MANIFEST_COVER_ATTESTATION_SET_MISMATCH" in missing_cover_codes
    assert "MANIFEST_COVER_ATTESTATION_MISSING" in missing_cover_codes
    manifest_path.write_text(valid_manifest, encoding="utf-8")

    extra_cover_attestation = json.loads(valid_manifest)
    extra = dict(extra_cover_attestation["cover_route_attestations"][0])
    extra["candidate_id"] = "auto_unexpected_cover"
    extra_cover_attestation["cover_route_attestations"].append(extra)
    manifest_path.write_text(
        json.dumps(extra_cover_attestation, ensure_ascii=False),
        encoding="utf-8",
    )
    extra_cover_result = audit_package(root)
    assert "MANIFEST_COVER_ATTESTATION_SET_MISMATCH" in {
        issue["code"] for issue in extra_cover_result["issues"]
    }
    manifest_path.write_text(valid_manifest, encoding="utf-8")

    invalid_boundary_record = json.loads(valid_record)
    invalid_boundary_record["story_contract"]["boundary_semantic_review"] = None
    invalid_boundary_record["boundary_audit"] = {
        "boundary_authority": "human_source_reviewed_end",
        "manual_end_authority": "fixture source-reviewed closure",
    }
    record.write_text(
        json.dumps(invalid_boundary_record, ensure_ascii=False),
        encoding="utf-8",
    )
    boundary_result = audit_package(root)
    boundary_codes = {issue["code"] for issue in boundary_result["issues"]}
    assert "BOUNDARY_SEMANTIC_REVIEW_NOT_PASS" in boundary_codes
    assert "HUMAN_BOUNDARY_AUTHORITY_DRIFT" in boundary_codes
    record.write_text(valid_record, encoding="utf-8")

    stale_grid_record = json.loads(valid_record)
    stale_grid_review = stale_grid_record["story_contract"]["boundary_semantic_review"]
    stale_grid_review["final_endpoint_binding"]["final_cue_grid_sha256"] = "sha256:" + "f" * 64
    stale_grid_record["boundary_audit"]["final_delivery_boundary_semantic_review"] = (
        stale_grid_review
    )
    record.write_text(
        json.dumps(stale_grid_record, ensure_ascii=False),
        encoding="utf-8",
    )
    stale_grid_result = audit_package(root)
    assert "BOUNDARY_SEMANTIC_REVIEW_NOT_PASS" in {
        issue["code"] for issue in stale_grid_result["issues"]
    }
    record.write_text(valid_record, encoding="utf-8")

    wrong_final_scope = json.loads(valid_record)
    wrong_final_scope_review = wrong_final_scope["story_contract"]["boundary_semantic_review"]
    wrong_final_scope_review["review_scope"] = "source_full_window"
    wrong_final_scope["boundary_audit"]["final_delivery_boundary_semantic_review"] = (
        wrong_final_scope_review
    )
    record.write_text(
        json.dumps(wrong_final_scope, ensure_ascii=False),
        encoding="utf-8",
    )
    wrong_final_scope_result = audit_package(root)
    assert "BOUNDARY_SEMANTIC_REVIEW_NOT_PASS" in {
        issue["code"] for issue in wrong_final_scope_result["issues"]
    }
    record.write_text(valid_record, encoding="utf-8")

    wrong_source_scope = json.loads(valid_record)
    wrong_source_scope_review = wrong_source_scope["boundary_audit"]["boundary_semantic_review"]
    wrong_source_scope_review["review_scope"] = "final_delivery"
    rebound_final_review = wrong_source_scope["story_contract"]["boundary_semantic_review"]
    rebound_final_review["source_separation_witness"]["source_review_sha256"] = (
        semantic_review_sha256(wrong_source_scope_review)
    )
    wrong_source_scope["boundary_audit"]["final_delivery_boundary_semantic_review"] = (
        rebound_final_review
    )
    record.write_text(
        json.dumps(wrong_source_scope, ensure_ascii=False),
        encoding="utf-8",
    )
    wrong_source_scope_result = audit_package(root)
    assert "BOUNDARY_SEMANTIC_REVIEW_NOT_PASS" in {
        issue["code"] for issue in wrong_source_scope_result["issues"]
    }
    record.write_text(valid_record, encoding="utf-8")

    final_review_binding_drift = json.loads(valid_record)
    final_review_binding_drift["boundary_audit"]["final_delivery_boundary_semantic_review"][
        "summary"
    ] = "unbound audit copy"
    record.write_text(
        json.dumps(final_review_binding_drift, ensure_ascii=False),
        encoding="utf-8",
    )
    final_binding_result = audit_package(root)
    assert "BOUNDARY_SEMANTIC_REVIEW_BINDING_DRIFT" in {
        issue["code"] for issue in final_binding_result["issues"]
    }
    record.write_text(valid_record, encoding="utf-8")

    invalid_delivery_coverage = json.loads(valid_record)
    invalid_delivery_coverage["boundary_audit"]["final_end_ms"] = 3_999
    record.write_text(
        json.dumps(invalid_delivery_coverage, ensure_ascii=False),
        encoding="utf-8",
    )
    delivery_coverage_result = audit_package(root)
    assert "BOUNDARY_DELIVERY_COVERAGE_INVALID" in {
        issue["code"] for issue in delivery_coverage_result["issues"]
    }
    record.write_text(valid_record, encoding="utf-8")

    for invalid_maximum_tail_pad_ms in (401, 400.0):
        invalid_tail_pad = json.loads(valid_record)
        invalid_tail_pad["boundary_audit"]["tail_pad_coverage_bridge"]["maximum_tail_pad_ms"] = (
            invalid_maximum_tail_pad_ms
        )
        record.write_text(
            json.dumps(invalid_tail_pad, ensure_ascii=False),
            encoding="utf-8",
        )
        invalid_tail_pad_result = audit_package(root)
        assert "BOUNDARY_DELIVERY_COVERAGE_INVALID" in {
            issue["code"] for issue in invalid_tail_pad_result["issues"]
        }
    record.write_text(valid_record, encoding="utf-8")

    for invalid_closure_ms, bridge_status in (
        (3_999, "USED"),
        (4_000.0, "NOT_NEEDED"),
    ):
        invalid_closure_lower_bound = json.loads(valid_record)
        invalid_closure_lower_bound["boundary_audit"]["tail_pad_coverage_bridge"].update(
            {
                "status": bridge_status,
                "closure_lower_bound_ms": invalid_closure_ms,
            }
        )
        record.write_text(
            json.dumps(invalid_closure_lower_bound, ensure_ascii=False),
            encoding="utf-8",
        )
        invalid_closure_result = audit_package(root)
        assert "BOUNDARY_DELIVERY_COVERAGE_INVALID" in {
            issue["code"] for issue in invalid_closure_result["issues"]
        }
    record.write_text(valid_record, encoding="utf-8")

    invalid_source_witness = json.loads(valid_record)
    tampered_final_review = invalid_source_witness["story_contract"]["boundary_semantic_review"]
    tampered_final_review["source_separation_witness"]["source_review_sha256"] = (
        "sha256:" + "0" * 64
    )
    invalid_source_witness["boundary_audit"]["final_delivery_boundary_semantic_review"] = (
        tampered_final_review
    )
    record.write_text(
        json.dumps(invalid_source_witness, ensure_ascii=False),
        encoding="utf-8",
    )
    invalid_witness_result = audit_package(root)
    assert "BOUNDARY_SOURCE_SEPARATION_WITNESS_INVALID" in {
        issue["code"] for issue in invalid_witness_result["issues"]
    }
    record.write_text(valid_record, encoding="utf-8")

    for witness_field, drifted_value in (
        ("source_request_sha256", "sha256:" + "1" * 64),
        ("source_cue_grid_sha256", "sha256:" + "2" * 64),
        ("source_recommended_end_ms", 4_000.0),
        ("source_final_start_ms", 0.0),
        ("source_final_end_ms", 4_000.0),
    ):
        invalid_source_binding = json.loads(valid_record)
        rebound_review = invalid_source_binding["story_contract"]["boundary_semantic_review"]
        rebound_review["source_separation_witness"][witness_field] = drifted_value
        invalid_source_binding["boundary_audit"]["final_delivery_boundary_semantic_review"] = (
            rebound_review
        )
        record.write_text(
            json.dumps(invalid_source_binding, ensure_ascii=False),
            encoding="utf-8",
        )
        invalid_source_binding_result = audit_package(root)
        assert "BOUNDARY_SOURCE_SEPARATION_WITNESS_INVALID" in {
            issue["code"] for issue in invalid_source_binding_result["issues"]
        }
    record.write_text(valid_record, encoding="utf-8")

    invalid_delivery_local_endpoint = json.loads(valid_record)
    nonlocal_final_review = invalid_delivery_local_endpoint["story_contract"][
        "boundary_semantic_review"
    ]
    nonlocal_final_review["final_endpoint_binding"]["final_start_ms"] = 1
    invalid_delivery_local_endpoint["boundary_audit"]["final_delivery_boundary_semantic_review"] = (
        nonlocal_final_review
    )
    record.write_text(
        json.dumps(invalid_delivery_local_endpoint, ensure_ascii=False),
        encoding="utf-8",
    )
    invalid_local_result = audit_package(root)
    assert "BOUNDARY_FINAL_DELIVERY_ENDPOINT_INVALID" in {
        issue["code"] for issue in invalid_local_result["issues"]
    }
    record.write_text(valid_record, encoding="utf-8")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["cover_route_attestations"][0]["final_cover_sha256"] = "sha256:" + "0" * 64
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    cover_result = audit_package(root)
    assert cover_result["passed"] is False
    assert "MANIFEST_COVER_ATTESTATION_DRIFT" in {issue["code"] for issue in cover_result["issues"]}
    manifest["cover_route_attestations"][0]["final_cover_sha256"] = recorded_generation[
        "final_cover_sha256"
    ]
    manifest["items"][0]["title"] = "【李豆沙】旧标题"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    title_result = audit_package(root)
    assert title_result["passed"] is False
    assert "MANIFEST_ITEM_TITLE_DRIFT" in {issue["code"] for issue in title_result["issues"]}
    manifest["items"][0]["title"] = "【李豆沙】南町当面追问最最最最喜欢"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

    # Terminal-evidence refresh regression: the three final package surfaces
    # are independently bound.  A stale chat/final-review/boundary carry must
    # expose all three failures together, while the exact current receipts
    # return the same production-shaped package to a clean audit.
    current_chat = chat_authority.read_text(encoding="utf-8")
    stale_chat = json.loads(current_chat)
    stale_chat["final_text_srt_sha256"] = "0" * 64
    stale_chat["final_speaker_srt_sha256"] = "0" * 64
    stale_chat["final_review_audit"]["reviewed_srt_sha256"] = "sha256:" + "0" * 64
    stale_boundary = stale_chat["final_review_audit"]["boundary_semantic_review"]
    stale_boundary["cue_grid_sha256"] = "sha256:" + "0" * 64
    stale_boundary["final_endpoint_binding"]["final_cue_grid_sha256"] = "sha256:" + "0" * 64
    stale_boundary["final_endpoint_binding"]["semantic_cue_grid_sha256"] = "sha256:" + "0" * 64
    chat_authority.write_text(json.dumps(stale_chat, ensure_ascii=False), encoding="utf-8")
    stale_record = json.loads(valid_record)
    stale_record["artifact_hashes"]["chat_authority_audit_sha256"] = (
        "sha256:" + hashlib.sha256(chat_authority.read_bytes()).hexdigest()
    )
    stale_record["story_contract"]["boundary_semantic_review"] = stale_boundary
    stale_record["boundary_audit"]["final_delivery_boundary_semantic_review"] = stale_boundary
    record.write_text(json.dumps(stale_record, ensure_ascii=False), encoding="utf-8")
    stale_terminal_result = audit_package(root)
    stale_terminal_codes = {issue["code"] for issue in stale_terminal_result["issues"]}
    assert {
        "SUBTITLE_SPEAKER_SRT_CHAT_HASH_MISMATCH",
        "FINAL_REVIEW_SRT_BINDING_MISMATCH",
        "BOUNDARY_FINAL_DELIVERY_CUE_GRID_MISMATCH",
    } <= stale_terminal_codes
    chat_authority.write_text(current_chat, encoding="utf-8")
    record.write_text(valid_record, encoding="utf-8")
    refreshed_terminal_result = audit_package(root)
    refreshed_terminal_codes = {issue["code"] for issue in refreshed_terminal_result["issues"]}
    assert not {
        "SUBTITLE_SPEAKER_SRT_CHAT_HASH_MISMATCH",
        "FINAL_REVIEW_SRT_BINDING_MISMATCH",
        "BOUNDARY_FINAL_DELIVERY_CUE_GRID_MISMATCH",
    } & refreshed_terminal_codes

    srt.write_text(
        "1\n00:00:00,000 --> 00:00:04,000\n大恩老师说非常亚撒西\n",
        encoding="utf-8",
    )
    result = audit_package(root)
    codes = {issue["code"] for issue in result["issues"]}
    assert result["passed"] is False
    assert "STORY_CONTRACT_SUBTITLE_HASH_DRIFT" in codes
    assert "NANCHO_ALIAS_UNRESOLVED" in codes


def test_story_contract_is_mandatory_by_date_and_cover_alias_cannot_drift(
    tmp_path: Path,
):
    root = tmp_path / "pkg"
    root.mkdir()
    (root / "review_manifest.json").write_text(
        json.dumps(
            {
                "date": "2026-07-22",
                "status": "finished_review_package_no_upload_pending_human_review",
                "items": [{"stem": "legacy-without-contract"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    result = audit_package(root)
    codes = {issue["code"] for issue in result["issues"]}
    assert result["passed"] is False
    assert "REVIEW_PACKAGE_UPLOAD_POLICY_INVALID" in codes
    assert "REVIEW_PACKAGE_RUN_MODE_INVALID" in codes
    assert "STORY_CONTRACT_RECORD_MISSING" in codes

    transcript = "南町nightin说非常亚撒西"
    srt = _write(
        root / "cover-drift.srt",
        f"1\n00:00:00,000 --> 00:00:04,000\n{transcript}\n",
    )
    scorecard = normalize_selection_scorecard(
        {
            "tier": 1,
            "tier_basis": "relationship_chain",
            "tier_reason": "联动关系与反转",
            "tier_evidence_cues": [1],
            "dimensions": {
                "lidousha_centrality": 4,
                "stance_intensity": 3,
                "audience_salience": 4,
                "relationship_interaction": 4,
                "persona_reversal": 3,
                "comedic_payoff": 3,
                "self_contained": 4,
            },
            "uncertainty_penalty": 0,
            "fatigue_penalty": 0,
        },
        start_cue=1,
        end_cue=1,
    )
    contract = build_story_contract(
        candidate_id="cover-drift",
        selection_hook="南町当面追问李豆沙",
        transcript_text=transcript,
        selection_scorecard=scorecard,
        session_relation_authority={
            "state": "CONFIRMED",
            "participants": ["lidousha", "nancho"],
        },
    )
    binding = {
        key: contract[key]
        for key in (
            "schema_version",
            "relation_state",
            "participants",
            "source_media_sha256s",
            "cover_fallback_mode",
        )
    }
    record = root / "cover-drift.record.json"
    record.write_text(
        json.dumps(
            {
                "story_contract": contract,
                "publish_staging": {
                    "cover_text": "大恩当面追问",
                    "cover_generation": {
                        "cover_text": "大恩当面追问",
                        "rendered_lines": ["大恩当面追问"],
                        "story_contract": binding,
                    },
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (root / "review_manifest.json").write_text(
        json.dumps(
            {
                "date": "2026-07-22",
                "run_mode": "RECOVERY_REVIEW",
                "upload_allowed": False,
                "items": [
                    {
                        "stem": "cover-drift",
                        "title": "【李豆沙】南町当面追问",
                        "subtitle_srt": srt.name,
                        "record": record.name,
                        "ai_cover_generated": True,
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    result = audit_package(root)
    codes = {issue["code"] for issue in result["issues"]}
    assert result["passed"] is False
    assert "NANCHO_ALIAS_UNRESOLVED" in codes


def test_audit_blocks_package_with_extended_invalid_review_draft_status(tmp_path: Path):
    root = tmp_path / "pkg"
    root.mkdir()
    (root / "review_manifest.json").write_text(
        json.dumps(
            {
                "status": "invalid_review_draft_song_boundary_subtitle_cover_failed",
                "items": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = audit_package(root)

    codes = {issue["code"] for issue in result["issues"]}
    assert "PACKAGE_MARKED_INVALID_REVIEW_DRAFT" in codes
    assert result["passed"] is False
    assert result["blocking_issue_count"] >= 1


def test_audit_blocks_package_with_invalid_redo_required_marker(tmp_path: Path):
    root = tmp_path / "pkg"
    root.mkdir()
    (root / "review_manifest.json").write_text(
        json.dumps({"status": "finished", "items": []}, ensure_ascii=False),
        encoding="utf-8",
    )
    (root / "INVALID_REDO_REQUIRED.json").write_text(
        json.dumps(
            {
                "status": "invalid_review_draft_song_boundary_subtitle_cover_failed",
                "reason": "Ivan review: source clip cuts the song",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = audit_package(root)

    codes = {issue["code"] for issue in result["issues"]}
    assert "PACKAGE_MARKED_INVALID_REDO_REQUIRED" in codes
    assert result["passed"] is False


def test_legacy_retry_receipt_keeps_historical_package_auditable():
    """A retry receipt frozen before ASR-derived owners were unbound asserted
    whole-set identity (strictly stronger than today's rule).  It must remain
    valid; a new-format contract may not fall back to that legacy shape."""

    from src.autoslice.review_package_owner_audit import _retry_verification_valid

    scope = {"scope_sha256": "sha256:" + "b" * 64}
    legacy = {
        "owner_set_sha256": "sha256:" + "c" * 64,
        "boundary_retry_owner_contract_verification": {
            "status": "PASS",
            "expected_contract_sha256": "sha256:" + "d" * 64,
            "owner_set_sha256": "sha256:" + "c" * 64,
            "owner_eligibility_scope_sha256": scope["scope_sha256"],
        },
    }
    assert _retry_verification_valid(frozen=legacy, owner_scope=scope) is True

    drifted = {
        **legacy,
        "boundary_retry_owner_contract_verification": {
            **legacy["boundary_retry_owner_contract_verification"],
            "owner_set_sha256": "sha256:" + "0" * 64,
        },
    }
    assert _retry_verification_valid(frozen=drifted, owner_scope=scope) is False

    modern_contract_legacy_receipt = {
        **legacy,
        "deterministic_owner_set_sha256": "sha256:" + "e" * 64,
    }
    assert (
        _retry_verification_valid(frozen=modern_contract_legacy_receipt, owner_scope=scope) is False
    )


# 合并说明：以下为 ft-a8600994 分支独有的验收面测试。主线 df4afed 窄移植同一
# 终端投影特性时用了 _TP_* 具名常量 + 四条金丝雀的写法,与 ft 这条内联字面量写法
# 在同一段文本里交织冲突。取主线那段(覆盖更全:fail-closed 参数化/跨界 cue 证据/
# 不放宽普通短收尾),并把 ft 这条独有用例原样补回,两边覆盖都不丢。
def _projected_source_boundary_review(candidate_id: str) -> dict[str, object]:
    reviewed_end_ms = 67_760
    closure_end_ms = 67_670
    authority_sha256 = "sha256:" + "a" * 64
    grid_sha256 = "sha256:" + "e" * 64
    closure_text_sha256 = "sha256:" + "f" * 64
    binding = {
        "kind": "reviewed_exact_interval_terminal_projection",
        "cue_index": 1,
        "cue_start_ms": 64_440,
        "cue_end_ms": closure_end_ms,
        "cue_text_sha256": closure_text_sha256,
        "reviewed_endpoint_ms": reviewed_end_ms,
        "terminal_drift_ms": 90,
        "max_terminal_drift_ms": 250,
        "crossing_witness_cue_index": 2,
        "crossing_witness_start_ms": closure_end_ms,
        "crossing_witness_end_ms": 69_210,
        "crossing_witness_text_sha256": "sha256:" + "b" * 64,
        "authority_sha256": authority_sha256,
        "cue_grid_sha256": grid_sha256,
    }
    review = _passing_boundary_review(
        candidate_id,
        end_ms=reviewed_end_ms,
        cue_grid_digest=grid_sha256,
    )
    review.update(
        {
            "evidence_cue_indexes": [1, 2],
            "boundary_search_scope": {
                "reviewed_exact_interval_projection": {
                    "schema_version": ("reviewed-exact-interval-terminal-projection-scope.v1"),
                    "authority_sha256": authority_sha256,
                    "reviewed_endpoint_ms": reviewed_end_ms,
                    "max_terminal_drift_ms": 250,
                }
            },
            "recommendation_relaxations": [binding],
            "selected_terminal_projection_binding": binding,
        }
    )
    endpoint = review["final_endpoint_binding"]
    assert isinstance(endpoint, dict)
    endpoint.update(
        {
            "final_snapped_end_ms": closure_end_ms,
            "final_start_ms": 9_630,
            "final_end_ms": reviewed_end_ms,
            "closure_text_sha256": closure_text_sha256,
        }
    )
    return review


def test_reviewed_terminal_projection_bridge_survives_package_audit(
    tmp_path: Path,
):
    stem = "candidate-reviewed-terminal-projection"
    source_review = _projected_source_boundary_review(stem)
    subtitle = _write(
        tmp_path / f"{stem}.srt",
        "1\n00:00:00,000 --> 00:00:58,130\n审定收尾\n",
    )
    final_cues = parse_srt_cues(subtitle.read_text(encoding="utf-8"))
    final_review = _passing_boundary_review(
        stem,
        end_ms=58_130,
        review_scope="final_delivery",
        source_review=source_review,
        cue_grid_digest=cue_grid_sha256(final_cues),
        closure_text=final_cues[-1].text,
    )
    record = {
        "boundary_audit": {
            "boundary_authority": ("correlated_semantic_review_plus_deterministic_guards"),
            "boundary_semantic_review": source_review,
            "final_delivery_boundary_semantic_review": final_review,
            "final_start_ms": 9_630,
            "snapped_sentence_end_ms": 67_670,
            "final_end_ms": 67_760,
            "boundary_selection_lower_bound_ms": 67_670,
            "delivery_coverage_lower_bound_ms": 67_760,
            "tail_pad_coverage_bridge": {
                "status": "USED",
                "closure_lower_bound_ms": 67_670,
                "delivery_lower_bound_ms": 67_760,
                "maximum_tail_pad_ms": 400,
            },
            "delivery_coverage_verification": {
                "status": "PASS",
                "failure": None,
            },
        }
    }

    def run_audit() -> set[str]:
        issues: list[dict] = []
        audit_boundary_contract(
            issue_adder=lambda rows, code, **fields: rows.append({"code": code, **fields}),
            issues=issues,
            stem=stem,
            record_path=tmp_path / f"{stem}.record.json",
            subtitle_path=subtitle,
            exact_final_review={"boundary_semantic_review": final_review},
            record=record,
            story_contract={"boundary_semantic_review": final_review},
            required=True,
            is_song=False,
        )
        return {issue["code"] for issue in issues}

    assert "BOUNDARY_DELIVERY_COVERAGE_INVALID" not in run_audit()

    record["boundary_audit"]["tail_pad_coverage_bridge"]["closure_lower_bound_ms"] = 67_760
    assert "BOUNDARY_DELIVERY_COVERAGE_INVALID" in run_audit()

    record["boundary_audit"]["tail_pad_coverage_bridge"]["closure_lower_bound_ms"] = 67_670
    source_review["evidence_cue_indexes"] = [1]
    assert "BOUNDARY_SEMANTIC_REVIEW_NOT_PASS" in run_audit()
