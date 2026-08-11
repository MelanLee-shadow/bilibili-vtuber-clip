from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from src.autoslice import producer_request
from src.autoslice.subtitle_regression import SCHEMA_VERSION as REGRESSION_SCHEMA_VERSION


ROOT = Path(__file__).resolve().parents[1]
CANDIDATE_ID = "preflight_candidate"


def _spec_path(
    tmp_path: Path,
    *,
    truth_inputs: dict[str, object] | None = None,
) -> Path:
    path = tmp_path / "spec.json"
    document = {
        "candidate_id": CANDIDATE_ID,
        "human_truth_mode": "delivery",
        "output_root": str(tmp_path / "output"),
        "pieces": [],
    }
    document.update(truth_inputs or {})
    path.write_text(
        json.dumps(document),
        encoding="utf-8",
    )
    return path


def _args(
    spec_path: Path,
    *,
    text_overrides: Path | None = None,
    subtitle_regression: Path | None = None,
) -> argparse.Namespace:
    return argparse.Namespace(
        spec=spec_path,
        subtitle_text_overrides=text_overrides,
        subtitle_regression=subtitle_regression,
        speaker_overrides=None,
        ssh_host="localhost",
    )


def _load(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    text_overrides: Path | None = None,
    subtitle_regression: Path | None = None,
    spec_truth_inputs: dict[str, object] | None = None,
) -> producer_request.ProducerRequest:
    monkeypatch.setenv("AUTOSLICE_HUMAN_TRUTH_MODE", "delivery")
    monkeypatch.setattr(
        producer_request,
        "require_branding_intro",
        lambda *_args, **_kwargs: None,
    )
    spec_path = _spec_path(tmp_path, truth_inputs=spec_truth_inputs)
    return producer_request.load_producer_request(
        _args(
            spec_path,
            text_overrides=text_overrides,
            subtitle_regression=subtitle_regression,
        ),
        repo_root=ROOT,
        profile_asset_file=lambda _name: tmp_path / "unused.json",
    )


@pytest.mark.parametrize("schema_version", [1, 2, 3])
def test_text_override_preflight_accepts_supported_schema_versions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    schema_version: int,
) -> None:
    asset = tmp_path / "text-overrides.json"
    asset.write_text(json.dumps({"schema_version": schema_version}), encoding="utf-8")

    request = _load(tmp_path, monkeypatch, text_overrides=asset)

    assert request.text_override_path == asset
    assert request.out_root.is_dir()


def test_subtitle_regression_preflight_accepts_its_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = tmp_path / "subtitle-regression.json"
    asset.write_text(
        json.dumps(
            {
                "schema_version": REGRESSION_SCHEMA_VERSION,
                "candidate_id": CANDIDATE_ID,
                "required_payload_substrings": ["reviewed truth"],
            }
        ),
        encoding="utf-8",
    )

    request = _load(tmp_path, monkeypatch, subtitle_regression=asset)

    assert request.subtitle_regression_path == asset
    assert request.out_root.is_dir()


@pytest.mark.parametrize(
    ("field", "payload", "message"),
    [
        (
            "text_overrides",
            {"schema_version": "subtitle-redelivery-baseline.v1"},
            "text override schema_version must be 1, 2, or 3",
        ),
        (
            "subtitle_regression",
            {"schema_version": "subtitle-redelivery-baseline.v1"},
            "subtitle regression schema_version",
        ),
        ("text_overrides", {}, "text override schema_version"),
        ("subtitle_regression", {}, "subtitle regression schema_version"),
        ("text_overrides", [], "text override document must be a JSON object"),
        (
            "subtitle_regression",
            [],
            "subtitle regression document must be an object",
        ),
        (
            "text_overrides",
            {"schema_version": True},
            "text override schema_version must be 1, 2, or 3",
        ),
        (
            "subtitle_regression",
            {"schema_version": 1},
            "subtitle regression schema_version",
        ),
    ],
)
def test_bad_truth_document_schema_fails_before_output_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    payload: object,
    message: str,
) -> None:
    asset = tmp_path / "wrong-truth-asset.json"
    asset.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        _load(tmp_path, monkeypatch, **{field: asset})

    assert not (tmp_path / "output").exists()


@pytest.mark.parametrize("field", ["text_overrides", "subtitle_regression"])
def test_missing_truth_asset_fails_before_output_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
) -> None:
    missing = tmp_path / "missing.json"

    with pytest.raises(ValueError, match="must be a regular non-symlink file"):
        _load(tmp_path, monkeypatch, **{field: missing})

    assert not (tmp_path / "output").exists()


@pytest.mark.parametrize(
    ("field", "payload"),
    [
        ("text_overrides", {"schema_version": 3}),
        ("subtitle_regression", {"schema_version": REGRESSION_SCHEMA_VERSION}),
    ],
)
def test_symlinked_truth_asset_fails_before_output_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    payload: object,
) -> None:
    target = tmp_path / "target.json"
    target.write_text(json.dumps(payload), encoding="utf-8")
    symlink = tmp_path / "truth-link.json"
    symlink.symlink_to(target)

    with pytest.raises(ValueError, match="must be a regular non-symlink file"):
        _load(tmp_path, monkeypatch, **{field: symlink})

    assert not (tmp_path / "output").exists()


@pytest.mark.parametrize(
    ("spec_field", "payload"),
    [
        ("subtitle_text_overrides", {"schema_version": 3}),
        (
            "subtitle_regression",
            {
                "schema_version": REGRESSION_SCHEMA_VERSION,
                "candidate_id": CANDIDATE_ID,
                "required_payload_substrings": ["reviewed truth"],
            },
        ),
    ],
)
def test_relative_spec_truth_symlink_fails_before_output_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    spec_field: str,
    payload: object,
) -> None:
    target = tmp_path / "target.json"
    target.write_text(json.dumps(payload), encoding="utf-8")
    symlink = tmp_path / "truth-link.json"
    symlink.symlink_to(target)

    with pytest.raises(ValueError, match="must be a regular non-symlink file"):
        _load(
            tmp_path,
            monkeypatch,
            spec_truth_inputs={spec_field: symlink.name},
        )

    assert not (tmp_path / "output").exists()
