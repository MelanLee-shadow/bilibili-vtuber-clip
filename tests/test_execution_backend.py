import json
import shutil
from pathlib import Path

import pytest

from src.autoslice.execution_backend import LocalBackend, SSHBackend
from src.autoslice.execution_backend import backend_from_environment
from src.autoslice.produce_dispatch import produce_batch_windowed
from src.autoslice.runtime_storage import CapacityAdmission, CapacityProbeError


def capacity(base, free):
    return CapacityAdmission(runtime_base=base, free_bytes_fn=lambda _: free,
        operational_reserve_bytes=2 * 1024**3)


def make_backend(base, monkeypatch, *, profile_id="lidousha"):
    monkeypatch.delenv("AUTOSLICE_PROFILE_BINDING", raising=False)
    repo = base / "repo"
    repo.mkdir(parents=True)
    profile_path = repo / "profiles/lidousha/profile.json"
    profile_path.parent.mkdir(parents=True)
    shutil.copy2(Path(__file__).resolve().parents[1] / "profiles/lidousha/profile.json", profile_path)
    if profile_id != "lidousha":
        document = json.loads(profile_path.read_text())
        document["profile_id"] = profile_id
        document["identity"]["output_directory"] = profile_id
        profile_path = repo / "profiles" / profile_id / "profile.json"
        profile_path.parent.mkdir(parents=True)
        profile_path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n")
    (repo / "DEPLOYED_COMMIT").write_text("a" * 40)
    (repo / "DEPLOYED_AUTHORITY_MANIFEST.json").write_text("{}")
    from src.autoslice.channel_profile import binding_for_profile, load_channel_profile
    profile = load_channel_profile(repo_root=repo, profile_id=profile_id, environ={})
    profile_binding = binding_for_profile(profile)
    from src.autoslice.overflow_release import seal
    release = seal(repo)
    backend = SSHBackend(base, {"mode": "auto", "release_sha256": release,
        "profile_binding": profile_binding,
        "nodes": [{"host": "jp", "runtime_root": str(base), "worker_root": "/data/control"}]}, log=lambda _: None)
    monkeypatch.setattr(backend, "_rpc", lambda *a, **kw: {"release_sha256": release,
        "slot_busy": False, "available_bytes": 90 * 1024**3, "startup_ok": True,
        "worker_root": "/data/control", "runtime_root": str(base), "worker_store_id": "c" * 32})
    media = base / "source.mp4"
    media.write_bytes(b"source")
    return backend, {"cid": "clip", "segment_path": str(media), "start_ms": 0, "end_ms": 60000}


def test_local_preserves_original_callable_kwargs_and_admission(tmp_path):
    backend = LocalBackend()
    item = {"cid": "clip", "start_ms": 0, "end_ms": 60000}
    lease, _ = backend.reserve(capacity(tmp_path, 8 * 1024**3), date="2026-10-05", item=item,
        admission_item=item, lane="talk", prepare_only=True)
    seen = []
    def produce(date, row, **kw):
        seen.append((date, row, kw))
        return {"status": "prepared"}
    assert backend.execute(lease, produce, "2026-10-05", item, {"prepare_only": True}) == {"status": "prepared"}
    assert seen == [("2026-10-05", item, {"prepare_only": True})]
    lease.release()
    assert lease.reservation.controller.reserved_bytes == 0


def test_low_disk_routes_before_original_local_admission(tmp_path, monkeypatch):
    backend, item = make_backend(tmp_path, monkeypatch)
    admission = capacity(tmp_path, 2 * 1024**3 + 512 * 1024**2)
    assert admission.reserve_for_item(item)[0] is None
    lease, details = backend.reserve(admission, date="2026-10-05", item=item,
        admission_item=item, lane="talk", prepare_only=True)
    assert lease.node["host"] == "jp"
    assert details["backend"] == "ssh"
    lease.release()
    assert admission.reserved_bytes == 0


@pytest.mark.parametrize("store", ["c" * 32, "d" * 32, "e" * 32])
def test_prepared_recovery_uses_bound_store_and_never_local_fallback(tmp_path, monkeypatch, store):
    backend, item = make_backend(tmp_path, monkeypatch)
    backend.nodes.append({**backend.nodes[0], "host": "sjc"})
    original_probe = backend._rpc
    def probe(node, *args, **kwargs):
        row = original_probe(node, *args, **kwargs)
        return {**row, "worker_store_id": ("c" if node["host"] == "jp" else "d") * 32}
    monkeypatch.setattr(backend, "_rpc", probe)
    item["prepared_recovery"] = {"worker_store_id": store}
    plain = {key: value for key, value in item.items() if key != "prepared_recovery"}
    assert backend._identity("2026-10-05", item, "talk")[0] != backend._identity("2026-10-05", plain, "talk")[0]
    admission = capacity(tmp_path, 80 * 1024**3)  # local would fit without the recovery constraint
    lease, details = backend.reserve(admission, date="2026-10-05", item=item,
                                     admission_item=item, lane="talk", prepare_only=True)
    if store == "e" * 32:
        assert lease is None and details["reason"] == "no-compatible-overflow-worker"
    else:
        assert lease.node["host"] == ("jp" if store == "c" * 32 else "sjc")
        assert details["backend"] == "ssh"
        lease.release()
    assert admission.reserved_bytes == 0


def test_parallel_admission_claims_distinct_workers_before_either_submit(tmp_path, monkeypatch):
    backend,item=make_backend(tmp_path,monkeypatch)
    backend.config['mode']='remote'
    backend.nodes.append({**backend.nodes[0],'host':'sjc'})
    admission=capacity(tmp_path,20*1024**3)
    leases=[]
    for cid in ('first','second','third'):
        row={**item,'cid':cid}
        lease,_=backend.reserve(admission,date='2026-10-05',item=row,admission_item=row,lane='talk',prepare_only=True)
        leases.append(lease)
    assert [lease.node['host'] if lease else None for lease in leases]==['jp','sjc',None]
    # Another backend instance/process uses the same controller claims; the
    # remote probe still reports idle because neither task has submitted yet.
    cold=SSHBackend(tmp_path,backend.config,log=lambda _:None)
    monkeypatch.setattr(cold,'_rpc',backend._rpc)
    row={**item,'cid':'fourth'}
    assert cold.reserve(admission,date='2026-10-05',item=row,admission_item=row,lane='talk',prepare_only=True)[0] is None
    leases[0].release()
    fourth,_=cold.reserve(admission,date='2026-10-05',item=row,admission_item=row,lane='talk',prepare_only=True)
    assert fourth.node['host']=='jp'
    fourth.release();leases[1].release()
    assert admission.reserved_bytes==0


