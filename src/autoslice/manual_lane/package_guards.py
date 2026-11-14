"""Four guards at the start of the package step (step 8), before anything is built or deleted.

SPEECH_WITHOUT_SUBTITLES  a finished clip must not carry a stretch of real speech that has no subtitle (incident:
    the owner marked gift-thanks sentences 删, the import kept only the rest, the video was not cut, and the video went out with
    seconds of speech and no subtitle).  The machine dictation of the clip (asr/bcut.srt, the clip's clock) is held against
    subs/final.srt: between two neighbouring subtitles, a gap of GAP_S or more that holds, whole, dictation with at least
    MIN_SPOKEN_CHARS characters of speech (laughter and filler sounds do not count) fails the step - unless the subtitle
    step was asked about that very stretch and kept it without a line (subs/unsubtitled.json: sung lyrics, a background voice).  The stretch before the
    first and after the last subtitle is not looked at (head and tail belong to the boundary step).  No dictation file: not
    stopped, and PROCESS says the check was not done.
PUBLISHED_PACKAGE_PROTECTED  a package that is sealed (review_manifest.json) and was used online must not be deleted or
    overwritten: the publication evidence binds the package by path and hash.  Used online is: the package of the clip that is
    the published manuscript itself (its first publication), or - for a successor clip (`<cid>_r<N>`, `<cid>_trim`), whose package
    carries the manuscript's name in its own directory - a package that a manifest of the manuscript's receipt chain attests (the
    first publication, or a completed same-BV video or cover repair).  A successor's package that was sealed but never went
    online is not protected and is made again.  Redoing a used package is done in another directory (a successor clip:
    `revise`, or trim_run).  The registry or the chain cannot be read: also stops (cannot confirm).
AUDIO_SILENT_UNDER_SUBTITLE  a finished clip must have sound under its subtitles: a subtitle of at least a second whose whole
    time is digital silence in burn/final.mp4 (intro included) fails the step, naming the line and its time (package_audio;
    numbers and their evidence: lane_rules.json audio_under_subtitle).  Where the silence came from does not matter.
REVISION_AUTHORITY_UNBUILT  a successor of a published manuscript gets its same-BV repair authority here (revision_authority);
    when it cannot be built the step stops, so that no package is made that cannot be used for the revision.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import TypedDict

from . import doubt_policy, normalize, package_audio, revision_authority, subtitle_track, title_history
from .publication_id import publication_cid
from .runner import Ctx, StepOutcome
from .srt_text import SrtCue, parse_srt
from .step_io import _fail

SPEECH_WITHOUT_SUBTITLES = "SPEECH_WITHOUT_SUBTITLES"
PUBLISHED_PACKAGE_PROTECTED = "PUBLISHED_PACKAGE_PROTECTED"
GAP_S = 3.0              # a stretch between two neighbouring subtitles at least this long is looked at
MIN_SPOKEN_CHARS = 4     # speech in such a stretch (dictation, laughter and fillers left out) of at least this many characters stops the step
SEAL_FILE = "review_manifest.json"


class Gap(TypedDict):
    start_s: float
    end_s: float
    text: str


def spoken_chars(text: str) -> int:
    """The characters of real speech in a dictation line: no laughter, no filler sounds, no punctuation."""
    return len(doubt_policy.sim_key(normalize.strip_laughter(text)))


def gaps_with_speech(final: list[subtitle_track.TrackCue], dictation: list[SrtCue]) -> list[Gap]:
    """The gaps of `GAP_S` or more between two neighbouring final cues that hold at least `MIN_SPOKEN_CHARS` characters of
    speech, as the dictation lines that lie whole inside the gap (start, end and their text)."""
    out: list[Gap] = []
    for before, after in zip(final, final[1:]):
        low, high = float(before["end_s"]), float(after["start_s"])
        if high - low < GAP_S:
            continue
        inside = [c for c in dictation if low <= float(c["start_s"]) and float(c["end_s"]) <= high]
        if sum(spoken_chars(str(c["text"])) for c in inside) >= MIN_SPOKEN_CHARS:
            out.append({"start_s": low, "end_s": high, "text": " ".join(str(c["text"]).strip() for c in inside)})
    return out


DECIDED_FILE = "subs/unsubtitled.json"


def record_decided(clip_dir: Path, gaps: list[Gap], decided: list[Gap]) -> None:
    """The stretches of `gaps` (what the final subtitles leave without a line) that the subtitle step was asked about
    by name and kept without a subtitle, giving its reason (sung lyrics of an existing song, a voice in the background):
    written for the package step, which does not stop for them.  Nothing decided: the file is removed."""
    kept = [g for g in gaps if any(d["start_s"] < g["end_s"] and g["start_s"] < d["end_s"] for d in decided)]
    path = clip_dir / DECIDED_FILE
    if kept:
        path.write_text(json.dumps(kept, ensure_ascii=False, indent=1), encoding="utf-8")
    else:
        path.unlink(missing_ok=True)


def undecided(clip_dir: Path, gaps: list[Gap]) -> list[Gap]:
    """`gaps` without the ones the subtitle step decided on (record_decided): same stretch, within half a second."""
    path = clip_dir / DECIDED_FILE
    decided = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []
    return [g for g in gaps if not any(abs(g["start_s"] - d["start_s"]) <= 0.5 and abs(g["end_s"] - d["end_s"]) <= 0.5 for d in decided)]


def _attested(old_package: Path, used: list[dict]) -> bool:
    """Whether `old_package` is one of the `used` packages (title_history.used_packages): its directory is a package_root, or its
    seal is the review manifest a publication attested, by path or by sha256."""
    seal = old_package / SEAL_FILE
    digest = hashlib.sha256(seal.read_bytes()).hexdigest()
    here = {os.path.normpath(p) for p in (old_package, old_package.resolve())}
    seals = {os.path.normpath(p) for p in (seal, seal.resolve())}
    for package in used:
        sealed = package.get("review_manifest")
        if os.path.normpath(package["package_root"]) in here or (
                sealed and (os.path.normpath(sealed["path"]) in seals or sealed["sha256"].removeprefix("sha256:") == digest)):
            return True
    return False


def protected(ctx: Ctx, old_package: Path) -> StepOutcome | None:
    """PUBLISHED_PACKAGE_PROTECTED when `old_package` is sealed and was used online (or that cannot be told), else None.  The one
    definition: step 8 and `redo` both ask it.  Used online: the clip is the published manuscript itself, or (a successor clip)
    the package is attested by the manuscript's receipt chain."""
    clip_cid = str(ctx.state.data["clip"]["cid"])
    cid = publication_cid(clip_cid)
    if not (old_package / SEAL_FILE).is_file():
        return None
    try:
        published = title_history.published_status(ctx, cid)
    except (OSError, ValueError, RuntimeError) as exc:
        return _fail(PUBLISHED_PACKAGE_PROTECTED, f"{old_package} 已封存，读不到出版登记，无法确认「{cid}」是否已发布，"
                     f"不删旧包：{type(exc).__name__}: {exc!s:.200}")
    if not published:
        return None
    if clip_cid != cid:         # a successor: its package carries the manuscript's name but is its own; only a package that went online is protected
        try:
            online = _attested(old_package, title_history.used_packages(ctx, cid))
        except (OSError, ValueError, RuntimeError) as exc:
            return _fail(PUBLISHED_PACKAGE_PROTECTED, f"{old_package} 已封存且「{cid}」已发布，读不到出版登记的回执链，无法判定这个包是否用于过线上，"
                         f"不删旧包：{type(exc).__name__}: {exc!s:.600}")
        if not online:
            return None
        return _fail(PUBLISHED_PACKAGE_PROTECTED, f"{old_package} 已封存，且是「{cid}」线上版本（首发或某次已完成的换视频/换封面）所用的包："
                     f"已发布的包不许覆盖，要改请先 `revise` 建后继片，再在后继片上重做")
    return _fail(PUBLISHED_PACKAGE_PROTECTED, f"{old_package} 已封存且「{cid}」已发布：已发布的包不许覆盖，"
                 f"已发布的片请先 `revise` 建后继片，再在后继片上重做")


