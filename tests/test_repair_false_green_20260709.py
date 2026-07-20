import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from scripts import authorized_upload
from scripts import repair_false_green_20260709 as repair
from src.autoslice.song_repair import AGY_AUDIO_LRC_OBSERVATION_SCHEMA_VERSION


INCIDENT_SOURCE_PAYLOAD = b"background-song-source"


@pytest.fixture(autouse=True)
def _pin_fixture_incident_source_hash(monkeypatch):
    """Keep tiny fixture media while exercising the production hash pin."""

    monkeypatch.setattr(
        repair,
        "INCIDENT_FULL_SOURCE_SHA256",
        hashlib.sha256(INCIDENT_SOURCE_PAYLOAD).hexdigest(),
    )


def _json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _wait_for_path(path: Path, process: subprocess.Popen[str], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        if process.poll() is not None:
            raise AssertionError(f"process exited before creating {path}: rc={process.returncode}")
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {path}")


def test_direct_script_bootstraps_repo_imports_from_an_unrelated_cwd(tmp_path):
    script = Path(repair.__file__).resolve()
    probe = (
        "import runpy\n"
        f"ns = runpy.run_path({str(script)!r})\n"
        "try:\n"
        "    ns['_validate_background_performance']({}, schema_version='agy-audio-lrc-observation.v4', observations=[], "
        "first_lyric_start_ms=0, last_lyric_end_ms=1)\n"
        "except ImportError as exc:\n"
        "    print(exc)\n"
        "    raise SystemExit(1)\n"
        "except Exception:\n"
        "    raise SystemExit(0)\n"
        "raise SystemExit(2)\n"
    )
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)

    completed = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr


def _authorized_upload_args(tmp_path: Path, *, lock: Path, uploader: Path) -> list[str]:
    video = tmp_path / "lock-video.mp4"
    cover = tmp_path / "lock-cover.png"
    video.write_bytes(b"lock-video")
    cover.write_bytes(b"lock-cover")
    manifest = tmp_path / "lock.upload_manifest.json"
    assert authorized_upload.main(
        [
            "make-manifest",
            "--video",
            str(video),
            "--cover",
            str(cover),
            "--title",
            "lock integration",
            "--quote",
            "test authorization",
            "--out",
            str(manifest),
        ]
    ) == 0
    return [
        sys.executable,
        str(Path(authorized_upload.__file__).resolve()),
        "upload",
        "--manifest",
        str(manifest),
        "--ledger",
        str(tmp_path / "lock-ledger.jsonl"),
        "--lock",
        str(lock),
        # 本测试标的是锁竞争契约；season 流程在真子进程里无法 monkeypatch
        # （会打真 API），由 test_authorized_upload_season.py 专门覆盖。
        "--skip-season",
        "--uploader",
        str(uploader),
    ]


