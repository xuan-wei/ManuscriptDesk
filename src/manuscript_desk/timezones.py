"""IANA timezone validation and grouped UI data."""

from __future__ import annotations

from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

DEFAULT_TIMEZONE = "Asia/Shanghai"
_PROFILE_TIMEZONE_CHOICES = (
    ("UTC", "UTC · 协调世界时"),
    ("Pacific/Honolulu", "UTC−10 · 檀香山"),
    ("America/Anchorage", "UTC−09/−08 · 安克雷奇"),
    ("America/Los_Angeles", "UTC−08/−07 · 洛杉矶"),
    ("America/Denver", "UTC−07/−06 · 丹佛"),
    ("America/Chicago", "UTC−06/−05 · 芝加哥"),
    ("America/New_York", "UTC−05/−04 · 纽约"),
    ("America/Halifax", "UTC−04/−03 · 哈利法克斯"),
    ("America/Sao_Paulo", "UTC−03 · 圣保罗"),
    ("Atlantic/Azores", "UTC−01/+00 · 亚速尔群岛"),
    ("Europe/London", "UTC+00/+01 · 伦敦"),
    ("Europe/Paris", "UTC+01/+02 · 巴黎"),
    ("Europe/Helsinki", "UTC+02/+03 · 赫尔辛基"),
    ("Africa/Johannesburg", "UTC+02 · 约翰内斯堡"),
    ("Europe/Moscow", "UTC+03 · 莫斯科"),
    ("Asia/Dubai", "UTC+04 · 迪拜"),
    ("Asia/Karachi", "UTC+05 · 卡拉奇"),
    ("Asia/Kolkata", "UTC+05:30 · 加尔各答"),
    ("Asia/Dhaka", "UTC+06 · 达卡"),
    ("Asia/Bangkok", "UTC+07 · 曼谷"),
    ("Asia/Shanghai", "UTC+08 · 上海"),
    ("Asia/Tokyo", "UTC+09 · 东京"),
    ("Australia/Sydney", "UTC+10/+11 · 悉尼"),
    ("Pacific/Auckland", "UTC+12/+13 · 奥克兰"),
)
_GROUP_ORDER = (
    "UTC",
    "Africa",
    "America",
    "Antarctica",
    "Arctic",
    "Asia",
    "Atlantic",
    "Australia",
    "Europe",
    "Indian",
    "Pacific",
    "Etc / Other",
)


def validate_timezone(value: object) -> str:
    """Return a valid IANA timezone key or raise ``ValueError``."""
    timezone_name = str(value or "").strip()
    if not timezone_name or len(timezone_name) > 255 or "\x00" in timezone_name:
        raise ValueError("invalid timezone")
    try:
        ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        raise ValueError("invalid timezone") from exc
    return timezone_name


def profile_timezone_choices() -> tuple[tuple[str, str], ...]:
    """Return the deliberate, compact set of timezone choices used by profiles."""
    return _PROFILE_TIMEZONE_CHOICES


@lru_cache(maxsize=1)
def timezone_names() -> tuple[str, ...]:
    """Return every loadable timezone key in stable, duplicate-free order."""
    names = {name for name in available_timezones() if name and "\x00" not in name}
    names.add("UTC")
    return tuple(sorted(name for name in names if _is_loadable(name)))


def _is_loadable(name: str) -> bool:
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return False
    return True


@lru_cache(maxsize=1)
def grouped_timezones() -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Group timezone keys for a native ``select`` without changing their values."""
    groups: dict[str, list[str]] = {label: [] for label in _GROUP_ORDER}
    for name in timezone_names():
        if name == "UTC":
            label = "UTC"
        else:
            prefix = name.split("/", 1)[0]
            label = prefix if prefix in groups and prefix != "UTC" else "Etc / Other"
        groups[label].append(name)
    return tuple((label, tuple(groups[label])) for label in _GROUP_ORDER if groups[label])
