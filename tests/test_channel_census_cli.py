"""Offline CLI acceptance: real entrypoint, fake providers, input-bound resume."""
from dataclasses import asdict
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import wave

import pytest

from scripts import channel_census as cli
from src.autoslice import channel_census as cc
from src.autoslice.channel_profile import ChannelProfileError, load_channel_profile

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/channel_census"
METRIC = """# Metric
## 七维量化表（层内细排用）
每项：0=没有，1=弱或附带，2=明确但普通，3=强且构成主要看点，4=不可替代且有完整证据链。
1. **主播中心度**：主播的选择。
2. **观点强度**：明确立场。
3. **受众兴趣对齐**：数学、游戏。
   对解题过程和解释感兴趣。
4. **关系互动**：来回回应。
5. **人设反差**：前后依据。
6. **喜剧兑现**：明确收口。
7. **自足性**：候选边界。
## 不应进入提示
另一个段落。
"""
CONFIG = cc.CensusConfig("testhost", (cc.AudienceInterest("math", "数学", "解题与解释"),),
                         "## 七维量化表（层内细排用）", "3. **受众兴趣对齐")


def config_doc(config=CONFIG):
    return json.loads(json.dumps({"schema_version": cc.CENSUS_CONFIG_SCHEMA, **asdict(config)}))


