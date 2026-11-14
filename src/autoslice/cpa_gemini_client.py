"""Default native Gemini transport. CPA owns credential selection and retries.

The switch is read at dispatch, defaults on, and accepts an explicit ``0`` opt-out.
Callers retain their model/provider/cache identity; transport evidence is separate.
No Google key, OAuth file, management endpoint or attribution file is read here.
"""
from __future__ import annotations

from typing import TypeAlias

JsonValue: TypeAlias = "None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]"


