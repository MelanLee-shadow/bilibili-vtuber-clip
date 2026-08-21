from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

import scripts.refresh_candidate_source_fact_review as refresh_runner
import src.autoslice.candidate_source_fact_refresh as refresh
import src.autoslice.review_package_source_fact_audit as package_source_fact_audit
from src.autoslice.candidate_public_text_surface_authority import (
    load_candidate_public_text_surface_authority,
)
from src.autoslice.source_fact_review import validate_source_fact_review
from scripts.build_lidousha_daily_review_manifest import _validate_source_fact_receipts


def _sha(value: object) -> str:
    return "sha256:" + hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _text_sha(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def _historical(*, hook: str, old_title: str) -> dict[str, object]:
    result: dict[str, object] = {
        "schema_version": "lidousha-source-fact-review.v1",
        "status": "PASS",
        "decision": "REPAIRED",
        "original_selection_hook": hook + " 原始",
        "original_title": old_title,
        "final_selection_hook": hook,
        "final_title": old_title,
        "passes": [{"status": "REPAIR", "evidence": ["frozen cue"]}, {"status": "KEEP"}],
        "speaker_evidence": {
            "state": "AbsentAuthorized",
            "reason": "speaker_mode_uniform_host",
            "policy_ids": {"absence": "speaker_mode_uniform_host/v1"},
        },
    }
    result["receipt_sha256"] = _sha(result)
    return result


def _fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    sealed = load_candidate_public_text_surface_authority(refresh.CANDIDATE_ID)
    assert sealed is not None and sealed.is_manual_title_resolution
    prompt = "candidate source-fact refresh context"
    authority = replace(
        sealed,
        clip_context_prompt_sha256=_text_sha(prompt),
        story_contract_sha256=None,
    )
    title = authority.resolved_title
    hook = authority.resolved_selection_hook
    srt = tmp_path / "final.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:01,000\n冻结事实\n", encoding="utf-8")
    transcript = "冻结事实"
    scorecard = {"status": "CURRENT", "score": 9}
    speaker = _historical(hook=hook, old_title=authority.superseded_title)["speaker_evidence"]
    assert isinstance(speaker, dict)
    historical = _historical(hook=hook, old_title=authority.superseded_title)
    context = refresh.build_public_text_source_fact_context(authority, clip_context_prompt=prompt)
    contract = {
        "candidate_id": refresh.CANDIDATE_ID,
        "selection_hook": hook,
        "clip_context_prompt": prompt,
        "selection_scorecard": scorecard,
        "source_fact_review": historical,
    }
    authority_document = {
        "schema_version": refresh.AUTHORITY_SCHEMA,
        "candidate_id": refresh.CANDIDATE_ID,
        "recording_date": "2026-08-11",
        "historical_provider_receipt": {
            key: historical[key]
            for key in (
                "receipt_sha256",
                "original_title",
                "final_title",
                "original_selection_hook",
                "final_selection_hook",
                "status",
                "decision",
            )
        },
        "public_text_authority": {
            "relative_path": "assets/lidousha/candidate_public_text_surface_authorities/"
            "auto_173005_934_1166.public-text-surface-authority.v1.json",
            "authority_sha256": authority.authority_sha256,
        },
        "runtime_bindings": {
            "selection_hook": hook,
            "selection_hook_sha256": _text_sha(hook),
            "title": title,
            "title_sha256": _text_sha(title),
            "final_reviewed_srt_sha256": "sha256:"
            + hashlib.sha256(srt.read_bytes()).hexdigest(),
            "final_transcript_sha256": _text_sha(transcript),
            "clip_context_prompt_sha256": _text_sha(prompt),
            "selection_scorecard_sha256": _sha(scorecard),
            "speaker_evidence_sha256": _sha(speaker),
            "entity_context_sha256": context["context_sha256"],
            "story_contract_sha256": _sha(contract),
        },
        "preimages": {
            "state_record_canonical_sha256": "sha256:9c712111a12183b6d664fe55a87c62be5e401f6da7c4e6acf181e4f918d20043",
            "state_file": {"path": "/opt/bilive/autoslice/state/2026-08-11.json", "sha256": "sha256:f6c2af20879f47fb287f798ef2862ea34234c321544a848f3545956dc899eda1", "bytes": 416317, "uid": 0, "gid": 0, "mode": 0o644, "post_mode": 0o644, "type": "regular"},
            "documents": [
                {"path": "/opt/bilive/autoslice/out/2026-08-11/auto_173005_934_1166/replacement_recuts/auto_173005_934_1166.record.json", "sha256": "sha256:0ce47d7129c2b920d4cdaefc814cf58cc7eca6d2481e651af060c94f5dc99e6f", "bytes": 118043, "uid": 0, "gid": 0, "mode": 0o600, "post_mode": 0o600, "type": "regular"},
                {"path": "/opt/bilive/autoslice/repo/lidousha/2026-08-11/本想看“CP误解向”，李豆沙却越看越.record.json", "sha256": "sha256:0ce47d7129c2b920d4cdaefc814cf58cc7eca6d2481e651af060c94f5dc99e6f", "bytes": 118043, "uid": 0, "gid": 0, "mode": 0o600, "post_mode": 0o600, "type": "regular"},
                {"path": "/opt/bilive/autoslice/out/2026-08-11/auto_173005_934_1166/replacement_recuts/auto_173005_934_1166.recut.publish.json", "sha256": "sha256:b019d927606c71d6b701a0cf44b7c45d7661f0da98b7c3beea167d6ff7df0572", "bytes": 63182, "uid": 0, "gid": 0, "mode": 0o600, "post_mode": 0o600, "type": "regular"},
            ],
        },
        "scope": {
            "candidate_scoped": True,
            "provider_call_required": False,
            "subtitle_text_mutation_authorized": False,
            "speaker_label_mutation_authorized": False,
            "upload_authorized": False,
        },
    }
    authority_document["authority_sha256"] = _sha(authority_document)
    monkeypatch.setattr(refresh, "_load_authority", lambda _root: authority_document)
    monkeypatch.setattr(
        refresh, "load_candidate_public_text_surface_authority", lambda *_a, **_k: authority
    )
    return authority, historical, contract, srt, transcript, scorecard, speaker, authority_document


