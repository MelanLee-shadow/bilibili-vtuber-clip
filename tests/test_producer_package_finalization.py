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
        "2\n00:00:01,000 --> 00:00:02,000\n旧错误第二句\n\n"
        "3\n00:00:02,000 --> 00:00:03,000\n旧静音幻听\n",
        encoding="utf-8",
    )
    ledger = tmp_path / "subtitle-truth.json"
    ledger.write_text(
        json.dumps(
            {
                "schema_version": "source-subtitle-truth-ledger.v1",
                "entries": [
                    {
                        "knowledge_type": "SOURCE_INTERVAL_TRUTH",
                        "truth_id": "new-reviewed-line",
                        "recording_basename": "recording.mp4",
                        "source_start_ms": 102_020,
                        "source_end_ms": 103_020,
                        "action": "replace_cue",
                        "text": "Ivan新源真值",
                        "authority": "newer source review",
                        "required": True,
                    },
                    {
                        "knowledge_type": "SOURCE_INTERVAL_TRUTH",
                        "truth_id": "drop-silent-cue",
                        "recording_basename": "recording.mp4",
                        "source_start_ms": 103_020,
                        "source_end_ms": 104_020,
                        "action": "drop_cue",
                        "authority": "reviewed silence",
                        "required": True,
                    }
                ],
            },
            ensure_ascii=False,
        )
        + "\n",
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
            "ledger_path": str(ledger),
            "applied": [
                {
                    "action": "replace_cue",
                    # Padded timeline; final_start=1000 rebases this to the
                    # second delivery cue.
                    "local_windows": [
                        {"start_ms": 2_020, "end_ms": 3_020}
                    ],
                },
                {
                    "action": "drop_cue",
                    "local_windows": [
                        {"start_ms": 3_020, "end_ms": 4_020}
                    ],
                }
            ],
            "satisfied": [],
        }
    }
    spec = {
        "pieces": [
            {
                "remote_media": "/recordings/recording.mp4",
                "start_ms": 100_000,
                "end_ms": 104_020,
            }
        ],
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
        final_end=4_020,
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
    assert "旧静音幻听" not in output
    assert recut.redelivery_baseline_audit is not None
    assert recut.redelivery_baseline_audit["status"] == "APPLIED"
    assert recut.redelivery_baseline_audit["source_truth_reapplication"][
        "status"
    ] == "APPLIED"
    assert recut.redelivery_baseline_audit["protected_intervals"] == [
        {"start_ms": 2_020, "end_ms": 3_020}
    ]
    assert recut.redelivery_baseline_audit_path is not None
    assert recut.redelivery_baseline_audit_path.is_file()


def test_final_recut_replays_truth_after_broad_window_was_satisfied(
    tmp_path: Path,
) -> None:
    """One canonical mention must not hide a wrong sibling in the same window."""

    padded = tmp_path / "padded.mp4"
    padded.write_bytes(b"padded")
    (tmp_path / "out").mkdir()
    padded_provenance = tmp_path / "padded.provenance.json"
    padded_provenance.write_text("{}\n", encoding="utf-8")
    baseline = tmp_path / "prior-delivery.srt"
    baseline.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n旧审定第一句\n\n"
        "2\n00:00:01,000 --> 00:00:02,000\n就灰神，好像是灰神吧\n\n"
        "3\n00:00:02,000 --> 00:00:03,000\n灰神说救救李姐\n",
        encoding="utf-8",
    )
    ledger = tmp_path / "subtitle-truth.json"
    ledger.write_text(
        json.dumps(
            {
                "schema_version": "source-subtitle-truth-ledger.v1",
                "entries": [
                    {
                        "knowledge_type": "SOURCE_INTERVAL_TRUTH",
                        "truth_id": "entity-spelling",
                        "recording_basename": "recording.mp4",
                        "source_start_ms": 102_000,
                        "source_end_ms": 104_000,
                        "action": "replace_substring",
                        "replacements": [
                            {"surface": "灰神", "canonical": "毁神"}
                        ],
                        "required_text": "毁神",
                        "authority": "reviewed spoken-name spelling",
                        "required": True,
                    }
                ],
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    def run_command(command: list[str], **_kwargs) -> None:
        Path(command[-1]).write_bytes(b"recut")

    def write_source_range_srt(
        _cues, _start_ms: int, _end_ms: int, output: Path
    ) -> None:
        output.write_text(
            "1\n00:00:00,000 --> 00:00:01,000\n随机漂移第一句\n\n"
            "2\n00:00:01,000 --> 00:00:02,000\n就毁神，好像是毁神吧\n\n"
            "3\n00:00:02,000 --> 00:00:03,000\n鼠神说救救李姐\n",
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
            "schema_version": "source-subtitle-truth-audit.v1",
            "status": "ALREADY_SATISFIED",
            "ledger_path": str(ledger),
            "applied": [],
            "satisfied": [
                {
                    "truth_id": "entity-spelling",
                    "action": "replace_substring",
                    "local_windows": [
                        {"start_ms": 2_000, "end_ms": 4_000}
                    ],
                }
            ],
            "failures": [],
        }
    }
    spec = {
        "pieces": [
            {
                "remote_media": "/recordings/recording.mp4",
                "start_ms": 100_000,
                "end_ms": 104_000,
            }
        ],
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
        final_end=4_000,
        sanitized=[],
        timing_qa={},
        text_override_path=None,
        adapters=adapters,
        spec_parent=tmp_path,
        chat_authority_audit=chat_audit,
    )

    output = recut.subtitle_path.read_text(encoding="utf-8")
    assert "旧审定第一句" in output
    assert "就毁神，好像是毁神吧" in output
    assert "毁神说救救李姐" in output
    assert "鼠神" not in output
    assert "灰神" not in output
    assert chat_audit["source_subtitle_truth_audit"]["status"] == "APPLIED"
    assert chat_audit["source_subtitle_truth_audit"]["applied"][0][
        "local_windows"
    ] == [{"start_ms": 2_000, "end_ms": 4_000}]
    assert chat_audit["source_subtitle_truth_post_redelivery_audit"][
        "timeline_basis"
    ] == "final_delivery"
    assert recut.redelivery_baseline_audit is not None
    assert recut.redelivery_baseline_audit["source_truth_reapplication"][
        "status"
    ] == "APPLIED"


def test_redelivery_baseline_resolves_actual_unproven_foreign_shape() -> None:
    source_language_audit = {
        "status": "DEFERRED_TO_REDELIVERY_BASELINE",
        "unproven_foreign_introductions": [
            {
                "cue_index": 37,
                "start_ms": 85_220,
                "end_ms": 87_900,
                "draft": "分牙三四关就毁神",
                "attempted": "非常やさしい，就病院坂灵",
            }
        ],
    }
    baseline_audit = {
        "status": "APPLIED",
        "failures": [],
        "mappings": [
            {
                "current_cue_index": 34,
                "start_ms": 75_220,
                "end_ms": 77_900,
            }
        ],
    }
    final_text = (
        "1\n00:01:15,220 --> 00:01:17,900\n分牙三四关就毁神——\n"
    )

    resolved = (
        finalization._resolve_deferred_foreign_introductions_after_redelivery(
            source_language_audit=source_language_audit,
            final_text=final_text,
            baseline_audit=baseline_audit,
            final_start=10_000,
        )
    )

    assert resolved
    assert source_language_audit["status"] == (
        "RESOLVED_BY_REDELIVERY_BASELINE"
    )
    assert source_language_audit["deferred_resolution"]["status"] == "PASS"


def test_redelivery_baseline_keeps_block_when_foreign_surface_survives() -> None:
    source_language_audit = {
        "status": "DEFERRED_TO_REDELIVERY_BASELINE",
        "unproven_foreign_introductions": [
            {
                "cue_index": 37,
                "start_ms": 85_220,
                "end_ms": 87_900,
                "attempted": "非常やさしい，就病院坂灵",
            }
        ],
    }
    baseline_audit = {
        "status": "APPLIED",
        "failures": [],
        "mappings": [
            {
                "current_cue_index": 34,
                "start_ms": 75_220,
                "end_ms": 77_900,
            }
        ],
    }
    final_text = (
        "1\n00:01:15,220 --> 00:01:17,900\n仍然非常やさしい\n"
    )

    resolved = (
        finalization._resolve_deferred_foreign_introductions_after_redelivery(
            source_language_audit=source_language_audit,
            final_text=final_text,
            baseline_audit=baseline_audit,
            final_start=10_000,
        )
    )

    assert not resolved
    assert source_language_audit["status"] == (
        "BLOCKED_REDELIVERY_BASELINE_DID_NOT_RESOLVE_FOREIGN_INTRODUCTION"
    )
