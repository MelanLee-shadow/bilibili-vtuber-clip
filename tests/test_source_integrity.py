from src.autoslice.source_integrity import (
    MediaSegmentObservation,
    build_source_range_ledger,
    plan_bilibili_replay_compensation,
    plan_bilibili_replay_download_commands,
)


def test_danmaku_outruns_tiny_media_requires_replay_compensation():
    ledger = build_source_range_ledger(
        room_id="22966160",
        session_date="2026-06-29",
        segments=[
            MediaSegmentObservation(
                path="/app/Videos/22966160/2026-06-29/tiny.m4s",
                start_ms=0,
                end_ms=4_040,
                duration_ms=4_040,
                size_bytes=3_369,
                probed_ok=True,
            )
        ],
        expected_start_ms=0,
        expected_end_ms=7_200_000,
        danmaku_latest_ms=7_100_000,
        active_media_size_growth_bytes=0,
    )

    assert ledger.compensation_required is True
    assert ledger.replay_probe_required is True
    assert ledger.can_use_local_source is False
    assert ledger.missing_ranges[0].start_ms == 4_040
    codes = {issue.code for issue in ledger.issues}
    assert "MEDIA_SEGMENT_TOO_SHORT" in codes
    assert "DANMAKU_OUTRUNS_MEDIA" in codes
    assert "MEDIA_STALLED_WHILE_DANMAKU_ADVANCES" in codes


def test_contiguous_valid_segments_do_not_need_replay_probe():
    ledger = build_source_range_ledger(
        room_id="22966160",
        session_date="2026-06-29",
        segments=[
            MediaSegmentObservation(path="a.m4s", start_ms=0, end_ms=60_000, duration_ms=60_000, size_bytes=5_000_000),
            MediaSegmentObservation(path="b.m4s", start_ms=60_000, end_ms=120_000, duration_ms=60_000, size_bytes=5_000_000),
        ],
        expected_start_ms=0,
        expected_end_ms=120_000,
        danmaku_latest_ms=119_000,
        active_media_size_growth_bytes=1024,
    )

    assert ledger.compensation_required is False
    assert ledger.replay_probe_required is False
    assert ledger.can_use_local_source is True
    assert ledger.missing_ranges == ()
    assert ledger.issues == ()


def test_gap_between_segments_records_missing_source_range():
    ledger = build_source_range_ledger(
        room_id="22966160",
        session_date="2026-06-29",
        segments=[
            MediaSegmentObservation(path="a.m4s", start_ms=0, end_ms=60_000, duration_ms=60_000, size_bytes=5_000_000),
            MediaSegmentObservation(path="b.m4s", start_ms=90_000, end_ms=150_000, duration_ms=60_000, size_bytes=5_000_000),
        ],
        expected_start_ms=0,
        expected_end_ms=150_000,
        max_gap_ms=2_000,
    )

    assert ledger.compensation_required is True
    assert [(r.start_ms, r.end_ms) for r in ledger.missing_ranges] == [(60_000, 90_000)]
    assert "MEDIA_COVERAGE_GAP" in {issue.code for issue in ledger.issues}


def test_replay_compensation_plan_fails_closed_without_auth_or_availability():
    ledger = build_source_range_ledger(
        room_id="22966160",
        session_date="2026-06-29",
        segments=[],
        expected_start_ms=0,
        expected_end_ms=60_000,
    )

    no_auth = plan_bilibili_replay_compensation(ledger, auth_available=False, replay_available=None)
    assert no_auth.status == "BLOCKED"
    assert "BILIBILI_REPLAY_AUTH_REQUIRED" in no_auth.reason_codes
    assert no_auth.download_ranges == ()

    unavailable = plan_bilibili_replay_compensation(ledger, auth_available=True, replay_available=False)
    assert unavailable.status == "RETRY_INFRA"
    assert "BILIBILI_REPLAY_UNAVAILABLE" in unavailable.reason_codes
    assert unavailable.download_ranges == ()