def _frozen_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """A stable four-target runtime; descriptors never follow a mutation."""

    _authority, historical, contract, srt, _text, _score, _speaker, _document = _fixture(tmp_path, monkeypatch)
    title = "【李豆沙】经小李判断，薇欧拉对阿拉蕾就是铁暗恋！"
    def record() -> dict[str, object]:
        return {"schema_version": "delivery-record.v1", "subtitle_path": str(srt), "story_contract": copy.deepcopy(contract), "publish_staging": {"title": title, "source_fact_review": copy.deepcopy(historical)}}
    paths = [tmp_path / "one.record.json", tmp_path / "two.record.json", tmp_path / "draft.publish.json"]
    documents = [record(), record(), {"schema_version": "shadow-publish-draft.v1", "title": title, "source_fact_review": copy.deepcopy(historical)}]
    for path, document in zip(paths, documents, strict=True):
        path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    base = tmp_path / "runtime"
    (base / "out" / "2026-08-11" / refresh.CANDIDATE_ID).mkdir(parents=True)
    (base / "state").mkdir()
    (base / "DISABLED").write_text("", encoding="utf-8")
    state = {"picks": [{"candidate_id": refresh.CANDIDATE_ID, "title": title}]}
    state_path = base / "state" / "2026-08-11.json"
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    def descriptor(path: Path) -> dict[str, object]:
        stat_result = path.stat()
        return {"path": str(path), "sha256": refresh_runner._file_sha(path), "bytes": stat_result.st_size, "uid": stat_result.st_uid, "gid": stat_result.st_gid, "mode": stat_result.st_mode & 0o777, "post_mode": stat_result.st_mode & 0o777, "type": "regular"}
    frozen = {"state_record_canonical_sha256": refresh_runner._canonical_sha(state["picks"][0]), "state_file": descriptor(state_path), "documents": [descriptor(path) for path in paths]}
    receipt = {"receipt_sha256": "sha256:" + "2" * 64, "status": "PASS", "historical_provider_receipt": {"speaker_evidence": copy.deepcopy(historical["speaker_evidence"])}, "candidate_public_text_source_fact_refresh": {"authority_sha256": "sha256:" + "3" * 64}}
    monkeypatch.setattr(refresh_runner, "BASE", base)
    monkeypatch.setattr(refresh_runner, "_active_documents", lambda **_k: list(zip(paths, documents, strict=True)))
    monkeypatch.setattr(refresh_runner, "load_candidate_source_fact_refresh_preimages", lambda **_k: copy.deepcopy(frozen))
    monkeypatch.setattr(refresh_runner, "build_candidate_public_text_source_fact_refresh_review", lambda **_k: copy.deepcopy(receipt))
    monkeypatch.setattr(refresh_runner, "validate_source_fact_review", lambda *_a, **_k: True)
    return {"base": base, "paths": paths, "state": state_path, "receipt": receipt}


