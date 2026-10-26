import json
from pathlib import Path

from scripts import auto_review_shadow_daemon as daemon


def _write(path: Path, content: str | bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    return path


def _write_date_dir(
    root: Path,
    *,
    room_id: str = "123456",
    date: str = "2026-06-25",
    stem: str = "1s_test-",
    publish_extra: dict | None = None,
    evidence_data: dict | None = None,
    with_jingting_done: bool = True,
) -> Path:
    date_dir = root / room_id / date
    publish_json = {
        "title": stem,
        "video_path": str(date_dir / f"{stem}.flv"),
        "cover_path": str(date_dir / f"{stem}.cover.png"),
        "subtitle_path": str(date_dir / "subtitles" / f"{stem}.srt"),
        "evidence_path": str(date_dir / "evidence" / f"{stem}.evidence.json"),
        "upload_enabled": False,
    }
    if publish_extra:
        publish_json.update(publish_extra)
    _write(date_dir / f"{stem}.flv", b"fake flv\n")
    _write(date_dir / f"{stem}.cover.png", b"fake cover\n")
    _write(date_dir / "subtitles" / f"{stem}.srt", "1\n00:00:00,000 --> 00:00:01,000\n你好\n")
    _write(date_dir / "evidence" / f"{stem}.evidence.json", json.dumps(evidence_data or {}, ensure_ascii=False) + "\n")
    _write(date_dir / f"{stem}.jingting.srt", "1\n00:00:00,000 --> 00:00:01,000\n你好\n")
    if with_jingting_done:
        _write(date_dir / f"{stem}.jingting.done", "{}\n")
    _write(date_dir / f"{stem}.jingting.manifest.json", json.dumps({"provider": "agy", "agy_rc": 0, "model": "Gemini", "provider_fallback_used": False}) + "\n")
    _write(date_dir / f"{stem}.publish.json", json.dumps(publish_json, ensure_ascii=False) + "\n")
    return date_dir


def test_daemon_directory_lock_is_exclusive_and_released(tmp_path):
    lock_path = tmp_path / "reports" / ".lidousha_auto_review_shadow_123456.lock"

    assert daemon.acquire_directory_lock(lock_path) is True
    assert daemon.acquire_directory_lock(lock_path) is False
    assert lock_path.joinpath("pid").read_text(encoding="utf-8").strip()

    daemon.release_directory_lock(lock_path)

    assert not lock_path.exists()
    assert daemon.acquire_directory_lock(lock_path) is True
    daemon.release_directory_lock(lock_path)


def test_daemon_directory_lock_reclaims_zombie_pid_lock(tmp_path, monkeypatch):
    lock_path = tmp_path / "reports" / ".lidousha_auto_review_shadow_123456.lock"
    lock_path.mkdir(parents=True)
    lock_path.joinpath("pid").write_text("12345\n", encoding="utf-8")
    lock_path.joinpath("created_at").write_text("2026-07-01T00:00:00+00:00\n", encoding="utf-8")

    monkeypatch.setattr(daemon.os, "getpid", lambda: 99999)
    monkeypatch.setattr(daemon.os, "kill", lambda pid, signal: None)
    monkeypatch.setattr(daemon, "_proc_state", lambda pid: "Z", raising=False)

    assert daemon.acquire_directory_lock(lock_path) is True
    assert lock_path.joinpath("pid").read_text(encoding="utf-8").strip() == "99999"

    daemon.release_directory_lock(lock_path)


def test_shadow_output_dir_adds_suffix_when_timestamp_collides(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon, "_shadow_timestamp", lambda: "20260630T012602Z")
    existing = tmp_path / "shadow" / "123456-2026-06-29-20260630T012602Z"
    existing.mkdir(parents=True)
    (existing / "sentinel.auto_review.done").write_text("sentinel\n", encoding="utf-8")

    output_dir = daemon._shadow_output_dir(tmp_path, room_id="123456", date_name="2026-06-29")

    assert output_dir.name == "123456-2026-06-29-20260630T012602Z-01"
    assert not output_dir.exists()
    assert (existing / "sentinel.auto_review.done").exists()


def test_run_once_skips_unchanged_inputs_without_rerunning_pipeline(tmp_path, monkeypatch):
    videos_root = tmp_path / "Videos"
    report_root = tmp_path / "reports"
    _write_date_dir(videos_root)
    source_media = _write(videos_root / "123456" / "2026-06-25" / "123456_20260625-19-00-00.m4s", b"source media")
    monkeypatch.setattr(daemon, "_probe_duration", lambda path: 60.0 if path == source_media else 1.0)
    calls: list[str] = []

    def fake_run_shadow_pipeline(*, review_package, output_dir, no_upload):
        calls.append(str(output_dir))
        output_dir.mkdir(parents=True, exist_ok=True)
        summary = {
            "counts": {"candidates_evaluated": 1, "auto_upload": 0, "auto_recut": 0, "block": 1, "drop": 0, "retry": 0},
            "gap_summary": {"JINGTING_REVIEW_REQUIRED": 1},
            "validations": {"no_upload": no_upload, "no_upload_or_free_deploy_performed": no_upload},
        }
        (output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False) + "\n", encoding="utf-8")
        return summary

    monkeypatch.setattr(daemon, "run_shadow_pipeline", fake_run_shadow_pipeline)

    first = daemon.run_once(
        videos_root=videos_root,
        report_root=report_root,
        room_id="123456",
        date="2026-06-25",
        require_jingting_complete=True,
        force=False,
    )
    second = daemon.run_once(
        videos_root=videos_root,
        report_root=report_root,
        room_id="123456",
        date="2026-06-25",
        require_jingting_complete=True,
        force=False,
    )

    assert first["status"] == "ran"
    assert second["status"] == "skipped"
    assert second["reason"] == "unchanged"
    assert second["last_output_dir"] == first["output_dir"]
    assert len(calls) == 1
    persisted_summary = json.loads((Path(first["output_dir"]) / "summary.json").read_text(encoding="utf-8"))
    persisted_integrity = json.loads((Path(first["output_dir"]) / "source_integrity.json").read_text(encoding="utf-8"))
    assert persisted_summary["source_integrity"]["ledger"]["schema_version"] == "source-range-ledger.v1"
    assert persisted_integrity == persisted_summary["source_integrity"]


