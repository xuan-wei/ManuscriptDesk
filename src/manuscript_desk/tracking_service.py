import logging
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from .browser_factory import BrowserLaunchError
from .credentials import resolve_tenant_credentials
from .repository import Repository
from .secrets_store import SecretStore
from .tracking_models import JournalTrackResult

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TenantRunContext:
    owner_user_id: int
    requested_by_user_id: int
    request_id: int | None = None
    run_id: int | None = None

    def __post_init__(self):
        if self.owner_user_id <= 0 or self.requested_by_user_id != self.owner_user_id:
            raise PermissionError("tenant run context must be owner-scoped")


def create_provider(provider: str, headless: bool):
    if provider == "scholarone":
        from .scholarone_tracker import ScholarOneTracker

        return ScholarOneTracker(headless=headless)
    if provider == "editorial_manager":
        from .editorial_manager_tracker import EditorialManagerTracker

        return EditorialManagerTracker(headless=headless)
    raise ValueError(f"不支持的投稿系统: {provider}")


def _safe_config(config: Mapping) -> dict:
    return {
        key: config.get(key)
        for key in (
            "id",
            "provider",
            "journal_name",
            "journal_url",
            "short_name",
            "username",
            "site_key",
            "credential_ref",
        )
    }


def _tracking_error_code(exc: Exception) -> str:
    if isinstance(exc, BrowserLaunchError):
        return "browser_start_failed"
    code = str(getattr(exc, "error_code", "") or exc).strip().casefold()
    allowed = {
        "browser_timeout",
        "credentials_unavailable",
        "empty_result_anomaly",
        "provider_login_failed",
        "provider_unavailable",
        "unsafe_provider_redirect",
    }
    return code if code in allowed else "tracking_failed"


class TrackingService:
    """Orchestrates provider scrapers while keeping credentials and writes tenant-bound."""

    def __init__(
        self,
        repository: Repository,
        secret_store: SecretStore,
        max_workers: int = 4,
        provider_factory: Callable[[str, bool], object] = create_provider,
    ):
        self.repository = repository
        self.secret_store = secret_store
        self.max_workers = max_workers
        self.provider_factory = provider_factory

    def _track_one(self, context: TenantRunContext, config: Mapping, headless: bool) -> dict:
        started = time.monotonic()
        safe = _safe_config(config)
        provider = safe.get("provider") or "scholarone"
        tracker = None
        try:
            credentials = resolve_tenant_credentials(safe, context.owner_user_id, self.secret_store)
            tracker = self.provider_factory(provider, headless)
            manuscripts = tracker.fetch(safe, credentials)
            return JournalTrackResult(
                safe.get("journal_name") or "",
                provider,
                safe.get("id"),
                True,
                manuscripts,
                elapsed_seconds=time.monotonic() - started,
            ).to_dict()
        except Exception as exc:
            error_code = _tracking_error_code(exc)
            logger.error(
                "tenant %s journal %s tracking failed: type=%s code=%s",
                context.owner_user_id,
                safe.get("id"),
                type(exc).__name__,
                error_code,
            )
            return JournalTrackResult(
                safe.get("journal_name") or "",
                provider,
                safe.get("id"),
                False,
                error_code=error_code,
                elapsed_seconds=time.monotonic() - started,
            ).to_dict()
        finally:
            if tracker:
                try:
                    tracker.close()
                except Exception:
                    logger.warning("provider close failed for tenant %s", context.owner_user_id)

    def _persist(self, context: TenantRunContext, config: Mapping, result: Mapping) -> None:
        config_id = int(config["id"])
        if result.get("success"):
            manuscripts = list(result.get("manuscripts", []))
            outcome = str(result.get("outcome") or ("complete" if manuscripts else "anomalous_empty"))
        else:
            manuscripts = []
            outcome = "failed"
        self.repository.persist_journal_result(
            context.owner_user_id,
            config_id,
            {
                "success": bool(result.get("success")),
                "outcome": outcome,
                "manuscripts": manuscripts,
                "error_code": result.get("error_code") or ("tracking_failed" if not result.get("success") else ""),
            },
        )

    def track_journals(
        self, context: TenantRunContext, journal_configs: Sequence[Mapping] | None = None, headless: bool = True
    ) -> dict:
        configs = list(
            journal_configs
            if journal_configs is not None
            else self.repository.list_journal_configs(context.owner_user_id, enabled_only=True)
        )
        # Reject injected/cross-tenant IDs before workers or secret resolution run.
        owned = {row["id"]: row for row in self.repository.list_journal_configs(context.owner_user_id)}
        if any(config.get("id") not in owned for config in configs):
            raise PermissionError("journal configuration does not belong to tenant")
        if not configs:
            return {
                "success": False,
                "result": "failed",
                "error": "no_enabled_journals",
                "total_manuscripts": 0,
                "attempted_journals": 0,
                "successful_journals": 0,
                "failed_journals": 0,
                "results": {},
                "journal_results": [],
            }
        results = []
        with ThreadPoolExecutor(max_workers=min(self.max_workers, len(configs))) as executor:
            futures = {
                executor.submit(self._track_one, context, owned[c["id"]], headless): owned[c["id"]] for c in configs
            }
            for future in as_completed(futures):
                result = future.result()
                results.append(result)
                self._persist(context, futures[future], result)
        successful = sum(bool(result.get("success")) for result in results)
        attempted = len(results)
        failed = attempted - successful
        total_manuscripts = sum(len(result.get("manuscripts", [])) for result in results)
        if successful == 0:
            outcome = "failed"
        elif failed:
            outcome = "partial"
        elif total_manuscripts == 0:
            outcome = "no_changes"
        else:
            outcome = "success"
        return {
            "success": successful > 0,
            "result": outcome,
            "error": next((r.get("error_code") for r in results if not r.get("success")), None),
            "total_manuscripts": total_manuscripts,
            "attempted_journals": attempted,
            "successful_journals": successful,
            "failed_journals": failed,
            "results": {r["journal_name"]: r.get("manuscripts", []) for r in results},
            "journal_results": results,
        }
