"""Focused proof-closure fingerprints for the unattended runner."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any


def pipeline_fingerprint(
    *, repo_root: Path, profile_tool: Callable[[str], Path], channel_profile: Any,
    exclusions: set[str], speaker_authority: Callable[[], object],
    speaker_authority_errors: tuple[type[BaseException], ...],
) -> str:
    """Hash all deployed Talk-capable proof surfaces and provider authority."""

    hasher = hashlib.sha256()
    explicit = {
        "scripts/session_autoslice.py", "scripts/free_asr_client.py",
        "scripts/apply_subtitle_text_overrides.py", "scripts/cpa_semantic_qa_llm.py",
        "scripts/gemini_slice_jingting.py", "scripts/llm_via_cpa.sh",
        "scripts/produce_slice_package.py", "scripts/repair_reviewed_covers.py",
        "scripts/resume_frozen_talk_package.py", "scripts/run_auto_review_shadow_pipeline.py",
        "scripts/run_full_session_selector_cpa_shadow.py",
    }
    paths = [repo_root / relative for relative in explicit]
    paths.append(profile_tool("cover_regenerator"))
    paths.extend(channel_profile.fingerprint_paths(repo_root=repo_root))
    autoslice_src = repo_root / "src" / "autoslice"
    paths.extend(
        path for path in (autoslice_src.rglob("*.py") if autoslice_src.is_dir() else [])
        if path.relative_to(repo_root).as_posix() not in exclusions
    )

    def label(path: Path) -> str:
        try:
            return path.relative_to(repo_root).as_posix()
        except ValueError:
            return str(path)

    for path in (path for path in paths if not path.is_file()):
        hasher.update(label(path).encode("utf-8") + b"\0MISSING\0")
    for path in sorted((path for path in paths if path.is_file()), key=label):
        hasher.update(label(path).encode("utf-8") + b"\0")
        hasher.update(path.read_bytes())
        hasher.update(b"\0")
    for key in (
        "AUTOSLICE_SPEAKER_ROUTING_PROVIDER_COMMAND_JSON",
        "AUTOSLICE_SPEAKER_ROUTING_PROVIDER_NAME",
        "AUTOSLICE_SPEAKER_ROUTING_PROVIDER_ALGORITHM_ID",
        "AUTOSLICE_SPEAKER_ROUTING_PROVIDER_ARTIFACTS_JSON",
    ):
        hasher.update(b"runtime-config\0" + key.encode("utf-8") + b"\0")
        hasher.update(os.environ.get(key, "").encode("utf-8") + b"\0")
    try:
        authority = speaker_authority()
    except speaker_authority_errors as exc:
        hasher.update(f"provider-authority-unavailable:{type(exc).__name__}".encode())
    else:
        if authority is not None:
            hasher.update(json.dumps(authority, sort_keys=True, separators=(",", ":")).encode())
    return "sha256:" + hasher.hexdigest()


def song_pipeline_fingerprint(
    *, repo_root: Path, profile_id: str, channel_profile: Any,
    profile_tool: Callable[[str], Path], profile_asset_file: Callable[[str], Path],
    profile_asset_directory: Callable[[str], Path], policy: dict[str, object],
) -> str:
    """Hash only the Song proof closure, excluding Talk-only recovery code."""

    explicit = {
        "scripts/cpa_semantic_qa_llm.py", "scripts/free_asr_client.py",
        "scripts/gemini_slice_jingting.py", "scripts/llm_via_cpa.sh",
        "scripts/run_full_session_selector_cpa_shadow.py", "src/autoslice/agy_lrc_alignment.py",
        "src/autoslice/boundary_resolver.py", "src/autoslice/candidate_selection.py",
        "src/autoslice/channel_profile.py", "src/autoslice/content_evidence.py",
        "src/autoslice/cover_emote.py", "src/autoslice/cover_generation.py",
        "src/autoslice/cpa_semantic_qa.py", "src/autoslice/danmaku_evidence.py",
        "src/autoslice/delivery_recovery.py", "src/autoslice/full_session_candidate_selector.py",
        "src/autoslice/full_session_transcription.py", "src/autoslice/gemini_backup_policy.py",
        "src/autoslice/host_vocal_proof.py", "src/autoslice/jingting_chunker.py",
        "src/autoslice/llm_client.py", "src/autoslice/publish_staging.py",
        "src/autoslice/produce_dispatch.py", "src/autoslice/producer_batch_projection.py",
        "src/autoslice/producer_batch_runner_integration.py",
        "src/autoslice/producer_batch_transaction.py",
        "src/autoslice/producer_delivery_prepare.py",
        "src/autoslice/producer_delivery_transaction.py",
        "src/autoslice/published_song_history.py", "src/autoslice/render_qa.py",
        "src/autoslice/review_evidence.py", "src/autoslice/semantic_candidate_selector.py",
        "src/autoslice/source_context_executor.py", "src/autoslice/source_context_planner.py",
        "src/autoslice/source_integrity.py", "src/autoslice/style_profile.py",
        "src/autoslice/subtitle_rendering.py", "src/autoslice/title_policy.py",
        "src/autoslice/runner_state_writeback.py",
        "src/autoslice/upload_tag_policy.py", "src/autoslice/verified_io.py",
        "src/autoslice/visual_song_discovery.py",
    }
    paths: list[Path] = [repo_root / relative for relative in explicit]
    paths.extend((repo_root / "src" / "autoslice").glob("song_*.py"))
    if "emote_library" in channel_profile.asset_files:
        paths.append(profile_asset_file("emote_library"))
    paths.extend((
        repo_root / "profiles" / profile_id / "profile.json", profile_tool("cover_regenerator"),
        profile_asset_file("cover_identity_prompt"), profile_asset_file("known_songs"),
        profile_asset_file("published_songs"), profile_asset_file("persona"),
        profile_asset_file("slice_selection_metric"), profile_asset_file("title_policy"),
        profile_asset_file("title_style"), profile_asset_file("upload_tag_policy"),
        profile_asset_file("voiceprint_profile"),
    ))
    fonts = profile_asset_directory("fonts")
    paths.extend(fonts.rglob("*") if fonts.is_dir() else [])
    hasher = hashlib.sha256()
    hasher.update(b"song-pipeline-fingerprint.v1\0")
    for path in sorted(set(paths), key=str):
        try:
            relative = path.relative_to(repo_root).as_posix()
        except ValueError:
            relative = str(path)
        hasher.update(relative.encode("utf-8") + b"\0")
        hasher.update(path.read_bytes() if path.is_file() else b"MISSING")
        hasher.update(b"\0")
    hasher.update(json.dumps(policy, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    return "sha256:" + hasher.hexdigest()