def _incident_fixture(
    tmp_path: Path,
    *,
    negative_candidate_id: str = repair.INCIDENT_RERUN_CANDIDATE_ID,
    source_path: Path | None = None,
    source_payload: bytes = INCIDENT_SOURCE_PAYLOAD,
) -> dict[str, object]:
    base = tmp_path / "autoslice"
    repo = base / "repo"
    (base / "DISABLED").parent.mkdir(parents=True)
    (base / "DISABLED").write_text("disabled for acceptance\n", encoding="utf-8")
    delivery = repo / "lidousha" / repair.DATE
    superseded = delivery / "_superseded" / "歌切_芽吹くとき.manual-rerun-report.json"
    previous_quarantine = repo / repair.INCIDENT_PREVIOUS_QUARANTINE_RELATIVE
    previous_quarantine.mkdir(parents=True)
    previous_quarantine_files = [
        previous_quarantine / name
        for name in sorted(repair.INCIDENT_PREVIOUS_QUARANTINE_FILENAMES)
    ]
    for path in previous_quarantine_files:
        path.write_bytes(f"already quarantined: {path.name}".encode("utf-8"))
    v4 = {
        "schema_version": "manual-rerun.v1",
        "status": "completed",
        "item": {
            "segment_path": (
                "/root/clouddrive2/CloudNAS/CloudDrive/123云盘/live-streaming/22966160/2026-07-09/"
                "22966160_2026-07-09-22-30-19-.mp4"
            ),
            "anchor_start_ms": 166_220,
            "anchor_end_ms": 321_760,
            "seg_dur_ms": repair.INCIDENT_SEGMENT_DURATION_MS,
        },
        "result": {
            "candidate_id": repair.INCIDENT_RERUN_CANDIDATE_ID,
            "start_ms": repair.INCIDENT_RETRY_START_MS,
            "end_ms": repair.INCIDENT_RETRY_END_MS,
            "retried_full_source": True,
            "song_complete": True,
            "delivered": str(delivery / "old-false-green.mp4"),
            "title": "【李豆沙】豆沙歌，《芽吹くとき》",
        },
        "final_acceptance": {
            "status": "ACCEPTED_NO_UPLOAD",
            "state_repaired": True,
            "cover": {"status": "AI_COVER_READY"},
            "old_package_quarantine": str(previous_quarantine),
            "quarantined_files": [str(path) for path in previous_quarantine_files],
        },
    }
    v4_path = base / "reports" / "manual_rerun_2026-07-09_mebukutoki_v4.json"
    _json(v4_path, v4)
    superseded.parent.mkdir(parents=True, exist_ok=True)
    superseded.write_bytes(v4_path.read_bytes())

    picks = [
        {
            "candidate_id": f"talk_{index}",
            "status": "review_ready",
            "hook": f"talk hook {index}",
            "title": f"【李豆沙】talk {index}",
            "start_ms": 0,
            "end_ms": 60_000,
            "summary": {},
        }
        for index in range(5)
    ]
    state = {
        "status": "review_ready",
        "picks": picks,
        "segments_done": ["one"],
        "segments_dead": {},
        "pending_talk": [],
        "pending_song": [],
        "songs": [
            {
                "candidate_id": "song_200028_507",
                "status": "blocked",
                "decision": "BLOCK",
                "reason_codes": ["END_BOUNDARY_LOW"],
                "danmaku": 8,
            },
            {
                "candidate_id": repair.TARGET_CANDIDATE_ID,
                "status": "review_ready",
                "decision": "AUTO_RECUT",
                "reason_codes": ["SONG_FULL_BOUNDARY_READY"],
                "song_complete": True,
                "lyrics_alignment_ready": True,
                "delivered": str(delivery / "old-false-green.mp4"),
                "delivered_sidecars": {"subtitle": str(delivery / "old.srt")},
                "title": "【李豆沙】豆沙歌，《芽吹くとき》",
                "preview": "下播前演唱 yonige《芽吹くとき》",
                "cover_status": "AI_COVER_READY",
                "song_completion_evidence": {"ready": True},
                "segment": "recording.mp4",
                "start_ms": 1000,
                "end_ms": 2000,
                "danmaku": 18,
                "rc": 0,
                "window_classified_song": True,
                "supersedes_false_green": {
                    "quarantine": str(previous_quarantine),
                },
            },
        ],
    }
    active_state = base / "state" / f"{repair.DATE}.json"
    state_bak = base / "state" / f"{repair.DATE}.json.bak"
    _json(active_state, state)
    _json(state_bak, {**state, "updated_at": "older"})
    latest = base / "reports" / "latest.md"
    latest.write_text("# stale latest\n", encoding="utf-8")
    summary = delivery / "AUTOSLICE_SUMMARY.md"
    summary.parent.mkdir(parents=True, exist_ok=True)
    tail = (
        "## 2026-07-10 Ivan 审片点名执行\n\n"
        "这段人工处置必须按字节保留。\n"
    ).encode("utf-8")
    summary.write_bytes(b"# stale automatic front\n\n" + tail)

    source = source_path or (base / repair.INCIDENT_FULL_SOURCE_RELATIVE)
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(source_payload)
    run_root = base / "out" / "acceptance" / "negative-run"
    ledger = base / "reports" / "upload_ledger.jsonl"
    unrelated = json.dumps(
        {"artifact_id": "unrelated", "manifest": "/other/clip.upload_manifest.json", "rc": 0},
        ensure_ascii=False,
    ) + "\n"
    ledger.write_text(unrelated, encoding="utf-8")
    snapshot = base / "forensics" / "no-upload-snapshots" / "pytest-negative.json"
    assert repair.main(
        [
            "--base",
            str(base),
            "--repo-root",
            str(repo),
            "--ledger",
            str(ledger),
            "--capture-no-upload-snapshot",
            str(snapshot),
            "--planned-run-id",
            negative_candidate_id,
            "--planned-run-root",
            str(run_root),
            "--planned-source-video",
            str(source),
        ]
    ) == 0
    candidate_dir = run_root / negative_candidate_id
    repair_report = candidate_dir / "song_repair" / f"{negative_candidate_id}.song-repair.json"
    performance = {
        "mode": repair.BACKGROUND_MODE,
        "confidence": 0.99,
        "continuous_live_song_performance": True,
        "background_recording_likelihood": 0.99,
        "same_lidousha_live_performer_across_all_lyrics": False,
        "other_singer_or_harmony_present": False,
        "recorded_or_playback_vocal_present": True,
        "evidence": [
            {"time_ms": 1000, "observation": "background at head"},
            {"time_ms": 5000, "observation": "background at middle"},
            {"time_ms": 9000, "observation": "background at tail"},
        ],
        "notes": "the same mastered background recording continues across the full lyric span",
    }
    agy_job = repair_report.parent / "agy_audio_lrc" / f"{negative_candidate_id}-fixture"
    agy_job.mkdir(parents=True)
    agy_source = agy_job / "input.mp4"
    agy_source.write_bytes(source.read_bytes())
    agy_lrc = agy_job / "source.lrc"
    agy_lrc.write_text(
        "".join(f"[00:{index + 1:02d}.000]line {index}\n" for index in range(9)),
        encoding="utf-8",
    )
    agy_prompt = agy_job / "prompt.md"
    agy_prompt.write_text("fixture strict AGY background observation\n", encoding="utf-8")
    observations = [
        {
            "lrc_index": index,
            "lrc_time_ms": (index + 1) * 1000,
            "text": f"line {index}",
            "heard": True,
            "live_start_ms": (index + 1) * 1000,
            "live_end_ms": (index + 1) * 1000 + 500,
            "confidence": 0.95,
            "lyric_vocal_subject": "RECORDED_OR_PLAYBACK_SINGER",
            "lidousha_role": "SILENT_OR_NOT_AUDIBLE",
            "same_live_vocal_source_as_lidousha": False,
            "other_singer_or_harmony_audible": False,
            "recorded_or_playback_vocal_audible": True,
        }
        for index in range(9)
    ]
    agy_output = agy_job / "alignment.json"
    _json(
        agy_output,
        {
            "schema_version": AGY_AUDIO_LRC_OBSERVATION_SCHEMA_VERSION,
            "record": {
                "attempt_id": agy_job.name,
                "candidate_id": negative_candidate_id,
                "source_sha256": _sha(agy_source),
                "lrc_sha256": _sha(agy_lrc),
                "source_duration_ms": repair.INCIDENT_FULL_SOURCE_DURATION_MS,
            },
            "observations": observations,
            "spot_checks": [],
            "live_performance": performance,
            "post_song_talk_start_ms": None,
        },
    )
    _json(
        agy_job / "run.manifest.json",
        {
            "schema_version": "agy-audio-lrc-run.v1",
            "attempt_id": agy_job.name,
            "candidate_id": negative_candidate_id,
            "provider": "agy",
            "model": "Gemini 3.5 Flash (High)",
            "provider_fallback_used": False,
            "agy_rc": 0,
            "sandbox": True,
            "artifacts": {
                "source_origin_path": str(source.resolve()),
                "source_path": str(agy_source),
                "source_sha256": _sha(agy_source),
                "source_duration_ms": repair.INCIDENT_FULL_SOURCE_DURATION_MS,
                "lrc_path": str(agy_lrc),
                "lrc_sha256": _sha(agy_lrc),
                "prompt_path": str(agy_prompt),
                "prompt_sha256": _sha(agy_prompt),
                "output_path": str(agy_output),
                "output_sha256": _sha(agy_output),
            },
        },
    )
    _json(
        repair_report,
        {
            "schema_version": "song-repair-report.v1",
            "repaired": False,
            "attempts": [],
            "song_boundary": None,
            "lyrics_alignment": None,
            "reason_codes": sorted(repair.REQUIRED_REASONS),
            "live_performance": performance,
        },
    )
    negative = run_root / "summary.json"
    record = {
        "candidate_id": negative_candidate_id,
        "candidate_dir": str(candidate_dir),
        "decision_action": "BLOCK",
        "reason_codes": sorted(repair.REQUIRED_REASONS),
        "boundary_resolution": {
            "action": "BLOCK",
            "reason_codes": sorted(repair.REQUIRED_REASONS),
        },
        "source_context_job": {
            "schema_version": "source-context-job-from-full-session-candidate.v1",
            "candidate_id": negative_candidate_id,
            "content_type_hint": "song",
            "song_candidate": True,
            "requires_full_source_song_boundary_redo": True,
            "timeline": {
                "source_duration_ms": repair.INCIDENT_FULL_SOURCE_DURATION_MS,
                "anchor_start_ms": repair.INCIDENT_LOCAL_ANCHOR_START_MS,
                "anchor_end_ms": repair.INCIDENT_LOCAL_ANCHOR_END_MS,
                "context_start_ms": 0,
                "context_end_ms": repair.INCIDENT_FULL_SOURCE_DURATION_MS,
                "context_duration_ms": repair.INCIDENT_FULL_SOURCE_DURATION_MS,
            },
            "song_repair_gate": {
                "status": "BLOCKED",
                "reason_codes": sorted(repair.REQUIRED_REASONS),
                "live_performance": performance,
                "repair_report_path": str(repair_report),
            },
        },
    }
    _json(
        negative,
        {
            "schema_version": "full-session-selector-cpa-shadow-run.v1",
            "source_video": str(source),
            "no_upload": True,
            "records": [record],
            "last_shadow_summary": {
                "records": [
                    {
                        "decision_action": "BLOCK",
                        "reason_codes": sorted(repair.REQUIRED_REASONS),
                        "boundary_resolution": {
                            "action": "BLOCK",
                            "reason_codes": sorted(repair.REQUIRED_REASONS),
                        },
                        "source_context_job": record["source_context_job"],
                    }
                ]
            },
        },
    )
    return {
        "base": base,
        "repo": repo,
        "superseded": superseded,
        "active_state": active_state,
        "state_bak": state_bak,
        "v4": v4_path,
        "latest": latest,
        "summary": summary,
        "tail": tail,
        "negative": negative,
        "source": source,
        "ledger": ledger,
        "snapshot": snapshot,
        "agy_output": agy_output,
        "agy_manifest": agy_job / "run.manifest.json",
        "previous_quarantine": previous_quarantine,
    }


