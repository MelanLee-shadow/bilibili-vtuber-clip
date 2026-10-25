"""Whole-path offline observation (plan A stage one): one stop/collect switch, no provider."""
from __future__ import annotations

import hashlib
import json
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from src.autoslice import continuation_subchecks as subchecks
from src.autoslice import whole_path_observation as wpo


class _Check(Exception):
    reason_code = "CURRENT_FIXTURE_DRIFT"


def _validator(fail: bool) -> str:
    checks = subchecks.Group("fixture_validator", depth=1)
    with checks.check("first"):
        if fail:
            raise _Check("CURRENT_FIXTURE_DRIFT")
    with checks.check("second"):
        pass
    checks.finish()
    return "continued"


def test_production_and_plain_audit_still_stop_at_the_first_failure():
    with pytest.raises(_Check):
        _validator(True)
    with subchecks.audit() as rows:
        with pytest.raises(_Check):
            _validator(True)
    assert [row["status"] for row in rows] == ["FAIL", "PASS"]
    assert not subchecks.collecting()


def test_collect_mode_is_the_only_switch_and_continues_past_a_failed_group():
    with wpo.observe() as rows:
        assert subchecks.collecting()
        assert _validator(True) == "continued"
    assert [(row["check"], row["status"]) for row in rows] == [
        ("first", "FAIL"), ("second", "PASS"), ("<finish>", "COLLECTED")]
    assert rows[-1]["reason_code"] == "CURRENT_FIXTURE_DRIFT"
    assert not subchecks.collecting()  # the switch is scoped to the observation
    with pytest.raises(_Check):
        _validator(True)


def test_collect_mode_on_the_real_continuation_validator(tmp_path):
    from src.autoslice import producer_text_pipeline as pipeline
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from test_continuation_authority_class import _cold_read, _interrupted_writer_state

    chat, text, context = _interrupted_writer_state(tmp_path)
    chat["mutation_authorized"] = True
    with pytest.raises(pipeline.CurrentTextContinuationError):
        _cold_read(chat, text, context)
    with wpo.observe() as rows:
        _cold_read(chat, text, context)  # recorded, not raised
    status = {row["check"]: row["status"] for row in rows}
    assert status["chat_authority.mutation_flag"] == "FAIL"
    assert status["<finish>"] == "COLLECTED"
    blocks = wpo.blocks_of(rows, {"outcome": "BLOCKED"}, None, Path.cwd())
    assert blocks[0]["check"] == "CURRENT_CHAT_AUTHORITY_MUTATION_INVALID"
    assert blocks[0]["production_stop"] is True and blocks[0]["pre_judgment"] == "PRE_JUDGMENT"


def test_sentinel_is_not_swallowed_by_production_retry_wrappers():
    from src.autoslice import infra_retry_policy

    assert issubclass(wpo.ObservationBoundary, BaseException)
    assert not issubclass(wpo.ObservationBoundary, Exception)

    def reached():
        raise wpo.ObservationBoundary("PROVIDER_TEXT_UNRECORDED")

    with pytest.raises(wpo.ObservationBoundary):
        try:
            reached()
        except Exception:  # noqa: BLE001 - a production fallback must not see it
            pytest.fail("sentinel swallowed")
    with pytest.raises(wpo.ObservationBoundary):
        infra_retry_policy.cpa_text_retry_call(reached, record=lambda row: None,
                                               sleeper=lambda s: None)
    with pytest.raises(wpo.ObservationBoundary):
        infra_retry_policy.terminal_retry_call(reached)


@pytest.mark.parametrize(("argv", "expected"), [
    (["ssh", "localhost", "true"], "ssh"),
    (["nice", "-n", "10", "ssh", "host"], "ssh"),
    (["bash", "/x/scripts/llm_via_cpa.sh", "p", "c"], "llm_via_cpa.sh"),
    (["python3", "/x/scripts/llm_via_runtime_cpa_ssh.py", "p"], "llm_via_runtime_cpa_ssh.py"),
    ("agy --prompt x", "agy"),
    (["ffmpeg", "-i", "/x/a.mp4", "/x/b.mp4"], None),
    (["cp", "/x/a", "/x/b"], None),
])
def test_provider_process_names_only_provider_or_remote_commands(argv, expected):
    assert wpo.provider_process(argv) == expected


