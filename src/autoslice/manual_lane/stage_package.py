"""Steps 8-10: the finished-clip package, the finished-media lane (FMR), CPA review with automatic revision, upload switch.

FMR is the lightweight review lane; upload needs a sealed package and an attested PASS receipt.
This module makes the pilot calls (init -> check -> attest -> seal), without copying the lane's checks.
check's FLAG and FAIL findings are recorded, not blocking.

Review (TASK-9): one CPA reading of the finished package.  Its fixes (subtitle text, speakers of a linked clip, title,
tags) are adopted as written, applied in place to the products, and only the steps a fix invalidates are rebuilt;
the review is not asked again (no convergence rounds).  The one interception is doubt_policy.HOLD_RULE (定不下且影响
意思): CPA can decide the residual uncertainty of existing doubts as well as add a new hold.  A package with a hold is
finished but not sealed; one without is attested PASS and sealed.  Step 10 stays behind the upload switch.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from collections.abc import MutableMapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, NotRequired, TypedDict, cast

from src.autoslice.channel_envelope import EnvelopeError
from src.autoslice.upload_description import description_for_title

from . import (compilation, cover_import, dissent_register, doubt_policy, focused_retry, gates, normalize, package_guards,
               publication_id, review_owner, revision_authority, speaker_v8, step10_run, subtitle_track, title_authority, writing_canon)
from .adapters import CPA_JUDGE_MODEL, recover_cpa_terminal
from .compilation import MemberPiece
from .config import participant_description, participant_names, placeholder_as_person
from .runner import Ctx, StepOutcome
from .srt_text import SrtCue
from .review_owner import cue_number as _cue_number
from .review_owner import subtitle_edits as _subtitle_edits
from .state import bcut_source_zero_ms
from .step_io import _cache_cpa_answer, _cpa_cache_path, _effort_of, _record_cpa_files, _write_atomic
from .stage_impl import (
    _ask_cpa,
    _cpa_ok,
    _fail,
    _json_from_text,
    _log,
    _render,
    linked_session,
    parse_srt,
)

if TYPE_CHECKING:
    from src.autoslice.cpa_gemini_client import JsonValue
    from .review_owner import Dissent
    from .state import CpaEvent
    from .wide_held import WideProgress, WideRound


class ReviewDoubt(doubt_policy.DecisionView, total=False):
    cue_id: int | None
    current: str
    start_s: float
    end_s: float
    abs_start_ms: int
    abs_end_ms: int
    maintained: bool
    basis: str | None
    reason: str | None
    source: str
    candidates: list[doubt_policy.Candidate]
    alternatives: list[str]
    readings: dict[str, str]
    reasons: list[str]
    danmaku: list[str]
    asr_notes: list[str]
    speaker_note: str
    read_aloud: str
    review_note: str


class ReviewFix(speaker_v8.SpeakerReview, total=False):
    cue_id: int | str | None
    text: str | None


class ReviewHold(TypedDict, total=False):
    cue_id: int | str | None
    reason: str | None


class ReviewDoubtDecision(TypedDict, total=False):
    id: str
    confidence: str
    affects_meaning: bool
    reason: str


class ReviewFixes(TypedDict, total=False):
    subtitles: list[ReviewFix | None]
    speakers: list[ReviewFix | None]
    title: str | None
    tags: JsonValue


class ReviewNote(TypedDict, total=False):
    item: JsonValue
    cue_id: JsonValue
    text: JsonValue
    severity: JsonValue


class ReviewPromptCandidate(TypedDict, total=False):
    text: str | None
    source: str | None
    sources: list[str] | None
    families: list[str]
    supporters: int


class ReviewPromptDoubt(TypedDict, total=False):
    id: str | None
    final_cues: list[int]
    final_text: str | None
    state: str | None
    confidence: str | None
    affects_meaning: bool | None
    held: bool
    basis: str | None
    reason: str | None
    readings: dict[str, str]
    current: str | None
    reasons: list[str]
    danmaku: list[str]
    asr_notes: list[str]
    speaker_note: str | None
    read_aloud: str | None
    candidates: list[ReviewPromptCandidate]


class ReviewAnswer(TypedDict, total=False):
    fixes: ReviewFixes
    holds: list[ReviewHold]
    doubt_decisions: list[ReviewDoubtDecision]
    notes: list[ReviewNote]
    findings: list[ReviewNote]
    review_md: str | None


class ReviewApplied(TypedDict):
    subtitles: int
    speakers: int
    title: bool
    tags: bool
    holds: int
    decisions: int
    skipped: list[str]
    cpa: list[CpaEvent]
    dissent: NotRequired[list[Dissent]]
    adopted: NotRequired[list[str]]     # owner-decided clip: the sentences he did not touch that the review changed


class ReviewCounts(TypedDict):
    subtitles: int
    speakers: int
    title: bool
    tags: bool
    holds: int
    decisions: int


class ReviewRecord(TypedDict):
    call: str | None
    cpa: list[CpaEvent]
    applied: ReviewCounts
    skipped: list[str]
    notes: list[ReviewNote]
    rebuild: bool
    held_ids: NotRequired[list[str]]
    fresh_rebuild: NotRequired[bool]
    wide_held: NotRequired[WideProgress]
    dissent: NotRequired[list[Dissent]]


class DedupeDetail(gates.DedupeResult, total=False):
    block_upload: JsonValue
    reason: JsonValue


class MemberDoubt(TypedDict):
    id: str
    grade: str
    cue_index: int | None
    final_text: str | None
    basis: str


class MemberContextSrt(TypedDict):
    path: str
    source_zero_srt_ms: int


class MemberSource(TypedDict):
    pieces: list[MemberPiece]
    context_srt: MemberContextSrt


class MemberMaker(TypedDict):
    kind: str
    id: str


class MemberCoverClaims(TypedDict):
    lines: list[str]


class MemberDuplicate(TypedDict):
    status: str | None
    block_upload: bool
    reason: JsonValue
    result_json: str


class Member(TypedDict):
    schema_version: str
    member_slug: str
    recording_date: str
    maker: MemberMaker
    files: dict[str, str]
    source: MemberSource
    selection_hook: str
    doubts: list[MemberDoubt]
    cover_claims: MemberCoverClaims
    speaker_claims: list[str]
    duplicate_check: MemberDuplicate

REPO_ROOT = Path(__file__).resolve().parents[3]
FMR_SCRIPT = REPO_ROOT / "scripts" / "finished_media_review.py"
MAKER_PREFIX = "manual-lane-maker"
REVIEWER_PREFIX = "cpa-review"
PKG = "pkg"
REVIEW_KEY: Final = "review"    # state.review: the adopted review of this package (fixes applied, holds, notes)
# Rewind targets: what an applied fix invalidates.  A subtitle text or speaker fix is applied to final.srt and
# speaker.srt together (subtitle_track), so step 4 is not run again (it would re-classify the speakers, and an imported
# push would be lost); the burn, the tags (bound to the final.srt bytes, FMR A10) and the package are rebuilt.  The
# title and cover stay: a fix to one sentence is not a new story, and the review fixes the title itself when it must.
# A title fix rebuilds the tags (bound to the title) and the package; a tags fix only the package.  The review step is
# in every set so that it runs once more to finish (attest/seal) without asking CPA again.
REWIND_SUBTITLES = ("burn", "tags", "package", "review")
REWIND_TITLE = ("tags", "package", "review")
REWIND_TAGS = ("package", "review")


def maker_id(ctx: Ctx) -> str:
    return f"{MAKER_PREFIX}-{ctx.state.data['clip']['cid']}"


def reviewer_id() -> str:
    return f"{REVIEWER_PREFIX}-{CPA_JUDGE_MODEL}"


def _pkg(ctx: Ctx) -> Path:
    return ctx.clip_dir / PKG


def member_slug(cid: str) -> str:
    """The lane's slug rule is [a-z0-9][a-z0-9-]{0,40}; the manuscript's clip id (a trimmed successor's is its original's) maps onto it one to one."""
    return publication_id.publication_cid(cid).lower().replace("_", "-")


def grade_of(row: ReviewDoubt) -> str:
    """The lane wants A-D per doubt.  The pipeline's own evidence tier: A = two independent witnesses agree
    (maintained); B = CPA settled it from the context (ADOPT or FIT_CONTEXT); C = kept as is; D = held under
    doubt_policy.HOLD_RULE (定不下且影响意思; the same definition as step 3 and the review)."""
    if doubt_policy.is_hold(row):
        return "D"
    if row.get("maintained"):
        return "A"
    return "B" if row.get("state") in {"ADOPT", "FIT_CONTEXT"} else "C"


def package_dir(ctx: Ctx) -> Path:
    """Where the lane writes the package, as in the pilot clips: out/<recording_date>/fm-<date>-<slug>."""
    date = ctx.session["recording_date"]
    return ctx.clip_dir / "fmr" / "out" / date / f"fm-{date.replace('-', '')}-{member_slug(ctx.state.data['clip']['cid'])}"


def _fmr(*args: str) -> subprocess.CompletedProcess[str]:
    """One call to the lane's CLI from the repo root; the lane itself decides what its output means."""
    return subprocess.run([sys.executable, str(FMR_SCRIPT), *args], cwd=str(REPO_ROOT),
                          capture_output=True, text=True, timeout=3600,
                          check=False)   # the callers read the rc and the lane's own FAIL lines