def test_typed_refresh_preserves_exact_provider_receipt_and_rebinds_current_title(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    authority, historical, contract, srt, transcript, scorecard, speaker, authority_document = _fixture(
        tmp_path, monkeypatch
    )
    review = refresh.build_candidate_public_text_source_fact_refresh_review(
        repo_root=tmp_path,
        historical_provider_receipt=historical,
        candidate_id=refresh.CANDIDATE_ID,
        selection_hook=authority.resolved_selection_hook,
        title=authority.resolved_title,
        final_transcript=transcript,
        clip_context_prompt=str(contract["clip_context_prompt"]),
        selection_scorecard=scorecard,
        story_contract=contract,
        final_reviewed_srt_path=srt,
        speaker_evidence=speaker,
    )
    assert review["historical_provider_receipt"] == historical
    assert review["final_title"] == authority.resolved_title
    assert review["decision"] == refresh.DECISION
    after_contract = copy.deepcopy(contract)
    after_contract["source_fact_review"] = review
    assert validate_source_fact_review(
        review,
        selection_hook=authority.resolved_selection_hook,
        title=authority.resolved_title,
        final_transcript=transcript,
        clip_context_prompt=str(contract["clip_context_prompt"]),
        selection_scorecard=scorecard,
        story_contract=after_contract,
        candidate_id=refresh.CANDIDATE_ID,
        final_reviewed_srt_path=srt,
        speaker_evidence=speaker,
    )

    def valid(**overrides: object) -> bool:
        values: dict[str, object] = {
            "selection_hook": authority.resolved_selection_hook,
            "title": authority.resolved_title,
            "final_transcript": transcript,
            "clip_context_prompt": str(contract["clip_context_prompt"]),
            "selection_scorecard": scorecard,
            "story_contract": after_contract,
            "candidate_id": refresh.CANDIDATE_ID,
            "final_reviewed_srt_path": srt,
            "speaker_evidence": speaker,
        }
        values.update(overrides)
        return validate_source_fact_review(review, **values)  # type: ignore[arg-type]

    tampered_pass = copy.deepcopy(review)
    tampered_pass["historical_provider_receipt"]["passes"] = []
    assert not validate_source_fact_review(tampered_pass, **{
        "selection_hook": authority.resolved_selection_hook,
        "title": authority.resolved_title,
        "final_transcript": transcript,
        "clip_context_prompt": str(contract["clip_context_prompt"]),
        "selection_scorecard": scorecard,
        "story_contract": contract,
        "candidate_id": refresh.CANDIDATE_ID,
        "final_reviewed_srt_path": srt,
        "speaker_evidence": speaker,
    })
    assert not valid(title=authority.superseded_title)
    previous_context = authority_document["runtime_bindings"]["entity_context_sha256"]
    authority_document["runtime_bindings"]["entity_context_sha256"] = "sha256:" + "0" * 64
    assert not valid()
    authority_document["runtime_bindings"]["entity_context_sha256"] = previous_context
    tampered_status = copy.deepcopy(review)
    tampered_status["historical_provider_receipt"]["status"] = "FAIL"
    assert not validate_source_fact_review(tampered_status, **{
        "selection_hook": authority.resolved_selection_hook,
        "title": authority.resolved_title,
        "final_transcript": transcript,
        "clip_context_prompt": str(contract["clip_context_prompt"]),
        "selection_scorecard": scorecard,
        "story_contract": after_contract,
        "candidate_id": refresh.CANDIDATE_ID,
        "final_reviewed_srt_path": srt,
        "speaker_evidence": speaker,
    })
    sealed_status = authority_document["historical_provider_receipt"]["status"]
    authority_document["historical_provider_receipt"]["status"] = "FAIL"
    assert not valid()
    authority_document["historical_provider_receipt"]["status"] = sealed_status
    sealed_decision = authority_document["historical_provider_receipt"]["decision"]
    authority_document["historical_provider_receipt"]["decision"] = "KEEP"
    assert not valid()
    authority_document["historical_provider_receipt"]["decision"] = sealed_decision
    previous_srt = srt.read_bytes()
    srt.write_text("1\n00:00:00,000 --> 00:00:01,000\n漂移\n", encoding="utf-8")
    assert not valid()
    srt.write_bytes(previous_srt)


def test_post_projection_passes_daily_builder_and_package_source_fact_auditor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    authority, historical, contract, srt, transcript, scorecard, speaker, _authority_document = _fixture(
        tmp_path, monkeypatch
    )
    review = refresh.build_candidate_public_text_source_fact_refresh_review(
        repo_root=tmp_path,
        historical_provider_receipt=historical,
        candidate_id=refresh.CANDIDATE_ID,
        selection_hook=authority.resolved_selection_hook,
        title=authority.resolved_title,
        final_transcript=transcript,
        clip_context_prompt=str(contract["clip_context_prompt"]),
        selection_scorecard=scorecard,
        story_contract=contract,
        final_reviewed_srt_path=srt,
        speaker_evidence=speaker,
    )
    post_contract = copy.deepcopy(contract)
    post_contract["source_fact_review"] = review
    record = {
        "speaker_mode": "uniform_host",
        "story_contract": post_contract,
        "publish_staging": {"title": authority.resolved_title, "source_fact_review": review},
    }
    publish = {"title": authority.resolved_title, "source_fact_review": review}
    assert _validate_source_fact_receipts(
        record_doc=record,
        publish_doc=publish,
        subtitle_path=srt,
        speaker_evidence=speaker,
        qixi_repo_root=tmp_path,
    ) == review["receipt_sha256"]
    publish_path = tmp_path / "draft.publish.json"
    publish_path.write_text(json.dumps(publish, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(
        package_source_fact_audit,
        "rebuild_package_speaker_evidence",
        lambda **_kwargs: speaker,
    )
    assert package_source_fact_audit.audit_story_source_fact_receipt(
        root=tmp_path,
        manifest={},
        item={},
        subtitle_path=srt,
        publish_path=publish_path,
        record_path=None,
        record=record,
        story_contract=post_contract,
        artifact_title=authority.resolved_title,
        final_transcript=transcript,
        qixi_repo_root=tmp_path,
    ) == ()


def test_prepare_projects_only_receipt_mirrors_and_preflight_failure_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _authority, historical, contract, srt, _transcript, _scorecard, _speaker, _authority_document = _fixture(
        tmp_path, monkeypatch
    )
    title = "【李豆沙】经小李判断，薇欧拉对阿拉蕾就是铁暗恋！"

    def record() -> dict[str, object]:
        return {
            "schema_version": "delivery-record.v1",
            "subtitle_path": str(srt),
            "artifact_hashes": {"video_sha256": "sha256:" + "1" * 64},
            "story_contract": copy.deepcopy(contract),
            "publish_staging": {"title": title, "source_fact_review": copy.deepcopy(historical)},
        }

    def publish() -> dict[str, object]:
        return {
            "schema_version": "shadow-publish-draft.v1",
            "title": title,
            "source_fact_review": copy.deepcopy(historical),
        }

    paths = [tmp_path / "delivery.record.json", tmp_path / "source.record.json", tmp_path / "draft.publish.json"]
    docs = [record(), record(), publish()]
    for path, document in zip(paths, docs, strict=True):
        path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    active = [(path, document) for path, document in zip(paths, docs, strict=True)]
    state = {"picks": [{"candidate_id": refresh.CANDIDATE_ID, "title": title}]}
    monkeypatch.setattr(refresh_runner, "_active_documents", lambda **_k: active)
    def descriptor(path: Path) -> dict[str, object]:
        info = path.stat()
        return {
            "path": str(path), "sha256": refresh_runner._file_sha(path), "bytes": info.st_size,
            "uid": info.st_uid, "gid": info.st_gid, "mode": info.st_mode & 0o777,
            "post_mode": info.st_mode & 0o777, "type": "regular",
        }

    def preimages(**_k: object) -> dict[str, object]:
        state_file = refresh_runner._state_path("2026-08-11")
        state_descriptor = descriptor(state_file) if state_file.exists() else {
            "path": str(state_file), "sha256": "sha256:" + "0" * 64, "bytes": 0,
            "uid": 0, "gid": 0, "mode": 0o600, "post_mode": 0o600, "type": "regular",
        }
        return {
            "state_record_canonical_sha256": refresh_runner._canonical_sha(state["picks"][0]),
            "state_file": state_descriptor,
            "documents": [descriptor(path) for path in paths],
        }

    monkeypatch.setattr(refresh_runner, "load_candidate_source_fact_refresh_preimages", preimages)
    new_receipt = {
        "receipt_sha256": "sha256:" + "2" * 64,
        "status": "PASS",
        "historical_provider_receipt": {"speaker_evidence": copy.deepcopy(historical["speaker_evidence"])},
        "candidate_public_text_source_fact_refresh": {"authority_sha256": "sha256:" + "3" * 64},
    }
    monkeypatch.setattr(refresh_runner, "build_candidate_public_text_source_fact_refresh_review", lambda **_k: new_receipt)
    monkeypatch.setattr(refresh_runner, "validate_source_fact_review", lambda *_a, **_k: True)
    updated, intended, receipt = refresh_runner.prepare_refresh(date="2026-08-11", state=state)
    assert receipt == new_receipt
    assert updated["picks"][0]["source_fact_review"] == new_receipt
    for _path, before, after in intended:
        old, new = json.loads(before), json.loads(after)
        if new["schema_version"] == "shadow-publish-draft.v1":
            old.pop("source_fact_review")
            new.pop("source_fact_review")
        else:
            old["story_contract"].pop("source_fact_review")
            new["story_contract"].pop("source_fact_review")
            old["publish_staging"].pop("source_fact_review")
            new["publish_staging"].pop("source_fact_review")
        assert old == new

    active[0][1]["publish_staging"].pop("source_fact_review")
    originals = {path: path.read_bytes() for path in paths}
    with pytest.raises(refresh_runner.CandidateSourceFactRefreshRunError):
        refresh_runner.prepare_refresh(date="2026-08-11", state=state)
    assert {path: path.read_bytes() for path in paths} == originals

    active[0] = (paths[0], record())
    disabled_root = tmp_path / "runtime"
    (disabled_root / "out" / "2026-08-11" / refresh.CANDIDATE_ID).mkdir(parents=True)
    (disabled_root / "state").mkdir()
    (disabled_root / "DISABLED").write_text("", encoding="utf-8")
    state_path = disabled_root / "state" / "2026-08-11.json"
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    monkeypatch.setattr(refresh_runner, "BASE", disabled_root)
    result = refresh_runner.run(date="2026-08-11")
    assert result["status"] == "REFRESHED_NO_PROVIDER"
    journal = json.loads(Path(result["transaction_receipt"]).read_text(encoding="utf-8"))
    assert journal["status"] == journal["transaction_receipt"]["status"] == "PREPARED"
    assert (Path(result["transaction_receipt"]).parent / "finalized-receipt.json").is_file()
    assert json.loads(state_path.read_text(encoding="utf-8"))["picks"][0]["source_fact_review"] == new_receipt
    target_bytes = {path: path.read_bytes() for path in [*paths, state_path]}
    (Path(result["transaction_receipt"]).parent / "foreign-artifact").write_text("foreign", encoding="utf-8")
    with pytest.raises(refresh_runner.CandidateSourceFactRefreshRunError):
        refresh_runner.run(date="2026-08-11")
    assert {path: path.read_bytes() for path in [*paths, state_path]} == target_bytes


@pytest.mark.parametrize("phase", ["intent", "finalized"])
def test_refresh_replays_creation_interruptions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    _frozen_runtime(tmp_path, monkeypatch)
    real_create = refresh_runner._create_private_bytes
    def interrupted(path: Path, payload: bytes, **kwargs: object) -> None:
        if path.name == ("intent.json" if phase == "intent" else "finalized-receipt.json"):
            raise OSError("injected")
        real_create(path, payload, **kwargs)
    with monkeypatch.context() as scoped:
        scoped.setattr(refresh_runner, "_create_private_bytes", interrupted)
        with pytest.raises(OSError, match="injected"):
            refresh_runner.run(date="2026-08-11")
    result = refresh_runner.run(date="2026-08-11")
    root = Path(result["transaction_receipt"]).parent
    assert (root / "finalized-receipt.json").is_file()
    assert not list(root.glob("target-*.tmp"))


@pytest.mark.parametrize("index", range(4))
@pytest.mark.parametrize("phase", ["before", "after_temp", "after_replace"])
def test_refresh_replays_each_target_interruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, index: int, phase: str
) -> None:
    _frozen_runtime(tmp_path, monkeypatch)
    real_install = refresh_runner._install_target_bytes
    calls = 0
    def interrupted(*args: object, **kwargs: object) -> None:
        nonlocal calls
        if calls == index:
            calls += 1
            if phase == "before":
                raise OSError("injected")
            real_install(*args, **kwargs)
            raise OSError("injected")
        calls += 1
        real_install(*args, **kwargs)
    with monkeypatch.context() as scoped:
        scoped.setattr(refresh_runner, "_install_target_bytes", interrupted)
        with pytest.raises(OSError, match="injected"):
            refresh_runner.run(date="2026-08-11")
    result = refresh_runner.run(date="2026-08-11")
    root = Path(result["transaction_receipt"]).parent
    assert (root / "finalized-receipt.json").is_file()
    assert not list(root.glob("target-*.tmp"))


@pytest.mark.parametrize("index", range(4))
def test_refresh_replays_post_temp_fsync_interruptions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, index: int
) -> None:
    _frozen_runtime(tmp_path, monkeypatch)
    calls = 0
    def interrupt(_temporary: Path) -> None:
        nonlocal calls
        if calls == index:
            calls += 1
            raise OSError("after-fsync")
        calls += 1
    with monkeypatch.context() as scoped:
        scoped.setattr(refresh_runner, "_TARGET_TEMP_FSYNC_HOOK", interrupt)
        with pytest.raises(OSError, match="after-fsync"):
            refresh_runner.run(date="2026-08-11")
    result = refresh_runner.run(date="2026-08-11")
    assert (Path(result["transaction_receipt"]).parent / "finalized-receipt.json").is_file()
    assert not list(Path(result["transaction_receipt"]).parent.glob("target-*.tmp"))


@pytest.mark.parametrize(
    "name",
    ["intent.json", "finalized-receipt.json", *(f"target-{index:02d}.tmp" for index in range(4))],
)
@pytest.mark.parametrize("symlink", [False, True])
def test_refresh_rejects_foreign_root_artifacts_before_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, symlink: bool
) -> None:
    runtime = _frozen_runtime(tmp_path, monkeypatch)
    base = runtime["base"]
    assert isinstance(base, Path)
    root = base / "out" / "2026-08-11" / refresh.CANDIDATE_ID / ".source-fact-refresh-private" / "transaction"
    root.parent.mkdir(mode=0o700)
    root.mkdir(mode=0o700)
    foreign = root / name
    if symlink:
        foreign.symlink_to(root / "missing")
    else:
        foreign.write_text("foreign", encoding="utf-8")
    targets = [*runtime["paths"], runtime["state"]]
    assert all(isinstance(path, Path) for path in targets)
    before = {path: path.read_bytes() for path in targets}
    with pytest.raises(refresh_runner.CandidateSourceFactRefreshRunError):
        refresh_runner.run(date="2026-08-11")
    assert {path: path.read_bytes() for path in targets} == before
    assert foreign.exists() or foreign.is_symlink()


