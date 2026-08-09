import hashlib

from src.autoslice.redelivery_boundary_projection import (
    AUTHORITY_CONFIG_KEY,
    PROJECTION_MODE,
    PROJECTION_MODE_CONFIG_KEY,
    build_terminal_projection_authority,
)

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


def test_v2_v11_prefix_straddler_ending_at_first_reviewed_cue_is_uncovered(
    tmp_path,
):
    """The V11 prefix crosses only reviewed leading air, not a reviewed cue."""

    baseline = tmp_path / "baseline.srt"
    baseline.write_text(
        _srt(
            (250, 1_570, "你别坐地上"),
            (1_570, 3_470, "可是我现在真的很像坐在地上"),
        ),
        encoding="utf-8",
    )
    config = _config_v2(
        baseline,
        absolute_source_start_ms=1_572_910,
        absolute_source_end_ms=1_576_380,
    )
    current = _srt(
        (0, 2_920, "另外小N老师"),
        (2_920, 4_240, "随机第一句"),
        (4_240, 6_140, "随机第二句"),
    )

    output, audit = _run_v2(
        current,
        baseline=baseline,
        tmp_path=tmp_path,
        config=config,
        current_source_start_ms=1_570_240,
        current_source_end_ms=1_576_380,
    )

    assert audit["status"] == "APPLIED"
    assert "另外小N老师" in output
    assert "你别坐地上" in output
    assert "可是我现在真的很像坐在地上" in output
    assert audit["uncovered_current_cues"] == [
        {
            "current_cue_index": 1,
            "absolute_source_start_ms": 1_570_240,
            "absolute_source_end_ms": 1_573_160,
            "text": "另外小N老师",
            "boundary_position": "prefix",
            "classification": (
                "BOUNDARY_STRADDLE_WITHOUT_REVIEWED_CUE_OVERLAP"
            ),
            "reviewed_cue_support_boundary_ms": 1_573_160,
        }
    ]
    assert [
        (
            row["current_cue_index"],
            row["baseline_cue_index"],
            row["start_drift_ms"],
            row["end_drift_ms"],
        )
        for row in audit["mappings"]
    ] == [(2, 1, 0, 0), (3, 2, 0, 0)]


def test_v2_prefix_straddler_overlapping_first_reviewed_cue_by_1ms_fails(
    tmp_path,
):
    baseline = tmp_path / "baseline.srt"
    baseline.write_text(
        _srt((250, 1_570, "首条审定句")),
        encoding="utf-8",
    )
    config = _config_v2(
        baseline,
        absolute_source_start_ms=1_572_910,
        absolute_source_end_ms=1_574_480,
    )
    current = _srt(
        (0, 2_921, "跨进审定句一毫秒"),
        (2_921, 4_240, "随机审定句"),
    )

    output, audit = _run_v2(
        current,
        baseline=baseline,
        tmp_path=tmp_path,
        config=config,
        current_source_start_ms=1_570_240,
        current_source_end_ms=1_574_480,
    )

    assert output == current
    assert audit["status"] == "FAILED"
    assert audit["failures"] == [
        {
            "reason_code": (
                "REDELIVERY_CURRENT_CUE_STRADDLES_REVIEWED_COVERAGE"
            ),
            "current_cue_index": 1,
            "absolute_source_start_ms": 1_570_240,
            "absolute_source_end_ms": 1_573_161,
        }
    ]


def test_v2_tail_straddler_starting_at_last_reviewed_cue_end_is_uncovered(
    tmp_path,
):
    baseline = tmp_path / "baseline.srt"
    baseline.write_text(
        _srt(
            (0, 1_000, "第一条审定句"),
            (1_000, 2_750, "最后一条审定句"),
        ),
        encoding="utf-8",
    )
    config = _config_v2(
        baseline,
        absolute_source_end_ms=103_000,
    )
    current = _srt(
        (0, 1_000, "随机第一句"),
        (1_000, 2_750, "随机第二句"),
        (2_750, 3_500, "新增尾句"),
    )

    output, audit = _run_v2(
        current,
        baseline=baseline,
        tmp_path=tmp_path,
        config=config,
        current_source_end_ms=103_500,
    )

    assert audit["status"] == "APPLIED"
    assert "第一条审定句" in output
    assert "最后一条审定句" in output
    assert "新增尾句" in output
    assert audit["uncovered_current_cues"] == [
        {
            "current_cue_index": 3,
            "absolute_source_start_ms": 102_750,
            "absolute_source_end_ms": 103_500,
            "text": "新增尾句",
            "boundary_position": "tail",
            "classification": (
                "BOUNDARY_STRADDLE_WITHOUT_REVIEWED_CUE_OVERLAP"
            ),
            "reviewed_cue_support_boundary_ms": 102_750,
        }
    ]


