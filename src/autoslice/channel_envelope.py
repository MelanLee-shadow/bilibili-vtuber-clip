"""Channel-owned mechanical publish envelope: one definition, two outputs.

Authority: 公开规则：给出格式的检查格式的应该来自同一个定义……两个输出
而已，像这种机械的规则……直接脚本生成就行了 and 「涉及两个主播的都要放。而且
李豆沙在前」; the title/cover skill's 「CP 名或 【李豆沙×对方】，简介附对方空间链接」.

Every mechanical envelope field is produced and checked here only:

* talk-title prefix — ``render_talk_title_prefix(partners)`` builds
 ``【host】`` or ``【host×p1×p2】`` from the selected profile's talk prefix;
 ``parse_talk_title(title)`` returns the partners again and accepts only that
 render's range (host first, U+00D7, partner non-empty without ``【】×`` or edge
 whitespace, no duplicate, never the host itself);
* description related-streamer lines — ``render_publish_description(base,
 partners)`` appends one ``相关主播：<canonical> <homepage>`` line per partner,
 resolved from the profile's structured streamer registry
 (``streamer_registry_sources`` ``reviewed_accounts``); ``parse_publish_description``
 inverts it and accepts only that render's range.

Both outputs take the same structured partner tuple, so ``parse(render(p)) == p``
holds for each, and title and description agree by construction. Models and
people write only the free body; the envelope is rendered by this script.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from src.autoslice.channel_profile import load_channel_profile


REPO_ROOT = Path(__file__).resolve().parents[2]
CHANNEL_PROFILE = load_channel_profile(REPO_ROOT)

ENVELOPE_OPENER = "【"
ENVELOPE_CLOSER = "】"
PAIR_SEPARATOR = "×"
RELATED_STREAMER_LABEL = "相关主播："

_TALK_PREFIX = CHANNEL_PROFILE.talk_title_prefix
_SONG_PREFIX = CHANNEL_PROFILE.song_title_prefix
_BRACKETED = (
    len(_TALK_PREFIX) >= len(ENVELOPE_OPENER) + len(ENVELOPE_CLOSER) + 1
    and _TALK_PREFIX.startswith(ENVELOPE_OPENER)
    and _TALK_PREFIX.endswith(ENVELOPE_CLOSER)
)
HOST_NAME = (
    _TALK_PREFIX[len(ENVELOPE_OPENER) : -len(ENVELOPE_CLOSER)] if _BRACKETED else _TALK_PREFIX
)
_BANNED = re.escape(ENVELOPE_OPENER + ENVELOPE_CLOSER + PAIR_SEPARATOR)
_PARTNER_PATTERN = rf"[^{_BANNED}\s](?:[^{_BANNED}]*[^{_BANNED}\s])?"
_PARTNER_RX = re.compile(_PARTNER_PATTERN)
_TALK_PREFIX_RX = (
    re.compile(
        re.escape(_TALK_PREFIX[: -len(ENVELOPE_CLOSER)])
        + "(?:"
        + re.escape(PAIR_SEPARATOR)
        + _PARTNER_PATTERN
        + ")*"
        + re.escape(ENVELOPE_CLOSER)
    )
    if _BRACKETED
    else re.compile(re.escape(_TALK_PREFIX))
)
_HOMEPAGE_RX = re.compile(r"https://space\.bilibili\.com/[1-9][0-9]{0,31}")


class EnvelopeError(ValueError):
    """A value outside the envelope definition's domain."""


@dataclass(frozen=True)
class TalkTitleEnvelope:
    prefix: str
    partners: tuple[str, ...]
    body: str


@dataclass(frozen=True)
class RelatedStreamer:
    canonical: str
    homepage: str


def _partner_tuple_problem(partners: Sequence[str]) -> str | None:
    values = tuple(partners)
    if values and not _BRACKETED:
        return "TALK_PARTNER_UNSUPPORTED_BY_PROFILE_PREFIX"
    for value in values:
        if not isinstance(value, str) or _PARTNER_RX.fullmatch(value) is None:
            return "TALK_PARTNER_INVALID"
        if value == HOST_NAME:
            return "TALK_PARTNER_IS_HOST"
    if len(set(values)) != len(values):
        return "TALK_PARTNER_DUPLICATE"
    return None


