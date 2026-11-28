"""Single authority for CPA quality routing, quota fallback, and wall-clock budget.

Order follows OpenAI's model capability ranking: 6.2 Sol is the primary default
(6.1 Sol as transient OpenAI ladder step), and Gemini 4 Neon is the cross-route
fallback used strictly on definite quota exhaustion.
Credential order (Pro -> Plus -> sudocode) remains CPA server authority.
"""
from __future__ import annotations

import contextlib
import contextvars
import json
import os
from pathlib import Path
import shlex
import sys
from collections.abc import Callable, Iterator, Mapping
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .llm_client import LlmCall, LlmCallError, LlmConfig

CPA_MODEL_LADDER = ("gpt-6.2-sol", "gpt-6.1-sol")
TOP_CPA_MODEL = CPA_MODEL_LADDER[0]
CPA_QUOTA_FALLBACK_MODEL = "gemini-4-neon"
CPA_QUOTA_FALLBACK_MIN_OUTPUT_TOKENS = 32768
CPA_DEFER_QUOTA_FALLBACK_ENV = "AUTOSLICE_CPA_DEFER_QUOTA_FALLBACK"
_DEFER_QUOTA_FALLBACK: contextvars.ContextVar[bool] = contextvars.ContextVar("cpa_defer_quota_fallback", default=False)
# Health probes and explicit historical experiments are not fallback routes.
CPA_HEALTH_MODEL = "gpt-6-luna"
ASTRA_MODEL = "gpt-6-astra"
ASTRA_PREVIEW_MODEL = "gpt-6.1-astra"
RAW_AV_UNSUPPORTED_MODELS = frozenset(
    (*CPA_MODEL_LADDER, "gpt-6-sol", CPA_QUOTA_FALLBACK_MODEL, ASTRA_MODEL, ASTRA_PREVIEW_MODEL)
)
DEFAULT_CPA_ATTEMPT_TIMEOUT_SECONDS = 150

QUOTA_REFUSAL_CODES = frozenset({
    "organization_spend_limit_exceeded",
    "insufficient_quota",
    "insufficient_user_quota",
    "credit_balance_exhausted",
    "billing_hard_limit_reached",
    "usage_limit_reached",
})
QUOTA_REFUSAL_TYPES = frozenset({
    "insufficient_quota",
    "usage_limit_reached",
})


def definite_quota_error_code(raw: bytes | str | Mapping[str, object] | None) -> str | None:
    """Return the upstream error code/type when ``raw`` is a definite quota refusal, else None."""
    if raw is None:
        return None
    if isinstance(raw, (bytes, str)):
        text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
        try:
            parsed = json.loads(text)
        except ValueError:
            return None
    elif isinstance(raw, Mapping):
        parsed = raw
    else:
        return None
    if not isinstance(parsed, Mapping):
        return None
    err = parsed.get("error")
    target = err if isinstance(err, Mapping) else parsed
    code = target.get("code")
    if isinstance(code, str) and code in QUOTA_REFUSAL_CODES:
        return code
    err_type = target.get("type")
    if isinstance(err_type, str) and err_type in QUOTA_REFUSAL_TYPES:
        return err_type
    return None


def _integer(env: Mapping[str, str], name: str, default: int, *, minimum: int = 0) -> int:
    value = env.get(name, str(default))
    if not value.isdecimal() or int(value) < minimum:
        raise ValueError(f'{name} must be an integer >= {minimum}')
    return int(value)


def attempt_timeout_seconds(env: Mapping[str, str] | None = None) -> int:
    # Three-day medium n=4131: p99.9=135.7s; only 3 successful calls >150s.
    return _integer(os.environ if env is None else env,
                    'CPA_ATTEMPT_TIMEOUT_SECONDS', DEFAULT_CPA_ATTEMPT_TIMEOUT_SECONDS, minimum=1)


def model_ladder(model: str | None = None) -> tuple[str, ...]:
    """A ranked model selects its suffix; explicit probe/experiment models stay pinned."""
    if model is None:
        return CPA_MODEL_LADDER
    if model in CPA_MODEL_LADDER:
        return CPA_MODEL_LADDER[CPA_MODEL_LADDER.index(model):]
    return (model,)


def resolve_models(value: str | None = None, *, exact_model: bool = False) -> tuple[str, ...]:
    selected = (value or TOP_CPA_MODEL).split()
    if exact_model:
        if not value or len(selected) != 1 or selected[0] not in (*CPA_MODEL_LADDER, CPA_QUOTA_FALLBACK_MODEL):
            raise ValueError('CPA_EXACT_MODEL_REQUIRES_ONE_CANONICAL_MODEL')
        return tuple(selected)
    # Compatibility with explicitly supplied non-production test/experiment chains.
    return model_ladder(selected[0]) if len(selected) == 1 else tuple(selected)


