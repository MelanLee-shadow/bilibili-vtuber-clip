"""Completed raw text reuse through ordinary callees; all providers are offline."""

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from src.autoslice import normal_text_generation_checkpoint as checkpoint
from src.autoslice import publish_staging as staging
from scripts import suggest_upload_tags as tags

TITLE = "【李豆沙】读春雪时分享自己的读书感受"
IDENTITY = {
    "transport": "offline-fixture",
    "provider": "no-provider",
    "models": ["offline-model"],
    "effort": "high",
}


def fixture_call(response, calls):
    def call(prompt):
        calls.append(prompt)
        return response

    call.cpa_cache_identity = deepcopy(IDENTITY)
    return call


def srt_at(root):
    path = root / "candidate.srt"
    path.write_text("1\n00:00:00,000 --> 00:00:02,000\n分享春雪的摘抄和读书感受。\n")
    return path


def run_raw(root, srt, call, **kw):
    return checkpoint.completed_generation(
        artifact_root=root,
        candidate_id="candidate",
        lane=kw.pop("lane", "title"),
        attempt=kw.pop("attempt", 1),
        prompt=kw.pop("prompt", "full actual prompt"),
        call=call,
        dependencies=lambda: checkpoint.subtitle_dependency(srt),
        **kw,
    )


@pytest.mark.parametrize("response", ['{"title":"完整原件"}', "not JSON", ""])
def test_complete_raw_response_reused_before_parser(tmp_path, response):
    srt = srt_at(tmp_path)
    calls = []
    call = fixture_call(response, calls)
    assert run_raw(tmp_path, srt, call) == response
    assert run_raw(tmp_path, srt, call) == response
    assert len(calls) == 1
    saved = json.loads(next(tmp_path.rglob("result.json")).read_text())
    assert saved["response_text"] == response
    assert saved["response_sha256"] == "sha256:" + hashlib.sha256(response.encode()).hexdigest()


@pytest.mark.parametrize(
    "changed", ["prompt", "attempt", "lane", "srt", "model", "effort", "provider"]
)
def test_real_generation_dependency_change_does_not_reuse(tmp_path, changed):
    srt = srt_at(tmp_path)
    calls = []
    call = fixture_call("full response", calls)
    run_raw(tmp_path, srt, call)
    kw = {}
    if changed in ("prompt", "attempt", "lane"):
        kw[changed] = {"prompt": "new actual prompt", "attempt": 2, "lane": "tags"}[changed]
    elif changed == "srt":
        srt.write_text(srt.read_text().replace("春雪", "新作品"))
    elif changed == "model":
        call.cpa_cache_identity["models"] = ["new-model"]
    else:
        call.cpa_cache_identity[changed] = "new-" + changed
    run_raw(tmp_path, srt, call, **kw)
    assert len(calls) == 2
    assert len(list(tmp_path.rglob("result.json"))) == 2


@pytest.mark.parametrize("missing", ["identity", "models", "effort", "endpoint"])
def test_unknown_provider_identity_never_claims_reuse(tmp_path, missing):
    srt = srt_at(tmp_path)
    calls = []
    call = fixture_call("full response", calls)
    if missing == "identity":
        del call.cpa_cache_identity
    elif missing == "endpoint":
        call.cpa_cache_identity["transport"] = "cpa_command"
    else:
        del call.cpa_cache_identity[missing]
    run_raw(tmp_path, srt, call)
    run_raw(tmp_path, srt, call)
    assert len(calls) == 2
    assert not list(tmp_path.rglob("CURRENT.json"))


@pytest.mark.parametrize(
    "tamper", ["response", "request", "missing_request", "missing_result", "pointer"]
)
def test_saved_original_tamper_blocks_without_redispatch(tmp_path, tamper):
    srt = srt_at(tmp_path)
    calls = []
    call = fixture_call("complete original", calls)
    run_raw(tmp_path, srt, call)
    result = next(tmp_path.rglob("result.json"))
    request = result.parent / "request.json"
    pointer = next(tmp_path.rglob("CURRENT.json"))
    if tamper == "response":
        saved = json.loads(result.read_text())
        saved["response_text"] = "different"
        result.write_text(json.dumps(saved))
    elif tamper == "request":
        request.write_text("{}")
    elif tamper == "missing_request":
        request.unlink()
    elif tamper == "missing_result":
        result.unlink()
    else:
        pointer.write_text('{"receipt":"../../result.json","sha256":"bad"}')
    with pytest.raises(ValueError):
        run_raw(tmp_path, srt, call)
    assert len(calls) == 1


