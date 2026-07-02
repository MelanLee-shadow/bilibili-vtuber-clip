import json
from pathlib import Path

from scripts import gemini_slice_jingting as jingting


def _write(path: Path, content: str | bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    return path


def test_agy_subprocess_env_sets_home_when_daemon_environment_omits_it(monkeypatch):
    monkeypatch.delenv("HOME", raising=False)

    env = jingting.agy_subprocess_env()

    assert env["HOME"]
    assert env["HOME"].startswith("/")


def test_pending_slices_skip_retry_marker_by_default(tmp_path):
    room = "22966160"
    date_dir = tmp_path / room / "2026-06-29"
    stem = "1s_test_22966160_2026-06-29-22-05-02-"
    video = _write(date_dir / f"{stem}.flv", b"fake video")
    _write(date_dir / "subtitles" / f"{stem}.srt", "1\n00:00:00,000 --> 00:00:01,000\n你好\n")
    _write(date_dir / f"{stem}.jingting.retry.json", json.dumps({"error": "agy failed"}) + "\n")

    pending = jingting.pending_slices(str(tmp_path), room, "2026-06-29", False)
    retry_pending = jingting.pending_slices(str(tmp_path), room, "2026-06-29", False, retry_failed=True)

    assert str(video) not in pending
    assert str(video) in retry_pending


def test_directory_lock_is_exclusive_and_released(tmp_path):
    lock = tmp_path / "slice.jingting.lock"

    assert jingting.acquire_directory_lock(lock) is True
    assert jingting.acquire_directory_lock(lock) is False
    assert lock.joinpath("pid").exists()

    jingting.release_directory_lock(lock)

    assert not lock.exists()
    assert jingting.acquire_directory_lock(lock) is True
    jingting.release_directory_lock(lock)