def _args(fixture: dict[str, object], *extra: str) -> list[str]:
    return [
        "--base",
        str(fixture["base"]),
        "--repo-root",
        str(fixture["repo"]),
        "--negative-result",
        str(fixture["negative"]),
        "--superseded-v4-report",
        str(fixture["superseded"]),
        "--ledger",
        str(fixture["ledger"]),
        "--no-upload-snapshot",
        str(fixture["snapshot"]),
        "--expected-source-sha256",
        _sha(fixture["source"]),
        "--transaction-id",
        "pytest-tx",
        *extra,
    ]


def test_default_plan_is_read_only_and_lists_state_last(tmp_path):
    fixture = _incident_fixture(tmp_path)
    before = {name: Path(path).read_bytes() for name, path in fixture.items() if isinstance(path, Path) and path.is_file()}

    assert repair.main(_args(fixture)) == 0

    after = {name: Path(path).read_bytes() for name, path in fixture.items() if name in before}
    assert after == before
    assert not list((fixture["base"] / "forensics").glob("false-green-20260709-*"))
    assert not (fixture["base"] / "reports" / "manual_rerun_2026-07-09_mebukutoki_v5.json").exists()


def test_committed_literal_v3_incident_evidence_remains_plan_recoverable(tmp_path):
    fixture = _incident_fixture(tmp_path)

    def to_literal_v3(performance: dict[str, object]) -> None:
        performance["continuous_singing"] = performance.pop("continuous_live_song_performance")
        performance["same_lidousha_live_singer_across_all_lyrics"] = performance.pop(
            "same_lidousha_live_performer_across_all_lyrics"
        )

    raw = json.loads(fixture["agy_output"].read_text(encoding="utf-8"))
    raw["schema_version"] = "agy-audio-lrc-observation.v3"
    to_literal_v3(raw["live_performance"])
    _json(fixture["agy_output"], raw)
    manifest = json.loads(fixture["agy_manifest"].read_text(encoding="utf-8"))
    manifest["artifacts"]["output_sha256"] = _sha(fixture["agy_output"])
    _json(fixture["agy_manifest"], manifest)

    negative = json.loads(fixture["negative"].read_text(encoding="utf-8"))
    outer_gate = negative["records"][0]["source_context_job"]["song_repair_gate"]
    inner_gate = negative["last_shadow_summary"]["records"][0]["source_context_job"]["song_repair_gate"]
    for gate in (outer_gate, inner_gate):
        to_literal_v3(gate["live_performance"])
    repair_report = Path(outer_gate["repair_report_path"])
    report = json.loads(repair_report.read_text(encoding="utf-8"))
    to_literal_v3(report["live_performance"])
    _json(repair_report, report)
    _json(fixture["negative"], negative)

    assert repair.main(_args(fixture)) == 0
    assert not list((fixture["base"] / "forensics").glob("false-green-20260709-*"))


def test_snapshot_materializes_durable_empty_ledger_inode_when_absent(tmp_path):
    base = tmp_path / "autoslice"
    (base / "DISABLED").parent.mkdir(parents=True)
    (base / "DISABLED").write_text("disabled\n", encoding="utf-8")
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    ledger = base / "reports" / "upload_ledger.jsonl"
    snapshot = base / "forensics" / "no-upload-snapshots" / "absent-ledger.json"

    assert repair.main(
        [
            "--base",
            str(base),
            "--capture-no-upload-snapshot",
            str(snapshot),
            "--planned-run-id",
            "absent_ledger_run",
            "--planned-run-root",
            str(base / "out" / "acceptance" / "absent-ledger-run"),
            "--planned-source-video",
            str(source),
        ]
    ) == 0

    payload = json.loads(snapshot.read_text(encoding="utf-8"))
    assert ledger.is_file() and ledger.read_bytes() == b""
    assert payload["ledger_existed"] is True
    assert isinstance(payload["ledger_device"], int)
    assert isinstance(payload["ledger_inode"], int)