def _fmr_flags(text: str) -> list[str]:
    """Lines the lane printed as FLAG or FAIL: recorded in PROCESS.md, never used to stop."""
    return [ln.strip() for ln in text.splitlines() if " FLAG" in ln or " FAIL" in ln or ln.startswith("FAIL")]


def description_text(title: str) -> str:
    """The publish description exactly as the uploader renders it (src/autoslice/upload_description.description_for_title, which the uploader uses too).
    The FMR A9 check and the uploader's own drift check recompute the same bytes, so the package carries them too.
    Partners resolve from the reviewed registry (assets/lidousha/streamer_registry_sources.v1.json); a partner the
    reviewed registry does not know raises EnvelopeError, which the uploader refuses as well."""
    return description_for_title(title)


def _missing(paths: dict[str, Path]) -> list[str]:
    return [name for name, p in paths.items() if not p.exists()]


def _bcut(ctx: Ctx) -> list[SrtCue]:
    path = ctx.clip_dir / "asr/bcut.srt"
    return parse_srt(path.read_text(encoding="utf-8")) if path.exists() else []


def doubt_numbers(ctx: Ctx, doubts: Sequence[ReviewDoubt]) -> dict[str, list[int]]:
    """{doubt id: final.srt numbers} for the current final subtitles (subtitle_track.doubt_finals)."""
    final = subtitle_track.parse_blocks((ctx.clip_dir / "subs/final.srt").read_text(encoding="utf-8"))
    return subtitle_track.doubt_finals(cast(Sequence[subtitle_track.DoubtTime], doubts), final, _bcut(ctx),
                                       int(ctx.state.data["clip"]["start_ms"]))


def current_doubts(ctx: Ctx) -> tuple[list[ReviewDoubt], dict[str, list[int]]]:
    """The doubt table as every step-8/9 reader sees it, with its numbers (doubt_numbers).  The words of a doubt are the
    words of the final cues it owns in the current track (subtitle_track), not the text stored in doubts.json: every
    writer after step 3 (step 4's lone-character join, a review fix) edits the track, so a stored copy could disagree
    with the subtitles (TASK-9 25.4: six review rewrites were shown with the old words).  A doubt with no final cue
    keeps its stored text.  The table itself is not rewritten here."""
    doubts = cast(list[ReviewDoubt], json.loads((ctx.clip_dir / "subs/doubts.json").read_text(encoding="utf-8")))
    numbers = doubt_numbers(ctx, doubts)
    track = subtitle_track.parse_blocks((ctx.clip_dir / "subs/final.srt").read_text(encoding="utf-8"))
    for d in doubts:
        owned = [n for n in numbers.get(str(d["id"]), []) if 1 <= n <= len(track)]
        if owned:
            d["final_text"] = "".join(subtitle_track.plain_text(track[n - 1]["text"]) for n in owned)
    return doubts, numbers


def _selection_context(hook: str, chosen: object) -> tuple[str, str]:
    """Use the original selection hook, or the chosen CPA title candidate's existing punchline."""
    if hook.strip():
        return hook, "原始选片 clip.hook"
    if isinstance(chosen, dict):
        punchline = chosen.get("punchline")
        if isinstance(punchline, str) and punchline.strip():
            return punchline, "CPA 已选候选 chosen.punchline"
    return hook, "缺失"


def _member(ctx: Ctx, *, files: dict[str, str], cover_lines: list[str], dedup: DedupeDetail,
            cut_local: Path, duration_ms: int, selection_hook: str) -> Member:
    """The member file the lane reads, shaped like the pilot clips' member.json.  The context subtitles are the
    original BCUT file, which may start at the source or at a bound segment time; only its real context is available.
    The cut's own clock is the piece clock (piece starts at 0).  A doubt's cue_index is its final.srt number (the lane
    indexes final.srt), not the BCUT cue id; a doubt with no final cue is left without one."""
    clip = ctx.state.data["clip"]
    doubts, numbers = current_doubts(ctx)
    manifest = compilation.load_manifest(ctx.clip_dir)
    pieces: list[MemberPiece] = ([{"path": str(cut_local), "start_ms": 0, "end_ms": duration_ms}] if manifest is None
              else compilation.member_pieces(manifest, str(cut_local), clip["start_ms"], duration_ms))
    if manifest is not None:
        recordings = {r["name"]: r for r in manifest["recordings"]}
        for piece in pieces:
            recording = recordings[piece["recording"]]
            piece["recording_context_srt"] = {
                "path": str(compilation.recording_bcut_path(ctx.clip_dir, recording)),
                "source_zero_srt_ms": -recording["bcut_source_zero_ms"],
            }
    return {
        "schema_version": "finished-media-member.v1",
        "member_slug": member_slug(clip["cid"]),
        "recording_date": ctx.session["recording_date"],
        "maker": {"kind": "AGENT", "id": maker_id(ctx)},
        "files": {**files, "intro_receipt": str(ctx.clip_dir / "burn/intro_result.json")},
        "source": {
            "pieces": pieces,
            "context_srt": {"path": clip["mac_bcut_srt"],
                            "source_zero_srt_ms": clip["start_ms"] - bcut_source_zero_ms(clip)},
        },
        "selection_hook": selection_hook,
        "doubts": [{"id": d["id"], "grade": grade_of(d),
                    "cue_index": (cast(Sequence[int | None], numbers.get(str(d["id"]), [])) or [None])[0],
                    "final_text": d.get("final_text"),
                    "basis": (d.get("basis") or "").strip() or f"{d.get('state')}（CPA 按上下文判定）"}
                   for d in doubts],
        "cover_claims": {"lines": cover_lines},
        "speaker_claims": [],
        "duplicate_check": {"status": dedup.get("decision"), "block_upload": bool(dedup.get("block_upload")),
                            "reason": dedup.get("reason"), "result_json": str(ctx.clip_dir / "dedup/result.json")},
    }


def step8_package(ctx: Ctx) -> StepOutcome:
    rd = ctx.remote_dir
    needed = {
        "title": ctx.clip_dir / "title/title.txt",
        "cover": ctx.clip_dir / "cover/cover.png",
        "final_srt": ctx.clip_dir / "subs/final.srt",
        "speaker_srt": ctx.clip_dir / "speaker/speaker.srt",
        "tags": ctx.clip_dir / "tags/tags.json",
        "intro": ctx.clip_dir / "burn/intro_result.json",
        "dedup": ctx.clip_dir / "dedup/result.json",
        "cut_probe": ctx.clip_dir / "cut/probe.json",
    }
    missing = _missing(needed)
    if missing:
        return _fail("PACKAGE_INPUT_MISSING", f"missing products: {', '.join(missing)}")
    if bad := writing_canon.not_canonical(ctx):     # a writing of the table's words left without a verdict
        return _fail("WRITING_NOT_CANONICAL", bad)
    refused, guard_notes = package_guards.check(ctx, package := package_dir(ctx))     # before anything is built or deleted
    if refused:
        return refused
    (pkg := _pkg(ctx)).mkdir(parents=True, exist_ok=True)
    ctx.adapters.remote.get(f"{rd}/burn/final.mp4", pkg / "final.mp4")
    ctx.adapters.remote.get(f"{rd}/burn/final.ass", pkg / "final.ass")
    cut_local = ctx.clip_dir / "fmr_source" / "cut.mp4"
    if not cut_local.exists():
        cut_local.parent.mkdir(parents=True, exist_ok=True)
        ctx.adapters.remote.get(f"{rd}/cut/cut.mp4", cut_local)
    for name, src in (("final.srt", needed["final_srt"]), ("speaker.srt", needed["speaker_srt"]),
                      ("title.txt", needed["title"]), ("cover.png", needed["cover"]),
                      ("tags.json", needed["tags"])):
        shutil.copyfile(src, pkg / name)
    title = (pkg / "title.txt").read_text(encoding="utf-8").strip()
    try:
        description = description_text(title)
    except EnvelopeError as exc:  # a partner the reviewed registry does not know: the uploader refuses it as well
        return _fail("DESCRIPTION_ENVELOPE_UNRESOLVED", f"{exc!s:.200}")
    except Exception as exc:  # noqa: BLE001 - the uploader's builder is outside the lane: any failure of it stops
        # this step with its own reason (the package is not built), as the uploader would refuse it
        return _fail("DESCRIPTION_FAILED", f"{type(exc).__name__}: {exc!s:.200}")
    (pkg / "description.txt").write_text(description, encoding="utf-8")
    candidates = json.loads((ctx.clip_dir / "title/candidates.json").read_text(encoding="utf-8"))
    cover_lines = [str(x) for x in (candidates.get("cover_lines") or [])]
    selection_hook, hook_origin = _selection_context(ctx.state.data["clip"]["hook"], candidates.get("chosen"))
    duration_ms = int(round(float(json.loads(needed["cut_probe"].read_text(encoding="utf-8"))["format"]["duration"]) * 1000))
    files = {"video": str(pkg / "final.mp4"), "srt": str(pkg / "final.srt"), "speaker_srt": str(pkg / "speaker.srt"),
             "ass": str(pkg / "final.ass"), "cover": str(pkg / "cover.png"), "title": str(pkg / "title.txt"),
             "description": str(pkg / "description.txt")}
    dedup = cast(DedupeDetail, json.loads(needed["dedup"].read_text(encoding="utf-8")))
    member = _member(ctx, files=files, cover_lines=cover_lines, dedup=dedup, cut_local=cut_local,
                     duration_ms=duration_ms, selection_hook=selection_hook)
    member_path = pkg / f"{ctx.state.data['clip']['cid']}.member.json"
    member_path.write_text(json.dumps(member, ensure_ascii=False, indent=2), encoding="utf-8")

    if package.exists():          # a rewound round rebuilds the package (a sealed, published one was refused above)
        shutil.rmtree(package)
    init = _fmr("init", "--member", str(member_path), "--out-root", str(ctx.clip_dir / "fmr"))
    if init.returncode != 0:
        return _fail("FMR_INIT_REFUSED", (init.stdout + init.stderr)[-400:])
    if not package.is_dir():
        return _fail("FMR_PACKAGE_MISSING", f"init did not create {package}")
    check = _fmr("check", "--package", str(package), "--tags-from", str(ctx.clip_dir / "tags/tags.json"), *revision_authority.fmr_args(ctx))
    check_log = pkg / "fmr_check.log"
    check_log.write_text(check.stdout + check.stderr, encoding="utf-8")
    if check.returncode not in (0, 1):   # 1 = the lane's findings (recorded below); anything else = refused
        return _fail("FMR_CHECK_REFUSED", (check.stdout + check.stderr)[-400:])
    flags = _fmr_flags(check.stdout)
    products = {"package": str(pkg / "final.mp4"), "member": str(member_path), "fmr_check": str(check_log),
                "fmr_package": str(package), **revision_authority.record(ctx, pkg, guard_notes)}
    findings = package / "findings.md"
    if findings.exists():
        products["fmr_findings"] = str(findings)
    return StepOutcome(True, products=products,
                       note=f"FMR check rc={check.returncode}，记录 {len(flags)} 条 FLAG/FAIL（不拦）",
                       process=[f"成片包已组装：{title}",
                                f"本片看点来源：{hook_origin}",
                                "FMR init 完成，check 完成（结果写入 fmr_check.log；FLAG/FAIL 只记录）"]
                       + guard_notes + [f"FMR {line[:200]}" for line in flags[:20]])


