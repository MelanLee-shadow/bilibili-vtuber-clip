from datetime import date
from pathlib import Path

import pytest

from scripts.pull_recent_autoslice import (
    delete_protection_excludes,
    list_remote_dates,
    list_remote_subdirs,
    local_only_dirs,
    parse_iso_date,
    pull_date,
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
        stdout = "/remote/channel/2026-07-17\n/remote/channel/2026-07-18\n"

    monkeypatch.setattr(
        "scripts.pull_recent_autoslice.run_checked",
        lambda command: Result(),
    )

    assert list_remote_dates("free", "/remote/channel") == [
        "2026-07-17",
        "2026-07-18",
    ]


@pytest.mark.parametrize("value", ["2026-7-01", "not-a-date", "2026-02-30"])
def test_parse_iso_date_rejects_invalid_values(value: str) -> None:
    with pytest.raises(ValueError):
        parse_iso_date(value)


def test_list_remote_subdirs_parses_nul_separated_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Result:
        stdout = (
            "/r/2026-07-19/舞台切片-萤火虫互动舞台\0"
            "/r/2026-07-19/舞台切片-萤火虫互动舞台/素材\0"
        ).encode("utf-8")

    monkeypatch.setattr(
        "scripts.pull_recent_autoslice.subprocess.run",
        lambda *args, **kwargs: Result(),
    )

    assert list_remote_subdirs("free", "/r/2026-07-19") == {
        "舞台切片-萤火虫互动舞台",
        "舞台切片-萤火虫互动舞台/素材",
    }


def test_local_only_dirs_reports_topmost_and_nested_under_shared(
    tmp_path: Path,
) -> None:
    (tmp_path / "舞台切片-同步").mkdir()
    (tmp_path / "舞台切片-同步" / "人工子目录").mkdir()
    (tmp_path / "正式补切-审阅").mkdir()
    (tmp_path / "正式补切-审阅" / "evidence").mkdir()

    protected = local_only_dirs(tmp_path, {"舞台切片-同步"})

    # The whole local-only tree is covered by its topmost dir; a human dir
    # nested inside a remote-synced dir still gets its own entry.
    assert protected == ["正式补切-审阅", "舞台切片-同步/人工子目录"]


def test_local_only_dirs_matches_decomposed_local_names(tmp_path: Path) -> None:
    nfd_name = "revie\u0301w"  # decomposed e-acute, as APFS may return it
    (tmp_path / nfd_name).mkdir()

    assert local_only_dirs(tmp_path, set()) == [nfd_name]
    assert local_only_dirs(tmp_path, {"revi\u00e9w"}) == []  # NFC twin on remote


def test_delete_protection_excludes_are_anchored_and_escaped() -> None:
    assert delete_protection_excludes(["正式补切-审阅", "a*b?c[d"]) == [
        "--exclude=/正式补切-审阅/",
        "--exclude=/a\\*b\\?c\\[d/",
    ]


def test_pull_date_excludes_only_local_only_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "2026-07-19"
    destination.mkdir()
    (destination / "舞台切片-同步").mkdir()
    (destination / "正式补切-审阅").mkdir()

    monkeypatch.setattr(
        "scripts.pull_recent_autoslice.list_remote_subdirs",
        lambda host, remote_dir: {"舞台切片-同步"},
    )
    commands: list[list[str]] = []
    monkeypatch.setattr(
        "scripts.pull_recent_autoslice.subprocess.run",
        lambda command, **kwargs: commands.append(list(command)),
    )

    pull_date("free", "/r", tmp_path, date(2026, 7, 19))

    assert commands == [
        [
            "rsync",
            "-a",
            "--delete",
            "--exclude=/正式补切-审阅/",
            "--timeout=120",
            "free:/r/2026-07-19/",
            f"{destination}/",
        ]
    ]
