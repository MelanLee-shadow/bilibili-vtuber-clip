"""Per-session configuration for a manual-lane run (speaker mode, released items, identities).

Speaker colour is off unless a manual enablement is recorded for the session.  The current rule only
recognises collab sessions from 10-30 onward, so a 10-24 session without a record is uniform_host.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, NotRequired, TypedDict, cast

if TYPE_CHECKING:
    from src.autoslice.cpa_gemini_client import JsonValue

SPEAKER_RULE = ("默认 uniform_host 不分色；只有 10-30 起手动开启的联动场次才分色（memory: "
                "lidousha-speaker-uniform-host-phase）。开启须有记录，否则不分色。")


class SpeakerPeople(TypedDict, total=False):
    participants: list[list[str]]
    speaker_count: int | str | None


class SessionBlock(SpeakerPeople):
    """Session fields copied into a clip's persisted state."""

    recording_date: str
    speaker_mode: str
    enablement_evidence: str | None
    speaker_rule: str
    own_uploader_mids: list[int]


_UNKNOWN_PARTICIPANTS = frozenset({"连线", "连线嘉宾", "嘉宾", "身份未知", "未知", "unknown",
                                   "未具名嘉宾", "未知嘉宾", "匿名嘉宾"})


def unknown_participant(name: str) -> bool:
    return name.strip().casefold() in _UNKNOWN_PARTICIPANTS


def placeholder_as_person(session: SpeakerPeople, text: str) -> bool:
    """Recognize the call label in a person's grammatical slot, retaining ordinary call actions and guest roles."""
    if not any(g and str(g[0]).strip() == "连线" for g in session.get("participants") or []):
        return False
    person_slot = r"(?:让|被|和|跟|与|同|给|把|对|向|问|找|叫|劝|陪|听|喊|请)\s*连线"
    call_or_guest = r"(?:的?(?:嘉宾|对象|朋友|主播|观众|搭档)|断|打开|关|挂|结束|开启|中|时|过程)"
    subject = r"(?:^|[，。！？；】\s])连线(?:说|问|表示|觉得|以为|被|笑|哭|喊|回复|承认|告诉|要求)"
    return bool(re.search(person_slot + "(?!" + call_or_guest + ")", text)
                or re.search(subject, text)
                or re.search(r"(?:^|[，。！？：；\s])连线\s*[:：]", text))


def participant_names(session: SpeakerPeople, *, visual: bool = False) -> list[str]:
    """Project speaker labels to known identities; keep anonymous guests as roles for visual composition."""
    names = []
    for group in session.get("participants") or []:
        if not group:
            continue
        name = str(group[0]).strip()
        if unknown_participant(name):
            if visual:
                names.append("未具名嘉宾")
        elif name:
            names.append(name)
    return names


def participant_description(session: SpeakerPeople, *, deduplicate: bool = False) -> str:
    names = participant_names(session)
    labels = [str(g[0]).strip() for g in session.get("participants") or [] if g and unknown_participant(str(g[0]))]
    suffix = ("。连线方身份未知，标签不是名字" if all(n == "连线" for n in labels)
              else "。有未具名嘉宾，标签不是名字") if labels else ""
    return "、".join(dict.fromkeys(names) if deduplicate else names) + suffix


class _ReleaseInput(TypedDict):
    reason: JsonValue
    quote: NotRequired[JsonValue]


class _SessionInput(TypedDict):
    recording_date: JsonValue
    participants: list[list[JsonValue]]
    speaker_mode: NotRequired[JsonValue]
    enablement_evidence: NotRequired[str | None]
    own_uploader_mids: NotRequired[list[str | int | float]]
    released: NotRequired[list[_ReleaseInput]]
    speaker_rule: NotRequired[JsonValue]


@dataclass(frozen=True)
class SessionConfig:
    recording_date: str
    speaker_mode: str
    enablement_evidence: str | None
    participants: tuple[tuple[str, ...], ...]
    own_uploader_mids: tuple[int, ...]
    released: tuple[dict[str, str], ...] = ()
    speaker_rule: str = SPEAKER_RULE
    extra: dict[str, JsonValue] = field(default_factory=dict)

    def as_session_block(self) -> SessionBlock:
        return {
            "recording_date": self.recording_date,
            "speaker_mode": self.speaker_mode,
            "enablement_evidence": self.enablement_evidence,
            "speaker_rule": self.speaker_rule,
            "participants": [list(g) for g in self.participants],
            "own_uploader_mids": list(self.own_uploader_mids),
        }


def load_session_config(path: Path) -> SessionConfig:
    raw = cast("dict[str, JsonValue]", json.loads(Path(path).read_text(encoding="utf-8")))
    data = cast(_SessionInput, raw)
    if "reviewer_labels" in data:
        # Listening labels are imported with `listen-import` into the clip state (hash-scoped to stage 3).
        raise ValueError("reviewer_labels must not be written in the session config; use listen-import")
    return SessionConfig(
        recording_date=str(data["recording_date"]),
        speaker_mode=str(data.get("speaker_mode", "uniform_host")),
        enablement_evidence=data.get("enablement_evidence"),
        participants=tuple(tuple(str(a) for a in group) for group in data["participants"]),
        own_uploader_mids=tuple(int(m) for m in data.get("own_uploader_mids", [])),
        released=tuple({"reason": str(r["reason"]), "quote": str(r.get("quote", ""))}
                       for r in data.get("released", [])),
        speaker_rule=str(data.get("speaker_rule", SPEAKER_RULE)),
        extra={k: v for k, v in raw.items() if k not in {
            "recording_date", "speaker_mode", "enablement_evidence", "participants",
            "own_uploader_mids", "released", "speaker_rule"}},
    )
