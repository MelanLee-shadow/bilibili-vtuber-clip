from __future__ import annotations

import copy
import hashlib
import json

from src.autoslice import visual_witness
from src.autoslice.cover_generation import (
    _cover_art_direction,
    _cover_prompt,
)
from src.autoslice.cover_host_identity_gate import _GAME_QUESTION, _QUESTION
from src.autoslice.cover_polish_gate import (
    _POLISH_FACE_QUESTION,
    _polish_face_binding_failure,
    _verify_polish_face_integrity,
)
from src.autoslice.cover_source_composition import (
    _GAME_QUESTION_PREFIX,
    _QUESTION_PREFIX,
)


def test_source_and_final_face_questions_preserve_source_eyepatch():
    for question in (_QUESTION_PREFIX, _GAME_QUESTION_PREFIX):
        assert "原本被眼罩遮住的一只眼不算缺失" in question
        assert "直播卡片、文字或其他遮挡物" in question
    assert "眼罩遮住一只眼睛本身不算脸部缺失" in _POLISH_FACE_QUESTION
    assert "眼罩是否符合源图由身份核验另行判断" in _POLISH_FACE_QUESTION
    assert "卡片边框、已渲染文字或新增遮挡物" in _POLISH_FACE_QUESTION
    assert "双眼、嘴巴、下巴都必须" not in _POLISH_FACE_QUESTION
    for question in (_QUESTION, _GAME_QUESTION):
        assert "原有眼罩是身份特征和 intentional occlusion" in question
        assert "不得补画被遮住的眼" in question
        assert "画面边缘、卡片、已渲染文字或新加物体" in question


def _hash(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_polish_eyepatch_pass_is_hash_bound_but_old_fail_stays_failed(
    tmp_path, monkeypatch
):
    cover = tmp_path / "cover.png"
    cover.write_bytes(b"hash-bound-eyepatch-cover")
    captured: dict[str, str] = {}

    def passing_probe(image_path, question, **_kwargs):
        captured["question"] = question
        return {
            "status": "OBSERVED",
            "provider": "cpa",
            "image_sha256": _hash(image_path),
            "answer": json.dumps(
                {
                    "face_complete": True,
                    "missing": [],
                    "tongue_out": False,
                    "reason": "原有眼罩完整保留；可见眼、嘴、下巴和轮廓未被裁切",
                },
                ensure_ascii=False,
            ),
        }

    monkeypatch.setattr(visual_witness, "image_vision_probe", passing_probe)
    verification = _verify_polish_face_integrity(
        cover, base_url="https://cpa.example/v1", api_key="secret"
    )
    generation = {"final_cover_sha256": "sha256:" + _hash(cover)}

    assert verification["status"] == "PASS"
    assert "眼罩遮住一只眼睛本身不算脸部缺失" in captured["question"]
    assert _polish_face_binding_failure(
        generation, verification, "screenshot_polish"
    ) is None

    old_fail = copy.deepcopy(verification)
    old_fail.update(
        status="FAIL",
        reason_code="FACE_INCOMPLETE",
        detail="一只眼睛被眼罩遮挡，双眼未完整可见",
        verdict={
            "face_complete": False,
            "missing": ["eyes"],
            "tongue_out": False,
            "reason": "一只眼睛被眼罩遮挡，双眼未完整可见",
        },
    )
    old_fail["witness"]["answer"] = json.dumps(
        old_fail["verdict"], ensure_ascii=False
    )
    failure = _polish_face_binding_failure(
        generation, old_fail, "screenshot_polish"
    )
    assert failure is not None
    assert "FACE_INCOMPLETE" in failure

    changed_hash = {"final_cover_sha256": "sha256:" + "0" * 64}
    assert _polish_face_binding_failure(
        changed_hash, verification, "screenshot_polish"
    ) is not None


def test_polish_eyepatch_exception_does_not_relax_tongue_veto(
    tmp_path, monkeypatch
):
    cover = tmp_path / "cover.png"
    cover.write_bytes(b"tongue-veto")

    def tongue_probe(image_path, _question, **_kwargs):
        return {
            "status": "OBSERVED",
            "provider": "cpa",
            "image_sha256": _hash(image_path),
            "answer": json.dumps(
                {
                    "face_complete": True,
                    "missing": [],
                    "tongue_out": True,
                    "reason": "脸完整但吐舌",
                },
                ensure_ascii=False,
            ),
        }

    monkeypatch.setattr(visual_witness, "image_vision_probe", tongue_probe)
    verification = _verify_polish_face_integrity(
        cover, base_url="https://cpa.example/v1", api_key="secret"
    )
    assert verification["status"] == "FAIL"
    assert verification["reason_code"] == "TONGUE_OUT"


def test_bookshop_candidate_locks_q_style_story_and_source_eyepatch_identity():
    candidate_id = "auto_222823_493_595"
    title = "【主播】动漫书店百合区三本书，封面都在背后掐脖子"
    cover_text = "百合区三本书\n都在背后掐脖子"
    direction = _cover_art_direction(
        candidate_id=candidate_id,
        title=title,
        cover_text=cover_text,
        story_hook="动漫书店百合区连续三本书都用了相似的背后掐脖子封面构图",
        allow_punch=False,
    )
    prompt = _cover_prompt(
        title=title, cover_text=cover_text, art_direction=direction
    )

    assert "chibi (Q-style) original editorial redraw" in direction.visual_brief
    assert "not a polished livestream screenshot" in direction.visual_brief
    assert "three separate fictional yuri books" in direction.visual_brief
    assert "source-visible eyepatch" in direction.visual_brief
    assert "exact outfit layers and hair ornaments" in direction.visual_brief
    assert "never reveal or invent the covered eye" in direction.visual_brief
    assert direction.visual_brief in prompt

    unrelated = _cover_art_direction(
        candidate_id="auto_unrelated",
        title=title,
        cover_text=cover_text,
        story_hook="另一条切片",
        allow_punch=False,
    )
    assert unrelated.visual_brief == ""
