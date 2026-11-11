"""Controlled no-native-task code continuation preserves original invocation."""

import json

import pytest

from scripts import subtitle_regression_eval as cli
from src.autoslice.argon_escalation_contract import canonical
from tests.test_argon_s2_joint_runtime import _execution_args, _argv, _saved_bytes
from tests.smoke_common_runtime import scenario

pytest_plugins = ["tests.test_argon_s2_joint_runtime"]


def test_unmocked_normal_cli_code_successor_does_not_reset_or_manufacture_progress(tmp_path):
    result = scenario(
        tmp_path / "successor", 0o022, "synthetic-previous-code", exercise_successor=True
    )
    assert result["status"] == "PASS" and result["original_invocation_preserved"]
    assert not result["code_only_revision_forced_new_generation"]


@pytest.mark.parametrize(
    "blocked",
    [
        "unknown_invocation",
        "native_handle",
        "reservation",
        "wrong_parent",
        "grant_drift",
        "runtime_drift",
    ],
)
def test_public_code_successor_refuses_unknown_native_task_or_campaign_change(
    scene, plan_args, monkeypatch, blocked
):
    args = _execution_args(plan_args, scene.root / "output")

    def run(package, out, code, environment, **options):
        out.mkdir(exist_ok=options["resume"])
        raw = json.loads((plan_args.prepare_argon_comparison / "sample/run.json").read_bytes())
        (out / "final.srt").write_text(scene.srt)
        from src.autoslice.argon_escalation_contract import sha

        raw["final_srt_sha256"] = sha(scene.srt.encode())
        (out / "run.json").write_bytes(canonical(raw))
        (out / "authority.json").write_bytes(b"{}")
        return raw

    monkeypatch.setattr(cli, "run_package", run)
    assert cli.main(_argv(args)) == 1
    original = args.code_identity
    args.resume_comparison = True
    args.code_identity = "synthetic-repaired-code"
    predecessor = original
    if blocked == "unknown_invocation":
        (args.output / "invocations/unresolved.started.json").write_bytes(b"{}")
    elif blocked == "native_handle":
        p = args.output / "runtime-state/cache/argon-consumer/target/batch/attempt-1/handle.json"
        p.parent.mkdir(parents=True)
        p.write_bytes(b"{}")
    elif blocked == "reservation":
        p = plan_args.output / "cost-reservations/target.json"
        p.parent.mkdir()
        p.write_bytes(b'{"status":"RESERVED_OR_OUTCOME_UNKNOWN"}')
    elif blocked == "wrong_parent":
        predecessor = "unrelated-old-code"
    elif blocked == "grant_drift":
        raw = json.loads(args.comparison_grant.read_bytes())
        raw["authority"] += "-changed"
        args.comparison_grant.write_bytes(canonical(raw))
    else:
        args.runtime_root = str(scene.root / "different-provider-runtime")
    before = _saved_bytes(args.output)
    with pytest.raises(ValueError):
        cli.main([*_argv(args), "--comparison-code-successor", predecessor])
    assert _saved_bytes(args.output) == before and not scene.provider.calls
