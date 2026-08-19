"""Generate the lidousha-title-cover-joint-qc.v1 receipt via a real CPA call.

Run on free from /opt/bilive/autoslice/repo:
  python3 scripts/run_title_cover_joint_qc.py <package_root> <title> <out_path>

2026-08-09 收编自 free:/tmp/run_title_cover_joint_qc.py(受骗片会话手作工具):
身份行按 assets/lidousha/persona.md 修正——墨镜是可选配饰不是身份特征
(v1 把"墨镜"写成必备,lidousha_primary 系统性假阴性,213135 案 1P2F)。
The verdict is the exact parsed CPA answer (validator replays this bond);
any gate the model fails leaves status=FAIL and the upload chain stops.
"""
import hashlib, json, os, stat, sys, time
from pathlib import Path

sys.path.insert(0, ".")
from src.autoslice.cpa_frame_witness import image_vision_probe


def load_env(path: str) -> dict:
    out = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        if "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def resolve_package_inputs(
    package_root: Path, title: str
) -> tuple[dict, dict, Path]:
    """Resolve the exact same-stem upload cover after daily assembly."""

    def strict_regular(
        path: Path, *, label: str, root: Path | None = None
    ) -> Path:
        absolute = path.absolute()
        cursor = Path(absolute.anchor)
        for component in absolute.parts[1:]:
            cursor /= component
            info = os.lstat(cursor)
            if stat.S_ISLNK(info.st_mode):
                raise ValueError(f"{label} traverses a symlink")
        if not stat.S_ISREG(os.lstat(absolute).st_mode):
            raise ValueError(f"{label} is not a regular file")
        resolved = absolute.resolve(strict=True)
        if root is not None and not resolved.is_relative_to(root):
            raise ValueError(f"{label} escapes package root")
        return resolved

    package_absolute = package_root.absolute()
    package_info = os.lstat(package_absolute)
    if stat.S_ISLNK(package_info.st_mode) or not stat.S_ISDIR(package_info.st_mode):
        raise ValueError("package root is not a real directory")
    package_root = package_absolute.resolve(strict=True)
    review_path = package_root / "review_manifest.json"
    review_path = strict_regular(
        review_path, label="review manifest", root=package_root
    )
    review = json.loads(review_path.read_text(encoding="utf-8"))
    items = review.get("items")
    if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
        raise ValueError("review manifest must contain exactly one item")
    item = items[0]
    if item.get("title") != title:
        raise ValueError("review manifest title differs from joint-QC title")

    def package_file(key: str, *, direct: bool = True) -> Path:
        raw = item.get(key)
        if not isinstance(raw, str) or not raw or Path(raw).is_absolute():
            raise ValueError(f"review manifest {key} is not package-relative")
        relative = Path(raw)
        if any(part in {"", ".", ".."} for part in relative.parts):
            raise ValueError(f"review manifest {key} has an unsafe component")
        path = strict_regular(
            package_root / relative,
            label=f"review manifest {key}",
            root=package_root,
        )
        if direct and path.parent != package_root:
            raise ValueError(f"review manifest {key} is not a package-root file")
        return path

    video_path = package_file("video")
    if video_path.suffix != ".mp4":
        raise ValueError("review manifest video is not MP4")
    upload_stem = video_path.name.removesuffix(".mp4")
    expected_cover_name = f"{upload_stem}.cover.png"
    expected_record_name = f"{upload_stem}.record.json"
    if item.get("cover") != expected_cover_name:
        raise ValueError("review manifest cover is not same-stem with video")
    if item.get("record") != expected_record_name:
        raise ValueError("review manifest record is not same-stem with video")
    record_path = package_file("record")
    publish_path = package_file("publish_json")
    cover_path = package_file("cover")
    record = json.loads(record_path.read_text(encoding="utf-8"))
    publish = json.loads(publish_path.read_text(encoding="utf-8"))
    if publish.get("title") != title:
        raise ValueError("publish title differs from joint-QC title")
    generation = publish.get("cover_generation")
    if not isinstance(generation, dict):
        raise ValueError("publish cover generation is missing")
    generated_raw = generation.get("final_cover") or publish.get("cover_path")
    if not isinstance(generated_raw, str) or not generated_raw:
        raise ValueError("publish final cover path is missing")
    generated_path = Path(generated_raw)
    if not generated_path.is_absolute():
        generated_path = package_root / generated_path
    generated_path = generated_path.absolute()
    same_stem_sha = hashlib.sha256(cover_path.read_bytes()).hexdigest()
    declared_sha = str(generation.get("final_cover_sha256") or "").removeprefix(
        "sha256:"
    )
    # Song review packages are copied wholesale from the delivery location
    # into an independent package root (build_lidousha_song_review_manifest.py);
    # publish.json's cover_generation.final_cover is copied byte-for-byte and
    # still names the pre-copy delivery path, which lives outside this
    # package root and may not even exist any more.  Detect that lexically
    # (no filesystem access outside the package) before ever touching the
    # path, and fall back to the package's own same-stem cover -- already
    # proven a real in-package regular file above -- only if its actual bytes
    # hash matches the frozen cover-generation hash recorded in publish.json.
    # Talk packages keep the video/publish/cover co-located, so their
    # final_cover always resolves inside the package root and this branch is
    # never taken for them.
    lexical_generated = Path(os.path.normpath(str(generated_path)))
    if not lexical_generated.is_relative_to(package_root):
        if not declared_sha or same_stem_sha != declared_sha:
            raise ValueError(
                "publish final cover escapes package root and the package's "
                "same-stem cover does not match the frozen cover generation "
                "hash"
            )
        return record, publish, cover_path
    generated_path = strict_regular(
        generated_path, label="publish final cover", root=package_root
    )
    generated_sha = hashlib.sha256(generated_path.read_bytes()).hexdigest()
    if not declared_sha or generated_sha != declared_sha or same_stem_sha != declared_sha:
        raise ValueError("same-stem cover differs from frozen cover generation")
    return record, publish, cover_path


