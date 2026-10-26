"""Read asset binding definitions without importing the production runner.

AST inspection is intentional: importing the runner would initialize runtime
configuration. No asset contents, credentials, or production state are read.
"""

from __future__ import annotations

import ast
import json
from collections.abc import Sequence
from pathlib import Path

from .persona_facts import AssetBindingSurface, BindingSurfaceKind, InflightBinding, WriteDecision, WriteDecisionKind

REPO_ROOT = Path(__file__).resolve().parents[2]


def _tree(root: Path, relative: str) -> ast.Module:
    return ast.parse((root / relative).read_bytes(), filename=relative)


def _function(tree: ast.Module, name: str) -> ast.FunctionDef:
    return next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == name)


def _assignment(node: ast.AST, name: str) -> ast.expr:
    for part in ast.walk(node):
        if isinstance(part, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in part.targets):
            return part.value
        if isinstance(part, ast.AnnAssign) and isinstance(part.target, ast.Name) and part.target.id == name:
            return part.value if part.value is not None else _missing(name)
    return _missing(name)


def _missing(name: str) -> ast.expr:
    raise ValueError(f"Missing binding definition: {name}")


def _callee(call: ast.Call) -> str:
    return call.func.id if isinstance(call.func, ast.Name) else call.func.attr if isinstance(call.func, ast.Attribute) else ""


def _asset_calls(node: ast.AST) -> tuple[tuple[str, int], ...]:
    return tuple((part.args[0].value, part.lineno) for part in ast.walk(node)
                 if isinstance(part, ast.Call) and _callee(part) in {"asset_file", "profile_asset_file"}
                 and part.args and isinstance(part.args[0], ast.Constant) and isinstance(part.args[0].value, str))


def _asset_dependencies(node: ast.AST, name: str, seen: frozenset[str] = frozenset()) -> frozenset[str]:
    """Follow local assignments from a digest input back to profile asset calls."""
    if name in seen:
        return frozenset()
    keys: set[str] = set()
    for part in ast.walk(node):
        if not isinstance(part, ast.Assign) or not any(isinstance(target, ast.Name) and target.id == name for target in part.targets):
            continue
        keys.update(key for key, _ in _asset_calls(part.value))
        for reference in ast.walk(part.value):
            if isinstance(reference, ast.Name):
                keys.update(_asset_dependencies(node, reference.id, seen | {name}))
    return frozenset(keys)


def _predicate(node: ast.expr, variable: str, key: str) -> bool:
    """Evaluate only the cover asset filter's membership/boolean grammar."""
    if isinstance(node, ast.BoolOp):
        values = [_predicate(value, variable, key) for value in node.values]
        return all(values) if isinstance(node.op, ast.And) else any(values)
    if isinstance(node, ast.Compare) and len(node.ops) == 1 and len(node.comparators) == 1:
        left, right = node.left, node.comparators[0]
        if isinstance(left, ast.Constant) and isinstance(left.value, str) and isinstance(right, ast.Name) and right.id == variable:
            contained = left.value in key
        elif isinstance(left, ast.Name) and left.id == variable:
            choices: object = ast.literal_eval(right)
            if not isinstance(choices, (tuple, list, set, frozenset)):
                raise ValueError("Unsupported cover binding membership")
            contained = key in choices
        else:
            raise ValueError("Unsupported cover binding predicate")
        if isinstance(node.ops[0], ast.In):
            return contained
        if isinstance(node.ops[0], ast.NotIn):
            return not contained
    raise ValueError("Unsupported cover binding predicate")


