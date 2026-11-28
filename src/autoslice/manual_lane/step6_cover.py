"""Step 6 cover: frame selection, candidate rendering, scoring and choice."""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Collection, Mapping, Sequence
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, cast

from . import text_overlap
from .. import cover_layout_rotation, cover_style_rotation
from ..cover_style_rotation import REPO_TABLE as COVER_STYLE_TABLE
from .adapters import REMOTE_REPO, remote_python, root_script
from . import channel, cover_column, cover_guests, cover_scope
from .cover_pick import ROW_FACTS, face_view
from .cover_redo import RetryBudget
from .cover_run import TEXT_TOO_LONG_RC
from .cover_thumb import ThumbVerdict, check_thumb
from .lane_rules import rules
from .layout import REPO_ROOT, layout
from .runner import Ctx
from .srt_text import SrtCue, _context_lines, _json_from_text
from .step6_cover_types import (
    BaseAvoid as BaseAvoid,
    BaseFrame as BaseFrame,
    CoverBrief as CoverBrief,
    CoverCandidate as CoverCandidate,
    CoverCandidateCall as CoverCandidateCall,
    CoverCandidateResult as CoverCandidateResult,
    CoverChoice as CoverChoice,
    CoverEmote as CoverEmote,
    CoverPeers as CoverPeers,
    CoverPeople as CoverPeople,
    CoverAction as CoverAction,
    CoverPlan as CoverPlan,
    CoverProducts as CoverProducts,
    CoverRender as CoverRender,
    CoverRenderResult as CoverRenderResult,
    CoverRotation as CoverRotation,
    CoverRoute as CoverRoute,
    CoverScore as CoverScore,
    CoverSummary as CoverSummary,
    CoverVerdict as CoverVerdict,
    FrameCall as FrameCall,
    FrameDocument as FrameDocument,
    Generation as Generation,
    RecentCover as RecentCover,
    RecentCovers as RecentCovers,
)
from .step_io import (CpaVisionUnavailable, REMOTE_TOOLS, _ask_cpa, _clip, _cpa_ok, _local, _log, _other_live, _render,
                      _run_remote, _write_atomic, linked_session, raise_if_vision_unavailable, superseded,
                      vision_cpa_event, vision_models)

if TYPE_CHECKING:
    from src.autoslice.cpa_gemini_client import JsonValue

    from .state import CpaEvent


COVER_RENDERER = layout().cover_skill_scripts / "render_cover.py"


COVER_STYLES = COVER_STYLE_TABLE
COVER_ROTATION = Path(cover_style_rotation.__file__)
COVER_RECENT_STYLES = 3            # skill: a style the last three covers used is skipped
# The one composition table (owner,, the covers all looked the same): the channel's
# template table and the shared selector, uploaded as they are; the media-host tool and the skill renderer both use them.
COVER_LAYOUT_ROTATION = Path(cover_layout_rotation.__file__)
SKILL_PIN = REPO_ROOT / "config" / "title_skill_source.json"   # the title-cover skill commit the deploy installs


def cover_layout_table() -> Path:
    """The active channel's composition template table (profile asset cover_layout_templates)."""
    from src.autoslice.cover_profile_assets import CHANNEL_PROFILE, ROOT
    return CHANNEL_PROFILE.asset_file("cover_layout_templates", repo_root=ROOT)


def renderer_outdated(renderer: Path) -> str | None:
    """Why the installed title-cover skill renderer cannot lay out this lane's template table, or None.  The renderer is
    the skill's (a separate repository, installed on its own): one older than the table has no bottom layout and stops
    the media-host tool with a KeyError at the first bottom template, so it is checked before anything is sent."""
    found = re.search(r"^LAYOUT_SCHEMA\s*=\s*['\"]([^'\"]+)['\"]", renderer.read_text(encoding="utf-8"), re.M)
    if found and found.group(1) == cover_layout_rotation.SCHEMA:
        return None
    return (f"{renderer} 的 LAYOUT_SCHEMA 是 {found.group(1) if found else '（没有）'}，样板表要 {cover_layout_rotation.SCHEMA}："
            + skill_install_advice())


def skill_install_advice() -> str:
    """Which skill to install, at which version, where: the skill the profile names (as configured: a versioned install
    resolves to a content hash), what is installed there now (a versioned install's SOURCE.json; a checkout has none) and
    the commit this repository pins (config/title_skill_source.json; the public snapshot carries none)."""
    lane = layout()
    root = lane.cover_skill_scripts.parent
    source = root / "SOURCE.json"
    installed = (f"那里装的是提交 {json.loads(source.read_text(encoding='utf-8'))['commit']}" if source.is_file()
                 else "那里没有 SOURCE.json，不是按版本装的")
    pinned = (f"该装本仓库钉的提交 {json.loads(SKILL_PIN.read_text(encoding='utf-8'))['agent_toolkit_commit']}"
              f"（{SKILL_PIN.relative_to(REPO_ROOT)}）" if SKILL_PIN.is_file() else "本仓库没有钉技能提交")
    return (f"先装新版 {lane.cover_skill_name} 技能：装到 profile 配的 cover_skill_scripts {lane.cover_skill_configured}"
            f"（现在解析到 {root}，{installed}）；{pinned}")
COVER_RECENT_BACKGROUNDS = rules("cover")["background_recent_covers"]   # lane_rules cover.background_recent_covers (source there)
COVER_FONT_REMOTE = layout().cover_font   # the cover font on the media host
# The skill renderer draws a Latin word with a lowercase a in this font (锐字真言体's a is a single-storey ɑ: "awawa"
# read "qwqwq"), found beside the cover font; the channel's committed copy (OFL) is sent there with every cover.
COVER_LATIN_FONT = channel.asset("fonts/ZCOOLKuaiLe-Regular.ttf")
COVER_FACE_BOX = [560, 150, 1360, 850]


def cover_line_limits() -> tuple[int, int]:
    """(first line, punchline) most characters the cover text may have, from lane_rules `cover` (derived there from the
    feed-safe width and the text sizes the renderer must reach), so the title step holds them before anything is rendered."""
    cover = rules("cover")
    return cover["first_line_max_chars"], cover["punch_line_max_chars"]


COVER_PAUSES = "。，、；,;"           # shown as a space inside a line
COVER_LINE_END = COVER_PAUSES + "：:"   # dropped at the end of a line; inside it a colon is the 「名字：原话」 form (70-cover)


def cover_line(text: object) -> str:
    """A cover line as it is shown: the canonical writing, no pause mark or colon at its end, and a pause mark inside it
    shown as a space (11-23 625460: the punchline 「勾到了。我要笑」 was rendered with its full stop).  A colon inside the
    line stays (「安晚：暖一暖」 is the name-and-words form step 70 allows), and so do ！？… (how the line is said).  Every
    cover line a model writes in step 6 comes through here."""
    from .normalize import canon_text

    line = canon_text(str(text).strip())[0].strip().rstrip(COVER_LINE_END + " ")
    return re.sub(rf"\s*[{COVER_PAUSES}]+\s*", " ", line).strip()


def cover_lines_overflow(lines: Sequence[str]) -> str:
    """Empty when every line is within its limit (the last line is the punchline), else the sentence that says which line
    is too long and by how much: the same words feed the title rewrite and the cover-text shortening."""
    first, punch = cover_line_limits()
    limits = [first] * (len(lines) - 1) + [punch]
    over = [f"「{ln}」{len(ln)} 字，{'笑点行' if i == len(lines) - 1 else '第一行'}最多 {limit} 字"
            for i, (ln, limit) in enumerate(zip(lines, limits, strict=True)) if len(ln) > limit]
    return "；".join(over)
COVER_RETRIES = rules("cover")["redo_retries"]   # lane_rules cover.redo_retries (source there): the one retry budget of the cover step,
# spent by a shortened cover text and by a redo of routes vetoed on their material (cover_redo.RetryBudget)
COVER_TEXT_TOO_LONG = "COVER_RENDER_FAILED_RC23"   # the renderer's text-does-not-fit exit


