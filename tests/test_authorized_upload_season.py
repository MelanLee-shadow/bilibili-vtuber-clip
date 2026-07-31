"""Season (合集) membership is part of the publish (Ivan 2026-07-20): the manifest
freezes the lane at review time, upload finishes the add + PUBLIC re-verify, and
season-add is the idempotent retry.  发布未入集 = 流程未完成 (exit 6)."""
import json

import pytest

import scripts.authorized_upload as au

TALK_TITLE = "【李豆沙】这是一个足够长度的谈话切片标题"
SONG_TITLE = "【李豆沙】豆沙歌，《暖暖》"
TEST_TAGS = ["李豆沙", "虚拟主播", "直播切片"]


@pytest.fixture(autouse=True)
def _isolated_default_upload_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(au, "DEFAULT_UPLOAD_LOCK", tmp_path / "default-upload.lock")


class FakeBili:
    """Stateful season API: display flips only after episodes/add — mirroring the
    snake_case pitfall where trusting the add's code 0 without a public re-read
    shipped a false green."""

    def __init__(self, *, state=0, add_code=0):
        self.state = state
        self.add_code = add_code
        self.added_sections: list[dict] = []
        self.calls: list[str] = []
        self._season_by_section = {9320779: "小李切片", 9364628: "小李歌唱"}
        self._season_id_by_section = {9320779: 8383206, 9364628: 8410735}
        self._displayed: str | None = None
        self._title: str | None = None
        self._aid = 111
        self._cid = 222
        self.member_source = au.EXPECTED_SOURCE

    def build(self, cookie_json):
        def http(url, data=None, is_json=False):
            self.calls.append(url)
            if "web-interface/view" in url:
                data_obj = {
                    "state": self.state,
                    "aid": self._aid,
                    "cid": self._cid,
                    "title": self._title,
                    "desc": au.DEFAULT_DESCRIPTION,
                    "tid": au.EXPECTED_TID,
                    "copyright": au.EXPECTED_COPYRIGHT,
                }
                if self.state != 0:
                    return {"code": -404, "data": None}
                if self._displayed:
                    data_obj["is_season_display"] = True
                    section_id = next(
                        key for key, value in self._season_by_section.items()
                        if value == self._displayed
                    )
                    data_obj["ugc_season"] = {
                        "id": self._season_id_by_section[section_id],
                        "title": self._displayed,
                    }
                else:
                    data_obj["is_season_display"] = False
                return {"code": 0, "data": data_obj}
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
                assert is_json and "sectionId" in data and "episodes" in data, (
                    "episodes/add must use the camelCase JSON contract"
                )
                assert data["episodes"][0]["charging_pay"] == 0
                self.added_sections.append(data)
                self._title = data["episodes"][0]["title"]
                if self.add_code in (0, au.SEASON_ADD_ALREADY_IN):
                    self._displayed = self._season_by_section[data["sectionId"]]
                return {"code": self.add_code, "message": str(self.add_code)}
            if "vupre/web/archive/view" in url:
                return {
                    "code": 0,
                    "data": {
                        "archive": {
                            "aid": self._aid,
                            "bvid": "BV1TEST",
                            "title": self._title,
                            "desc": au.DEFAULT_DESCRIPTION,
                            "tag": TEST_TAGS,
                            "tid": au.EXPECTED_TID,
                            "copyright": au.EXPECTED_COPYRIGHT,
                            "source": self.member_source,
                        }
                    },
                }
            if "creative/web/season/section?id=" in url:
                section_id = int(url.rsplit("=", 1)[-1])
                return {
                    "code": 0,
                    "data": {
                        "id": section_id,
                        "season_id": self._season_id_by_section[section_id],
                        "episodes": [
                            {
                                "aid": self._aid,
                                "bvid": "BV1TEST",
                                "title": self._title,
                            }
                        ],
                    },
                }
            if "tag/archive/tags" in url:
                return {"code": 0, "data": [{"tag_name": tag} for tag in TEST_TAGS]}
            raise AssertionError(f"unexpected url {url}")

        return http, "csrf-test"


