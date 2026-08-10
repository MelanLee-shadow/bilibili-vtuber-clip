"""歌名命名权威：判定「可能是歌」之后，命名权归听音频那条链。

Ivan 2026-08-10 逐字：「之前难道不是 gemini 听音频识别歌曲吗？如果意识到了
可能是歌再从 BCUT 切换过来」。

真数据背景 —— 2026-08-08 `song_210131_1210`：
* BCUT 中文 ASR 派生的 hook 写着《新型病毒》（与真名《心型病毒》同音，
  ``pypinyin`` 实测都是 ``xin xing bing du``，纯文本侧不可分）；
* 同一条候选的音频链其实已经证成：``netease://song/536937431``、
  ``matched_line_ratio=1.0``、``FULL_SONG_READY``；
* 但这行最终被 host-vocal 判否（``NO_LIDOUSHA_VOCAL_DETECTED``），旧代码只在
  整条交付门全过时才扶正歌名，于是 state 里只剩 BCUT 的错名。
"""

import hashlib
import json
from pathlib import Path

import pytest

import scripts.free_session_autoslice as runner
from src.autoslice import song_lane
from src.autoslice.song_delivery import SongDeliveryError, _commit_verified_song_package
from src.autoslice.song_name_authority import (
    authoritative_song_title,
    carry_song_identity_evidence,
    extract_audio_song_name_authority,
    ordered_song_lrc_queries,
    record_song_naming,
    song_name_hint_candidates,
)


XINXING_HOOK = "生日舞台高密度打call曲目《新型病毒》，从嘴硬“绝对不可能”唱到承认已经喜欢上你。"
XINXING_PREVIEW = "初次见面明明没有感觉 反而还厌倦你一场凡人嘴脸 你的问题我也只是默默点头当作敷衍"


def _audio_summary_record(*, song_title="心型病毒 (Live)"):
    """《心型病毒》全源趟的真实 summary 形状（音频对齐 + host-vocal 判否）。"""

    return {
        "candidate_id": "seededsong_120000_242040",
        "decision_action": "BLOCK",
        "reason_codes": ["SONG_NOT_LIDOUSHA_SINGING"],
        "source_context_job": {
            "song_candidate": True,
            "song_boundary": {
                "status": "FULL_SONG_READY",
                "source": "song_repair.netease",
                "song_title": song_title,
            },
            "lyrics_alignment": {
                "status": "READY",
                "provider": "netease",
                "model": "netease-agy-audio-lrc-global-shift-v1",
                "external_lrc": "netease://song/536937431",
                "matched_line_ratio": 1.0,
                "alignment_report_path": "/out/seededsong.lyrics-alignment-report.json",
                "alignment_report_sha256": "d80bdbd077963bbd0f123434790e658e71b5cb35d3e5fc17f8dbd4444fb3067a",
            },
        },
    }


def _text_only_summary_record():
    """窄窗那趟没有 audio aligner：同样写出 FULL_SONG_READY，但只是文本对齐。"""

    record = _audio_summary_record(song_title="新型病毒")
    record["source_context_job"]["lyrics_alignment"]["model"] = (
        "netease-lrc-global-shift-align-v2"
    )
    return record


# ---------------------------------------------------------------- (a) 权威


def test_audio_chain_is_the_naming_authority_with_auditable_provenance():
    authority = extract_audio_song_name_authority(_audio_summary_record())

    assert authority["song_title"] == "心型病毒 (Live)"
    assert authority["authority"] == "audio_lrc"
    assert authority["evidence_source"] == "agy_audio_lrc"
    assert authority["source_ref"] == "netease://song/536937431"
    assert authority["provider"] == "netease"
    assert authority["model"] == "netease-agy-audio-lrc-global-shift-v1"
    assert authority["matched_line_ratio"] == 1.0
    assert authority["alignment_report_sha256"].startswith("d80bdbd0")
    assert authoritative_song_title({"song_name_authority": authority}) == "心型病毒 (Live)"