def test_replay_compensation_plan_is_download_ready_only_when_auth_and_replay_available():
    ledger = build_source_range_ledger(
        room_id="22966160",
        session_date="2026-06-29",
        segments=[],
        expected_start_ms=0,
        expected_end_ms=60_000,
    )

    plan = plan_bilibili_replay_compensation(ledger, auth_available=True, replay_available=True)

    assert plan.status == "READY_TO_DOWNLOAD"
    assert plan.reason_codes == ()
    assert [(r.start_ms, r.end_ms) for r in plan.download_ranges] == [(0, 60_000)]
    manifest = plan.to_manifest()
    assert "cookie" not in str(manifest).lower()
    assert manifest["room_id"] == "22966160"


def test_replay_download_command_plan_fails_closed_without_url_tool_or_auth(tmp_path):
    ledger = build_source_range_ledger(
        room_id="22966160",
        session_date="2026-06-29",
        segments=[],
        expected_start_ms=60_000,
        expected_end_ms=90_000,
    )
    compensation = plan_bilibili_replay_compensation(ledger, auth_available=True, replay_available=True)

    missing_url = plan_bilibili_replay_download_commands(
        compensation,
        replay_url=None,
        output_dir=tmp_path,
        cookie_file=tmp_path / "cookie.json",
        tool_path="/usr/bin/yt-dlp",
    )
    assert missing_url.status == "BLOCKED"
    assert "BILIBILI_REPLAY_URL_REQUIRED" in missing_url.reason_codes
    assert missing_url.commands == ()

    missing_auth = plan_bilibili_replay_download_commands(
        compensation,
        replay_url="https://www.bilibili.com/video/BVplaceholder",
        output_dir=tmp_path,
        cookie_file=None,
        tool_path="/usr/bin/yt-dlp",
    )
    assert missing_auth.status == "BLOCKED"
    assert "BILIBILI_REPLAY_AUTH_REQUIRED" in missing_auth.reason_codes
    assert missing_auth.commands == ()

    missing_tool = plan_bilibili_replay_download_commands(
        compensation,
        replay_url="https://www.bilibili.com/video/BVplaceholder",
        output_dir=tmp_path,
        cookie_file=tmp_path / "cookie.json",
        tool_path=None,
    )
    assert missing_tool.status == "RETRY_INFRA"
    assert "BILIBILI_DOWNLOAD_TOOL_MISSING" in missing_tool.reason_codes
    assert missing_tool.commands == ()


def test_replay_download_command_plan_builds_redacted_yt_dlp_sections(tmp_path):
    ledger = build_source_range_ledger(
        room_id="22966160",
        session_date="2026-06-29",
        segments=[],
        expected_start_ms=60_000,
        expected_end_ms=90_000,
    )
    compensation = plan_bilibili_replay_compensation(ledger, auth_available=True, replay_available=True)
    cookie_file = tmp_path / "cookie.json"

    command_plan = plan_bilibili_replay_download_commands(
        compensation,
        replay_url="https://www.bilibili.com/video/BVplaceholder",
        output_dir=tmp_path / "downloads",
        cookie_file=cookie_file,
        tool_path="/usr/local/bin/yt-dlp",
    )

    assert command_plan.status == "READY"
    assert command_plan.reason_codes == ()
    assert len(command_plan.commands) == 1
    argv = command_plan.commands[0]
    assert argv[:2] == ("/usr/local/bin/yt-dlp", "https://www.bilibili.com/video/BVplaceholder")
    assert "--download-sections" in argv
    assert "*00:01:00.000-00:01:30.000" in argv
    assert "--cookies" in argv
    assert str(cookie_file) in argv
    manifest = command_plan.to_manifest()
    assert str(cookie_file) not in str(manifest)
    assert "[REDACTED_COOKIE_FILE]" in str(manifest)
