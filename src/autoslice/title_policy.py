"""Profile-owned deterministic title gates and selection-hook binding."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Mapping

from src.autoslice.channel_profile import load_channel_profile


TITLE_POLICY_SCHEMA = "vtuber-slice.title-policy.v1"
REPO_ROOT = Path(__file__).resolve().parents[2]
CHANNEL_PROFILE = load_channel_profile(REPO_ROOT)


class TitlePolicyError(ValueError):
    pass


def _load_title_policy() -> Mapping[str, object]:
    path = CHANNEL_PROFILE.asset_file("title_policy")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TitlePolicyError(f"cannot read title policy {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise TitlePolicyError("title policy must be an object")
    expected = {
        "schema_version",
        "banned_hype_words",
        "suffix_only_hype_words",
        "banned_filler_words",
        "banned_regexes",
        "min_length",
        "max_length",
        "max_attempts",
        "generic_hook_words",
        "meaningless_particles",
    }
    if set(payload) != expected:
        raise TitlePolicyError(
            "title policy keys mismatch: "
            f"missing={sorted(expected - set(payload))}, "
            f"unknown={sorted(set(payload) - expected)}"
        )
    if payload.get("schema_version") != TITLE_POLICY_SCHEMA:
        raise TitlePolicyError(
            f"unsupported title policy schema {payload.get('schema_version')!r}"
        )
    return payload


def _strings(payload: Mapping[str, object], key: str) -> tuple[str, ...]:
    values = payload.get(key)
    if not isinstance(values, list) or any(
        not isinstance(value, str) or not value for value in values
    ):
        raise TitlePolicyError(f"title policy {key} must be a non-empty string list")
    if len(values) != len(set(values)):
        raise TitlePolicyError(f"title policy {key} must not contain duplicates")
    return tuple(values)


def _positive_int(payload: Mapping[str, object], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise TitlePolicyError(f"title policy {key} must be a positive integer")
    return value


_POLICY = _load_title_policy()
_LIDOUSHA_TITLE_PREFIX = CHANNEL_PROFILE.talk_title_prefix
_TITLE_BANNED_HYPE_WORDS = _strings(_POLICY, "banned_hype_words")
_TITLE_SUFFIX_ONLY_HYPE_WORDS = _strings(_POLICY, "suffix_only_hype_words")
_TITLE_BANNED_FILLER_WORDS = _strings(_POLICY, "banned_filler_words")
_TITLE_BANNED_REGEXES = tuple(
    re.compile(pattern) for pattern in _strings(_POLICY, "banned_regexes")
)
# Backwards-compatible name used by existing callers/tests.  The asset can add
# further patterns without forcing the shadow runner to know about them.
_TITLE_BANNED_MIAO_RE = _TITLE_BANNED_REGEXES[0]
_TITLE_BANNED_SUFFIX_RE = re.compile(
    "到(?:"
    + "|".join(_TITLE_BANNED_HYPE_WORDS + _TITLE_SUFFIX_ONLY_HYPE_WORDS)
    + ")"
)
_TITLE_MIN_LEN = _positive_int(_POLICY, "min_length")
_TITLE_MAX_LEN = _positive_int(_POLICY, "max_length")
_TITLE_MAX_ATTEMPTS = _positive_int(_POLICY, "max_attempts")
if _TITLE_MIN_LEN > _TITLE_MAX_LEN:
    raise TitlePolicyError("title policy min_length exceeds max_length")
_SELECTION_HOOK_GENERIC_WORDS = (
    CHANNEL_PROFILE.display_name,
    CHANNEL_PROFILE.short_name,
    *_strings(_POLICY, "generic_hook_words"),
)
_SELECTION_HOOK_GENERIC_ANCHORS = set(_SELECTION_HOOK_GENERIC_WORDS)
_SELECTION_HOOK_MEANINGLESS_RE = re.compile(
    "(?:"
    + "|".join(
        re.escape(value)
        for value in (
            *_SELECTION_HOOK_GENERIC_WORDS,
            *_strings(_POLICY, "meaningless_particles"),
        )
    )
    + ")"
)


def _title_policy_violations(title: str) -> list[str]:
    """Return deterministic policy codes tripped by an automatic title."""

    violations: list[str] = []
    if _TITLE_BANNED_SUFFIX_RE.search(title):
        violations.append("banned_universal_suffix")
    if any(word in title for word in _TITLE_BANNED_HYPE_WORDS):
        violations.append("banned_hype_word")
    if any(word in title for word in _TITLE_BANNED_FILLER_WORDS):
        violations.append("banned_filler_word")
    if any(pattern.search(title) for pattern in _TITLE_BANNED_REGEXES):
        violations.append("banned_filler_word")
    return violations


_SONG_NAME_IN_TITLE_RX = re.compile(r"《([^《》]{1,80})》")


def canonicalize_song_catalog_title(title: str) -> str:
    """Collapse an automatic song title to the fixed catalog form.

    Ivan 2026-07-14 / 2026-07-19 铁律：歌切标题 = 歌切前缀 + 《歌名》，前缀与
    《歌名》之间、《歌名》之后都不允许任何字符（含「｜副标题」/hook 尾巴）。
    这是所有自动标题的最终 choke point——无论标题来自 LLM、兜底模板还是恢复
    路径，只要带歌切前缀就在这里折叠定形。谈话标题与不含《歌名》的标题原样
    通过；Ivan 手定标题（title_llm_call=None）不经过本函数。
    """

    stripped = title.strip()
    prefix = CHANNEL_PROFILE.song_title_prefix
    if not stripped.startswith(prefix):
        return stripped
    match = _SONG_NAME_IN_TITLE_RX.search(stripped[len(prefix):])
    if match is None:
        return stripped
    return f"{prefix}《{match.group(1)}》"


def _ensure_lidousha_prefix(title: str) -> str:
    """Guarantee the selected profile's publish prefix on an automatic title."""

    stripped = title.strip()
    return (
        stripped
        if stripped.startswith(_LIDOUSHA_TITLE_PREFIX)
        else _LIDOUSHA_TITLE_PREFIX + stripped
    )


