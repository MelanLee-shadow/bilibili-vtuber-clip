"""Which pastel background a cover gets when it does not use the live frame (step 6, 70-cover: emote, cutout, ai_sticker).

The colours are a table, lane_rules.json `cover.backgrounds` (a gradient of two colours each, with a hue family and a
light or deep tone, and the basis of the choice), not one pair written into the routes.  For one candidate the table is
filtered and ordered, never drawn at random:

1. the person does not melt into it: the share of the layers' opaque pixels that are within `background_layer_near_distance`
   (RGB distance, the style table's own metric) of the background colour at their row stays under
   `background_layer_max_fused_frac` (a white-clothed host on a pale background is the case the approved batch changed);
2. the text reads: the fill colour of each line (the style table's bottom colour, as the renderer's own contrast check
   takes it) is at least the style table's `contrast.min_distance` from the background under that line, and the stroke
   ring that is farther from the background is at least `background_stroke_min_distance` from it;
3. among those, backgrounds neither used by the last covers nor by this session come first (else the one used longest
   ago);
4. the content hash starts a walk round the hue families still in play, shifted by the route's place in
   `background_route_order` (the routes run in separate processes and cannot see each other, so the shift is what keeps
   them apart; routes of one process also skip a family an earlier route took), and picks one entry of the first family
   by the same hash: the same input is always the same background.

When nothing passes, the entry that misses by least is used and the record says so (`tier`); nothing is hidden by a
default.  The record (`choose` returns it) is kept on the candidate row and in choose.json.
"""
from PIL import Image

SAMPLE_ROWS = 48     # a layer is read at this many rows (aspect kept): enough to count colours, cheap beside the render


def _rgb(value):
    return tuple(int(value[i:i + 2], 16) for i in (1, 3, 5))


def _distance(a, b):
    return sum((x - y) ** 2 for x, y in zip(a, b, strict=True)) ** 0.5


def _gradient(colors, y, height):
    """The renderer's pastel(): the first colour at the top, the second at the bottom, linear in y."""
    t = min(1.0, max(0.0, y / height))
    return tuple(colors[0][i] + (colors[1][i] - colors[0][i]) * t for i in range(3))


def layer_samples(renderer, stickers):
    """[(r, g, b, canvas y, weight)] for the opaque pixels of the sticker layers as the renderer will paste them: its
    own cutout, crop, bounding box and height, read at SAMPLE_ROWS rows; weight is the canvas area one sample stands for."""
    out = []
    for st in stickers:
        with Image.open(st["src"]) as src:
            im = renderer.cutout(src, st.get("cutout", "dark"))
        if st.get("crop"):
            im = im.crop(tuple(st["crop"]))
        box = im.getchannel("A").getbbox()
        if not box:
            continue
        im = im.crop(box)
        small = im.resize((max(1, round(im.width * SAMPLE_ROWS / im.height)), SAMPLE_ROWS), Image.BOX)
        step = int(st["h"]) / SAMPLE_ROWS
        pixels = small.load()
        for row in range(SAMPLE_ROWS):
            for col in range(small.width):
                r, g, b, a = pixels[col, row]
                if a >= 128:
                    out.append((r, g, b, st["y"] + (row + 0.5) * step, step * step))
    return out


def _text_distances(renderer, style, colors, n_lines, strip, rules):
    """(smallest fill distance, smallest stroke distance) over the lines: each line's share of the text strip, the
    gradient's colour at its middle (a linear gradient's mean over a span)."""
    shares = rules["text_line_share"] if n_lines == 2 else [1.0]
    recipe = renderer.STROKES[style["stroke"]]
    rings = [tuple(recipe[k]) for k in ("inner", "outer") if recipe[k] is not None]
    fills, strokes, y = [], [], strip[0]
    for index, share in enumerate(shares):
        top, y = y, y + share * (strip[1] - strip[0])
        under = _gradient(colors, (top + y) / 2, renderer.H)
        fills.append(_distance(renderer.PAL[style["colors"][index]][1], under))
        strokes.append(max(_distance(ring, under) for ring in rings))
    return min(fills), min(strokes)


def _layer_figures(samples, colors, near, height):
    """(share of the layer pixels within `near` of the background at their row, weighted mean colour's distance from the
    background at the weighted mean row); (0, None) when there is no layer."""
    total = sum(s[4] for s in samples)
    if not total:
        return 0.0, None
    fused = sum(s[4] for s in samples if _distance(s[:3], _gradient(colors, s[3], height)) < near)
    mean = tuple(sum(s[i] * s[4] for s in samples) / total for i in range(3))
    mean_y = sum(s[3] * s[4] for s in samples) / total
    return fused / total, _distance(mean, _gradient(colors, mean_y, height))


