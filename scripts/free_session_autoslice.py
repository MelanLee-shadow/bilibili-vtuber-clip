#!/usr/bin/env python3
"""Unattended post-stream auto-slice runner (runs ON the free host).

Ivan's goal (2026-07-05): when a 李豆沙 stream ends, free starts the FULL
canonical pipeline by itself — no human kick-off:

    stream end (fresh recorder-neutral status from BililiveRecorder adapter)
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
- **Dead segments**: recorder restart stubs (a few KB of mp4) and segments whose
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

from scripts.suggest_upload_tags import generate_upload_tags
from src.autoslice.candidate_truth_asset import candidate_truth_fingerprint, resolve_candidate_truth_asset_path
from src.autoslice.channel_profile import load_channel_profile
from src.autoslice.runtime_candidate_asset import bind_runtime_candidate_asset
from src.autoslice.game_context import bind_session_game_context
from src.autoslice.streamer_dynamics import bind_session_theme_hints
from src.autoslice.host_vocal_proof import verify_host_vocal_proof_claim
from src.autoslice.reviewed_subtitle_baseline_registry import (
    ReviewedSubtitleBaseline,
    load_candidate_reviewed_subtitle_baseline,
)
from src.autoslice.source_integrity import audit_finalized_recording_inventory
from src.autoslice.legacy_hls_recovery import (
    LegacyHlsRecoveryError,
    recover_finalized_legacy_hls,
)
from src.autoslice.batch_terminal_state import (
    project_terminal_batch_state,
    project_terminal_song_disposition,
)
from src.autoslice import (
    live_gate,
    publication_reconciliation,
    runner_state_writeback,
    semantic_evidence_scorecard_refresh as semantic_chat_refresh,
)
from src.autoslice.producer_batch_transaction import (
    ProducerBatchTransactionError,
    resume_pending_batch,
)
from src.autoslice.producer_batch_runner_integration import (
    commit_projected_prefix,
    dispatch_prepared_lane,
    finish_producer_date,
    initialize_date_state,
    maintain_selected_source_fact_recovery,
    pre_dispatch_block_message,
    project_prepared_results,
    reject_materialized_prepare_results,
    resume_prepared_batch_if_present,
)
from src.autoslice.runner_batch_dispatch import produce_batch as _dispatch_produce_batch
from src.autoslice.runner_heartbeat import write_heartbeat as _write_heartbeat
from src.autoslice.runner_alerts import write_alert as _write_alert
from src.autoslice.runner_pipeline_fingerprints import (
    pipeline_fingerprint as _pipeline_fingerprint,
    song_pipeline_fingerprint as _song_pipeline_fingerprint,
)
from src.autoslice.qixi_transaction_core import exclusive_runner_commit
from src.autoslice.historical_fastlane_authority import (
    HistoricalFastlaneAuthorityError,
    exclusive_tick as historical_exclusive_tick,
    finish_historical_run,
    load_and_start_historical_run,
    normalize_historical_room_id,
)
from src.autoslice.live_gate import format_live_basis, live_signal_divergence
from src.autoslice.selection_scorecard import (
    SelectionCalibrationPolicyError,
    load_selected_selection_calibration_policy,
)
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
from src.autoslice.session_relation_authority import resolve_session_relation
from src.autoslice.structured_chat_binding import (
    StructuredChatBindingError,
    resolve_structured_chat_binding as _resolve_structured_chat_binding,
)

CHANNEL_PROFILE = load_channel_profile(REPO_ROOT)
PROFILE_ID = CHANNEL_PROFILE.profile_id
PROFILE_DISPLAY_NAME = CHANNEL_PROFILE.display_name
PROFILE_OUTPUT_DIRECTORY = CHANNEL_PROFILE.output_directory
PROFILE_HOST_SPEAKER_LABEL = CHANNEL_PROFILE.host_speaker_label
PROFILE_GUEST_SPEAKER_LABEL = CHANNEL_PROFILE.guest_speaker_label
HOST_VOCAL_PRESENT_DECISION = CHANNEL_PROFILE.decision("host_vocal_present")


def session_relation_for_segment(
    date: str,
    segment: Path,
    *,
    source_sha256: str | None = None,
) -> dict[str, object] | None:
    """Resolve the committed relation ledger independently of capture sidecars."""

    return resolve_session_relation(
        ledger_path=CHANNEL_PROFILE.asset_file("session_relation_ledger"),
        date=date,
        recording_path=segment,
        source_sha256=source_sha256,
    )


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
os.environ.setdefault("AUTOSLICE_BASE", str(BASE))
# Ivan 2026-07-13: during the speaker data-accumulation phase every delivered
# clip keeps the single host (李豆沙) subtitle style and speaker uncertainty
# must never reject a delivery. "required"/"auto" stay available for the
# future re-enable decision.
SPEAKER_MODE = os.environ.get("AUTOSLICE_SPEAKER_MODE", "uniform_host")
if SPEAKER_MODE not in {"uniform_host", "required", "auto"}:
    SPEAKER_MODE = "uniform_host"
ROOM = os.environ.get("AUTOSLICE_ROOM", CHANNEL_PROFILE.room_id)
_DEFAULT_REC_ROOT = Path(f"/root/clouddrive2/CloudNAS/CloudDrive/123云盘/live-streaming/{ROOM}")
REC_ROOT = Path(
    os.environ.get(
        "AUTOSLICE_REC_ROOT",
        str(_DEFAULT_REC_ROOT),
    )
)
# Recovery can temporarily point REC_ROOT at an official-replay staging tree.
# Structured chat still belongs to the canonical recorder archive, so it gets
# an independent root instead of accidentally following that override.
CANONICAL_REC_ROOT = Path(
    os.environ.get(
        "AUTOSLICE_CANONICAL_REC_ROOT",
        str(_DEFAULT_REC_ROOT),
    )
)
RECORDER_STATUS_PATH = Path(os.environ.get("AUTOSLICE_RECORDER_STATUS_PATH", "/opt/bilive/recording/status.json"))
RECORDER_ADAPTER_STATE_PATH = Path(
    os.environ.get("AUTOSLICE_RECORDER_ADAPTER_STATE_PATH", "/opt/bilive/recording/adapter-state.json")
)
HISTORICAL_RECORDER_ENDPOINT = "http://127.0.0.1:23566/graphql"
HISTORICAL_RECORDER_ENV = Path("/opt/bilive/recorder.env")
RECORDER_STATUS_MAX_AGE_SECONDS = int(os.environ.get("AUTOSLICE_RECORDER_STATUS_MAX_AGE_SECONDS", "180"))
LIVE_WITHOUT_RECORDING_WARN_SECONDS = int(os.environ.get("AUTOSLICE_LIVE_WITHOUT_RECORDING_WARN_SECONDS", "1800"))
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
# This must stay >= the largest cap any dated grant can award (assets/lidousha/
# talk_quota_policy_authority.v1.json; guarded by test_talk_quota_policy_freeze)
# or an authorized session's later slots silently starve mid-run once already
# picked records exhaust the attempt budget, before its own cap is reached.
TALK_ATTEMPT_CAP = 20  # reject unsafe content candidates and backfill, bounded
# 冒烟同款 backfill（帽更小）：talk[0] 一票否决曾让整次冒烟颗粒无收，而它偏偏
# 是文档推荐的"第一支切片"入口——单候选级 fail-closed 时换下一个候选再试。
SMOKE_TALK_ATTEMPT_CAP = 3
MAX_SONGS_PER_SESSION = 1  # Ivan 2026-07-16: 每场直播至多一个歌切；已发布歌曲不再出
MAX_SONGS_PER_DATE = MAX_SONGS_PER_SESSION  # compatibility alias for callers/tests
MIN_TALK_EFFECTIVE_DURATION_MS = 45_000
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
AUTOMATIC_MAINTENANCE_NOT_BEFORE = os.environ.get("AUTOSLICE_AUTOMATIC_MAINTENANCE_NOT_BEFORE", "2026-07-11")
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
        "JINGTING_PROVENANCE_MISSING",
        "JINGTING_PROVIDER_NOT_AGY",
        "JINGTING_AGY_FAILED",
        "JINGTING_MODEL_MISSING",
        "JINGTING_PROVIDER_FALLBACK_USED",
        "JINGTING_PROVIDER_FALLBACK_UNKNOWN",
        "SONG_WINDOW_CUT_FAILED",
        "SONG_SOURCE_TRANSCRIPT_EMPTY",
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
TALK_COVER_PENDING_STATUS = "media_ready_cover_pending"
# Recall pool, not delivery quota. Long sessions are recalled in overlapping
# 30-minute windows and need enough global slack for review gates before the
# per-live-session top-5 delivery selection.
# 2026-08-08 12 -> 18 (+50%, Ivan「候选也最好搞多一点」): a RESOLVED game
# session can now deliver up to 20 talk picks, so the recall pool feeding it
# needs more slack too.
PER_SEGMENT_CANDIDATES = 18
MIN_SEGMENT_BYTES = 5_000_000  # recorder restart stubs are a few KB — dead on sight
BCUT_MAX_ATTEMPTS = 2
TITLE_MAX_ATTEMPTS = 3
COVER_REPAIR_MAX_ATTEMPTS = 3  # one attempt per tick → retries spread ~10min apart
COVER_REPAIR_LIFETIME_ATTEMPT_CAP = 9  # three bounded repair generations; never loop forever
MAX_PARALLEL_PRODUCE = 5  # slices are independent; produce them concurrently (each is
# network-bound on AGY/CPA/gpt-image-2, so a few in flight
# cut wall-clock ~3x; bounded by free CPU + CPA concurrency)
# Top-5 is a ceiling, not a promise to ship five weak events.  The 2026-07-16
# 0.78-confidence 《夏雪冬花》 candidate was admitted only because the session
# still had an empty seat; that is the same quota-pressure failure mode that
# used to split one coherent event into two clips.  Low-confidence recalls are
# terminally recorded as not selected instead of living in the retry backlog.
MIN_TALK_CONFIDENCE = 0.80
PIECE_PRE_MS = 10_000
# Post context must cover the deterministic source-review demand for the
# common (origin == semantic end) case in one pass: initial repair cap 30s +
# source witness reserve 15s + margin.  A short window is not cheaper — it
# forces a widened-context retry that re-transcribes the whole padded window.
PIECE_POST_MS = 48_000
BOUNDARY_CONTEXT_RETRY_POST_MS = 90_000
BOUNDARY_REPAIR_INITIAL_CAP_MS = 30_000
BOUNDARY_REPAIR_RETRY_CAP_MS = 60_000
SPEAKER_ROUTING_FINAL_TAIL_GUARD_MS = 1_000
SONG_WINDOW_PRE_MS = 15_000  # window must stay SONG-dominated or the in-window
SONG_WINDOW_POST_MS = 20_000  # recall reclassifies it as talk (smoke-proven at
# ±60/45s and ±180/150s); 15/20s matches the
# validated 虫儿飞 run.
# A recall window is still only an anchor.  If it identifies a song but cannot
# prove both LRC ends, retry once with enough ORIGINAL source for a normal
# full-length performance.  The old ±45s retry only repaired slightly clipped
# anchors; on 2026-07-16 it fed 158s of a roughly seven-minute 《CRYING FOR
# YOU》 performance to the LRC gate, so the strict gate correctly rejected an
# artifact that the retry itself had made incomplete.  Detection anchors tend
# to cover the first verse, hence the deliberately tail-heavy 2m/6m envelope.
# The segment duration still caps the window, and the seeded anchor keeps the
# selector focused when the envelope also contains pre/post-song talk.
SONG_PROOF_RETRY_PRE_MS = 120_000
SONG_PROOF_RETRY_POST_MS = 360_000
SONG_TALK_QUARANTINE_GUARD_MS = 5_000
SESSION_INTRO_BGM_MAX_OFFSET_MS = 180_000
SESSION_OUTRO_BGM_MAX_REMAINING_MS = 240_000
SONG_ANCHOR_TRIM_MIN_MS = 20_000  # only retry on the danmaku-dense core when the
# trim drops ≥20s of talk padding off an end
DATE_RX = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# Read-only presentation modules must not invalidate media/content evidence or
# wake recoverable production work. Their output is regenerated from state.
PIPELINE_FINGERPRINT_EXCLUSIONS = {
    "src/autoslice/reporting.py",
}
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
    """Proof-closure fingerprint used to retry old recoverable BLOCKs."""

    return _pipeline_fingerprint(
        repo_root=REPO_ROOT, profile_tool=profile_tool, channel_profile=CHANNEL_PROFILE,
        exclusions=PIPELINE_FINGERPRINT_EXCLUSIONS,
        speaker_authority=_speaker_routing_provider_authority,
        speaker_authority_errors=(OSError, TypeError, ValueError, SpeakerRoutingError),
    )


def song_pipeline_fingerprint() -> str:
    """Hash only surfaces that can change Song proof or Song delivery."""

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
        "song_talk_quarantine_guard_ms": SONG_TALK_QUARANTINE_GUARD_MS,
        "session_intro_bgm_max_offset_ms": SESSION_INTRO_BGM_MAX_OFFSET_MS,
        "session_outro_bgm_max_remaining_ms": SESSION_OUTRO_BGM_MAX_REMAINING_MS,
        "song_anchor_trim_min_ms": SONG_ANCHOR_TRIM_MIN_MS,
        "song_terminal_performer_rejection_codes": sorted(SONG_TERMINAL_PERFORMER_REJECTION_CODES),
        "song_infra_transient_reason_codes": sorted(SONG_INFRA_TRANSIENT_REASON_CODES),
        "cpa_deep_command": CPA_CMD_DEEP,
        "cpa_title_command": CPA_CMD_TITLE,
        "cpa_standard_command": CPA_CMD_STANDARD,
        "cpa_structured_command": CPA_CMD_STRUCTURED,
    }
    return _song_pipeline_fingerprint(
        repo_root=REPO_ROOT, profile_id=PROFILE_ID, channel_profile=CHANNEL_PROFILE,
        profile_tool=profile_tool, profile_asset_file=profile_asset_file,
        profile_asset_directory=profile_asset_directory, policy=policy,
    )


def human_truth_mode() -> str:
    """Select whether reviewed candidate truth may enter generation inputs."""

    mode = os.environ.get("AUTOSLICE_HUMAN_TRUTH_MODE", "delivery").strip().lower()
    if mode not in {"delivery", "withheld"}:
        raise ValueError("AUTOSLICE_HUMAN_TRUTH_MODE must be delivery or withheld")
    return mode


def candidate_text_override_path(candidate_id: str) -> Path | None:
    """Return the one canonical candidate override, rejecting path indirection."""

    return resolve_candidate_truth_asset_path(
        root=profile_asset_directory("subtitle_text_overrides"),
        candidate_id=candidate_id,
        suffix=".text.v1.json",
        truth_is_available=human_truth_mode() != "withheld",
        label="subtitle text override",
    )


def candidate_subtitle_regression_path(candidate_id: str) -> Path | None:
    """Return the one canonical candidate regression gate, without indirection."""

    return resolve_candidate_truth_asset_path(
        root=profile_asset_directory("subtitle_regressions"),
        candidate_id=candidate_id,
        suffix=".subtitle-regression.v1.json",
        truth_is_available=human_truth_mode() != "withheld",
        label="subtitle regression",
    )


def candidate_reviewed_subtitle_baseline(
    candidate_id: str,
) -> ReviewedSubtitleBaseline | None:
    """Return the candidate's hash-bound reviewed text, independent of cover state."""

    if not re.fullmatch(r"[A-Za-z0-9_-]{1,96}", str(candidate_id or "")):
        raise ValueError("unsafe candidate id for reviewed subtitle baseline")
    if human_truth_mode() == "withheld":
        return None
    return load_candidate_reviewed_subtitle_baseline(
        profile_asset_directory("reviewed_subtitle_baselines"),
        candidate_id,
    )


