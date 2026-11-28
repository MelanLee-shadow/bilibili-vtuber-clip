"""Which composition a cover takes: the channel's template table and the one selector (no PIL dependency).

The authority is the channel asset ``cover_layout_templates.json`` (``assets/<channel>/``).  The production lane (step 6,
the media-host tool ``cover_candidates.py``) and the title-cover skill's ``render_cover.py`` both load this module and the
table; neither keeps its own list of layouts.  A template fixes where the words go (``text``: top, left, right, diagonal,
bottom), how they are broken (two lines, or a column of 3-4 short rows) and how the person is framed (face height, where
the face centre stands, how high the face sits).  Its ``applies`` says for which covers it can be made.

Selection (``select``): the templates that apply to this cover, minus those used by the last ``recent_covers`` covers and
the last ``session_recent_covers`` covers of the same stream, in the order least recently used first (never used first;
ties by the content hash); each is tried by the caller's ``attempt`` (it lays the cover out and renders it); one that does
not fit, or whose punchline stays under ``punch_floor_px``, is skipped with its reason and the next one is tried.  The
recently used ones come last, least recently used first, and taking one of them is recorded as ``recent_repeat_forced``.
The frame geometry a template asks for (``window``) is here too, so the skill and the lane crop a live frame the same way.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

SCHEMA = "cover-layout-templates.v2"
TEXTS = ("top", "left", "right", "diagonal", "bottom")
ROWS = ("lines", "column")
PEOPLE = ("solo", "together", "panels")
NEEDS = ("column_rows", "fans_emote", "watching")
FRAME_W, FRAME_H = 1920, 1080
# Minimum share of a template's punch_floor accepted by the below-floor last resort for pack collaborations.
PACK_PUNCH_FALLBACK_RATIO = 0.85


class CoverLayoutTableError(ValueError):
    pass


class TemplateRefused(ValueError):
    """Raised by an ``attempt`` when this template cannot be made for this cover (its reason is the message)."""


def _num(value: object, lo: float, hi: float) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and lo <= value <= hi


def validate_table(table: object) -> dict:
    if not isinstance(table, dict) or table.get("schema_version") != SCHEMA:
        raise CoverLayoutTableError("COVER_LAYOUT_TABLE_SCHEMA_INVALID")
    for key in ("recent_covers", "session_recent_covers", "route_run_max"):
        if isinstance(table.get(key), bool) or not isinstance(table.get(key), int) or table[key] < 0:
            raise CoverLayoutTableError(f"COVER_LAYOUT_TABLE_{key.upper()}_INVALID")
    if isinstance(table.get("punch_floor_px"), bool) or not isinstance(table.get("punch_floor_px"), int) or table["punch_floor_px"] < 120:
        raise CoverLayoutTableError("COVER_LAYOUT_TABLE_PUNCH_FLOOR_INVALID")
    for key in ("short_punch_chars", "short_punch_chars_people_min"):
        if isinstance(table.get(key), bool) or not isinstance(table.get(key), int) or table[key] < 1:
            raise CoverLayoutTableError(f"COVER_LAYOUT_TABLE_{key.upper()}_INVALID")
    templates = table.get("templates")
    if not isinstance(templates, list) or not templates:
        raise CoverLayoutTableError("COVER_LAYOUT_TABLE_TEMPLATES_INVALID")
    seen: set[str] = set()
    for t in templates:
        if not isinstance(t, dict):
            raise CoverLayoutTableError("COVER_LAYOUT_TABLE_TEMPLATES_INVALID")
        tid = t.get("template_id")
        if not isinstance(tid, str) or not tid or tid in seen:
            raise CoverLayoutTableError("COVER_LAYOUT_TABLE_TEMPLATE_ID_INVALID")
        seen.add(tid)
        if "family" in t and (not isinstance(t["family"], str) or not t["family"]):
            raise CoverLayoutTableError(f"COVER_LAYOUT_TABLE_FAMILY_INVALID:{tid}")
        if "pack_layout" in t and t["pack_layout"] not in ("split", "pair", "portraits", "stacked", "dialogue"):
            raise CoverLayoutTableError(f"COVER_LAYOUT_TABLE_PACK_LAYOUT_INVALID:{tid}")
        if t.get("text") not in TEXTS or t.get("rows") not in ROWS:
            raise CoverLayoutTableError(f"COVER_LAYOUT_TABLE_TEMPLATE_INVALID:{tid}")
        if t["rows"] == "column" and t["text"] not in ("left", "right"):
            raise CoverLayoutTableError(f"COVER_LAYOUT_TABLE_TEMPLATE_INVALID:{tid}")
        framing = t.get("framing")
        if not isinstance(framing, dict):
            raise CoverLayoutTableError(f"COVER_LAYOUT_TABLE_FRAMING_INVALID:{tid}")
        if framing.get("face_frac") is not None and not _num(framing["face_frac"], 0.25, 0.8):
            raise CoverLayoutTableError(f"COVER_LAYOUT_TABLE_FRAMING_INVALID:{tid}")
        for key in ("face_x_frac", "face_top_frac"):
            if framing.get(key) is not None and not _num(framing[key], 0.0, 1.0):
                raise CoverLayoutTableError(f"COVER_LAYOUT_TABLE_FRAMING_INVALID:{tid}")
        applies = t.get("applies")
        if not isinstance(applies, dict):
            raise CoverLayoutTableError(f"COVER_LAYOUT_TABLE_APPLIES_INVALID:{tid}")
        people = applies.get("people")
        if not isinstance(people, list) or not people or any(p not in PEOPLE for p in people):
            raise CoverLayoutTableError(f"COVER_LAYOUT_TABLE_APPLIES_INVALID:{tid}")
        if any(n not in NEEDS for n in applies.get("needs") or []):
            raise CoverLayoutTableError(f"COVER_LAYOUT_TABLE_APPLIES_INVALID:{tid}")
        for key in ("max_punch_chars", "max_first_chars", "lines"):
            if key in applies and (isinstance(applies[key], bool) or not isinstance(applies[key], int) or applies[key] < 1):
                raise CoverLayoutTableError(f"COVER_LAYOUT_TABLE_APPLIES_INVALID:{tid}")
        if "routes" in applies and (not isinstance(applies["routes"], list) or not applies["routes"]):
            raise CoverLayoutTableError(f"COVER_LAYOUT_TABLE_APPLIES_INVALID:{tid}")
        if "no_guest_lines" in applies and not isinstance(applies["no_guest_lines"], bool):
            raise CoverLayoutTableError(f"COVER_LAYOUT_TABLE_APPLIES_INVALID:{tid}")
        if "wordy_punch_floor_px" in t and not (
                _num(t["wordy_punch_floor_px"], 120, table["punch_floor_px"]) and isinstance(t["wordy_punch_floor_px"], int)
                and isinstance(t.get("wordy_punch_floor_people_min"), int) and t["wordy_punch_floor_people_min"] >= 2):
            raise CoverLayoutTableError(f"COVER_LAYOUT_TABLE_WORDY_FLOOR_INVALID:{tid}")
    return table


def punch_floor(table: Mapping, template: Mapping, facts: Mapping) -> int:
    """The punchline size this template must reach: the table's ``punch_floor_px``, or the template's
    ``wordy_punch_floor_px`` for a cover of at least ``wordy_punch_floor_people_min`` people whose words step 6 could not
    write short (``facts["wordy"]``; the table's ``short_punch_chars``)."""
    if (facts.get("wordy") and "wordy_punch_floor_px" in template
            and int(facts.get("people_count") or 1) >= int(template["wordy_punch_floor_people_min"])):
        return int(template["wordy_punch_floor_px"])
    return int(table["punch_floor_px"])


def load_table(path: Path | str) -> dict:
    """Read and validate the table. Missing or malformed input raises; there is no built-in fallback."""

    path = Path(path)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise CoverLayoutTableError(f"COVER_LAYOUT_TABLE_UNREADABLE: {path}") from exc
    try:
        table = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CoverLayoutTableError("COVER_LAYOUT_TABLE_JSON_INVALID") from exc
    return {**validate_table(table), "table_sha256": hashlib.sha256(raw).hexdigest()}


def not_applicable(template: Mapping, facts: Mapping) -> str | None:
    """Why this template cannot be made for a cover with these facts, or None when it can.

    ``facts``: ``people`` (solo | together | panels: she alone; others in the same picture; a split screen of the
    collaboration's people), ``lines`` (the cover lines, the last is the punchline), ``column_rows`` (the lines broken
    into a column's rows exist), ``route`` (optional: the candidate's route), ``fans_emote``, ``watching`` (bools);
    ``people_count`` and ``wordy`` only move a template's punch floor (``punch_floor``)."""
    applies = template["applies"]
    if facts["people"] not in applies["people"]:
        return f"人物：{facts['people']} 不在 {'/'.join(applies['people'])} 里"
    route = facts.get("route")
    if route and "routes" in applies and route not in applies["routes"]:
        return f"做法：{route} 做不了这个构图"
    if applies.get("no_guest_lines") and facts.get("guest_line"):
        return "嘉宾头像离文字区太远，嘉宾原话或说话人不明时无法确认文字贴着本人"
    if facts.get("fans_emote") and "fans_emote" not in (applies.get("needs") or []):
        return "粉丝贴纸占着主表情包的另一侧，只有表情包拼贴放得下"
    for need in applies.get("needs") or []:
        if not facts.get(need):
            return {"column_rows": "封面字没有拆成竖列的短行", "fans_emote": "没有粉丝贴纸", "watching": "不是看别人内容的片"}[need]
    lines = list(facts["lines"])
    if "lines" in applies and len(lines) != applies["lines"]:
        return f"字数：要 {applies['lines']} 行，这张 {len(lines)} 行"
    if "max_punch_chars" in applies and lines and len(lines[-1]) > applies["max_punch_chars"]:
        return f"字数：笑点行 {len(lines[-1])} 字，超过 {applies['max_punch_chars']}"
    if "max_first_chars" in applies and len(lines) > 1 and len(lines[0]) > applies["max_first_chars"]:
        return f"字数：第一行 {len(lines[0])} 字，超过 {applies['max_first_chars']}"
    return None


def order(table: Mapping, facts: Mapping, history: Sequence[str | None] = (), session_history: Sequence[str | None] = (),
          content_sha256: str = "0" * 16) -> tuple[list[dict], list[dict], list[dict]]:
    """(fresh, recent, skipped): the applicable templates in the order they are tried, the applicable ones the recent
    covers used (tried last, least recently used first), and the others with why.  ``history`` is every earlier cover's
    template id, oldest first (None or an unknown id for a cover without one); ``session_history`` the same for this
    stream's covers."""
    templates = list(table["templates"])
    known = {t["template_id"] for t in templates}
    family = {t["template_id"]: t.get("family", t["template_id"]) for t in templates}
    hist = [h for h in history if h in known]
    sess = [h for h in session_history if h in known]
    n, m = int(table["recent_covers"]), int(table["session_recent_covers"])
    window = {family[h]: "recent_used" for h in (hist[-n:] if n else [])}
    for h in sess[-m:] if m else []:
        window.setdefault(family[h], "session_used")
    last = {family[h]: i for i, h in enumerate(hist)}
    base = int(content_sha256[:16], 16) % len(templates) if len(content_sha256) >= 16 else 0
    rank = {t["template_id"]: (i - base) % len(templates) for i, t in enumerate(templates)}
    fresh, recent, skipped = [], [], []
    for t in templates:
        why = not_applicable(t, facts)
        if why:
            skipped.append({"template_id": t["template_id"], "reason": "not_applicable: " + why})
        elif family[t["template_id"]] in window:
            recent.append(t)
        else:
            fresh.append(t)
    fresh.sort(key=lambda t: (last.get(family[t["template_id"]], -1), rank[t["template_id"]]))
    recent.sort(key=lambda t: (last.get(family[t["template_id"]], -1), rank[t["template_id"]]))
    skipped += [{"template_id": t["template_id"], "reason": window[family[t["template_id"]]]} for t in recent]
    return fresh, recent, skipped


def select(table: Mapping, facts: Mapping, *, attempt: Callable[[Mapping], Mapping], history: Sequence[str | None] = (),
           session_history: Sequence[str | None] = (), content_sha256: str = "0" * 16) -> tuple[dict, dict]:
    """Return ``(attempt_result, selection)`` for the first template that fits.

    ``attempt(template)`` lays the cover out with that template and returns a mapping with ``punch`` (the punchline's
    size in px); it raises ``TemplateRefused``, ``ValueError`` or ``SystemExit`` when the template cannot be made (the
    text does not fit, it would cover a face, the frame has no such window ...).  A result under ``punch_floor_px`` is
    kept aside and the next template is tried (a template never shrinks the words; the floor is ``punch_floor``'s); when
    none reaches its floor, the one with the largest punchline is taken (``punch_below_floor``).  For pack
    collaborations (``people == "panels"``), if no non-adjacent template reaches its floor, the largest drawn
    non-adjacent result at least ``PACK_PUNCH_FALLBACK_RATIO`` of its floor is taken first (``punch_below_floor``),
    otherwise the immediately prior family at or above its floor, least recently used variant first
    (``adjacent_repeat_forced``), and otherwise the immediately prior family at least ``PACK_PUNCH_FALLBACK_RATIO`` of
    its floor, least recently used variant first (``adjacent_repeat_forced+punch_below_floor``).  The returned
    selection records ``punch_px`` and ``punch_floor_px`` and omits the chosen template from ``skipped``.  Raises
    ``TemplateRefused`` when no template can be made."""
    fresh, recent, skipped = order(table, facts, history, session_history, content_sha256)
    skipped = list(skipped)
    below: list[tuple[int, int, dict, Mapping, str | None]] = []
    adjacent: list[dict] = []
    by_id = {t["template_id"]: t for t in table["templates"]}
    # For pack collaborations a different position of the same split-screen is still the same composition.
    # Never take the immediate prior family, including the last cover of this stream if another stream intervened.
    forbidden = {by_id[h].get("family", h) for h in [*history[-1:], *session_history[-1:]] if h in by_id} if facts["people"] == "panels" else set()

    def finish(template: Mapping, result: Mapping, fallback: str | None, *, punch: int, floor: int) -> tuple[dict, dict]:
        tid = template["template_id"]
        selection = {"template_id": tid, "text": template["text"], "rows": template["rows"],
                     "framing": dict(template["framing"]), "table_sha256": table.get("table_sha256"),
                     "facts": {k: facts[k] for k in sorted(facts) if k != "lines"},
                     "history": [h for h in history][-max(int(table["recent_covers"]), 1):],
                     "session_history": [h for h in session_history][-max(int(table["session_recent_covers"]), 1):],
                     "punch_px": punch, "punch_floor_px": floor,
                     "skipped": [s for s in skipped if s["template_id"] != tid], "fallback": fallback}
        return {**dict(result), "template": dict(template)}, selection

    for group, forced in ((fresh, None), (recent, "recent_repeat_forced")):
        for template in group:
            if template.get("family", template["template_id"]) in forbidden:
                skipped.append({"template_id": template["template_id"], "reason": "adjacent_family_forbidden"})
                adjacent.append(template)
                continue
            try:
                result = attempt(template)
            except (TemplateRefused, ValueError, SystemExit) as exc:
                skipped.append({"template_id": template["template_id"], "reason": "does_not_fit: " + str(exc)[:160]})
                continue
            punch, floor = int(result["punch"]), punch_floor(table, template, facts)
            if punch < floor:
                skipped.append({"template_id": template["template_id"], "reason": f"punch_below_floor: {punch}px < {floor}px"})
                below.append((punch, floor, dict(template), result, forced))
                continue
            return finish(template, result, forced, punch=punch, floor=floor)
    usable = ([b for b in below if b[0] >= b[1] * PACK_PUNCH_FALLBACK_RATIO]
              if facts["people"] == "panels" else below)
    if usable:
        punch, floor, template, result, forced = max(usable, key=lambda b: (b[0], b[4] is None))
        return finish(template, result, "punch_below_floor" + (f"+{forced}" if forced else ""), punch=punch, floor=floor)
    if adjacent:
        last_used = {h: i for i, h in enumerate(h for h in history if h in by_id)}
        last_sess = {h: i for i, h in enumerate(h for h in session_history if h in by_id)}
        adjacent_below: list[tuple[int, int, dict, Mapping]] = []
        for template in sorted(adjacent, key=lambda t: (last_used.get(t["template_id"], -1), last_sess.get(t["template_id"], -1))):
            try:
                result = attempt(template)
            except (TemplateRefused, ValueError, SystemExit) as exc:
                skipped.append({"template_id": template["template_id"], "reason": "does_not_fit: " + str(exc)[:160]})
                continue
            punch, floor = int(result["punch"]), punch_floor(table, template, facts)
            if punch < floor:
                skipped.append({"template_id": template["template_id"], "reason": f"punch_below_floor: {punch}px < {floor}px"})
                if punch >= floor * PACK_PUNCH_FALLBACK_RATIO:
                    adjacent_below.append((punch, floor, dict(template), result))
                continue
            return finish(template, result, "adjacent_repeat_forced", punch=punch, floor=floor)
        if adjacent_below:
            punch, floor, template, result = adjacent_below[0]
            return finish(template, result, "adjacent_repeat_forced+punch_below_floor", punch=punch, floor=floor)
    raise TemplateRefused("NO_TEMPLATE_FITS: " + "; ".join(f"{s['template_id']} {s['reason']}" for s in skipped))


def window(face: Sequence[int], head_top: int | None, keep: Sequence[Sequence[int]], framing: Mapping, *,
           src_w: int = FRAME_W, src_h: int = FRAME_H, chin_margin_frac: float = 0.12, floor_frac: float = 0.25,
           pad_side: str | None = None) -> tuple[dict | None, str | None]:
    """({crop, face, face_px, target_px, limited_by, pad}, None) for the window [x0, y0, width] of a live frame (16:9
    output) that frames her face as the template says, or (None, why).

    The face gets ``face_frac`` of the cover's height unless her head and chin (and the ``keep`` boxes) need a wider window
    (never one that cuts the chin or puts the face under the veto floor).  ``framing["cut_head"]`` lets the window cut the
    top of her head (hair, ears) for a close-up; the forehead stays.  The face centre goes to ``face_x_frac`` of the width
    and the face top to ``face_top_frac`` of the height, as far as the frame allows (``limited_by`` FRAME_EDGE).
    ``face_x_frac`` / ``face_top_frac`` None: centred / the head at the window's top.

    ``pad_side`` (left | right: the side the words go): the window may reach past the frame's edge on that side, so a
    face in the middle of the frame can still stand beside the words; ``pad`` then says how many px of ground the frame
    is extended by on that side (``crop`` and ``face`` are in the extended frame's pixels, which starts at the pad when the
    pad is on the left)."""
    fh = face[3] - face[1]
    if fh <= 0:
        return None, "FACE_EMPTY"
    if head_top is None and not framing.get("cut_head"):
        return None, "HEAD_TOP_UNMEASURED"
    top_needed = face[1] - round(0.25 * fh) if framing.get("cut_head") else head_top
    margin = round(chin_margin_frac * fh)
    top = max(0, min([top_needed, *[b[1] for b in keep]]))
    bottom = min(src_h, max([face[3] + margin, *[b[3] for b in keep]]))
    target = round(framing["face_frac"] * FRAME_H)
    w_tall = math.ceil((bottom - top) * FRAME_W / FRAME_H)
    w_floor = FRAME_W * fh / (floor_frac * FRAME_H)
    w = min(src_w, round(src_h * FRAME_W / FRAME_H), max(math.floor(FRAME_W * fh / target), w_tall))
    if w > w_floor:
        return None, "FACE_UNDER_FLOOR"
    h = round(w * FRAME_H / FRAME_W)
    fx = framing.get("face_x_frac")
    wish_x = round((face[0] + face[2]) / 2 - (0.5 if fx is None else fx) * w)
    wish_x = min(max(wish_x, face[2] - w), face[0])     # a wide face is moved in rather than cut by the cover's edge
    lo, hi = 0, src_w - w
    if pad_side == "left":          # the window may start left of the frame, as long as her face stays in it
        lo = min(0, max(face[2] - w, wish_x))
    elif pad_side == "right":
        hi = max(src_w - w, min(face[0], wish_x))
    x0 = min(max(wish_x, lo), hi)
    ft = framing.get("face_top_frac")
    wish_y = top if ft is None else round(face[1] - ft * h)
    y0 = min(max(wish_y, 0), src_h - h)
    y0 = min(y0, top)                       # the head (or forehead) stays in
    y0 = max(y0, bottom - h)                # the chin stays in
    if y0 < 0 or y0 + h > src_h or y0 + h < bottom or y0 > top:
        return None, "WINDOW_CUTS_HEAD_OR_CHIN"
    if any(b[0] < x0 or b[2] > x0 + w for b in keep):
        return None, "KEEPS_OUTSIDE"
    pad = {"side": "left", "px": -x0} if x0 < 0 else {"side": "right", "px": x0 + w - src_w} if x0 + w > src_w else None
    shift = pad["px"] if pad and pad["side"] == "left" else 0
    scale = FRAME_W / w
    out = [round((face[0] - x0) * scale), round((face[1] - y0) * scale), round((face[2] - x0) * scale), round((face[3] - y0) * scale)]
    limited = ("FRAME_EDGE" if x0 != wish_x or y0 != wish_y else
               None if out[3] - out[1] >= target - 1 else "HEAD_AND_CHIN")
    return {"crop": [x0 + shift, y0, w], "face": out, "face_px": out[3] - out[1], "target_px": target, "limited_by": limited,
            "pad": pad}, None


def describe(selection: Mapping) -> str:
    """One PROCESS / report line: the template taken and why the ones before it were passed over."""
    tid = selection["template_id"]
    skipped = "；".join(
        f"{s['template_id']}（{s['reason']}）"
        for s in selection.get("skipped") or []
        if s.get("template_id") != tid
    )
    tail = f"；顺延：{skipped}" if skipped else ""
    fallback_text = str(selection.get("fallback") or "")
    punch, floor = selection.get("punch_px"), selection.get("punch_floor_px")
    if fallback_text and punch is not None and floor is not None and punch < floor:
        fallback_text = f"{fallback_text}: {punch}px < {floor}px"
    fallback = f"（{fallback_text}）" if fallback_text else ""
    return f"构图样板 {tid}{fallback}{tail}"
