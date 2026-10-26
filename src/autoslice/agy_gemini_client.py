"""One client for the whole ``AGY -> Gemini`` witness chain.

公开规则逐字裁定：「我认为这些地方不应该分散，反而是防止代码屎山的
重要决策，需要调用AGY->gemini 这条链的，全都复用一种接口才好」。

Before this module seven call sites each re-derived "where is agy", "did the
call fail and how", and "which Gemini key may I use next". Coverage was
wildly uneven: two sites (``agy_frame_witness``/``visual_song_discovery``) had
no Gemini leg at all and simply died wherever AGY was missing, while four
hardcoded an absolute ``agy``.

The second standing ruling this module encodes is 公开规则's repeated instruction
not to install AGY on wsl (that host holds the Gemini keys instead). **AGY
absence is therefore a supported deployment shape, not an incident**: every
leg here degrades to Gemini rather than raising, and the receipt says
``AGY_BINARY_ABSENT`` so operators do not go hunting for a broken AGY that was
never meant to exist on that box.

What is deliberately NOT unified: each call site keeps its own prompt, its own
answer schema, and its own key-ladder *policy knobs*. Two different rulings
are live at once —

* (entity/LRC lanes): a pure-quota free-chain round may be repeated
 back-to-back inside one run until the >=3 strike policy is satisfiable.
* (foreign-span lane): a fully-429 free chain is deterministic
 exhaustion, so the paid backup fires in the SAME round.

Collapsing those into one shape would silently rewrite a production policy, so
``run_gemini_key_ladder`` takes them as parameters instead. Key ORDER never
varies: AGY subscription -> free keys 1..3 -> policy-gated paid backup.
"""

from __future__ import annotations

from src.autoslice.evidence_identity import file_sha256 as _evidence_file_sha256
import base64
import hashlib
import json
import math
import numbers
import os
import re
import shutil
import subprocess
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from src.autoslice import gemini_backup_policy
from src.autoslice import cpa_gemini_client
from src.autoslice.structured_output import (
    agy_structured_args, current_contract, gemini_format, format_retry_call,
    contract_receipt,
    FormatRetryExhausted,
)
from src.autoslice.provider_slots import ProviderSlotTimeout, provider_wait_for_call, runtime_provider_slot

# AGY CLI display names are a separate namespace from Gemini REST model IDs.
# Keep caller effort and explicit overrides; one default family serves every
# ordinary AGY lane instead of independent stale per-caller version strings.
DEFAULT_AGY_FLASH_HIGH = "Gemini 3.8 Flash (High)"
DEFAULT_AGY_FLASH_LOW = "Gemini 3.8 Flash (Low)"

# --------------------------------------------------------------------------
# 1. Binary resolution
# --------------------------------------------------------------------------

# Both historical names stay accepted so no deployment env has to change.
# ``AGY_BIN`` is the majority spelling; ``AUTOSLICE_AGY_BIN`` came from the
# visual-song lane.
LOCAL_AGY_ENV_ALIASES: tuple[str, ...] = ("AGY_BIN", "AUTOSLICE_AGY_BIN")
LOCAL_AGY_HOME_RELATIVE = ".local/bin/agy"

# The SSH lanes run agy on the free host, where the production install really
# does live under root's home.  It is a REMOTE path, so it never had anything
# to do with whether the local box (wsl/mac) has agy — but it still belongs
# behind one env-overridable name instead of four string literals.
REMOTE_AGY_ENV = "AGY_REMOTE_BIN"
REMOTE_AGY_DEFAULT = "agy"


def local_agy_default() -> str:
    """Best-guess local agy path. Never an absolute ``/root/...`` literal."""

    return str(Path.home() / LOCAL_AGY_HOME_RELATIVE)


def resolve_local_agy_binary(
    explicit: str | os.PathLike[str] | None = None,
    *,
    env_names: Sequence[str] = LOCAL_AGY_ENV_ALIASES,
) -> str:
    """Resolve the local agy binary: explicit -> env aliases -> home -> PATH.

    Always returns a string.  A path that does not exist is a perfectly normal
    answer (see the module docstring): the caller runs it, gets
    ``FileNotFoundError``, and ``classify_agy_launch_error`` turns that into
    ``AGY_BINARY_ABSENT`` so the Gemini leg takes over.
    """

    if explicit:
        return str(explicit)
    for name in env_names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    home_candidate = local_agy_default()
    if Path(home_candidate).is_file():
        return home_candidate
    return shutil.which("agy") or home_candidate


def resolve_local_agy_executable(
    explicit: str | os.PathLike[str] | None = None,
    *,
    env_names: Sequence[str] = LOCAL_AGY_ENV_ALIASES,
) -> str:
    """``resolve_local_agy_binary`` plus a PATH lookup for bare names."""

    binary = resolve_local_agy_binary(explicit, env_names=env_names)
    return shutil.which(binary) or binary


