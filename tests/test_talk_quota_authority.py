"""Compatibility and fail-closed checks for the shared dated quota authority."""

from copy import deepcopy
import json

import pytest

from src.autoslice import talk_quota_authority as authority


from tests.quota_support import synthetic_quota_authority

@pytest.fixture(autouse=True)
def _synthetic_quota_input(tmp_path, monkeypatch):
    path = tmp_path / "synthetic-quota-authority.json"
    path.write_text(json.dumps(synthetic_quota_authority(), ensure_ascii=False))
    monkeypatch.setattr(authority, "DEFAULT_AUTHORITY_PATH", path)


def _write(tmp_path, document):
    path = tmp_path / "quota.json"
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    return path





@pytest.mark.parametrize("legacy", [False, True])
def test_talk_grants_and_legacy_v1_reading_remain_unchanged(tmp_path, legacy):
    document = authority.load_talk_quota_policy_authority()
    assert document is not None
    if legacy:
        document["default_policy"].pop("song")
        document["entries"] = [row for row in document["entries"] if row["scope"] != "song"]
    path = _write(tmp_path, document)
    assert authority.validate_talk_quota_policy_authority(document) == document
    for date, scope, cap, gate in [
        ("2026-08-07", "game", 10, 85),
        ("2026-08-08", "event", 15, 85),
        ("2026-08-07", "event", 5, None),
        ("2026-08-08", "game", 5, None),
        ("2026-08-08", "talk", 5, None),
        ("2026-10-21", "talk", 5, None),
    ]:
        grant = authority.resolve_quota_grant(date, scope, path=path)
        assert (grant.cap, grant.extra_slot_min_score) == (cap, gate)
    song = authority.resolve_quota_grant("2026-10-21", "song", path=path)
    assert song.cap == (1 if legacy else 2)
    if legacy:
        assert song.source == "default:legacy_song_policy"


@pytest.mark.parametrize("failure", ["absent", "unreadable", "duplicate_scope", "duplicate_id"])
def test_song_authority_failure_denies_widening(tmp_path, failure):
    path = tmp_path / "quota.json"
    if failure == "unreadable":
        path.write_text("{invalid", encoding="utf-8")
    elif failure.startswith("duplicate"):
        document = authority.load_talk_quota_policy_authority()
        duplicate = deepcopy(next(row for row in document["entries"] if row["scope"] == "song"))
        if failure == "duplicate_scope":
            duplicate["entry_id"] += "-duplicate"
        else:
            duplicate["recording_date"] = "2026-10-22"
        document["entries"].append(duplicate)
        _write(tmp_path, document)
        with pytest.raises(authority.TalkQuotaAuthorityError, match="duplicate"):
            authority.validate_talk_quota_policy_authority(document)
    grant = authority.resolve_quota_grant("2026-10-21", "song", path=path)
    assert (grant.cap, grant.extra_slot_min_score) == (1, None)
    assert grant.source == ("default:authority_absent" if failure == "absent" else "default:authority_invalid")


@pytest.mark.parametrize("change", [
    {"cap": 2}, {"cap": True}, {"cap": 0},
    {"extra_slot_min_score": 85}, {"authority": ""},
])
def test_song_default_cannot_become_a_global_widening(tmp_path, change):
    document = authority.load_talk_quota_policy_authority()
    document["default_policy"]["song"].update(change)
    with pytest.raises(authority.TalkQuotaAuthorityError):
        authority.validate_talk_quota_policy_authority(document)
    grant = authority.resolve_quota_grant("2026-10-21", "song", path=_write(tmp_path, document))
    assert grant.cap == 1
    assert grant.source == "default:authority_invalid"


@pytest.mark.parametrize("caller_cap", [0, 99])
def test_song_fallback_cannot_be_replaced_by_a_caller(tmp_path, caller_cap):
    grant = authority.resolve_quota_grant(
        "2026-10-22", "song", default_cap=caller_cap, path=tmp_path / "absent.json",
    )
    assert grant.cap == 1
