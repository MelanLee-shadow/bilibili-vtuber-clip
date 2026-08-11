import hashlib
import json
from pathlib import Path

import pytest

from scripts.apply_subtitle_text_overrides import parse_srt
from scripts.materialize_reviewed_speaker_truth_delivery import (
    ARBITRATION_NOT_REQUIRED,
    ARBITRATION_SCHEMA,
    DeliveryCompileError,
    REVIEW_STATUS_COMPLETE,
    REVIEW_STATUS_PARTIAL_MACHINE,
    compile_delivery,
)
from src.autoslice.reviewed_speaker_baseline import load_reviewed_speaker_baseline


CANDIDATE = "synthetic_delivery"
AUTHORITY = "Ivan synthetic delivery authority"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fixture(tmp_path: Path) -> dict[str, object]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    source_srt = tmp_path / "source.srt"
    source_srt.write_text(
        """1
00:00:00,000 --> 00:00:01,500
主播一

2
00:00:01,500 --> 00:00:02,200
原二

3
00:00:02,200 --> 00:00:03,000
原三

4
00:00:03,000 --> 00:00:04,000
可能听见

5
00:00:04,000 --> 00:00:05,500
主播五
""",
        encoding="utf-8",
    )
    media = tmp_path / "media.mp4"
    media.write_bytes(b"synthetic media")
    automatic_srt = tmp_path / "assets" / "automatic-labelled.srt"
    automatic_srt.parent.mkdir()
    automatic_srt.write_text(
        """1
00:00:00,000 --> 00:00:01,500
[李豆沙] 主播一

2
00:00:01,500 --> 00:00:02,900
[连线] 嘉宾说 主播答(跃起)

3
00:00:03,000 --> 00:00:04,000
[连线] 可能听见

4
00:00:04,000 --> 00:00:05,500
[李豆沙] 主播五
""",
        encoding="utf-8",
    )
    truth = {
        "schema": "ivan-speaker-truth-diff.v2",
        "candidate_id": CANDIDATE,
        "source_machine_sha256": _sha256(source_srt),
        "authority": AUTHORITY,
        "cues": [
            {
                "cue": 1,
                "timing": "00:00:00,000 --> 00:00:01,500",
                "machine_label": None,
                "machine_text": "主播一",
                "truth_segments": [{"label": "李豆沙", "text": "主播一"}],
                "truth_text": "主播一",
                "text_changed": False,
            },
            {
                "cue": 2,
                "timing": "00:00:01,500 --> 00:00:02,200",
                "annotated_timing": "00:00:01,500 --> 00:00:02,900",
                "merged_from": [2, 3],
                "merged_machine_timings": [
                    "00:00:01,500 --> 00:00:02,200",
                    "00:00:02,200 --> 00:00:03,000",
                ],
                "machine_label": None,
                "machine_text": "原二 原三",
                "truth_segments": [
                    {"label": "连线", "text": "嘉宾说"},
                    {"label": "李豆沙", "text": "主播答(跃起)"},
                ],
                "truth_text": "嘉宾说 主播答(跃起)",
                "text_changed": True,
            },
            {
                "cue": 4,
                "timing": "00:00:03,000 --> 00:00:04,000",
                "machine_label": None,
                "machine_text": "可能听见",
                "truth_segments": [
                    {"label": None, "text": "可能听见(无可分辨人声）"}
                ],
                "truth_text": "可能听见(无可分辨人声）",
                "text_changed": True,
            },
            {
                "cue": 5,
                "timing": "00:00:04,000 --> 00:00:05,500",
                "annotated_timing": "00:00:04,001 --> 00:00:05,501",
                "machine_label": None,
                "machine_text": "主播五",
                "truth_segments": [{"label": "李豆沙", "text": "主播五"}],
                "truth_text": "主播五",
                "text_changed": False,
            },
        ],
    }
    truth_path = tmp_path / "reports" / "truth.json"
    truth_path.parent.mkdir()
    truth_path.write_text(json.dumps(truth, ensure_ascii=False), encoding="utf-8")
    arbitration = {
        "schema_version": ARBITRATION_SCHEMA,
        "candidate_id": CANDIDATE,
        "automatic_labelled_srt_sha256": _sha256(automatic_srt),
        "receipt_sha256": "a" * 64,
        "decisions": {
            "4": {
                "action": "retain_machine",
                "human_voice_observed": True,
                "speaker": "连线",
                "reason": "positive synthetic voice witness",
            }
        },
    }
    return {
        "truth_path": truth_path,
        "source_text_srt": source_srt,
        "source_media": media,
        "repo_root": tmp_path,
        "authority": AUTHORITY,
        "arbitration": arbitration,
        "automatic_srt_path": automatic_srt,
        "source_recording_basename": "source.mp4",
        "source_recording_sha256": "e" * 64,
        "absolute_source_start_ms": 10_000,
        "absolute_source_end_ms": 15_500,
        "reviewed_at": "2026-08-09",
    }