def test_interruption_between_complete_response_and_current_recovers(tmp_path, monkeypatch):
    srt = srt_at(tmp_path)
    calls = []
    call = fixture_call("complete original", calls)
    original = checkpoint._atomic_json

    def write(path, value):
        if path.name == "CURRENT.json":
            raise OSError("offline CURRENT publication failure")
        return original(path, value)

    monkeypatch.setattr(checkpoint, "_atomic_json", write)
    with pytest.raises(OSError):
        run_raw(tmp_path, srt, call)
    monkeypatch.setattr(checkpoint, "_atomic_json", original)
    assert run_raw(tmp_path, srt, call) == "complete original"
    assert len(calls) == 1
    assert next(tmp_path.rglob("CURRENT.json")).is_file()


def test_ambiguous_completed_receipts_without_pointer_block(tmp_path):
    srt = srt_at(tmp_path)
    calls = []
    call = fixture_call("response", calls)
    run_raw(tmp_path, srt, call)
    result = next(tmp_path.rglob("result.json"))
    next(tmp_path.rglob("CURRENT.json")).unlink()
    shutil.copytree(result.parent, result.parent.parent / ("f" * 32))
    with pytest.raises(ValueError, match="AMBIGUOUS"):
        run_raw(tmp_path, srt, call)
    assert len(calls) == 1


def test_input_disappears_after_full_response_preserved_but_not_reusable(tmp_path):
    srt = srt_at(tmp_path)
    calls = []

    def call(prompt):
        calls.append(prompt)
        srt.unlink()
        return "complete original"

    call.cpa_cache_identity = deepcopy(IDENTITY)
    with pytest.raises(ValueError, match="INPUT_DRIFT"):
        run_raw(tmp_path, srt, call)
    saved = json.loads(next(tmp_path.rglob("result.json")).read_text())
    assert saved["response_text"] == "complete original"
    assert saved["status"] == "INPUT_DRIFT_NOT_REUSABLE"


def test_ambiguous_transport_failure_not_cached_as_completion(tmp_path):
    srt = srt_at(tmp_path)
    calls = []

    def call(prompt):
        calls.append(prompt)
        raise RuntimeError("offline no complete response")

    call.cpa_cache_identity = deepcopy(IDENTITY)
    for _ in range(2):
        with pytest.raises(RuntimeError):
            run_raw(tmp_path, srt, call)
    assert len(calls) == 2
    assert not list(tmp_path.rglob("result.json"))
    assert len(list(tmp_path.rglob("failure.json"))) == 2


def test_title_replays_rejected_comparisons_then_original_bounded_retry(tmp_path):
    srt = srt_at(tmp_path)
    calls = []
    call = fixture_call(
        json.dumps(
            {
                "title": TITLE,
                "candidates": [],
                "editorial_choice": {"selected": "old", "reason": "fixture"},
            }
        ),
        calls,
    )

    def run():
        return staging._resolve_automatic_title(
            base_prompt="actual prompt",
            title_llm_call=call,
            selection_hook="读书感受",
            initial_title_source="job_title",
            generation_root=tmp_path,
            generation_subtitle_path=srt,
            input_binding={"candidate_id": "candidate"},
        )

    first = run()
    second = run()
    assert first == second
    assert first.title_authority_error.startswith("title_policy_violation:")
    assert len(calls) == staging._TITLE_MAX_ATTEMPTS
    assert all(x["status"] == "REJECTED_POLICY" for x in first.title_generation_attempts)


def test_tags_reparse_raw_and_do_not_key_on_saved_record_or_merge_options(tmp_path):
    srt = srt_at(tmp_path)
    calls = []
    call = fixture_call('{"tags":[{"tag":"读书分享","why":"fixture"}]}', calls)
    first = tags.generate_upload_tags(TITLE, srt, llm_call=call)
    assert first["content_review_status"] == "COMPLETED"
    srt.with_suffix(".record.json").write_text(json.dumps({"upload_tags": first}))
    second = tags.generate_upload_tags(TITLE, srt, llm_call=call, max_tags=5)
    assert len(calls) == 1
    assert second["content_review_status"] == "COMPLETED"
    assert len(second["final_tags"]) <= 5
    tags.generate_upload_tags(TITLE, srt, llm_call=call, existing_tags=["different"])
    assert len(calls) == 2


