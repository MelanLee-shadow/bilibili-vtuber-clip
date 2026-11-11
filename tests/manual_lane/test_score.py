from src.autoslice.manual_lane.score import align, normalize, score_rows


def test_normalize_drops_laughter_question_marks_punct_and_fullwidth_digits():
    assert normalize("有1吗？ 嗯？有！（笑）") == normalize("有１吗嗯有")
    assert normalize("有熊猫吗（?）？ 有！") == "有熊猫吗有"
    assert normalize("（笑）") == ""


def test_align_by_overlap_concatenates_and_takes_largest_overlap_speaker():
    truth = [{"id": "c-0", "abs_start_ms": 0, "abs_end_ms": 1000, "text": "我们走", "speaker": "李豆沙"},
             {"id": "c-1", "abs_start_ms": 1000, "abs_end_ms": 2000, "text": "好的", "speaker": "雾深Girimi"}]
    pipe = [{"abs_start_ms": 0, "abs_end_ms": 600, "text": "我们", "speaker": None},
            {"abs_start_ms": 600, "abs_end_ms": 1000, "text": "走", "speaker": None},
            {"abs_start_ms": 1000, "abs_end_ms": 2000, "text": "好的", "speaker": "李豆沙"},
            {"abs_start_ms": 2300, "abs_end_ms": 2600, "text": "额外", "speaker": None}]
    rows, uncovered = align(truth, pipe)
    assert rows[0]["pipe_text"] == "我们走"
    assert rows[1]["pred_speaker"] == "李豆沙"
    assert [u["text"] for u in uncovered] == ["额外"]
    s = score_rows(rows)
    assert s["exact"] == 2
    assert s["speaker_correct"] == 0  # cue0 undecided (None), cue1 truth 雾深 vs predicted 李豆沙


def test_scorer_v2_drops_laughter_and_notes_on_both_sides_and_reads_numerals_as_one_word(monkeypatch):
    from src.autoslice.manual_lane import score as sc

    monkeypatch.setenv("MANUAL_LANE_SCORER", "v2")
    assert sc.normalize("哈哈哈哈。乱讲") == sc.normalize("乱讲")
    assert sc.normalize("面对大家。我（哼唧）") == sc.normalize("面对大家我")
    assert sc.normalize("我不是0就是1") == sc.normalize("我不是零就是一")
    assert sc.normalize("真的吗") != sc.normalize("真的呀")            # a different word stays different
    monkeypatch.setenv("MANUAL_LANE_SCORER", "v1")
    assert sc.normalize("哈哈哈哈。乱讲") != sc.normalize("乱讲")      # the frozen definition is kept for comparison


def test_scorer_v2_gives_every_pipeline_cue_to_the_truth_window_it_overlaps_most(monkeypatch):
    from src.autoslice.manual_lane import score as sc

    truth = [{"id": "c-0", "abs_start_ms": 0, "abs_end_ms": 2000, "text": "前一句啊", "speaker": "李豆沙"},
             {"id": "c-2000", "abs_start_ms": 2000, "abs_end_ms": 4000, "text": "后一句", "speaker": "李豆沙"}]
    # the pipeline cut the first sentence later: "啊" sits in a cue lying 40 % in window 1 and 60 % in window 2
    pipe = [{"abs_start_ms": 0, "abs_end_ms": 1600, "text": "前一句", "speaker": "李豆沙"},
            {"abs_start_ms": 1600, "abs_end_ms": 2600, "text": "啊", "speaker": "李豆沙"},
            {"abs_start_ms": 2600, "abs_end_ms": 4000, "text": "后一句", "speaker": "李豆沙"}]
    monkeypatch.setenv("MANUAL_LANE_SCORER", "v2")
    rows, uncovered = sc.align(truth, pipe)
    assert [r["pipe_text"] for r in rows] == ["前一句", "啊后一句"] and uncovered == []
    monkeypatch.setenv("MANUAL_LANE_SCORER", "v1")
    rows, uncovered = sc.align(truth, pipe)
    assert [r["pipe_text"] for r in rows] == ["前一句", "啊后一句"]


def test_scorer_v1_loses_a_cue_that_lies_less_than_half_in_every_window_and_v2_keeps_it(monkeypatch):
    from src.autoslice.manual_lane import score as sc

    truth = [{"id": "c-0", "abs_start_ms": 0, "abs_end_ms": 2000, "text": "甲乙丙", "speaker": None},
             {"id": "c-2000", "abs_start_ms": 2000, "abs_end_ms": 2100, "text": "嗯", "speaker": None},
             {"id": "c-2100", "abs_start_ms": 2100, "abs_end_ms": 4000, "text": "丁戊", "speaker": None}]
    pipe = [{"abs_start_ms": 1200, "abs_end_ms": 2900, "text": "甲乙丙丁", "speaker": None}]
    monkeypatch.setenv("MANUAL_LANE_SCORER", "v1")
    rows, uncovered = sc.align(truth, pipe)
    assert [r["pipe_text"] for r in rows] == ["", "", ""] and len(uncovered) == 1
    monkeypatch.setenv("MANUAL_LANE_SCORER", "v2")
    rows, uncovered = sc.align(truth, pipe)
    assert rows[0]["pipe_text"] == "甲乙丙丁" and uncovered == []


def test_exact_flat_does_not_depend_on_where_the_output_cuts_its_lines():
    from src.autoslice.manual_lane import score as sc

    truth = ["甲乙丙", "丁戊", "己庚"]
    assert sc.exact_flat(truth, "甲乙丙丁戊己庚") == 3          # one merged line: every sentence still right
    assert sc.exact_flat(truth, "甲乙丙丁戌己庚") == 2          # one wrong character: that sentence only
    assert sc.exact_flat(truth, "甲乙丙己庚") == 2              # a missing sentence counts as wrong


def test_scorer_v3_drops_the_filler_words_on_both_sides_from_the_one_filler_list(monkeypatch):

    from src.autoslice.manual_lane import doubt_policy, score

    monkeypatch.delenv("MANUAL_LANE_SCORER", raising=False)
    assert score.scorer_version() == "v3"
    assert score.normalize("嗯，好的呀") == score.normalize("好的") == "好的"
    monkeypatch.setattr(doubt_policy, "FILLERS", "嗯")                  # the list is doubt_policy's, not a copy
    assert score.normalize("嗯，好的呀") == "好的呀"
    monkeypatch.setenv("MANUAL_LANE_SCORER", "v2")
    assert score.normalize("嗯，好的呀") == "嗯好的呀"