def local_agy_available(
    explicit: str | os.PathLike[str] | None = None,
    *,
    env_names: Sequence[str] = LOCAL_AGY_ENV_ALIASES,
    require_executable: bool = False,
) -> bool:
    """True when a local agy binary is present.

    Presence is ``is_file()`` by default — the same test every pre-existing
    call site used, and the one the suite's non-executable ``fake_agy``
    fixtures rely on.  Callers that genuinely need the exec bit opt in.
    """

    resolved = resolve_local_agy_executable(explicit, env_names=env_names)
    if not Path(resolved).is_file():
        return False
    return os.access(resolved, os.X_OK) if require_executable else True


def resolve_remote_agy_binary() -> str:
    """Remote (free-host) agy path, env-overridable via ``AGY_REMOTE_BIN``."""

    return os.environ.get(REMOTE_AGY_ENV, "").strip() or REMOTE_AGY_DEFAULT


# --------------------------------------------------------------------------
# 2. AGY failure classification
# --------------------------------------------------------------------------

AGY_BINARY_ABSENT = "AGY_BINARY_ABSENT"
AGY_SUBPROCESS_ERROR = "AGY_SUBPROCESS_ERROR"
AGY_TIMEOUT = "AGY_TIMEOUT"
AGY_QUOTA_EXHAUSTED = "AGY_QUOTA_EXHAUSTED"

# An explicit, account-wide quota sentence.  Deliberately narrow: a bare "429"
# in a log line is a transient rate limit, not an exhausted subscription.
EXPLICIT_AGY_QUOTA_RX = re.compile(
    r"\b(?:individual\s+)?quota\s+(?:(?:has\s+been|is)\s+)?"
    r"(?:reached|exhausted|exceeded)\b",
    re.IGNORECASE,
)


def classify_agy_launch_error(exc: BaseException) -> str:
    """Separate "agy is not installed here" from "agy blew up"."""

    if isinstance(exc, ProviderSlotTimeout):
        return AGY_TIMEOUT
    if isinstance(exc, subprocess.TimeoutExpired):
        return AGY_TIMEOUT
    if isinstance(exc, FileNotFoundError):
        return AGY_BINARY_ABSENT
    return AGY_SUBPROCESS_ERROR


# POSIX shells report "command not found" as 127.  On the SSH lanes that is
# exactly the remote-side twin of a local FileNotFoundError: agy is not
# installed on that host, which is a supported shape, not an incident.
REMOTE_AGY_NOT_FOUND_RC = 127


def classify_remote_agy_rc(rc: int | None) -> str:
    """Classify the ``rc=N`` line an SSH agy job leaves behind."""

    if rc is None:
        return AGY_TIMEOUT
    if rc == REMOTE_AGY_NOT_FOUND_RC:
        return AGY_BINARY_ABSENT
    return f"AGY_FAILED_RC_{rc}"


def parse_remote_rc_line(rc_line: str) -> int | None:
    """``"rc=127"`` -> 127; anything unparseable -> None."""

    match = re.fullmatch(r"\s*rc=(-?\d+)\s*", str(rc_line or ""))
    return int(match.group(1)) if match else None


def classify_agy_failure(returncode: int, stdout: str, stderr: str) -> str:
    """Granular non-zero classification (entity-lane semantics)."""

    diagnostic = f"{stdout}\n{stderr}".casefold()
    if EXPLICIT_AGY_QUOTA_RX.search(diagnostic):
        return AGY_QUOTA_EXHAUSTED
    if any(marker in diagnostic for marker in ("timeout", "timed out")):
        return AGY_TIMEOUT
    if any(marker in diagnostic for marker in ("429", "rate limit", "too many requests")):
        return "AGY_RATE_LIMITED"
    if re.search(r"\b5[0-9]{2}\b", diagnostic):
        return "AGY_SERVER_ERROR"
    return f"AGY_FAILED_RC_{returncode}"


@dataclass(frozen=True)
class AgyRun:
    """Outcome of one local agy subprocess attempt."""

    completed: subprocess.CompletedProcess | None
    failure_category: str | None
    launch_error_type: str | None = None
    launch_error: BaseException | None = None
    format_rejected: bool = False
    transport_receipt: cpa_gemini_client.JsonMap | None = None

    @property
    def launched(self) -> bool:
        return self.completed is not None and self.transport_receipt is None

    def partial_output(self) -> tuple[str, str]:
        """(stdout, stderr) salvaged from a timed-out run; empty otherwise."""

        exc = self.launch_error
        return (
            _as_text(getattr(exc, "stdout", None)),
            _as_text(getattr(exc, "stderr", None)),
        )

    @property
    def returncode(self) -> int | None:
        return None if self.completed is None else self.completed.returncode

    @property
    def stdout(self) -> str:
        return "" if self.completed is None else (self.completed.stdout or "")

    @property
    def stderr(self) -> str:
        return "" if self.completed is None else (self.completed.stderr or "")


