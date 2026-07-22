import hashlib
import json
from pathlib import Path

from scripts.apply_subtitle_text_overrides import apply_document
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


def test_final_recut_rebases_timeline_bound_text_override(
    tmp_path: Path,
) -> None:
    padded = tmp_path / "padded.mp4"
    padded.write_bytes(b"padded")
    (tmp_path / "out").mkdir()
    padded_provenance = tmp_path / "padded.provenance.json"
    padded_provenance.write_text("{}\n", encoding="utf-8")
    override = (
        Path(__file__).resolve().parents[1]
        / "assets/lidousha/subtitle_text_overrides/auto_225942_962_980.text.v1.json"
    )

    def run_command(command: list[str], **_kwargs) -> None:
        Path(command[-1]).write_bytes(b"recut")

    def write_source_range_srt(
        _cues, _start_ms: int, _end_ms: int, output: Path
    ) -> None:
        output.write_text(
            "1\n00:00:04,770 --> 00:00:07,970\n让李豆沙线下叫停了时\n",
            encoding="utf-8",
        )

    def unused(*_args, **_kwargs):
        raise AssertionError("unrelated finalization adapter was called")

    adapters = finalization.ProducerFinalizationAdapters(
        accurate_recut_command=lambda **kwargs: ["recut", str(kwargs["output_media"])],
        run_command=run_command,
        write_source_range_srt=write_source_range_srt,
        apply_text_override_document=apply_document,
        run_speaker_finalization=unused,
        burn_preview_subtitles=unused,
        stage_publish_draft=unused,
        generate_upload_tags=unused,
        delivery_root=lambda: tmp_path / "delivery",
    )

    recut = finalization._materialize_final_recut(
        spec={"pieces": [{"start_ms": 952_920}]},
        cid="auto_225942_962_980",
        out_root=tmp_path / "out",
        padded=padded,
        padded_provenance_path=padded_provenance,
        piece_provenance_rows=[],
        final_start=9_770,
        final_end=32_840,
        sanitized=[],
        timing_qa={},
        text_override_path=override,
        adapters=adapters,
    )

    assert "让李豆沙线下叫kmx" in recut.subtitle_path.read_text(encoding="utf-8")
    assert recut.text_manifest is not None
    assert recut.text_manifest["source_timeline_offset_ms"] == 9_770


def test_final_recut_applies_hash_bound_redelivery_baseline_outside_truth(
    tmp_path: Path,
) -> None:
    padded = tmp_path / "padded.mp4"
    padded.write_bytes(b"padded")
    (tmp_path / "out").mkdir()
    padded_provenance = tmp_path / "padded.provenance.json"
    padded_provenance.write_text("{}\n", encoding="utf-8")
    baseline = tmp_path / "prior-delivery.srt"
    baseline.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n旧审定第一句\n\n"
        "2\n00:00:01,000 --> 00:00:02,000\n旧错误第二句\n",
        encoding="utf-8",
    )

    def run_command(command: list[str], **_kwargs) -> None:
        Path(command[-1]).write_bytes(b"recut")

    def write_source_range_srt(
        _cues, _start_ms: int, _end_ms: int, output: Path
    ) -> None:
        output.write_text(
            "1\n00:00:00,020 --> 00:00:01,020\n随机漂移第一句\n\n"
            "2\n00:00:01,020 --> 00:00:02,020\nIvan新源真值\n",
            encoding="utf-8",
        )

    def unused(*_args, **_kwargs):
        raise AssertionError("unrelated finalization adapter was called")

    adapters = finalization.ProducerFinalizationAdapters(
        accurate_recut_command=lambda **kwargs: [
            "recut",
            str(kwargs["output_media"]),
        ],
        run_command=run_command,
        write_source_range_srt=write_source_range_srt,
        apply_text_override_document=unused,
        run_speaker_finalization=unused,
        burn_preview_subtitles=unused,
        stage_publish_draft=unused,
        generate_upload_tags=unused,
        delivery_root=lambda: tmp_path / "delivery",
    )
    chat_audit = {
        "source_subtitle_truth_audit": {
            "status": "APPLIED",
            "applied": [
                {
                    # Padded timeline; final_start=1000 rebases this to the
                    # second delivery cue.
                    "local_windows": [{"start_ms": 2_000, "end_ms": 3_000}]
                }
            ],
            "satisfied": [],
        }
    }
    spec = {
        "pieces": [{"start_ms": 100_000}],
        "subtitle_redelivery_baseline": {
            "schema_version": "subtitle-redelivery-baseline.v1",
            "mode": "preserve_text_outside_source_truth",
            "path": str(baseline),
            "sha256": hashlib.sha256(baseline.read_bytes()).hexdigest(),
            "authority": "previous reviewed delivery",
        },
    }

    recut = finalization._materialize_final_recut(
        spec=spec,
        cid="candidate",
        out_root=tmp_path / "out",
        padded=padded,
        padded_provenance_path=padded_provenance,
        piece_provenance_rows=[],
        final_start=1_000,
        final_end=3_020,
        sanitized=[],
        timing_qa={},
        text_override_path=None,
        adapters=adapters,
        spec_parent=tmp_path,
        chat_authority_audit=chat_audit,
    )

    output = recut.subtitle_path.read_text(encoding="utf-8")
    assert "00:00:00,020 --> 00:00:01,020\n旧审定第一句" in output
    assert "00:00:01,020 --> 00:00:02,020\nIvan新源真值" in output
    assert "旧错误第二句" not in output
    assert recut.redelivery_baseline_audit is not None
    assert recut.redelivery_baseline_audit["status"] == "APPLIED"
    assert recut.redelivery_baseline_audit_path is not None
    assert recut.redelivery_baseline_audit_path.is_file()
