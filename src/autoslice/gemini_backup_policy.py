"""Last-resort PAID Gemini key policy (Ivan 2026-07-13).

The three free keys (``GEMINI_API_KEY``/``_2``/``_3``) are the primary
slicing keys. ``GEMINI_KEY_BACKUP`` is a PAID key and must stay rare:

* A work item (identified by its content hash) becomes eligible only after
  the complete free-key chain has already failed **>= 3 separate rounds**
  for that same item — i.e. waiting and retrying did not bring the free
  keys back. The strike count is persisted per item, so the gate holds
  across ticks and process restarts.
* A hard daily usage cap backstops the "非常克制" requirement
  (default 12 calls/day, override ``GEMINI_PAID_BACKUP_DAILY_CAP``).
* Development use: only when the free keys are genuinely unusable AND the
  work is needed the same day — never as the routine dev key. That part is
  operator discipline; this module enforces the production gate.

The key value never enters logs, manifests, or ledgers. Ledgers record only
counts, timestamps, item hashes and bounded purpose strings, so a delivery
validator can prove the gate held without ever seeing a secret.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
from pathlib import Path

PAID_KEY_ENV = "GEMINI_KEY_BACKUP"
MIN_FREE_CHAIN_STRIKES = 3
DEFAULT_DAILY_CAP = 12
PAID_KEY_TIER = "paid_backup"
FREE_KEY_TIER = "free"


def _ledger_root() -> Path:
    base = Path(os.environ.get("AUTOSLICE_BASE", "/opt/bilive/autoslice"))
    return base / "state" / "gemini-paid-backup"


def paid_backup_key() -> str | None:
    value = os.environ.get(PAID_KEY_ENV, "").strip()
    return value or None


def daily_cap() -> int:
    raw = os.environ.get("GEMINI_PAID_BACKUP_DAILY_CAP", "").strip()
    if re.fullmatch(r"[0-9]{1,4}", raw or ""):
        return int(raw)
    return DEFAULT_DAILY_CAP


def _safe_item_name(item_key: str) -> str:
    safe = "".join(ch for ch in str(item_key) if ch.isalnum() or ch in "-_.")[:128]
    return safe or "unkeyed"


def _strike_path(item_key: str) -> Path:
    return _ledger_root() / "strikes" / f"{_safe_item_name(item_key)}.json"


def free_chain_strikes(item_key: str) -> int:
    """Completed free-key-chain failure rounds recorded for this item."""

    try:
        data = json.loads(_strike_path(item_key).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    strikes = data.get("strikes") if isinstance(data, dict) else None
    return len(strikes) if isinstance(strikes, list) else 0


def record_free_chain_failure(item_key: str) -> int:
    """Record one complete free-key-chain failure round; returns the total.

    Ledger IO must never break the calling adapter: on an unwritable ledger
    (e.g. a dev machine without the production BASE) the strike simply is
    not persisted — and ``paid_attempt_allowed`` independently refuses paid
    use whenever the ledger is unavailable, so unaccounted paid calls stay
    impossible.
    """

    path = _strike_path(item_key)
    existing: list[str] = []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("strikes"), list):
            existing = [str(value) for value in data["strikes"]]
    except (OSError, ValueError):
        existing = []
    existing.append(dt.datetime.now(dt.timezone.utc).isoformat())
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(
            json.dumps(
                {"item_key": str(item_key), "strikes": existing[-64:]},
                ensure_ascii=False,
                indent=1,
            )
            + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, path)
    except OSError:
        pass
    return len(existing)


def _usage_path(day: dt.date) -> Path:
    return _ledger_root() / f"usage-{day.strftime('%Y%m%d')}.jsonl"


def paid_calls_today() -> int:
    path = _usage_path(dt.datetime.now(dt.timezone.utc).date())
    try:
        with path.open("r", encoding="utf-8") as handle:
            return sum(1 for line in handle if line.strip())
    except OSError:
        return 0


def paid_attempt_allowed(
    item_key: str, *, prior_strikes: int | None = None
) -> tuple[bool, str]:
    """Gate one paid attempt; returns (allowed, bounded machine reason).

    ``prior_strikes`` is the strike count from BEFORE the current failure
    round, so a paid attempt can fire at the earliest in the round after
    three complete free-chain failures.
    """

    if paid_backup_key() is None:
        return False, "PAID_KEY_NOT_CONFIGURED"
    strikes = free_chain_strikes(item_key) if prior_strikes is None else int(prior_strikes)
    if strikes < MIN_FREE_CHAIN_STRIKES:
        return False, f"FREE_CHAIN_STRIKES_{strikes}_BELOW_{MIN_FREE_CHAIN_STRIKES}"
    try:
        _ledger_root().mkdir(parents=True, exist_ok=True)
    except OSError:
        # No auditable ledger -> no paid use, ever.
        return False, "PAID_LEDGER_UNAVAILABLE"
    used = paid_calls_today()
    cap = daily_cap()
    if used >= cap:
        return False, f"PAID_DAILY_CAP_REACHED_{used}_OF_{cap}"
    return True, f"FREE_CHAIN_STRIKES_{strikes}"


def record_paid_use(item_key: str, *, purpose: str) -> dict[str, object]:
    """Append one paid-key use to today's ledger and return the policy stamp.

    The returned stamp is what adapters embed into their run manifests so a
    later delivery validator can prove the gate held (strikes >= 3, cap not
    exceeded) without any secret material.
    """

    now = dt.datetime.now(dt.timezone.utc)
    used_before = paid_calls_today()
    stamp: dict[str, object] = {
        "key_tier": PAID_KEY_TIER,
        "item_key": _safe_item_name(item_key),
        "free_chain_strikes": free_chain_strikes(item_key),
        "calls_today_before": used_before,
        "daily_cap": daily_cap(),
        "recorded_at": now.isoformat(),
        "purpose": str(purpose)[:64],
    }
    path = _usage_path(now.date())
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(stamp, ensure_ascii=False) + "\n")
    return stamp
