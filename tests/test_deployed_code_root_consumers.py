"""Exercise G-LIST consumers with distinct legacy and published trees, offline."""
from __future__ import annotations

import importlib
import io
import json
import shutil
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import import_external_package as external
from scripts import llm_via_runtime_cpa_ssh as bridge
from scripts import plan_recovery_review_rerun as planner
from scripts import replay_reviewed_subtitle_baseline as cli
from src.autoslice import channel_profile, llm_client, provider_slots
from src.autoslice import producer_delivery_transaction as delivery
from src.autoslice import published_recovery_package as recovery
from src.autoslice import published_recovery_package_contract as contract
from src.autoslice import reviewed_baseline_replay as replay
from src.autoslice import reviewed_baseline_replay_authority as replay_authority
from src.autoslice import fastlane_c2_private_authority as c2

import test_published_recovery_package as published_fixtures

SHA = "a" * 40
DATE = "2026-08-14"


def tree(tmp_path, topology):
    runtime = tmp_path / "runtime"
    legacy = runtime / "repo"
    legacy.mkdir(parents=True, exist_ok=True)
    (legacy / "TREE").write_text("stale")
    (legacy / "DEPLOYED_COMMIT").write_text("b" * 40 + "\n")
    (legacy / "DEPLOYED_AUTHORITY_MANIFEST.json").write_text("stale manifest")
    if topology == "legacy":
        return runtime, legacy
    repo = runtime / "versions" / SHA / "repo"
    repo.mkdir(parents=True)
    (repo / "TREE").write_text("version")
    (repo / "DEPLOYED_COMMIT").write_text(SHA + "\n")
    (repo / "DEPLOYED_AUTHORITY_MANIFEST.json").write_text("version manifest")
    (runtime / "current").symlink_to(f"versions/{SHA}")
    return runtime, repo


class Reached(BaseException):
    """Stop at an I/O boundary without being swallowed by production guards."""


def observe(path):
    raise Reached(Path(path))


SITES = [
    "recovery_preflight_179", "recovery_revalidate_297",
    "replay_copy_659", "replay_llm_880", "replay_profile_1486",
    "replay_projection_1625", "portable_profile_176",
    "readiness_graph_355", "readiness_plan_408",
    "diagnostic_commit_1003", "diagnostic_manifest_1010", "cli_plan_1357",
    "cleanup_seals_613_614", "c2_closure_260", "external_default_1237",
]


