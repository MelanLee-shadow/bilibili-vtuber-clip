from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.autoslice.producer_boundary_resolution import (
    _redelivery_baseline_head_rel_ms,
)
from src.autoslice.redelivery_full_window_replay import (
    FullWindowReplayError,
    exact_full_window_replay_enabled,
)
from src.autoslice.redelivery_source_binding import V2RedeliverySourceBinding
from src.autoslice.reviewed_subtitle_baseline_registry import (
    load_candidate_reviewed_subtitle_baseline,
)
from src.autoslice.redelivery_time_domain import (
    DELIVERY_LOCAL,
    PIECE_LOCAL,
    RedeliveryTimeDomainError,
    operator_v3_time_domain,
    require_baseline_receipt_parity,
    require_delivery_local_interval,
)


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets" / "lidousha" / "reviewed_subtitle_baselines"
PIN = {"schema_version": "operator-reviewed-text-full-ownership-pin.v3"}


def _config(*, domain: str | None, start: int, end: int) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": "subtitle-redelivery-baseline.v2",
        "exact_interval_replay": True,
        "absolute_source_start_ms": start,
        "absolute_source_end_ms": end,
        "operator_text_full_ownership": dict(PIN),
    }
    if domain is not None:
        value["time_domain"] = domain
    return value


def _binding() -> V2RedeliverySourceBinding:
    return V2RedeliverySourceBinding(
        absolute_source_start_ms=1_270_920,
        absolute_source_end_ms=1_328_694,
        source_recording_basename="recording.mp4",
        source_sha256="a" * 64,
        content_absolute_start_ms=1_261_170,
        content_absolute_end_ms=1_376_550,
        padded_content_start_ms=0,
        padded_content_end_ms=115_380,
    )


@pytest.mark.parametrize("domain", [None, "UNKNOWN", "delivery_local"])
def test_operator_v3_unknown_time_domain_fails_closed(domain: str | None) -> None:
    with pytest.raises(
        RedeliveryTimeDomainError,
        match="TIME_DOMAIN_MISSING_OR_INVALID",
    ):
        operator_v3_time_domain(_config(domain=domain, start=0, end=1))


def test_delivery_local_interval_must_equal_final_media_source_interval() -> None:
    config = _config(
        domain=DELIVERY_LOCAL,
        start=1_261_170,
        end=1_376_550,
    )
    with pytest.raises(
        RedeliveryTimeDomainError,
        match="DELIVERY_INTERVAL_MISMATCH",
    ):
        require_delivery_local_interval(
            config,
            absolute_start_ms=1_270_920,
            absolute_end_ms=1_328_694,
        )


def test_only_piece_local_baseline_selects_full_window_replay() -> None:
    binding = _binding()
    assert exact_full_window_replay_enabled(
        _config(
            domain=PIECE_LOCAL,
            start=binding.content_absolute_start_ms,
            end=binding.content_absolute_end_ms,
        ),
        binding,
    ) is True
    assert exact_full_window_replay_enabled(
        _config(
            domain=DELIVERY_LOCAL,
            start=binding.absolute_source_start_ms,
            end=binding.absolute_source_end_ms,
        ),
        binding,
    ) is False
    with pytest.raises(FullWindowReplayError, match="TIME_DOMAIN_MISSING_OR_INVALID"):
        exact_full_window_replay_enabled(
            _config(domain=None, start=0, end=1),
            binding,
        )


def test_piece_local_baseline_never_moves_media_head_to_source_zero() -> None:
    spec = {
        "semantic_start_ms": 1_271_170,
        "pieces": [{"start_ms": 1_261_170, "end_ms": 1_376_550}],
        "subtitle_redelivery_baseline": _config(
            domain=PIECE_LOCAL,
            start=1_261_170,
            end=1_376_550,
        ),
    }
    assert _redelivery_baseline_head_rel_ms(spec) is None


def test_delivery_local_head_rejects_the_old_full_piece_projection() -> None:
    spec = {
        "semantic_start_ms": 1_271_170,
        "pieces": [{"start_ms": 1_261_170, "end_ms": 1_376_550}],
        "subtitle_redelivery_baseline": _config(
            domain=DELIVERY_LOCAL,
            start=1_261_170,
            end=1_376_550,
        ),
    }
    with pytest.raises(
        SystemExit,
        match="DELIVERY_HEAD_SEMANTIC_MISMATCH",
    ):
        _redelivery_baseline_head_rel_ms(spec)
    spec["subtitle_redelivery_baseline"] = _config(
        domain=DELIVERY_LOCAL,
        start=1_270_920,
        end=1_328_694,
    )
    assert _redelivery_baseline_head_rel_ms(spec) == 9_750


