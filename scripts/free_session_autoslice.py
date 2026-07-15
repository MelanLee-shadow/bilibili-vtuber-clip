#!/usr/bin/env python3
"""Unattended post-stream auto-slice runner (runs ON the free host).

Ivan's goal (2026-07-05): when a 李豆沙 stream ends, free starts the FULL
canonical pipeline by itself — no human kick-off:

    stream end (blrec live_status via API)
      → per new segment: BCUT aggregate ASR transcript (ms timeline)
      → semantic recall candidate selection (CPA, viewer-perspective, with the
        curated slice-selection metric; deterministic fallback lanes if the
        LLM is down — zero-output is loud, never silent)
      → top-N talk candidates + up to 2 songs (highest danmaku)
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
  queue; final pick = top MAX_SONGS_PER_DATE by danmaku count.
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
- **Budget = deliveries**: gate-blocked songs no longer consume the per-date
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

BASE = Path(os.environ.get("AUTOSLICE_BASE", "/opt/bilive/autoslice"))
# Ivan 2026-07-13: during the speaker data-accumulation phase every delivered
# clip keeps the single host (李豆沙) subtitle style and speaker uncertainty
# must never reject a delivery. "required"/"auto" stay available for the
# future re-enable decision.
SPEAKER_MODE = os.environ.get("AUTOSLICE_SPEAKER_MODE", "uniform_host")
if SPEAKER_MODE not in {"uniform_host", "required", "auto"}:
    SPEAKER_MODE = "uniform_host"
ROOM = os.environ.get("AUTOSLICE_ROOM", "22966160")
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
        str(REPO_ROOT / "assets/lidousha/voiceprint_profile.v1.json"),
    )
)
HOST_VOCAL_REFERENCE_DIR = Path(
    os.environ.get("AUTOSLICE_HOST_VOCAL_REFERENCE_DIR", str(BASE / "voiceprints/lidousha"))
)
HOST_VOCAL_MODEL_DIR = Path(
    os.environ.get(
        "AUTOSLICE_HOST_VOCAL_MODEL_DIR",
        str(BASE / "models/campp"),
    )
)
MAX_TALK_PICKS = 5
TALK_ATTEMPT_CAP = 10  # reject unsafe content candidates and backfill, bounded
MAX_SONGS_PER_DATE = 2  # Ivan 2026-07-05: 每场直播至多两个歌切，按弹幕最高的两个
TALK_PER_SEGMENT_CAP = 2  # diversity guard on the GLOBAL confidence ranking; slack refills
SONG_ATTEMPT_CAP = 6  # per-pipeline-generation song attempts for one date
SONG_LIFETIME_ATTEMPT_CAP = 18  # absolute date cap including superseded attempts;
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
        "SONG_NOT_LIDOUSHA_SINGING",
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
        "scripts/regenerate_lidousha_cover.py",
        "scripts/repair_reviewed_covers.py",
        "scripts/resume_frozen_talk_package.py",
        "scripts/run_auto_review_shadow_pipeline.py",
        "scripts/run_full_session_selector_cpa_shadow.py",
        "assets/lidousha/entity_confusables.json",
        "assets/lidousha/glossary.txt",
        "assets/lidousha/intro/branding_intro.v1.json",
        "assets/lidousha/known_songs.json",
        "assets/lidousha/persona.md",
        "assets/lidousha/slice_selection_metric.md",
        "assets/lidousha/subtitle_correction_principles.md",
        "assets/lidousha/timely_terms.json",
        "assets/lidousha/topic_entity_graph.json",
        "assets/lidousha/title_style.md",
        "assets/lidousha/voiceprint_profile.v1.json",
    }
    paths = [REPO_ROOT / relative for relative in explicit]
    autoslice_src = REPO_ROOT / "src" / "autoslice"
    paths.extend(autoslice_src.rglob("*.py") if autoslice_src.is_dir() else [])
    cover_fonts = REPO_ROOT / "assets" / "lidousha" / "fonts"
    paths.extend(path for path in cover_fonts.rglob("*") if path.is_file())
    missing = [path for path in paths if not path.is_file()]
    for path in missing:
        hasher.update(path.relative_to(REPO_ROOT).as_posix().encode("utf-8") + b"\0MISSING\0")
    paths = [path for path in paths if path.is_file()]
    for path in sorted(paths, key=lambda value: value.relative_to(REPO_ROOT).as_posix()):
        relative = path.relative_to(REPO_ROOT).as_posix()
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
    root = REPO_ROOT / "assets" / "lidousha" / "subtitle_text_overrides"
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
    root = REPO_ROOT / "assets" / "lidousha" / "subtitle_regressions"
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
    root = REPO_ROOT / "assets" / "lidousha" / "speaker_overrides"
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
    verified_song_fallback_title,
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


def song_completion_evidence(record: dict) -> dict:
    """Runner wrapper (extracted 2026-07-15): the 843-line proof lives in
    src/autoslice/song_completion.py; inject the two runner-owned symbols it
    reaches (_has_exact_av_streams, HOST_VOCAL_PROFILE) so tests patching them
    on the runner keep steering the real proof path."""
    return _song_completion.song_completion_evidence(
        record,
        has_exact_av_streams=_has_exact_av_streams,
        host_vocal_profile=HOST_VOCAL_PROFILE,
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
            "src/autoslice/speaker_finalizer.py",
            "assets/lidousha/voiceprint_profile.v1.json",
        )
    paths = [REPO_ROOT / relative for relative in relatives]
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
    committed_timely_terms = REPO_ROOT / "assets" / "lidousha" / "timely_terms.json"
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

    blind_topic_graph = os.environ.get("AUTOSLICE_BLIND_TOPIC_ENTITY_GRAPH")
    configured_topic_graph = os.environ.get("AUTOSLICE_TOPIC_ENTITY_GRAPH")
    runtime_topic_graph = BASE / "state" / "topic_entity_graph.json"
    committed_topic_graph = REPO_ROOT / "assets" / "lidousha" / "topic_entity_graph.json"
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

    tracked_speaker_profile = REPO_ROOT / "assets" / "lidousha" / "voiceprint_profile.v1.json"
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


def date_chat_jsonl_files(date: str) -> list[Path]:
    """All structured live-event sidecars for a date's recordings, deduped.

    Reuses the same segment→sidecar lookup as segment discovery (``find_chat_jsonl``)
    instead of re-globbing the recordings root, so it stays consistent with
    whatever a test's ``list_segments``/``REC_ROOT`` monkeypatch already covers.
    """

    seen: dict[str, Path] = {}
    for segment in list_segments(date):
        jsonl = find_chat_jsonl(segment)
        if jsonl is not None:
            seen.setdefault(str(jsonl.resolve()), jsonl)
    return list(seen.values())


def _clean_dian_ge_title(raw: str) -> str | None:
    title = _TRAILING_PUNCT_RX.sub("", str(raw or "").strip())
    if not title or not (1 <= len(title) <= 20):
        return None
    return title


def dian_ge_song_titles(date: str, *, cap: int = 40) -> list[str]:
    """点歌 danmaku pool: titles a viewer explicitly requested by name.

    REUSES ``src.autoslice.chat_authority.load_chat_jsonl`` (the same
    authoritative structured danmaku/SC parser the finalization lane uses,
    including its recording-epoch handling) instead of a second bilibili
    DANMU_MSG decoder.
    """

    from src.autoslice.chat_authority import load_chat_jsonl

    titles: list[str] = []
    seen_norm: set[str] = set()
    for jsonl_path in date_chat_jsonl_files(date):
        try:
            evidence = load_chat_jsonl(jsonl_path)
        except (OSError, ValueError):
            continue
        for item in evidence:
            if item.kind != "danmaku":
                continue
            match = _DIAN_GE_RX.match(item.text.strip())
            if not match:
                continue
            title = _clean_dian_ge_title(match.group(1))
            if title is None:
                continue
            norm = title.lower()
            if norm in seen_norm:
                continue
            seen_norm.add(norm)
            titles.append(title)
            if len(titles) >= cap:
                return titles
    return titles


def visual_song_titles(state: dict) -> list[str]:
    """Screen-songlist titles seen anywhere in the date so far.

    The numbered songlist overlay (right-top panel) persists on screen across
    recording segments, so every segment's inventory entry — plus the
    cross-segment dedup identities already tracked in
    ``visual_song_seen_entries`` (``list:<n>:<title>`` / ``media:<stem>:<start_ms>:<title>``) —
    is in scope, not just the current segment's.
    """

    titles: list[str] = []
    seen_norm: set[str] = set()

    def _add(raw_title) -> None:
        title = str(raw_title or "").strip()
        if not title:
            return
        norm = normalize_visual_title(title)
        if not norm or norm in seen_norm:
            return
        seen_norm.add(norm)
        titles.append(title)

    inventory = state.get("visual_song_inventory")
    if isinstance(inventory, dict):
        for entry in inventory.values():
            if not isinstance(entry, dict):
                continue
            for candidate in entry.get("candidates") or []:
                if isinstance(candidate, dict):
                    _add(candidate.get("song_title"))
    for entry in state.get("visual_song_seen_entries") or []:
        if not isinstance(entry, str):
            continue
        if entry.startswith("list:"):
            parts = entry.split(":", 2)
            if len(parts) == 3:
                _add(parts[2])
        elif entry.startswith("media:"):
            parts = entry.split(":", 3)
            if len(parts) == 4:
                _add(parts[3])
    return titles


def known_song_titles() -> list[str]:
    """Curated recurring-song titles (assets/lidousha/known_songs.json).

    This is a code-owned static asset, not a per-candidate human-truth file —
    unlike ``candidate_text_override_path``/``candidate_subtitle_regression_path``/
    ``candidate_speaker_override_path`` it has no ``human_truth_mode() ==
    "withheld"`` gate anywhere in this codebase (it is already used
    unconditionally for song-lane fingerprint pinning in
    ``run_auto_review_shadow_pipeline._load_known_songs``), so it is safe to
    include in both delivery and withheld runs.
    """

    path = REPO_ROOT / "assets" / "lidousha" / "known_songs.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    songs = payload.get("songs") if isinstance(payload, dict) else None
    if not isinstance(songs, list):
        return []
    titles: list[str] = []
    for song in songs:
        if isinstance(song, dict):
            title = str(song.get("title") or "").strip()
            if title:
                titles.append(title)
    return titles


def collect_song_name_candidates(date: str, state: dict, *, cap: int = 60) -> list[str]:
    """Machine-evidence song-name pool for the talk lane's deterministic pin
    (Ivan 2026-07-13 — 7/11 delivery bug: ``下一首歌是爱拉拉爱`` instead of
    《爱啦啦》 with zero song-name context available to the correction lanes).

    Sources, in priority order: the screen songlist panel, 点歌 danmaku, then
    the curated known-songs table.  Every source here is machine evidence (or
    a code-owned asset) — never Ivan human-truth review, so this is safe in
    both delivery and withheld/blind runs.
    """

    titles: list[str] = []
    seen_norm: set[str] = set()

    def _extend(source: list[str]) -> None:
        for title in source:
            norm = normalize_visual_title(title)
            if not norm or norm in seen_norm:
                continue
            seen_norm.add(norm)
            titles.append(title)

    _extend(visual_song_titles(state))
    _extend(dian_ge_song_titles(date))
    _extend(known_song_titles())
    return titles[:cap]




_SRT_TS_RX = re.compile(
    r"(\d\d):(\d\d):(\d\d),(\d\d\d)\s*-->\s*(\d\d):(\d\d):(\d\d),(\d\d\d)"
)


def delivered_paths(date: str, rec: dict) -> tuple[Path, Path] | None:
    """(mp4, cover) delivery paths for a pick/song record, or None if the mp4
    was never delivered (failed/gated records have nothing to repair)."""
    if rec.get("delivered"):  # song lane records the delivered path explicitly
        mp4 = Path(rec["delivered"])
    else:
        name = safe_name(rec.get("hook", ""), rec.get("candidate_id", ""))
        mp4 = REPO_ROOT / "lidousha" / date / f"{name}.mp4"
    if not mp4.is_file():
        return None
    return mp4, mp4.with_suffix(".cover.png")


def cover_ref_for(date: str, cid: str) -> Path | None:
    """The producer's CLEAN reference frame (pre-burn).  Preferred over frame
    grabs from the delivered mp4, whose burned subtitles would leak into the
    gpt-image-2 identity reference."""
    return next(iter(sorted((BASE / "out" / date / cid).glob("**/cover_refs/*.cover-ref.png"))), None)


def _json_file_bytes(payload: dict) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _atomic_write_bytes_file(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _atomic_write_json_file(path: Path, payload: dict) -> None:
    _atomic_write_bytes_file(path, _json_file_bytes(payload))


def repair_covers(
    date: str,
    state: dict,
    *,
    candidate_ids: set[str] | frozenset[str] | None = None,
    expected_cover_texts: dict[str, str] | None = None,
) -> None:
    """Phase D: delivered clips whose REAL-AI cover was blocked (CPA image lane
    hiccups: gateway 400s, provider outages) get a bounded cover-only retry —
    the mp4 is already good, nothing is re-produced.  One attempt per record
    per tick; permanently blocked covers stay loud in the review summary."""
    records = state.get("picks", []) + state.get("songs", [])
    expected_cover_texts = dict(expected_cover_texts or {})
    if expected_cover_texts and (
        candidate_ids is None
        or any(
            re.fullmatch(r"[A-Za-z0-9_-]{1,96}", str(candidate_id or "")) is None
            or not isinstance(expected, str)
            or not expected.strip()
            for candidate_id, expected in expected_cover_texts.items()
        )
        or not set(expected_cover_texts).issubset(set(candidate_ids))
    ):
        raise ValueError("expected cover text authority must be scoped to selected candidates")
    if candidate_ids is not None:
        requested = set(candidate_ids)
        if not requested or any(
            re.fullmatch(r"[A-Za-z0-9_-]{1,96}", str(value or "")) is None
            for value in requested
        ):
            raise ValueError("selected cover repair candidate ids are invalid or empty")
        matches: dict[str, list[dict]] = {candidate_id: [] for candidate_id in requested}
        for record in records:
            if isinstance(record, dict) and record.get("candidate_id") in matches:
                matches[str(record["candidate_id"])].append(record)
        invalid = {
            candidate_id: len(rows)
            for candidate_id, rows in matches.items()
            if len(rows) != 1
        }
        if invalid:
            raise ValueError(
                f"selected cover repair candidates are missing or duplicated: {invalid}"
            )
        # Scope recovery, budget refresh, exhaustion handling, and provider
        # calls alike.  A selected repair must never mutate a neighboring
        # candidate merely because that record also happens to need a cover.
        records = [record for record in records if record.get("candidate_id") in requested]
    recovered = False
    for record in records:
        paths = delivered_paths(date, record)
        if paths is not None:
            recovered = (
                _roll_forward_prepared_cover_transactions(date, record, *paths)
                or recovered
            )
            recovered = _recover_committed_cover_binding(date, record, *paths) or recovered
    if recovered:
        write_state(date, state)
    fingerprint = pipeline_fingerprint()
    for record in records:
        if (record.get("status") in DELIVERED_TALK_STATUSES or record.get("delivered")) and record.get("title"):
            _refresh_cover_repair_budget(record, fingerprint)
    needed = [r for r in records if cover_repair_needed(date, r)]
    exhausted = [r for r in needed if not _cover_repair_eligible(r)]
    for record in exhausted:
        record["cover_integrity_status"] = "INVALID_REPAIR_BUDGET_EXHAUSTED"
        record["cover_repair_exhausted"] = True
        status = str(record.get("cover_status") or "BLOCKED_AI_COVER_REQUIRED")
        if "repair_budget_exhausted" not in status:
            record["cover_status"] = f"{status}(repair_budget_exhausted)"
    if exhausted:
        write_state(date, state)
    todo = [r for r in needed if _cover_repair_eligible(r)]
    if not todo:
        return
    for rec in todo:
        mp4, cover = delivered_paths(date, rec)
        try:
            _cover_authority_preflight(date, rec, mp4)
        except (OSError, ValueError) as exc:
            rec["cover_integrity_status"] = "INVALID_AUTHORITY_PREFLIGHT"
            rec["cover_status"] = "BLOCKED_COVER_AUTHORITY_PREFLIGHT"
            rec["cover_authority_preflight_error"] = f"{type(exc).__name__}: {exc}"
            log(
                f"cover repair {rec.get('candidate_id', '?')}: authority preflight "
                f"blocked before image request: {type(exc).__name__}: {exc}"
            )
            write_state(date, state)
            continue
        rec["cover_repair_attempts"] = rec.get("cover_repair_attempts", 0) + 1
        rec["cover_repair_lifetime_attempts"] = rec.get("cover_repair_lifetime_attempts", 0) + 1
        cid = rec.get("candidate_id", "?")
        log(f"cover repair {cid} (attempt {rec['cover_repair_attempts']}/{COVER_REPAIR_MAX_ATTEMPTS})")
        log_path = BASE / "logs" / f"{date}_{cid}_cover.log"
        ref = cover_ref_for(date, cid)
        src_args = ["--ref", str(ref)] if ref else ["--media", str(mp4)]
        repair_root = BASE / "out" / date / str(cid) / "cover_repair"
        attempt_id = (
            f"{fingerprint.removeprefix('sha256:')[:12]}-"
            f"{rec['cover_repair_attempts']:02d}-{time.time_ns()}"
        )
        generation_root = repair_root / "generations" / attempt_id
        generated_cover = generation_root / "final.cover.png"
        ai_background = generation_root / "ai-background.png"
        rec["cover_repair_last_generation_dir"] = str(generation_root)
        rec["cover_integrity_status"] = "REPAIR_ATTEMPT_IN_PROGRESS"
        rec["cover_repair_exhausted"] = False
        if cover.is_file():
            stale_sha = _sha256_regular_file(cover)
            stale_path = repair_root / "stale_covers" / f"{cover.name}.{stale_sha}.png"
            if not stale_path.is_file():
                stale_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(cover, stale_path)
        # Charge and persist the bounded attempt before the external image
        # request.  A process/host crash after provider spend cannot evade the
        # lifetime cap or replay the same budget slot forever.
        write_state(date, state)
        try:
            with open(log_path, "a", encoding="utf-8") as sink:
                completed = subprocess.run(
                    [sys.executable, str(REPO_ROOT / "scripts" / "regenerate_lidousha_cover.py"),
                     "--title", str(rec["title"]), *src_args,
                     "--candidate-id", str(cid), "--ai-bg", str(ai_background),
                     "--out", str(generated_cover)],
                    check=False, stdout=sink, stderr=subprocess.STDOUT, timeout=1200,
                    cwd=str(REPO_ROOT), env=child_env_for_date(date),
                )
            rc = completed.returncode
        except subprocess.TimeoutExpired:
            rc = -1
        bound = False
        if rc == 0 and generated_cover.is_file():
            try:
                expected_cover_text = expected_cover_texts.get(str(cid))
                if expected_cover_text is not None:
                    generation, _generation_path = _validate_repaired_cover_generation(
                        cover=generated_cover,
                        title=str(rec["title"]),
                        candidate_id=str(cid),
                    )
                    rendered_lines = generation.get("rendered_lines")
                    canonical = lambda value: re.sub(r"\s+", "", str(value))
                    if (
                        generation.get("cover_text") != expected_cover_text
                        or not isinstance(rendered_lines, list)
                        or not rendered_lines
                        or any(not isinstance(value, str) for value in rendered_lines)
                        or canonical("".join(rendered_lines))
                        != canonical(expected_cover_text)
                    ):
                        raise ValueError(
                            "generated cover did not preserve the reviewed text/punctuation"
                        )
                _bind_repaired_cover(date, rec, mp4, cover, generated_cover)
            except (OSError, ValueError, SongDeliveryError) as exc:
                with open(log_path, "a", encoding="utf-8") as sink:
                    sink.write(f"\nCOVER_BINDING_FAILED: {type(exc).__name__}: {exc}\n")
                rc = -2
            else:
                bound = True
                rec["cover_integrity_status"] = "VALID_BOUND"
                log(f"cover repaired and hash-bound → {cover.name}")
        if not bound:
            if (
                rec["cover_repair_attempts"] >= COVER_REPAIR_MAX_ATTEMPTS
                or rec["cover_repair_lifetime_attempts"] >= COVER_REPAIR_LIFETIME_ATTEMPT_CAP
            ):
                rec["cover_status"] = f"{rec.get('cover_status') or 'BLOCKED_AI_COVER_REQUIRED'}(repair_failed_x{rec['cover_repair_attempts']})"
                rec["cover_integrity_status"] = "INVALID_REPAIR_BUDGET_EXHAUSTED"
                rec["cover_repair_exhausted"] = True
                log(f"cover repair failed {rec['cover_repair_attempts']}x — left for human review (see {log_path.name})")
            else:
                rec["cover_integrity_status"] = "INVALID_REPAIR_PENDING"
        write_state(date, state)


def write_reports(date: str, state: dict) -> None:
    delivery = REPO_ROOT / "lidousha" / date
    delivery.mkdir(parents=True, exist_ok=True)

    def fmt_dur(pick: dict) -> str:
        secs = max(0, (pick.get("end_ms", 0) - pick.get("start_ms", 0)) // 1000)
        return f"{secs // 60}:{secs % 60:02d}"

    picks = state.get("picks", [])
    songs = state.get("songs", [])
    delivered_talk = sum(1 for p in picks if p.get("status") in DELIVERED_TALK_STATUSES)
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
            f"| `{safe_name(pick.get('hook',''), pick.get('candidate_id','?'))}`{status_mark} "
            f"| {fmt_dur(pick)} "
            f"| {pick.get('title') or '(未生成)'} "
            f"| {pick.get('hook') or '(兜底lane无理由)'} "
            f"| {pick.get('confidence') if pick.get('confidence') is not None else '—'} "
            f"| {s.get('closure_sentence') or '?'} "
            f"| {s.get('boundary_verdict') or '?'} "
            f"| {pick.get('cover_status') or s.get('cover_status') or '?'} |"
        )
    lines += ["", f"## 歌切（至多 {MAX_SONGS_PER_DATE} 个、按弹幕量排序；仅李豆沙本人演唱且完整才切；背景音乐/原曲播放/SONG_PARTIAL 均不交付；被拦不占配额、备份自动回填）", ""]
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
    if state.get("status") == "no_delivery":
        lines += ["", "> ⚠ 本场 0 条交付（候选被门拦截/失败/耗尽）。这不是成功状态，需人工过目落选与拦截原因。"]
    (delivery / "AUTOSLICE_SUMMARY.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    report = BASE / "reports" / "latest.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(
        f"# autoslice runner 最新状态\n\n- 时间: {time.strftime('%Y-%m-%d %H:%M:%S %z')}\n"
        f"- 日期: {date}  状态: {state.get('status')}\n"
        f"- 谈话: {delivered_talk} 交付(自修复 {repaired}, 不可修复 {unrepairable}) / {len(picks)} 尝试 (pending {len(state.get('pending_talk', []))})\n"
        f"- 歌切: {delivered_songs} 交付 / {blocked_songs} 门拦 / {len(songs)} 尝试 (pending {len(state.get('pending_song', []))})\n"
        f"- 交付: {REPO_ROOT}/lidousha/{date}/ (Mac launchd 拉取)\n",
        encoding="utf-8",
    )


def visual_song_config_from_env() -> VisualSongConfig:
    """Malformed optional tuning cannot disable the independent ASR lane."""

    try:
        sample_seconds = int(os.environ.get("AUTOSLICE_VISUAL_SONG_SAMPLE_SECONDS", "10"))
        timeout_seconds = int(os.environ.get("AUTOSLICE_VISUAL_SONG_TIMEOUT_SECONDS", "900"))
    except ValueError:
        sample_seconds, timeout_seconds = 10, 900
    return VisualSongConfig(
        sample_every_seconds=max(5, sample_seconds),
        timeout_seconds=max(60, timeout_seconds),
    )


def discover_segments(date: str, state: dict) -> None:
    """Phase A: transcribe + recall new segments into pending queues."""
    done = set(state.setdefault("segments_done", []))
    dead = state.setdefault("segments_dead", {})
    attempts = state.setdefault("bcut_attempts", {})
    pending_talk = state.setdefault("pending_talk", [])
    pending_song = state.setdefault("pending_song", [])
    visual_inventory = state.setdefault("visual_song_inventory", {})
    visual_seen = set(state.setdefault("visual_song_seen_entries", []))

    for segment in list_segments(date):
        stem = segment.stem
        if stem in done or stem in dead:
            continue
        try:
            size = segment.stat().st_size
        except OSError:
            continue
        if size < MIN_SEGMENT_BYTES:
            dead[stem] = f"stub_too_small({size}B)"
            log(f"segment {segment.name}: dead stub ({size}B), skipping forever")
            continue
        srt = bcut_transcribe(segment, date)
        if srt is None:
            attempts[stem] = attempts.get(stem, 0) + 1
            if attempts[stem] >= BCUT_MAX_ATTEMPTS:
                dead[stem] = f"bcut_failed_x{attempts[stem]}"
                log(f"segment {segment.name}: BCUT failed {attempts[stem]}x → dead")
            continue
        xml = find_danmaku_xml(segment)
        chat_jsonl = find_chat_jsonl(segment)
        candidates, lane, extras = recall_candidates(srt, danmaku_hints(xml))
        seg_dur = ffprobe_ms(segment)
        visual_result = discover_visual_songs(
            segment,
            BASE / "cache" / date / "visual-song-inventory",
            duration_ms=seg_dur,
            config=visual_song_config_from_env(),
        )
        visual_inventory[stem] = visual_result.to_manifest()
        fresh_visual = []
        for visual_candidate in visual_result.candidates:
            # The numbered overlay is cumulative across recording segments.
            # Deduplicate a stable numbered row across the date while still
            # allowing an unnumbered/repeated performance at another interval.
            list_index = visual_candidate.list_index
            identity = (
                f"list:{list_index}:{normalize_visual_title(visual_candidate.song_title)}"
                if list_index is not None
                else f"media:{stem}:{visual_candidate.start_ms}:{normalize_visual_title(visual_candidate.song_title)}"
            )
            if identity in visual_seen:
                continue
            visual_seen.add(identity)
            fresh_visual.append(visual_candidate)
        state["visual_song_seen_entries"] = sorted(visual_seen)
        if visual_result.status == "FAILED":
            log(f"{segment.name}: visual song inventory failed open ({visual_result.error})")
        else:
            log(
                f"{segment.name}: visual song inventory {len(fresh_visual)} new / "
                f"{len(visual_result.candidates)} visible ({'cache' if visual_result.cache_hit else 'AGY High'})"
            )
        log(f"{segment.name}: {len(candidates)} candidate(s) via {lane}")
        seg_tag = re.sub(r"\D", "", stem)[-6:]
        recalled_song_items: list[dict] = []
        for cand in candidates:
            meta = extras.get(cand.anchor.candidate_id, {})
            base_item = {
                "segment_path": str(segment),
                "seg_dur_ms": seg_dur,
                "xml": str(xml) if xml else None,
                "chat_jsonl": str(chat_jsonl) if chat_jsonl else None,
                "hook": meta.get("hook", ""),
                "confidence": meta.get("confidence"),
                "lane": lane,
                "preview": cand.text_preview[:80],
                "bcut_srt_path": str(srt),
            }
            if getattr(cand, "content_type_hint", "talk") == "song":
                a0, a1 = int(cand.anchor.anchor_start_ms), int(cand.anchor.anchor_end_ms)
                song_item = {
                    **base_item,
                    "cid": f"song_{seg_tag}_{a0 // 1000}",
                    "anchor_start_ms": a0,
                    "anchor_end_ms": a1,
                    "danmaku": danmaku_count_in(str(xml) if xml else None, a0, a1),
                }
                recalled_song_items.append(song_item)
            else:
                b = cand.boundary
                s0 = max(0, int(b.resolved_start_ms))
                s1 = min(seg_dur, int(b.resolved_end_ms)) if seg_dur else int(b.resolved_end_ms)
                pending_talk.append({
                    **base_item,
                    "cid": f"auto_{seg_tag}_{s0 // 1000}_{s1 // 1000}",
                    "start_ms": s0,
                    "end_ms": s1,
                })
        combined_song_items = union_visual_song_candidates(
            recalled_song_items,
            fresh_visual,
            segment_tag=seg_tag,
        )
        for song_item in combined_song_items:
            song_item.setdefault("segment_path", str(segment))
            song_item.setdefault("seg_dur_ms", seg_dur)
            song_item.setdefault("xml", str(xml) if xml else None)
            song_item.setdefault("chat_jsonl", str(chat_jsonl) if chat_jsonl else None)
            song_item.setdefault(
                "danmaku",
                danmaku_count_in(
                    str(xml) if xml else None,
                    int(song_item["anchor_start_ms"]),
                    int(song_item["anchor_end_ms"]),
                ),
            )
            pending_song.append(song_item)
            _remember_song_quarantine_interval(state, song_item)
        done.add(stem)
    state["segments_done"] = sorted(done)


def session_sealed(date: str, state: dict) -> bool:
    """The date's recordings are STABLE: same segment inventory (names+sizes)
    as the previous tick, with at least one segment.  Selecting before seal
    hands the early segments the whole quota (2026-07-09 audit: a conf=0.94
    late-arriving candidate lost to five earlier 0.85-0.90 ones).  Costs one
    extra tick (~10 min) of latency after stream end; also absorbs the
    recorder's final flush.  Mutates state['seg_snapshot'] for the next tick."""
    snapshot: dict[str, int] = {}
    for segment in list_segments(date):
        try:
            snapshot[segment.stem] = segment.stat().st_size
        except OSError:
            return False  # flaky source read — never seal on a lie
    prev = state.get("seg_snapshot")
    state["seg_snapshot"] = snapshot
    return bool(snapshot) and prev == snapshot


