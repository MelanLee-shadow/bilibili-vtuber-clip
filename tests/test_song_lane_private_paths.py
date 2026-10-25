"""Exercise real lane/path/prepare gates; replace only media or provider work.

The ffmpeg smoke uses real, tiny synthetic media. Selector observations and the
positive audio-proof result are frozen test doubles, never production evidence.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest

import scripts.session_autoslice as runner

DATE = "2099-01-02"
CID = "song_private_fixture"
REAL_RUN = subprocess.run


def digest(path):
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def srt_text(marker="EXPLICIT"):
    spans = [(200, 202), *[(start, start + 8) for start in range(220, 321, 10)], (345, 350)]
    def stamp(seconds):
        return f"00:{seconds // 60:02d}:{seconds % 60:02d},000"
    return "\n\n".join(
        f"{n}\n{stamp(start)} --> {stamp(end)}\n{marker} lyric {n}"
        for n, (start, end) in enumerate(spans, 1)
    ) + "\n"


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    base = tmp_path.resolve() / "runtime"
    base.mkdir()
    source = tmp_path.resolve() / "source.mp4"
    source.write_bytes(b"synthetic source, replaced by real media in the smoke")
    transcript = tmp_path.resolve() / "source.bcut.srt"
    transcript.write_text(srt_text(), encoding="utf-8")
    global_srt = base / "cache" / DATE / "source.bcut.srt"
    global_srt.parent.mkdir(parents=True)
    global_srt.write_text(srt_text("GLOBAL"), encoding="utf-8")
    private = base / "private-runs" / "fixture"
    monkeypatch.setattr(runner, "BASE", base)
    monkeypatch.setattr(runner, "pipeline_fingerprint", lambda: "fixture-pipeline")
    monkeypatch.setattr(runner, "song_pipeline_fingerprint", lambda: "fixture-song")
    monkeypatch.setattr(runner, "song_selector_env", lambda _date: {"AUTOSLICE_BASE": str(base)})
    monkeypatch.setattr(runner, "cpa_qa_cmd", lambda: "fixture-provider-not-executed")
    monkeypatch.setattr(runner, "published_song_match", lambda _title: None)
    return SimpleNamespace(
        base=base, source=source, transcript=transcript, global_srt=global_srt,
        private=private, out=private / DATE / CID,
        item={"cid": CID, "segment_path": str(source), "seg_dur_ms": 900_000,
              "anchor_start_ms": 200_000, "anchor_end_ms": 350_000},
    )


def selector_double(monkeypatch, *, records=None, real_media=False):
    calls = []
    def run(command, **kwargs):
        if command[0] == "ffmpeg":
            if real_media:
                return REAL_RUN(command, **kwargs)
            Path(command[-1]).write_bytes(b"fixture window media")
            return SimpleNamespace(returncode=0, stderr="")
        assert "--output-dir" in command, command
        output = Path(command[command.index("--output-dir") + 1])
        transcript = Path(command[command.index("--source-srt") + 1])
        video = Path(command[command.index("--source-video") + 1])
        inner = command[command.index("--seed-song-candidate-id") + 1]
        call = {"output": output, "transcript": transcript, "video": video,
                "text": transcript.read_text(), "env": kwargs["env"],
                "full": "--agy-audio-lrc-align" in command, "inner": inner}
        index = len(calls)
        calls.append(call)
        record = records(index, call) if records else {
            "candidate_id": inner, "decision_action": "BLOCK", "reason_codes": ["SONG_PARTIAL"],
            "source_context_job": {"song_candidate": True},
        }
        (output / "summary.json").write_text(json.dumps({"records": [record]}))
        kwargs["stdout"].write(f"fixture selector attempt {index}\n")
        Path(kwargs["env"]["AUTOSLICE_CPA_USAGE_FILE"]).open("a").write('{"fixture":true}\n')
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(runner.subprocess, "run", run)
    return calls


def private_produce(runtime, **kwargs):
    return runner.produce_song(DATE, runtime.item, prepare_only=True,
                               output_root=runtime.private, source_srt=runtime.transcript, **kwargs)


def assert_private_work(runtime, calls, result):
    assert calls
    for call in calls:
        assert call["output"].is_relative_to(runtime.out / "song-selector")
        assert call["transcript"].is_relative_to(runtime.out)
        assert call["video"].is_relative_to(runtime.out)
        assert "EXPLICIT" in call["text"] and "GLOBAL" not in call["text"]
        assert Path(call["env"]["AUTOSLICE_CPA_USAGE_FILE"]).is_relative_to(runtime.out)
        # The output location must not substitute a new authority/cache runtime.
        assert call["env"]["AUTOSLICE_BASE"] == str(runtime.base)
    assert Path(result["log"]).is_relative_to(runtime.out)
    assert not (runtime.base / "out").exists()
    assert not (runtime.base / "logs").exists()
    assert not (runtime.base / "state").exists()
    assert not (runtime.base / "reports").exists()
    assert (runtime.private / "reports" / "production-attempts" / f"{DATE}.jsonl").is_file()


def test_default_entry_keeps_original_paths(runtime, monkeypatch):
    calls = selector_double(monkeypatch)
    result = runner.produce_song(DATE, runtime.item)
    original_out = runtime.base / "out" / DATE / CID
    assert [call["full"] for call in calls] == [False, True]
    assert all(call["output"].is_relative_to(original_out) for call in calls)
    assert all("GLOBAL" in call["text"] and "EXPLICIT" not in call["text"] for call in calls)
    assert Path(result["log"]) == runtime.base / "logs" / f"{DATE}_{CID}.log"
    assert "delivered" not in result


@pytest.mark.parametrize("full_reason", ["SONG_PARTIAL", "SONG_BACKGROUND_PLAYBACK_ONLY", "UNKNOWN_FIXTURE_VERDICT"])
def test_private_tight_full_keeps_negative_and_unknown_results(runtime, monkeypatch, full_reason):
    def records(index, call):
        return {"candidate_id": call["inner"], "decision_action": "BLOCK",
                "reason_codes": ["SONG_PARTIAL" if index == 0 else full_reason],
                "source_context_job": {"song_candidate": True}}
    calls = selector_double(monkeypatch, records=records)
    original = runtime.transcript.read_bytes()
    result = private_produce(runtime)
    assert [call["full"] for call in calls] == [False, True]
    assert_private_work(runtime, calls, result)
    assert result["full_source_authoritative_block"] is True
    assert result["song_complete"] is False
    assert full_reason in result["reason_codes"]
    assert "delivered" not in result and "prepared_delivery" not in result
    assert runtime.transcript.read_bytes() == original


def test_private_core_retry_reads_the_explicit_transcript(runtime, monkeypatch):
    def records(index, call):
        return {"candidate_id": call["inner"], "decision_action": "BLOCK",
                "reason_codes": ["SONG_PARTIAL"],
                "source_context_job": {"song_candidate": index > 0}}
    runtime.global_srt.write_text("", encoding="utf-8")
    calls = selector_double(monkeypatch, records=records)
    result = private_produce(runtime)
    assert len(calls) == 2 and "_core" in calls[1]["output"].parent.name
    assert result["retried_core"] is True
    assert_private_work(runtime, calls, result)
    assert "delivered" not in result and "prepared_delivery" not in result


def test_private_full_source_resume_has_no_tight_replay(runtime, monkeypatch):
    runtime.item["resume_full_source"] = True
    calls = selector_double(monkeypatch)
    result = private_produce(runtime)
    assert [call["full"] for call in calls] == [True]
    assert result["resumed_full_source_after_transient"] is True
    assert_private_work(runtime, calls, result)


def test_missing_explicit_transcript_never_falls_back_to_global(runtime, monkeypatch):
    runtime.transcript.unlink()
    calls = selector_double(monkeypatch)
    result = private_produce(runtime)
    assert calls == []
    assert result["status"] == "failed"
    assert result["transient_failure_code"] == "SONG_SOURCE_TRANSCRIPT_EMPTY"
    assert "delivered" not in result and "prepared_delivery" not in result


@pytest.mark.parametrize("case", ["commit", "missing_source", "outside_scope", "unsafe_id", "symlink"])
def test_private_input_contract_rejects_before_media_or_provider(runtime, monkeypatch, case):
    calls = selector_double(monkeypatch)
    kwargs = {"prepare_only": True, "output_root": runtime.private, "source_srt": runtime.transcript}
    if case == "commit":
        kwargs["prepare_only"] = False
    elif case == "missing_source":
        kwargs.pop("source_srt")
    elif case == "outside_scope":
        kwargs["output_root"] = runtime.base / "out"
    elif case == "unsafe_id":
        runtime.item["cid"] = "../escape"
    else:
        runtime.private.parent.mkdir()
        runtime.private.symlink_to(runtime.base, target_is_directory=True)
    with pytest.raises(ValueError, match="private Song"):
        runner.produce_song(DATE, runtime.item, **kwargs)
    assert calls == []
    assert not (runtime.base / "out").exists()


def test_previously_published_song_still_stops_before_private_production(runtime, monkeypatch):
    runtime.item["title_hint"] = "published fixture song"
    monkeypatch.setattr(runner, "published_song_match", lambda _title: {"bvid": "fixture"})
    calls = selector_double(monkeypatch)
    result = private_produce(runtime)
    assert result["reason_codes"] == ["SONG_PREVIOUSLY_PUBLISHED"]
    assert calls == [] and not (runtime.base / "out").exists()
    assert not list(runtime.private.rglob("*.mp4"))


def install_test_deployment(base):
    """Synthetic deployment seal, confined to this test's temporary runtime."""
    repo = base / "repo"
    repo.mkdir()
    body = {"schema_version": "deployed-authority-manifest.v1", "deployed_commit": "a" * 40,
            "entries": {"docs/pipeline/50-song-lane.md": {"bytes": 1, "sha256": "sha256:" + "b" * 64}}}
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    (repo / "DEPLOYED_COMMIT").write_text("a" * 40 + "\n")
    (repo / "DEPLOYED_AUTHORITY_MANIFEST.json").write_text(
        json.dumps({**body, "manifest_sha256": "sha256:" + hashlib.sha256(encoded).hexdigest()}))


