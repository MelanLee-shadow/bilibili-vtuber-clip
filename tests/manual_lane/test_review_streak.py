"""The unattended-upload count (review_streak): broadcasts in a row that went online with nothing to change."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.autoslice.manual_lane import review_streak as rs


PROOF = {"class": "k", "generator": "g", "guard": "tests/x.py::test_g", "controls": "c",
         "unseen_members": [{"member": "m1", "entered": "existing", "result": "ok"},
                            {"member": "m2", "entered": "upstream", "result": "ok"}]}


def _row(day: str, found: str = "before_upload", status: str = "landed", **kw: object) -> dict[str, object]:
    return {"day": day, "clips": ["c"], "found": found, "by": "x", "noticed": day, "problem": "p", "why_passed": "w",
            "landed": "check" if status == "landed" else "", "status": status, **(PROOF if status == "landed" else {}),
            **kw}


def _ledger(tmp_path: Path, rows: list[dict[str, object]]) -> Path:
    path = tmp_path / rs.LEDGER
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    return path


def _task(tmp_path: Path, clips: dict[str, str], *, not_uploaded: tuple[str, ...] = ()) -> Path:
    task = tmp_path / "task"
    (task / "state").mkdir(parents=True)
    for cid, day in clips.items():
        upload = {} if cid in not_uploaded else {"status": "DONE"}
        doc = {"clip": {"recording_date": day}, "steps": {"upload": upload}}
        (task / "state" / f"{cid}.json").write_text(json.dumps(doc), encoding="utf-8")
    return task


def test_streak_counts_clean_broadcasts_back_to_the_first_one_with_a_change() -> None:
    days = {"2026-11-15": ["a"], "2026-11-16": ["b"], "2026-11-18": ["c"], "2026-11-20": ["d"], "2026-11-21": ["e"]}
    report = rs.tally([_row("2026-11-16"), _row("2026-11-18", "after_publish")], days)
    assert report["streak"] == 2 and report["latest_unclean"] == "2026-11-18" and not report["reached"]
    assert [d["clean"] for d in report["days"]] == [True, False, False, True, True]


def test_a_revision_makes_the_day_unclean_even_when_the_account_forgot_it() -> None:
    report = rs.tally([], {"2026-11-20": ["d", "d_r2"], "2026-11-21": ["e"]})
    assert report["streak"] == 1 and report["unrecorded"] == ["2026-11-20"]
    assert report["days"][0]["revisions"] == ["d_r2"] and report["days"][0]["clips"] == 1
    recorded = rs.tally([_row("2026-11-20", "after_publish")], {"2026-11-20": ["d", "d_r2"]})
    assert recorded["unrecorded"] == []


def test_days_before_the_count_start_and_rows_that_do_not_count_are_left_out() -> None:
    days = {"2026-11-14": ["z"], "2026-11-15": ["a"]}
    report = rs.tally([_row("2026-11-14"), _row("2026-11-15", counts=False)], days)
    assert [d["day"] for d in report["days"]] == ["2026-11-15"] and report["streak"] == 1


def test_ten_clean_broadcasts_reach_the_target() -> None:
    days = {f"2026-12-{d:02d}": [f"c{d}"] for d in range(1, 11)}
    assert rs.tally([_row("2026-11-30")], days)["reached"] is True
    assert rs.tally([_row("2026-12-01")], days)["streak"] == 9


@pytest.mark.parametrize("bad, words", [
    ({"day": "11-20"}, "YYYY-MM-DD"), ({"found": "later"}, "found"), ({"status": "done"}, "status"),
    ({"clips": []}, "clips"), ({"status": "landed", "landed": " "}, "what it became"), ({"counts": "no"}, "counts"),
    ({"guard": ""}, "guard"), ({"unseen_members": PROOF["unseen_members"][:1]}, "two or more"),
    ({"unseen_members": [{**m, "entered": "existing"} for m in PROOF["unseen_members"]]}, "upstream input"),
    ({"unseen_members": [{"member": "m", "entered": "upstream"}, {"member": "n", "entered": "existing"}]}, "result")])
def test_a_malformed_row_stops_the_reading(tmp_path: Path, bad: dict[str, object], words: str) -> None:
    with pytest.raises(rs.LedgerError, match=words):
        rs.load(_ledger(tmp_path, [{**_row("2026-11-20"), **bad}]))


def test_write_counts_the_uploads_of_the_task_and_keeps_the_report(tmp_path: Path) -> None:
    task = _task(tmp_path, {"a": "2026-11-20", "b": "2026-11-21", "b_r2": "2026-11-21", "c": "2026-11-22"},
                 not_uploaded=("c",))
    report = rs.write(task, _ledger(tmp_path, [_row("2026-11-21", "after_publish")]))
    assert [d["day"] for d in report["days"]] == ["2026-11-20", "2026-11-21"] and report["streak"] == 0
    assert json.loads((task / rs.STREAK_FILE).read_text(encoding="utf-8"))["streak"] == 0
    assert "连续无改场次 0/10" in rs.line(report)


def test_open_rows_are_counted_and_shown_in_the_table() -> None:
    rows = [_row("2026-11-14", status="open"), _row("2026-11-15")]
    assert rs.tally(rows, {})["open"] == 1
    table = rs.markdown(rows)
    assert "**未落成**" in table and table.count("\n") == 4 and "m2（上游新进）：ok" in table


def test_an_open_row_needs_no_proof_and_a_withdrawn_row_says_why(tmp_path: Path) -> None:
    assert rs.load(_ledger(tmp_path, [_row("2026-11-20", status="open")]))[0]["status"] == "open"
    with pytest.raises(rs.LedgerError, match="what it became"):
        rs.load(_ledger(tmp_path, [_row("2026-11-20", status="withdrawn")]))
