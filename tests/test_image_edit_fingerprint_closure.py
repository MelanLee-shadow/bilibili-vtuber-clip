"""Moving the transport out must not remove it from existing proof hashes."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from src.autoslice.review_package_policy_fingerprint import build_policy_fingerprint
from src.autoslice.runner_pipeline_fingerprints import (
    pipeline_fingerprint,
    song_pipeline_fingerprint,
)


def _fingerprint(root: Path, lane: str) -> str:
    profile = SimpleNamespace(
        asset_files={},
        asset_file=lambda key: root / "assets" / (key + ".json"),
        asset_directory=lambda key: root / "assets" / key,
        fingerprint_paths=lambda **_kwargs: [],
    )

    def tool(key):
        return root / "scripts" / (key + ".py")

    if lane == "talk":
        return pipeline_fingerprint(
            repo_root=root,
            profile_tool=tool,
            channel_profile=profile,
            exclusions=set(),
            speaker_authority=lambda: None,
            speaker_authority_errors=(ValueError,),
        )
    if lane == "song":
        return song_pipeline_fingerprint(
            repo_root=root,
            profile_id="synthetic",
            channel_profile=profile,
            profile_tool=tool,
            profile_asset_file=profile.asset_file,
            profile_asset_directory=profile.asset_directory,
            policy={},
        )
    return build_policy_fingerprint(
        root=root,
        entrypoint=root / "scripts/audit.py",
        channel_profile=profile,
        policy_epoch="test-existing-proof-closure",
    )


@pytest.mark.parametrize("lane", ["talk", "song", "package"])
def test_image_transport_bytes_remain_in_existing_proof_closure(tmp_path, lane):
    transport = tmp_path / "src/autoslice/cpa_image_edit.py"
    transport.parent.mkdir(parents=True)
    transport.write_text("transport_version = 1\n")
    before = _fingerprint(tmp_path, lane)
    assert _fingerprint(tmp_path, lane) == before
    transport.write_text("transport_version = 2\n")
    assert _fingerprint(tmp_path, lane) != before