def test_bcut_and_ocr_names_are_labelled_candidates_never_authority():
    candidates = song_name_hint_candidates(
        {"hook": XINXING_HOOK, "preview": XINXING_PREVIEW, "title_hint": "新型病毒"}
    )

    assert candidates == [
        {"song_title": "新型病毒", "source": "visual_song_ocr", "authority": False},
    ]
    assert all(item["authority"] is False for item in candidates)
    # 视觉 OCR 与 hook 引号标题恰好同名时合成一条；来源不同则各自留档。
    other = song_name_hint_candidates({"hook": XINXING_HOOK, "title_hint": "另一首"})
    assert [item["source"] for item in other] == ["visual_song_ocr", "asr_hook_quoted"]
    assert [item["song_title"] for item in other] == ["另一首", "新型病毒"]


def test_blocked_but_identified_row_still_records_the_audio_verified_name(tmp_path):
    """host-vocal 判否 ≠ 不知道是哪首歌：命名与授权解耦。"""

    selector_dir = tmp_path / "attempt"
    selector_dir.mkdir()
    (selector_dir / "summary.json").write_text(
        json.dumps({"records": [_audio_summary_record()]}, ensure_ascii=False),
        encoding="utf-8",
    )
    result = {"hook": XINXING_HOOK, "preview": XINXING_PREVIEW}
    song_lane._read_song_selector_summary(selector_dir=selector_dir, result=result)
    record_song_naming(result)

    assert result["song_name_authority"]["song_title"] == "心型病毒 (Live)"
    assert [item["song_title"] for item in result["song_title_candidates"]] == ["新型病毒"]
    assert result["song_title_candidates"][0]["authority"] is False


def test_authority_proved_in_the_full_source_pass_is_hoisted_not_buried():
    """窄窗趟没有 audio aligner，真名只可能在 _full 那趟出现。"""

    result = {
        "hook": XINXING_HOOK,
        "full_source_retry": {
            "status": "blocked",
            "song_name_authority": extract_audio_song_name_authority(
                _audio_summary_record()
            ),
        },
    }
    record_song_naming(result)

    assert result["song_name_authority"]["song_title"] == "心型病毒 (Live)"


def test_canonical_title_comes_from_the_audio_authority():
    result = {"title": "【李豆沙】豆沙歌，《新型病毒》"}

    song_lane._apply_canonical_song_title(
        result, summary_record=_audio_summary_record(), cid="song_210131_1210"
    )

    assert result["title"] == "【李豆沙】豆沙歌，《心型病毒 (Live)》"
    assert result["title_before_canonical_override"] == "【李豆沙】豆沙歌，《新型病毒》"


