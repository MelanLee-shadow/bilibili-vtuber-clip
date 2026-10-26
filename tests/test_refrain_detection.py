from pathlib import Path

import pytest

from src.autoslice.jingting_chunker import parse_srt_cues
from src.autoslice.refrain_detection import (
    bind_refrain_occurrence,
    detect_refrains,
    is_refrain_variant,
    is_restatement_from_refrain,
    refrain_support,
)
from src.autoslice.subtitle_acceptability_policy import load_policy






def test_frequency_scope_thresholds_and_no_pair_only_claim():
    p = load_policy()
    texts = ["不许跟别人说话", "不许跟别人讲话了", "不许跟别人说话了"]
    assert detect_refrains(texts, policy=p)
    assert not detect_refrains(texts[:2], policy=p)
    assert not detect_refrains([{"index": 1, "text": x} for x in texts], policy=p)
    assert not detect_refrains(["哈哈哈哈哈哈"] * 8, policy=p)
    p["refrain"]["min_occurrences"] = 4
    assert not detect_refrains(texts, policy=p)
    with pytest.raises(TypeError):
        is_restatement_from_refrain(texts[0], texts[1])
