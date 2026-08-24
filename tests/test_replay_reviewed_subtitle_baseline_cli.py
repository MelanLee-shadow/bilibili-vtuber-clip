from __future__ import annotations

import hashlib
import json
import subprocess
import threading
import venv as venv_module
from types import SimpleNamespace
from pathlib import Path

import pytest

from scripts import replay_reviewed_subtitle_baseline as cli
from scripts.build_lidousha_daily_review_manifest import DailyManifestError
from src.autoslice.branding_intro import BrandingIntroError
from src.autoslice.producer_speaker import SpeakerFinalizationBlockedError
from src.autoslice.fastlane_c2_private_authority import classify_c2_private_path_unavailable


def test_success_matrix_has_unique_terminal_predicates() -> None:
    plan = SimpleNamespace(matrix=(
        {"predicate": "SPEAKER_ASS_BURN_REBUILD", "status": "PENDING_STAGE"},
        {"predicate": "TITLE_COVER_PRECONDITIONS", "status": "PENDING_STAGE"},
        {"predicate": "FINAL_REVIEW_AND_PACKAGE_AUDIT", "status": "PENDING_STAGE"},
        {"predicate": "UPLOAD_ALLOWED", "status": "PASS_FALSE"},
    ))
    matrix = cli._matrix(plan, status="PASS")
    assert len({row["predicate"] for row in matrix}) == len(matrix)
    assert all(row["status"] not in {"PENDING", "PENDING_STAGE", "BLOCK"} for row in matrix)
    assert matrix[-1] == {"predicate": "UPLOAD_ALLOWED", "status": "PASS_FALSE"}


def test_failure_matrix_keeps_independent_gates_distinct() -> None:
    plan = SimpleNamespace(matrix=(
        {"predicate": "SEALED_BASELINE", "status": "PASS"},
        {"predicate": "COVER_ROUTE_PIXEL_HOST_PARTICIPANT_PUNCH", "status": "PENDING_STAGE"},
    ))
    matrix = {row["predicate"]: row for row in cli._matrix(
        plan, status="NOT_EVALUATED",
        failures={"SOURCE_FACT_REVIEW": "REPLAY_SOURCE_FACT_FAILED", "PACKAGE_AUDIT": "REPLAY_AUDIT_FAILED"},
    )}
    assert matrix["SEALED_BASELINE"]["status"] == "PASS"
    assert matrix["COVER_ROUTE_PIXEL_HOST_PARTICIPANT_PUNCH"]["status"] == "NOT_EVALUATED"
    assert matrix["SOURCE_FACT_REVIEW"] == {
        "predicate": "SOURCE_FACT_REVIEW", "status": "FAIL", "reason_code": "REPLAY_SOURCE_FACT_FAILED",
    }
    assert matrix["PACKAGE_AUDIT"] == {
        "predicate": "PACKAGE_AUDIT", "status": "FAIL", "reason_code": "REPLAY_AUDIT_FAILED",
    }


def test_prepare_converts_finalizer_system_exit_to_per_candidate_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = tmp_path / "record.json"
    record.write_text("{}\n")
    baseline = SimpleNamespace(config={"sha256": "a" * 64})
    plan = SimpleNamespace(date="2026-08-14", candidate_id="cid", record_path=record, baseline=baseline)
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    monkeypatch.setattr(cli, "stage_replay", lambda *_a, **_kw: (_ for _ in ()).throw(SystemExit(2)))
    with pytest.raises(cli._PrepareFailure) as caught:
        cli._prepare(plan, runtime=tmp_path, stage_parent=parent)
    assert caught.value.provider_attempted is False
    assert caught.value.reason_code == "REPLAY_PREPARE_SYSTEM_EXIT"


def test_prepare_retains_typed_stage_reason_in_sanitized_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    record = tmp_path / "record.json"
    record.write_text("{}\n")
    baseline = SimpleNamespace(config={"sha256": "a" * 64})
    plan = SimpleNamespace(date="2026-08-14", candidate_id="cid", record_path=record, baseline=baseline)
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    monkeypatch.setattr(
        cli, "stage_replay",
        lambda *_a, **_kw: (_ for _ in ()).throw(cli.ReviewedBaselineReplayError("REPLAY_BASELINE_APPLICATION_FAILED")),
    )
    with pytest.raises(cli._PrepareFailure) as caught:
        cli._prepare(plan, runtime=tmp_path, stage_parent=parent)
    assert caught.value.provider_attempted is False
    assert caught.value.reason_code == "REPLAY_BASELINE_APPLICATION_FAILED"


def test_prepare_records_only_an_actual_provider_callback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    record = tmp_path / "record.json"
    record.write_text("{}\n")
    plan = SimpleNamespace(
        date="2026-08-14", candidate_id="cid", record_path=record,
        baseline=SimpleNamespace(config={"sha256": "a" * 64}),
    )
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    stage = parent / "candidate-stage"
    stage.mkdir()
    (stage / "stage.json").write_text("{}\n")
    monkeypatch.setattr(cli, "stage_replay", lambda *_args, **_kwargs: {"stage": str(stage)})
    monkeypatch.setattr(cli, "_production_llm_call", lambda **_kwargs: lambda _prompt: "{}")

    def provider_failure(*_args, **kwargs):
        kwargs["provider_invocation"]()
        raise cli.ReviewedBaselineReplayError("REPLAY_FINALIZER_CHAT_AUTHORITY_DRIFT")

    monkeypatch.setattr(cli, "synthesize_replay_spec_and_finalize_private", provider_failure)
    with pytest.raises(cli._PrepareFailure) as caught:
        cli._prepare(plan, runtime=tmp_path, stage_parent=parent)
    assert caught.value.provider_attempted is True
    assert caught.value.reason_code == "REPLAY_FINALIZER_CHAT_AUTHORITY_DRIFT"


def test_prepare_c2_path_diagnostic_is_closed_and_replaces_only_missing_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = tmp_path / "record.json"
    record.write_text("{}\n")
    plan = SimpleNamespace(
        date="2026-08-14", candidate_id="cid", record_path=record,
        baseline=SimpleNamespace(config={"sha256": "a" * 64}),
    )
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    stage = parent / "candidate-stage"
    stage.mkdir()
    (stage / "stage.json").write_text("{}\n")
    monkeypatch.setattr(cli, "stage_replay", lambda *_args, **_kwargs: {"stage": str(stage)})
    monkeypatch.setattr(cli, "_production_llm_call", lambda **_kwargs: lambda _prompt: "{}")

    def missing_path(*_args: object, **_kwargs: object) -> object:
        return cli.regular_binding(
            tmp_path / "token=secret" / "provider-response" / "missing.json",
            label="PROVENANCE",
        )

    monkeypatch.setattr(cli, "synthesize_replay_spec_and_finalize_private", missing_path)
    with pytest.raises(cli._PrepareFailure) as caught:
        cli._prepare(
            plan, runtime=tmp_path, stage_parent=parent,
            path_unavailable_diagnostic=classify_c2_private_path_unavailable,
        )
    failure = caught.value
    assert failure.reason_code == "C2_PRIVATE_REPLAY_PATH_UNAVAILABLE_REGULAR_PROVENANCE_PARENT"
    assert failure.predicate_failures == (("PRIVATE_FINALIZATION", failure.reason_code),)
    rendered = json.dumps(vars(failure), sort_keys=True)
    assert "token=secret" not in rendered
    assert "provider-response" not in rendered
    assert str(tmp_path) not in rendered
    assert not stage.exists()


