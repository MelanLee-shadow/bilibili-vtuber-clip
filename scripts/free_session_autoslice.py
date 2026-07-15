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


def danmaku_hints(xml_path: Path | None) -> str | None:
    if xml_path is None:
        return None
    try:
        from src.autoslice.danmaku_evidence import find_danmaku_bursts, load_danmaku_xml

        items = load_danmaku_xml(xml_path)
        bursts = find_danmaku_bursts(items)
    except Exception:  # noqa: BLE001 — hints are optional enrichment
        return None
    lines = []
    for burst in bursts[:6]:
        sample = " / ".join(burst.sample_texts[:3])
        lines.append(f"{burst.start_ms // 60000:02d}:{burst.start_ms // 1000 % 60:02d} x{burst.count}: {sample}")
    return "弹幕突发时段(观众密集反应，强候选提示):\n" + "\n".join(lines) if lines else None


def danmaku_count_in(xml_path_str: str | None, start_ms: int, end_ms: int) -> int:
    if not xml_path_str:
        return 0
    try:
        from src.autoslice.danmaku_evidence import load_danmaku_xml

        items = load_danmaku_xml(Path(xml_path_str))
    except Exception:  # noqa: BLE001
        return 0
    return sum(1 for item in items if start_ms <= item.offset_ms < end_ms)


def slice_srt(src_srt: Path, start_ms: int, end_ms: int, dest: Path) -> int:
    """Cut [start_ms, end_ms) out of an SRT and rebase timestamps to 0."""
    from scripts.run_auto_review_shadow_pipeline import _parse_srt

    def ts(ms: int) -> str:
        ms = max(0, ms)
        return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"

    blocks = []
    for cue in _parse_srt(src_srt):
        if cue.source_end_ms <= start_ms or cue.source_start_ms >= end_ms:
            continue
        blocks.append(
            f"{len(blocks) + 1}\n{ts(cue.source_start_ms - start_ms)} --> {ts(cue.source_end_ms - start_ms)}\n{cue.text}"
        )
    dest.write_text("\n\n".join(blocks) + "\n" if blocks else "", encoding="utf-8")
    return len(blocks)


def safe_name(text: str, fallback: str) -> str:
    """Human-readable delivery filename from the recall hook."""
    cleaned = re.sub(r"[\\/:*?\"<>|\s]+", "", (text or "").strip())
    return cleaned[:18] if cleaned else fallback


def last_json_block(text: str) -> dict:
    """Parse the LAST balanced top-level JSON object in text (produce logs end
    with a summary object that contains nested objects — a non-greedy regex
    can't match it; this walks braces from the last closing brace backwards)."""
    end = text.rfind("}")
    while end != -1:
        depth = 0
        for start in range(end, -1, -1):
            ch = text[start]
            if ch == "}":
                depth += 1
            elif ch == "{":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[start : end + 1])
                        if isinstance(obj, dict):
                            return obj
                    except ValueError:
                        break
                    break
        end = text.rfind("}", 0, max(0, end))
    return {}


def recall_candidates(srt_path: Path, hints: str | None) -> tuple[list, str, dict]:
    """(candidates, lane, extras) — semantic lane first, deterministic fallback.

    extras maps candidate_id → {"hook": 选片理由, "confidence": 打分} so the
    review summary can show WHY each clip was picked (Ivan 2026-07-06).
    """
    from scripts.run_auto_review_shadow_pipeline import _parse_srt
    from src.autoslice.full_session_candidate_selector import (
        select_fallback_session_candidates,
        select_full_session_candidates,
    )
    from src.autoslice.llm_client import LlmCallError, LlmConfig, build_llm_call
    from src.autoslice.semantic_candidate_selector import select_semantic_session_candidates

    cues = _parse_srt(srt_path)
    if not cues:
        return [], "empty", {}
    llm = build_llm_call(
        LlmConfig(
            transport="command",
            # Talk semantic recall = deep open-ended lane → gpt-5.6-sol medium.
            command_template=f"bash {REPO_ROOT}/scripts/llm_via_cpa.sh {{prompt_file}} {{completion_file}} 'gpt-5.6-sol gpt-5.5 gpt-5.4' medium",
            timeout_seconds=600.0,
        )
    )
    try:
        candidates, diag = select_semantic_session_candidates(
            cues, llm_call=llm, max_candidates=PER_SEGMENT_CANDIDATES, danmaku_hints=hints
        )
        hooks = diag.get("hooks") or {}
        extras = {}
        for cand in candidates:
            cid = cand.anchor.candidate_id
            extras[cid] = {
                "hook": str(hooks.get(cid) or ""),
                "confidence": round(float(getattr(cand.boundary, "start_boundary_score", 0.5) or 0.5), 2),
            }
        # Deterministic song supplement: recall's candidate cap squeezes songs
        # out on song-heavy streams (first real run: 4+ songs sung, 1 caught).
        try:
            supplement = select_fallback_session_candidates(cues, max_candidates=8)
        except Exception:  # noqa: BLE001
            supplement = []
        for cand in supplement:
            if getattr(cand, "content_type_hint", "talk") != "song":
                continue
            if any(
                getattr(c, "content_type_hint", "talk") == "song"
                and not (cand.anchor.anchor_end_ms <= c.anchor.anchor_start_ms or cand.anchor.anchor_start_ms >= c.anchor.anchor_end_ms)
                for c in candidates
            ):
                continue  # overlaps a recall song → duplicate
            candidates.append(cand)
            extras[cand.anchor.candidate_id] = {"hook": "确定性歌检测补充(演唱段)", "confidence": 0.5}
        return candidates, "semantic_recall", extras
    except LlmCallError as exc:
        log(f"semantic recall failed ({exc}); falling back to deterministic lanes")
        primary = select_full_session_candidates(cues, max_candidates=PER_SEGMENT_CANDIDATES)
        fallback = select_fallback_session_candidates(cues, max_candidates=8)
        for candidate in fallback:
            if any(
                min(candidate.anchor.anchor_end_ms, existing.anchor.anchor_end_ms)
                > max(candidate.anchor.anchor_start_ms, existing.anchor.anchor_start_ms)
                for existing in primary
            ):
                continue
            primary.append(candidate)
        return primary, "deterministic_fallback", {}


def read_publish_meta(work_dir: Path) -> dict:
    for publish in sorted(work_dir.glob("replacement_recuts/*.publish.json")):
        try:
            d = json.loads(publish.read_text(encoding="utf-8"))
            hashes = d.get("artifact_hashes") if isinstance(d.get("artifact_hashes"), dict) else {}
            return {
                "title": d.get("title"),
                "title_source": d.get("title_source"),
                "title_authority_status": d.get("title_authority_status"),
                "title_authority_error": d.get("title_authority_error"),
                "cover_status": d.get("cover_status"),
                "cover_path": d.get("cover_path"),
                "cover_sha256": hashes.get("cover_sha256"),
                "cover_generation": d.get("cover_generation"),
                "video_sha256": hashes.get("burned_video_sha256") or hashes.get("video_sha256"),
            }
        except (OSError, ValueError):
            continue
    return {}


def _speaker_review_manifest_state(work_dir: Path) -> dict[str, tuple[int, int, int, int]]:
    state: dict[str, tuple[int, int, int, int]] = {}
    for path in work_dir.glob("replacement_recuts/*.speaker-final.json"):
        try:
            metadata = path.stat()
        except OSError:
            continue
        state[str(path)] = (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_mtime_ns,
            metadata.st_size,
        )
    return state


def read_speaker_review_meta(
    work_dir: Path,
    *,
    previous_state: dict[str, tuple[int, int, int, int]],
) -> dict:
    for manifest_path in sorted(work_dir.glob("replacement_recuts/*.speaker-final.json")):
        try:
            metadata = manifest_path.stat()
            current_state = (
                metadata.st_dev,
                metadata.st_ino,
                metadata.st_mtime_ns,
                metadata.st_size,
            )
            if previous_state.get(str(manifest_path)) == current_state:
                continue
            payload = manifest_path.read_bytes()
            after_read = manifest_path.stat()
            if current_state != (
                after_read.st_dev,
                after_read.st_ino,
                after_read.st_mtime_ns,
                after_read.st_size,
            ):
                continue
            document = json.loads(payload)
            rows = validate_speaker_review_manifest_document(document)
        except (OSError, ValueError, SpeakerFinalizationError):
            continue
        return {
            "speaker_review_manifest": str(manifest_path),
            "speaker_review_manifest_sha256": "sha256:"
            + hashlib.sha256(payload).hexdigest(),
            "speaker_review_source_media_sha256": document["source_media_sha256"],
            "speaker_review_text_final_srt_sha256": document["text_final_srt_sha256"],
            "speaker_review_context_unresolved_cues": document["context_unresolved_cues"],
            "speaker_review_reason": document["reason"],
            "speaker_review_required_cues": rows,
        }
    return {}


def _speaker_evidence_insufficient_failure(attempt_output: str) -> bool:
    """Recognize only deterministic identity-evidence shortage from finalizer.

    Other ``SPEAKER_FINALIZATION_BLOCKED`` errors include missing/drifted
    runtime assets and malformed manifests.  Those must remain retryable
    producer failures rather than being hidden as a rejected content pick.
    """

    return (
        "SPEAKER_FINALIZATION_BLOCKED" in attempt_output
        and "SpeakerFinalizationError: not enough Li Dousha clip anchors:"
        in attempt_output
    )


def classify_talk_failure(attempt_output: str) -> dict:
    """Persist a stable failure identity instead of a bare generic status."""

    tail = attempt_output[-8000:]
    nonempty = [line.strip() for line in tail.splitlines() if line.strip()]
    message = nonempty[-1][:1200] if nonempty else "producer exited without diagnostic"
    if "BOUNDARY_UNREPAIRABLE" in tail:
        kind, stage, recoverable = "content_boundary", "boundary_resolution", False
    elif "voiceprint_profile.v1.json" in tail and (
        "FileNotFoundError" in tail or "No such file" in tail
    ):
        kind, stage, recoverable = "runtime_prerequisite", "speaker_preflight", True
    elif "SPEAKER_REVIEW_REQUIRED" in tail:
        kind, stage, recoverable = "speaker_evidence", "speaker_finalization", False
    elif _speaker_evidence_insufficient_failure(tail):
        kind, stage, recoverable = "speaker_evidence", "speaker_finalization", False
    elif any(
        marker in tail.upper()
        for marker in ("TOO MANY REQUESTS", "INDIVIDUAL QUOTA REACHED", "TIMED OUT", "TIMEOUT")
    ):
        kind, stage, recoverable = "provider_transient", "external_provider", True
    else:
        kind, stage, recoverable = "producer_error", "unknown", True
    normalized = re.sub(r"/[^\s:'\"]+", "<path>", message)
    normalized = re.sub(r"\b\d{8,}\b", "<n>", normalized)
    fingerprint = hashlib.sha256(f"{kind}\0{stage}\0{normalized}".encode("utf-8")).hexdigest()
    return {
        "failure_kind": kind,
        "failure_stage": stage,
        "failure_message": message,
        "failure_fingerprint": "sha256:" + fingerprint,
        "failure_recoverable": recoverable,
    }


def produce_talk(date: str, item: dict, *, reuse_cover: bool = False) -> dict:
    """Run produce_slice_package for one pending talk item (plain-dict spec).

    Returns the result record; deterministic speaker uncertainty is preserved
    as ``speaker_review_required`` instead of a generic retryable failure.
    A title_failed pick is cleaned up (no delivery with a cid title/cover) and
    retried on a later resume.  ``reuse_cover`` keeps the existing delivered
    cover (subtitle-only re-run) and skips the ~90s AI cover step.
    """
    cid = item["cid"]
    out_root = BASE / "out" / date
    delivery_name = safe_name(item.get("hook", ""), cid)
    spec = {
        "candidate_id": cid,
        "date": date,
        "human_truth_mode": human_truth_mode(),
        "output_root": str(out_root),
        "delivery_name": delivery_name,
        "selection_hook": item.get("hook", ""),
        "given_title": None,
        "lead_pad_ms": 400,
        "semantic_start_ms": item["start_ms"],
        "semantic_end_ms": item["end_ms"],
        "boundary_repair_extend_cap_ms": BOUNDARY_REPAIR_INITIAL_CAP_MS,
        "pieces": [
            {
                "remote_media": item["segment_path"],
                "start_ms": max(0, item["start_ms"] - PIECE_PRE_MS),
                "end_ms": min(item["seg_dur_ms"], item["end_ms"] + PIECE_POST_MS) if item["seg_dur_ms"] else item["end_ms"] + PIECE_POST_MS,
                **({"danmaku_xml_local": item["xml"]} if item.get("xml") else {}),
                **({"chat_jsonl_local": item["chat_jsonl"]} if item.get("chat_jsonl") else {}),
            }
        ],
    }
    song_name_candidates = item.get("song_name_candidates")
    if song_name_candidates:
        # Machine-evidence song-name pool (screen songlist + 点歌 + known-songs)
        # for the deterministic pin — see src/autoslice/song_name_pin.py.
        spec["song_name_candidates"] = list(song_name_candidates)
    candidate_text_override = candidate_text_override_path(cid)
    if candidate_text_override is not None:
        spec["subtitle_text_overrides"] = str(candidate_text_override)
    candidate_subtitle_regression = candidate_subtitle_regression_path(cid)
    if candidate_subtitle_regression is not None:
        spec["subtitle_regression"] = str(candidate_subtitle_regression)
    candidate_speaker_override = candidate_speaker_override_path(cid)
    if candidate_speaker_override is not None:
        spec["speaker_overrides"] = str(candidate_speaker_override)
    for routing_key in (
        "speaker_routing_claim",
        "speaker_routing_claim_sha256",
        "speaker_routing_candidate",
    ):
        if item.get(routing_key) is not None:
            spec[routing_key] = item[routing_key]
    out_root.mkdir(parents=True, exist_ok=True)
    spec_path = out_root / f"spec_{cid}.json"
    spec_path.write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
    log_path = BASE / "logs" / f"{date}_{cid}.log"
    log(f"producing {cid} ({(item['end_ms'] - item['start_ms']) // 1000}s) from {Path(item['segment_path']).name}")
    cmd = [sys.executable, str(REPO_ROOT / "scripts" / "produce_slice_package.py"),
           "--spec", str(spec_path), "--ssh-host", "localhost", "--speaker-mode", SPEAKER_MODE]
    if reuse_cover:
        cmd.append("--reuse-cover")
    boundary_context_retries = 0

    def run_producer():
        speaker_review_state = _speaker_review_manifest_state(out_root / cid)
        attempt_offset = log_path.stat().st_size if log_path.is_file() else 0
        with open(log_path, "a", encoding="utf-8") as sink:
            completed = subprocess.run(
                cmd, check=False, stdout=sink, stderr=subprocess.STDOUT, timeout=5400,
                cwd=str(REPO_ROOT), env=child_env_for_date(date),
            )
        with open(log_path, "rb") as source:
            source.seek(attempt_offset)
            attempt_output = source.read().decode("utf-8", "replace")
        return completed, attempt_output, speaker_review_state

    completed, attempt_output, previous_speaker_review_state = run_producer()
    first_tail = attempt_output[-4000:]
    if (
        completed.returncode != 0
        and "BOUNDARY_UNREPAIRABLE" in first_tail
        and "retry_scope=same_topic_continues" in first_tail
    ):
        piece = spec["pieces"][0]
        retry_end = (
            min(item["seg_dur_ms"], item["end_ms"] + BOUNDARY_CONTEXT_RETRY_POST_MS)
            if item["seg_dur_ms"]
            else item["end_ms"] + BOUNDARY_CONTEXT_RETRY_POST_MS
        )
        current_cap = int(spec["boundary_repair_extend_cap_ms"])
        if retry_end > piece["end_ms"] and BOUNDARY_REPAIR_RETRY_CAP_MS > current_cap:
            boundary_context_retries = 1
            piece["end_ms"] = retry_end
            spec["boundary_repair_extend_cap_ms"] = BOUNDARY_REPAIR_RETRY_CAP_MS
            spec_path.write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
            with open(log_path, "a", encoding="utf-8") as sink:
                sink.write(
                    f"\nBOUNDARY_CONTEXT_RETRY: widening source post-context to {retry_end}ms "
                    f"and absolute repair cap to {BOUNDARY_REPAIR_RETRY_CAP_MS}ms "
                    f"(semantic end remains {item['end_ms']}ms)\n"
                )
            completed, attempt_output, previous_speaker_review_state = run_producer()
    result = {
        "candidate_id": cid, "segment": Path(item["segment_path"]).name,
        "start_ms": item["start_ms"], "end_ms": item["end_ms"],
        "hook": item.get("hook", ""), "confidence": item.get("confidence"),
        "lane": item.get("lane", ""), "rc": completed.returncode, "log": str(log_path),
        "boundary_context_retries": boundary_context_retries,
        "talk_repair_retry_count": int(item.get("talk_repair_retry_count") or 0),
        "talk_transient_retry_count": int(item.get("talk_transient_retry_count") or 0),
        "selected_repair": bool(item.get("selected_repair")),
        "retry_reason": item.get("retry_reason"),
        "pipeline_fingerprint": talk_pipeline_fingerprint(cid),
    }
    # Classify only bytes written by this subprocess attempt.  The log is
    # append-only; a stale boundary marker followed by a transient CPA error
    # must not make the new attempt terminal again.
    tail = attempt_output[-4000:]
    result["summary"] = last_json_block(tail)
    result.update(read_publish_meta(out_root / cid))
    if completed.returncode != 0:
        result.update(classify_talk_failure(attempt_output))
        result["failure_recovery_fingerprint"] = talk_failure_recovery_fingerprint(
            str(result["failure_kind"]), cid
        )
        if result["failure_recoverable"]:
            retry_epoch = int(time.time()) + SONG_INFRA_RETRY_BASE_SECONDS
            result["next_retry_at_epoch"] = retry_epoch
            result["next_retry_at"] = time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(retry_epoch)
            )
        if (
            "TITLE_AUTHORITY_UNRESOLVED" in tail
            and str(result.get("title_authority_status") or "").startswith("UNRESOLVED")
        ):
            # The producer now fails before delivery.  Keep cleanup for old
            # partial/stale attempts, then classify this deterministic lane so
            # the bounded title retry policy can act on it.
            delivered = REPO_ROOT / "lidousha" / date
            for f in delivered.glob(f"{delivery_name}.*"):
                f.unlink(missing_ok=True)
            recuts = out_root / cid / "replacement_recuts"
            if recuts.is_dir():
                import shutil

                shutil.rmtree(recuts, ignore_errors=True)
            result["status"] = "title_failed"
            return result
        if "SPEAKER_REVIEW_REQUIRED" in attempt_output:
            review_meta = read_speaker_review_meta(
                out_root / cid,
                previous_state=previous_speaker_review_state,
            )
            if review_meta:
                result.update(review_meta)
                result["status"] = "speaker_review_required"
                return result
        if _speaker_evidence_insufficient_failure(tail):
            result["status"] = "speaker_evidence_insufficient"
            return result
        # BOUNDARY_UNREPAIRABLE is deterministic for this pipeline generation;
        # a later fingerprint change can earn a bounded retry.
        result["status"] = (
            "boundary_unrepairable" if "BOUNDARY_UNREPAIRABLE" in tail else "failed"
        )
        return result
    if str(result.get("title_authority_status") or "").startswith("UNRESOLVED"):
        # No delivery with a cid title / cid-text cover — clean and retry later.
        delivered = REPO_ROOT / "lidousha" / date
        for f in delivered.glob(f"{delivery_name}.*"):
            f.unlink(missing_ok=True)
        recuts = out_root / cid / "replacement_recuts"
        if recuts.is_dir():
            import shutil

            shutil.rmtree(recuts, ignore_errors=True)
        result["status"] = "title_failed"
        return result
    # Boundary self-repair (Ivan 2026-07-10) replaced quarantine: a delivered
    # clip is clean by construction — red flags either got repaired (trail in
    # boundary_repairs) or the produce exited non-zero above (no delivery).
    summary = result.get("summary") or {}
    result["red_flags"] = list(summary.get("red_flags") or [])
    result["boundary_repairs"] = list(summary.get("boundary_repairs") or [])
    result["status"] = "review_ready"
    return result


