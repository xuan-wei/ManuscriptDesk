import logging
import os
import shutil
import stat
import tempfile
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service

logger = logging.getLogger(__name__)

_CACHED_DRIVER_PATH: str | None = None
_CACHED_CHROMIUM_PATH: str | None = None


class BrowserLaunchError(RuntimeError):
    error_code = "browser_start_failed"

    def __init__(self):
        super().__init__(self.error_code)


def _executable_path(config_name: str, candidates: tuple[str, ...]) -> str:
    configured = os.environ.get(config_name, "").strip()
    path = configured or next((value for name in candidates if (value := shutil.which(name))), "")
    if not path or not os.path.isfile(path) or not os.access(path, os.X_OK):
        raise BrowserLaunchError()
    return path


def _driver_path() -> str:
    global _CACHED_DRIVER_PATH
    if not _CACHED_DRIVER_PATH or not os.path.exists(_CACHED_DRIVER_PATH):
        _CACHED_DRIVER_PATH = _executable_path("MANUSCRIPT_DESK_CHROMEDRIVER", ("chromedriver",))
    return _CACHED_DRIVER_PATH


def _chromium_path() -> str:
    global _CACHED_CHROMIUM_PATH
    if not _CACHED_CHROMIUM_PATH or not os.path.exists(_CACHED_CHROMIUM_PATH):
        _CACHED_CHROMIUM_PATH = _executable_path(
            "MANUSCRIPT_DESK_CHROMIUM", ("chromium", "chromium-browser", "google-chrome")
        )
    return _CACHED_CHROMIUM_PATH


def _session_root() -> Path:
    root = Path(tempfile.mkdtemp(prefix="manuscript-desk-chromium-"))
    root.chmod(stat.S_IRWXU)
    for name in ("profile", "cache", "config", "runtime", "home"):
        path = root / name
        path.mkdir(mode=0o700)
    return root


def _cleanup_session(root: Path) -> None:
    shutil.rmtree(root, ignore_errors=True)


def create_chrome_driver(headless: bool = True) -> webdriver.Chrome:
    """Create an isolated Chrome driver suitable for a read-only container."""

    root = _session_root()
    options = Options()
    options.page_load_strategy = "eager"
    options.binary_location = _chromium_path()
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    if os.environ.get("MANUSCRIPT_DESK_DISABLE_DEV_SHM_USAGE", "").strip().casefold() in {"1", "true", "yes"}:
        options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--blink-settings=imagesEnabled=false")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-background-networking")
    options.add_argument("--no-first-run")
    options.add_argument("--disable-default-apps")
    options.add_argument("--remote-debugging-pipe")
    options.add_argument(f"--user-data-dir={root / 'profile'}")
    options.add_argument(f"--disk-cache-dir={root / 'cache'}")
    options.add_argument(
        "--user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    options.add_experimental_option("prefs", {"profile.managed_default_content_settings.images": 2})

    service = Service(_driver_path())
    service.env = {
        **os.environ,
        "HOME": str(root / "home"),
        "XDG_CONFIG_HOME": str(root / "config"),
        "XDG_CACHE_HOME": str(root / "cache"),
        "XDG_RUNTIME_DIR": str(root / "runtime"),
    }
    try:
        driver = webdriver.Chrome(service=service, options=options)
    except Exception as exc:
        logger.error("Chromium launch failed: %s", type(exc).__name__)
        _cleanup_session(root)
        raise BrowserLaunchError() from exc

    original_quit = driver.quit

    def quit_and_cleanup() -> None:
        try:
            original_quit()
        finally:
            _cleanup_session(root)

    driver.quit = quit_and_cleanup
    driver.set_page_load_timeout(60)
    driver.set_script_timeout(30)
    driver.implicitly_wait(3)
    return driver


def diagnose_browser() -> bool:
    driver = None
    try:
        print(f"chromium={Path(_chromium_path()).name}")
        print(f"chromedriver={Path(_driver_path()).name}")
        driver = create_chrome_driver(headless=True)
        driver.get("data:text/html,<title>ManuscriptDesk Browser Check</title>")
        if driver.title != "ManuscriptDesk Browser Check":
            raise BrowserLaunchError()
        print("browser_check=ok")
        return True
    except BrowserLaunchError:
        print("browser_check=browser_start_failed")
        return False
    finally:
        if driver is not None:
            driver.quit()


if __name__ == "__main__":
    raise SystemExit(0 if diagnose_browser() else 1)
