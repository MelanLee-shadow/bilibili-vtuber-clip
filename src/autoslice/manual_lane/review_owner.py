"""Step 9 for what the channel owner decided himself: the subtitles of a clip imported with `--decided-by owner`
(step_import.owner_decided), the title body he picked for a clip (title_verdicts approved_title_body) and an imported,
finished cover (cover_import: its picture and lines).

His authority covers only the sentences he edited (imported["owner_edited"], owner_edits; a push without a baseline: all
of them).  The review's subtitle, speaker and hold proposals for one of those, a doubt decision on one, its title fix
for a picked title and its cover fix for an imported cover (the image is never replaced) are not adopted: each becomes an
objection (Dissent) that dissent_register keeps in the clip's state, in full, with the default disposition "keep his writing"
(the owner-final rule): it does not stop the clip.  The same proposals for a sentence he did not touch are adopted like
any clip's (PROCESS names them).  The one place that decides is `split`, called at the top of stage_package.apply_fixes, so
the main review, the focused review, the wide post-review rounds and every replay of a saved answer all pass through it.
Tag fixes are never touched.  Also holds the review-answer cue helpers the split shares with apply_fixes.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from functools import partial
from typing import TYPE_CHECKING, Any, TypedDict, cast

from src.autoslice.title_editorial_method import AuthorityError

from . import cover_import, normalize, owner_edits, subtitle_track, title_verdicts
from .runner import Ctx
from .step_import import IMPORT_DIR, imported, owner_decided

if TYPE_CHECKING:
    from .stage_package import ReviewAnswer, ReviewApplied, ReviewFix, ReviewFixes, ReviewRecord

SECTION = "## 复审的异议（频道主已定下的内容，按频道主定稿规则保留）"
KINDS = {"text": "改字", "speaker": "改说话人", "hold": "保留项", "decision": "疑点裁决", "title": "改标题", "cover": "改封面"}
NO_REASON = "复审未单独写理由（见 REVIEW.md 复审说明）"


class Dissent(TypedDict):
    """One review objection to what the owner decided, not adopted."""
    kind: str            # text | speaker | hold | decision | title | cover (KINDS)
    cue: int | None      # final.srt number of the sentence; None for a doubt decision, a title and a cover
    text: str            # the sentence as it stands (a decision: the doubt id; a title: the current title; a cover: its lines)
    proposed: str        # what the review wanted, for people: the new text, "old → new" speaker, the hold reason, the decision, the title, the cover lines
    value: str           # the same as a value `resolve-dissent --apply` can use: the new text, the new speaker label
    reason: str          # why the review wanted it


def cue_number(raw: Any, count: int) -> int | None:
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return None
    return n if 1 <= n <= count else None


def subtitle_edits(fixes: Sequence[ReviewFix | None], track: Sequence[subtitle_track.TrackCue],
                   skipped: list[str]) -> dict[int, subtitle_track.TrackCue]:
    """The review's text fixes as edited cues by final cue number (a fix equal to the current text is not an edit)."""
    edited: dict[int, subtitle_track.TrackCue] = {}
    for fix in fixes:
        n = cue_number((fix or {}).get("cue_id"), len(track))
        text = str((fix or {}).get("text") or "").strip()
        if n is None or not text:
            skipped.append(f"subtitle fix not applied (no such cue or empty text): {fix!r}"[:160])
            continue
        if subtitle_track.plain_text(track[n - 1]["text"]) != text:
            edited[n] = cast(subtitle_track.TrackCue, dict(track[n - 1], text=text))
        else:
            edited.pop(n, None)   # a later valid choice of the current text cancels an earlier proposed edit
    return edited


def _title_picked(ctx: Ctx, skipped: list[str]) -> bool:
    """Whether the owner picked this clip's title body (step 6's lookup).  An asset that cannot be read counts as no
    pick, and the reason goes to `skipped` (PROCESS) rather than failing the review."""
    try:
        return title_verdicts.load_verdicts().approved(str(ctx.state.data["clip"].get("cid") or "")) is not None
    except (AuthorityError, OSError) as exc:
        skipped.append(f"选定标题资产读不到，按没有选定标题处理：{exc}"[:200])
        return False


def _owner_numbers(ctx: Ctx, track: Sequence[subtitle_track.TrackCue]) -> set[int] | None:
    """The final track's cue numbers the owner edited; None when the import cannot tell (every cue counts)."""
    rec = imported(ctx)
    edited = rec.get("owner_edited") if rec else None
    if edited is None:
        return None
    pushed = owner_edits.pushed_cues(ctx.clip_dir / IMPORT_DIR / "final.srt", ctx.clip_dir / IMPORT_DIR / "speaker.srt")
    return owner_edits.track_numbers(track, pushed, edited)


