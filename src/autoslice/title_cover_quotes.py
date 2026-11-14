"""Final-subtitle quote and candidate checks; no media, ASR or title-as-source."""

from __future__ import annotations

import re

from .title_quote_check import QUOTES as QUOTES
from .title_quote_check import check_title_quotes as check_title_quotes
from .title_quote_check import final_cues as final_cues, find_quote as find_quote


def check_cover_quotes(lines: list[str], final_srt: str) -> list[dict]:
    result = []
    for line in lines:
        body = re.split(r"[：:]", line, maxsplit=1)[-1]
        match = find_quote(body, final_srt)
        quoted = any(QUOTES.findall(line))
        result.append(
            {
                "line": line,
                "status": "VERBATIM" if match else "MISSING_QUOTE" if quoted else "PARAPHRASE",
                "match": match,
                "semantic_review_required": not bool(match),
            }
        )
    return result


