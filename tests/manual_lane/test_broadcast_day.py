"""U429 (公开规则): a broadcast day runs from 04:00 to 04:00 Beijing time; a recording belongs to the day its stream
started. One definition (discover_media.broadcast_days) for the days discover lists, what a day holds, and when it is over."""
from __future__ import annotations

from fakes import FakeRemote

from src.autoslice.manual_lane import discover_media as dm
from src.autoslice.manual_lane.adapters import RemoteResult

ROOM = "22966160"


def _t(stem):
    return dm.started_at(f"{ROOM}_{stem}")


def _stream(*stems, last=None):
    """Recordings cut back to back: each one written until the next starts (the last one until `last`, else +30 min)."""
    starts = [_t(s) for s in stems]
    ends = starts[1:] + [last if last is not None else starts[-1] + 1800]
    return {f"{ROOM}_{s}": (a, b) for s, a, b in zip(stems, starts, ends, strict=True)}


def test_the_day_turns_at_four_in_the_morning_beijing_time():
    days = dm.broadcast_days({**_stream("20261118-03-20-00", last=_t("20261118-03-25-00")), **_stream("20261118-04-00-00")})
    assert days == {f"{ROOM}_20261118-03-20-00": "2026-11-17", f"{ROOM}_20261118-04-00-00": "2026-11-18"}
    assert dm.started_at(f"{ROOM}_20261118-04-00-00") == 1794945600.0          # Beijing, whatever the host's own zone (20:00Z)


def test_a_stream_running_past_four_stays_whole_on_the_day_it_started():
    stream = _stream("20261118-23-57-37", "20261119-00-27-37", "20261119-03-57-40", "20261119-04-27-41")
    assert set(dm.broadcast_days(stream).values()) == {"2026-11-18"}


def test_a_stream_after_midnight_is_the_day_before_and_a_new_stream_after_four_is_a_new_day():
    late = _stream("20261119-01-10-00", "20261119-01-40-00")                 # started 01:10: still the 18th's broadcast
    morning = _stream("20261119-09-00-00")                                    # an hour-long break, then 09:00: the 19th
    restarted = _stream("20261119-04-40-00")                                  # a break of over 30 minutes across 04:00
    days = dm.broadcast_days({**late, **morning, **restarted})
    assert [days[k] for k in late] == ["2026-11-18", "2026-11-18"]
    assert days[next(iter(morning))] == "2026-11-19" and days[next(iter(restarted))] == "2026-11-19"


def test_a_recording_without_a_start_time_in_its_name_stops_rather_than_guessing_its_day():
    try:
        dm.started_at("recording")
    except dm.DiscoverError as exc:
        assert exc.code == "RECORDING_NAME_WITHOUT_START"
    else:
        raise AssertionError("no error")


class Listing:
    """The media host's answer: NOW, the directories that exist and their files (path, size, mtime)."""

    def __init__(self, now, files):
        self.now, self.files = now, files          # {calendar dir: [(stem, size, written)]}

    def __call__(self, name, body, remote):
        if name.startswith("disc-dates"):
            rows = "".join(f"D\t{d}\n" for d in self.files) + "".join(
                f"R\t{dm.archive_dir(d)}\t{stem}.flv\t{w}\n" for d, rows in self.files.items() for stem, _s, w in rows)
            return RemoteResult(0, f"NOW {self.now}\n{rows}", "")
        assert name.startswith("disc-inv-"), name
        out = [f"NOW {self.now}"]
        for d, rows in self.files.items():
            if dm.archive_dir(d) in body:
                out.append(f"DIR\t{d}")
                for stem, size, written in rows:
                    out += [f"F\t{dm.archive_dir(d)}/{stem}.flv\t{size}\t{written}", f"F\t{dm.archive_dir(d)}/{stem}.xml\t10\t{written}"]
        return RemoteResult(0, "\n".join(out) + "\n", "")


def _rows(stream, size=10**9):
    return [(stem, size, written) for stem, (_started, written) in stream.items()]


def _host(now):
    """花礼 11-18 (goldfish 5): started 23:57, the recorder's next file in the 19th's directory; the 18th's directory also
    holds 01:00 of the night before (another stream, the 17th's)."""
    night_before = _stream("20261118-01-00-00", last=_t("20261118-01-20-00"))
    evening = _stream("20261118-23-57-37", "20261119-00-27-37", last=_t("20261119-00-50-00"))
    return Listing(now, {"2026-11-18": _rows(night_before) + _rows(dict(list(evening.items())[:1])),
                         "2026-11-19": _rows(dict(list(evening.items())[1:]))})


def test_a_day_holds_its_streams_from_both_directories_and_not_the_small_hours_of_the_day_before():
    remote = FakeRemote(on_script=_host(now=_t("20261119-03-00-00")))
    inv = dm.inventory(remote, "2026-11-18")
    assert [s.name[-17:] for s in inv.segments] == ["20261118-23-57-37", "20261119-00-27-37"]
    assert all(s.date == "2026-11-18" for s in inv.segments) and inv.exists
    assert [s.name[-17:] for s in dm.inventory(remote, "2026-11-17").segments] == ["20261118-01-00-00"]
    assert dm.list_dates(remote)[0] == ["2026-11-17", "2026-11-18"]          # the 19th's directory is not a day of its own


def test_the_day_is_over_when_its_stream_has_been_quiet_or_the_next_days_stream_began():
    still = dm.inventory(FakeRemote(on_script=_host(now=_t("20261119-01-00-00"))), "2026-11-18")
    assert not still.settled(60, 24) and still.finished(60)[1]               # the 00:27 file was written ten minutes ago
    quiet = dm.inventory(FakeRemote(on_script=_host(now=_t("20261119-02-00-00"))), "2026-11-18")
    assert quiet.settled(60, 24) and not quiet.finished(60)[1]
    host = _host(now=_t("20261119-20-10-00"))
    host.files["2026-11-19"] += _rows(_stream("20261119-20-00-00"))          # the 19th's stream began ten minutes ago
    nextday = dm.inventory(FakeRemote(on_script=host), "2026-11-18")
    assert nextday.next_day and nextday.settled(60, 24)
    assert not dm.inventory(FakeRemote(on_script=host), "2026-11-19").settled(60, 24)
