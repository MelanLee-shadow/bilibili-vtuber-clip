"""30-boundary 有界重试：runner 与单候选入口共用同一函数。

规则：30-boundary「正常首轮 cap 为 30 秒；只有有界重试才可把同一 spec 的 cap
提升到 60 秒，不能另开无上限扩窗。」单候选入口只在 retry_scope=
same_topic_continues 时触发、只一次；端点由首轮 bound scope 推出，不由调用方
指定。fake producer、无 provider、无网络。
"""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import scripts.boundary_retry_entry as entry
import scripts.session_autoslice as runner
from src.autoslice import talk_lane
from src.autoslice.boundary_semantic_review import (
    SOURCE_WITNESS_RESERVE_MS,
    required_source_context_end_ms,
)
from src.autoslice.producer_boundary_owner_contract import (
    freeze_required_boundary_owner_contract,
)

CID = "semantictalk_shared_retry"
PIECE_START = 1_428_900
SEMANTIC_END = 1_549_030


def exhausted(scope: str | None) -> str:
    tail = f" retry_scope={scope}" if scope else ""
    return (
        'BOUNDARY_CONTEXT_EXHAUSTED: ["BOUNDARY_CONTEXT_EXHAUSTED", '
        '"SAME_TOPIC_CONTINUES_AFTER_TARGET"] max_forward_ms=30000'
        f"{tail}\n"
    )


def build_case(root: Path, *, piece_end: int, cap: int = 30_000) -> dict:
    """Write the first attempt's spec, review scope and frozen owner set."""

    out_root = root / "out"
    candidate_root = out_root / CID
    candidate_root.mkdir(parents=True, exist_ok=True)
    spec = {
        "candidate_id": CID,
        "date": "2026-10-12",
        "output_root": str(out_root),
        "semantic_start_ms": 1_438_650,
        "semantic_end_ms": SEMANTIC_END,
        "boundary_repair_extend_cap_ms": cap,
        "pieces": [{"remote_media": "/recordings/source.flv",
                    "start_ms": PIECE_START, "end_ms": piece_end}],
    }
    audit: dict[str, object] = {}
    _target, scope = freeze_required_boundary_owner_contract(
        spec=json.loads(json.dumps(spec)),
        durations=[piece_end - PIECE_START],
        chat_authority_audit=audit,
        required_boundary_owners=[],
    )
    (candidate_root / f"{CID}.chat-authority.json").write_text(
        json.dumps(audit, ensure_ascii=False), encoding="utf-8")
    (candidate_root / f"{CID}.review-flags.json").write_text(json.dumps({
        "boundary_semantic_review": {
            "schema_version": "talk-boundary-semantic-review.v1",
            "status": "BLOCK", "needs_more_context": True,
            "retry_scope": "same_topic_continues",
            "boundary_search_scope": scope,
        }}), encoding="utf-8")
    spec_path = out_root / f"spec_{CID}.json"
    spec_path.write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
    required = required_source_context_end_ms(scope, repair_cap_ms=60_000)
    return {"spec": spec, "spec_path": spec_path, "out_root": out_root,
            "required_end_ms": required}