def _cover_frame_time(cues: Sequence[SrtCue], cover_lines: Sequence[str], cut_len: float,
                      peaks: Sequence[tuple[float, str]] = ()) -> float:
    """The clip's moment the cover frames are looked for around (C8, 标题封面落在最高一层的看点上，这条): the subtitle the
    punchline stands in (the cover words are written for the clip's landing); a punchline that is no subtitle's words
    (reworded, exaggerated) falls back to the selection's peak (`clip_peaks`) whose words the cover lines share, else its
    first peak; the middle of the cut only when the selection named none."""
    needle = text_overlap.key(cover_lines[-1]) if cover_lines else ""
    for cue in cues:
        if needle and needle in text_overlap.key(cue["text"]):
            return (cue["start_s"] + cue["end_s"]) / 2
    if peaks:
        said = text_overlap.key("".join(cover_lines))
        return max(peaks, key=lambda p: text_overlap.shared(text_overlap.key(p[1]), said, min_run=2))[0]
    return cut_len / 2


def clip_peaks(ctx: Ctx, cut_len: float) -> list[tuple[float, str]]:
    """The selection's peaks of this clip (clip state `peaks`, on the recording's clock) as (seconds into the cut, words),
    in the selection's order; a peak outside the cut is left out, and a cut pieced together (a trim or a compilation)
    has no single recording clock and gives none."""
    clip = _clip(ctx)
    if _source_offset_s(ctx) is None:
        return []
    out = []
    for peak in clip.get("peaks") or []:
        t = (int(cast(int, peak["at_ms"])) - clip["start_ms"]) / 1000
        if 1.0 <= t <= cut_len - 1.0:
            out.append((round(t, 3), str(peak.get("quote") or "")))
    return out


BASE_FRAME_ROUTES = ("screenshot", "cutout")     # the routes whose cover shows the live frame (a cut-out is drawn from it)


def _source_offset_s(ctx: Ctx) -> float | None:
    """Where the cut starts on its recording's clock, or None for a cut pieced together from parts of it (a trim with a
    hole, a compilation: compilation.load_manifest), whose seconds after the first hole are on no recording's clock."""
    from .compilation import load_manifest

    return None if load_manifest(ctx.clip_dir) is not None else _clip(ctx)["start_ms"] / 1000


def base_avoid(ctx: Ctx) -> BaseAvoid:
    """The base frames of this stream's covers so far (70-cover 同场不重复底帧): the frame step passes over one that
    repeats them (pick_frame.repeat_of).  A cover chosen before the frames were recorded has none and is not compared."""
    clip, date = _clip(ctx), ctx.session.get("recording_date")
    return {"source": clip.get("source_name"), "offset_s": _source_offset_s(ctx),
            "covers": [cast(BaseFrame, dict(base, cid=c["cid"])) for c in _lane_covers(ctx)
                       if c.get("recording_date") == date and c["route"] in BASE_FRAME_ROUTES and (base := c.get("base_frame"))]}


def base_frame(ctx: Ctx, made_t: float) -> BaseFrame:
    """The live frame the chosen cover was made on, for the stream's next covers (its face hash only when the frame
    step read her face on that very frame)."""
    clip = _clip(ctx)
    try:
        base = json.loads((ctx.clip_dir / "cover/frames.json").read_text(encoding="utf-8")).get("base") or {}
    except (OSError, ValueError):
        base = {}
    same = base.get("t") is not None and abs(float(base["t"]) - made_t) < 0.01
    offset = _source_offset_s(ctx)
    return {"source": clip.get("source_name"), "t_source": None if offset is None else round(offset + made_t, 3),
            "dhash": base.get("dhash") if same else None}


COVER_FRAME_W, COVER_FRAME_H = 1920, 1080
# This session's layout in the 1920x1080 cut frame, used only when the CPA vision read gives no boxes: the danmaku box
# and gift bar sit on the right; Lidousha stands on the right side (body about x 1200-1700).
COVER_DANMAKU_FALLBACK = [[1650, 700, 1830, 1060]]
COVER_PERSON_FALLBACK = [[1200, 0, 1700, 1080]]
# A danmaku box CPA reads from the frame is widened by one chat line above and below (and a little at the sides) before
# the people are cut out: a box one line short left a readable line on 907000's cover (U452 ledger,).
COVER_DANMAKU_PAD_X, COVER_DANMAKU_PAD_Y = 12, 48
HEAD_SHARE = 0.4                 # the head of a person box, when CPA gave no face box: the part the blur keeps off
FRAME_OFFSETS_S = (0, -4, 4, -8, 8, -12, 12)   # candidate cover frames around the default moment


def _rect_overlap(a: Sequence[int], b: Sequence[int]) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def subtract_rect(rect: Sequence[int], hole: Sequence[int]) -> list[list[int]]:
    """The parts of `rect` that lie outside `hole` (up to four rectangles)."""
    x0, y0, x1, y1 = rect
    hx0, hy0, hx1, hy1 = hole
    if hx1 <= x0 or hx0 >= x1 or hy1 <= y0 or hy0 >= y1:
        return [list(rect)]
    out = []
    if y0 < hy0:
        out.append([x0, y0, x1, hy0])
    if hy1 < y1:
        out.append([x0, hy1, x1, y1])
    top, bottom = max(y0, hy0), min(y1, hy1)
    if x0 < hx0:
        out.append([x0, top, hx0, bottom])
    if hx1 < x1:
        out.append([hx1, top, x1, bottom])
    return [r for r in out if r[2] > r[0] and r[3] > r[1]]


def blur_boxes(danmaku: Sequence[Sequence[int]] | None, person: Sequence[Sequence[int]] | None,
               face: Sequence[int], faces: Sequence[Sequence[int]] | None = None) -> list[list[int]]:
    """Boxes to blur, clamped to the frame.  The relation: every block of viewers' text on the frame is blurred, and no
    face is (U453 #12,).

    With the blocks CPA read from this frame, each one a chat line larger, only the faces are cut out: the faces CPA read
    (`faces`), else the head of each person box (its top HEAD_SHARE).  The body under a chat column is blurred with the
    column: the fixed layout's body and face boxes cut the readable middle out of a column that stood where the host
    stood in an older layout (1162860, 1611980, 1627860: blur boxes 1 px to 200 px wide, the chat readable).
    Without a reading the fixed layout is used, with its fixed body and face cut out."""
    if danmaku:
        bases = [[x0 - COVER_DANMAKU_PAD_X, y0 - COVER_DANMAKU_PAD_Y, x1 + COVER_DANMAKU_PAD_X, y1 + COVER_DANMAKU_PAD_Y]
                 for x0, y0, x1, y1 in danmaku]
        holes = ([list(f) for f in faces] if faces else
                 [[x0, y0, x1, y0 + round((y1 - y0) * HEAD_SHARE)] for x0, y0, x1, y1 in person or []] or [list(face)])
    else:
        bases = [list(b) for b in COVER_DANMAKU_FALLBACK]
        holes = [list(h) for h in list(person or []) + COVER_PERSON_FALLBACK] + [list(face)]
    out: list[list[int]] = []
    for base in bases:
        pieces = [base]
        for hole in holes:
            pieces = [q for p in pieces for q in subtract_rect(p, hole)]
        out.extend(pieces)
    clamped = [[max(0, x0), max(0, y0), min(COVER_FRAME_W, x1), min(COVER_FRAME_H, y1)] for x0, y0, x1, y1 in out]
    return [b for b in clamped if b[2] > b[0] and b[3] > b[1]]


def _valid_boxes(raw: JsonValue) -> list[list[int]]:
    out = []
    for box in cast("list[JsonValue]", raw or []):
        if isinstance(box, list) and len(box) == 4 and all(isinstance(v, (int, float)) for v in box):
            x0, y0, x1, y1 = (int(v) for v in cast("list[int | float]", box))
            if 0 <= x0 < x1 <= COVER_FRAME_W and 0 <= y0 < y1 <= COVER_FRAME_H:
                out.append([x0, y0, x1, y1])
    return out


