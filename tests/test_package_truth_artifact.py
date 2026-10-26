"""Frozen truth identity and scope checks at the actual package consumer."""

import copy
import hashlib
import json
from pathlib import Path

import pytest

from src.autoslice import package_truth_artifact as artifact
from src.autoslice.clip_context import _canonical_json_sha256


def write(path, document):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, ensure_ascii=False))
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def fixture(tmp_path, monkeypatch, entries=()):
    canonical = tmp_path / "repo/assets/profile/subtitle_truth_ledger.v1.json"
    doc = {
        "schema_version": "source-subtitle-truth-ledger.v1",
        "entries": list(entries),
        "source_aliases": [],
    }
    digest = write(canonical, doc)
    historical = tmp_path / "prior/repo/assets/profile/subtitle_truth_ledger.v1.json"
    write(historical, doc)
    monkeypatch.setattr(artifact, "profile_ledgers", lambda *_: [canonical, historical])
    candidate = "candidate-a"
    producer = tmp_path / "private/output" / candidate
    frozen = producer / "source-truth-input" / ("ledger." + digest[7:] + ".json")
    write(frozen, doc)
    context = {
        "schema_version": "lidousha-clip-context.v1",
        "candidate_id": candidate,
        "mutation_authorized": False,
        "whole_clip_draft_srt": "",
        "whole_clip_draft_srt_sha256": "sha256:" + hashlib.sha256(b"").hexdigest(),
        "retrieval_budget": {"whole_clip_transcript_truncated": False},
        "pieces": [
            {
                "recording_basename": "source.mp4",
                "source_media_sha256": "sha256:" + "a" * 64,
                "start_ms": 1000,
                "end_ms": 11000,
            }
        ],
    }
    context["context_sha256"] = _canonical_json_sha256(context)
    record_path = tmp_path / "package/item.record.json"
    write(record_path.with_name("item.clip-context.json"), context)
    record = {
        "candidate_id": candidate,
        "chat_authority_audit_path": str(producer / "candidate-a.chat-authority.json"),
        "clip_context_payload_sha256": context["context_sha256"],
        "start_ms": 0,
        "end_ms": 6000,
    }
    provenance = {
        "source_piece": {
            "source_path": "/source/source.mp4",
            "source_sha256": "a" * 64,
            "start_ms": 1000,
            "end_ms": 11000,
        },
        "final_recut": {
            "start_ms": 2000,
            "end_ms": 8000,
            "absolute_source_start_ms": 3000,
            "absolute_source_end_ms": 9000,
        },
    }
    truth = {
        "ledger_path": str(frozen),
        "ledger_sha256": digest,
        "status": "NO_RELEVANT_INTERVAL",
        "applied": [],
        "satisfied": [],
    }
    authority = {
        "source_truth_attempt": {
            "schema_version": "producer-source-truth-attempt.v1",
            "ledger": {"path": str(frozen), "sha256": digest},
        }
    }
    args = dict(
        canonical=canonical,
        repo_root=tmp_path / "repo",
        provenance=provenance,
        authority=authority,
        record=record,
        record_path=record_path,
    )
    return truth, args, doc, frozen


def entry(identity, start=4000, end=5000, **extra):
    return dict(
        truth_id=identity,
        knowledge_type="SOURCE_INTERVAL_TRUTH",
        recording_basename="source.mp4",
        source_media_sha256="sha256:" + "a" * 64,
        source_start_ms=start,
        source_end_ms=end,
        **extra,
    )


def test_frozen_regular_identity_and_portable_resolution(tmp_path, monkeypatch):
    truth, args, _, frozen = fixture(tmp_path, monkeypatch)
    before = frozen.read_bytes()
    assert artifact.source_truth_artifact_valid(truth, **args)
    assert artifact.source_truth_artifact_valid(truth, **args)
    assert frozen.read_bytes() == before
    frozen.unlink()  # Portable package still resolves exact committed profile bytes.
    assert artifact.source_truth_artifact_valid(truth, **args)


@pytest.mark.parametrize(
    "mutation",
    ["source", "window", "duration", "context", "tamper", "locator", "symlink", "history"],
)
def test_frozen_identity_rejects_unbound_inputs(tmp_path, monkeypatch, mutation):
    truth, args, _, frozen = fixture(tmp_path, monkeypatch)
    if mutation == "source":
        args["provenance"]["source_piece"]["source_sha256"] = "b" * 64
    elif mutation == "window":
        args["provenance"]["final_recut"]["absolute_source_start_ms"] += 1
    elif mutation == "duration":
        args["record"]["end_ms"] += 1
    elif mutation == "context":
        args["record"]["clip_context_payload_sha256"] = "sha256:" + "b" * 64
    elif mutation == "tamper":
        frozen.write_bytes(b"{}")
    elif mutation == "locator":
        truth["ledger_path"] = str(tmp_path / frozen.name)
        args["authority"]["source_truth_attempt"]["ledger"]["path"] = truth["ledger_path"]
    elif mutation == "symlink":
        frozen.unlink()
        frozen.symlink_to(args["canonical"])
    else:
        truth["ledger_sha256"] = "sha256:" + "b" * 64
        args["authority"]["source_truth_attempt"]["ledger"]["sha256"] = truth["ledger_sha256"]
    assert not artifact.source_truth_artifact_valid(truth, **args)


def test_progression_only_ignores_unrelated_truth(tmp_path, monkeypatch):
    truth, args, doc, _ = fixture(tmp_path, monkeypatch)
    changed = copy.deepcopy(doc)
    changed["entries"] = [entry("unrelated", 12000, 13000)]
    write(args["canonical"], changed)
    assert artifact.source_truth_artifact_valid(truth, **args)
    changed["entries"].append(entry("new-related"))
    write(args["canonical"], changed)
    assert not artifact.source_truth_artifact_valid(truth, **args)


def test_same_truth_id_revision_is_not_equivalent(tmp_path, monkeypatch):
    truth, args, doc, _ = fixture(tmp_path, monkeypatch, [entry("known", revision_id="old")])
    truth.update(status="APPLIED", applied=[{"truth_id": "known"}])
    assert artifact.source_truth_artifact_valid(truth, **args)
    doc["entries"][0]["revision_id"] = "new"
    write(args["canonical"], doc)
    assert not artifact.source_truth_artifact_valid(truth, **args)


def test_retained_asset_requires_its_deployed_manifest_bytes(tmp_path):
    relative = Path("assets/profile/subtitle_truth_ledger.v1.json")
    repo = tmp_path / "retained/repo"
    raw_sha = write(
        repo / relative, {"schema_version": "source-subtitle-truth-ledger.v1", "entries": []}
    )
    commit = "a" * 40
    (repo / "DEPLOYED_COMMIT").write_text(commit)
    manifest = {
        "schema_version": "deployed-authority-manifest.v1",
        "deployed_commit": commit,
        "entries": {
            relative.as_posix(): {"sha256": raw_sha, "bytes": (repo / relative).stat().st_size}
        },
    }
    manifest["manifest_sha256"] = _canonical_json_sha256(manifest)
    write(repo / "DEPLOYED_AUTHORITY_MANIFEST.json", manifest)
    assert artifact._registered_asset(repo, relative)
    (repo / relative).write_bytes(b"forged history")
    assert not artifact._registered_asset(repo, relative)
