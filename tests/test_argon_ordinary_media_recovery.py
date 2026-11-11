"""Normal narrow acquisition -> saved manifest -> fresh Argon history reader."""
from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

from src.autoslice.acoustic_witness_adjudication import build_witness_request
from src.autoslice.argon_escalation_contract import canonical, sha
from src.autoslice.argon_escalation_integration import ArgonIntegration, read_ordinary_history
from test_targeted_relisten_public_contract import SRT, acquire_narrow, build_offline_rig


def setup_acquisition(tmp_path, monkeypatch, offset=0, provider="agy", **kwargs):
    rig = build_offline_rig(tmp_path, monkeypatch, offset_ms=offset, **kwargs)
    rig.duration = kwargs.get("source_duration_ms", 10000)
    rig.local = replace(rig.local, model="Gemini 3.8 Flash (High)")
    rig.verifier = rig.local
    if provider == "gemini_api":
        from src.autoslice import entity_audio_verifier as audio
        monkeypatch.setenv(audio.ENTITY_AUDIO_API_MODEL_ENV, "gemini-3.8-flash")
        monkeypatch.setenv(audio.ENTITY_AUDIO_DISABLE_AGY_ENV, "1")
        monkeypatch.setattr(audio.agy_gemini_client, "local_agy_available", lambda _: False)
        def api_reply(**call):
            # Only the provider boundary is synthetic; crop/transcode, prompts,
            # raw persistence and manifest generation remain the normal writer.
            rig.transport.calls.append({"provider": "gemini_api", "offline": True})
            raw = {"schema_version": "subtitle-span-acoustic-witness.v1", "status": "OBSERVED",
                "target_audible": True, "heard_pinyin": rig.transport.pinyin,
                "uncertain_positions": [4, 5], "syllable_count": 6,
                "confidence": .72, "reason": "synthetic API hearing"}
            call["response_path"].write_bytes(canonical(raw))
            return SimpleNamespace(observed=raw, accepted_key_tier=None,
                accepted_key_ordinal=None, configured_key_count=0,
                paid_policy_stamp=None, paid_gate_reason=None)
        monkeypatch.setattr(audio, "_run_gemini_api_fallback", api_reply)
    # Retained runtime search is confined to this fixture, including its source.
    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path))
    witness = acquire_narrow(rig)
    return rig, witness


def session_for(rig):
    owner = ArgonIntegration(source_media=rig.source, source_sha256=sha(rig.source.read_bytes()),
        source_duration_ms=rig.duration, ordinary_output_dir=rig.output,
        runtime=SimpleNamespace(base=rig.tmp), ordinary_verifier=rig.local)
    return owner.session(rig.check, rig.finding)


@pytest.mark.parametrize("route", ["direct", "cache_replay"])
@pytest.mark.parametrize("recovery", ["copy", "rebuild"])
@pytest.mark.parametrize("offset", [0, 500])
def test_normal_writer_saved_input_reaches_fresh_argon_consumer(tmp_path, monkeypatch, route, recovery, offset):
    rig, witness = setup_acquisition(tmp_path, monkeypatch, offset)
    assert_cold_collection(rig, witness, route, recovery)


def assert_cold_collection(rig, witness, route, recovery):
    tmp_path = rig.tmp
    original = Path(witness["relisten_artifacts"]["audio_path"])
    expected = original.read_bytes()
    immutable = {p: p.read_bytes() for p in rig.output.rglob("*") if p.is_file() and p != original}
    if recovery == "copy":
        retained = tmp_path / "retained" / original.name
        retained.parent.mkdir()
        retained.write_bytes(expected)
    original.unlink()
    if route == "cache_replay":
        witness = rig.local.probe_witness_cache(build_witness_request(rig.check))
        assert witness["witness_cache_replay"]["provider_call_count"] == 0
    payload = tmp_path / "saved.json"
    payload.write_bytes(canonical({"check": rig.check, "finding": rig.finding, "witness": witness,
        "source": str(rig.source), "duration": rig.duration,
        "recording_date": rig.local.recording_date, "model": rig.local.model,
        "output": str(rig.output), "base": str(tmp_path)}))
    # Start before Argon has collected history: this interpreter must reopen the
    # ordinary writer's saved originals, not inherit a warm recovery memo.
    consumer = tmp_path / "cold_consumer.py"
    consumer.write_text('''import json, socket, sys
from pathlib import Path
from types import SimpleNamespace
from src.autoslice import entity_audio_verifier as audio
from src.autoslice.argon_escalation_contract import sha, canonical
from src.autoslice.argon_escalation_integration import ArgonIntegration, read_ordinary_history
def forbidden(*a, **k):
    raise AssertionError("cold history must not dispatch")
socket.socket.connect = forbidden
p = json.loads(Path(sys.argv[1]).read_bytes())
source = Path(p["source"])
verifier = audio._LocalAudioVerifier(source_media=source, output_dir=Path(p["output"]),
    recording_date=p["recording_date"], source_duration_ms=p["duration"],
    source_sha256=sha(source.read_bytes()), source_stat_binding=audio._source_stat_binding(source),
    binary="agy", model=p["model"], timeout="1s", timely_context="")
owner = ArgonIntegration(source_media=source, source_sha256=sha(source.read_bytes()),
    source_duration_ms=p["duration"], ordinary_output_dir=Path(p["output"]),
    runtime=SimpleNamespace(base=Path(p["base"])), ordinary_verifier=verifier)
session = owner.session(p["check"], p["finding"])
assert session.consider(p["check"], p["witness"], None, None) is False
rows = read_ordinary_history(session.root, session.spec)
assert len(rows) == 1 and rows[0][1] == "OBSERVED"
print(json.dumps({"history_count": len(rows), "event_sha256": rows[0][3]["event_sha256"]}))
''')
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]), PYTHONDONTWRITEBYTECODE="1")
    result = subprocess.run([sys.executable, str(consumer), str(payload)], env=env,
        text=True, capture_output=True, timeout=45)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["history_count"] == 1
    assert not original.exists() and len(rig.transport.calls) == 1
    assert all(p.read_bytes() == raw for p, raw in immutable.items())


