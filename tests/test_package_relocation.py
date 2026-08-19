from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from src.autoslice.addressee_attribution import rebuild_speaker_evidence
from src.autoslice.package_relocation import (
    PackageRelocationError,
    relocate_slice_package,
)
from src.autoslice.review_evidence import SourceCue
from src.autoslice.source_fact_review import (
    review_and_repair_source_facts,
    validate_source_fact_review,
)


CID = "auto_synthetic_100_200"
SOURCE_WORKSPACE = "/home/维护者/Project"
SOURCE_REPO = f"{SOURCE_WORKSPACE}/repo"
SOURCE_PACKAGE = (
    f"{SOURCE_WORKSPACE}/vtuber-reproduce/out/2099-01-01/{CID}/replacement_recuts"
)
SOURCE_CANDIDATE = str(Path(SOURCE_PACKAGE).parent)


def _json_bytes(value: dict) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode()


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _write(path: Path, payload: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return _digest(payload)


def _source_package_path(relative: str) -> str:
    return f"{SOURCE_PACKAGE}/{relative}"


def _source_repo_path(relative: str) -> str:
    return f"{SOURCE_REPO}/{relative}"


@dataclass
class Fixture:
    package: Path
    repo: Path
    evidence: Path
    record_path: Path
    speaker_path: Path
    publish_path: Path
    before: dict[str, bytes]

    def relocate(self) -> dict:
        return relocate_slice_package(
            self.package,
            candidate_id=CID,
            source_package_root=SOURCE_PACKAGE,
            destination_package_root=self.package,
            source_repo_root=SOURCE_REPO,
            destination_repo_root=self.repo,
            evidence_root=self.evidence,
            source_workspace_root=SOURCE_WORKSPACE,
        )


def _fixture(tmp_path: Path) -> Fixture:
    package = tmp_path / "candidate" / "replacement_recuts"
    package.mkdir(parents=True)
    evidence = package.parent
    repo = tmp_path / "deployed-repo"
    repo.mkdir()

    payloads = {
        "video": b"synthetic-video",
        "subtitle": b"1\n00:00:00,000 --> 00:00:01,000\nhello\n",
        "speaker_srt": b"1\n00:00:00,000 --> 00:00:01,000\n[HOST] hello\n",
        "ass": b"synthetic-ass",
        "burned": b"synthetic-burned-video",
        "cover": b"synthetic-final-cover",
        "pre": b"synthetic-pre-overlay",
        "ai": b"synthetic-ai-background",
        "reference": b"synthetic-reference",
        "mask": b"synthetic-title-mask",
        "chat": b'{"schema":"chat"}\n',
        "context": b'{"schema":"context"}\n',
        "profile": b'{"schema":"profile"}\n',
        "override": b'{"schema":"override"}\n',
    }
    files = {
        "video": f"{CID}.recut.mp4",
        "subtitle": f"{CID}.recut.srt",
        "speaker_srt": f"{CID}.recut.speaker-final.srt",
        "ass": f"{CID}.recut.speaker-final.ass",
        "burned": f"{CID}.recut.burned-final-speaker.mp4",
        "cover": f"covers/{CID}.cover.png",
        "pre": f"covers/{CID}.cover.pre-overlay.png",
        "ai": f"covers_ai_original/{CID}.ai.png",
        "reference": f"cover_refs/{CID}.png",
        "mask": f"covers/{CID}.cover.title-mask.png",
    }
    hashes = {
        key: _write(package / files[key], payloads[key])
        for key in files
    }
    chat_name = f"{CID}.chat-authority.json"
    context_name = f"{CID}.clip-context.json"
    hashes["chat"] = _write(evidence / chat_name, payloads["chat"])
    hashes["context"] = _write(evidence / context_name, payloads["context"])
    hashes["profile"] = _write(
        repo / "assets/lidousha/profile.json", payloads["profile"]
    )
    hashes["override"] = _write(
        repo / "assets/lidousha/override.json", payloads["override"]
    )

    speaker = {
        "schema_version": "lidousha-speaker-finalization.v1",
        "status": "READY",
        "production_ready": True,
        "source_media": _source_package_path(files["video"]),
        "source_media_sha256": hashes["video"],
        "text_final_srt": _source_package_path(files["subtitle"]),
        "text_final_srt_sha256": hashes["subtitle"],
        "profile": _source_repo_path("assets/lidousha/profile.json"),
        "profile_sha256": "sha256:" + hashes["profile"],
        "speaker_override": _source_repo_path("assets/lidousha/override.json"),
        "speaker_override_sha256": "sha256:" + hashes["override"],
        "source_session_anchor_manifest": None,
        "source_session_anchor_manifest_sha256": None,
        "mixed_overlap_evidence": None,
        "mixed_overlap_evidence_sha256": None,
        "output_review_srt": _source_package_path(files["speaker_srt"]),
        "output_review_srt_sha256": hashes["speaker_srt"],
        "output_ass": _source_package_path(files["ass"]),
        "output_ass_sha256": hashes["ass"],
        "analysis": {
            "frozen_witness_path": f"{SOURCE_CANDIDATE}/speaker-work/cue.wav"
        },
        "final_decisions": [{"speaker": "HOST", "confidence": 0.99}],
        "ephemeral_runtime_paths": {
            "producer_copy": f"{SOURCE_PACKAGE}/ephemeral-copy.srt"
        },
        "runtime_host": "wsl",
    }
    speaker_payload = _json_bytes(speaker)
    speaker_path = package / f"{CID}.recut.speaker-final.json"
    speaker_path.write_bytes(speaker_payload)
    payloads["chat"] = _json_bytes(
        {
            "schema": "chat",
            "speaker_manifest_sha256": _digest(speaker_payload),
        }
    )
    hashes["chat"] = _write(evidence / chat_name, payloads["chat"])

    cover_generation = {
        "status": "AI_COVER_READY",
        "final_cover": _source_package_path(files["cover"]),
        "final_cover_sha256": "sha256:" + hashes["cover"],
        "pre_overlay_path": _source_package_path(files["pre"]),
        "pre_overlay_sha256": "sha256:" + hashes["pre"],
        "ai_background": _source_package_path(files["ai"]),
        "ai_background_sha256": "sha256:" + hashes["ai"],
        "reference_image": _source_package_path(files["reference"]),
        "reference_sha256": "sha256:" + hashes["reference"],
        "rendered_text_pixels": {
            "mask_path": _source_package_path(files["mask"]),
            "mask_sha256": "sha256:" + hashes["mask"],
            "pre_overlay_path": _source_package_path(files["pre"]),
            "pre_overlay_sha256": "sha256:" + hashes["pre"],
        },
        "request_path": f"{SOURCE_PACKAGE}/evidence/frozen-request.json",
        "request_sha256": "sha256:" + "1" * 64,
        "response_path": f"{SOURCE_PACKAGE}/evidence/frozen-response.json",
        "response_sha256": "sha256:" + "2" * 64,
        "final_host_identity_verification": {
            "witness": {
                "image_path": f"{SOURCE_PACKAGE}/covers/frozen-witness.png"
            }
        },
    }
    base_artifact_hashes = {
        "video_sha256": "sha256:" + hashes["video"],
        "subtitle_sha256": "sha256:" + hashes["subtitle"],
        "speaker_review_srt_sha256": "sha256:" + hashes["speaker_srt"],
        "ass_sha256": "sha256:" + hashes["ass"],
        "burned_video_sha256": "sha256:" + hashes["burned"],
        "chat_authority_audit_sha256": "sha256:" + hashes["chat"],
        "clip_context_file_sha256": "sha256:" + hashes["context"],
        "cover_sha256": "sha256:" + hashes["cover"],
        "ai_background_sha256": "sha256:" + hashes["ai"],
        "cover_reference_sha256": "sha256:" + hashes["reference"],
    }
    publish = {
        "schema_version": "shadow-publish-draft.v1",
        "candidate_id": CID,
        "upload_enabled": False,
        "video_path": _source_package_path(files["video"]),
        "cover_path": _source_package_path(files["cover"]),
        "title": "synthetic title",
        "cover_generation": cover_generation,
        "artifact_hashes": base_artifact_hashes,
        "source_fact_review": {
            "frozen_path": f"{SOURCE_PACKAGE}/evidence/fact-review.json"
        },
        "title_story_audit": {"status": "PASS"},
    }
    publish_payload = _json_bytes(publish)
    publish_path = package / f"{CID}.recut.publish.json"
    publish_path.write_bytes(publish_payload)

    record = {
        "status": "MATERIALIZED",
        "media_path": _source_package_path(files["video"]),
        "subtitle_path": _source_package_path(files["subtitle"]),
        "speaker_review_srt_path": _source_package_path(files["speaker_srt"]),
        "subtitle_ass_path": _source_package_path(files["ass"]),
        "speaker_finalization_manifest_path": _source_package_path(
            f"{CID}.recut.speaker-final.json"
        ),
        "speaker_finalization_manifest_sha256": "sha256:"
        + _digest(speaker_payload),
        "speaker_finalization": speaker,
        "chat_authority_audit_path": f"{SOURCE_CANDIDATE}/{chat_name}",
        "clip_context_path": f"{SOURCE_CANDIDATE}/{context_name}",
        "burned_preview": {
            "path": _source_package_path(files["burned"]),
            "ass_path": _source_package_path(files["ass"]),
            "command": [
                "ffmpeg",
                "-i",
                _source_package_path(files["video"]),
                _source_package_path(files["burned"]),
            ],
            "branding_intro": {
                "policy_manifest_path": _source_repo_path(
                    "assets/lidousha/intro/frozen.json"
                )
            },
        },
        "publish_staging": {
            "status": "STAGED",
            "cover_path": publish["cover_path"],
            "publish_json_path": _source_package_path(
                f"{CID}.recut.publish.json"
            ),
            "cover_generation": cover_generation,
            "title": publish["title"],
            "source_fact_review": publish["source_fact_review"],
            "title_story_audit": publish["title_story_audit"],
            "upload_enabled": publish["upload_enabled"],
        },
        "artifact_hashes": {
            **base_artifact_hashes,
            "publish_draft_sha256": "sha256:" + _digest(publish_payload),
        },
        "boundary_audit": {
            "frozen_source_path": f"{SOURCE_CANDIDATE}/boundary-audit.json"
        },
        "story_contract": {
            "frozen_context_path": f"{SOURCE_CANDIDATE}/story-context.json"
        },
        "selection_scorecard": {"status": "PASS"},
        "subtitle_timing_qa": {"status": "PASS"},
        "upload_tags": ["李豆沙", "synthetic"],
    }
    record_payload = _json_bytes(record)
    record_path = package / f"{CID}.record.json"
    record_path.write_bytes(record_payload)
    return Fixture(
        package=package,
        repo=repo,
        evidence=evidence,
        record_path=record_path,
        speaker_path=speaker_path,
        publish_path=publish_path,
        before={
            "record": record_payload,
            "speaker": speaker_payload,
            "publish": publish_payload,
        },
    )


def _load(path: Path) -> dict:
    return json.loads(path.read_text())


def _changed_pointer_texts(before: object, after: object, pointer: tuple[str, ...] = ()) -> list[str]:
    if isinstance(before, dict) and isinstance(after, dict):
        if set(before) != set(after):
            return ["/" + "/".join(pointer)]
        rows: list[str] = []
        for key in before:
            rows.extend(
                _changed_pointer_texts(before[key], after[key], (*pointer, key))
            )
        return rows
    if isinstance(before, list) and isinstance(after, list):
        if len(before) != len(after):
            return ["/" + "/".join(pointer)]
        rows: list[str] = []
        for index, (left, right) in enumerate(zip(before, after)):
            rows.extend(
                _changed_pointer_texts(left, right, (*pointer, str(index)))
            )
        return rows
    return [] if before == after else ["/" + "/".join(pointer)]


def _write_forged_committed_graph(
    fixture: Fixture,
    *,
    speaker: dict,
    publish: dict,
    record: dict,
) -> None:
    speaker_payload = _json_bytes(speaker)
    publish_payload = _json_bytes(publish)
    record["speaker_finalization"] = speaker
    record["speaker_finalization_manifest_sha256"] = (
        "sha256:" + _digest(speaker_payload)
    )
    record["artifact_hashes"]["publish_draft_sha256"] = (
        "sha256:" + _digest(publish_payload)
    )
    record_payload = _json_bytes(record)
    payloads = {
        "speaker": speaker_payload,
        "publish": publish_payload,
        "record": record_payload,
    }
    documents = {"speaker": speaker, "publish": publish, "record": record}
    journal_path = fixture.package / f".{CID}.package-relocation.json"
    journal = _load(journal_path)
    for label in ("speaker", "publish", "record"):
        before = json.loads(fixture.before[label])
        journal["documents"][label]["after_sha256"] = _digest(payloads[label])
        journal["documents"][label]["changed_pointers"] = sorted(
            _changed_pointer_texts(before, documents[label])
        )
    journal["speaker_manifest_lineage"]["after_sha256"] = _digest(
        speaker_payload
    )
    fixture.speaker_path.write_bytes(speaker_payload)
    fixture.publish_path.write_bytes(publish_payload)
    fixture.record_path.write_bytes(record_payload)
    journal_path.write_bytes(_json_bytes(journal))


def _replace_shared_artifact_hash(
    fixture: Fixture, hash_key: str, digest: str
) -> None:
    publish = _load(fixture.publish_path)
    publish["artifact_hashes"][hash_key] = "sha256:" + digest
    publish_payload = _json_bytes(publish)
    fixture.publish_path.write_bytes(publish_payload)
    record = _load(fixture.record_path)
    record["artifact_hashes"] = {
        **publish["artifact_hashes"],
        "publish_draft_sha256": "sha256:" + _digest(publish_payload),
    }
    fixture.record_path.write_bytes(_json_bytes(record))


def _bind_candidate_record_artifact(
    fixture: Fixture,
    *,
    path_key: str,
    hash_key: str,
    basename: str,
    payload: bytes,
) -> Path:
    artifact = fixture.evidence / basename
    digest = _write(artifact, payload)
    _replace_shared_artifact_hash(fixture, hash_key, digest)
    record = _load(fixture.record_path)
    record[path_key] = f"{SOURCE_CANDIDATE}/{basename}"
    fixture.record_path.write_bytes(_json_bytes(record))
    return artifact


def _bind_package_record_artifact(
    fixture: Fixture,
    *,
    path_key: str,
    hash_key: str,
    basename: str,
    payload: bytes,
) -> Path:
    artifact = fixture.package / basename
    digest = _write(artifact, payload)
    _replace_shared_artifact_hash(fixture, hash_key, digest)
    record = _load(fixture.record_path)
    record[path_key] = _source_package_path(basename)
    fixture.record_path.write_bytes(_json_bytes(record))
    return artifact


def test_relocation_commits_hash_bound_graph_and_is_idempotent(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    before_record = _load(fixture.record_path)
    before_speaker = _load(fixture.speaker_path)
    before_publish = _load(fixture.publish_path)

    receipt = fixture.relocate()

    assert receipt["status"] == "COMMITTED"
    record = _load(fixture.record_path)
    speaker = _load(fixture.speaker_path)
    publish = _load(fixture.publish_path)
    assert record["speaker_finalization"] == speaker
    assert record["speaker_finalization_manifest_sha256"] == (
        "sha256:" + _digest(fixture.speaker_path.read_bytes())
    )
    assert record["artifact_hashes"]["publish_draft_sha256"] == (
        "sha256:" + _digest(fixture.publish_path.read_bytes())
    )
    assert record["publish_staging"]["cover_generation"] == publish["cover_generation"]
    assert speaker["source_media"].startswith(str(fixture.package) + "/")
    assert speaker["profile"].startswith(str(fixture.repo) + "/")
    assert publish["video_path"].startswith(str(fixture.package) + "/")
    assert record["chat_authority_audit_path"] == str(
        fixture.package / f"{CID}.chat-authority.json"
    )
    assert record["clip_context_path"] == str(
        fixture.package / f"{CID}.clip-context.json"
    )
    assert (fixture.package / f"{CID}.chat-authority.json").read_bytes() == (
        fixture.evidence / f"{CID}.chat-authority.json"
    ).read_bytes()

    # Provenance-bearing subtrees are deliberately not rewritten.
    assert speaker["analysis"] == before_speaker["analysis"]
    assert speaker["ephemeral_runtime_paths"] == before_speaker["ephemeral_runtime_paths"]
    assert publish["cover_generation"]["request_path"] == before_publish[
        "cover_generation"
    ]["request_path"]
    assert record["burned_preview"]["command"] == before_record[
        "burned_preview"
    ]["command"]
    assert record["boundary_audit"] == before_record["boundary_audit"]
    changed = receipt["documents"]
    assert "/analysis/frozen_witness_path" not in changed["speaker"][
        "changed_pointers"
    ]
    assert "/burned_preview/command" not in changed["record"][
        "changed_pointers"
    ]
    assert "/speaker_finalization_manifest_sha256" in changed["record"][
        "changed_pointers"
    ]
    assert receipt["speaker_manifest_lineage"]["before_sha256"] == (
        json.loads(
            (fixture.package / f"{CID}.chat-authority.json").read_text()
        )["speaker_manifest_sha256"]
    )

    stable = {
        "record": fixture.record_path.read_bytes(),
        "speaker": fixture.speaker_path.read_bytes(),
        "publish": fixture.publish_path.read_bytes(),
    }
    second = fixture.relocate()
    assert second == receipt
    assert fixture.record_path.read_bytes() == stable["record"]
    assert fixture.speaker_path.read_bytes() == stable["speaker"]
    assert fixture.publish_path.read_bytes() == stable["publish"]


def test_split_speaker_source_fact_identity_survives_real_relocation(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    segments = [
        ("00:00:00,000", "00:00:00,500", "李豆沙", "he"),
        ("00:00:00,500", "00:00:01,000", "连线", "llo"),
    ]
    speaker_srt = (
        "\n\n".join(
            f"{index}\n{start} --> {end}\n[{speaker}] {text}"
            for index, (start, end, speaker, text) in enumerate(
                segments, start=1
            )
        )
        + "\n"
    ).encode()
    speaker_srt_sha256 = _write(
        fixture.package / f"{CID}.recut.speaker-final.srt",
        speaker_srt,
    )
    cues = [SourceCue("source-1", 0, 1_000, "hello")]

    speaker = _load(fixture.speaker_path)
    speaker.update(
        {
            "output_review_srt_sha256": speaker_srt_sha256,
            "source_cue_count": 1,
            "output_cue_count": 2,
            "final_decisions": [
                {
                    "source_index": 1,
                    "start": start,
                    "end": end,
                    "speaker": speaker_label,
                    "text": text,
                    "decision_source": "reviewer_reviewed_truth",
                    "layer": 0,
                    "placement": "main",
                }
                for start, end, speaker_label, text in segments
            ],
        }
    )
    speaker_payload = _json_bytes(speaker)
    fixture.speaker_path.write_bytes(speaker_payload)

    record = _load(fixture.record_path)
    record["speaker_finalization"] = speaker
    record["speaker_finalization_manifest_sha256"] = (
        "sha256:" + _digest(speaker_payload)
    )
    record["artifact_hashes"]["speaker_review_srt_sha256"] = (
        "sha256:" + speaker_srt_sha256
    )
    before_evidence = rebuild_speaker_evidence(
        record,
        cues,
        speaker_srt_bytes=speaker_srt,
        speaker_manifest_bytes=speaker_payload,
    )
    assert before_evidence.transcript == "1.1 [李豆沙] he\n1.2 [连线] llo"

    hook = "这段测试字幕内容完整。"
    title = "【李豆沙】完整的测试字幕"
    final_transcript = "hello"
    source_fact_review = review_and_repair_source_facts(
        selection_hook=hook,
        title=title,
        final_transcript=final_transcript,
        speaker_transcript=before_evidence.transcript,
        speaker_evidence=before_evidence.speaker_evidence,
        clip_context_prompt="",
        llm_call=lambda _prompt: json.dumps(
            {
                "schema_version": "lidousha-source-fact-review.v1",
                "status": "KEEP",
                "final_selection_hook": hook,
                "final_title": title,
                "supported_by": ["final_transcript"],
                "changed_surfaces": [],
                "addressee_attribution": [],
                "selection_scorecard_review": {
                    "status": "NOT_NEEDED",
                    "reason": "selection hook remains unchanged",
                },
                "summary": "字幕证据足以支持测试文案。",
            },
            ensure_ascii=False,
        ),
    )
    validation = {
        "selection_hook": hook,
        "title": title,
        "final_transcript": final_transcript,
        "clip_context_prompt": "",
        "speaker_evidence": before_evidence.speaker_evidence,
    }
    assert validate_source_fact_review(source_fact_review, **validation)

    chat_path = fixture.evidence / f"{CID}.chat-authority.json"
    chat = _load(chat_path)
    chat["speaker_manifest_sha256"] = _digest(speaker_payload)
    chat_payload = _json_bytes(chat)
    chat_path.write_bytes(chat_payload)

    publish = _load(fixture.publish_path)
    publish["source_fact_review"] = source_fact_review
    publish["artifact_hashes"].update(
        {
            "speaker_review_srt_sha256": "sha256:" + speaker_srt_sha256,
            "chat_authority_audit_sha256": "sha256:" + _digest(chat_payload),
        }
    )
    publish_payload = _json_bytes(publish)
    fixture.publish_path.write_bytes(publish_payload)
    record["publish_staging"]["source_fact_review"] = source_fact_review
    record["artifact_hashes"] = {
        **publish["artifact_hashes"],
        "publish_draft_sha256": "sha256:" + _digest(publish_payload),
    }
    fixture.record_path.write_bytes(_json_bytes(record))

    receipt = fixture.relocate()

    relocated_record = _load(fixture.record_path)
    relocated_speaker_payload = fixture.speaker_path.read_bytes()
    after_evidence = rebuild_speaker_evidence(
        relocated_record,
        cues,
        speaker_srt_bytes=speaker_srt,
        speaker_manifest_bytes=relocated_speaker_payload,
    )
    assert after_evidence.speaker_evidence == before_evidence.speaker_evidence
    assert (
        after_evidence.speaker_evidence_sha256
        == before_evidence.speaker_evidence_sha256
    )
    relocated_review = relocated_record["publish_staging"]["source_fact_review"]
    assert relocated_review == source_fact_review
    assert validate_source_fact_review(
        relocated_review,
        **{
            **validation,
            "speaker_evidence": after_evidence.speaker_evidence,
        },
    )
    assert _load(fixture.publish_path)["source_fact_review"] == relocated_review
    assert "/output_review_srt" in receipt["documents"]["speaker"][
        "changed_pointers"
    ]
    assert "/speaker_finalization/output_review_srt" in receipt["documents"][
        "record"
    ]["changed_pointers"]


def test_prepared_mixed_generation_recovers_forward(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    fixture.relocate()
    journal_path = fixture.package / f".{CID}.package-relocation.json"
    journal = _load(journal_path)
    journal["status"] = "PREPARED"
    journal.pop("committed_at")
    journal_path.write_bytes(_json_bytes(journal))

    # Simulate a crash after publish but before the speaker swap was durable.
    fixture.speaker_path.write_bytes(fixture.before["speaker"])
    recovered = fixture.relocate()

    assert recovered["status"] == "COMMITTED"
    assert _digest(fixture.speaker_path.read_bytes()) == recovered["documents"][
        "speaker"
    ]["after_sha256"]
    assert _load(fixture.record_path)["speaker_finalization"] == _load(
        fixture.speaker_path
    )


def test_unknown_wsl_path_fails_before_any_mutation(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    record = _load(fixture.record_path)
    record["unreviewed_runtime_path"] = f"{SOURCE_WORKSPACE}/secret/file.json"
    fixture.record_path.write_bytes(_json_bytes(record))
    before = fixture.record_path.read_bytes()

    # 合并说明：主线 bc4e853 上游化同一份 fasttrack 合同时，把这条报错从 "unknown
    # WSL path" 泛化成 "unknown external-host path"（产出主机不止 wsl，还有 Mac）。
    # 取主线那版模块，故这里同步期望词面；判据本身没变。
    with pytest.raises(PackageRelocationError, match="unknown external-host path"):
        fixture.relocate()

    assert fixture.record_path.read_bytes() == before
    assert not (fixture.package / f".{CID}.package-relocation.json").exists()
    assert not (fixture.package / f"{CID}.chat-authority.json").exists()


def test_symlinked_artifact_fails_closed(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    media = fixture.package / f"{CID}.recut.mp4"
    outside = tmp_path / "outside-video"
    outside.write_bytes(media.read_bytes())
    media.unlink()
    media.symlink_to(outside)

    with pytest.raises(PackageRelocationError, match="symlink forbidden"):
        fixture.relocate()

    assert not (fixture.package / f".{CID}.package-relocation.json").exists()


def test_embedded_speaker_mismatch_is_rejected(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    record = _load(fixture.record_path)
    record["speaker_finalization"]["runtime_host"] = "tampered"
    fixture.record_path.write_bytes(_json_bytes(record))

    with pytest.raises(PackageRelocationError, match="embedded speaker differs"):
        fixture.relocate()


def test_stale_artifact_hash_is_rejected(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    (fixture.package / f"{CID}.recut.srt").write_bytes(b"tampered subtitle")

    with pytest.raises(PackageRelocationError, match="SHA-256 mismatch"):
        fixture.relocate()


def test_mutable_path_outside_destination_roots_is_rejected(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    publish = _load(fixture.publish_path)
    publish["video_path"] = "/var/tmp/untrusted-video.mp4"
    publish_payload = _json_bytes(publish)
    fixture.publish_path.write_bytes(publish_payload)
    record = _load(fixture.record_path)
    record["artifact_hashes"]["publish_draft_sha256"] = (
        "sha256:" + _digest(publish_payload)
    )
    fixture.record_path.write_bytes(_json_bytes(record))

    with pytest.raises(PackageRelocationError, match="escapes trusted roots"):
        fixture.relocate()


def test_artifact_map_cover_binding_drift_is_rejected(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    publish = _load(fixture.publish_path)
    publish["artifact_hashes"]["ai_background_sha256"] = "sha256:" + "a" * 64
    publish_payload = _json_bytes(publish)
    fixture.publish_path.write_bytes(publish_payload)
    record = _load(fixture.record_path)
    record["artifact_hashes"] = {
        **publish["artifact_hashes"],
        "publish_draft_sha256": "sha256:" + _digest(publish_payload),
    }
    fixture.record_path.write_bytes(_json_bytes(record))

    with pytest.raises(PackageRelocationError, match="record/cover"):
        fixture.relocate()


def test_stale_mirrored_title_is_rejected(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    record = _load(fixture.record_path)
    record["publish_staging"]["title"] = "stale title"
    fixture.record_path.write_bytes(_json_bytes(record))

    with pytest.raises(
        PackageRelocationError,
        match="record/publish: mirrored field title differs",
    ):
        fixture.relocate()


def test_missing_mirrored_title_is_rejected(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    record = _load(fixture.record_path)
    del record["publish_staging"]["title"]
    fixture.record_path.write_bytes(_json_bytes(record))

    with pytest.raises(PackageRelocationError, match="staging field set differs"):
        fixture.relocate()


@pytest.mark.parametrize(
    ("path_key", "hash_key"),
    [
        ("talk_filler_audit_path", "talk_filler_audit_sha256"),
    ],
)
def test_candidate_root_audits_are_relocated_and_hash_checked(
    tmp_path: Path,
    path_key: str,
    hash_key: str,
) -> None:
    fixture = _fixture(tmp_path)
    basename = f"{CID}.{path_key}.json"
    artifact = _bind_candidate_record_artifact(
        fixture,
        path_key=path_key,
        hash_key=hash_key,
        basename=basename,
        payload=b'{"status":"PASS"}\n',
    )

    fixture.relocate()
    assert _load(fixture.record_path)[path_key] == str(artifact)

    broken = _fixture(tmp_path / "broken")
    broken_artifact = _bind_candidate_record_artifact(
        broken,
        path_key=path_key,
        hash_key=hash_key,
        basename=basename,
        payload=b'{"status":"PASS"}\n',
    )
    broken_artifact.write_bytes(b"tampered")
    with pytest.raises(PackageRelocationError, match="SHA-256 mismatch"):
        broken.relocate()


@pytest.mark.parametrize(
    ("path_key", "hash_key"),
    [
        ("redelivery_baseline_audit_path", "redelivery_baseline_audit_sha256"),
        ("subtitle_regression_audit_path", "subtitle_regression_audit_sha256"),
        ("text_finalization_manifest_path", "text_finalization_manifest_sha256"),
    ],
)
def test_package_root_audits_are_relocated_and_hash_checked(
    tmp_path: Path,
    path_key: str,
    hash_key: str,
) -> None:
    fixture = _fixture(tmp_path)
    basename = f"{CID}.{path_key}.json"
    artifact = _bind_package_record_artifact(
        fixture,
        path_key=path_key,
        hash_key=hash_key,
        basename=basename,
        payload=b'{"status":"PASS"}\n',
    )

    fixture.relocate()
    assert _load(fixture.record_path)[path_key] == str(artifact)


def test_committed_media_cannot_rebind_from_package_to_candidate(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    fixture.relocate()
    source = fixture.package / f"{CID}.recut.mp4"
    duplicate = fixture.evidence / f"{CID}.same-video.mp4"
    duplicate.write_bytes(source.read_bytes())
    speaker = _load(fixture.speaker_path)
    publish = _load(fixture.publish_path)
    record = _load(fixture.record_path)
    speaker["source_media"] = str(duplicate)
    publish["video_path"] = str(duplicate)
    record["media_path"] = str(duplicate)
    _write_forged_committed_graph(
        fixture, speaker=speaker, publish=publish, record=record
    )
    frozen = {
        path: path.read_bytes()
        for path in (fixture.speaker_path, fixture.publish_path, fixture.record_path)
    }

    with pytest.raises(PackageRelocationError, match="wrong root role.*package"):
        fixture.relocate()

    assert all(path.read_bytes() == payload for path, payload in frozen.items())


@pytest.mark.parametrize(
    ("path_key", "hash_key", "source_role", "target_role"),
    [
        (
            "talk_filler_audit_path",
            "talk_filler_audit_sha256",
            "candidate",
            "package",
        ),
        (
            "subtitle_regression_audit_path",
            "subtitle_regression_audit_sha256",
            "package",
            "candidate",
        ),
    ],
)
def test_committed_audit_cannot_cross_root_with_same_bytes(
    tmp_path: Path,
    path_key: str,
    hash_key: str,
    source_role: str,
    target_role: str,
) -> None:
    fixture = _fixture(tmp_path)
    basename = f"{CID}.{path_key}.json"
    bind = {
        "candidate": _bind_candidate_record_artifact,
        "package": _bind_package_record_artifact,
    }[source_role]
    source = bind(
        fixture,
        path_key=path_key,
        hash_key=hash_key,
        basename=basename,
        payload=b'{"status":"PASS"}\n',
    )
    fixture.before = {
        "record": fixture.record_path.read_bytes(),
        "speaker": fixture.speaker_path.read_bytes(),
        "publish": fixture.publish_path.read_bytes(),
    }
    fixture.relocate()
    target_root = {
        "candidate": fixture.evidence,
        "package": fixture.package,
    }[target_role]
    duplicate = target_root / f"duplicate-{basename}"
    duplicate.write_bytes(source.read_bytes())
    speaker = _load(fixture.speaker_path)
    publish = _load(fixture.publish_path)
    record = _load(fixture.record_path)
    record[path_key] = str(duplicate)
    _write_forged_committed_graph(
        fixture, speaker=speaker, publish=publish, record=record
    )

    with pytest.raises(PackageRelocationError, match="wrong root role"):
        fixture.relocate()


def test_text_finalization_manifest_must_be_hash_bound_and_exist(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    record = _load(fixture.record_path)
    record["text_finalization_manifest_path"] = (
        f"{SOURCE_PACKAGE}/{CID}.missing-text-finalization.json"
    )
    fixture.record_path.write_bytes(_json_bytes(record))

    with pytest.raises(PackageRelocationError, match="incomplete binding"):
        fixture.relocate()

    bound = _fixture(tmp_path / "bound")
    _replace_shared_artifact_hash(
        bound,
        "text_finalization_manifest_sha256",
        "0" * 64,
    )
    record = _load(bound.record_path)
    record["text_finalization_manifest_path"] = (
        f"{SOURCE_PACKAGE}/{CID}.missing-text-finalization.json"
    )
    bound.record_path.write_bytes(_json_bytes(record))
    with pytest.raises(PackageRelocationError, match="artifact missing"):
        bound.relocate()


def test_chat_authority_must_bind_pre_relocation_speaker(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    chat_path = fixture.evidence / f"{CID}.chat-authority.json"
    chat = _load(chat_path)
    chat["speaker_manifest_sha256"] = "0" * 64
    chat_payload = _json_bytes(chat)
    chat_path.write_bytes(chat_payload)
    _replace_shared_artifact_hash(
        fixture,
        "chat_authority_audit_sha256",
        _digest(chat_payload),
    )

    with pytest.raises(
        PackageRelocationError,
        match="speaker manifest does not bind pre-relocation bytes",
    ):
        fixture.relocate()


def test_candidate_local_speaker_override_maps_to_evidence_root(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    override_name = f"{CID}.candidate-override.json"
    override_payload = b'{"schema":"candidate-override"}\n'
    override_sha = _write(fixture.evidence / override_name, override_payload)

    speaker = _load(fixture.speaker_path)
    speaker["speaker_override"] = f"{SOURCE_CANDIDATE}/{override_name}"
    speaker["speaker_override_sha256"] = "sha256:" + override_sha
    speaker_payload = _json_bytes(speaker)
    fixture.speaker_path.write_bytes(speaker_payload)

    chat_path = fixture.evidence / f"{CID}.chat-authority.json"
    chat = _load(chat_path)
    chat["speaker_manifest_sha256"] = _digest(speaker_payload)
    chat_payload = _json_bytes(chat)
    chat_path.write_bytes(chat_payload)

    publish = _load(fixture.publish_path)
    publish["artifact_hashes"]["chat_authority_audit_sha256"] = (
        "sha256:" + _digest(chat_payload)
    )
    publish_payload = _json_bytes(publish)
    fixture.publish_path.write_bytes(publish_payload)
    record = _load(fixture.record_path)
    record["speaker_finalization"] = speaker
    record["speaker_finalization_manifest_sha256"] = (
        "sha256:" + _digest(speaker_payload)
    )
    record["artifact_hashes"] = {
        **publish["artifact_hashes"],
        "publish_draft_sha256": "sha256:" + _digest(publish_payload),
    }
    fixture.record_path.write_bytes(_json_bytes(record))

    fixture.relocate()
    assert _load(fixture.speaker_path)["speaker_override"] == str(
        fixture.evidence / override_name
    )


def test_committed_profile_cannot_rebind_from_repo_to_package(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    fixture.relocate()
    speaker = _load(fixture.speaker_path)
    duplicate = fixture.package / "duplicate-profile.json"
    duplicate.write_bytes(Path(speaker["profile"]).read_bytes())
    speaker["profile"] = str(duplicate)
    publish = _load(fixture.publish_path)
    record = _load(fixture.record_path)
    _write_forged_committed_graph(
        fixture, speaker=speaker, publish=publish, record=record
    )

    with pytest.raises(PackageRelocationError, match="wrong root role.*repo"):
        fixture.relocate()


def _configure_candidate_override(fixture: Fixture) -> Path:
    override_name = f"{CID}.candidate-override.json"
    override_payload = b'{"schema":"candidate-override"}\n'
    override = fixture.evidence / override_name
    override_sha = _write(override, override_payload)
    speaker = _load(fixture.speaker_path)
    speaker["speaker_override"] = f"{SOURCE_CANDIDATE}/{override_name}"
    speaker["speaker_override_sha256"] = "sha256:" + override_sha
    speaker_payload = _json_bytes(speaker)
    fixture.speaker_path.write_bytes(speaker_payload)
    chat_path = fixture.evidence / f"{CID}.chat-authority.json"
    chat = _load(chat_path)
    chat["speaker_manifest_sha256"] = _digest(speaker_payload)
    chat_payload = _json_bytes(chat)
    chat_path.write_bytes(chat_payload)
    publish = _load(fixture.publish_path)
    publish["artifact_hashes"]["chat_authority_audit_sha256"] = (
        "sha256:" + _digest(chat_payload)
    )
    publish_payload = _json_bytes(publish)
    fixture.publish_path.write_bytes(publish_payload)
    record = _load(fixture.record_path)
    record["speaker_finalization"] = speaker
    record["speaker_finalization_manifest_sha256"] = (
        "sha256:" + _digest(speaker_payload)
    )
    record["artifact_hashes"] = {
        **publish["artifact_hashes"],
        "publish_draft_sha256": "sha256:" + _digest(publish_payload),
    }
    record_payload = _json_bytes(record)
    fixture.record_path.write_bytes(record_payload)
    fixture.before = {
        "record": record_payload,
        "speaker": speaker_payload,
        "publish": publish_payload,
    }
    return override


@pytest.mark.parametrize("origin", ["repo", "candidate"])
def test_committed_override_preserves_its_preimage_root_role(
    tmp_path: Path,
    origin: str,
) -> None:
    fixture = _fixture(tmp_path)
    if origin == "candidate":
        _configure_candidate_override(fixture)
    fixture.relocate()
    speaker = _load(fixture.speaker_path)
    source = Path(speaker["speaker_override"])
    if origin == "repo":
        duplicate = fixture.evidence / "duplicate-override.json"
    else:
        duplicate = fixture.repo / "assets/lidousha/duplicate-override.json"
        duplicate.parent.mkdir(parents=True, exist_ok=True)
    duplicate.write_bytes(source.read_bytes())
    speaker["speaker_override"] = str(duplicate)
    publish = _load(fixture.publish_path)
    record = _load(fixture.record_path)
    _write_forged_committed_graph(
        fixture, speaker=speaker, publish=publish, record=record
    )

    with pytest.raises(
        PackageRelocationError,
        match="COMMITTED graph contains unrelocated locator fields",
    ):
        fixture.relocate()


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("transaction", "invalid transaction_id"),
        ("document_path", "speaker path mismatch"),
        ("changed_pointer", "speaker changed pointer not allowed"),
        ("evidence", "staged evidence chat_authority hash drift"),
        ("lineage", "speaker lineage hash mismatch"),
        ("timestamp_order", "committed_at precedes prepared_at"),
        ("commit_order", "commit_order mismatch"),
    ],
)
def test_committed_journal_tampering_is_rejected(
    tmp_path: Path,
    mutation: str,
    message: str,
) -> None:
    fixture = _fixture(tmp_path)
    fixture.relocate()
    journal_path = fixture.package / f".{CID}.package-relocation.json"
    journal = _load(journal_path)
    if mutation == "transaction":
        journal["transaction_id"] = "forged"
    elif mutation == "document_path":
        journal["documents"]["speaker"]["path"] = "other.json"
    elif mutation == "changed_pointer":
        journal["documents"]["speaker"]["changed_pointers"].append("/analysis")
        journal["documents"]["speaker"]["changed_pointers"].sort()
    elif mutation == "evidence":
        journal["staged_evidence"]["chat_authority"]["sha256"] = "0" * 64
    elif mutation == "lineage":
        journal["speaker_manifest_lineage"]["after_sha256"] = "0" * 64
    elif mutation == "timestamp_order":
        journal["prepared_at"] = "2099-01-01T00:00:00Z"
        journal["committed_at"] = "2000-01-01T00:00:00Z"
    else:
        journal["commit_order"] = list(reversed(journal["commit_order"]))
    journal_path.write_bytes(_json_bytes(journal))

    with pytest.raises(PackageRelocationError, match=message):
        fixture.relocate()


@pytest.mark.parametrize(
    ("evidence_label", "filename"),
    [
        ("chat_authority", f"{CID}.chat-authority.json"),
        ("clip_context", f"{CID}.clip-context.json"),
        (
            "pre_relocation_speaker",
            f".{CID}.pre-relocation-speaker.json",
        ),
        (
            "pre_relocation_publish",
            f".{CID}.pre-relocation-publish.json",
        ),
        (
            "pre_relocation_record",
            f".{CID}.pre-relocation-record.json",
        ),
    ],
)
def test_committed_replay_rejects_missing_staged_evidence(
    tmp_path: Path,
    evidence_label: str,
    filename: str,
) -> None:
    fixture = _fixture(tmp_path)
    fixture.relocate()
    (fixture.package / filename).unlink()

    with pytest.raises(
        PackageRelocationError,
        match=f"COMMITTED evidence missing:.*{evidence_label}",
    ):
        fixture.relocate()


def test_committed_journal_cannot_attest_a_noop_graph(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    fixture.relocate()
    journal_path = fixture.package / f".{CID}.package-relocation.json"
    journal = _load(journal_path)
    for row in journal["documents"].values():
        row["before_sha256"] = row["after_sha256"]
        row["changed_pointers"] = []
    journal["speaker_manifest_lineage"]["before_sha256"] = journal[
        "speaker_manifest_lineage"
    ]["after_sha256"]
    journal["speaker_manifest_lineage"]["chat_speaker_manifest_sha256"] = journal[
        "speaker_manifest_lineage"
    ]["after_sha256"]
    journal_path.write_bytes(_json_bytes(journal))

    with pytest.raises(PackageRelocationError, match="no-op transaction"):
        fixture.relocate()


def test_committed_journal_cannot_attest_unrelocated_wsl_locators(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    fixture.relocate()
    journal_path = fixture.package / f".{CID}.package-relocation.json"
    journal = _load(journal_path)

    speaker = json.loads(fixture.before["speaker"])
    speaker_payload = json.dumps(
        speaker, ensure_ascii=False, separators=(",", ":")
    ).encode()
    publish = json.loads(fixture.before["publish"])
    publish_payload = json.dumps(
        publish, ensure_ascii=False, separators=(",", ":")
    ).encode()
    record = json.loads(fixture.before["record"])
    record["speaker_finalization"] = speaker
    record["speaker_finalization_manifest_sha256"] = (
        "sha256:" + _digest(speaker_payload)
    )
    record["artifact_hashes"]["publish_draft_sha256"] = (
        "sha256:" + _digest(publish_payload)
    )
    record_payload = json.dumps(
        record, ensure_ascii=False, separators=(",", ":")
    ).encode()
    fixture.speaker_path.write_bytes(speaker_payload)
    fixture.publish_path.write_bytes(publish_payload)
    fixture.record_path.write_bytes(record_payload)

    after_payloads = {
        "speaker": speaker_payload,
        "publish": publish_payload,
        "record": record_payload,
    }
    for label, pointer in {
        "speaker": "/source_media",
        "publish": "/video_path",
        "record": "/media_path",
    }.items():
        journal["documents"][label]["before_sha256"] = _digest(
            fixture.before[label]
        )
        journal["documents"][label]["after_sha256"] = _digest(
            after_payloads[label]
        )
        journal["documents"][label]["changed_pointers"] = [pointer]
    journal["speaker_manifest_lineage"]["before_sha256"] = _digest(
        fixture.before["speaker"]
    )
    journal["speaker_manifest_lineage"]["after_sha256"] = _digest(
        speaker_payload
    )
    journal["speaker_manifest_lineage"]["chat_speaker_manifest_sha256"] = (
        _digest(fixture.before["speaker"])
    )
    journal_path.write_bytes(_json_bytes(journal))

    with pytest.raises(
        PackageRelocationError,
        match="COMMITTED graph contains unrelocated locator fields",
    ):
        fixture.relocate()


def test_source_and_destination_roots_cannot_be_the_same(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    with pytest.raises(PackageRelocationError, match="must not form a no-op"):
        relocate_slice_package(
            fixture.package,
            candidate_id=CID,
            source_package_root=fixture.package,
            destination_package_root=fixture.package,
            source_repo_root=fixture.repo,
            destination_repo_root=fixture.repo,
            evidence_root=fixture.evidence,
            source_workspace_root=SOURCE_WORKSPACE,
        )


def test_cli_reports_committed_receipt(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    command = [
        sys.executable,
        "scripts/relocate_slice_package.py",
        str(fixture.package),
        "--candidate",
        CID,
        "--source-package-root",
        SOURCE_PACKAGE,
        "--destination-package-root",
        str(fixture.package),
        "--source-repo-root",
        SOURCE_REPO,
        "--destination-repo-root",
        str(fixture.repo),
        "--evidence-root",
        str(fixture.evidence),
    ]
    completed = subprocess.run(
        command,
        cwd=Path(__file__).resolve().parents[1],
        check=True,
        capture_output=True,
        text=True,
    )
    output = json.loads(completed.stdout)
    assert output["passed"] is True
    assert output["receipt"]["status"] == "COMMITTED"


def test_cli_failure_is_nonzero_and_machine_readable(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    record = _load(fixture.record_path)
    record["unknown"] = f"{SOURCE_WORKSPACE}/not-allowed"
    fixture.record_path.write_bytes(_json_bytes(record))
    completed = subprocess.run(
        [
            sys.executable,
            "scripts/relocate_slice_package.py",
            str(fixture.package),
            "--candidate",
            CID,
            "--source-package-root",
            SOURCE_PACKAGE,
            "--destination-package-root",
            str(fixture.package),
            "--source-repo-root",
            SOURCE_REPO,
            "--destination-repo-root",
            str(fixture.repo),
        ],
        cwd=Path(__file__).resolve().parents[1],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 1
    assert json.loads(completed.stdout)["passed"] is False


def test_candidate_traversal_is_rejected(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    with pytest.raises(PackageRelocationError, match="unsafe component"):
        relocate_slice_package(
            fixture.package,
            candidate_id="../escape",
            source_package_root=SOURCE_PACKAGE,
            destination_package_root=fixture.package,
            source_repo_root=SOURCE_REPO,
            destination_repo_root=fixture.repo,
        )


def test_destination_package_root_must_match_physical_root_before_mutation(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    journal = fixture.package / f".{CID}.package-relocation.json"
    chat_target = fixture.package / f"{CID}.chat-authority.json"
    context_target = fixture.package / f"{CID}.clip-context.json"

    with pytest.raises(
        PackageRelocationError,
        match="destination package root does not match the physical package root",
    ):
        relocate_slice_package(
            fixture.package,
            candidate_id=CID,
            source_package_root=SOURCE_PACKAGE,
            destination_package_root="/opt/bilive/autoslice/out/2099-01-01/"
            f"{CID}/replacement_recuts",
            source_repo_root=SOURCE_REPO,
            destination_repo_root=fixture.repo,
            evidence_root=fixture.evidence,
            source_workspace_root=SOURCE_WORKSPACE,
        )

    assert not journal.exists()
    assert not chat_target.exists()
    assert not context_target.exists()


def test_destination_candidate_root_must_be_physical_package_parent(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    other_evidence = tmp_path / "other-candidate"
    other_evidence.mkdir()

    with pytest.raises(
        PackageRelocationError,
        match="destination candidate root does not match",
    ):
        relocate_slice_package(
            fixture.package,
            candidate_id=CID,
            source_package_root=SOURCE_PACKAGE,
            destination_package_root=fixture.package,
            source_repo_root=SOURCE_REPO,
            destination_repo_root=fixture.repo,
            evidence_root=other_evidence,
            source_workspace_root=SOURCE_WORKSPACE,
        )

    assert not (fixture.package / f".{CID}.package-relocation.json").exists()


def test_equal_length_cross_role_root_collision_is_rejected(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    profile = fixture.repo / "assets/lidousha/profile.json"
    duplicate = fixture.package / "assets/lidousha/profile.json"
    duplicate.parent.mkdir(parents=True)
    duplicate.write_bytes(profile.read_bytes())
    override = fixture.repo / "assets/lidousha/override.json"
    duplicate_override = fixture.package / "assets/lidousha/override.json"
    duplicate_override.write_bytes(override.read_bytes())

    with pytest.raises(PackageRelocationError, match="ambiguous relocation root"):
        relocate_slice_package(
            fixture.package,
            candidate_id=CID,
            source_package_root=SOURCE_PACKAGE,
            destination_package_root=fixture.package,
            source_repo_root=SOURCE_REPO,
            destination_repo_root=fixture.package,
            evidence_root=fixture.evidence,
            source_workspace_root=SOURCE_WORKSPACE,
        )


def test_existing_evidence_symlink_never_overwrites_target(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    outside = tmp_path / "outside-chat"
    outside.write_bytes(b"outside-canary")
    target = fixture.package / f"{CID}.chat-authority.json"
    target.symlink_to(outside)

    with pytest.raises(PackageRelocationError, match="unsafe package target"):
        fixture.relocate()

    assert outside.read_bytes() == b"outside-canary"