def test_original_absent_assignment_survives_controller_fix_and_dispatch_metadata(tmp_path,monkeypatch):
    from src.autoslice.overflow_release import seal
    backend,item=make_backend(tmp_path,monkeypatch);backend.config['mode']='remote'
    backend.nodes[0]['versioned']=True
    first,_=backend.reserve(capacity(tmp_path,20*1024**3),date='2026-10-05',item=item,
        admission_item=item,lane='talk',prepare_only=True)
    original=dict(first.assignment);first.release()
    old_release=backend.release;old_repo=tmp_path/'versions'/old_release/'repo'
    shutil.copytree(tmp_path/'repo',old_repo)
    code=tmp_path/'repo/src/autoslice/execution_backend.py';code.parent.mkdir(parents=True)
    code.write_text('controller transport fix')
    current_release=seal(tmp_path/'repo')
    cold=SSHBackend(tmp_path,{**backend.config,'release_sha256':current_release},log=lambda _:None)
    def rpc(node,action,**kw):
        assert kw['identity']['release_sha256']==old_release
        return {'status':'absent','job_id':original['job_id'],'attempt_id':original['attempt_id'],
            'worker_store_id':'c'*32,'startup_ok':True,'release_sha256':old_release,
            'runtime_root':str(tmp_path),'worker_root':'/data/control'}
    monkeypatch.setattr(cold,'_rpc',rpc)
    enriched={**item,'talk_dependency_snapshot':{'controller':'snapshot'},'runtime_scratch_dir':'/temporary/lease'}
    resumed,_=cold.reserve(capacity(tmp_path,20*1024**3),date='2026-10-05',item=enriched,
        admission_item=item,lane='talk',prepare_only=True)
    assert resumed.assignment==original
    resumed.release()
    commands=[]
    from types import SimpleNamespace
    import src.autoslice.execution_backend as module
    def command(argv,**kw):
        commands.append(argv[-1])
        return SimpleNamespace(returncode=0,stdout=json.dumps(rpc(None,'status',identity=original)).encode(),stderr=b'')
    monkeypatch.setattr(module.subprocess,'run',command)
    SSHBackend._rpc(cold,cold.nodes[0],'status',identity=original)
    SSHBackend._rpc(cold,cold.nodes[0],'probe',identity=original)
    assert all('/versions/'+old_release+'/repo/scripts/' in command for command in commands)
    assert '--job-id' in commands[0] and '--job-id' not in commands[1]
    with pytest.raises(CapacityProbeError,match='input changed'):
        cold.reserve(capacity(tmp_path,20*1024**3),date='2026-10-05',item={**enriched,'end_ms':61000},
            admission_item=item,lane='talk',prepare_only=True)
    producer=tmp_path/'repo/scripts/produce_slice_package.py';producer.parent.mkdir()
    producer.write_text('producer changed')
    newer=SSHBackend(tmp_path,{**backend.config,'release_sha256':seal(tmp_path/'repo')},log=lambda _:None)
    with pytest.raises(CapacityProbeError,match='producer release changed'):
        newer._verify_assigned_release(old_release)
    newer._verify_assigned_release(old_release,require_producer_compatibility=False)
    with pytest.raises(CapacityProbeError,match='producer release changed'):
        newer._verify_assigned_release(old_release)


@pytest.mark.parametrize('unsafe',[None,'process_alive','result_present','bundle_present'])
def test_terminal_worker_failure_is_consumed_only_with_bound_dead_empty_attempt(tmp_path,monkeypatch,unsafe):
    import src.autoslice.execution_backend as module
    backend,item=make_backend(tmp_path,monkeypatch);backend.config['mode']='remote'
    lease,_=backend.reserve(capacity(tmp_path,20*1024**3),date='2026-10-05',item=item,
        admission_item=item,lane='talk',prepare_only=True)
    assignment=lease.assignment
    state={k:assignment[k] for k in ('job_id','attempt_id','release_sha256','input_sha256','lane','date')}
    state.update(status='failed',lifecycle_status='failed',process_alive=False,result_present=False,bundle_present=False,
        worker_store_id='c'*32,error_code='execution_failed',startup_stage='executor_started')
    if unsafe:state[unsafe]=True
    monkeypatch.setattr(backend,'_rpc',lambda node,action,**kw:state if action=='status' else pytest.fail('must not submit'))
    path=tmp_path/'overflow-dispatch'/(assignment['job_id']+'.imported.json')
    if unsafe:
        with pytest.raises(CapacityProbeError,match='not safely finished'):
            backend.execute(lease,lambda *a:None,'2026-10-05',item,{'prepare_only':True})
        assert not path.exists() and module._has_unconsumed_assignment(tmp_path)
    else:
        result=backend.execute(lease,lambda *a:None,'2026-10-05',item,{'prepare_only':True})
        assert result['status']=='failed' and result['result_source']=='WORKER_TERMINAL_STATUS_WITHOUT_PRODUCER_RESULT'
        assert not module._has_unconsumed_assignment(tmp_path)
        assert backend.execute(lease,None,'2026-10-05',item,{})==result
        corrupted=json.loads(path.read_text());corrupted['remote_terminal']['job_id']='f'*64;path.write_text(json.dumps(corrupted))
        with pytest.raises(CapacityProbeError):module._has_unconsumed_assignment(tmp_path)
    lease.release()


