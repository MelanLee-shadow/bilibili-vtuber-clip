from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

import src.autoslice.reviewed_subtitle_baseline_registry as baseline_registry
import src.autoslice.source_fact_review as source_fact_review
import src.autoslice.source_fact_staging as source_fact_staging
from src.autoslice.addressee_attribution import SpeakerEvidenceResult, SpeakerEvidenceState
from src.autoslice.manual_title_keep_authority import (
    ManualTitleKeepAuthorityError,
    ManualTitleKeepAuthorityV1,
    bytes_sha256,
    canonical_sha256,
    text_sha256,
    validate_manual_title_keep_authority,
    validate_manual_title_keep_authority_document,
)
from src.autoslice.publish_staging import _stage_publish_draft
from src.autoslice.review_evidence import SourceCue
from src.autoslice.source_fact_review import (
    authorize_manual_title_keep,
    resolve_source_fact_entity_context,
    source_fact_review_passes,
    validate_source_fact_review,
)


ROOT = Path(__file__).resolve().parents[2]
CANDIDATE_ID = "auto_223750_578_734"
AUTHORITY_PATH = (
    ROOT / "assets/lidousha/authorities" / f"{CANDIDATE_ID}.manual-title-keep-authority.v1.json"
)
SRT_PATH = ROOT / "assets/lidousha/reviewed_subtitle_baselines" / f"{CANDIDATE_ID}.reviewed.srt"


@pytest.fixture(autouse=True)
def _allow_precommit_exact_interval_authority(monkeypatch: pytest.MonkeyPatch) -> None:
    """The repository seal is covered separately; these assets are not committed yet."""

    monkeypatch.setattr(
        baseline_registry,
        "require_repository_asset_authority",
        lambda **_kwargs: None,
    )


def _document() -> dict[str, object]:
    return json.loads(AUTHORITY_PATH.read_text(encoding="utf-8"))


def _bundle(document: dict[str, object] | None = None) -> ManualTitleKeepAuthorityV1:
    value = _document() if document is None else document
    return ManualTitleKeepAuthorityV1(
        document=value,
        repo_path=AUTHORITY_PATH.relative_to(ROOT),
        file_sha256=bytes_sha256(AUTHORITY_PATH.read_bytes()),
    )


def _runtime(document: dict[str, object] | None = None) -> dict[str, object]:
    value = _document() if document is None else document
    replay = value["replay"]
    approved = value["approved_title"]
    assert isinstance(replay, dict) and isinstance(approved, dict)
    blocked = replay["blocked_source_fact_review"]
    assert isinstance(blocked, dict)
    return {
        "candidate_id": CANDIDATE_ID,
        "title": approved["value"],
        "selection_hook": blocked["original_selection_hook"],
        "final_transcript": replay["final_transcript"],
        "clip_context_prompt": replay["clip_context_prompt"],
        "selection_scorecard": replay["selection_scorecard"],
        "final_reviewed_srt_path": SRT_PATH,
        "speaker_evidence": blocked["speaker_evidence"],
        "entity_context": resolve_source_fact_entity_context(
            candidate_id=CANDIDATE_ID,
            final_reviewed_srt_path=SRT_PATH,
        ),
    }


def _rehash(document: dict[str, object]) -> None:
    document.pop("authority_sha256", None)
    document["authority_sha256"] = canonical_sha256(document)


def test_exact_story_keep_authority_passes_without_title_mutation() -> None:
    document = validate_manual_title_keep_authority_document(_document())
    consumption = validate_manual_title_keep_authority(_bundle(document), **_runtime(document))
    blocked = document["replay"]["blocked_source_fact_review"]
    resolved = authorize_manual_title_keep(blocked, consumption=consumption)

    assert source_fact_review_passes(resolved)
    assert resolved["decision"] == "PASS_WITH_RECORDED_DISSENT"
    assert resolved["final_title"] == document["approved_title"]["value"]
    assert resolved["blocked_source_fact_review"] == blocked
    assert resolved["recorded_dissent"]["status"] == "RECORDED_NON_BLOCKING"
    assert resolved["manual_title_keep_authority_consumption"] == consumption


