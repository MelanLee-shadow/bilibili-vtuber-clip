from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from src.autoslice.repository_asset_authority import _canonical_sha256


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "oci3_no_upload_soak.sh"


def _write(path: Path, payload: str | bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, str):
        path.write_text(payload, encoding="utf-8")
    else:
        path.write_bytes(payload)
    path.chmod(mode)


def _manifest(repo: Path, paths: list[str]) -> None:
    entries: dict[str, dict[str, object]] = {}
    for relative in sorted(paths):
        path = repo / relative
        info = path.stat()
        if path.is_dir():
            entries[relative] = {"type": "dir", "mode": stat.S_IMODE(info.st_mode) & ~0o022}
        else:
            entries[relative] = {
                "type": "file",
                "mode": stat.S_IMODE(info.st_mode) & ~0o022,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
    payload = {
        "schema_version": "oci3-shadow-autoslice-repo-manifest.v1",
        "commit": "a" * 40,
        "entries": entries,
    }
    payload["tree_sha256"] = hashlib.sha256(
        json.dumps(entries, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    _write(repo / "DEPLOYED_MANIFEST.json", json.dumps(payload, sort_keys=True) + "\n", 0o644)

    authority_relative = "assets/lidousha/publication_registry.v1.json"
    authority = {
        "schema_version": "deployed-authority-manifest.v1",
        "deployed_commit": "a" * 40,
        "entries": {
            authority_relative: {
                "bytes": (repo / authority_relative).stat().st_size,
                "sha256": "sha256:" + hashlib.sha256((repo / authority_relative).read_bytes()).hexdigest(),
            }
        },
    }
    authority["manifest_sha256"] = _canonical_sha256(authority)
    _write(repo / "DEPLOYED_AUTHORITY_MANIFEST.json", json.dumps(authority, sort_keys=True) + "\n", 0o644)
    _write(repo / "DEPLOYED_COMMIT", "a" * 40 + "\n", 0o644)


def _fixture(tmp_path: Path, *, foreign_cron: str = "") -> tuple[Path, dict[str, str], Path]:
    root = tmp_path / "oci3"
    base = root / "opt/bilive/autoslice"
    repo = base / "repo"
    # Keep the absolute-looking literals visible to the OSS exporter, then
    # make them relative to the synthetic fixture root at runtime.
    mount = root / Path("/path/to/cloud-drive").relative_to("/")
    source = mount / Path("/live-streaming-oci3test/22966160").relative_to("/")
    recorder = root / "opt/bilive/recording"
    base.mkdir(parents=True)
    repo.mkdir(parents=True)
    (repo / "src/autoslice").mkdir(parents=True)
    source.mkdir(parents=True)
    recorder.mkdir(parents=True)
    (base / "models/campp").mkdir(parents=True)
    (base / "voiceprints/lidousha").mkdir(parents=True)
    (base / "logs").mkdir(parents=True)
    mount.mkdir(parents=True, exist_ok=True)

    _write(repo / "scripts/session_autoslice.py", "#!/usr/bin/env python3\n", 0o644)
    _write(repo / "scripts/silero_vad_spans.py", "# fixture\n", 0o644)
    _write(repo / "assets/vad/silero_vad.onnx", b"vad\n", 0o644)
    _write(repo / "assets/lidousha/voiceprint_profile.v1.json", "{}\n", 0o644)
    _write(repo / "assets/lidousha/publication_registry.v1.json", "{}\n", 0o644)
    authority_module = ROOT / "src/autoslice/repository_asset_authority.py"
    shutil.copy2(authority_module, repo / "src/autoslice/repository_asset_authority.py")
    (repo / "src/autoslice/repository_asset_authority.py").chmod(0o644)
    for directory in ("scripts", "src", "src/autoslice", "assets", "assets/vad", "assets/lidousha"):
        (repo / directory).chmod(0o755)
    _manifest(
        repo,
        [
            "scripts",
            "scripts/session_autoslice.py",
            "scripts/silero_vad_spans.py",
            "src",
            "src/autoslice",
            "src/autoslice/repository_asset_authority.py",
            "assets",
            "assets/vad",
            "assets/vad/silero_vad.onnx",
            "assets/lidousha",
            "assets/lidousha/voiceprint_profile.v1.json",
            "assets/lidousha/publication_registry.v1.json",
        ],
    )
    _write(base / "cpa.env", "CPA_BASE_URL=https://cpa.example.test/v1\nCPA_API_KEY=fixture\n", 0o600)
    _write(base / "DISABLED", b"", 0o644)
    _write(
        recorder / "status.json",
        json.dumps(
            {
                "schema_version": "recorder-neutral-status.v1",
                "room_id": "22966160",
                "generated_at_epoch": time.time(),
                "service_reachable": True,
                "live_status": False,
                "error": None,
            }
        )
        + "\n",
        0o644,
    )
    _write(
        recorder / "adapter-state.json",
        json.dumps({"schema_version": "bililive-recorder-adapter-state.v1"}) + "\n",
        0o600,
    )
    agy = root / "root/.local/bin/agy"
    _write(agy, "#!/bin/sh\nprintf 'agy 1.1.22\\n'\n", 0o755)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    mount_probe = bin_dir / "findmnt"
    _write(mount_probe, f"#!/bin/sh\nprintf '%s fuse CloudFS\\n' '{mount}'\n", 0o755)
    flock = bin_dir / "flock"
    _write(flock, "#!/bin/sh\nexit 0\n", 0o755)
    timeout = bin_dir / "timeout"
    _write(timeout, "#!/bin/sh\nshift\nexec \"$@\"\n", 0o755)
    stat_bin = bin_dir / "stat"
    _write(
        stat_bin,
        "#!/bin/sh\n"
        "if [ \"$1\" = -c ]; then\n"
        "  if [ \"$3\" = -- ]; then path=\"$4\"; else path=\"$3\"; fi\n"
        "  python3 - \"$2\" \"$path\" <<'PY_STAT'\n"
        "import os, stat, sys\n"
        "info = os.lstat(sys.argv[2])\n"
        "values = {'%h': info.st_nlink, '%u': info.st_uid, '%g': info.st_gid, '%a': format(stat.S_IMODE(info.st_mode), 'o')}\n"
        "print(values[sys.argv[1]])\n"
        "PY_STAT\n"
        "else\n"
        "  exec /usr/bin/stat \"$@\"\n"
        "fi\n",
        0o755,
    )
    crontab_state = tmp_path / "crontab"
    crontab = bin_dir / "crontab"
    _write(
        crontab,
        "#!/bin/sh\n"
        "if [ \"$1\" = -l ]; then\n"
        f"  if [ -f '{crontab_state}' ]; then cat '{crontab_state}'; exit 0; fi\n"
        "  echo 'no crontab for root' >&2; exit 1\n"
        "fi\n"
        f"cp -- \"$1\" '{crontab_state}'\n",
        0o755,
    )
    if foreign_cron:
        crontab_state.write_text(foreign_cron, encoding="utf-8")
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
        "OCI3_NO_UPLOAD_SOAK_TEST_MODE": "1",
        "OCI3_NO_UPLOAD_SOAK_TEST_ROOT": str(root),
        "OCI3_NO_UPLOAD_SOAK_TEST_MAIN_PYTHON": sys.executable,
        "OCI3_NO_UPLOAD_SOAK_TEST_DIAR_PYTHON": sys.executable,
        "OCI3_NO_UPLOAD_SOAK_TEST_ENGINE_PYTHON": sys.executable,
        "OCI3_NO_UPLOAD_SOAK_TEST_AGY": str(agy),
        "OCI3_NO_UPLOAD_SOAK_TEST_FINDMNT": str(mount_probe),
        "OCI3_NO_UPLOAD_SOAK_TEST_CRONTAB": str(crontab),
        "OCI3_NO_UPLOAD_SOAK_TEST_FLOCK": str(flock),
        "OCI3_NO_UPLOAD_SOAK_TEST_TIMEOUT": str(timeout),
    }
    return root, env, crontab_state


def _run(env: dict[str, str], mode: str, *, extra: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    run_env = dict(env)
    run_env.update(extra or {})
    return subprocess.run([str(SCRIPT), mode], cwd=ROOT, env=run_env, text=True, capture_output=True, check=False)


def _managed_line(crontab_state: Path) -> str:
    return next(line for line in crontab_state.read_text(encoding="utf-8").splitlines() if "AUTOSLICE_OCI3_NO_UPLOAD_SOAK=1" in line)


def test_shell_contract_and_no_upload_pins() -> None:
    assert subprocess.run(["bash", "-n", str(SCRIPT)], check=False).returncode == 0
    source = SCRIPT.read_text(encoding="utf-8")
    assert "AUTOSLICE_IGNORE_LIVE_HOLD=1" not in source
    assert "AUTO_UPLOAD=" not in source
    assert "authorized_upload" not in source
    assert "biliup" not in source
    assert 'SHADOW_TICK_LOCK_PATH="/opt/bilive/autoslice-shadow/tick.lock"' in source
    assert 'SHADOW_RUNNER_LOCK_PATH="/opt/bilive/autoslice-shadow/runner.lock"' in source
    assert '"$SHADOW_TICK_LOCK_PATH" "$SHADOW_RUNNER_LOCK_PATH"' in source


def test_activate_verify_idempotent_and_exact_environment(tmp_path: Path) -> None:
    _root, env, crontab = _fixture(tmp_path, foreign_cron="17 * * * * /usr/bin/true\n")
    activated = _run(env, "activate")
    assert activated.returncode == 0, activated.stderr
    assert not (Path(env["OCI3_NO_UPLOAD_SOAK_TEST_ROOT"]) / "opt/bilive/autoslice/DISABLED").exists()
    line = _managed_line(crontab)
    for token in (
        "*/10 * * * *",
        "flock -n",
        "AUTOSLICE_START_DATE=2026-08-20",
        "AUTOSLICE_MAX_PARALLEL_PRODUCE=1",
        "GEMINI_PAID_BACKUP_DAILY_CAP=0",
        "AUTOSLICE_UPLOAD_ENABLED=0",
        "-u AUTO_UPLOAD",
        "-u GEMINI_PAID_BACKUP_DEV_EXCEPTION",
        "-u AUTOSLICE_IGNORE_LIVE_HOLD",
    ):
        assert token in line
    assert "AUTO_UPLOAD=" not in line
    assert "AUTOSLICE_IGNORE_LIVE_HOLD=1" not in line
    assert "authorized_upload" not in line
    assert "biliup" not in line
    verified = _run(env, "verify")
    assert verified.returncode == 0, verified.stderr
    again = _run(env, "activate")
    assert again.returncode == 0, again.stderr
    assert crontab.read_text(encoding="utf-8").splitlines().count(line) == 1
    assert crontab.read_text(encoding="utf-8").splitlines()[0] == "17 * * * * /usr/bin/true"


def test_activate_rolls_back_cron_disabled_and_new_lock(tmp_path: Path) -> None:
    _root, env, crontab = _fixture(tmp_path, foreign_cron="3 * * * * /usr/bin/true\n")
    failed = _run(env, "activate", extra={"OCI3_NO_UPLOAD_SOAK_TEST_FAIL_AFTER_CRON": "1"})
    assert failed.returncode != 0
    assert crontab.read_text(encoding="utf-8") == "3 * * * * /usr/bin/true\n"
    root = Path(env["OCI3_NO_UPLOAD_SOAK_TEST_ROOT"])
    assert (root / "opt/bilive/autoslice/DISABLED").read_bytes() == b""
    assert (root / "opt/bilive/autoslice/DISABLED").stat().st_mode & 0o777 == 0o644
    assert not (root / "opt/bilive/autoslice/oci3-no-upload-soak.lock").exists()


def test_deactivate_preserves_foreign_cron_and_is_idempotent(tmp_path: Path) -> None:
    _root, env, crontab = _fixture(tmp_path, foreign_cron="1 * * * * /usr/bin/true\n")
    assert _run(env, "activate").returncode == 0
    stopped = _run(env, "deactivate")
    assert stopped.returncode == 0, stopped.stderr
    root = Path(env["OCI3_NO_UPLOAD_SOAK_TEST_ROOT"])
    assert (root / "opt/bilive/autoslice/DISABLED").is_file()
    assert crontab.read_text(encoding="utf-8") == "1 * * * * /usr/bin/true\n"
    stopped_again = _run(env, "deactivate")
    assert stopped_again.returncode == 0, stopped_again.stderr
    assert crontab.read_text(encoding="utf-8") == "1 * * * * /usr/bin/true\n"


def test_start_date_parser_filter_and_free_unset(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import scripts.session_autoslice as runner

    assert runner._parse_start_date("") is None
    assert runner._parse_start_date(None) is None
    assert runner._parse_start_date("2026-08-20") == "2026-08-20"
    for invalid in ("2026-8-20", "2026-02-30", "yesterday"):
        with pytest.raises(ValueError):
            runner._parse_start_date(invalid)

    recording_root = tmp_path / "recordings"
    for date in ("2026-08-01", "2026-08-02", "2026-08-03", "2026-08-04"):
        (recording_root / date).mkdir(parents=True)
    monkeypatch.setattr(runner, "REC_ROOT", recording_root)
    monkeypatch.setattr(runner, "BASE", tmp_path / "base")
    monkeypatch.setattr(runner, "AUTOSLICE_START_DATE", None)
    assert runner.list_dates() == ["2026-08-02", "2026-08-03", "2026-08-04"]
    monkeypatch.setattr(runner, "AUTOSLICE_START_DATE", "2026-08-03")
    assert runner.list_dates() == ["2026-08-03", "2026-08-04"]


def test_start_date_walks_oldest_unfinished_backlog(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import scripts.session_autoslice as runner

    recording_root = tmp_path / "recordings"
    state_root = tmp_path / "base" / "state"
    state_root.mkdir(parents=True)
    for date in (
        "2026-08-20",
        "2026-08-21",
        "2026-08-22",
        "2026-08-23",
    ):
        (recording_root / date).mkdir(parents=True)
    monkeypatch.setattr(runner, "REC_ROOT", recording_root)
    monkeypatch.setattr(runner, "BASE", tmp_path / "base")
    monkeypatch.setattr(runner, "AUTOSLICE_START_DATE", "2026-08-20")
    (state_root / "2026-08-23.json").write_text(
        json.dumps({"status": "source_incomplete"}),
        encoding="utf-8",
    )

    # The bounded OCI3 window starts at the first unfinished date, even when
    # newer recordings exist; a missing state is conservatively unfinished.
    assert runner.list_dates() == ["2026-08-20", "2026-08-21", "2026-08-22"]

    # Every terminal projection, including publication-closure projections,
    # advances the bounded window; retry/incomplete statuses are not in this
    # set and remain eligible.
    for status in sorted(runner.START_DATE_TERMINAL_STATUSES):
        (state_root / "2026-08-20.json").write_text(
            json.dumps({"status": status}),
            encoding="utf-8",
        )
        assert runner._start_date_is_terminal("2026-08-20")

    (state_root / "2026-08-20.json").write_text(
        json.dumps({"status": "review_ready"}),
        encoding="utf-8",
    )
    # Once the first date is terminal, the next tick advances by one date.
    assert runner.list_dates() == ["2026-08-21", "2026-08-22", "2026-08-23"]


def test_invalid_start_date_fails_before_runner_provider_import() -> None:
    env = os.environ.copy()
    env["AUTOSLICE_START_DATE"] = "2026-02-30"
    completed = subprocess.run(
        [sys.executable, "-c", "import scripts.session_autoslice"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode != 0
    assert "AUTOSLICE_START_DATE" in completed.stderr
