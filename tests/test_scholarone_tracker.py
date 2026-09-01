import pytest
from selenium.common.exceptions import WebDriverException

from manuscript_desk.credentials import Credentials
from manuscript_desk.provider_origins import UnsafeNavigationError
from manuscript_desk.scholarone_tracker import ScholarOneTracker
from manuscript_desk.tracking_models import BrowserTabCrashedError, ProviderLoginError


class Field:
    def __init__(self):
        self.values = []

    def clear(self):
        pass

    def send_keys(self, value):
        self.values.append(value)


class LoginButton:
    def __init__(self, driver, redirect=None):
        self.driver = driver
        self.redirect = redirect

    def click(self):
        if self.redirect:
            self.driver.current_url = self.redirect


class FakeDriver:
    def __init__(self, redirect_after_submit=None):
        self.current_url = ""
        self.redirect_after_submit = redirect_after_submit
        self.username = Field()
        self.password = Field()

    def get(self, value):
        self.current_url = value

    def find_element(self, _by, value):
        if value == "PASSWORD":
            return self.password
        if value == "logInButton":
            return LoginButton(self, self.redirect_after_submit)
        raise AssertionError(value)


class ImmediateWait:
    def __init__(self, driver, _timeout):
        self.driver = driver

    def until(self, _condition):
        if not self.driver.username.values:
            return self.driver.username
        return object()


def tracker_with_driver(driver):
    tracker = ScholarOneTracker.__new__(ScholarOneTracker)
    tracker.driver = driver
    tracker.headless = True
    tracker.db_manager = None
    return tracker


def test_login_canonicalizes_legacy_http_and_validates_before_credentials(monkeypatch):
    driver = FakeDriver()
    tracker = tracker_with_driver(driver)
    monkeypatch.setattr("manuscript_desk.scholarone_tracker.WebDriverWait", ImmediateWait)

    assert tracker.login_to_journal("http://MC.manuscriptcentral.com/JOURNAL", "user", "secret") is True
    assert driver.current_url == "https://mc.manuscriptcentral.com/JOURNAL"
    assert driver.username.values == ["user"]
    assert driver.password.values == ["secret"]


def test_login_rejects_unsafe_redirect_before_password_submission(monkeypatch):
    driver = FakeDriver(redirect_after_submit="https://evil.example.test/login")
    tracker = tracker_with_driver(driver)
    monkeypatch.setattr("manuscript_desk.scholarone_tracker.WebDriverWait", ImmediateWait)

    with pytest.raises(UnsafeNavigationError, match="unsafe_provider_redirect"):
        tracker.login_to_journal("https://mc.manuscriptcentral.com/JOURNAL", "user", "secret")


def test_login_does_not_swallow_browser_tab_crash(monkeypatch):
    class CrashedDriver(FakeDriver):
        def get(self, _value):
            raise WebDriverException("session deleted because of page crash")

    tracker = tracker_with_driver(CrashedDriver())
    monkeypatch.setattr("manuscript_desk.scholarone_tracker.WebDriverWait", ImmediateWait)

    with pytest.raises(BrowserTabCrashedError, match="browser_tab_crashed"):
        tracker.login_to_journal("https://mc.manuscriptcentral.com/JOURNAL", "user", "secret")


def test_fetch_uses_typed_safe_login_error(monkeypatch):
    tracker = tracker_with_driver(FakeDriver())
    monkeypatch.setattr(tracker, "login_to_journal", lambda *_args: False)

    with pytest.raises(ProviderLoginError, match="provider_login_failed"):
        tracker.fetch(
            {"journal_url": "https://mc.manuscriptcentral.com/JOURNAL", "journal_name": "Journal"},
            Credentials("user", "secret"),
        )