def _exchange(root: Path, name: str, prompt: str, completion: str, *, started: str,
              status: str = "COMPLETED", model: str | None = "gpt-x") -> None:
    folder = root / f"request-{name}"
    folder.mkdir(parents=True)
    (folder / "prompt.txt").write_bytes(prompt.encode())
    (folder / "completion.txt").write_bytes(completion.encode())
    (folder / "request.json").write_text(json.dumps({
        "status": "STARTED", "started_utc": started,
        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest()}))
    (folder / "terminal.json").write_text(json.dumps({
        "status": status, "answering_model": model,
        "completion_sha256": hashlib.sha256(completion.encode()).hexdigest()}))


def _window():
    return (wpo._utc("2026-10-24T17:36:00Z"), wpo._utc("2026-10-24T18:00:00Z"))


def test_exchange_archive_admits_only_completed_same_run_unambiguous_prompts(tmp_path):
    root = tmp_path / "cpa-text-exchanges"
    _exchange(root, "a", "same-run", "answer-a", started="2026-10-24T17:40:00+00:00")
    _exchange(root, "b", "older-run", "answer-b", started="2026-10-22T10:00:00+00:00")
    _exchange(root, "c", "unknown", "answer-c", started="2026-10-24T17:41:00+00:00", status="OUTCOME_UNKNOWN")
    _exchange(root, "d", "twice", "one", started="2026-10-24T17:42:00+00:00")
    _exchange(root, "e", "twice", "two", started="2026-10-24T17:43:00+00:00")
    archive = wpo.ExchangeArchive([root], _window())
    assert archive.get("same-run")["completion"] == "answer-a"
    assert archive.get("older-run") is None and archive.get("unknown") is None
    assert archive.get("twice") is None and len(archive.ambiguous) == 1
    assert wpo.ExchangeArchive([root], None).get("same-run") is None


def test_unrecorded_prompt_names_its_nearest_recorded_prompt_without_text(tmp_path):
    root = tmp_path / "cpa-text-exchanges"
    _exchange(root, "a", "header\n字幕一\n{\"pass\": 4}\nfooter", "x", started="2026-10-24T17:40:00+00:00")
    _exchange(root, "b", "other\nprompt", "y", started="2026-10-24T17:41:00+00:00")
    near = wpo.ExchangeArchive([root], _window()).nearest("header\n字幕一\n{\"pass\": 5}\nfooter")
    assert near["folder"] == "request-a" and near["changed_lines"] == 2
    assert near["changed_lines_masked"] == ['-{"pass": 4}', '+{"pass": 5}']
    assert near["changed_line_pairs"] == [{"first_difference_at": 9, "old_length": 11, "new_length": 11,
                                           "old_masked": '{"pass": 4}', "new_masked": '{"pass": 5}'}]
    assert wpo._mask('{"t": "字幕\\u5b57"}') == '{"t": "···"}'


@pytest.fixture
def installed(tmp_path, monkeypatch):
    from src.autoslice import llm_client, producer_stage_log

    for owner, name in ((socket.socket, "connect"), (socket.socket, "connect_ex"),
                        (socket, "create_connection"), (socket, "getaddrinfo"),
                        (subprocess.Popen, "__init__"), (llm_client, "_call_command"),
                        (llm_client, "_call_direct"), (producer_stage_log, "emit")):
        monkeypatch.setattr(owner, name, getattr(owner, name))
    root = tmp_path / "cpa-text-exchanges"
    _exchange(root, "a", "recorded prompt", "recorded answer", started="2026-10-24T17:40:00+00:00")
    observation = wpo.Observation(Path(__file__).resolve().parents[1], wpo.ExchangeArchive([root], _window()))
    observation.install()
    return observation


def test_recorded_prompt_is_replayed_and_any_other_provider_step_is_unobserved(installed):
    from src.autoslice import llm_client
    from src.autoslice.producer_stage_log import producer_stage

    config = llm_client.LlmConfig(transport="command", command_template="bash llm_via_cpa.sh {prompt_file}")
    with producer_stage("finalization"):
        answer = llm_client._call_command("recorded prompt", config)
        assert answer == "recorded answer" and answer.answering_model == "gpt-x"
        with pytest.raises(wpo.ObservationBoundary) as missing:
            llm_client._call_command("new prompt", config)
    assert missing.value.reason == "PROVIDER_TEXT_UNRECORDED"
    assert missing.value.detail["nearest_recorded"]["folder"] == "request-a"
    assert installed.unobserved[0]["stage"] == "finalization"
    assert installed.replayed[0]["stage"] == "finalization"
    with pytest.raises(wpo.ObservationBoundary):
        socket.create_connection(("192.0.2.1", 443))
    with pytest.raises(wpo.ObservationBoundary):
        subprocess.run(["ssh", "localhost", "true"])
    assert subprocess.run(["true"]).returncode == 0  # local tools still run
    assert installed.stage_path() == ""