def test_prepare_c2_path_diagnostic_rejects_untrusted_callback_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = tmp_path / "record.json"
    record.write_text("{}\n")
    plan = SimpleNamespace(
        date="2026-08-14", candidate_id="cid", record_path=record,
        baseline=SimpleNamespace(config={"sha256": "a" * 64}),
    )
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    stage = parent / "candidate-stage"
    stage.mkdir()
    (stage / "stage.json").write_text("{}\n")
    monkeypatch.setattr(cli, "stage_replay", lambda *_args, **_kwargs: {"stage": str(stage)})
    monkeypatch.setattr(cli, "_production_llm_call", lambda **_kwargs: lambda _prompt: "{}")

    def missing_path(*_args: object, **_kwargs: object) -> object:
        return cli.regular_binding(tmp_path / "missing" / "record.json", label="PROVENANCE")

    monkeypatch.setattr(cli, "synthesize_replay_spec_and_finalize_private", missing_path)
    with pytest.raises(cli._PrepareFailure) as caught:
        cli._prepare(
            plan, runtime=tmp_path, stage_parent=parent,
            path_unavailable_diagnostic=lambda _exc: "C2_PRIVATE_REPLAY_PATH_UNAVAILABLE_TOKEN_SECRET",
        )
    assert caught.value.reason_code == "C2_PRIVATE_REPLAY_PATH_UNAVAILABLE_CLASSIFIER_INVALID"
    assert caught.value.predicate_failures == (("PRIVATE_FINALIZATION", caught.value.reason_code),)
    assert not stage.exists()


