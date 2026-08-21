import hashlib
import json
from pathlib import Path

import pytest

from src.autoslice import reviewed_cover_committed_recovery_authority as recovery
from src.autoslice.repository_asset_authority import (
    DEPLOYED_AUTHORITY_MANIFEST,
    build_deployed_authority_manifest,
)


TITLE = "【李豆沙】经小李判断，薇欧拉对阿拉蕾就是铁暗恋！"
CANDIDATE = "auto_173005_934_1166"


def _sha(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _write_json(path: Path, value: object) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()
    path.write_bytes(raw)
    return raw


def _entry(runtime: Path, path: Path) -> dict[str, str]:
    return {
        "absolute_path": str(path.resolve()),
        "runtime_relative_path": path.resolve().relative_to(runtime.resolve()).as_posix(),
        "sha256": _sha(path.read_bytes()),
    }


def _fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    runtime = tmp_path / "runtime"
    repo = runtime / "repo"
    plan_path = repo / "assets/plan.json"
    plan_raw = _write_json(plan_path, {"candidate_id": CANDIDATE})
    generation_root = runtime / "out/2026-08-11" / CANDIDATE / "cover_repair/generations/old"
    final_cover = generation_root / "final.cover.png"
    final_cover.parent.mkdir(parents=True, exist_ok=True)
    final_cover.write_bytes(b"sealed cover")
    delivery_cover = repo / "lidousha/2026-08-11/delivery.cover.png"
    delivery_cover.parent.mkdir(parents=True, exist_ok=True)
    delivery_cover.write_bytes(final_cover.read_bytes())
    generation_path = generation_root / "final.cover.cover_generation.json"
    generation = {
        "candidate_id": CANDIDATE,
        "title": TITLE,
        "status": "AI_COVER_READY",
        "final_cover": str(final_cover),
        "final_cover_sha256": _sha(final_cover.read_bytes()),
    }
    _write_json(generation_path, generation)
    binding_path = generation_root / "final.cover.cover-binding.json"
    binding = {
        "schema_version": "lidousha-cover-repair-binding.v1",
        "candidate_id": CANDIDATE,
        "title": TITLE,
        "cover_path": str(delivery_cover),
        "cover_sha256": _sha(delivery_cover.read_bytes()),
        "generation_manifest_path": str(generation_path),
        "generation_manifest_sha256": _sha(generation_path.read_bytes()),
        "generation_cover_path": str(final_cover),
        "generation_cover_sha256": _sha(final_cover.read_bytes()),
    }
    _write_json(binding_path, binding)
    view = {
        "title": TITLE, "cover_path": str(delivery_cover), "cover_generation": generation,
    }
    pointer = {"path": str(binding_path), "sha256": _sha(binding_path.read_bytes())}
    video = runtime / "out/2026-08-11" / CANDIDATE / "delivery.mp4"
    subtitle = runtime / "out/2026-08-11" / CANDIDATE / "delivery.srt"
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"frozen video")
    subtitle.write_bytes(b"frozen subtitles")
    documents = []
    for index in range(3):
        path = runtime / "out/2026-08-11" / CANDIDATE / "docs" / f"{index}.json"
        document = {
            "artifact_hashes": {
                "cover_sha256": _sha(final_cover.read_bytes()),
                "video_sha256": _sha(video.read_bytes()),
                "subtitle_sha256": _sha(subtitle.read_bytes()),
            },
            "cover_repair_binding": pointer,
            "media_path": str(video),
            "subtitle_path": str(subtitle),
        }
        if index == 2:
            document.update({"schema_version": "shadow-publish-draft.v1", **view})
        else:
            document["publish_staging"] = view
            document["boundary_audit"] = {"frozen": True}
            document["story_contract"] = {"frozen": True}
        _write_json(path, document)
        documents.append(path)
    delivery_publish = repo / "lidousha/2026-08-11/delivery.publish.json"
    delivery_publish.write_bytes(documents[2].read_bytes())
    transaction_path = generation_root / "cover-transaction.json"
    transaction_targets = [
        ("delivery_cover", delivery_cover), ("binding", binding_path),
        ("delivery_publish", delivery_publish), ("delivery_record", documents[0]),
        ("source_record", documents[1]), ("source_publish", documents[2]),
    ]
    transaction_entries = []
    for index, (_role, target) in enumerate(transaction_targets):
        blob = generation_root / "transaction/intended" / f"{index:03d}.bin"
        blob.parent.mkdir(parents=True, exist_ok=True)
        blob.write_bytes(target.read_bytes())
        transaction_entries.append({
            "target": str(target), "intended_blob": str(blob),
            "intended_sha256": _sha(blob.read_bytes()),
        })
    _write_json(transaction_path, {
        "schema_version": "lidousha-cover-transaction.v1", "status": "COMMITTED",
        "candidate_id": CANDIDATE, "title": TITLE, "upload_enabled": False,
        "generation_dir": str(generation_root), "entries": transaction_entries,
    })
    journal_path = runtime / "out/2026-08-11/reviewed_cover_repairs/plan/invalidation-transaction.json"
    journal = {
        "schema_version": "reviewed-cover-invalidation-transaction.v1", "status": "COMMITTED",
        "date": "2026-08-11", "plan_sha256": hashlib.sha256(plan_raw).hexdigest(),
        "code_fingerprint": "sha256:" + "a" * 64, "upload_enabled": False, "entries": [],
    }
    _write_json(journal_path, journal)
    record = {"candidate_id": CANDIDATE, "title": TITLE, "reviewed_cover_repair": {"plan_sha256": journal["plan_sha256"]}}
    authority = {
        "schema_version": recovery.SCHEMA_VERSION,
        "candidate": {"candidate_id": CANDIDATE, "recording_date": "2026-08-11", "title": TITLE},
        "plan": {"repo_relative_path": "assets/plan.json", "file_sha256": _sha(plan_raw), "plan_sha256": journal["plan_sha256"]},
        "invalidation_journal": {
            **_entry(runtime, journal_path), "schema_version": journal["schema_version"],
            "status": "COMMITTED", "old_code_fingerprint": journal["code_fingerprint"],
        },
        "state_preimage": {"canonical_sha256": recovery._canonical_state_sha256(record)},
        "successor_code_fingerprint": "sha256:" + "e" * 64,
        "successor": {
            "transaction": _entry(runtime, transaction_path),
            "generation_manifest": _entry(runtime, generation_path),
            "binding": _entry(runtime, binding_path),
            "generated_final_cover": _entry(runtime, final_cover),
            "delivery_cover": _entry(runtime, delivery_cover),
            "delivery_publish": _entry(runtime, delivery_publish),
            "transaction_entries": [
                {
                    "role": role,
                    "target_absolute_path": str(target),
                    "target_runtime_relative_path": target.relative_to(runtime).as_posix(),
                    "intended_blob_absolute_path": transaction_entries[index]["intended_blob"],
                    "intended_blob_runtime_relative_path": Path(transaction_entries[index]["intended_blob"]).relative_to(runtime).as_posix(),
                    "intended_sha256": transaction_entries[index]["intended_sha256"],
                }
                for index, (role, target) in enumerate(transaction_targets)
            ],
        },
        "active_documents": [{**_entry(runtime, path), "title": TITLE} for path in documents],
        "immutable": {
            "video": _entry(runtime, video), "subtitle": _entry(runtime, subtitle),
            "ass": {"absolute_path": None, "sha256": None},
            "boundary_audit_sha256": recovery._json_sha256({"frozen": True}),
            "story_contract_sha256": recovery._json_sha256({"frozen": True}),
        },
    }
    authority["authority_sha256"] = recovery._json_sha256(authority)
    authority_path = repo / "assets/recovery.json"
    _write_json(authority_path, authority)
    monkeypatch.setattr(recovery, "AUTHORITY_PATH", Path("assets/recovery.json"))
    monkeypatch.setattr(recovery, "require_repository_asset_authority", lambda **_kwargs: object())
    return runtime, repo, plan_path, journal_path, journal, record, authority, generation_root


