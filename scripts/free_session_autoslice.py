#!/usr/bin/env python3
"""Unattended post-stream auto-slice runner (runs ON the free host).

Ivan's goal (2026-07-05): when a 李豆沙 stream ends, free starts the FULL
canonical pipeline by itself — no human kick-off:

    stream end (blrec live_status via API)
      → per new segment: BCUT aggregate ASR transcript (ms timeline)
      → semantic recall candidate selection (CPA, viewer-perspective, with the
        curated slice-selection metric; deterministic fallback lanes if the
        LLM is down — zero-output is loud, never silent)
      → per-live-session top-N talk candidates + up to 1 new-to-channel song
      → produce_slice_package per candidate (BCUT+AGY+CPA text, final pronouns,
        sentence boundaries, CAM+++context speaker finalization, colour ASS
        burn, REAL CPA cover, 李豆沙-style title) / song LRC lane with the strict
        completeness gate
      → delivery under <repo>/lidousha/<date>/ + AUTOSLICE_SUMMARY.md with the
        selection reason (hook) and confidence per clip for human review
      → status report file (no chat/email; Mac pulls via launchd)

Upload stays OFF by design: this runner has no upload path at all and
produce_slice_package writes publish drafts with upload_enabled=false.

HARD LESSONS BAKED IN (first real run, 2026-07-06):
- **CPA health gate**: recordings can wait, garbage cannot be unshipped.  If
  the CPA chat lane is down (gpt-5.6/5.5/5.4 provider outages happen), the
  batch is DEFERRED (status=paused_cpa_down) and resumes on a later tick —
  clips are never produced with cid titles / cid-text covers.
- **Title is part of the product**: a pick whose title generation failed is
  NOT delivered; it stays pending and is retried on resume (bounded).
- **Dead segments**: blrec restart stubs (a few KB of mp4) and segments whose
  BCUT transcription fails twice are marked dead and never retried again (the
  first run retried a 2.9KB stub every 10 minutes forever).
- **Subtitle fonts**: the sapphire72 ASS names "Microsoft YaHei"; Linux needs
  a CJK fallback font installed (`apt install fonts-noto-cjk`) or every glyph
  burns as a tofu box.  Checked at startup, loud in the report if missing.
- **Songs**: semantic recall's per-segment candidate cap squeezes songs out,
  so a deterministic performance-detector supplement also feeds the song
  queue; final pick = top MAX_SONGS_PER_SESSION per live session by danmaku count.
- **Covers self-heal**: the CPA image lane fails independently of the chat
  lane (2026-07-06: gateway 400 "multiples of 16" blanked a whole batch's
  covers while titles/subtitles were fine).  A delivered clip without a cover
  gets a bounded cover-only repair pass (regenerate_lidousha_cover) on later
  ticks — never a re-produce, never silent (summary shows repair state).

RUNNER v4 (2026-07-09 external audit — "the control plane was lying"):
- **Source health first**: a dead/hung CloudDrive FUSE mount used to read as
  "no new recordings" and the heartbeat stayed green while the RECORDER's
  write path was broken.  Every tick now probes the mount (subprocess ls with
  timeout, hang-proof); failure → ALERT file + SOURCE_UNAVAILABLE heartbeat +
  do nothing.  A cron watchdog (free_mount_watchdog.sh) self-heals the mount.
- **Honest status words**: song gate BLOCK is `blocked`, never `ok`; a batch
  with zero deliveries is `no_delivery`, never `done`.  Delivered talk is
  `review_ready` — clean by construction: deterministic boundary red flags are
  SELF-REPAIRED inside produce_slice_package (Ivan 2026-07-10: unattended means
  fix-or-refuse, no deliver-and-ask-a-human quarantine), and an unrepairable
  boundary is `boundary_unrepairable` (no delivery; retried only after a
  relevant pipeline change, with a lifetime cap).
- **Budget = deliveries**: gate-blocked songs no longer consume the per-session
  song budget; the danmaku-sorted backlog backfills (bounded SONG_ATTEMPT_CAP).
- **Global selection + sealing**: talk picks are ranked globally by recall
  confidence (soft per-segment diversity cap) and selection only happens after
  the segment inventory is STABLE across ticks — late segments compete instead
  of arriving to spent quota.
- **Atomic state**: tmp+rename writes with .bak; corrupt state quarantines the
  file and BLOCKS the date (state_corrupt_blocked) instead of silently
  reprocessing from scratch.

Deployment (free):
    repo   /opt/bilive/autoslice/repo        (rsync of scripts/ src/ assets/)
    env    /opt/bilive/autoslice/cpa.env     (CPA_BASE_URL / CPA_API_KEY, 600)
    state  /opt/bilive/autoslice/state/<date>.json
    cron   */10 min: flock -n lock python3 scripts/free_session_autoslice.py --once
    kill   touch /opt/bilive/autoslice/DISABLED to pause everything
    deps   fonts-noto-cjk (subtitle rendering), ffmpeg, PIL, self-ssh key,
           /opt/bilive/autoslice/venv-diar + pinned CAM++ model/voiceprints
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.autoslice.channel_profile import load_channel_profile
from src.autoslice.host_vocal_proof import verify_host_vocal_proof_claim
from src.autoslice.song_repair import (
    AGY_AUDIO_LRC_OBSERVATION_SCHEMA_VERSION,
    LYRIC_VOCAL_ASSERTION_KEYS,
    derive_live_arrangement_completeness,
    load_audio_lrc_json_artifact,
    live_performance_failure_reason_codes,
    validate_audio_lrc_canonical_projection,
    validate_audio_lrc_execution_metadata,
    validate_live_performance_observation,
)
from src.autoslice.collab_evidence_capture import (
    CollabEvidenceCaptureError,
    WORKER_REQUEST_SCHEMA_VERSION,
    evaluate_trigger as evaluate_collab_capture_trigger,
    validate_worker_request_document,
)
from src.autoslice.speaker_finalizer import (
    SpeakerFinalizationError,
    validate_speaker_review_manifest_document,
)
from src.autoslice.speaker_session_router import (
    FAST_SOLO,
    PROVIDER_SANITIZED_ENVIRONMENT,
    REQUEST_SCHEMA_VERSION as SPEAKER_ROUTING_REQUEST_SCHEMA,
    ROUTER_POLICY_VERSION as SPEAKER_ROUTING_POLICY_VERSION,
    RUN_BINARY_FINALIZER,
    SpeakerRoutingError,
    build_provider_authority,
    generate_speaker_routing,
    routing_runtime_fingerprint,
    routing_policy_fingerprint,
    segment_binding_sha256,
    segment_stat_signature,
    validate_provider_authority,
)
from src.autoslice.visual_song_discovery import (
    VisualSongConfig,
    discover_visual_songs,
    normalize_visual_title,
    union_visual_song_candidates,
)

CHANNEL_PROFILE = load_channel_profile(REPO_ROOT)
PROFILE_ID = CHANNEL_PROFILE.profile_id
PROFILE_DISPLAY_NAME = CHANNEL_PROFILE.display_name
PROFILE_OUTPUT_DIRECTORY = CHANNEL_PROFILE.output_directory
PROFILE_HOST_SPEAKER_LABEL = CHANNEL_PROFILE.host_speaker_label
PROFILE_GUEST_SPEAKER_LABEL = CHANNEL_PROFILE.guest_speaker_label
HOST_VOCAL_PRESENT_DECISION = CHANNEL_PROFILE.decision("host_vocal_present")
HOST_VOCAL_ABSENT_DECISION = CHANNEL_PROFILE.decision("host_vocal_absent")
VERIFIED_HOST_SINGING_DECISION = CHANNEL_PROFILE.decision("verified_host_singing")
HOST_NOT_SINGING_REASON = CHANNEL_PROFILE.decision("host_not_singing_reason")
LYRIC_VOCAL_SUBJECT = CHANNEL_PROFILE.decision("lyric_vocal_subject")


def profile_asset_file(key: str) -> Path:
    return CHANNEL_PROFILE.asset_file(key, repo_root=REPO_ROOT)


def profile_asset_directory(key: str) -> Path:
    return CHANNEL_PROFILE.asset_directory(key, repo_root=REPO_ROOT)


def profile_delivery_root() -> Path:
    return CHANNEL_PROFILE.delivery_root_for(REPO_ROOT)


def profile_tool(key: str) -> Path:
    return CHANNEL_PROFILE.tool(key, repo_root=REPO_ROOT)


BASE = Path(os.environ.get("AUTOSLICE_BASE", "/opt/bilive/autoslice"))
# Ivan 2026-07-13: during the speaker data-accumulation phase every delivered
# clip keeps the single host (李豆沙) subtitle style and speaker uncertainty
# must never reject a delivery. "required"/"auto" stay available for the
# future re-enable decision.
SPEAKER_MODE = os.environ.get("AUTOSLICE_SPEAKER_MODE", "uniform_host")
if SPEAKER_MODE not in {"uniform_host", "required", "auto"}:
    SPEAKER_MODE = "uniform_host"
ROOM = os.environ.get("AUTOSLICE_ROOM", CHANNEL_PROFILE.room_id)
REC_ROOT = Path(
    os.environ.get(
        "AUTOSLICE_REC_ROOT",
        f"/root/clouddrive2/CloudNAS/CloudDrive/123云盘/live-streaming/{ROOM}",
    )
)
BLREC_PORT = int(os.environ.get("AUTOSLICE_BLREC_PORT", "22333"))
BILIVE_ENV = Path("/opt/bilive/.env")
CPA_ENV = BASE / "cpa.env"
HOST_VOCAL_PYTHON = Path(os.environ.get("AUTOSLICE_HOST_VOCAL_PYTHON", str(BASE / "venv-diar/bin/python")))
HOST_VOCAL_PROFILE = Path(
    os.environ.get(
        "AUTOSLICE_HOST_VOCAL_PROFILE",
        str(profile_asset_file("voiceprint_profile")),
    )
)
HOST_VOCAL_REFERENCE_DIR = Path(
    os.environ.get(
        "AUTOSLICE_HOST_VOCAL_REFERENCE_DIR",
        str(BASE / "voiceprints" / CHANNEL_PROFILE.voiceprint_reference_subdirectory),
    )
)
HOST_VOCAL_MODEL_DIR = Path(
    os.environ.get(
        "AUTOSLICE_HOST_VOCAL_MODEL_DIR",
        str(BASE / "models/campp"),
    )
)
MAX_TALK_PICKS = 5
TALK_ATTEMPT_CAP = 10  # reject unsafe content candidates and backfill, bounded
MAX_SONGS_PER_SESSION = 1  # Ivan 2026-07-16: 每场直播至多一个歌切；已发布歌曲不再出
MAX_SONGS_PER_DATE = MAX_SONGS_PER_SESSION  # compatibility alias for callers/tests
TALK_PER_SEGMENT_CAP = 2  # diversity guard on the GLOBAL confidence ranking; slack refills
SONG_ATTEMPT_CAP = 6  # per-pipeline-generation song attempts for one live session
SONG_LIFETIME_ATTEMPT_CAP = 18  # absolute session cap including superseded attempts;
                                # permits two self-healing generations after the initial run
SONG_INFRA_RETRY_CAP = 6
SONG_INFRA_RETRY_BASE_SECONDS = 15 * 60
SONG_INFRA_RETRY_MAX_SECONDS = 6 * 60 * 60
TALK_REPAIR_LIFETIME_RETRY_CAP = 3  # all retries of one already-selected talk
# A deployment fingerprint is provenance, not blanket authorization to replay
# every historical failure.  Ordinary cron maintenance begins at this horizon;
# older dates remain available to explicit/manual recovery code paths without
# being woken by a routine --once tick after unrelated pipeline changes.
AUTOMATIC_MAINTENANCE_NOT_BEFORE = os.environ.get(
    "AUTOSLICE_AUTOMATIC_MAINTENANCE_NOT_BEFORE", "2026-07-11"
)
SONG_TERMINAL_PERFORMER_REJECTION_CODES = frozenset(
    {
        "SONG_BACKGROUND_PLAYBACK_ONLY",
        HOST_NOT_SINGING_REASON,
    }
)
SONG_INFRA_TRANSIENT_REASON_CODES = frozenset(
    {
        "AGY_SOURCE_CONTEXT_RUNNER_FAILED",
        "AGY_QUOTA_EXHAUSTED",
        "AGY_EMPTY_OUTPUT",
        "AGY_FAILED_RC",
        "AGY_TIMEOUT",
        "AGY_AND_GEMINI_API_FAILED",
        "CPA_RATE_LIMITED",
        "CPA_MODEL_DOWN",
        "CPA_UPSTREAM_5XX",
        "CPA_UPSTREAM_TIMEOUT",
    }
)
# Delivered-to-review talk statuses.  "ok" (pre-2026-07-09) and "quarantine"
# (pre-2026-07-10 delivered-with-flags) are kept ONLY so old state files still
# count as delivered; new records are always review_ready — boundary red flags
# are self-repaired in produce_slice_package, and an unrepairable boundary is
# boundary_unrepairable (no delivery; fingerprint-gated bounded self-heal).
DELIVERED_TALK_STATUSES = {"ok", "review_ready", "quarantine"}
PER_SEGMENT_CANDIDATES = 4
MIN_SEGMENT_BYTES = 5_000_000  # blrec restart stubs are a few KB — dead on sight
BCUT_MAX_ATTEMPTS = 2
TITLE_MAX_ATTEMPTS = 3
COVER_REPAIR_MAX_ATTEMPTS = 3  # one attempt per tick → retries spread ~10min apart
COVER_REPAIR_LIFETIME_ATTEMPT_CAP = 9  # three bounded repair generations; never loop forever
MAX_PARALLEL_PRODUCE = 3  # slices are independent; produce them concurrently (each is
                          # network-bound on AGY/CPA/gpt-image-2, so a few in flight
                          # cut wall-clock ~3x; bounded by free CPU + CPA concurrency)
PIECE_PRE_MS = 10_000
PIECE_POST_MS = 32_000
BOUNDARY_CONTEXT_RETRY_POST_MS = 90_000
BOUNDARY_REPAIR_INITIAL_CAP_MS = 30_000
BOUNDARY_REPAIR_RETRY_CAP_MS = 60_000
SPEAKER_ROUTING_FINAL_TAIL_GUARD_MS = 1_000
SONG_WINDOW_PRE_MS = 15_000   # window must stay SONG-dominated or the in-window
SONG_WINDOW_POST_MS = 20_000  # recall reclassifies it as talk (smoke-proven at
                              # ±60/45s and ±180/150s); 15/20s matches the
                              # validated 虫儿飞 run.
# A recall window is still only an anchor.  If it identifies a song but cannot
# prove both LRC ends, retry once with enough original-source context for the
# boundary resolver to recover missed intro/tail audio.  This fixed the 7/9
# 《芽吹くとき》case where aggregate ASR began ~18s late and ended ~27s
# early; the old 15/20 source window physically excluded the true boundaries.
SONG_PROOF_RETRY_PRE_MS = 45_000
SONG_PROOF_RETRY_POST_MS = 45_000
SONG_ANCHOR_TRIM_MIN_MS = 20_000  # only retry on the danmaku-dense core when the
                                  # trim drops ≥20s of talk padding off an end
DATE_RX = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# Per-stage CPA model chains (2026-07-10, Ivan): sol ONLY where open-ended
# judgment is load-bearing — semantic recall (editorial pick over a 30-min
# transcript) and the single brand-critical title call (high effort, short
# prompt).  Terra (the everyday 5.5 successor) carries song hints: fuzzy
# world-knowledge recall from garbled ASR, NOT a known-good-shape task — and it
# is non-load-bearing anyway (known_songs fingerprint pinning + clean-line
# search are the authority; a wrong hint is discarded by the alignment gate).
# Luna carries cover art direction: a structured pick with a known good shape,
# high volume, deterministic fallback + judge guardrails — the doc-exact luna
# lane.  Every chain falls back gpt-5.5 → gpt-5.4.
CPA_CMD_DEEP = "bash scripts/llm_via_cpa.sh {prompt_file} {completion_file} 'gpt-5.6-sol gpt-5.5 gpt-5.4' medium"
CPA_CMD_TITLE = "bash scripts/llm_via_cpa.sh {prompt_file} {completion_file} 'gpt-5.6-sol gpt-5.5 gpt-5.4' high"
CPA_CMD_STANDARD = "bash scripts/llm_via_cpa.sh {prompt_file} {completion_file} 'gpt-5.6-terra gpt-5.5 gpt-5.4' medium"
CPA_CMD_STRUCTURED = "bash scripts/llm_via_cpa.sh {prompt_file} {completion_file} 'gpt-5.6-luna gpt-5.5 gpt-5.4' medium"
# The selector's --cpa-command is the semantic-QA JUDGE lane (request/response
# JSON contract), NOT a prompt/completion LLM template — canonical validated
# command per docs/spark/2026-06-30-future-live-e2e-runbook.md.  The selector
# does NOT run it through a shell, so the api-base must be substituted here
# (the key stays off the command line via --api-key-env).
# 2026-07-10 (Ivan): the judge moved to gpt-5.6-luna on the /responses route
# (structured verdict = the doc-exact luna lane; gpt-5.x misroute on chat).
# max-tokens 16000 keeps headroom for reasoning burn; --retries 3 absorbs the
# upstream empty-completion quirk.  Judge failure stays fail-closed (BLOCK,
# advisory-only for delivery since the 2026-07-10 song contract).


def pipeline_fingerprint() -> str:
    """Proof-closure fingerprint used to retry old recoverable BLOCKs.

    Hash every deployed code/config surface that can affect recall, boundary,
    lyrics, voice identity, packaging, or evidence authority.  The voiceprint
    profile contains the expected external model/reference digests; deployment
    refuses a runtime whose private assets disagree with that profile.
    """

    hasher = hashlib.sha256()
    explicit = {
        "scripts/free_session_autoslice.py",
        "scripts/free_asr_client.py",
        "scripts/apply_subtitle_text_overrides.py",
        "scripts/cpa_semantic_qa_llm.py",
        "scripts/gemini_slice_jingting.py",
        "scripts/llm_via_cpa.sh",
        "scripts/produce_slice_package.py",
        "scripts/repair_reviewed_covers.py",
        "scripts/resume_frozen_talk_package.py",
        "scripts/run_auto_review_shadow_pipeline.py",
        "scripts/run_full_session_selector_cpa_shadow.py",
    }
    paths = [REPO_ROOT / relative for relative in explicit]
    paths.append(profile_tool("cover_regenerator"))
    paths.extend(CHANNEL_PROFILE.fingerprint_paths(repo_root=REPO_ROOT))
    autoslice_src = REPO_ROOT / "src" / "autoslice"
    paths.extend(autoslice_src.rglob("*.py") if autoslice_src.is_dir() else [])

    def path_label(path: Path) -> str:
        try:
            return path.relative_to(REPO_ROOT).as_posix()
        except ValueError:
            return str(path)

    missing = [path for path in paths if not path.is_file()]
    for path in missing:
        hasher.update(path_label(path).encode("utf-8") + b"\0MISSING\0")
    paths = [path for path in paths if path.is_file()]
    for path in sorted(paths, key=path_label):
        relative = path_label(path)
        hasher.update(relative.encode("utf-8") + b"\0")
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
    # Wake recoverable work when configured provider bytes change at the same
    # path.  Invalid/missing authority is hashed as an explicit unavailable
    # state and remains fail-closed in prepare_speaker_routing.
    try:
        authority = _speaker_routing_provider_authority()
    except (OSError, TypeError, ValueError, SpeakerRoutingError) as exc:
        hasher.update(f"provider-authority-unavailable:{type(exc).__name__}".encode())
    else:
        if authority is not None:
            hasher.update(
                json.dumps(authority, sort_keys=True, separators=(",", ":")).encode()
            )
    return "sha256:" + hasher.hexdigest()


def song_pipeline_fingerprint() -> str:
    """Hash only surfaces that can change song proof or song delivery.

    The historical global fingerprint includes every talk subtitle/entity
    module and asset.  Using it for song BLOCK recovery made a talk-only entity
    fix requeue every old LRC failure and consume the paid Gemini fallback.
    Keep song recovery sensitive to its real proof closure while excluding
    talk-only ASR/entity authority.
    """

    explicit = {
        "scripts/cpa_semantic_qa_llm.py",
        "scripts/free_asr_client.py",
        "scripts/gemini_slice_jingting.py",
        "scripts/llm_via_cpa.sh",
        "scripts/run_full_session_selector_cpa_shadow.py",
        "src/autoslice/agy_lrc_alignment.py",
        "src/autoslice/boundary_resolver.py",
        "src/autoslice/candidate_selection.py",
        "src/autoslice/channel_profile.py",
        "src/autoslice/content_evidence.py",
        "src/autoslice/cover_generation.py",
        "src/autoslice/cpa_semantic_qa.py",
        "src/autoslice/danmaku_evidence.py",
        "src/autoslice/delivery_recovery.py",
        "src/autoslice/full_session_candidate_selector.py",
        "src/autoslice/full_session_transcription.py",
        "src/autoslice/gemini_backup_policy.py",
        "src/autoslice/host_vocal_proof.py",
        "src/autoslice/jingting_chunker.py",
        "src/autoslice/llm_client.py",
        "src/autoslice/publish_staging.py",
        "src/autoslice/published_song_history.py",
        "src/autoslice/render_qa.py",
        "src/autoslice/reporting.py",
        "src/autoslice/review_evidence.py",
        "src/autoslice/semantic_candidate_selector.py",
        "src/autoslice/source_context_executor.py",
        "src/autoslice/source_context_planner.py",
        "src/autoslice/source_integrity.py",
        "src/autoslice/style_profile.py",
        "src/autoslice/subtitle_rendering.py",
        "src/autoslice/title_policy.py",
        "src/autoslice/upload_tag_policy.py",
        "src/autoslice/verified_io.py",
        "src/autoslice/visual_song_discovery.py",
    }
    paths = [REPO_ROOT / relative for relative in explicit]
    paths.extend((REPO_ROOT / "src" / "autoslice").glob("song_*.py"))
    paths.extend(
        (
            REPO_ROOT / "profiles" / PROFILE_ID / "profile.json",
            profile_tool("cover_regenerator"),
            profile_asset_file("cover_identity_prompt"),
            profile_asset_file("known_songs"),
            profile_asset_file("published_songs"),
            profile_asset_file("persona"),
            profile_asset_file("slice_selection_metric"),
            profile_asset_file("title_policy"),
            profile_asset_file("title_style"),
            profile_asset_file("upload_tag_policy"),
            profile_asset_file("voiceprint_profile"),
        )
    )
    fonts = profile_asset_directory("fonts")
    paths.extend(fonts.rglob("*") if fonts.is_dir() else [])

    hasher = hashlib.sha256()
    hasher.update(b"song-pipeline-fingerprint.v1\0")
    for path in sorted(set(paths), key=lambda item: str(item)):
        try:
            relative = path.relative_to(REPO_ROOT).as_posix()
        except ValueError:
            relative = str(path)
        hasher.update(relative.encode("utf-8") + b"\0")
        hasher.update(path.read_bytes() if path.is_file() else b"MISSING")
        hasher.update(b"\0")
    policy = {
        "max_songs_per_session": MAX_SONGS_PER_SESSION,
        "song_attempt_cap": SONG_ATTEMPT_CAP,
        "song_lifetime_attempt_cap": SONG_LIFETIME_ATTEMPT_CAP,
        "song_infra_retry_cap": SONG_INFRA_RETRY_CAP,
        "song_infra_retry_base_seconds": SONG_INFRA_RETRY_BASE_SECONDS,
        "song_infra_retry_max_seconds": SONG_INFRA_RETRY_MAX_SECONDS,
        "song_window_pre_ms": SONG_WINDOW_PRE_MS,
        "song_window_post_ms": SONG_WINDOW_POST_MS,
        "song_proof_retry_pre_ms": SONG_PROOF_RETRY_PRE_MS,
        "song_proof_retry_post_ms": SONG_PROOF_RETRY_POST_MS,
        "song_anchor_trim_min_ms": SONG_ANCHOR_TRIM_MIN_MS,
        "song_terminal_performer_rejection_codes": sorted(
            SONG_TERMINAL_PERFORMER_REJECTION_CODES
        ),
        "song_infra_transient_reason_codes": sorted(
            SONG_INFRA_TRANSIENT_REASON_CODES
        ),
        "cpa_deep_command": CPA_CMD_DEEP,
        "cpa_title_command": CPA_CMD_TITLE,
        "cpa_standard_command": CPA_CMD_STANDARD,
        "cpa_structured_command": CPA_CMD_STRUCTURED,
    }
    hasher.update(
        json.dumps(policy, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    return "sha256:" + hasher.hexdigest()


def human_truth_mode() -> str:
    """Select whether reviewed candidate truth may enter generation inputs."""

    mode = os.environ.get("AUTOSLICE_HUMAN_TRUTH_MODE", "delivery").strip().lower()
    if mode not in {"delivery", "withheld"}:
        raise ValueError("AUTOSLICE_HUMAN_TRUTH_MODE must be delivery or withheld")
    return mode


def candidate_text_override_path(candidate_id: str) -> Path | None:
    """Return the one canonical candidate override, rejecting path indirection."""

    if not re.fullmatch(r"[A-Za-z0-9_-]{1,96}", str(candidate_id or "")):
        raise ValueError("unsafe candidate id for subtitle text override")
    if human_truth_mode() == "withheld":
        return None
    root = profile_asset_directory("subtitle_text_overrides")
    path = root / f"{candidate_id}.text.v1.json"
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise ValueError("candidate subtitle text override must be a regular non-symlink file")
    if path.resolve().parent != root.resolve():
        raise ValueError("candidate subtitle text override escapes its canonical asset root")
    return path


def candidate_subtitle_regression_path(candidate_id: str) -> Path | None:
    """Return the one canonical candidate regression gate, without indirection."""

    if not re.fullmatch(r"[A-Za-z0-9_-]{1,96}", str(candidate_id or "")):
        raise ValueError("unsafe candidate id for subtitle regression")
    if human_truth_mode() == "withheld":
        return None
    root = profile_asset_directory("subtitle_regressions")
    path = root / f"{candidate_id}.subtitle-regression.v1.json"
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise ValueError("candidate subtitle regression must be a regular non-symlink file")
    if path.resolve().parent != root.resolve():
        raise ValueError("candidate subtitle regression escapes its canonical asset root")
    return path


def candidate_speaker_override_path(candidate_id: str) -> Path | None:
    """Return the candidate's hash-bound speaker truth, withheld during blind tests."""

    if not re.fullmatch(r"[A-Za-z0-9_-]{1,96}", str(candidate_id or "")):
        raise ValueError("unsafe candidate id for speaker override")
    if human_truth_mode() == "withheld":
        return None
    root = profile_asset_directory("speaker_overrides")
    path = root / f"{candidate_id}.speaker.v1.json"
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise ValueError("candidate speaker override must be a regular non-symlink file")
    if path.resolve().parent != root.resolve():
        raise ValueError("candidate speaker override escapes its canonical asset root")
    return path


