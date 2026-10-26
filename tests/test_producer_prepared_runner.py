import hashlib
import json
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.autoslice import producer_prepared_runner as runner_module


DATE = "2026-10-15"
CID = "prepared-candidate"


def _raw_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _resume_root(spec_path: Path, spec_sha256: str, prepared_path: Path) -> Path:
    spec = json.loads(spec_path.read_text())
    identity = {
        "spec_sha256": spec_sha256,
        "prepared_manifest_sha256": _raw_sha(prepared_path),
    }
    digest = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return Path(spec["output_root"]) / spec["candidate_id"] / ".prepared-resumes" / digest


def _case(tmp_path: Path, *, spec_overrides: dict | None = None):
    base = (tmp_path / "runtime").resolve()
    repo = (tmp_path / "repo").resolve()
    source = base / "recordings" / "source.mp4"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"source bytes")
    output_root = base / "out" / DATE
    output_root.mkdir(parents=True)
    repo_scripts = repo / "scripts"
    repo_scripts.mkdir(parents=True)
    (repo_scripts / "produce_slice_package.py").write_text("# fake command\n")

    prepared = base / "prepared-input.json"
    prepared.write_bytes(b"original prepared manifest")
    spec_path = output_root / f"spec_{CID}.json"
    spec = {
        "candidate_id": CID,
        "date": DATE,
        "runtime_root": str(base),
        "output_root": str(output_root),
        "pieces": [
            {
                "remote_media": str(source),
                "start_ms": -400,
                "end_ms": 2_400,
            }
        ],
    }
    if spec_overrides:
        spec.update(spec_overrides)
    spec_path.write_text(json.dumps(spec, sort_keys=True), encoding="utf-8")

    capsule = {
        "schema_version": "producer-prepared-runner-recovery.v1",
        "spec_path": str(spec_path),
        "spec_sha256": _raw_sha(spec_path),
        "prepared_path": str(prepared),
        "prepared_manifest_sha256": _raw_sha(prepared),
        "worker_store_id": "a" * 32,
    }
    item = {
        "cid": CID,
        "candidate_id": CID,
        "segment_path": str(source),
        "session_id": "session-1",
        "start_ms": 800,
        "end_ms": 1_800,
        "hook": "a recovered story",
        "confidence": 0.92,
        "selection_scorecard": {"story": 1},
        "session_relation_authority": {"status": "PASS"},
        "lane": "talk",
        "pieces": [{"remote_media": str(source), "start_ms": 800, "end_ms": 1_800}],
        "prepared_recovery": capsule,
    }
    runner = SimpleNamespace(
        BASE=base,
        REPO_ROOT=repo,
        last_json_block=lambda text: json.loads(text.strip().splitlines()[-1]),
        classify_talk_failure=lambda _text: {
            "failure_kind": "producer_failed",
            "failure_stage": "producer",
            "failure_recoverable": True,
        },
        talk_pipeline_fingerprint=lambda candidate_id: f"fingerprint:{candidate_id}",
        child_env_for_date=lambda date: {"DATE_FROM_RUNNER": date},
    )
    return runner, item, spec_path, prepared, base, repo


def _native_summary(base: Path, *, candidate_id: str = CID, upload_enabled=False):
    manifest = base / ".prepared-deliveries" / "talk" / candidate_id / ("b" * 64) / "prepared.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_bytes(b"native prepared handle")
    return {
        "candidate_id": candidate_id,
        "prepared_delivery": {
            "manifest_path": str(manifest),
            "prepared_sha256": "sha256:" + "c" * 64,
            "upload_enabled": upload_enabled,
        },
        "native_budget": {"provider_calls": 3},
    }