def test_tags_current_subtitle_change_during_call_remains_failed(tmp_path):
    srt = srt_at(tmp_path)

    def call(_):
        srt.write_text("changed")
        return '{"tags":[]}'

    call.cpa_cache_identity = deepcopy(IDENTITY)
    result = tags.generate_upload_tags(TITLE, srt, llm_call=call)
    assert result["status"] == "FAILED"
    assert "CURRENT_TAG_SUBTITLE_CHANGED_DURING_GENERATION" in result["error"]
    assert (
        json.loads(next(tmp_path.rglob("result.json")).read_text())["status"]
        == "INPUT_DRIFT_NOT_REUSABLE"
    )


def test_tags_default_adapter_binds_actual_frozen_endpoint_without_secret(tmp_path, monkeypatch):
    srt = srt_at(tmp_path)
    calls = []
    configs = []
    monkeypatch.delenv("AUTOSLICE_BASE", raising=False)
    monkeypatch.setenv("CPA_BASE_URL", "https://offline.invalid/v1")
    monkeypatch.setenv("CPA_API_KEY", "OFFLINE-NO-CREDENTIAL")

    def build(config):
        configs.append(config)
        call = fixture_call('{"tags":[]}', calls)
        call.cpa_cache_identity = {
            "transport": "cpa_command",
            "models": ["offline-model"],
            "effort": "medium",
        }
        return call

    monkeypatch.setattr(tags, "build_llm_call", build)
    tags.generate_upload_tags(TITLE, srt)
    tags.generate_upload_tags(TITLE, srt)
    assert len(calls) == 1
    saved = next(tmp_path.rglob("result.json")).read_text()
    assert "OFFLINE-NO-CREDENTIAL" not in saved
    assert "https://offline.invalid/v1" not in saved
    assert configs[0].command_child_env["CPA_BASE_URL"] == "https://offline.invalid/v1"
    monkeypatch.setenv("CPA_BASE_URL", "https://different.invalid/v1")
    tags.generate_upload_tags(TITLE, srt)
    assert len(calls) == 2


# Different processes exercise the ordinary entrypoints and provider identity.
COLD_DRIVER = r"""
import json,os,socket,sys,time
from copy import deepcopy
from pathlib import Path
from src.autoslice import normal_text_generation_checkpoint as checkpoint
from src.autoslice import publish_staging as staging
from scripts.suggest_upload_tags import generate_upload_tags
from src.autoslice.publish_staging_paths import validate_staged_editorial_files
root=Path(sys.argv[1]); kind=sys.argv[2]; mode=sys.argv[3]
srt=root/'candidate.srt'; log=root/'dispatch.jsonl'
def denied(*a,**k):raise RuntimeError('NETWORK_DISABLED')
socket.socket.connect=denied
# lidousha-title-cover section 4 shape (60-title L10-14): 2-4 compared candidates,
# each with a verbatim final-subtitle quote, exactly one SELECTED.
response=json.dumps({'title':'【李豆沙】读春雪时分享自己的读书感受','candidates':[{'title':'【李豆沙】读春雪时分享自己的读书感受','quote':{'text':'读书感受','speaker':''},'status':'SELECTED','rationale':'offline fixture'},{'title':'【李豆沙】只剩一句“读书感受”的备选','quote':{'text':'读书感受','speaker':''},'status':'REJECTED','rationale':'offline fixture'}],'editorial_choice':{'selected':'【李豆沙】读春雪时分享自己的读书感受','reason':'offline fixture'}}) if kind=='title' else '{"tags":[{"tag":"读书分享","why":"offline fixture"}]}'
def call(prompt):
 with log.open('a') as f:f.write(json.dumps({'pid':os.getpid(),'prompt':prompt})+'\n')
 if mode=='parallel':time.sleep(.2)
 return response
call.cpa_cache_identity={'transport':'offline-fixture','provider':'no-provider','models':['offline-model'],'effort':'high'}
if mode=='current-fail':
 original=checkpoint._atomic_json
 def atomic(path,value):
  if path.name=='CURRENT.json':raise OSError('offline CURRENT failure')
  return original(path,value)
 checkpoint._atomic_json=atomic
try:
 if kind=='title':
  publish=root/'candidate.publish.json'; original=Path.write_text
  def write(path,*a,**k):
   if mode=='save-fail' and path==publish:raise OSError('offline publish-save failure')
   return original(path,*a,**k)
  Path.write_text=write
  record=staging._stage_publish_draft({'status':'MATERIALIZED','candidate_id':'candidate','subtitle_path':str(srt),'media_path':str(root/'candidate.mp4')},candidate_id='candidate',title='candidate',cues=[],run_ffmpeg=False,title_llm_call=call,skip_cover=True,private_artifact_root=root)
  Path.write_text=original
  if record['publish_staging']['title_authority_error'] and mode=='current-fail':raise OSError('offline CURRENT failure')
  validate_staged_editorial_files(record,record['publish_staging'],candidate_id='candidate',subtitle_path=srt)
  (root/'staged.json').write_text(json.dumps(record,ensure_ascii=False))
  result={'status':'COMPLETED','attempt_status':record['publish_staging']['title_generation_attempts'][-1]['status'],'current_binding':record['publish_staging']['current_binding']}
 else:
  value=generate_upload_tags('【李豆沙】读春雪时分享自己的读书感受',srt,llm_call=call)
  if mode=='save-fail':raise OSError('offline parent record-save failure')
  if mode=='current-fail':raise OSError('offline CURRENT failure')
  (root/'candidate.record.json').write_text(json.dumps({'upload_tags':value},ensure_ascii=False))
  result={'status':'COMPLETED','tags':value}
except OSError as e:result={'status':'INJECTED_FAILURE','error':str(e)}
result['pid']=os.getpid();print(json.dumps(result,ensure_ascii=False))
"""


