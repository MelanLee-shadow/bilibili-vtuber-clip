"""F5 金丝雀：句内子窗混说检测从"恒空"变成"真重叠窗出证据"。

诊断出处见 src/autoslice/speaker_overlap_evidence.py 模块 docstring：
产出侧此前根本不存在（无 in-repo producer + provider env 未配置 +
AUDITED_PROVIDER_BUNDLES 空 dict 三重断路），所以
`mixed_overlap_evidence` 只能是 null。这些用例锁死新产出者的两个承诺：
真重叠窗出非空证据、且该证据能被现有 mixed gate 的校验器直接吃下。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from scripts.apply_subtitle_text_overrides import TextCue
from src.autoslice.speaker_common import GUEST_SPEAKER, HOST_SPEAKER
from src.autoslice.speaker_evidence import validate_mixed_overlap_evidence_document
from src.autoslice.speaker_overlap_evidence import (
    DETECTION_SCHEMA,
    MAX_SUBCUE_WINDOWS,
    MIN_SUBCUE_WINDOW_MS,
    SUBCUE_OVERLAP_ENV,
    build_mixed_overlap_evidence_document,
    detect_subcue_mixed_overlap,
    extract_window_wav,
    mixed_window_reason,
    plan_subcue_windows,
    subcue_mixed_overlap_disclosure,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class _Scene:
    """A synthetic two-speaker clip with scripted CAM++ similarities.

    cue 1 is the mixed cue: its first half is the host, its second half is the
    guest.  cue 2 is a clean guest anchor.  Nothing here touches real audio —
    the extractor and the similarity function are the two injected seams.
    """

    def __init__(self, tmp_path: Path, *, mixed: bool = True) -> None:
        self.root = tmp_path
        self.work_dir = tmp_path / "work"
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.media = tmp_path / "clip.mp4"
        self.media.write_bytes(b"synthetic two speaker clip")
        self.text_srt = tmp_path / "text-final.srt"
        self.text_srt.write_text(
            "1\n00:00:00,000 --> 00:00:04,000\n我先说一句然后连线接话\n\n"
            "2\n00:00:05,000 --> 00:00:07,000\n连线独立说的一句\n",
            encoding="utf-8",
        )
        self.cues = [
            TextCue(1, "00:00:00,000", "00:00:04,000", "我先说一句然后连线接话"),
            TextCue(2, "00:00:05,000", "00:00:07,000", "连线独立说的一句"),
        ]
        cue_dir = self.work_dir / "cue-wavs"
        cue_dir.mkdir(parents=True, exist_ok=True)
        self.cue_audio_paths = []
        for index in (1, 2):
            path = cue_dir / f"cue-{index:04d}.wav"
            path.write_bytes(f"cue-{index}-audio".encode())
            self.cue_audio_paths.append(path)
        self.host_prints = []
        for name in ("host-a", "host-b"):
            path = tmp_path / f"{name}.wav"
            path.write_bytes(name.encode())
            self.host_prints.append(path)
        self.guest_groups = [[1]]
        self.mixed = mixed
        self.extracted: list[Path] = []

    def extract_window(self, *, media_path, start_ms, end_ms, output_path) -> None:
        assert media_path == self.media
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(f"{output_path.name}:{start_ms}-{end_ms}".encode())
        self.extracted.append(output_path)

    def _window_is_host(self, window_path: Path) -> bool:
        name = window_path.name
        if name.startswith("cue-0002"):
            return False
        if not self.mixed:
            return True
        # cue 1: windows 01/02 are the host, 03/04 are the guest talking over.
        return name.endswith(("w01.wav", "w02.wav"))

    def similarity(self, left: Path, right: Path) -> float:
        window, reference = (right, left) if "w" in right.name else (left, right)
        host_reference = reference in self.host_prints
        if self._window_is_host(window):
            return 0.9 if host_reference else 0.2
        return 0.2 if host_reference else 0.9

    def detect(self, **overrides):
        kwargs = {
            "cues": self.cues,
            "cue_audio_paths": self.cue_audio_paths,
            "media_path": self.media,
            "text_srt_path": self.text_srt,
            "work_dir": self.work_dir,
            "host_prints": self.host_prints,
            "guest_groups": self.guest_groups,
            "threshold": 0.0,
            "band": 0.10,
            "similarity": self.similarity,
            "extract_window": self.extract_window,
        }
        kwargs.update(overrides)
        return detect_subcue_mixed_overlap(**kwargs)


def test_plan_subcue_windows_is_deterministic_and_skips_short_cues() -> None:
    assert plan_subcue_windows(0, MIN_SUBCUE_WINDOW_MS * 2 - 1) == []
    assert plan_subcue_windows(0, 1_400) == [(0, 700), (700, 1_400)]
    assert plan_subcue_windows(1_000, 5_000) == [
        (1_000, 2_000),
        (2_000, 3_000),
        (3_000, 4_000),
        (4_000, 5_000),
    ]
    assert len(plan_subcue_windows(0, 60_000)) == MAX_SUBCUE_WINDOWS
    # Windows tile the cue exactly: no gap, no overlap, no rounding drift.
    plan = plan_subcue_windows(37, 9_311)
    assert plan[0][0] == 37 and plan[-1][1] == 9_311
    assert all(left[1] == right[0] for left, right in zip(plan, plan[1:]))


def test_only_confident_disagreement_accuses_a_cue() -> None:
    assert mixed_window_reason([HOST_SPEAKER, GUEST_SPEAKER]) == (["CUE_MIXED_SPEAKER"], 2)
    assert mixed_window_reason([HOST_SPEAKER, HOST_SPEAKER]) == ([], 1)
    assert mixed_window_reason([GUEST_SPEAKER, None, GUEST_SPEAKER]) == ([], 1)
    # A blended/overlapped window lands in the ambiguity band; ambiguity alone
    # must never manufacture evidence.
    assert mixed_window_reason([None, None]) == ([], 0)
    assert mixed_window_reason([]) == ([], 0)


def test_real_overlap_window_produces_gate_consumable_evidence(tmp_path: Path) -> None:
    """F5 主金丝雀：合成双说话人重叠窗 -> evidence 非空且 mixed gate 可消费。"""

    scene = _Scene(tmp_path)
    disclosure = scene.detect()

    assert disclosure["schema_version"] == DETECTION_SCHEMA
    assert disclosure["mixed_cue_count"] == 1
    assert disclosure["mixed_source_cues"] == [1]
    assert disclosure["evidence_status"] == "REVIEW_REQUIRED"
    assert disclosure["consumed_by_this_run"] is False

    evidence_path = Path(str(disclosure["evidence_document"]))
    document = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert document["review_required_cues"], "real overlap window must not stay null"
    assert document["status"] == "REVIEW_REQUIRED"

    # The load-bearing assertion: "供 speaker finalizer 的 mixed gate 消费" is
    # machine-checkable exactly here — the existing validator accepts it.
    rows = validate_mixed_overlap_evidence_document(
        document,
        expected_media_sha256=_sha256(scene.media),
        expected_text_sha256=_sha256(scene.text_srt),
        cues=scene.cues,
        expected_audio_root=evidence_path.parent,
    )
    assert [row["source_cue"] for row in rows] == [1]
    assert rows[0]["reason_codes"] == ["CUE_MIXED_SPEAKER"]
    assert rows[0]["provider_details"]["cluster_count"] == 2
    assert rows[0]["text"] == scene.cues[0].text


def test_clean_single_speaker_clip_yields_clear_evidence(tmp_path: Path) -> None:
    scene = _Scene(tmp_path, mixed=False)
    disclosure = scene.detect()

    assert disclosure["mixed_cue_count"] == 0
    assert disclosure["evidence_status"] == "CLEAR"
    document = json.loads(
        Path(str(disclosure["evidence_document"])).read_text(encoding="utf-8")
    )
    assert document["review_required_cues"] == []
    assert (
        validate_mixed_overlap_evidence_document(
            document,
            expected_media_sha256=_sha256(scene.media),
            expected_text_sha256=_sha256(scene.text_srt),
            cues=scene.cues,
            expected_audio_root=Path(str(disclosure["evidence_document"])).parent,
        )
        == []
    )


def test_confident_whole_cue_margin_is_not_a_prefilter(tmp_path: Path) -> None:
    """8/7 假李豆沙 cue33/37 的 margin 是 0.43/0.34——远在模糊带之外。

    任何"只给模糊带 cue 分窗"的优化都会在这一类上重建阈值死区，所以窗口
    资格只看时长。这里用一个整句 margin 明确落在 HOST 硬通过侧的 cue 复现
    该形状，检测器仍须逐窗看见分歧。
    """

    scene = _Scene(tmp_path)
    whole_cue_margin = scene.similarity(
        scene.host_prints[0], scene.work_dir / "subcue-windows" / "cue-0001-w01.wav"
    ) - scene.similarity(
        scene.cue_audio_paths[1], scene.work_dir / "subcue-windows" / "cue-0001-w01.wav"
    )
    assert whole_cue_margin > 0.10 * 3  # confidently outside the ambiguity band

    disclosure = scene.detect()
    assert disclosure["mixed_source_cues"] == [1]
    flagged = next(
        row for row in disclosure["cue_windows"] if row["source_cue"] == 1
    )
    assert [window["label"] for window in flagged["windows"]] == [
        HOST_SPEAKER,
        HOST_SPEAKER,
        GUEST_SPEAKER,
        GUEST_SPEAKER,
    ]


def test_short_cues_are_skipped_and_disclosed(tmp_path: Path) -> None:
    scene = _Scene(tmp_path)
    scene.cues = [
        TextCue(1, "00:00:00,000", "00:00:00,400", "太短"),
        *scene.cues[1:],
    ]
    scene.text_srt.write_text(
        "1\n00:00:00,000 --> 00:00:00,400\n太短\n\n"
        "2\n00:00:05,000 --> 00:00:07,000\n连线独立说的一句\n",
        encoding="utf-8",
    )
    disclosure = scene.detect()
    assert disclosure["skipped_short_cue_count"] == 1
    assert disclosure["windowed_cue_count"] == 1
    assert disclosure["mixed_cue_count"] == 0


def test_disclosure_wrapper_fails_open_on_detector_fault(tmp_path: Path) -> None:
    scene = _Scene(tmp_path)

    def broken_extract(**_kwargs):
        raise RuntimeError("ffmpeg missing")

    result = subcue_mixed_overlap_disclosure(
        cues=scene.cues,
        cue_audio_paths=scene.cue_audio_paths,
        media_path=scene.media,
        text_srt_path=scene.text_srt,
        work_dir=scene.work_dir,
        host_prints=scene.host_prints,
        guest_groups=scene.guest_groups,
        threshold=0.0,
        band=0.10,
        similarity=scene.similarity,
        extract_window=broken_extract,
    )
    assert result["status"] == "UNAVAILABLE"
    assert "ffmpeg missing" in str(result["error"])


def test_disclosure_wrapper_honours_the_kill_switch(tmp_path: Path, monkeypatch) -> None:
    scene = _Scene(tmp_path)
    monkeypatch.setenv(SUBCUE_OVERLAP_ENV, "0")
    result = subcue_mixed_overlap_disclosure(
        cues=scene.cues,
        cue_audio_paths=scene.cue_audio_paths,
        media_path=scene.media,
        text_srt_path=scene.text_srt,
        work_dir=scene.work_dir,
        host_prints=scene.host_prints,
        guest_groups=scene.guest_groups,
        threshold=0.0,
        band=0.10,
        similarity=scene.similarity,
        extract_window=scene.extract_window,
    )
    assert result["status"] == "DISABLED"
    assert scene.extracted == []

    monkeypatch.setenv(SUBCUE_OVERLAP_ENV, "1")
    enabled = subcue_mixed_overlap_disclosure(
        cues=scene.cues,
        cue_audio_paths=scene.cue_audio_paths,
        media_path=scene.media,
        text_srt_path=scene.text_srt,
        work_dir=scene.work_dir,
        host_prints=scene.host_prints,
        guest_groups=scene.guest_groups,
        threshold=0.0,
        band=0.10,
        similarity=scene.similarity,
        extract_window=scene.extract_window,
    )
    assert enabled["status"] == "READY"
    assert enabled["mixed_cue_count"] == 1


def test_evidence_document_rejects_a_row_without_bound_audio(tmp_path: Path) -> None:
    """The document is only useful if the gate would still refuse a fake row."""

    scene = _Scene(tmp_path)
    document = build_mixed_overlap_evidence_document(
        [
            {
                "source_cue": 1,
                "zero_based_index": 0,
                "start": scene.cues[0].start,
                "end": scene.cues[0].end,
                "text": scene.cues[0].text,
                "audio_sha256": "a" * 64,
                "reason_codes": ["CUE_MIXED_SPEAKER"],
                "provider_details": {
                    "cluster_count": 2,
                    "audio_path": str((tmp_path / "nope.wav").resolve()),
                },
            }
        ],
        source_media_sha256=_sha256(scene.media),
        text_final_srt_sha256=_sha256(scene.text_srt),
        config_sha256="c" * 64,
    )
    with pytest.raises(Exception):
        validate_mixed_overlap_evidence_document(
            document,
            expected_media_sha256=_sha256(scene.media),
            expected_text_sha256=_sha256(scene.text_srt),
            cues=scene.cues,
            expected_audio_root=tmp_path,
        )


def test_finalizer_wires_the_text_srt_and_discloses_without_touching_the_gate_field(
    tmp_path: Path,
) -> None:
    """接线金丝雀：analyzer 拿得到 text_srt_path，披露块落进 READY manifest。

    同时锁死命名不撞车：自产 sidecar **不得**写进顶层
    ``mixed_overlap_evidence[_sha256]``——那两个键是 producer 侧
    ``run_speaker_finalizer`` 的输入 binding 断言对象，写进去等于每轮生产
    都抛 SPEAKER_FINALIZATION_MIXED_OVERLAP_BINDING_MISMATCH。
    """

    from src.autoslice.speaker_finalizer import finalize_speaker_subtitles

    media = tmp_path / "clean.mp4"
    media.write_bytes(b"clean media")
    text_srt = tmp_path / "text-final.srt"
    text_srt.write_text("1\n00:00:00,000 --> 00:00:02,000\n嘿嘿嘿\n", encoding="utf-8")
    profile = tmp_path / "profile.json"
    profile.write_text("{}", encoding="utf-8")
    (tmp_path / "refs").mkdir()
    (tmp_path / "model").mkdir()
    seen: dict[str, object] = {}
    disclosure = {
        "schema_version": DETECTION_SCHEMA,
        "status": "READY",
        "mixed_cue_count": 1,
        "evidence_document": str(tmp_path / "detected-mixed-overlap-evidence.json"),
        "consumed_by_this_run": False,
    }

    def analyzer(**kwargs):
        seen.update(kwargs)
        return {
            "decisions": [
                {"speaker": HOST_SPEAKER, "decision_source": "campp_audio", "margin": 0.4}
            ],
            "context_unresolved_cues": [],
            "subcue_mixed_overlap": disclosure,
        }

    manifest = finalize_speaker_subtitles(
        media_path=media,
        text_srt_path=text_srt,
        profile_path=profile,
        reference_dir=tmp_path / "refs",
        model_dir=tmp_path / "model",
        output_srt_path=tmp_path / "speaker.srt",
        output_ass_path=tmp_path / "speaker.ass",
        output_manifest_path=tmp_path / "speaker.json",
        work_dir=tmp_path / "work",
        analyzer=analyzer,
    )

    assert seen["text_srt_path"] == text_srt
    assert manifest["analysis"]["subcue_mixed_overlap"] == disclosure
    assert manifest["mixed_overlap_evidence"] is None
    assert manifest["mixed_overlap_evidence_sha256"] is None


def test_finalizer_call_site_kwargs_match_the_detector_signature() -> None:
    """守住唯一一行密闭套件跑不到的生产代码。

    `_run_campplus_analysis` 里那一处调用要 campp 运行时才会执行（Mac 上没有），
    所以 kwarg 改名只会静默降级成 UNAVAILABLE、通道又变回恒空。这里直接对
    调用点做 AST 比对：调用面和函数签名对不上就红。
    """

    import ast
    import inspect
    from pathlib import Path as _Path

    from src.autoslice import speaker_finalizer

    tree = ast.parse(_Path(speaker_finalizer.__file__).read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "subcue_mixed_overlap_disclosure"
    ]
    assert len(calls) == 1, "expected exactly one production call site"
    passed = {keyword.arg for keyword in calls[0].keywords}
    accepted = set(inspect.signature(detect_subcue_mixed_overlap).parameters)
    required = {
        name
        for name, parameter in inspect.signature(detect_subcue_mixed_overlap).parameters.items()
        if parameter.default is inspect.Parameter.empty
    }
    assert passed <= accepted, f"call site passes unknown kwargs: {passed - accepted}"
    assert required <= passed, f"call site omits required kwargs: {required - passed}"


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg unavailable")
def test_default_extractor_really_cuts_a_window(tmp_path: Path) -> None:
    """The default extractor is a real ffmpeg call, not a fiction in the seam."""

    media = tmp_path / "tone.wav"
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
            "-ac", "1", "-ar", "16000", str(media),
        ],
        check=True,
        timeout=120,
    )
    output = tmp_path / "windows" / "cue-0001-w01.wav"
    extract_window_wav(media_path=media, start_ms=500, end_ms=1_500, output_path=output)
    assert output.is_file() and output.stat().st_size > 0