def test_unfinished_upload_intent_blocks_repair_globally(tmp_path, capsys):
    fixture = _incident_fixture(tmp_path)
    intent = {
        "event": "UPLOAD_ATTEMPT_STARTED",
        "attempt_id": "a" * 32,
        "artifact_id": "unrelated-artifact",
        "video_sha256": "b" * 64,
        "cover_sha256": "c" * 64,
        "title": "unrelated upload",
        "authorized_by": "Ivan",
        "authorization_quote": "test",
        "manifest": "/tmp/opaque.upload_manifest.json",
        "manifest_sha256": "d" * 64,
        "uploader": "/tmp/uploader",
        "at": "2026-07-10T12:00:00+0000",
    }
    with fixture["ledger"].open("a", encoding="utf-8") as sink:
        sink.write(json.dumps(intent) + "\n")

    assert repair.main(_args(fixture, "--apply")) == 2
    assert "unfinished upload intent" in capsys.readouterr().err
    assert not list((fixture["base"] / "forensics").glob("false-green-20260709-*"))


def test_apply_commits_six_file_cas_and_preserves_manual_tail(tmp_path):
    fixture = _incident_fixture(tmp_path)
    old_bytes = {
        "active_state": fixture["active_state"].read_bytes(),
        "state_backup": fixture["state_bak"].read_bytes(),
        "v4_report": fixture["v4"].read_bytes(),
        "superseded_v4_report": fixture["superseded"].read_bytes(),
        "latest_report": fixture["latest"].read_bytes(),
        "summary": fixture["summary"].read_bytes(),
    }

    assert repair.main(_args(fixture, "--apply")) == 0

    assert fixture["active_state"].read_bytes() == fixture["state_bak"].read_bytes()
    state = json.loads(fixture["active_state"].read_text(encoding="utf-8"))
    assert state["status"] == "review_ready"
    assert state["song_quarantine_intervals"] == [
        {
            "segment_path": (
                "/root/clouddrive2/CloudNAS/CloudDrive/123云盘/live-streaming/22966160/2026-07-09/"
                "22966160_2026-07-09-22-30-19-.mp4"
            ),
            "start_ms": 121_220,
            "end_ms": 366_760,
            "original_anchor_start_ms": 166_220,
            "original_anchor_end_ms": 321_760,
            "candidate_id": repair.TARGET_CANDIDATE_ID,
            "reason_code": "SONG_INTERVAL_REQUIRES_JOINT_SINGING_PROOF",
        }
    ]
    assert len(state["songs"]) == 2
    assert sum(song.get("status") == "blocked" for song in state["songs"]) == 2
    assert sum(bool(song.get("delivered")) for song in state["songs"]) == 0
    repaired_song = next(song for song in state["songs"] if song["candidate_id"] == repair.TARGET_CANDIDATE_ID)
    assert repaired_song["decision"] == "BLOCK"
    assert repaired_song["song_complete"] is False
    assert repaired_song["full_source_performer_rejection"] is True
    assert repaired_song["reason_codes"] == sorted(repair.REQUIRED_REASONS)
    assert repaired_song["song_repair_gate"]["live_performance"]["mode"] == repair.BACKGROUND_MODE
    assert "delivered" not in repaired_song
    assert "title" not in repaired_song
    assert "cover_status" not in repaired_song
    assert "log" not in repaired_song
    assert "rc" not in repaired_song
    tombstone = next(item for item in state["superseded_song_records"] if item["candidate_id"] == repair.TARGET_CANDIDATE_ID)
    assert tombstone["disposition"] == repair.REVOCATION_STATUS
    assert "delivered" not in json.dumps(tombstone, ensure_ascii=False)

    summary_bytes = fixture["summary"].read_bytes()
    assert summary_bytes[summary_bytes.index(repair.TAIL_MARKER) :] == fixture["tail"]
    summary_text = summary_bytes.decode("utf-8")
    assert "歌 **0 交付** · 2 被完整性门拦截 · 共尝试 2" in summary_text
    assert "背景音乐/原曲播放/SONG_PARTIAL 均不交付" in summary_text
    latest_text = fixture["latest"].read_text(encoding="utf-8")
    assert "歌切: 0 交付 / 2 门拦 / 2 尝试" in latest_text
    assert f"交付: {fixture['repo']}/lidousha/{repair.DATE}/" in latest_text
    assert "repair-20260709-render-" not in latest_text

    for path in (fixture["v4"], fixture["superseded"]):
        tombstone_report = json.loads(path.read_text(encoding="utf-8"))
        assert tombstone_report["status"] == repair.REVOCATION_STATUS
        assert tombstone_report["song_complete"] is False
        rendered = path.read_text(encoding="utf-8")
        assert "ACCEPTED_NO_UPLOAD" not in rendered
        assert "AI_COVER_READY" not in rendered
        assert "下播前演唱" not in rendered

    v5_path = fixture["base"] / "reports" / "manual_rerun_2026-07-09_mebukutoki_v5.json"
    v5 = json.loads(v5_path.read_text(encoding="utf-8"))
    assert v5["status"] == repair.V5_STATUS
    assert v5["no_upload_verification"]["status"] == "PASSED_RUN_SCOPED"
    assert v5["no_upload_verification"]["run_id"] == repair.INCIDENT_RERUN_CANDIDATE_ID
    assert v5["fresh_negative_result"]["sha256"] == _sha(fixture["negative"])

    tx_dir = fixture["base"] / "forensics" / "false-green-20260709-pytest-tx"
    manifest = json.loads((tx_dir / "manifest.json").read_text(encoding="utf-8"))
    journal = json.loads((tx_dir / "journal.json").read_text(encoding="utf-8"))
    assert manifest["cas_target_count"] == 6
    assert manifest["apply_order"] == list(repair.AUTHORITY_TARGET_ORDER)
    assert manifest["apply_order"][0] == "state_backup"
    assert manifest["apply_order"][-1] == "active_state"
    assert journal["state"] == "COMMITTED"
    for name, original in old_bytes.items():
        backup = Path(manifest["targets"][name]["backup_path"])
        assert backup.read_bytes() == original
        assert manifest["targets"][name]["backup_sha256"] == hashlib.sha256(original).hexdigest()


def test_run_specific_ledger_match_refuses_without_mutation(tmp_path, capsys):
    fixture = _incident_fixture(tmp_path)
    before_state = fixture["active_state"].read_bytes()
    with fixture["ledger"].open("a", encoding="utf-8") as sink:
        sink.write(
            json.dumps(
                {
                    "manifest": f"/tmp/{json.loads(fixture['negative'].read_text())['records'][0]['candidate_id']}.upload_manifest.json",
                    "rc": 0,
                }
            )
            + "\n"
        )

    assert repair.main(_args(fixture, "--apply")) == 2
    assert "run-specific no-upload proof failed" in capsys.readouterr().err
    assert fixture["active_state"].read_bytes() == before_state
    assert not list((fixture["base"] / "forensics").glob("false-green-20260709-*"))


