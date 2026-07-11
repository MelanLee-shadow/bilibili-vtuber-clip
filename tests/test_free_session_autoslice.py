"""Unit tests for the unattended runner's pure helpers (first-real-run lessons)."""
import json
import hashlib
from pathlib import Path

import pytest

import scripts.free_session_autoslice as runner
from tests.host_vocal_test_support import bind_ready_live_performance_report, make_ready_host_vocal_claim
from scripts.free_session_autoslice import (
    COVER_REPAIR_MAX_ATTEMPTS,
    MAX_SONGS_PER_DATE,
    MAX_TALK_PICKS,
    cover_repair_needed,
    delivered_paths,
    last_json_block,
    prioritize,
    safe_name,
)


def test_last_json_block_parses_nested_produce_summary():
    """The produce log summary contains nested objects — a non-greedy regex
    failed on it (first run's summary showed 标题=(见log) everywhere)."""
    tail = 'noise\n{"a": 1}\nmore\n{"candidate_id": "x", "timing_qa": {"dropped": 0, "retimed": 3}, "title": "真标题"}\n'
    obj = last_json_block(tail)
    assert obj["title"] == "真标题"
    assert obj["timing_qa"]["retimed"] == 3


def test_last_json_block_empty_on_garbage():
    assert last_json_block("no json here }{ broken") == {}


def test_song_selector_env_pins_term_context_to_recording_date(monkeypatch):
    monkeypatch.setattr(runner, "child_env", lambda: {"BASE": "kept"})

    env = runner.song_selector_env("2026-07-10")

    assert env["BASE"] == "kept"
    assert env["LIDOUSHA_TERM_AS_OF"] == "2026-07-10"


def test_safe_name_sanitizes_and_falls_back():
    assert safe_name("百合是工作？/她当场*不买书", "cid") == "百合是工作？她当场不买书"
    assert safe_name("", "cid") == "cid"
    assert len(safe_name("很长" * 40, "cid")) <= 18


def test_prioritize_caps_talk_and_songs_with_reasons():
    state = {
        "picks": [], "songs": [],
        "pending_talk": [
            {"segment_path": f"/rec/talkseg{i % 2}.mp4", "start_ms": i * 1000, "end_ms": i * 1000 + 30_000,
             "hook": f"hook{i}", "confidence": 0.9, "cid": f"auto_{i}"}
            for i in range(MAX_TALK_PICKS + 3)
        ],
        "pending_song": [
            {"segment_path": "/rec/seg0.mp4", "anchor_start_ms": i * 10_000, "anchor_end_ms": i * 10_000 + 60_000,
             "danmaku": i * 10, "cid": f"song_{i}", "hook": "", "preview": ""}
            for i in range(MAX_SONGS_PER_DATE + 2)
        ],
    }
    prioritize(state)
    assert len(state["pending_talk"]) == MAX_TALK_PICKS
    assert len(state["not_selected"]) == 3
    assert len(state["pending_song"]) == MAX_SONGS_PER_DATE
    # 弹幕最高的两个被保留（降序）
    assert [s["danmaku"] for s in state["pending_song"]] == [30, 20]
    # backlog 保留结构化条目（门拦截后回填的来源），不再是只读字符串
    assert len(state["song_backlog"]) == 2
    assert all(isinstance(b, dict) for b in state["song_backlog"])


def _delivered_talk_pick(tmp_path, monkeypatch, *, with_cover: bool) -> dict:
    """A delivered ok talk pick in a fake repo delivery dir (2026-07-06 real
    case: CPA image gateway 400 blanked covers while the mp4/title were fine)."""
    monkeypatch.setattr(runner, "REPO_ROOT", tmp_path)
    rec = {"candidate_id": "auto_1_2_3", "status": "ok", "hook": "钩子",
           "title": "【李豆沙】标题", "cover_status": "BLOCKED_AI_COVER_REQUIRED"}
    delivery = tmp_path / "lidousha" / "2026-07-06"
    delivery.mkdir(parents=True)
    (delivery / f"{safe_name('钩子', 'auto_1_2_3')}.mp4").write_bytes(b"mp4")
    if with_cover:
        (delivery / f"{safe_name('钩子', 'auto_1_2_3')}.cover.png").write_bytes(b"png")
    return rec


def test_cover_repair_needed_for_delivered_pick_without_cover(tmp_path, monkeypatch):
    rec = _delivered_talk_pick(tmp_path, monkeypatch, with_cover=False)
    assert cover_repair_needed("2026-07-06", rec)
    mp4, cover = delivered_paths("2026-07-06", rec)
    assert mp4.is_file() and cover.name.endswith(".cover.png")


def test_cover_repair_not_needed_when_cover_exists(tmp_path, monkeypatch):
    rec = _delivered_talk_pick(tmp_path, monkeypatch, with_cover=True)
    assert not cover_repair_needed("2026-07-06", rec)


def test_cover_repair_bounded_and_skips_undelivered(tmp_path, monkeypatch):
    rec = _delivered_talk_pick(tmp_path, monkeypatch, with_cover=False)
    rec["cover_repair_attempts"] = COVER_REPAIR_MAX_ATTEMPTS
    assert not cover_repair_needed("2026-07-06", rec)
    # title_failed/failed picks were never delivered → nothing to repair
    assert not cover_repair_needed("2026-07-06", {"candidate_id": "auto_9", "status": "failed", "title": "x"})
    # a pick without a title must never get a cover (title is part of the product)
    ok_no_title = _delivered_talk_pick(tmp_path / "b", monkeypatch, with_cover=False)
    ok_no_title["title"] = None
    assert not cover_repair_needed("2026-07-06", ok_no_title)


