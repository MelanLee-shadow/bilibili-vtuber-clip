"""Unit tests for the unattended runner's pure helpers (first-real-run lessons)."""
import json
import hashlib

import scripts.free_session_autoslice as runner
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


def test_safe_name_sanitizes_and_falls_back():
    assert safe_name("百合是工作？/她当场*不买书", "cid") == "百合是工作？她当场不买书"
    assert safe_name("", "cid") == "cid"
    assert len(safe_name("很长" * 40, "cid")) <= 18


def test_prioritize_caps_talk_and_songs_with_reasons():
    state = {
        "picks": [], "songs": [],
        "pending_talk": [
            {"segment_path": f"/rec/seg{i % 2}.mp4", "start_ms": i * 1000, "end_ms": i * 1000 + 30_000,
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


def test_song_delivery_rule_final():
    """Ivan 2026-07-10 最终规则：至多2个按弹幕排序（prioritize/refill 管）；
    是歌+唱完整就交付；语义判定只是参考。没唱完整(SONG_PARTIAL)不强行切。"""
    semantic_noise = ["END_BOUNDARY_LOW", "CPA_SEMANTIC_INCOMPLETE",
                      "VIEWER_CONTEXT_INCOMPLETE", "ADVISORY_NO_NATURAL_CLOSURE"]
    assert runner.song_delivery_ok(True, semantic_noise) is True   # 语义码不拦
    assert runner.song_delivery_ok(True, []) is True
    assert runner.song_delivery_ok(True, semantic_noise + ["SONG_PARTIAL"]) is False  # 没唱完整不切
    assert runner.song_delivery_ok(False, []) is False             # 不是歌不切
    assert runner.song_delivery_ok(True, None) is True


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


def test_write_reports_quarantine_shows_red_flags(tmp_path, monkeypatch):
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
    assert "1 条 ⚠quarantine" in text

def test_record_is_song_recognizes_non_lrc_songs():
    """7/9 实锤：日语歌《ただそばにいて》LRC 钉歌失败(song_boundary/alignment 全空)，
    但召回记录是 semanticsong_* —— 它是歌，必须参与交付竞争。"""
    assert runner.record_is_song({"candidate_id": "semanticsong_15000_170540", "source_context_job": {}})
    assert runner.record_is_song({"candidate_id": "x", "source_context_job": {"song_boundary": {"a": 1}}})
    assert runner.record_is_song({"candidate_id": "x", "source_context_job": {"lyrics_alignment": {"m": 0.9}}})
    assert not runner.record_is_song({"candidate_id": "semantictalk_3320_50570_ctxexp", "source_context_job": {}})
    assert not runner.record_is_song({})
