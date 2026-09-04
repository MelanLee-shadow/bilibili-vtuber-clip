"""Small, dependency-injectable helpers for the CPA runtime boundary."""

from __future__ import annotations

import json
import os
import stat
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any


def resolve_cpa_env_path(
    default_path: Path,
    *,
    environment: Mapping[str, str] | None = None,
) -> Path:
    """Resolve and validate the explicitly bound CPA credential file.

    Free's default remains ``<AUTOSLICE_BASE>/cpa.env``.  An isolated runner
    may bind another credential file with ``AUTOSLICE_CPA_ENV``; the override
    is validated on every use so a late environment change cannot silently
    fall back to the default.
    """

    env = os.environ if environment is None else environment
    raw_override = env.get("AUTOSLICE_CPA_ENV")
    if raw_override is None:
        return default_path
    if not raw_override:
        raise RuntimeError("AUTOSLICE_CPA_ENV must be a non-empty absolute path")
    path = Path(raw_override)
    if not path.is_absolute():
        raise RuntimeError("AUTOSLICE_CPA_ENV must be an absolute path")
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise RuntimeError(f"AUTOSLICE_CPA_ENV is unavailable: {path}") from exc
    if os.path.islink(path) or not stat.S_ISREG(info.st_mode):
        raise RuntimeError(f"AUTOSLICE_CPA_ENV must be a regular non-symlink file: {path}")
    if info.st_uid != os.getuid():
        raise RuntimeError(f"AUTOSLICE_CPA_ENV is not owned by the current user: {path}")
    if stat.S_IMODE(info.st_mode) != 0o600:
        raise RuntimeError(f"AUTOSLICE_CPA_ENV must have mode 0600: {path}")
    return path


def build_cpa_qa_command(
    env_path: Path,
    *,
    load_env_file: Callable[[Path], Mapping[str, str]],
    environment: Mapping[str, str],
) -> str:
    """Build the direct semantic-QA command from the bound CPA endpoint."""

    env_file = load_env_file(env_path)
    base = (env_file.get("CPA_BASE_URL") or environment.get("CPA_BASE_URL") or "").rstrip("/")
    if not base:
        raise RuntimeError(f"CPA_BASE_URL missing from environment and {env_path}")
    return (
        "python3 scripts/cpa_semantic_qa_llm.py --request {request_json} --response {response_json} "
        f"--transport direct --model gpt-5.6-luna --fallback-model gpt-5.5 --api-mode responses "
        f"--reasoning-effort medium --max-tokens 16000 --retries 3 --api-base {base} --api-key-env CPA_API_KEY"
    )


def probe_cpa_health(
    env_path: Path,
    *,
    load_env_file: Callable[[Path], Mapping[str, str]],
    environment: Mapping[str, str],
    request_factory: Callable[..., Any] = urllib.request.Request,
    urlopen: Callable[..., Any] = urllib.request.urlopen,
) -> bool:
    """Probe the same CPA failover chain used by the production batch gate."""

    env = load_env_file(env_path)
    base = (env.get("CPA_BASE_URL") or environment.get("CPA_BASE_URL", "")).rstrip("/")
    key = env.get("CPA_API_KEY") or environment.get("CPA_API_KEY", "")
    if not base or not key:
        return False
    for model in ("gpt-5.6-sol", "gpt-5.5", "gpt-5.4"):
        body = json.dumps(
            {
                "model": model,
                "input": "回复:OK",
                "reasoning": {"effort": "low"},
                "max_output_tokens": 2000,
            }
        ).encode()
        req = request_factory(
            f"{base}/responses",
            data=body,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
            },
        )
        try:
            with urlopen(req, timeout=60) as resp:
                if resp.status == 200:
                    return True
        except Exception:  # noqa: BLE001 — any failure means this model is down
            continue
    return False