_SRT_TS_RX = re.compile(
    r"(\d\d):(\d\d):(\d\d),(\d\d\d)\s*-->\s*(\d\d):(\d\d):(\d\d),(\d\d\d)"
)


def _srt_cue_spans(srt_path: Path, lo_ms: int, hi_ms: int) -> list[tuple[int, int]]:
    """(start_ms, end_ms) cue spans overlapping [lo,hi] from a whole-segment SRT."""
    try:
        text = srt_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    spans = []
    for m in _SRT_TS_RX.finditer(text):
        s = (int(m[1]) * 3600 + int(m[2]) * 60 + int(m[3])) * 1000 + int(m[4])
        e = (int(m[5]) * 3600 + int(m[6]) * 60 + int(m[7])) * 1000 + int(m[8])
        if e >= lo_ms and s <= hi_ms:
            spans.append((s, e))
    return spans


def _song_core_span(srt_path: Path, lo_ms: int, hi_ms: int,
                    *, min_gap_ms: int = 8_000, edge_frac: float = 0.35, min_span_ms: int = 90_000,
                    tail_gap_ms: int = 16_000, tail_frac: float = 0.12) -> tuple[int, int]:
    """Trim [lo,hi] to the sung core using ASR speech gaps.  A performance is set
    off from the surrounding chatter by a ≥8s music-intro gap at the front and a
    ≥16s outro gap at the back (《屑屑》: the recall anchor bloated BOTH ends — into
    the pig-nose talk before AND the '谢谢大家' talk after, so the window kept
    classifying as talk).  LEAD trim: song starts after the last big gap in the
    leading ``edge_frac``.  TAIL trim is DELIBERATELY conservative — only a LARGE
    (≥16s, bigger than a mid-song instrumental interlude) gap in the LAST
    ``tail_frac`` of the span counts, so the song's own late breaks are never
    clipped (clipping → SONG_PARTIAL, worse than carrying a little outro).  Returns
    (lo,hi) unchanged / partially-trimmed when a trim would be degenerate."""
    cues = _srt_cue_spans(srt_path, lo_ms, hi_ms)
    if len(cues) < 4 or hi_ms - lo_ms <= min_span_ms:
        return lo_ms, hi_ms
    span = hi_ms - lo_ms
    lead_cut = lo_ms + edge_frac * span
    lead = [(s1 - e0, s1) for (s0, e0), (s1, e1) in zip(cues, cues[1:]) if e0 <= lead_cut and (s1 - e0) >= min_gap_ms]
    new_lo = max(lead, key=lambda g: g[0])[1] if lead else lo_ms
    tail_cut = hi_ms - tail_frac * span
    tail = [e0 for (s0, e0), (s1, e1) in zip(cues, cues[1:]) if s1 >= tail_cut and (s1 - e0) >= tail_gap_ms]
    new_hi = min(tail) if tail else hi_ms
    if new_lo == lo_ms and new_hi == hi_ms:
        return lo_ms, hi_ms
    if new_hi - new_lo < min_span_ms:  # a trim would over-shorten → keep the generous span
        return lo_ms, hi_ms
    return int(new_lo), int(new_hi)


def song_status(rc: int, delivered: bool) -> str:
    """Honest song-lane status words (2026-07-09 audit): the selector exiting 0
    only means the PIPELINE ran.  blocked = not a song / performance incomplete
    / no materialized artifact — never 'ok'."""
    if rc != 0:
        return "failed"
    return "review_ready" if delivered else "blocked"


def song_proof_retry_window(anchor_start_ms: int, anchor_end_ms: int, segment_duration_ms: int) -> tuple[int, int]:
    """Expand a recall anchor against the original segment for LRC proof."""
    start_ms = max(0, anchor_start_ms - SONG_PROOF_RETRY_PRE_MS)
    end_ms = anchor_end_ms + SONG_PROOF_RETRY_POST_MS
    if segment_duration_ms:
        end_ms = min(segment_duration_ms, end_ms)
    return start_ms, end_ms


def song_delivery_ok(
    selector_rc: int,
    is_song: bool,
    reason_codes,
    completion_evidence: bool | dict | None,
) -> bool:
    """Ivan's FINAL song rule (2026-07-10): at most MAX_SONGS_PER_DATE per date,
    danmaku-desc; a song is delivered when the window IS a song and the
    performance is AFFIRMATIVELY PROVEN complete.  Absence of ``SONG_PARTIAL``
    is not evidence: the 2026-07-09 ``芽吹くとき`` run had no LRC proof
    and therefore never emitted that negative code, but was still incorrectly
    delivered.  Everything else the semantic judge flags (closure, viewer
    context, boundary style, AUTO_UPLOAD/BLOCK itself) is reviewer REFERENCE,
    not a delivery gate."""
    # A bare bool was the pre-host-vocal compatibility shortcut.  It can carry
    # no hash-bound performer identity and is therefore no longer acceptable.
    reasons = {str(code) for code in (reason_codes or [])}
    proof_path = completion_evidence.get("host_vocal_proof_path") if isinstance(completion_evidence, dict) else None
    proof_sha256 = completion_evidence.get("host_vocal_proof_sha256") if isinstance(completion_evidence, dict) else None
    proof_ready = (
        isinstance(completion_evidence, dict)
        and completion_evidence.get("ready") is True
        and completion_evidence.get("host_vocal_status") == "READY"
        and completion_evidence.get("host_vocal_decision") == "LIDOUSHA_VOCAL_PRESENT_ON_LYRIC_CHECKPOINTS"
        and completion_evidence.get("live_performance_status") == "READY"
        and completion_evidence.get("live_performance_mode") == "LIVE_STREAMER_SINGING"
        and completion_evidence.get("joint_singing_decision") == "VERIFIED_LIDOUSHA_SINGING"
        and isinstance(proof_path, str)
        and isinstance(proof_sha256, str)
        and _matches_sha256(Path(proof_path), proof_sha256)
    )
    identity_failure = (
        "SONG_NOT_LIDOUSHA_SINGING" in reasons
        or "SONG_BACKGROUND_PLAYBACK_ONLY" in reasons
        or "SONG_LIVE_PERFORMANCE_UNPROVEN" in reasons
        or any(
        code.startswith("SONG_HOST_VOCAL_") and code != "SONG_HOST_VOCAL_VERIFIED" for code in reasons
        )
    )
    return (
        selector_rc == 0
        and bool(is_song)
        and proof_ready
        and "SONG_PARTIAL" not in reasons
        and not identity_failure
    )


def fresh_song_selector_dir(out_dir: Path, tag: str) -> Path:
    """Create an empty, invocation-owned selector output directory.

    Selector attempts used to share ``song_selector{tag}``.  A failed process
    could therefore leave the runner reading a previous invocation's valid
    ``summary.json`` and artifacts.  Keep every attempt as non-destructive
    evidence under the stable tag directory, but give the current subprocess a
    new empty child so only files it writes can influence this attempt.
    """
    history_dir = out_dir / f"song_selector{tag}"
    history_dir.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="attempt-", dir=history_dir))


def song_window_media_path(
    out_dir: Path,
    candidate_id: str,
    tag: str,
    start_ms: int,
    end_ms: int,
) -> Path:
    """Return an interval-bound path for a materialized song proof window.

    The old fixed ``<candidate><tag>_source.mp4`` name let a later retry reuse
    bytes cut for a different interval.  The SRT and declared duration then
    described the new interval while AGY/CAM++ read the old media.  Bind the
    exact source interval into the filename so stale windows remain available
    for forensics but can never satisfy a different attempt.
    """

    lane = tag.removeprefix("_") or "tight"
    return out_dir / f"{candidate_id}_{lane}_{start_ms}_{end_ms}_source.mp4"


def classify_song_selector_transient(log_text: str) -> str | None:
    """Turn selector/provider diagnostics into a stable retry reason code."""

    upper = log_text.upper()
    if "TOO MANY REQUESTS" in upper or re.search(
        r"(?:HTTP(?: ERROR)?|STATUS(?: CODE)?|RESPONSE)\D{0,12}429\b", upper
    ):
        return "CPA_RATE_LIMITED"
    if "CPA_LLM_JUDGE_MODEL_DOWN" in upper:
        return "CPA_MODEL_DOWN"
    if re.search(r"HTTP(?: ERROR)?\s*(?:5\d\d|ERROR 5\d\d)", upper):
        return "CPA_UPSTREAM_5XX"
    if "TIMEOUT" in upper or "TIMED OUT" in upper:
        return "CPA_UPSTREAM_TIMEOUT"
    return None


def song_infra_retry_delay_seconds(completed_retry_count: int) -> int:
    """Exponential cross-tick backoff, capped so a provider outage stays bounded."""

    exponent = max(0, int(completed_retry_count))
    return min(SONG_INFRA_RETRY_MAX_SECONDS, SONG_INFRA_RETRY_BASE_SECONDS * (2**exponent))


def song_review_retry_after_seconds(summary_record: dict, selector_dir: Path) -> int | None:
    """Read a retry-after value only from this selector attempt's sidecar."""

    candidate_dir_raw = summary_record.get("candidate_dir")
    if not isinstance(candidate_dir_raw, str) or not candidate_dir_raw:
        return None
    try:
        candidate_dir = Path(candidate_dir_raw).resolve(strict=True)
        candidate_dir.relative_to(selector_dir.resolve(strict=True))
    except (OSError, ValueError):
        return None
    values: list[int] = []
    for marker in candidate_dir.glob("source_context/*.jingting.review-required.json"):
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        value = (data.get("metadata") or {}).get("retry_after_seconds")
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            values.append(value)
    return max(values) if values else None


def scheduled_song_retry_epoch(state: dict) -> int | None:
    """Earliest future infrastructure retry; its presence makes a date nonterminal."""

    epochs: list[int] = []
    for record in state.get("songs", []):
        if not isinstance(record, dict) or record.get("status") not in {"blocked", "failed"}:
            continue
        reasons = {str(code) for code in record.get("reason_codes") or []}
        if not reasons & SONG_INFRA_TRANSIENT_REASON_CODES:
            continue
        value = record.get("next_retry_at_epoch")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            epochs.append(int(value))
    return min(epochs) if epochs else None


def scheduled_talk_retry_epoch(state: dict) -> int | None:
    epochs: list[int] = []
    for record in state.get("picks", []):
        if not isinstance(record, dict) or record.get("failure_recoverable") is not True:
            continue
        value = record.get("next_retry_at_epoch")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            epochs.append(int(value))
    return min(epochs) if epochs else None


def scheduled_retry_epoch(state: dict) -> int | None:
    values = [scheduled_song_retry_epoch(state), scheduled_talk_retry_epoch(state)]
    return min(value for value in values if value is not None) if any(
        value is not None for value in values
    ) else None


def record_is_song(entry: dict) -> bool:
    """The window IS a song when the in-window recall classified it as one
    (semanticsong_* record id) OR the LRC lane pinned/aligned it.  Keep that
    upstream anchor through later selector retries; final delivery still needs
    independent positive boundary and lyric evidence and otherwise fails closed."""
    job = entry.get("source_context_job") or {}
    return (
        str(entry.get("candidate_id") or "").startswith("semanticsong")
        or job.get("content_type_hint") == "song"
        or job.get("song_candidate") is True
        or job.get("requires_full_source_song_boundary_redo") is True
        or bool(job.get("song_boundary"))
        or bool(job.get("lyrics_alignment"))
    )


def song_delivery_artifacts(record: dict) -> dict:
    """Best-known materialized artifacts for a song record, with sha256 hashes
    whenever the pipeline recorded them (hash hygiene stays; SEMANTIC gating
    does not — the delivery decision is song_delivery_ok).  A missing/blocked
    cover never blocks the video: covers are generated after the release gate
    now, and repair_covers backfills delivered clips."""
    recut = record.get("materialized_recut")
    if not isinstance(recut, dict):
        return {}
    out: dict = {}
    burned = recut.get("burned_preview")
    if isinstance(burned, dict) and burned.get("status") == "BURNED" and burned.get("path"):
        out["video_path"] = str(burned["path"])
        if burned.get("burned_sha256"):
            out["video_sha256"] = str(burned["burned_sha256"])
    recut_hashes = recut.get("artifact_hashes")
    if isinstance(recut_hashes, dict) and recut_hashes.get("burned_video_sha256"):
        out["video_sha256"] = str(recut_hashes["burned_video_sha256"])
    if recut.get("subtitle_path"):
        out["subtitle_path"] = str(recut["subtitle_path"])
        if isinstance(recut_hashes, dict) and recut_hashes.get("subtitle_sha256"):
            out["subtitle_sha256"] = str(recut_hashes["subtitle_sha256"])
    if recut.get("manifest_path"):
        out["recut_manifest_path"] = str(recut["manifest_path"])
        if recut.get("manifest_sha256"):
            out["recut_manifest_sha256"] = str(recut["manifest_sha256"])
    gate = recut.get("cover_release_gate")
    if isinstance(gate, dict):
        out["cover_release_gate_satisfied"] = gate.get("satisfied")
        out["release_gate_path"] = str(gate.get("path") or "")
        gate_hashes = gate.get("artifact_hashes")
        if isinstance(gate_hashes, dict) and gate_hashes.get("burned_video_sha256"):
            out["video_sha256"] = str(gate_hashes["burned_video_sha256"])
    staging = recut.get("publish_staging")
    if isinstance(staging, dict) and staging.get("status") == "STAGED":
        if staging.get("title") or record.get("title"):
            out["title"] = str(staging.get("title") or record.get("title") or "")
        if staging.get("cover_path"):
            out["cover_path"] = str(staging["cover_path"])
            recut_hashes = recut.get("artifact_hashes")
            if isinstance(recut_hashes, dict) and recut_hashes.get("cover_sha256"):
                out["cover_sha256"] = str(recut_hashes["cover_sha256"])
    return out