def chain_deadline_seconds(env: Mapping[str, str] | None = None, *, models: int | None = None,
                           attempts: int = 3) -> int:
    env = os.environ if env is None else env
    models = len(CPA_MODEL_LADDER) if models is None else models
    retries = max(attempts, _integer(env, 'CPA_TRANSIENT_ATTEMPTS_PER_MODEL', 3, minimum=1))
    full = models * retries * attempt_timeout_seconds(env) + _integer(env, 'CPA_BACKOFF_MAX_TOTAL_SECONDS', 30)
    return _integer(env, 'CPA_DEADLINE_SECONDS', full)


def cpa_call_timeout_seconds(env: Mapping[str, str] | None = None, *, models: int | None = None,
                             attempts: int = 3) -> int:
    """Outer subprocess bound, including a last in-flight request and 60s overhead."""
    env = os.environ if env is None else env
    models = len(CPA_MODEL_LADDER) if models is None else models
    deadline = chain_deadline_seconds(env, models=models, attempts=attempts)
    if deadline == 0:
        retries = max(attempts, _integer(env, 'CPA_TRANSIENT_ATTEMPTS_PER_MODEL', 3, minimum=1))
        deadline = models * retries * attempt_timeout_seconds(env) + _integer(env, 'CPA_BACKOFF_MAX_TOTAL_SECONDS', 30)
    return deadline + attempt_timeout_seconds(env) + 60


def classify_http_error_code(status: int, body: bytes) -> str:
    if status == 400 and any(token in body for token in (
            b'group_capability_unavailable', '当前分组不支持'.encode(), b'current group does not support')):
        return 'group_capability_unavailable'
    quota_code = definite_quota_error_code(body)
    return quota_code if quota_code else f'HTTP_STATUS_{status}'


def is_definite_quota_code(code: object) -> bool:
    return isinstance(code, str) and (code in QUOTA_REFUSAL_CODES or code in QUOTA_REFUSAL_TYPES)


def is_definite_quota_exception(exc: BaseException) -> bool:
    if getattr(exc, "quota_fallback_exhausted", False):
        return False
    diag = getattr(exc, "provider_diagnostics", None)
    return isinstance(diag, Mapping) and is_definite_quota_code(diag.get("provider_error_code"))


def min_output_tokens_for_model(model: str | None, requested: int) -> int:
    return max(requested, CPA_QUOTA_FALLBACK_MIN_OUTPUT_TOKENS) if model == CPA_QUOTA_FALLBACK_MODEL else requested


def defer_quota_fallback_active(env: Mapping[str, str] | None = None) -> bool:
    target = os.environ if env is None else env
    return _DEFER_QUOTA_FALLBACK.get() or target.get(CPA_DEFER_QUOTA_FALLBACK_ENV) == "1"


@contextlib.contextmanager
def defer_quota_fallback() -> Iterator[None]:
    token = _DEFER_QUOTA_FALLBACK.set(True)
    try:
        yield
    finally:
        _DEFER_QUOTA_FALLBACK.reset(token)


def quota_fallback_action(code: object, err_code: str, attempt: int, attempts: int, model: str) -> str | None:
    del code, attempt, attempts
    if not is_definite_quota_code(err_code):
        return None
    if defer_quota_fallback_active():
        return 'RAISE'
    if CPA_QUOTA_FALLBACK_MODEL and CPA_QUOTA_FALLBACK_MODEL != model:
        return CPA_QUOTA_FALLBACK_MODEL
    return 'RAISE'


def rewrite_cpa_command_template(template: str, target_model: str, default_effort: str = "medium") -> str | None:
    parts = shlex.split(template or "")
    for idx, part in enumerate(parts):
        name = Path(part).name
        if name == "llm_via_cpa.sh":
            head, rest = parts[:idx + 1], list(parts[idx + 1:])
            while len(rest) < 2:
                rest.append("{prompt_file}" if not rest else "{completion_file}")
            if len(rest) == 2:
                rest.extend([target_model, default_effort])
            else:
                rest[2] = target_model
            return " ".join(
                part if part in ("{prompt_file}", "{completion_file}") else shlex.quote(part)
                for part in (*head, *rest)
            )
        if name == "llm_via_runtime_cpa_ssh.py":
            out = list(parts)
            for j, token in enumerate(out):
                if token == "--model" and j + 1 < len(out):
                    out[j + 1] = target_model
                    return " ".join(
                        p if p in ("{prompt_file}", "{completion_file}") else shlex.quote(p)
                        for p in out
                    )
    return None