def test_fresh_clip_context_is_diagnostic_only_and_hash_bound() -> None:
    document = validate_manual_title_keep_authority_document(_document())
    replay = document["replay"]
    assert isinstance(replay, dict)
    sealed_prompt = str(replay["clip_context_prompt"])
    consumptions: list[dict[str, object]] = []
    receipts: list[dict[str, object]] = []

    for fresh_prompt in (
        "fresh ASR grid A: 小心呆呆鸟",
        "fresh ASR grid B: 南町，NT往右走了",
    ):
        consumption = validate_manual_title_keep_authority(
            _bundle(document),
            **{**_runtime(document), "clip_context_prompt": fresh_prompt},
        )
        resolved = authorize_manual_title_keep(
            replay["blocked_source_fact_review"], consumption=consumption
        )
        assert source_fact_review_passes(resolved)
        assert resolved["decision"] == "PASS_WITH_RECORDED_DISSENT"
        assert consumption["clip_context_prompt_sha256"] == text_sha256(sealed_prompt)
        assert consumption["diagnostic_clip_context_prompt_sha256"] == text_sha256(fresh_prompt)
        assert consumption["diagnostic_clip_context_matches_adjudication"] is False
        consumptions.append(consumption)
        receipts.append(resolved)

    assert (
        consumptions[0]["diagnostic_clip_context_prompt_sha256"]
        != consumptions[1]["diagnostic_clip_context_prompt_sha256"]
    )
    assert receipts[0]["receipt_sha256"] != receipts[1]["receipt_sha256"]


@pytest.mark.parametrize("mutation", ["missing", "tampered"])
def test_sealed_adjudication_clip_context_drift_fails_closed(mutation: str) -> None:
    document = _document()
    replay = document["replay"]
    assert isinstance(replay, dict)
    if mutation == "missing":
        replay.pop("clip_context_prompt")
        expected = "REPLAY_SCHEMA_INVALID"
    else:
        replay["clip_context_prompt"] = str(replay["clip_context_prompt"]) + "\n篡改"
        replay["clip_context_prompt_sha256"] = text_sha256(str(replay["clip_context_prompt"]))
        expected = "SEALED_ADJUDICATION_CONTEXT_MISMATCH"
    _rehash(document)

    with pytest.raises(ManualTitleKeepAuthorityError, match=expected):
        validate_manual_title_keep_authority_document(document)


def test_machine_proposal_is_distinct_and_unauthorized() -> None:
    document = _document()
    approved = document["approved_title"]
    machine = document["displayed_machine_title_snapshot"]
    blocked = document["blocked_source_fact"]
    assert machine["value"] == ("【李豆沙】莉亚求小李“就算你是狼也放过我”，结伴后小李突然连声道歉")
    assert machine["sha256"] == (
        "sha256:7fa74276ea79db40119ac18fa2de998d8d1cfb8a7c0fb4fe5a49aa5420391c91"
    )
    assert machine["source"] == {
        "artifact_id": (
            "vtuber-reproduce/run/20260811-e4249d4/auto_223750_578_734.recut.publish.json"
        ),
        "artifact_sha256": (
            "sha256:6c6fee6d9df1d7ba8693ac48c44fc9da87db44a8c4f9eb37d41142ef379d68c2"
        ),
        "json_pointer": "/source_fact_review/passes/0/changed_surfaces/1/after",
        "source_fact_receipt_sha256": (
            "sha256:8a42ee978768d4db17068989cdb990b8d00bc8ce05cdf8d26c0670b566b995ce"
        ),
    }
    assert approved["sha256"] == text_sha256(approved["value"])
    assert blocked["proposed_title_sha256"] == text_sha256(blocked["proposed_title"])
    assert blocked["proposed_title_sha256"] != approved["sha256"]
    with pytest.raises(ManualTitleKeepAuthorityError, match="RUNTIME_BINDING_MISMATCH"):
        validate_manual_title_keep_authority(
            _bundle(document),
            **{**_runtime(document), "title": blocked["proposed_title"]},
        )


