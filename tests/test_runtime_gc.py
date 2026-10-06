"""Real-byte retirement, reconstruction, drift and cached replay regressions."""

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("runtime_gc", ROOT / "scripts/runtime_gc.py")
gc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gc)


def sha(p):
    return "sha256:" + hashlib.sha256(p.read_bytes()).hexdigest()


def fixture(tmp_path):
    base = tmp_path / "pipeline"
    base.mkdir()
    source = base / "source.wav"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=2",
            "-y",
            str(source),
        ],
        check=True,
    )
    request = {
        "schema_version": "nested-source-audio-alignment-request.v1",
        "source_media": {
            "path": str(source),
            "sha256": sha(source),
            "bytes": source.stat().st_size,
        },
    }
    request_sha = gc.canonical_sha(request)
    root = base / "private-runs" / "alignment"
    job = root / request_sha.removeprefix("sha256:")
    job.mkdir(parents=True)
    (root / "alignment.lock").touch()
    raw = job / "source-8k-stereo.f32le"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(source),
            "-map",
            "0:a:0",
            "-ar",
            "8000",
            "-ac",
            "2",
            "-c:a",
            "pcm_f32le",
            "-f",
            "f32le",
            str(raw),
        ],
        check=True,
    )
    result = {
        "schema_version": "nested-media-source-audio-alignment-result.v1",
        "request_sha256": request_sha,
        "alignment_evidence": {
            "extraction": {
                "source_media": request["source_media"],
                "source_pcm": {
                    "path": str(raw),
                    "sha256": sha(raw),
                    "bytes": raw.stat().st_size,
                    "returncode": 0,
                    "sample_rate_hz": 8000,
                    "channels": 2,
                },
            }
        },
    }
    gc.write_json(job / "INPUT.json", request)
    gc.write_json(job / "RESULT.json", result)
    gc.write_json(
        job / "PROCESS.json",
        {
            "schema_version": "nested-source-audio-alignment-process.v1",
            "status": "COMPLETE",
            "request_sha256": request_sha,
            "result_path": str(job / "RESULT.json"),
            "result_sha256": sha(job / "RESULT.json"),
        },
    )
    return base, job, source, raw


def collect(base, raw, **kw):
    return gc.collect_pcm(
        raw, base, base / "gc-state", time.monotonic() + 20, busy=lambda *_: None, **kw
    )


def test_proof_only_then_exact_recovery_and_retained_evidence(tmp_path):
    base, job, source, raw = fixture(tmp_path)
    original = raw.read_bytes()
    evidence = {p.name: p.read_bytes() for p in job.glob("*.json")}
    assert collect(base, raw)["status"] == "PROVED_ONLY"
    assert raw.read_bytes() == original
    record = collect(base, raw, apply=True)
    assert record["status"] == "REMOVED"
    assert not raw.exists()
    assert {p.name: p.read_bytes() for p in job.glob("*.json")} == evidence
    restored = base / "restored.f32le"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(source),
            "-map",
            "0:a:0",
            "-ar",
            "8000",
            "-ac",
            "2",
            "-c:a",
            "pcm_f32le",
            "-f",
            "f32le",
            str(restored),
        ],
        check=True,
    )
    assert restored.read_bytes() == original
    receipts = list((base / "gc-state" / "recoveries").glob("*.json"))
    assert len(receipts) == 1 and json.loads(receipts[0].read_text())["preimage"]["sha256"] == sha(
        restored
    )


