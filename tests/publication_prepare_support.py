"""Synthetic package dependencies; no repository publication authority."""
from pathlib import Path
import json
from src.autoslice.publication_readiness import READY_TO_PREPARE

def _repository(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    return repo

def _row(tmp_path: Path, *, candidate_id: str = "candidate-1") -> tuple[Path, dict]:
    runtime = tmp_path / "runtime"
    package = runtime / "out/2026-08-21/candidate-1/replacement_recuts"
    package.mkdir(parents=True)
    files = {
        "record": package / f"{candidate_id}.record.json",
        "publish": package / f"{candidate_id}.publish.json",
        "burned_video": package / f"{candidate_id}.mp4",
        "cover": package / f"{candidate_id}.cover.png",
        "review_manifest": package / "review_manifest.json",
        "package_audit": package / "package_audit.json",
        "title_cover_qc": package / f"{candidate_id}.title-cover-joint-qc.json",
    }
    files["record"].write_text(
        json.dumps({"candidate_id": candidate_id}), encoding="utf-8"
    )
    files["publish"].write_text(
        json.dumps(
            {
                "candidate_id": candidate_id,
                "title": "【示例频道】合格测试标题",
            }
        ),
        encoding="utf-8",
    )
    for key in (
        "burned_video",
        "cover",
        "review_manifest",
        "package_audit",
        "title_cover_qc",
    ):
        files[key].write_bytes((key + "\n").encode())
    return runtime, {
        "candidate_id": candidate_id,
        "recording_date": "2026-08-21",
        "lane": "talk",
        "category": READY_TO_PREPARE,
        "reason_codes": ["LOCAL_PREPARATION_AVAILABLE"],
        "package_dependencies": {key: str(path) for key, path in files.items()},
    }
