from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.autoslice.cover_generation import (
    LidoushaCoverArtDirection,
    _overlay_lidousha_cover_title,
)


ROOT = Path(__file__).resolve().parent
BACKGROUND = ROOT / "ai-background.png"


def render(name: str, text: str, layout: str) -> None:
    direction = LidoushaCoverArtDirection(
        role="witty_smug",
        expression_en=(
            "Closed-mouth mischievous smirk with one eyebrow raised, "
            "playfully interrogative and slightly biting back, never sticking out her tongue."
        ),
        background_style="cobalt-comic-burst",
        layout=layout,
        hook_color="yellow",
        is_song=False,
        hook_word="最最最喜欢",
    )
    out = ROOT / f"{name}.png"
    metadata = _overlay_lidousha_cover_title(
        BACKGROUND,
        out,
        cover_text=text,
        art_direction=direction,
    )
    (ROOT / f"{name}.json").write_text(
        json.dumps({"cover_text": text, **metadata}, ensure_ascii=False, indent=2)
        + "\n",
        encoding="utf-8",
    )


render("a-banner-short", "提到我就要\n最最最喜欢？", "banner")
render("b-banner-why", "为什么提到我\n就要最最最喜欢？", "banner")
render("c-split-short", "提到我就要\n最最最喜欢？", "left-split")