def ready_selector_record(call):
    """Frozen provider/materialization result; the real delivery verifier consumes it."""
    inner = call["inner"]
    root = call["output"] / inner / "replacement_recuts"
    root.mkdir(parents=True)
    paths = {"media": root / f"{inner}.recut.mp4", "burned": root / f"{inner}.recut.burned-final.mp4",
             "subtitle": root / f"{inner}.recut.srt", "manifest": root / f"{inner}.recut.manifest.json",
             "alignment": root / "alignment.json", "proof": root / "host-vocal.json"}
    for role, path in paths.items():
        path.write_bytes((f"OFFLINE_TEST frozen {role}\n").encode())
    paths["subtitle"].write_text("1\n00:00:00,000 --> 00:00:01,000\nfixture lyric\n")
    gate = {"schema_version": "slice-cover-release-gate.v1", "candidate_id": inner,
            "decision_action": "AUTO_UPLOAD", "reason_codes": ["SONG_FULL_BOUNDARY_READY"],
            "satisfied": False}
    record = {"candidate_id": inner, "decision_action": "AUTO_UPLOAD",
              "reason_codes": ["SONG_FULL_BOUNDARY_READY"], "source_context_job": {
                  "song_candidate": True,
                  "song_boundary": {"status": "FULL_SONG_READY", "song_title": "路径测试"},
                  "lyrics_alignment": {"status": "READY", "model": "OFFLINE_TEST-agy-audio-lrc-global-shift-v1",
                                       "alignment_report_path": str(paths["alignment"]),
                                       "alignment_report_sha256": digest(paths["alignment"])},
              },
              "materialized_recut": {
                  "status": "MATERIALIZED", "candidate_id": inner,
                  "media_path": str(paths["media"]), "subtitle_path": str(paths["subtitle"]),
                  "manifest_path": str(paths["manifest"]), "manifest_sha256": digest(paths["manifest"]),
                  "burned_preview": {"status": "BURNED", "path": str(paths["burned"]),
                                     "burned_sha256": digest(paths["burned"])},
                  "artifact_hashes": {"video_sha256": digest(paths["media"]),
                                      "burned_video_sha256": digest(paths["burned"]),
                                      "subtitle_sha256": digest(paths["subtitle"])},
                  "cover_release_gate": gate,
                  "publish_staging": {"status": "SKIPPED_RELEASE_GATE", "decision_action": "AUTO_UPLOAD",
                                      "title": "【李豆沙】豆沙歌，《路径测试》", "upload_enabled": False},
              }}
    record["_fixture_completion"] = {
        "ready": True, "reason_codes": [], "lyrics_alignment_status": "READY",
        "alignment_report_path": str(paths["alignment"]), "alignment_report_sha256": digest(paths["alignment"]),
        "host_vocal_proof_path": str(paths["proof"]), "host_vocal_proof_sha256": digest(paths["proof"]),
        "host_vocal_status": "READY", "host_vocal_decision": runner.HOST_VOCAL_PRESENT_DECISION,
        "live_performance_status": "READY", "live_performance_mode": "LIVE_STREAMER_SINGING",
        "joint_singing_decision": runner.VERIFIED_HOST_SINGING_DECISION,
    }
    return record


