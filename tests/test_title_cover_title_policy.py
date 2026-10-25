"""Automatic talk title skill: real consumers with offline JSON fixtures."""

from __future__ import annotations
import copy
import json
import pytest

from src.autoslice import publish_staging, title_policy
from src.autoslice.publish_staging_paths import (
    current_editorial_binding,
    validate_current_editorial_binding,
)
from src.autoslice.title_cover_quotes import (
    aligned_quote_subtitles,
    check_title_candidates,
    check_title_quotes,
    find_quote,
    final_title_skill_receipt,
    validate_title_skill_selection,
)

SRT = (
    "1\n00:00:00,000 --> 00:00:02,000\n我讨厌你\n\n2\n00:00:02,100 --> 00:00:04,000\n你不许讨厌我\n"
)
TITLE = "【李豆沙】怎么“我讨厌你”还“不许讨厌我”？"


def candidates():
    other = "【李豆沙】“不许讨厌我”，这句话到底怎么接"
    return {
        "title": TITLE,
        "candidates": [
            {
                "title": TITLE,
                "quote": {"text": "不许讨厌我", "speaker": ""},
                "status": "SELECTED",
                "rationale": "OFFLINE fixture: one source-backed contrast",
            },
            {
                "title": other,
                "quote": {"text": "不许讨厌我", "speaker": ""},
                "status": "REJECTED",
                "rationale": "OFFLINE fixture: less complete contrast",
            },
        ],
        "editorial_choice": {"selected": TITLE, "reason": "OFFLINE candidate comparison"},
    }


def resolve(payload, srt=SRT, **kwargs):
    return publish_staging._resolve_automatic_title(
        base_prompt="OFFLINE test",
        title_llm_call=lambda _: json.dumps(payload, ensure_ascii=False),
        selection_hook="not a fact source",
        initial_title_source="job_title",
        final_srt_text=srt,
        **kwargs,
    )


def test_normal_title_requires_complete_final_srt_without_audio_fallback():
    with pytest.raises(ValueError, match="TITLE_FINAL_SUBTITLES_REQUIRED"):
        title_policy.build_automatic_talk_title_prompt(
            selection_hook="source unavailable",
            transcript_sample="a partial transcript",
            important_ip_prompt="",
            clip_context_prompt="",
            persona_asset="",
            style_asset="",
        )
    assert check_title_candidates(candidates(), SRT)["status"] == "PASS"
    result = resolve(candidates())
    assert result.title_authority_error is None and result.staged_title == TITLE
    assert result.title_generation_attempts[0]["title_cover_skill"]["audio_calls"] == 0


@pytest.mark.parametrize("count", [0, 1, 5])
def test_all_automatic_calls_require_two_to_four_compared_candidates(count):
    payload = candidates()
    payload["candidates"] = (payload["candidates"] * 3)[:count]
    result = resolve(payload)
    assert "TITLE_CANDIDATES_REQUIRE_2_TO_4" in result.title_policy_violations


@pytest.mark.parametrize(
    "mutation",
    ["unused_true_quote", "second_false_quote", "false_rejected", "missing_reason", "duplicate"],
)
def test_every_candidate_and_every_quote_is_checked(mutation):
    value = candidates()
    if mutation == "unused_true_quote":
        value["candidates"][0]["quote"]["text"] = "你不许讨厌我"
    elif mutation == "second_false_quote":
        value["title"] = value["candidates"][0]["title"] = TITLE.replace("我讨厌你", "我要暴富")
        value["editorial_choice"]["selected"] = value["title"]
    elif mutation == "false_rejected":
        value["candidates"][1]["title"] = "【李豆沙】“我已经结婚了”，这句话到底怎么接"
    elif mutation == "missing_reason":
        value["candidates"][1]["rationale"] = ""
    else:
        value["candidates"][1]["title"] = value["candidates"][0]["title"]
    assert check_title_candidates(value, SRT)["status"] == "FAIL"
    assert resolve(value).title_authority_error is not None


