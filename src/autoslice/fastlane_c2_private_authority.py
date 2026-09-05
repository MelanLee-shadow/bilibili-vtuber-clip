"""OSS stub: private C2 authority is not redistributed."""
from __future__ import annotations


_PREFIX = "C2_PRIVATE_REPLAY_PATH_UNAVAILABLE_"
_REGULAR_ROLES = {"PROVENANCE": "REGULAR_PROVENANCE_PARENT"}


def classify_c2_private_path_unavailable(exc: BaseException) -> str:
    """Classify the generic replay path failure without exposing its path."""

    if not isinstance(exc, Exception) or str(exc) != "REPLAY_PATH_UNAVAILABLE":
        return _PREFIX + "CALL_LOCUS_UNCLASSIFIED"
    frames = []
    trace = exc.__traceback__
    while trace is not None:
        frames.append(trace.tb_frame)
        trace = trace.tb_next
    for index, frame in enumerate(frames):
        if (
            frame.f_code.co_name != "_safe_directory"
            or not frame.f_code.co_filename.endswith("src/autoslice/reviewed_baseline_replay.py")
            or index == 0
        ):
            continue
        caller = frames[index - 1]
        if caller.f_code.co_name == "regular_binding":
            return _PREFIX + _REGULAR_ROLES.get(
                caller.f_locals.get("label"), "REGULAR_BINDING_UNCLASSIFIED"
            )
    return _PREFIX + "CALL_LOCUS_UNCLASSIFIED"