def render_talk_title_prefix(partners: Sequence[str] = ()) -> str:
    """Render the talk prefix for an ordered partner tuple (host always first)."""

    values = tuple(partners)
    problem = _partner_tuple_problem(values)
    if problem is not None:
        raise EnvelopeError(problem)
    if not values:
        return _TALK_PREFIX
    return (
        _TALK_PREFIX[: -len(ENVELOPE_CLOSER)]
        + "".join(PAIR_SEPARATOR + value for value in values)
        + ENVELOPE_CLOSER
    )


def parse_talk_title(title: object) -> TalkTitleEnvelope | None:
    """Invert ``render_talk_title_prefix``; None when ``title`` has no prefix in its range."""

    text = str(title or "")
    match = _TALK_PREFIX_RX.match(text)
    if match is None:
        return None
    prefix = match.group(0)
    inner = prefix[len(_TALK_PREFIX) - len(ENVELOPE_CLOSER) : -len(ENVELOPE_CLOSER)] if _BRACKETED else ""
    partners = tuple(inner.split(PAIR_SEPARATOR)[1:]) if inner else ()
    if _partner_tuple_problem(partners) is not None or render_talk_title_prefix(partners) != prefix:
        return None
    return TalkTitleEnvelope(prefix=prefix, partners=partners, body=text[len(prefix) :])


def accepted_talk_title_prefix(title: object) -> str | None:
    envelope = parse_talk_title(title)
    return envelope.prefix if envelope is not None else None


def talk_title_partners(title: object) -> tuple[str, ...]:
    envelope = parse_talk_title(title)
    return envelope.partners if envelope is not None else ()


def talk_title_body(title: object) -> str:
    """The free body after an accepted talk prefix (the whole text when none)."""

    text = str(title or "")
    envelope = parse_talk_title(text)
    return envelope.body if envelope is not None else text


def title_display_body(title: object) -> str:
    """Body shown on the cover: strip the song catalog prefix or the talk prefix."""

    text = str(title or "").strip()
    if text.startswith(_SONG_PREFIX):
        return text[len(_SONG_PREFIX) :]
    return talk_title_body(text)


def render_talk_title(body: str, partners: Sequence[str] = ()) -> str:
    return render_talk_title_prefix(partners) + str(body)


def ensure_talk_title(title: object, partners: Sequence[str] | None = None) -> str:
    """Generator entry: put the script-rendered prefix on a model/human title.

    ``partners=None`` keeps a declared (already accepted) prefix and only adds
    the plain prefix when none is present — the manual path, where the prefix a
    deliverer wrote is the declaration checked by ``talk_envelope_violations``.
    A partner tuple re-renders the prefix from that structured list, discarding
    any prefix the model wrote.  An invalid tuple falls back to the plain prefix,
    which the checker then reports as ``talk_title_partners_mismatch``.
    """

    stripped = str(title or "").strip()
    envelope = parse_talk_title(stripped)
    if partners is None:
        return stripped if envelope is not None else _TALK_PREFIX + stripped
    body = envelope.body if envelope is not None else stripped
    try:
        return render_talk_title(body, partners)
    except EnvelopeError:
        return _TALK_PREFIX + body


def leading_envelope_preserved(before: object, after: object) -> bool:
    """A reviewed repair keeps the leading 【…】 envelope of the title it repairs."""

    original = str(before or "")
    if not original.startswith(ENVELOPE_OPENER):
        return True
    head = original.split(ENVELOPE_CLOSER, 1)[0] + ENVELOPE_CLOSER
    return isinstance(after, str) and after.startswith(head)


def story_contract_partners(story_contract: object) -> tuple[str, ...]:
    """Structured partners of an automatic clip: confirmed session participants.

    Source: ``session_relation_ledger`` → ``session_relation_authority`` →
    ``StoryContract.participants`` (``build_story_contract``).  Only a CONFIRMED
    relation with ``relation_claim_allowed`` contributes; the host row is the
    prefix itself and never a partner.  Ledger order is kept.
    """

    if not isinstance(story_contract, Mapping):
        return ()
    if (
        story_contract.get("relation_state") != "CONFIRMED"
        or story_contract.get("relation_claim_allowed") is not True
    ):
        return ()
    partners: list[str] = []
    for row in story_contract.get("participants") or []:
        if not isinstance(row, Mapping):
            continue
        if str(row.get("canonical_id") or "") == CHANNEL_PROFILE.profile_id:
            continue
        name = str(row.get("display_name") or "").strip()
        if name and name != HOST_NAME and name not in partners:
            partners.append(name)
    return tuple(partners)