def _write_song_active_record(
    summary_record: dict,
    *,
    delivery_candidate_id: str,
    title: str,
    video_sha256: str,
    summary_authority_root: Path,
) -> tuple[Path, str]:
    """Persist the invocation-owned materialized song record next to its
    publish draft so later cover repair has the same exact active-document
    authority as talk delivery.  The delivery copy is included in the verified
    song manifest; this source copy remains in the immutable selector attempt."""

    if summary_authority_root.is_symlink():
        raise SongDeliveryError("song summary authority root may not be a symlink")
    try:
        authority_root = summary_authority_root.resolve(strict=True)
    except OSError as exc:
        raise SongDeliveryError(f"song summary authority root is missing: {exc}") from exc
    if not authority_root.is_dir():
        raise SongDeliveryError("song summary authority root is not a directory")

    materialized = summary_record.get("materialized_recut")
    if not isinstance(materialized, dict):
        raise SongDeliveryError("song summary has no materialized_recut record")
    record = copy.deepcopy(materialized)
    staging = record.get("publish_staging")
    if not isinstance(staging, dict):
        raise SongDeliveryError("song materialized record has no publish_staging")
    source_candidate_id = str(summary_record.get("candidate_id") or "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,96}", source_candidate_id):
        raise SongDeliveryError("song active record has an unsafe source candidate id")

    # A verified song can legitimately reach the runner while the generic
    # publish/cover release gate is still closed.  In particular,
    # SONG_FULL_BOUNDARY_READY is a positive song proof, but the generic gate
    # deliberately requires *zero* reason codes.  Do not weaken that gate or
    # pretend a cover exists.  Instead materialize the minimum no-upload
    # publish authority required by verified delivery and later cover repair.
    # This branch is intentionally narrow: any semantic/content blocker still
    # fails closed and cannot manufacture a publish draft.
    publish_value = staging.get("publish_json_path")
    if not isinstance(publish_value, str) or not publish_value:
        gate = record.get("cover_release_gate")
        gate_reasons = (
            [str(code) for code in gate.get("reason_codes", [])]
            if isinstance(gate, dict) and isinstance(gate.get("reason_codes"), list)
            else []
        )
        deferred_decision = str(staging.get("decision_action") or "")
        if (
            staging.get("status") != "SKIPPED_RELEASE_GATE"
            or deferred_decision not in {"AUTO_UPLOAD", "AUTO_RECUT"}
            or staging.get("upload_enabled") is not False
            or not isinstance(gate, dict)
            or gate.get("schema_version") != "slice-cover-release-gate.v1"
            or gate.get("candidate_id") != source_candidate_id
            or gate.get("decision_action") != deferred_decision
            or gate.get("satisfied") is not False
            or gate_reasons != ["SONG_FULL_BOUNDARY_READY"]
            or summary_record.get("decision_action") != deferred_decision
            or [str(code) for code in summary_record.get("reason_codes", [])]
            != ["SONG_FULL_BOUNDARY_READY"]
        ):
            raise SongDeliveryError(
                "song active publish draft missing outside the verified deferred-cover case"
            )
        media_value = record.get("media_path")
        manifest_value = record.get("manifest_path")
        burned = record.get("burned_preview")
        burned_value = burned.get("path") if isinstance(burned, dict) else None
        if not all(isinstance(value, str) and value for value in (media_value, manifest_value, burned_value)):
            raise SongDeliveryError("song deferred publish authority is missing materialized paths")
        try:
            media_path = Path(str(media_value)).resolve(strict=True)
            manifest_path = Path(str(manifest_value)).resolve(strict=True)
            burned_path = Path(str(burned_value)).resolve(strict=True)
        except OSError as exc:
            raise SongDeliveryError(f"song deferred publish materialized path is missing: {exc}") from exc
        artifact_root = manifest_path.parent
        if (
            media_path.parent != artifact_root
            or burned_path.parent != artifact_root
            or not artifact_root.is_relative_to(authority_root)
            or not manifest_path.name.endswith(".recut.manifest.json")
            or not media_path.name.endswith(".recut.mp4")
            or not burned_path.name.endswith(".mp4")
            or not _matches_sha256(burned_path, video_sha256)
        ):
            raise SongDeliveryError("song deferred publish artifact-root/video binding mismatch")
        publish_path = media_path.with_suffix(".publish.json")
        if publish_path.parent != artifact_root or publish_path.is_symlink():
            raise SongDeliveryError("song deferred publish path escapes the materialized artifact root")

        cover_text = title.removeprefix("【李豆沙】豆沙歌，").strip() or title
        cover_generation = {
            "workflow": "verified-song-delivery-deferred-cover.v1",
            "status": "BLOCKED",
            "detail": (
                "cover generation deferred until the hash-bound no-upload song "
                "delivery authority exists"
            ),
            "attempted_models": [],
        }
        artifact_hashes = copy.deepcopy(record.get("artifact_hashes"))
        if not isinstance(artifact_hashes, dict):
            raise SongDeliveryError("song deferred publish artifact_hashes is not an object")
        publish = {
            "schema_version": "shadow-publish-draft.v1",
            "candidate_id": source_candidate_id,
            "upload_enabled": False,
            "title": title,
            "title_source": "runner_verified_song_fallback",
            "title_policy_violations": [],
            "video_path": str(media_path),
            "cover_text": cover_text,
            "cover_path": None,
            "cover_status": "BLOCKED_AI_COVER_REQUIRED",
            "cover_generation": cover_generation,
            "reason_codes": ["CPA_AI_COVER_REQUIRED"],
            "artifact_hashes": artifact_hashes,
            "delivery_authority": {
                "schema_version": "verified-song-deferred-cover-authority.v1",
                "release_gate_path": str(gate.get("path") or staging.get("release_gate_path") or ""),
                "release_gate_satisfied": False,
                "release_gate_reason_codes": gate_reasons,
                "upload_enabled": False,
            },
        }
        if publish_path.exists():
            existing_publish = _read_json_object(
                publish_path, label="existing deferred song publish draft"
            )
            if existing_publish != publish:
                raise SongDeliveryError("conflicting deferred song publish draft already exists")
        else:
            _atomic_write_json_file(publish_path, publish)
        record["publish_staging"] = {
            "status": "STAGED",
            "title": title,
            "title_source": "runner_verified_song_fallback",
            "title_policy_violations": [],
            "cover_status": "BLOCKED_AI_COVER_REQUIRED",
            "cover_path": None,
            "cover_text": cover_text,
            "cover_generation": cover_generation,
            "reason_codes": ["CPA_AI_COVER_REQUIRED"],
            "publish_json_path": str(publish_path),
            "release_gate_path": str(gate.get("path") or staging.get("release_gate_path") or ""),
            "upload_enabled": False,
        }
        staging = record["publish_staging"]
        publish_value = str(publish_path)

    if (
        staging.get("title") != title
        or staging.get("upload_enabled") is not False
        or not isinstance(publish_value, str)
        or _document_video_hash(record) != video_sha256
    ):
        raise SongDeliveryError("song active record title/video/upload binding mismatch")
    publish_path = Path(publish_value)
    if publish_path.is_symlink():
        raise SongDeliveryError("song active publish draft may not be a symlink")
    try:
        publish_path = publish_path.resolve(strict=True)
    except OSError as exc:
        raise SongDeliveryError(f"song active publish draft is missing: {exc}") from exc
    if not publish_path.is_relative_to(authority_root):
        raise SongDeliveryError("song active publish draft escapes the bound summary attempt")
    publish = _read_json_object(publish_path, label="song active publish draft")
    if (
        publish.get("schema_version") != "shadow-publish-draft.v1"
        or publish.get("candidate_id") != source_candidate_id
        or publish.get("title") != title
        or publish.get("upload_enabled") is not False
        or _document_video_hash(publish) != video_sha256
    ):
        raise SongDeliveryError("song active publish candidate/title/video/upload binding mismatch")
    if publish_path.name.endswith(".recut.publish.json"):
        record_path = publish_path.with_name(
            publish_path.name[: -len(".recut.publish.json")] + ".record.json"
        )
    elif publish_path.name.endswith(".publish.json"):
        record_path = publish_path.with_name(
            publish_path.name[: -len(".publish.json")] + ".record.json"
        )
    else:
        raise SongDeliveryError("song active publish draft has an unexpected filename")
    if record_path.is_symlink() or not record_path.parent.resolve(strict=True).is_relative_to(
        authority_root
    ):
        raise SongDeliveryError("song active record escapes the bound summary attempt")
    record["delivery_candidate_id"] = delivery_candidate_id
    record["source_candidate_id"] = source_candidate_id
    _atomic_write_json_file(record_path, record)
    return record_path, "sha256:" + _sha256_regular_file(record_path)


def _commit_verified_song_package(
    *,
    date: str,
    delivery_candidate_id: str,
    summary_record: dict,
    title: str,
    selector_rc: int,
    summary_authority_root: Path,
) -> dict:
    """Commit one already-proven song without rerunning ASR/LRC/AGY.

    The same function is used by the fresh selector path and by bounded
    crash/packaging recovery.  It re-verifies every positive song proof and
    every artifact hash before exposing the manifest-last public package.
    Covers remain optional and upload is always disabled.
    """

    if selector_rc != 0:
        raise SongDeliveryError("song selector did not exit successfully")
    if not isinstance(title, str) or not title.strip():
        raise SongDeliveryError("verified song package has no title")
    title = title.strip()
    if summary_authority_root.is_symlink():
        raise SongDeliveryError("song summary authority root may not be a symlink")
    try:
        authority_root = summary_authority_root.resolve(strict=True)
        candidate_root = (BASE / "out" / date / delivery_candidate_id).resolve(strict=True)
    except OSError as exc:
        raise SongDeliveryError(f"song summary/candidate authority root is missing: {exc}") from exc
    if not authority_root.is_relative_to(candidate_root):
        raise SongDeliveryError("song summary authority root escapes the outer candidate")
    reasons = [str(code) for code in summary_record.get("reason_codes", [])]
    completion = song_completion_evidence(summary_record)
    if not song_delivery_ok(
        selector_rc,
        record_is_song(summary_record),
        reasons,
        completion,
    ):
        raise SongDeliveryError("song proof chain is not currently delivery-ready")

    artifacts = song_delivery_artifacts(summary_record)
    video_value = artifacts.get("video_path")
    video_sha256 = artifacts.get("video_sha256")
    if not isinstance(video_value, str) or not isinstance(video_sha256, str):
        raise SongDeliveryError("verified song has no hash-bound burned video")
    burned = Path(video_value)
    if not _matches_sha256(burned, video_sha256):
        raise SongDeliveryError("verified song burned video hash mismatch")

    required_sources = (
        (
            "subtitle",
            artifacts.get("subtitle_path"),
            artifacts.get("subtitle_sha256"),
            ".srt",
        ),
        (
            "lyrics_alignment_report",
            completion.get("alignment_report_path"),
            completion.get("alignment_report_sha256"),
            ".lyrics-alignment-report.json",
        ),
        (
            "host_vocal_proof",
            completion.get("host_vocal_proof_path"),
            completion.get("host_vocal_proof_sha256"),
            ".host-vocal-proof.json",
        ),
        (
            "recut_manifest",
            artifacts.get("recut_manifest_path"),
            artifacts.get("recut_manifest_sha256"),
            ".recut.manifest.json",
        ),
    )
    for role, source_value, sha_value, _suffix in required_sources:
        if not isinstance(source_value, str) or not isinstance(sha_value, str):
            raise SongDeliveryError(f"missing hash-bound delivery sidecar: {role}")
        if not _matches_sha256(Path(source_value), sha_value):
            raise SongDeliveryError(f"hash-bound delivery sidecar drifted: {role}")

    name = _song_delivery_basename(title, delivery_candidate_id)
    delivery = REPO_ROOT / "lidousha" / date
    delivery.mkdir(parents=True, exist_ok=True)
    specs: dict[str, tuple[Path, Path, str]] = {
        "video": (burned, delivery / f"{name}.mp4", video_sha256),
    }
    for role, source_value, sha_value, suffix in required_sources:
        specs[role] = (Path(str(source_value)), delivery / f"{name}{suffix}", str(sha_value))

    active_record_path, active_record_sha256 = _write_song_active_record(
        summary_record,
        delivery_candidate_id=delivery_candidate_id,
        title=title,
        video_sha256=video_sha256,
        summary_authority_root=authority_root,
    )
    specs["active_record"] = (
        active_record_path,
        delivery / f"{name}.record.json",
        active_record_sha256,
    )

    cover_ok = False
    cover: Path | None = None
    cover_value = artifacts.get("cover_path")
    cover_sha256 = artifacts.get("cover_sha256")
    if isinstance(cover_value, str) and isinstance(cover_sha256, str):
        cover = Path(cover_value)
        cover_ok = _matches_sha256(cover, cover_sha256)
        if cover_ok:
            specs["cover"] = (cover, delivery / f"{name}.cover.png", cover_sha256)

    receipt = _atomic_verified_song_delivery(
        candidate_id=delivery_candidate_id,
        manifest_path=delivery / f"{name}.delivery.manifest.json",
        artifact_specs=specs,
        absent_artifacts=(
            {} if cover_ok else {"cover": delivery / f"{name}.cover.png"}
        ),
    )
    delivered_artifacts = receipt["artifacts"]
    sidecar_roles = [role for role in delivered_artifacts if role != "video"]
    result = {
        "delivered": delivered_artifacts["video"]["path"],
        "delivered_sha256": delivered_artifacts["video"]["sha256"],
        "video_sha256": delivered_artifacts["video"]["sha256"],
        "delivered_sidecars": {
            role: delivered_artifacts[role]["path"] for role in sidecar_roles
        },
        "delivered_sidecar_hashes": {
            role: delivered_artifacts[role]["sha256"] for role in sidecar_roles
        },
        "delivery_manifest_path": receipt["manifest_path"],
        "delivery_manifest_sha256": receipt["manifest_sha256"],
        "delivery_upload_enabled": receipt["upload_enabled"],
        "cover_status": "AI_COVER_READY" if "cover" in delivered_artifacts else "BLOCKED_AI_COVER_REQUIRED",
    }
    if "cover" in delivered_artifacts:
        materialized = summary_record.get("materialized_recut")
        staging = materialized.get("publish_staging") if isinstance(materialized, dict) else None
        result["cover_path"] = delivered_artifacts["cover"]["path"]
        result["cover_sha256"] = delivered_artifacts["cover"]["sha256"]
        if isinstance(staging, dict):
            result["cover_generation"] = staging.get("cover_generation")
    if receipt["cleanup_warnings"]:
        result["delivery_cleanup_warnings"] = receipt["cleanup_warnings"]
    return result


VERIFIED_SONG_DELIVERY_SCHEMA_VERSION = "verified-song-delivery.v1"


class SongDeliveryError(RuntimeError):
    """A verified song package could not be committed to the delivery root."""


def _song_delivery_basename(title_or_hook: object, candidate_id: object) -> str:
    """Readable basename with an injective, runner-owned candidate suffix.

    The readable title prefix is deliberately short, so it cannot own
    uniqueness.  Song candidates are generated from the safe ASCII id grammar;
    retain that complete id in the public basename so concurrent songs with the
    same title can never replace one another's verified package.
    """

    candidate = str(candidate_id or "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,96}", candidate):
        raise SongDeliveryError(f"unsafe song delivery candidate id: {candidate!r}")
    readable = safe_name(f"歌切_{str(title_or_hook or '')}", "歌切")
    return f"{readable}__{candidate}"


def _sha256_regular_file(path: Path) -> str:
    """Hash one regular file without following a final-component symlink."""
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise SongDeliveryError(f"cannot open verified delivery artifact {path}: {exc}") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise SongDeliveryError(f"verified delivery artifact is not a regular file: {path}")
        digest = hashlib.sha256()
        with os.fdopen(descriptor, "rb") as source:
            descriptor = -1
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _hidden_delivery_path(destination: Path, label: str) -> Path:
    descriptor, name = tempfile.mkstemp(
        prefix=f".{destination.name}.{label}-",
        dir=destination.parent,
    )
    os.close(descriptor)
    path = Path(name)
    path.unlink()
    return path


def _stage_verified_copy(source: Path, destination: Path, expected_sha256: str) -> tuple[Path, str]:
    """Copy to a same-directory hidden temp and fsync it before returning."""
    normalized = _normalized_sha256(expected_sha256)
    if normalized is None:
        raise SongDeliveryError(f"invalid expected sha256 for {destination.name}")
    if source.is_symlink():
        raise SongDeliveryError(f"refusing symlink delivery source: {source}")

    descriptor, name = tempfile.mkstemp(
        prefix=f".{destination.name}.delivery-",
        suffix=".tmp",
        dir=destination.parent,
    )
    staged = Path(name)
    try:
        source_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        source_descriptor = os.open(source, source_flags)
        try:
            source_metadata = os.fstat(source_descriptor)
            if not stat.S_ISREG(source_metadata.st_mode):
                raise SongDeliveryError(f"delivery source is not a regular file: {source}")
            with os.fdopen(source_descriptor, "rb") as source_file, os.fdopen(descriptor, "wb") as target_file:
                source_descriptor = -1
                descriptor = -1
                shutil.copyfileobj(source_file, target_file, length=1024 * 1024)
                os.fchmod(target_file.fileno(), stat.S_IMODE(source_metadata.st_mode))
                target_file.flush()
                os.fsync(target_file.fileno())
        finally:
            if source_descriptor >= 0:
                os.close(source_descriptor)
        copied_sha256 = _sha256_regular_file(staged)
        if copied_sha256 != normalized:
            raise SongDeliveryError(
                f"copied hash mismatch for {destination.name}: expected {normalized}, got {copied_sha256}"
            )
        return staged, copied_sha256
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        staged.unlink(missing_ok=True)
        raise


def _stage_verified_bytes(payload: bytes, destination: Path) -> tuple[Path, str]:
    expected = hashlib.sha256(payload).hexdigest()
    descriptor, name = tempfile.mkstemp(
        prefix=f".{destination.name}.delivery-",
        suffix=".tmp",
        dir=destination.parent,
    )
    staged = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as target:
            descriptor = -1
            target.write(payload)
            target.flush()
            os.fsync(target.fileno())
        actual = _sha256_regular_file(staged)
        if actual != expected:
            raise SongDeliveryError(
                f"staged manifest hash mismatch for {destination.name}: expected {expected}, got {actual}"
            )
        return staged, actual
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        staged.unlink(missing_ok=True)
        raise