def test_run_once_reports_source_integrity_before_jingting_gate(tmp_path, monkeypatch):
    videos_root = tmp_path / "Videos"
    report_root = tmp_path / "reports"
    date_dir = _write_date_dir(videos_root, date="2026-06-29", stem="1s_missing_jingting-")
    (date_dir / "1s_missing_jingting-.jingting.done").unlink()
    source_media = _write(date_dir / "123456_20260629-19-00-11.m4s", b"tiny")
    _write(date_dir / "123456_20260629.danmaku.jsonl", '{"timeline_ms":7100000,"text":"still live"}\n')

    def fake_probe_duration(path: Path):
        if path == source_media:
            return 4.04
        return 42.0

    monkeypatch.setattr(daemon, "_probe_duration", fake_probe_duration)

    result = daemon.run_once(
        videos_root=videos_root,
        report_root=report_root,
        room_id="123456",
        date="2026-06-29",
        require_jingting_complete=True,
        force=False,
    )

    assert result["status"] == "skipped"
    assert result["reason"] == "jingting_incomplete"
    integrity = result["source_integrity"]
    ledger = integrity["ledger"]
    plan = integrity["compensation_plan"]
    issue_codes = {issue["code"] for issue in ledger["issues"]}
    assert ledger["compensation_required"] is True
    assert ledger["can_use_local_source"] is False
    assert "DANMAKU_OUTRUNS_MEDIA" in issue_codes
    assert "MEDIA_STALLED_WHILE_DANMAKU_ADVANCES" in issue_codes
    assert plan["status"] == "BLOCKED"
    assert "BILIBILI_REPLAY_AUTH_REQUIRED" in plan["reason_codes"]


