"""Cover image selection and accepted receipt model history."""

from typing import Final

PRIMARY_MODEL: Final = "gpt-image-2.6-sunburst"
COMPATIBILITY_MODEL: Final = "gpt-image-2"

REDRAW_MODELS: Final = frozenset({
    PRIMARY_MODEL, "gpt-image-2.6-flare", "gpt-image-2.5", COMPATIBILITY_MODEL,
})
RECEIPT_MODELS: Final = REDRAW_MODELS | {"gpt-image-1.5"}
FALLBACK_CHAINS: Final = frozenset({
    (PRIMARY_MODEL, COMPATIBILITY_MODEL),
    ("gpt-image-2.5", COMPATIBILITY_MODEL),
    (COMPATIBILITY_MODEL, "gpt-image-1.5"),
})
