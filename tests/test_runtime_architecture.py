from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MAX_ACTIVE_FUNCTION_LINES = 300
MAX_ACTIVE_MODULE_LINES = 2_000
SCRIPT_EXCLUSIONS = {
    # Incident-specific forensic repair retained as historical evidence, not a
    # production runtime entry point.
    Path("scripts/repair_false_green_20260709.py"),
}
ENTRY_FILE_LINE_BUDGETS = {
    Path("scripts/produce_slice_package.py"): 500,
    Path("scripts/run_auto_review_shadow_pipeline.py"): 1_150,
    Path("scripts/run_full_session_selector_cpa_shadow.py"): 900,
}
FOCUSED_MODULE_LINE_BUDGETS = {
    # Compatibility/public workflow facades must not absorb extracted domains.
    Path("src/autoslice/chat_authority.py"): 150,
    Path("src/autoslice/song_repair.py"): 1_200,
    Path("src/autoslice/speaker_finalizer.py"): 1_800,
    # Extracted domains retain a small amount of headroom for real behavior,
    # while failing long before another 3k-4k line domain bus can form.
    Path("src/autoslice/chat_evidence.py"): 1_300,
    Path("src/autoslice/chat_repair.py"): 1_050,
    Path("src/autoslice/chat_proposals.py"): 1_250,
    Path("src/autoslice/song_common.py"): 525,
    Path("src/autoslice/song_lrc_provider.py"): 550,
    Path("src/autoslice/song_alignment.py"): 1_175,
    Path("src/autoslice/song_performance.py"): 1_200,
    Path("src/autoslice/speaker_common.py"): 100,
    Path("src/autoslice/speaker_context.py"): 500,
    Path("src/autoslice/speaker_evidence.py"): 625,
    Path("src/autoslice/full_session_transcription.py"): 1_200,
    Path("src/autoslice/boundary_endpoint_binding.py"): 120,
    Path("src/autoslice/producer_boundary_review_stage.py"): 225,
    Path("src/autoslice/review_package_boundary_contract.py"): 350,
}


def _active_runtime_files() -> list[Path]:
    files = [*sorted((ROOT / "src/autoslice").rglob("*.py"))]
    files.extend(
        path
        for path in sorted((ROOT / "scripts").rglob("*.py"))
        if path.relative_to(ROOT) not in SCRIPT_EXCLUSIONS
    )
    return files


def test_active_runtime_functions_stay_bounded() -> None:
    violations: list[str] = []
    for path in _active_runtime_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            line_count = (node.end_lineno or node.lineno) - node.lineno + 1
            if line_count > MAX_ACTIVE_FUNCTION_LINES:
                violations.append(
                    f"{path.relative_to(ROOT)}:{node.lineno} "
                    f"{node.name} is {line_count} lines"
                )
    assert violations == [], (
        f"active runtime functions must stay <= {MAX_ACTIVE_FUNCTION_LINES} lines:\n"
        + "\n".join(violations)
    )


def test_active_runtime_modules_stay_bounded() -> None:
    violations = []
    for path in _active_runtime_files():
        line_count = len(path.read_text(encoding="utf-8").splitlines())
        if line_count > MAX_ACTIVE_MODULE_LINES:
            violations.append(
                f"{path.relative_to(ROOT)} is {line_count} lines "
                f"(global budget {MAX_ACTIVE_MODULE_LINES})"
            )
    assert violations == [], "active runtime module growth regressed:\n" + "\n".join(violations)


def test_extracted_entry_files_stay_thin() -> None:
    violations = []
    for relative, budget in ENTRY_FILE_LINE_BUDGETS.items():
        line_count = len((ROOT / relative).read_text(encoding="utf-8").splitlines())
        if line_count > budget:
            violations.append(f"{relative} is {line_count} lines (budget {budget})")
    assert violations == [], "entry-point growth regressed:\n" + "\n".join(violations)


