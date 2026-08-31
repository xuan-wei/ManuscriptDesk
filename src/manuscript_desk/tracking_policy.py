import re

# Workflow states that should remain in history but not appear in current
# dashboards, statistics, starred pages, or change counters.
IGNORED_CURRENT_STATUSES: dict[str, set[str]] = {
    "scholarone": {"Draft"},
    "editorial_manager": {"Draft"},
}


def is_ignored_current_status(provider: str, status: str) -> bool:
    provider_name = provider or "scholarone"
    return (status or "") in IGNORED_CURRENT_STATUSES.get(provider_name, set())


def extract_original_id_and_version(manuscript_id: str, provider: str = "scholarone") -> tuple[str, int]:
    if provider == "editorial_manager":
        match = re.match(r"^(.+?)R(\d+)$", manuscript_id, re.IGNORECASE)
        if match:
            return match.group(1), int(match.group(2)) + 1
        return manuscript_id, 1

    match = re.match(r"^(.+?)(?:\.R(\d+))?$", manuscript_id)
    if match:
        return match.group(1), int(match.group(2)) + 1 if match.group(2) else 1
    return manuscript_id, 1


def classify_status(status: str, provider: str = "scholarone", final_disposition: str = "") -> tuple[str, str]:
    current = (status or "").strip()
    current_lower = current.lower()

    if provider == "editorial_manager":
        disposition = (final_disposition or "").strip()
        disposition_lower = disposition.lower()
        if "accept" in disposition_lower or "completed - accept" in current_lower:
            return "Accept", current or "Completed - Accept"
        if "reject and resubmit" in disposition_lower or "reject and resubmit" in current_lower:
            return "Reject and Resubmit", current or disposition
        if "reject" in disposition_lower or "completed - reject" in current_lower:
            return "Reject", current or "Completed - Reject"
        if "minor revision" in current_lower or "major revision" in current_lower:
            return "Revision", current
        if any(word in current_lower for word in ("revision requested", "revise")):
            return "Revision", current
        if "draft" in current_lower or "incomplete submission" in current_lower:
            return "Draft", current or "Draft"
        return "Under Review", current or "Unknown"

    if not current:
        return "Under Review", "Unknown"
    if "accept" in current_lower:
        return "Accept", "Accept"
    if "reject and resubmit" in current_lower or ("resubmit" in current_lower and "reject" in current_lower):
        return "Reject and Resubmit", "Reject and Resubmit"
    if "minor revision" in current_lower:
        return "Revision", "Minor Revision"
    if "major revision" in current_lower:
        return "Revision", "Major Revision"
    if "revision" in current_lower or "revise" in current_lower:
        return "Revision", "Revision"
    if "reject" in current_lower:
        return "Reject", "Desk Reject" if "desk reject" in current_lower else "Reject"
    if "draft" in current_lower:
        return "Draft", "Draft"
    if "awaiting" in current_lower:
        match = re.search(r"[Aa]waiting\s+([^\)\n]+)", current)
        return "Under Review", f"Awaiting {match.group(1).strip()}" if match else "Awaiting"
    if "under review" in current_lower:
        return "Under Review", "Under Review"
    return "Under Review", current or "其他状态"
