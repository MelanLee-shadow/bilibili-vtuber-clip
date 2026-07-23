"""Manifest-bound upload channel (2026-07-09 audit P0-3): authorization must be
cryptographically tied to the exact reviewed artifacts, uploads must be
idempotent, and the uploader can only receive manifest args — never hand-typed."""
import fcntl
import json
import os
import sys

import pytest

import scripts.authorized_upload as au

TEST_TAGS = ["李豆沙", "虚拟主播", "直播切片"]
VALID_TITLE = "【李豆沙】这是一个足够长度的测试标题"


@pytest.fixture(autouse=True)
def _isolated_default_upload_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(au, "DEFAULT_UPLOAD_LOCK", tmp_path / "default-upload.lock")


@pytest.fixture(autouse=True)
def _stub_season_http(monkeypatch):
    """Canned always-successful season API so pre-existing upload tests keep
    exercising the manifest/ledger/uploader contract; season-specific behavior
    is covered in test_authorized_upload_season.py."""

    def fake_build(cookie_json):
        def http(url, data=None, is_json=False):
            if "web-interface/view" in url:
                return {
                    "code": 0,
                    "data": {
                        "state": 0,
                        "aid": 111,
                        "cid": 222,
                        "title": "t",
                        "is_season_display": True,
                        "ugc_season": {"id": 8383206, "title": "小李切片"},
                    },
                }
            if "x/web/archives" in url:
                return {"code": 0, "data": {"arc_audits": [], "page": {"count": 0}}}
            if "web/seasons" in url:
                return {
                    "code": 0,
                    "data": {
                        "seasons": [
                            {
                                "season": {"id": 8383206, "title": "小李切片"},
                                "sections": {"sections": [{"id": 9320779, "title": "正片"}]},
                            },
                            {
                                "season": {"id": 8410735, "title": "小李歌唱"},
                                "sections": {"sections": [{"id": 9364628, "title": "正片"}]},
                            },
                        ]
                    },
                }
            if "episodes/add" in url:
                return {"code": 0, "message": "0"}
            if "tag/archive/tags" in url:
                return {"code": 0, "data": [{"tag_name": "李豆沙"}]}
            raise AssertionError(f"unexpected url {url}")

        return http, "csrf-test"

    monkeypatch.setattr(au, "_build_season_http", fake_build)
    monkeypatch.setattr(
        au,
        "public_verify_flow",
        lambda manifest, bvid, **kwargs: {
            "schema_version": "authorized-upload-public-verify.v2",
            "status": "VERIFIED_PUBLIC",
            "bvid": bvid,
            "public_view": {"aid": 111, "cid": 222},
            "verified_at": au.now(),
            "problems": [],
        },
    )