def asset_binding_surfaces(
    asset_key: str, *, repo_root: Path = REPO_ROOT, profile_id: str = "lidousha",
) -> tuple[AssetBindingSurface, ...]:
    """Derive surfaces from the actual stage, fingerprint and receipt owners.

    ``profile.json`` denotes the manifest itself. Unknown future asset keys are
    also checked against the cover filter, even before a profile registers them.
    A changed/unrecognized definition raises rather than claiming no bindings.
    """
    runner_ref = "scripts/session_autoslice.py"
    runner = _tree(repo_root, runner_ref)
    fingerprints_ref = "src/autoslice/runner_pipeline_fingerprints.py"
    fingerprints = _tree(repo_root, fingerprints_ref)
    surfaces: list[AssetBindingSurface] = []

    def add(kind: BindingSurfaceKind, relative: str, line: int, detail: str) -> None:
        surface = AssetBindingSurface(kind, f"{relative}:{line}", detail)
        if surface not in surfaces:
            surfaces.append(surface)

    stages = _assignment(runner, "stage_asset_keys")
    if not isinstance(stages, ast.Dict):
        raise ValueError("stage_asset_keys must remain a literal mapping")
    for stage, keys in zip(stages.keys, stages.values, strict=True):
        if stage is None or not isinstance(stage, ast.Constant) or not isinstance(stage.value, str):
            raise ValueError("Invalid stage binding")
        if asset_key in ast.literal_eval(keys):
            add("TALK_STAGE_SNAPSHOT", runner_ref, keys.lineno, stage.value)

    # Session authority enters the text snapshot through consumer_inputs, rather
    # than the stage's direct file list. Join the actual resolver call to that key.
    consumer_keys = _assignment(_function(fingerprints, "talk_dependency_snapshot"), "input_keys")
    if not isinstance(consumer_keys, ast.Dict):
        raise ValueError("Invalid snapshot consumer_inputs")
    for call in ast.walk(runner):
        if not isinstance(call, ast.Call) or _callee(call) != "resolve_session_relation":
            continue
        ledger_keys = tuple(key for key, _ in _asset_calls(call))
        if asset_key not in ledger_keys:
            continue
        for stage, keys in zip(consumer_keys.keys, consumer_keys.values, strict=True):
            if stage is not None and isinstance(stage, ast.Constant) and "session_relation_authority" in ast.literal_eval(keys):
                add("TALK_STAGE_SNAPSHOT", fingerprints_ref, keys.lineno, str(stage.value))
        authority_ref = "src/autoslice/session_relation_authority.py"
        authority = _function(_tree(repo_root, authority_ref), "resolve_session_relation")
        sha_calls = [part for part in ast.walk(authority) if isinstance(part, ast.Call) and _callee(part) == "_sha256"
                     and any(isinstance(arg, ast.Name) and arg.id == "ledger_path" for arg in part.args)]
        if not sha_calls:
            raise ValueError("Session authority receipt binding changed")
        add("AUTHORITY_RECEIPT_SHA", authority_ref, sha_calls[0].lineno, "ledger_sha256")

    song = _function(fingerprints, "song_pipeline_fingerprint")
    for key, line in _asset_calls(song):
        if key == asset_key:
            add("SONG_PROOF_CLOSURE", fingerprints_ref, line, "song_pipeline_fingerprint")
    if asset_key == "profile.json":
        manifest = next(part for part in ast.walk(song) if isinstance(part, ast.Constant) and part.value == "profile.json")
        add("SONG_PROOF_CLOSURE", fingerprints_ref, manifest.lineno, "manifest")

    profile_ref = f"profiles/{profile_id}/profile.json"
    profile = json.loads((repo_root / profile_ref).read_bytes())
    broad = _function(fingerprints, "pipeline_fingerprint")
    if not any(isinstance(part, ast.Call) and _callee(part) == "fingerprint_paths" for part in ast.walk(broad)):
        raise ValueError("Broad profile fingerprint binding changed")
    if asset_key == "profile.json" or asset_key in profile["assets"]["pipeline_fingerprint_files"]:
        add("BROAD_PIPELINE_FINGERPRINT", profile_ref, 1, "fingerprint_paths")

    audit_ref = "src/autoslice/review_package_policy_fingerprint.py"
    for key, line in _asset_calls(_function(_tree(repo_root, audit_ref), "build_policy_fingerprint")):
        if key == asset_key:
            add("PACKAGE_AUDIT_POLICY", audit_ref, line, "build_policy_fingerprint")

    cover_ref = "src/autoslice/normal_cover_generation_checkpoint.py"
    cover = _function(_tree(repo_root, cover_ref), "generation_identity")
    filters = [part for part in ast.walk(cover) if isinstance(part, ast.GeneratorExp)
               and isinstance(part.elt, ast.Call) and _callee(part.elt) == "asset_file"]
    if len(filters) != 1 or len(filters[0].generators) != 1:
        raise ValueError("Cover asset binding definition changed")
    generator = filters[0].generators[0]
    if not isinstance(generator.target, ast.Name) or not generator.ifs:
        raise ValueError("Cover asset binding filter changed")
    if all(_predicate(condition, generator.target.id, asset_key) for condition in generator.ifs):
        add("COVER_GENERATION_CHECKPOINT", cover_ref, filters[0].lineno, "generation_identity")
    if asset_key == "profile.json":
        manifest_attr = next(part for part in ast.walk(cover) if isinstance(part, ast.Attribute) and part.attr == "manifest_path")
        add("COVER_GENERATION_CHECKPOINT", cover_ref, manifest_attr.lineno, "manifest")

    child = _function(runner, "child_env")
    for loop in ast.walk(child):
        if not isinstance(loop, ast.For) or not any(isinstance(part, ast.Call) and _callee(part) == "bind_runtime_candidate_asset" for part in ast.walk(loop)):
            continue
        for row in ast.literal_eval(loop.iter):
            if row[0] == asset_key:
                add("RUNTIME_CANDIDATE_BINDING", runner_ref, loop.lineno, row[2])
    # Some runtime selections bind their digest directly rather than through
    # bind_runtime_candidate_asset. Follow their actual local alias chain.
    for assignment in ast.walk(child):
        if not isinstance(assignment, ast.Assign):
            continue
        digest_targets = [target.slice.value for target in assignment.targets
                          if isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name)
                          and target.value.id == "env" and isinstance(target.slice, ast.Constant)
                          and isinstance(target.slice.value, str) and target.slice.value.endswith("_SHA256")]
        for call in ast.walk(assignment.value):
            if not isinstance(call, ast.Call) or _callee(call) != "_sha256_regular_file" or not call.args or not isinstance(call.args[0], ast.Name):
                continue
            if asset_key in _asset_dependencies(child, call.args[0].id):
                for target_key in digest_targets:
                    add("RUNTIME_CANDIDATE_BINDING", runner_ref, assignment.lineno, target_key)

    frozen_ref = "src/autoslice/producer_source_truth_checkpoint.py"
    frozen_tree = _tree(repo_root, frozen_ref)
    frozen_calls = [part for part in ast.walk(_function(frozen_tree, "freeze_ledger"))
                    if isinstance(part, ast.Call) and _callee(part) == "_frozen_binding"]
    if not frozen_calls:
        raise ValueError("Source truth frozen binding definition changed")
    # Join the origin asset assignment to the exact attribute passed to freeze.
    for assignment in ast.walk(frozen_tree):
        if not isinstance(assignment, ast.Assign):
            continue
        for target in assignment.targets:
            if not isinstance(target, ast.Attribute) or asset_key not in {key for key, _ in _asset_calls(assignment.value)}:
                continue
            if any(isinstance(call, ast.Call) and _callee(call) == "freeze_ledger"
                   and call.args and isinstance(call.args[0], ast.Attribute) and call.args[0].attr == target.attr
                   for call in ast.walk(frozen_tree)):
                add("AUTHORITY_RECEIPT_SHA", frozen_ref, frozen_calls[0].lineno, "source-truth-input/ledger")
    return tuple(surfaces)


def plan_knowledge_write(asset_key: str, *, inflight: Sequence[InflightBinding]) -> WriteDecision:
    surfaces = asset_binding_surfaces(asset_key)
    kinds = {surface.kind for surface in surfaces}
    # Callers supply bindings for K's current bytes. A pinned deployment is a
    # blocker until the owner confirms isolation on the old deploy or terminality.
    blocking = tuple(item for item in inflight if item.surface in kinds and item.pinned_deploy_sha is not None)
    decision: WriteDecisionKind = "DEFER_UNTIL_TERMINAL" if blocking else "DEPLOY_CLASS_CHANGE" if surfaces else "WRITE_ANYTIME"
    return WriteDecision(asset_key, decision, surfaces, blocking)
