import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import src.autoslice.fastlane_c2_private_authority as c2
import src.autoslice.reviewed_baseline_replay as replay
import src.autoslice.reviewed_baseline_replay_authority as authority_module
import scripts.run_fastlane_c2_private_replay as c2_runner_module
from scripts.run_fastlane_c2_private_replay import main as c2_runner
from src.autoslice.repository_asset_authority import build_deployed_authority_manifest


CID, DATE = c2.C2_CANDIDATE_ID, c2.C2_RECORDING_DATE


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _context_sha(value: object) -> str:
    return "sha256:" + hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _canonical_sha(value: object) -> str:
    return "sha256:" + hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, allow_nan=False, sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _seal_uniform_host_closure(
    *, runtime: Path, record_path: Path, record: dict[str, object], chat_path: Path,
) -> Path:
    repo = runtime / "repo"
    hashes = record["artifact_hashes"]
    assert isinstance(hashes, dict)
    chat = json.loads(chat_path.read_text())
    closure = {
        "schema_version": c2._C2_UNIFORM_HOST_CLOSURE_SCHEMA,
        "candidate_id": CID,
        "recording_date": DATE,
        "scope": "PRIVATE_NO_UPLOAD",
        "predecessor_record": {
            "sha256": _sha(record_path.read_bytes()),
            "speaker_mode": "uniform_host",
            "subtitle_style": "lidousha-final-sapphire72",
            "artifact_hashes": {
                name: hashes[name]
                for name in (
                    "chat_authority_audit_sha256", "publish_draft_sha256",
                    "subtitle_sha256", "speaker_review_srt_sha256", "ass_sha256",
                )
            },
        },
        "portable_chat_authority": {"sha256": _sha(chat_path.read_bytes()), **chat},
        "uniform_host_projection": {
            "record_ass_role": "DISPLAY_ASS",
            "record_ass_sha256": hashes["ass_sha256"],
            "speaker_ass_role": "NO_SEPARATE_SPEAKER_ASS",
            "speaker_srt_role": "TEXT_SRT_FALLBACK",
        },
    }
    closure["authority_sha256"] = _canonical_sha(closure)
    path = repo / c2._C2_UNIFORM_HOST_CLOSURE_RELATIVE
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(closure), encoding="utf-8")
    commit = "a" * 40
    (repo / "DEPLOYED_COMMIT").write_text(commit + "\n")
    manifest = build_deployed_authority_manifest(
        repo_root=repo, deployed_commit=commit,
        relative_paths=[c2._C2_UNIFORM_HOST_CLOSURE_RELATIVE],
    )
    (repo / "DEPLOYED_AUTHORITY_MANIFEST.json").write_text(json.dumps(manifest))
    return path