def test_compiler_strips_notes_merges_and_renumbers_before_speaker_binding(
    tmp_path: Path,
) -> None:
    result = compile_delivery(**_fixture(tmp_path))
    baseline = str(result["baseline_srt"])
    # Ivan 2026-08-10：手标动作注记是内容，机器听写元评论才是注记。
    assert "(跃起)" in baseline
    assert "无可分辨人声" not in baseline
    assert "2\n00:00:01,500 --> 00:00:02,900\n嘉宾说 主播答(跃起)" in baseline
    assert "3\n00:00:03,000 --> 00:00:04,000\n可能听见" in baseline
    assert "4\n00:00:04,000 --> 00:00:05,500\n主播五" in baseline

    override = result["speaker_override"]
    assert override["status"] == REVIEW_STATUS_PARTIAL_MACHINE
    assert "explicitly uncovered cues" in override["notes"]
    assert [row["source_cue"] for row in override["overrides"]] == [1, 2, 4]
    assert override["reviewed_speaker_baseline"]["anchor_source_cues"] == [1, 4]
    assert [
        row["source_cue"]
        for row in override["reviewed_speaker_baseline"]["machine_cues"]
    ] == [3]
    assert override["reviewed_speaker_baseline"]["machine_cues"][0]["speaker"] == "连线"
    merged = override["overrides"][1]
    assert merged["segments"][0]["start"] == "00:00:01,500"
    assert merged["segments"][-1]["end"] == "00:00:02,900"
    assert merged["segments"][0]["end"] == merged["segments"][1]["start"]

    baseline_path = tmp_path / "compiled.srt"
    baseline_path.write_text(baseline, encoding="utf-8")
    loaded = load_reviewed_speaker_baseline(
        override,
        candidate_id=CANDIDATE,
        cues=parse_srt(baseline_path),
        repo_root=tmp_path,
    )
    assert loaded is not None
    assert loaded.machine_cues == (3,)
    assert result["receipt"]["schema_version"] == (
        "reviewed-speaker-truth-delivery.v2"
    )
    assert result["receipt"]["automatic_labelled_srt"] == {
        "path": "assets/automatic-labelled.srt",
        "sha256": _sha256(tmp_path / "assets/automatic-labelled.srt"),
        "cue_count": 4,
    }


def test_content_stage_notes_render_and_machine_notes_are_stripped(
    tmp_path: Path,
) -> None:
    """Ivan 2026-08-10 令的两条回归：内容注记进字幕，机器注记不进。"""

    result = compile_delivery(**_fixture(tmp_path))
    baseline = str(result["baseline_srt"])
    override = result["speaker_override"]
    receipt = result["receipt"]

    # 1. 手标动作注记「(跃起)」= 内容：既留在交付基线，也留在说话人 override
    #    的 expect/segment 词面里（否则后续绑定会按剥掉的文本判漂移）。
    assert "主播答(跃起)" in baseline
    merged = next(
        row for row in override["overrides"] if row["source_cue"] == 2
    )
    assert merged["expect"]["text"] == "嘉宾说 主播答(跃起)"
    assert merged["segments"][-1]["text"] == "主播答(跃起)"

    # 2. 机器注记「(无可分辨人声)」= 剥离，不烧。
    assert "无可分辨人声" not in baseline
    machine_row = override["reviewed_speaker_baseline"]["machine_cues"][0]
    assert machine_row["expect"]["text"] == "可能听见"

    by_truth_cue = {row["truth_cue"]: row for row in receipt["transformations"]}
    assert by_truth_cue[2]["removed_machine_notes"] == []
    assert by_truth_cue[2]["retained_content_notes"] == ["跃起"]
    assert by_truth_cue[4]["removed_machine_notes"] == ["无可分辨人声"]
    assert by_truth_cue[4]["retained_content_notes"] == []


