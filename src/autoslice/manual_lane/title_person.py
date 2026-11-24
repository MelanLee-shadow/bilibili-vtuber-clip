"""One title, one voice (step 6 self-check `person`).

Owner 「你前面一句话是以李豆沙的口吻说的，后面一句话又是第三人称说的，不喜欢第三人称」.  A title whose
narration calls the host by name and which also says 我 reads as two people, whichever comes first
(「李豆沙续费Oopz后，我那个高贵的会员标志…」 and 「李豆沙聊香蕉牛奶是不是侄女，什么？我之前问过她呀…」
went online; the writer had put her name in front to pass the swap check).  So outside quotation marks a title body
holds the host's name or a first-person word, not both: either her words all the way, or narration all the way.
Words inside quotation marks are a quotation and are not counted.  Of 108 published titles the rule names three (all
of the 「小李：…我…」 form the skill already advises against); of the owner's 20 corpus titles, none.

The host's names are the channel's `title_name_forms` (the same pattern title_history reads)."""

from __future__ import annotations

import re
from collections.abc import Sequence

from src.autoslice.channel_envelope import parse_talk_title
from src.autoslice.manual_lane import channel, text_overlap

_NAME_FORMS = re.compile(str(channel.text()["title_name_forms"]) or r"(?!)")
_QUOTED = re.compile(r"“[^”]*”|「[^」]*」|『[^』]*』|\"[^\"]*\"")
_FIRST_PERSON = re.compile(r"我|咱|俺")
_NOT_A_PRONOUN = ("自我",)


def mixed_person(title: str) -> tuple[str, str] | None:
    """(the host's name the narration uses, the first-person word) when the body holds both outside quotation marks."""
    envelope = parse_talk_title(title)
    body = _QUOTED.sub("", envelope.body if envelope is not None else title)
    for word in _NOT_A_PRONOUN:
        body = body.replace(word, "")
    name, first = _NAME_FORMS.search(body), _FIRST_PERSON.search(body)
    return (name.group(), first.group()) if name and first else None


_CLAUSE = re.compile(r"[^，,。.！!？?、；;：:…~～\s“”「」『』\"]+")
_LABEL = re.compile(r"^\[([^\]]+)\]\s*(.*)$")
_ATTRIBUTION = re.compile(r"(?:[:：]|说|喊|问|道|叫|吐槽|感叹|哭诉)\s*[“「『\"]?\s*$")


def _labelled_lines(subtitles: str) -> list[tuple[str, str]]:
    """(speaker, letters) per cue of an SRT whose lines carry `[说话人] ` labels (title_authority.speaker_srt)."""
    out = []
    for block in re.split(r"\n\s*\n", subtitles.strip()):
        rows = block.splitlines()[2:]
        m = _LABEL.match(rows[0]) if rows else None
        if m:
            out.append((m.group(1), text_overlap.letters(" ".join([m.group(2), *rows[1:]]))))
    return out


def _source(clause: str, lines: list[tuple[str, str]]) -> str:
    """The speaker of the line the clause was taken from: its letters stand in that line (or in two neighbouring lines
    of one speaker); "" when no line holds it or lines of different speakers do."""
    want = text_overlap.letters(clause)
    found = {spk for spk, text in lines if want in text}
    found |= {a[0] for a, b in zip(lines, lines[1:]) if a[0] == b[0] and want in a[1] + b[1]}
    return next(iter(found)) if len(found) == 1 else ""


def misattributed(title: str, subtitles: str, *, host: str, guests: Sequence[str]) -> list[tuple[str, str]]:
    """(first-person clause, who said it) for each clause of the title whose 我 a viewer would take for someone else.

    Owner (U467): 「有的地方你用一个我，别人观众不知道这个到底是说的是我是这联动里的谁」 — 583860's
    「我真的要被优化掉了！我们感觉要没了！」 put a guest's 我 and the host's 我们 side by side with nobody named.  A
    first-person clause is read as the speaker the title names just before it (「礼墨：我要笑」, 「安晚说“…”」, the last
    name in that stretch), else as the host's; it is wrong when its subtitle line is someone else's (a line labelled only
    连线 takes any guest's name).  Clauses not found in the subtitles are the writer's own narration, and solo clips have
    nothing to confuse."""
    lines = _labelled_lines(subtitles)
    if not any(spk != host for spk, _ in lines):
        return []
    envelope = parse_talk_title(title)
    body = envelope.body if envelope is not None else title
    out = []
    for m in _CLAUSE.finditer(body):
        clause = m.group()
        if not _FIRST_PERSON.search(clause.replace("自我", "")):
            continue
        said_by = _source(clause, lines)
        if not said_by:
            continue
        read_as = _attributed(_lead(body, m.start()), host, guests)
        if said_by == host:
            wrong = read_as != host
        elif said_by in guests:
            wrong = read_as != said_by
        else:                       # a guest label that names nobody: any guest's name will do
            wrong = read_as == host
        if wrong:
            out.append((clause, said_by))
    return out


_OPEN, _CLOSE = "“「『", "”」』"


def _lead(body: str, at: int) -> str:
    """The words that attribute what starts at `at`: the stretch before it (before its opening quotation mark when it is
    inside one) back to the previous sentence break, when that stretch ends with 「：」 or a verb of saying."""
    before = body[:at]
    depth_at = max((i for i, ch in enumerate(before) if ch in _OPEN), default=-1)
    if depth_at > max((i for i, ch in enumerate(before) if ch in _CLOSE), default=-1):
        before = before[:depth_at + 1]
    cut = max(before.rfind(c) for c in "。！!？?；;")
    lead = before[cut + 1:]
    return lead if _ATTRIBUTION.search(lead) else ""


def _attributed(lead: str, host: str, guests: Sequence[str]) -> str:
    """Whom the lead names last (its full name or the Chinese part of it; the host also by the channel's name forms);
    the host when it names nobody."""
    at = {g: max(lead.rfind(g), lead.rfind(_han(g)) if _han(g) else -1) for g in guests}
    host_at = max([lead.rfind(host), *(m.start() for m in _NAME_FORMS.finditer(lead))])
    best = max(at, key=lambda g: at[g], default="")
    return best if best and at[best] > host_at else host


def _han(name: str) -> str:
    """The Chinese part of a name written Chinese then letters (礼墨Sumi -> 礼墨), the short form titles use."""
    m = re.match(r"[一-鿿]{2,}", name)
    return m.group() if m and m.group() != name else ""


def misattributed_reason(found: list[tuple[str, str]]) -> str:
    return ("「我」分不清是谁：" + "；".join(f"“{c}”是{s}说的" for c, s in found)
            + "，标题里没写出是她说的，观众会当成主播自己；写明是谁说的（如「名字：…」），或换成主播自己说的话")


def reason(mixed: tuple[str, str]) -> str:
    return (f"人称不一致：旁白里叫了主播的名字（{mixed[0]}），又出现第一人称“{mixed[1]}”，读起来像两个人；"
            "整条用她的话（不写她的名字），或整条旁白（不出现“我”）；带引号的原话不算")
