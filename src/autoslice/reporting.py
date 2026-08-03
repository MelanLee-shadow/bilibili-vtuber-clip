"""Human-readable and machine-readable autoslice batch reports."""

from __future__ import annotations

import time
from collections.abc import Mapping
from pathlib import Path

from src.autoslice.cover_route_evidence import validate_cover_route_decision
from src.autoslice.candidate_selection import (
    _exact_talk_contract_ids,
    exact_talk_contract_closure,
)
from src.autoslice.runner_proxy import RunnerProxy
from src.autoslice.publication_reconciliation import (
    publication_row_is_verified,
)


_runner = RunnerProxy()


def _candidate_id(row: dict) -> str:
    return str(row.get("candidate_id") or row.get("cid") or "")


def _current_compliant_delivery(row: dict) -> bool:
    return publication_row_is_verified(row) or (
        row.get("status") in _runner.DELIVERED_TALK_STATUSES
        and row.get("bundle_lifecycle") == "CURRENT"
        and row.get("bundle_compliance") == "COMPLIANT"
    )


def _current_talk_reserves(state: dict, attempts: list[dict]) -> list[dict]:
    """Project current reserves; never replay append-only selection prose."""

    terminal_ids = {_candidate_id(row) for row in attempts if _candidate_id(row)}
    exact_ids = set(_exact_talk_contract_ids(state))
    seen: set[str] = set()
    reserves: list[dict] = []
    for queue, disposition in (
        (state.get("pending_talk", []), "SELECTED_PENDING"),
        (state.get("talk_backlog", []), "RESERVE"),
        (state.get("talk_below_confidence_threshold", []), "BELOW_THRESHOLD"),
    ):
        for raw in queue if isinstance(queue, list) else []:
            if not isinstance(raw, dict):
                continue
            cid = _candidate_id(raw)
            if not cid or cid in terminal_ids or cid in seen:
                continue
            if exact_ids and cid not in exact_ids:
                continue
            row = dict(raw)
            row["candidate_disposition"] = disposition
            reserves.append(row)
            seen.add(cid)
    return reserves


def _score_label(row: dict) -> str:
    scorecard = row.get("selection_scorecard")
    if isinstance(scorecard, dict) and scorecard.get("status") == "VALID":
        return f"T{scorecard.get('tier')} / {scorecard.get('effective_score')}"
    confidence = row.get("confidence")
    return f"未量化 / conf={confidence if confidence is not None else '—'}"


def _report_cell(value: object) -> str:
    """Keep evidence readable without corrupting the Markdown table."""

    return " ".join(str(value or "").split()).replace("|", "／")


def _cover_route_projection(row: dict) -> dict[str, object]:
    """Project the executed cover route instead of the legacy readiness flag.

    ``AI_COVER_READY`` is an old publish-protocol status meaning that a cover
    artifact is ready.  It does not prove that image generation was selected,
    attempted, or used.  Human reports must derive those facts from the
    validated v2 route document and fail visibly back to the legacy status when
    that evidence is absent or malformed.
    """

    summary = row.get("summary") if isinstance(row.get("summary"), Mapping) else {}
    generation = row.get("cover_generation")
    if not isinstance(generation, Mapping):
        publish_staging = (
            row.get("publish_staging")
            if isinstance(row.get("publish_staging"), Mapping)
            else {}
        )
        generation = publish_staging.get("cover_generation")
    fallback = str(row.get("cover_status") or summary.get("cover_status") or "?")
    if not isinstance(generation, Mapping) or not validate_cover_route_decision(
        generation, allow_legacy_v1=False
    ):
        return {
            "label": f"旧就绪状态：{fallback}",
            "reason": "缺少可验证的 lidousha-cover-route-decision.v2；不能据此判断是否使用 AI",
            "selected": "UNKNOWN",
            "actual": "UNKNOWN",
            "execution_status": "UNKNOWN",
            "image_generation_attempted": None,
            "image_generation_used": None,
            "alternatives": [],
            "evidence_status": "MISSING_OR_INVALID",
        }

    route = generation["route_decision"]
    assert isinstance(route, Mapping)
    selected = str(route.get("selected_treatment") or "")
    actual = str(route.get("actual_treatment") or "")
    attempted = route.get("image_generation_attempted") is True
    used = route.get("image_generation_used") is True
    route_labels = {
        "screenshot_direct": "截图直出",
        "screenshot_polish": "截图轻调",
        "cpa_redraw": "AI 重绘",
    }
    if used:
        ai_label = "AI已调用并用于最终图"
    elif attempted:
        ai_label = "AI已调用但未用于最终图"
    else:
        ai_label = "AI未调用"
    selected_label = route_labels.get(selected, selected or "UNKNOWN")
    actual_label = route_labels.get(actual, actual or "UNKNOWN")
    if selected == actual:
        label = f"{actual_label}（{ai_label}）"
    else:
        label = f"{actual_label}（原选{selected_label}；{ai_label}）"
    alternatives = []
    for alternative in route.get("rejected_alternatives") or []:
        if not isinstance(alternative, Mapping):
            continue
        alternatives.append(
            {
                "treatment": str(alternative.get("treatment") or "UNKNOWN"),
                "reason": str(alternative.get("rejected_reason") or ""),
            }
        )
    return {
        "label": label,
        "reason": str(route.get("selected_rationale") or route.get("reason") or ""),
        "selected": selected,
        "actual": actual,
        "execution_status": str(route.get("execution_status") or ""),
        "image_generation_attempted": attempted,
        "image_generation_used": used,
        "alternatives": alternatives,
        "evidence_status": "VALID_V2",
    }