@pytest.mark.parametrize(
    "damage",
    ["running", "receipt", "source", "raw", "hardlink", "symlink", "open", "decode", "lock"],
)
def test_unsafe_or_unrecoverable_is_kept(tmp_path, damage):
    base, job, source, raw = fixture(tmp_path)
    kw = {}
    held = None
    if damage == "running":
        p = job / "PROCESS.json"
        d = json.loads(p.read_text())
        d["status"] = "RUNNING"
        gc.write_json(p, d)
    elif damage == "receipt":
        p = job / "RESULT.json"
        p.write_text(p.read_text() + " ")
    elif damage == "source":
        source.write_bytes(source.read_bytes() + b"drift")
    elif damage == "raw":
        raw.write_bytes(b"bad")
    elif damage == "hardlink":
        os.link(raw, job / "alias")
    elif damage == "symlink":
        saved = raw.with_suffix(".saved")
        raw.rename(saved)
        raw.symlink_to(saved)
    elif damage == "open":
        kw["busy"] = lambda *_: (_ for _ in ()).throw(gc.Keep("open file"))
    elif damage == "decode":
        kw["decoder"] = lambda *_: ("sha256:" + "0" * 64, raw.stat().st_size, [])
    elif damage == "lock":
        held = (job.parent / "alignment.lock").open("r")
        gc.fcntl.flock(held, gc.fcntl.LOCK_EX | gc.fcntl.LOCK_NB)
    try:
        with pytest.raises((ValueError, OSError)):
            gc.collect_pcm(
                raw,
                base,
                base / "gc-state",
                time.monotonic() + 20,
                apply=True,
                **({"busy": lambda *_: None} | kw),
            )
        assert raw.exists()
        assert not list((base / "gc-state").glob("recoveries/*.json"))
    finally:
        if held:
            held.close()


def test_inventory_deduplicates_hardlinks_and_never_enters_cloud(tmp_path):
    base = tmp_path / "pipeline"
    (base / "out").mkdir(parents=True)
    p = base / "out" / "x_source.mp4"
    p.write_bytes(b"1234")
    (base / "reports").mkdir()
    os.link(p, base / "reports" / "copy.mp4")
    (base / "private-runs").symlink_to("/tmp")
    rows, candidates, errors = gc.inventory(base, time.monotonic() + 20)
    assert sum(r["files"] for r in rows.values()) == 1
    assert candidates == [] and errors


def test_cached_alignment_replay_survives_scratch_collection(tmp_path):
    alignment = pytest.importorskip("src.autoslice.nested_source_audio_alignment")
    base, job, _, raw = fixture(tmp_path)
    request = json.loads((job / "INPUT.json").read_text())
    collect(base, raw, apply=True)
    cached = alignment._replay_cached(request, job / "INPUT.json", job)
    assert cached["replayed"] is True
    assert not raw.exists()


@pytest.mark.skipif(
    not Path("/proc/self/fd").exists() or os.geteuid() != 0,
    reason="real complete Linux /proc census requires root",
)
def test_real_open_fd_owner_is_kept(tmp_path):
    base, job, _, raw = fixture(tmp_path)
    child = subprocess.Popen(
        [
            "python3",
            "-c",
            'import sys,time; f=open(sys.argv[1],"rb"); print("ready",flush=True); time.sleep(20)',
            str(raw),
        ],
        stdout=subprocess.PIPE,
    )
    try:
        assert child.stdout.readline() == b"ready\n"
        with pytest.raises(gc.Keep, match="active process|open"):
            gc.collect_pcm(raw, base, base / "gc-state", time.monotonic() + 20, apply=True)
        assert raw.exists()
    finally:
        child.terminate()
        child.wait(timeout=5)


def test_post_unlink_receipt_failure_reports_removed_subset(tmp_path, monkeypatch):
    base, job, _, raw = fixture(tmp_path)
    parent_identity = (job.stat().st_dev, job.stat().st_ino)
    original_fsync = gc.os.fsync

    def fail_target_directory(fd):
        s = os.fstat(fd)
        if (s.st_dev, s.st_ino) == parent_identity:
            raise OSError("simulated post-unlink directory sync failure")
        return original_fsync(fd)

    monkeypatch.setattr(gc.os, "fsync", fail_target_directory)
    record = collect(base, raw, apply=True)
    assert not raw.exists()
    assert record["status"] == "REMOVED"
    assert "receipt_update_error" in record
    receipts = list((base / "gc-state" / "recoveries").glob("*.json"))
    assert json.loads(receipts[0].read_text())["status"] == "PREPARED"