@pytest.mark.parametrize("topology", ["version", "legacy"])
@pytest.mark.parametrize("site", SITES)
def test_consumer_reads_selected_tree(tmp_path, monkeypatch, site, topology):
    runtime, expected = tree(tmp_path, topology)
    plan = SimpleNamespace(candidate_id="cid", date=DATE, record_path=tmp_path / "record")

    def invoke():
        if site.startswith("recovery_"):
            monkeypatch.setattr(recovery, "build_recovery_publication_authorities",
                                lambda **kw: observe(Path(kw["registry_path"]).parents[2]))
            if site == "recovery_preflight_179":
                recovery.published_recovery_preflight(plan, runtime_root=runtime,
                                                     expected_bvid="BV1Sahj65ExM")
            else:
                monkeypatch.setattr(recovery, "_revalidate_state", lambda *a, **kw: None)
                monkeypatch.setattr(recovery, "regular_binding", lambda *a, **kw: SimpleNamespace(sha256="record"))
                recovery._revalidate_inputs(plan, SimpleNamespace(source_record_sha256="record"),
                                            runtime_root=runtime)
        elif site == "replay_copy_659":
            (tmp_path / "private").mkdir()
            monkeypatch.setattr(delivery, "deployment_authority_binding", lambda *a: {})
            monkeypatch.setattr(replay, "regular_binding", lambda path, **kw: observe(path.parent))
            replay._copy_deployed_authority(source_runtime_root=runtime,
                                          private_runtime_root=tmp_path / "private")
        elif site == "replay_llm_880":
            monkeypatch.setattr(llm_client, "runtime_cpa_command_environment", lambda *a: {})
            monkeypatch.setattr(llm_client, "build_llm_call", lambda config: observe(
                Path(__import__("shlex").split(config.command_template)[1]).parents[1]))
            replay._production_llm_call(runtime_root=runtime, effort="medium")
        elif site in {"replay_profile_1486", "portable_profile_176"}:
            monkeypatch.setattr(channel_profile, "load_channel_profile", observe)
            if site == "replay_profile_1486":
                replay._profile_delivery_root(runtime_authority_root=runtime, date=DATE)
            else:
                replay_authority._portable_date_root(runtime_authority_root=runtime, date=DATE,
                                                     safe_directory=replay._safe_directory, error=ValueError)
        elif site == "replay_projection_1625":
            private = tmp_path / "private"
            private.mkdir()
            spec = private / "spec.json"
            spec.write_text(json.dumps({"output_root": str(private / "out" / DATE / "cid")}))
            staged = private / "staged.json"
            staged.write_text("{}")
            monkeypatch.setattr(replay, "_profile_delivery_root", lambda **kw: tmp_path / "delivery")
            monkeypatch.setattr(delivery, "_read_document", lambda *a: {"artifacts": [
                {"role": role, "staged_path": str(staged), "staged_sha256": replay.regular_binding(staged, label="T").sha256}
                for role in ("record", "publish", "chat_authority", "speaker_manifest")
            ]})
            monkeypatch.setattr(replay, "_project_document_locators", lambda *a, **kw: observe(kw["mappings"][2][1]))
            replay.project_private_finalization_to_live(
                SimpleNamespace(date=DATE, candidate_id="cid", package_root=runtime / "out" / DATE / "cid"),
                finalization=SimpleNamespace(private_runtime_root=private, prepared_sha256="a" * 64,
                                             prepared_manifest=private / "prepared.json", spec_path=spec),
                runtime_authority_root=runtime)
        elif site.startswith("readiness_"):
            from src.autoslice import publication_readiness, runner_state_writeback
            if site == "readiness_graph_355":
                monkeypatch.setattr(publication_readiness, "build_readiness_graph", lambda **kw: observe(kw["repository_root"]))
            else:
                reason = next(iter(cli._REPLAY_OWNED_STALE_REASONS))
                category = next(iter(cli._REPLAY_NORMALIZABLE_CATEGORIES))
                monkeypatch.setattr(publication_readiness, "build_readiness_graph", lambda **kw: {"rows": [
                    {"recording_date": DATE, "candidate_id": "cid", "category": category, "reason_codes": [reason]}]})
                monkeypatch.setattr(runner_state_writeback, "read_exact_state_preimage", lambda *a, **kw: json.dumps(
                    {"picks": [{"candidate_id": "cid", "status": "candidate_rejected", "lane": "talk"}]}).encode())
                monkeypatch.setattr(cli, "build_replay_plan", lambda **kw: observe(kw["repo_root"]))
            cli._readiness_graph(runtime=runtime, date=DATE, candidate_ids=["cid"])
        elif site.startswith("diagnostic_"):
            # Let both real reads finish, then inspect the emitted, sealed receipt.
            plan.baseline = SimpleNamespace(config={"sha256": "x"})
            plan.expected_video_sha256 = "y"
            cli._sanitized_failure_receipt(runtime=runtime, plan=plan, matrix=[], provider_attempted=False)
            receipt = json.loads(next((runtime / "reports").rglob("*.json")).read_text())
            if site == "diagnostic_commit_1003":
                assert receipt["deployed_commit"] == (expected / "DEPLOYED_COMMIT").read_text().strip()
            else:
                assert receipt["deployed_authority_manifest_sha256"] == replay.regular_binding(
                    expected / "DEPLOYED_AUTHORITY_MANIFEST.json", label="T").sha256
            observe(expected)
        elif site == "cli_plan_1357":
            monkeypatch.setattr(cli, "build_replay_plan", lambda **kw: observe(kw["repo_root"]))
            cli.main(["--plan", "--runtime-root", str(runtime), "--date", DATE, "--candidate-id", "cid"])
        elif site == "cleanup_seals_613_614":
            monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
            cleanup = importlib.import_module("cleanup_preflight_scan")
            manifest = runtime / ".prepared-deliveries/talk/cid/hash/prepared.json"
            handle = SimpleNamespace(manifest_path=manifest)
            monkeypatch.setattr(delivery, "load_prepared_delivery", lambda **kw: handle)
            monkeypatch.setattr(delivery, "_read_document", lambda *a, **kw: {})
            monkeypatch.setattr(delivery, "deployment_authority_binding", lambda *a: {})
            monkeypatch.setattr(delivery, "_checked_artifacts", lambda *a, **kw: None)
            seen = []
            cleanup._prepared_recovery_references(str(runtime), {str(manifest): json.dumps(
                {"artifacts": [], "deployed_authority": {}})}, set(), set(), lambda path, **kw: seen.append(Path(path)))
            assert seen == [expected / "DEPLOYED_COMMIT", expected / "DEPLOYED_AUTHORITY_MANIFEST.json"]
            observe(expected)
        elif site == "c2_closure_260":
            c2._load_c2_uniform_host_chat_closure(runtime=runtime, private_directory=replay._safe_directory,
                private_binding=lambda path, **kw: observe(path.parents[3]), load_json=lambda *a, **kw: {}, error=ValueError)
        elif site == "external_default_1237":
            monkeypatch.setattr(external, "run_import", lambda **kw: observe(kw["repo_root"]))
            external.main(["--source", str(tmp_path), "--base", str(runtime), "--date", DATE, "--candidate", "cid"])

    with pytest.raises(Reached) as caught:
        invoke()
    selected = caught.value.args[0]
    assert selected == expected, f"{site}: selected {selected}, expected {expected}"
    assert (selected / "TREE").read_text() == ("version" if topology == "version" else "stale")