def test_normal_private_batch_entry_runs_parallel_dispatch_while_queue_stays_paused(tmp_path,monkeypatch):
    import hashlib,sys,threading
    from types import SimpleNamespace
    from scripts import prepare_slice_batch as cli
    from src.autoslice.channel_profile import profile_environment
    backend,item=make_backend(tmp_path,monkeypatch)
    backend.config['mode']='remote';backend.nodes.append({**backend.nodes[0],'host':'sjc'})
    (tmp_path/'DISABLED').touch()
    source2=tmp_path/'source2.mp4';source2.write_bytes(b'another real source')
    rows=[{**item,'source_media_sha256':hashlib.sha256(Path(item['segment_path']).read_bytes()).hexdigest()},
          {**item,'cid':'second','segment_path':str(source2),'source_media_sha256':hashlib.sha256(source2.read_bytes()).hexdigest()}]
    batch=tmp_path/'batch.json';batch.write_text(json.dumps({'schema_version':'autoslice-prepare-talk-batch.v1',
        'date':'2026-10-05','profile_binding':backend.profile_binding,'items':rows}))
    original=batch.read_bytes();out=tmp_path/'result.json';barrier=threading.Barrier(2);nodes=[]
    def talk(date,row,**kw):
        assert kw=={'prepare_only':True}
        assert cli.os.environ['GEMINI_PAID_BACKUP_DAILY_CAP']=='0'
        assert cli.os.environ['AUTOSLICE_ENABLE_UPLOAD']=='0'
        barrier.wait(timeout=5)
        return {'candidate_id':row['cid'],'status':'failed','rc':1,'failure_kind':'offline_fixture'}
    def execute(lease,fn,date,row,kw):
        nodes.append(lease.node['host'])
        return fn(date,row,**kw)
    monkeypatch.setattr(backend,'execute',execute)
    runner=SimpleNamespace(BASE=tmp_path,CHANNEL_PROFILE=backend.profile,EXECUTION_BACKEND=backend,
        MAX_PARALLEL_PRODUCE=2,MIN_FREE_BYTES=0,OPERATIONAL_RESERVE_BYTES=0,
        produce_talk=talk,produce_song=lambda *a:None,log=lambda _:None,
        talk_pipeline_fingerprint=lambda _: 'talk',pipeline_fingerprint=lambda:'all',song_pipeline_fingerprint=lambda:'song',
        SONG_WINDOW_PRE_MS=0,SONG_WINDOW_POST_MS=0,live_hold_recheck=lambda:False,
        producer_stop_requested=lambda:True,profile_environment=profile_environment,
        load_env_file=lambda *a:{},load_gemini_credentials=lambda:{},_cpa_env_path=lambda:tmp_path/'cpa.env')
    monkeypatch.setitem(sys.modules,'scripts.session_autoslice',runner)
    import scripts
    monkeypatch.setattr(scripts,'session_autoslice',runner,raising=False)
    monkeypatch.setenv('GEMINI_PAID_BACKUP_DEV_EXCEPTION','1')
    assert cli.main(['--batch-json',str(batch),'--out-json',str(out)])==1
    result=json.loads(out.read_text())
    assert set(nodes)=={'jp','sjc'} and len(result['results'])==2
    assert all(row['status']=='failed' for row in result['results'])
    assert result['batch_sha256']==hashlib.sha256(original).hexdigest()
    assert batch.read_bytes()==original and (tmp_path/'DISABLED').exists() and not (tmp_path/'state').exists()
    assert 'GEMINI_PAID_BACKUP_DEV_EXCEPTION' not in cli.os.environ
    source2.write_bytes(b'input drift')
    with pytest.raises(SystemExit):cli.main(['--batch-json',str(batch),'--out-json',str(out)])
    assert len(nodes)==2


def test_unready_probe_is_skipped_before_unassigned_worker_assignment(tmp_path, monkeypatch):
    backend, item = make_backend(tmp_path, monkeypatch)
    backend.config["mode"] = "remote"
    backend.nodes = [
        {"host": "bad", "runtime_root": str(tmp_path), "worker_root": "/data/bad"},
        {"host": "good", "runtime_root": str(tmp_path), "worker_root": "/data/good"},
    ]
    seen = []

    def probe(node, action, **kwargs):
        assert action == "probe"
        seen.append(node["host"])
        return {
            "release_sha256": backend.release,
            "slot_busy": False,
            "available_bytes": 90 * 1024**3,
            "startup_ok": node["host"] == "good",
            "startup_error_code": None if node["host"] == "good" else "runtime_prerequisite_failed",
            "worker_root": node["worker_root"], "runtime_root": str(tmp_path), "worker_store_id": "c" * 32,
        }

    monkeypatch.setattr(backend, "_rpc", probe)
    lease, details = backend.reserve(
        capacity(tmp_path, 100 * 1024**3), date="2026-10-05", item=item,
        admission_item=item, lane="talk", prepare_only=True,
    )

    assert seen == ["bad", "good"]
    assert lease is not None and lease.node["host"] == "good"
    assert details["backend"] == "ssh"
    lease.release()


def test_unready_existing_assignment_is_preserved(tmp_path, monkeypatch):
    backend, item = make_backend(tmp_path, monkeypatch)
    backend.config["mode"] = "remote"
    first, _ = backend.reserve(
        capacity(tmp_path, 100 * 1024**3), date="2026-10-05", item=item,
        admission_item=item, lane="talk", prepare_only=True,
    )
    assert first is not None
    first.release()
    assignment_path = tmp_path / "overflow-dispatch" / f"{first.assignment['job_id']}.json"
    original = assignment_path.read_bytes()

    def unavailable(node, action, **kwargs):
        assert node["host"] == "jp"
        if action == "status":
            return {"status": "absent", "worker_store_id": "c" * 32, "job_id": kwargs["identity"]["job_id"],
                    "attempt_id": kwargs["identity"]["attempt_id"], "process_alive": False,
                    "result_present": False}
        assert action == "probe"
        return {
            "release_sha256": backend.release,
            "slot_busy": False,
            "available_bytes": 90 * 1024**3,
            "startup_ok": False,
            "startup_error_code": "runtime_prerequisite_failed",
        }

    monkeypatch.setattr(backend, "_rpc", unavailable)
    with pytest.raises(CapacityProbeError, match="startup preflight"):
        backend.reserve(
            capacity(tmp_path, 100 * 1024**3), date="2026-10-05", item=item,
            admission_item=item, lane="talk", prepare_only=True,
        )

    assert assignment_path.read_bytes() == original
    assert json.loads(assignment_path.read_text())["job_id"] == first.assignment["job_id"]


def test_imported_completion_recovers_before_startup_probe(tmp_path, monkeypatch):
    backend, item = make_backend(tmp_path, monkeypatch)
    backend.config["mode"] = "remote"
    first, _ = backend.reserve(
        capacity(tmp_path, 100 * 1024**3), date="2026-10-05", item=item,
        admission_item=item, lane="talk", prepare_only=True,
    )
    first.release()
    assignment_path = tmp_path / "overflow-dispatch" / f"{first.assignment['job_id']}.json"
    imported_path = assignment_path.with_suffix(".imported.json")
    imported_path.write_text(json.dumps({
        "candidate_id": item["cid"], "status": "delivery_prepared_no_target",
        "profile_binding": backend.profile_binding,
    }))

    def forbidden_rpc(*args, **kwargs):
        raise AssertionError("a controller-local imported result needs no worker startup probe")

    monkeypatch.setattr(backend, "_rpc", forbidden_rpc)
    recovered, details = backend.reserve(
        capacity(tmp_path, 0), date="2026-10-05", item=item,
        admission_item=item, lane="talk", prepare_only=True,
    )
    assert details["recovery"] is True
    assert recovered.assignment["job_id"] == first.assignment["job_id"]
    recovered.release()


