"""How a chat line is matched against what was said: the text normal form, its similarity score and the
earliest delay at which a spoken reading can follow a send.

Split out of chat_evidence and chat_repair so the lane reuses the same match without loading the chat authority;
both re-export the names for their existing importers."""
from __future__ import annotations

from difflib import SequenceMatcher

from src.autoslice.text_scripts import character_script

# Preserve the former re.IGNORECASE ASCII range's four Unicode fold surfaces.
# This is normalization compatibility, not another script inventory.
_CHAT_CASEFOLD_SURFACES = frozenset("İıſK")


READ_ALOUD_MIN_DELAY_MS = 2_000


def normalize_chat_text(text: str) -> str:
    return "".join(char for char in str(text)
        if character_script(char) in {"latin", "kana", "han"}
        or "0" <= char <= "9" or char in _CHAT_CASEFOLD_SURFACES).lower()


def _match_metrics(authority: str, candidate: str) -> tuple[float, float, float, float, int]:
    left, right = normalize_chat_text(authority), normalize_chat_text(candidate)
    if not left or not right:
        return 0.0, 0.0, 0.0, 0.0, 0
    matcher = SequenceMatcher(None, left, right)
    common = sum(block.size for block in matcher.get_matching_blocks())
    coverage = common / len(left)
    precision = common / len(right)
    ratio = matcher.ratio()
    score = 0.50 * ratio + 0.35 * coverage + 0.15 * precision
    return score, ratio, coverage, precision, common
