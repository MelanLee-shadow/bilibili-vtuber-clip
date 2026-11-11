"""Resolve deployed code without treating a retired mutable repo as current."""

from __future__ import annotations

import os
import inspect
from pathlib import Path
import re

_VERSION_TARGET_RX = re.compile(r"^versions/([0-9a-f]{40})$")
DEFAULT_RUNTIME_BASE = Path("/opt/bilive/autoslice")
_EXECUTING_REPO: Path | None = Path(__file__).resolve().parents[2]
_VERSION_ENV = "AUTOSLICE_RUNTIME_VERSION_ROOT"


class DeployedCodeRootError(ValueError):
    """The published deployment pointer or its version tree is invalid."""


def deployed_code_root(
    base: Path, *, require_current: bool = False, executing_repo: Path | None = None
) -> Path:
    """Use this process's explicit version, otherwise the published pointer.

    ``require_current`` is the administrative pointer check. Ordinary consumers
    keep their CLI/import lease's physical root, including a staged version.
    A generated remote bootstrap has no local executing root to transplant.
    """

    base = Path(base)
    if not require_current:
        own = executing_repo if executing_repo is not None else _EXECUTING_REPO
        executing = own if own is not None and own.parent.parent == base / "versions" else None
        pinned = os.environ.get(_VERSION_ENV)
        selected: Path | None
        if pinned:
            selected = Path(pinned)
            if executing is not None and selected != executing:
                raise DeployedCodeRootError("deployed authority version differs from running code")
        else:
            selected = executing
        if selected is not None:
            if (not selected.is_absolute() or selected.name != "repo"
                    or selected.parent.parent != base / "versions"):
                raise DeployedCodeRootError("deployed authority version is outside runtime")
            for path in (base / "versions", selected.parent, selected):
                if path.is_symlink() or not path.is_dir():
                    raise DeployedCodeRootError("deployed authority version is incomplete")
            identity = selected.parent.name
            marker = selected / "DEPLOYED_COMMIT"
            manifest = selected / "DEPLOYED_MANIFEST.json"
            try:
                commit = marker.read_text(encoding="utf-8").strip()
                overflow = selected / "OVERFLOW_RELEASE_SHA256"
                valid = (
                    re.fullmatch(r"[0-9a-f]{40}", commit) is not None
                    and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", identity) is not None
                    and not marker.is_symlink() and manifest.is_file() and not manifest.is_symlink()
                    and (commit == identity if len(identity) == 40 else
                         not overflow.is_symlink() and overflow.read_text(encoding="utf-8").strip() == identity)
                )
            except (OSError, UnicodeError) as exc:
                raise DeployedCodeRootError("deployed authority version is incomplete") from exc
            if not valid:
                raise DeployedCodeRootError("deployed authority version is incomplete")
            return selected
    current = base / "current"
    try:
        current.lstat()
    except FileNotFoundError:
        if require_current:
            raise DeployedCodeRootError(
                "deployed code root: base/current must be the versioned deployment symlink"
            )
        return base / "repo"
    if not current.is_symlink():
        raise DeployedCodeRootError(
            "deployed code root: base/current must be the versioned deployment symlink"
        )
    target = os.readlink(current)
    match = _VERSION_TARGET_RX.fullmatch(target)
    if match is None:
        raise DeployedCodeRootError("deployed code root: current target is not versions/<commit>")
    version = base / "versions" / match.group(1)
    root = version / "repo"
    for path in (base / "versions", version, root):
        if path.is_symlink() or not path.is_dir():
            raise DeployedCodeRootError(f"deployed code root: {path} is not a real directory")
    marker = root / "DEPLOYED_COMMIT"
    try:
        recorded = marker.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise DeployedCodeRootError("deployed code root: DEPLOYED_COMMIT is unreadable") from exc
    if marker.is_symlink() or recorded != f"{match.group(1)}\n":
        raise DeployedCodeRootError("deployed code root: DEPLOYED_COMMIT does not name its version")
    return root


def deployed_code_root_reference(base: Path = DEFAULT_RUNTIME_BASE) -> Path:
    """Reference current code on another host without probing the local disk.

    This is a symbolic path, not evidence that a remote deployment is valid.
    Use the resolver (or its generated bootstrap) before executing remote code.
    """
    return Path(base) / "current" / "repo"


def deployed_code_root_bootstrap(
    base: Path = DEFAULT_RUNTIME_BASE, *, bind_repo: bool = True
) -> str:
    """Generate a stdlib-only Python program binding ``repo`` on its host.

    Ship the actual resolver implementation so SSH snippets cannot drift into
    another definition or import retired code while trying to locate current.
    With ``bind_repo=False``, emit only definitions so a request-driven program
    can call the same resolver with its runtime root after reading the request.
    """
    return (
        "import os, re\nfrom pathlib import Path\n"
        + f"_VERSION_TARGET_RX = re.compile({_VERSION_TARGET_RX.pattern!r})\n"
        + f"_EXECUTING_REPO = None\n_VERSION_ENV = {_VERSION_ENV!r}\n"
        + inspect.getsource(DeployedCodeRootError)
        + "\n"
        + inspect.getsource(deployed_code_root)
        + (f"\nrepo = deployed_code_root(Path({os.fspath(base)!r}))\n" if bind_repo else "")
    )


def main() -> None:
    """Stdlib-only entry for shell consumers, including remote references."""
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, default=DEFAULT_RUNTIME_BASE)
    parser.add_argument("--reference", action="store_true")
    parser.add_argument("--bootstrap", action="store_true")
    args = parser.parse_args()
    if args.bootstrap:
        print(deployed_code_root_bootstrap(args.base), end="")
    elif args.reference:
        print(deployed_code_root_reference(args.base))
    else:
        print(deployed_code_root(args.base))


if __name__ == "__main__":
    main()
