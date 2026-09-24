from __future__ import annotations

import hashlib
import json
from pathlib import Path

from PIL import Image, ImageDraw
import pytest

from src.autoslice.cover_source_composition import verify_source_composition
from src.autoslice.host_only_source_exclusion import (
    AUTHORITY_SCHEMA,
    CROP_STRATEGY,
    HostOnlySourceExclusionError,
    materialize_host_only_safe_region_identity_card,
)


def _sha(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _probe(verdict: dict[str, object]):
    def probe(path: Path, _question: str, **_kwargs: object) -> dict[str, object]:
        return {
            "status": "OBSERVED",
            "provider": "cpa",
            "model": "gpt-test",
            "image_sha256": _sha(Path(path)).removeprefix("sha256:"),
            "answer": json.dumps(verdict, ensure_ascii=False),
            "routing": {
                "preferred_provider": "cpa",
                "selected_provider": "cpa",
                "fallback_used": False,
            },
        }

    return probe


def _fixture(tmp_path: Path) -> dict[str, object]:
    reference = tmp_path / "reference.png"
    image = Image.new("RGB", (1920, 1080), (20, 150, 80))
    draw = ImageDraw.Draw(image)
    draw.rectangle((20, 250, 210, 760), fill=(255, 0, 0))
    draw.rectangle((1545, 700, 1919, 1079), fill=(0, 0, 255))
    image.save(reference)
    source_verification = verify_source_composition(
        reference_path=reference,
        reference_sha256=_sha(reference),
        story_hook="唱完很累",
        title="唱完很累",
        image_probe=_probe(
            {
                "lidousha_bbox_frac": [0.316, 0.0, 0.824, 1.0],
                "source_face_complete": True,
                "faithful_crop_can_make_dominant": True,
                "source_carries_story_reaction": True,
                "cpa_redraw_recommended": False,
                "reason": "host centered",
            }
        ),
    )
    assert source_verification["status"] == "PASS"
    receipt = tmp_path / "source-composition.json"
    receipt.write_text(
        json.dumps(source_verification, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    receipt_sha256 = _sha(receipt)
    authority = {
        "schema_version": AUTHORITY_SCHEMA,
        "status": "PASS",
        "scope": "HOST_ONLY_SOURCE_PIXEL_EXCLUSION",
        "candidate_id": "candidate",
        "reference_sha256": _sha(reference),
        "source_size": [1920, 1080],
        "source_composition_receipt_sha256": receipt_sha256,
        "source_composition_witness_sha256": source_verification[
            "witness_receipt_sha256"
        ],
        "source_composition_host_bbox_norm": [0.316, 0.0, 0.824, 1.0],
        "diagnostic_host_bbox_norm": [0.332, 0.0, 0.773, 1.0],
        "non_host_entities": [
            {"label": "left avatar", "bbox_norm": [0.02, 0.2, 0.11, 0.8]},
            {"label": "right avatar", "bbox_norm": [0.805, 0.7, 0.99, 0.99]},
        ],
        "safe_source_region_norm": [0.235, 0.0, 0.802, 1.0],
        "safe_region_excludes_all_non_host": True,
        "diagnostic": {
            "provider": "cpa",
            "status": "OBSERVED",
            "image_sha256": _sha(reference),
            "receipt_sha256": "sha256:" + "d" * 64,
        },
        "accepted_by": "test-root",
        "accepted_at": "2026-09-24T00:00:00Z",
    }
    authority_path = tmp_path / "exclusion.json"
    authority_path.write_text(
        json.dumps(authority, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {
        "reference": reference,
        "verification": source_verification,
        "receipt": receipt,
        "receipt_sha256": receipt_sha256,
        "authority": authority,
        "authority_path": authority_path,
    }


def _render(tmp_path: Path, fixture: dict[str, object]):
    return materialize_host_only_safe_region_identity_card(
        reference_path=fixture["reference"],
        output_path=tmp_path / "out.png",
        frame_ms=65_000,
        candidate_id="candidate",
        source_composition_verification=fixture["verification"],
        source_composition_receipt_path=fixture["receipt"],
        source_composition_receipt_sha256=fixture["receipt_sha256"],
        exclusion_authority_path=fixture["authority_path"],
        exclusion_authority_sha256=_sha(fixture["authority_path"]),
    )


def test_safe_region_identity_card_excludes_all_outside_pixels(tmp_path: Path):
    fixture = _fixture(tmp_path)
    evidence = _render(tmp_path, fixture)

    assert evidence["crop_strategy"] == CROP_STRATEGY
    assert evidence["crop_box"] == [451, 0, 1540, 1080]
    assert evidence["identity_card_background"] == "BLURRED_HOST_ONLY_SAFE_REGION"
    assert evidence["host_only_source_exclusion_authority"]["sha256"] == _sha(
        fixture["authority_path"]
    )
    with Image.open(tmp_path / "out.png") as output:
        assert output.size == (1920, 1080)
        colors = output.convert("RGB").getdata()
        assert all(not (red > 230 and green < 25 and blue < 25) for red, green, blue in colors)
        assert all(not (blue > 230 and red < 25 and green < 25) for red, green, blue in colors)


def test_safe_region_intersection_fails_closed(tmp_path: Path):
    fixture = _fixture(tmp_path)
    authority = fixture["authority"]
    authority["safe_source_region_norm"] = [0.235, 0.0, 0.83, 1.0]
    fixture["authority_path"].write_text(
        json.dumps(authority, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(
        HostOnlySourceExclusionError, match="intersects non-host entity"
    ):
        _render(tmp_path, fixture)
    assert not (tmp_path / "out.png").exists()


def test_diagnostic_host_must_remain_inside_source_authority(tmp_path: Path):
    fixture = _fixture(tmp_path)
    authority = fixture["authority"]
    authority["diagnostic_host_bbox_norm"] = [0.2, 0.0, 0.773, 1.0]
    fixture["authority_path"].write_text(
        json.dumps(authority, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(
        HostOnlySourceExclusionError, match="escaped source authority"
    ):
        _render(tmp_path, fixture)


def test_reference_and_receipt_bindings_fail_closed(tmp_path: Path):
    fixture = _fixture(tmp_path)
    fixture["receipt"].write_text("{}\n", encoding="utf-8")

    with pytest.raises(
        HostOnlySourceExclusionError, match="receipt bytes drifted"
    ):
        _render(tmp_path, fixture)
