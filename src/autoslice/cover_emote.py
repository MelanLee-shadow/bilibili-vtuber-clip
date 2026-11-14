"""Official emote stickers as cover candidates (维护者 10/19, CVR-02: "作默认很不错").

The library is a committed per-channel manifest (``emote_library`` profile
asset); the sticker media ships with this repo under the profile tree
(``assets/<profile>/emote/``; override the root via ``AUTOSLICE_EMOTE_DIR``).  Policy, enforced by the callers
in ``title_cover_staging`` (the candidate producer) and the legacy routes:

- An official emote is a DEFAULT candidate, like a screenshot, a collage, a cut-out
  or an AI route. No route is preset: candidates are compared on the finished cover by
  the scorecard, and the highest total wins (``title_cover_candidates.score_candidate``).
  The 7/19 "only on a strong reason" rule is superseded.
- When the planner omits an emote candidate, ``pick_emote_for_text`` picks one by
  manifest keywords (deterministic, no model call). A hook with no matching sticker
  only records a reason; it never blocks the cover.
- ``companion`` mode (emote AND character in one frame) is reserved for
  分身/复数小李 memes or drawing a panda-class sticker to depict kmx —
  kmx (读 kimo熊) is the NAME OF HER FANS, not a mascot; the sticker stands
  in for the audience instead of drawing generic crowd people.
- Song covers never use emotes.
- Sticker redraws must stay light: same pose/expression/design, only polished
  and integrated; like the character redraw, the sticker owns most of the frame.

Everything in this module fails OPEN back to the remaining feasible candidates (a
missing/mismatched sticker downgrades the ART DIRECTION only); the cover IMAGE
itself stays fail-closed in the staging pipeline.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from src.autoslice.channel_profile import ChannelProfileError, load_channel_profile


ROOT = Path(__file__).resolve().parents[2]

_EMOTE_SCHEMA_PREFIX = "lidousha-emote-library"
# Keyword split for manifest ``use_when`` phrases; the skill copy (choose_cover.py) uses the same pattern.
_EMOTE_KEYWORD_SPLIT = re.compile(r"[/、，,；;：:（）()“”\"'\s]+")
_EMOTE_KEYWORD_MIN_CHARS = 2
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class EmoteEntry:
    """One official sticker: identity, media pointers, and selection semantics."""

    id: str
    label: str
    subject: str          # "lidousha" (chibi 小李) | "panda_creature" (兽形本体)
    file: str             # small original sticker, relative to the media root
    hd_file: str          # AI-upscaled redraw used as the CPA reference image
    hd_sha256: str
    baked_text: str       # caption baked into the sticker art ("" = none)
    description: str      # what the sticker shows (pose/expression/props)
    use_when: str         # strong-match scenarios for the selection judge


@dataclass(frozen=True)
class EmoteLibrary:
    enabled: bool
    entries: tuple[EmoteEntry, ...]
    # Manifest-declared media roots probed AFTER the env override and BEFORE
    # the in-repo default (the free runner's tree-out media lives here, same
    # pattern as the branding intro's runtime_media_paths — deploying the
    # manifest is enough, no service-env injection).
    runtime_roots: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return self.enabled and bool(self.entries)

    def get(self, emote_id: str) -> EmoteEntry | None:
        for entry in self.entries:
            if entry.id == emote_id:
                return entry
        return None

    @property
    def ids(self) -> frozenset[str]:
        return frozenset(entry.id for entry in self.entries)


EMPTY_EMOTE_LIBRARY = EmoteLibrary(enabled=False, entries=())


def _safe_media_relative(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    relative = Path(value.strip())
    if relative.is_absolute() or ".." in relative.parts:
        return None
    return str(relative)


def load_emote_library(repo_root: Path | None = None) -> EmoteLibrary:
    """Parse the selected profile's emote manifest.  Fail-OPEN: any missing,
    malformed, or unsafe manifest yields the empty library (⇒ the cover always
    uses the remaining feasible candidates)."""

    root = repo_root or ROOT
    try:
        profile = load_channel_profile(root)
        manifest_path = profile.asset_file("emote_library", repo_root=root)
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (ChannelProfileError, OSError, json.JSONDecodeError, ValueError):
        return EMPTY_EMOTE_LIBRARY
    if not isinstance(payload, dict):
        return EMPTY_EMOTE_LIBRARY
    schema = payload.get("schema_version")
    if not (isinstance(schema, str) and schema.startswith(_EMOTE_SCHEMA_PREFIX)):
        return EMPTY_EMOTE_LIBRARY
    raw_entries = payload.get("emotes")
    if not isinstance(raw_entries, list):
        return EMPTY_EMOTE_LIBRARY
    entries: list[EmoteEntry] = []
    seen_ids: set[str] = set()
    for raw in raw_entries:
        if not isinstance(raw, dict):
            return EMPTY_EMOTE_LIBRARY
        emote_id = raw.get("id")
        label = raw.get("label")
        hd_sha256 = raw.get("hd_sha256")
        file_rel = _safe_media_relative(raw.get("file"))
        hd_rel = _safe_media_relative(raw.get("hd_file"))
        if not (
            isinstance(emote_id, str)
            and emote_id.strip()
            and emote_id not in seen_ids
            and isinstance(label, str)
            and label.strip()
            and file_rel
            and hd_rel
            and isinstance(hd_sha256, str)
            and _SHA256_RE.fullmatch(hd_sha256)
        ):
            return EMPTY_EMOTE_LIBRARY
        seen_ids.add(emote_id)
        subject = raw.get("subject")
        entries.append(
            EmoteEntry(
                id=emote_id.strip(),
                label=label.strip(),
                subject=subject if subject in ("lidousha", "panda_creature") else "lidousha",
                file=file_rel,
                hd_file=hd_rel,
                hd_sha256=hd_sha256,
                baked_text=str(raw.get("baked_text") or "").strip(),
                description=str(raw.get("description") or "").strip(),
                use_when=str(raw.get("use_when") or "").strip(),
            )
        )
    media_root = payload.get("media_root")
    runtime_roots: tuple[str, ...] = ()
    if isinstance(media_root, dict):
        raw_roots = media_root.get("runtime_roots")
        if isinstance(raw_roots, list):
            runtime_roots = tuple(
                item.strip() for item in raw_roots if isinstance(item, str) and item.strip()
            )
    return EmoteLibrary(
        enabled=bool(payload.get("enabled", True)),
        entries=tuple(entries),
        runtime_roots=runtime_roots,
    )


def emote_keywords(entry: EmoteEntry) -> tuple[str, ...]:
    """Deterministic match keys: the label plus the manifest's ``use_when`` phrases."""

    parts = [entry.label, *_EMOTE_KEYWORD_SPLIT.split(entry.use_when)]
    return tuple(dict.fromkeys(p for p in parts if len(p) >= _EMOTE_KEYWORD_MIN_CHARS))


def pick_emote_for_text(
    text: str, library: EmoteLibrary, *, excluded: Iterable[str] = ()
) -> tuple[str | None, str]:
    """Pick the lidousha sticker whose keywords occur most often in ``text``.

    ``text`` is the cover lines plus the final-subtitle quotes. ``excluded`` holds
    emote ids that may not be the main emote (same source session or last five covers).
    The panda creature is never a main emote; it is the kmx layer. Ties keep manifest
    order. No match returns ``(None, reason)``: a record, never a gate.
    """

    if not library:
        return None, "EMOTE_LIBRARY_DISABLED"
    blocked = set(excluded)
    best: tuple[EmoteEntry, list[str]] | None = None
    for entry in library.entries:
        if entry.subject != "lidousha" or entry.id in blocked:
            continue
        matched = [key for key in emote_keywords(entry) if key in text]
        if matched and (best is None or len(matched) > len(best[1])):
            best = (entry, matched)
    if best is None:
        return None, "NO_EMOTION_MATCH: 官方表情包清单中没有与这段原话情绪相符的条目"
    return best[0].id, "MATCHED: " + "、".join(best[1])