def song_delivery_budget(state: dict) -> int:
    """Remaining song DELIVERY slots, including verified commit reservations.

    An ordinary gate-BLOCKED attempt must not eat a slot (2026-07-09 audit:
    two BLOCKs consumed both slots and the date still read ``done``).  Once a
    song has passed the positive full-song/host proof and only its atomic
    packaging failed, however, that exact hash-bound attempt owns a slot until
    deterministic recovery either commits it or an operator revokes it.  This
    prevents two later songs from filling the quota and a delayed recovery
    silently exposing a third delivery.
    """

    consumed = sum(
        1
        for song in state.get("songs", [])
        if isinstance(song, dict)
        and (
            bool(song.get("delivered"))
            or song.get("verified_delivery_pending_commit") is True
        )
    )
    return max(0, MAX_SONGS_PER_DATE - consumed)


def _remember_song_quarantine_interval(state: dict, item: dict) -> None:
    """Persist source intervals that may contain a song before any rendering.

    Candidate-local BLOCK was insufficient: an overlapping semantic talk
    candidate could otherwise be produced first and launder background music,
    a guest song, or an unverified performance through the talk lane.  The
    interval taint survives song backlog moves, retries, and later state ticks.
    """

    segment = str(item.get("segment_path") or item.get("segment") or "").strip()
    anchor_start_ms = item.get("anchor_start_ms")
    anchor_end_ms = item.get("anchor_end_ms")
    if (
        not segment
        or isinstance(anchor_start_ms, bool)
        or not isinstance(anchor_start_ms, int)
        or isinstance(anchor_end_ms, bool)
        or not isinstance(anchor_end_ms, int)
        or not 0 <= anchor_start_ms < anchor_end_ms
    ):
        return
    # Quarantining only the recall anchor still lets a talk sibling escape with
    # the song's intro or tail.  Cover the same conservative source range that
    # the authoritative full-proof retry is allowed to inspect.
    start_ms = max(0, anchor_start_ms - SONG_PROOF_RETRY_PRE_MS)
    end_ms = anchor_end_ms + SONG_PROOF_RETRY_POST_MS
    segment_duration_ms = item.get("seg_dur_ms")
    if (
        isinstance(segment_duration_ms, int)
        and not isinstance(segment_duration_ms, bool)
        and segment_duration_ms > 0
    ):
        end_ms = min(segment_duration_ms, end_ms)
    interval = {
        "segment_path": segment,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "original_anchor_start_ms": anchor_start_ms,
        "original_anchor_end_ms": anchor_end_ms,
        "candidate_id": str(item.get("cid") or item.get("candidate_id") or ""),
        "reason_code": "SONG_INTERVAL_REQUIRES_JOINT_SINGING_PROOF",
    }
    intervals = state.setdefault("song_quarantine_intervals", [])
    identity = (Path(segment).name, anchor_start_ms, anchor_end_ms)
    if any(
        isinstance(existing, dict)
        and (
            Path(str(existing.get("segment_path") or "")).name,
            existing.get("original_anchor_start_ms", existing.get("start_ms")),
            existing.get("original_anchor_end_ms", existing.get("end_ms")),
        )
        == identity
        for existing in intervals
    ):
        return
    intervals.append(interval)


