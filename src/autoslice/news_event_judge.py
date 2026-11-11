"""CPA prompts and strict parsing for the news/event crawler.

CPA decides whether a search result shows an event, project, alliance or collaboration, what kind it is, who takes
part and how they relate; this module only states the question, requires structured JSON and rejects answers that
break the contract (unknown url, wrong type, bad relation).  It carries no name list.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TypedDict, cast

from src.autoslice.knowledge_graph import Graph, JsonValue
from src.autoslice.llm_client import extract_json_object

EVENT_KINDS = ("event", "project", "group")
PARTICIPANT_KINDS = ("person", "group", "project", "event", "community")
RELATIONS = ("member_of", "founded", "participates_in", "related_to")
CATALOG_KINDS = ("person", "group", "project", "event")
CATALOG_CANDIDATE_LIMIT = 250
MAX_NAME_CHARS = 40
MAX_SUMMARY_CHARS = 160
MAX_ALIASES = 6
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")


class JudgeError(ValueError):
    """The CPA answer does not fit the requested JSON contract."""


class Row(TypedDict):
    """One search result as CPA sees it."""
    title: str | None
    url: str
    snippet: str | None


Batch = Sequence[tuple[str, Sequence[Row]]]   # (seed, its search results)


@dataclass(frozen=True)
class Participant:
    name: str
    kind: str
    relation: str
    id: str | None = None


@dataclass(frozen=True)
class Finding:
    url: str
    seed: str
    kind: str
    name: str
    participants: tuple[Participant, ...]
    id: str | None = None
    aliases: tuple[str, ...] = ()
    summary: str = ""
    valid_from: str | None = None
    valid_until: str | None = None


def catalog(graph: Graph) -> list[dict[str, JsonValue]]:
    """People, groups, projects and events the answer may point to by id: every accepted node, and the newest
    candidates (those this crawler found earlier) up to CATALOG_CANDIDATE_LIMIT."""
    def entry(node_id: str) -> dict[str, JsonValue]:
        node = graph.nodes[node_id]
        return {"id": node.id, "kind": node.kind, "name": node.names["display"], "aliases": list(node.aliases[:4])}

    wanted = [n for n in graph.nodes.values() if n.kind in CATALOG_KINDS]
    accepted = [n.id for n in wanted if n.status == "accepted"]
    newest = sorted((n for n in wanted if n.status == "candidate"),
                    key=lambda n: max(s.at or "" for s in n.sources))
    candidates = [n.id for n in newest][-CATALOG_CANDIDATE_LIMIT:]
    return [entry(node_id) for node_id in (*accepted, *candidates)]


def mention_prompt(owner: str, texts: Sequence[str]) -> str:
    body = "\n".join(f"- {text}" for text in texts)
    return (
        f"下面是主播「{owner}」自己最近发的 B 站动态原文。请列出文中提到的其他人物（主播、嘉宾、联动对象）、"
        "企划、活动、联盟、游戏或赛事的名称，供之后联网检索它们的近期新闻。\n"
        "规则：名称必须逐字出现在动态原文里，不要补全、翻译或猜测；不含主播自己；没有就返回空列表。\n"
        f"动态：\n{body}\n\n"
        '只输出 JSON：{"mentions": ["名称", ...]}')


def parse_mentions(raw: str, texts: Sequence[str]) -> list[str]:
    """The names CPA listed that really occur in the dynamics text (an invented name never becomes a seed)."""
    names = _object(raw).get("mentions")
    if not isinstance(names, list):
        raise JudgeError("mentions must be a list")
    haystack = "\n".join(texts)
    out: list[str] = []
    for name in names:
        if isinstance(name, str) and 2 <= len(name.strip()) <= MAX_NAME_CHARS and name.strip() in haystack \
                and name.strip() not in out:
            out.append(name.strip())
    return out


def judge_prompt(owner: str, today: str, nodes: Sequence[Mapping[str, JsonValue]], batches: Batch) -> str:
    material = [{"seed": seed, "results": list(rows)} for seed, rows in batches]
    return (
        f"今天是 {today}。你在为主播「{owner}」所在的虚拟主播圈整理事件、企划、联盟与联动，供字幕、标题和人设"
        "查阅。下面每个检索词（seed）带着若干检索结果（标题、网址、摘要）。\n\n"
        "任务：逐条判断一条结果是否表明一个具体的事件、企划、联盟、联动或活动（例如联动直播、联合企划、成立的联盟、"
        "夜谈类系列节目、展会合作、周年活动），以及谁参与、各自是什么关系。以下不要输出：与圈内无关的结果"
        "（小说、百科词条、广告）、只是泛泛介绍某人的结果、仅仅是某人某一场普通直播或某个视频本身（除非它自己就是"
        "一次联动或活动）。\n"
        "规则：\n"
        "1. 只依据所给标题和摘要，不补充你自己记得的内容；拿不准就不输出。\n"
        "2. url 必须逐字取自该条结果；一条结果最多输出一项。\n"
        "3. kind：event（一次或一个系列活动、联动）、project（企划/节目/合作项目）、group（联盟/团体）。\n"
        "4. name 用结果里通行的写法；aliases 写同一事物的其他叫法。summary 一两句话（不超过 160 字），"
        "写清是什么、谁参与、何时；valid_from/valid_until 只在结果明写时填，格式 YYYY-MM-DD。\n"
        "5. 先对照下面的“已知节点”：结果说的事物或人物就是其中某一个（同一件事的简称、繁简体、近音近形写法也算）时，"
        "在 id 填该节点的 id（逐字），name 仍写结果里的写法；确实不在其中才不填 id。同一件事在不同结果里"
        "写法不同，也要用同一个 name。\n"
        "6. participants 是参与者：relation 取 member_of（是某团体/联盟的成员）、founded（发起了它）、"
        "participates_in（参加了该事件/项目）、related_to（有关但不属于以上）；关系方向为“参与者 → 本项”。"
        "参与者同样可填已知节点 id。\n\n"
        f"已知节点：{json.dumps(nodes, ensure_ascii=False)}\n\n"
        f"检索材料：{json.dumps(material, ensure_ascii=False)}\n\n"
        '只输出 JSON：{"items": [{"url", "kind", "id"?, "name", "aliases"?, "summary"?, "valid_from"?, '
        '"valid_until"?, "participants": [{"id"?, "name", "kind", "relation"}]}]}；没有符合的就返回 {"items": []}。')


def _object(raw: str) -> Mapping[str, JsonValue]:
    return cast("Mapping[str, JsonValue]", extract_json_object(raw))


def _text(value: JsonValue, field: str, *, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > limit:
        raise JudgeError(f"{field} must be a non-empty string of at most {limit} characters")
    return value.strip()


def _optional_text(value: JsonValue, field: str, *, limit: int) -> str:
    return "" if value in (None, "") else _text(value, field, limit=limit)


def _date(value: JsonValue) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str) or not _DATE.match(value):
        raise JudgeError("date must be YYYY-MM-DD")
    return value


def _id(value: JsonValue) -> str | None:
    return value if isinstance(value, str) and value else None


def _participant(part: JsonValue) -> Participant:
    if not isinstance(part, dict) or part.get("kind") not in PARTICIPANT_KINDS \
            or part.get("relation") not in RELATIONS:
        raise JudgeError("participant needs a known kind and relation")
    return Participant(_text(part.get("name"), "participant name", limit=MAX_NAME_CHARS),
                       str(part["kind"]), str(part["relation"]), _id(part.get("id")))


def _finding(row: JsonValue, seed_of_url: Mapping[str, str]) -> Finding:
    if not isinstance(row, dict):
        raise JudgeError("item must be an object")
    url = row.get("url")
    if not isinstance(url, str) or url not in seed_of_url:
        raise JudgeError("url is not one of the search results")
    kind = row.get("kind")
    if not isinstance(kind, str) or kind not in EVENT_KINDS:
        raise JudgeError(f"kind {kind!r} is not one of {EVENT_KINDS}")
    participants = row.get("participants")
    if not isinstance(participants, list):
        raise JudgeError("participants must be a list")
    aliases = row.get("aliases") or []
    if not isinstance(aliases, list):
        raise JudgeError("aliases must be a list")
    clean = (a.strip() for a in aliases if isinstance(a, str))
    return Finding(
        url=url, seed=seed_of_url[url], kind=kind, name=_text(row.get("name"), "name", limit=MAX_NAME_CHARS),
        participants=tuple(_participant(p) for p in participants), id=_id(row.get("id")),
        aliases=tuple(dict.fromkeys(a for a in clean if a and len(a) <= MAX_NAME_CHARS))[:MAX_ALIASES],
        summary=_optional_text(row.get("summary"), "summary", limit=MAX_SUMMARY_CHARS),
        valid_from=_date(row.get("valid_from")), valid_until=_date(row.get("valid_until")))


def parse_judgement(raw: str, batches: Batch) -> tuple[list[Finding], Counter[str]]:
    """Findings that satisfy the contract, and a count of rejected items by reason.  A malformed envelope raises."""
    items = _object(raw).get("items")
    if not isinstance(items, list):
        raise JudgeError("items must be a list")
    seed_of_url = {row["url"]: seed for seed, rows in batches for row in rows}
    found: list[Finding] = []
    rejected: Counter[str] = Counter()
    for row in items:
        try:
            found.append(_finding(row, seed_of_url))
        except JudgeError as exc:
            rejected[str(exc)] += 1
    return found, rejected
