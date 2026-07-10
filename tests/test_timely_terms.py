import datetime as dt
import json

import scripts.gemini_slice_jingting as jingting


def test_timely_terms_are_fresh_recency_candidates_not_blind_replacements(tmp_path, monkeypatch):
    snapshot = tmp_path / "timely.json"
    snapshot.write_text(
        json.dumps(
            {
                "schema_version": "lidousha-timely-terms.v1",
                "generated_at": "2026-07-10T00:00:00-04:00",
                "expires_at": "2026-08-10T00:00:00-04:00",
                "terms": [
                    {
                        "canonical": "梦限大",
                        "readings": ["mengxianda"],
                        "aliases": ["夢限大みゅーたいぷ"],
                        "confusables": ["Mujica"],
                        "topic_entities": ["邦多利"],
                        "active_from": "2026-06-01",
                        "active_until": "2026-09-30",
                        "reason": "current anime",
                        "sources": [{"url": "https://example.test/official", "published_at": "2026-06-01"}],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(jingting, "TIMELY_TERMS_PATHS", [str(snapshot)])

    fresh = jingting.timely_terms_context(
        as_of=dt.datetime(2026, 7, 10, 12, tzinfo=dt.timezone.utc)
    )
    assert "snapshot_status: FRESH" in fresh
    assert "梦限大" in fresh and "Mujica" in fresh and "mengxianda" in fresh
    assert "只提供候选，不是盲替换表" in fresh

    stale = jingting.timely_terms_context(
        as_of=dt.datetime(2026, 9, 1, 12, tzinfo=dt.timezone.utc)
    )
    assert "STALE" in stale
    assert "禁止覆盖音频/结构化原文" in stale


def test_timely_terms_use_recording_date_and_exclude_future_sources(tmp_path, monkeypatch):
    snapshot = tmp_path / "timely.json"
    snapshot.write_text(
        json.dumps(
            {
                "schema_version": "lidousha-timely-terms.v1",
                "generated_at": "2026-07-10T00:00:00-04:00",
                "expires_at": "2026-08-10T00:00:00-04:00",
                "terms": [
                    {
                        "canonical": "未来专名",
                        "readings": ["weilai"],
                        "active_from": "2026-01-01",
                        "active_until": "2026-12-31",
                        "sources": [
                            {"url": "https://example.test/future", "published_at": "2026-07-11"}
                        ],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(jingting, "TIMELY_TERMS_PATHS", [str(snapshot)])
    monkeypatch.setenv("LIDOUSHA_TERM_AS_OF", "2026-07-10")

    assert jingting.timely_terms_context() == ""


def test_old_recording_does_not_receive_current_window_claim(monkeypatch):
    monkeypatch.setenv("LIDOUSHA_TERM_AS_OF", "2025-01-01")

    context = jingting.glossary()

    assert "时效实体快照" not in context
    assert "2026 年 7 月动画" not in context
    assert "稳定规范写法：梦限大" in context


def test_recording_date_is_derived_from_live_path_or_blrec_filename():
    assert (
        jingting.recording_date_from_path(
            "/opt/bilive/autoslice/out/2026-07-10/candidate/padded.mp4"
        )
        == "2026-07-10"
    )
    assert (
        jingting.recording_date_from_path("22966160_20260710-20-00-09.mp4")
        == "2026-07-10"
    )


def test_agy_prompt_filters_timely_terms_as_of_recording_date(monkeypatch):
    monkeypatch.delenv("LIDOUSHA_TERM_AS_OF", raising=False)

    old_prompt = jingting.agy_prompt("draft", as_of_date="2025-01-01")
    current_prompt = jingting.agy_prompt("draft", as_of_date="2026-07-10")

    assert "时效实体快照" not in old_prompt
    assert "snapshot_status: FRESH" in current_prompt
    assert "梦限大" in current_prompt
