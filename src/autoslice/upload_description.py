"""The archive description the uploader submits, and the one a package must carry to match it.

Split out of scripts/authorized_upload so the manual lane renders the same bytes without importing the whole
uploader; authorized_upload re-exports every name for its existing callers."""
from __future__ import annotations

from collections.abc import Sequence

from src.autoslice.audience_text import assert_audience_text
from src.autoslice.channel_envelope import append_description_extra, publish_description_for_title

import os
# 简介第一行默认带项目署名（README 礼节性请求的默认实现；不想带可改掉）；
# 频道自己的简介行是身份数据：来自环境变量（见 .env.example），不硬编码。
PROJECT_ATTRIBUTION_LINE = (
    "本切片由 bilibili-vtuber-clip 项目提供："
    "https://github.com/MelanLee-shadow/bilibili-vtuber-clip"
)
SUBMISSION_DESCRIPTION = (
    PROJECT_ATTRIBUTION_LINE + "\n" + os.environ.get("AUTOSLICE_UPLOAD_DESC", "")
).strip()
EXPECTED_SOURCE = "https://live.bilibili.com/"
# biliup's APP submission sends the two-line description above, while
# Bilibili's public/member archive surfaces prepend the copyright source URL.
# Freeze the observed public contract instead of treating that deterministic
# server-side projection as metadata drift.


def _channel_lines() -> list[str] | None:
    """The active channel's own description lines after the attribution line (its ``upload_description`` asset,
    ``{"lines": [...]}``).  None for the default channel without that asset (the lines above are its own); any other
    channel without it gets no lines, never another channel's homepage and room."""
    import json
    import os
    from pathlib import Path

    from src.autoslice.channel_profile import DEFAULT_CHANNEL_PROFILE_ID, load_channel_profile

    channel = load_channel_profile(Path(__file__).resolve().parents[2])
    if "upload_description" in channel.asset_files:
        raw = json.loads(channel.asset_file("upload_description").read_text(encoding="utf-8"))
        return [str(line) for line in raw["lines"]]
    if channel.profile_id == DEFAULT_CHANNEL_PROFILE_ID:
        return None
    given = os.environ.get("AUTOSLICE_UPLOAD_DESC", "").strip()   # a deployer's own lines, as the public tree reads them
    return [given] if given else []


_LINES = _channel_lines()
if _LINES is not None:
    SUBMISSION_DESCRIPTION = "\n".join([PROJECT_ATTRIBUTION_LINE, *_LINES])
DEFAULT_DESCRIPTION = EXPECTED_SOURCE + "\n" + SUBMISSION_DESCRIPTION
assert_audience_text(DEFAULT_DESCRIPTION, surface="description")


def description_for_title(
    title: str, *, submission: bool = False, extra: str | Sequence[str] | None = None
) -> str:
    """Archive description = channel lines + the title partners' related lines.

    ``extra`` (finished-media review lane ``description_extra``: a string, or a
    list of strings that each take one line) is appended after the
    related-streamer lines; ``None`` leaves the description byte-identical.
    """
    description = publish_description_for_title(
        SUBMISSION_DESCRIPTION if submission else DEFAULT_DESCRIPTION, title
    )
    return append_description_extra(description, extra)