def _note_not_selected(state: dict, entry: str) -> None:
    """Record a not-selected line once; selection reruns every tick and must stay idempotent."""
    notes = state.setdefault("not_selected", [])
    if entry not in notes:
        notes.append(entry)


def quarantine_overlapping_talk_candidates(state: dict) -> None:
    """Remove every talk candidate overlapping a known song-like interval.

    Songs have their own proof-bearing lane.  A talk-shaped sibling, parent,
    child, merge, or retry may never materialize the same audio while the song
    interval is unresolved or blocked.  We deliberately keep the quarantine
    even after a valid song delivery: the same interval must not also escape as
    an ordinary talk artifact that bypasses the song gate.
    """

    for source in (state.get("pending_song", []), state.get("song_backlog", [])):
        for item in (source if isinstance(source, list) else []):
            if isinstance(item, dict):
                _remember_song_quarantine_interval(state, item)

    intervals = [item for item in state.get("song_quarantine_intervals", []) if isinstance(item, dict)]
    kept: list[dict] = []
    blocked = state.setdefault("song_overlap_blocked_talk", [])
    for talk in state.get("pending_talk", []):
        talk_segment = Path(str(talk.get("segment_path") or talk.get("segment") or "")).name
        talk_start = talk.get("start_ms")
        talk_end = talk.get("end_ms")
        overlap = next(
            (
                interval
                for interval in intervals
                if talk_segment
                and talk_segment == Path(str(interval.get("segment_path") or "")).name
                and isinstance(talk_start, int)
                and not isinstance(talk_start, bool)
                and isinstance(talk_end, int)
                and not isinstance(talk_end, bool)
                and isinstance(interval.get("start_ms"), int)
                and not isinstance(interval.get("start_ms"), bool)
                and isinstance(interval.get("end_ms"), int)
                and not isinstance(interval.get("end_ms"), bool)
                and max(talk_start, int(interval["start_ms"])) < min(talk_end, int(interval["end_ms"]))
            ),
            None,
        )
        if overlap is None:
            kept.append(talk)
            continue
        tombstone = {
            "candidate_id": str(talk.get("cid") or talk.get("candidate_id") or ""),
            "segment_path": str(talk.get("segment_path") or talk.get("segment") or ""),
            "start_ms": talk_start,
            "end_ms": talk_end,
            "status": "blocked",
            "reason_code": "TALK_OVERLAPS_UNVERIFIED_SONG_INTERVAL",
            "song_candidate_id": str(overlap.get("candidate_id") or ""),
            "song_start_ms": overlap.get("start_ms"),
            "song_end_ms": overlap.get("end_ms"),
        }
        if tombstone not in blocked:
            blocked.append(tombstone)
        _note_not_selected(
            state,
            f"{talk_segment} {int(talk_start or 0) // 1000}-{int(talk_end or 0) // 1000}s "
            "(门拦:与未验证/已阻断歌切区间重叠,不得走 talk 旁路)",
        )
    state["pending_talk"] = kept




