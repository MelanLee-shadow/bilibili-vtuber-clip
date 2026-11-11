"""Ordinary media-host execution and CPA response transport, with no network calls."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.autoslice.manual_lane import adapters, cli
from src.autoslice.manual_lane.adapters import RemoteResult, SshCpaJudge
from src.autoslice.manual_lane.state import ClipState


def test_cli_run_selects_local_only_when_explicit(tmp_path, monkeypatch):
    task = tmp_path / "task"
    ClipState.create(task / "state/c.json", clip={"cid": "c", "remote_dir": str(tmp_path / "media")}, session={})
    selected = []

    def run(ctx, *args, **kwargs):
        selected.append(ctx.adapters.remote)
        return SimpleNamespace(passed=[], reused=[], stopped_at=None, reason=None, halted_before_upload=True)

    monkeypatch.setattr(cli, "run_clip", run)
    args = ["run", "--task-dir", str(task), "--cid", "c", "--until", "0"]
    assert cli.main(args) == 0
    assert isinstance(selected[-1], adapters.SshRemote)
    assert cli.main([*args, "--on-media-host"]) == 0
    assert isinstance(selected[-1], adapters.LocalRemote)
    assert selected[-1] is not selected[0]


def test_local_scripts_use_bash_preserve_result_and_timeout(tmp_path):
    remote = adapters.LocalRemote(scripts_dir=str(tmp_path / "scripts"))
    result = remote.run_script(name="failure", body="printf 'ok'; printf 'bad' >&2; exit 7\n", timeout=5)
    assert (result.rc, result.stdout, result.stderr) == (7, "ok", "bad")
    assert (tmp_path / "scripts/failure.sh").read_text().startswith("#!/bin/bash\nset -uo pipefail\n")
    with pytest.raises(subprocess.TimeoutExpired):
        remote.run_script(name="timeout", body="sleep 3\n", timeout=1)


def test_local_copy_is_byte_preserving_for_same_and_different_paths(tmp_path):
    remote = adapters.LocalRemote(scripts_dir=str(tmp_path / "scripts"))
    source = tmp_path / "source.bin"
    data = bytes(range(256)) * 9
    source.write_bytes(data)
    remote.put(source, str(source))
    remote.get(str(source), source)
    hardlink = tmp_path / "hardlink.bin"
    hardlink.hardlink_to(source)
    remote.put(source, str(hardlink))
    remote.get(str(hardlink), source)
    copy = tmp_path / "target" / "copy.bin"
    remote.put(source, str(copy))
    received = tmp_path / "received" / "copy.bin"
    remote.get(str(copy), received)
    assert all(p.read_bytes() == data for p in (source, hardlink, copy, received))
    assert remote.sha256(str(copy)) == hashlib.sha256(data).hexdigest()
    assert remote.sha256(str(tmp_path / "absent")) is None
    with pytest.raises((OSError, RuntimeError)):
        remote.get(str(tmp_path / "absent"), received)


@pytest.mark.parametrize("complete", [True, False])
def test_cpa_reads_actual_json_through_remote_without_ssh(tmp_path, monkeypatch, complete):
    local = adapters.LocalRemote(scripts_dir=str(tmp_path / "scripts"))
    clip = tmp_path / "clip"
    calls = []

    class OfflineProvider:
        def put(self, source: Path, target: str):
            local.put(source, target)

        def run_script(self, *, name, body, timeout):
            calls.append(name)
            if name == "cpa-synthetic":
                out = clip / "cpa/synthetic.out.json"
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(json.dumps({"complete": complete, "text": "synthetic", "attempts": 1,
                                           "prompt_sha256": hashlib.sha256(adapters._wire_prompt("synthetic").encode()).hexdigest(),
                                           "model": adapters.CPA_JUDGE_MODEL, "effort": adapters.CPA_JUDGE_EFFORT, "stream": True,
                                           "http_log": [{"http": 200}], "usage": {"output_tokens": 3}}))
                return RemoteResult(0, "", "")
            return local.run_script(name=name, body=body, timeout=timeout)

    def forbidden(*args, **kwargs):
        raise AssertionError("the CPA read must use the selected Remote")

    monkeypatch.setattr(adapters, "_ssh", forbidden)
    result = SshCpaJudge(OfflineProvider()).ask(call_id="synthetic", prompt_text="synthetic", remote_dir=str(clip))
    assert calls == ["cpa-synthetic", "cpa-read-synthetic"]
    assert result["complete"] is complete and result["rc"] == (0 if complete else 1)
    assert result["response_text"] == ("synthetic" if complete else "")
    assert result["http_log"] == [{"http": 200}] and result["usage"] == {"output_tokens": 3}
