"""On the media host the session's chat is read from the recorder's archive there, not over ssh to the recorder."""

import pytest

from src.autoslice.manual_lane import cli, session_events
from src.autoslice.manual_lane.adapters import DanmakuSource
from src.autoslice.manual_lane.chat_archive import ArchiveChat, archive_room_dir
from src.autoslice.manual_lane.layout import layout


def _xml(start: str, comments: list[tuple[float, str]]) -> bytes:
    rows = "".join(f'<d p="{t},1,25,0,0,0,0,{i}" user="u">{text}</d>' for i, (t, text) in enumerate(comments))
    return f'<?xml version="1.0"?><i><BililiveRecorderRecordInfo start_time="{start}"/>{rows}</i>'.encode()


def _archive(tmp_path):
    day = tmp_path / "room" / "2026-01-02"
    day.mkdir(parents=True)
    (day / "a.xml").write_bytes(_xml("2026-01-02T12:00:00+08:00", [(5.0, "早先的一句"), (1700.0, "段尾的一句")]))
    (day / "b.xml").write_bytes(_xml("2026-01-02T12:30:00+08:00", [(7.0, "本段的一句")]))
    (day / "b.flv").write_bytes(b"")
    return ArchiveChat(room_dir=str(tmp_path / "room"), reader=())


def test_the_recorders_path_is_answered_from_the_archive(tmp_path):
    chat = _archive(tmp_path)
    directory = f"{layout().danmaku_dir}/2026-01-02"
    assert chat.names(directory) == ["a.xml", "b.xml"]
    assert chat.fetch(f"{directory}/b.xml") == (tmp_path / "room/2026-01-02/b.xml").read_bytes()


def test_the_whole_session_is_read_through_it(tmp_path):
    chat = _archive(tmp_path)
    directory = f"{layout().danmaku_dir}/2026-01-02"
    rows, unknown = session_events.load(chat, directory, "b", chat.fetch(f"{directory}/b.xml"))
    assert unknown == [] and sorted(r["text"] for r in rows) == ["早先的一句", "本段的一句", "段尾的一句"]
    assert min(r["t_s"] for r in rows) < 0          # the earlier segment's line lies before this segment's start


def test_a_file_that_is_not_there_and_a_foreign_path_are_errors_not_empty_answers(tmp_path):
    chat = _archive(tmp_path)
    with pytest.raises(RuntimeError):
        chat.fetch(f"{layout().danmaku_dir}/2026-01-02/missing.xml")
    with pytest.raises(RuntimeError):
        chat.names("/somewhere/else/2026-01-02")


def test_the_controller_on_the_media_host_uses_the_archive(tmp_path):
    assert isinstance(cli._adapters(str(tmp_path / "steps"), True).danmaku, ArchiveChat)
    assert isinstance(cli._adapters(str(tmp_path / "steps"), False).danmaku, DanmakuSource)
    assert archive_room_dir().endswith("/" + layout().room_id)
