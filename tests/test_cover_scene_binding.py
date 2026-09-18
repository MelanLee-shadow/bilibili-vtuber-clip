"""Keep final-image constraints intact across the shared production seam."""

import pytest

from src.autoslice.cover_scene_binding import run_final_host_identity_witness


@pytest.mark.parametrize(
    ("scene", "fallback"),
    [
        ("talk", "HOST_ONLY_GENERIC"),
        ("talk", "HOST_ONLY_RELATION_EXPLICIT"),
        ("talk", "VERIFIED_DUAL_STREAM_FRAME"),
        ("game", "HOST_ONLY_GENERIC"),
    ],
)
def test_final_identity_receives_bound_story_and_scene(scene, fallback):
    story = {"cover_fallback_mode": fallback}
    calls = []

    def verifier(*, host_only_required=False, scene_kind="talk", **kwargs):
        calls.append((host_only_required, scene_kind, kwargs))
        return {"status": "OBSERVED_BY_TEST_DOUBLE"}

    result = run_final_host_identity_witness(
        verifier,
        final_cover_path="final.png",
        final_cover_sha256="sha256:" + "a" * 64,
        reference_path="reference.png",
        base_url="",
        api_key="",
        cover_generation={
            "story_contract": story,
            "source_composition_verification": {"scene_kind": scene},
        },
    )

    assert result["status"] == "OBSERVED_BY_TEST_DOUBLE"
    assert len(calls) == 1
    assert calls[0][0] is (scene == "talk" and fallback.startswith("HOST_ONLY_"))
    assert calls[0][1] == scene
    assert calls[0][2]["final_cover_path"] == "final.png"


def test_unbound_legacy_talk_does_not_invent_a_story_contract():
    calls = []

    def verifier(**kwargs):
        calls.append(kwargs)
        return {"status": "OBSERVED_BY_TEST_DOUBLE"}

    run_final_host_identity_witness(
        verifier,
        final_cover_path="final.png",
        final_cover_sha256="sha256:" + "a" * 64,
        reference_path="reference.png",
        base_url="",
        api_key="",
        cover_generation={},
    )
    assert "story_contract" not in calls[0]
    assert "host_only_required" not in calls[0]
    assert "scene_kind" not in calls[0]
