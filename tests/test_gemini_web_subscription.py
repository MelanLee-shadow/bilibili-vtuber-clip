import contextlib
import json
from pathlib import Path
import sys
import types

import pytest

from scripts import gemini_web_subscription as provider


class _FakeLocator:
    def __init__(self, text="", *, visible=True, enabled=True, on_click=None, on_fill=None, on_files=None):
        self.text = text
        self.visible = visible
        self.enabled = enabled
        self._on_click = on_click
        self._on_fill = on_fill
        self._on_files = on_files

    def count(self):
        return 1

    def nth(self, index):
        if index != 0:
            raise IndexError(index)
        return self

    def is_visible(self):
        return self.visible

    def is_enabled(self):
        return self.enabled

    def inner_text(self):
        return self.text

    def text_content(self):
        return self.text

    def click(self):
        if self._on_click:
            self._on_click()

    def fill(self, value):
        if self._on_fill:
            self._on_fill(value)

    def set_input_files(self, value):
        if self._on_files:
            self._on_files(value)

    def press(self, _key):
        return None


class _FakeFileChooser:
    def __init__(self, page):
        self.page = page

    def set_files(self, value):
        self.page._set_files(value)


class _FakeFileChooserExpectation:
    def __init__(self, page):
        self.value = _FakeFileChooser(page)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _FakePage:
    def __init__(self, *, login=False, model_label=None, response="done", account_menu=False, body_text=None, mode_picker=False, attachment_trigger=False, chooser_only_confirmation=False, progressbar_visible=False):
        self.url = "https://accounts.google.com/signin/v2/identifier" if login else "https://gemini.google.com/app?token=redacted"
        self.model_label = model_label
        self.body_text = "Sign in with Google" if login else (body_text or "")
        self.account_menu = account_menu
        self.mode_picker = mode_picker
        self.model_menu_open = False
        self.attachment_trigger = attachment_trigger
        self.attachment_menu_open = False
        self.attachment_trigger_clicked = False
        self.file_chooser_requested = False
        self.chooser_only_confirmation = chooser_only_confirmation
        self.progressbar_visible = progressbar_visible
        self.send_enabled = False
        self.selected_file = None
        self.latest_response = response
        self.response_complete = True
        self.prompt = None
        self.upload_confirmed = False
        self.screenshots = []
        self.screenshot_full_pages = []

    def goto(self, url, **_kwargs):
        self.url = url

    def get_by_text(self, text, exact=False):
        visible = bool(self.model_label and text == self.model_label and (not self.mode_picker or self.model_menu_open))
        return _FakeLocator(self.model_label or "", visible=visible, on_click=lambda: None)

    def get_by_role(self, role, name=None, **_kwargs):
        if role == "progressbar":
            return _FakeLocator("Uploading", visible=self.progressbar_visible)
        if role == "textbox":
            return _FakeLocator("", on_fill=self._fill_prompt)
        if role == "button":
            if self.mode_picker and _matches(name, "Open mode picker, currently Flash Extended"):
                return _FakeLocator("Flash Extended", on_click=self._open_model_menu)
            if self.attachment_trigger and _matches(name, "Upload & tools"):
                return _FakeLocator("Upload & tools", on_click=self._open_attachment_menu)
            if self.account_menu and _matches(name, "Google Account"):
                return _FakeLocator("Google Account")
            if _matches(name, "Send"):
                return _FakeLocator("Send", enabled=self.send_enabled, on_click=self._submit_prompt)
            if self.model_menu_open and self.model_label and _matches(name, self.model_label):
                return _FakeLocator(self.model_label)
            if self.attachment_menu_open and _matches(name, "Upload files"):
                return _FakeLocator("Upload files", on_click=self._choose_upload)
        if role == "menuitem" and self.attachment_menu_open and _matches(name, "Upload files"):
            return _FakeLocator("Upload files", on_click=self._choose_upload)
        if role == "menuitem" and self.model_menu_open and self.model_label and _matches(name, self.model_label):
            return _FakeLocator(self.model_label, on_click=self._choose_model)
        return _FakeLocator("", visible=False)

    def expect_file_chooser(self, **_kwargs):
        self.file_chooser_requested = True
        return _FakeFileChooserExpectation(self)

    def locator(self, selector):
        if selector == "body":
            return _FakeLocator(self.body_text)
        if selector == 'input[type="file"]':
            return _FakeLocator(on_files=self._set_files)
        return _FakeLocator("", visible=False)

    def screenshot(self, *, path, **_kwargs):
        path = Path(path)
        path.write_bytes(b"fake screenshot")
        self.screenshots.append(path)
        self.screenshot_full_pages.append(_kwargs.get("full_page"))

    def _fill_prompt(self, value):
        self.prompt = value

    def _submit_prompt(self):
        if isinstance(self.latest_response, str) and self.latest_response.strip():
            self.latest_response = [self.latest_response, self.latest_response]
        elif isinstance(self.latest_response, list) and self.latest_response:
            self.latest_response.append(self.latest_response[-1])

    def _set_files(self, value):
        self.selected_file = value
        if self.chooser_only_confirmation:
            self.send_enabled = True
            return
        self.upload_confirmed = True
        self.body_text = f"Attached {Path(value).name}"

    def _open_model_menu(self):
        self.model_menu_open = True

    def _choose_model(self):
        self.model_menu_open = False

    def _open_attachment_menu(self):
        self.attachment_trigger_clicked = True
        self.attachment_menu_open = True

    def _choose_upload(self):
        self.attachment_menu_open = False


