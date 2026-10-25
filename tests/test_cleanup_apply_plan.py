"""Current qualification and real mutex tests; all artifacts are disposable."""
from __future__ import annotations

import fcntl
import hashlib
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from src.autoslice import producer_batch_transaction as batch
from src.autoslice import producer_delivery_transaction as delivery
from src.autoslice import qixi_transaction_core as core
from src.autoslice.repository_asset_authority import _canonical_sha256
from src.autoslice.runner_state_writeback import state_bytes, write_exact_state_under_lease

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
apply = importlib.import_module("cleanup_apply_plan")
planner = importlib.import_module("cleanup_preflight_scan")
REAL_QUIET_WINDOW = planner.quiet_window
DATE, CID = "2026-10-13", "auto_100000_1_2"


@pytest.fixture
def case(tmp_path, monkeypatch):
    root = tmp_path.resolve() / "runtime"
    target = root / "out" / DATE / CID / "working.wav"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"original fixture")
    (root / "DISABLED").touch()
    (root / "state").mkdir()
    state = {"picks": [{"candidate_id": CID, "status": "failed"}],
             "pending_talk": [], "pending_song": []}
    state_path = root / "state" / f"{DATE}.json"
    state_path.write_bytes(state_bytes(state))
    plan = tmp_path / "approved-plan.json"
    monkeypatch.setattr(planner, "quiet_window", lambda _: [])
    monkeypatch.setattr(apply, "quiet_window", lambda _: [])
    monkeypatch.setattr(sys, "argv", ["cleanup_preflight_scan.py", "--base", str(root), "--json", str(plan)])
    assert planner.main() == 0
    assert [row["path"] for row in json.loads(plan.read_text())] == [str(target)]
    return {"root": root, "target": target, "state": state, "state_path": state_path, "plan": plan}


def invoke(case, monkeypatch, *extra):
    monkeypatch.setattr(sys, "argv", ["cleanup_apply_plan.py", str(case["plan"]),
                                      "--base", str(case["root"]), "--dry-run", *map(str, extra)])
    return apply.main()


def change_state(case, **fields):
    with core.exclusive_runner_commit(case["root"]) as lease:
        write_exact_state_under_lease(case["state_path"], runtime_root=case["root"], lease=lease,
                                     expected_before=case["state_path"].read_bytes(),
                                     after={**case["state"], **fields})


def fixture_authority(case):
    root = case["root"]
    repo = root / "repo"
    repo.mkdir(exist_ok=True)
    body = {"schema_version": "deployed-authority-manifest.v1", "deployed_commit": "a" * 40,
            "entries": {"docs/pipeline/80-package-delivery.md": {"bytes": 1, "sha256": "sha256:" + "b" * 64}}}
    (repo / "DEPLOYED_COMMIT").write_text("a" * 40 + "\n")
    (repo / "DEPLOYED_AUTHORITY_MANIFEST.json").write_text(json.dumps({**body, "manifest_sha256": _canonical_sha256(body)}))


@pytest.mark.parametrize("process,expected", [
    ("configuration", 0), ("ffmpeg", 2), ("ps_failed", 2), ("entry_unreadable", 2),
])
def test_planner_and_apply_share_real_quiet_gate(case, monkeypatch, process, expected):
    monkeypatch.setattr(planner, "quiet_window", REAL_QUIET_WINDOW)
    monkeypatch.setattr(apply, "quiet_window", REAL_QUIET_WINDOW)
    monkeypatch.setattr(sys, "platform", "linux")
    reply = {"configuration": "12 python3", "ffmpeg": "12 ffmpeg",
             "entry_unreadable": "12 python3", "ps_failed": ""}[process]
    real_run = subprocess.run
    def run(argv, **kwargs):
        if argv[0] == "ps":
            return subprocess.CompletedProcess(argv, int(process == "ps_failed"), reply, "")
        return real_run(argv, **kwargs)
    def entry(*args):
        if process == "entry_unreadable":
            raise PermissionError("synthetic necessary-entry denial")
        return ["python3", "-c", "import time; time.sleep(2)", "--ffmpeg", "/bin/ffmpeg"]
    monkeypatch.setattr(planner.subprocess, "run", run)
    monkeypatch.setattr(planner, "_process_argv", entry)
    original = case["target"].read_bytes()
    fresh = case["root"].parent / "quiet-plan.json"
    monkeypatch.setattr(sys, "argv", ["cleanup_preflight_scan.py", "--base", str(case["root"]),
                                      "--json", str(fresh)])
    assert planner.main() == expected
    assert fresh.exists() is (expected == 0)
    if expected == 0:
        case["plan"] = fresh
    assert invoke(case, monkeypatch) == expected
    assert case["target"].read_bytes() == original


