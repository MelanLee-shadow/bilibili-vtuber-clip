"""Manifest-bound upload channel (2026-07-09 audit P0-3): authorization must be
cryptographically tied to the exact reviewed artifacts, uploads must be
idempotent, and the uploader can only receive manifest args — never hand-typed."""
import fcntl
import json
import os
import sys

import pytest

import scripts.authorized_upload as au


@pytest.fixture(autouse=True)
def _isolated_default_upload_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(au, "DEFAULT_UPLOAD_LOCK", tmp_path / "default-upload.lock")


def _mk(tmp_path, title="【李豆沙】标题", quote="可以上传了"):
    video = tmp_path / "clip.mp4"
    cover = tmp_path / "clip.cover.png"
    video.write_bytes(b"fake-video-bytes")
    cover.write_bytes(b"fake-cover-bytes")
    manifest = tmp_path / "clip.upload_manifest.json"
    rc = au.main([
        "make-manifest", "--video", str(video), "--cover", str(cover),
        "--title", title, "--quote", quote, "--out", str(manifest),
    ])
    assert rc == 0
    return video, cover, manifest


def _ledger_rows(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_make_manifest_then_verify_ok(tmp_path, capsys):
    _, _, manifest = _mk(tmp_path)
    assert au.main(["verify", "--manifest", str(manifest)]) == 0
    data = json.loads(manifest.read_text(encoding="utf-8"))
    assert data["artifact_id"] == data["video"]["sha256"][:12]
    assert data["authorization"]["quote"] == "可以上传了"


def test_verify_refuses_hash_drift(tmp_path, capsys):
    video, _, manifest = _mk(tmp_path)
    video.write_bytes(b"tampered-after-review")  # 审的是A、传的必须还是A
    assert au.main(["verify", "--manifest", str(manifest)]) == 2
    assert "HASH DRIFT" in capsys.readouterr().err


def test_make_manifest_requires_authorization_quote(tmp_path):
    video = tmp_path / "v.mp4"
    cover = tmp_path / "c.png"
    video.write_bytes(b"v")
    cover.write_bytes(b"c")
    rc = au.main(["make-manifest", "--video", str(video), "--cover", str(cover),
                  "--title", "t", "--quote", "   "])
    assert rc == 2


def _mk_with_tags(tmp_path, tags: str):
    video = tmp_path / "v.mp4"
    cover = tmp_path / "c.png"
    video.write_bytes(b"v")
    cover.write_bytes(b"c")
    manifest = tmp_path / "v.upload_manifest.json"
    rc = au.main(["make-manifest", "--video", str(video), "--cover", str(cover),
                  "--title", "【李豆沙】标题", "--quote", "可以上传了",
                  "--tags", tags, "--out", str(manifest)])
    return rc, manifest


def test_make_manifest_freezes_valid_tags(tmp_path):
    rc, manifest = _mk_with_tags(tmp_path, "李豆沙,虚拟主播,虚拟UP主,直播切片,侄女,百合")
    assert rc == 0
    data = json.loads(manifest.read_text(encoding="utf-8"))
    assert data["tags"] == ["李豆沙", "虚拟主播", "虚拟UP主", "直播切片", "侄女", "百合"]
    assert au.main(["verify", "--manifest", str(manifest)]) == 0


def test_make_manifest_refuses_bad_tag_lines(tmp_path, capsys):
    # 超过实测上限 12 个
    rc, _ = _mk_with_tags(tmp_path, ",".join(f"t{i}" for i in range(13)))
    assert rc == 2
    # 大小写视作重复
    rc, _ = _mk_with_tags(tmp_path, "VUP,vup")
    assert rc == 2
    # 单 tag 超 20 字符
    rc, _ = _mk_with_tags(tmp_path, "一" * 21)
    assert rc == 2


def test_verify_refuses_manifest_with_tampered_tags(tmp_path, capsys):
    rc, manifest = _mk_with_tags(tmp_path, "李豆沙,虚拟主播")
    assert rc == 0
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["tags"] = data["tags"] + [f"t{i}" for i in range(12)]  # 事后塞爆 tag 位
    manifest.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    assert au.main(["verify", "--manifest", str(manifest)]) == 2


def _write_record_sidecar(video, tags, status="OK"):
    record = video.parent / (video.name[: -len(".mp4")] + ".record.json")
    record.write_text(json.dumps({
        "upload_tags": {"engine": "suggest-upload-tags.v1", "status": status, "final_tags": tags},
    }, ensure_ascii=False), encoding="utf-8")
    return record


def test_make_manifest_auto_picks_tags_from_record_sidecar(tmp_path):
    video, cover = tmp_path / "clip.mp4", tmp_path / "c.png"
    video.write_bytes(b"v")
    cover.write_bytes(b"c")
    _write_record_sidecar(video, ["李豆沙", "虚拟主播", "侄女", "百合"])
    manifest = tmp_path / "m.json"
    assert au.main(["make-manifest", "--video", str(video), "--cover", str(cover),
                    "--title", "t", "--quote", "q", "--out", str(manifest)]) == 0
    data = json.loads(manifest.read_text(encoding="utf-8"))
    assert data["tags"] == ["李豆沙", "虚拟主播", "侄女", "百合"]
    assert data["tags_source"].startswith("record.json:")


def test_make_manifest_cli_tags_override_record_and_no_tags_skips(tmp_path):
    video, cover = tmp_path / "clip.mp4", tmp_path / "c.png"
    video.write_bytes(b"v")
    cover.write_bytes(b"c")
    _write_record_sidecar(video, ["李豆沙", "记录里的"])
    m1 = tmp_path / "m1.json"
    assert au.main(["make-manifest", "--video", str(video), "--cover", str(cover),
                    "--title", "t", "--quote", "q", "--tags", "李豆沙,手给的", "--out", str(m1)]) == 0
    assert json.loads(m1.read_text())["tags"] == ["李豆沙", "手给的"]
    m2 = tmp_path / "m2.json"
    assert au.main(["make-manifest", "--video", str(video), "--cover", str(cover),
                    "--title", "t", "--quote", "q", "--no-tags", "--out", str(m2)]) == 0
    assert "tags" not in json.loads(m2.read_text())


def test_make_manifest_ignores_failed_tag_record_and_refuses_unreadable(tmp_path, capsys):
    video, cover = tmp_path / "clip.mp4", tmp_path / "c.png"
    video.write_bytes(b"v")
    cover.write_bytes(b"c")
    record = _write_record_sidecar(video, [], status="FAILED")
    m1 = tmp_path / "m1.json"
    assert au.main(["make-manifest", "--video", str(video), "--cover", str(cover),
                    "--title", "t", "--quote", "q", "--out", str(m1)]) == 0
    assert "tags" not in json.loads(m1.read_text())  # FAILED 记录 → 无tag, 基础位回退
    record.write_text("{not-json", encoding="utf-8")
    m2 = tmp_path / "m2.json"
    rc = au.main(["make-manifest", "--video", str(video), "--cover", str(cover),
                  "--title", "t", "--quote", "q", "--out", str(m2)])
    assert rc == 2  # 坏 sidecar 必须响, 不许静默无tag
    assert "unreadable" in capsys.readouterr().err


def test_upload_passes_manifest_tag_line_to_uploader(tmp_path, capsys):
    rc, manifest = _mk_with_tags(tmp_path, "李豆沙,虚拟主播,侄女")
    assert rc == 0
    stub = tmp_path / "stub_upload.sh"
    stub.write_text(
        "#!/bin/bash\n"
        '[ "${AUTHORIZED_UPLOAD:-}" = "1" ] || exit 4\n'
        'echo "args=$#"\n'
        'echo "tagline=${4:-<none>}"\n'
        "echo rc=0\necho BVID=BV1TAG\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    ledger = tmp_path / "ledger.jsonl"
    assert au.main(["upload", "--manifest", str(manifest), "--ledger", str(ledger),
                    "--uploader", str(stub)]) == 0
    out = capsys.readouterr().out
    assert "args=4" in out
    assert "tagline=李豆沙,虚拟主播,侄女" in out
    finished = _ledger_rows(ledger)[-1]
    assert finished["tags"] == "李豆沙,虚拟主播,侄女"


def test_upload_without_tags_keeps_three_arg_uploader_contract(tmp_path, capsys):
    _, _, manifest = _mk(tmp_path)
    stub = tmp_path / "stub_upload.sh"
    stub.write_text(
        "#!/bin/bash\n"
        '[ "${AUTHORIZED_UPLOAD:-}" = "1" ] || exit 4\n'
        'echo "args=$#"\n'
        "echo rc=0\necho BVID=BV1NOTAG\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    ledger = tmp_path / "ledger.jsonl"
    assert au.main(["upload", "--manifest", str(manifest), "--ledger", str(ledger),
                    "--uploader", str(stub)]) == 0
    out = capsys.readouterr().out
    assert "args=3" in out
    assert "tags" not in _ledger_rows(ledger)[-1]


def _stub_uploader(tmp_path):
    """Uploader stub that proves it got manifest args + the authorization env."""
    stub = tmp_path / "stub_upload.sh"
    stub.write_text(
        "#!/bin/bash\n"
        '[ "${AUTHORIZED_UPLOAD:-}" = "1" ] || { echo "no-auth-env" >&2; exit 4; }\n'
        'echo "got: $1 | $2 | $3"\n'
        "echo rc=0\n"
        "echo BVID=BV1TEST\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return stub


def test_upload_runs_uploader_with_manifest_args_and_ledgers(tmp_path, capsys):
    video, cover, manifest = _mk(tmp_path)
    ledger = tmp_path / "ledger.jsonl"
    rc = au.main(["upload", "--manifest", str(manifest), "--ledger", str(ledger),
                  "--uploader", str(_stub_uploader(tmp_path))])
    assert rc == 0
    out = capsys.readouterr().out
    assert f"got: {video.resolve()} | {cover.resolve()} | 【李豆沙】标题" in out
    rows = _ledger_rows(ledger)
    assert [row["event"] for row in rows] == ["UPLOAD_ATTEMPT_STARTED", "UPLOAD_ATTEMPT_FINISHED"]
    assert rows[0]["attempt_id"] == rows[1]["attempt_id"]
    entry = rows[-1]
    assert entry["rc"] == 0 and entry["bvid"] == "BV1TEST"
    assert entry["video_sha256"] == json.loads(manifest.read_text())["video"]["sha256"]
    assert entry["authorization_quote"] == "可以上传了"


def test_upload_is_idempotent_by_video_hash(tmp_path, capsys):
    """充电器重复投稿事故的防线：同一视频 hash 第二次上传必须硬拒。"""
    _, _, manifest = _mk(tmp_path)
    ledger = tmp_path / "ledger.jsonl"
    stub = _stub_uploader(tmp_path)
    assert au.main(["upload", "--manifest", str(manifest), "--ledger", str(ledger), "--uploader", str(stub)]) == 0
    rc = au.main(["upload", "--manifest", str(manifest), "--ledger", str(ledger), "--uploader", str(stub)])
    assert rc == 3
    assert "already uploaded" in capsys.readouterr().err
    assert len(ledger.read_text().splitlines()) == 2  # 拒绝的不追加第二个 attempt


def test_upload_refuses_drifted_artifact(tmp_path, capsys):
    video, _, manifest = _mk(tmp_path)
    ledger = tmp_path / "ledger.jsonl"
    video.write_bytes(b"drifted")
    rc = au.main(["upload", "--manifest", str(manifest), "--ledger", str(ledger),
                  "--uploader", str(_stub_uploader(tmp_path))])
    assert rc == 2
    assert not ledger.exists()  # 没上传就没有账本条目


def test_failed_upload_ledgered_but_retryable(tmp_path):
    _, _, manifest = _mk(tmp_path)
    ledger = tmp_path / "ledger.jsonl"
    bad = tmp_path / "bad_upload.sh"
    bad.write_text("#!/bin/bash\necho boom >&2\nexit 7\n", encoding="utf-8")
    bad.chmod(0o755)
    assert au.main(["upload", "--manifest", str(manifest), "--ledger", str(ledger), "--uploader", str(bad)]) == 7
    entry = _ledger_rows(ledger)[-1]
    assert entry["rc"] == 7 and entry["bvid"] is None
    # rc!=0 的账本条目不算已上传 → 重试不会被幂等门误拦
    assert au.main(["upload", "--manifest", str(manifest), "--ledger", str(ledger),
                    "--uploader", str(_stub_uploader(tmp_path))]) == 0
    assert len(_ledger_rows(ledger)) == 4


def test_upload_holds_shared_lock_through_uploader_and_ledger_append(tmp_path, capsys):
    """The child uploader must observe the repair/upload lock as already held."""
    ledger = tmp_path / "ledger.jsonl"
    lock = tmp_path / "shared-upload.lock"
    probe = tmp_path / "probe_lock.py"
    probe.write_text(
        "import fcntl, os, sys\n"
        f"fd = os.open({str(lock)!r}, os.O_RDWR | os.O_CREAT, 0o600)\n"
        "try:\n"
        "    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
        "except BlockingIOError:\n"
        "    print('LOCK_HELD')\n"
        "    raise SystemExit(0)\n"
        "print('LOCK_NOT_HELD')\n"
        "raise SystemExit(9)\n",
        encoding="utf-8",
    )
    probe.chmod(0o755)
    cover = tmp_path / "cover.png"
    cover.write_bytes(b"cover")
    manifest = tmp_path / "lock-probe.upload_manifest.json"
    assert au.main(
        [
            "make-manifest",
            "--video",
            str(probe),
            "--cover",
            str(cover),
            "--title",
            "lock probe",
            "--quote",
            "test only",
            "--out",
            str(manifest),
        ]
    ) == 0

    rc = au.main(
        [
            "upload",
            "--manifest",
            str(manifest),
            "--ledger",
            str(ledger),
            "--lock",
            str(lock),
            "--uploader",
            sys.executable,
        ]
    )

    assert rc == 0
    output = capsys.readouterr().out
    assert "LOCK_HELD" in output
    assert "LOCK_NOT_HELD" not in output
    assert _ledger_rows(ledger)[-1]["rc"] == 0


def test_upload_refuses_instead_of_queueing_behind_repair_lock(tmp_path, capsys):
    _, _, manifest = _mk(tmp_path)
    ledger = tmp_path / "ledger.jsonl"
    lock = tmp_path / "shared-upload.lock"
    marker = tmp_path / "uploader-ran"
    stub = tmp_path / "must-not-run.sh"
    stub.write_text(f"#!/bin/bash\ntouch {str(marker)!r}\n", encoding="utf-8")
    stub.chmod(0o755)

    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        rc = au.main(
            [
                "upload",
                "--manifest",
                str(manifest),
                "--ledger",
                str(ledger),
                "--lock",
                str(lock),
                "--uploader",
                str(stub),
            ]
        )
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    assert rc == 4
    assert "shared upload/repair lock is busy" in capsys.readouterr().err
    assert not marker.exists()
    assert not ledger.exists()


def test_started_intent_is_fsynced_before_subprocess_and_terminal_is_durable(tmp_path, monkeypatch):
    _, _, manifest = _mk(tmp_path)
    ledger = tmp_path / "ledger.jsonl"
    fsync_calls = []
    real_fsync = au.os.fsync

    def tracked_fsync(fd):
        fsync_calls.append(fd)
        return real_fsync(fd)

    def inspect_then_finish(cmd, **kwargs):
        rows = _ledger_rows(ledger)
        assert len(rows) == 1
        assert rows[0]["event"] == "UPLOAD_ATTEMPT_STARTED"
        assert len(rows[0]["manifest_sha256"]) == 64
        # file + parent directory were both fsynced before the side effect.
        assert len(fsync_calls) >= 2
        return au.subprocess.CompletedProcess(cmd, 0, stdout="BVID=BV1INTENT\n", stderr="")

    monkeypatch.setattr(au.os, "fsync", tracked_fsync)
    monkeypatch.setattr(au.subprocess, "run", inspect_then_finish)

    assert au.main(["upload", "--manifest", str(manifest), "--ledger", str(ledger)]) == 0
    rows = _ledger_rows(ledger)
    assert [row["event"] for row in rows] == ["UPLOAD_ATTEMPT_STARTED", "UPLOAD_ATTEMPT_FINISHED"]
    assert rows[0]["attempt_id"] == rows[1]["attempt_id"]
    assert rows[1]["bvid"] == "BV1INTENT"
    assert len(fsync_calls) >= 4


def test_crash_after_started_intent_blocks_all_later_uploads(tmp_path, monkeypatch, capsys):
    _, _, manifest = _mk(tmp_path)
    ledger = tmp_path / "ledger.jsonl"

    def crash_after_intent(cmd, **kwargs):
        assert _ledger_rows(ledger)[-1]["event"] == "UPLOAD_ATTEMPT_STARTED"
        raise RuntimeError("simulated process crash before terminal row")

    monkeypatch.setattr(au.subprocess, "run", crash_after_intent)
    with pytest.raises(RuntimeError, match="simulated process crash"):
        au.main(["upload", "--manifest", str(manifest), "--ledger", str(ledger)])
    assert [row["event"] for row in _ledger_rows(ledger)] == ["UPLOAD_ATTEMPT_STARTED"]

    called = False

    def must_not_run(cmd, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("unresolved intent must block before uploader")

    monkeypatch.setattr(au.subprocess, "run", must_not_run)
    assert au.main(["upload", "--manifest", str(manifest), "--ledger", str(ledger)]) == 5
    assert "unresolved UPLOAD_ATTEMPT_STARTED" in capsys.readouterr().err
    assert called is False
    assert len(_ledger_rows(ledger)) == 1
