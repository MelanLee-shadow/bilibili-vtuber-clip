"""The channel owner's per-candidate verdicts on titles, read where step 6 uses them.

assets/lidousha/title_owner_verdicts.v1.json holds, for every clip the owner went through, the clip's main line, each
candidate title he was shown with the grade he gave it (`grade` 2 很不错 / 1 还可以 / 0 一般 / -1 差 / -2 尤其差, null when
he did not mention it) and his own words.  He adds each new batch to the same file, so the next clip is written and
ranked against more of his taste.  Two pieces of text are rendered from it, both always without the current clip
(`exclude_cid`, and its source clip when the current one is a trimmed successor `<cid>_trim`; the successor is the same
manuscript, so it also takes the verdict the owner gave the source clip):

- `liked_and_disliked`: what he accepted, and what he rejected with his words, for the title prompt;
- `corpus`: clip by clip, for the ranking prompt (title_pick).

A missing or malformed file stops the step (AuthorityError): a title is not written without the taste it was to follow.
The file carries his private words and is not part of the public export.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import NotRequired, TypedDict

from src.autoslice.title_editorial_method import AuthorityError, Source, _read

from . import channel
from .publication_id import lineage, publication_cid
from .step_io import _render

VERDICTS = channel.asset("title_owner_verdicts.v1.json")
SCHEMA = "title-owner-verdicts.v1"
LIKED_MIN = 1              # grade of 还可以 and above: accepted
DISLIKED_MAX = -1          # grade of 差 and below: rejected


class VerdictCandidate(TypedDict):
    letter: str
    title_body: str
    grade: int | None        # 2 很不错, 1 还可以, 0 一般, -1 差, -2 尤其差; None when he did not mention it
    label: str


class VerdictClip(TypedDict):
    """One clip the owner went through.  `approved_title_body` / `approved_evidence` are present only when he picked the
    title body for this clip himself (the body without the prefix, and his words)."""
    no: int
    cid: str
    main_line: str
    punchline: str
    owner_comment: str
    candidates: list[VerdictCandidate]
    approved_title_body: NotRequired[str]
    approved_evidence: NotRequired[str]


class ApprovedTitle(TypedDict):
    title_body: str
    evidence: str


def _text(row: object, key: str, where: str) -> str:
    value = row.get(key) if isinstance(row, dict) else None
    if not isinstance(value, str):
        raise AuthorityError(f"title_owner_verdicts.v1.json: {where}.{key} is not a string")
    return value


def _candidate(raw: object, where: str) -> VerdictCandidate:
    if not isinstance(raw, dict):
        raise AuthorityError(f"title_owner_verdicts.v1.json: {where} is not an object")
    grade = raw.get("grade")
    if grade is not None and (not isinstance(grade, int) or isinstance(grade, bool)):
        raise AuthorityError(f"title_owner_verdicts.v1.json: {where}.grade is neither an integer nor null")
    body = _text(raw, "title_body", where)
    if not body.strip():
        raise AuthorityError(f"title_owner_verdicts.v1.json: {where}.title_body is empty")
    return {"letter": _text(raw, "letter", where), "title_body": body, "grade": grade,
            "label": _text(raw, "label", where)}


def _clip(raw: object, index: int) -> VerdictClip:
    where = f"clips[{index}]"
    if not isinstance(raw, dict):
        raise AuthorityError(f"title_owner_verdicts.v1.json: {where} is not an object")
    number = raw.get("no")
    if not isinstance(number, int) or isinstance(number, bool):
        raise AuthorityError(f"title_owner_verdicts.v1.json: {where}.no is not an integer")
    cid = _text(raw, "cid", where)
    if not cid.strip():
        raise AuthorityError(f"title_owner_verdicts.v1.json: {where}.cid is empty")
    candidates = raw.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise AuthorityError(f"title_owner_verdicts.v1.json: {where}.candidates is not a non-empty list")
    clip: VerdictClip = {
        "no": number, "cid": cid, "main_line": _text(raw, "main_line", where),
        "punchline": _text(raw, "punchline", where), "owner_comment": _text(raw, "owner_comment", where),
        "candidates": [_candidate(c, f"{where}.candidates[{i}]") for i, c in enumerate(candidates)]}
    if "approved_title_body" in raw or "approved_evidence" in raw:
        body = _text(raw, "approved_title_body", where)
        evidence = _text(raw, "approved_evidence", where)
        if not body.strip() or not evidence.strip():
            raise AuthorityError(f"title_owner_verdicts.v1.json: {where} needs both a non-empty approved_title_body "
                                 f"and the approved_evidence (his words)")
        clip["approved_title_body"], clip["approved_evidence"] = body, evidence
    return clip


def excluded_cids(cid: str) -> frozenset[str]:
    """The clip ids whose verdicts must not reach the current clip: itself and every clip it was made from, down to the
    manuscript (all of them are earlier versions of the one manuscript)."""
    return lineage(cid)


@dataclass(frozen=True)
class OwnerVerdicts:
    clips: tuple[VerdictClip, ...]
    source: Source

    def others(self, exclude_cid: str) -> list[VerdictClip]:
        gone = excluded_cids(exclude_cid)
        return [c for c in self.clips if c["cid"] not in gone]

    def approved(self, cid: str) -> ApprovedTitle | None:
        """The title body the owner picked for this manuscript (the clip id, or the source clip's for a trimmed successor),
        with his words; None when he has not."""
        for clip in self.clips:
            if clip["cid"] == publication_cid(cid) and "approved_title_body" in clip:
                return {"title_body": clip["approved_title_body"].strip(), "evidence": clip["approved_evidence"]}
        return None

    def liked_and_disliked(self, exclude_cid: str) -> str:
        """Two lists: the candidates he accepted, and the ones he rejected with his words about the clip."""
        good: list[str] = []
        bad: list[str] = []
        for clip in self.others(exclude_cid):
            graded = [(c["title_body"].strip(), c["grade"]) for c in clip["candidates"]]
            good += [f"- {body}" for body, grade in graded if grade is not None and grade >= LIKED_MIN]
            rejected = [body for body, grade in graded if grade is not None and grade <= DISLIKED_MAX]
            if rejected:
                bad.append(f"- {'／'.join(rejected)}　他的原话：{clip['owner_comment']}")
        return ("他认可的（很不错／还可以）：\n" + "\n".join(good) + "\n\n他判差的（差／尤其差），以及他说的毛病：\n"
                + "\n".join(bad))

    def corpus(self, exclude_cid: str) -> str:
        """Clip by clip: what the clip is about, each candidate with his label, then his words."""
        blocks = []
        for clip in self.others(exclude_cid):
            lines = [f"### 片 {clip['no']}", f"这段讲的是：{clip['main_line']}"]
            lines += [f"- {c['title_body'].strip()} → {c['label']}" for c in clip["candidates"]]
            lines.append(f"他的原话：{clip['owner_comment']}")
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks)

    def title_block(self, exclude_cid: str) -> str:
        """The section the title prompt carries after the main-line block (prompts/title_owner_verdicts.md), and when the
        owner has picked this clip's title body, the sentence that says so (prompts/title_approved.md).  The taste texts
        above never hold the clip itself."""
        block = _render("title_owner_verdicts.md", owner_verdicts=self.liked_and_disliked(exclude_cid)).strip()
        approved = self.approved(exclude_cid)
        if approved is None:
            return block
        return block + "\n\n" + _render("title_approved.md", approved_title_body=approved["title_body"]).strip()


def load_verdicts() -> OwnerVerdicts:
    text, source = _read(f"{channel.ASSETS_REL}/title_owner_verdicts.v1.json", VERDICTS)
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise AuthorityError(f"title_owner_verdicts.v1.json: not JSON: {exc}") from exc
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA:
        raise AuthorityError(f"title_owner_verdicts.v1.json: schema_version is not {SCHEMA}")
    clips = data.get("clips")
    if not isinstance(clips, list):
        raise AuthorityError("title_owner_verdicts.v1.json: clips is not a list")
    return OwnerVerdicts(tuple(_clip(c, i) for i, c in enumerate(clips)), source)


def liked_and_disliked(exclude_cid: str) -> str:
    return load_verdicts().liked_and_disliked(exclude_cid)


def corpus(exclude_cid: str) -> str:
    return load_verdicts().corpus(exclude_cid)
