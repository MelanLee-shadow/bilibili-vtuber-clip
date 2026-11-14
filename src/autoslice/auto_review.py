from __future__ import annotations

try:
    from enum import StrEnum
except ImportError:  # Python 3.10 on the free/bilive container.
    from enum import Enum

    class StrEnum(str, Enum):
        pass


class DecisionAction(StrEnum):
    AUTO_UPLOAD = "AUTO_UPLOAD"
    AUTO_RECUT = "AUTO_RECUT"
    DROP = "DROP"
    BLOCK = "BLOCK"
    RETRY = "RETRY"


