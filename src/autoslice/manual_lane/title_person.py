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

from src.autoslice.channel_envelope import parse_talk_title
from src.autoslice.manual_lane import channel

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


def reason(mixed: tuple[str, str]) -> str:
    return (f"人称不一致：旁白里叫了主播的名字（{mixed[0]}），又出现第一人称“{mixed[1]}”，读起来像两个人；"
            "整条用她的话（不写她的名字），或整条旁白（不出现“我”）；带引号的原话不算")
