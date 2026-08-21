from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import src.autoslice.operator_exact_title_source_fact_authority as authority_module
import src.autoslice.source_fact_review as source_fact_review
import src.autoslice.source_fact_staging as source_fact_staging
import src.autoslice.review_package_source_fact_audit as package_source_fact_audit
from scripts.build_lidousha_daily_review_manifest import DailyManifestError, _validate_source_fact_receipts
from src.autoslice.operator_exact_title_source_fact_authority import (
    AUTHORITY_REPO_PATH,
    PASS_DECISION,
    OperatorExactTitleSourceFactAuthority,
    OperatorExactTitleSourceFactAuthorityError,
    authorize_operator_exact_title_source_fact,
    bytes_sha256,
    canonical_sha256,
    consume_operator_exact_title_source_fact_authority,
    text_sha256,
    validate_authority_document,
)


ROOT = Path(__file__).resolve().parents[2]
CANDIDATE_ID = "auto_123655_1613_1676"
TITLE = "【李豆沙】熊猫头要一本正经的新增‘熊今饭’环节了"


def _rehash(document: dict[str, object]) -> None:
    failed = document["failed_source_fact_review"]
    assert isinstance(failed, dict)
    failed.pop("receipt_sha256", None)
    failed["receipt_sha256"] = canonical_sha256(failed)
    document.pop("authority_sha256", None)
    document["authority_sha256"] = canonical_sha256(document)


def _document() -> dict[str, object]:
    return json.loads((ROOT / AUTHORITY_REPO_PATH).read_text(encoding="utf-8"))


