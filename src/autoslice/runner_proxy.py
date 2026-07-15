"""Lazy access to the live unattended-runner module.

The runner is imported as ``scripts.free_session_autoslice`` by tests and
``python -m``, but cron executes it as ``__main__``.  Resolving the module only
when an attribute is accessed avoids re-executing the runner during its own
imports while preserving its existing monkeypatch surface.
"""

from __future__ import annotations

import sys


class RunnerProxy:
    """Resolve attributes on whichever module currently owns the runner."""

    def __getattr__(self, name: str):
        module = sys.modules.get("scripts.free_session_autoslice") or sys.modules.get(
            "__main__"
        )
        return getattr(module, name)
