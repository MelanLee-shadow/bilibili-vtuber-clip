#!/usr/bin/env python3
"""Isolated, receipt-first adapter for the consumer Gemini web UI.

Normal Playwright Chromium is used. Google credentials, cookies, and browser
storage are never read, exported, imported, or printed.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import select
import socket
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Iterator, Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit

SCHEMA_VERSION = "gemini-web-subscription-receipt.v1"
DEFAULT_URL = "https://gemini.google.com/app"
POLL_SECONDS = 0.25
CDP_READY_TIMEOUT_SECONDS = 20.0
EXIT_CODES = {
    "SUCCESS": 0,
    "LOGIN_READY": 0,
    "LOGIN_REQUIRED": 3,
    "MODEL_LABEL_NOT_OBSERVED": 4,
    "UPLOAD_NOT_CONFIRMED": 5,
    "RESPONSE_TIMEOUT": 6,
    "INVALID_RESPONSE": 7,
    "PROFILE_BUSY": 8,
    "INVALID_INPUT": 9,
    "DEPENDENCY_UNAVAILABLE": 10,
}


class ProviderError(RuntimeError):
    def __init__(self, status: str, detail: str = "") -> None:
        super().__init__(detail or status)
        self.status = status


def utc_timestamp() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sanitize_page_url(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = urlsplit(str(value))
        if not parsed.scheme or not parsed.hostname:
            return None
        host = parsed.hostname
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        port = parsed.port
        netloc = host if port is None else f"{host}:{port}"
        return urlunsplit((parsed.scheme.lower(), netloc, parsed.path or "/", "", ""))
    except ValueError:
        return None


def _atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    os.close(fd)
    temporary = Path(name)
    try:
        temporary.write_text(value, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(OSError):
            temporary.unlink()


def _regular_file(path: Path, label: str) -> Path:
    try:
        info = path.lstat()
    except OSError as exc:
        raise ProviderError("INVALID_INPUT", f"{label} unavailable") from exc
    if path.is_symlink() or not path.is_file() or info.st_size <= 0:
        raise ProviderError("INVALID_INPUT", f"{label} must be a non-empty file")
    return path


def ensure_profile_dir(profile_dir: Path) -> Path:
    profile_dir = Path(profile_dir).expanduser()
    try:
        if profile_dir.exists() or profile_dir.is_symlink():
            if profile_dir.is_symlink() or not profile_dir.is_dir():
                raise ProviderError("INVALID_INPUT", "profile must be a real directory")
        else:
            profile_dir.mkdir(parents=True, exist_ok=False)
    except OSError as exc:
        raise ProviderError("INVALID_INPUT", "profile cannot be created") from exc
    if profile_dir.is_symlink() or not profile_dir.is_dir():
        raise ProviderError("INVALID_INPUT", "profile must be a real directory")
    try:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(profile_dir, flags)
        try:
            os.fchmod(fd, 0o700)
        finally:
            os.close(fd)
        if profile_dir.stat().st_mode & 0o777 != 0o700:
            raise ProviderError("INVALID_INPUT", "profile mode must be 0700")
    except ProviderError:
        raise
    except OSError as exc:
        raise ProviderError("INVALID_INPUT", "profile mode cannot be secured") from exc
    return profile_dir


@contextlib.contextmanager
def profile_lock(profile_dir: Path) -> Iterator[None]:
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        with os.fdopen(os.open(Path(profile_dir) / ".gemini-web-subscription.lock", flags, 0o600), "r+") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in (errno.EACCES, errno.EAGAIN):
                    raise ProviderError("PROFILE_BUSY") from exc
                raise ProviderError("INVALID_INPUT", "profile lock cannot be acquired") from exc
            yield
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise ProviderError("INVALID_INPUT", "profile lock path is invalid") from exc
        raise


def _password_prefs_disabled(profile_dir: Path) -> bool:
    path = profile_dir / "Default" / "Preferences"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return False
    return bool(isinstance(value, Mapping) and value.get("credentials_enable_service") is False and isinstance(value.get("profile"), Mapping) and value["profile"].get("password_manager_enabled") is False)


def prepare_profile_preferences(profile_dir: Path) -> None:
    default = profile_dir / "Default"
    if default.exists() or default.is_symlink():
        if default.is_symlink() or not default.is_dir():
            raise ProviderError("INVALID_INPUT", "profile Default path is invalid")
    else:
        default.mkdir()
    path = default / "Preferences"
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file():
            raise ProviderError("INVALID_INPUT", "profile preferences path is invalid")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as exc:
            raise ProviderError("INVALID_INPUT", "profile preferences are invalid") from exc
        if not isinstance(data, dict):
            raise ProviderError("INVALID_INPUT", "profile preferences are invalid")
    else:
        data = {}
    profile = data.setdefault("profile", {})
    if not isinstance(profile, dict):
        raise ProviderError("INVALID_INPUT", "profile preferences are invalid")
    data["credentials_enable_service"], profile["password_manager_enabled"] = False, False
    _atomic_text(path, json.dumps(data, ensure_ascii=False, sort_keys=True) + "\n")
    if not _password_prefs_disabled(profile_dir):
        raise ProviderError("INVALID_INPUT", "password-saving preferences were not applied")


def _context_page(value: Any) -> tuple[Any, Any]:
    if isinstance(value, (tuple, list)) and len(value) == 2:
        return value[0], value[1]
    contexts = getattr(value, "contexts", None)
    if contexts:
        context = contexts[0]
        pages = getattr(context, "pages", None)
        if pages:
            return context, pages[0]
        new_page = getattr(context, "new_page", None)
        return context, new_page() if callable(new_page) else context
    pages = getattr(value, "pages", None)
    if pages:
        return value, pages[0]
    new_page = getattr(value, "new_page", None)
    return value, new_page() if callable(new_page) else value


def _executable(path: Path, label: str) -> Path:
    path = Path(path).expanduser()
    try:
        info = path.lstat()
    except OSError as exc:
        raise ProviderError("INVALID_INPUT", f"{label} unavailable") from exc
    if path.is_symlink() or not path.is_file() or not os.access(path, os.X_OK):
        raise ProviderError("INVALID_INPUT", f"{label} must be a real executable")
    if info.st_size <= 0:
        raise ProviderError("INVALID_INPUT", f"{label} is empty")
    return path


def _display_number() -> str:
    socket_dir = Path("/tmp/.X11-unix")
    for number in range(90, 200):
        socket_path = socket_dir / f"X{number}"
        if not socket_path.exists() and not socket_path.is_symlink():
            return f":{number}"
    raise ProviderError("DEPENDENCY_UNAVAILABLE", "no ephemeral Xvfb display is free")


def _terminate_process(process: Any) -> None:
    """Terminate and reap one child; tolerate an already-exited mock/process."""

    poll = getattr(process, "poll", None)
    try:
        running = poll() is None if callable(poll) else True
    except Exception:
        running = True
    if not running:
        return
    terminate = getattr(process, "terminate", None)
    if callable(terminate):
        with contextlib.suppress(Exception):
            terminate()
    wait = getattr(process, "wait", None)
    if callable(wait):
        try:
            wait(timeout=5)
            return
        except subprocess.TimeoutExpired:
            pass
        except Exception:
            return
    kill = getattr(process, "kill", None)
    if callable(kill):
        with contextlib.suppress(Exception):
            kill()
    if callable(wait):
        with contextlib.suppress(Exception):
            wait(timeout=5)


def _wait_cdp_port(process: Any, port: int, *, timeout_seconds: float) -> None:
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    while True:
        poll = getattr(process, "poll", None)
        if callable(poll) and poll() is not None:
            raise ProviderError("DEPENDENCY_UNAVAILABLE", "Chromium exited before CDP was ready")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.25):
                return
        except OSError:
            if time.monotonic() >= deadline:
                raise ProviderError("DEPENDENCY_UNAVAILABLE", "Chromium CDP endpoint was not ready")
            time.sleep(POLL_SECONDS)


@contextlib.contextmanager
def _direct_cdp_session(
    profile_dir: Path,
    *,
    browser_executable: Path,
    xvfb_bin: Path | None,
    display: str | None,
    cdp_port: int,
) -> Iterator[tuple[Any, Any]]:
    """Launch full Chromium behind a short-lived Xvfb and attach over CDP."""

    browser_executable = _executable(browser_executable, "Chromium")
    if xvfb_bin is not None:
        xvfb_bin = _executable(xvfb_bin, "Xvfb")
    if not 0 <= int(cdp_port) <= 65_535:
        raise ProviderError("INVALID_INPUT", "CDP port is invalid")
    environment = os.environ.copy()
    xvfb = browser = None
    chosen_display = display
    if xvfb_bin is not None:
        if chosen_display is None:
            chosen_display = _display_number()
        if not re.fullmatch(r":[0-9]+", chosen_display):
            raise ProviderError("INVALID_INPUT", "Xvfb display is invalid")
        xvfb = subprocess.Popen(
            [str(xvfb_bin), chosen_display, "-screen", "0", "1280x720x24", "-nolisten", "tcp"],
            env={**environment, "DISPLAY": chosen_display},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        environment["DISPLAY"] = chosen_display
    elif not environment.get("DISPLAY"):
        raise ProviderError("DEPENDENCY_UNAVAILABLE", "direct CDP requires Xvfb or DISPLAY")

    port = int(cdp_port)
    if port == 0:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            port = int(probe.getsockname()[1])
    try:
        browser = subprocess.Popen(
            [
                str(browser_executable),
                f"--user-data-dir={profile_dir}",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-session-crashed-bubble",
                "--remote-debugging-address=127.0.0.1",
                f"--remote-debugging-port={port}",
                "about:blank",
            ],
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _wait_cdp_port(browser, port, timeout_seconds=CDP_READY_TIMEOUT_SECONDS)
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise ProviderError("DEPENDENCY_UNAVAILABLE", "Playwright is not installed") from exc
        with sync_playwright() as playwright:
            try:
                connected = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            except Exception as exc:
                raise ProviderError("DEPENDENCY_UNAVAILABLE", "Playwright could not attach over CDP") from exc
            try:
                yield _context_page(connected)
            finally:
                close = getattr(connected, "close", None)
                if callable(close):
                    with contextlib.suppress(Exception):
                        close()
    finally:
        if browser is not None:
            _terminate_process(browser)
        if xvfb is not None:
            _terminate_process(xvfb)


@contextlib.contextmanager
def browser_session(
    profile_dir: Path,
    *,
    headless: bool,
    browser_factory: Callable[[Path, bool], Any] | None = None,
    direct_cdp: bool = False,
    browser_executable: Path | None = None,
    xvfb_bin: Path | None = None,
    display: str | None = None,
    cdp_port: int = 0,
) -> Iterator[tuple[Any, Any]]:
    prepare_profile_preferences(profile_dir)
    if browser_factory is not None:
        opened = browser_factory(profile_dir, headless)
        if hasattr(opened, "__enter__") and hasattr(opened, "__exit__"):
            with opened as value:
                yield _context_page(value)
            return
        context, page = _context_page(opened)
        try:
            yield context, page
        finally:
            close = getattr(context, "close", None)
            if callable(close):
                close()
        return
    if direct_cdp:
        if browser_executable is None:
            raise ProviderError("INVALID_INPUT", "direct CDP requires Chromium")
        with _direct_cdp_session(
            profile_dir,
            browser_executable=browser_executable,
            xvfb_bin=xvfb_bin,
            display=display,
            cdp_port=cdp_port,
        ) as value:
            yield value
        return
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise ProviderError("DEPENDENCY_UNAVAILABLE", "Playwright is not installed") from exc
    with sync_playwright() as playwright:
        launch_kwargs = {
            "user_data_dir": str(profile_dir),
            "headless": headless,
            "accept_downloads": False,
        }
        if browser_executable is not None:
            launch_kwargs["executable_path"] = str(
                _executable(browser_executable, "Chromium")
            )
        context = playwright.chromium.launch_persistent_context(**launch_kwargs)
        if not _password_prefs_disabled(profile_dir):
            context.close()
            raise ProviderError("INVALID_INPUT", "password-saving preferences changed")
        try:
            yield _context_page(context)
        finally:
            context.close()


def _locators(page: Any, method_name: str, *args: Any, **kwargs: Any) -> list[Any]:
    method = getattr(page, method_name, None)
    if not callable(method):
        return []
    try:
        locator = method(*args, **kwargs)
    except Exception:
        return []
    count, nth = getattr(locator, "count", None), getattr(locator, "nth", None)
    if not callable(count) or not callable(nth):
        return [locator]
    try:
        return [nth(i) for i in range(min(int(count()), 32))]
    except Exception:
        return [locator]


def _visible(locator: Any) -> bool:
    check = getattr(locator, "is_visible", None)
    try:
        return bool(check()) if callable(check) else bool(getattr(locator, "visible", True))
    except Exception:
        return False


def _text(locator: Any) -> str:
    for name in ("inner_text", "text_content"):
        method = getattr(locator, name, None)
        if callable(method):
            with contextlib.suppress(Exception):
                value = method()
                if value:
                    return str(value).strip()
    return ""


def _body(page: Any) -> str:
    direct = getattr(page, "body_text", None)
    if isinstance(direct, str):
        return direct
    for locator in _locators(page, "locator", "body"):
        value = _text(locator)
        if value:
            return value
    return ""


def _click(locator: Any) -> bool:
    click = getattr(locator, "click", None)
    if not callable(click):
        return False
    try:
        click()
        return True
    except Exception:
        return False


def is_google_login_surface(page: Any) -> bool:
    raw = str(getattr(page, "url", "") or "").lower()
    try:
        parsed = urlsplit(raw)
    except ValueError:
        parsed = None
    if parsed and parsed.hostname == "accounts.google.com":
        return True
    body = _body(page).lower()
    return bool(
        re.search(r"(?:sign\s+in|log\s+in|choose\s+an\s+account|enter\s+your\s+(?:email|phone))", body)
        and re.search(r"(?:google|account|gemini|email|phone)", body)
    )


def is_authenticated_gemini_app(page: Any) -> bool:
    """Require positive account UI evidence; a missing login banner is insufficient."""
    try:
        parsed = urlsplit(str(getattr(page, "url", "") or ""))
    except ValueError:
        parsed = None
    if not parsed or parsed.hostname != "gemini.google.com" or is_google_login_surface(page) or not _body(page).strip():
        return False
    account_names = (
        re.compile(r"(?:google\s+)?account(?:\s+menu)?|(?:open|view|manage)\s+account", re.I),
        re.compile(r"(?:google\s*)?(?:account|accounts|profile|帳戶|账户|賬戶|账号|帳號)", re.I),
    )
    for name in account_names:
        for role in ("button", "link", "menuitem", "combobox"):
            if any(_visible(item) for item in _locators(page, "get_by_role", role, name=name)):
                return True
        for method_name in ("get_by_label", "get_by_alt_text"):
            if any(_visible(item) for item in _locators(page, method_name, name)):
                return True
    return False


def _poll(predicate: Callable[[], Any], timeout: float, sleeper: Callable[[float], None]) -> Any:
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        with contextlib.suppress(Exception):
            value = predicate()
            if value:
                return value
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        sleeper(min(POLL_SECONDS, remaining))


def _exact_visible(page: Any, label: str) -> tuple[Any, str] | None:
    expected = " ".join(label.split())
    for role in ("option", "menuitem", "button", "combobox"):
        for locator in _locators(page, "get_by_role", role, name=label):
            actual = _text(locator)
            if _visible(locator) and " ".join(actual.split()) == expected:
                return locator, actual
    for locator in _locators(page, "get_by_text", label, exact=True):
        actual = _text(locator)
        if _visible(locator) and " ".join(actual.split()) == expected:
            return locator, actual
    return None


def select_visible_model_label(page: Any, requested_label: str, *, timeout_seconds: float = 10.0, sleeper: Callable[[float], None] = time.sleep) -> str | None:
    requested_label = requested_label.strip()
    if not requested_label:
        return None
    current, expected = _exact_visible(page, requested_label), " ".join(requested_label.split())
    for role in ("button", "combobox"):
        for locator in _locators(page, "get_by_role", role, name=requested_label):
            if _visible(locator) and " ".join(_text(locator).split()) == expected:
                return _text(locator) or requested_label
    if current is None:
        for trigger_name in (re.compile(r"(?:select|choose|change)\s+model|mode\s+picker|model", re.I), re.compile(r"gemini", re.I)):
            trigger = _poll(
                lambda: next(
                    (locator for role in ("button", "combobox") for locator in _locators(page, "get_by_role", role, name=trigger_name) if _visible(locator)),
                    None,
                ),
                timeout_seconds,
                sleeper,
            )
            if trigger is not None and _click(trigger):
                current = _poll(lambda: _exact_visible(page, requested_label), timeout_seconds, sleeper)
                if current is not None:
                    break
    if current is None or not _click(current[0]):
        return None
    selected_label = _text(current[0]) or current[1]
    return selected_label if " ".join(selected_label.split()) == expected else None


def _visible_upload_busy(page: Any) -> bool:
    if any(_visible(item) for item in _locators(page, "get_by_role", "progressbar")):
        return True
    if any(_visible(item) for item in _locators(page, "locator", '[aria-busy="true"]')):
        return True
    return bool(re.search(r"(?:uploading|upload\s+in\s+progress|processing\s+attachment|cancel\s+(?:send|upload))", _body(page), re.I))


def upload_video(page: Any, video_path: Path, *, timeout_seconds: float = 120.0, sleeper: Callable[[float], None] = time.sleep) -> bool:
    def visible_named(role: str, label: str) -> Any | None:
        return next((item for item in _locators(page, "get_by_role", role, name=label, exact=True) if _visible(item)), None)

    trigger = _poll(lambda: visible_named("button", "Upload & tools"), timeout_seconds, sleeper)
    if trigger is None or not _click(trigger):
        return False

    def upload_item() -> Any | None:
        for role in ("menuitem", "button"):
            item = visible_named(role, "Upload files")
            if item is not None:
                return item
        return next((item for item in _locators(page, "get_by_text", "Upload files", exact=True) if _visible(item)), None)

    item = _poll(upload_item, timeout_seconds, sleeper)
    expect_file_chooser = getattr(page, "expect_file_chooser", None)
    if item is None or not callable(expect_file_chooser):
        return False
    try:
        with expect_file_chooser(timeout=max(1.0, timeout_seconds) * 1000) as chooser_info:
            if not _click(item):
                return False
        chooser = getattr(chooser_info, "value", None)
        set_files = getattr(chooser, "set_files", None)
        if not callable(set_files):
            return False
        set_files(str(video_path))
    except Exception:
        return False
    name, stem = video_path.name.lower(), video_path.stem.lower()
    stable_ready_polls = 0

    def confirmed() -> bool:
        nonlocal stable_ready_polls
        if _visible_upload_busy(page):
            stable_ready_polls = 0
            return False
        text = _body(page).lower()
        send_ready = any(
            _visible(item)
            and callable(getattr(item, "is_enabled", None))
            and bool(item.is_enabled())
            for send_name in (re.compile(r"^(?:send|submit)$", re.I), re.compile(r"send|submit", re.I))
            for item in _locators(page, "get_by_role", "button", name=send_name)
        )
        explicit = bool(getattr(page, "upload_confirmed", False) is True or name in text or (stem and stem in text) or re.search(r"(?:upload|attach|attachment).{0,80}(?:ready|complete|ed)", text))
        if not explicit and not send_ready:
            stable_ready_polls = 0
            return False
        stable_ready_polls += 1
        return stable_ready_polls >= 2

    return bool(_poll(confirmed, timeout_seconds, sleeper))


def submit_prompt(page: Any, prompt: str) -> bool:
    textbox = None
    for name in (re.compile(r"(?:message|prompt|ask)", re.I), None):
        kwargs = {"name": name} if name else {}
        textbox = next((item for item in _locators(page, "get_by_role", "textbox", **kwargs) if _visible(item) and callable(getattr(item, "fill", None))), None)
        if textbox is not None:
            break
    if textbox is None:
        textbox = next((item for selector in ("textarea", '[contenteditable="true"]') for item in _locators(page, "locator", selector) if _visible(item) and callable(getattr(item, "fill", None))), None)
    if textbox is None:
        return False
    try:
        textbox.fill(prompt)
    except Exception:
        return False
    for name in (re.compile(r"^(?:send|submit)$", re.I), re.compile(r"send|submit", re.I)):
        if any(_visible(item) and _click(item) for item in _locators(page, "get_by_role", "button", name=name)):
            return True
    press = getattr(textbox, "press", None)
    if callable(press):
        with contextlib.suppress(Exception):
            press("Enter")
            return True
    return False


def _response_candidates(page: Any) -> tuple[list[str], bool]:
    value = getattr(page, "latest_response", None)
    if value is not None:
        if isinstance(value, str):
            return ([value.strip()] if value.strip() else []), False
        if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
            invalid = any(not isinstance(item, str) for item in value)
            return [item.strip() for item in value if isinstance(item, str) and item.strip()], invalid
        return [], True
    values = []
    for role in ("article", "region"):
        values.extend(_text(item) for item in _locators(page, "get_by_role", role) if _visible(item) and _text(item))
    for selector in ('[data-message-author-role="assistant"]', '[data-message-author-role="model"]', '[data-testid*="response"]', 'div.markdown.markdown-main-panel.md-content'):
        values.extend(_text(item) for item in _locators(page, "locator", selector) if _visible(item) and _text(item))
    if values:
        return [value.strip() for value in values], False
    return [], False


def wait_for_latest_response(page: Any, *, previous_candidates: Sequence[str] = (), timeout_seconds: float = 180.0, stability_polls: int = 3, sleeper: Callable[[float], None] = time.sleep) -> str:
    baseline = tuple(previous_candidates)
    deadline, previous, stable, invalid = time.monotonic() + max(0.0, timeout_seconds), None, 0, False
    while True:
        values, bad = _response_candidates(page)
        invalid |= bad
        if tuple(values) == baseline and len(values) <= len(baseline):
            previous, stable = None, 0
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProviderError("INVALID_RESPONSE" if invalid else "RESPONSE_TIMEOUT")
            sleeper(min(POLL_SECONDS, remaining))
            continue
        current = values[-1] if values else None
        if current and current == previous:
            stable += 1
        elif current:
            previous, stable = current, 1
        complete = getattr(page, "response_complete", None)
        complete = bool(complete) if complete is not None else not bool(re.search(r"stop\s+generating|cancel\s+generation|generating[.…]", _body(page).lower()))
        if current and stable >= max(2, stability_polls) and complete:
            return current
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ProviderError("INVALID_RESPONSE" if invalid else "RESPONSE_TIMEOUT")
        sleeper(min(POLL_SECONDS, remaining))


def capture_screenshot(page: Any, directory: Path, label: str) -> dict[str, str]:
    if directory.exists() or directory.is_symlink():
        if directory.is_symlink() or not directory.is_dir():
            raise ProviderError("INVALID_RESPONSE", "screenshot directory is invalid")
    else:
        directory.mkdir(parents=True)
    try:
        directory.chmod(0o700)
    except OSError as exc:
        raise ProviderError("INVALID_RESPONSE", "screenshot directory permissions failed") from exc
    path = directory / f"{re.sub(r'[^A-Za-z0-9_.-]+', '_', label).strip('._') or 'page'}.png"
    screenshot = getattr(page, "screenshot", None)
    if not callable(screenshot):
        raise ProviderError("INVALID_RESPONSE", "screenshot unavailable")
    try:
        screenshot(path=str(path), full_page=True)
    except TypeError:
        screenshot(path=str(path))
    except Exception as exc:
        raise ProviderError("INVALID_RESPONSE", "screenshot failed") from exc
    if not path.is_file() or path.is_symlink():
        raise ProviderError("INVALID_RESPONSE", "screenshot missing")
    try:
        path.chmod(0o600)
    except OSError as exc:
        raise ProviderError("INVALID_RESPONSE", "screenshot permissions failed") from exc
    return {"path": str(path.resolve()), "sha256": sha256_file(path)}


def build_receipt(*, mode: str, status: str, started_at: str, finished_at: str, requested_model_label: str | None = None, observed_model_label: str | None = None, video_sha256: str | None = None, prompt_sha256: str | None = None, raw_response_sha256: str | None = None, raw_response_path: str | None = None, page_url: str | None = None, screenshots: Sequence[Mapping[str, str]] = (), error_type: str | None = None, login_surface_observed: bool | None = None) -> dict[str, Any]:
    receipt: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "provider": "gemini_web_subscription",
        "mode": mode,
        "status": status,
        "started_at": started_at,
        "finished_at": finished_at,
        "requested_model_label": requested_model_label,
        "observed_model_label": observed_model_label,
        "video_sha256": video_sha256,
        "prompt_sha256": prompt_sha256,
        "raw_response_sha256": raw_response_sha256,
        "raw_response_path": raw_response_path,
        "page_url": sanitize_page_url(page_url),
        "screenshots": [dict(item) for item in screenshots],
        "backend_model_status": "UNVERIFIED",
        "backend_model_id": None,
        "backend_identity": {"status": "UNVERIFIED", "model_id": None, "source": "consumer_web_ui", "note": "A visible consumer UI label does not prove the exact backend model."},
    }
    if error_type:
        receipt["error_type"] = error_type
    if login_surface_observed is not None:
        receipt["login_surface_observed"] = login_surface_observed
    return receipt


def _write_receipt(path: Path, receipt: Mapping[str, Any]) -> dict[str, Any]:
    _atomic_text(path, json.dumps(dict(receipt), ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return dict(receipt)


def _fail(path: Path, *, mode: str, status: str, started: str, requested: str | None = None, observed: str | None = None, video: str | None = None, prompt: str | None = None, page_url: str | None = None, screenshots: Sequence[Mapping[str, str]] = (), error_type: str | None = None, login: bool | None = None) -> dict[str, Any]:
    return _write_receipt(path, build_receipt(mode=mode, status=status, started_at=started, finished_at=utc_timestamp(), requested_model_label=requested, observed_model_label=observed, video_sha256=video, prompt_sha256=prompt, page_url=page_url, screenshots=screenshots, error_type=error_type, login_surface_observed=login))


def _navigate(page: Any, url: str) -> None:
    goto = getattr(page, "goto", None)
    if not callable(goto):
        raise ProviderError("INVALID_RESPONSE", "page cannot navigate")
    try:
        goto(url, wait_until="domcontentloaded")
    except TypeError:
        goto(url)
    except Exception as exc:
        raise ProviderError("INVALID_RESPONSE", "navigation failed") from exc


def run_subscription(*, video_path: Path, prompt_path: Path, model_label: str, profile_dir: Path, receipt_path: Path, url: str = DEFAULT_URL, response_out: Path | None = None, screenshot_dir: Path | None = None, headless: bool = False, direct_cdp: bool = False, browser_executable: Path | None = None, xvfb_bin: Path | None = None, display: str | None = None, cdp_port: int = 0, upload_timeout_seconds: float = 120.0, response_timeout_seconds: float = 180.0, stability_polls: int = 3, browser_factory: Callable[[Path, bool], Any] | None = None, sleeper: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    started, receipt_path = utc_timestamp(), Path(receipt_path).expanduser()
    response_out = response_out or receipt_path.with_suffix(".response.txt")
    screenshot_dir = screenshot_dir or receipt_path.parent / f"{receipt_path.stem}.screenshots"
    video_sha = prompt_sha = None
    try:
        video = _regular_file(Path(video_path).expanduser(), "video")
        prompt_file = _regular_file(Path(prompt_path).expanduser(), "prompt")
        prompt_text = prompt_file.read_text(encoding="utf-8")
        if not prompt_text.strip():
            raise ProviderError("INVALID_INPUT", "prompt is empty")
        video_sha, prompt_sha, profile = sha256_file(video), sha256_file(prompt_file), ensure_profile_dir(Path(profile_dir))
    except ProviderError as exc:
        return _fail(receipt_path, mode="run", status=exc.status, started=started, requested=model_label, video=video_sha, prompt=prompt_sha, error_type=type(exc).__name__)
    except UnicodeError as exc:
        return _fail(receipt_path, mode="run", status="INVALID_INPUT", started=started, requested=model_label, video=video_sha, prompt=prompt_sha, error_type=type(exc).__name__)
    page_url = observed = None
    screenshots: list[dict[str, str]] = []

    def fail(status: str, error_type: str | None = None, login: bool | None = None) -> dict[str, Any]:
        return _fail(receipt_path, mode="run", status=status, started=started, requested=model_label, observed=observed, video=video_sha, prompt=prompt_sha, page_url=page_url, screenshots=screenshots, error_type=error_type, login=login)

    try:
        with profile_lock(profile):
            with browser_session(profile, headless=headless, browser_factory=browser_factory, direct_cdp=direct_cdp, browser_executable=browser_executable, xvfb_bin=xvfb_bin, display=display, cdp_port=cdp_port) as (_context, page):
                _navigate(page, url)
                page_url = sanitize_page_url(str(getattr(page, "url", "") or ""))
                _poll(
                    lambda: is_google_login_surface(page) or is_authenticated_gemini_app(page),
                    min(30.0, max(0.0, upload_timeout_seconds)),
                    sleeper,
                )
                page_url = sanitize_page_url(str(getattr(page, "url", "") or ""))
                if is_google_login_surface(page) or not is_authenticated_gemini_app(page):
                    with contextlib.suppress(ProviderError):
                        screenshots.append(capture_screenshot(page, Path(screenshot_dir), "login-required"))
                    return fail("LOGIN_REQUIRED", login=is_google_login_surface(page))
                observed = select_visible_model_label(page, model_label, sleeper=sleeper)
                if observed is None:
                    with contextlib.suppress(ProviderError):
                        screenshots.append(capture_screenshot(page, Path(screenshot_dir), "model-label"))
                    return fail("MODEL_LABEL_NOT_OBSERVED")
                if not upload_video(page, video, timeout_seconds=upload_timeout_seconds, sleeper=sleeper):
                    with contextlib.suppress(ProviderError):
                        screenshots.append(capture_screenshot(page, Path(screenshot_dir), "upload"))
                    return fail("UPLOAD_NOT_CONFIRMED")
                previous_candidates, _ = _response_candidates(page)
                if not submit_prompt(page, prompt_text):
                    raise ProviderError("INVALID_RESPONSE", "prompt submission failed")
                with contextlib.suppress(Exception):
                    screenshots.append(capture_screenshot(page, Path(screenshot_dir), "submitted"))
                try:
                    raw = wait_for_latest_response(page, previous_candidates=previous_candidates, timeout_seconds=response_timeout_seconds, stability_polls=stability_polls, sleeper=sleeper)
                except ProviderError as exc:
                    with contextlib.suppress(Exception):
                        screenshots.append(capture_screenshot(page, Path(screenshot_dir), exc.status.lower().replace("_", "-")))
                    raise
                response_path = Path(response_out).expanduser()
                _atomic_text(response_path, raw)
                screenshots.append(capture_screenshot(page, Path(screenshot_dir), "response"))
                return _write_receipt(receipt_path, build_receipt(mode="run", status="SUCCESS", started_at=started, finished_at=utc_timestamp(), requested_model_label=model_label, observed_model_label=observed, video_sha256=video_sha, prompt_sha256=prompt_sha, raw_response_sha256=sha256_file(response_path), raw_response_path=str(response_path.resolve()), page_url=page_url, screenshots=screenshots))
    except ProviderError as exc:
        return fail(exc.status if exc.status in EXIT_CODES else "INVALID_RESPONSE", type(exc).__name__)
    except Exception as exc:
        return fail("INVALID_RESPONSE", type(exc).__name__)


def _wait_for_manual_action(page: Any, seconds: float, sleeper: Callable[[float], None] = time.sleep) -> None:
    interactive = bool(getattr(sys.stdin, "isatty", lambda: False)())
    if seconds <= 0:
        return
    if not interactive:
        stable = 0

        def ready_after_stability() -> bool:
            nonlocal stable
            if is_authenticated_gemini_app(page):
                stable += 1
                return stable >= 8
            stable = 0
            return False

        _poll(ready_after_stability, seconds, sleeper)
        return
    print("Complete Google sign-in in the headed browser, then press Enter.", flush=True)
    with contextlib.suppress(OSError):
        ready, _, _ = select.select([sys.stdin], [], [], seconds)
        if ready:
            sys.stdin.readline()


def run_login_probe(*, profile_dir: Path, receipt_path: Path, url: str = DEFAULT_URL, manual_wait_seconds: float = 900.0, screenshot_dir: Path | None = None, direct_cdp: bool = False, browser_executable: Path | None = None, xvfb_bin: Path | None = None, display: str | None = None, cdp_port: int = 0, browser_factory: Callable[[Path, bool], Any] | None = None, sleeper: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    started, receipt_path = utc_timestamp(), Path(receipt_path).expanduser()
    screenshot_dir = screenshot_dir or receipt_path.parent / f"{receipt_path.stem}.screenshots"
    try:
        profile = ensure_profile_dir(Path(profile_dir))
    except ProviderError as exc:
        return _fail(receipt_path, mode="login", status=exc.status, started=started, error_type=type(exc).__name__)
    page_url = None
    screenshots: list[dict[str, str]] = []
    try:
        with profile_lock(profile):
            with browser_session(profile, headless=False, browser_factory=browser_factory, direct_cdp=direct_cdp, browser_executable=browser_executable, xvfb_bin=xvfb_bin, display=display, cdp_port=cdp_port) as (_context, page):
                _navigate(page, url)
                initial_login = is_google_login_surface(page)
                with contextlib.suppress(ProviderError):
                    screenshots.append(capture_screenshot(page, Path(screenshot_dir), "login-probe"))
                _wait_for_manual_action(page, manual_wait_seconds, sleeper)
                final_login = is_google_login_surface(page)
                page_url = sanitize_page_url(str(getattr(page, "url", "") or ""))
                if not is_authenticated_gemini_app(page):
                    status = "LOGIN_REQUIRED"
                else:
                    status = "LOGIN_READY"
                with contextlib.suppress(ProviderError):
                    screenshots.append(capture_screenshot(page, Path(screenshot_dir), "login-probe-final"))
                return _write_receipt(receipt_path, build_receipt(mode="login", status=status, started_at=started, finished_at=utc_timestamp(), page_url=page_url, screenshots=screenshots, login_surface_observed=initial_login or final_login))
    except ProviderError as exc:
        return _fail(receipt_path, mode="login", status=exc.status if exc.status in EXIT_CODES else "INVALID_RESPONSE", started=started, page_url=page_url, screenshots=screenshots, error_type=type(exc).__name__)
    except Exception as exc:
        return _fail(receipt_path, mode="login", status="INVALID_RESPONSE", started=started, page_url=page_url, screenshots=screenshots, error_type=type(exc).__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    login = commands.add_parser("login", aliases=("manual-login", "probe"), help="headed manual login/probe")
    login.add_argument("--profile-dir", required=True, type=Path)
    login.add_argument("--receipt", required=True, type=Path)
    login.add_argument("--url", default=DEFAULT_URL)
    login.add_argument("--manual-wait-seconds", type=float, default=900.0)
    login.add_argument("--screenshot-dir", type=Path)
    login.add_argument("--direct-cdp", action="store_true")
    login.add_argument("--browser-executable", type=Path)
    login.add_argument("--xvfb-bin", type=Path)
    login.add_argument("--display")
    login.add_argument("--cdp-port", type=int, default=0)
    run = commands.add_parser("run", help="upload one video and submit one prompt")
    run.add_argument("video", type=Path)
    run.add_argument("--prompt-file", required=True, type=Path)
    run.add_argument("--model-label", required=True)
    run.add_argument("--profile-dir", required=True, type=Path)
    run.add_argument("--receipt", required=True, type=Path)
    run.add_argument("--response-out", type=Path)
    run.add_argument("--screenshot-dir", type=Path)
    run.add_argument("--url", default=DEFAULT_URL)
    run.add_argument("--headless", action="store_true")
    run.add_argument("--direct-cdp", action="store_true")
    run.add_argument("--browser-executable", type=Path)
    run.add_argument("--xvfb-bin", type=Path)
    run.add_argument("--display")
    run.add_argument("--cdp-port", type=int, default=0)
    run.add_argument("--upload-timeout-seconds", type=float, default=120.0)
    run.add_argument("--response-timeout-seconds", type=float, default=180.0)
    run.add_argument("--stability-polls", type=int, default=3)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command in {"login", "manual-login", "probe"}:
        receipt = run_login_probe(profile_dir=args.profile_dir, receipt_path=args.receipt, url=args.url, manual_wait_seconds=args.manual_wait_seconds, screenshot_dir=args.screenshot_dir, direct_cdp=args.direct_cdp, browser_executable=args.browser_executable, xvfb_bin=args.xvfb_bin, display=args.display, cdp_port=args.cdp_port)
    else:
        receipt = run_subscription(video_path=args.video, prompt_path=args.prompt_file, model_label=args.model_label, profile_dir=args.profile_dir, receipt_path=args.receipt, url=args.url, response_out=args.response_out, screenshot_dir=args.screenshot_dir, headless=args.headless, direct_cdp=args.direct_cdp, browser_executable=args.browser_executable, xvfb_bin=args.xvfb_bin, display=args.display, cdp_port=args.cdp_port, upload_timeout_seconds=args.upload_timeout_seconds, response_timeout_seconds=args.response_timeout_seconds, stability_polls=args.stability_polls)
    status = str(receipt.get("status") or "INVALID_RESPONSE")
    print(json.dumps({"status": status, "receipt": str(Path(args.receipt).expanduser())}, ensure_ascii=False))
    return EXIT_CODES.get(status, 2)


if __name__ == "__main__":
    raise SystemExit(main())