def _note_for(review: ReviewAnswer, cue: int, count: int) -> str:
    notes = [str(n.get("text") or "").strip() for n in review.get("notes") or []
             if isinstance(n, dict) and cue_number(n.get("cue_id"), count) == cue]
    return "；".join(t for t in notes if t) or NO_REASON


def _last_fix(fixes: Sequence[ReviewFix | None], cue: int, count: int) -> ReviewFix | None:
    return next((f for f in reversed(fixes) if cue_number((f or {}).get("cue_id"), count) == cue), None)


def _is_his(raw: Any, count: int, mine: set[int] | None) -> bool:
    """The owner's sentence (a cue number that names none counts as his: there is nothing to adopt)."""
    n = cue_number(raw, count)
    return n is None or mine is None or n in mine


def _text_dissent(review: ReviewAnswer, fixes: ReviewFixes, track: Sequence[subtitle_track.TrackCue],
                  his: Callable[[Any], bool]) -> list[Dissent]:
    texts = fixes.pop("subtitles", [])
    out: list[Dissent] = []
    for n, cue in sorted(subtitle_edits([f for f in texts if his((f or {}).get("cue_id"))], track, []).items()):
        fix = _last_fix(texts, n, len(track))
        proposed = subtitle_track.plain_text(cue["text"])
        out.append({"kind": "text", "cue": n, "text": subtitle_track.plain_text(track[n - 1]["text"]),
                    "proposed": proposed, "value": proposed,
                    "reason": str((fix or {}).get("reason") or "").strip() or _note_for(review, n, len(track))})
    if kept := [f for f in texts if not his((f or {}).get("cue_id"))]:
        fixes["subtitles"] = kept
    return out


def _speaker_dissent(review: ReviewAnswer, fixes: ReviewFixes, track: Sequence[subtitle_track.TrackCue],
                     his: Callable[[Any], bool]) -> list[Dissent]:
    out: list[Dissent] = []
    kept: list[ReviewFix | None] = []
    for fix in fixes.pop("speakers", []):
        at, label = cue_number((fix or {}).get("cue_id"), len(track)), str((fix or {}).get("speaker") or "").strip()
        if not his((fix or {}).get("cue_id")):
            kept.append(fix)
        elif at is not None and label and track[at - 1].get("speaker") != label:
            why = "：".join(str((fix or {}).get(k) or "").strip() for k in ("evidence", "quote")).strip("：")
            out.append({"kind": "speaker", "cue": at, "text": subtitle_track.plain_text(track[at - 1]["text"]),
                        "proposed": f"{track[at - 1].get('speaker') or '无'} → {label}", "value": label,
                        "reason": why or _note_for(review, at, len(track))})
    if kept:
        fixes["speakers"] = kept
    return out


def _hold_dissent(answer: ReviewAnswer, track: Sequence[subtitle_track.TrackCue],
                  his: Callable[[Any], bool]) -> list[Dissent]:
    out: list[Dissent] = []
    kept: list[Any] = []
    for hold in answer.pop("holds", None) or []:
        if not isinstance(hold, dict):
            continue
        at = cue_number(hold.get("cue_id"), len(track))
        if not his(hold.get("cue_id")):
            kept.append(hold)
        elif at is not None:
            why = str(hold.get("reason") or "").strip() or "复审：定不下且影响意思"
            out.append({"kind": "hold", "cue": at, "text": subtitle_track.plain_text(track[at - 1]["text"]),
                        "proposed": why, "value": why, "reason": why})
    if kept:
        answer["holds"] = kept
    return out


def _decision_dissent(ctx: Ctx, answer: ReviewAnswer, mine: set[int] | None) -> list[Dissent]:
    decisions = answer.get("doubt_decisions")
    if not isinstance(decisions, list):
        return []
    owned = _owned_doubts(ctx, mine)
    out: list[Dissent] = []
    kept: list[Any] = []
    for d in decisions:
        if isinstance(d, dict) and d.get("confidence") == "低" and d.get("affects_meaning") is True and (
                mine is None or str(d.get("id")) in owned):
            why = f"低把握且影响意思：{str(d.get('reason') or '').strip()}"
            out.append({"kind": "decision", "cue": None, "text": str(d.get("id")), "proposed": why, "value": why,
                        "reason": str(d.get("reason") or "").strip() or NO_REASON})
        else:
            kept.append(d)
    answer["doubt_decisions"] = kept
    return out


