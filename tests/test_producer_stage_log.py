"""J-lite: producer stage timestamps are one-line, text-free and decision-neutral."""
from __future__ import annotations

from datetime import datetime
import inspect
import json
import re

import pytest

from src.autoslice import producer_stage_log as stage_log
from src.autoslice import producer_text_pipeline as pipeline

LINE = re.compile(
    r"PRODUCER_STAGE (?P<ts>\S+) (?P<event>START|END) (?P<stage>[a-z_.]+)(?P<fields>( [a-z_]+=\S+)*)"
)


def _lines(err: str) -> list[re.Match]:
    rows = [line for line in err.splitlines() if line.startswith(stage_log.PREFIX)]
    matches = [LINE.fullmatch(line) for line in rows]
    assert all(matches), rows
    return matches


def test_stage_emits_one_utc_line_per_event_and_drops_free_text(capsys):
    with stage_log.producer_stage("unit.alpha", count=3, mode="cpa", text="她说 原文", path="/a b"):
        pass
    with stage_log.producer_stage("unit.alpha"):
        pass
    err = capsys.readouterr().err
    rows = _lines(err)
    assert [(m["event"], m["stage"]) for m in rows] == [
        ("START", "unit.alpha"), ("END", "unit.alpha"), ("START", "unit.alpha"), ("END", "unit.alpha")]
    for m in rows:
        stamp = datetime.fromisoformat(m["ts"].replace("Z", "+00:00"))
        assert m["ts"].endswith("Z") and stamp.utcoffset().total_seconds() == 0
    assert " count=3" in rows[0]["fields"] and " mode=cpa" in rows[0]["fields"]
    assert "她说" not in err and "原文" not in err and "/a b" not in err and "text=" not in err
    assert " elapsed_s=" in rows[1]["fields"] and " n=2" in rows[3]["fields"]
    assert "{" not in err and "}" not in err


def test_failure_writes_no_extra_line_and_reraises_the_same_exception(capsys):
    error = SystemExit("FINAL_REVIEW_RELEASE_BLOCKED: FINAL_REVIEW_DISCOVERY_INCOMPLETE")
    with pytest.raises(SystemExit) as raised:
        with stage_log.producer_stage("unit.failing"):
            raise error
    assert raised.value is error
    rows = _lines(capsys.readouterr().err)
    assert [(m["event"], m["stage"]) for m in rows] == [("START", "unit.failing")]


def test_staged_adapter_returns_unchanged_and_keeps_identity_metadata(capsys):
    def adapter(record, *, flag):
        return {"record": record, "flag": flag}

    wrapped = stage_log.staged("unit.adapter", adapter)
    assert wrapped({"a": 1}, flag=True) == {"record": {"a": 1}, "flag": True}
    assert wrapped.__wrapped__ is adapter and wrapped.__name__ == "adapter"
    assert [m["event"] for m in _lines(capsys.readouterr().err)] == ["START", "END"]


def test_pending_review_contract_source_is_not_changed_by_stage_wrapping():
    """The semantic review contract hashes _run_final_review's own source."""
    assert pipeline._run_final_review.__wrapped__.__name__ == "_run_final_review"
    source = inspect.getsource(pipeline._run_final_review)
    assert source.startswith("def _run_final_review(") and "producer_stage" not in source
    assert inspect.getsource(pipeline._run_final_review.__wrapped__) == source


def test_correction_review_round_is_timestamped(capsys, monkeypatch):
    monkeypatch.setenv("AUTOSLICE_DISABLE_FINAL_REVIEW", "1")
    srt = "1\n00:00:00,000 --> 00:00:01,000\n当前文字\n"
    out, audit = pipeline._run_final_review(
        srt_text=srt, chat_authority_audit={}, handled_entity_cues=set(),
        verify_confusable_entity=lambda _request: None, adapters=None)
    assert out == srt and audit["status"] == "SKIPPED"
    rows = _lines(capsys.readouterr().err)
    assert [(m["event"], m["stage"]) for m in rows] == [
        ("START", "correction_review"), ("END", "correction_review")]


def test_production_log_parsers_ignore_stage_lines():
    """The free runner reads the last JSON block and the failure tail."""
    from scripts import session_autoslice as runner
    classify_talk_failure, last_json_block = runner.classify_talk_failure, runner.last_json_block

    summary = json.dumps({"candidate_id": "c", "nested": {"ok": True}}, indent=2)
    start = f"{stage_log.PREFIX} 2026-10-21T03:13:51.408Z START finalization n=1"
    end = f"{stage_log.PREFIX} 2026-10-21T03:14:51.408Z END finalization n=1 elapsed_s=60.000"
    assert last_json_block(f"{start}\n{summary}\n{end}\n") == json.loads(summary)
    failure = "Traceback (most recent call last):\nSystemExit: FINAL_REVIEW_RELEASE_BLOCKED: FINAL_REVIEW_DISCOVERY_INCOMPLETE\n"
    plain = classify_talk_failure(failure)
    logged = classify_talk_failure(f"{start}\n{failure}")
    assert logged == plain