def test_known_mixed_speakers_and_partly_unknown_attribution_fail():
    mixed = SRT.replace("我讨厌你", "[kmx] 我讨厌你", 1).replace(
        "你不许讨厌我", "[李豆沙] 你不许讨厌我"
    )
    assert find_quote("我讨厌你你不许讨厌我", mixed) is None
    partial = SRT.replace("我讨厌你", "[李豆沙] 我讨厌你", 1)
    assert find_quote("我讨厌你你不许讨厌我", partial, speaker="李豆沙") is None
    assert check_title_quotes("【李豆沙】李豆沙说“不许讨厌我”", SRT)["status"] == "FAIL"
    assert check_title_quotes(TITLE, SRT)["matches"][0]["speaker_status"] == "UNKNOWN"


def test_matching_same_quote_uses_verified_requested_speaker():
    srt = "1\n00:00:00,000 --> 00:00:01,000\n[kmx] 不许讨厌我\n\n2\n00:00:01,000 --> 00:00:02,000\n[李豆沙] 不许讨厌我\n"
    match = find_quote("不许讨厌我", srt, speaker="李豆沙")
    assert match["cues"] == ["2"]


def test_stutter_removal_does_not_delete_negation_or_legitimate_repetition():
    srt = "1\n00:00:00,000 --> 00:00:02,000\n我，我不，不喜欢，人人有份，好好学习\n"
    assert find_quote("我不喜欢", srt)
    assert find_quote("我喜欢", srt) is None
    assert find_quote("人有份好学习", srt) is None


def test_aligned_speaker_subtitles_cannot_add_or_move_words(tmp_path):
    path = tmp_path / "speaker.srt"
    path.write_text(SRT.replace("我讨厌你", "[李豆沙] 我讨厌你", 1))
    record = {"speaker_review_srt_path": str(path)}
    assert "[李豆沙]" in aligned_quote_subtitles(record, SRT)
    path.write_text(path.read_text().replace("我讨厌你", "我要暴富"))
    with pytest.raises(ValueError, match="NOT_ALIGNED"):
        aligned_quote_subtitles(record, SRT)


def test_cached_raw_title_is_rechecked_against_current_subtitle(tmp_path, monkeypatch):
    called = []

    def cached(**kwargs):
        called.append(kwargs["lane"])
        return json.dumps(candidates(), ensure_ascii=False)

    monkeypatch.setattr(publish_staging, "completed_generation", cached)
    path = tmp_path / "final.srt"
    path.write_text(SRT.replace("不许讨厌我", "已经回家了"))
    result = resolve(
        candidates(),
        srt=path.read_text(),
        generation_root=tmp_path,
        generation_subtitle_path=path,
        input_binding={"candidate_id": "fixture"},
    )
    assert called and result.title_authority_error is not None
    assert "TITLE_QUOTE_NOT_IN_FINAL_SUBTITLES" in result.title_policy_violations


def test_fact_repair_final_title_and_saved_consumers_recheck_quotes(tmp_path):
    result = resolve(candidates())
    attempts = result.title_generation_attempts
    receipt = final_title_skill_receipt(TITLE, SRT, SRT, attempts)
    changed = TITLE.replace("不许讨厌我", "已经结婚了")
    assert final_title_skill_receipt(changed, SRT, SRT, attempts)["quote_check"]["status"] == "FAIL"
    path = tmp_path / "final.srt"
    path.write_text(SRT)
    record = {"candidate_id": "fixture", "subtitle_path": str(path)}
    staging = {
        "candidate_id": "fixture",
        "title": TITLE,
        "cover_path": None,
        "title_source": "llm",
        "title_generation_attempts": attempts,
        "title_authority_error": None,
    }
    binding = current_editorial_binding(record, staging, candidate_id="fixture")
    staging["current_binding"] = binding
    staging["title_selection"] = {
        "final_title": TITLE,
        "final_title_sha256": binding["title_sha256"],
        "final_input_sha256": binding["input_sha256"],
        "title_cover_skill": receipt,
    }
    validate_current_editorial_binding(record, staging, candidate_id="fixture")
    forged = copy.deepcopy(staging)
    forged["title"] = changed
    forged["title_selection"]["final_title"] = changed
    forged["title_selection"]["title_cover_skill"]["quote_check"]["status"] = "PASS"
    with pytest.raises(ValueError, match="TITLE_FINAL_SKILL_RECEIPT_DRIFT"):
        validate_title_skill_selection(record, forged)