def refill_songs(state: dict) -> None:
    """Top up pending_song from the structured backlog, danmaku-desc, honoring
    both the delivery budget and the hard per-date attempt cap.  Legacy string
    backlog entries (pre-v4 states) stay for the report but cannot backfill."""
    backlog = state.setdefault("song_backlog", [])
    pending = state.get("pending_song", [])
    selected_repairs = [item for item in pending if item.get("selected_repair")]
    pool = [item for item in pending if not item.get("selected_repair")] + [
        b for b in backlog if isinstance(b, dict)
    ]
    legacy = [b for b in backlog if not isinstance(b, dict)]
    pool.sort(key=lambda x: (-(x.get("danmaku") or 0), -(x["anchor_end_ms"] - x["anchor_start_ms"])))
    attempts_left_generation = max(0, SONG_ATTEMPT_CAP - len(state.get("songs", [])))
    lifetime_attempts = len(state.get("songs", [])) + len(state.get("song_superseded_attempts", []))
    attempts_left_lifetime = max(0, SONG_LIFETIME_ATTEMPT_CAP - lifetime_attempts)
    allowed = min(song_delivery_budget(state), attempts_left_generation, attempts_left_lifetime)
    # Infrastructure retries belong to already-selected songs.  A date-level
    # discovery/backfill cap must never discard them merely because sibling
    # attempts filled the historical tombstone budget.
    state["pending_song"] = selected_repairs + pool[:allowed]
    state["song_backlog"] = pool[allowed:] + legacy


