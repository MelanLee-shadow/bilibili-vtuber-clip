from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

from src.autoslice import runtime_storage
from src.autoslice.failed_pick_import import PackageImportError
from src.autoslice.package_import_capacity import copy_capacity
from src.autoslice.produce_dispatch import _capacity_item, produce_batch_windowed
from src.autoslice.song_lane import _runtime_scratch_selector_env
from src.autoslice.talk_lane import _runtime_scratch_child_env


GIB = 1024**3


def _dispatch_kwargs(base: Path, *, free_bytes_fn, **extra):
    def produce_talk(_date, item, **_kwargs):
        Path(item["runtime_scratch_dir"], "recut.tmp").write_bytes(b"temporary")
        return {"candidate_id": item["cid"], "rc": 0, "status": "review_ready"}

    return dict(
        produce_talk_fn=produce_talk,
        produce_song_fn=lambda *_args, **_kwargs: {"rc": 0, "status": "READY"},
        base=base,
        max_parallel=1,
        min_free_bytes=16 * GIB,
        free_bytes_fn=free_bytes_fn,
        operational_reserve_bytes=2 * GIB,
        peak_bytes_per_second=16 * 1024**2,
        log=lambda _message: None,
        talk_pipeline_fingerprint=lambda _cid: "talk",
        pipeline_fingerprint=lambda: "pipeline",
        song_pipeline_fingerprint=lambda: "song",
        song_window_pre_ms=0,
        song_window_post_ms=0,
        **extra,
    ), produce_talk


def test_sufficient_clip_below_pressure_threshold_runs_and_records_request(tmp_path: Path):
    kwargs, produce_talk = _dispatch_kwargs(
        tmp_path,
        free_bytes_fn=lambda _path: 4 * GIB,
    )
    item = {"cid": "clip-1", "start_ms": 0, "end_ms": 1_000}
    result = produce_batch_windowed("2026-10-04", [item], produce_talk, **kwargs)

    assert result[0]["status"] == "review_ready"
    requests = json.loads(
        (tmp_path / "reports/capacity-pressure-requests.v1.json").read_text()
    )
    assert len(requests["requests"]) == 1
    assert requests["requests"][0]["native_housekeeping_session"] == {
        "requested": True,
        "created": False,
    }
    assert json.loads(
        (tmp_path / "runtime-scratch/registry.v1.json").read_text()
    )["entries"] == []


def test_insufficient_peak_yields_without_calling_producer(tmp_path: Path):
    called = []

    def produce(_date, _item, **_kwargs):
        called.append(True)
        return {"rc": 0, "status": "review_ready"}

    result = produce_batch_windowed(
        "2026-10-04",
        [{"cid": "too-large", "start_ms": 0, "end_ms": 300_000}],
        produce,
        produce_talk_fn=produce,
        produce_song_fn=produce,
        base=tmp_path,
        max_parallel=1,
        min_free_bytes=16 * GIB,
        free_bytes_fn=lambda _path: 2 * GIB + 1,
        operational_reserve_bytes=2 * GIB,
        peak_bytes_per_second=16 * 1024**2,
        log=lambda _message: None,
        talk_pipeline_fingerprint=lambda _cid: "talk",
        pipeline_fingerprint=lambda: "pipeline",
        song_pipeline_fingerprint=lambda: "song",
        song_window_pre_ms=0,
        song_window_post_ms=0,
    )

    assert result == []
    assert called == []


def test_parallel_reservations_do_not_oversubscribe(tmp_path: Path):
    admission = runtime_storage.CapacityAdmission(
        runtime_base=tmp_path,
        free_bytes_fn=lambda _path: 100,
        operational_reserve_bytes=10,
        peak_bytes_per_second=1,
    )
    first = admission.reserve_bytes(60)
    second = admission.reserve_bytes(60)

    assert first is not None
    assert second is None
    first.release()
    assert admission.reserve_bytes(60) is not None


def test_bad_probe_is_a_concrete_admission_failure(tmp_path: Path):
    with pytest.raises(runtime_storage.CapacityProbeError):
        runtime_storage.CapacityAdmission(
            runtime_base=tmp_path,
            free_bytes_fn=lambda _path: (_ for _ in ()).throw(OSError("probe down")),
            operational_reserve_bytes=1,
        ).reserve_bytes(1)


def test_invalid_capacity_configuration_is_not_silently_defaulted():
    with pytest.raises(ValueError, match="non-negative integer"):
        runtime_storage.parse_operational_reserve_bytes("two-gib")
    with pytest.raises(ValueError, match="non-negative integer"):
        runtime_storage.parse_peak_bytes_per_second("sixteen-mib")


def test_actual_clip_window_wins_over_source_duration_and_planned_copy_fails_closed(tmp_path: Path):
    source = tmp_path / "hour-long-recording.mp4"
    source.write_bytes(b"read-only source")
    budget = runtime_storage.estimate_peak_allocation(
        {"start_ms": 100, "end_ms": 1_100, "seg_dur_ms": 3_600_000, "segment_path": str(source)},
        peak_bytes_per_second=10,
    )
    assert budget.duration_ms == 1_000
    assert budget.estimated_peak_bytes == 10
    assert budget.known_source_bytes == 0

    with pytest.raises(runtime_storage.CapacityBudgetError, match="not materialized"):
        runtime_storage.estimate_peak_allocation(
            {"start_ms": 0, "end_ms": 1_000, "planned_source_paths": ["/missing/source.mp4"]},
            peak_bytes_per_second=1,
        )

    fractional = runtime_storage.estimate_peak_allocation(
        {"duration": 210.7}, peak_bytes_per_second=10
    )
    assert fractional.duration_ms == 210_700