@pytest.mark.parametrize("ordinary_observer", [False, True])
def test_prepared_runner_uses_exact_private_command_and_projects_native_summary(tmp_path, monkeypatch, ordinary_observer):
    runner, item, spec_path, prepared, base, repo = _case(tmp_path)
    monkeypatch.setattr(runner_module, "canonical_prepared_resume_output_root", _resume_root)
    spec_before = spec_path.read_bytes()
    prepared_before = prepared.read_bytes()
    observed = {}
    summary = _native_summary(base)

    def fake_run(command, **kwargs):
        observed["command"] = command
        observed["kwargs"] = kwargs
        kwargs["stdout"].write(json.dumps(summary) + "\n")
        kwargs["stdout"].flush()
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(runner_module.subprocess, "run", fake_run)
    from src.autoslice.production_attempts import observed_production_attempt
    trace = _resume_root(spec_path, item["prepared_recovery"]["spec_sha256"], prepared) / CID / f"{CID}.cpa-usage.jsonl"
    observer = observed_production_attempt(runtime_root=base, date=DATE, candidate_id=CID,
        lane="talk", trace=trace) if ordinary_observer else nullcontext()
    with observer:
        result = runner_module.produce_prepared_talk(
            runner=runner,
            date=DATE,
            item=item,
            prepare_only=True,
            reuse_cover=False,
            child_env={"FROM_ROOT": "yes"},
        )

    derived = _resume_root(
        spec_path, item["prepared_recovery"]["spec_sha256"], prepared
    )
    assert observed["command"] == [
        runner_module.sys.executable,
        str(repo / "scripts" / "produce_slice_package.py"),
        "--spec",
        str(spec_path),
        "--resume-prepared",
        str(prepared),
        "--resume-spec-sha256",
        item["prepared_recovery"]["spec_sha256"],
        "--resume-output-root",
        str(derived),
        "--prepare-only",
        "--correct",
        "cpa",
        "--ssh-host",
        "localhost",
        "--speaker-mode",
        "uniform_host",
    ]
    assert observed["kwargs"]["cwd"] == str(repo)
    assert observed["kwargs"]["check"] is False
    assert observed["kwargs"]["stderr"] is runner_module.subprocess.STDOUT
    assert observed["kwargs"]["env"]["FROM_ROOT"] == "yes"
    assert observed["kwargs"]["env"]["AUTOSLICE_CPA_USAGE_FILE"] == str(
        derived / CID / f"{CID}.cpa-usage.jsonl"
    )
    assert result["status"] == "delivery_prepared_no_target"
    assert result["candidate_id"] == CID
    assert result["prepared_summary"] == summary
    assert result["prepared_summary"]["candidate_id"] == CID
    assert result["prepared_summary"]["prepared_delivery"]["upload_enabled"] is False
    assert result["prepared_recovery_proof"] == {
        "schema_version": "producer-prepared-runner-recovery-proof.v1",
        "spec_sha256": item["prepared_recovery"]["spec_sha256"],
        "prepared_manifest_sha256": item["prepared_recovery"]["prepared_manifest_sha256"],
    }
    assert "prepared_recovery" not in result
    assert spec_path.read_bytes() == spec_before
    assert prepared.read_bytes() == prepared_before
    log = Path(result["log"])
    assert log.parent == derived and log.name.startswith("producer-") and log.suffix == ".log"
    assert json.loads(log.read_text().strip()) == summary


def test_prepared_runner_keeps_each_attempt_log_immutable(tmp_path, monkeypatch):
    runner, item, _spec_path, _prepared, base, _repo = _case(tmp_path)
    monkeypatch.setattr(runner_module, "canonical_prepared_resume_output_root", _resume_root)
    summaries = [_native_summary(base), _native_summary(base)]
    summaries[0]["attempt"] = 1
    summaries[1]["attempt"] = 2
    calls = []

    def fake_run(_command, **kwargs):
        summary = summaries[len(calls)]
        calls.append(summary)
        kwargs["stdout"].write(json.dumps(summary) + "\n")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(runner_module.subprocess, "run", fake_run)
    first = runner_module.produce_prepared_talk(
        runner=runner, date=DATE, item=item, prepare_only=True, reuse_cover=False
    )
    second = runner_module.produce_prepared_talk(
        runner=runner, date=DATE, item=item, prepare_only=True, reuse_cover=False
    )
    assert first["prepared_summary"] == summaries[0]
    assert second["prepared_summary"] == summaries[1]
    assert first["log"] != second["log"]
    assert Path(first["log"]).parent == Path(second["log"]).parent
    assert json.loads(Path(first["log"]).read_text()) == summaries[0]
    assert json.loads(Path(second["log"]).read_text()) == summaries[1]


