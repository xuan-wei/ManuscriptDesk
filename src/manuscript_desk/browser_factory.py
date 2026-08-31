import logging
import os
import shutil

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service

logger = logging.getLogger(__name__)

_CACHED_DRIVER_PATH: str | None = None


def _find_driver_binary(driver_path: str) -> str:
    if os.path.basename(driver_path) == "chromedriver":
        return driver_path

    driver_dir = os.path.dirname(driver_path)
    candidate = os.path.join(driver_dir, "chromedriver")
    if os.path.exists(candidate):
        return candidate

    search_root = os.path.dirname(driver_dir)
    for root, _, files in os.walk(search_root):
        if "chromedriver" in files:
            return os.path.join(root, "chromedriver")
    return driver_path


def create_chrome_driver(headless: bool = True) -> webdriver.Chrome:
    """Create the shared, scrape-optimized Chrome driver."""

    options = Options()
    options.page_load_strategy = "eager"
    if headless:
        options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--blink-settings=imagesEnabled=false")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-background-networking")
    options.add_argument("--no-first-run")
    options.add_argument("--disable-default-apps")
    options.add_argument(
        "--user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    options.add_experimental_option("prefs", {"profile.managed_default_content_settings.images": 2})

    global _CACHED_DRIVER_PATH
    driver_path = _CACHED_DRIVER_PATH
    if not driver_path or not os.path.exists(driver_path):
        configured_driver = os.environ.get("MANUSCRIPT_DESK_CHROMEDRIVER", "").strip()
        system_driver = shutil.which("chromedriver")
        if configured_driver:
            driver_path = configured_driver
        elif system_driver:
            driver_path = system_driver
        else:
            raise RuntimeError("ChromeDriver is not installed; configure MANUSCRIPT_DESK_CHROMEDRIVER")
        if not os.path.isfile(driver_path) or not os.access(driver_path, os.X_OK):
            raise RuntimeError("ChromeDriver is not executable")
        _CACHED_DRIVER_PATH = driver_path
    else:
        logger.info("复用已缓存的ChromeDriver")

    driver = webdriver.Chrome(service=Service(driver_path), options=options)
    driver.set_page_load_timeout(60)
    driver.set_script_timeout(30)
    driver.implicitly_wait(3)
    return driver
