"""Step 3, the subtitle agent's `frame` tool: the picture at a moment of the cut.

A sentence often names something the picture shows (她头上的大墨镜, the plush in her hands, a word on the screen she
reads out).  The recognisers only hear it; the reviewer settles it by looking.  The agent asks
{"tool": "frame", "t": 51.5, "question": "她头上戴着什么"} (or "times": up to MAX_FRAMES moments) and gets, for
every moment, what the vision route saw: `seen` false means the picture does not show it, which is no evidence either
way.  The reading is step4_other_voices.read_frames with this tool's remote reader (look_at_frames.py, the cover
step's vision client); the same question at the same moments is read once and reused.  Its paid calls go to `pay`
and stand in the tool result (`cpa`), so a resumed conversation counts them again (`paid`).
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any

from . import step4_other_voices as ov
from .runner import Ctx
from .step_io import REMOTE_TOOLS

TOOL = "frame"
SCRIPT = REMOTE_TOOLS / "look_at_frames.py"
PURPOSE = "subtitle-frame"
MAX_FRAMES = 3
MAX_QUESTION = 200
NOTE = "画面读数是视觉模型看图后的描述，也会看错：只是证据。seen 为 false 表示画面上看不出，不算正反证据；与各来源的音对不上就不套。"


def _times(req: dict[str, Any], cut_len: float) -> list[float]:
    raw = req.get("times", [req.get("t")] if "t" in req else None)
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_FRAMES:
        raise ValueError(f"frame needs t, or times with 1–{MAX_FRAMES} moments")
    if any(type(t) not in (int, float) or not 0 <= t <= cut_len for t in raw):
        raise ValueError(f"frame times must lie in the cut (0–{cut_len:.1f} s)")
    return [round(float(t), 2) for t in raw]


def look(ctx: Ctx, req: dict[str, Any], cut_len: float, tag: str, pay: Callable[[dict[str, Any]], object]) -> dict[str, Any]:
    question = req.get("question")
    if not isinstance(question, str) or not question.strip() or len(question) > MAX_QUESTION:
        raise ValueError(f"frame needs a question about the picture (at most {MAX_QUESTION} characters)")
    times = _times(req, cut_len)
    key = hashlib.sha256(json.dumps([question.strip(), times], ensure_ascii=False).encode("utf-8")).hexdigest()[:12]
    got = ov.read_frames(ctx, f"look{tag}-{key}", [{"id": f"f{i}", "t": t} for i, t in enumerate(times)],
                         script=SCRIPT, question=question.strip(), purpose=PURPOSE)
    for call in got.calls:
        pay(dict(call))
    spent = {"cpa": list(got.calls)} if got.calls else {}
    if got.error:       # unknown, not absent; what it cost is still counted
        return {"question": question.strip(), "error": f"读取失败，结果未知（不是没有）：{got.error}"[:400], **spent}
    frames = [{"t": f["t"], **({"seen": f.get("seen"), "answer": f.get("answer")} if f["ok"] else {"error": f.get("error")})}
              for f in got.frames]
    return {"question": question.strip(), "frames": frames, "note": NOTE, **spent}


def paid(request: object, result: object) -> list[dict[str, Any]]:
    """The paid calls a recorded `frame` tool result made (for a resumed conversation's count)."""
    if not (isinstance(request, dict) and request.get("tool") == TOOL and isinstance(result, dict)):
        return []
    return [r for r in result.get("cpa") or [] if isinstance(r, dict)]