def _write_title_cover_qc(cover, title):
    verdict = {
        "lidousha_primary": True,
        "thumbnail_readable": True,
        "physical_text_line_count": 2,
        "single_clear_hook": True,
        "text_overcrowded": False,
        "title_cover_aligned": True,
        "unrelated_or_misleading_elements": [],
        "pass": True,
        "reason": "李豆沙主体清楚，两行钩子与标题一致。",
    }
    cover_sha = au.sha256_file(cover)
    receipt = cover.parent / "title-cover-joint-qc.json"
    receipt.write_text(
        json.dumps(
            {
                "schema_version": au.TITLE_COVER_QC_SCHEMA_VERSION,
                "candidate_id": "candidate-test",
                "title": title,
                "title_sha256": "sha256:" + au._sha256_text(title),
                "cover_path": str(cover.resolve()),
                "cover_sha256": "sha256:" + cover_sha,
                "preferred_provider": "cpa",
                "selected_provider": "cpa",
                "witness": {
                    "schema_version": (
                        au.TITLE_COVER_QC_WITNESS_SCHEMA_VERSION
                    ),
                    "image_path": str(cover.resolve()),
                    "model": "gpt-test-cpa",
                    "provider": "cpa",
                    "image_sha256": cover_sha,
                    "status": "OBSERVED",
                    "answer": json.dumps(
                        verdict, ensure_ascii=False, separators=(",", ":")
                    ),
                },
                "verdict": verdict,
                "status": "PASS",
                "pass": True,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return receipt


def _mk(tmp_path, title=TALK_TITLE, season_args=()):
    video = tmp_path / "clip.mp4"
    cover = tmp_path / "clip.cover.png"
    video.write_bytes(b"fake-video-bytes")
    cover.write_bytes(b"fake-cover-bytes")
    subtitle = tmp_path / "clip.srt"
    subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\n测试\n", encoding="utf-8")
    record = tmp_path / "clip.record.json"
    record.write_text(json.dumps({
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
        "upload_tags": {"engine": "test", "status": "OK", "final_tags": TEST_TAGS},
    }, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "review_manifest.json").write_text(json.dumps({
        "items": [{
            "stem": "clip",
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
        }]
    }, ensure_ascii=False), encoding="utf-8")
    audit = tmp_path / "clip.package_audit.json"
    audit.write_text(
        json.dumps(au.audit_package(tmp_path), ensure_ascii=False),
        encoding="utf-8",
    )
    title_cover_qc = _write_title_cover_qc(cover, title)
    manifest = tmp_path / "clip.upload_manifest.json"
    rc = au.main([
        "make-manifest", "--video", str(video), "--cover", str(cover),
        "--package-audit", str(audit), "--title", title, "--quote", "可以上传",
        "--title-cover-qc", str(title_cover_qc),
        "--out", str(manifest),
        *season_args,
    ])
    return rc, manifest


def _uploader(tmp_path, bvid="BV1TEST"):
    stub = tmp_path / "stub_upload.sh"
    body = "#!/bin/bash\necho rc=0\n"
    if bvid:
        body += f"echo BVID={bvid}\n"
    stub.write_text(body, encoding="utf-8")
    stub.chmod(0o755)
    return stub


@pytest.mark.parametrize("schema", ["app", "biliup"])
def test_build_season_http_really_constructs_requests(
    tmp_path,
    monkeypatch,
    schema,
):
    """Regression for the shadowed-import UnboundLocalError: exercise the REAL
    _build_season_http closure (both GET and JSON-POST branches) against a
    monkeypatched urlopen — mocking the builder wholesale hid a crash that only
    fired on the first production call."""
    import io
    import urllib.request as _urlreq

    cookie_json = tmp_path / "cookie.json"
    cookie_info = {"cookie_info": {"cookies": [
            {"name": "SESSDATA", "value": "sess-value"},
            {"name": "bili_jct", "value": "csrf-value"},
        ]}}
    cookie_shape = {"data": cookie_info} if schema == "app" else cookie_info
    cookie_json.write_text(json.dumps(cookie_shape), encoding="utf-8")
    seen = []

    def fake_urlopen(request, timeout=0):
        seen.append(request)
        return io.BytesIO(json.dumps({"code": 0, "data": {"ok": True}}).encode("utf-8"))

    monkeypatch.setattr(_urlreq, "urlopen", fake_urlopen)
    http, csrf = au._build_season_http(cookie_json)
    assert csrf == "csrf-value"

    assert http(au.VIEW_API.format(bvid="BV1X"))["code"] == 0
    assert "Cookie" not in seen[0].headers  # 公开 API 不带 cookie

    assert http(au.EPISODES_ADD_API.format(csrf=csrf), data={"sectionId": 1, "episodes": []}, is_json=True)["code"] == 0
    member_request = seen[1]
    assert member_request.get_header("Cookie") and "sess-value" in member_request.get_header("Cookie")
    assert json.loads(member_request.data.decode("utf-8"))["sectionId"] == 1

    assert http("https://member.bilibili.com/form", data={"a": "b"})["code"] == 0
    assert seen[2].data == b"a=b"  # form 编码分支同样必须真的能构造请求


def test_build_season_http_rejects_ambiguous_cookie_without_secret(tmp_path):
    cookie_json = tmp_path / "cookie.json"
    cookie_json.write_text(json.dumps({
        "cookie_info": {
            "cookies": [{"name": "bili_jct", "value": "top-secret-value"}]
        },
        "data": {
            "cookie_info": {
                "cookies": [
                    {"name": "bili_jct", "value": "nested-secret-value"}
                ]
            }
        },
    }), encoding="utf-8")

    with pytest.raises(au.CookieSchemaError, match="ambiguous") as raised:
        au._build_season_http(cookie_json)

    assert "top-secret-value" not in str(raised.value)
    assert "nested-secret-value" not in str(raised.value)


def test_same_bv_adapter_splits_api_and_biliup_cookie_files(tmp_path):
    api_cookie = tmp_path / "app-cookie.json"
    api_cookie.write_text(json.dumps({
        "data": {
            "cookie_info": {
                "cookies": [
                    {"name": "SESSDATA", "value": "sess-value"},
                    {"name": "bili_jct", "value": "csrf-value"},
                ]
            }
        }
    }), encoding="utf-8")
    biliup_cookie = tmp_path / "biliup-cookie.json"
    biliup_cookie.write_text(json.dumps({
        "cookie_info": {
            "cookies": [
                {"name": "SESSDATA", "value": "sess-value"},
                {"name": "bili_jct", "value": "csrf-value"},
            ]
        }
    }), encoding="utf-8")

    adapter = au._same_bv_adapter(api_cookie, biliup_cookie)

    assert adapter.session.cookie_path == api_cookie
    assert adapter.session.biliup_cookie_path == biliup_cookie


def test_lane_derivation_is_a_title_choke_point():
    assert au.derive_season_lane(SONG_TITLE) == "song"
    assert au.derive_season_lane(TALK_TITLE) == "talk"
    # 前缀必须逐字匹配歌切目录式，防止“豆沙歌”散文标题误入歌合集
    assert au.derive_season_lane("【李豆沙】听豆沙歌的人") == "talk"


def test_make_manifest_freezes_season_binding(tmp_path):
    rc, manifest = _mk(tmp_path)
    assert rc == 0
    season = json.loads(manifest.read_text())["season"]
    assert season == {"lane": "talk", "season_title": "小李切片", "source": "auto:title-prefix"}

    rc, manifest = _mk(tmp_path, title=SONG_TITLE)
    assert rc == 0
    assert json.loads(manifest.read_text())["season"]["season_title"] == "小李歌唱"


def test_make_manifest_explicit_none_and_contradiction(tmp_path):
    rc, manifest = _mk(tmp_path, season_args=("--season", "none"))
    assert rc == 0
    assert json.loads(manifest.read_text())["season"] is None

    rc, _ = _mk(tmp_path, title=SONG_TITLE, season_args=("--season", "talk"))
    assert rc == 2  # 标题决定合集；强行改道必须拒绝


def test_verify_refuses_tampered_season_block(tmp_path, capsys):
    rc, manifest = _mk(tmp_path)
    assert rc == 0
    data = json.loads(manifest.read_text())
    data["season"]["season_title"] = "别的合集"
    manifest.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    assert au.main(["verify", "--manifest", str(manifest)]) == 2


def test_upload_finishes_season_add_and_public_verify(tmp_path, monkeypatch, capsys):
    fake = FakeBili()
    monkeypatch.setattr(au, "_build_season_http", fake.build)
    rc, manifest = _mk(tmp_path)
    assert rc == 0
    ledger = tmp_path / "ledger.jsonl"
    rc = au.main([
        "upload", "--manifest", str(manifest), "--ledger", str(ledger),
        "--uploader", str(_uploader(tmp_path)), "--cookie-json", str(tmp_path / "unused.json"),
    ])
    assert rc == 0
    assert fake.added_sections[0]["sectionId"] == 9320779  # talk → 小李切片 正片
    sidecar = json.loads((tmp_path / "clip.season_verify.json").read_text())
    assert sidecar["status"] == "IN_SEASON_PUBLIC"
    assert sidecar["ugc_season_title"] == "小李切片"
    assert sidecar["bvid"] == "BV1TEST"
    assert json.loads((tmp_path / "clip.public_verify.json").read_text())["status"] == "VERIFIED_PUBLIC"
    assert json.loads((tmp_path / "clip.uploaded.json").read_text())["status"] == "VERIFIED_PUBLIC"
    assert any(
        url.endswith("creative/web/season/section?id=9320779")
        for url in fake.calls
    )


def test_upload_song_manifest_targets_song_season(tmp_path, monkeypatch):
    fake = FakeBili()
    monkeypatch.setattr(au, "_build_season_http", fake.build)
    rc, manifest = _mk(tmp_path, title=SONG_TITLE)
    assert rc == 0
    rc = au.main([
        "upload", "--manifest", str(manifest), "--ledger", str(tmp_path / "l.jsonl"),
        "--uploader", str(_uploader(tmp_path)), "--cookie-json", str(tmp_path / "unused.json"),
    ])
    assert rc == 0
    assert fake.added_sections[0]["sectionId"] == 9364628  # song → 小李歌唱 正片


def test_creator_metadata_mismatch_blocks_success_after_archive_exists(
    tmp_path, monkeypatch
):
    fake = FakeBili()
    fake.member_source = "https://wrong.example/"
    monkeypatch.setattr(au, "_build_season_http", fake.build)
    rc, manifest = _mk(tmp_path)
    assert rc == 0
    ledger = tmp_path / "ledger.jsonl"
    assert au.main([
        "upload", "--manifest", str(manifest), "--ledger", str(ledger),
        "--uploader", str(_uploader(tmp_path)), "--cookie-json", str(tmp_path / "unused.json"),
        "--public-wait", "0",
    ]) == 6
    evidence = json.loads((tmp_path / "clip.public_verify.json").read_text())
    assert evidence["status"] == "PUBLIC_VERIFY_FAILED"
    assert "Creator archive source mismatch" in evidence["problems"]
    assert not (tmp_path / "clip.uploaded.json").exists()
    assert json.loads(ledger.read_text().splitlines()[-1])["rc"] == 6


def test_upload_pending_transcode_exits_6_and_season_add_retries(tmp_path, monkeypatch):
    fake = FakeBili(state=-30)
    monkeypatch.setattr(au, "_build_season_http", fake.build)
    rc, manifest = _mk(tmp_path)
    assert rc == 0
    ledger = tmp_path / "ledger.jsonl"
    rc = au.main([
        "upload", "--manifest", str(manifest), "--ledger", str(ledger),
        "--uploader", str(_uploader(tmp_path)), "--cookie-json", str(tmp_path / "unused.json"),
        "--season-wait", "0",
    ])
    assert rc == 6  # 已投稿但入集未完成 ≠ 完成
    assert json.loads((tmp_path / "clip.season_verify.json").read_text())["status"] == "PENDING_TRANSCODE"
    assert not fake.added_sections

    # 转码完成后 season-add 从账本拿 bvid 幂等补挂
    fake.state = 0
    rc = au.main([
        "season-add", "--manifest", str(manifest), "--ledger", str(ledger),
        "--cookie-json", str(tmp_path / "unused.json"),
    ])
    assert rc == 0
    assert fake.added_sections[0]["sectionId"] == 9320779
    assert json.loads((tmp_path / "clip.season_verify.json").read_text())["status"] == "IN_SEASON_PUBLIC"


def test_season_add_treats_already_in_season_as_success(tmp_path, monkeypatch):
    fake = FakeBili(add_code=au.SEASON_ADD_ALREADY_IN)
    monkeypatch.setattr(au, "_build_season_http", fake.build)
    rc, manifest = _mk(tmp_path)
    assert rc == 0
    ledger = tmp_path / "ledger.jsonl"
    assert au.main([
        "upload", "--manifest", str(manifest), "--ledger", str(ledger),
        "--uploader", str(_uploader(tmp_path)), "--cookie-json", str(tmp_path / "unused.json"),
    ]) == 0
    assert json.loads((tmp_path / "clip.season_verify.json").read_text())["season_add_code"] == au.SEASON_ADD_ALREADY_IN


def test_upload_without_bvid_is_incomplete(tmp_path, monkeypatch, capsys):
    fake = FakeBili()
    monkeypatch.setattr(au, "_build_season_http", fake.build)
    rc, manifest = _mk(tmp_path)
    assert rc == 0
    rc = au.main([
        "upload", "--manifest", str(manifest), "--ledger", str(tmp_path / "l.jsonl"),
        "--uploader", str(_uploader(tmp_path, bvid=None)), "--cookie-json", str(tmp_path / "unused.json"),
    ])
    assert rc == 6
    assert "without re-uploading" in capsys.readouterr().err


def test_upload_respects_explicit_none_and_skip_flag(tmp_path, monkeypatch, capsys):
    fake = FakeBili()
    fake._title = TALK_TITLE
    monkeypatch.setattr(au, "_build_season_http", fake.build)
    rc, manifest = _mk(tmp_path, season_args=("--season", "none"))
    assert rc == 0
    rc = au.main([
        "upload", "--manifest", str(manifest), "--ledger", str(tmp_path / "l.jsonl"),
        "--uploader", str(_uploader(tmp_path)), "--cookie-json", str(tmp_path / "unused.json"),
    ])
    assert rc == 0
    assert not fake.added_sections  # opt-out 不触碰 season mutation API

    rc, manifest2 = _mk(tmp_path)
    assert rc == 0
    rc = au.main([
        "upload", "--manifest", str(manifest2), "--ledger", str(tmp_path / "l2.jsonl"),
        "--uploader", str(_uploader(tmp_path)), "--skip-season",
        "--cookie-json", str(tmp_path / "unused.json"),
    ])
    assert rc == 6
    assert json.loads((tmp_path / "clip.public_verify.json").read_text())["status"] == "SKIPPED_BY_EMERGENCY_FLAG"


def test_legacy_manifest_remains_verify_readable_but_cannot_new_upload(tmp_path):
    rc, manifest = _mk(tmp_path, title=SONG_TITLE)
    assert rc == 0
    data = json.loads(manifest.read_text())
    data["manifest_version"] = 2
    data.pop("schema_version")
    data.pop("package_attestation")
    data.pop("description")
    data.pop("publish_policy")
    del data["season"]  # 2026-07-20 之前的 manifest 没有 season 键
    manifest.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    assert au.main(["verify", "--manifest", str(manifest)]) == 0
    assert au.main([
        "upload", "--manifest", str(manifest), "--ledger", str(tmp_path / "ledger.jsonl"),
        "--uploader", str(_uploader(tmp_path)),
    ]) == 2
