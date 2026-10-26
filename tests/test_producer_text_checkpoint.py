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


def test_source_checkpoint_binds_final_and_earlier_text_separately(tmp_path):
    module = _module()
    earlier = _srt("清嗓误识别", "喝口水")
    final = _srt("喝口水")
    audit = {"final_output_srt_sha256": hashlib.sha256(final.encode()).hexdigest()}
    module.retain_post_transcript_text(audit, earlier)
    srt_path, authority, context, padded = [tmp_path / name for name in
                                          ("current.srt", "audit.json", "context.json", "padded.mp4")]
    srt_path.write_text(final)
    authority.write_text(json.dumps(audit))
    context.write_text("{}")
    padded.write_bytes(b"bound media")
    checkpoint = tmp_path / "checkpoint.json"
    module.write_source_text_checkpoint(checkpoint, candidate_id="candidate", srt_path=srt_path,
                                        authority_path=authority, context_path=context, padded=padded)
    saved = json.loads(checkpoint.read_text())
    from pathlib import Path
    frozen = json.loads(Path(saved["authority"]["path"]).read_text())
    assert frozen == audit
    assert module.load_post_transcript_text(frozen) == earlier
    assert Path(saved["srt"]["path"]).read_text() == final
    assert saved["release_authorized"] is False
    for field, value in [("final_output_srt_sha256", "b" * 64), (TEXT_KEY, earlier + "drift")]:
        changed = audit | {field: value}
        authority.write_text(json.dumps(changed))
        with pytest.raises(ValueError, match="CHECKPOINT.*(DRIFT|INVALID)"):
            module.write_source_text_checkpoint(checkpoint, candidate_id="candidate", srt_path=srt_path,
                                                authority_path=authority, context_path=context, padded=padded)


def test_source_checkpoint_freezes_wider_boundary_separately_from_display(tmp_path):
    from src.autoslice.boundary_semantic_review import cue_grid_sha256
    from src.autoslice.jingting_chunker import parse_srt_cues
    module = _module()
    display = _srt('当前正文')
    wider = display + '\n2\n00:00:03,000 --> 00:00:04,000\n下一话题\n'
    path = tmp_path/'source-boundary-context.srt';path.write_text(wider)
    grid = cue_grid_sha256(parse_srt_cues(wider))
    audit = {'final_output_srt_sha256':hashlib.sha256(display.encode()).hexdigest(),
        'final_review_audit':{'source_boundary_context':{
            'schema_version':'source-boundary-transcript-context.v1','role':'SOURCE_DOMAIN_BOUNDARY_REVIEW_CONTEXT',
            'source_srt_path':str(path),'source_srt_sha256':'sha256:' + hashlib.sha256(wider.encode()).hexdigest(),
            'source_cue_grid_sha256':grid},'boundary_semantic_review':{'review_scope':'source_full_window','cue_grid_sha256':grid}}}
    module.retain_post_transcript_text(audit,display)
    srt,authority,context,padded=[tmp_path/n for n in ('display.srt','a.json','ctx.json','padded.mp4')]
    srt.write_text(display);authority.write_text(json.dumps(audit));context.write_text('{}');padded.write_bytes(b'media')
    checkpoint=tmp_path/'source.json'
    kwargs=dict(candidate_id='candidate',srt_path=srt,authority_path=authority,context_path=context,padded=padded)
    module.write_source_text_checkpoint(checkpoint,**kwargs)
    saved=json.loads(checkpoint.read_text())
    from pathlib import Path
    assert Path(saved['srt']['path']).read_text()==display
    assert Path(saved['boundary_srt']['path']).read_text()==wider
    path.write_text(wider.replace('下一话题','篡改'))
    assert Path(saved['boundary_srt']['path']).read_text()==wider
    with pytest.raises(ValueError,match='BOUNDARY_DRIFT'):
        module.write_source_text_checkpoint(checkpoint,**kwargs)


def test_interrupted_frozen_copy_does_not_poison_the_hash_target(tmp_path, monkeypatch):
    from src.autoslice import package_import

    source = tmp_path / "current.srt"
    source.write_text(_srt("已完成的当前稿"))
    real_replace = package_import.os.replace

    def interrupt_before_commit(_temporary, _target):
        raise KeyboardInterrupt("INJECTED_AFTER_TEMP_WRITE_BEFORE_ATOMIC_REPLACE")

    monkeypatch.setattr(package_import.os, "replace", interrupt_before_commit)
    root = tmp_path / "frozen"
    with pytest.raises(KeyboardInterrupt):
        _module()._frozen_binding(root, "srt", source)
    assert not list(root.glob("srt.*"))
    monkeypatch.setattr(package_import.os, "replace", real_replace)
    binding = _module()._frozen_binding(root, "srt", source)
    from pathlib import Path

    target = Path(binding["path"])
    assert target.read_bytes() == source.read_bytes()
    target.write_text("real drift")
    with pytest.raises(ValueError, match="ARTIFACT_DRIFT"):
        _module()._frozen_binding(root, "srt", source)
    assert target.read_text() == "real drift"
