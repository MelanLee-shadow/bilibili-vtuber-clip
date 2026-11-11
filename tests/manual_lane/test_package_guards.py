"""The two guards at the start of step 8: speech with no subtitle (SPEECH_WITHOUT_SUBTITLES) and a sealed, published package
that must not be deleted (PUBLISHED_PACKAGE_PROTECTED).  The finished-media lane is the recorder of test_fmr_calls; the
registry is a fake."""

from __future__ import annotations

import json

import pytest
from test_fmr_calls import Recorder, _patch, _prepared

from src.autoslice.manual_lane import package_guards, title_history
from src.autoslice.manual_lane import stage_package as sp
from src.autoslice.manual_lane.adapters import RemoteResult


def _srt(rows):
    return "".join(f"{i}\n{_ts(a)} --> {_ts(b)}\n{t}\n\n" for i, (a, b, t) in enumerate(rows, 1))


def _ts(seconds):
    ms = round(seconds * 1000)
    return f"{ms // 3_600_000:02d}:{ms // 60_000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def _clip(tmp_path, final, dictation):
    ctx, _cpa = _prepared(tmp_path)
    (ctx.clip_dir / "subs/final.srt").write_text(_srt(final), encoding="utf-8")
    if dictation is None:
        (ctx.clip_dir / "asr/bcut.srt").unlink()
    else:
        (ctx.clip_dir / "asr/bcut.srt").write_text(_srt(dictation), encoding="utf-8")
    return ctx


FINAL = [(0.0, 2.0, "第一句"), (7.0, 9.0, "第二句")]            # a 5 s gap, 2.0 to 7.0


# ----- D1 ---------------------------------------------------------------------------------------------------------------
def test_speech_in_a_gap_of_three_seconds_or_more_fails_step_8_and_lists_the_stretch_and_its_words(tmp_path, monkeypatch):
    ctx = _clip(tmp_path, FINAL, [(0.0, 2.0, "第一句"), (3.0, 5.5, "这里还有人在说话"), (7.0, 9.0, "第二句")])
    rec = Recorder()
    _patch(monkeypatch, rec)
    out = sp.step8_package(ctx)
    assert not out.ok and out.reason == "SPEECH_WITHOUT_SUBTITLES"
    assert "2.0-7.0s" in out.note and "这里还有人在说话" in out.note
    assert rec.calls == [] and not (ctx.clip_dir / "pkg/final.mp4").exists()            # nothing built


def test_every_stretch_is_listed(tmp_path, monkeypatch):
    final = FINAL + [(14.0, 16.0, "第三句")]
    ctx = _clip(tmp_path, final, [(3.0, 5.5, "第一段没有字幕"), (10.0, 12.0, "第二段也没有字幕")])
    _patch(monkeypatch, Recorder())
    out = sp.step8_package(ctx)
    assert out.reason == "SPEECH_WITHOUT_SUBTITLES" and "共 2 段" in out.note
    assert "2.0-7.0s" in out.note and "9.0-14.0s" in out.note and "第二段也没有字幕" in out.note


def test_laughter_and_fillers_in_the_gap_do_not_stop_the_step(tmp_path, monkeypatch):
    ctx = _clip(tmp_path, FINAL, [(3.0, 4.0, "哈哈哈哈（笑）"), (4.0, 5.0, "嗯，啊……"), (5.0, 6.0, "呃哦")])
    _patch(monkeypatch, Recorder())
    out = sp.step8_package(ctx)
    assert out.ok, out.note


def test_a_gap_under_three_seconds_does_not_stop_the_step(tmp_path, monkeypatch):
    ctx = _clip(tmp_path, [(0.0, 2.0, "第一句"), (4.9, 6.0, "第二句")], [(2.1, 4.8, "这里有一整句的话")])
    _patch(monkeypatch, Recorder())
    assert sp.step8_package(ctx).ok


def test_speech_before_the_first_and_after_the_last_subtitle_is_not_looked_at(tmp_path, monkeypatch):
    ctx = _clip(tmp_path, FINAL, [(0.0, 1.9, "片头有一些话说在前面"), (9.5, 14.0, "片尾也有一些话说在后面")])
    _patch(monkeypatch, Recorder())
    assert sp.step8_package(ctx).ok