def test_failed_attempt_logs_survive_ordinary_export_and_sequential_import(tmp_path, monkeypatch):
    from tests.test_overflow_bundle import _seal
    from src.autoslice.channel_profile import binding_for_profile, load_channel_profile
    from src.autoslice.overflow_bundle import export_bundle, import_bundle
    runner, item, _spec, _prepared, base, _repo = _case(tmp_path)
    # This fixture's ordinary segment is a bounded generated candidate input,
    # so the full native runner result uses the real bundle allowlist.
    source = Path(item['segment_path'])
    bounded_source = base/'out'/DATE/CID/'input-source.mp4'
    bounded_source.parent.mkdir()
    source.rename(bounded_source)
    item['segment_path'] = str(bounded_source)
    item['pieces'][0]['remote_media'] = str(bounded_source)
    document = json.loads(_spec.read_text())
    document['pieces'][0]['remote_media'] = str(bounded_source)
    _spec.write_text(json.dumps(document))
    item['prepared_recovery']['spec_sha256'] = _raw_sha(_spec)
    _seal(base)
    binding = binding_for_profile(load_channel_profile(base/'repo', environ={}))
    monkeypatch.setattr(runner_module, 'canonical_prepared_resume_output_root', _resume_root)
    calls = []
    def run(_command, **kwargs):
        calls.append(len(calls) + 1)
        kwargs['stdout'].write(json.dumps({'failed_attempt': calls[-1]}) + '\n')
        return SimpleNamespace(returncode=1)
    monkeypatch.setattr(runner_module.subprocess, 'run', run)
    rows = []
    for number in (1, 2):
        result = runner_module.produce_prepared_talk(runner=runner, date=DATE,
            item=item, prepare_only=True, reuse_cover=False)
        identity = dict(job_id='job-1', attempt_id=f'attempt-{number}',
            release_sha256='sha256:'+'1'*64, input_sha256='sha256:'+'2'*64)
        result.update(identity, profile_binding=binding)
        bundle = tmp_path/f'bundle-{number}'
        export_bundle(result, base, bundle_dir=bundle, **identity,
            date=DATE, lane='talk', expected_profile_binding=binding)
        log = Path(result['log'])
        rows.append((bundle, identity, log, log.read_bytes()))
    assert rows[0][2] != rows[1][2]
    for _, _, log, _ in rows:
        log.unlink()  # Simulate logs not yet materialized on the controller.
    for bundle, identity, log, original in rows:
        imported = import_bundle(bundle, base, identity['job_id'], identity['attempt_id'],
            identity['release_sha256'], identity['input_sha256'], expected_profile_binding=binding)
        assert imported['rc'] == 1 and imported['status'] == 'failed'
        assert Path(imported['log']) == log and log.read_bytes() == original
    assert all(log.read_bytes() == original for _, _, log, original in rows)