def agy_argv(
    binary: str,
    *,
    job_dir: str | os.PathLike[str],
    model: str,
    prompt: str,
    print_timeout: str,
    response_schema: Mapping[str, Any] | None = None,
    json_mode: bool = False,
    schema_path: str | os.PathLike[str] | None = None,
) -> list[str]:
    """The one sandboxed agy command line every local caller uses."""

    argv = [
        str(binary),
        "--sandbox",
        "--dangerously-skip-permissions",
        "--add-dir",
        str(job_dir),
        "--model",
        model,
        "-p",
        prompt,
        "--print-timeout",
        print_timeout,
    ]
    if json_mode or response_schema is not None or schema_path is not None:
        if schema_path is None:
            schema = current_contract(response_schema)['schema'] or {'type': 'object'}
            schema_text = json.dumps(schema, ensure_ascii=False, sort_keys=True)
            schema_path = Path(job_dir) / ('response-schema-' + hashlib.sha256(schema_text.encode()).hexdigest()[:16] + '.json')
            Path(job_dir).mkdir(parents=True, exist_ok=True)
            Path(schema_path).write_text(schema_text, encoding='utf-8')
        argv.extend(agy_structured_args(schema_path))
    return argv


def run_local_agy(
    argv: Sequence[str],
    *,
    cwd: str | os.PathLike[str],
    timeout: float,
    env: Mapping[str, str] | None = None,
    command_runner: Callable[..., subprocess.CompletedProcess] | None = None,
    response_validator: Callable[[subprocess.CompletedProcess], object] | None = None,
    cpa_prompt: str | None = None,
    cpa_files: Sequence[Path] = (),
    cpa_output_path: Path | None = None,
    cpa_response_mime_type: str | None = "application/json",
    cpa_response_schema: cpa_gemini_client.JsonMap | None = None,
) -> AgyRun:
    """Run agy locally, never raising for absence/timeout/spawn failure.

    ``subprocess.run`` is looked up on the module object at call time so the
    suite's existing global ``monkeypatch.setattr(mod.subprocess, "run", ...)``
    seams keep working after this extraction.
    """

    if cpa_gemini_client.enabled():
        if cpa_prompt is None:
            raise ValueError("CPA_GEMINI_REQUEST_DESCRIPTION_REQUIRED")
        model = list(argv)[list(argv).index('--model') + 1]
        schema = cpa_response_schema
        if schema is None and '--json-schema' in argv:
            schema = json.loads(Path(list(argv)[list(argv).index('--json-schema') + 1]).read_text(encoding='utf-8'))
        completion = cpa_gemini_client.generate_files(
            prompt=cpa_prompt, files=cpa_files, model=model,
            purpose="local_agy_compatibility", timeout_seconds=timeout,
            response_mime_type=cpa_response_mime_type, response_schema=schema)
        if cpa_output_path is not None:
            cpa_output_path.write_text(completion.text, encoding='utf-8')
        Path(cwd, 'cpa-gemini.receipt.json').write_text(
            json.dumps(dict(completion.receipt), sort_keys=True), encoding='utf-8')
        # Compatibility container only; no process or AGY tool trace is claimed.
        completed = subprocess.CompletedProcess(list(argv), 0, completion.text, '')
        if response_validator is not None:
            response_validator(completed)
        return AgyRun(completed=completed, failure_category=None,
                      transport_receipt=completion.receipt)
    runner = command_runner or subprocess.run
    format_rejected = False
    kwargs: dict[str, Any] = {
        "cwd": str(cwd),
        "check": False,
        "capture_output": True,
        "text": True,
        "timeout": timeout,
    }
    if env is not None:
        kwargs["env"] = dict(env)
    try:
        with runtime_provider_slot(provider='agy', modality='audio_video',
                                   timeout_seconds=provider_wait_for_call(timeout)) as lease:
            if response_validator is None:
                completed = runner(list(argv), **kwargs)
            else:
                attempts = []
                original_prompt = list(argv)[list(argv).index('-p') + 1]
                def send(actual_prompt):
                    nonlocal completed
                    command = list(argv)
                    command[command.index('-p') + 1] = actual_prompt
                    completed = runner(command, **kwargs)
                    return completed.stdout or ''
                def validate(_raw):
                    if completed.returncode == 0:
                        response_validator(completed)
                try:
                    format_retry_call(original_prompt, send, validate=validate,
                        record=attempts.append, return_invalid=True)
                    format_rejected = attempts[-1]['status'] == 'FORMAT_REJECTED'
                finally:
                    Path(cwd, 'agy.format-attempts.json').write_text(
                        json.dumps({'attempts': attempts, 'physical_calls': len(attempts),
                            'observation_count': 1,
                            'original_prompt_sha256': hashlib.sha256(original_prompt.encode()).hexdigest(),
                            'model': list(argv)[list(argv).index('--model') + 1],
                            'schema_sha256': _evidence_file_sha256(Path(list(argv)[list(argv).index('--json-schema') + 1]))
                                if '--json-schema' in argv else None}, indent=2), encoding='utf-8')
            if lease is not None:
                if completed.returncode != 0:
                    lease.report({'status': 'FAILED', 'provider_error_code': classify_agy_failure(
                        completed.returncode, completed.stdout or '', completed.stderr or '')})
                try:
                    lease.report(json.loads(completed.stdout or '{}'))
                except (ValueError, TypeError):
                    pass
    except (OSError, ProviderSlotTimeout, subprocess.TimeoutExpired) as exc:
        return AgyRun(
            completed=None,
            failure_category=classify_agy_launch_error(exc),
            launch_error_type=type(exc).__name__,
            launch_error=exc,
        )
    return AgyRun(completed=completed, failure_category=None, format_rejected=format_rejected)