def prepared(case, source=None, cid=CID):
    fixture_authority(case)
    root = case["root"]
    source = source or case["target"]
    return delivery.prepare_delivery(runtime_root=root, lane="talk", candidate_id=cid, artifacts=[
        delivery.DeliveryArtifact("video", source, root / "delivery" / f"{cid}.mp4",
                                  "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest())])


def test_new_reference_rejects_entire_approved_set(case, monkeypatch, capsys):
    rows = json.loads(case["plan"].read_text())
    second = case["target"].with_name("second.wav")
    second.write_bytes(b"second fixture")
    rows.insert(0, {**rows[0], "path": str(second), "bytes": second.stat().st_size,
                    "preimage": planner.capture_preimage(str(case["root"]), str(second))})
    case["plan"].write_text(json.dumps(rows))
    (case["root"] / "assets").mkdir()
    (case["root"] / "assets" / "new-reference.json").write_text(json.dumps({"source": str(case["target"])}))
    called = []
    monkeypatch.setattr(apply, "remove_if_matches", lambda *a, **k: called.append(a))
    assert invoke(case, monkeypatch) == 2
    assert called == [] and second.exists() and case["target"].exists()
    assert "gate2_authority_cited" in capsys.readouterr().err


def test_added_pending_under_same_live_lease_is_rejected(case, monkeypatch, capsys):
    with core.exclusive_runner_commit(case["root"]) as lease:
        write_exact_state_under_lease(case["state_path"], runtime_root=case["root"], lease=lease,
                                     expected_before=case["state_path"].read_bytes(),
                                     after={**case["state"], "pending_talk": [{"cid": CID}]})
        assert invoke(case, monkeypatch) == 2
        assert core.current_runner_commit_lease(runtime_root=case["root"]) is lease
    assert case["target"].exists()
    assert "gate3_owner_has_queued_work" in capsys.readouterr().err


def test_new_prepared_handle_is_rejected(case, monkeypatch, capsys):
    prepared(case)
    assert invoke(case, monkeypatch) == 2 and case["target"].exists()
    assert "gate2_authority_cited" in capsys.readouterr().err


def test_new_checkpoint_only_dependency_is_rejected(case, monkeypatch, capsys):
    if not hasattr(batch, "checkpoint_prepared_result"):
        pytest.skip("completion consumer not integrated in baseline")
    artifact = case["root"] / "finished.bin"
    artifact.write_bytes(b"prepared artifact")
    handle = prepared(case, artifact)
    result = {"status": "delivery_prepared_no_target", "prepared_delivery": {
        "manifest_path": str(handle.manifest_path), "prepared_sha256": "sha256:" + handle.prepared_sha256}}
    binding = {"date": DATE, "lane": "talk", "candidate_id": CID}
    batch.checkpoint_prepared_result(entry=batch.PreparedBatchEntry("talk", CID, handle), binding=binding,
                                     result=result, source_dependencies=batch.bind_prepared_result_inputs({case["target"]: True}))
    assert batch.load_prepared_result_checkpoint(runtime_root=case["root"], lane="talk", candidate_id=CID, binding=binding) == result
    assert invoke(case, monkeypatch) == 2 and case["target"].exists()
    assert "gate2_authority_cited" in capsys.readouterr().err


@pytest.mark.parametrize("status", ["making", "published", None])
def test_missing_or_changed_owner_basis_is_rejected(case, monkeypatch, status):
    if status is None:
        case["state_path"].rename(case["state_path"].with_suffix(".saved"))
    else:
        change_state(case, picks=[{"candidate_id": CID, "status": status}])
    assert invoke(case, monkeypatch) == 2 and case["target"].exists()


@pytest.mark.parametrize("bad", ["{broken", "[]", '{"pending_talk": {}}', '{"pending_song": [4]}'])
def test_invalid_state_is_not_silently_ignored(case, monkeypatch, bad, capsys):
    # Keep a valid terminal claim in one date: the other malformed current
    # document must still refuse qualification, not disappear from pending.
    (case["root"] / "state" / "2026-10-12.json").write_text(bad)
    assert invoke(case, monkeypatch) == 2 and case["target"].exists()
    error = capsys.readouterr().err
    assert "cleanup state" in error or "cleanup pending" in error


@pytest.mark.parametrize("marker", ["recovery", "recovery_file", "frozen"])
def test_added_in_flight_marker_is_rejected(case, monkeypatch, marker, capsys):
    if marker == "recovery":
        (case["root"] / "recovery" / DATE / CID).mkdir(parents=True)
    elif marker == "recovery_file":
        parent = case["root"] / "recovery" / DATE
        parent.mkdir(parents=True)
        (parent / CID).write_bytes(b"existing marker signal")
    else:
        (case["target"].parent / "replacement_recuts.pre-audit").mkdir()
    assert invoke(case, monkeypatch) == 2 and case["target"].exists()
    assert "gate3_owner_mid_replacement" in capsys.readouterr().err


def test_current_authority_scan_failure_rejects(case, monkeypatch):
    (case["root"] / "assets").mkdir()
    (case["root"] / "assets" / "broken.txt").write_bytes(b"\xff")
    assert invoke(case, monkeypatch) == 2 and case["target"].exists()


def test_state_drift_during_qualification_rejects(case, monkeypatch, capsys):
    native = planner._authority_text
    refs = planner.authority_references(str(case["root"]))
    def changing(path, expected):
        answer = native(path, expected)
        if path == str(case["state_path"]):
            case["state_path"].write_bytes(state_bytes({**case["state"], "pending_talk": [{"cid": CID}]}))
        return answer
    monkeypatch.setattr(planner, "_authority_text", changing)
    # Restrict to the state read after the actual reference scan, preserving
    # the reference scanner's own native checks before the injection.
    monkeypatch.setattr(apply, "authority_references", lambda _: refs)
    assert invoke(case, monkeypatch) == 2 and case["target"].exists()
    assert "state changed during qualification" in capsys.readouterr().err


def test_unchanged_plan_keeps_exact_scope_and_dry_bytes(case, monkeypatch):
    extra = case["target"].with_name("unapproved.wav")
    extra.write_bytes(b"unapproved file")
    before = planner.capture_preimage(str(case["root"]), str(case["target"]))
    manifest = case["plan"].with_name("dry-manifest.json")
    assert invoke(case, monkeypatch, "--manifest", manifest) == 0
    recorded = json.loads(manifest.read_text())
    assert recorded["paths"] == [str(case["target"])] and recorded["would_delete_files"] == 1
    assert recorded["deleted_files"] == recorded["freed_bytes"] == 0
    assert extra.exists() and planner.capture_preimage(str(case["root"]), str(case["target"])) == before


def test_target_drift_remains_rejected(case, monkeypatch, capsys):
    case["target"].write_bytes(b"modified fixture")
    assert invoke(case, monkeypatch) == 2 and case["target"].exists()
    assert "preimage verification failed" in capsys.readouterr().err


def scan_current(case, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["cleanup_preflight_scan.py", "--base", str(case["root"]),
                                     "--json", str(case["plan"])])
    rc = planner.main()
    return rc, json.loads(case["plan"].read_text())


@pytest.mark.parametrize("other_status", ["failed", "speaker_review_required"])
@pytest.mark.parametrize("other_work", ["none", "pending", "recovery", "recovery_file", "frozen"])
def test_other_date_same_cid_does_not_change_current_terminal_owner(
    case, monkeypatch, other_status, other_work
):
    other_day = "2026-10-12"
    doc = {"date": other_day, "picks": [{"candidate_id": CID, "status": other_status}],
           "pending_talk": [{"cid": CID}] if other_work == "pending" else []}
    (case["root"] / "state" / f"{other_day}.json").write_text(json.dumps(doc))
    if other_work == "recovery":
        (case["root"] / "recovery" / other_day / CID).mkdir(parents=True)
    elif other_work == "recovery_file":
        parent = case["root"] / "recovery" / other_day
        parent.mkdir(parents=True)
        (parent / CID).write_bytes(b"other-date existing marker signal")
    elif other_work == "frozen":
        (case["root"] / "out" / other_day / CID / "replacement_recuts.pre-test").mkdir(parents=True)
    before = case["target"].read_bytes()
    rc, rows = scan_current(case, monkeypatch)
    assert rc == 0 and len(rows) == 1
    assert rows[0]["owner_identity"] == {
        "runtime_root": str(case["root"]), "date": DATE, "candidate_id": CID,
    }
    assert invoke(case, monkeypatch) == 0 and case["target"].read_bytes() == before


def test_active_older_date_cannot_borrow_newer_terminal_state(case, monkeypatch):
    day = "2026-10-12"
    active = case["root"] / "out" / day / CID / "working.wav"
    active.parent.mkdir(parents=True)
    active.write_bytes(b"active dated candidate")
    (case["root"] / "state" / f"{day}.json").write_text(json.dumps({
        "date": day, "picks": [{"candidate_id": CID, "status": "speaker_review_required"}],
    }))
    rc, rows = scan_current(case, monkeypatch)
    assert rc == 0 and [row["path"] for row in rows] == [str(case["target"])]
    assert invoke(case, monkeypatch) == 0 and active.read_bytes() == b"active dated candidate"


def test_missing_dated_owner_cannot_borrow_terminal_or_accept_old_plan(case, monkeypatch):
    case["state_path"].write_text(json.dumps({"date": DATE, "picks": []}))
    (case["root"] / "state" / "2026-10-12.json").write_text(json.dumps(case["state"]))
    old_plan = case["plan"].read_bytes()
    assert invoke(case, monkeypatch) == 2
    assert case["plan"].read_bytes() == old_plan and case["target"].exists()
    assert scan_current(case, monkeypatch) == (0, [])


@pytest.mark.parametrize("claims", [
    ["failed", "speaker_review_required"], ["speaker_review_required", "failed"],
    ["failed", "published"], ["published", "failed"],
])
def test_conflicting_same_date_claims_hold_old_and_fresh_plan(case, monkeypatch, claims):
    case["state_path"].write_text(json.dumps({
        "picks": [{"candidate_id": CID, "status": status} for status in claims],
    }))
    assert invoke(case, monkeypatch) == 2 and case["target"].exists()
    assert scan_current(case, monkeypatch) == (0, [])


@pytest.mark.parametrize("location", ["document", "row", "pending"])
def test_explicit_state_date_mismatch_blocks_and_preserves_plan(case, monkeypatch, location):
    doc = json.loads(case["state_path"].read_text())
    if location == "document":
        doc["date"] = "2026-10-12"
    elif location == "row":
        doc["picks"][0]["date"] = "2026-10-12"
    else:
        doc["pending_talk"] = [{"cid": CID, "date": "2026-10-12"}]
    case["state_path"].write_text(json.dumps(doc))
    before = case["plan"].read_bytes()
    assert invoke(case, monkeypatch) == 2
    assert scan_current(case, monkeypatch)[0] == 2
    assert case["plan"].read_bytes() == before and case["target"].exists()


@pytest.mark.parametrize("field", ["day", "owner", "root", "identity_day", "identity_cid", "missing"])
def test_plan_identity_claim_must_match_actual_path(case, monkeypatch, field):
    rows = json.loads(case["plan"].read_text())
    row = rows[0]
    if field == "missing":
        row.pop("owner_identity")
    elif field in {"day", "owner"}:
        row[field] = "2026-10-12" if field == "day" else "auto_999999_9_10"
    else:
        key = {"root": "runtime_root", "identity_day": "date", "identity_cid": "candidate_id"}[field]
        row["owner_identity"][key] = {"root": str(case["root"].parent),
                                     "identity_day": "2026-10-12",
                                     "identity_cid": "auto_999999_9_10"}[field]
    case["plan"].write_text(json.dumps(rows))
    before = case["plan"].read_bytes()
    assert invoke(case, monkeypatch) == 2
    assert case["target"].exists() and case["plan"].read_bytes() == before


def test_unrelated_cid_text_in_root_or_nested_path_is_not_an_owner(tmp_path, monkeypatch):
    root = tmp_path.resolve() / "auto_999999_9_10" / "runtime"
    target = root / "out" / DATE / CID / "song_999999_7" / "working.wav"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"synthetic nested intermediate")
    (root / "state").mkdir()
    (root / "state" / f"{DATE}.json").write_text(json.dumps({
        "picks": [{"candidate_id": CID, "status": "failed"}],
    }))
    local = {"root": root, "target": target, "plan": tmp_path / "plan.json"}
    monkeypatch.setattr(planner, "quiet_window", lambda _: [])
    monkeypatch.setattr(apply, "quiet_window", lambda _: [])
    rc, rows = scan_current(local, monkeypatch)
    assert rc == 0 and len(rows) == 1 and rows[0]["owner"] == CID
    assert invoke(local, monkeypatch) == 0 and target.exists()


def test_candidate_text_outside_actual_candidate_component_is_held(case, monkeypatch):
    stray = case["root"] / "out" / DATE / "not-an-owner" / CID / "working.wav"
    stray.parent.mkdir(parents=True)
    stray.write_bytes(b"unbound synthetic file")
    rc, rows = scan_current(case, monkeypatch)
    assert rc == 0 and [row["path"] for row in rows] == [str(case["target"])]
    row = rows[0]
    row.update(path=str(stray), bytes=stray.stat().st_size,
               preimage=planner.capture_preimage(str(case["root"]), str(stray)))
    case["plan"].write_text(json.dumps([row]))
    assert invoke(case, monkeypatch) == 2 and stray.exists()


def test_stale_plan_current_active_stays_held_despite_other_date_terminal(case, monkeypatch):
    (case["root"] / "state" / "2026-10-12.json").write_text(json.dumps(case["state"]))
    change_state(case, picks=[{"candidate_id": CID, "status": "speaker_review_required"}])
    old_plan = case["plan"].read_bytes()
    assert invoke(case, monkeypatch) == 2 and case["target"].exists()
    assert case["plan"].read_bytes() == old_plan


def test_conflicting_pending_candidate_claims_refuse(case, monkeypatch):
    doc = {**case["state"], "pending_talk": [{"cid": CID, "candidate_id": "auto_999999_9_10"}]}
    case["state_path"].write_text(json.dumps(doc))
    old_plan = case["plan"].read_bytes()
    assert invoke(case, monkeypatch) == 2 and case["target"].exists()
    assert scan_current(case, monkeypatch)[0] == 2
    assert case["plan"].read_bytes() == old_plan


def subprocess_attempt(root):
    code = '''import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from src.autoslice.qixi_transaction_core import exclusive_runner_commit
try:
    with exclusive_runner_commit(Path(sys.argv[2])): print("ACQUIRED")
except ValueError as exc: print(str(exc))
'''
    return subprocess.run([sys.executable, "-c", code, str(Path(core.__file__).resolve().parents[2]), str(root)],
                          capture_output=True, text=True, timeout=5,
                          env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})


def native_apply(case, *extra, pass_fds=()):
    # Import the exact integration consumer tree before the isolated apply
    # source. The baseline checkout intentionally lacks A's writer-side API.
    code = '''import sys
sys.path.insert(0, sys.argv[1])
from src.autoslice import qixi_transaction_core
sys.path.insert(0, sys.argv[2])
import cleanup_apply_plan
sys.argv = ["cleanup_apply_plan.py", *sys.argv[3:]]
raise SystemExit(cleanup_apply_plan.main())
'''
    return subprocess.run([sys.executable, "-c", code, str(Path(core.__file__).resolve().parents[2]),
                           str(ROOT / "scripts"), str(case["plan"]), "--base", str(case["root"]),
                           "--dry-run", *map(str, extra)], pass_fds=pass_fds,
                          capture_output=True, text=True, timeout=5,
                          env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})


