"""Which channel the title/tag receipts' research method sources belong to, and the active manifest's locator."""
from __future__ import annotations

from pathlib import Path

from src.autoslice.source_fact_review import EDITORIAL_RESEARCH_METHOD_PROVENANCE


EDITORIAL_RESEARCH_METHOD_PROFILE = "lidousha"


def editorial_research_method_provenance(profile_id: str) -> list[dict[str, str]]:
    return ([dict(row) for row in EDITORIAL_RESEARCH_METHOD_PROVENANCE]
            if profile_id == EDITORIAL_RESEARCH_METHOD_PROFILE else [])


def profile_assets_locator(profile: object, repo_root: Path) -> str:
    """`<the active manifest>#assets.title_style,assets.upload_tag_policy` (was always profiles/lidousha/...)."""
    manifest = Path(getattr(profile, "manifest_path"))
    try:
        where = manifest.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        where = str(manifest)
    return where + "#assets.title_style,assets.upload_tag_policy"
