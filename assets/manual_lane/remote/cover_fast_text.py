"""Making the skill renderer's text painting fast, without changing what it paints (step 6, 70-cover).

`render_cover.line_layer` fills the holes of a stroked line with PIL's `ImageDraw.floodfill`, a Python loop over pixels
that takes about a second per line.  A run to the floor size asks for dozens of (line, size) pairs, and the renderer asks
for the same pair again for every layout it tries, so one cover could spend minutes in it (a real run: 380 s for a
refused cover).  Two changes, both exact:

- `fast_floodfill` fills the same region by connected runs of equal bytes instead of pixels (4-connected, as PIL's);
- `fast_renderer` makes `line_layer` remember each painted line (a copy is handed out; the cache is never drawn on).

Used only on the media host's copy of the renderer inside cover_candidates.py; the skill's file is not edited.
"""
import bisect
import re

from PIL import Image, ImageDraw


def _runs(data, w, h, seed):
    pattern = re.compile(re.escape(bytes([seed])) + b"+")
    return [[(m.start() - y * w, m.end() - y * w) for m in pattern.finditer(data, y * w, (y + 1) * w)] for y in range(h)]


def fast_floodfill(image, xy, value, border=None, thresh=0):
    """ImageDraw.floodfill for the case the renderer uses (mode L, no border, no threshold): the seed's connected region
    (4-connected) becomes `value`.  Any other call is PIL's own."""
    if image.mode != "L" or border is not None or thresh:
        return ImageDraw.floodfill(image, xy, value, border, thresh)
    w, h = image.size
    data = bytearray(image.tobytes())
    seed = data[xy[1] * w + xy[0]]
    if seed == value:
        return None
    rows = _runs(bytes(data), w, h, seed)
    starts = [[r[0] for r in row] for row in rows]
    x, y = xy
    first = bisect.bisect_right(starts[y], x) - 1
    seen, stack = {(y, first)}, [(y, first)]
    fill = bytes([value])
    while stack:
        y, i = stack.pop()
        s, e = rows[y][i]
        data[y * w + s:y * w + e] = fill * (e - s)
        for ny in (y - 1, y + 1):
            if not 0 <= ny < h:
                continue
            j = max(0, bisect.bisect_right(starts[ny], s) - 1)
            while j < len(rows[ny]) and rows[ny][j][0] < e:
                if rows[ny][j][1] > s and (ny, j) not in seen:
                    seen.add((ny, j))
                    stack.append((ny, j))
                j += 1
    image.paste(Image.frombytes("L", (w, h), bytes(data)))
    return None


class _FastDraw:
    """`ImageDraw` as the renderer sees it, with the fast flood fill."""

    def __getattr__(self, name):
        return getattr(ImageDraw, name)

    floodfill = staticmethod(fast_floodfill)


def fast_renderer(renderer):
    """The renderer with the fast flood fill and a remembered `line_layer`.  Idempotent."""
    if getattr(renderer.line_layer, "memoized", False):
        return renderer
    renderer.ImageDraw = _FastDraw()
    paint, cache = renderer.line_layer, {}

    def line_layer(*args):
        if args not in cache:
            cache[args] = paint(*args)
        return cache[args].copy()
    line_layer.memoized = True
    renderer.line_layer = line_layer
    return renderer