def test_placeholder_credentials_reach_construction_without_reading_cpa_env(tmp_path, monkeypatch):
    from src.autoslice import llm_client, producer_final_review_transport as transport

    original = llm_client.runtime_cpa_command_environment
    for module in list(sys.modules.values()):  # restore every rebound reference afterwards
        for attr, value in list(vars(module).items()) if hasattr(module, "__dict__") else ():
            if value is original:
                monkeypatch.setattr(module, attr, value)
    observation = wpo.Observation(tmp_path, wpo.ExchangeArchive([], None))
    observation.placeholder_credentials()
    env = transport.runtime_cpa_command_environment(tmp_path / "missing-runtime")
    assert env["CPA_API_KEY"] == wpo.PLACEHOLDER_CPA_ENVIRONMENT["CPA_API_KEY"]
    assert llm_client.runtime_cpa_command_environment is transport.runtime_cpa_command_environment


def test_blocks_order_taint_and_classification(tmp_path):
    rows = [{"validator": "v", "call_site": "f:1", "check": "a", "status": "FAIL",
             "reason_code": "CURRENT_FINAL_SRT_DRIFT"},
            {"validator": "v", "call_site": "f:1", "check": "<finish>", "status": "COLLECTED",
             "reason_code": "CURRENT_FINAL_SRT_DRIFT"}]
    try:
        raise SystemExit("BOUNDARY_CURRENT_DELIVERY_CLOSURE_TIMING_DRIFT: current_last_end=74320 source_end=63280")
    except SystemExit as exc:
        stop = exc
    blocks = wpo.blocks_of(rows, {"outcome": "BLOCKED", "stage_at_stop": "boundary"}, stop,
                           Path(__file__).resolve().parents[1])
    assert [block["check"] for block in blocks] == [
        "CURRENT_FINAL_SRT_DRIFT", "BOUNDARY_CURRENT_DELIVERY_CLOSURE_TIMING_DRIFT"]
    assert [block["production_stop"] for block in blocks] == [True, False]
    assert blocks[1]["tainted"] is True and blocks[1]["stage"] == "boundary"
    assert blocks[1]["site"].startswith("tests/test_whole_path_observation.py:")
    assert wpo.classify("TALK_EFFECTIVE_DURATION_NOT_OVER_60S_AFTER_BOUNDARY") == "JUDGMENT_OR_POLICY"
    assert wpo.classify("FINAL_REVIEW_UNRESOLVED_FINDINGS") == "JUDGMENT_OR_POLICY"
    assert wpo.classify("FINAL_REVIEW_ENGINEERING_FAILURE") == "PRE_JUDGMENT"
    markdown = wpo.render_markdown([{"candidate_id": "c", "outcome": "BLOCKED", "blocks": blocks}])
    assert "BOUNDARY_CURRENT_DELIVERY_CLOSURE_TIMING_DRIFT" in markdown


def test_release_block_evidence_reads_only_closed_finding_codes(tmp_path):
    path = tmp_path / "cid.chat-authority.json"
    finding = {"cue_index": 6, "suggestion": "字幕原文不进报告",
               "lifecycle": {"state": "CONTRACT_ERROR", "release_action": "RECOVER_ENGINEERING"},
               "terminal_funnel_error": {"reason_code": "TARGET_MATERIAL_CHANNEL_ATTEMPT_UNRESOLVED:WIDE"}}
    path.write_text(json.dumps({"final_review_audit": {
        "status": "FLAGGED", "release_gate": "BLOCK", "reason_codes": ["FINAL_REVIEW_ENGINEERING_FAILURE"],
        "findings": [finding, finding]}}, ensure_ascii=False))
    summary = wpo._review_summary(f"FINAL_REVIEW_RELEASE_BLOCKED: FINAL_REVIEW_ENGINEERING_FAILURE: {path}")
    assert summary["reason_codes"] == ["FINAL_REVIEW_ENGINEERING_FAILURE"] and summary["findings"] == 2
    assert summary["finding_lifecycle_state|release_action|funnel_reason"] == {
        "CONTRACT_ERROR|RECOVER_ENGINEERING|TARGET_MATERIAL_CHANNEL_ATTEMPT_UNRESOLVED:WIDE": 2}
    assert "字幕原文" not in json.dumps(summary, ensure_ascii=False)