def _as_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


# --------------------------------------------------------------------------
# 3. Gemini keys, failure classes and the shared ladder
# --------------------------------------------------------------------------

FREE_KEY_ENV_NAMES: tuple[str, ...] = (
    "GEMINI_API_KEY",
    "GEMINI_API_KEY_2",
    "GEMINI_API_KEY_3",
)


def free_api_keys() -> list[str]:
    """Distinct configured free keys, in policy order. Values are never logged."""

    if cpa_gemini_client.enabled():
        return ["CPA_MANAGED"]  # Compatibility token, never a Google credential.
    return list(
        dict.fromkeys(
            value
            for name in FREE_KEY_ENV_NAMES
            if (value := os.environ.get(name))
        )
    )


def classify_gemini_failure(exc: BaseException) -> str:
    """Bounded structural category for one failed Gemini attempt."""

    if isinstance(exc, GeminiRequestConfigError):
        return "GEMINI_API_REQUEST_CONFIG_ERROR"
    status = getattr(exc, "code", None)
    if status == 429:
        return "GEMINI_API_QUOTA_EXHAUSTED"
    if isinstance(status, int) and 500 <= status <= 599:
        return "GEMINI_API_SERVER_ERROR"
    if status in {401, 403}:
        return "GEMINI_API_AUTH_FAILED"
    if isinstance(exc, (TimeoutError, subprocess.TimeoutExpired)) or isinstance(
        getattr(exc, "reason", None), TimeoutError
    ):
        return "GEMINI_API_TIMEOUT"
    if isinstance(exc, (json.JSONDecodeError, ValueError)):
        return "GEMINI_API_INVALID_OUTPUT"
    return "GEMINI_API_REQUEST_FAILED"


