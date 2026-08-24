from pathlib import Path

from scripts.build_fastlane_c2_candidate_package import _cue_graph, _replace_ass_cues
from scripts.build_fastlane_c2_private_successor import load, project


def test_c2_candidate_graph_keeps_all_but_the_one_operator_repair(tmp_path: Path):
    source = Path("assets/lidousha/fastlane_c2_private/auto_203011_328_389.pipeline-diagnostic.srt")
    final = tmp_path / "auto_203011_328_389.recut.srt"
    final.write_text(project(source.read_text(encoding="utf-8"), load()), encoding="utf-8")
    graph = _cue_graph(source, final, load())

    assert len(graph["rows"]) == 22
    assert [row["cue"] for row in graph["rows"] if row["disposition"] == "OPERATOR_REPAIR"] == [5]
    assert graph["rows"][20]["after"] == "小豆哪有好吵"


def test_c2_candidate_ass_projection_repairs_both_bound_surfaces(tmp_path: Path):
    source = tmp_path / "source.ass"
    target = tmp_path / "target.ass"
    source.write_text(
        "Dialogue: 0,0:00:08.72,0:00:11.24,Default,,0,0,0,,wrong-five\n"
        "Dialogue: 0,0:00:56.08,0:00:58.86,Default,,0,0,0,,wrong-twenty-one\n",
        encoding="utf-8",
    )

    _replace_ass_cues(source, target)

    assert target.read_text(encoding="utf-8") == (
        "Dialogue: 0,0:00:08.72,0:00:11.24,Default,,0,0,0,,小豆老公；； 不是你老公\n"
        "Dialogue: 0,0:00:56.08,0:00:58.86,Default,,0,0,0,,小豆哪有好吵\n"
    )