def _fixture(tmp_path: Path):
    runtime = tmp_path / "runtime"; candidate = runtime / "out" / DATE / CID
    package = candidate / "replacement_recuts"; package.mkdir(parents=True)
    canonical = tmp_path / "canonical" / "out" / DATE / CID / "replacement_recuts"
    canonical.mkdir(parents=True)
    source_sha, subtitle_sha, ass_sha = ("sha256:" + x * 64 for x in "123")
    draft = "1\n00:00:00,000 --> 00:00:01,000\ntext\n"
    clip = {"schema_version": "lidousha-clip-context.v1", "candidate_id": CID, "recording_date": DATE,
            "mutation_authorized": False, "whole_clip_draft_srt": draft,
            "whole_clip_draft_srt_sha256": _sha(draft.encode()), "pieces": [{"source_media_sha256": source_sha}],
            "retrieval_budget": {"whole_clip_transcript_truncated": False}}
    clip["context_sha256"] = _context_sha(clip)
    chat = {"schema_version": "chat-authority-audit.v2", "status": "APPLIED_AND_VERIFIED",
            "final_status": "FINAL_ARTIFACTS_VERIFIED", "final_output_srt_sha256": subtitle_sha,
            "final_text_srt_sha256": subtitle_sha, "final_speaker_srt_sha256": subtitle_sha,
            "speaker_ass_sha256": None,
            "final_review_audit": {"schema_version": "final-review-audit.v2", "status": "CLEAN", "reviewed_srt_sha256": subtitle_sha},
            "structured_chat_binding_audit": {"schema_version": "structured-chat-binding-audit.v1", "status": "PASS"}}
    portable = runtime / "repo" / "lidousha" / DATE; portable.mkdir(parents=True)
    stem = "sealed-c2"
    for name, payload in (("chat-authority", chat), ("clip-context", clip)):
        (portable / f"{stem}.{name}.json").write_text(json.dumps(payload))
    chat_path, clip_path = portable / f"{stem}.chat-authority.json", portable / f"{stem}.clip-context.json"
    publish = {"artifact_hashes": {"chat_authority_audit_sha256": _sha(chat_path.read_bytes()), "clip_context_file_sha256": _sha(clip_path.read_bytes())}}
    publish_path = portable / f"{stem}.publish.json"; publish_path.write_text(json.dumps(publish))
    record = {"chat_authority_audit_path": str(canonical / f"{CID}.chat-authority.json"),
              "clip_context_path": str(canonical / f"{CID}.clip-context.json"),
              "clip_context_payload_sha256": clip["context_sha256"],
              "artifact_hashes": {**publish["artifact_hashes"], "publish_draft_sha256": _sha(publish_path.read_bytes()),
                                  "subtitle_sha256": subtitle_sha, "speaker_review_srt_sha256": None, "ass_sha256": ass_sha},
              "speaker_mode": "uniform_host", "subtitle_style": "lidousha-final-sapphire72",
              "story_contract": {"candidate_id": CID, "clip_context_binding": {"context_sha256": clip["context_sha256"]}}}
    record_path = package / f"{CID}.record.json"; record_path.write_text(json.dumps(record))
    (portable / f"{stem}.record.json").write_bytes(record_path.read_bytes())
    _seal_uniform_host_closure(
        runtime=runtime, record_path=record_path, record=record, chat_path=chat_path,
    )
    plan = SimpleNamespace(candidate_id=CID, date=DATE, package_root=candidate)
    return runtime, plan, record_path, record, portable


def _resolve(runtime, plan, record_path, record, portable, monkeypatch, reads):
    monkeypatch.setattr(authority_module, "_portable_date_root", lambda **_kwargs: portable)
    def bound(path, *, label):
        reads.append(Path(path).absolute())
        return replay.regular_binding(path, label=label)
    return c2.resolve_c2_private_record_authority(
        plan=plan, record_binding=replay.regular_binding(record_path, label="RECORD"), record=record,
        runtime_authority_root=runtime, source_media_sha256="sha256:" + "1" * 64,
        regular_binding=bound, safe_directory=replay._safe_directory, load_json=replay._load_json,
        error=replay.ReviewedBaselineReplayError,
    )


def test_c2_private_adapter_reads_only_private_mirrors(monkeypatch, tmp_path):
    runtime, plan, record_path, record, portable = _fixture(tmp_path); reads = []
    resolved = _resolve(runtime, plan, record_path, record, portable, monkeypatch, reads)
    assert resolved.source == "portable-record-mirror"
    assert all(path.is_relative_to(runtime) for path in reads)
    assert Path(resolved.chat.path).is_relative_to(runtime)
    assert Path(resolved.clip_context.path).is_relative_to(runtime)


