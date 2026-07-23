from pathlib import Path

import src.autoslice.reporting as reporting
from src.autoslice.cover_route_evidence import (
    build_cover_route_decision,
    record_cover_route_execution,
)


class _Runner:
    DELIVERED_TALK_STATUSES = {"ok", "review_ready"}
    MAX_SONGS_PER_SESSION = 1
    PROFILE_DISPLAY_NAME = "李豆沙"

    def __init__(self, root: Path) -> None:
        self.BASE = root
        self._delivery = root / "delivery"

    def profile_delivery_root(self) -> Path:
        return self._delivery

    @staticmethod
    def safe_name(text: str, fallback: str) -> str:
        return text or fallback


def _screenshot_direct_generation() -> dict[str, object]:
    story_contract = {
        "schema_version": "story-contract.v1",
        "selection_hook": "真实画面已经表达当面对质",
        "relation_state": "UNKNOWN",
        "participants": [],
    }
    generation: dict[str, object] = {
        "story_contract": story_contract,
        "title": "【李豆沙】当面对质",
        "cover_text": "当面对质",
        "method": "screenshot_direct",
        "cover_origin": "SOURCE_SCREENSHOT",
    }
    generation["route_decision"] = build_cover_route_decision(
        selected_treatment="screenshot_direct",
        selected_rationale="真实来源帧已经同时表达人物关系和冲突钩子",
        story_contract=story_contract,
        reference_authority={},
        decision_inputs={"composition_strength": "STRONG"},
        title=str(generation["title"]),
        cover_text=str(generation["cover_text"]),
    )
    record_cover_route_execution(
        generation,
        actual_treatment="screenshot_direct",
        execution_status="READY",
        image_generation_attempted=False,
        image_generation_used=False,
    )
    return generation


def test_report_projects_products_rejects_and_reserves_exclusively(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(reporting, "_runner", _Runner(tmp_path))
    state = {
        "status": "review_ready",
        "run_mode": "RECOVERY_REVIEW",
        "source_authority": "OFFICIAL_COMPLETE_REPLAY",
        "upload_allowed": False,
        "session_relation_authority": {"state": "CONFIRMED"},
        "picks": [
            {
                "candidate_id": "delivered",
                "status": "review_ready",
                "bundle_lifecycle": "CURRENT",
                "bundle_compliance": "COMPLIANT",
                "start_ms": 0,
                "end_ms": 60_000,
                "effective_duration_ms": 272_000,
                "summary": {"duration_ms": 300_400},
                "hook": "已交付",
                "title": "【李豆沙】已交付",
                "cover_status": "AI_COVER_READY",
                "cover_generation": _screenshot_direct_generation(),
            },
            {
                "candidate_id": "rejected",
                "status": "candidate_rejected",
                "start_ms": 70_000,
                "end_ms": 130_000,
                "hook": "被拒绝",
                "failure_stage": "foreign_source_transcription",
                "rejection_reason": "subtitle_authority_unresolved_backfilled",
                "gate_violation": {
                    "token": "look and Rollie",
                    "cue_index": 4,
                    "start_ms": 7330,
                    "end_ms": 11970,
                    "missing_witnesses": ["positive_source_audio_transcription"],
                },
            },
            {
                "candidate_id": "legacy-delivered",
                "status": "review_ready",
                "start_ms": 131_000,
                "end_ms": 139_000,
                "hook": "缺失包口径的旧交付",
            },
        ],
        "talk_backlog": [
            {"cid": "delivered", "hook": "旧重复，不应出现"},
            {
                "cid": "reserve",
                "start_ms": 140_000,
                "end_ms": 200_000,
                "hook": "当前候补",
                "confidence": 0.9,
            },
        ],
        "not_selected": ["已交付旧落选字符串"],
        "songs": [],
    }

    reporting.write_reports("2026-07-22", state)
    summary = (
        tmp_path / "delivery/2026-07-22/AUTOSLICE_SUMMARY.md"
    ).read_text(encoding="utf-8")
    products = summary.split("## 候选门禁拒绝", 1)[0]
    rejects = summary.split("## 候选门禁拒绝", 1)[1].split(
        "## 当前谈话候补", 1
    )[0]
    reserves = summary.split("## 当前谈话候补", 1)[1]

    assert "被拒绝" not in products
    assert "candidate_rejected" not in products
    assert "缺失包口径的旧交付" not in products.split(
        "## 旧版或合规状态未知的包", 1
    )[0]
    assert "legacy-delivered" in summary
    assert "UNKNOWN" in summary
    assert "look and Rollie" in rejects
    assert "当前候补" in reserves
    assert "旧重复，不应出现" not in reserves
    assert "已交付旧落选字符串" not in summary
    assert "NO_TRIGGER 仅表示开发旁路未触发，绝不等于非联动" in summary
    assert "会话关系权威: **CONFIRMED**" in summary
    assert "| 5:00 |" in summary
    assert "截图直出（AI未调用）" in summary
    assert "真实来源帧已经同时表达人物关系和冲突钩子" in summary
    assert "未选 截图轻调" in summary
    assert "未选 AI 重绘" in summary


def test_report_does_not_infer_ai_route_from_legacy_ready_status(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(reporting, "_runner", _Runner(tmp_path))
    state = {
        "status": "review_ready",
        "picks": [
            {
                "candidate_id": "legacy-ready",
                "status": "review_ready",
                "bundle_lifecycle": "CURRENT",
                "bundle_compliance": "COMPLIANT",
                "start_ms": 0,
                "end_ms": 30_000,
                "hook": "旧包",
                "cover_status": "AI_COVER_READY",
            }
        ],
        "songs": [],
    }

    reporting.write_reports("2026-07-22", state)
    summary = (
        tmp_path / "delivery/2026-07-22/AUTOSLICE_SUMMARY.md"
    ).read_text(encoding="utf-8")

    assert "旧就绪状态：AI_COVER_READY" in summary
    assert "不能据此判断是否使用 AI" in summary
    assert "证据=MISSING_OR_INVALID" in summary
