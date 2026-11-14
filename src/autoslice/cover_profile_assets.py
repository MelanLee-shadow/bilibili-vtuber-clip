"""The cover pipeline's repo root, its loaded channel profile and the profile asset text reader.

Split out of cover_generation so the lane reads the same cover identity and ledger assets without loading the
cover generator; cover_generation imports the names back."""
from __future__ import annotations

from pathlib import Path

from src.autoslice.channel_profile import load_channel_profile

ROOT = Path(__file__).resolve().parents[2]
CHANNEL_PROFILE = load_channel_profile(ROOT)


def profile_asset_text(key: str) -> str:
    try:
        return CHANNEL_PROFILE.asset_file(key, repo_root=ROOT).read_text(
            encoding="utf-8"
        ).strip()
    except OSError:
        return "(资产文件缺失)"
