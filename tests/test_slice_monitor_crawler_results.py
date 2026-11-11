"""The slice monitor reports a read-only crawler whose last run did not succeed."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "slice_monitor_under_test",
    Path(__file__).resolve().parents[1] / "scripts" / "slice_monitor.py",
)
monitor = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(monitor)


def test_parse_crawler_results_keeps_only_crawler_units():
    text = (
        "oci3-crawl-timely-terms.service|success|0\n"
        "oci3-crawl-streamer-dynamics.service|exit-code|2\n"
        "unrelated.service|failed|1\n"
        "garbage line without separator\n"
    )

    assert monitor.parse_crawler_results(text) == {
        "oci3-crawl-timely-terms.service": ("success", "0"),
        "oci3-crawl-streamer-dynamics.service": ("exit-code", "2"),
    }


def test_crawler_problems_flags_every_non_success_result_and_nothing_else():
    problems = monitor.crawler_problems(
        {
            "oci3-crawl-timely-terms.service": ("success", "0"),
            "oci3-crawl-streamer-dynamics.service": ("exit-code", "2"),
            "oci3-crawl-topic-entity.service": ("", ""),
        }
    )

    assert [problem["id"] for problem in problems] == [
        "crawler_streamer-dynamics_failed",
        "crawler_topic-entity_failed",
    ]
    assert all(problem["sev"] == "WARN" for problem in problems)
    assert "logs/streamer-dynamics.log" in problems[0]["fix"]


def test_crawler_problems_flags_nonzero_exit_status_even_when_result_says_success():
    problems = monitor.crawler_problems({"oci3-crawl-timely-terms.service": ("success", "1")})

    assert [problem["id"] for problem in problems] == ["crawler_timely-terms_failed"]
    assert "ExitStatus=1" in problems[0]["msg"]


def test_crawler_problems_is_silent_when_every_crawler_succeeded_or_nothing_was_probed():
    assert monitor.crawler_problems({"oci3-crawl-timely-terms.service": ("success", "0")}) == []
    assert monitor.crawler_problems(None) == []


def test_autoslice_probe_carries_crawler_results_into_the_alert_verdict(monkeypatch):
    stdout = (
        "2026-11-10T12:00:00+0000 20261110:review_ready\n__SEP__\n__CRAWL__\n"
        "oci3-crawl-timely-terms.service|success|0\n"
        "oci3-crawl-streamer-dynamics.service|exit-code|2\n"
    )
    monkeypatch.setattr(
        monitor.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=stdout, stderr=""),
    )
    autoslice = monitor.run_autoslice_probe()
    assert autoslice["crawlers"]["oci3-crawl-streamer-dynamics.service"] == ("exit-code", "2")

    probe = {
        "ok": True, "now": 0.0, "disk": {}, "flags": {}, "audio": {},
        "procs": {"recorder": 1, "scan": 0, "upload": 0},
        "rooms": {monitor.PRIMARY_ROOM: {}},
        "autoslice": autoslice,
    }
    verdict, problems, _actions, notes = monitor.evaluate(probe, {})

    assert "crawler_streamer-dynamics_failed" in {p["id"] for p in problems}
    assert "crawler_timely-terms_failed" not in {p["id"] for p in problems}
    assert any(n.startswith("爬虫 unit") and "streamer-dynamics=exit-code/2" in n for n in notes)
    assert verdict != "OK"
