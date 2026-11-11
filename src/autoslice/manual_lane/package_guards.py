"""Four guards at the start of the package step (step 8), before anything is built or deleted.

SPEECH_WITHOUT_SUBTITLES  a finished clip must not carry a stretch of real speech that has no subtitle (incident:
    the owner marked gift-thanks sentences 删, the import kept only the rest, the video was not cut, and the video went out with
    seconds of speech and no subtitle).  The machine dictation of the clip (asr/bcut.srt, the clip's clock) is held against
    subs/final.srt: between two neighbouring subtitles, a gap of GAP_S or more that holds, whole, dictation with at least
    MIN_SPOKEN_CHARS characters of speech (laughter and filler sounds do not count) fails the step.  The stretch before the
    first and after the last subtitle is not looked at (head and tail belong to the boundary step).  No dictation file: not
    stopped, and PROCESS says the check was not done.
PUBLISHED_PACKAGE_PROTECTED  a package that is sealed (review_manifest.json) and whose manuscript is published must not be
    deleted or overwritten: the first-publication evidence binds the package by path and hash.  Redoing a published clip is
    done in another directory (a successor clip: `revise`, or trim_run).  The registry cannot be read: also stops (cannot
    confirm).
AUDIO_SILENT_UNDER_SUBTITLE  a finished clip must have sound under its subtitles: a subtitle of at least a second whose whole
    time is digital silence in burn/final.mp4 (intro included) fails the step, naming the line and its time (package_audio;
    numbers and their evidence: lane_rules.json audio_under_subtitle).  Where the silence came from does not matter.
REVISION_AUTHORITY_UNBUILT  a successor of a published manuscript gets its same-BV repair authority here (revision_authority);
    when it cannot be built the step stops, so that no package is made that cannot be used for the revision.
"""

from __future__ import annotations

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


def check(ctx: Ctx, old_package: Path) -> tuple[StepOutcome | None, list[str]]:
    """(the failure that stops step 8, or None; PROCESS lines to add to the step's own).  `old_package`: where the lane
    will build this clip's package, which step 8 deletes first when it is there."""
    cid = publication_cid(str(ctx.state.data["clip"]["cid"]))
    if (old_package / SEAL_FILE).is_file():
        try:
            published = title_history.published_status(ctx, cid)
        except (OSError, ValueError, RuntimeError) as exc:
            return _fail(PUBLISHED_PACKAGE_PROTECTED, f"{old_package} 已封存，读不到出版登记，无法确认「{cid}」是否已发布，"
                         f"不删旧包：{type(exc).__name__}: {exc!s:.200}"), []
        if published:
            return _fail(PUBLISHED_PACKAGE_PROTECTED, f"{old_package} 已封存且「{cid}」已发布：已发布的包不许覆盖，"
                         f"已发布的片请先 `revise` 建后继片，再在后继片上重做"), []
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
    gaps = gaps_with_speech(final, parse_srt(dictation_file.read_text(encoding="utf-8")))
    if gaps:
        return _fail(SPEECH_WITHOUT_SUBTITLES, f"两句字幕之间空了 {GAP_S:g} 秒以上而有实际说话内容，共 {len(gaps)} 段："
                     + "；".join(f"{g['start_s']:.1f}-{g['end_s']:.1f}s「{g['text']}」" for g in gaps)), []
    return None, notes
