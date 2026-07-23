import hashlib

from src.autoslice.redelivery_subtitle_baseline import (
    MODE,
    SCHEMA_VERSION,
    SCHEMA_VERSION_V2,
    apply_redelivery_subtitle_baseline,
)

SOURCE_BASENAME = "recording.mp4"
SOURCE_SHA256 = "a" * 64


def _srt(*rows):
    def stamp(value):
        seconds, millis = divmod(value, 1_000)
        return f"00:00:{seconds:02d},{millis:03d}"

    return "\n\n".join(
        f"{index}\n{stamp(start)} --> {stamp(end)}\n{text}"
        for index, (start, end, text) in enumerate(rows, start=1)
    ) + "\n"


def _config(path):
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": MODE,
        "path": str(path),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "authority": "previous reviewed delivery",
    }


def _config_v2(
    path,
    *,
    absolute_source_start_ms=100_000,
    absolute_source_end_ms=102_000,
    source_recording_basename=SOURCE_BASENAME,
    source_sha256=SOURCE_SHA256,
):
    return {
        "schema_version": SCHEMA_VERSION_V2,
        "mode": MODE,
        "path": str(path),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "authority": "previous reviewed delivery",
        "absolute_source_start_ms": absolute_source_start_ms,
        "absolute_source_end_ms": absolute_source_end_ms,
        "source_recording_basename": source_recording_basename,
        "source_sha256": source_sha256,
    }


def _run_v2(
    current,
    *,
    baseline,
    tmp_path,
    config=None,
    current_source_start_ms=100_000,
    current_source_end_ms=102_000,
    current_source_recording_basename=SOURCE_BASENAME,
    current_source_sha256=SOURCE_SHA256,
    protected_windows=(),
):
    return apply_redelivery_subtitle_baseline(
        current,
        config=config or _config_v2(baseline),
        spec_parent=tmp_path,
        protected_windows=protected_windows,
        current_source_start_ms=current_source_start_ms,
        current_source_end_ms=current_source_end_ms,
        current_source_recording_basename=current_source_recording_basename,
        current_source_sha256=current_source_sha256,
    )


def test_redelivery_preserves_prior_text_but_keeps_new_timing(tmp_path):
    baseline = tmp_path / "baseline.srt"
    baseline.write_text(
        _srt((0, 1_000, "旧审定第一句"), (1_000, 2_000, "旧审定第二句")),
        encoding="utf-8",
    )
    current = _srt(
        (20, 1_020, "随机漂移第一句"),
        (1_020, 2_020, "随机漂移第二句"),
    )

    output, audit = apply_redelivery_subtitle_baseline(
        current,
        config=_config(baseline),
        spec_parent=tmp_path,
    )

    assert "00:00:00,020 --> 00:00:01,020\n旧审定第一句" in output
    assert "00:00:01,020 --> 00:00:02,020\n旧审定第二句" in output
    assert "随机漂移" not in output
    assert audit["status"] == "APPLIED"
    assert audit["changed_cue_count"] == 2


def test_source_truth_window_remains_newer_than_redelivery_baseline(tmp_path):
    baseline = tmp_path / "baseline.srt"
    baseline.write_text(
        _srt((0, 1_000, "旧审定第一句"), (1_000, 2_000, "旧错误文字")),
        encoding="utf-8",
    )
    current = _srt(
        (0, 1_000, "随机漂移第一句"),
        (1_000, 2_000, "Ivan新源真值"),
    )

    output, audit = apply_redelivery_subtitle_baseline(
        current,
        config=_config(baseline),
        spec_parent=tmp_path,
        protected_windows=[(1_000, 2_000)],
    )

    assert "旧审定第一句" in output
    assert "Ivan新源真值" in output
    assert "旧错误文字" not in output
    assert audit["protected_cue_count"] == 1


def test_redelivery_fails_closed_on_segmentation_drift(tmp_path):
    baseline = tmp_path / "baseline.srt"
    baseline.write_text(
        _srt((0, 1_000, "第一句"), (1_000, 2_000, "第二句")),
        encoding="utf-8",
    )
    current = _srt((0, 2_000, "两句被合并"))

    output, audit = apply_redelivery_subtitle_baseline(
        current,
        config=_config(baseline),
        spec_parent=tmp_path,
    )

    assert output == current
    assert audit["status"] == "FAILED"
    assert any(
        row["reason_code"] == "REDELIVERY_BASELINE_CUE_UNCONSUMED"
        for row in audit["failures"]
    )


