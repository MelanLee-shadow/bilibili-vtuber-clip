"""Clip profile: the one definition of a delivered clip's form.

公开规则:04Z：「在这种与李豆沙相关的，李豆沙不是主角的切片里，不需要加李豆沙片头」
公开规则：给出格式的检查格式的应该来自同一个定义…两个输出而已

A clip's form used to be implied by ``lane`` (talk/song) alone, so a related
clip in which the host is not the protagonist inherited the talk intro and the
default talk collection. The form is now declared once, in the producer spec
as ``spec["clip_profile"]``; a record or upload manifest only transcribes the
declared document. Everything that needs the form derives it here:

* ``requires_intro(profile)`` — whether the delivered video carries the
 channel intro. The intro belongs only to talk clips whose protagonist is the
 host; songs (SONG-07「不放片头直接进歌」) and non-protagonist related clips are
 derived members of the same rule, not separate exemptions. Callers gate
 their existing ``require_branding_intro`` call on it, so the channel manifest
 switch and the ``AUTOSLICE_BRANDING_INTRO=off`` test/emergency hatch stay
 where they are.
* ``season_for(profile)`` — the collection the archive joins.
* ``intro_binding_problem(profile, binding, ...)`` — the delivery-gate check
 that a burned intro binding agrees with the form.

Only ``host_is_protagonist`` and ``delivery_kind`` feed these derivations.
``input_kind`` and ``watched_media`` are declared here so later consumers read
one definition; no derivation in this module consumes them. ``watched_media``
keeps its existing single declaration site, the spec's top-level
``watched_media`` field; the spec's ``clip_profile`` block must not repeat it.

A spec, record or manifest without a declaration derives the historical lane
default in memory. The default is never written back, so existing bytes and
their hash bindings are unchanged. The account-bound collection/section IDs
live only in ``SEASON_IDS``.
"""

from __future__ import annotations

from types import MappingProxyType


# Collection titles and the maintainer account's collection/section IDs, keyed
# by delivery kind.  The IDs are account-bound (public export: deployer
# supplied); the title is what the public archive view must display.
SEASON_TITLES = MappingProxyType(
    {"talk": "小李切片", "song": "小李歌唱", "independent_related": "李豆沙相关"}
)
_SEASON_IDS_ENV = "AUTOSLICE_SEASON_IDS"


def season_ids() -> dict[str, dict[str, int]]:
    """入集校验的合集/小节 ID——账号专属，强制部署方自填，无默认值。"""
    import json
    import os

    raw = os.environ.get(_SEASON_IDS_ENV)
    if not raw:
        raise SystemExit(
            "AUTOSLICE_SEASON_IDS 未配置：发布入集校验需要你自己账号的合集/小节 ID。\n"
            '格式：{"talk": {"season_id": 0, "section_id": 0}, '
            '"song": {"season_id": 0, "section_id": 0}, '
            '"independent_related": {"season_id": 0, "section_id": 0}}\n'
            "获取：在创作中心手动把任一稿件加入目标合集，再用 "
            "scripts/bili_archive_tool.py view 回读该稿件的 season_id/section_id。"
        )
    return json.loads(raw)


def _channel_season_routes() -> dict[str, dict[str, object]] | None:
    """The active channel's own collections, from its ``season_routes`` asset
    (``{route: {"season_title", "season_id", "section_id"}}``).  None for the
    default channel without that asset (the routes above are its own).  Any
    other channel without the asset has no collections at all, so its uploads
    stop before joining one: never another channel's collections."""

    import json
    from pathlib import Path

    from .channel_profile import DEFAULT_CHANNEL_PROFILE_ID, load_channel_profile

    channel = load_channel_profile(Path(__file__).resolve().parents[2])
    if "season_routes" in channel.asset_files:
        raw = json.loads(channel.asset_file("season_routes").read_text(encoding="utf-8"))
        return {route: dict(block) for route, block in raw["routes"].items()}
    return None if channel.profile_id == DEFAULT_CHANNEL_PROFILE_ID else {}


_CHANNEL_ROUTES = _channel_season_routes()
if _CHANNEL_ROUTES is not None:
    SEASON_TITLES = MappingProxyType({route: str(b["season_title"]) for route, b in _CHANNEL_ROUTES.items()})
    SEASON_IDS = {route: {"season_id": int(b["season_id"]), "section_id": int(b["section_id"])}  # type: ignore[call-overload]
                  for route, b in _CHANNEL_ROUTES.items()}


