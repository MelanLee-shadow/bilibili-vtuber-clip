"""Automatic (A-class) checks of the finished-media review lane.

Every check returns a result row ``{id, status, reason_codes, detail}`` where
status is PASS, FAIL, FLAG, SKIPPED_DISABLED or SKIPPED_UNAVAILABLE.  FAIL and
FLAG are listed in findings.md for the reviewer to handle in REVIEW.md; the
lane never blocks on them.  Checks never modify the finished media or
subtitles; they only write evidence files.

Checks reuse existing library validators wherever one exists.  Provider
calls (BCUT, jianying, CPA tags) go through injectable ``Adapters`` so tests
and rehearsals replay recorded answers.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .common import (
    AUTO_CHECKS_NAME,
    AUTO_CHECKS_SCHEMA,
    EVIDENCE_DIR,
    NOMINATIONS_NAME,
    FmrError,
    canonical_sha,
    evidence_ref,
    file_sha,
    is_sha,
    load_json,
    srt_cues,
    strip_speaker_label,
    write_json,
)
from .member import package_files, member_copy_path

PASS, FAIL, FLAG = "PASS", "FAIL", "FLAG"
SKIPPED = ("SKIPPED_DISABLED", "SKIPPED_UNAVAILABLE")
# Pilot/production convention recorded in evidence (docs/pipeline/85): the
# silence probe parameters are not a verdict threshold, only a locator.
SILENCE_NOISE_DB = -38
SILENCE_MIN_S = 0.1
CONTEXT_CUES_EACH_SIDE = 3
MAX_FRAMES_PER_REASON = 5


@dataclass
class Adapters:
    """Provider and subprocess seams.  ``None`` means the production default."""

    gate: Any = None  # FinalSubtitleAudioGateAdapters (BCUT)
    jianying: Callable[[bytes], dict] | None = None
    use_jianying: bool = True
    tags: Callable[..., dict] | None = None
    run: Callable[[list[str]], subprocess.CompletedProcess] | None = None

    def run_command(self, command: list[str]) -> subprocess.CompletedProcess:
        if self.run is not None:
            return self.run(command)
        return subprocess.run(command, capture_output=True, text=True, check=False, timeout=1800)


@dataclass
class Context:
    root: Path
    cid: str
    member: dict[str, Any]
    adapters: Adapters = field(default_factory=Adapters)
    record: dict[str, Any] = field(default_factory=dict)
    nominations: dict[str, list[dict[str, Any]]] = field(default_factory=lambda: {"j1": [], "j6": []})

    @property
    def files(self) -> dict[str, Path]:
        return package_files(self.root, self.cid)

    @property
    def evidence(self) -> Path:
        path = self.root / EVIDENCE_DIR
        path.mkdir(parents=True, exist_ok=True)
        return path

    def text(self, key: str) -> str:
        return self.files[key].read_text(encoding="utf-8")


def row(check_id: str, status: str, reasons: list[str] | None = None, **detail: Any) -> dict[str, Any]:
    return {"id": check_id, "status": status, "reason_codes": sorted(set(reasons or [])), "detail": detail}


# --------------------------------------------------------------------- A1
def _source_sha(ctx: Context, path: Path) -> str:
    cache_path = ctx.evidence / "source-sha-cache.json"
    cache = load_json(cache_path) if cache_path.is_file() else {}
    stat = path.stat()
    key = str(path)
    hit = cache.get(key)
    if isinstance(hit, dict) and hit.get("size") == stat.st_size and hit.get("mtime_ns") == stat.st_mtime_ns:
        return str(hit["sha256"])
    digest = file_sha(path)
    cache[key] = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "sha256": digest}
    write_json(cache_path, cache)
    return digest


def a1_seal(ctx: Context) -> dict[str, Any]:
    reasons: list[str] = []
    files: dict[str, Any] = {}
    for key, path in ctx.files.items():
        if path.is_symlink() or not path.is_file():
            reasons.append(f"FMR_A1_FILE_MISSING_{key.upper()}")
            continue
        files[key] = {"path": path.name, "sha256": file_sha(path), "bytes": path.stat().st_size}
    member_file = member_copy_path(ctx.root, ctx.cid)
    if not member_file.is_file():
        reasons.append("FMR_A1_MEMBER_COPY_MISSING")
    else:
        files["member"] = {"path": member_file.name, "sha256": file_sha(member_file),
                           "bytes": member_file.stat().st_size}
    pieces: list[dict[str, Any]] = []
    for piece in ctx.member["source"]["pieces"]:
        src = Path(piece["path"])
        if src.is_symlink() or not src.is_file():
            reasons.append("FMR_A1_SOURCE_MISSING")
            continue
        pieces.append({
            "source_media_sha256": _source_sha(ctx, src),
            "source_start_ms": piece["start_ms"],
            "source_end_ms": piece["end_ms"],
            "source_name": src.name,
        })
    intro_receipt_ok = True
    try:
        load_json(ctx.files["intro_receipt"])
    except FmrError:
        intro_receipt_ok = False
        reasons.append("FMR_A1_INTRO_RECEIPT_UNREADABLE")
    if not reasons:
        ctx.record["pieces"] = pieces
    return row("A1", FAIL if reasons else PASS, reasons, files=files, pieces=pieces,
               intro_receipt_readable=intro_receipt_ok)


# --------------------------------------------------------------------- A2
def _visual_constants():
    from scripts import audit_review_package as audit

    return audit


def srt_shape_problems(path: Path) -> list[str]:
    """Release validation plus the line/length/static-cue contract of the audit."""
    from src.autoslice.subtitle_validation import validate_srt_file

    audit = _visual_constants()
    problems: list[str] = []
    verdict = validate_srt_file(path)
    if verdict.get("status") != "PASS":
        problems.extend(
            str(err.get("code") or "SRT_RELEASE_VALIDATION_FAILED")
            for err in verdict.get("errors") or [] if isinstance(err, dict)
        ) or problems.append("SRT_RELEASE_VALIDATION_FAILED")
    for cue in audit._parse_srt(path):
        lines = [line.strip() for line in cue["lines"] if line.strip()]
        if len(lines) > audit.DEFAULT_MAX_VISUAL_LINES:
            problems.append("SUBTITLE_CUE_TOO_MANY_LINES")
        if any(audit._text_len(line) > audit.DEFAULT_MAX_VISUAL_LINE_CHARS for line in lines):
            problems.append("SUBTITLE_LINE_TOO_LONG")
        if cue["end"] - cue["start"] >= audit.LONG_STATIC_CUE_SECONDS and lines:
            problems.append("SUBTITLE_LONG_STATIC_CUE")
    return sorted(set(problems))


def a2_srt(ctx: Context) -> dict[str, Any]:
    problems = srt_shape_problems(ctx.files["srt"])
    cues = srt_cues(ctx.text("srt"))
    return row("A2", FAIL if problems else PASS, problems, cue_count=len(cues))


# --------------------------------------------------------------------- A3
def speaker_problems(root: Path, cid: str) -> tuple[list[str], list[dict[str, Any]]]:
    """ASS/speaker.srt event parity plus speaker.srt/srt text parity and label set."""
    from src.autoslice.branding_intro import CHANNEL_PROFILE
    from src.autoslice.review_package_ass_audit import audit_review_package_ass

    files = package_files(root, cid)
    audit = _visual_constants()
    result = audit_review_package_ass(
        root=root,
        item={"ass_path": files["ass"].name, "speaker_srt": files["speaker_srt"].name},
        portable_required=False,
        max_visual_lines=audit.DEFAULT_MAX_VISUAL_LINES,
        max_visual_line_chars=audit.DEFAULT_MAX_VISUAL_LINE_CHARS,
    )
    problems = [issue.code for issue in result.issues]
    plain = srt_cues(files["srt"].read_text(encoding="utf-8"))
    labeled = srt_cues(files["speaker_srt"].read_text(encoding="utf-8"))
    host, guest = CHANNEL_PROFILE.host_speaker_label, CHANNEL_PROFILE.guest_speaker_label
    rows: list[dict[str, Any]] = []
    if len(plain) != len(labeled):
        problems.append("FMR_SPEAKER_CUE_COUNT_MISMATCH")
    for left, right in zip(plain, labeled):
        label, body = strip_speaker_label(right["text"])
        if (left["start_ms"], left["end_ms"]) != (right["start_ms"], right["end_ms"]):
            problems.append("FMR_SPEAKER_TIMING_MISMATCH")
        if left["text"] != body:
            problems.append("FMR_SPEAKER_TEXT_MISMATCH")
        if label and label not in {host, guest}:
            problems.append("FMR_SPEAKER_LABEL_UNKNOWN")
        rows.append({"cue_index": right["index"], "label": label or host, "text": body})
    return sorted(set(problems)), rows


def a3_speaker(ctx: Context) -> dict[str, Any]:
    problems, rows = speaker_problems(ctx.root, ctx.cid)
    ctx.record["_speaker_rows"] = rows
    return row("A3", FAIL if problems else PASS, problems, cue_count=len(rows))


# --------------------------------------------------------------------- A4 / A4b
def _intro_offset_ms(ctx: Context) -> int:
    receipt = load_json(ctx.files["intro_receipt"])
    value = receipt.get("intro_offset_ms")
    if type(value) is not int or value <= 0:
        raise FmrError("FMR_INTRO_OFFSET_INVALID")
    return value


def a4_audio_witness(ctx: Context) -> dict[str, Any]:
    """BCUT witness via the existing library; disagreement cues go to nominations.json for the reviewer."""
    from src.autoslice.final_subtitle_audio_gate import (
        FinalSubtitleAudioGateError,
        capture_final_subtitle_audio_check,
        validate_final_subtitle_audio_check,
    )
    from src.autoslice.witness_disagreement import find_witness_disagreements

    try:
        offset = _intro_offset_ms(ctx)
    except FmrError as exc:
        return row("A4", FAIL, [str(exc).split(":")[0]])
    video_sha = file_sha(ctx.files["video"])
    record = {
        "burned_preview": {
            "status": "BURNED", "path": str(ctx.files["video"]), "burned_sha256": video_sha,
            "branding_intro": {"intro_offset_ms": offset},
        },
        "artifact_hashes": {"burned_video_sha256": video_sha},
    }
    try:
        capture_final_subtitle_audio_check(
            record, ctx.files["srt"], ctx.root, ctx.cid, adapters=ctx.adapters.gate)
        validate_final_subtitle_audio_check(
            record, ctx.files["srt"], ctx.files["video"], ctx.root, candidate_id=ctx.cid)
    except (FinalSubtitleAudioGateError, ValueError, OSError) as exc:
        return row("A4", FAIL, ["FMR_A4_AUDIO_WITNESS_REJECTED"], error=str(exc)[:400])
    evidence = record["subtitle_audio_correspondence"]
    ctx.record["subtitle_audio_correspondence"] = evidence
    ctx.record.setdefault("artifact_hashes", {}).update(record["artifact_hashes"])
    ctx.record["_burned_preview"] = record["burned_preview"]
    witness = Path(evidence["witness_srt_path"])
    noms = find_witness_disagreements(
        ctx.text("srt"), witness.read_text(encoding="utf-8"), intro_offset_ms=offset)
    for nom in noms:
        ctx.nominations["j1"].append({
            "ref": f"bcut:{nom['nomination_id']}", "source": "BCUT_WITNESS",
            "cue_index": nom["cue_index"], "final_span": nom["final_span"],
            "witness_span": nom["witness_span"],
        })
    return row("A4", PASS, [], witness_status=evidence.get("status"), nomination_count=len(noms),
               intro_offset_ms=offset)


def a4b_jianying(ctx: Context) -> dict[str, Any]:
    if not ctx.adapters.use_jianying:
        return row("A4b", "SKIPPED_DISABLED", ["FMR_A4B_DISABLED"])
    from scripts import free_asr_client
    from src.autoslice.final_subtitle_audio_gate import _default_extract_audio
    from src.autoslice.witness_disagreement import find_witness_disagreements

    try:
        offset = _intro_offset_ms(ctx)
        fn = ctx.adapters.jianying or free_asr_client.transcribe_jianying
        extract = (ctx.adapters.gate.extract_audio if ctx.adapters.gate is not None
                   else _default_extract_audio)
        with tempfile.TemporaryDirectory(prefix=".fmr-jy-", dir=ctx.evidence) as tmp:
            audio = Path(tmp) / "input.mp3"
            extract(ctx.files["video"], audio)
            raw = fn(audio.read_bytes())
        witness_text = free_asr_client.to_srt(raw)
        noms = find_witness_disagreements(ctx.text("srt"), witness_text, intro_offset_ms=offset)
    except Exception as exc:  # provider/unavailable is a documented, non-fatal skip
        return row("A4b", "SKIPPED_UNAVAILABLE", ["FMR_A4B_UNAVAILABLE"], error=str(exc)[:300])
    write_json(ctx.evidence / "jianying.raw.json", raw)
    (ctx.evidence / "jianying.witness.srt").write_text(witness_text, encoding="utf-8")
    for nom in noms:
        ctx.nominations["j1"].append({
            "ref": f"jianying:{nom['nomination_id']}", "source": "JIANYING_WITNESS",
            "cue_index": nom["cue_index"], "final_span": nom["final_span"],
            "witness_span": nom["witness_span"],
        })
    return row("A4b", PASS, [], nomination_count=len(noms),
               evidence=[evidence_ref(ctx.root, ctx.evidence / "jianying.witness.srt")])


# --------------------------------------------------------------------- A5
def a5_decode_probe(ctx: Context) -> dict[str, Any]:
    from src.autoslice.branding_intro import BrandingIntroError, _probe_media, _stream_contract

    reasons: list[str] = []
    decode = ctx.adapters.run_command(
        ["ffmpeg", "-nostdin", "-hide_banner", "-v", "error", "-i", str(ctx.files["video"]), "-f", "null", "-"])
    if decode.returncode != 0 or (decode.stderr or "").strip():
        reasons.append("FMR_A5_FULL_DECODE_FAILED")
    try:
        probe = _probe_media(ctx.files["video"])
        contract = _stream_contract(probe)
    except BrandingIntroError as exc:
        return row("A5", FAIL, reasons + ["FMR_A5_PROBE_FAILED"], error=str(exc)[:300])
    if probe["counts"] != {"video": 1, "audio": 1, "other": 0}:
        reasons.append("FMR_A5_STREAM_COUNT")
    declared = (load_json(ctx.files["intro_receipt"]).get("verification") or {}).get("stream_contract")
    if isinstance(declared, dict) and declared != contract:
        reasons.append("FMR_A5_STREAM_CONTRACT_DRIFT")
    ctx.record["_video_probe"] = {"duration_ms": probe["duration_ms"], "contract": contract}
    return row("A5", FAIL if reasons else PASS, reasons, duration_ms=probe["duration_ms"], contract=contract)


# --------------------------------------------------------------------- A6
def intro_problems(receipt: dict[str, Any], *, video_duration_ms: int | None) -> list[str]:
    """Roster and arithmetic checks on the intro receipt (media replay is separate)."""
    from src.autoslice.branding_intro import (
        _COPY_CONCAT_DURATION_TOLERANCE_MS,
        _REENCODE_DURATION_TOLERANCE_MS,
        BrandingIntroError,
        CHANNEL_PROFILE,
        load_branding_intro_policy,
        policy_intros,
    )

    problems: list[str] = []
    try:
        policy = load_branding_intro_policy(CHANNEL_PROFILE.asset_file("branding_intro_manifest"))
    except BrandingIntroError:
        return ["BRANDING_INTRO_POLICY_INVALID"]
    if policy is None:
        return []
    if receipt.get("status") != "PREPENDED":
        problems.append("BRANDING_INTRO_BINDING_NOT_PREPENDED")
    offset = receipt.get("intro_offset_ms")
    if type(offset) is not int or offset <= 0:
        problems.append("BRANDING_INTRO_OFFSET_INVALID")
    entries = {str(e.get("intro_id")): e for e in policy_intros(policy) if isinstance(e, dict)}
    entry = entries.get(str(receipt.get("intro_id") or ""))
    if entry is None:
        problems.append("BRANDING_INTRO_MANIFEST_ENTRY_MISSING")
    else:
        video = entry.get("video") if isinstance(entry.get("video"), dict) else {}
        if str(receipt.get("intro_media_sha256") or "").removeprefix("sha256:") != str(
            video.get("sha256") or "").removeprefix("sha256:"):
            problems.append("BRANDING_INTRO_MANIFEST_HASH_MISMATCH")
    before = receipt.get("main_duration_ms_before")
    if video_duration_ms is not None and type(before) is int and type(offset) is int:
        tolerance = (_COPY_CONCAT_DURATION_TOLERANCE_MS if "copy" in str(receipt.get("method") or "")
                     else _REENCODE_DURATION_TOLERANCE_MS)
        if abs(video_duration_ms - (before + offset)) > tolerance:
            problems.append("BRANDING_INTRO_DURATION_ARITHMETIC")
    return sorted(set(problems))


def a6_intro(ctx: Context) -> dict[str, Any]:
    """Roster, receipt arithmetic and (when present) the roster media bytes.

    A matched intro is re-encoded to the main stream contract, so its decoded
    frames legitimately differ from the roster media; the roster hash plus the
    duration arithmetic are the bindable facts.
    """
    receipt = load_json(ctx.files["intro_receipt"])
    probe = ctx.record.get("_video_probe") or {}
    problems = intro_problems(receipt, video_duration_ms=probe.get("duration_ms"))
    roster_media = "SKIPPED_MEDIA_UNAVAILABLE"
    intro_media = Path(str(receipt.get("intro_media_path") or ""))
    if intro_media.is_file():
        same = file_sha(intro_media).removeprefix("sha256:") == str(
            receipt.get("intro_media_sha256") or "").removeprefix("sha256:")
        roster_media = "HASH_EQUAL" if same else "HASH_MISMATCH"
        if not same:
            problems.append("BRANDING_INTRO_MEDIA_FILE_HASH_MISMATCH")
    if not problems:
        ctx.record["_intro"] = {
            key: receipt.get(key) for key in (
                "status", "intro_id", "intro_media_sha256", "intro_offset_ms", "method",
                "policy_manifest_sha256")
        }
    return row("A6", FAIL if problems else PASS, problems, roster_media=roster_media,
               intro_id=receipt.get("intro_id"), intro_offset_ms=receipt.get("intro_offset_ms"))


# --------------------------------------------------------------------- A7
def _silence_around(ctx: Context, source: Path, point_ms: int) -> dict[str, Any]:
    lead_ms = min(1000, point_ms)
    window_start = point_ms - lead_ms
    done = ctx.adapters.run_command([
        "ffmpeg", "-nostdin", "-hide_banner", "-ss", f"{window_start / 1000:.3f}", "-t", "2",
        "-i", str(source), "-vn", "-af", f"silencedetect=noise={SILENCE_NOISE_DB}dB:d={SILENCE_MIN_S}",
        "-f", "null", "-"])
    starts = [float(m) for m in re.findall(r"silence_start: (-?[\d.]+)", done.stderr or "")]
    ends = [float(m) for m in re.findall(r"silence_end: ([\d.]+)", done.stderr or "")]
    intervals = []
    for index, start in enumerate(starts):
        intervals.append((start, ends[index] if index < len(ends) else float("inf")))
    local = lead_ms / 1000
    inside = any(start - 1e-6 <= local <= end + 1e-6 for start, end in intervals)
    return {
        "point_ms": point_ms, "window_start_ms": window_start, "inside_silence": inside,
        "noise_db": SILENCE_NOISE_DB, "min_silence_s": SILENCE_MIN_S,
        "intervals_s": [[round(s + window_start / 1000, 3),
                         None if e == float("inf") else round(e + window_start / 1000, 3)]
                        for s, e in intervals],
    }


def _context_cues(ctx: Context) -> tuple[list[dict], list[dict]]:
    source = ctx.member["source"]
    first, last = source["pieces"][0], source["pieces"][-1]

    def cues_for(piece: dict) -> tuple[list[dict], int, int]:
        if "recording" in piece:
            spec = piece["recording_context_srt"]
            start, end = piece["recording_start_ms"], piece["recording_end_ms"]
        else:
            spec = source["context_srt"]
            start, end = piece["start_ms"], piece["end_ms"]
        zero = spec["source_zero_srt_ms"]
        cues = srt_cues(Path(spec["path"]).read_text(encoding="utf-8"))
        for cue in cues:
            cue["source_start_ms"] = cue["start_ms"] - zero
            cue["source_end_ms"] = cue["end_ms"] - zero
        return cues, start, end

    first_cues, first_start, _ = cues_for(first)
    last_cues, _, last_end = cues_for(last)
    before = [c for c in first_cues if c["source_start_ms"] < first_start][-CONTEXT_CUES_EACH_SIDE:]
    after = [c for c in last_cues if c["source_end_ms"] > last_end][:CONTEXT_CUES_EACH_SIDE]
    pick = lambda c: {"index": c["index"], "source_start_ms": c["source_start_ms"],
                      "source_end_ms": c["source_end_ms"], "text": c["text"]}
    return [pick(c) for c in before], [pick(c) for c in after]


def a7_source_interval(ctx: Context) -> dict[str, Any]:
    from src.autoslice.talk_filler import MIN_TALK_EFFECTIVE_DURATION_MS

    reasons: list[str] = []
    flags: list[str] = []
    pieces = ctx.member["source"]["pieces"]
    effective = sum(p["end_ms"] - p["start_ms"] for p in pieces)
    if effective <= MIN_TALK_EFFECTIVE_DURATION_MS:
        reasons.append("TALK_EFFECTIVE_DURATION_NOT_OVER_MINIMUM")
    cuts = {}
    for label, piece, point in (("start", pieces[0], pieces[0]["start_ms"]),
                                ("end", pieces[-1], pieces[-1]["end_ms"])):
        try:
            cuts[label] = _silence_around(ctx, Path(piece["path"]), point)
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            return row("A7", FAIL, reasons + ["FMR_A7_SILENCE_PROBE_FAILED"], error=str(exc)[:300])
        if not cuts[label]["inside_silence"]:
            flags.append(f"CUT_{label.upper()}_NOT_IN_SILENCE")
    try:
        before, after = _context_cues(ctx)
    except (OSError, FmrError, ValueError, KeyError) as exc:
        return row("A7", FAIL, reasons + ["FMR_A7_CONTEXT_UNREADABLE"], error=str(exc)[:300])
    if not before or not after:
        reasons.append("FMR_A7_CONTEXT_MISSING")
    write_json(ctx.evidence / "boundary-context.json", {
        "schema_version": "finished-media-boundary-context.v1",
        "clock": "source_file_ms", "before": before, "after": after,
        "next_topic_first_cue": after[0] if after else None, "cuts": cuts,
        "effective_ms": effective, "minimum_exclusive_ms": MIN_TALK_EFFECTIVE_DURATION_MS,
    })
    ctx.record["_a7"] = {"effective_ms": effective}
    status = FAIL if reasons else (FLAG if flags else PASS)
    return row("A7", status, reasons + flags, effective_ms=effective,
               interval_correspondence="NOT_RUN_OPTIONAL",
               evidence=[evidence_ref(ctx.root, ctx.evidence / "boundary-context.json")])


# --------------------------------------------------------------------- A8
def canon_hits(texts: dict[str, str]) -> list[dict[str, Any]]:
    from src.autoslice.surface_canon import (
        canonicalize_caption_display_surfaces,
        canonicalize_hard_meme_surfaces,
    )

    hits: list[dict[str, Any]] = []
    for name, text in texts.items():
        for fn in (canonicalize_hard_meme_surfaces, canonicalize_caption_display_surfaces):
            _, replacements = fn(text)
            hits.extend({"where": name, **item} for item in replacements)
    return hits


def a8_canon(ctx: Context) -> dict[str, Any]:
    texts = {
        "srt": ctx.text("srt"), "speaker_srt": ctx.text("speaker_srt"),
        "title": ctx.text("title").strip(), "description": ctx.text("description"),
        "cover_lines": "\n".join(ctx.member["cover_claims"]["lines"]),
    }
    hits = canon_hits(texts)
    write_json(ctx.evidence / "canon-hits.json", {"hits": hits})
    return row("A8", FAIL if hits else PASS, ["FMR_A8_CANON_SURFACE_HIT"] if hits else [], hits=hits)


# --------------------------------------------------------------------- A9
def title_problems(title: str, speaker_srt: str) -> tuple[list[str], dict[str, Any]]:
    from src.autoslice.title_cover_quotes import check_title_quotes
    from src.autoslice.title_policy import publish_title_policy_violations

    problems = list(publish_title_policy_violations(title, lane="talk", enforce_automatic_style=True))
    quotes = check_title_quotes(title, speaker_srt)
    problems.extend(quotes.get("reason_codes") or [])
    return sorted(set(problems)), quotes


def a9_title_quotes(ctx: Context) -> dict[str, Any]:
    from scripts import authorized_upload as au
    from src.autoslice.title_cover_quotes import check_cover_quotes

    title = ctx.text("title").strip()
    speaker = ctx.text("speaker_srt")
    problems, quotes = title_problems(title, speaker)
    try:
        description = au.description_for_title(title, extra=ctx.member.get("description_extra"))
    except Exception as exc:  # EnvelopeError: partner envelope cannot render
        problems.append("FMR_A9_DESCRIPTION_ENVELOPE_INVALID")
        description = ""
        quotes = {**quotes, "envelope_error": str(exc)[:200]}
    cover_rows = check_cover_quotes(ctx.member["cover_claims"]["lines"], speaker)
    if any(r["status"] == "MISSING_QUOTE" for r in cover_rows):
        problems.append("FMR_A9_COVER_QUOTE_MISSING")
    write_json(ctx.evidence / "quotes.json", {
        "title": quotes, "cover_lines": cover_rows, "manifest_description": description})
    paraphrase = [r["line"] for r in cover_rows if r["status"] == "PARAPHRASE"]
    status = FAIL if problems else (FLAG if paraphrase else PASS)
    return row("A9", status, problems + (["COVER_LINE_PARAPHRASE"] if paraphrase and not problems else []),
               paraphrase_lines=paraphrase)


# --------------------------------------------------------------------- A10
def a10_tags(ctx: Context, tags_from: Path | None = None) -> dict[str, Any]:
    from src.autoslice.authorized_upload_metadata import validate_tag_generation, validate_tags

    title = ctx.text("title").strip()
    srt_sha = file_sha(ctx.files["srt"])
    if tags_from is not None:
        generated = load_json(tags_from)
        binding = generated.get("input_binding") or {}
        title_sha = "sha256:" + __import__("hashlib").sha256(title.encode("utf-8")).hexdigest()
        if (str(binding.get("title_sha256") or "").removeprefix("sha256:") != title_sha.removeprefix("sha256:")
                or str(binding.get("srt_sha256") or "").removeprefix("sha256:") != srt_sha.removeprefix("sha256:")):
            return row("A10", FAIL, ["FMR_A10_TAGS_INPUT_BINDING_MISMATCH"])
    else:
        fn = ctx.adapters.tags
        if fn is None:
            from scripts.suggest_upload_tags import generate_upload_tags as fn
        generated = fn(title, ctx.files["srt"])
    tags = generated.get("final_tags")
    problems = []
    if not isinstance(tags, list) or not tags:
        problems.append("FMR_A10_TAGS_EMPTY")
    else:
        problems.extend(validate_tags(tags))
    problems.extend(validate_tag_generation(generated))
    if generated.get("status") not in {"OK"}:
        problems.append("FMR_A10_TAG_GENERATION_NOT_COMPLETE")
    write_json(ctx.evidence / "tags.json", generated)
    if not problems:
        ctx.record["upload_tags"] = generated
    return row("A10", FAIL if problems else PASS, [str(p)[:80] for p in problems], tag_count=len(tags or []))


# --------------------------------------------------------------------- A11
def a11_cover(ctx: Context) -> dict[str, Any]:
    from PIL import Image

    from src.autoslice.cover_generation import _COVER_CANVAS

    reasons: list[str] = []
    cover = ctx.files["cover"]
    with open(cover, "rb") as handle:
        if handle.read(8) != b"\x89PNG\r\n\x1a\n":
            reasons.append("FMR_A11_COVER_NOT_PNG")
    with Image.open(cover) as image:
        size = list(image.size)
    if tuple(size) != tuple(_COVER_CANVAS):
        reasons.append("FMR_A11_COVER_CANVAS_MISMATCH")
    claims = ctx.member["cover_claims"]
    detail: dict[str, Any] = {"size": size}
    detail.update(_font_and_render_checks(claims, reasons))
    # three thumbnail sizes the reviewer actually looks at
    thumbs = ctx.evidence / "cover"
    thumbs.mkdir(parents=True, exist_ok=True)
    with Image.open(cover) as image:
        image = image.convert("RGB")
        shots = {"cover.png": image, "thumb-phone.png": image.resize((360, 202)),
                 "thumb-phone-x3.png": image.resize((120, 67)).resize((360, 202))}
        for name, img in shots.items():
            img.save(thumbs / name, "PNG")
    detail["thumbnails"] = [evidence_ref(ctx.root, thumbs / name) for name in shots]
    return row("A11", FAIL if reasons else PASS, reasons, **detail)


def _font_and_render_checks(claims: dict[str, Any], reasons: list[str]) -> dict[str, Any]:
    from src.autoslice.cover_style import MIN_PUNCH, SAFE_X0, SAFE_X1, missing_glyphs

    out: dict[str, Any] = {"font_check": "NOT_DECLARED", "render_check": "NOT_DECLARED"}
    font = claims.get("font_file")
    if isinstance(font, str) and font:
        path = Path(font)
        if not path.is_file():
            reasons.append("FMR_A11_FONT_UNAVAILABLE")
            out["font_check"] = "FONT_MISSING"
        else:
            declared = claims.get("font_sha256")
            if is_sha(declared) and file_sha(path) != declared:
                reasons.append("FMR_A11_FONT_HASH_MISMATCH")
            missing = missing_glyphs(str(path), "".join(claims["lines"]))
            if missing:
                reasons.append("FMR_A11_GLYPH_MISSING")
            out["font_check"] = "MISSING_GLYPHS" if missing else "OK"
            out["missing_glyphs"] = missing
    render = claims.get("render_json")
    if isinstance(render, str) and render and Path(render).is_file():
        doc = json.loads(Path(render).read_text(encoding="utf-8"))
        lines = doc.get("lines") or []
        sizes = [row_.get("size") for row_ in lines if isinstance(row_, dict)]
        out["render_check"] = "OK"
        if not sizes or max(s for s in sizes if isinstance(s, int)) < MIN_PUNCH:
            reasons.append("FMR_A11_PUNCH_TEXT_TOO_SMALL")
            out["render_check"] = "FAIL"
        if any(not (SAFE_X0 <= r_["x"] and r_["x"] + r_["w"] <= SAFE_X1) for r_ in lines
               if isinstance(r_, dict) and {"x", "w"} <= set(r_)):
            reasons.append("FMR_A11_TEXT_OUTSIDE_SAFE_ZONE")
            out["render_check"] = "FAIL"
    return out


# --------------------------------------------------------------------- A13
def a13_duplicates(ctx: Context) -> dict[str, Any]:
    """The external duplicate check is already a standalone tool; record its verdict if given."""
    verdict = ctx.member.get("duplicate_check")
    if not isinstance(verdict, dict):
        return row("A13", "SKIPPED_UNAVAILABLE", ["FMR_A13_NOT_SUPPLIED"])
    status = str(verdict.get("status") or "")
    if status == "DUPLICATE":
        return row("A13", FAIL, ["FMR_A13_DUPLICATE"], verdict=verdict)
    return row("A13", PASS, [], verdict=verdict)


# ------------------------------------------------- reviewer aids (nominations.json)
def build_speaker_nominations(ctx: Context) -> list[dict[str, Any]]:
    from src.autoslice.branding_intro import CHANNEL_PROFILE

    host, guest = CHANNEL_PROFILE.host_speaker_label, CHANNEL_PROFILE.guest_speaker_label
    rows = ctx.record.get("_speaker_rows") or []
    claimed = {c.get("cue_index") for c in ctx.member.get("speaker_claims") or [] if isinstance(c, dict)}
    reasons_by_cue: dict[int, list[str]] = {}
    first_host: int | None = None
    for item in rows:
        index, label, text = item["cue_index"], item["label"], item["text"]
        found = reasons_by_cue.setdefault(index, [])
        if label == guest:
            found.append("GUEST_LABEL")
        if re.search(r"李豆沙|小李|豆沙", text):
            found.append("HOST_NAME_MENTION")
        if re.search(r"下一(道)?题|第[一二三四五六七八九十\d]+题|^\d+[.、]", text):
            found.append("QUESTION_READ")
        if index in claimed:
            found.append("MAKER_CLAIM")
        if label == host and first_host is None:
            first_host = index
    if first_host is not None:
        reasons_by_cue[first_host].append("SAMPLE_HOST")
    return [
        {"ref": f"cue:{index}", "cue_index": index, "reasons": sorted(set(reasons)),
         "label": next(r["label"] for r in rows if r["cue_index"] == index)}
        for index, reasons in sorted(reasons_by_cue.items()) if reasons
    ]


def extract_frames(ctx: Context, nominations: list[dict[str, Any]]) -> list[str]:
    offset = _intro_offset_ms(ctx)
    cues = {c["index"]: c for c in srt_cues(ctx.text("srt"))}
    frames = ctx.evidence / "frames"
    frames.mkdir(parents=True, exist_ok=True)
    per_reason: dict[str, int] = {}
    reasons_out: list[str] = []
    for nom in nominations:
        quota_ok = False
        for reason in nom["reasons"]:
            if per_reason.get(reason, 0) < MAX_FRAMES_PER_REASON:
                per_reason[reason] = per_reason.get(reason, 0) + 1
                quota_ok = True
        cue = cues.get(nom["cue_index"])
        if not quota_ok or cue is None:
            continue
        mid = offset + (cue["start_ms"] + cue["end_ms"]) // 2
        target = frames / f"cue-{nom['cue_index']}.png"
        done = ctx.adapters.run_command([
            "ffmpeg", "-nostdin", "-hide_banner", "-v", "error", "-y", "-ss", f"{mid / 1000:.3f}",
            "-i", str(ctx.files["video"]), "-frames:v", "1", str(target)])
        if done.returncode == 0 and target.is_file():
            nom["frame"] = evidence_ref(ctx.root, target)
        else:
            reasons_out.append(f"FMR_FRAME_EXTRACTION_FAILED_CUE_{nom['cue_index']}")
    return reasons_out


def build_doubt_nominations(ctx: Context) -> list[dict[str, Any]]:
    return [
        {"ref": f"doubt:{d['id']}", "source": "MAKER_DOUBT", "grade": d["grade"],
         "cue_index": d.get("cue_index")}
        for d in ctx.member.get("doubts") or []
    ]


# ------------------------------------------------------------------ orchestration
STATIC_ORDER = (a1_seal, a2_srt, a3_speaker, a5_decode_probe, a6_intro, a8_canon, a9_title_quotes,
                a11_cover)


def run_checks(ctx: Context, *, tags_from: Path | None = None) -> dict[str, Any]:
    """Run every A check, write evidence/auto-checks.json and nominations.json."""
    rows: list[dict[str, Any]] = []
    for fn in (a1_seal, a2_srt, a3_speaker, a5_decode_probe, a6_intro):
        rows.append(fn(ctx))
    rows.append(a4_audio_witness(ctx))
    rows.append(a4b_jianying(ctx))
    rows.append(a7_source_interval(ctx))
    for fn in (a8_canon, a9_title_quotes):
        rows.append(fn(ctx))
    rows.append(a10_tags(ctx, tags_from))
    rows.append(a11_cover(ctx))
    rows.append(a13_duplicates(ctx))
    ctx.nominations["j1"].extend(build_doubt_nominations(ctx))
    ctx.nominations["j6"] = build_speaker_nominations(ctx)
    frame_problems = extract_frames(ctx, ctx.nominations["j6"])
    if frame_problems:
        rows.append(row("A12", FAIL, frame_problems))
    else:
        rows.append(row("A12", PASS, [], frame_count=sum(1 for n in ctx.nominations["j6"] if "frame" in n)))
    nominations = {"schema_version": "finished-media-nominations.v1", **ctx.nominations}
    write_json(ctx.evidence / NOMINATIONS_NAME, nominations)
    document = {
        "schema_version": AUTO_CHECKS_SCHEMA,
        "candidate_id": ctx.cid,
        "checks": rows,
        "nominations_sha256": canonical_sha(nominations),
    }
    write_json(ctx.evidence / AUTO_CHECKS_NAME, document)
    return document
