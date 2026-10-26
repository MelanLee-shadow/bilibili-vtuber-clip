"""Timing contracts: overlap, incomplete evidence, producer exit transparency."""
from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.autoslice import run_time_ledger as ledger


def stamp(seconds):
    return datetime.fromtimestamp(1_800_000_000 + seconds, timezone.utc).isoformat()


def save(root, name, data):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    os.utime(path, (1_800_000_000, 1_800_000_000))
    return path


def exchange(root, name, start, end=None, **terminal):
    save(root, f"cpa-text-exchanges/request-{name}/request.json", {"started_utc": stamp(start)})
    if end is not None:
        save(root, f"cpa-text-exchanges/request-{name}/terminal.json",
             {"ended_utc": stamp(end), "status": "COMPLETED", **terminal})


def test_parallel_calls_use_union_clip_window_and_keep_missing_end(tmp_path):
    assert ledger.timestamp("2026-10-24T00:00:00.123456789Z") == ledger.timestamp("2026-10-24T00:00:00.123456Z")
    exchange(tmp_path, "a", 0, 20)
    exchange(tmp_path, "b", 10, 30)
    exchange(tmp_path, "open", 35)
    exchange(tmp_path, "old", -50, -20)
    result = ledger.build_ledger(ledger.collect_sources(tmp_path), start=stamp(5), end=stamp(40))
    assert result["totals"]["wall_s"] == 35
    assert result["totals"]["call_inflight_s"] == 25
    assert result["totals"]["call_duration_sum_s"] == 40
    assert result["failure_counts"]["text"]["untimed_or_open"] == 1
    assert len(result["spans"]) == 3
    with pytest.raises(ValueError):
        ledger.merge_intervals([(2, 1)])
    assert ledger.merge_intervals([(2, 3), (0, 2), (1, 2)]) == [(0, 3)]


def test_nested_stage_scope_is_not_continuous_activity_and_writes_split_gaps(tmp_path):
    lines = [f"PRODUCER_STAGE {stamp(0)} START pipeline n=1",
             f"PRODUCER_STAGE {stamp(10)} START discovery n=1",
             f"PRODUCER_STAGE {stamp(20)} END discovery n=1 elapsed_s=10.000",
             f"PRODUCER_STAGE {stamp(1000)} END pipeline n=1 elapsed_s=1000.000"]
    manifest = save(tmp_path, "time-ledger-artifacts.json", {
        "schema_version": "run-time-ledger-artifacts.v1",
        "files": [{"path": "current.srt", "mtime_unix_s": 1_800_000_500, "size_bytes": 20}]})
    result = ledger.build_ledger(ledger.collect_sources(tmp_path, stage_lines=lines),
                                 start=stamp(0), end=stamp(1000))
    assert result["totals"]["instrumented_busy_s"] == 10
    assert result["totals"]["dead_time_s"] == 980
    assert [g["duration_s"] for g in result["gaps"] if g["dead"]] == [480, 500]
    assert {r["name"]: r["union_s"] for r in result["stage_table"]} == {"pipeline": 1000, "discovery": 10}
    assert manifest.exists()


def test_audio_deduplicates_context_and_acquisition_without_fabricating_latency(tmp_path):
    data = {"status": "COMPLETED", "response_source": {"path": "/original/acquisition/raw.json"},
            "provider_audio": {"duration_ms": 999999},
            "provider_failures": [{"http_status": 429}, {"http_status": 429},
                                  {"error_type": "TimeoutError"}]}
    save(tmp_path, "gemini-range-context/x/execution.json", data)
    save(tmp_path, "gemini-range-acquisition/x/execution.json", data)
    result = ledger.build_ledger(ledger.collect_sources(tmp_path), start=stamp(0), end=stamp(1000))
    assert result["failure_counts"]["audio"] == {"records": 1, "http_429": 2, "timeouts": 1, "untimed_or_open": 1}
    assert result["totals"]["call_inflight_s"] == 0
    assert result["spans"][0]["duration_s"] is None


def test_argon_transport_wait_and_future_task_span(tmp_path):
    save(tmp_path, "argon/task/calls/a.start.json", {"method": "GET", "path": "batches/1", "started_unix_s": 1_800_000_010})
    save(tmp_path, "argon/task/calls/a.outcome.json", {"http_status": 429, "finished_unix_s": 1_800_000_020})
    save(tmp_path, "argon/task/calls/b.start.json", {"method": "POST", "path": "models/gemini:generateContent", "started_unix_s": 1_800_000_015})
    save(tmp_path, "argon/task/calls/b.outcome.json", {"transport_reason": "HTTP_TIMEOUT", "finished_unix_s": 1_800_000_030})
    path = tmp_path / "time-spans.jsonl"
    path.write_text(json.dumps({"schema_version": "run-time-span.v1", "span_id": "f1", "kind": "wait",
                                "name": "backoff", "started_utc": stamp(50), "ended_utc": stamp(70)}) + "\ninvalid\n")
    os.utime(path, (1_800_000_000, 1_800_000_000))
    result = ledger.build_ledger(ledger.collect_sources(tmp_path), start=stamp(0), end=stamp(100))
    assert result["totals"]["call_inflight_s"] == 15
    assert len(result["slowest_waits"]) == 2
    assert result["failure_counts"]["audio"]["http_429"] == 1
    assert result["failure_counts"]["audio"]["timeouts"] == 1
    assert result["coverage"]["issues"]["invalid_task_span"] == 1