def run_runner_path(case, monkeypatch, tmp_path, *, seg_dur_ms, outputs):
    monkeypatch.setattr(runner, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(runner, "_speaker_review_manifest_state", lambda _d: None)
    monkeypatch.setattr(runner, "child_env_for_date", lambda _date: {})
    monkeypatch.setattr(runner, "talk_pipeline_fingerprint", lambda _cid: "sha256:" + "a" * 64)
    calls, spec_bytes = [], []

    def fake_run(command, **kwargs):
        calls.append(command)
        spec_bytes.append(case["spec_path"].read_bytes())
        kwargs["stdout"].write(outputs[len(calls) - 1])
        kwargs["stdout"].flush()
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(talk_lane.subprocess, "run", fake_run)
    result = talk_lane._run_talk_producer_with_boundary_context_retry(
        date="2026-10-12",
        item={"seg_dur_ms": seg_dur_ms, "end_ms": SEMANTIC_END},
        candidate_id=CID, spec=json.loads(case["spec_path"].read_text(encoding="utf-8")),
        out_root=case["out_root"], spec_path=case["spec_path"],
        log_path=tmp_path / "logs" / f"{CID}.log",
        cmd=["python3", "produce_slice_package.py", "--spec", str(case["spec_path"])],
    )
    return result, calls, spec_bytes


def run_entry_path(case, *, seg_dur_ms, outputs, first_attempt_log=None):
    calls = []

    def fake_command(command, sink):
        calls.append(list(command))
        text = outputs[len(calls) - 1]
        sink.write(text)
        return SimpleNamespace(returncode=1), text

    sink = io.StringIO()
    rc = entry.run_boundary_retry_entry(
        spec_path=case["spec_path"],
        producer_args=["--ssh-host", "localhost", "--prepare-only"],
        first_attempt_log=first_attempt_log,
        first_attempt_exit_code=1 if first_attempt_log else None,
        python="python3", python_flags=[], run_command=fake_command,
        probe_ms=lambda _path: seg_dur_ms, sink=sink,
    )
    lines = [line for line in sink.getvalue().splitlines()
             if line.startswith(entry.RESULT_PREFIX)]
    assert "{" not in lines[-1] and "}" not in lines[-1]
    summary = {}
    for pair in lines[-1][len(entry.RESULT_PREFIX):].split(" "):
        key, value = pair.split("=", 1)
        summary[key] = int(value) if value.lstrip("-").isdigit() else (None if value == "none" else value)
    return rc, calls, summary


def test_entry_constants_are_the_runner_constants():
    assert entry.BOUNDARY_REPAIR_RETRY_CAP_MS == runner.BOUNDARY_REPAIR_RETRY_CAP_MS == 60_000
    assert talk_lane.SINGLE_CANDIDATE_BOUNDARY_RETRY_SCOPES == {"same_topic_continues"}
    # Plan A (admin): the headroom is a fixed 3s owned by the shared
    # function; no caller passes a window size it could be derived from.
    assert talk_lane.BOUNDARY_RETRY_HEADROOM_MS == 3_000
    import inspect
    assert {"piece_post_ms", "initial_cap_ms"}.isdisjoint(
        inspect.signature(talk_lane.boundary_context_retry).parameters)


@pytest.mark.parametrize("piece_end_delta,seg_dur_ms", [
    (0, 1_804_165),          # 源上下文已够但无首轮文字检查点：会重新推导文字，加 headroom
    (-20_000, 1_804_165),    # 首轮不够：放宽到 bound scope 要求终点＋headroom
    (-20_000, None),         # 文件尾小于终点＋headroom：夹到录像段时长
])
def test_runner_and_entry_rewrite_the_same_spec_bytes(
    tmp_path, monkeypatch, piece_end_delta, seg_dur_ms,
):
    probe = build_case(tmp_path / "probe", piece_end=1_600_000)
    piece_end = probe["required_end_ms"] + piece_end_delta
    if seg_dur_ms is None:
        seg_dur_ms = probe["required_end_ms"] + 1_000
    outputs = [exhausted("same_topic_continues")] * 3

    runner_case = build_case(tmp_path, piece_end=piece_end)
    runner_result, runner_calls, runner_specs = run_runner_path(
        runner_case, monkeypatch, tmp_path, seg_dur_ms=seg_dur_ms, outputs=outputs)
    runner_rewritten = runner_specs[1]

    entry_case = build_case(tmp_path, piece_end=piece_end)  # same paths, fresh first attempt
    assert entry_case["spec_path"].read_bytes() == runner_specs[0]
    rc, entry_calls, summary = run_entry_path(entry_case, seg_dur_ms=seg_dur_ms, outputs=outputs)
    retry_path = entry.retry_spec_path(entry_case["spec_path"])

    assert retry_path.read_bytes() == runner_rewritten
    # Second BLOCK never yields a third attempt, on either path.
    assert len(runner_calls) == len(entry_calls) == 2
    assert runner_result[3] == summary["boundary_context_retries"] == 1
    assert rc == 1 and summary["status"] == "RETRIED"
    assert entry_calls[0][entry_calls[0].index("--spec") + 1] == str(entry_case["spec_path"])
    assert entry_calls[1][entry_calls[1].index("--spec") + 1] == str(retry_path)
    assert entry_calls[0][4:] == entry_calls[1][4:] == ["--ssh-host", "localhost", "--prepare-only"]
    rewritten = json.loads(runner_rewritten)
    headroom = talk_lane.BOUNDARY_RETRY_HEADROOM_MS
    expected_end = min(max(piece_end, entry_case["required_end_ms"]) + headroom, seg_dur_ms)
    assert rewritten["pieces"][-1]["end_ms"] == summary["retry_piece_end_ms"] == expected_end
    assert rewritten["boundary_repair_extend_cap_ms"] == 60_000 <= runner.BOUNDARY_REPAIR_RETRY_CAP_MS
    assert rewritten["boundary_retry_frozen_owner_contract"]["contract_sha256"] == (
        summary["boundary_retry_owner_contract_sha256"])
    # The caller's original spec is left untouched by the entry.
    assert entry_case["spec_path"].read_bytes() == runner_specs[0]


def test_entry_reuses_a_finished_first_attempt_log(tmp_path, monkeypatch):
    outputs = [exhausted("same_topic_continues")] * 3
    probe = build_case(tmp_path / "probe", piece_end=1_600_000)
    piece_end = probe["required_end_ms"]
    runner_case = build_case(tmp_path, piece_end=piece_end)
    _result, _calls, runner_specs = run_runner_path(
        runner_case, monkeypatch, tmp_path, seg_dur_ms=1_804_165, outputs=outputs)

    case = build_case(tmp_path, piece_end=piece_end)
    log = tmp_path / "producer.log"
    log.write_text("PRODUCER_STAGE 2026-10-21T08:57:29.604Z START boundary n=1\n"
                   + outputs[0], encoding="utf-8")
    rc, calls, summary = run_entry_path(case, seg_dur_ms=1_804_165, outputs=outputs[1:],
                                        first_attempt_log=log)
    assert len(calls) == 1 and rc == 1
    assert summary["status"] == "RETRIED" and summary["first_attempt_retry_scope"] == "same_topic_continues"
    assert entry.retry_spec_path(case["spec_path"]).read_bytes() == runner_specs[1]


@pytest.mark.parametrize("scope", ["source_witness_reserve", None])
def test_entry_does_not_retry_other_blocks(tmp_path, scope):
    probe = build_case(tmp_path / "probe", piece_end=1_600_000)
    case = build_case(tmp_path, piece_end=probe["required_end_ms"] - 20_000)
    original = case["spec_path"].read_bytes()
    rc, calls, summary = run_entry_path(case, seg_dur_ms=1_804_165, outputs=[exhausted(scope)] * 2)
    assert len(calls) == 1 and rc == 1
    assert summary["status"] == "NOT_RETRIED" and summary["boundary_context_retries"] == 0
    assert not entry.retry_spec_path(case["spec_path"]).exists()
    assert case["spec_path"].read_bytes() == original


def test_entry_never_raises_cap_past_sixty_seconds(tmp_path):
    probe = build_case(tmp_path / "probe", piece_end=1_600_000)
    case = build_case(tmp_path, piece_end=probe["required_end_ms"], cap=60_000)
    rc, calls, summary = run_entry_path(
        case, seg_dur_ms=1_804_165, outputs=[exhausted("same_topic_continues")] * 2)
    assert len(calls) == 1 and summary["status"] == "NOT_RETRIED"
    assert not entry.retry_spec_path(case["spec_path"]).exists()


def test_entry_without_a_probed_recording_does_not_widen(tmp_path):
    probe = build_case(tmp_path / "probe", piece_end=1_600_000)
    case = build_case(tmp_path, piece_end=probe["required_end_ms"] - 20_000)
    rc, calls, summary = run_entry_path(
        case, seg_dur_ms=0, outputs=[exhausted("same_topic_continues")] * 2)
    assert len(calls) == 1 and rc == 1
    assert summary["status"] == "SEGMENT_DURATION_UNKNOWN"
    assert not entry.retry_spec_path(case["spec_path"]).exists()


def test_entry_cli_owns_the_spec_argument(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(entry, "run_boundary_retry_entry", lambda **kw: seen.update(kw) or 0)
    spec = tmp_path / "spec.json"
    assert entry.main(["--spec", str(spec), "--", "--ssh-host", "localhost", "--reuse-cover"]) == 0
    assert seen["spec_path"] == spec and seen["producer_args"] == ["--ssh-host", "localhost", "--reuse-cover"]
    assert seen["first_attempt_log"] is None and seen["first_attempt_exit_code"] is None
    with pytest.raises(SystemExit):
        entry.main(["--spec", str(spec), "--", "--spec", str(spec)])
    with pytest.raises(SystemExit):
        entry.main(["--spec", str(spec), "--first-attempt-log", str(spec), "--"])


def test_cross_segment_reserve_stays_a_runner_only_input(tmp_path, monkeypatch):
    """Runner: a proven next segment supplies the reserve; the entry has none."""

    probe = build_case(tmp_path / "probe", piece_end=1_600_000)
    seg_dur_ms = probe["required_end_ms"] - 5_000
    reserve = {"piece": {"remote_media": "/recordings/next.flv", "start_ms": 0, "end_ms": 7_000,
                         "piece_role": "boundary_witness_reserve"},
               "chat_binding_absent": True, "next_segment_name": "next.flv",
               "gap_ms": 0, "continuity_delta_ms": 0}
    seen = []
    monkeypatch.setattr(talk_lane, "_discover_cross_segment_witness_reserve",
                        lambda **kw: seen.append(kw["deficit_ms"]) or reserve)
    runner_case = build_case(tmp_path, piece_end=probe["required_end_ms"] - 20_000)
    result, calls, specs = run_runner_path(
        runner_case, monkeypatch, tmp_path, seg_dur_ms=seg_dur_ms,
        outputs=[exhausted("same_topic_continues")] * 2)
    rewritten = json.loads(specs[1])
    assert len(calls) == 2 and result[3] == 1
    assert seen == [runner_case["required_end_ms"] - seg_dur_ms]
    assert rewritten["pieces"][0]["end_ms"] == seg_dur_ms  # widened to its own file end
    assert rewritten["pieces"][1] == reserve["piece"]
    assert rewritten["boundary_repair_extend_cap_ms"] == 60_000

    entry_case = build_case(tmp_path, piece_end=probe["required_end_ms"] - 20_000)
    rc, entry_calls, summary = run_entry_path(
        entry_case, seg_dur_ms=seg_dur_ms, outputs=[exhausted("same_topic_continues")] * 2)
    assert len(entry_calls) == 1 and summary["status"] == "NOT_RETRIED"


def test_entry_tees_the_real_child_output():
    sink = io.StringIO()
    completed, output = entry._tee_command(
        [sys.executable, "-c", "import sys; print('甲'); sys.exit(3)"], sink)
    assert completed.returncode == 3 and output == sink.getvalue() == "甲\n"


def test_producer_record_stays_the_last_json_object_in_the_log(tmp_path):
    case = build_case(tmp_path, piece_end=1_600_000)
    record = {"candidate_id": CID, "status": "ok", "nested": {"rc": 0}}

    def fake_command(command, sink):
        text = json.dumps(record) + "\n"
        sink.write(text)
        return SimpleNamespace(returncode=0), text

    sink = io.StringIO()
    rc = entry.run_boundary_retry_entry(
        spec_path=case["spec_path"], producer_args=[], python="python3", python_flags=[],
        run_command=fake_command, probe_ms=lambda _path: 1_804_165, sink=sink)
    assert rc == 0
    assert sink.getvalue().splitlines()[-1].startswith(entry.RESULT_PREFIX + "schema_version=")
    assert talk_lane.last_json_block(sink.getvalue()) == record



def _first_attempt_checkpoint(case):
    root = case["out_root"] / CID
    (root / f"{CID}.source-text-checkpoint.json").write_text("{}", encoding="utf-8")


def test_xiamen_shape_keeps_the_window_and_resumes_the_first_text(tmp_path, monkeypatch):
    """021d68b7: window already covers the bound scope and the first attempt
    left its text checkpoint; widening by headroom only forced a padded the
    checkpoint can never resume (CURRENT_AUTO_RESUME_CHECKPOINT_PADDED_DRIFT)."""

    probe = build_case(tmp_path / "probe", piece_end=1_600_000)
    piece_end = probe["required_end_ms"]
    outputs = [exhausted("same_topic_continues")] * 3
    runner_case = build_case(tmp_path, piece_end=piece_end)
    _first_attempt_checkpoint(runner_case)
    _result, _calls, runner_specs = run_runner_path(
        runner_case, monkeypatch, tmp_path, seg_dur_ms=1_804_165, outputs=outputs)

    entry_case = build_case(tmp_path, piece_end=piece_end)
    rc, entry_calls, summary = run_entry_path(entry_case, seg_dur_ms=1_804_165, outputs=outputs)
    rewritten = json.loads(entry.retry_spec_path(entry_case["spec_path"]).read_bytes())

    assert entry.retry_spec_path(entry_case["spec_path"]).read_bytes() == runner_specs[1]
    assert rewritten["pieces"] == json.loads(runner_specs[0])["pieces"]  # same window, same padded
    assert rewritten["boundary_repair_extend_cap_ms"] == 60_000
    assert rewritten["boundary_retry_frozen_owner_contract"]["contract_sha256"]
    assert summary["status"] == "RETRIED" and len(entry_calls) == 2


def test_a_window_that_must_widen_still_gets_headroom_with_a_checkpoint(tmp_path):
    probe = build_case(tmp_path / "probe", piece_end=1_600_000)
    case = build_case(tmp_path, piece_end=probe["required_end_ms"] - 20_000)
    _first_attempt_checkpoint(case)
    run_entry_path(case, seg_dur_ms=1_804_165, outputs=[exhausted("same_topic_continues")] * 2)
    rewritten = json.loads(entry.retry_spec_path(case["spec_path"]).read_bytes())
    headroom = talk_lane.BOUNDARY_RETRY_HEADROOM_MS
    assert rewritten["pieces"][-1]["end_ms"] == case["required_end_ms"] + headroom


@pytest.mark.parametrize("checkpoint", [True, False])
def test_runner_first_window_already_holds_the_bounded_retry(tmp_path, monkeypatch, checkpoint):
    """Plan A (admin): the first pass reserves the one 60 s retry.

    Common case (search origin == semantic end): first window = semantic end
    + PIECE_POST_MS (78 s) = retry cap 60 s + witness reserve 15 s + fixed 3 s
    headroom, so the retry never widens the window. With the first text
    checkpoint present it resumes that checkpoint on the same padded.
    """
    first_end = SEMANTIC_END + runner.PIECE_POST_MS
    case = build_case(tmp_path, piece_end=first_end)
    required = case["required_end_ms"]
    assert required == SEMANTIC_END + runner.BOUNDARY_REPAIR_RETRY_CAP_MS + SOURCE_WITNESS_RESERVE_MS
    assert first_end - required == talk_lane.BOUNDARY_RETRY_HEADROOM_MS == 3_000
    if checkpoint:
        _first_attempt_checkpoint(case)
    result, calls, specs = run_runner_path(
        case, monkeypatch, tmp_path, seg_dur_ms=1_804_165,
        outputs=[exhausted("same_topic_continues")] * 2)
    rewritten = json.loads(specs[1])
    assert len(calls) == 2 and result[3] == 1
    assert rewritten["pieces"] == json.loads(specs[0])["pieces"]  # never widened
    assert rewritten["boundary_repair_extend_cap_ms"] == 60_000
    log = (tmp_path / "logs" / f"{CID}.log").read_text(encoding="utf-8")
    assert ("first_attempt_text_checkpoint=resumed" in log) is checkpoint