def split(ctx: Ctx, review: ReviewAnswer, skipped: list[str]) -> tuple[ReviewAnswer, list[Dissent] | None]:
    """The answer apply_fixes may adopt, and the objections it must not (None: neither kind applies to this clip, nothing
    changes).  Owner-decided subtitles, for the sentences he edited: text fixes, speaker fixes, holds, and doubt decisions
    that would hold a sentence (低 and changes the meaning) are dropped as objections; for the others they stay in the
    answer.  A picked title: the title fix is dropped.  An imported cover: the cover fix is dropped.  Everything else is kept."""
    subtitles, title, cover = owner_decided(ctx), _title_picked(ctx, skipped), cover_import.imported_cover(ctx)
    if not (subtitles or title or cover):
        return review, None
    dissent: list[Dissent] = []
    fixes = cast("ReviewFixes", dict(review.get("fixes") or {}))
    if cover:
        asked = cast("dict[str, Any]", fixes).pop("cover", None)      # an imported cover is never replaced: the wish is only recorded
        wanted = [str(x).strip() for x in asked] if isinstance(asked, list) else []
        if any(wanted) and wanted != cover["lines"]:
            proposed = " / ".join(wanted)
            dissent.append({"kind": "cover", "cue": None, "text": " / ".join(cover["lines"]), "proposed": proposed,
                            "value": proposed, "reason": NO_REASON})
    if title:
        proposed = normalize.canon_text(str(fixes.pop("title", None) or "").strip())[0]
        current = (ctx.clip_dir / "title/title.txt").read_text(encoding="utf-8").strip() if proposed else ""
        if proposed and proposed != current:
            dissent.append({"kind": "title", "cue": None, "text": current, "proposed": proposed, "value": proposed,
                            "reason": NO_REASON})
    answer = cast("ReviewAnswer", dict(review, fixes=fixes))
    if not subtitles:
        return answer, dissent
    track = subtitle_track.read_track(ctx.clip_dir)
    mine = _owner_numbers(ctx, track)
    his = partial(_is_his, count=len(track), mine=mine)
    dissent += _text_dissent(review, fixes, track, his) + _speaker_dissent(review, fixes, track, his)
    dissent += _hold_dissent(answer, track, his) + _decision_dissent(ctx, answer, mine)
    return answer, dissent


def _owned_doubts(ctx: Ctx, mine: set[int] | None) -> set[str]:
    """The doubt ids whose sentences are the owner's (a doubt that names no final sentence counts as his)."""
    if mine is None:
        return set()
    from .stage_package import current_doubts

    doubts, numbers = current_doubts(ctx)
    return {str(d["id"]) for d in doubts if not numbers.get(str(d["id"])) or mine.intersection(numbers[str(d["id"])])}


def adopted_lines(ctx: Ctx, track: Sequence[subtitle_track.TrackCue],
                  edited: dict[int, subtitle_track.TrackCue]) -> list[str]:
    """PROCESS lines for the review's edits that an owner-decided clip adopts (sentences the owner did not touch)."""
    if not owner_decided(ctx):
        return []
    lines: list[str] = []
    for n, cue in sorted(edited.items()):
        old = track[n - 1]
        if cue["text"] != old["text"]:
            lines.append(f"复审修改了频道主未改动的第 {n} 句：{subtitle_track.plain_text(old['text'])} → "
                         f"{subtitle_track.plain_text(cue['text'])}")
        if cue.get("speaker") != old.get("speaker"):
            lines.append(f"复审修改了频道主未改动的第 {n} 句的说话人：{old.get('speaker') or '无'} → {cue.get('speaker')}")
    return lines


def carry(record: ReviewRecord, applied: ReviewApplied) -> None:
    """Put the applied step's objections into the review record (state.review["dissent"]), without repeating one.  The
    clip's state keeps them for good in dissent_register: this record is rebuilt whenever step 9 runs again."""
    new = applied.get("dissent")
    if new is not None:
        have = record.get("dissent", [])
        record["dissent"] = [*have, *(d for d in new if d not in have)]


def skipped_lines(applied: ReviewApplied) -> list[str]:
    """The same objections as `未采用` lines, for the wide rounds' report."""
    return [f"{KINDS[d['kind']]}（频道主已定下）" + tail(d) for d in applied.get("dissent") or []]


def process_lines(dissent: Sequence[Dissent] | None) -> list[str]:
    if dissent is None:
        return []
    return [f"复审的异议（频道主已定下的内容，按频道主定稿规则保留）{len(dissent)} 条", *(f"  {KINDS[d['kind']]}" + tail(d) for d in dissent)]


def tail(d: Dissent) -> str:
    where = f" 第 {d['cue']} 句" if d["cue"] is not None else f" {d['text']}"
    now = f"「{d['text']}」" if d["cue"] is not None else ""
    return f"{where}{now}：复审意见 {d['proposed']}；复审理由 {d['reason']}"
