"""Where a cover route's seconds go (step 6, 70-cover; cover_candidates.py on the media host).

Each route's wall time is split into named phases and written into its row (`phases_s`), so a slow route shows at a
glance whether it waited on a locate or a generate call (model), laid the picture out, measured the face, rendered, or
was judged.  The tool runs its routes one after another in one process, so one "current route" is enough; the phase
decorators add to whichever route is current.  No other module is imported.
"""
import contextlib
import functools
import time

CLOCK = {"route": None, "seconds": {}, "now": time.monotonic}
PHASES = ("locate", "generate", "layout", "measure", "render", "judge")


def route(name):
    """Make `name` the route the following phases are charged to."""
    CLOCK["route"] = name
    CLOCK["seconds"].setdefault(name, {})


@contextlib.contextmanager
def phase(name):
    started = CLOCK["now"]()
    try:
        yield
    finally:
        mine = CLOCK["seconds"].setdefault(CLOCK["route"] or "", {})
        mine[name] = round(mine.get(name, 0.0) + CLOCK["now"]() - started, 2)


def timed(name):
    """Decorator: the call's seconds are charged to phase `name` of the current route."""
    def wrap(fn):
        @functools.wraps(fn)
        def inner(*args, **kwargs):
            with phase(name):
                return fn(*args, **kwargs)
        return inner
    return wrap


def seconds(name):
    """The phases charged to route `name` so far (a copy)."""
    return dict(CLOCK["seconds"].get(name, {}))
