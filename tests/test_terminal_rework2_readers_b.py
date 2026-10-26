"""Compressed review-record round trips for the remaining source readers."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


def _sha(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _compact_chat(tmp_path: Path, *, name: str = "candidate.chat-authority.json"):
    from src.autoslice import argon_escalation_storage as storage
    from src.autoslice.review_evidence_store import REF_SCHEMA, compact_review, encoded

    subtitle = "sha256:" + "1" * 64
    review = {
        "schema_version": "final-review-audit.v2",
        "status": "CLEAN",
        "reviewed_srt_sha256": subtitle,
        # ``findings`` is retained inline by the compact writer.  Keeping a
        # large scalar list therefore forces the containing final-review
        # document itself to become a reference rather than merely compacting
        # one child field.
        "findings": list(range(5_000)),
    }
    packed_review = compact_review(review, tmp_path)
    review_raw = encoded(packed_review)
    review_sha = storage.blob(tmp_path / "evidence-store", review_raw)
    chat = {
        "schema_version": "chat-authority-audit.v2",
        "status": "NO_MATCH",
        "final_status": "FINAL_ARTIFACTS_VERIFIED",
        "final_output_srt_sha256": subtitle,
        "final_text_srt_sha256": subtitle,
        "final_speaker_srt_sha256": subtitle,
        "speaker_ass_sha256": "sha256:" + "2" * 64,
        "final_review_audit": {
            "schema_version": REF_SCHEMA,
            "sha256": review_sha,
            "path": str(tmp_path / "evidence-store" / "blobs" / f"{review_sha}.bin"),
        },
        "structured_chat_binding_audit": {
            "schema_version": "structured-chat-binding-audit.v1",
            "status": "PASS",
        },
    }
    path = tmp_path / name
    path.write_text(json.dumps(chat, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    raw = path.read_bytes()
    compact = json.loads(raw)
    assert compact["final_review_audit"]["schema_version"] == "review-evidence-reference.v1"
    expanded_chat = {**chat, "final_review_audit": review}
    return path, raw, expanded_chat, review


def test_chat_readers_expand_compact_review_after_raw_wire_binding(tmp_path: Path):
    from src.autoslice import (
        qixi_operator_exact_title_source_fact as qixi,
        retained_partial_evidence as retained,
        semantic_request_history as history,
        source_checkpoint_parent as checkpoint,
    )
    from src.autoslice import reviewed_baseline_replay as replay

    path, raw, chat, review = _compact_chat(tmp_path)
    expected = _sha(raw)

    qixi_value = qixi._json_payload(
        raw, label="CHAT", directory=path.parent, review=True
    )
    assert qixi_value["final_review_audit"] == review

    bound_value = checkpoint.bound(checkpoint.binding(path))
    assert bound_value["final_review_audit"] == review
    generic_authority = tmp_path / "authority.json"
    generic_authority.write_bytes(raw)
    assert checkpoint.bound(checkpoint.binding(generic_authority))["final_review_audit"] == review

    history_path, history_raw, history_value = history.read_review_bound(
        {"path": str(path), "sha256": expected}
    )
    assert history_path == path and history_raw == raw
    assert history_value["final_review_audit"] == review
    retained_path, retained_raw, retained_value = retained.read_review_bound(
        {"path": str(path), "sha256": expected}
    )
    assert retained_path == path and retained_raw == raw
    assert retained_value["final_review_audit"] == review


    # This is the same expanded value used by the reviewed replay path; the
    # source reader still retains the unchanged sealed wire bytes separately.
    binding = replay.regular_binding(path, label="CHAT")
    loaded = replay._load_json(binding, label="CHAT", review=True)
    assert loaded == chat


def test_foreign_failure_reader_expands_compact_audit(tmp_path: Path):
    from src.autoslice.foreign_source_failure_evidence import foreign_source_gate_violation
    from src.autoslice.review_evidence_store import persist_review

    path = tmp_path / "foreign.chat-authority.json"
    audit = {
        "status": "BLOCKED_FOREIGN_SOURCE",
        "unproven_foreign_introductions": [
            {"attempted": "bonjour", "cue_index": 4, "start_ms": 100, "end_ms": 200}
        ],
        "audio_witness_rows": [],
        "cpa_adjudication_rows": [],
        "padding": "audit padding " * 2_000,
    }
    persist_review(path, {"final_source_language_preservation_audit": audit})
    result = foreign_source_gate_violation(
        f"FOREIGN_SOURCE_TRANSCRIPTION_REQUIRED: {path}"
    )
    assert result is not None
    assert result["token"] == "bonjour"
    assert result["missing_witnesses"] == ["positive_source_audio_transcription"]
    assert result["artifact_hashes"]["chat_authority_sha256"] == _sha(path.read_bytes())


def test_replay_authority_and_c2_validator_consume_expanded_chat(tmp_path: Path):
    from src.autoslice import reviewed_baseline_replay as replay
    from src.autoslice import reviewed_baseline_replay_authority as authority

    path, raw, chat, _review = _compact_chat(tmp_path)
    binding = replay.regular_binding(path, label="CHAT")
    subtitle = chat["final_text_srt_sha256"]
    record = {
        "artifact_hashes": {
            "subtitle_sha256": subtitle,
            "speaker_review_srt_sha256": subtitle,
            "ass_sha256": "sha256:" + "2" * 64,
        }
    }
    authority._validate_chat(
        binding,
        record=record,
        load_json=replay._load_json,
        error=ValueError,
    )

    assert _sha(raw) == binding.sha256


def test_replay_after_image_reads_projected_chat_as_review(tmp_path: Path, monkeypatch):
    from src.autoslice import package_relocation_contract as relocation
    from src.autoslice import producer_delivery_transaction as delivery
    from src.autoslice import reviewed_baseline_replay as replay
    from src.autoslice import reviewed_baseline_replay_after_image as after_image
    from src.autoslice import reviewed_baseline_replay_transaction as transaction

    path, _raw, chat, review = _compact_chat(tmp_path)
    package_root = tmp_path / "package"
    package_root.mkdir()
    names = {
        "record": "candidate.record.json",
        "publish": "candidate.publish.json",
        "chat_authority": path.name,
        "speaker_manifest": "candidate.speaker.json",
        "clip_context": "candidate.clip.json",
    }
    for name in names.values():
        (package_root / name).write_text("{}\n", encoding="utf-8")
    audit_path = tmp_path / "package-audit.json"
    audit_path.write_text("{}\n", encoding="utf-8")
    record_before = tmp_path / "record-before.json"
    record_before.write_text("{}\n", encoding="utf-8")
    projected_record = tmp_path / "projected-record.json"
    projected_record.write_text("{}\n", encoding="utf-8")
    projected_publish = tmp_path / "projected-publish.json"
    projected_publish.write_text("{}\n", encoding="utf-8")
    projected_speaker = tmp_path / "projected-speaker.json"
    projected_speaker.write_text("{}\n", encoding="utf-8")
    projected_clip = tmp_path / "projected-clip.json"
    projected_clip.write_text("{}\n", encoding="utf-8")
    projection = SimpleNamespace(
        record=replay.regular_binding(projected_record, label="RECORD"),
        publish=replay.regular_binding(projected_publish, label="PUBLISH"),
        chat=replay.regular_binding(path, label="CHAT"),
        speaker_manifest=replay.regular_binding(projected_speaker, label="SPEAKER"),
    )
    plan = SimpleNamespace(
        date="2026-01-01", candidate_id="candidate", package_root=tmp_path / "live",
        record_path=record_before,
    )
    package = SimpleNamespace(
        root=package_root, package_audit=SimpleNamespace(path=audit_path),
    )
    finalization = SimpleNamespace(private_runtime_root=tmp_path / "private")
    (tmp_path / "private").mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    state = SimpleNamespace(
        before=b"before", after=b"after",
        delivered={
            role: {"source": str(package_root / name), "target": str(runtime / (role + ".json"))}
            for role, name in {
                "record": names["record"],
                "publish": names["publish"],
                "chat_authority": names["chat_authority"],
                "speaker_manifest": names["speaker_manifest"],
                "clip_context": names["clip_context"],
            }.items()
        },
    )
    seen: list[tuple[str, bool]] = []
    original_load_json = replay._load_json

    def load_json(binding, *, label, review=False):
        seen.append((label, review))
        if label == "PROJECTED_CHAT":
            return original_load_json(binding, label=label, review=review)
        if label == "PROJECTED_RECORD":
            return {"chat_authority_audit_path": str(tmp_path / "candidate.chat-authority.json"),
                    "clip_context_path": str(tmp_path / "candidate.clip-context.json")}
        return {}

    monkeypatch.setattr(replay, "_load_json", load_json)
    monkeypatch.setattr(replay, "_safe_directory", lambda value: Path(value).absolute())
    monkeypatch.setattr(replay, "_prepared_package_names", lambda *args, **kwargs: names)
    monkeypatch.setattr(replay, "_replay_package_root", lambda _plan: tmp_path / "live")
    monkeypatch.setattr(replay, "project_replay_state_after", lambda *args, **kwargs: state)
    monkeypatch.setattr(delivery, "deployment_authority_binding", lambda _root: {"commit": "fixed"})
    monkeypatch.setattr(after_image, "exact_candidate_sidecar_target", lambda *args, **kwargs: None)
    monkeypatch.setattr(relocation, "pointer_applies", lambda *args, **kwargs: False)
    monkeypatch.setattr(transaction, "stage_lane_after_image_artifacts", lambda **kwargs: ())

    result = after_image.build_replay_after_image(
        plan, runtime_root=runtime, state_path=tmp_path / "state.json",
        finalization=finalization, package=package, projection=projection,
    )
    assert result.state_after == b"after"
    assert ("PROJECTED_CHAT", True) in seen
    assert result.artifacts == ()
    assert chat["final_review_audit"] == review


def test_completed_prepare_revalidation_checks_chat_wire_before_expansion(tmp_path: Path, monkeypatch):
    from src.autoslice import producer_completed_prepare_revalidation as completed
    from src.autoslice import producer_delivery_prepare as prepare
    from src.autoslice import producer_media as media
    from src.autoslice import producer_package_finalization as finalization
    from src.autoslice import publication_title_exception as title_policy
    from src.autoslice import publish_staging_paths as staging_paths
    from src.autoslice import review_package_source_fact_audit as source_fact
    from src.autoslice import final_review_contract as review_contract
    from src.autoslice import final_subtitle_audio_gate as subtitle_audio
    from src.autoslice import addressee_attribution as addressee
    from src.autoslice import subtitle_validation as subtitle_validation

    chat_path, chat_raw, _chat, review = _compact_chat(tmp_path)
    srt = tmp_path / "subtitle.srt"
    srt.write_text("1\n00:00:01,000 --> 00:00:02,000\n原文\n", encoding="utf-8")
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    ass = tmp_path / "speaker.ass"
    ass.write_bytes(b"ass")
    record_path = tmp_path / "record.json"
    publish_path = tmp_path / "publish.json"
    record = {
        "candidate_id": "candidate",
        "artifact_hashes": {
            "subtitle_sha256": _sha(srt.read_bytes()),
            "chat_authority_audit_sha256": _sha(chat_raw),
        },
        "publish_staging": {"title": "标题", "cover_status": "NO_COVER", "source_fact_review": {}},
        "story_contract": {},
    }
    record_path.write_text(json.dumps(record), encoding="utf-8")
    publish_path.write_text(json.dumps({"candidate_id": "candidate", "title": "标题"}), encoding="utf-8")
    paths = {
        "record": record_path, "chat_authority": chat_path, "publish": publish_path,
        "subtitle": srt, "video": video, "speaker_ass": ass,
    }
    monkeypatch.setattr(completed, "_read_document", lambda _handle: {
        "artifacts": [{"role": role, "source_path": str(path)} for role, path in paths.items()]
    })
    monkeypatch.setattr(completed, "_read_regular_bytes", lambda path: Path(path).read_bytes())
    monkeypatch.setattr(media, "_validated_burned_artifact", lambda _record: video)
    monkeypatch.setattr(media, "_validated_burned_ass_artifact", lambda _record: ass)
    monkeypatch.setattr(prepare, "_recut_provenance", lambda _record: None)
    monkeypatch.setattr(staging_paths, "validate_staged_editorial_files", lambda *args, **kwargs: None)
    monkeypatch.setattr(title_policy, "candidate_title_policy_violations", lambda **kwargs: [])
    monkeypatch.setattr(subtitle_validation, "validate_srt_file", lambda _path: {"errors": []})
    monkeypatch.setattr(subtitle_audio, "validate_final_subtitle_audio_check", lambda *args, **kwargs: None)
    monkeypatch.setattr(source_fact, "_source_fact_receipt_valid", lambda *args, **kwargs: True)
    monkeypatch.setattr(addressee, "build_addressee_evidence", lambda *args, **kwargs: (
        "原文", SimpleNamespace(speaker_evidence={})
    ))
    monkeypatch.setattr(finalization, "_audit_story_bound_cover", lambda *args, **kwargs: ([], None))
    seen: list[object] = []
    monkeypatch.setattr(review_contract, "validate_final_review_release",
                        lambda value, **kwargs: seen.append(value))

    handle = SimpleNamespace(lane="talk", candidate_id="candidate", runtime_root=tmp_path)
    completed.validate_completed_prepare_artifacts(handle, {})
    assert seen == [review]