def is_quota_error(exc: BaseException) -> bool:
    """HTTP 429 / quota-class detection across urllib and requests shapes."""

    status = getattr(exc, "code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    if status == 429:
        return True
    text = f"{type(exc).__name__}: {exc}".lower()
    return "429" in text or "resource_exhausted" in text or "quota" in text


@dataclass(frozen=True)
class GeminiAttemptFailure:
    """One failed key attempt, in the shape every receipt already records."""

    key_tier: str
    key_ordinal: int
    attempt_round: int
    category: str
    exception: BaseException

    @property
    def error_type(self) -> str:
        return type(self.exception).__name__

    @property
    def http_status(self) -> int | None:
        status = getattr(self.exception, "code", None)
        return status if isinstance(status, int) else None


@dataclass
class LadderOutcome:
    """What the ladder accepted (if anything) plus its accounting proof."""

    observed: Any = None
    accepted_key_tier: str | None = None
    accepted_key_ordinal: int | None = None
    configured_key_count: int = 0
    paid_policy_stamp: Mapping[str, Any] | None = None
    paid_gate_reason: str | None = None
    last_attempt_round: int = 1

    @property
    def accepted(self) -> bool:
        return self.accepted_key_tier is not None


def quota_fastpath_paid_gate(
    item_key: str, quota_flags: object, *, key_configured: bool | None = None,
) -> tuple[bool, str] | None:
    """Same-round paid admission after a fully quota-class free chain.

 公开规则：先调用AGY订阅，然后用免费key，最后用付费key「付费key不能加
 冷却期」; a free chain that came back entirely HTTP 429 is
 deterministic quota exhaustion, so the paid key steps in within the same
 round (the >= 3 strikes rule only applies to non-quota failures).
 Returns ``None`` when the chain was not purely quota-class; otherwise the
 ``gemini_backup_policy`` gate decision. The caller ledgers every paid call
 with ``record_paid_use`` before dispatch.
 """

    flags = list(quota_flags or [])
    if not flags or not all(flags):
        return None
    allowed, reason = gemini_backup_policy.paid_attempt_allowed(
        item_key, prior_strikes=gemini_backup_policy.MIN_FREE_CHAIN_STRIKES,
        key_configured=key_configured,
    )
    return allowed, f"QUOTA_FASTPATH:{reason}"


def run_gemini_key_ladder(
    *,
    item_key: str,
    observe: Callable[[str], Any],
    purpose: str,
    record_failure: Callable[[GeminiAttemptFailure], None] | None = None,
    record_paid_skipped: Callable[[int, int, str], None] | None = None,
    classify: Callable[[BaseException], str] = classify_gemini_failure,
    max_free_rounds: int | None = None,
    quota_fastpath: bool = False,
    quota_predicate: Callable[[BaseException], bool] = is_quota_error,
    silent_when_paid_unconfigured: bool = True,
    strike_on_empty_chain: bool = False,
) -> LadderOutcome:
    """Free keys 1..N in rounds, then the policy-gated paid backup key.

    ``observe(key)`` is the caller's WHOLE attempt (request + validate +
    persist).  It returns the accepted object or raises; the ladder never
    inspects its shape.  Passing it in also keeps each module's own
    monkeypatch seam (``mod._gemini_api_observe`` and friends) live.

    Policy knobs, both real and both currently in production:

    ``max_free_rounds``
        Repeat the free chain up to N times inside one run, but only while
        every failure in the round was quota-class .  Defaults to
        ``gemini_backup_policy.MIN_FREE_CHAIN_STRIKES``.
    ``quota_fastpath``
        Single round; if the whole free chain came back quota-class, treat the
        strike requirement as already met and let the paid key fire in this
        same round .
    """

    if cpa_gemini_client.enabled():
        # Exactly one dispatch. No client key reads, strikes, cooldown or paid gate.
        outcome = LadderOutcome(configured_key_count=0)
        try:
            outcome.observed = observe("CPA_MANAGED")
        except Exception as exc:
            if record_failure is not None:
                record_failure(GeminiAttemptFailure("UNKNOWN", 0, 1, classify(exc), exc))
            return outcome
        outcome.accepted_key_tier = "UNKNOWN"
        return outcome
    rounds = (
        1
        if quota_fastpath
        else int(max_free_rounds or gemini_backup_policy.MIN_FREE_CHAIN_STRIKES)
    )
    free_keys = free_api_keys()
    outcome = LadderOutcome(configured_key_count=len(free_keys))
    # Two different quota notions, both load-bearing and NOT interchangeable:
    #   categories -> gemini_backup_policy.quota_exhausted_round, the strict
    #     "was this whole round GEMINI_API_QUOTA_EXHAUSTED" test that decides
    #     whether another free round may run .
    #   flags      -> the broad 429/resource-exhausted sniff that decides the
    #     same-round paid fastpath .
    categories: list[str] = []
    quota_flags: list[bool] = []
    format_exhausted = False

    def attempt(key: str, *, key_tier: str, key_ordinal: int, attempt_round: int) -> bool:
        nonlocal format_exhausted
        try:
            observed = observe(key)
        except Exception as exc:  # each key is an independent failover lane
            format_exhausted = isinstance(exc, FormatRetryExhausted)
            category = classify(exc)
            categories.append(category)
            quota_flags.append(quota_predicate(exc))
            if record_failure is not None:
                record_failure(
                    GeminiAttemptFailure(
                        key_tier=key_tier,
                        key_ordinal=key_ordinal,
                        attempt_round=attempt_round,
                        category=category,
                        exception=exc,
                    )
                )
            return False
        outcome.observed = observed
        outcome.accepted_key_tier = key_tier
        outcome.accepted_key_ordinal = key_ordinal
        return True

    for attempt_round in range(1, rounds + 1):
        outcome.last_attempt_round = attempt_round
        round_start = len(categories)
        for key_ordinal, key in enumerate(free_keys, start=1):
            if attempt(
                key,
                key_tier=gemini_backup_policy.FREE_KEY_TIER,
                key_ordinal=key_ordinal,
                attempt_round=attempt_round,
            ):
                return outcome
            if format_exhausted:
                return outcome
        if not item_key or (not free_keys and not strike_on_empty_chain):
            break
        strikes = gemini_backup_policy.record_free_chain_failure(item_key)
        if quota_fastpath or strikes >= gemini_backup_policy.MIN_FREE_CHAIN_STRIKES:
            break
        if not gemini_backup_policy.quota_exhausted_round(categories[round_start:]):
            break

    if not item_key:
        return outcome

    paid_ordinal = len(free_keys) + 1
    fastpath = quota_fastpath_paid_gate(item_key, quota_flags) if quota_fastpath else None
    if fastpath is not None:
        allowed, gate_reason = fastpath
    else:
        allowed, gate_reason = gemini_backup_policy.paid_attempt_allowed(item_key)
    outcome.paid_gate_reason = gate_reason
    if allowed:
        # Ledger every paid call, including failed requests or validation.
        # Persist before dispatch so a ledger error cannot leave an untracked call.
        paid_policy_stamp = gemini_backup_policy.record_paid_use(
            item_key, purpose=purpose,
            admission=gate_reason if fastpath is not None else None,
        )
        if attempt(
            str(gemini_backup_policy.paid_backup_key()),
            key_tier=gemini_backup_policy.PAID_KEY_TIER,
            key_ordinal=paid_ordinal,
            attempt_round=outcome.last_attempt_round,
        ):
            outcome.paid_policy_stamp = paid_policy_stamp
    elif record_paid_skipped is not None:
        unconfigured = gate_reason.endswith("PAID_KEY_NOT_CONFIGURED")
        if not (silent_when_paid_unconfigured and unconfigured):
            record_paid_skipped(paid_ordinal, outcome.last_attempt_round, gate_reason)
    return outcome


# --------------------------------------------------------------------------
# 4. Direct Gemini generateContent (the leg AGY-less hosts actually run on)
# --------------------------------------------------------------------------

GEMINI_API_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
)
GEMINI_REQUEST_MAX_BYTES = 20_000_000
GEMINI_VISION_MODEL_ENV = "AGY_GEMINI_VISION_MODEL"
GEMINI_VISION_MODEL_DEFAULT = "gemini-3.6-flash"