def candidate_speaker_override_path(candidate_id: str) -> Path | None:
    """Return the candidate's hash-bound speaker truth, withheld during blind tests."""

    return resolve_candidate_truth_asset_path(
        root=profile_asset_directory("speaker_overrides"),
        candidate_id=candidate_id,
        suffix=".speaker.v1.json",
        truth_is_available=human_truth_mode() != "withheld",
        label="speaker override",
    )


def candidate_entity_projection_path(candidate_id: str) -> Path | None:
    """Return this candidate's canonical entity surfaces without indirection."""

    asset_root_relative = CHANNEL_PROFILE.asset_root.relative_to(CHANNEL_PROFILE.repo_root)
    root = REPO_ROOT / asset_root_relative / "candidate_entity_projections"
    return resolve_candidate_truth_asset_path(
        root=root,
        candidate_id=candidate_id,
        suffix=".entity-projection.v1.json",
        truth_is_available=human_truth_mode() != "withheld",
        label="entity projection",
    )


def candidate_public_text_surface_authority_path(candidate_id: str) -> Path | None:
    asset_root_relative = CHANNEL_PROFILE.asset_root.relative_to(CHANNEL_PROFILE.repo_root)
    return resolve_candidate_truth_asset_path(
        root=REPO_ROOT / asset_root_relative / "candidate_public_text_surface_authorities",
        candidate_id=candidate_id,
        suffix=".public-text-surface-authority.v1.json",
        truth_is_available=human_truth_mode() != "withheld",
        label="public text surface authority",
    )