def _runtime(tmp_path: Path) -> tuple[OperatorExactTitleSourceFactAuthority, dict[str, object]]:
    document = copy.deepcopy(_document())
    source = document["source_binding"]
    failed = document["failed_source_fact_review"]
    assert isinstance(source, dict) and isinstance(failed, dict)
    media = source["source_media"]
    assert isinstance(media, dict)
    final_transcript = "final reviewed transcript"
    srt_path = tmp_path / "replacement_recuts" / f"{CANDIDATE_ID}.recut.srt"
    srt_path.parent.mkdir()
    srt_bytes = b"1\n00:00:00,000 --> 00:00:01,000\nfinal reviewed transcript\n"
    srt_path.write_bytes(srt_bytes)
    review_path = srt_path.parent.parent / f"{CANDIDATE_ID}.review-flags.json"
    review_payload = json.dumps(
        {
            "schema_version": "final-review-audit.v2",
            "status": "CLEAN",
            "reviewed_srt_sha256": bytes_sha256(srt_bytes),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    review_path.write_bytes(review_payload)
    chat_path = tmp_path / f"{CANDIDATE_ID}.chat-authority.json"
    chat_payload = json.dumps(
        {"schema_version": "chat-authority-audit.v2", "status": "APPLIED_AND_VERIFIED"},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    chat_path.write_bytes(chat_payload)
    source_piece = json.dumps(
        [
            {
                "end_ms": media["end_ms"],
                "recording_basename": media["basename"],
                "source_media_sha256": media["sha256"],
                "start_ms": media["start_ms"],
            }
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    prompt = f"candidate_id: {CANDIDATE_ID}\nsource_pieces: {source_piece}\n"
    context_path = tmp_path / f"{CANDIDATE_ID}.clip-context.json"
    context_payload = json.dumps(
        {"candidate_id": CANDIDATE_ID, "prompt": prompt},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    context_path.write_bytes(context_payload)
    scorecard = {"schema_version": "lidousha-selection-scorecard.v1", "status": "VALID"}
    boundary = {
        "schema_version": "boundary-audit.v1",
        "verdict": "ok_sentence_boundary_cut",
        "final_delivery_boundary_semantic_review": {
            "status": "PASS",
            "final_endpoint_binding": {"status": "PASS"},
        },
    }
    reviewed_srt = source["reviewed_srt"]
    review = source["final_review"]
    chat = source["chat_authority"]
    assert isinstance(reviewed_srt, dict) and isinstance(review, dict) and isinstance(chat, dict)
    reviewed_srt.update({"sha256": bytes_sha256(srt_bytes), "bytes": len(srt_bytes)})
    review.update({"sha256": bytes_sha256(review_payload), "bytes": len(review_payload)})
    chat.update({"sha256": bytes_sha256(chat_payload), "bytes": len(chat_payload)})
    source["clip_context_file_sha256"] = bytes_sha256(context_payload)
    source["clip_context_prompt_sha256"] = text_sha256(prompt)
    source["selection_scorecard_sha256"] = canonical_sha256(scorecard)
    source["boundary_audit_sha256"] = canonical_sha256(boundary)
    passes = failed["passes"]
    assert isinstance(passes, list) and isinstance(passes[0], dict)
    passes[0]["final_transcript_sha256"] = text_sha256(final_transcript)
    _rehash(document)
    bundle = OperatorExactTitleSourceFactAuthority(
        document=validate_authority_document(document),
        repo_path=AUTHORITY_REPO_PATH,
        file_sha256=bytes_sha256(b"synthetic-sealed-authority"),
    )
    record = {
        "subtitle_path": str(srt_path),
        "clip_context_path": str(context_path),
        "chat_authority_audit_path": str(chat_path),
        "boundary_audit": boundary,
        "story_contract": {
            "candidate_id": CANDIDATE_ID,
            "selection_hook": source["initial_selection_hook"],
            "selection_hook_sha256": source["initial_selection_hook_sha256"],
            "selection_scorecard": scorecard,
            "clip_context_prompt": prompt,
        },
    }
    return bundle, {
        "record": record,
        "final_transcript": final_transcript,
        "srt_path": srt_path,
        "speaker_evidence": failed["speaker_evidence"],
    }


def _consume(bundle: OperatorExactTitleSourceFactAuthority, runtime: dict[str, object]) -> dict[str, object]:
    record = runtime["record"]
    assert isinstance(record, dict)
    return consume_operator_exact_title_source_fact_authority(
        bundle,
        candidate_id=CANDIDATE_ID,
        title=TITLE,
        selection_hook=str(record["story_contract"]["selection_hook"]),
        final_transcript=str(runtime["final_transcript"]),
        final_reviewed_srt_path=Path(str(runtime["srt_path"])),
        record=record,
        speaker_evidence=runtime["speaker_evidence"],
    )


def _resolved_record(runtime: dict[str, object]) -> dict[str, object]:
    record = copy.deepcopy(runtime["record"])
    source = _document()["source_binding"]
    assert isinstance(record, dict) and isinstance(source, dict)
    contract = record["story_contract"]
    assert isinstance(contract, dict)
    hook = source["resolved_selection_hook"]
    contract["selection_hook"] = hook
    contract["selection_hook_sha256"] = source["resolved_selection_hook_sha256"]
    contract["clip_context_prompt"] = (
        f"candidate_id: {CANDIDATE_ID}\nselection_hook: {hook}\n"
        + contract["clip_context_prompt"]
    )
    return record


def test_live_sealed_asset_has_exact_operator_and_provider_dissent_shape() -> None:
    document = validate_authority_document(_document())
    operator = document["operator_authorization"]
    failed = document["failed_source_fact_review"]
    assert isinstance(operator, dict) and isinstance(failed, dict)
    assert operator["conversation_id"] == "0df2296b-500a-4681-ab5e-6fb46dc39579"
    assert operator["line"] == 947
    assert operator["timestamp"] == "2026-08-19T00:08:52.249Z"
    assert failed["receipt_sha256"] == "sha256:99326ca3eb53ab11db585ef2ff25df0a8f74b6356c60d8045210b12838b3de9f"


def test_candidate_scoped_consumption_replays_every_bound_surface(tmp_path: Path) -> None:
    bundle, runtime = _runtime(tmp_path)
    consumption = _consume(bundle, runtime)
    record = runtime["record"]
    assert isinstance(record, dict)
    assert consumption["decision"] == PASS_DECISION
    assert consumption["provider_pass_claim"] is False
    receipt = authorize_operator_exact_title_source_fact(consumption)
    assert source_fact_review.source_fact_review_passes(receipt)
    assert receipt["final_title"] == TITLE
    assert receipt["final_selection_hook"] == bundle.document["source_binding"]["resolved_selection_hook"]
    assert receipt["recorded_source_fact_dissent"]["status"] == "FAILED"
    assert receipt["recorded_source_fact_dissent"]["receipt_sha256"] == consumption[
        "failed_source_fact_receipt_sha256"
    ]


@pytest.mark.parametrize(
    "mutation",
    ["title", "srt", "final_review", "chat", "boundary", "context", "story", "scorecard"],
)
def test_bound_runtime_drift_fails_before_provider_or_cover(
    tmp_path: Path, mutation: str
) -> None:
    bundle, runtime = _runtime(tmp_path)
    record = runtime["record"]
    assert isinstance(record, dict)
    if mutation == "title":
        title = TITLE + " drift"
    else:
        title = TITLE
        if mutation == "srt":
            Path(str(runtime["srt_path"])).write_text("drift", encoding="utf-8")
        elif mutation == "final_review":
            Path(str(runtime["srt_path"])).parent.parent.joinpath(
                f"{CANDIDATE_ID}.review-flags.json"
            ).write_text("{}", encoding="utf-8")
        elif mutation == "chat":
            Path(str(record["chat_authority_audit_path"])).write_text("{}", encoding="utf-8")
        elif mutation == "boundary":
            record["boundary_audit"] = {"schema_version": "boundary-audit.v1"}
        elif mutation == "context":
            Path(str(record["clip_context_path"])).write_text("{}", encoding="utf-8")
        elif mutation == "story":
            record["story_contract"]["selection_hook"] = "drift"
        else:
            record["story_contract"]["selection_scorecard"] = {"status": "DRIFT"}
    with pytest.raises(OperatorExactTitleSourceFactAuthorityError):
        consume_operator_exact_title_source_fact_authority(
            bundle,
            candidate_id=CANDIDATE_ID,
            title=title,
            selection_hook=str(record["story_contract"]["selection_hook"]),
            final_transcript=str(runtime["final_transcript"]),
            final_reviewed_srt_path=Path(str(runtime["srt_path"])),
            record=record,
            speaker_evidence=runtime["speaker_evidence"],
        )


def test_wrong_quote_or_failed_receipt_cannot_be_rehashed_into_authority() -> None:
    for field in ("quote", "failed"):
        document = _document()
        if field == "quote":
            document["operator_authorization"]["quote"] = "wrong quote"
        else:
            document["failed_source_fact_review"]["status"] = "PASS"
        _rehash(document)
        with pytest.raises(OperatorExactTitleSourceFactAuthorityError):
            validate_authority_document(document)


def test_staging_consumes_exact_authority_without_provider_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle, runtime = _runtime(tmp_path)
    record = runtime["record"]
    assert isinstance(record, dict)
    monkeypatch.setattr(
        source_fact_staging, "load_operator_exact_title_source_fact_authority", lambda _cid: bundle
    )
    monkeypatch.setattr(source_fact_staging, "load_manual_title_keep_authority", lambda _cid: None)
    monkeypatch.setattr(
        source_fact_staging, "load_deterministic_text_surface_authority", lambda _cid: None
    )
    monkeypatch.setattr(
        source_fact_staging,
        "build_addressee_evidence",
        lambda _record, _cues: (
            runtime["final_transcript"],
            SimpleNamespace(speaker_evidence=runtime["speaker_evidence"]),
        ),
    )
    result = source_fact_staging.resolve_initial_source_fact_review(
        candidate_id=CANDIDATE_ID,
        title_source="ivan_manual_override",
        title=TITLE,
        selection_hook=str(record["story_contract"]["selection_hook"]),
        story_contract=record["story_contract"],
        record=record,
        cues=[],
        source_fact_llm_call=lambda _prompt: pytest.fail("provider must not be called"),
        recovery_publication_authority=None,
        title_authority_status="RESOLVED_MANUAL",
        title_llm_enabled=False,
        prior_authority_error=None,
    )
    assert result.authority_error is None
    assert result.review is not None
    assert result.review["decision"] == PASS_DECISION
    assert result.review["final_title"] == TITLE
    assert result.review["final_selection_hook"] == bundle.document["source_binding"][
        "resolved_selection_hook"
    ]


def test_staging_rejects_srt_drift_before_provider_or_cover_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle, runtime = _runtime(tmp_path)
    record = runtime["record"]
    assert isinstance(record, dict)
    Path(str(runtime["srt_path"])).write_text("drift", encoding="utf-8")
    monkeypatch.setattr(
        source_fact_staging, "load_operator_exact_title_source_fact_authority", lambda _cid: bundle
    )
    monkeypatch.setattr(source_fact_staging, "load_manual_title_keep_authority", lambda _cid: None)
    monkeypatch.setattr(
        source_fact_staging, "load_deterministic_text_surface_authority", lambda _cid: None
    )
    monkeypatch.setattr(
        source_fact_staging,
        "build_addressee_evidence",
        lambda _record, _cues: (
            runtime["final_transcript"],
            SimpleNamespace(speaker_evidence=runtime["speaker_evidence"]),
        ),
    )
    result = source_fact_staging.resolve_initial_source_fact_review(
        candidate_id=CANDIDATE_ID,
        title_source="ivan_manual_override",
        title=TITLE,
        selection_hook=str(record["story_contract"]["selection_hook"]),
        story_contract=record["story_contract"],
        record=record,
        cues=[],
        source_fact_llm_call=lambda _prompt: pytest.fail("provider must not be called"),
        recovery_publication_authority=None,
        title_authority_status="RESOLVED_MANUAL",
        title_llm_enabled=False,
        prior_authority_error=None,
    )
    assert result.review is None
    assert result.authority_status == "BLOCKED_SOURCE_FACT_REVIEW"
    assert result.violation == "operator_exact_title_source_fact_authority_invalid"


def test_auditor_replays_authority_and_rejects_consumption_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle, runtime = _runtime(tmp_path)
    record = _resolved_record(runtime)
    assert isinstance(record, dict)
    consumption = _consume(bundle, runtime)
    receipt = authorize_operator_exact_title_source_fact(consumption)
    monkeypatch.setattr(authority_module, "load_operator_exact_title_source_fact_authority", lambda _cid, **_kw: bundle)
    validation = {
        "selection_hook": record["story_contract"]["selection_hook"],
        "title": TITLE,
        "final_transcript": runtime["final_transcript"],
        "candidate_id": CANDIDATE_ID,
        "final_reviewed_srt_path": runtime["srt_path"],
        "record": record,
        "speaker_evidence": runtime["speaker_evidence"],
        "repo_root": ROOT,
    }
    assert authority_module.validate_operator_exact_title_source_fact_receipt(receipt, **validation)
    tampered = copy.deepcopy(receipt)
    tampered["operator_exact_title_source_fact_authority_consumption"]["provider_pass_claim"] = True
    body = dict(tampered)
    body.pop("receipt_sha256")
    tampered["receipt_sha256"] = canonical_sha256(body)
    assert not authority_module.validate_operator_exact_title_source_fact_receipt(tampered, **validation)


def test_resolved_hook_passes_daily_manifest_and_package_auditor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle, runtime = _runtime(tmp_path)
    receipt = authorize_operator_exact_title_source_fact(_consume(bundle, runtime))
    record = _resolved_record(runtime)
    story_contract = record["story_contract"]
    assert isinstance(story_contract, dict)
    story_contract["source_fact_review"] = receipt
    record["publish_staging"] = {"title": TITLE, "source_fact_review": receipt}
    publish = {"title": TITLE, "source_fact_review": receipt}
    monkeypatch.setattr(
        authority_module,
        "load_operator_exact_title_source_fact_authority",
        lambda _cid, **_kw: bundle,
    )
    assert _validate_source_fact_receipts(
        record_doc=record,
        publish_doc=publish,
        subtitle_path=Path(str(runtime["srt_path"])),
        speaker_evidence=runtime["speaker_evidence"],
        qixi_repo_root=ROOT,
    ) == receipt["receipt_sha256"]
    assert package_source_fact_audit._source_fact_receipt_valid(
        receipt,
        record=record,
        story_contract=story_contract,
        artifact_title=TITLE,
        final_transcript=runtime["final_transcript"],
        subtitle_path=Path(str(runtime["srt_path"])),
        rebuilt_speaker_evidence=runtime["speaker_evidence"],
        qixi_repo_root=ROOT,
    )
    old_hook = copy.deepcopy(receipt)
    old_hook["final_selection_hook"] = old_hook["original_selection_hook"]
    body = dict(old_hook)
    body.pop("receipt_sha256")
    old_hook["receipt_sha256"] = canonical_sha256(body)
    with pytest.raises(DailyManifestError, match="invalid or stale"):
        _validate_source_fact_receipts(
            record_doc={
                **record,
                "story_contract": {**story_contract, "source_fact_review": old_hook},
                "publish_staging": {"title": TITLE, "source_fact_review": old_hook},
            },
            publish_doc={"title": TITLE, "source_fact_review": old_hook},
            subtitle_path=Path(str(runtime["srt_path"])),
            speaker_evidence=runtime["speaker_evidence"],
            qixi_repo_root=ROOT,
        )


def test_extra_candidate_cannot_consume_authority(tmp_path: Path) -> None:
    bundle, runtime = _runtime(tmp_path)
    record = runtime["record"]
    assert isinstance(record, dict)
    with pytest.raises(OperatorExactTitleSourceFactAuthorityError):
        consume_operator_exact_title_source_fact_authority(
            bundle,
            candidate_id="auto_123655_1613_1677",
            title=TITLE,
            selection_hook=str(record["story_contract"]["selection_hook"]),
            final_transcript=str(runtime["final_transcript"]),
            final_reviewed_srt_path=Path(str(runtime["srt_path"])),
            record=record,
            speaker_evidence=runtime["speaker_evidence"],
        )