def start_cold(root, kind, mode):
    repo = Path(__file__).resolve().parents[1]
    return subprocess.Popen(
        [sys.executable, "-c", COLD_DRIVER, str(root), kind, mode],
        cwd=repo,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )


def finish_cold(process):
    stdout, stderr = process.communicate(timeout=60)
    assert process.returncode == 0, stderr
    return json.loads(stdout)


@pytest.mark.parametrize("kind", ["title", "tags"])
def test_ordinary_cold_save_failure_existing_output_reentry_reuses(tmp_path, kind):
    srt_at(tmp_path)
    (tmp_path / "candidate.mp4").write_bytes(b"OFFLINE_MEDIA_PLACEHOLDER")
    failed = finish_cold(start_cold(tmp_path, kind, "save-fail"))
    restarted = finish_cold(start_cold(tmp_path, kind, "restart"))
    existing = finish_cold(start_cold(tmp_path, kind, "existing"))
    assert failed["status"] == "INJECTED_FAILURE"
    assert restarted["status"] == existing["status"] == "COMPLETED"
    assert len({x["pid"] for x in (failed, restarted, existing)}) == 3
    assert len((tmp_path / "dispatch.jsonl").read_text().splitlines()) == 1
    if kind == "title":
        assert restarted["attempt_status"] == "ACCEPTED"
        assert restarted["current_binding"] == existing["current_binding"]
    else:
        assert restarted["tags"]["content_review_status"] == "COMPLETED"


@pytest.mark.parametrize("kind", ["title", "tags"])
def test_ordinary_cold_current_interruption_recovers(tmp_path, kind):
    srt_at(tmp_path)
    (tmp_path / "candidate.mp4").write_bytes(b"OFFLINE_MEDIA_PLACEHOLDER")
    failed = finish_cold(start_cold(tmp_path, kind, "current-fail"))
    restarted = finish_cold(start_cold(tmp_path, kind, "restart"))
    assert failed["status"] == "INJECTED_FAILURE"
    assert restarted["status"] == "COMPLETED"
    assert failed["pid"] != restarted["pid"]
    assert len((tmp_path / "dispatch.jsonl").read_text().splitlines()) == 1


@pytest.mark.parametrize("kind", ["title", "tags"])
def test_ordinary_concurrent_same_input_dispatches_once(tmp_path, kind):
    srt_at(tmp_path)
    (tmp_path / "candidate.mp4").write_bytes(b"OFFLINE_MEDIA_PLACEHOLDER")
    processes = [start_cold(tmp_path, kind, "parallel") for _ in range(2)]
    values = [finish_cold(p) for p in processes]
    assert all(x["status"] == "COMPLETED" for x in values)
    assert values[0]["pid"] != values[1]["pid"]
    assert len((tmp_path / "dispatch.jsonl").read_text().splitlines()) == 1