def _atomic_verified_song_delivery(
    *,
    candidate_id: str,
    manifest_path: Path,
    artifact_specs: dict[str, tuple[Path, Path, str]],
    absent_artifacts: dict[str, Path] | None = None,
) -> dict:
    """Atomically expose a hash-bound song package, committing its manifest last.

    All artifact bytes are copied and verified in hidden, same-directory files
    before any public delivery name is touched.  Existing files are held under
    hidden backup names during the short commit window so a caught replace or
    post-copy verification failure can restore the previous complete package.
    The manifest is installed last and is the durable commit marker; it is
    explicitly no-upload and its own hash is returned for state binding.
    """
    if not candidate_id or "video" not in artifact_specs:
        raise SongDeliveryError("verified song delivery requires candidate_id and video")
    absent_artifacts = absent_artifacts or {}
    parent = manifest_path.parent
    parent.mkdir(parents=True, exist_ok=True)
    parent_real = parent.resolve(strict=True)
    if manifest_path.parent.resolve(strict=True) != parent_real:
        raise SongDeliveryError("delivery manifest escaped its parent")

    staged: dict[str, tuple[Path, Path, str]] = {}
    manifest_artifacts: dict[str, dict[str, str]] = {}
    destinations: set[Path] = set()
    try:
        for role, spec in artifact_specs.items():
            if not isinstance(role, str) or not role or not isinstance(spec, tuple) or len(spec) != 3:
                raise SongDeliveryError("invalid verified delivery artifact specification")
            source, destination, expected_value = spec
            if not isinstance(source, Path) or not isinstance(destination, Path):
                raise SongDeliveryError(f"invalid paths for verified delivery artifact {role}")
            if destination.parent.resolve(strict=True) != parent_real or destination.name.startswith("."):
                raise SongDeliveryError(f"delivery destination escaped or is hidden: {destination}")
            if destination in destinations or destination == manifest_path:
                raise SongDeliveryError(f"duplicate delivery destination: {destination}")
            destinations.add(destination)
            normalized = _normalized_sha256(expected_value)
            if normalized is None:
                raise SongDeliveryError(f"invalid expected sha256 for {role}")
            source_real = source.resolve(strict=True)
            temp_path, copied_sha = _stage_verified_copy(source, destination, normalized)
            staged[role] = (temp_path, destination, copied_sha)
            manifest_artifacts[role] = {
                "path": str(destination.absolute()),
                "sha256": f"sha256:{copied_sha}",
                "source_path": str(source_real),
                "source_sha256": f"sha256:{normalized}",
            }

        manifest_absent: dict[str, dict[str, str]] = {}
        for role, destination in absent_artifacts.items():
            if (
                not isinstance(role, str)
                or not role
                or role in artifact_specs
                or not isinstance(destination, Path)
                or destination.parent.resolve(strict=True) != parent_real
                or destination.name.startswith(".")
                or destination in destinations
                or destination == manifest_path
            ):
                raise SongDeliveryError(f"invalid absent delivery artifact specification: {role}")
            destinations.add(destination)
            manifest_absent[role] = {"path": str(destination.absolute()), "status": "ABSENT"}

        manifest_payload = {
            "schema_version": VERIFIED_SONG_DELIVERY_SCHEMA_VERSION,
            "status": "DELIVERED_NO_UPLOAD",
            "candidate_id": candidate_id,
            "upload_enabled": False,
            "artifacts": manifest_artifacts,
            "absent_artifacts": manifest_absent,
        }
        manifest_bytes = (
            json.dumps(manifest_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        manifest_temp, manifest_sha = _stage_verified_bytes(manifest_bytes, manifest_path)
        staged["__manifest__"] = (manifest_temp, manifest_path, manifest_sha)
    except BaseException:
        for temp_path, _destination, _digest in staged.values():
            temp_path.unlink(missing_ok=True)
        raise

    # Sidecars first, then the video, with the manifest last as the commit marker.
    install_order = sorted(role for role in artifact_specs if role != "video") + ["video", "__manifest__"]
    backup_order = [staged[role][1] for role in install_order] + list(absent_artifacts.values())
    backups: dict[Path, Path] = {}
    installed: set[Path] = set()
    try:
        for destination in backup_order:
            if os.path.lexists(destination):
                if destination.is_dir() and not destination.is_symlink():
                    raise SongDeliveryError(f"delivery destination is a directory: {destination}")
                backup = _hidden_delivery_path(destination, "previous")
                backups[destination] = backup
                os.replace(destination, backup)
        if backups:
            _fsync_directory(parent)

        for role in install_order:
            temp_path, destination, expected = staged[role]
            if role == "__manifest__":
                # Seal only after every public artifact still matches the
                # selector/materialization hash at the commit boundary.
                for artifact_role in artifact_specs:
                    _artifact_temp, artifact_destination, artifact_expected = staged[artifact_role]
                    actual = _sha256_regular_file(artifact_destination)
                    if actual != artifact_expected:
                        raise SongDeliveryError(
                            f"pre-manifest delivery hash mismatch for {artifact_destination.name}: "
                            f"expected {artifact_expected}, got {actual}"
                        )
            installed.add(destination)
            os.replace(temp_path, destination)
            _fsync_directory(parent)
            actual = _sha256_regular_file(destination)
            if actual != expected:
                raise SongDeliveryError(
                    f"final delivery hash mismatch for {destination.name}: expected {expected}, got {actual}"
                )
    except BaseException as exc:
        rollback_errors: list[str] = []
        for destination in reversed(backup_order):
            if destination in installed:
                try:
                    destination.unlink(missing_ok=True)
                except OSError as rollback_exc:
                    rollback_errors.append(f"remove {destination}: {rollback_exc}")
            backup = backups.get(destination)
            if backup is not None and os.path.lexists(backup):
                try:
                    os.replace(backup, destination)
                except OSError as rollback_exc:
                    rollback_errors.append(f"restore {destination}: {rollback_exc}")
        for temp_path, _destination, _digest in staged.values():
            try:
                temp_path.unlink(missing_ok=True)
            except OSError as rollback_exc:
                rollback_errors.append(f"remove temp {temp_path}: {rollback_exc}")
        try:
            _fsync_directory(parent)
        except OSError as rollback_exc:
            rollback_errors.append(f"fsync {parent}: {rollback_exc}")
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        detail = f"; rollback errors: {'; '.join(rollback_errors)}" if rollback_errors else ""
        raise SongDeliveryError(f"atomic verified delivery failed: {exc}{detail}") from exc

    cleanup_warnings: list[str] = []
    for backup in backups.values():
        try:
            backup.unlink(missing_ok=True)
        except OSError as exc:
            cleanup_warnings.append(f"remove superseded backup {backup}: {exc}")
    if backups:
        try:
            _fsync_directory(parent)
        except OSError as exc:
            cleanup_warnings.append(f"fsync superseded backup cleanup: {exc}")
    return {
        "manifest_path": str(manifest_path.resolve(strict=True)),
        "manifest_sha256": f"sha256:{manifest_sha}",
        "artifacts": manifest_artifacts,
        "upload_enabled": False,
        "cleanup_warnings": cleanup_warnings,
    }


def produce_song(date: str, item: dict) -> dict:
    """Song lane (Ivan 2026-07-05): cut a tight window around the sung anchor,
    run the canonical song pipeline (NetEase/LRCLIB global-shift alignment + strict
    completeness gate, fail-closed) and deliver only gate-passing results.

    Anchor-bleed guard (Ivan 2026-07-06): a recall song-anchor can begin dozens
    of seconds inside the PRECEDING talk (《今天也过得很愉快》's anchor led with the
    pig-nose banter), so the window leads with talk and the in-window recall
    latches onto that talk (window_classified_song=False → AUTO_RECUT, no song
    delivered).  When the first attempt misses the song, retry ONCE on the
    danmaku-dense core of the anchor.  Self-correcting: it only fires when the
    song was missed, so a clean song window (嘉宾) runs exactly once as before."""
    seg_dur_ms = item["seg_dur_ms"]
    segment = Path(item["segment_path"])
    cid = item["cid"]
    out_dir = BASE / "out" / date / cid
    out_dir.mkdir(parents=True, exist_ok=True)

    def window_for(a0: int, a1: int) -> tuple[int, int]:
        s = max(0, a0 - SONG_WINDOW_PRE_MS)
        e = min(seg_dur_ms, a1 + SONG_WINDOW_POST_MS) if seg_dur_ms else a1 + SONG_WINDOW_POST_MS
        return s, e

    def attempt(start: int, end: int, tag: str) -> dict:
        log(f"song lane {cid}{tag}: window {start // 1000}-{end // 1000}s (danmaku x{item.get('danmaku', 0)}) from {segment.name}")
        result = {"candidate_id": cid, "segment": segment.name, "start_ms": start, "end_ms": end,
                  "danmaku": item.get("danmaku", 0), "hook": item.get("hook", ""), "preview": item.get("preview", "")[:60], "rc": -1,
                  "discovery_lane": item.get("lane"), "title_hint": item.get("title_hint"),
                  "visual_song_evidence": item.get("visual_song_evidence"),
                  "pipeline_fingerprint": pipeline_fingerprint(),
                  "transient_retry_count": int(item.get("transient_retry_count") or 0),
                  "anchor_start_ms": item.get("anchor_start_ms"),
                  "anchor_end_ms": item.get("anchor_end_ms")}
        window_mp4 = song_window_media_path(out_dir, cid, tag, start, end)
        if not window_mp4.is_file():
            cut = subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                 "-ss", f"{start / 1000:.3f}", "-to", f"{end / 1000:.3f}", "-i", str(segment),
                 "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-c:a", "aac", "-b:a", "192k",
                 str(window_mp4)],
                check=False, capture_output=True, text=True, timeout=3600,
            )
            if cut.returncode != 0 or not window_mp4.is_file():
                result["error"] = f"window cut failed: {cut.stderr[-200:]}"
                result["status"] = "failed"
                return result

        src_srt = BASE / "cache" / date / f"{segment.stem}.bcut.srt"
        window_srt = out_dir / f"{cid}{tag}_source.srt"
        if slice_srt(src_srt, start, end, window_srt) == 0:
            result["error"] = "empty window srt"
            result["status"] = "failed"
            return result

        # NOTE: segment danmaku XML is segment-relative — do not pass it to the
        # selector (it would misalign against the window-relative video); LRC is
        # the subtitle authority for songs anyway.
        selector_dir = fresh_song_selector_dir(out_dir, tag)
        log_path = BASE / "logs" / f"{date}_{cid}.log"
        try:
            log_offset = log_path.stat().st_size
        except OSError:
            log_offset = 0
        with open(log_path, "a", encoding="utf-8") as sink:
            selector_env = song_selector_env(date)
            selector_command = [
                 sys.executable, str(REPO_ROOT / "scripts" / "run_full_session_selector_cpa_shadow.py"),
                 "--source-video", str(window_mp4), "--source-srt", str(window_srt),
                 "--output-dir", str(selector_dir), "--max-candidates", "1",
                 "--source-duration-ms", str(max(0, end - start)),
                 "--cpa-command", cpa_qa_cmd(),
                 "--semantic-recall-llm-command", CPA_CMD_DEEP,
                 "--song-hint-llm-command", CPA_CMD_STANDARD,
                 "--title-llm-command", CPA_CMD_TITLE,
                 "--cover-art-direction-llm-command", CPA_CMD_STRUCTURED,
                 "--lrc-provider", "auto", "--burn-preview", "--publish-staging",
                 "--branding-intro-manifest", str(REPO_ROOT / "assets" / "lidousha" / "intro" / "branding_intro.v1.json"),
                 "--host-vocal-python", str(HOST_VOCAL_PYTHON),
                 "--host-vocal-reference-profile", str(HOST_VOCAL_PROFILE),
                 "--host-vocal-reference-dir", str(HOST_VOCAL_REFERENCE_DIR),
                 "--host-vocal-model-dir", str(HOST_VOCAL_MODEL_DIR),
            ]
            semantic_hook = str(item.get("hook") or "").strip()
            known_song_query = str(item.get("preview") or "").strip()
            visual_title_hint = str(item.get("title_hint") or "").strip()
            # Search the quoted song title first.  Passing the entire prose
            # preview ("下播前演唱 yonige《芽吹くとき》") made LRCLIB return no
            # rows even though the exact title returns the canonical timed LRC.
            quoted_titles = re.findall(
                r"[《「『]([^》」』]{1,80})[》」』]",
                "\n".join([semantic_hook, known_song_query]),
            )
            # The semantic selector already named many songs correctly even
            # when singing ASR was unusable.  Query that title before the ASR
            # preview; LRC/audio proof still decides whether it is truly the
            # performed song, so this is recall improvement rather than trust.
            for query in dict.fromkeys([visual_title_hint, *quoted_titles[:2], known_song_query]):
                if query:
                    selector_command.extend(["--song-lrc-query", query])
            # Every invocation already came from the upstream song lane, which
            # owns this anchor.  Re-asking a nondeterministic semantic LLM to
            # decide whether the same window is a song made Japanese garbage
            # ASR randomly produce NO_FULL_SESSION_CANDIDATES.  Keep the anchor
            # for tight/core/full attempts; only the full attempt may escalate
            # to expensive audio+LRC proof.  Delivery still requires the
            # independent positive full-song proof below.
            selector_command.extend(
                [
                    "--seed-song-candidate-id",
                    f"seededsong_{max(0, anchor_start - start)}_{min(end - start, anchor_end - start)}",
                    "--seed-song-anchor-start-ms",
                    str(max(0, anchor_start - start)),
                    "--seed-song-anchor-end-ms",
                    str(min(end - start, anchor_end - start)),
                ]
            )
            if tag == "_full":
                selector_command.append("--agy-audio-lrc-align")
            completed = subprocess.run(
                selector_command,
                check=False, stdout=sink, stderr=subprocess.STDOUT, timeout=5400,
                cwd=str(REPO_ROOT), env=selector_env,
            )
        try:
            with log_path.open("r", encoding="utf-8", errors="replace") as source:
                source.seek(log_offset)
                invocation_log = source.read()
        except OSError:
            invocation_log = ""
        transient_code = classify_song_selector_transient(invocation_log)
        result["rc"] = completed.returncode
        result["log"] = str(log_path)
        decision = None
        reasons: list = []
        is_song = False
        summary_record: dict = {}
        summary_path = selector_dir / "summary.json"
        if summary_path.is_file():
            try:
                summary = json.loads(summary_path.read_text(encoding="utf-8"))
                for entry in summary.get("records", []) if isinstance(summary, dict) else []:
                    if not isinstance(entry, dict):
                        continue
                    summary_record = entry
                    decision = entry.get("decision_action") or decision
                    reasons = list(entry.get("reason_codes") or reasons)
                    is_song = is_song or record_is_song(entry)
            except ValueError:
                pass
        if summary_record:
            try:
                result["selector_summary_path"] = str(summary_path.resolve(strict=True))
                result["selector_summary_sha256"] = "sha256:" + _sha256_regular_file(summary_path)
                result["selector_record_candidate_id"] = str(
                    summary_record.get("candidate_id") or ""
                )
            except (OSError, SongDeliveryError):
                # Missing summary authority only disables zero-compute
                # packaging recovery.  The current invocation can still use
                # its in-memory record and the independently hash-bound proof
                # chain below.
                pass
        result["decision"] = decision
        completion = song_completion_evidence(summary_record)
        reason_set = {str(code) for code in reasons}
        specific_agy_transient = next(
            (
                code
                for code in (
                    "AGY_QUOTA_EXHAUSTED",
                    "AGY_TIMEOUT",
                    "AGY_EMPTY_OUTPUT",
                    "AGY_FAILED_RC",
                    "AGY_AND_GEMINI_API_FAILED",
                )
                if code in reason_set
            ),
            None,
        )
        if specific_agy_transient is not None:
            transient_code = specific_agy_transient
        elif transient_code is None and "AGY_SOURCE_CONTEXT_RUNNER_FAILED" in reason_set:
            transient_code = "AGY_SOURCE_CONTEXT_RUNNER_FAILED"
        result["reason_codes"] = list(
            dict.fromkeys(
                [*reasons, *completion["reason_codes"], *([transient_code] if transient_code else [])]
            )
        )
        if transient_code:
            retry_count = int(result.get("transient_retry_count") or 0)
            provider_retry_after = song_review_retry_after_seconds(summary_record, selector_dir)
            retry_delay = max(
                song_infra_retry_delay_seconds(retry_count),
                (provider_retry_after + 60) if provider_retry_after is not None else 0,
            )
            next_retry_epoch = int(time.time()) + retry_delay
            result["transient_failure_code"] = transient_code
            result["retry_after_seconds"] = retry_delay
            result["next_retry_at_epoch"] = next_retry_epoch
            result["next_retry_at"] = time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(next_retry_epoch)
            )
        result["window_classified_song"] = is_song
        result["song_completion_evidence"] = completion
        artifacts = song_delivery_artifacts(summary_record)
        if artifacts.get("title"):
            result["title"] = artifacts["title"]
        if not result.get("title"):  # pre-gate-era summaries carry no staging title
            for publish in sorted(selector_dir.glob("**/replacement_recuts/*.publish.json")):
                try:
                    result["title"] = json.loads(publish.read_text(encoding="utf-8")).get("title")
                except (OSError, ValueError):
                    pass
        # Burned video: only the summary-recorded, hash-bound artifact is
        # eligible.  Old selector debris must never inherit a newer proof.
        burned = Path(artifacts["video_path"]) if artifacts.get("video_path") else None
        if burned is not None and (
            not isinstance(artifacts.get("video_sha256"), str)
            or not _matches_sha256(burned, artifacts["video_sha256"])
        ):
            log(f"song lane {cid}: burned video hash missing/drifted since materialization — refusing stale artifact")
            burned = None
        result["song_complete"] = completion["ready"] is True
        result["lyrics_alignment_ready"] = completion["lyrics_alignment_status"] == "READY"
        if result["song_complete"] and not result.get("title"):
            boundary = (summary_record.get("source_context_job") or {}).get("song_boundary") or {}
            result["title"] = verified_song_fallback_title(boundary.get("song_title"), item.get("hook"))
        if "cover_release_gate_satisfied" in artifacts:
            result["cover_release_gate_satisfied"] = artifacts["cover_release_gate_satisfied"]
        if burned is not None and burned.is_file() and song_delivery_ok(
            completed.returncode, is_song, reasons, completion
        ):
            try:
                delivery_update = _commit_verified_song_package(
                    date=date,
                    delivery_candidate_id=cid,
                    summary_record=summary_record,
                    title=str(result.get("title") or item.get("hook") or ""),
                    selector_rc=completed.returncode,
                    summary_authority_root=summary_path.parent,
                )
            except (OSError, SongDeliveryError, ValueError) as exc:
                log(
                    f"song lane {cid}: atomic verified delivery refused: "
                    f"{type(exc).__name__}: {exc}"
                )
                result["delivery_error"] = f"{type(exc).__name__}: {exc}"
                result["reason_codes"] = list(
                    dict.fromkeys([*(result.get("reason_codes") or []), "SONG_DELIVERY_ATOMIC_COPY_FAILED"])
                )
                # Positive song/host proof has already passed.  Reserve this
                # delivery slot so later songs cannot fill the quota before
                # the exact hash-bound package is deterministically recovered.
                recovery_authority = _song_delivery_recovery_authority(
                    date=date,
                    outer_candidate_id=cid,
                    summary_path=result.get("selector_summary_path"),
                    summary_sha256=result.get("selector_summary_sha256"),
                    source_candidate_id=result.get("selector_record_candidate_id"),
                    title=result.get("title"),
                )
                if recovery_authority is not None:
                    result["verified_delivery_pending_commit"] = True
                    result["song_delivery_recovery_authority"] = recovery_authority
                else:
                    # A reservation without its complete state envelope can
                    # neither recover nor requeue, permanently consuming a
                    # delivery slot.  Keep capacity open and allow one bounded
                    # fresh attempt to recapture the missing authority.
                    result["reason_codes"] = list(
                        dict.fromkeys(
                            [
                                *(result.get("reason_codes") or []),
                                "SONG_DELIVERY_RECOVERY_AUTHORITY_MISSING",
                            ]
                        )
                    )
            else:
                result.update(delivery_update)
        result["status"] = song_status(completed.returncode, bool(result.get("delivered")))
        return result

    anchor_start, anchor_end = item["anchor_start_ms"], item["anchor_end_ms"]
    src_srt = BASE / "cache" / date / f"{segment.stem}.bcut.srt"
    # A previous authoritative full-source pass that failed only because its
    # AGY runner timed out already proved that the tight window is a song but
    # lacks complete boundary evidence.  Cross-tick infrastructure recovery
    # should resume that expensive stage directly instead of spending another
    # model call rediscovering the same incomplete tight result.
    if item.get("resume_full_source") is True:
        full_start, full_end = song_proof_retry_window(anchor_start, anchor_end, seg_dur_ms)
        resumed = attempt(full_start, full_end, "_full")
        resumed["retried_full_source"] = True
        resumed["resumed_full_source_after_transient"] = True
        return resumed

    result = attempt(*window_for(anchor_start, anchor_end), "")
    if result.get("window_classified_song") and not result.get("song_complete"):
        full_start, full_end = song_proof_retry_window(anchor_start, anchor_end, seg_dur_ms)
        if full_start < result.get("start_ms", full_start) or full_end > result.get("end_ms", full_end):
            log(
                f"song lane {cid}: song identified but positive LRC boundary proof is missing — "
                f"retrying with original-source context {full_start // 1000}-{full_end // 1000}s"
            )
            proof_retry = attempt(full_start, full_end, "_full")
            if proof_retry.get("song_complete"):
                result = {**proof_retry, "retried_full_source": True}
            else:
                result["full_source_retry"] = {
                    key: proof_retry.get(key)
                    for key in (
                        "start_ms",
                        "end_ms",
                        "rc",
                        "status",
                        "reason_codes",
                        "window_classified_song",
                        "song_complete",
                        "song_completion_evidence",
                        "transient_failure_code",
                        "next_retry_at_epoch",
                        "next_retry_at",
                    )
                }
                for key in (
                    "transient_failure_code",
                    "next_retry_at_epoch",
                    "next_retry_at",
                ):
                    if proof_retry.get(key) is not None:
                        result[key] = proof_retry[key]
                # The full-source AGY/CAM++ pass is authoritative.  A timeout,
                # nonzero exit, malformed/missing artifact, unknown reason, or
                # semantic rejection are all the same at this boundary: not a
                # complete positive.  Keep the tight attempt only for timeline
                # forensics; every nonpositive authoritative result revokes all
                # top-level authorization rather than promoting a hand-picked
                # subset of known performer reason codes.
                proof_reasons = [str(code) for code in (proof_retry.get("reason_codes") or [])]
                if not proof_reasons:
                    proof_reasons = ["SONG_AUTHORITATIVE_RETRY_INCOMPLETE"]
                result["decision"] = "BLOCK"
                result["reason_codes"] = list(
                    dict.fromkeys([*(result.get("reason_codes") or []), *proof_reasons])
                )
                result["song_completion_evidence"] = proof_retry.get("song_completion_evidence")
                result["song_complete"] = False
                result["lyrics_alignment_ready"] = bool(proof_retry.get("lyrics_alignment_ready"))
                result["full_source_authoritative_block"] = True
                result.pop("delivered", None)
                result.pop("delivered_sidecars", None)
                if proof_retry.get("rc") == 0 and SONG_TERMINAL_PERFORMER_REJECTION_CODES.intersection(proof_reasons):
                    result["full_source_performer_rejection"] = True
    if not result.get("window_classified_song") and not result.get("delivered"):
        d0, d1 = _song_core_span(src_srt, anchor_start, anchor_end)
        if d0 >= anchor_start + SONG_ANCHOR_TRIM_MIN_MS or d1 <= anchor_end - SONG_ANCHOR_TRIM_MIN_MS:
            log(f"song lane {cid}: window classified as talk — retrying on sung core {d0 // 1000}-{d1 // 1000}s")
            retry = attempt(*window_for(d0, d1), "_core")
            if retry.get("window_classified_song") or retry.get("delivered"):
                result = {**retry, "retried_core": True}
    return result


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