# ----- review: one reading, fixes adopted in place, then attest/seal -----------------------------------------------
def _review_doubts(doubts: Sequence[ReviewDoubt], numbers: dict[str, list[int]]) -> list[ReviewPromptDoubt]:
    """What the reviewer sees of the doubt table: the rows a producer decided (not the unsuspicious BCUT cues), named by
    their final.srt numbers, with the decision, its confidence and basis and the candidates."""
    out: list[ReviewPromptDoubt] = []
    for d in doubts:
        if d.get("source") in {"NOT_SUSPICIOUS", "FILLER_KEPT"} and not doubt_policy.is_hold(d):
            continue
        row: ReviewPromptDoubt = {"id": d.get("id"), "final_cues": numbers.get(str(d.get("id")), []), "final_text": d.get("final_text"),
               "state": d.get("state"), "confidence": d.get("confidence"), "affects_meaning": d.get("affects_meaning"),
               "held": doubt_policy.is_hold(d), "basis": d.get("basis"), "reason": d.get("reason"),
               "current": d.get("current"), "readings": d.get("readings", {}),
               "reasons": d.get("reasons", []), "danmaku": d.get("danmaku", []),
               "asr_notes": d.get("asr_notes", []), "speaker_note": d.get("speaker_note"),
               "read_aloud": d.get("read_aloud"),
               "candidates": [cast(ReviewPromptCandidate, dict(c)) for c in d.get("candidates") or []]
               or [{"text": a} for a in d.get("alternatives") or []]}
        out.append(cast(ReviewPromptDoubt,
                        {k: v for k, v in row.items() if v not in (None, "", [])} | {"final_cues": row["final_cues"]}))
    return out


def _track_lines(cues: Sequence[subtitle_track.TrackCue], *, speakers: bool) -> str:
    return "\n".join(f"{i} {subtitle_track.fmt_ts(float(c['start_s']))} "
                     + (f"[{c.get('speaker') or '?'}] " if speakers else "") + subtitle_track.plain_text(c["text"])
                     for i, c in enumerate(cues, 1))


