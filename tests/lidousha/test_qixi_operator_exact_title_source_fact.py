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
import src.autoslice.source_fact_staging as source_fact_staging
from src.autoslice import qixi_post_correction_public_surface as public_surface
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
    transcript = "final reviewed transcript"
    scorecard = {"schema_version": "lidousha-selection-scorecard.v1", "status": "VALID"}
    boundary = {"schema_version": "boundary-audit.v1", "verdict": "ok_sentence_boundary_cut"}
    srt_path = tmp_path / "recut.srt"
    srt_payload = b"1\n00:00:00,000 --> 00:00:01,000\nfinal reviewed transcript\n"
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
            "final_text_srt_sha256": bytes_sha256(srt_payload)[7:],
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
            "final_transcript_sha256": text_sha256(transcript),
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
    assert (
        document["sealed_before"]["record"]["sha256"]
        == document["sealed_before"]["delivery_record"]["sha256"]
    )
    assert document["sealed_before"]["state"]["sha256"] == (
        "sha256:159f8f8ba0d9734f3ceee69968c3febf4916ae7d8d02c708f7b15a58d6b173bc"
    )
    assert document["scope"]["provider_pass_claim"] is False


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
    record = json.loads(paths["record"].read_text(encoding="utf-8"))
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
                "final_transcript_sha256": text_sha256("女友感"),
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
                "final_text_srt_sha256": _public_descriptor(paths["srt"])["sha256"][7:],
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
            "final_transcript_sha256": text_sha256("女友感"),
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
            "final_transcript_sha256": source["final_transcript_sha256"],
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


def test_two_phase_postcommit_successor_replays_daily_and_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
    transcript = "女友感"
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
        staging.update({"title": TITLE, "source_fact_review": review})
        publish_path = Path(str(kwargs["private_publish_json_path"]))
        staged_publish = json.loads(publish_path.read_text(encoding="utf-8"))
        staged_publish.update({"title": TITLE, "source_fact_review": review})
        publish_path.write_bytes(public_surface._json_bytes(staged_publish))
        return staged

    inputs = public_surface.validate_runtime(
        public_authority, repo_root=repo, runtime_root=Path(str(public_authority["runtime_root"]))
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