def test_canonical_adapter_endpoint_identity_includes_scheme_port_without_secret(tmp_path):
    from src.autoslice.llm_client import LlmConfig, build_llm_call

    script = tmp_path / "llm_via_cpa.sh"
    script.write_text("# offline request framing; never executed\n")
    config = LlmConfig(
        transport="command",
        command_template=f"bash {script} {{prompt_file}} {{completion_file}} 'gpt-6.1-sol' high 1",
        command_child_env={
            "CPA_BASE_URL": "https://offline.invalid:443/v1",
            "CPA_API_KEY": "OFFLINE-NO-CREDENTIAL",
        },
    )
    call = build_llm_call(config)
    first = checkpoint.provider_identity(call)
    assert first is not None
    config.command_child_env["CPA_BASE_URL"] = "https://offline.invalid:9443/v1"
    second = checkpoint.provider_identity(call)
    assert first != second
    assert "OFFLINE-NO-CREDENTIAL" not in json.dumps([first, second])
    assert "https://offline.invalid" not in json.dumps([first, second])


def test_canonical_request_framing_change_invalidates_raw_generation(tmp_path, monkeypatch):
    from src.autoslice import llm_client

    script = tmp_path / "llm_via_cpa.sh"
    script.write_text("# offline framing with max_output_tokens=16000; never executed\n")
    srt = srt_at(tmp_path)
    calls = []
    monkeypatch.setattr(llm_client, "_provider_dispatch", lambda call, runtime, **kwargs: call())
    monkeypatch.setattr(
        llm_client, "_call_command", lambda prompt, config: calls.append(prompt) or "raw complete"
    )
    call = llm_client.build_llm_call(
        llm_client.LlmConfig(
            transport="command",
            command_template=f"bash {script} {{prompt_file}} {{completion_file}} 'gpt-6.1-sol' high 1",
            command_child_env={"CPA_BASE_URL": "https://offline.invalid/v1"},
        )
    )
    run_raw(tmp_path, srt, call)
    run_raw(tmp_path, srt, call)
    assert len(calls) == 1
    script.write_text("# offline framing with max_output_tokens=32000; never executed\n")
    run_raw(tmp_path, srt, call)
    assert len(calls) == 2


def staged_response_after_rename_failure(root, monkeypatch):
    srt = srt_at(root)
    calls = []
    call = fixture_call("complete response before rename", calls)
    original = os.replace

    def replace(source, target):
        if Path(target).name == "result.json":
            raise OSError("offline result rename failure after inode fsync")
        return original(source, target)

    monkeypatch.setattr(os, "replace", replace)
    with pytest.raises(OSError):
        run_raw(root, srt, call)
    monkeypatch.setattr(os, "replace", original)
    staged = next(root.rglob(".result.json.*.tmp"))
    assert json.loads(staged.read_text())["response_text"] == "complete response before rename"
    return srt, call, calls, staged


def test_fsynced_result_before_rename_recovered_without_dispatch(tmp_path, monkeypatch):
    srt, call, calls, staged = staged_response_after_rename_failure(tmp_path, monkeypatch)
    original_bytes = staged.read_bytes()
    assert run_raw(tmp_path, srt, call) == "complete response before rename"
    assert len(calls) == 1
    assert next(tmp_path.rglob("result.json")).read_bytes() == original_bytes


def test_matching_staged_request_and_result_recovered(tmp_path, monkeypatch):
    srt, call, calls, staged = staged_response_after_rename_failure(tmp_path, monkeypatch)
    request = staged.parent / "request.json"
    original_request = request.read_bytes()
    request.rename(request.with_name(".request.json." + "e" * 32 + ".tmp"))
    assert run_raw(tmp_path, srt, call) == "complete response before rename"
    assert len(calls) == 1
    assert request.read_bytes() == original_request


@pytest.mark.parametrize(
    "damage", ["partial", "tamper", "conflict", "request_tamper", "request_missing"]
)
def test_staged_completion_partial_tamper_conflict_refuses_dispatch(tmp_path, monkeypatch, damage):
    srt, call, calls, staged = staged_response_after_rename_failure(tmp_path, monkeypatch)
    if damage == "partial":
        staged.write_bytes(staged.read_bytes()[:20])
    elif damage == "tamper":
        saved = json.loads(staged.read_text())
        saved["response_text"] = "changed response"
        staged.write_text(json.dumps(saved))
    elif damage == "conflict":
        saved = json.loads(staged.read_text())
        saved["response_text"] = "another complete response"
        saved["response_sha256"] = (
            "sha256:" + hashlib.sha256(saved["response_text"].encode()).hexdigest()
        )
        staged.with_name(".result.json." + "d" * 32 + ".tmp").write_text(json.dumps(saved))
    elif damage == "request_tamper":
        (staged.parent / "request.json").write_text("{}")
    else:
        (staged.parent / "request.json").unlink()
    with pytest.raises(ValueError):
        run_raw(tmp_path, srt, call)
    assert len(calls) == 1
