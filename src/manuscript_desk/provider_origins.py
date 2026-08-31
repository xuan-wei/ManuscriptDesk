from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit


class ProviderValidationError(ValueError):
    """Raised when a configured or current provider URL is not trusted."""


@dataclass(frozen=True)
class ProviderOrigin:
    key: str
    display_name: str
    allowed_domain: str

    def normalize_url(self, value: str) -> str:
        raw = (value or "").strip()
        if not raw or any(ord(character) < 32 for character in raw):
            raise ProviderValidationError("provider_url_invalid")
        parsed = urlsplit(raw)
        if parsed.scheme.casefold() != "https":
            raise ProviderValidationError("provider_https_required")
        if parsed.username is not None or parsed.password is not None:
            raise ProviderValidationError("provider_userinfo_forbidden")
        if not parsed.hostname or parsed.fragment:
            raise ProviderValidationError("provider_url_invalid")
        try:
            host = parsed.hostname.rstrip(".").encode("idna").decode("ascii").casefold()
            port = parsed.port
        except (UnicodeError, ValueError) as exc:
            raise ProviderValidationError("provider_host_invalid") from exc
        if port not in (None, 443):
            raise ProviderValidationError("provider_port_forbidden")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None or host == "localhost":
            raise ProviderValidationError("provider_host_forbidden")
        allowed = self.allowed_domain
        if host != allowed and not host.endswith(f".{allowed}"):
            raise ProviderValidationError("provider_host_mismatch")
        path = parsed.path or "/"
        return urlunsplit(("https", host, path, parsed.query, ""))

    def derive_site_key(self, value: str) -> str:
        parsed = urlsplit(self.normalize_url(value))
        parts = [part for part in parsed.path.split("/") if part]
        site = parts[0].casefold() if parts else "default"
        return f"{parsed.hostname}:{site}"


PROVIDER_ORIGINS = {
    "scholarone": ProviderOrigin("scholarone", "ScholarOne", "manuscriptcentral.com"),
    "editorial_manager": ProviderOrigin("editorial_manager", "Editorial Manager", "editorialmanager.com"),
}


def get_provider_origin(provider: str) -> ProviderOrigin:
    try:
        return PROVIDER_ORIGINS[provider]
    except KeyError as exc:
        raise ProviderValidationError("provider_unsupported") from exc


def normalize_provider_url(provider: str, value: str) -> str:
    return get_provider_origin(provider).normalize_url(value)


class UnsafeNavigationError(RuntimeError):
    """Safe public error raised after an unexpected provider redirect."""


class SafeProviderNavigation:
    def __init__(self, driver, provider: str):
        self.driver = driver
        self.origin = get_provider_origin(provider)

    def validate(self, value: str | None = None) -> str:
        target = value if value is not None else getattr(self.driver, "current_url", "")
        try:
            return self.origin.normalize_url(target)
        except ProviderValidationError as exc:
            raise UnsafeNavigationError("unsafe_provider_redirect") from exc

    def open(self, value: str) -> None:
        self.driver.get(self.validate(value))
        self.validate()

    def open_link(self, value: str) -> None:
        self.open(value)

    def before_credentials(self) -> None:
        self.validate()
