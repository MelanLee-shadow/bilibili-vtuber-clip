"""Unit tests for the unattended runner's pure helpers (first-real-run lessons)."""
import json
import hashlib
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import scripts.free_session_autoslice as runner
import src.autoslice.speaker_session_router as speaker_router
from src.autoslice.boundary_semantic_review import (
    build_boundary_search_scope,
)
from src.autoslice.chat_authority import recording_start_epoch_ms
from src.autoslice.producer_boundary_owner_contract import (
    freeze_required_boundary_owner_contract,
    frozen_boundary_owner_contract_sha256,
    redelivery_baseline_boundary_owner,
    validate_frozen_boundary_owner_contract,
)
from src.autoslice.talk_lane import (
    _bound_boundary_retry_source_end_ms,
)
from src.autoslice.visual_song_discovery import VisualSongCandidate, VisualSongDiscoveryResult
from src.autoslice.selection_scorecard import normalize_selection_scorecard
from src.autoslice.cover_route_evidence import (
    build_cover_route_decision,
    record_cover_route_execution,
)
from tests.host_vocal_test_support import bind_ready_live_performance_report, make_ready_host_vocal_claim
from scripts.free_session_autoslice import (
    COVER_REPAIR_MAX_ATTEMPTS,
    MAX_SONGS_PER_DATE,
    MAX_TALK_PICKS,
    cover_repair_needed,
    delivered_paths,
    last_json_block,
    prioritize,
    safe_name,
)


def test_last_json_block_parses_nested_produce_summary():
    """The produce log summary contains nested objects — a non-greedy regex
    failed on it (first run's summary showed 标题=(见log) everywhere)."""
    tail = 'noise\n{"a": 1}\nmore\n{"candidate_id": "x", "timing_qa": {"dropped": 0, "retimed": 3}, "title": "真标题"}\n'
    obj = last_json_block(tail)
    assert obj["title"] == "真标题"
    assert obj["timing_qa"]["retimed"] == 3


def _routing_item(tmp_path: Path) -> dict:
    segment = tmp_path / "segment.mp4"
    segment.write_bytes(b"sealed segment")
    srt = tmp_path / "segment.bcut.srt"
    srt.write_text(
        "1\n00:00:20,000 --> 00:00:22,000\n测试\n",
        encoding="utf-8",
    )
    return {
        "cid": "candidate-1",
        "segment_path": str(segment),
        "bcut_srt_path": str(srt),
        "seg_dur_ms": 120_000,
        "start_ms": 20_000,
        "end_ms": 30_000,
    }


@pytest.fixture(autouse=True)
def _empty_test_speaker_provider_allowlist():
    speaker_router.AUDITED_PROVIDER_BUNDLES.clear()
    yield
    speaker_router.AUDITED_PROVIDER_BUNDLES.clear()


def _allow_configured_test_speaker_provider() -> None:
    authority = runner._speaker_routing_provider_authority(require_audited=False)
    assert authority is not None
    speaker_router.AUDITED_PROVIDER_BUNDLES[
        str(authority["algorithm_fingerprint"])
    ] = str(authority["provider_bundle_fingerprint"])


def test_speaker_router_absent_provider_adds_no_hash_or_model_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = _routing_item(tmp_path)
    item["speaker_routing_claim"] = "stale"
    monkeypatch.delenv("AUTOSLICE_SPEAKER_ROUTING_PROVIDER_COMMAND_JSON", raising=False)
    monkeypatch.setattr(
        runner,
        "segment_binding_sha256",
        lambda _path: pytest.fail("no provider must not hash the recording"),
    )

    assert runner.prepare_speaker_routing("2026-07-10", [item]) is None
    assert "speaker_routing_claim" not in item
    assert "speaker_routing_candidate" not in item


def test_speaker_router_rejects_date_path_escape_before_any_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = tmp_path / "autoslice"
    monkeypatch.setattr(runner, "BASE", base)
    item = _routing_item(tmp_path)
    state: dict = {}

    assert (
        runner.prepare_speaker_routing("../../escape", [item], state=state) is None
    )
    assert "speaker_routing_session" not in state
    assert not (tmp_path / "escape").exists()
    assert not (base / "escape").exists()
    assert not (base / "state" / "escape").exists()
    assert not (base / "state" / "speaker-routing").exists()


def test_speaker_router_runtime_authority_changes_pipeline_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key in (
        "AUTOSLICE_SPEAKER_ROUTING_PROVIDER_COMMAND_JSON",
        "AUTOSLICE_SPEAKER_ROUTING_PROVIDER_NAME",
        "AUTOSLICE_SPEAKER_ROUTING_PROVIDER_ALGORITHM_ID",
        "AUTOSLICE_SPEAKER_ROUTING_PROVIDER_ARTIFACTS_JSON",
    ):
        monkeypatch.delenv(key, raising=False)
    baseline = runner.pipeline_fingerprint()
    monkeypatch.setenv("AUTOSLICE_SPEAKER_ROUTING_PROVIDER_NAME", "new-provider")
    assert runner.pipeline_fingerprint() != baseline


@pytest.mark.parametrize(
    "verdict,expected_decision",
    [("SOLO_HOST", "FAST_SOLO"), ("UNCERTAIN", "RUN_BINARY_FINALIZER")],
)
def test_speaker_router_provider_command_is_shell_free_and_fail_safe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    verdict: str,
    expected_decision: str,
) -> None:
    item = _routing_item(tmp_path)
    provider_script = tmp_path / "provider.py"
    provider_script.write_text(
        "import hashlib,json,sys\n"
        f"verdict={verdict!r}\n"
        "request_path,output_path=sys.argv[1:]\n"
        "payload=open(request_path,'rb').read()\n"
        "request=json.loads(payload)\n"
        "results=[]\n"
        "for candidate in request['candidates']:\n"
        " unresolved=1 if verdict=='UNCERTAIN' else 0\n"
        " evidence={'schema_version':'lidousha-speaker-routing-acoustic-evidence.v1',"
        "'source_media_sha256':candidate['segment_content_sha256'],"
        "'window_start_ms':candidate['start_ms'],'window_end_ms':candidate['end_ms'],"
        "'audio_window_sha256':'b'*64,'analyzed_speech_ms':1000,"
        "'coverage_unit_count':2,'covered_unit_count':2-unresolved,"
        "'unresolved_unit_count':unresolved,'speaker_count_lower_bound':1,"
        "'overlap_detected':False,'mixed_speaker_within_unit_detected':False,"
        "'acoustic_observation_sha256':'c'*64}\n"
        " results.append({'candidate_id':candidate['candidate_id'],"
        "'segment_binding_sha256':candidate['segment_binding_sha256'],"
        "'bcut_srt_sha256':candidate['bcut_srt_sha256'],'verdict':verdict,"
        "'complete_coverage':not unresolved,'unresolved_cue_count':unresolved,"
        "'mixed_or_overlap_detected':False,'evidence':evidence,"
        "'evidence_sha256':hashlib.sha256(json.dumps(evidence,sort_keys=True,"
        "separators=(',',':')).encode()).hexdigest()})\n"
        "document={'schema_version':'lidousha-speaker-routing-provider-evidence.v3',"
        "'status':'READY','request_sha256':hashlib.sha256(payload).hexdigest(),"
        "'pipeline_fingerprint':request['pipeline_fingerprint'],"
        "'routing_runtime_fingerprint':request['routing_runtime_fingerprint'],"
        "'input_modality':'audio','transcript_or_llm_used':False,"
        "'provider':request['provider_authority'],"
        "'candidate_results':results}\n"
        "open(output_path,'w').write(json.dumps(document))\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(runner, "BASE", tmp_path / "autoslice")
    monkeypatch.setattr(runner, "pipeline_fingerprint", lambda: "sha256:" + "9" * 64)
    monkeypatch.setenv(
        "AUTOSLICE_SPEAKER_ROUTING_PROVIDER_COMMAND_JSON",
        json.dumps(
            [
                sys.executable,
                str(provider_script),
                "{request}",
                "{output}",
            ]
        ),
    )
    monkeypatch.setenv("AUTOSLICE_SPEAKER_ROUTING_PROVIDER_NAME", "fixture")
    monkeypatch.setenv("AUTOSLICE_SPEAKER_ROUTING_PROVIDER_ALGORITHM_ID", "fixture-v1")
    artifacts = {}
    for role in ("config", "model", "profile"):
        path = tmp_path / f"provider-{role}.bin"
        path.write_bytes(f"sealed {role}".encode())
        artifacts[role] = str(path)
    artifacts.update({"executable": str(Path(sys.executable).resolve()), "script": str(provider_script)})
    monkeypatch.setenv(
        "AUTOSLICE_SPEAKER_ROUTING_PROVIDER_ARTIFACTS_JSON", json.dumps(artifacts)
    )
    command_template = json.loads(
        os.environ["AUTOSLICE_SPEAKER_ROUTING_PROVIDER_COMMAND_JSON"]
    )
    command_template[0] = str(Path(sys.executable).resolve())
    monkeypatch.setenv(
        "AUTOSLICE_SPEAKER_ROUTING_PROVIDER_COMMAND_JSON", json.dumps(command_template)
    )
    _allow_configured_test_speaker_provider()

    claim = runner.prepare_speaker_routing("2026-07-10", [item])

    assert claim["decision"] == expected_decision
    assert Path(item["speaker_routing_claim"]).is_file()
    assert item["speaker_routing_candidate"]["start_ms"] == 10_000
    assert item["speaker_routing_candidate"]["end_ms"] == 91_000
    assert item["speaker_routing_candidate"]["pipeline_fingerprint"] == "sha256:" + "9" * 64


def _configure_provider_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    declared_script: Path,
    executed_script: Path | None = None,
) -> None:
    executable = str(Path(sys.executable).resolve())
    monkeypatch.setenv(
        "AUTOSLICE_SPEAKER_ROUTING_PROVIDER_COMMAND_JSON",
        json.dumps(
            [
                executable,
                str(executed_script or declared_script),
                "{request}",
                "{output}",
            ]
        ),
    )
    monkeypatch.setenv("AUTOSLICE_SPEAKER_ROUTING_PROVIDER_NAME", "fixture")
    monkeypatch.setenv("AUTOSLICE_SPEAKER_ROUTING_PROVIDER_ALGORITHM_ID", "fixture-v1")
    artifacts = {"executable": executable, "script": str(declared_script)}
    for role in ("config", "model", "profile"):
        path = tmp_path / f"closure-{role}.bin"
        path.write_bytes(role.encode())
        artifacts[role] = str(path)
    monkeypatch.setenv(
        "AUTOSLICE_SPEAKER_ROUTING_PROVIDER_ARTIFACTS_JSON", json.dumps(artifacts)
    )


def test_production_empty_allowlist_never_executes_external_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = _routing_item(tmp_path)
    marker = tmp_path / "executed"
    script = tmp_path / "untrusted-provider.py"
    script.write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n",
        encoding="utf-8",
    )
    _configure_provider_artifacts(tmp_path, monkeypatch, declared_script=script)
    monkeypatch.setattr(runner, "BASE", tmp_path / "autoslice")

    assert runner.prepare_speaker_routing("2026-07-10", [item]) is None
    assert not marker.exists()
    assert "speaker_routing_claim" not in item


def test_declared_benign_script_cannot_hide_executed_transcript_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = _routing_item(tmp_path)
    benign = tmp_path / "benign-acoustic.py"
    benign.write_text("raise SystemExit('test-only benign bundle')\n", encoding="utf-8")
    marker = tmp_path / "transcript-script-executed"
    transcript = tmp_path / "transcript-provider.py"
    transcript.write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n",
        encoding="utf-8",
    )
    _configure_provider_artifacts(
        tmp_path,
        monkeypatch,
        declared_script=benign,
        executed_script=transcript,
    )
    artifacts = json.loads(
        os.environ["AUTOSLICE_SPEAKER_ROUTING_PROVIDER_ARTIFACTS_JSON"]
    )
    authority = speaker_router.build_provider_authority(
        name="fixture",
        algorithm_id="fixture-v1",
        artifact_paths=artifacts,
    )
    speaker_router.AUDITED_PROVIDER_BUNDLES[
        str(authority["algorithm_fingerprint"])
    ] = str(authority["provider_bundle_fingerprint"])
    monkeypatch.setattr(runner, "BASE", tmp_path / "autoslice")

    assert runner.prepare_speaker_routing("2026-07-10", [item]) is None
    assert not marker.exists()
    assert "speaker_routing_claim" not in item


def test_inline_or_extra_provider_argv_is_rejected() -> None:
    request = Path("/tmp/request.json")
    output = Path("/tmp/output.json")
    for argv in (
        [str(Path(sys.executable).resolve()), "-c", "print('unsafe')", "{request}", "{output}"],
        [str(Path(sys.executable).resolve()), "/provider.py", "/undeclared/model", "{request}", "{output}"],
    ):
        os.environ["AUTOSLICE_SPEAKER_ROUTING_PROVIDER_COMMAND_JSON"] = json.dumps(argv)
        try:
            with pytest.raises(ValueError, match="exactly executable, script"):
                runner._speaker_routing_provider_command(
                    request_path=request, output_path=output
                )
        finally:
            os.environ.pop("AUTOSLICE_SPEAKER_ROUTING_PROVIDER_COMMAND_JSON", None)


def test_speaker_router_seals_full_inventory_and_keeps_binary_on_subset_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    solo_dir = tmp_path / "solo"
    collab_dir = tmp_path / "collab"
    solo_dir.mkdir()
    collab_dir.mkdir()
    solo = _routing_item(solo_dir)
    collab = _routing_item(collab_dir)
    solo["cid"] = "solo"
    collab["cid"] = "collab"
    provider_script = tmp_path / "provider.py"
    provider_script.write_text(
        "import hashlib,json,sys\n"
        "request_path,output_path=sys.argv[1:]\n"
        "payload=open(request_path,'rb').read(); request=json.loads(payload)\n"
        "results=[]\n"
        "for index,candidate in enumerate(request['candidates']):\n"
        " verdict='SOLO_HOST' if index==0 else 'MULTI_SPEAKER'\n"
        " evidence={'schema_version':'lidousha-speaker-routing-acoustic-evidence.v1',"
        "'source_media_sha256':candidate['segment_content_sha256'],"
        "'window_start_ms':candidate['start_ms'],'window_end_ms':candidate['end_ms'],"
        "'audio_window_sha256':'b'*64,'analyzed_speech_ms':1000,"
        "'coverage_unit_count':1,'covered_unit_count':1,'unresolved_unit_count':0,"
        "'speaker_count_lower_bound':1 if index==0 else 2,"
        "'overlap_detected':False,'mixed_speaker_within_unit_detected':False,"
        "'acoustic_observation_sha256':'c'*64}\n"
        " results.append({'candidate_id':candidate['candidate_id'],"
        "'segment_binding_sha256':candidate['segment_binding_sha256'],"
        "'bcut_srt_sha256':candidate['bcut_srt_sha256'],'verdict':verdict,"
        "'complete_coverage':True,'unresolved_cue_count':0,"
        "'mixed_or_overlap_detected':False,'evidence':evidence,"
        "'evidence_sha256':hashlib.sha256(json.dumps(evidence,sort_keys=True,"
        "separators=(',',':')).encode()).hexdigest()})\n"
        "document={'schema_version':'lidousha-speaker-routing-provider-evidence.v3',"
        "'status':'READY','request_sha256':hashlib.sha256(payload).hexdigest(),"
        "'pipeline_fingerprint':request['pipeline_fingerprint'],"
        "'routing_runtime_fingerprint':request['routing_runtime_fingerprint'],"
        "'input_modality':'audio','transcript_or_llm_used':False,"
        "'provider':request['provider_authority'],'candidate_results':results}\n"
        "open(output_path,'w').write(json.dumps(document))\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(runner, "BASE", tmp_path / "autoslice")
    monkeypatch.setattr(runner, "pipeline_fingerprint", lambda: "sha256:" + "9" * 64)
    monkeypatch.setenv(
        "AUTOSLICE_SPEAKER_ROUTING_PROVIDER_COMMAND_JSON",
        json.dumps([str(Path(sys.executable).resolve()), str(provider_script), "{request}", "{output}"]),
    )
    monkeypatch.setenv("AUTOSLICE_SPEAKER_ROUTING_PROVIDER_NAME", "fixture")
    monkeypatch.setenv("AUTOSLICE_SPEAKER_ROUTING_PROVIDER_ALGORITHM_ID", "fixture-v1")
    artifacts = {"executable": str(Path(sys.executable).resolve()), "script": str(provider_script)}
    for role in ("config", "model", "profile"):
        path = tmp_path / f"sticky-{role}.bin"
        path.write_bytes(role.encode())
        artifacts[role] = str(path)
    monkeypatch.setenv(
        "AUTOSLICE_SPEAKER_ROUTING_PROVIDER_ARTIFACTS_JSON", json.dumps(artifacts)
    )
    _allow_configured_test_speaker_provider()
    state: dict = {}

    first = runner.prepare_speaker_routing(
        "2026-07-10", [solo, collab], state=state
    )
    first_claim = solo["speaker_routing_claim"]
    assert first["decision"] == "RUN_BINARY_FINALIZER"
    assert state["speaker_routing_session"]["provider_classification_sealed"] is True
    assert [row["cid"] for row in state["speaker_routing_session"]["sealed_inventory"]] == [
        "solo",
        "collab",
    ]

    monkeypatch.delenv("AUTOSLICE_SPEAKER_ROUTING_PROVIDER_COMMAND_JSON")
    retry = _routing_item(solo_dir)
    retry["cid"] = "solo"
    second = runner.prepare_speaker_routing("2026-07-10", [retry], state=state)

    assert second["decision"] == "RUN_BINARY_FINALIZER"
    assert retry["speaker_routing_claim"] == first_claim
    assert len(second["candidate_results"]) == 2
    authority_path = Path(
        state["speaker_routing_session"]["authority_path"]
    )
    assert authority_path.is_file()
    assert "/generations/" in str(authority_path)

    state["speaker_routing_session"]["sealed_inventory"] = [
        state["speaker_routing_session"]["sealed_inventory"][0]
    ]
    tampered_retry = _routing_item(solo_dir)
    tampered_retry["cid"] = "solo"
    assert (
        runner.prepare_speaker_routing(
            "2026-07-10", [tampered_retry], state=state
        )
        is None
    )
    assert "speaker_routing_claim" not in tampered_retry
    assert state["speaker_routing_authority_status"] == "ROLLBACK_OR_TAMPER_DETECTED"

    state.pop("speaker_routing_session")
    deleted_retry = _routing_item(solo_dir)
    deleted_retry["cid"] = "solo"
    assert (
        runner.prepare_speaker_routing(
            "2026-07-10", [deleted_retry], state=state
        )
        is None
    )
    assert "speaker_routing_claim" not in deleted_retry
    assert state["speaker_routing_authority_status"] == (
        "ROLLBACK_STATE_AUTHORITY_MISSING"
    )


def test_speaker_router_new_unmatched_candidate_stays_binary_unbound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    known = _routing_item(tmp_path)
    state = {
        "speaker_routing_session": {
            "schema_version": "lidousha-speaker-routing-session.v1",
            "date": "2026-07-10",
            "sealed_inventory": [],
            "sealed_inventory_sha256": hashlib.sha256(b"[]").hexdigest(),
            "decision": None,
            "claim_path": None,
            "claim_sha256": None,
            "pipeline_fingerprint": None,
            "provider_classification_sealed": False,
        }
    }
    monkeypatch.delenv("AUTOSLICE_SPEAKER_ROUTING_PROVIDER_COMMAND_JSON", raising=False)

    assert runner.prepare_speaker_routing("2026-07-10", [known], state=state) is None
    assert "speaker_routing_claim" not in known


def test_speaker_router_new_pipeline_generation_reuses_full_external_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    first_dir.mkdir()
    second_dir.mkdir()
    solo = _routing_item(first_dir)
    collab = _routing_item(second_dir)
    solo["cid"] = "solo"
    collab["cid"] = "collab"
    monkeypatch.setattr(runner, "BASE", tmp_path / "autoslice")
    monkeypatch.delenv(
        "AUTOSLICE_SPEAKER_ROUTING_PROVIDER_COMMAND_JSON", raising=False
    )
    generation_a = "sha256:" + "a" * 64
    generation_b = "sha256:" + "b" * 64
    monkeypatch.setattr(runner, "pipeline_fingerprint", lambda: generation_a)
    state: dict = {}

    assert (
        runner.prepare_speaker_routing(
            "2026-07-10", [solo, collab], state=state
        )
        is None
    )
    first_authority = state["speaker_routing_session"]["authority_path"]
    monkeypatch.setattr(runner, "pipeline_fingerprint", lambda: generation_b)
    retry = _routing_item(first_dir)
    retry["cid"] = "solo"

    assert (
        runner.prepare_speaker_routing("2026-07-10", [retry], state=state)
        is None
    )
    session = state["speaker_routing_session"]
    assert session["generation_pipeline_fingerprint"] == generation_b
    assert [row["cid"] for row in session["sealed_inventory"]] == [
        "solo",
        "collab",
    ]
    new_authority = json.loads(Path(session["authority_path"]).read_text())
    assert new_authority["previous_authority_path"] == first_authority


def test_last_json_block_empty_on_garbage():
    assert last_json_block("no json here }{ broken") == {}


def test_song_selector_env_pins_term_context_to_recording_date(monkeypatch):
    monkeypatch.setattr(runner, "child_env", lambda: {"BASE": "kept"})

    env = runner.song_selector_env("2026-07-10")

    assert env["BASE"] == "kept"
    assert env["LIDOUSHA_TERM_AS_OF"] == "2026-07-10"


def test_child_env_pins_timely_snapshot_path_and_hash(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(runner, "CPA_ENV", tmp_path / "missing.env")
    snapshot = tmp_path / "assets" / "lidousha" / "timely_terms.json"
    snapshot.parent.mkdir(parents=True)
    snapshot.write_bytes(b'{"snapshot":"bytes"}\n')

    env = runner.child_env_for_date("2026-07-10")

    assert env["LIDOUSHA_TIMELY_TERMS"] == str(snapshot.resolve())
    assert env["LIDOUSHA_TIMELY_TERMS_SHA256"] == (
        "sha256:" + hashlib.sha256(snapshot.read_bytes()).hexdigest()
    )
    assert env["LIDOUSHA_TERM_AS_OF"] == "2026-07-10"


def test_child_env_imports_only_named_gemini_fallback_keys(tmp_path, monkeypatch):
    bilive_env = tmp_path / "bilive.env"
    bilive_env.write_text(
        "GEMINI_API_KEY=primary\nGEMINI_API_KEY_2=secondary\nRECORD_KEY=must-not-leak\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(runner, "BILIVE_ENV", bilive_env)
    monkeypatch.setattr(runner, "CPA_ENV", tmp_path / "missing-cpa.env")

    env = runner.child_env()

    assert env["GEMINI_API_KEY"] == "primary"
    assert env["GEMINI_API_KEY_2"] == "secondary"
    assert env.get("RECORD_KEY") != "must-not-leak"


def test_child_env_prefers_runtime_crawler_snapshot_without_dirtying_repo(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    base = tmp_path / "runtime"
    monkeypatch.setattr(runner, "REPO_ROOT", repo)
    monkeypatch.setattr(runner, "BASE", base)
    monkeypatch.setattr(runner, "CPA_ENV", tmp_path / "missing.env")
    committed = repo / "assets/lidousha/timely_terms.json"
    runtime = base / "state/timely_terms.json"
    committed.parent.mkdir(parents=True)
    runtime.parent.mkdir(parents=True)
    committed.write_bytes(b'{"source":"committed"}\n')
    runtime.write_bytes(b'{"source":"crawler"}\n')

    env = runner.child_env_for_date("2026-07-12")

    assert env["LIDOUSHA_TIMELY_TERMS"] == str(runtime.resolve())
    assert env["LIDOUSHA_TIMELY_TERMS_SHA256"] == (
        "sha256:" + hashlib.sha256(runtime.read_bytes()).hexdigest()
    )


def test_child_env_prefers_runtime_psplive_roster_snapshot(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    base = tmp_path / "runtime"
    monkeypatch.setattr(runner, "REPO_ROOT", repo)
    monkeypatch.setattr(runner, "BASE", base)
    monkeypatch.setattr(runner, "CPA_ENV", tmp_path / "missing.env")
    committed = repo / "assets/lidousha/psplive_roster.v1.md"
    runtime = base / "state/psplive_roster.json"
    committed.parent.mkdir(parents=True)
    runtime.parent.mkdir(parents=True)
    committed.write_text("fallback roster\n", encoding="utf-8")
    runtime.write_bytes(b'{"source":"official-crawler"}\n')

    env = runner.child_env_for_date("2026-07-16")

    assert env["LIDOUSHA_PSPLIVE_ROSTER"] == str(runtime.resolve())
    assert env["LIDOUSHA_PSPLIVE_ROSTER_SHA256"] == (
        "sha256:" + hashlib.sha256(runtime.read_bytes()).hexdigest()
    )


def test_safe_name_sanitizes_and_falls_back():
    assert safe_name("百合是工作？/她当场*不买书", "cid") == "百合是工作？她当场不买书"
    assert safe_name("", "cid") == "cid"
    assert len(safe_name("很长" * 40, "cid")) <= 18


def test_discover_segments_unions_visual_songs_and_attaches_title_hint(tmp_path, monkeypatch):
    segment = tmp_path / "22966160_20260710-21-49-59.mp4"
    with segment.open("wb") as target:
        target.seek(runner.MIN_SEGMENT_BYTES)
        target.write(b"x")
    srt = tmp_path / "segment.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:01,000\n歌词\n", encoding="utf-8")
    monkeypatch.setattr(runner, "list_segments", lambda _date: [segment])
    monkeypatch.setattr(runner, "bcut_transcribe", lambda *_args: srt)
    monkeypatch.setattr(runner, "find_danmaku_xml", lambda _segment: None)
    monkeypatch.setattr(runner, "find_chat_jsonl", lambda _segment: None)
    monkeypatch.setattr(
        runner,
        "resolve_structured_chat_binding",
        lambda *_args, **_kwargs: {
            "structured_chat_required": False,
            "chat_binding_status": "NOT_REGISTERED",
        },
    )
    monkeypatch.setattr(runner, "danmaku_hints", lambda _xml: None)
    monkeypatch.setattr(runner, "danmaku_count_in", lambda *_args: 0)
    monkeypatch.setattr(runner, "ffprobe_ms", lambda _segment: 700_000)
    asr_song = SimpleNamespace(
        anchor=SimpleNamespace(candidate_id="semanticsong_100000_260000", anchor_start_ms=100_000, anchor_end_ms=260_000),
        content_type_hint="song",
        text_preview="ASR歌词",
    )
    monkeypatch.setattr(
        runner,
        "recall_candidates",
        lambda *_args: ([asr_song], "semantic_recall", {"semanticsong_100000_260000": {"hook": "ASR召回", "confidence": 0.8}}),
    )
    visual = VisualSongDiscoveryResult(
        status="READY",
        candidates=(
            VisualSongCandidate("晴る", 90_000, 280_000, {"list_index": 10}, 0.98),
            VisualSongCandidate("太阳系disco", 400_000, 620_000, {"list_index": 15}, 0.97),
        ),
        cache_path=str(tmp_path / "cache.json"),
        content_fingerprint="a" * 64,
        config_sha256="b" * 64,
    )
    monkeypatch.setattr(runner, "discover_visual_songs", lambda *_args, **_kwargs: visual)
    monkeypatch.setattr(runner, "BASE", tmp_path / "base")
    state = {}

    runner.discover_segments("2026-07-10", state)

    assert len(state["pending_song"]) == 2
    matched, visual_only = state["pending_song"]
    assert matched["cid"] == "song_214959_100"
    assert matched["title_hint"] == "晴る"
    assert matched["visual_song_matched_to_asr"] is True
    assert visual_only["cid"].startswith("songvis_214959_400_")
    assert visual_only["title_hint"] == "太阳系disco"
    assert len(state["song_quarantine_intervals"]) == 2
    assert state["visual_song_seen_entries"] == ["list:10:晴る", "list:15:太阳系disco"]


def test_discover_segments_calibrates_with_final_stable_candidate_id(
    tmp_path, monkeypatch
):
    segment = tmp_path / "22966160_20260722-19-34-50.mp4"
    with segment.open("wb") as target:
        target.seek(runner.MIN_SEGMENT_BYTES)
        target.write(b"x")
    srt = tmp_path / "segment.srt"
    srt.write_text(
        "1\n00:59:33,000 --> 00:59:35,000\n金发有角妹妹\n\n"
        "2\n01:01:03,000 --> 01:01:05,000\n男角色不熟\n",
        encoding="utf-8",
    )
    original = normalize_selection_scorecard(
        {
            "tier": 1,
            "tier_basis": "cp_positioning",
            "tier_reason": "金发有角妹妹与礼墨联想、男性角色反转",
            "tier_evidence_cues": [1, 2],
            "dimensions": {
                "lidousha_centrality": 4,
                "stance_intensity": 4,
                "audience_salience": 4,
                "relationship_interaction": 3,
                "persona_reversal": 4,
                "comedic_payoff": 4,
                "self_contained": 4,
            },
            "uncertainty_penalty": 1,
            "fatigue_penalty": 0,
        },
        start_cue=1,
        end_cue=2,
    )
    assert original is not None and original["effective_score"] == 95.25
    candidate = SimpleNamespace(
        anchor=SimpleNamespace(
            candidate_id="semantictalk_3573000_3665000",
            anchor_start_ms=3_573_000,
            anchor_end_ms=3_665_000,
        ),
        boundary=SimpleNamespace(
            resolved_start_ms=3_573_000,
            resolved_end_ms=3_665_000,
        ),
        content_type_hint="talk",
        text_preview="金发有角妹妹",
    )
    monkeypatch.setattr(runner, "list_segments", lambda _date: [segment])
    monkeypatch.setattr(runner, "bcut_transcribe", lambda *_args: srt)
    monkeypatch.setattr(runner, "find_danmaku_xml", lambda _segment: None)
    monkeypatch.setattr(runner, "find_chat_jsonl", lambda _segment: None)
    monkeypatch.setattr(
        runner,
        "resolve_structured_chat_binding",
        lambda *_args, **_kwargs: {
            "structured_chat_required": False,
            "chat_binding_status": "NOT_REGISTERED",
        },
    )
    monkeypatch.setattr(runner, "danmaku_hints", lambda _xml: None)
    monkeypatch.setattr(runner, "ffprobe_ms", lambda _segment: 4_000_000)
    monkeypatch.setattr(
        runner,
        "recall_candidates",
        lambda *_args: (
            [candidate],
            "semantic_recall",
            {
                candidate.anchor.candidate_id: {
                    "hook": "金发有角妹妹与男角色反转",
                    "confidence": 0.99,
                    "selection_scorecard": original,
                }
            },
        ),
    )
    monkeypatch.setattr(
        runner,
        "discover_visual_songs",
        lambda *_args, **_kwargs: VisualSongDiscoveryResult(
            status="READY",
            candidates=(),
            cache_path=str(tmp_path / "visual.json"),
            content_fingerprint="a" * 64,
            config_sha256="b" * 64,
        ),
    )
    monkeypatch.setattr(runner, "BASE", tmp_path / "base")
    state = {}

    runner.discover_segments("2026-07-22", state)

    assert state["pending_talk"][0]["cid"] == "auto_193450_3573_3665"
    assert (
        state["pending_talk"][0]["selection_scorecard"]["effective_score"]
        == 80.25
    )


# --- song_name_candidates (Ivan 2026-07-13, talk-lane pinning) --------------


def test_visual_song_titles_reads_inventory_and_both_seen_entry_forms():
    """Source A: the numbered songlist panel's inventory entries plus the
    cross-segment dedup identities (``list:<n>:<title>`` / ``media:<stem>:<start_ms>:<title>``)."""

    state = {
        "visual_song_inventory": {
            "seg_a": {
                "candidates": [
                    {"song_title": "爱啦啦", "start_ms": 0, "end_ms": 1000},
                    "not-a-dict-must-be-skipped",
                    {"start_ms": 0, "end_ms": 1000},  # missing song_title, skipped
                ]
            },
            "seg_b_not_dict": "ignored",
        },
        "visual_song_seen_entries": [
            "list:10:晴る",
            "media:22966160_20260710-19-00-17:400000:太阳系disco",
            "list:10:晴る",  # duplicate identity — must not double-add
            "malformed-entry-without-colon-form",
        ],
    }
    titles = runner.visual_song_titles(state)
    assert titles == ["爱啦啦", "晴る", "太阳系disco"]


def test_dian_ge_song_titles_reuses_chat_authority_parser(tmp_path, monkeypatch):
    """Reuses ``src.autoslice.chat_authority.load_chat_jsonl`` — the same
    authoritative structured danmaku/SC parser — instead of a second bilibili
    DANMU_MSG decoder."""

    segment = tmp_path / "22966160_20260710-19-00-17.mp4"
    segment.write_bytes(b"seg")
    jsonl = segment.with_suffix(".jsonl")
    base = 1_752_150_000
    rows = [
        {"cmd": "DANMU_MSG", "info": [[0, 0, 0, 0, base], "点歌 爱啦啦"]},
        {"cmd": "DANMU_MSG", "info": [[0, 0, 0, 0, base + 1], "点歌   屑屑！！"]},
        {"cmd": "DANMU_MSG", "info": [[0, 0, 0, 0, base + 2], "点歌爱啦啦"]},  # dup identity, folded
        {"cmd": "DANMU_MSG", "info": [[0, 0, 0, 0, base + 3], "随便聊聊天"]},  # no 点歌 prefix
        {
            "cmd": "SUPER_CHAT_MESSAGE",
            "send_time": (base + 4) * 1000,
            "data": {"id": 1, "ts": base + 4, "message": "点歌 屑屑", "user_info": {"uname": "x"}},
        },  # superchat is NOT part of the 点歌 danmaku pool
        {
            "cmd": "DANMU_MSG",
            "info": [[0, 0, 0, 0, base + 5], "点歌 " + "长" * 25],
        },  # over length-20 cap, dropped
    ]
    jsonl.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")
    monkeypatch.setattr(runner, "list_segments", lambda _date: [segment])
    monkeypatch.setattr(runner, "find_chat_jsonl", lambda _segment: jsonl)

    titles = runner.dian_ge_song_titles("2026-07-10")

    assert titles == ["爱啦啦", "屑屑"]


def test_dian_ge_song_titles_caps_pool(tmp_path, monkeypatch):
    segment = tmp_path / "22966160_20260710-19-00-17.mp4"
    segment.write_bytes(b"seg")
    jsonl = segment.with_suffix(".jsonl")
    base = 1_752_150_000
    rows = [
        {"cmd": "DANMU_MSG", "info": [[0, 0, 0, 0, base + i], f"点歌 歌曲{i}"]}
        for i in range(60)
    ]
    jsonl.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")
    monkeypatch.setattr(runner, "list_segments", lambda _date: [segment])
    monkeypatch.setattr(runner, "find_chat_jsonl", lambda _segment: jsonl)

    titles = runner.dian_ge_song_titles("2026-07-10", cap=40)

    assert len(titles) == 40
    assert titles[0] == "歌曲0"


def test_known_song_titles_reads_repo_asset(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    known = repo / "assets" / "lidousha"
    known.mkdir(parents=True)
    (known / "known_songs.json").write_text(
        json.dumps({"songs": [{"title": "屑屑"}, {"title": "芽吹くとき"}, {"not_a_title": "x"}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(runner, "REPO_ROOT", repo)

    assert runner.known_song_titles() == ["屑屑", "芽吹くとき"]


def test_known_song_titles_fails_open_on_missing_asset(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "REPO_ROOT", tmp_path / "no-such-repo")
    assert runner.known_song_titles() == []


def test_collect_song_name_candidates_combines_and_dedupes_all_sources(monkeypatch):
    monkeypatch.setattr(runner, "visual_song_titles", lambda _state: ["爱啦啦", "晴る"])
    monkeypatch.setattr(runner, "dian_ge_song_titles", lambda _date, **_kwargs: ["屑屑", "爱拉拉"])  # ASR-fold dup of 爱啦啦
    monkeypatch.setattr(runner, "known_song_titles", lambda: ["屑屑", "芽吹くとき"])

    titles = runner.collect_song_name_candidates("2026-07-10", {})

    # 爱拉拉 folds to the same identity as 爱啦啦 (normalize_visual_title only
    # strips separators/case, so this is testing exact-string dedup here);
    # 屑屑 from known_songs must not duplicate the one already added from 点歌.
    assert titles == ["爱啦啦", "晴る", "屑屑", "爱拉拉", "芽吹くとき"]


def test_collect_song_name_candidates_respects_cap(monkeypatch):
    monkeypatch.setattr(runner, "visual_song_titles", lambda _state: [f"曲{i}" for i in range(80)])
    monkeypatch.setattr(runner, "dian_ge_song_titles", lambda _date, **_kwargs: [])
    monkeypatch.setattr(runner, "known_song_titles", lambda: [])

    titles = runner.collect_song_name_candidates("2026-07-10", {}, cap=60)

    assert len(titles) == 60


def test_produce_talk_threads_song_name_candidates_into_spec(tmp_path, monkeypatch):
    date = "2026-07-11"
    base = tmp_path / "autoslice"
    repo = tmp_path / "repo"
    (base / "logs").mkdir(parents=True)
    repo.mkdir()
    monkeypatch.setattr(runner, "BASE", base)
    monkeypatch.setattr(runner, "REPO_ROOT", repo)
    monkeypatch.setattr(runner, "child_env", lambda: {})
    monkeypatch.setattr(runner, "pipeline_fingerprint", lambda: "sha256:test")

    class Completed:
        returncode = 1

    def fake_run(_command, **kwargs):
        kwargs["stdout"].write("CPA temporarily unavailable\n")
        kwargs["stdout"].flush()
        return Completed()

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    runner.produce_talk(
        date,
        {
            "cid": "auto_songname_100_150",
            "segment_path": "/recordings/segment.mp4",
            "seg_dur_ms": 900_000,
            "start_ms": 100_000,
            "end_ms": 150_000,
            "hook": "test",
            "song_name_candidates": ["爱啦啦", "屑屑"],
        },
    )

    spec = json.loads((base / "out" / date / "spec_auto_songname_100_150.json").read_text())
    assert spec["song_name_candidates"] == ["爱啦啦", "屑屑"]


def test_produce_talk_omits_song_name_candidates_when_absent(tmp_path, monkeypatch):
    date = "2026-07-11"
    base = tmp_path / "autoslice"
    repo = tmp_path / "repo"
    (base / "logs").mkdir(parents=True)
    repo.mkdir()
    monkeypatch.setattr(runner, "BASE", base)
    monkeypatch.setattr(runner, "REPO_ROOT", repo)
    monkeypatch.setattr(runner, "child_env", lambda: {})
    monkeypatch.setattr(runner, "pipeline_fingerprint", lambda: "sha256:test")

    class Completed:
        returncode = 1

    def fake_run(_command, **kwargs):
        kwargs["stdout"].write("CPA temporarily unavailable\n")
        kwargs["stdout"].flush()
        return Completed()

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    runner.produce_talk(
        date,
        {
            "cid": "auto_nosongname_100_150",
            "segment_path": "/recordings/segment.mp4",
            "seg_dur_ms": 900_000,
            "start_ms": 100_000,
            "end_ms": 150_000,
            "hook": "test",
        },
    )

    spec = json.loads((base / "out" / date / "spec_auto_nosongname_100_150.json").read_text())
    assert "song_name_candidates" not in spec


def test_prioritize_caps_talk_and_songs_with_reasons():
    state = {
        "picks": [], "songs": [],
        "pending_talk": [
            {"segment_path": f"/rec/talkseg{i % 2}.mp4", "start_ms": i * 1000, "end_ms": i * 1000 + 30_000,
             "hook": f"hook{i}", "confidence": 0.9, "cid": f"auto_{i}"}
            for i in range(MAX_TALK_PICKS + 3)
        ],
        "pending_song": [
            {"segment_path": "/rec/seg0.mp4", "anchor_start_ms": i * 10_000, "anchor_end_ms": i * 10_000 + 60_000,
             "danmaku": i * 10, "cid": f"song_{i}", "hook": "", "preview": ""}
            for i in range(MAX_SONGS_PER_DATE + 2)
        ],
    }
    prioritize(state)
    assert len(state["pending_talk"]) == MAX_TALK_PICKS
    assert len(state["not_selected"]) == 3
    assert len(state["talk_backlog"]) == 3
    assert len(state["pending_song"]) == MAX_SONGS_PER_DATE
    # 新策略只保留弹幕最高的一首。
    assert [s["danmaku"] for s in state["pending_song"]] == [20]
    # backlog 保留结构化条目（门拦截后回填的来源），不再是只读字符串
    assert len(state["song_backlog"]) == 2
    assert all(isinstance(b, dict) for b in state["song_backlog"])


def _delivered_talk_pick(tmp_path, monkeypatch, *, with_cover: bool) -> dict:
    """A delivered ok talk pick in a fake repo delivery dir (2026-07-06 real
    case: CPA image gateway 400 blanked covers while the mp4/title were fine)."""
    monkeypatch.setattr(runner, "REPO_ROOT", tmp_path)
    rec = {"candidate_id": "auto_1_2_3", "status": "ok", "hook": "钩子",
           "title": "【李豆沙】标题", "cover_status": "BLOCKED_AI_COVER_REQUIRED"}
    delivery = tmp_path / "lidousha" / "2026-07-06"
    delivery.mkdir(parents=True)
    (delivery / f"{safe_name('钩子', 'auto_1_2_3')}.mp4").write_bytes(b"mp4")
    if with_cover:
        (delivery / f"{safe_name('钩子', 'auto_1_2_3')}.cover.png").write_bytes(b"png")
    return rec


def test_cover_repair_needed_for_delivered_pick_without_cover(tmp_path, monkeypatch):
    rec = _delivered_talk_pick(tmp_path, monkeypatch, with_cover=False)
    assert cover_repair_needed("2026-07-06", rec)
    mp4, cover = delivered_paths("2026-07-06", rec)
    assert mp4.is_file() and cover.name.endswith(".cover.png")


def test_blocked_stale_cover_is_repaired_even_when_file_exists(tmp_path, monkeypatch):
    rec = _delivered_talk_pick(tmp_path, monkeypatch, with_cover=True)
    assert cover_repair_needed("2026-07-06", rec)
    rec["cover_status"] = "AI_COVER_READY"
    # A bare PNG plus a status word is not proof that it matches the current
    # title/video/generation.  Missing hashes must fail closed.
    assert cover_repair_needed("2026-07-06", rec)


def test_cover_repair_bounded_and_skips_undelivered(tmp_path, monkeypatch):
    rec = _delivered_talk_pick(tmp_path, monkeypatch, with_cover=False)
    rec["cover_repair_attempts"] = COVER_REPAIR_MAX_ATTEMPTS
    assert cover_repair_needed("2026-07-06", rec)  # integrity remains loud
    assert not runner._cover_repair_eligible(rec)  # only paid retry is capped
    # title_failed/failed picks were never delivered → nothing to repair
    assert not cover_repair_needed("2026-07-06", {"candidate_id": "auto_9", "status": "failed", "title": "x"})
    # a pick without a title must never get a cover (title is part of the product)
    ok_no_title = _delivered_talk_pick(tmp_path / "b", monkeypatch, with_cover=False)
    ok_no_title["title"] = None
    assert not cover_repair_needed("2026-07-06", ok_no_title)


def _write_srt(path, cues):
    """cues: list of (start_ms, end_ms, text) → an SRT file."""
    def ts(ms):
        h, ms = divmod(ms, 3600_000)
        m, ms = divmod(ms, 60_000)
        s, ms = divmod(ms, 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
    blocks = [f"{i+1}\n{ts(s)} --> {ts(e)}\n{t}" for i, (s, e, t) in enumerate(cues)]
    path.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")


def test_song_core_span_trims_leading_talk_and_outro(tmp_path):
    """Anchor-bleed guard (Ivan 2026-07-06 《屑屑》): the recall song-anchor began
    ~50s inside the preceding talk.  _song_core_span uses the music-intro/outro
    speech gaps to trim to the sung core — and must NOT split at a mid-song
    instrumental interlude."""
    srt = tmp_path / "seg.bcut.srt"
    cues = []
    cues += [(t, t + 2000, "闲聊") for t in range(0, 30_000, 3000)]        # 0-30s talk (dense)
    # 30-50s music intro (GAP, no cues)
    cues += [(t, t + 2500, "歌词") for t in range(50_000, 138_000, 4000)]  # 50-138s verse
    # 138-158s instrumental interlude (GAP, mid-song — must be ignored)
    cues += [(t, t + 2500, "歌词") for t in range(158_000, 230_000, 4000)] # 158-230s
    # 230-250s outro gap
    cues += [(t, t + 2000, "谢谢大家") for t in range(250_000, 260_000, 3000)]
    _write_srt(srt, cues)

    lo, hi = runner._song_core_span(srt, 0, 260_000)
    assert 46_000 <= lo <= 54_000, f"lead not trimmed to the song start: {lo}"
    # tail trims to the song end at the LARGE outro gap (230-250s), NOT the equal
    # mid-song interlude at 138-158s (which is outside the last 12% tail zone and
    # must never move the end — clipping mid-song → SONG_PARTIAL).
    assert 226_000 <= hi <= 236_000, f"tail should trim to the outro gap, got {hi}"
    # a clean anchor with no big edge gaps is returned unchanged
    tight = tmp_path / "tight.bcut.srt"
    _write_srt(tight, [(t, t + 2500, "歌词") for t in range(0, 200_000, 4000)])
    assert runner._song_core_span(tight, 0, 200_000) == (0, 200_000)
    # missing file / too-short span → unchanged (no false trim)
    assert runner._song_core_span(tmp_path / "nope.srt", 0, 260_000) == (0, 260_000)
    assert runner._song_core_span(srt, 0, 60_000) == (0, 60_000)


def test_song_work_item_normalizes_persisted_attempt_row(tmp_path, monkeypatch):
    from src.autoslice.song_lane import normalize_song_work_item

    date = "2026-07-16"
    segment = tmp_path / date / "recording.mp4"
    segment.parent.mkdir()
    segment.write_bytes(b"recording")
    monkeypatch.setattr(runner, "REC_ROOT", tmp_path)
    monkeypatch.setattr(runner, "ffprobe_ms", lambda path: 491_270 if path == segment else 0)

    normalized = normalize_song_work_item(
        date,
        {
            "candidate_id": "song_200010_41",
            "segment": segment.name,
            "anchor_start_ms": 41_860,
            "anchor_end_ms": 131_270,
            "discovery_lane": "semantic_recall",
        },
    )

    assert normalized["cid"] == "song_200010_41"
    assert normalized["segment_path"] == str(segment)
    assert normalized["seg_dur_ms"] == 491_270
    assert normalized["lane"] == "semantic_recall"


def test_extracted_song_lane_helpers_preserve_runner_patch_surface(tmp_path, monkeypatch):
    """Moving helpers out of the runner must not bypass its public patch seam."""

    class PatchedHelperReached(RuntimeError):
        pass

    def patched_srt_spans(*_args, **_kwargs):
        raise PatchedHelperReached

    monkeypatch.setattr(runner, "_srt_cue_spans", patched_srt_spans)
    with pytest.raises(PatchedHelperReached):
        runner._song_core_span(tmp_path / "unused.srt", 0, 200_000)

    monkeypatch.setattr(runner, "scheduled_song_retry_epoch", lambda _state: 11)
    monkeypatch.setattr(runner, "scheduled_talk_retry_epoch", lambda _state: 22)
    assert runner.scheduled_retry_epoch({}) == 11

    base = tmp_path / "autoslice"
    cid = "song_patch_surface"
    date = "2026-07-15"
    anchor_start, anchor_end = 50_000, 100_000
    window_path = runner.song_window_media_path(
        base / "out" / date / cid,
        cid,
        "",
        anchor_start - runner.SONG_WINDOW_PRE_MS,
        anchor_end + runner.SONG_WINDOW_POST_MS,
    )
    window_path.parent.mkdir(parents=True)
    window_path.write_bytes(b"already materialized")
    monkeypatch.setattr(runner, "BASE", base)
    monkeypatch.setattr(runner, "slice_srt", lambda *_args, **_kwargs: 1)

    def patched_selector_dir(*_args, **_kwargs):
        raise PatchedHelperReached

    monkeypatch.setattr(runner, "fresh_song_selector_dir", patched_selector_dir)
    with pytest.raises(PatchedHelperReached):
        runner.produce_song(
            date,
            {
                "cid": cid,
                "segment_path": str(tmp_path / "unused.mp4"),
                "seg_dur_ms": 200_000,
                "anchor_start_ms": anchor_start,
                "anchor_end_ms": anchor_end,
                "danmaku": 0,
                "hook": "",
                "preview": "",
            },
        )


def test_cover_repair_budget_resets_per_pipeline_generation_but_keeps_lifetime_cap():
    rec = {
        "cover_repair_generation": "sha256:old",
        "cover_repair_attempts": 2,
        "cover_repair_lifetime_attempts": 2,
    }
    assert runner._refresh_cover_repair_budget(rec, "sha256:new")
    assert rec["cover_repair_attempts"] == 0
    assert rec["cover_repair_lifetime_attempts"] == 2
    assert rec["cover_repair_attempt_history"] == [
        {"pipeline_fingerprint": "sha256:old", "attempts": 2}
    ]
    assert not runner._refresh_cover_repair_budget(rec, "sha256:new")


def test_cover_ref_prefers_clean_producer_frame(tmp_path, monkeypatch):
    """The delivered mp4 has burned subtitles — the identity reference must be
    the producer's clean cover-ref frame whenever it survives on disk."""
    monkeypatch.setattr(runner, "BASE", tmp_path)
    assert runner.cover_ref_for("2026-07-06", "auto_1") is None
    refs = tmp_path / "out" / "2026-07-06" / "auto_1" / "replacement_recuts" / "cover_refs"
    refs.mkdir(parents=True)
    (refs / "auto_1.cover-ref.png").write_bytes(b"png")
    assert runner.cover_ref_for("2026-07-06", "auto_1").name == "auto_1.cover-ref.png"


def _cover_binding_fixture(tmp_path, monkeypatch, *, song=False):
    monkeypatch.setattr(runner, "BASE", tmp_path / "autoslice")
    monkeypatch.setattr(runner, "REPO_ROOT", tmp_path / "repo")
    date = "2026-07-10"
    cid = "song_outer" if song else "auto_1"
    source_cid = "seededsong_100_200" if song else cid
    title = "【李豆沙】标题"
    delivery = runner.REPO_ROOT / "lidousha" / date
    delivery.mkdir(parents=True)
    mp4 = delivery / "钩子.mp4"
    cover = delivery / "钩子.cover.png"
    mp4.write_bytes(b"video")
    cover.write_bytes(b"stale-cover")
    generation_root = runner.BASE / "out" / date / cid / "cover_repair" / "generations" / "attempt-1"
    generation_root.mkdir(parents=True)
    generated_cover = generation_root / "final.cover.png"
    generated_cover.write_bytes(b"new-cover")
    evidence = generation_root / "evidence"
    evidence.mkdir()
    ai_bg = evidence / "ai.png"
    reference = evidence / "ref.png"
    request = evidence / "request.json"
    response = evidence / "response.json"
    for path, value in (
        (ai_bg, b"ai"),
        (reference, b"ref"),
        (request, b"request"),
        (response, b"response"),
    ):
        path.write_bytes(value)

    def digest(path):
        return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()

    generation_path = generated_cover.with_suffix(".cover_generation.json")
    generation = {
        "workflow": "regenerate_lidousha_cover",
        "status": "AI_COVER_READY",
        "method": "images.edit",
        "model": "gpt-image-1.5",
        "fallback_used": False,
        "model_fallback_used": True,
        "attempted_models": ["gpt-image-2", "gpt-image-1.5"],
        "candidate_id": cid,
        "title": title,
        "final_cover": str(generated_cover),
        "final_cover_sha256": digest(generated_cover),
        "ai_background": str(ai_bg),
        "ai_background_sha256": digest(ai_bg),
        "reference_image": str(reference),
        "reference_sha256": digest(reference),
        "request_path": str(request),
        "request_sha256": digest(request),
        "response_path": str(response),
        "response_sha256": digest(response),
    }
    generation_path.write_text(json.dumps(generation), encoding="utf-8")
    artifact_root = runner.BASE / "out" / date / cid
    if song:
        artifact_root = artifact_root / "song_selector_full" / "attempt-1"
    artifact_root = artifact_root / "replacement_recuts"
    artifact_root.mkdir(parents=True)
    publish_path = artifact_root / f"{source_cid}.recut.publish.json"
    video_sha = digest(mp4)
    record_payload = {
        **(
            {"delivery_candidate_id": cid, "source_candidate_id": source_cid}
            if song
            else {}
        ),
        "artifact_hashes": {"burned_video_sha256": video_sha},
        "publish_staging": {
            "title": title,
            "cover_status": "BLOCKED_AI_COVER_REQUIRED",
            "reason_codes": ["CPA_AI_COVER_REQUIRED"],
            "publish_json_path": str(publish_path),
            "upload_enabled": False,
        },
    }
    delivery_record = mp4.with_suffix(".record.json")
    delivery_record.write_text(json.dumps(record_payload), encoding="utf-8")
    source_record = artifact_root / f"{source_cid}.record.json"
    source_record.write_text(json.dumps(record_payload), encoding="utf-8")
    publish_path.write_text(
        json.dumps(
            {
                "schema_version": "shadow-publish-draft.v1",
                "candidate_id": source_cid,
                "title": title,
                "artifact_hashes": {"burned_video_sha256": video_sha},
                "cover_status": "BLOCKED_AI_COVER_REQUIRED",
                "reason_codes": ["CPA_IMAGE_EDIT_HTTP_ERROR"],
                "upload_enabled": False,
            }
        ),
        encoding="utf-8",
    )
    rec = {
        "candidate_id": cid,
        "title": title,
        "status": "review_ready",
        "cover_status": "BLOCKED_AI_COVER_REQUIRED",
        "summary": {},
    }
    delivery_manifest = None
    if song:
        delivery_manifest = mp4.with_suffix(".delivery.manifest.json")
        manifest_payload = {
            "schema_version": runner.VERIFIED_SONG_DELIVERY_SCHEMA_VERSION,
            "status": "DELIVERED_NO_UPLOAD",
            "candidate_id": cid,
            "upload_enabled": False,
            "artifacts": {
                "video": {"path": str(mp4), "sha256": video_sha},
                "active_record": {
                    "path": str(delivery_record),
                    "sha256": digest(delivery_record),
                    "source_path": str(source_record),
                    "source_sha256": digest(source_record),
                },
            },
            "absent_artifacts": {"cover": {"path": str(cover), "status": "ABSENT"}},
        }
        delivery_manifest.write_text(json.dumps(manifest_payload), encoding="utf-8")
        rec.update(
            {
                "delivered": str(mp4),
                "delivered_sha256": video_sha,
                "video_sha256": video_sha,
                "delivered_sidecars": {"active_record": str(delivery_record)},
                "delivered_sidecar_hashes": {
                    "active_record": digest(delivery_record)
                },
                "delivery_manifest_path": str(delivery_manifest),
                "delivery_manifest_sha256": digest(delivery_manifest),
                "delivery_upload_enabled": False,
            }
        )
    return {
        "date": date,
        "cid": cid,
        "source_cid": source_cid,
        "title": title,
        "mp4": mp4,
        "cover": cover,
        "generated_cover": generated_cover,
        "generation_path": generation_path,
        "delivery_record": delivery_record,
        "source_record": source_record,
        "publish_path": publish_path,
        "delivery_manifest": delivery_manifest,
        "rec": rec,
        "digest": digest,
    }


def test_bind_repaired_cover_updates_only_active_state_publish_and_records(tmp_path, monkeypatch):
    fx = _cover_binding_fixture(tmp_path, monkeypatch)
    historical = runner.BASE / "out" / fx["date"] / fx["cid"] / "quarantine" / "old.publish.json"
    historical.parent.mkdir(parents=True)
    historical.write_bytes(b'{"historical":true}\n')
    historical_before = historical.read_bytes()

    runner._bind_repaired_cover(
        fx["date"], fx["rec"], fx["mp4"], fx["cover"], fx["generated_cover"]
    )

    rec = fx["rec"]
    assert rec["cover_status"] == "REPAIRED_AI_COVER"
    assert fx["cover"].read_bytes() == b"new-cover"
    assert runner._matches_sha256(fx["cover"], rec["cover_sha256"])
    assert runner._matches_sha256(
        Path(rec["cover_binding_path"]), rec["cover_binding_sha256"]
    )
    assert not cover_repair_needed(fx["date"], {**rec, "delivered": str(fx["mp4"])})
    record = json.loads(fx["delivery_record"].read_text(encoding="utf-8"))
    source = json.loads(fx["source_record"].read_text(encoding="utf-8"))
    publish = json.loads(fx["publish_path"].read_text(encoding="utf-8"))
    assert record["artifact_hashes"]["cover_sha256"] == rec["cover_sha256"]
    assert record["publish_staging"]["cover_status"] == "AI_COVER_READY"
    assert source["artifact_hashes"]["cover_sha256"] == rec["cover_sha256"]
    assert publish["artifact_hashes"]["cover_sha256"] == rec["cover_sha256"]
    assert publish["cover_status"] == "AI_COVER_READY"
    assert publish["reason_codes"] == []
    assert publish["upload_enabled"] is False
    assert historical.read_bytes() == historical_before


def test_bind_repaired_cover_carries_active_story_and_route_authority(
    tmp_path, monkeypatch
):
    from src.autoslice.cover_route_evidence import (
        validate_cover_route_decision,
    )

    fx = _cover_binding_fixture(tmp_path, monkeypatch)
    story_contract = {
        "schema_version": "lidousha-story-contract.v1",
        "relation_state": "UNKNOWN",
        "participants": [],
        "cover_reference_authority": None,
    }
    for path in (
        fx["delivery_record"],
        fx["source_record"],
        fx["publish_path"],
    ):
        document = json.loads(path.read_text(encoding="utf-8"))
        document["story_contract"] = story_contract
        path.write_text(json.dumps(document), encoding="utf-8")

    runner._bind_repaired_cover(
        fx["date"], fx["rec"], fx["mp4"], fx["cover"], fx["generated_cover"]
    )

    generation = fx["rec"]["cover_generation"]
    assert generation["story_contract"] == story_contract
    assert generation["route_decision"]["selected_treatment"] == "cpa_redraw"
    assert generation["route_decision"]["actual_treatment"] == "cpa_redraw"
    assert generation["route_decision"]["execution_status"] == "READY"
    assert validate_cover_route_decision(generation)


def test_repaired_cover_binding_detects_title_media_binding_and_hash_tampering(tmp_path, monkeypatch):
    fx = _cover_binding_fixture(tmp_path, monkeypatch)
    runner._bind_repaired_cover(
        fx["date"], fx["rec"], fx["mp4"], fx["cover"], fx["generated_cover"]
    )
    base = {**fx["rec"], "delivered": str(fx["mp4"])}
    assert not cover_repair_needed(fx["date"], base)

    wrong_title = {**base, "title": "【李豆沙】另一个标题"}
    assert cover_repair_needed(fx["date"], wrong_title)
    wrong_title["cover_repair_attempts"] = COVER_REPAIR_MAX_ATTEMPTS
    assert cover_repair_needed(fx["date"], wrong_title)
    assert not runner._cover_repair_eligible(wrong_title)

    fx["mp4"].write_bytes(b"different-video")
    assert cover_repair_needed(fx["date"], base)
    fx["mp4"].write_bytes(b"video")

    missing_hash = dict(base)
    missing_hash.pop("cover_sha256")
    assert cover_repair_needed(fx["date"], missing_hash)

    wrong_cover_path = {**base, "cover_path": str(fx["cover"].with_name("wrong.png"))}
    assert cover_repair_needed(fx["date"], wrong_cover_path)

    wrong_generation = {**base, "cover_generation": {"status": "tampered"}}
    assert cover_repair_needed(fx["date"], wrong_generation)

    wrong_summary = json.loads(json.dumps(base))
    wrong_summary["summary"]["cover_status"] = "tampered"
    assert cover_repair_needed(fx["date"], wrong_summary)

    binding_path = Path(base["cover_binding_path"])
    binding = json.loads(binding_path.read_text(encoding="utf-8"))
    binding["title"] = "tampered-but-rehashed"
    binding_path.write_text(json.dumps(binding, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tampered = {**base, "cover_binding_sha256": fx["digest"](binding_path)}
    assert cover_repair_needed(fx["date"], tampered)


def test_repaired_cover_binding_detects_active_publish_drift(tmp_path, monkeypatch):
    fx = _cover_binding_fixture(tmp_path, monkeypatch)
    runner._bind_repaired_cover(
        fx["date"], fx["rec"], fx["mp4"], fx["cover"], fx["generated_cover"]
    )
    base = {**fx["rec"], "delivered": str(fx["mp4"])}
    assert not cover_repair_needed(fx["date"], base)
    publish = json.loads(fx["publish_path"].read_text(encoding="utf-8"))
    publish["cover_status"] = "BLOCKED_AI_COVER_REQUIRED"
    fx["publish_path"].write_text(json.dumps(publish), encoding="utf-8")
    exhausted = {**base, "cover_repair_attempts": COVER_REPAIR_MAX_ATTEMPTS}
    assert cover_repair_needed(fx["date"], exhausted)
    assert not runner._cover_repair_eligible(exhausted)


def test_initial_producer_cover_accepts_hash_bound_source_to_delivery_copy(tmp_path, monkeypatch):
    fx = _cover_binding_fixture(tmp_path, monkeypatch)
    source_cover = fx["generated_cover"]
    fx["cover"].write_bytes(source_cover.read_bytes())
    generation = json.loads(fx["generation_path"].read_text(encoding="utf-8"))
    generation.pop("status")  # real _stage_lidousha_ai_cover shape
    generation["final_cover_sha256"] = fx["digest"](source_cover)
    expected_cover = fx["digest"](fx["cover"])
    video_sha = fx["digest"](fx["mp4"])

    for record_path in (fx["delivery_record"], fx["source_record"]):
        record = json.loads(record_path.read_text(encoding="utf-8"))
        record["artifact_hashes"]["cover_sha256"] = expected_cover
        record["publish_staging"].update(
            {
                "cover_status": "AI_COVER_READY",
                "cover_path": str(source_cover),
                "cover_generation": generation,
            }
        )
        record_path.write_text(json.dumps(record), encoding="utf-8")
    publish = json.loads(fx["publish_path"].read_text(encoding="utf-8"))
    publish["artifact_hashes"]["cover_sha256"] = expected_cover
    publish.update(
        {
            "cover_status": "AI_COVER_READY",
            "cover_path": str(source_cover),
            "cover_generation": generation,
        }
    )
    fx["publish_path"].write_text(json.dumps(publish), encoding="utf-8")
    rec = {
        **fx["rec"],
        "cover_status": "AI_COVER_READY",
        "cover_path": str(source_cover),
        "cover_sha256": expected_cover,
        "video_sha256": video_sha,
        "cover_generation": generation,
        "delivered": str(fx["mp4"]),
    }

    assert not cover_repair_needed(fx["date"], rec)


def _initial_screenshot_cover_proof_fixture(
    tmp_path,
    monkeypatch,
    *,
    screenshot_frame,
    reference_selection,
):
    fx = _cover_binding_fixture(tmp_path, monkeypatch)
    source_cover = fx["generated_cover"]
    fx["cover"].write_bytes(source_cover.read_bytes())
    reference = source_cover.with_name("screenshot-reference.png")
    reference.write_bytes(b"source-frame")
    expected_cover = fx["digest"](fx["cover"])
    video_sha = fx["digest"](fx["mp4"])
    generation = {
        "title": fx["title"],
        "method": "screenshot_direct",
        "model": "none",
        "image_gen_model": "none",
        "cover_origin": "SOURCE_SCREENSHOT",
        "fallback_used": False,
        "reference_image": str(reference),
        "reference_sha256": fx["digest"](reference),
        "rendered_lines": ["双人截图"],
        "final_cover": str(source_cover),
        "final_cover_sha256": expected_cover,
    }
    if screenshot_frame is not None:
        generation["screenshot_frame"] = screenshot_frame
    if reference_selection is not None:
        generation["reference_selection"] = reference_selection
    generation["route_decision"] = build_cover_route_decision(
        selected_treatment="screenshot_direct",
        selected_rationale="hash-bound real stream frame has both participants",
        story_contract=None,
        reference_authority=None,
        decision_inputs={"cover_mode": "auto"},
    )
    record_cover_route_execution(
        generation,
        actual_treatment="screenshot_direct",
        execution_status="READY",
        image_generation_attempted=False,
        image_generation_used=False,
    )
    for record_path in (fx["delivery_record"], fx["source_record"]):
        record = json.loads(record_path.read_text(encoding="utf-8"))
        record["artifact_hashes"]["cover_sha256"] = expected_cover
        record["publish_staging"].update(
            {
                "cover_status": "AI_COVER_READY",
                "cover_path": str(source_cover),
                "cover_generation": generation,
            }
        )
        record_path.write_text(json.dumps(record), encoding="utf-8")
    publish = json.loads(fx["publish_path"].read_text(encoding="utf-8"))
    publish["artifact_hashes"]["cover_sha256"] = expected_cover
    publish.update(
        {
            "cover_status": "AI_COVER_READY",
            "cover_path": str(source_cover),
            "cover_generation": generation,
        }
    )
    fx["publish_path"].write_text(json.dumps(publish), encoding="utf-8")
    rec = {
        **fx["rec"],
        "cover_status": "AI_COVER_READY",
        "cover_path": str(source_cover),
        "cover_sha256": expected_cover,
        "video_sha256": video_sha,
        "cover_generation": generation,
        "delivered": str(fx["mp4"]),
    }
    return fx, rec, reference


def test_initial_screenshot_cover_is_valid_and_never_requeued_as_ai_repair(
    tmp_path, monkeypatch
):
    fx, rec, reference = _initial_screenshot_cover_proof_fixture(
        tmp_path,
        monkeypatch,
        screenshot_frame={"frame_ms": 21_500},
        reference_selection={"best_ms": 21_500},
    )
    assert not cover_repair_needed(fx["date"], rec)
    reference.write_bytes(b"tampered")
    assert cover_repair_needed(fx["date"], rec)


@pytest.mark.parametrize(
    ("screenshot_frame", "reference_selection"),
    [
        (None, {"best_ms": 21_500}),
        ({}, {"best_ms": 21_500}),
        ({"frame_ms": True}, {"best_ms": 21_500}),
        ({"frame_ms": 21_500}, None),
        ({"frame_ms": 21_500}, {}),
        ({"frame_ms": 21_500}, {"best_ms": True}),
        ({"frame_ms": -1}, {"best_ms": -1}),
        ({"frame_ms": 21_500}, {"best_ms": 21_501}),
    ],
    ids=[
        "missing-screenshot-proof",
        "missing-screenshot-frame-ms",
        "boolean-screenshot-frame-ms",
        "missing-reference-selection",
        "missing-reference-best-ms",
        "boolean-reference-best-ms",
        "negative-frame-selection",
        "frame-selection-mismatch",
    ],
)
def test_initial_screenshot_cover_frame_selection_binding_fails_closed(
    tmp_path,
    monkeypatch,
    screenshot_frame,
    reference_selection,
):
    fx, rec, _reference = _initial_screenshot_cover_proof_fixture(
        tmp_path,
        monkeypatch,
        screenshot_frame=screenshot_frame,
        reference_selection=reference_selection,
    )

    assert cover_repair_needed(fx["date"], rec)


def test_invalid_screenshot_cover_fails_closed_before_generic_ai_repair(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(runner, "BASE", tmp_path / "autoslice")
    monkeypatch.setattr(
        runner, "pipeline_fingerprint", lambda: "sha256:" + "a" * 64
    )
    mp4 = tmp_path / "clip.mp4"
    cover = tmp_path / "clip.cover.png"
    mp4.write_bytes(b"video")
    cover.write_bytes(b"cover")
    rec = {
        "candidate_id": "auto_screenshot",
        "status": "review_ready",
        "title": "【李豆沙】截图路线",
        "cover_status": "AI_COVER_READY",
        "cover_generation": {
            "method": "screenshot_direct",
            "route_decision": {
                "schema_version": "lidousha-cover-route-decision.v1",
                "selected_treatment": "screenshot_direct",
                "reason": "real stream frame",
            },
        },
    }
    state = {"picks": [rec], "songs": []}
    monkeypatch.setattr(
        runner, "delivered_paths", lambda _date, _rec: (mp4, cover)
    )
    monkeypatch.setattr(runner, "write_state", lambda _date, _state: None)

    def forbidden_run(*_args, **_kwargs):
        raise AssertionError("screenshot drift must never call an AI repair tool")

    monkeypatch.setattr(runner.subprocess, "run", forbidden_run)
    runner.repair_covers("2026-07-22", state)

    assert rec.get("cover_repair_attempts", 0) == 0
    assert rec["cover_status"] == "BLOCKED_SCREENSHOT_COVER_REPAIR_REQUIRED"
    assert rec["cover_integrity_status"] == (
        "INVALID_SCREENSHOT_ROUTE_REPAIR_REQUIRED"
    )


def test_pending_screenshot_cover_queues_one_route_preserving_producer_rerun(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(runner, "BASE", tmp_path / "autoslice")
    monkeypatch.setattr(
        runner, "pipeline_fingerprint", lambda: "sha256:" + "a" * 64
    )
    mp4 = tmp_path / "clip.mp4"
    cover = tmp_path / "clip.cover.png"
    mp4.write_bytes(b"video")
    cover.write_bytes(b"cover")
    rec = {
        "candidate_id": "auto_pending_screenshot",
        "status": runner.TALK_COVER_PENDING_STATUS,
        "title": "【李豆沙】待补截图路线",
        "cover_status": "BLOCKED_AI_COVER_REQUIRED",
        # Historical retry counts are not this policy's budget.  A new
        # pipeline fingerprint must receive one route-preserving attempt.
        "talk_transient_retry_count": 7,
        "cover_generation": {
            "route_decision": {
                "schema_version": "lidousha-cover-route-decision.v2",
                "selected_treatment": "screenshot_polish",
            },
        },
    }
    state = {"picks": [rec], "songs": []}
    monkeypatch.setattr(
        runner, "delivered_paths", lambda _date, _rec: (mp4, cover)
    )
    monkeypatch.setattr(runner, "write_state", lambda _date, _state: None)

    def forbidden_run(*_args, **_kwargs):
        raise AssertionError("route-preserving retry must use the normal producer")

    monkeypatch.setattr(runner.subprocess, "run", forbidden_run)
    runner.repair_covers("2026-07-26", state)

    assert rec["status"] == "failed"
    assert rec["failure_kind"] == "cover_route_regeneration"
    assert rec["failure_recoverable"] is True
    assert rec["cover_route_regeneration_fingerprint"] == (
        "sha256:" + "a" * 64
    )
    assert rec["cover_route_regeneration_attempts"] == 1
    assert rec["cover_status"] == "SCREENSHOT_ROUTE_REGENERATION_QUEUED"
    assert (
        rec["cover_integrity_status"]
        == "INVALID_SCREENSHOT_ROUTE_REGENERATION_QUEUED"
    )

    # The producer returned another blocked screenshot package under the same
    # code fingerprint: keep it loud, but never spin the same build forever.
    rec["status"] = runner.TALK_COVER_PENDING_STATUS
    rec["cover_status"] = "BLOCKED_AI_COVER_REQUIRED"
    runner.repair_covers("2026-07-26", state)
    assert rec["status"] == runner.TALK_COVER_PENDING_STATUS
    assert rec["cover_status"] == "BLOCKED_SCREENSHOT_COVER_REPAIR_REQUIRED"
    assert rec["cover_route_regeneration_attempts"] == 1

    # A later code deployment gets one new bounded attempt, regardless of the
    # lifetime talk transient counter.
    monkeypatch.setattr(
        runner, "pipeline_fingerprint", lambda: "sha256:" + "b" * 64
    )
    runner.repair_covers("2026-07-26", state)
    assert rec["status"] == "failed"
    assert rec["cover_route_regeneration_fingerprint"] == (
        "sha256:" + "b" * 64
    )
    assert rec["cover_route_regeneration_attempts"] == 2


def test_list_dates_keeps_aged_out_source_incomplete_date(
    tmp_path, monkeypatch
):
    rec_root = tmp_path / "recordings"
    state_root = tmp_path / "autoslice" / "state"
    state_root.mkdir(parents=True)
    for date in (
        "2026-07-22",
        "2026-07-24",
        "2026-07-25",
        "2026-07-26",
    ):
        (rec_root / date).mkdir(parents=True)
    (state_root / "2026-07-22.json").write_text(
        json.dumps({"status": "source_incomplete"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(runner, "REC_ROOT", rec_root)
    monkeypatch.setattr(runner, "BASE", tmp_path / "autoslice")

    assert runner.list_dates() == [
        "2026-07-22",
        "2026-07-24",
        "2026-07-25",
        "2026-07-26",
    ]


def test_list_dates_keeps_aged_out_recovery_until_pending_work_drains(
    tmp_path, monkeypatch
):
    rec_root = tmp_path / "recordings"
    state_root = tmp_path / "autoslice" / "state"
    state_root.mkdir(parents=True)
    for date in (
        "2026-07-22",
        "2026-07-24",
        "2026-07-25",
        "2026-07-26",
    ):
        (rec_root / date).mkdir(parents=True)
    historical = state_root / "2026-07-22.json"
    historical.write_text(
        json.dumps(
            {
                "status": "sealing",
                "source_recoveries": [{"status": "RECOVERED"}],
                "pending_talk": [{"start_ms": 1_000, "end_ms": 61_000}],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(runner, "REC_ROOT", rec_root)
    monkeypatch.setattr(runner, "BASE", tmp_path / "autoslice")

    assert runner.list_dates()[0] == "2026-07-22"

    historical.write_text(
        json.dumps(
            {
                "status": "review_ready_with_failures",
                "source_recoveries": [{"status": "RECOVERED"}],
                "pending_talk": [],
                "pending_song": [],
                "picks": [
                    {
                        "candidate_id": "recovered-failure",
                        "status": "failed",
                        "failure_recoverable": False,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    assert runner.list_dates()[0] == "2026-07-22"

    historical.write_text(
        json.dumps(
            {
                "status": "review_ready",
                "source_recoveries": [{"status": "RECOVERED"}],
                "pending_talk": [],
                "pending_song": [],
            }
        ),
        encoding="utf-8",
    )
    assert runner.list_dates() == [
        "2026-07-24",
        "2026-07-25",
        "2026-07-26",
    ]


def test_cover_binding_prevalidation_leaves_everything_unchanged_on_bad_active_record(tmp_path, monkeypatch):
    fx = _cover_binding_fixture(tmp_path, monkeypatch)
    fx["source_record"].write_text("not-json", encoding="utf-8")
    watched = [fx["cover"], fx["delivery_record"], fx["source_record"], fx["publish_path"]]
    before = {path: path.read_bytes() for path in watched}
    state_before = json.loads(json.dumps(fx["rec"]))

    with pytest.raises(ValueError, match="active source record"):
        runner._bind_repaired_cover(
            fx["date"], fx["rec"], fx["mp4"], fx["cover"], fx["generated_cover"]
        )

    assert {path: path.read_bytes() for path in watched} == before
    assert fx["rec"] == state_before
    assert not fx["generated_cover"].with_suffix(".cover-binding.json").exists()


def test_cover_binding_rolls_back_target_and_documents_on_mid_commit_failure(tmp_path, monkeypatch):
    fx = _cover_binding_fixture(tmp_path, monkeypatch)
    watched = [fx["cover"], fx["delivery_record"], fx["source_record"], fx["publish_path"]]
    before = {path: path.read_bytes() for path in watched}
    state_before = json.loads(json.dumps(fx["rec"]))
    real_write = runner._atomic_write_bytes_file
    failed = False

    def fail_second_document(path, payload):
        nonlocal failed
        if Path(path) == fx["source_record"] and not failed:
            failed = True
            raise OSError("injected mid-commit failure")
        real_write(path, payload)

    monkeypatch.setattr(runner, "_atomic_write_bytes_file", fail_second_document)
    with pytest.raises(OSError, match="injected mid-commit failure"):
        runner._bind_repaired_cover(
            fx["date"], fx["rec"], fx["mp4"], fx["cover"], fx["generated_cover"]
        )

    assert {path: path.read_bytes() for path in watched} == before
    assert fx["rec"] == state_before
    assert not fx["generated_cover"].with_suffix(".cover-binding.json").exists()
    journal = json.loads(
        (fx["generated_cover"].parent / "cover-transaction.json").read_text(
            encoding="utf-8"
        )
    )
    assert journal["status"] == "ROLLED_BACK"


def test_song_cover_binding_supports_distinct_outer_and_selector_candidate_ids(tmp_path, monkeypatch):
    fx = _cover_binding_fixture(tmp_path, monkeypatch, song=True)
    assert fx["cid"] != fx["source_cid"]
    old_active_record_hash = fx["rec"]["delivered_sidecar_hashes"]["active_record"]

    runner._bind_repaired_cover(
        fx["date"], fx["rec"], fx["mp4"], fx["cover"], fx["generated_cover"]
    )

    assert not cover_repair_needed(fx["date"], fx["rec"])
    manifest = json.loads(fx["delivery_manifest"].read_text(encoding="utf-8"))
    assert runner._matches_sha256(
        fx["delivery_manifest"], fx["rec"]["delivery_manifest_sha256"]
    )
    assert manifest["candidate_id"] == fx["cid"]
    assert manifest["artifacts"]["cover"]["path"] == str(fx["cover"])
    assert manifest["artifacts"]["cover"]["sha256"] == fx["rec"]["cover_sha256"]
    assert fx["rec"]["delivered_sidecars"]["active_record"] == str(
        fx["delivery_record"]
    )
    assert fx["rec"]["delivered_sidecar_hashes"]["active_record"] == manifest[
        "artifacts"
    ]["active_record"]["sha256"]
    assert (
        fx["rec"]["delivered_sidecar_hashes"]["active_record"]
        != old_active_record_hash
    )
    assert "cover" not in manifest["absent_artifacts"]
    publish = json.loads(fx["publish_path"].read_text(encoding="utf-8"))
    assert publish["candidate_id"] == fx["source_cid"]

    drifted_state = json.loads(json.dumps(fx["rec"]))
    drifted_state["delivered_sidecar_hashes"]["active_record"] = "sha256:" + "0" * 64
    assert cover_repair_needed(fx["date"], drifted_state)

    drifted_cover_path = json.loads(json.dumps(fx["rec"]))
    drifted_cover_path["delivered_sidecars"]["cover"] = str(
        fx["cover"].with_name("wrong.cover.png")
    )
    assert cover_repair_needed(fx["date"], drifted_cover_path)

    drifted_cover_hash = json.loads(json.dumps(fx["rec"]))
    drifted_cover_hash["delivered_sidecar_hashes"]["cover"] = "sha256:" + "0" * 64
    assert cover_repair_needed(fx["date"], drifted_cover_hash)

    drifted_top_level_cover_path = json.loads(json.dumps(fx["rec"]))
    drifted_top_level_cover_path["cover_path"] = str(
        fx["cover"].with_name("wrong-top-level.cover.png")
    )
    assert cover_repair_needed(fx["date"], drifted_top_level_cover_path)

    # Manifest drift is part of the public song package and must invalidate the
    # otherwise-valid binding.
    manifest["artifacts"]["cover"]["sha256"] = "sha256:" + "0" * 64
    fx["delivery_manifest"].write_text(json.dumps(manifest), encoding="utf-8")
    fx["rec"]["delivery_manifest_sha256"] = fx["digest"](fx["delivery_manifest"])
    assert cover_repair_needed(fx["date"], fx["rec"])


def test_song_active_record_materialization_uses_publish_compatible_filename(tmp_path, monkeypatch):
    fx = _cover_binding_fixture(tmp_path, monkeypatch, song=True)
    fx["source_record"].unlink()
    record_payload = json.loads(fx["delivery_record"].read_text(encoding="utf-8"))
    record_payload.pop("delivery_candidate_id", None)
    record_payload.pop("source_candidate_id", None)
    summary_record = {
        "candidate_id": fx["source_cid"],
        "materialized_recut": record_payload,
    }

    path, digest = runner._write_song_active_record(
        summary_record,
        delivery_candidate_id=fx["cid"],
        title=fx["title"],
        video_sha256=fx["digest"](fx["mp4"]),
        summary_authority_root=fx["publish_path"].parent,
    )

    assert path == fx["source_record"]
    assert runner._matches_sha256(path, digest)
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["delivery_candidate_id"] == fx["cid"]
    assert document["source_candidate_id"] == fx["source_cid"]


def test_song_active_record_rejects_publish_outside_bound_attempt(tmp_path, monkeypatch):
    fx = _cover_binding_fixture(tmp_path, monkeypatch, song=True)
    record_payload = json.loads(fx["delivery_record"].read_text(encoding="utf-8"))
    record_payload.pop("delivery_candidate_id", None)
    record_payload.pop("source_candidate_id", None)
    bound_attempt = tmp_path / "bound-attempt"
    bound_attempt.mkdir()

    with pytest.raises(runner.SongDeliveryError, match="escapes the bound summary attempt"):
        runner._write_song_active_record(
            {
                "candidate_id": fx["source_cid"],
                "materialized_recut": record_payload,
            },
            delivery_candidate_id=fx["cid"],
            title=fx["title"],
            video_sha256=fx["digest"](fx["mp4"]),
            summary_authority_root=bound_attempt,
        )


def _deferred_song_summary(tmp_path, *, reason_codes=None, decision_action="AUTO_UPLOAD"):
    root = tmp_path / "attempt" / "seededsong_ready" / "replacement_recuts"
    root.mkdir(parents=True)
    media = root / "seededsong_ready.recut.mp4"
    burned = root / "seededsong_ready.recut.burned-final.mp4"
    subtitle = root / "seededsong_ready.recut.srt"
    manifest = root / "seededsong_ready.recut.manifest.json"
    alignment = tmp_path / "song.lyrics-alignment-report.json"
    proof = tmp_path / "song.host-vocal-proof.json"
    payloads = {
        media: b"recut-video",
        burned: b"burned-video",
        subtitle: b"1\n00:00:00,000 --> 00:00:01,000\nlyric\n",
        manifest: b'{"status":"MATERIALIZED"}\n',
        alignment: b'{"status":"READY"}\n',
        proof: b'{"decision":"LIDOUSHA_VOCAL_PRESENT_ON_LYRIC_CHECKPOINTS"}\n',
    }
    for path, payload in payloads.items():
        path.write_bytes(payload)

    def digest(path):
        return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()

    reasons = list(reason_codes or ["SONG_FULL_BOUNDARY_READY"])
    gate_path = root.parent / "seededsong_ready.cover-release-gate.json"
    gate_path.write_text("{}\n", encoding="utf-8")
    summary = {
        "candidate_id": "seededsong_ready",
        "decision_action": decision_action,
        "reason_codes": reasons,
        "source_context_job": {"song_candidate": True},
        "materialized_recut": {
            "status": "MATERIALIZED",
            "candidate_id": "seededsong_ready",
            "media_path": str(media),
            "subtitle_path": str(subtitle),
            "manifest_path": str(manifest),
            "manifest_sha256": digest(manifest),
            "burned_preview": {
                "status": "BURNED",
                "path": str(burned),
                "burned_sha256": digest(burned),
            },
            "artifact_hashes": {
                "video_sha256": digest(media),
                "burned_video_sha256": digest(burned),
                "subtitle_sha256": digest(subtitle),
            },
            "cover_release_gate": {
                "schema_version": "slice-cover-release-gate.v1",
                "candidate_id": "seededsong_ready",
                "decision_action": decision_action,
                "reason_codes": reasons,
                "satisfied": False,
                "path": str(gate_path),
            },
            "publish_staging": {
                "status": "SKIPPED_RELEASE_GATE",
                "decision_action": decision_action,
                "reason_codes": reasons,
                "release_gate_path": str(gate_path),
                "upload_enabled": False,
            },
        },
    }
    return {
        "summary": summary,
        "root": root,
        "media": media,
        "burned": burned,
        "subtitle": subtitle,
        "manifest": manifest,
        "alignment": alignment,
        "proof": proof,
        "digest": digest,
    }


@pytest.mark.parametrize("decision_action", ["AUTO_UPLOAD", "AUTO_RECUT"])
def test_song_active_record_materializes_no_upload_deferred_cover_authority(
    tmp_path, decision_action
):
    fx = _deferred_song_summary(tmp_path, decision_action=decision_action)
    title = "【李豆沙】豆沙歌，《想和你迎着台风去看海》｜台风天唱甜甜的"

    record_path, record_sha = runner._write_song_active_record(
        fx["summary"],
        delivery_candidate_id="song_outer",
        title=title,
        video_sha256=fx["digest"](fx["burned"]),
        summary_authority_root=fx["root"].parent,
    )

    publish_path = fx["media"].with_suffix(".publish.json")
    assert record_path == fx["root"] / "seededsong_ready.record.json"
    assert runner._matches_sha256(record_path, record_sha)
    record = json.loads(record_path.read_text(encoding="utf-8"))
    publish = json.loads(publish_path.read_text(encoding="utf-8"))
    assert record["delivery_candidate_id"] == "song_outer"
    assert record["source_candidate_id"] == "seededsong_ready"
    assert record["publish_staging"]["status"] == "STAGED"
    assert record["publish_staging"]["title"] == title
    assert record["publish_staging"]["cover_status"] == "BLOCKED_AI_COVER_REQUIRED"
    assert record["publish_staging"]["cover_path"] is None
    assert record["publish_staging"]["upload_enabled"] is False
    assert publish["schema_version"] == "shadow-publish-draft.v1"
    assert publish["candidate_id"] == "seededsong_ready"
    assert publish["title"] == title
    assert publish["cover_status"] == "BLOCKED_AI_COVER_REQUIRED"
    assert publish["cover_path"] is None
    assert publish["upload_enabled"] is False
    assert publish["artifact_hashes"]["burned_video_sha256"] == fx["digest"](fx["burned"])
    assert publish["delivery_authority"]["release_gate_reason_codes"] == [
        "SONG_FULL_BOUNDARY_READY"
    ]

    # A packaging retry is idempotent, but conflicting authority is never
    # overwritten silently.
    second_path, second_sha = runner._write_song_active_record(
        fx["summary"],
        delivery_candidate_id="song_outer",
        title=title,
        video_sha256=fx["digest"](fx["burned"]),
        summary_authority_root=fx["root"].parent,
    )
    assert (second_path, second_sha) == (record_path, record_sha)
    publish["title"] = "tampered"
    publish_path.write_text(json.dumps(publish), encoding="utf-8")
    with pytest.raises(runner.SongDeliveryError, match="conflicting deferred"):
        runner._write_song_active_record(
            fx["summary"],
            delivery_candidate_id="song_outer",
            title=title,
            video_sha256=fx["digest"](fx["burned"]),
            summary_authority_root=fx["root"].parent,
        )


def test_song_active_record_does_not_bypass_nonpositive_release_gate(tmp_path):
    fx = _deferred_song_summary(tmp_path, reason_codes=["TERMINOLOGY_QA_FAILED"])
    with pytest.raises(runner.SongDeliveryError, match="outside the verified deferred-cover case"):
        runner._write_song_active_record(
            fx["summary"],
            delivery_candidate_id="song_outer",
            title="【李豆沙】豆沙歌，测试",
            video_sha256=fx["digest"](fx["burned"]),
            summary_authority_root=fx["root"].parent,
        )
    assert not fx["media"].with_suffix(".publish.json").exists()


def test_commit_verified_song_package_delivers_without_cover(tmp_path, monkeypatch):
    date = "2026-07-10"
    base = tmp_path / "autoslice"
    candidate_root = base / "out" / date / "song_outer"
    fx = _deferred_song_summary(candidate_root)
    repo = tmp_path / "repo"
    monkeypatch.setattr(runner, "BASE", base)
    monkeypatch.setattr(runner, "REPO_ROOT", repo)
    completion = {
        "ready": True,
        "reason_codes": [],
        "alignment_report_path": str(fx["alignment"]),
        "alignment_report_sha256": fx["digest"](fx["alignment"]),
        "host_vocal_proof_path": str(fx["proof"]),
        "host_vocal_proof_sha256": fx["digest"](fx["proof"]),
    }
    monkeypatch.setattr(runner, "song_completion_evidence", lambda _record: completion)
    monkeypatch.setattr(runner, "song_delivery_ok", lambda *_args, **_kwargs: True)
    title = "【李豆沙】豆沙歌，《想和你迎着台风去看海》｜台风天唱甜甜的"

    result = runner._commit_verified_song_package(
        date=date,
        delivery_candidate_id="song_outer",
        summary_record=fx["summary"],
        title=title,
        selector_rc=0,
        summary_authority_root=fx["root"].parent,
    )

    assert result["cover_status"] == "BLOCKED_AI_COVER_REQUIRED"
    assert result["delivery_upload_enabled"] is False
    manifest_path = Path(result["delivery_manifest_path"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "DELIVERED_NO_UPLOAD"
    assert manifest["upload_enabled"] is False
    assert set(manifest["artifacts"]) == {
        "video",
        "subtitle",
        "lyrics_alignment_report",
        "host_vocal_proof",
        "recut_manifest",
        "active_record",
    }
    assert manifest["absent_artifacts"]["cover"]["status"] == "ABSENT"
    assert not Path(manifest["absent_artifacts"]["cover"]["path"]).exists()


def test_song_cover_binding_rolls_back_manifest_and_records_on_commit_failure(tmp_path, monkeypatch):
    fx = _cover_binding_fixture(tmp_path, monkeypatch, song=True)
    watched = [
        fx["cover"],
        fx["delivery_record"],
        fx["source_record"],
        fx["publish_path"],
        fx["delivery_manifest"],
    ]
    before = {path: path.read_bytes() for path in watched}
    state_before = json.loads(json.dumps(fx["rec"]))
    real_write = runner._atomic_write_bytes_file
    failed = False

    def fail_manifest_commit(path, payload):
        nonlocal failed
        if Path(path) == fx["delivery_manifest"] and not failed:
            failed = True
            raise OSError("injected song manifest commit failure")
        real_write(path, payload)

    monkeypatch.setattr(runner, "_atomic_write_bytes_file", fail_manifest_commit)
    with pytest.raises(OSError, match="song manifest commit failure"):
        runner._bind_repaired_cover(
            fx["date"], fx["rec"], fx["mp4"], fx["cover"], fx["generated_cover"]
        )

    assert {path: path.read_bytes() for path in watched} == before
    assert fx["rec"] == state_before
    assert not fx["generated_cover"].with_suffix(".cover-binding.json").exists()


@pytest.mark.parametrize("song", [False, True])
def test_prepared_cover_transaction_rolls_forward_after_abrupt_partial_commit(
    tmp_path, monkeypatch, song
):
    fx = _cover_binding_fixture(tmp_path, monkeypatch, song=song)
    persistent_old_state = json.loads(json.dumps(fx["rec"]))
    runner._bind_repaired_cover(
        fx["date"], fx["rec"], fx["mp4"], fx["cover"], fx["generated_cover"]
    )
    journal_path = Path(fx["rec"]["cover_transaction_path"])
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    assert journal["status"] == "COMMITTED"

    # Reconstruct the exact on-disk crash window: restore the pre-transaction
    # bytes, leave only the first two public targets installed, and keep the
    # durable journal at PREPARED as SIGKILL/host loss would.
    for entry in journal["entries"]:
        target = Path(entry["target"])
        if entry["original_exists"]:
            runner._atomic_write_bytes_file(
                target, Path(entry["original_blob"]).read_bytes()
            )
        else:
            target.unlink(missing_ok=True)
    journal["status"] = "PREPARED"
    runner._atomic_write_json_file(journal_path, journal)
    for entry in journal["entries"][:2]:
        runner._atomic_write_bytes_file(
            Path(entry["target"]), Path(entry["intended_blob"]).read_bytes()
        )

    assert runner._roll_forward_prepared_cover_transactions(
        fx["date"], persistent_old_state, fx["mp4"], fx["cover"]
    )
    assert (
        json.loads(journal_path.read_text(encoding="utf-8"))["status"]
        == "COMMITTED"
    )
    assert runner._recover_committed_cover_binding(
        fx["date"], persistent_old_state, fx["mp4"], fx["cover"]
    )
    assert not cover_repair_needed(fx["date"], persistent_old_state)
    if song:
        manifest = json.loads(fx["delivery_manifest"].read_text(encoding="utf-8"))
        assert persistent_old_state["delivered_sidecar_hashes"][
            "active_record"
        ] == manifest["artifacts"]["active_record"]["sha256"]


def test_prepared_cover_transaction_rejects_redirect_to_historical_path(
    tmp_path, monkeypatch
):
    fx = _cover_binding_fixture(tmp_path, monkeypatch)
    runner._bind_repaired_cover(
        fx["date"], fx["rec"], fx["mp4"], fx["cover"], fx["generated_cover"]
    )
    journal_path = Path(fx["rec"]["cover_transaction_path"])
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    for entry in journal["entries"]:
        target = Path(entry["target"])
        if entry["original_exists"]:
            runner._atomic_write_bytes_file(
                target, Path(entry["original_blob"]).read_bytes()
            )
        else:
            target.unlink(missing_ok=True)

    historical = (
        runner.BASE
        / "out"
        / fx["date"]
        / fx["cid"]
        / "quarantine"
        / "historical.publish.json"
    )
    historical.parent.mkdir(parents=True)
    historical.write_bytes(b'{"historical":true}\n')
    historical_before = historical.read_bytes()
    publish_entry = next(
        entry
        for entry in journal["entries"]
        if Path(entry["target"]) == fx["publish_path"]
    )
    publish_entry["target"] = str(historical)
    journal["status"] = "PREPARED"
    runner._atomic_write_json_file(journal_path, journal)

    assert not runner._roll_forward_prepared_cover_transactions(
        fx["date"], fx["rec"], fx["mp4"], fx["cover"]
    )
    assert historical.read_bytes() == historical_before
    assert json.loads(journal_path.read_text(encoding="utf-8"))["status"] == "PREPARED"


@pytest.mark.parametrize("song", [False, True])
def test_committed_cover_binding_recovers_after_state_write_crash_without_regeneration(
    tmp_path, monkeypatch, song
):
    fx = _cover_binding_fixture(tmp_path, monkeypatch, song=song)
    persistent_old_state = json.loads(json.dumps(fx["rec"]))
    if not song:
        persistent_old_state.update(
            {
                "status": runner.TALK_COVER_PENDING_STATUS,
                "bundle_lifecycle": "PENDING_COVER",
                "bundle_compliance": "COVER_REQUIRED",
            }
        )
    runner._bind_repaired_cover(
        fx["date"], fx["rec"], fx["mp4"], fx["cover"], fx["generated_cover"]
    )
    # Simulate a process crash before the updated in-memory record reached the
    # atomic state JSON: filesystem transaction is new, loaded state is old.
    assert runner._recover_committed_cover_binding(
        fx["date"], persistent_old_state, fx["mp4"], fx["cover"]
    )
    assert persistent_old_state["cover_integrity_status"] == "VALID_BOUND_RECOVERED"
    assert not cover_repair_needed(fx["date"], persistent_old_state)
    if song:
        assert runner._matches_sha256(
            Path(persistent_old_state["delivery_manifest_path"]),
            persistent_old_state["delivery_manifest_sha256"],
        )
    else:
        assert persistent_old_state["status"] == "review_ready"
        assert persistent_old_state["bundle_lifecycle"] == "CURRENT"
        assert persistent_old_state["bundle_compliance"] == "COMPLIANT"


def test_cover_repair_needed_for_delivered_song(tmp_path, monkeypatch):
    """Ivan 2026-07-10：交付了的歌就该有封面——语义判定(BLOCK/reason codes)不再
    锁死补封面；没交付的(blocked)自然没有 delivered，永远不补。"""
    monkeypatch.setattr(runner, "REPO_ROOT", tmp_path)
    delivery = tmp_path / "lidousha" / "2026-07-06"
    delivery.mkdir(parents=True)
    (delivery / "歌切_嘉宾.mp4").write_bytes(b"mp4")
    rec = {"candidate_id": "song_1", "status": "review_ready", "title": "【李豆沙】《嘉宾》",
           "delivered": str(delivery / "歌切_嘉宾.mp4"), "decision": "BLOCK",
           "reason_codes": ["ADVISORY_NO_NATURAL_CLOSURE"]}
    assert cover_repair_needed("2026-07-06", rec)  # 交付但语义BLOCK → 照样补封面
    (delivery / "歌切_嘉宾.cover.png").write_bytes(b"png")
    assert cover_repair_needed("2026-07-06", rec)  # bare PNG has no title/video/hash authority
    # 未交付(门拦)的歌永远不补封面
    assert not cover_repair_needed("2026-07-06", {"candidate_id": "song_2", "status": "blocked",
                                                  "title": "x", "decision": "BLOCK"})


def test_legacy_song_authority_preflight_blocks_before_image_request(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "BASE", tmp_path / "autoslice")
    monkeypatch.setattr(runner, "pipeline_fingerprint", lambda: "sha256:" + "a" * 64)
    delivery = tmp_path / "delivery"
    delivery.mkdir()
    mp4 = delivery / "legacy.mp4"
    cover = delivery / "legacy.cover.png"
    mp4.write_bytes(b"video")
    record = {
        "candidate_id": "semanticsong_legacy",
        "title": "【李豆沙】旧歌切",
        "status": "ok",
        "delivered": str(mp4),
        "cover_status": "REPAIRED_AI_COVER",
    }
    state = {"picks": [], "songs": [record]}
    monkeypatch.setattr(runner, "delivered_paths", lambda _date, _rec: (mp4, cover))
    writes = []
    monkeypatch.setattr(runner, "write_state", lambda _date, _state: writes.append(True))

    def forbidden_run(*_args, **_kwargs):
        raise AssertionError("legacy authority failure must not spend an image request")

    monkeypatch.setattr(runner.subprocess, "run", forbidden_run)
    runner.repair_covers("2026-07-10", state)

    assert record.get("cover_repair_attempts", 0) == 0
    assert record["cover_status"] == "BLOCKED_COVER_AUTHORITY_PREFLIGHT"
    assert record["cover_integrity_status"] == "INVALID_AUTHORITY_PREFLIGHT"
    assert "delivery record" in record["cover_authority_preflight_error"]
    assert writes


def test_blocked_title_authority_requeues_producer_before_cover_spend(
    tmp_path, monkeypatch
):
    fx = _cover_binding_fixture(tmp_path, monkeypatch)
    for path in (
        fx["delivery_record"],
        fx["source_record"],
        fx["publish_path"],
    ):
        document = json.loads(path.read_text(encoding="utf-8"))
        publish_view = (
            document
            if document.get("schema_version") == "shadow-publish-draft.v1"
            else document["publish_staging"]
        )
        publish_view["title_authority_status"] = (
            "BLOCKED_PUBLISH_TITLE_POLICY"
        )
        publish_view["title_authority_error"] = (
            "publish_title_policy_violation:banned_filler_word"
        )
        publish_view["title_policy_violations"] = ["banned_filler_word"]
        path.write_text(json.dumps(document), encoding="utf-8")
    state = {"picks": [fx["rec"]], "songs": []}
    monkeypatch.setattr(
        runner, "pipeline_fingerprint", lambda: "sha256:" + "e" * 64
    )
    monkeypatch.setattr(
        runner,
        "delivered_paths",
        lambda _date, _rec: (fx["mp4"], fx["cover"]),
    )
    monkeypatch.setattr(runner, "write_state", lambda _date, _state: None)

    def forbidden_run(*_args, **_kwargs):
        raise AssertionError("blocked title must not spend an image request")

    monkeypatch.setattr(runner.subprocess, "run", forbidden_run)
    runner.repair_covers(fx["date"], state)

    record = fx["rec"]
    assert record["status"] == "failed"
    assert record["failure_kind"] == "title_authority"
    assert record["failure_stage"] == "cover_authority_preflight"
    assert record["failure_recoverable"] is True
    assert (
        record["cover_status"]
        == "BLOCKED_TITLE_AUTHORITY_REGENERATION_REQUIRED"
    )
    assert record.get("cover_repair_attempts", 0) == 0


def test_delivered_lane_with_valid_active_docs_still_requires_song_manifest(
    tmp_path, monkeypatch
):
    fx = _cover_binding_fixture(tmp_path, monkeypatch)
    # Candidate ids are not the lane authority (real ones include
    # semanticsong_*).  The delivered field is the stable song signal.
    fx["rec"]["delivered"] = str(fx["mp4"])
    state = {"picks": [], "songs": [fx["rec"]]}
    monkeypatch.setattr(runner, "pipeline_fingerprint", lambda: "sha256:" + "d" * 64)
    monkeypatch.setattr(
        runner, "delivered_paths", lambda _date, _rec: (fx["mp4"], fx["cover"])
    )
    monkeypatch.setattr(runner, "write_state", lambda _date, _state: None)

    def forbidden_run(*_args, **_kwargs):
        raise AssertionError("missing song manifest must block before provider call")

    monkeypatch.setattr(runner.subprocess, "run", forbidden_run)
    runner.repair_covers(fx["date"], state)

    assert fx["rec"].get("cover_repair_attempts", 0) == 0
    assert fx["rec"]["cover_status"] == "BLOCKED_COVER_AUTHORITY_PREFLIGHT"
    assert "no verified active-record manifest" in fx["rec"][
        "cover_authority_preflight_error"
    ]


def test_per_clip_http_500_consumes_attempt_and_does_not_starve_next_cover(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "BASE", tmp_path / "autoslice")
    monkeypatch.setattr(runner, "pipeline_fingerprint", lambda: "sha256:" + "a" * 64)
    monkeypatch.setattr(runner, "cover_ref_for", lambda _date, _cid: None)
    monkeypatch.setattr(runner, "child_env", lambda: {})
    monkeypatch.setattr(runner, "_cover_authority_preflight", lambda *_args: None)
    monkeypatch.setattr(runner, "write_state", lambda _date, _state: None)
    (runner.BASE / "logs").mkdir(parents=True)

    paths = {}
    picks = []
    for cid in ("first", "second"):
        mp4 = tmp_path / f"{cid}.mp4"
        mp4.write_bytes(cid.encode())
        paths[cid] = (mp4, tmp_path / f"{cid}.cover.png")
        picks.append(
            {
                "candidate_id": cid,
                "status": "review_ready",
                "title": f"【李豆沙】{cid}",
                "cover_status": "BLOCKED_AI_COVER_REQUIRED",
            }
        )
    monkeypatch.setattr(runner, "delivered_paths", lambda _date, rec: paths[rec["candidate_id"]])

    calls = []

    class Completed:
        def __init__(self, returncode):
            self.returncode = returncode

    def fake_run(cmd, **_kwargs):
        cid = cmd[cmd.index("--candidate-id") + 1]
        output = Path(cmd[cmd.index("--out") + 1])
        calls.append((cid, output))
        if cid == "first":
            return Completed(1)  # deterministic per-clip 500/other failure
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"generated")
        return Completed(0)

    monkeypatch.setattr(runner.subprocess, "run", fake_run)

    def fake_bind(_date, rec, _mp4, _cover, generated_cover):
        assert generated_cover.is_file()
        rec["cover_status"] = "REPAIRED_AI_COVER"

    monkeypatch.setattr(runner, "_bind_repaired_cover", fake_bind)

    state = {"picks": picks, "songs": []}
    runner.repair_covers("2026-07-10", state)

    assert [cid for cid, _output in calls] == ["first", "second"]
    assert picks[0]["cover_repair_attempts"] == 1
    assert picks[1]["cover_status"] == "REPAIRED_AI_COVER"
    assert calls[0][1].parent != calls[1][1].parent


def test_selected_cover_repair_scopes_recovery_budget_and_detection(monkeypatch):
    selected = {
        "candidate_id": "selected",
        "status": "review_ready",
        "title": "【李豆沙】selected",
    }
    neighbor = {
        "candidate_id": "neighbor",
        "status": "review_ready",
        "title": "【李豆沙】neighbor",
    }
    state = {"picks": [selected, neighbor], "songs": []}
    calls = []
    monkeypatch.setattr(
        runner,
        "delivered_paths",
        lambda _date, rec: (Path(f"/{rec['candidate_id']}.mp4"), Path(f"/{rec['candidate_id']}.png")),
    )
    monkeypatch.setattr(
        runner,
        "_roll_forward_prepared_cover_transactions",
        lambda _date, rec, *_paths: calls.append(("roll", rec["candidate_id"])) or False,
    )
    monkeypatch.setattr(
        runner,
        "_recover_committed_cover_binding",
        lambda _date, rec, *_paths: calls.append(("recover", rec["candidate_id"])) or False,
    )
    monkeypatch.setattr(
        runner,
        "_refresh_cover_repair_budget",
        lambda rec, _fingerprint: calls.append(("budget", rec["candidate_id"])) or False,
    )
    monkeypatch.setattr(
        runner,
        "cover_repair_needed",
        lambda _date, rec: calls.append(("needed", rec["candidate_id"])) or False,
    )
    monkeypatch.setattr(runner, "pipeline_fingerprint", lambda: "sha256:test")

    runner.repair_covers(
        "2026-07-10", state, candidate_ids={"selected"}
    )

    assert calls == [
        ("roll", "selected"),
        ("recover", "selected"),
        ("budget", "selected"),
        ("needed", "selected"),
    ]


def test_selected_cover_repair_rejects_unknown_or_duplicate_state_rows():
    state = {
        "picks": [
            {"candidate_id": "duplicate"},
            {"candidate_id": "duplicate"},
        ],
        "songs": [],
    }
    with pytest.raises(ValueError, match="missing or duplicated"):
        runner.repair_covers(
            "2026-07-10", state, candidate_ids={"duplicate"}
        )
    with pytest.raises(ValueError, match="missing or duplicated"):
        runner.repair_covers(
            "2026-07-10", state, candidate_ids={"unknown"}
        )


def test_reviewed_cover_text_gate_rejects_dropped_question_mark_before_binding(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(runner, "BASE", tmp_path / "autoslice")
    monkeypatch.setattr(runner, "pipeline_fingerprint", lambda: "sha256:" + "a" * 64)
    monkeypatch.setattr(runner, "cover_ref_for", lambda _date, _cid: None)
    monkeypatch.setattr(runner, "child_env", lambda: {})
    monkeypatch.setattr(runner, "_cover_authority_preflight", lambda *_args: None)
    monkeypatch.setattr(runner, "write_state", lambda _date, _state: None)
    (runner.BASE / "logs").mkdir(parents=True)
    mp4 = tmp_path / "clip.mp4"
    mp4.write_bytes(b"video")
    cover = tmp_path / "clip.cover.png"
    record = {
        "candidate_id": "auto_punctuation",
        "status": "review_ready",
        "title": "【李豆沙】去彩排前连问三遍：你们还要来找我玩，好不好？",
        "cover_status": "BLOCKED_AI_COVER_REQUIRED",
    }
    monkeypatch.setattr(runner, "delivered_paths", lambda _date, _rec: (mp4, cover))

    class Completed:
        returncode = 0

    commands = []

    def generate(command, **_kwargs):
        commands.append(command)
        output = Path(command[command.index("--out") + 1])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"generated")
        return Completed()

    monkeypatch.setattr(runner.subprocess, "run", generate)
    monkeypatch.setattr(
        runner,
        "_validate_repaired_cover_generation",
        lambda **_kwargs: (
            {
                "cover_text": "去彩排前连问三遍\n你们还要来找我玩，好不好？",
                "rendered_lines": ["去彩排前", "连问三遍", "你们还要", "来找我玩", "，好不好"],
            },
            tmp_path / "generation.json",
        ),
    )
    monkeypatch.setattr(
        runner,
        "_bind_repaired_cover",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("punctuation-bad cover must not bind")
        ),
    )

    state = {"picks": [record], "songs": []}
    runner.repair_covers(
        "2026-07-10",
        state,
        candidate_ids={"auto_punctuation"},
        expected_cover_texts={
            "auto_punctuation": "去彩排前连问三遍\n你们还要来找我玩，好不好？"
        },
    )

    assert record["cover_repair_attempts"] == 1
    assert record["cover_integrity_status"] == "INVALID_REPAIR_PENDING"
    assert record.get("cover_binding_path") is None
    assert commands[0][commands[0].index("--cover-text") + 1] == (
        "去彩排前连问三遍\n你们还要来找我玩，好不好？"
    )


def test_cover_repair_attempts_use_unique_immutable_generation_directories(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "BASE", tmp_path / "autoslice")
    monkeypatch.setattr(runner, "pipeline_fingerprint", lambda: "sha256:" + "b" * 64)
    monkeypatch.setattr(runner, "cover_ref_for", lambda _date, _cid: None)
    monkeypatch.setattr(runner, "child_env", lambda: {})
    monkeypatch.setattr(runner, "_cover_authority_preflight", lambda *_args: None)
    monkeypatch.setattr(runner, "write_state", lambda _date, _state: None)
    (runner.BASE / "logs").mkdir(parents=True)
    mp4 = tmp_path / "clip.mp4"
    mp4.write_bytes(b"video")
    cover = tmp_path / "clip.cover.png"
    rec = {
        "candidate_id": "clip",
        "status": "review_ready",
        "title": "【李豆沙】clip",
        "cover_status": "BLOCKED_AI_COVER_REQUIRED",
    }
    monkeypatch.setattr(runner, "delivered_paths", lambda _date, _rec: (mp4, cover))
    outputs = []

    class Failed:
        returncode = 1

    def fail(cmd, **_kwargs):
        outputs.append(Path(cmd[cmd.index("--out") + 1]))
        return Failed()

    monkeypatch.setattr(runner.subprocess, "run", fail)
    state = {"picks": [rec], "songs": []}
    runner.repair_covers("2026-07-10", state)
    runner.repair_covers("2026-07-10", state)

    assert len(outputs) == 2
    assert outputs[0] != outputs[1]
    assert outputs[0].parent.parent.name == "generations"
    assert outputs[1].parent.parent.name == "generations"
    assert rec["cover_repair_attempts"] == 2
    assert rec["cover_repair_lifetime_attempts"] == 2


def test_third_successful_generation_with_binding_failure_is_loudly_exhausted(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "BASE", tmp_path / "autoslice")
    fingerprint = "sha256:" + "c" * 64
    monkeypatch.setattr(runner, "pipeline_fingerprint", lambda: fingerprint)
    monkeypatch.setattr(runner, "cover_ref_for", lambda _date, _cid: None)
    monkeypatch.setattr(runner, "child_env", lambda: {})
    monkeypatch.setattr(runner, "_cover_authority_preflight", lambda *_args: None)
    writes = []
    monkeypatch.setattr(runner, "write_state", lambda _date, state: writes.append(json.loads(json.dumps(state))))
    (runner.BASE / "logs").mkdir(parents=True)
    mp4 = tmp_path / "clip.mp4"
    mp4.write_bytes(b"video")
    cover = tmp_path / "clip.cover.png"
    rec = {
        "candidate_id": "clip",
        "status": "review_ready",
        "title": "【李豆沙】clip",
        "cover_status": "BLOCKED_AI_COVER_REQUIRED",
        "cover_repair_generation": fingerprint,
        "cover_repair_attempts": 2,
        "cover_repair_lifetime_attempts": 2,
    }
    monkeypatch.setattr(runner, "delivered_paths", lambda _date, _rec: (mp4, cover))

    class Completed:
        returncode = 0

    def generate(cmd, **_kwargs):
        output = Path(cmd[cmd.index("--out") + 1])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"generated")
        return Completed()

    monkeypatch.setattr(runner.subprocess, "run", generate)
    monkeypatch.setattr(
        runner,
        "_bind_repaired_cover",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("bad active publish")),
    )
    state = {"picks": [rec], "songs": []}
    runner.repair_covers("2026-07-10", state)

    assert len(writes) >= 2  # attempt charged before provider call, outcome persisted after
    assert rec["cover_repair_attempts"] == 3
    assert rec["cover_repair_exhausted"] is True
    assert rec["cover_integrity_status"] == "INVALID_REPAIR_BUDGET_EXHAUSTED"
    assert "repair_failed_x3" in rec["cover_status"]


def test_cover_reuse_background_without_bound_manifest_fails_closed(tmp_path):
    from scripts.regenerate_lidousha_cover import regenerate_cover

    with pytest.raises(SystemExit, match="REUSE_BG_REQUIRES_BOUND_SOURCE_MANIFEST"):
        regenerate_cover(
            title="【李豆沙】测试",
            out_path=tmp_path / "cover.png",
            ai_bg_path=tmp_path / "old-ai.png",
            reuse_bg=True,
            use_llm=False,
        )


def test_produce_batch_preserves_order_and_isolates_crashes():
    """Parallel batch (Ivan 2026-07-07 提速): results come back in input order and
    one crashing slice becomes a failed result instead of killing the batch."""
    items = [{"cid": f"c{i}", "hook": f"h{i}"} for i in range(5)]

    def fake_produce(date, item):
        if item["cid"] == "c2":
            raise RuntimeError("boom")
        return {"candidate_id": item["cid"], "status": "ok"}

    results = runner.produce_batch("2026-07-06", items, fake_produce)
    assert [r["candidate_id"] for r in results] == ["c0", "c1", "c2", "c3", "c4"]  # order preserved
    assert results[2]["status"] == "failed" and "boom" in results[2]["error"]  # crash isolated
    assert all(r["status"] == "ok" for i, r in enumerate(results) if i != 2)
    assert runner.produce_batch("2026-07-06", [], fake_produce) == []

# ---------------------------------------------------------------------------
# runner v4 (2026-07-09 external audit: "the control plane was lying")
# ---------------------------------------------------------------------------


def test_prioritize_global_confidence_ranking_beats_arrival_order():
    """7/9 real failure: a conf=0.94 candidate lost to five earlier 0.85-0.90
    because selection was segment round-robin.  Ranking is now GLOBAL."""
    state = {
        "picks": [], "songs": [], "pending_song": [],
        "pending_talk": [
            {"segment_path": "/rec/early.mp4", "start_ms": i * 1000, "end_ms": i * 1000 + 9000,
             "hook": f"e{i}", "confidence": 0.86, "cid": f"e{i}"}
            for i in range(5)
        ] + [
            {"segment_path": "/rec/late.mp4", "start_ms": 0, "end_ms": 9000,
             "hook": "拜早年联欢晚会", "confidence": 0.94, "cid": "late"},
        ],
    }
    prioritize(state)
    kept = {i["cid"] for i in state["pending_talk"]}
    assert "late" in kept, "最高分候选必须赢，不看到达顺序"
    assert len(state["pending_talk"]) == MAX_TALK_PICKS
    assert "全局排序" in state["not_selected"][0]


def test_prioritize_hard_tier_beats_confidence_for_722_calibration_pair():
    """7/22: 礼墨反转是 Tier 1；下播挽留只是 Tier 2。"""

    def scorecard(tier: int) -> dict:
        normalized = normalize_selection_scorecard(
            {
                "tier": tier,
                "tier_basis": (
                    "relationship_chain" if tier == 1 else "personal_stance"
                ),
                "tier_reason": "7/22 calibrated pair",
                "tier_evidence_cues": [1, 2],
                "dimensions": {
                    "lidousha_centrality": 4,
                    "stance_intensity": 3,
                    "audience_salience": 4,
                    "relationship_interaction": 4 if tier == 1 else 2,
                    "persona_reversal": 3,
                    "comedic_payoff": 3,
                    "self_contained": 4,
                },
                "uncertainty_penalty": 0,
                "fatigue_penalty": 0,
            },
            start_cue=1,
            end_cue=2,
        )
        assert normalized is not None
        return normalized

    state = {
        "picks": [],
        "songs": [],
        "pending_song": [],
        "pending_talk": [
            {
                "segment_path": "/rec/full.mp4",
                "start_ms": 5_390_000,
                "end_ms": 5_520_000,
                "hook": "下播时没人挽留谁更可怜",
                "confidence": 0.99,
                "selection_scorecard": scorecard(2),
                "cid": "signoff",
            },
            {
                "segment_path": "/rec/full.mp4",
                "start_ms": 670_000,
                "end_ms": 910_000,
                "hook": "最喜欢的金发有角妹妹像礼墨又秒改低才对",
                "confidence": 0.81,
                "selection_scorecard": scorecard(1),
                "cid": "sumi-reversal",
            },
        ],
    }

    prioritize(state)

    assert [item["cid"] for item in state["pending_talk"]] == [
        "sumi-reversal",
        "signoff",
    ]


def test_semantic_talk_cannot_produce_without_valid_quant_scorecard():
    result = runner.produce_talk(
        "2026-07-22",
        {
            "cid": "unscored",
            "segment_path": "/rec/full.mp4",
            "start_ms": 0,
            "end_ms": 60_000,
            "hook": "未量化候选",
            "confidence": 0.99,
            "lane": "semantic_recall_sharded",
            "selection_scorecard": None,
        },
    )

    assert result["status"] == "candidate_rejected"
    assert result["rejection_reason"] == "selection_scorecard_missing_or_invalid"
    assert result["failure_evidence"]["gate"] == "SELECTION_SCORECARD_REQUIRED"


def test_recording_session_annotation_uses_live_start_metadata(tmp_path, monkeypatch):
    """Two streams on one date must be keyed by the recorder's live start,
    not collapsed into the date-level legacy quota."""
    segment = tmp_path / "22966160_20260716-20-30-03.mp4"
    segment.write_bytes(b"media")
    segment.with_suffix(".meta.json").write_text(
        json.dumps(
            {
                "Date": "2026-07-16 19:59:58+08:00",
                "description": {"LiveStartTime": "2026-07-16 19:59:58+08:00"},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(runner, "list_segments", lambda _date: [segment])
    state = {
        "picks": [{"segment": segment.name, "status": "review_ready"}],
        "talk_backlog": [{"segment_path": str(segment), "cid": "late"}],
    }

    assert runner.annotate_state_sessions("2026-07-16", state) is True
    expected = "live-20260716T195958+0800"
    assert state["segment_sessions"][segment.stem] == expected
    assert state["recording_sessions"] == [expected]
    assert state["picks"][0]["session_id"] == expected
    assert state["talk_backlog"][0]["session_id"] == expected


def test_session_relation_authority_does_not_bleed_into_solo_stream_same_date(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    collab = tmp_path / "22966160_20260722-19-34-50.mp4"
    solo = tmp_path / "22966160_20260722-23-00-00.mp4"
    collab.write_bytes(b"collab")
    solo.write_bytes(b"solo")
    authority = {
        "schema_version": "session-relation-authority.v1",
        "relation_id": "lidousha-nancho",
        "state": "CONFIRMED",
    }
    monkeypatch.setattr(runner, "list_segments", lambda _date: [collab, solo])
    monkeypatch.setattr(
        runner,
        "recording_session_id",
        lambda segment, _date: f"live-{segment.stem}",
    )
    monkeypatch.setattr(
        runner,
        "session_relation_for_segment",
        lambda _date, segment: authority if segment == collab else None,
    )
    state = {
        "pending_talk": [
            {"cid": "collab", "segment_path": str(collab)},
            {"cid": "solo", "segment_path": str(solo)},
        ]
    }

    assert runner.annotate_state_sessions("2026-07-22", state) is True
    assert state["pending_talk"][0]["session_relation_authority"] == authority
    assert "session_relation_authority" not in state["pending_talk"][1]


def test_session_relation_binds_verified_official_replay_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    segment = tmp_path / "22966160_20260722-19-34-50.mp4"
    segment.write_bytes(b"official replay fixture")
    source_sha256 = "sha256:" + "a" * 64
    calls: list[dict[str, object]] = []

    monkeypatch.setattr(runner, "list_segments", lambda _date: [segment])
    monkeypatch.setattr(
        runner,
        "session_relation_for_segment",
        lambda date, path, **kwargs: calls.append(
            {"date": date, "path": path, **kwargs}
        )
        or {
            "schema_version": "session-relation-authority.v1",
            "relation_id": "lidousha-nancho",
            "state": "CONFIRMED",
            "bound_source_sha256": kwargs.get("source_sha256"),
        },
    )
    state = {
        "source_sha256": source_sha256,
        "source_authority": "OFFICIAL_COMPLETE_REPLAY",
        "source_integrity": {
            "status": "PASS",
            "can_select": True,
            "consumer_segments": [str(segment)],
            "issues": [],
        }
    }

    assert runner.annotate_state_sessions("2026-07-22", state) is True
    assert calls[0]["source_sha256"] == source_sha256
    assert state["session_relation_authority"]["bound_source_sha256"] == source_sha256


def test_session_relation_does_not_bind_unverified_declared_source_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    segment = tmp_path / "22966160_20260722-19-34-50.mp4"
    segment.write_bytes(b"unverified fixture")
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(runner, "list_segments", lambda _date: [segment])
    monkeypatch.setattr(
        runner,
        "session_relation_for_segment",
        lambda date, path, **kwargs: calls.append(kwargs) or None,
    )
    state = {
        "source_sha256": "sha256:" + "b" * 64,
        "source_authority": "RECORDER",
        "source_integrity": {
            "status": "PASS",
            "can_select": True,
            "consumer_segments": [str(segment)],
            "issues": [],
        },
    }

    runner.annotate_state_sessions("2026-07-22", state)
    assert calls == [{}]


def test_session_annotation_recovers_state_segment_missing_from_inventory(tmp_path, monkeypatch):
    segment = tmp_path / "22966160_20260716-20-30-03.mp4"
    segment.write_bytes(b"media")
    segment.with_suffix(".meta.json").write_text(
        json.dumps({"description": {"LiveStartTime": "2026-07-16 19:59:58+08:00"}}),
        encoding="utf-8",
    )
    # A flaky FUSE listing can omit one file even though a durable state row
    # still points to that readable recording.
    monkeypatch.setattr(runner, "list_segments", lambda _date: [])
    state = {"talk_backlog": [{"segment_path": str(segment), "cid": "late"}]}

    assert runner.annotate_state_sessions("2026-07-16", state) is True
    assert state["talk_backlog"][0]["session_id"] == "live-20260716T195958+0800"
    assert state["segment_sessions"][segment.stem] == "live-20260716T195958+0800"


def test_prioritize_allocates_talk_quota_per_live_session():
    first = "live-20260716T140049+0800"
    evening = "live-20260716T195958+0800"
    state = {
        "picks": [
            {"candidate_id": f"first-{index}", "status": "review_ready", "session_id": first}
            for index in range(MAX_TALK_PICKS)
        ],
        "songs": [],
        "pending_song": [],
        "pending_talk": [
            {
                "cid": f"evening-{index}",
                "segment_path": f"/rec/evening-{index // 2}.mp4",
                "start_ms": index * 10_000,
                "end_ms": index * 10_000 + 9_000,
                "confidence": 0.99 - index * 0.01,
                "hook": f"晚场候选{index}",
                "session_id": evening,
            }
            for index in range(MAX_TALK_PICKS + 1)
        ],
    }

    runner.prioritize(state)

    assert len(state["pending_talk"]) == MAX_TALK_PICKS
    assert {item["session_id"] for item in state["pending_talk"]} == {evening}
    assert [item["cid"] for item in state["talk_backlog"]] == [f"evening-{MAX_TALK_PICKS}"]


def test_song_attempt_cap_and_delivery_budget_are_per_live_session():
    first = "live-20260716T140049+0800"
    evening = "live-20260716T195958+0800"
    state = {
        "picks": [],
        "songs": [
            {"candidate_id": f"first-song-{index}", "status": "blocked", "session_id": first}
            for index in range(runner.SONG_ATTEMPT_CAP)
        ],
        "pending_song": [],
        "song_backlog": [
            {
                "cid": f"evening-song-{index}",
                "segment_path": "/rec/evening.mp4",
                "anchor_start_ms": index * 100_000,
                "anchor_end_ms": index * 100_000 + 60_000,
                "danmaku": 100 - index,
                "session_id": evening,
            }
            for index in range(3)
        ],
    }

    runner.refill_songs(state)

    assert [item["cid"] for item in state["pending_song"]] == ["evening-song-0"]
    assert [item["cid"] for item in state["song_backlog"]] == [
        "evening-song-1",
        "evening-song-2",
    ]


def test_selected_song_repairs_cannot_overfill_one_session_delivery_quota():
    first = "live-first"
    second = "live-second"
    state = {
        "songs": [],
        "pending_song": [
            {
                "cid": f"repair-{session_id}-{index}",
                "segment_path": f"/rec/{session_id}.mp4",
                "anchor_start_ms": index * 10_000,
                "anchor_end_ms": index * 10_000 + 5_000,
                "selected_repair": True,
                "session_id": session_id,
            }
            for session_id, count in ((first, 4), (second, 3))
            for index in range(count)
        ],
        "song_backlog": [],
    }

    runner.refill_songs(state)

    selected_by_session = {
        session_id: sum(
            1 for item in state["pending_song"] if item["session_id"] == session_id
        )
        for session_id in (first, second)
    }
    assert selected_by_session == {
        first: MAX_SONGS_PER_DATE,
        second: MAX_SONGS_PER_DATE,
    }
    assert len(state["song_backlog"]) == 7 - 2 * MAX_SONGS_PER_DATE


def test_new_session_backlog_reopens_an_otherwise_finished_date():
    first = "live-20260716T140049+0800"
    evening = "live-20260716T195958+0800"
    state = {
        "picks": [
            {"candidate_id": f"first-{index}", "status": "review_ready", "session_id": first}
            for index in range(MAX_TALK_PICKS)
        ],
        "songs": [],
        "talk_backlog": [
            {
                "cid": "evening",
                "segment_path": "/rec/evening.mp4",
                "start_ms": 0,
                "end_ms": 9_000,
                "confidence": 0.95,
                "session_id": evening,
            }
        ],
        "song_backlog": [],
    }

    assert runner.backlog_has_eligible_session_work(state) is True


def test_produce_batch_preserves_session_id_on_success():
    result = runner.produce_batch(
        "2026-07-16",
        [{"cid": "evening", "session_id": "live-evening"}],
        lambda _date, _item: {"candidate_id": "evening", "status": "review_ready"},
    )

    assert result[0]["session_id"] == "live-evening"


def test_produce_batch_forwards_persisted_reuse_cover_for_talk_repairs(monkeypatch):
    calls = []

    def fake_talk(_date, item, *, reuse_cover=False):
        calls.append((item["cid"], reuse_cover))
        return {"candidate_id": item["cid"], "status": "review_ready"}

    monkeypatch.setattr(runner, "produce_talk", fake_talk)

    results = runner.produce_batch(
        "2026-07-16",
        [
            {"cid": "repair", "reuse_cover": True},
            {"cid": "fresh"},
        ],
        fake_talk,
    )

    assert calls == [("repair", True), ("fresh", False)]
    assert [row["candidate_id"] for row in results] == ["repair", "fresh"]


def test_recoverable_failed_pick_reserves_its_seat_from_backfill():
    """Ivan 2026-07-13 对账铁律：可恢复失败的原选手先复活，候补不许趁基础设施
    故障上位（7/11 实况：2 条 failed 席被当空席→候补顶上→复活后一天超发 7 条）。
    重试额度耗尽的不再占席。"""
    def pick(status, recoverable=None, retries=0):
        row = {"status": status, "candidate_id": f"p{status}{retries}"}
        if recoverable is not None:
            row["failure_recoverable"] = recoverable
            row["talk_transient_retry_count"] = retries
        return row

    base_pending = [
        {"segment_path": f"/rec/s{i}.mp4", "start_ms": i * 1000, "end_ms": i * 1000 + 9000,
         "hook": f"h{i}", "confidence": 0.9 - i * 0.01, "cid": f"c{i}"}
        for i in range(6)
    ]
    state = {
        "picks": [pick("review_ready"), pick("review_ready"),
                  pick("failed", recoverable=True, retries=0)],
        "songs": [], "pending_song": [],
        "pending_talk": list(base_pending),
    }
    prioritize(state)
    # 5 席 - 2 已交付 - 1 复活保留 = 2 个候补名额
    assert len(state["pending_talk"]) == 2, [p["cid"] for p in state["pending_talk"]]

    exhausted = {
        "picks": [pick("review_ready"), pick("review_ready"),
                  pick("failed", recoverable=True, retries=99)],
        "songs": [], "pending_song": [],
        "pending_talk": list(base_pending),
    }
    prioritize(exhausted)
    # 重试耗尽 → 席位释放 → 3 个候补名额
    assert len(exhausted["pending_talk"]) == 3


def test_not_selected_stays_deduped_across_rerun_ticks():
    """7/11 real failure: 边界自修复后的重选 tick 把同一批候补重复 append，
    AUTOSLICE_SUMMARY 落选一节整段重复。重跑必须幂等。"""
    state = {
        "picks": [], "songs": [], "pending_song": [],
        "pending_talk": [
            {"segment_path": f"/rec/talkseg{i % 2}.mp4", "start_ms": i * 1000, "end_ms": i * 1000 + 30_000,
             "hook": f"hook{i}", "confidence": 0.9, "cid": f"auto_{i}"}
            for i in range(MAX_TALK_PICKS + 3)
        ],
    }
    prioritize(state)
    first = list(state["not_selected"])
    assert len(first) == 3
    # 模拟下一个 tick：落选候补重新参与全局重选
    state["pending_talk"] = state["pending_talk"] + state["talk_backlog"]
    prioritize(state)
    assert state["not_selected"] == first


def test_prioritize_diversity_cap_is_soft():
    """One segment with 6 candidates and nothing else: the per-segment cap must
    yield rather than deliver fewer than the quota."""
    state = {
        "picks": [], "songs": [], "pending_song": [],
        "pending_talk": [
            {"segment_path": "/rec/one.mp4", "start_ms": i, "end_ms": i + 9,
             "hook": str(i), "confidence": 0.9 - i * 0.01, "cid": str(i)}
            for i in range(6)
        ],
    }
    prioritize(state)
    assert len(state["pending_talk"]) == MAX_TALK_PICKS


def test_prioritize_treats_top_five_as_ceiling_not_low_confidence_fill_target():
    state = {
        "picks": [],
        "songs": [],
        "pending_song": [],
        "pending_talk": [
            {
                "segment_path": "/rec/high.mp4",
                "start_ms": 0,
                "end_ms": 30_000,
                "hook": "高信心完整事件",
                "confidence": 0.81,
                "cid": "high",
            },
            {
                "segment_path": "/rec/low.mp4",
                "start_ms": 40_000,
                "end_ms": 70_000,
                "hook": "低信心候选不应为了凑数交付",
                "confidence": 0.78,
                "cid": "low",
            },
        ],
    }

    prioritize(state)

    assert [item["cid"] for item in state["pending_talk"]] == ["high"]
    assert state["talk_backlog"] == []
    assert [item["cid"] for item in state["talk_below_confidence_threshold"]] == [
        "low"
    ]
    assert any("top-5是上限不是凑数目标" in line for line in state["not_selected"])


def test_rejected_talk_candidate_automatically_backfills_next_ranked_reserve():
    candidates = [
        {
            "segment_path": f"/rec/seg{i}.mp4",
            "start_ms": i * 10_000,
            "end_ms": i * 10_000 + 9_000,
            "hook": f"hook-{i}",
            "confidence": 0.99 - i * 0.01,
            "cid": f"talk-{i}",
        }
        for i in range(6)
    ]
    state = {"picks": [], "songs": [], "pending_song": [], "pending_talk": candidates}
    prioritize(state)
    assert [item["cid"] for item in state["talk_backlog"]] == ["talk-5"]

    state["pending_talk"] = []
    state["picks"] = [
        {"candidate_id": f"talk-{i}", "status": "review_ready"}
        for i in range(4)
    ] + [
        {
            "candidate_id": "talk-4",
            "status": "candidate_rejected",
            "rejection_reason": "unsafe_boundary_backfilled",
        }
    ]
    prioritize(state)

    assert [item["cid"] for item in state["pending_talk"]] == ["talk-5"]
    assert state["talk_backlog"] == []


def test_prioritize_assigns_distinct_cover_background_slots_within_session():
    candidates = [
        {
            "segment_path": "/rec/session.mp4",
            "start_ms": index * 60_000,
            "end_ms": index * 60_000 + 50_000,
            "hook": f"hook-{index}",
            "confidence": 0.99 - index * 0.01,
            "cid": f"talk-{index}",
            "session_id": "live-20260722T193450+0800",
        }
        for index in range(4)
    ]
    state = {
        "picks": [],
        "songs": [],
        "pending_song": [],
        "pending_talk": candidates,
    }

    prioritize(state)

    slots = [item["cover_diversity_slot"] for item in state["pending_talk"]]
    assert slots == [0, 1, 2, 3]
    assert len(set(slots)) == 4


def test_process_date_blocks_finalized_raw_segment_missing_mp4(monkeypatch, tmp_path):
    date = "2026-07-22"
    rec_root = tmp_path / "recordings"
    date_dir = rec_root / date
    date_dir.mkdir(parents=True)
    good = "22966160_20260722-19-35-15"
    orphan = "22966160_20260722-20-05-11"
    (date_dir / f"{good}.mp4").write_bytes(b"usable")
    (date_dir / f"{orphan}.m4s").write_bytes(b"raw")
    (date_dir / f"{orphan}.m3u8").write_text(
        "#EXTM3U\n#EXT-X-ENDLIST\n",
        encoding="utf-8",
    )
    state = {
        "status": "review_ready",
        "segments_done": [good],
        "segments_dead": {},
        "pending_talk": [],
        "pending_song": [],
        "picks": [{"candidate_id": "old", "status": "review_ready"}],
        "songs": [],
    }
    alerts = []
    monkeypatch.setattr(runner, "REC_ROOT", rec_root)
    monkeypatch.setattr(runner, "read_state", lambda _date: state)
    monkeypatch.setattr(runner, "runtime_health_error", lambda: None)
    monkeypatch.setattr(runner, "write_state", lambda _date, _state: None)
    monkeypatch.setattr(runner, "write_reports", lambda _date, _state: None)
    monkeypatch.setattr(runner, "write_alert", lambda code, message: alerts.append((code, message)))
    monkeypatch.setattr(
        runner,
        "cpa_healthy",
        lambda: pytest.fail("source inventory must block before the CPA gate"),
    )

    runner.process_date(date)

    assert state["status"] == "source_incomplete"
    assert state["source_integrity"]["can_select"] is False
    assert state["source_integrity"]["issues"][0]["code"] == "FINALIZED_PLAYLIST_WITHOUT_MP4"
    assert alerts and alerts[0][0] == "SOURCE_INCOMPLETE"


def test_process_date_backfills_after_speaker_anchor_evidence_shortage(monkeypatch, tmp_path):
    date = "2026-07-11"
    monkeypatch.setattr(runner, "BASE", tmp_path / "autoslice")
    first = {
        "segment_path": "/rec/session.mp4",
        "seg_dur_ms": 2_000_000,
        "start_ms": 1_367_000,
        "end_ms": 1_407_000,
        "hook": "speaker anchor shortage",
        "confidence": 0.99,
        "cid": "auto_154845_1367_1407",
    }
    reserve = {
        "segment_path": "/rec/session.mp4",
        "seg_dur_ms": 2_000_000,
        "start_ms": 1_450_000,
        "end_ms": 1_490_000,
        "hook": "safe reserve",
        "confidence": 0.98,
        "cid": "auto_reserve",
    }
    state = {
        "status": "processing",
        "segments_done": ["session"],
        "segments_dead": {},
        "pending_talk": [first, reserve],
        "pending_song": [],
        "picks": [],
        "songs": [],
    }
    monkeypatch.setattr(runner, "MAX_TALK_PICKS", 1)
    monkeypatch.setattr(runner, "AUTOMATIC_MAINTENANCE_NOT_BEFORE", date)
    monkeypatch.setattr(runner, "read_state", lambda _date: state)
    monkeypatch.setattr(runner, "runtime_health_error", lambda: None)
    monkeypatch.setattr(runner, "recover_bound_song_deliveries", lambda _date, _state: 0)
    monkeypatch.setattr(runner, "requeue_recoverable_talks", lambda _date, _state: 0)
    monkeypatch.setattr(runner, "requeue_recoverable_songs", lambda _date, _state: 0)
    monkeypatch.setattr(runner, "requeue_recoverable_deliveries", lambda _date, _state: (0, 0, 0))
    monkeypatch.setattr(runner, "list_segments", lambda _date: [])
    monkeypatch.setattr(runner, "cover_repair_needed", lambda _date, _row: False)
    monkeypatch.setattr(runner, "cpa_healthy", lambda: True)
    monkeypatch.setattr(runner, "discover_segments", lambda _date, _state: None)
    monkeypatch.setattr(runner, "session_sealed", lambda _date, _state: True)
    monkeypatch.setattr(runner, "refill_songs", lambda _state: None)
    monkeypatch.setattr(runner, "repair_covers", lambda _date, _state: None)
    monkeypatch.setattr(runner, "write_state", lambda _date, _state: None)
    monkeypatch.setattr(runner, "write_reports", lambda _date, _state: None)
    attempted: list[str] = []

    def fake_produce_batch(_date, items, _producer):
        cid = items[0]["cid"]
        attempted.append(cid)
        if cid == first["cid"]:
            return [
                {
                    "candidate_id": cid,
                    "status": "speaker_evidence_insufficient",
                    "failure_kind": "speaker_evidence",
                    "failure_stage": "speaker_finalization",
                    "failure_recoverable": False,
                }
            ]
        return [{"candidate_id": cid, "status": "review_ready"}]

    monkeypatch.setattr(runner, "produce_batch", fake_produce_batch)

    runner.process_date(date)

    assert attempted == [first["cid"], reserve["cid"]]
    assert state["picks"] == [
        {
            "candidate_id": first["cid"],
            "status": "candidate_rejected",
            "failure_kind": "speaker_evidence",
            "failure_stage": "speaker_finalization",
            "failure_recoverable": False,
            "rejected_status": "speaker_evidence_insufficient",
            "rejection_reason": "speaker_identity_unresolved_backfilled",
        },
        {
            "candidate_id": reserve["cid"],
            "status": "review_ready",
            "bundle_lifecycle": "CURRENT",
            "bundle_compliance": "COMPLIANT",
        },
    ]
    assert state["pending_talk"] == []


def test_process_date_exact_selection_preserves_speaker_evidence_failure(
    monkeypatch, tmp_path
):
    date = "2026-07-11"
    candidate = {
        "segment_path": "/rec/session.mp4",
        "seg_dur_ms": 2_000_000,
        "start_ms": 1_367_000,
        "end_ms": 1_407_000,
        "hook": "speaker anchor shortage",
        "confidence": 0.99,
        "cid": "auto_154845_1367_1407",
    }
    state = {
        "status": "processing",
        "run_mode": "RECOVERY_REVIEW",
        "upload_allowed": False,
        "talk_selection_contract": {
            "schema_version": "talk-selection-contract.v1",
            "mode": "EXACT_CANDIDATE_SET_NO_BACKFILL",
            "candidate_ids": [candidate["cid"]],
            "source_state_sha256": "sha256:" + "a" * 64,
            "authority": "Ivan selected the exact recovery set",
        },
        "segments_done": ["session"],
        "segments_dead": {},
        "pending_talk": [candidate],
        "pending_song": [],
        "picks": [],
        "songs": [],
    }
    monkeypatch.setattr(runner, "BASE", tmp_path / "autoslice")
    monkeypatch.setattr(runner, "AUTOMATIC_MAINTENANCE_NOT_BEFORE", date)
    monkeypatch.setattr(runner, "read_state", lambda _date: state)
    monkeypatch.setattr(runner, "runtime_health_error", lambda: None)
    monkeypatch.setattr(runner, "recover_bound_song_deliveries", lambda *_args: 0)
    monkeypatch.setattr(runner, "requeue_recoverable_talks", lambda *_args: 0)
    monkeypatch.setattr(runner, "requeue_recoverable_songs", lambda *_args: 0)
    monkeypatch.setattr(
        runner, "requeue_recoverable_deliveries", lambda *_args: (0, 0, 0)
    )
    monkeypatch.setattr(runner, "list_segments", lambda _date: [])
    monkeypatch.setattr(runner, "cover_repair_needed", lambda *_args: False)
    monkeypatch.setattr(runner, "cpa_healthy", lambda: True)
    monkeypatch.setattr(runner, "discover_segments", lambda *_args: None)
    monkeypatch.setattr(runner, "session_sealed", lambda *_args: True)
    monkeypatch.setattr(runner, "refill_songs", lambda _state: None)
    monkeypatch.setattr(runner, "repair_covers", lambda *_args: None)
    monkeypatch.setattr(runner, "write_state", lambda *_args: None)
    monkeypatch.setattr(runner, "write_reports", lambda *_args: None)
    monkeypatch.setattr(
        runner,
        "produce_batch",
        lambda *_args: [
            {
                "candidate_id": candidate["cid"],
                "status": "speaker_evidence_insufficient",
                "failure_kind": "speaker_evidence",
                "failure_stage": "speaker_finalization",
                "failure_recoverable": False,
            }
        ],
    )

    runner.process_date(date)

    assert len(state["picks"]) == 1
    result = state["picks"][0]
    assert result["status"] == "speaker_evidence_insufficient"
    assert "rejected_status" not in result
    assert result["backfill_suppressed_by_exact_contract"]["status"] == (
        "speaker_evidence_insufficient"
    )
    assert state["status"] == "recovery_incomplete"


def test_prioritize_blocks_all_talk_shapes_overlapping_song_interval():
    """A blocked/background song cannot be laundered as a sibling talk cut."""

    state = {
        "picks": [],
        "songs": [],
        "pending_song": [
            {
                "segment_path": "/rec/session.mp4",
                "anchor_start_ms": 100_000,
                "anchor_end_ms": 160_000,
                "danmaku": 9,
                "cid": "song_background",
            }
        ],
        "pending_talk": [
            {"segment_path": "/rec/session.mp4", "start_ms": 98_000, "end_ms": 164_000, "cid": "parent"},
            {"segment_path": "/rec/session.mp4", "start_ms": 110_000, "end_ms": 120_000, "cid": "child"},
            {"segment_path": "/rec/session.mp4", "start_ms": 159_999, "end_ms": 170_000, "cid": "tail"},
            {"segment_path": "/rec/session.mp4", "start_ms": 160_000, "end_ms": 170_000, "cid": "adjacent"},
            {"segment_path": "/rec/session.mp4", "start_ms": 525_000, "end_ms": 535_000, "cid": "outside"},
            {"segment_path": "/rec/other.mp4", "start_ms": 110_000, "end_ms": 120_000, "cid": "other"},
        ],
    }

    prioritize(state)

    assert {item["cid"] for item in state["pending_talk"]} == {"outside", "other"}
    assert {item["candidate_id"] for item in state["song_overlap_blocked_talk"]} == {
        "parent",
        "child",
        "tail",
        "adjacent",
    }
    assert state["song_quarantine_intervals"] == [
        {
            "segment_path": "/rec/session.mp4",
            "start_ms": 95_000,
            "end_ms": 165_000,
            "original_anchor_start_ms": 100_000,
            "original_anchor_end_ms": 160_000,
            "candidate_id": "song_background",
            "reason_code": "SONG_INTERVAL_REQUIRES_JOINT_SINGING_PROOF",
        }
    ]

    # The taint is durable even after the song leaves pending/backlog.
    state["pending_song"] = []
    state["song_backlog"] = []
    state["pending_talk"] = [
        {"segment_path": "/rec/session.mp4", "start_ms": 120_000, "end_ms": 130_000, "cid": "late_clone"}
    ]
    prioritize(state)
    assert state["pending_talk"] == []
    assert state["song_overlap_blocked_talk"][-1]["candidate_id"] == "late_clone"


def test_quarantine_canonicalizes_persisted_broad_proof_window():
    state = {
        "picks": [],
        "songs": [],
        "pending_song": [],
        "song_backlog": [],
        "pending_talk": [
            {
                "segment_path": "/rec/session.mp4",
                "start_ms": 200_000,
                "end_ms": 260_000,
                "cid": "talk_outside_real_anchor",
                "confidence": 0.9,
            }
        ],
        "song_quarantine_intervals": [
            {
                "segment_path": "/rec/session.mp4",
                "start_ms": 0,
                "end_ms": 520_000,
                "original_anchor_start_ms": 100_000,
                "original_anchor_end_ms": 160_000,
                "candidate_id": "song_background",
                "reason_code": "SONG_INTERVAL_REQUIRES_JOINT_SINGING_PROOF",
            }
        ]
    }

    prioritize(state)

    assert state["song_quarantine_intervals"][0]["start_ms"] == 95_000
    assert state["song_quarantine_intervals"][0]["end_ms"] == 165_000
    assert [item["cid"] for item in state["pending_talk"]] == [
        "talk_outside_real_anchor"
    ]


def test_july18_talks_are_not_blocked_by_song_proof_search_context():
    session_id = "live-20260718T225932+0800"
    state = {
        "picks": [],
        "songs": [],
        "pending_song": [
            {
                "segment_path": "/rec/22966160_20260718-23-29-39.mp4",
                "seg_dur_ms": 1_798_015,
                "anchor_start_ms": 1_244_050,
                "anchor_end_ms": 1_508_570,
                "danmaku": 173,
                "cid": "song_warm",
                "session_id": session_id,
            },
            {
                "segment_path": "/rec/22966160_20260718-23-59-36.mp4",
                "seg_dur_ms": 1_440_088,
                "anchor_start_ms": 0,
                "anchor_end_ms": 205_760,
                "danmaku": 187,
                "cid": "song_cross_segment",
                "session_id": session_id,
            },
        ],
        "pending_talk": [
            {
                "segment_path": "/rec/22966160_20260718-22-59-42.mp4",
                "start_ms": 434_530,
                "end_ms": 520_220,
                "cid": "talk_01",
                "confidence": 0.91,
                "session_id": session_id,
            },
            {
                "segment_path": "/rec/22966160_20260718-23-29-39.mp4",
                "start_ms": 1_565_590,
                "end_ms": 1_693_270,
                "cid": "talk_03",
                "confidence": 0.90,
                "session_id": session_id,
            },
            {
                "segment_path": "/rec/22966160_20260718-23-59-36.mp4",
                "start_ms": 527_500,
                "end_ms": 695_400,
                "cid": "talk_05",
                "confidence": 0.89,
                "session_id": session_id,
            },
        ],
    }

    prioritize(state)

    assert {item["cid"] for item in state["pending_talk"]} == {
        "talk_01",
        "talk_03",
        "talk_05",
    }
    assert state.get("song_overlap_blocked_talk", []) == []


def test_session_edge_bgm_is_excluded_without_misclassifying_rotated_song():
    session_id = "live-20260718T225932+0800"
    state = {
        "picks": [],
        "songs": [],
        "pending_talk": [],
        "pending_song": [
            {
                "segment_path": "/rec/22966160_20260718-22-59-42.mp4",
                "seg_dur_ms": 1_797_861,
                "anchor_start_ms": 77_650,
                "anchor_end_ms": 160_360,
                "cid": "opening_bgm",
                "session_id": session_id,
                "danmaku": 0,
            },
            {
                "segment_path": "/rec/22966160_20260718-23-59-36.mp4",
                "seg_dur_ms": 1_440_088,
                "anchor_start_ms": 0,
                "anchor_end_ms": 205_760,
                "cid": "cross_segment_host_song",
                "session_id": session_id,
                "danmaku": 187,
            },
            {
                "segment_path": "/rec/22966160_20260718-23-59-36.mp4",
                "seg_dur_ms": 1_440_088,
                "anchor_start_ms": 1_240_780,
                "anchor_end_ms": 1_312_300,
                "cid": "ending_bgm",
                "session_id": session_id,
                "danmaku": 11,
            },
        ],
    }

    prioritize(state)

    assert [item["cid"] for item in state["pending_song"]] == [
        "cross_segment_host_song"
    ]
    assert {
        row["candidate_id"]: row["reason_code"]
        for row in state["song_edge_bgm_excluded"]
    } == {
        "opening_bgm": "SESSION_INTRO_BGM_BY_POSITION",
        "ending_bgm": "SESSION_OUTRO_BGM_BY_POSITION",
    }


def test_outro_position_uses_whole_session_end_even_when_last_segment_has_no_song():
    session_id = "live-20260718T225932+0800"
    early_stem = "22966160_20260718-22-59-42"
    later_stem = "22966160_20260718-23-29-39"
    state = {
        "picks": [],
        "songs": [],
        "pending_talk": [],
        "segment_sessions": {
            early_stem: session_id,
            later_stem: session_id,
        },
        "segment_durations_ms": {
            early_stem: 1_797_861,
            later_stem: 1_798_015,
        },
        "pending_song": [
            {
                "segment_path": f"/rec/{early_stem}.mp4",
                "seg_dur_ms": 1_797_861,
                "anchor_start_ms": 1_500_000,
                "anchor_end_ms": 1_700_000,
                "cid": "real_song_before_later_quiet_segment",
                "session_id": session_id,
                "danmaku": 100,
            }
        ],
    }

    prioritize(state)

    assert [item["cid"] for item in state["pending_song"]] == [
        "real_song_before_later_quiet_segment"
    ]
    assert state.get("song_edge_bgm_excluded", []) == []


def test_produce_talk_rejects_effective_duration_not_over_45_seconds(
    monkeypatch,
):
    monkeypatch.setattr(
        runner,
        "talk_pipeline_fingerprint",
        lambda _candidate_id: "sha256:test",
    )

    result = runner.produce_talk(
        "2026-07-18",
        {
            "cid": "auto_232939_1520_1538",
            "segment_path": "/recordings/segment.mp4",
            "start_ms": 1_520_610,
            "end_ms": 1_538_450,
        },
    )

    assert result["status"] == "candidate_rejected"
    assert result["effective_duration_ms"] == 17_840
    assert result["reason_codes"] == ["TALK_EFFECTIVE_DURATION_NOT_OVER_45S"]


def test_produce_talk_materializes_reviewed_july18_jump_pieces(
    tmp_path,
    monkeypatch,
):
    date = "2026-07-18"
    base = tmp_path / "autoslice"
    repo = tmp_path / "repo"
    (base / "logs").mkdir(parents=True)
    repo.mkdir()
    monkeypatch.setattr(runner, "BASE", base)
    monkeypatch.setattr(runner, "REPO_ROOT", repo)
    monkeypatch.setattr(runner, "child_env_for_date", lambda _date: {})
    monkeypatch.setattr(
        runner,
        "talk_pipeline_fingerprint",
        lambda _candidate_id: "sha256:test",
    )

    class Completed:
        returncode = 0

    def fake_run(_command, **kwargs):
        kwargs["stdout"].write('{"red_flags": [], "boundary_repairs": []}\n')
        kwargs["stdout"].flush()
        return Completed()

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    result = runner.produce_talk(
        date,
        {
            "cid": "auto_235936_527_695",
            "segment_path": "/recordings/segment.mp4",
            "seg_dur_ms": 1_434_340,
            "start_ms": 527_500,
            "end_ms": 695_400,
            "hook": "和成天下和老李头",
            "reviewed_filler_removals": [
                {"start_ms": 559_670, "end_ms": 569_730, "reason": "gift_thanks"},
                {"start_ms": 603_170, "end_ms": 611_530, "reason": "gift_thanks"},
                {"start_ms": 638_270, "end_ms": 650_250, "reason": "gift_thanks"},
            ],
        },
    )

    spec = json.loads(
        (base / "out" / date / "spec_auto_235936_527_695.json").read_text()
    )
    # The fake producer writes no publish/cover proof.  Media may exist, but it
    # must not be mislabeled review-ready until cover maintenance binds one.
    assert result["status"] == runner.TALK_COVER_PENDING_STATUS
    assert result["effective_duration_ms"] == 137_500
    assert result["talk_filler_removal_count"] == 3
    assert [(row["start_ms"], row["end_ms"]) for row in spec["pieces"]] == [
        (517_500, 559_670),
        (569_730, 603_170),
        (611_530, 638_270),
        (650_250, 743_400),
    ]
    assert spec["minimum_effective_duration_ms"] == 45_000


def test_song_status_words():
    """7/9 audit P0: gate BLOCK was recorded as ok because the subprocess exited 0."""
    assert runner.song_status(1, False) == "failed"
    assert runner.song_status(0, True) == "review_ready"
    assert runner.song_status(0, False) == "blocked"


def test_song_delivery_artifacts_extraction_with_hashes():
    record = {
        "decision_action": "BLOCK",  # semantic decision must NOT gate extraction
        "reason_codes": ["CPA_SEMANTIC_INCOMPLETE", "ADVISORY_NO_NATURAL_CLOSURE"],
        "title": "fallback",
        "materialized_recut": {
            "cover_release_gate": {
                "satisfied": True,
                "path": "/current/gate.json",
                "artifact_hashes": {"burned_video_sha256": "sha256:" + "a" * 64},
            },
            "burned_preview": {"status": "BURNED", "path": "/current/video.mp4"},
            "artifact_hashes": {"cover_sha256": "sha256:" + "b" * 64},
            "publish_staging": {
                "status": "STAGED",
                "title": "【李豆沙】本次歌切",
                "cover_path": "/current/cover.png",
            },
        },
    }
    assert runner.song_delivery_artifacts(record) == {
        "video_path": "/current/video.mp4",
        "video_sha256": "sha256:" + "a" * 64,
        "cover_path": "/current/cover.png",
        "cover_sha256": "sha256:" + "b" * 64,
        "title": "【李豆沙】本次歌切",
        "release_gate_path": "/current/gate.json",
        "cover_release_gate_satisfied": True,
    }


def test_song_delivery_artifacts_video_survives_unsatisfied_cover_gate():
    """Cover gate unsatisfied = 还没出封面，不等于不是歌：video 物料必须照常
    可提取（封面由 repair_covers 事后补）。"""
    record = {
        "decision_action": "AUTO_UPLOAD",
        "reason_codes": [],
        "materialized_recut": {
            "cover_release_gate": {"satisfied": False},
            "burned_preview": {"status": "BURNED", "path": "/current/video.mp4"},
            "publish_staging": {"status": "STAGED", "cover_path": "/pending/cover.png"},
        },
    }
    artifacts = runner.song_delivery_artifacts(record)
    assert artifacts["video_path"] == "/current/video.mp4"
    assert artifacts["cover_release_gate_satisfied"] is False
    assert runner.song_delivery_artifacts({}) == {}


def test_song_delivery_rule_final_requires_positive_completion_proof(tmp_path):
    """Ivan 2026-07-10 最终规则：至多2个按弹幕排序（prioritize/refill 管）；
    是歌+唱完整就交付；语义判定只是参考。没唱完整(SONG_PARTIAL)不强行切。"""
    semantic_noise = ["END_BOUNDARY_LOW", "CPA_SEMANTIC_INCOMPLETE",
                      "VIEWER_CONTEXT_INCOMPLETE", "ADVISORY_NO_NATURAL_CLOSURE"]
    proof_path = tmp_path / "host-vocal-proof.json"
    proof_path.write_text("{}", encoding="utf-8")
    proof = {
        "ready": True,
        "host_vocal_status": "READY",
        "host_vocal_decision": "LIDOUSHA_VOCAL_PRESENT_ON_LYRIC_CHECKPOINTS",
        "live_performance_status": "READY",
        "live_performance_mode": "LIVE_STREAMER_SINGING",
        "joint_singing_decision": "VERIFIED_LIDOUSHA_SINGING",
        "host_vocal_proof_path": str(proof_path),
        "host_vocal_proof_sha256": hashlib.sha256(proof_path.read_bytes()).hexdigest(),
    }
    assert runner.song_delivery_ok(0, True, semantic_noise, proof) is True   # 语义码不拦
    assert runner.song_delivery_ok(0, True, [], True) is False  # bool cannot carry performer/hash proof
    assert runner.song_delivery_ok(0, True, semantic_noise + ["SONG_PARTIAL"], proof) is False  # 没唱完整不切
    assert runner.song_delivery_ok(0, False, [], proof) is False             # 不是歌不切
    assert runner.song_delivery_ok(0, True, None, None) is False             # 没有正向证据必须失败关闭
    assert runner.song_delivery_ok(1, True, [], proof) is False               # 当前 selector 失败不得交付
    assert runner.song_delivery_ok(0, True, ["SONG_NOT_LIDOUSHA_SINGING"], proof) is False
    assert runner.song_delivery_ok(0, True, ["SONG_HOST_VOCAL_PROOF_INVALID"], proof) is False
    proof["host_vocal_status"] = "BLOCKED"
    assert runner.song_delivery_ok(0, True, [], proof) is False


def test_failed_song_selector_cannot_reuse_stale_summary_or_deliver(tmp_path, monkeypatch):
    """P1 regression: a previous valid selector summary used to survive in the
    fixed output directory.  When the next selector crashed, produce_song read
    that stale proof and copied the old video as this invocation's delivery.

    The old attempt remains on disk for forensics, while the failing current
    subprocess gets a distinct empty output directory.  Its own diagnostic
    summary is read, but its non-zero return code still prevents delivery.
    """
    date = "2026-07-09"
    cid = "song_stale_regression"
    base = tmp_path / "autoslice"
    repo = tmp_path / "repo"
    out_dir = base / "out" / date / cid
    logs_dir = base / "logs"
    out_dir.mkdir(parents=True)
    logs_dir.mkdir(parents=True)
    repo.mkdir()
    monkeypatch.setattr(runner, "BASE", base)
    monkeypatch.setattr(runner, "REPO_ROOT", repo)
    monkeypatch.setattr(runner, "child_env", lambda: {})
    monkeypatch.setattr(runner, "cpa_qa_cmd", lambda: "judge")

    segment = tmp_path / "segment.mp4"
    segment.write_bytes(b"source")
    anchor_start, anchor_end, duration = 50_000, 100_000, 200_000
    window_start = anchor_start - runner.SONG_WINDOW_PRE_MS
    window_end = anchor_end + runner.SONG_WINDOW_POST_MS
    runner.song_window_media_path(
        out_dir, cid, "", window_start, window_end
    ).write_bytes(b"current-window")

    stale_video = tmp_path / "stale.burned.mp4"
    stale_video.write_bytes(b"stale-but-valid")
    stale_sha = "sha256:" + hashlib.sha256(stale_video.read_bytes()).hexdigest()
    stale_dir = out_dir / "song_selector"
    stale_dir.mkdir()
    stale_summary = {
        "records": [{
            "candidate_id": "semanticsong_stale",
            "decision_action": "AUTO_UPLOAD",
            "reason_codes": [],
        }]
    }
    stale_summary_path = stale_dir / "summary.json"
    stale_summary_path.write_text(json.dumps(stale_summary), encoding="utf-8")

    def fake_slice_srt(_source, _start, _end, destination):
        destination.write_text("1\n00:00:00,000 --> 00:00:01,000\n歌词\n", encoding="utf-8")
        return 1

    monkeypatch.setattr(runner, "slice_srt", fake_slice_srt)
    monkeypatch.setattr(runner, "_song_core_span", lambda *_args: (anchor_start, anchor_end))

    def fake_completion(record):
        ready = bool(record)
        return {
            "ready": ready,
            "reason_codes": [] if ready else ["SONG_FULL_BOUNDARY_PROOF_MISSING"],
            "lyrics_alignment_status": "READY" if ready else None,
            "alignment_report_path": None,
            "alignment_report_sha256": None,
        }

    monkeypatch.setattr(runner, "song_completion_evidence", fake_completion)
    monkeypatch.setattr(
        runner,
        "song_delivery_artifacts",
        lambda record: {
            "video_path": str(stale_video),
            "video_sha256": stale_sha,
            "title": "【李豆沙】本次诊断结果" if record.get("candidate_id") == "semanticsong_current" else "【李豆沙】旧歌切",
        } if record else {},
    )

    selector_dirs = []

    class FailedSelector:
        returncode = 1

    def fake_run(command, **_kwargs):
        selector_dir = Path(command[command.index("--output-dir") + 1])
        selector_dirs.append(selector_dir)
        (selector_dir / "summary.json").write_text(json.dumps({
            "records": [{
                "candidate_id": "semanticsong_current",
                "decision_action": "AUTO_UPLOAD",
                "reason_codes": [],
            }]
        }), encoding="utf-8")
        return FailedSelector()

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    result = runner.produce_song(date, {
        "cid": cid,
        "segment_path": str(segment),
        "seg_dur_ms": duration,
        "anchor_start_ms": anchor_start,
        "anchor_end_ms": anchor_end,
        "danmaku": 18,
        "hook": "旧 summary 不能冒充本次结果",
        "preview": "",
    })

    assert result["start_ms"] == window_start and result["end_ms"] == window_end
    assert result["rc"] == 1 and result["status"] == "failed"
    assert result["window_classified_song"] is True
    assert result["title"] == "【李豆沙】本次诊断结果", "只能读本次新 summary，不能读旧标题"
    assert "delivered" not in result, "即使失败进程写了完整 summary，rc != 0 也不得交付"
    assert len(selector_dirs) == 1
    assert selector_dirs[0].parent == stale_dir and selector_dirs[0] != stale_dir
    assert (selector_dirs[0] / "summary.json").is_file()
    assert stale_summary_path.is_file(), "旧 attempt 只隔离保留，不做破坏性删除"
    assert json.loads(stale_summary_path.read_text(encoding="utf-8")) == stale_summary
    assert not list((repo / "lidousha" / date).glob("*.mp4"))


def test_full_song_proof_retry_seeds_original_anchor_and_enables_audio_lrc(tmp_path, monkeypatch):
    date = "2026-07-09"
    cid = "song_seed_retry"
    base = tmp_path / "autoslice"
    repo = tmp_path / "repo"
    out_dir = base / "out" / date / cid
    (base / "logs").mkdir(parents=True)
    out_dir.mkdir(parents=True)
    repo.mkdir()
    monkeypatch.setattr(runner, "BASE", base)
    monkeypatch.setattr(runner, "REPO_ROOT", repo)
    monkeypatch.setattr(runner, "child_env", lambda: {})
    monkeypatch.setattr(runner, "cpa_qa_cmd", lambda: "judge")

    segment = tmp_path / "segment.mp4"
    segment.write_bytes(b"segment")
    anchor_start, anchor_end, duration = 50_000, 100_000, 200_000
    tight_start = max(0, anchor_start - runner.SONG_WINDOW_PRE_MS)
    tight_end = min(duration, anchor_end + runner.SONG_WINDOW_POST_MS)
    full_start, full_end = runner.song_proof_retry_window(anchor_start, anchor_end, duration)
    runner.song_window_media_path(out_dir, cid, "", tight_start, tight_end).write_bytes(b"tight")
    runner.song_window_media_path(out_dir, cid, "_full", full_start, full_end).write_bytes(b"full")

    def fake_slice_srt(_source, _start, _end, destination):
        destination.write_text("1\n00:00:00,000 --> 00:00:01,000\n歌词\n", encoding="utf-8")
        return 1

    monkeypatch.setattr(runner, "slice_srt", fake_slice_srt)
    selector_commands = []

    class Completed:
        returncode = 0

    def fake_run(command, **_kwargs):
        selector_commands.append(command)
        selector_dir = Path(command[command.index("--output-dir") + 1])
        seeded = "--seed-song-candidate-id" in command
        full_source = "--agy-audio-lrc-align" in command
        (selector_dir / "summary.json").write_text(
            json.dumps(
                {
                    "records": [
                        {
                            "candidate_id": "seededsong_45000_95000" if seeded else "semanticsong_15000_65000",
                            "decision_action": "BLOCK",
                            "reason_codes": (
                                ["SONG_BACKGROUND_PLAYBACK_ONLY", "SONG_NOT_LIDOUSHA_SINGING"]
                                if full_source
                                else [
                                    "TIGHT_ATTEMPT_DIAGNOSTIC",
                                    "SONG_FULL_BOUNDARY_PROOF_MISSING",
                                ]
                            ),
                            "source_context_job": {
                                "content_type_hint": "song",
                                "song_candidate": True,
                                "requires_full_source_song_boundary_redo": True,
                            },
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        return Completed()

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    result = runner.produce_song(
        date,
        {
            "cid": cid,
            "segment_path": str(segment),
            "seg_dur_ms": duration,
            "anchor_start_ms": anchor_start,
            "anchor_end_ms": anchor_end,
            "danmaku": 18,
            "hook": "测试",
            "preview": "下播前演唱 yonige《芽吹くとき》",
        },
    )

    assert result["window_classified_song"] is True
    assert "full_source_retry" in result
    assert result["decision"] == "BLOCK"
    assert result["full_source_authoritative_block"] is True
    assert result["full_source_performer_rejection"] is True
    assert "SONG_BACKGROUND_PLAYBACK_ONLY" in result["reason_codes"]
    assert "SONG_NOT_LIDOUSHA_SINGING" in result["reason_codes"]
    assert "TIGHT_ATTEMPT_DIAGNOSTIC" not in result["reason_codes"]
    assert (
        "TIGHT_ATTEMPT_DIAGNOSTIC"
        in result["initial_attempt_reason_codes"]
    )
    assert len(selector_commands) == 2
    tight_command = selector_commands[0]
    full_command = selector_commands[1]
    assert "--seed-song-candidate-id" in tight_command
    assert tight_command[tight_command.index("--seed-song-candidate-id") + 1] == "seededsong_15000_65000"
    assert "--agy-audio-lrc-align" not in tight_command
    assert "--agy-audio-lrc-align" in full_command
    expected_full_seed = (
        f"seededsong_{anchor_start - full_start}_{anchor_end - full_start}"
    )
    assert (
        full_command[full_command.index("--seed-song-candidate-id") + 1]
        == expected_full_seed
    )
    assert full_command[full_command.index("--seed-song-anchor-start-ms") + 1] == str(
        anchor_start - full_start
    )
    assert full_command[full_command.index("--seed-song-anchor-end-ms") + 1] == str(
        anchor_end - full_start
    )
    assert full_command[full_command.index("--source-duration-ms") + 1] == str(
        full_end - full_start
    )
    query_indexes = [index for index, value in enumerate(full_command) if value == "--song-lrc-query"]
    assert [full_command[index + 1] for index in query_indexes] == [
        "芽吹くとき",
        "下播前演唱 yonige《芽吹くとき》",
    ]


def test_full_song_authoritative_retry_timeout_always_promotes_block(tmp_path, monkeypatch):
    date = "2026-07-09"
    cid = "song_timeout_retry"
    base = tmp_path / "autoslice"
    repo = tmp_path / "repo"
    out_dir = base / "out" / date / cid
    (base / "logs").mkdir(parents=True)
    out_dir.mkdir(parents=True)
    repo.mkdir()
    monkeypatch.setattr(runner, "BASE", base)
    monkeypatch.setattr(runner, "REPO_ROOT", repo)
    monkeypatch.setattr(runner, "child_env", lambda: {})
    monkeypatch.setattr(runner, "cpa_qa_cmd", lambda: "judge")
    monkeypatch.setattr(
        runner,
        "slice_srt",
        lambda _src, _start, _end, dest: (dest.write_text(
            "1\n00:00:00,000 --> 00:00:01,000\n歌词\n", encoding="utf-8"
        ) or 1),
    )

    segment = tmp_path / "segment.mp4"
    segment.write_bytes(b"segment")
    anchor_start, anchor_end, duration = 50_000, 100_000, 200_000
    tight_start = max(0, anchor_start - runner.SONG_WINDOW_PRE_MS)
    tight_end = min(duration, anchor_end + runner.SONG_WINDOW_POST_MS)
    full_start, full_end = runner.song_proof_retry_window(anchor_start, anchor_end, duration)
    runner.song_window_media_path(out_dir, cid, "", tight_start, tight_end).write_bytes(b"tight")
    runner.song_window_media_path(out_dir, cid, "_full", full_start, full_end).write_bytes(b"full")

    calls = 0

    class Completed:
        def __init__(self, returncode):
            self.returncode = returncode

    def fake_run(command, **_kwargs):
        nonlocal calls
        calls += 1
        selector_dir = Path(command[command.index("--output-dir") + 1])
        if calls == 1:
            (selector_dir / "summary.json").write_text(
                json.dumps(
                    {
                        "records": [
                            {
                                "candidate_id": "seededsong_15000_65000",
                                "decision_action": "BLOCK",
                                "reason_codes": ["SONG_FULL_BOUNDARY_PROOF_MISSING"],
                                "source_context_job": {
                                    "content_type_hint": "song",
                                    "song_candidate": True,
                                    "requires_full_source_song_boundary_redo": True,
                                },
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            return Completed(0)
        # Authoritative full-source attempt times out and writes no summary.
        return Completed(124)

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    result = runner.produce_song(
        date,
        {
            "cid": cid,
            "segment_path": str(segment),
            "seg_dur_ms": 200_000,
            "anchor_start_ms": anchor_start,
            "anchor_end_ms": anchor_end,
            "danmaku": 18,
            "hook": "测试",
            "preview": "《测试歌》",
        },
    )

    assert calls == 2
    assert result["decision"] == "BLOCK"
    assert result["song_complete"] is False
    assert result["full_source_authoritative_block"] is True
    assert result.get("full_source_performer_rejection") is not True
    assert result["full_source_retry"]["rc"] == 124
    assert "SONG_FULL_BOUNDARY_PROOF_MISSING" in result["reason_codes"]
    assert "delivered" not in result


@pytest.mark.parametrize(
    ("audio_provider", "paid_backup"),
    [("agy", False), ("gemini_api", False), ("gemini_api", True)],
)
def test_song_completion_evidence_is_hash_bound_and_requires_lrc_materialization(
    tmp_path, monkeypatch, audio_provider, paid_backup
):
    report = tmp_path / "song.lyrics-alignment-report.json"
    report_payload = {
        "schema_version": "lyrics-alignment-report.v1",
        "alignment_model": "external_lrc_global_shift.v1",
        "candidate_id": "song-proof",
        "provider": "lrclib",
        "song_title": "芽吹くとき",
        "source_ref": "https://lrclib.net/api/get/33542202",
        "offset_ms": 1_500,
        "nominal_lrc_zero_ms": 1_500,
        "first_lyric_start_ms": 1_500,
        "last_lyric_end_ms": 32_500,
        "line_count": 8,
        "matched_line_count": 8,
        "matched_line_ratio": 1.0,
        "lyric_lines": [{"lrc_time_ms": index * 4_000, "text": f"歌词{index}"} for index in range(8)],
        "alignment": [{"lrc_time_ms": index * 4_000, "matched_cue_id": f"cue-{index}"} for index in range(8)],
    }
    report.write_text(json.dumps(report_payload, ensure_ascii=False) + "\n", encoding="utf-8")
    source = tmp_path / "song.source.mp4"
    source.write_bytes(b"source-video")
    bind_ready_live_performance_report(
        report,
        source_media=source,
        candidate_id="song-proof",
        provider=audio_provider,
        paid_backup=paid_backup,
    )
    host_vocal_claim, host_vocal_profile = make_ready_host_vocal_claim(
        tmp_path / "host-vocal",
        source_media=source,
        alignment_report=report,
        candidate_id="song-proof",
    )
    monkeypatch.setattr(runner, "HOST_VOCAL_PROFILE", host_vocal_profile)
    report_sha = hashlib.sha256(report.read_bytes()).hexdigest()
    subtitle = tmp_path / "song.srt"
    subtitle.write_text("1\n00:00:01,500 --> 00:00:02,500\n歌词0\n", encoding="utf-8")
    subtitle_sha = "sha256:" + hashlib.sha256(subtitle.read_bytes()).hexdigest()
    recut_media = tmp_path / "song.recut.mp4"
    recut_media.write_bytes(b"recut-video")
    recut_media_sha = "sha256:" + hashlib.sha256(recut_media.read_bytes()).hexdigest()
    burned = tmp_path / "song.burned.mp4"
    burned.write_bytes(b"burned-video")
    burned_sha = "sha256:" + hashlib.sha256(burned.read_bytes()).hexdigest()
    ass = tmp_path / "song.final-sapphire72.ass"
    ass.write_text("[Script Info]\n", encoding="utf-8")
    ass_sha = "sha256:" + hashlib.sha256(ass.read_bytes()).hexdigest()
    source_path = str(source.resolve())
    source_sha = "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest()
    report_bound = json.loads(report.read_text(encoding="utf-8"))
    audio_artifacts = report_bound["audio_alignment_artifacts"]
    stream_contract = runner._expected_song_stream_contract()
    accurate_command = runner._expected_song_recut_command(
        source_path, str(recut_media.resolve()), 0, 33_500
    )
    burn_command = [
        "ffmpeg", "-i", str(recut_media.resolve()), "-vf", "subtitles=test.ass",
        "-map", "0:v:0", "-map", "0:a:0", "-sn", "-dn",
        "-map_metadata", "-1", "-map_chapters", "-1", str(burned.resolve()),
    ]
    proofs = {
        "lyrics_alignment_report_path": str(report.resolve()),
        "lyrics_alignment_report_sha256": report_sha,
        "host_vocal_proof_path": str(Path(host_vocal_claim["proof_path"]).resolve()),
        "host_vocal_proof_sha256": host_vocal_claim["proof_sha256"],
        "agy_run_manifest_path": str(Path(audio_artifacts["run_manifest_path"]).resolve()),
        "agy_run_manifest_sha256": audio_artifacts["run_manifest_sha256"],
        "post_song_anchor_start_ms": 33_500,
    }
    artifact_hashes = {
        "video_sha256": recut_media_sha,
        "subtitle_sha256": subtitle_sha,
        "burned_video_sha256": burned_sha,
        "ass_sha256": ass_sha,
    }
    burned_record = {
        "status": "BURNED",
        "path": str(burned.resolve()),
        "ass_path": str(ass.resolve()),
        "burned_sha256": burned_sha,
        "subtitle_style": "lidousha-final-sapphire72",
        "pillarbox_16_9": False,
        "command": burn_command,
        "stream_contract": stream_contract,
    }
    output_binding = {
        "schema_version": runner.VERIFIED_SONG_OUTPUT_BINDING_SCHEMA_VERSION,
        "candidate_id": "song-proof",
        "source": {"canonical_path": source_path, "sha256": source_sha},
        "interval": {"start_ms": 0, "end_ms": 33_500, "duration_ms": 33_500},
        "post_song_anchor_start_ms": 33_500,
        "proofs": proofs,
        "stream_contract": stream_contract,
        "recut_transform": {
            "schema_version": "song-recut-transform.v1",
            "method": "two_stage_seek_reencode",
            "start_ms": 0,
            "duration_ms": 33_500,
            "command": accurate_command,
            "video_codec": "libx264",
            "audio_codec": "aac",
        },
        "artifacts": {
            "recut_media_path": str(recut_media.resolve()),
            "recut_media_sha256": recut_media_sha,
            "subtitle_path": str(subtitle.resolve()),
            "subtitle_sha256": subtitle_sha,
            "burned_media_path": str(burned.resolve()),
            "burned_media_sha256": burned_sha,
            "ass_path": str(ass.resolve()),
            "ass_sha256": ass_sha,
        },
        "burn_transform": {
            "schema_version": "song-subtitle-burn-transform.v1",
            "command": burn_command,
            "subtitle_style": "lidousha-final-sapphire72",
            "pillarbox_16_9": False,
        },
    }
    recut_manifest = tmp_path / "song.recut.manifest.json"
    manifest_payload = {
        "schema_version": runner.MATERIALIZED_RECUT_SCHEMA_VERSION,
        "status": "MATERIALIZED",
        "reason_codes": [],
        "candidate_id": "song-proof",
        "source_video_path": source_path,
        "source_binding": output_binding["source"],
        "requested_range": output_binding["interval"],
        "media_path": str(recut_media.resolve()),
        "subtitle_path": str(subtitle.resolve()),
        "subtitle_source": "external_lrc_global_shift",
        "lyric_offset_ms": 1_500,
        "accurate_command": accurate_command,
        "stream_contract": stream_contract,
        "recut_transform": output_binding["recut_transform"],
        "song_output_proof_binding": proofs,
        "verified_output_binding": output_binding,
        "artifact_hashes": artifact_hashes,
        "burned_preview": burned_record,
        "accurate_rerender_used": True,
    }
    recut_manifest.write_text(json.dumps(manifest_payload, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    recut_manifest_sha = "sha256:" + hashlib.sha256(recut_manifest.read_bytes()).hexdigest()
    monkeypatch.setattr(runner, "_has_exact_av_streams", lambda _path: True)
    record = {
        "candidate_id": "song-proof",
        "source_context_job": {
            "song_boundary": {
                "status": "FULL_SONG_READY",
                "song_title": "芽吹くとき",
                "clip_start_ms": 0,
                "nominal_lrc_zero_ms": 1_500,
                "first_lyric_start_ms": 1_500,
                "last_lyric_end_ms": 32_500,
                "clip_end_ms": 33_500,
                "completion_basis": "FULL_STUDIO_SEQUENCE",
            },
            "lyrics_alignment": {
                "status": "READY",
                "provider": "lrclib",
                "model": "lrclib-agy-audio-lrc-global-shift-v1",
                "source": "song_repair.lrclib",
                "external_lrc": "https://lrclib.net/api/get/33542202",
                "offset_ms": 1_500,
                "nominal_lrc_zero_ms": 1_500,
                "matched_line_ratio": 1.0,
                "alignment_report_path": str(report),
                "alignment_report_sha256": report_sha,
                "source_media_path": source_path,
                "source_media_sha256": source_sha,
                "completion_basis": "FULL_STUDIO_SEQUENCE",
            },
            "host_vocal_proof": host_vocal_claim,
        },
        "materialized_recut": {
            "status": "MATERIALIZED",
            "reason_codes": [],
            "candidate_id": "song-proof",
            "start_ms": 0,
            "end_ms": 33_500,
            "duration_ms": 33_500,
            "source_video_path": source_path,
            "source_binding": output_binding["source"],
            "media_path": str(recut_media.resolve()),
            "subtitle_source": "external_lrc_global_shift",
            "accurate_rerender_used": True,
            "render_qa": {
                "code": "ACTUAL_CUT_ERROR_OK",
                "pass": True,
                "severity": "PASS",
                "evidence": {"actual_cut_error_ms": 0, "threshold_ms": 100},
            },
            "subtitle_path": str(subtitle),
            "lyric_offset_ms": 1_500,
            "manifest_path": str(recut_manifest),
            "manifest_sha256": recut_manifest_sha,
            "accurate_command": accurate_command,
            "stream_contract": stream_contract,
            "recut_transform": output_binding["recut_transform"],
            "song_output_proof_binding": proofs,
            "verified_output_binding": output_binding,
            "burned_preview": burned_record,
            "artifact_hashes": artifact_hashes,
        },
    }
    # Publish staging may append cover hashes after the recut manifest has
    # already sealed the media/subtitle/burn artifacts; that extension is not
    # a recut-manifest mismatch.
    record["materialized_recut"]["artifact_hashes"]["cover_sha256"] = "sha256:" + "0" * 64
    evidence = runner.song_completion_evidence(record)
    assert evidence["ready"] is True
    assert evidence["reason_codes"] == []

    if audio_provider == "gemini_api":
        provider_bound_report = json.loads(report.read_text(encoding="utf-8"))
        mismatched_provider_report = dict(provider_bound_report)
        mismatched_provider_report["audio_alignment_provider"] = "agy"
        report.write_text(json.dumps(mismatched_provider_report, ensure_ascii=False) + "\n", encoding="utf-8")
        record["source_context_job"]["lyrics_alignment"]["alignment_report_sha256"] = hashlib.sha256(
            report.read_bytes()
        ).hexdigest()
        evidence = runner.song_completion_evidence(record)
        assert evidence["ready"] is False
        assert "SONG_AUDIO_LRC_PROVIDER_INVALID" in evidence["reason_codes"]
        report.write_text(json.dumps(provider_bound_report, ensure_ascii=False) + "\n", encoding="utf-8")
        record["source_context_job"]["lyrics_alignment"]["alignment_report_sha256"] = hashlib.sha256(
            report.read_bytes()
        ).hexdigest()

    original_source_bytes = source.read_bytes()
    source.write_bytes(b"tampered-source-video")
    evidence = runner.song_completion_evidence(record)
    assert evidence["ready"] is False
    assert "SONG_RECUT_SOURCE_BINDING_INVALID" in evidence["reason_codes"]
    source.write_bytes(original_source_bytes)

    saved_binding = json.loads(json.dumps(record["materialized_recut"]["verified_output_binding"]))
    record["materialized_recut"]["verified_output_binding"]["source"]["canonical_path"] = str(
        (tmp_path / "different-source.mp4").resolve()
    )
    evidence = runner.song_completion_evidence(record)
    assert evidence["ready"] is False
    assert "SONG_RECUT_SOURCE_BINDING_INVALID" in evidence["reason_codes"]
    record["materialized_recut"]["verified_output_binding"] = saved_binding

    record["materialized_recut"]["end_ms"] = 33_499
    evidence = runner.song_completion_evidence(record)
    assert evidence["ready"] is False
    assert "SONG_RECUT_INTERVAL_BINDING_INVALID" in evidence["reason_codes"]
    record["materialized_recut"]["end_ms"] = 33_500

    saved_contract = record["materialized_recut"]["stream_contract"]
    record["materialized_recut"]["stream_contract"] = {
        **saved_contract,
        "ffmpeg_maps": ["0:v:0", "0:a:1"],
    }
    evidence = runner.song_completion_evidence(record)
    assert evidence["ready"] is False
    assert "SONG_RECUT_STREAM_CONTRACT_INVALID" in evidence["reason_codes"]
    record["materialized_recut"]["stream_contract"] = saved_contract

    original_manifest_bytes = recut_manifest.read_bytes()
    original_manifest_sha = record["materialized_recut"]["manifest_sha256"]
    self_consistent_tamper = json.loads(original_manifest_bytes)
    self_consistent_tamper["accurate_command"] = [*accurate_command[:-1], "tampered-output.mp4"]
    recut_manifest.write_text(
        json.dumps(self_consistent_tamper, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    record["materialized_recut"]["manifest_sha256"] = (
        "sha256:" + hashlib.sha256(recut_manifest.read_bytes()).hexdigest()
    )
    evidence = runner.song_completion_evidence(record)
    assert evidence["ready"] is False
    assert "SONG_RECUT_MANIFEST_CONTENT_INVALID" in evidence["reason_codes"]
    recut_manifest.write_bytes(original_manifest_bytes)
    record["materialized_recut"]["manifest_sha256"] = original_manifest_sha

    recut_manifest.write_text("{}\n", encoding="utf-8")
    evidence = runner.song_completion_evidence(record)
    assert evidence["ready"] is False
    assert "SONG_RECUT_MANIFEST_HASH_INVALID" in evidence["reason_codes"]
    recut_manifest.write_bytes(original_manifest_bytes)

    # Regression for the 2026-07-09 《芽吹くとき》 false positive: even a
    # perfect 8/8 LRC/global-shift proof cannot stand in for 李豆沙 singing.
    saved_host_claim = record["source_context_job"].pop("host_vocal_proof")
    evidence = runner.song_completion_evidence(record)
    assert evidence["ready"] is False
    assert "SONG_HOST_VOCAL_UNPROVEN" in evidence["reason_codes"]
    record["source_context_job"]["host_vocal_proof"] = saved_host_claim

    # The model name and report evidence type are a two-way binding.  Removing
    # evidence_source from an audio report must not downgrade it to the looser
    # legacy-text proof gate.
    ready_report_payload = json.loads(report.read_text(encoding="utf-8"))
    missing_type_payload = dict(ready_report_payload)
    missing_type_payload.pop("evidence_source", None)
    report.write_text(json.dumps(missing_type_payload, ensure_ascii=False) + "\n", encoding="utf-8")
    record["source_context_job"]["lyrics_alignment"]["alignment_report_sha256"] = hashlib.sha256(report.read_bytes()).hexdigest()
    evidence = runner.song_completion_evidence(record)
    assert evidence["ready"] is False
    assert "SONG_ALIGNMENT_EVIDENCE_TYPE_MISMATCH" in evidence["reason_codes"]
    assert evidence["live_performance_status"] is None
    assert evidence["joint_singing_decision"] is None
    report.write_text(json.dumps(ready_report_payload, ensure_ascii=False) + "\n", encoding="utf-8")
    record["source_context_job"]["lyrics_alignment"]["alignment_report_sha256"] = hashlib.sha256(report.read_bytes()).hexdigest()

    # An audio-derived report cannot rely on its own hash alone: the final
    # runner edge also requires the current audio/LRC/prompt/raw-output/run
    # manifest bindings and an attested High-model no-fallback run.
    report_payload["evidence_source"] = "agy_audio_lrc"
    report.write_text(json.dumps(report_payload, ensure_ascii=False) + "\n", encoding="utf-8")
    record["source_context_job"]["lyrics_alignment"]["alignment_report_sha256"] = hashlib.sha256(report.read_bytes()).hexdigest()
    evidence = runner.song_completion_evidence(record)
    assert evidence["ready"] is False
    assert "SONG_AUDIO_LRC_ARTIFACTS_INVALID" in evidence["reason_codes"]
    assert evidence["live_performance_status"] is None
    assert evidence["joint_singing_decision"] is None
    report_payload.pop("evidence_source")
    report.write_text(json.dumps(report_payload, ensure_ascii=False) + "\n", encoding="utf-8")
    record["source_context_job"]["lyrics_alignment"]["alignment_report_sha256"] = hashlib.sha256(report.read_bytes()).hexdigest()

    # The same negative nominal zero in all three documents is still invalid:
    # equality cannot make unavailable pre-source music become complete.
    report_payload["nominal_lrc_zero_ms"] = -1
    report.write_text(json.dumps(report_payload, ensure_ascii=False) + "\n", encoding="utf-8")
    record["source_context_job"]["song_boundary"]["nominal_lrc_zero_ms"] = -1
    record["source_context_job"]["lyrics_alignment"]["nominal_lrc_zero_ms"] = -1
    record["source_context_job"]["lyrics_alignment"]["alignment_report_sha256"] = hashlib.sha256(report.read_bytes()).hexdigest()
    evidence = runner.song_completion_evidence(record)
    assert evidence["ready"] is False
    assert "SONG_NOMINAL_LRC_ZERO_INVALID" in evidence["reason_codes"]

    # A rehashed report from a different nominal-zero calculation must not be
    # mixed with the current boundary/materialized recut.
    report_payload["nominal_lrc_zero_ms"] = 1_501
    report.write_text(json.dumps(report_payload, ensure_ascii=False) + "\n", encoding="utf-8")
    record["source_context_job"]["song_boundary"]["nominal_lrc_zero_ms"] = 1_500
    record["source_context_job"]["lyrics_alignment"]["nominal_lrc_zero_ms"] = 1_500
    record["source_context_job"]["lyrics_alignment"]["alignment_report_sha256"] = hashlib.sha256(report.read_bytes()).hexdigest()
    evidence = runner.song_completion_evidence(record)
    assert evidence["ready"] is False
    assert "SONG_NOMINAL_LRC_ZERO_MISMATCH" in evidence["reason_codes"]

    report_payload["nominal_lrc_zero_ms"] = 1_500
    report.write_text(json.dumps(report_payload, ensure_ascii=False) + "\n", encoding="utf-8")
    record["source_context_job"]["lyrics_alignment"]["alignment_report_sha256"] = hashlib.sha256(report.read_bytes()).hexdigest()

    record["materialized_recut"]["accurate_rerender_used"] = False
    evidence = runner.song_completion_evidence(record)
    assert evidence["ready"] is False
    assert "SONG_ACCURATE_RERENDER_REQUIRED" in evidence["reason_codes"]
    record["materialized_recut"]["accurate_rerender_used"] = True

    record["materialized_recut"]["reason_codes"] = ["FFMPEG_ACCURATE_RECUT_FAILED"]
    evidence = runner.song_completion_evidence(record)
    assert evidence["ready"] is False
    assert "SONG_ACCURATE_RERENDER_FAILED" in evidence["reason_codes"]
    record["materialized_recut"]["reason_codes"] = []

    record["materialized_recut"]["render_qa"]["pass"] = False
    evidence = runner.song_completion_evidence(record)
    assert evidence["ready"] is False
    assert "SONG_RENDER_QA_FAILED" in evidence["reason_codes"]
    record["materialized_recut"]["render_qa"]["pass"] = True

    # A self-consistent hash over an empty report is not semantic proof.
    report.write_text("{}\n", encoding="utf-8")
    record["source_context_job"]["lyrics_alignment"]["alignment_report_sha256"] = hashlib.sha256(report.read_bytes()).hexdigest()
    evidence = runner.song_completion_evidence(record)
    assert evidence["ready"] is False
    assert "SONG_ALIGNMENT_REPORT_SCHEMA_INVALID" in evidence["reason_codes"]

    report.write_text(json.dumps(report_payload, ensure_ascii=False) + "\n", encoding="utf-8")
    record["source_context_job"]["lyrics_alignment"]["alignment_report_sha256"] = report_sha
    report.write_text("tampered\n", encoding="utf-8")
    evidence = runner.song_completion_evidence(record)
    assert evidence["ready"] is False
    assert "SONG_ALIGNMENT_REPORT_HASH_INVALID" in evidence["reason_codes"]

    report.write_text(json.dumps(report_payload, ensure_ascii=False) + "\n", encoding="utf-8")
    record["materialized_recut"]["subtitle_source"] = "asr_cues"
    evidence = runner.song_completion_evidence(record)
    assert evidence["ready"] is False
    assert "SONG_EXTERNAL_LRC_SUBTITLE_NOT_MATERIALIZED" in evidence["reason_codes"]

    record["materialized_recut"]["subtitle_source"] = "external_lrc_global_shift"
    burned.write_bytes(b"stale-video")
    evidence = runner.song_completion_evidence(record)
    assert evidence["ready"] is False
    assert "SONG_BURNED_PREVIEW_HASH_INVALID" in evidence["reason_codes"]


def test_verified_song_fallback_title_is_fixed_catalog_form():
    # Ivan 2026-07-14/19 铁律：歌切标题就是「【李豆沙】豆沙歌，《歌名》」，
    # hook 永远被忽略，《歌名》后不加任何字。
    assert runner.verified_song_fallback_title("芽吹くとき", "下播前的温柔哄睡小歌，唱完刷晚安") == (
        "【李豆沙】豆沙歌，《芽吹くとき》"
    )
    assert runner.verified_song_fallback_title(
        "想和你迎着台风去看海", "台风天唱甜甜的《和你迎着台风去看海》"
    ) == "【李豆沙】豆沙歌，《想和你迎着台风去看海》"
    assert runner.verified_song_fallback_title("暖暖") == "【李豆沙】豆沙歌，《暖暖》"
    assert runner.verified_song_fallback_title(None) is None


def test_song_proof_retry_padding_exceeds_recall_padding():
    assert runner.SONG_PROOF_RETRY_PRE_MS > runner.SONG_WINDOW_PRE_MS
    assert runner.SONG_PROOF_RETRY_POST_MS > runner.SONG_WINDOW_POST_MS
    assert runner.song_proof_retry_window(166_220, 321_760, 483_352) == (46_220, 483_352)
    assert runner.song_proof_retry_window(10_000, 90_000, 100_000) == (0, 100_000)
    # Regression: the 2026-07-16 《CRYING FOR YOU》 anchor covered only the
    # first verse.  Full proof must include the rest of a seven-minute song,
    # rather than expanding the same incomplete excerpt by just 45 seconds.
    assert runner.song_proof_retry_window(208_540, 311_810, 639_473) == (
        88_540,
        639_473,
    )


def test_song_artifact_hash_check_rejects_mutation_and_symlink(tmp_path):
    artifact = tmp_path / "current.mp4"
    artifact.write_bytes(b"current")
    digest = "sha256:" + hashlib.sha256(b"current").hexdigest()
    assert runner._matches_sha256(artifact, digest)
    artifact.write_bytes(b"mutated")
    assert not runner._matches_sha256(artifact, digest)
    link = tmp_path / "link.mp4"
    link.symlink_to(artifact)
    assert not runner._matches_sha256(link, "sha256:" + hashlib.sha256(b"mutated").hexdigest())


def _verified_delivery_specs(tmp_path):
    sources = tmp_path / "materialized"
    delivery = tmp_path / "delivery"
    sources.mkdir()
    payloads = {
        "video": b"verified-burned-video",
        "cover": b"verified-cover",
        "subtitle": b"1\n00:00:00,000 --> 00:00:01,000\nlyric\n",
        "lyrics_alignment_report": b'{"status":"READY"}\n',
        "host_vocal_proof": b'{"decision":"LIDOUSHA_VOCAL_PRESENT_ON_LYRIC_CHECKPOINTS"}\n',
        "recut_manifest": b'{"status":"MATERIALIZED"}\n',
    }
    suffixes = {
        "video": ".mp4",
        "cover": ".cover.png",
        "subtitle": ".srt",
        "lyrics_alignment_report": ".lyrics-alignment-report.json",
        "host_vocal_proof": ".host-vocal-proof.json",
        "recut_manifest": ".recut.manifest.json",
    }
    specs = {}
    for role, payload in payloads.items():
        source = sources / f"source{suffixes[role]}"
        source.write_bytes(payload)
        destination = delivery / f"song{suffixes[role]}"
        specs[role] = (
            source,
            destination,
            "sha256:" + hashlib.sha256(payload).hexdigest(),
        )
    return delivery, payloads, specs


def test_atomic_verified_song_delivery_records_final_hashes_and_no_upload_manifest(tmp_path):
    delivery, payloads, specs = _verified_delivery_specs(tmp_path)
    manifest = delivery / "song.delivery.manifest.json"

    receipt = runner._atomic_verified_song_delivery(
        candidate_id="song_delivery_positive",
        manifest_path=manifest,
        artifact_specs=specs,
    )

    assert receipt["upload_enabled"] is False
    assert receipt["manifest_path"] == str(manifest.resolve())
    assert runner._matches_sha256(manifest, receipt["manifest_sha256"])
    manifest_payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert manifest_payload["schema_version"] == runner.VERIFIED_SONG_DELIVERY_SCHEMA_VERSION
    assert manifest_payload["status"] == "DELIVERED_NO_UPLOAD"
    assert manifest_payload["upload_enabled"] is False
    assert manifest_payload["candidate_id"] == "song_delivery_positive"
    assert set(manifest_payload["artifacts"]) == set(payloads)
    for role, payload in payloads.items():
        delivered = specs[role][1]
        expected = "sha256:" + hashlib.sha256(payload).hexdigest()
        assert delivered.read_bytes() == payload
        assert receipt["artifacts"][role]["path"] == str(delivered.resolve())
        assert receipt["artifacts"][role]["sha256"] == expected
        assert manifest_payload["artifacts"][role]["sha256"] == expected
        assert runner._matches_sha256(delivered, expected)
    assert not [path for path in delivery.iterdir() if path.name.startswith(".")]


def test_atomic_verified_song_delivery_rejects_sidecar_copy_hash_mismatch(tmp_path):
    delivery, _payloads, specs = _verified_delivery_specs(tmp_path)
    subtitle_source, subtitle_destination, _subtitle_hash = specs["subtitle"]
    specs["subtitle"] = (subtitle_source, subtitle_destination, "sha256:" + "0" * 64)

    with pytest.raises(runner.SongDeliveryError, match="copied hash mismatch"):
        runner._atomic_verified_song_delivery(
            candidate_id="song_sidecar_mismatch",
            manifest_path=delivery / "song.delivery.manifest.json",
            artifact_specs=specs,
        )

    assert list(delivery.iterdir()) == [], "a sidecar mismatch must expose no partial package"


def test_atomic_verified_song_delivery_removes_unverified_optional_sidecar(tmp_path):
    delivery, _payloads, specs = _verified_delivery_specs(tmp_path)
    delivery.mkdir()
    stale_cover = specs["cover"][1]
    stale_cover.write_bytes(b"stale-cover-from-previous-attempt")
    specs.pop("cover")
    manifest = delivery / "song.delivery.manifest.json"

    runner._atomic_verified_song_delivery(
        candidate_id="song_without_verified_cover",
        manifest_path=manifest,
        artifact_specs=specs,
        absent_artifacts={"cover": stale_cover},
    )

    assert not stale_cover.exists(), "an unverified old cover must not survive beside the new video"
    manifest_payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert manifest_payload["absent_artifacts"] == {
        "cover": {"path": str(stale_cover.absolute()), "status": "ABSENT"}
    }


def test_atomic_verified_song_delivery_detects_post_replace_corruption_and_rolls_back(tmp_path, monkeypatch):
    delivery, _payloads, specs = _verified_delivery_specs(tmp_path)
    manifest = delivery / "song.delivery.manifest.json"
    video_destination = specs["video"][1]
    real_replace = runner.os.replace

    def replace_then_corrupt(source, destination):
        real_replace(source, destination)
        if Path(destination) == video_destination:
            video_destination.write_bytes(b"corrupted-after-atomic-replace")

    monkeypatch.setattr(runner.os, "replace", replace_then_corrupt)
    with pytest.raises(runner.SongDeliveryError, match="final delivery hash mismatch"):
        runner._atomic_verified_song_delivery(
            candidate_id="song_post_copy_corruption",
            manifest_path=manifest,
            artifact_specs=specs,
        )

    assert list(delivery.iterdir()) == [], "post-copy corruption must revoke video and every sidecar"


def test_atomic_verified_song_delivery_manifest_replace_failure_exposes_no_partial(tmp_path, monkeypatch):
    delivery, _payloads, specs = _verified_delivery_specs(tmp_path)
    manifest = delivery / "song.delivery.manifest.json"
    real_replace = runner.os.replace

    def interrupted_replace(source, destination):
        real_replace(source, destination)
        if Path(destination) == manifest:
            raise OSError("simulated interruption after manifest replace")

    monkeypatch.setattr(runner.os, "replace", interrupted_replace)
    with pytest.raises(runner.SongDeliveryError, match="simulated interruption after manifest replace"):
        runner._atomic_verified_song_delivery(
            candidate_id="song_interrupted_commit",
            manifest_path=manifest,
            artifact_specs=specs,
        )

    assert list(delivery.iterdir()) == [], "manifest-last failure must roll back all public artifact names"


def _specs_with_basename(tmp_path, candidate_id, payload_prefix):
    tmp_path.mkdir(parents=True, exist_ok=True)
    delivery, payloads, specs = _verified_delivery_specs(tmp_path)
    basename = runner._song_delivery_basename("【李豆沙】豆沙歌，同一个很长的标题", candidate_id)
    renamed = {}
    for role, (source, _destination, expected) in specs.items():
        payload = payload_prefix + payloads[role]
        source.write_bytes(payload)
        suffix = {
            "video": ".mp4",
            "cover": ".cover.png",
            "subtitle": ".srt",
            "lyrics_alignment_report": ".lyrics-alignment-report.json",
            "host_vocal_proof": ".host-vocal-proof.json",
            "recut_manifest": ".recut.manifest.json",
        }[role]
        renamed[role] = (
            source,
            delivery / f"{basename}{suffix}",
            "sha256:" + hashlib.sha256(payload).hexdigest(),
        )
    return delivery, basename, renamed


def test_same_title_different_song_candidates_have_distinct_sequential_deliveries(tmp_path):
    _delivery_a, basename_a, specs_a = _specs_with_basename(tmp_path / "a", "song_candidate_a", b"a-")
    _delivery_b, basename_b, specs_b = _specs_with_basename(tmp_path / "b", "song_candidate_b", b"b-")
    shared = tmp_path / "delivery"
    shared.mkdir()
    specs_a = {role: (source, shared / destination.name, sha) for role, (source, destination, sha) in specs_a.items()}
    specs_b = {role: (source, shared / destination.name, sha) for role, (source, destination, sha) in specs_b.items()}

    receipt_a = runner._atomic_verified_song_delivery(
        candidate_id="song_candidate_a",
        manifest_path=shared / f"{basename_a}.delivery.manifest.json",
        artifact_specs=specs_a,
    )
    receipt_b = runner._atomic_verified_song_delivery(
        candidate_id="song_candidate_b",
        manifest_path=shared / f"{basename_b}.delivery.manifest.json",
        artifact_specs=specs_b,
    )

    assert basename_a != basename_b
    assert receipt_a["manifest_path"] != receipt_b["manifest_path"]
    assert Path(receipt_a["artifacts"]["video"]["path"]).is_file()
    assert Path(receipt_b["artifacts"]["video"]["path"]).is_file()
    assert runner._matches_sha256(
        Path(receipt_a["artifacts"]["video"]["path"]), receipt_a["artifacts"]["video"]["sha256"]
    )
    assert runner._matches_sha256(
        Path(receipt_b["artifacts"]["video"]["path"]), receipt_b["artifacts"]["video"]["sha256"]
    )


def test_same_title_different_song_candidates_have_distinct_concurrent_deliveries(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    _delivery_a, basename_a, specs_a = _specs_with_basename(tmp_path / "a", "song_parallel_a", b"a-")
    _delivery_b, basename_b, specs_b = _specs_with_basename(tmp_path / "b", "song_parallel_b", b"b-")
    shared = tmp_path / "delivery"
    shared.mkdir()
    specs_a = {role: (source, shared / destination.name, sha) for role, (source, destination, sha) in specs_a.items()}
    specs_b = {role: (source, shared / destination.name, sha) for role, (source, destination, sha) in specs_b.items()}

    def deliver(candidate_id, basename, specs):
        return runner._atomic_verified_song_delivery(
            candidate_id=candidate_id,
            manifest_path=shared / f"{basename}.delivery.manifest.json",
            artifact_specs=specs,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(deliver, "song_parallel_a", basename_a, specs_a),
            pool.submit(deliver, "song_parallel_b", basename_b, specs_b),
        ]
        receipts = [future.result() for future in futures]

    assert basename_a != basename_b
    assert len({receipt["manifest_path"] for receipt in receipts}) == 2
    for receipt in receipts:
        video = Path(receipt["artifacts"]["video"]["path"])
        assert runner._matches_sha256(video, receipt["artifacts"]["video"]["sha256"])


@pytest.mark.parametrize("candidate_id", ["", "../escape", "id with spaces", "x" * 97])
def test_song_delivery_basename_rejects_unsafe_candidate_ids(candidate_id):
    with pytest.raises(runner.SongDeliveryError, match="unsafe song delivery candidate id"):
        runner._song_delivery_basename("same title", candidate_id)


def test_blocked_songs_do_not_consume_budget_and_backlog_backfills():
    blocked = {"status": "blocked", "candidate_id": "song_a"}
    state = {
        "picks": [], "songs": [blocked], "pending_song": [],
        "song_backlog": [
            {"segment_path": "/rec/s.mp4", "anchor_start_ms": 0, "anchor_end_ms": 60_000,
             "danmaku": 50, "cid": "song_b"},
            {"segment_path": "/rec/s.mp4", "anchor_start_ms": 100_000, "anchor_end_ms": 160_000,
             "danmaku": 40, "cid": "song_c"},
        ],
    }
    runner.refill_songs(state)
    # 一次 BLOCK 不消耗交付配额 → 唯一席位由弹幕最高的备份回填。
    assert [s["cid"] for s in state["pending_song"]] == ["song_b"]
    assert [s["cid"] for s in state["song_backlog"]] == ["song_c"]


def test_song_attempt_cap_bounds_backfill():
    state = {
        "picks": [], "songs": [{"status": "blocked"}] * runner.SONG_ATTEMPT_CAP,
        "pending_song": [],
        "song_backlog": [{"segment_path": "/rec/s.mp4", "anchor_start_ms": 0,
                          "anchor_end_ms": 1, "danmaku": 9, "cid": "x"}],
    }
    runner.refill_songs(state)
    assert state["pending_song"] == [], "攻击面：回填必须有硬上限，不能无限产歌"


def test_song_delivery_budget_counts_deliveries_and_verified_commit_reservations():
    state = {
        "songs": [
            {"status": "review_ready", "delivered": "/x.mp4"},
            {"status": "blocked"},
            {"status": "blocked", "verified_delivery_pending_commit": True},
        ]
    }
    assert runner.song_delivery_budget(state) == 0


def test_legacy_string_backlog_tolerated():
    state = {"picks": [], "songs": [], "pending_song": [],
             "song_backlog": ["旧版字符串条目 (超出上限)"]}
    runner.refill_songs(state)
    assert state["pending_song"] == []
    assert state["song_backlog"] == ["旧版字符串条目 (超出上限)"]


def test_write_state_atomic_and_read_state_corruption(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "BASE", tmp_path)
    runner.write_state("2026-07-09", {"status": "processing", "n": 1})
    runner.write_state("2026-07-09", {"status": "review_ready", "n": 2})
    assert runner.read_state("2026-07-09")["n"] == 2
    # 主文件损坏 → 隔离留证 + 从 .bak（上一次好写入）恢复并持久化
    runner.state_path("2026-07-09").write_text("{broken", encoding="utf-8")
    st = runner.read_state("2026-07-09")
    assert st.get("n") == 1 and st.get("state_restored_from_bak")
    assert list((tmp_path / "state").glob("*.corrupt-*"))
    assert runner.read_state("2026-07-09").get("n") == 1  # 恢复已持久化，读取幂等
    # 损坏且无 .bak → 永久 BLOCK，绝不当"全新日期"从头重产重交付
    p2 = runner.state_path("2026-07-08")
    p2.parent.mkdir(parents=True, exist_ok=True)
    p2.write_text("{broken", encoding="utf-8")
    assert runner.read_state("2026-07-08")["status"] == "state_corrupt_blocked"
    assert runner.read_state("2026-07-08")["status"] == "state_corrupt_blocked"  # 幂等
    # 真正缺失 → 全新日期
    assert runner.read_state("2026-01-01") == {}


def test_source_health_error_detects_missing_root(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "REC_ROOT", tmp_path / "nope")
    assert runner.source_health_error() is not None
    monkeypatch.setattr(runner, "REC_ROOT", tmp_path)
    assert runner.source_health_error() is None


def test_tick_source_unavailable_is_loud_and_fail_closed(tmp_path, monkeypatch):
    """7/9 P0 实况：挂载死了，心跳还写 live=False dates=(none) 假绿。"""
    monkeypatch.setattr(runner, "BASE", tmp_path)
    monkeypatch.setattr(runner, "cjk_font_present", lambda: True)
    monkeypatch.setattr(runner, "source_health_error", lambda: "Transport endpoint is not connected")
    touched = []
    monkeypatch.setattr(runner, "recorder_live_status", lambda: touched.append(1) or False)
    assert runner.tick() == 0
    heartbeat = (tmp_path / "reports" / "heartbeat.txt").read_text(encoding="utf-8")
    assert "SOURCE_UNAVAILABLE" in heartbeat
    alert = (tmp_path / "reports" / "ALERT_SOURCE_UNAVAILABLE.txt").read_text(encoding="utf-8")
    assert "Transport endpoint" in alert
    assert not touched, "源不可用时什么都不该跑（fail-closed）"


def test_live_hold_ignored_only_for_closed_date_isolated_base(tmp_path, monkeypatch):
    """Ivan 2026-07-13：直播中临时放行 7/10 隔离回填。豁免开关只对
    只挂历史日期的 BASE 生效；能看到今天目录的（=生产形态）即使误设也冻结。"""
    monkeypatch.setattr(runner, "REC_ROOT", tmp_path)
    (tmp_path / "2026-07-10").mkdir()
    # 未设开关：live=True / None 都冻结
    assert runner._live_hold_active(True) is True
    assert runner._live_hold_active(None) is True
    assert runner._live_hold_active(False) is False
    # 设开关 + 只有历史日期：放行
    monkeypatch.setenv("AUTOSLICE_IGNORE_LIVE_HOLD", "1")
    assert runner._live_hold_active(True) is False
    assert runner._live_hold_active(None) is False
    # 能看到今天（UTC 或北京日）→ 拒绝豁免
    import time as _t
    today_cst = _t.strftime("%Y-%m-%d", _t.gmtime(_t.time() + 8 * 3600))
    (tmp_path / today_cst).mkdir()
    assert runner._live_hold_active(True) is True


def test_recorder_live_status_is_freshness_bound_and_holds_during_finalize(
    tmp_path, monkeypatch
):
    status_path = tmp_path / "status.json"
    monkeypatch.setattr(runner, "RECORDER_STATUS_PATH", status_path)
    monkeypatch.setattr(runner, "RECORDER_STATUS_MAX_AGE_SECONDS", 180)
    monkeypatch.setattr(runner.time, "time", lambda: 1_800_000_000.0)

    base = {
        "schema_version": "recorder-neutral-status.v1",
        "backend": "BililiveRecorder",
        "generated_at_epoch": 1_799_999_950.0,
        "room_id": runner.ROOM,
        "service_reachable": True,
        "error": None,
        "streaming": False,
        "recording": False,
        "finalizing": False,
        "live_status": 0,
    }
    status_path.write_text(json.dumps(base), encoding="utf-8")
    assert runner.recorder_live_status() is False

    status_path.write_text(json.dumps({**base, "finalizing": True}), encoding="utf-8")
    assert runner.recorder_live_status() is True

    status_path.write_text(
        json.dumps({**base, "generated_at_epoch": 1_799_999_000.0}),
        encoding="utf-8",
    )
    assert runner.recorder_live_status() is None

    status_path.write_text(
        json.dumps({**base, "service_reachable": False, "live_status": None}),
        encoding="utf-8",
    )
    assert runner.recorder_live_status() is None


def test_session_sealed_requires_stable_inventory(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "REC_ROOT", tmp_path)
    date_dir = tmp_path / "2026-07-09"
    date_dir.mkdir()
    seg = date_dir / f"{runner.ROOM}_2026-07-09-19-30-00-.mp4"
    seg.write_bytes(b"x" * 10)
    state = {}
    assert not runner.session_sealed("2026-07-09", state), "首见清单不算稳定"
    assert runner.session_sealed("2026-07-09", state), "跨 tick 未变 → 封场"
    seg.write_bytes(b"x" * 20)  # 录制还在写
    assert not runner.session_sealed("2026-07-09", state)
    assert runner.session_sealed("2026-07-09", state)
    (date_dir / f"{runner.ROOM}_2026-07-09-20-00-00-.mp4").write_bytes(b"y")  # 晚段到达
    assert not runner.session_sealed("2026-07-09", state), "晚段必须重新参与竞争"


def test_write_reports_no_delivery_is_loud(tmp_path, monkeypatch):
    """7/9 P0：两歌全 BLOCK、0 交付，摘要却写 done。现在必须刺眼。"""
    monkeypatch.setattr(runner, "BASE", tmp_path)
    monkeypatch.setattr(runner, "REPO_ROOT", tmp_path)
    state = {
        "status": "no_delivery", "picks": [], "segments_done": [], "segments_dead": {},
        "pending_talk": [], "pending_song": [],
        "songs": [{"candidate_id": "song_a", "status": "blocked", "decision": "BLOCK",
                   "reason_codes": ["END_BOUNDARY_LOW"], "danmaku": 18}],
    }
    runner.write_reports("2026-07-09", state)
    text = (tmp_path / "lidousha" / "2026-07-09" / "AUTOSLICE_SUMMARY.md").read_text(encoding="utf-8")
    assert "no_delivery" in text
    assert "0 条交付" in text
    assert "歌 **0 交付**" in text
    assert "done" not in text.splitlines()[2]


def test_write_reports_legacy_quarantine_rows_still_render(tmp_path, monkeypatch):
    """Old state files may still carry pre-2026-07-10 quarantine picks — they
    stay visible (delivered + flags shown) but are labelled as legacy."""
    monkeypatch.setattr(runner, "BASE", tmp_path)
    monkeypatch.setattr(runner, "REPO_ROOT", tmp_path)
    state = {
        "status": "review_ready", "segments_done": [], "segments_dead": {},
        "pending_talk": [], "pending_song": [], "songs": [],
        "picks": [{"candidate_id": "auto_1", "status": "quarantine", "hook": "钩子",
                   "title": "【李豆沙】标题", "confidence": 0.9,
                   "red_flags": ["speech_continues_5000ms_after_cut"],
                   "start_ms": 0, "end_ms": 60_000, "summary": {}}],
    }
    runner.write_reports("2026-07-09", state)
    text = (tmp_path / "lidousha" / "2026-07-09" / "AUTOSLICE_SUMMARY.md").read_text(encoding="utf-8")
    assert "quarantine[speech_continues_5000ms_after_cut]" in text
    assert "1 条旧版 quarantine" in text


def test_write_reports_boundary_repair_and_unrepairable(tmp_path, monkeypatch):
    """Post-2026-07-10 vocabulary (Ivan: unattended = fix-or-refuse): delivered
    picks are clean with the repair trail shown; unrepairable boundaries are
    undelivered failures — no new quarantine state."""
    monkeypatch.setattr(runner, "BASE", tmp_path)
    monkeypatch.setattr(runner, "REPO_ROOT", tmp_path)
    state = {
        "status": "review_ready", "segments_done": [], "segments_dead": {},
        "pending_talk": [], "pending_song": [], "songs": [],
        "picks": [
            {"candidate_id": "auto_1", "status": "review_ready",
             "bundle_lifecycle": "CURRENT", "bundle_compliance": "COMPLIANT",
             "hook": "钩子A",
             "title": "【李豆沙】标题A", "confidence": 0.9,
             "boundary_repairs": [{"flags": ["speech_continues_1800ms_after_cut"], "snapped_end_ms": 65_000}],
             "start_ms": 0, "end_ms": 60_000, "summary": {}},
            {"candidate_id": "auto_2", "status": "boundary_unrepairable", "hook": "钩子B",
             "title": None, "confidence": 0.8,
             "start_ms": 0, "end_ms": 50_000, "summary": {}},
        ],
    }
    runner.write_reports("2026-07-09", state)
    text = (tmp_path / "lidousha" / "2026-07-09" / "AUTOSLICE_SUMMARY.md").read_text(encoding="utf-8")
    assert "边界自修复×1" in text
    assert "1 条边界自修复后交付" in text
    assert "1 条边界不可修复未交付" in text
    assert "✗边界不可修复未交付" in text
    assert "⚠quarantine" not in text


def test_bound_song_delivery_recovery_uses_exact_hashed_summary_before_requeue(
    tmp_path, monkeypatch
):
    date = "2026-07-10"
    cid = "song_212005_1444"
    base = tmp_path / "autoslice"
    summary_path = (
        base
        / "out"
        / date
        / cid
        / "song_selector_full"
        / "attempt-current"
        / "seededsong_ready"
        / "summary.json"
    )
    summary_path.parent.mkdir(parents=True)
    summary_payload = {
        "records": [
            {
                "candidate_id": "seededsong_ready",
                "decision_action": "AUTO_UPLOAD",
                "reason_codes": ["SONG_FULL_BOUNDARY_READY"],
            }
        ]
    }
    summary_path.write_text(json.dumps(summary_payload), encoding="utf-8")
    summary_sha = "sha256:" + hashlib.sha256(summary_path.read_bytes()).hexdigest()
    monkeypatch.setattr(runner, "BASE", base)
    calls = []

    def fake_commit(**kwargs):
        calls.append(kwargs)
        return {
            "delivered": "/delivery/song.mp4",
            "delivered_sha256": "sha256:" + "1" * 64,
            "video_sha256": "sha256:" + "1" * 64,
            "delivered_sidecars": {"active_record": "/delivery/song.record.json"},
            "delivered_sidecar_hashes": {"active_record": "sha256:" + "2" * 64},
            "delivery_manifest_path": "/delivery/song.delivery.manifest.json",
            "delivery_manifest_sha256": "sha256:" + "3" * 64,
            "delivery_upload_enabled": False,
            "cover_status": "BLOCKED_AI_COVER_REQUIRED",
        }

    monkeypatch.setattr(runner, "_commit_verified_song_package", fake_commit)
    record = {
        "candidate_id": cid,
        "status": "blocked",
        "rc": 0,
        "title": "【李豆沙】豆沙歌，《想和你迎着台风去看海》｜台风天唱甜甜的",
        "reason_codes": ["SONG_FULL_BOUNDARY_READY", "SONG_DELIVERY_ATOMIC_COPY_FAILED"],
        "delivery_error": "old packaging bug",
        "selector_summary_path": str(summary_path),
        "selector_summary_sha256": summary_sha,
        "selector_record_candidate_id": "seededsong_ready",
        "verified_delivery_pending_commit": True,
    }
    record["song_delivery_recovery_authority"] = runner._song_delivery_recovery_authority(
        date=date,
        outer_candidate_id=cid,
        summary_path=str(summary_path),
        summary_sha256=summary_sha,
        source_candidate_id="seededsong_ready",
        title=record["title"],
    )
    state = {"songs": [record]}

    assert runner.recover_bound_song_deliveries(date, state) == 1
    assert len(calls) == 1
    assert calls[0]["summary_record"]["candidate_id"] == "seededsong_ready"
    assert calls[0]["selector_rc"] == 0
    assert record["status"] == "review_ready"
    assert record["delivered"] == "/delivery/song.mp4"
    assert record["delivery_upload_enabled"] is False
    assert record["delivery_recovered_without_selector_rerun"] is True
    assert "SONG_DELIVERY_ATOMIC_COPY_FAILED" not in record["reason_codes"]
    assert "delivery_error" not in record
    assert "verified_delivery_pending_commit" not in record

    # Summary drift revokes recovery instead of silently selecting another
    # attempt from the candidate directory.
    record.pop("delivered")
    record["status"] = "blocked"
    record["reason_codes"].append("SONG_DELIVERY_ATOMIC_COPY_FAILED")
    record["verified_delivery_pending_commit"] = True
    summary_path.write_text(json.dumps({"records": []}), encoding="utf-8")
    assert runner.recover_bound_song_deliveries(date, state) == 0
    assert len(calls) == 1

    # State drift after binding (including a title edit) also revokes recovery.
    summary_path.write_text(json.dumps(summary_payload), encoding="utf-8")
    record["selector_summary_sha256"] = (
        "sha256:" + hashlib.sha256(summary_path.read_bytes()).hexdigest()
    )
    record["title"] = "【李豆沙】豆沙歌，被篡改的标题"
    assert runner.recover_bound_song_deliveries(date, state) == 0
    assert len(calls) == 1


def test_bound_song_delivery_recovery_never_exceeds_daily_quota(
    tmp_path, monkeypatch
):
    date = "2026-07-10"
    base = tmp_path / "autoslice"
    monkeypatch.setattr(runner, "BASE", base)
    calls = []

    def reserved_record(cid: str) -> dict:
        summary_path = base / "out" / date / cid / "attempt" / "summary.json"
        summary_path.parent.mkdir(parents=True)
        summary_path.write_text(
            json.dumps(
                {
                    "records": [
                        {
                            "candidate_id": f"inner_{cid}",
                            "decision_action": "AUTO_UPLOAD",
                            "reason_codes": ["SONG_FULL_BOUNDARY_READY"],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        record = {
            "candidate_id": cid,
            "status": "blocked",
            "rc": 0,
            "title": f"【李豆沙】豆沙歌，《{cid}》｜已验证待打包",
            "reason_codes": [
                "SONG_FULL_BOUNDARY_READY",
                "SONG_DELIVERY_ATOMIC_COPY_FAILED",
            ],
            "selector_summary_path": str(summary_path),
            "selector_summary_sha256": (
                "sha256:" + hashlib.sha256(summary_path.read_bytes()).hexdigest()
            ),
            "selector_record_candidate_id": f"inner_{cid}",
            "verified_delivery_pending_commit": True,
        }
        record["song_delivery_recovery_authority"] = runner._song_delivery_recovery_authority(
            date=date,
            outer_candidate_id=cid,
            summary_path=record["selector_summary_path"],
            summary_sha256=record["selector_summary_sha256"],
            source_candidate_id=record["selector_record_candidate_id"],
            title=record["title"],
        )
        return record

    def fake_commit(**kwargs):
        cid = kwargs["delivery_candidate_id"]
        calls.append(cid)
        return {
            "delivered": f"/delivery/{cid}.mp4",
            "delivery_upload_enabled": False,
        }

    monkeypatch.setattr(runner, "_commit_verified_song_package", fake_commit)

    # A full quota must stop before reading or committing the reservation.
    full_state = {
        "songs": [
            *[
                {"candidate_id": f"done_{index}", "delivered": f"/{index}.mp4"}
                for index in range(MAX_SONGS_PER_DATE)
            ],
            reserved_record("reserved_full"),
        ]
    }
    assert runner.recover_bound_song_deliveries(date, full_state) == 0
    assert calls == []
    assert full_state["songs"][-1]["verified_delivery_pending_commit"] is True

    # With the one session slot open, only the first of two reservations may commit.
    one_slot_state = {
        "songs": [
            reserved_record("reserved_first"),
            reserved_record("reserved_second"),
        ]
    }
    assert runner.recover_bound_song_deliveries(date, one_slot_state) == 1
    assert calls == ["reserved_first"]
    assert one_slot_state["songs"][0]["delivered"] == "/delivery/reserved_first.mp4"
    assert "verified_delivery_pending_commit" not in one_slot_state["songs"][0]
    assert one_slot_state["songs"][1]["verified_delivery_pending_commit"] is True

    # A full earlier broadcast must not consume a later broadcast's recovery
    # reservation on the same calendar date.
    calls.clear()
    later = reserved_record("reserved_later_session")
    later["session_id"] = "live-later"
    per_session_state = {
        "songs": [
            *[
                {
                    "candidate_id": f"early_{index}",
                    "delivered": f"/early-{index}.mp4",
                    "session_id": "live-early",
                }
                for index in range(MAX_SONGS_PER_DATE)
            ],
            later,
        ]
    }
    assert runner.recover_bound_song_deliveries(date, per_session_state) == 1
    assert calls == ["reserved_later_session"]


def test_explicit_song_recovery_authority_backfill_verifies_exact_attempt_and_title(
    tmp_path, monkeypatch
):
    date = "2026-07-10"
    cid = "song_212005_1444"
    base = tmp_path / "autoslice"
    candidate_root = base / "out" / date / cid
    fx = _deferred_song_summary(candidate_root)
    fx["summary"]["source_context_job"]["song_boundary"] = {
        "status": "FULL_SONG_READY",
        "song_title": "想和你迎着台风去看海",
    }
    summary_path = candidate_root / "attempt" / "summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(
        json.dumps({"records": [fx["summary"]]}, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(runner, "BASE", base)
    monkeypatch.setattr(runner, "song_completion_evidence", lambda _record: {"ready": True})
    monkeypatch.setattr(runner, "song_delivery_ok", lambda *_args, **_kwargs: True)
    state_record = {
        "candidate_id": cid,
        "status": "blocked",
        "rc": 0,
        "hook": "台风天唱甜甜的《和你迎着台风去看海》",
        "reason_codes": ["SONG_FULL_BOUNDARY_READY", "SONG_DELIVERY_ATOMIC_COPY_FAILED"],
    }
    state = {"songs": [state_record]}

    assert runner.bind_song_delivery_recovery_authority(
        date,
        state,
        candidate_id=cid,
        summary_path=summary_path,
    ) is True
    assert state_record["selector_summary_path"] == str(summary_path.resolve())
    assert runner._matches_sha256(
        summary_path, state_record["selector_summary_sha256"]
    )
    assert state_record["selector_record_candidate_id"] == "seededsong_ready"
    assert state_record["verified_delivery_pending_commit"] is True
    # Ivan 2026-07-14/19 铁律：恢复路径的标题同样是固定目录式，hook 被忽略。
    assert state_record["title"] == "【李豆沙】豆沙歌，《想和你迎着台风去看海》"
    assert state_record["selector_summary_authority_backfill"]["upload_enabled"] is False
    assert state_record["song_delivery_recovery_authority"] == {
        "schema_version": "song-delivery-recovery-authority.v1",
        "date": date,
        "outer_candidate_id": cid,
        "selector_summary_path": str(summary_path.resolve()),
        "selector_summary_sha256": state_record["selector_summary_sha256"],
        "source_candidate_id": "seededsong_ready",
        "title": state_record["title"],
        "upload_enabled": False,
    }

    escaped = tmp_path / "outside" / "summary.json"
    escaped.parent.mkdir()
    escaped.write_text(summary_path.read_text(encoding="utf-8"), encoding="utf-8")
    state_record.pop("selector_summary_path")
    state_record.pop("selector_summary_sha256")
    state_record.pop("selector_record_candidate_id")
    with pytest.raises(runner.SongDeliveryError, match="escapes the outer candidate"):
        runner.bind_song_delivery_recovery_authority(
            date,
            state,
            candidate_id=cid,
            summary_path=escaped,
        )


def test_pipeline_change_requeues_unproven_song_without_treating_it_as_performer_rejection(tmp_path, monkeypatch):
    date = "2026-07-10"
    rec_root = tmp_path / "recordings"
    date_dir = rec_root / date
    date_dir.mkdir(parents=True)
    segment = date_dir / "22966160_20260710-20-00-09.mp4"
    segment.write_bytes(b"media")
    monkeypatch.setattr(runner, "REC_ROOT", rec_root)
    monkeypatch.setattr(runner, "song_pipeline_fingerprint", lambda: "sha256:new")
    monkeypatch.setattr(runner, "ffprobe_ms", lambda _path: 600_000)
    monkeypatch.setattr(runner, "find_danmaku_xml", lambda _path: None)
    monkeypatch.setattr(runner, "find_chat_jsonl", lambda _path: None)
    state = {
        "pending_song": [],
        "songs": [
            {
                "candidate_id": "song_200009_217",
                "segment": segment.name,
                "start_ms": 202_000,
                "end_ms": 483_000,
                "status": "blocked",
                "reason_codes": ["SONG_LIVE_PERFORMANCE_UNPROVEN", "SONG_HOST_VOCAL_UNPROVEN"],
                "pipeline_fingerprint": "sha256:old",
                "song_pipeline_fingerprint": "sha256:old",
                "hook": "《怎么办》",
                "discovery_lane": "visual_song_list",
                "title_hint": "怎么办",
                "visual_song_evidence": {"frame_ms": 217_000, "list_index": 3},
                "session_id": "live-20260710T200000+0800",
            }
        ],
    }

    assert runner.requeue_recoverable_songs(date, state) == 1
    assert state["songs"] == []
    assert state["pending_song"][0]["anchor_start_ms"] == 217_000
    assert state["pending_song"][0]["anchor_end_ms"] == 463_000
    assert state["pending_song"][0]["retry_reason"] == "pipeline_fingerprint_changed"
    assert state["pending_song"][0]["lane"] == "visual_song_list"
    assert state["pending_song"][0]["title_hint"] == "怎么办"
    assert state["pending_song"][0]["visual_song_evidence"] == {
        "frame_ms": 217_000,
        "list_index": 3,
    }
    assert state["pending_song"][0]["session_id"] == "live-20260710T200000+0800"
    assert state["song_superseded_attempts"][0]["session_id"] == "live-20260710T200000+0800"


def test_song_requeue_lifetime_cap_is_per_live_session(tmp_path, monkeypatch):
    date = "2026-07-10"
    rec_root = tmp_path / "recordings"
    date_dir = rec_root / date
    date_dir.mkdir(parents=True)
    segment = date_dir / "22966160_20260710-22-00-00.mp4"
    segment.write_bytes(b"media")
    monkeypatch.setattr(runner, "REC_ROOT", rec_root)
    monkeypatch.setattr(runner, "song_pipeline_fingerprint", lambda: "sha256:new")
    monkeypatch.setattr(runner, "ffprobe_ms", lambda _path: 600_000)
    monkeypatch.setattr(runner, "find_danmaku_xml", lambda _path: None)
    monkeypatch.setattr(runner, "find_chat_jsonl", lambda _path: None)
    old_session = "live-old"
    new_session = "live-new"
    state = {
        "pending_song": [],
        "songs": [
            *[
                {
                    "candidate_id": f"old-{index}",
                    "delivered": f"/{index}.mp4",
                    "session_id": old_session,
                }
                for index in range(runner.SONG_LIFETIME_ATTEMPT_CAP)
            ],
            {
                "candidate_id": "song_new_session",
                "segment": segment.name,
                "start_ms": 100_000,
                "end_ms": 400_000,
                "status": "blocked",
                "reason_codes": ["SONG_LIVE_PERFORMANCE_UNPROVEN"],
                "pipeline_fingerprint": "sha256:old",
                "song_pipeline_fingerprint": "sha256:old",
                "session_id": new_session,
            },
        ],
    }

    assert runner.requeue_recoverable_songs(date, state) == 1
    assert state["pending_song"][0]["session_id"] == new_session


def test_verified_song_commit_reservation_is_not_requeued(monkeypatch):
    monkeypatch.setattr(runner, "song_pipeline_fingerprint", lambda: "sha256:new")
    record = {
        "candidate_id": "song_verified_pending_commit",
        "status": "blocked",
        "rc": 0,
        "reason_codes": [
            "SONG_FULL_BOUNDARY_READY",
            "SONG_DELIVERY_ATOMIC_COPY_FAILED",
        ],
        "pipeline_fingerprint": "sha256:old",
        "verified_delivery_pending_commit": True,
    }
    state = {"pending_song": [], "songs": [record]}

    assert runner.requeue_recoverable_songs("2026-07-10", state) == 0
    assert state["songs"] == [record]
    assert state["pending_song"] == []


def test_missing_song_recovery_authority_does_not_reserve_quota_and_gets_one_retry(
    tmp_path, monkeypatch
):
    date = "2026-07-10"
    rec_root = tmp_path / "recordings"
    date_dir = rec_root / date
    date_dir.mkdir(parents=True)
    segment = date_dir / "22966160_20260710-20-00-09.mp4"
    segment.write_bytes(b"media")
    monkeypatch.setattr(runner, "REC_ROOT", rec_root)
    monkeypatch.setattr(runner, "song_pipeline_fingerprint", lambda: "sha256:same")
    monkeypatch.setattr(runner, "ffprobe_ms", lambda _path: 600_000)
    monkeypatch.setattr(runner, "find_danmaku_xml", lambda _path: None)
    monkeypatch.setattr(runner, "find_chat_jsonl", lambda _path: None)
    record = {
        "candidate_id": "song_missing_authority",
        "segment": segment.name,
        "start_ms": 100_000,
        "end_ms": 400_000,
        "anchor_start_ms": 120_000,
        "anchor_end_ms": 380_000,
        "status": "blocked",
        "rc": 0,
        "reason_codes": [
            "SONG_DELIVERY_ATOMIC_COPY_FAILED",
            "SONG_DELIVERY_RECOVERY_AUTHORITY_MISSING",
        ],
        "pipeline_fingerprint": "sha256:same",
        "song_pipeline_fingerprint": "sha256:same",
        "hook": "《测试歌》",
    }
    state = {"pending_song": [], "songs": [record]}

    assert runner.song_delivery_budget(state) == MAX_SONGS_PER_DATE
    assert runner.requeue_recoverable_songs(date, state) == 1
    assert state["songs"] == []
    assert state["pending_song"][0]["cid"] == "song_missing_authority"
    assert state["pending_song"][0]["transient_retry_count"] == 1


def test_pipeline_fingerprint_covers_song_proof_closure(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "REPO_ROOT", tmp_path)
    load_bearing = [
        "scripts/free_session_autoslice.py",
        "scripts/free_asr_client.py",
            "scripts/apply_subtitle_text_overrides.py",
            "scripts/cpa_semantic_qa_llm.py",
            "scripts/llm_via_cpa.sh",
            "scripts/regenerate_lidousha_cover.py",
            "scripts/resume_frozen_talk_package.py",
            "scripts/run_auto_review_shadow_pipeline.py",
        "src/autoslice/agy_lrc_alignment.py",
        "src/autoslice/host_vocal_proof.py",
        "assets/lidousha/known_songs.json",
        "assets/lidousha/voiceprint_profile.v1.json",
            "assets/lidousha/entity_confusables.json",
            "assets/lidousha/persona.md",
            "assets/lidousha/title_style.md",
        ]
    for relative in load_bearing:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"base:{relative}\n", encoding="utf-8")
    report_only = tmp_path / "src/autoslice/reporting.py"
    report_only.parent.mkdir(parents=True, exist_ok=True)
    report_only.write_text("base:report projection\n", encoding="utf-8")
    baseline = runner.pipeline_fingerprint()

    for relative in load_bearing:
        path = tmp_path / relative
        original = path.read_text(encoding="utf-8")
        path.write_text(original + "changed\n", encoding="utf-8")
        assert runner.pipeline_fingerprint() != baseline, relative
        path.write_text(original, encoding="utf-8")

    unrelated = tmp_path / "README.md"
    unrelated.parent.mkdir(parents=True, exist_ok=True)
    unrelated.write_text("unrelated\n", encoding="utf-8")
    assert runner.pipeline_fingerprint() == baseline

    report_only.write_text("changed:report projection\n", encoding="utf-8")
    assert runner.pipeline_fingerprint() == baseline


def test_song_pipeline_fingerprint_excludes_talk_entity_authority(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "REPO_ROOT", tmp_path)
    song_paths = [
        "scripts/run_full_session_selector_cpa_shadow.py",
        "src/autoslice/agy_lrc_alignment.py",
        "src/autoslice/song_lane.py",
        "assets/lidousha/known_songs.json",
        "assets/lidousha/voiceprint_profile.v1.json",
    ]
    talk_only_paths = [
        "src/autoslice/reporting.py",
        "src/autoslice/chat_evidence.py",
        "src/autoslice/chat_proposals.py",
        "src/autoslice/chat_repair.py",
        "src/autoslice/producer_text_pipeline.py",
        "assets/lidousha/entity_confusables.json",
        "assets/lidousha/subtitle_correction_principles.md",
    ]
    for relative in [*song_paths, *talk_only_paths]:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"base:{relative}\n", encoding="utf-8")

    baseline = runner.song_pipeline_fingerprint()
    for relative in talk_only_paths:
        path = tmp_path / relative
        original = path.read_text(encoding="utf-8")
        path.write_text(original + "changed\n", encoding="utf-8")
        assert runner.song_pipeline_fingerprint() == baseline, relative
        path.write_text(original, encoding="utf-8")
    for relative in song_paths:
        path = tmp_path / relative
        original = path.read_text(encoding="utf-8")
        path.write_text(original + "changed\n", encoding="utf-8")
        assert runner.song_pipeline_fingerprint() != baseline, relative
        path.write_text(original, encoding="utf-8")


def test_legacy_global_song_fingerprint_is_migrated_without_retry(monkeypatch):
    monkeypatch.setattr(runner, "song_pipeline_fingerprint", lambda: "sha256:scoped")
    record = {
        "candidate_id": "song_legacy_global",
        "status": "blocked",
        "reason_codes": ["SONG_LIVE_PERFORMANCE_UNPROVEN"],
        "pipeline_fingerprint": "sha256:old-global",
    }
    state = {"pending_song": [], "songs": [record]}

    assert runner.requeue_recoverable_songs("2026-07-10", state) == 0
    assert record["song_pipeline_fingerprint"] == "sha256:scoped"
    assert state["song_pipeline_fingerprint_baseline"] == "sha256:scoped"
    assert state["pending_song"] == []


def test_talk_fingerprint_scopes_candidate_override_add_edit_delete(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(runner, "pipeline_fingerprint", lambda: "sha256:" + "a" * 64)
    root = tmp_path / "assets/lidousha/subtitle_text_overrides"
    root.mkdir(parents=True)
    first_before = runner.talk_pipeline_fingerprint("auto_first")
    second_before = runner.talk_pipeline_fingerprint("auto_second")

    override = root / "auto_first.text.v1.json"
    override.write_text('{"version":1}\n', encoding="utf-8")
    first_added = runner.talk_pipeline_fingerprint("auto_first")
    assert first_added != first_before
    assert runner.talk_pipeline_fingerprint("auto_second") == second_before

    override.write_text('{"version":2}\n', encoding="utf-8")
    assert runner.talk_pipeline_fingerprint("auto_first") != first_added
    override.unlink()
    assert runner.talk_pipeline_fingerprint("auto_first") == first_before


def test_talk_fingerprint_scopes_candidate_regression_add_edit_delete(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(runner, "pipeline_fingerprint", lambda: "sha256:" + "a" * 64)
    root = tmp_path / "assets/lidousha/subtitle_regressions"
    root.mkdir(parents=True)
    first_before = runner.talk_pipeline_fingerprint("auto_first")
    second_before = runner.talk_pipeline_fingerprint("auto_second")

    regression = root / "auto_first.subtitle-regression.v1.json"
    regression.write_text('{"version":1}\n', encoding="utf-8")
    first_added = runner.talk_pipeline_fingerprint("auto_first")
    assert first_added != first_before
    assert runner.talk_pipeline_fingerprint("auto_second") == second_before

    regression.write_text('{"version":2}\n', encoding="utf-8")
    assert runner.talk_pipeline_fingerprint("auto_first") != first_added
    regression.unlink()
    assert runner.talk_pipeline_fingerprint("auto_first") == first_before


def test_talk_fingerprint_scopes_reviewed_subtitle_baseline_bytes(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(runner, "pipeline_fingerprint", lambda: "sha256:" + "a" * 64)
    root = tmp_path / "assets/lidousha/reviewed_subtitle_baselines"
    root.mkdir(parents=True)
    first_before = runner.talk_pipeline_fingerprint("auto_first")
    second_before = runner.talk_pipeline_fingerprint("auto_second")
    baseline = root / "auto_first.reviewed.srt"
    baseline.write_text("1\n00:00:00,000 --> 00:00:01,000\n原稿\n", encoding="utf-8")
    manifest = root / "auto_first.subtitle-baseline.v1.json"

    def write_manifest() -> None:
        manifest.write_text(
            json.dumps(
                {
                    "registry_schema_version": "candidate-reviewed-subtitle-baseline.v1",
                    "candidate_id": "auto_first",
                    "schema_version": "subtitle-redelivery-baseline.v2",
                    "mode": "preserve_text_outside_source_truth",
                    "path": baseline.name,
                    "sha256": hashlib.sha256(baseline.read_bytes()).hexdigest(),
                    "authority": "Ivan reviewed delivery",
                    "source_recording_basename": "recording.mp4",
                    "source_sha256": "b" * 64,
                    "absolute_source_start_ms": 10_000,
                    "absolute_source_end_ms": 11_000,
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )

    write_manifest()
    first_added = runner.talk_pipeline_fingerprint("auto_first")
    assert first_added != first_before
    assert runner.talk_pipeline_fingerprint("auto_second") == second_before

    baseline.write_text("1\n00:00:00,000 --> 00:00:01,000\n修订稿\n", encoding="utf-8")
    write_manifest()
    assert runner.talk_pipeline_fingerprint("auto_first") != first_added


def test_candidate_override_discovery_rejects_symlink(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "REPO_ROOT", tmp_path)
    root = tmp_path / "assets/lidousha/subtitle_text_overrides"
    root.mkdir(parents=True)
    outside = tmp_path / "outside.json"
    outside.write_text("{}\n", encoding="utf-8")
    (root / "auto_bad.text.v1.json").symlink_to(outside)

    with pytest.raises(ValueError, match="non-symlink"):
        runner.candidate_text_override_path("auto_bad")


def test_candidate_regression_discovery_rejects_symlink(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "REPO_ROOT", tmp_path)
    root = tmp_path / "assets/lidousha/subtitle_regressions"
    root.mkdir(parents=True)
    outside = tmp_path / "outside.json"
    outside.write_text("{}\n", encoding="utf-8")
    (root / "auto_bad.subtitle-regression.v1.json").symlink_to(outside)

    with pytest.raises(ValueError, match="non-symlink"):
        runner.candidate_subtitle_regression_path("auto_bad")


def test_song_lifetime_cap_survives_repeated_pipeline_changes(tmp_path, monkeypatch):
    date = "2026-07-10"
    rec_root = tmp_path / "recordings"
    date_dir = rec_root / date
    date_dir.mkdir(parents=True)
    segment = date_dir / "22966160_20260710-20-00-09.mp4"
    segment.write_bytes(b"media")
    monkeypatch.setattr(runner, "REC_ROOT", rec_root)
    monkeypatch.setattr(runner, "SONG_LIFETIME_ATTEMPT_CAP", 2)
    monkeypatch.setattr(runner, "pipeline_fingerprint", lambda: "sha256:generation-3")
    state = {
        "pending_song": [],
        "song_superseded_attempts": [{"candidate_id": "song_old_generation"}],
        "songs": [
            {
                "candidate_id": "song_current",
                "segment": segment.name,
                "start_ms": 100_000,
                "end_ms": 300_000,
                "status": "blocked",
                "reason_codes": ["SONG_LIVE_PERFORMANCE_UNPROVEN"],
                "pipeline_fingerprint": "sha256:generation-2",
            }
        ],
    }

    assert runner.requeue_recoverable_songs(date, state) == 0
    assert state["pending_song"] == []
    assert len(state["songs"]) == 1


def test_confirmed_non_host_song_is_terminal_across_pipeline_change(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "pipeline_fingerprint", lambda: "sha256:new")
    state = {
        "pending_song": [],
        "songs": [
            {
                "candidate_id": "song_other",
                "status": "blocked",
                "reason_codes": ["SONG_NOT_LIDOUSHA_SINGING"],
                "pipeline_fingerprint": "sha256:old",
            }
        ],
    }
    assert runner.requeue_recoverable_songs("2026-07-10", state) == 0
    assert len(state["songs"]) == 1


def test_transient_agy_failure_retries_across_ticks_with_backoff(tmp_path, monkeypatch):
    date = "2026-07-10"
    rec_root = tmp_path / "recordings"
    date_dir = rec_root / date
    date_dir.mkdir(parents=True)
    segment = date_dir / "22966160_20260710-19-30-09.mp4"
    segment.write_bytes(b"media")
    monkeypatch.setattr(runner, "REC_ROOT", rec_root)
    monkeypatch.setattr(runner, "pipeline_fingerprint", lambda: "sha256:same")
    monkeypatch.setattr(runner, "ffprobe_ms", lambda _path: 500_000)
    monkeypatch.setattr(runner, "find_danmaku_xml", lambda _path: None)
    monkeypatch.setattr(runner, "find_chat_jsonl", lambda _path: None)
    monkeypatch.setattr(runner.time, "time", lambda: 10_000)
    record = {
        "candidate_id": "song_timeout",
        "segment": segment.name,
        "start_ms": 100_000,
        "end_ms": 300_000,
        "status": "blocked",
        "reason_codes": ["AGY_SOURCE_CONTEXT_RUNNER_FAILED"],
        "pipeline_fingerprint": "sha256:same",
        "transient_retry_count": 0,
        "next_retry_at_epoch": 9_999,
        "full_source_retry": {"status": "blocked"},
    }
    state = {"pending_song": [], "songs": [record]}
    assert runner.requeue_recoverable_songs(date, state) == 1
    assert state["pending_song"][0]["transient_retry_count"] == 1
    assert state["pending_song"][0]["resume_full_source"] is True

    state = {
        "pending_song": [],
        "songs": [{**record, "transient_retry_count": 1, "next_retry_at_epoch": 10_001}],
    }
    assert runner.requeue_recoverable_songs(date, state) == 0
    state["songs"][0]["next_retry_at_epoch"] = 9_999
    assert runner.requeue_recoverable_songs(date, state) == 1
    assert state["pending_song"][0]["transient_retry_count"] == 2


def test_song_selector_env_gives_song_context_a_bounded_long_timeout(monkeypatch):
    monkeypatch.delenv("SONG_AGY_PRINT_TIMEOUT", raising=False)
    monkeypatch.setattr(runner, "child_env_for_date", lambda _date: {})
    assert runner.song_selector_env("2026-07-10")["AGY_PRINT_TIMEOUT"] == "30m"

    monkeypatch.setenv("SONG_AGY_PRINT_TIMEOUT", "24m")
    assert runner.song_selector_env("2026-07-10")["AGY_PRINT_TIMEOUT"] == "24m"


@pytest.mark.parametrize(
    ("diagnostic", "expected"),
    [
        ("HTTP Error 429: Too Many Requests", "CPA_RATE_LIMITED"),
        ("CPA_LLM_JUDGE_MODEL_DOWN: luna", "CPA_MODEL_DOWN"),
        ("direct LLM call failed: HTTP Error 503", "CPA_UPSTREAM_5XX"),
        ("request timed out", "CPA_UPSTREAM_TIMEOUT"),
        ("semantic decision: BLOCK", None),
    ],
)
def test_song_selector_transient_diagnostics_are_structured(diagnostic, expected):
    assert runner.classify_song_selector_transient(diagnostic) == expected


def test_rate_limited_song_retries_across_ticks_with_exponential_backoff(tmp_path, monkeypatch):
    date = "2026-07-12"
    rec_root = tmp_path / "recordings"
    date_dir = rec_root / date
    date_dir.mkdir(parents=True)
    segment = date_dir / "22966160_20260712-19-00-17.mp4"
    segment.write_bytes(b"media")
    monkeypatch.setattr(runner, "REC_ROOT", rec_root)
    monkeypatch.setattr(runner, "pipeline_fingerprint", lambda: "sha256:same")
    monkeypatch.setattr(runner, "ffprobe_ms", lambda _path: 500_000)
    monkeypatch.setattr(runner, "find_danmaku_xml", lambda _path: None)
    monkeypatch.setattr(runner, "find_chat_jsonl", lambda _path: None)
    monkeypatch.setattr(runner.time, "time", lambda: 10_000)
    record = {
        "candidate_id": "song_rate_limited",
        "segment": segment.name,
        "start_ms": 100_000,
        "end_ms": 300_000,
        "status": "failed",
        "reason_codes": ["CPA_RATE_LIMITED"],
        "pipeline_fingerprint": "sha256:same",
        "transient_retry_count": 2,
        "next_retry_at_epoch": 10_001,
    }
    state = {"pending_song": [], "songs": [record]}

    assert runner.requeue_recoverable_songs(date, state) == 0
    record["next_retry_at_epoch"] = 9_999
    assert runner.requeue_recoverable_songs(date, state) == 1
    assert state["pending_song"][0]["transient_retry_count"] == 3
    assert state["pending_song"][0]["retry_reason"] == "transient_infrastructure_failure"
    assert runner.song_infra_retry_delay_seconds(0) == 15 * 60
    assert runner.song_infra_retry_delay_seconds(20) == runner.SONG_INFRA_RETRY_MAX_SECONDS


def test_rate_limited_song_retry_cap_is_terminal_for_same_pipeline(tmp_path, monkeypatch):
    date = "2026-07-12"
    rec_root = tmp_path / "recordings"
    date_dir = rec_root / date
    date_dir.mkdir(parents=True)
    segment = date_dir / "22966160_20260712-19-00-17.mp4"
    segment.write_bytes(b"media")
    monkeypatch.setattr(runner, "REC_ROOT", rec_root)
    monkeypatch.setattr(runner, "pipeline_fingerprint", lambda: "sha256:same")
    record = {
        "candidate_id": "song_rate_limited",
        "segment": segment.name,
        "start_ms": 100_000,
        "end_ms": 300_000,
        "status": "failed",
        "reason_codes": ["CPA_RATE_LIMITED"],
        "pipeline_fingerprint": "sha256:same",
        "transient_retry_count": runner.SONG_INFRA_RETRY_CAP,
    }
    state = {"pending_song": [], "songs": [record]}

    # Recoverable provider outages do not become content failures merely
    # because a historical retry counter crossed an alerting threshold.
    assert runner.requeue_recoverable_songs(date, state) == 1
    assert state["pending_song"][0]["transient_retry_count"] == runner.SONG_INFRA_RETRY_CAP + 1


def test_date_lifetime_cap_does_not_discard_selected_infrastructure_retry(tmp_path, monkeypatch):
    date = "2026-07-12"
    rec_root = tmp_path / "recordings"
    date_dir = rec_root / date
    date_dir.mkdir(parents=True)
    segment = date_dir / "22966160_20260712-19-00-17.mp4"
    segment.write_bytes(b"media")
    monkeypatch.setattr(runner, "REC_ROOT", rec_root)
    monkeypatch.setattr(runner, "pipeline_fingerprint", lambda: "sha256:same")
    monkeypatch.setattr(runner, "ffprobe_ms", lambda _path: 500_000)
    monkeypatch.setattr(runner, "find_danmaku_xml", lambda _path: None)
    monkeypatch.setattr(runner, "find_chat_jsonl", lambda _path: None)
    monkeypatch.setattr(runner.time, "time", lambda: 10_000)
    record = {
        "candidate_id": "song_quota",
        "segment": segment.name,
        "start_ms": 100_000,
        "end_ms": 300_000,
        "status": "blocked",
        "reason_codes": ["AGY_QUOTA_EXHAUSTED"],
        "pipeline_fingerprint": "sha256:same",
        "next_retry_at_epoch": 9_999,
    }
    state = {
        "pending_song": [],
        "songs": [record] + [{"candidate_id": f"old-{i}"} for i in range(5)],
        "song_superseded_attempts": [{"candidate_id": f"sup-{i}"} for i in range(12)],
        "song_backlog": [],
    }

    assert len(state["songs"]) + len(state["song_superseded_attempts"]) == 18
    assert runner.requeue_recoverable_songs(date, state) == 1
    runner.refill_songs(state)
    assert [item["cid"] for item in state["pending_song"]] == ["song_quota"]


def test_scheduled_song_retry_keeps_date_nonterminal():
    future = 9_999_999_999
    state = {
        "songs": [
            {
                "status": "blocked",
                "reason_codes": ["AGY_QUOTA_EXHAUSTED"],
                "next_retry_at_epoch": future,
            }
        ]
    }
    assert runner.scheduled_song_retry_epoch(state) == future


def test_past_retry_metadata_does_not_keep_date_in_retry_wait():
    state = {
        "picks": [
            {
                "status": "failed",
                "failure_recoverable": True,
                "next_retry_at_epoch": 1,
            }
        ],
        "songs": [
            {
                "status": "blocked",
                "reason_codes": ["AGY_QUOTA_EXHAUSTED"],
                "next_retry_at_epoch": 1,
            }
        ],
    }

    assert runner.scheduled_talk_retry_epoch(state) is None
    assert runner.scheduled_song_retry_epoch(state) is None


def test_talk_failure_persists_runtime_stage_and_stable_fingerprint():
    output = (
        "Traceback (most recent call last):\n"
        "FileNotFoundError: [Errno 2] No such file or directory: "
        "'/tmp/repo/assets/lidousha/voiceprint_profile.v1.json'\n"
    )
    first = runner.classify_talk_failure(output)
    second = runner.classify_talk_failure(output.replace("/tmp/repo", "/different/repo"))

    assert first["failure_kind"] == "runtime_prerequisite"
    assert first["failure_stage"] == "speaker_preflight"
    assert first["failure_recoverable"] is True
    assert first["failure_fingerprint"] == second["failure_fingerprint"]


def test_talk_failure_classifies_real_clip_anchor_shortage_as_speaker_evidence():
    output = (
        "Traceback (most recent call last):\n"
        "RuntimeError: SPEAKER_FINALIZATION_BLOCKED: SpeakerFinalizationError: "
        "not enough Li Dousha clip anchors: [6]\n"
    )

    classified = runner.classify_talk_failure(output)

    assert classified["failure_kind"] == "speaker_evidence"
    assert classified["failure_stage"] == "speaker_finalization"
    assert classified["failure_recoverable"] is False


def test_talk_failure_classifies_vanished_source_media_as_terminal():
    classified = runner.classify_talk_failure(
        "RuntimeError: SOURCE_MEDIA_MISSING: "
        "/rec/22966160/2026-07-25/22966160_20260725-19-20-00.mp4"
    )

    assert classified["failure_kind"] == "source_media"
    assert classified["failure_stage"] == "source_media_binding"
    # Recoverable would make the runner treat a permanent loss as an outage and
    # break the batch, starving candidates whose recordings are still present.
    assert classified["failure_recoverable"] is False


def test_talk_failure_keeps_unreachable_recording_mount_recoverable():
    classified = runner.classify_talk_failure(
        "RuntimeError: SOURCE_RECORDING_ROOT_UNAVAILABLE: "
        "/rec/22966160/2026-07-25/22966160_20260725-19-20-00.mp4"
    )

    assert classified["failure_kind"] == "runtime_prerequisite"
    assert classified["failure_stage"] == "source_media_binding"
    assert classified["failure_recoverable"] is True


@pytest.mark.parametrize(
    "message",
    [
        "OSError: [Errno 107] Transport endpoint is not connected",
        "OSError: [Errno 131] State not recoverable",
    ],
)
def test_talk_failure_classifies_mid_production_fuse_disconnect_as_infrastructure(
    message,
):
    classified = runner.classify_talk_failure(message)

    assert classified["failure_kind"] == "runtime_prerequisite"
    assert classified["failure_stage"] == "source_media_binding"
    assert classified["failure_recoverable"] is True


def test_absent_source_media_error_separates_loss_from_mount_outage(tmp_path):
    from src.autoslice.producer_media import _absent_source_media_error

    live_root = tmp_path / "2026-07-25"
    live_root.mkdir()
    (live_root / "sibling.mp4").write_bytes(b"kept")
    vanished = live_root / "gone.mp4"

    assert _absent_source_media_error(vanished).startswith("SOURCE_MEDIA_MISSING:")

    unmounted = tmp_path / "unmounted" / "2026-07-25" / "gone.mp4"
    assert _absent_source_media_error(unmounted).startswith(
        "SOURCE_RECORDING_ROOT_UNAVAILABLE:"
    )


def test_source_media_sha256_refuses_vanished_recording(tmp_path):
    import pytest

    from src.autoslice.producer_media import _source_media_sha256

    date_root = tmp_path / "2026-07-25"
    date_root.mkdir()
    (date_root / "sibling.mp4").write_bytes(b"kept")

    with pytest.raises(RuntimeError, match="SOURCE_MEDIA_MISSING"):
        _source_media_sha256("localhost", date_root / "gone.mp4")


def test_talk_failure_classifies_chat_authority_finalization_as_terminal():
    classified = runner.classify_talk_failure(
        "RuntimeError: CHAT_AUTHORITY_FINALIZATION_FAILED: "
        "exact structured-chat wording did not survive final subtitles"
    )

    assert classified["failure_kind"] == "subtitle_authority"
    assert classified["failure_stage"] == "chat_authority_finalization"
    assert classified["failure_recoverable"] is False


def test_talk_failure_classifies_chat_authority_final_artifact_as_terminal():
    # Package-finalization surface verification failure is deterministic
    # content, not infrastructure: as unknown/recoverable it churned every
    # tick (2026-07-24 auto_193129_850_940).
    classified = runner.classify_talk_failure(
        "CHAT_AUTHORITY_FINAL_ARTIFACT_FAILED: "
        "/out/2026-07-24/auto_193129_850_940/auto_193129_850_940.chat-authority.json"
    )

    assert classified["failure_kind"] == "subtitle_authority"
    assert classified["failure_stage"] == "chat_authority_final_artifact"
    assert classified["failure_recoverable"] is False


def _write_final_review_failure_surfaces(
    root: Path,
    candidate_id: str,
    audit: dict,
) -> Path:
    candidate_dir = root / candidate_id
    candidate_dir.mkdir(parents=True)
    review_flags = candidate_dir / f"{candidate_id}.review-flags.json"
    review_flags.write_text(
        json.dumps(audit, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    chat_authority = candidate_dir / f"{candidate_id}.chat-authority.json"
    chat_authority.write_text(
        json.dumps(
            {
                "schema_version": "chat-authority-audit.v2",
                "final_review_audit": audit,
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return chat_authority


def _final_review_failure_audit(
    *,
    finding_count: int,
    boundary_status: str,
) -> dict:
    boundary_blocked = boundary_status != "PASS"
    findings = [
        {
            "cue_index": index + 1,
            "kind": "context",
            "repair_class": "spoken_unit",
            "suspect": f"误听{index + 1}",
            "suggestion": f"正词{index + 1}",
            "why": f"candidate-specific finding {index + 1}",
        }
        for index in range(finding_count)
    ]
    reason_codes = ["FINAL_REVIEW_UNRESOLVED_FINDINGS"] if findings else []
    if boundary_blocked:
        reason_codes.append("FINAL_REVIEW_BOUNDARY_SEMANTIC_BLOCKED")
    return {
        "schema_version": "final-review-audit.v2",
        "status": "FLAGGED",
        "release_gate": "BLOCK",
        "reason_codes": reason_codes,
        "reviewed_srt_sha256": "sha256:" + "a" * 64,
        "discovery": {
            "status": "COMPLETE",
            "explicit_empty_findings": not findings,
        },
        "findings": findings,
        "validated_finding_count": len(findings),
        "boundary_semantic_review": {
            "schema_version": "talk-boundary-semantic-review.v1",
            "status": boundary_status,
            "reason_codes": (
                ["TARGET_SENTENCE_COMPLETE"]
                if not boundary_blocked
                else [
                    "NO_VALID_CLOSED_BOUNDARY_WITHIN_PROVIDED_CUES",
                    "STORY_CLOSED_NOT_PROVEN",
                ]
            ),
            "target_ms": 230_760,
            "recommended_end_ms": None if boundary_blocked else 230_360,
            "syntax_complete": not boundary_blocked,
            "story_closed": not boundary_blocked,
            "content_anchor_covered": not boundary_blocked,
            "next_topic_separated": not boundary_blocked,
            "summary": (
                "clean closure"
                if not boundary_blocked
                else "same-topic answer continues after the target"
            ),
        },
    }


def test_v9_final_review_blocks_keep_deterministic_category_and_evidence(
    tmp_path: Path,
):
    # 7/22 v9 produced three finding-only blocks and two finding+boundary
    # blocks.  The old classifier collapsed all five into the same retryable
    # provider failure and the same fingerprint.
    cases = [
        ("auto_193450_3573_3665", 1, "PASS", "subtitle_authority", "final_review_findings"),
        ("auto_193450_672_945", 6, "PASS", "subtitle_authority", "final_review_findings"),
        (
            "auto_193450_1863_2056",
            1,
            "BLOCK",
            "content_boundary",
            "final_review_boundary_semantic",
        ),
        (
            "auto_193450_1573_1672",
            1,
            "BLOCK",
            "content_boundary",
            "final_review_boundary_semantic",
        ),
        ("auto_193450_1475_1543", 4, "PASS", "subtitle_authority", "final_review_findings"),
    ]
    fingerprints = set()
    for candidate_id, finding_count, boundary_status, kind, stage in cases:
        authority = _write_final_review_failure_surfaces(
            tmp_path,
            candidate_id,
            _final_review_failure_audit(
                finding_count=finding_count,
                boundary_status=boundary_status,
            ),
        )
        classified = runner.classify_talk_failure(
            "FINAL_REVIEW_RELEASE_BLOCKED: FINAL_REVIEW_RELEASE_GATE_BLOCKED: "
            f"{authority}"
        )

        assert classified["failure_kind"] == kind
        assert classified["failure_stage"] == stage
        assert classified["failure_recoverable"] is False
        evidence = classified["failure_evidence"]
        assert evidence["candidate_id"] == candidate_id
        assert evidence["reason_codes"]
        assert evidence["validated_finding_count"] == finding_count
        assert len(evidence["findings"]) == finding_count
        assert evidence["boundary_semantic_review"]["status"] == boundary_status
        fingerprints.add(classified["failure_fingerprint"])

    assert len(fingerprints) == len(cases)


def test_final_review_discovery_unavailable_remains_retryable_with_evidence(
    tmp_path: Path,
):
    candidate_id = "auto_provider_failure"
    audit = {
        "schema_version": "final-review-audit.v2",
        "status": "AUDITOR_UNAVAILABLE",
        "release_gate": "BLOCK",
        "reason_codes": ["FINAL_REVIEW_RESPONSE_FINDINGS_MISSING"],
        "reviewed_srt_sha256": "sha256:" + "b" * 64,
        "discovery": {
            "status": "AUDITOR_UNAVAILABLE",
            "detail": "provider returned an object without findings",
        },
        "findings": [],
        "validated_finding_count": 0,
        "boundary_semantic_review": {
            "schema_version": "talk-boundary-semantic-review.v1",
            "status": "PASS",
        },
    }
    authority = _write_final_review_failure_surfaces(
        tmp_path,
        candidate_id,
        audit,
    )

    classified = runner.classify_talk_failure(
        "FINAL_REVIEW_RELEASE_BLOCKED: FINAL_REVIEW_DISCOVERY_INCOMPLETE: "
        f"{authority}"
    )

    assert classified["failure_kind"] == "provider_transient"
    assert classified["failure_stage"] == "final_review_discovery"
    assert classified["failure_recoverable"] is True
    assert classified["failure_evidence"]["discovery"] == audit["discovery"]
    assert classified["failure_evidence"]["reason_codes"] == [
        "FINAL_REVIEW_RESPONSE_FINDINGS_MISSING"
    ]


def test_correction_discovery_unavailable_is_bounded_retryable_before_findings(
    tmp_path: Path,
):
    audit = _final_review_failure_audit(
        finding_count=3,
        boundary_status="PASS",
    )
    audit["reason_codes"].append(
        "FINAL_REVIEW_CORRECTION_MUTATION_AUTHORITY_INVALID"
    )
    audit["correction_pass"] = {
        "schema_version": "final-review-audit.v1",
        "status": "AUDITOR_UNAVAILABLE",
        "release_gate": "BLOCK",
        "reason_codes": [
            "FINAL_REVIEW_PROVIDER_OR_JSON_UNAVAILABLE"
        ],
        "discovery": {
            "status": "AUDITOR_UNAVAILABLE",
            "detail": "provider timeout",
        },
        "findings": [],
        "applied_count": 0,
        "error_type": "FinalReviewAuditError",
    }
    audit["correction_mutation_authority"] = {
        "schema_version": "subtitle-correction-mutation-audit.v1",
        "status": "BLOCK",
        "applied_count": 0,
        "validated_mutation_count": 0,
        "failures": [
            {
                "reason_code": "CORRECTION_DISCOVERY_INCOMPLETE",
                "upstream_reason_codes": [
                    "FINAL_REVIEW_PROVIDER_OR_JSON_UNAVAILABLE"
                ],
            }
        ],
    }
    authority = _write_final_review_failure_surfaces(
        tmp_path,
        "auto_correction_provider_failure",
        audit,
    )

    classified = runner.classify_talk_failure(
        "FINAL_REVIEW_RELEASE_BLOCKED: "
        "FINAL_REVIEW_CORRECTION_MUTATION_AUTHORITY_INVALID: "
        f"{authority}"
    )

    assert classified["failure_kind"] == "provider_transient"
    assert classified["failure_stage"] == (
        "final_review_correction_discovery"
    )
    assert classified["failure_recoverable"] is True
    evidence = classified["failure_evidence"]
    assert evidence["validated_finding_count"] == 3
    assert evidence["correction_pass"] == {
        "schema_version": "final-review-audit.v1",
        "status": "AUDITOR_UNAVAILABLE",
        "reason_codes": [
            "FINAL_REVIEW_PROVIDER_OR_JSON_UNAVAILABLE"
        ],
        "discovery": {
            "status": "AUDITOR_UNAVAILABLE",
            "detail": "provider timeout",
        },
        "error_type": "FinalReviewAuditError",
        "applied_count": 0,
    }
    assert evidence["correction_mutation_authority"]["failures"] == [
        {
            "reason_code": "CORRECTION_DISCOVERY_INCOMPLETE",
            "upstream_reason_codes": [
                "FINAL_REVIEW_PROVIDER_OR_JSON_UNAVAILABLE"
            ],
        }
    ]


def test_carryover_receipt_does_not_hide_retryable_correction_provider_failure(
    tmp_path: Path,
):
    audit = _final_review_failure_audit(
        finding_count=3,
        boundary_status="PASS",
    )
    audit["reason_codes"].append(
        "FINAL_REVIEW_CORRECTION_MUTATION_AUTHORITY_INVALID"
    )
    audit["correction_pass"] = {
        "schema_version": "final-review-audit.v1",
        "status": "AUDITOR_UNAVAILABLE",
        "release_gate": "BLOCK",
        "reason_codes": ["FINAL_REVIEW_PROVIDER_OR_JSON_UNAVAILABLE"],
        "discovery": {
            "status": "AUDITOR_UNAVAILABLE",
            "detail": "provider timeout",
        },
        "findings": [],
        "applied_count": 0,
        "error_type": "FinalReviewAuditError",
    }
    audit["correction_mutation_authority"] = {
        "schema_version": "subtitle-correction-mutation-audit.v1",
        "status": "BLOCK",
        "applied_count": 0,
        "validated_mutation_count": 0,
        "failures": [
            {
                "reason_code": "CORRECTION_DISCOVERY_INCOMPLETE",
                "upstream_reason_codes": [
                    "FINAL_REVIEW_PROVIDER_OR_JSON_UNAVAILABLE"
                ],
            }
        ],
    }
    authority = _write_final_review_failure_surfaces(
        tmp_path,
        "auto_carryover_provider_failure",
        audit,
    )
    chat_document = json.loads(authority.read_text(encoding="utf-8"))
    chat_document["final_review_audit"]["carryover_persisted_count"] = 3
    authority.write_text(
        json.dumps(chat_document, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )

    classified = runner.classify_talk_failure(
        "FINAL_REVIEW_RELEASE_BLOCKED: "
        "FINAL_REVIEW_CORRECTION_MUTATION_AUTHORITY_INVALID: "
        f"{authority}"
    )

    assert classified["failure_kind"] == "provider_transient"
    assert classified["failure_stage"] == (
        "final_review_correction_discovery"
    )
    assert classified["failure_recoverable"] is True
    evidence = classified["failure_evidence"]
    assert evidence["audit_surfaces_consistent"] is True
    assert evidence["audit_nested_additive_fields"] == [
        "carryover_persisted_count"
    ]


def test_exact_final_repaired_carryover_is_retryable(
    tmp_path: Path,
):
    candidate_id = "auto_exact_carryover"
    audit = _final_review_failure_audit(
        finding_count=2,
        boundary_status="PASS",
    )
    for row in audit["findings"]:
        row["proposed_full_cue"] = (
            f"第{row['cue_index']}条正式修复文本"
        )
        row["exact_release_adjudication"] = {
            "schema_version": "subtitle-span-adjudication.v1",
            "status": "OBSERVED",
            "repaired": True,
            "decision_authority": "CPA_JUDGE",
        }
    authority = _write_final_review_failure_surfaces(
        tmp_path,
        candidate_id,
        audit,
    )
    chat_document = json.loads(authority.read_text(encoding="utf-8"))
    chat_document["final_review_audit"]["carryover_persisted_count"] = 2
    authority.write_text(
        json.dumps(chat_document, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    carryover = authority.with_name(
        f"{candidate_id}.final-review-carryover.json"
    )
    carryover.write_text(
        json.dumps(
            {
                "schema_version": "final-review-carryover.v1",
                "findings": [
                    {
                        "cue": row["cue_index"],
                        "suspect": row["suspect"],
                        "proposed_full_cue": row["proposed_full_cue"],
                    }
                    for row in audit["findings"]
                ],
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    classified = runner.classify_talk_failure(
        "FINAL_REVIEW_RELEASE_BLOCKED: "
        "FINAL_REVIEW_UNRESOLVED_FINDINGS: "
        f"{authority}"
    )

    assert classified["failure_kind"] == "subtitle_authority"
    assert classified["failure_stage"] == "final_review_carryover"
    assert classified["failure_recoverable"] is True
    evidence = classified["failure_evidence"]
    assert evidence["audit_surfaces_consistent"] is True
    assert evidence["carryover"]["declared_count"] == 2
    assert evidence["carryover"]["observed_count"] == 2
    assert evidence["carryover"]["retry_ready"] is True


def test_mismatched_exact_final_carryover_stays_terminal(
    tmp_path: Path,
):
    candidate_id = "auto_bad_carryover"
    audit = _final_review_failure_audit(
        finding_count=1,
        boundary_status="PASS",
    )
    audit["findings"][0]["proposed_full_cue"] = "正确提案"
    audit["findings"][0]["exact_release_adjudication"] = {
        "schema_version": "subtitle-span-adjudication.v1",
        "status": "OBSERVED",
        "repaired": True,
    }
    authority = _write_final_review_failure_surfaces(
        tmp_path,
        candidate_id,
        audit,
    )
    chat_document = json.loads(authority.read_text(encoding="utf-8"))
    chat_document["final_review_audit"]["carryover_persisted_count"] = 1
    authority.write_text(
        json.dumps(chat_document, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    authority.with_name(
        f"{candidate_id}.final-review-carryover.json"
    ).write_text(
        json.dumps(
            {
                "schema_version": "final-review-carryover.v1",
                "findings": [
                    {
                        "cue": 1,
                        "suspect": audit["findings"][0]["suspect"],
                        "proposed_full_cue": "被篡改的提案",
                    }
                ],
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    classified = runner.classify_talk_failure(
        "FINAL_REVIEW_RELEASE_BLOCKED: "
        "FINAL_REVIEW_UNRESOLVED_FINDINGS: "
        f"{authority}"
    )

    assert classified["failure_stage"] == "final_review_findings"
    assert classified["failure_recoverable"] is False
    assert (
        classified["failure_evidence"]["carryover"]["retry_ready"]
        is False
    )


def test_malformed_correction_contract_is_terminal_not_provider_retry(
    tmp_path: Path,
):
    audit = _final_review_failure_audit(
        finding_count=0,
        boundary_status="PASS",
    )
    audit["reason_codes"] = [
        "FINAL_REVIEW_CORRECTION_MUTATION_AUTHORITY_INVALID"
    ]
    audit["correction_pass"] = {
        "schema_version": "final-review-audit.v1",
        "status": "CLEAN",
        "findings": [],
        "applied_count": 0,
    }
    audit["correction_mutation_authority"] = {
        "schema_version": "subtitle-correction-mutation-audit.v1",
        "status": "BLOCK",
        "applied_count": 0,
        "validated_mutation_count": 0,
        "failures": [
            {"reason_code": "CORRECTION_MUTATION_AUTHORITY_INVALID"}
        ],
    }
    authority = _write_final_review_failure_surfaces(
        tmp_path,
        "auto_correction_contract_failure",
        audit,
    )

    classified = runner.classify_talk_failure(
        "FINAL_REVIEW_RELEASE_BLOCKED: "
        "FINAL_REVIEW_CORRECTION_MUTATION_AUTHORITY_INVALID: "
        f"{authority}"
    )

    assert classified["failure_kind"] == "final_review_contract"
    assert classified["failure_stage"] == "final_review_contract"
    assert classified["failure_recoverable"] is False


def test_final_review_v1_schema_failure_is_terminal_not_provider_retry(
    tmp_path: Path,
):
    audit = _final_review_failure_audit(
        finding_count=0,
        boundary_status="PASS",
    )
    audit["schema_version"] = "final-review-audit.v1"
    authority = _write_final_review_failure_surfaces(
        tmp_path,
        "legacy-v1",
        audit,
    )

    classified = runner.classify_talk_failure(
        "FINAL_REVIEW_RELEASE_BLOCKED: FINAL_REVIEW_AUDIT_SCHEMA_INVALID: "
        f"{authority}"
    )

    assert classified["failure_kind"] == "final_review_contract"
    assert classified["failure_recoverable"] is False
    assert classified["failure_evidence"]["audit_loaded"] is False


def test_final_review_missing_or_inconsistent_audit_is_terminal_contract(
    tmp_path: Path,
):
    candidate_id = "auto_contract_failure"
    audit = _final_review_failure_audit(
        finding_count=0,
        boundary_status="PASS",
    )
    del audit["boundary_semantic_review"]
    authority = _write_final_review_failure_surfaces(
        tmp_path / "missing-boundary",
        candidate_id,
        audit,
    )

    missing_boundary = runner.classify_talk_failure(
        "FINAL_REVIEW_RELEASE_BLOCKED: FINAL_REVIEW_RELEASE_GATE_BLOCKED: "
        f"{authority}"
    )

    assert missing_boundary["failure_kind"] == "final_review_contract"
    assert missing_boundary["failure_recoverable"] is False

    unavailable_audit = {
        "schema_version": "final-review-audit.v2",
        "status": "AUDITOR_UNAVAILABLE",
        "release_gate": "BLOCK",
        "reason_codes": ["FINAL_REVIEW_RESPONSE_FINDINGS_MISSING"],
        "discovery": {"status": "AUDITOR_UNAVAILABLE"},
        "findings": [],
        "validated_finding_count": 0,
        "boundary_semantic_review": {"status": "PASS"},
    }
    inconsistent = _write_final_review_failure_surfaces(
        tmp_path / "inconsistent",
        candidate_id,
        unavailable_audit,
    )
    chat_document = json.loads(inconsistent.read_text(encoding="utf-8"))
    chat_document["final_review_audit"]["status"] = "FLAGGED"
    inconsistent.write_text(
        json.dumps(chat_document, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )

    surface_mismatch = runner.classify_talk_failure(
        "FINAL_REVIEW_RELEASE_BLOCKED: FINAL_REVIEW_RELEASE_GATE_BLOCKED: "
        f"{inconsistent}"
    )

    assert surface_mismatch["failure_kind"] == "final_review_contract"
    assert surface_mismatch["failure_recoverable"] is False
    assert surface_mismatch["failure_evidence"]["audit_surfaces_consistent"] is False


def test_final_review_failure_fingerprint_normalizes_run_root(
    tmp_path: Path,
):
    candidate_id = "auto_same_candidate"
    audit = _final_review_failure_audit(
        finding_count=1,
        boundary_status="PASS",
    )
    first_audit = json.loads(json.dumps(audit))
    first_audit["discovery"]["detail"] = (
        f"loaded from {tmp_path / 'run-a' / 'provider-response.json'}"
    )
    second_audit = json.loads(json.dumps(audit))
    second_audit["discovery"]["detail"] = (
        f"loaded from {tmp_path / 'run-b' / 'provider-response.json'}"
    )
    first_path = _write_final_review_failure_surfaces(
        tmp_path / "run-a",
        candidate_id,
        first_audit,
    )
    second_path = _write_final_review_failure_surfaces(
        tmp_path / "run-b",
        candidate_id,
        second_audit,
    )
    for chat_path, run_root in (
        (first_path, tmp_path / "run-a"),
        (second_path, tmp_path / "run-b"),
    ):
        chat_document = json.loads(chat_path.read_text(encoding="utf-8"))
        chat_document["ledger_path"] = str(
            run_root / "repo/assets/lidousha/subtitle_truth_ledger.v1.json"
        )
        chat_path.write_text(
            json.dumps(chat_document, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )

    first = runner.classify_talk_failure(
        "FINAL_REVIEW_RELEASE_BLOCKED: FINAL_REVIEW_RELEASE_GATE_BLOCKED: "
        f"{first_path}"
    )
    second = runner.classify_talk_failure(
        "FINAL_REVIEW_RELEASE_BLOCKED: FINAL_REVIEW_RELEASE_GATE_BLOCKED: "
        f"{second_path}"
    )

    assert first["failure_fingerprint"] == second["failure_fingerprint"]


def test_talk_failure_classifies_foreign_source_transcription_as_terminal():
    classified = runner.classify_talk_failure(
        "FOREIGN_SOURCE_TRANSCRIPTION_REQUIRED: /tmp/candidate.chat-authority.json"
    )

    assert classified["failure_kind"] == "subtitle_authority"
    assert classified["failure_stage"] == "foreign_source_transcription"
    assert classified["failure_recoverable"] is False


def test_talk_failure_persists_exact_foreign_source_gate_witness(tmp_path: Path):
    authority = tmp_path / "candidate.chat-authority.json"
    authority.write_text(
        json.dumps(
            {
                "input_srt_sha256": "sha256:input",
                "output_srt_sha256": "sha256:output",
                "foreign_script_consistency_audit": {
                    "mixed_cjk_latin_cues": [
                        {
                            "cue_index": 7,
                            "start_ms": 12_000,
                            "end_ms": 13_400,
                            "text": "错误 ll nn hhb",
                            "latin_words": ["ll", "nn", "hhb"],
                        }
                    ],
                    "audio_witness_rows": [
                        {
                            "cue_index": 7,
                            "witnessed": False,
                            "audio_sha256": "audio",
                        }
                    ],
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    classified = runner.classify_talk_failure(
        f"FOREIGN_SOURCE_TRANSCRIPTION_REQUIRED: {authority}"
    )

    violation = classified["gate_violation"]
    assert violation["token"] == "ll nn hhb"
    assert violation["cue_index"] == 7
    assert (violation["start_ms"], violation["end_ms"]) == (12_000, 13_400)
    assert violation["missing_witnesses"] == ["positive_source_audio_transcription"]
    assert violation["artifact_hashes"]["audio_sha256"] == "sha256:audio"


def test_foreign_source_rejection_reports_unwitnessed_cue_not_first_valid_english(
    tmp_path: Path,
):
    authority = tmp_path / "candidate.chat-authority.json"
    authority.write_text(
        json.dumps(
            {
                "foreign_script_consistency_audit": {
                    "mixed_cjk_latin_cues": [
                        {
                            "cue_index": 70,
                            "start_ms": 149_510,
                            "end_ms": 152_390,
                            "text": "我今天 I don't care",
                            "latin_words": ["I", "don't", "care"],
                        },
                        {
                            "cue_index": 95,
                            "start_ms": 215_490,
                            "end_ms": 217_850,
                            "text": "最后有 staff 跑过来说李豆沙你 BGM",
                            "latin_words": ["staff", "BGM"],
                        },
                    ],
                    "audio_witness_rows": [
                        {"cue_index": 70, "witnessed": True, "audio_sha256": "real"},
                        {"cue_index": 95, "witnessed": False, "audio_sha256": "false"},
                    ],
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    violation = runner.classify_talk_failure(
        f"FOREIGN_SOURCE_TRANSCRIPTION_REQUIRED: {authority}"
    )["gate_violation"]

    assert violation["token"] == "staff BGM"
    assert violation["cue_index"] == 95
    assert violation["unresolved_findings"] == [
        {
            "token": "staff BGM",
            "cue_index": 95,
            "start_ms": 215_490,
            "end_ms": 217_850,
            "text": "最后有 staff 跑过来说李豆沙你 BGM",
        }
    ]


def test_terminal_subtitle_authority_failure_backfills_without_weakening_gate():
    result = {
        "candidate_id": "blocked-subtitle",
        "status": "failed",
        "failure_kind": "subtitle_authority",
        "failure_stage": "foreign_source_transcription",
        "failure_recoverable": False,
    }

    assert runner.backfillable_talk_rejection(result) == (
        "failed",
        "subtitle_authority_unresolved_backfilled",
    )


def test_exact_selection_keeps_terminal_failure_requeueable():
    result = {
        "candidate_id": "blocked-subtitle",
        "status": "failed",
        "failure_kind": "subtitle_authority",
        "failure_stage": "foreign_source_transcription",
        "failure_recoverable": False,
    }

    rejected = runner.apply_talk_backfill_rejection_policy(
        result, exact_selected=True
    )

    assert rejected is False
    assert result["status"] == "failed"
    assert result["backfill_suppressed_by_exact_contract"]["reason"] == (
        "subtitle_authority_unresolved_backfilled"
    )


def test_ordinary_selection_materializes_backfillable_terminal_failure():
    result = {
        "candidate_id": "blocked-subtitle",
        "status": "failed",
        "failure_kind": "subtitle_authority",
        "failure_recoverable": False,
    }

    rejected = runner.apply_talk_backfill_rejection_policy(
        result, exact_selected=False
    )

    assert rejected is True
    assert result["status"] == "candidate_rejected"
    assert result["rejected_status"] == "failed"


@pytest.mark.parametrize(
    "message",
    [
        "SPEAKER_FINALIZATION_BLOCKED: SpeakerFinalizationError: "
        "CAM++ model tree drifted during speaker analysis",
        "SPEAKER_FINALIZATION_FAILED: remote worker exited before writing a manifest",
    ],
)
def test_talk_failure_does_not_launder_speaker_infrastructure_errors_as_evidence(message):
    classified = runner.classify_talk_failure(message)

    assert classified["failure_kind"] == "producer_error"
    assert classified["failure_stage"] == "unknown"
    assert classified["failure_recoverable"] is True


def test_redelivery_baseline_failure_is_not_unknown_retryable_producer_error():
    classified = runner.classify_talk_failure(
        "REDELIVERY_SUBTITLE_BASELINE_FAILED: "
        "/tmp/auto_193450_1863_2056.redelivery-baseline.json"
    )

    assert classified["failure_kind"] == "subtitle_authority"
    assert classified["failure_stage"] == "redelivery_subtitle_baseline"
    assert classified["failure_recoverable"] is False


def test_runtime_health_rejects_missing_tracked_speaker_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(
        runner,
        "HOST_VOCAL_PROFILE",
        tmp_path / "assets/lidousha/voiceprint_profile.v1.json",
    )
    (tmp_path / "scripts").mkdir(parents=True)
    (tmp_path / "scripts/produce_slice_package.py").write_text("# producer\n", encoding="utf-8")
    (tmp_path / "src/autoslice").mkdir(parents=True)
    (tmp_path / "src/autoslice/source_context_executor.py").write_text("# executor\n", encoding="utf-8")

    assert "speaker_profile" in runner.runtime_health_error()

    runner.HOST_VOCAL_PROFILE.parent.mkdir(parents=True)
    runner.HOST_VOCAL_PROFILE.write_text("{}\n", encoding="utf-8")
    assert runner.runtime_health_error() is None


def test_runtime_health_rejects_invalid_selection_calibration_before_work(
    tmp_path, monkeypatch
):
    from src.autoslice.selection_scorecard import SelectionCalibrationPolicyError

    speaker_profile = tmp_path / "assets/lidousha/voiceprint_profile.v1.json"
    speaker_profile.parent.mkdir(parents=True)
    speaker_profile.write_text("{}\n", encoding="utf-8")
    (tmp_path / "scripts").mkdir(parents=True)
    (tmp_path / "scripts/produce_slice_package.py").write_text(
        "# producer\n", encoding="utf-8"
    )
    (tmp_path / "src/autoslice").mkdir(parents=True)
    (tmp_path / "src/autoslice/source_context_executor.py").write_text(
        "# executor\n", encoding="utf-8"
    )
    monkeypatch.setattr(runner, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(runner, "HOST_VOCAL_PROFILE", speaker_profile)

    def invalid_policy():
        raise SelectionCalibrationPolicyError(
            "SELECTION_CALIBRATION_POLICY_JSON_INVALID",
            tmp_path / "selection.json",
        )

    monkeypatch.setattr(
        runner, "load_selected_selection_calibration_policy", invalid_policy
    )

    assert runner.runtime_health_error() == (
        "SELECTION_CALIBRATION_POLICY_INVALID: "
        f"SELECTION_CALIBRATION_POLICY_JSON_INVALID: {tmp_path / 'selection.json'}"
    )


def test_invalid_audio_lrc_observation_gets_one_same_fingerprint_retry(tmp_path, monkeypatch):
    date = "2026-07-10"
    rec_root = tmp_path / "recordings"
    date_dir = rec_root / date
    date_dir.mkdir(parents=True)
    segment = date_dir / "22966160_20260710-21-20-05.mp4"
    segment.write_bytes(b"media")
    monkeypatch.setattr(runner, "REC_ROOT", rec_root)
    monkeypatch.setattr(runner, "pipeline_fingerprint", lambda: "sha256:same")
    monkeypatch.setattr(runner, "ffprobe_ms", lambda _path: 500_000)
    monkeypatch.setattr(runner, "find_danmaku_xml", lambda _path: None)
    monkeypatch.setattr(runner, "find_chat_jsonl", lambda _path: None)
    record = {
        "candidate_id": "song_invalid_agy",
        "segment": segment.name,
        "start_ms": 100_000,
        "end_ms": 300_000,
        "status": "blocked",
        "reason_codes": ["SONG_AUDIO_LRC_ALIGNMENT_INVALID"],
        "pipeline_fingerprint": "sha256:same",
        "transient_retry_count": 0,
    }
    state = {"pending_song": [], "songs": [record]}

    assert runner.requeue_recoverable_songs(date, state) == 1
    assert state["pending_song"][0]["transient_retry_count"] == 1

    state = {"pending_song": [], "songs": [{**record, "transient_retry_count": 1}]}
    assert runner.requeue_recoverable_songs(date, state) == 0


def test_unexpected_song_crash_preserves_retry_reconstruction(tmp_path, monkeypatch):
    date = "2026-07-10"
    rec_root = tmp_path / "recordings"
    date_dir = rec_root / date
    date_dir.mkdir(parents=True)
    segment = date_dir / "22966160_20260710-20-00-09.mp4"
    segment.write_bytes(b"media")
    monkeypatch.setattr(runner, "REC_ROOT", rec_root)
    monkeypatch.setattr(runner, "pipeline_fingerprint", lambda: "sha256:same")
    monkeypatch.setattr(runner, "ffprobe_ms", lambda _path: 600_000)
    monkeypatch.setattr(runner, "find_danmaku_xml", lambda _path: None)
    monkeypatch.setattr(runner, "find_chat_jsonl", lambda _path: None)

    def crash(_date, _item):
        raise TimeoutError("worker vanished")

    item = {
        "cid": "song_crash",
        "segment_path": str(segment),
        "seg_dur_ms": 600_000,
        "anchor_start_ms": 200_000,
        "anchor_end_ms": 400_000,
        "hook": "《测试歌》",
        "lane": "visual_song_inventory",
        "title_hint": "测试歌",
        "visual_song_evidence": {"frame_ms": 205_000, "text": "测试歌"},
    }
    record = runner.produce_batch(date, [item], crash)[0]
    state = {"pending_song": [], "songs": [record]}

    assert record["reason_codes"] == ["PRODUCE_UNEXPECTED_EXCEPTION"]
    assert record["segment"] == segment.name
    assert runner.requeue_recoverable_songs(date, state) == 1
    assert state["pending_song"][0]["anchor_start_ms"] == 200_000
    assert state["pending_song"][0]["transient_retry_count"] == 1
    assert state["pending_song"][0]["title_hint"] == "测试歌"
    assert state["pending_song"][0]["visual_song_evidence"]["frame_ms"] == 205_000


def _write_test_boundary_owner_contract(
    *,
    candidate_root: Path,
    candidate_id: str,
    spec_path: Path,
) -> None:
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    durations = [
        int(piece["end_ms"]) - int(piece["start_ms"])
        for piece in spec["pieces"]
    ]
    audit: dict[str, object] = {}
    freeze_required_boundary_owner_contract(
        spec=spec,
        durations=durations,
        chat_authority_audit=audit,
        required_boundary_owners=[],
    )
    (candidate_root / f"{candidate_id}.chat-authority.json").write_text(
        json.dumps(audit, ensure_ascii=False),
        encoding="utf-8",
    )


def test_redelivery_baseline_is_text_authority_not_boundary_owner():
    owners = redelivery_baseline_boundary_owner(
        {
            "candidate_id": "candidate-a",
            "subtitle_redelivery_baseline": {
                "schema_version": "subtitle-redelivery-baseline.v2",
                "exact_interval_replay": True,
                "absolute_source_start_ms": 100_000,
                "absolute_source_end_ms": 130_000,
                "source_sha256": "sha256:" + "a" * 64,
            },
            "pieces": [
                {
                    "start_ms": 100_000,
                    "end_ms": 130_000,
                    "source_media_sha256": "sha256:" + "a" * 64,
                }
            ],
        },
        [30_000],
    )

    assert owners == []


@pytest.mark.parametrize("drift_kind", ["scope", "owner_set"])
def test_boundary_retry_frozen_scope_or_owner_drift_is_rejected(
    drift_kind,
):
    initial_spec = {
        "candidate_id": "candidate-a",
        "semantic_start_ms": 100_000,
        "semantic_end_ms": 110_000,
        "boundary_repair_extend_cap_ms": 30_000,
        "pieces": [{"start_ms": 95_000, "end_ms": 125_000}],
    }
    owners = [
        {
            "owner_kind": "source_subtitle_truth",
            "owner_id": "story-truth",
            "required": True,
            "source_start_ms": 104_000,
            "source_end_ms": 106_000,
            "local_windows": [{"start_ms": 9_000, "end_ms": 11_000}],
        }
    ]
    initial_audit: dict[str, object] = {}
    freeze_required_boundary_owner_contract(
        spec=initial_spec,
        durations=[30_000],
        chat_authority_audit=initial_audit,
        required_boundary_owners=owners,
    )
    expected = initial_audit["frozen_boundary_owner_contract"]

    retry_spec = {
        **initial_spec,
        "pieces": [{"start_ms": 95_000, "end_ms": 140_000}],
        "boundary_repair_extend_cap_ms": 60_000,
        "boundary_retry_frozen_owner_contract": expected,
    }
    retry_owners = [dict(owners[0])]
    if drift_kind == "scope":
        retry_spec["semantic_end_ms"] = 111_000
    else:
        retry_owners[0] = {
            **retry_owners[0],
            "owner_id": "later-candidate-truth",
        }

    with pytest.raises(
        RuntimeError,
        match="BOUNDARY_RETRY_OWNER_SET_DRIFT",
    ):
        freeze_required_boundary_owner_contract(
            spec=retry_spec,
            durations=[45_000],
            chat_authority_audit={},
            required_boundary_owners=retry_owners,
        )


def test_boundary_retry_only_widening_witness_context_preserves_owner_set():
    initial_spec = {
        "candidate_id": "candidate-a",
        "semantic_start_ms": 100_000,
        "semantic_end_ms": 110_000,
        "boundary_repair_extend_cap_ms": 30_000,
        "pieces": [{"start_ms": 95_000, "end_ms": 125_000}],
    }
    owners = [
        {
            "owner_kind": "source_subtitle_truth",
            "owner_id": "story-truth",
            "required": True,
            "source_start_ms": 104_000,
            "source_end_ms": 106_000,
            "local_windows": [{"start_ms": 9_000, "end_ms": 11_000}],
        }
    ]
    initial_audit: dict[str, object] = {}
    freeze_required_boundary_owner_contract(
        spec=initial_spec,
        durations=[30_000],
        chat_authority_audit=initial_audit,
        required_boundary_owners=[dict(owners[0])],
    )
    retry_spec = {
        **initial_spec,
        "pieces": [{"start_ms": 95_000, "end_ms": 140_000}],
        "boundary_repair_extend_cap_ms": 60_000,
        "boundary_retry_frozen_owner_contract": initial_audit[
            "frozen_boundary_owner_contract"
        ],
    }
    retry_audit: dict[str, object] = {}

    freeze_required_boundary_owner_contract(
        spec=retry_spec,
        durations=[45_000],
        chat_authority_audit=retry_audit,
        required_boundary_owners=[dict(owners[0])],
    )

    frozen = retry_audit["frozen_boundary_owner_contract"]
    assert (
        frozen["boundary_retry_owner_contract_verification"]["status"]
        == "PASS"
    )
    assert frozen["owner_set_sha256"] == (
        initial_audit["frozen_boundary_owner_contract"][
            "owner_set_sha256"
        ]
    )


def test_boundary_retry_owner_set_drift_is_terminal_pipeline_contract():
    classified = runner.classify_talk_failure(
        "BOUNDARY_RETRY_OWNER_SET_DRIFT: owner id/window changed"
    )

    assert classified["failure_kind"] == "pipeline_contract"
    assert classified["failure_stage"] == "boundary_retry_owner_contract"
    assert classified["failure_recoverable"] is False


def test_talk_boundary_failure_widens_original_source_and_retries(tmp_path, monkeypatch):
    date = "2026-07-10"
    base = tmp_path / "autoslice"
    repo = tmp_path / "repo"
    (base / "logs").mkdir(parents=True)
    repo.mkdir()
    override = repo / "assets/lidousha/subtitle_text_overrides/auto_212005_163_311.text.v1.json"
    override.parent.mkdir(parents=True)
    override.write_text("{}\n", encoding="utf-8")
    regression = repo / "assets/lidousha/subtitle_regressions/auto_212005_163_311.subtitle-regression.v1.json"
    regression.parent.mkdir(parents=True)
    regression.write_text("{}\n", encoding="utf-8")
    baseline_root = repo / "assets/lidousha/reviewed_subtitle_baselines"
    baseline_root.mkdir(parents=True)
    baseline = baseline_root / "auto_212005_163_311.reviewed.srt"
    baseline.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n已审文本\n",
        encoding="utf-8",
    )
    (baseline_root / "auto_212005_163_311.subtitle-baseline.v1.json").write_text(
        json.dumps(
            {
                "registry_schema_version": "candidate-reviewed-subtitle-baseline.v1",
                "candidate_id": "auto_212005_163_311",
                "schema_version": "subtitle-redelivery-baseline.v2",
                "mode": "preserve_text_outside_source_truth",
                "path": baseline.name,
                "sha256": hashlib.sha256(baseline.read_bytes()).hexdigest(),
                "authority": "Ivan reviewed delivery",
                "source_recording_basename": "22966160_20260710-21-20-05.mp4",
                "source_sha256": "c" * 64,
                "absolute_source_start_ms": 163_000,
                "absolute_source_end_ms": 164_000,
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(runner, "BASE", base)
    monkeypatch.setattr(runner, "REPO_ROOT", repo)
    monkeypatch.setattr(runner, "child_env", lambda: {})
    monkeypatch.setattr(runner, "pipeline_fingerprint", lambda: "sha256:test")
    calls = []

    class Completed:
        def __init__(self, returncode):
            self.returncode = returncode

    def fake_run(command, **kwargs):
        calls.append(command)
        sink = kwargs["stdout"]
        if len(calls) == 1:
            cid = "auto_212005_163_311"
            candidate_root = base / "out" / date / cid
            candidate_root.mkdir(parents=True, exist_ok=True)
            scope = build_boundary_search_scope(
                semantic_target_ms=158_000,
                repair_cap_ms=30_000,
                last_piece_start_ms=153_000,
            )
            (
                candidate_root / f"{cid}.review-flags.json"
            ).write_text(
                json.dumps(
                    {
                        "boundary_semantic_review": {
                            "schema_version": (
                                "talk-boundary-semantic-review.v1"
                            ),
                            "status": "BLOCK",
                            "needs_more_context": True,
                            "retry_scope": "same_topic_continues",
                            "boundary_search_scope": scope,
                        }
                    }
                ),
                encoding="utf-8",
            )
            _write_test_boundary_owner_contract(
                candidate_root=candidate_root,
                candidate_id=cid,
                spec_path=Path(command[command.index("--spec") + 1]),
            )
            sink.write(
                "BOUNDARY_CONTEXT_EXHAUSTED: no closure in current source context; "
                "reason_codes=STORY_CLOSED_NOT_PROVEN "
                "retry_scope=same_topic_continues\n"
            )
            sink.flush()
            return Completed(1)
        sink.write('{"red_flags": [], "boundary_repairs": [{"snapped_end_ms": 182540}]}\n')
        sink.flush()
        return Completed(0)

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    result = runner.produce_talk(
        date,
        {
            "cid": "auto_212005_163_311",
            "segment_path": "/recordings/22966160_20260710-21-20-05.mp4",
            "seg_dur_ms": 900_000,
            "start_ms": 163_000,
            "end_ms": 311_000,
            "xml": None,
            "chat_jsonl": None,
            "hook": "小李嘴硬",
            "confidence": 0.9,
            "lane": "semantic",
        },
    )

    assert len(calls) == 2
    assert result["status"] == runner.TALK_COVER_PENDING_STATUS
    assert result["boundary_context_retries"] == 1
    spec = json.loads((base / "out" / date / "spec_auto_212005_163_311.json").read_text())
    assert spec["semantic_end_ms"] == 311_000
    assert spec["pieces"][0]["end_ms"] == 386_000
    assert spec["boundary_repair_extend_cap_ms"] == 60_000
    assert spec["subtitle_text_overrides"] == str(override)
    assert spec["subtitle_regression"] == str(regression)
    assert spec["subtitle_redelivery_baseline"]["schema_version"] == (
        "subtitle-redelivery-baseline.v2"
    )
    assert spec["subtitle_redelivery_baseline"]["path"] == str(baseline.resolve())


def test_boundary_retry_keeps_ceiling_plus_next_topic_witness(tmp_path):
    """Retry source context follows the bound scope, not a legacy +90s guess."""

    cid = "generic_bound_scope"
    out_root = tmp_path / "out"
    candidate_root = out_root / cid
    candidate_root.mkdir(parents=True)
    scope = build_boundary_search_scope(
        semantic_target_ms=202_720,
        manual_lower_bound_ms=230_760,
        required_owner_end_ms=230_760,
        repair_cap_ms=30_000,
        last_piece_start_ms=1_853_760,
    )
    (candidate_root / f"{cid}.review-flags.json").write_text(
        json.dumps(
            {
                "boundary_semantic_review": {
                    "schema_version": (
                        "talk-boundary-semantic-review.v1"
                    ),
                    "status": "BLOCK",
                    "needs_more_context": True,
                    "boundary_search_scope": scope,
                }
            }
        ),
        encoding="utf-8",
    )

    assert _bound_boundary_retry_source_end_ms(
        out_root=out_root,
        candidate_id=cid,
        repair_cap_ms=60_000,
    ) == 2_159_520


def test_source_witness_reserve_retry_materializes_1573_scope_once(
    tmp_path,
    monkeypatch,
):
    date = "2026-07-22"
    cid = "auto_193450_1573_1672"
    base = tmp_path / "autoslice"
    repo = tmp_path / "repo"
    (base / "logs").mkdir(parents=True)
    repo.mkdir()
    monkeypatch.setattr(runner, "BASE", base)
    monkeypatch.setattr(runner, "REPO_ROOT", repo)
    monkeypatch.setattr(runner, "child_env", lambda: {})
    monkeypatch.setattr(runner, "pipeline_fingerprint", lambda: "sha256:test")
    calls = []

    class Completed:
        def __init__(self, returncode):
            self.returncode = returncode

    def fake_run(command, **kwargs):
        calls.append(command)
        sink = kwargs["stdout"]
        if len(calls) == 1:
            candidate_root = base / "out" / date / cid
            candidate_root.mkdir(parents=True, exist_ok=True)
            scope = build_boundary_search_scope(
                semantic_target_ms=109_820,
                manual_lower_bound_ms=116_840,
                structured_payoff_ms=116_830,
                required_owner_end_ms=116_830,
                repair_cap_ms=30_000,
                last_piece_start_ms=1_563_150,
            )
            (
                candidate_root / f"{cid}.review-flags.json"
            ).write_text(
                json.dumps(
                    {
                        "boundary_semantic_review": {
                            "schema_version": (
                                "talk-boundary-semantic-review.v1"
                            ),
                            "status": "BLOCK",
                            "needs_more_context": True,
                            "retry_scope": "source_witness_reserve",
                            "boundary_search_scope": scope,
                        }
                    }
                ),
                encoding="utf-8",
            )
            _write_test_boundary_owner_contract(
                candidate_root=candidate_root,
                candidate_id=cid,
                spec_path=Path(command[command.index("--spec") + 1]),
            )
            sink.write(
                "BOUNDARY_CONTEXT_EXHAUSTED: "
                '["BOUNDARY_SOURCE_WITNESS_RESERVE_INCOMPLETE"] '
                "max_forward_ms=30000 "
                "retry_scope=source_witness_reserve\n"
            )
            sink.flush()
            return Completed(1)
        sink.write(
            '{"red_flags": [], "boundary_repairs": '
            '[{"snapped_end_ms": 141410}]}\n'
        )
        sink.flush()
        return Completed(0)

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    result = runner.produce_talk(
        date,
        {
            "cid": cid,
            "segment_path": "/recordings/segment.mp4",
            "seg_dur_ms": 7_250_082,
            "start_ms": 1_573_150,
            "end_ms": 1_672_970,
            "hook": "椅子反差",
            "given_end_ms": 1_679_990,
            "given_end_authority": "Ivan-reviewed source closure",
        },
    )

    assert len(calls) == 2
    assert result["boundary_context_retries"] == 1
    spec = json.loads(
        (base / "out" / date / f"spec_{cid}.json").read_text()
    )
    assert spec["semantic_end_ms"] == 1_672_970
    assert spec["given_end_ms"] == 1_679_990
    assert spec["pieces"][0]["end_ms"] == 1_754_990
    assert spec["boundary_repair_extend_cap_ms"] == 60_000


def _cross_segment_reserve_fixture(tmp_path, monkeypatch, *, next_segment_name):
    """Shared scaffolding for the cross-segment witness-reserve retry tests.

    ``scoped_retry_end`` (386_000ms, from the fixed scope below) exceeds the
    300_000ms ``seg_dur_ms`` — the exact BOUNDARY_SOURCE_WITNESS_RESERVE
    condition that spills past a segment's own recording file near a
    ~30-minute rotation boundary.
    """

    date = "2026-07-24"
    cid = "auto_190124_1571_1804"
    base = tmp_path / "autoslice"
    repo = tmp_path / "repo"
    (base / "logs").mkdir(parents=True)
    repo.mkdir()
    monkeypatch.setattr(runner, "BASE", base)
    monkeypatch.setattr(runner, "REPO_ROOT", repo)
    monkeypatch.setattr(runner, "child_env", lambda: {})
    monkeypatch.setattr(runner, "pipeline_fingerprint", lambda: "sha256:test")

    recordings = tmp_path / "recordings"
    current_segment = recordings / "22966160_20260724-19-01-24.mp4"
    next_segment = recordings / next_segment_name
    monkeypatch.setattr(
        runner,
        "list_segments",
        lambda requested_date: [current_segment, next_segment],
    )

    calls = []

    class Completed:
        def __init__(self, returncode):
            self.returncode = returncode

    def fake_run(command, **kwargs):
        calls.append(command)
        sink = kwargs["stdout"]
        if len(calls) == 1:
            candidate_root = base / "out" / date / cid
            candidate_root.mkdir(parents=True, exist_ok=True)
            scope = build_boundary_search_scope(
                semantic_target_ms=158_000,
                repair_cap_ms=30_000,
                last_piece_start_ms=153_000,
            )
            (candidate_root / f"{cid}.review-flags.json").write_text(
                json.dumps(
                    {
                        "boundary_semantic_review": {
                            "schema_version": (
                                "talk-boundary-semantic-review.v1"
                            ),
                            "status": "BLOCK",
                            "needs_more_context": True,
                            "retry_scope": "source_witness_reserve",
                            "boundary_search_scope": scope,
                        }
                    }
                ),
                encoding="utf-8",
            )
            _write_test_boundary_owner_contract(
                candidate_root=candidate_root,
                candidate_id=cid,
                spec_path=Path(command[command.index("--spec") + 1]),
            )
            sink.write(
                "BOUNDARY_CONTEXT_EXHAUSTED: "
                '["BOUNDARY_SOURCE_WITNESS_RESERVE_INCOMPLETE"] '
                "max_forward_ms=30000 "
                "retry_scope=source_witness_reserve\n"
            )
            sink.flush()
            return Completed(1)
        sink.write(
            '{"red_flags": [], "boundary_repairs": [{"snapped_end_ms": 195000}]}\n'
        )
        sink.flush()
        return Completed(0)

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    item = {
        "cid": cid,
        "segment_path": str(current_segment),
        "seg_dur_ms": 300_000,
        "start_ms": 100_000,
        "end_ms": 158_000,
        "hook": "跨段见证储备",
    }
    return date, cid, base, calls, item


def test_boundary_retry_appends_cross_segment_reserve_when_wallclock_continuous(
    tmp_path, monkeypatch,
):
    date, cid, base, calls, item = _cross_segment_reserve_fixture(
        tmp_path,
        monkeypatch,
        # 19:01:24 + seg_dur_ms(300_000ms=5min) == 19:06:24 exactly: proven
        # wall-clock-continuous recording, no gap/restart.
        next_segment_name="22966160_20260724-19-06-24.mp4",
    )

    result = runner.produce_talk(date, item)

    assert len(calls) == 2
    assert result["boundary_context_retries"] == 1
    spec = json.loads((base / "out" / date / f"spec_{cid}.json").read_text())
    # Widened to the full in-segment ceiling (300_000ms is all this segment
    # file has), then the remaining deficit (386_000 - 300_000 = 86_000)
    # plus the 2_000ms margin is covered by the appended reserve piece.
    assert spec["pieces"][0]["end_ms"] == 300_000
    assert len(spec["pieces"]) == 2
    reserve = spec["pieces"][1]
    assert reserve["piece_role"] == "boundary_witness_reserve"
    assert reserve["remote_media"].endswith("22966160_20260724-19-06-24.mp4")
    assert reserve["start_ms"] == 0
    assert reserve["end_ms"] == 88_000
    assert spec["boundary_repair_extend_cap_ms"] == 60_000
    # No committed chat ledger/sidecar exists for this synthetic next segment
    # — the reserve piece must disclose the gap rather than fabricate one.
    assert spec["reserve_chat_binding_absent"] is True
    log_text = (base / "logs" / f"{date}_{cid}.log").read_text(encoding="utf-8")
    assert "reserve_next_segment=22966160_20260724-19-06-24.mp4" in log_text
    assert "reserve_gap_ms=86000" in log_text


def test_boundary_retry_refuses_cross_segment_reserve_when_wallclock_discontinuous(
    tmp_path, monkeypatch,
):
    date, cid, base, calls, item = _cross_segment_reserve_fixture(
        tmp_path,
        monkeypatch,
        # 19:01:24 + 5min would land at 19:06:24; this next segment starts
        # at 19:40:24 instead — a 34-minute gap far past the 5s tolerance,
        # i.e. the recording actually stopped and restarted.
        next_segment_name="22966160_20260724-19-40-24.mp4",
    )

    result = runner.produce_talk(date, item)

    assert len(calls) == 1
    assert result["boundary_context_retries"] == 0
    assert result["status"] == "boundary_unrepairable"
    assert result["failure_evidence"]["retry_scope"] == "source_witness_reserve"
    spec = json.loads((base / "out" / date / f"spec_{cid}.json").read_text())
    assert len(spec["pieces"]) == 1
    assert spec["boundary_repair_extend_cap_ms"] == 30_000
    assert "reserve_chat_binding_absent" not in spec


def test_talk_boundary_context_exhausted_retries_once_then_stops(
    tmp_path,
    monkeypatch,
):
    date = "2026-07-10"
    base = tmp_path / "autoslice"
    repo = tmp_path / "repo"
    (base / "logs").mkdir(parents=True)
    repo.mkdir()
    monkeypatch.setattr(runner, "BASE", base)
    monkeypatch.setattr(runner, "REPO_ROOT", repo)
    monkeypatch.setattr(runner, "child_env", lambda: {})
    monkeypatch.setattr(runner, "pipeline_fingerprint", lambda: "sha256:test")
    calls = []

    class Completed:
        returncode = 1

    def fake_run(command, **kwargs):
        calls.append(command)
        if len(calls) == 1:
            cid = "auto_context_exhausted"
            candidate_root = base / "out" / date / cid
            candidate_root.mkdir(parents=True, exist_ok=True)
            scope = build_boundary_search_scope(
                semantic_target_ms=60_000,
                repair_cap_ms=30_000,
                last_piece_start_ms=90_000,
            )
            (
                candidate_root / f"{cid}.review-flags.json"
            ).write_text(
                json.dumps(
                    {
                        "boundary_semantic_review": {
                            "schema_version": (
                                "talk-boundary-semantic-review.v1"
                            ),
                            "status": "BLOCK",
                            "needs_more_context": True,
                            "retry_scope": "same_topic_continues",
                            "boundary_search_scope": scope,
                        }
                    }
                ),
                encoding="utf-8",
            )
            _write_test_boundary_owner_contract(
                candidate_root=candidate_root,
                candidate_id=cid,
                spec_path=Path(command[command.index("--spec") + 1]),
            )
        kwargs["stdout"].write(
            "BOUNDARY_CONTEXT_EXHAUSTED: no closed ending after expanded context; "
            "reason_codes=STORY_CLOSED_NOT_PROVEN,"
            "NEXT_TOPIC_SEPARATED_NOT_PROVEN "
            "retry_scope=same_topic_continues\n"
        )
        kwargs["stdout"].flush()
        return Completed()

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    result = runner.produce_talk(
        date,
        {
            "cid": "auto_context_exhausted",
            "segment_path": "/recordings/segment.mp4",
            "seg_dur_ms": 900_000,
            "start_ms": 100_000,
            "end_ms": 150_000,
            "hook": "同一话题仍未收束",
        },
    )

    assert len(calls) == 2
    assert result["status"] == "boundary_unrepairable"
    assert result["boundary_context_retries"] == 1
    assert result["failure_kind"] == "content_boundary"
    assert result["failure_stage"] == "boundary_semantic_review"
    assert result["failure_recoverable"] is False
    assert result["failure_evidence"]["retry_scope"] == "same_topic_continues"
    assert result["failure_evidence"]["reason_codes"] == [
        "STORY_CLOSED_NOT_PROVEN",
        "NEXT_TOPIC_SEPARATED_NOT_PROVEN",
    ]
    assert "next_retry_at" not in result


def test_talk_boundary_retry_refuses_missing_bound_scope(
    tmp_path,
    monkeypatch,
):
    date = "2026-07-10"
    base = tmp_path / "autoslice"
    repo = tmp_path / "repo"
    (base / "logs").mkdir(parents=True)
    repo.mkdir()
    monkeypatch.setattr(runner, "BASE", base)
    monkeypatch.setattr(runner, "REPO_ROOT", repo)
    monkeypatch.setattr(runner, "child_env", lambda: {})
    monkeypatch.setattr(
        runner, "pipeline_fingerprint", lambda: "sha256:test"
    )
    calls = []

    class Completed:
        returncode = 1

    def fake_run(command, **kwargs):
        calls.append(command)
        kwargs["stdout"].write(
            "BOUNDARY_CONTEXT_EXHAUSTED: "
            '["STORY_CLOSED_NOT_PROVEN"] '
            "max_forward_ms=30000 "
            "retry_scope=same_topic_continues\n"
        )
        kwargs["stdout"].flush()
        return Completed()

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    result = runner.produce_talk(
        date,
        {
            "cid": "auto_missing_bound_scope",
            "segment_path": "/recordings/segment.mp4",
            "seg_dur_ms": 900_000,
            "start_ms": 100_000,
            "end_ms": 150_000,
            "hook": "缺失 scope 不得扩窗",
        },
    )

    assert len(calls) == 1
    assert result["boundary_context_retries"] == 0
    spec = json.loads(
        (
            base
            / "out"
            / date
            / "spec_auto_missing_bound_scope.json"
        ).read_text()
    )
    assert spec["pieces"][0]["end_ms"] == 198_000
    assert spec["boundary_repair_extend_cap_ms"] == 30_000


def test_boundary_context_failure_preserves_json_reason_codes():
    classified = runner.classify_talk_failure(
        'BOUNDARY_CONTEXT_EXHAUSTED: ["STORY_CLOSED_NOT_PROVEN", '
        '"NEXT_TOPIC_SEPARATED_NOT_PROVEN"] max_forward_ms=60000 '
        "retry_scope=same_topic_continues"
    )

    assert classified["failure_evidence"]["reason_codes"] == [
        "STORY_CLOSED_NOT_PROVEN",
        "NEXT_TOPIC_SEPARATED_NOT_PROVEN",
    ]


def test_talk_boundary_failure_does_not_retry_without_continuation_scope(tmp_path, monkeypatch):
    date = "2026-07-10"
    base = tmp_path / "autoslice"
    repo = tmp_path / "repo"
    (base / "logs").mkdir(parents=True)
    repo.mkdir()
    monkeypatch.setattr(runner, "BASE", base)
    monkeypatch.setattr(runner, "REPO_ROOT", repo)
    monkeypatch.setattr(runner, "child_env", lambda: {})
    monkeypatch.setattr(runner, "pipeline_fingerprint", lambda: "sha256:test")
    calls = []

    class Completed:
        returncode = 1

    def fake_run(command, **kwargs):
        calls.append(command)
        kwargs["stdout"].write(
            "BOUNDARY_UNREPAIRABLE: distinct next island; "
            "extend_cap=30000ms retry_scope=none\n"
        )
        kwargs["stdout"].flush()
        return Completed()

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    result = runner.produce_talk(
        date,
        {
            "cid": "auto_distinct_next_topic",
            "segment_path": "/recordings/segment.mp4",
            "seg_dur_ms": 900_000,
            "start_ms": 100_000,
            "end_ms": 150_000,
            "hook": "已经收束的话题",
        },
    )

    assert len(calls) == 1
    assert result["status"] == "boundary_unrepairable"
    assert result["boundary_context_retries"] == 0
    spec = json.loads((base / "out" / date / "spec_auto_distinct_next_topic.json").read_text())
    assert spec["pieces"][0]["end_ms"] == 198_000
    assert spec["boundary_repair_extend_cap_ms"] == 30_000


def test_talk_title_authority_failure_is_classified_and_leaves_no_delivery(tmp_path, monkeypatch):
    date = "2026-07-10"
    base = tmp_path / "autoslice"
    repo = tmp_path / "repo"
    (base / "logs").mkdir(parents=True)
    repo.mkdir()
    monkeypatch.setattr(runner, "BASE", base)
    monkeypatch.setattr(runner, "REPO_ROOT", repo)
    monkeypatch.setattr(runner, "child_env", lambda: {})
    monkeypatch.setattr(runner, "pipeline_fingerprint", lambda: "sha256:test")

    cid = "auto_title_blocked"
    hook = "弹幕让李豆沙表演上下摇"
    delivery_name = safe_name(hook, cid)

    class Completed:
        returncode = 1

    def fake_run(_command, **kwargs):
        recuts = base / "out" / date / cid / "replacement_recuts"
        recuts.mkdir(parents=True)
        (recuts / f"{cid}.publish.json").write_text(
            json.dumps(
                {
                    "title": cid,
                    "title_authority_status": "UNRESOLVED_AUTO",
                    "title_authority_error": "title_policy_violation",
                    "artifact_hashes": {},
                }
            ),
            encoding="utf-8",
        )
        delivery = repo / "lidousha" / date
        delivery.mkdir(parents=True)
        (delivery / f"{delivery_name}.mp4").write_bytes(b"stale")
        kwargs["stdout"].write("TITLE_AUTHORITY_UNRESOLVED: title_policy_violation\n")
        kwargs["stdout"].flush()
        return Completed()

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    result = runner.produce_talk(
        date,
        {
            "cid": cid,
            "segment_path": "/recordings/segment.mp4",
            "seg_dur_ms": 900_000,
            "start_ms": 100_000,
            "end_ms": 150_000,
            "xml": None,
            "chat_jsonl": None,
            "hook": hook,
            "confidence": 0.9,
            "lane": "semantic",
        },
    )

    assert result["status"] == "title_failed"
    assert not list((repo / "lidousha" / date).glob(f"{delivery_name}.*"))
    assert not (base / "out" / date / cid / "replacement_recuts").exists()


def test_stale_boundary_log_does_not_classify_new_transient_failure(tmp_path, monkeypatch):
    date = "2026-07-10"
    base = tmp_path / "autoslice"
    repo = tmp_path / "repo"
    (base / "logs").mkdir(parents=True)
    repo.mkdir()
    monkeypatch.setattr(runner, "BASE", base)
    monkeypatch.setattr(runner, "REPO_ROOT", repo)
    monkeypatch.setattr(runner, "child_env", lambda: {})
    monkeypatch.setattr(runner, "pipeline_fingerprint", lambda: "sha256:test")
    log_path = base / "logs" / f"{date}_auto_stale.log"
    log_path.write_text("BOUNDARY_UNREPAIRABLE: old attempt\n", encoding="utf-8")
    stale_recuts = base / "out" / date / "auto_stale" / "replacement_recuts"
    stale_recuts.mkdir(parents=True)
    (stale_recuts / "auto_stale.speaker-final.json").write_text(
        json.dumps(
            {
                "status": "SPEAKER_REVIEW_REQUIRED",
                "production_ready": False,
                "reason": "old attempt",
            }
        ),
        encoding="utf-8",
    )
    calls = []

    class Completed:
        returncode = 1

    def fake_run(command, **kwargs):
        calls.append(command)
        kwargs["stdout"].write("CPA temporarily unavailable\n")
        kwargs["stdout"].flush()
        return Completed()

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    result = runner.produce_talk(
        date,
        {
            "cid": "auto_stale",
            "segment_path": "/recordings/segment.mp4",
            "seg_dur_ms": 900_000,
            "start_ms": 100_000,
            "end_ms": 200_000,
            "hook": "test",
            "cover_route_regeneration_fingerprint": "sha256:cover-build",
            "cover_route_regeneration_attempts": 3,
        },
    )

    assert len(calls) == 1
    assert result["status"] == "failed"
    assert result["boundary_context_retries"] == 0
    assert result["cover_route_regeneration_fingerprint"] == (
        "sha256:cover-build"
    )
    assert result["cover_route_regeneration_attempts"] == 3
    assert "speaker_review_manifest" not in result


def test_pipeline_change_requeues_old_selected_boundary_failure(tmp_path, monkeypatch):
    date = "2026-07-10"
    rec_root = tmp_path / "recordings"
    date_dir = rec_root / date
    date_dir.mkdir(parents=True)
    segment = date_dir / "22966160_20260710-21-20-05.mp4"
    segment.write_bytes(b"media")
    monkeypatch.setattr(runner, "REC_ROOT", rec_root)
    monkeypatch.setattr(runner, "pipeline_fingerprint", lambda: "sha256:new")
    monkeypatch.setattr(runner, "ffprobe_ms", lambda _path: 900_000)
    monkeypatch.setattr(runner, "find_danmaku_xml", lambda _path: None)
    monkeypatch.setattr(runner, "find_chat_jsonl", lambda _path: None)
    state = {
        "pending_talk": [],
        "picks": [
            {
                "candidate_id": "auto_212005_163_311",
                "segment": segment.name,
                "start_ms": 163_000,
                "end_ms": 311_000,
                "given_end_ms": 318_000,
                "given_end_authority": "Ivan reviewed complete closing sentence",
                "status": "boundary_unrepairable",
                "pipeline_fingerprint": "sha256:old",
                "hook": "小李嘴硬",
                "confidence": 0.94,
                "session_id": "live-20260710T200000+0800",
                "cover_route_regeneration_fingerprint": "sha256:cover-old",
                "cover_route_regeneration_attempts": 2,
            },
            {"candidate_id": "delivered", "status": "review_ready"},
        ],
    }

    assert runner.requeue_recoverable_talks(date, state) == 1
    assert [row["candidate_id"] for row in state["picks"]] == ["delivered"]
    assert state["pending_talk"][0]["selected_repair"] is True
    assert state["pending_talk"][0]["talk_repair_retry_count"] == 1
    assert state["pending_talk"][0]["session_id"] == "live-20260710T200000+0800"
    assert state["pending_talk"][0][
        "cover_route_regeneration_fingerprint"
    ] == "sha256:cover-old"
    assert state["pending_talk"][0]["cover_route_regeneration_attempts"] == 2
    assert state["pending_talk"][0]["given_end_ms"] == 318_000
    assert state["pending_talk"][0]["given_end_authority"] == (
        "Ivan reviewed complete closing sentence"
    )
    assert state["talk_superseded_attempts"][0]["superseded_by"] == "sha256:new"
    assert state["talk_superseded_attempts"][0]["session_id"] == "live-20260710T200000+0800"


def test_requeue_recoverable_talk_preserves_state_when_mount_drops_mid_tick(
    tmp_path, monkeypatch
):
    date = "2026-07-10"
    rec_root = tmp_path / "recordings"
    date_dir = rec_root / date
    date_dir.mkdir(parents=True)
    segment = date_dir / "22966160_20260710-21-20-05.mp4"
    segment.write_bytes(b"media")
    monkeypatch.setattr(runner, "REC_ROOT", rec_root)
    monkeypatch.setattr(
        runner, "talk_pipeline_fingerprint", lambda _cid: "sha256:new"
    )
    monkeypatch.setattr(
        runner,
        "talk_failure_recovery_fingerprint",
        lambda _kind, _cid: "sha256:new-recovery",
    )
    original_is_file = Path.is_file

    def mount_drops(path):
        if path == segment:
            raise OSError(131, "State not recoverable", str(path))
        return original_is_file(path)

    monkeypatch.setattr(Path, "is_file", mount_drops)
    state = {
        "pending_talk": [],
        "picks": [
            {
                "candidate_id": "selected",
                "segment": segment.name,
                "start_ms": 100_000,
                "end_ms": 200_000,
                "status": "failed",
                "failure_kind": "runtime_prerequisite",
                "failure_recoverable": True,
                "failure_recovery_fingerprint": "sha256:old-recovery",
                "pipeline_fingerprint": "sha256:old",
            }
        ],
    }

    assert runner.requeue_recoverable_talks(date, state) == 0
    assert state["pending_talk"] == []
    assert state["picks"][0]["candidate_id"] == "selected"
    assert state["picks"][0]["recovery_source_status"] == (
        "SOURCE_RECORDING_ROOT_UNAVAILABLE"
    )


def test_pipeline_change_requeues_legacy_exact_candidate_rejection(
    tmp_path, monkeypatch
):
    date = "2026-07-10"
    rec_root = tmp_path / "recordings"
    date_dir = rec_root / date
    date_dir.mkdir(parents=True)
    segment = date_dir / "segment.mp4"
    segment.write_bytes(b"media")
    monkeypatch.setattr(runner, "REC_ROOT", rec_root)
    monkeypatch.setattr(
        runner, "talk_pipeline_fingerprint", lambda _cid: "sha256:new"
    )
    current_recovery = {"value": "sha256:old-recovery"}
    monkeypatch.setattr(
        runner,
        "talk_failure_recovery_fingerprint",
        lambda _kind, _cid: current_recovery["value"],
    )
    monkeypatch.setattr(runner, "ffprobe_ms", lambda _path: 900_000)
    monkeypatch.setattr(runner, "find_danmaku_xml", lambda _path: None)
    monkeypatch.setattr(
        runner,
        "resolve_structured_chat_binding",
        lambda _segment, **_kwargs: {
            "structured_chat_required": False,
            "chat_binding_status": "NOT_REGISTERED",
        },
    )
    state = {
        "run_mode": "RECOVERY_REVIEW",
        "upload_allowed": False,
        "talk_selection_contract": {
            "schema_version": "talk-selection-contract.v1",
            "mode": "EXACT_CANDIDATE_SET_NO_BACKFILL",
            "candidate_ids": ["selected"],
            "source_state_sha256": "sha256:" + "a" * 64,
            "authority": "Ivan selected the exact recovery set",
        },
        "pending_talk": [],
        "picks": [
            {
                "candidate_id": "selected",
                "segment": segment.name,
                "start_ms": 100_000,
                "end_ms": 200_000,
                "status": "candidate_rejected",
                "rejected_status": "failed",
                "rejection_reason": (
                    "subtitle_authority_unresolved_backfilled"
                ),
                "failure_kind": "subtitle_authority",
                "failure_recoverable": False,
                "failure_recovery_fingerprint": "sha256:old-recovery",
                "pipeline_fingerprint": "sha256:old",
            }
        ],
    }

    assert runner.requeue_recoverable_talks(date, state) == 0
    assert state["picks"][0]["status"] == "candidate_rejected"

    current_recovery["value"] = "sha256:new-recovery"
    assert runner.requeue_recoverable_talks(date, state) == 1
    assert state["picks"] == []
    assert state["pending_talk"][0]["cid"] == "selected"

    assert state["pending_talk"][0]["retry_reason"] == (
        "pipeline_fingerprint_changed"
    )
    assert state["talk_superseded_attempts"][0]["status"] == (
        "candidate_rejected"
    )


def _requeue_gating_state(tmp_path, monkeypatch, *, failure_kind, transient_count):
    date = "2026-07-25"
    rec_root = tmp_path / "recordings"
    date_dir = rec_root / date
    date_dir.mkdir(parents=True)
    segment = date_dir / "22966160_20260725-19-20-00.mp4"
    segment.write_bytes(b"media")
    monkeypatch.setattr(runner, "REC_ROOT", rec_root)
    monkeypatch.setattr(
        runner, "talk_pipeline_fingerprint", lambda _cid: "sha256:same"
    )
    monkeypatch.setattr(
        runner,
        "talk_failure_recovery_fingerprint",
        lambda _kind, _cid: "sha256:same-recovery",
    )
    monkeypatch.setattr(runner, "ffprobe_ms", lambda _path: 900_000)
    monkeypatch.setattr(runner, "find_danmaku_xml", lambda _path: None)
    monkeypatch.setattr(
        runner,
        "resolve_structured_chat_binding",
        lambda _segment, **_kwargs: {
            "structured_chat_required": False,
            "chat_binding_status": "NOT_REGISTERED",
        },
    )
    return date, {
        "pending_talk": [],
        "picks": [
            {
                "candidate_id": "auto_192000_371_669",
                "segment": segment.name,
                "start_ms": 371_000,
                "end_ms": 669_000,
                "status": "failed",
                "failure_kind": failure_kind,
                "failure_stage": "unknown",
                "failure_recoverable": True,
                "failure_recovery_fingerprint": "sha256:same-recovery",
                "pipeline_fingerprint": "sha256:same",
                "talk_transient_retry_count": transient_count,
                "next_retry_at_epoch": 0,
                "hook": "test",
            }
        ],
    }


def test_unknown_recoverable_failure_stops_after_bounded_transient_retry(
    tmp_path, monkeypatch
):
    # Regression: unknown producer_error with recoverable=True used to qualify
    # as an unlimited timer-based infrastructure retry, so a deterministic
    # defect (vanished source before classification existed) churned every
    # tick forever (2026-07-25 five-candidate incident).
    date, state = _requeue_gating_state(
        tmp_path, monkeypatch, failure_kind="producer_error", transient_count=1
    )

    assert runner.requeue_recoverable_talks(date, state) == 0
    assert state["picks"][0]["status"] == "failed"
    assert state["pending_talk"] == []


def test_confirmed_infrastructure_wait_keeps_timer_retries(tmp_path, monkeypatch):
    # A classifier-confirmed infrastructure wait (mount outage, provider
    # quota) must keep waking on its retry timer even after the bounded
    # transient retry is spent.
    date, state = _requeue_gating_state(
        tmp_path, monkeypatch, failure_kind="runtime_prerequisite", transient_count=3
    )

    assert runner.requeue_recoverable_talks(date, state) == 1
    assert state["pending_talk"][0]["cid"] == "auto_192000_371_669"
    assert state["pending_talk"][0]["retry_reason"] == (
        "transient_infrastructure_failure"
    )


def test_infrastructure_retry_cooldown_survives_unrelated_pipeline_change(
    tmp_path, monkeypatch
):
    date, state = _requeue_gating_state(
        tmp_path,
        monkeypatch,
        failure_kind="provider_transient",
        transient_count=3,
    )
    state["picks"][0]["next_retry_at_epoch"] = 10_001
    monkeypatch.setattr(runner.time, "time", lambda: 10_000)
    monkeypatch.setattr(
        runner,
        "talk_failure_recovery_fingerprint",
        lambda _kind, _cid: "sha256:changed-by-unrelated-deploy",
    )

    assert runner.requeue_recoverable_talks(date, state) == 0
    assert state["pending_talk"] == []
    assert state["picks"][0]["failure_kind"] == "provider_transient"


def test_exact_terminal_failure_waits_for_relevant_fingerprint_change(
    tmp_path, monkeypatch
):
    date = "2026-07-10"
    date_dir = tmp_path / date
    date_dir.mkdir()
    segment = date_dir / "segment.mp4"
    segment.write_bytes(b"media")
    monkeypatch.setattr(runner, "REC_ROOT", tmp_path)
    current_recovery = {"value": "sha256:same-recovery"}
    monkeypatch.setattr(
        runner,
        "talk_pipeline_fingerprint",
        lambda _cid: "sha256:new",
    )
    monkeypatch.setattr(
        runner,
        "talk_failure_recovery_fingerprint",
        lambda _kind, _cid: current_recovery["value"],
    )
    monkeypatch.setattr(runner, "ffprobe_ms", lambda _path: 900_000)
    monkeypatch.setattr(runner, "find_danmaku_xml", lambda _path: None)
    monkeypatch.setattr(
        runner,
        "resolve_structured_chat_binding",
        lambda *_args, **_kwargs: {
            "structured_chat_required": False,
            "chat_binding_status": "NOT_REGISTERED",
        },
    )
    failed = {
        "candidate_id": "selected",
        "segment": segment.name,
        "start_ms": 100_000,
        "end_ms": 200_000,
        "status": "failed",
        "failure_kind": "subtitle_authority",
        "failure_recoverable": False,
        "failure_recovery_fingerprint": "sha256:same-recovery",
        "pipeline_fingerprint": "sha256:old",
    }
    state = {"pending_talk": [], "picks": [failed]}

    assert runner.requeue_recoverable_talks(date, state) == 0
    assert state["picks"] == [failed]

    current_recovery["value"] = "sha256:changed-recovery"
    assert runner.requeue_recoverable_talks(date, state) == 1
    assert state["picks"] == []
    assert state["pending_talk"][0]["cid"] == "selected"

    speaker_state = {
        "pending_talk": [],
        "picks": [
            {
                **failed,
                "candidate_id": "speaker-selected",
                "status": "speaker_evidence_insufficient",
                "failure_kind": "speaker_evidence",
            }
        ],
    }
    assert runner.requeue_recoverable_talks(date, speaker_state) == 1
    assert speaker_state["pending_talk"][0]["cid"] == "speaker-selected"


def test_ordinary_or_forged_candidate_rejection_is_not_requeued(monkeypatch):
    monkeypatch.setattr(
        runner,
        "talk_failure_recovery_fingerprint",
        lambda *_args: pytest.fail("terminal rejection must not be fingerprinted"),
    )
    canonical_rejection = {
        "candidate_id": "ordinary",
        "status": "candidate_rejected",
        "rejected_status": "failed",
        "failure_kind": "subtitle_authority",
        "rejection_reason": "subtitle_authority_unresolved_backfilled",
    }
    ordinary = {"pending_talk": [], "picks": [canonical_rejection]}

    assert runner.requeue_recoverable_talks("2026-07-10", ordinary) == 0

    forged = {
        "run_mode": "RECOVERY_REVIEW",
        "upload_allowed": False,
        "talk_selection_contract": {
            "schema_version": "talk-selection-contract.v1",
            "mode": "EXACT_CANDIDATE_SET_NO_BACKFILL",
            "candidate_ids": ["ordinary"],
            "source_state_sha256": "sha256:" + "a" * 64,
            "authority": "Ivan selected the exact recovery set",
        },
        "pending_talk": [],
        "picks": [
            {
                **canonical_rejection,
                "rejection_reason": "handwritten_noncanonical_reason",
            }
        ],
    }
    assert runner.requeue_recoverable_talks("2026-07-10", forged) == 0


def test_relevant_fix_requeues_selected_subtitle_authority_rejection(
    tmp_path, monkeypatch
):
    date = "2026-07-10"
    rec_root = tmp_path / "recordings"
    date_dir = rec_root / date
    date_dir.mkdir(parents=True)
    segment = date_dir / "segment.mp4"
    segment.write_bytes(b"media")
    monkeypatch.setattr(runner, "REC_ROOT", rec_root)
    monkeypatch.setattr(runner, "ffprobe_ms", lambda _path: 900_000)
    monkeypatch.setattr(
        runner,
        "talk_failure_recovery_fingerprint",
        lambda _kind, _cid: "sha256:fixed-authority",
    )
    state = {
        "pending_talk": [],
        "picks": [
            {
                "candidate_id": "selected-authority-repair",
                "segment": segment.name,
                "start_ms": 100_000,
                "end_ms": 200_000,
                "status": "candidate_rejected",
                "selected_repair": True,
                "failure_kind": "subtitle_authority",
                "failure_stage": "chat_authority_finalization",
                "failure_recoverable": False,
                "failure_recovery_fingerprint": "sha256:old-authority",
                "rejection_reason": (
                    "subtitle_authority_unresolved_backfilled"
                ),
                "talk_repair_retry_count": 3,
            }
        ],
    }

    assert runner.requeue_recoverable_talks(date, state) == 1
    assert state["picks"] == []
    assert state["pending_talk"][0]["cid"] == (
        "selected-authority-repair"
    )
    assert state["pending_talk"][0]["talk_repair_retry_count"] == 4
    assert state["pending_talk"][0]["retry_reason"] == (
        "pipeline_fingerprint_changed"
    )


def test_recoverable_talk_with_unbound_given_end_fails_closed(tmp_path, monkeypatch):
    date = "2026-07-10"
    rec_root = tmp_path / "recordings"
    date_dir = rec_root / date
    date_dir.mkdir(parents=True)
    segment = date_dir / "22966160_20260710-21-20-05.mp4"
    segment.write_bytes(b"media")
    monkeypatch.setattr(runner, "REC_ROOT", rec_root)
    monkeypatch.setattr(runner, "pipeline_fingerprint", lambda: "sha256:new")
    monkeypatch.setattr(runner, "ffprobe_ms", lambda _path: 900_000)
    state = {
        "pending_talk": [],
        "picks": [
            {
                "candidate_id": "auto_212005_163_311",
                "segment": segment.name,
                "start_ms": 163_000,
                "end_ms": 311_000,
                "given_end_ms": 318_000,
                "status": "failed",
                "pipeline_fingerprint": "sha256:old",
            }
        ],
    }

    assert runner.requeue_recoverable_talks(date, state) == 0
    assert state["pending_talk"] == []
    assert state["picks"][0]["given_end_ms"] == 318_000


def test_recoverable_talk_with_broken_chat_alias_stays_unqueued(
    tmp_path, monkeypatch
):
    date = "2026-07-10"
    rec_root = tmp_path / "recordings"
    date_dir = rec_root / date
    date_dir.mkdir(parents=True)
    segment = date_dir / "22966160_20260710-21-20-05.mp4"
    segment.write_bytes(b"media")
    monkeypatch.setattr(runner, "REC_ROOT", rec_root)
    monkeypatch.setattr(runner, "pipeline_fingerprint", lambda: "sha256:new")
    monkeypatch.setattr(runner, "ffprobe_ms", lambda _path: 900_000)
    monkeypatch.setattr(runner, "find_danmaku_xml", lambda _path: None)

    def fail_binding(*_args, **_kwargs):
        raise runner.StructuredChatBindingError(
            "STRUCTURED_CHAT_ALIAS_CANONICAL_JSONL_MISSING"
        )

    monkeypatch.setattr(runner, "resolve_structured_chat_binding", fail_binding)
    state = {
        "pending_talk": [],
        "picks": [
            {
                "candidate_id": "auto_212005_163_311",
                "segment": segment.name,
                "start_ms": 163_000,
                "end_ms": 311_000,
                "status": "failed",
                "pipeline_fingerprint": "sha256:old",
            }
        ],
    }

    assert runner.requeue_recoverable_talks(date, state) == 0
    assert state["pending_talk"] == []
    assert state["picks"][0]["recovery_chat_binding_status"] == "BLOCKED"
    assert "STRUCTURED_CHAT_ALIAS_CANONICAL_JSONL_MISSING" in state["picks"][0][
        "recovery_chat_binding_error"
    ]


def test_selected_talk_transient_failure_gets_one_same_fingerprint_retry(tmp_path, monkeypatch):
    date = "2026-07-10"
    rec_root = tmp_path / "recordings"
    date_dir = rec_root / date
    date_dir.mkdir(parents=True)
    segment = date_dir / "22966160_20260710-21-20-05.mp4"
    segment.write_bytes(b"media")
    monkeypatch.setattr(runner, "REC_ROOT", rec_root)
    monkeypatch.setattr(runner, "pipeline_fingerprint", lambda: "sha256:same")
    monkeypatch.setattr(runner, "ffprobe_ms", lambda _path: 900_000)
    monkeypatch.setattr(runner, "find_danmaku_xml", lambda _path: None)
    monkeypatch.setattr(runner, "find_chat_jsonl", lambda _path: None)
    failed = {
        "candidate_id": "auto_212005_163_311",
        "segment": segment.name,
        "start_ms": 163_000,
        "end_ms": 311_000,
        "status": "failed",
        "pipeline_fingerprint": "sha256:same",
        "selected_repair": True,
        "talk_repair_retry_count": 1,
        "talk_transient_retry_count": 0,
    }
    state = {"pending_talk": [], "picks": [failed]}

    assert runner.requeue_recoverable_talks(date, state) == 1
    assert state["pending_talk"][0]["retry_reason"] == "transient_produce_failure"
    assert state["pending_talk"][0]["talk_repair_retry_count"] == 2
    assert state["pending_talk"][0]["talk_transient_retry_count"] == 1

    state = {
        "pending_talk": [],
        "picks": [{**failed, "talk_repair_retry_count": 2, "talk_transient_retry_count": 1}],
    }
    assert runner.requeue_recoverable_talks(date, state) == 0


def test_selected_talk_relevant_fix_bypasses_exhausted_legacy_lifetime_cap(tmp_path, monkeypatch):
    date = "2026-07-10"
    rec_root = tmp_path / "recordings"
    date_dir = rec_root / date
    date_dir.mkdir(parents=True)
    segment = date_dir / "segment.mp4"
    segment.write_bytes(b"media")
    monkeypatch.setattr(runner, "REC_ROOT", rec_root)
    monkeypatch.setattr(runner, "pipeline_fingerprint", lambda: "sha256:new")
    monkeypatch.setattr(runner, "ffprobe_ms", lambda _path: 900_000)
    monkeypatch.setattr(runner, "TALK_REPAIR_LIFETIME_RETRY_CAP", 3)
    state = {
        "pending_talk": [],
        "picks": [
            {
                "candidate_id": "repair",
                "segment": segment.name,
                "start_ms": 1,
                "end_ms": 2,
                "status": "failed",
                "pipeline_fingerprint": "sha256:old",
                "talk_repair_retry_count": 3,
            }
        ],
    }

    assert runner.requeue_recoverable_talks(date, state) == 1
    assert state["picks"] == []
    assert state["pending_talk"][0]["talk_repair_retry_count"] == 4


def test_cover_route_regeneration_has_independent_one_shot_budget_at_talk_cap(
    tmp_path, monkeypatch
):
    date = "2026-07-10"
    rec_root = tmp_path / "recordings"
    date_dir = rec_root / date
    date_dir.mkdir(parents=True)
    segment = date_dir / "segment.mp4"
    segment.write_bytes(b"media")
    monkeypatch.setattr(runner, "REC_ROOT", rec_root)
    monkeypatch.setattr(
        runner, "talk_pipeline_fingerprint", lambda _cid: "sha256:same"
    )
    monkeypatch.setattr(
        runner,
        "talk_failure_recovery_fingerprint",
        lambda _kind, _cid: "sha256:same",
    )
    monkeypatch.setattr(runner, "ffprobe_ms", lambda _path: 900_000)
    monkeypatch.setattr(runner, "find_danmaku_xml", lambda _path: None)
    monkeypatch.setattr(runner, "find_chat_jsonl", lambda _path: None)
    monkeypatch.setattr(runner, "TALK_REPAIR_LIFETIME_RETRY_CAP", 3)
    state = {
        "pending_talk": [],
        "picks": [
            {
                "candidate_id": "repair",
                "segment": segment.name,
                "start_ms": 1,
                "end_ms": 2,
                "status": "failed",
                "failure_kind": "cover_route_regeneration",
                "failure_recoverable": True,
                "pipeline_fingerprint": "sha256:same",
                "talk_repair_retry_count": 3,
                "talk_transient_retry_count": 1,
                "cover_route_regeneration_fingerprint": "sha256:cover-build",
                "cover_route_regeneration_attempts": 1,
            }
        ],
    }

    assert runner.requeue_recoverable_talks(date, state) == 1
    queued = state["pending_talk"][0]
    assert queued["retry_reason"] == "cover_route_regeneration"
    assert queued["talk_repair_retry_count"] == 4
    assert queued["talk_transient_retry_count"] == 1
    assert queued["cover_route_regeneration_fingerprint"] == "sha256:cover-build"
    assert queued["cover_route_regeneration_attempts"] == 1


def test_failure_scoped_talk_fingerprint_ignores_unrelated_graph_change(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "REPO_ROOT", tmp_path)
    for relative in (
        "scripts/produce_slice_package.py",
        "src/autoslice/jingting_chunker.py",
        "src/autoslice/subtitle_timing_qa.py",
        "assets/lidousha/topic_entity_graph.json",
    ):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(relative, encoding="utf-8")
    baseline = runner.talk_failure_recovery_fingerprint("content_boundary", "candidate")

    graph = tmp_path / "assets/lidousha/topic_entity_graph.json"
    graph.write_text("unrelated graph update", encoding="utf-8")
    assert runner.talk_failure_recovery_fingerprint("content_boundary", "candidate") == baseline

    producer = tmp_path / "scripts/produce_slice_package.py"
    producer.write_text("boundary fix", encoding="utf-8")
    assert runner.talk_failure_recovery_fingerprint("content_boundary", "candidate") != baseline


def test_selected_boundary_repair_bypasses_filled_talk_quota(monkeypatch):
    monkeypatch.setattr(runner, "MAX_TALK_PICKS", 1)
    monkeypatch.setattr(runner, "refill_songs", lambda _state: None)
    state = {
        "picks": [{"candidate_id": "already", "status": "review_ready"}],
        "pending_talk": [
            {
                "cid": "repair",
                "segment_path": "/recordings/segment.mp4",
                "start_ms": 1,
                "end_ms": 2,
                "selected_repair": True,
                "confidence": 0.1,
            },
            {
                "cid": "new",
                "segment_path": "/recordings/segment.mp4",
                "start_ms": 3,
                "end_ms": 4,
                "confidence": 1.0,
            },
        ],
    }

    runner.prioritize(state)

    assert [item["cid"] for item in state["pending_talk"]] == ["repair"]


def test_selected_repair_defers_same_session_backfill_until_result(monkeypatch):
    monkeypatch.setattr(runner, "MAX_TALK_PICKS", 2)
    monkeypatch.setattr(runner, "refill_songs", lambda _state: None)
    state = {
        "picks": [
            {
                "candidate_id": "delivered",
                "status": "review_ready",
                "session_id": "session-a",
                "cover_diversity_slot": 0,
            }
        ],
        "pending_talk": [
            {
                "cid": "repair",
                "segment_path": "/recordings/segment.mp4",
                "start_ms": 1,
                "end_ms": 2,
                "selected_repair": True,
                "confidence": 0.99,
                "session_id": "session-a",
                "cover_diversity_slot": 1,
            },
            {
                "cid": "reserve",
                "segment_path": "/recordings/segment.mp4",
                "start_ms": 3,
                "end_ms": 4,
                "confidence": 0.98,
                "session_id": "session-a",
            },
        ],
    }

    runner.prioritize(state)

    assert [item["cid"] for item in state["pending_talk"]] == ["repair"]
    assert [item["cid"] for item in state["talk_backlog"]] == ["reserve"]

    state["picks"].append(
        {
            "candidate_id": "repair",
            "status": "candidate_rejected",
            "session_id": "session-a",
            "cover_diversity_slot": 1,
        }
    )
    state["pending_talk"] = []
    runner.prioritize(state)

    assert [item["cid"] for item in state["pending_talk"]] == ["reserve"]
    assert state["pending_talk"][0]["cover_diversity_slot"] == 1


def test_recovery_exact_selection_never_backfills_rejected_slot(monkeypatch):
    monkeypatch.setattr(runner, "MAX_TALK_PICKS", 2)
    monkeypatch.setattr(runner, "refill_songs", lambda _state: None)
    state = {
        "run_mode": "RECOVERY_REVIEW",
        "upload_allowed": False,
        "picks": [
            {
                "candidate_id": "delivered",
                "status": "review_ready",
                "session_id": "session-a",
            },
            {
                "candidate_id": "repair",
                "status": "candidate_rejected",
                "session_id": "session-a",
            },
        ],
        "pending_talk": [],
        "talk_backlog": [
            {
                "cid": "reserve",
                "segment_path": "/recordings/segment.mp4",
                "start_ms": 3,
                "end_ms": 4,
                "confidence": 0.98,
                "session_id": "session-a",
            }
        ],
        "talk_selection_contract": {
            "schema_version": "talk-selection-contract.v1",
            "mode": "EXACT_CANDIDATE_SET_NO_BACKFILL",
            "candidate_ids": ["repair"],
            "source_state_sha256": "sha256:" + "a" * 64,
            "authority": "Ivan selected the exact recovery set",
        },
    }

    runner.prioritize(state)

    assert state["pending_talk"] == []
    assert [item["cid"] for item in state["talk_backlog"]] == ["reserve"]
    assert runner.backlog_has_eligible_session_work(state) is False


def test_recovery_exact_selection_recovers_selected_row_from_backlog(monkeypatch):
    monkeypatch.setattr(runner, "refill_songs", lambda _state: None)
    selected = {"cid": "selected", "session_id": "session-a"}
    reserve = {"cid": "reserve", "session_id": "session-a"}
    state = {
        "run_mode": "RECOVERY_REVIEW",
        "upload_allowed": False,
        "picks": [],
        "pending_talk": [],
        "talk_backlog": [reserve, selected],
        "talk_selection_contract": {
            "schema_version": "talk-selection-contract.v1",
            "mode": "EXACT_CANDIDATE_SET_NO_BACKFILL",
            "candidate_ids": ["selected"],
            "source_state_sha256": "sha256:" + "a" * 64,
            "authority": "Ivan selected the exact recovery set",
        },
    }

    runner.prioritize(state)

    assert state["pending_talk"] == [selected]
    assert state["talk_backlog"] == [reserve]


def test_recovery_malformed_exact_selection_contract_fails_closed(monkeypatch):
    monkeypatch.setattr(runner, "refill_songs", lambda _state: None)
    state = {
        "run_mode": "RECOVERY_REVIEW",
        "upload_allowed": False,
        "pending_talk": [],
        "talk_backlog": [{"cid": "reserve", "session_id": "session-a"}],
        "talk_selection_contract": {
            "schema_version": "talk-selection-contract.v1",
            "mode": "EXACT_CANDIDATE_SET_NO_BACKFILL",
            "candidate_ids": [],
            "source_state_sha256": "sha256:" + "a" * 64,
            "authority": "Ivan selected the exact recovery set",
        },
    }

    with pytest.raises(ValueError, match="INVALID_EXACT_TALK_SELECTION_CONTRACT"):
        runner.prioritize(state)
    with pytest.raises(ValueError, match="INVALID_EXACT_TALK_SELECTION_CONTRACT"):
        runner.backlog_has_eligible_session_work(state)


def _exact_terminal_state(*candidate_ids: str) -> dict:
    return {
        "run_mode": "RECOVERY_REVIEW",
        "upload_allowed": False,
        "pending_talk": [],
        "picks": [],
        "songs": [],
        "talk_selection_contract": {
            "schema_version": "talk-selection-contract.v1",
            "mode": "EXACT_CANDIDATE_SET_NO_BACKFILL",
            "candidate_ids": list(candidate_ids),
            "source_state_sha256": "sha256:" + "a" * 64,
            "authority": "Ivan selected the exact recovery set",
        },
    }


def test_exact_terminal_projection_beats_future_retry_metadata(monkeypatch):
    future = 9_999_999_999
    state = _exact_terminal_state("delivered", "waiting")
    state["picks"] = [
        {
            "candidate_id": "delivered",
            "status": "review_ready",
            "bundle_lifecycle": "CURRENT",
            "bundle_compliance": "COMPLIANT",
            "rc": 0,
        },
        {
            "candidate_id": "waiting",
            "status": "failed",
            "failure_recoverable": True,
            "next_retry_at_epoch": future,
        },
    ]

    terminal = runner._project_terminal_batch_state(state)

    assert terminal["retry_epoch"] == future
    assert state["status"] == "recovery_incomplete"
    assert state["next_retry_at_epoch"] == future
    assert state["exact_talk_contract_closure"]["status"] == "INCOMPLETE"


def test_exact_complete_projection_ignores_unrelated_retry_for_release_status():
    future = 9_999_999_999
    state = _exact_terminal_state("delivered")
    state["picks"] = [
        {
            "candidate_id": "delivered",
            "status": "review_ready",
            "bundle_lifecycle": "CURRENT",
            "bundle_compliance": "COMPLIANT",
            "rc": 0,
        }
    ]
    state["songs"] = [
        {
            "candidate_id": "song",
            "status": "blocked",
            "reason_codes": ["AGY_SOURCE_CONTEXT_RUNNER_FAILED"],
            "transient_retry_count": 1,
                "next_retry_at_epoch": future,
        }
    ]

    runner._project_terminal_batch_state(state)

    assert state["status"] == "review_ready"
    assert state["next_retry_at_epoch"] == future
    assert state["exact_talk_contract_closure"]["status"] == "COMPLETE"


def test_ordinary_terminal_projection_clears_stale_closure_and_retry_metadata():
    state = {
        "picks": [{"candidate_id": "delivered", "status": "review_ready"}],
        "songs": [],
        "pending_talk": [],
        "exact_talk_contract_closure": {"status": "INCOMPLETE"},
        "next_retry_at_epoch": 9_999,
        "next_retry_at": "stale",
    }

    runner._project_terminal_batch_state(state)

    assert state["status"] == "review_ready"
    assert "exact_talk_contract_closure" not in state
    assert "next_retry_at_epoch" not in state
    assert "next_retry_at" not in state


def test_user_selection_override_keeps_its_slot_alongside_repairs(monkeypatch):
    monkeypatch.setattr(runner, "MAX_TALK_PICKS", 5)
    monkeypatch.setattr(runner, "refill_songs", lambda _state: None)
    override = {
        "schema_version": "talk-selection-override.v1",
        "event_type": "USER_SELECTION_OVERRIDE",
        "candidate_id": "brainflick",
        "selected_slot": 5,
        "authority": "Ivan-stated: make this clip",
        "scorecard_sha256": "sha256:" + "a" * 64,
    }
    state = {
        "picks": [],
        "pending_talk": [
            {
                "cid": "repair",
                "segment_path": "/recordings/segment.mp4",
                "start_ms": 1,
                "end_ms": 2,
                "selected_repair": True,
                "confidence": 0.99,
                "session_id": "session-a",
            },
            {
                "cid": "brainflick",
                "segment_path": "/recordings/segment.mp4",
                "start_ms": 3,
                "end_ms": 4,
                "confidence": 0.87,
                "session_id": "session-a",
                "selection_override": override,
            },
            {
                "cid": "higher-score-reserve",
                "segment_path": "/recordings/segment.mp4",
                "start_ms": 5,
                "end_ms": 6,
                "confidence": 0.99,
                "session_id": "session-a",
            },
        ],
    }

    runner.prioritize(state)

    assert [item["cid"] for item in state["pending_talk"]] == [
        "repair",
        "brainflick",
    ]
    assert [item["cid"] for item in state["talk_backlog"]] == [
        "higher-score-reserve"
    ]


def test_talk_fingerprint_scopes_speaker_override_and_withholds_truth(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(runner, "pipeline_fingerprint", lambda: "sha256:" + "a" * 64)
    monkeypatch.delenv("AUTOSLICE_HUMAN_TRUTH_MODE", raising=False)
    root = tmp_path / "assets/lidousha/speaker_overrides"
    root.mkdir(parents=True)
    baseline = runner.talk_pipeline_fingerprint("auto_singleton")
    override = root / "auto_singleton.speaker.v1.json"
    override.write_text('{"version":1}\n', encoding="utf-8")
    added = runner.talk_pipeline_fingerprint("auto_singleton")
    assert added != baseline
    override.write_text('{"version":2}\n', encoding="utf-8")
    assert runner.talk_pipeline_fingerprint("auto_singleton") != added
    override.unlink()
    assert runner.talk_pipeline_fingerprint("auto_singleton") == baseline

    override.write_text('{"human_truth":"must not be read"}\n', encoding="utf-8")
    monkeypatch.setenv("AUTOSLICE_HUMAN_TRUTH_MODE", "withheld")
    withheld = runner.talk_pipeline_fingerprint("auto_singleton")
    override.write_text('{"human_truth":"changed but still unread"}\n', encoding="utf-8")
    assert runner.candidate_speaker_override_path("auto_singleton") is None
    assert runner.talk_pipeline_fingerprint("auto_singleton") == withheld


def test_talk_speaker_review_is_structured_and_not_boundary_retried(tmp_path, monkeypatch):
    date = "2026-07-10"
    cid = "auto_170019_580_757"
    base = tmp_path / "autoslice"
    repo = tmp_path / "repo"
    (base / "logs").mkdir(parents=True)
    repo.mkdir()
    monkeypatch.setattr(runner, "BASE", base)
    monkeypatch.setattr(runner, "REPO_ROOT", repo)
    monkeypatch.setattr(runner, "child_env", lambda: {})
    monkeypatch.setattr(runner, "pipeline_fingerprint", lambda: "sha256:test")
    calls = []

    class Completed:
        returncode = 1

    def fake_run(command, **kwargs):
        calls.append(command)
        recuts = base / "out" / date / cid / "replacement_recuts"
        recuts.mkdir(parents=True)
        (recuts / f"{cid}.speaker-final.json").write_text(
            json.dumps(
                {
                    "status": "SPEAKER_REVIEW_REQUIRED",
                    "production_ready": False,
                    "reason": "singleton requires review",
                    "source_media_sha256": "a" * 64,
                    "text_final_srt_sha256": "b" * 64,
                    "context_unresolved_cues": [75],
                    "review_required_cues": [
                        {
                            "source_cue": 75,
                            "zero_based_index": 74,
                            "start": "00:02:28,000",
                            "end": "00:02:30,030",
                            "text": "我这，哈哈",
                            "seed_score": 0.33793,
                            "host_bank_score": 0.28934,
                            "audio_sha256": "c" * 64,
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        kwargs["stdout"].write("SPEAKER_REVIEW_REQUIRED: singleton requires review\n")
        kwargs["stdout"].flush()
        return Completed()

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    result = runner.produce_talk(
        date,
        {
            "cid": cid,
            "segment_path": "/recordings/segment.mp4",
            "seg_dur_ms": 900_000,
            "start_ms": 580_000,
            "end_ms": 757_000,
            "hook": "singleton fixture",
        },
    )

    assert len(calls) == 1
    assert result["status"] == "speaker_review_required"
    assert result["boundary_context_retries"] == 0
    assert result["speaker_review_required_cues"][0]["source_cue"] == 75
    assert result["speaker_review_manifest_sha256"].startswith("sha256:")
    assert result["speaker_review_source_media_sha256"] == "a" * 64


def test_talk_clip_anchor_shortage_becomes_nonretryable_speaker_evidence(
    tmp_path, monkeypatch
):
    date = "2026-07-11"
    cid = "auto_154845_1367_1407"
    base = tmp_path / "autoslice"
    repo = tmp_path / "repo"
    (base / "logs").mkdir(parents=True)
    repo.mkdir()
    monkeypatch.setattr(runner, "BASE", base)
    monkeypatch.setattr(runner, "REPO_ROOT", repo)
    monkeypatch.setattr(runner, "child_env_for_date", lambda _date: {})
    monkeypatch.setattr(runner, "pipeline_fingerprint", lambda: "sha256:test")

    class Completed:
        returncode = 1

    def fake_run(_command, **kwargs):
        kwargs["stdout"].write(
            "RuntimeError: SPEAKER_FINALIZATION_BLOCKED: SpeakerFinalizationError: "
            "not enough Li Dousha clip anchors: [6]\n"
        )
        kwargs["stdout"].flush()
        return Completed()

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    result = runner.produce_talk(
        date,
        {
            "cid": cid,
            "segment_path": "/recordings/segment.mp4",
            "seg_dur_ms": 2_000_000,
            "start_ms": 1_367_000,
            "end_ms": 1_413_000,
            "hook": "real blind-eval shape",
        },
    )

    assert result["status"] == "speaker_evidence_insufficient"
    assert result["failure_kind"] == "speaker_evidence"
    assert result["failure_stage"] == "speaker_finalization"
    assert result["failure_recoverable"] is False
    assert "next_retry_at_epoch" not in result


@pytest.mark.parametrize(
    "manifest_document",
    [
        None,
        {
            "status": "SPEAKER_REVIEW_REQUIRED",
            "production_ready": False,
            "reason": "minimal malformed manifest",
        },
    ],
)
def test_talk_speaker_review_marker_without_bound_manifest_stays_failed(
    tmp_path, monkeypatch, manifest_document
):
    date = "2026-07-10"
    cid = "auto_unbound_speaker_review"
    base = tmp_path / "autoslice"
    repo = tmp_path / "repo"
    (base / "logs").mkdir(parents=True)
    repo.mkdir()
    monkeypatch.setattr(runner, "BASE", base)
    monkeypatch.setattr(runner, "REPO_ROOT", repo)
    monkeypatch.setattr(runner, "child_env", lambda: {})
    monkeypatch.setattr(runner, "pipeline_fingerprint", lambda: "sha256:test")

    class Completed:
        returncode = 1

    def fake_run(_command, **kwargs):
        if manifest_document is not None:
            recuts = base / "out" / date / cid / "replacement_recuts"
            recuts.mkdir(parents=True)
            (recuts / f"{cid}.speaker-final.json").write_text(
                json.dumps(manifest_document), encoding="utf-8"
            )
        kwargs["stdout"].write("SPEAKER_REVIEW_REQUIRED: marker only\n")
        kwargs["stdout"].flush()
        return Completed()

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    result = runner.produce_talk(
        date,
        {
            "cid": cid,
            "segment_path": "/recordings/segment.mp4",
            "seg_dur_ms": 900_000,
            "start_ms": 100_000,
            "end_ms": 200_000,
            "hook": "fixture",
        },
    )

    assert result["status"] == "failed"
    assert "speaker_review_manifest_sha256" not in result


def test_read_speaker_review_meta_rejects_unchanged_manifest(tmp_path):
    recuts = tmp_path / "replacement_recuts"
    recuts.mkdir()
    manifest = recuts / "candidate.speaker-final.json"
    manifest.write_text(
        json.dumps(
            {
                "status": "SPEAKER_REVIEW_REQUIRED",
                "production_ready": False,
                "reason": "stale but otherwise valid",
                "source_media_sha256": "a" * 64,
                "text_final_srt_sha256": "b" * 64,
                "context_unresolved_cues": [1],
                "review_required_cues": [
                    {
                        "source_cue": 1,
                        "zero_based_index": 0,
                        "start": "00:00:00,000",
                        "end": "00:00:01,000",
                        "text": "fixture",
                        "audio_sha256": "c" * 64,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    previous = runner._speaker_review_manifest_state(tmp_path)

    assert runner.read_speaker_review_meta(tmp_path, previous_state=previous) == {}


def test_speaker_review_requeues_only_after_fingerprint_change(tmp_path, monkeypatch):
    date = "2026-07-10"
    rec_root = tmp_path / "recordings"
    date_dir = rec_root / date
    date_dir.mkdir(parents=True)
    segment = date_dir / "segment.mp4"
    segment.write_bytes(b"media")
    monkeypatch.setattr(runner, "REC_ROOT", rec_root)
    monkeypatch.setattr(runner, "talk_pipeline_fingerprint", lambda _cid: "sha256:same")
    monkeypatch.setattr(runner, "ffprobe_ms", lambda _path: 900_000)
    monkeypatch.setattr(runner, "find_danmaku_xml", lambda _path: None)
    monkeypatch.setattr(runner, "find_chat_jsonl", lambda _path: None)
    record = {
        "candidate_id": "auto_singleton",
        "segment": segment.name,
        "start_ms": 100_000,
        "end_ms": 200_000,
        "status": "speaker_review_required",
        "pipeline_fingerprint": "sha256:same",
        "talk_repair_retry_count": 0,
        "talk_transient_retry_count": 0,
    }
    state = {"pending_talk": [], "picks": [record]}

    assert runner.requeue_recoverable_talks(date, state) == 0
    assert state["picks"] == [record]
    monkeypatch.setattr(runner, "talk_pipeline_fingerprint", lambda _cid: "sha256:new")
    assert runner.requeue_recoverable_talks(date, state) == 1
    assert state["pending_talk"][0]["retry_reason"] == "pipeline_fingerprint_changed"
    assert state["pending_talk"][0]["talk_transient_retry_count"] == 0


def test_process_date_wakes_old_boundary_failure_without_new_segments(monkeypatch):
    date = "2026-07-10"
    monkeypatch.setattr(runner, "AUTOMATIC_MAINTENANCE_NOT_BEFORE", date)
    state = {
        "status": "review_ready_with_failures",
        "segments_done": ["segment"],
        "segments_dead": {},
        "pending_talk": [],
        "pending_song": [],
        "songs": [],
        "picks": [
            {
                "candidate_id": "old_boundary",
                "segment": "segment.mp4",
                "start_ms": 10_000,
                "end_ms": 20_000,
                "status": "boundary_unrepairable",
                "pipeline_fingerprint": "sha256:old",
            }
        ],
    }
    produced = []
    monkeypatch.setattr(runner, "read_state", lambda _date: state)

    def wake(_date, value):
        value["pending_talk"].append(
            {
                "cid": "old_boundary",
                "segment_path": "/recordings/segment.mp4",
                "start_ms": 10_000,
                "end_ms": 20_000,
                "selected_repair": True,
            }
        )
        value["picks"] = []
        return 1

    monkeypatch.setattr(
        runner,
        "requeue_recoverable_deliveries",
        lambda tick_date, value: (0, wake(tick_date, value), 0),
    )
    monkeypatch.setattr(runner, "pipeline_fingerprint", lambda: "sha256:new")
    monkeypatch.setattr(runner, "write_state", lambda _date, _state: None)
    monkeypatch.setattr(runner, "list_segments", lambda _date: [])
    monkeypatch.setattr(runner, "cover_repair_needed", lambda _date, _row: False)
    monkeypatch.setattr(runner, "cpa_healthy", lambda: True)
    monkeypatch.setattr(runner, "discover_segments", lambda _date, _state: None)
    monkeypatch.setattr(runner, "session_sealed", lambda _date, _state: True)
    monkeypatch.setattr(runner, "refill_songs", lambda _state: None)

    def produce(_date, items, _fn):
        produced.extend(items)
        return [{"candidate_id": items[0]["cid"], "status": "review_ready"}]

    monkeypatch.setattr(runner, "produce_batch", produce)
    monkeypatch.setattr(runner, "repair_covers", lambda _date, _state: None)
    monkeypatch.setattr(runner, "write_reports", lambda _date, _state: None)

    runner.process_date(date)

    assert [item["cid"] for item in produced] == ["old_boundary"]
    assert state["picks"][0]["status"] == "review_ready"
    assert state["pending_talk"] == []


def test_process_date_does_not_auto_maintain_pre_horizon_history(monkeypatch):
    date = "2026-07-10"
    monkeypatch.setattr(runner, "AUTOMATIC_MAINTENANCE_NOT_BEFORE", "2026-07-11")
    state = {
        "status": "review_ready_with_failures",
        "segments_done": ["segment"],
        "segments_dead": {},
        "pending_talk": [],
        "pending_song": [],
        "songs": [{"candidate_id": "old_song", "status": "blocked"}],
        "picks": [
            {
                "candidate_id": "old_talk",
                "status": "boundary_unrepairable",
                "pipeline_fingerprint": "sha256:old",
            },
            {
                "candidate_id": "old_cover",
                "status": "review_ready",
                "title": "old cover",
            },
        ],
    }
    monkeypatch.setattr(runner, "read_state", lambda _date: state)
    monkeypatch.setattr(
        runner,
        "requeue_recoverable_deliveries",
        lambda _date, _state: pytest.fail(
            "pre-horizon maintenance must not auto-requeue"
        ),
    )
    monkeypatch.setattr(
        runner,
        "requeue_recoverable_talks",
        lambda _date, _state: pytest.fail("pre-horizon talk must not auto-requeue"),
    )
    monkeypatch.setattr(
        runner,
        "requeue_recoverable_songs",
        lambda _date, _state: pytest.fail("pre-horizon song must not auto-requeue"),
    )
    monkeypatch.setattr(runner, "list_segments", lambda _date: [])
    monkeypatch.setattr(runner, "write_state", lambda _date, _state: None)
    monkeypatch.setattr(runner, "write_reports", lambda _date, _state: None)
    monkeypatch.setattr(
        runner,
        "cover_repair_needed",
        lambda _date, _record: pytest.fail("pre-horizon cover must not auto-repair"),
    )

    runner.process_date(date)

    assert state["pending_talk"] == []
    assert state["pending_song"] == []


def test_process_date_transient_selected_repair_remains_retryable(tmp_path, monkeypatch):
    date = "2026-07-10"
    rec_root = tmp_path / "recordings"
    date_dir = rec_root / date
    date_dir.mkdir(parents=True)
    segment = date_dir / "segment.mp4"
    segment.write_bytes(b"media")
    pending = {
        "cid": "old_boundary",
        "segment_path": str(segment),
        "seg_dur_ms": 100_000,
        "start_ms": 10_000,
        "end_ms": 20_000,
        "selected_repair": True,
        "talk_repair_retry_count": 1,
        "talk_transient_retry_count": 0,
    }
    state = {
        "status": "processing",
        "segments_done": ["segment"],
        "segments_dead": {},
        "pending_talk": [pending],
        "pending_song": [],
        "songs": [],
        "picks": [],
    }
    monkeypatch.setattr(runner, "REC_ROOT", rec_root)
    monkeypatch.setattr(runner, "read_state", lambda _date: state)
    monkeypatch.setattr(runner, "pipeline_fingerprint", lambda: "sha256:same")
    monkeypatch.setattr(runner, "write_state", lambda _date, _state: None)
    monkeypatch.setattr(runner, "list_segments", lambda _date: [])
    monkeypatch.setattr(runner, "cover_repair_needed", lambda _date, _row: False)
    monkeypatch.setattr(runner, "cpa_healthy", lambda: True)
    monkeypatch.setattr(runner, "discover_segments", lambda _date, _state: None)
    monkeypatch.setattr(runner, "session_sealed", lambda _date, _state: True)
    monkeypatch.setattr(runner, "refill_songs", lambda _state: None)
    monkeypatch.setattr(runner, "ffprobe_ms", lambda _path: 100_000)
    monkeypatch.setattr(runner, "find_danmaku_xml", lambda _path: None)
    monkeypatch.setattr(runner, "find_chat_jsonl", lambda _path: None)

    def fail_once(_date, items, _fn):
        item = items[0]
        return [
            {
                "candidate_id": item["cid"],
                "segment": Path(item["segment_path"]).name,
                "start_ms": item["start_ms"],
                "end_ms": item["end_ms"],
                "status": "failed",
                "pipeline_fingerprint": "sha256:same",
                "selected_repair": True,
                "talk_repair_retry_count": item["talk_repair_retry_count"],
                "talk_transient_retry_count": item["talk_transient_retry_count"],
            }
        ]

    monkeypatch.setattr(runner, "produce_batch", fail_once)
    monkeypatch.setattr(runner, "repair_covers", lambda _date, _state: None)
    monkeypatch.setattr(runner, "write_reports", lambda _date, _state: None)

    runner.process_date(date)

    assert state["picks"][0]["status"] == "failed"
    assert state["pending_talk"] == []
    assert runner.requeue_recoverable_talks(date, state) == 1
    assert state["pending_talk"][0]["talk_transient_retry_count"] == 1

def test_record_is_song_recognizes_non_lrc_songs():
    """7/9 实锤：日语歌《ただそばにいて》LRC 钉歌失败(song_boundary/alignment 全空)，
    但召回记录是 semanticsong_* —— 它是歌，必须参与交付竞争。"""
    assert runner.record_is_song({"candidate_id": "semanticsong_15000_170540", "source_context_job": {}})
    assert runner.record_is_song({"candidate_id": "x", "source_context_job": {"song_boundary": {"a": 1}}})
    assert runner.record_is_song({"candidate_id": "x", "source_context_job": {"lyrics_alignment": {"m": 0.9}}})
    assert runner.record_is_song({"candidate_id": "seeded", "source_context_job": {"content_type_hint": "song"}})
    assert runner.record_is_song({"candidate_id": "seeded", "source_context_job": {"song_candidate": True}})
    assert runner.record_is_song({"candidate_id": "seeded", "source_context_job": {"requires_full_source_song_boundary_redo": True}})
    assert not runner.record_is_song({"candidate_id": "semantictalk_3320_50570_ctxexp", "source_context_job": {}})
    assert not runner.record_is_song({})


def test_cpa_qa_cmd_reads_cpa_env_for_direct_produce_song_entrypoint(tmp_path, monkeypatch):
    cpa_env = tmp_path / "cpa.env"
    cpa_env.write_text("CPA_BASE_URL=https://cpa.example.test/v1\nCPA_API_KEY=not-used-in-argv\n", encoding="utf-8")
    monkeypatch.setattr(runner, "CPA_ENV", cpa_env)
    monkeypatch.setenv("CPA_BASE_URL", "https://stale-environment.invalid/v1")

    command = runner.cpa_qa_cmd()

    assert "--api-base https://cpa.example.test/v1" in command
    assert "not-used-in-argv" not in command


def test_cpa_qa_cmd_routes_judge_to_luna_responses(monkeypatch):
    """2026-07-10: the semantic-QA judge lane runs gpt-5.6-luna on /responses
    (structured verdict = the doc-exact luna lane; gpt-5.x misroute on chat)."""
    monkeypatch.setattr(runner, "load_env_file", lambda _p: {"CPA_BASE_URL": "https://cpa.test/v1"})
    cmd = runner.cpa_qa_cmd()
    assert "--model gpt-5.6-luna" in cmd
    assert "--api-mode responses" in cmd
    assert "--max-tokens 16000" in cmd
    assert "--retries 3" in cmd
    assert "--api-base https://cpa.test/v1" in cmd


def test_list_segments_dedupes_duplicate_fuse_dirents(tmp_path, monkeypatch):
    date = "2026-07-19"
    date_dir = tmp_path / date
    date_dir.mkdir()
    for stem in ("20260719-16-01-12", "20260719-16-31-09"):
        (date_dir / f"{runner.ROOM}_{stem}.mp4").write_bytes(b"x")
    monkeypatch.setattr(runner, "REC_ROOT", tmp_path)
    real_glob = Path.glob

    def duplicated_glob(self, pattern):
        results = list(real_glob(self, pattern))
        # CloudFS FUSE has been observed emitting each dirent twice while its
        # upload queue drains; list_segments must collapse them.
        return results + results

    monkeypatch.setattr(Path, "glob", duplicated_glob)

    names = [segment.name for segment in runner.list_segments(date)]

    assert len(names) == 2
    assert names == sorted(names)


def test_structured_chat_binding_resolves_hash_bound_source_alias(
    tmp_path, monkeypatch
):
    alias_name = "22966160_20260722-19-34-50.mp4"
    canonical_name = "22966160_20260722-19-35-15.mp4"
    segment = tmp_path / "official-replay" / alias_name
    segment.parent.mkdir()
    segment.write_bytes(b"official replay bytes")
    # A same-stem local file cannot bypass a committed alias: the canonical
    # recorder sidecar and its declared timeline remain authoritative.
    segment.with_suffix(".jsonl").write_text(
        '{"cmd":"DANMU_MSG","info":[[0,0,0,0,1],"untrusted local"]}\n',
        encoding="utf-8",
    )
    source_sha256 = "sha256:" + hashlib.sha256(segment.read_bytes()).hexdigest()

    ledger = tmp_path / "subtitle-truth.json"
    ledger.write_text(
        json.dumps(
            {
                "schema_version": "source-subtitle-truth-ledger.v1",
                "source_aliases": [
                    {
                        "alias_id": "20260722-official-replay",
                        "alias_recording_basename": alias_name,
                        "alias_source_sha256": source_sha256,
                        "canonical_recording_basename": canonical_name,
                        "alias_timeline_offset_ms": 37,
                        "authority": "test hash and timeline authority",
                    }
                ],
                "entries": [],
            }
        ),
        encoding="utf-8",
    )
    canonical_root = tmp_path / "canonical"
    sources = canonical_root / "2026-07-22" / "sources"
    sources.mkdir(parents=True)
    jsonl = sources / Path(canonical_name).with_suffix(".jsonl").name
    jsonl.write_text(
        '{"cmd":"DANMU_MSG","info":[[0,0,0,0,1784777715],"证据"]}\n',
        encoding="utf-8",
    )
    jsonl.with_suffix(".meta.json").write_text(
        json.dumps(
            {
                "description": {
                    "RecordStartTime": "2026-07-22 19:35:15+08:00"
                }
            }
        ),
        encoding="utf-8",
    )
    # A near timestamp is not an authority and must never be selected.
    (sources / "22966160_20260722-19-35-16.jsonl").write_text(
        '{"cmd":"DANMU_MSG"}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(runner, "CANONICAL_REC_ROOT", canonical_root)
    monkeypatch.setattr(
        runner,
        "profile_asset_file",
        lambda key: ledger
        if key == "subtitle_truth_ledger"
        else (_ for _ in ()).throw(KeyError(key)),
    )

    binding = runner.resolve_structured_chat_binding(
        segment,
        source_sha256=source_sha256,
    )

    assert binding["chat_jsonl"] == str(jsonl)
    assert binding["chat_jsonl_sha256"] == (
        "sha256:" + hashlib.sha256(jsonl.read_bytes()).hexdigest()
    )
    assert binding["chat_origin_epoch_ms"] == recording_start_epoch_ms(jsonl)
    assert binding["chat_timeline_offset_ms"] == 37
    assert binding["structured_chat_required"] is True
    assert binding["chat_source_alias_id"] == "20260722-official-replay"
    assert binding["chat_canonical_recording_basename"] == canonical_name
    assert binding["chat_binding_status"] == "BOUND_SOURCE_ALIAS"


def test_structured_chat_alias_rejects_source_hash_drift(
    tmp_path, monkeypatch
):
    segment = tmp_path / "22966160_20260722-19-34-50.mp4"
    segment.write_bytes(b"drifted official replay")
    ledger = tmp_path / "subtitle-truth.json"
    ledger.write_text(
        json.dumps(
            {
                "schema_version": "source-subtitle-truth-ledger.v1",
                "source_aliases": [
                    {
                        "alias_id": "20260722-official-replay",
                        "alias_recording_basename": segment.name,
                        "alias_source_sha256": "sha256:" + "a" * 64,
                        "canonical_recording_basename": (
                            "22966160_20260722-19-35-15.mp4"
                        ),
                        "alias_timeline_offset_ms": 0,
                        "authority": "test authority",
                    }
                ],
                "entries": [],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        runner,
        "profile_asset_file",
        lambda key: ledger
        if key == "subtitle_truth_ledger"
        else (_ for _ in ()).throw(KeyError(key)),
    )

    with pytest.raises(
        runner.StructuredChatBindingError,
        match="STRUCTURED_CHAT_ALIAS_SOURCE_SHA256_MISMATCH",
    ):
        runner.resolve_structured_chat_binding(
            segment,
            source_sha256="sha256:" + "b" * 64,
        )


def test_structured_chat_without_sidecar_or_alias_is_explicitly_optional(
    tmp_path, monkeypatch
):
    segment = tmp_path / "legacy.mp4"
    segment.write_bytes(b"legacy")
    ledger = tmp_path / "subtitle-truth.json"
    ledger.write_text(
        json.dumps(
            {
                "schema_version": "source-subtitle-truth-ledger.v1",
                "source_aliases": [],
                "entries": [],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        runner,
        "profile_asset_file",
        lambda key: ledger
        if key == "subtitle_truth_ledger"
        else (_ for _ in ()).throw(KeyError(key)),
    )

    assert runner.resolve_structured_chat_binding(segment) == {
        "chat_jsonl": None,
        "structured_chat_required": False,
        "chat_binding_status": "OPTIONAL_ABSENT",
    }


def test_boundary_retry_tolerates_asr_derived_owner_jitter():
    """A widened witness-reserve retry re-derives the transcript, so a
    story-chat owner matched from fresh ASR may shift or vanish.  Only the
    immutable scope and committed-truth owners bind across attempts."""

    initial_spec = {
        "candidate_id": "candidate-a",
        "semantic_start_ms": 100_000,
        "semantic_end_ms": 110_000,
        "boundary_repair_extend_cap_ms": 30_000,
        "pieces": [{"start_ms": 95_000, "end_ms": 125_000}],
    }
    truth_owner = {
        "owner_kind": "source_subtitle_truth",
        "owner_id": "story-truth",
        "required": True,
        "source_start_ms": 104_000,
        "source_end_ms": 106_000,
        "local_windows": [{"start_ms": 9_000, "end_ms": 11_000}],
    }
    first_attempt_audit: dict[str, object] = {
        "entity_repairs": [
            {
                "matched_start_ms": 6_240,
                "matched_end_ms": 7_120,
            }
        ]
    }
    freeze_required_boundary_owner_contract(
        spec=initial_spec,
        durations=[30_000],
        chat_authority_audit=first_attempt_audit,
        required_boundary_owners=[dict(truth_owner)],
    )
    first_contract = first_attempt_audit["frozen_boundary_owner_contract"]
    assert first_contract["required_owner_count"] == 2

    retry_spec = {
        **initial_spec,
        "pieces": [{"start_ms": 95_000, "end_ms": 140_000}],
        "boundary_repair_extend_cap_ms": 60_000,
        "boundary_retry_frozen_owner_contract": first_contract,
    }
    # Fresh ASR on the retry: the entity repair row does not reoccur.
    retry_audit: dict[str, object] = {"entity_repairs": []}

    freeze_required_boundary_owner_contract(
        spec=retry_spec,
        durations=[45_000],
        chat_authority_audit=retry_audit,
        required_boundary_owners=[dict(truth_owner)],
    )

    frozen = retry_audit["frozen_boundary_owner_contract"]
    receipt = frozen["boundary_retry_owner_contract_verification"]
    assert receipt["status"] == "PASS"
    assert receipt["asr_derived_owner_binding"] == "per_attempt"
    assert (
        receipt["deterministic_owner_set_sha256"]
        == first_contract["deterministic_owner_set_sha256"]
    )
    assert frozen["owner_set_sha256"] != first_contract["owner_set_sha256"]


def test_boundary_retry_still_rejects_committed_truth_owner_drift():
    initial_spec = {
        "candidate_id": "candidate-a",
        "semantic_start_ms": 100_000,
        "semantic_end_ms": 110_000,
        "boundary_repair_extend_cap_ms": 30_000,
        "pieces": [{"start_ms": 95_000, "end_ms": 125_000}],
    }
    truth_owner = {
        "owner_kind": "source_subtitle_truth",
        "owner_id": "story-truth",
        "required": True,
        "source_start_ms": 104_000,
        "source_end_ms": 106_000,
        "local_windows": [{"start_ms": 9_000, "end_ms": 11_000}],
    }
    initial_audit: dict[str, object] = {}
    freeze_required_boundary_owner_contract(
        spec=initial_spec,
        durations=[30_000],
        chat_authority_audit=initial_audit,
        required_boundary_owners=[dict(truth_owner)],
    )

    retry_spec = {
        **initial_spec,
        "pieces": [{"start_ms": 95_000, "end_ms": 140_000}],
        "boundary_repair_extend_cap_ms": 60_000,
        "boundary_retry_frozen_owner_contract": initial_audit[
            "frozen_boundary_owner_contract"
        ],
    }

    with pytest.raises(
        RuntimeError,
        match="BOUNDARY_RETRY_OWNER_SET_DRIFT",
    ):
        freeze_required_boundary_owner_contract(
            spec=retry_spec,
            durations=[45_000],
            chat_authority_audit={},
            required_boundary_owners=[],
        )


def test_legacy_frozen_contract_without_deterministic_digest_stays_auditable():
    """Contracts frozen before the deterministic subset existed are immutable
    evidence (e.g. 2026-07-24's delivered auto_183122_1209_1410).  They must
    keep validating, while a present-but-wrong digest still fails closed."""

    spec = {
        "candidate_id": "candidate-a",
        "semantic_start_ms": 100_000,
        "semantic_end_ms": 110_000,
        "boundary_repair_extend_cap_ms": 30_000,
        "pieces": [{"start_ms": 95_000, "end_ms": 125_000}],
    }
    audit: dict[str, object] = {}
    freeze_required_boundary_owner_contract(
        spec=spec,
        durations=[30_000],
        chat_authority_audit=audit,
        required_boundary_owners=[
            {
                "owner_kind": "source_subtitle_truth",
                "owner_id": "story-truth",
                "required": True,
                "source_start_ms": 104_000,
                "source_end_ms": 106_000,
                "local_windows": [{"start_ms": 9_000, "end_ms": 11_000}],
            }
        ],
    )
    frozen = dict(audit["frozen_boundary_owner_contract"])

    legacy = {
        key: value
        for key, value in frozen.items()
        if key != "deterministic_owner_set_sha256"
    }
    legacy["contract_sha256"] = frozen_boundary_owner_contract_sha256(legacy)
    assert validate_frozen_boundary_owner_contract(legacy)["status"] == "FROZEN"

    tampered = dict(legacy)
    tampered["deterministic_owner_set_sha256"] = "sha256:" + "0" * 64
    tampered["contract_sha256"] = frozen_boundary_owner_contract_sha256(tampered)
    with pytest.raises(
        RuntimeError,
        match="BOUNDARY_RETRY_OWNER_SET_DRIFT",
    ):
        validate_frozen_boundary_owner_contract(tampered)


def test_produce_batch_yields_to_deploy_guard(tmp_path, monkeypatch):
    """Ivan 2026-07-27 部署优先令：deploy.guard 在场时不再派发新候选，
    在飞项完成、其余顺延下个 tick——马拉松 tick 不再压部署数小时。"""

    import threading

    monkeypatch.setattr(runner, "BASE", tmp_path)
    monkeypatch.setattr(runner, "MAX_PARALLEL_PRODUCE", 1)
    guard = tmp_path / "deploy.guard"
    calls = []
    gate = threading.Event()

    def fake_produce(date, item):
        calls.append(item["cid"])
        # 第一条生产期间部署 guard 落地
        guard.mkdir(exist_ok=True)
        gate.set()
        return {"cid": item["cid"], "status": "ok"}

    items = [{"cid": "a"}, {"cid": "b"}, {"cid": "c"}]
    results = runner.produce_batch("2026-07-27", items, fake_produce)

    assert calls == ["a"]  # b/c 顺延
    assert [row["cid"] for row in results] == ["a"]
