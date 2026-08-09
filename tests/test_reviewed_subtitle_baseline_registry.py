import hashlib
import json
from pathlib import Path

import pytest

from src.autoslice.reviewed_subtitle_baseline_registry import (
    ReviewedSubtitleBaselineRegistryError,
    load_candidate_reviewed_subtitle_baseline,
)
from src.autoslice.jingting_chunker import parse_srt_cues
from src.autoslice.redelivery_boundary_projection import (
    PROJECTION_MODE,
    PROJECTION_MODE_CONFIG_KEY,
)
from src.autoslice.subtitle_validation import validate_srt_file


def _write_asset(root: Path, candidate_id: str = "auto_1_2_3") -> tuple[Path, Path]:
    root.mkdir(parents=True, exist_ok=True)
    baseline = root / f"{candidate_id}.reviewed.srt"
    baseline.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n已审文本\n",
        encoding="utf-8",
    )
    manifest = root / f"{candidate_id}.subtitle-baseline.v1.json"
    manifest.write_text(
        json.dumps(
            {
                "registry_schema_version": "candidate-reviewed-subtitle-baseline.v1",
                "candidate_id": candidate_id,
                "schema_version": "subtitle-redelivery-baseline.v2",
                "mode": "preserve_text_outside_source_truth",
                "exact_interval_replay": True,
                "path": baseline.name,
                "sha256": hashlib.sha256(baseline.read_bytes()).hexdigest(),
                "authority": "Ivan reviewed delivery",
                "source_recording_basename": "recording.mp4",
                "source_sha256": "a" * 64,
                "absolute_source_start_ms": 10_000,
                "absolute_source_end_ms": 11_000,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return manifest, baseline


def test_absent_candidate_baseline_is_optional(tmp_path):
    assert load_candidate_reviewed_subtitle_baseline(tmp_path, "auto_1_2_3") is None


def test_loads_hash_bound_v2_and_resolves_only_sibling_path(tmp_path):
    manifest, baseline = _write_asset(tmp_path)

    loaded = load_candidate_reviewed_subtitle_baseline(tmp_path, "auto_1_2_3")

    assert loaded is not None
    assert loaded.manifest_path == manifest.resolve()
    assert loaded.baseline_path == baseline.resolve()
    assert loaded.config["path"] == str(baseline.resolve())
    assert loaded.config["schema_version"] == "subtitle-redelivery-baseline.v2"
    assert loaded.config["exact_interval_replay"] is True
    assert "registry_schema_version" not in loaded.config
    assert "candidate_id" not in loaded.config
    assert loaded.fingerprint_paths == (manifest.resolve(), baseline.resolve())


def test_loads_only_the_supported_opt_in_terminal_projection_mode(tmp_path):
    manifest, _baseline = _write_asset(tmp_path)
    document = json.loads(manifest.read_text(encoding="utf-8"))
    document[PROJECTION_MODE_CONFIG_KEY] = PROJECTION_MODE
    manifest.write_text(json.dumps(document), encoding="utf-8")

    loaded = load_candidate_reviewed_subtitle_baseline(
        tmp_path, "auto_1_2_3"
    )

    assert loaded is not None
    assert loaded.config[PROJECTION_MODE_CONFIG_KEY] == PROJECTION_MODE


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda doc: doc.update(candidate_id="auto_other"), "id does not match"),
        (lambda doc: doc.update(path="../outside.srt"), "sibling filename"),
        (lambda doc: doc.update(sha256="0" * 64), "sha256 does not match"),
        (lambda doc: doc.update(authority=""), "authority is required"),
        (
            lambda doc: doc.update(source_recording_basename="../recording.mp4"),
            "must not contain a path",
        ),
        (lambda doc: doc.update(source_sha256="bad"), "source sha256 is invalid"),
        (
            lambda doc: doc.update(exact_interval_replay="yes"),
            "replay flag must be boolean",
        ),
        (
            lambda doc: doc.update(terminal_projection_mode="trust_me"),
            "projection mode is unsupported",
        ),
        (
            lambda doc: doc.update(
                terminal_projection_mode=PROJECTION_MODE,
                exact_interval_replay=False,
            ),
            "projection requires exact interval replay",
        ),
        (
            lambda doc: doc.update(absolute_source_end_ms=10_000),
            "source interval is invalid",
        ),
    ],
)
def test_present_invalid_manifest_fails_closed(tmp_path, mutate, message):
    manifest, _baseline = _write_asset(tmp_path)
    document = json.loads(manifest.read_text(encoding="utf-8"))
    mutate(document)
    manifest.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ReviewedSubtitleBaselineRegistryError, match=message):
        load_candidate_reviewed_subtitle_baseline(tmp_path, "auto_1_2_3")