def _write_srt(path, cues):
    """cues: list of (start_ms, end_ms, text) → an SRT file."""
    def ts(ms):
        h, ms = divmod(ms, 3600_000)
        m, ms = divmod(ms, 60_000)
        s, ms = divmod(ms, 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
    blocks = [f"{i+1}\n{ts(s)} --> {ts(e)}\n{t}" for i, (s, e, t) in enumerate(cues)]
    path.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")


def test_song_core_span_trims_leading_talk_and_outro(tmp_path):
    """Anchor-bleed guard (Ivan 2026-07-06 《屑屑》): the recall song-anchor began
    ~50s inside the preceding talk.  _song_core_span uses the music-intro/outro
    speech gaps to trim to the sung core — and must NOT split at a mid-song
    instrumental interlude."""
    srt = tmp_path / "seg.bcut.srt"
    cues = []
    cues += [(t, t + 2000, "闲聊") for t in range(0, 30_000, 3000)]        # 0-30s talk (dense)
    # 30-50s music intro (GAP, no cues)
    cues += [(t, t + 2500, "歌词") for t in range(50_000, 138_000, 4000)]  # 50-138s verse
    # 138-158s instrumental interlude (GAP, mid-song — must be ignored)
    cues += [(t, t + 2500, "歌词") for t in range(158_000, 230_000, 4000)] # 158-230s
    # 230-250s outro gap
    cues += [(t, t + 2000, "谢谢大家") for t in range(250_000, 260_000, 3000)]
    _write_srt(srt, cues)

    lo, hi = runner._song_core_span(srt, 0, 260_000)
    assert 46_000 <= lo <= 54_000, f"lead not trimmed to the song start: {lo}"
    # tail trims to the song end at the LARGE outro gap (230-250s), NOT the equal
    # mid-song interlude at 138-158s (which is outside the last 12% tail zone and
    # must never move the end — clipping mid-song → SONG_PARTIAL).
    assert 226_000 <= hi <= 236_000, f"tail should trim to the outro gap, got {hi}"
    # a clean anchor with no big edge gaps is returned unchanged
    tight = tmp_path / "tight.bcut.srt"
    _write_srt(tight, [(t, t + 2500, "歌词") for t in range(0, 200_000, 4000)])
    assert runner._song_core_span(tight, 0, 200_000) == (0, 200_000)
    # missing file / too-short span → unchanged (no false trim)
    assert runner._song_core_span(tmp_path / "nope.srt", 0, 260_000) == (0, 260_000)
    assert runner._song_core_span(srt, 0, 60_000) == (0, 60_000)


def test_image_lane_down_signatures():
    """通道断供(运营侧,自愈)不烧有限重试次数；真实 4xx/叠字失败才计数。"""
    assert runner.image_lane_down("BLOCKED_AI_COVER_REQUIRED: ... 分组 Codex-Plus 下模型 gpt-image-2 的可用渠道不存在（retry）")
    assert runner.image_lane_down("CPA_IMAGE_EDIT_HTTP_ERROR HTTP 500: upstream")
    assert not runner.image_lane_down('HTTP 400: {"error":{"message":"width and height must be positive multiples of 16"}}')
    assert not runner.image_lane_down("COVER_REFERENCE_EXTRACTION_FAILED: boom")


def test_cover_ref_prefers_clean_producer_frame(tmp_path, monkeypatch):
    """The delivered mp4 has burned subtitles — the identity reference must be
    the producer's clean cover-ref frame whenever it survives on disk."""
    monkeypatch.setattr(runner, "BASE", tmp_path)
    assert runner.cover_ref_for("2026-07-06", "auto_1") is None
    refs = tmp_path / "out" / "2026-07-06" / "auto_1" / "replacement_recuts" / "cover_refs"
    refs.mkdir(parents=True)
    (refs / "auto_1.cover-ref.png").write_bytes(b"png")
    assert runner.cover_ref_for("2026-07-06", "auto_1").name == "auto_1.cover-ref.png"


def test_cover_repair_needed_for_delivered_song(tmp_path, monkeypatch):
    """Ivan 2026-07-10：交付了的歌就该有封面——语义判定(BLOCK/reason codes)不再
    锁死补封面；没交付的(blocked)自然没有 delivered，永远不补。"""
    monkeypatch.setattr(runner, "REPO_ROOT", tmp_path)
    delivery = tmp_path / "lidousha" / "2026-07-06"
    delivery.mkdir(parents=True)
    (delivery / "歌切_嘉宾.mp4").write_bytes(b"mp4")
    rec = {"candidate_id": "song_1", "status": "review_ready", "title": "【李豆沙】《嘉宾》",
           "delivered": str(delivery / "歌切_嘉宾.mp4"), "decision": "BLOCK",
           "reason_codes": ["ADVISORY_NO_NATURAL_CLOSURE"]}
    assert cover_repair_needed("2026-07-06", rec)  # 交付但语义BLOCK → 照样补封面
    (delivery / "歌切_嘉宾.cover.png").write_bytes(b"png")
    assert not cover_repair_needed("2026-07-06", rec)
    # 未交付(门拦)的歌永远不补封面
    assert not cover_repair_needed("2026-07-06", {"candidate_id": "song_2", "status": "blocked",
                                                  "title": "x", "decision": "BLOCK"})


def test_produce_batch_preserves_order_and_isolates_crashes():
    """Parallel batch (Ivan 2026-07-07 提速): results come back in input order and
    one crashing slice becomes a failed result instead of killing the batch."""
    items = [{"cid": f"c{i}", "hook": f"h{i}"} for i in range(5)]

    def fake_produce(date, item):
        if item["cid"] == "c2":
            raise RuntimeError("boom")
        return {"candidate_id": item["cid"], "status": "ok"}

    results = runner.produce_batch("2026-07-06", items, fake_produce)
    assert [r["candidate_id"] for r in results] == ["c0", "c1", "c2", "c3", "c4"]  # order preserved
    assert results[2]["status"] == "failed" and "boom" in results[2]["error"]  # crash isolated
    assert all(r["status"] == "ok" for i, r in enumerate(results) if i != 2)
    assert runner.produce_batch("2026-07-06", [], fake_produce) == []

# ---------------------------------------------------------------------------
# runner v4 (2026-07-09 external audit: "the control plane was lying")
# ---------------------------------------------------------------------------


def test_prioritize_global_confidence_ranking_beats_arrival_order():
    """7/9 real failure: a conf=0.94 candidate lost to five earlier 0.85-0.90
    because selection was segment round-robin.  Ranking is now GLOBAL."""
    state = {
        "picks": [], "songs": [], "pending_song": [],
        "pending_talk": [
            {"segment_path": "/rec/early.mp4", "start_ms": i * 1000, "end_ms": i * 1000 + 9000,
             "hook": f"e{i}", "confidence": 0.86, "cid": f"e{i}"}
            for i in range(5)
        ] + [
            {"segment_path": "/rec/late.mp4", "start_ms": 0, "end_ms": 9000,
             "hook": "拜早年联欢晚会", "confidence": 0.94, "cid": "late"},
        ],
    }
    prioritize(state)
    kept = {i["cid"] for i in state["pending_talk"]}
    assert "late" in kept, "最高分候选必须赢，不看到达顺序"
    assert len(state["pending_talk"]) == MAX_TALK_PICKS
    assert "全局排序" in state["not_selected"][0]


def test_prioritize_diversity_cap_is_soft():
    """One segment with 6 candidates and nothing else: the per-segment cap must
    yield rather than deliver fewer than the quota."""
    state = {
        "picks": [], "songs": [], "pending_song": [],
        "pending_talk": [
            {"segment_path": "/rec/one.mp4", "start_ms": i, "end_ms": i + 9,
             "hook": str(i), "confidence": 0.9 - i * 0.01, "cid": str(i)}
            for i in range(6)
        ],
    }
    prioritize(state)
    assert len(state["pending_talk"]) == MAX_TALK_PICKS


def test_prioritize_blocks_all_talk_shapes_overlapping_song_interval():
    """A blocked/background song cannot be laundered as a sibling talk cut."""

    state = {
        "picks": [],
        "songs": [],
        "pending_song": [
            {
                "segment_path": "/rec/session.mp4",
                "anchor_start_ms": 100_000,
                "anchor_end_ms": 160_000,
                "danmaku": 9,
                "cid": "song_background",
            }
        ],
        "pending_talk": [
            {"segment_path": "/rec/session.mp4", "start_ms": 98_000, "end_ms": 164_000, "cid": "parent"},
            {"segment_path": "/rec/session.mp4", "start_ms": 110_000, "end_ms": 120_000, "cid": "child"},
            {"segment_path": "/rec/session.mp4", "start_ms": 159_999, "end_ms": 170_000, "cid": "tail"},
            {"segment_path": "/rec/session.mp4", "start_ms": 160_000, "end_ms": 170_000, "cid": "adjacent"},
            {"segment_path": "/rec/session.mp4", "start_ms": 205_000, "end_ms": 215_000, "cid": "outside"},
            {"segment_path": "/rec/other.mp4", "start_ms": 110_000, "end_ms": 120_000, "cid": "other"},
        ],
    }

    prioritize(state)

    assert {item["cid"] for item in state["pending_talk"]} == {"outside", "other"}
    assert {item["candidate_id"] for item in state["song_overlap_blocked_talk"]} == {
        "parent",
        "child",
        "tail",
        "adjacent",
    }
    assert state["song_quarantine_intervals"] == [
        {
            "segment_path": "/rec/session.mp4",
            "start_ms": 55_000,
            "end_ms": 205_000,
            "original_anchor_start_ms": 100_000,
            "original_anchor_end_ms": 160_000,
            "candidate_id": "song_background",
            "reason_code": "SONG_INTERVAL_REQUIRES_JOINT_SINGING_PROOF",
        }
    ]

    # The taint is durable even after the song leaves pending/backlog.
    state["pending_song"] = []
    state["song_backlog"] = []
    state["pending_talk"] = [
        {"segment_path": "/rec/session.mp4", "start_ms": 120_000, "end_ms": 130_000, "cid": "late_clone"}
    ]
    prioritize(state)
    assert state["pending_talk"] == []
    assert state["song_overlap_blocked_talk"][-1]["candidate_id"] == "late_clone"


def test_song_status_words():
    """7/9 audit P0: gate BLOCK was recorded as ok because the subprocess exited 0."""
    assert runner.song_status(1, False) == "failed"
    assert runner.song_status(0, True) == "review_ready"
    assert runner.song_status(0, False) == "blocked"


def test_song_delivery_artifacts_extraction_with_hashes():
    record = {
        "decision_action": "BLOCK",  # semantic decision must NOT gate extraction
        "reason_codes": ["CPA_SEMANTIC_INCOMPLETE", "ADVISORY_NO_NATURAL_CLOSURE"],
        "title": "fallback",
        "materialized_recut": {
            "cover_release_gate": {
                "satisfied": True,
                "path": "/current/gate.json",
                "artifact_hashes": {"burned_video_sha256": "sha256:" + "a" * 64},
            },
            "burned_preview": {"status": "BURNED", "path": "/current/video.mp4"},
            "artifact_hashes": {"cover_sha256": "sha256:" + "b" * 64},
            "publish_staging": {
                "status": "STAGED",
                "title": "【李豆沙】本次歌切",
                "cover_path": "/current/cover.png",
            },
        },
    }
    assert runner.song_delivery_artifacts(record) == {
        "video_path": "/current/video.mp4",
        "video_sha256": "sha256:" + "a" * 64,
        "cover_path": "/current/cover.png",
        "cover_sha256": "sha256:" + "b" * 64,
        "title": "【李豆沙】本次歌切",
        "release_gate_path": "/current/gate.json",
        "cover_release_gate_satisfied": True,
    }


def test_song_delivery_artifacts_video_survives_unsatisfied_cover_gate():
    """Cover gate unsatisfied = 还没出封面，不等于不是歌：video 物料必须照常
    可提取（封面由 repair_covers 事后补）。"""
    record = {
        "decision_action": "AUTO_UPLOAD",
        "reason_codes": [],
        "materialized_recut": {
            "cover_release_gate": {"satisfied": False},
            "burned_preview": {"status": "BURNED", "path": "/current/video.mp4"},
            "publish_staging": {"status": "STAGED", "cover_path": "/pending/cover.png"},
        },
    }
    artifacts = runner.song_delivery_artifacts(record)
    assert artifacts["video_path"] == "/current/video.mp4"
    assert artifacts["cover_release_gate_satisfied"] is False
    assert runner.song_delivery_artifacts({}) == {}


def test_song_delivery_rule_final_requires_positive_completion_proof(tmp_path):
    """Ivan 2026-07-10 最终规则：至多2个按弹幕排序（prioritize/refill 管）；
    是歌+唱完整就交付；语义判定只是参考。没唱完整(SONG_PARTIAL)不强行切。"""
    semantic_noise = ["END_BOUNDARY_LOW", "CPA_SEMANTIC_INCOMPLETE",
                      "VIEWER_CONTEXT_INCOMPLETE", "ADVISORY_NO_NATURAL_CLOSURE"]
    proof_path = tmp_path / "host-vocal-proof.json"
    proof_path.write_text("{}", encoding="utf-8")
    proof = {
        "ready": True,
        "host_vocal_status": "READY",
        "host_vocal_decision": "LIDOUSHA_VOCAL_PRESENT_ON_LYRIC_CHECKPOINTS",
        "live_performance_status": "READY",
        "live_performance_mode": "LIVE_STREAMER_SINGING",
        "joint_singing_decision": "VERIFIED_LIDOUSHA_SINGING",
        "host_vocal_proof_path": str(proof_path),
        "host_vocal_proof_sha256": hashlib.sha256(proof_path.read_bytes()).hexdigest(),
    }
    assert runner.song_delivery_ok(0, True, semantic_noise, proof) is True   # 语义码不拦
    assert runner.song_delivery_ok(0, True, [], True) is False  # bool cannot carry performer/hash proof
    assert runner.song_delivery_ok(0, True, semantic_noise + ["SONG_PARTIAL"], proof) is False  # 没唱完整不切
    assert runner.song_delivery_ok(0, False, [], proof) is False             # 不是歌不切
    assert runner.song_delivery_ok(0, True, None, None) is False             # 没有正向证据必须失败关闭
    assert runner.song_delivery_ok(1, True, [], proof) is False               # 当前 selector 失败不得交付
    assert runner.song_delivery_ok(0, True, ["SONG_NOT_LIDOUSHA_SINGING"], proof) is False
    assert runner.song_delivery_ok(0, True, ["SONG_HOST_VOCAL_PROOF_INVALID"], proof) is False
    proof["host_vocal_status"] = "BLOCKED"
    assert runner.song_delivery_ok(0, True, [], proof) is False


def test_failed_song_selector_cannot_reuse_stale_summary_or_deliver(tmp_path, monkeypatch):
    """P1 regression: a previous valid selector summary used to survive in the
    fixed output directory.  When the next selector crashed, produce_song read
    that stale proof and copied the old video as this invocation's delivery.

    The old attempt remains on disk for forensics, while the failing current
    subprocess gets a distinct empty output directory.  Its own diagnostic
    summary is read, but its non-zero return code still prevents delivery.
    """
    date = "2026-07-09"
    cid = "song_stale_regression"
    base = tmp_path / "autoslice"
    repo = tmp_path / "repo"
    out_dir = base / "out" / date / cid
    logs_dir = base / "logs"
    out_dir.mkdir(parents=True)
    logs_dir.mkdir(parents=True)
    repo.mkdir()
    monkeypatch.setattr(runner, "BASE", base)
    monkeypatch.setattr(runner, "REPO_ROOT", repo)
    monkeypatch.setattr(runner, "child_env", lambda: {})
    monkeypatch.setattr(runner, "cpa_qa_cmd", lambda: "judge")

    segment = tmp_path / "segment.mp4"
    segment.write_bytes(b"source")
    anchor_start, anchor_end, duration = 50_000, 100_000, 200_000
    window_start = anchor_start - runner.SONG_WINDOW_PRE_MS
    window_end = anchor_end + runner.SONG_WINDOW_POST_MS
    (out_dir / f"{cid}_source.mp4").write_bytes(b"current-window")

    stale_video = tmp_path / "stale.burned.mp4"
    stale_video.write_bytes(b"stale-but-valid")
    stale_sha = "sha256:" + hashlib.sha256(stale_video.read_bytes()).hexdigest()
    stale_dir = out_dir / "song_selector"
    stale_dir.mkdir()
    stale_summary = {
        "records": [{
            "candidate_id": "semanticsong_stale",
            "decision_action": "AUTO_UPLOAD",
            "reason_codes": [],
        }]
    }
    stale_summary_path = stale_dir / "summary.json"
    stale_summary_path.write_text(json.dumps(stale_summary), encoding="utf-8")

    def fake_slice_srt(_source, _start, _end, destination):
        destination.write_text("1\n00:00:00,000 --> 00:00:01,000\n歌词\n", encoding="utf-8")
        return 1

    monkeypatch.setattr(runner, "slice_srt", fake_slice_srt)
    monkeypatch.setattr(runner, "_song_core_span", lambda *_args: (anchor_start, anchor_end))

    def fake_completion(record):
        ready = bool(record)
        return {
            "ready": ready,
            "reason_codes": [] if ready else ["SONG_FULL_BOUNDARY_PROOF_MISSING"],
            "lyrics_alignment_status": "READY" if ready else None,
            "alignment_report_path": None,
            "alignment_report_sha256": None,
        }

    monkeypatch.setattr(runner, "song_completion_evidence", fake_completion)
    monkeypatch.setattr(
        runner,
        "song_delivery_artifacts",
        lambda record: {
            "video_path": str(stale_video),
            "video_sha256": stale_sha,
            "title": "【李豆沙】本次诊断结果" if record.get("candidate_id") == "semanticsong_current" else "【李豆沙】旧歌切",
        } if record else {},
    )

    selector_dirs = []

    class FailedSelector:
        returncode = 1

    def fake_run(command, **_kwargs):
        selector_dir = Path(command[command.index("--output-dir") + 1])
        selector_dirs.append(selector_dir)
        (selector_dir / "summary.json").write_text(json.dumps({
            "records": [{
                "candidate_id": "semanticsong_current",
                "decision_action": "AUTO_UPLOAD",
                "reason_codes": [],
            }]
        }), encoding="utf-8")
        return FailedSelector()

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    result = runner.produce_song(date, {
        "cid": cid,
        "segment_path": str(segment),
        "seg_dur_ms": duration,
        "anchor_start_ms": anchor_start,
        "anchor_end_ms": anchor_end,
        "danmaku": 18,
        "hook": "旧 summary 不能冒充本次结果",
        "preview": "",
    })

    assert result["start_ms"] == window_start and result["end_ms"] == window_end
    assert result["rc"] == 1 and result["status"] == "failed"
    assert result["window_classified_song"] is True
    assert result["title"] == "【李豆沙】本次诊断结果", "只能读本次新 summary，不能读旧标题"
    assert "delivered" not in result, "即使失败进程写了完整 summary，rc != 0 也不得交付"
    assert len(selector_dirs) == 1
    assert selector_dirs[0].parent == stale_dir and selector_dirs[0] != stale_dir
    assert (selector_dirs[0] / "summary.json").is_file()
    assert stale_summary_path.is_file(), "旧 attempt 只隔离保留，不做破坏性删除"
    assert json.loads(stale_summary_path.read_text(encoding="utf-8")) == stale_summary
    assert not list((repo / "lidousha" / date).glob("*.mp4"))


def test_full_song_proof_retry_seeds_original_anchor_and_enables_audio_lrc(tmp_path, monkeypatch):
    date = "2026-07-09"
    cid = "song_seed_retry"
    base = tmp_path / "autoslice"
    repo = tmp_path / "repo"
    out_dir = base / "out" / date / cid
    (base / "logs").mkdir(parents=True)
    out_dir.mkdir(parents=True)
    repo.mkdir()
    monkeypatch.setattr(runner, "BASE", base)
    monkeypatch.setattr(runner, "REPO_ROOT", repo)
    monkeypatch.setattr(runner, "child_env", lambda: {})
    monkeypatch.setattr(runner, "cpa_qa_cmd", lambda: "judge")

    segment = tmp_path / "segment.mp4"
    segment.write_bytes(b"segment")
    anchor_start, anchor_end, duration = 50_000, 100_000, 200_000
    (out_dir / f"{cid}_source.mp4").write_bytes(b"tight")
    (out_dir / f"{cid}_full_source.mp4").write_bytes(b"full")

    def fake_slice_srt(_source, _start, _end, destination):
        destination.write_text("1\n00:00:00,000 --> 00:00:01,000\n歌词\n", encoding="utf-8")
        return 1

    monkeypatch.setattr(runner, "slice_srt", fake_slice_srt)
    selector_commands = []

    class Completed:
        returncode = 0

    def fake_run(command, **_kwargs):
        selector_commands.append(command)
        selector_dir = Path(command[command.index("--output-dir") + 1])
        seeded = "--seed-song-candidate-id" in command
        full_source = "--agy-audio-lrc-align" in command
        (selector_dir / "summary.json").write_text(
            json.dumps(
                {
                    "records": [
                        {
                            "candidate_id": "seededsong_45000_95000" if seeded else "semanticsong_15000_65000",
                            "decision_action": "BLOCK",
                            "reason_codes": (
                                ["SONG_BACKGROUND_PLAYBACK_ONLY", "SONG_NOT_LIDOUSHA_SINGING"]
                                if full_source
                                else ["SONG_FULL_BOUNDARY_PROOF_MISSING"]
                            ),
                            "source_context_job": {
                                "content_type_hint": "song",
                                "song_candidate": True,
                                "requires_full_source_song_boundary_redo": True,
                            },
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        return Completed()

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    result = runner.produce_song(
        date,
        {
            "cid": cid,
            "segment_path": str(segment),
            "seg_dur_ms": duration,
            "anchor_start_ms": anchor_start,
            "anchor_end_ms": anchor_end,
            "danmaku": 18,
            "hook": "测试",
            "preview": "下播前演唱 yonige《芽吹くとき》",
        },
    )

    assert result["window_classified_song"] is True
    assert "full_source_retry" in result
    assert result["decision"] == "BLOCK"
    assert result["full_source_authoritative_block"] is True
    assert result["full_source_performer_rejection"] is True
    assert "SONG_BACKGROUND_PLAYBACK_ONLY" in result["reason_codes"]
    assert "SONG_NOT_LIDOUSHA_SINGING" in result["reason_codes"]
    assert len(selector_commands) == 2
    tight_command = selector_commands[0]
    full_command = selector_commands[1]
    assert "--seed-song-candidate-id" in tight_command
    assert tight_command[tight_command.index("--seed-song-candidate-id") + 1] == "seededsong_15000_65000"
    assert "--agy-audio-lrc-align" not in tight_command
    assert "--agy-audio-lrc-align" in full_command
    assert full_command[full_command.index("--seed-song-candidate-id") + 1] == "seededsong_45000_95000"
    assert full_command[full_command.index("--seed-song-anchor-start-ms") + 1] == "45000"
    assert full_command[full_command.index("--seed-song-anchor-end-ms") + 1] == "95000"
    assert full_command[full_command.index("--source-duration-ms") + 1] == "140000"
    query_indexes = [index for index, value in enumerate(full_command) if value == "--song-lrc-query"]
    assert [full_command[index + 1] for index in query_indexes] == [
        "芽吹くとき",
        "下播前演唱 yonige《芽吹くとき》",
    ]


def test_full_song_authoritative_retry_timeout_always_promotes_block(tmp_path, monkeypatch):
    date = "2026-07-09"
    cid = "song_timeout_retry"
    base = tmp_path / "autoslice"
    repo = tmp_path / "repo"
    out_dir = base / "out" / date / cid
    (base / "logs").mkdir(parents=True)
    out_dir.mkdir(parents=True)
    repo.mkdir()
    monkeypatch.setattr(runner, "BASE", base)
    monkeypatch.setattr(runner, "REPO_ROOT", repo)
    monkeypatch.setattr(runner, "child_env", lambda: {})
    monkeypatch.setattr(runner, "cpa_qa_cmd", lambda: "judge")
    monkeypatch.setattr(
        runner,
        "slice_srt",
        lambda _src, _start, _end, dest: (dest.write_text(
            "1\n00:00:00,000 --> 00:00:01,000\n歌词\n", encoding="utf-8"
        ) or 1),
    )

    segment = tmp_path / "segment.mp4"
    segment.write_bytes(b"segment")
    for tag in ("", "_full"):
        (out_dir / f"{cid}{tag}_source.mp4").write_bytes(tag.encode() or b"tight")

    calls = 0

    class Completed:
        def __init__(self, returncode):
            self.returncode = returncode

    def fake_run(command, **_kwargs):
        nonlocal calls
        calls += 1
        selector_dir = Path(command[command.index("--output-dir") + 1])
        if calls == 1:
            (selector_dir / "summary.json").write_text(
                json.dumps(
                    {
                        "records": [
                            {
                                "candidate_id": "seededsong_15000_65000",
                                "decision_action": "BLOCK",
                                "reason_codes": ["SONG_FULL_BOUNDARY_PROOF_MISSING"],
                                "source_context_job": {
                                    "content_type_hint": "song",
                                    "song_candidate": True,
                                    "requires_full_source_song_boundary_redo": True,
                                },
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            return Completed(0)
        # Authoritative full-source attempt times out and writes no summary.
        return Completed(124)

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    result = runner.produce_song(
        date,
        {
            "cid": cid,
            "segment_path": str(segment),
            "seg_dur_ms": 200_000,
            "anchor_start_ms": 50_000,
            "anchor_end_ms": 100_000,
            "danmaku": 18,
            "hook": "测试",
            "preview": "《测试歌》",
        },
    )

    assert calls == 2
    assert result["decision"] == "BLOCK"
    assert result["song_complete"] is False
    assert result["full_source_authoritative_block"] is True
    assert result.get("full_source_performer_rejection") is not True
    assert result["full_source_retry"]["rc"] == 124
    assert "SONG_FULL_BOUNDARY_PROOF_MISSING" in result["reason_codes"]
    assert "delivered" not in result


def test_song_completion_evidence_is_hash_bound_and_requires_lrc_materialization(tmp_path, monkeypatch):
    report = tmp_path / "song.lyrics-alignment-report.json"
    report_payload = {
        "schema_version": "lyrics-alignment-report.v1",
        "alignment_model": "external_lrc_global_shift.v1",
        "candidate_id": "song-proof",
        "provider": "lrclib",
        "song_title": "芽吹くとき",
        "source_ref": "https://lrclib.net/api/get/33542202",
        "offset_ms": 1_500,
        "nominal_lrc_zero_ms": 1_500,
        "first_lyric_start_ms": 1_500,
        "last_lyric_end_ms": 32_500,
        "line_count": 8,
        "matched_line_count": 8,
        "matched_line_ratio": 1.0,
        "lyric_lines": [{"lrc_time_ms": index * 4_000, "text": f"歌词{index}"} for index in range(8)],
        "alignment": [{"lrc_time_ms": index * 4_000, "matched_cue_id": f"cue-{index}"} for index in range(8)],
    }
    report.write_text(json.dumps(report_payload, ensure_ascii=False) + "\n", encoding="utf-8")
    source = tmp_path / "song.source.mp4"
    source.write_bytes(b"source-video")
    bind_ready_live_performance_report(report, source_media=source, candidate_id="song-proof")
    host_vocal_claim, host_vocal_profile = make_ready_host_vocal_claim(
        tmp_path / "host-vocal",
        source_media=source,
        alignment_report=report,
        candidate_id="song-proof",
    )
    monkeypatch.setattr(runner, "HOST_VOCAL_PROFILE", host_vocal_profile)
    report_sha = hashlib.sha256(report.read_bytes()).hexdigest()
    subtitle = tmp_path / "song.srt"
    subtitle.write_text("1\n00:00:01,500 --> 00:00:02,500\n歌词0\n", encoding="utf-8")
    subtitle_sha = "sha256:" + hashlib.sha256(subtitle.read_bytes()).hexdigest()
    recut_media = tmp_path / "song.recut.mp4"
    recut_media.write_bytes(b"recut-video")
    recut_media_sha = "sha256:" + hashlib.sha256(recut_media.read_bytes()).hexdigest()
    burned = tmp_path / "song.burned.mp4"
    burned.write_bytes(b"burned-video")
    burned_sha = "sha256:" + hashlib.sha256(burned.read_bytes()).hexdigest()
    ass = tmp_path / "song.final-sapphire72.ass"
    ass.write_text("[Script Info]\n", encoding="utf-8")
    ass_sha = "sha256:" + hashlib.sha256(ass.read_bytes()).hexdigest()
    source_path = str(source.resolve())
    source_sha = "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest()
    report_bound = json.loads(report.read_text(encoding="utf-8"))
    audio_artifacts = report_bound["audio_alignment_artifacts"]
    stream_contract = runner._expected_song_stream_contract()
    accurate_command = runner._expected_song_recut_command(
        source_path, str(recut_media.resolve()), 0, 33_500
    )
    burn_command = [
        "ffmpeg", "-i", str(recut_media.resolve()), "-vf", "subtitles=test.ass",
        "-map", "0:v:0", "-map", "0:a:0", "-sn", "-dn",
        "-map_metadata", "-1", "-map_chapters", "-1", str(burned.resolve()),
    ]
    proofs = {
        "lyrics_alignment_report_path": str(report.resolve()),
        "lyrics_alignment_report_sha256": report_sha,
        "host_vocal_proof_path": str(Path(host_vocal_claim["proof_path"]).resolve()),
        "host_vocal_proof_sha256": host_vocal_claim["proof_sha256"],
        "agy_run_manifest_path": str(Path(audio_artifacts["run_manifest_path"]).resolve()),
        "agy_run_manifest_sha256": audio_artifacts["run_manifest_sha256"],
        "post_song_anchor_start_ms": 33_500,
    }
    artifact_hashes = {
        "video_sha256": recut_media_sha,
        "subtitle_sha256": subtitle_sha,
        "burned_video_sha256": burned_sha,
        "ass_sha256": ass_sha,
    }
    burned_record = {
        "status": "BURNED",
        "path": str(burned.resolve()),
        "ass_path": str(ass.resolve()),
        "burned_sha256": burned_sha,
        "subtitle_style": "lidousha-final-sapphire72",
        "pillarbox_16_9": False,
        "command": burn_command,
        "stream_contract": stream_contract,
    }
    output_binding = {
        "schema_version": runner.VERIFIED_SONG_OUTPUT_BINDING_SCHEMA_VERSION,
        "candidate_id": "song-proof",
        "source": {"canonical_path": source_path, "sha256": source_sha},
        "interval": {"start_ms": 0, "end_ms": 33_500, "duration_ms": 33_500},
        "post_song_anchor_start_ms": 33_500,
        "proofs": proofs,
        "stream_contract": stream_contract,
        "recut_transform": {
            "schema_version": "song-recut-transform.v1",
            "method": "two_stage_seek_reencode",
            "start_ms": 0,
            "duration_ms": 33_500,
            "command": accurate_command,
            "video_codec": "libx264",
            "audio_codec": "aac",
        },
        "artifacts": {
            "recut_media_path": str(recut_media.resolve()),
            "recut_media_sha256": recut_media_sha,
            "subtitle_path": str(subtitle.resolve()),
            "subtitle_sha256": subtitle_sha,
            "burned_media_path": str(burned.resolve()),
            "burned_media_sha256": burned_sha,
            "ass_path": str(ass.resolve()),
            "ass_sha256": ass_sha,
        },
        "burn_transform": {
            "schema_version": "song-subtitle-burn-transform.v1",
            "command": burn_command,
            "subtitle_style": "lidousha-final-sapphire72",
            "pillarbox_16_9": False,
        },
    }
    recut_manifest = tmp_path / "song.recut.manifest.json"
    manifest_payload = {
        "schema_version": runner.MATERIALIZED_RECUT_SCHEMA_VERSION,
        "status": "MATERIALIZED",
        "reason_codes": [],
        "candidate_id": "song-proof",
        "source_video_path": source_path,
        "source_binding": output_binding["source"],
        "requested_range": output_binding["interval"],
        "media_path": str(recut_media.resolve()),
        "subtitle_path": str(subtitle.resolve()),
        "subtitle_source": "external_lrc_global_shift",
        "lyric_offset_ms": 1_500,
        "accurate_command": accurate_command,
        "stream_contract": stream_contract,
        "recut_transform": output_binding["recut_transform"],
        "song_output_proof_binding": proofs,
        "verified_output_binding": output_binding,
        "artifact_hashes": artifact_hashes,
        "burned_preview": burned_record,
        "accurate_rerender_used": True,
    }
    recut_manifest.write_text(json.dumps(manifest_payload, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    recut_manifest_sha = "sha256:" + hashlib.sha256(recut_manifest.read_bytes()).hexdigest()
    monkeypatch.setattr(runner, "_has_exact_av_streams", lambda _path: True)
    record = {
        "candidate_id": "song-proof",
        "source_context_job": {
            "song_boundary": {
                "status": "FULL_SONG_READY",
                "song_title": "芽吹くとき",
                "clip_start_ms": 0,
                "nominal_lrc_zero_ms": 1_500,
                "first_lyric_start_ms": 1_500,
                "last_lyric_end_ms": 32_500,
                "clip_end_ms": 33_500,
            },
            "lyrics_alignment": {
                "status": "READY",
                "provider": "lrclib",
                "model": "lrclib-agy-audio-lrc-global-shift-v1",
                "source": "song_repair.lrclib",
                "external_lrc": "https://lrclib.net/api/get/33542202",
                "offset_ms": 1_500,
                "nominal_lrc_zero_ms": 1_500,
                "matched_line_ratio": 1.0,
                "alignment_report_path": str(report),
                "alignment_report_sha256": report_sha,
                "source_media_path": source_path,
                "source_media_sha256": source_sha,
            },
            "host_vocal_proof": host_vocal_claim,
        },
        "materialized_recut": {
            "status": "MATERIALIZED",
            "reason_codes": [],
            "candidate_id": "song-proof",
            "start_ms": 0,
            "end_ms": 33_500,
            "duration_ms": 33_500,
            "source_video_path": source_path,
            "source_binding": output_binding["source"],
            "media_path": str(recut_media.resolve()),
            "subtitle_source": "external_lrc_global_shift",
            "accurate_rerender_used": True,
            "render_qa": {
                "code": "ACTUAL_CUT_ERROR_OK",
                "pass": True,
                "severity": "PASS",
                "evidence": {"actual_cut_error_ms": 0, "threshold_ms": 100},
            },
            "subtitle_path": str(subtitle),
            "lyric_offset_ms": 1_500,
            "manifest_path": str(recut_manifest),
            "manifest_sha256": recut_manifest_sha,
            "accurate_command": accurate_command,
            "stream_contract": stream_contract,
            "recut_transform": output_binding["recut_transform"],
            "song_output_proof_binding": proofs,
            "verified_output_binding": output_binding,
            "burned_preview": burned_record,
            "artifact_hashes": artifact_hashes,
        },
    }
    # Publish staging may append cover hashes after the recut manifest has
    # already sealed the media/subtitle/burn artifacts; that extension is not
    # a recut-manifest mismatch.
    record["materialized_recut"]["artifact_hashes"]["cover_sha256"] = "sha256:" + "0" * 64
    evidence = runner.song_completion_evidence(record)
    assert evidence["ready"] is True
    assert evidence["reason_codes"] == []

    original_source_bytes = source.read_bytes()
    source.write_bytes(b"tampered-source-video")
    evidence = runner.song_completion_evidence(record)
    assert evidence["ready"] is False
    assert "SONG_RECUT_SOURCE_BINDING_INVALID" in evidence["reason_codes"]
    source.write_bytes(original_source_bytes)

    saved_binding = json.loads(json.dumps(record["materialized_recut"]["verified_output_binding"]))
    record["materialized_recut"]["verified_output_binding"]["source"]["canonical_path"] = str(
        (tmp_path / "different-source.mp4").resolve()
    )
    evidence = runner.song_completion_evidence(record)
    assert evidence["ready"] is False
    assert "SONG_RECUT_SOURCE_BINDING_INVALID" in evidence["reason_codes"]
    record["materialized_recut"]["verified_output_binding"] = saved_binding

    record["materialized_recut"]["end_ms"] = 33_499
    evidence = runner.song_completion_evidence(record)
    assert evidence["ready"] is False
    assert "SONG_RECUT_INTERVAL_BINDING_INVALID" in evidence["reason_codes"]
    record["materialized_recut"]["end_ms"] = 33_500

    saved_contract = record["materialized_recut"]["stream_contract"]
    record["materialized_recut"]["stream_contract"] = {
        **saved_contract,
        "ffmpeg_maps": ["0:v:0", "0:a:1"],
    }
    evidence = runner.song_completion_evidence(record)
    assert evidence["ready"] is False
    assert "SONG_RECUT_STREAM_CONTRACT_INVALID" in evidence["reason_codes"]
    record["materialized_recut"]["stream_contract"] = saved_contract

    original_manifest_bytes = recut_manifest.read_bytes()
    original_manifest_sha = record["materialized_recut"]["manifest_sha256"]
    self_consistent_tamper = json.loads(original_manifest_bytes)
    self_consistent_tamper["accurate_command"] = [*accurate_command[:-1], "tampered-output.mp4"]
    recut_manifest.write_text(
        json.dumps(self_consistent_tamper, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    record["materialized_recut"]["manifest_sha256"] = (
        "sha256:" + hashlib.sha256(recut_manifest.read_bytes()).hexdigest()
    )
    evidence = runner.song_completion_evidence(record)
    assert evidence["ready"] is False
    assert "SONG_RECUT_MANIFEST_CONTENT_INVALID" in evidence["reason_codes"]
    recut_manifest.write_bytes(original_manifest_bytes)
    record["materialized_recut"]["manifest_sha256"] = original_manifest_sha

    recut_manifest.write_text("{}\n", encoding="utf-8")
    evidence = runner.song_completion_evidence(record)
    assert evidence["ready"] is False
    assert "SONG_RECUT_MANIFEST_HASH_INVALID" in evidence["reason_codes"]
    recut_manifest.write_bytes(original_manifest_bytes)

    # Regression for the 2026-07-09 《芽吹くとき》 false positive: even a
    # perfect 8/8 LRC/global-shift proof cannot stand in for 李豆沙 singing.
    saved_host_claim = record["source_context_job"].pop("host_vocal_proof")
    evidence = runner.song_completion_evidence(record)
    assert evidence["ready"] is False
    assert "SONG_HOST_VOCAL_UNPROVEN" in evidence["reason_codes"]
    record["source_context_job"]["host_vocal_proof"] = saved_host_claim

    # The model name and report evidence type are a two-way binding.  Removing
    # evidence_source from an audio report must not downgrade it to the looser
    # legacy-text proof gate.
    ready_report_payload = json.loads(report.read_text(encoding="utf-8"))
    missing_type_payload = dict(ready_report_payload)
    missing_type_payload.pop("evidence_source", None)
    report.write_text(json.dumps(missing_type_payload, ensure_ascii=False) + "\n", encoding="utf-8")
    record["source_context_job"]["lyrics_alignment"]["alignment_report_sha256"] = hashlib.sha256(report.read_bytes()).hexdigest()
    evidence = runner.song_completion_evidence(record)
    assert evidence["ready"] is False
    assert "SONG_ALIGNMENT_EVIDENCE_TYPE_MISMATCH" in evidence["reason_codes"]
    assert evidence["live_performance_status"] is None
    assert evidence["joint_singing_decision"] is None
    report.write_text(json.dumps(ready_report_payload, ensure_ascii=False) + "\n", encoding="utf-8")
    record["source_context_job"]["lyrics_alignment"]["alignment_report_sha256"] = hashlib.sha256(report.read_bytes()).hexdigest()

    # An audio-derived report cannot rely on its own hash alone: the final
    # runner edge also requires the current audio/LRC/prompt/raw-output/run
    # manifest bindings and an attested High-model no-fallback run.
    report_payload["evidence_source"] = "agy_audio_lrc"
    report.write_text(json.dumps(report_payload, ensure_ascii=False) + "\n", encoding="utf-8")
    record["source_context_job"]["lyrics_alignment"]["alignment_report_sha256"] = hashlib.sha256(report.read_bytes()).hexdigest()
    evidence = runner.song_completion_evidence(record)
    assert evidence["ready"] is False
    assert "SONG_AUDIO_LRC_ARTIFACTS_INVALID" in evidence["reason_codes"]
    assert evidence["live_performance_status"] is None
    assert evidence["joint_singing_decision"] is None
    report_payload.pop("evidence_source")
    report.write_text(json.dumps(report_payload, ensure_ascii=False) + "\n", encoding="utf-8")
    record["source_context_job"]["lyrics_alignment"]["alignment_report_sha256"] = hashlib.sha256(report.read_bytes()).hexdigest()

    # The same negative nominal zero in all three documents is still invalid:
    # equality cannot make unavailable pre-source music become complete.
    report_payload["nominal_lrc_zero_ms"] = -1
    report.write_text(json.dumps(report_payload, ensure_ascii=False) + "\n", encoding="utf-8")
    record["source_context_job"]["song_boundary"]["nominal_lrc_zero_ms"] = -1
    record["source_context_job"]["lyrics_alignment"]["nominal_lrc_zero_ms"] = -1
    record["source_context_job"]["lyrics_alignment"]["alignment_report_sha256"] = hashlib.sha256(report.read_bytes()).hexdigest()
    evidence = runner.song_completion_evidence(record)
    assert evidence["ready"] is False
    assert "SONG_NOMINAL_LRC_ZERO_INVALID" in evidence["reason_codes"]

    # A rehashed report from a different nominal-zero calculation must not be
    # mixed with the current boundary/materialized recut.
    report_payload["nominal_lrc_zero_ms"] = 1_501
    report.write_text(json.dumps(report_payload, ensure_ascii=False) + "\n", encoding="utf-8")
    record["source_context_job"]["song_boundary"]["nominal_lrc_zero_ms"] = 1_500
    record["source_context_job"]["lyrics_alignment"]["nominal_lrc_zero_ms"] = 1_500
    record["source_context_job"]["lyrics_alignment"]["alignment_report_sha256"] = hashlib.sha256(report.read_bytes()).hexdigest()
    evidence = runner.song_completion_evidence(record)
    assert evidence["ready"] is False
    assert "SONG_NOMINAL_LRC_ZERO_MISMATCH" in evidence["reason_codes"]

    report_payload["nominal_lrc_zero_ms"] = 1_500
    report.write_text(json.dumps(report_payload, ensure_ascii=False) + "\n", encoding="utf-8")
    record["source_context_job"]["lyrics_alignment"]["alignment_report_sha256"] = hashlib.sha256(report.read_bytes()).hexdigest()

    record["materialized_recut"]["accurate_rerender_used"] = False
    evidence = runner.song_completion_evidence(record)
    assert evidence["ready"] is False
    assert "SONG_ACCURATE_RERENDER_REQUIRED" in evidence["reason_codes"]
    record["materialized_recut"]["accurate_rerender_used"] = True

    record["materialized_recut"]["reason_codes"] = ["FFMPEG_ACCURATE_RECUT_FAILED"]
    evidence = runner.song_completion_evidence(record)
    assert evidence["ready"] is False
    assert "SONG_ACCURATE_RERENDER_FAILED" in evidence["reason_codes"]
    record["materialized_recut"]["reason_codes"] = []

    record["materialized_recut"]["render_qa"]["pass"] = False
    evidence = runner.song_completion_evidence(record)
    assert evidence["ready"] is False
    assert "SONG_RENDER_QA_FAILED" in evidence["reason_codes"]
    record["materialized_recut"]["render_qa"]["pass"] = True

    # A self-consistent hash over an empty report is not semantic proof.
    report.write_text("{}\n", encoding="utf-8")
    record["source_context_job"]["lyrics_alignment"]["alignment_report_sha256"] = hashlib.sha256(report.read_bytes()).hexdigest()
    evidence = runner.song_completion_evidence(record)
    assert evidence["ready"] is False
    assert "SONG_ALIGNMENT_REPORT_SCHEMA_INVALID" in evidence["reason_codes"]

    report.write_text(json.dumps(report_payload, ensure_ascii=False) + "\n", encoding="utf-8")
    record["source_context_job"]["lyrics_alignment"]["alignment_report_sha256"] = report_sha
    report.write_text("tampered\n", encoding="utf-8")
    evidence = runner.song_completion_evidence(record)
    assert evidence["ready"] is False
    assert "SONG_ALIGNMENT_REPORT_HASH_INVALID" in evidence["reason_codes"]

    report.write_text(json.dumps(report_payload, ensure_ascii=False) + "\n", encoding="utf-8")
    record["materialized_recut"]["subtitle_source"] = "asr_cues"
    evidence = runner.song_completion_evidence(record)
    assert evidence["ready"] is False
    assert "SONG_EXTERNAL_LRC_SUBTITLE_NOT_MATERIALIZED" in evidence["reason_codes"]

    record["materialized_recut"]["subtitle_source"] = "external_lrc_global_shift"
    burned.write_bytes(b"stale-video")
    evidence = runner.song_completion_evidence(record)
    assert evidence["ready"] is False
    assert "SONG_BURNED_PREVIEW_HASH_INVALID" in evidence["reason_codes"]


def test_verified_song_fallback_title_keeps_song_and_hook():
    assert runner.verified_song_fallback_title("芽吹くとき", "下播前的温柔哄睡小歌，唱完刷晚安") == (
        "【李豆沙】豆沙歌，《芽吹くとき》｜下播前的温柔哄睡小歌"
    )


def test_song_proof_retry_padding_exceeds_recall_padding():
    assert runner.SONG_PROOF_RETRY_PRE_MS > runner.SONG_WINDOW_PRE_MS
    assert runner.SONG_PROOF_RETRY_POST_MS > runner.SONG_WINDOW_POST_MS
    assert runner.song_proof_retry_window(166_220, 321_760, 483_352) == (121_220, 366_760)
    assert runner.song_proof_retry_window(10_000, 90_000, 100_000) == (0, 100_000)


def test_song_artifact_hash_check_rejects_mutation_and_symlink(tmp_path):
    artifact = tmp_path / "current.mp4"
    artifact.write_bytes(b"current")
    digest = "sha256:" + hashlib.sha256(b"current").hexdigest()
    assert runner._matches_sha256(artifact, digest)
    artifact.write_bytes(b"mutated")
    assert not runner._matches_sha256(artifact, digest)
    link = tmp_path / "link.mp4"
    link.symlink_to(artifact)
    assert not runner._matches_sha256(link, "sha256:" + hashlib.sha256(b"mutated").hexdigest())


def _verified_delivery_specs(tmp_path):
    sources = tmp_path / "materialized"
    delivery = tmp_path / "delivery"
    sources.mkdir()
    payloads = {
        "video": b"verified-burned-video",
        "cover": b"verified-cover",
        "subtitle": b"1\n00:00:00,000 --> 00:00:01,000\nlyric\n",
        "lyrics_alignment_report": b'{"status":"READY"}\n',
        "host_vocal_proof": b'{"decision":"LIDOUSHA_VOCAL_PRESENT_ON_LYRIC_CHECKPOINTS"}\n',
        "recut_manifest": b'{"status":"MATERIALIZED"}\n',
    }
    suffixes = {
        "video": ".mp4",
        "cover": ".cover.png",
        "subtitle": ".srt",
        "lyrics_alignment_report": ".lyrics-alignment-report.json",
        "host_vocal_proof": ".host-vocal-proof.json",
        "recut_manifest": ".recut.manifest.json",
    }
    specs = {}
    for role, payload in payloads.items():
        source = sources / f"source{suffixes[role]}"
        source.write_bytes(payload)
        destination = delivery / f"song{suffixes[role]}"
        specs[role] = (
            source,
            destination,
            "sha256:" + hashlib.sha256(payload).hexdigest(),
        )
    return delivery, payloads, specs


def test_atomic_verified_song_delivery_records_final_hashes_and_no_upload_manifest(tmp_path):
    delivery, payloads, specs = _verified_delivery_specs(tmp_path)
    manifest = delivery / "song.delivery.manifest.json"

    receipt = runner._atomic_verified_song_delivery(
        candidate_id="song_delivery_positive",
        manifest_path=manifest,
        artifact_specs=specs,
    )

    assert receipt["upload_enabled"] is False
    assert receipt["manifest_path"] == str(manifest.resolve())
    assert runner._matches_sha256(manifest, receipt["manifest_sha256"])
    manifest_payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert manifest_payload["schema_version"] == runner.VERIFIED_SONG_DELIVERY_SCHEMA_VERSION
    assert manifest_payload["status"] == "DELIVERED_NO_UPLOAD"
    assert manifest_payload["upload_enabled"] is False
    assert manifest_payload["candidate_id"] == "song_delivery_positive"
    assert set(manifest_payload["artifacts"]) == set(payloads)
    for role, payload in payloads.items():
        delivered = specs[role][1]
        expected = "sha256:" + hashlib.sha256(payload).hexdigest()
        assert delivered.read_bytes() == payload
        assert receipt["artifacts"][role]["path"] == str(delivered.resolve())
        assert receipt["artifacts"][role]["sha256"] == expected
        assert manifest_payload["artifacts"][role]["sha256"] == expected
        assert runner._matches_sha256(delivered, expected)
    assert not [path for path in delivery.iterdir() if path.name.startswith(".")]


def test_atomic_verified_song_delivery_rejects_sidecar_copy_hash_mismatch(tmp_path):
    delivery, _payloads, specs = _verified_delivery_specs(tmp_path)
    subtitle_source, subtitle_destination, _subtitle_hash = specs["subtitle"]
    specs["subtitle"] = (subtitle_source, subtitle_destination, "sha256:" + "0" * 64)

    with pytest.raises(runner.SongDeliveryError, match="copied hash mismatch"):
        runner._atomic_verified_song_delivery(
            candidate_id="song_sidecar_mismatch",
            manifest_path=delivery / "song.delivery.manifest.json",
            artifact_specs=specs,
        )

    assert list(delivery.iterdir()) == [], "a sidecar mismatch must expose no partial package"


def test_atomic_verified_song_delivery_removes_unverified_optional_sidecar(tmp_path):
    delivery, _payloads, specs = _verified_delivery_specs(tmp_path)
    delivery.mkdir()
    stale_cover = specs["cover"][1]
    stale_cover.write_bytes(b"stale-cover-from-previous-attempt")
    specs.pop("cover")
    manifest = delivery / "song.delivery.manifest.json"

    runner._atomic_verified_song_delivery(
        candidate_id="song_without_verified_cover",
        manifest_path=manifest,
        artifact_specs=specs,
        absent_artifacts={"cover": stale_cover},
    )

    assert not stale_cover.exists(), "an unverified old cover must not survive beside the new video"
    manifest_payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert manifest_payload["absent_artifacts"] == {
        "cover": {"path": str(stale_cover.absolute()), "status": "ABSENT"}
    }


def test_atomic_verified_song_delivery_detects_post_replace_corruption_and_rolls_back(tmp_path, monkeypatch):
    delivery, _payloads, specs = _verified_delivery_specs(tmp_path)
    manifest = delivery / "song.delivery.manifest.json"
    video_destination = specs["video"][1]
    real_replace = runner.os.replace

    def replace_then_corrupt(source, destination):
        real_replace(source, destination)
        if Path(destination) == video_destination:
            video_destination.write_bytes(b"corrupted-after-atomic-replace")

    monkeypatch.setattr(runner.os, "replace", replace_then_corrupt)
    with pytest.raises(runner.SongDeliveryError, match="final delivery hash mismatch"):
        runner._atomic_verified_song_delivery(
            candidate_id="song_post_copy_corruption",
            manifest_path=manifest,
            artifact_specs=specs,
        )

    assert list(delivery.iterdir()) == [], "post-copy corruption must revoke video and every sidecar"


def test_atomic_verified_song_delivery_manifest_replace_failure_exposes_no_partial(tmp_path, monkeypatch):
    delivery, _payloads, specs = _verified_delivery_specs(tmp_path)
    manifest = delivery / "song.delivery.manifest.json"
    real_replace = runner.os.replace

    def interrupted_replace(source, destination):
        real_replace(source, destination)
        if Path(destination) == manifest:
            raise OSError("simulated interruption after manifest replace")

    monkeypatch.setattr(runner.os, "replace", interrupted_replace)
    with pytest.raises(runner.SongDeliveryError, match="simulated interruption after manifest replace"):
        runner._atomic_verified_song_delivery(
            candidate_id="song_interrupted_commit",
            manifest_path=manifest,
            artifact_specs=specs,
        )

    assert list(delivery.iterdir()) == [], "manifest-last failure must roll back all public artifact names"


def _specs_with_basename(tmp_path, candidate_id, payload_prefix):
    tmp_path.mkdir(parents=True, exist_ok=True)
    delivery, payloads, specs = _verified_delivery_specs(tmp_path)
    basename = runner._song_delivery_basename("【李豆沙】豆沙歌，同一个很长的标题", candidate_id)
    renamed = {}
    for role, (source, _destination, expected) in specs.items():
        payload = payload_prefix + payloads[role]
        source.write_bytes(payload)
        suffix = {
            "video": ".mp4",
            "cover": ".cover.png",
            "subtitle": ".srt",
            "lyrics_alignment_report": ".lyrics-alignment-report.json",
            "host_vocal_proof": ".host-vocal-proof.json",
            "recut_manifest": ".recut.manifest.json",
        }[role]
        renamed[role] = (
            source,
            delivery / f"{basename}{suffix}",
            "sha256:" + hashlib.sha256(payload).hexdigest(),
        )
    return delivery, basename, renamed


def test_same_title_different_song_candidates_have_distinct_sequential_deliveries(tmp_path):
    _delivery_a, basename_a, specs_a = _specs_with_basename(tmp_path / "a", "song_candidate_a", b"a-")
    _delivery_b, basename_b, specs_b = _specs_with_basename(tmp_path / "b", "song_candidate_b", b"b-")
    shared = tmp_path / "delivery"
    shared.mkdir()
    specs_a = {role: (source, shared / destination.name, sha) for role, (source, destination, sha) in specs_a.items()}
    specs_b = {role: (source, shared / destination.name, sha) for role, (source, destination, sha) in specs_b.items()}

    receipt_a = runner._atomic_verified_song_delivery(
        candidate_id="song_candidate_a",
        manifest_path=shared / f"{basename_a}.delivery.manifest.json",
        artifact_specs=specs_a,
    )
    receipt_b = runner._atomic_verified_song_delivery(
        candidate_id="song_candidate_b",
        manifest_path=shared / f"{basename_b}.delivery.manifest.json",
        artifact_specs=specs_b,
    )

    assert basename_a != basename_b
    assert receipt_a["manifest_path"] != receipt_b["manifest_path"]
    assert Path(receipt_a["artifacts"]["video"]["path"]).is_file()
    assert Path(receipt_b["artifacts"]["video"]["path"]).is_file()
    assert runner._matches_sha256(
        Path(receipt_a["artifacts"]["video"]["path"]), receipt_a["artifacts"]["video"]["sha256"]
    )
    assert runner._matches_sha256(
        Path(receipt_b["artifacts"]["video"]["path"]), receipt_b["artifacts"]["video"]["sha256"]
    )


def test_same_title_different_song_candidates_have_distinct_concurrent_deliveries(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    _delivery_a, basename_a, specs_a = _specs_with_basename(tmp_path / "a", "song_parallel_a", b"a-")
    _delivery_b, basename_b, specs_b = _specs_with_basename(tmp_path / "b", "song_parallel_b", b"b-")
    shared = tmp_path / "delivery"
    shared.mkdir()
    specs_a = {role: (source, shared / destination.name, sha) for role, (source, destination, sha) in specs_a.items()}
    specs_b = {role: (source, shared / destination.name, sha) for role, (source, destination, sha) in specs_b.items()}

    def deliver(candidate_id, basename, specs):
        return runner._atomic_verified_song_delivery(
            candidate_id=candidate_id,
            manifest_path=shared / f"{basename}.delivery.manifest.json",
            artifact_specs=specs,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(deliver, "song_parallel_a", basename_a, specs_a),
            pool.submit(deliver, "song_parallel_b", basename_b, specs_b),
        ]
        receipts = [future.result() for future in futures]

    assert basename_a != basename_b
    assert len({receipt["manifest_path"] for receipt in receipts}) == 2
    for receipt in receipts:
        video = Path(receipt["artifacts"]["video"]["path"])
        assert runner._matches_sha256(video, receipt["artifacts"]["video"]["sha256"])


@pytest.mark.parametrize("candidate_id", ["", "../escape", "id with spaces", "x" * 97])
def test_song_delivery_basename_rejects_unsafe_candidate_ids(candidate_id):
    with pytest.raises(runner.SongDeliveryError, match="unsafe song delivery candidate id"):
        runner._song_delivery_basename("same title", candidate_id)


def test_blocked_songs_do_not_consume_budget_and_backlog_backfills():
    blocked = {"status": "blocked", "candidate_id": "song_a"}
    state = {
        "picks": [], "songs": [blocked], "pending_song": [],
        "song_backlog": [
            {"segment_path": "/rec/s.mp4", "anchor_start_ms": 0, "anchor_end_ms": 60_000,
             "danmaku": 50, "cid": "song_b"},
            {"segment_path": "/rec/s.mp4", "anchor_start_ms": 100_000, "anchor_end_ms": 160_000,
             "danmaku": 40, "cid": "song_c"},
        ],
    }
    runner.refill_songs(state)
    # 一次 BLOCK 不消耗任何交付配额 → 两个坑都还在，从备份按弹幕回填
    assert [s["cid"] for s in state["pending_song"]] == ["song_b", "song_c"]
    assert state["song_backlog"] == []


def test_song_attempt_cap_bounds_backfill():
    state = {
        "picks": [], "songs": [{"status": "blocked"}] * runner.SONG_ATTEMPT_CAP,
        "pending_song": [],
        "song_backlog": [{"segment_path": "/rec/s.mp4", "anchor_start_ms": 0,
                          "anchor_end_ms": 1, "danmaku": 9, "cid": "x"}],
    }
    runner.refill_songs(state)
    assert state["pending_song"] == [], "攻击面：回填必须有硬上限，不能无限产歌"


def test_song_delivery_budget_counts_deliveries_only():
    state = {"songs": [{"status": "review_ready", "delivered": "/x.mp4"}, {"status": "blocked"}]}
    assert runner.song_delivery_budget(state) == MAX_SONGS_PER_DATE - 1


def test_legacy_string_backlog_tolerated():
    state = {"picks": [], "songs": [], "pending_song": [],
             "song_backlog": ["旧版字符串条目 (超出上限)"]}
    runner.refill_songs(state)
    assert state["pending_song"] == []
    assert state["song_backlog"] == ["旧版字符串条目 (超出上限)"]


def test_write_state_atomic_and_read_state_corruption(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "BASE", tmp_path)
    runner.write_state("2026-07-09", {"status": "processing", "n": 1})
    runner.write_state("2026-07-09", {"status": "review_ready", "n": 2})
    assert runner.read_state("2026-07-09")["n"] == 2
    # 主文件损坏 → 隔离留证 + 从 .bak（上一次好写入）恢复并持久化
    runner.state_path("2026-07-09").write_text("{broken", encoding="utf-8")
    st = runner.read_state("2026-07-09")
    assert st.get("n") == 1 and st.get("state_restored_from_bak")
    assert list((tmp_path / "state").glob("*.corrupt-*"))
    assert runner.read_state("2026-07-09").get("n") == 1  # 恢复已持久化，读取幂等
    # 损坏且无 .bak → 永久 BLOCK，绝不当"全新日期"从头重产重交付
    p2 = runner.state_path("2026-07-08")
    p2.parent.mkdir(parents=True, exist_ok=True)
    p2.write_text("{broken", encoding="utf-8")
    assert runner.read_state("2026-07-08")["status"] == "state_corrupt_blocked"
    assert runner.read_state("2026-07-08")["status"] == "state_corrupt_blocked"  # 幂等
    # 真正缺失 → 全新日期
    assert runner.read_state("2026-01-01") == {}


def test_source_health_error_detects_missing_root(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "REC_ROOT", tmp_path / "nope")
    assert runner.source_health_error() is not None
    monkeypatch.setattr(runner, "REC_ROOT", tmp_path)
    assert runner.source_health_error() is None


def test_tick_source_unavailable_is_loud_and_fail_closed(tmp_path, monkeypatch):
    """7/9 P0 实况：挂载死了，心跳还写 live=False dates=(none) 假绿。"""
    monkeypatch.setattr(runner, "BASE", tmp_path)
    monkeypatch.setattr(runner, "cjk_font_present", lambda: True)
    monkeypatch.setattr(runner, "source_health_error", lambda: "Transport endpoint is not connected")
    touched = []
    monkeypatch.setattr(runner, "blrec_live_status", lambda: touched.append(1) or False)
    assert runner.tick() == 0
    heartbeat = (tmp_path / "reports" / "heartbeat.txt").read_text(encoding="utf-8")
    assert "SOURCE_UNAVAILABLE" in heartbeat
    alert = (tmp_path / "reports" / "ALERT_SOURCE_UNAVAILABLE.txt").read_text(encoding="utf-8")
    assert "Transport endpoint" in alert
    assert not touched, "源不可用时什么都不该跑（fail-closed）"


def test_session_sealed_requires_stable_inventory(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "REC_ROOT", tmp_path)
    date_dir = tmp_path / "2026-07-09"
    date_dir.mkdir()
    seg = date_dir / f"{runner.ROOM}_2026-07-09-19-30-00-.mp4"
    seg.write_bytes(b"x" * 10)
    state = {}
    assert not runner.session_sealed("2026-07-09", state), "首见清单不算稳定"
    assert runner.session_sealed("2026-07-09", state), "跨 tick 未变 → 封场"
    seg.write_bytes(b"x" * 20)  # 录制还在写
    assert not runner.session_sealed("2026-07-09", state)
    assert runner.session_sealed("2026-07-09", state)
    (date_dir / f"{runner.ROOM}_2026-07-09-20-00-00-.mp4").write_bytes(b"y")  # 晚段到达
    assert not runner.session_sealed("2026-07-09", state), "晚段必须重新参与竞争"


def test_write_reports_no_delivery_is_loud(tmp_path, monkeypatch):
    """7/9 P0：两歌全 BLOCK、0 交付，摘要却写 done。现在必须刺眼。"""
    monkeypatch.setattr(runner, "BASE", tmp_path)
    monkeypatch.setattr(runner, "REPO_ROOT", tmp_path)
    state = {
        "status": "no_delivery", "picks": [], "segments_done": [], "segments_dead": {},
        "pending_talk": [], "pending_song": [],
        "songs": [{"candidate_id": "song_a", "status": "blocked", "decision": "BLOCK",
                   "reason_codes": ["END_BOUNDARY_LOW"], "danmaku": 18}],
    }
    runner.write_reports("2026-07-09", state)
    text = (tmp_path / "lidousha" / "2026-07-09" / "AUTOSLICE_SUMMARY.md").read_text(encoding="utf-8")
    assert "no_delivery" in text
    assert "0 条交付" in text
    assert "歌 **0 交付**" in text
    assert "done" not in text.splitlines()[2]


def test_write_reports_legacy_quarantine_rows_still_render(tmp_path, monkeypatch):
    """Old state files may still carry pre-2026-07-10 quarantine picks — they
    stay visible (delivered + flags shown) but are labelled as legacy."""
    monkeypatch.setattr(runner, "BASE", tmp_path)
    monkeypatch.setattr(runner, "REPO_ROOT", tmp_path)
    state = {
        "status": "review_ready", "segments_done": [], "segments_dead": {},
        "pending_talk": [], "pending_song": [], "songs": [],
        "picks": [{"candidate_id": "auto_1", "status": "quarantine", "hook": "钩子",
                   "title": "【李豆沙】标题", "confidence": 0.9,
                   "red_flags": ["speech_continues_5000ms_after_cut"],
                   "start_ms": 0, "end_ms": 60_000, "summary": {}}],
    }
    runner.write_reports("2026-07-09", state)
    text = (tmp_path / "lidousha" / "2026-07-09" / "AUTOSLICE_SUMMARY.md").read_text(encoding="utf-8")
    assert "quarantine[speech_continues_5000ms_after_cut]" in text
    assert "1 条旧版 quarantine" in text


def test_write_reports_boundary_repair_and_unrepairable(tmp_path, monkeypatch):
    """Post-2026-07-10 vocabulary (Ivan: unattended = fix-or-refuse): delivered
    picks are clean with the repair trail shown; unrepairable boundaries are
    undelivered failures — no new quarantine state."""
    monkeypatch.setattr(runner, "BASE", tmp_path)
    monkeypatch.setattr(runner, "REPO_ROOT", tmp_path)
    state = {
        "status": "review_ready", "segments_done": [], "segments_dead": {},
        "pending_talk": [], "pending_song": [], "songs": [],
        "picks": [
            {"candidate_id": "auto_1", "status": "review_ready", "hook": "钩子A",
             "title": "【李豆沙】标题A", "confidence": 0.9,
             "boundary_repairs": [{"flags": ["speech_continues_1800ms_after_cut"], "snapped_end_ms": 65_000}],
             "start_ms": 0, "end_ms": 60_000, "summary": {}},
            {"candidate_id": "auto_2", "status": "boundary_unrepairable", "hook": "钩子B",
             "title": None, "confidence": 0.8,
             "start_ms": 0, "end_ms": 50_000, "summary": {}},
        ],
    }
    runner.write_reports("2026-07-09", state)
    text = (tmp_path / "lidousha" / "2026-07-09" / "AUTOSLICE_SUMMARY.md").read_text(encoding="utf-8")
    assert "边界自修复×1" in text
    assert "1 条边界自修复后交付" in text
    assert "1 条边界不可修复未交付" in text
    assert "✗边界不可修复未交付" in text
    assert "⚠quarantine" not in text


def test_pipeline_change_requeues_unproven_song_without_treating_it_as_performer_rejection(tmp_path, monkeypatch):
    date = "2026-07-10"
    rec_root = tmp_path / "recordings"
    date_dir = rec_root / date
    date_dir.mkdir(parents=True)
    segment = date_dir / "22966160_20260710-20-00-09.mp4"
    segment.write_bytes(b"media")
    monkeypatch.setattr(runner, "REC_ROOT", rec_root)
    monkeypatch.setattr(runner, "pipeline_fingerprint", lambda: "sha256:new")
    monkeypatch.setattr(runner, "ffprobe_ms", lambda _path: 600_000)
    monkeypatch.setattr(runner, "find_danmaku_xml", lambda _path: None)
    monkeypatch.setattr(runner, "find_chat_jsonl", lambda _path: None)
    state = {
        "pending_song": [],
        "songs": [
            {
                "candidate_id": "song_200009_217",
                "segment": segment.name,
                "start_ms": 202_000,
                "end_ms": 483_000,
                "status": "blocked",
                "reason_codes": ["SONG_LIVE_PERFORMANCE_UNPROVEN", "SONG_HOST_VOCAL_UNPROVEN"],
                "pipeline_fingerprint": "sha256:old",
                "hook": "《怎么办》",
            }
        ],
    }

    assert runner.requeue_recoverable_songs(date, state) == 1
    assert state["songs"] == []
    assert state["pending_song"][0]["anchor_start_ms"] == 217_000
    assert state["pending_song"][0]["anchor_end_ms"] == 463_000
    assert state["pending_song"][0]["retry_reason"] == "pipeline_fingerprint_changed"


def test_pipeline_fingerprint_covers_song_proof_closure(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "REPO_ROOT", tmp_path)
    load_bearing = [
        "scripts/free_session_autoslice.py",
        "scripts/free_asr_client.py",
        "scripts/cpa_semantic_qa_llm.py",
        "scripts/llm_via_cpa.sh",
        "scripts/run_auto_review_shadow_pipeline.py",
        "src/autoslice/agy_lrc_alignment.py",
        "src/autoslice/host_vocal_proof.py",
        "assets/lidousha/known_songs.json",
        "assets/lidousha/voiceprint_profile.v1.json",
        "assets/lidousha/entity_confusables.json",
    ]
    for relative in load_bearing:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"base:{relative}\n", encoding="utf-8")
    baseline = runner.pipeline_fingerprint()

    for relative in load_bearing:
        path = tmp_path / relative
        original = path.read_text(encoding="utf-8")
        path.write_text(original + "changed\n", encoding="utf-8")
        assert runner.pipeline_fingerprint() != baseline, relative
        path.write_text(original, encoding="utf-8")

    unrelated = tmp_path / "assets/lidousha/title_style.md"
    unrelated.parent.mkdir(parents=True, exist_ok=True)
    unrelated.write_text("unrelated\n", encoding="utf-8")
    assert runner.pipeline_fingerprint() == baseline


def test_song_lifetime_cap_survives_repeated_pipeline_changes(tmp_path, monkeypatch):
    date = "2026-07-10"
    rec_root = tmp_path / "recordings"
    date_dir = rec_root / date
    date_dir.mkdir(parents=True)
    segment = date_dir / "22966160_20260710-20-00-09.mp4"
    segment.write_bytes(b"media")
    monkeypatch.setattr(runner, "REC_ROOT", rec_root)
    monkeypatch.setattr(runner, "SONG_LIFETIME_ATTEMPT_CAP", 2)
    monkeypatch.setattr(runner, "pipeline_fingerprint", lambda: "sha256:generation-3")
    state = {
        "pending_song": [],
        "song_superseded_attempts": [{"candidate_id": "song_old_generation"}],
        "songs": [
            {
                "candidate_id": "song_current",
                "segment": segment.name,
                "start_ms": 100_000,
                "end_ms": 300_000,
                "status": "blocked",
                "reason_codes": ["SONG_LIVE_PERFORMANCE_UNPROVEN"],
                "pipeline_fingerprint": "sha256:generation-2",
            }
        ],
    }

    assert runner.requeue_recoverable_songs(date, state) == 0
    assert state["pending_song"] == []
    assert len(state["songs"]) == 1


def test_confirmed_non_host_song_is_terminal_across_pipeline_change(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "pipeline_fingerprint", lambda: "sha256:new")
    state = {
        "pending_song": [],
        "songs": [
            {
                "candidate_id": "song_other",
                "status": "blocked",
                "reason_codes": ["SONG_NOT_LIDOUSHA_SINGING"],
                "pipeline_fingerprint": "sha256:old",
            }
        ],
    }
    assert runner.requeue_recoverable_songs("2026-07-10", state) == 0
    assert len(state["songs"]) == 1


def test_transient_agy_failure_gets_one_same_fingerprint_retry(tmp_path, monkeypatch):
    date = "2026-07-10"
    rec_root = tmp_path / "recordings"
    date_dir = rec_root / date
    date_dir.mkdir(parents=True)
    segment = date_dir / "22966160_20260710-19-30-09.mp4"
    segment.write_bytes(b"media")
    monkeypatch.setattr(runner, "REC_ROOT", rec_root)
    monkeypatch.setattr(runner, "pipeline_fingerprint", lambda: "sha256:same")
    monkeypatch.setattr(runner, "ffprobe_ms", lambda _path: 500_000)
    monkeypatch.setattr(runner, "find_danmaku_xml", lambda _path: None)
    monkeypatch.setattr(runner, "find_chat_jsonl", lambda _path: None)
    record = {
        "candidate_id": "song_timeout",
        "segment": segment.name,
        "start_ms": 100_000,
        "end_ms": 300_000,
        "status": "blocked",
        "reason_codes": ["AGY_SOURCE_CONTEXT_RUNNER_FAILED"],
        "pipeline_fingerprint": "sha256:same",
        "transient_retry_count": 0,
    }
    state = {"pending_song": [], "songs": [record]}
    assert runner.requeue_recoverable_songs(date, state) == 1
    assert state["pending_song"][0]["transient_retry_count"] == 1

    state = {"pending_song": [], "songs": [{**record, "transient_retry_count": 1}]}
    assert runner.requeue_recoverable_songs(date, state) == 0


def test_invalid_audio_lrc_observation_gets_one_same_fingerprint_retry(tmp_path, monkeypatch):
    date = "2026-07-10"
    rec_root = tmp_path / "recordings"
    date_dir = rec_root / date
    date_dir.mkdir(parents=True)
    segment = date_dir / "22966160_20260710-21-20-05.mp4"
    segment.write_bytes(b"media")
    monkeypatch.setattr(runner, "REC_ROOT", rec_root)
    monkeypatch.setattr(runner, "pipeline_fingerprint", lambda: "sha256:same")
    monkeypatch.setattr(runner, "ffprobe_ms", lambda _path: 500_000)
    monkeypatch.setattr(runner, "find_danmaku_xml", lambda _path: None)
    monkeypatch.setattr(runner, "find_chat_jsonl", lambda _path: None)
    record = {
        "candidate_id": "song_invalid_agy",
        "segment": segment.name,
        "start_ms": 100_000,
        "end_ms": 300_000,
        "status": "blocked",
        "reason_codes": ["SONG_AUDIO_LRC_ALIGNMENT_INVALID"],
        "pipeline_fingerprint": "sha256:same",
        "transient_retry_count": 0,
    }
    state = {"pending_song": [], "songs": [record]}

    assert runner.requeue_recoverable_songs(date, state) == 1
    assert state["pending_song"][0]["transient_retry_count"] == 1

    state = {"pending_song": [], "songs": [{**record, "transient_retry_count": 1}]}
    assert runner.requeue_recoverable_songs(date, state) == 0


def test_unexpected_song_crash_preserves_retry_reconstruction(tmp_path, monkeypatch):
    date = "2026-07-10"
    rec_root = tmp_path / "recordings"
    date_dir = rec_root / date
    date_dir.mkdir(parents=True)
    segment = date_dir / "22966160_20260710-20-00-09.mp4"
    segment.write_bytes(b"media")
    monkeypatch.setattr(runner, "REC_ROOT", rec_root)
    monkeypatch.setattr(runner, "pipeline_fingerprint", lambda: "sha256:same")
    monkeypatch.setattr(runner, "ffprobe_ms", lambda _path: 600_000)
    monkeypatch.setattr(runner, "find_danmaku_xml", lambda _path: None)
    monkeypatch.setattr(runner, "find_chat_jsonl", lambda _path: None)

    def crash(_date, _item):
        raise TimeoutError("worker vanished")

    item = {
        "cid": "song_crash",
        "segment_path": str(segment),
        "seg_dur_ms": 600_000,
        "anchor_start_ms": 200_000,
        "anchor_end_ms": 400_000,
        "hook": "《测试歌》",
    }
    record = runner.produce_batch(date, [item], crash)[0]
    state = {"pending_song": [], "songs": [record]}

    assert record["reason_codes"] == ["PRODUCE_UNEXPECTED_EXCEPTION"]
    assert record["segment"] == segment.name
    assert runner.requeue_recoverable_songs(date, state) == 1
    assert state["pending_song"][0]["anchor_start_ms"] == 200_000
    assert state["pending_song"][0]["transient_retry_count"] == 1


def test_talk_boundary_failure_widens_original_source_and_retries(tmp_path, monkeypatch):
    date = "2026-07-10"
    base = tmp_path / "autoslice"
    repo = tmp_path / "repo"
    (base / "logs").mkdir(parents=True)
    repo.mkdir()
    monkeypatch.setattr(runner, "BASE", base)
    monkeypatch.setattr(runner, "REPO_ROOT", repo)
    monkeypatch.setattr(runner, "child_env", lambda: {})
    monkeypatch.setattr(runner, "pipeline_fingerprint", lambda: "sha256:test")
    calls = []

    class Completed:
        def __init__(self, returncode):
            self.returncode = returncode

    def fake_run(command, **kwargs):
        calls.append(command)
        sink = kwargs["stdout"]
        if len(calls) == 1:
            sink.write("BOUNDARY_UNREPAIRABLE: no closure after 0 repair(s)\n")
            sink.flush()
            return Completed(1)
        sink.write('{"red_flags": [], "boundary_repairs": [{"snapped_end_ms": 182540}]}\n')
        sink.flush()
        return Completed(0)

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    result = runner.produce_talk(
        date,
        {
            "cid": "auto_212005_163_311",
            "segment_path": "/recordings/22966160_20260710-21-20-05.mp4",
            "seg_dur_ms": 900_000,
            "start_ms": 163_000,
            "end_ms": 311_000,
            "xml": None,
            "chat_jsonl": None,
            "hook": "小李嘴硬",
            "confidence": 0.9,
            "lane": "semantic",
        },
    )

    assert len(calls) == 2
    assert result["status"] == "review_ready"
    assert result["boundary_context_retries"] == 1
    spec = json.loads((base / "out" / date / "spec_auto_212005_163_311.json").read_text())
    assert spec["semantic_end_ms"] == 311_000
    assert spec["pieces"][0]["end_ms"] == 401_000


def test_stale_boundary_log_does_not_classify_new_transient_failure(tmp_path, monkeypatch):
    date = "2026-07-10"
    base = tmp_path / "autoslice"
    repo = tmp_path / "repo"
    (base / "logs").mkdir(parents=True)
    repo.mkdir()
    monkeypatch.setattr(runner, "BASE", base)
    monkeypatch.setattr(runner, "REPO_ROOT", repo)
    monkeypatch.setattr(runner, "child_env", lambda: {})
    monkeypatch.setattr(runner, "pipeline_fingerprint", lambda: "sha256:test")
    log_path = base / "logs" / f"{date}_auto_stale.log"
    log_path.write_text("BOUNDARY_UNREPAIRABLE: old attempt\n", encoding="utf-8")
    calls = []

    class Completed:
        returncode = 1

    def fake_run(command, **kwargs):
        calls.append(command)
        kwargs["stdout"].write("CPA temporarily unavailable\n")
        kwargs["stdout"].flush()
        return Completed()

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    result = runner.produce_talk(
        date,
        {
            "cid": "auto_stale",
            "segment_path": "/recordings/segment.mp4",
            "seg_dur_ms": 900_000,
            "start_ms": 100_000,
            "end_ms": 200_000,
            "hook": "test",
        },
    )

    assert len(calls) == 1
    assert result["status"] == "failed"
    assert result["boundary_context_retries"] == 0


def test_pipeline_change_requeues_old_selected_boundary_failure(tmp_path, monkeypatch):
    date = "2026-07-10"
    rec_root = tmp_path / "recordings"
    date_dir = rec_root / date
    date_dir.mkdir(parents=True)
    segment = date_dir / "22966160_20260710-21-20-05.mp4"
    segment.write_bytes(b"media")
    monkeypatch.setattr(runner, "REC_ROOT", rec_root)
    monkeypatch.setattr(runner, "pipeline_fingerprint", lambda: "sha256:new")
    monkeypatch.setattr(runner, "ffprobe_ms", lambda _path: 900_000)
    monkeypatch.setattr(runner, "find_danmaku_xml", lambda _path: None)
    monkeypatch.setattr(runner, "find_chat_jsonl", lambda _path: None)
    state = {
        "pending_talk": [],
        "picks": [
            {
                "candidate_id": "auto_212005_163_311",
                "segment": segment.name,
                "start_ms": 163_000,
                "end_ms": 311_000,
                "status": "boundary_unrepairable",
                "pipeline_fingerprint": "sha256:old",
                "hook": "小李嘴硬",
                "confidence": 0.94,
            },
            {"candidate_id": "delivered", "status": "review_ready"},
        ],
    }

    assert runner.requeue_recoverable_talks(date, state) == 1
    assert [row["candidate_id"] for row in state["picks"]] == ["delivered"]
    assert state["pending_talk"][0]["selected_repair"] is True
    assert state["pending_talk"][0]["talk_repair_retry_count"] == 1
    assert state["talk_superseded_attempts"][0]["superseded_by"] == "sha256:new"


def test_selected_talk_transient_failure_gets_one_same_fingerprint_retry(tmp_path, monkeypatch):
    date = "2026-07-10"
    rec_root = tmp_path / "recordings"
    date_dir = rec_root / date
    date_dir.mkdir(parents=True)
    segment = date_dir / "22966160_20260710-21-20-05.mp4"
    segment.write_bytes(b"media")
    monkeypatch.setattr(runner, "REC_ROOT", rec_root)
    monkeypatch.setattr(runner, "pipeline_fingerprint", lambda: "sha256:same")
    monkeypatch.setattr(runner, "ffprobe_ms", lambda _path: 900_000)
    monkeypatch.setattr(runner, "find_danmaku_xml", lambda _path: None)
    monkeypatch.setattr(runner, "find_chat_jsonl", lambda _path: None)
    failed = {
        "candidate_id": "auto_212005_163_311",
        "segment": segment.name,
        "start_ms": 163_000,
        "end_ms": 311_000,
        "status": "failed",
        "pipeline_fingerprint": "sha256:same",
        "selected_repair": True,
        "talk_repair_retry_count": 1,
        "talk_transient_retry_count": 0,
    }
    state = {"pending_talk": [], "picks": [failed]}

    assert runner.requeue_recoverable_talks(date, state) == 1
    assert state["pending_talk"][0]["retry_reason"] == "transient_produce_failure"
    assert state["pending_talk"][0]["talk_repair_retry_count"] == 2
    assert state["pending_talk"][0]["talk_transient_retry_count"] == 1

    state = {
        "pending_talk": [],
        "picks": [{**failed, "talk_repair_retry_count": 2, "talk_transient_retry_count": 1}],
    }
    assert runner.requeue_recoverable_talks(date, state) == 0


def test_selected_talk_retry_respects_lifetime_cap_after_pipeline_change(tmp_path, monkeypatch):
    date = "2026-07-10"
    rec_root = tmp_path / "recordings"
    date_dir = rec_root / date
    date_dir.mkdir(parents=True)
    segment = date_dir / "segment.mp4"
    segment.write_bytes(b"media")
    monkeypatch.setattr(runner, "REC_ROOT", rec_root)
    monkeypatch.setattr(runner, "pipeline_fingerprint", lambda: "sha256:new")
    monkeypatch.setattr(runner, "TALK_REPAIR_LIFETIME_RETRY_CAP", 3)
    state = {
        "pending_talk": [],
        "picks": [
            {
                "candidate_id": "repair",
                "segment": segment.name,
                "start_ms": 1,
                "end_ms": 2,
                "status": "failed",
                "pipeline_fingerprint": "sha256:old",
                "talk_repair_retry_count": 3,
            }
        ],
    }

    assert runner.requeue_recoverable_talks(date, state) == 0
    assert state["pending_talk"] == []


def test_selected_boundary_repair_bypasses_filled_talk_quota(monkeypatch):
    monkeypatch.setattr(runner, "MAX_TALK_PICKS", 1)
    monkeypatch.setattr(runner, "refill_songs", lambda _state: None)
    state = {
        "picks": [{"candidate_id": "already", "status": "review_ready"}],
        "pending_talk": [
            {
                "cid": "repair",
                "segment_path": "/recordings/segment.mp4",
                "start_ms": 1,
                "end_ms": 2,
                "selected_repair": True,
                "confidence": 0.1,
            },
            {
                "cid": "new",
                "segment_path": "/recordings/segment.mp4",
                "start_ms": 3,
                "end_ms": 4,
                "confidence": 1.0,
            },
        ],
    }

    runner.prioritize(state)

    assert [item["cid"] for item in state["pending_talk"]] == ["repair"]


def test_process_date_wakes_old_boundary_failure_without_new_segments(monkeypatch):
    date = "2026-07-10"
    monkeypatch.setattr(runner, "AUTOMATIC_MAINTENANCE_NOT_BEFORE", date)
    state = {
        "status": "review_ready_with_failures",
        "segments_done": ["segment"],
        "segments_dead": {},
        "pending_talk": [],
        "pending_song": [],
        "songs": [],
        "picks": [
            {
                "candidate_id": "old_boundary",
                "segment": "segment.mp4",
                "start_ms": 10_000,
                "end_ms": 20_000,
                "status": "boundary_unrepairable",
                "pipeline_fingerprint": "sha256:old",
            }
        ],
    }
    produced = []
    monkeypatch.setattr(runner, "read_state", lambda _date: state)

    def wake(_date, value):
        value["pending_talk"].append(
            {
                "cid": "old_boundary",
                "segment_path": "/recordings/segment.mp4",
                "start_ms": 10_000,
                "end_ms": 20_000,
                "selected_repair": True,
            }
        )
        value["picks"] = []
        return 1

    monkeypatch.setattr(runner, "requeue_recoverable_talks", wake)
    monkeypatch.setattr(runner, "requeue_recoverable_songs", lambda _date, _state: 0)
    monkeypatch.setattr(runner, "pipeline_fingerprint", lambda: "sha256:new")
    monkeypatch.setattr(runner, "write_state", lambda _date, _state: None)
    monkeypatch.setattr(runner, "list_segments", lambda _date: [])
    monkeypatch.setattr(runner, "cover_repair_needed", lambda _date, _row: False)
    monkeypatch.setattr(runner, "cpa_healthy", lambda: True)
    monkeypatch.setattr(runner, "discover_segments", lambda _date, _state: None)
    monkeypatch.setattr(runner, "session_sealed", lambda _date, _state: True)
    monkeypatch.setattr(runner, "refill_songs", lambda _state: None)

    def produce(_date, items, _fn):
        produced.extend(items)
        return [{"candidate_id": items[0]["cid"], "status": "review_ready"}]

    monkeypatch.setattr(runner, "produce_batch", produce)
    monkeypatch.setattr(runner, "repair_covers", lambda _date, _state: None)
    monkeypatch.setattr(runner, "write_reports", lambda _date, _state: None)

    runner.process_date(date)

    assert [item["cid"] for item in produced] == ["old_boundary"]
    assert state["picks"][0]["status"] == "review_ready"
    assert state["pending_talk"] == []


def test_process_date_does_not_auto_maintain_pre_horizon_history(monkeypatch):
    date = "2026-07-10"
    monkeypatch.setattr(runner, "AUTOMATIC_MAINTENANCE_NOT_BEFORE", "2026-07-11")
    state = {
        "status": "review_ready_with_failures",
        "segments_done": ["segment"],
        "segments_dead": {},
        "pending_talk": [],
        "pending_song": [],
        "songs": [{"candidate_id": "old_song", "status": "blocked"}],
        "picks": [
            {
                "candidate_id": "old_talk",
                "status": "boundary_unrepairable",
                "pipeline_fingerprint": "sha256:old",
            },
            {
                "candidate_id": "old_cover",
                "status": "review_ready",
                "title": "old cover",
            },
        ],
    }
    monkeypatch.setattr(runner, "read_state", lambda _date: state)
    monkeypatch.setattr(
        runner,
        "requeue_recoverable_talks",
        lambda _date, _state: pytest.fail("pre-horizon talk must not auto-requeue"),
    )
    monkeypatch.setattr(
        runner,
        "requeue_recoverable_songs",
        lambda _date, _state: pytest.fail("pre-horizon song must not auto-requeue"),
    )
    monkeypatch.setattr(runner, "list_segments", lambda _date: [])
    monkeypatch.setattr(
        runner,
        "cover_repair_needed",
        lambda _date, _record: pytest.fail("pre-horizon cover must not auto-repair"),
    )

    runner.process_date(date)

    assert state["pending_talk"] == []
    assert state["pending_song"] == []


def test_process_date_transient_selected_repair_remains_retryable(tmp_path, monkeypatch):
    date = "2026-07-10"
    rec_root = tmp_path / "recordings"
    date_dir = rec_root / date
    date_dir.mkdir(parents=True)
    segment = date_dir / "segment.mp4"
    segment.write_bytes(b"media")
    pending = {
        "cid": "old_boundary",
        "segment_path": str(segment),
        "seg_dur_ms": 100_000,
        "start_ms": 10_000,
        "end_ms": 20_000,
        "selected_repair": True,
        "talk_repair_retry_count": 1,
        "talk_transient_retry_count": 0,
    }
    state = {
        "status": "processing",
        "segments_done": ["segment"],
        "segments_dead": {},
        "pending_talk": [pending],
        "pending_song": [],
        "songs": [],
        "picks": [],
    }
    monkeypatch.setattr(runner, "REC_ROOT", rec_root)
    monkeypatch.setattr(runner, "read_state", lambda _date: state)
    monkeypatch.setattr(runner, "pipeline_fingerprint", lambda: "sha256:same")
    monkeypatch.setattr(runner, "write_state", lambda _date, _state: None)
    monkeypatch.setattr(runner, "list_segments", lambda _date: [])
    monkeypatch.setattr(runner, "cover_repair_needed", lambda _date, _row: False)
    monkeypatch.setattr(runner, "cpa_healthy", lambda: True)
    monkeypatch.setattr(runner, "discover_segments", lambda _date, _state: None)
    monkeypatch.setattr(runner, "session_sealed", lambda _date, _state: True)
    monkeypatch.setattr(runner, "refill_songs", lambda _state: None)
    monkeypatch.setattr(runner, "ffprobe_ms", lambda _path: 100_000)
    monkeypatch.setattr(runner, "find_danmaku_xml", lambda _path: None)
    monkeypatch.setattr(runner, "find_chat_jsonl", lambda _path: None)

    def fail_once(_date, items, _fn):
        item = items[0]
        return [
            {
                "candidate_id": item["cid"],
                "segment": Path(item["segment_path"]).name,
                "start_ms": item["start_ms"],
                "end_ms": item["end_ms"],
                "status": "failed",
                "pipeline_fingerprint": "sha256:same",
                "selected_repair": True,
                "talk_repair_retry_count": item["talk_repair_retry_count"],
                "talk_transient_retry_count": item["talk_transient_retry_count"],
            }
        ]

    monkeypatch.setattr(runner, "produce_batch", fail_once)
    monkeypatch.setattr(runner, "repair_covers", lambda _date, _state: None)
    monkeypatch.setattr(runner, "write_reports", lambda _date, _state: None)

    runner.process_date(date)

    assert state["picks"][0]["status"] == "failed"
    assert state["pending_talk"] == []
    assert runner.requeue_recoverable_talks(date, state) == 1
    assert state["pending_talk"][0]["talk_transient_retry_count"] == 1

def test_record_is_song_recognizes_non_lrc_songs():
    """7/9 实锤：日语歌《ただそばにいて》LRC 钉歌失败(song_boundary/alignment 全空)，
    但召回记录是 semanticsong_* —— 它是歌，必须参与交付竞争。"""
    assert runner.record_is_song({"candidate_id": "semanticsong_15000_170540", "source_context_job": {}})
    assert runner.record_is_song({"candidate_id": "x", "source_context_job": {"song_boundary": {"a": 1}}})
    assert runner.record_is_song({"candidate_id": "x", "source_context_job": {"lyrics_alignment": {"m": 0.9}}})
    assert runner.record_is_song({"candidate_id": "seeded", "source_context_job": {"content_type_hint": "song"}})
    assert runner.record_is_song({"candidate_id": "seeded", "source_context_job": {"song_candidate": True}})
    assert runner.record_is_song({"candidate_id": "seeded", "source_context_job": {"requires_full_source_song_boundary_redo": True}})
    assert not runner.record_is_song({"candidate_id": "semantictalk_3320_50570_ctxexp", "source_context_job": {}})
    assert not runner.record_is_song({})


def test_cpa_qa_cmd_reads_cpa_env_for_direct_produce_song_entrypoint(tmp_path, monkeypatch):
    cpa_env = tmp_path / "cpa.env"
    cpa_env.write_text("CPA_BASE_URL=https://cpa.example.test/v1\nCPA_API_KEY=not-used-in-argv\n", encoding="utf-8")
    monkeypatch.setattr(runner, "CPA_ENV", cpa_env)
    monkeypatch.setenv("CPA_BASE_URL", "https://stale-environment.invalid/v1")

    command = runner.cpa_qa_cmd()

    assert "--api-base https://cpa.example.test/v1" in command
    assert "not-used-in-argv" not in command


def test_cpa_qa_cmd_routes_judge_to_luna_responses(monkeypatch):
    """2026-07-10: the semantic-QA judge lane runs gpt-5.6-luna on /responses
    (structured verdict = the doc-exact luna lane; gpt-5.x misroute on chat)."""
    monkeypatch.setattr(runner, "load_env_file", lambda _p: {"CPA_BASE_URL": "https://cpa.test/v1"})
    cmd = runner.cpa_qa_cmd()
    assert "--model gpt-5.6-luna" in cmd
    assert "--api-mode responses" in cmd
    assert "--max-tokens 16000" in cmd
    assert "--retries 3" in cmd
    assert "--api-base https://cpa.test/v1" in cmd
