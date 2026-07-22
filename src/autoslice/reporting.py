"""Human-readable and machine-readable autoslice batch reports."""

from __future__ import annotations

import time
from pathlib import Path

from src.autoslice.runner_proxy import RunnerProxy


_runner = RunnerProxy()


def write_reports(date: str, state: dict) -> None:
    delivery = _runner.profile_delivery_root() / date
    delivery.mkdir(parents=True, exist_ok=True)

    def fmt_dur(pick: dict) -> str:
        secs = max(0, (pick.get("end_ms", 0) - pick.get("start_ms", 0)) // 1000)
        return f"{secs // 60}:{secs % 60:02d}"

    picks = state.get("picks", [])
    songs = state.get("songs", [])
    delivered_talk = sum(1 for p in picks if p.get("status") in _runner.DELIVERED_TALK_STATUSES)
    repaired = sum(1 for p in picks if p.get("boundary_repairs"))
    unrepairable = sum(1 for p in picks if p.get("status") == "boundary_unrepairable")
    quarantined = sum(1 for p in picks if p.get("status") == "quarantine")  # legacy states only
    delivered_songs = sum(1 for s in songs if s.get("delivered"))
    blocked_songs = sum(1 for s in songs if s.get("status") == "blocked")
    capture = (
        state.get("collab_evidence_capture")
        if isinstance(state.get("collab_evidence_capture"), dict)
        else {}
    )
    talk_notes = []
    if repaired:
        talk_notes.append(f"{repaired} 条边界自修复后交付")
    if unrepairable:
        talk_notes.append(f"{unrepairable} 条边界不可修复未交付")
    if quarantined:
        talk_notes.append(f"{quarantined} 条旧版 quarantine(历史状态)")
    lines = [
        f"# {date} 无人值守自动切片批次",
        "",
        f"- 状态: **{state.get('status')}**  (runner v4; 上传永远关闭，全部成品仅供人工审查)",
        f"- 交付实况: 谈话 **{delivered_talk} 交付**{('（' + '，'.join(talk_notes) + '）') if talk_notes else ''} / "
        f"歌 **{delivered_songs} 交付** · {blocked_songs} 被完整性门拦截 · 共尝试 {len(songs)}",
        f"- 段: 完成 {len(state.get('segments_done', []))} / 死段 {len(state.get('segments_dead', {}))} / 待产出 talk {len(state.get('pending_talk', []))} + song {len(state.get('pending_song', []))}",
        f"- 联动证据旁路: **{capture.get('status', 'NOT_RUN')}** · "
        f"未来候选场 {capture.get('candidate_session_count', 0)}/"
        f"{capture.get('candidate_session_quota', 5)}（仅未标注开发证据，不代表已确认联动或可训练）",
        "",
        "## 谈话成品（审查要点：标题、选片理由、边界收束）",
        "",
        "| 成品 | 时长 | 标题 | 选片理由(hook) | 信心 | 收束句 | 边界 | 封面 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for pick in state.get("picks", []):
        s = pick.get("summary") or {}
        if pick.get("status") in ("ok", "review_ready"):
            repairs = pick.get("boundary_repairs") or []
            status_mark = f"（边界自修复×{len(repairs)}）" if repairs else ""
        elif pick.get("status") == "quarantine":  # legacy states only
            status_mark = f" ⚠quarantine[{','.join(pick.get('red_flags') or [])}]"
        elif pick.get("status") == "boundary_unrepairable":
            status_mark = " ✗边界不可修复未交付"
        else:
            status_mark = f" ⚠{pick.get('status')}"
        lines.append(
            f"| `{_runner.safe_name(pick.get('hook',''), pick.get('candidate_id','?'))}`{status_mark} "
            f"| {fmt_dur(pick)} "
            f"| {pick.get('title') or '(未生成)'} "
            f"| {pick.get('hook') or '(兜底lane无理由)'} "
            f"| {pick.get('confidence') if pick.get('confidence') is not None else '—'} "
            f"| {s.get('closure_sentence') or '?'} "
            f"| {s.get('boundary_verdict') or '?'} "
            f"| {pick.get('cover_status') or s.get('cover_status') or '?'} |"
        )
    lines += ["", f"## 歌切（每场至多 {_runner.MAX_SONGS_PER_SESSION} 个、本日汇总；按弹幕量排序；已发布歌曲跳过；仅{_runner.PROFILE_DISPLAY_NAME}本人演唱且完整才切；背景音乐/原曲播放/SONG_PARTIAL 均不交付；被拦不占配额、备份自动回填）", ""]
    if songs:
        lines += ["| 歌 | 弹幕 | 门判定 | 原因码 | 标题 | 交付 |", "|---|---|---|---|---|---|"]
        for song in songs:
            lines.append(
                f"| `{song.get('candidate_id')}` | x{song.get('danmaku', 0)} "
                f"| {song.get('decision') or '?'} | {','.join(song.get('reason_codes') or []) or '—'} "
                f"| {song.get('title') or '—'} "
                f"| {'✓ ' + Path(song['delivered']).name if song.get('delivered') else '未过门不交付'} |"
            )
    else:
        lines.append("(本场未检出/未产出歌切)")
    backlog = state.get("song_backlog", [])
    if backlog:
        def fmt_backlog(b) -> str:
            if not isinstance(b, dict):
                return str(b)  # legacy pre-v4 string entries
            return (
                f"{Path(b['segment_path']).name} {b['anchor_start_ms'] // 1000}-{b['anchor_end_ms'] // 1000}s "
                f"弹幕x{b.get('danmaku', 0)}: {b.get('hook') or b.get('preview', '')[:40]}"
            )
        lines += ["", "## 歌切候选备份（按弹幕排序；门拦截后自动回填的来源）", ""] + [f"- {fmt_backlog(b)}" for b in backlog]
    # 保序去重：历史 state 可能带有逐 tick 重复 append 的旧条目
    not_selected = list(dict.fromkeys(state.get("not_selected", [])))
    if not_selected:
        lines += ["", "## 落选谈话候选（供复核选片是否漏才）", ""] + [f"- {n}" for n in not_selected]
    dead = state.get("segments_dead", {})
    if dead:
        lines += ["", "## 死段（不再重试）", ""] + [f"- {k}: {v}" for k, v in dead.items()]
    if state.get("status") == "paused_cpa_down":
        lines += ["", "> ⚠ CPA 链路不可用，批次已暂停；cron 每 10 分钟自动重试，恢复后从断点续产。"]
    if state.get("status") == "source_incomplete":
        source_integrity = (
            state.get("source_integrity")
            if isinstance(state.get("source_integrity"), dict)
            else {}
        )
        issue_codes = [
            str(issue.get("code") or "SOURCE_INCOMPLETE")
            for issue in source_integrity.get("issues", [])
            if isinstance(issue, dict)
        ]
        lines += [
            "",
            "> ⚠ **源录像不完整，已禁止进入选片/完成态。** "
            + ("原因码：" + "、".join(issue_codes) if issue_codes else "需检查录像段终态。"),
        ]
    if state.get("status") == "no_delivery":
        lines += ["", "> ⚠ 本场 0 条交付（候选被门拦截/失败/耗尽）。这不是成功状态，需人工过目落选与拦截原因。"]
    (delivery / "AUTOSLICE_SUMMARY.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    report = _runner.BASE / "reports" / "latest.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(
        f"# autoslice runner 最新状态\n\n- 时间: {time.strftime('%Y-%m-%d %H:%M:%S %z')}\n"
        f"- 日期: {date}  状态: {state.get('status')}\n"
        f"- 谈话: {delivered_talk} 交付(自修复 {repaired}, 不可修复 {unrepairable}) / {len(picks)} 尝试 (pending {len(state.get('pending_talk', []))})\n"
        f"- 歌切: {delivered_songs} 交付 / {blocked_songs} 门拦 / {len(songs)} 尝试 (pending {len(state.get('pending_song', []))})\n"
        f"- 交付: {_runner.profile_delivery_root()}/{date}/ (Mac launchd 拉取)\n",
        encoding="utf-8",
    )