@pytest.mark.parametrize("symlink", [False, True])
def test_refresh_rejects_foreign_transaction_root_before_any_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, symlink: bool
) -> None:
    runtime = _frozen_runtime(tmp_path, monkeypatch)
    base = runtime["base"]
    assert isinstance(base, Path)
    root = base / "out" / "2026-08-11" / refresh.CANDIDATE_ID / ".source-fact-refresh-private" / "transaction"
    root.parent.mkdir(mode=0o700)
    if symlink:
        root.symlink_to(root.parent / "missing-root")
    else:
        root.write_text("foreign root", encoding="utf-8")
    targets = [*runtime["paths"], runtime["state"]]
    assert all(isinstance(path, Path) for path in targets)
    before = {path: path.read_bytes() for path in targets}
    with pytest.raises(refresh_runner.CandidateSourceFactRefreshRunError):
        refresh_runner.run(date="2026-08-11")
    assert {path: path.read_bytes() for path in targets} == before
    assert root.is_symlink() if symlink else root.read_text(encoding="utf-8") == "foreign root"


@pytest.mark.parametrize("index", range(4))
@pytest.mark.parametrize("symlink", [False, True])
def test_refresh_rejects_foreign_target_before_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, index: int, symlink: bool
) -> None:
    runtime = _frozen_runtime(tmp_path, monkeypatch)
    paths = runtime["paths"]
    assert isinstance(paths, list)
    all_targets = [*paths, runtime["state"]]
    assert all(isinstance(path, Path) for path in all_targets)
    target = all_targets[index]
    assert isinstance(target, Path)
    if symlink:
        target.unlink()
        target.symlink_to(tmp_path / "missing-target")
    else:
        target.write_text("foreign", encoding="utf-8")
    others = [path for path in all_targets if path != target]
    before = {path: path.read_bytes() for path in others}
    with pytest.raises(refresh_runner.CandidateSourceFactRefreshRunError):
        refresh_runner.run(date="2026-08-11")
    assert {path: path.read_bytes() for path in others} == before
    assert target.is_symlink() if symlink else target.read_text(encoding="utf-8") == "foreign"


