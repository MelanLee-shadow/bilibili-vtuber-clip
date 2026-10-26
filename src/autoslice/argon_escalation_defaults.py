"""Production default for the explicit Argon upgrade capability.

integration decision (41 "Argon 疑难片段升级普通入口"): production
SPECs carry ``argon_escalation`` enabled; an explicit value in a SPEC
(including ``{"enabled": false}`` in comparison/regression tooling) is kept.
Argon calls keep the existing AGY -> free key -> paid key ladder and ledger.

COST-01 (公开规则：先调用AGY订阅，然后用免费key，最后用付费key
「付费key不能加冷却期」; 免费key用完后必须接付费key「这是要求」):
the production default therefore opens the paid backup and the same-key
realtime method. Paid use still passes ``gemini_backup_policy`` and is
ledgered per call. A production SPEC that only says ``{"enabled": true}``
gets the missing COST-01 keys; explicit keys and comparison SPECs are kept.
"""
from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Mapping, MutableMapping

PRODUCTION_ARGON_ESCALATION: Mapping[str, object] = {
    "enabled": True, "allow_paid": True, "allow_realtime_fallback": True,
}
DEFAULT_BASIS = "PRODUCTION_DEFAULT_2026-10-21_COST-01"
_COMPARISON_KEYS = ("comparison_plan", "comparison_grant", "comparison_case_ids")


def apply_production_argon_default(spec: MutableMapping[str, object]) -> bool:
    """Set the production default, or fill COST-01 keys an enabled SPEC omits."""
    current = spec.get("argon_escalation")
    if "argon_escalation" not in spec:
        spec["argon_escalation"] = copy.deepcopy(dict(PRODUCTION_ARGON_ESCALATION))
        return True
    if (not isinstance(current, MutableMapping) or current.get("enabled") is not True
            or any(key in current for key in _COMPARISON_KEYS)):
        return False
    missing = {k: v for k, v in PRODUCTION_ARGON_ESCALATION.items() if k not in current}
    current.update(copy.deepcopy(missing))
    return bool(missing)


def is_production_runtime_spec(spec: Mapping[str, object]) -> bool:
    """A derived rerun SPEC bound to the live runtime base (B-style copies)."""
    base, root = os.environ.get("AUTOSLICE_BASE"), spec.get("runtime_root")
    if not base or not isinstance(root, str) or not root:
        return False
    try:
        return Path(root).absolute() == Path(base).absolute()
    except (OSError, ValueError):
        return False