def main() -> int:
    package_root = Path(sys.argv[1]).resolve()
    title = sys.argv[2]
    out_path = Path(sys.argv[3])
    env = load_env("/opt/bilive/autoslice/cpa.env")

    record, publish, cover_path = resolve_package_inputs(package_root, title)
    story = record.get("story_contract") or {}
    candidate_id = str(story.get("candidate_id") or record.get("delivery_candidate_id") or "")
    cover_sha = hashlib.sha256(cover_path.read_bytes()).hexdigest()

    question = (
        "你是李豆沙频道的标题+封面联合质检员。下图是最终封面,拟用标题是:\n"
        f"《{title}》\n"
        "请只输出一个 JSON 对象(不要 markdown 代码块,不要多余文字),字段与含义:\n"
        '{"lidousha_primary": bool 封面主体是否是李豆沙(白发+头顶小熊猫耳的虚拟熊猫少女;熊猫耳长在头上不是头套/帽子;头顶墨镜或发饰是可选配饰,可有可无),'
        '"thumbnail_readable": bool 缩略图尺寸下封面大字是否清晰可读,'
        '"single_clear_hook": bool 封面文案是否构成一个清晰单一的钩子,'
        '"text_overcrowded": bool 文字是否过度拥挤,'
        '"title_cover_aligned": bool 标题与封面是否讲同一件事,'
        '"physical_text_line_count": int 封面主文案的物理行数,'
        '"unrelated_or_misleading_elements": [] 与内容无关或误导的元素列表(没有就空数组),'
        '"reason": str 一句话理由,'
        '"pass": bool 综合是否通过}\n'
        "如实判断,不要迎合。"
    )
    witness = image_vision_probe(
        cover_path,
        question,
        api_base=env.get("CPA_BASE_URL", ""),
        api_key=env.get("CPA_API_KEY", ""),
    )
    witness["image_path"] = str(cover_path)
    verdict = None
    answer = witness.get("answer")
    if isinstance(answer, str):
        try:
            cleaned = answer.strip()
            if cleaned.startswith("```"):
                cleaned = cleaned.strip("`\n")
                cleaned = cleaned[cleaned.find("{"):]
            start, end = cleaned.find("{"), cleaned.rfind("}")
            cleaned = cleaned[start:end + 1]
            verdict = json.loads(cleaned)
            witness["answer"] = cleaned
        except ValueError:
            verdict = None
    ok = (
        isinstance(verdict, dict)
        and witness.get("status") == "OBSERVED"
        and verdict.get("lidousha_primary") is True
        and verdict.get("thumbnail_readable") is True
        and verdict.get("single_clear_hook") is True
        and verdict.get("text_overcrowded") is False
        and verdict.get("title_cover_aligned") is True
        and verdict.get("physical_text_line_count") in (1, 2)
        and verdict.get("unrelated_or_misleading_elements") == []
        and verdict.get("pass") is True
    )
    receipt = {
        "schema_version": "lidousha-title-cover-joint-qc.v1",
        "candidate_id": candidate_id,
        "title": title,
        "title_sha256": "sha256:" + hashlib.sha256(title.encode("utf-8")).hexdigest(),
        "cover_path": str(cover_path),
        "cover_sha256": "sha256:" + cover_sha,
        "preferred_provider": "cpa",
        "selected_provider": "cpa",
        "witness": witness,
        "verdict": verdict,
        "status": "PASS" if ok else "FAIL",
        "pass": bool(ok),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print("status:", receipt["status"], "| verdict:", json.dumps(verdict, ensure_ascii=False)[:200])
    print("receipt:", out_path)
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