def test_real_xinxing_bingdu_flow_switches_naming_off_bcut(tmp_path, monkeypatch):
    """2026-08-08 song_210131_1210 真形状端到端。

    窄窗趟（无 ``--agy-audio-lrc-align``）只判定「是歌」；全源趟跑出音频对齐
    并证到《心型病毒 (Live)》，但 host-vocal 判否 → 整行 blocked。修复前这条
    记录只剩 BCUT 的《新型病毒》；修复后真名带出处留在 ``song_name_authority``，
    错名降级成 ``song_title_candidates``，而交付授权一点没放宽。
    """

    date, cid = "2026-08-08", "song_210131_1210"
    base = tmp_path / "autoslice"
    (base / "logs").mkdir(parents=True)
    out_dir = base / "out" / date / cid
    out_dir.mkdir(parents=True)
    monkeypatch.setattr(runner, "BASE", base)
    monkeypatch.setattr(runner, "REPO_ROOT", tmp_path / "repo")
    monkeypatch.setattr(runner, "child_env", lambda: {})
    monkeypatch.setattr(runner, "cpa_qa_cmd", lambda: "judge")

    segment = tmp_path / "segment.mp4"
    segment.write_bytes(b"segment")
    anchor_start, anchor_end, duration = 1_210_340, 1_332_380, 1_800_000
    full_start, full_end = runner.song_proof_retry_window(anchor_start, anchor_end, duration)
    runner.song_window_media_path(
        out_dir,
        cid,
        "",
        max(0, anchor_start - runner.SONG_WINDOW_PRE_MS),
        min(duration, anchor_end + runner.SONG_WINDOW_POST_MS),
    ).write_bytes(b"tight")
    runner.song_window_media_path(out_dir, cid, "_full", full_start, full_end).write_bytes(b"full")
    monkeypatch.setattr(
        runner,
        "slice_srt",
        lambda _s, _a, _b, destination: (
            destination.write_text("1\n00:00:00,000 --> 00:00:01,000\n歌词\n", encoding="utf-8"),
            1,
        )[1],
    )

    class Completed:
        returncode = 0

    commands = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        selector_dir = Path(command[command.index("--output-dir") + 1])
        if "--agy-audio-lrc-align" in command:
            record = _audio_summary_record()
        else:
            record = {
                "candidate_id": "semanticsong_tight",
                "decision_action": "BLOCK",
                "reason_codes": ["SONG_FULL_BOUNDARY_PROOF_MISSING"],
                "source_context_job": {"song_candidate": True},
            }
        (selector_dir / "summary.json").write_text(
            json.dumps({"records": [record]}, ensure_ascii=False), encoding="utf-8"
        )
        return Completed()

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    monkeypatch.setattr(runner, "record_is_song", lambda _record: True)

    result = runner.produce_song(
        date,
        {
            "cid": cid,
            "segment_path": str(segment),
            "seg_dur_ms": duration,
            "anchor_start_ms": anchor_start,
            "anchor_end_ms": anchor_end,
            "hook": XINXING_HOOK,
            "preview": XINXING_PREVIEW,
        },
    )

    # host-vocal 判否是有证据的确定性弃选；命名照记，授权照拒。
    assert result["status"] == "candidate_rejected"
    assert result["full_source_performer_rejection"] is True
    assert result.get("delivered") is None
    assert result["song_name_authority"]["song_title"] == "心型病毒 (Live)"
    assert result["song_name_authority"]["source_ref"] == "netease://song/536937431"
    assert result["song_name_authority"]["evidence_source"] == "agy_audio_lrc"
    assert [item["song_title"] for item in result["song_title_candidates"]] == ["新型病毒"]
    assert result["song_title_candidates"][0]["authority"] is False
    # 命名切换不给交付放行，也不改窄窗趟的检索顺序。
    assert "title" not in result
    assert _lrc_queries(commands[0]) == ["新型病毒", XINXING_PREVIEW]


# ------------------------------------------------- (b) 音频没证成 = 没有权威名


def test_text_only_alignment_never_mints_a_naming_authority():
    """纯文本 LRC 对齐归根到底还是 BCUT 中文 ASR 的匹配结果。"""

    assert extract_audio_song_name_authority(_text_only_summary_record()) is None
    assert extract_audio_song_name_authority({}) is None
    assert (
        extract_audio_song_name_authority(
            {"source_context_job": {"song_boundary": {"song_title": "新型病毒"}}}
        )
        is None
    )


def test_without_audio_proof_there_is_no_title_and_no_fallback_to_the_hint():
    result = {"hook": XINXING_HOOK, "title_hint": "新型病毒"}

    song_lane._apply_canonical_song_title(
        result, summary_record=_text_only_summary_record(), cid="song_210131_1210"
    )
    record_song_naming(result)

    assert "title" not in result
    assert result["song_name_authority"] is None
    # 垃圾名仍然完整留档，但只以 candidate 身份存在。
    assert [item["song_title"] for item in result["song_title_candidates"]] == ["新型病毒"]


def test_a_nameless_song_package_is_refused_rather_than_named_from_the_hook():
    """交付槽的 sink：没有权威名就拿不到名字，也就交付不了。"""

    with pytest.raises(SongDeliveryError, match="has no title"):
        _commit_verified_song_package(
            date="2026-08-08",
            delivery_candidate_id="song_210131_1210",
            summary_record=_audio_summary_record(),
            title="",
            selector_rc=0,
            summary_authority_root=Path("/nonexistent"),
        )


