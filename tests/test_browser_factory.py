from pathlib import Path

import pytest

from manuscript_desk import browser_factory


class FakeDriver:
    def __init__(self):
        self.title = ""
        self.quit_calls = 0

    def set_page_load_timeout(self, _value):
        pass

    def set_script_timeout(self, _value):
        pass

    def implicitly_wait(self, _value):
        pass

    def get(self, _url):
        self.title = "ManuscriptDesk Browser Check"

    def quit(self):
        self.quit_calls += 1


def test_browser_factory_uses_isolated_profile_and_cleans_up(monkeypatch, tmp_path):
    chromium = tmp_path / "chromium"
    chromedriver = tmp_path / "chromedriver"
    for path in (chromium, chromedriver):
        path.write_text("binary")
        path.chmod(0o700)
    monkeypatch.setenv("MANUSCRIPT_DESK_CHROMIUM", str(chromium))
    monkeypatch.setenv("MANUSCRIPT_DESK_CHROMEDRIVER", str(chromedriver))
    monkeypatch.setattr(browser_factory, "_CACHED_DRIVER_PATH", None)
    monkeypatch.setattr(browser_factory, "_CACHED_CHROMIUM_PATH", None)
    captured = {}
    fake = FakeDriver()

    def create_driver(*, service, options):
        captured["service"] = service
        captured["options"] = options
        return fake

    monkeypatch.setattr(browser_factory.webdriver, "Chrome", create_driver)
    driver = browser_factory.create_chrome_driver()
    arguments = captured["options"].arguments
    profile_argument = next(value for value in arguments if value.startswith("--user-data-dir="))
    profile = Path(profile_argument.split("=", 1)[1])
    root = profile.parent
    assert root.exists() and root.stat().st_mode & 0o777 == 0o700
    assert "--headless=new" in arguments
    assert "--remote-debugging-pipe" in arguments
    assert "--disable-dev-shm-usage" not in arguments
    assert captured["options"].binary_location == str(chromium)
    assert captured["service"].env["HOME"].startswith(str(root))
    assert captured["service"].env["XDG_RUNTIME_DIR"].startswith(str(root))

    driver.quit()

    assert fake.quit_calls == 1
    assert not root.exists()


def test_browser_factory_can_opt_out_of_dev_shm(monkeypatch, tmp_path):
    chromium = tmp_path / "chromium"
    chromedriver = tmp_path / "chromedriver"
    for path in (chromium, chromedriver):
        path.write_text("binary")
        path.chmod(0o700)
    monkeypatch.setenv("MANUSCRIPT_DESK_CHROMIUM", str(chromium))
    monkeypatch.setenv("MANUSCRIPT_DESK_CHROMEDRIVER", str(chromedriver))
    monkeypatch.setenv("MANUSCRIPT_DESK_DISABLE_DEV_SHM_USAGE", "true")
    monkeypatch.setattr(browser_factory, "_CACHED_DRIVER_PATH", None)
    monkeypatch.setattr(browser_factory, "_CACHED_CHROMIUM_PATH", None)
    captured = {}
    fake = FakeDriver()

    def create_driver(*, service, options):
        captured["options"] = options
        return fake

    monkeypatch.setattr(browser_factory.webdriver, "Chrome", create_driver)
    driver = browser_factory.create_chrome_driver()
    assert "--disable-dev-shm-usage" in captured["options"].arguments
    driver.quit()


def test_browser_factory_wraps_launch_failure_and_cleans_up(monkeypatch, tmp_path):
    chromium = tmp_path / "chromium"
    chromedriver = tmp_path / "chromedriver"
    for path in (chromium, chromedriver):
        path.write_text("binary")
        path.chmod(0o700)
    monkeypatch.setenv("MANUSCRIPT_DESK_CHROMIUM", str(chromium))
    monkeypatch.setenv("MANUSCRIPT_DESK_CHROMEDRIVER", str(chromedriver))
    monkeypatch.setattr(browser_factory, "_CACHED_DRIVER_PATH", None)
    monkeypatch.setattr(browser_factory, "_CACHED_CHROMIUM_PATH", None)
    roots = []
    original = browser_factory._session_root

    def session_root():
        root = original()
        roots.append(root)
        return root

    monkeypatch.setattr(browser_factory, "_session_root", session_root)
    monkeypatch.setattr(
        browser_factory.webdriver,
        "Chrome",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("sensitive selenium detail")),
    )

    with pytest.raises(browser_factory.BrowserLaunchError, match="browser_start_failed"):
        browser_factory.create_chrome_driver()

    assert roots and not roots[0].exists()


def test_browser_diagnostic_is_credential_free(monkeypatch, capsys):
    fake = FakeDriver()
    monkeypatch.setattr(browser_factory, "_chromium_path", lambda: "/usr/bin/chromium")
    monkeypatch.setattr(browser_factory, "_driver_path", lambda: "/usr/bin/chromedriver")
    monkeypatch.setattr(browser_factory, "create_chrome_driver", lambda headless=True: fake)

    assert browser_factory.diagnose_browser() is True
    output = capsys.readouterr().out
    assert output == "chromium=chromium\nchromedriver=chromedriver\nbrowser_check=ok\n"
    assert fake.quit_calls == 1