def test_v2_tail_straddler_overlapping_last_reviewed_cue_by_1ms_fails(
    tmp_path,
):
    baseline = tmp_path / "baseline.srt"
    baseline.write_text(
        _srt(
            (0, 1_000, "第一条审定句"),
            (1_000, 2_750, "最后一条审定句"),
        ),
        encoding="utf-8",
    )
    config = _config_v2(
        baseline,
        absolute_source_end_ms=103_000,
    )
    current = _srt(
        (0, 1_000, "随机第一句"),
        (1_000, 2_749, "随机第二句"),
        (2_749, 3_500, "跨进审定句一毫秒"),
    )

    output, audit = _run_v2(
        current,
        baseline=baseline,
        tmp_path=tmp_path,
        config=config,
        current_source_end_ms=103_500,
    )

    assert output == current
    assert audit["status"] == "FAILED"
    assert audit["failures"] == [
        {
            "reason_code": (
                "REDELIVERY_CURRENT_CUE_STRADDLES_REVIEWED_COVERAGE"
            ),
            "current_cue_index": 3,
            "absolute_source_start_ms": 102_749,
            "absolute_source_end_ms": 103_500,
        }
    ]


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


def test_v2_exact_interval_replays_reviewed_missing_cues_when_authorized(
    tmp_path,
):
    baseline = tmp_path / "baseline.srt"
    baseline.write_text(
        _srt((0, 1_000, "第一句"), (1_000, 2_000, "第二句")),
        encoding="utf-8",
    )
    current = _srt((0, 1_000, "随机第一句"))
    config = _config_v2(baseline)
    config["exact_interval_replay"] = True

    output, audit = _run_v2(
        current,
        baseline=baseline,
        tmp_path=tmp_path,
        config=config,
    )

    assert output == baseline.read_text(encoding="utf-8")
    assert audit["status"] == "APPLIED"
    assert audit["application_strategy"] == "exact_reviewed_interval_replay"
    assert audit["timing_authority"] == (
        "hash_bound_reviewed_srt_exact_source_interval"
    )
    assert audit["current_cue_count"] == 1
    assert audit["replayed_cue_count"] == 2
    assert audit["changed_cue_count"] == 2
    assert audit["failures"] == []
    assert audit["mappings"][0]["pre_replay_cues"] == [
        {
            "current_cue_index": 1,
            "start_ms": 0,
            "end_ms": 1_000,
            "text": "随机第一句",
        }
    ]
    assert audit["mappings"][1]["pre_replay_cues"] == []