def _validate_repaired_cover_generation(
    *, cover: Path, title: str, candidate_id: str
) -> tuple[dict, Path]:
    manifest_path = cover.with_suffix(".cover_generation.json")
    try:
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"cover generation manifest is missing or invalid: {exc}") from exc
    if not isinstance(document, dict):
        raise ValueError("cover generation manifest must be an object")
    if document.get("status") != "AI_COVER_READY":
        raise ValueError("cover generation did not reach AI_COVER_READY")
    if document.get("workflow") != "regenerate_lidousha_cover":
        raise ValueError("unexpected cover generation workflow")
    if document.get("method") != "images.edit" or document.get("fallback_used") is not False:
        raise ValueError("cover generation must be a real images.edit result without frame fallback")
    if document.get("candidate_id") != candidate_id or document.get("title") != title:
        raise ValueError("cover generation candidate/title binding mismatch")
    model = str(document.get("model") or "")
    if model not in {"gpt-image-2", "gpt-image-1.5"}:
        raise ValueError(f"unapproved cover image model: {model!r}")
    attempted = [str(item) for item in document.get("attempted_models") or []]
    fallback_used = bool(document.get("model_fallback_used"))
    if not attempted or attempted[-1] != model or len(attempted) != len(set(attempted)):
        raise ValueError("attempted_models must be a unique ordered chain ending in the selected model")
    if fallback_used != (len(attempted) > 1):
        raise ValueError("cover model fallback flag does not match attempted_models")
    if fallback_used and attempted != ["gpt-image-2", "gpt-image-1.5"]:
        raise ValueError("unapproved cover model fallback chain")
    try:
        if Path(str(document.get("final_cover") or "")).resolve(strict=True) != cover.resolve(strict=True):
            raise ValueError("cover generation final_cover path mismatch")
    except OSError as exc:
        raise ValueError(f"cover generation final artifact is missing: {exc}") from exc
    if not _matches_sha256(cover, str(document.get("final_cover_sha256") or "")):
        raise ValueError("cover generation final_cover hash mismatch")
    for path_key, hash_key in (
        ("ai_background", "ai_background_sha256"),
        ("reference_image", "reference_sha256"),
        ("request_path", "request_sha256"),
        ("response_path", "response_sha256"),
    ):
        path_value, hash_value = document.get(path_key), document.get(hash_key)
        if not isinstance(path_value, str) or not isinstance(hash_value, str):
            raise ValueError(f"cover generation missing {path_key}/{hash_key}")
        if not _matches_sha256(Path(path_value), hash_value):
            raise ValueError(f"cover generation {path_key} hash mismatch")
    return document, manifest_path


def _read_json_object(path: Path, *, label: str) -> dict:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"{label} is missing or invalid ({path}): {exc}") from exc
    if not isinstance(document, dict):
        raise ValueError(f"{label} is not an object: {path}")
    return document


def _document_video_hash(document: dict) -> str | None:
    hashes = document.get("artifact_hashes")
    if not isinstance(hashes, dict):
        return None
    value = hashes.get("burned_video_sha256") or hashes.get("video_sha256")
    return str(value) if isinstance(value, str) else None


def _active_cover_documents(
    *, date: str, candidate_id: str, title: str, mp4: Path, media_sha256: str
) -> list[tuple[Path, dict]]:
    """Load and validate only the delivery record plus its explicitly-bound
    active source record/publish draft.  Never recursively glob a candidate
    directory: old attempts and quarantines are immutable evidence."""

    delivery_record = mp4.with_suffix(".record.json")
    delivery_document = _read_json_object(delivery_record, label="delivery record")
    delivery_staging = delivery_document.get("publish_staging")
    if not isinstance(delivery_staging, dict):
        raise ValueError("delivery record has no publish_staging object")
    if delivery_staging.get("title") != title or delivery_staging.get("upload_enabled") is not False:
        raise ValueError("delivery record title/upload binding mismatch")
    if delivery_document.get("delivery_candidate_id") not in (None, candidate_id):
        raise ValueError("delivery record candidate binding mismatch")
    source_candidate_id = str(delivery_document.get("source_candidate_id") or candidate_id)
    if _document_video_hash(delivery_document) != media_sha256:
        raise ValueError("delivery record video hash does not match current delivery")

    active_root = (BASE / "out" / date / candidate_id).resolve()
    explicit_publish = delivery_staging.get("publish_json_path")
    if isinstance(explicit_publish, str) and explicit_publish:
        publish_path = Path(explicit_publish)
    else:
        publish_path = active_root / "replacement_recuts" / f"{candidate_id}.recut.publish.json"
    try:
        publish_resolved = publish_path.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"active publish draft is missing: {publish_path}") from exc
    if not publish_resolved.is_relative_to(active_root):
        raise ValueError("active publish draft escapes the current candidate root")

    publish_document = _read_json_object(publish_resolved, label="active publish draft")
    if (
        publish_document.get("schema_version") != "shadow-publish-draft.v1"
        or publish_document.get("candidate_id") != source_candidate_id
        or publish_document.get("title") != title
        or publish_document.get("upload_enabled") is not False
        or _document_video_hash(publish_document) != media_sha256
    ):
        raise ValueError("active publish candidate/title/video/upload binding mismatch")

    name = publish_resolved.name
    if name.endswith(".recut.publish.json"):
        source_record = publish_resolved.with_name(name[: -len(".recut.publish.json")] + ".record.json")
    elif name.endswith(".publish.json"):
        source_record = publish_resolved.with_name(name[: -len(".publish.json")] + ".record.json")
    else:
        raise ValueError(f"unrecognized active publish filename: {publish_resolved.name}")
    source_document = _read_json_object(source_record, label="active source record")
    source_staging = source_document.get("publish_staging")
    if not isinstance(source_staging, dict):
        raise ValueError("active source record has no publish_staging object")
    if (
        source_staging.get("title") != title
        or source_staging.get("upload_enabled") is not False
        or _document_video_hash(source_document) != media_sha256
    ):
        raise ValueError("active source record title/video/upload binding mismatch")
    if source_document.get("delivery_candidate_id") not in (None, candidate_id):
        raise ValueError("active source record candidate binding mismatch")
    if str(source_document.get("source_candidate_id") or source_candidate_id) != source_candidate_id:
        raise ValueError("active source record source-candidate binding mismatch")
    explicit_source_publish = source_staging.get("publish_json_path")
    if isinstance(explicit_source_publish, str) and Path(explicit_source_publish).resolve() != publish_resolved:
        raise ValueError("active source record points at a different publish draft")

    documents: list[tuple[Path, dict]] = [(delivery_record, delivery_document)]
    if source_record.resolve() != delivery_record.resolve():
        documents.append((source_record, source_document))
    documents.append((publish_resolved, publish_document))
    return documents


def _active_song_delivery_manifest(
    rec: dict,
    *,
    candidate_id: str,
    mp4: Path,
    documents: list[tuple[Path, dict]],
) -> tuple[Path, dict] | None:
    manifest_value = rec.get("delivery_manifest_path")
    if manifest_value is None:
        return None
    manifest_sha256 = rec.get("delivery_manifest_sha256")
    if not isinstance(manifest_value, str) or not isinstance(manifest_sha256, str):
        raise ValueError("song delivery manifest state binding is incomplete")
    manifest_path = Path(manifest_value)
    try:
        manifest_resolved = manifest_path.resolve(strict=True)
    except OSError as exc:
        raise ValueError("song delivery manifest is missing") from exc
    if manifest_resolved.parent != mp4.parent.resolve() or not _matches_sha256(
        manifest_resolved, manifest_sha256
    ):
        raise ValueError("song delivery manifest path/hash binding mismatch")
    manifest = _read_json_object(manifest_resolved, label="song delivery manifest")
    artifacts = manifest.get("artifacts")
    if (
        manifest.get("schema_version") != VERIFIED_SONG_DELIVERY_SCHEMA_VERSION
        or manifest.get("status") != "DELIVERED_NO_UPLOAD"
        or manifest.get("candidate_id") != candidate_id
        or manifest.get("upload_enabled") is not False
        or not isinstance(artifacts, dict)
    ):
        raise ValueError("song delivery manifest authority mismatch")
    video = artifacts.get("video")
    if not isinstance(video, dict):
        raise ValueError("song delivery manifest has no video artifact")
    try:
        video_path = Path(str(video.get("path") or "")).resolve(strict=True)
    except OSError as exc:
        raise ValueError("song delivery manifest video is missing") from exc
    if video_path != mp4.resolve() or not _matches_sha256(mp4, str(video.get("sha256") or "")):
        raise ValueError("song delivery manifest video binding mismatch")

    delivery_record = mp4.with_suffix(".record.json").resolve(strict=True)
    source_records = [path.resolve(strict=True) for path, _doc in documents if path.suffixes[-2:] == [".record", ".json"] and path.resolve() != delivery_record]
    if len(source_records) != 1:
        raise ValueError("song delivery manifest requires one active source record")
    active_record = artifacts.get("active_record")
    if not isinstance(active_record, dict):
        raise ValueError("song delivery manifest has no active_record artifact")
    try:
        delivered_record_path = Path(str(active_record.get("path") or "")).resolve(strict=True)
        source_record_path = Path(str(active_record.get("source_path") or "")).resolve(strict=True)
    except OSError as exc:
        raise ValueError("song delivery manifest record artifact is missing") from exc
    if (
        delivered_record_path != delivery_record
        or source_record_path != source_records[0]
        or not _matches_sha256(delivery_record, str(active_record.get("sha256") or ""))
        or not _matches_sha256(source_records[0], str(active_record.get("source_sha256") or ""))
    ):
        raise ValueError("song delivery manifest active_record binding mismatch")
    delivered_sidecars = rec.get("delivered_sidecars")
    delivered_sidecar_hashes = rec.get("delivered_sidecar_hashes")
    if not isinstance(delivered_sidecars, dict) or not isinstance(
        delivered_sidecar_hashes, dict
    ):
        raise ValueError("song delivery state has no sidecar authority maps")
    if (
        delivered_sidecars.get("active_record") != str(delivery_record)
        or delivered_sidecar_hashes.get("active_record") != active_record.get("sha256")
    ):
        raise ValueError("song delivery state active_record binding mismatch")
    cover_artifact = artifacts.get("cover")
    if cover_artifact is not None:
        if not isinstance(cover_artifact, dict) or (
            delivered_sidecars.get("cover") != cover_artifact.get("path")
            or delivered_sidecar_hashes.get("cover") != cover_artifact.get("sha256")
        ):
            raise ValueError("song delivery state cover binding mismatch")
    return manifest_resolved, manifest


def _updated_song_delivery_manifest(
    manifest: dict,
    *,
    delivery_record_path: Path,
    delivery_record: dict,
    source_record_path: Path,
    source_record: dict,
    cover: Path,
    generated_cover: Path,
    cover_sha256: str,
    binding_path: Path,
    binding_sha256: str,
) -> dict:
    updated = copy.deepcopy(manifest)
    artifacts = updated.get("artifacts")
    if not isinstance(artifacts, dict):
        raise ValueError("song delivery manifest artifacts is not an object")
    delivery_record_sha = "sha256:" + hashlib.sha256(_json_file_bytes(delivery_record)).hexdigest()
    source_record_sha = "sha256:" + hashlib.sha256(_json_file_bytes(source_record)).hexdigest()
    artifacts["active_record"] = {
        "path": str(delivery_record_path),
        "sha256": delivery_record_sha,
        "source_path": str(source_record_path),
        "source_sha256": source_record_sha,
    }
    artifacts["cover"] = {
        "path": str(cover),
        "sha256": cover_sha256,
        "source_path": str(generated_cover),
        "source_sha256": cover_sha256,
    }
    absent = updated.get("absent_artifacts")
    if isinstance(absent, dict):
        absent.pop("cover", None)
    updated["cover_repair_binding"] = {
        "path": str(binding_path),
        "sha256": binding_sha256,
    }
    return updated


def _cover_reason_codes_without_transient_failure(value: object) -> list[str]:
    prefixes = ("CPA_AI_COVER", "CPA_IMAGE_EDIT", "COVER_REFERENCE_EXTRACTION")
    return [
        str(code)
        for code in (value if isinstance(value, list) else [])
        if not str(code).startswith(prefixes)
        and str(code) != "REVIEWED_COVER_REPLACEMENT_REQUIRED"
    ]


def _updated_cover_document(
    document: dict,
    *,
    cover: Path,
    cover_sha256: str,
    generation: dict,
    binding_path: Path,
    binding_sha256: str,
) -> dict:
    updated = copy.deepcopy(document)
    artifacts = updated.setdefault("artifact_hashes", {})
    if not isinstance(artifacts, dict):
        raise ValueError("artifact_hashes is not an object")
    artifacts["cover_sha256"] = cover_sha256
    updated["cover_repair_binding"] = {
        "path": str(binding_path),
        "sha256": binding_sha256,
    }
    if updated.get("schema_version") == "shadow-publish-draft.v1":
        updated.update(
            {
                "cover_status": "AI_COVER_READY",
                "cover_path": str(cover),
                "cover_generation": generation,
                "reason_codes": _cover_reason_codes_without_transient_failure(
                    updated.get("reason_codes")
                ),
                "upload_enabled": False,
            }
        )
    staging = updated.get("publish_staging")
    if isinstance(staging, dict):
        staging.update(
            {
                "cover_status": "AI_COVER_READY",
                "cover_path": str(cover),
                "cover_generation": generation,
                "reason_codes": _cover_reason_codes_without_transient_failure(
                    staging.get("reason_codes")
                ),
                "upload_enabled": False,
            }
        )
    return updated


def _restore_transaction_files(originals: dict[Path, bytes | None]) -> bool:
    restored = True
    for path, content in reversed(list(originals.items())):
        try:
            if content is None:
                path.unlink(missing_ok=True)
            else:
                _atomic_write_bytes_file(path, content)
        except OSError:
            # Preserve the original exception.  The state remains uncommitted,
            # and the next tick's binding validator will loudly reject any
            # partial filesystem state left by an actual disk failure.
            restored = False
    for path, content in originals.items():
        if content is None:
            restored = restored and not path.exists()
        else:
            try:
                restored = restored and path.read_bytes() == content
            except OSError:
                restored = False
    return restored


COVER_TRANSACTION_SCHEMA_VERSION = "lidousha-cover-transaction.v1"


