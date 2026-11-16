"""The channel's core draws: which of them a candidate's main line is about, and the points the views give them.

公开规则：你现在不能按照过去的内容构建新的评分标准吗，类似PCA。按照播放量来解释各个要素。特别是我说过了，百合，
关系，撒娇，CP，联动，这是她的核心看点…核心看点播放量自然就高. The draws and their points are the channel's asset
(assets/<profile>/core_draws.json), fitted on the account's published clips: points = how many times the views double
when the main line is on that draw (month-adjusted, shrunk by how sure the fit is) × `points_per_doubling`. A channel
without the asset has no draws and nothing here changes its selection.

The model only says which draws a candidate's main line is on (one call per recording, all its candidates at once); the
points are read from the asset at seating time, so a refit changes the seating of kept recalls without a new call.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from functools import lru_cache
from typing import Any

from . import channel

ASSET = "core_draws.json"
SCHEMA = "core-draws.v1"
TEXT_LIMIT = 1500                     # characters of a candidate's subtitles in the labelling prompt


class DrawsError(RuntimeError):
    """The labelling answer is not usable (the recording is left for the next run, like a failed recall call)."""


@lru_cache(maxsize=1)
def load() -> dict[str, Any] | None:
    path = channel.asset(ASSET)
    if not path.exists():
        return None
    doc = json.loads(path.read_text(encoding="utf-8"))
    draws = doc.get("draws")
    if (doc.get("schema") != SCHEMA or not isinstance(doc.get("version"), str) or not isinstance(draws, list) or not draws
            or not all(isinstance(d, dict) and isinstance(d.get("key"), str) and isinstance(d.get("points"), (int, float))
                       and isinstance(d.get("definition"), str) and isinstance(d.get("name"), str) for d in draws)
            or len({d["key"] for d in draws}) != len(draws) or not isinstance(doc.get("max_points"), (int, float))):
        raise DrawsError(f"{path}: schema {SCHEMA}, version, max_points, draws[{{key, name, definition, points}}] with distinct keys")
    return doc


def points(entry: Mapping[str, Any], asset: Mapping[str, Any] | None = None) -> float:
    """The draw points of a labelled candidate, at most the asset's `max_points` (draws stacked on one clip do not keep
    adding views); 0 when the channel has no draws or the candidate is not labelled."""
    asset = load() if asset is None else asset
    label = entry.get("core_draws")
    if asset is None or not isinstance(label, Mapping):
        return 0.0
    table = {d["key"]: float(d["points"]) for d in asset["draws"]}
    return round(min(float(asset["max_points"]), sum(table.get(key, 0.0) for key in label.get("draws", []))), 2)


def prompt(entries: Sequence[Mapping[str, Any]], asset: Mapping[str, Any]) -> str:
    lines = [f"- {d['key']}（{d['name']}）：{d['definition']}" for d in asset["draws"]]
    items = [{"id": e["candidate_id"], "hook": str(e.get("hook", "")), "text": str(e.get("text", ""))[:TEXT_LIMIT]} for e in entries]
    return (
        "下面是一场直播里召回的切片候选（每条给了选片理由和字幕）。请判断每条的主线落在哪些看点上。\n"
        "只算主线：片子大部分在讲它、或它就是片子的笑点/落点；只顺带提一句、或只出现一个名字的不算。一条可以没有看点，也可以有几个。\n"
        "看点（key：定义）：\n" + "\n".join(lines) + "\n\n候选：\n" + json.dumps(items, ensure_ascii=False, indent=1) +
        '\n\n只输出 JSON：{"labels": [{"id": "候选 id", "draws": ["key", ...], "why": "一句话：主线是什么、为什么是这些看点"}]}，每个候选恰好一条。'
    )


def parse(raw: str, ids: Sequence[str], asset: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    match = re.search(r"\{.*\}", raw, re.S)
    try:
        doc = json.loads(match.group(0) if match else raw)
    except json.JSONDecodeError as exc:
        raise DrawsError(f"not JSON: {exc}") from exc
    keys = {d["key"] for d in asset["draws"]}
    out: dict[str, dict[str, Any]] = {}
    for row in doc.get("labels", []) if isinstance(doc, dict) else []:
        cid, draws = row.get("id") if isinstance(row, dict) else None, row.get("draws") if isinstance(row, dict) else None
        if cid not in ids or cid in out or not isinstance(draws, list) or not all(isinstance(d, str) and d in keys for d in draws):
            raise DrawsError(f"bad label row {str(row)[:200]}")
        out[cid] = {"version": asset["version"], "draws": sorted(set(draws)), "why": str(row.get("why", "")).strip()[:300]}
    if set(out) != set(ids):
        raise DrawsError(f"labels for {sorted(set(ids) - set(out))} missing")
    return out


def label(pool: list[dict[str, Any]], ask: Callable[[str], str]) -> bool:
    """Label, in place, the candidates of `pool` that have no label of the asset's version.  Returns whether any changed."""
    asset = load()
    if asset is None:
        return False
    todo = [e for e in pool if (e.get("core_draws") or {}).get("version") != asset["version"]]
    if not todo:
        return False
    labels = parse(ask(prompt(todo, asset)), [e["candidate_id"] for e in todo], asset)
    for entry in todo:
        entry["core_draws"] = labels[entry["candidate_id"]]
    return True