def _format_delivery_duration(pick: Mapping[str, object]) -> str:
    summary = pick.get("summary")
    summary_duration = (
        summary.get("duration_ms") if isinstance(summary, dict) else None
    )
    effective_duration = pick.get("effective_duration_ms")
    if isinstance(summary_duration, int) and not isinstance(
        summary_duration, bool
    ):
        duration_ms = summary_duration
    elif isinstance(effective_duration, int) and not isinstance(
        effective_duration, bool
    ):
        duration_ms = effective_duration
    else:
        duration_ms = int(pick.get("end_ms") or 0) - int(
            pick.get("start_ms") or 0
        )
    secs = max(0, duration_ms // 1000)
    return f"{secs // 60}:{secs % 60:02d}"


def write_reports(date: str, state: dict) -> None:
    delivery = _runner.profile_delivery_root() / date
    delivery.mkdir(parents=True, exist_ok=True)

    picks = [row for row in state.get("picks", []) if isinstance(row, dict)]
    exact_ids = set(_exact_talk_contract_ids(state))
    songs = state.get("songs", [])
    current_deliveries = [
        row
        for row in picks
        if _current_compliant_delivery(row)
        and (not exact_ids or _candidate_id(row) in exact_ids)
    ]
    stale_deliveries = [
        row
        for row in picks
        if row.get("status") in _runner.DELIVERED_TALK_STATUSES
        and row not in current_deliveries
    ]
    rejected_talk = [
        row
        for row in picks
        if row.get("status")
        in {
            "candidate_rejected",
            "boundary_unrepairable",
            "speaker_review_required",
            "speaker_evidence_insufficient",
            "failed",
            "quarantine",  # read-only compatibility for pre-2026-07-10 state
        }
        and (not exact_ids or _candidate_id(row) in exact_ids)
    ]
    reserves = _current_talk_reserves(state, picks)
    delivered_talk = len(current_deliveries)
    repaired = sum(1 for p in current_deliveries if p.get("boundary_repairs"))
    unrepairable = sum(1 for p in picks if p.get("status") == "boundary_unrepairable")
    quarantined = sum(1 for p in picks if p.get("status") == "quarantine")  # legacy states only
    delivered_songs = sum(
        1
        for s in songs
        if s.get("delivered") or publication_row_is_verified(s)
    )
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
        f"- 状态: **{state.get('status')}**",
        f"- 运行模式: **{state.get('run_mode', 'PRODUCTION')}** · "
        f"来源: **{state.get('source_authority', 'RECORDER')}** · "
        f"上传许可: **{'是' if state.get('upload_allowed') is True else '否'}**",
        f"- 包口径: “谈话成品”只投影 CURRENT + COMPLIANT + 已交付；"
        f"拒绝/候补/旧政策包互斥显示（旧包 {len(stale_deliveries)} 条）",
        f"- 交付实况: 谈话 **{delivered_talk} 交付**{('（' + '，'.join(talk_notes) + '）') if talk_notes else ''} / "
        f"歌 **{delivered_songs} 交付** · {blocked_songs} 被完整性门拦截 · 共尝试 {len(songs)}",
        f"- 段: 完成 {len(state.get('segments_done', []))} / 死段 {len(state.get('segments_dead', {}))} / 待产出 talk {len(state.get('pending_talk', []))} + song {len(state.get('pending_song', []))}",
        f"- 联动证据旁路: **{capture.get('status', 'NOT_RUN')}**（NO_TRIGGER 仅表示开发旁路未触发，绝不等于非联动） · "
        f"未来候选场 {capture.get('candidate_session_count', 0)}/"
        f"{capture.get('candidate_session_quota', 5)}（仅未标注开发证据，不代表已确认联动或可训练）",
        f"- 会话关系权威: **{(state.get('session_relation_authority') or {}).get('state', 'UNKNOWN') if isinstance(state.get('session_relation_authority'), dict) else 'UNKNOWN'}**",
        "",
        "## 谈话成品（仅当前合规交付）",
        "",
        "| 成品 | 时长 | 标题 | 选片理由(hook) | 量化分 | 收束句 | 边界 | 封面实际路线 | 路由理由 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for pick in current_deliveries:
        s = pick.get("summary") or {}
        repairs = pick.get("boundary_repairs") or []
        status_mark = f"（边界自修复×{len(repairs)}）" if repairs else ""
        cover_route = _cover_route_projection(pick)
        lines.append(
            f"| `{_runner.safe_name(pick.get('hook',''), pick.get('candidate_id','?'))}`{status_mark} "
            f"| {_format_delivery_duration(pick)} "
            f"| {pick.get('title') or '(未生成)'} "
            f"| {pick.get('hook') or '(兜底lane无理由)'} "
            f"| {_score_label(pick)} "
            f"| {s.get('closure_sentence') or '?'} "
            f"| {s.get('boundary_verdict') or '?'} "
            f"| {_report_cell(cover_route['label'])} "
            f"| {_report_cell(cover_route['reason'])} |"
        )
    if not current_deliveries:
        lines.append("| — | — | （无当前合规交付） | — | — | — | — | — | — |")

    if exact_ids:
        closure = exact_talk_contract_closure(state)
        lines += [
            "",
            "## 精确恢复契约闭环（发布真值）",
            "",
            f"- 闭环状态: **{closure['status']}**；只有 COMPLETE 才允许批次为 `review_ready`。",
            "",
            "| candidate | 唯一处置 | 尝试 | 待处理 | 当前状态 | 合规 |",
            "|---|---|---|---|---|---|",
        ]
        for row in closure["rows"]:
            assert isinstance(row, dict)
            lines.append(
                f"| `{row.get('candidate_id')}` | {row.get('disposition')} | "
                f"{row.get('attempt_count')} | {row.get('pending_count')} | "
                f"{row.get('status') or '—'} | "
                f"{row.get('bundle_lifecycle') or '—'} / "
                f"{row.get('bundle_compliance') or '—'} |"
            )
        outside = closure.get("outside_contract_attempt_ids") or []
        if outside:
            lines.append(
                "- 阻塞：活跃 picks 混入契约外 candidate："
                + "、".join(f"`{value}`" for value in outside)
            )

    if current_deliveries:
        lines += [
            "",
            "## 封面路线审计（以实际执行证据为准）",
            "",
            "> 内部兼容状态 `AI_COVER_READY` 只表示封面文件已就绪，不表示使用了 AI。"
            "以下结论只来自通过校验的 `lidousha-cover-route-decision.v2`；缺证时会显式显示 UNKNOWN。",
            "",
        ]
        route_labels = {
            "screenshot_direct": "截图直出",
            "screenshot_polish": "截图轻调",
            "cpa_redraw": "AI 重绘",
            "UNKNOWN": "UNKNOWN",
        }
        for pick in current_deliveries:
            cover_route = _cover_route_projection(pick)
            lines.append(
                f"- `{_candidate_id(pick) or '?'}`：**{_report_cell(cover_route['label'])}**；"
                f"证据={cover_route['evidence_status']}；执行状态="
                f"{_report_cell(cover_route['execution_status'])}；选中理由："
                f"{_report_cell(cover_route['reason'])}"
            )
            for alternative in cover_route["alternatives"]:
                assert isinstance(alternative, Mapping)
                treatment = str(alternative.get("treatment") or "UNKNOWN")
                lines.append(
                    f"  - 决策时未选 {route_labels.get(treatment, treatment)}："
                    f"{_report_cell(alternative.get('reason'))}"
                )

    if stale_deliveries:
        lines += [
            "",
            "## 旧版或合规状态未知的包（失败关闭，不是成品）",
            "",
            "| candidate | 原状态 | 生命周期 | 合规状态 | 处置 |",
            "|---|---|---|---|---|",
        ]
        for row in stale_deliveries:
            lines.append(
                f"| `{_candidate_id(row) or '?'}` | {row.get('status') or '—'} | "
                f"{row.get('bundle_lifecycle') or 'UNKNOWN'} | "
                f"{row.get('bundle_compliance') or 'UNKNOWN'} | "
                "不进入成品；需在当前政策下重新审计/重出 |"
            )

    if rejected_talk:
        lines += [
            "",
            "## 候选门禁拒绝（终态，不是成品）",
            "",
            "| candidate | hook | 门 | 原因 | 精确证据 |",
            "|---|---|---|---|---|",
        ]
        for row in rejected_talk:
            status = str(row.get("status") or "")
            evidence = row.get("gate_violation") or row.get("failure_evidence")
            if isinstance(evidence, dict):
                if evidence.get("token") or evidence.get("cue_index") is not None:
                    evidence_label = (
                        f"token={evidence.get('token') or '—'}; "
                        f"cue={evidence.get('cue_index') or '—'}; "
                        f"span={evidence.get('start_ms', '—')}-{evidence.get('end_ms', '—')}ms; "
                        f"missing={','.join(evidence.get('missing_witnesses') or []) or '—'}"
                    )
                else:
                    evidence_label = (
                        f"gate={evidence.get('gate') or '—'}; "
                        f"lane={evidence.get('lane') or '—'}; "
                        f"reason={evidence.get('reason_code') or '—'}"
                    )
            else:
                evidence_label = "LEGACY_REJECTION_EVIDENCE_INCOMPLETE"
            if status == "quarantine":
                reason = f"quarantine[{','.join(row.get('red_flags') or [])}]"
            elif status == "boundary_unrepairable":
                reason = "✗边界不可修复未交付"
            else:
                reason = row.get("rejection_reason") or row.get("failure_kind") or "—"
            lines.append(
                f"| `{_candidate_id(row) or '?'}` | {row.get('hook') or '—'} | "
                f"{row.get('failure_stage') or row.get('status')} | "
                f"{reason} | "
                f"{evidence_label} |"
            )

    if reserves:
        lines += [
            "",
            "## 当前谈话候补（动态投影；已交付/已拒绝不会重复出现）",
            "",
            "| candidate | 处置 | 时间 | hook | 量化分 |",
            "|---|---|---|---|---|",
        ]
        for row in reserves:
            lines.append(
                f"| `{_candidate_id(row)}` | {row['candidate_disposition']} | "
                f"{int(row.get('start_ms') or 0) // 1000}-{int(row.get('end_ms') or 0) // 1000}s | "
                f"{row.get('hook') or '—'} | {_score_label(row)} |"
            )
    lines += ["", f"## 歌切（每场至多 {_runner.MAX_SONGS_PER_SESSION} 个、本日汇总；按弹幕量排序；已发布歌曲跳过；仅{_runner.PROFILE_DISPLAY_NAME}本人演唱且完整才切；背景音乐/原曲播放/SONG_PARTIAL 均不交付；被拦不占配额、备份自动回填）", ""]
    if songs:
        lines += ["| 歌 | 弹幕 | 门判定 | 原因码 | 标题 | 交付 |", "|---|---|---|---|---|---|"]
        for song in songs:
            lines.append(
                f"| `{song.get('candidate_id')}` | x{song.get('danmaku', 0)} "
                f"| {song.get('decision') or '?'} | {','.join(song.get('reason_codes') or []) or '—'} "
                f"| {song.get('title') or '—'} "
                f"| {('✓ ' + Path(song['delivered']).name) if song.get('delivered') else ('✓ 已公开 ' + str(song.get('bvid'))) if publication_row_is_verified(song) else '未过门不交付'} |"
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
    # ``not_selected`` is legacy append-only event prose.  It is deliberately
    # not projected: a reserve promoted after a rejection used to remain there
    # forever and appear simultaneously as product and loser.
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
    if state.get("status") == "recovery_incomplete":
        lines += [
            "",
            "> ⚠ 精确恢复契约尚未闭环：至少一个指定候选缺失、失败、重复或不是 CURRENT+COMPLIANT；不得称为 review_ready。",
        ]
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