def test_completed_remote_assignment_recovers_without_startup_probe(tmp_path, monkeypatch):
    backend, item = make_backend(tmp_path, monkeypatch)
    backend.config["mode"] = "remote"
    first, _ = backend.reserve(
        capacity(tmp_path, 100 * 1024**3), date="2026-10-05", item=item,
        admission_item=item, lane="talk", prepare_only=True,
    )
    first.release()
    seen = []

    def completed_status(node, action, **kwargs):
        seen.append(action)
        assert action == "status"
        return {
            "status": "completed", "worker_store_id": "c" * 32, "process_alive": False,
            "result_present": True, "result_sha256": "a" * 64,
        }

    monkeypatch.setattr(backend, "_rpc", completed_status)
    recovered, details = backend.reserve(
        capacity(tmp_path, 0), date="2026-10-05", item=item,
        admission_item=item, lane="talk", prepare_only=True,
    )
    assert details["recovery"] is True
    assert recovered.assignment["job_id"] == first.assignment["job_id"]
    assert seen == ["status"]
    recovered.release()


def test_completed_prior_release_result_import_uses_its_actual_assignment_release(tmp_path, monkeypatch):
    from src.autoslice.overflow_release import seal
    from src.autoslice import overflow_bundle
    backend, item = make_backend(tmp_path, monkeypatch)
    backend.config['mode'] = 'remote'
    backend.nodes[0]['versioned'] = True
    first, _ = backend.reserve(capacity(tmp_path, 100*1024**3), date='2026-10-05',
        item=item, admission_item=item, lane='talk', prepare_only=True)
    original = dict(first.assignment)
    first.release()
    shutil.copytree(tmp_path/'repo', tmp_path/'versions'/backend.release/'repo')
    code = tmp_path/'repo/src/autoslice/overflow_bundle.py'
    code.parent.mkdir(parents=True)
    code.write_text('current result reader repair')
    current = seal(tmp_path/'repo')
    cold = SSHBackend(tmp_path, {**backend.config, 'release_sha256': current}, log=lambda _: None)
    cold._verify_assigned_release(original['release_sha256'], require_producer_compatibility=False)
    assert current != original['release_sha256']
    calls = []
    def rpc(node, action, **kwargs):
        calls.append(action)
        assert action in {'status', 'fetch-description'}
        assert kwargs['identity']['release_sha256'] == original['release_sha256']
        return {'status': 'completed', 'worker_store_id': 'c'*32,
            'bundle_path': '/worker/completed/bundle', 'bundle_bytes': 100}
    monkeypatch.setattr(cold, '_rpc', rpc)
    monkeypatch.setattr(cold, '_copy', lambda *a, **k: None)
    result = {'status': 'failed', 'rc': 1, 'candidate_id': 'clip', 'profile_binding': cold.profile_binding}
    def imported(**kwargs):
        assert kwargs['expected_release_sha256'] == original['release_sha256']
        assert kwargs['expected_attempt_id'] == original['attempt_id']
        assert kwargs['expected_input_sha256'] == original['input_sha256']
        return result
    monkeypatch.setattr(overflow_bundle, 'import_bundle', imported)
    lease, details = cold.reserve(capacity(tmp_path, 0), date='2026-10-05',
        item=item, admission_item=item, lane='talk', prepare_only=True)
    assert details['recovery'] is True
    def no_producer(*a, **k):
        pytest.fail('completed-result recovery must never launch a producer')
    assert cold.execute(lease, no_producer, '2026-10-05', item, {'prepare_only': True})['rc'] == 1
    lease.release()
    assert calls == ['status', 'status', 'fetch-description']
    saved = tmp_path/'overflow-dispatch'/(original['job_id']+'.imported.json')
    assert json.loads(saved.read_text())['rc'] == 1


@pytest.mark.parametrize("startup_stage", ["pre_executor", "producer_import"])
def test_startup_typed_failure_retries_same_attempt_after_gate_repair(tmp_path, monkeypatch, startup_stage):
    from src.autoslice.producer_request import ProducerRuntimePrerequisiteError

    backend, item = make_backend(tmp_path, monkeypatch)
    backend.config["mode"] = "remote"
    first, _ = backend.reserve(
        capacity(tmp_path, 100 * 1024**3), date="2026-10-05", item=item,
        admission_item=item, lane="talk", prepare_only=True,
    )
    first.release()
    retry = {**item, "talk_transient_retry_count": 1}
    old_job_id = first.assignment["job_id"]
    calls = []

    def repaired_rpc(node, action, **kwargs):
        if action == "status":
            return {
                "status": "failed", "worker_store_id": "c" * 32, "error_code": "runtime_prerequisite_failed",
                "startup_stage": startup_stage, "process_alive": False,
                "result_present": False,
            }
        if action == "submit":
            calls.append(kwargs["request"])
            return {
                "status": "failed", "worker_store_id": "c" * 32, "error_code": "runtime_prerequisite_failed",
                "startup_stage": "executor_started", "process_alive": False,
                "result_present": False,
            }
        raise AssertionError(action)

    monkeypatch.setattr(backend, "_rpc", repaired_rpc)
    monkeypatch.setattr(backend, "_stage_sources", lambda *args, **kwargs: [])
    lease, details = backend.reserve(
        capacity(tmp_path, 100 * 1024**3), date="2026-10-05", item=retry,
        admission_item=item, lane="talk", prepare_only=True,
    )
    assert details["recovery"] is True
    assert lease.assignment["job_id"] == old_job_id
    producer_calls = []
    with pytest.raises(ProducerRuntimePrerequisiteError, match=r"^RUNTIME_PREREQUISITE:"):
        backend.execute(
            lease, lambda *args, **kwargs: producer_calls.append((args, kwargs)),
            "2026-10-05", retry, {"prepare_only": True},
        )
    lease.release()
    assert len(calls) == 1
    assert calls[0]["job_id"] == old_job_id
    assert calls[0]["attempt_id"] == first.assignment["attempt_id"]
    assert producer_calls == []


