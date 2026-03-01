"""Phone number normalization utility."""

import re


def normalize_phone(phone: str) -> str:
    """Normalize phone to E.164-ish format: strip spaces/hyphens, add + prefix.

    Returns empty string if phone has fewer than 5 digits.

    Examples:
        "91 98765 43210" → "+919876543210"
        "+91-9876-543210" → "+919876543210"
        "919876543210" → "+919876543210"
        "" → ""
    """
    phone = phone.strip().replace(" ", "").replace("-", "")
    if len(re.sub(r"\D", "", phone)) < 5:
        return ""
    if not phone.startswith("+"):
        phone = f"+{phone}"
    return phone