def test_only_released_owned_workspace_is_reclaimed(tmp_path: Path):
    authoritative = tmp_path / "out" / "final.mp4"
    authoritative.parent.mkdir()
    authoritative.write_bytes(b"keep")
    unknown = tmp_path / "unknown.tmp"
    unknown.write_bytes(b"keep")

    active = runtime_storage.create_ephemeral_workspace(tmp_path, owner="active")
    (active.path / "active.bin").write_bytes(b"active")
    failed = runtime_storage.create_ephemeral_workspace(tmp_path, owner="failed")
    (failed.path / "failed.bin").write_bytes(b"failed")
    failed.release(failed=True)
    completed = runtime_storage.create_ephemeral_workspace(tmp_path, owner="completed")
    payload = completed.path / "payload.bin"
    payload.write_bytes(b"reclaim-me")
    completed.release()

    first = runtime_storage.collect_ephemeral(tmp_path, reason="test-active")
    assert first["reclaimed_bytes"] == len(b"reclaim-me")
    assert active.path.exists()
    assert failed.path.exists()
    assert authoritative.exists()
    assert unknown.exists()
    assert not completed.path.exists()


@pytest.mark.parametrize("env_builder", [_runtime_scratch_child_env, _runtime_scratch_selector_env])
def test_real_child_tempfile_uses_dispatcher_lease_and_is_reclaimed(tmp_path: Path, env_builder):
    created_paths: list[Path] = []
    prior_removed: list[bool] = []

    def produce(_date, item, **_kwargs):
        if created_paths:
            prior_removed.append(not created_paths[-1].exists())
        env = env_builder({}, item)
        child = subprocess.run(
            [
                sys.executable,
                "-c",
                "import tempfile; f=tempfile.NamedTemporaryFile(delete=False, suffix='.mp4'); f.write(b'owned-media-temp'); f.close(); print(f.name)",
            ],
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )
        created = Path(child.stdout.strip())
        created_paths.append(created)
        assert created.read_bytes() == b"owned-media-temp"
        return {"candidate_id": item["cid"], "rc": 0, "status": "review_ready"}

    result = produce_batch_windowed(
        "2026-10-04",
        [
            {"cid": "real-child-0", "start_ms": 0, "end_ms": 1_000},
            {"cid": "real-child-1", "start_ms": 2_000, "end_ms": 3_000},
        ],
        produce,
        produce_talk_fn=produce,
        produce_song_fn=produce,
        base=tmp_path,
        max_parallel=1,
        min_free_bytes=0,
        free_bytes_fn=lambda _path: 4 * GIB,
        operational_reserve_bytes=2 * GIB,
        peak_bytes_per_second=16 * 1024**2,
        log=lambda _message: None,
        talk_pipeline_fingerprint=lambda _cid: "talk",
        pipeline_fingerprint=lambda: "pipeline",
        song_pipeline_fingerprint=lambda: "song",
        song_window_pre_ms=0,
        song_window_post_ms=0,
    )
    assert [row["status"] for row in result] == ["review_ready", "review_ready"]
    assert len(created_paths) == 2
    assert prior_removed == [True]
    assert created_paths[0].parent.name.startswith("work-")
    assert not created_paths[0].exists()
    assert json.loads((tmp_path / "runtime-scratch/registry.v1.json").read_text())["entries"] == []


def test_dispatch_capacity_uses_bounded_padded_windows():
    song = _capacity_item(
        {"anchor_start_ms": 100_000, "anchor_end_ms": 110_000, "seg_dur_ms": 200_000},
        talk_window_pre_ms=10_000,
        talk_window_post_ms=48_000,
        song_window_pre_ms=15_000,
        song_window_post_ms=20_000,
    )
    assert song["capacity_duration_ms"] == 45_000
    talk = _capacity_item(
        {"start_ms": 100_000, "end_ms": 110_000, "seg_dur_ms": 200_000},
        talk_window_pre_ms=10_000,
        talk_window_post_ms=48_000,
        song_window_pre_ms=15_000,
        song_window_post_ms=20_000,
    )
    assert talk["capacity_duration_ms"] == 68_000


def test_package_copy_admission_uses_exact_copy_plus_operational_reserve(tmp_path: Path):
    destination = tmp_path / "package" / "video.mp4"
    entries = [{"status": "WOULD_COPY", "destination": str(destination), "bytes": 90}]

    rows = copy_capacity(
        entries,
        runtime_base=tmp_path,
        operational_reserve_bytes=10,
        pressure_threshold_bytes=0,
        free_bytes_fn=lambda _path: 100,
    )
    assert rows[0]["required_bytes"] == 100

    with pytest.raises(PackageImportError) as error:
        copy_capacity(
            entries,
            runtime_base=tmp_path,
            operational_reserve_bytes=10,
            pressure_threshold_bytes=0,
            free_bytes_fn=lambda _path: 99,
        )
    assert error.value.code == "INSUFFICIENT_DISK_SPACE"
