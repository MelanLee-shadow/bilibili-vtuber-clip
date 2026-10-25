"""Prevent retired-code selection across filesystem, SSH and shell consumers."""
from __future__ import annotations

import ast
import inspect
import json
from pathlib import Path
import re
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
# Exactly one historical exporter preimage. Match the whole line so adding
# executable paths elsewhere in that file cannot inherit the exemption.
HISTORICAL_LINES = {
    "scripts/_export_oss_policy.py": {
        "        '  `/opt/bilive/autoslice/repo` 不再是已部署代码。\\n'",
    },
}
OLD_CODE_ROOT = re.compile(r"/opt/bilive/autoslice/repo(?=/|[^A-Za-z0-9_]|$)")


def test_no_retired_code_root_literals() -> None:
    offenders = []
    for directory in ("scripts", "src", "ops"):
        for path in sorted((ROOT / directory).rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except UnicodeError:
                continue
            relative = path.relative_to(ROOT).as_posix()
            for number, line in enumerate(lines, 1):
                if OLD_CODE_ROOT.search(line) and line not in HISTORICAL_LINES.get(relative, set()):
                    offenders.append(f"{relative}:{number}: {line}")
    assert not offenders, "\n".join(offenders)




def test_remote_bridge_only_uses_the_generated_resolver() -> None:
    from scripts import llm_via_runtime_cpa_ssh as bridge
    from src.autoslice.deployed_code_root import deployed_code_root, deployed_code_root_bootstrap

    # This assertion fails on the old hand-copied bridge, even when its
    # topology behavior happens to match the canonical implementation today.
    assert inspect.getsource(deployed_code_root) in bridge.REMOTE_PROGRAM
    assert bridge.deployed_code_root_bootstrap is deployed_code_root_bootstrap
    assert deployed_code_root_bootstrap(bind_repo=False) in bridge.REMOTE_PROGRAM
    selection = ast.parse(bridge.REMOTE_PROGRAM.split("sys.path.insert(0, str(repo))", 1)[0])
    definitions = [node for node in selection.body if isinstance(node, ast.FunctionDef)]
    assert [node.name for node in definitions] == ["deployed_code_root"]
    # Outside the emitted function, selection can only call the shared
    # resolver with the request runtime; it must not reimplement its probes.
    selection.body = [node for node in selection.body if node not in definitions]
    calls = [node for node in ast.walk(selection) if isinstance(node, ast.Call)]
    resolver_calls = [node for node in calls if isinstance(node.func, ast.Name)
                      and node.func.id == "deployed_code_root"]
    assert len(resolver_calls) == 1
    assert ast.dump(resolver_calls[0]) == ast.dump(ast.parse("deployed_code_root(runtime)").body[0].value)
    assert not any(isinstance(node.func, ast.Attribute) and node.func.attr in
                   {"lstat", "readlink", "is_symlink", "is_dir", "read_text", "fullmatch"} for node in calls)


def deployment(tmp_path: Path, commit: str = "a" * 40) -> tuple[Path, Path]:
    base = tmp_path / "runtime with spaces"
    repo = base / "versions" / commit / "repo"
    repo.mkdir(parents=True)
    (repo / "DEPLOYED_COMMIT").write_text(commit + "\n")
    (base / "current").symlink_to(f"versions/{commit}")
    (base / "repo").mkdir()
    return base, repo


@pytest.mark.parametrize("commit", ["a" * 40, "d" * 40])
def test_generated_bootstrap_and_shell_cli_follow_current(tmp_path: Path, commit: str) -> None:
    from src.autoslice.deployed_code_root import deployed_code_root_bootstrap

    base, repo = deployment(tmp_path, commit)
    result = subprocess.run([sys.executable, "-I", "-c", deployed_code_root_bootstrap(base) + "print(repo)"],
                            check=True, capture_output=True, text=True)
    assert result.stdout.strip() == str(repo)
    cli = ROOT / "src/autoslice/deployed_code_root.py"
    result = subprocess.run([sys.executable, "-I", str(cli), "--base", str(base)],
                            check=True, capture_output=True, text=True)
    assert result.stdout.strip() == str(repo)
    reference = subprocess.run([sys.executable, "-I", str(cli), "--base", str(base), "--reference"],
                               check=True, capture_output=True, text=True)
    assert reference.stdout.strip() == str(base / "current/repo")
    # Resolve afresh on the execution host after current switches. There is no
    # per-version registration or code rewrite in the consuming program.
    program = deployed_code_root_bootstrap(base) + "print(repo)"
    successor = base / "versions" / ("e" * 40) / "repo"
    successor.mkdir(parents=True)
    (successor / "DEPLOYED_COMMIT").write_text("e" * 40 + "\n")
    (base / "current").unlink()
    (base / "current").symlink_to("versions/" + "e" * 40)
    result = subprocess.run([sys.executable, "-I", "-c", program], check=True, capture_output=True, text=True)
    assert result.stdout.strip() == str(successor)




@pytest.mark.parametrize("explicit", [False, True])
def test_vad_ordinary_remote_provider_executes_current_or_explicit_script(tmp_path: Path, monkeypatch, explicit: bool) -> None:
    from src.autoslice import subtitle_timing_qa as timing
    from src.autoslice.deployed_code_root import deployed_code_root_bootstrap

    base, repo = deployment(tmp_path)
    for target, offset in ((repo, 10), (base / "repo", 999)):
        script = target / "scripts/silero_vad_spans.py"
        script.parent.mkdir()
        script.write_text('print(\'{"spans":[{"start_ms":' + str(offset) + ',"end_ms":20}]}\')\n')
    chosen = tmp_path / "explicit vad.py"
    chosen.write_text('print(\'{"spans":[{"start_ms":30,"end_ms":40}]}\')\n')
    monkeypatch.delenv("AUTOSLICE_VAD_SCRIPT", raising=False)
    monkeypatch.setenv("AUTOSLICE_VAD_PYTHON", sys.executable)
    monkeypatch.setattr(timing, "deployed_code_root_bootstrap", lambda: deployed_code_root_bootstrap(base))
    actual_run = subprocess.run
    commands = []

    def fake_run(command, **kwargs):
        if command[0] == "ssh":
            commands.append(command[-1])
            return actual_run(["bash", "-c", command[-1]], **kwargs)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(timing.subprocess, "run", fake_run)
    provider = timing.build_ssh_silero_vad_provider("remote-test", remote_script=str(chosen) if explicit else None)
    assert provider(tmp_path / "input.mp4", 100, 200) == [timing.SpeechSpan(130, 140) if explicit else timing.SpeechSpan(110, 120)]
    assert len(commands) == 1
    if not explicit:
        (base / "current").unlink()
        (base / "current").symlink_to("repo")
        with pytest.raises(RuntimeError, match="silero vad failed"):
            provider(tmp_path / "input.mp4", 100, 200)


def test_speaker_ordinary_remote_command_uses_one_current_root(tmp_path: Path, monkeypatch) -> None:
    from src.autoslice import producer_speaker as speaker
    from src.autoslice.deployed_code_root import deployed_code_root_bootstrap

    base, repo = deployment(tmp_path)
    staging = tmp_path / "staging"
    profile = staging / "assets/profile.json"
    profile.parent.mkdir(parents=True)
    profile.write_text("{}")
    (repo / "assets").mkdir()
    (repo / "assets/profile.json").write_text("{}")
    receipt = tmp_path / "execution.json"
    executable = tmp_path / "speaker python"
    executable.write_text(
        f"#!{sys.executable}\nimport json,os,sys\nfrom pathlib import Path\n"
        + f"Path({str(receipt)!r}).write_text(json.dumps({{'cwd':os.getcwd(),'profile':str(Path(sys.argv[sys.argv.index('--profile')+1]).resolve())}}))\n"
        + "sys.exit(37)\n"
    )
    executable.chmod(0o755)
    media = tmp_path / "media.mp4"
    text = tmp_path / "text.srt"
    media.write_bytes(b"media")
    text.write_text("text")
    monkeypatch.setattr(speaker, "ROOT", staging)
    monkeypatch.setattr(speaker, "validate_producer_runtime_prerequisites", lambda **kwargs: None)
    monkeypatch.setattr(speaker, "deployed_code_root_bootstrap", lambda: deployed_code_root_bootstrap(base))
    monkeypatch.setattr(speaker, "run", lambda *args, **kwargs: None)
    actual_run = subprocess.run

    def fake_run(command, **kwargs):
        if command[0] == "ssh" and "repo=$(python3" in command[-1]:
            return actual_run(["bash", "-c", command[-1]], **kwargs)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(speaker.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="SPEAKER_FINALIZATION_FAILED"):
        speaker.run_speaker_finalizer(host="remote-test", candidate_id="new-member", media_path=media,
            text_srt_path=text, output_srt_path=tmp_path / "final.srt", output_ass_path=tmp_path / "final.ass",
            output_manifest_path=tmp_path / "final.json", work_dir=tmp_path / "work",
            profile_path=profile, speaker_python=executable)
    observed = json.loads(receipt.read_text())
    assert observed == {"cwd": str(repo), "profile": str(repo / "assets/profile.json")}


@pytest.mark.parametrize("versioned", [False, True])
def test_legacy_installer_resolves_on_target_and_refuses_immutable_tree(tmp_path: Path, versioned: bool) -> None:
    base, repo = deployment(tmp_path)
    if not versioned:
        (base / "current").unlink()
    source = (ROOT / "scripts/deploy_autoslice.sh").read_text()
    start = source.index('DEPLOYED_ROOT_BOOTSTRAP=')
    end = source.index('STAGE=$REMOTE_BASE/repo.deploy-', start)
    # Execute the real preflight with a local SSH transport. No installer,
    # producer, model or production path is executed by this fixture.
    program = (
        'set -euo pipefail\nROOT=$1\nREMOTE_BASE=$2\nHOST=fixture\n'
        'remote_ssh() { shift; bash -c "$1"; }\n'
        + source[start:end] + '\nprintf "%s\\n" "$REMOTE_REPO"\n'
    )
    result = subprocess.run(["bash", "-c", program, "preflight", str(ROOT), str(base)],
                            capture_output=True, text=True)
    if versioned:
        assert result.returncode == 2 and "versioned deployment entrypoint" in result.stderr
        assert (repo / "DEPLOYED_COMMIT").read_text() == "a" * 40 + "\n"
    else:
        assert result.returncode == 0 and result.stdout.strip() == str(base / "repo")
        start = source.index('new_adapter_source=')
        end = source.index('managed_crontab_exact() {', start)
        payload = (
            'set -euo pipefail\nrepo=$1\nprintf -v cron_repo \'%q\' "$repo"\n'
            + source[start:end] + '\nprintf "%s\\n%s\\n" "$new_adapter_source" "$runner_cron"\n'
        )
        result = subprocess.run(["bash", "-c", payload, "payload", str(repo)],
                                check=True, capture_output=True, text=True)
        adapter, cron = result.stdout.splitlines()
        assert adapter == str(repo / "ops/recording/bililive_recorder_adapter.py")
        cd = cron.split("cd ", 1)[1].split(" && ", 1)[0]
        cwd = subprocess.run(["bash", "-c", "cd " + cd + "; pwd"],
                             check=True, capture_output=True, text=True)
        assert cwd.stdout.strip() == str(repo)
