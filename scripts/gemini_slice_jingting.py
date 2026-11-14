#!/usr/bin/env python3
"""
Channel-profile-aware slice fine-transcription runner.

Runs on the media host. Full-recording subtitles still drive rough semantic
slicing; this script refines final slice sidecars only and writes a separate
`<slice>.jingting.srt` plus a hash/log manifest.

Providers:
  - gemini: direct Gemini API, audio + draft SRT -> corrected SRT.
  - agy: Antigravity CLI on the free host, local media + draft SRT -> output.srt.

Examples:
  gemini_slice_jingting.py --provider agy /path/to/slice.flv
  gemini_slice_jingting.py --provider agy --once --room ROOM_ID
  gemini_slice_jingting.py --provider agy --daemon --room ROOM_ID
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.autoslice.channel_profile import load_channel_profile

CHANNEL_PROFILE = load_channel_profile(REPO_ROOT)


_GLOSSARY_ENV = os.environ.get("AUTOSLICE_GLOSSARY") or os.environ.get("LIDOUSHA_GLOSSARY")
# Repo-vendored glossary is the source of truth (sync it to free with
# scripts/sync_profile_assets.sh); host paths keep the production daemon
# working.  Without the repo path the local pipeline silently ran jingting
# with an EMPTY glossary (the 小寺/小室-class name errors).
_REPO_GLOSSARY = str(CHANNEL_PROFILE.asset_file("glossary", repo_root=REPO_ROOT))
GLOSSARY_PATHS = [_GLOSSARY_ENV] if _GLOSSARY_ENV else [_REPO_GLOSSARY]
if not _GLOSSARY_ENV and CHANNEL_PROFILE.profile_id == "lidousha":
    GLOSSARY_PATHS.extend(
        ["/opt/bilive/app/lidousha_glossary.txt", "/app/lidousha_glossary.txt"]
    )
# Subtitle correction PRINCIPLES (the "how to correct" rules) live in a single
# authoritative file; glossary.txt is now the TERM canon only.  glossary()
# concatenates both so every correction prompt gets the full principle set from
# one source — edit principles in one place, all prompts stay in sync.
_PRINCIPLES_ENV = os.environ.get("AUTOSLICE_SUBTITLE_PRINCIPLES") or os.environ.get(
    "LIDOUSHA_SUBTITLE_PRINCIPLES"
)
_REPO_PRINCIPLES = str(
    CHANNEL_PROFILE.asset_file("subtitle_correction_principles", repo_root=REPO_ROOT)
)
PRINCIPLES_PATHS = [_PRINCIPLES_ENV] if _PRINCIPLES_ENV else [_REPO_PRINCIPLES]
if not _PRINCIPLES_ENV and CHANNEL_PROFILE.profile_id == "lidousha":
    PRINCIPLES_PATHS.extend(
        [
            "/opt/bilive/app/lidousha_subtitle_principles.md",
            "/app/lidousha_subtitle_principles.md",
        ]
    )
_TIMELY_TERMS_ENV = os.environ.get("AUTOSLICE_TIMELY_TERMS") or os.environ.get(
    "LIDOUSHA_TIMELY_TERMS"
)
_REPO_TIMELY_TERMS = str(CHANNEL_PROFILE.asset_file("timely_terms", repo_root=REPO_ROOT))
TIMELY_TERMS_PATHS = [_TIMELY_TERMS_ENV] if _TIMELY_TERMS_ENV else [_REPO_TIMELY_TERMS]
if not _TIMELY_TERMS_ENV and CHANNEL_PROFILE.profile_id == "lidousha":
    TIMELY_TERMS_PATHS.extend(
        [
            "/opt/bilive/app/lidousha_timely_terms.json",
            "/app/lidousha_timely_terms.json",
        ]
    )


TIMELY_TERMS_SCHEMA = "lidousha-timely-terms.v1"
TIMELY_TERMS_SOURCE_HOSTS = frozenset(
    {
        "anilist.co",
        "animenewsnetwork.com",
        "bang-dream.com",
        "bgm.tv",
        "bilibili.com",
        "bushiroad.com",
        "tv-tokyo.co.jp",
    }
)
TIMELY_TERMS_MAX_BYTES = 512 * 1024
TIMELY_TERMS_MAX_COUNT = 256
TIMELY_TERMS_PROMPT_MAX_COUNT = 64
_TIMELY_TERM_ATOM_MAX_CHARS = 64
_TIMELY_TERM_ATOM_PUNCTUATION = frozenset(" !！?？&+＋-_/・·.．()（）∞'")
_TIMELY_TERM_INSTRUCTION_RX = re.compile(
    r"(?i)(?:\bignore\b.{0,24}\binstructions?\b|"
    r"\b(?:system|developer)\b.{0,24}\bprompt\b|"
    r"\bprevious\b.{0,24}\binstructions?\b|"
    r"忽略.{0,12}(?:指令|提示)|系统提示|执行.{0,12}命令|调用.{0,12}工具)"
)


class TimelyTermsValidationError(ValueError):
    """The curated timely-term snapshot is not safe to use as prompt data."""


def _read_first(paths) -> str:
    for path in paths:
        if not path:
            continue
        try:
            return Path(path).read_text(encoding="utf-8")
        except OSError:
            continue
    return ""


def _strict_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise TimelyTermsValidationError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise TimelyTermsValidationError(f"non-finite JSON value is forbidden: {value}")


def _require_exact_fields(
    value: object,
    *,
    required: frozenset[str],
    optional: frozenset[str] = frozenset(),
    label: str,
) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TimelyTermsValidationError(f"{label} must be a JSON object")
    if any(not isinstance(key, str) for key in value):
        raise TimelyTermsValidationError(f"{label} field names must be strings")
    keys = set(value)
    missing = required - keys
    unknown = keys - required - optional
    if missing:
        raise TimelyTermsValidationError(f"{label} missing fields: {sorted(missing)}")
    if unknown:
        raise TimelyTermsValidationError(f"{label} has unknown fields: {sorted(unknown)}")
    return value


def _require_metadata_text(value: object, *, label: str, max_chars: int) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise TimelyTermsValidationError(f"{label} must be a non-empty trimmed string")
    if len(value) > max_chars:
        raise TimelyTermsValidationError(f"{label} exceeds {max_chars} characters")
    if any(unicodedata.category(char).startswith("C") or char in "\r\n" for char in value):
        raise TimelyTermsValidationError(f"{label} contains control characters")
    return value


def _require_term_atom(value: object, *, label: str) -> str:
    text = _require_metadata_text(
        value,
        label=label,
        max_chars=_TIMELY_TERM_ATOM_MAX_CHARS,
    )
    if "  " in text:
        raise TimelyTermsValidationError(f"{label} contains repeated whitespace")
    if _TIMELY_TERM_INSTRUCTION_RX.search(text):
        raise TimelyTermsValidationError(f"{label} resembles an instruction, not an entity name")
    for char in text:
        category = unicodedata.category(char)
        if category[:1] not in {"L", "M", "N"} and char not in _TIMELY_TERM_ATOM_PUNCTUATION:
            raise TimelyTermsValidationError(
                f"{label} contains a character outside the term-data allowlist: {char!r}"
            )
    return text


def _require_term_atom_list(
    value: object,
    *,
    label: str,
    min_items: int = 0,
    max_items: int = 16,
) -> list[str]:
    if not isinstance(value, list) or not min_items <= len(value) <= max_items:
        raise TimelyTermsValidationError(
            f"{label} must contain between {min_items} and {max_items} strings"
        )
    result = [_require_term_atom(item, label=f"{label}[{index}]") for index, item in enumerate(value)]
    if len(set(result)) != len(result):
        raise TimelyTermsValidationError(f"{label} contains duplicate values")
    return result


def _timely_term_identity_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(
        char
        for char in normalized
        if unicodedata.category(char)[:1] in {"L", "M", "N"}
    )


def _require_iso_date(value: object, *, label: str) -> dt.date:
    text = _require_metadata_text(value, label=label, max_chars=10)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        raise TimelyTermsValidationError(f"{label} must use YYYY-MM-DD")
    try:
        parsed = dt.date.fromisoformat(text)
    except ValueError as exc:
        raise TimelyTermsValidationError(f"{label} is not a valid date") from exc
    if parsed.isoformat() != text:
        raise TimelyTermsValidationError(f"{label} is not a canonical ISO date")
    return parsed


def _require_timestamp(value: object, *, label: str) -> dt.datetime:
    text = _require_metadata_text(value, label=label, max_chars=40)
    if not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})",
        text,
    ):
        raise TimelyTermsValidationError(f"{label} must be an ISO timestamp with timezone")
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TimelyTermsValidationError(f"{label} is not a valid timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise TimelyTermsValidationError(f"{label} must include a timezone")
    return parsed


def _require_official_source_url(value: object, *, label: str) -> str:
    url = _require_metadata_text(value, label=label, max_chars=512)
    if any(char.isspace() for char in url):
        raise TimelyTermsValidationError(f"{label} must not contain whitespace")
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise TimelyTermsValidationError(f"{label} is not a valid URL") from exc
    if parsed.scheme != "https" or not parsed.hostname:
        raise TimelyTermsValidationError(f"{label} must use HTTPS")
    if parsed.username is not None or parsed.password is not None or port is not None:
        raise TimelyTermsValidationError(f"{label} must not contain credentials or a port")
    if parsed.query or parsed.fragment:
        raise TimelyTermsValidationError(f"{label} must be a stable URL without query or fragment")
    if not parsed.path.startswith("/") or parsed.path == "/":
        raise TimelyTermsValidationError(f"{label} must identify a specific official page")
    try:
        host = parsed.hostname.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise TimelyTermsValidationError(f"{label} has an invalid hostname") from exc
    if not any(
        host == allowed or host.endswith("." + allowed)
        for allowed in TIMELY_TERMS_SOURCE_HOSTS
    ):
        raise TimelyTermsValidationError(f"{label} host is not on the source allowlist")
    decoded_path = urllib.parse.unquote(parsed.path)
    if any(char in decoded_path for char in "\\<>\r\n\t"):
        raise TimelyTermsValidationError(f"{label} contains unsafe path characters")
    return url


def validate_timely_terms_payload(payload: object) -> dict[str, object]:
    """Validate and normalize one manually curated, source-backed snapshot."""

    document = _require_exact_fields(
        payload,
        required=frozenset(
            {"schema_version", "generated_at", "expires_at", "status", "terms"}
        ),
        label="snapshot",
    )
    if document["schema_version"] != TIMELY_TERMS_SCHEMA:
        raise TimelyTermsValidationError("snapshot schema_version is unsupported")
    if document["status"] != "fresh":
        raise TimelyTermsValidationError("snapshot status must be 'fresh'")
    generated_at = _require_timestamp(document["generated_at"], label="generated_at")
    expires_at = _require_timestamp(document["expires_at"], label="expires_at")
    if generated_at >= expires_at:
        raise TimelyTermsValidationError("expires_at must be later than generated_at")

    raw_terms = document["terms"]
    if not isinstance(raw_terms, list) or not 1 <= len(raw_terms) <= TIMELY_TERMS_MAX_COUNT:
        raise TimelyTermsValidationError(
            f"terms must contain between 1 and {TIMELY_TERMS_MAX_COUNT} entries"
        )
    terms: list[dict[str, object]] = []
    canonical_owners: dict[str, int] = {}
    for index, raw_term in enumerate(raw_terms):
        label = f"terms[{index}]"
        term = _require_exact_fields(
            raw_term,
            required=frozenset(
                {
                    "canonical",
                    "readings",
                    "aliases",
                    "confusables",
                    "topic_entities",
                    "active_from",
                    "active_until",
                    "sources",
                }
            ),
            optional=frozenset({"display_name", "reason"}),
            label=label,
        )
        canonical = _require_term_atom(term["canonical"], label=f"{label}.canonical")
        canonical_key = _timely_term_identity_key(canonical)
        if canonical_key in canonical_owners:
            raise TimelyTermsValidationError("canonical timely terms must be unique")
        canonical_owners[canonical_key] = index
        active_from = _require_iso_date(term["active_from"], label=f"{label}.active_from")
        active_until = _require_iso_date(term["active_until"], label=f"{label}.active_until")
        if active_from > active_until:
            raise TimelyTermsValidationError(f"{label} active window is reversed")

        raw_sources = term["sources"]
        if not isinstance(raw_sources, list) or not 1 <= len(raw_sources) <= 8:
            raise TimelyTermsValidationError(f"{label}.sources must contain between 1 and 8 entries")
        sources: list[dict[str, str]] = []
        source_urls: set[str] = set()
        for source_index, raw_source in enumerate(raw_sources):
            source_label = f"{label}.sources[{source_index}]"
            source = _require_exact_fields(
                raw_source,
                required=frozenset({"url", "published_at", "publisher"}),
                label=source_label,
            )
            url = _require_official_source_url(source["url"], label=f"{source_label}.url")
            if url in source_urls:
                raise TimelyTermsValidationError(f"{label}.sources contains duplicate URLs")
            source_urls.add(url)
            published_at = _require_iso_date(
                source["published_at"], label=f"{source_label}.published_at"
            )
            publisher = _require_metadata_text(
                source["publisher"], label=f"{source_label}.publisher", max_chars=128
            )
            sources.append(
                {
                    "url": url,
                    "published_at": published_at.isoformat(),
                    "publisher": publisher,
                }
            )

        normalized: dict[str, object] = {
            "canonical": canonical,
            "readings": _require_term_atom_list(
                term["readings"], label=f"{label}.readings", min_items=1, max_items=12
            ),
            "aliases": _require_term_atom_list(term["aliases"], label=f"{label}.aliases"),
            "confusables": _require_term_atom_list(
                term["confusables"], label=f"{label}.confusables"
            ),
            "topic_entities": _require_term_atom_list(
                term["topic_entities"], label=f"{label}.topic_entities"
            ),
            "active_from": active_from.isoformat(),
            "active_until": active_until.isoformat(),
            "sources": sources,
        }
        if "display_name" in term:
            normalized["display_name"] = _require_term_atom(
                term["display_name"], label=f"{label}.display_name"
            )
        if "reason" in term:
            normalized["reason"] = _require_metadata_text(
                term["reason"], label=f"{label}.reason", max_chars=512
            )
        terms.append(normalized)

    # A canonical duplicated as another term's alias/reading is the same ambiguous
    # entity split that the crawler is required to merge.  Fail closed here as
    # well so manually supplied or stale snapshots cannot bypass that invariant.
    for index, term in enumerate(terms):
        for surface in [*term["aliases"], *term["readings"]]:
            surface_key = _timely_term_identity_key(surface)
            owner = canonical_owners.get(surface_key)
            if owner is not None and owner != index:
                raise TimelyTermsValidationError(
                    "canonical timely term conflicts with another term alias or reading"
                )

    return {
        "schema_version": TIMELY_TERMS_SCHEMA,
        "generated_at": str(document["generated_at"]),
        "expires_at": str(document["expires_at"]),
        "status": "fresh",
        "terms": terms,
    }


def validate_timely_terms_json(raw: str) -> dict[str, object]:
    if not isinstance(raw, str):
        raise TimelyTermsValidationError("snapshot must be UTF-8 JSON text")
    if len(raw.encode("utf-8")) > TIMELY_TERMS_MAX_BYTES:
        raise TimelyTermsValidationError("snapshot exceeds the maximum byte size")
    try:
        payload = json.loads(
            raw,
            object_pairs_hook=_strict_json_object,
            parse_constant=_reject_json_constant,
        )
    except TimelyTermsValidationError:
        raise
    except (TypeError, ValueError) as exc:
        raise TimelyTermsValidationError(f"snapshot is not strict JSON: {exc}") from exc
    return validate_timely_terms_payload(payload)


def load_validated_timely_terms_snapshot(path: str | Path) -> dict[str, object]:
    snapshot_path = Path(path)
    try:
        if snapshot_path.stat().st_size > TIMELY_TERMS_MAX_BYTES:
            raise TimelyTermsValidationError("snapshot exceeds the maximum byte size")
        raw = snapshot_path.read_text(encoding="utf-8")
    except UnicodeError as exc:
        raise TimelyTermsValidationError("snapshot must be UTF-8") from exc
    return validate_timely_terms_json(raw)


def approved_timely_terms(*, as_of: dt.datetime | None = None) -> list[dict[str, object]]:
    """Structured, date-bounded, source-verified timely terms.

    This is the same gated read/validate/expiry/active-window filter
    ``timely_terms_context`` renders into a prompt string, exposed as data so
    other deterministic (non-LLM) consumers, e.g. term-boundary unification,
    can reuse the exact same blind-mode-safe loader instead of re-parsing
    ``TIMELY_TERMS_PATHS`` themselves.
    """

    if (
        os.environ.get("AUTOSLICE_DISABLE_TIMELY_TERMS") == "1"
        or os.environ.get("LIDOUSHA_DISABLE_TIMELY_TERMS") == "1"
    ):
        return []
    raw = _read_first(TIMELY_TERMS_PATHS)
    if not raw:
        return []
    expected_sha256 = (
        os.environ.get("AUTOSLICE_TIMELY_TERMS_SHA256")
        or os.environ.get("LIDOUSHA_TIMELY_TERMS_SHA256", "")
    ).removeprefix("sha256:").lower()
    if expected_sha256:
        if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
            return []
        if hashlib.sha256(raw.encode("utf-8")).hexdigest() != expected_sha256:
            return []
    try:
        payload = validate_timely_terms_json(raw)
    except TimelyTermsValidationError:
        return []
    term_as_of = os.environ.get("AUTOSLICE_TERM_AS_OF") or os.environ.get(
        "LIDOUSHA_TERM_AS_OF"
    )
    if as_of is None and term_as_of:
        try:
            as_of_date = dt.date.fromisoformat(term_as_of)
            as_of = dt.datetime.combine(as_of_date, dt.time(12), tzinfo=dt.timezone.utc)
        except ValueError:
            return []
    now = as_of or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)
    # Activity/source dates are calendar claims, so retain the caller's
    # recording-date timezone. Only timestamp expiry comparison is normalized.
    today = now.date()
    now_utc = now.astimezone(dt.timezone.utc)
    expiry = _require_timestamp(payload["expires_at"], label="expires_at")
    if now_utc > expiry.astimezone(dt.timezone.utc):
        return []

    approved_records: list[dict[str, object]] = []
    for term in payload["terms"]:
        active_from = dt.date.fromisoformat(str(term["active_from"]))
        active_until = dt.date.fromisoformat(str(term["active_until"]))
        if not active_from <= today <= active_until:
            continue
        has_source_on_recording_date = any(
            dt.date.fromisoformat(str(source["published_at"])) <= today
            for source in term["sources"]
        )
        # A current term without a source that existed on the recording date
        # is future leakage, not evidence.
        if not has_source_on_recording_date:
            continue
        approved_records.append(
            {
                "canonical": term["canonical"],
                "readings": term["readings"],
                "aliases": term["aliases"],
                "confusables": term["confusables"],
                "topic": term["topic_entities"],
                "active_window": {
                    "from": active_from.isoformat(),
                    "until": active_until.isoformat(),
                },
            }
        )
        # The snapshot can retain broad lookback/lookahead coverage, while a
        # correction prompt stays bounded.  Crawler order is deterministic and
        # recency/popularity ranked; downstream audio/chat evidence still owns
        # the final entity choice.
        if len(approved_records) >= TIMELY_TERMS_PROMPT_MAX_COUNT:
            break
    return approved_records