def talk_pipeline_fingerprint(candidate_id: str) -> str:
    """Base code/config proof plus only this talk's optional truth assets."""

    base = pipeline_fingerprint()
    reviewed_baseline = candidate_reviewed_subtitle_baseline(candidate_id)
    truth_assets = [
        path
        for path in (
            candidate_text_override_path(candidate_id),
            candidate_subtitle_regression_path(candidate_id),
            candidate_speaker_override_path(candidate_id),
            candidate_entity_projection_path(candidate_id),
            candidate_public_text_surface_authority_path(candidate_id),
        )
        if path is not None
    ]
    if reviewed_baseline is not None:
        truth_assets.extend(reviewed_baseline.fingerprint_paths)
    # Preserve the historical base fingerprint for the overwhelmingly common
    # no-override case.  Adding/removing this candidate's truth asset still
    # changes/reverts its fingerprint without waking every legacy talk once.
    return candidate_truth_fingerprint(
        base_fingerprint=base,
        candidate_id=candidate_id,
        asset_paths=truth_assets,
        repo_root=REPO_ROOT,
        truth_withheld=human_truth_mode() == "withheld",
    )


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
from src.autoslice.talk_failure_recovery_policy import (  # noqa: E402
    subtitle_authority_recovery_relatives,
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
    CONTENT_BOUNDARY_RECOVERY_RELATIVES,
    TALK_RECOVERY_FAILURE_STATUSES,
    _song_delivery_recovery_authority,
    apply_talk_backfill_rejection_policy,
    backfillable_talk_rejection,
    historical_source_recovery_in_progress,
    recover_bound_song_deliveries,
    bind_song_delivery_recovery_authority,
    requeue_recoverable_deliveries,
    requeue_stale_current_recovery_talks,
    requeue_recoverable_songs,
    requeue_recoverable_talks,
)
from src.autoslice.candidate_selection import (  # noqa: E402
    _exact_talk_contract_ids,
    exact_talk_contract_closure,
    session_sealed,
    song_delivery_budget,
    _remember_song_quarantine_interval,
    _note_not_selected,
    exclude_session_edge_bgm_candidates,
    quarantine_overlapping_talk_candidates,
    backlog_has_eligible_session_work,
    refill_songs,
    prioritize, replace_scoped_pending_talk_items, scoped_pending_talk_items,
)
from src.autoslice import historical_failed_talk_scope, operator_processing_scope as operator_scope  # noqa: E402
from src.autoslice import selected_source_fact_recovery_persistence  # noqa: E402
from src.autoslice.exact_talk_recovery_scope import (
    suppress_exact_talk_recovery_song_work,
)  # noqa: E402
from src.autoslice.selection_rescore import split_produce_blocked_talk_items  # noqa: E402
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


