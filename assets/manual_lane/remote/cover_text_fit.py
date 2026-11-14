"""Cover text sizing before the render (step 6, 70-cover): how big the two lines can be given where the face sits.

The skill renderer (render_cover.py) lays the lines of a `top` cover into the strip from y=30 down to the face top minus
10, giving line 1 at most 42 % and the punchline (last line) at most 58 % of that strip, each as painted ink (stroke and tilt
included); the feed-safe zone is 1400 px wide.  Nothing here changes the renderer: the numbers it uses are mirrored in
lane_rules.json `cover` (with their source) so the lane can compute, before rendering, (a) the face top a punchline of
the floor size needs, and (b) the size line 1 can have without leaving its share.  The renderer's own rule stays: line 1
is at most the style's size_ratio of the punchline.

Pure functions over an `ink(index, size) -> (width, height)` callable, so tests use a fake and the tool passes the real
renderer's painted ink for the style the rotation will pick.  No image library is imported here.
"""
import math


def renderer_ink(renderer, font, lines, style):
    """ink(i, size): the painted width and height of line i at `size` in `style` (stroke, tilt, colour), measured by the
    renderer's own line_layer, rotated as the renderer rotates it."""
    from PIL import Image

    cache = {}

    def ink(i, size):
        key = (i, size)
        if key not in cache:
            colors = style["colors"]
            layer = renderer.line_layer(font, lines[i]["text"], size, colors[min(i, len(colors) - 1)], style["stroke"])
            box = layer.rotate(style["tilt"], resample=Image.BICUBIC, expand=True).getbbox()
            cache[key] = (box[2] - box[0], box[3] - box[1])
        return cache[key]
    return ink


def probe_style(renderer, content_sha256, recent_style_ids):
    """The style the renderer's rotation will try first for this content (same table, hash and recent covers)."""
    res, _ = renderer.select_style(renderer.TABLE, content_sha256, lambda style: {"style": style, "low_contrast": False, "overridden": []},
                                   recent_style_ids)
    return res["style"]


def _largest(ink, i, start, minimum, max_w, max_h):
    """The renderer's own fit: shrink by 6 from `start` until the ink fits (or the renderer's 60 px stop)."""
    size = start
    while True:
        w, h = ink(i, size)
        if (w <= max_w and h <= max_h) or size <= max(60, minimum - 1):
            return size, w <= max_w and h <= max_h
        size -= 6


def need_face_top(ink, n_lines, rules, goal_px):
    """The smallest face top (output y) that lets the last line reach `goal_px`: its ink height must fit its share of the
    strip above the face.  `goal_px` is rounded up to the renderer's own size grid (250, 244, ... 202, 196: a floor of 200
    is reached only by 202).  None when the line is too wide for the safe zone at that size (the face cannot help)."""
    last = n_lines - 1
    start = rules["punch_start_px"]                       # the renderer shrinks by 6 from its start: only these sizes occur
    goal_px = start - 6 * ((start - goal_px) // 6) if goal_px <= start else goal_px
    w, h = ink(last, goal_px)
    if w > rules["text_safe_width_px"]:
        return None
    share = rules["text_line_share"][-1] if n_lines == 2 else 1.0
    return rules["text_zone_top_px"] + rules["text_zone_gap_px"] + math.ceil(h / share)


def plan_text(ink, n_lines, face_top, style, rules):
    """Size the lines of a top-layout cover whose face begins at `face_top`.  Returns the plan: punch_px (what the
    renderer will reach), floor_met, first_px (None for one line), the strip, and `layout`: `top`, or `auto` when top
    cannot reach the floor so the renderer may try the other layouts.  Never raises: a floor that cannot be reached is
    reported as floor_met=False with the size that is reached."""
    if n_lines > 2:      # a column's rows (cover_layout.Column): the renderer sizes them beside the face, not in the strip
        return {"column": True, "floor_px": rules["punch_floor_px"], "first_px": None, "layout": None}
    last = n_lines - 1
    strip = face_top - rules["text_zone_top_px"] - rules["text_zone_gap_px"]
    shares = rules["text_line_share"] if n_lines == 2 else [1.0]
    safe = rules["text_safe_width_px"]
    punch, _ = _largest(ink, last, rules["punch_start_px"], 60, safe, strip * shares[-1])
    plan = {"strip_px": strip, "punch_px": punch, "floor_px": rules["punch_floor_px"], "first_px": None,
            "floor_met": punch >= rules["punch_floor_px"]}
    plan["layout"] = "top" if plan["floor_met"] else "auto"
    if n_lines == 2:
        room = min(strip * shares[0], strip - ink(last, punch)[1])      # line 1 keeps to what the punchline leaves
        start = min(rules["first_line_max_px"], punch)
        first, _ = _largest(ink, 0, start, rules["first_line_min_px"], safe - rules["text_stagger_px"], room)
        plan["first_px"] = max(rules["first_line_min_px"], min(first, int(punch * style["size_ratio"])))
    return plan


def apply_plan(spec, plan):
    """Pin line 1's computed size (a hand-written size is the author's and stays) and, when the top layout cannot give
    the punchline its floor, let the renderer choose the layout."""
    if plan["first_px"] is not None and not spec["lines"][0].get("size"):
        spec["lines"][0]["size"] = plan["first_px"]
    if spec.get("layout") == "top":
        spec["layout"] = plan["layout"]
    return spec
