"""Title/tag receipts cite the active channel's manifest and only its own research (goldfish 4: 枝堇Sumire's
tags.json named profiles/lidousha/profile.json and the default channel's title research reports)."""
from pathlib import Path

import pytest

from src.autoslice.channel_profile import load_channel_profile
from src.autoslice.editorial_research_provenance import editorial_research_method_provenance, profile_assets_locator
from src.autoslice.source_fact_review import EDITORIAL_RESEARCH_METHOD_PROVENANCE

ROOT = Path(__file__).resolve().parents[1]


def test_the_default_channel_receipt_is_unchanged():
    profile = load_channel_profile(ROOT, profile_id="lidousha", environ={})
    assert profile_assets_locator(profile, ROOT) == "profiles/lidousha/profile.json#assets.title_style,assets.upload_tag_policy"
    assert editorial_research_method_provenance("lidousha") == [dict(r) for r in EDITORIAL_RESEARCH_METHOD_PROVENANCE]


@pytest.mark.skipif(not (ROOT / "profiles/sumi/profile.json").is_file(), reason="the maintainer's second channel is not in the public tree")
def test_another_channel_cites_its_own_manifest_and_no_research_of_the_default_channel():
    profile = load_channel_profile(ROOT, profile_id="sumi", environ={})
    assert profile_assets_locator(profile, ROOT).startswith("profiles/sumi/profile.json#")
    assert editorial_research_method_provenance("sumi") == []
