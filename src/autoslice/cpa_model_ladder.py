"""Single authority for CPA quality routing and its wall-clock budget.

Order follows OpenAI's model capability ranking, narrowed by 维护者's explicit
instruction: currently fall back only from 6.1 Sol to 6 Sol.
Credential order (Pro -> Plus -> sudocode) remains CPA server authority.
"""
from __future__ import annotations

import os
import sys
from collections.abc import Mapping

CPA_MODEL_LADDER = ("gpt-6.1-sol", "gpt-6-sol")
TOP_CPA_MODEL = CPA_MODEL_LADDER[0]
# Health probes and explicit historical experiments are not fallback routes.
CPA_HEALTH_MODEL = "gpt-6-luna"
ASTRA_MODEL = "gpt-6-astra"
ASTRA_PREVIEW_MODEL = "gpt-6.1-astra"
RAW_AV_UNSUPPORTED_MODELS = frozenset((*CPA_MODEL_LADDER, ASTRA_MODEL, ASTRA_PREVIEW_MODEL))
DEFAULT_CPA_ATTEMPT_TIMEOUT_SECONDS = 150


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


def resolve_models(value: str | None = None) -> tuple[str, ...]:
    selected = (value or TOP_CPA_MODEL).split()
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


if __name__ == '__main__':
    if sys.argv[1] == 'models':
        print(' '.join(resolve_models(sys.argv[2] if len(sys.argv) > 2 else None)))
    elif sys.argv[1] == 'timeout':
        print(attempt_timeout_seconds())
    elif sys.argv[1] == 'deadline':
        print(chain_deadline_seconds(models=int(sys.argv[2]), attempts=int(sys.argv[3])))