def test_changed_candidate_with_imported_content_failure_can_retry(tmp_path, monkeypatch):
    backend, item = make_backend(tmp_path, monkeypatch)
    backend.config["mode"] = "remote"
    admission = capacity(tmp_path, 100 * 1024**3)
    first, _ = backend.reserve(admission, date="2026-10-05", item=item,
        admission_item=item, lane="talk", prepare_only=True)
    first.release()
    assignment_path = tmp_path / "overflow-dispatch" / f"{first.assignment['job_id']}.json"
    assignment_path.with_suffix(".imported.json").write_text(json.dumps({
        "status": "candidate_rejected", "profile_binding": backend.profile_binding,
    }))
    calls = []

    def rpc(node, action, **kwargs):
        calls.append(action)
        if action == "status":
            return {"status": "completed", "worker_store_id": "c" * 32}
        assert action == "probe"
        return {"release_sha256": backend.release, "startup_ok": True,
                "slot_busy": False, "available_bytes": 100 * 1024**3,
                "worker_root": node["worker_root"], "runtime_root": str(tmp_path), "worker_store_id": "c" * 32}

    monkeypatch.setattr(backend, "_rpc", rpc)
    retry = {**item, "talk_transient_retry_count": 1}
    next_lease, details = backend.reserve(admission, date="2026-10-05", item=retry,
        admission_item=item, lane="talk", prepare_only=True)
    assert next_lease.assignment["job_id"] != first.assignment["job_id"]
    assert details.get("recovery") is not True
    assert calls == ["status", "probe"]
    next_lease.release()


def test_typed_remote_startup_failure_reaches_controller_with_assignment(tmp_path, monkeypatch):
    from src.autoslice.producer_request import ProducerRuntimePrerequisiteError

    backend, item = make_backend(tmp_path, monkeypatch)
    backend.config["mode"] = "remote"
    lease, _ = backend.reserve(
        capacity(tmp_path, 100 * 1024**3), date="2026-10-05", item=item,
        admission_item=item, lane="talk", prepare_only=True,
    )
    assert lease is not None
    lease.release()
    assignment_path = tmp_path / "overflow-dispatch" / f"{lease.assignment['job_id']}.json"

    monkeypatch.setattr(
        backend,
        "_rpc",
        lambda node, action, **kwargs: {
            "status": "failed", "worker_store_id": "c" * 32,
            "error_code": "runtime_prerequisite_failed",
        },
    )
    producer_calls = []

    with pytest.raises(ProducerRuntimePrerequisiteError, match=r"^RUNTIME_PREREQUISITE:"):
        backend.execute(
            lease,
            lambda *args, **kwargs: producer_calls.append((args, kwargs)),
            "2026-10-05",
            item,
            {"prepare_only": True},
        )

    assert producer_calls == []
    assert assignment_path.is_file()


def test_rpc_propagates_typed_cli_startup_error(tmp_path, monkeypatch):
    from src.autoslice import execution_backend as backend_module
    from src.autoslice.producer_request import ProducerRuntimePrerequisiteError

    backend, _item = make_backend(tmp_path, monkeypatch)

    class Completed:
        returncode = 2
        stdout = b'{"error":"runtime_prerequisite_failed","ok":false}\n'
        stderr = b""

    monkeypatch.setattr(backend_module.subprocess, "run", lambda *args, **kwargs: Completed())
    with pytest.raises(ProducerRuntimePrerequisiteError, match=r"^RUNTIME_PREREQUISITE:"):
        SSHBackend._rpc(backend, backend.nodes[0], "submit", request={})


def test_disconnected_assignment_cannot_fall_back_local_or_evade_with_retry(tmp_path, monkeypatch):
    backend, item = make_backend(tmp_path, monkeypatch)
    lease, _ = backend.reserve(capacity(tmp_path, 2 * 1024**3 + 512 * 1024**2), date="2026-10-05",
        item=item, admission_item=item, lane="talk", prepare_only=True)
    lease.release()
    def disconnect(*args, **kwargs):
        raise ConnectionError("lost SSH")
    monkeypatch.setattr(backend, "_rpc", disconnect)
    with pytest.raises(CapacityProbeError, match="unreachable"):
        backend.reserve(capacity(tmp_path, 100 * 1024**3), date="2026-10-05", item=item,
            admission_item=item, lane="talk", prepare_only=True)
    with pytest.raises(CapacityProbeError, match="unreachable"):
        backend.reserve(capacity(tmp_path, 100 * 1024**3), date="2026-10-05",
            item={**item, "talk_transient_retry_count": 1}, admission_item=item, lane="talk", prepare_only=True)


def test_dispatch_backend_keeps_lane_identity_and_input_order(tmp_path, monkeypatch):
    backend, item = make_backend(tmp_path, monkeypatch)
    seen = []
    def talk(date, row, **kwargs):
        seen.append((row["cid"], kwargs))
        return {"candidate_id": row["cid"], "status": "ok", "rc": 0}
    monkeypatch.setattr(backend, "execute", lambda lease, fn, date, row, kw: fn(date, row, **kw))
    rows = produce_batch_windowed("2026-10-05", [dict(item, reuse_cover=True), dict(item, cid="second")], talk,
        produce_talk_fn=talk, produce_song_fn=lambda *a: {}, base=tmp_path, max_parallel=2,
        free_bytes_fn=lambda _: 2 * 1024**3 + 512 * 1024**2, log=lambda _: None,
        talk_pipeline_fingerprint=lambda _: "talk", pipeline_fingerprint=lambda: "all",
        song_pipeline_fingerprint=lambda: "song", song_window_pre_ms=0, song_window_post_ms=0,
        prepare_only=True, execution_backend=backend)
    assert [row["candidate_id"] for row in rows] == ["clip", "second"]
    assert seen == [("clip", {"prepare_only": True, "reuse_cover": True}), ("second", {"prepare_only": True})]


def test_changed_retry_recovers_running_assignment_without_relaunch(tmp_path, monkeypatch):
    backend, item = make_backend(tmp_path, monkeypatch)
    controller = capacity(tmp_path, 2 * 1024**3 + 512 * 1024**2)
    first, _ = backend.reserve(controller, date="2026-10-05", item=item, admission_item=item,
        lane="talk", prepare_only=True)
    first.release()
    def rpc(node, action, **kwargs):
        if action == "status":
            return {"status": "running", "worker_store_id": "c" * 32}
        return {"release_sha256": backend.release, "slot_busy": False, "available_bytes": 90 * 1024**3}
    monkeypatch.setattr(backend, "_rpc", rpc)
    retry = {**item, "talk_transient_retry_count": 1}
    second, details = backend.reserve(controller, date="2026-10-05", item=retry,
        admission_item=item, lane="talk", prepare_only=True)
    assert details["recovery"] is True
    assert second.assignment["job_id"] == first.assignment["job_id"]
    second.release()