def verified_song_fallback_title(song_title: str | None, hook: str | None = None) -> str | None:
    return _song_completion.verified_song_fallback_title(
        song_title,
        hook,
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
    return _speaker_routing_session._speaker_date_root(date, ctx=_speaker_runner_context())


def _speaker_generation_root(date: str, pipeline: str) -> Path:
    return _speaker_routing_session._speaker_generation_root(date, pipeline, ctx=_speaker_runner_context())


def _write_speaker_session_authority(path: Path, document: dict) -> str:
    return _speaker_routing_session._write_speaker_session_authority(path, document, ctx=_speaker_runner_context())


def prepare_speaker_routing(date: str, items: list[dict], *, state: dict | None = None) -> dict | None:
    return _speaker_routing_session.prepare_speaker_routing(date, items, state=state, ctx=_speaker_runner_context())


def _capture_state_from_result(result: dict) -> dict[str, object]:
    return _speaker_routing_session._capture_state_from_result(result, ctx=_speaker_runner_context())


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

    scoped_failure_kinds = {
        "content_boundary",
        "speaker_evidence",
        "runtime_prerequisite",
        "subtitle_authority",
    }
    if failure_kind not in scoped_failure_kinds:
        return talk_pipeline_fingerprint(candidate_id)
    if failure_kind == "content_boundary":
        relatives = CONTENT_BOUNDARY_RECOVERY_RELATIVES
    elif failure_kind == "subtitle_authority":
        relatives = subtitle_authority_recovery_relatives(profile_asset_file("subtitle_truth_ledger"))
    else:
        relatives = (
            "scripts/produce_slice_package.py",
            "src/autoslice/speaker_common.py",
            "src/autoslice/speaker_context.py",
            "src/autoslice/speaker_evidence.py",
            "src/autoslice/speaker_finalizer.py",
            # 证据不足时的 best-effort 分离本体（Ivan 2026-08-10 第二次裁定）：
            # 它现在也是"能修好一条说话人失败"的代码之一，改了必须唤醒停泊件。
            "src/autoslice/speaker_guess.py",
            profile_asset_file("voiceprint_profile"),
        )
    paths = [relative if isinstance(relative, Path) else REPO_ROOT / relative for relative in relatives]
    # A sealed exhaustive reviewed baseline is a candidate-specific repair for
    # subtitle-authority failures.  Include only its own hash-bound closure;
    # an unrelated candidate baseline must not consume this retry's budget.
    if failure_kind == "subtitle_authority":
        reviewed_baseline = candidate_reviewed_subtitle_baseline(candidate_id)
        if reviewed_baseline is not None:
            paths.extend(reviewed_baseline.fingerprint_paths)
    override = candidate_text_override_path(candidate_id) if failure_kind == "subtitle_authority" else (
        candidate_speaker_override_path(candidate_id) if failure_kind in {"speaker_evidence", "runtime_prerequisite"} else None
    )
    paths.extend([override] if override is not None else [])
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
    env["AGY_MODEL"] = os.environ.get("SONG_AGY_MODEL", "Gemini 3.6 Flash (High)")
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
    # judge/声学缓存根：主树固定 BASE；V15 恢复树由其 launcher 覆写。
    env.setdefault("AUTOSLICE_BASE", str(BASE))
    # 产线子进程的 stdout 进日志 sink（非 TTY）：不关缓冲的话 libc 全缓冲，
    # 每候选日志十几分钟 0 字节、退出才 dump——观察者只能猜死没死。
    env.setdefault("PYTHONUNBUFFERED", "1")
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
    selected_assets = {}
    for asset_key, state_name, env_suffix in (
        ("timely_terms", "timely_terms.json", "TIMELY_TERMS"),
        ("psplive_roster", "psplive_roster.json", "PSPLIVE_ROSTER"),
        ("streamer_registry", "streamer_registry.json", "STREAMER_REGISTRY"),
        ("community_names", "community_names.json", "COMMUNITY_NAMES"),
    ):
        selected_assets[asset_key] = bind_runtime_candidate_asset(
            env,
            selectors=os.environ,
            truth_mode=truth_mode,
            env_suffix=env_suffix,
            runtime_path=BASE / "state" / state_name,
            committed_path=profile_asset_file(asset_key),
            digest_file=_sha256_regular_file,
        )
    timely_terms = selected_assets["timely_terms"]

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
    if truth_mode == "withheld" and topic_graph is not None and topic_graph.is_file() and not topic_graph.is_symlink():
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
                and graph_payload.get("input_timely_terms_sha256") == _sha256_regular_file(timely_terms)
            )
        except (OSError, ValueError, AttributeError):
            graph_matches_blind_snapshot = False
        if not graph_matches_blind_snapshot:
            topic_graph = None
    if topic_graph is not None and topic_graph.is_file() and not topic_graph.is_symlink():
        env["LIDOUSHA_TOPIC_ENTITY_GRAPH"] = str(topic_graph.resolve())
        env["LIDOUSHA_TOPIC_ENTITY_GRAPH_SHA256"] = "sha256:" + _sha256_regular_file(topic_graph)
        env.pop("LIDOUSHA_DISABLE_TOPIC_ENTITY_GRAPH", None)
    elif truth_mode == "withheld":
        env["LIDOUSHA_DISABLE_TOPIC_ENTITY_GRAPH"] = "1"
        env.pop("LIDOUSHA_TOPIC_ENTITY_GRAPH", None)
        env.pop("LIDOUSHA_TOPIC_ENTITY_GRAPH_SHA256", None)
    return env


def child_env_for_date(recording_date: str) -> dict[str, str]:
    env = child_env()
    env["LIDOUSHA_TERM_AS_OF"] = recording_date
    # 会话游戏语境（Ivan 2026-08-07 指令：鹅鸭杀场三症状通病修复）。检测与
    # 状态文件都在 src.autoslice.game_context；这里只按日期绑定 env，失败即
    # 无语境，绝不阻断产线。
    bind_session_game_context(
        env,
        recording_date=recording_date,
        recordings_root=REC_ROOT,
        cache_root=BASE / "cache",
        state_root=BASE / "state",
        glossary_path=profile_asset_file("game_glossary"),
        truth_mode=human_truth_mode(),
        selectors=os.environ,
    )
    bind_session_theme_hints(
        env,
        recording_date=recording_date,
        snapshot_path=BASE / "state" / "streamer_dynamics.json",
        state_root=BASE / "state",
        truth_mode=human_truth_mode(),
        selectors=os.environ,
    )
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
            {
                "model": model,
                "input": "回复:OK",
                "reasoning": {"effort": "low"},
                "max_output_tokens": 2000,
            }
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


def recorder_live_status() -> bool | None:
    """True=active, False=sealed, None=unknown (src.autoslice.live_gate)."""

    return live_gate.read_recorder_live_status(
        RECORDER_STATUS_PATH,
        room=ROOM,
        log=log,
        max_age_seconds=RECORDER_STATUS_MAX_AGE_SECONDS,
    )


def live_hold_recheck(*, allow_eval_hold_override: bool = True) -> bool:
    """Positive-only mid-tick live gate (src.autoslice.live_gate)."""

    return live_gate.positive_live_hold_recheck(
        RECORDER_STATUS_PATH,
        recorder_live_status,
        lambda live: _live_hold_active(live, allow_eval_hold_override=allow_eval_hold_override),
    )


def live_determination_basis(live: bool | None) -> dict:
    """Recorder evidence + today's day dir (src.autoslice.live_gate)."""

    return live_gate.live_determination_basis(live, RECORDER_STATUS_PATH, list_dates)


def state_path(date: str) -> Path:
    return BASE / "state" / f"{date}.json"


def _apply_runtime_publication_projection(date: str, state: dict) -> dict:
    try:
        changed = publication_reconciliation.apply_runtime_publications_to_state(
            date=date,
            state=state,
            autoslice_base=BASE,
        )
    except (
        OSError,
        publication_reconciliation.PublicationReconciliationError,
    ) as exc:
        if state.get("status") != "publication_reconciliation_blocked":
            state["prepublication_reconciliation_status"] = state.get("status")
        state["status"] = "publication_reconciliation_blocked"
        state["publication_reconciliation_error"] = str(exc)
        write_state(date, state)
        return state
    if state.get("status") == "publication_reconciliation_blocked":
        state["status"] = state.pop("prepublication_reconciliation_status", "no_delivery")
        state.pop("publication_reconciliation_error", None)
        changed = True
    if changed:
        write_state(date, state)
    return state