# These are exact REST model IDs.  Keep this table small and explicit: an
# unknown model may be a future provider addition with different generation
# controls, so optional knobs must fail closed instead of being guessed from a
# prefix such as ``gemini-3``.
GEMINI_THINKING_LEVELS: dict[str, frozenset[str]] = {
    "gemini-3-flash-preview": frozenset({"MINIMAL", "LOW", "MEDIUM", "HIGH"}),
    "gemini-3.5-flash": frozenset({"MINIMAL", "LOW", "MEDIUM", "HIGH"}),
    "gemini-3.6-flash": frozenset({"MINIMAL", "LOW", "MEDIUM", "HIGH"}),
    "gemini-3.7-flash": frozenset({"LOW", "MEDIUM", "HIGH"}),
    "gemini-3.8-flash": frozenset({"LOW", "MEDIUM", "HIGH"}),
    "gemini-3-pro-preview": frozenset({"LOW", "HIGH"}),
    "gemini-3.1-pro-preview": frozenset({"LOW", "MEDIUM", "HIGH"}),
}
GEMINI_LEGACY_THINKING_BUDGET_MODELS = frozenset(
    {"gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.5-flash-lite"}
)
_GENERATION_CONFIG_KEYS = frozenset({"thinkingBudget", "thinkingLevel"})


class GeminiRequestConfigError(ValueError):
    """A request parameter combination rejected before contacting Gemini."""


def model_supports_thinking_level(model: str) -> bool:
    """Return whether *model* has an explicitly supported thinking-level enum."""

    return isinstance(model, str) and model.strip() in GEMINI_THINKING_LEVELS


def model_supports_thinking_budget(model: str) -> bool:
    """Return whether *model* uses the legacy integer thinking budget field."""

    return isinstance(model, str) and model.strip() in GEMINI_LEGACY_THINKING_BUDGET_MODELS


def _validate_temperature(model: str, temperature: float | None) -> float | None:
    if temperature is None:
        return None
    if isinstance(temperature, bool) or not isinstance(temperature, numbers.Real):
        raise GeminiRequestConfigError(
            "temperature must be a finite number when explicitly configured"
        )
    value = float(temperature)
    if not math.isfinite(value):
        raise GeminiRequestConfigError(
            "temperature must be a finite number when explicitly configured"
        )
    model_name = model.strip() if isinstance(model, str) else model
    if model_name not in GEMINI_LEGACY_THINKING_BUDGET_MODELS:
        raise GeminiRequestConfigError(
            f"temperature is not permitted by the project Gemini generation "
            f"compatibility policy for model {model!r}; omit custom sampling for "
            "known Gemini 3 models and unknown models"
        )
    return value


