"""Real wrapper -> selector CLI/report consumers with frozen provider results."""
from __future__ import annotations

import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

import scripts.session_autoslice as runner
from test_song_lane_private_paths import DATE, CID, REAL_RUN, runtime as runtime


BOOTSTRAP = '''
import json
import os
from pathlib import Path
import sys
sys.path.insert(0, {repo!r})
from scripts import run_full_session_selector_cpa_shadow as selector
from src.autoslice.cpa_runtime import record_cpa_event

# Only business provider/materialization work is frozen. The real CLI parser,
# seeded candidate, SRT parsing, recovery, observations and summary writer run.
selector._build_lrc_provider = lambda *_args, **_kwargs: None
selector._build_host_vocal_prover = lambda **_kwargs: None
def frozen_review(**kwargs):
    args = kwargs['args']
    candidate = kwargs['original_candidate']
    record_cpa_event('cache_hit', {{'kind': 'OFFLINE_TEST_FROZEN_SELECTOR'}})
    (args.output_dir/'fixture-runtime.json').write_text(json.dumps({{
        'configured_base': os.environ.get('AUTOSLICE_BASE'),
        'report_root': str(args.runtime_report_root),
        'source_video': str(args.source_video),
        'source_srt': str(args.source_srt),
    }}))
    return {{'candidate_id': candidate.anchor.candidate_id,
            'candidate_dir': str(args.output_dir/candidate.anchor.candidate_id),
            'decision_action': 'BLOCK', 'reason_codes': ['UNKNOWN_FIXTURE_VERDICT'],
            'source_context_job': {{'song_candidate': True}}}}, None
selector._review_one_selected_candidate = frozen_review
raise SystemExit(selector.main(sys.argv[1:]))
'''


@pytest.mark.parametrize("custom_reports", [False, True])
def test_wrapper_and_real_selector_child_keep_private_reports(runtime, tmp_path, monkeypatch, custom_reports):
    bootstrap = tmp_path / "frozen-selector.py"
    bootstrap.write_text(BOOTSTRAP.format(repo=str(Path(runner.__file__).resolve().parents[1])))
    calls = []
    def dispatch(command, **kwargs):
        if command[0] == "ffmpeg":
            Path(command[-1]).write_bytes(b"OFFLINE_TEST media; real ffmpeg covered separately")
            return SimpleNamespace(returncode=0, stderr="")
        assert Path(command[1]).name == "run_full_session_selector_cpa_shadow.py"
        calls.append(command)
        # Start an actual Python child with the exact generated CLI arguments.
        # The bootstrap installs the frozen business response before real main().
        return REAL_RUN([command[0], str(bootstrap), *command[2:]], **kwargs)
    monkeypatch.setattr(runner.subprocess, "run", dispatch)
    report_root = runtime.private / "observations" if custom_reports else runtime.private
    extra = {"runtime_report_root": report_root} if custom_reports else {}
    source_before = runtime.transcript.read_bytes()
    result = runner.produce_song(
        DATE, runtime.item, prepare_only=True, output_root=runtime.private,
        source_srt=runtime.transcript, **extra,
    )
    assert len(calls) == 2
    assert result["full_source_authoritative_block"] is True
    assert "UNKNOWN_FIXTURE_VERDICT" in result["reason_codes"]
    assert "prepared_delivery" not in result and "delivered" not in result
    for command in calls:
        assert command[command.index("--runtime-report-root") + 1] == str(report_root)
        output = Path(command[command.index("--output-dir") + 1])
        actual = json.loads((output / "fixture-runtime.json").read_text())
        assert actual["configured_base"] == str(runtime.base)
        assert actual["report_root"] == str(report_root)
        assert Path(actual["source_video"]).is_relative_to(runtime.out)
        assert "EXPLICIT" in Path(actual["source_srt"]).read_text()
    rows = [json.loads(line) for line in
            (report_root / "reports" / "production-attempts" / f"{DATE}.jsonl").read_text().splitlines()]
    assert {row["stage"] for row in rows} == {"production", "song_selector"}
    assert sum(row["status"] == "RUNNING" for row in rows) == 3
    assert sum(row["status"] == "BLOCKED" for row in rows) == 3
    traces = list((report_root / "reports" / "production-attempt-traces").glob("*.jsonl"))
    assert len(traces) == 2
    assert all("OFFLINE_TEST_FROZEN_SELECTOR" in path.read_text() for path in traces)
    assert (report_root / "reports" / "latest.md").is_file()
    assert runtime.transcript.read_bytes() == source_before
    for forbidden in ("out", "logs", "reports", "state", ".prepared-deliveries"):
        assert not (runtime.base / forbidden).exists(), forbidden


def test_selector_default_report_root_stays_on_configured_runtime(runtime, tmp_path):
    bootstrap = tmp_path / "frozen-selector.py"
    bootstrap.write_text(BOOTSTRAP.format(repo=str(Path(runner.__file__).resolve().parents[1])))
    source = runtime.private / DATE / "source.mp4"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"OFFLINE_TEST source identity")
    output = runtime.private / DATE / "song-selector" / "default-output"
    command = [sys.executable, str(bootstrap), "--source-video", str(source),
               "--source-srt", str(runtime.transcript), "--output-dir", str(output),
               "--source-duration-ms", "900000", "--seed-song-candidate-id", CID,
               "--seed-song-anchor-start-ms", "200000", "--seed-song-anchor-end-ms", "350000",
               "--cpa-command", "OFFLINE_TEST_NOT_CALLED"]
    completed = REAL_RUN(command, env={"AUTOSLICE_BASE": str(runtime.base)},
                         capture_output=True, text=True, timeout=30)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    rows = [json.loads(line) for line in
            (runtime.base / "reports" / "production-attempts" / f"{DATE}.jsonl").read_text().splitlines()]
    assert [row["status"] for row in rows] == ["RUNNING", "BLOCKED"]
    assert not (runtime.private / "reports").exists()


def test_private_report_root_cannot_escape_before_any_observation(runtime):
    with pytest.raises(ValueError, match="reports must stay inside output_root"):
        runner.produce_song(DATE, runtime.item, prepare_only=True,
                            output_root=runtime.private, source_srt=runtime.transcript,
                            runtime_report_root=runtime.base)
    assert not runtime.private.exists()
    assert not (runtime.base / "out").exists()
    assert not (runtime.base / "reports").exists()