class _FakeContext:
    def __init__(self, page):
        self.pages = [page]
        self.closed = False

    def close(self):
        self.closed = True


def _matches(pattern, value):
    if hasattr(pattern, "search"):
        return bool(pattern.search(value))
    return pattern is not None and str(pattern).lower() in value.lower()


def _inputs(tmp_path):
    video = tmp_path / "clip.mp4"
    prompt = tmp_path / "prompt.txt"
    video.write_bytes(b"video")
    prompt.write_text("transcribe this", encoding="utf-8")
    return video, prompt


class _ResponseContainerPage:
    latest_response = None

    def locator(self, selector):
        if selector == "div.markdown.markdown-main-panel.md-content":
            return _FakeLocator('{"control": "TEXT", "result": "OK"}')
        return _FakeLocator("", visible=False)


def test_response_candidates_reads_current_gemini_response_container():
    values, invalid = provider._response_candidates(_ResponseContainerPage())

    assert values == ['{"control": "TEXT", "result": "OK"}']
    assert invalid is False


def test_wait_response_requires_new_node_but_accepts_identical_appended_text():
    page = _FakePage(response=["previous"])
    baseline, invalid = provider._response_candidates(page)
    assert baseline == ["previous"]
    assert invalid is False

    with pytest.raises(provider.ProviderError) as error:
        provider.wait_for_latest_response(
            page,
            previous_candidates=baseline,
            timeout_seconds=0,
            sleeper=lambda _seconds: None,
        )
    assert error.value.status == "RESPONSE_TIMEOUT"

    page.latest_response = ["previous", "previous"]
    assert provider.wait_for_latest_response(
        page,
        previous_candidates=baseline,
        timeout_seconds=1,
        stability_polls=2,
        sleeper=lambda _seconds: None,
    ) == "previous"


def test_receipt_never_promotes_visible_label_to_backend_model():
    receipt = provider.build_receipt(
        mode="run",
        status="SUCCESS",
        started_at="2026-09-02T00:00:00Z",
        finished_at="2026-09-02T00:00:01Z",
        requested_model_label="Gemini 3.8 Flash",
        observed_model_label="Gemini 3.8 Flash",
    )

    assert receipt["observed_model_label"] == "Gemini 3.8 Flash"
    assert receipt["backend_identity"] == {
        "status": "UNVERIFIED",
        "model_id": None,
        "source": "consumer_web_ui",
        "note": "A visible consumer UI label does not prove the exact backend model.",
    }


