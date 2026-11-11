"""Explicit environment survives sudo/exec boundaries without inheriting ambient env."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from scripts import continuation_authority_preflight as preflight
from scripts import predeploy_contract_gauntlet as gauntlet
from src.autoslice import whole_path_observation as observation

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("assignment", [
    "PATH=/tmp", "LD_PRELOAD=/tmp/x", "AUTOSLICE_x=y", "AUTOSLICE_=y", "NO_EQUALS",
    "AUTOSLICE_FILE=/opt/bilive/.env", "LIDOUSHA_FILE=/opt/bilive/autoslice/cpa.env",
    'AUTOSLICE_JSON={"path":"/x/credentials.json"}', "LIDOUSHA_FILE=/x/.ssh/id_rsa",
    "LIDOUSHA_FILE=/x/application_default_credentials.json", "LIDOUSHA_FILE=/x/service-account.json",
    "LIDOUSHA_FILE=/x/token.json", "LIDOUSHA_FILE=/x/id_ed25519",
    "AUTOSLICE_BASE=/tmp/escape", "AUTOSLICE_FINAL_REVIEW_RUNTIME_ROOT=/tmp/escape",
])
def test_invalid_environment_rejected_by_cli_before_sandbox(tmp_path, monkeypatch, assignment):
    monkeypatch.setattr(sys, "argv", ["gauntlet", "--phase", "continuation", "--state", str(tmp_path),
                                      "--sandbox-env", assignment])
    monkeypatch.setattr(gauntlet.subprocess, "run", lambda *a, **k: pytest.fail("sandbox launched"))
    with pytest.raises(SystemExit) as exc:
        gauntlet.main()
    assert exc.value.code == 2


@pytest.mark.parametrize("phase", ["continuation", "whole-path"])
@pytest.mark.parametrize("options,expected", [
    ([], []),
    (["--final-review-mode", "per_finding"], ["AUTOSLICE_FINAL_REVIEW_MODE=per_finding"]),
    (["--sandbox-env", 'AUTOSLICE_PROFILE_BINDING={"path":"/a b/profile.json"}',
      "--sandbox-env", "LIDOUSHA_TERM_AS_OF=2026-10-24"],
     ['AUTOSLICE_PROFILE_BINDING={"path":"/a b/profile.json"}', "LIDOUSHA_TERM_AS_OF=2026-10-24"]),
])
def test_gauntlet_passes_only_explicit_assignments_as_argv(tmp_path, monkeypatch, phase, options, expected):
    slices = tmp_path / "slices.txt"
    slices.write_text("/in/cid/SPEC.json existing_srt\n")
    seen = []

    def fake_run(command, **kwargs):
        seen.extend(command)
        work = Path(command[9]) / "reports"
        work.mkdir(parents=True)
        (work / "cid.json").write_text(json.dumps({"candidate_id": "cid", "outcome": "REACHED_GATE",
            "sandbox_env_keys": [item.split("=", 1)[0] for item in expected],
            "final_review_mode": "per_finding" if options[:1] == ["--final-review-mode"] else "legacy"}))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setenv("AUTOSLICE_FINAL_REVIEW_MODE", "ambient-must-not-cross-sudo")
    monkeypatch.setattr(gauntlet.subprocess, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["gauntlet", "--phase", phase, "--state", str(tmp_path / "state"),
                                      "--slices", str(slices), *options])
    assert gauntlet.main() == 0
    assert [seen[i + 1] for i, word in enumerate(seen) if word == "--sandbox-env"] == expected
    summary = json.loads((tmp_path / "state" / f"{phase}.json").read_text())[0]
    assert summary["sandbox_env_keys"] == [item.split("=", 1)[0] for item in expected]
    assert "ambient-must-not-cross-sudo" not in seen


@pytest.mark.parametrize("assignment", [
    'AUTOSLICE_PROFILE_BINDING={"x":"a b=c"}',
    "LIDOUSHA_STARTUP_PAYLOAD=",
    "LIDOUSHA_STARTUP_PAYLOAD=public secret story\n雪=ok\r",
    "LIDOUSHA_STARTUP_PAYLOAD=" + os.fsdecode(b"\xff=opaque\n"),
])
def test_shared_validator_nul_protocol_preserves_startup_values(assignment):
    result = subprocess.run([sys.executable, "-I", "-B", str(ROOT / "scripts/continuation_authority_preflight.py"),
                             "--validate-sandbox-env", assignment], capture_output=True, check=True)
    name = assignment.split("=", 1)[0]
    assert result.stdout.split(b"\0") == [os.fsencode(assignment),
        ("CONTINUATION_PREFLIGHT_SANDBOX_ENV_KEYS=" + json.dumps([name])).encode(), b""]


def test_default_validator_output_is_empty_and_direct_name_rejection_has_code():
    default = subprocess.run([sys.executable, "-I", "-B", str(ROOT / "scripts/continuation_authority_preflight.py"),
                              "--validate-sandbox-env"], capture_output=True, check=True)
    assert default.stdout == b""
    bad = subprocess.run([sys.executable, "-I", "-B", str(ROOT / "scripts/continuation_authority_preflight.py"),
                          "--validate-sandbox-env", "PATH=evil"], capture_output=True)
    assert bad.returncode == 2 and bad.stdout == b"" and b"SANDBOX_ENV_NAME_INVALID" in bad.stderr


def test_explicit_runtime_roots_can_only_repeat_the_fixed_sandbox_root():
    expected = {name: "/opt/bilive/autoslice" for name in
                ["AUTOSLICE_BASE", "AUTOSLICE_FINAL_REVIEW_RUNTIME_ROOT"]}
    assert preflight.validate_sandbox_env([name + "=" + value for name, value in expected.items()]) == expected


def test_empty_explicit_mode_preserves_the_production_legacy_default(monkeypatch):
    assert preflight.validate_sandbox_env(["AUTOSLICE_FINAL_REVIEW_MODE="]) == {"AUTOSLICE_FINAL_REVIEW_MODE": ""}
    monkeypatch.setenv("AUTOSLICE_FINAL_REVIEW_MODE", "")
    monkeypatch.delenv("CONTINUATION_PREFLIGHT_SANDBOX_ENV_KEYS", raising=False)
    assert preflight.sandbox_environment_report()["final_review_mode"] == "legacy"


def test_mode_sugar_retains_its_declared_domain(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["gauntlet", "--phase", "continuation", "--state", str(tmp_path),
                                      "--final-review-mode", "typo"])
    with pytest.raises(SystemExit) as exc:
        gauntlet.main()
    assert exc.value.code == 2


def test_conflicting_mode_sugar_is_rejected():
    with pytest.raises(ValueError, match="conflicting"):
        preflight.validate_sandbox_env(["AUTOSLICE_FINAL_REVIEW_MODE=legacy", "AUTOSLICE_FINAL_REVIEW_MODE=per_finding"])


@pytest.mark.parametrize("reporter", [preflight, observation])
def test_reports_only_effective_explicit_key_names(tmp_path, monkeypatch, reporter):
    monkeypatch.setenv("CONTINUATION_PREFLIGHT_SANDBOX_ENV_KEYS", '["AUTOSLICE_FINAL_REVIEW_MODE", "LIDOUSHA_GONE"]')
    monkeypatch.setenv("AUTOSLICE_FINAL_REVIEW_MODE", "per_finding")
    monkeypatch.setenv("LIDOUSHA_AMBIENT", "must-not-appear")
    monkeypatch.delenv("LIDOUSHA_GONE", raising=False)
    monkeypatch.setattr(reporter, "isolation_problems", lambda: ["fixture"])
    report = reporter.run_one(argparse.Namespace(spec=tmp_path / "spec", code_root=ROOT))
    assert report["final_review_mode"] == "per_finding"
    assert report["sandbox_env_keys"] == ["AUTOSLICE_FINAL_REVIEW_MODE"]
    assert "must-not-appear" not in json.dumps(report)


def test_default_report_and_whole_path_summary(monkeypatch):
    monkeypatch.delenv("CONTINUATION_PREFLIGHT_SANDBOX_ENV_KEYS", raising=False)
    monkeypatch.delenv("AUTOSLICE_FINAL_REVIEW_MODE", raising=False)
    metadata = preflight.sandbox_environment_report()
    assert metadata == {"sandbox_env_keys": [], "final_review_mode": "legacy"}
    assert observation.summary_row(metadata)["sandbox_env_keys"] == []


def test_canary_branch_remains_identical_to_baseline():
    relative = "scripts/continuation_authority_preflight_sandbox.sh"
    current = (ROOT / relative).read_text()
    branch = current.split('if [ "$SLICES" = --canary ]; then', 1)[1].split("\nfi", 1)[0]
    # Frozen from 674b87490; works in the CI archive, which has no .git directory.
    assert hashlib.sha256(branch.encode()).hexdigest() == "29403856154e5fa1741968ac3c6076477a6262ef26b53a09b3155f88c344fac5"
