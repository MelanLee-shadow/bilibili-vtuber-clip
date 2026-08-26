from __future__ import annotations

import hashlib

import pytest

from src.autoslice import fastlane_c7b_private_adapter as adapter


def test_c7b_private_revival_has_exact_final_text_title_and_no_live_capability() -> None:
    result = adapter.build_private_revival()
    assert result["candidate_id"] == "auto_130040_201_255"
    assert result["title"] == "李姐也是脑控大师，但即使被脑控仍然信不了李1是怎么回事呢"
    assert result["content_boundary"]["status"] == "REVIVED_CANDIDATE_PRIVATE_ONLY"
    assert result["capabilities"] == {key: False for key in ("provider", "state", "ssh", "deploy", "upload")}
    text = result["reviewed_srt"]["bytes"].decode("utf-8")
    assert hashlib.sha256(result["reviewed_srt"]["bytes"]).hexdigest() == result["reviewed_srt"]["sha256"][7:]
    assert "可能这就是kmx" in text and "脑控状态都信不了李1\n\n19" in text
    assert "怎么这样" in text and "信李侄的是个什么状态" in text
    assert "怎么了你" not in text and "女状态" not in text


def test_c7b_private_revival_fails_closed_on_reviewed_bytes_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(adapter, "_SRT", adapter._SRT.with_name("missing.reviewed.srt"))
    with pytest.raises(adapter.C7bPrivateAuthorityError, match="C7B_PRIVATE_REGULAR_FILE_REQUIRED"):
        adapter.build_private_revival()


def test_c7b_replay_carry_is_exact_and_never_generic() -> None:
    base = {"given_title": None, "published_cover_carry": None}
    assert adapter.apply_replay_carry(base, candidate_id="other", recording_date="2026-08-14", baseline_sha256="sha256:" + "0" * 64) is base
    carried = adapter.apply_replay_carry(base, candidate_id="auto_130040_201_255", recording_date="2026-08-14", baseline_sha256="sha256:e07e2e23d2eacebbeb95a4700c7fa4005fb4342a432c28d0adde1d4e68e98457")
    assert carried["given_title"] == "李姐也是脑控大师，但即使被脑控仍然信不了李1是怎么回事呢"
    assert carried["published_cover_carry"]["sha256"].endswith("df3df8ae3902d7d0118c08f7d1c8aee0d577ed2cd12d4a87e62c747421b2a96f")