def test_holdout_new_source_and_unseen_physical_window(tmp_path, monkeypatch):
    # Added after the patch: a new upstream source, cue window and offset,
    # with no per-member registration, copied recipe or prepared history.
    srt = SRT.replace("00:00:02,000 --> 00:00:04,000", "00:00:02,300 --> 00:00:03,700")
    rig, witness = setup_acquisition(tmp_path, monkeypatch, 900,
        srt=srt, source_duration_ms=12000)
    assert_cold_collection(rig, witness, "cache_replay", "rebuild")


@pytest.mark.parametrize("route", ["direct", "cache_replay"])
def test_api_writer_uses_the_same_missing_media_contract(tmp_path, monkeypatch, route):
    rig, witness = setup_acquisition(tmp_path, monkeypatch, provider="gemini_api")
    assert witness["provider"] == "gemini_api"
    assert_cold_collection(rig, witness, route, "rebuild")


@pytest.mark.parametrize("damage", ["absent_source", "source_sha", "audio_sha", "audio_bytes",
    "symlink", "path_substitution", "clock", "source_binding", "raw_unknown", "raw_incomplete"])
def test_recovery_does_not_admit_unbound_or_incomplete_hearings(tmp_path, monkeypatch, damage):
    rig, witness = setup_acquisition(tmp_path, monkeypatch)
    session = session_for(rig)
    audio = Path(witness["relisten_artifacts"]["audio_path"])
    manifest_path = audio.parent / "verdict.manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    retained = tmp_path / "retained" / audio.name
    retained.parent.mkdir()
    if damage not in {"absent_source", "source_sha", "audio_sha"}:
        shutil.copyfile(audio, retained)
    audio.unlink()
    if damage == "absent_source":
        rig.source.unlink()
    elif damage == "source_sha":
        rig.source.write_bytes(b"changed source")
    elif damage == "audio_sha":
        manifest["audio_clip_sha256"] = "0" * 64
    elif damage == "audio_bytes":
        audio.write_bytes(b"existing changed audio")
    elif damage == "symlink":
        audio.symlink_to(retained)
    elif damage == "path_substitution":
        manifest["audio_clip"] = str(retained)
    elif damage == "clock":
        manifest["timeline_binding"]["source_media"]["target_start_ms"] += 100
    elif damage == "source_binding":
        manifest["source_media_sha256"] = "f" * 64
    else:
        response = next(audio.parent.glob("verdict.raw*"))
        raw = {"schema_version": "subtitle-span-acoustic-witness.v1",
               "status": "UNKNOWN" if damage == "raw_unknown" else "OBSERVED"}
        response.write_bytes(canonical(raw))
        manifest["response_sha256"] = sha(response.read_bytes())
    manifest_path.write_bytes(canonical(manifest))
    try:
        assert session.consider(rig.check, witness, None, None) is False
    except (ValueError, OSError):
        pass
    assert not read_ordinary_history(session.root, session.spec)
    assert len(rig.transport.calls) == 1


def test_present_valid_ordinary_input_keeps_existing_history(tmp_path, monkeypatch):
    rig, witness = setup_acquisition(tmp_path, monkeypatch)
    session = session_for(rig)
    assert session.consider(rig.check, witness, None, None) is False
    assert len(read_ordinary_history(session.root, session.spec)) == 1
    assert len(rig.transport.calls) == 1
