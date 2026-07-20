"""Season (合集) membership is part of the publish (Ivan 2026-07-20): the manifest
freezes the lane at review time, upload finishes the add + PUBLIC re-verify, and
season-add is the idempotent retry.  发布未入集 = 流程未完成 (exit 6)."""
import json

import pytest

import scripts.authorized_upload as au

TALK_TITLE = "【李豆沙】谈话切片标题"
SONG_TITLE = "【李豆沙】豆沙歌，《暖暖》"


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
        self._season_by_section = {11: "小李切片", 22: "小李歌唱"}
        self._displayed: str | None = None

    def build(self, cookie_json):
        def http(url, data=None, is_json=False):
            self.calls.append(url)
            if "web-interface/view" in url:
                data_obj = {"state": self.state, "aid": 111, "cid": 222, "title": "t"}
                if self.state != 0:
                    return {"code": -404, "data": None}
                if self._displayed:
                    data_obj["is_season_display"] = True
                    data_obj["ugc_season"] = {"title": self._displayed}
                else:
                    data_obj["is_season_display"] = False
                return {"code": 0, "data": data_obj}
            if "web/seasons" in url:
                return {
                    "code": 0,
                    "data": {
                        "seasons": [
                            {
                                "season": {"id": 1, "title": "小李切片"},
                                "sections": {"sections": [{"id": 11, "title": "正片"}]},
                            },
                            {
                                "season": {"id": 2, "title": "小李歌唱"},
                                "sections": {"sections": [{"id": 22, "title": "正片"}]},
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
                if self.add_code in (0, au.SEASON_ADD_ALREADY_IN):
                    self._displayed = self._season_by_section[data["sectionId"]]
                return {"code": self.add_code, "message": str(self.add_code)}
            if "tag/archive/tags" in url:
                return {"code": 0, "data": [{"tag_name": "李豆沙"}]}
            raise AssertionError(f"unexpected url {url}")

        return http, "csrf-test"


def _mk(tmp_path, title=TALK_TITLE, season_args=()):
    video = tmp_path / "clip.mp4"
    cover = tmp_path / "clip.cover.png"
    video.write_bytes(b"fake-video-bytes")
    cover.write_bytes(b"fake-cover-bytes")
    manifest = tmp_path / "clip.upload_manifest.json"
    rc = au.main([
        "make-manifest", "--video", str(video), "--cover", str(cover),
        "--title", title, "--quote", "可以上传", "--out", str(manifest),
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
    assert fake.added_sections[0]["sectionId"] == 11  # talk → 小李切片 正片
    sidecar = json.loads((tmp_path / "clip.season_verify.json").read_text())
    assert sidecar["status"] == "IN_SEASON_PUBLIC"
    assert sidecar["ugc_season_title"] == "小李切片"
    assert sidecar["bvid"] == "BV1TEST"


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
    assert fake.added_sections[0]["sectionId"] == 22  # song → 小李歌唱 正片


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
    assert fake.added_sections[0]["sectionId"] == 11
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
    assert "season-add" in capsys.readouterr().err


def test_upload_respects_explicit_none_and_skip_flag(tmp_path, monkeypatch, capsys):
    fake = FakeBili()
    monkeypatch.setattr(au, "_build_season_http", fake.build)
    rc, manifest = _mk(tmp_path, season_args=("--season", "none"))
    assert rc == 0
    rc = au.main([
        "upload", "--manifest", str(manifest), "--ledger", str(tmp_path / "l.jsonl"),
        "--uploader", str(_uploader(tmp_path)), "--cookie-json", str(tmp_path / "unused.json"),
    ])
    assert rc == 0
    assert not fake.calls  # opt-out 不触碰 season API

    rc, manifest2 = _mk(tmp_path)
    assert rc == 0
    rc = au.main([
        "upload", "--manifest", str(manifest2), "--ledger", str(tmp_path / "l2.jsonl"),
        "--uploader", str(_uploader(tmp_path)), "--skip-season",
        "--cookie-json", str(tmp_path / "unused.json"),
    ])
    assert rc == 0
    assert "NOT complete" in capsys.readouterr().err


def test_legacy_manifest_derives_lane_from_frozen_title(tmp_path, monkeypatch):
    fake = FakeBili()
    monkeypatch.setattr(au, "_build_season_http", fake.build)
    rc, manifest = _mk(tmp_path, title=SONG_TITLE)
    assert rc == 0
    data = json.loads(manifest.read_text())
    del data["season"]  # 2026-07-20 之前的 manifest 没有 season 键
    manifest.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    ledger = tmp_path / "ledger.jsonl"
    assert au.main([
        "upload", "--manifest", str(manifest), "--ledger", str(ledger),
        "--uploader", str(_uploader(tmp_path)), "--cookie-json", str(tmp_path / "unused.json"),
    ]) == 0
    assert fake.added_sections[0]["sectionId"] == 22
    sidecar = json.loads((tmp_path / "clip.season_verify.json").read_text())
    assert sidecar["season_binding_source"] == "derived-from-frozen-title"