def _set_cover_transaction_status(journal_path: Path, journal: dict, status: str) -> None:
    updated = copy.deepcopy(journal)
    updated["status"] = status
    updated["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _atomic_write_json_file(journal_path, updated)
    journal.clear()
    journal.update(updated)


def _prepare_cover_transaction(
    *,
    generated_cover: Path,
    candidate_id: str,
    title: str,
    mp4: Path,
    target_payloads: list[tuple[Path, bytes]],
) -> tuple[Path, dict, dict[Path, bytes | None]]:
    transaction_root = generated_cover.parent / "transaction"
    originals_root = transaction_root / "originals"
    intended_root = transaction_root / "intended"
    journal_path = generated_cover.parent / "cover-transaction.json"
    if journal_path.exists():
        raise ValueError(f"immutable cover transaction already exists: {journal_path}")
    originals: dict[Path, bytes | None] = {}
    entries: list[dict] = []
    seen: set[Path] = set()
    for index, (target, intended) in enumerate(target_payloads):
        target = target.absolute()
        if target in seen:
            raise ValueError(f"duplicate cover transaction target: {target}")
        seen.add(target)
        original = target.read_bytes() if target.is_file() else None
        originals[target] = original
        intended_blob = intended_root / f"{index:03d}.bin"
        _atomic_write_bytes_file(intended_blob, intended)
        original_blob = None
        original_sha256 = None
        if original is not None:
            original_blob = originals_root / f"{index:03d}.bin"
            _atomic_write_bytes_file(original_blob, original)
            original_sha256 = "sha256:" + hashlib.sha256(original).hexdigest()
        entries.append(
            {
                "target": str(target),
                "intended_blob": str(intended_blob),
                "intended_sha256": "sha256:" + hashlib.sha256(intended).hexdigest(),
                "original_exists": original is not None,
                "original_blob": str(original_blob) if original_blob is not None else None,
                "original_sha256": original_sha256,
            }
        )
    journal = {
        "schema_version": COVER_TRANSACTION_SCHEMA_VERSION,
        "status": "PREPARED",
        "candidate_id": candidate_id,
        "title": title,
        "media_path": str(mp4.resolve(strict=True)),
        "media_sha256": "sha256:" + _sha256_regular_file(mp4),
        "generation_dir": str(generated_cover.parent.resolve(strict=True)),
        "entries": entries,
        "upload_enabled": False,
        "prepared_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    _atomic_write_json_file(journal_path, journal)
    return journal_path, journal, originals


def _roll_forward_prepared_cover_transactions(
    date: str, rec: dict, mp4: Path, cover: Path
) -> bool:
    """Idempotently complete a PREPARED filesystem transaction after SIGKILL
    or host loss.  Intended bytes are immutable, hash-checked blobs; target
    paths are restricted to this candidate's active/delivery/generation roots."""

    cid = str(rec.get("candidate_id") or "")
    title = str(rec.get("title") or "")
    root = BASE / "out" / date / cid / "cover_repair" / "generations"
    recovered = False
    for journal_path in sorted(root.glob("*/cover-transaction.json")):
        try:
            journal = _read_json_object(journal_path, label="cover transaction journal")
        except ValueError:
            continue
        if journal.get("status") != "PREPARED":
            continue
        entries = journal.get("entries")
        try:
            generation_dir = Path(str(journal.get("generation_dir") or "")).resolve(strict=True)
            media_path = Path(str(journal.get("media_path") or "")).resolve(strict=True)
        except OSError:
            continue
        if (
            journal.get("schema_version") != COVER_TRANSACTION_SCHEMA_VERSION
            or journal.get("candidate_id") != cid
            or journal.get("title") != title
            or journal.get("upload_enabled") is not False
            or generation_dir != journal_path.parent.resolve()
            or media_path != mp4.resolve()
            or not _matches_sha256(mp4, str(journal.get("media_sha256") or ""))
            or not isinstance(entries, list)
            or not entries
        ):
            continue
        validated: list[tuple[Path, bytes]] = []
        seen: set[Path] = set()
        binding_payload: dict | None = None
        valid = True
        for entry in entries:
            if not isinstance(entry, dict):
                valid = False
                break
            target = Path(str(entry.get("target") or "")).absolute()
            intended_blob = Path(str(entry.get("intended_blob") or ""))
            try:
                intended_allowed = intended_blob.resolve(strict=True).is_relative_to(
                    journal_path.parent.resolve(strict=True)
                )
                intended_matches = _matches_sha256(
                    intended_blob, str(entry.get("intended_sha256") or "")
                )
            except OSError:
                valid = False
                break
            if target in seen or target.is_symlink() or not intended_allowed or not intended_matches:
                valid = False
                break
            seen.add(target)
            try:
                payload = intended_blob.read_bytes()
            except OSError:
                valid = False
                break
            validated.append((target, payload))
            if target.name.endswith(".cover-binding.json"):
                try:
                    candidate_binding = json.loads(payload.decode("utf-8"))
                except (UnicodeError, ValueError):
                    valid = False
                    break
                if isinstance(candidate_binding, dict):
                    binding_payload = candidate_binding
        if not valid or binding_payload is None:
            continue
        try:
            generation_cover = Path(str(binding_payload.get("generation_cover_path") or "")).resolve(strict=True)
            generation, generation_path = _validate_repaired_cover_generation(
                cover=generation_cover,
                title=title,
                candidate_id=cid,
            )
        except (OSError, ValueError):
            continue
        if (
            binding_payload.get("schema_version") != "lidousha-cover-repair-binding.v1"
            or binding_payload.get("candidate_id") != cid
            or binding_payload.get("title") != title
            or binding_payload.get("upload_enabled") is not False
            or binding_payload.get("media_path") != str(mp4.resolve())
            or binding_payload.get("media_sha256") != journal.get("media_sha256")
            or binding_payload.get("cover_path") != str(cover.resolve())
            or binding_payload.get("cover_sha256")
            != binding_payload.get("generation_cover_sha256")
            or not _matches_sha256(
                generation_cover,
                str(binding_payload.get("generation_cover_sha256") or ""),
            )
            or binding_payload.get("generation_manifest_path") != str(generation_path.resolve())
            or not _matches_sha256(
                generation_path,
                str(binding_payload.get("generation_manifest_sha256") or ""),
            )
            or binding_payload.get("selected_model") != generation.get("model")
        ):
            continue
        binding_target = generation_cover.with_suffix(".cover-binding.json").absolute()
        try:
            active_documents = _active_cover_documents(
                date=date,
                candidate_id=cid,
                title=title,
                mp4=mp4,
                media_sha256=str(journal.get("media_sha256") or ""),
            )
        except (OSError, ValueError):
            continue
        expected_targets = {
            binding_target,
            *(path.absolute() for path, _document in active_documents),
        }
        if generation_cover.resolve() != cover.resolve():
            expected_targets.add(cover.absolute())
        if rec.get("delivered"):
            manifest_value = rec.get("delivery_manifest_path")
            if (
                binding_payload.get("authority_type") != "verified_song_delivery"
                or not isinstance(manifest_value, str)
                or binding_payload.get("delivery_manifest_path") != manifest_value
            ):
                continue
            expected_targets.add(Path(manifest_value).absolute())
        elif (
            binding_payload.get("authority_type") != "talk_delivery_record"
            or binding_payload.get("delivery_manifest_path") is not None
        ):
            continue
        if seen != expected_targets or binding_target not in seen:
            # Never accept a directory-wide capability.  The only writable
            # paths are the exact active record/publish files derived above,
            # the delivery artifacts, and this generation's binding.
            continue

        payloads = {target: payload for target, payload in validated}
        binding_sha256 = "sha256:" + hashlib.sha256(payloads[binding_target]).hexdigest()
        if generation_cover.resolve() != cover.resolve():
            if (
                "sha256:" + hashlib.sha256(payloads[cover.absolute()]).hexdigest()
                != binding_payload.get("cover_sha256")
            ):
                continue
        intended_documents: dict[Path, dict] = {}
        for document_path, current_document in active_documents:
            try:
                intended_document = json.loads(
                    payloads[document_path.absolute()].decode("utf-8")
                )
            except (KeyError, UnicodeError, ValueError):
                valid = False
                break
            if not isinstance(intended_document, dict):
                valid = False
                break
            hashes = intended_document.get("artifact_hashes")
            pointer = intended_document.get("cover_repair_binding")
            publish_view = (
                intended_document
                if intended_document.get("schema_version") == "shadow-publish-draft.v1"
                else intended_document.get("publish_staging")
            )
            if (
                not isinstance(hashes, dict)
                or hashes.get("cover_sha256") != binding_payload.get("cover_sha256")
                or not isinstance(pointer, dict)
                or pointer.get("path") != str(binding_target)
                or pointer.get("sha256") != binding_sha256
                or not isinstance(publish_view, dict)
                or publish_view.get("title") != title
                or publish_view.get("cover_status") != "AI_COVER_READY"
                or publish_view.get("cover_path") != str(cover)
                or publish_view.get("cover_generation") != generation
                or publish_view.get("upload_enabled") is not False
                or _document_video_hash(intended_document)
                != journal.get("media_sha256")
                or intended_document.get("schema_version")
                != current_document.get("schema_version")
                or intended_document.get("candidate_id")
                != current_document.get("candidate_id")
                or intended_document.get("delivery_candidate_id")
                != current_document.get("delivery_candidate_id")
                or intended_document.get("source_candidate_id")
                != current_document.get("source_candidate_id")
            ):
                valid = False
                break
            current_staging = current_document.get("publish_staging")
            intended_staging = intended_document.get("publish_staging")
            if isinstance(current_staging, dict) and (
                not isinstance(intended_staging, dict)
                or intended_staging.get("publish_json_path")
                != current_staging.get("publish_json_path")
            ):
                valid = False
                break
            intended_documents[document_path.resolve()] = intended_document
        if not valid:
            continue
        if rec.get("delivered"):
            manifest_target = Path(str(rec["delivery_manifest_path"])).absolute()
            try:
                intended_manifest = json.loads(payloads[manifest_target].decode("utf-8"))
            except (KeyError, UnicodeError, ValueError):
                continue
            if not isinstance(intended_manifest, dict):
                continue
            manifest_artifacts = (
                intended_manifest.get("artifacts")
                if isinstance(intended_manifest, dict)
                else None
            )
            video_artifact = (
                manifest_artifacts.get("video")
                if isinstance(manifest_artifacts, dict)
                else None
            )
            cover_artifact = (
                manifest_artifacts.get("cover")
                if isinstance(manifest_artifacts, dict)
                else None
            )
            active_record = (
                manifest_artifacts.get("active_record")
                if isinstance(manifest_artifacts, dict)
                else None
            )
            manifest_pointer = (
                intended_manifest.get("cover_repair_binding")
                if isinstance(intended_manifest, dict)
                else None
            )
            delivery_record_path = mp4.with_suffix(".record.json").resolve()
            source_record_paths = [
                path
                for path in intended_documents
                if path.name.endswith(".record.json") and path != delivery_record_path
            ]
            if len(source_record_paths) != 1:
                continue
            source_record_path = source_record_paths[0]
            if (
                intended_manifest.get("schema_version")
                != VERIFIED_SONG_DELIVERY_SCHEMA_VERSION
                or intended_manifest.get("status") != "DELIVERED_NO_UPLOAD"
                or intended_manifest.get("candidate_id") != cid
                or intended_manifest.get("upload_enabled") is not False
                or not isinstance(video_artifact, dict)
                or video_artifact.get("path") != str(mp4)
                or video_artifact.get("sha256") != journal.get("media_sha256")
                or not isinstance(cover_artifact, dict)
                or cover_artifact.get("path") != str(cover)
                or cover_artifact.get("sha256")
                != binding_payload.get("cover_sha256")
                or cover_artifact.get("source_path") != str(generation_cover)
                or cover_artifact.get("source_sha256")
                != binding_payload.get("generation_cover_sha256")
                or not isinstance(active_record, dict)
                or active_record.get("path") != str(delivery_record_path)
                or active_record.get("sha256")
                != "sha256:"
                + hashlib.sha256(payloads[delivery_record_path.absolute()]).hexdigest()
                or active_record.get("source_path") != str(source_record_path)
                or active_record.get("source_sha256")
                != "sha256:"
                + hashlib.sha256(payloads[source_record_path.absolute()]).hexdigest()
                or not isinstance(manifest_pointer, dict)
                or manifest_pointer.get("path") != str(binding_target)
                or manifest_pointer.get("sha256") != binding_sha256
            ):
                continue
        try:
            for target, payload in validated:
                _atomic_write_bytes_file(target, payload)
            for entry in entries:
                if not _matches_sha256(
                    Path(str(entry["target"])), str(entry["intended_sha256"])
                ):
                    raise OSError("cover transaction roll-forward verification failed")
            _set_cover_transaction_status(journal_path, journal, "COMMITTED")
        except OSError:
            continue
        recovered = True
    return recovered


def _bind_repaired_cover(
    date: str,
    rec: dict,
    mp4: Path,
    cover: Path,
    generated_cover: Path | None = None,
) -> None:
    cid = str(rec.get("candidate_id") or "")
    title = str(rec.get("title") or "")
    generated_cover = generated_cover or cover
    generation, generation_path = _validate_repaired_cover_generation(
        cover=generated_cover, title=title, candidate_id=cid
    )
    cover_sha256 = "sha256:" + _sha256_regular_file(generated_cover)
    media_sha256 = "sha256:" + _sha256_regular_file(mp4)
    generation_sha256 = "sha256:" + _sha256_regular_file(generation_path)
    documents = _active_cover_documents(
        date=date,
        candidate_id=cid,
        title=title,
        mp4=mp4,
        media_sha256=media_sha256,
    )
    song_manifest = _active_song_delivery_manifest(
        rec,
        candidate_id=cid,
        mp4=mp4,
        documents=documents,
    )
    binding_path = generated_cover.with_suffix(".cover-binding.json")
    if binding_path.exists():
        raise ValueError(f"immutable cover binding already exists: {binding_path}")
    binding = {
        "schema_version": "lidousha-cover-repair-binding.v1",
        "candidate_id": cid,
        "title": title,
        "media_path": str(mp4.resolve(strict=True)),
        "media_sha256": media_sha256,
        "cover_path": str(cover.resolve()),
        "cover_sha256": cover_sha256,
        "generation_cover_path": str(generated_cover.resolve(strict=True)),
        "generation_cover_sha256": cover_sha256,
        "generation_manifest_path": str(generation_path.resolve(strict=True)),
        "generation_manifest_sha256": generation_sha256,
        "selected_model": generation["model"],
        "attempted_models": generation.get("attempted_models") or [],
        "model_fallback_used": bool(generation.get("model_fallback_used")),
        "authority_type": "verified_song_delivery" if song_manifest is not None else "talk_delivery_record",
        "delivery_manifest_path": str(song_manifest[0]) if song_manifest is not None else None,
        "bound_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "upload_enabled": False,
    }
    binding_bytes = _json_file_bytes(binding)
    binding_sha256 = "sha256:" + hashlib.sha256(binding_bytes).hexdigest()
    updated_documents = [
        (
            path,
            _updated_cover_document(
                document,
                cover=cover,
                cover_sha256=cover_sha256,
                generation=generation,
                binding_path=binding_path,
                binding_sha256=binding_sha256,
            ),
        )
        for path, document in documents
    ]
    updated_song_manifest: tuple[Path, dict] | None = None
    if song_manifest is not None:
        updated_by_path = {path.resolve(): (path, document) for path, document in updated_documents}
        delivery_record_path = mp4.with_suffix(".record.json").resolve(strict=True)
        source_record_paths = [
            path.resolve(strict=True)
            for path, _document in updated_documents
            if path.name.endswith(".record.json") and path.resolve() != delivery_record_path
        ]
        if len(source_record_paths) != 1:
            raise ValueError("song cover binding requires one updated active source record")
        source_record_path = source_record_paths[0]
        updated_song_manifest = (
            song_manifest[0],
            _updated_song_delivery_manifest(
                song_manifest[1],
                delivery_record_path=delivery_record_path,
                delivery_record=updated_by_path[delivery_record_path][1],
                source_record_path=source_record_path,
                source_record=updated_by_path[source_record_path][1],
                cover=cover,
                generated_cover=generated_cover,
                cover_sha256=cover_sha256,
                binding_path=binding_path,
                binding_sha256=binding_sha256,
            ),
        )
        # The verified song delivery manifest remains the commit marker and is
        # therefore installed after its active records and publish draft.
        updated_documents.append(updated_song_manifest)

    target_payloads: list[tuple[Path, bytes]] = []
    if generated_cover.resolve() != cover.resolve():
        target_payloads.append((cover, generated_cover.read_bytes()))
    target_payloads.append((binding_path, binding_bytes))
    target_payloads.extend(
        (document_path, _json_file_bytes(document))
        for document_path, document in updated_documents
    )
    journal_path, journal, originals = _prepare_cover_transaction(
        generated_cover=generated_cover,
        candidate_id=cid,
        title=title,
        mp4=mp4,
        target_payloads=target_payloads,
    )
    try:
        for target, payload in target_payloads:
            _atomic_write_bytes_file(target, payload)
        for target, payload in target_payloads:
            if target.read_bytes() != payload:
                raise OSError(f"cover transaction verification failed: {target}")
        _set_cover_transaction_status(journal_path, journal, "COMMITTED")
    except BaseException:
        restored = _restore_transaction_files(originals)
        if restored:
            try:
                _set_cover_transaction_status(journal_path, journal, "ROLLED_BACK")
            except OSError:
                # Keep PREPARED if the journal status cannot be updated.  The
                # next tick can safely roll the immutable intended bytes
                # forward instead of spending on another image request.
                pass
        raise

    repaired_record = copy.deepcopy(rec)
    repaired_record.update(
        {
            "cover_status": "REPAIRED_AI_COVER",
            "cover_path": str(cover),
            "cover_sha256": cover_sha256,
            "cover_generation": generation,
            "cover_generation_path": str(generation_path),
            "cover_generation_sha256": generation_sha256,
            "cover_binding_path": str(binding_path),
            "cover_binding_sha256": binding_sha256,
        }
    )
    if updated_song_manifest is not None:
        manifest_path, manifest_document = updated_song_manifest
        repaired_record["delivery_manifest_path"] = str(manifest_path)
        repaired_record["delivery_manifest_sha256"] = (
            "sha256:" + hashlib.sha256(_json_file_bytes(manifest_document)).hexdigest()
        )
        delivered_sidecars = repaired_record.setdefault("delivered_sidecars", {})
        delivered_sidecar_hashes = repaired_record.setdefault("delivered_sidecar_hashes", {})
        manifest_artifacts = manifest_document.get("artifacts")
        active_record = (
            manifest_artifacts.get("active_record")
            if isinstance(manifest_artifacts, dict)
            else None
        )
        if not isinstance(active_record, dict):
            raise ValueError("updated song manifest lost active_record authority")
        if isinstance(delivered_sidecars, dict):
            delivered_sidecars["cover"] = str(cover)
            delivered_sidecars["active_record"] = str(active_record["path"])
        if isinstance(delivered_sidecar_hashes, dict):
            delivered_sidecar_hashes["cover"] = cover_sha256
            delivered_sidecar_hashes["active_record"] = str(active_record["sha256"])
    repaired_record["cover_transaction_path"] = str(journal_path)
    repaired_record["cover_transaction_status"] = "COMMITTED"
    if isinstance(repaired_record.get("summary"), dict):
        repaired_record["summary"].update(
            {
                "cover_status": "REPAIRED_AI_COVER",
                "cover_path": str(cover),
                "cover_sha256": cover_sha256,
                "cover_binding_path": str(binding_path),
                "cover_binding_sha256": binding_sha256,
            }
        )
    rec.clear()
    rec.update(repaired_record)


def _cover_binding_valid(date: str, rec: dict, mp4: Path, cover: Path) -> bool:
    binding_path_value = rec.get("cover_binding_path")
    binding_sha256 = rec.get("cover_binding_sha256")
    if not isinstance(binding_path_value, str) or not isinstance(binding_sha256, str):
        return False
    binding_path = Path(binding_path_value)
    expected_root = (BASE / "out" / date / str(rec.get("candidate_id") or "") / "cover_repair" / "generations").resolve()
    try:
        binding_resolved = binding_path.resolve(strict=True)
        state_cover_resolved = Path(str(rec.get("cover_path") or "")).resolve(
            strict=True
        )
    except OSError:
        return False
    if (
        state_cover_resolved != cover.resolve()
        or not binding_resolved.is_relative_to(expected_root)
        or not _matches_sha256(binding_resolved, binding_sha256)
    ):
        return False
    try:
        binding = _read_json_object(binding_resolved, label="cover repair binding")
        media_path = Path(str(binding.get("media_path") or "")).resolve(strict=True)
        cover_path = Path(str(binding.get("cover_path") or "")).resolve(strict=True)
        generation_path = Path(str(binding.get("generation_manifest_path") or "")).resolve(strict=True)
        generation_cover = Path(str(binding.get("generation_cover_path") or "")).resolve(strict=True)
    except (OSError, ValueError):
        return False
    if (
        binding.get("schema_version") != "lidousha-cover-repair-binding.v1"
        or binding.get("candidate_id") != rec.get("candidate_id")
        or binding.get("title") != rec.get("title")
        or binding.get("upload_enabled") is not False
        or media_path != mp4.resolve()
        or cover_path != cover.resolve()
        or not _matches_sha256(mp4, str(binding.get("media_sha256") or ""))
        or not _matches_sha256(cover, str(binding.get("cover_sha256") or ""))
        or not _matches_sha256(generation_cover, str(binding.get("generation_cover_sha256") or ""))
        or not _matches_sha256(generation_path, str(binding.get("generation_manifest_sha256") or ""))
        or rec.get("cover_sha256") != binding.get("cover_sha256")
        or rec.get("cover_generation_path") != str(generation_path)
        or rec.get("cover_generation_sha256") != binding.get("generation_manifest_sha256")
    ):
        return False
    try:
        generation, validated_path = _validate_repaired_cover_generation(
            cover=generation_cover,
            title=str(rec.get("title") or ""),
            candidate_id=str(rec.get("candidate_id") or ""),
        )
    except (OSError, ValueError):
        return False
    if not (
        validated_path.resolve() == generation_path
        and binding.get("selected_model") == generation.get("model")
        and str(binding.get("cover_sha256")) == str(binding.get("generation_cover_sha256"))
        and rec.get("cover_generation") == generation
    ):
        return False
    summary = rec.get("summary")
    if isinstance(summary, dict) and summary and (
        summary.get("cover_status") != "REPAIRED_AI_COVER"
        or summary.get("cover_path") != str(cover)
        or summary.get("cover_sha256") != binding.get("cover_sha256")
        or summary.get("cover_binding_path") != str(binding_path)
        or summary.get("cover_binding_sha256") != binding_sha256
    ):
        return False
    try:
        documents = _active_cover_documents(
            date=date,
            candidate_id=str(rec.get("candidate_id") or ""),
            title=str(rec.get("title") or ""),
            mp4=mp4,
            media_sha256=str(binding.get("media_sha256") or ""),
        )
    except (OSError, ValueError):
        return False
    for _path, document in documents:
        hashes = document.get("artifact_hashes")
        pointer = document.get("cover_repair_binding")
        if (
            not isinstance(hashes, dict)
            or hashes.get("cover_sha256") != binding.get("cover_sha256")
            or not isinstance(pointer, dict)
            or pointer.get("path") != str(binding_path)
            or pointer.get("sha256") != binding_sha256
        ):
            return False
        publish_view = (
            document
            if document.get("schema_version") == "shadow-publish-draft.v1"
            else document.get("publish_staging")
        )
        if not isinstance(publish_view, dict):
            return False
        try:
            published_cover = Path(str(publish_view.get("cover_path") or "")).resolve(strict=True)
        except OSError:
            return False
        if (
            publish_view.get("cover_status") != "AI_COVER_READY"
            or publish_view.get("upload_enabled") is not False
            or published_cover != cover.resolve()
            or publish_view.get("cover_generation") != generation
        ):
            return False
    try:
        song_manifest = _active_song_delivery_manifest(
            rec,
            candidate_id=str(rec.get("candidate_id") or ""),
            mp4=mp4,
            documents=documents,
        )
    except (OSError, ValueError):
        return False
    if song_manifest is None:
        return binding.get("authority_type") == "talk_delivery_record" and binding.get("delivery_manifest_path") is None
    manifest_path, manifest = song_manifest
    artifacts = manifest.get("artifacts")
    cover_artifact = artifacts.get("cover") if isinstance(artifacts, dict) else None
    pointer = manifest.get("cover_repair_binding")
    if not isinstance(cover_artifact, dict) or not isinstance(pointer, dict):
        return False
    try:
        manifest_cover = Path(str(cover_artifact.get("path") or "")).resolve(strict=True)
        manifest_source = Path(str(cover_artifact.get("source_path") or "")).resolve(strict=True)
    except OSError:
        return False
    return (
        binding.get("authority_type") == "verified_song_delivery"
        and binding.get("delivery_manifest_path") == str(manifest_path)
        and manifest_cover == cover.resolve()
        and manifest_source == generation_cover.resolve()
        and _matches_sha256(cover, str(cover_artifact.get("sha256") or ""))
        and _matches_sha256(generation_cover, str(cover_artifact.get("source_sha256") or ""))
        and pointer.get("path") == str(binding_path)
        and pointer.get("sha256") == binding_sha256
    )


def _recover_committed_cover_binding(date: str, rec: dict, mp4: Path, cover: Path) -> bool:
    """Recover state after a crash between the filesystem binding commit and
    ``write_state``.  Immutable generations plus active-doc/manifest pointers
    are sufficient to reconstruct state without another paid image request.
    Partial bindings are ignored because the ordinary full validator must pass
    before any state field is changed."""

    if not cover.is_file():
        return False
    if str(rec.get("cover_status") or "").upper() == "REPAIRED_AI_COVER" and _cover_binding_valid(
        date, rec, mp4, cover
    ):
        return False
    cid = str(rec.get("candidate_id") or "")
    title = str(rec.get("title") or "")
    root = BASE / "out" / date / cid / "cover_repair" / "generations"
    try:
        candidates = sorted(
            root.glob("*/*.cover-binding.json"),
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        )
    except OSError:
        return False
    for binding_path in candidates:
        try:
            binding = _read_json_object(binding_path, label="recoverable cover binding")
            generation_path = Path(str(binding.get("generation_manifest_path") or "")).resolve(strict=True)
            generation_cover = Path(str(binding.get("generation_cover_path") or "")).resolve(strict=True)
            generation, validated_path = _validate_repaired_cover_generation(
                cover=generation_cover,
                title=title,
                candidate_id=cid,
            )
            if validated_path.resolve() != generation_path:
                continue
            tentative = copy.deepcopy(rec)
            tentative.update(
                {
                    "cover_status": "REPAIRED_AI_COVER",
                    "cover_path": str(cover),
                    "cover_sha256": str(binding.get("cover_sha256") or ""),
                    "cover_generation": generation,
                    "cover_generation_path": str(generation_path),
                    "cover_generation_sha256": str(
                        binding.get("generation_manifest_sha256") or ""
                    ),
                    "cover_binding_path": str(binding_path),
                    "cover_binding_sha256": "sha256:" + _sha256_regular_file(binding_path),
                    "cover_integrity_status": "VALID_BOUND_RECOVERED",
                }
            )
            if binding.get("authority_type") == "verified_song_delivery":
                manifest_path = Path(str(binding.get("delivery_manifest_path") or "")).resolve(strict=True)
                tentative["delivery_manifest_path"] = str(manifest_path)
                tentative["delivery_manifest_sha256"] = (
                    "sha256:" + _sha256_regular_file(manifest_path)
                )
                manifest = _read_json_object(
                    manifest_path, label="recoverable song delivery manifest"
                )
                artifacts = manifest.get("artifacts")
                active_record = (
                    artifacts.get("active_record") if isinstance(artifacts, dict) else None
                )
                if not isinstance(active_record, dict):
                    continue
                sidecars = tentative.setdefault("delivered_sidecars", {})
                sidecar_hashes = tentative.setdefault("delivered_sidecar_hashes", {})
                if isinstance(sidecars, dict):
                    sidecars["cover"] = str(cover)
                    sidecars["active_record"] = str(active_record.get("path") or "")
                if isinstance(sidecar_hashes, dict):
                    sidecar_hashes["cover"] = tentative["cover_sha256"]
                    sidecar_hashes["active_record"] = str(
                        active_record.get("sha256") or ""
                    )
            if not _cover_binding_valid(date, tentative, mp4, cover):
                continue
        except (OSError, ValueError, SongDeliveryError):
            continue
        tentative["cover_repair_recovered_at"] = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
        )
        if isinstance(tentative.get("summary"), dict):
            tentative["summary"].update(
                {
                    "cover_status": "REPAIRED_AI_COVER",
                    "cover_path": str(cover),
                    "cover_sha256": tentative["cover_sha256"],
                    "cover_binding_path": tentative["cover_binding_path"],
                    "cover_binding_sha256": tentative["cover_binding_sha256"],
                }
            )
        rec.clear()
        rec.update(tentative)
        return True
    return False


def _initial_cover_proof_valid(date: str, rec: dict, mp4: Path, cover: Path) -> bool:
    expected_cover = rec.get("cover_sha256")
    expected_video = rec.get("video_sha256") or rec.get("delivered_sha256")
    generation = rec.get("cover_generation")
    if (
        not isinstance(expected_cover, str)
        or not isinstance(expected_video, str)
        or not isinstance(generation, dict)
        or not _matches_sha256(cover, expected_cover)
        or not _matches_sha256(mp4, expected_video)
        or generation.get("title") != rec.get("title")
        or generation.get("method") != "images.edit"
        or generation.get("fallback_used") is not False
        or generation.get("final_cover_sha256") != expected_cover
    ):
        return False
    try:
        source_cover = Path(str(generation.get("final_cover") or "")).resolve(strict=True)
    except OSError:
        return False
    if not _matches_sha256(source_cover, expected_cover):
        return False
    try:
        documents = _active_cover_documents(
            date=date,
            candidate_id=str(rec.get("candidate_id") or ""),
            title=str(rec.get("title") or ""),
            mp4=mp4,
            media_sha256=str(expected_video),
        )
    except (OSError, ValueError):
        return False
    for _path, document in documents:
        hashes = document.get("artifact_hashes")
        publish_view = (
            document
            if document.get("schema_version") == "shadow-publish-draft.v1"
            else document.get("publish_staging")
        )
        if not isinstance(hashes, dict) or not isinstance(publish_view, dict):
            return False
        try:
            published_source_cover = Path(str(publish_view.get("cover_path") or "")).resolve(strict=True)
        except OSError:
            return False
        if (
            hashes.get("cover_sha256") != expected_cover
            or publish_view.get("cover_status") != "AI_COVER_READY"
            or publish_view.get("upload_enabled") is not False
            or published_source_cover != source_cover
            or publish_view.get("cover_generation") != generation
        ):
            return False
    try:
        song_manifest = _active_song_delivery_manifest(
            rec,
            candidate_id=str(rec.get("candidate_id") or ""),
            mp4=mp4,
            documents=documents,
        )
    except (OSError, ValueError):
        return False
    if song_manifest is not None:
        artifacts = song_manifest[1].get("artifacts")
        cover_artifact = artifacts.get("cover") if isinstance(artifacts, dict) else None
        if not isinstance(cover_artifact, dict):
            return False
        try:
            manifest_cover = Path(str(cover_artifact.get("path") or "")).resolve(strict=True)
            manifest_source = Path(str(cover_artifact.get("source_path") or "")).resolve(strict=True)
        except OSError:
            return False
        if (
            manifest_cover != cover.resolve()
            or manifest_source != source_cover
            or not _matches_sha256(cover, str(cover_artifact.get("sha256") or ""))
            or not _matches_sha256(source_cover, str(cover_artifact.get("source_sha256") or ""))
        ):
            return False
    return True


def _refresh_cover_repair_budget(rec: dict, fingerprint: str) -> bool:
    if rec.get("cover_repair_generation") == fingerprint:
        return False
    old_attempts = max(0, int(rec.get("cover_repair_attempts") or 0))
    lifetime = rec.get("cover_repair_lifetime_attempts")
    if lifetime is None:
        lifetime = old_attempts
    history = rec.setdefault("cover_repair_attempt_history", [])
    if not isinstance(history, list):
        history = []
        rec["cover_repair_attempt_history"] = history
    if old_attempts or rec.get("cover_repair_generation"):
        history.append(
            {
                "pipeline_fingerprint": rec.get("cover_repair_generation") or "legacy-unversioned",
                "attempts": old_attempts,
            }
        )
    rec["cover_repair_generation"] = fingerprint
    rec["cover_repair_attempts"] = 0
    rec["cover_repair_lifetime_attempts"] = max(0, int(lifetime))
    return True


def cover_repair_needed(date: str, rec: dict) -> bool:
    # Delivered = passed the delivery gate (for songs: is-song + complete, see
    # song_delivery_ok) — every delivered clip deserves a cover, regardless of
    # what the ADVISORY semantic judge said (Ivan 2026-07-10).  Blocked/failed
    # records have no delivery and never get covers.
    delivered = rec.get("status") in DELIVERED_TALK_STATUSES or bool(rec.get("delivered"))
    if not delivered or not rec.get("title"):
        return False
    paths = delivered_paths(date, rec)
    if paths is None:
        return False
    mp4, cover = paths
    if not cover.is_file():
        return True
    status = str(rec.get("cover_status") or "").upper()
    if "BLOCKED_AI_COVER_REQUIRED" in status or "REPAIR_FAILED" in status:
        return True
    if status == "REPAIRED_AI_COVER":
        return not _cover_binding_valid(date, rec, mp4, cover)
    if status == "AI_COVER_READY":
        return not _initial_cover_proof_valid(date, rec, mp4, cover)
    # A bare PNG or an unknown status has no current title/video/hash authority.
    return True


def _cover_repair_eligible(rec: dict) -> bool:
    """Budget limits paid generation attempts, never integrity detection.
    ``cover_repair_needed`` must stay fail-closed even after exhaustion so a
    later title/media/document drift remains loud in state and reports."""

    return (
        int(rec.get("cover_repair_attempts") or 0) < COVER_REPAIR_MAX_ATTEMPTS
        and int(rec.get("cover_repair_lifetime_attempts") or 0)
        < COVER_REPAIR_LIFETIME_ATTEMPT_CAP
    )


def _cover_authority_preflight(date: str, rec: dict, mp4: Path) -> None:
    """Verify every mutable authority document before a paid image request.

    Binding used to discover missing legacy song records only after generation,
    which spent an image request on a package that could never commit.  Future
    song deliveries carry a verified manifest plus active-record state maps;
    older packages fail loudly and cost-free until an explicit migration is
    performed from their original proof chain.
    """

    candidate_id = str(rec.get("candidate_id") or "")
    documents = _active_cover_documents(
        date=date,
        candidate_id=candidate_id,
        title=str(rec.get("title") or ""),
        mp4=mp4,
        media_sha256="sha256:" + _sha256_regular_file(mp4),
    )
    manifest = _active_song_delivery_manifest(
        rec,
        candidate_id=candidate_id,
        mp4=mp4,
        documents=documents,
    )
    # `delivered` is the runner's stable song-lane signal.  Candidate ids can
    # be song_*, semanticsong_*, or a future anchor family, whereas delivered
    # talk records intentionally do not set this field.
    if rec.get("delivered") and manifest is None:
        raise ValueError("legacy delivered song has no verified active-record manifest")


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


def requeue_recoverable_songs(date: str, state: dict) -> int:
    """Retry non-terminal song BLOCKs when the pipeline changes.

    `UNPROVEN`, missing proof, provider ambiguity, and runner failure mean the
    proof path did not finish; they are not evidence that someone else sang.
    A content fingerprint change earns one new attempt budget.  A transient
    AGY source-context failure additionally gets one same-fingerprint retry.
    Confirmed background playback / non-Li-Dousha singing remains terminal.
    """

    current = pipeline_fingerprint()
    lifetime_attempts = len(state.get("songs", [])) + len(
        state.get("song_superseded_attempts", [])
    )
    existing_pending = {
        str(item.get("cid") or item.get("candidate_id") or "")
        for item in state.get("pending_song", [])
        if isinstance(item, dict)
    }
    kept: list[dict] = []
    requeued: list[dict] = []
    for record in state.get("songs", []):
        if (
            not isinstance(record, dict)
            or record.get("delivered")
            or record.get("verified_delivery_pending_commit") is True
            or record.get("status") not in {"blocked", "failed"}
        ):
            kept.append(record)
            continue
        reasons = {str(code) for code in record.get("reason_codes") or []}
        if reasons & SONG_TERMINAL_PERFORMER_REJECTION_CODES:
            kept.append(record)
            continue
        changed = record.get("pipeline_fingerprint") != current
        retry_count = int(record.get("transient_retry_count") or 0)
        infra_transient = bool(reasons & SONG_INFRA_TRANSIENT_REASON_CODES)
        next_retry_at = record.get("next_retry_at_epoch")
        infra_retry_due = (
            infra_transient
            and (
                not isinstance(next_retry_at, (int, float))
                or isinstance(next_retry_at, bool)
                or time.time() >= float(next_retry_at)
            )
        )
        legacy_transient = (
            bool(
                reasons
                & {
                    "AGY_SOURCE_CONTEXT_RUNNER_FAILED",
                    "PRODUCE_UNEXPECTED_EXCEPTION",
                    "SONG_AUDIO_LRC_ALIGNMENT_INVALID",
                    "SONG_DELIVERY_RECOVERY_AUTHORITY_MISSING",
                }
            )
            and retry_count < 1
        )
        transient = infra_retry_due or legacy_transient
        content_change_retry = changed and lifetime_attempts < SONG_LIFETIME_ATTEMPT_CAP
        cid = str(record.get("candidate_id") or "")
        if not cid or cid in existing_pending or not (content_change_retry or transient):
            kept.append(record)
            continue

        segment_name = Path(str(record.get("segment") or record.get("segment_path") or "")).name
        segment = REC_ROOT / date / segment_name
        if not segment.is_file():
            kept.append(record)
            continue
        start_ms, end_ms = record.get("start_ms"), record.get("end_ms")
        if not isinstance(start_ms, int) or not isinstance(end_ms, int) or start_ms >= end_ms:
            kept.append(record)
            continue
        anchor_start = int(record.get("anchor_start_ms") or max(0, start_ms + SONG_WINDOW_PRE_MS))
        anchor_end = int(record.get("anchor_end_ms") or max(anchor_start + 1, end_ms - SONG_WINDOW_POST_MS))
        seg_dur = ffprobe_ms(segment)
        item = {
            "cid": cid,
            "segment_path": str(segment),
            "seg_dur_ms": seg_dur,
            "anchor_start_ms": anchor_start,
            "anchor_end_ms": min(seg_dur, anchor_end) if seg_dur else anchor_end,
            "xml": str(xml) if (xml := find_danmaku_xml(segment)) else None,
            "chat_jsonl": str(chat) if (chat := find_chat_jsonl(segment)) else None,
            "hook": record.get("hook", ""),
            "preview": record.get("preview", ""),
            "danmaku": int(record.get("danmaku") or 0),
            # Visual title evidence is a first-class song identity hint.  A
            # retry that drops it is weaker than the failed attempt and can
            # repeat the same LRC ambiguity forever (for example 群青 variants
            # or a wide frame window that attached the next song title).
            "lane": record.get("discovery_lane") or record.get("lane"),
            "title_hint": record.get("title_hint"),
            "visual_song_evidence": record.get("visual_song_evidence"),
            "transient_retry_count": retry_count + (1 if transient else 0),
            "selected_repair": True,
            "retry_reason": (
                "pipeline_fingerprint_changed"
                if content_change_retry
                else "transient_infrastructure_failure"
                if infra_retry_due
                else "transient_source_context_failure"
            ),
            "resume_full_source": bool(
                "AGY_SOURCE_CONTEXT_RUNNER_FAILED" in reasons
                and isinstance(record.get("full_source_retry"), dict)
            ),
        }
        requeued.append(item)
        existing_pending.add(cid)
        state.setdefault("song_superseded_attempts", []).append(
            {
                "candidate_id": cid,
                "status": record.get("status"),
                "reason_codes": list(record.get("reason_codes") or []),
                "pipeline_fingerprint": record.get("pipeline_fingerprint"),
                "superseded_by": current,
                "retry_reason": item["retry_reason"],
            }
        )
        _remember_song_quarantine_interval(state, item)
    state["songs"] = kept
    state.setdefault("pending_song", []).extend(requeued)
    return len(requeued)


def _song_delivery_recovery_authority(
    *,
    date: str,
    outer_candidate_id: str,
    summary_path: object,
    summary_sha256: object,
    source_candidate_id: object,
    title: object,
) -> dict | None:
    """Build the exact state envelope consumed by packaging-only recovery."""

    if not (
        re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(date or ""))
        and re.fullmatch(r"[A-Za-z0-9_-]{1,96}", str(outer_candidate_id or ""))
        and isinstance(summary_path, str)
        and isinstance(summary_sha256, str)
        and re.fullmatch(r"sha256:[0-9a-f]{64}", summary_sha256)
        and re.fullmatch(r"[A-Za-z0-9_-]{1,96}", str(source_candidate_id or ""))
        and isinstance(title, str)
        and bool(title.strip())
    ):
        return None
    return {
        "schema_version": "song-delivery-recovery-authority.v1",
        "date": date,
        "outer_candidate_id": outer_candidate_id,
        "selector_summary_path": summary_path,
        "selector_summary_sha256": summary_sha256,
        "source_candidate_id": str(source_candidate_id),
        "title": title,
        "upload_enabled": False,
    }


