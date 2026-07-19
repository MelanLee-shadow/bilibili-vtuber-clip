from datetime import date
from pathlib import Path

import pytest

from scripts.pull_recent_autoslice import (
    list_remote_dates,
    parse_iso_date,
    read_archived_dates,
    select_recent_dates,
    write_archived_dates,
)


def test_select_recent_dates_honors_window_and_archive() -> None:
    selected = select_recent_dates(
        [
            "branding",
            "2026-07-14",
            "2026-07-15",
            "2026-07-16",
            "2026-07-17",
            "2026-07-18",
        ],
        today=date(2026, 7, 18),
        days=3,
        archived={date(2026, 7, 16)},
    )

    assert selected == [date(2026, 7, 17), date(2026, 7, 18)]


def test_select_recent_dates_tolerates_beijing_date_ahead_of_local() -> None:
    # Delivery dirs are named by Beijing recording date; a post-midnight
    # Beijing stream shows up as "tomorrow" while the Mac clock is still on
    # today.  It must sync immediately, but dates further ahead stay out.
    selected = select_recent_dates(
        ["2026-07-18", "2026-07-19", "2026-07-20"],
        today=date(2026, 7, 18),
        days=2,
        archived=set(),
    )

    assert selected == [date(2026, 7, 18), date(2026, 7, 19)]


def test_archived_dates_round_trip_sorted(tmp_path: Path) -> None:
    archive_path = tmp_path / "archived-dates.txt"
    write_archived_dates(
        archive_path,
        {date(2026, 7, 18), date(2026, 7, 16)},
    )

    assert archive_path.read_text(encoding="utf-8") == "2026-07-16\n2026-07-18\n"
    assert read_archived_dates(archive_path) == {
        date(2026, 7, 16),
        date(2026, 7, 18),
    }


def test_archived_dates_support_comments(tmp_path: Path) -> None:
    archive_path = tmp_path / "archived-dates.txt"
    archive_path.write_text("# uploaded\n2026-07-16 # BV upload complete\n", encoding="utf-8")

    assert read_archived_dates(archive_path) == {date(2026, 7, 16)}


def test_list_remote_dates_uses_printed_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    class Result:
        stdout = "/remote/lidousha/2026-07-17\n/remote/lidousha/2026-07-18\n"

    monkeypatch.setattr(
        "scripts.pull_recent_autoslice.run_checked",
        lambda command: Result(),
    )

    assert list_remote_dates("free", "/remote/lidousha") == [
        "2026-07-17",
        "2026-07-18",
    ]


@pytest.mark.parametrize("value", ["2026-7-01", "not-a-date", "2026-02-30"])
def test_parse_iso_date_rejects_invalid_values(value: str) -> None:
    with pytest.raises(ValueError):
        parse_iso_date(value)