@pytest.mark.parametrize("topology", ["version", "legacy"])
@pytest.mark.parametrize("site", ["registry_490", "state_authority_497"])
def test_live_recovery_recheck_reads_selected_tree(tmp_path, monkeypatch, topology, site):
    # Reuse the existing real private-package producer and its exact receipt checks.
    published_fixtures.test_prepare_published_recovery_materializes_private_package_without_state_write(tmp_path, monkeypatch)
    runtime = tmp_path / "runtime"
    legacy = runtime / "repo"
    (legacy / "TREE").write_text("stale")
    expected = legacy
    if topology == "version":
        expected = runtime / "versions" / SHA / "repo"
        shutil.copytree(legacy, expected)
        (expected / "TREE").write_text("version")
        (runtime / "current").symlink_to(f"versions/{SHA}")
        (expected / "DEPLOYED_MANIFEST.json").write_text("{}")
    monkeypatch.setattr(contract, "REGISTRY_REPO_PATH", recovery.REGISTRY_REPO_PATH)
    monkeypatch.setattr(contract, "REGISTRY_SHA256", recovery.REGISTRY_SHA256)
    package = tmp_path / "operator-private" / published_fixtures.CID / "replacement_recuts"
    from src.autoslice import recovery_title_authority
    if site == "registry_490":
        def publication(**kw):
            assert Path(kw["registry_path"]) == Path(kw["repo_root"]) / recovery.REGISTRY_REPO_PATH
            observe(kw["repo_root"])
        monkeypatch.setattr(recovery_title_authority, "build_recovery_publication_authorities", publication)
    else:
        monkeypatch.setattr(contract, "load_published_recovery_state_authority", lambda **kw: observe(kw["repo_root"]))
    with pytest.raises(Reached) as caught:
        contract.validate_materialized_published_recovery_package(package)
    assert caught.value.args[0] == expected


@pytest.mark.parametrize("topology", ["version", "legacy"])
def test_provider_recognizes_runtime_without_requiring_legacy_repo(tmp_path, monkeypatch, topology):
    runtime, _ = tree(tmp_path, topology)
    if topology == "version":
        shutil.rmtree(runtime / "repo")
    monkeypatch.delenv("AUTOSLICE_BASE", raising=False)
    monkeypatch.setattr(provider_slots, "_CANONICAL_PRODUCTION_ROOT", runtime)
    assert provider_slots.configured_runtime_root() == runtime


@pytest.mark.parametrize("topology", ["version", "legacy"])
def test_recovery_planner_accepts_only_its_deployed_target_repo(tmp_path, monkeypatch, topology):
    runtime, expected = tree(tmp_path, topology)
    source = tmp_path / "source"
    source.mkdir()
    recordings = runtime / "recordings"
    recordings.mkdir()
    fingerprint = "sha256:" + "1" * 64
    args = ["--source-base", str(source), "--target-base", str(runtime), "--date", DATE,
            "--expected-source-state-sha256", fingerprint, "--expected-old-fingerprint", fingerprint,
            "--expected-new-fingerprint", fingerprint, "--publication-authority-asset", "fixture.json",
            "--expected-publication-authority-sha256", fingerprint, "--candidate-id", "cid"]
    monkeypatch.setattr(planner, "__file__", str(expected / "scripts/plan_recovery_review_rerun.py"))
    monkeypatch.setattr(planner, "_regular_file_bytes", lambda path, **kw: observe(path))
    with pytest.raises(Reached) as caught:
        planner.main(args)
    assert caught.value.args[0] == source / "state" / f"{DATE}.json"