def setup_case(tmp_path):
    fixture = json.loads((FIXTURE / "cli-recording.json").read_text())
    media = tmp_path / "recording.wav"
    with wave.open(str(media), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(1)
        out.setframerate(fixture["sample_rate"])
        out.writeframes(b"\x80" * (fixture["duration_ms"] * fixture["sample_rate"] // 1000))
    manifest = json.loads((ROOT / "profiles/_template/profile.json").read_text())
    manifest["profile_id"] = "testhost"
    manifest["identity"]["display_name"] = "测试主播"
    manifest["assets"]["root"] = "assets/testhost"
    # Resolve assets inside the pytest tree, not the production/default assets.
    manifest["assets"]["files"]["slice_selection_metric"] = "metric.md"
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps(manifest))
    # The loader intentionally resolves an external manifest's assets under ROOT.
    # Pass a private repo_root for API checks; CLI loads the same profile with
    # its standard project root and an assets root relative to that root.
    # A private CLI carrier keeps relative assets entirely under pytest's temp root.
    carrier = tmp_path / "tree"
    (carrier / "scripts").mkdir(parents=True)
    (carrier / "src").symlink_to(ROOT / "src", target_is_directory=True)
    for script in ("channel_census.py", "validate_channel_profile.py", "free_asr_client.py"):
        (carrier / "scripts" / script).write_bytes((ROOT / "scripts" / script).read_bytes())
    manifest["assets"]["root"] = "assets/testhost"
    assets = carrier / "assets/testhost"
    assets.mkdir(parents=True)
    (assets / "metric.md").write_text(METRIC)
    (assets / "channel_census_config.v1.json").write_text(json.dumps(config_doc()))
    profile.write_text(json.dumps(manifest))
    recordings = tmp_path / "recordings.json"
    recordings.write_text(json.dumps({"recordings": [{
        "path": media.name, "duration_ms": fixture["duration_ms"],
        "recording_date": "2026-01-01", "danmaku_starts_ms": fixture["danmaku_starts_ms"],
    }]}))
    command = [sys.executable, "-B", str(carrier / "scripts/channel_census.py"),
               "--manifest", str(profile), "--recordings", str(recordings),
               "--out", str(tmp_path / "out"), "--host", "fake-worker",
               "--census-id", "test-three-windows", "--label-model", "fake.v1",
               "--transcriber", "census_fake_provider:transcribe",
               "--transcript-algorithm", "fake-asr.v1", "--labeler", "census_fake_provider:label"]
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": f"{FIXTURE}:{ROOT}",
           "CENSUS_TEST_EVENTS": str(tmp_path / "events"),
           "CENSUS_TEST_RECORDING": str(FIXTURE / "cli-recording.json")}
    return command, env, assets


def execute(command, env):
    return subprocess.run(command, env=env, capture_output=True, text=True, timeout=30)


def events(env):
    return Path(env["CENSUS_TEST_EVENTS"]).read_text().splitlines()


def snapshot(out):
    return {p.relative_to(out).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in out.rglob("*") if p.is_file() and p.name != ".lock"}


def test_cli_three_windows_stop_resume_and_input_sha(tmp_path):
    command, env, assets = setup_case(tmp_path)
    out = tmp_path / "out"
    interrupted = execute(command, {**env, "CENSUS_TEST_STOP": str(out / "STOP")})
    assert interrupted.returncode == 75, interrupted.stderr
    assert events(env).count("transcribe") == 1
    assert sum(e.startswith("label:") for e in events(env)) == 1
    (out / "STOP").unlink()
    before = snapshot(out)
    resumed = execute(command, env)
    assert resumed.returncode == 0, resumed.stderr
    for name, original in before.items():
        assert snapshot(out)[name] == original  # no rewrite, including manifest timestamp
    assert events(env).count("transcribe") == 1
    assert events(env).count("build") == 2  # initial validation + construction; no reconstruction
    assert sum(e.startswith("label:") for e in events(env)) == 3
    run_dir = Path(json.loads(resumed.stdout)["run_dir"])
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["census_id"] == "test-three-windows"
    assert manifest["profile_id"] == "testhost"
    assert manifest["executed_on_host"] == "fake-worker"
    assert manifest["label_model"] == "fake.v1"
    assert manifest["transcript_algorithm"] == "fake-asr.v1"
    assert manifest["window_seconds"] == 300
    assert manifest["created_at"]
    assert manifest["recordings"][0] == {
        "basename": "recording.wav", "sha256": hashlib.sha256((tmp_path / "recording.wav").read_bytes()).hexdigest(),
        "duration_ms": 900000, "recording_date": "2026-01-01",
    }
    prompt = (run_dir / "label-prompt.txt").read_bytes()
    assert hashlib.sha256(prompt).hexdigest() == manifest["label_prompt_sha256"]
    windows = json.loads((run_dir / "windows.json").read_text())
    assert [(w["start_s"], w["end_s"]) for w in windows] == [(0, 300), (300, 600), (600, 900)]
    assert all(g["basis"] == "CENSUS_LABEL" for w in windows for g in w["grades"])
    raw_inputs = json.loads(next((out / "windows").glob("*.json")).read_text())
    assert [w["danmaku_count"] for w in raw_inputs] == [2, 1, 1]
    assert len(raw_inputs[0]["cues"]) == 2  # cue crossing boundary stays at its start
    for path in (out / "labels").glob("*.json"):
        label = json.loads(path.read_text())
        assert len(label) == 11
        assert set(label["grades"]) == set(cc.CENSUS_WINDOW_DIMENSIONS)
        assert "self_contained" not in label["grades"]
    clues = json.loads((run_dir / "selection_census_clues.v1.json").read_text())
    assert clues["provenance"]["window_count"] == 3
    assert clues["provenance"]["valid_window_count"] == 3
    assert (run_dir / "report.md").is_file()
    before = snapshot(out)
    prior_events = events(env)
    assert execute(command, env).returncode == 0
    assert events(env) == prior_events
    assert snapshot(out) == before
    # Changing only a prompt input invalidates labels, retaining transcript/window stages.
    (assets / "metric.md").write_text(METRIC.replace("主播的选择", "主播的具体选择"))
    changed = execute(command, env)
    assert changed.returncode == 0, changed.stderr
    assert json.loads(changed.stdout)["run_dir"] != str(run_dir)
    assert events(env).count("transcribe") == 1
    assert events(env).count("build") == 2
    assert sum(e.startswith("label:") for e in events(env)) == 6
    # Different input bytes invalidate transcription and its downstream windows/labels.
    with (tmp_path / "recording.wav").open("ab") as media:
        media.write(b"changed")
    assert execute(command, env).returncode == 0
    assert events(env).count("transcribe") == 2
    assert sum(e.startswith("label:") for e in events(env)) == 9


def test_prompt_uses_runtime_metric_and_lint_only_audience_paragraph():
    prompt = cc.build_census_label_prompt(host_display_name="甲主播", metric_text=METRIC, config=CONFIG)
    section = cc.extract_metric_section(METRIC, heading=CONFIG.metric_dimension_section_heading)
    assert "主播：甲主播" in prompt and section in prompt
    assert "另一个段落" not in prompt
    assert "朗读、引述、角色台词和播放内容不算主播本人立场" in prompt
    assert "不给4" in prompt and "不评 self_contained" in prompt
    assert cc.parse_census_config(config_doc(), profile_id="testhost") == CONFIG
    with pytest.raises(ValueError, match="INTEREST_NOT_IN_METRIC"):
        cc.build_census_label_prompt(host_display_name="甲", metric_text=METRIC.replace(
            "数学、游戏", "游戏") + "数学", config=CONFIG)
    for metric in (METRIC.replace("## 七维量化表", "## 其他表"), METRIC + section):
        with pytest.raises(ValueError, match="SECTION_NOT_UNIQUE"):
            cc.lint_census_config(CONFIG, metric_text=metric)
    with pytest.raises(ValueError, match="PROFILE_MISMATCH"):
        cc.parse_census_config(config_doc(), profile_id="another")


def test_window_silence_partial_tail_and_text_limit():
    cues = (cc.CensusTranscriptCue(300000, 301000, "字" * 3000),)
    windows = cc.build_census_windows(cues, recording_group="r", window_seconds=300, duration_ms=610001)
    assert len(windows) == 3
    assert windows[0].cues == () and windows[2].end_s == 611
    assert len(cc.census_window_text(cli.capped_window(windows[1]))) == 2500
    with pytest.raises(ValueError, match="CUE_OUTSIDE"):
        cc.build_census_windows(cues, recording_group="r", window_seconds=300, duration_ms=10)
    with pytest.raises(ValueError, match="INVALID_CENSUS_WINDOW_SECONDS"):
        cc.build_census_windows((), recording_group="r", window_seconds=0)


def valid_label():
    return {"window_id": "r:0000", "host_activity": "FREE_TALK", "secondary_activities": [],
            "topic": "话题", "grades": dict.fromkeys(cc.CENSUS_WINDOW_DIMENSIONS, None),
            "audience_interest_hits": [], "persons": [], "address_terms": [],
            "trigger_kinds": ["UNKNOWN"], "quoted_or_roleplay": False, "summary": "转写缺失"}


@pytest.mark.parametrize("mutation", [
    lambda row: row.update(score=4), lambda row: row.update(window_id="other"),
    lambda row: row["grades"].update(comedic_payoff=4),
    lambda row: row["grades"].update(comedic_payoff=True),
    lambda row: row["grades"].update(self_contained=2),
    lambda row: row.update(host_activity="ADDRESS"),
    lambda row: row.update(host_activity="UNSPECIFIED"),
    lambda row: row.update(secondary_activities=["RELATION"]),
    lambda row: row.update(trigger_kinds=["INVALID"]),
    lambda row: row.update(audience_interest_hits=["unknown"]),
    lambda row: row.update(quoted_or_roleplay=None),
    lambda row: row.update(topic="字" * 13), lambda row: row.update(summary="字" * 21),
])
def test_strict_label_contract_rejects_invalid_rows(mutation):
    window = cc.CensusWindowInput("r:0000", "r", 0, 300, (), 0, True)
    row = valid_label()
    mutation(row)
    with pytest.raises(ValueError):
        cc.parse_census_window_label(row, window=window, config=CONFIG)


def test_missing_asr_preserves_unmeasured_not_zero():
    window = cc.CensusWindowInput("r:0000", "r", 0, 300, (), 0, True)
    row = valid_label()
    parsed = cc.parse_census_window_label(row, window=window, config=CONFIG)
    assert all(g.grade is None and g.basis == "CENSUS_LABEL" for g in parsed.grades)
    row["grades"]["comedic_payoff"] = 0
    with pytest.raises(ValueError, match="GRADE_WITHOUT_TRANSCRIPT"):
        cc.parse_census_window_label(row, window=window, config=CONFIG)


def test_create_only_and_lock_prevent_overwrites_or_duplicate_work(tmp_path):
    path = tmp_path / "stage.json"
    cli.create_only(path, b"one")
    before = path.stat().st_mtime_ns
    cli.create_only(path, b"one")
    assert path.stat().st_mtime_ns == before
    with pytest.raises(ValueError, match="ARTIFACT_CONFLICT"):
        cli.create_only(path, b"two")
    assert path.read_bytes() == b"one" and not list(tmp_path.glob(".census-*"))
    command, env, _ = setup_case(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    with (out / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = execute(command, env)
        assert result.returncode == 75 and "CENSUS_LOCKED" in result.stderr
    assert not Path(env["CENSUS_TEST_EVENTS"]).exists()


def test_bcut_adapter_uses_existing_client_without_fallback(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setitem(sys.modules, "scripts.free_asr_client", SimpleNamespace(
        extract_audio_mp3=lambda path: seen.append(path) or b"audio",
        transcribe_bcut=lambda sound: seen.append(sound) or {"utterances": [
            {"start_time": 12, "end_time": 30, "transcript": "内容"}]},
    ))
    ref = cc.RecordingRef("r.wav", "0" * 64, 1000, None)
    path = tmp_path / "r.wav"
    assert cli.transcribe_bcut(ref, path) == (cc.CensusTranscriptCue(12, 30, "内容"),)
    assert seen == [path, b"audio"]





def test_template_config_and_no_fingerprint_registration(tmp_path):
    config = json.loads((ROOT / 'profiles/_template/channel_census_config.v1.json').read_text())
    assert cc.parse_census_config(config, profile_id='REPLACE_ME').audience_interests == ()
    doc = json.loads((ROOT / "profiles/_template/profile.json").read_text())
    assert not {cc.CENSUS_CLUES_ASSET_KEY, cc.CENSUS_CONFIG_ASSET_KEY} & set(
        doc["assets"]["pipeline_fingerprint_files"])
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(doc))
    profile = load_channel_profile(tmp_path, manifest_path=path, environ={})
    for key in (cc.CENSUS_CLUES_ASSET_KEY, cc.CENSUS_CONFIG_ASSET_KEY):
        assert profile.asset_file(key) not in profile.missing_runtime_paths()
        assert profile.asset_file(key) not in profile.fingerprint_paths()
    assert profile.asset_file("persona") in profile.missing_runtime_paths()
    for key in ("selection_census_clues", "persona_facts"):
        changed = json.loads(json.dumps(doc))
        changed["assets"]["files"][key] = f"{key}.json"
        changed["assets"]["pipeline_fingerprint_files"].append(key)
        path = tmp_path / "bad-profile.json"
        path.write_text(json.dumps(changed))
        with pytest.raises(ChannelProfileError, match="MUST_NOT_BE_FINGERPRINTED"):
            load_channel_profile(ROOT, manifest_path=path, environ={})


def test_profile_validator_reports_lint_without_changing_ready_semantics(tmp_path):
    command, env, assets = setup_case(tmp_path)
    script = str(Path(command[2]).with_name("validate_channel_profile.py"))
    profile = str(tmp_path / "profile.json")
    check = [sys.executable, "-B", script, "--manifest", profile, "--config-only"]
    result = execute(check, env)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "READY" and payload["census_config_lint"] == "VALID"
    (assets / "metric.md").write_text(METRIC.replace("数学、游戏", "游戏"))
    result = execute(check, env)
    assert result.returncode == 0
    assert json.loads(result.stdout)["census_config_lint"].startswith("INVALID:")