def test_profile_lock_is_nonblocking(tmp_path):
    profile = provider.ensure_profile_dir(tmp_path / "profile")
    with provider.profile_lock(profile):
        with pytest.raises(provider.ProviderError) as error:
            with provider.profile_lock(profile):
                pass
    assert error.value.status == "PROFILE_BUSY"


def test_run_fails_closed_when_google_login_is_required(tmp_path):
    video, prompt = _inputs(tmp_path)
    page = _FakePage(login=True)
    context = _FakeContext(page)

    receipt = provider.run_subscription(
        video_path=video,
        prompt_path=prompt,
        model_label="Gemini 3.8 Flash",
        profile_dir=tmp_path / "profile",
        receipt_path=tmp_path / "login-required.json",
        response_timeout_seconds=0,
        browser_factory=lambda _profile, _headless: context,
    )

    assert receipt["status"] == "LOGIN_REQUIRED"
    assert receipt["backend_identity"]["status"] == "UNVERIFIED"
    assert receipt["raw_response_sha256"] is None
    assert json.loads((tmp_path / "login-required.json").read_text())["status"] == "LOGIN_REQUIRED"


def test_background_login_probe_polls_until_login_clears(tmp_path, monkeypatch):
    page = _FakePage(login=True)
    context = _FakeContext(page)
    waits = []

    monkeypatch.setattr(provider.sys.stdin, "isatty", lambda: False)

    def no_sleep(seconds):
        waits.append(seconds)
        assert context.closed is False
        page.body_text = "Gemini\nNew chat"
        page.account_menu = True
        page.url = "https://gemini.google.com/app?after=login"

    receipt = provider.run_login_probe(
        profile_dir=tmp_path / "profile",
        receipt_path=tmp_path / "probe.json",
        manual_wait_seconds=10,
        browser_factory=lambda _profile, _headless: context,
        sleeper=no_sleep,
    )

    assert waits
    assert receipt["status"] == "LOGIN_READY"
    assert receipt["login_surface_observed"] is True
    assert receipt["page_url"] == "https://gemini.google.com/app"


def test_login_probe_waits_for_delayed_blank_materialization(tmp_path, monkeypatch):
    page = _FakePage(login=False)
    context = _FakeContext(page)
    waits = []

    monkeypatch.setattr(provider.sys.stdin, "isatty", lambda: False)

    def no_sleep(seconds):
        waits.append(seconds)
        assert context.closed is False
        page.body_text = "Gemini\nNew chat"
        page.account_menu = True

    receipt = provider.run_login_probe(
        profile_dir=tmp_path / "profile",
        receipt_path=tmp_path / "probe.json",
        manual_wait_seconds=10,
        browser_factory=lambda _profile, _headless: context,
        sleeper=no_sleep,
    )

    assert waits
    assert receipt["status"] == "LOGIN_READY"
    assert receipt["login_surface_observed"] is False