def test_terminal_projection_requires_and_receipts_exact_replay(tmp_path):
    baseline = tmp_path / "baseline.srt"
    baseline.write_text(
        _srt((0, 1_000, "第一句"), (1_000, 2_000, "审核收尾")),
        encoding="utf-8",
    )
    config = _config_v2(baseline)
    config["exact_interval_replay"] = True
    config[PROJECTION_MODE_CONFIG_KEY] = PROJECTION_MODE
    spec = {
        "candidate_id": "candidate-projection",
        "pieces": [
            {
                "remote_media": f"/recordings/{SOURCE_BASENAME}",
                "start_ms": 100_000,
                "end_ms": 103_000,
            }
        ],
        "subtitle_redelivery_baseline": config,
    }
    authority = build_terminal_projection_authority(
        spec=spec,
        piece_provenance_rows=[
            {
                "source_path": f"/recordings/{SOURCE_BASENAME}",
                "source_sha256": SOURCE_SHA256,
            }
        ],
        spec_parent=tmp_path,
    )
    assert authority is not None
    config[AUTHORITY_CONFIG_KEY] = authority

    output, audit = _run_v2(
        _srt((0, 1_000, "随机第一句")),
        baseline=baseline,
        tmp_path=tmp_path,
        config=config,
    )

    assert output == baseline.read_text(encoding="utf-8")
    assert audit["status"] == "APPLIED"
    assert audit["terminal_projection_materialization"] == {
        "schema_version": (
            "reviewed-exact-interval-terminal-projection-materialization.v1"
        ),
        "status": "PASS",
        "authority_sha256": authority["authority_sha256"],
        "application_strategy": "exact_reviewed_interval_replay",
        "absolute_source_start_ms": 100_000,
        "absolute_source_end_ms": 102_000,
        "video_tail_extension_ms": 0,
    }

    failed_output, failed_audit = _run_v2(
        _srt((0, 1_000, "随机第一句"), (1_000, 2_200, "越界尾巴")),
        baseline=baseline,
        tmp_path=tmp_path,
        config=config,
        current_source_end_ms=102_200,
    )
    assert "越界尾巴" in failed_output
    assert failed_audit["status"] == "FAILED"
    assert any(
        row["reason_code"]
        == "REDELIVERY_TERMINAL_PROJECTION_REQUIRES_EXACT_INTERVAL"
        for row in failed_audit["failures"]
    )


def test_v2_exact_interval_replays_with_bounded_video_only_tail(
    tmp_path,
):
    baseline = tmp_path / "baseline.srt"
    baseline.write_text(
        _srt((0, 1_000, "第一句"), (1_000, 2_000, "审核收尾")),
        encoding="utf-8",
    )
    config = _config_v2(baseline)
    config["exact_interval_replay"] = True
    current = _srt(
        (0, 1_000, "随机第一句"),
        (1_000, 2_400, "随机网格把收尾拉进尾垫"),
    )

    output, audit = _run_v2(
        current,
        baseline=baseline,
        tmp_path=tmp_path,
        config=config,
        current_source_end_ms=102_400,
    )

    assert output == baseline.read_text(encoding="utf-8")
    assert audit["status"] == "APPLIED"
    assert audit["application_strategy"] == "exact_reviewed_interval_replay"
    assert audit["video_tail_extension_ms"] == 400
    assert audit["failures"] == []


def test_v2_exact_interval_rejects_tail_beyond_renderer_pad(
    tmp_path,
):
    baseline = tmp_path / "baseline.srt"
    baseline.write_text(
        _srt((0, 1_000, "第一句"), (1_000, 2_000, "审核收尾")),
        encoding="utf-8",
    )
    config = _config_v2(baseline)
    config["exact_interval_replay"] = True
    current = _srt(
        (0, 1_000, "随机第一句"),
        (1_000, 2_401, "越界新内容"),
    )

    output, audit = _run_v2(
        current,
        baseline=baseline,
        tmp_path=tmp_path,
        config=config,
        current_source_end_ms=102_401,
    )

    assert output == current
    assert audit["status"] == "FAILED"
    assert audit.get("application_strategy") is None


def test_v2_exact_interval_flag_falls_back_when_current_interval_is_trimmed(
    tmp_path,
):
    baseline = tmp_path / "baseline.srt"
    baseline.write_text(
        _srt(
            (0, 1_000, "旧首句"),
            (1_000, 2_000, "旧第二句"),
            (2_000, 3_000, "旧第三句"),
        ),
        encoding="utf-8",
    )
    config = _config_v2(
        baseline,
        absolute_source_start_ms=100_000,
        absolute_source_end_ms=103_000,
    )
    config["exact_interval_replay"] = True
    current = _srt(
        (0, 1_000, "随机第二句"),
        (1_000, 2_000, "随机第三句"),
    )

    output, audit = _run_v2(
        current,
        baseline=baseline,
        tmp_path=tmp_path,
        config=config,
        current_source_start_ms=101_000,
        current_source_end_ms=103_000,
    )

    assert "旧首句" not in output
    assert "旧第二句" in output and "旧第三句" in output
    assert audit["status"] == "APPLIED"
    assert audit.get("application_strategy") is None