def talk_pipeline_fingerprint(candidate_id: str) -> str:
    """Base code/config proof plus only this talk's optional truth assets."""

    base = pipeline_fingerprint()
    truth_assets = [
        path
        for path in (
            candidate_text_override_path(candidate_id),
            candidate_subtitle_regression_path(candidate_id),
            candidate_speaker_override_path(candidate_id),
        )
        if path is not None
    ]
    # Preserve the historical base fingerprint for the overwhelmingly common
    # no-override case.  Adding/removing this candidate's truth asset still
    # changes/reverts its fingerprint without waking every legacy talk once.
    if not truth_assets:
        if human_truth_mode() == "withheld":
            hasher = hashlib.sha256()
            hasher.update(b"talk-pipeline-fingerprint.v4\0")
            hasher.update(base.encode("utf-8") + b"\0human_truth=withheld\0")
            return "sha256:" + hasher.hexdigest()
        return base
    hasher = hashlib.sha256()
    hasher.update(b"talk-pipeline-fingerprint.v5\0")
    hasher.update(base.encode("utf-8") + b"\0")
    hasher.update(str(candidate_id).encode("utf-8") + b"\0")
    for path in sorted(truth_assets, key=lambda item: item.relative_to(REPO_ROOT).as_posix()):
        relative = path.relative_to(REPO_ROOT).as_posix()
        hasher.update(relative.encode("utf-8") + b"\0")
        hasher.update(path.read_bytes())
        hasher.update(b"\0")
    return "sha256:" + hasher.hexdigest()