@pytest.mark.parametrize("proof_decision", ["positive", "negative", "unknown"])
def test_private_prepare_runs_existing_transaction_and_host_gate(runtime, monkeypatch, proof_decision):
    install_test_deployment(runtime.base)
    delivery = runtime.base / "delivery"
    monkeypatch.setattr(runner, "profile_delivery_root", lambda: delivery)
    monkeypatch.setattr(runner, "generate_upload_tags", lambda *_args, **_kw: {
        "engine": "OFFLINE_TEST", "status": "OK_NO_LLM", "final_tags": ["李豆沙"], "final_tag_line": "李豆沙"})
    original_completion = runner.song_completion_evidence
    monkeypatch.setattr(runner, "song_completion_evidence",
                        lambda record: record.get("_fixture_completion") or original_completion(record))
    def records(index, call):
        if index == 0:
            return {"candidate_id": call["inner"], "decision_action": "BLOCK", "reason_codes": ["SONG_PARTIAL"],
                    "source_context_job": {"song_candidate": True}}
        record = ready_selector_record(call)
        if proof_decision != "positive":
            record["_fixture_completion"]["host_vocal_decision"] = proof_decision.upper()
        return record
    calls = selector_double(monkeypatch, records=records)
    result = private_produce(runtime)
    assert [call["full"] for call in calls] == [False, True]
    assert_private_work(runtime, calls, result)
    assert "delivered" not in result and not delivery.exists()
    if proof_decision != "positive":
        assert "prepared_delivery" not in result
        assert not (runtime.base / ".prepared-deliveries").exists()
        assert result["status"] == "blocked"
        return
    assert result["status"] == "delivery_prepared_no_target", result
    handle = Path(result["prepared_delivery"]["manifest_path"])
    assert handle.is_relative_to(runtime.base / ".prepared-deliveries" / "song" / CID)
    document = json.loads(handle.read_text())
    assert document["deployed_authority"]["commit"] == "a" * 40
    assert result["prepared_delivery"]["upload_enabled"] is False
    assert all(Path(row["source_path"]).is_relative_to(runtime.out) for row in document["artifacts"])
    assert all(not Path(row["target_path"]).exists() for row in document["artifacts"])


@pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
                    reason="real media path smoke requires ffmpeg/ffprobe")
def test_private_real_ffmpeg_window_and_srt_smoke(runtime, monkeypatch):
    command = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
               "-f", "lavfi", "-i", "color=c=blue:s=160x90:r=10:d=4",
               "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
               "-c:v", "libx264", "-threads", "1", "-c:a", "aac", "-shortest", str(runtime.source)]
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    REAL_RUN(command, check=True, capture_output=True, timeout=30, env=env)
    runtime.item.update(seg_dur_ms=4000, anchor_start_ms=1000, anchor_end_ms=3000)
    runtime.transcript.write_text("1\n00:00:01,000 --> 00:00:03,000\nEXPLICIT synthetic lyric\n")
    source_hash = digest(runtime.source)
    calls = selector_double(monkeypatch, real_media=True)
    result = private_produce(runtime)
    assert len(calls) == 1
    assert_private_work(runtime, calls, result)
    probe = REAL_RUN(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json",
                      str(calls[0]["video"])], check=True, capture_output=True, text=True, timeout=15, env=env)
    assert 3.8 <= float(json.loads(probe.stdout)["format"]["duration"]) <= 4.2
    REAL_RUN(["ffmpeg", "-nostdin", "-v", "error", "-i", str(calls[0]["video"]), "-f", "null", "-"],
             check=True, capture_output=True, timeout=30, env=env)
    assert digest(runtime.source) == source_hash
    assert result["song_complete"] is False and "prepared_delivery" not in result