def test_typed_startup_failure_assignment_is_not_replaced_by_retry(tmp_path, monkeypatch):
    backend, item = make_backend(tmp_path, monkeypatch)
    backend.config["mode"] = "remote"
    first, _ = backend.reserve(
        capacity(tmp_path, 100 * 1024**3), date="2026-10-05", item=item,
        admission_item=item, lane="talk", prepare_only=True,
    )
    assert first is not None
    first.release()

    def rpc(node, action, **kwargs):
        if action == "status":
            return {
                "status": "failed", "worker_store_id": "c" * 32, "error_code": "runtime_prerequisite_failed",
                "startup_stage": "executor_started", "process_alive": False,
                "result_present": False,
            }
        return {
            "release_sha256": backend.release,
            "slot_busy": False,
            "available_bytes": 90 * 1024**3,
            "startup_ok": True,
        }

    monkeypatch.setattr(backend, "_rpc", rpc)
    retry = {**item, "talk_transient_retry_count": 1}
    with pytest.raises(CapacityProbeError, match="startup preflight failed"):
        backend.reserve(
            capacity(tmp_path, 100 * 1024**3), date="2026-10-05", item=retry,
            admission_item=item, lane="talk", prepare_only=True,
        )

    assignment_files = [
        path for path in (tmp_path / "overflow-dispatch").glob("*.json")
        if path.parent.name == "overflow-dispatch"
    ]
    assert assignment_files == [tmp_path / "overflow-dispatch" / f"{first.assignment['job_id']}.json"]


def test_code_drift_cannot_hide_an_unconsumed_remote_assignment(tmp_path, monkeypatch):
    import json
    backend, item = make_backend(tmp_path, monkeypatch)
    config = tmp_path / "execution-backends.json"
    config.write_text(json.dumps(backend.config))
    lease, _ = backend.reserve(capacity(tmp_path, 2 * 1024**3 + 512 * 1024**2),
        date="2026-10-05", item=item, admission_item=item, lane="talk", prepare_only=True)
    lease.release()
    (tmp_path / "repo/OVERFLOW_RELEASE_SHA256").unlink()
    with pytest.raises(CapacityProbeError, match="unconsumed assignment"):
        backend_from_environment(tmp_path, log=lambda _: None)
    imported = tmp_path / "overflow-dispatch" / f"{lease.assignment['job_id']}.imported.json"
    imported.write_text(json.dumps({
        "status": "candidate_rejected", "profile_binding": backend.profile_binding,
    }))
    assert backend_from_environment(tmp_path, log=lambda _: None) is None


@pytest.mark.parametrize("remove_config", [False, True])
def test_rollback_cannot_restart_an_unconsumed_remote_job_locally(tmp_path, monkeypatch, remove_config):
    import json
    backend, item = make_backend(tmp_path, monkeypatch)
    lease, _ = backend.reserve(capacity(tmp_path, 2 * 1024**3 + 512 * 1024**2),
        date="2026-10-05", item=item, admission_item=item, lane="talk", prepare_only=True)
    lease.release()
    if not remove_config:
        (tmp_path / "execution-backends.json").write_text(json.dumps({"mode": "local"}))
    with pytest.raises(CapacityProbeError, match="unconsumed assignment"):
        backend_from_environment(tmp_path, log=lambda _: None)


def test_backend_identity_includes_selected_profile_binding(tmp_path, monkeypatch):
    default_backend, _item = make_backend(tmp_path / "default", monkeypatch)
    alternate_backend, _alternate_item = make_backend(
        tmp_path / "alternate", monkeypatch, profile_id="audit-other"
    )
    same_item = {"cid": "clip", "segment_path": "/same/source.mp4", "start_ms": 0, "end_ms": 60000}
    default_key, default_body = default_backend._identity("2026-10-05", same_item, "talk")
    alternate_key, alternate_body = alternate_backend._identity(
        "2026-10-05", same_item, "talk"
    )
    assert default_key != alternate_key
    assert default_body["profile_binding"]["profile_id"] == "lidousha"
    assert alternate_body["profile_binding"]["profile_id"] == "audit-other"


def test_legacy_assignment_binding_is_rejected_before_rpc_and_preserved(tmp_path, monkeypatch):
    backend, item = make_backend(tmp_path, monkeypatch)
    backend.config["mode"] = "remote"
    lease, _ = backend.reserve(
        capacity(tmp_path, 100 * 1024**3), date="2026-10-05", item=item,
        admission_item=item, lane="talk", prepare_only=True,
    )
    lease.release()
    assignment_path = tmp_path / "overflow-dispatch" / f"{lease.assignment['job_id']}.json"
    pointer_path = tmp_path / "overflow-dispatch/candidates/2026-10-05-talk-clip.json"
    assignment_before = assignment_path.read_bytes()
    pointer_before = pointer_path.read_bytes()
    assignment = json.loads(assignment_before)
    assignment.pop("profile_binding")
    assignment_path.write_text(json.dumps(assignment, sort_keys=True) + "\n")
    legacy_assignment = assignment_path.read_bytes()
    monkeypatch.setattr(backend, "_rpc", lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("legacy assignment must fail before RPC")
    ))
    with pytest.raises(CapacityProbeError, match="profile binding"):
        backend.reserve(
            capacity(tmp_path, 100 * 1024**3), date="2026-10-05", item=item,
            admission_item=item, lane="talk", prepare_only=True,
        )
    assert assignment_path.read_bytes() == legacy_assignment
    assert pointer_path.read_bytes() == pointer_before


def test_backend_config_cannot_replace_bound_parent_selection(tmp_path, monkeypatch):
    from src.autoslice.channel_profile import PROFILE_BINDING_ENV, ChannelProfileError
    backend, _item = make_backend(tmp_path, monkeypatch)
    monkeypatch.setenv(PROFILE_BINDING_ENV, json.dumps(backend.profile_binding))
    changed = {**backend.profile_binding, "manifest_sha256": "f" * 64}
    with pytest.raises(ChannelProfileError, match="differs"):
        SSHBackend(tmp_path, {**backend.config, "profile_binding": changed}, log=lambda _: None)


def test_no_overflow_configuration_needs_no_runtime_profile(tmp_path, monkeypatch):
    monkeypatch.delenv("AUTOSLICE_EXECUTION_CONFIG", raising=False)
    assert backend_from_environment(tmp_path, log=lambda _: None) is None