def test_full_width_machine_note_variants_are_both_stripped(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    truth_path = fixture["truth_path"]
    truth = json.loads(truth_path.read_text(encoding="utf-8"))
    row = next(item for item in truth["cues"] if item["cue"] == 4)
    row["truth_segments"][0]["text"] = "可能听见（无可辨别人声)"
    row["truth_text"] = "可能听见（无可辨别人声)"
    truth_path.write_text(json.dumps(truth, ensure_ascii=False), encoding="utf-8")
    baseline = str(compile_delivery(**fixture)["baseline_srt"])
    assert "无可辨别人声" not in baseline
    assert "可能听见" in baseline


def test_truth_full_ownership_pin_covers_the_whole_delivery_grid(
    tmp_path: Path,
) -> None:
    result = compile_delivery(**_fixture(tmp_path))
    manifest = result["baseline_manifest"]
    pin = manifest["truth_full_ownership"]
    assert pin["schema_version"] == "truth-full-ownership-pin.v1"
    assert pin["baseline_sha256"] == manifest["sha256"]
    assert pin["truth_input"]["path"] == "reports/truth.json"
    assert pin["cue_count"] == 4
    assert pin["reviewed_override_count"] == 3
    assert pin["machine_cues"] == [3]
    assert (
        pin["reviewed_override_count"] + len(pin["machine_cues"])
        == pin["cue_count"]
    )
    assert result["receipt"]["truth_full_ownership"] == pin


def test_v1_baseline_never_carries_a_truth_full_ownership_pin(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    fixture["baseline_schema_version"] = "subtitle-redelivery-baseline.v1"
    manifest = compile_delivery(**fixture)["baseline_manifest"]
    assert "truth_full_ownership" not in manifest
def test_compiler_keeps_sentence_comma_but_trims_routed_segment_prefix(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    truth = json.loads(fixture["truth_path"].read_text(encoding="utf-8"))
    merged = truth["cues"][1]
    merged["truth_segments"] = [
        {"label": "连线", "text": "嘉宾说"},
        {"label": "李豆沙", "text": "，主播答(跃起)"},
    ]
    merged["truth_text"] = "嘉宾说，主播答(跃起)"
    fixture["truth_path"].write_text(
        json.dumps(truth, ensure_ascii=False),
        encoding="utf-8",
    )
    automatic_srt = fixture["automatic_srt_path"]
    automatic_srt.write_text(
        automatic_srt.read_text(encoding="utf-8").replace(
            "[连线] 嘉宾说 主播答",
            "[连线] 嘉宾说，主播答",
        ),
        encoding="utf-8",
    )
    fixture["arbitration"]["automatic_labelled_srt_sha256"] = _sha256(
        automatic_srt
    )

    result = compile_delivery(**fixture)

    assert "嘉宾说，主播答" in result["baseline_srt"]
    routed = result["speaker_override"]["overrides"][1]["segments"]
    # 合并说明：ft 分支写这条时,尾部括注还是一律剥离,故原断言是「主播答」。
    # 主线 0625105 之后按 Ivan 2026-08-10 令改成 typed 白名单——只剥机器注记
    # (无可辨别人声/无可分辨人声),手标注记「(跃起)」保留。本条测试真正要验的是
    # 行首「，」被移出渲染段(见下方 removed_render_boundary_separators 断言),
    # 与括注策略无关,故只把期望值对齐到已裁定的保留语义,不动任何门。
    assert [segment["text"] for segment in routed] == ["嘉宾说", "主播答(跃起)"]
    transform = result["receipt"]["transformations"][1]
    assert transform["removed_render_boundary_separators"] == [
        {"segment_position": 2, "separator": "，"}
    ]


def test_compiler_requires_arbitration_for_every_unlabelled_truth_cue(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    fixture["arbitration"]["decisions"] = {}
    with pytest.raises(DeliveryCompileError, match="requires audio arbitration"):
        compile_delivery(**fixture)


def test_compiler_allows_no_arbitration_only_for_fully_labelled_truth(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    truth = json.loads(fixture["truth_path"].read_text(encoding="utf-8"))
    unresolved = next(row for row in truth["cues"] if row["cue"] == 4)
    unresolved["truth_segments"] = [{"label": "连线", "text": "可能听见"}]
    unresolved["truth_text"] = "可能听见"
    unresolved["text_changed"] = False
    fixture["truth_path"].write_text(
        json.dumps(truth, ensure_ascii=False), encoding="utf-8"
    )
    fixture["arbitration"] = None

    result = compile_delivery(**fixture)

    disposition = result["receipt"]["arbitration_disposition"]
    assert disposition["status"] == ARBITRATION_NOT_REQUIRED
    assert disposition["truth_input_sha256"] == _sha256(fixture["truth_path"])
    assert result["speaker_override"]["reviewed_speaker_baseline"][
        "machine_cues"
    ] == []
    assert result["speaker_override"]["status"] == REVIEW_STATUS_COMPLETE
    assert "no machine decision is retained" in result["speaker_override"]["notes"]
    assert result["baseline_manifest"]["truth_full_ownership"][
        "arbitration_disposition"
    ] == disposition


def test_compiler_rejects_omitted_arbitration_when_one_truth_cue_is_unlabelled(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    fixture["arbitration"] = None

    with pytest.raises(
        DeliveryCompileError, match="unlabelled truth cue requires"
    ):
        compile_delivery(**fixture)


def test_compiler_rejects_machine_label_or_automatic_hash_drift(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    fixture["arbitration"]["automatic_labelled_srt_sha256"] = "0" * 64
    with pytest.raises(DeliveryCompileError, match="automatic SRT hash mismatch"):
        compile_delivery(**fixture)

    fixture = _fixture(tmp_path / "speaker-drift")
    fixture["arbitration"]["decisions"]["4"]["speaker"] = "unknown"
    with pytest.raises(DeliveryCompileError, match="speaker is invalid"):
        compile_delivery(**fixture)


def test_compiler_rejects_noncanonical_automatic_srt_bytes(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    automatic = fixture["automatic_srt_path"]
    automatic.write_bytes(
        automatic.read_text(encoding="utf-8").replace("\n", "\r\n").encode()
    )
    fixture["arbitration"]["automatic_labelled_srt_sha256"] = _sha256(automatic)

    with pytest.raises(DeliveryCompileError, match="not canonical labelled SRT"):
        compile_delivery(**fixture)


def test_compiler_rejects_external_and_symlinked_automatic_srt(
    tmp_path: Path,
) -> None:
    external_fixture = _fixture(tmp_path / "external-root")
    external = tmp_path / "outside-automatic.srt"
    external.write_bytes(external_fixture["automatic_srt_path"].read_bytes())
    external_fixture["automatic_srt_path"] = external
    external_fixture["arbitration"]["automatic_labelled_srt_sha256"] = _sha256(
        external
    )
    with pytest.raises(DeliveryCompileError, match="inside the repository"):
        compile_delivery(**external_fixture)

    symlink_fixture = _fixture(tmp_path / "symlink-root")
    automatic = symlink_fixture["automatic_srt_path"]
    real = automatic.with_name("real-automatic.srt")
    automatic.rename(real)
    automatic.symlink_to(real.name)
    symlink_fixture["arbitration"]["automatic_labelled_srt_sha256"] = _sha256(real)
    with pytest.raises(DeliveryCompileError, match="must not traverse symlinks"):
        compile_delivery(**symlink_fixture)


def test_compiler_rejects_truth_to_source_text_hash_drift(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    fixture["source_text_srt"].write_text("drift", encoding="utf-8")
    with pytest.raises(DeliveryCompileError, match="does not bind source text"):
        compile_delivery(**fixture)


def test_compiler_rejects_extra_arbitration_rows(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    fixture["arbitration"]["decisions"]["1"] = {
        "action": "retain_machine",
        "human_voice_observed": True,
        "speaker": "连线",
        "reason": "not an unresolved cue",
    }
    with pytest.raises(DeliveryCompileError, match="unused rows"):
        compile_delivery(**fixture)


def test_compiler_can_emit_existing_v1_baseline_for_multi_piece_delivery(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    fixture["baseline_schema_version"] = "subtitle-redelivery-baseline.v1"
    result = compile_delivery(**fixture)
    manifest = result["baseline_manifest"]
    assert manifest["schema_version"] == "subtitle-redelivery-baseline.v1"
    assert "exact_interval_replay" not in manifest
    assert "absolute_source_start_ms" not in manifest