def _read_state_untracked(date: str) -> dict:
    """State loader that never mistakes damage for a fresh start.

    Missing file → {} (genuinely new date).  Corrupt JSON → the damaged file is
    quarantined for forensics and the .bak (previous good write) is restored;
    with no usable .bak the date is BLOCKED (state_corrupt_blocked), because
    reprocessing 'from scratch' would re-produce and re-deliver everything
    (2026-07-09 audit: corruption must be loud, not a silent reset)."""
    path = state_path(date)
    bak = path.with_suffix(".json.bak")
    try:
        return _apply_runtime_publication_projection(date, json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        try:  # crash window between the two os.replace()s in write_state
            return _apply_runtime_publication_projection(date, json.loads(bak.read_text(encoding="utf-8")))
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
            blocked = {
                "status": "state_corrupt_blocked",
                "state_error": f"corrupt json, no usable .bak: {exc}",
            }
            path.write_text(json.dumps(blocked, ensure_ascii=False, indent=2), encoding="utf-8")
            return blocked
        log(f"state for {date} restored from .bak")
        restored["state_restored_from_bak"] = True
        write_state(date, restored)
        return _apply_runtime_publication_projection(date, restored)


def read_state(date: str) -> dict:
    return runner_state_writeback.track_state(state_path(date), _read_state_untracked(date))


write_state = runner_state_writeback.make_date_state_writer(
    state_path,
    runtime_root=lambda: BASE,
    updated_at=lambda: time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    log=lambda message: log(message),
)


def write_alert(name: str, message: str) -> None:
    """Append a local alert; the Mac launchd pull is the delivery channel."""
    _write_alert(BASE, name, message)


def source_health_error() -> str | None:
    """Probe the recordings mount via a subprocess `ls` so a HUNG FUSE mount
    (which blocks Python's stat() forever) times out instead of wedging the
    tick.  Returns None when healthy, else a short error string.  2026-07-09:
    the CloudDrive endpoint died and every layer above swallowed the OSError
    into 'no dates' — the control plane kept reporting green for hours."""
    try:
        completed = subprocess.run(["ls", str(REC_ROOT)], check=False, capture_output=True, text=True, timeout=25)
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
    try:
        load_selected_selection_calibration_policy()
    except SelectionCalibrationPolicyError as exc:
        return f"SELECTION_CALIBRATION_POLICY_INVALID: {exc.reason_code}: {exc.path}"
    return None


def list_dates() -> list[str]:
    try:
        names = [p.name for p in REC_ROOT.iterdir() if p.is_dir() and DATE_RX.match(p.name)]
    except OSError as exc:
        log(f"list_dates: recordings root unreadable: {exc}")
        return []
    selected = set(sorted(names)[-3:])
    # A historical date with a known finalized-source gap must not age out of
    # the latest-three cron window before the new recovery lane can repair it.
    # Keep a historical source recovery visible until its recovered work leaves
    # sealing/processing and its pending queues drain.  Otherwise a successful
    # source repair changes ``source_incomplete`` to ``sealing`` and immediately
    # ages itself out before the next tick can materialize the recovered clips.
    # Old completed/review dates still remain dormant.
    state_dir = BASE / "state"
    for date in names:
        if date in selected:
            continue
        path = state_dir / f"{date}.json"
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        recovery_in_progress = historical_source_recovery_in_progress(state, TALK_COVER_PENDING_STATUS)
        # 第三条例外：运维显式点名（Ivan 2026-08-10 逐字「87 现在需要纳入处理
        # 范围」）。判据、出处校验与"干完就自动出圈"全在
        # src/autoslice/operator_processing_scope.py。
        admission = operator_scope.operator_scope_admission(state, date=date)
        if admission.log_line:
            log(f"list_dates: {date}: {admission.log_line}")
        if state.get("status") == "source_incomplete" or recovery_in_progress or admission.admitted:
            selected.add(date)
    return sorted(selected)


def list_segments(date: str) -> list[Path]:
    date_dir = REC_ROOT / date
    try:
        files = sorted(date_dir.glob(f"{ROOM}_*.mp4"))
    except OSError as exc:
        log(f"list_segments({date}): unreadable: {exc}")
        return []
    # The CloudFS FUSE view can emit duplicate directory entries for one file
    # (observed 2026-07-19 while its upload queue drained); keep one per name.
    unique: dict[str, Path] = {}
    for f in files:
        if f.parent == date_dir:
            unique.setdefault(f.name, f)
    return list(unique.values())


def ffprobe_ms(path: Path) -> int:
    completed = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        check=False,
        capture_output=True,
        text=True,
        timeout=600,
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
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "free_asr_client.py"),
            str(segment),
            "--srt",
            str(cache),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=900,
        cwd=str(REPO_ROOT),
        env=child_env(),
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
                if (
                    re.sub(r"\D", "", jsonl.stem) == digits
                    and not jsonl.is_symlink()
                    and jsonl.is_file()
                    and jsonl.stat().st_size > 0
                ):
                    return jsonl
        except OSError:
            continue
    return None


def resolve_structured_chat_binding(
    segment: Path,
    *,
    source_sha256: str | None = None,
) -> dict[str, object]:
    """Bind a segment to structured chat without timestamp-near guessing.

    Legacy recordings with neither a sidecar nor a committed source alias keep
    the explicit ``required=false`` compatibility state.  Once a source alias
    exists, however, the exact alias media hash and canonical sidecar are
    mandatory: silently dropping that evidence would recreate the subtitle
    failures this binding is designed to prevent.
    """

    return _resolve_structured_chat_binding(
        segment,
        alias_ledger_path=profile_asset_file("subtitle_truth_ledger"),
        canonical_rec_root=CANONICAL_REC_ROOT,
        direct_jsonl=find_chat_jsonl(segment),
        source_sha256=source_sha256,
    )


_DIAN_GE_RX = re.compile(r"^点歌\s*(.+)$")
_TRAILING_PUNCT_RX = re.compile(r"[\s,.!?~～，。！？、·…\-_]+$")

_SRT_TS_RX = re.compile(r"(\d\d):(\d\d):(\d\d),(\d\d\d)\s*-->\s*(\d\d):(\d\d):(\d\d),(\d\d\d)")


def produce_batch(
    date: str, items: list[dict], produce_fn, *, prepare_only: bool = False,
) -> list[dict]:
    """Windowed concurrent production with deploy-yield (src.autoslice.produce_dispatch)."""
    return _dispatch_produce_batch(
        date, items, produce_fn, globals(), prepare_only=prepare_only,
    )


def _project_terminal_batch_state(state: dict, *, mutate_songs: bool = True) -> dict[str, object]:
    """Project one honest terminal status from closure, delivery, and retry state.

    Retry scheduling is metadata, not completion authority.  In particular, an
    exact recovery contract must remain ``recovery_incomplete`` while any
    selected candidate is missing or noncompliant even when another record has
    a future retry timestamp.
    """

    exact_closure = exact_talk_contract_closure(state)
    retry_epoch = scheduled_retry_epoch(state)
    return project_terminal_batch_state(
        state,
        delivered_talk_statuses=DELIVERED_TALK_STATUSES,
        talk_failure_statuses=TALK_RECOVERY_FAILURE_STATUSES,
        cover_pending_status=TALK_COVER_PENDING_STATUS,
        exact_closure=exact_closure,
        retry_epoch=retry_epoch,
        terminal_song_performer_rejection_codes=(SONG_TERMINAL_PERFORMER_REJECTION_CODES),
        song_infra_transient_reason_codes=SONG_INFRA_TRANSIENT_REASON_CODES,
        song_infra_retry_cap=SONG_INFRA_RETRY_CAP,
        mutate_songs=mutate_songs,
    )