def recover_bound_song_deliveries(date: str, state: dict) -> int:
    """Finish a verified song's packaging without recomputing its proof.

    A selector attempt records the exact summary path, hash and inner
    candidate id before delivery packaging begins.  If the process crashes or
    a later packaging-only bug is fixed, the next maintenance tick can replay
    only the deterministic manifest-last commit.  No directory glob or stale
    attempt selection is allowed.
    """

    recovered = 0
    delivered = sum(
        1
        for song in state.get("songs", [])
        if isinstance(song, dict) and bool(song.get("delivered"))
    )
    if delivered >= MAX_SONGS_PER_DATE:
        return 0
    for record in state.get("songs", []):
        if delivered >= MAX_SONGS_PER_DATE:
            break
        if (
            not isinstance(record, dict)
            or record.get("delivered")
            or record.get("verified_delivery_pending_commit") is not True
            or record.get("status") not in {"blocked", "failed"}
            or "SONG_DELIVERY_ATOMIC_COPY_FAILED"
            not in {str(code) for code in record.get("reason_codes", [])}
        ):
            continue
        cid = str(record.get("candidate_id") or "")
        summary_value = record.get("selector_summary_path")
        summary_sha256 = record.get("selector_summary_sha256")
        source_candidate_id = str(record.get("selector_record_candidate_id") or "")
        title = record.get("title")
        expected_authority = _song_delivery_recovery_authority(
            date=date,
            outer_candidate_id=cid,
            summary_path=summary_value,
            summary_sha256=summary_sha256,
            source_candidate_id=source_candidate_id,
            title=title,
        )
        if (
            expected_authority is None
            or record.get("song_delivery_recovery_authority") != expected_authority
        ):
            log(f"song delivery recovery {cid}: state authority envelope missing or drifted")
            continue
        if not (
            re.fullmatch(r"[A-Za-z0-9_-]{1,96}", cid)
            and isinstance(summary_value, str)
            and isinstance(summary_sha256, str)
            and re.fullmatch(r"[A-Za-z0-9_-]{1,96}", source_candidate_id)
            and isinstance(title, str)
            and title.strip()
            and record.get("rc") == 0
        ):
            continue
        summary_path = Path(summary_value)
        try:
            candidate_root = (BASE / "out" / date / cid).resolve(strict=True)
            summary_resolved = summary_path.resolve(strict=True)
        except OSError:
            continue
        if (
            summary_path.is_symlink()
            or not summary_resolved.is_relative_to(candidate_root)
            or summary_resolved.name != "summary.json"
            or not _matches_sha256(summary_path, summary_sha256)
        ):
            log(f"song delivery recovery {cid}: selector summary authority drifted")
            continue
        try:
            summary = _read_json_object(summary_resolved, label="bound selector summary")
        except ValueError as exc:
            log(f"song delivery recovery {cid}: {exc}")
            continue
        matches = [
            entry
            for entry in summary.get("records", [])
            if isinstance(entry, dict)
            and str(entry.get("candidate_id") or "") == source_candidate_id
        ]
        if len(matches) != 1:
            log(
                f"song delivery recovery {cid}: bound selector record is not unique "
                f"({len(matches)} match(es))"
            )
            continue
        try:
            delivery_update = _commit_verified_song_package(
                date=date,
                delivery_candidate_id=cid,
                summary_record=matches[0],
                title=title,
                selector_rc=0,
                summary_authority_root=summary_resolved.parent,
            )
        except (OSError, SongDeliveryError, ValueError) as exc:
            log(
                f"song delivery recovery {cid}: deterministic packaging refused: "
                f"{type(exc).__name__}: {exc}"
            )
            continue
        record.update(delivery_update)
        record["reason_codes"] = [
            str(code)
            for code in record.get("reason_codes", [])
            if str(code) != "SONG_DELIVERY_ATOMIC_COPY_FAILED"
        ]
        record.pop("delivery_error", None)
        record.pop("verified_delivery_pending_commit", None)
        record["status"] = "review_ready"
        record["delivery_recovered_without_selector_rerun"] = True
        record["delivery_recovered_at"] = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
        )
        recovered += 1
        delivered += 1
        log(f"song delivery recovery {cid}: committed verified package without selector rerun")
    return recovered


