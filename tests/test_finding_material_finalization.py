from copy import deepcopy
import hashlib
import json

import pytest

from src.autoslice import finding_context as context
from src.autoslice.review_errors import ContractRejection
from tests.test_wpb_finding_context import check


def _assert_identity(value):
    def sha(v):
        return hashlib.sha256(
            json.dumps(
                v, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode()
        ).hexdigest()

    assert value["physical_question_sha256"] == sha(value["physical_question"])
    assert value["materials_sha256"] == sha(
        {
            k: v
            for k, v in value.items()
            if k not in {"physical_question_sha256", "materials_sha256"}
        }
    )
    assert context.validate_materials(value) == value


@pytest.mark.parametrize("kind", ["version", "check", "model", "origin"])
def test_fresh_material_is_hashed_once(kind, monkeypatch):
    request = check()
    kw = {"contracts": {"policy": "one"}, "model_identity": {"model": "one"}}
    base = context.materials_for_check(request, _read_only=True, **kw)
    version = context.material_version_for_check(request, **kw)
    material_calls = []
    original = context.digest

    def counted(value):
        if (
            isinstance(value, dict)
            and {
                "schema_version",
                "physical_question",
                "local_context",
                "candidates",
                "bound_observation_ids",
                "contracts",
                "model_identity",
            }
            == value.keys()
        ):
            material_calls.append(1)
        return original(value)

    monkeypatch.setattr(context, "digest", counted)
    if kind == "version":
        result = version.to_document()
        expected = 1
    elif kind == "check":
        result = context.materials_for_check(request, _read_only=True, **kw)
        expected = 1
    elif kind == "model":
        result = context.materials_for_model(base, {"model": "two"}, _read_only=True)
        expected = 2
    else:
        result = context.materials_for_origin(base, request, _read_only=True)
        expected = 2
    assert len(material_calls) == expected
    _assert_identity(result)


def test_external_tamper_origin_and_mutation_controls():
    request = check()
    source = {"nested": {"v": ["old"]}}
    item = context.materials_for_check(request, contracts=source, model_identity={"m": "one"})
    source["nested"]["v"].append("changed")
    assert item["contracts"]["nested"]["v"] == ["old"]
    forged = deepcopy(item)
    forged["contracts"]["nested"]["v"].append("changed")
    with pytest.raises(ContractRejection):
        context.validate_materials(forged)
    with pytest.raises(ContractRejection):
        context.materials_for_model(forged, {"m": "two"})
    malformed = deepcopy(item)
    malformed["candidates"] = ["z", "a"]
    with pytest.raises(ContractRejection):
        context.validate_materials(malformed)
    other = deepcopy(request)
    other["matched_end_ms"] += 1
    with pytest.raises(ContractRejection):
        context.materials_for_origin(item, other, _read_only=True)
    physical_forgery = deepcopy(item)
    physical_forgery["physical_question_sha256"] = "0" * 64
    with pytest.raises(ContractRejection):
        context.validate_materials(physical_forgery)
    returned = context.materials_for_model(item, {"m": "two"})
    returned["contracts"]["nested"]["v"].append("return mutation")
    assert item["contracts"]["nested"]["v"] == ["old"]
    _assert_identity(item)


def test_read_only_sharing_and_public_isolation():
    shared = {"evidence": ["source"]}
    contracts = {"left": shared, "right": shared}
    request = check()
    readonly = context.materials_for_check(
        request, contracts=contracts, model_identity={"m": "one"}, _read_only=True
    )
    public = context.materials_for_check(request, contracts=contracts, model_identity={"m": "one"})
    assert readonly["contracts"]["left"] is readonly["contracts"]["right"]
    assert public["contracts"]["left"] is not public["contracts"]["right"]
    readonly["contracts"]["left"]["evidence"].append("changed")
    assert contracts["left"]["evidence"] == ["source"]
    assert public["contracts"]["left"]["evidence"] == ["source"]
