"""Cross-stage protocol fixtures; no real provider or content-quality claim."""
import copy
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from src.autoslice import producer_text_pipeline as pipeline
from src.autoslice.producer_text_checkpoint import write_source_text_checkpoint
from src.autoslice.retained_source_input import load_retained_source_input
from tests.test_source_checkpoint_parent import parent_fixture
from tests.test_producer_current_text_auto_resume import (
    _pipeline_kwargs, _stub_source_stage_reconstruction,
)
from tests.test_retained_source_input import _files, _forbidden


def _cold_source_parent(input_path):
    """Enter the normal retained loader and pipeline in a genuinely new process."""
    payload = json.loads(Path(input_path).read_text())
    c = payload["checkpoint"]
    for key in ("padded", "out_root", "authority_path", "context_path"):
        c[key] = Path(c[key])
    spec, output = payload["spec"], Path(payload["output"])
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("AUTOSLICE_BASE", spec["runtime_root"])
        _stub_source_stage_reconstruction(patch, c)
        patch.setattr(pipeline, "_build_final_review_llm_call", _forbidden)
        load_retained_source_input(spec=spec, cid=c["cid"], padded=c["padded"], out_root=output)
        result = pipeline.run_text_pipeline(
            **{**_pipeline_kwargs(c), "spec": spec, "out_root": output})
        return {
            "pid": os.getpid(),
            "count": result.chat_authority_audit["cpa_adjudication_usage"]["provider_adjudication_count"],
            "boundary": result.chat_authority_audit["final_review_audit"]["boundary_semantic_review"]["status"],
            "srt_equal": result.srt_text == c["srt"],
            "release_authorized": result.chat_authority_audit["source_input_observation_parent"]["release_authorized"],
        }


@pytest.mark.parametrize("damage", [False, True], ids=["later_usage", "successor_drift"])
def test_source_parent_prefers_real_saved_successor_in_fresh_process(tmp_path, monkeypatch, damage):
    c, spec, output = parent_fixture(tmp_path, monkeypatch)
    parent_before = _files(c["out_root"])
    output.mkdir(parents=True)
    load_retained_source_input(spec=spec, cid=c["cid"], padded=c["padded"], out_root=output)
    monkeypatch.setattr(pipeline, "_build_final_review_llm_call", _forbidden)
    first = pipeline.run_text_pipeline(**{**_pipeline_kwargs(c), "spec": spec, "out_root": output})
    assert first.chat_authority_audit["cpa_adjudication_usage"]["provider_adjudication_count"] == 4
    # Explicit synthetic later history, not a claim of two real judge dispatches.
    # Save through the ordinary SOURCE emitter, so cold entry must prefer its
    # immutable successor over the still-declared external parent count of four.
    authority = copy.deepcopy(first.chat_authority_audit)
    authority["cpa_adjudication_usage"]["provider_adjudication_count"] = 6
    authority["cpa_adjudication_usage"]["prior_provider_adjudication_count"] = 4
    authority["final_review_audit"]["cpa_adjudication_usage"] = copy.deepcopy(
        authority["cpa_adjudication_usage"])
    live = output / "later-authority-fixture.json"
    live.write_text(json.dumps(authority))
    checkpoint = output / f'{c["cid"]}.source-text-checkpoint.json'
    write_source_text_checkpoint(
        checkpoint, candidate_id=c["cid"], srt_path=c["out_root"] / "padded.fresh.srt",
        authority_path=live, context_path=first.clip_context_path, padded=c["padded"])
    native = json.loads(checkpoint.read_text())
    if damage:
        frozen = Path(native["authority"]["path"])
        frozen.write_bytes(frozen.read_bytes() + b" ")
    payload = tmp_path / "cold-input.json"
    payload.write_text(json.dumps({
        "checkpoint": {k: str(v) if isinstance(v, Path) else v for k, v in c.items()},
        "spec": spec, "output": str(output),
    }))
    command = [sys.executable, "-B", "-c",
        "import json,sys; from tests.test_xiamen_relisten_integration import _cold_source_parent; "
        "print(json.dumps(_cold_source_parent(sys.argv[1])))", str(payload)]
    cold = subprocess.run(command, capture_output=True, text=True, timeout=45)
    (tmp_path / "cold-TERMINAL.json").write_text(json.dumps({
        "returncode": cold.returncode, "stdout": cold.stdout, "stderr": cold.stderr,
        "fixture_only_later_count": 6, "real_provider_calls": 0,
    }))
    if damage:
        assert cold.returncode != 0
        assert "AUTHORITY" in cold.stderr and "DRIFT" in cold.stderr
        assert not cold.stdout.strip()
    else:
        assert cold.returncode == 0, cold.stderr
        result = json.loads(cold.stdout)
        assert result["pid"] != os.getpid()
        assert result["count"] == 6
        assert result["boundary"] == "BLOCK" and result["srt_equal"]
        assert result["release_authorized"] is False
    assert _files(c["out_root"]) == parent_before


def test_public_configure_api_rejects_web_conflict_before_creating_state(tmp_path):
    from tests.test_final_media_review_version_binding import install, seal, SOURCE_FILES, REPO
    from tests.test_final_media_review_gemini_api_runtime import _runtime_tree
    from src.autoslice.final_media_review_gemini_api_runtime import MODEL_ENV
    from src.autoslice.final_media_review_model_capability_sentinel import SENTINEL_RUNTIME_FILENAME
    from src.autoslice.runtime_code_lease import VERSION_ENV
    root, env = _runtime_tree(tmp_path)
    repo = install(root, "a" * 40)
    for name in ("src", "scripts"):
        shutil.copytree(REPO / name, repo / name, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for relative in SOURCE_FILES:
        (repo / relative).chmod(0o755 if relative.startswith("scripts/") else 0o644)
    seal(repo, "a" * 40)
    wrapper = repo.parent / "runtime-code-lease.py"
    shutil.copyfile(REPO / "src/autoslice/runtime_code_lease.py", wrapper)
    selected = {**os.environ, **env, "AUTOSLICE_FINAL_MEDIA_GEMINI_WEB_ENABLED": "1"}
    selected.pop(VERSION_ENV, None)
    selected.pop("PYTHONPATH", None)
    command = [sys.executable, "-B", str(wrapper), str(repo),
        str(repo / "scripts/admit_final_media_review.py"), "configure-api",
        "--runtime-root", str(root), "--model", env[MODEL_ENV]]
    result = subprocess.run(command, env=selected, capture_output=True, text=True, timeout=45)
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert json.loads(result.stderr)["reason"] == "FINAL_MEDIA_REVIEW_ROUTE_CONFLICT"
    assert not (root / SENTINEL_RUNTIME_FILENAME).exists()
    assert not (root / "final-media-review-raw-av-capability.json").exists()
