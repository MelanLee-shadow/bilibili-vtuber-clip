import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import src.autoslice.fastlane_c2_private_authority as c2
import src.autoslice.reviewed_baseline_replay as replay
import src.autoslice.reviewed_baseline_replay_authority as authority_module
from scripts.run_fastlane_c2_private_replay import main as c2_runner


CID, DATE = c2.C2_CANDIDATE_ID, c2.C2_RECORDING_DATE


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _context_sha(value: object) -> str:
    return "sha256:" + hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _fixture(tmp_path: Path):
    runtime = tmp_path / "runtime"; package = runtime / "out" / DATE / CID
    package.mkdir(parents=True)
    canonical = tmp_path / "canonical" / "out" / DATE / CID
    canonical.mkdir(parents=True)
    source_sha, subtitle_sha, speaker_sha, ass_sha = ("sha256:" + x * 64 for x in "1234")
    draft = "1\n00:00:00,000 --> 00:00:01,000\ntext\n"
    clip = {"schema_version": "lidousha-clip-context.v1", "candidate_id": CID, "recording_date": DATE,
            "mutation_authorized": False, "whole_clip_draft_srt": draft,
            "whole_clip_draft_srt_sha256": _sha(draft.encode()), "pieces": [{"source_media_sha256": source_sha}],
            "retrieval_budget": {"whole_clip_transcript_truncated": False}}
    clip["context_sha256"] = _context_sha(clip)
    chat = {"schema_version": "chat-authority-audit.v2", "status": "APPLIED_AND_VERIFIED",
            "final_status": "FINAL_ARTIFACTS_VERIFIED", "final_output_srt_sha256": subtitle_sha,
            "final_text_srt_sha256": subtitle_sha, "final_speaker_srt_sha256": speaker_sha,
            "speaker_ass_sha256": ass_sha,
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
                                  "subtitle_sha256": subtitle_sha, "speaker_review_srt_sha256": speaker_sha, "ass_sha256": ass_sha},
              "story_contract": {"candidate_id": CID, "clip_context_binding": {"context_sha256": clip["context_sha256"]}}}
    record_path = package / f"{CID}.record.json"; record_path.write_text(json.dumps(record))
    (portable / f"{stem}.record.json").write_bytes(record_path.read_bytes())
    plan = SimpleNamespace(candidate_id=CID, date=DATE, package_root=package)
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
    assert not reads


def test_c2_runner_cannot_open_generic_apply_or_other_candidate():
    with pytest.raises(SystemExit, match="C2_PRIVATE_REPLAY_APPLY_FORBIDDEN"):
        c2_runner(["--apply", "--date", DATE, "--candidate-id", CID, "--runtime-root", "/private/x", "--private-stage-parent", "/private/y"])
    with pytest.raises(SystemExit, match="C2_PRIVATE_REPLAY_CANDIDATE_INVALID"):
        c2_runner(["--plan", "--date", DATE, "--candidate-id", "other", "--runtime-root", "/private/x"])
