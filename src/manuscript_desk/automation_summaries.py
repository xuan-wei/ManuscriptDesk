from __future__ import annotations

from collections.abc import Mapping
from typing import Any

SUMMARY_VERSION = 1
MAX_FAILURES = 5
MAX_JOURNAL_NAME_LENGTH = 100

_RESULT_CODES = {"success", "partial", "no_changes", "failed", "running", "cancelled", "finished"}
_UPDATE_SCOPES = {"all", "starred", "working"}
_REASON_CODES = {
    "no_enabled_starred_journals",
    "no_enabled_working_journals",
    "no_enabled_journals",
}
_ERROR_CODES = {
    "automation_failed",
    "browser_start_failed",
    "browser_tab_crashed",
    "browser_timeout",
    "credentials_unavailable",
    "email_delivery_failed",
    "empty_result_anomaly",
    "no_enabled_journals",
    "personal_smtp_unavailable",
    "provider_login_failed",
    "provider_unavailable",
    "system_smtp_unavailable",
    "tracking_failed",
    "unsafe_provider_redirect",
}
_COUNT_FIELDS = (
    "total_manuscripts",
    "attempted_journals",
    "successful_journals",
    "failed_journals",
    "requested_journals",
    "skipped_disabled_journals",
    "item_count",
    "new_status_count",
    "recipient_count",
)


def _safe_count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return min(value, 1_000_000_000)


def _safe_result(value: object, status: str) -> str:
    candidate = str(value or "").strip().casefold()
    if candidate in _RESULT_CODES:
        return candidate
    fallback = str(status or "").strip().casefold()
    return fallback if fallback in _RESULT_CODES else "failed"


def _safe_error(value: object) -> str | None:
    candidate = str(value or "").strip().casefold()
    return candidate if candidate in _ERROR_CODES else None


def generic_historical_summary(status: str = "failed") -> dict[str, Any]:
    return {
        "summary_version": SUMMARY_VERSION,
        "result": _safe_result(None, status),
        "historical": True,
        "message_code": "historical_summary_unavailable",
    }


def project_automation_summary(
    value: object,
    *,
    status: str,
    kind: str | None = None,
    journal_names_by_id: Mapping[int, str] | None = None,
    historical: bool = False,
) -> dict[str, Any]:
    """Return the only automation-run summary shape allowed at rest or over APIs."""
    if not isinstance(value, Mapping):
        return (
            generic_historical_summary(status)
            if historical
            else {
                "summary_version": SUMMARY_VERSION,
                "result": _safe_result(None, status),
            }
        )

    known_inputs = {
        "summary_version",
        "result",
        "success",
        "error",
        "error_code",
        "historical",
        "message_code",
        "update_scope",
        "reason",
        "failures",
        "journal_results",
        *_COUNT_FIELDS,
    }
    recognized = any(key in value for key in known_inputs)
    if historical and not recognized:
        return generic_historical_summary(status)

    result: dict[str, Any] = {
        "summary_version": SUMMARY_VERSION,
        "result": _safe_result(value.get("result"), status),
    }
    if kind in {"update", "email"}:
        result["kind"] = kind

    for field in _COUNT_FIELDS:
        count = _safe_count(value.get(field))
        if count is not None:
            result[field] = count

    scope = str(value.get("update_scope") or "").strip().casefold()
    if scope in _UPDATE_SCOPES:
        result["update_scope"] = scope
    reason = str(value.get("reason") or "").strip().casefold()
    if reason in _REASON_CODES:
        result["reason"] = reason

    error_code = _safe_error(value.get("error_code") or value.get("error"))
    if error_code:
        result["error_code"] = error_code

    names = journal_names_by_id or {}
    owned_names = {str(name).strip() for name in names.values() if str(name).strip()}
    raw_failures = value.get("failures")
    if not isinstance(raw_failures, list):
        raw_failures = value.get("journal_results")
    failures: list[dict[str, str]] = []
    if isinstance(raw_failures, list):
        for entry in raw_failures:
            if len(failures) >= MAX_FAILURES:
                break
            if not isinstance(entry, Mapping):
                continue
            if entry.get("success") is True:
                continue
            code = _safe_error(entry.get("code") or entry.get("error_code") or entry.get("error"))
            config_id = entry.get("journal_config_id")
            if config_id is None:
                config_id = entry.get("config_id")
            if config_id is None:
                config_id = entry.get("id")
            name = names.get(config_id) if isinstance(config_id, int) and not isinstance(config_id, bool) else None
            if not name:
                candidate_name = str(entry.get("journal_name") or "").strip()
                name = candidate_name if candidate_name in owned_names else None
            if not code:
                code = "tracking_failed"
            failure: dict[str, str] = {"code": code}
            if name:
                failure["journal_name"] = str(name).strip()[:MAX_JOURNAL_NAME_LENGTH]
            failures.append(failure)
    if failures:
        result["failures"] = failures

    if historical:
        result["historical"] = True
    return result