@pytest.mark.parametrize(
    ("mutation", "error"),
    [
        (
            lambda d: d["blocked_source_fact"].__setitem__("finding_class", "ACTOR_CONTRADICTION"),
            "FINDING_SCOPE_INVALID",
        ),
        (
            lambda d: d["blocked_source_fact"].__setitem__("span", "突然连声道歉"),
            "FINDING_SCOPE_INVALID",
        ),
        (
            lambda d: d["blocked_source_fact"].__setitem__("proposed_title", "【李豆沙】任意标题"),
            "PROPOSED_TITLE_HASH_MISMATCH",
        ),
        (
            lambda d: d["blocked_source_fact"].__setitem__("receipt_sha256", "sha256:" + "0" * 64),
            "BLOCKED_RECEIPT_BINDING_MISMATCH",
        ),
        (
            lambda d: d["displayed_machine_title_snapshot"].pop("value"),
            "MACHINE_TITLE_SNAPSHOT_SCHEMA_INVALID",
        ),
        (
            lambda d: d.__setitem__("candidate_id", "auto_reused_000_001"),
            "SOURCE_ENTITY_BINDING_MISMATCH",
        ),
    ],
)
def test_authority_scope_drift_fails_closed(mutation, error: str) -> None:
    document = _document()
    mutation(document)
    _rehash(document)
    with pytest.raises(ManualTitleKeepAuthorityError, match=error):
        validate_manual_title_keep_authority_document(document)


@pytest.mark.parametrize(
    "verdict",
    ["WRONG_ADDRESSEE", "ACTOR_CONTRADICTION", "EVENT_CONTRADICTION", "TEMPORAL_CONTRADICTION"],
)
def test_unrelated_blocking_contradictions_cannot_be_resolved(verdict: str) -> None:
    document = _document()
    review = document["replay"]["blocked_source_fact_review"]
    review["passes"][0]["addressee_attribution"][0]["verdict"] = verdict
    body = dict(review)
    body.pop("receipt_sha256")
    review["receipt_sha256"] = canonical_sha256(body)
    document["blocked_source_fact"]["receipt_sha256"] = review["receipt_sha256"]
    _rehash(document)
    with pytest.raises(ManualTitleKeepAuthorityError, match="UNMATCHED_BLOCKING_FINDING"):
        validate_manual_title_keep_authority_document(document)


def test_hard_actor_contradiction_cannot_be_relabelled_as_compression_hedge() -> None:
    document = _document()
    approved = document["approved_title"]["value"]
    hard_rewrite = approved.replace("莉娅求小李", "小李求莉娅", 1)
    blocked_meta = document["blocked_source_fact"]
    blocked_meta["proposed_title"] = hard_rewrite
    blocked_meta["proposed_title_sha256"] = text_sha256(hard_rewrite)
    review = document["replay"]["blocked_source_fact_review"]
    finding = review["passes"][0]["changed_surfaces"][0]
    finding["after"] = hard_rewrite
    finding["reason"] = "交换事件双方。"
    review["passes"][0]["final_title"] = hard_rewrite
    blocked_meta["finding_fingerprint_sha256"] = canonical_sha256(finding)
    document["dissent"]["reason"] = finding["reason"]
    document["dissent"]["proposed_title"] = hard_rewrite
    review_body = dict(review)
    review_body.pop("receipt_sha256")
    review["receipt_sha256"] = canonical_sha256(review_body)
    blocked_meta["receipt_sha256"] = review["receipt_sha256"]
    _rehash(document)

    with pytest.raises(ManualTitleKeepAuthorityError, match="NOT_COMPRESSION_HEDGE"):
        validate_manual_title_keep_authority_document(document)