# ---- speaker routing / collab evidence capture (extracted 2026-07-14) ------
# Implementation lives in src/autoslice/speaker_routing_session.py; these
# wrappers rebuild the context from module globals AT CALL TIME so tests that
# monkeypatch BASE/log/child_env on this module keep steering the behaviour.
from src.autoslice.speaker_routing_session import (  # noqa: E402
    SPEAKER_ROUTING_SESSION_AUTHORITY_SCHEMA,
    SPEAKER_ROUTING_DATE_RE,
    RunnerContext as _SpeakerRunnerContext,
    _clear_speaker_routing_fields,
    _speaker_routing_provider_command,
    _speaker_routing_provider_authority,
    _sealed_speaker_inventory,
    _attach_speaker_routing_claim,
    _speaker_inventory_sha256,
    _speaker_session_authority_integrity,
    _load_speaker_session_authority,
    _speaker_session_state_from_authority,
    _speaker_session_state_matches_authority,
)
from src.autoslice import speaker_routing_session as _speaker_routing_session  # noqa: E402

from src.autoslice.verified_io import (  # noqa: E402
    _matches_sha256,
    _canonical_existing_path,
    _normalized_sha256,
    _read_json_object,
    _document_video_hash,
)
from src.autoslice import song_completion as _song_completion  # noqa: E402
from src.autoslice.song_completion import (  # noqa: E402
    MATERIALIZED_RECUT_SCHEMA_VERSION,
    VERIFIED_SONG_OUTPUT_BINDING_SCHEMA_VERSION,
    SONG_STREAM_CONTRACT_SCHEMA_VERSION,
    _expected_song_stream_contract,
    _expected_song_recut_command,
    _has_exact_av_streams,
)
from src.autoslice.song_lane import (  # noqa: E402
    _srt_cue_spans,
    _song_core_span,
    song_status,
    song_proof_retry_window,
    song_delivery_ok,
    fresh_song_selector_dir,
    song_window_media_path,
    classify_song_selector_transient,
    song_infra_retry_delay_seconds,
    song_review_retry_after_seconds,
    scheduled_song_retry_epoch,
    scheduled_talk_retry_epoch,
    scheduled_retry_epoch,
    produce_song,
)
from src.autoslice.published_song_history import (  # noqa: E402
    PublishedSongHistoryError,
    published_song_match as _published_song_match,
)
from src.autoslice.talk_lane import (  # noqa: E402
    danmaku_hints,
    danmaku_count_in,
    slice_srt,
    safe_name,
    last_json_block,
    recall_candidates,
    read_publish_meta,
    _speaker_review_manifest_state,
    read_speaker_review_meta,
    _speaker_evidence_insufficient_failure,
    classify_talk_failure,
    produce_talk,
)
from src.autoslice.session_discovery import (  # noqa: E402
    date_chat_jsonl_files,
    _clean_dian_ge_title,
    dian_ge_song_titles,
    visual_song_titles,
    known_song_titles,
    collect_song_name_candidates,
    visual_song_config_from_env,
    recording_session_id,
    annotate_state_sessions,
    discover_segments,
)
from src.autoslice.song_delivery import (  # noqa: E402
    VERIFIED_SONG_DELIVERY_SCHEMA_VERSION,
    SongDeliveryError,
    record_is_song,
    song_delivery_artifacts,
    _write_song_active_record,
    _commit_verified_song_package,
    _song_delivery_basename,
    _sha256_regular_file,
    _fsync_directory,
    _hidden_delivery_path,
    _stage_verified_copy,
    _stage_verified_bytes,
    _atomic_verified_song_delivery,
)
from src.autoslice.delivery_recovery import (  # noqa: E402
    requeue_recoverable_songs,
    _song_delivery_recovery_authority,
    recover_bound_song_deliveries,
    bind_song_delivery_recovery_authority,
    requeue_recoverable_talks,
)
from src.autoslice.candidate_selection import (  # noqa: E402
    session_sealed,
    song_delivery_budget,
    _remember_song_quarantine_interval,
    _note_not_selected,
    quarantine_overlapping_talk_candidates,
    backlog_has_eligible_session_work,
    refill_songs,
    prioritize,
)
from src.autoslice.cover_repair import (  # noqa: E402
    COVER_TRANSACTION_SCHEMA_VERSION,
    _validate_repaired_cover_generation,
    _active_cover_documents,
    _active_song_delivery_manifest,
    _updated_song_delivery_manifest,
    _cover_reason_codes_without_transient_failure,
    _updated_cover_document,
    _restore_transaction_files,
    _set_cover_transaction_status,
    _prepare_cover_transaction,
    _roll_forward_prepared_cover_transactions,
    _bind_repaired_cover,
    _cover_binding_valid,
    _recover_committed_cover_binding,
    _initial_cover_proof_valid,
    _refresh_cover_repair_budget,
    cover_repair_needed,
    _cover_repair_eligible,
    _cover_authority_preflight,
)
from src.autoslice.cover_maintenance import (  # noqa: E402
    delivered_paths,
    cover_ref_for,
    _json_file_bytes,
    _atomic_write_bytes_file,
    _atomic_write_json_file,
    repair_covers,
)
from src.autoslice.reporting import write_reports  # noqa: E402