@pytest.mark.parametrize("surface", ["pointer", "assignment", "cache"])
@pytest.mark.parametrize("mutation", ["missing", "different"])
def test_durable_profile_conflict_rejects_before_rpc_and_preserves_bytes(tmp_path, monkeypatch, surface, mutation):
    backend, item = make_backend(tmp_path, monkeypatch)
    backend.config["mode"] = "remote"
    lease, _ = backend.reserve(capacity(tmp_path, 100 * 1024**3), date="2026-10-05", item=item,
        admission_item=item, lane="talk", prepare_only=True)
    lease.release()
    assignment = tmp_path / "overflow-dispatch" / (lease.assignment["job_id"] + ".json")
    pointer = tmp_path / "overflow-dispatch/candidates/2026-10-05-talk-clip.json"
    cached = assignment.with_suffix(".imported.json")
    cached.write_text(json.dumps({"status": "candidate_rejected", "candidate_id": "clip", "profile_binding": backend.profile_binding}))
    paths = {"pointer": pointer, "assignment": assignment, "cache": cached}
    changed = json.loads(paths[surface].read_text())
    if mutation == "missing":
        changed.pop("profile_binding")
    else:
        changed["profile_binding"] = {**changed["profile_binding"], "manifest_sha256": "f" * 64}
    paths[surface].write_text(json.dumps(changed))
    before = {name: path.read_bytes() for name, path in paths.items()}
    monkeypatch.setattr(backend, "_rpc", lambda *a, **k: (_ for _ in ()).throw(AssertionError("unexpected RPC")))
    with pytest.raises(CapacityProbeError, match="profile binding"):
        backend.reserve(capacity(tmp_path, 100 * 1024**3), date="2026-10-05", item=item,
            admission_item=item, lane="talk", prepare_only=True)
    assert {name: path.read_bytes() for name, path in paths.items()} == before


def ambiguous_assignment(base, monkeypatch):
    """Persist a real backend request while transport fails before admission."""
    backend, item = make_backend(base, monkeypatch)
    backend.config["mode"] = "remote"
    lease, _ = backend.reserve(capacity(base, 100 * 1024**3), date="2026-10-05",
        item=item, admission_item=item, lane="talk", prepare_only=True)
    submitted = []
    def rpc(node, action, **kwargs):
        if action == "probe":
            return {"release_sha256": backend.release, "startup_ok": True,
                    "worker_root": node["worker_root"], "runtime_root": str(base), "worker_store_id": "c" * 32}
        if action == "status":
            identity = kwargs["identity"]
            return {"status": "absent", "worker_store_id": "c" * 32, "job_id": identity["job_id"], "attempt_id": identity["attempt_id"]}
        assert action == "submit"
        submitted.append(kwargs["request"])
        raise ConnectionError("before acceptance")
    monkeypatch.setattr(backend, "_rpc", rpc)
    monkeypatch.setattr(backend, "_copy", lambda *a, **kw: None)
    with pytest.raises(RuntimeError, match="task absent"):
        backend.execute(lease, lambda *a, **kw: pytest.fail("local fallback"),
                        "2026-10-05", item, {"prepare_only": True})
    lease.release()
    path = base / "overflow-dispatch" / f"{lease.assignment['job_id']}.json"
    return backend, item, path, submitted[0]


def test_absent_submitting_changed_retry_uses_frozen_request_and_attempt(tmp_path, monkeypatch):
    backend, item, path, request = ambiguous_assignment(tmp_path, monkeypatch)
    pointer = tmp_path / "overflow-dispatch/candidates/2026-10-05-talk-clip.json"
    pointer_bytes = pointer.read_bytes()
    cold = SSHBackend(tmp_path, {**backend.config, "mode": "auto"}, log=lambda _: None)
    sent = []
    def rpc(node, action, **kw):
        if action == "probe":
            return {"release_sha256": cold.release, "startup_ok": True,
                    "worker_root": node["worker_root"], "runtime_root": str(tmp_path), "worker_store_id": "c" * 32}
        identity = kw.get("identity") or kw.get("request")
        if action == "fetch-description":
            raise ConnectionError("stop after accepted receipt")
        if action == "submit":
            sent.append(kw["request"])
        return {"status": "completed" if action == "submit" else "absent", "worker_store_id": "c" * 32,
                "job_id": identity["job_id"], "attempt_id": identity["attempt_id"]}
    monkeypatch.setattr(cold, "_rpc", rpc)
    monkeypatch.setattr(cold, "_copy", lambda *a, **kw: None)
    retry = dict(item, talk_transient_retry_count=1, selected_repair=True, retry_reason="transient_produce_failure")
    lease, details = cold.reserve(capacity(tmp_path, 100 * 1024**3), date="2026-10-05",
        item=retry, admission_item=retry, lane="talk", prepare_only=True)
    assert details["recovery"] is True
    with pytest.raises(ConnectionError, match="stop after"):
        cold.execute(lease, lambda *a, **kw: pytest.fail("local fallback"),
                     "2026-10-05", retry, {"prepare_only": True})
    lease.release()
    assert sent == [request]
    saved = json.loads(path.read_text())
    assert saved["request"] == request
    assert saved["phase"] == "accepted"
    assert saved["job_id"] == request["job_id"] and saved["attempt_id"] == request["attempt_id"]
    assert pointer.read_bytes() == pointer_bytes
    assert len(list(path.parent.glob("*.json"))) == 1


@pytest.mark.parametrize("change", ["worker_root", "source_bytes", "input", "release", "request_digest", "legacy_request", "legacy_authority", "accepted", "reply_identity"])
def test_absent_recovery_rejects_changed_authority_or_input(tmp_path, monkeypatch, change):
    backend, item, path, request = ambiguous_assignment(tmp_path, monkeypatch)
    config = json.loads(json.dumps(backend.config))
    saved = json.loads(path.read_text())
    retry = dict(item, talk_transient_retry_count=1)
    if change == "worker_root":
        config["nodes"][0]["worker_root"] = "/data/other-control"
    if change == "source_bytes":
        Path(item["segment_path"]).write_bytes(b"Source")
    if change == "input":
        retry["end_ms"] += 1
    if change == "release":
        from src.autoslice.overflow_release import seal
        (tmp_path / "repo/DEPLOYED_COMMIT").write_text("b" * 40)
        config["release_sha256"] = seal(tmp_path / "repo")
    if change == "request_digest":
        saved["request_sha256"] = "f" * 64
    if change == "legacy_request":
        saved.pop("request")
    if change == "legacy_authority":
        saved.pop("worker_authority")
    if change == "accepted":
        saved["phase"] = "accepted"
    path.write_text(json.dumps(saved))
    before = path.read_bytes()
    cold = SSHBackend(tmp_path, {**config, "mode": "auto"}, log=lambda _: None)
    actions = []
    def rpc(node, action, **kw):
        actions.append(action)
        assert action == "status"
        identity = kw["identity"]
        return {"status": "absent", "worker_store_id": "c" * 32, "job_id": identity["job_id"],
                "attempt_id": "f" * 32 if change == "reply_identity" else identity["attempt_id"]}
    monkeypatch.setattr(cold, "_rpc", rpc)
    with pytest.raises(CapacityProbeError, match="preserve assignment"):
        cold.reserve(capacity(tmp_path, 100 * 1024**3), date="2026-10-05",
            item=retry, admission_item=retry, lane="talk", prepare_only=True)
    assert "submit" not in actions
    assert path.read_bytes() == before