def _validate(fx):
    runtime, repo, plan_path, journal_path, journal, record, authority, _generation_root = fx
    recovery.validate_committed_recovery(
        repo_root=repo, runtime_root=runtime, plan_path=plan_path,
        plan_sha256=journal["plan_sha256"], journal_path=journal_path, journal=journal,
        records={CANDIDATE: record}, state_is_bound=True,
        code_fingerprint=authority["successor_code_fingerprint"],
    )


def _reseal_authority(repo: Path, authority: dict) -> None:
    authority["authority_sha256"] = recovery._json_sha256(
        {key: value for key, value in authority.items() if key != "authority_sha256"}
    )
    _write_json(repo / recovery.AUTHORITY_PATH, authority)


def test_two_layer_committed_recovery_replays_sealed_candidate_without_writes(tmp_path, monkeypatch):
    fx = _fixture(tmp_path, monkeypatch)
    before = {path: path.read_bytes() for path in fx[0].rglob("*") if path.is_file()}
    _validate(fx)
    assert before == {path: path.read_bytes() for path in fx[0].rglob("*") if path.is_file()}


def test_committed_recovery_authority_loads_only_from_a_deployed_manifest(tmp_path, monkeypatch):
    source = Path(__file__).resolve().parents[1] / recovery.AUTHORITY_PATH
    deployed = tmp_path / "deployed"
    asset = deployed / recovery.AUTHORITY_PATH
    asset.parent.mkdir(parents=True)
    asset.write_bytes(source.read_bytes())
    commit = "a" * 40
    (deployed / "DEPLOYED_COMMIT").write_text(commit + "\n")
    manifest = build_deployed_authority_manifest(
        repo_root=deployed, deployed_commit=commit, relative_paths=[recovery.AUTHORITY_PATH]
    )
    (deployed / DEPLOYED_AUTHORITY_MANIFEST).write_text(json.dumps(manifest))
    assert recovery.load_committed_recovery_authority(repo_root=deployed)["candidate"]["candidate_id"] == CANDIDATE
    asset.write_bytes(asset.read_bytes() + b"\n")
    with pytest.raises(recovery.ReviewedCoverCommittedRecoveryAuthorityError):
        recovery.load_committed_recovery_authority(repo_root=deployed)