@pytest.mark.parametrize("topology", ["version", "legacy"])
@pytest.mark.parametrize("site", ["remote_import_45", "remote_wrapper_84"])
def test_remote_program_selects_code_before_import_and_wrapper(tmp_path, monkeypatch, topology, site):
    runtime, expected = tree(tmp_path, topology)
    # Execute the actual remote program locally. All provider and process edges
    # are inert fakes; no SSH, credential file, or service is touched.
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"runtime_root": str(runtime), "prompt": "fixture",
                       "model": "fixture", "effort": "medium", "attempts": 1})))
    monkeypatch.setattr(sys, "path", list(sys.path))
    def environment(_runtime):
        if site == "remote_import_45":
            observe(sys.path[0])
        return {"CPA_BASE_URL": "https://example.invalid/v1", "CPA_API_KEY": "fixture-only"}
    monkeypatch.setattr(llm_client, "runtime_cpa_command_environment", environment)
    @contextmanager
    def slot(**kw):
        yield None
    monkeypatch.setattr(provider_slots, "runtime_provider_slot", slot)
    import subprocess
    monkeypatch.setattr(subprocess, "run", lambda command, **kw: observe(Path(command[1]).parents[1]))
    with pytest.raises(Reached) as caught:
        exec(compile(bridge.REMOTE_PROGRAM, "<remote-program-fixture>", "exec"), {})
    assert caught.value.args[0] == expected


@pytest.mark.parametrize("surface", ["registry", "state_authority"])
def test_real_recovery_preflight_and_revalidation_ignore_stale_assets(tmp_path, monkeypatch, surface):
    plan = published_fixtures._plan(tmp_path)
    runtime = tmp_path / "runtime"
    before = published_fixtures._preflight_for_materialization(plan, runtime, monkeypatch)
    repo = runtime / "versions" / SHA / "repo"
    shutil.copytree(runtime / "repo", repo)
    (repo / "DEPLOYED_MANIFEST.json").write_text("{}")
    (runtime / "current").symlink_to(f"versions/{SHA}")
    relative = recovery.REGISTRY_REPO_PATH if surface == "registry" else contract.STATE_AUTHORITY_REPO_PATH
    (runtime / "repo" / relative).write_text("stale asset")
    current = recovery.published_recovery_preflight(plan, runtime_root=runtime, expected_bvid=before.bvid)
    assert current == before
    recovery._revalidate_inputs(plan, current, runtime_root=runtime)
    # Existing hash guards must still reject drift in the actual deployed tree.
    (repo / relative).write_text("drifted current asset")
    with pytest.raises(replay.ReviewedBaselineReplayError):
        recovery.published_recovery_preflight(plan, runtime_root=runtime, expected_bvid=before.bvid)
    with pytest.raises(replay.ReviewedBaselineReplayError):
        recovery._revalidate_inputs(plan, current, runtime_root=runtime)