def test_finding_evidence_drift_blocks_even_with_rehashed_package_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = _document()
    consumption = validate_manual_title_keep_authority(_bundle(document), **_runtime(document))
    resolved = authorize_manual_title_keep(
        document["replay"]["blocked_source_fact_review"], consumption=consumption
    )
    tampered = copy.deepcopy(resolved)
    tampered["blocked_source_fact_review"]["passes"][0]["changed_surfaces"][0]["evidence"][0] = (
        "final_transcript: 漂移"
    )
    tampered.pop("receipt_sha256")
    tampered = source_fact_review._finalize_receipt(tampered)
    monkeypatch.setattr(
        source_fact_review, "load_manual_title_keep_authority", lambda _cid: _bundle(document)
    )

    assert not validate_source_fact_review(
        tampered,
        selection_hook=str(_runtime(document)["selection_hook"]),
        title=str(_runtime(document)["title"]),
        final_transcript=str(_runtime(document)["final_transcript"]),
        clip_context_prompt=str(_runtime(document)["clip_context_prompt"]),
        selection_scorecard=_runtime(document)["selection_scorecard"],
        candidate_id=CANDIDATE_ID,
        final_reviewed_srt_path=SRT_PATH,
        speaker_evidence=_runtime(document)["speaker_evidence"],
    )


def test_shared_auditor_validator_reloads_exact_keep_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = _document()
    runtime = _runtime(document)
    consumption = validate_manual_title_keep_authority(_bundle(document), **runtime)
    resolved = authorize_manual_title_keep(
        document["replay"]["blocked_source_fact_review"], consumption=consumption
    )
    monkeypatch.setattr(
        source_fact_review,
        "load_manual_title_keep_authority",
        lambda _cid: _bundle(document),
    )

    assert validate_source_fact_review(
        resolved,
        selection_hook=str(runtime["selection_hook"]),
        title=str(runtime["title"]),
        final_transcript=str(runtime["final_transcript"]),
        clip_context_prompt=str(runtime["clip_context_prompt"]),
        selection_scorecard=runtime["selection_scorecard"],
        candidate_id=CANDIDATE_ID,
        final_reviewed_srt_path=SRT_PATH,
        speaker_evidence=runtime["speaker_evidence"],
    )
    # Context-bearing KEEP receipts cannot be downgraded to legacy validation.
    assert not validate_source_fact_review(
        resolved,
        selection_hook=str(runtime["selection_hook"]),
        title=str(runtime["title"]),
        final_transcript=str(runtime["final_transcript"]),
        clip_context_prompt=str(runtime["clip_context_prompt"]),
        selection_scorecard=runtime["selection_scorecard"],
    )


@pytest.mark.parametrize(
    "drift",
    ["source_media", "interval", "entity_context", "speaker_evidence"],
)
def test_runtime_truth_drift_blocks(drift: str) -> None:
    document = _document()
    runtime = _runtime(document)
    if drift == "source_media":
        runtime["entity_context"] = copy.deepcopy(runtime["entity_context"])
        runtime["entity_context"]["candidate_binding"]["source_sha256"] = "0" * 64
    elif drift == "interval":
        runtime["entity_context"] = copy.deepcopy(runtime["entity_context"])
        runtime["entity_context"]["candidate_binding"]["absolute_source_end_ms"] += 1
    elif drift == "entity_context":
        runtime["entity_context"] = copy.deepcopy(runtime["entity_context"])
        runtime["entity_context"]["context_sha256"] = "sha256:" + "0" * 64
    else:
        runtime["speaker_evidence"] = copy.deepcopy(runtime["speaker_evidence"])
        runtime["speaker_evidence"]["speaker_final_srt_sha256"] = "sha256:" + "0" * 64
    with pytest.raises(ManualTitleKeepAuthorityError):
        validate_manual_title_keep_authority(_bundle(document), **runtime)


def test_plain_srt_byte_and_intermediate_symlink_drift_block(tmp_path: Path) -> None:
    document = _document()
    drifted = tmp_path / "drifted.srt"
    drifted.write_bytes(SRT_PATH.read_bytes() + b"\n")
    with pytest.raises(ManualTitleKeepAuthorityError, match="PLAIN_SRT_MISMATCH"):
        validate_manual_title_keep_authority(
            _bundle(document),
            **{**_runtime(document), "final_reviewed_srt_path": drifted},
        )

    real_parent = tmp_path / "real"
    real_parent.mkdir()
    real_srt = real_parent / "reviewed.srt"
    real_srt.write_bytes(SRT_PATH.read_bytes())
    linked_parent = tmp_path / "linked"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    with pytest.raises(ManualTitleKeepAuthorityError, match="CONTAINS_SYMLINK"):
        validate_manual_title_keep_authority(
            _bundle(document),
            **{
                **_runtime(document),
                "final_reviewed_srt_path": linked_parent / "reviewed.srt",
            },
        )