def test_extracted_domain_modules_stay_focused() -> None:
    violations = []
    for relative, budget in FOCUSED_MODULE_LINE_BUDGETS.items():
        line_count = len((ROOT / relative).read_text(encoding="utf-8").splitlines())
        if line_count > budget:
            violations.append(f"{relative} is {line_count} lines (budget {budget})")
    assert violations == [], "domain-module growth regressed:\n" + "\n".join(violations)


def test_dynamic_boundary_context_cap_is_wired_to_post_authority_review_only() -> None:
    """The spec cap belongs to post-authority boundary review only.

    A previous refactor attached the new keyword to the adjacent
    ``_apply_entity_authority`` call.  Unit tests of the two helpers stayed
    green, while the real producer entry point would have raised ``TypeError``.
    Boundary review must also stay out of ``_run_final_review`` because source
    truth can still delete or renumber cues during finalization.
    """

    path = ROOT / "src/autoslice/producer_text_pipeline.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    calls = {
        node.func.id: {keyword.arg for keyword in node.keywords}
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id
        in {
            "_apply_entity_authority",
            "_run_final_review",
            "review_final_boundary_semantics",
        }
    }

    assert "boundary_max_forward_ms" not in calls["_apply_entity_authority"]
    assert "boundary_max_forward_ms" not in calls["_run_final_review"]
    assert (
        "boundary_max_forward_ms"
        in calls["review_final_boundary_semantics"]
    )


def test_final_text_result_cues_and_receipt_reach_the_same_boundary_resolver() -> None:
    """The resolver must consume one post-authority result, not mixed grids."""

    path = ROOT / "scripts/produce_slice_package.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    main = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "main"
    )
    assignments = [
        node
        for node in ast.walk(main)
        if isinstance(node, ast.Assign) and len(node.targets) == 1
    ]

    def assignment_to(name: str) -> ast.Assign:
        matches = [
            node
            for node in assignments
            if isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == name
        ]
        assert len(matches) == 1
        return matches[0]

    cues_value = assignment_to("cues").value
    assert isinstance(cues_value, ast.Attribute)
    assert isinstance(cues_value.value, ast.Name)
    assert cues_value.value.id == "text_result"
    assert cues_value.attr == "cues"

    chat_value = assignment_to("chat_authority_audit").value
    assert isinstance(chat_value, ast.Attribute)
    assert isinstance(chat_value.value, ast.Name)
    assert chat_value.value.id == "text_result"
    assert chat_value.attr == "chat_authority_audit"

    resolver_calls = [
        node
        for node in ast.walk(main)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "resolve_producer_boundary"
    ]
    assert len(resolver_calls) == 1
    resolver_keywords = {
        keyword.arg: keyword.value
        for keyword in resolver_calls[0].keywords
    }
    assert isinstance(resolver_keywords["cues"], ast.Name)
    assert resolver_keywords["cues"].id == "cues"

    receipt_source = assignment_to("final_review_audit").value
    assert isinstance(receipt_source, ast.BoolOp)
    receipt_call = receipt_source.values[0]
    assert isinstance(receipt_call, ast.Call)
    assert isinstance(receipt_call.func, ast.Attribute)
    assert isinstance(receipt_call.func.value, ast.Name)
    assert receipt_call.func.value.id == "chat_authority_audit"
    assert receipt_call.func.attr == "get"
    assert isinstance(receipt_call.args[0], ast.Constant)
    assert receipt_call.args[0].value == "final_review_audit"

    receipt_projection = [
        node
        for node in assignments
        if isinstance(node.targets[0], ast.Subscript)
        and isinstance(node.targets[0].value, ast.Name)
        and node.targets[0].value.id == "spec"
        and isinstance(node.targets[0].slice, ast.Constant)
        and node.targets[0].slice.value == "boundary_semantic_review"
    ]
    assert len(receipt_projection) == 2
    before, after = sorted(receipt_projection, key=lambda node: node.lineno)
    assert before.lineno < resolver_calls[0].lineno < after.lineno
    assert isinstance(before.value, ast.Name)
    assert before.value.id == "boundary_semantic_review"
    assert isinstance(after.value, ast.Name)
    assert after.value.id == "resolved_boundary_semantic"
