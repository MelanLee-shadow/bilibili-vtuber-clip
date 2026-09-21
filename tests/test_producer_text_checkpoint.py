"""Recover real post-correction bytes; never turn a checkpoint into approval.

Synthetic native-stage failures reproduce a real missing-SRT incident without
sending audio or text to a provider. The production release gates stay intact.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import importlib
import json

import pytest

from src.autoslice import producer_text_pipeline as pipeline

TEXT_KEY = "post_transcript_entity_output_srt"
SHA_KEY = "post_transcript_entity_output_srt_sha256"


def _srt(*texts):
    return "\n\n".join(
        f"{i}\n00:00:{2*i:02d},000 --> 00:00:{2*i+1:02d},000\n{text}"
        for i, text in enumerate(texts, 1)
    ) + "\n"


def _stage(tmp_path, monkeypatch, *, blocked):
    # An earlier CPA sidecar must not be mistaken for this post-correction text.
    earlier = _srt("这是早一轮的开场", "you们知道苹果要出，呃，you", "这里结束了")
    current = earlier.replace("早一轮", "当前一轮")
    (tmp_path / "earlier.cpa-reviewed.srt").write_text(earlier)
    chat = {
        "status": "PASS",
        "source_truth_preview_receipts": {
            stage: {"schema_version": "source-truth-deterministic-preview.v1", "ledger_sha256": None}
            for stage in ("pre_entity_arbitration", "pre_correction_review")
        },
    }
    if not blocked:
        current = current.replace("you们知道苹果要出，呃，you", "今天讲的是普通话题")
    correction = {"schema_version": "final-review-audit.v1", "status": "PARTIAL", "findings": [], "applied_count": 0}
    args = dict(
        spec={"pieces": []}, durations=[], srt_text=current,
        chat_authority_audit=chat, transcript_entity_audit={"status": "PASS"},
        referent_groups=[], final_review_audit=correction, song_name_candidates=[],
        known_songs_path=tmp_path / "known-songs.json", session_topic_authorities=(),
        source_language_witness_srt=current, text_override_path=None,
        source_truth_ledger_path=None, out_root=tmp_path, cid="synthetic_checkpoint",
        padded=None,  # No provider can run in this stage-only fixture.
    )
    if blocked:
        with pytest.raises(SystemExit, match="FOREIGN_SOURCE_TRANSCRIPTION_REQUIRED"):
            pipeline._finalize_text_evidence(**args)
        assert not (tmp_path / "padded.fresh.srt").exists()
    else:
        output = pipeline._finalize_text_evidence(**args)
        assert output.srt_text == current
    stored = json.loads((tmp_path / "synthetic_checkpoint.chat-authority.json").read_text())
    assert stored[SHA_KEY] == hashlib.sha256(current.encode()).hexdigest()
    assert stored[TEXT_KEY] == current
    assert stored["final_review_audit"] == correction
    assert (tmp_path / "earlier.cpa-reviewed.srt").read_text() == earlier
    return stored, current


def test_native_block_persists_the_actual_current_text_not_only_its_hash(tmp_path, monkeypatch):
    stored, _ = _stage(tmp_path, monkeypatch, blocked=True)
    assert stored["foreign_script_consistency_audit"]["status"] == "BLOCKED_MIXED_CJK_LATIN_PHRASE"


def test_success_retains_the_same_stage_snapshot_without_changing_output(tmp_path, monkeypatch):
    _stage(tmp_path, monkeypatch, blocked=False)


def _module():
    return importlib.import_module("src.autoslice.producer_text_checkpoint")


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_snapshot_roundtrip_preserves_exact_string_and_other_fields(newline):
    module = _module()
    text = _srt("这是测试").replace("\n", newline)
    audit = {"status": "PARTIAL", "release_gate": "BLOCK", "findings": [{"cue": 1}]}
    original = deepcopy(audit)
    module.retain_post_transcript_text(audit, text)
    assert module.load_post_transcript_text(audit) == text
    assert {k: audit[k] for k in original} == original
    assert set(audit) == set(original) | {TEXT_KEY, SHA_KEY}


def test_old_hash_only_receipt_is_not_silently_upgraded():
    audit = {SHA_KEY: "a" * 64, "status": "PARTIAL"}
    before = deepcopy(audit)
    assert _module().load_post_transcript_text(audit) is None
    assert audit == before


@pytest.mark.parametrize("edit", [
    {TEXT_KEY: None}, {TEXT_KEY: 17}, {SHA_KEY: None}, {SHA_KEY: "a" * 64},
    {TEXT_KEY: "a different transcript"}, {SHA_KEY: "sha256:" + "a" * 64},
])
def test_tampered_or_malformed_snapshot_is_rejected(edit):
    module = _module()
    audit = {}
    module.retain_post_transcript_text(audit, _srt("当前中间稿"))
    audit.update(edit)
    with pytest.raises(ValueError, match="POST_TRANSCRIPT_TEXT_CHECKPOINT_INVALID"):
        module.load_post_transcript_text(audit)


def test_subsequent_final_text_does_not_relabel_the_intermediate_checkpoint():
    module = _module()
    audit = {"final_output_srt_sha256": "b" * 64, "status": "PARTIAL"}
    text = _srt("更早的中间稿")
    module.retain_post_transcript_text(audit, text)
    assert module.load_post_transcript_text(audit) == text
    assert audit["final_output_srt_sha256"] == "b" * 64
    assert audit["status"] == "PARTIAL"