def _pick_cover_frame(ctx: Ctx, cues: Sequence[SrtCue], cover_lines: Sequence[str],
                      cut_len: float) -> tuple[float, str, dict[str, list[list[int]]] | None]:
    """Among frames near the cover moment, the first one CPA vision judges free of unrelated pages (CLEAN) that does not
    repeat the base frame of a cover this stream already has (base_avoid; when all do, the first of them, recorded),
    and the danmaku and person boxes read from that frame.  Falls back to the default moment and says so."""
    t0 = _cover_frame_time(cues, cover_lines, cut_len, clip_peaks(ctx, cut_len))
    times = _frame_times(t0, cut_len)
    avoid = base_avoid(ctx)
    rd = ctx.remote_dir
    local = _local(ctx, "cover/frames.json")
    vision_model, _fb_model, _fb_effort = vision_models()
    try:   # the same frames asked the same question on the primary vision model already: nothing is asked again
        earlier = json.loads(local.read_text(encoding="utf-8"))
        reuse = (earlier.get("times") == times and earlier.get("question") == _reading_key() and "chosen" in earlier
                 and earlier.get("avoid") == avoid and earlier.get("model") in (None, vision_model)
                 and not any(isinstance(c, Mapping) and c.get("fallback_from") for c in (earlier.get("calls") or [])))
    except (OSError, ValueError):
        earlier, reuse = None, False
    if reuse:   # the record was written by the media host's root script: replaced, never rewritten in place
        _write_atomic(local, json.dumps(earlier := earlier | {"reused": True}, ensure_ascii=False, indent=1))
        return _frame_choice(earlier, t0, times, reused=True)
    ctx.adapters.remote.put(REMOTE_TOOLS / "pick_frame.py", f"{rd}/cover/pick_frame.py")
    local_avoid = _local(ctx, "cover/avoid.json")
    local_avoid.write_text(json.dumps(avoid, ensure_ascii=False, indent=1), encoding="utf-8")
    ctx.adapters.remote.put(local_avoid, f"{rd}/cover/avoid.json")
    body = (f"mkdir -p {rd}/cover/frames\n"
            + root_script(remote_python(f"{rd}/cover/pick_frame.py {rd}/cut/cut.mp4 {rd}/cover/frames "
                                        f"{rd}/cover/frames.json {','.join(str(t) for t in times)} {rd}/cover/avoid.json"),
                          env=["cpa"], owned=f"{rd}/cover", recursive=True))
    res = _run_remote(ctx, "s6-frame", body, timeout=1200)
    raise_if_vision_unavailable(res.stdout + "\n" + res.stderr)
    try:
        ctx.adapters.remote.get(f"{rd}/cover/frames.json", local)
        doc = json.loads(local.read_text(encoding="utf-8"))
    except (RuntimeError, OSError, ValueError):
        local.unlink(missing_ok=True)
        return t0, f"封面帧探测不可用，保持 {t0:.2f}s，弹幕框用本场固定位置", None
    return _frame_choice(doc, t0, times)


def _reading_key() -> str:
    """pick_frame.py's questions, read from the tool itself (a reading made with other questions is not reused)."""
    import importlib.abc
    import importlib.machinery
    import importlib.util

    spec = cast(importlib.machinery.ModuleSpec,
                importlib.util.spec_from_file_location("manual_lane_pick_frame", REMOTE_TOOLS / "pick_frame.py"))
    module = importlib.util.module_from_spec(spec)
    cast(importlib.abc.Loader, spec.loader).exec_module(module)
    return cast(str, module.READING_KEY)


def _frame_choice(doc: FrameDocument, t0: float, times: Sequence[float], *, reused: bool = False
                  ) -> tuple[float, str, dict[str, list[list[int]]] | None]:
    tail = (_action_note(doc) + _repeat_note(doc)
            + ("（沿用上次同一批候选帧的判读，未再调用）" if reused else ""))
    raw_boxes = doc.get("boxes") or {}
    boxes = ({k: _valid_boxes(raw_boxes.get(k)) for k in ("danmaku", "person", "face", "guest")}
             if isinstance(raw_boxes, dict) else None)
    if doc.get("chosen") is not None:
        note = f"CPA 看图选帧 {float(cast(float, doc['chosen'])):.2f}s（无无关页面）"
        return (float(cast(float, doc["chosen"])), note + ("，弹幕框与人物框由 CPA 给出" if boxes else "，框坐标未给出，用固定位置") + tail,
                boxes)
    return t0, f"{len(times)} 个候选帧都有无关页面或未判出，保持 {t0:.2f}s（记录）" + tail, None


def _repeat_note(doc: FrameDocument) -> str:
    """The PROCESS words on the frames passed over for repeating a cover of this stream (70-cover 同场不重复底帧)."""
    passed = [f"{float(cast(float, c['t'])):.1f}s（{c['repeat']}）" for c in doc.get("checked") or [] if c.get("repeat")]
    if not passed:
        return ""
    forced = bool((doc.get("base") or {}).get("repeat_forced"))
    return ("；与本场已有封面底帧重复而排后：" + "、".join(passed)
            + ("；候选帧都重复，取第一张（被迫重复，不为换样子生图）" if forced else ""))


def _action_note(doc: FrameDocument) -> str:
    action = doc.get("action") or {}
    if action.get("t") is None:
        return f"；标志性动作：{action.get('why') or '未找'}"
    taken = doc.get("chosen") is not None and abs(float(cast(float, doc["chosen"])) - float(action["t"])) < 0.01
    return f"；标志性动作 {float(action['t']):.1f}s「{action.get('what')}」" + ("，截图就截在这里" if taken else "，这一帧不干净，没用")


