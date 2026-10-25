"""Normal producer -> actual CLI/factory/audio -> disk -> real pronoun consumer.

Only the child ML loader/similarity and external text-CPA boundaries are fake.
All files and tone audio are synthetic. No human labels or real .env inputs.
"""

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import socket
import sys
import wave

import pytest

from src.autoslice import producer_text_pipeline as pipeline
from src.autoslice import host_vocal_proof as hv
from tests.test_producer_text_pipeline_final_review import _adapters, _correction_pass

SRT = (
    "1\n00:00:00,200 --> 00:00:01,200\n她说第一句\n\n"
    "2\n00:00:01,500 --> 00:00:02,500\n她说第二句\n\n"
    "3\n00:00:02,800 --> 00:00:03,800\n她说第三句\n"
)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_wave(path, seconds=8, amplitude=1200):
    import array

    # Distinct time sections make shifted extraction verifiable without listening.
    samples = array.array("h", (amplitude + (n // 1600) % 20 for n in range(seconds * 16000)))
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(16000)
        out.writeframes(samples.tobytes())


@pytest.fixture
def case(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("No network in producer-generation tests")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    media = tmp_path / "padded.wav"
    write_wave(media)
    model = tmp_path / "model"
    model.mkdir()
    (model / "weights").write_bytes(b"Synthetic model fixture, not loadable ML weights")
    refs = tmp_path / "references"
    refs.mkdir()
    reference_rows = []
    for index in range(3):
        p = refs / f"r{index}.wav"
        write_wave(p, 1, 1500 + index)
        reference_rows.append({"id": f"r{index}", "filename": p.name, "sha256": sha(p)})
    profile = tmp_path / "profile.json"
    profile.write_text(
        json.dumps(
            {
                "schema_version": hv.GENERIC_PROFILE_SCHEMA_VERSION,
                "profile_id": "synthetic-enrollment",
                "subject": hv.CHANNEL_PROFILE.display_name,
                "policy": hv._canonical_policy(),
                "model": {
                    "model_id": "synthetic-campp",
                    "tree_sha256": hv._sha256_directory(model),
                },
                "references": reference_rows,
            }
        )
    )
    runtime = tmp_path / "synthetic-speaker-python"
    runtime.write_text(
        "#!"
        + sys.executable
        + "\n"
        + """
import json, pathlib, runpy, socket, sys, wave
assert sys.argv[1:4] == ['-B','-m','src.autoslice.speaker_attribution']
sys.path.insert(0, str(pathlib.Path.cwd()))
def forbidden(*a, **k): raise AssertionError('No network in child ML fake')
socket.socket.connect = forbidden
from src.autoslice import speaker_finalizer as f
f._load_campplus_pipeline = lambda path: object()
def build(**kwargs):
    def similarity(ref, cue):
        with wave.open(str(cue), 'rb') as wav:
            assert wav.getframerate() == 16000
            assert wav.getnframes() > 0
        return {'cue-0001.wav':.85, 'cue-0002.wav':.82}.get(cue.name, .20)
    return similarity
f._build_embedding_similarity = build
sys.argv = ['speaker_attribution'] + sys.argv[4:]
runpy.run_module('src.autoslice.speaker_attribution', run_name='__main__')
"""
    )
    runtime.chmod(0o700)
    prompts = []

    def pronoun(prompt):
        prompts.append(prompt)
        return json.dumps(
            {
                "decisions": [
                    {
                        "occurrence_id": f"cue-{i}-occurrence-1",
                        "action": "KEEP_CURRENT",
                        "current_token": "她",
                        "replacement_token": "她",
                        "referent": "UNSPECIFIED",
                        "reason": "Synthetic neutral external CPA decision",
                    }
                    for i in (1, 2, 3)
                ]
            }
        )

    monkeypatch.setattr(pipeline, "_build_pronoun_audit_llm_call", lambda: pronoun)
    monkeypatch.setattr(
        pipeline, "_build_final_review_llm_call", lambda: lambda _: '{"findings": []}'
    )
    return {
        "media": media,
        "profile": profile,
        "model": model,
        "refs": refs,
        "runtime": runtime,
        "prompts": prompts,
        "out": tmp_path / "out",
        "tmp": tmp_path,
    }


def call_producer(
    case,
    *,
    srt=SRT,
    offset=0,
    end=4000,
    enabled=True,
    authority=None,
    source_claim="actual",
    extra_options=None,
):
    from src.autoslice.clip_context import _canonical_json_sha256

    context = {
        "schema_version": "lidousha-clip-context.v1",
        "candidate_id": "synthetic-member",
        "recording_date": "2026-10-16",
        "selection_hook": "Synthetic multi-speaker clip",
        "pieces": [
            {"start_ms": 0, "end_ms": 8000, "source_media_sha256": "sha256:" + sha(case["media"])}
        ],
        "whole_clip_draft_srt": srt,
        "whole_clip_draft_srt_sha256": "sha256:" + hashlib.sha256(srt.encode()).hexdigest(),
        "structured_chat": [],
        "topic_resolution": {},
        "session_topic_authorities": [],
        "speech_memory": {"entries": []},
        "retrieval_budget": {"whole_clip_transcript_truncated": False},
        "mutation_authorized": False,
    }
    context["context_sha256"] = _canonical_json_sha256(context)
    opts = {
        "enabled": enabled,
        "speaker_python": str(case["runtime"]),
        "reference_dir": str(case["refs"]),
        "model_dir": str(case["model"]),
    }
    opts.update(extra_options or {})
    spec = {"date": "2026-10-16", "speaker_attribution_generation": opts}
    if source_claim is not None:
        spec["source_piece_clock"] = {
            "padded_sha256": sha(case["media"]) if source_claim == "actual" else source_claim
        }
    case["out"].mkdir(exist_ok=True)
    callback = pipeline._build_exact_final_review_callback(
        spec=spec,
        out_root=case["out"],
        cid="synthetic-member",
        padded=case["media"],
        adapters=replace(_adapters(), profile_asset_file=lambda _: case["profile"]),
        authoritative_chat=(),
        clip_context=context,
        final_review_audit=_correction_pass(),
        verify_confusable_entity=None,
        truth_ownership=None,
        screen_read_probe=None,
        # Existing text-recovery mode avoids unrelated boundary-model dispatch;
        # the real final-review and pronoun consumers still run and stay BLOCK.
        text_recovery_only=True,
    )
    authority = {} if authority is None else authority
    result = callback(srt, authority, offset, end)
    return result, authority


def test_normal_producer_generates_persists_and_consumes_without_identity_input(case):
    result, authority = call_producer(case)
    assert case["prompts"], result
    assert "1. HOST（voiceprint_anchor）" in case["prompts"][0]
    assert "2. HOST（voiceprint_anchor）" in case["prompts"][0]
    assert "3. OTHER" not in case["prompts"][0]
    meta = result["speaker_attribution_generation"]
    assert meta["status"] == "GENERATED", meta
    doc = json.loads(Path(meta["attribution_path"]).read_text())
    assert doc == authority["speaker_attribution"] == result["speaker_attribution"]
    assert sha(Path(meta["attribution_path"])) == meta["attribution_sha256"]
    assert doc["runtime"]["context_attempted"] is False
    assert result["release_gate"] == "BLOCK"  # Optional S2 never clears normal gates.
    assert "speaker_attribution_context_sha256" in result["candidate_pronoun_consistency_audit"]


def test_timeout_report_cannot_be_accepted_with_a_zero_exit_status(case, monkeypatch):
    from src.autoslice import producer_speaker_attribution as producer

    execute = producer._execute

    def timeout_at_exit(command, directory):
        execution = execute(command, directory)
        assert execution["returncode"] == 0
        execution["timed_out"] = True  # Timeout can race with normal child exit.
        return execution

    monkeypatch.setattr(producer, "_execute", timeout_at_exit)
    first, authority = call_producer(case)
    meta = first["speaker_attribution_generation"]
    assert meta["status"] == "UNKNOWN"
    assert meta["reason_code"] == "SPEAKER_RUNTIME_FAILED"
    assert {row["speaker"] for row in authority["speaker_attribution"]["cues"]} == {"UNKNOWN"}
    directory = Path(meta["attribution_path"]).parent
    raw = directory / "attribution.raw.json"
    saved = raw.read_bytes()
    assert not (directory / "terminal.json").exists()
    assert json.loads((directory / "execution.json").read_text())["timed_out"] is True
    monkeypatch.setattr(producer, "_execute", lambda *a: pytest.fail("Do not rerun timed-out request"))
    second, _ = call_producer(case)
    assert second["speaker_attribution_generation"]["status"] == "UNKNOWN"
    assert raw.read_bytes() == saved


def test_cold_callback_reuses_completed_disk_result_and_reaches_consumer(case):
    first, _ = call_producer(case)
    meta = first["speaker_attribution_generation"]
    raw = Path(meta["attribution_path"]).read_bytes()
    case["prompts"].clear()
    second, _ = call_producer(case)  # New callback, empty authority, same disk inputs.
    assert second["speaker_attribution_generation"]["status"] == "CACHE_HIT"
    assert Path(meta["attribution_path"]).read_bytes() == raw
    assert "1. HOST（voiceprint_anchor）" in case["prompts"][0]
    assert len(list((case["out"] / "speaker-attribution").glob("*/execution.json"))) == 1


@pytest.mark.parametrize("offset", [0, 2300])
def test_normal_producer_translates_clock_and_exact_pcm_samples(case, offset):
    from src.autoslice.speaker_attribution import attribution_context_for_srt

    result, _ = call_producer(case, offset=offset, end=offset + 4000)
    meta = result["speaker_attribution_generation"]
    assert meta["status"] == "GENERATED", meta
    doc = result["speaker_attribution"]
    binding = doc["source_binding"]
    assert binding["schema_version"] == "speaker-attribution-source.v2"
    assert binding["timeline_offset_ms"] == offset
    directory = Path(meta["attribution_path"]).parent
    with wave.open(str(case["media"]), "rb") as wav:
        original = wav.readframes(wav.getnframes())
    for window, item in zip(binding["geometry"]["cue_windows"], doc["cue_audio"], strict=True):
        a, b = window["extracted_source_window_ms"]
        assert window["source_window_ms"] == [x + offset for x in window["target_window_ms"]]
        with wave.open(str(directory / "work" / item["relative_path"]), "rb") as wav:
            assert wav.readframes(wav.getnframes()) == original[a * 16 * 2 : b * 16 * 2]
    assert "1. HOST" in attribution_context_for_srt(
        doc,
        SRT,
        expected_source_media_sha256=sha(case["media"]),
        expected_timeline_offset_ms=offset,
    )
    assert attribution_context_for_srt(doc, SRT, expected_timeline_offset_ms=offset + 1) == ""
    assert "1. HOST（voiceprint_anchor）" in case["prompts"][0]


@pytest.mark.parametrize(
    "change",
    ["media", "srt", "profile", "clock", "end", "registered_model", "registered_ref", "runtime"],
)
def test_cold_changed_inputs_never_reuse_prior_request(case, change):
    first, _ = call_producer(case)
    old = first["speaker_attribution_generation"]
    original = Path(old["attribution_path"]).read_bytes()
    kwargs = {}
    if change == "media":
        write_wave(case["media"], amplitude=2300)
    elif change == "srt":
        kwargs["srt"] = SRT.replace("第一句", "另外一句")
    elif change == "profile":
        p = json.loads(case["profile"].read_text())
        p["profile_id"] = "new-registration"
        case["profile"].write_text(json.dumps(p))
    elif change == "clock":
        kwargs.update(offset=2000, end=6000)
    elif change == "end":
        kwargs["end"] = 4200
    elif change == "registered_model":
        (case["model"] / "weights").write_bytes(b"new synthetic model bytes")
        p = json.loads(case["profile"].read_text())
        p["model"]["tree_sha256"] = hv._sha256_directory(case["model"])
        case["profile"].write_text(json.dumps(p))
    elif change == "registered_ref":
        write_wave(case["refs"] / "r0.wav", 1, 3100)
        p = json.loads(case["profile"].read_text())
        p["references"][0]["sha256"] = sha(case["refs"] / "r0.wav")
        case["profile"].write_text(json.dumps(p))
    else:
        with case["runtime"].open("a") as f:
            f.write("\n# Synthetic runtime revision\n")
    case["prompts"].clear()
    second, _ = call_producer(case, **kwargs)
    new = second["speaker_attribution_generation"]
    assert new["status"] == "GENERATED", new
    assert new["attribution_path"] != old["attribution_path"]
    assert Path(old["attribution_path"]).read_bytes() == original
    assert "1. HOST（voiceprint_anchor）" in case["prompts"][0]


@pytest.mark.parametrize(
    "change",
    ["model_drift", "reference_drift", "missing_model", "missing_runtime", "missing_profile"],
)
def test_invalid_assets_drop_old_identity_without_new_generation(case, change):
    first, _ = call_producer(case)
    old = Path(first["speaker_attribution_generation"]["attribution_path"]).read_bytes()
    if change == "model_drift":
        (case["model"] / "weights").write_bytes(b"drift")
    elif change == "reference_drift":
        write_wave(case["refs"] / "r0.wav", 1, 1700)
    elif change == "missing_model":
        (case["model"] / "weights").unlink()
    elif change == "missing_runtime":
        case["runtime"].unlink()
    else:
        case["profile"].unlink()
    case["prompts"].clear()
    second, _ = call_producer(case, authority={"speaker_attribution": first["speaker_attribution"]})
    assert second["speaker_attribution_generation"]["status"] == "UNKNOWN"
    assert {r["speaker"] for r in second["speaker_attribution"]["cues"]} == {"UNKNOWN"}
    assert "voiceprint_anchor" not in case["prompts"][0]
    assert Path(first["speaker_attribution_generation"]["attribution_path"]).read_bytes() == old
    assert len(list((case["out"] / "speaker-attribution").glob("*/execution.json"))) == 1


@pytest.mark.parametrize("offset,end", [(1000, 4000), (5000, 9000), (-1, 4000), (0, 0)])
def test_unsupported_or_outside_media_clock_is_unknown_not_offset_zero(case, offset, end):
    result, _ = call_producer(case, offset=offset, end=end)
    assert result["speaker_attribution_generation"]["status"] == "UNKNOWN"
    assert {r["speaker"] for r in result["speaker_attribution"]["cues"]} == {"UNKNOWN"}
    assert "voiceprint_anchor" not in case["prompts"][0]


@pytest.mark.parametrize("mode", ["low_scores", "one_host", "missing_ml", "runtime_exit"])
def test_no_acoustic_support_or_runtime_failure_never_manufactures_identity(case, mode):
    text = case["runtime"].read_text()
    if mode == "low_scores":
        text = text.replace(
            "'cue-0001.wav':.85, 'cue-0002.wav':.82", "'cue-0001.wav':.20, 'cue-0002.wav':.21"
        )
    elif mode == "one_host":
        text = text.replace("'cue-0002.wav':.82", "'cue-0002.wav':.20")
    elif mode == "missing_ml":
        text = text.replace(
            "lambda path: object()",
            "lambda path: (_ for _ in ()).throw(ImportError('Synthetic no ML'))",
        )
    else:
        text = "#!" + sys.executable + "\nimport sys\nsys.exit(17)\n"
    case["runtime"].write_text(text)
    first, _ = call_producer(case)
    assert {r["speaker"] for r in first["speaker_attribution"]["cues"]} == {"UNKNOWN"}
    assert "voiceprint_anchor" not in case["prompts"][0]
    case["prompts"].clear()
    second, _ = call_producer(case)
    assert {r["speaker"] for r in second["speaker_attribution"]["cues"]} == {"UNKNOWN"}
    assert "voiceprint_anchor" not in case["prompts"][0]
    assert len(list((case["out"] / "speaker-attribution").glob("*/execution.json"))) == 1
    if mode in ("low_scores", "one_host"):
        assert second["speaker_attribution_generation"]["status"] == "CACHE_HIT"
    else:
        assert second["speaker_attribution_generation"]["status"] == "UNKNOWN"


@pytest.mark.parametrize("enabled", [False, None, 1, "true"])
def test_no_explicit_true_opt_in_preserves_original_path(case, enabled):
    result, _ = call_producer(case, enabled=enabled)
    assert "speaker_attribution_generation" not in result
    assert not (case["out"] / "speaker-attribution").exists()
    assert case["prompts"] and "voiceprint_anchor" not in case["prompts"][0]


@pytest.mark.parametrize(
    "damage",
    [
        "fake_host",
        "non_host",
        "relative_host",
        "geometry",
        "duplicate",
        "model",
        "reference",
        "flags",
        "audio",
        "srt",
        "partial",
    ],
)
def test_cold_corrupt_or_forged_identity_is_not_reused_or_reexecuted(case, damage):
    first, _ = call_producer(case)
    raw = Path(first["speaker_attribution_generation"]["attribution_path"])
    directory = raw.parent
    doc = json.loads(raw.read_text())
    if damage == "fake_host":
        doc["cues"][2].update(speaker="HOST", evidence="voiceprint_anchor")
        doc["cues"][2]["voiceprint"]["anchor"] = "HOST"
    elif damage == "non_host":
        doc["cues"][2]["voiceprint"]["anchor"] = "NON_HOST"
    elif damage == "relative_host":
        doc["cues"][0]["evidence"] = "voiceprint_relative_anchor"
    elif damage == "geometry":
        doc["source_binding"]["geometry"]["cue_windows"][0]["source_window_ms"][0] += 1
    elif damage == "duplicate":
        doc["cues"][1] = doc["cues"][0]
    elif damage == "model":
        doc["source_binding"]["model_tree_sha256"] = "a" * 64
    elif damage == "reference":
        doc["source_binding"]["references"][0]["sha256"] = "b" * 64
    elif damage == "flags":
        doc["display_authorized"] = True
    elif damage == "audio":
        with (directory / "work/cue-wavs/cue-0001.wav").open("ab") as f:
            f.write(b"tampered synthetic audio")
    elif damage == "srt":
        (directory / "input.srt").write_text(SRT.replace("第一句", "different"))
    else:
        (directory / "terminal.json").unlink()
    if damage not in ("audio", "srt", "partial"):
        raw.write_text(json.dumps(doc))
        # Even when the incidental checksum is updated, semantic/source guards must reject.
        terminal = json.loads((directory / "terminal.json").read_text())
        terminal["raw_sha256"] = sha(raw)
        (directory / "terminal.json").write_text(json.dumps(terminal))
    altered = raw.read_bytes()
    case["prompts"].clear()
    second, _ = call_producer(case)
    assert second["speaker_attribution_generation"]["status"] == "UNKNOWN"
    assert "voiceprint_anchor" not in case["prompts"][0]
    assert raw.read_bytes() == altered
    assert len(list((case["out"] / "speaker-attribution").glob("*/execution.json"))) == 1


def test_two_scores_from_same_audio_do_not_count_as_independent_support(case):
    duplicate = SRT.replace("00:00:01,500 --> 00:00:02,500", "00:00:00,200 --> 00:00:01,200")
    result, _ = call_producer(case, srt=duplicate)
    assert result["speaker_attribution_generation"]["status"] == "UNKNOWN"
    assert "voiceprint_anchor" not in case["prompts"][0]


def test_no_preconstructed_source_receipt_is_required(case):
    result, authority = call_producer(case, source_claim=None)
    assert result["speaker_attribution_generation"]["status"] == "GENERATED"
    assert authority["speaker_attribution"]["source_binding"]["media_sha256"] == sha(case["media"])
    assert "1. HOST（voiceprint_anchor）" in case["prompts"][0]


def test_declared_current_source_mismatch_does_not_dispatch_or_consume(case):
    result, _ = call_producer(case, source_claim="f" * 64)
    assert (
        result["speaker_attribution_generation"]["reason_code"] == "SPEAKER_CURRENT_SOURCE_MISMATCH"
    )
    assert not list((case["out"] / "speaker-attribution").glob("*/execution.json"))
    assert "voiceprint_anchor" not in case["prompts"][0]


def test_missing_optional_evidence_storage_is_not_a_new_whole_clip_gate(case):
    case["out"].mkdir()
    (case["out"] / "speaker-attribution").write_text("Synthetic storage collision")
    result, _ = call_producer(case)
    assert result["speaker_attribution_generation"]["status"] == "UNKNOWN"
    assert "SPEAKER_GENERATION_IO_UNAVAILABLE" in result["speaker_attribution"]["reason_codes"]
    assert case["prompts"] and "voiceprint_anchor" not in case["prompts"][0]
    assert result["reason_codes"] == ["FINAL_REVIEW_BOUNDARY_SEMANTIC_BLOCKED"]


def test_non_boolean_context_option_does_not_dispatch_or_load_provider_env(case):
    result, _ = call_producer(case, extra_options={"context": "true"})
    assert (
        result["speaker_attribution_generation"]["reason_code"]
        == "SPEAKER_CONTEXT_OPTION_INVALID"
    )
    assert not list((case["out"] / "speaker-attribution").glob("*/execution.json"))
    assert "voiceprint_anchor" not in case["prompts"][0]


def test_input_drift_while_real_child_runs_discards_identity(case):
    text = case["runtime"].read_text()
    text = text.replace(
        "f._load_campplus_pipeline = lambda path: object()",
        """
def drifting_loader(path):
    media = pathlib.Path(sys.argv[sys.argv.index('--media')+1])
    with media.open('ab') as stream: stream.write(b'changed during synthetic model load')
    return object()
f._load_campplus_pipeline = drifting_loader""",
    )
    case["runtime"].write_text(text)
    result, _ = call_producer(case)
    assert result["speaker_attribution_generation"]["status"] == "UNKNOWN"
    assert "voiceprint_anchor" not in case["prompts"][0]
    raws = list((case["out"] / "speaker-attribution").glob("*/attribution.raw.json"))
    assert len(raws) == 1
    assert "SCORER_SOURCE_DRIFT" in json.loads(raws[0].read_text())["reason_codes"]


def test_overlapping_scores_with_another_disjoint_support_are_not_blanket_rejected(case):
    text = case["runtime"].read_text().replace(".get(cue.name, .20)", ".get(cue.name, .85)")
    case["runtime"].write_text(text)
    duplicate = SRT.replace("00:00:01,500 --> 00:00:02,500", "00:00:00,200 --> 00:00:01,200")
    result, _ = call_producer(case, srt=duplicate)
    assert result["speaker_attribution_generation"]["status"] == "GENERATED"
    assert "3. HOST（voiceprint_anchor）" in case["prompts"][0]


@pytest.mark.parametrize("artifact", ["execution.json", "stdout.log", "stderr.log", "started.json"])
def test_incomplete_or_changed_execution_custody_cannot_replay(case, artifact):
    first, _ = call_producer(case)
    directory = Path(first["speaker_attribution_generation"]["attribution_path"]).parent
    path = directory / artifact
    with path.open("a") as f:
        f.write("\ncorrupt synthetic execution record\n")
    case["prompts"].clear()
    second, _ = call_producer(case)
    assert second["speaker_attribution_generation"]["status"] == "UNKNOWN"
    assert "voiceprint_anchor" not in case["prompts"][0]
    assert len(list((case["out"] / "speaker-attribution").glob("*/execution.json"))) == 1


@pytest.mark.parametrize("damage", ["decoded_duration", "decoded_bytes"])
def test_cold_decoded_source_artifact_and_duration_are_revalidated(case, damage):
    first, _ = call_producer(case)
    raw = Path(first["speaker_attribution_generation"]["attribution_path"])
    directory = raw.parent
    if damage == "decoded_duration":
        doc = json.loads(raw.read_text())
        doc["source_binding"]["geometry"]["audio_duration_ms"] += 1000
        raw.write_text(json.dumps(doc))
        terminal = json.loads((directory / "terminal.json").read_text())
        terminal["raw_sha256"] = sha(raw)
        (directory / "terminal.json").write_text(json.dumps(terminal))
    else:
        with (directory / "work/clip-16k-mono.wav").open("ab") as stream:
            stream.write(b"decoded source drift")
    case["prompts"].clear()
    second, _ = call_producer(case)
    assert second["speaker_attribution_generation"]["status"] == "UNKNOWN"
    assert "voiceprint_anchor" not in case["prompts"][0]
    assert len(list((case["out"] / "speaker-attribution").glob("*/execution.json"))) == 1
