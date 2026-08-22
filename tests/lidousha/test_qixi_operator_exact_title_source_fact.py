from __future__ import annotations

import copy
import json
import os
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

import src.autoslice.qixi_operator_exact_title_source_fact as authority_module
import src.autoslice.cover_generation as cover_generation
import src.autoslice.publish_staging as publish_staging
import src.autoslice.qixi_post_correction_projection_paths as projection_paths
import src.autoslice.source_fact_staging as source_fact_staging
from src.autoslice import qixi_post_correction_public_surface as public_surface
from src.autoslice import qixi_post_correction_public_artifact_recovery as basename_recovery
from src.autoslice.jingting_chunker import parse_srt_cues
from src.autoslice.cover_punch_semantics import cover_text_requires_punch_for_thumbnail
from scripts.build_lidousha_daily_review_manifest import (
    DailyManifestError,
    _validate_source_fact_receipts,
)
from src.autoslice.qixi_operator_exact_title_source_fact import (
    ASSET_PATH,
    CANDIDATE_ID,
    Authority,
    QixiOperatorExactTitleSourceFactError,
    bytes_sha256,
    canonical_sha256,
    consume_authority,
    text_sha256,
    validate_authority_document,
)
from src.autoslice.review_package_source_fact_audit import _source_fact_receipt_valid
from tests import test_qixi_post_correction_public_surface as public_surface_tests


ROOT = Path(__file__).resolve().parents[2]
TITLE = "【李豆沙】小李有女友感吗？宿敌是否有点亲密了"


def _rehash(document: dict[str, object]) -> None:
    historical = document["historical_provider_pass"]
    assert isinstance(historical, dict)
    document["authority_sha256"] = canonical_sha256(
        {key: value for key, value in document.items() if key != "authority_sha256"}
    )


def _document() -> dict[str, object]:
    return json.loads((ROOT / ASSET_PATH).read_text(encoding="utf-8"))


def _runtime(tmp_path: Path) -> tuple[Authority, dict[str, object]]:
    document = copy.deepcopy(_document())
    source = document["source_binding"]
    historical = document["historical_provider_pass"]
    sealed = document["sealed_before"]
    assert isinstance(source, dict) and isinstance(historical, dict) and isinstance(sealed, dict)
    hook = str(source["selection_hook"])
    speaker = source["speaker_evidence"]
    transcript = "current post-human-correction transcript"
    historical_transcript = "historical provider-pass transcript"
    historical_srt_sha256 = bytes_sha256(b"historical provider-pass SRT\n")
    scorecard = {"schema_version": "lidousha-selection-scorecard.v1", "status": "VALID"}
    boundary = {"schema_version": "boundary-audit.v1", "verdict": "ok_sentence_boundary_cut"}
    srt_path = tmp_path / "recut.srt"
    srt_payload = (
        b"1\n00:00:00,000 --> 00:00:01,000\n"
        b"current post-human-correction transcript\n"
    )
    srt_path.write_bytes(srt_payload)
    source["reviewed_srt"] = {
        "path": str(srt_path),
        "bytes": len(srt_payload),
        "sha256": bytes_sha256(srt_payload),
    }
    piece = source["source_piece"]
    assert isinstance(piece, dict)
    prompt = f"source_pieces: {json.dumps([{'end_ms': piece['end_ms'], 'recording_basename': piece['basename'], 'source_media_sha256': piece['sha256'], 'start_ms': piece['start_ms']}], ensure_ascii=False, separators=(',', ':'))}"
    source["clip_context_prompt_sha256"] = text_sha256(prompt)
    source["selection_scorecard_sha256"] = canonical_sha256(scorecard)
    source["final_transcript_sha256"] = text_sha256(transcript)
    source["boundary_audit_sha256"] = canonical_sha256(boundary)
    correction_path = tmp_path / "correction.json"
    correction_payload = json.dumps(
        {
            "schema_version": "human-subtitle-correction.v2",
            "candidate_id": CANDIDATE_ID,
            "before_srt_sha256": historical_srt_sha256[7:],
            "after_srt_sha256": bytes_sha256(srt_payload)[7:],
        },
        sort_keys=True,
    ).encode()
    correction_path.write_bytes(correction_payload)
    source["correction"] = {
        "path": str(correction_path),
        "bytes": len(correction_payload),
        "sha256": bytes_sha256(correction_payload),
    }
    context_path = tmp_path / "context.json"
    context_payload = json.dumps(
        {
            "schema_version": "lidousha-clip-context.v1",
            "candidate_id": CANDIDATE_ID,
            "selection_hook": hook,
        },
        ensure_ascii=False,
        sort_keys=True,
    ).encode()
    context_path.write_bytes(context_payload)
    source["clip_context"] = {
        "path": str(context_path),
        "bytes": len(context_payload),
        "sha256": bytes_sha256(context_payload),
    }
    chat_path = tmp_path / "chat.json"
    chat_payload = json.dumps(
        {
            "schema_version": "chat-authority-audit.v2",
            "status": "APPLIED_AND_VERIFIED",
            "final_text_srt_sha256": historical_srt_sha256[7:],
        },
        sort_keys=True,
    ).encode()
    chat_path.write_bytes(chat_payload)
    source["chat_authority"] = {
        "path": str(chat_path),
        "bytes": len(chat_payload),
        "sha256": bytes_sha256(chat_payload),
    }
    historical.update(
        {
            "final_transcript_sha256": text_sha256(historical_transcript),
            "clip_context_prompt_sha256": text_sha256(prompt),
            "selection_scorecard_sha256": canonical_sha256(scorecard),
            "speaker_evidence": speaker,
            "speaker_evidence_sha256": canonical_sha256(speaker),
        }
    )
    receipt = {
        "schema_version": "lidousha-source-fact-review.v1",
        "status": "PASS",
        "decision": "KEEP",
        "original_title": historical["historical_title"],
        "final_title": historical["historical_title"],
        "original_selection_hook": hook,
        "final_selection_hook": hook,
        "passes": [
            {
                "status": "KEEP",
                "final_transcript_sha256": historical["final_transcript_sha256"],
                "clip_context_prompt_sha256": historical["clip_context_prompt_sha256"],
                "selection_scorecard_sha256": historical["selection_scorecard_sha256"],
            }
        ],
        "speaker_evidence": speaker,
        "speaker_evidence_sha256": historical["speaker_evidence_sha256"],
    }
    receipt["receipt_sha256"] = canonical_sha256(receipt)
    historical["receipt_sha256"] = receipt["receipt_sha256"]
    historical["receipt"] = receipt
    record = {
        "subtitle_path": str(srt_path),
        "boundary_audit": boundary,
        "story_contract": {
            "candidate_id": CANDIDATE_ID,
            "selection_hook": hook,
            "selection_hook_sha256": source["selection_hook_sha256"],
            "selection_scorecard": scorecard,
            "clip_context_prompt": prompt,
            "source_fact_review": receipt,
        },
        "publish_staging": {"source_fact_review": receipt},
    }
    preprovider_contract = copy.deepcopy(record["story_contract"])
    preprovider_contract.pop("source_fact_review")
    source["preprovider_story_contract_sha256"] = canonical_sha256(preprovider_contract)
    runtime_root = tmp_path / "runtime"
    record_root = runtime_root / "records"
    record_root.mkdir(parents=True)
    record_path = record_root / "record.json"
    delivery_path = record_root / "delivery.json"
    publish_path = record_root / "publish.json"
    state_path = runtime_root / "state.json"
    record_payload = json.dumps(record, ensure_ascii=False, sort_keys=True).encode()
    record_path.write_bytes(record_payload)
    delivery_path.write_bytes(record_payload)
    publish_payload = json.dumps(
        {"source_fact_review": receipt}, ensure_ascii=False, sort_keys=True
    ).encode()
    publish_path.write_bytes(publish_payload)
    state_payload = json.dumps(
        {
            "picks": [
                {
                    "candidate_id": CANDIDATE_ID,
                    "title": historical["historical_title"],
                    "upload_enabled": False,
                }
            ]
        },
        ensure_ascii=False,
        sort_keys=True,
    ).encode()
    state_path.write_bytes(state_payload)
    for key, path in (
        ("record", record_path),
        ("delivery_record", delivery_path),
        ("publish", publish_path),
    ):
        payload = path.read_bytes()
        sealed[key] = {"path": str(path), "bytes": len(payload), "sha256": bytes_sha256(payload)}
    sealed["state"] = {
        "path": str(state_path),
        "bytes": len(state_payload),
        "sha256": bytes_sha256(state_payload),
        "mode": 0o644,
    }
    _rehash(document)
    authority = Authority(
        validate_authority_document(document), ASSET_PATH, bytes_sha256(b"synthetic")
    )
    return authority, {
        "record": record,
        "srt_path": srt_path,
        "speaker": speaker,
        "transcript": transcript,
        "historical_transcript": historical_transcript,
        "preprovider_contract": preprovider_contract,
    }