def test_run_once_uses_full_session_selector_when_no_prepared_slices_exist(tmp_path, monkeypatch):
    videos_root = tmp_path / "Videos"
    report_root = tmp_path / "reports"
    date_dir = videos_root / "123456" / "2026-06-30"
    source_video = _write(date_dir / "sources" / "123456_20260630-20-00-00.mp4", b"full source bytes\n")
    source_srt = _write(
        date_dir / "sources" / "123456_20260630-20-00-00.srt",
        (
            "1\n00:00:00,000 --> 00:00:02,000\n晚上好我先调一下麦\n\n"
            "2\n00:00:10,000 --> 00:00:12,000\n我跟你们说一个事\n\n"
            "3\n00:00:20,000 --> 00:00:24,000\n然后她突然发来一句话\n\n"
            "4\n00:00:35,000 --> 00:00:39,000\n结果她回我你也是这个表情\n\n"
            "5\n00:00:45,000 --> 00:00:48,000\n哈哈哈哈就很离谱\n"
        ),
    )
    calls: list[dict] = []

    def fake_probe_duration(path: Path):
        return 120.0 if path == source_video else 0.0

    def fake_run_shadow_pipeline(**kwargs):
        calls.append(kwargs)
        output_dir = kwargs["output_dir"]
        output_dir.mkdir(parents=True, exist_ok=True)
        summary = {
            "mode": "live_source",
            "counts": {"candidates_evaluated": 1, "auto_upload": 1, "auto_recut": 0, "block": 0, "drop": 0, "retry": 0},
            "gap_summary": {},
            "validations": {"no_upload": kwargs["no_upload"], "no_upload_or_free_deploy_performed": kwargs["no_upload"]},
        }
        (output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False) + "\n", encoding="utf-8")
        return summary

    monkeypatch.setattr(daemon, "_probe_duration", fake_probe_duration)
    monkeypatch.setattr(daemon, "run_shadow_pipeline", fake_run_shadow_pipeline)
    _seed_recording_complete(report_root, source_video)

    result = daemon.run_once(
        videos_root=videos_root,
        report_root=report_root,
        room_id="123456",
        date="2026-06-30",
        require_jingting_complete=True,
        force=False,
    )

    assert result["status"] == "ran"
    assert result["full_session_selector"]["recording_completion"]["complete"] is True
    assert result["route_counts"] == {
        "review_package_candidates": 0,
        "live_source_candidates": 1,
        "missing_jingting_done_unroutable": 0,
    }
    assert result["full_session_selector"]["status"] == "SELECTED"
    assert result["full_session_selector"]["candidate_count"] == 1
    assert result["validations"]["no_upload"] is True
    assert result["validations"]["no_upload_or_free_deploy_performed"] is True
    assert len(calls) == 1
    call = calls[0]
    assert call["source_video"] == source_video
    assert call["source_srt"] == source_srt
    job = call["source_context_job"]
    assert job["candidate_id"] == "fullctx_10000_48000"
    assert job["timeline"]["anchor_start_ms"] == 10_000
    assert job["timeline"]["anchor_end_ms"] == 48_000
    assert job["timeline"]["context_start_ms"] == 10_000
    assert job["timeline"]["context_duration_ms"] == 38_000
    assert call["no_upload"] is True


def test_date_source_integrity_uses_replacement_source_when_it_covers_expected_range(tmp_path, monkeypatch):
    videos_root = tmp_path / "Videos"
    date_dir = _write_date_dir(videos_root, date="2026-06-30", stem="1s_replacement_fixture-")
    first = _write(date_dir / "123456_20260630-20-00-00.m4s", b"tiny")
    second = _write(date_dir / "123456_20260630-20-10-00.m4s", b"recovered")
    replacement_a = _write(date_dir / "replacement_source" / "BVfull" / "01-BVfull_p1.remux.mp4", b"a" * (128 * 1024))
    replacement_b = _write(date_dir / "replacement_source" / "BVfull" / "02-BVfull_p2.remux.mp4", b"b" * (128 * 1024))

    def fake_probe_duration(path: Path):
        if path == first:
            return 4.0
        if path == second:
            return 60.0
        if path == replacement_a:
            return 300.0
        if path == replacement_b:
            return 370.0
        return 42.0

    monkeypatch.setattr(daemon, "_probe_duration", fake_probe_duration)

    integrity = daemon.build_date_source_integrity(date_dir, room_id="123456", slices=daemon.discover_slices(date_dir))

    assert integrity["ledger"]["can_use_local_source"] is True
    assert integrity["ledger"]["compensation_required"] is False
    assert integrity["ledger"]["missing_ranges"] == []
    assert integrity["compensation_plan"]["status"] == "NOT_REQUIRED"
    assert integrity["replacement_source"]["status"] == "COVERS_EXPECTED_RANGE"
    assert integrity["replacement_source"]["duration_ms"] >= integrity["ledger"]["expected_range"]["duration_ms"]
    assert len(integrity["replacement_source"]["parts"]) == 2