def test_c2_uniform_host_receipt_is_private_hash_bound_and_idempotent(monkeypatch, tmp_path):
    runtime, plan, record_path, record, portable = _fixture(tmp_path); reads = []
    _resolve(runtime, plan, record_path, record, portable, monkeypatch, reads)
    receipt_path = record_path.parent / (
        f"{CID}.c2-uniform-host-chat-closure-receipt.v1.json"
    )
    payload = receipt_path.read_bytes()
    receipt = json.loads(payload)
    body = dict(receipt); declared = body.pop("receipt_sha256")
    assert set(receipt) == {
        "schema_version", "scope", "candidate_id", "recording_date",
        "runtime_root_sha256", "closure_authority", "record", "chat",
        "uniform_host_projection", "receipt_sha256",
    }
    assert receipt["schema_version"] == c2._C2_UNIFORM_HOST_CLOSURE_RECEIPT_SCHEMA
    assert receipt["scope"] == "PRIVATE_NO_UPLOAD"
    assert receipt["candidate_id"] == CID
    assert receipt["recording_date"] == DATE
    assert receipt["runtime_root_sha256"] == _sha(str(runtime).encode("utf-8"))
    assert receipt["closure_authority"]["relative_path"] == (
        c2._C2_UNIFORM_HOST_CLOSURE_RELATIVE.as_posix()
    )
    assert receipt["record"]["sha256"] == _sha(record_path.read_bytes())
    assert receipt["record"]["relative_path"].startswith(f"out/{DATE}/{CID}/")
    assert receipt["chat"]["relative_path"].startswith("repo/")
    assert receipt["chat"]["final_speaker_srt_sha256"] == receipt["chat"]["final_text_srt_sha256"]
    assert receipt["chat"]["speaker_ass_sha256"] is None
    assert declared == _canonical_sha(body)
    assert str(runtime) not in payload.decode("utf-8")
    before = _sha(payload)
    _resolve(runtime, plan, record_path, record, portable, monkeypatch, reads)
    assert _sha(receipt_path.read_bytes()) == before


def test_committed_c2_uniform_host_closure_binds_the_confirmed_legacy_fields():
    path = Path(__file__).parents[1] / c2._C2_UNIFORM_HOST_CLOSURE_RELATIVE
    document = json.loads(path.read_text())
    validated = c2._validate_c2_uniform_host_closure_document(
        document, error=replay.ReviewedBaselineReplayError,
    )
    assert validated["candidate_id"] == CID
    assert validated["recording_date"] == DATE
    predecessor = validated["predecessor_record"]
    chat = validated["portable_chat_authority"]
    assert predecessor["sha256"] == (
        "sha256:7917578664f3e8fd1a3a3c1cad1ed9c69c96b57a9e0aa828b90bd501ee51d11e"
    )
    assert predecessor["artifact_hashes"]["speaker_review_srt_sha256"] is None
    assert chat["sha256"] == predecessor["artifact_hashes"]["chat_authority_audit_sha256"]
    assert chat["final_speaker_srt_sha256"] == chat["final_text_srt_sha256"]
    assert chat["speaker_ass_sha256"] is None


def test_generic_authority_keeps_rejecting_uniform_host_null_inner_bindings(monkeypatch, tmp_path):
    runtime, _plan, record_path, record, portable = _fixture(tmp_path)
    monkeypatch.setattr(authority_module, "_portable_date_root", lambda **_kwargs: portable)
    generic_plan = SimpleNamespace(
        candidate_id=CID,
        date=DATE,
        package_root=Path(record["chat_authority_audit_path"]).parent,
    )
    with pytest.raises(
        replay.ReviewedBaselineReplayError,
        match="REPLAY_FINALIZER_CHAT_AUTHORITY_RECORD_CLOSURE_DRIFT",
    ):
        authority_module.resolve_record_bound_finalizer_authority(
            plan=generic_plan,
            record_binding=replay.regular_binding(record_path, label="RECORD"),
            record=record,
            runtime_authority_root=runtime,
            source_media_sha256="sha256:" + "1" * 64,
            regular_binding=replay.regular_binding,
            safe_directory=replay._safe_directory,
            load_json=replay._load_json,
            error=replay.ReviewedBaselineReplayError,
        )


