"""What the channel is: the host's basic facts (assets/<profile>/persona.md), read in one place.

Every model call that judges her words reads the same facts: what the subtitles say (step 3: how she names herself,
her catchphrases, how she calls her audience), who is speaking (step 4), what the clip is about and how to title it
(step 6).  Owner one capability, one interface, used wherever the pipeline needs it.  The file is rendered
from the fact ledger (persona_facts), so a fact accepted later reaches every reader.
"""
from __future__ import annotations

from pathlib import Path

PERSONA = Path(__file__).resolve().parents[3] / "assets" / "lidousha" / "persona.md"
COVER_SECTION = "## 封面表情/角色映射"
COVER_WORD = "封面"            # a fact line labelled with this word is about the cover picture, not about how she talks


def basics(text: str | None = None) -> str:
    """persona.md whole, without its cover-expression section (from that heading line to the next `## ` heading or
    the end)."""
    if text is None:
        text = PERSONA.read_text(encoding="utf-8")
    kept: list[str] = []
    skipping = False
    for line in text.splitlines():
        if line.startswith(COVER_SECTION):
            skipping = True
            continue
        if skipping and line.startswith("## "):
            skipping = False
        if not skipping:
            kept.append(line)
    return "\n".join(kept).strip()


def spoken(text: str | None = None) -> str:
    """The facts a reader of her speech needs: `basics` without the lines whose label is about the cover picture, and
    without the ledger's markers."""
    lines = []
    for line in basics(text).splitlines():
        label = line.lstrip("- ").split("：", 1)[0]
        if COVER_WORD in label or line.startswith("# "):
            continue
        lines.append(line.split(" <!--", 1)[0].rstrip())
    return "\n".join(lines).strip()


def fans(text: str | None = None) -> str:
    """The fan-culture line (how the audience is called), without its bullet; "" when the file has none."""
    if text is None:
        text = PERSONA.read_text(encoding="utf-8")
    return next((ln.strip()[2:] for ln in text.splitlines() if ln.strip().startswith("- 粉丝文化")), "")


SUBTITLE_NOTE = ("频道基本事实（主播是谁、怎么称呼自己和观众、说话的习惯；用来判断一个音在她嘴里更可能是哪个词、"
                 "一句话像不像她会说的。它不是语音证据：不能据此补她没说的字，也不能推翻来源一致的音）：")


OBSERVED_HEAD = "## 切片里多次观察到（尚未经频道主审定，只作线索）"


def observed(task: Path | None) -> str:
    """What the task's clips have shown of her in several sessions (persona_observe.lines), as a block; "" when none."""
    from . import persona_observe

    rows = persona_observe.lines(task) if task is not None else []
    return "\n".join([OBSERVED_HEAD, *rows]) if rows else ""


def for_subtitles(task: Path | None = None) -> str:
    """The block the subtitle agent reads (step 3)."""
    return "\n".join(filter(None, [SUBTITLE_NOTE, spoken(), observed(task)]))