def song_completion_evidence(record: dict) -> dict:
    """Runner wrapper (extracted 2026-07-15): the 843-line proof lives in
    src/autoslice/song_completion.py; inject the two runner-owned symbols it
    reaches (_has_exact_av_streams, HOST_VOCAL_PROFILE) so tests patching them
    on the runner keep steering the real proof path."""
    return _song_completion.song_completion_evidence(
        record,
        has_exact_av_streams=_has_exact_av_streams,
        host_vocal_profile=HOST_VOCAL_PROFILE,
        host_vocal_present_decision=HOST_VOCAL_PRESENT_DECISION,
        host_vocal_absent_decision=HOST_VOCAL_ABSENT_DECISION,
        verified_host_singing_decision=VERIFIED_HOST_SINGING_DECISION,
        host_not_singing_reason=HOST_NOT_SINGING_REASON,
    )


def verified_song_fallback_title(song_title: str | None, hook: str | None) -> str | None:
    return _song_completion.verified_song_fallback_title(
        song_title,
        hook,
        song_hook_template=CHANNEL_PROFILE.song_hook_template,
        song_plain_template=CHANNEL_PROFILE.song_plain_template,
    )


def published_song_match(value: str) -> dict | None:
    """Match against reviewed history plus successful production uploads."""

    return _published_song_match(
        value,
        snapshot_path=profile_asset_file("published_songs"),
        ledger_path=BASE / "reports" / "upload_ledger.jsonl",
        song_title_prefix=CHANNEL_PROFILE.song_title_prefix,
    )


def _speaker_runner_context() -> _SpeakerRunnerContext:
    return _SpeakerRunnerContext(
        base=BASE,
        repo_root=REPO_ROOT,
        room=ROOM,
        piece_pre_ms=PIECE_PRE_MS,
        boundary_repair_retry_cap_ms=BOUNDARY_REPAIR_RETRY_CAP_MS,
        final_tail_guard_ms=SPEAKER_ROUTING_FINAL_TAIL_GUARD_MS,
        date_rx=DATE_RX,
        log=log,
        pipeline_fingerprint=pipeline_fingerprint,
        child_env=child_env,
        atomic_write_json=_atomic_write_json_file,
    )


def _speaker_date_root(date: str) -> Path:
    return _speaker_routing_session._speaker_date_root(
        date, ctx=_speaker_runner_context()
    )


def _speaker_generation_root(date: str, pipeline: str) -> Path:
    return _speaker_routing_session._speaker_generation_root(
        date, pipeline, ctx=_speaker_runner_context()
    )


def _write_speaker_session_authority(path: Path, document: dict) -> str:
    return _speaker_routing_session._write_speaker_session_authority(
        path, document, ctx=_speaker_runner_context()
    )


def prepare_speaker_routing(
    date: str, items: list[dict], *, state: dict | None = None
) -> dict | None:
    return _speaker_routing_session.prepare_speaker_routing(
        date, items, state=state, ctx=_speaker_runner_context()
    )


def _capture_state_from_result(result: dict) -> dict[str, object]:
    return _speaker_routing_session._capture_state_from_result(
        result, ctx=_speaker_runner_context()
    )


def queue_collab_evidence_capture(
    date: str,
    state: dict,
    candidates: list[dict],
    *,
    routing_claim: dict | None,
) -> dict[str, object]:
    return _speaker_routing_session.queue_collab_evidence_capture(
        date,
        state,
        candidates,
        routing_claim=routing_claim,
        ctx=_speaker_runner_context(),
    )