@pytest.mark.parametrize("field", ["boundary_audit", "story_contract"])
def test_resealed_document_semantic_drift_is_rejected(tmp_path, monkeypatch, field):
    fx = _fixture(tmp_path, monkeypatch)
    runtime, repo, _plan, _journal_path, _journal, _record, authority, _generation_root = fx
    path = runtime / "out/2026-08-11" / CANDIDATE / "docs/0.json"
    document = json.loads(path.read_text())
    document[field] = {"frozen": "tampered"}
    _write_json(path, document)
    authority["active_documents"][0]["sha256"] = _sha(path.read_bytes())
    _reseal_authority(repo, authority)
    with pytest.raises(recovery.ReviewedCoverCommittedRecoveryAuthorityError):
        _validate(fx)


def test_resealed_target_that_no_longer_matches_its_intended_blob_is_rejected(tmp_path, monkeypatch):
    fx = _fixture(tmp_path, monkeypatch)
    runtime, repo, _plan, _journal_path, _journal, _record, authority, _generation_root = fx
    path = runtime / "out/2026-08-11" / CANDIDATE / "docs/0.json"
    document = json.loads(path.read_text())
    document["nonsemantic_audit_note"] = "resealed target mutation"
    _write_json(path, document)
    authority["active_documents"][0]["sha256"] = _sha(path.read_bytes())
    _reseal_authority(repo, authority)
    with pytest.raises(recovery.ReviewedCoverCommittedRecoveryAuthorityError):
        _validate(fx)


def test_resealed_record_media_subtitle_locator_drift_is_rejected(tmp_path, monkeypatch):
    fx = _fixture(tmp_path, monkeypatch)
    runtime, repo, _plan, _journal_path, _journal, _record, authority, generation_root = fx
    alternate_video = runtime / "out/2026-08-11" / CANDIDATE / "alternate.mp4"
    alternate_subtitle = runtime / "out/2026-08-11" / CANDIDATE / "alternate.srt"
    alternate_video.write_bytes(b"alternate video")
    alternate_subtitle.write_bytes(b"alternate subtitle")
    document_paths = [runtime / "out/2026-08-11" / CANDIDATE / "docs" / f"{index}.json" for index in range(2)]
    for index, path in enumerate(document_paths):
        document = json.loads(path.read_text())
        document["media_path"] = str(alternate_video)
        document["subtitle_path"] = str(alternate_subtitle)
        document["artifact_hashes"]["video_sha256"] = _sha(alternate_video.read_bytes())
        document["artifact_hashes"]["subtitle_sha256"] = _sha(alternate_subtitle.read_bytes())
        _write_json(path, document)
        authority["active_documents"][index]["sha256"] = _sha(path.read_bytes())
    transaction_path = generation_root / "cover-transaction.json"
    transaction = json.loads(transaction_path.read_text())
    for index, path in ((3, document_paths[0]), (4, document_paths[1])):
        blob = Path(transaction["entries"][index]["intended_blob"])
        blob.write_bytes(path.read_bytes())
        transaction["entries"][index]["intended_sha256"] = _sha(blob.read_bytes())
        authority["successor"]["transaction_entries"][index]["intended_sha256"] = _sha(blob.read_bytes())
    _write_json(transaction_path, transaction)
    authority["successor"]["transaction"]["sha256"] = _sha(transaction_path.read_bytes())
    _reseal_authority(repo, authority)
    with pytest.raises(recovery.ReviewedCoverCommittedRecoveryAuthorityError):
        _validate(fx)


