"""Generate the lidousha-title-cover-joint-qc.v1 receipt via a real CPA call.

Run on free from /opt/bilive/autoslice/repo:
  python3 scripts/run_title_cover_joint_qc.py <package_root> <title> <out_path>

2026-08-09 收编自 free:/tmp/run_title_cover_joint_qc.py(受骗片会话手作工具):
身份行按 assets/lidousha/persona.md 修正——墨镜是可选配饰不是身份特征
(v1 把"墨镜"写成必备,lidousha_primary 系统性假阴性,213135 案 1P2F)。
The verdict is the exact parsed CPA answer (validator replays this bond);
any gate the model fails leaves status=FAIL and the upload chain stops.
"""
import hashlib, json, sys, time
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


def resolve_delivery_cover(package_root: Path, publish: dict) -> Path:
    """Bind the cover path the upload manifest will actually carry.

    publish 声明的是生成路由(``covers/<...>.png``);daily manifest builder 另外
    在包根装配同茎上传别名 ``<video.stem>.cover.png``,而
    ``authorized_upload make-manifest`` 的 title+cover QC 门比对的是
    ``manifest.cover.path``——即同茎别名。别名存在且逐字节相同就绑别名,QC 回执
    与上传清单才指同一个文件(否则门红:"joint-QC cover_path does not bind final
    cover")。别名缺失或字节漂移时退回 publish 声明路径,由该门 fail-closed。
    """

    declared = Path(
        str(
            (publish.get("cover_generation") or {}).get("final_cover")
            or publish.get("cover_path")
        )
    ).resolve()
    digest = hashlib.sha256(declared.read_bytes()).hexdigest()
    aliases = sorted(
        path
        for path in package_root.glob("*.cover.png")
        if path.is_file()
        and not path.is_symlink()
        and hashlib.sha256(path.read_bytes()).hexdigest() == digest
    )
    if len(aliases) == 1:
        return aliases[0].resolve()
    return declared


def main() -> int:
    package_root = Path(sys.argv[1]).resolve()
    title = sys.argv[2]
    out_path = Path(sys.argv[3])
    env = load_env("/opt/bilive/autoslice/cpa.env")

    record = json.loads(next(package_root.glob("*.record.json")).read_text(encoding="utf-8"))
    story = record.get("story_contract") or {}
    candidate_id = str(story.get("candidate_id") or record.get("delivery_candidate_id") or "")
    publish = json.loads(next(package_root.glob("*.publish.json")).read_text(encoding="utf-8"))
    cover_path = resolve_delivery_cover(package_root, publish)
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
        "authority": "IVAN_EXPLICIT_20260808(title+cover both hand-authorized)",
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print("status:", receipt["status"], "| verdict:", json.dumps(verdict, ensure_ascii=False)[:200])
    print("receipt:", out_path)
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