@pytest.mark.parametrize("topology", ["version", "legacy"])
def test_remote_program_imports_and_runs_only_selected_fixture_tree(tmp_path, topology):
    runtime, _ = tree(tmp_path / "request root with spaces", topology)
    repos = [runtime / "repo"]
    if topology == "version":
        repos.append(runtime / "versions" / SHA / "repo")
    successor = runtime / "versions" / ("c" * 40) / "repo"
    successor.mkdir(parents=True)
    (successor / "TREE").write_text("successor")
    (successor / "DEPLOYED_COMMIT").write_text("c" * 40 + "\n")
    repos.append(successor)
    for repo in repos:
        label = (repo / "TREE").read_text()
        package = repo / "src" / "autoslice"
        package.mkdir(parents=True)
        (repo / "src" / "__init__.py").write_text("")
        (package / "__init__.py").write_text("")
        (package / "llm_client.py").write_text(
            "def runtime_cpa_command_environment(runtime):\n"
            "    return {'CPA_BASE_URL': 'https://example.invalid/v1', "
            "'CPA_API_KEY': 'fixture-only', 'FIXTURE_IMPORT': " + repr(label) + "}\n")
        (package / "provider_slots.py").write_text(
            "from contextlib import contextmanager\n"
            "@contextmanager\n"
            "def runtime_provider_slot(**kwargs):\n"
            "    yield None\n")
        (package / "cpa_model_ladder.py").write_text(
            "def cpa_call_timeout_seconds(*args, **kwargs):\n"
            "    return 1140\n")
        scripts = repo / "scripts"
        scripts.mkdir()
        (scripts / "llm_via_cpa.sh").write_text(
            'printf "%s:%s" "$FIXTURE_IMPORT" ' + label + ' > "$2"\n')
    program = tmp_path / "remote-program-fixture.py"
    program.write_text(bridge.REMOTE_PROGRAM)
    result = subprocess.run([sys.executable, "-B", str(program)], cwd=tmp_path,
                            input=json.dumps({"runtime_root": str(runtime), "prompt": "fixture",
                                              "model": "fixture", "effort": "medium", "attempts": 1}),
                            text=True, capture_output=True, timeout=15, check=False)
    assert result.returncode == 0, result.stderr
    label = "version" if topology == "version" else "stale"
    assert json.loads(result.stdout)["completion"] == f"{label}:{label}"
    # Reuse exactly the same generated program and request after publication
    # switches. Both the import and wrapper must follow the new version.
    (runtime / "current").unlink(missing_ok=True)
    (runtime / "current").symlink_to("versions/" + "c" * 40)
    result = subprocess.run(result.args, cwd=tmp_path,
                            input=json.dumps({"runtime_root": str(runtime), "prompt": "fixture",
                                              "model": "fixture", "effort": "medium", "attempts": 1}),
                            text=True, capture_output=True, timeout=15, check=False)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["completion"] == "successor:successor"


def test_real_materialized_recovery_recheck_ignores_stale_assets(tmp_path, monkeypatch):
    published_fixtures.test_prepare_published_recovery_materializes_private_package_without_state_write(tmp_path, monkeypatch)
    runtime = tmp_path / "runtime"
    repo = runtime / "versions" / SHA / "repo"
    shutil.copytree(runtime / "repo", repo)
    (repo / "DEPLOYED_MANIFEST.json").write_text("{}")
    (runtime / "current").symlink_to(f"versions/{SHA}")
    for relative in (recovery.REGISTRY_REPO_PATH, contract.STATE_AUTHORITY_REPO_PATH):
        (runtime / "repo" / relative).write_text("stale asset")
    source = runtime / "out" / DATE / published_fixtures.CID / "replacement_recuts" / f"{published_fixtures.CID}.record.json"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes((tmp_path / "old-record.json").read_bytes())
    monkeypatch.setattr(contract, "REGISTRY_REPO_PATH", recovery.REGISTRY_REPO_PATH)
    monkeypatch.setattr(contract, "REGISTRY_SHA256", recovery.REGISTRY_SHA256)
    package = tmp_path / "operator-private" / published_fixtures.CID / "replacement_recuts"
    result = contract.validate_materialized_published_recovery_package(package)
    assert Path(result["path"]).is_file()
    (repo / contract.STATE_AUTHORITY_REPO_PATH).write_text("drifted current asset")
    with pytest.raises(ValueError, match="HASH_DRIFT"):
        contract.validate_materialized_published_recovery_package(package)


@pytest.mark.parametrize("location", ["source_root", "source_legacy", "source_version",
                                      "target_root", "target_legacy", "target_version"])
def test_recovery_planner_preserves_all_auto_upload_guards(tmp_path, monkeypatch, location):
    target, expected = tree(tmp_path / "target", "version")
    source, source_repo = tree(tmp_path / "source", "version")
    (target / "recordings").mkdir()
    roots = {"source_root": source, "source_legacy": source / "repo", "source_version": source_repo,
             "target_root": target, "target_legacy": target / "repo", "target_version": expected}
    marker = roots[location] / "AUTO_UPLOAD"
    marker.touch()
    fingerprint = "sha256:" + "1" * 64
    args = ["--source-base", str(source), "--target-base", str(target), "--date", DATE,
            "--expected-source-state-sha256", fingerprint, "--expected-old-fingerprint", fingerprint,
            "--expected-new-fingerprint", fingerprint, "--publication-authority-asset", "fixture.json",
            "--expected-publication-authority-sha256", fingerprint, "--candidate-id", "cid"]
    monkeypatch.setattr(planner, "__file__", str(expected / "scripts/plan_recovery_review_rerun.py"))
    with pytest.raises(SystemExit, match="AUTO_UPLOAD is forbidden"):
        planner.main(args)