def test_lock_stays_held_from_qualification_through_remove(case, monkeypatch):
    checks = []
    native_qualify, native_remove = apply.authority_references, apply.remove_if_matches
    def qualify(root):
        checks.append(subprocess_attempt(case["root"]).stdout.strip())
        return native_qualify(root)
    def remove(*args, **kwargs):
        checks.append(subprocess_attempt(case["root"]).stdout.strip())
        return native_remove(*args, **kwargs)
    monkeypatch.setattr(apply, "authority_references", qualify)
    monkeypatch.setattr(apply, "remove_if_matches", remove)
    assert invoke(case, monkeypatch) == 0
    assert checks == ["runner lock busy", "runner lock busy"]
    assert subprocess_attempt(case["root"]).stdout.strip() == "ACQUIRED"


def test_reuses_live_embedding_without_unlocking_it(case, monkeypatch):
    with core.exclusive_runner_commit(case["root"]) as lease:
        assert invoke(case, monkeypatch) == 0
        assert core.current_runner_commit_lease(runtime_root=case["root"]) is lease
        assert subprocess_attempt(case["root"]).stdout.strip() == "runner lock busy"


def test_inherited_fd_native_cli_and_parent_lock_lifetime(case):
    fd = os.open(case["root"] / "runner.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = native_apply(case, "--runner-lock-fd", fd, pass_fds=(fd,))
        assert result.returncode == 0, result.stdout + result.stderr
        assert "would delete 1 files" in result.stdout
        assert subprocess_attempt(case["root"]).stdout.strip() == "runner lock busy"
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    assert subprocess_attempt(case["root"]).stdout.strip() == "ACQUIRED"


def test_external_lock_without_fd_refuses_without_deadlock(case):
    fd = os.open(case["root"] / "runner.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = native_apply(case)
        assert result.returncode == 2 and "runner lock busy" in result.stderr
        assert "--runner-lock-fd" in result.stderr and case["target"].exists()
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def test_wrong_inherited_fd_refuses(case, monkeypatch):
    fd = os.open(case["target"], os.O_RDONLY)
    try:
        assert invoke(case, monkeypatch, "--runner-lock-fd", fd) == 2
        assert case["target"].exists()
    finally:
        os.close(fd)


def test_replaced_lock_inode_rejects_before_remove(case, monkeypatch):
    native = apply.in_flight_candidates
    def replaced(root):
        result = native(root)
        lock = case["root"] / "runner.lock"
        lock.rename(lock.with_name("retained-runner.lock"))
        lock.touch(mode=0o600)
        return result
    monkeypatch.setattr(apply, "in_flight_candidates", replaced)
    assert invoke(case, monkeypatch) == 2 and case["target"].exists()


def test_missing_private_writer_exclusion_refuses(case, monkeypatch, capsys):
    monkeypatch.delattr(core, "_recovery_reference_window", raising=False)
    assert invoke(case, monkeypatch) == 2 and case["target"].exists()
    assert "writer exclusion is not integrated" in capsys.readouterr().err


def private_writer_attempt(case, mode, handle=None):
    # A real independent process calls exactly the private API that raced the
    # original planner/apply. No provider, service or real runtime is involved.
    payload = {"root": str(case["root"]), "source": str(case["target"]), "mode": mode,
               "manifest": str(handle.manifest_path) if handle else None,
               "dependencies": batch.bind_prepared_result_inputs({case["target"]: True})}
    code = '''import hashlib, json, sys
from pathlib import Path
sys.dont_write_bytecode = True
sys.path.insert(0, sys.argv[1])
from src.autoslice import producer_delivery_transaction as d, producer_batch_transaction as b
from src.autoslice.qixi_transaction_core import exclusive_runner_commit
p = json.loads(sys.argv[2]); root, source = Path(p["root"]), Path(p["source"])
cid = "auto_100000_1_2"
try:
    if p["mode"] == "prepare":
        handle = d.prepare_delivery(runtime_root=root, lane="talk", candidate_id=cid,
                    artifacts=[d.DeliveryArtifact("video", source, root/"delivery"/(cid+".mp4"),
                    "sha256:"+hashlib.sha256(source.read_bytes()).hexdigest())])
    else:
        handle = d.load_prepared_delivery(runtime_root=root, manifest_path=Path(p["manifest"]))
    result = {"status": "delivery_prepared_no_target", "prepared_delivery": {
        "manifest_path": str(handle.manifest_path), "prepared_sha256": "sha256:"+handle.prepared_sha256}}
    binding = {"date": "2026-10-13", "lane": "talk", "candidate_id": cid}
    b.checkpoint_prepared_result(entry=b.PreparedBatchEntry("talk", cid, handle), binding=binding,
                                result=result, source_dependencies=p["dependencies"])
    assert b.load_prepared_result_checkpoint(runtime_root=root, lane="talk", candidate_id=cid, binding=binding) == result
    with exclusive_runner_commit(root) as lease:
        d.preflight_prepared_delivery(handle=handle, lease=lease)
    print(json.dumps({"blocked": False, "checkpoint_replay": "PASS", "formal_preflight": "PASS"}))
except (d.ProducerDeliveryTransactionError, b.ProducerBatchTransactionError) as exc:
    print(json.dumps({"blocked": True, "error": str(exc)}))
'''
    result = subprocess.run([sys.executable, "-c", code,
                             str(Path(delivery.__file__).resolve().parents[2]), json.dumps(payload)],
                            capture_output=True, text=True, timeout=10,
                            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize("mode", ["prepare", "checkpoint"])
def test_private_writer_conflicts_after_qualification_then_recovers(case, monkeypatch, mode):
    fixture_authority(case)
    handle = None
    if mode == "checkpoint":
        artifact = case["root"] / "finished.bin"
        artifact.write_bytes(b"unrelated prepared artifact")
        handle = prepared(case, artifact)
    observed = []
    native = apply.remove_if_matches
    def before_remove(*args, **kwargs):
        answer = private_writer_attempt(case, mode, handle)
        observed.append(answer)
        assert answer["blocked"] and "recovery reference lock busy" in answer["error"]
        assert list(case["root"].glob(".prepared-deliveries/*/*/*/dispatch-result-*.json")) == []
        if mode == "prepare":
            assert list(case["root"].glob(".prepared-deliveries/*/*/*/prepared.json")) == []
        return native(*args, **kwargs)
    monkeypatch.setattr(apply, "remove_if_matches", before_remove)
    assert invoke(case, monkeypatch) == 0 and len(observed) == 1 and case["target"].exists()
    resumed = private_writer_attempt(case, mode, handle)
    assert resumed == {"blocked": False, "checkpoint_replay": "PASS", "formal_preflight": "PASS"}
    # Normal recovery has now created a genuine dependency: a second apply
    # must reject rather than reuse the old approved plan.
    monkeypatch.setattr(apply, "remove_if_matches", native)
    assert invoke(case, monkeypatch) == 2 and case["target"].exists()


def test_writer_first_refuses_maintenance_then_releases(case, monkeypatch):
    with core.recovery_reference_write_window(case["root"]):
        result = native_apply(case)
        assert result.returncode == 2 and "recovery reference lock busy" in result.stderr
    assert invoke(case, monkeypatch) == 0 and case["target"].exists()
