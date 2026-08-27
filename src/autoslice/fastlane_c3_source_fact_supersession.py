"""C3-only current-input binding for its sealed no-provider source-fact closure."""
from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from src.autoslice.repository_asset_authority import require_repository_asset_authority

CANDIDATE_ID = "auto_220021_561_670"
RECORDING_DATE = "2026-08-13"
DECISION = "FASTLANE_C3_TERMINAL_SOURCE_FACT_SUPERSESSION"
SCHEMA = "fastlane-c3-terminal-source-fact-supersession.v1"
ASSET = Path("assets/lidousha/fastlane_c3_source_fact_supersession/auto_220021_561_670.v1.json")
OUTER_RAW_SHA256 = "sha256:72ba250c3c270016251fb25bb88550650b0f64c570f1df456bd2857c949960bd"
OUTER_RECEIPT_SHA256 = "sha256:134d68073041638898940626a660f8282fbe92b1f13fe10c6f1a17fb3a74d216"
HISTORICAL_RECEIPT_SHA256 = "sha256:2e9eef6618d4b21146d2a071ac535be9342cb5c5b4be67f02c582596b223f31d"
LINE947 = {
    "session_id": "0df2296b-500a-4681-ab5e-6fb46dc39579",
    "line": 947,
    "timestamp": "2026-08-19T00:08:52.249Z",
    "uuid": "555195ed-ec18-418d-a311-558f7e54291f",
    "raw_line_sha256": "sha256:e64d4409aaf36193c27f3d67cd8e3fae69a6d3ae543a29a6c26f57c77d61c2aa",
    "decoded_content_sha256": "sha256:0e0e69e54fc06c88296536c6dfbca947181170873529c5de508a2af39aa93f6b",
}
REINFORCEMENTS = (
    {"line": 1643, "raw_line_sha256": "sha256:2269c653fa6be7fb0c20df98a7348d5f5c57176e3e41fe80eb13b85c39307609"},
    {"line": 1745, "raw_line_sha256": "sha256:7f97b7f8b6a7ca9cad7f54e02836a185b41c5ea158d70cb31f0867a174f13329"},
)

class C3SourceFactSupersessionError(ValueError): pass

def _canon(value: object) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

def _text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()

