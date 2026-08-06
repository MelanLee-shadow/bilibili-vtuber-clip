"""Shared fail-closed selection of profile candidate snapshots for child jobs."""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Mapping, MutableMapping


def bind_runtime_candidate_asset(
    env: MutableMapping[str, str],
    *,
    selectors: Mapping[str, str],
    truth_mode: str,
    env_suffix: str,
    runtime_path: Path,
    committed_path: Path,
    digest_file: Callable[[Path], str],
) -> Path | None:
    """Select blind/configured/runtime/committed bytes and bind their digest."""

    blind = selectors.get(f"AUTOSLICE_BLIND_{env_suffix}")
    configured = selectors.get(f"AUTOSLICE_{env_suffix}")
    if truth_mode == "withheld":
        selected = Path(blind) if blind else None
    elif configured:
        selected = Path(configured)
    elif runtime_path.is_file() and not runtime_path.is_symlink():
        selected = runtime_path
    else:
        selected = committed_path
    value_key = f"LIDOUSHA_{env_suffix}"
    digest_key = f"{value_key}_SHA256"
    disable_key = f"LIDOUSHA_DISABLE_{env_suffix}"
    if selected is not None and selected.is_file() and not selected.is_symlink():
        env[value_key] = str(selected.resolve())
        env[digest_key] = "sha256:" + digest_file(selected)
        env.pop(disable_key, None)
    elif truth_mode == "withheld":
        env[disable_key] = "1"
        env.pop(value_key, None)
        env.pop(digest_key, None)
    return selected
