"""Shared interpretation of provider submission dates (never changes source history)."""

import re
from datetime import datetime, timezone


def parse_submission_date(value: object) -> datetime | None:
    text = str(value or "").strip()
    # Python 3.10 accepts only 3/6 fractional digits; normalize ISO fractions.
    normalized = re.sub(r"(?<=\d)\.(\d+)", lambda match: "." + match[1][:6].ljust(6, "0"), text)
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError:
        parsed = None
        for fmt in ("%d-%b-%Y", "%d-%b-%y", "%b %d, %Y", "%B %d, %Y", "%Y/%m/%d", "%d %b %Y"):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
    if parsed is None:
        return None
    try:
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except (OverflowError, ValueError):
        return None


def submission_order_key(item: dict) -> tuple:
    parsed = parse_submission_date(item.get("submitted_date"))
    return (parsed is not None, parsed or datetime.min.replace(tzinfo=timezone.utc), item["original_id"])


def annotate_submission_date(item: dict) -> dict:
    parsed = parse_submission_date(item.get("submitted_date"))
    item["submission_sort_date"] = parsed.isoformat(timespec="microseconds") if parsed else ""
    return item