def test_run_one_refuses_outside_the_sandbox(tmp_path, monkeypatch):
    monkeypatch.delenv("CONTINUATION_PREFLIGHT_SANDBOX", raising=False)
    args = wpo.argparse.Namespace(spec=tmp_path / "spec.json", code_root=Path(__file__).resolve().parents[1],
                                  substrate="existing_srt", report=tmp_path / "r.json",
                                  time_budget_s=0, exchange_window="")
    report = wpo.run_one(args)
    assert report["outcome"] == "REFUSED_NOT_ISOLATED"
    assert "SANDBOX_LAUNCHER_REQUIRED" in report["isolation_problems"]


def test_exec_entry_runs_one_fresh_isolated_process_per_slice(tmp_path, monkeypatch):
    slices = tmp_path / "slices.txt"
    slices.write_text("/in/a/SPEC.json existing_srt\n\n/in/b/SPEC.json aggregate_asr\n")
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs["env"]))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(wpo.subprocess, "run", fake_run)
    code = tmp_path / "code"
    assert wpo.main([str(code), "--slices", str(slices), "--report-dir", str(tmp_path / "reports"),
                     "--exchange-window", "2026-10-24T17:36:00Z..2026-10-24T18:00:00Z"]) == 0
    assert [command[command.index("--spec") + 1] for command, _ in calls] == ["/in/a/SPEC.json", "/in/b/SPEC.json"]
    command, env = calls[0]
    assert command[:6] == ["ionice", "-c", "3", "nice", "-n", "10"]
    assert command[command.index("-B") + 1] == str(code.resolve() / "src/autoslice/whole_path_observation.py")
    assert "--run-one" in command and "--exchange-window" in command
    assert env["GEMINI_PAID_BACKUP_DEV_EXCEPTION"] == "0"


def test_gauntlet_whole_path_phase_uses_the_continuation_sandbox(tmp_path, monkeypatch):
    from scripts import predeploy_contract_gauntlet as gauntlet

    slices = tmp_path / "slices.txt"
    slices.write_text("/in/cid/SPEC.json existing_srt\n")
    seen = {}

    def fake_run(command, **kwargs):
        seen["command"] = command
        work = Path(command[command.index("--report-dir") + 1])
        work.mkdir(parents=True, exist_ok=True)
        (work / "cid.json").write_text(json.dumps({
            "candidate_id": "cid", "outcome": "BLOCKED", "stage_at_stop": "boundary",
            "blocks": [{"check": "BOUNDARY_CURRENT_DELIVERY_CLOSURE_TIMING_DRIFT", "stage": "boundary",
                        "production_stop": True}]}))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(gauntlet.subprocess, "run", fake_run)
    state = tmp_path / "state"
    monkeypatch.setattr(sys, "argv", ["gauntlet", "--phase", "whole-path", "--state", str(state),
                                      "--repo", str(Path(__file__).resolve().parents[1]),
                                      "--slices", str(slices), "--snapshot", "/snap",
                                      "--exchange-window", "a..b", "--time-budget-s", "60"])
    assert gauntlet.main() == 0
    command = seen["command"]
    assert command[:7] == ["sudo", "-n", "unshare", "-m", "-n", "--propagation", "private"]
    assert command[8].endswith("scripts/continuation_authority_preflight_sandbox.sh")
    assert command[12].endswith("src/autoslice/whole_path_observation.py") and command[13] == "--exec"
    assert command[command.index("--exchange-window") + 1] == "a..b"
    summary = json.loads((state / "whole-path.json").read_text())
    assert summary[0]["blocks"][0]["check"] == "BOUNDARY_CURRENT_DELIVERY_CLOSURE_TIMING_DRIFT"
    assert "BOUNDARY_CURRENT_DELIVERY_CLOSURE_TIMING_DRIFT" in (state / "WHOLE-PATH.md").read_text()
    monkeypatch.setattr(sys, "argv", ["gauntlet", "--phase", "whole-path", "--state", str(tmp_path / "s2")])
    with pytest.raises(SystemExit):
        gauntlet.main()