def test_dictation_that_is_not_whole_inside_the_gap_and_less_than_four_spoken_characters_do_not_count():
    final = [{"start_s": 0.0, "end_s": 2.0, "text": "a"}, {"start_s": 7.0, "end_s": 9.0, "text": "b"}]
    crossing = [{"start_s": 1.5, "end_s": 4.0, "text": "这句压着前一句的尾巴"}]
    short = [{"start_s": 3.0, "end_s": 4.0, "text": "好的，嗯"}, {"start_s": 5.0, "end_s": 6.0, "text": "对啊"}]
    assert package_guards.gaps_with_speech(final, crossing) == [] and package_guards.gaps_with_speech(final, short) == []
    both = [{"start_s": 3.0, "end_s": 4.0, "text": "好的啊"}, {"start_s": 5.0, "end_s": 6.0, "text": "对吧"}]      # 好的 + 对吧: 4 spoken characters in all
    assert [(g["start_s"], g["end_s"]) for g in package_guards.gaps_with_speech(final, both)] == [(2.0, 7.0)]


def test_no_dictation_file_does_not_stop_the_step_and_process_says_the_check_was_not_done(tmp_path, monkeypatch):
    ctx = _clip(tmp_path, FINAL, None)
    _patch(monkeypatch, Recorder())
    out = sp.step8_package(ctx)
    assert out.ok and "没有机器听写（asr/bcut.srt），未做有声无字幕检查" in out.process


def test_with_a_dictation_file_process_has_no_such_line(tmp_path, monkeypatch):
    ctx = _clip(tmp_path, FINAL, [(0.0, 2.0, "第一句")])
    _patch(monkeypatch, Recorder())
    out = sp.step8_package(ctx)
    assert out.ok and not any("未做有声无字幕检查" in line for line in out.process)


# ----- D2 ---------------------------------------------------------------------------------------------------------------
def _old_package(ctx, *, sealed):
    old = sp.package_dir(ctx)
    old.mkdir(parents=True, exist_ok=True)
    (old / "marker").write_text("first publication evidence", encoding="utf-8")
    if sealed:
        (old / "review_manifest.json").write_text("{}", encoding="utf-8")
    return old


def _registry(monkeypatch, *rows):
    monkeypatch.setattr(title_history, "load_publication_registry", lambda: {"entries": list(rows)})


PUBLISHED = {"candidate_id": "fm-20261024-clip", "status": "published", "bvid": "BV1"}


@pytest.mark.parametrize("candidate", ["fm-20261024-clip", "clip"])
def test_a_sealed_package_of_a_published_manuscript_is_not_deleted_and_step_8_fails(tmp_path, monkeypatch, candidate):
    ctx, _ = _prepared(tmp_path)
    old = _old_package(ctx, sealed=True)
    _registry(monkeypatch, {**PUBLISHED, "candidate_id": candidate})
    rec = Recorder()
    _patch(monkeypatch, rec)
    out = sp.step8_package(ctx)
    assert not out.ok and out.reason == "PUBLISHED_PACKAGE_PROTECTED" and "已发布的包不许覆盖" in out.note and "`revise` 建后继片" in out.note
    assert (old / "marker").read_text(encoding="utf-8") == "first publication evidence" and (old / "review_manifest.json").is_file()
    assert rec.calls == []


def test_a_sealed_package_that_is_not_published_is_rebuilt_as_before(tmp_path, monkeypatch):
    ctx, _ = _prepared(tmp_path)
    old = _old_package(ctx, sealed=True)
    _registry(monkeypatch, {**PUBLISHED, "status": "hold_pending_review"}, {**PUBLISHED, "candidate_id": "fm-20261024-clip2", "bvid": "B2"})
    rec = Recorder()
    _patch(monkeypatch, rec)
    assert sp.step8_package(ctx).ok
    assert [c[0] for c in rec.calls] == ["init", "check"] and not (old / "marker").exists()


def test_an_unsealed_package_of_a_published_manuscript_is_rebuilt_as_before(tmp_path, monkeypatch):
    ctx, _ = _prepared(tmp_path)
    old = _old_package(ctx, sealed=False)
    _registry(monkeypatch, PUBLISHED)
    rec = Recorder()
    _patch(monkeypatch, rec)
    assert sp.step8_package(ctx).ok and not (old / "marker").exists()


