"""Manifest-bound upload channel (2026-07-09 audit P0-3): authorization must be
cryptographically tied to the exact reviewed artifacts, uploads must be
idempotent, and the uploader can only receive manifest args — never hand-typed."""
import json

import scripts.authorized_upload as au


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
    entry = json.loads(ledger.read_text(encoding="utf-8").splitlines()[0])
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
    assert len(ledger.read_text().splitlines()) == 1  # 拒绝的不记成功条目


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
    entry = json.loads(ledger.read_text(encoding="utf-8").splitlines()[0])
    assert entry["rc"] == 7 and entry["bvid"] is None
    # rc!=0 的账本条目不算已上传 → 重试不会被幂等门误拦
    assert au.main(["upload", "--manifest", str(manifest), "--ledger", str(ledger),
                    "--uploader", str(_stub_uploader(tmp_path))]) == 0
