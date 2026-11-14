"""Source-backed topic -> work -> proper-name routing for subtitle correction.

The graph is deliberately separate from the global glossary.  A clip must
first match a topic/work surface before any child character names are exposed
to a correction model.  The graph narrows candidates; raw audio and exact
structured chat remain the authorities for which candidate was spoken.
"""

from __future__ import annotations


from src.autoslice.topic_entity_graph_fields import dedupe_key as dedupe_key, MAX_TOPICS as MAX_TOPICS
from src.autoslice.topic_entity_graph_load import (
    load_topic_entity_graph as load_topic_entity_graph,
    validate_topic_entity_graph as validate_topic_entity_graph,
)


