"""The pixel canvas a cover is composed on and the size requested from the image gateway.

Split out of cover_generation so the lane checks imported covers against the same canvas without loading the cover
generator; cover_generation imports the names back."""
from __future__ import annotations

_COVER_CANVAS = (1920, 1080)
# The CPA image gateway (new_api) rejects sizes that aren't positive multiples
# of 16 with HTTP 400 model_price_error — 1080 isn't one (first hit,
# blanked a whole unattended batch's covers).  Request the nearest compliant
# size and normalize the returned image back onto the 1920x1080 overlay canvas.
_COVER_REQUEST_SIZE = "1920x1088"
