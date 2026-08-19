from __future__ import annotations

import copy
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Mapping

import pytest

import src.autoslice.publish_staging as publish_staging
import src.autoslice.source_fact_review as source_fact_review
import src.autoslice.source_fact_staging as source_fact_staging
from src.autoslice.deterministic_text_surface_resolution import (
    ADJUDICATION_PROMPT_FILENAME,
    ASSET_DIRECTORY,
    AUTHORITY_FILENAME,
    CANDIDATE_ID,
    CLIP_CONTEXT_PROMPT_SHA256,
    ENTITY_CONTEXT_SHA256,
    ENTITY_PROJECTION_SHA256,
    EXACT_COMBINED_SHA256,
    EXACT_HOOK,
    EXACT_TITLE,
    FAILED_RECEIPT_FILENAME,
    FAILED_SOURCE_FACT_RECEIPT_SHA256,
    FINAL_TRANSCRIPT_SHA256,
    OLD_HOOK,
    OLD_TITLE,
    SOURCE_BASENAME,
    SOURCE_END_MS,
    SOURCE_SHA256,
    SOURCE_START_MS,
    SPEAKER_EVIDENCE_SHA256,
    SELECTION_SCORECARD_SHA256,
    DeterministicTextSurfaceResolutionError,
    canonical_sha256,
    combined_surface_sha256,
    consume_deterministic_text_surface_authority,
    load_deterministic_text_surface_authority,
    text_sha256,
    validate_deterministic_text_surface_document,
)
from src.autoslice.review_evidence import SourceCue
from src.autoslice.story_contract import build_story_contract


ROOT = Path(__file__).resolve().parents[2]
AUTHORITY_PATH = ROOT / ASSET_DIRECTORY / AUTHORITY_FILENAME
FAILED_RECEIPT_PATH = ROOT / ASSET_DIRECTORY / FAILED_RECEIPT_FILENAME
ADJUDICATION_PROMPT_RELATIVE = ASSET_DIRECTORY / ADJUDICATION_PROMPT_FILENAME
SRT_RELATIVE = Path(f"assets/lidousha/reviewed_subtitle_baselines/{CANDIDATE_ID}.reviewed.srt")
MANIFEST_RELATIVE = Path(
    f"assets/lidousha/reviewed_subtitle_baselines/{CANDIDATE_ID}.subtitle-baseline.v1.json"
)
ENTITY_RELATIVE = Path(
    f"assets/lidousha/candidate_entity_projections/{CANDIDATE_ID}.entity-projection.v1.json"
)

_FROZEN_SELECTION_SCORECARD: dict[str, object] = {
    "dimensions": {
        "audience_salience": 4,
        "comedic_payoff": 4,
        "lidousha_centrality": 4,
        "persona_reversal": 3,
        "relationship_interaction": 4,
        "self_contained": 4,
        "stance_intensity": 3,
    },
    "effective_score": 91.5,
    "end_cue": 523,
    "fatigue_penalty": 0.0,
    "raw_score": 92.5,
    "reason_codes": [],
    "requested_tier": 1,
    "schema_version": "lidousha-selection-scorecard.v1",
    "start_cue": 488,
    "status": "VALID",
    "tier": 1,
    "tier_basis": "cp_positioning",
    "tier_evidence_cues": [488, 499, 503, 505, 507, 519],
    "tier_reason": (
        "从弹幕提问跳舞对象触发，完整讲述小李与新SII公主抱、第一次、负责、截图赖上这一串暧昧关系拉扯，且有明确笑点收束。"
    ),
    "uncertainty_penalty": 1.0,
    "weights": {
        "audience_salience": 15,
        "comedic_payoff": 10,
        "lidousha_centrality": 25,
        "persona_reversal": 10,
        "relationship_interaction": 15,
        "self_contained": 5,
        "stance_intensity": 20,
    },
}


def _document() -> dict[str, object]:
    return json.loads(AUTHORITY_PATH.read_text(encoding="utf-8"))


def _failed_receipt() -> dict[str, object]:
    return json.loads(FAILED_RECEIPT_PATH.read_text(encoding="utf-8"))


def _rehash(document: dict[str, object]) -> None:
    document.pop("authority_sha256", None)
    document["authority_sha256"] = canonical_sha256(document)


def _final_transcript(reviewed_srt_path: Path) -> str:
    text = reviewed_srt_path.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
    cues = []
    for block in re.split(r"\n{2,}", text.strip()):
        lines = block.splitlines()
        cues.append("\n".join(lines[2:]).strip())
    return "\n".join(cue for cue in cues if cue)


def _source_cues(reviewed_srt_path: Path) -> list[SourceCue]:
    return [
        SourceCue(
            cue_id=str(index),
            source_start_ms=(index - 1) * 1_000,
            source_end_ms=index * 1_000,
            text=text,
            language="zh",
        )
        for index, text in enumerate(
            _final_transcript(reviewed_srt_path).splitlines(),
            start=1,
        )
    ]


def _sealed_repo(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name
    for relative in (
        ASSET_DIRECTORY / AUTHORITY_FILENAME,
        ASSET_DIRECTORY / FAILED_RECEIPT_FILENAME,
        ASSET_DIRECTORY / ADJUDICATION_PROMPT_FILENAME,
        SRT_RELATIVE,
        MANIFEST_RELATIVE,
        ENTITY_RELATIVE,
    ):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=tests",
            "-c",
            "user.email=tests@example.invalid",
            "commit",
            "-qm",
            "seal deterministic authority",
        ],
        check=True,
    )
    return root


def _consume(
    root: Path,
    *,
    reviewed_srt_path: Path | None = None,
    final_transcript: str | None = None,
    clip_context_prompt: str | None = None,
    selection_scorecard: object = None,
    entity_context: object = None,
) -> dict[str, object]:
    authority = load_deterministic_text_surface_authority(CANDIDATE_ID, root=root)
    assert authority is not None
    repository_srt_path = root / SRT_RELATIVE
    return consume_deterministic_text_surface_authority(
        authority,
        candidate_id=CANDIDATE_ID,
        source_recording_basename=SOURCE_BASENAME,
        source_sha256=SOURCE_SHA256,
        absolute_source_start_ms=SOURCE_START_MS,
        absolute_source_end_ms=SOURCE_END_MS,
        reviewed_srt_path=reviewed_srt_path or repository_srt_path,
        speaker_evidence=copy.deepcopy(authority.document["uniform_host_authority"]["evidence"]),
        failed_source_fact_review=copy.deepcopy(authority.failed_source_fact_receipt),
        original_title=OLD_TITLE,
        original_selection_hook=OLD_HOOK,
        final_transcript=(
            final_transcript
            if final_transcript is not None
            else _final_transcript(repository_srt_path)
        ),
        clip_context_prompt=(
            clip_context_prompt
            if clip_context_prompt is not None
            else authority.adjudication_prompt
        ),
        selection_scorecard=(
            selection_scorecard
            if selection_scorecard is not None
            else copy.deepcopy(_FROZEN_SELECTION_SCORECARD)
        ),
        entity_context=(
            entity_context
            if entity_context is not None
            else copy.deepcopy(authority.failed_source_fact_receipt["entity_context"])
        ),
    )














































