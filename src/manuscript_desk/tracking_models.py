from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum


class TrackingErrorCode(str, Enum):
    BROWSER_START_FAILED = "browser_start_failed"
    BROWSER_TAB_CRASHED = "browser_tab_crashed"
    BROWSER_TIMEOUT = "browser_timeout"
    CREDENTIALS_UNAVAILABLE = "credentials_unavailable"
    EMPTY_RESULT_ANOMALY = "empty_result_anomaly"
    PROVIDER_LOGIN_FAILED = "provider_login_failed"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    UNSAFE_PROVIDER_REDIRECT = "unsafe_provider_redirect"
    TRACKING_FAILED = "tracking_failed"


class SafeTrackingError(RuntimeError):
    """Tracking failure whose public representation is a fixed, non-sensitive code."""

    error_code: TrackingErrorCode = TrackingErrorCode.TRACKING_FAILED

    def __init__(self) -> None:
        super().__init__(self.error_code.value)


class BrowserTabCrashedError(SafeTrackingError):
    error_code = TrackingErrorCode.BROWSER_TAB_CRASHED


class CredentialsUnavailableError(SafeTrackingError):
    error_code = TrackingErrorCode.CREDENTIALS_UNAVAILABLE


class ProviderLoginError(SafeTrackingError):
    error_code = TrackingErrorCode.PROVIDER_LOGIN_FAILED


class ProviderUnavailableError(SafeTrackingError):
    error_code = TrackingErrorCode.PROVIDER_UNAVAILABLE


_BROWSER_CRASH_SIGNATURES = (
    "tab crashed",
    "session deleted because of page crash",
    "session deleted as the browser has closed the connection",
    "invalid session id",
    "chrome not reachable",
    "disconnected: not connected to devtools",
    "disconnected: unable to receive message from renderer",
    "target frame detached",
)


def is_browser_tab_crash(exc: BaseException) -> bool:
    """Recognize Selenium/Chrome terminal tab and session failures."""

    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        detail = str(current).casefold()
        if any(signature in detail for signature in _BROWSER_CRASH_SIGNATURES):
            return True
        current = current.__cause__ or current.__context__
    return False


def raise_if_browser_tab_crashed(exc: BaseException) -> None:
    if isinstance(exc, BrowserTabCrashedError):
        raise exc
    if is_browser_tab_crash(exc):
        raise BrowserTabCrashedError() from exc


@dataclass
class ManuscriptSnapshot:
    """Provider-neutral manuscript snapshot returned by a tracker."""

    provider: str
    journal_config_id: int | None
    journal: str
    manuscript_id: str
    original_id: str
    source_original_id: str
    version_number: int
    title: str
    author_role: str = "unknown"
    authorship: str = ""
    status_text: str = ""
    status: str = ""
    detailed_status: str = ""
    submitted_date: str = ""
    decision_date: str = ""
    status_date: str = ""
    final_disposition: str = ""
    workflow_folder: str = ""
    SE_or_DE: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class JournalTrackResult:
    """Outcome for one configured journal."""

    journal_name: str
    provider: str
    journal_config_id: int | None
    success: bool
    manuscripts: list[dict] = field(default_factory=list)
    error_code: str = ""
    error_message: str = ""
    elapsed_seconds: float = 0.0

    @property
    def manuscripts_found(self) -> int:
        return len(self.manuscripts)

    def to_dict(self) -> dict:
        return {
            "journal_name": self.journal_name,
            "provider": self.provider,
            "journal_config_id": self.journal_config_id,
            "success": self.success,
            "manuscripts": self.manuscripts,
            "manuscripts_found": self.manuscripts_found,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "elapsed_seconds": self.elapsed_seconds,
        }