def _file(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()

def _load(repo_root: Path) -> dict[str, Any]:
    path = repo_root / ASSET
    raw = path.read_bytes()
    require_repository_asset_authority(repo_root=repo_root, relative_path=ASSET, observed_bytes=raw)
    value = json.loads(raw)
    if not isinstance(value, dict) or set(value) != {"schema_version", "candidate_id", "recording_date", "authority", "authority_sha256"}:
        raise C3SourceFactSupersessionError("C3_SOURCE_FACT_SUPERSESSION_ASSET_SCHEMA")
    seal = value.pop("authority_sha256")
    if not isinstance(seal, str) or _canon(value) != seal:
        raise C3SourceFactSupersessionError("C3_SOURCE_FACT_SUPERSESSION_ASSET_SEAL")
    if value["schema_version"] != SCHEMA or value["candidate_id"] != CANDIDATE_ID or value["recording_date"] != RECORDING_DATE:
        raise C3SourceFactSupersessionError("C3_SOURCE_FACT_SUPERSESSION_IDENTITY")
    if not isinstance(value["authority"], dict):
        raise C3SourceFactSupersessionError("C3_SOURCE_FACT_SUPERSESSION_AUTHORITY")
    value["authority_sha256"] = seal
    return value


def load_authority(*, repo_root: Path) -> dict[str, Any]:
    """Load the repository-sealed line947 supersession authority."""
    return _load(repo_root)


def _rebuild_c3_source_fact_supersession(*, repo_root: Path, outer_receipt: Mapping[str, object], candidate_id: str, recording_date: str, title: str, selection_hook: str, final_transcript: str, clip_context_prompt: str, selection_scorecard: object, speaker_evidence: object, speaker_finalization_sha256: str, final_reviewed_srt_path: Path, replay_plan: object | None, clip_context_prompt_sha256: str | None = None) -> dict[str, Any]:
    asset = _load(repo_root); a = asset["authority"]
    # C3's date is lane geometry, never a story-contract convenience field.
    # A direct caller may omit a plan only for legacy validation; a supplied
    # plan must be the exact C3 plan and is authoritative over all inputs.
    if replay_plan is not None and (
        getattr(replay_plan, "candidate_id", None) != CANDIDATE_ID
        or getattr(replay_plan, "date", None) != RECORDING_DATE
        or candidate_id != replay_plan.candidate_id
        or recording_date != replay_plan.date
    ):
        raise C3SourceFactSupersessionError("C3_SOURCE_FACT_SUPERSESSION_REPLAY_PLAN")
    if not isinstance(a, Mapping) or candidate_id != CANDIDATE_ID or recording_date != RECORDING_DATE or not final_reviewed_srt_path.is_file() or final_reviewed_srt_path.is_symlink():
        raise C3SourceFactSupersessionError("C3_SOURCE_FACT_SUPERSESSION_INPUT")
    historical = outer_receipt.get("historical_keep_receipt")
    terminal = outer_receipt.get("fastlane_terminal_source_fact_preservation")
    outer_body = dict(outer_receipt); outer_seal = outer_body.pop("receipt_sha256", None)
    historical_body = dict(historical) if isinstance(historical, Mapping) else {}; historical_seal = historical_body.pop("receipt_sha256", None)
    if not isinstance(historical, Mapping) or not isinstance(terminal, Mapping) or outer_receipt.get("decision") != "OPERATOR_FASTLANE_TERMINAL_SOURCE_FACT_PRESERVATION" or outer_receipt.get("status") != "PASS" or outer_seal != OUTER_RECEIPT_SHA256 or _canon(outer_body) != outer_seal or historical_seal != HISTORICAL_RECEIPT_SHA256 or _canon(historical_body) != historical_seal or historical.get("status") != "PASS" or historical.get("decision") != "KEEP":
        raise C3SourceFactSupersessionError("C3_SOURCE_FACT_SUPERSESSION_OUTER")
    computed_prompt_sha = _text(clip_context_prompt)
    if clip_context_prompt_sha256 is not None and clip_context_prompt_sha256 != computed_prompt_sha:
        raise C3SourceFactSupersessionError("C3_SOURCE_FACT_SUPERSESSION_PROMPT_BINDING_DRIFT")
    expected = {"outer_receipt_sha256": OUTER_RAW_SHA256, "outer_receipt_self_seal": OUTER_RECEIPT_SHA256, "historical_keep_receipt_sha256": historical.get("receipt_sha256"), "historical_keep_self_seal": HISTORICAL_RECEIPT_SHA256, "claude_line947": LINE947, "claude_reinforcements": list(REINFORCEMENTS), "title_sha256": _text(title), "selection_hook_sha256": _text(selection_hook), "final_transcript_sha256": _text(final_transcript), "clip_context_prompt_sha256": prompt_sha, "selection_scorecard_sha256": _canon(selection_scorecard), "speaker_evidence_sha256": _canon(speaker_evidence), "speaker_finalization_sha256": speaker_finalization_sha256, "final_reviewed_srt_sha256": _file(final_reviewed_srt_path), "line947_authority": "IVAN_CLAUDE_LINE947_EXHAUSTIVE", "provider_call_required": False, "subtitle_text_mutation_authorized": False, "state_mutation_authorized": False, "deploy_authorized": False, "upload_authorized": False}
    if dict(a) != expected or terminal.get("candidate_id") != CANDIDATE_ID or terminal.get("status") != "VALID" or terminal.get("historical_keep_receipt_sha256") != historical.get("receipt_sha256"):
        raise C3SourceFactSupersessionError("C3_SOURCE_FACT_SUPERSESSION_BINDING_DRIFT")
    consumption = {"schema_version": SCHEMA, "status": "VALID", "candidate_id": CANDIDATE_ID, "recording_date": RECORDING_DATE, "authority_sha256": asset["authority_sha256"], **expected}
    result: dict[str, Any] = {"schema_version": "lidousha-source-fact-review.v1", "status": "PASS", "decision": DECISION, "original_selection_hook": historical.get("original_selection_hook"), "original_title": historical.get("original_title"), "final_selection_hook": selection_hook, "final_title": title, "historical_provider_receipt": copy.deepcopy(dict(historical)), "sealed_outer_terminal_receipt": copy.deepcopy(dict(outer_receipt)), "c3_terminal_source_fact_supersession": consumption}
    result["receipt_sha256"] = _canon(result)
    return result

def build_c3_source_fact_supersession(*, replay_plan: object, **kwargs: Any) -> dict[str, Any]:
    """Mint C3's typed receipt only from an explicit immutable ReplayPlan."""
    if replay_plan is None:
        raise C3SourceFactSupersessionError("C3_SOURCE_FACT_SUPERSESSION_REPLAY_PLAN")
    return _rebuild_c3_source_fact_supersession(replay_plan=replay_plan, **kwargs)

def validate_c3_source_fact_supersession(review: object, **kwargs: Any) -> bool:
    if not isinstance(review, Mapping) or not isinstance(review.get("historical_provider_receipt"), Mapping) or not isinstance(review.get("sealed_outer_terminal_receipt"), Mapping): return False
    try:
        # Revalidation rebuilds a persisted immutable receipt; it is not a
        # minting surface and therefore cannot be used to bypass ReplayPlan.
        expected = _rebuild_c3_source_fact_supersession(
            outer_receipt=review["sealed_outer_terminal_receipt"], replay_plan=None, **kwargs,
        )
    except (OSError, TypeError, ValueError, KeyError): return False
    return dict(review) == expected