def test_source_hash_only_ledger_match_refuses_without_mutation(tmp_path, capsys):
    fixture = _incident_fixture(tmp_path)
    before_state = fixture["active_state"].read_bytes()
    with fixture["ledger"].open("a", encoding="utf-8") as sink:
        sink.write(
            json.dumps(
                {
                    "video_sha256": _sha(fixture["source"]),
                    "manifest": "/tmp/unrelated-name.upload_manifest.json",
                    "title": "unrelated-looking title",
                    "rc": 0,
                }
            )
            + "\n"
        )

    assert repair.main(_args(fixture, "--apply")) == 2
    assert "run-specific no-upload proof failed" in capsys.readouterr().err
    assert fixture["active_state"].read_bytes() == before_state
    assert not list((fixture["base"] / "forensics").glob("false-green-20260709-*"))


def test_repair_lock_makes_authorized_uploader_refuse_without_queueing(tmp_path):
    base = tmp_path / "autoslice"
    base.mkdir()
    entered = tmp_path / "uploader-entered"
    uploader = tmp_path / "uploader.sh"
    uploader.write_text(f"#!/bin/sh\nprintf entered > {str(entered)!r}\nexit 0\n", encoding="utf-8")
    uploader.chmod(0o755)
    command = _authorized_upload_args(tmp_path, lock=base / "upload.lock", uploader=uploader)

    with repair.upload_lock(base):
        process = subprocess.Popen(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stdout, stderr = process.communicate(timeout=5)
        assert process.returncode == 4, (stdout, stderr)
        assert not entered.exists()
        assert not (tmp_path / "lock-ledger.jsonl").exists()
    assert "upload/repair lock is busy" in stderr


def test_active_authorized_uploader_makes_repair_refuse_busy_lock(tmp_path, capsys):
    fixture = _incident_fixture(tmp_path)
    entered = tmp_path / "holding-uploader-entered"
    release = tmp_path / "release-uploader"
    uploader = tmp_path / "holding-uploader.sh"
    uploader.write_text(
        "#!/bin/sh\n"
        f"printf entered > {str(entered)!r}\n"
        f"while [ ! -e {str(release)!r} ]; do sleep 0.02; done\n"
        "exit 0\n",
        encoding="utf-8",
    )
    uploader.chmod(0o755)
    command = _authorized_upload_args(
        tmp_path,
        lock=fixture["base"] / "upload.lock",
        uploader=uploader,
    )
    process = subprocess.Popen(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        _wait_for_path(entered, process)
        assert repair.main(_args(fixture)) == 2
        assert "authorized uploader lock is busy" in capsys.readouterr().err
        assert not list((fixture["base"] / "forensics").glob("false-green-20260709-*"))
    finally:
        release.touch()
        stdout, stderr = process.communicate(timeout=5)
    assert process.returncode == 0, (stdout, stderr)


def test_raw_agy_evidence_must_cover_lyric_head_middle_tail(tmp_path, capsys):
    fixture = _incident_fixture(tmp_path)
    bad_evidence = [
        {"time_ms": 1000, "observation": "head only one"},
        {"time_ms": 2000, "observation": "head only two"},
        {"time_ms": 3000, "observation": "head only three"},
    ]

    raw = json.loads(fixture["agy_output"].read_text(encoding="utf-8"))
    raw["live_performance"]["evidence"] = bad_evidence
    _json(fixture["agy_output"], raw)
    manifest = json.loads(fixture["agy_manifest"].read_text(encoding="utf-8"))
    manifest["artifacts"]["output_sha256"] = _sha(fixture["agy_output"])
    _json(fixture["agy_manifest"], manifest)

    negative = json.loads(fixture["negative"].read_text(encoding="utf-8"))
    outer_gate = negative["records"][0]["source_context_job"]["song_repair_gate"]
    inner_gate = negative["last_shadow_summary"]["records"][0]["source_context_job"]["song_repair_gate"]
    outer_gate["live_performance"]["evidence"] = bad_evidence
    inner_gate["live_performance"]["evidence"] = bad_evidence
    repair_report = Path(outer_gate["repair_report_path"])
    report = json.loads(repair_report.read_text(encoding="utf-8"))
    report["live_performance"]["evidence"] = bad_evidence
    _json(repair_report, report)
    _json(fixture["negative"], negative)

    assert repair.main(_args(fixture, "--apply")) == 2
    assert "head, middle, and tail" in capsys.readouterr().err
    assert not list((fixture["base"] / "forensics").glob("false-green-20260709-*"))


def test_streamer_talking_over_recorded_vocal_is_valid_incident_rejection(tmp_path):
    fixture = _incident_fixture(tmp_path)
    mode = "STREAMER_TALKING_OVER_MUSIC"

    raw = json.loads(fixture["agy_output"].read_text(encoding="utf-8"))
    raw["live_performance"]["mode"] = mode
    _json(fixture["agy_output"], raw)
    manifest = json.loads(fixture["agy_manifest"].read_text(encoding="utf-8"))
    manifest["artifacts"]["output_sha256"] = _sha(fixture["agy_output"])
    _json(fixture["agy_manifest"], manifest)

    negative = json.loads(fixture["negative"].read_text(encoding="utf-8"))
    outer_gate = negative["records"][0]["source_context_job"]["song_repair_gate"]
    inner_gate = negative["last_shadow_summary"]["records"][0]["source_context_job"]["song_repair_gate"]
    outer_gate["live_performance"]["mode"] = mode
    inner_gate["live_performance"]["mode"] = mode
    repair_report = Path(outer_gate["repair_report_path"])
    report = json.loads(repair_report.read_text(encoding="utf-8"))
    report["live_performance"]["mode"] = mode
    _json(repair_report, report)
    _json(fixture["negative"], negative)

    assert repair.main(_args(fixture, "--apply")) == 0
    state = json.loads(fixture["active_state"].read_text(encoding="utf-8"))
    repaired_song = next(
        song for song in state["songs"] if song["candidate_id"] == repair.TARGET_CANDIDATE_ID
    )
    assert repaired_song["song_repair_gate"]["live_performance"]["mode"] == mode


def test_one_recorded_row_cannot_authorize_mostly_lidousha_singing_incident_repair(tmp_path, capsys):
    fixture = _incident_fixture(tmp_path)
    raw = json.loads(fixture["agy_output"].read_text(encoding="utf-8"))
    raw["live_performance"]["mode"] = "STREAMER_TALKING_OVER_MUSIC"
    for row in raw["observations"][1:]:
        row["lyric_vocal_subject"] = "LIDOUSHA"
        row["lidousha_role"] = "SINGING_THIS_LYRIC"
        row["same_live_vocal_source_as_lidousha"] = True
        row["recorded_or_playback_vocal_audible"] = False
    _json(fixture["agy_output"], raw)
    manifest = json.loads(fixture["agy_manifest"].read_text(encoding="utf-8"))
    manifest["artifacts"]["output_sha256"] = _sha(fixture["agy_output"])
    _json(fixture["agy_manifest"], manifest)

    negative = json.loads(fixture["negative"].read_text(encoding="utf-8"))
    outer_gate = negative["records"][0]["source_context_job"]["song_repair_gate"]
    inner_gate = negative["last_shadow_summary"]["records"][0]["source_context_job"]["song_repair_gate"]
    outer_gate["live_performance"]["mode"] = "STREAMER_TALKING_OVER_MUSIC"
    inner_gate["live_performance"]["mode"] = "STREAMER_TALKING_OVER_MUSIC"
    repair_report = Path(outer_gate["repair_report_path"])
    report = json.loads(repair_report.read_text(encoding="utf-8"))
    report["live_performance"]["mode"] = "STREAMER_TALKING_OVER_MUSIC"
    _json(repair_report, report)
    _json(fixture["negative"], negative)

    assert repair.main(_args(fixture, "--apply")) == 2
    assert "does not exclusively prove recorded playback" in capsys.readouterr().err
    assert not list((fixture["base"] / "forensics").glob("false-green-20260709-*"))


def test_conflicting_song_ready_reason_cannot_authorize_incident_repair(tmp_path, capsys):
    fixture = _incident_fixture(tmp_path)
    negative = json.loads(fixture["negative"].read_text(encoding="utf-8"))
    outer = negative["records"][0]
    inner = negative["last_shadow_summary"]["records"][0]
    for record in (outer, inner):
        record["reason_codes"].append("SONG_FULL_BOUNDARY_READY")
        record["boundary_resolution"]["reason_codes"].append("SONG_FULL_BOUNDARY_READY")
        record["source_context_job"]["song_repair_gate"]["reason_codes"].append(
            "SONG_FULL_BOUNDARY_READY"
        )
    repair_report = Path(outer["source_context_job"]["song_repair_gate"]["repair_report_path"])
    report = json.loads(repair_report.read_text(encoding="utf-8"))
    report["reason_codes"].append("SONG_FULL_BOUNDARY_READY")
    _json(repair_report, report)
    _json(fixture["negative"], negative)

    assert repair.main(_args(fixture, "--apply")) == 2
    assert "not the exact incident hard rejection" in capsys.readouterr().err
    assert not list((fixture["base"] / "forensics").glob("false-green-20260709-*"))


def test_agy_source_origin_must_match_selector_source(tmp_path, capsys):
    fixture = _incident_fixture(tmp_path)
    unrelated = tmp_path / "unrelated-source.mp4"
    unrelated.write_bytes(fixture["source"].read_bytes())
    manifest = json.loads(fixture["agy_manifest"].read_text(encoding="utf-8"))
    manifest["artifacts"]["source_origin_path"] = str(unrelated.resolve())
    _json(fixture["agy_manifest"], manifest)

    assert repair.main(_args(fixture, "--apply")) == 2
    assert "source origin is not the selector source video" in capsys.readouterr().err
    assert not list((fixture["base"] / "forensics").glob("false-green-20260709-*"))


def test_self_consistent_agy_from_other_media_cannot_bind_to_incident_source(tmp_path, capsys):
    fixture = _incident_fixture(tmp_path)
    manifest = json.loads(fixture["agy_manifest"].read_text(encoding="utf-8"))
    agy_source = Path(manifest["artifacts"]["source_path"])
    agy_source.write_bytes(b"unrelated-background-media-used-only-by-agy")
    unrelated_sha = _sha(agy_source)
    manifest["artifacts"]["source_sha256"] = unrelated_sha

    raw = json.loads(fixture["agy_output"].read_text(encoding="utf-8"))
    raw["record"]["source_sha256"] = unrelated_sha
    _json(fixture["agy_output"], raw)
    manifest["artifacts"]["output_sha256"] = _sha(fixture["agy_output"])
    _json(fixture["agy_manifest"], manifest)

    assert repair.main(_args(fixture, "--apply")) == 2
    assert "AGY input media is not the immutable incident full-source bytes" in capsys.readouterr().err
    assert not list((fixture["base"] / "forensics").glob("false-green-20260709-*"))


@pytest.mark.parametrize("extra", [(), ("--apply",)])
def test_unrelated_background_candidate_cannot_authorize_plan_or_apply(tmp_path, capsys, extra):
    fixture = _incident_fixture(
        tmp_path,
        negative_candidate_id="unrelated_background_song_negative",
    )

    assert repair.main(_args(fixture, *extra)) == 2
    assert "not the verified incident rerun candidate" in capsys.readouterr().err
    assert not list((fixture["base"] / "forensics").glob("false-green-20260709-*"))


def test_byte_identical_background_at_unrelated_origin_cannot_authorize_apply(tmp_path, capsys):
    unrelated_source = tmp_path / "other-session" / "background.mp4"
    fixture = _incident_fixture(tmp_path, source_path=unrelated_source)

    assert repair.main(_args(fixture, "--apply")) == 2
    assert "fresh negative incident full-source video" in capsys.readouterr().err
    assert not list((fixture["base"] / "forensics").glob("false-green-20260709-*"))


def test_operator_asserted_hash_for_unrelated_bytes_cannot_authorize_apply(tmp_path, capsys):
    fixture = _incident_fixture(
        tmp_path,
        source_payload=b"different-background-negative-with-self-consistent-evidence",
    )

    # _incident_fixture binds the snapshot, selector, AGY source copy, and CLI
    # assertion to these bytes.  Only the immutable incident hash disagrees.
    assert repair.main(_args(fixture, "--apply")) == 2
    assert "not the immutable incident full-source hash" in capsys.readouterr().err
    assert not list((fixture["base"] / "forensics").glob("false-green-20260709-*"))


def test_self_consistent_negative_with_unrelated_seed_range_cannot_authorize_apply(tmp_path, capsys):
    fixture = _incident_fixture(tmp_path)
    negative = json.loads(fixture["negative"].read_text(encoding="utf-8"))
    for record in (negative["records"][0], negative["last_shadow_summary"]["records"][0]):
        timeline = record["source_context_job"]["timeline"]
        timeline["anchor_start_ms"] += 1_000
        timeline["anchor_end_ms"] += 1_000
    _json(fixture["negative"], negative)

    assert repair.main(_args(fixture, "--apply")) == 2
    assert "seed timeline is not the incident retry range" in capsys.readouterr().err
    assert not list((fixture["base"] / "forensics").glob("false-green-20260709-*"))


def test_negative_source_context_schema_drift_cannot_authorize_apply(tmp_path, capsys):
    fixture = _incident_fixture(tmp_path)
    negative = json.loads(fixture["negative"].read_text(encoding="utf-8"))
    for record in (negative["records"][0], negative["last_shadow_summary"]["records"][0]):
        record["source_context_job"]["schema_version"] = "wrong-source-context-schema.v1"
    _json(fixture["negative"], negative)

    assert repair.main(_args(fixture, "--apply")) == 2
    assert "not the immutable incident song seed" in capsys.readouterr().err
    assert not list((fixture["base"] / "forensics").glob("false-green-20260709-*"))


def test_incident_report_rejects_source_segment_duration_drift(tmp_path):
    fixture = _incident_fixture(tmp_path)
    v4 = json.loads(fixture["v4"].read_text(encoding="utf-8"))
    v4["item"]["seg_dur_ms"] = 340_000
    _json(fixture["v4"], v4)
    fixture["superseded"].write_bytes(fixture["v4"].read_bytes())
    paths = repair.authority_paths(fixture["base"], fixture["repo"], fixture["superseded"])
    inputs, _hashes = repair.read_authority_inputs(paths)

    with pytest.raises(repair.RepairError, match="not the immutable source-segment duration"):
        repair.validate_known_false_green_inputs(inputs, repo_root=fixture["repo"])


def test_unrelated_existing_quarantine_cannot_hide_an_active_false_green_path(tmp_path):
    fixture = _incident_fixture(tmp_path)
    unexpected = fixture["repo"] / "lidousha" / repair.DATE / "_quarantine" / "unrelated"
    unexpected.mkdir(parents=True)
    (unexpected / "hidden.mp4").write_bytes(b"not the pinned previous quarantine")
    state = json.loads(fixture["active_state"].read_text(encoding="utf-8"))
    target = next(song for song in state["songs"] if song["candidate_id"] == repair.TARGET_CANDIDATE_ID)
    target["supersedes_false_green"]["quarantine"] = str(unexpected)
    _json(fixture["active_state"], state)
    paths = repair.authority_paths(fixture["base"], fixture["repo"], fixture["superseded"])
    inputs, _hashes = repair.read_authority_inputs(paths)

    with pytest.raises(repair.RepairError, match="does not point to the exact previously quarantined"):
        repair.validate_known_false_green_inputs(inputs, repo_root=fixture["repo"])


def test_delivered_field_cannot_hide_inside_the_pinned_previous_quarantine(tmp_path):
    fixture = _incident_fixture(tmp_path)
    hidden = next(path for path in fixture["previous_quarantine"].iterdir() if path.suffix == ".mp4")
    state = json.loads(fixture["active_state"].read_text(encoding="utf-8"))
    target = next(song for song in state["songs"] if song["candidate_id"] == repair.TARGET_CANDIDATE_ID)
    target["delivered"] = str(hidden)
    _json(fixture["active_state"], state)
    paths = repair.authority_paths(fixture["base"], fixture["repo"], fixture["superseded"])
    inputs, _hashes = repair.read_authority_inputs(paths)

    with pytest.raises(repair.RepairError, match="still exist in the active delivery root"):
        repair.validate_known_false_green_inputs(inputs, repo_root=fixture["repo"])


def test_previous_quarantine_report_path_must_be_canonical_absolute(tmp_path):
    fixture = _incident_fixture(tmp_path)
    v4 = json.loads(fixture["v4"].read_text(encoding="utf-8"))
    pinned = fixture["previous_quarantine"]
    v4["final_acceptance"]["old_package_quarantine"] = str(
        pinned.parent / ".." / pinned.parent.name / pinned.name
    )
    _json(fixture["v4"], v4)
    fixture["superseded"].write_bytes(fixture["v4"].read_bytes())
    paths = repair.authority_paths(fixture["base"], fixture["repo"], fixture["superseded"])
    inputs, _hashes = repair.read_authority_inputs(paths)

    with pytest.raises(repair.RepairError, match="does not name the exact previously quarantined"):
        repair.validate_known_false_green_inputs(inputs, repo_root=fixture["repo"])


def test_symlinked_delivery_path_cannot_escape_false_green_containment(tmp_path):
    fixture = _incident_fixture(tmp_path)
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"outside")
    active_link = fixture["repo"] / "lidousha" / repair.DATE / "old-false-green.mp4"
    active_link.symlink_to(outside)
    paths = repair.authority_paths(fixture["base"], fixture["repo"], fixture["superseded"])
    inputs, _hashes = repair.read_authority_inputs(paths)

    with pytest.raises(repair.RepairError, match="contains a symlink component"):
        repair.validate_known_false_green_inputs(inputs, repo_root=fixture["repo"])


@pytest.mark.parametrize(
    ("section", "field", "bad_value"),
    [
        ("result", "candidate_id", "other_rerun"),
        ("result", "start_ms", repair.INCIDENT_RETRY_START_MS + 1),
        ("result", "end_ms", repair.INCIDENT_RETRY_END_MS - 1),
        ("item", "segment_path", "/root/clouddrive2/other-recording.mp4"),
        ("item", "anchor_start_ms", repair.INCIDENT_ANCHOR_START_MS + 1),
        ("item", "anchor_end_ms", repair.INCIDENT_ANCHOR_END_MS - 1),
    ],
)
def test_verified_v4_authority_must_match_exact_incident_spec(tmp_path, section, field, bad_value):
    fixture = _incident_fixture(tmp_path)
    v4 = json.loads(fixture["v4"].read_text(encoding="utf-8"))
    v4[section][field] = bad_value
    _json(fixture["v4"], v4)
    fixture["superseded"].write_bytes(fixture["v4"].read_bytes())
    paths = repair.authority_paths(fixture["base"], fixture["repo"], fixture["superseded"])
    inputs, _hashes = repair.read_authority_inputs(paths)

    with pytest.raises(repair.RepairError):
        repair.validate_known_false_green_inputs(inputs, repo_root=fixture["repo"])


def test_recover_rolls_forward_prepared_partial_transaction(tmp_path):
    fixture = _incident_fixture(tmp_path)
    assert repair.main(_args(fixture, "--apply")) == 0
    tx_dir = fixture["base"] / "forensics" / "false-green-20260709-pytest-tx"
    manifest = json.loads((tx_dir / "manifest.json").read_text(encoding="utf-8"))

    # Simulate a PREPARED crash with reports already installed but the active
    # state still at its exact input bytes. Recovery must roll forward, not copy
    # the old forensic backup into state.bak or other staged targets.
    active = Path(manifest["targets"]["active_state"]["path"])
    active.write_bytes(Path(manifest["targets"]["active_state"]["backup_path"]).read_bytes())
    v5_path = Path(manifest["new_v5_report"]["path"])
    v5_path.unlink()
    (tx_dir / "journal.json").write_text(
        json.dumps(
            {
                "repair_txid": "pytest-tx",
                "state": "PREPARED",
                "at": "test",
                "manifest_sha256": _sha(tx_dir / "manifest.json"),
            }
        )
        + "\n",
        encoding="utf-8",
    )

    assert repair.main(
        [
            "--base",
            str(fixture["base"]),
            "--repo-root",
            str(fixture["repo"]),
            "--recover",
            str(tx_dir),
        ]
    ) == 0
    journal = json.loads((tx_dir / "journal.json").read_text(encoding="utf-8"))
    assert journal["state"] == "COMMITTED"
    assert _sha(v5_path) == manifest["new_v5_report"]["staged_sha256"]
    assert _sha(fixture["active_state"]) == manifest["targets"]["active_state"]["staged_sha256"]
    assert fixture["active_state"].read_bytes() == fixture["state_bak"].read_bytes()


def test_recover_refuses_rotated_ledger_even_when_bytes_are_identical(tmp_path, capsys):
    fixture = _incident_fixture(tmp_path)
    assert repair.main(_args(fixture, "--apply")) == 0
    tx_dir = fixture["base"] / "forensics" / "false-green-20260709-pytest-tx"
    manifest = json.loads((tx_dir / "manifest.json").read_text(encoding="utf-8"))
    active = Path(manifest["targets"]["active_state"]["path"])
    active.write_bytes(Path(manifest["targets"]["active_state"]["backup_path"]).read_bytes())
    Path(manifest["new_v5_report"]["path"]).unlink()
    (tx_dir / "journal.json").write_text(
        json.dumps(
            {
                "repair_txid": "pytest-tx",
                "state": "PREPARED",
                "at": "test",
                "manifest_sha256": _sha(tx_dir / "manifest.json"),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    ledger_bytes = fixture["ledger"].read_bytes()
    rotated = fixture["ledger"].with_suffix(".rotated")
    fixture["ledger"].rename(rotated)
    fixture["ledger"].write_bytes(ledger_bytes)

    assert repair.main(
        [
            "--base",
            str(fixture["base"]),
            "--repo-root",
            str(fixture["repo"]),
            "--recover",
            str(tx_dir),
        ]
    ) == 2
    assert "ledger inode changed" in capsys.readouterr().err
    journal = json.loads((tx_dir / "journal.json").read_text(encoding="utf-8"))
    assert journal["state"] == "PREPARED"


def test_recover_refuses_symlinked_superseded_parent_without_external_write(tmp_path, capsys):
    fixture = _incident_fixture(tmp_path)
    assert repair.main(_args(fixture, "--apply")) == 0
    tx_dir = fixture["base"] / "forensics" / "false-green-20260709-pytest-tx"

    superseded_root = fixture["superseded"].parent
    hidden_root = superseded_root.with_name("_superseded.hidden")
    superseded_root.rename(hidden_root)
    escape_root = tmp_path / "escape"
    escape_root.mkdir()
    escaped_report = escape_root / fixture["superseded"].name
    escaped_report.write_bytes((hidden_root / fixture["superseded"].name).read_bytes())
    escaped_before = escaped_report.read_bytes()
    superseded_root.symlink_to(escape_root, target_is_directory=True)

    assert repair.main(
        [
            "--base",
            str(fixture["base"]),
            "--repo-root",
            str(fixture["repo"]),
            "--recover",
            str(tx_dir),
        ]
    ) == 2
    assert "symlink component" in capsys.readouterr().err
    assert escaped_report.read_bytes() == escaped_before


def test_in_instruction_parent_swap_uses_pinned_dirfd_and_never_writes_escape(
    tmp_path, monkeypatch, capsys
):
    fixture = _incident_fixture(tmp_path)
    superseded_root = fixture["superseded"].parent
    hidden_root = superseded_root.with_name("_superseded.hidden")
    escape_root = tmp_path / "instruction-race-escape"
    escape_root.mkdir()
    escaped_report = escape_root / fixture["superseded"].name
    escaped_report.write_bytes(fixture["superseded"].read_bytes())
    escaped_before = escaped_report.read_bytes()

    real_install = repair._atomic_install_at
    calls = 0

    def swapping_install(parent_fd: int, name: str, payload: bytes) -> None:
        nonlocal calls
        calls += 1
        # state.bak, v5, and reports/v4 are calls 1..3.  Swap the parent in
        # the final instruction before installing the superseded v4 target.
        if calls == 4:
            superseded_root.rename(hidden_root)
            superseded_root.symlink_to(escape_root, target_is_directory=True)
        real_install(parent_fd, name, payload)

    monkeypatch.setattr(repair, "_atomic_install_at", swapping_install)
    assert repair.main(_args(fixture, "--apply")) == 2
    assert "symlink component" in capsys.readouterr().err
    assert escaped_report.read_bytes() == escaped_before
    journal = json.loads(
        (
            fixture["base"]
            / "forensics"
            / "false-green-20260709-pytest-tx"
            / "journal.json"
        ).read_text(encoding="utf-8")
    )
    assert journal["state"] == "PREPARED"
