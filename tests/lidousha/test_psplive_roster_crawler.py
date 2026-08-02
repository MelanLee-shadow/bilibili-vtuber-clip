import datetime as dt
import json
from pathlib import Path

import pytest

from src.autoslice.psplive_roster_crawler import (
    PspliveRosterError,
    build_snapshot,
    load_source_config,
    validate_snapshot,
)


def _config() -> dict:
    return load_source_config(Path("assets/lidousha/psplive_roster_sources.v1.json"))


def _official_payload(config: dict) -> dict:
    participants = list(config["member_overrides"])
    source = config["sources"][0]
    return {
        "code": 0,
        "data": {
            "owner": {
                "mid": source["owner_mid"],
                "name": source["owner_name"],
            },
            "title": "【2025新春】虚拟艺人团体psplive给大家拜年啦！",
            "desc": "简介\n参与人员\n" + "、".join(participants),
        },
    }


def test_official_roster_snapshot_contains_kaya_and_is_occurrence_neutral():
    config = _config()
    snapshot = build_snapshot(
        api_payloads=[_official_payload(config)],
        config=config,
        generated_at=dt.datetime(2026, 7, 17, tzinfo=dt.timezone.utc),
    )

    kaya = next(row for row in snapshot["members"] if row["canonical"] == "萱萱卡娅")
    assert "Kaya" in kaya["aliases"]
    assert snapshot["occurrence_policy"] == (
        "ENTITY_EXISTENCE_ONLY_NOT_CUE_OCCURRENCE_EVIDENCE"
    )
    assert len(snapshot["members"]) == 26


def test_official_roster_owner_mismatch_fails_closed():
    config = _config()
    payload = _official_payload(config)
    payload["data"]["owner"]["mid"] = 1

    with pytest.raises(PspliveRosterError, match="owner identity mismatch"):
        build_snapshot(
            api_payloads=[payload],
            config=config,
            generated_at=dt.datetime(2026, 7, 17, tzinfo=dt.timezone.utc),
        )


def test_snapshot_without_occurrence_neutral_policy_is_rejected():
    config = _config()
    snapshot = build_snapshot(
        api_payloads=[_official_payload(config)],
        config=config,
        generated_at=dt.datetime(2026, 7, 17, tzinfo=dt.timezone.utc),
    )
    drifted = json.loads(json.dumps(snapshot, ensure_ascii=False))
    drifted["occurrence_policy"] = "GLOSSARY_NAME_ALWAYS_WINS"

    with pytest.raises(PspliveRosterError, match="occurrence-neutral"):
        validate_snapshot(drifted)


def test_runtime_snapshot_reaches_subtitle_prompt_without_becoming_occurrence_proof(
    tmp_path, monkeypatch
):
    from scripts import gemini_slice_jingting as jingting
    from src.autoslice.psplive_roster_crawler import snapshot_json

    config = _config()
    snapshot = build_snapshot(
        api_payloads=[_official_payload(config)],
        config=config,
        generated_at=dt.datetime(2026, 7, 17, tzinfo=dt.timezone.utc),
    )
    path = tmp_path / "psplive_roster.json"
    path.write_text(snapshot_json(snapshot), encoding="utf-8")
    monkeypatch.setattr(jingting, "PSPLIVE_ROSTER_PATHS", (str(path),))

    context = jingting.psplive_roster_context(
        as_of=dt.datetime(2026, 7, 17, tzinfo=dt.timezone.utc)
    )

    assert "萱萱卡娅" in context
    assert "Kaya" in context
    assert "绝不证明本句提到了它" in context
    assert "所有其他专名地位完全平等" in context