def _build_thinking_config(
    model: str, thinking: Mapping[str, Any] | None
) -> dict[str, Any] | None:
    if thinking is None:
        return None
    if not isinstance(thinking, Mapping):
        raise GeminiRequestConfigError("thinking must be a mapping of REST camelCase fields")
    if not thinking:
        return None

    keys = set(thinking)
    unknown = keys - _GENERATION_CONFIG_KEYS
    if unknown:
        rendered = ", ".join(sorted(map(str, unknown)))
        raise GeminiRequestConfigError(
            "unsupported thinking field(s): "
            f"{rendered}; use REST camelCase thinkingBudget/thinkingLevel"
        )
    if keys == _GENERATION_CONFIG_KEYS:
        raise GeminiRequestConfigError(
            "thinkingBudget and thinkingLevel are mutually exclusive"
        )

    model_name = model.strip() if isinstance(model, str) else model
    if "thinkingBudget" in keys:
        if model_name not in GEMINI_LEGACY_THINKING_BUDGET_MODELS:
            if model_name in GEMINI_THINKING_LEVELS:
                raise GeminiRequestConfigError(
                    f"thinkingBudget is not permitted by the project Gemini "
                    f"generation compatibility policy for level model {model!r}; "
                    "use SONG_GEMINI_API_THINKING_LEVEL or thinkingLevel"
                )
            raise GeminiRequestConfigError(
                f"thinkingBudget is unsupported for unknown model {model!r}"
            )
        value = thinking["thinkingBudget"]
        if isinstance(value, bool) or not isinstance(value, int):
            raise GeminiRequestConfigError("thinkingBudget must be an integer, not bool")
        if value < -1:
            raise GeminiRequestConfigError("thinkingBudget must be -1 or a non-negative integer")
        return {"thinkingBudget": value}

    if model_name not in GEMINI_THINKING_LEVELS:
        if model_name in GEMINI_LEGACY_THINKING_BUDGET_MODELS:
            raise GeminiRequestConfigError(
                f"thinkingLevel is unsupported for legacy budget model {model!r}"
            )
        raise GeminiRequestConfigError(
            f"thinkingLevel is unsupported for unknown model {model!r}"
        )
    value = thinking["thinkingLevel"]
    if not isinstance(value, str):
        raise GeminiRequestConfigError("thinkingLevel must be a string enum value")
    canonical = value.strip().upper()
    if canonical not in GEMINI_THINKING_LEVELS[model_name]:
        allowed = ", ".join(sorted(GEMINI_THINKING_LEVELS[model_name]))
        raise GeminiRequestConfigError(
            f"thinkingLevel {value!r} is unsupported for model {model!r}; "
            f"allowed values: {allowed}"
        )
    return {"thinkingLevel": canonical}


def build_generation_config(
    *,
    model: str,
    temperature: float | None = None,
    max_output_tokens: int = 65_536,
    response_mime_type: str | None = "application/json",
    thinking: Mapping[str, Any] | None = None,
    response_schema: Mapping[str, Any] | None = None,
    json_mode: bool | None = None,
) -> dict[str, Any]:
    """Build a REST ``generationConfig`` with model-aware optional fields.

    The shared client deliberately accepts the existing ``thinking`` mapping
    seam, but validates its REST camelCase keys and emits no sampling field by
    default.  Validation runs before request construction/network I/O.
    """

    if not isinstance(model, str) or not model.strip():
        raise GeminiRequestConfigError("model must be a non-empty string")
    temperature_value = _validate_temperature(model, temperature)
    thinking_config = _build_thinking_config(model, thinking)
    generation_config: dict[str, Any] = {"maxOutputTokens": max_output_tokens}
    if temperature_value is not None:
        generation_config["temperature"] = temperature_value
    if response_mime_type:
        generation_config["responseMimeType"] = response_mime_type
    generation_config.update(gemini_format(response_schema,
        json_mode=response_mime_type == 'application/json' if json_mode is None else json_mode))
    if thinking_config is not None:
        generation_config["thinkingConfig"] = thinking_config
    return generation_config


def gemini_vision_model() -> str:
    return os.environ.get(GEMINI_VISION_MODEL_ENV, "").strip() or GEMINI_VISION_MODEL_DEFAULT


def _response_text(payload: object) -> str:
    candidates = payload.get("candidates") if isinstance(payload, dict) else None
    candidate = candidates[0] if isinstance(candidates, list) and candidates else None
    content = candidate.get("content") if isinstance(candidate, dict) else None
    parts = content.get("parts") if isinstance(content, dict) else None
    if not isinstance(parts, list):
        return ""
    return "".join(
        str(part.get("text") or "") for part in parts if isinstance(part, dict)
    )


