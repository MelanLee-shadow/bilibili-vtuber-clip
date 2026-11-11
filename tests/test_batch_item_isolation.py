"""Ordinary-entry regressions shared with the unchanged baseline carrier."""
import json
import ast
import os
from pathlib import Path
import subprocess

import pytest

from src.autoslice import runtime_gc as gc


def old_file(path, content=b"media"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    stamp = 1_600_000_000
    os.utime(path, (stamp, stamp))
    os.utime(path.parent, (stamp, stamp))
    return path


@pytest.mark.parametrize("phase", ["walk-file", "walk-directory", "unreadable", "plan"])
def test_gc_cli_quarantines_one_group_not_the_batch(tmp_path, monkeypatch, capsys, phase):
    bad = old_file(tmp_path / "private-runs/bad/cache.wav")
    sibling = old_file(bad.parent / "keep.mp4")
    good = old_file(tmp_path / "private-runs/good/reclaim.mp4")
    proc = tmp_path / "proc"
    proc.mkdir()
    (proc / "locks").write_text("")
    stat_impl, lstat_impl = Path.stat, Path.lstat
    walk_impl = gc.walk_entries
    triggered = []
    scanned_bad = False
    target = bad.parent if phase == "walk-directory" else bad

    def observed_walk(root):
        nonlocal scanned_bad
        for path, info in walk_impl(root):
            # Exercise a scan failure independently of the walker's stat cache.
            if path == bad and phase == "walk-file":
                triggered.append(True)
                bad.unlink()
                raise FileNotFoundError("file disappeared during scan")
            yield path, info
        if root == bad.parent:
            scanned_bad = True

    def raced_stat(path, *args, **kwargs):
        # stat on the regular file is not the walk-file lstat seam.
        if path == target and phase in {"walk-directory", "unreadable"} and not triggered:
            triggered.append(True)
            if phase == "unreadable":
                raise PermissionError("input unreadable")
            raise FileNotFoundError("directory disappeared")
        return stat_impl(path, *args, **kwargs)

    def raced_lstat(path, *args, **kwargs):
        if path == bad:
            if phase == "plan" and scanned_bad:
                triggered.append(True)
                bad.unlink()
                raise FileNotFoundError("file disappeared during plan")
            if phase == "unreadable":
                raise PermissionError("input unreadable")
        return lstat_impl(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", raced_stat)
    monkeypatch.setattr(Path, "lstat", raced_lstat)
    monkeypatch.setattr(gc, "walk_entries", observed_walk)
    output = tmp_path / "plan.json"
    rc = gc.main(["--base", str(tmp_path), "--home", str(tmp_path / "no-home"),
                  "--proc", str(proc), "--json", str(output)])
    assert rc == 0, capsys.readouterr().out
    result = json.loads(output.read_text())
    assert {row["path"] for row in result["targets"]} == {str(good)}
    assert result["item_failures"]
    assert triggered or phase == "unreadable"
    assert sibling.exists() and good.exists()  # CLI is genuinely read-only
    assert result["summary"]["private-runs"]["files"] == 1


def test_gc_os_walk_error_is_not_silently_ignored(tmp_path, monkeypatch):
    media = old_file(tmp_path / "private-runs/bad/keep.mp4")
    good = old_file(tmp_path / "private-runs/good/reclaim.mp4")
    original = os.scandir

    def scandir(root, *args, **kwargs):
        if Path(root) == media.parent:
            raise PermissionError("directory unreadable")
        return original(root, *args, **kwargs)

    # Both os.walk and the direct scandir walker must surface this OS error.
    monkeypatch.setattr(gc.os, "scandir", scandir)
    result = gc.collect(tmp_path, home=tmp_path / "none", live=gc.Activity())
    assert {row["path"] for row in result["targets"]} == {str(good)}
    assert result["item_failures"][0]["reason_code"] == "ITEM_UNREADABLE"


def test_gc_cli_unreadable_optional_home_keeps_other_roots(tmp_path, monkeypatch, capsys):
    good = old_file(tmp_path / "private-runs/good/reclaim.mp4")
    home = tmp_path / "clone-home"
    home.mkdir()
    proc = tmp_path / "proc"
    proc.mkdir()
    (proc / "locks").write_text("")
    is_dir = Path.is_dir
    def unreadable(path):
        if path == home:
            raise PermissionError("clone parent unreadable")
        return is_dir(path)
    monkeypatch.setattr(Path, "is_dir", unreadable)
    output = tmp_path / "plan.json"
    assert gc.main(["--base", str(tmp_path), "--home", str(home),
                    "--proc", str(proc), "--json", str(output)]) == 0, capsys.readouterr().out
    result = json.loads(output.read_text())
    assert {row["path"] for row in result["targets"]} == {str(good)}
    assert result["item_failures"][0]["reason_code"] == "ITEM_UNREADABLE"


@pytest.mark.parametrize("authority", ["policy", "registry"])
def test_gc_authority_still_refuses_whole_batch(tmp_path, monkeypatch, capsys, authority):
    old_file(tmp_path / "private-runs/good/keep.mp4")
    monkeypatch.setattr(gc, "activity", lambda *_: gc.Activity())
    if authority == "policy":
        (tmp_path / "runtime-gc.json").write_text("{bad json")
    else:
        from src.autoslice import publication_registry
        def refused(*_args, **_kwargs):
            raise PermissionError("registry unreadable")
        monkeypatch.setattr(publication_registry, "load_publication_registry", refused)
    assert gc.main(["--base", str(tmp_path)]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "REFUSED"


def test_census_bad_decode_missing_source_bad_label_continue_and_resume(tmp_path):
    from test_channel_census_cli import setup_case, execute, events
    command, env, _ = setup_case(tmp_path)
    recordings = tmp_path / "recordings.json"
    row = json.loads(recordings.read_text())["recordings"][0]
    missing = {**row, "path": "missing.wav"}
    broken = {**row, "path": "broken.mp4"}
    (tmp_path / "broken.mp4").write_bytes(b"one video frame, no audio")
    recordings.write_text(json.dumps({"recordings": [broken, missing, row]}))
    provider = tmp_path / "item_failure_provider.py"
    provider.write_text('''
import json, os, subprocess
from census_fake_provider import transcribe as good_transcribe, label as good_label
def transcribe(recording, media_path):
    if media_path.name == "broken.mp4":
        raise subprocess.CalledProcessError(234, ["ffmpeg"])
    return good_transcribe(recording, media_path)
def label(window, *, prompt):
    if window.start_s == 300 and not os.environ.get("FIX_LABEL"):
        return "not JSON"
    return good_label(window, prompt=prompt)
''')
    command[command.index("--transcriber") + 1] = "item_failure_provider:transcribe"
    command[command.index("--labeler") + 1] = "item_failure_provider:label"
    env["PYTHONPATH"] = str(tmp_path) + os.pathsep + env["PYTHONPATH"]
    first = execute(command, env)
    assert first.returncode == 0, first.stderr
    receipt = json.loads(first.stdout)
    assert receipt["status"] == "PARTIAL"
    partial = Path(receipt["run_dir"])
    result = json.loads((partial / "batch-result.json").read_text())
    assert result["labeled_windows"] == 2
    assert len(result["item_failures"]) == 3
    assert {item["reason_code"] for item in result["item_failures"]} == {"ITEM_INVALID", "ITEM_DISAPPEARED"}
    before = {p: p.read_bytes() for p in (tmp_path / "out/labels").glob("*.json")}
    second = execute(command, {**env, "FIX_LABEL": "1"})
    assert second.returncode == 0, second.stderr
    result2 = json.loads((Path(json.loads(second.stdout)["run_dir"]) / "batch-result.json").read_text())
    assert result2["labeled_windows"] == 3 and len(result2["item_failures"]) == 2
    assert events(env).count("transcribe") == 1
    assert all(path.read_bytes() == payload for path, payload in before.items())


def test_census_identity_refusal_is_not_an_item_skip(tmp_path):
    from test_channel_census_cli import setup_case, execute
    command, env, _ = setup_case(tmp_path)
    (tmp_path / "wrong_identity.py").write_text('''
import json
from census_fake_provider import label as good_label
def label(window, *, prompt):
    row = json.loads(good_label(window, prompt=prompt))
    row["window_id"] = "another window"
    return json.dumps(row)
''')
    command[command.index("--labeler") + 1] = "wrong_identity:label"
    env["PYTHONPATH"] = str(tmp_path) + os.pathsep + env["PYTHONPATH"]
    result = execute(command, env)
    assert result.returncode == 2 and "CENSUS_LABEL_WINDOW_MISMATCH" in result.stderr


def test_census_cli_looping_source_locator_is_one_failed_recording(tmp_path):
    from test_channel_census_cli import setup_case, execute
    command, env, _ = setup_case(tmp_path)
    source = tmp_path / "recordings.json"
    row = json.loads(source.read_text())["recordings"][0]
    (tmp_path / "loop-a.wav").symlink_to("loop-b.wav")
    (tmp_path / "loop-b.wav").symlink_to("loop-a.wav")
    source.write_text(json.dumps({"recordings": [{**row, "path": "loop-a.wav"}, row]}))
    process = execute(command, env)
    assert process.returncode == 0, process.stderr
    receipt = json.loads(process.stdout)
    assert receipt["status"] == "PARTIAL"
    result = json.loads((Path(receipt["run_dir"]) / "batch-result.json").read_text())
    assert result["labeled_windows"] == 3 and len(result["item_failures"]) == 1
    assert result["item_failures"][0]["reason_code"] == "ITEM_INVALID"


def test_census_provider_refusal_is_not_a_bad_label(tmp_path):
    from test_channel_census_cli import setup_case, execute
    command, env, _ = setup_case(tmp_path)
    (tmp_path / "refusing_provider.py").write_text('''
def label(window, *, prompt):
    raise ValueError("SOURCE_CHECKPOINT_PARENT_REFUSED")
''')
    command[command.index("--labeler") + 1] = "refusing_provider:label"
    env["PYTHONPATH"] = str(tmp_path) + os.pathsep + env["PYTHONPATH"]
    result = execute(command, env)
    assert result.returncode == 2 and "SOURCE_CHECKPOINT_PARENT_REFUSED" in result.stderr


def test_census_same_bytes_at_another_locator_reuses_completed_work(tmp_path):
    from test_channel_census_cli import setup_case, execute, events
    command, env, _ = setup_case(tmp_path)
    first = execute(command, env)
    assert first.returncode == 0, first.stderr
    before = events(env)
    source = tmp_path / "recordings.json"
    rows = json.loads(source.read_text())
    rows["recordings"][0]["path"] = str(tmp_path / "recording.wav")
    relocated = tmp_path / "different-list.json"
    relocated.write_text(json.dumps(rows))
    command[command.index("--recordings") + 1] = str(relocated)
    second = execute(command, env)
    assert second.returncode == 0, second.stderr
    assert json.loads(first.stdout)["run_dir"] == json.loads(second.stdout)["run_dir"]
    assert events(env) == before


def test_new_independent_member_enters_existing_speaker_cli(tmp_path, monkeypatch):
    """Held-out member: a batch with an unreadable first input and valid next input."""
    from scripts import batch_speaker_review as cli
    from lidousha.test_batch_speaker_review import _plan
    plan = _plan()
    first = plan["entries"][0]
    plan["entries"].append({**first, "candidate_id": "new_member", "review_name": "02_new"})
    plan["expected_count"] = 2
    source = tmp_path / "batch.json"
    source.write_text(json.dumps(plan))
    seen = []
    def build(entry, **kwargs):
        seen.append(entry["candidate_id"])
        if len(seen) == 1:
            raise FileNotFoundError("source disappeared")
        return {"candidate_id": entry["candidate_id"], "generator_sha256": kwargs["generator_sha256"]}
    monkeypatch.setattr(cli, "build_review_item", build)
    monkeypatch.setattr(cli, "_write_review_csv", lambda *_: None)
    assert cli.main(["--plan", str(source), "--output-dir", str(tmp_path / "result")]) == 3
    result = json.loads((tmp_path / "result/batch-manifest.json").read_text())
    assert seen == [first["candidate_id"], "new_member"]
    assert result["ready_count"] == 1 and len(result["failures"]) == 1


def test_reference_scan_cli_accounts_bad_records_and_continues(tmp_path, monkeypatch, capsys):
    from scripts import scan_state_dangling_media_refs as cli
    delivery = tmp_path / "repo/lidousha/2026-01-01"
    delivery.mkdir(parents=True)
    (delivery / "bad.record.json").write_text("[]")
    (delivery / "good.record.json").write_text('{"video_path":"/good/auto_1_2_3.mp4"}')
    state = tmp_path / "state"
    state.mkdir()
    (state / "bad.json").write_text("broken JSON")
    missing = str(tmp_path / "out/missing.mp4")
    (state / "good.json").write_text(json.dumps({"picks": [
        {"candidate_id": "auto_1_2_3", "video_path": missing},
    ]}))
    output = tmp_path / "result.json"
    monkeypatch.setattr(cli.sys, "argv", ["scan", "--base", str(tmp_path), "--json", str(output)])
    assert cli.main() == 0
    failures = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
    assert len(failures) == 2 and all(row["reason_code"] == "ITEM_INVALID" for row in failures)
    rows = json.loads(output.read_text())
    assert len(rows) == 1 and rows[0]["missing_path"] == missing


def test_complete_strict_allowlist():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(["bash", "scripts/check_mypy_strict.sh"], cwd=root,
                            text=True, capture_output=True, timeout=180)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("module,entry", [
    ("src/autoslice/runtime_gc.py", "groups"),
    ("src/autoslice/runtime_gc.py", "collect"),
    ("scripts/channel_census.py", "recordings_from_list"),
    ("scripts/channel_census.py", "run_census"),
    ("scripts/scan_state_dangling_media_refs.py", "surviving_stem_index"),
    ("scripts/scan_state_dangling_media_refs.py", "main"),
])
def test_input_batch_entry_inventory_has_shared_isolation(module, entry):
    """A removed integration cannot pass just because the helper itself works."""
    root = Path(__file__).resolve().parents[1]
    tree = ast.parse((root / module).read_text())
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == entry)
    assert any(isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
               and node.func.attr == "isolate" for node in ast.walk(function)), (module, entry)
