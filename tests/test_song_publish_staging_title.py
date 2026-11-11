"""歌切投稿暂存的标题与封面路线（60-title 歌切铁律，50-song-lane 命名权威）。

真数据背景 ——第 21 次部署《快乐手帐》``song_130025_580``：
选片器把歌词预览当 job 标题送进暂存，``classification`` 为空，于是跑了谈话
标题模型（得到「“相信还有明天”这句歌词，我想留给低落时的自己」）和谈话封面
候选；随后 ``song_lane._apply_canonical_song_title`` 把交付标题改成
「【李豆沙】豆沙歌，《快乐手帐》」，``_write_song_active_record`` 比对暂存标题
必然拒收（``song active record title/video/upload binding mismatch``）。
"""

import json
from pathlib import Path

import pytest

from scripts import run_auto_review_shadow_pipeline as shadow_pipeline
from src.autoslice import song_lane
from src.autoslice.song_name_authority import (
    AUDIO_ALIGNMENT_MODEL_SUFFIX,
    SONG_NAME_AUTHORITY_UNPROVEN,
    song_catalog_publish_title,
)

CATALOG = "【李豆沙】豆沙歌，《快乐手帐》"
LYRIC_PREVIEW_JOB_TITLE = "天呀，生活总是平平淡淡\n偶尔磕磕绊绊\n让脑筋多转转"


def _song_job(*, model: str = "netease" + AUDIO_ALIGNMENT_MODEL_SUFFIX) -> dict:
    return {
        "candidate_id": "seededsong_120000_349590",
        "song_candidate": True,
        "song_boundary": {
            "status": "FULL_SONG_READY",
            "source": "song_repair.netease",
            "song_title": "快乐手帐",
        },
        "lyrics_alignment": {
            "status": "READY",
            "provider": "netease",
            "model": model,
            "external_lrc": "netease://song/1970344021",
            "matched_line_ratio": 0.8571,
        },
    }


def _materialized(tmp_path: Path) -> dict:
    recuts = tmp_path / "replacement_recuts"
    recuts.mkdir(parents=True)
    media = recuts / "seededsong.recut.mp4"
    media.write_bytes(b"fake media bytes\n")
    subtitle = recuts / "seededsong.recut.srt"
    subtitle.write_text("1\n00:00:00,000 --> 00:00:03,000\n生活总是平平淡淡\n", encoding="utf-8")
    return {
        "status": "MATERIALIZED",
        "media_path": str(media),
        "subtitle_path": str(subtitle),
        "manifest_path": str(recuts / "seededsong.recut.manifest.json"),
        "artifact_hashes": {"video_sha256": "sha256:" + "0" * 64},
    }


def _counting_title_llm(calls: list[str]):
    def call(prompt: str) -> str:
        calls.append(prompt)
        return json.dumps({"title": "“相信还有明天”这句歌词，我想留给低落时的自己"}, ensure_ascii=False)

    return call


def _stage(tmp_path: Path, *, song_job, title_llm_call, decision=None):
    return shadow_pipeline._stage_publish_after_release_gate(
        _materialized(tmp_path),
        decision=decision or shadow_pipeline.ReviewDecision(action=shadow_pipeline.DecisionAction.AUTO_UPLOAD),
        candidate_id="seededsong_120000_349590",
        title=LYRIC_PREVIEW_JOB_TITLE,
        cues=[],
        output_dir=tmp_path,
        run_ffmpeg=False,
        title_llm_call=title_llm_call,
        song_job=song_job,
    )


@pytest.fixture(autouse=True)
def _no_cpa(monkeypatch):
    monkeypatch.delenv("CPA_BASE_URL", raising=False)
    monkeypatch.delenv("CPA_API_KEY", raising=False)
    monkeypatch.setenv("AUTOSLICE_COVER_MODE", "cpa")


def test_catalog_title_reads_only_the_audio_proven_name():
    assert song_catalog_publish_title(_song_job()) == CATALOG
    assert song_catalog_publish_title(_song_job(model="netease-text-lrc-global-shift-v1")) is None
    job = _song_job()
    job["song_boundary"]["status"] = "PARTIAL"
    assert song_catalog_publish_title(job) is None
    assert song_catalog_publish_title({}) is None


def test_verified_song_stages_the_catalog_title_and_song_cover_without_the_talk_title_model(tmp_path):
    calls: list[str] = []
    record = _stage(tmp_path, song_job=_song_job(), title_llm_call=_counting_title_llm(calls))

    staging = record["publish_staging"]
    publish = json.loads(Path(staging["publish_json_path"]).read_text(encoding="utf-8"))
    assert calls == []
    assert staging["status"] == "STAGED"
    assert staging["title"] == publish["title"] == CATALOG
    # 歌切封面字是《歌名》（song 路线），不是谈话标题正文。
    assert publish["cover_text"] == "《快乐手帐》"
    assert record["cover_release_gate"]["satisfied"] is True

    # 交付端的 canonical override 读同一权威：暂存标题不会再被改写，
    # 因而 _write_song_active_record 的标题绑定成立。
    result = {"title": staging["title"]}
    song_lane._apply_canonical_song_title(
        result, summary_record={"source_context_job": _song_job()}, cid="song_130025_580"
    )
    assert result == {"title": CATALOG}


def test_song_without_audio_name_authority_is_held_at_the_release_gate(tmp_path):
    calls: list[str] = []
    record = _stage(
        tmp_path,
        song_job=_song_job(model="netease-text-lrc-global-shift-v1"),
        title_llm_call=_counting_title_llm(calls),
    )

    assert calls == []
    assert record["publish_staging"]["status"] == "SKIPPED_RELEASE_GATE"
    assert record["publish_staging"]["reason_codes"] == [SONG_NAME_AUTHORITY_UNPROVEN]
    assert record["cover_release_gate"]["satisfied"] is False
    assert not list(tmp_path.glob("**/*.publish.json"))


def test_existing_release_gate_reasons_are_kept_for_an_unnamed_song(tmp_path):
    record = _stage(
        tmp_path,
        song_job=_song_job(model="netease-text-lrc-global-shift-v1"),
        title_llm_call=None,
        decision=shadow_pipeline.ReviewDecision(
            action=shadow_pipeline.DecisionAction.AUTO_UPLOAD,
            reason_codes=("SONG_FULL_BOUNDARY_READY",),
        ),
    )

    assert record["publish_staging"]["reason_codes"] == [
        "SONG_FULL_BOUNDARY_READY",
        SONG_NAME_AUTHORITY_UNPROVEN,
    ]


def test_talk_staging_still_receives_the_job_title_and_title_model(tmp_path, monkeypatch):
    captured = {}
    title_llm = _counting_title_llm([])

    def fake_stage(record, **kwargs):
        captured.update(kwargs)
        return {**record, "publish_staging": {"status": "STAGED", "upload_enabled": False}}

    monkeypatch.setattr(shadow_pipeline, "_stage_publish_draft", fake_stage)
    _stage(tmp_path, song_job=None, title_llm_call=title_llm)

    assert captured["title"] == LYRIC_PREVIEW_JOB_TITLE
    assert captured["title_llm_call"] is title_llm
