import argparse
import hashlib
import json
import runpy
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.autoslice import branding_intro as bi


A = {"recording_date": "2026-11-06", "candidate_id": "semantictalk_686120_762640"}
B = {"recording_date": "2026-11-04", "candidate_id": "semantictalk_632500_775610"}


def _entry(root, name, clip=None):
    media = root / f"{name}.mp4"
    media.write_bytes(name.encode())
    entry = {"intro_id": name, "video": {"sha256": hashlib.sha256(media.read_bytes()).hexdigest(),
                                          "duration_ms": 1000}, "runtime_media_paths": [str(media)]}
    if clip is not None:
        entry["one_shot"] = {"clips": [clip]}
    return entry


def _write(path, intros):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema_version": bi.BRANDING_INTRO_SCHEMA_V2, "enabled": True,
                               "rotation": {"mode": "main-sha256", "launch_boost_days": 3,
                                            "launch_weight": 4}, "intros": intros}))


@pytest.fixture
def roster(tmp_path, monkeypatch):
    monkeypatch.setattr(bi, "_utc_now", lambda: datetime(2026, 11, 8, 20, tzinfo=timezone.utc))
    monkeypatch.delenv(bi.BRANDING_INTRO_PICK_ENV, raising=False)
    entries = [_entry(tmp_path, f"z{i}") for i in range(1, 5)]
    entries[3]["introduced_at"] = "2026-11-08T18:25:10Z"
    manifest = tmp_path / bi.BRANDING_INTRO_MANIFEST_RELPATH
    _write(manifest, entries)
    old = bi.require_branding_intro(tmp_path, manifest_path=manifest)
    entries.extend([_entry(tmp_path, "A", A), _entry(tmp_path, "B", B)])
    _write(manifest, entries)
    return tmp_path, manifest, entries, old


def test_exact_binding_and_rotation_preserved(roster, monkeypatch):
    root, manifest, entries, old = roster
    for clip, name in ((A, "A"), (B, "B")):
        ctx = bi.require_branding_intro(root, manifest_path=manifest, clip_identity=clip)
        picked, receipt = bi._pick_intro(ctx, "0" * 64)
        assert picked["intro_id"] == name
        assert receipt["selector"] == "clip_identity" and receipt["clip_identity"] == clip
        assert {p.name for p in ctx["required_media_paths"]} == {f"z{i}.mp4" for i in range(1, 5)} | {f"{name}.mp4"}
    other = dict(A, recording_date="2026-11-07")
    ctx = bi.require_branding_intro(root, manifest_path=manifest, clip_identity=other)
    assert ctx["rotation_effective_weights"] == old["rotation_effective_weights"] == {
        "z1": 1, "z2": 1, "z3": 1, "z4": 4}
    for bucket in range(128):
        digest = f"{bucket:016x}" + "0" * 48
        assert bi._pick_intro(ctx, digest)[0]["intro_id"] == bi._pick_intro(old, digest)[0]["intro_id"]
    monkeypatch.setenv(bi.BRANDING_INTRO_PICK_ENV, "A")
    with pytest.raises(bi.BrandingIntroError, match="names no manifest intro"):
        bi._pick_intro(ctx, "0" * 64)
    bound = bi.require_branding_intro(root, manifest_path=manifest, clip_identity=A)
    assert bi._pick_intro(bound, "0" * 64)[0]["intro_id"] == "A"


def test_missing_and_drift_only_block_bound_clip(roster):
    root, manifest, entries, _ = roster
    path = Path(entries[4]["runtime_media_paths"][0])
    path.unlink()
    with pytest.raises(bi.BrandingIntroError, match="unavailable"):
        bi.require_branding_intro(root, manifest_path=manifest, clip_identity=A)
    assert bi.require_branding_intro(root, manifest_path=manifest, clip_identity=B)["intro_id"] == "B"
    assert bi.require_branding_intro(root, manifest_path=manifest, clip_identity=dict(A, recording_date="2026-11-05"))
    path.write_bytes(b"wrong")
    with pytest.raises(bi.BrandingIntroError, match="sha256 drift"):
        bi.require_branding_intro(root, manifest_path=manifest, clip_identity=A)


def test_missing_identity_fails_closed_but_recorded_one_shot_can_be_pinned(roster):
    root, manifest, entries, _ = roster
    context = bi.require_branding_intro(root, manifest_path=manifest)
    with pytest.raises(bi.BrandingIntroError, match="needs clip identity"):
        bi._pick_intro(context, "0" * 64)
    binding = {"status": "PREPENDED", "intro_id": "A", "intro_media_sha256": entries[4]["video"]["sha256"],
               "intro_offset_ms": 1000}
    pinned = bi.pin_existing_delivery_intro(context, binding)
    assert bi._pick_intro(pinned, "0" * 64)[0]["intro_id"] == "A"
    bound = bi.require_branding_intro(root, manifest_path=manifest, clip_identity=A)
    previous = {"status": "PREPENDED", "intro_id": "z1", "intro_media_sha256": entries[0]["video"]["sha256"],
                "intro_offset_ms": 1000}
    assert bi._pick_intro(bi.pin_existing_delivery_intro(bound, previous), "0" * 64)[0]["intro_id"] == "z1"
    Path(entries[4]["runtime_media_paths"][0]).write_bytes(b"corrupted")
    with pytest.raises(bi.BrandingIntroError, match="sha256 drift"):
        bi.pin_existing_delivery_intro(context, binding)


