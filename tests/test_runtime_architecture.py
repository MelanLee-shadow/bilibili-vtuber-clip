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