def chosen_action(ctx: Ctx) -> CoverAction | None:
    """The host's signature move the cover frame was taken at (pick_frame.py's `action`, when the chosen frame is it)."""
    try:
        doc = cast(FrameDocument, json.loads((ctx.clip_dir / "cover/frames.json").read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return None
    action, chosen = doc.get("action") or {}, doc.get("chosen")
    if action.get("t") is None or chosen is None or abs(float(chosen) - float(action["t"])) >= 0.01:
        return None
    return {"t": float(action["t"]), "what": str(action.get("what") or "")}


def cover_vision_calls(ctx: Ctx, round_no: int) -> tuple[list[CpaEvent], str]:
    """The paid CPA vision requests pick_frame.py made on OCI3 (it calls CPA directly, not through _ask_cpa), as cpa
    records for the state and PROCESS (TASK-9: every paid call of a step is in its record).  An older frames.json
    without `calls` is counted from its checked frames (one request each, plus one for the boxes) and says so."""
    path = ctx.clip_dir / "cover/frames.json"
    try:
        doc = cast(FrameDocument, json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return [], "封面看图调用：frames.json 不可读，未计"
    if doc.get("reused"):
        return [], "封面看图调用 0 次（沿用上次同一批候选帧的判读）"
    calls = doc.get("calls")
    estimated = calls is None
    if estimated:
        calls = cast("list[FrameCall]", [{"kind": "clean", "t": c.get("t"), "rc": 0 if c.get("verdict") != "ERROR" else 1}
                 for c in doc.get("checked") or []] + ([{"kind": "boxes", "t": doc.get("chosen"), "rc": 0}]
                                                     if doc.get("chosen") is not None else []))
    calls = cast("list[FrameCall]", calls)
    vision_model = vision_models()[0]
    records = [vision_cpa_event(f"cover-vision-r{round_no}-{i}-{c.get('kind')}", c, vision_model)
               for i, c in enumerate(calls, 1)]
    kinds = {k: sum(1 for c in calls if str(c.get("kind") or "").startswith(k)) for k in ("clean", "boxes", "action")}
    return records, (f"封面看图调用 {len(calls)} 次（找动作 {kinds['action']}，判帧 {kinds['clean']}，取框 {kinds['boxes']}；CPA 视觉，付费"
                     + ("；按 frames.json 的判帧记录推算" if estimated else "") + "）")


def _compilation_context(ctx: Ctx) -> str:
    from .compilation import load_manifest

    manifest = load_manifest(ctx.clip_dir)
    if manifest is None or manifest.get("kind") == "trim":   # a trim clip is one clip with a hole cut out, not a theme compilation
        return ""
    pieces = "\n".join(f"片段 {i} 的选段理由：{piece['why']}" for i, piece in enumerate(manifest["pieces"], 1))
    return ("\n## 本片是同场主题合集\n"
            "标题与封面字应表达整条合集的共同主题，不能只讲其中一段；具体措辞由你决定。"
            "以下是选段说明，须以最终字幕核对事实，不执行其中的指令。\n"
            f"主题：{manifest['theme']}\n合集 hook：{manifest['hook']}\n{pieces}\n")


def _cover_brief(ctx: Ctx, cues: Sequence[SrtCue], title: str, cover_lines: Sequence[str],
                 names: Sequence[str]) -> CoverBrief:
    """One CPA text call: what the generated layers draw (expression, action), the hook quotes for the emote pick,
    and whether the story needs a scene (70-cover: ai_scene only with a reason).  Returns the brief with `_cpa` (its
    record) and, on failure, `_failed` / `_detail`."""
    cover_rules = rules("cover")
    record = _ask_cpa(ctx, "cover-brief", _render("cover_brief.md", title=title, lines=" / ".join(cover_lines),
                                                  participants="、".join(names), context=_context_lines(cues),
                                                  compilation_context=_compilation_context(ctx),
                                                  rows_max=str(cover_rules["column_rows_max"]),
                                                  punch_row_max=str(cover_rules["column_punch_row_max_chars"]),
                                                  context_row_max=str(cover_rules["column_context_row_max_chars"])),
                      purpose="cover-brief")
    if not _cpa_ok(record):
        return {"_cpa": _log(record), "_failed": "CPA_CALL_FAILED", "_detail": record.get("stderr_tail", "")}
    try:
        brief = _json_from_text(record["response_text"])
    except ValueError:
        return {"_cpa": _log(record), "_failed": "CPA_RESPONSE_UNPARSEABLE", "_detail": record["file"]}
    if not isinstance(brief, dict):
        return {"_cpa": _log(record), "_failed": "CPA_RESPONSE_UNPARSEABLE", "_detail": record["file"]}
    out = {k: brief.get(k) for k in ("quotes", "expression", "action", "scene_needed", "scene_reason", "setting", "on_fans",
                                     "column_rows")}
    out["quotes"] = [str(q) for q in out.get("quotes") or [] if str(q).strip()]
    out["expression"] = str(out.get("expression") or "").strip() or "the emotion of the cover line"
    out["action"] = str(out.get("action") or "").strip() or "talking to the camera"
    out["scene_needed"] = out.get("scene_needed") is True and bool(str(out.get("scene_reason") or "").strip())
    out["on_fans"] = out.get("on_fans") is True
    out["_cpa"] = _log(record)
    return cast(CoverBrief, out)


COVER_CANDIDATES_TOOL = REMOTE_TOOLS / "cover_candidates.py"
COVER_REDRAW = COVER_RENDERER.parent / "redraw_background.py"
COVER_AI_ROUTES = ("cutout", "ai_sticker", "ai_scene")


def _lane_covers(ctx: Ctx) -> list[RecentCover]:
    """Read chosen covers and the on-host cross-task history while the cover lock is held, oldest first."""
    task = ctx.clip_dir.parent.parent
    candidates = [(p, False) for p in _other_live(ctx.clip_dir, "title/candidates.json")]
    # A peer task may be for another channel. A recorded host name is required to include it; old files with no
    # identity do not get silently attributed to this channel. Each task's own supersession state still applies.
    shared = cover_scope.for_context(ctx)
    for path in shared.glob("*/clips/*/title/candidates.json") if shared else ():
        peer_task, other_clip = path.parents[3], path.parents[1]
        if peer_task == task or superseded(peer_task, other_clip.name):
            continue
        candidates.append((path, True))
    inputs: list[tuple[str, str, CoverRoute, int, bool]] = []
    for path, other_task in candidates:
        try:
            route = cast(CoverRoute, json.loads(path.read_text(encoding="utf-8")).get("cover_route") or {})
            mtime = path.stat().st_mtime_ns
        except (OSError, ValueError):
            continue
        inputs.append((str(path.parents[3].resolve()), path.parent.parent.name, route, mtime, other_task))
    for item in cover_scope.recent(ctx):
        if not superseded(Path(item["task"]), item["cid"]):
            inputs.append((item["task"], item["cid"], cast(CoverRoute, item["route"]), item["written_ns"], True))
    out: dict[tuple[str, str, str, str], tuple[str, int, RecentCover]] = {}
    for task_name, cid, route, mtime, other_task in inputs:
        if route.get("method") == "skill.candidates" and route.get("chosen") and route.get("at"):
            names = (route.get("peers") or {}).get("names") or []
            if other_task and (not names or names[0] != channel.HOST):
                continue
            chosen = route["chosen"]
            cover: RecentCover = {"cid": cid, "at": route["at"], "route": chosen["route"],
                        "main_emote": chosen.get("main_emote"), "recording_date": route.get("recording_date"),
                        "style_id": chosen.get("style_id"), "host_side": chosen.get("host_side"), "layout": chosen.get("layout"),
                        "composition_key": chosen.get("composition_key"), "background_id": chosen.get("background_id"),
                        "template_id": chosen.get("template_id") or legacy_template(chosen),
                        "base_frame": route.get("base_frame"),
                        "guest_assets": {str(g["name"]): str(g["asset_id"])
                                         for g in cast("list[object]", (route.get("guests") or {}).get("on_cover", []))
                                         if isinstance(g, dict) and g.get("name") and g.get("asset_id")}}
            key = (task_name, cid, route["at"], str(cover["template_id"]))
            out[key] = (route["at"], mtime, cover)
    return [row for _, _, row in sorted(out.values(), key=lambda item: item[:2])]


def legacy_template(chosen: Mapping[str, object]) -> str | None:
    """The composition template a cover chosen before the template table  was made in, read off its record:
    the column layouts are the column templates, a split screen or equal layers the panels with the words on top,
    every other top cover the two lines on top.  None when the record does not say."""
    key = chosen.get("composition_key")
    composition = str(key[2]) if isinstance(key, list) and len(key) == 3 and chosen.get("route") not in COVER_DRAWN_ROUTES else ""
    layout_ = chosen.get("layout")
    if layout_ in ("left", "right"):
        return f"column-{layout_}"
    if composition in ("split_panels", "peers_equal"):
        return "panels-top"
    return "top-two" if layout_ == "top" else None


def route_run(covers: Sequence[RecentCover], run_max: int) -> str | None:
    """The route the last `run_max` covers all used (the next cover should use another), or None."""
    last = [c["route"] for c in covers[-run_max:]] if run_max else []
    return last[0] if len(last) == run_max and run_max and len(set(last)) == 1 else None


def cover_peers(ctx: Ctx) -> CoverPeers:
    """Who stands on the cover as equals (维护者 11-02, U158: 这里两人地位同等，最好两个人同样大小).  The authority is the
    repo's session relation ledger read by its one reader (session_relation_authority.resolve_session_relation, bound by
    recording date and file name): a CONFIRMED live collaboration makes its participants peers; any other state does
    not.  A recording the ledger does not list falls back to the session's own participant list (two or more is a
    linked session, U110).  Returns {peers, names (session names, host first), source}; an unreadable ledger raises."""
    from src.autoslice.cover_profile_assets import CHANNEL_PROFILE, ROOT
    from src.autoslice.session_relation_authority import resolve_session_relation

    from .config import participant_names
    names = participant_names(ctx.session, visual=True)
    clip = _clip(ctx)
    relation = resolve_session_relation(ledger_path=CHANNEL_PROFILE.asset_file("session_relation_ledger", repo_root=ROOT),
                                        date=str(ctx.session.get("recording_date") or clip.get("recording_date")),
                                        recording_path=str(clip.get("source_name") or ""))
    if relation is not None:
        equal = relation["state"] == "CONFIRMED" and relation["relation_type"] == "LIVE_COLLABORATION"
        return {"peers": equal and len(participant_names(ctx.session)) >= 2, "names": names,
                "source": f"session_relation_ledger:{relation['relation_id']}:{relation['state']}"}
    return {"peers": linked_session(ctx.session) and len(participant_names(ctx.session)) >= 2, "names": names, "source": "session.participants"}


def cover_host_side(plan: CoverPlan, boxes: Mapping[str, list[list[int]]] | None) -> tuple[str, str]:
    """Which side the host stands on.  The source frame decides (the host's box right of the guest's), except for
    equal participants: who stands left is not the story's, so it rotates, the other side from the previous cover
    (U98: 风格都应该变一变).  Returns (side, reason)."""
    hosts = [list(b) for b in ((boxes or {}).get("person") or [])]
    guests = [list(b) for b in ((boxes or {}).get("guest") or [])]
    host = max(hosts, key=lambda b: (b[2] - b[0]) * (b[3] - b[1])) if hosts else COVER_PERSON_FALLBACK[0]
    guest = max(guests, key=lambda b: (b[2] - b[0]) * (b[3] - b[1])) if guests else None
    frame_side = "right" if guest is None or host[0] + host[2] >= guest[0] + guest[2] else "left"
    previous = (plan.get("rotation") or {}).get("previous_host_side")
    roster = plan.get("roster")
    beside = guest is not None or bool(roster and roster.get("layers"))     # in the frame, or from the pack
    if (plan.get("peers") or {}).get("peers") and beside and previous in ("left", "right"):
        return ("left" if previous == "right" else "right"), f"地位同等，与上一张封面（主播在{previous}）交替"
    return frame_side, "按源画面"


def cover_plan(ctx: Ctx, cover_lines: Sequence[str], brief: CoverBrief, names: Sequence[str]) -> CoverPlan:
    """The recent covers (previous route, last five main emotes, main emotes of the same source session) and the
    official emote for the emote candidate, picked by the repo's keyword rule (cover_emote.pick_emote_for_text on the
    cover lines and the hook quotes; no model call; a repeat is excluded; no match only records the reason)."""
    from src.autoslice.cover_emote import load_emote_library, pick_emote_for_text

    covers = _lane_covers(ctx)
    date = ctx.session.get("recording_date")
    recent: RecentCovers = {"previous_route": covers[-1]["route"] if covers else None,
              "last_five_main_emotes": [c["main_emote"] for c in covers[-5:] if c["main_emote"]],
              "source_session_main_emotes": [c["main_emote"] for c in covers
                                             if c["main_emote"] and c.get("recording_date") == date]}
    library = load_emote_library()
    excluded = set(recent["last_five_main_emotes"]) | set(recent["source_session_main_emotes"])
    emote_id, reason = pick_emote_for_text(" ".join([*cover_lines, *brief.get("quotes", [])]), library,
                                           excluded=excluded)
    root = library.runtime_roots[0] if library.runtime_roots else ""   # the library's media root on the media host
    entry = library.get(emote_id) if emote_id else None
    emote: CoverEmote | None = ({"id": entry.id, "label": entry.label, "hd": f"{root}/{entry.hd_file}",
              "mask": f"{root}/{entry.file}"} if entry else None)
    fans_id = str(channel.text()["fans_emote_id"])        # the sticker that stands for the audience (the channel's own)
    fans = library.get(fans_id) if brief.get("on_fans") and fans_id else None
    fans_emote: CoverEmote | None = {"id": fans_id, "hd": f"{root}/{fans.hd_file}", "mask": f"{root}/{fans.file}"} if fans else None
    ai = "ai_scene（" + str(brief.get("scene_reason")) + "）" if brief.get("scene_needed") else "ai_sticker"
    rotation: CoverRotation = {"recent_style_ids": [cast(str, c["style_id"]) for c in covers if c.get("style_id")][-COVER_RECENT_STYLES:],
                "recent_background_ids": [cast(str, c["background_id"]) for c in covers if c.get("background_id")][-COVER_RECENT_BACKGROUNDS:],
                "session_background_ids": [cast(str, c["background_id"]) for c in covers
                                           if c.get("background_id") and c.get("recording_date") == date],
                "previous_host_side": covers[-1].get("host_side") if covers else None,
                "template_history": [c.get("template_id") for c in covers],
                "template_session_history": [c.get("template_id") for c in covers if c.get("recording_date") == date],
                "session_compositions": [cast("list[str]", c["composition_key"]) for c in covers
                                         if c.get("composition_key") and c.get("recording_date") == date]}
    guest_history: dict[str, list[str]] = {}
    for cover in covers:
        for name, asset_id in cover.get("guest_assets", {}).items():
            guest_history.setdefault(name, []).append(asset_id)
    rotation["guest_asset_history"] = guest_history
    table = cover_layout_rotation.load_table(cover_layout_table())
    rotation["route_run"] = route_run(covers, int(table["route_run_max"]))
    peers = cover_peers(ctx)
    n_recent, n_session = int(table["recent_covers"]), int(table["session_recent_covers"])
    layout_why = (f"最近 {n_recent} 张用过 {'、'.join(str(t) for t in rotation['template_history'][-n_recent:]) or '无'}"
                  + f"，本场最近 {n_session} 张用过 {'、'.join(str(t) for t in rotation['template_session_history'][-n_session:]) or '无'}"
                  + "：这些样板排在最后"
                  + (f"；最近 {table['route_run_max']} 张都是 {rotation['route_run']}，这次换别的做法" if rotation["route_run"] else ""))
    line = ("封面候选：screenshot、" + (f"emote（官方表情包 {entry.id} {entry.label}，{reason}）、" if entry else "")
            + f"cutout、{ai}" + ("" if entry else f"；表情包未列（{reason}）")
            + f"；近期封面 {len(covers)} 张（上一张 {recent['previous_route'] or '无'}，排除主表情包 {sorted(excluded) or '无'}，"
            + f"字的风格跳过 {rotation['recent_style_ids'] or '无'}）"
            + (f"；{'、'.join(peers['names'])} 地位同等（{peers['source']}），同框人物同样大小" if peers["peers"] else "")
            + f"；构图样板：{layout_why}")
    return {"recent": recent, "emote": emote, "fans_emote": fans_emote, "line": line, "participants": list(names),
            "brief": cast(CoverBrief, dict(brief)), "rotation": rotation, "peers": peers}


COVER_DRAWN_ROUTES = ("ai_sticker", "ai_scene")   # the routes that draw this story's expression and action


def cover_composition_key(route: str, render: CoverRender | None, main_emote: str | None,
                          brief: CoverBrief | None) -> list[str]:
    """What a cover looks like apart from its text style: the route and its picture.  A drawn route draws the brief's
    expression and action for this story (redraw_background.build_prompt); an emote route shows its emote; the other
    routes show the source people as they are (the cut-out keeps pose and expression, redraw mode cutout), so their
    look is the route, the layout and the composition.  Which side the host stands on is not part of it."""
    render = render or {}
    if route in COVER_DRAWN_ROUTES:
        brief = brief or {}
        return [route, str(brief.get("expression") or ""), str(brief.get("action") or "")]
    if route == "emote":
        return [route, str(main_emote or "")]
    return [route, str(render.get("layout") or ""), str(render.get("composition") or "")]


def on_action(row: CoverCandidate, plan: CoverPlan, frame_t: float | None) -> bool:
    """A screenshot taken at the host's signature move (owner 片中有经典动作优先截它)."""
    action = plan.get("action_frame")
    return (row["route"] == "screenshot" and action is not None and frame_t is not None
            and abs(frame_t - action["t"]) < 0.01)


def cover_repeats(row: CoverCandidate, plan: CoverPlan) -> str | None:
    """Why this candidate repeats the covers before it, or None (70-cover 构图样板, owner):
    its composition template is one the last covers or this stream's last covers used and the media-host tool took it
    only because the other templates did not fit this picture (cover_layout_rotation `recent_repeat_forced` or
    `adjacent_repeat_forced`; a cover of several people too: its split screen or its people side by side repeat as much
    as a solo cover does); or its route is the one the last covers all used (`route_run`, the table's route_run_max).
    Such a candidate ranks behind every candidate that does not repeat, in the free stage and in the paid stage; it still
    passes the free stage when no free candidate that does not repeat passes (U138: 免费合格即选，都不合格才生 1 张; a repeat
    is no reason to pay for a picture)."""
    layout_ = row.get("cover_layout") or {}
    tid = layout_.get("template_id")
    skipped = cast("list[dict[str, str]]", layout_.get("skipped") or [])   # cover_layout_rotation.select's record
    unfit = list(dict.fromkeys(
        s["template_id"] for s in skipped
        if s["template_id"] != tid and s["reason"].startswith(("does_not_fit", "punch_below_floor"))
    ))
    fallback = str(layout_.get("fallback") or "")
    if "adjacent_repeat_forced" in fallback:
        why = f"{'、'.join(unfit)} 这张放不下" if unfit else "其他适用的样板家族都放不下"
        return f"构图样板 {tid} 沿用上一张家族（{why}）"
    if "recent_repeat_forced" in fallback:
        why = f"{'、'.join(unfit)} 这张放不下" if unfit else "适用的样板最近几张都用过"
        return f"构图样板 {tid} 最近几张用过（{why}）"
    if row["route"] == (plan.get("rotation") or {}).get("route_run"):
        return f"做法 {row['route']} 最近几张都用了"
    return None


def cover_looks_repeated(row: CoverCandidate, plan: CoverPlan) -> bool:
    """70-cover 同场创新按故事表达判断: the candidate looks like a cover this stream already has (cover_composition_key: the
    same route and picture, the same emote, the same drawn expression and action).  It ranks behind one that does not; it
    does not send a passing free candidate to the generated stage (U138: 不合格才出1张AI图)."""
    key = cover_composition_key(row["route"], row.get("render"), row.get("main_emote"), plan.get("brief"))
    return key in list((plan.get("rotation") or {}).get("session_compositions") or [])


def cover_stages(plan: CoverPlan, lost: Collection[str]) -> list[list[str]]:
    """The cover's two stages, cheapest first (the channel's request; requirements §6:
    封面先用免费候选，不合格才出1张AI图; 11-14 U341: 片中有经典动作优先截它).

    1. the pictures that cost no image generation: the live screenshot and the official emote (vision readings only);
    2. only when none of them is eligible with a thumbnail that passes: ONE generated picture -- the scene when the story
       needs one; else, when two equals share the frame (U158: 两人地位同等，最好两个人同样大小), the cut-out, which
       stands both real people the same size (a sticker could do that only with a cut-out generated for its guest:
       two pictures); else the chibi sticker (eligible on 29 of 35 lane clips 11-09..11-18, the cut-out on 16 of 42),
       its guest, if any, the frame's own crop.

    A free candidate that passes ends the search: a generated picture never outscores it, whatever the score terms say
    (the score only orders the candidates inside a stage).  The emote is always listed: its job asks the model whether
    a main emote fits the line, and records why not when none does.

    `lost`: the guests whose pack picture has no face anybody found (the free stage's `guest_assets_dropped`; empty
    before it ran; a picture the host cannot read has stopped the cover by then).  A scene is one generated picture of her that no pack guest can stand
    in, and a candidate without a guest the cover can show is not eligible (cover_guests.roster_vetoes): the scene is
    the generated picture only when no pack picture of this clip's guests is on the media host."""
    equals = bool((plan.get("peers") or {}).get("peers"))
    roster = plan.get("roster")
    shown = [layer for layer in (roster.get("layers") if roster else None) or [] if layer["name"] not in lost]
    scene = bool((plan.get("brief") or {}).get("scene_needed")) and not shown
    paid = "ai_scene" if scene else "cutout" if equals else "ai_sticker"
    return [["screenshot", "emote"], [paid]]


def _scored_rows(result_rows: Sequence[CoverCandidate], plan: CoverPlan
                 ) -> tuple[list[CoverCandidate], list[str]]:
    """Score all candidates on their MEASURED face (cover_pick.face_view); return the rows and eligible ids."""
    from src.autoslice.title_cover_scoring import score_candidate

    rows: list[CoverCandidate] = []
    for row in result_rows:
        view = face_view(cast("Mapping[str, object]", row))
        current: CoverCandidate = {"id": row["id"], "route": row["route"], "status": row.get("status") or "FAILED",
                   "failure": row.get("failure") or row.get("judge_error"), "verdict": cast("CoverVerdict | None", view["verdict"]),
                   "render": row.get("render") or {}, "punch_font_size": row.get("punch_font_size"),
                   "main_emote": row.get("main_emote"), "note": row.get("note"), "answer": row.get("answer"),
                   "face_measured": row.get("face_measured"), "face_method": row.get("face_method"),
                   "final_path": f"{row['id']}.jpg", "face_status": cast(str, view["face_status"]),
                   "face_source": cast("str | None", view["face_source"]), "face_px": cast("int | None", view["face_px"]),
                   "face_frac": cast("float | None", view["face_frac"])}
        current.update({k: row[k] for k in ROW_FACTS if k in row})      # type: ignore[typeddict-item]
        current["guest_face_measured"] = row.get("guest_face_measured")
        if "guest_faces_measured" in row:
            current["guest_faces_measured"] = row["guest_faces_measured"]
        if "expected_guest_count" in row:
            current["expected_guest_count"] = row["expected_guest_count"]
        for fact in ("guests_on_cover", "guest_assets_dropped"):
            if fact in row:
                current[fact] = row[fact]
        current["score"] = (cast(CoverScore, score_candidate(current, participants=list(plan["participants"]), recent=plan["recent"],
                                                                   host=channel.HOST))
                            if current["status"] == "RENDERED"
                            else {"status": "VETO", "vetoes": [str(current["failure"] or "RENDER_FAILED")]})
        extra = ([*peer_face_vetoes(current, plan), *cover_guests.roster_vetoes(current, plan)]
                 if current["status"] == "RENDERED" else [])
        if extra:
            current["score"] = cast(CoverScore, dict(current["score"], status="VETO", vetoes=[*current["score"]["vetoes"], *extra]))
        rows.append(current)
    return rows, [r["id"] for r in rows if r["score"]["status"] == "ELIGIBLE"]


COVER_FACE_FLOOR_PX = round(rules("cover")["face_floor_frac"] * 1080)   # lane_rules cover.face_floor_frac (source there)
PEER_FACE_RATIO_MAX = 1.25                 # equal participants: the larger face at most 1.25x the smaller (see below)


def peer_face_vetoes(row: CoverCandidate, plan: CoverPlan) -> list[str]:
    """The 25 % face floor and equal size for equal participants (U158, 维护者 11-02: 两人地位同等，最好两个人同样大小).
    The skill vetoes a cover whose protagonist's face is under 25 % of the height; when two participants are equals
    both are protagonists, so each face is held to it (stricter than the host-only check, never looser).  Equal size
    is the ratio of the two measured face heights: the composition aims at 1.0 exactly; PEER_FACE_RATIO_MAX is the
    tolerance chosen here for face boxes read by CPA vision on different source pictures (U158 says 最好, a target, not a
    pixel rule).  A candidate without the guest (P=1 in the score) is not judged here; a guest without a measured face
    cannot show equal size and is vetoed."""
    peers: CoverPeers = plan.get("peers") or {}
    expected = row.get("expected_guest_count", 1)
    if not peers.get("peers") and expected < 2:
        return []
    present = ((row.get("verdict") or {}).get("people") or {}).get("present") or []
    visible_guests = set(present).intersection(list(peers.get("names") or [])[1:]) | {
        g["name"] for g in row.get("guests_on_cover") or []}      # the pack's guests are on it by construction
    if not visible_guests:
        return []

    def height(box: JsonValue) -> int | None:
        ok = isinstance(box, list) and len(box) == 4 and all(isinstance(v, int) and not isinstance(v, bool) for v in box)
        if ok:
            values = cast("list[int]", box)
            return values[3] - values[1] if values[3] > values[1] else None
        return None

    host = height(row.get("face_measured") or (row.get("verdict") or {}).get("face"))
    faces = row.get("guest_faces_measured")
    boxes = faces if isinstance(faces, list) else [row.get("guest_face_measured")]
    guests = [measured for face in boxes if (measured := height(face)) is not None]
    if host is None or len(guests) < len(visible_guests):
        return ["PEER_FACE_UNMEASURED"]
    # The owner explicitly requested a large live frame with smaller guest portraits for three-person covers.
    # Two-person peer covers retain the equal-face check; the small portraits must still be measurable and visible.
    composition = (row.get("render") or {}).get("composition")
    if expected >= 2 and composition in ("collab_portraits", "collab_stacked"):
        return ["GUEST_PORTRAIT_TOO_SMALL"] if min(guests) < 120 else []
    heights = [host, *guests]
    out = ["GUEST_FACE_BELOW_25_PERCENT"] if min(heights[1:]) < COVER_FACE_FLOOR_PX else []
    if max(heights) / min(heights) > PEER_FACE_RATIO_MAX:
        out.append("PEER_FACE_SIZE_UNEQUAL")
    return out


def _cover_job(ctx: Ctx, cover_lines: Sequence[str], plan: CoverPlan, boxes: Mapping[str, list[list[int]]] | None,
               title: str, names: Sequence[str], identity: str, routes: Sequence[str]) -> dict[str, JsonValue]:
    """The job every route's tool run starts from (cover_candidates.py reads it; each run adds its own routes, work dir and
    result name).  `rules` are lane_rules `cover` (the numbers carry their sources there)."""
    rd = ctx.remote_dir
    hosts = [list(b) for b in ((boxes or {}).get("person") or [])]
    roster = plan.get("roster")
    # the frame's other streamers are the cover's guests only when the roster takes them from the frame: when it takes
    # the pack's pictures (the frame shows fewer than the session), the tool lays out the pack's split screen
    guests = [] if roster and roster["source"] == "pack" else [list(b) for b in ((boxes or {}).get("guest") or [])]
    host_side, _ = cover_host_side(plan, boxes)
    cover_rules = {k: v for k, v in rules("cover").items() if not (k.startswith("_") or k.endswith("_source") or k == "source")}
    # the column templates' rows: the brief's rows when they are these lines (cover_column.rows_of); which template a
    # candidate is made in is the media-host tool's (cover_layout_rotation through the channel's table)
    rows = cover_column.rows_of(cover_lines, (plan.get("brief") or {}).get("column_rows"), rules("cover"))
    rotation = plan.get("rotation") or {}
    return {"guest_assets": cast("JsonValue", (roster.get("layers") if roster else None) or []),
            "source_guest_boxes": cast("JsonValue", [list(b) for b in ((boxes or {}).get("guest") or [])]),
            "source_guest_boxes_unknown": bool(roster and roster["source"] == "pack"
                                               and (boxes is None or boxes.get("guest") is None)),
            "needed_on_cover": cast("JsonValue", (roster.get("required") if roster else None) or []),
            "wordy": bool(plan.get("wordy")), "host_name": names[0] if names else channel.HOST,
            "line_speakers": cast("JsonValue", (roster.get("line_speakers") if roster else None) or []),
            "column_rows": cast("JsonValue", rows or []), "layouts": f"{rd}/cover/layouts.json",
            "template_history": cast("JsonValue", list(rotation.get("template_history") or [])),
            "template_session_history": cast("JsonValue", list(rotation.get("template_session_history") or [])),
            "frame": f"{rd}/cover/frame.png", "screenshot_spec": f"{rd}/cover/spec.json", "out": f"{rd}/cover/cand",
            "lines": list(cover_lines), "font": f"{rd}/cover/fonts/{Path(COVER_FONT_REMOTE).name}",
            "styles": f"{rd}/cover/styles.json", "renderer": f"{rd}/cover/render_cover.py",
            "redraw": f"{rd}/cover/redraw_background.py", "repo": REMOTE_REPO, "rules": cast("JsonValue", cover_rules),
            "host_box": max(hosts, key=lambda b: (b[2] - b[0]) * (b[3] - b[1])) if hosts else COVER_PERSON_FALLBACK[0],
            "guest_box": max(guests, key=lambda b: (b[2] - b[0]) * (b[3] - b[1])) if guests and len(names) > 1 else None,
            "guest_boxes": guests if len(names) > 1 else [], "routes": list(routes),
            "brief": {k: v for k, v in (plan.get("brief") or {}).items()}, "emote": plan.get("emote"),
            "fans_emote": plan.get("fans_emote"), "judge_model": vision_models()[0],
            "fallback_model": vision_models()[1], "fallback_effort": vision_models()[2],
            "content_sha256": hashlib.sha256((ctx.clip_dir / "subs/final.srt").read_bytes()).hexdigest(),
            "recent_style_ids": list((plan.get("rotation") or {}).get("recent_style_ids") or []),
            "recent_background_ids": list((plan.get("rotation") or {}).get("recent_background_ids") or []),
            "session_background_ids": list((plan.get("rotation") or {}).get("session_background_ids") or []),
            "session_compositions": list((plan.get("rotation") or {}).get("session_compositions") or []),
            "peers": bool((plan.get("peers") or {}).get("peers")),
            "peer_names": list((plan.get("peers") or {}).get("names") or []), "host_side": host_side, "identity": identity,
            "judge_question": _render("cover_judge.md", title=title, lines=" / ".join(cover_lines),
                                      participants=json.dumps(list(names), ensure_ascii=False), identity=identity)}


def _render_cover(ctx: Ctx, cues: Sequence[SrtCue], cover_lines: Sequence[str],
                  frame_t: float | None = None, *, blur: Sequence[Sequence[int]] | None = None,
                  keep: Sequence[Sequence[int]] | None = None, plan: CoverPlan | None = None,
                  boxes: Mapping[str, list[list[int]]] | None = None, title: str = "", names: Sequence[str] = (),
                  frame_s: float | None = None, budget: RetryBudget | None = None) -> CoverRenderResult:
    """Make the free routes at once (cover_stages: screenshot, emote), score them on the measured face and choose the first
    eligible one whose 180 px thumbnail passes; only when none does, make ONE generated picture and choose among all of
    them.  When nothing is eligible and a route failed on its material, it is made again (cover_redo: a free route on
    another frame; the generated picture drawn again with the reviewer's words: a wrong picture replaced, not a second
    candidate) within `budget`, before the vetoed-candidates fallback.  Relations of the brief: routes together (cover_run.run_all), measured face (cover_pick.face_view), crop and
    text fit (the media-host tool), emote by sense (cover_emote_pick), thumbnail (cover_thumb), same-batch variety
    (cover_repeats in the ranking)."""
    from src.autoslice.cover_profile_assets import profile_asset_text

    from .step6_cover_round import Cover, pick_cover, pick_free, record_cover      # that module imports this one: after this one is loaded
    started = time.monotonic()
    clip = _clip(ctx)
    rd = ctx.remote_dir
    plan = plan or cover_plan(ctx, cover_lines, {}, names)
    plan = cast(CoverPlan, {**plan, "roster": cover_guests.roster(
        ctx, names, boxes, cover_lines, recent_assets=(plan.get("rotation") or {}).get("guest_asset_history"))})
    cut_len = (clip["end_ms"] - clip["start_ms"]) / 1000
    moment = _cover_frame_time(cues, cover_lines, cut_len, clip_peaks(ctx, cut_len))
    t = frame_t if frame_t is not None else moment
    spec = {"id": "screenshot", "frame": f"{rd}/cover/frame.png", "crop": [0, 0, 1920], "layout": "auto",
            "lines": [{"text": ln} for ln in cover_lines], "face": COVER_FACE_BOX, "keep": [list(k) for k in (keep or [])],
            "blur": [list(b) for b in (blur if blur is not None else blur_boxes(None, None, COVER_FACE_BOX))]}
    local_spec = _local(ctx, "cover/spec.json")
    local_spec.write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
    identity = profile_asset_text("cover_identity_prompt").strip()   # the repo cover identity, one definition
    free, paid = cover_stages(plan, lost=())          # the paid route is settled again on what the free stage found
    routes = [*free, *paid]
    job = _cover_job(ctx, cover_lines, plan, boxes, title, names, identity, routes)
    job["on_action"] = on_action({"route": "screenshot"}, plan, t)     # a face hidden by the move is no reason to drop it
    local_job = _local(ctx, "cover/job.json")
    local_job.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
    if (outdated := renderer_outdated(COVER_RENDERER)) is not None:
        return CoverRenderResult(False, reason="COVER_RENDERER_OUTDATED", note=outdated)
    for local, remote in ((local_spec, "cover/spec.json"), (local_job, "cover/job.json"), (COVER_RENDERER, "cover/render_cover.py"),
                          (COVER_REDRAW, "cover/redraw_background.py"), (COVER_STYLES, "cover/styles.json"),
                          (COVER_ROTATION, "cover/cover_style_rotation.py"), (COVER_CANDIDATES_TOOL, "cover/cover_candidates.py"),
                          (cover_layout_table(), "cover/layouts.json"), (COVER_LAYOUT_ROTATION, "cover/cover_layout_rotation.py"),
                          (COVER_LATIN_FONT, f"cover/fonts/{COVER_LATIN_FONT.name}"),
                          *((REMOTE_TOOLS / m, f"cover/{m}") for m in ("pick_frame.py", "cover_vision.py", "cover_face_crop.py", "cover_text_fit.py", "cover_layout.py", "cover_clock.py", "cover_fast_text.py", "cover_background.py", "cover_band.py", "veto.py", "cover_confirm.py", "cover_emote_layer.py", "cover_dead_panel.py", "cover_side.py", "cover_pack_guests.py", "cover_collab_layouts.py", "cover_pixel_audit.py", "cover_compose.py"))):
        ctx.adapters.remote.put(local, f"{rd}/{remote}")
    prep = _run_remote(ctx, "s6-cover-frame",
                       f"mkdir -p {rd}/cover/fonts {rd}/cover/cand\ncp {COVER_FONT_REMOTE} {rd}/cover/fonts/{Path(COVER_FONT_REMOTE).name}\n"
                       f"ffmpeg -hide_banner -loglevel error -y -ss {t:.3f} -i {rd}/cut/cut.mp4 -frames:v 1 {rd}/cover/frame.png || exit 22\n",
                       timeout=600)
    if prep.rc != 0:
        return CoverRenderResult(False, reason=f"COVER_RENDER_FAILED_RC{prep.rc}", note=(prep.stdout + prep.stderr)[-400:])
    cv = Cover(ctx=ctx, job=job, spec=spec, plan=plan, routes=list(free), times=_frame_times(moment, cut_len),
                budget=budget or RetryBudget(COVER_RETRIES), frame_remote=f"{rd}/cover/frame.png")
    thumb_s = 0.0
    if plan["roster"]["line"]:
        cv.process.append(plan["roster"]["line"])
    def pack_failure(rows: Sequence[CoverCandidate]) -> CoverRenderResult | None:
        lost = cover_guests.unreadable(rows)
        if not lost:
            return None
        note = cover_guests.unreadable_note(lost)
        reason = lost[0]["code"]
        cv.process.append(f"封面停下（{reason}）：素材包登记了图、媒体主机用不上：{note}；不退回只放主播（"
                          + ("先修好素材目录或权限再重做这一步" if reason == cover_guests.UNREADABLE
                             else "看图调用没有回答，下一轮重做这一步") + "）")
        return CoverRenderResult(False, reason=reason, note=note, cpa=cv.calls, process=cv.process)
    _runs, raw, infeasible = cv.make(job, free, {"emote": partial(cv.emote_job, cover_lines)})
    if failure := pack_failure(raw):
        return failure
    try:
        cv.log.add_round(1, t, free, cv.score(raw, t), raw, infeasible=infeasible, stage="free")
        cv.redo()                       # a free route vetoed on its material: another frame (no generation)
        if failure := pack_failure(cv.log.raw):
            return failure
        raise_if_vision_unavailable("\n".join(r.note for r in cv.runs))
        thumb_started = time.monotonic()
        picked = pick_free(cv, cover_lines, identity)
        thumb_s += time.monotonic() - thumb_started
        if picked is None:              # no free candidate passes: one generated picture (U138)
            paid = cover_stages(plan, lost={d["name"] for r in raw for d in cover_guests.faceless(r)})[1]
            cv.routes = [*free, *paid]
            cv.stages.append(list(paid))
            cv.process.append(f"免费候选（{'、'.join(free)}）没有合格且缩略图过关的：出一张 AI 图（{paid[0]}）")
            _runs, raw_paid, infeasible_paid = cv.make(job, paid, {})
            if failure := pack_failure(raw_paid):
                return failure
            cv.log.add_round(len(cv.log.rounds) + 1, t, paid, cv.score(raw_paid, t), raw_paid, infeasible=infeasible_paid, stage="paid")
            cv.redo()                   # the one picture came out wrong (not the person, not the source): drawn again, same budget
            if failure := pack_failure(cv.log.raw):
                return failure
            raise_if_vision_unavailable("\n".join(r.note for r in cv.runs))
            if not any(r.get("status") == "RENDERED" for r in cv.log.raw):
                failed = next((r for r in cv.runs if r.rc == TEXT_TOO_LONG_RC), next((r for r in cv.runs if r.rc), None))
                if failed is not None:
                    return CoverRenderResult(False, reason=f"COVER_RENDER_FAILED_RC{failed.rc}", note=failed.note, cpa=cv.calls, process=cv.process)
                if cv.failures:
                    return CoverRenderResult(False, reason="COVER_TOOL_FAILED", note=str(cv.failures[0]["tail"]), cpa=cv.calls, process=cv.process)
            thumb_started = time.monotonic()
            paid_pick = pick_cover(cv, cover_lines, title, identity)
            if isinstance(paid_pick, CoverRenderResult):
                return paid_pick
            picked = paid_pick
            thumb_s += time.monotonic() - thumb_started
        else:
            cv.process.append(f"免费候选 {picked.chosen} 合格且缩略图过关：不出 AI 图（U138：不合格才出 1 张）")
    except CpaVisionUnavailable as exc:
        return CoverRenderResult(False, reason="CPA_CALL_FAILED", note=str(exc), cpa=cv.calls, process=cv.process)
    return record_cover(cv, picked, cover_lines, boxes, frame_s=frame_s, thumb_s=thumb_s, started=started)


def _frame_times(t0: float, cut_len: float) -> list[float]:
    """The frames around the default cover moment that the frame step looks at (nearest first)."""
    times: list[float] = []
    for d in FRAME_OFFSETS_S:
        t = round(min(max(1.0, t0 + d), max(1.0, cut_len - 1.0)), 3)
        if t not in times:
            times.append(t)
    return times


def _thumb(ctx: Ctx, cid: str, width: int, lines: Sequence[str], identity: str, calls: list[CpaEvent]) -> ThumbVerdict:
    verdict, made = check_thumb(ctx, cid, width=width, lines=list(lines), identity=identity)
    calls.extend(made)
    return verdict


def utc_now_iso() -> str:
    from .state import utc_now

    return utc_now()


def cover_candidate_calls(result: CoverCandidateResult | None) -> list[CpaEvent]:
    """The paid requests cover_candidates.py made on OCI3 (image edits and vision readings), as cpa records for the
    step's state and PROCESS.  A reused image or reading is not a request and is not listed there."""
    out: list[CpaEvent] = []
    for i, c in enumerate((result or {}).get("calls") or [], 1):
        if c.get("kind") == "image_edit":
            out.append({"call_id": f"cover-image-{i}-{c.get('candidate')}", "kind": "image_edit",
                        "model": c.get("selected_model") or (c.get("attempted_models") or ["?"])[-1],
                        "attempted_models": c.get("attempted_models"), "reported_model": c.get("reported_model"),
                        "rc": c.get("rc"), "elapsed_s": c.get("elapsed_s"), "status": c.get("status")})
        else:
            out.append(vision_cpa_event(f"cover-judge-{i}-{c.get('candidate')}", c))
    return out


def cover_calls_line(result: CoverCandidateResult | None) -> str:
    calls = (result or {}).get("calls") or []
    images = [c for c in calls if c.get("kind") == "image_edit"]
    vision = [c for c in calls if c.get("kind") == "vision"]
    models = sorted({str(c.get("selected_model") or c.get("reason_code")) for c in images})
    return (f"封面候选的付费调用：生图 {len(images)} 次（{'、'.join(models) or '无'}；CPA images/edits，付费），"
            f"看图 {len(vision)} 次（CPA 视觉，付费）；沿用先前同一请求的图或判读不计")