def test_prepare_binds_pinned_speaker_runtime_not_controller_python(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = tmp_path / "record.json"
    record.write_text("{}\n")
    plan = SimpleNamespace(
        date="2026-08-14", candidate_id="cid", record_path=record,
        baseline=SimpleNamespace(config={"sha256": "a" * 64}),
    )
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    stage = parent / "candidate-stage"
    stage.mkdir()
    (stage / "stage.json").write_text("{}\n")
    monkeypatch.setattr(cli, "stage_replay", lambda *_args, **_kwargs: {"stage": str(stage)})
    monkeypatch.setattr(cli, "_production_llm_call", lambda **_kwargs: lambda _prompt: "{}")
    captured: dict[str, object] = {}

    def capture_runtime(*_args, **kwargs):
        captured["speaker_python"] = kwargs["speaker_python"]
        raise cli.ReviewedBaselineReplayError("REPLAY_FINALIZER_CHAT_AUTHORITY_DRIFT")

    monkeypatch.setattr(cli, "synthesize_replay_spec_and_finalize_private", capture_runtime)
    with pytest.raises(cli._PrepareFailure):
        cli._prepare(plan, runtime=tmp_path, stage_parent=parent)
    assert captured["speaker_python"] == tmp_path / "venv-diar/bin/python"


def test_prepare_binds_explicit_private_preflight_interpreter_without_losing_venv_launcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = tmp_path / "record.json"
    record.write_text("{}\n")
    plan = SimpleNamespace(date="2026-08-14", candidate_id="cid", record_path=record,
                           baseline=SimpleNamespace(config={"sha256": "a" * 64}))
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    stage = parent / "candidate-stage"
    stage.mkdir()
    (stage / "stage.json").write_text("{}\n")
    interpreter = tmp_path / "interpreter"
    interpreter.write_text("#!/bin/sh\nexit 0\n")
    interpreter.chmod(0o700)
    requested = tmp_path / "venv-python"
    requested.symlink_to(interpreter)
    monkeypatch.setattr(cli, "stage_replay", lambda *_args, **_kwargs: {"stage": str(stage)})
    monkeypatch.setattr(cli, "_stage_speaker_python_revalidator", lambda *_args: lambda: None)
    monkeypatch.setattr(cli, "_production_llm_call", lambda **_kwargs: lambda _prompt: "{}")
    captured: dict[str, object] = {}

    def capture_runtime(*_args, **kwargs):
        captured["speaker_python"] = kwargs["speaker_python"]
        raise cli.ReviewedBaselineReplayError("REPLAY_FINALIZER_CHAT_AUTHORITY_DRIFT")

    monkeypatch.setattr(cli, "synthesize_replay_spec_and_finalize_private", capture_runtime)
    with pytest.raises(cli._PrepareFailure):
        cli._prepare(plan, runtime=tmp_path, stage_parent=parent, speaker_python=requested)
    # The resolved regular target is hash-bound, but the venv launcher itself
    # must be invoked so Python retains its virtualenv site-packages.
    assert captured["speaker_python"] == requested.absolute()


def _sealed_speaker_binding_stage(tmp_path: Path, document: dict[str, object]) -> Path:
    stage = tmp_path / "stage"
    stage.mkdir(mode=0o700)
    binding_path = stage / "speaker-python-binding.json"
    binding_path.write_bytes(cli._canon(document))
    binding_path.chmod(0o600)
    stage_document = stage / "stage.json"
    stage_document.write_bytes(cli._canon({
        "speaker_python_binding": {
            "path": binding_path.name,
            "sha256": cli.regular_binding(binding_path, label="SPEAKER_PYTHON_BINDING").sha256,
        },
    }))
    stage_document.chmod(0o600)
    return stage


def test_speaker_python_binding_rejects_launcher_swap(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    for target in (first, second):
        target.write_text("#!/bin/sh\nexit 0\n")
        target.chmod(0o700)
    requested = tmp_path / "venv-python"
    requested.symlink_to(first)
    document = cli._speaker_python_binding(requested)
    stage = _sealed_speaker_binding_stage(tmp_path, document)
    verifier = cli._stage_speaker_python_revalidator(stage, document)
    requested.unlink()
    requested.symlink_to(second)
    with pytest.raises(cli.ReviewedBaselineReplayError, match="SPEAKER_PYTHON_BINDING_DRIFT"):
        verifier()


def test_speaker_python_binding_rejects_resolved_target_drift(tmp_path: Path) -> None:
    target = tmp_path / "interpreter"
    target.write_text("#!/bin/sh\nexit 0\n")
    target.chmod(0o700)
    requested = tmp_path / "venv-python"
    requested.symlink_to(target)
    document = cli._speaker_python_binding(requested)
    stage = _sealed_speaker_binding_stage(tmp_path, document)
    verifier = cli._stage_speaker_python_revalidator(stage, document)
    target.write_text("#!/bin/sh\necho changed\n")
    target.chmod(0o700)
    with pytest.raises(cli.ReviewedBaselineReplayError, match="SPEAKER_PYTHON_BINDING_DRIFT"):
        verifier()


def test_speaker_python_binding_rejects_pyvenv_drift(tmp_path: Path) -> None:
    venv = tmp_path / "venv"
    launcher_dir = venv / "bin"
    launcher_dir.mkdir(parents=True)
    pyvenv = venv / "pyvenv.cfg"
    pyvenv.write_text("home = /trusted\n")
    target = tmp_path / "interpreter"
    target.write_text("#!/bin/sh\nexit 0\n")
    target.chmod(0o700)
    requested = launcher_dir / "python"
    requested.symlink_to(target)
    document = cli._speaker_python_binding(requested)
    stage = _sealed_speaker_binding_stage(tmp_path, document)
    verifier = cli._stage_speaker_python_revalidator(stage, document)
    pyvenv.write_text("home = /changed\n")
    with pytest.raises(cli.ReviewedBaselineReplayError, match="SPEAKER_PYTHON_BINDING_DRIFT"):
        verifier()


def test_speaker_python_binding_keeps_venv_launcher_prefix(tmp_path: Path) -> None:
    venv = tmp_path / "venv"
    # Symlinking is the portable venv layout for the uv Python distributed on
    # macOS: copying its executable loses the adjacent shared library.
    venv_module.EnvBuilder(with_pip=False, symlinks=True).create(venv)
    requested = venv / "bin" / "python"
    document = cli._speaker_python_binding(requested)
    stage = _sealed_speaker_binding_stage(tmp_path, document)
    cli._stage_speaker_python_revalidator(stage, document)()
    assert subprocess.check_output(
        [str(requested), "-c", "import sys; print(sys.prefix)"], text=True,
    ).strip() == str(venv)


def test_speaker_python_revalidator_rereads_stage_seal_at_invocation(tmp_path: Path) -> None:
    target = tmp_path / "interpreter"
    target.write_text("#!/bin/sh\nexit 0\n")
    target.chmod(0o700)
    requested = tmp_path / "venv-python"
    requested.symlink_to(target)
    document = cli._speaker_python_binding(requested)
    stage = _sealed_speaker_binding_stage(tmp_path, document)
    verifier = cli._stage_speaker_python_revalidator(stage, document)
    (stage / "speaker-python-binding.json").write_text('{"changed":true}')
    (stage / "speaker-python-binding.json").chmod(0o600)
    with pytest.raises(cli.ReviewedBaselineReplayError, match="SPEAKER_PYTHON_STAGE_BINDING_DRIFT"):
        verifier()


@pytest.mark.parametrize(
    "mode",
    [
        ["--plan"],
        ["--apply", "--private-stage-parent", "/private/stage"],
        ["--readiness-graph"],
    ],
)
def test_main_refuses_speaker_override_outside_private_full_dry(mode: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    argv = [*mode, "--runtime-root", "/private/runtime", "--date", "2026-08-14",
            "--speaker-python", "/private/venv/bin/python"]
    if "--readiness-graph" not in mode:
        argv.extend(["--candidate-id", "cid"])
    assert cli.main(argv) == 2
    assert json.loads(capsys.readouterr().out)["reason_code"] == "REPLAY_SPEAKER_PYTHON_PRIVATE_DRY_RUN_ONLY"


def test_safe_reason_code_keeps_only_typed_codes() -> None:
    assert cli._safe_reason_code(cli.ReviewedBaselineReplayError("REPLAY_BASELINE_APPLICATION_FAILED")) == (
        "REPLAY_BASELINE_APPLICATION_FAILED"
    )
    assert cli._safe_reason_code(RuntimeError("/private/provider stderr: token=secret")) == (
        "REPLAY_PREPARE_EXCEPTION"
    )
    assert cli._safe_reason_code(
        SpeakerFinalizationBlockedError(
            "SPEAKER_FINALIZATION_BLOCKED: token=secret /private/provider stderr"
        )
    ) == "SPEAKER_FINALIZATION_BLOCKED"
    assert cli._safe_reason_code(
        BrandingIntroError("no verified intro candidate in branding context")
    ) == "REPLAY_BRANDING_AUTHORITY_BLOCKED"
    assert cli._safe_reason_code(
        cli.ReviewedBaselineReplayError(
            "REPLAY_PRIVATE_MANIFEST_SPEAKER_GUESS_REQUIRES_HUMAN_REVIEW"
        )
    ) == "REPLAY_PRIVATE_MANIFEST_SPEAKER_GUESS_REQUIRES_HUMAN_REVIEW"
    assert cli._failure_predicate(
        "REPLAY_FROZEN_TITLE_AUTHORITY_DRIFT_"
        "SOURCE_FACT_REVIEW_SPEAKER_GUESS_REQUIRES_HUMAN_REVIEW"
    ) == "SOURCE_FACT_REVIEW"
    assert DailyManifestError(
        "speaker evidence rejected", reason_code="SPEAKER_GUESS_REQUIRES_HUMAN_REVIEW"
    ).reason_code == "SPEAKER_GUESS_REQUIRES_HUMAN_REVIEW"


def test_safe_reason_code_extracts_only_allowlisted_system_exit_prefix() -> None:
    assert cli._safe_reason_code(
        SystemExit("FINAL_REVIEW_RELEASE_BLOCKED: /private/secret: provider stderr")
    ) == "FINAL_REVIEW_RELEASE_BLOCKED"
    assert cli._safe_reason_code(SystemExit(23)) == "REPLAY_PREPARE_SYSTEM_EXIT"
    assert cli._safe_reason_code(SystemExit("UNTRUSTED_PROVIDER_SECRET: token=abc")) == (
        "REPLAY_PREPARE_SYSTEM_EXIT"
    )


def test_generic_prepare_exception_keeps_only_internal_locus_and_no_message(tmp_path: Path) -> None:
    def internal_value_error() -> None:
        raise ValueError("token=secret /private/stage provider stderr")

    try:
        internal_value_error()
    except ValueError as exc:
        failure = cli._prepare_failure(exc=exc)
    diagnostic = failure.exception_diagnostic
    assert diagnostic is not None
    assert diagnostic["exception_type"] == "ValueError"
    assert diagnostic["exception_locus"]["module"] == "tests.test_replay_reviewed_subtitle_baseline_cli"
    assert diagnostic["exception_locus"]["function"] == "internal_value_error"
    assert "token=secret" not in json.dumps(diagnostic, sort_keys=True)
    assert "/private/stage" not in json.dumps(diagnostic, sort_keys=True)

    runtime = tmp_path / "runtime"
    repo = runtime / "repo"
    repo.mkdir(parents=True)
    (repo / "DEPLOYED_COMMIT").write_text("a" * 40 + "\n")
    (repo / "DEPLOYED_AUTHORITY_MANIFEST.json").write_text("{}\n")
    plan = SimpleNamespace(
        date="2026-08-14", candidate_id="cid", expected_video_sha256="sha256:" + "b" * 64,
        baseline=SimpleNamespace(config={"sha256": "c" * 64}),
    )
    receipt_sha = cli._sanitized_failure_receipt(
        runtime=runtime, plan=plan, matrix=[], provider_attempted=False,
        exception_diagnostic=diagnostic,
    )
    receipt = next((runtime / "reports").rglob(receipt_sha.removeprefix("sha256:") + ".json"))
    payload = receipt.read_text()
    assert json.loads(payload)["exception_diagnostic"] == diagnostic
    assert "token=secret" not in payload
    assert "/private/stage" not in payload


def test_generic_exception_drops_external_locus_and_typed_errors_stay_unchanged() -> None:
    namespace: dict[str, object] = {}
    exec(compile(
        "def capture():\n    try:\n        raise ValueError('secret outside repository')\n    except ValueError as exc:\n        return exc\n",
        "/tmp/untrusted-provider.py", "exec",
    ), namespace)
    external = namespace["capture"]()
    assert isinstance(external, ValueError)
    assert cli._generic_exception_diagnostic(external) == {"exception_type": "ValueError"}
    typed = cli._prepare_failure(exc=cli.ReviewedBaselineReplayError("REPLAY_BASELINE_APPLICATION_FAILED"))
    assert typed.exception_diagnostic is None


@pytest.mark.parametrize("code", [23, "token=secret /private/stage provider prompt"])
def test_opaque_system_exit_keeps_only_repo_locus_and_stable_secret_free_receipt(
    tmp_path: Path, code: object,
) -> None:
    def local_exit() -> None:
        raise SystemExit(code)

    try:
        local_exit()
    except SystemExit as exc:
        failure = cli._prepare_failure(exc=exc)
    diagnostic = failure.exception_diagnostic
    assert diagnostic is not None
    assert diagnostic["exception_type"] == "SystemExit"
    assert diagnostic["exception_locus"]["module"] == "tests.test_replay_reviewed_subtitle_baseline_cli"
    assert diagnostic["exception_locus"]["function"] == "local_exit"
    serialized = json.dumps(diagnostic, sort_keys=True)
    assert "token=secret" not in serialized
    assert "/private/stage" not in serialized

    runtime = tmp_path / "runtime"
    repo = runtime / "repo"
    repo.mkdir(parents=True)
    (repo / "DEPLOYED_COMMIT").write_text("a" * 40 + "\n")
    (repo / "DEPLOYED_AUTHORITY_MANIFEST.json").write_text("{}\n")
    plan = SimpleNamespace(
        date="2026-08-14", candidate_id="cid", expected_video_sha256="sha256:" + "b" * 64,
        baseline=SimpleNamespace(config={"sha256": "c" * 64}),
    )
    first = cli._sanitized_failure_receipt(
        runtime=runtime, plan=plan, matrix=[], provider_attempted=False,
        exception_diagnostic=diagnostic,
    )
    second = cli._sanitized_failure_receipt(
        runtime=runtime, plan=plan, matrix=[], provider_attempted=False,
        exception_diagnostic=diagnostic,
    )
    assert first == second
    receipt = next((runtime / "reports").rglob(first.removeprefix("sha256:") + ".json"))
    receipt_text = receipt.read_text()
    assert json.loads(receipt_text)["exception_diagnostic"] == diagnostic
    assert "token=secret" not in receipt_text
    assert "/private/stage" not in receipt_text


def test_system_exit_diagnostic_drops_external_only_traceback_and_nonopaque_exit() -> None:
    namespace: dict[str, object] = {}
    exec(compile(
        "def capture():\n    try:\n        raise SystemExit('token=secret /private/provider')\n    except SystemExit as exc:\n        return exc\n",
        "/tmp/untrusted-provider.py", "exec",
    ), namespace)
    external = namespace["capture"]()
    assert isinstance(external, SystemExit)
    assert cli._system_exit_diagnostic(external) == {"exception_type": "SystemExit"}
    assert cli._system_exit_diagnostic(SystemExit("FINAL_REVIEW_RELEASE_BLOCKED: token=secret")) is None


@pytest.mark.parametrize(
    "locus",
    [
        {"module": "not-a-module", "function": "f", "line": 1},
        {"module": "m" * 241, "function": "f", "line": 1},
        {"module": "tests.ok", "function": "f", "line": 0},
    ],
)
def test_closed_system_exit_diagnostic_refuses_malformed_or_oversize_locus(locus: dict[str, object]) -> None:
    assert cli._closed_exception_diagnostic({
        "exception_type": "SystemExit", "exception_locus": locus,
    }) is None


def test_record_bound_authority_failures_get_their_own_predicate() -> None:
    assert cli._failure_predicate("REPLAY_FINALIZER_CHAT_AUTHORITY_DRIFT") == "RECORD_BOUND_CHAT_AUTHORITY"
    assert cli._failure_predicate("REPLAY_FINALIZER_CLIP_CONTEXT_PAYLOAD_DRIFT") == "RECORD_BOUND_CLIP_CONTEXT"


@pytest.mark.parametrize(
    ("reason", "predicate"),
    [
        (
            "REPLAY_FROZEN_TITLE_AUTHORITY_DRIFT_SOURCE_FACT_REVIEW",
            "SOURCE_FACT_REVIEW",
        ),
        (
            "REPLAY_FROZEN_TITLE_AUTHORITY_DRIFT_SOURCE_FACT_REVIEW_MISSING",
            "SOURCE_FACT_REVIEW",
        ),
        (
            "REPLAY_FROZEN_TITLE_AUTHORITY_DRIFT_SOURCE_FACT_REVIEW_CPA_TEXT_REVIEW_CALL_FAILED",
            "SOURCE_FACT_REVIEW",
        ),
        (
            "REPLAY_FROZEN_TITLE_AUTHORITY_DRIFT_SOURCE_FACT_REVIEW_CPA_ENTITY_SURFACE_RESPONSE_INVALID",
            "SOURCE_FACT_REVIEW",
        ),
        (
            "REPLAY_FROZEN_TITLE_AUTHORITY_DRIFT_SOURCE_FACT_REVIEW_CPA_TEXT_REVIEW_ADDRESSEE_ATTRIBUTION_UNRESOLVED",
            "SOURCE_FACT_REVIEW",
        ),
        (
            "REPLAY_FROZEN_TITLE_AUTHORITY_DRIFT_SOURCE_FACT_REVIEW_SOURCE_FACT_FINAL_REVIEWED_SRT_REQUIRED",
            "SOURCE_FACT_REVIEW",
        ),
        (
            "REPLAY_FROZEN_TITLE_AUTHORITY_DRIFT_STAGED_TITLE_MISMATCH",
            "FROZEN_TITLE_AUTHORITY",
        ),
        (
            "REPLAY_FROZEN_TITLE_AUTHORITY_DRIFT_STORY_RESOLVED_HOOK_MISMATCH",
            "FROZEN_TITLE_AUTHORITY",
        ),
        (
            "REPLAY_FROZEN_TITLE_AUTHORITY_DRIFT_TITLE_AUTHORITY_ERROR",
            "FROZEN_TITLE_AUTHORITY",
        ),
    ],
)
def test_replay_title_surface_reasons_use_closed_predicate_mapping(
    reason: str, predicate: str,
) -> None:
    assert cli._failure_predicate(reason) == predicate


def test_replay_title_surface_reason_with_unrecognized_suffix_stays_private() -> None:
    assert cli._failure_predicate(
        "REPLAY_FROZEN_TITLE_AUTHORITY_DRIFT_SOURCE_FACT_REVIEW:provider-secret"
    ) == "PRIVATE_FINALIZATION"


def _review_flags(
    stage: Path, *, date: str = "2026-08-14", cid: str = "cid",
    discovery: dict[str, object] | None = None,
) -> Path:
    path = stage / "finalizer-runtime" / date / cid / f"{cid}.review-flags.json"
    path.parent.mkdir(parents=True)
    document: dict[str, object] = {
        "schema_version": "final-review-audit.v2",
        "status": "FLAGGED",
        "release_gate": "BLOCK",
        "reason_codes": ["FINAL_REVIEW_UNRESOLVED_FINDINGS"],
        "boundary_semantic_review": {
            "schema_version": "talk-boundary-semantic-review.v1",
            "status": "BLOCK",
            "reason_codes": ["BOUNDARY_SEMANTIC_REVIEW_REQUIRED"],
        },
    }
    if discovery is not None:
        document["discovery"] = discovery
    path.write_text(json.dumps(document) + "\n")
    return path


def test_prepare_extracts_exact_final_review_flags_without_private_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = tmp_path / "record.json"
    record.write_text("{}\n")
    plan = SimpleNamespace(
        date="2026-08-14", candidate_id="cid", record_path=record,
        baseline=SimpleNamespace(config={"sha256": "a" * 64}),
    )
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    stage = parent / "candidate-stage"
    stage.mkdir()
    (stage / "stage.json").write_text("{}\n")
    _review_flags(stage)
    monkeypatch.setattr(cli, "stage_replay", lambda *_args, **_kwargs: {"stage": str(stage)})
    monkeypatch.setattr(cli, "_production_llm_call", lambda **_kwargs: lambda _prompt: "{}")
    monkeypatch.setattr(
        cli, "synthesize_replay_spec_and_finalize_private",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            SystemExit("FINAL_REVIEW_RELEASE_BLOCKED: /private/secret/provider stderr")
        ),
    )
    with pytest.raises(cli._PrepareFailure) as caught:
        cli._prepare(plan, runtime=tmp_path, stage_parent=parent)
    failure = caught.value
    assert failure.reason_code == "FINAL_REVIEW_RELEASE_BLOCKED"
    assert dict(failure.predicate_failures) == {
        "EXACT_DELIVERY_BOUNDARY_REVIEW": "BOUNDARY_SEMANTIC_REVIEW_REQUIRED",
        "EXACT_FINAL_RELEASE_REVIEW": "FINAL_REVIEW_UNRESOLVED_FINDINGS",
    }
    assert "/private" not in json.dumps({"reason": failure.reason_code, "rows": failure.predicate_failures})
    assert not stage.exists()


def test_prepare_retains_only_closed_provider_failure_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = tmp_path / "record.json"
    record.write_text("{}\n")
    plan = SimpleNamespace(
        date="2026-08-14", candidate_id="cid", record_path=record,
        baseline=SimpleNamespace(config={"sha256": "a" * 64}),
    )
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    stage = parent / "candidate-stage"
    stage.mkdir()
    (stage / "stage.json").write_text("{}\n")
    _review_flags(stage, discovery={
        "provider_class": "service",
        "provider_status_codes": [503, 408, 503],
        "provider_detail": "token=secret /private/stage prompt completion request-id",
        "detail": "LlmCallError",
    })
    monkeypatch.setattr(cli, "stage_replay", lambda *_args, **_kwargs: {"stage": str(stage)})
    monkeypatch.setattr(cli, "_production_llm_call", lambda **_kwargs: lambda _prompt: "{}")
    monkeypatch.setattr(
        cli, "synthesize_replay_spec_and_finalize_private",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(SystemExit("FINAL_REVIEW_RELEASE_BLOCKED: token=secret")),
    )
    with pytest.raises(cli._PrepareFailure) as caught:
        cli._prepare(plan, runtime=tmp_path, stage_parent=parent)
    failure = caught.value
    assert failure.provider_failure_summary == {
        "provider_class": "service", "provider_status_codes": [408, 503],
    }
    assert "token=secret" not in json.dumps(vars(failure), sort_keys=True)
    assert "/private/stage" not in json.dumps(vars(failure), sort_keys=True)
    assert not stage.exists()


def test_replay_provider_summary_keeps_only_allowlisted_json_parse_code(tmp_path: Path) -> None:
    discovery = {
        "provider_error_code": "LLM_JSON_NO_OBJECT",
        "provider_detail": "token=secret /private/prompt completion",
        "provider_class": "forged",
        "provider_status_codes": [999],
    }
    assert cli._provider_failure_summary(discovery) == {
        "provider_error_code": "LLM_JSON_NO_OBJECT",
    }

    runtime = tmp_path / "runtime"
    repo = runtime / "repo"
    repo.mkdir(parents=True)
    (repo / "DEPLOYED_COMMIT").write_text("a" * 40 + "\n")
    (repo / "DEPLOYED_AUTHORITY_MANIFEST.json").write_text("{}\n")
    plan = SimpleNamespace(
        date="2026-08-14", candidate_id="cid",
        expected_video_sha256="sha256:" + "b" * 64,
        baseline=SimpleNamespace(config={"sha256": "c" * 64}),
    )
    receipt_sha = cli._sanitized_failure_receipt(
        runtime=runtime, plan=plan, matrix=[], provider_attempted=True,
        provider_failure_summary=cli._provider_failure_summary(discovery),
    )
    receipt = next((runtime / "reports").rglob(receipt_sha.removeprefix("sha256:") + ".json"))
    payload = receipt.read_text()
    assert json.loads(payload)["provider_failure_summary"] == {
        "provider_error_code": "LLM_JSON_NO_OBJECT",
    }
    assert "token=secret" not in payload
    assert "/private/prompt" not in payload


def test_replay_provider_summary_keeps_only_closed_transport_reason() -> None:
    assert cli._provider_failure_summary({
        "provider_error_code": "LLM_COMMAND_FAILED",
        "provider_detail": "token=secret /private/prompt",
    }) == {"provider_error_code": "LLM_COMMAND_FAILED"}


@pytest.mark.parametrize("discovery", [
    None,
    {"provider_class": "forged", "provider_status_codes": [503]},
    {"provider_class": ["service"], "provider_status_codes": [503]},
    {"provider_class": "service", "provider_status_codes": [99, 600]},
    {"provider_class": "service", "provider_status_codes": [True]},
    {"provider_class": "service", "provider_status_codes": "503"},
])
def test_review_flags_drops_invalid_provider_failure_summary(
    tmp_path: Path, discovery: dict[str, object] | None,
) -> None:
    stage = tmp_path / "stage"
    stage.mkdir()
    _review_flags(stage, discovery=discovery)
    plan = SimpleNamespace(date="2026-08-14", candidate_id="cid")
    failures, summary = cli._review_flags_diagnostics(stage=stage, plan=plan)
    assert failures
    assert summary is None


def test_sanitized_receipt_drops_provider_detail_and_keeps_closed_summary(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    repo = runtime / "repo"
    repo.mkdir(parents=True)
    (repo / "DEPLOYED_COMMIT").write_text("a" * 40 + "\n")
    (repo / "DEPLOYED_AUTHORITY_MANIFEST.json").write_text("{}\n")
    plan = SimpleNamespace(
        date="2026-08-14", candidate_id="cid", expected_video_sha256="sha256:" + "b" * 64,
        baseline=SimpleNamespace(config={"sha256": "c" * 64}),
    )
    receipt_sha = cli._sanitized_failure_receipt(
        runtime=runtime, plan=plan, matrix=[], provider_attempted=True,
        provider_failure_summary={
            "provider_class": "quota", "provider_status_codes": [429, 429],
            "provider_detail": "token=secret /private/stage prompt completion",
        },
    )
    receipt = next((runtime / "reports").rglob(receipt_sha.removeprefix("sha256:") + ".json"))
    payload = receipt.read_text()
    document = json.loads(payload)
    assert document["provider_failure_summary"] == {
        "provider_class": "quota", "provider_status_codes": [429],
    }
    assert "token=secret" not in payload
    assert "/private/stage" not in payload
    assert "prompt completion" not in payload


@pytest.mark.parametrize("kind", ["malformed", "wrong-schema", "oversized", "symlink"])
def test_review_flags_unsafe_or_invalid_are_ignored(
    tmp_path: Path, kind: str,
) -> None:
    stage = tmp_path / "stage"
    stage.mkdir()
    path = _review_flags(stage)
    if kind == "malformed":
        path.write_text("{not-json")
    elif kind == "wrong-schema":
        document = json.loads(path.read_text())
        document["schema_version"] = "wrong.v1"
        path.write_text(json.dumps(document))
    elif kind == "oversized":
        path.write_bytes(b"x" * (cli._MAX_REVIEW_FLAGS_BYTES + 1))
    else:
        target = tmp_path / "flags.json"
        target.write_text(path.read_text())
        path.unlink()
        path.symlink_to(target)
    plan = SimpleNamespace(date="2026-08-14", candidate_id="cid")
    assert cli._review_flags_predicate_failures(stage=stage, plan=plan) == ()


def test_prepare_cleans_stage_when_review_flags_are_invalid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = tmp_path / "record.json"
    record.write_text("{}\n")
    plan = SimpleNamespace(
        date="2026-08-14", candidate_id="cid", record_path=record,
        baseline=SimpleNamespace(config={"sha256": "a" * 64}),
    )
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    stage = parent / "candidate-stage"
    stage.mkdir()
    (stage / "stage.json").write_text("{}\n")
    _review_flags(stage).write_text("{malformed")
    monkeypatch.setattr(cli, "stage_replay", lambda *_args, **_kwargs: {"stage": str(stage)})
    monkeypatch.setattr(cli, "_production_llm_call", lambda **_kwargs: lambda _prompt: "{}")
    monkeypatch.setattr(
        cli, "synthesize_replay_spec_and_finalize_private",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(SystemExit("FINAL_REVIEW_RELEASE_BLOCKED: /private/secret")),
    )
    with pytest.raises(cli._PrepareFailure) as caught:
        cli._prepare(plan, runtime=tmp_path, stage_parent=parent)
    assert caught.value.reason_code == "FINAL_REVIEW_RELEASE_BLOCKED"
    assert caught.value.predicate_failures == (("EXACT_FINAL_RELEASE_REVIEW", "FINAL_REVIEW_RELEASE_BLOCKED"),)
    assert not stage.exists()


def test_main_returns_two_for_blocked_prepare_without_external_rc_wrapper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    plan = SimpleNamespace(
        date="2026-08-14", candidate_id="cid", matrix=(),
        baseline=SimpleNamespace(config={"sha256": "a" * 64}),
    )
    monkeypatch.setattr(cli, "_safe_directory", lambda path: Path(path))
    monkeypatch.setattr(cli, "_runtime_gate", lambda _runtime: None)
    monkeypatch.setattr(cli, "build_replay_plan", lambda **_kwargs: plan)

    def fail_prepare(*_args: object, **_kwargs: object) -> object:
        raise cli._PrepareFailure(
            reason_code="REPLAY_BASELINE_APPLICATION_FAILED", provider_attempted=False,
            provider_failure_summary={
                "provider_class": "service", "provider_status_codes": [503, 408, 503],
                "provider_detail": "token=secret /private/stage",
            },
        )

    captured: dict[str, object] = {}
    monkeypatch.setattr(cli, "_prepare", fail_prepare)
    monkeypatch.setattr(
        cli, "_sanitized_failure_receipt",
        lambda **kwargs: captured.update(kwargs) or "sha256:" + "d" * 64,
    )
    assert cli.main([
        "--full-dry-run", "--runtime-root", str(runtime), "--date", "2026-08-14",
        "--candidate-id", "cid", "--private-stage-parent", str(parent),
    ]) == 2
    assert captured["provider_attempted"] is False
    assert captured["provider_failure_summary"] == {
        "provider_class": "service", "provider_status_codes": [408, 503],
    }
    assert captured["matrix"][-1] == {
        "predicate": "PRIVATE_FINALIZATION", "status": "FAIL",
        "reason_code": "REPLAY_BASELINE_APPLICATION_FAILED",
    }
    item = json.loads(capsys.readouterr().out)["candidates"][0]
    assert item["status"] == "BLOCKED"
    assert item["provider_failure_summary"] == {
        "provider_class": "service", "provider_status_codes": [408, 503],
    }


def test_main_uses_same_closed_generic_diagnostic_in_item_and_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    plan = SimpleNamespace(date="2026-08-14", candidate_id="cid", matrix=(), baseline=SimpleNamespace(config={"sha256": "a" * 64}))
    diagnostic = {"exception_type": "ValueError", "exception_locus": {
        "module": "src.autoslice.reviewed_baseline_replay", "function": "prepare", "line": 91,
    }}
    monkeypatch.setattr(cli, "_safe_directory", lambda path: Path(path))
    monkeypatch.setattr(cli, "_runtime_gate", lambda _runtime: None)
    monkeypatch.setattr(cli, "build_replay_plan", lambda **_kwargs: plan)
    monkeypatch.setattr(cli, "_prepare", lambda *_args, **_kwargs: (_ for _ in ()).throw(
        cli._PrepareFailure(reason_code="REPLAY_PREPARE_EXCEPTION", provider_attempted=False,
                            exception_diagnostic=diagnostic)
    ))
    captured: dict[str, object] = {}
    monkeypatch.setattr(cli, "_sanitized_failure_receipt", lambda **kwargs: captured.update(kwargs) or "sha256:" + "d" * 64)
    assert cli.main([
        "--full-dry-run", "--runtime-root", str(runtime), "--date", "2026-08-14",
        "--candidate-id", "cid", "--private-stage-parent", str(parent),
    ]) == 2
    assert captured["exception_diagnostic"] == diagnostic
    item = json.loads(capsys.readouterr().out)["candidates"][0]
    assert item["exception_diagnostic"] == diagnostic


def test_full_dry_receipt_carries_only_precise_review_flag_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    plan = SimpleNamespace(
        date="2026-08-14", candidate_id="cid", matrix=(),
        baseline=SimpleNamespace(config={"sha256": "a" * 64}),
    )
    monkeypatch.setattr(cli, "_safe_directory", lambda path: Path(path))
    monkeypatch.setattr(cli, "_runtime_gate", lambda _runtime: None)
    monkeypatch.setattr(cli, "build_replay_plan", lambda **_kwargs: plan)

    def fail_prepare(*_args: object, **_kwargs: object) -> object:
        raise cli._PrepareFailure(
            reason_code="FINAL_REVIEW_RELEASE_BLOCKED", provider_attempted=True,
            predicate_failures=(
                ("EXACT_DELIVERY_BOUNDARY_REVIEW", "BOUNDARY_SEMANTIC_REVIEW_REQUIRED"),
                ("EXACT_FINAL_RELEASE_REVIEW", "FINAL_REVIEW_UNRESOLVED_FINDINGS"),
            ),
        )

    captured: dict[str, object] = {}
    monkeypatch.setattr(cli, "_prepare", fail_prepare)
    monkeypatch.setattr(
        cli, "_sanitized_failure_receipt",
        lambda **kwargs: captured.update(kwargs) or "sha256:" + "d" * 64,
    )
    assert cli.main([
        "--full-dry-run", "--runtime-root", str(runtime), "--date", "2026-08-14",
        "--candidate-id", "cid", "--private-stage-parent", str(parent),
    ]) == 2
    rows = {row["predicate"]: row for row in captured["matrix"]}
    assert rows["EXACT_DELIVERY_BOUNDARY_REVIEW"] == {
        "predicate": "EXACT_DELIVERY_BOUNDARY_REVIEW", "status": "FAIL",
        "reason_code": "BOUNDARY_SEMANTIC_REVIEW_REQUIRED",
    }
    assert rows["EXACT_FINAL_RELEASE_REVIEW"] == {
        "predicate": "EXACT_FINAL_RELEASE_REVIEW", "status": "FAIL",
        "reason_code": "FINAL_REVIEW_UNRESOLVED_FINDINGS",
    }
    output = capsys.readouterr().out
    assert "PRIVATE_FINALIZATION" not in output
    assert "/private" not in output


def test_prepared_manifest_diagnostic_hash_is_file_digest_not_inner_seal(tmp_path: Path) -> None:
    manifest = tmp_path / "prepared.json"
    manifest.write_text('{"prepared_sha256":"sha256:' + "a" * 64 + '"}\n')
    manifest.chmod(0o600)
    finalization = SimpleNamespace(
        prepared_manifest=manifest, prepared_sha256="sha256:" + "a" * 64,
    )
    digest = cli._prepared_manifest_sha256(finalization)
    assert digest == "sha256:" + hashlib.sha256(manifest.read_bytes()).hexdigest()
    assert digest != finalization.prepared_sha256


def test_private_parent_rejects_a_symlink_ancestor(tmp_path: Path) -> None:
    actual = tmp_path / "actual"
    actual.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(actual, target_is_directory=True)
    with pytest.raises(Exception):
        cli._private_parent(linked / "stage")


def test_readiness_graph_is_scoped_and_does_not_prepare(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    (runtime / "repo").mkdir(parents=True)
    (runtime / "state").mkdir()
    (runtime / "state" / "2026-08-14.json").write_text('{"picks":[]}\n')
    monkeypatch.setattr(cli, "_safe_directory", lambda path: Path(path))
    monkeypatch.setattr(
        "src.autoslice.publication_readiness.build_readiness_graph",
        lambda **_kwargs: {"rows": [
            {"candidate_id": "one", "recording_date": "2026-08-14", "category": "READY_TO_PREPARE", "reason_codes": ["LOCAL_PREPARATION_AVAILABLE"]},
            {"candidate_id": "two", "recording_date": "2026-08-15", "category": "NEEDS_PROVIDER", "reason_codes": ["COVER_QC_MISSING"]},
        ], "graph_blockers": []},
    )
    result = cli._readiness_graph(runtime=runtime, date="2026-08-14", candidate_ids=[])
    assert result["rows"] == [{"candidate_id": "one", "recording_date": "2026-08-14", "category": "READY_TO_PREPARE", "reason_codes": ["LOCAL_PREPARATION_AVAILABLE"]}]
    assert result["upload_allowed"] is False


def test_readiness_graph_normalizes_semantic_recall_stale_replay_to_prepare(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    (runtime / "repo").mkdir(parents=True)
    (runtime / "state").mkdir()
    (runtime / "state" / "2026-08-14.json").write_text(json.dumps({"picks": [{
        "cid": "one", "lane": "semantic_recall", "status": "candidate_rejected",
    }]}))
    monkeypatch.setattr(
        "src.autoslice.publication_readiness.build_readiness_graph",
        lambda **_kwargs: {"rows": [{
            "candidate_id": "one", "recording_date": "2026-08-14",
            "category": "STATE_DRIFT", "reason_codes": [
                "STATE_ROW_NOT_REVIEW_READY", "PACKAGE_ARTIFACT_HASH_DRIFT", "COVER_QC_MISSING",
            ],
        }], "graph_blockers": []},
    )
    monkeypatch.setattr(cli, "build_replay_plan", lambda **_kwargs: SimpleNamespace(
        baseline=SimpleNamespace(config={"sha256": "a" * 64}),
    ))
    result = cli._readiness_graph(runtime=runtime, date="2026-08-14", candidate_ids=[])
    row = result["rows"][0]
    assert row["category"] == "READY_TO_PREPARE"
    assert row["replay_readiness"] == {
        "predicate": "SEALED_REVIEWED_BASELINE_REPLAY_READY", "status": "PASS",
        "baseline_sha256": "a" * 64,
    }
    assert row["generic_observation"]["category"] == "STATE_DRIFT"


def test_readiness_graph_normalizes_only_selection_terminal_for_sharded_talk(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    (runtime / "repo").mkdir(parents=True)
    (runtime / "state").mkdir()
    (runtime / "state" / "2026-08-14.json").write_text(json.dumps({"picks": [{
        "cid": "one", "lane": "semantic_recall_sharded", "status": "candidate_rejected",
    }]}))
    monkeypatch.setattr(
        "src.autoslice.publication_readiness.build_readiness_graph",
        lambda **_kwargs: {"rows": [{
            "candidate_id": "one", "recording_date": "2026-08-14",
            "category": "NEEDS_IVAN_TRUTH", "reason_codes": ["SELECTION_SUPPORT_TERMINAL_BLOCKED"],
        }], "graph_blockers": []},
    )
    monkeypatch.setattr(cli, "build_replay_plan", lambda **_kwargs: SimpleNamespace(
        baseline=SimpleNamespace(config={"sha256": "a" * 64}),
    ))
    row = cli._readiness_graph(runtime=runtime, date="2026-08-14", candidate_ids=[])["rows"][0]
    assert row["category"] == "READY_TO_PREPARE"
    assert row["generic_observation"] == {
        "category": "NEEDS_IVAN_TRUTH", "reason_codes": ["SELECTION_SUPPORT_TERMINAL_BLOCKED"],
    }


@pytest.mark.parametrize(
    ("lane", "category", "reasons"),
    [
        ("semantic_recall", "NEEDS_IVAN_TRUTH", ["HUMAN_TRUTH_MISSING"]),
        ("talk", "NEEDS_IVAN_TRUTH", ["HOLD_PENDING_REVIEW"]),
        ("song", "STATE_DRIFT", ["STATE_ROW_NOT_REVIEW_READY"]),
    ],
)
def test_readiness_graph_never_normalizes_unowned_reason_or_song_lane(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, lane: str, category: str, reasons: list[str],
) -> None:
    runtime = tmp_path / "runtime"
    (runtime / "repo").mkdir(parents=True)
    (runtime / "state").mkdir()
    (runtime / "state" / "2026-08-14.json").write_text(json.dumps({"picks": [{
        "cid": "one", "lane": lane, "status": "candidate_rejected",
    }]}))
    monkeypatch.setattr(
        "src.autoslice.publication_readiness.build_readiness_graph",
        lambda **_kwargs: {"rows": [{
            "candidate_id": "one", "recording_date": "2026-08-14",
            "category": category, "reason_codes": reasons,
        }], "graph_blockers": []},
    )
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        cli, "build_replay_plan",
        lambda **kwargs: calls.append(kwargs) or pytest.fail("unowned row must not build a replay plan"),
    )
    row = cli._readiness_graph(runtime=runtime, date="2026-08-14", candidate_ids=[])["rows"][0]
    assert row["category"] == category
    assert "generic_observation" not in row
    assert calls == []


def test_readiness_graph_keeps_stale_row_blocked_without_sealed_baseline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    (runtime / "repo").mkdir(parents=True)
    (runtime / "state").mkdir()
    (runtime / "state" / "2026-08-14.json").write_text(json.dumps({"picks": [{
        "cid": "one", "lane": "semantic_recall", "status": "candidate_rejected",
    }]}))
    monkeypatch.setattr(
        "src.autoslice.publication_readiness.build_readiness_graph",
        lambda **_kwargs: {"rows": [{
            "candidate_id": "one", "recording_date": "2026-08-14",
            "category": "STATE_DRIFT", "reason_codes": ["STATE_ROW_NOT_REVIEW_READY"],
        }], "graph_blockers": []},
    )
    monkeypatch.setattr(
        cli, "build_replay_plan",
        lambda **_kwargs: (_ for _ in ()).throw(cli.ReviewedBaselineReplayError("REPLAY_BASELINE_MISSING")),
    )
    row = cli._readiness_graph(runtime=runtime, date="2026-08-14", candidate_ids=[])["rows"][0]
    assert row["category"] == "NEEDS_IVAN_TRUTH"
    assert row["replay_readiness"] == {
        "predicate": "SEALED_REVIEWED_BASELINE_REPLAY_READY", "status": "FAIL",
        "reason_code": "REPLAY_BASELINE_MISSING",
    }


def test_full_package_prepare_overlaps_and_apply_rebinds_state_serially(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """The future boundary includes package preparation, not merely synthesize."""

    runtime = tmp_path / "runtime"
    (runtime / "repo").mkdir(parents=True)
    (runtime / "state").mkdir()
    (runtime / "state" / "2026-08-14.json").write_text('{"generation":0}\n')
    (runtime / "DISABLED").write_text("disabled\n")
    stage_parent = tmp_path / "stages"
    stage_parent.mkdir(mode=0o700)
    plans = [
        SimpleNamespace(date="2026-08-14", candidate_id=cid, matrix=(), baseline=SimpleNamespace(config={"sha256": "a" * 64}))
        for cid in ("one", "two")
    ]
    monkeypatch.setattr(cli, "build_replay_plan", lambda **kwargs: next(
        item for item in plans if item.candidate_id == kwargs["candidate_id"]
    ))
    active = 0
    maximum = 0
    barrier = threading.Barrier(2)
    state_generations: list[int] = []
    commits: list[tuple[str, int]] = []

    def fake_prepare(plan, *, runtime, stage_parent, state_path, speaker_python):
        nonlocal active, maximum
        stage = stage_parent / plan.candidate_id
        stage.mkdir()
        active += 1
        maximum = max(maximum, active)
        barrier.wait(timeout=2)
        active -= 1
        return stage, SimpleNamespace(prepared_sha256="sha256:" + "b" * 64), SimpleNamespace(
            after=object(), projection=object(),
        ), None, (), False

    def fake_rebind(plan, *, runtime_root, state_path, finalization, after, projection):
        assert projection is not None
        generation = json.loads(state_path.read_text())["generation"]
        state_generations.append(generation)
        return SimpleNamespace(candidate_id=plan.candidate_id, generation=generation)

    def fake_commit(*, runtime_root, after):
        commits.append((after.candidate_id, after.generation))
        path = runtime_root / "state" / "2026-08-14.json"
        path.write_text(json.dumps({"generation": after.generation + 1}) + "\n")
        journal = runtime_root / f"{after.candidate_id}.journal"
        journal.write_text("journal\n")
        return journal

    monkeypatch.setattr(cli, "_prepare", fake_prepare)
    monkeypatch.setattr(cli, "rebind_replay_after_image_state", fake_rebind)
    monkeypatch.setattr(cli, "commit_prepared_after_image", fake_commit)
    monkeypatch.setattr(cli, "cleanup_prepared_after_image_stage", lambda **_kwargs: None)
    monkeypatch.setattr(cli, "stream_binding", lambda *_args, **_kwargs: SimpleNamespace(sha256="sha256:" + "c" * 64))
    assert cli.main([
        "--apply", "--runtime-root", str(runtime), "--date", "2026-08-14",
        "--candidate-id", "one", "--candidate-id", "two",
        "--private-stage-parent", str(stage_parent),
    ]) == 0
    assert maximum == 2
    assert state_generations == [0, 1]
    assert commits == [("one", 0), ("two", 1)]
    assert json.loads(capsys.readouterr().out)["candidates"][1]["status"] == "COMMITTED"


def test_committed_cleanup_oserror_cannot_mask_journal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    runtime = tmp_path / "runtime"
    (runtime / "repo").mkdir(parents=True)
    (runtime / "state").mkdir()
    (runtime / "state" / "2026-08-14.json").write_text('{"generation":0}\n')
    (runtime / "DISABLED").write_text("disabled\n")
    stage_parent = tmp_path / "stages"
    stage_parent.mkdir(mode=0o700)
    plan = SimpleNamespace(
        date="2026-08-14", candidate_id="one", matrix=(),
        baseline=SimpleNamespace(config={"sha256": "a" * 64}),
    )
    monkeypatch.setattr(cli, "build_replay_plan", lambda **_kwargs: plan)
    stage = stage_parent / "one"
    stage.mkdir()
    monkeypatch.setattr(
        cli, "_prepare",
        lambda *_args, **_kwargs: (stage, SimpleNamespace(prepared_manifest=stage / "prepared.json"), SimpleNamespace(
            after=object(), projection=object(),
        ), None, (), False),
    )
    monkeypatch.setattr(cli, "rebind_replay_after_image_state", lambda *_args, **_kwargs: SimpleNamespace())
    journal = runtime / "journal.json"
    journal.write_text("journal\n")
    monkeypatch.setattr(cli, "commit_prepared_after_image", lambda **_kwargs: journal)
    monkeypatch.setattr(cli, "cleanup_prepared_after_image_stage", lambda **_kwargs: None)
    monkeypatch.setattr(cli, "_cleanup_private_stage", lambda **_kwargs: (_ for _ in ()).throw(OSError("disk")))
    monkeypatch.setattr(cli, "stream_binding", lambda *_args, **_kwargs: SimpleNamespace(sha256="sha256:" + "c" * 64))
    assert cli.main([
        "--apply", "--runtime-root", str(runtime), "--date", "2026-08-14",
        "--candidate-id", "one", "--private-stage-parent", str(stage_parent),
    ]) == 2
    item = json.loads(capsys.readouterr().out)["candidates"][0]
    assert item["status"] == "COMMITTED_CLEANUP_UNCONFIRMED"
    assert item["journal_sha256"] == "sha256:" + "c" * 64
    assert item["predicate_matrix"][-1] == {"predicate": "PRIVATE_STAGE_CLEANUP", "status": "FAIL"}


def test_rebind_failure_cleans_uncharted_transaction_stage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    runtime = tmp_path / "runtime"
    (runtime / "repo").mkdir(parents=True)
    (runtime / "state").mkdir()
    (runtime / "state" / "2026-08-14.json").write_text('{"generation":0}\n')
    (runtime / "DISABLED").write_text("disabled\n")
    stage_parent = tmp_path / "stages"
    stage_parent.mkdir(mode=0o700)
    stage = stage_parent / "one"
    stage.mkdir()
    plan = SimpleNamespace(date="2026-08-14", candidate_id="one", matrix=(),
                           baseline=SimpleNamespace(config={"sha256": "a" * 64}))
    prepared_after = SimpleNamespace(after=object(), projection=object())
    monkeypatch.setattr(cli, "build_replay_plan", lambda **_kwargs: plan)
    monkeypatch.setattr(cli, "_prepare", lambda *_args, **_kwargs: (
        stage, SimpleNamespace(prepared_manifest=stage / "prepared.json"), prepared_after, None, (), False,
    ))
    monkeypatch.setattr(
        cli, "rebind_replay_after_image_state",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("state drift")),
    )
    cleaned: list[object] = []
    monkeypatch.setattr(
        cli, "cleanup_prepared_after_image_stage",
        lambda **kwargs: cleaned.append(kwargs["after"]),
    )
    monkeypatch.setattr(cli, "commit_prepared_after_image", lambda **_kwargs: pytest.fail("must not commit"))
    monkeypatch.setattr(cli, "_cleanup_private_stage", lambda **_kwargs: None)
    monkeypatch.setattr(cli, "_sanitized_failure_receipt", lambda **_kwargs: "sha256:" + "d" * 64)
    assert cli.main([
        "--apply", "--runtime-root", str(runtime), "--date", "2026-08-14",
        "--candidate-id", "one", "--private-stage-parent", str(stage_parent),
    ]) == 2
    assert cleaned == [prepared_after.after]
    assert json.loads(capsys.readouterr().out)["candidates"][0]["status"] == "BLOCKED"