def test_date_source_integrity_uses_recording_filenames_to_find_wall_clock_gap(tmp_path, monkeypatch):
    videos_root = tmp_path / "Videos"
    date_dir = _write_date_dir(videos_root, date="2026-06-29", stem="1s_gap_fixture-")
    first = _write(date_dir / "123456_20260629-19-00-11.m4s", b"tiny")
    second = _write(date_dir / "123456_20260629-21-35-13.m4s", b"recovered")

    def fake_probe_duration(path: Path):
        if path == first:
            return 4.04
        if path == second:
            return 60.0
        return 42.0

    monkeypatch.setattr(daemon, "_probe_duration", fake_probe_duration)
    slices = daemon.discover_slices(date_dir)

    integrity = daemon.build_date_source_integrity(date_dir, room_id="123456", slices=slices)

    ledger = integrity["ledger"]
    issue_codes = {issue["code"] for issue in ledger["issues"]}
    assert "MEDIA_COVERAGE_GAP" in issue_codes
    assert ledger["missing_ranges"] == [
        {"start_ms": 68_415_040, "end_ms": 77_713_000, "duration_ms": 9_297_960}
    ]
    assert ledger["expected_range"]["start_ms"] == 68_411_000
    assert ledger["expected_range"]["end_ms"] == 77_773_000


def test_run_once_routes_missing_jingting_done_via_live_source_when_metadata_is_present(tmp_path, monkeypatch):
    videos_root = tmp_path / "Videos"
    report_root = tmp_path / "reports"
    source_video = tmp_path / "sources" / "full-session.mp4"
    source_srt = tmp_path / "sources" / "full-session.srt"
    _write(source_video, b"source bytes\n")
    _write(source_srt, "1\n00:00:00,000 --> 00:02:00,000\n完整场次\n")
    _write_date_dir(
        videos_root,
        date="2026-06-30",
        stem="1s_live_source-",
        publish_extra={
            "source_video": str(source_video),
            "source_srt": str(source_srt),
            "anchor_start_ms": 12_000,
            "anchor_end_ms": 18_000,
        },
        with_jingting_done=False,
    )
    calls: list[dict] = []

    def fake_run_shadow_pipeline(**kwargs):
        calls.append(kwargs)
        output_dir = kwargs["output_dir"]
        output_dir.mkdir(parents=True, exist_ok=True)
        summary = {
            "mode": "live_source" if kwargs.get("source_video") else "review_package",
            "counts": {"candidates_evaluated": 1, "auto_upload": 0, "auto_recut": 0, "block": 0, "drop": 0, "retry": 0},
            "gap_summary": {},
            "validations": {"no_upload": kwargs["no_upload"], "no_upload_or_free_deploy_performed": kwargs["no_upload"]},
        }
        (output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False) + "\n", encoding="utf-8")
        return summary

    monkeypatch.setattr(daemon, "run_shadow_pipeline", fake_run_shadow_pipeline)

    result = daemon.run_once(
        videos_root=videos_root,
        report_root=report_root,
        room_id="123456",
        date="2026-06-30",
        require_jingting_complete=True,
        force=False,
    )

    assert result["status"] == "ran"
    assert result["route_counts"] == {
        "review_package_candidates": 0,
        "live_source_candidates": 1,
        "missing_jingting_done_unroutable": 0,
    }
    assert len(calls) == 1
    call = calls[0]
    assert call["source_video"] == source_video
    assert call["source_srt"] == source_srt
    assert call["source_context_job"]["anchor_start_ms"] == 12_000
    assert call["source_context_job"]["anchor_end_ms"] == 18_000
    assert call["no_upload"] is True
    assert result["live_source_runs"][0]["summary"]["mode"] == "live_source"
    assert result["live_source_metadata_gaps"] == []



def test_run_once_reports_exact_live_source_fields_when_jingting_done_is_missing_but_source_srt_is_absent(tmp_path, monkeypatch):
    videos_root = tmp_path / "Videos"
    report_root = tmp_path / "reports"
    source_video = tmp_path / "sources" / "full-session.mp4"
    _write(source_video, b"source bytes\n")
    _write_date_dir(
        videos_root,
        date="2026-06-30",
        stem="1s_missing_source_srt-",
        evidence_data={
            "source_video": str(source_video),
            "source_start": 315,
            "source_end": 438,
        },
        with_jingting_done=False,
    )

    monkeypatch.setattr(daemon, "run_shadow_pipeline", lambda **kwargs: (_ for _ in ()).throw(AssertionError("should not run")))

    result = daemon.run_once(
        videos_root=videos_root,
        report_root=report_root,
        room_id="123456",
        date="2026-06-30",
        require_jingting_complete=True,
        force=False,
    )

    assert result["status"] == "skipped"
    assert result["reason"] == "jingting_incomplete"
    assert result["missing_jingting_done"] == ["1s_missing_source_srt-"]
    gap = result["live_source_metadata_gaps"][0]
    assert gap["stem"] == "1s_missing_source_srt-"
    assert gap["missing_fields"] == ["source_srt"]
    assert gap["found_fields"] == ["source_video", "anchor_start_ms", "anchor_end_ms"]
    assert "source_srt (or full_source_srt/full_session_srt/transcript_path equivalent)" in gap["required_upstream_fields"]