def test_ambiguous_or_invalid_one_shot_binding_is_rejected(roster):
    root, manifest, entries, _ = roster
    entries[5]["one_shot"]["clips"] = [A]
    _write(manifest, entries)
    with pytest.raises(bi.BrandingIntroError, match="duplicate one-shot"):
        bi.load_branding_intro_policy(manifest)
    entries[5]["one_shot"]["clips"] = [{"recording_date": "2026-02-30", "candidate_id": "x"}]
    _write(manifest, entries)
    with pytest.raises(bi.BrandingIntroError, match="date is invalid"):
        bi.load_branding_intro_policy(manifest)


def test_manual_remote_helper_resolves_the_real_clip_identity(roster, monkeypatch, tmp_path):
    root, _, _, _ = roster
    source = tmp_path / "main.mp4"
    source.write_bytes(b"main")
    result = tmp_path / "result.json"
    output = tmp_path / "out.mp4"
    helper = Path(__file__).resolve().parents[2] / "assets/manual_lane/remote/prepend_intro.py"
    monkeypatch.setenv("MANUAL_LANE_REMOTE_REPO", str(root))
    monkeypatch.setenv("MANUAL_LANE_INTRO_DIR", str(tmp_path / "no-local-copy"))
    monkeypatch.setattr(sys, "argv", [str(helper), str(source), str(output), str(tmp_path / "work"),
                                     str(result), B["recording_date"], B["candidate_id"]])
    def fake_prepend(*, context, main_path, work_dir):
        picked, receipt = bi._pick_intro(context, "0" * 64)
        assert picked["intro_id"] == "B" and receipt["clip_identity"] == B
        return {"status": "PREPENDED", "intro_id": picked["intro_id"], "intro_offset_ms": 1000}
    monkeypatch.setattr(bi, "prepend_branding_intro", fake_prepend)
    runpy.run_path(str(helper), run_name="__main__")
    assert json.loads(result.read_text())["intro_id"] == "B"


def test_normal_producer_passes_the_spec_identity(tmp_path, monkeypatch):
    from src.autoslice import producer_request
    from src.autoslice.channel_profile import binding_for_profile, load_channel_profile

    root = Path(__file__).resolve().parents[2]
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps({"candidate_id": A["candidate_id"], "date": A["recording_date"],
                                "human_truth_mode": "delivery", "output_root": str(tmp_path / "out"),
                                "pieces": []}))
    args = argparse.Namespace(spec=spec, subtitle_text_overrides=None, subtitle_regression=None,
                              speaker_overrides=None, ssh_host="localhost")
    class Captured(Exception):
        pass
    def capture(*args, **kwargs):
        assert kwargs["clip_identity"] == A
        raise Captured
    monkeypatch.setattr(producer_request, "require_branding_intro", capture)
    monkeypatch.setenv("AUTOSLICE_HUMAN_TRUTH_MODE", "delivery")
    with pytest.raises(Captured):
        producer_request.load_producer_request(
            args, repo_root=root, profile_asset_file=lambda _: tmp_path / "intro.json",
            trusted_parent_profile_binding=binding_for_profile(load_channel_profile(root, environ={})))


def test_overflow_stages_rotation_and_the_bound_one_shot(roster, monkeypatch):
    from types import SimpleNamespace
    from src.autoslice import channel_profile, execution_backend

    root, manifest, _, _ = roster
    repo = Path(__file__).resolve().parents[2]
    source = root / "source.flv"
    source.write_bytes(b"source")
    monkeypatch.setattr(execution_backend, "active_repository", lambda _: repo)
    monkeypatch.setattr(channel_profile, "load_channel_profile", lambda **_: SimpleNamespace(
        asset_file=lambda *args, **kwargs: manifest))
    paths = execution_backend._source_inputs(root, A["recording_date"],
                                              {"cid": A["candidate_id"], "segment_path": str(source)})
    assert {path.name for path in paths} == {"source.flv", "z1.mp4", "z2.mp4", "z3.mp4", "z4.mp4", "A.mp4"}
    with pytest.raises(execution_backend.runtime_storage.CapacityBudgetError, match="needs clip identity"):
        execution_backend._source_inputs(root, A["recording_date"], {"segment_path": str(source)})


@pytest.mark.parametrize("clip,expected", [(A, "A"), (B, "B"),
                                          (dict(A, recording_date="2026-11-07"), None)])
def test_real_prepend_uses_only_the_selected_intro(roster, clip, expected):
    root, manifest, entries, _ = roster
    source = root / "synthetic.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=size=160x90:rate=30",
                    "-f", "lavfi", "-i", "sine=sample_rate=48000", "-t", "0.5", "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-ac", "2", str(source)], check=True)
    data = source.read_bytes()
    for entry in entries:
        Path(entry["runtime_media_paths"][0]).write_bytes(data)
        entry["video"]["sha256"] = hashlib.sha256(data).hexdigest()
    _write(manifest, entries)
    context = bi.require_branding_intro(root, manifest_path=manifest, clip_identity=clip)
    receipt = bi.prepend_branding_intro(context=context, main_path=source, work_dir=root / "render")
    assert receipt["status"] == "PREPENDED"
    if expected is None:
        assert receipt["intro_id"] in {"z1", "z2", "z3", "z4"}
    else:
        assert receipt["intro_id"] == expected