@pytest.mark.parametrize("kind", ["candidate", "date", "self_hash", "speaker_fallback"])
def test_c2_uniform_host_closure_rejects_unsealed_or_invalid_authority(
    monkeypatch, tmp_path, kind,
):
    runtime, plan, record_path, record, portable = _fixture(tmp_path); reads = []
    path = runtime / "repo" / c2._C2_UNIFORM_HOST_CLOSURE_RELATIVE
    document = json.loads(path.read_text())
    if kind == "candidate":
        document["candidate_id"] = "other"
    elif kind == "date":
        document["recording_date"] = "2026-08-14"
    elif kind == "self_hash":
        document["authority_sha256"] = "sha256:" + "f" * 64
    else:
        document["portable_chat_authority"]["final_speaker_srt_sha256"] = "sha256:" + "f" * 64
        document["authority_sha256"] = _canonical_sha({
            key: value for key, value in document.items() if key != "authority_sha256"
        })
    path.write_text(json.dumps(document))
    with pytest.raises(
        replay.ReviewedBaselineReplayError,
        match="C2_PRIVATE_AUTHORITY_UNIFORM_HOST_CLOSURE_INVALID",
    ):
        _resolve(runtime, plan, record_path, record, portable, monkeypatch, reads)


def test_c2_uniform_host_closure_rejects_record_hash_or_schema_drift(monkeypatch, tmp_path):
    runtime, plan, record_path, record, portable = _fixture(tmp_path); reads = []
    record["artifact_hashes"]["ass_sha256"] = "sha256:" + "f" * 64
    record_path.write_text(json.dumps(record))
    with pytest.raises(
        replay.ReviewedBaselineReplayError,
        match="C2_PRIVATE_AUTHORITY_UNIFORM_HOST_CLOSURE_RECORD_DRIFT",
    ):
        _resolve(runtime, plan, record_path, record, portable, monkeypatch, reads)


def test_c2_uniform_host_closure_requires_the_private_deploy_manifest(monkeypatch, tmp_path):
    runtime, plan, record_path, record, portable = _fixture(tmp_path); reads = []
    path = runtime / "repo" / c2._C2_UNIFORM_HOST_CLOSURE_RELATIVE
    # Whitespace preserves the sealed semantic self-hash, but must still fail
    # the private deployed-manifest byte binding.
    path.write_text(path.read_text() + "\n")
    with pytest.raises(
        replay.ReviewedBaselineReplayError,
        match="C2_PRIVATE_AUTHORITY_UNIFORM_HOST_CLOSURE_UNSEALED",
    ):
        _resolve(runtime, plan, record_path, record, portable, monkeypatch, reads)


def test_c2_uniform_host_closure_rejects_tampered_or_symlinked_receipt(monkeypatch, tmp_path):
    runtime, plan, record_path, record, portable = _fixture(tmp_path); reads = []
    receipt = record_path.parent / f"{CID}.c2-uniform-host-chat-closure-receipt.v1.json"
    receipt.write_text("{}")
    with pytest.raises(
        replay.ReviewedBaselineReplayError,
        match="C2_PRIVATE_AUTHORITY_UNIFORM_HOST_CLOSURE_RECEIPT_DRIFT",
    ):
        _resolve(runtime, plan, record_path, record, portable, monkeypatch, reads)

    runtime, plan, record_path, record, portable = _fixture(tmp_path / "symlink"); reads = []
    receipt = record_path.parent / f"{CID}.c2-uniform-host-chat-closure-receipt.v1.json"
    receipt.symlink_to(tmp_path / "outside-receipt.json")
    with pytest.raises(
        replay.ReviewedBaselineReplayError,
        match="C2_PRIVATE_AUTHORITY_UNIFORM_HOST_CLOSURE_RECEIPT_UNSAFE",
    ):
        _resolve(runtime, plan, record_path, record, portable, monkeypatch, reads)