def process_date(date: str) -> None:
    # A durable producer batch is always replayed before any health/provider
    # gate or new dispatch.  Its private handles already bind all artifacts;
    # recovery therefore never reruns CPA/AGY/cover work.
    try:
        resumed = resume_prepared_batch_if_present(runtime_root=BASE, date=date)
    except ProducerBatchTransactionError as exc:
        log(f"{date}: producer batch recovery blocked: {exc}")
        return
    except Exception as exc:
        log(f"{date}: runner commit lease unavailable for producer recovery: {exc}")
        return
    if resumed is not None:
        log(f"{date}: resumed prepared producer batch without provider dispatch")
    state = initialize_date_state(read_state, date)
    if (blocked := pre_dispatch_block_message(state, date=date)) is not None:
        write_alert(blocked[0], blocked[1])
        log(blocked[2])
        return
    frozen_talk_candidate_ids = historical_failed_talk_scope.freeze(state, date=date)
    song_preimage = operator_scope.snapshot_song_state_collections(state) if frozen_talk_candidate_ids is not None else None
    def persist_state() -> None:
        operator_scope.restore_song_state_collections(state, song_preimage)
        write_state(date, state)
    runtime_err = runtime_health_error()
    if runtime_err:
        changed = state.get("runtime_error") != runtime_err or state.get("status") != "paused_runtime_invalid"
        state["status"] = "paused_runtime_invalid"
        state["runtime_error"] = runtime_err
        persist_state()
        if changed:
            write_alert("RUNTIME_INVALID", f"{date}: {runtime_err}")
        log(f"{date}: runtime invalid — batch deferred without consuming candidate retries: {runtime_err}")
        return
    state.pop("runtime_error", None)
    try:
        recovered_hls = recover_finalized_legacy_hls(
            REC_ROOT / date,
            room_id=ROOM,
        )
    except LegacyHlsRecoveryError as exc:
        recovered_hls = []
        state["source_recovery_error"] = str(exc)
    else:
        state.pop("source_recovery_error", None)
        if recovered_hls:
            state.setdefault("source_recoveries", []).extend(recovered_hls)
            persist_state()
            log(f"{date}: recovered {len(recovered_hls)} finalized legacy HLS segment(s) into hash-bound MP4")
    source_inventory = audit_finalized_recording_inventory(
        REC_ROOT / date, room_id=ROOM, adapter_state_path=RECORDER_ADAPTER_STATE_PATH
    )
    previous_source_inventory = state.get("source_integrity")
    state["source_integrity"] = source_inventory
    if not source_inventory["can_select"]:
        changed = state.get("status") != "source_incomplete" or previous_source_inventory != source_inventory
        state["status"] = "source_incomplete"
        persist_state()
        write_reports(date, state)
        codes = sorted(
            {
                str(issue.get("code") or "SOURCE_INCOMPLETE")
                for issue in source_inventory.get("issues", [])
                if isinstance(issue, dict)
            }
        )
        if changed:
            write_alert(
                "SOURCE_INCOMPLETE",
                f"{date}: source inventory blocks selection ({','.join(codes)})",
            )
        log(f"{date}: source incomplete — selection blocked before early return ({','.join(codes)})")
        return
    session_changed = historical_failed_talk_scope.annotate_sessions(date, state, include_song_rows=frozen_talk_candidate_ids is None, candidate_ids=frozen_talk_candidate_ids)
    if session_changed:
        persist_state()
        if session_changed < 0:
            return
    automatic_maintenance = date >= AUTOMATIC_MAINTENANCE_NOT_BEFORE
    (
        recovered_song_deliveries,
        requeued_stale_talks,
        requeued_talks,
        requeued_songs,
        song_fingerprint_baseline_changed,
    ) = maintain_selected_source_fact_recovery(
        runtime_root=BASE,
        maintain=selected_source_fact_recovery_persistence.maintain_and_persist,
        date=date, state=state, automatic_maintenance=automatic_maintenance,
        candidate_ids=frozen_talk_candidate_ids, persist=persist_state,
        readback=lambda: json.loads(state_path(date).read_text(encoding="utf-8")),
        log=log, song_pipeline_fingerprint=song_pipeline_fingerprint,
    )
    has_new, has_pending, needs_cover = historical_failed_talk_scope.work_flags(date, state, automatic_maintenance=automatic_maintenance, candidate_ids=frozen_talk_candidate_ids)
    if not has_new and not has_pending and not needs_cover:
        terminal = _project_terminal_batch_state(state, mutate_songs=frozen_talk_candidate_ids is None)
        persist_state()
        write_reports(date, state)
        if recovered_song_deliveries:
            log(f"{date}: finalized {recovered_song_deliveries} recovered song package(s) without requiring CPA")
        if terminal["retry_epoch"] is not None:
            log(f"{date}: terminal state {state['status']} with next retry at {state['next_retry_at']}")
        return
    # CPA gate: recall, reconcile, titles and covers all need the chat lane.
    # Recordings can wait — never produce garbage during a provider outage.
    if not cpa_healthy():
        state["status"] = "paused_cpa_down"
        persist_state()
        log(f"{date}: CPA chat lane down — batch deferred to a later tick")
        return
    log(f"processing {date}: new={has_new} pending={has_pending}")
    state.setdefault("picks", [])
    state.setdefault("songs", [])
    state["status"] = "processing"
    persist_state()
    historical_failed_talk_scope.discover(date, state, frozen_talk_candidate_ids)
    if frozen_talk_candidate_ids is None:
        suppress_exact_talk_recovery_song_work(state, phase="after_segment_discovery")
    # Session sealing: transcription/recall above runs as segments appear, but
    # SELECTION waits until the inventory is stable so every candidate of the
    # session competes for the quota (late segments used to arrive after the
    # slots were spent).  Cover repairs for already-delivered clips still run.
    if (has_new or has_pending) and not session_sealed(date, state):
        state["status"] = "sealing"
        persist_state()
        log(f"{date}: segment inventory not stable yet — selection deferred to next tick (sealing)")
        return
    refresh_count = historical_failed_talk_scope.refresh_scorecards(date, state, frozen_talk_candidate_ids)
    if refresh_count:
        persist_state()
    if refresh_count < 0:
        operator_scope.restore_song_state_collections(state, song_preimage)
        log(f"{date}: semantic-chat scorecard refresh blocked; production deferred")
        return
    # Keep a structured, session-wide snapshot for the unlabelled evidence
    # sidecar before prioritize() reduces production to top-5 talk clips.
    capture_candidates = historical_failed_talk_scope.prioritize_and_capture(date, state, frozen_talk_candidate_ids)
    if capture_candidates is None:
        persist_state()
        return
    if frozen_talk_candidate_ids is None:
        suppress_exact_talk_recovery_song_work(state, phase="after_prioritize")
    exact_contract_ids = set(_exact_talk_contract_ids(state))
    prepare_ok, routing_claim = historical_failed_talk_scope.prepare_production_context(date, state, frozen_talk_candidate_ids)
    if not prepare_ok:
        persist_state()
        return
    persist_state()
    # Phase C: produce talk picks CONCURRENTLY (they're independent; each is
    # network-bound on AGY/CPA/gpt-image-2).  title_failed picks (CPA title lane
    # flaky) stay pending and retry on a later tick — the succeeded ones are kept,
    # not re-done.  CPA was gated at entry; a mid-batch outage just fails a slice.
    while scoped_pending_talk_items(state, frozen_talk_candidate_ids):
        talk_items = scoped_pending_talk_items(state, frozen_talk_candidate_ids)
        talk_items, rescore_blocked_items = split_produce_blocked_talk_items(talk_items)
        if rescore_blocked_items:
            log(f"{date}: {len(rescore_blocked_items)} talk item(s) held for pending source-fact rescore")
        production_preimage = historical_failed_talk_scope.production_preimage(state, frozen_talk_candidate_ids)
        try:
            state_before_dispatch, results, prepared_entries = dispatch_prepared_lane(
                runtime_root=BASE, date=date, lane="talk", items=talk_items,
                state_path=state_path(date), state=state, produce_batch=produce_batch,
                produce_fn=produce_talk, runner=sys.modules[__name__],
            )
        except (runner_state_writeback.RunnerStateWritebackError, ValueError) as exc:
            log(f"{date}: Talk prepare protocol rejected: {exc}")
            return
        # deploy-yield 只返回已开工项（输入序前缀）；未派发的尾巴必须留在
        # 队列里等下个 tick，否则候选无声蒸发（2026-07-27 850_940 批次案）。
        deferred_tail = talk_items[len(results) :] + rescore_blocked_items
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
            candidate_id = str(item.get("cid") or item.get("candidate_id") or "")
            if apply_talk_backfill_rejection_policy(result, exact_selected=candidate_id in exact_contract_ids):
                rejected += 1
            if result.get("failure_recoverable") is True:
                recoverable_failure = True
            if result.get("status") in DELIVERED_TALK_STATUSES:
                result["bundle_lifecycle"] = "CURRENT"
                result["bundle_compliance"] = "COMPLIANT"
            elif result.get("status") == TALK_COVER_PENDING_STATUS:
                result["bundle_lifecycle"] = "PENDING_COVER"
                result["bundle_compliance"] = "COVER_REQUIRED"
            state["picks"].append(result)
        replace_scoped_pending_talk_items(
            state, frozen_talk_candidate_ids,
            retry + deferred_tail,
        )
        if not historical_failed_talk_scope.seal_production_transition(date, state, frozen_talk_candidate_ids, production_preimage):
            if not prepared_entries:
                persist_state()
            return
        if prepared_entries:
            try:
                commit_projected_prefix(
                    runtime_root=BASE, date=date, state_path=state_path(date),
                    state_before=state_before_dispatch, state=state,
                    entries=prepared_entries,
                )
            except Exception as exc:
                log(f"{date}: prepared talk prefix commit deferred: {exc}")
                return
        else:
            persist_state()
        if retry:  # some title lanes flaky → back off, resume the rest next tick
            state["status"] = "paused_cpa_down"
            persist_state()
            write_reports(date, state)
            log(f"{date}: {len(retry)} title(s) failed — will retry on a later tick")
            queue_collab_evidence_capture(
                date,
                state,
                capture_candidates,
                routing_claim=routing_claim,
            )
            persist_state()
            write_reports(date, state)
            return
        if deferred_tail:
            # 让位部署：本 tick 收官，尾巴已持久化在 pending_talk 等新代码。
            log(f"{date}: {len(deferred_tail)} talk item(s) deferred for deploy — resuming next tick")
            break
        if recoverable_failure:
            # The selected item is waiting on infrastructure.  Do not spend a
            # second candidate merely to hide the outage or exceed top-5 when
            # the original resumes.
            break
        if rejected and frozen_talk_candidate_ids is None:
            historical_failed_talk_scope.reprioritize(state, frozen_talk_candidate_ids)
            persist_state()
            continue
        break
    if frozen_talk_candidate_ids is None:
        suppress_exact_talk_recovery_song_work(state, phase="before_song_lane")
    # Song lane with bounded backfill: a gate-BLOCKED song frees its slot for
    # the next backlog song (danmaku-desc) until the delivery budget is met,
    # the backlog runs dry, or SONG_ATTEMPT_CAP is hit.
    while frozen_talk_candidate_ids is None and state["pending_song"]:
        song_items = list(state["pending_song"])
        try:
            state_before_dispatch, song_results, prepared_song_entries = dispatch_prepared_lane(
                runtime_root=BASE, date=date, lane="song", items=song_items,
                state_path=state_path(date), state=state, produce_batch=produce_batch,
                produce_fn=produce_song, runner=sys.modules[__name__],
            )
        except (runner_state_writeback.RunnerStateWritebackError, ValueError) as exc:
            log(f"{date}: Song prepare protocol rejected: {exc}")
            return
        state["songs"].extend(song_results)
        # 同 talk：deploy-yield 未派发的歌尾巴留队，不许无声蒸发。
        state["pending_song"] = song_items[len(song_results) :]
        if state["pending_song"]:
            if prepared_song_entries:
                try:
                    commit_projected_prefix(
                        runtime_root=BASE, date=date, state_path=state_path(date),
                        state_before=state_before_dispatch, state=state,
                        entries=prepared_song_entries,
                    )
                except Exception as exc:
                    log(f"{date}: prepared song prefix commit deferred: {exc}")
                    return
            else:
                write_state(date, state)
            log(f"{date}: {len(state['pending_song'])} song item(s) deferred for deploy — resuming next tick")
            break
        refill_songs(state)
        if prepared_song_entries:
            try:
                commit_projected_prefix(
                    runtime_root=BASE, date=date, state_path=state_path(date),
                    state_before=state_before_dispatch, state=state,
                    entries=prepared_song_entries,
                )
            except Exception as exc:
                log(f"{date}: prepared song prefix commit deferred: {exc}")
                return
        else:
            persist_state()
    finish_producer_date(
        runtime_root=BASE, date=date, state=state, mutate_songs=frozen_talk_candidate_ids is None,
        repair_covers=historical_failed_talk_scope.repair_covers,
        automatic_maintenance=automatic_maintenance, candidate_ids=frozen_talk_candidate_ids,
        project_terminal=_project_terminal_batch_state, persist_state=persist_state,
        write_reports=write_reports, log=log, capture_candidates=capture_candidates,
        routing_claim=routing_claim, queue_collab=queue_collab_evidence_capture,
    )