def evaluate(renderer, entry, samples, style, n_lines, strip, rules):
    """One table entry against this candidate: the measured numbers, whether each condition holds, and `miss` (how far
    the conditions are missed, 0 when all hold; only used to rank when none does)."""
    colors = [_rgb(c) for c in entry["colors"]]
    text, stroke = _text_distances(renderer, style, colors, n_lines, strip, rules)
    fused, mean = _layer_figures(samples, colors, rules["background_layer_near_distance"], renderer.H)
    need_text, need_stroke = renderer.MIN_CONTRAST, rules["background_stroke_min_distance"]
    failed = [name for name, bad in (("TEXT_FILL_LOW_CONTRAST", text < need_text), ("TEXT_STROKE_LOW_CONTRAST", stroke < need_stroke),
                                     ("PERSON_MELTS_INTO_BACKGROUND", fused > rules["background_layer_max_fused_frac"])) if bad]
    miss = (max(0, need_text - text) / need_text + max(0, need_stroke - stroke) / need_stroke
            + max(0, fused - rules["background_layer_max_fused_frac"]))
    return {"id": entry["id"], "text_distance": round(text, 1), "stroke_distance": round(stroke, 1),
            "layer_fused_frac": round(fused, 3), "layer_mean_distance": None if mean is None else round(mean, 1),
            "ok": not failed, "failed": failed, "miss": round(miss, 3)}


def _hue_walk(pool, sha, shift):
    """The hue families of `pool` (table order) rotated to start where the content hash and the route's shift say."""
    ring = list(dict.fromkeys(e["hue"] for e in pool))
    start = (int(sha[16:32], 16) + shift) % len(ring)
    return ring[start:] + ring[:start]


def _select(passing, rank, sha, shift, taken):
    """(entry, rank level, hue walk).  The least recently used level that still has a hue family no other route of this
    process took; when no level has, the first level and the walk's first family (the families are shared).  The entry of
    the family is the content hash's."""
    levels = sorted(set(rank.values()))
    for level in levels:
        pool = [e for e in passing if rank[e["id"]] == level]
        walk = _hue_walk(pool, sha, shift)
        if free := [h for h in walk if h not in taken]:
            break
    else:
        level = levels[0]
        pool = [e for e in passing if rank[e["id"]] == level]
        walk = free = _hue_walk(pool, sha, shift)
    group = [e for e in pool if e["hue"] == free[0]]
    return group[int(sha[:16], 16) % len(group)], level, walk


def choose(renderer, spec, style, route, job):
    """Set spec["bg"] and return the record of the choice (None for a spec that uses the live frame: no background to
    choose).  `style` is the text style the renderer will try first for this content.  The hue families the routes of
    this process already took are kept on the job (`background_taken`), so the routes of one process keep apart too."""
    if "frame" in spec:
        return None
    taken = job.setdefault("background_taken", [])
    rules, table = job["rules"], job["rules"]["backgrounds"]
    sha = job.get("content_sha256") or renderer.default_content_sha256(spec)
    strip = (rules["text_zone_top_px"], max(rules["text_zone_top_px"] + 1, spec["face"][1] - rules["text_zone_gap_px"]))
    samples = layer_samples(renderer, spec.get("stickers") or [])
    rows = {e["id"]: evaluate(renderer, e, samples, style, len(spec["lines"]), strip, rules) for e in table}
    passing = [e for e in table if rows[e["id"]]["ok"]]
    tier = "passes_all"
    if not passing:
        tier, passing = "none_passes_least_missed", [min(table, key=lambda e: rows[e["id"]]["miss"])]
    recent, session = list(job.get("recent_background_ids") or ()), set(job.get("session_background_ids") or ())
    # level 0: neither among the last covers' nor this session's; else 1 + the place of its last use (oldest use first)
    rank = {e["id"]: 0 if e["id"] not in recent and e["id"] not in session
            else 1 + max((i for i, r in enumerate(recent) if r == e["id"]), default=-1) + 1 for e in passing}
    shift = rules["background_route_order"].index(route) if route in rules["background_route_order"] else 0
    picked, level, walk = _select(passing, rank, sha, shift, taken)
    if level and tier == "passes_all":
        tier = "passes_all_but_recently_used"
    taken.append(picked["hue"])
    spec["bg"] = list(picked["colors"])
    return {"id": picked["id"], "colors": list(picked["colors"]), "hue": picked["hue"], "tone": picked["tone"], "tier": tier,
            "style_id": style["style_id"], "route": route, "hue_walk": walk, "route_shift": shift,
            "hues_taken_by_other_routes": list(taken[:-1]), "recently_used": recent, "session_used": sorted(session),
            "chosen": rows[picked["id"]], "considered": list(rows.values()), "basis": picked.get("basis")}
