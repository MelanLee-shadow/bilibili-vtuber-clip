from scripts.evaluate_speaker_phase1 import align_prediction
from scripts.build_speaker_blind_review import Cue


def test_align_prediction_uses_time_overlap_not_matching_cue_number() -> None:
    item = {"review_id": "x:cue-99", "start_ms": 900, "end_ms": 3100, "duration_ms": 2200}
    cues = [Cue(1, 0, 1000, "a"), Cue(2, 1000, 3000, "b"), Cue(3, 3000, 4000, "c")]
    manifest = {
        "analysis": {
            "decisions": [
                {"source_index": 1, "speaker": "连线", "decision_source": "campp_audio"},
                {"source_index": 2, "speaker": "主播", "decision_source": "whole_clip_context"},
                {"source_index": 3, "speaker": "连线", "decision_source": "campp_audio"},
            ]
        }
    }
    result = align_prediction(item, manifest, cues)
    assert result["predicted"] == "主播"
    assert result["decision_source"] == "whole_clip_context"
    assert result["label_overlap_ms"] == {"连线": 200, "主播": 2000}
    assert result["coverage"] == 1.0
