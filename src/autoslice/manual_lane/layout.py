"""Where the manual lane's runtime lives: hosts, deployed tools, work directories, frozen models (U125: the code names
no host path; the project is public).

One definition, read once.  The values come from `manual_lane.json` in the active channel profile's directory (the
profile the repo already selects: channel_profile.resolve_channel_profile_manifest, AUTOSLICE_PROFILE /
AUTOSLICE_PROFILE_MANIFEST); every key can be overridden by the environment variable MANUAL_LANE_<KEY> (nested keys
joined with "_", upper case, e.g. MANUAL_LANE_SPEAKER_V8_PYTHON).  Everything else is derived here from those few roots,
so a caller never spells a deployed path itself.  Channel facts stay in their own assets: the room id in the profile
manifest, the emote media root in the emote library, the intro file names in the branding-intro manifest.  A session's
work directory is <work_root>/<recording date>; no default is a particular day's directory.  The lane's shared assets
(verified intro copies, the cover font) live under <work_root>/assets, not in any task's directory: archiving a task
directory must never break the next clip.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from src.autoslice.cpa_gemini_client import JsonValue

REPO_ROOT = Path(__file__).resolve().parents[3]
LAYOUT_FILE = "manual_lane.json"
SCHEMA = "manual-lane.runtime-layout.v1"
ENV_PREFIX = "MANUAL_LANE_"
_STRINGS = ("remote_host", "remote_user", "autoslice_base", "work_root", "danmaku_host", "danmaku_root",
            "cover_skill_scripts")
ASSETS_DIR = "assets"                                  # <work_root>/assets: the lane's shared, undated assets
INTRO_SUBDIR = "intro"                                 # ... /intro: verified copies of the branding intros
COVER_FONT_FILE = "fonts/RuiZiZhenYan.ttf"             # ... /fonts/RuiZiZhenYan.ttf: the cover font
_SPEAKER_V8 = ("code_dir", "python", "model", "score")
_FREE_ASR = ("min_interval_s", "rate_limit_cooldown_s", "attempts", "rate_limit_attempts")


class LayoutError(ValueError):
    """manual_lane.json is missing, malformed, or a required key is empty."""


@dataclass(frozen=True)
class FreeAsrPacing:
    """Pacing of the free ASR providers (BCUT, jianying; U152).  See assets/manual_lane/remote/free_asr_paced.py."""
    min_interval_s: float
    rate_limit_cooldown_s: float
    attempts: int
    rate_limit_attempts: int


@dataclass(frozen=True)
class Layout:
    remote_host: str              # ssh alias of the media host (OCI3)
    remote_user: str              # the unprivileged user that owns the lane's remote files
    autoslice_base: str           # deployed autoslice runtime base on the media host
    work_root: str                # the lane's work root on the media host (sessions, frozen-model workdirs)
    danmaku_host: str             # ssh alias of the recording archive (N100)
    danmaku_root: str             # recordings root on the archive; <room id>/<date>/<name>.xml below it
    room_id: str                  # the channel's live room (profile identity.room_id)
    intro_dir: str                # verified copies of the branding intros, readable by remote_user (<work_root>/assets/intro)
    cover_font: str               # the cover font on the media host, readable by remote_user (<work_root>/assets/fonts/...)
    cover_skill_scripts: Path     # local: the title-cover skill's scripts (render_cover.py, redraw_background.py)
    speaker_v8: Mapping[str, str]  # the frozen v8 speaker workspace: code_dir, python, model, score
    free_asr: FreeAsrPacing
    search_mcp_url: str = ""      # Search Router MCP address (work/circle lookup); optional, "" = not configured
    recordings_direct_root: str = ""   # recordings copied straight from the recorder to the media host (<root>/<room id>/<date>/);
    #                                    optional: a day found there is read from there, any other day from the archive
    recordings_direct_host: str = ""   # the media host's ssh alias of the recorder's recordings (read-only rsync); optional:
    #                                    with it and the root, discover copies the newest days before it looks

    @property
    def remote_repo(self) -> str:
        return f"{self.autoslice_base}/current/repo"

    @property
    def remote_py(self) -> str:
        return f"{self.autoslice_base}/venv-main/bin/python"

    def env_file(self, name: str) -> str:
        """A provider's credentials file on the media host (sourced only inside a root subshell, never read here)."""
        return f"{self.autoslice_base}/{name}.env"

    @property
    def usage_ledger(self) -> str:
        return f"{self.autoslice_base}/state/cpa-gemini/requests.jsonl"

    @property
    def news_graph_root(self) -> str:
        """The news/event crawler's candidate graph (scheduled_crawlers.v1.json, job news-events: --write-root)."""
        return f"{self.autoslice_base}/state/news_event_graph"

    @property
    def speaker_v8_root(self) -> str:
        return f"{self.work_root}/speaker-v8"

    @property
    def free_asr_state_dir(self) -> str:
        return f"{self.work_root}/.free-asr-pace"

    def session_root(self, recording_date: str) -> str:
        return f"{self.work_root}/{recording_date}"

    @property
    def danmaku_dir(self) -> str:
        return f"{self.danmaku_root}/{self.room_id}"


