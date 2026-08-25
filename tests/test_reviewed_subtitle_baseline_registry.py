import hashlib
import json
from pathlib import Path

import pytest

import src.autoslice.reviewed_subtitle_baseline_registry as baseline_registry
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


ROOT = Path(__file__).resolve().parents[1]
EXACT_CANDIDATE = "auto_223750_578_734"


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


def _write_exact_lidousha_assets(lidousha_root: Path) -> Path:
    source_lidousha = ROOT / "assets/lidousha"
    baseline_root = lidousha_root / "reviewed_subtitle_baselines"
    authority_root = lidousha_root / "reviewed_exact_source_intervals"
    baseline_root.mkdir(parents=True, exist_ok=True)
    authority_root.mkdir(parents=True, exist_ok=True)
    for name in (
        f"{EXACT_CANDIDATE}.subtitle-baseline.v1.json",
        f"{EXACT_CANDIDATE}.reviewed.srt",
    ):
        (baseline_root / name).write_bytes(
            (source_lidousha / "reviewed_subtitle_baselines" / name).read_bytes()
        )
    authority_name = f"{EXACT_CANDIDATE}.reviewed-exact-source-interval.v1.json"
    (authority_root / authority_name).write_bytes(
        (source_lidousha / "reviewed_exact_source_intervals" / authority_name).read_bytes()
    )
    return baseline_root


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

    loaded = load_candidate_reviewed_subtitle_baseline(tmp_path, "auto_1_2_3")

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


def _copy_c5_v3_asset(root: Path) -> Path:
    source = ROOT / "assets/lidousha/reviewed_subtitle_baselines"
    candidate_id = "auto_113028_1271_1328"
    root.mkdir(parents=True, exist_ok=True)
    for suffix in (
        ".reviewed.srt",
        ".pipeline-diagnostic.srt",
        ".operator-decisions.v3.json",
        ".operator-truth-diff.v2.json",
        ".subtitle-baseline.v1.json",
    ):
        (root / f"{candidate_id}{suffix}").write_bytes(
            (source / f"{candidate_id}{suffix}").read_bytes()
        )
    return root / f"{candidate_id}.subtitle-baseline.v1.json"


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda doc: doc.pop("time_domain"), "TIME_DOMAIN_MISSING_OR_INVALID"),
        (lambda doc: doc.update(time_domain="UNKNOWN"), "TIME_DOMAIN_MISSING_OR_INVALID"),
        (
            lambda doc: doc.update(absolute_source_end_ms=doc["absolute_source_start_ms"] + 500),
            "TIME_DOMAIN_GEOMETRY_INVALID",
        ),
    ],
)
def test_operator_v3_time_domain_is_mandatory_and_bounds_the_srt(
    tmp_path: Path, mutate, message: str,
) -> None:
    manifest = _copy_c5_v3_asset(tmp_path)
    document = json.loads(manifest.read_text(encoding="utf-8"))
    mutate(document)
    manifest.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ReviewedSubtitleBaselineRegistryError, match=message):
        load_candidate_reviewed_subtitle_baseline(tmp_path, "auto_113028_1271_1328")


def test_symlinked_baseline_fails_closed(tmp_path):
    manifest, baseline = _write_asset(tmp_path)
    target = tmp_path / "real.srt"
    baseline.rename(target)
    baseline.symlink_to(target)

    with pytest.raises(ReviewedSubtitleBaselineRegistryError, match="non-symlink"):
        load_candidate_reviewed_subtitle_baseline(tmp_path, "auto_1_2_3")


def test_exact_authority_uses_canonical_paths_under_explicit_trusted_repo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "trusted-repo"
    baseline_root = _write_exact_lidousha_assets(repo_root / "assets/lidousha")
    sealed_paths: list[Path] = []

    def seal(**kwargs: object) -> None:
        assert kwargs["repo_root"] == repo_root
        sealed_paths.append(Path(kwargs["relative_path"]))

    monkeypatch.setattr(
        baseline_registry,
        "require_repository_asset_authority",
        seal,
    )

    loaded = load_candidate_reviewed_subtitle_baseline(
        baseline_root,
        EXACT_CANDIDATE,
        repo_root=repo_root,
    )

    assert loaded is not None
    assert loaded.exact_interval_authority is not None
    assert sealed_paths == [
        Path(
            "assets/lidousha/reviewed_subtitle_baselines/"
            f"{EXACT_CANDIDATE}.subtitle-baseline.v1.json"
        ),
        Path(
            "assets/lidousha/reviewed_exact_source_intervals/"
            f"{EXACT_CANDIDATE}.reviewed-exact-source-interval.v1.json"
        ),
    ]


@pytest.mark.parametrize("target_location", ["outside", "inside"])
def test_exact_authority_rejects_symlinked_lidousha_parent_even_if_target_is_repo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target_location: str,
) -> None:
    repo_root = tmp_path / "trusted-repo"
    (repo_root / "assets").mkdir(parents=True)
    if target_location == "outside":
        target = tmp_path / "outside-assets/lidousha"
    else:
        target = repo_root / "relocated/lidousha"
    _write_exact_lidousha_assets(target)
    (repo_root / "assets/lidousha").symlink_to(
        target,
        target_is_directory=True,
    )
    monkeypatch.setattr(
        baseline_registry,
        "require_repository_asset_authority",
        lambda **_kwargs: None,
    )

    with pytest.raises(
        ReviewedSubtitleBaselineRegistryError,
        match="parent symlink",
    ):
        load_candidate_reviewed_subtitle_baseline(
            repo_root / "assets/lidousha/reviewed_subtitle_baselines",
            EXACT_CANDIDATE,
            repo_root=repo_root,
        )


def test_exact_authority_directory_symlink_is_normalized_to_registry_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "trusted-repo"
    lidousha_root = repo_root / "assets/lidousha"
    baseline_root = _write_exact_lidousha_assets(lidousha_root)
    canonical_authority_root = lidousha_root / "reviewed_exact_source_intervals"
    outside_authority_root = tmp_path / "outside-exact-authority"
    canonical_authority_root.rename(outside_authority_root)
    canonical_authority_root.symlink_to(
        outside_authority_root,
        target_is_directory=True,
    )
    monkeypatch.setattr(
        baseline_registry,
        "require_repository_asset_authority",
        lambda **_kwargs: None,
    )

    with pytest.raises(ReviewedSubtitleBaselineRegistryError, match="parent symlink"):
        load_candidate_reviewed_subtitle_baseline(
            baseline_root,
            EXACT_CANDIDATE,
            repo_root=repo_root,
        )


def test_committed_nancho_baseline_binds_new_truths_to_absolute_source_timeline():
    root = (
        Path(__file__).resolve().parents[1] / "assets" / "lidousha" / "reviewed_subtitle_baselines"
    )
    loaded = load_candidate_reviewed_subtitle_baseline(
        root,
        "auto_193450_672_945",
    )

    assert loaded is not None
    assert loaded.config["source_recording_basename"] == ("22966160_20260722-19-34-50.mp4")
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

    assert (
        validate_srt_file(
            loaded.baseline_path,
            media_duration_ms=(
                loaded.config["absolute_source_end_ms"] - loaded.config["absolute_source_start_ms"]
            ),
        )["status"]
        == "PASS"
    )


def test_committed_chair_baseline_ends_at_fake_cry_before_next_superchat():
    root = (
        Path(__file__).resolve().parents[1] / "assets" / "lidousha" / "reviewed_subtitle_baselines"
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