def test_song_lane_delivery_title_no_longer_falls_back_to_the_hook(tmp_path, monkeypatch):
    """produce_song 的交付调用点只递 result['title']，不再 or 到 item['hook']。"""

    captured = {}

    def fake_commit(**kwargs):
        captured.update(kwargs)
        raise SongDeliveryError("stop after capturing the title")

    burned = tmp_path / "burned.mp4"
    burned.write_bytes(b"burned")
    burned_sha = "sha256:" + hashlib.sha256(burned.read_bytes()).hexdigest()

    date, cid = "2026-08-08", "song_210131_1210"
    base = tmp_path / "autoslice"
    (base / "logs").mkdir(parents=True)
    (base / "out" / date / cid).mkdir(parents=True)
    monkeypatch.setattr(runner, "BASE", base)
    monkeypatch.setattr(runner, "REPO_ROOT", tmp_path / "repo")
    monkeypatch.setattr(runner, "child_env", lambda: {})
    monkeypatch.setattr(runner, "cpa_qa_cmd", lambda: "judge")
    monkeypatch.setattr(runner, "_commit_verified_song_package", fake_commit)
    monkeypatch.setattr(runner, "song_delivery_ok", lambda *_a, **_k: True)
    monkeypatch.setattr(
        runner,
        "song_completion_evidence",
        lambda _record: {"ready": True, "reason_codes": [], "lyrics_alignment_status": "READY"},
    )
    monkeypatch.setattr(
        runner,
        "song_delivery_artifacts",
        lambda _record: {"video_path": str(burned), "video_sha256": burned_sha},
    )
    monkeypatch.setattr(
        runner, "_song_delivery_recovery_authority", lambda **_kwargs: None
    )
    monkeypatch.setattr(runner, "published_song_match", lambda _title: None)
    segment = tmp_path / "segment.mp4"
    segment.write_bytes(b"segment")
    anchor_start, anchor_end, duration = 50_000, 100_000, 200_000
    runner.song_window_media_path(
        base / "out" / date / cid,
        cid,
        "",
        max(0, anchor_start - runner.SONG_WINDOW_PRE_MS),
        min(duration, anchor_end + runner.SONG_WINDOW_POST_MS),
    ).write_bytes(b"tight")
    monkeypatch.setattr(
        runner,
        "slice_srt",
        lambda _s, _a, _b, destination: (
            destination.write_text("1\n00:00:00,000 --> 00:00:01,000\n歌词\n", encoding="utf-8"),
            1,
        )[1],
    )

    class Completed:
        returncode = 0

    def fake_run(command, **_kwargs):
        selector_dir = Path(command[command.index("--output-dir") + 1])
        # 音频对齐缺席（窄窗趟本来就没有），所以没有任何权威名。
        (selector_dir / "summary.json").write_text(
            json.dumps({"records": [_text_only_summary_record()]}, ensure_ascii=False),
            encoding="utf-8",
        )
        return Completed()

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    monkeypatch.setattr(runner, "record_is_song", lambda _record: True)

    result = runner.produce_song(
        date,
        {
            "cid": cid,
            "segment_path": str(segment),
            "seg_dur_ms": duration,
            "anchor_start_ms": anchor_start,
            "anchor_end_ms": anchor_end,
            "hook": XINXING_HOOK,
            "preview": XINXING_PREVIEW,
        },
    )

    assert captured["title"] == ""
    assert XINXING_HOOK not in captured["title"]
    assert result["song_name_authority"] is None


# ------------------------------------------------------- (c) 检索不再被污染


def _lrc_queries(command):
    return [
        command[index + 1]
        for index, value in enumerate(command)
        if value == "--song-lrc-query"
    ]


def _selector_command(item):
    return song_lane._build_song_selector_command(
        item=item,
        window_mp4=Path("/w.mp4"),
        window_srt=Path("/w.srt"),
        selector_dir=Path("/sel"),
        start=0,
        end=1_000,
        anchor_start=0,
        anchor_end=1_000,
        tag="_full",
    )


