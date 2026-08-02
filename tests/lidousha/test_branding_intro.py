import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scripts.run_auto_review_shadow_pipeline as shadow_pipeline
from src.autoslice.branding_intro import (
    BRANDING_INTRO_ENV_SWITCH,
    BRANDING_INTRO_PICK_ENV,
    BRANDING_INTRO_SCHEMA,
    BRANDING_INTRO_SCHEMA_V2,
    BrandingIntroError,
    load_branding_intro_policy,
    policy_intros,
    prepend_branding_intro,
    require_branding_intro,
    resolve_intro_media,
)


def _write_av(path: Path, *, seconds: float, size: str, fps: int, sample_rate: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", f"testsrc=size={size}:rate={fps}",
            "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate={sample_rate}",
            "-t", f"{seconds:.3f}",
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-ar", str(sample_rate), "-ac", "2",
            "-shortest", str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _duration_ms(path: Path) -> int:
    completed = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    return round(float(completed.stdout.strip()) * 1000)


def _write_policy(
    repo_root: Path,
    intro_media: Path,
    *,
    enabled: bool = True,
    sha256: str | None = None,
) -> Path:
    manifest_path = repo_root / "assets" / "lidousha" / "intro" / "branding_intro.v1.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": BRANDING_INTRO_SCHEMA,
                "intro_id": "test-intro-v1",
                "enabled": enabled,
                "video": {
                    "sha256": sha256 or _sha256(intro_media),
                    "duration_ms": max(1, _duration_ms(intro_media)),
                },
                "runtime_media_paths": [str(intro_media)],
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    return manifest_path


@pytest.fixture(scope="module")
def intro_media(tmp_path_factory) -> Path:
    return _write_av(
        tmp_path_factory.mktemp("intro") / "intro.mp4",
        seconds=1.0,
        size="192x108",
        fps=24,
        sample_rate=44100,
    )


@pytest.fixture(scope="module")
def intro_media_b(tmp_path_factory) -> Path:
    return _write_av(
        tmp_path_factory.mktemp("intro-b") / "intro-b.mp4",
        seconds=1.4,
        size="192x108",
        fps=24,
        sample_rate=44100,
    )


def _write_policy_v2(
    repo_root: Path,
    intro_a: Path,
    intro_b: Path,
    *,
    sha_b: str | None = None,
) -> Path:
    manifest_path = repo_root / "assets" / "lidousha" / "intro" / "branding_intro.v1.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": BRANDING_INTRO_SCHEMA_V2,
                "enabled": True,
                "rotation": {"mode": "main-sha256"},
                "intros": [
                    {
                        "intro_id": "rotation-a",
                        "video": {
                            "sha256": _sha256(intro_a),
                            "duration_ms": max(1, _duration_ms(intro_a)),
                        },
                        "runtime_media_paths": [str(intro_a)],
                    },
                    {
                        "intro_id": "rotation-b",
                        "video": {
                            "sha256": sha_b or _sha256(intro_b),
                            "duration_ms": max(1, _duration_ms(intro_b)),
                        },
                        "runtime_media_paths": [str(intro_b)],
                    },
                ],
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    return manifest_path


def test_policy_absent_and_disabled_mean_off(tmp_path, intro_media):
    assert require_branding_intro(tmp_path) is None
    _write_policy(tmp_path, intro_media, enabled=False)
    assert require_branding_intro(tmp_path) is None


def test_policy_invalid_fails_closed(tmp_path, intro_media):
    manifest_path = _write_policy(tmp_path, intro_media)
    broken = json.loads(manifest_path.read_text(encoding="utf-8"))
    broken["schema_version"] = "something-else"
    manifest_path.write_text(json.dumps(broken), encoding="utf-8")
    with pytest.raises(BrandingIntroError):
        require_branding_intro(tmp_path)
    broken["schema_version"] = BRANDING_INTRO_SCHEMA
    del broken["video"]
    manifest_path.write_text(json.dumps(broken), encoding="utf-8")
    with pytest.raises(BrandingIntroError):
        require_branding_intro(tmp_path)


def test_env_switch(tmp_path, intro_media, monkeypatch):
    _write_policy(tmp_path, intro_media)
    monkeypatch.setenv(BRANDING_INTRO_ENV_SWITCH, "off")
    assert require_branding_intro(tmp_path) is None
    monkeypatch.setenv(BRANDING_INTRO_ENV_SWITCH, "offf")
    with pytest.raises(BrandingIntroError):
        require_branding_intro(tmp_path)
    monkeypatch.setenv(BRANDING_INTRO_ENV_SWITCH, "")
    context = require_branding_intro(tmp_path)
    assert context is not None and context["intro_id"] == "test-intro-v1"


def test_media_hash_gate(tmp_path, intro_media):
    manifest_path = _write_policy(tmp_path, intro_media, sha256="0" * 64)
    policy = load_branding_intro_policy(manifest_path)
    with pytest.raises(BrandingIntroError, match="sha256 drift"):
        resolve_intro_media(policy, tmp_path)
    with pytest.raises(BrandingIntroError, match="must not proceed"):
        require_branding_intro(tmp_path)
    _write_policy(tmp_path, intro_media)
    context = require_branding_intro(tmp_path)
    assert Path(str(context["media_path"])) == intro_media


def test_prepend_matches_main_contract_and_extends_duration(tmp_path, intro_media):
    _write_policy(tmp_path, intro_media)
    context = require_branding_intro(tmp_path)
    main = _write_av(
        tmp_path / "main.mp4", seconds=2.0, size="256x144", fps=30, sample_rate=48000
    )
    main_duration = _duration_ms(main)
    main_sha_before = _sha256(main)
    binding = prepend_branding_intro(
        context=context, main_path=main, work_dir=tmp_path / "intro-work"
    )
    assert binding["status"] == "PREPENDED"
    assert binding["main_sha256_before"] == main_sha_before
    assert binding["intro_offset_ms"] > 0
    assert binding["verification"]["full_decode"] == "clean"
    contract = binding["verification"]["stream_contract"]
    assert (contract["width"], contract["height"]) == (256, 144)
    assert contract["fps"] == "30/1"
    assert contract["sample_rate"] == 48000
    assert _sha256(main) != main_sha_before
    grown_ms = _duration_ms(main) - main_duration
    assert abs(grown_ms - binding["intro_offset_ms"]) <= 250
    assert not (tmp_path / "intro-work").exists()


def test_prepend_rejects_drifted_intro(tmp_path, intro_media):
    _write_policy(tmp_path, intro_media)
    context = dict(require_branding_intro(tmp_path))
    context["media_sha256"] = "1" * 64
    main = _write_av(tmp_path / "main.mp4", seconds=1.0, size="128x128", fps=30, sample_rate=48000)
    with pytest.raises(BrandingIntroError, match="drifted"):
        prepend_branding_intro(context=context, main_path=main, work_dir=tmp_path / "w")


def _materialized_record(tmp_path: Path) -> dict:
    media = _write_av(tmp_path / "clip.recut.mp4", seconds=2.0, size="128x128", fps=30, sample_rate=48000)
    srt = tmp_path / "clip.recut.srt"
    srt.write_text("1\n00:00:00,200 --> 00:00:01,200\n测试字幕\n", encoding="utf-8")
    return {
        "status": "MATERIALIZED",
        "media_path": str(media),
        "subtitle_path": str(srt),
    }


def test_burn_applies_branding_intro_before_hashing(tmp_path, intro_media):
    _write_policy(tmp_path, intro_media)
    context = require_branding_intro(tmp_path)
    record = _materialized_record(tmp_path)
    main_duration = _duration_ms(Path(record["media_path"]))
    burned = shadow_pipeline._burn_preview_subtitles(
        dict(record), run_ffmpeg=True, branding_intro=context
    )
    preview = burned["burned_preview"]
    assert preview["status"] == "BURNED"
    assert preview["branding_intro"]["status"] == "PREPENDED"
    burned_path = Path(preview["path"])
    assert preview["burned_sha256"] == "sha256:" + _sha256(burned_path)
    assert burned["artifact_hashes"]["burned_video_sha256"] == preview["burned_sha256"]
    assert _duration_ms(burned_path) >= main_duration + 500


def test_burn_fails_closed_when_intro_unusable(tmp_path, intro_media):
    _write_policy(tmp_path, intro_media)
    context = dict(require_branding_intro(tmp_path))
    context["media_sha256"] = "2" * 64
    burned = shadow_pipeline._burn_preview_subtitles(
        _materialized_record(tmp_path), run_ffmpeg=True, branding_intro=context
    )
    preview = burned["burned_preview"]
    assert preview["status"] == "FAILED"
    assert preview["reason_code"] == "BRANDING_INTRO_FAILED"
    assert "drifted" in preview["error"]


def test_burn_dry_run_annotates_skip(tmp_path, intro_media):
    _write_policy(tmp_path, intro_media)
    context = require_branding_intro(tmp_path)
    record = _materialized_record(tmp_path)
    burned = shadow_pipeline._burn_preview_subtitles(
        dict(record), run_ffmpeg=False, branding_intro=context
    )
    assert burned["burned_preview"]["status"] == "DRY_RUN"
    assert burned["burned_preview"]["branding_intro"]["status"] == "DRY_RUN_SKIPPED"


def test_burn_without_branding_keeps_legacy_shape(tmp_path):
    record = _materialized_record(tmp_path)
    burned = shadow_pipeline._burn_preview_subtitles(dict(record), run_ffmpeg=True)
    preview = burned["burned_preview"]
    assert preview["status"] == "BURNED"
    assert preview["branding_intro"] is None


def test_rotation_context_resolves_all_candidates(tmp_path, intro_media, intro_media_b):
    _write_policy_v2(tmp_path, intro_media, intro_media_b)
    context = require_branding_intro(tmp_path)
    assert [c["intro_id"] for c in context["candidates"]] == ["rotation-a", "rotation-b"]
    assert context["rotation_mode"] == "main-sha256"
    # Legacy top-level fields keep pointing at the first roster member.
    assert Path(str(context["media_path"])) == intro_media


def test_rotation_drifted_member_fails_closed(tmp_path, intro_media, intro_media_b):
    _write_policy_v2(tmp_path, intro_media, intro_media_b, sha_b="3" * 64)
    with pytest.raises(BrandingIntroError, match="must not proceed"):
        require_branding_intro(tmp_path)


def test_rotation_pick_is_content_keyed_and_stable(tmp_path, intro_media, intro_media_b):
    _write_policy_v2(tmp_path, intro_media, intro_media_b)
    context = require_branding_intro(tmp_path)
    main = _write_av(tmp_path / "main.mp4", seconds=2.0, size="256x144", fps=30, sample_rate=48000)
    main_bytes = main.read_bytes()
    main_sha = _sha256(main)
    expected_id = ["rotation-a", "rotation-b"][int(main_sha[:16], 16) % 2]
    binding = prepend_branding_intro(context=context, main_path=main, work_dir=tmp_path / "w1")
    assert binding["status"] == "PREPENDED"
    assert binding["intro_id"] == expected_id
    assert binding["rotation"]["picked_intro_id"] == expected_id
    assert binding["rotation"]["selector"] == "main_sha256"
    assert binding["rotation"]["candidate_intro_ids"] == ["rotation-a", "rotation-b"]
    # Re-burn of the same material picks the same intro (content-keyed).
    main2 = tmp_path / "main2.mp4"
    main2.write_bytes(main_bytes)
    binding2 = prepend_branding_intro(context=context, main_path=main2, work_dir=tmp_path / "w2")
    assert binding2["intro_id"] == expected_id


def test_rotation_env_override(tmp_path, intro_media, intro_media_b, monkeypatch):
    _write_policy_v2(tmp_path, intro_media, intro_media_b)
    context = require_branding_intro(tmp_path)
    main = _write_av(tmp_path / "main.mp4", seconds=1.0, size="128x128", fps=30, sample_rate=48000)
    monkeypatch.setenv(BRANDING_INTRO_PICK_ENV, "rotation-b")
    binding = prepend_branding_intro(context=context, main_path=main, work_dir=tmp_path / "w")
    assert binding["intro_id"] == "rotation-b"
    assert binding["rotation"]["selector"] == "env_override"
    monkeypatch.setenv(BRANDING_INTRO_PICK_ENV, "no-such-intro")
    main2 = _write_av(tmp_path / "main2.mp4", seconds=1.0, size="128x128", fps=30, sample_rate=48000)
    with pytest.raises(BrandingIntroError, match="names no manifest intro"):
        prepend_branding_intro(context=context, main_path=main2, work_dir=tmp_path / "w2")


def test_repo_manifest_binds_installed_intro_bytes():
    manifest_path = ROOT / "assets" / "lidousha" / "intro" / "branding_intro.v1.json"
    policy = load_branding_intro_policy(manifest_path)
    assert policy is not None and policy["enabled"] is True
    intros = policy_intros(policy)
    assert [entry["intro_id"] for entry in intros] == [
        "huozi-lidousha-shiling-budui-weiaizuoyi-z1-v2",
        "huozi-lidousha-kmx-baobao-guankou-z2-v1",
    ]
    assert policy["rotation"]["mode"] == "main-sha256"

    z1 = intros[0]
    z1_sha = str(z1["video"]["sha256"])
    z1_render_path = ROOT / "assets" / "lidousha" / "intro" / "lidousha-branding-intro.v1.render-manifest.json"
    z1_render = json.loads(z1_render_path.read_text(encoding="utf-8"))
    assert z1_render["status"] == "REVIEW_READY_NO_UPLOAD"
    assert z1_render["artifacts"]["video"]["sha256"] == z1_sha
    assert z1_render["target"] == z1["target_text"]
    assert z1["provenance"]["render_manifest_sha256"] == _sha256(z1_render_path)
    assert z1["provenance"]["verified_plan_sha256"] == z1_render["verified_plan_sha256"]

    z2 = intros[1]
    z2_sha = str(z2["video"]["sha256"])
    z2_render_path = ROOT / "assets" / "lidousha" / "intro" / "lidousha-branding-intro.z2-kmx-baobao-guankou-20260719.render-manifest.json"
    z2_render = json.loads(z2_render_path.read_text(encoding="utf-8"))
    assert z2_render["status"] == "ADOPTED_IN_ROTATION"
    assert z2_render["artifacts"]["video"]["sha256"] == z2_sha
    assert z2_render["target_text"] == z2["target_text"]
    assert z2["provenance"]["render_manifest_sha256"] == _sha256(z2_render_path)
    z2_srt_path = ROOT / "assets" / "lidousha" / "intro" / "lidousha-branding-intro.z2-kmx-baobao-guankou-20260719.srt"
    assert _sha256(z2_srt_path) == z2["subtitle_srt"]["sha256"]

    # The media itself stays outside git; when a runtime copy is present it
    # must match the committed binding exactly.
    for entry, expected_sha in ((z1, z1_sha), (z2, z2_sha)):
        for raw in entry["runtime_media_paths"]:
            candidate = Path(str(raw))
            if not candidate.is_absolute():
                candidate = ROOT / candidate
            if candidate.is_file():
                assert _sha256(candidate) == expected_sha