@pytest.mark.parametrize("status", ["unknown", "unreachable"])
def test_ambiguous_recovery_unknown_or_unreachable_never_resubmits(tmp_path, monkeypatch, status):
    backend, item, path, request = ambiguous_assignment(tmp_path, monkeypatch)
    cold = SSHBackend(tmp_path, {**backend.config, "mode": "auto"}, log=lambda _: None)
    actions = []
    def rpc(node, action, **kw):
        actions.append(action)
        assert action == "status"
        if status == "unreachable":
            raise ConnectionError("still disconnected")
        return {"status": status, "worker_store_id": "c" * 32, "job_id": request["job_id"], "attempt_id": request["attempt_id"]}
    monkeypatch.setattr(cold, "_rpc", rpc)
    retry = dict(item, talk_transient_retry_count=1)
    if status == "unreachable":
        with pytest.raises(CapacityProbeError, match="unreachable"):
            cold.reserve(capacity(tmp_path, 100 * 1024**3), date="2026-10-05", item=retry,
                         admission_item=retry, lane="talk", prepare_only=True)
    else:
        lease, _ = cold.reserve(capacity(tmp_path, 100 * 1024**3), date="2026-10-05", item=retry,
                                admission_item=retry, lane="talk", prepare_only=True)
        with pytest.raises(RuntimeError, match="task unknown"):
            cold.execute(lease, lambda *a, **kw: pytest.fail("local fallback"),
                         "2026-10-05", retry, {"prepare_only": True})
        lease.release()
    assert "submit" not in actions
    assert json.loads(path.read_text())["request"] == request


def test_absent_recovery_changed_producer_kwargs_never_submits(tmp_path, monkeypatch):
    backend, item, path, request = ambiguous_assignment(tmp_path, monkeypatch)
    cold = SSHBackend(tmp_path, backend.config, log=lambda _: None)
    def rpc(node, action, **kw):
        if action == "probe":
            return {"release_sha256": cold.release, "startup_ok": True,
                    "worker_root": node["worker_root"], "runtime_root": str(tmp_path), "worker_store_id": "c" * 32}
        assert action == "status"
        return {"status": "absent", "worker_store_id": "c" * 32, "job_id": request["job_id"], "attempt_id": request["attempt_id"]}
    monkeypatch.setattr(cold, "_rpc", rpc)
    retry = dict(item, talk_transient_retry_count=1)
    lease, _ = cold.reserve(capacity(tmp_path, 100 * 1024**3), date="2026-10-05", item=retry,
                            admission_item=retry, lane="talk", prepare_only=True)
    before = path.read_bytes()
    with pytest.raises(CapacityProbeError, match="saved request"):
        cold.execute(lease, lambda *a, **kw: pytest.fail("local fallback"),
                     "2026-10-05", retry, {"prepare_only": True, "reuse_cover": True})
    lease.release()
    assert path.read_bytes() == before


@pytest.mark.parametrize("store_id", [None, "d" * 32, "invalid"])
def test_pending_assignment_rejects_unknown_or_replaced_store(tmp_path, monkeypatch, store_id):
    backend, item, path, request = ambiguous_assignment(tmp_path, monkeypatch)
    before = path.read_bytes()
    pointer = tmp_path / "overflow-dispatch/candidates/2026-10-05-talk-clip.json"
    pointer_before = pointer.read_bytes()
    cold = SSHBackend(tmp_path, {**backend.config, "mode": "auto"}, log=lambda _: None)
    actions = []
    def rpc(node, action, **kw):
        actions.append(action)
        assert action == "status"
        return {"status": "absent", "worker_store_id": store_id,
                "job_id": request["job_id"], "attempt_id": request["attempt_id"]}
    monkeypatch.setattr(cold, "_rpc", rpc)
    with pytest.raises(CapacityProbeError, match="store binding missing or changed"):
        cold.reserve(capacity(tmp_path, 100 * 1024**3), date="2026-10-05",
            item=dict(item, talk_transient_retry_count=1), admission_item=item,
            lane="talk", prepare_only=True)
    assert actions == ["status"]
    assert path.read_bytes() == before and pointer.read_bytes() == pointer_before


def test_legacy_pending_assignment_cannot_acquire_store_binding(tmp_path, monkeypatch):
    backend, item, path, request = ambiguous_assignment(tmp_path, monkeypatch)
    saved = json.loads(path.read_text())
    saved["worker_authority"].pop("worker_store_id")
    saved["request"].pop("worker_store_id")
    from src.autoslice.overflow_worker import request_sha256
    saved["request_sha256"] = request_sha256(saved["request"])
    path.write_text(json.dumps(saved))
    before = path.read_bytes()
    monkeypatch.setattr(backend, "_rpc", lambda node, action, **kw: {
        "status": "absent", "worker_store_id": "c" * 32,
        "job_id": request["job_id"], "attempt_id": request["attempt_id"]})
    with pytest.raises(CapacityProbeError, match="store binding missing or changed"):
        backend.reserve(capacity(tmp_path, 100 * 1024**3), date="2026-10-05",
            item=dict(item, talk_transient_retry_count=1), admission_item=item,
            lane="talk", prepare_only=True)
    assert path.read_bytes() == before


def test_store_change_between_reserve_and_execute_never_submits(tmp_path, monkeypatch):
    backend, item, path, request = ambiguous_assignment(tmp_path, monkeypatch)
    before = path.read_bytes()
    store_id = "c" * 32
    def rpc(node, action, **kw):
        assert action != "submit"
        if action == "probe":
            return {"startup_ok": True, "release_sha256": backend.release,
                    "runtime_root": str(tmp_path), "worker_root": node["worker_root"],
                    "worker_store_id": store_id}
        return {"status": "absent", "worker_store_id": store_id,
                "job_id": request["job_id"], "attempt_id": request["attempt_id"]}
    monkeypatch.setattr(backend, "_rpc", rpc)
    lease, _ = backend.reserve(capacity(tmp_path, 100 * 1024**3), date="2026-10-05",
        item=item, admission_item=item, lane="talk", prepare_only=True)
    store_id = "d" * 32
    with pytest.raises(CapacityProbeError, match="store binding missing or changed"):
        backend.execute(lease, lambda *a, **kw: pytest.fail("local fallback"),
                        "2026-10-05", item, {"prepare_only": True})
    lease.release()
    assert path.read_bytes() == before
