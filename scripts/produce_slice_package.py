#!/usr/bin/env python3
"""Produce a finished Li Dousha talk-slice package from explicit window specs.

This is the standard "圈中候选→出成品" driver (Ivan 2026-07-04). Its core
contract is the TOPIC-CLOSURE boundary rule: a clip must end where the topic
lands, on a COMPLETE sentence — never mid-sentence, never mid-story.

Flow: remote accurate piece cuts (supports cross-segment stitching) → local
concat → fresh whole-window transcription (glossary+danmaku+screen text) →
sentence-snap the final end to a transcription cue boundary near the semantic
target (fail closed if none) → VAD boundary audit + deterministic red flags →
SELF-REPAIR loop (Ivan 2026-07-10: flags move the cut to the next verifiably
clean sentence end / pull the opening onto the straddled sentence; an
unrepairable boundary fails closed — no quarantine state) → final accurate cut
→ VAD-sanitized text-final subtitles → speaker finalization → colour ASS burn
→ title/cover staging → flat delivery copy to lidousha/<date>/.

Spec JSON:
{
  "candidate_id": "...",
  "date": "2026-07-02",
  "output_root": "reports/.../finals",
  "delivery_name": "买弹幕梗当场拆台",
  "given_title": null,                      # Ivan-given title is verbatim-final
  "lead_pad_ms": 300,
  "pieces": [                                # concatenated in order
    {"remote_media": "<abs path on free>", "start_ms": ..., "end_ms": ...,
     "danmaku_xml_local": "<local path>"}
  ],
  "semantic_end_ms": <absolute ms in the LAST piece's segment timeline>
}
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.run_auto_review_shadow_pipeline import (
    _accurate_reencode_recut_command,
    _burn_preview_subtitles,
    _sha256,
    _stage_publish_draft,
    _write_source_range_srt,
)
from scripts.run_full_session_selector_cpa_shadow import (
    _build_aggregate_asr_transcriber,
    _build_ssh_agy_transcribe_runner,
)
from scripts.apply_subtitle_text_overrides import apply_document as apply_text_override_document
from scripts.apply_speaker_turn_overrides import SPEAKER_SUBTITLE_STYLE_ID
from src.autoslice.danmaku_evidence import DanmakuItem, load_danmaku_xml
from src.autoslice.jingting_chunker import parse_srt_cues
from src.autoslice.llm_client import LlmConfig, build_llm_call
from src.autoslice.review_evidence import SourceCue
from src.autoslice.subtitle_timing_qa import build_ssh_silero_vad_provider, sanitize_cue_timing

SNAP_BEFORE_MS = 6_000
SNAP_AFTER_MS = 9_000
SC_PRE_CONTEXT_MS = 900_000  # include SCs up to 15min before the clip: she CLEARS THE
                             # SC BACKLOG in batches, reading SCs minutes after they
                             # appeared (《想要成为真正的拉拉》SC was read ~4min later),
                             # so "recent" is not enough — content-match picks the right
                             # one out of the backlog, irrelevant ones are ignored.


def _load_superchats(jsonl_path: Path) -> list[tuple[int, str, str]]:
    """(video_relative_ms, sender_uname, message) for SUPER_CHAT events in a blrec
    .jsonl event log.  SUPER_CHAT carries the EXACT full sender uname (unlike gift
    events, whose uname is masked to 小***), so both the name and the on-screen
    text she reads/thanks are recoverable ground truth.  Video-relative time uses
    the earliest event's send_time as t=0 (same as the danmaku XML offsets).
    De-duplicates the CN/JPN twin events by message text."""
    if not jsonl_path.is_file():
        return []

    def _send_time(d: dict) -> float | None:
        st = d.get("send_time")
        if not isinstance(st, (int, float)):
            st = (d.get("data") or {}).get("send_time")
        return st if isinstance(st, (int, float)) else None

    raw: list[tuple[float, str, str]] = []
    base: float | None = None
    for line in jsonl_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        st = _send_time(d)
        if st is not None:
            base = st if base is None else min(base, st)
        if str(d.get("cmd", "")).startswith("SUPER_CHAT_MESSAGE") and st is not None:
            data = d.get("data") or {}
            msg = data.get("message")
            uname = (data.get("user_info") or {}).get("uname") or ""
            if isinstance(msg, str) and msg.strip():
                raw.append((st, str(uname).strip(), msg.strip()))
    if base is None:
        return []
    out: list[tuple[int, str, str]] = []
    seen: set[str] = set()
    for st, uname, msg in raw:
        if msg in seen:
            continue
        seen.add(msg)
        out.append((int(st - base), uname, msg))
    return out
START_SNAP_MS = 2_500
TAIL_PAD_MS = 400
LEAD_AIR_MS = 250
REFINE_IF_OFF_BY_MS = 2_500
REFINE_IF_CUE_LONGER_MS = 10_000


def snap_end_to_sentence(cue_ends_ms: list[int], target_ms: int) -> int | None:
    """Nearest transcription cue end to the semantic target — the cut must sit
    on a complete-sentence boundary or nowhere."""

    candidates = [end for end in cue_ends_ms if target_ms - SNAP_BEFORE_MS <= end <= target_ms + SNAP_AFTER_MS]
    if not candidates:
        return None
    return min(candidates, key=lambda end: abs(end - target_ms))


def snap_start_to_sentence(cue_starts_ms: list[int], target_ms: int) -> int | None:
    """Nearest sentence START to the intended opening — the clip must open on
    a complete sentence, with a little lead air, or nowhere."""

    candidates = [s for s in cue_starts_ms if abs(s - target_ms) <= START_SNAP_MS]
    if not candidates:
        return None
    return min(candidates, key=lambda s: abs(s - target_ms))


def needs_tail_refinement(cues, *, snapped_end: int | None, target_ms: int) -> bool:
    """A run-on cue straddling the target, or a snap far off target, means the
    coarse cue grid cannot place the closure — do a fine micro-pass."""

    if snapped_end is None or abs(snapped_end - target_ms) > REFINE_IF_OFF_BY_MS:
        return True
    straddling = next((c for c in cues if c.start_ms < target_ms < c.end_ms), None)
    return bool(straddling and (straddling.end_ms - straddling.start_ms) > REFINE_IF_CUE_LONGER_MS)


def boundary_audit(spans, *, start_ms: int, cut_ms: int, start_snapped: bool, end_snapped: bool) -> dict:
    """Both cuts must sit on sentence boundaries; the audio at each cut is
    recorded honestly (speech flowing through a sentence-boundary cut is
    allowed, an unsnapped cut is not)."""

    crossing = next((s for s in spans if s.start_ms < cut_ms < s.end_ms), None)
    verdict = "ok_sentence_boundary_cut"
    if not start_snapped:
        verdict = "start_not_on_sentence_boundary"
    elif not end_snapped:
        verdict = "end_not_on_sentence_boundary"
    return {
        "start_on_sentence_boundary": start_snapped,
        "end_on_sentence_boundary": end_snapped,
        "end_cut_inside_speech_island": bool(crossing),
        "end_island_continues_ms": (crossing.end_ms - cut_ms) if crossing else 0,
        "verdict": verdict,
    }


ISLAND_CONTINUES_FLAG_MS = 1_500


def _norm_cue_text(text: str) -> str:
    return "".join(str(text).split())


def boundary_red_flags(
    *,
    audit: dict,
    cues,
    sanitized,
    final_start_ms: int,
    final_end_ms: int,
    snapped_end_ms: int,
    closure_text: str,
) -> list[str]:
    """Deterministic boundary red flags (2026-07-09 external audit).

    A green boundary verdict only says both cuts SNAPPED to ASR cue boundaries;
    the real 7/6+7/9 deliveries showed that is not enough (cuts inside a still-
    running speech island, final SRT ending on a different sentence than the
    claimed closure, next-topic text flashing in the tail pad).  These checks
    need no LLM.  Policy (Ivan 2026-07-10): an unattended pipeline REPAIRS what
    its own auditors detect — flags drive the deterministic self-repair loop in
    main(); a clip that cannot be repaired fails closed (no delivery).  There
    is no deliver-and-ask-a-human quarantine state."""
    flags: list[str] = []
    continues_ms = int(audit.get("end_island_continues_ms") or 0)
    if continues_ms >= ISLAND_CONTINUES_FLAG_MS:
        flags.append(f"speech_continues_{continues_ms}ms_after_cut")
    if any(c.start_ms < final_start_ms - 50 and c.end_ms > final_start_ms + 300 for c in cues):
        flags.append("opens_mid_sentence")
    if any(snapped_end_ms <= c.start_ms < final_end_ms for c in cues):
        flags.append("next_sentence_enters_tail_pad")
    if sanitized and _norm_cue_text(sanitized[-1].text) != _norm_cue_text(closure_text):
        flags.append("closure_not_final_subtitle")
    return flags


BOUNDARY_REPAIR_EXTEND_CAP_MS = 25_000
MAX_BOUNDARY_REPAIRS = 3


def next_clean_closure(cues, spans, *, after_ms: int, padded_dur_ms: int,
                       cap_ms: int = BOUNDARY_REPAIR_EXTEND_CAP_MS) -> int | None:
    """Earliest LATER sentence end whose cut raises no deterministic red flag:
    nothing starts inside its tail pad and no speech island runs
    ≥ ISLAND_CONTINUES_FLAG_MS past the cut.

    This is the unattended repair move (Ivan 2026-07-10): extend FORWARD to
    where the talk actually lands — never retract, which would drop the very
    content the pick was chosen for.  Bounded by ``cap_ms`` (beyond that she is
    mid-monologue and the clip fails closed instead)."""
    ends = sorted({c.end_ms for c in cues if after_ms < c.end_ms <= after_ms + cap_ms})
    for end in ends:
        cut = min(padded_dur_ms, end + TAIL_PAD_MS)
        if any(end <= c.start_ms < cut for c in cues):
            continue
        crossing = next((s for s in spans if s.start_ms < cut < s.end_ms), None)
        if crossing and crossing.end_ms - cut >= ISLAND_CONTINUES_FLAG_MS:
            continue
        return end
    return None


def repair_start_for_straddler(cues, *, final_start_ms: int) -> int | None:
    """Repair an ``opens_mid_sentence`` flag by opening on the straddling
    sentence's own start — include the whole sentence rather than slicing into
    it.  Returns that sentence's start_ms (the new snapped start; the caller
    re-derives final_start with lead air), or None when no cue straddles the
    opening."""
    straddler = next(
        (c for c in cues if c.start_ms < final_start_ms - 50 and c.end_ms > final_start_ms + 300),
        None,
    )
    if straddler is None:
        return None
    return straddler.start_ms


def run(cmd: list[str], *, timeout: int = 3600) -> None:
    completed = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=timeout)
    if completed.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed rc={completed.returncode}: {completed.stderr[-400:]}")


def ffprobe_duration_ms(path: Path) -> int:
    completed = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return int(float(completed.stdout.strip()) * 1000)


def _validated_burned_artifact(record: dict) -> Path:
    """Return the exact burn bound into the record, never a directory glob."""

    preview = record.get("burned_preview")
    if not isinstance(preview, dict) or preview.get("status") != "BURNED":
        raise RuntimeError(f"FINAL_SUBTITLE_BURN_NOT_READY: {preview}")
    value = preview.get("path")
    burned = Path(str(value)) if value else None
    if burned is None or not burned.is_file():
        raise RuntimeError(f"FINAL_SUBTITLE_BURN_MISSING: {value}")
    actual = "sha256:" + _sha256(burned)
    expected_preview = preview.get("burned_sha256")
    expected_record = (record.get("artifact_hashes") or {}).get("burned_video_sha256")
    if expected_preview != actual or expected_record != actual:
        raise RuntimeError(
            f"FINAL_SUBTITLE_BURN_HASH_MISMATCH: preview={expected_preview} "
            f"record={expected_record} actual={actual}"
        )
    return burned


def _resolved_optional_path(value: object, *, relative_to: Path) -> Path | None:
    if not isinstance(value, (str, Path)) or not str(value):
        return None
    path = Path(value)
    return path if path.is_absolute() else (relative_to / path).resolve()


def _rebase_remote_speaker_manifest(
    manifest: dict,
    *,
    host: str,
    media_path: Path,
    text_srt_path: Path,
    override_path: Path | None,
    output_srt_path: Path,
    output_ass_path: Path,
) -> dict:
    """Replace deleted /tmp paths while retaining their execution provenance."""

    rebased = dict(manifest)
    path_keys = (
        "source_media",
        "text_final_srt",
        "speaker_override",
        "output_review_srt",
        "output_ass",
    )
    rebased["runtime_host"] = host
    rebased["ephemeral_runtime_paths"] = {key: manifest.get(key) for key in path_keys}
    rebased.update(
        {
            "source_media": str(media_path.resolve()),
            "text_final_srt": str(text_srt_path.resolve()),
            "speaker_override": str(override_path.resolve()) if override_path is not None else None,
            "output_review_srt": str(output_srt_path.resolve()),
            "output_ass": str(output_ass_path.resolve()),
        }
    )
    return rebased


def run_speaker_finalizer(
    *,
    host: str,
    candidate_id: str,
    media_path: Path,
    text_srt_path: Path,
    output_srt_path: Path,
    output_ass_path: Path,
    output_manifest_path: Path,
    work_dir: Path,
    override_path: Path | None = None,
    speaker_python: Path = Path("/opt/bilive/autoslice/venv-diar/bin/python"),
    reference_dir: Path = Path("/opt/bilive/autoslice/voiceprints/lidousha"),
    model_dir: Path = Path("/opt/bilive/autoslice/models/campp"),
) -> dict:
    """Run the pinned speaker runtime locally on free or through a remote temp.

    The command consumes the already-final text SRT.  Outputs are accepted only
    when the manifest says READY and every returned artifact hash matches.
    """

    safe_cid = re.sub(r"[^A-Za-z0-9_.-]+", "_", candidate_id)[:80]
    local_host = host in {"localhost", "127.0.0.1", "::1"}
    profile = ROOT / "assets" / "lidousha" / "voiceprint_profile.v1.json"
    output_srt_path.parent.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)
    # Never accept stale artifacts from a previous successful attempt if the
    # current runtime exits without replacing one of them.
    for stale in (output_srt_path, output_ass_path, output_manifest_path):
        stale.unlink(missing_ok=True)
    if local_host:
        command = [
            str(speaker_python), "-m", "src.autoslice.speaker_finalizer",
            "--media", str(media_path), "--text-srt", str(text_srt_path),
            "--profile", str(profile), "--reference-dir", str(reference_dir),
            "--model-dir", str(model_dir), "--output-srt", str(output_srt_path),
            "--output-ass", str(output_ass_path), "--output-manifest", str(output_manifest_path),
            "--work-dir", str(work_dir),
        ]
        if override_path is not None:
            command.extend(["--overrides", str(override_path)])
        completed = subprocess.run(
            command, cwd=str(ROOT), check=False, capture_output=True, text=True, timeout=1800
        )
    else:
        remote_dir = f"/tmp/autoslice-speaker-{safe_cid}-{os.getpid()}"
        remote_media = f"{remote_dir}/media.mp4"
        remote_srt = f"{remote_dir}/text-final.srt"
        remote_output_srt = f"{remote_dir}/speaker-final.srt"
        remote_output_ass = f"{remote_dir}/speaker-final.ass"
        remote_manifest = f"{remote_dir}/speaker-final.json"
        remote_override = f"{remote_dir}/overrides.json"
        run(["ssh", host, f"rm -rf {shlex.quote(remote_dir)} && mkdir -p {shlex.quote(remote_dir)}/work"], timeout=120)
        try:
            run(["scp", "-q", str(media_path), str(text_srt_path), f"{host}:{remote_dir}/"], timeout=1800)
            # scp preserves local basenames, normalize to fixed remote names.
            remote_setup = (
                f"mv {shlex.quote(remote_dir + '/' + media_path.name)} {shlex.quote(remote_media)}; "
                f"mv {shlex.quote(remote_dir + '/' + text_srt_path.name)} {shlex.quote(remote_srt)}"
            )
            run(["ssh", host, remote_setup], timeout=120)
            if override_path is not None:
                run(["scp", "-q", str(override_path), f"{host}:{remote_override}"], timeout=120)
            remote_command = [
                str(speaker_python), "-m", "src.autoslice.speaker_finalizer",
                "--media", remote_media, "--text-srt", remote_srt,
                "--profile", "/opt/bilive/autoslice/repo/assets/lidousha/voiceprint_profile.v1.json",
                "--reference-dir", str(reference_dir), "--model-dir", str(model_dir),
                "--output-srt", remote_output_srt, "--output-ass", remote_output_ass,
                "--output-manifest", remote_manifest, "--work-dir", f"{remote_dir}/work",
            ]
            if override_path is not None:
                remote_command.extend(["--overrides", remote_override])
            shell_command = "cd /opt/bilive/autoslice/repo && " + " ".join(
                shlex.quote(part) for part in remote_command
            )
            completed = subprocess.run(
                ["ssh", host, shell_command], check=False, capture_output=True, text=True, timeout=1800
            )
            if completed.returncode == 0:
                for remote_source, local_target in (
                    (remote_output_srt, output_srt_path),
                    (remote_output_ass, output_ass_path),
                    (remote_manifest, output_manifest_path),
                ):
                    run(["scp", "-q", f"{host}:{remote_source}", str(local_target)], timeout=600)
        finally:
            subprocess.run(["ssh", host, f"rm -rf {shlex.quote(remote_dir)}"], check=False, timeout=120)
    if completed.returncode != 0 or not output_manifest_path.is_file():
        raise RuntimeError(
            "SPEAKER_FINALIZATION_FAILED: " + (completed.stderr or completed.stdout)[-1200:]
        )
    manifest = json.loads(output_manifest_path.read_text(encoding="utf-8"))
    if not local_host:
        manifest = _rebase_remote_speaker_manifest(
            manifest,
            host=host,
            media_path=media_path,
            text_srt_path=text_srt_path,
            override_path=override_path,
            output_srt_path=output_srt_path,
            output_ass_path=output_ass_path,
        )
        output_manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    if manifest.get("status") != "READY" or manifest.get("production_ready") is not True:
        raise RuntimeError(f"SPEAKER_FINALIZATION_BLOCKED: {manifest.get('reason')}")
    expected = {
        output_srt_path: manifest.get("output_review_srt_sha256"),
        output_ass_path: manifest.get("output_ass_sha256"),
    }
    for path, digest in expected.items():
        if not path.is_file() or digest != _sha256(path):
            raise RuntimeError(f"SPEAKER_FINALIZATION_HASH_MISMATCH: {path}")
    if manifest.get("source_media_sha256") != _sha256(media_path):
        raise RuntimeError("SPEAKER_FINALIZATION_MEDIA_BINDING_MISMATCH")
    if manifest.get("text_final_srt_sha256") != _sha256(text_srt_path):
        raise RuntimeError("SPEAKER_FINALIZATION_TEXT_BINDING_MISMATCH")
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--ssh-host", default="free")
    parser.add_argument(
        "--substrate",
        choices=("aggregate_asr", "agy_fresh"),
        default="aggregate_asr",
        help="subtitle substrate: aggregate_asr = free ASR (bcut/jianying, accurate ms timeline) + text-only correction (default); agy_fresh = legacy agy whole-window transcription.",
    )
    parser.add_argument(
        "--speaker-mode",
        choices=("required",),
        default="required",
        help="talk speaker finalization is required and fails closed",
    )
    parser.add_argument("--subtitle-text-overrides", type=Path, help="hash-bound human text decisions applied before speaker inference")
    parser.add_argument("--speaker-overrides", type=Path, help="hash-bound reviewed turn/split/overlap decisions applied after automatic speaker inference")
    parser.add_argument(
        "--speaker-python",
        type=Path,
        default=Path(os.environ.get("AUTOSLICE_SPEAKER_PYTHON", "/opt/bilive/autoslice/venv-diar/bin/python")),
    )
    parser.add_argument(
        "--correct",
        choices=("bcut_agy_cpa", "cpa", "agy", "none"),
        default="bcut_agy_cpa",
        help="correction: bcut_agy_cpa = BCUT draft + AGY refine (hears audio) + CPA reconcile (default, best quality); cpa = CPA text-only (fast, blind to audio); agy = AGY refine only; none = raw BCUT.",
    )
    parser.add_argument(
        "--screen-text",
        action="store_true",
        help="cpa correct only: also feed agy-extracted on-screen text (superchat cards/titles) to CPA. Off by default — the glossary is the reliable authority for known names; agy vision on stylized cards is unreliable and can override the glossary. Use only when a clip's meaning hinges on on-screen text NOT yet in the glossary.",
    )
    parser.add_argument(
        "--reuse-cover",
        action="store_true",
        help="subtitle-only re-run: keep the EXISTING delivered cover, skip the AI cover (art-direction LLM + gpt-image-2 ~90s/clip). Title still regenerates. Use when re-correcting subtitles on an already-covered clip.",
    )
    args = parser.parse_args(argv)
    spec = json.loads(args.spec.read_text(encoding="utf-8"))

    cid = spec["candidate_id"]
    out_root = Path(spec["output_root"]) / cid
    out_root.mkdir(parents=True, exist_ok=True)
    host = args.ssh_host

    # 1. Remote accurate piece cuts (production encode params), pull local.
    piece_paths: list[Path] = []
    for index, piece in enumerate(spec["pieces"]):
        local = out_root / f"piece_{index}_{piece['start_ms']}_{piece['end_ms']}.mp4"
        if not local.exists():
            remote_tmp = f"/tmp/produce_{cid}_{index}.mp4"
            cmd = _accurate_reencode_recut_command(
                source_video=Path(piece["remote_media"]),
                output_media=Path(remote_tmp),
                start_ms=piece["start_ms"],
                duration_ms=piece["end_ms"] - piece["start_ms"],
            )
            run(["ssh", host, " ".join(shlex.quote(str(part)) for part in cmd)], timeout=3600)
            run(["scp", "-q", f"{host}:{remote_tmp}", str(local)], timeout=1800)
            run(["ssh", host, f"rm -f {shlex.quote(remote_tmp)}"], timeout=60)
        piece_paths.append(local)
    durations = [ffprobe_duration_ms(p) for p in piece_paths]

    padded = out_root / f"padded_{spec['pieces'][0]['start_ms']}.mp4"
    if len(piece_paths) == 1:
        if not padded.exists():
            run(["cp", str(piece_paths[0]), str(padded)])
    elif not padded.exists():
        concat_list = out_root / "concat.txt"
        concat_list.write_text("".join(f"file '{p.resolve()}'\n" for p in piece_paths), encoding="utf-8")
        run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
             "-i", str(concat_list), "-c", "copy", str(padded)])
    padded_dur = ffprobe_duration_ms(padded)

    # 2. Danmaku + on-screen SUPER_CHATs merged onto the concat timeline.
    merged: list[DanmakuItem] = []
    offset = 0
    for piece, dur in zip(spec["pieces"], durations):
        xml = piece.get("danmaku_xml_local")
        if xml and Path(xml).is_file():
            for item in load_danmaku_xml(Path(xml)):
                rel = item.offset_ms - piece["start_ms"]
                if -1_000 <= rel <= dur + 1_000:
                    merged.append(DanmakuItem(offset_ms=offset + max(0, rel), text=item.text))
            # On-screen superchats (exact text from the blrec .jsonl sibling) — the
            # audio-only ASR garbles what she reads off an SC card, so feed the SC
            # text as strong wording context (Ivan 2026-07-07: 有些地方要结合画面SC).
            jsonl = piece.get("superchat_jsonl_local") or str(Path(xml).with_suffix(".jsonl"))
            for sc_ms, sc_uname, sc_text in _load_superchats(Path(jsonl)):
                rel = sc_ms - piece["start_ms"]
                # she reads/thanks SCs a while after they appear and BATCHES them
                # (Ivan 2026-07-07), so include SCs from up to ~2min BEFORE the clip —
                # an opening thank often clears older SCs.  Pre-clip ones are marked
                # (此前) and placed at the clip start; content-match decides the pairing.
                if -SC_PRE_CONTEXT_MS <= rel <= dur + 1_000:
                    marker = f"·{sc_uname}" if sc_uname else ""
                    prefix = "【SC此前" if rel < 0 else "【SC"
                    label = f"{prefix}{marker}】{sc_text}"
                    merged.append(DanmakuItem(offset_ms=offset + max(0, rel), text=label))
        offset += dur
    merged.sort(key=lambda item: item.offset_ms)

    # 3. Fresh transcription of the padded window.
    if args.substrate == "aggregate_asr":
        transcriber = _build_aggregate_asr_transcriber(
            host, danmaku_items=merged or None, window_start_ms=0, source_video=padded, correct=args.correct, screen_text=args.screen_text
        )
    else:
        transcriber = _build_ssh_agy_transcribe_runner(host, danmaku_items=merged or None, window_start_ms=0)
    vad = build_ssh_silero_vad_provider(host)
    spans = vad(padded, 0, padded_dur)
    srt_text = transcriber(padded, [(s.start_ms, s.end_ms) for s in spans])
    (out_root / "padded.fresh.srt").write_text(srt_text, encoding="utf-8")
    cues = [c for c in parse_srt_cues(srt_text) if c.text.strip()]
    if len(cues) < 3:
        raise SystemExit("FRESH_TRANSCRIPTION_TOO_SPARSE")

    # 4a. Sentence-snap the START (the clip must open on a sentence).
    first_piece = spec["pieces"][0]
    target_start_rel = spec.get("semantic_start_ms", first_piece["start_ms"]) - first_piece["start_ms"]
    snapped_start = snap_start_to_sentence([c.start_ms for c in cues], target_start_rel)
    final_start = max(0, (snapped_start if snapped_start is not None else target_start_rel) - LEAD_AIR_MS)

    # 4b. Sentence-snap the END; a run-on cue near the closure triggers a
    #     fine-grained micro re-transcription of the tail so the closure
    #     sentence gets its own boundary.
    last_piece = spec["pieces"][-1]
    target_rel = sum(durations[:-1]) + (spec["semantic_end_ms"] - last_piece["start_ms"])
    snapped = snap_end_to_sentence([c.end_ms for c in cues], target_rel)
    refinement_used = False
    if needs_tail_refinement(cues, snapped_end=snapped, target_ms=target_rel):
        refinement_used = True
        refine_start = max(0, target_rel - 20_000)
        refine_end = min(padded_dur, target_rel + 15_000)
        tail_clip = out_root / "tail_refine.mp4"
        run(_accurate_reencode_recut_command(source_video=padded, output_media=tail_clip, start_ms=refine_start, duration_ms=refine_end - refine_start))
        tail_srt = transcriber(tail_clip, None)
        (out_root / "tail_refine.fresh.srt").write_text(tail_srt, encoding="utf-8")
        fine = [c for c in parse_srt_cues(tail_srt) if c.text.strip()]
        fine_lifted = [
            type(c)(index=c.index, start_ms=c.start_ms + refine_start, end_ms=c.end_ms + refine_start, text=c.text)
            for c in fine
        ]
        snapped = snap_end_to_sentence([c.end_ms for c in fine_lifted], target_rel)
        if snapped is not None:
            # Splice: fine cues replace coarse cues inside the refined window.
            cues = [c for c in cues if c.end_ms <= refine_start or c.start_ms >= refine_end] + fine_lifted
            cues.sort(key=lambda c: c.start_ms)
    if snapped is None:
        raise SystemExit(
            f"NO_SENTENCE_BOUNDARY_NEAR_TARGET: target={target_rel}ms; nearest cue ends="
            f"{sorted((c.end_ms for c in cues), key=lambda e: abs(e - target_rel))[:3]}"
        )
    closure_cue = next(c for c in cues if c.end_ms == snapped)

    # 4c. Boundary self-repair loop (Ivan 2026-07-10): unattended means the
    # pipeline FIXES what its own deterministic auditors detect — there is no
    # deliver-and-ask-a-human quarantine state.  Pure computation (no ffmpeg,
    # no LLM): each pass re-audits; a flagged end moves FORWARD to the next
    # verifiably clean sentence end, a straddled opening moves back onto that
    # sentence's own start.  Unrepairable clips fail closed BEFORE any media
    # is cut.
    audit_path = out_root / f"{cid}.boundary_audit.json"
    boundary_repairs: list[dict] = []
    while True:
        final_end = min(padded_dur, snapped + TAIL_PAD_MS)
        audit = boundary_audit(
            spans,
            start_ms=final_start,
            cut_ms=final_end,
            start_snapped=snapped_start is not None,
            end_snapped=True,
        )
        audit.update(
            {
                "semantic_start_target_rel_ms": target_start_rel,
                "snapped_sentence_start_ms": snapped_start,
                "final_start_ms": final_start,
                "opening_sentence": next((c.text for c in cues if c.start_ms == snapped_start), None),
                "semantic_target_rel_ms": target_rel,
                "snapped_sentence_end_ms": snapped,
                "final_end_ms": final_end,
                "closure_sentence": closure_cue.text,
                "tail_refinement_used": refinement_used,
                "boundary_repairs": boundary_repairs,
            }
        )
        audit_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if audit["verdict"] != "ok_sentence_boundary_cut":
            raise SystemExit(f"BOUNDARY_AUDIT_FAILED: {json.dumps(audit, ensure_ascii=False)}")
        source_cues = [
            SourceCue(f"fresh_{i:04d}", max(c.start_ms, final_start), min(c.end_ms, final_end), c.text.strip(), "zh", "speech", 1.0)
            for i, c in enumerate(cues, start=1)
            if c.start_ms < final_end and c.end_ms > final_start
        ]
        sanitized, timing_qa = sanitize_cue_timing(source_cues, spans, window_start_ms=final_start, window_end_ms=final_end)
        red_flags = boundary_red_flags(
            audit=audit,
            cues=cues,
            sanitized=sanitized,
            final_start_ms=final_start,
            final_end_ms=final_end,
            snapped_end_ms=snapped,
            closure_text=closure_cue.text,
        )
        if not red_flags:
            break
        repair: dict = {}
        if len(boundary_repairs) < MAX_BOUNDARY_REPAIRS:
            if "opens_mid_sentence" in red_flags:
                new_snap_start = repair_start_for_straddler(cues, final_start_ms=final_start)
                if new_snap_start is not None and new_snap_start != snapped_start:
                    repair["snapped_start_ms"] = new_snap_start
            if any(flag != "opens_mid_sentence" for flag in red_flags):
                new_end = next_clean_closure(cues, spans, after_ms=snapped, padded_dur_ms=padded_dur)
                if new_end is not None:
                    repair["snapped_end_ms"] = new_end
        if not repair:
            audit["red_flags"] = red_flags
            audit_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            raise SystemExit(
                f"BOUNDARY_UNREPAIRABLE: {','.join(red_flags)} after {len(boundary_repairs)} repair(s); "
                f"snapped={snapped}ms target={target_rel}ms extend_cap={BOUNDARY_REPAIR_EXTEND_CAP_MS}ms"
            )
        boundary_repairs.append({"flags": red_flags, **repair})
        if "snapped_start_ms" in repair:
            snapped_start = repair["snapped_start_ms"]
            final_start = max(0, snapped_start - LEAD_AIR_MS)
        if "snapped_end_ms" in repair:
            snapped = repair["snapped_end_ms"]
            closure_cue = next(c for c in cues if c.end_ms == snapped)
    audit["red_flags"] = []
    audit_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # 5. Final accurate cut + VAD-sanitized subtitles rebased to the cut.
    recut_dir = out_root / "replacement_recuts"
    recut_dir.mkdir(exist_ok=True)
    media_path = recut_dir / f"{cid}.recut.mp4"
    run(_accurate_reencode_recut_command(source_video=padded, output_media=media_path, start_ms=final_start, duration_ms=final_end - final_start))
    subtitle_path = media_path.with_suffix(".srt")
    text_override_path = args.subtitle_text_overrides or _resolved_optional_path(
        spec.get("subtitle_text_overrides"), relative_to=args.spec.parent
    )
    text_manifest_path: Path | None = None
    if text_override_path is not None:
        automatic_text_path = media_path.with_suffix(".automatic-text.srt")
        _write_source_range_srt(sanitized, final_start, final_end, automatic_text_path)
        text_manifest_path = media_path.with_suffix(".text-finalization.json")
        apply_text_override_document(
            automatic_text_path, text_override_path, subtitle_path, text_manifest_path
        )
    else:
        _write_source_range_srt(sanitized, final_start, final_end, subtitle_path)
    (recut_dir / f"{cid}.recut.timing_qa.json").write_text(
        json.dumps(timing_qa, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    speaker_manifest: dict | None = None
    speaker_review_srt: Path | None = None
    speaker_ass: Path | None = None
    speaker_manifest_path: Path | None = None
    if args.speaker_mode == "required":
        speaker_review_srt = media_path.with_suffix(".speaker-final.srt")
        speaker_ass = media_path.with_suffix(".speaker-final.ass")
        speaker_manifest_path = media_path.with_suffix(".speaker-final.json")
        speaker_override_path = args.speaker_overrides or _resolved_optional_path(
            spec.get("speaker_overrides"), relative_to=args.spec.parent
        )
        speaker_manifest = run_speaker_finalizer(
            host=host,
            candidate_id=cid,
            media_path=media_path,
            text_srt_path=subtitle_path,
            output_srt_path=speaker_review_srt,
            output_ass_path=speaker_ass,
            output_manifest_path=speaker_manifest_path,
            work_dir=recut_dir / f"{cid}.speaker-work",
            override_path=speaker_override_path,
            speaker_python=args.speaker_python,
        )

    record: dict = {
        "status": "MATERIALIZED",
        "media_path": str(media_path),
        "subtitle_path": str(subtitle_path),
        "subtitle_source": f"{args.substrate}+{args.correct}+pronoun+text_final",
        "start_ms": 0,
        "end_ms": final_end - final_start,
        "duration_ms": final_end - final_start,
        "artifact_hashes": {
            "video_sha256": "sha256:" + _sha256(media_path),
            "subtitle_sha256": "sha256:" + _sha256(subtitle_path),
            **({"ass_sha256": "sha256:" + _sha256(speaker_ass)} if speaker_ass is not None else {}),
            **({"speaker_review_srt_sha256": "sha256:" + _sha256(speaker_review_srt)} if speaker_review_srt is not None else {}),
        },
        "text_finalization_manifest_path": str(text_manifest_path) if text_manifest_path is not None else None,
        "speaker_review_srt_path": str(speaker_review_srt) if speaker_review_srt is not None else None,
        "subtitle_ass_path": str(speaker_ass) if speaker_ass is not None else None,
        "subtitle_style": SPEAKER_SUBTITLE_STYLE_ID if speaker_ass is not None else "lidousha-final-sapphire72",
        "speaker_finalization_manifest_path": str(speaker_manifest_path) if speaker_manifest_path is not None else None,
        "speaker_finalization_manifest_sha256": ("sha256:" + _sha256(speaker_manifest_path)) if speaker_manifest_path is not None else None,
        "speaker_finalization": speaker_manifest,
        "subtitle_timing_qa": timing_qa,
        "boundary_audit": audit,
    }
    record = _burn_preview_subtitles(record, run_ffmpeg=True)
    if not isinstance(record.get("burned_preview"), dict) or record["burned_preview"].get("status") != "BURNED":
        raise SystemExit(f"FINAL_SUBTITLE_BURN_FAILED: {record.get('burned_preview')}")
    burned = _validated_burned_artifact(record)

    # 6. Title (Ivan-given verbatim, else style-asset LLM) + cover + delivery.
    given_title = spec.get("given_title")
    # Title LLM only when no manual title (iron rule: manual titles pass through
    # untouched). Cover art-direction LLM ALWAYS runs (Ivan 2026-07-04): even with
    # a hand-given title the cover still benefits from persona-fit expression /
    # layout / background; it is fail-open, so it never blocks.
    # Per-stage CPA chains (2026-07-10, Ivan): title is a single brand-critical
    # short call → gpt-5.6-sol at high effort; art direction is a structured
    # pick with a known good shape, deterministic fallback and judge guardrails
    # → gpt-5.6-luna at medium (the doc-exact luna lane).  Both fall back
    # 5.5 → 5.4.
    title_llm = None
    if not given_title:
        title_llm = build_llm_call(
            LlmConfig(transport="command", command_template="bash scripts/llm_via_cpa.sh {prompt_file} {completion_file} 'gpt-5.6-sol gpt-5.5 gpt-5.4' high", timeout_seconds=180.0)
        )
    art_direction_llm = None if args.reuse_cover else build_llm_call(
        LlmConfig(transport="command", command_template="bash scripts/llm_via_cpa.sh {prompt_file} {completion_file} 'gpt-5.6-luna gpt-5.5 gpt-5.4' medium", timeout_seconds=180.0)
    )
    final_title_cues = [
        SourceCue(
            f"text_final_{index:04d}", cue.start_ms, cue.end_ms, cue.text.strip(),
            "zh", "speech", 1.0,
        )
        for index, cue in enumerate(parse_srt_cues(subtitle_path.read_text(encoding="utf-8")), start=1)
        if cue.text.strip()
    ]
    record = _stage_publish_draft(
        record,
        candidate_id=cid,
        title=given_title or cid,
        cues=final_title_cues,
        run_ffmpeg=True,
        title_llm_call=title_llm,
        art_direction_llm_call=art_direction_llm,
        skip_cover=args.reuse_cover,
    )
    staging = record.get("publish_staging") or {}
    record_path = recut_dir / f"{cid}.record.json"
    with record_path.open("w", encoding="utf-8") as handle:
        json.dump(record, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")

    delivery = ROOT / "lidousha" / spec["date"]
    delivery.mkdir(parents=True, exist_ok=True)
    name = spec.get("delivery_name") or cid
    # Old sapphire renders may coexist in replacement_recuts; copy only the
    # exact hash-bound speaker burn made by this run.
    burned = _validated_burned_artifact(record)
    run(["cp", str(burned), str(delivery / f"{name}.mp4")])
    run(["cp", str(subtitle_path), str(delivery / f"{name}.srt")])
    for source, suffix in (
        (speaker_review_srt, ".speaker.srt"),
        (speaker_ass, ".speaker.ass"),
        (speaker_manifest_path, ".speaker.json"),
        (text_manifest_path, ".text-finalization.json"),
        (record_path, ".record.json"),
    ):
        if source is not None and source.is_file():
            run(["cp", str(source), str(delivery / f"{name}{suffix}")])
    cover = staging.get("cover_path")
    if cover and Path(cover).is_file():
        run(["cp", str(cover), str(delivery / f"{name}.cover.png")])

    print(json.dumps(
        {
            "candidate_id": cid,
            "final_end_ms": final_end,
            "closure_sentence": closure_cue.text,
            "boundary_verdict": audit["verdict"],
            "red_flags": red_flags,
            "boundary_repairs": boundary_repairs,
            "timing_qa": timing_qa.get("counts"),
            "cover_status": staging.get("cover_status"),
            "title": staging.get("title"),
            "delivery": str(delivery / f"{name}.mp4"),
            "subtitle": str(delivery / f"{name}.srt"),
            "speaker_subtitle": str(delivery / f"{name}.speaker.srt") if speaker_review_srt else None,
            "speaker_ass": str(delivery / f"{name}.speaker.ass") if speaker_ass else None,
            "speaker_status": speaker_manifest.get("status") if speaker_manifest else "OFF",
        },
        ensure_ascii=False,
        indent=2,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