def check(ctx: Ctx, old_package: Path) -> tuple[StepOutcome | None, list[str]]:
    """(the failure that stops step 8, or None; PROCESS lines to add to the step's own).  `old_package`: where the lane
    will build this clip's package, which step 8 deletes first when it is there."""
    if (refused := protected(ctx, old_package)) is not None:
        return refused, []
    refused, notes = revision_authority.prepare(ctx, old_package.name)
    if refused:
        return refused, []
    refused, audio_notes = package_audio.check(ctx)         # the clip itself: sound under every subtitle (AUDIO_SILENT_UNDER_SUBTITLE)
    if refused:
        return refused, []
    notes = [*notes, *audio_notes]
    dictation_file = ctx.clip_dir / "asr/bcut.srt"
    if not dictation_file.is_file():
        return None, [*notes, "没有机器听写（asr/bcut.srt），未做有声无字幕检查"]
    final = subtitle_track.parse_blocks((ctx.clip_dir / "subs/final.srt").read_text(encoding="utf-8"))
    found = gaps_with_speech(final, parse_srt(dictation_file.read_text(encoding="utf-8")))
    gaps = undecided(ctx.clip_dir, found)
    if len(gaps) < len(found):
        notes = [*notes, f"有声无字幕检查：{len(found) - len(gaps)} 段是字幕步骤被点名问过后决定不出字幕的（{DECIDED_FILE}），不拦"]
    if gaps:
        return _fail(SPEECH_WITHOUT_SUBTITLES, f"两句字幕之间空了 {GAP_S:g} 秒以上而有实际说话内容，共 {len(gaps)} 段："
                     + "；".join(f"{g['start_s']:.1f}-{g['end_s']:.1f}s「{g['text']}」" for g in gaps)), []
    return None, notes
