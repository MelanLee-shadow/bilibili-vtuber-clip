"""Step 5: ASS, burn, intro (片头 fail-closed, 维护者).

Split out of stage_impl.py (TASK-13); stage_impl re-exports every name, so callers and tests keep
`stage_impl.<name>`.
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path

from . import (
    gates,
    normalize,
    publication_id,
    speaker_v8,
    spoken_time,
)
from .adapters import remote_python
from .lane_rules import rules
from .layout import layout
from .runner import Ctx, StepOutcome
from .srt_text import format_srt, parse_srt
from .step_io import REMOTE_FONTS, REMOTE_LANE_FONTS, REMOTE_TOOLS, _fail, _local, _run_remote


def _manuscript_intro(ctx: Ctx, manuscript: str) -> str | None:
    """The intro the manuscript's own clip was burned with (its burn/intro_result.json): what its published package carries.
    None when the manuscript's clip has no readable receipt."""
    try:
        intro_id = json.loads((ctx.clip_dir.parent / manuscript / "burn/intro_result.json").read_text(encoding="utf-8")).get("intro_id")
    except (OSError, ValueError):
        return None
    return intro_id if isinstance(intro_id, str) and intro_id else None



def _burned_lines(ctx: Ctx) -> Path:
    """The subtitles that are burned: speaker.srt, without the watched video's lines when that video carries its own
    subtitles (step 4 says so in decision.json: `video_subtitled`).  The cue list itself is not changed.  A line that is
    burned then stands where its own words were said (spoken_time: the list placed it after a line of the video that
    is no longer shown; owner, a line of hers came up late), never past the burned lines next to it."""
    speaker = ctx.clip_dir / "speaker/speaker.srt"
    decision = ctx.clip_dir / "speaker/decision.json"
    doc = json.loads(decision.read_text(encoding="utf-8")) if decision.exists() else {}
    if not doc.get("video_subtitled"):
        return speaker
    host = f"[{speaker_v8.HOST}] "
    partly = {host + str(d["burned_from"]): host + str(d["burned_text"]) for d in doc["cues"] if d.get("burned_text")}
    kept = [dict(c, text=partly.get(" ".join(c["text"].split()), c["text"]))
            for c in parse_srt(speaker.read_text(encoding="utf-8")) if not c["text"].startswith(f"[{speaker_v8.GUEST_LABEL}]")]
    tracks = [parse_srt(path.read_text(encoding="utf-8")) for name in ("jianying", "bcut")
              if (path := ctx.clip_dir / f"asr/{name}.srt").exists()]
    reach = float(rules("other_voices")["own_time_reach_s"])
    spans = [(float(c["start_s"]), float(c["end_s"])) for c in kept]
    for i, cue in enumerate(kept):
        lo = max(spans[i][0] - reach, spans[i - 1][1] if i else 0.0)
        hi = min(spans[i][1] + reach, spans[i + 1][0] if i + 1 < len(kept) else spans[i][1] + reach)
        said = spoken_time.spoken_span(cue["text"].removeprefix(host), tracks, lo, hi)
        if said is not None:
            cue["start_s"], cue["end_s"] = max(said[0], lo), min(said[1], hi)
    out = _local(ctx, "burn/speaker.burned.srt")
    out.write_text(format_srt(normalize._sequence(kept)), encoding="utf-8")
    return out


