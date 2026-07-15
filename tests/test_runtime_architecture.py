from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MAX_ACTIVE_FUNCTION_LINES = 300
SCRIPT_EXCLUSIONS = {
    # Incident-specific forensic repair retained as historical evidence, not a
    # production runtime entry point.
    Path("scripts/repair_false_green_20260709.py"),
}
ENTRY_FILE_LINE_BUDGETS = {
    Path("scripts/produce_slice_package.py"): 500,
    Path("scripts/run_auto_review_shadow_pipeline.py"): 1_150,
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


def test_extracted_entry_files_stay_thin() -> None:
    violations = []
    for relative, budget in ENTRY_FILE_LINE_BUDGETS.items():
        line_count = len((ROOT / relative).read_text(encoding="utf-8").splitlines())
        if line_count > budget:
            violations.append(f"{relative} is {line_count} lines (budget {budget})")
    assert violations == [], "entry-point growth regressed:\n" + "\n".join(violations)