@pytest.mark.parametrize("error", [PermissionError("root only"), OSError("gone"), ValueError("PUBLICATION_REGISTRY_SCHEMA_INVALID")])
def test_a_registry_that_cannot_be_read_stops_step_8_and_leaves_the_sealed_package(tmp_path, monkeypatch, error):
    ctx, _ = _prepared(tmp_path)
    old = _old_package(ctx, sealed=True)

    def unreadable():
        raise error

    monkeypatch.setattr(title_history, "load_publication_registry", unreadable)
    rec = Recorder()
    _patch(monkeypatch, rec)
    out = sp.step8_package(ctx)
    assert not out.ok and out.reason == "PUBLISHED_PACKAGE_PROTECTED" and "读不到出版登记" in out.note
    assert (old / "marker").is_file() and rec.calls == []


def test_a_registry_that_is_never_asked_when_there_is_no_sealed_package(tmp_path, monkeypatch):
    ctx, _ = _prepared(tmp_path)
    monkeypatch.setattr(title_history, "load_publication_registry", lambda: pytest.fail("the registry was read"))
    _patch(monkeypatch, Recorder())
    assert sp.step8_package(ctx).ok
    _old_package(ctx, sealed=False)
    assert sp.step8_package(ctx).ok


def test_a_successors_sealed_package_is_protected_by_the_originals_publication(tmp_path, monkeypatch):
    ctx, _ = _prepared(tmp_path)
    ctx.state.data["clip"]["cid"] = "clip_trim"
    old = _old_package(ctx, sealed=True)
    assert old.name == "fm-20261024-clip"
    _registry(monkeypatch, PUBLISHED)
    _patch(monkeypatch, Recorder())
    assert sp.step8_package(ctx).reason == "PUBLISHED_PACKAGE_PROTECTED" and (old / "marker").is_file()


def _through_the_media_host(tmp_path, monkeypatch, answer):
    """The registry is root-only here: the check goes through the media host's projection (as the title history does)."""
    ctx, _ = _prepared(tmp_path)
    old = _old_package(ctx, sealed=True)
    monkeypatch.setattr(title_history, "load_publication_registry", lambda: (_ for _ in ()).throw(PermissionError("root-only")))
    seen = []

    def script(name, body, remote):
        seen.append((name, json.loads(remote.files[f"{ctx.remote_dir}/title/history_request.json"])))
        return answer

    ctx.adapters.remote.on_script = lambda name, body, remote: script(name, body, remote) if name == "s8-published-status" else RemoteResult(0, "", "")
    _patch(monkeypatch, Recorder())
    return ctx, old, seen


def test_a_root_only_registry_is_read_through_the_media_hosts_projection(tmp_path, monkeypatch):
    ctx, old, seen = _through_the_media_host(tmp_path, monkeypatch, RemoteResult(0, json.dumps({"published": True}), ""))
    out = sp.step8_package(ctx)
    assert out.reason == "PUBLISHED_PACKAGE_PROTECTED" and "已发布的包不许覆盖" in out.note and (old / "marker").is_file()
    assert seen == [("s8-published-status", {"candidate_id": "clip", "check_published": True})]


def test_the_projection_saying_not_published_lets_step_8_rebuild(tmp_path, monkeypatch):
    ctx, old, _seen = _through_the_media_host(tmp_path, monkeypatch, RemoteResult(0, json.dumps({"published": False}), ""))
    assert sp.step8_package(ctx).ok and not (old / "marker").exists()


@pytest.mark.parametrize("answer", [RemoteResult(1, "", "sudo: a password is required"), RemoteResult(0, "not json", ""),
                                    RemoteResult(0, json.dumps({"published": "yes"}), "")])
def test_a_projection_that_fails_or_answers_badly_stops_step_8_and_leaves_the_package(tmp_path, monkeypatch, answer):
    ctx, old, _seen = _through_the_media_host(tmp_path, monkeypatch, answer)
    out = sp.step8_package(ctx)
    assert out.reason == "PUBLISHED_PACKAGE_PROTECTED" and "读不到出版登记" in out.note and (old / "marker").is_file()
