"""A circle name that is not the person's own name: where it is written down, and the mechanical sign that ties it to a name.

Owner, after the lane and the crawler both failed to place 「香蕉牛奶」: 香蕉牛奶是咲间妮娜的圈内昵称，取自名字拼音首字母 xjnn；这类昵称新闻爬虫应当查得到.  Two things were missing, and both are here for the lane's lookup
(manual_lane/circle_context.py) and the crawler (clip_name_identity.py) alike:

- where to look: the circle's encyclopedias list a streamer's other names under 别名.  "<name> <host>" and "<name> 虚拟主播"
  find the everyday meaning of the words (a drink); `ALIAS_QUERY` finds the page whose list of names holds it.
- the sign: a nickname made this way has the pinyin first letters of the name it stands for (咲间妮娜 → xjnn → 香蕉牛奶).
  `letters` gives them; `signs` finds, in a search result, the letters written out or a name of the same length with the
  same letters; `same_letters` finds such names among names already known.  A sign is a lead for the reader of the
  results, never the answer: many names share four letters.
- `written` finds the name in a text under another spelling of the same sound: the page of 謝兒 lists 謝寶, the clip
  writes 谢宝 (the first member that was not used to design this, and it failed on an exact comparison).  No converter
  between the two scripts is installed; the pinyin is the same, and a name's other spellings of one sound are one name.
- `listed` is the search both use: the alias query; asked once more when no result shows the sign (the router rotates
  its engines, and one of three answered without the page on the day this was written); and a result that shows the
  sign but whose snippet does not hold the name has its page opened and the text around the name put in the snippet
  (an encyclopedia page begins with navigation; the list of names is further down).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence

from .acoustic_pinyin import text_pinyin_tokens

ALIAS_QUERY = "{name} 别名 虚拟UP主"
PASSAGE_CHARS = 280                    # what is kept of a page that was opened: the text around the name
PAGES_READ = 2                         # pages opened for one name
MIN_CHARS, MAX_CHARS = 3, 6            # two letters tie a name to nothing; a longer run is a sentence, not a name
_HAN = re.compile(r"[一-鿿]+")
_LATIN = re.compile(r"[A-Za-z]+")


def letters(name: str) -> str:
    """The pinyin first letter of every character of a name written in MIN_CHARS..MAX_CHARS Chinese characters, else ""."""
    name = name.strip()
    if not _HAN.fullmatch(name) or not MIN_CHARS <= len(name) <= MAX_CHARS:
        return ""
    syllables = text_pinyin_tokens(name) or []
    return "".join(s[0] for s in syllables).lower() if len(syllables) == len(name) and all(syllables) else ""


def signs(name: str, title: str, snippet: str = "") -> list[str]:
    """What a search result shows of the name's letters: the letters as a word of their own (anywhere in the result), and
    a run of Chinese characters in the title, as long as the name, with the same letters (a page is titled by the
    person's name)."""
    want = letters(name)
    if not want:
        return []
    found = [m.group(0) for m in _LATIN.finditer(f"{title} {snippet}") if m.group(0).lower() == want]
    found += [run for run in _HAN.findall(title) if run != name.strip() and letters(run) == want]
    return list(dict.fromkeys(found))


def same_letters(name: str, known: Iterable[tuple[str, Iterable[str]]]) -> list[str]:
    """The known names (display, spellings) one of whose spellings has the name's letters or is the letters written out."""
    want = letters(name)
    if not want:
        return []
    return list(dict.fromkeys(
        display for display, spellings in known
        if any(s.strip() != name.strip() and (s.strip().lower() == want or letters(s) == want) for s in spellings)))


def _where(text: str, name: str) -> tuple[int, int] | None:
    """Where the text writes the name: as it is, else a run of as many Chinese characters with the same pinyin."""
    name = name.strip()
    at = text.find(name) if name else -1
    if at >= 0:
        return at, at + len(name)
    want = (text_pinyin_tokens(name) or []) if _HAN.fullmatch(name) and len(name) >= 2 else []
    if len(want) != len(name) or not all(want):
        return None
    for run in _HAN.finditer(text):
        got = (text_pinyin_tokens(run.group(0)) or []) if len(run.group(0)) >= len(want) else []
        if len(got) != len(run.group(0)):
            continue
        for i in range(len(got) - len(want) + 1):
            if got[i:i + len(want)] == want:
                return run.start() + i, run.start() + i + len(want)
    return None


def written(text: str, name: str) -> str:
    """The name as the text writes it (itself, or another spelling of the same sound); "" when it does not."""
    span = _where(text, name)
    return text[span[0]:span[1]] if span else ""


def passage(text: str, name: str) -> str:
    """The PASSAGE_CHARS of a page around the first place it writes the name, on one line; "" when it does not."""
    flat = " ".join(text.split())
    span = _where(flat, name)
    if span is None:
        return ""
    start = max(0, span[0] - PASSAGE_CHARS // 2)
    return flat[start:start + PASSAGE_CHARS]


Row = dict[str, object]


def listed(name: str, search: Callable[[str], Sequence[Mapping[str, object]]],
           page: Callable[[str, str], str] | None = None) -> list[Row]:
    """The alias query's results for a name (title, url, snippet), those that show its letters first.  `page(url, about)`
    reads a page; `about` says what is looked for on it."""
    query = ALIAS_QUERY.format(name=name)

    def shown(row: Mapping[str, object]) -> bool:
        return bool(signs(name, str(row.get("title") or ""), str(row.get("snippet") or "")))

    rows: list[Row] = [dict(r) for r in search(query)]
    if letters(name) and not any(shown(r) for r in rows):
        seen = {r.get("url") for r in rows}
        rows += [dict(r) for r in search(query) if r.get("url") not in seen]
    opened = 0
    for row in rows:
        if page is None or opened >= PAGES_READ or not row.get("url") or not shown(row) \
                or written(str(row.get("snippet") or ""), name):
            continue
        opened += 1
        try:
            found = passage(page(str(row["url"]), f"别名 {name}"), name)
        except Exception:  # noqa: BLE001 - a page that cannot be read leaves the result as the search gave it
            continue
        if found:
            row["snippet"] = found
    return sorted(rows, key=lambda r: not shown(r))