def bind_song_delivery_recovery_authority(
    date: str,
    state: dict,
    *,
    candidate_id: str,
    summary_path: Path,
) -> bool:
    """Explicitly bind a pre-fix verified attempt for deterministic recovery.

    Older runner versions did not persist selector-summary authority before
    packaging.  This migration never searches attempt directories: an operator
    must supply the exact summary path.  The path, bytes, unique inner record,
    complete-song proof, host identity and canonical LRC title are all verified
    before state gains the three fields consumed by
    ``recover_bound_song_deliveries``.
    """

    matches = [
        record
        for record in state.get("songs", [])
        if isinstance(record, dict)
        and str(record.get("candidate_id") or "") == candidate_id
    ]
    if len(matches) != 1:
        raise SongDeliveryError(
            f"song recovery backfill requires one state record, found {len(matches)}"
        )
    state_record = matches[0]
    if (
        state_record.get("delivered")
        or state_record.get("status") not in {"blocked", "failed"}
        or state_record.get("rc") != 0
        or "SONG_DELIVERY_ATOMIC_COPY_FAILED"
        not in {str(code) for code in state_record.get("reason_codes", [])}
    ):
        raise SongDeliveryError("song recovery backfill state is not packaging-failure eligible")
    if summary_path.is_symlink():
        raise SongDeliveryError("song recovery backfill summary may not be a symlink")
    try:
        candidate_root = (BASE / "out" / date / candidate_id).resolve(strict=True)
        summary_resolved = summary_path.resolve(strict=True)
    except OSError as exc:
        raise SongDeliveryError(f"song recovery backfill path is missing: {exc}") from exc
    if (
        summary_resolved.name != "summary.json"
        or not summary_resolved.is_relative_to(candidate_root)
        or not summary_resolved.is_file()
    ):
        raise SongDeliveryError("song recovery backfill summary escapes the outer candidate")
    summary_sha256 = "sha256:" + _sha256_regular_file(summary_resolved)
    summary = _read_json_object(summary_resolved, label="song recovery backfill summary")
    records = [entry for entry in summary.get("records", []) if isinstance(entry, dict)]
    if len(records) != 1:
        raise SongDeliveryError(
            f"song recovery backfill requires one selector record, found {len(records)}"
        )
    summary_record = records[0]
    completion = song_completion_evidence(summary_record)
    if not song_delivery_ok(
        0,
        record_is_song(summary_record),
        summary_record.get("reason_codes", []),
        completion,
    ):
        raise SongDeliveryError("song recovery backfill proof chain is not delivery-ready")
    job = summary_record.get("source_context_job")
    boundary = job.get("song_boundary") if isinstance(job, dict) else None
    canonical_song_title = boundary.get("song_title") if isinstance(boundary, dict) else None
    title = verified_song_fallback_title(canonical_song_title, state_record.get("hook"))
    if title is None:
        raise SongDeliveryError("song recovery backfill has no canonical LRC-bound title")
    source_candidate_id = str(summary_record.get("candidate_id") or "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,96}", source_candidate_id):
        raise SongDeliveryError("song recovery backfill inner candidate id is unsafe")

    intended = {
        "selector_summary_path": str(summary_resolved),
        "selector_summary_sha256": summary_sha256,
        "selector_record_candidate_id": source_candidate_id,
    }
    existing = {
        key: state_record.get(key)
        for key in intended
        if state_record.get(key) is not None
    }
    if existing and existing != intended:
        raise SongDeliveryError("song recovery backfill conflicts with existing state authority")
    changed = (
        any(state_record.get(key) != value for key, value in intended.items())
        or state_record.get("verified_delivery_pending_commit") is not True
    )
    state_record.update(intended)
    state_record["verified_delivery_pending_commit"] = True
    state_record["title"] = title
    recovery_authority = _song_delivery_recovery_authority(
        date=date,
        outer_candidate_id=candidate_id,
        summary_path=str(summary_resolved),
        summary_sha256=summary_sha256,
        source_candidate_id=source_candidate_id,
        title=title,
    )
    if recovery_authority is None:  # defensive: all fields were validated above
        raise SongDeliveryError("song recovery backfill authority envelope is invalid")
    state_record["song_delivery_recovery_authority"] = recovery_authority
    state_record["selector_summary_authority_backfill"] = {
        "schema_version": "song-selector-summary-authority-backfill.v1",
        "path": str(summary_resolved),
        "sha256": summary_sha256,
        "source_candidate_id": source_candidate_id,
        "title": title,
        "upload_enabled": False,
        "bound_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    return changed


def requeue_recoverable_talks(date: str, state: dict) -> int:
    """Retry undelivered selected talks with bounded generation semantics.

    Boundary failures wake on a relevant pipeline change.  A generic producer
    failure additionally gets one same-fingerprint retry so a transient CPA or
    worker crash cannot permanently lose an already-selected candidate.  All
    retries share one per-candidate lifetime cap.
    """

    existing_pending = {
        str(item.get("cid") or item.get("candidate_id") or "")
        for item in state.get("pending_talk", [])
        if isinstance(item, dict)
    }
    kept: list[dict] = []
    requeued: list[dict] = []
    for record in state.get("picks", []):
        if not isinstance(record, dict) or record.get("status") not in {
            "boundary_unrepairable",
            "speaker_review_required",
            "failed",
        }:
            kept.append(record)
            continue
        cid = str(record.get("candidate_id") or record.get("cid") or "")
        try:
            current = talk_pipeline_fingerprint(cid)
            current_recovery = talk_failure_recovery_fingerprint(
                record.get("failure_kind"), cid
            )
        except ValueError:
            kept.append(record)
            continue
        retry_count = int(record.get("talk_repair_retry_count") or 0)
        transient_count = int(record.get("talk_transient_retry_count") or 0)
        recorded_recovery = record.get("failure_recovery_fingerprint") or record.get(
            "pipeline_fingerprint"
        )
        changed = recorded_recovery != current_recovery
        next_retry_at = record.get("next_retry_at_epoch")
        infrastructure_retry = bool(
            record.get("failure_recoverable") is True
            and (
                not isinstance(next_retry_at, (int, float))
                or isinstance(next_retry_at, bool)
                or time.time() >= float(next_retry_at)
            )
        )
        transient = (
            record.get("status") == "failed" and transient_count < 1
        ) or infrastructure_retry
        if (
            not cid
            or cid in existing_pending
            or not (changed or transient)
            or (
                retry_count >= TALK_REPAIR_LIFETIME_RETRY_CAP
                and not infrastructure_retry
                and not changed
            )
        ):
            kept.append(record)
            continue
        segment_name = Path(str(record.get("segment") or record.get("segment_path") or "")).name
        segment = REC_ROOT / date / segment_name
        start_ms, end_ms = record.get("start_ms"), record.get("end_ms")
        if (
            not segment_name
            or not segment.is_file()
            or isinstance(start_ms, bool)
            or not isinstance(start_ms, int)
            or isinstance(end_ms, bool)
            or not isinstance(end_ms, int)
            or start_ms >= end_ms
        ):
            kept.append(record)
            continue
        seg_dur = ffprobe_ms(segment)
        item = {
            "cid": cid,
            "segment_path": str(segment),
            "seg_dur_ms": seg_dur,
            "start_ms": start_ms,
            "end_ms": min(seg_dur, end_ms) if seg_dur else end_ms,
            "xml": str(xml) if (xml := find_danmaku_xml(segment)) else None,
            "chat_jsonl": str(chat) if (chat := find_chat_jsonl(segment)) else None,
            "hook": record.get("hook", ""),
            "confidence": record.get("confidence"),
            "lane": record.get("lane", ""),
            "preview": record.get("preview", ""),
            "selected_repair": True,
            "talk_repair_retry_count": retry_count + 1,
            "talk_transient_retry_count": transient_count + (1 if transient and not changed else 0),
            "retry_reason": (
                "pipeline_fingerprint_changed"
                if changed
                else "transient_infrastructure_failure"
                if infrastructure_retry
                else "transient_produce_failure"
            ),
            "bcut_srt_path": str(BASE / "cache" / date / f"{segment.stem}.bcut.srt"),
        }
        requeued.append(item)
        existing_pending.add(cid)
        state.setdefault("talk_superseded_attempts", []).append(
            {
                "candidate_id": cid,
                "status": record.get("status"),
                "pipeline_fingerprint": record.get("pipeline_fingerprint"),
                "superseded_by": current,
                "failure_recovery_fingerprint": record.get(
                    "failure_recovery_fingerprint"
                ),
                "superseded_recovery_fingerprint": current_recovery,
                "talk_repair_retry_count": retry_count,
                "talk_transient_retry_count": transient_count,
                "retry_reason": item["retry_reason"],
                "failure_kind": record.get("failure_kind"),
                "failure_stage": record.get("failure_stage"),
                "failure_fingerprint": record.get("failure_fingerprint"),
            }
        )
    state["picks"] = kept
    state.setdefault("pending_talk", []).extend(requeued)
    return len(requeued)


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