def test_missing_sources_need_explicit_window_and_do_not_read_audits_or_secrets(tmp_path, monkeypatch):
    (tmp_path / "cpa.env").write_text("must not read")
    (tmp_path / "c.chat-authority.json").write_text("must not read")
    original = Path.read_text

    def guarded(path, *args, **kwargs):
        assert path.name not in {"cpa.env", "c.chat-authority.json"}
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded)
    sources = ledger.collect_sources(tmp_path)
    with pytest.raises(ValueError, match="window"):
        ledger.build_ledger(sources)
    result = ledger.build_ledger(sources, start=stamp(0), end=stamp(100))
    assert result["failure_counts"]["text"]["records"] == 0
    assert result["totals"]["wall_s"] == 100
    with pytest.raises(ValueError, match="timestamp"):
        ledger.build_ledger(sources, start="2026-10-24T00:00:00", end=stamp(100))


def test_live_stage_capture_does_not_duplicate_log_source(tmp_path):
    line = f"PRODUCER_STAGE {stamp(0)} START unit n=1\n"
    (tmp_path / "producer.log").write_text(line)
    sources = ledger.collect_sources(tmp_path, stage_lines=[line])
    assert len(sources["rows"]) == 1


@pytest.mark.parametrize("error", [None, SystemExit(7), RuntimeError("same exception")])
def test_real_main_exit_writes_ledger_and_preserves_return_exception_output(tmp_path, monkeypatch, capsys, error):
    from scripts import produce_slice_package as producer
    from src.autoslice import production_attempts
    from src.autoslice.producer_stage_log import producer_stage

    request = SimpleNamespace(cid="c", out_root=tmp_path, spec={"date": "2026-10-24"})
    monkeypatch.setattr(producer, "parse_producer_args", lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr(producer, "load_producer_request", lambda *a, **k: request)
    monkeypatch.setattr(production_attempts, "observed_production_attempt", lambda **k: nullcontext())

    def produce(*args):
        with producer_stage("unit.work"):
            print('{"candidate_id":"c"}')
            if error is not None:
                raise error
        return 23

    monkeypatch.setattr(producer, "_produce_loaded_request", produce)
    if error is None:
        assert producer.main([]) == 23
    else:
        with pytest.raises(type(error)) as exc:
            producer.main([])
        assert exc.value is error
    result = json.loads((tmp_path / "c.time-ledger.json").read_text())
    assert result["stage_table"][0]["open_count"] == int(error is not None)
    assert result["failure_counts"]["text"]["records"] == 0
    captured = capsys.readouterr()
    assert captured.out == '{"candidate_id":"c"}\n'
    assert all(line.startswith("PRODUCER_STAGE ") for line in captured.err.splitlines())


@pytest.mark.parametrize("error", [None, SystemExit(9), RuntimeError("original")])
def test_observer_failure_never_masks_exit(tmp_path, monkeypatch, capsys, error):
    def fail(*args, **kwargs):
        raise OSError("disk unavailable")

    monkeypatch.setattr(ledger, "write_ledger", fail)
    original_stderr = __import__("sys").stderr
    if error is None:
        with ledger.producer_run_ledger(out_root=tmp_path, cid="c"):
            print("unchanged")
    else:
        with pytest.raises(type(error)) as exc:
            with ledger.producer_run_ledger(out_root=tmp_path, cid="c"):
                raise error
        assert exc.value is error
    assert __import__("sys").stderr is original_stderr
    assert not (tmp_path / "c.time-ledger.json").exists()
    assert capsys.readouterr().err == ""


def test_offline_cli_writes_json_and_summary(tmp_path, capsys):
    from scripts.run_time_report import main
    root = tmp_path / "input"
    root.mkdir()
    exchange(root, "a", 0, 20, provider_diagnostics={"provider_http_status": 429, "error_type": "TimeoutError"})
    output, summary = tmp_path / "ledger.json", tmp_path / "ledger.md"
    assert main([str(root), "--start", stamp(0), "--end", stamp(100), "--output", str(output), "--summary", str(summary)]) == 0
    result = json.loads(output.read_text())
    assert result["failure_counts"]["text"]["http_429"] == 1
    assert result["failure_counts"]["text"]["timeouts"] == 1
    assert "总墙钟 100.000 s" in summary.read_text()
    assert "最慢 10 个调用" in capsys.readouterr().out