def _selection_hook_first_clause(selection_hook: str | None) -> str:
    return re.split(
        r"[，,。.!！?？；;：:\n…]",
        str(selection_hook or "").strip(),
        maxsplit=1,
    )[0].strip()


def _selection_hook_anchor_valid(
    *, anchor: object, selection_hook: str, title: str
) -> bool:
    """Require an automatic title to retain a concrete main-event phrase."""

    if not isinstance(anchor, str):
        return False
    anchor = anchor.strip()
    first_clause = _selection_hook_first_clause(selection_hook)
    title_body = str(title).removeprefix(_LIDOUSHA_TITLE_PREFIX).strip()
    title_lead_clause = re.split(
        r"[，,。.!！?？；;：:\n…]", title_body, maxsplit=1
    )[0].strip()
    meaningful = _SELECTION_HOOK_MEANINGLESS_RE.sub("", anchor).strip()
    return bool(
        2 <= len(anchor) <= 12
        and anchor not in _SELECTION_HOOK_GENERIC_ANCHORS
        and len(meaningful) >= 2
        and anchor in first_clause
        and anchor in title_lead_clause
        and title_lead_clause.find(anchor) <= 10
    )


def _selection_hook_fallback_title(selection_hook: str | None) -> str | None:
    """Build a conservative source-bound title after all model attempts fail."""

    raw = str(selection_hook or "").strip().rstrip("。；; ")
    if not raw:
        return None
    clauses = [
        part.strip()
        for part in re.split(r"[，,。；;：:\n…]", raw)
        if part.strip()
    ]
    if not clauses:
        return None
    first = clauses[0].replace(
        CHANNEL_PROFILE.display_name, CHANNEL_PROFILE.short_name
    )
    body = first
    if len(clauses) > 1:
        second = clauses[1].replace(
            CHANNEL_PROFILE.display_name, CHANNEL_PROFILE.short_name
        )
        if second.startswith("她"):
            second = "结果" + second[1:]
        candidate = f"{first}，{second}"
        if len(_ensure_lidousha_prefix(candidate)) <= _TITLE_MAX_LEN:
            body = candidate
    available = _TITLE_MAX_LEN - len(_LIDOUSHA_TITLE_PREFIX)
    body = body[:available].rstrip("，,、；;：: ")
    title = _ensure_lidousha_prefix(body)
    return title if _TITLE_MIN_LEN <= len(title) <= _TITLE_MAX_LEN else None