def is_quota_fallback_redirect(routing: Mapping[str, object], answered: Mapping[str, object]) -> bool:
    return (answered.get("models") == [CPA_QUOTA_FALLBACK_MODEL]
            and routing.get("models") != [CPA_QUOTA_FALLBACK_MODEL])


def run_with_cpa_quota_fallback(
    call_obj: LlmCall, config: LlmConfig, cpa_text: bool, prompt: str,
    run_primary: LlmCall, build_call: Callable[[LlmConfig], LlmCall],
    error_cls: type[LlmCallError], fb_holder: dict[str, LlmCall],
) -> str:
    cur_id: Mapping[str, object] | None = getattr(call_obj, "cpa_cache_identity", None)
    if (not cpa_text or getattr(call_obj, "pinned_model_route", False) or not isinstance(cur_id, Mapping)
            or cur_id.get("models") == [CPA_QUOTA_FALLBACK_MODEL]):
        return run_primary(prompt)
    fb_config = fallback_llm_config(config)
    if fb_config is None:
        return run_primary(prompt)
    try:
        with defer_quota_fallback():
            return run_primary(prompt)
    except error_cls as exc:
        if not is_definite_quota_exception(exc):
            raise
        if "fb" not in fb_holder:
            fb_call = build_call(fb_config)
            fb_id = {**cur_id, "models": [CPA_QUOTA_FALLBACK_MODEL]}
            if "max_tokens" in fb_id:
                fb_id["max_tokens"] = fb_config.max_tokens
            setattr(fb_call, "cpa_cache_identity", fb_id)
            rb: Mapping[str, object] | None = getattr(call_obj, "provider_runtime_binding", None)
            if rb is not None:
                setattr(fb_call, "provider_runtime_binding", {
                    **rb, **({"provider_model": [CPA_QUOTA_FALLBACK_MODEL]} if "provider_model" in rb else {})})
            fb_holder["fb"] = fb_call
        try:
            return fb_holder["fb"](prompt)
        except error_cls as fb_exc:
            models = cur_id.get("models")
            p_model = models[0] if isinstance(models, list) and models else config.model or TOP_CPA_MODEL
            err_code = str(exc.provider_diagnostics.get("provider_error_code") or "")
            msg = f"CPA quota fallback failed: primary {p_model} quota exhausted ({err_code}); fallback {CPA_QUOTA_FALLBACK_MODEL} failed ({fb_exc})"
            out = error_cls(msg, provider_diagnostics=fb_exc.provider_diagnostics, response_received=fb_exc.response_received)
            setattr(out, "quota_fallback_exhausted", True)
            raise out from fb_exc


def fallback_llm_config(config: LlmConfig) -> LlmConfig | None:
    from dataclasses import replace

    env_base = dict(config.command_child_env) if config.command_child_env is not None else None
    if env_base is not None:
        env_base.pop(CPA_DEFER_QUOTA_FALLBACK_ENV, None)
    if config.transport == "direct":
        if not config.model or config.model == CPA_QUOTA_FALLBACK_MODEL:
            return None
        return replace(config, model=CPA_QUOTA_FALLBACK_MODEL,
                       max_tokens=min_output_tokens_for_model(CPA_QUOTA_FALLBACK_MODEL, config.max_tokens))
    if config.transport == "command":
        rewritten = rewrite_cpa_command_template(config.command_template or "", CPA_QUOTA_FALLBACK_MODEL)
        if rewritten is None:
            return None
        return replace(config, command_template=rewritten, command_child_env=env_base)
    return None


if __name__ == '__main__':
    if sys.argv[1] == 'models':
        print(' '.join(resolve_models(sys.argv[2] if len(sys.argv) > 2 else None)))
    elif sys.argv[1] == 'exact-model':
        print(' '.join(resolve_models(sys.argv[2] if len(sys.argv) > 2 else None, exact_model=True)))
    elif sys.argv[1] == 'timeout':
        print(attempt_timeout_seconds())
    elif sys.argv[1] == 'deadline':
        print(chain_deadline_seconds(models=int(sys.argv[2]), attempts=int(sys.argv[3])))
    elif sys.argv[1] == 'quota-fallback':
        print(CPA_QUOTA_FALLBACK_MODEL)
    elif sys.argv[1] == 'is-quota-file':
        try:
            with open(sys.argv[2], 'rb') as fh:
                found = definite_quota_error_code(fh.read(65536))
        except OSError:
            found = None
        if found:
            print(found)
            sys.exit(0)
        sys.exit(1)