def write_heartbeat(body: str) -> None:
    _write_heartbeat(BASE, body)


def _live_hold_active(live: bool | None, *, allow_eval_hold_override: bool = True) -> bool:
    """直播期间冻结处理（True/未知都冻结，fail-safe；src.autoslice.live_gate）。"""

    return live_gate.live_hold_active(
        live,
        rec_root=REC_ROOT,
        list_dates=list_dates,
        log=log,
        # A historical authority is deliberately *not* an ignore-hold token:
        # only clean live=False reaches its normal tick.  The existing eval
        # switch is retained solely for non-historical isolated bases.
        ignore_hold=(
            allow_eval_hold_override
            and os.environ.get("AUTOSLICE_IGNORE_LIVE_HOLD", "") == "1"
        ),
    )


def tick(*, historical_date: str | None = None) -> int:
    if (BASE / "DISABLED").exists() and historical_date is None:
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
    live = recorder_live_status()
    basis = live_determination_basis(live)
    if _live_hold_active(live, allow_eval_hold_override=historical_date is None):
        report = live_gate.live_hold_report(
            live,
            basis,
            marker_path=BASE / "state" / "live_without_recording.json",
            warn_after_seconds=LIVE_WITHOUT_RECORDING_WARN_SECONDS,
        )
        for name, message in report["alerts"]:
            write_alert(name, message)
            log(f"{name}: {message}")
        write_heartbeat(report["heartbeat"])
        log(report["log"])
        return 0
    checked = []
    deferred: list[str] = []
    dates = [historical_date] if historical_date is not None else list_dates()
    for index, date in enumerate(dates):
        # 2026-08-09: a marathon tick read `live` once at 09:40 and was still
        # producing at 11:40 — 37 minutes into a stream that began at 11:03.
        # A stale start-of-tick reading must not license hours of work.
        rechecked_live_hold = (
            live_hold_recheck()
            if historical_date is None
            else live_hold_recheck(allow_eval_hold_override=False)
        )
        if rechecked_live_hold:
            deferred = list(dates[index:])
            log(f"room went LIVE mid-tick — deferring dates {' '.join(deferred)}")
            break
        state = read_state(date)
        if state.get("status") == "manual_preclaim":
            checked.append(f"{date}:manual_preclaim")
            continue
        checked.append(f"{date}:{state.get('status', 'new')}")
        process_date(date)
    suffix = f" live_yield_deferred={' '.join(deferred)}" if deferred else ""
    write_heartbeat(f"live={live} source=ok dates={' '.join(checked) or '(none)'}{suffix}")
    log(f"tick done: live={live} dates={' '.join(checked) or '(none)'}{suffix}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="single tick (cron entry point)")
    parser.add_argument(
        "--preclaim",
        nargs="+",
        metavar="DATE",
        help="mark dates as manually handled; runner will never touch them",
    )
    parser.add_argument(
        "--smoke-segment",
        type=Path,
        help="end-to-end smoke: recall+produce ONE talk candidate from this segment into the smoke area",
    )
    parser.add_argument(
        "--historical-authority",
        type=Path,
        help="sealed one-time historical authority; only legal with --once while DISABLED remains present",
    )
    args = parser.parse_args(argv)
    BASE.mkdir(parents=True, exist_ok=True)
    # Inject CPA credentials into OUR process too: the semantic-recall llm_call
    # runs llm_via_cpa.sh from this process (not via child_env()), and without
    # this the recall lane silently degrades to the deterministic fallback.
    os.environ.update(load_env_file(CPA_ENV))
    if args.historical_authority and not args.once:
        parser.error("--historical-authority requires --once")
    if args.historical_authority and (args.preclaim or args.smoke_segment):
        parser.error("--historical-authority cannot combine with preclaim or smoke")
    if args.historical_authority:
        try:
            # The manual path holds the outer tick lock throughout the normal
            # tick.  STARTED is durable before any process_date/provider work;
            # an interrupted STARTED receipt is intentionally unreplayable.
            with historical_exclusive_tick(BASE):
                started = load_and_start_historical_run(
                    authority_path=args.historical_authority, runtime_root=BASE,
                    recording_root=REC_ROOT, adapter_status_path=RECORDER_STATUS_PATH,
                    adapter_state_path=RECORDER_ADAPTER_STATE_PATH,
                    room_id=normalize_historical_room_id(ROOM),
                    recorder_endpoint=HISTORICAL_RECORDER_ENDPOINT, recorder_env=HISTORICAL_RECORDER_ENV,
                )
                try:
                    result = tick(historical_date=str(started["recording_date"]))
                except BaseException:
                    finish_historical_run(authority_path=args.historical_authority, runtime_root=BASE, failed=True)
                    raise
                finish_historical_run(authority_path=args.historical_authority, runtime_root=BASE, failed=result != 0)
                return result
        except HistoricalFastlaneAuthorityError as exc:
            log(f"historical authority refused: {exc}")
            return 2
    if args.preclaim:
        for date in args.preclaim:
            write_state(
                date,
                {
                    "status": "manual_preclaim",
                    "segments_done": [s.stem for s in list_segments(date)],
                },
            )
            log(f"preclaimed {date}")
        return 0
    if args.smoke_segment:
        segment = args.smoke_segment
        date = "smoke"
        srt = bcut_transcribe(segment, date)
        if srt is None:
            return 1
        xml = find_danmaku_xml(segment)
        chat_binding = resolve_structured_chat_binding(segment)
        candidates, lane, extras = recall_candidates(srt, danmaku_hints(xml), xml)
        talk = [c for c in candidates if getattr(c, "content_type_hint", "talk") != "song"]
        log(f"smoke: {len(candidates)} candidates via {lane}; producing first deliverable talk candidate")
        if not talk:
            log("smoke: no talk candidate found")
            return 1
        seg_tag = re.sub(r"\D", "", segment.stem)[-6:]
        seg_dur_ms = ffprobe_ms(segment)
        attempts = talk[:SMOKE_TALK_ATTEMPT_CAP]
        result: dict = {}
        for attempt_index, cand in enumerate(attempts, start=1):
            meta = extras.get(cand.anchor.candidate_id, {})
            item = {
                "cid": f"auto_{seg_tag}_{int(cand.boundary.resolved_start_ms) // 1000}_{int(cand.boundary.resolved_end_ms) // 1000}",
                "segment_path": str(segment),
                "seg_dur_ms": seg_dur_ms,
                "start_ms": max(0, int(cand.boundary.resolved_start_ms)),
                "end_ms": int(cand.boundary.resolved_end_ms),
                "xml": str(xml) if xml else None,
                **chat_binding,
                "hook": meta.get("hook", ""),
                "confidence": meta.get("confidence"),
                "selection_scorecard": (
                    dict(meta["selection_scorecard"]) if isinstance(meta.get("selection_scorecard"), dict) else None
                ),
                "lane": lane,
                "bcut_srt_path": str(srt),
                "filler_proposals": list(meta.get("filler_proposals") or []),
                "filler_proposal_srt_sha256": meta.get("filler_proposal_srt_sha256"),
                "merge_gap_removals": list(meta.get("merge_gap_removals") or []),
            }
            result = produce_talk(date, item)
            if result.get("status") in DELIVERED_TALK_STATUSES:
                if attempt_index > 1:
                    result = {**result, "smoke_backfill_attempt": attempt_index}
                break
            log(
                f"smoke: candidate {item['cid']} not delivered "
                f"(status={result.get('status')}); "
                f"attempt {attempt_index}/{len(attempts)}"
            )
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0 if result.get("status") in DELIVERED_TALK_STATUSES else 1
    if args.once:
        return tick()
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