def test_v2_exact_interval_replay_flag_must_be_boolean(tmp_path):
    baseline = tmp_path / "baseline.srt"
    baseline.write_text(_srt((0, 1_000, "第一句")), encoding="utf-8")
    config = _config_v2(baseline)
    config["exact_interval_replay"] = "yes"

    output, audit = _run_v2(
        _srt((0, 1_000, "随机第一句")),
        baseline=baseline,
        tmp_path=tmp_path,
        config=config,
    )

    assert "随机第一句" in output
    assert audit["status"] == "FAILED"
    assert audit["failures"] == [
        {
            "reason_code": (
                "REDELIVERY_BASELINE_EXACT_INTERVAL_REPLAY_INVALID"
            )
        }
    ]


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


def test_release_grade_merge_equivalence_accepted_in_v2_alignment():
    """1863 案（2026-07-27）：生产端把 240ms「哦」并入「这样吗」后，恒等
    比较必须认「合并=基线相邻拼接+时窗并集」，否则合并版永久失败；
    文本多字/时窗越界仍拒。"""

    from src.autoslice.jingting_chunker import SrtCue
    from src.autoslice.redelivery_subtitle_baseline import (
        _release_grade_merge_equivalent,
    )

    baseline = [
        SrtCue(1, 25_190, 27_130, "是你在玩游戏我就走了"),
        SrtCue(2, 27_130, 27_370, "哦"),
        SrtCue(3, 27_370, 28_010, "这样吗"),
    ]
    merged = SrtCue(2, 27_130, 28_010, "哦，这样吗")
    assert _release_grade_merge_equivalent(merged, [1, 2], baseline)

    # 文本带私货 → 拒
    tampered = SrtCue(2, 27_130, 28_010, "哦，这样吗啊")
    assert not _release_grade_merge_equivalent(tampered, [1, 2], baseline)
    # 时窗越界 → 拒
    stretched = SrtCue(2, 27_130, 29_500, "哦，这样吗")
    assert not _release_grade_merge_equivalent(stretched, [1, 2], baseline)
    # 非相邻基线 → 拒
    assert not _release_grade_merge_equivalent(merged, [0, 2], baseline)


def test_release_grade_merge_accepts_bounded_fresh_grid_edge_drift():
    """1863 生产实案：文本等价的「嘻，晓得吧」+「行」合并后，新 ASR
    外沿比 reviewed 并集早 320ms；应接受，但 400ms 之外仍拒。"""

    from src.autoslice.jingting_chunker import SrtCue
    from src.autoslice.redelivery_subtitle_baseline import (
        _release_grade_merge_equivalent,
    )

    baseline = [
        SrtCue(1, 75_720, 77_200, "嘻，晓得吧"),
        SrtCue(2, 77_280, 78_280, "行"),
    ]
    production = SrtCue(1, 75_720, 77_960, "嘻，晓得吧，行")
    assert _release_grade_merge_equivalent(production, [0, 1], baseline)

    beyond_cap = SrtCue(1, 75_720, 77_879, "嘻，晓得吧，行")
    assert not _release_grade_merge_equivalent(
        beyond_cap, [0, 1], baseline
    )


def test_v2_renders_bounded_release_grade_merge_without_strong_row(tmp_path):
    baseline = tmp_path / "baseline.srt"
    baseline.write_text(
        _srt(
            (15_720, 17_200, "嘻，晓得吧"),
            (17_280, 18_280, "行"),
        ),
        encoding="utf-8",
    )
    current = _srt((15_720, 17_960, "嘻，晓得吧，行"))

    output, audit = _run_v2(
        current,
        baseline=baseline,
        tmp_path=tmp_path,
        current_source_end_ms=200_000,
        config=_config_v2(
            baseline,
            absolute_source_start_ms=100_000,
            absolute_source_end_ms=200_000,
        ),
    )

    assert output == current
    assert audit["status"] == "ALREADY_SATISFIED"
    assert audit["accepted_release_grade_merges"] == [
        {
            "current_cue_index": 1,
            "baseline_cue_indexes": [1, 2],
        }
    ]
    assert audit["mappings"][0]["mapping_kind"] == (
        "release_grade_merge_equivalent"
    )