def generate_content(
    *,
    prompt: str,
    key: str,
    model: str,
    inline_data: bytes | None = None,
    mime_type: str = "image/jpeg",
    inline_parts: Sequence[tuple[bytes, str]] | None = None,
    response_mime_type: str | None = "application/json",
    temperature: float | None = None,
    max_output_tokens: int = 65_536,
    thinking: Mapping[str, Any] | None = None,
    timeout_seconds: int = 180,
    max_request_bytes: int = GEMINI_REQUEST_MAX_BYTES,
    degrade_on_400: bool = False,
    response_schema: Mapping[str, Any] | None = None,
    json_mode: bool | None = None,
    response_validator: Callable[[str], object] | None = None,
) -> str:
    """One inline-payload Gemini request; the key lives only in the header.

    Handles audio (``audio/mpeg``) and vision (``image/jpeg``) identically —
    the only difference is the declared mime type.  ``degrade_on_400`` remains
    as a compatibility argument for callers, but HTTP 400 is always propagated
    after the single unchanged request; silently dropping thinking controls can
    change the evidence produced by a lane.
    """

    payloads: list[tuple[bytes, str]] = list(inline_parts or [])
    if inline_data is not None:
        payloads.insert(0, (inline_data, mime_type))
    parts: list[dict[str, Any]] = [{"text": prompt}]
    parts.extend(
        {
            "inline_data": {
                "mime_type": part_mime,
                "data": base64.b64encode(part_bytes).decode("ascii"),
            }
        }
        for part_bytes, part_mime in payloads
    )
    generation_config = build_generation_config(
        model=model,
        temperature=temperature,
        max_output_tokens=max_output_tokens,
        response_mime_type=response_mime_type,
        thinking=thinking,
        response_schema=response_schema,
        json_mode=json_mode,
    )
    body: dict[str, Any] = {
        "contents": [{"parts": parts}],
        "generationConfig": generation_config,
    }
    observation_input_sha256 = hashlib.sha256(json.dumps({
        'model': model, 'prompt': prompt,
        'parts': [(hashlib.sha256(data).hexdigest(), mime) for data, mime in payloads]},
        sort_keys=True).encode()).hexdigest()
    physical_attempt = 0

    def post(request_body: Mapping[str, Any]) -> object:
        nonlocal physical_attempt
        physical_attempt += 1
        request_bytes = json.dumps(request_body, ensure_ascii=False).encode("utf-8")
        if len(request_bytes) > max_request_bytes:
            raise RuntimeError("GEMINI_API_REQUEST_TOO_LARGE")
        if cpa_gemini_client.enabled():
            import time
            started, cpa_status = time.monotonic(), 'TRANSPORT_ERROR'
            try:
                completion = cpa_gemini_client.generate_native(
                    model=model, body=request_body, purpose="generate_content",
                    timeout_seconds=timeout_seconds)
                cpa_status = completion.receipt['http_status']
                return completion.payload
            except Exception as exc:
                cpa_status = getattr(exc, 'code', cpa_status)
                raise
            finally:
                if any(part_mime.startswith('audio/') for _, part_mime in payloads):
                    from src.autoslice.cpa_runtime import record_cpa_event
                    fields = {'provider': 'gemini_api', 'model': model,
                        'transport': 'cpa_gemini_native', 'status': cpa_status,
                        'observation_input_sha256': observation_input_sha256,
                        'format_attempt': physical_attempt, 'elapsed_seconds': round(time.monotonic() - started, 3)}
                    record_cpa_event('audio_model_request', fields)
                    record_cpa_event('audio_http_attempt', fields)
        request = urllib.request.Request(
            GEMINI_API_URL.format(model=urllib.parse.quote(model, safe="")),
            data=request_bytes,
            headers={"content-type": "application/json", "x-goog-api-key": key},
        )
        transport_timeout = min(600, max(30, int(timeout_seconds)))
        with runtime_provider_slot(
            timeout_seconds=provider_wait_for_call(transport_timeout),
            provider='gemini', modality=('audio_video' if any(mime.startswith('video/') for _, mime in payloads)
                else 'audio' if any(mime.startswith('audio/') for _, mime in payloads)
                else 'vision' if payloads else 'text'),
        ):
            import time
            started, status = time.monotonic(), 'TRANSPORT_ERROR'
            try:
                with urllib.request.urlopen(request, timeout=transport_timeout) as response:
                    status = getattr(response,'status',200)
                    return json.load(response)
            except Exception as exc:
                status = getattr(exc,'code',status)
                raise
            finally:
                if any(part_mime.startswith('audio/') for _,part_mime in payloads):
                    from src.autoslice.cpa_runtime import record_cpa_event
                    fields = {'provider':'gemini_api','model':model,'status':status,
                              'observation_input_sha256': observation_input_sha256,
                              'format_attempt': physical_attempt,
                              'elapsed_seconds':round(time.monotonic()-started,3)}
                    record_cpa_event('audio_model_request',fields)
                    record_cpa_event('audio_http_attempt',fields)

    def send(actual_prompt):
        request_body = {**body, 'contents': [{'parts': [{'text': actual_prompt}, *parts[1:]]}]}
        return _response_text(post(request_body))
    from src.autoslice.cpa_runtime import record_cpa_event
    receipt = contract_receipt(current_contract(response_schema,
        json_mode=response_mime_type == 'application/json' if json_mode is None else json_mode))
    return format_retry_call(prompt, send, validate=response_validator,
        exhausted_error=True,
        record=lambda attempt_receipt: record_cpa_event('audio_format_attempt' if any(
            part_mime.startswith('audio/') for _, part_mime in payloads) else 'vision_format_attempt',
            {'provider': 'gemini_api', 'model': model, 'observation_input_sha256': observation_input_sha256,
             **attempt_receipt, **receipt}))