def test_redelivery_fails_closed_on_stale_baseline_hash(tmp_path):
    baseline = tmp_path / "baseline.srt"
    baseline.write_text(_srt((0, 1_000, "第一句")), encoding="utf-8")
    config = _config(baseline)
    config["sha256"] = "0" * 64

    current = _srt((0, 1_000, "随机漂移"))
    output, audit = apply_redelivery_subtitle_baseline(
        current,
        config=config,
        spec_parent=tmp_path,
    )

    assert output == current
    assert audit["status"] == "FAILED"
    assert audit["failures"] == [
        {"reason_code": "REDELIVERY_BASELINE_SHA256_MISMATCH"}
    ]


def test_v2_projects_by_absolute_source_time_after_start_shift(tmp_path):
    baseline = tmp_path / "baseline.srt"
    baseline.write_text(
        _srt(
            (0, 800, "旧首句，已被新边界裁掉"),
            (1_000, 2_000, "旧审定第二句"),
            (2_000, 3_000, "旧审定第三句"),
        ),
        encoding="utf-8",
    )
    config = _config_v2(
        baseline,
        absolute_source_start_ms=100_000,
        absolute_source_end_ms=103_000,
    )
    current = _srt(
        (0, 1_000, "随机漂移第二句"),
        (1_000, 2_000, "随机漂移第三句"),
    )

    output, audit = _run_v2(
        current,
        baseline=baseline,
        tmp_path=tmp_path,
        config=config,
        current_source_start_ms=101_000,
        current_source_end_ms=103_000,
    )

    assert "00:00:00,000 --> 00:00:01,000\n旧审定第二句" in output
    assert "00:00:01,000 --> 00:00:02,000\n旧审定第三句" in output
    assert "旧首句" not in output
    assert "随机漂移" not in output
    assert audit["status"] == "APPLIED"
    assert audit["effective_reviewed_overlap"] == {
        "absolute_source_start_ms": 101_000,
        "absolute_source_end_ms": 103_000,
    }
    assert audit["omitted_by_new_boundary"] == [
        {
            "baseline_cue_index": 1,
            "absolute_source_start_ms": 100_000,
            "absolute_source_end_ms": 100_800,
            "text": "旧首句，已被新边界裁掉",
        }
    ]
    assert [
        (
            row["current_absolute_source_start_ms"],
            row["baseline_absolute_source_start_ms"],
        )
        for row in audit["mappings"]
    ] == [(101_000, 101_000), (102_000, 102_000)]


def test_v2_tail_extension_stays_current_and_unowned(tmp_path):
    baseline = tmp_path / "baseline.srt"
    baseline.write_text(
        _srt((0, 1_000, "旧审定第一句"), (1_000, 2_000, "旧审定第二句")),
        encoding="utf-8",
    )
    current = _srt(
        (0, 1_000, "随机第一句"),
        (1_000, 2_000, "随机第二句"),
        (2_200, 3_000, "新增长尾句"),
    )

    output, audit = _run_v2(
        current,
        baseline=baseline,
        tmp_path=tmp_path,
        current_source_end_ms=103_000,
    )

    assert "旧审定第一句" in output
    assert "旧审定第二句" in output
    assert "新增长尾句" in output
    assert audit["status"] == "APPLIED"
    assert audit["uncovered_current_intervals"] == [
        {
            "position": "tail",
            "absolute_source_start_ms": 102_000,
            "absolute_source_end_ms": 103_000,
            "local_start_ms": 2_000,
            "local_end_ms": 3_000,
        }
    ]
    assert audit["uncovered_current_cue_count"] == 1
    assert audit["owned_intervals"] == [
        {"start_ms": 0, "end_ms": 1_000},
        {"start_ms": 1_000, "end_ms": 2_000},
    ]