def _stage_frozen_intent(runtime: dict[str, object]) -> tuple[Path, dict[str, object]]:
    state = runtime["state"]
    assert isinstance(state, Path)
    raw = state.read_bytes()
    decoded = json.loads(raw)
    updated, intended, receipt = refresh_runner.prepare_refresh(date="2026-08-11", state=decoded)
    intent_path, journal = refresh_runner._stage_transaction(
        date="2026-08-11", state_raw=raw, updated=updated, intended=intended, receipt=receipt
    )
    return intent_path, journal


@pytest.mark.parametrize(
    "name",
    ["finalized-receipt.json", *(f"target-{index:02d}.tmp" for index in range(4))],
)
@pytest.mark.parametrize("symlink", [False, True])
def test_refresh_rejects_foreign_artifact_after_intent_before_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, symlink: bool
) -> None:
    runtime = _frozen_runtime(tmp_path, monkeypatch)
    intent_path, _journal = _stage_frozen_intent(runtime)
    foreign = intent_path.parent / name
    if symlink:
        foreign.symlink_to(intent_path.parent / "foreign")
    else:
        foreign.write_text("foreign", encoding="utf-8")
    targets = [*runtime["paths"], runtime["state"]]
    assert all(isinstance(path, Path) for path in targets)
    before = {path: path.read_bytes() for path in targets}
    with pytest.raises(refresh_runner.CandidateSourceFactRefreshRunError):
        refresh_runner.run(date="2026-08-11")
    assert {path: path.read_bytes() for path in targets} == before
    assert foreign.exists() or foreign.is_symlink()


