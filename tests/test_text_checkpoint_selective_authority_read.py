"""Checkpoint writers bind a few authority fields without cold-reading the whole document.

闺蜜 rerun (c0a3b2f0c): after FINAL_REVIEW_RELEASE_BLOCKED the producer
spent 50 minutes and >12 GB expanding a 386 MB compact chat authority inside
``write_current_text_checkpoint`` until the kernel OOM killer ended it.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from src.autoslice import argon_escalation_storage as store
from src.autoslice import producer_text_checkpoint as checkpoint
from src.autoslice.review_evidence_store import REF_SCHEMA, compact_review, persist_document, review_from_bytes

SRT = "1\n00:00:01,000 --> 00:00:03,000\n保留原文\n"
DIGEST = hashlib.sha256(SRT.encode()).hexdigest()


def authority(*, big):
    review = {
        "schema_version": "final-review-audit.v2", "reviewed_srt_sha256": "sha256:" + DIGEST,
        "release_gate": "BLOCK", "findings": [{
            "cue_index": 1, "exact_release_adjudication": {
                "request": {"whole_clip_current_srt": SRT, "padding": big},
                "stopped_adjudication": {"terminal_review": {"raw_completion": big}}}}],
    }
    # Many small pass receipts: the receipt as a whole exceeds the publisher's
    # inline limit while no single child does, so a successor-format writer
    # stores the whole field by reference (one blob the checkpoint must read).
    passes = [{"schema_version": "exact-final-cpa-self-heal-pass.v1", "pass_index": index,
               "input_srt_sha256": "sha256:" + f"{index:064x}", "output_srt_sha256": "sha256:" + f"{index + 1:064x}",
               "repairs": []} for index in range(1, 61)]
    # Many small direct fields make the receipt dict itself exceed the limit too.
    notes = {f"receipt_note_{index}": "n" * 100 for index in range(120)}
    return {"final_output_srt_sha256": DIGEST, "post_transcript_entity_output_srt": SRT,
            "post_transcript_entity_output_srt_sha256": DIGEST, "final_review_audit": review,
            "exact_final_cpa_self_heal": {"status": "REVIEW_PENDING", "passes": passes,
                                          "final_srt_sha256": "sha256:" + DIGEST, **notes}}


def references(value, found=None):
    found = [] if found is None else found
    if isinstance(value, dict):
        if value.get("schema_version") == REF_SCHEMA:
            found.append(value["sha256"])
        else:
            for item in value.values():
                references(item, found)
    elif isinstance(value, list):
        for item in value:
            references(item, found)
    return found


@pytest.fixture
def scene(tmp_path, monkeypatch):
    srt_path = tmp_path / "input.srt"
    srt_path.write_text(SRT, encoding="utf-8")
    padded = tmp_path / "padded.mp4"
    padded.write_bytes(b"synthetic media")
    context_path = tmp_path / "context.json"
    context_path.write_text("{}\n", encoding="utf-8")
    reads = []
    real = store.get_blob

    def counting(root, digest):
        raw = real(root, digest)  # only blobs actually delivered count as reads
        reads.append(digest)
        return raw

    monkeypatch.setattr(store, "get_blob", counting)
    return {"root": tmp_path, "srt": srt_path, "padded": padded, "context": context_path, "reads": reads}


def write_both(scene, authority_path):
    checkpoint.write_source_text_checkpoint(
        scene["root"] / "source-checkpoint.json", candidate_id="candidate", srt_path=scene["srt"],
        authority_path=authority_path, context_path=scene["context"], padded=scene["padded"])
    checkpoint.write_current_text_checkpoint(
        scene["root"] / "current-checkpoint.json", candidate_id="candidate", srt_path=scene["srt"],
        authority_path=authority_path, context_path=scene["context"], padded=scene["padded"], source_clock={})


def test_checkpoint_writers_never_expand_review_evidence_they_do_not_bind(scene):
    authority_path = scene["root"] / "authority.json"
    persist_document(authority_path, authority(big="x" * 20_000))
    compact = json.loads(authority_path.read_text())
    finding_blobs = references(compact["final_review_audit"]["findings"])
    assert finding_blobs, "the finding's evidence must be stored by reference for this scene"
    # The review evidence is unreadable (huge, missing or corrupt): a whole-document
    # cold read fails here, which is where the producer died.
    for digest in finding_blobs:
        (scene["root"] / "evidence-store" / "blobs" / f"{digest}.bin").unlink()
    with pytest.raises(Exception):
        review_from_bytes(authority_path.read_bytes(), directory=authority_path.parent)
    scene["reads"].clear()

    write_both(scene, authority_path)

    assert not set(scene["reads"]) & set(finding_blobs)
    current = json.loads((scene["root"] / "current-checkpoint.json").read_text())
    assert current["srt"]["sha256"] == "sha256:" + DIGEST
    assert current["authority"]["sha256"] == "sha256:" + hashlib.sha256(authority_path.read_bytes()).hexdigest()
    assert current["release_authorized"] is False
    source = json.loads((scene["root"] / "source-checkpoint.json").read_text())
    assert source["srt"]["sha256"] == "sha256:" + DIGEST and source["release_authorized"] is False


def test_bound_fields_stored_by_reference_are_still_resolved_and_checked(scene):
    # Every level stored by reference (the successor publisher's shape): the
    # self-heal receipt, the retained transcript and the review are blobs.
    document = authority(big="y" * 20_000)
    document["final_output_srt_sha256"] = "0" * 64  # only the pending receipt binds these bytes
    document["post_transcript_entity_output_srt"] = SRT + "\n" * 9_000
    document["post_transcript_entity_output_srt_sha256"] = hashlib.sha256(
        document["post_transcript_entity_output_srt"].encode()).hexdigest()
    compact = compact_review(document, scene["root"])
    assert compact["exact_final_cpa_self_heal"].get("schema_version") == REF_SCHEMA
    assert compact["post_transcript_entity_output_srt"].get("schema_version") == REF_SCHEMA
    authority_path = scene["root"] / "authority.json"
    authority_path.write_text(json.dumps(compact, ensure_ascii=False, sort_keys=True))

    checkpoint.write_current_text_checkpoint(
        scene["root"] / "current-checkpoint.json", candidate_id="candidate", srt_path=scene["srt"],
        authority_path=authority_path, context_path=scene["context"], padded=scene["padded"], source_clock={})
    assert compact["exact_final_cpa_self_heal"]["sha256"] in scene["reads"]
    assert set(scene["reads"]).isdisjoint(references(compact["final_review_audit"]["findings"]))

    # The source writer binds final_output_srt_sha256 (here deliberately foreign)
    # and the retained transcript: the drift check keeps firing after resolution.
    with pytest.raises(ValueError, match="SOURCE_TEXT_CHECKPOINT_SRT_AUTHORITY_DRIFT"):
        checkpoint.write_source_text_checkpoint(
            scene["root"] / "source-checkpoint.json", candidate_id="candidate", srt_path=scene["srt"],
            authority_path=authority_path, context_path=scene["context"], padded=scene["padded"])
    # And the current writer still refuses bytes that neither receipt binds.
    other = scene["root"] / "other.srt"
    other.write_text(SRT.replace("保留原文", "改过的字"), encoding="utf-8")
    with pytest.raises(ValueError, match="CURRENT_TEXT_CHECKPOINT_SRT_AUTHORITY_DRIFT"):
        checkpoint.write_current_text_checkpoint(
            scene["root"] / "current-checkpoint-2.json", candidate_id="candidate", srt_path=other,
            authority_path=authority_path, context_path=scene["context"], padded=scene["padded"], source_clock={})


def test_a_corrupt_bound_field_is_still_rejected(scene):
    document = authority(big="z" * 20_000)
    compact = compact_review(document, scene["root"])
    reference = compact["exact_final_cpa_self_heal"]
    blob = scene["root"] / "evidence-store" / "blobs" / f"{reference['sha256']}.bin"
    blob.write_bytes(blob.read_bytes() + b" ")
    authority_path = scene["root"] / "authority.json"
    authority_path.write_text(json.dumps(compact, ensure_ascii=False, sort_keys=True))
    with pytest.raises(Exception):
        checkpoint.write_current_text_checkpoint(
            scene["root"] / "current-checkpoint.json", candidate_id="candidate", srt_path=scene["srt"],
            authority_path=authority_path, context_path=scene["context"], padded=scene["padded"], source_clock={})