@pytest.mark.parametrize("field", ["candidate", "chat_hash", "clip_hash", "canonical_locator"])
def test_c2_private_adapter_rejects_identity_drift(monkeypatch, tmp_path, field):
    runtime, plan, record_path, record, portable = _fixture(tmp_path); reads = []
    if field == "candidate": record["story_contract"]["candidate_id"] = "wrong"
    elif field == "chat_hash": record["artifact_hashes"]["chat_authority_audit_sha256"] = "sha256:" + "f" * 64
    elif field == "clip_hash": record["artifact_hashes"]["clip_context_file_sha256"] = "sha256:" + "f" * 64
    else: record["chat_authority_audit_path"] = str(Path(record["chat_authority_audit_path"]).with_name("wrong.json"))
    with pytest.raises(replay.ReviewedBaselineReplayError):
        _resolve(runtime, plan, record_path, record, portable, monkeypatch, reads)


def test_c2_private_adapter_rejects_outside_read(monkeypatch, tmp_path):
    runtime, plan, record_path, record, portable = _fixture(tmp_path); reads = []
    # A portable root outside the private runtime must never reach the binding function.
    outside = tmp_path / "outside"; outside.mkdir()
    for source in portable.iterdir():
        if source.is_file():
            (outside / source.name).write_bytes(source.read_bytes())
    monkeypatch.setattr(authority_module, "_portable_date_root", lambda **_kwargs: outside)
    with pytest.raises(replay.ReviewedBaselineReplayError, match="PORTABLE_AUTHORITY_UNSAFE"):
        _resolve(runtime, plan, record_path, record, outside, monkeypatch, reads)
    assert reads
    assert all(path.is_relative_to(runtime) for path in reads)
    assert all(not path.is_relative_to(outside) for path in reads)


def test_c2_runner_cannot_open_generic_apply_or_other_candidate():
    with pytest.raises(SystemExit, match="C2_PRIVATE_REPLAY_APPLY_FORBIDDEN"):
        c2_runner(["--apply", "--date", DATE, "--candidate-id", CID, "--runtime-root", "/private/x", "--private-stage-parent", "/private/y"])
    with pytest.raises(SystemExit, match="C2_PRIVATE_REPLAY_CANDIDATE_INVALID"):
        c2_runner(["--plan", "--date", DATE, "--candidate-id", "other", "--runtime-root", "/private/x"])
    with pytest.raises(SystemExit, match="C2_PRIVATE_REPLAY_CANDIDATE_INVALID"):
        c2_runner(["--plan", "--date", DATE, "--candidate-id"])
    with pytest.raises(SystemExit, match="C2_PRIVATE_REPLAY_DATE_INVALID"):
        c2_runner(["--plan", "--date", "--candidate-id", CID, "--runtime-root", "/private/x"])


def test_c2_runner_injects_only_the_closed_path_diagnostic(monkeypatch):
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        c2_runner_module.replay,
        "main",
        lambda args, **kwargs: captured.update({"args": args, "kwargs": kwargs}) or 0,
    )
    assert c2_runner([
        "--plan", "--date", DATE, "--candidate-id", CID,
        "--runtime-root", "/private/only-a-test",
    ]) == 0
    assert captured["args"] == [
        "--plan", "--date", DATE, "--candidate-id", CID,
        "--runtime-root", "/private/only-a-test",
    ]
    assert captured["kwargs"] == {
        "_record_authority_resolver": c2.resolve_c2_private_record_authority,
        "_path_unavailable_diagnostic": c2.classify_c2_private_path_unavailable,
    }


def test_c2_path_unavailable_classifier_reports_only_allowlisted_regular_role(tmp_path):
    missing = tmp_path / "token=secret" / "provider-response" / "record.json"
    with pytest.raises(replay.ReviewedBaselineReplayError) as caught:
        replay.regular_binding(missing, label="PROVENANCE")
    reason = c2.classify_c2_private_path_unavailable(caught.value)
    assert reason == "C2_PRIVATE_REPLAY_PATH_UNAVAILABLE_REGULAR_PROVENANCE_PARENT"
    rendered = json.dumps({"reason_code": reason})
    assert str(tmp_path) not in rendered
    assert "token=secret" not in rendered
    assert "provider-response" not in rendered