def test_v2_uncovered_prefix_and_tail_are_preserved(tmp_path):
    baseline = tmp_path / "baseline.srt"
    baseline.write_text(
        _srt((0, 1_000, "旧审定第一句"), (1_000, 2_000, "旧审定第二句")),
        encoding="utf-8",
    )
    config = _config_v2(
        baseline,
        absolute_source_start_ms=101_000,
        absolute_source_end_ms=103_000,
    )
    current = _srt(
        (0, 800, "未覆盖前缀"),
        (1_000, 2_000, "随机第一句"),
        (2_000, 3_000, "随机第二句"),
        (3_200, 4_000, "未覆盖尾句"),
    )

    output, audit = _run_v2(
        current,
        baseline=baseline,
        tmp_path=tmp_path,
        config=config,
        current_source_start_ms=100_000,
        current_source_end_ms=104_000,
    )

    assert "未覆盖前缀" in output
    assert "旧审定第一句" in output
    assert "旧审定第二句" in output
    assert "未覆盖尾句" in output
    assert audit["uncovered_current_intervals"] == [
        {
            "position": "prefix",
            "absolute_source_start_ms": 100_000,
            "absolute_source_end_ms": 101_000,
            "local_start_ms": 0,
            "local_end_ms": 1_000,
        },
        {
            "position": "tail",
            "absolute_source_start_ms": 103_000,
            "absolute_source_end_ms": 104_000,
            "local_start_ms": 3_000,
            "local_end_ms": 4_000,
        },
    ]
    assert audit["uncovered_current_cue_count"] == 2


def test_v2_projects_protected_drop_window_on_absolute_timeline(tmp_path):
    baseline = tmp_path / "baseline.srt"
    baseline.write_text(
        _srt(
            (0, 1_000, "旧边界外句"),
            (1_000, 2_000, "旧审定保留句"),
            (2_000, 3_000, "旧静音幻听"),
        ),
        encoding="utf-8",
    )
    config = _config_v2(
        baseline,
        absolute_source_start_ms=100_000,
        absolute_source_end_ms=103_000,
    )
    current = _srt((0, 1_000, "随机保留句"))

    output, audit = _run_v2(
        current,
        baseline=baseline,
        tmp_path=tmp_path,
        config=config,
        current_source_start_ms=101_000,
        current_source_end_ms=103_000,
        protected_windows=[(1_000, 2_000)],
    )

    assert "旧审定保留句" in output
    assert "旧静音幻听" not in output
    assert audit["status"] == "APPLIED"
    assert audit["protected_absolute_intervals"] == [
        {
            "absolute_source_start_ms": 102_000,
            "absolute_source_end_ms": 103_000,
        }
    ]


def test_v2_allows_fully_protected_overlap_for_later_source_truth(tmp_path):
    baseline = tmp_path / "baseline.srt"
    baseline.write_text(_srt((0, 1_000, "旧静音幻听")), encoding="utf-8")
    config = _config_v2(
        baseline,
        absolute_source_start_ms=100_000,
        absolute_source_end_ms=101_000,
    )
    current = _srt((1_100, 2_000, "覆盖区外新句"))

    output, audit = _run_v2(
        current,
        baseline=baseline,
        tmp_path=tmp_path,
        config=config,
        current_source_end_ms=102_000,
        protected_windows=[(0, 1_000)],
    )

    assert output == current
    assert audit["status"] == "ALREADY_SATISFIED"
    assert audit["mapped_cue_count"] == 0
    assert audit["uncovered_current_cue_count"] == 1


def test_v2_fails_closed_when_covered_baseline_cue_is_missing(tmp_path):
    baseline = tmp_path / "baseline.srt"
    baseline.write_text(
        _srt((0, 1_000, "第一句"), (1_000, 2_000, "第二句")),
        encoding="utf-8",
    )
    current = _srt((0, 1_000, "随机第一句"))

    output, audit = _run_v2(
        current,
        baseline=baseline,
        tmp_path=tmp_path,
    )

    assert output == current
    assert audit["status"] == "FAILED"
    assert any(
        row["reason_code"] == "REDELIVERY_BASELINE_CUE_UNCONSUMED"
        and row["baseline_cue_index"] == 2
        for row in audit["failures"]
    )


def test_v2_fails_closed_on_ambiguous_alignment(tmp_path):
    baseline = tmp_path / "baseline.srt"
    baseline.write_text(
        _srt((0, 1_000, "候选甲"), (0, 1_000, "候选乙")),
        encoding="utf-8",
    )
    current = _srt((0, 1_000, "随机文本"))

    output, audit = _run_v2(
        current,
        baseline=baseline,
        tmp_path=tmp_path,
    )

    assert output == current
    assert audit["status"] == "FAILED"
    assert any(
        row["reason_code"] == "REDELIVERY_CURRENT_CUE_ALIGNMENT_AMBIGUOUS"
        for row in audit["failures"]
    )