@pytest.mark.parametrize("role", ["state", "document"])
def test_refresh_rejects_resealed_unrelated_intended_mutation_before_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    runtime = _frozen_runtime(tmp_path, monkeypatch)
    intent_path, journal = _stage_frozen_intent(runtime)
    entries = journal["entries"]
    assert isinstance(entries, list)
    state_path = runtime["state"]
    assert isinstance(state_path, Path)
    entry = next(
        row for row in entries
        if (Path(str(row["target"])) == state_path) == (role == "state")
    )
    after = json.loads(refresh_runner._unb64(entry["after_bytes_b64"]).decode("utf-8"))
    after["unrelated_resealed_tamper"] = role
    payload = refresh_runner._journal_bytes(after)
    entry["after_bytes_b64"] = refresh_runner._b64(payload)
    entry["after_sha256"] = refresh_runner._sha256(payload)
    receipt = journal["source_fact_review"]
    assert isinstance(receipt, dict)
    journal["transaction_receipt"] = refresh_runner._signed_receipt(
        status="PREPARED",
        authority_sha256=str(journal["authority_sha256"]),
        receipt_sha256=str(receipt["receipt_sha256"]),
        targets_sha256=str(journal["sealed_targets_sha256"]),
        entries=entries,
    )
    refresh_runner._signed_journal(journal)
    intent_path.write_bytes(refresh_runner._journal_bytes(journal))
    targets = [*runtime["paths"], state_path]
    assert all(isinstance(path, Path) for path in targets)
    before = {path: path.read_bytes() for path in targets}
    with pytest.raises(refresh_runner.CandidateSourceFactRefreshRunError):
        refresh_runner.run(date="2026-08-11")
    assert {path: path.read_bytes() for path in targets} == before