def test_c2_path_unavailable_classifier_rejects_unknown_binding_label(tmp_path):
    missing = tmp_path / "token=secret" / "provider-response" / "record.json"
    with pytest.raises(replay.ReviewedBaselineReplayError) as caught:
        replay.regular_binding(missing, label="UNTRUSTED_PROVIDER_SECRET")
    reason = c2.classify_c2_private_path_unavailable(caught.value)
    assert reason == "C2_PRIVATE_REPLAY_PATH_UNAVAILABLE_REGULAR_BINDING_UNCLASSIFIED"
    rendered = json.dumps({"reason_code": reason})
    assert "UNTRUSTED_PROVIDER_SECRET" not in rendered
    assert str(tmp_path) not in rendered


def test_c2_path_unavailable_classifier_maps_deployed_runtime_locus(tmp_path):
    with pytest.raises(replay.ReviewedBaselineReplayError) as caught:
        replay._copy_deployed_authority(
            source_runtime_root=tmp_path / "token=secret" / "missing-runtime",
            private_runtime_root=tmp_path / "private-runtime",
        )
    reason = c2.classify_c2_private_path_unavailable(caught.value)
    assert reason == "C2_PRIVATE_REPLAY_PATH_UNAVAILABLE_DEPLOYED_AUTHORITY_RUNTIME"
    assert str(tmp_path) not in json.dumps({"reason_code": reason})


def _provenance_fixture(tmp_path: Path):
    runtime = tmp_path / "runtime"; candidate = runtime / "out" / DATE / CID
    recuts = candidate / "replacement_recuts"; recuts.mkdir(parents=True)
    padded = candidate / "padded_318740_437660.mp4"; piece = candidate / "piece_0_318740_437660.mp4"
    final = recuts / f"{CID}.recut.mp4"
    padded.write_bytes(b"source"); piece.write_bytes(b"source"); final.write_bytes(b"final")
    old = tmp_path / "previous" / "out" / DATE / CID
    old_recuts = old / "replacement_recuts"
    document = {
        "final_recut": {"output_path": str(old_recuts / final.name), "output_sha256": hashlib.sha256(b"final").hexdigest(),
                        "source_path": str(old / padded.name), "source_sha256": hashlib.sha256(b"source").hexdigest()},
        "padded": {"output_path": str(old / padded.name), "output_sha256": hashlib.sha256(b"source").hexdigest(),
                   "inputs": [{"path": str(old / piece.name), "sha256": hashlib.sha256(b"source").hexdigest()}]},
        "source_piece": {"output_path": str(old / piece.name), "output_sha256": hashlib.sha256(b"source").hexdigest(),
                         "source_path": "/recordings/exact-source.mp4", "source_sha256": "a" * 64},
    }
    provenance = recuts / f"{CID}.recut.provenance.json"
    provenance.write_text(json.dumps(document), encoding="utf-8")
    return runtime, recuts, provenance, document


def test_c2_private_provenance_projects_exact_five_current_locators(tmp_path):
    runtime, recuts, provenance, _ = _provenance_fixture(tmp_path)
    target = c2.materialize_c2_private_provenance(runtime_root=runtime, candidate_id=CID, date=DATE)
    assert target == provenance
    document = json.loads(target.read_text())
    candidate = runtime / "out" / DATE / CID
    assert document["final_recut"]["output_path"] == str(recuts / f"{CID}.recut.mp4")
    assert document["final_recut"]["source_path"] == str(candidate / "padded_318740_437660.mp4")
    assert document["padded"]["inputs"][0]["path"] == str(candidate / "piece_0_318740_437660.mp4")
    assert document["padded"]["output_path"] == str(candidate / "padded_318740_437660.mp4")
    assert document["source_piece"]["output_path"] == str(candidate / "piece_0_318740_437660.mp4")
    receipt = json.loads((recuts / f"{CID}.recut.provenance.c2-private-projection-receipt.json").read_text())
    assert receipt["allowed_pointers"] == list(c2._C2_PROVENANCE_POINTERS)
    assert len(receipt["rows"]) == 5
    assert all(row["new"]["path"].startswith(str(candidate)) for row in receipt["rows"])
    assert c2.materialize_c2_private_provenance(runtime_root=runtime, candidate_id=CID, date=DATE) == target