@pytest.mark.parametrize("damage", ["spec_sha", "candidate", "date", "runtime", "public", "source"])
def test_prepared_runner_rejects_hash_or_scope_drift(tmp_path, damage):
    overrides = {}
    date = DATE
    runner, item, spec_path, prepared, base, _repo = _case(tmp_path)
    if damage == "spec_sha":
        item["prepared_recovery"]["spec_sha256"] = "0" * 64
    elif damage == "candidate":
        spec = json.loads(spec_path.read_text())
        spec["candidate_id"] = "other-candidate"
        spec_path.write_text(json.dumps(spec, sort_keys=True))
        item["prepared_recovery"]["spec_sha256"] = _raw_sha(spec_path)
    elif damage == "date":
        date = "2026-10-16"
    elif damage == "runtime":
        overrides["runtime_root"] = str(base / "other-runtime")
    elif damage == "public":
        overrides["output_root"] = str(base / "public-output")
    elif damage == "source":
        overrides["pieces"] = [{"remote_media": str(base / "foreign.mp4")}]
    if overrides:
        spec = json.loads(spec_path.read_text())
        spec.update(overrides)
        spec_path.write_text(json.dumps(spec, sort_keys=True))
        item["prepared_recovery"]["spec_sha256"] = _raw_sha(spec_path)

    with pytest.raises(ValueError, match="PREPARED_RUNNER_"):
        runner_module.produce_prepared_talk(
            runner=runner,
            date=date,
            item=item,
            prepare_only=True,
            reuse_cover=False,
        )


@pytest.mark.parametrize(
    "field,value",
    [("prepare_only", False), ("reuse_cover", True)],
)
def test_prepared_runner_requires_private_prepare_contract(tmp_path, field, value):
    runner, item, _spec_path, _prepared, _base, _repo = _case(tmp_path)
    kwargs = {"prepare_only": True, "reuse_cover": False}
    kwargs[field] = value
    with pytest.raises(ValueError, match="REQUIRES_PRIVATE_PREPARE_WITHOUT_COVER_REUSE"):
        runner_module.produce_prepared_talk(
            runner=runner, date=DATE, item=item, **kwargs
        )


@pytest.mark.parametrize(
    "summary_mutation",
    [
        lambda summary, _base: summary.pop("prepared_delivery"),
        lambda summary, _base: summary.__setitem__("candidate_id", "wrong-candidate"),
        lambda summary, _base: summary["prepared_delivery"].__setitem__("upload_enabled", True),
        lambda summary, base: summary["prepared_delivery"].__setitem__(
            "manifest_path", str(base / "public" / "prepared.json")
        ),
    ],
)
def test_prepared_runner_rejects_missing_or_invalid_native_summary(
    tmp_path, monkeypatch, summary_mutation
):
    runner, item, _spec_path, _prepared, base, _repo = _case(tmp_path)
    monkeypatch.setattr(runner_module, "canonical_prepared_resume_output_root", _resume_root)
    summary = _native_summary(base)
    summary_mutation(summary, base)

    def fake_run(_command, **kwargs):
        kwargs["stdout"].write(json.dumps(summary) + "\n")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(runner_module.subprocess, "run", fake_run)
    with pytest.raises(ValueError, match="PREPARED_RUNNER_"):
        runner_module.produce_prepared_talk(
            runner=runner, date=DATE, item=item, prepare_only=True, reuse_cover=False
        )


def test_prepared_runner_returns_real_classified_failure_without_prepared_projection(
    tmp_path, monkeypatch
):
    runner, item, _spec_path, _prepared, _base, _repo = _case(tmp_path)
    monkeypatch.setattr(runner_module, "canonical_prepared_resume_output_root", _resume_root)

    def fake_run(_command, **kwargs):
        kwargs["stdout"].write("producer failed\n{}\n")
        return SimpleNamespace(returncode=17)

    monkeypatch.setattr(runner_module.subprocess, "run", fake_run)
    result = runner_module.produce_prepared_talk(
        runner=runner, date=DATE, item=item, prepare_only=True, reuse_cover=False
    )
    assert result["status"] == "failed"
    assert result["rc"] == 17
    assert result["failure_kind"] == "producer_failed"
    assert "prepared_summary" not in result
    assert "prepared_recovery_proof" not in result
