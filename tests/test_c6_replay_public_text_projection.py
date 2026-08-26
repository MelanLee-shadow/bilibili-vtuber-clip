"""Committed coverage for the C6 public-text derived projection."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import src.autoslice.c6_replay_public_text_projection as projection
from src.autoslice.candidate_public_text_surface_authority import (
    load_candidate_public_text_surface_authority,
)

ROOT = Path(__file__).parents[1]
AUTHORITY = load_candidate_public_text_surface_authority(
    projection.CANDIDATE_ID, root=ROOT
)
assert AUTHORITY is not None


def _sha(value: object) -> str:
    return "sha256:" + hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _story(*, hook: str, transcript: str, source_fact: object | None = None) -> dict[str, object]:
    story: dict[str, object] = {
        "candidate_id": projection.CANDIDATE_ID,
        "selection_hook": hook,
        "transcript_sha256": transcript,
        "input_audits": [{"text_sha256": _sha(hook)}, {"text_sha256": transcript}],
        "clip_context_binding": {"context_sha256": projection.EXPECTED_CLIP_CONTEXT_SHA256},
        "source_media_sha256s": [projection.SOURCE_MEDIA_SHA256]
        if hasattr(projection, "SOURCE_MEDIA_SHA256")
        else ["sha256:" + "a" * 64],
        "fixed_metadata": "faithful-story-fixture",
    }
    if source_fact is not None:
        story["source_fact_review"] = source_fact
    return story


def _receipt(old: dict[str, object], fresh: dict[str, object]) -> dict[str, object]:
    receipt: dict[str, object] = {
        "schema_version": projection.SCHEMA_VERSION,
        "status": "CONSUMED",
        "result_type": "DERIVED_REPLAY_CONSUMPTION",
        "candidate_id": projection.CANDIDATE_ID,
        "recording_date": projection.RECORDING_DATE,
        "root_public_text_authority_sha256": projection.EXPECTED_AUTHORITY_SHA256,
        "root_authority_consumption_sha256": _sha({"fixture": "root-consumption"}),
        "old_story_contract_sha256": _sha(old),
        "fresh_story_contract_sha256": _sha(fresh),
        "clip_context_sha256": projection.EXPECTED_CLIP_CONTEXT_SHA256,
        "baseline_sha256": projection.EXPECTED_BASELINE_SHA256,
        "decision_ledger_sha256": projection.EXPECTED_DECISION_LEDGER_SHA256,
        "truth_diff_sha256": projection.EXPECTED_TRUTH_DIFF_SHA256,
        "pipeline_srt_sha256": projection.EXPECTED_PIPELINE_SRT_SHA256,
        "old_transcript_sha256": projection.EXPECTED_OLD_TRANSCRIPT_SHA256,
        "fresh_transcript_sha256": projection.EXPECTED_FRESH_TRANSCRIPT_SHA256,
        "fresh_artifact_hashes": {
            "story_contract_sha256": projection.EXPECTED_FRESH_STORY_SHA256,
            "selection_hook_sha256": projection.EXPECTED_RESOLVED_HOOK_SHA256,
            "title_sha256": projection.EXPECTED_TITLE_SHA256,
            "cover_lines_sha256": projection.EXPECTED_COVER_LINES_SHA256,
            "reviewed_baseline_sha256": projection.EXPECTED_BASELINE_SHA256,
        },
        "allowed_canonical_diff_paths": sorted(projection.ALLOWED_DIFF_PATHS),
        "cue17_transition": {"cue": 17, "before": projection.EXPECTED_OLD_CUE17, "after": projection.EXPECTED_NEW_CUE17},
        "subtitle_text_mutation_authorized": False,
        "speaker_label_mutation_authorized": False,
        "upload_authorized": False,
        "registry_hold_released": False,
    }
    receipt["receipt_sha256"] = projection._receipt_seal(receipt)
    return receipt


@pytest.fixture
def fixture_pair(monkeypatch: pytest.MonkeyPatch) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    old = _story(hook=AUTHORITY.input_selection_hook, transcript="sha256:" + "1" * 64)
    fresh = _story(hook=AUTHORITY.resolved_selection_hook, transcript="sha256:" + "2" * 64)
    monkeypatch.setattr(projection, "EXPECTED_PERSISTED_STORY_SHA256", _sha(old))
    monkeypatch.setattr(projection, "EXPECTED_FRESH_STORY_SHA256", _sha(fresh))
    monkeypatch.setattr(projection, "EXPECTED_OLD_TRANSCRIPT_SHA256", old["transcript_sha256"])
    monkeypatch.setattr(projection, "EXPECTED_FRESH_TRANSCRIPT_SHA256", fresh["transcript_sha256"])
    return old, fresh, _receipt(old, fresh)


def test_exact_positive_composition_accepts_post_source_fact_normalization(
    fixture_pair: tuple[dict[str, object], dict[str, object], dict[str, object]],
) -> None:
    _old, fresh, receipt = fixture_pair
    with_review = {**fresh, "source_fact_review": {"provider": "downstream"}}
    assert projection.validate_c6_replay_public_text_consumption_for_fresh_story(
        receipt, fresh_story=with_review, authority=AUTHORITY
    ) == receipt


def test_receipt_self_seal_and_every_bound_field_are_checked(
    fixture_pair: tuple[dict[str, object], dict[str, object], dict[str, object]],
) -> None:
    old, fresh, receipt = fixture_pair
    fields = [
        "schema_version", "status", "result_type", "candidate_id", "recording_date",
        "root_public_text_authority_sha256", "root_authority_consumption_sha256",
        "old_story_contract_sha256", "fresh_story_contract_sha256", "clip_context_sha256",
        "baseline_sha256", "decision_ledger_sha256", "truth_diff_sha256", "pipeline_srt_sha256",
        "old_transcript_sha256", "fresh_transcript_sha256", "fresh_artifact_hashes",
        "allowed_canonical_diff_paths", "cue17_transition", "subtitle_text_mutation_authorized",
        "speaker_label_mutation_authorized", "upload_authorized", "registry_hold_released",
    ]
    for field in fields:
        tampered = copy.deepcopy(receipt)
        if isinstance(tampered[field], dict):
            tampered[field]["tampered"] = True
        elif isinstance(tampered[field], list):
            tampered[field] = list(tampered[field]) + ["/tampered"]
        elif isinstance(tampered[field], bool):
            tampered[field] = True
        else:
            tampered[field] = "tampered"
        with pytest.raises(projection.C6ReplayPublicTextProjectionError):
            projection.validate_c6_replay_public_text_consumption_for_fresh_story(
                tampered, fresh_story=fresh, authority=AUTHORITY
            )
    resealed = copy.deepcopy(receipt)
    resealed["baseline_sha256"] = "sha256:" + "0" * 64
    resealed["receipt_sha256"] = projection._receipt_seal(resealed)
    with pytest.raises(projection.C6ReplayPublicTextProjectionError):
        projection.validate_c6_replay_public_text_consumption_for_fresh_story(
            resealed, fresh_story=fresh, authority=AUTHORITY
        )
    for story, key in ((fresh, "title"), (fresh, "cover"), (fresh, "cue"), (old, "metadata")):
        changed = copy.deepcopy(story)
        changed[key] = "tampered"
        with pytest.raises(projection.C6ReplayPublicTextProjectionError):
            projection.validate_c6_replay_public_text_consumption_for_fresh_story(
                receipt, fresh_story=changed, authority=AUTHORITY
            )


def test_full_validator_recomputes_root_consumption_and_story_identity(
    fixture_pair: tuple[dict[str, object], dict[str, object], dict[str, object]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old, fresh, receipt = fixture_pair
    monkeypatch.setattr(
        "src.autoslice.candidate_public_text_surface_authority.consume_candidate_public_text_surface_authority",
        lambda *args, **kwargs: {"fixture": "root-consumption"},
    )
    assert projection.validate_c6_replay_public_text_consumption(
        receipt, old_story=old, fresh_story=fresh, authority=AUTHORITY
    ) == receipt
    tampered = copy.deepcopy(receipt)
    tampered["root_authority_consumption_sha256"] = _sha({"fixture": "other"})
    tampered["receipt_sha256"] = projection._receipt_seal(tampered)
    with pytest.raises(projection.C6ReplayPublicTextProjectionError):
        projection.validate_c6_replay_public_text_consumption(
            tampered, old_story=old, fresh_story=fresh, authority=AUTHORITY
        )


def test_committed_truth_lane_seals_are_real_and_other_candidate_is_strict() -> None:
    base = ROOT / "assets/lidousha/reviewed_subtitle_baselines"
    expected = {
        "auto_120032_753_816.reviewed.srt": projection.EXPECTED_BASELINE_SHA256,
        "auto_120032_753_816.pipeline-diagnostic.srt": projection.EXPECTED_PIPELINE_SRT_SHA256,
        "auto_120032_753_816.operator-decisions.v3.json": projection.EXPECTED_DECISION_LEDGER_SHA256,
        "auto_120032_753_816.operator-truth-diff.v2.json": projection.EXPECTED_TRUTH_DIFF_SHA256,
    }
    for name, digest in expected.items():
        assert "sha256:" + hashlib.sha256((base / name).read_bytes()).hexdigest() == digest
    other = SimpleNamespace(candidate_id="auto_other", date=projection.RECORDING_DATE)
    with pytest.raises(projection.C6ReplayPublicTextProjectionError, match="PLAN_IDENTITY"):
        projection.build_c6_replay_public_text_resolver(plan=other, root=ROOT)