def _optional_json_object(path: Path) -> dict[str, Any]:
    """Historical cover evidence may be absent or unreadable; report that, without inventing a verdict."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _cover_review_materials(clip_dir: Path) -> tuple[str, str]:
    candidates = _optional_json_object(clip_dir / "title/candidates.json")
    lines = candidates.get("cover_lines")
    cover_text = (json.dumps(lines, ensure_ascii=False) if isinstance(lines, list) and lines
                  and all(isinstance(line, str) for line in lines) else "未提供（没有可用的封面字记录）")
    choose = _optional_json_object(clip_dir / "cover/cand/choose.json")
    selected, rows = choose.get("chosen"), choose.get("rows")
    matches = ([r for r in rows if isinstance(r, dict) and r.get("id") == selected]
               if isinstance(selected, str) and selected and isinstance(rows, list) else [])
    verdict = matches[0].get("verdict") if len(matches) == 1 else None
    image_review = (json.dumps({"chosen": selected, "verdict": verdict}, ensure_ascii=False, indent=1)
                    if isinstance(verdict, dict) and verdict else "未提供（没有可用的选中图片判读记录）")
    return cover_text, image_review


def _review_materials(ctx: Ctx) -> tuple[str, str, str]:
    """One factual material rendering for the ordinary and focused reviews."""
    pkg = _pkg(ctx)
    package = package_dir(ctx)
    doubts, numbers = current_doubts(ctx)
    linked = linked_session(ctx.session)
    track = subtitle_track.read_track(ctx.clip_dir)
    names = participant_description(ctx.session)
    if linked:
        session_line = (f"本场是联动，参与者：{names}。字幕带说话人标签：[{speaker_v8.HOST}] 是李豆沙本人，"
                        f"[{speaker_v8.GUEST_LABEL}] 是不是李豆沙的另一个人（不辨是谁）。要按上下文复审说话人。")
        speaker_item = ("2. 说话人：标签来自声音证据（第 4 步已按语境复核过声音打平的句子），是默认值。只有这一句里有明确的"
                        "内容证据才写进 fixes.speakers：说话人自称、自报身份，或者说的是只有这一方才会说的、关于自己的事；"
                        f"evidence 写「{'」或「'.join(speaker_v8.EVIDENCE_KINDS)}」，quote 从这一句原文里逐字摘出说话人说自己"
                        "的那几个字。称呼某人、谁在提问谁在回答、前后句是谁、评论或调侃的口吻、用第三人称提到某人、念弹幕，"
                        "都可能另有解释，不能据此改。拿不准不写（说话人不确定不拦）。")
    else:
        session_line = f"本场只有主播一人（{names}），所有台词都是主播，不分说话人。"
        speaker_item = "2. 说话人：单人场次，不查。"
    findings = (package / "findings.md").read_text(encoding="utf-8") if (package / "findings.md").exists() else ""
    hook = ctx.state.data["clip"]["hook"]
    chosen = None
    if not hook.strip():
        candidates = json.loads((ctx.clip_dir / "title/candidates.json").read_text(encoding="utf-8"))
        chosen = candidates.get("chosen")
    selection_hook, hook_origin = _selection_context(hook, chosen)
    cover_text, image_review = _cover_review_materials(ctx.clip_dir)
    materials = "\n\n".join([
        "## FMR 检查（findings.md，仅供参考）\n" + findings,
        f"## 本片看点（来源：{hook_origin}）\n" + selection_hook,
        "## 标题\n" + (pkg / "title.txt").read_text(encoding="utf-8"),
        "## 封面字（title/candidates.json）\n" + cover_text,
        "## 选中封面既有 CPA 图像判读（cover/cand/choose.json；本次文字复审未独立看图）\n" + image_review,
        "## 简介\n" + (pkg / "description.txt").read_text(encoding="utf-8"),
        "## 标签\n" + json.dumps(json.loads((pkg / "tags.json").read_text(encoding="utf-8")).get("final_tags"),
                                ensure_ascii=False),
        "## 字幕疑点（final_cues 是最终字幕里的序号）\n" + json.dumps(_review_doubts(doubts, numbers), ensure_ascii=False,
                                                                indent=1),
        "## 最终字幕（序号 时间 " + ("[说话人] " if linked else "") + "文字）\n" + _track_lines(track, speakers=linked),
    ])
    return session_line, speaker_item, materials


def _with_review_materials(prompt: str, materials: str) -> str:
    # Insert evidence last so literal template tokens in a hook, reason, or subtitle are never expanded.
    before, marker, after = prompt.rpartition("{materials}")
    if not marker:
        raise ValueError("review prompt missing {materials}")
    return before + materials + after


def review_prompt(ctx: Ctx) -> str:
    """Review the current package using the shared factual materials and hold rule."""
    session_line, speaker_item, materials = _review_materials(ctx)
    rules, _sources = title_authority.review_rules()   # U74: the skill's title rules, read now and put in last
    session_marker = "\x00SESSION\x00"
    context_marker = "\x00COMPILATION\x00"
    prompt = _render("review.md", session=session_marker, speaker_item=speaker_item, hold_rule=doubt_policy.HOLD_RULE,
                     compilation_context=context_marker, guest=speaker_v8.GUEST_LABEL,
                     host=speaker_v8.HOST).replace("{title_rules}", rules).replace("{cover_fix}", cover_import.review_slot(ctx))
    from .step6_cover import _compilation_context

    prompt = prompt.replace(session_marker, session_line).replace(context_marker, _compilation_context(ctx))
    return _with_review_materials(prompt, materials)


def review_held_prompt(ctx: Ctx, held_ids: Sequence[str]) -> str:
    """Ask only for the missing legacy dispositions, using the same factual material rendering."""
    session_line, speaker_item, materials = _review_materials(ctx)
    session_marker = "\x00SESSION\x00"
    context_marker = "\x00COMPILATION\x00"
    prompt = _render("review_held.md", session=session_marker, speaker_item=speaker_item,
                     hold_rule=doubt_policy.HOLD_RULE, compilation_context=context_marker,
                     guest=speaker_v8.GUEST_LABEL, host=speaker_v8.HOST)
    targets = "## 本次只处置的疑点 ID\n" + json.dumps(list(held_ids), ensure_ascii=False)
    from .step6_cover import _compilation_context

    prompt = prompt.replace(session_marker, session_line).replace(context_marker, _compilation_context(ctx))
    return _with_review_materials(prompt, targets + "\n\n" + materials)


def apply_fixes(ctx: Ctx, review: ReviewAnswer) -> ReviewApplied:
    """Apply the review's fixes in place, once: subtitle text and speakers on the one cue list (final.srt and
    speaker.srt written together, subtitle_track), the title (维护者's title rules still apply), the tags.  A cue named
    by a number that is not in the final subtitles, an empty text, an unknown speaker label or a title that breaks
    维护者's rules is skipped with the reason.  The counts are what actually changed in the files (a fix equal to the
    current text is not a change).  Reviewer holds are written into the doubt table under doubt_policy.HOLD_RULE."""
    changed: ReviewApplied = {"subtitles": 0, "speakers": 0, "title": False, "tags": False, "holds": 0,
                              "decisions": 0, "skipped": [], "cpa": []}
    review, dissent = review_owner.split(ctx, review, changed["skipped"])    # the owner's subtitles and title: not the review's
    if dissent is not None:
        changed["dissent"] = dissent
        dissent_register.add(ctx.state, dissent)      # kept in the clip's state until disposed of
    fixes = review.get("fixes") or {}
    track = subtitle_track.read_track(ctx.clip_dir)
    linked = linked_session(ctx.session)
    has_speakers = (ctx.clip_dir / "speaker/speaker.srt").exists() and all(c.get("speaker") for c in track)
    edited = _subtitle_edits(fixes.get("subtitles") or [], track, changed["skipped"])
    if linked and has_speakers:
        changed["speakers"] = _speaker_edits(fixes.get("speakers") or [], track, edited, changed["skipped"])
    if edited:
        changed["subtitles"] = _write_edits(ctx, track, edited, linked=linked, has_speakers=has_speakers)
        if adopted := review_owner.adopted_lines(ctx, track, edited):
            changed["adopted"] = adopted
    hold_rows = [h for h in review.get("holds") or [] if isinstance(h, dict)]
    if hold_rows:
        changed["holds"] = _add_review_holds(ctx, hold_rows, track, changed["skipped"])
    if "doubt_decisions" in review:
        changed["decisions"] = _apply_doubt_decisions(ctx, review["doubt_decisions"], changed["skipped"])
    changed["title"] = _title_fix(ctx, str(fixes.get("title") or "").strip(), changed["skipped"], changed["cpa"])
    changed["tags"] = _tags_fix(ctx, fixes["tags"]) if "tags" in fixes else False
    return changed


def _speaker_edits(fixes: Sequence[ReviewFix | None], track: Sequence[subtitle_track.TrackCue],
                   edited: dict[int, subtitle_track.TrackCue],
                   skipped: list[str]) -> int:
    """The review's speaker fixes, added to `edited`; returns how many speakers changed.  The acoustic label stands
    without content evidence (TASK-18)."""
    for fix in fixes:
        n = _cue_number((fix or {}).get("cue_id"), len(track))
        label = str((fix or {}).get("speaker") or "").strip()
        if n is None or label not in {speaker_v8.HOST, speaker_v8.GUEST_LABEL}:
            skipped.append(f"speaker fix not applied (no such cue or unknown label): {fix!r}"[:160])
            continue
        why = speaker_v8.content_evidence(fix, subtitle_track.plain_text(track[n - 1]["text"]))
        if track[n - 1].get("speaker") != label and why:
            skipped.append(f"speaker fix not applied ({why}): {fix!r}"[:160])
            continue
        if track[n - 1].get("speaker") != label:
            edited[n] = cast(subtitle_track.TrackCue, dict(edited.get(n, track[n - 1]), speaker=label))
        elif n in edited:
            cue = cast(subtitle_track.TrackCue, dict(edited[n], speaker=label))
            if cue["text"] == track[n - 1]["text"]:
                edited.pop(n)
            else:
                edited[n] = cue
    return sum(c.get("speaker") != track[n - 1].get("speaker") for n, c in edited.items())


def _write_edits(ctx: Ctx, track: Sequence[subtitle_track.TrackCue], edited: dict[int, subtitle_track.TrackCue], *, linked: bool,
                 has_speakers: bool) -> int:
    """Write the edited cues into the one cue list (rewritten texts canoned and shaped, the release shape over the
    whole track) and record the text fixes in the doubt table.  Returns how many cue texts changed."""
    texts = {n: c["text"] for n, c in edited.items() if c["text"] != track[n - 1]["text"]}
    _record_text_fixes(ctx, texts, track)   # before the renumbering: the doubt rows that own these cues
    out: list[subtitle_track.TrackCue] = []
    for n, cue in enumerate(track, 1):
        if n in texts:
            out.extend(normalize.shape_cues([edited[n]]))    # canon and shape the new text; the track's shape below
        else:
            out.append(edited.get(n, cue))
    shaped, lone = normalize.release_shape(out, one_speaker=not linked)
    subtitle_track.write_track(ctx.clip_dir, shaped, speakers=has_speakers)
    from .stage_impl import record_lone

    record_lone(ctx.clip_dir, lone, fresh=False)
    return len(texts)


def _title_fix(ctx: Ctx, title: str, skipped: list[str], cpa_calls: list[CpaEvent]) -> bool:
    """The review's title, canoned, when it differs from the current one and passes what step 6's title passed (TASK-25:
    the review's title no longer skips them): 维护者's banned words, the publish gate, the quote check against the
    final subtitles with speakers, and the skill's judge on the title with the package's cover lines."""
    from src.autoslice.title_quote_check import check_title_quotes

    from .step6_title import publish_gate_violations, title_inputs, title_judge

    current_title = (ctx.clip_dir / "title/title.txt").read_text(encoding="utf-8").strip()
    if not title:
        return False
    canon_title = normalize.canon_text(title)[0]
    broken = gates.title_banned(canon_title)
    if broken:
        skipped.append(f"title breaks 维护者's title rules {broken}")
        return False
    if canon_title == current_title:
        return False
    gate = publish_gate_violations(canon_title) + (["placeholder_identity"] if placeholder_as_person(ctx.session, canon_title) else [])
    if gate:
        skipped.append(f"review title fails the publish gate {gate}")
        return False
    names = participant_names(ctx.session)
    _track, lines, srt_text = title_inputs(ctx, names)
    quotes = check_title_quotes(canon_title, srt_text)
    if quotes["status"] != "PASS":
        skipped.append(f"review title fails the quote check {quotes['reason_codes']}")
        return False
    cands_path = ctx.clip_dir / "title/candidates.json"
    cover = (json.loads(cands_path.read_text(encoding="utf-8")).get("cover_lines") or []) if cands_path.exists() else []
    judged = title_judge(ctx, "review-title-judge", lines, canon_title, cover, cpa_calls)
    if isinstance(judged, StepOutcome):
        skipped.append(f"review title not judged (CPA call failed): {judged.note}"[:200])
        return False
    if judged["overall"] != "PASS":
        skipped.append("review title fails the skill judge: " + "；".join(judged["fail_reasons"])[:300])
        return False
    (ctx.clip_dir / "title/title.txt").write_text(canon_title + "\n", encoding="utf-8")
    return True


def _tags_fix(ctx: Ctx, tags: Any) -> bool:
    from .step7_tags import apply_review_tags
    return apply_review_tags(ctx, tags)


def _record_text_fixes(ctx: Ctx, texts: dict[int, str], track: Sequence[subtitle_track.TrackCue]) -> None:
    """The doubt table follows the subtitles: a doubt row whose final cue the review rewrote carries the new text, and
    already-held rows keep their uncertainty until a valid explicit final decision says otherwise."""
    if not texts:
        return
    path = ctx.clip_dir / "subs/doubts.json"
    doubts = cast(list[ReviewDoubt], json.loads(path.read_text(encoding="utf-8")))
    numbers = doubt_numbers(ctx, doubts)
    for d in doubts:
        mine = [n for n in numbers.get(str(d["id"]), []) if n in texts]
        if mine:
            before = d.get("final_text")
            was_held = doubt_policy.is_hold(d)
            owned = numbers[str(d["id"])]
            d["final_text"] = "".join(subtitle_track.plain_text(texts.get(n, track[n - 1]["text"])) for n in owned)
            if not was_held:
                d["confidence"] = "高"
            d["review_note"] = f"复审改写（原为「{before}」）"
            if d.get("state") != "UNRESOLVED" or not was_held:
                d["state"] = "ADOPT"
    path.write_text(json.dumps(doubts, ensure_ascii=False, indent=2), encoding="utf-8")


def _apply_doubt_decisions(ctx: Ctx, decisions: object, skipped: list[str]) -> int:
    """Apply only unambiguous, fully specified CPA judgments to the existing doubt rows."""
    if not isinstance(decisions, list):
        skipped.append("doubt decisions not applied (expected a list)")
        return 0
    path = ctx.clip_dir / "subs/doubts.json"
    doubts = cast(list[ReviewDoubt], json.loads(path.read_text(encoding="utf-8")))
    by_id = {d["id"]: d for d in doubts}
    selected = {d["id"]: d for d in current_doubts(ctx)[0]}
    ids = [d.get("id") for d in decisions if isinstance(d, dict) and isinstance(d.get("id"), str)]
    count = 0
    for decision in decisions:
        if not isinstance(decision, dict):
            skipped.append(f"doubt decision not applied (not an object): {decision!r}"[:160])
            continue
        did, confidence = decision.get("id"), decision.get("confidence")
        affects, reason = decision.get("affects_meaning"), decision.get("reason")
        if (not isinstance(did, str) or not did or did not in by_id or ids.count(did) != 1
                or sum(d.get("id") == did for d in doubts) != 1
                or confidence not in ("高", "低") or type(affects) is not bool
                or not isinstance(reason, str) or not reason.strip()):
            skipped.append(f"doubt decision not applied (invalid or ambiguous): {decision!r}"[:160])
            continue
        row = by_id[did]
        row["confidence"] = confidence
        row["affects_meaning"] = affects
        selected_text = selected[did].get("final_text")
        if row.get("state") == "UNRESOLVED" and confidence == "高" and str(selected_text or "").strip():
            row["state"] = "FIT_CONTEXT"
            row["final_text"] = selected_text
        row["review_note"] = f"复审最终判断：{reason.strip()}"
        count += 1
    if count:
        path.write_text(json.dumps(doubts, ensure_ascii=False, indent=2), encoding="utf-8")
    return count


def _add_review_holds(ctx: Ctx, holds: Sequence[ReviewHold], track: Sequence[subtitle_track.TrackCue],
                      skipped: list[str]) -> int:
    """A sentence the reviewer finds undecided and meaning-changing is held under the same rule as step 3's: the doubt
    rows of that final cue are marked low confidence and affecting the meaning, with the reviewer's reason; a cue with
    no doubt row gets one.  Returns the number of cues held."""
    path = ctx.clip_dir / "subs/doubts.json"
    doubts = cast(list[ReviewDoubt], json.loads(path.read_text(encoding="utf-8")))
    numbers = doubt_numbers(ctx, doubts)
    owner = {n: did for did, ns in numbers.items() for n in ns}
    by_id = {str(d["id"]): d for d in doubts}
    count = 0
    for hold in holds:
        n = _cue_number(hold.get("cue_id"), len(track))
        if n is None:
            skipped.append(f"hold not applied (no such cue): {hold!r}"[:160])
            continue
        reason = str(hold.get("reason") or "").strip() or "复审：定不下且影响意思"
        row = by_id.get(owner.get(n, ""))
        if row is None:
            cue = track[n - 1]
            row = {"id": f"rv{n:03d}", "cue_id": None, "start_s": cue["start_s"], "end_s": cue["end_s"],
                   "current": subtitle_track.plain_text(cue["text"]), "state": "KEEP",
                   "final_text": subtitle_track.plain_text(cue["text"]), "source": "CPA_REVIEW"}
            doubts.append(row)
            by_id[row["id"]] = row
        row["confidence"] = "低"
        row["affects_meaning"] = True
        row["review_note"] = f"复审：{reason}"
        count += 1
    path.write_text(json.dumps(doubts, ensure_ascii=False, indent=2), encoding="utf-8")
    return count


def _focused_review(ctx: Ctx, review: ReviewAnswer, held_ids: Sequence[str]) -> tuple[ReviewAnswer, list[str]]:
    """Keep a focused answer within the held IDs and their own final cues."""
    doubts, numbers = current_doubts(ctx)
    targets = set(held_ids)
    allowed_cues = {n for d in doubts if d["id"] in targets for n in numbers.get(d["id"], [])}
    count = len(subtitle_track.read_track(ctx.clip_dir))
    skipped: list[str] = []
    raw_fixes = review.get("fixes") or {}
    fixes: ReviewFixes = {}
    if not isinstance(raw_fixes, dict):
        skipped.append("focused fixes not applied (expected an object)")
        raw_fixes = {}
    for key in ("subtitles", "speakers"):
        rows = raw_fixes.get(key) or []
        if not isinstance(rows, list):
            skipped.append(f"focused {key} fixes not applied (expected a list)")
            continue
        selected: list[ReviewFix | None] = []
        for row in rows:
            if isinstance(row, dict) and _cue_number(row.get("cue_id"), count) in allowed_cues:
                selected.append(cast(ReviewFix, row))
            else:
                skipped.append(f"focused {key} fix outside the held cues: {row!r}"[:160])
        fixes[key] = selected
    if raw_fixes.get("title") or raw_fixes.get("tags") or review.get("holds"):
        skipped.append("focused title, tags, or new holds not applied (outside the held scope)")
    scoped: ReviewAnswer = {"fixes": fixes, "notes": review.get("notes") or [],
                            "review_md": review.get("review_md")}
    if "doubt_decisions" in review:
        decisions: object = review["doubt_decisions"]
        if isinstance(decisions, list):
            keep = []
            for decision in decisions:
                if (isinstance(decision, dict) and isinstance(decision.get("id"), str)
                        and decision["id"] in targets):
                    keep.append(decision)
                else:
                    skipped.append(f"focused decision outside the held IDs: {decision!r}"[:160])
            scoped["doubt_decisions"] = cast(list[ReviewDoubtDecision], keep)
        else:
            skipped.append("focused doubt decisions not applied (expected a list)")
    return scoped, skipped


def _combine_reviews(main: ReviewAnswer, focused: ReviewAnswer) -> ReviewAnswer:
    """Apply the later focused answer over the main answer before touching the shared tracks."""
    combined = cast(ReviewAnswer, dict(main))
    fixes = cast(ReviewFixes, dict(main.get("fixes") or {}))
    later = focused.get("fixes") or {}
    # Keep both answers in order: the same shared validators decide which later fix can supersede the earlier one.
    fixes["subtitles"] = [*(fixes.get("subtitles") or []), *(later.get("subtitles") or [])]
    fixes["speakers"] = [*(fixes.get("speakers") or []), *(later.get("speakers") or [])]
    combined["fixes"] = fixes
    if "doubt_decisions" in focused:
        later_decisions = focused["doubt_decisions"]
        earlier = main.get("doubt_decisions")
        if isinstance(later_decisions, list) and isinstance(earlier, list):
            ids = {d.get("id") for d in later_decisions if isinstance(d, dict) and isinstance(d.get("id"), str)}
            combined["doubt_decisions"] = [d for d in earlier if not isinstance(d, dict)
                                          or not isinstance(d.get("id"), str) or d.get("id") not in ids] \
                                          + later_decisions
        else:
            combined["doubt_decisions"] = later_decisions
    return combined


def _review_record(review: ReviewAnswer, applied: ReviewApplied, call: CpaEvent, *, calls: list[CpaEvent],
                   focused: ReviewAnswer | None = None, held_ids: list[str] | None = None) -> ReviewRecord:
    notes = [n for n in review.get("notes") or [] if isinstance(n, dict)]
    # an answer in the older shape (verdict / findings) loses nothing: its findings are kept as notes, marked
    notes += [{"item": f.get("item", ""), "cue_id": f.get("cue_id", "-"),
               "text": f"（旧格式 {f.get('severity', '')}）{f.get('text', '')}"}
              for f in review.get("findings") or [] if isinstance(f, dict)]
    if focused is not None:
        notes = [{**n, "text": f"（既有复审历史）{n.get('text', '')}"} for n in notes]
        notes += [{**n, "text": f"（遗留疑点处置）{n.get('text', '')}"}
                  for n in focused.get("notes") or [] if isinstance(n, dict)]
    record: ReviewRecord = {"call": call.get("call_id"), "cpa": [*calls, *applied["cpa"]],
                            "applied": cast(ReviewCounts, {k: v for k, v in applied.items()
                                                           if k not in {"skipped", "cpa", "dissent", "adopted"}}),
                            "skipped": applied["skipped"], "notes": notes, "rebuild": False}
    review_owner.carry(record, applied)
    if held_ids is not None:
        record["held_ids"] = held_ids
    return record


def _write_review_md(ctx: Ctx, review: ReviewAnswer, record: ReviewRecord,
                     focused: ReviewAnswer | None = None) -> Path:
    """REVIEW.md = the reviewer's own report, then what the pipeline did with it (applied fixes, holds, notes), so the
    report bound into the receipt says that the fixes were adopted without a second reading."""
    doubts, _numbers = current_doubts(ctx)
    held = doubt_policy.holds(doubts)
    applied = record["applied"]
    if focused is None:
        lines = [str(review.get("review_md") or "").strip()]
    else:
        lines = ["## 既有复审（历史记录，未含本次遗留疑点处置）", str(review.get("review_md") or "").strip(),
                 "", "## 遗留疑点处置（仅指定疑点）", str(focused.get("review_md") or "").strip()]
    lines += ["", "## 流水线采用记录",
             f"- 本次执行实际变更：字幕 {applied['subtitles']} 条，说话人 {applied['speakers']} 条，"
             f"标题{'已改' if applied['title'] else '未改'}，标签{'已改' if applied['tags'] else '未改'}；复审新增定不下 "
             f"{applied['holds']} 句；明确处置疑点 {applied['decisions']} 条",
             *(f"- 未采用：{s}" for s in record["skipped"]),
             f"- 定不下且影响意思：{len(held)} 句"
             + ("" if not held else "：" + "；".join(f"{d['id']}「{d.get('final_text')}」" for d in held)),
             *(f"- 最终疑点判断 {d['id']}：confidence={d.get('confidence')}，"
               f"affects_meaning={d.get('affects_meaning')}；{d.get('review_note')}"
               for d in doubts if str(d.get("review_note") or "").startswith("复审最终判断：")),
             *(f"- 记录：{n.get('item', '')} 第 {n.get('cue_id', '-')} 句 {n.get('text', '')}" for n in record["notes"])]
    path = _pkg(ctx) / "REVIEW.md"
    path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
    return path


def _saved_review(record: ReviewRecord, call_id: str | None = None) -> CpaEvent | StepOutcome:
    """Recover the recorded review's complete terminal answer without a new prompt or cache mutation."""
    call = call_id if call_id is not None else record.get("call")
    matches = [event for event in record.get("cpa", []) if event.get("call_id") == call]
    if not isinstance(call, str) or not call or len(matches) != 1:
        return _fail("CPA_CALL_FAILED", "saved review call is missing or ambiguous")
    original = matches[0]
    cache_path = original.get("cache_path")
    if not isinstance(cache_path, str) or not cache_path:
        return _fail("CPA_CALL_FAILED", "saved review cache path is missing", cpa=[original])
    try:
        raw = json.loads(Path(cache_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return _fail("CPA_CALL_FAILED", f"saved review cache unreadable: {type(exc).__name__}", cpa=[original])
    if (not isinstance(raw, dict) or raw.get("complete") is not True or type(raw.get("rc")) is not int
            or raw["rc"] != 0 or not isinstance(raw.get("response_text"), str) or not raw["response_text"].strip()):
        return _fail("CPA_CALL_FAILED", "saved review cache is not a complete successful answer", cpa=[original])
    return cast("CpaEvent", {**raw, **original, "rc": raw["rc"], "complete": raw["complete"],
                             "response_text": raw["response_text"], "cached": True})


def _prepare_focused_call(ctx: Ctx, record: ReviewRecord, held_ids: list[str],
                          call_id: str, *, target_key: str = "held_ids",
                          addition: str = "") -> tuple[CpaEvent, bool] | StepOutcome | None:
    """Preserve or verify the focused call identity and its original question before dispatch."""
    targets_store = cast(MutableMapping[str, list[str]], record)
    existing = [c for c in record.get("cpa", []) if c.get("call_id") == call_id]
    prompt_path = ctx.clip_dir / "cpa" / f"{call_id}.prompt.txt"
    completion_path = ctx.clip_dir / "cpa" / f"{call_id}.completion.txt"
    if existing:
        if len(existing) != 1:
            return _fail("CPA_CALL_FAILED", "saved focused review call is ambiguous", cpa=existing)
        if target_key not in targets_store:
            targets = focused_retry.held_targets_from_prompt(prompt_path.read_bytes().decode("utf-8")) if prompt_path.is_file() else None
            if not targets:
                return _fail("CPA_CALL_FAILED", "saved focused review targets are missing", cpa=existing)
            targets_store[target_key] = targets
        elif focused_retry.valid_held_targets(targets_store[target_key]) is None:
            return _fail("CPA_CALL_FAILED", "saved focused review targets are invalid", cpa=existing)
        if prompt_path.is_file() and focused_retry.held_targets_from_prompt(prompt_path.read_bytes().decode("utf-8")) != targets_store[target_key]:
            return _fail("CPA_CALL_FAILED", "saved focused review targets disagree with prompt", cpa=existing)
        return existing[0], True
    else:
        if not held_ids and not prompt_path.exists() and not completion_path.exists():
            return None
        recovering_request = False
        if prompt_path.exists():
            recovering_request = True
            prompt = prompt_path.read_bytes().decode("utf-8")
            targets = focused_retry.held_targets_from_prompt(prompt)
            if not targets:
                return _fail("CPA_CALL_FAILED", "saved focused review prompt has no usable targets")
        elif completion_path.exists():
            return _fail("CPA_CALL_FAILED", "saved focused review prompt is missing")
        else:
            targets = focused_retry.valid_held_targets(held_ids)
            if targets is None:
                return _fail("CPA_CALL_FAILED", "focused review targets are invalid")
            prompt = review_held_prompt(ctx, targets) + addition
            _write_atomic(prompt_path, prompt)
        effort, _note = _effort_of("review")
        cache = _cpa_cache_path(ctx, prompt, effort=effort)
        event: CpaEvent = {"call_id": call_id, "model": CPA_JUDGE_MODEL, "effort": effort, "stream": True,
                           "purpose": "review", "file": str(ctx.clip_dir / "cpa" / call_id), "cache_path": str(cache)}
        targets_store[target_key] = targets
        record["cpa"].append(event)
        ctx.state.put(REVIEW_KEY, record)
        return event, recovering_request


def _focused_terminal(ctx: Ctx, record: ReviewRecord, held_ids: list[str], *, call_id: str = "review-held",
                      target_key: str = "held_ids", addition: str = "") -> CpaEvent | StepOutcome | None:
    """Reuse a complete terminal, or resume a prepared request with its original prompt and call identity."""
    prepared = _prepare_focused_call(ctx, record, held_ids, call_id, target_key=target_key, addition=addition)
    if prepared is None or isinstance(prepared, StepOutcome):
        return prepared
    event, recovering_request = prepared
    prompt_path = ctx.clip_dir / "cpa" / f"{call_id}.prompt.txt"
    cache_path = event.get("cache_path")
    if not isinstance(cache_path, str) or not cache_path:
        return _fail("CPA_CALL_FAILED", "focused review cache path is missing", cpa=[event])
    if Path(cache_path).exists() or event.get("complete") is True:
        return _saved_review(record, call_id)
    if "rc" in event and not focused_retry.may_resume_focused(event):
        return _fail("CPA_CALL_FAILED", event.get("stderr_tail") or str(event.get("reason") or "focused call failed"),
                     cpa=[event])
    if not prompt_path.is_file():
        return _fail("CPA_CALL_FAILED", "prepared focused review prompt is missing", cpa=[event])
    prompt = prompt_path.read_bytes().decode("utf-8")
    effort, _note = _effort_of("review")
    if (event.get("effort") != effort or event.get("model") != CPA_JUDGE_MODEL or event.get("stream") is not True
            or str(_cpa_cache_path(ctx, prompt, effort=effort)) != cache_path):
        return _fail("CPA_CALL_FAILED", "prepared focused review prompt or route changed", cpa=[event])
    if recovering_request and "rc" not in event:
        try:
            recovered = recover_cpa_terminal(ctx.adapters.remote, call_id=call_id, prompt_text=prompt,
                                             model=CPA_JUDGE_MODEL, effort=effort, remote_dir=ctx.remote_dir)
            if recovered is not None:
                answer = cast("CpaEvent", {**recovered, "cached": True, "purpose": "review"})
                _record_cpa_files(ctx, call_id, prompt, answer, Path(cache_path), only_missing=True)
                _cache_cpa_answer(Path(cache_path), answer, keep_http_log=True)
                index = next(i for i, c in enumerate(record["cpa"]) if c.get("call_id") == call_id)
                record["cpa"][index] = _log(answer)
                ctx.state.save()
                return answer
        except (OSError, ValueError, RuntimeError) as exc:
            return _fail("CPA_CALL_FAILED", f"focused terminal recovery failed: {exc}", cpa=[event])
    result = _ask_cpa(ctx, call_id, prompt, purpose="review")
    index = next(i for i, c in enumerate(record["cpa"]) if c.get("call_id") == call_id)
    record["cpa"][index] = _log(result)
    ctx.state.save()
    if result.get("complete") is not True or type(result.get("rc")) is not int or result["rc"] != 0:
        return _fail("CPA_CALL_FAILED", result.get("stderr_tail") or str(result.get("reason") or
                     "focused call has no complete terminal"), cpa=[result])
    return result


def _review_answer(call: CpaEvent, *, saved: bool) -> ReviewAnswer | StepOutcome:
    try:
        answer = cast(ReviewAnswer, _json_from_text(call["response_text"]))
        if not isinstance(answer, dict):
            raise ValueError("not an object")
    except ValueError:
        if saved:
            # _fail removes unparseable caches; recorded terminals must remain intact.
            return StepOutcome(False, reason="CPA_RESPONSE_UNPARSEABLE", note=call.get("file", ""), cpa=[_log(call)])
        return _fail("CPA_RESPONSE_UNPARSEABLE", call.get("file", ""), cpa=[call])
    return answer


def _replace_call(calls: list[CpaEvent], event: CpaEvent) -> list[CpaEvent]:
    logged = _log(event)
    return [logged if c.get("call_id") == event.get("call_id") else c for c in calls]


def _finish_rebuilt_review(ctx: Ctx, record: ReviewRecord, pkg: Path) -> StepOutcome | None:
    """Finish an adopted rebuild, or persist its end before handling a missing legacy disposition."""
    if not (record.get("rebuild") and ctx.state.data.get("rewind", {}).get("by") == "review"
            and (pkg / "REVIEW.md").is_file()):
        return None
    # A fresh review's own rebuild is not a second review.  An older interrupted rebuild may still have the
    # legacy missing disposition, so inspect its saved answer before taking the fast completion branch.
    has_focused = any(c.get("call_id") == "review-held" for c in record.get("cpa", []))
    needs_legacy = False
    if (not record.get("wide_held") and not record.get("fresh_rebuild") and not has_focused
            and doubt_policy.holds(current_doubts(ctx)[0])):
        saved_main = _saved_review(record)
        if isinstance(saved_main, StepOutcome):
            return saved_main
        saved_answer = _review_answer(saved_main, saved=True)
        if isinstance(saved_answer, StepOutcome):
            return saved_answer
        needs_legacy = "doubt_decisions" not in saved_answer
    if has_focused:
        stored_focused = _focused_terminal(ctx, record, [])
        if isinstance(stored_focused, StepOutcome):
            return stored_focused
        if stored_focused is None:
            return _fail("CPA_CALL_FAILED", "saved focused review is missing")
        answer = _review_answer(stored_focused, saved=True)
        if isinstance(answer, StepOutcome):
            return answer
    record["rebuild"] = False
    if not needs_legacy:
        record.pop("fresh_rebuild", None)
    ctx.state.save()
    if needs_legacy:
        return None
    # The earlier calls stay in this step's record even though rewind dropped the step entry.
    return _finish_or_wide(ctx, record,
                           ["复审修改已在重建的成片包里（重建未再次询问 CPA；调用记录保留原复审及已有遗留处置）"])


def _focused_answer(ctx: Ctx, record: ReviewRecord, review: ReviewAnswer,
                    had_focused: bool) -> tuple[CpaEvent, ReviewAnswer] | StepOutcome | None:
    """Read the existing focused answer or obtain the one missing legacy disposition."""
    doubts, _numbers = current_doubts(ctx)
    legacy_ids = ([d["id"] for d in doubts if doubt_policy.is_hold(d)]
                  if "doubt_decisions" not in review and not record.get("fresh_rebuild") else [])
    result = (None if record.get("fresh_rebuild") and not had_focused
              else _focused_terminal(ctx, record, legacy_ids))
    if isinstance(result, StepOutcome) or result is None:
        return result
    if not _cpa_ok(result):
        return _fail("CPA_CALL_FAILED", result.get("stderr_tail", ""), cpa=[result])
    parsed = _review_answer(result, saved=True)
    if isinstance(parsed, StepOutcome):
        return parsed
    return result, parsed


def _review_rewind(ctx: Ctx, applied: ReviewApplied, before_grades: dict[str, str]) -> tuple[str, ...]:
    """Rebuild only the products invalidated by the adopted review."""
    rewind: tuple[str, ...]
    if applied["subtitles"] or applied["speakers"]:
        rewind = REWIND_SUBTITLES
    elif applied["title"]:
        rewind = REWIND_TITLE
    elif applied["tags"]:
        rewind = REWIND_TAGS
    else:
        rewind = ()
    after_grades = {d["id"]: grade_of(d) for d in current_doubts(ctx)[0]}
    if before_grades != after_grades and not rewind:
        rewind = REWIND_TAGS     # the package's doubt grades changed: rebuild the package only
    return rewind


def _review_process(ctx: Ctx, record: ReviewRecord, applied: ReviewApplied, *, recovering: bool,
                    focused_call: CpaEvent | None, had_focused: bool) -> list[str]:
    """Describe the adopted review and the provenance of any focused disposition."""
    process = [f"CPA 复审（{CPA_JUDGE_MODEL}，一次）：字幕修改 {applied['subtitles']} 条，说话人修改 {applied['speakers']} 条，"
               f"标题{'已改' if applied['title'] else '未改'}，标签{'已改' if applied['tags'] else '未改'}，"
               f"新增定不下且影响意思 {applied['holds']} 句（修改直接采用）"]
    if recovering:
        process.insert(0, "复用已记录的 CPA 复审终态（cached，未再次请求；新补材料未进入原复审）")
    if focused_call is not None:
        process.append("遗留疑点缺项处置：" + ("复用原 focused 终态" if had_focused or focused_call.get("cached")
                                               else "新增一次 focused 请求") + "；范围仅指定疑点")
    process.append(f"CPA 明确处置既有疑点 {applied['decisions']} 条")
    process += [f"最终疑点判断 {d['id']}：confidence={d.get('confidence')}，"
                f"affects_meaning={d.get('affects_meaning')}；{d.get('review_note')}"
                for d in current_doubts(ctx)[0]
                if str(d.get("review_note") or "").startswith("复审最终判断：")]
    process += [f"未采用：{s}" for s in applied["skipped"]] + review_owner.process_lines(applied.get("dissent")) + applied.get("adopted", [])
    process += [f"复审记录：{n.get('item', '')} 第 {n.get('cue_id', '-')} 句 {str(n.get('text', ''))[:160]}"
                for n in record["notes"]]
    return process


def _wide_round_snapshot(ctx: Ctx, number: int, *, restore: bool) -> None:
    """Restore a partially applied review to its pre-application text before replaying its saved terminal."""
    root = ctx.clip_dir / f"listen/wide-held/round-{number}"
    for rel in ("subs/final.srt", "subs/doubts.json", "speaker/speaker.srt", "pkg/REVIEW.md"):
        current, saved = ctx.clip_dir / rel, root / rel
        if restore:
            if saved.exists():
                shutil.copyfile(saved, current)
        elif current.exists():
            saved.parent.mkdir(parents=True, exist_ok=True)
            if not saved.exists():
                shutil.copyfile(current, saved)


def _write_wide_review(ctx: Ctx, wide: WideProgress) -> None:
    before = ctx.clip_dir / "listen/wide-held/before/pkg/REVIEW.md"
    lines = [before.read_text(encoding="utf-8").rstrip(), "", "## 复审后加长窗口的疑点处置"]
    for number, row in enumerate(wide["rounds"], 1):
        if row.get("phase") == "applied":
            lines += [f"\n### 第 {number} 轮（仅指定疑点）", str(row.get("review_md") or "").strip(),
                      *(f"- 未采用：{s}" for s in row.get("skipped", []))]
    held = doubt_policy.holds(current_doubts(ctx)[0])
    lines.append(f"\n- 当前定不下且影响意思：{len(held)} 句" +
                 ("：" + "；".join(f"{d['id']}「{d.get('final_text')}」" for d in held) if held else ""))
    _write_atomic(_pkg(ctx) / "REVIEW.md", "\n".join(lines).strip() + "\n")


def _finish_or_wide(ctx: Ctx, record: ReviewRecord, process: list[str]) -> StepOutcome:
    """Use the existing focused review and rewind path for at most two post-review listening rounds."""
    from . import wide_held

    wide = record.get("wide_held")
    if wide and wide["rounds"] and wide["rounds"][-1].get("phase") == "applying":
        _wide_round_snapshot(ctx, len(wide["rounds"]), restore=True)
    doubts, numbers = current_doubts(ctx)
    held = doubt_policy.holds(doubts)
    if wide is None:
        if not held:
            wide_held.snapshot_before(ctx)
            return _finish(ctx, cpa=record["cpa"], process=process +
                           ["复审后宽窗：hold 0 → 0；窗口 0；MAI/MOSS/3.8/argon/CPA 调用 0/0/0/0/0"])
        wide = wide_held.prepare(ctx, held, numbers)
        record["wide_held"] = wide
        ctx.state.save()
    if not (_pkg(ctx) / "REVIEW.md").exists():
        _write_wide_review(ctx, wide)
    row: WideRound
    while held and (len(wide["rounds"]) < 2 or wide["rounds"][-1].get("phase") != "applied"):
        if wide["rounds"] and wide["rounds"][-1].get("phase") != "applied":
            row = wide["rounds"][-1]
            number = len(wide["rounds"])
        else:
            number = len(wide["rounds"]) + 1
            row = {"phase": "collecting", "held_ids": [str(d["id"]) for d in held]}
            wide["rounds"].append(row)
            ctx.state.save()
        held_ids = row["held_ids"]
        try:
            addition, counts = wide_held.collect(ctx, wide, held_ids, number)
        except wide_held.WideListeningError as exc:
            return _fail(exc.reason, str(exc), cpa=record["cpa"])
        call_id = f"review-wide-held-{number}"
        target_key = f"wide_held_ids_{number}"
        prior = next((c for c in record["cpa"] if c.get("call_id") == call_id and c.get("complete") is True), None)
        call = _focused_terminal(ctx, record, held_ids, call_id=call_id, target_key=target_key, addition=addition)
        if isinstance(call, StepOutcome):
            return call
        if call is None:
            return _fail("CPA_CALL_FAILED", "wide focused review is missing")
        answer = _review_answer(call, saved=True)
        if isinstance(answer, StepOutcome):
            return answer
        if row.get("phase") != "applying":
            _wide_round_snapshot(ctx, number, restore=False)
            row["phase"] = "applying"
            ctx.state.save()
        before_grades = {d["id"]: grade_of(d) for d in current_doubts(ctx)[0]}
        scoped, skipped = _focused_review(ctx, answer, held_ids)
        applied = apply_fixes(ctx, scoped)
        applied["skipped"].extend([*skipped, *review_owner.skipped_lines(applied)])
        process.extend(applied.get("adopted", []))
        review_owner.carry(record, applied)
        after = len(doubt_policy.holds(current_doubts(ctx)[0]))
        rewind = _review_rewind(ctx, applied, before_grades)
        original_call = prior if prior is not None else call
        was_cached = bool(original_call.get("cached"))
        row.update({"phase": "applied", "after_holds": after, "counts": counts,
                    "review_md": answer.get("review_md"), "skipped": applied["skipped"],
                    "cpa_cached": was_cached})
        record["cpa"] = _replace_call(record["cpa"], original_call)
        wide["pending_rewind"] = list(rewind)
        record["rebuild"] = bool(rewind)
        _write_wide_review(ctx, wide)
        ctx.state.save()
        process.append(f"复审后宽窗第 {number} 轮：hold {len(held_ids)} → {after}；窗口 {counts['windows']}；"
                       f"MAI/MOSS/3.8/argon 调用 {counts['mai']}/{counts['moss']}/{counts['38m']}/{counts['argon']}；"
                       f"CPA {'缓存' if was_cached else '原始新调用'} 1 次"
                       + ("（续跑复用终态，未重发）" if prior is not None else ""))
        if rewind:
            return StepOutcome(True, products={"review": str(_pkg(ctx) / "REVIEW.md")}, cpa=record["cpa"],
                               rewind=rewind, note="宽窗复审修改已采用，重建受影响成片", process=process)
        doubts, numbers = current_doubts(ctx)
        held = doubt_policy.holds(doubts)
    process.append(f"复审后宽窗汇总：hold {wide['before_holds']} → {len(held)}；完成 {len(wide['rounds'])} 轮")
    return _finish(ctx, cpa=record["cpa"], process=process)


def step9_review(ctx: Ctx) -> StepOutcome:
    pkg = _pkg(ctx)
    package = package_dir(ctx)
    if not package.is_dir() or not (pkg / "final.srt").exists():
        return _fail("PACKAGE_INPUT_MISSING", "step 8 package is missing")
    ctx.state.data.pop("revisions", None)          # the old revision counter (TASK-8 states): no longer used
    record = ctx.state.data.get(REVIEW_KEY)
    if record:
        finished = _finish_rebuilt_review(ctx, record, pkg)
        if finished is not None:
            return finished
        wide = record.get("wide_held")
        if wide:
            if record.get("rebuild") and wide.get("pending_rewind"):
                return StepOutcome(True, products={"review": str(pkg / "REVIEW.md")}, cpa=record["cpa"],
                                   rewind=tuple(wide["pending_rewind"]), note="恢复宽窗复审后的重建")
            return _finish_or_wide(ctx, record, ["恢复复审后宽窗的已有进度"])
    recovering = bool(record)
    rec = _saved_review(record) if record else _ask_cpa(ctx, "review", review_prompt(ctx), purpose="review")
    if isinstance(rec, StepOutcome):
        return rec
    if not _cpa_ok(rec):
        return _fail("CPA_CALL_FAILED", rec.get("stderr_tail", ""), cpa=[rec])
    review = _review_answer(rec, saved=recovering)
    if isinstance(review, StepOutcome):
        return review
    focused: ReviewAnswer | None = None
    focused_call: CpaEvent | None = None
    had_focused = bool(record and any(c.get("call_id") == "review-held" for c in record.get("cpa", [])))
    if record:
        result = _focused_answer(ctx, record, review, had_focused)
        if isinstance(result, StepOutcome):
            return result
        if result is not None:
            focused_call, focused = result
    before_grades = {d["id"]: grade_of(d) for d in current_doubts(ctx)[0]}
    if focused is not None and record:
        scoped, skipped = _focused_review(ctx, focused, record.get("held_ids", []))
        applied = apply_fixes(ctx, _combine_reviews(review, scoped))
        applied["skipped"].extend(skipped)
    else:
        applied = apply_fixes(ctx, review)
    calls = list(record.get("cpa", [])) if record else [_log(rec)]
    calls = _replace_call(calls, rec)
    if focused_call is not None:
        calls = _replace_call(calls, focused_call)
    record = _review_record(review, applied, rec, calls=calls, focused=focused,
                            held_ids=record.get("held_ids") if record and focused is not None else None)
    _write_review_md(ctx, review, record, focused)
    process = _review_process(ctx, record, applied, recovering=recovering, focused_call=focused_call,
                              had_focused=had_focused)
    rewind = _review_rewind(ctx, applied, before_grades)
    ctx.state.put(REVIEW_KEY, record)
    if rewind:
        record["rebuild"] = True
        if not recovering:
            record["fresh_rebuild"] = True
        ctx.state.save()
        return StepOutcome(True, products={"review": str(pkg / "REVIEW.md")}, cpa=record["cpa"], rewind=rewind,
                           note=f"复审修改已采用，重建 {', '.join(r for r in rewind if r != 'review')}", process=process)
    ctx.state.save()
    return _finish_or_wide(ctx, record, process)


def _finish(ctx: Ctx, *, cpa: list[CpaEvent], process: list[str]) -> StepOutcome:
    """Attest PASS and seal unless doubt_policy.HOLD_RULE holds a sentence (a held package is finished, not sealed).  The
    review's objections to the owner's edits (dissent_register) do not stop it: PROCESS.md lists them, kept by default."""
    pkg = _pkg(ctx)
    package = package_dir(ctx)
    review_md = pkg / "REVIEW.md"
    products = {"review": str(review_md)}
    dissent_register.write_section(ctx.state, review_md)
    process = process + dissent_register.process_lines(ctx.state)
    held = doubt_policy.holds(current_doubts(ctx)[0])
    if held:
        process = process + [f"定不下且影响意思 {len(held)} 句：成片包已完成，不 attest、不封存，保留待处理："
                             + "，".join(f"{d['id']}「{d.get('final_text')}」" for d in held)]
        return StepOutcome(True, products=products, cpa=cpa, process=process,
                           note=f"复审完成；定不下且影响意思 {len(held)} 句，未封存")
    attest = _fmr("attest", "--package", str(package), "--review", str(review_md),
                  "--reviewer-id", reviewer_id(), "--verdict", "PASS", "--reviewer-kind", "AGENT")
    process = process + [f"FMR attest（reviewer {reviewer_id()}，制作人 {maker_id(ctx)}）rc={attest.returncode}"]
    if attest.returncode == 2:
        return _fail("FMR_ATTEST_REFUSED", (attest.stdout + attest.stderr)[-400:], cpa=cpa, process=process)
    if attest.returncode != 0:
        return StepOutcome(True, products=products, cpa=cpa,
                           note="FMR attest 回执未通过（记录，未封存）", process=process + [attest.stdout[-300:]])
    seal = _fmr("seal", "--package", str(package), *revision_authority.fmr_args(ctx))
    process.append(f"FMR seal rc={seal.returncode}：{(seal.stdout + seal.stderr).strip()[-300:]}")
    if seal.returncode == 2:
        return _fail("FMR_SEAL_REFUSED", (seal.stdout + seal.stderr)[-400:], cpa=cpa, process=process)
    manifest = package / "review_manifest.json"
    if manifest.exists():
        products["review_manifest"] = str(manifest)
    sealed = seal.returncode == 0
    return StepOutcome(True, products=products, cpa=cpa,
                       note=("FMR 已封存（review_manifest.json）" if sealed
                             else "FMR seal 未通过（BLOCK 已记录，未封存，不能上传）"),
                       process=process)


def step10_upload(ctx: Ctx) -> StepOutcome:
    """Only reached with --upload and a non-empty quote (runner).  The whole step is step10_run.run."""
    return step10_run.run(ctx, package_dir(ctx))
