import logging
import re

from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait

from .browser_factory import create_chrome_driver
from .provider_origins import SafeProviderNavigation
from .tracking_models import (
    BrowserTimeoutError,
    ManuscriptSnapshot,
    ProviderContractError,
    ProviderUnavailableError,
    SafeTrackingError,
    TrackingDiagnosticCode,
    raise_if_browser_tab_crashed,
)

logger = logging.getLogger(__name__)


class EditorialManagerTracker:
    """Read-only tracker for Editorial Manager author dashboards."""

    FOLDERS = (
        ("submissions_being_processed", "Submissions Being Processed", "auth_pendSubmissions.asp", 1),
        ("submissions_needing_revision", "Submissions Needing Revision", "auth_revSubmissions.asp", 2),
        ("revisions_being_processed", "Revisions Being Processed", "auth_RevisionsBeingProcessed.asp", 2),
        ("submissions_with_decision", "Submissions with a Decision", "auth_compSubmissions.asp", 3),
    )

    HEADER_ALIASES = {
        "manuscript_id": {"manuscript number"},
        "title": {"title"},
        "submitted_date": {"initial date submitted", "date submission began"},
        "status_date": {"status date"},
        "current_status": {"current status"},
        "authorship": {"authorship"},
        "decision_date": {"date final disposition set"},
        "final_disposition": {"final disposition"},
    }

    def __init__(self, headless: bool = True):
        self.driver = create_chrome_driver(headless=headless)
        self.wait = WebDriverWait(self.driver, 20)
        self.author_menu_url = ""

    @staticmethod
    def normalize_text(value: str) -> str:
        return re.sub(r"\s+", " ", value or "").strip()

    @staticmethod
    def extract_identity(manuscript_id: str):
        match = re.match(r"^(.+?)R(\d+)$", manuscript_id or "", re.IGNORECASE)
        if match:
            return match.group(1), int(match.group(2)) + 1
        return manuscript_id, 1

    @staticmethod
    def normalize_status(current_status: str, final_disposition: str):
        current = EditorialManagerTracker.normalize_text(current_status)
        disposition = EditorialManagerTracker.normalize_text(final_disposition)
        current_lower = current.lower()
        disposition_lower = disposition.lower()

        if "accept" in disposition_lower or "completed - accept" in current_lower:
            return "Accept", current or "Completed - Accept"
        if "reject and resubmit" in disposition_lower or "reject and resubmit" in current_lower:
            return "Reject and Resubmit", current or disposition
        if "reject" in disposition_lower or "completed - reject" in current_lower:
            return "Reject", current or "Completed - Reject"
        if "minor revision" in current_lower or "major revision" in current_lower:
            return "Revision", current
        if "revision requested" in current_lower or "revise" in current_lower:
            return "Revision", current
        return "Under Review", current or "Unknown"

    @staticmethod
    def map_author_role(authorship: str) -> str:
        value = EditorialManagerTracker.normalize_text(authorship).lower()
        if value == "corresponding author":
            return "primary"
        if value == "other author":
            return "coauthor"
        return "unknown"

    def login(self, journal_url: str, username: str, password: str) -> None:
        stage = "open_landing"
        try:
            navigation = SafeProviderNavigation(self.driver, "editorial_manager")
            navigation.open(journal_url)
            stage = "find_login_link"
            login_link = self.wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "a#Login")))
            href = login_link.get_attribute("href") or ""
            stage = "open_login_form"
            if href and not href.lower().startswith("javascript:"):
                navigation.open_link(href)
            else:
                self.driver.execute_script("arguments[0].click();", login_link)
                navigation.validate()

            self.driver.switch_to.default_content()
            stage = "switch_login_frame"
            frame = self.wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "iframe#content")))
            self.driver.switch_to.frame(frame)

            stage = "find_traditional_login"
            details = self.wait.until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "details#traditionalLoginCollapsible"))
            )
            if not details.get_attribute("open"):
                self.driver.execute_script("arguments[0].open=true", details)

            stage = "fill_login_form"
            username_field = self.wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "#username")))
            password_field = self.wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "#passwordTextbox")))
            navigation.before_credentials()
            username_field.clear()
            username_field.send_keys(username)
            password_field.clear()
            password_field.send_keys(password)
            stage = "submit_author_login"
            self.driver.find_element(By.CSS_SELECTOR, "input[name='authorLogin']").click()

            stage = "wait_post_login_wrapper"
            self.driver.switch_to.default_content()
            self.wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "a#logoutLink")))
            stage = "switch_author_frame"
            frame = self.wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "iframe#content")))
            self.driver.switch_to.frame(frame)
            stage = "confirm_author_menu"
            self.wait.until(lambda driver: "Author Main Menu" in driver.page_source)
            self.author_menu_url = self.driver.execute_script("return window.location.href")
        except SafeTrackingError:
            # Browser crash and unsafe-redirect failures are already typed and
            # must keep their category; never downgrade them here.
            raise
        except Exception as exc:
            raise_if_browser_tab_crashed(exc)
            # Only stable, non-sensitive categories cross this boundary; the
            # dynamic stage and Selenium exception type stay in local logs.
            logger.warning(
                "editorial manager login not confirmed: stage=%s type=%s",
                stage,
                type(exc).__name__,
            )
            if isinstance(exc, TimeoutException):
                raise BrowserTimeoutError(TrackingDiagnosticCode.PROVIDER_LOGIN_RESPONSE_TIMEOUT) from exc
            raise ProviderUnavailableError(TrackingDiagnosticCode.PROVIDER_LOGIN_PAGE_UNAVAILABLE) from exc

    def _return_to_author_menu(self) -> None:
        # Keep navigation inside the authenticated content frame. Loading the
        # protected path directly can return the wrapper instead of menu HTML.
        try:
            if "Author Main Menu" in self.driver.page_source:
                return
        except Exception:
            pass

        self.driver.switch_to.default_content()
        frame = self.wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "iframe#content")))
        self.driver.switch_to.frame(frame)
        if "Author Main Menu" in self.driver.page_source:
            return
        links = self.driver.find_elements(By.CSS_SELECTOR, "a[href*='AuthorMainMenu.aspx'], a#MainMenu")
        if not links:
            raise ProviderContractError(TrackingDiagnosticCode.PROVIDER_AUTHOR_MENU_CHANGED)
        self.driver.execute_script("arguments[0].click();", links[0])
        self.wait.until(lambda driver: "Author Main Menu" in driver.page_source)

    def _find_folder_link(self, label: str, href_fragment: str):
        anchors = self.driver.find_elements(
            By.XPATH,
            f"//a[contains(@href, '{href_fragment}') or starts-with(normalize-space(), '{label}')]",
        )
        if anchors:
            return anchors[0]

        matching_text = self.driver.find_elements(
            By.XPATH,
            f"//*[self::span or self::li or self::div][starts-with(normalize-space(), '{label}')]",
        )
        if matching_text:
            return None
        raise ProviderContractError(TrackingDiagnosticCode.PROVIDER_FOLDER_STRUCTURE_CHANGED)

    def _set_page_size(self) -> None:
        for selector in ("select#size1", "select#size2"):
            selects = self.driver.find_elements(By.CSS_SELECTOR, selector)
            if not selects:
                continue
            select = Select(selects[0])
            if any(option.get_attribute("value") == "500" or option.text.strip() == "500" for option in select.options):
                table = self.driver.find_elements(By.CSS_SELECTOR, "table#datatable")
                old_table = table[0] if table else None
                select.select_by_visible_text("500")
                if old_table:
                    try:
                        WebDriverWait(self.driver, 10).until(EC.staleness_of(old_table))
                    except TimeoutException:
                        pass
                return

    def _header_map(self, table) -> dict[str, int]:
        raw_headers = [self.normalize_text(th.text).lower() for th in table.find_elements(By.CSS_SELECTOR, "th")]
        mapping = {}
        for index, header in enumerate(raw_headers):
            for canonical, aliases in self.HEADER_ALIASES.items():
                if header in aliases and canonical not in mapping:
                    mapping[canonical] = index
        return mapping

    @classmethod
    def parse_row_values(
        cls, headers: dict[str, int], cells: list[str], folder: str, journal: str, config_id: int | None, site_key: str
    ) -> ManuscriptSnapshot:
        def value(field: str) -> str:
            index = headers.get(field)
            return cls.normalize_text(cells[index]) if index is not None and index < len(cells) else ""

        manuscript_id = value("manuscript_id")
        title = value("title")
        current_status = value("current_status")
        final_disposition = value("final_disposition")
        if not manuscript_id or not title or (not current_status and not final_disposition):
            raise ProviderContractError(TrackingDiagnosticCode.PROVIDER_ROW_DATA_INCOMPLETE)

        source_original_id, version_number = cls.extract_identity(manuscript_id)
        category, detail = cls.normalize_status(current_status, final_disposition)
        authorship = value("authorship")
        operational_id = f"editorial_manager:{site_key}:{source_original_id}"

        return ManuscriptSnapshot(
            provider="editorial_manager",
            journal_config_id=config_id,
            journal=journal,
            manuscript_id=manuscript_id,
            original_id=operational_id,
            source_original_id=source_original_id,
            version_number=version_number,
            title=title,
            author_role=cls.map_author_role(authorship),
            authorship=authorship,
            status_text=current_status,
            status=category,
            detailed_status=detail,
            submitted_date=value("submitted_date"),
            decision_date=value("decision_date"),
            status_date=value("status_date"),
            final_disposition=final_disposition,
            workflow_folder=folder,
        )

    def _parse_current_folder(
        self, folder_key: str, journal: str, config_id: int | None, site_key: str
    ) -> list[ManuscriptSnapshot]:
        self._set_page_size()
        table = self.wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "table#datatable")))
        headers = self._header_map(table)
        missing = {"manuscript_id", "title"} - headers.keys()
        if "current_status" not in headers and "final_disposition" not in headers:
            missing.add("current_status_or_final_disposition")
        if missing:
            raise ProviderContractError(TrackingDiagnosticCode.PROVIDER_TABLE_HEADERS_CHANGED)

        snapshots = []
        for row in table.find_elements(By.CSS_SELECTOR, "tr[id^='row']"):
            cells = [cell.text for cell in row.find_elements(By.CSS_SELECTOR, "td")]
            snapshots.append(self.parse_row_values(headers, cells, folder_key, journal, config_id, site_key))
        return snapshots

    def fetch(self, config: dict, credentials) -> list[dict]:
        self.login(config["journal_url"], credentials.username, credentials.password)
        collected = {}
        priority_by_id = {}

        for folder_key, label, href_fragment, priority in self.FOLDERS:
            self._return_to_author_menu()
            link = self._find_folder_link(label, href_fragment)
            if link is None:
                logger.info("%s: %s 为空", config["journal_name"], label)
                continue

            # Folder navigation must occur inside the content frame; direct
            # driver.get() can replace the wrapper and lose frame context.
            self.driver.execute_script("arguments[0].click();", link)
            self.wait.until(
                lambda driver: href_fragment.lower()
                in (driver.execute_script("return window.location.href") or "").lower()
                or bool(driver.find_elements(By.CSS_SELECTOR, "table#datatable"))
            )
            snapshots = self._parse_current_folder(
                folder_key,
                config["journal_name"],
                config.get("id"),
                config.get("site_key") or "unknown",
            )
            logger.info("%s: %s 获取到 %d 篇", config["journal_name"], label, len(snapshots))
            for snapshot in snapshots:
                current_priority = priority_by_id.get(snapshot.manuscript_id, -1)
                if priority >= current_priority:
                    collected[snapshot.manuscript_id] = snapshot
                    priority_by_id[snapshot.manuscript_id] = priority

        return [snapshot.to_dict() for snapshot in collected.values()]

    def close(self) -> None:
        if self.driver:
            self.driver.quit()
            self.driver = None