def prioritize(state: dict) -> None:
    """Phase B: GLOBAL talk ranking by recall confidence (the metric asset is
    embedded in the recall prompt, so confidence carries its hard tiers), with
    a soft per-segment diversity cap that yields when slots would go unfilled.
    Replaces the segment round-robin that let five early candidates claim the
    whole quota regardless of score.  Songs: top danmaku, budget = deliveries."""
    # Preserve confidence-ranked reserve candidates so a deterministic
    # boundary/speaker rejection can automatically free its slot.  They used
    # to survive only as report strings, making top-5 mean "try exactly five
    # and accept fewer on any content-level refusal".
    prior_backlog = [
        item for item in state.pop("talk_backlog", []) if isinstance(item, dict)
    ]
    state.setdefault("pending_talk", []).extend(prior_backlog)
    quarantine_overlapping_talk_candidates(state)
    pending_talk = state.get("pending_talk", [])
    selected_repairs = [item for item in pending_talk if item.get("selected_repair")]
    pending_talk = [item for item in pending_talk if not item.get("selected_repair")]
    produced = sum(1 for p in state.get("picks", []) if p.get("status") in DELIVERED_TALK_STATUSES)
    # 对账铁律（Ivan 2026-07-13）：可恢复失败的原选手优先复活，其席位保留——
    # 候补不许趁基础设施故障上位（此前 failed 席被当空席，复活后一天超发 7 条）。
    # 重试额度耗尽的不再占席（否则永久卡死一席，整日欠交付）。
    reserved_for_revival = sum(
        1
        for p in state.get("picks", [])
        if p.get("status") == "failed"
        and p.get("failure_recoverable") is True
        and int(p.get("talk_transient_retry_count") or 0)
        + int(p.get("talk_repair_retry_count") or 0)
        < TALK_REPAIR_LIFETIME_RETRY_CAP
    )
    attempts_left = max(0, TALK_ATTEMPT_CAP - len(state.get("picks", [])))
    slots = min(max(0, MAX_TALK_PICKS - produced - reserved_for_revival), attempts_left)
    ranked = sorted(pending_talk, key=lambda x: -(x.get("confidence") or 0.0))
    keep: list[dict] = []
    deferred: list[dict] = []
    per_seg: dict[str, int] = {}
    for item in ranked:
        seg = item["segment_path"]
        if len(keep) < slots and per_seg.get(seg, 0) < TALK_PER_SEGMENT_CAP:
            keep.append(item)
            per_seg[seg] = per_seg.get(seg, 0) + 1
        else:
            deferred.append(item)
    # The diversity cap is SOFT: refill unused slots from the deferred list
    # (still confidence-ordered) rather than deliver fewer than `slots` picks.
    for item in list(deferred):
        if len(keep) >= slots:
            break
        keep.append(item)
        deferred.remove(item)
    # These candidates already won selection in an earlier generation and
    # failed without delivery.  Do not discard the retry merely because
    # successful siblings now fill the ordinary delivery quota.
    state["pending_talk"] = selected_repairs + keep
    state["talk_backlog"] = deferred
    for item in deferred:
        _note_not_selected(
            state,
            f"{Path(item['segment_path']).name} {item['start_ms'] // 1000}-{item['end_ms'] // 1000}s "
            f"conf={item.get('confidence')} hook={item.get('hook', '')[:40]} "
            f"(候补:全场按信心分全局排序取{MAX_TALK_PICKS}席,同段软上限{TALK_PER_SEGMENT_CAP})",
        )
    refill_songs(state)