_RECEIPTS = {
    "auto_203011_328_389": (
        ASSETS
        / "auto_203011_328_389.operator-reviewed-subtitle-baseline-delivery.v1.json"
    ),
    **{
        candidate_id: (
            ROOT
            / "docs/reviews"
            / f"{candidate_id}-operator-reviewed-subtitle-baseline-delivery.v1.json"
        )
        for candidate_id in (
            "auto_113028_1271_1328",
            "auto_113028_1602_1698",
            "auto_120032_753_816",
            "auto_123036_727_785",
        )
    },
}


@pytest.mark.parametrize("candidate_id", sorted(_RECEIPTS))
def test_checked_in_v3_manifest_receipt_parity(candidate_id: str) -> None:
    manifest = json.loads(
        (ASSETS / f"{candidate_id}.subtitle-baseline.v1.json").read_text(
            encoding="utf-8"
        )
    )
    receipt = json.loads(_RECEIPTS[candidate_id].read_text(encoding="utf-8"))
    require_baseline_receipt_parity(manifest, receipt)


def test_manifest_receipt_parity_rejects_each_authority_axis() -> None:
    candidate_id = "auto_113028_1271_1328"
    manifest = json.loads(
        (ASSETS / f"{candidate_id}.subtitle-baseline.v1.json").read_text(
            encoding="utf-8"
        )
    )
    receipt = json.loads(_RECEIPTS[candidate_id].read_text(encoding="utf-8"))
    mutations = [
        lambda value: value.__setitem__("candidate_id", "other"),
        lambda value: value["source_recording"].__setitem__(
            "time_domain", PIECE_LOCAL
        ),
        lambda value: value["source_recording"].__setitem__("sha256", "0" * 64),
        lambda value: value["source_srt"].__setitem__("sha256", "0" * 64),
        lambda value: value["truth_lanes"]["release_truth"].__setitem__(
            "srt_sha256", "0" * 64
        ),
        lambda value: value["truth_lanes"]["decision_ledger"].__setitem__(
            "sha256", "0" * 64
        ),
        lambda value: value["source_recording"].__setitem__(
            "absolute_start_ms", 0
        ),
        lambda value: value.__setitem__("baseline_sha256", "0" * 64),
        lambda value: value["reviewed_srt"].__setitem__("sha256", "0" * 64),
        lambda value: value.__setitem__("changed_cue_count", 0),
        lambda value: value["changed_cues"][0].__setitem__(
            "absolute_source_start_ms", 0
        ),
    ]
    for mutate in mutations:
        changed = json.loads(json.dumps(receipt))
        mutate(changed)
        with pytest.raises(
            RedeliveryTimeDomainError,
            match="REDELIVERY_BASELINE_RECEIPT_PARITY_MISMATCH",
        ):
            require_baseline_receipt_parity(manifest, changed)


def test_legacy_c3_exact_authority_synthesizes_only_its_sealed_domain() -> None:
    manifest = json.loads(
        (ASSETS / "auto_220021_561_670.subtitle-baseline.v1.json").read_text(
            encoding="utf-8"
        )
    )
    assert "time_domain" not in manifest
    baseline = load_candidate_reviewed_subtitle_baseline(
        ASSETS,
        "auto_220021_561_670",
        repo_root=ROOT,
    )
    assert baseline is not None
    assert baseline.config["time_domain"] == DELIVERY_LOCAL


@pytest.mark.parametrize(
    ("candidate_id", "domain", "start", "end"),
    [
        ("auto_113028_1271_1328", DELIVERY_LOCAL, 1_270_920, 1_328_694),
        ("auto_113028_1602_1698", DELIVERY_LOCAL, 1_602_510, 1_699_300),
        ("auto_120032_753_816", DELIVERY_LOCAL, 753_540, 816_400),
        ("auto_123036_727_785", DELIVERY_LOCAL, 726_910, 786_090),
        ("auto_203011_328_389", PIECE_LOCAL, 318_740, 437_660),
    ],
)
def test_checked_in_v3_assets_declare_observed_time_domain(
    candidate_id: str, domain: str, start: int, end: int,
) -> None:
    manifest = json.loads(
        (ASSETS / f"{candidate_id}.subtitle-baseline.v1.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["time_domain"] == domain
    assert (
        manifest["absolute_source_start_ms"],
        manifest["absolute_source_end_ms"],
    ) == (start, end)
