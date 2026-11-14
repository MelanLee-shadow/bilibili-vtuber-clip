"""How long before a clip each kind of chat event can still be the thing the streamer is answering.

Kept apart from producer_chat_input so the lane reads the same horizons without loading the chat
authority; producer_chat_input re-exports them for its existing importers."""
from __future__ import annotations


SC_PRE_CONTEXT_MS = 900_000  # include SCs up to 15min before the clip: she CLEARS THE

GIFT_PRE_CONTEXT_MS = 120_000  # thanks for a gift usually follow within a couple

# GUARD_BUY thanks can be delayed while the streamer clears queued SCs.  The
# July 22 verified panoja event is acknowledged 232,140ms later; keep this in
# lockstep with chat_proposals.GUARD_THANK_WINDOW_AFTER_MS.
GUARD_PRE_CONTEXT_MS = 300_000