def _consume(authority: Authority, runtime: dict[str, object]) -> dict[str, object]:
    record = runtime["record"]
    assert isinstance(record, dict)
    story = record["story_contract"]
    assert isinstance(story, dict)
    return consume_authority(
        authority,
        candidate_id=CANDIDATE_ID,
        title=TITLE,
        selection_hook=str(story["selection_hook"]),
        final_transcript=str(runtime["transcript"]),
        final_reviewed_srt_path=Path(str(runtime["srt_path"])),
        record=record,
        speaker_evidence=runtime["speaker"],
    )


def test_live_asset_binds_exact_operator_title_and_historical_pass() -> None:
    document = validate_authority_document(_document())
    assert document["approved_title"]["value"] == TITLE
    assert (
        document["historical_provider_pass"]["receipt_sha256"]
        == "sha256:098902bc2219bc33993bc17c7593103949c1aa5187b6fc608fac83ddd2d0dd26"
    )
    assert (
        document["source_binding"]["reviewed_srt"]["sha256"]
        == "sha256:a172c08564d57c72782e44e74583c17c3a95a241b798fcfe8df3fddda8b91617"
    )
    assert (
        document["source_binding"]["correction"]["sha256"]
        == "sha256:90de28c3ec0a01baa36a84fbcebc197322c8cf9817dcebb2799a41e7107cb4e0"
    )
    assert document["source_binding"]["final_transcript_sha256"] == (
        "sha256:5a8cc4a8f8a663fc1e257f161d55d460753d202fdb1979696eec85ccae15c268"
    )
    assert document["source_binding"]["preprovider_story_contract_sha256"] == (
        "sha256:c02f0d38bef3585eeb5811cb7d387a82d48c3385a647072240445426fc86234d"
    )
    assert document["historical_provider_pass"]["final_transcript_sha256"] == (
        "sha256:0bd8115110cd691fc2fd0480cb160cd8cd0cbb5ae1b548b00f164f5c3d55a367"
    )
    assert (
        document["source_binding"]["final_transcript_sha256"]
        != document["historical_provider_pass"]["final_transcript_sha256"]
    )
    assert (
        document["sealed_before"]["record"]["sha256"]
        == document["sealed_before"]["delivery_record"]["sha256"]
    )
    assert document["sealed_before"]["state"]["sha256"] == (
        "sha256:159f8f8ba0d9734f3ceee69968c3febf4916ae7d8d02c708f7b15a58d6b173bc"
    )
    assert document["scope"]["provider_pass_claim"] is False


def test_current_corrected_transcript_is_independent_from_historical_provider_pass(
    tmp_path: Path,
) -> None:
    authority, runtime = _runtime(tmp_path)
    source = authority.document["source_binding"]
    historical = authority.document["historical_provider_pass"]
    assert isinstance(source, dict) and isinstance(historical, dict)
    assert source["final_transcript_sha256"] != historical["final_transcript_sha256"]
    assert _consume(authority, runtime)["status"] == "CONSUMED"

    record = runtime["record"]
    assert isinstance(record, dict)
    with pytest.raises(QixiOperatorExactTitleSourceFactError):
        consume_authority(
            authority,
            candidate_id=CANDIDATE_ID,
            title=TITLE,
            selection_hook=str(record["story_contract"]["selection_hook"]),
            final_transcript=str(runtime["historical_transcript"]),
            final_reviewed_srt_path=Path(str(runtime["srt_path"])),
            record=record,
            speaker_evidence=runtime["speaker"],
        )

    current_drift = copy.deepcopy(authority.document)
    current_source = current_drift["source_binding"]
    assert isinstance(current_source, dict)
    current_source["final_transcript_sha256"] = text_sha256("different current transcript")
    _rehash(current_drift)
    drifted_current = Authority(
        validate_authority_document(current_drift), ASSET_PATH, bytes_sha256(b"synthetic")
    )
    with pytest.raises(QixiOperatorExactTitleSourceFactError):
        _consume(drifted_current, runtime)

    historical_drift = copy.deepcopy(authority.document)
    drifted_historical = historical_drift["historical_provider_pass"]
    assert isinstance(drifted_historical, dict)
    drifted_historical["final_transcript_sha256"] = text_sha256("different historical transcript")
    _rehash(historical_drift)
    with pytest.raises(QixiOperatorExactTitleSourceFactError):
        validate_authority_document(historical_drift)