def test_refresh_rejects_cross_device_before_target_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _frozen_runtime(tmp_path, monkeypatch)
    targets = [*runtime["paths"], runtime["state"]]
    assert all(isinstance(path, Path) for path in targets)
    before = {path: path.read_bytes() for path in targets}
    root = refresh_runner._transaction_root("2026-08-11")
    monkeypatch.setattr(refresh_runner, "_target_parent_device", lambda _path: root.parent.stat().st_dev + 1)
    with pytest.raises(refresh_runner.CandidateSourceFactRefreshRunError, match="transaction filesystem"):
        refresh_runner.run(date="2026-08-11")
    assert {path: path.read_bytes() for path in targets} == before


def test_refresh_rejects_live_drift_immediately_before_final_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _frozen_runtime(tmp_path, monkeypatch)
    target = runtime["paths"][0]
    assert isinstance(target, Path)
    def drift(_journal: object) -> None:
        target.write_text("foreign pre-final drift", encoding="utf-8")
    monkeypatch.setattr(refresh_runner, "_FINAL_RECEIPT_PREWRITE_HOOK", drift)
    with pytest.raises(refresh_runner.CandidateSourceFactRefreshRunError, match="final target snapshot drifted"):
        refresh_runner.run(date="2026-08-11")
    root = refresh_runner._transaction_root("2026-08-11")
    assert not (root / "finalized-receipt.json").exists()
    assert target.read_text(encoding="utf-8") == "foreign pre-final drift"