def load_related_streamers(path: Path | None = None) -> dict[str, RelatedStreamer]:
    """Structured partner registry: reviewed accounts with an official homepage."""

    from src.autoslice.streamer_registry_crawler import (
        load_source_config,
        reviewed_account_records,
    )

    config = load_source_config(path or CHANNEL_PROFILE.asset_file("streamer_registry_sources"))
    registry: dict[str, RelatedStreamer] = {}
    for row in reviewed_account_records(config):
        canonical = str(row["canonical"])
        homepage = str(row["homepage"])
        if _PARTNER_RX.fullmatch(canonical) is None or _HOMEPAGE_RX.fullmatch(homepage) is None:
            continue
        registry.setdefault(canonical, RelatedStreamer(canonical=canonical, homepage=homepage))
    return registry


def resolve_partners(
    partners: Sequence[str], *, registry: Mapping[str, RelatedStreamer] | None = None
) -> tuple[RelatedStreamer, ...]:
    values = tuple(partners)
    problem = _partner_tuple_problem(values)
    if problem is not None:
        raise EnvelopeError(problem)
    if not values:
        return ()
    known = load_related_streamers() if registry is None else registry
    missing = [value for value in values if value not in known]
    if missing:
        raise EnvelopeError("TALK_PARTNER_UNRESOLVED:" + ",".join(missing))
    return tuple(known[value] for value in values)


def render_related_streamer_lines(
    partners: Sequence[str], *, registry: Mapping[str, RelatedStreamer] | None = None
) -> tuple[str, ...]:
    return tuple(
        f"{RELATED_STREAMER_LABEL}{row.canonical} {row.homepage}"
        for row in resolve_partners(partners, registry=registry)
    )


def render_publish_description(
    base: str,
    partners: Sequence[str] = (),
    *,
    registry: Mapping[str, RelatedStreamer] | None = None,
) -> str:
    """Channel description plus one related-streamer line per partner."""

    lines = render_related_streamer_lines(partners, registry=registry)
    return "\n".join((base, *lines)) if lines else base


def publish_description_for_title(
    base: str, title: object, *, registry: Mapping[str, RelatedStreamer] | None = None
) -> str:
    """The description whose related-streamer lines come from the title's partners."""

    return render_publish_description(base, talk_title_partners(str(title or "").strip()), registry=registry)


def parse_publish_description(
    description: object, *, registry: Mapping[str, RelatedStreamer] | None = None
) -> tuple[str, tuple[str, ...]] | None:
    """Invert ``render_publish_description``: (base, partners) or None."""

    text = str(description if description is not None else "")
    lines = text.split("\n")
    partners: list[str] = []
    while len(lines) > 1 and lines[-1].startswith(RELATED_STREAMER_LABEL):
        rest = lines.pop()[len(RELATED_STREAMER_LABEL) :]
        name, separator, _homepage = rest.rpartition(" ")
        if not separator:
            return None
        partners.insert(0, name)
    if any(line.startswith(RELATED_STREAMER_LABEL) for line in lines):
        return None
    base = "\n".join(lines)
    try:
        rendered = render_publish_description(base, partners, registry=registry)
    except EnvelopeError:
        return None
    return (base, tuple(partners)) if rendered == text else None


def talk_envelope_violations(
    title: object,
    *,
    expected_partners: Sequence[str] | None = None,
    registry: Mapping[str, RelatedStreamer] | None = None,
) -> list[str]:
    """Check one talk title against the structured partner list.

    ``expected_partners`` is the generator's structured list (automatic clips);
    ``None`` means the title's own accepted prefix is the deliverer's explicit
    declaration (manual/reaction entries).  Every partner must resolve to the
    structured registry so the description can render its homepage link.
    The title gate reports a missing/malformed prefix separately.
    """

    envelope = parse_talk_title(str(title or "").strip())
    if envelope is None:
        return []
    violations: list[str] = []
    if expected_partners is not None and envelope.partners != tuple(expected_partners):
        violations.append("talk_title_partners_mismatch")
    try:
        resolve_partners(envelope.partners, registry=registry)
    except EnvelopeError:
        violations.append("talk_partner_unresolved")
    return violations
