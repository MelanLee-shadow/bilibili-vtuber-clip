from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from src.autoslice.existing_subtitle_surface import (
    AUTHORITY_KIND,
    CONFIG_KEY,
    SCHEMA_VERSION,
    select_nonaggregate_transcriber_builder,
)


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _surface(tmp_path: Path) -> tuple[dict, Path, str]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    media = tmp_path / "padded.mp4"
    media.write_bytes(b"exact padded media bytes")
    srt = tmp_path / "padded.cpa-reviewed.srt"
    text = (
        "1\n00:00:00,500 --> 00:00:01,500\n开场\n\n"
        "2\n00:00:02,000 --> 00:00:03,250\n下一话题\n"
    )
    srt.write_text(text, encoding="utf-8")
    spec = {
        "candidate_id": "candidate",
        CONFIG_KEY: {
            "schema_version": SCHEMA_VERSION,
            "candidate_id": "candidate",
            "path": str(srt),
            "sha256": _sha(srt.read_bytes()),
            "source_media_sha256": _sha(media.read_bytes()),
            "time_domain": "padded_local",
            "authority_kind": AUTHORITY_KIND,
        },
    }
    return spec, media, text


def test_existing_surface_is_hash_bound_non_asr_substrate(tmp_path: Path) -> None:
    spec, media, text = _surface(tmp_path)
    legacy_calls: list[object] = []

    def legacy(*args, **kwargs):
        legacy_calls.append((args, kwargs))
        raise AssertionError("existing_srt must not call an ASR builder")

    builder = select_nonaggregate_transcriber_builder(
        "existing_srt",
        spec=spec,
        padded=media,
        padded_duration_ms=4_000,
        legacy_builder=legacy,
    )
    transcriber = builder("localhost", danmaku_items=None, window_start_ms=0)

    assert transcriber(media, [(0, 4_000)]) == text
    assert legacy_calls == []
    assert spec["existing_subtitle_surface_receipt"] == {
        "schema_version": "existing-subtitle-surface-consumption.v1",
        "status": "PASS",
        "candidate_id": "candidate",
        "subtitle_sha256": _sha(text.encode("utf-8")),
        "source_media_sha256": _sha(media.read_bytes()),
        "time_domain": "padded_local",
        "authority_kind": AUTHORITY_KIND,
        "cue_count": 2,
        "asr_calls": 0,
        "provider_calls": 0,
    }


def test_other_substrates_keep_the_existing_builder() -> None:
    legacy = object()
    assert select_nonaggregate_transcriber_builder(
        "agy_fresh",
        spec={},
        padded=Path("unused"),
        padded_duration_ms=1,
        legacy_builder=legacy,
    ) is legacy


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda spec, _media: spec[CONFIG_KEY].update(candidate_id="other"), "candidate"),
        (lambda spec, _media: spec[CONFIG_KEY].update(sha256="sha256:" + "0" * 64), "subtitle sha256"),
        (lambda spec, _media: spec[CONFIG_KEY].update(source_media_sha256="sha256:" + "0" * 64), "source media sha256"),
        (lambda spec, _media: spec[CONFIG_KEY].update(time_domain="delivery_local"), "padded_local"),
        (lambda spec, _media: spec[CONFIG_KEY].update(authority_kind="HUMAN_TRUTH"), "authority kind"),
    ],
)
def test_existing_surface_rejects_contract_drift(tmp_path: Path, mutation, message: str) -> None:
    spec, media, _text = _surface(tmp_path)
    mutation(spec, media)
    with pytest.raises(ValueError, match=message):
        select_nonaggregate_transcriber_builder(
            "existing_srt",
            spec=spec,
            padded=media,
            padded_duration_ms=4_000,
            legacy_builder=object(),
        )


def test_existing_surface_rejects_symlink_and_cue_outside_media(tmp_path: Path) -> None:
    spec, media, _text = _surface(tmp_path)
    original = Path(spec[CONFIG_KEY]["path"])
    linked = tmp_path / "linked.srt"
    linked.symlink_to(original)
    spec[CONFIG_KEY]["path"] = str(linked)
    with pytest.raises(ValueError, match="regular non-symlink"):
        select_nonaggregate_transcriber_builder(
            "existing_srt",
            spec=spec,
            padded=media,
            padded_duration_ms=4_000,
            legacy_builder=object(),
        )

    spec, media, _text = _surface(tmp_path / "second")
    Path(spec[CONFIG_KEY]["path"]).write_text(
        "1\n00:00:03,500 --> 00:00:04,500\n越界\n",
        encoding="utf-8",
    )
    spec[CONFIG_KEY]["sha256"] = _sha(Path(spec[CONFIG_KEY]["path"]).read_bytes())
    with pytest.raises(ValueError, match="outside padded media"):
        select_nonaggregate_transcriber_builder(
            "existing_srt",
            spec=spec,
            padded=media,
            padded_duration_ms=4_000,
            legacy_builder=object(),
        )


def test_producer_parser_exposes_existing_srt_substrate() -> None:
    from src.autoslice.producer_request import parse_producer_args

    args = parse_producer_args(
        ["--spec", "candidate.json", "--substrate", "existing_srt"],
        description="test",
        speaker_display_name="李豆沙",
        default_speaker_mode="uniform_host",
    )
    assert args.substrate == "existing_srt"