def _person_styles(ctx: Ctx, burned: Path) -> tuple[Path | None, str]:
    """burn/speaker.persons.json for render_ass.py: the guests step 4 named (speaker/persons.json) in their own colours
    (person_colours; owner), each burned guest line taking the person of the named line of the same label it
    overlaps most (a line step 9 rewrote keeps the name of the line it was).  (path, PROCESS line); no path when no
    guest line is named or no named guest has a colour."""
    from . import guest_identity, person_colours
    from .step4_speaker import PERSONS

    out = _local(ctx, "burn/speaker.persons.json")
    out.unlink(missing_ok=True)
    if not (ctx.clip_dir / PERSONS).exists():
        return None, ""
    rows = guest_identity.named_rows(ctx.clip_dir)
    known, guest = person_colours.people(), person_colours.channel_guest_style()
    styles: dict[str, str] = {}
    cues: list[str | None] = []
    who: dict[str, int] = {}
    unnamed = 0
    for cue in parse_srt(burned.read_text(encoding="utf-8")):
        if not cue["text"].startswith(f"[{speaker_v8.GUEST_LABEL}]"):
            cues.append(None)
            continue
        person = guest_identity.person_at(rows, float(cue["start_s"]), float(cue["end_s"]))
        style = person_colours.guest_style(person, guest, known) if person else None
        if person is None or style is None:
            unnamed += 1
            cues.append(None)
            continue
        styles[style[0]] = style[1]
        who[person] = who.get(person, 0) + 1
        cues.append(style[0])
    named_people = sorted({str(r["person"]) for r in rows})
    missing = [p for p in named_people if p not in known]
    if not styles:
        return None, (f"嘉宾颜色：定到人的嘉宾（{'、'.join(named_people)}）在 person_colours 里都没有登记颜色，按通用嘉宾样式"
                      if named_people else "")
    out.write_text(json.dumps({"styles": styles, "cues": cues}, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return out, ("嘉宾颜色（各用 person_colours 给她登记的颜色）：" + "、".join(f"{p} {n} 句 {known[p]['outline']}" for p, n in who.items())
                 + (f"；{'、'.join(missing)} 没有登记颜色" if missing else "")
                 + (f"；{unnamed} 句嘉宾句按通用嘉宾样式（没定到人或没有颜色）" if unnamed else ""))


def step5_burn(ctx: Ctx) -> StepOutcome:
    rd = ctx.remote_dir
    clip = ctx.state.data["clip"]
    recording_date = ctx.session["recording_date"]
    if clip.get("recording_date", recording_date) != recording_date:
        return _fail("INTRO_CLIP_DATE_MISMATCH", "clip and session recording dates differ")
    # The intro belongs to the manuscript: a one-shot intro is bound to its candidate id, and a successor (`<cid>_trim`,
    # `<cid>_r<N>`) is the same manuscript, so it asks for the intro under the manuscript's id, as the original did.  A
    # clip that is not a successor is its own manuscript: nothing changes for it.
    manuscript = publication_id.publication_cid(clip["cid"])
    identity_args = f"{shlex.quote(recording_date)} {shlex.quote(manuscript)}"
    pinned = _manuscript_intro(ctx, manuscript) if manuscript != clip["cid"] else None
    pin_env = f"MANUAL_LANE_PIN_INTRO={shlex.quote(pinned)} " if pinned else ""
    burned = _burned_lines(ctx)
    ctx.adapters.remote.put(burned, f"{rd}/burn/speaker.srt")
    persons, colour_line = _person_styles(ctx, burned)
    if persons is not None:
        ctx.adapters.remote.put(persons, f"{rd}/burn/speaker.persons.json")
    ctx.adapters.remote.put(REMOTE_TOOLS / "render_ass.py", f"{rd}/tools/render_ass.py")
    ctx.adapters.remote.put(REMOTE_TOOLS / "prepend_intro.py", f"{rd}/tools/prepend_intro.py")
    # libass finds a glyph the style's font lacks only through fontconfig, so the lane's fallback fonts (a monochrome
    # emoji font: 🥺 is newer than the host's Symbola) join the host's fonts there, for this step only.
    body = (f"mkdir -p {rd}/burn {rd}/tools\n"
            f"printf '%s\\n' '<?xml version=\"1.0\"?><fontconfig><include ignore_missing=\"yes\">/etc/fonts/fonts.conf</include>' "
            f"'<dir>{REMOTE_LANE_FONTS}</dir><cachedir>{rd}/burn/fontcache</cachedir></fontconfig>' > {rd}/burn/fonts.conf\n"
            f"export FONTCONFIG_FILE={rd}/burn/fonts.conf\n"
            f"{remote_python(f'{rd}/tools/render_ass.py {rd}/burn/speaker.srt {rd}/burn/final.ass {REMOTE_FONTS}' + (f' {rd}/burn/speaker.persons.json' if persons else ''))} || exit 11\n"
            f"ffmpeg -hide_banner -loglevel error -y -i {rd}/cut/cut.mp4 "
            f"-vf \"subtitles='{rd}/burn/final.ass':fontsdir='{REMOTE_FONTS}'\" "
            f"-c:v libx264 -preset veryfast -crf 20 -c:a copy -movflags +faststart {rd}/burn/main_burned.mp4 || exit 12\n"
            f"MANUAL_LANE_INTRO_DIR={layout().intro_dir} {pin_env}"
            f"{remote_python(f'{rd}/tools/prepend_intro.py {rd}/burn/main_burned.mp4 {rd}/burn/final.mp4 ')}"
            f"{rd}/burn/intro_work {rd}/burn/intro_result.json {identity_args} || exit 13\n")
    res = _run_remote(ctx, "s5-burn", body, timeout=3600)
    if res.rc == 11 and "GLYPH_MISSING" in res.stdout:
        return _fail("SUBTITLE_GLYPH_MISSING", "字幕里有主机上没有字体能画的字，烧出来会是方框；先在主机装上能画它的字体"
                     "（单色字体放进 assets/manual_lane/fonts 随代码部署即可，第 5 步把它加进 fontconfig；主机的 libass 不画彩色 emoji），再重跑第 5 步：" + res.stdout.split("GLYPH_MISSING", 1)[1][:300])
    if res.rc != 0:
        return _fail(f"BURN_FAILED_RC{res.rc}", (res.stdout + res.stderr)[-400:])
    local_result = _local(ctx, "burn/intro_result.json")
    ctx.adapters.remote.get(f"{rd}/burn/intro_result.json", local_result)
    intro = json.loads(local_result.read_text(encoding="utf-8"))
    verdict = gates.intro_gate(intro)
    if not verdict.ok:
        return _fail(verdict.reason_code, verdict.detail)
    process = [f"片头前置：{verdict.evidence.get('intro_id')}，偏移 {verdict.evidence.get('intro_offset_ms')} ms"]
    if verdict.evidence.get("intro_id") is None:
        process = [f"不加片头：本频道的片头清单写明不用片头（{intro.get('reason')}）"]
    elif manuscript != clip["cid"]:
        if pinned is None:
            process.append(f"后继片的片头：无法确认「{manuscript}」线上那版用的片头（它的 burn/intro_result.json 读不到），按稿件 id 取片头")
        elif pinned != verdict.evidence.get("intro_id"):
            process.append(f"后继片的片头：线上那版用的是 {pinned}，它已不在当前可选的片头里，改用了 {verdict.evidence.get('intro_id')}")
        else:
            process.append(f"后继片的片头：与「{manuscript}」线上那版相同（{pinned}）")
    if colour_line:
        process.append(colour_line)
    return StepOutcome(True, products={"intro_result": str(local_result)},
                       note=f"intro={verdict.evidence.get('intro_id')} offset={verdict.evidence.get('intro_offset_ms')}ms "
                            f"OCI3 {rd}/burn/final.mp4",
                       process=process)
