"""The channel the lane runs for: the one place the lane reads who the host is.

Everything about one channel lives in its profile (profiles/<id>/): the host's names and speaker label, the title
prefix, the cover identity and the asset directory are the channel profile's (src/autoslice/channel_profile.py);
the few sentences the lane's prompts say about the channel are in `manual_lane_channel.json` beside it.  The lane's
code, prompts (assets/manual_lane/prompts) and rule numbers (lane_rules.json) name no channel: a prompt says `{host}`,
`{fans_name}`, `{channel_entities}`, `{listen_name_examples}`, `{cover_expression_rule}`, and `prompt()` fills them in.
Switching channel is switching profile (AUTOSLICE_PROFILE, read when the process starts).

tests/manual_lane/test_channel_neutral.py fails when a profile's names appear in the lane again.
"""

from __future__ import annotations

import json
import os
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import Any

from src.autoslice.channel_profile import ChannelProfile, load_channel_profile

REPO_ROOT = Path(__file__).resolve().parents[3]
PROMPTS = REPO_ROOT / "assets" / "manual_lane" / "prompts"
TEXT_FILE = "manual_lane_channel.json"
TEXT_KEYS = ("fans_name", "channel_entities", "listen_name_examples", "cover_expression_rule", "title_name_forms",
             "title_name_note", "fans_emote_id", "picture_body_hint", "picture_face_hint", "editor_nicknames")


class ChannelTextError(ValueError):
    """manual_lane_channel.json is missing, malformed, or lacks a key."""


@lru_cache(maxsize=1)
def profile() -> ChannelProfile:
    return load_channel_profile(REPO_ROOT)


@lru_cache(maxsize=1)
def text() -> dict[str, Any]:
    """The channel's own sentences for the lane's prompts (profiles/<id>/manual_lane_channel.json)."""
    path = profile().manifest_path.parent / TEXT_FILE
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ChannelTextError(f"{path}: {type(exc).__name__}: {exc}") from exc
    missing = [k for k in TEXT_KEYS if k not in doc]
    if missing:
        raise ChannelTextError(f"{path}: missing {missing}")
    return doc


HOST = profile().display_name                 # how the host is named in prompts and process notes
HOST_LABEL = profile().host_speaker_label     # the speaker label of the host's lines
ASSETS = profile().asset_root                 # the channel's asset directory (persona, glossary, title style, fonts ...)
ASSETS_REL = ASSETS.relative_to(REPO_ROOT).as_posix()


def asset(name: str) -> Path:
    return ASSETS / name


def prompt_fields() -> dict[str, str]:
    doc = text()
    return {"host": HOST, "fans_name": str(doc["fans_name"]), "channel_entities": str(doc["channel_entities"]).rstrip("\n"),
            "listen_name_examples": str(doc["listen_name_examples"]),
            "cover_expression_rule": str(doc["cover_expression_rule"])}


def fill(template: str) -> str:
    for key, value in prompt_fields().items():
        template = template.replace("{" + key + "}", value)
    return template


def prompt(name: str) -> str:
    """prompts/<name> with the channel's fields filled in; every other `{field}` is the caller's."""
    return fill((PROMPTS / name).read_text(encoding="utf-8"))


def prompt_file(name: str) -> Path:
    """`prompt(name)` as a file, for the tools that read their prompt from disk on the media host."""
    out = Path(tempfile.gettempdir()) / f"manual-lane-prompts-{profile().profile_id}-{os.getuid()}" / name
    body = prompt(name)
    if not out.is_file() or out.read_text(encoding="utf-8") != body:
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_name(f".{out.name}.{os.getpid()}.tmp")
        tmp.write_text(body, encoding="utf-8")
        os.replace(tmp, out)
    return out