def produce_batch(date: str, items: list[dict], produce_fn) -> list[dict]:
    """Produce ``items`` CONCURRENTLY (bounded by MAX_PARALLEL_PRODUCE), preserving
    input order.  Each slice is an independent subprocess (produce_slice_package /
    the song selector), so threads just wait on those; a crash in one becomes a
    failed result and never kills the batch.  ``produce_fn`` is produce_talk /
    produce_song, called as fn(date, item)."""
    from concurrent.futures import ThreadPoolExecutor

    def _one(item: dict) -> dict:
        try:
            return produce_fn(date, item)
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
    requeued_talks = requeue_recoverable_talks(date, state) if automatic_maintenance else 0
    requeued_songs = requeue_recoverable_songs(date, state) if automatic_maintenance else 0
    if requeued_talks or requeued_songs:
        write_state(date, state)
        log(
            f"{date}: requeued {requeued_talks} boundary talk failure(s) and "
            f"{requeued_songs} recoverable song BLOCK(s) for pipeline {pipeline_fingerprint()[:19]}…"
        )
    has_new = any(
        s.stem not in set(state.get("segments_done", [])) and s.stem not in state.get("segments_dead", {})
        for s in list_segments(date)
    )
    has_pending = bool(state.get("pending_talk") or state.get("pending_song"))
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
