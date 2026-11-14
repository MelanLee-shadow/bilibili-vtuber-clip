"""Step registry for the manual-lane pipeline.

Numbers are the step labels used everywhere (state, PROCESS.md, --until).  List order is the serial order, and the
order of PROCESS.md: the boundary (1) comes before the external duplicate check (0), because the check looks at the cut
interval.  A step starts when the steps in DEPENDS[number] are done; steps that do not depend on each other run at once
(runner.run_clip).  Every step is declared in DEPENDS and depends only on steps earlier in the list.
"""

from __future__ import annotations

from . import stage_impl, stage_package
from .runner import Step
from .source_read import HOST_ENCODE

STEPS: tuple[Step, ...] = (
    Step(1, "boundary", "边界（CPA 判定起止，采用）", stage_impl.step1_boundary),
    Step(0, "dedupe", "外片查重", stage_impl.step0_dedupe),
    Step(2, "cut", "剪辑", stage_impl.step2_cut),
    Step(3, "subtitles", "字幕：ASR、听音证人、疑点，CPA 按上下文定字", stage_impl.step3_subtitles),
    Step(4, "speaker", "说话人", stage_impl.step4_speaker),
    Step(5, "burn", "片头与烧录", stage_impl.step5_burn),
    Step(6, "title_cover", "标题与封面", stage_impl.step6_title_cover),
    Step(7, "tags", "标签", stage_impl.step7_tags),
    Step(8, "package", "成片包", stage_package.step8_package),
    Step(9, "review", "CPA 复审", stage_package.step9_review),
    Step(10, "upload", "上传（须显式授权，默认关闭）", stage_package.step10_upload),
)

# step number -> the steps that must be done before it starts.  Taken from what each body reads:
#   1  the BCUT cues of the clip (state); fixes the cut interval in the clip's state
#   0  reads that interval (clip start/end); a duplicate stops the clip before it is cut
#   2  reads the interval (and snaps it, so it must come after the check that reads it); writes cut/cut.mp4
#   3  cut.mp4; writes subs/final.srt   4  subs/final.srt, cut.mp4; writes speaker/speaker.srt
#   5  speaker/speaker.srt, cut/cut.mp4; writes burn/final.mp4, burn/intro_result.json
#   6  subs/final.srt, speaker/speaker.srt (subtitle_track), cut/cut.mp4; writes title/title.txt, cover/
#   7  title/title.txt (step 6) and subs/final.srt; writes tags/tags.json
#   8  burn/ (5), title/ and cover/ (6), tags/ (7), speaker/ (4), cut/ (2); writes the package
#   9  the package (8)   10  the reviewed package (9)
DEPENDS: dict[int, tuple[int, ...]] = {
    1: (),
    0: (1,),
    2: (0, 1),
    3: (2,),
    4: (3,),
    5: (2, 4),
    6: (2, 4),
    7: (3, 6),
    8: (5, 6, 7),
    9: (8,),
    10: (9,),
}

# step number -> the lane_rules `concurrency` row that bounds how many clips run that step at once: the steps that encode
# video on the media host's CPU (cut from the source, burn) share the one row `host_encode`
GATES: dict[int, str] = {2: HOST_ENCODE, 5: HOST_ENCODE}
