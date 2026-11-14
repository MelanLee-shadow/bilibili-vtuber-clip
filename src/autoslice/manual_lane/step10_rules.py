"""Step 10's numbers and the authorization words, from lane_rules.json `upload` (see its `_doc`).

The authorization the upload tool wants (who authorized, in which words) is not typed per clip: it is the channel owner's rule,
written once in lane_rules.json.  A value that is not filled in stops step 10 before any command (UPLOAD_RULES_UNSET), so a
checkout with the placeholders can never publish."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .lane_rules import rules

RULES_UNSET = "UPLOAD_RULES_UNSET"


class UploadRulesUnset(RuntimeError):
    """A value step 10 needs is missing or empty in lane_rules.json `upload`; the message names which."""


@dataclass(frozen=True)
class Authorization:
    by: str
    quote: str
    rule_date: str


@dataclass(frozen=True)
class UploadRules:
    first_publication: Authorization
    revision: Authorization
    poll_interval_s: float
    max_polls: int
    quota_retry_s: float


def _text(doc: Mapping[str, Any], *path: str) -> str:
    value: Any = doc
    for key in path:
        value = value.get(key) if isinstance(value, Mapping) else None
    if not isinstance(value, str) or not value.strip():
        raise UploadRulesUnset(f"lane_rules.json upload.{'.'.join(path)} 未填（授权人、授权原话、规则日期都由规则给出，不在每条片上手填）")
    return value.strip()


def _number(doc: Mapping[str, Any], *path: str) -> float:
    value: Any = doc
    for key in path:
        value = value.get(key) if isinstance(value, Mapping) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise UploadRulesUnset(f"lane_rules.json upload.{'.'.join(path)} 不是非负数：{value!r}")
    return float(value)


def load() -> UploadRules:
    try:
        doc: Mapping[str, Any] = rules("upload")
    except KeyError as exc:
        raise UploadRulesUnset("lane_rules.json 没有 upload 一节") from exc
    by = _text(doc, "authorization", "authorized_by")

    def authorization(kind: str) -> Authorization:
        return Authorization(by=by, quote=_text(doc, "authorization", kind, "quote"),
                             rule_date=_text(doc, "authorization", kind, "rule_date"))

    polls = int(_number(doc, "wait", "max_polls"))
    if polls < 1:
        raise UploadRulesUnset("lane_rules.json upload.wait.max_polls 至少为 1")
    return UploadRules(first_publication=authorization("first_publication"), revision=authorization("revision"),
                       poll_interval_s=_number(doc, "wait", "poll_interval_s"), max_polls=polls,
                       quota_retry_s=_number(doc, "quota", "retry_after_s"))