def test_replay_recomputes_rejected_attempt_receipts_used_by_publish_choke(tmp_path):
    """A rejected attempt's stored receipt is never trusted from stored bytes."""

    payload = candidates()
    payload["candidates"][0]["quote"] = {"text": "已经结婚了", "speaker": ""}
    response = json.dumps(payload, ensure_ascii=False)
    honest = check_title_candidates(payload, SRT)
    assert honest["status"] == "FAIL"
    forged_receipt = copy.deepcopy(honest)
    forged_receipt["status"] = "PASS"
    forged_receipt["reason_codes"] = []
    attempt = {
        "attempt": 1,
        "status": "REJECTED_POLICY",
        "title": TITLE.removeprefix("【李豆沙】"),
        "response_text": response,
        "title_cover_skill": forged_receipt,
    }
    path = tmp_path / "final.srt"
    path.write_text(SRT)
    record = {"candidate_id": "fixture", "subtitle_path": str(path)}
    staging = {
        "title": TITLE,
        "title_source": "llm+lidousha_style_asset+deterministic_filler_removal_at_publish_choke",
        "title_generation_attempts": [attempt],
        "title_authority_error": None,
        "title_selection": {
            "title_cover_skill": final_title_skill_receipt(TITLE, SRT, SRT, [attempt]),
        },
    }
    with pytest.raises(ValueError, match="TITLE_CANDIDATE_QUOTE_RECEIPT_DRIFT"):
        validate_title_skill_selection(record, staging)


def test_manual_and_song_selection_keep_their_existing_rules():
    validate_title_skill_selection(
        {}, {"title": "【李豆沙】人工确认的标题", "title_source": "reviewer_manual_override"}
    )
    validate_title_skill_selection(
        {}, {"title": "【李豆沙】豆沙歌，《暖暖》", "title_source": "song_catalog"}
    )


def test_package_title_replay_uses_verified_portable_speaker_subtitles(tmp_path):
    from src.autoslice.publish_staging_paths import validate_package_editorial_binding

    final = tmp_path / "final.srt"
    final.write_text(SRT)
    speaker = tmp_path / "speaker.srt"
    quoted = SRT.replace("\n我讨厌你\n", "\n[李豆沙] 我讨厌你\n").replace(
        "\n你不许讨厌我\n",
        "\n[李豆沙] 你不许讨厌我\n",
    )
    speaker.write_text(quoted)
    cover = tmp_path / "fixture-cover.png"
    cover.write_bytes(b"opaque cover bytes for editorial hash binding only")
    attempts = resolve(candidates(), srt=quoted).title_generation_attempts
    receipt = final_title_skill_receipt(TITLE, SRT, quoted, attempts)
    record = {
        "candidate_id": "fixture",
        "subtitle_path": str(final),
        "speaker_review_srt_path": str(speaker),
    }
    staging = {
        "candidate_id": "fixture",
        "title": TITLE,
        "title_source": "llm",
        "cover_path": str(cover),
        "cover_generation": {},
        "title_generation_attempts": attempts,
        "title_authority_error": None,
    }
    binding = current_editorial_binding(record, staging, candidate_id="fixture")
    staging["current_binding"] = binding
    staging["title_selection"] = {
        "final_title": TITLE,
        "final_title_sha256": binding["title_sha256"],
        "final_input_sha256": binding["input_sha256"],
        "title_cover_skill": receipt,
    }
    record["publish_staging"] = staging
    publish = {**copy.deepcopy(staging), "subtitle_sha256": binding["subtitle_sha256"]}
    record["subtitle_path"] = "/absent-producer/final.srt"
    record["speaker_review_srt_path"] = "/absent-producer/speaker.srt"
    validate_package_editorial_binding(
        record,
        publish,
        candidate_id="fixture",
        subtitle_path=final,
        cover_path=cover,
        speaker_srt_path=speaker,
    )
    with pytest.raises(ValueError, match="TITLE_SPEAKER_PATH_MISSING"):
        validate_package_editorial_binding(
            record,
            publish,
            candidate_id="fixture",
            subtitle_path=final,
            cover_path=cover,
        )
    speaker.write_text(quoted.replace("[李豆沙]", "[kmx]"))
    with pytest.raises(ValueError, match="TITLE_.*DRIFT"):
        validate_package_editorial_binding(
            record,
            publish,
            candidate_id="fixture",
            subtitle_path=final,
            cover_path=cover,
            speaker_srt_path=speaker,
        )
