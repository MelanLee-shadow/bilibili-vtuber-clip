from copy import deepcopy
from pathlib import Path

from scripts.finalize_b2_caption_same_bv_recovery_package import _project_cover_generation


def test_cover_relocation_keeps_sealed_historical_witnesses(tmp_path: Path):
    paths = {}
    for key in ("cover", "reference", "ai_background", "builtin_provenance", "title_mask"):
        paths[key] = tmp_path / key
        paths[key].write_bytes(key.encode())
    witness = {"reference_path": "/original/reference.png", "comparison_path": "/original/witness.png", "receipt_sha256": "sealed"}
    generation = {
        "final_cover": "/original/cover.png",
        "reference_image": "/original/reference.png",
        "builtin_imagegen_provenance_path": "/original/source.json",
        "final_host_identity_verification": deepcopy(witness),
        "source_composition_verification": deepcopy(witness),
    }
    _project_cover_generation(generation, paths)
    assert generation["final_cover"] == str(paths["cover"])
    assert generation["reference_image"] == str(paths["reference"])
    assert generation["builtin_imagegen_provenance_path"] == str(paths["builtin_provenance"])
    assert generation["final_host_identity_verification"] == witness
    assert generation["source_composition_verification"] == witness
