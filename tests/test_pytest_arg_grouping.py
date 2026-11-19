"""Root conftest.pytest_configure: test paths are grouped by directory, so no file loses its directory's conftest."""

import importlib.util
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("root_conftest_for_grouping", ROOT / "conftest.py")   # "conftest" is ambiguous
assert _spec and _spec.loader
_root_conftest = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_root_conftest)
_grouped_by_directory = _root_conftest._grouped_by_directory


def test_paths_of_one_directory_come_together_in_first_seen_order():
    args = ["tests/manual_lane/test_a.py", "tests/test_b.py", "tests/manual_lane/test_c.py::t", "tests/test_d.py"]
    assert _grouped_by_directory(args) == [
        "tests/manual_lane/test_a.py", "tests/manual_lane/test_c.py::t", "tests/test_b.py", "tests/test_d.py"]


def test_a_file_separated_from_its_directory_on_the_command_line_still_gets_that_directorys_conftest():
    # 11-18: tests/manual_lane/x.py tests/y.py tests/manual_lane/z.py -- z ran without tests/manual_lane/conftest.py
    probe = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         # whole files: a node id as the first path does not show it
         "tests/manual_lane/test_conftest_probe_peer.py", "tests/test_pytest_arg_grouping_separator.py",
         "tests/manual_lane/test_conftest_reaches_every_file.py"],
        cwd=ROOT, capture_output=True, text=True, timeout=300)
    assert probe.returncode == 0, probe.stdout[-2000:]
