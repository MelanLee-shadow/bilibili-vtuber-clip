"""Which composition template each cover candidate is made in (step 6, 70-cover; cover_candidates.py on the media host).

Owner 2026-10-31, 2026-11-13, 2026-11-25: the covers all looked the same (one line top left, one line lower right, the
same picture).  The channel's table (job "layouts": cover_layout_templates.json) lists the compositions; the shared
selector cover_layout_rotation (uploaded beside this tool; the skill renderer uses the same module) orders the ones that
apply to a route's candidate: the templates the last covers and this stream's last covers used (job "template_history",
"template_session_history", the real covers of the task, step6_cover._lane_covers) come last.  `compose` makes the
candidate in each template in that order (the route's own method builds the spec for `run.column.template`), renders it
without the judge, and passes over a template the renderer refuses, whose punchline stays under the floor or whose frame
has no such window, with its reason.  The taken candidate is measured and judged; its row carries the selection
("cover_layout") and the PROCESS line ("cover_layout_note").
"""
import importlib.util
import os

try:
    import cover_layout_rotation as layouts           # uploaded beside this tool (step6_cover._render_cover)
except ModuleNotFoundError:                          # run from the repository: the shared module itself
    _path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "src", "autoslice",
                         "cover_layout_rotation.py")
    _spec = importlib.util.spec_from_file_location("cover_layout_rotation", _path)
    layouts = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(layouts)

# failures that do not depend on the template: trying the next one would only repeat them (and a generation is not asked
# again: cover_candidates.generate reuses an unchanged request)
ROUTE_FAILURES = ("IMAGE_EDIT_", "HOST_FACE_NOT_FOUND", "HOST_FACE_NOT_IN_FRAME", "HOST_FACE_NOT_ON_HER_FIGURE",
                  "STICKER_HOST_FACE_UNAVAILABLE", "PEOPLE_LAYERS_UNAVAILABLE", "PACK_FRAME_CONTAINS_OTHER_PERSON",
                  "PACK_FRAME_GUESTS_UNCHECKED", "GUESTS_DO_NOT_FIT:PORTRAIT_UNUSABLE")
# ... unless the failure under that prefix is the template's own (the cut-out reports its placement under
# PEOPLE_LAYERS_UNAVAILABLE: a template that leaves her picture's bottom cut inside the canvas, cover_layout.place_solo)
TEMPLATE_FAILURES = ("FIGURE_CUT_ENDS_ABOVE_FRAME_BOTTOM",)


class RouteFailed(Exception):
    """The route cannot be made in any template (not a ValueError: the selector does not pass it over)."""


def load(job):
    return layouts.load_table(job["layouts"])


def facts(run, route):
    """What the templates are matched against for this route's candidate (cover_layout_rotation.not_applicable)."""
    return {"people": "panels" if run.pack else "together" if run.guest_boxes else "solo",
            "people_count": 1 + len(run.pack or run.guest_boxes or []), "wordy": bool(run.job.get("wordy")),
            "lines": [ln["text"] for ln in run.lines], "column_rows": len(run.column.rows) >= 2, "route": route,
            "fans_emote": route == "emote" and bool(run.job.get("fans_emote")), "watching": False,
            # A rewritten or otherwise unattributed line may belong to a guest.  Do not place it over a distant host
            # just because the subtitle reader could not identify its speaker.
            "guest_line": bool(run.pack and (len(run.speakers) != len(run.lines)
                                              or any(s != run.host_name for s in run.speakers)))}


def compose(run, route, make):
    """Make `route`'s candidate in the first template that fits; append its judged row to run.rows, or record why
    none could be made (a renderer refusal in every template keeps status RENDER_FAILED: the caller shortens the words)."""
    taken = list(run.job.get("background_taken") or [])
    tried = []

    def attempt(template):
        run.column.template = template
        run.job["background_taken"] = list(taken)      # each try starts from the colours the earlier routes took
        if route == "cutout":                          # the cut-out's layers are made again (the sticker reads them)
            run.group_failed, run.group_failure = False, None
        tried.append(template["template_id"])
        n_specs, n_failed = len(run.specs), len(run.failed)
        make()
        if len(run.specs) > n_specs:
            item = run.specs.pop()
            row = run.draw(*item)
            if row.get("status") != "RENDERED":
                raise layouts.TemplateRefused(row.get("failure") or "RENDER_FAILED")
            return {"punch": row["punch_font_size"], "row": row, "item": item,
                    "background_taken": list(run.job.get("background_taken") or [])}
        failure = run.failed.pop()["failure"] if len(run.failed) > n_failed else "NO_CANDIDATE"
        if failure.startswith(ROUTE_FAILURES) and not any(t in failure for t in TEMPLATE_FAILURES):
            raise RouteFailed(failure)
        raise layouts.TemplateRefused(failure)

    known = facts(run, route)
    try:
        result, selection = layouts.select(
            run.table, known, attempt=attempt, history=run.job.get("template_history") or (),
            session_history=run.job.get("template_session_history") or (),
            content_sha256=run.job.get("content_sha256") or "0" * 16)
    except (RouteFailed, layouts.TemplateRefused) as exc:
        run.column.template = None
        run.job["background_taken"] = taken
        run.layouts[route] = {"failure": str(exc), "tried": tried}
        run.fail(route, str(exc))
        if isinstance(exc, layouts.TemplateRefused) and "RENDER_REFUSED" in str(exc):
            run.failed[-1]["status"] = "RENDER_FAILED"
        return
    template = result["template"]
    if tried[-1] != template["template_id"]:    # an earlier try was taken: made again (its files are current)
        result = attempt(template)
    run.column.template = None
    run.job["background_taken"] = result["background_taken"]
    selection["tried"] = tried
    run.layouts[route] = selection
    row = result["row"]
    row["punch_floor_px"] = layouts.punch_floor(run.table, template, known)   # this template's floor
    row["punch_below_floor"] = row["punch_font_size"] < row["punch_floor_px"]
    row["cover_layout"] = selection
    row["cover_layout_note"] = layouts.describe(selection)
    run.rows.append(run.judge_row(row, *result["item"]))