def talk_failure_recovery_fingerprint(failure_kind: str | None, candidate_id: str) -> str:
    """Hash only the code/assets capable of repairing a classified failure.

    A broad graph/crawler edit must not wake a speaker failure, while a real
    boundary or speaker fix must still earn a recovery attempt even after the
    old global lifetime counter was exhausted.  Legacy unclassified records
    use the historical full fingerprint once; the fresh attempt then persists
    a scoped identity.
    """

    if failure_kind not in {"content_boundary", "speaker_evidence", "runtime_prerequisite"}:
        return talk_pipeline_fingerprint(candidate_id)
    if failure_kind == "content_boundary":
        relatives = (
            "scripts/produce_slice_package.py",
            "src/autoslice/jingting_chunker.py",
            "src/autoslice/subtitle_timing_qa.py",
        )
    else:
        relatives = (
            "scripts/produce_slice_package.py",
            "src/autoslice/speaker_common.py",
            "src/autoslice/speaker_context.py",
            "src/autoslice/speaker_evidence.py",
            "src/autoslice/speaker_finalizer.py",
            profile_asset_file("voiceprint_profile"),
        )
    paths = [relative if isinstance(relative, Path) else REPO_ROOT / relative for relative in relatives]
    if failure_kind in {"speaker_evidence", "runtime_prerequisite"}:
        override = candidate_speaker_override_path(candidate_id)
        if override is not None:
            paths.append(override)
    hasher = hashlib.sha256()
    hasher.update(f"talk-failure-recovery.v1\0{failure_kind}\0".encode("utf-8"))
    for path in sorted(paths, key=lambda item: str(item)):
        try:
            relative = path.relative_to(REPO_ROOT).as_posix()
        except ValueError:
            relative = str(path)
        hasher.update(relative.encode("utf-8") + b"\0")
        hasher.update(path.read_bytes() if path.is_file() else b"MISSING")
        hasher.update(b"\0")
    return "sha256:" + hasher.hexdigest()


def song_selector_env(date: str) -> dict[str, str]:
    env = child_env_for_date(date)
    env["AGY_MODEL"] = os.environ.get("SONG_AGY_MODEL", "Gemini 3.5 Flash (High)")
    # Full-song source-context inspection is materially heavier than ordinary
    # talk windows.  A 269 s real 《怎么办》 run wrote its valid output.srt only
    # near the generic 15 minute deadline and was killed while finalizing.
    # Keep the retry bounded, but use the same 30 minute budget as the
    # established live-song AGY workflow instead of treating slow completion
    # as content failure.
    env["AGY_PRINT_TIMEOUT"] = os.environ.get("SONG_AGY_PRINT_TIMEOUT", "30m")
    env["LIDOUSHA_TERM_AS_OF"] = date
    return env


def cpa_qa_cmd() -> str:
    # ``produce_song`` is also a supported/manual repair entry point and does
    # not pass through ``main()``, which injects cpa.env into os.environ.  Read
    # the same credential file as child_env() so the judge command and its
    # subprocess environment cannot disagree (empty --api-base used to make a
    # manual rerun die in argparse before song proof even started).
    env_file = load_env_file(CPA_ENV)
    base = (env_file.get("CPA_BASE_URL") or os.environ.get("CPA_BASE_URL") or "").rstrip("/")
    if not base:
        raise RuntimeError(f"CPA_BASE_URL missing from environment and {CPA_ENV}")
    return (
        "python3 scripts/cpa_semantic_qa_llm.py --request {request_json} --response {response_json} "
        f"--transport direct --model gpt-5.6-luna --fallback-model gpt-5.5 --api-mode responses "
        f"--reasoning-effort medium --max-tokens 16000 --retries 3 --api-base {base} --api-key-env CPA_API_KEY"
    )


