"""Representation equality for evidence, never an authorization predicate.

Only producer-owned bags may reorder. All members (including duplicates), source
bindings, ordered source arrays and normative prompt text remain significant.
Raw request/response digests continue to verify the original stored bytes.
"""
from __future__ import annotations


from collections.abc import Mapping
from .prompt_material_hash import EVIDENCE_BAGS, _json, prompt_material_view
from .prompt_material_hash import canonical_materials as canonical_materials
from .prompt_material_hash import prompt_material_sha256 as prompt_material_sha256


def has_material_bag(value, *, _path=()):
    if isinstance(value, Mapping):
        return any(has_material_bag(child, _path=(*_path, key)) for key, child in value.items())
    if isinstance(value, list):
        return _path in EVIDENCE_BAGS or any(
            has_material_bag(child, _path=(*_path, i)) for i, child in enumerate(value))
    return False


def same_prompt_materials(left, right):
    return left == right or _json(prompt_material_view(left)) == _json(prompt_material_view(right))