def _seed_recording_complete(report_root: Path, source_video: Path) -> None:
    """Simulate a prior daemon sweep that already saw the file at this size."""
    import time as _time

    state_path = report_root / daemon.RECORDING_COMPLETION_STATE_FILENAME
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "observations": {
                    str(source_video): {
                        "size_bytes": source_video.stat().st_size,
                        "observed_epoch": _time.time() - 3_600,
                    }
                }
            }
        ),
        encoding="utf-8",
    )


def test_evaluate_recording_completion_lifecycle():
    first_complete, first = daemon.evaluate_recording_completion(
        None, size_bytes=100, now_epoch=1_000.0, min_quiet_seconds=300.0
    )
    assert first_complete is False and first["reason"] == "first_observation"

    growing_complete, growing = daemon.evaluate_recording_completion(
        first, size_bytes=200, now_epoch=1_100.0, min_quiet_seconds=300.0
    )
    assert growing_complete is False and growing["reason"] == "still_growing"

    short_complete, short = daemon.evaluate_recording_completion(
        growing, size_bytes=200, now_epoch=1_200.0, min_quiet_seconds=300.0
    )
    assert short_complete is False and short["reason"] == "quiet_window_short"
    # the quiet window anchors at the first same-size observation
    assert short["observed_epoch"] == 1_100.0

    stable_complete, stable = daemon.evaluate_recording_completion(
        short, size_bytes=200, now_epoch=1_500.0, min_quiet_seconds=300.0
    )
    assert stable_complete is True and stable["reason"] == "stable"
    assert stable["quiet_seconds"] >= 300.0


def test_run_once_skips_while_recording_is_still_growing(tmp_path, monkeypatch):
    videos_root = tmp_path / "Videos"
    report_root = tmp_path / "reports"
    date_dir = videos_root / "123456" / "2026-06-30"
    _write(date_dir / "sources" / "s.mp4", b"still growing bytes\n")
    _write(
        date_dir / "sources" / "s.srt",
        "1\n00:00:10,000 --> 00:00:12,000\n我跟你们说一个事\n\n2\n00:00:45,000 --> 00:00:48,000\n哈哈哈哈就很离谱\n",
    )
    monkeypatch.setattr(daemon, "_probe_duration", lambda path: 120.0)

    result = daemon.run_once(
        videos_root=videos_root,
        report_root=report_root,
        room_id="123456",
        date="2026-06-30",
        require_jingting_complete=True,
        force=False,
    )

    assert result["status"] == "skipped"
    assert result["reason"] == "recording_in_progress"
    assert result["recording_completion"]["reason"] == "first_observation"


def test_full_session_routes_fall_back_to_recall_and_stamp_glossary(tmp_path, monkeypatch):
    date_dir = tmp_path / "Videos" / "654321" / "2026-07-02"
    _write(date_dir / "sources" / "s.mp4", b"src\n")
    # No channel setup markers; one dense singing run => primary selector empty.
    blocks = []
    cursor = 50_000
    for index in range(18):
        start = cursor
        end = cursor + 5_200

        def _ts(ms):
            h, rem = divmod(ms, 3_600_000)
            m, rem = divmod(rem, 60_000)
            s, msec = divmod(rem, 1_000)
            return f"{h:02d}:{m:02d}:{s:02d},{msec:03d}"

        blocks.append(f"{index + 1}\n{_ts(start)} --> {_ts(end)}\n江湖难测侠骨柔情红颜梦第{index}句\n")
        cursor += 6_200
    source_srt = _write(date_dir / "sources" / "s.srt", "\n".join(blocks))
    lexicon_path = _write(
        tmp_path / "term_lexicon.json",
        json.dumps({"schema_version": "lidousha-term-lexicon.v1", "overrides": [{"canonical": "甲甲", "aliases": ["阿呆熊"]}]}),
    )
    monkeypatch.setenv("VTUBER_SLICE_TERM_LEXICON", str(lexicon_path))
    monkeypatch.setattr(daemon, "_probe_duration", lambda path: 200.0)

    routes, summary = daemon._full_session_live_source_routes(date_dir, room_id="654321")

    assert summary["status"] == "SELECTED"
    assert summary["selector_stage"] == "fallback_recall"
    assert routes
    job = routes[0].source_context_job
    assert job["selector_stage"] == "fallback_recall"
    assert job["glossary_path"] == str(lexicon_path.resolve())
    assert len(job["glossary_sha256"]) == 64