def test_sealed_chat_replays_predecessor_after_a_terminal_chat_successor(
    tmp_path: Path,
) -> None:
    """A terminal chat successor cannot rewrite predecessor source-fact truth."""

    authority, runtime = _runtime(tmp_path)
    binding = authority.document["source_binding"]
    assert isinstance(binding, dict)
    chat_descriptor = binding["chat_authority"]
    assert isinstance(chat_descriptor, dict)
    chat_path = Path(str(chat_descriptor["path"]))
    sealed = chat_path.read_bytes()
    # Production terminal successor shape: readable authority JSON with a
    # different hash than the predecessor descriptor.
    chat_path.write_text(
        json.dumps(
            {
                "schema_version": "chat-authority-audit.v2",
                "status": "APPLIED_AND_VERIFIED",
                "final_text_srt_sha256": "f" * 64,
                "terminal_successor": True,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    with pytest.raises(QixiOperatorExactTitleSourceFactError, match="CHAT_DRIFT"):
        _consume(authority, runtime)

    record = runtime["record"]
    assert isinstance(record, dict)
    kwargs = {
        "candidate_id": CANDIDATE_ID,
        "title": TITLE,
        "selection_hook": str(record["story_contract"]["selection_hook"]),
        "final_transcript": str(runtime["transcript"]),
        "final_reviewed_srt_path": Path(str(runtime["srt_path"])),
        "record": record,
        "speaker_evidence": runtime["speaker"],
    }
    assert consume_authority(
        authority, sealed_chat_authority_bytes=sealed, **kwargs
    )["status"] == "CONSUMED"
    with pytest.raises(QixiOperatorExactTitleSourceFactError, match="CHAT_DRIFT"):
        consume_authority(
            authority, sealed_chat_authority_bytes=sealed + b"tampered", **kwargs
        )


def test_validate_receipt_accepts_only_bridge_returned_terminal_prechat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Source-fact replay cannot accept a caller-supplied historical chat."""

    authority, runtime = _runtime(tmp_path)
    record = runtime["record"]
    assert isinstance(record, dict)
    binding = authority.document["source_binding"]
    assert isinstance(binding, dict)
    chat = Path(str(binding["chat_authority"]["path"]))
    sealed = chat.read_bytes()
    chat.write_text('{"terminal":"after"}', encoding="utf-8")
    review = authority_module.authorize(
        consume_authority(
            authority,
            candidate_id=CANDIDATE_ID,
            title=TITLE,
            selection_hook=str(record["story_contract"]["selection_hook"]),
            final_transcript=str(runtime["transcript"]),
            final_reviewed_srt_path=Path(str(runtime["srt_path"])),
            record=record,
            speaker_evidence=runtime["speaker"],
            sealed_chat_authority_bytes=sealed,
        )
    )
    monkeypatch.setattr(authority_module, "load_authority", lambda *_args, **_kwargs: authority)
    monkeypatch.setattr(
        authority_module, "_validate_committed_public_successor", lambda *_args: sealed,
    )
    kwargs = {
        "selection_hook": str(record["story_contract"]["selection_hook"]),
        "title": TITLE,
        "final_transcript": str(runtime["transcript"]),
        "candidate_id": CANDIDATE_ID,
        "final_reviewed_srt_path": Path(str(runtime["srt_path"])),
        "record": record,
        "speaker_evidence": runtime["speaker"],
        "repo_root": tmp_path,
    }
    assert authority_module.validate_receipt(review, **kwargs)
    assert not authority_module.validate_receipt(
        review, sealed_chat_authority_bytes=sealed, **kwargs,
    )


def _preprovider_record(runtime: dict[str, object]) -> dict[str, object]:
    record = copy.deepcopy(runtime["record"])
    contract = copy.deepcopy(runtime["preprovider_contract"])
    assert isinstance(record, dict) and isinstance(contract, dict)
    record["story_contract"] = contract
    return record


@pytest.mark.parametrize("drift", ["input_audits", "transcript", "cover", "semantic"])
def test_preprovider_receipt_absence_is_bound_to_one_canonical_contract(
    tmp_path: Path, drift: str
) -> None:
    authority, runtime = _runtime(tmp_path)
    preprovider_record = _preprovider_record(runtime)
    story = preprovider_record["story_contract"]
    assert isinstance(story, dict)
    kwargs = {
        "candidate_id": CANDIDATE_ID,
        "title": TITLE,
        "selection_hook": str(story["selection_hook"]),
        "final_transcript": str(runtime["transcript"]),
        "final_reviewed_srt_path": Path(str(runtime["srt_path"])),
        "record": preprovider_record,
        "speaker_evidence": runtime["speaker"],
    }
    assert consume_authority(
        authority, allow_preprovider_receipt_absent=True, **kwargs
    )["status"] == "CONSUMED"
    with pytest.raises(QixiOperatorExactTitleSourceFactError):
        consume_authority(authority, **kwargs)
    with pytest.raises(QixiOperatorExactTitleSourceFactError):
        consume_authority(
            authority,
            allow_preprovider_receipt_absent=True,
            projected_receipt={},
            **kwargs,
        )

    if drift == "input_audits":
        story["input_audits"] = [{"drift": True}]
    elif drift == "transcript":
        story["transcript"] = "drift"
    elif drift == "cover":
        story["cover_output_audits"] = {"drift": True}
    else:
        story["source_media_sha256s"] = ["sha256:" + "f" * 64]
    with pytest.raises(QixiOperatorExactTitleSourceFactError):
        consume_authority(authority, allow_preprovider_receipt_absent=True, **kwargs)


def test_preprovider_receipt_absence_rejects_wrong_bound_hash(tmp_path: Path) -> None:
    authority, runtime = _runtime(tmp_path)
    document = copy.deepcopy(authority.document)
    source = document["source_binding"]
    assert isinstance(source, dict)
    source["preprovider_story_contract_sha256"] = "sha256:" + "f" * 64
    _rehash(document)
    wrong_bound = Authority(
        validate_authority_document(document), ASSET_PATH, bytes_sha256(b"synthetic")
    )
    record = _preprovider_record(runtime)
    story = record["story_contract"]
    assert isinstance(story, dict)
    with pytest.raises(QixiOperatorExactTitleSourceFactError):
        consume_authority(
            wrong_bound,
            candidate_id=CANDIDATE_ID,
            title=TITLE,
            selection_hook=str(story["selection_hook"]),
            final_transcript=str(runtime["transcript"]),
            final_reviewed_srt_path=Path(str(runtime["srt_path"])),
            record=record,
            speaker_evidence=runtime["speaker"],
            allow_preprovider_receipt_absent=True,
        )


@pytest.mark.parametrize("drift", ["chat_before", "correction_after"])
def test_human_correction_chains_historical_chat_to_current_srt(
    tmp_path: Path, drift: str
) -> None:
    authority, runtime = _runtime(tmp_path)
    source = authority.document["source_binding"]
    assert isinstance(source, dict)
    correction = json.loads(Path(str(source["correction"]["path"])).read_text(encoding="utf-8"))
    if drift == "chat_before":
        correction["before_srt_sha256"] = "f" * 64
    else:
        correction["after_srt_sha256"] = "e" * 64
    correction_payload = json.dumps(correction, sort_keys=True).encode()
    correction_path = tmp_path / f"{drift}.correction.json"
    correction_path.write_bytes(correction_payload)

    drifted_document = copy.deepcopy(authority.document)
    drifted_source = drifted_document["source_binding"]
    assert isinstance(drifted_source, dict)
    drifted_source["correction"] = {
        "path": str(correction_path),
        "bytes": len(correction_payload),
        "sha256": bytes_sha256(correction_payload),
    }
    _rehash(drifted_document)
    drifted_authority = Authority(
        validate_authority_document(drifted_document), ASSET_PATH, bytes_sha256(b"synthetic")
    )
    with pytest.raises(QixiOperatorExactTitleSourceFactError):
        _consume(drifted_authority, runtime)


@pytest.mark.parametrize(
    "drift", ["title", "srt", "correction", "context", "chat", "boundary", "scorecard", "receipt"]
)
def test_runtime_drift_blocks_before_provider_or_cover(tmp_path: Path, drift: str) -> None:
    authority, runtime = _runtime(tmp_path)
    record = runtime["record"]
    assert isinstance(record, dict)
    title = TITLE
    if drift == "title":
        title += " drift"
    elif drift == "srt":
        Path(str(runtime["srt_path"])).write_text("drift", encoding="utf-8")
    elif drift == "boundary":
        record["boundary_audit"] = {}
    elif drift == "scorecard":
        record["story_contract"]["selection_scorecard"] = {}
    elif drift == "receipt":
        record["story_contract"]["source_fact_review"] = {}
    else:
        key = {"context": "clip_context", "chat": "chat_authority"}.get(drift, drift)
        path = Path(str(authority.document["source_binding"][key]["path"]))
        path.write_text("{}", encoding="utf-8")
    with pytest.raises(QixiOperatorExactTitleSourceFactError):
        consume_authority(
            authority,
            candidate_id=CANDIDATE_ID,
            title=title,
            selection_hook=str(record["story_contract"]["selection_hook"]),
            final_transcript=str(runtime["transcript"]),
            final_reviewed_srt_path=Path(str(runtime["srt_path"])),
            record=record,
            speaker_evidence=runtime["speaker"],
        )


def test_staging_and_auditors_replay_no_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    authority, runtime = _runtime(tmp_path)
    record = runtime["record"]
    assert isinstance(record, dict)
    monkeypatch.setattr(
        source_fact_staging, "load_qixi_operator_exact_title_authority", lambda _cid: authority
    )
    monkeypatch.setattr(source_fact_staging, "load_manual_title_keep_authority", lambda _cid: None)
    monkeypatch.setattr(
        source_fact_staging, "load_deterministic_text_surface_authority", lambda _cid: None
    )
    monkeypatch.setattr(
        source_fact_staging, "load_operator_exact_title_source_fact_authority", lambda _cid: None
    )
    monkeypatch.setattr(
        source_fact_staging,
        "build_addressee_evidence",
        lambda *_: (runtime["transcript"], SimpleNamespace(speaker_evidence=runtime["speaker"])),
    )
    result = source_fact_staging.resolve_initial_source_fact_review(
        candidate_id=CANDIDATE_ID,
        title_source="ivan_manual_override",
        title=TITLE,
        selection_hook=str(record["story_contract"]["selection_hook"]),
        story_contract=record["story_contract"],
        record=record,
        cues=[],
        source_fact_llm_call=lambda _: pytest.fail("provider called"),
        recovery_publication_authority=None,
        title_authority_status="RESOLVED_MANUAL",
        title_llm_enabled=False,
        prior_authority_error=None,
    )
    assert result.review is not None and result.review["decision"] == authority_module.DECISION
    assert (
        result.review["operator_exact_title_source_fact_authority_consumption"][
            "provider_pass_claim"
        ]
        is False
    )
    receipt = result.review
    record["story_contract"]["source_fact_review"] = receipt
    record["publish_staging"].update({"title": TITLE, "source_fact_review": receipt})
    monkeypatch.setattr(authority_module, "load_authority", lambda *_args, **_kw: authority)
    monkeypatch.setattr(
        authority_module,
        "_validate_committed_public_successor",
        lambda *_args, **_kwargs: None,
    )
    publish = {"title": TITLE, "source_fact_review": receipt}
    assert (
        _validate_source_fact_receipts(
            record_doc=record,
            publish_doc=publish,
            subtitle_path=Path(str(runtime["srt_path"])),
            speaker_evidence=runtime["speaker"],
            qixi_repo_root=ROOT,
        )
        == receipt["receipt_sha256"]
    )
    assert _source_fact_receipt_valid(
        receipt,
        record=record,
        story_contract=record["story_contract"],
        artifact_title=TITLE,
        final_transcript=str(runtime["transcript"]),
        subtitle_path=Path(str(runtime["srt_path"])),
        rebuilt_speaker_evidence=runtime["speaker"],
        qixi_repo_root=ROOT,
    )
    stale = copy.deepcopy(receipt)
    stale["final_selection_hook"] = "old hook"
    stale["receipt_sha256"] = canonical_sha256(
        {key: value for key, value in stale.items() if key != "receipt_sha256"}
    )
    with pytest.raises(DailyManifestError):
        _validate_source_fact_receipts(
            record_doc={
                **record,
                "story_contract": {**record["story_contract"], "source_fact_review": stale},
                "publish_staging": {"title": TITLE, "source_fact_review": stale},
            },
            publish_doc={"title": TITLE, "source_fact_review": stale},
            subtitle_path=Path(str(runtime["srt_path"])),
            speaker_evidence=runtime["speaker"],
            qixi_repo_root=ROOT,
        )


def _public_descriptor(path: Path) -> dict[str, object]:
    return public_surface_tests._descriptor(path)


def _public_sealed_descriptor(path: Path) -> dict[str, object]:
    return public_surface_tests._sealed_descriptor(path)


def _reseal_public_authority(repo: Path, authority: dict[str, object]) -> dict[str, object]:
    authority["authority_sha256"] = public_surface._canonical_sha256(
        {key: value for key, value in authority.items() if key != "authority_sha256"}
    )
    path = repo / public_surface.RELATIVE_AUTHORITY_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(public_surface._json_bytes(authority))
    subprocess.run(["git", "add", path.relative_to(repo).as_posix()], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "public",
        ],
        cwd=repo,
        check=True,
    )
    return authority


def _canonical_public_qixi_fixture(
    tmp_path: Path,
) -> tuple[Path, dict[str, object], Authority, dict[str, Path], dict[str, object]]:
    repo, _runtime_root, public_authority, paths = public_surface_tests._fixture(tmp_path)
    artifacts = public_authority["artifacts"]
    assert isinstance(artifacts, dict)
    paths["srt"].write_text(
        "1\n00:00:00,000 --> 00:00:01,000\ncurrent post-human-correction transcript\n",
        encoding="utf-8",
    )
    historical_srt_sha256 = bytes_sha256(b"historical provider-pass SRT\n")
    record = json.loads(paths["record"].read_text(encoding="utf-8"))
    hashes = record["artifact_hashes"]
    assert isinstance(hashes, dict)
    hashes["subtitle_sha256"] = _public_descriptor(paths["srt"])["sha256"]
    initial_story = public_surface._story_contract_rebuilder(
        record,
        srt_path=paths["srt"],
        clip_context_path=Path(str(record["clip_context_path"])),
    )(str(record["story_contract"]["selection_hook"]))
    historic = {
        "schema_version": "lidousha-source-fact-review.v1",
        "status": "PASS",
        "decision": "KEEP",
        "original_title": "old title",
        "final_title": "old title",
        "original_selection_hook": initial_story["selection_hook"],
        "final_selection_hook": initial_story["selection_hook"],
        "passes": [
            {
                "status": "KEEP",
                "final_transcript_sha256": text_sha256("historical provider-pass transcript"),
                "clip_context_prompt_sha256": text_sha256(
                    str(initial_story["clip_context_prompt"])
                ),
                "selection_scorecard_sha256": canonical_sha256(
                    initial_story["selection_scorecard"]
                ),
            }
        ],
        "speaker_evidence": public_surface_tests._UNIFORM_HOST_EVIDENCE,
        "speaker_evidence_sha256": canonical_sha256(public_surface_tests._UNIFORM_HOST_EVIDENCE),
    }
    historic["receipt_sha256"] = canonical_sha256(historic)
    record.update(
        {
            "boundary_audit": {},
            "story_contract": {**initial_story, "source_fact_review": historic},
            "publish_staging": {"title": "old title", "source_fact_review": historic},
        }
    )
    paths["record"].write_bytes(public_surface._json_bytes(record))
    paths["delivery"].write_bytes(paths["record"].read_bytes())
    publish = json.loads(paths["publish"].read_text(encoding="utf-8"))
    publish_hashes = publish["artifact_hashes"]
    assert isinstance(publish_hashes, dict)
    publish_hashes["subtitle_sha256"] = _public_descriptor(paths["srt"])["sha256"]
    publish.update({"title": "old title", "source_fact_review": historic})
    paths["publish"].write_bytes(public_surface._json_bytes(publish))
    state = json.loads(paths["state"].read_text(encoding="utf-8"))
    state["picks"][0].update({"title": "old title", "upload_enabled": False})
    paths["state"].write_bytes(public_surface._json_bytes(state))
    correction = Path(str(artifacts["correction"]["path"]))
    correction.write_bytes(
        public_surface._json_bytes(
            {
                "schema_version": "human-subtitle-correction.v2",
                "candidate_id": CANDIDATE_ID,
                "before_srt_sha256": historical_srt_sha256[7:],
                "after_srt_sha256": _public_descriptor(paths["srt"])["sha256"][7:],
            }
        )
    )
    record["human_text_correction_manifest_sha256"] = _public_descriptor(correction)["sha256"]
    paths["record"].write_bytes(public_surface._json_bytes(record))
    paths["delivery"].write_bytes(paths["record"].read_bytes())
    chat = Path(str(artifacts["chat"]["path"]))
    chat.write_bytes(
        public_surface._json_bytes(
            {
                "schema_version": "chat-authority-audit.v2",
                "status": "APPLIED_AND_VERIFIED",
                "final_text_srt_sha256": historical_srt_sha256[7:],
            }
        )
    )
    for role, descriptor in artifacts.items():
        path = Path(str(descriptor["path"]))
        public_authority["artifacts"][role] = _public_descriptor(path)
    public_authority["sealed_before"] = {
        "record": _public_sealed_descriptor(paths["record"]),
        "delivery_record": _public_sealed_descriptor(paths["delivery"]),
        "publish": _public_sealed_descriptor(paths["publish"]),
        "state": _public_sealed_descriptor(paths["state"]),
    }
    public_authority["state_file"] = _public_sealed_descriptor(paths["state"])
    public_authority["source_fact_preimage"] = {
        "clip_context_prompt_sha256": text_sha256(str(initial_story["clip_context_prompt"])),
        "clip_context_prompt_bytes": len(str(initial_story["clip_context_prompt"]).encode()),
        "source_fact_receipt_sha256": historic["receipt_sha256"],
        "speaker_evidence": public_surface_tests._UNIFORM_HOST_EVIDENCE,
        "speaker_evidence_sha256": public_surface._canonical_sha256(
            public_surface_tests._UNIFORM_HOST_EVIDENCE
        ),
    }
    public_authority = _reseal_public_authority(repo, public_authority)
    public_raw = (repo / public_surface.RELATIVE_AUTHORITY_PATH).read_bytes()

    document = _document()
    source = document["source_binding"]
    historical = document["historical_provider_pass"]
    assert isinstance(source, dict) and isinstance(historical, dict)
    piece = json.loads(Path(str(artifacts["clip_context"]["path"])).read_text())["pieces"][0]
    source.update(
        {
            "selection_hook": initial_story["selection_hook"],
            "selection_hook_sha256": text_sha256(str(initial_story["selection_hook"])),
            "selection_scorecard_sha256": canonical_sha256(initial_story["selection_scorecard"]),
            "clip_context_prompt_sha256": text_sha256(str(initial_story["clip_context_prompt"])),
            "final_transcript_sha256": text_sha256("current post-human-correction transcript"),
            "preprovider_story_contract_sha256": canonical_sha256(initial_story),
            "speaker_evidence": public_surface_tests._UNIFORM_HOST_EVIDENCE,
            "speaker_evidence_sha256": canonical_sha256(
                public_surface_tests._UNIFORM_HOST_EVIDENCE
            ),
            "reviewed_srt": public_authority["artifacts"]["srt"],
            "correction": public_authority["artifacts"]["correction"],
            "clip_context": public_authority["artifacts"]["clip_context"],
            "chat_authority": public_authority["artifacts"]["chat"],
            "boundary_audit_sha256": canonical_sha256({}),
            "source_piece": {
                "basename": piece["recording_basename"],
                "sha256": piece["source_media_sha256"],
                "start_ms": piece["start_ms"],
                "end_ms": piece["end_ms"],
            },
            "public_surface_authority": {
                "relative_path": public_surface.RELATIVE_AUTHORITY_PATH.as_posix(),
                "bytes": len(public_raw),
                "sha256": bytes_sha256(public_raw),
                "authority_sha256": public_authority["authority_sha256"],
            },
        }
    )
    historical.update(
        {
            "receipt": historic,
            "receipt_sha256": historic["receipt_sha256"],
            "historical_title": historic["final_title"],
            "selection_hook": initial_story["selection_hook"],
            "final_transcript_sha256": historic["passes"][0]["final_transcript_sha256"],
            "clip_context_prompt_sha256": source["clip_context_prompt_sha256"],
            "selection_scorecard_sha256": source["selection_scorecard_sha256"],
            "speaker_evidence": source["speaker_evidence"],
            "speaker_evidence_sha256": source["speaker_evidence_sha256"],
        }
    )
    document["sealed_before"] = {
        role: {
            key: value
            for key, value in public_authority["sealed_before"][role].items()
            if role == "state" or key != "mode"
        }
        for role in ("record", "delivery_record", "publish", "state")
    }
    _rehash(document)
    authority = Authority(
        validate_authority_document(document), ASSET_PATH, bytes_sha256(b"synthetic")
    )
    return repo, public_authority, authority, paths, record


def test_actual_canonical_preprovider_story_contract_resolves_without_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _repo, _public_authority, authority, paths, record = _canonical_public_qixi_fixture(tmp_path)
    source = authority.document["source_binding"]
    assert isinstance(source, dict)
    hook = str(record["story_contract"]["selection_hook"])
    canonical_story = public_surface._story_contract_rebuilder(
        record,
        srt_path=paths["srt"],
        clip_context_path=Path(str(record["clip_context_path"])),
    )(hook)
    assert canonical_story.get("source_fact_review") is None
    assert canonical_story.get("cover_output_audits") is None
    assert canonical_sha256(canonical_story) == source["preprovider_story_contract_sha256"]

    preprovider_record = copy.deepcopy(record)
    preprovider_record["story_contract"] = canonical_story
    monkeypatch.setattr(
        source_fact_staging, "load_qixi_operator_exact_title_authority", lambda _cid: authority
    )
    monkeypatch.setattr(source_fact_staging, "load_manual_title_keep_authority", lambda _cid: None)
    monkeypatch.setattr(
        source_fact_staging, "load_deterministic_text_surface_authority", lambda _cid: None
    )
    monkeypatch.setattr(
        source_fact_staging, "load_operator_exact_title_source_fact_authority", lambda _cid: None
    )
    result = source_fact_staging.resolve_initial_source_fact_review(
        candidate_id=CANDIDATE_ID,
        title_source="ivan_manual_override",
        title=TITLE,
        selection_hook=hook,
        story_contract=canonical_story,
        record=preprovider_record,
        cues=parse_srt_cues(paths["srt"].read_text(encoding="utf-8")),
        source_fact_llm_call=lambda _: pytest.fail("provider called"),
        recovery_publication_authority=None,
        title_authority_status="RESOLVED_MANUAL",
        title_llm_enabled=False,
        prior_authority_error=None,
    )
    assert result.review is not None
    assert result.review["decision"] == authority_module.DECISION


def test_qixi_actual_stage_projects_one_json_domain_cover_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    public_surface_tests._synthetic_cover_validators.__wrapped__(monkeypatch)
    repo, public_authority, authority, paths, _record = _canonical_public_qixi_fixture(tmp_path)
    monkeypatch.setattr(authority_module, "load_authority", lambda *_args, **_kwargs: authority)
    monkeypatch.setattr(
        source_fact_staging, "load_qixi_operator_exact_title_authority", lambda _cid: authority
    )
    monkeypatch.setattr(source_fact_staging, "load_manual_title_keep_authority", lambda _cid: None)
    monkeypatch.setattr(
        source_fact_staging, "load_deterministic_text_surface_authority", lambda _cid: None
    )
    monkeypatch.setattr(
        source_fact_staging, "load_operator_exact_title_source_fact_authority", lambda _cid: None
    )
    cover_kwargs: list[dict[str, object]] = []

    def tuple_cover(
        _record: object, *, private_artifact_root: Path, **_kwargs: object
    ) -> dict[str, object]:
        cover_kwargs.append(
            {
                key: _kwargs.get(key)
                for key in (
                    "art_direction_llm_call",
                    "enforce_final_host_identity",
                    "final_host_identity_verifier",
                )
            }
        )
        cover = private_artifact_root / "covers" / "canonical.cover.png"
        background = private_artifact_root / "covers_ai_original" / "canonical.background.png"
        cover.parent.mkdir(parents=True, exist_ok=True)
        background.parent.mkdir(parents=True, exist_ok=True)
        cover.write_bytes(b"canonical-cover")
        background.write_bytes(b"canonical-background")
        generation = {
            "final_cover": str(cover),
            "final_cover_sha256": bytes_sha256(cover.read_bytes()),
            "ai_background": str(background),
            "ai_background_sha256": bytes_sha256(background.read_bytes()),
            "cover_text_mode": "punch",
            "cover_text": "女友感",
            "rendered_lines": ("女友感",),
            "cover_punch": ("女友感",),
            "art_direction": {"cover_punch_semantic_review": {}},
        }
        return {
            "status": "AI_COVER_READY",
            "cover_path": str(cover),
            "cover_sha256": generation["final_cover_sha256"],
            "ai_background_sha256": generation["ai_background_sha256"],
            "cover_generation": generation,
            "reason_codes": [],
        }

    monkeypatch.setattr(publish_staging, "_stage_lidousha_ai_cover", tuple_cover)
    inputs = public_surface.validate_runtime(
        public_authority, repo_root=repo, runtime_root=Path(str(public_authority["runtime_root"]))
    )
    stage_root = Path(tempfile.mkdtemp(prefix="stage-", dir=paths["record"].parent))

    def punch_review_provider(_prompt: str) -> str:
        pytest.fail("provider called")

    try:
        targets, _metadata = public_surface._build_after_image(
            inputs,
            stage_root=stage_root,
            source_fact_llm_call=punch_review_provider,
            stage_publish=publish_staging._stage_publish_draft,
        )
        record = json.loads(targets[paths["record"]].decode("utf-8"))
        publish = json.loads(targets[paths["publish"]].decode("utf-8"))
        staging = record["publish_staging"]
        assert cover_kwargs == [
            {
                "art_direction_llm_call": punch_review_provider,
                "enforce_final_host_identity": True,
                "final_host_identity_verifier": publish_staging.verify_lidousha_final_host_identity,
            }
        ]
        assert staging["source_fact_review"] == publish["source_fact_review"]
        assert staging["cover_generation"] == publish["cover_generation"]
        assert staging["cover_generation"]["rendered_lines"] == ["女友感"]
        assert staging["cover_generation"]["cover_punch"] == ["女友感"]
        assert Path(str(staging["cover_generation"]["final_cover"])) in targets
        assert Path(str(staging["cover_generation"]["ai_background"])) in targets
        assert bytes_sha256(
            targets[Path(str(staging["cover_generation"]["final_cover"]))]
        ) == staging["cover_generation"]["final_cover_sha256"]
        assert bytes_sha256(
            targets[Path(str(staging["cover_generation"]["ai_background"]))]
        ) == staging["cover_generation"]["ai_background_sha256"]
        before = {
            path: path.read_bytes()
            for path in (paths["record"], paths["delivery"], paths["publish"], paths["state"])
        }
        public_surface._validate_after_image(
            authority=public_authority, before=before, after=targets
        )

        def after_documents(
            candidate_record: dict[str, object], candidate_publish: dict[str, object]
        ) -> dict[Path, bytes]:
            candidate_after = dict(targets)
            payload = public_surface._json_bytes(candidate_record)
            candidate_after[paths["record"]] = payload
            candidate_after[paths["delivery"]] = payload
            candidate_after[paths["publish"]] = public_surface._json_bytes(
                candidate_publish
            )
            return candidate_after

        changed_media_record = copy.deepcopy(record)
        changed_media_publish = copy.deepcopy(publish)
        for document in (changed_media_record, changed_media_publish):
            document["artifact_hashes"]["video_sha256"] = "sha256:" + "0" * 64
        with pytest.raises(
            public_surface.QixiPostCorrectionPublicSurfaceError,
            match="final media binding drifts",
        ):
            public_surface._validate_after_image(
                authority=public_authority,
                before=before,
                after=after_documents(changed_media_record, changed_media_publish),
            )

        unbound_background_record = copy.deepcopy(record)
        unbound_background_publish = copy.deepcopy(publish)
        for document in (unbound_background_record, unbound_background_publish):
            document["artifact_hashes"]["ai_background_sha256"] = "sha256:" + "0" * 64
        unbound_background_publish["cover_generation"]["ai_background_sha256"] = (
            "sha256:" + "0" * 64
        )
        unbound_background_record["publish_staging"]["cover_generation"][
            "ai_background_sha256"
        ] = "sha256:" + "0" * 64
        with pytest.raises(
            public_surface.QixiPostCorrectionPublicSurfaceError,
            match="cover-owned artifact hash binding drifts",
        ):
            public_surface._validate_after_image(
                authority=public_authority,
                before=before,
                after=after_documents(
                    unbound_background_record, unbound_background_publish
                ),
            )

        for mutation in ("missing", "extra"):
            projected_record = copy.deepcopy(record)
            projected_publish = copy.deepcopy(publish)
            if mutation == "missing":
                projected_publish["artifact_hashes"].pop("cover_sha256")
            else:
                projected_publish["artifact_hashes"]["unrelated_sha256"] = (
                    "sha256:" + "0" * 64
                )
            with pytest.raises(
                public_surface.QixiPostCorrectionPublicSurfaceError,
                match="publish artifact hash projection drifts",
            ):
                public_surface._validate_after_image(
                    authority=public_authority,
                    before=before,
                    after=after_documents(projected_record, projected_publish),
                )

        forged_record = copy.deepcopy(record)
        forged_publish = copy.deepcopy(publish)
        for receipt in (
            forged_record["story_contract"]["source_fact_review"],
            forged_record["publish_staging"]["source_fact_review"],
            forged_publish["source_fact_review"],
        ):
            receipt["final_title"] = "forged title"
        forged_after = dict(targets)
        forged_after[paths["record"]] = public_surface._json_bytes(forged_record)
        forged_after[paths["delivery"]] = public_surface._json_bytes(forged_record)
        forged_after[paths["publish"]] = public_surface._json_bytes(forged_publish)
        with pytest.raises(
            public_surface.QixiPostCorrectionPublicSurfaceError,
            match="source-fact receipt replay drifts",
        ):
            public_surface._validate_after_image(
                authority=public_authority, before=before, after=forged_after
            )
    finally:
        public_surface._remove_private_stage(stage_root)


@pytest.mark.parametrize(
    "punch_provider",
    (
        None,
        lambda _prompt: "{}",
    ),
    ids=("missing", "invalid"),
)
def test_qixi_long_talk_cover_keeps_missing_or_invalid_punch_review_fail_closed(
    punch_provider: object,
) -> None:
    """The Qixi delegate enables review; it never authorizes an invalid split."""

    long_cover_text = "小李有女友感吗宿敌是否有点亲密了真的假的"
    assert cover_text_requires_punch_for_thumbnail(long_cover_text)
    direction = cover_generation._lidousha_cover_art_direction(
        candidate_id=CANDIDATE_ID,
        title=TITLE,
        cover_text=long_cover_text,
        art_direction_llm_call=punch_provider,
        allow_punch=True,
        story_hook="小李回应女友感与宿敌关系。",
    )
    assert direction.cover_punch == ()
    with pytest.raises(ValueError, match="COVER_PUNCH_REVIEW_REQUIRED"):
        cover_generation._talk_locked_split(
            long_cover_text,
            art_direction=direction,
        )


def test_qixi_actual_stage_replays_full_host_identity_cover_witness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, public_authority, authority, paths, _record = _canonical_public_qixi_fixture(tmp_path)
    monkeypatch.setattr(authority_module, "load_authority", lambda *_args, **_kwargs: authority)
    monkeypatch.setattr(
        source_fact_staging, "load_qixi_operator_exact_title_authority", lambda _cid: authority
    )
    monkeypatch.setattr(source_fact_staging, "load_manual_title_keep_authority", lambda _cid: None)
    monkeypatch.setattr(
        source_fact_staging, "load_deterministic_text_surface_authority", lambda _cid: None
    )
    monkeypatch.setattr(
        source_fact_staging, "load_operator_exact_title_source_fact_authority", lambda _cid: None
    )
    observed: list[dict[str, object]] = []

    def covered_stage(
        record: object, *, private_artifact_root: Path, **kwargs: object
    ) -> dict[str, object]:
        assert isinstance(record, dict)
        observed.append(
            {
                key: kwargs.get(key)
                for key in (
                    "enforce_final_host_identity",
                    "final_host_identity_verifier",
                )
            }
        )
        generation, _ = public_surface_tests._materialize_real_cover_generation(
            private_artifact_root, record["story_contract"]
        )
        return {
            "status": "AI_COVER_READY",
            "cover_path": str(generation["final_cover"]),
            "cover_sha256": generation["final_cover_sha256"],
            "ai_background_sha256": generation["ai_background_sha256"],
            "cover_generation": generation,
            "reason_codes": [],
        }

    monkeypatch.setattr(publish_staging, "_stage_lidousha_ai_cover", covered_stage)
    inputs = public_surface.validate_runtime(
        public_authority,
        repo_root=repo,
        runtime_root=Path(str(public_authority["runtime_root"])),
    )
    stage_root = Path(tempfile.mkdtemp(prefix="stage-", dir=paths["record"].parent))
    try:
        targets, _metadata = public_surface._build_after_image(
            inputs,
            stage_root=stage_root,
            source_fact_llm_call=lambda _prompt: pytest.fail("provider called"),
        )
        record = json.loads(targets[paths["record"]].decode("utf-8"))
        generation = record["publish_staging"]["cover_generation"]
        assert observed == [
            {
                "enforce_final_host_identity": True,
                "final_host_identity_verifier": publish_staging.verify_lidousha_final_host_identity,
            }
        ]
        assert projection_paths.validate_cover_route_decision(
            generation, allow_legacy_v1=False
        )
        assert projection_paths.validate_rendered_text_pixel_evidence(generation)
        assert projection_paths.validate_final_host_identity_verification(generation)
        projection_paths.validate_replayed_cover(
            generation,
            story=record["story_contract"],
            package_root=paths["record"].parent,
            after=targets,
        )
    finally:
        public_surface._remove_private_stage(stage_root)


def test_qixi_actual_stage_rejects_cover_without_final_host_identity_witness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, public_authority, authority, paths, _record = _canonical_public_qixi_fixture(tmp_path)
    monkeypatch.setattr(authority_module, "load_authority", lambda *_args, **_kwargs: authority)
    monkeypatch.setattr(
        source_fact_staging, "load_qixi_operator_exact_title_authority", lambda _cid: authority
    )
    monkeypatch.setattr(source_fact_staging, "load_manual_title_keep_authority", lambda _cid: None)
    monkeypatch.setattr(
        source_fact_staging, "load_deterministic_text_surface_authority", lambda _cid: None
    )
    monkeypatch.setattr(
        source_fact_staging, "load_operator_exact_title_source_fact_authority", lambda _cid: None
    )

    def uncovered_stage(
        record: object, *, private_artifact_root: Path, **kwargs: object
    ) -> dict[str, object]:
        assert isinstance(record, dict)
        assert kwargs["enforce_final_host_identity"] is True
        generation, _ = public_surface_tests._materialize_real_cover_generation(
            private_artifact_root, record["story_contract"]
        )
        generation.pop("final_host_identity_verification")
        return {
            "status": "AI_COVER_READY",
            "cover_path": str(generation["final_cover"]),
            "cover_sha256": generation["final_cover_sha256"],
            "ai_background_sha256": generation["ai_background_sha256"],
            "cover_generation": generation,
            "reason_codes": [],
        }

    monkeypatch.setattr(publish_staging, "_stage_lidousha_ai_cover", uncovered_stage)
    inputs = public_surface.validate_runtime(
        public_authority,
        repo_root=repo,
        runtime_root=Path(str(public_authority["runtime_root"])),
    )
    stage_root = Path(tempfile.mkdtemp(prefix="stage-", dir=paths["record"].parent))
    try:
        targets, _metadata = public_surface._build_after_image(
            inputs,
            stage_root=stage_root,
            source_fact_llm_call=lambda _prompt: pytest.fail("provider called"),
        )
        before = {
            path: path.read_bytes()
            for path in (paths["record"], paths["delivery"], paths["publish"], paths["state"])
        }
        with pytest.raises(
            public_surface.QixiPostCorrectionPublicSurfaceError,
            match="cover route/pixel/identity evidence drifts",
        ):
            public_surface._validate_after_image(
                authority=public_authority, before=before, after=targets
            )
    finally:
        public_surface._remove_private_stage(stage_root)


@pytest.mark.parametrize(
    "forge",
    (
        lambda generation, _after: generation["route_decision"].update(
            {"selected_treatment": "forged"}
        ),
        lambda generation, _after: generation["rendered_text_pixels"].update(
            {"final_cover_sha256": "sha256:" + "0" * 64}
        ),
        lambda generation, _after: generation["final_host_identity_verification"].update(
            {"final_cover_sha256": "sha256:" + "0" * 64}
        ),
        lambda generation, _after: generation.update(
            {"final_cover": "/outside/sealed-package/final-cover.png"}
        ),
    ),
)
def test_qixi_replayed_cover_rejects_forged_route_pixel_identity_or_locator(
    tmp_path: Path, forge: object
) -> None:
    _repo, _public_authority, _authority, paths, record = _canonical_public_qixi_fixture(tmp_path)
    story = public_surface._story_contract_rebuilder(
        record,
        srt_path=paths["srt"],
        clip_context_path=Path(str(record["clip_context_path"])),
    )(str(record["story_contract"]["selection_hook"]))
    generation, after = public_surface_tests._materialize_real_cover_generation(
        paths["record"].parent / "real-cover", story
    )
    assert callable(forge)
    forge(generation, after)
    with pytest.raises(public_surface.QixiPostCorrectionPublicSurfaceError):
        projection_paths.validate_replayed_cover(
            generation,
            story=story,
            package_root=paths["record"].parent,
            after=after,
        )


@pytest.mark.parametrize(
    ("decision", "authority_status", "mutation"),
    (
        (
            authority_module.DECISION,
            authority_module.AUTHORITY_STATUS,
            None,
        ),
        (authority_module.DECISION, "RESOLVED_MANUAL", "status"),
        ("KEEP", authority_module.AUTHORITY_STATUS, "decision"),
        (authority_module.DECISION, "RESOLVED_UNKNOWN", "status"),
        (authority_module.DECISION, authority_module.AUTHORITY_STATUS, "title"),
        (authority_module.DECISION, authority_module.AUTHORITY_STATUS, "consumption"),
    ),
)
def test_qixi_manual_projection_maps_only_the_sealed_decision_status(
    decision: str, authority_status: str, mutation: str | None
) -> None:
    source_fact = {"decision": decision}
    staging: dict[str, object] = {
        "status": "STAGED",
        "title": TITLE,
        "title_source": "ivan_manual_override",
        "title_authority_status": authority_status,
        "title_authority_error": None,
        "title_policy_violations": [],
        "source_fact_review": source_fact,
        "manual_title_repair_authority_consumption": None,
        "public_text_surface_authority_consumption": None,
        "title_story_audit": {"status": "PASS"},
        "entity_projection_audit": None,
        "cover_entity_projection_audit": None,
        "upload_enabled": False,
    }
    if mutation == "title":
        staging["title"] = "forged title"
    elif mutation == "consumption":
        staging["public_text_surface_authority_consumption"] = {"forged": True}
    publish = {key: copy.deepcopy(value) for key, value in staging.items() if key != "status"}
    if mutation is None:
        projection_paths.validate_manual_title_projection(
            staging, publish, source_fact=source_fact, public_title=TITLE
        )
    else:
        with pytest.raises(public_surface.QixiPostCorrectionPublicSurfaceError):
            projection_paths.validate_manual_title_projection(
                staging, publish, source_fact=source_fact, public_title=TITLE
            )


@pytest.mark.parametrize("recover_basename", [False, True])
def test_two_phase_postcommit_successor_replays_daily_and_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recover_basename: bool
) -> None:
    """A real canonical public journal proves the postcommit successor."""

    public_surface_tests._synthetic_cover_validators.__wrapped__(monkeypatch)
    repo, public_authority, authority, paths, record_before = _canonical_public_qixi_fixture(
        tmp_path
    )
    monkeypatch.setattr(authority_module, "load_authority", lambda *_args, **_kw: authority)
    source = authority.document["source_binding"]
    historical = authority.document["historical_provider_pass"]
    assert isinstance(source, dict) and isinstance(historical, dict)
    speaker = source["speaker_evidence"]
    hook = str(source["selection_hook"])
    transcript = "current post-human-correction transcript"
    consumption = consume_authority(
        authority,
        candidate_id=CANDIDATE_ID,
        title=TITLE,
        selection_hook=hook,
        final_transcript=transcript,
        final_reviewed_srt_path=paths["srt"],
        record=record_before,
        speaker_evidence=speaker,
    )
    assert consumption["status"] == "CONSUMED"

    def stage_qixi_title(record: dict[str, object], **kwargs: object) -> dict[str, object]:
        staged = public_surface_tests._fake_stage(record, **kwargs)
        story = staged["story_contract"]
        staging = staged["publish_staging"]
        assert isinstance(story, dict) and isinstance(staging, dict)
        story["source_fact_review"] = historical["receipt"]
        review = authority_module.authorize(
            consume_authority(
                authority,
                candidate_id=CANDIDATE_ID,
                title=TITLE,
                selection_hook=hook,
                final_transcript=transcript,
                final_reviewed_srt_path=Path(str(record["subtitle_path"])),
                record=staged,
                speaker_evidence=speaker,
            )
        )
        story["source_fact_review"] = review
        staging.update(
            {
                "title": TITLE,
                "title_authority_status": authority_module.AUTHORITY_STATUS,
                "source_fact_review": review,
            }
        )
        publish_path = Path(str(kwargs["private_publish_json_path"]))
        staged_publish = json.loads(publish_path.read_text(encoding="utf-8"))
        staged_publish.update(
            {
                "title": TITLE,
                "title_authority_status": authority_module.AUTHORITY_STATUS,
                "source_fact_review": review,
            }
        )
        publish_path.write_bytes(public_surface._json_bytes(staged_publish))
        return staged

    inputs = public_surface.validate_runtime(
        public_authority, repo_root=repo, runtime_root=Path(str(public_authority["runtime_root"]))
    )
    if recover_basename:
        monkeypatch.setattr(
            public_surface,
            "_public_artifact_root",
            projection_paths.legacy_public_artifact_root,
        )
    stage_root = Path(tempfile.mkdtemp(prefix="stage-", dir=paths["record"].parent))
    try:
        targets, metadata = public_surface._build_after_image(
            inputs,
            stage_root=stage_root,
            source_fact_llm_call=lambda _prompt: pytest.fail("provider called"),
            stage_publish=stage_qixi_title,
        )
        journal_root = public_surface._journal_root(inputs)
        journal = public_surface._write_prepared_journal(
            journal_root, inputs=inputs, targets=targets, metadata=metadata
        )
    finally:
        public_surface._remove_private_stage(stage_root)

    # The precommit receipt cannot be replayed as a current source-fact receipt.
    provisional = authority_module.authorize(consumption)
    assert not authority_module.validate_receipt(
        provisional,
        selection_hook=hook,
        title=TITLE,
        final_transcript=transcript,
        candidate_id=CANDIDATE_ID,
        final_reviewed_srt_path=paths["srt"],
        record=record_before,
        speaker_evidence=speaker,
        repo_root=repo,
    )
    assert journal["status"] == "PREPARED"
    assert (
        public_surface._commit_journal(journal_root, journal, authority=public_authority)
        == "APPLIED"
    )
    if recover_basename:
        assert basename_recovery.recover(
            apply=True, repo_root=repo, authority=public_authority
        )["status"] == "RECOVERY_COMMITTED"

    record_after = json.loads(paths["record"].read_text(encoding="utf-8"))
    publish_after = json.loads(paths["publish"].read_text(encoding="utf-8"))
    story_after = record_after["story_contract"]
    receipt = story_after["source_fact_review"]
    assert isinstance(story_after, dict) and isinstance(receipt, dict)
    assert (
        _validate_source_fact_receipts(
            record_doc=record_after,
            publish_doc=publish_after,
            subtitle_path=paths["srt"],
            speaker_evidence=speaker,
            qixi_repo_root=repo,
        )
        == receipt["receipt_sha256"]
    )
    assert _source_fact_receipt_valid(
        receipt,
        record=record_after,
        story_contract=story_after,
        artifact_title=TITLE,
        final_transcript=transcript,
        subtitle_path=paths["srt"],
        rebuilt_speaker_evidence=speaker,
        qixi_repo_root=repo,
    )

    if recover_basename:
        from src.autoslice import qixi_terminal_evidence_refresh as terminal_refresh

        # A terminal authority is a strict third successor: merely placing it
        # in the repository changes the replay obligation, so a broken
        # terminal receipt cannot be masked by an otherwise valid basename
        # recovery chain.
        terminal_asset = repo / terminal_refresh.AUTHORITY_PATH
        terminal_asset.parent.mkdir(parents=True, exist_ok=True)
        terminal_asset.write_text("{}", encoding="utf-8")
        calls: list[Path] = []
        monkeypatch.setattr(
            terminal_refresh,
            "validate_committed_refresh",
            lambda *, repo_root: calls.append(repo_root),
        )
        assert (
            _validate_source_fact_receipts(
                record_doc=record_after,
                publish_doc=publish_after,
                subtitle_path=paths["srt"],
                speaker_evidence=speaker,
                qixi_repo_root=repo,
            )
            == receipt["receipt_sha256"]
        )
        assert calls == [repo]
        monkeypatch.setattr(
            terminal_refresh,
            "validate_committed_refresh",
            lambda **_kwargs: (_ for _ in ()).throw(ValueError("terminal drift")),
        )
        with pytest.raises(DailyManifestError, match="invalid or stale"):
            _validate_source_fact_receipts(
                record_doc=record_after,
                publish_doc=publish_after,
                subtitle_path=paths["srt"],
                speaker_evidence=speaker,
                qixi_repo_root=repo,
            )
        terminal_asset.unlink()
        migration_root = basename_recovery._recovery_root(public_authority)
        migration_journal = migration_root / "journal.json"
        migration_receipt = migration_root / "final-receipt.json"
        valid_migration_journal = migration_journal.read_bytes()
        valid_migration_receipt = migration_receipt.read_bytes()
        migration_receipt.unlink()
        with pytest.raises(DailyManifestError):
            _validate_source_fact_receipts(
                record_doc=record_after,
                publish_doc=publish_after,
                subtitle_path=paths["srt"],
                speaker_evidence=speaker,
                qixi_repo_root=repo,
            )
        migration_receipt.write_bytes(valid_migration_receipt)
        tampered_migration = json.loads(valid_migration_journal)
        tampered_migration["path_mapping"] = {}
        tampered_migration["journal_sha256"] = basename_recovery._journal_digest(
            tampered_migration
        )
        migration_journal.write_bytes(public_surface._json_bytes(tampered_migration))
        with pytest.raises(DailyManifestError):
            _validate_source_fact_receipts(
                record_doc=record_after,
                publish_doc=publish_after,
                subtitle_path=paths["srt"],
                speaker_evidence=speaker,
                qixi_repo_root=repo,
            )
        migration_journal.write_bytes(valid_migration_journal)

    journal_path = journal_root / "journal.json"
    receipt_path = journal_root / "final-receipt.json"
    valid_journal = journal_path.read_bytes()
    valid_receipt = receipt_path.read_bytes()

    def assert_rejected() -> None:
        with pytest.raises(DailyManifestError):
            _validate_source_fact_receipts(
                record_doc=record_after,
                publish_doc=publish_after,
                subtitle_path=paths["srt"],
                speaker_evidence=speaker,
                qixi_repo_root=repo,
            )
        assert not _source_fact_receipt_valid(
            receipt,
            record=record_after,
            story_contract=story_after,
            artifact_title=TITLE,
            final_transcript=transcript,
            subtitle_path=paths["srt"],
            rebuilt_speaker_evidence=speaker,
            qixi_repo_root=repo,
        )

    journal_path.unlink()
    assert_rejected()
    journal_path.write_bytes(valid_journal)
    prepared = json.loads(valid_journal)
    prepared["status"] = "PREPARED"
    prepared["journal_sha256"] = public_surface._journal_sha256(prepared)
    journal_path.write_bytes(public_surface._json_bytes(prepared))
    assert_rejected()
    journal_path.write_bytes(valid_journal)
    receipt_path.unlink()
    assert_rejected()
    receipt_path.write_bytes(valid_receipt)

    tampered_receipt = json.loads(valid_receipt)
    tampered_receipt["entries_sha256"] = bytes_sha256(b"tampered receipt")
    tampered_receipt["receipt_sha256"] = public_surface._canonical_sha256(
        {key: value for key, value in tampered_receipt.items() if key != "receipt_sha256"}
    )
    receipt_path.write_bytes(public_surface._json_bytes(tampered_receipt))
    assert_rejected()
    receipt_path.write_bytes(valid_receipt)

    wrong_predecessor = json.loads(valid_journal)
    wrong_predecessor["entries"][0]["before_sha256"] = bytes_sha256(b"wrong predecessor")
    wrong_predecessor["journal_sha256"] = public_surface._journal_sha256(wrong_predecessor)
    journal_path.write_bytes(public_surface._json_bytes(wrong_predecessor))
    receipt_path.write_bytes(
        public_surface._json_bytes(public_surface._final_receipt(wrong_predecessor))
    )
    assert_rejected()
    journal_path.write_bytes(valid_journal)
    receipt_path.write_bytes(valid_receipt)

    foreign = tmp_path / "outside-public-artifact.png"
    foreign.write_bytes(b"foreign")
    escaped = json.loads(valid_journal)
    cover_entry = next(entry for entry in escaped["entries"] if entry["role"] == "cover_artifact")
    extra = copy.deepcopy(cover_entry)
    extra.update(
        {
            "target": str(foreign),
            "after_bytes_b64": public_surface._b64(b"foreign"),
            "after_sha256": bytes_sha256(b"foreign"),
        }
    )
    installed = os.lstat(foreign)
    extra.update({"installed_device": installed.st_dev, "installed_inode": installed.st_ino})
    escaped["entries"].append(extra)
    escaped["journal_sha256"] = public_surface._journal_sha256(escaped)
    journal_path.write_bytes(public_surface._json_bytes(escaped))
    assert_rejected()
    journal_path.write_bytes(valid_journal)

    semantic = json.loads(valid_journal)
    record_entry = next(entry for entry in semantic["entries"] if entry["role"] == "record")
    record_entry["after_bytes_b64"] = public_surface._b64(b"{}")
    record_entry["after_sha256"] = bytes_sha256(b"{}")
    paths["record"].write_bytes(b"{}")
    semantic["journal_sha256"] = public_surface._journal_sha256(semantic)
    journal_path.write_bytes(public_surface._json_bytes(semantic))
    receipt_path.write_bytes(public_surface._json_bytes(public_surface._final_receipt(semantic)))
    assert_rejected()
