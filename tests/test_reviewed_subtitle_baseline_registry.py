import hashlib
import json
from pathlib import Path

import pytest

from src.autoslice.reviewed_subtitle_baseline_registry import (
    ReviewedSubtitleBaselineRegistryError,
    load_candidate_reviewed_subtitle_baseline,
)


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
    assert "registry_schema_version" not in loaded.config
    assert "candidate_id" not in loaded.config
    assert loaded.fingerprint_paths == (manifest.resolve(), baseline.resolve())


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