def _write_v3_package(video, cover, title, tags=None):
    tags = list(TEST_TAGS if tags is None else tags)
    stem = video.stem
    subtitle = video.parent / f"{stem}.srt"
    record = video.parent / f"{stem}.record.json"
    review = video.parent / "review_manifest.json"
    audit = video.parent / f"{stem}.package_audit.json"
    subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\n测试\n", encoding="utf-8")
    record.write_text(
        json.dumps(
            {
                "artifact_hashes": {
                    "burned_video_sha256": "sha256:" + au.sha256_file(video),
                    "cover_sha256": "sha256:" + au.sha256_file(cover),
                    "delivery_subtitle_sha256": "sha256:" + au.sha256_file(subtitle),
                },
                "publish_staging": {"title": title},
                "story_contract": {
                    "schema_version": "lidousha-story-contract.v1",
                    "candidate_id": "candidate-test",
                    "transcript_sha256": "sha256:"
                    + au.hashlib.sha256("测试".encode("utf-8")).hexdigest(),
                },
                "cover_generation": {
                    "workflow": "test-image-cover",
                    "method": "images.edit",
                    "model": "test-image-model",
                    "attempted_models": ["test-image-model"],
                    "ai_background": str(cover),
                    "ai_background_sha256": "sha256:" + au.sha256_file(cover),
                    "final_cover": str(cover),
                    "final_cover_sha256": "sha256:" + au.sha256_file(cover),
                    "fallback_used": False,
                },
                "upload_tags": {
                    "engine": "test",
                    "status": "OK",
                    "final_tags": tags,
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    review.write_text(
        json.dumps(
            {
                "items": [
                    {
                        "stem": stem,
                        "media": video.name,
                        "cover": cover.name,
                        "record": record.name,
                        "subtitle_srt": subtitle.name,
                        "title": title,
                        **(
                            {
                                "classification": "song",
                                "lyrics_alignment_report": "test-fixture",
                            }
                            if title.startswith(au.SONG_TITLE_PREFIX)
                            else {}
                        ),
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    audit.write_text(
        json.dumps(au.audit_package(video.parent), ensure_ascii=False),
        encoding="utf-8",
    )
    return audit


def _mk(tmp_path, title=VALID_TITLE, quote="可以上传了"):
    tmp_path.mkdir(parents=True, exist_ok=True)
    video = tmp_path / "clip.mp4"
    cover = tmp_path / "clip.cover.png"
    video.write_bytes(b"fake-video-bytes")
    cover.write_bytes(b"fake-cover-bytes")
    audit = _write_v3_package(video, cover, title)
    manifest = tmp_path / "clip.upload_manifest.json"
    rc = au.main([
        "make-manifest", "--video", str(video), "--cover", str(cover),
        "--package-audit", str(audit), "--title", title, "--quote", quote,
        "--out", str(manifest),
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
    assert data["description"] == (
        "https://live.bilibili.com/\n"
        "李豆沙个人主页：https://space.bilibili.com/1703797642\n"
        "李豆沙直播间：https://live.bilibili.com/22966160"
    )


def test_verify_refuses_hash_drift(tmp_path, capsys):
    video, _, manifest = _mk(tmp_path)
    video.write_bytes(b"tampered-after-review")  # 审的是A、传的必须还是A
    assert au.main(["verify", "--manifest", str(manifest)]) == 2
    assert "HASH DRIFT" in capsys.readouterr().err


def test_verify_refuses_attested_srt_or_audit_drift(tmp_path, capsys):
    _, _, manifest = _mk(tmp_path)
    data = json.loads(manifest.read_text())
    subtitle = data["package_attestation"]["subtitle"]["path"]
    with open(subtitle, "a", encoding="utf-8") as handle:
        handle.write("tamper\n")
    assert au.main(["verify", "--manifest", str(manifest)]) == 2
    assert "package_attestation.subtitle HASH DRIFT" in capsys.readouterr().err

    _, _, manifest2 = _mk(tmp_path / "other")
    data2 = json.loads(manifest2.read_text())
    audit = data2["package_attestation"]["package_audit"]["path"]
    with open(audit, "w", encoding="utf-8") as handle:
        json.dump({"passed": False, "root": str((tmp_path / "other").resolve())}, handle)
    assert au.main(["verify", "--manifest", str(manifest2)]) == 2
    err = capsys.readouterr().err
    assert "package_attestation.package_audit HASH DRIFT" in err
    assert "package audit did not pass" in err


def test_verify_refuses_record_coherence_even_if_attestation_hash_is_rebound(tmp_path, capsys):
    _, _, manifest = _mk(tmp_path)
    data = json.loads(manifest.read_text())
    record = data["package_attestation"]["record"]["path"]
    record_data = json.loads(open(record, encoding="utf-8").read())
    record_data["publish_staging"]["title"] = "被事后改掉的标题"
    with open(record, "w", encoding="utf-8") as handle:
        json.dump(record_data, handle, ensure_ascii=False)
    data["package_attestation"]["record"]["sha256"] = au.sha256_file(au.Path(record))
    with open(manifest, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False)
    assert au.main(["verify", "--manifest", str(manifest)]) == 2
    assert "record publish title mismatch" in capsys.readouterr().err


def test_make_manifest_requires_authorization_quote(tmp_path):
    video = tmp_path / "v.mp4"
    cover = tmp_path / "v.cover.png"
    video.write_bytes(b"v")
    cover.write_bytes(b"c")
    audit = _write_v3_package(video, cover, VALID_TITLE)
    rc = au.main(["make-manifest", "--video", str(video), "--cover", str(cover),
                  "--package-audit", str(audit), "--title", VALID_TITLE, "--quote", "   "])
    assert rc == 2


def _mk_with_tags(tmp_path, tags: str):
    video = tmp_path / "v.mp4"
    cover = tmp_path / "v.cover.png"
    video.write_bytes(b"v")
    cover.write_bytes(b"c")
    tag_list = [part.strip() for part in tags.split(",") if part.strip()]
    audit = _write_v3_package(video, cover, VALID_TITLE, tag_list)
    manifest = tmp_path / "v.upload_manifest.json"
    rc = au.main(["make-manifest", "--video", str(video), "--cover", str(cover),
                  "--package-audit", str(audit),
                  "--title", VALID_TITLE, "--quote", "可以上传了",
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
    video, cover = tmp_path / "clip.mp4", tmp_path / "clip.cover.png"
    video.write_bytes(b"v")
    cover.write_bytes(b"c")
    audit = _write_v3_package(
        video, cover, VALID_TITLE, ["李豆沙", "虚拟主播", "侄女", "百合"]
    )
    manifest = tmp_path / "m.json"
    assert au.main(["make-manifest", "--video", str(video), "--cover", str(cover),
                    "--package-audit", str(audit),
                    "--title", VALID_TITLE, "--quote", "q", "--out", str(manifest)]) == 0
    data = json.loads(manifest.read_text(encoding="utf-8"))
    assert data["tags"] == ["李豆沙", "虚拟主播", "侄女", "百合"]
    assert data["tags_source"].startswith("record.json:")


def test_make_manifest_cli_tags_must_match_record_and_no_tags_refuses(tmp_path):
    video, cover = tmp_path / "clip.mp4", tmp_path / "clip.cover.png"
    video.write_bytes(b"v")
    cover.write_bytes(b"c")
    audit = _write_v3_package(video, cover, VALID_TITLE, ["李豆沙", "记录里的"])
    m1 = tmp_path / "m1.json"
    assert au.main(["make-manifest", "--video", str(video), "--cover", str(cover),
                    "--package-audit", str(audit), "--title", VALID_TITLE, "--quote", "q",
                    "--tags", "李豆沙,手给的", "--out", str(m1)]) == 2
    m2 = tmp_path / "m2.json"
    assert au.main(["make-manifest", "--video", str(video), "--cover", str(cover),
                    "--package-audit", str(audit), "--title", VALID_TITLE, "--quote", "q",
                    "--no-tags", "--out", str(m2)]) == 2


def test_make_manifest_refuses_missing_or_unreadable_record_tags(tmp_path, capsys):
    video, cover = tmp_path / "clip.mp4", tmp_path / "clip.cover.png"
    video.write_bytes(b"v")
    cover.write_bytes(b"c")
    audit = _write_v3_package(video, cover, VALID_TITLE, [])
    record = sidecar = video.parent / "clip.record.json"
    data = json.loads(sidecar.read_text())
    data["upload_tags"] = {"engine": "test", "status": "FAILED", "final_tags": []}
    sidecar.write_text(json.dumps(data), encoding="utf-8")
    m1 = tmp_path / "m1.json"
    assert au.main(["make-manifest", "--video", str(video), "--cover", str(cover),
                    "--package-audit", str(audit), "--title", VALID_TITLE, "--quote", "q",
                    "--out", str(m1)]) == 2
    record.write_text("{not-json", encoding="utf-8")
    m2 = tmp_path / "m2.json"
    rc = au.main(["make-manifest", "--video", str(video), "--cover", str(cover),
                  "--package-audit", str(audit), "--title", VALID_TITLE, "--quote", "q",
                  "--out", str(m2)])
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


def test_upload_v3_always_passes_record_bound_tag_line(tmp_path, capsys):
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
    assert "args=4" in out
    assert _ledger_rows(ledger)[-1]["tags"] == ",".join(TEST_TAGS)


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
    assert f"got: {video.resolve()} | {cover.resolve()} | {VALID_TITLE}" in out
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
    probe = tmp_path / "probe_lock.mp4"
    probe.write_text(
        "import fcntl, os, sys\n"
        f"fd = os.open({str(lock)!r}, os.O_RDWR | os.O_CREAT, 0o600)\n"
        "try:\n"
        "    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
        "except BlockingIOError:\n"
        "    print('LOCK_HELD')\n"
        "    print('BVID=BV1LOCK')\n"
        "    raise SystemExit(0)\n"
        "print('LOCK_NOT_HELD')\n"
        "raise SystemExit(9)\n",
        encoding="utf-8",
    )
    probe.chmod(0o755)
    cover = tmp_path / "probe_lock.cover.png"
    cover.write_bytes(b"cover")
    audit = _write_v3_package(probe, cover, VALID_TITLE)
    manifest = tmp_path / "lock-probe.upload_manifest.json"
    assert au.main(
        [
            "make-manifest",
            "--video",
            str(probe),
            "--cover",
            str(cover),
            "--package-audit",
            str(audit),
            "--title",
            VALID_TITLE,
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


def test_rolling_quota_guard_uses_max_of_ledger_and_creator_estimates(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    current = 2_000_000_000.0
    timestamp = au.dt.datetime.fromtimestamp(current, tz=au.dt.timezone.utc).isoformat()
    rows = [
        {
            "at": timestamp,
            "video_sha256": f"{index:064x}",
            "rc": 0,
        }
        for index in range(9)
    ]
    ledger.write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )

    def http(url):
        return {
            "code": 0,
            "data": {
                "arc_audits": [
                    {"Archive": {"bvid": f"BV{index}", "ptime": current - 10}}
                    for index in range(10)
                ],
                "page": {"count": 10},
            },
        }

    evidence, problems = au.rolling_quota_guard(ledger, http=http, now_epoch=current)
    assert evidence["local_ledger_successes"] == 9
    assert evidence["creator_recent_archives"] == 10
    assert evidence["estimated_used"] == 10
    assert problems and "10/10" in problems[0]


def test_code_21566_is_recorded_as_authoritative_quota_signal(tmp_path, capsys):
    _, _, manifest = _mk(tmp_path)
    uploader = tmp_path / "quota.sh"
    uploader.write_text(
        "#!/bin/bash\n"
        'echo "ResponseData { code: 21566, message: 投稿过于频繁 }" >&2\n'
        "exit 1\n",
        encoding="utf-8",
    )
    uploader.chmod(0o755)
    ledger = tmp_path / "ledger.jsonl"
    assert au.main([
        "upload", "--manifest", str(manifest), "--ledger", str(ledger),
        "--uploader", str(uploader),
    ]) == 1
    finished = _ledger_rows(ledger)[-1]
    assert finished["quota_frequency_code"] == 21566
    assert "QUOTA AUTHORITATIVE" in capsys.readouterr().err


def test_public_verification_failure_never_writes_success_or_allows_reupload(
    tmp_path, monkeypatch, capsys
):
    _, _, manifest = _mk(tmp_path)
    monkeypatch.setattr(
        au,
        "public_verify_flow",
        lambda *args, **kwargs: {
            "schema_version": "authorized-upload-public-verify.v2",
            "status": "PUBLIC_VERIFY_FAILED",
            "problems": ["Creator archive title mismatch"],
            "public_view": {"aid": 111, "cid": 222},
        },
    )
    ledger = tmp_path / "ledger.jsonl"
    uploader = _stub_uploader(tmp_path)
    assert au.main([
        "upload", "--manifest", str(manifest), "--ledger", str(ledger),
        "--uploader", str(uploader), "--public-wait", "0",
    ]) == 6
    rows = _ledger_rows(ledger)
    assert rows[-1]["rc"] == 6
    assert rows[-1]["uploader_rc"] == 0
    assert not (tmp_path / "clip.uploaded.json").exists()
    assert json.loads((tmp_path / "clip.public_verify.json").read_text())["status"] == "PUBLIC_VERIFY_FAILED"
    assert au.main([
        "upload", "--manifest", str(manifest), "--ledger", str(ledger),
        "--uploader", str(uploader),
    ]) == 6
    assert "already created a Bilibili archive" in capsys.readouterr().err


def test_success_sidecars_and_ledger_bind_package_attestation(tmp_path):
    _, _, manifest = _mk(tmp_path)
    ledger = tmp_path / "ledger.jsonl"
    assert au.main([
        "upload", "--manifest", str(manifest), "--ledger", str(ledger),
        "--uploader", str(_stub_uploader(tmp_path)),
    ]) == 0
    manifest_data = json.loads(manifest.read_text())
    uploaded = json.loads((tmp_path / "clip.uploaded.json").read_text())
    public = json.loads((tmp_path / "clip.public_verify.json").read_text())
    finished = _ledger_rows(ledger)[-1]
    assert public["status"] == "VERIFIED_PUBLIC"
    assert uploaded["status"] == "VERIFIED_PUBLIC"
    for name in ("package_audit", "record", "subtitle", "review_manifest"):
        key = f"{name}_sha256"
        assert uploaded[key] == manifest_data["package_attestation"][name]["sha256"]
        assert finished[key] == manifest_data["package_attestation"][name]["sha256"]
