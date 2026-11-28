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
from collections.abc import Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import NotRequired, TypedDict

from src.autoslice.title_editorial_method import AuthorityError, Source, _read

from . import channel, text_overlap
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
    title body for this clip himself (the body without the prefix, and his words): he said to use it as the title.  A
    sentence he says while naming the draw or faulting a title is the draw, not a picked title (owner "这个是
    说看点，不是说标题…可以直接用，也可以按照这个围绕看点再起一个新的"): it goes in main_line/punchline with his words as
    `retitle_evidence`, and the title step writes the title from it."""
    no: int
    cid: str
    main_line: str
    punchline: str
    owner_comment: str
    candidates: list[VerdictCandidate]
    approved_title_body: NotRequired[str]
    approved_evidence: NotRequired[str]
    retitle_evidence: NotRequired[str]       # he asked for a new title without writing one (his words)


class RefusedMoment(TypedDict):
    text: str             # letters (text_overlap.key, names out) of the refused title and of the cues it lands on
    title_body: str       # the title he graded bad
    comment: str          # his words on the clip


MOMENT_MIN_RUN = 4         # letters a title and a cue share in a row for the title to land on that cue
COVER_ON_MOMENT = 0.5      # share of a cover line's letters found in a refused moment (in order) that puts it there
COVER_MIN_RUN = 3          # ... with at least this many of them in a row (a shared 你/了 here and there is chance)
# Measured on the four sealed 11-23 revisions (review of review-names, 11-26): every rewording of 897000's refused
# moment the step wrote or could write (你刚刚还在抓矿耶 0.88, 电脑无响应了 0.5, 豆沙：谁在抓？ 0.6) is at or above
# 0.5; as cover text 5 of its 74 cues are stopped, all of that exchange; 35 of 583860's 89, all within a cue of its four
# titles he graded bad (they are on every beat but 南町's, which stays open); none of 625460's or 443680's.  A
# liked title's landing is its own cues only, so a refused cue beside it stays refused.


def _without_names(text: str, names: Sequence[str]) -> str:
    """`text` (a key) without the participants' names, whole or their Han part (礼墨 of 礼墨Sumi): a name is in every
    title of a clip and is no moment."""
    for name in names:
        for spelling in (text_overlap.key(name), "".join(ch for ch in name if not ch.isascii())):
            if len(spelling) >= 2:
                text = text.replace(spelling, "")
    return text


def _longest_run(a: str, b: str) -> int:
    return max((m.size for m in SequenceMatcher(None, a, b, autojunk=False).get_matching_blocks()), default=0)


def refused_cover_lines(cid: str, lines: Sequence[str], cue_texts: Sequence[str], names: Sequence[str]) -> list[str]:
    """Why cover `lines` may not go out: a line is on a moment the owner refused (OwnerVerdicts.refused_moments): at
    least COVER_ON_MOMENT of its letters, names left out, are found there in order, COVER_MIN_RUN of them in a row, and
    more of them than on any moment of a title of the clip he liked or picked; [] otherwise.  A reworded line is still on
    the moment (897000: 「你刚刚还在抓矿耶」 on the refused 「但你刚刚在抓耶」)."""
    verdicts = load_verdicts()
    moments = verdicts.refused_moments(cid, cue_texts, names)
    kept = verdicts.kept_moments(cid, cue_texts, names)
    out = []
    for line in lines:
        letters = _without_names(text_overlap.key(line), names)
        if not letters:
            continue
        on_kept = max((text_overlap.share(letters, k) for k in kept), default=0.0)
        hit = next((m for m in moments if (on := text_overlap.share(letters, m["text"])) >= COVER_ON_MOMENT
                    and on > on_kept and _longest_run(letters, m["text"]) >= COVER_MIN_RUN), None)
        if hit is not None:
            out.append(f"封面字「{line}」落在频道主否掉的那一下；被判差的标题：{hit['title_body']}，写的就是这里"
                       f"（他的原话：{hit['comment'][:200]}）：换成这条片别的看点")
    return out


class ApprovedTitle(TypedDict):
    title_body: str
    evidence: str


class Retitle(TypedDict):
    evidence: str     # his words asking for a new title
    main_line: str    # what the clip is about, by his reading
    punchline: str    # its main draw, by his reading


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
    if "retitle_evidence" in raw:
        clip["retitle_evidence"] = _text(raw, "retitle_evidence", where)
        if not clip["retitle_evidence"].strip():
            raise AuthorityError(f"title_owner_verdicts.v1.json: {where}.retitle_evidence is empty")
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

    def retitle(self, cid: str) -> Retitle | None:
        """His words asking for a new title on this manuscript, with what he says the clip is about (main_line,
        punchline of his verdict): a revision then takes the title step's own pick, written from his reading of the clip
        instead of the model's, rather than keeping the title online.  None when he has not."""
        for clip in self.clips:
            if clip["cid"] == publication_cid(cid) and "retitle_evidence" in clip:
                return {"evidence": clip["retitle_evidence"], "main_line": clip["main_line"],
                        "punchline": clip["punchline"]}
        return None

    def _landing(self, body: str, cues: Sequence[str], names: Sequence[str], not_in: Sequence[str], *, beat: bool) -> str:
        """A title's letters (names out) and the cues it is written on: each cue it shares MOMENT_MIN_RUN letters in a
        row with (a run also in one of `not_in` not counted), with `beat` the cue before and after it too (897000: the
        refused title quotes 「我在卡，我在无响应」「但你刚刚在抓耶」; the r5 cover answered with the next cue, 「谁在抓？」);
        "" when it lands on no cue of the clip."""
        body = _without_names(text_overlap.key(body), names)
        hit = [i for i, cue in enumerate(cues) if any(
            m.size >= MOMENT_MIN_RUN and not any(body[m.a:m.a + m.size] in k for k in not_in)
            for m in SequenceMatcher(None, body, cue, autojunk=False).get_matching_blocks())]
        on = sorted({j for i in hit for j in ((i - 1, i, i + 1) if beat else (i,)) if 0 <= j < len(cues)})
        return body + "".join(cues[j] for j in on) if hit else ""

    def _graded(self, cid: str) -> tuple[list[VerdictCandidate], list[str]]:
        """The candidates he graded bad for this manuscript, and the bodies of those he liked or picked."""
        clip = next((c for c in self.clips if c["cid"] == publication_cid(cid)), None)
        if clip is None:
            return [], []
        kept = [c["title_body"] for c in clip["candidates"] if (c["grade"] or 0) >= LIKED_MIN]
        kept += [clip["approved_title_body"]] if "approved_title_body" in clip else []
        return [c for c in clip["candidates"] if c["grade"] is not None and c["grade"] <= DISLIKED_MAX], kept

    def refused_moments(self, cid: str, cue_texts: Sequence[str], names: Sequence[str]) -> list[RefusedMoment]:
        """Where the titles the owner graded bad for this manuscript (grade <= DISLIKED_MAX) land in its subtitles
        (`_landing`; a run also in a title he liked or picked is not theirs).  A cover text written there is written on
        the moment he refused (his grade on 897000's candidate that put its punchline there), however it is
        reworded around it (`refused_cover_lines`)."""
        refused, kept = self._graded(cid)
        cues = [text_overlap.key(t) for t in cue_texts]
        kept_keys = [_without_names(text_overlap.key(k), names) for k in kept]
        comment = next(c["owner_comment"] for c in self.clips if c["cid"] == publication_cid(cid)) if refused else ""
        out: list[RefusedMoment] = []
        for cand in refused:
            if text := self._landing(cand["title_body"], cues, names, kept_keys, beat=True):
                out.append({"text": text, "title_body": cand["title_body"], "comment": comment})
        return out

    def kept_moments(self, cid: str, cue_texts: Sequence[str], names: Sequence[str]) -> list[str]:
        """The landings (letters) of the titles of this manuscript he liked or picked: their own cues only, so a refused
        cue beside his liked one stays refused."""
        cues = [text_overlap.key(t) for t in cue_texts]
        return [t for k in self._graded(cid)[1] if (t := self._landing(k, cues, names, (), beat=False))]

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
