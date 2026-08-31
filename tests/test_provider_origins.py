import pytest

from manuscript_desk.provider_origins import (
    ProviderValidationError,
    SafeProviderNavigation,
    UnsafeNavigationError,
    get_provider_origin,
    normalize_provider_url,
)


@pytest.mark.parametrize(
    ("provider", "value", "expected"),
    [
        ("scholarone", "https://MC.ManuscriptCentral.com/site", "https://mc.manuscriptcentral.com/site"),
        ("scholarone", "https://mc.manuscriptcentral.com./site", "https://mc.manuscriptcentral.com/site"),
        ("editorial_manager", "https://www.editorialmanager.com/journal", "https://www.editorialmanager.com/journal"),
    ],
)
def test_provider_urls_are_normalized(provider, value, expected):
    assert normalize_provider_url(provider, value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "http://mc.manuscriptcentral.com/site",
        "https://manuscriptcentral.com.example.test/site",
        "https://example.test/manuscriptcentral.com/site",
        "https://user:password@mc.manuscriptcentral.com/site",
        "https://mc.manuscriptcentral.com:444/site",
        "https://127.0.0.1/site",
        "https://mc.manuscriptcentral.com/site#fragment",
        "https://manuscriptcentral.com.evil.test/",
    ],
)
def test_provider_urls_reject_untrusted_destinations(value):
    with pytest.raises(ProviderValidationError):
        normalize_provider_url("scholarone", value)


def test_site_key_is_derived_from_normalized_url():
    origin = get_provider_origin("scholarone")
    assert origin.derive_site_key("https://MC.manuscriptcentral.com/JOURNAL/login") == (
        "mc.manuscriptcentral.com:journal"
    )


class RedirectingDriver:
    def __init__(self, current_url=""):
        self.current_url = current_url

    def get(self, _value):
        self.current_url = "https://lookalike.example.test/login"


def test_navigation_revalidates_redirects():
    navigation = SafeProviderNavigation(RedirectingDriver(), "scholarone")
    with pytest.raises(UnsafeNavigationError):
        navigation.open("https://mc.manuscriptcentral.com/site")


def test_navigation_checks_origin_before_credentials():
    navigation = SafeProviderNavigation(RedirectingDriver("https://lookalike.example.test/login"), "scholarone")
    with pytest.raises(UnsafeNavigationError):
        navigation.before_credentials()