def test_publish_staging_replays_two_fresh_contexts_without_calling_provider(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    document = _document()
    replay = document["replay"]
    blocked = replay["blocked_source_fact_review"]
    evidence = blocked["speaker_evidence"]
    speaker_result = SpeakerEvidenceResult(
        state=SpeakerEvidenceState.PRESENT_VALID,
        transcript=evidence["speaker_transcript"],
        speaker_evidence=evidence,
        speaker_evidence_sha256=blocked["speaker_evidence_sha256"],
    )
    monkeypatch.setattr(
        source_fact_staging,
        "load_manual_title_keep_authority",
        lambda _cid: _bundle(document),
    )
    monkeypatch.setattr(
        source_fact_staging,
        "build_addressee_evidence",
        lambda _record, _cues: (replay["final_transcript"], speaker_result),
    )
    media = tmp_path / f"{CANDIDATE_ID}.recut.mp4"
    media.write_bytes(b"video")
    provider_calls = 0

    def provider(*_args: object, **_kwargs: object) -> dict[str, object]:
        nonlocal provider_calls
        provider_calls += 1
        raise AssertionError("provider must not run for deterministic KEEP replay")

    monkeypatch.setattr(source_fact_staging, "review_and_repair_source_facts", provider)

    def stage_cover(_record: dict[str, object], **_kwargs: object) -> dict[str, object]:
        cover = tmp_path / "cover.png"
        cover.write_bytes(b"cover")
        return {
            "status": "AI_COVER_READY",
            "cover_path": str(cover),
            "cover_generation": {"status": "READY", "rendered_lines": ["莉娅求小李放过我"]},
            "reason_codes": [],
        }

    staged_rows: list[dict[str, object]] = []
    for fresh_prompt in (
        "fresh ASR grid A: 小心呆呆鸟",
        "fresh ASR grid B: 南町，NT往右走了",
    ):
        story = {
            "schema_version": "lidousha-story-contract.v1",
            "candidate_id": CANDIDATE_ID,
            "selection_hook": blocked["original_selection_hook"],
            "selection_scorecard": replay["selection_scorecard"],
            "clip_context_prompt": fresh_prompt,
        }
        staged = _stage_publish_draft(
            {
                "status": "MATERIALIZED",
                "media_path": str(media),
                "subtitle_path": str(SRT_PATH),
                "story_contract": story,
                "artifact_hashes": {},
            },
            candidate_id=CANDIDATE_ID,
            title=str(document["approved_title"]["value"]),
            cues=[SourceCue("cue-1", 0, 1_000, "ignored")],
            run_ffmpeg=False,
            title_llm_call=None,
            selection_hook=str(blocked["original_selection_hook"]),
            source_fact_llm_call=None,
            stage_cover=stage_cover,
        )
        assert staged is not None
        staged_rows.append(staged)

    assert provider_calls == 0
    for fresh_prompt, staged in zip(
        ("fresh ASR grid A: 小心呆呆鸟", "fresh ASR grid B: 南町，NT往右走了"),
        staged_rows,
        strict=True,
    ):
        assert staged["publish_staging"]["title"] == document["approved_title"]["value"]
        assert staged["publish_staging"]["title_authority_status"] == "PASS_WITH_RECORDED_DISSENT"
        receipt = staged["story_contract"]["source_fact_review"]
        assert receipt["decision"] == "PASS_WITH_RECORDED_DISSENT"
        consumption = receipt["manual_title_keep_authority_consumption"]
        assert consumption["diagnostic_clip_context_prompt_sha256"] == text_sha256(fresh_prompt)
        assert consumption["diagnostic_clip_context_matches_adjudication"] is False
    assert (
        staged_rows[0]["story_contract"]["source_fact_review"]["receipt_sha256"]
        != staged_rows[1]["story_contract"]["source_fact_review"]["receipt_sha256"]
    )
