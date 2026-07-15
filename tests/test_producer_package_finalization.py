import json
from pathlib import Path

from src.autoslice import producer_package_finalization as finalization


def test_delivery_summary_uses_persisted_boundary_audit(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    burned = tmp_path / "burned.mp4"
    burned.write_bytes(b"burned")
    subtitle = tmp_path / "final.srt"
    subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\n完成\n", encoding="utf-8")
    record_path = tmp_path / "record.json"
    record_path.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(
        finalization, "_validated_burned_artifact", lambda _record: burned
    )

    copy_commands: list[list[str]] = []

    def unused(*_args, **_kwargs):
        raise AssertionError("unrelated finalization adapter was called")

    adapters = finalization.ProducerFinalizationAdapters(
        accurate_recut_command=unused,
        run_command=lambda command, **_kwargs: copy_commands.append(command),
        write_source_range_srt=unused,
        apply_text_override_document=unused,
        run_speaker_finalization=unused,
        burn_preview_subtitles=unused,
        stage_publish_draft=unused,
        generate_upload_tags=unused,
        delivery_root=lambda: tmp_path / "delivery",
    )
    recut = finalization.FinalRecutArtifacts(
        recut_dir=tmp_path,
        media_path=tmp_path / "recut.mp4",
        subtitle_path=subtitle,
        text_manifest_path=None,
        text_manifest=None,
    )
    speaker = finalization.SpeakerArtifacts(None, None, None, None)
    authority = finalization.AuthorityArtifacts(None, None)
    staged = finalization.StagedRecord(
        record={}, staging={"title": "标题"}, record_path=record_path
    )
    audit = {
        "closure_sentence": "这是落点",
        "verdict": "ok_sentence_boundary_cut",
        "red_flags": [],
        "boundary_repairs": [{"reason": "tail_clamped"}],
    }

    result = finalization._deliver_staged_record(
        spec={"date": "2026-07-15", "delivery_name": "成品"},
        cid="candidate-1",
        final_end=12_345,
        audit=audit,
        timing_qa={"counts": {"output": 1}},
        recut=recut,
        speaker=speaker,
        authority=authority,
        chat_authority_path=tmp_path / "chat-authority.json",
        staged=staged,
        adapters=adapters,
    )

    assert result == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["closure_sentence"] == "这是落点"
    assert summary["red_flags"] == []
    assert summary["boundary_repairs"] == [{"reason": "tail_clamped"}]
    assert len(copy_commands) == 3