@pytest.fixture
def selector_runner_stubs(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(runner, "cpa_qa_cmd", lambda: "judge")
    monkeypatch.setattr(runner, "profile_asset_file", lambda _name: tmp_path / "asset.json")


def test_verified_name_replaces_the_homophone_in_the_lrc_query_set(selector_runner_stubs):
    item = {
        "hook": XINXING_HOOK,
        "preview": XINXING_PREVIEW,
        "title_hint": "新型病毒",
        "song_name_authority": extract_audio_song_name_authority(_audio_summary_record()),
    }

    queries = _lrc_queries(_selector_command(item))

    assert queries[0] == "心型病毒 (Live)"
    assert "新型病毒" not in queries


def test_first_pass_query_order_is_unchanged_without_an_authority(selector_runner_stubs):
    """没有权威名时召回行为一字不改（视觉 hint → 引号标题 → 演唱 ASR 文本）。"""

    queries = _lrc_queries(
        _selector_command(
            {"hook": XINXING_HOOK, "preview": XINXING_PREVIEW, "title_hint": "画面歌名"}
        )
    )

    assert queries == ["画面歌名", "新型病毒", XINXING_PREVIEW]


def test_ordered_queries_ignore_a_non_audio_authority_shaped_field():
    assert ordered_song_lrc_queries(
        authority_title=authoritative_song_title(
            {"song_name_authority": {"authority": "ocr", "song_title": "新型病毒"}}
        ),
        visual_title_hint="画面歌名",
        quoted_titles=["新型病毒"],
        known_song_query="演唱 ASR",
    ) == ["画面歌名", "新型病毒", "演唱 ASR"]


def test_carried_authority_survives_a_retry_that_produced_no_fresh_audio_evidence(
    tmp_path, monkeypatch
):
    """一次 infra 失败不得把已证真名清回 None。

    重试链在本仓是常态（provider outage / AGY quota / 窗口切失败）。若某轮跑
    不到音频证据就把 ``song_name_authority`` 抹掉，state 行会被这轮结果整体
    替换，下一轮 requeue 就又带着 BCUT 的《新型病毒》去检索——修复等于只生效
    一轮。
    """

    date, cid = "2026-08-09", "song_210131_1210"
    base = tmp_path / "autoslice"
    (base / "logs").mkdir(parents=True)
    out_dir = base / "out" / date / cid
    out_dir.mkdir(parents=True)
    monkeypatch.setattr(runner, "BASE", base)
    monkeypatch.setattr(runner, "REPO_ROOT", tmp_path / "repo")
    monkeypatch.setattr(runner, "child_env", lambda: {})
    monkeypatch.setattr(runner, "cpa_qa_cmd", lambda: "judge")

    segment = tmp_path / "segment.mp4"
    segment.write_bytes(b"segment")
    anchor_start, anchor_end, duration = 50_000, 100_000, 200_000
    runner.song_window_media_path(
        out_dir,
        cid,
        "",
        max(0, anchor_start - runner.SONG_WINDOW_PRE_MS),
        min(duration, anchor_end + runner.SONG_WINDOW_POST_MS),
    ).write_bytes(b"tight")
    monkeypatch.setattr(
        runner,
        "slice_srt",
        lambda _s, _a, _b, destination: (
            destination.write_text("1\n00:00:00,000 --> 00:00:01,000\n歌词\n", encoding="utf-8"),
            1,
        )[1],
    )

    class Completed:
        returncode = 0

    commands = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        selector_dir = Path(command[command.index("--output-dir") + 1])
        (selector_dir / "summary.json").write_text(
            json.dumps(
                {
                    "records": [
                        {
                            "candidate_id": "seededsong_retry",
                            "decision_action": "BLOCK",
                            "reason_codes": ["AGY_QUOTA_EXHAUSTED"],
                            "source_context_job": {"song_candidate": True},
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        return Completed()

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    monkeypatch.setattr(runner, "record_is_song", lambda _record: False)

    authority = extract_audio_song_name_authority(_audio_summary_record())
    result = runner.produce_song(
        date,
        {
            "cid": cid,
            "segment_path": str(segment),
            "seg_dur_ms": duration,
            "anchor_start_ms": anchor_start,
            "anchor_end_ms": anchor_end,
            "hook": XINXING_HOOK,
            "preview": XINXING_PREVIEW,
            **carry_song_identity_evidence({"song_name_authority": authority}),
        },
    )

    assert _lrc_queries(commands[0])[0] == "心型病毒 (Live)"
    assert "新型病毒" not in _lrc_queries(commands[0])
    assert result["song_name_authority"] == authority
    assert carry_song_identity_evidence(result)["song_name_authority"] == authority


def test_retry_carries_the_audio_authority_so_the_bad_name_never_returns():
    authority = extract_audio_song_name_authority(_audio_summary_record())
    carried = carry_song_identity_evidence(
        {
            "title_hint": "新型病毒",
            "visual_song_evidence": {"song_title": "新型病毒"},
            "song_name_authority": authority,
        }
    )

    assert carried["song_name_authority"] == authority
    assert carried["title_hint"] == "新型病毒"
    assert authoritative_song_title(carried) == "心型病毒 (Live)"