def test_login_probe_rejects_transient_account_match_before_accounts_redirect(tmp_path, monkeypatch):
    page = _FakePage(account_menu=True, body_text="Gemini\nNew chat")
    context = _FakeContext(page)
    polls = []
    clock = [0.0]

    monkeypatch.setattr(provider.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(provider.time, "monotonic", lambda: clock.__setitem__(0, clock[0] + 0.25) or clock[0])

    def no_sleep(_seconds):
        polls.append(True)
        assert context.closed is False
        if len(polls) == 1:
            page.url = "https://accounts.google.com/v3/signin/identifier"
            page.body_text = "Sign in with Google"
            page.account_menu = False

    receipt = provider.run_login_probe(
        profile_dir=tmp_path / "profile",
        receipt_path=tmp_path / "probe.json",
        manual_wait_seconds=1,
        browser_factory=lambda _profile, _headless: context,
        sleeper=no_sleep,
    )

    assert len(polls) >= 2
    assert receipt["status"] == "LOGIN_REQUIRED"


def test_run_requires_exact_visible_requested_model_label(tmp_path, monkeypatch):
    video, prompt = _inputs(tmp_path)
    context = _FakeContext(_FakePage(account_menu=True, body_text="Gemini\nNew chat"))
    clock = [0.0]
    monkeypatch.setattr(provider.time, "monotonic", lambda: clock.__setitem__(0, clock[0] + 1.0) or clock[0])

    receipt = provider.run_subscription(
        video_path=video,
        prompt_path=prompt,
        model_label="Gemini 3.8 Flash",
        profile_dir=tmp_path / "profile",
        receipt_path=tmp_path / "model-required.json",
        browser_factory=lambda _profile, _headless: context,
        sleeper=lambda _seconds: None,
    )

    assert receipt["status"] == "MODEL_LABEL_NOT_OBSERVED"
    assert receipt["observed_model_label"] is None


def test_model_picker_button_reveals_exact_requested_label():
    page = _FakePage(model_label=None, mode_picker=False, body_text="Gemini\nNew chat")
    waits = []

    def reveal_mode_picker(seconds):
        waits.append(seconds)
        page.mode_picker = True
        page.model_label = "3.8 Flash"

    assert provider.select_visible_model_label(page, "3.8 Flash", sleeper=reveal_mode_picker) == "3.8 Flash"
    assert waits
    assert page.model_menu_open is False


def test_successful_fake_upload_response_binds_artifacts_and_disables_passwords(tmp_path):
    video, prompt = _inputs(tmp_path)
    page = _FakePage(model_label="Gemini 3.8 Flash", response="hello from Gemini", account_menu=True, body_text="Gemini\nNew chat", attachment_trigger=True, chooser_only_confirmation=True)
    context = _FakeContext(page)

    def browser_factory(profile, headless):
        assert headless is False
        assert profile.stat().st_mode & 0o777 == 0o700
        preferences = json.loads((profile / "Default" / "Preferences").read_text())
        assert preferences["credentials_enable_service"] is False
        assert preferences["profile"]["password_manager_enabled"] is False
        return context

    receipt = provider.run_subscription(
        video_path=video,
        prompt_path=prompt,
        model_label="Gemini 3.8 Flash",
        profile_dir=tmp_path / "profile",
        receipt_path=tmp_path / "success.json",
        browser_factory=browser_factory,
        sleeper=lambda _seconds: None,
    )

    assert receipt["status"] == "SUCCESS"
    assert receipt["video_sha256"] == provider.sha256_file(video)
    assert receipt["prompt_sha256"] == provider.sha256_file(prompt)
    response_path = Path(receipt["raw_response_path"])
    assert response_path.read_text(encoding="utf-8") == "hello from Gemini"
    assert receipt["raw_response_sha256"] == provider.sha256_file(response_path)
    assert receipt["observed_model_label"] == "Gemini 3.8 Flash"
    assert receipt["backend_identity"]["model_id"] is None
    assert receipt["page_url"] == "https://gemini.google.com/app"
    screenshot_path = Path(receipt["screenshots"][0]["path"])
    assert receipt["screenshots"][0]["sha256"] == provider.sha256_file(screenshot_path)
    assert screenshot_path.parent.stat().st_mode & 0o777 == 0o700
    assert screenshot_path.stat().st_mode & 0o777 == 0o600
    assert page.prompt == "transcribe this"
    assert page.attachment_trigger_clicked is True
    assert page.file_chooser_requested is True
    assert page.attachment_menu_open is False
    assert page.selected_file == str(video)
    assert page.upload_confirmed is False
    assert video.name.lower() not in page.body_text.lower()
    assert page.send_enabled is True


def test_upload_waits_for_progressbar_before_send_enabled_confirmation(tmp_path):
    video, _prompt = _inputs(tmp_path)
    page = _FakePage(body_text="Gemini\nNew chat", attachment_trigger=True, chooser_only_confirmation=True, progressbar_visible=True)
    busy_states = []

    def clear_progressbar(_seconds):
        busy_states.append(page.progressbar_visible)
        page.progressbar_visible = False

    assert provider.upload_video(page, video, timeout_seconds=1, sleeper=clear_progressbar) is True
    assert busy_states == [True, False]


def test_response_timeout_receipt_keeps_submitted_and_failure_screenshots(tmp_path):
    video, prompt = _inputs(tmp_path)
    page = _FakePage(model_label="Gemini 3.8 Flash", response="", account_menu=True, body_text="Gemini\nNew chat", attachment_trigger=True, chooser_only_confirmation=True)
    context = _FakeContext(page)

    receipt = provider.run_subscription(
        video_path=video,
        prompt_path=prompt,
        model_label="Gemini 3.8 Flash",
        profile_dir=tmp_path / "profile",
        receipt_path=tmp_path / "timeout.json",
        response_timeout_seconds=0,
        browser_factory=lambda _profile, _headless: context,
        sleeper=lambda _seconds: None,
    )

    assert receipt["status"] == "RESPONSE_TIMEOUT"
    screenshots = receipt["screenshots"]
    assert len(screenshots) >= 2
    names = {Path(item["path"]).stem for item in screenshots}
    assert {"submitted", "response-timeout"} <= names
    assert page.screenshot_full_pages[:2] == [True, True]
    for item in screenshots:
        screenshot_path = Path(item["path"])
        assert item["sha256"] == provider.sha256_file(screenshot_path)
        assert screenshot_path.stat().st_mode & 0o777 == 0o600


def test_direct_cdp_session_reaps_chromium_and_xvfb(tmp_path, monkeypatch):
    profile = tmp_path / "profile"
    browser = tmp_path / "chromium"
    xvfb = tmp_path / "Xvfb"
    profile.mkdir()
    browser.write_bytes(b"chromium")
    xvfb.write_bytes(b"xvfb")
    browser.chmod(0o755)
    xvfb.chmod(0o755)
    processes = []

    class FakeProcess:
        def __init__(self, command):
            self.command = command
            self.alive = True
            self.terminated = False
            self.waited = False

        def poll(self):
            return None if self.alive else 0

        def terminate(self):
            self.terminated = True
            self.alive = False

        def wait(self, timeout=None):
            self.waited = True
            return 0

        def kill(self):
            self.alive = False

    def fake_popen(command, **_kwargs):
        process = FakeProcess(command)
        processes.append(process)
        return process

    monkeypatch.setattr(provider.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(provider, "_wait_cdp_port", lambda *_args, **_kwargs: None)

    page = _FakePage(body_text="Gemini\nNew chat", account_menu=True)
    context = _FakeContext(page)

    class Connected:
        contexts = [context]
        closed = False

        def close(self):
            self.closed = True

    connected = Connected()

    class Chromium:
        def connect_over_cdp(self, _endpoint):
            return connected

    class Playwright:
        chromium = Chromium()

    @contextlib.contextmanager
    def fake_playwright():
        yield Playwright()

    sync_api = types.ModuleType("playwright.sync_api")
    sync_api.sync_playwright = fake_playwright
    playwright = types.ModuleType("playwright")
    playwright.sync_api = sync_api
    monkeypatch.setitem(sys.modules, "playwright", playwright)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", sync_api)

    with provider.browser_session(
        profile,
        headless=False,
        direct_cdp=True,
        browser_executable=browser,
        xvfb_bin=xvfb,
    ) as (_context, actual_page):
        assert actual_page is page

    assert connected.closed is True
    assert len(processes) == 2
    assert all(process.terminated and process.waited for process in processes)
    assert any("--remote-debugging-port=" in arg for arg in processes[1].command)