def log(msg: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def load_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            line = line.removeprefix("export ").strip()
            if "=" in line:
                key, value = line.split("=", 1)
                out[key.strip()] = value.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def child_env() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("AUTOSLICE_PROFILE", PROFILE_ID)
    env.update(load_env_file(CPA_ENV))
    # Gemini API is the automatic source-context failover when the AGY account
    # is quota-limited.  Import only these named secrets from the recorder env;
    # do not leak unrelated credentials into child processes.
    bilive_env = load_env_file(BILIVE_ENV)
    # GEMINI_KEY_BACKUP is the PAID last-resort key (Ivan 2026-07-13); the
    # gemini_backup_policy module gates every use (>= 3 free-chain failure
    # rounds per item + daily cap), so importing it here only makes the
    # fallback REACHABLE, never routine.
    for key in (
        "GEMINI_API_KEY",
        "GEMINI_API_KEY_2",
        "GEMINI_API_KEY_3",
        "GEMINI_KEY_BACKUP",
    ):
        if bilive_env.get(key):
            env[key] = bilive_env[key]
    env.setdefault("HOME", "/root")
    truth_mode = human_truth_mode()
    blind_timely_terms = os.environ.get("AUTOSLICE_BLIND_TIMELY_TERMS")
    configured_timely_terms = os.environ.get("AUTOSLICE_TIMELY_TERMS")
    runtime_timely_terms = BASE / "state" / "timely_terms.json"
    committed_timely_terms = profile_asset_file("timely_terms")
    if truth_mode == "withheld":
        timely_terms = Path(blind_timely_terms) if blind_timely_terms else None
    elif configured_timely_terms:
        timely_terms = Path(configured_timely_terms)
    elif runtime_timely_terms.is_file() and not runtime_timely_terms.is_symlink():
        timely_terms = runtime_timely_terms
    else:
        timely_terms = committed_timely_terms
    if timely_terms is not None and timely_terms.is_file() and not timely_terms.is_symlink():
        env["LIDOUSHA_TIMELY_TERMS"] = str(timely_terms.resolve())
        env["LIDOUSHA_TIMELY_TERMS_SHA256"] = (
            "sha256:" + _sha256_regular_file(timely_terms)
        )
        env.pop("LIDOUSHA_DISABLE_TIMELY_TERMS", None)
    elif truth_mode == "withheld":
        env["LIDOUSHA_DISABLE_TIMELY_TERMS"] = "1"
        env.pop("LIDOUSHA_TIMELY_TERMS", None)
        env.pop("LIDOUSHA_TIMELY_TERMS_SHA256", None)

    blind_psplive_roster = os.environ.get("AUTOSLICE_BLIND_PSPLIVE_ROSTER")
    configured_psplive_roster = os.environ.get("AUTOSLICE_PSPLIVE_ROSTER")
    runtime_psplive_roster = BASE / "state" / "psplive_roster.json"
    committed_psplive_roster = profile_asset_file("psplive_roster")
    if truth_mode == "withheld":
        psplive_roster = (
            Path(blind_psplive_roster) if blind_psplive_roster else None
        )
    elif configured_psplive_roster:
        psplive_roster = Path(configured_psplive_roster)
    elif runtime_psplive_roster.is_file() and not runtime_psplive_roster.is_symlink():
        psplive_roster = runtime_psplive_roster
    else:
        psplive_roster = committed_psplive_roster
    if (
        psplive_roster is not None
        and psplive_roster.is_file()
        and not psplive_roster.is_symlink()
    ):
        env["LIDOUSHA_PSPLIVE_ROSTER"] = str(psplive_roster.resolve())
        env["LIDOUSHA_PSPLIVE_ROSTER_SHA256"] = (
            "sha256:" + _sha256_regular_file(psplive_roster)
        )
        env.pop("LIDOUSHA_DISABLE_PSPLIVE_ROSTER", None)
    elif truth_mode == "withheld":
        env["LIDOUSHA_DISABLE_PSPLIVE_ROSTER"] = "1"
        env.pop("LIDOUSHA_PSPLIVE_ROSTER", None)
        env.pop("LIDOUSHA_PSPLIVE_ROSTER_SHA256", None)

    blind_topic_graph = os.environ.get("AUTOSLICE_BLIND_TOPIC_ENTITY_GRAPH")
    configured_topic_graph = os.environ.get("AUTOSLICE_TOPIC_ENTITY_GRAPH")
    runtime_topic_graph = BASE / "state" / "topic_entity_graph.json"
    committed_topic_graph = profile_asset_file("topic_entity_graph")
    if truth_mode == "withheld":
        topic_graph = Path(blind_topic_graph) if blind_topic_graph else None
    elif configured_topic_graph:
        topic_graph = Path(configured_topic_graph)
    elif runtime_topic_graph.is_file() and not runtime_topic_graph.is_symlink():
        topic_graph = runtime_topic_graph
    else:
        topic_graph = committed_topic_graph
    if (
        truth_mode == "withheld"
        and topic_graph is not None
        and topic_graph.is_file()
        and not topic_graph.is_symlink()
    ):
        # A blind graph must be generated from the exact blind timely snapshot
        # selected above.  This prevents a reviewed/committed graph from being
        # relabeled by path alone and makes the lineage auditable in the graph.
        try:
            graph_payload = json.loads(topic_graph.read_text(encoding="utf-8"))
            graph_matches_blind_snapshot = (
                timely_terms is not None
                and timely_terms.is_file()
                and not timely_terms.is_symlink()
                and graph_payload.get("generator") == "scripts/crawl_topic_entity_graph.py"
                and graph_payload.get("input_timely_terms_sha256")
                == _sha256_regular_file(timely_terms)
            )
        except (OSError, ValueError, AttributeError):
            graph_matches_blind_snapshot = False
        if not graph_matches_blind_snapshot:
            topic_graph = None
    if topic_graph is not None and topic_graph.is_file() and not topic_graph.is_symlink():
        env["LIDOUSHA_TOPIC_ENTITY_GRAPH"] = str(topic_graph.resolve())
        env["LIDOUSHA_TOPIC_ENTITY_GRAPH_SHA256"] = (
            "sha256:" + _sha256_regular_file(topic_graph)
        )
        env.pop("LIDOUSHA_DISABLE_TOPIC_ENTITY_GRAPH", None)
    elif truth_mode == "withheld":
        env["LIDOUSHA_DISABLE_TOPIC_ENTITY_GRAPH"] = "1"
        env.pop("LIDOUSHA_TOPIC_ENTITY_GRAPH", None)
        env.pop("LIDOUSHA_TOPIC_ENTITY_GRAPH_SHA256", None)
    return env


def child_env_for_date(recording_date: str) -> dict[str, str]:
    env = child_env()
    env["LIDOUSHA_TERM_AS_OF"] = recording_date
    return env


def cpa_healthy() -> bool:
    """One cheap real probe against the CPA chat lane, walking the same failover
    order production uses (gpt-5.6-sol, then the gpt-5.5 / gpt-5.4 fallbacks) —
    any healthy model in the chain means the lane can serve the batch.

    The whole batch is gated on this: provider outages (auth_unavailable 503)
    are a normal operating condition, and producing clips without a working
    title/reconcile LLM ships garbage.  Recordings can wait; the cron tick is
    the retry loop.
    """

    env = load_env_file(CPA_ENV)
    base = (env.get("CPA_BASE_URL") or os.environ.get("CPA_BASE_URL", "")).rstrip("/")
    key = env.get("CPA_API_KEY") or os.environ.get("CPA_API_KEY", "")
    if not base or not key:
        return False
    for model in ("gpt-5.6-sol", "gpt-5.5", "gpt-5.4"):
        body = json.dumps(
            {"model": model, "input": "回复:OK", "reasoning": {"effort": "low"}, "max_output_tokens": 2000}
        ).encode()
        req = urllib.request.Request(
            f"{base}/responses",
            data=body,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                if resp.status == 200:
                    return True
        except Exception:  # noqa: BLE001 — any failure means this model is down
            continue
    return False


def cjk_font_present() -> bool:
    completed = subprocess.run(["fc-list"], check=False, capture_output=True, text=True, timeout=30)
    return bool(re.search(r"CJK|WenQuan|LXGW|YaHei|PingFang", completed.stdout))


def blrec_live_status() -> bool | None:
    """True=live, False=not live, None=unknown (API down → fail-safe skip)."""
    key = load_env_file(BILIVE_ENV).get("RECORD_KEY", "")
    if not key:
        return None
    req = urllib.request.Request(
        f"http://127.0.0.1:{BLREC_PORT}/api/v1/tasks/{ROOM}/data",
        headers={"x-api-key": key},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        return int(data.get("room_info", {}).get("live_status", 0)) == 1
    except Exception as exc:  # noqa: BLE001 — any API failure means "unknown"
        log(f"blrec API unavailable: {exc}")
        return None


def state_path(date: str) -> Path:
    return BASE / "state" / f"{date}.json"


def read_state(date: str) -> dict:
    """State loader that never mistakes damage for a fresh start.

    Missing file → {} (genuinely new date).  Corrupt JSON → the damaged file is
    quarantined for forensics and the .bak (previous good write) is restored;
    with no usable .bak the date is BLOCKED (state_corrupt_blocked), because
    reprocessing 'from scratch' would re-produce and re-deliver everything
    (2026-07-09 audit: corruption must be loud, not a silent reset)."""
    path = state_path(date)
    bak = path.with_suffix(".json.bak")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        try:  # crash window between the two os.replace()s in write_state
            return json.loads(bak.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
    except OSError as exc:
        return {"status": "state_corrupt_blocked", "state_error": f"unreadable: {exc}"}
    except ValueError as exc:
        quarantined = path.with_name(f"{path.name}.corrupt-{time.strftime('%Y%m%dT%H%M%S')}")
        try:
            path.rename(quarantined)
        except OSError:
            pass
        log(f"state for {date} is corrupt ({exc}) → kept as {quarantined.name}")
        try:
            restored = json.loads(bak.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # Persist the blocked marker so EVERY subsequent read agrees — a
            # rename alone would make the next read see "new date" and happily
            # re-produce and re-deliver the whole batch.
            blocked = {"status": "state_corrupt_blocked", "state_error": f"corrupt json, no usable .bak: {exc}"}
            path.write_text(json.dumps(blocked, ensure_ascii=False, indent=2), encoding="utf-8")
            return blocked
        log(f"state for {date} restored from .bak")
        restored["state_restored_from_bak"] = True
        write_state(date, restored)
        return restored


def write_state(date: str, state: dict) -> None:
    """Atomic write (tmp + os.replace) keeping the previous version as .bak —
    a mid-write crash can no longer leave a half-written unparseable state."""
    state["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    path = state_path(date)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    if path.exists():
        os.replace(path, path.with_suffix(".json.bak"))
    os.replace(tmp, path)


def write_alert(name: str, message: str) -> None:
    """Append-only alert files under reports/ — the Mac launchd pull is the
    delivery channel (Ivan's rule: alerts travel via report files, not chat)."""
    path = BASE / "reports" / f"ALERT_{name}.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as sink:
        sink.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {message}\n")


def source_health_error() -> str | None:
    """Probe the recordings mount via a subprocess `ls` so a HUNG FUSE mount
    (which blocks Python's stat() forever) times out instead of wedging the
    tick.  Returns None when healthy, else a short error string.  2026-07-09:
    the CloudDrive endpoint died and every layer above swallowed the OSError
    into 'no dates' — the control plane kept reporting green for hours."""
    try:
        completed = subprocess.run(
            ["ls", str(REC_ROOT)], check=False, capture_output=True, text=True, timeout=25
        )
    except subprocess.TimeoutExpired:
        return f"listing {REC_ROOT} timed out after 25s (hung mount?)"
    if completed.returncode != 0:
        return (completed.stderr.strip() or f"ls rc={completed.returncode}")[:300]
    return None


def runtime_health_error() -> str | None:
    """Reject an incomplete code snapshot before it consumes candidate work.

    Production deploy already archives ``scripts/src/assets`` atomically, but
    the July 11/12 isolated eval repo was assembled without the tracked
    voiceprint profile.  Every talk then reached speaker finalization and
    failed independently, burning the per-candidate retry budget.  Runtime
    prerequisites are date-level infrastructure, so validate them once and
    pause without touching any candidate state.
    """

    tracked_speaker_profile = profile_asset_file("voiceprint_profile")
    required = {
        "tracked_speaker_profile": tracked_speaker_profile,
        "talk_producer": REPO_ROOT / "scripts" / "produce_slice_package.py",
        "source_context_executor": REPO_ROOT / "src" / "autoslice" / "source_context_executor.py",
    }
    if HOST_VOCAL_PROFILE != tracked_speaker_profile:
        required["configured_host_vocal_profile"] = HOST_VOCAL_PROFILE
    missing = [f"{name}={path}" for name, path in required.items() if not path.is_file()]
    if missing:
        return "missing runtime prerequisite(s): " + ", ".join(missing)
    try:
        profile = json.loads(tracked_speaker_profile.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return f"invalid speaker profile {tracked_speaker_profile}: {type(exc).__name__}: {exc}"
    if not isinstance(profile, dict):
        return f"invalid speaker profile {tracked_speaker_profile}: root must be an object"
    return None


def list_dates() -> list[str]:
    try:
        names = [p.name for p in REC_ROOT.iterdir() if p.is_dir() and DATE_RX.match(p.name)]
    except OSError as exc:
        log(f"list_dates: recordings root unreadable: {exc}")
        return []
    return sorted(names)[-3:]


def list_segments(date: str) -> list[Path]:
    date_dir = REC_ROOT / date
    try:
        files = sorted(date_dir.glob(f"{ROOM}_*.mp4"))
    except OSError as exc:
        log(f"list_segments({date}): unreadable: {exc}")
        return []
    return [f for f in files if f.parent == date_dir]


def ffprobe_ms(path: Path) -> int:
    completed = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        check=False, capture_output=True, text=True, timeout=600,
    )
    try:
        return int(float(completed.stdout.strip()) * 1000)
    except ValueError:
        return 0


def bcut_transcribe(segment: Path, date: str) -> Path | None:
    cache = BASE / "cache" / date / f"{segment.stem}.bcut.srt"
    if cache.is_file() and cache.stat().st_size > 0:
        return cache
    cache.parent.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "free_asr_client.py"), str(segment), "--srt", str(cache)],
        check=False, capture_output=True, text=True, timeout=900, cwd=str(REPO_ROOT), env=child_env(),
    )
    if completed.returncode != 0 or not cache.is_file() or cache.stat().st_size == 0:
        log(f"BCUT transcribe FAILED for {segment.name}: {completed.stderr[-200:]}")
        return None
    return cache


def find_danmaku_xml(segment: Path) -> Path | None:
    digits = re.sub(r"\D", "", segment.stem)
    for folder in (segment.parent, segment.parent / "sources"):
        try:
            for xml in folder.glob("*.xml"):
                if re.sub(r"\D", "", xml.stem) == digits and xml.stat().st_size > 0:
                    return xml
        except OSError:
            continue
    return None


def find_chat_jsonl(segment: Path) -> Path | None:
    """Find the structured live-event sidecar independently from XML health."""

    digits = re.sub(r"\D", "", segment.stem)
    for folder in (segment.parent, segment.parent / "sources"):
        try:
            for jsonl in folder.glob("*.jsonl"):
                if re.sub(r"\D", "", jsonl.stem) == digits and jsonl.stat().st_size > 0:
                    return jsonl
        except OSError:
            continue
    return None


_DIAN_GE_RX = re.compile(r"^点歌\s*(.+)$")
_TRAILING_PUNCT_RX = re.compile(r"[\s,.!?~～，。！？、·…\-_]+$")






_SRT_TS_RX = re.compile(
    r"(\d\d):(\d\d):(\d\d),(\d\d\d)\s*-->\s*(\d\d):(\d\d):(\d\d),(\d\d\d)"
)








def produce_batch(date: str, items: list[dict], produce_fn) -> list[dict]:
    """Produce ``items`` CONCURRENTLY (bounded by MAX_PARALLEL_PRODUCE), preserving
    input order.  Each slice is an independent subprocess (produce_slice_package /
    the song selector), so threads just wait on those; a crash in one becomes a
    failed result and never kills the batch.  ``produce_fn`` is produce_talk /
    produce_song, called as fn(date, item)."""
    from concurrent.futures import ThreadPoolExecutor

    def _one(item: dict) -> dict:
        try:
            result = produce_fn(date, item)
            if item.get("session_id"):
                result.setdefault("session_id", item["session_id"])
            return result
        except Exception as exc:  # noqa: BLE001 — one bad slice must not kill the batch
            log(f"produce crashed for {item.get('cid')}: {exc}")
            result = {
                "candidate_id": item.get("cid"),
                "rc": -1,
                "status": "failed",
                "error": str(exc),
                "reason_codes": ["PRODUCE_UNEXPECTED_EXCEPTION"],
                "pipeline_fingerprint": (
                    talk_pipeline_fingerprint(str(item.get("cid") or ""))
                    if produce_fn is produce_talk
                    else pipeline_fingerprint()
                ),
                **(
                    {"song_pipeline_fingerprint": song_pipeline_fingerprint()}
                    if produce_fn is produce_song
                    else {}
                ),
                **{
                    key: item[key]
                    for key in (
                        "hook",
                        "confidence",
                        "danmaku",
                        "preview",
                        "segment_path",
                        "seg_dur_ms",
                        "start_ms",
                        "end_ms",
                        "lane",
                        "title_hint",
                        "visual_song_evidence",
                        "selected_repair",
                        "talk_repair_retry_count",
                        "talk_transient_retry_count",
                        "retry_reason",
                        "anchor_start_ms",
                        "anchor_end_ms",
                        "transient_retry_count",
                        "session_id",
                    )
                    if key in item
                },
            }
            if item.get("segment_path"):
                result["segment"] = Path(str(item["segment_path"])).name
            anchor_start = item.get("anchor_start_ms")
            anchor_end = item.get("anchor_end_ms")
            if isinstance(anchor_start, int) and isinstance(anchor_end, int):
                result["start_ms"] = max(0, anchor_start - SONG_WINDOW_PRE_MS)
                result["end_ms"] = anchor_end + SONG_WINDOW_POST_MS
            return result

    if not items:
        return []
    workers = min(MAX_PARALLEL_PRODUCE, len(items))
    log(f"producing {len(items)} slice(s), up to {workers} in parallel")
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(_one, items))


def process_date(date: str) -> None:
    state = read_state(date)
    if state.get("status") == "state_corrupt_blocked":
        write_alert("STATE_CORRUPT", f"{date}: {state.get('state_error', 'state file corrupt')} — date BLOCKED, needs human")
        log(f"{date}: state corrupt — blocked, not reprocessing (would re-deliver everything)")
        return
    runtime_err = runtime_health_error()
    if runtime_err:
        changed = state.get("runtime_error") != runtime_err or state.get("status") != "paused_runtime_invalid"
        state["status"] = "paused_runtime_invalid"
        state["runtime_error"] = runtime_err
        write_state(date, state)
        if changed:
            write_alert("RUNTIME_INVALID", f"{date}: {runtime_err}")
        log(f"{date}: runtime invalid — batch deferred without consuming candidate retries: {runtime_err}")
        return
    state.pop("runtime_error", None)
    if annotate_state_sessions(date, state):
        write_state(date, state)
    automatic_maintenance = date >= AUTOMATIC_MAINTENANCE_NOT_BEFORE
    recovered_song_deliveries = (
        recover_bound_song_deliveries(date, state) if automatic_maintenance else 0
    )
    if recovered_song_deliveries:
        write_state(date, state)
        log(
            f"{date}: recovered {recovered_song_deliveries} verified song delivery "
            "package(s) without selector/ASR/LRC rerun"
        )
    song_fingerprint_baseline_before = state.get("song_pipeline_fingerprint_baseline")
    requeued_talks = requeue_recoverable_talks(date, state) if automatic_maintenance else 0
    requeued_songs = requeue_recoverable_songs(date, state) if automatic_maintenance else 0
    song_fingerprint_baseline_changed = (
        state.get("song_pipeline_fingerprint_baseline")
        != song_fingerprint_baseline_before
    )
    if requeued_talks or requeued_songs or song_fingerprint_baseline_changed:
        write_state(date, state)
    if requeued_talks or requeued_songs:
        log(
            f"{date}: requeued {requeued_talks} boundary talk failure(s) and "
            f"{requeued_songs} recoverable song BLOCK(s) for song pipeline "
            f"{song_pipeline_fingerprint()[:19]}…"
        )
    has_new = any(
        s.stem not in set(state.get("segments_done", [])) and s.stem not in state.get("segments_dead", {})
        for s in list_segments(date)
    )
    has_pending = bool(
        state.get("pending_talk")
        or state.get("pending_song")
        or backlog_has_eligible_session_work(state)
    )
    needs_cover = automatic_maintenance and any(
        cover_repair_needed(date, r)
        for r in state.get("picks", []) + state.get("songs", [])
    )
    if not has_new and not has_pending and not needs_cover:
        retry_epoch = scheduled_retry_epoch(state)
        if retry_epoch is not None:
            delivered = any(
                pick.get("status") in DELIVERED_TALK_STATUSES for pick in state.get("picks", [])
            ) or any(song.get("delivered") for song in state.get("songs", []))
            state["status"] = "review_ready_retry_wait" if delivered else "retry_wait"
            state["next_retry_at_epoch"] = retry_epoch
            state["next_retry_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(retry_epoch))
            write_state(date, state)
            write_reports(date, state)
            return
        if recovered_song_deliveries:
            delivered_talk = [
                pick
                for pick in state.get("picks", [])
                if pick.get("status") in DELIVERED_TALK_STATUSES
            ]
            delivered_songs = [song for song in state.get("songs", []) if song.get("delivered")]
            failures = [
                record
                for record in state.get("picks", []) + state.get("songs", [])
                if record.get("status")
                in ("failed", "boundary_unrepairable", "speaker_review_required")
            ]
            state["status"] = (
                "review_ready_with_failures" if failures else "review_ready"
            ) if delivered_talk or delivered_songs else "no_delivery"
            write_state(date, state)
            write_reports(date, state)
            log(
                f"{date}: finalized {recovered_song_deliveries} recovered song "
                "package(s) without requiring CPA"
            )
        return
    # CPA gate: recall, reconcile, titles and covers all need the chat lane.
    # Recordings can wait — never produce garbage during a provider outage.
    if not cpa_healthy():
        state["status"] = "paused_cpa_down"
        write_state(date, state)
        log(f"{date}: CPA chat lane down — batch deferred to a later tick")
        return
    log(f"processing {date}: new={has_new} pending={has_pending}")
    state.setdefault("picks", [])
    state.setdefault("songs", [])
    state["status"] = "processing"
    write_state(date, state)

    discover_segments(date, state)
    # Session sealing: transcription/recall above runs as segments appear, but
    # SELECTION waits until the inventory is stable so every candidate of the
    # session competes for the quota (late segments used to arrive after the
    # slots were spent).  Cover repairs for already-delivered clips still run.
    if (has_new or has_pending) and not session_sealed(date, state):
        state["status"] = "sealing"
        write_state(date, state)
        log(f"{date}: segment inventory not stable yet — selection deferred to next tick (sealing)")
        return
    # Keep a structured, session-wide snapshot for the unlabelled evidence
    # sidecar before prioritize() reduces production to top-5 talk clips.
    capture_candidates = [
        dict(item) for item in state.get("pending_talk", []) if isinstance(item, dict)
    ]
    prioritize(state)
    routing_claim = prepare_speaker_routing(
        date, state["pending_talk"], state=state
    )
    # Machine-evidence song-name pool for the talk lane's deterministic pin
    # (Ivan 2026-07-13): the screen songlist keeps accruing across the whole
    # date, so this is recomputed fresh every tick, not just once at discovery.
    song_name_candidates = collect_song_name_candidates(date, state)
    if song_name_candidates:
        for item in state["pending_talk"]:
            if isinstance(item, dict):
                item["song_name_candidates"] = song_name_candidates
    write_state(date, state)

    # Phase C: produce talk picks CONCURRENTLY (they're independent; each is
    # network-bound on AGY/CPA/gpt-image-2).  title_failed picks (CPA title lane
    # flaky) stay pending and retry on a later tick — the succeeded ones are kept,
    # not re-done.  CPA was gated at entry; a mid-batch outage just fails a slice.
    while state["pending_talk"]:
        talk_items = list(state["pending_talk"])
        results = produce_batch(date, talk_items, produce_talk)
        retry: list[dict] = []
        rejected = 0
        recoverable_failure = False
        for item, result in zip(talk_items, results):
            if result.get("status") == "title_failed":
                item["title_attempts"] = item.get("title_attempts", 0) + 1
                log(f"{item['cid']}: title generation failed (attempt {item['title_attempts']})")
                if item["title_attempts"] < TITLE_MAX_ATTEMPTS:
                    retry.append(item)
                    continue
                result["status"] = "failed"
                result["error"] = "title generation failed 3x"
            if result.get("status") in {
                "boundary_unrepairable",
                "speaker_review_required",
                "speaker_evidence_insufficient",
            }:
                result["rejected_status"] = result["status"]
                result["status"] = "candidate_rejected"
                result["rejection_reason"] = (
                    "unsafe_boundary_backfilled"
                    if result["rejected_status"] == "boundary_unrepairable"
                    else "speaker_identity_unresolved_backfilled"
                )
                rejected += 1
            if result.get("failure_recoverable") is True:
                recoverable_failure = True
            state["picks"].append(result)
        state["pending_talk"] = retry
        write_state(date, state)
        if retry:  # some title lanes flaky → back off, resume the rest next tick
            state["status"] = "paused_cpa_down"
            write_state(date, state)
            write_reports(date, state)
            log(f"{date}: {len(retry)} title(s) failed — will retry on a later tick")
            queue_collab_evidence_capture(
                date,
                state,
                capture_candidates,
                routing_claim=routing_claim,
            )
            write_state(date, state)
            write_reports(date, state)
            return
        if recoverable_failure:
            # The selected item is waiting on infrastructure.  Do not spend a
            # second candidate merely to hide the outage or exceed top-5 when
            # the original resumes.
            break
        if rejected:
            prioritize(state)
            write_state(date, state)
            continue
        break

    # Song lane with bounded backfill: a gate-BLOCKED song frees its slot for
    # the next backlog song (danmaku-desc) until the delivery budget is met,
    # the backlog runs dry, or SONG_ATTEMPT_CAP is hit.
    while state["pending_song"]:
        song_items = list(state["pending_song"])
        state["songs"].extend(produce_batch(date, song_items, produce_song))
        state["pending_song"] = []
        refill_songs(state)
        write_state(date, state)

    if automatic_maintenance:
        repair_covers(date, state)

    picks, songs = state["picks"], state["songs"]
    delivered_talk = [p for p in picks if p.get("status") in DELIVERED_TALK_STATUSES]
    repaired = [p for p in delivered_talk if p.get("boundary_repairs")]
    delivered_songs = [s for s in songs if s.get("delivered")]
    blocked_songs = [s for s in songs if s.get("status") == "blocked"]
    failures = [
        record
        for record in picks + songs
        if record.get("status")
        in ("failed", "boundary_unrepairable", "speaker_review_required")
    ]
    # Honest batch vocabulary (2026-07-09 audit: BLOCK+0 deliveries read 'done /
    # 0 failures').  A batch is review_ready only when something REACHED review.
    retry_epoch = scheduled_retry_epoch(state)
    if retry_epoch is not None:
        state["status"] = (
            "review_ready_retry_wait" if delivered_talk or delivered_songs else "retry_wait"
        )
        state["next_retry_at_epoch"] = retry_epoch
        state["next_retry_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(retry_epoch))
    elif delivered_talk or delivered_songs:
        state["status"] = "review_ready_with_failures" if failures else "review_ready"
    else:
        state["status"] = "no_delivery"
    write_state(date, state)
    write_reports(date, state)
    log(
        f"{date} batch finished [{state['status']}]: talk {len(delivered_talk)}/{len(picks)} delivered"
        f" ({len(repaired)} boundary-self-repaired), song {len(delivered_songs)} delivered"
        f" / {len(blocked_songs)} gate-blocked / {len(songs)} attempted, {len(failures)} failure(s)"
    )
    # Production is already committed to state/reports above.  Only now may a
    # rare collab trigger enqueue the separately bounded evidence worker.
    queue_collab_evidence_capture(
        date,
        state,
        capture_candidates,
        routing_claim=routing_claim,
    )
    write_state(date, state)
    write_reports(date, state)


def write_heartbeat(body: str) -> None:
    heartbeat = BASE / "reports" / "heartbeat.txt"
    heartbeat.parent.mkdir(parents=True, exist_ok=True)
    heartbeat.write_text(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {body}\n", encoding="utf-8")


def _live_hold_active(live: bool | None) -> bool:
    """直播期间冻结处理（True/未知都冻结，fail-safe）。

    例外（Ivan 2026-07-13）：隔离回填 BASE 处理的是几天前的已关闭文件，
    直播期间跑它们数据上安全，只有资源争抢风险（由外部护栏管）。设
    `AUTOSLICE_IGNORE_LIVE_HOLD=1` 可豁免——但带自我防护：**只要本 BASE 的
    录像根里能看到今天（UTC 或北京日）的日期目录，豁免拒绝生效**，因此
    生产面即使误设该 env 也依然冻结；能豁免的只有只挂历史日期的隔离面。
    """
    if live is False:
        return False
    if os.environ.get("AUTOSLICE_IGNORE_LIVE_HOLD", "") != "1":
        return True
    now = time.time()
    today_utc = time.strftime("%Y-%m-%d", time.gmtime(now))
    today_cst = time.strftime("%Y-%m-%d", time.gmtime(now + 8 * 3600))
    visible = set(list_dates())
    if visible & {today_utc, today_cst}:
        log(
            "AUTOSLICE_IGNORE_LIVE_HOLD=1 REFUSED: today's recordings are visible "
            f"in {REC_ROOT} — live hold stays (production-shape base)"
        )
        return True
    log(
        f"live={live} but hold IGNORED (AUTOSLICE_IGNORE_LIVE_HOLD=1, isolated backfill "
        f"base over closed dates {sorted(visible)})"
    )
    return False


def tick() -> int:
    if (BASE / "DISABLED").exists():
        log("DISABLED flag present — runner paused")
        return 0
    if not cjk_font_present():
        log("WARNING: no CJK font on this host — subtitles would burn as tofu boxes; run: apt install fonts-noto-cjk")
    # Source health FIRST (2026-07-09: a dead CloudDrive mount read as 'no new
    # recordings' and the heartbeat stayed green for hours while the recorder's
    # write path was broken).  A sick source is loud in the heartbeat AND in an
    # alert file, and the tick does nothing else — fail-closed.
    source_err = source_health_error()
    if source_err:
        write_alert("SOURCE_UNAVAILABLE", source_err)
        write_heartbeat(f"SOURCE_UNAVAILABLE({source_err}) dates=(skipped)")
        log(f"recordings source UNAVAILABLE: {source_err} — tick aborted, alert written")
        return 0
    live = blrec_live_status()
    if _live_hold_active(live):
        if live is None:
            write_heartbeat("live=? source=ok (blrec API unavailable — fail-safe skip)")
            log("live status unknown — fail-safe skip this tick")
        else:
            write_heartbeat("live=True source=ok (waiting for stream end)")
            log("room is LIVE — waiting for stream end")
        return 0
    checked = []
    for date in list_dates():
        state = read_state(date)
        if state.get("status") == "manual_preclaim":
            checked.append(f"{date}:manual_preclaim")
            continue
        checked.append(f"{date}:{state.get('status', 'new')}")
        process_date(date)
    write_heartbeat(f"live={live} source=ok dates={' '.join(checked) or '(none)'}")
    log(f"tick done: live={live} dates={' '.join(checked) or '(none)'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="single tick (cron entry point)")
    parser.add_argument("--preclaim", nargs="+", metavar="DATE", help="mark dates as manually handled; runner will never touch them")
    parser.add_argument("--smoke-segment", type=Path, help="end-to-end smoke: recall+produce ONE talk candidate from this segment into the smoke area")
    args = parser.parse_args(argv)
    BASE.mkdir(parents=True, exist_ok=True)
    # Inject CPA credentials into OUR process too: the semantic-recall llm_call
    # runs llm_via_cpa.sh from this process (not via child_env()), and without
    # this the recall lane silently degrades to the deterministic fallback.
    os.environ.update(load_env_file(CPA_ENV))
    if args.preclaim:
        for date in args.preclaim:
            write_state(date, {"status": "manual_preclaim", "segments_done": [s.stem for s in list_segments(date)]})
            log(f"preclaimed {date}")
        return 0
    if args.smoke_segment:
        segment = args.smoke_segment
        date = "smoke"
        srt = bcut_transcribe(segment, date)
        if srt is None:
            return 1
        xml = find_danmaku_xml(segment)
        chat_jsonl = find_chat_jsonl(segment)
        candidates, lane, extras = recall_candidates(srt, danmaku_hints(xml))
        talk = [c for c in candidates if getattr(c, "content_type_hint", "talk") != "song"]
        log(f"smoke: {len(candidates)} candidates via {lane}; producing first talk candidate")
        if not talk:
            log("smoke: no talk candidate found")
            return 1
        cand = talk[0]
        meta = extras.get(cand.anchor.candidate_id, {})
        seg_tag = re.sub(r"\D", "", segment.stem)[-6:]
        item = {
            "cid": f"auto_{seg_tag}_{int(cand.boundary.resolved_start_ms) // 1000}_{int(cand.boundary.resolved_end_ms) // 1000}",
            "segment_path": str(segment),
            "seg_dur_ms": ffprobe_ms(segment),
            "start_ms": max(0, int(cand.boundary.resolved_start_ms)),
            "end_ms": int(cand.boundary.resolved_end_ms),
            "xml": str(xml) if xml else None,
            "chat_jsonl": str(chat_jsonl) if chat_jsonl else None,
            "hook": meta.get("hook", ""),
            "confidence": meta.get("confidence"),
            "lane": lane,
        }
        result = produce_talk(date, item)
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0 if result.get("status") in DELIVERED_TALK_STATUSES else 1
    if args.once:
        return tick()
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