_PRIVATE_ARTIFACTS = [
    "intent.json", *(f"target-{index:02d}.tmp" for index in range(4)), "finalized-receipt.json",
]


@pytest.mark.parametrize("artifact", _PRIVATE_ARTIFACTS)
@pytest.mark.parametrize("phase", ["partial", "before_publish", "after_publish"])
def test_refresh_recovers_private_pending_publication_interruptions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, artifact: str, phase: str
) -> None:
    _frozen_runtime(tmp_path, monkeypatch)
    def match(path: Path) -> bool:
        return path.name == artifact + (".pending" if phase == "partial" else "")
    with monkeypatch.context() as scoped:
        if phase == "partial":
            scoped.setattr(
                refresh_runner, "_PRIVATE_PENDING_PARTIAL_HOOK",
                lambda pending: (_ for _ in ()).throw(OSError("pending-partial")) if match(pending) else None,
            )
        elif phase == "before_publish":
            scoped.setattr(
                refresh_runner, "_PRIVATE_PUBLICATION_BEFORE_PUBLISH_HOOK",
                lambda final: (_ for _ in ()).throw(OSError("pending-before-publish")) if match(final) else None,
            )
        else:
            scoped.setattr(
                refresh_runner, "_PRIVATE_PUBLICATION_AFTER_PUBLISH_HOOK",
                lambda final: (_ for _ in ()).throw(OSError("pending-after-publish")) if match(final) else None,
            )
        with pytest.raises(OSError, match="pending-"):
            refresh_runner.run(date="2026-08-11")
    result = refresh_runner.run(date="2026-08-11")
    root = Path(result["transaction_receipt"]).parent
    assert (root / "finalized-receipt.json").is_file()
    assert not list(root.glob("*.pending"))
    assert not list(root.glob("target-*.tmp"))


@pytest.mark.parametrize("artifact", _PRIVATE_ARTIFACTS)
@pytest.mark.parametrize("symlink", [False, True])
def test_refresh_rejects_foreign_pending_artifact_before_target_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, artifact: str, symlink: bool
) -> None:
    runtime = _frozen_runtime(tmp_path, monkeypatch)
    if artifact == "intent.json":
        base = runtime["base"]
        assert isinstance(base, Path)
        root = base / "out" / "2026-08-11" / refresh.CANDIDATE_ID / ".source-fact-refresh-private" / "transaction"
        root.parent.mkdir(mode=0o700)
        root.mkdir(mode=0o700)
    else:
        intent_path, _journal = _stage_frozen_intent(runtime)
        root = intent_path.parent
    foreign = root / (artifact + ".pending")
    if symlink:
        foreign.symlink_to(root / "foreign-pending")
    else:
        foreign.write_text("foreign pending", encoding="utf-8")
    targets = [*runtime["paths"], runtime["state"]]
    assert all(isinstance(path, Path) for path in targets)
    before = {path: path.read_bytes() for path in targets}
    with pytest.raises(refresh_runner.CandidateSourceFactRefreshRunError):
        refresh_runner.run(date="2026-08-11")
    assert {path: path.read_bytes() for path in targets} == before
    assert foreign.exists() or foreign.is_symlink()