@pytest.mark.parametrize("kind", ["stale_previous_root", "extra_pointer", "hash_mismatch"])
def test_c2_private_provenance_rejects_invalid_preimage(tmp_path, kind):
    runtime, _recuts, provenance, document = _provenance_fixture(tmp_path)
    if kind == "stale_previous_root":
        document["padded"]["output_path"] = str(tmp_path / "other" / "padded_318740_437660.mp4")
        expected = "C2_PRIVATE_PROVENANCE_STALE_PREVIOUS_ROOT"
    elif kind == "extra_pointer":
        document["final_recut"]["unexpected_path"] = "/unexpected"
        expected = "C2_PRIVATE_PROVENANCE_EXTRA_POINTER"
    else:
        document["final_recut"]["source_sha256"] = "f" * 64
        expected = "C2_PRIVATE_PROVENANCE_TARGET_HASH_MISMATCH"
    provenance.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match=expected):
        c2.materialize_c2_private_provenance(runtime_root=runtime, candidate_id=CID, date=DATE)


def test_c2_private_provenance_rejects_symlink_escape_and_wrong_scope(tmp_path):
    runtime, recuts, _provenance, _document = _provenance_fixture(tmp_path)
    final = recuts / f"{CID}.recut.mp4"; final.unlink(); final.symlink_to(tmp_path / "outside.mp4")
    with pytest.raises(ValueError, match="C2_PRIVATE_PROVENANCE_TARGET_UNSAFE"):
        c2.materialize_c2_private_provenance(runtime_root=runtime, candidate_id=CID, date=DATE)
    with pytest.raises(ValueError, match="C2_PRIVATE_PROVENANCE_CANDIDATE_SCOPE_INVALID"):
        c2.materialize_c2_private_provenance(runtime_root=runtime, candidate_id="other", date=DATE)
    with pytest.raises(ValueError, match="C2_PRIVATE_PROVENANCE_CANDIDATE_SCOPE_INVALID"):
        c2.materialize_c2_private_provenance(runtime_root=runtime, candidate_id=CID, date="2026-08-14")


def test_c2_private_provenance_rejects_candidate_directory_symlink(tmp_path):
    runtime, _recuts, _provenance, _document = _provenance_fixture(tmp_path)
    candidate = runtime / "out" / DATE / CID
    moved = tmp_path / "outside-candidate"; candidate.rename(moved); candidate.symlink_to(moved, target_is_directory=True)
    with pytest.raises(ValueError, match="C2_PRIVATE_PROVENANCE_CANDIDATE_UNSAFE"):
        c2.materialize_c2_private_provenance(runtime_root=runtime, candidate_id=CID, date=DATE)


def test_c2_private_provenance_rejects_stale_completed_projection_root(tmp_path):
    runtime, recuts, _provenance, _document = _provenance_fixture(tmp_path)
    c2.materialize_c2_private_provenance(runtime_root=runtime, candidate_id=CID, date=DATE)
    receipt_path = recuts / f"{CID}.recut.provenance.c2-private-projection-receipt.json"
    receipt = json.loads(receipt_path.read_text()); receipt["runtime_root"] = str(tmp_path / "previous-runtime")
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    with pytest.raises(ValueError, match="C2_PRIVATE_PROVENANCE_STALE_PREVIOUS_ROOT"):
        c2.materialize_c2_private_provenance(runtime_root=runtime, candidate_id=CID, date=DATE)
