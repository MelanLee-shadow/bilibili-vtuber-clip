#!/usr/bin/env python3
"""Read-only replay of one current title/cover choice using the private font."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.autoslice.title_cover_candidates import sha, validate_choice_bundle
from src.autoslice.package_import_candidate_cover import candidate_item
from src.autoslice.title_cover_package import package_choice_problems


def audit(publish_json: Path, package_root: Path | None = None) -> dict:
    publish = json.loads(publish_json.read_text(encoding="utf-8"))
    generation = publish["cover_generation"]
    binding = generation["candidate_selection"]
    if package_root is None:
        problems = validate_choice_bundle(generation)
        choose = Path(binding["choose_path"])
    else:
        item = candidate_item(package_root, binding["candidate_id"])
        problems = package_choice_problems(package_root, item, generation)
        choose = package_root / item["cover_choice"]
    return {
        "schema_version": "lidousha-title-cover-readback.v1",
        "status": "PASS" if not problems else "BLOCKED",
        "candidate_id": binding["candidate_id"],
        "choose_path": str(choose),
        "choose_sha256": sha(choose) if choose.is_file() else None,
        "publish_path": str(publish_json),
        "publish_sha256": sha(publish_json),
        "selected_id": binding["selected_id"],
        "problems": problems,
        "read_only": True,
        "provider_calls": 0,
        "publication_authorized": False,
        "scope": "Candidate evidence replay only; final package audit and joint-QC remain required.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--publish-json", type=Path, required=True)
    parser.add_argument("--package-root", type=Path)
    parser.add_argument("--out", type=Path, required=True, help="Create-only audit receipt")
    args = parser.parse_args()
    try:
        result = audit(
            args.publish_json.absolute(),
            args.package_root.absolute() if args.package_root else None,
        )
    except (OSError, ValueError, KeyError, TypeError) as exc:
        result = {
            "schema_version": "lidousha-title-cover-readback.v1",
            "status": "BLOCKED",
            "problems": [str(exc)],
            "read_only": True,
            "provider_calls": 0,
            "publication_authorized": False,
        }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("x", encoding="utf-8") as destination:
        destination.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {"status": result["status"], "receipt": str(args.out.absolute())}, ensure_ascii=False
        )
    )
    return 0 if result["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