def _profile_dir(environ: Mapping[str, str]) -> Path:
    from src.autoslice.channel_profile import resolve_channel_profile_manifest

    return resolve_channel_profile_manifest(REPO_ROOT, environ=environ).parent


def _room_id(environ: Mapping[str, str]) -> str:
    from src.autoslice.channel_profile import load_channel_profile

    return load_channel_profile(REPO_ROOT, environ=environ).room_id


def _value(doc: Mapping[str, JsonValue], key: str, environ: Mapping[str, str], *, section: str = "") -> JsonValue:
    env = ENV_PREFIX + (f"{section}_{key}" if section else key).upper()
    if environ.get(env):
        return environ[env]
    raw = cast("Mapping[str, JsonValue]", doc.get(section) or {}).get(key) if section else doc.get(key)
    if raw in (None, ""):
        raise LayoutError(f"{LAYOUT_FILE}: {section + '.' if section else ''}{key} is not set (or set {env})")
    return raw


def load_layout(environ: Mapping[str, str] | None = None, *, path: Path | None = None) -> Layout:
    env = os.environ if environ is None else environ
    source = path or _profile_dir(env) / LAYOUT_FILE
    try:
        doc = cast("dict[str, JsonValue]", json.loads(Path(source).read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:
        raise LayoutError(f"cannot read the lane's runtime layout {source}: {exc}") from exc
    if doc.get("schema") != SCHEMA:
        raise LayoutError(f"{source}: schema must be {SCHEMA!r}")
    strings = {k: str(_value(doc, k, env)) for k in _STRINGS}
    # Derived from work_root unless pinned by the profile or MANUAL_LANE_<KEY> (the derivation is the one definition).
    assets = f"{strings['work_root']}/{ASSETS_DIR}"
    strings["intro_dir"] = str(env.get(ENV_PREFIX + "INTRO_DIR") or doc.get("intro_dir") or f"{assets}/{INTRO_SUBDIR}")
    strings["cover_font"] = str(env.get(ENV_PREFIX + "COVER_FONT") or doc.get("cover_font") or f"{assets}/{COVER_FONT_FILE}")
    pacing = {k: _value(doc, k, env, section="free_asr") for k in _FREE_ASR}
    skill_scripts = Path(os.path.expanduser(strings["cover_skill_scripts"]))
    if not skill_scripts.is_absolute():
        skill_scripts = (REPO_ROOT / skill_scripts).resolve()
    return Layout(**{k: v for k, v in strings.items() if k != "cover_skill_scripts"},
                  cover_skill_scripts=skill_scripts,
                  search_mcp_url=str(env.get(ENV_PREFIX + "SEARCH_MCP_URL") or doc.get("search_mcp_url") or ""),
                  recordings_direct_root=str(env.get(ENV_PREFIX + "RECORDINGS_DIRECT_ROOT")
                                             or doc.get("recordings_direct_root") or ""),
                  recordings_direct_host=str(env.get(ENV_PREFIX + "RECORDINGS_DIRECT_HOST")
                                             or doc.get("recordings_direct_host") or ""),
                  room_id=str(env.get(ENV_PREFIX + "ROOM_ID") or _room_id(env)),
                  speaker_v8={k: str(_value(doc, k, env, section="speaker_v8")) for k in _SPEAKER_V8},
                  free_asr=FreeAsrPacing(min_interval_s=float(cast("str | float | int", pacing["min_interval_s"])),
                                         rate_limit_cooldown_s=float(cast("str | float | int", pacing["rate_limit_cooldown_s"])),
                                         attempts=int(cast("str | float | int", pacing["attempts"])),
                                         rate_limit_attempts=int(cast("str | float | int", pacing["rate_limit_attempts"]))))


@lru_cache(maxsize=1)
def layout() -> Layout:
    """The process's layout (read once; the environment is read at the first call)."""
    return load_layout()
