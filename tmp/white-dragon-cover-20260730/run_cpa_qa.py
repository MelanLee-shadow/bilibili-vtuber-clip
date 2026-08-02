from __future__ import annotations

import json
import os
import hashlib
from pathlib import Path

from PIL import Image, ImageDraw, ImageOps

from src.autoslice.cover_host_identity_gate import (
    verify_lidousha_final_host_identity,
)
from src.autoslice.cpa_frame_witness import image_vision_probe


ROOT = Path(
    "/opt/bilive/autoslice/reports/cover-only-repairs/"
    "2026-07-30-auto_192000_909_1014-white-milk-dragon"
)
FINAL_SHA256 = (
    "sha256:8bd5fa4f47468f514dc32bc265896405a46289a31ea930ac748234a18c989c06"
)


def sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def build_white_milk_dragon_comparison() -> Path:
    output = ROOT / "white-milk-dragon-reference-final-witness.png"
    canvas = Image.new("RGB", (2560, 1080), (10, 16, 34))
    draw = ImageDraw.Draw(canvas)
    draw.rectangle((0, 0, 920, 72), fill=(22, 66, 124))
    draw.rectangle((920, 0, 2560, 72), fill=(116, 38, 68))
    draw.text((24, 22), "USER WHITE-MILK-DRAGON REFERENCE", fill="white")
    draw.text((944, 22), "FINAL COVER", fill="white")
    with Image.open(ROOT / "60bae315bc53a44bede0582389460d20475948079.png") as source:
        reference = ImageOps.contain(source.convert("RGB"), (900, 988))
    with Image.open(ROOT / "final-cover-edited-v2.png") as source:
        final = ImageOps.contain(source.convert("RGB"), (1620, 988))
    canvas.paste(reference, (10 + (900 - reference.width) // 2, 82))
    canvas.paste(final, (930 + (1620 - final.width) // 2, 82))
    canvas.save(output, format="PNG", optimize=False)
    return output


def parse_json_object(answer: str) -> dict[str, object]:
    start = answer.index("{")
    end = answer.rindex("}") + 1
    value = json.loads(answer[start:end])
    if not isinstance(value, dict):
        raise ValueError("visual QA verdict is not an object")
    return value


def run_white_milk_dragon_visual_qa() -> dict[str, object]:
    comparison = build_white_milk_dragon_comparison()
    question = (
        "左侧是 Ivan 指定的李豆沙“白色奶龙”形象唯一权威参考，右侧是待替换的最终封面。"
        "严格检查右图：1) 最大、最居中的主角仍是李豆沙；2) 右上小贴纸必须明显取自左侧参考，"
        "保留白发、黑色熊猫耳、头顶大墨镜、僵直简化的白色机体和略怪异的小笑脸；"
        "3) 右图不得再有普通白龙、兽类龙、龙角、龙头、龙鳞、翅膀、3D 龙头模型、建模界面或显示器；"
        "4) 封面文字必须完整清楚显示为三行：“白色奶龙”（含引号）、表情小李、拒绝花钱，"
        "没有乱码、伪字、额外文字或水印。任一不确定即 FAIL。只输出 JSON："
        '{"reference_form_matches":true|false,'
        '"reference_features":["实际看到的对应特征"],'
        '"main_lidousha_dominant":true|false,'
        '"unrelated_dragon_absent":true|false,'
        '"incorrect_dragon_elements":["若有则列出"],'
        '"title_text_exact_and_readable":true|false,'
        '"observed_title_lines":["逐行抄录"],'
        '"extra_text_or_watermark_absent":true|false,'
        '"reason":"简短中文说明"}'
    )
    witness = image_vision_probe(
        comparison,
        question,
        api_base=os.environ.get("CPA_BASE_URL", ""),
        api_key=os.environ.get("CPA_API_KEY", ""),
        model="gpt-5.6-sol",
        max_tokens=1200,
    )
    receipt: dict[str, object] = {
        "schema_version": "lidousha-white-milk-dragon-cover-visual-qa.v1",
        "preferred_provider": "cpa",
        "reference_path": str(
            ROOT / "60bae315bc53a44bede0582389460d20475948079.png"
        ),
        "reference_sha256": sha256(
            ROOT / "60bae315bc53a44bede0582389460d20475948079.png"
        ),
        "final_cover_path": str(ROOT / "final-cover-edited-v2.png"),
        "final_cover_sha256": sha256(ROOT / "final-cover-edited-v2.png"),
        "comparison_path": str(comparison),
        "comparison_sha256": sha256(comparison),
        "witness": witness,
    }
    try:
        verdict = parse_json_object(str(witness.get("answer") or ""))
    except (ValueError, json.JSONDecodeError) as exc:
        receipt.update(
            status="FAIL",
            reason_code="VISUAL_QA_VERDICT_UNPARSEABLE",
            detail=f"{type(exc).__name__}: {exc}",
        )
        return receipt
    receipt["verdict"] = verdict
    passed = bool(
        witness.get("provider") == "cpa"
        and witness.get("status") == "OBSERVED"
        and verdict.get("reference_form_matches") is True
        and verdict.get("main_lidousha_dominant") is True
        and verdict.get("unrelated_dragon_absent") is True
        and verdict.get("incorrect_dragon_elements") == []
        and verdict.get("title_text_exact_and_readable") is True
        and verdict.get("observed_title_lines")
        == ["“白色奶龙”", "表情小李", "拒绝花钱"]
        and verdict.get("extra_text_or_watermark_absent") is True
    )
    receipt["status"] = "PASS" if passed else "FAIL"
    if not passed:
        receipt["reason_code"] = "WHITE_MILK_DRAGON_VISUAL_QA_FAILED"
    return receipt


def main() -> None:
    identity_path = ROOT / "cpa-host-identity-verification.v2.json"
    if identity_path.is_file():
        receipt = json.loads(identity_path.read_text(encoding="utf-8"))
    else:
        receipt = verify_lidousha_final_host_identity(
            final_cover_path=ROOT / "final-cover-edited-v2.png",
            final_cover_sha256=FINAL_SHA256,
            reference_path=ROOT / "source-reference.png",
            base_url=os.environ.get("CPA_BASE_URL", ""),
            api_key=os.environ.get("CPA_API_KEY", ""),
        )
        identity_path.write_text(
            json.dumps(receipt, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    print(
        json.dumps(
            {
                "status": receipt.get("status"),
                "reason_code": receipt.get("reason_code"),
                "provider": (receipt.get("witness") or {}).get("provider"),
                "verdict": receipt.get("verdict"),
                "comparison_sha256": receipt.get("comparison_sha256"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    visual_receipt = run_white_milk_dragon_visual_qa()
    (ROOT / "cpa-white-milk-dragon-visual-qa.v1.json").write_text(
        json.dumps(visual_receipt, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "white_milk_dragon_visual_qa": visual_receipt.get("status"),
                "reason_code": visual_receipt.get("reason_code"),
                "provider": (visual_receipt.get("witness") or {}).get("provider"),
                "verdict": visual_receipt.get("verdict"),
                "comparison_sha256": visual_receipt.get("comparison_sha256"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