def test_wrong_successor_code_fingerprint_is_rejected(tmp_path, monkeypatch):
    fx = _fixture(tmp_path, monkeypatch)
    runtime, repo, plan_path, journal_path, journal, record, _authority, _generation_root = fx
    with pytest.raises(recovery.ReviewedCoverCommittedRecoveryAuthorityError):
        recovery.validate_committed_recovery(
            repo_root=repo, runtime_root=runtime, plan_path=plan_path,
            plan_sha256=journal["plan_sha256"], journal_path=journal_path, journal=journal,
            records={CANDIDATE: record}, state_is_bound=True,
            code_fingerprint="sha256:" + "f" * 64,
        )


@pytest.mark.parametrize("kind", ["authority", "journal", "old_fingerprint", "transaction", "missing_successor", "binding", "generation", "document", "media", "subtitle", "state", "title", "extra_generation"])
def test_two_layer_committed_recovery_rejects_tampering(tmp_path, monkeypatch, kind):
    fx = _fixture(tmp_path, monkeypatch)
    runtime, repo, _plan, journal_path, journal, record, authority, generation_root = fx
    if kind == "authority":
        authority_path = repo / recovery.AUTHORITY_PATH
        data = json.loads(authority_path.read_text())
        data["authority_sha256"] = "sha256:" + "0" * 64
        _write_json(authority_path, data)
    elif kind == "journal":
        journal_path.write_bytes(b"{}")
    elif kind == "old_fingerprint":
        journal["code_fingerprint"] = "sha256:" + "d" * 64
    elif kind == "transaction":
        (generation_root / "cover-transaction.json").write_bytes(b"{}")
    elif kind == "missing_successor":
        (generation_root / "cover-transaction.json").unlink()
    elif kind == "binding":
        (generation_root / "final.cover.cover-binding.json").write_bytes(b"{}")
    elif kind == "generation":
        (generation_root / "final.cover.cover_generation.json").write_bytes(b"{}")
    elif kind == "document":
        (runtime / "out/2026-08-11" / CANDIDATE / "docs/0.json").write_bytes(b"{}")
    elif kind == "media":
        (runtime / "out/2026-08-11" / CANDIDATE / "delivery.mp4").write_bytes(b"changed")
    elif kind == "subtitle":
        (runtime / "out/2026-08-11" / CANDIDATE / "delivery.srt").write_bytes(b"changed")
    elif kind == "state":
        record["title"] = "wrong"
    elif kind == "title":
        data = json.loads((runtime / "out/2026-08-11" / CANDIDATE / "docs/2.json").read_text())
        data["title"] = "wrong"
        _write_json(runtime / "out/2026-08-11" / CANDIDATE / "docs/2.json", data)
    else:
        (generation_root.parent / "unexpected").mkdir()
    with pytest.raises(recovery.ReviewedCoverCommittedRecoveryAuthorityError):
        _validate(fx)


@pytest.mark.parametrize("status", ["PREPARED", "FINALIZED"])
def test_two_layer_committed_recovery_rejects_noncommitted_journal(tmp_path, monkeypatch, status):
    fx = _fixture(tmp_path, monkeypatch)
    fx[4]["status"] = status
    with pytest.raises(recovery.ReviewedCoverCommittedRecoveryAuthorityError):
        _validate(fx)