def test_v2_fails_closed_when_current_merges_baseline_cues(tmp_path):
    baseline = tmp_path / "baseline.srt"
    baseline.write_text(
        _srt((0, 900, "第一句"), (1_100, 2_000, "第二句")),
        encoding="utf-8",
    )
    current = _srt((0, 2_000, "合并后的随机文本"))

    output, audit = _run_v2(
        current,
        baseline=baseline,
        tmp_path=tmp_path,
    )

    assert output == current
    assert audit["status"] == "FAILED"
    assert any(
        row["reason_code"] == "REDELIVERY_CURRENT_CUE_MERGES_BASELINE_CUES"
        for row in audit["failures"]
    )


def test_v2_fails_closed_when_baseline_cue_is_split(tmp_path):
    baseline = tmp_path / "baseline.srt"
    baseline.write_text(_srt((0, 2_000, "原本一整句")), encoding="utf-8")
    current = _srt((0, 900, "拆分一"), (1_100, 2_000, "拆分二"))

    output, audit = _run_v2(
        current,
        baseline=baseline,
        tmp_path=tmp_path,
    )

    assert output == current
    assert audit["status"] == "FAILED"
    assert any(
        row["reason_code"]
        == "REDELIVERY_BASELINE_CUE_SPLIT_ACROSS_CURRENT_CUES"
        for row in audit["failures"]
    )


def test_v2_fails_closed_on_absolute_timeline_drift(tmp_path):
    baseline = tmp_path / "baseline.srt"
    baseline.write_text(_srt((0, 2_000, "旧审定句")), encoding="utf-8")
    config = _config_v2(
        baseline,
        absolute_source_start_ms=100_000,
        absolute_source_end_ms=103_000,
    )
    current = _srt((260, 2_260, "漂移后的随机句"))

    output, audit = _run_v2(
        current,
        baseline=baseline,
        tmp_path=tmp_path,
        config=config,
        current_source_end_ms=103_000,
    )

    assert output == current
    assert audit["status"] == "FAILED"
    assert any(
        row["reason_code"] == "REDELIVERY_CURRENT_CUE_ALIGNMENT_DRIFT"
        for row in audit["failures"]
    )


def test_v2_fails_closed_when_source_recording_hash_drifts(tmp_path):
    baseline = tmp_path / "baseline.srt"
    baseline.write_text(_srt((0, 1_000, "旧审定句")), encoding="utf-8")
    current = _srt((0, 1_000, "随机句"))

    output, audit = _run_v2(
        current,
        baseline=baseline,
        tmp_path=tmp_path,
        current_source_sha256="b" * 64,
    )

    assert output == current
    assert audit["status"] == "FAILED"
    assert audit["failures"] == [
        {
            "reason_code": "REDELIVERY_SOURCE_RECORDING_SHA256_MISMATCH",
            "expected": SOURCE_SHA256,
            "current": "b" * 64,
        }
    ]


def test_v2_fails_closed_when_source_recording_basename_differs(tmp_path):
    baseline = tmp_path / "baseline.srt"
    baseline.write_text(_srt((0, 1_000, "旧审定句")), encoding="utf-8")
    current = _srt((0, 1_000, "随机句"))

    output, audit = _run_v2(
        current,
        baseline=baseline,
        tmp_path=tmp_path,
        current_source_recording_basename="other-recording.mp4",
    )

    assert output == current
    assert audit["status"] == "FAILED"
    assert audit["failures"][0]["reason_code"] == (
        "REDELIVERY_SOURCE_RECORDING_BASENAME_MISMATCH"
    )


def test_v2_fails_closed_when_new_boundary_cuts_baseline_cue(tmp_path):
    baseline = tmp_path / "baseline.srt"
    baseline.write_text(_srt((0, 1_000, "不可裁半句")), encoding="utf-8")
    current = _srt((0, 500, "只剩后半句"))

    output, audit = _run_v2(
        current,
        baseline=baseline,
        tmp_path=tmp_path,
        current_source_start_ms=100_500,
        current_source_end_ms=101_000,
    )

    assert output == current
    assert audit["status"] == "FAILED"
    assert any(
        row["reason_code"] == "REDELIVERY_BASELINE_CUE_CUT_BY_NEW_BOUNDARY"
        for row in audit["failures"]
    )
