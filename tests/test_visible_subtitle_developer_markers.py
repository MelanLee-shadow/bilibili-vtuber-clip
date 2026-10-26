"""Offline surface checks; no production writer is wired by Part A."""

import ast
from pathlib import Path
import re

from src.autoslice.subtitle_acceptability_policy import load_policy

ROOT = Path(__file__).resolve().parents[1]


def _markers(text):
    return [
        pattern
        for pattern in load_policy()["disclosure"]["developer_marker_patterns"]
        if re.search(pattern, text, re.I)
    ]





def test_uploader_fixed_public_description_has_no_internal_markers():
    # Read literal source only: importing the uploader is unnecessary and would
    # load external-client configuration. Resolve just its fixed description.
    tree = ast.parse((ROOT / "scripts/authorized_upload.py").read_text())
    values = {}
    wanted = {
        "PROJECT_ATTRIBUTION_LINE",
        "SUBMISSION_DESCRIPTION",
        "EXPECTED_SOURCE",
        "DEFAULT_DESCRIPTION",
    }

    def evaluate(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Name):
            return values[node.id]
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return evaluate(node.left) + evaluate(node.right)
        if isinstance(node, ast.Call) and not node.keywords:
            if (isinstance(node.func, ast.Attribute) and node.func.attr == "strip"
                    and not node.args):
                return evaluate(node.func.value).strip()
            if (isinstance(node.func, ast.Attribute) and node.func.attr == "get"
                    and isinstance(node.func.value, ast.Attribute)
                    and isinstance(node.func.value.value, ast.Name)
                    and node.func.value.value.id == "os" and node.func.value.attr == "environ"
                    and len(node.args) == 2
                    and isinstance(node.args[0], ast.Constant)
                    and node.args[0].value == "AUTOSLICE_UPLOAD_DESC"):
                return evaluate(node.args[1])  # Fixed default; never read local environment.
        raise AssertionError("description contract contains an unsupported expression")

    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id in wanted
        ):
            values[node.targets[0].id] = evaluate(node.value)
    assert set(values) == wanted
    assert not _markers(values["DEFAULT_DESCRIPTION"])


def test_all_developer_marker_forms_are_internal_only():
    p = load_policy()
    assert p["disclosure"]["audience_surfaces"] == ["subtitle", "title", "cover", "description"]
    assert p["disclosure"]["audience_visible_allowed"] is False
    for text in [
        "听不清",
        "无法确定",
        "无法依听音确定",
        "（？）",
        "(?)",
        "[unclear]",
        "UNRESOLVED_BY_AUDIO",
    ]:
        assert _markers(text), text
