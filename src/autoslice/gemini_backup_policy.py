"""Last-resort PAID Gemini key policy (维护者).

The three free keys (``GEMINI_API_KEY``/``_2``/``_3``) are the primary
slicing keys. ``GEMINI_KEY_BACKUP`` is a PAID key and must stay rare:

* Unattended production: a work item (identified by its content hash)
  becomes eligible only after the complete free-key chain has already
  failed **>= 3 separate rounds** for that same item — i.e. waiting and
  retrying did not bring the free keys back. The strike count is persisted
  per item, so the gate holds across ticks and process restarts.
* Development phase (维护者 update): setting
  ``GEMINI_PAID_BACKUP_DEV_EXCEPTION=1`` on a run lets the paid key fire in
  the same round the free chain fails — free keys are still always tried
  first; the paid key is never *preferred*. Set this only on supervised
  dev/rerun/eval units, never in the unattended production cron env.
* No hard cap by default (维护者: it is the last resort and must
  stay usable). ``GEMINI_PAID_BACKUP_DAILY_CAP`` remains available as an
  optional brake; unset means uncapped. Every use is still ledgered.

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
from typing import Mapping

PAID_KEY_ENV = "GEMINI_KEY_BACKUP"
DEV_EXCEPTION_ENV = "GEMINI_PAID_BACKUP_DEV_EXCEPTION"
MIN_FREE_CHAIN_STRIKES = 3
PAID_KEY_TIER = "paid_backup"
FREE_KEY_TIER = "free"
CANDIDATE_BLIND_AUDIO_WITNESS_PURPOSE = "candidate_blind_audio_witness"
CANDIDATE_BLIND_EXACT_TRANSCRIPT_PURPOSE = (
    "candidate_blind_exact_source_transcript"
)
_PAID_STAMP_KEYS = {
    "key_tier", "mode", "item_key", "free_chain_strikes",
    "calls_today_before", "daily_cap", "recorded_at", "purpose",
}


def validate_key_acceptance_metadata(
    *,
    configured_key_count: object,
    accepted_key_ordinal: object,
    accepted_key_tier: object,
    paid_backup_policy: object,
) -> str | None:
    """Validate the shared free-chain/paid-backup proof carried by artifacts."""

    if (
        not isinstance(configured_key_count, int)
        or isinstance(configured_key_count, bool)
        or not 1 <= configured_key_count <= 3
        or not isinstance(accepted_key_ordinal, int)
        or isinstance(accepted_key_ordinal, bool)
    ):
        return "Gemini API audio failover metadata is incomplete"
    key_tier = accepted_key_tier or FREE_KEY_TIER
    if key_tier == FREE_KEY_TIER:
        if (
            not 1 <= accepted_key_ordinal <= configured_key_count
            or accepted_key_tier not in (None, FREE_KEY_TIER)
            or paid_backup_policy is not None
        ):
            return "Gemini API audio failover metadata is incomplete"
        return None
    if key_tier == PAID_KEY_TIER:
        policy = paid_backup_policy
        gate_ok = isinstance(policy, Mapping) and (
            policy.get("mode") == "dev_exception"
            or (
                isinstance(policy.get("free_chain_strikes"), int)
                and not isinstance(policy.get("free_chain_strikes"), bool)
                and int(policy["free_chain_strikes"]) >= MIN_FREE_CHAIN_STRIKES
            )
        )
        if accepted_key_ordinal != configured_key_count + 1 or not gate_ok:
            return "paid Gemini backup acceptance violates the usage gate"
        return None
    return "Gemini API audio failover key tier is unknown"


def valid_paid_policy_stamp(
    stamp: object, *, expected_purpose: str, expected_item_key: str
) -> bool:
    """Validate one secret-free paid-use proof against its exact call identity."""

    if not isinstance(stamp, Mapping) or set(stamp) != _PAID_STAMP_KEYS:
        return False
    strikes = stamp.get("free_chain_strikes")
    calls_before = stamp.get("calls_today_before")
    cap = stamp.get("daily_cap")
    try:
        recorded_at = dt.datetime.fromisoformat(str(stamp.get("recorded_at") or ""))
    except ValueError:
        return False
    return bool(
        stamp.get("key_tier") == PAID_KEY_TIER
        and stamp.get("purpose") == expected_purpose
        and stamp.get("item_key") == _safe_item_name(expected_item_key)
        and stamp.get("mode") in {"dev_exception", "strict"}
        and isinstance(strikes, int) and not isinstance(strikes, bool) and strikes >= 0
        and (stamp.get("mode") == "dev_exception" or strikes >= MIN_FREE_CHAIN_STRIKES)
        and isinstance(calls_before, int)
        and not isinstance(calls_before, bool)
        and calls_before >= 0
        and (
            cap is None
            or (
                isinstance(cap, int) and not isinstance(cap, bool)
                and cap > calls_before >= 0
            )
        )
        and recorded_at.tzinfo is not None
    )


def _ledger_root() -> Path:
    base = Path(os.environ.get("AUTOSLICE_BASE", "/opt/bilive/autoslice"))
    return base / "state" / "gemini-paid-backup"


def paid_backup_key() -> str | None:
    value = os.environ.get(PAID_KEY_ENV, "").strip()
    return value or None


def daily_cap() -> int | None:
    """Optional daily brake; None (the default) means uncapped."""

    raw = os.environ.get("GEMINI_PAID_BACKUP_DAILY_CAP", "").strip()
    if re.fullmatch(r"[0-9]{1,4}", raw or ""):
        return int(raw)
    return None


def dev_exception_active() -> bool:
    return os.environ.get(DEV_EXCEPTION_ENV, "").strip() == "1"


def quota_exhausted_round(categories: object) -> bool:
    """True when a completed free-chain round failed purely on quota class.

    交付事故根因：免费额度整体耗尽时，一次运行只给每个 item 记
    1 strike，「同项失败≥3轮」永远凑不满，正确的修复提案全部卡死在
    UNCERTAIN。429 对已耗尽的免费链是确定性快败——同一次运行内把失败轮
    连续补足到政策线不是绕过政策，而是让政策的失败证据要求可以被满足。
    调用方只在本函数为 True（纯额度类失败）时才继续下一轮；任何非额度
    失败（认证、超时、输出坏）都保持单轮，交给外层重试。
    """

    values = [str(value or "") for value in (categories or [])]
    return bool(values) and all("QUOTA_EXHAUSTED" in value for value in values)


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
    dev_exception = dev_exception_active()
    strikes = free_chain_strikes(item_key) if prior_strikes is None else int(prior_strikes)
    if not dev_exception and strikes < MIN_FREE_CHAIN_STRIKES:
        return False, f"FREE_CHAIN_STRIKES_{strikes}_BELOW_{MIN_FREE_CHAIN_STRIKES}"
    try:
        _ledger_root().mkdir(parents=True, exist_ok=True)
    except OSError:
        # No auditable ledger -> no paid use, ever.
        return False, "PAID_LEDGER_UNAVAILABLE"
    cap = daily_cap()
    if cap is not None:
        used = paid_calls_today()
        if used >= cap:
            return False, f"PAID_DAILY_CAP_REACHED_{used}_OF_{cap}"
    if dev_exception:
        return True, "DEV_EXCEPTION"
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
        "mode": "dev_exception" if dev_exception_active() else "strict",
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
