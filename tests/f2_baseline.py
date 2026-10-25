"""Load hash-bound baseline source from an exported tree, without Git."""
import hashlib
import json
from pathlib import Path
import types


BASELINE = "8ef35394b84e837dbb1b0a753638a66759dc807d"
BASELINE_MODULES = (
    "nonsemantic_sound",
    "closed_set_proposal_rebuild",
    "targeted_relisten",
    "semantic_terminal_adjudication",
)
REPO = Path(__file__).resolve().parents[1]
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures/f2_baseline"


def baseline_source(name, *, fixture_dir=FIXTURE_DIR):
    if name not in BASELINE_MODULES:
        raise AssertionError(f"F2_BASELINE_MODULE_UNSUPPORTED: {name}")
    manifest = json.loads((fixture_dir / "MANIFEST.json").read_text(encoding="utf-8"))
    if manifest.get("source_commit") != BASELINE:
        raise AssertionError("F2_BASELINE_COMMIT_MISMATCH")
    source_path = f"src/autoslice/{name}.py"
    matches = [row for row in manifest["files"] if row["source_path"] == source_path]
    if len(matches) != 1 or matches[0]["fixture_path"] != f"{name}.py.txt":
        raise AssertionError(f"F2_BASELINE_PATH_MISMATCH: {source_path}")
    row = matches[0]
    source = (fixture_dir / row["fixture_path"]).read_bytes()
    if hashlib.sha256(source).hexdigest() != row["sha256"]:
        raise AssertionError(f"F2_BASELINE_SHA256_MISMATCH: {source_path}")
    return source.decode("utf-8")


def baseline_module(name, *, fixture_dir=FIXTURE_DIR):
    source = baseline_source(name, fixture_dir=fixture_dir)
    source_path = f"src/autoslice/{name}.py"
    module = types.ModuleType(f"src.autoslice._f2_main_{name}")
    module.__package__ = "src.autoslice"
    # Keep the old module's repository-relative asset lookup semantics.
    module.__file__ = str(REPO / source_path)
    exec(compile(source, f"{BASELINE}:{source_path}", "exec"), module.__dict__)
    return module