def test_symlinked_baseline_fails_closed(tmp_path):
    manifest, baseline = _write_asset(tmp_path)
    target = tmp_path / "real.srt"
    baseline.rename(target)
    baseline.symlink_to(target)

    with pytest.raises(ReviewedSubtitleBaselineRegistryError, match="non-symlink"):
        load_candidate_reviewed_subtitle_baseline(tmp_path, "auto_1_2_3")


def test_committed_nancho_baseline_binds_new_truths_to_absolute_source_timeline():
    root = (
        Path(__file__).resolve().parents[1]
        / "assets"
        / "lidousha"
        / "reviewed_subtitle_baselines"
    )
    loaded = load_candidate_reviewed_subtitle_baseline(
        root,
        "auto_193450_672_945",
    )

    assert loaded is not None
    assert loaded.config["source_recording_basename"] == (
        "22966160_20260722-19-34-50.mp4"
    )
    assert loaded.config["source_sha256"] == (
        "0eb2778dc53e5eabbccae089e5db92d3fb3662d90e1dd2ddbe7765436718989a"
    )
    assert loaded.config["absolute_source_start_ms"] == 672_670
    cues = parse_srt_cues(loaded.baseline_path.read_text(encoding="utf-8"))
    by_text = {cue.text: cue for cue in cues}

    residence = by_text["确实要，要小心小N老师啊，"]
    assert (
        loaded.config["absolute_source_start_ms"] + residence.start_ms,
        loaded.config["absolute_source_start_ms"] + residence.end_ms,
    ) == (820_590, 824_090)

    formula = by_text["N和L一般都是NNLL，是吗"]
    assert (
        loaded.config["absolute_source_start_ms"] + formula.start_ms,
        loaded.config["absolute_source_start_ms"] + formula.end_ms,
    ) == (871_920, 874_980)

    response = by_text["行啊"]
    assert (
        loaded.config["absolute_source_start_ms"] + response.start_ms,
        loaded.config["absolute_source_start_ms"] + response.end_ms,
    ) == (835_950, 836_510)

    assert validate_srt_file(
        loaded.baseline_path,
        media_duration_ms=(
            loaded.config["absolute_source_end_ms"]
            - loaded.config["absolute_source_start_ms"]
        ),
    )["status"] == "PASS"


def test_committed_chair_baseline_ends_at_fake_cry_before_next_superchat():
    root = (
        Path(__file__).resolve().parents[1]
        / "assets"
        / "lidousha"
        / "reviewed_subtitle_baselines"
    )
    loaded = load_candidate_reviewed_subtitle_baseline(
        root,
        "auto_193450_1573_1672",
    )

    assert loaded is not None
    assert loaded.config["absolute_source_start_ms"] == 1_572_910
    assert loaded.config["absolute_source_end_ms"] == 1_672_970
    cues = parse_srt_cues(loaded.baseline_path.read_text(encoding="utf-8"))
    texts = [cue.text for cue in cues]
    assert texts[-1] == "假哭"
    assert "然后我就坐这一个" in texts
    assert "我真的很有型啊" in texts
    assert all("钢镚" not in text for text in texts)
    assert all("鼠标" not in text for text in texts)
    assert cues[-1].end_ms == 100_060
