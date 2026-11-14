"""Compatibility facade for structured chat evidence and finalization.

Implementation lives in the single-direction dependency chain
``chat_evidence -> chat_repair -> chat_proposals``.
"""

from __future__ import annotations

from src.autoslice.chat_evidence import load_referent_groups as load_referent_groups
