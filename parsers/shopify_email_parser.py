#!/usr/bin/env python3
"""Extract a customer inquiry from a Shopify Inbox notification email.

Shopify Inbox does not hand the store a structured webhook for every chat.
The practical production path is a Gmail notification. This module turns that
unstructured body into the three fields the rest of the pipeline needs:
customer name, customer email, and the inquiry text itself.

System addresses on shopify.com are ignored so the store's own notification
sender is never stored as the customer.
"""

from __future__ import annotations

import html
import json
import re
from pathlib import Path
from typing import Any

EMAIL_RE = re.compile(
    r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}",
    re.IGNORECASE,
)

# "From: Priya Nair <priya.nair@example.com>" or "Customer: Jane Doe (jane@x.com)"
FROM_LINE_RE = re.compile(
    r"(?im)^(?:from|customer|sender)\s*:\s*"
    r"(?P<name>[A-Za-z][A-Za-z .'\-]{0,80}?)"
    r"(?:"
    r"\s*<\s*(?P<email>[^>\n]+)\s*>"
    r"|\s+\((?P<email2>[^)\n]+)\)"
    r")?"
    r"\s*$"
)

NAME_LABEL_RE = re.compile(
    r"(?im)^(?:customer\s*name|contact\s*name|name)\s*:\s*(?P<name>[^\n<]{1,80})"
)

EMAIL_LABEL_RE = re.compile(
    r"(?im)^(?:customer\s*)?e-?mail\s*:\s*(?P<email>\S+)"
)

QUOTED_MESSAGE_RE = re.compile(
    r"[\"“](?P<msg>.+?)[\"”]",
    re.DOTALL,
)

MESSAGE_BLOCK_RE = re.compile(
    r"(?is)(?:^|\n)\s*(?:message|inquiry|customer\s+wrote|they\s+said)\s*:\s*"
    r"(?P<msg>.+?)"
    r"(?:\n\s*(?:reply|view|open|unsubscribe|—|-{3,}|https?://)|\Z)"
)

NOISE_LINE_RE = re.compile(
    r"(?i)^("
    r"reply|view conversation|view in shopify|open in shopify|"
    r"you received|new message|shopify inbox|unsubscribe|this email|"
    r"—|-{3,}"
    r")"
)

SYSTEM_EMAIL_DOMAINS = ("shopify.com", "shopifyemail.com", "myshopify.com")
SYSTEM_NAMES = {
    "shopify",
    "shopify inbox",
    "shopify chat",
    "notification",
    "notifications",
}


def parse_shopify_inbox_email(body: str | None) -> dict[str, Any]:
    """Parse a Shopify Inbox notification body.

    Never raises on missing or partial sender data. Absent fields are None
    and ``parse_confidence`` reports how many of the three targets were found.
    """
    text = _to_plain_text("" if body is None else str(body))

    customer_email = _email_from_label(text)
    customer_name, from_email = _identity_from_header(text)
    if from_email:
        customer_email = from_email

    if customer_name is None:
        customer_name = _name_from_label(text)

    if customer_email is None:
        customer_email = _first_customer_email(text)

    inquiry = _extract_inquiry(text)
    return {
        "customer_name": customer_name,
        "customer_email": customer_email,
        "inquiry_text": inquiry,
        "parse_confidence": _confidence(customer_name, customer_email, inquiry),
    }


def _to_plain_text(body: str) -> str:
    text = body.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p\s*>", "\n", text)
    text = html.unescape(text)

    def _keep_email_brackets(match: re.Match[str]) -> str:
        inner = match.group(1).strip()
        if EMAIL_RE.fullmatch(inner):
            return match.group(0)
        return " "

    text = re.sub(r"<([^>\n]+)>", _keep_email_brackets, text)
    return text.strip()


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = re.sub(r"\s+", " ", value).strip(" \t\"'“”")
    cleaned = cleaned.strip()
    return cleaned or None


def _is_system_email(email: str) -> bool:
    domain = email.rsplit("@", 1)[-1].lower()
    return any(domain == suffix or domain.endswith("." + suffix) for suffix in SYSTEM_EMAIL_DOMAINS)


def _normalize_email(raw: str | None) -> str | None:
    if not raw:
        return None
    match = EMAIL_RE.search(raw.strip("<>().,;"))
    if not match:
        return None
    email = match.group(0).lower()
    if _is_system_email(email):
        return None
    return email


def _usable_name(raw: str | None) -> str | None:
    name = _clean(raw)
    if not name:
        return None
    if "@" in name:
        name = _clean(EMAIL_RE.sub("", name))
    if not name or name.lower() in SYSTEM_NAMES:
        return None
    return name


def _email_from_label(text: str) -> str | None:
    match = EMAIL_LABEL_RE.search(text)
    if not match:
        return None
    return _normalize_email(match.group("email"))


def _identity_from_header(text: str) -> tuple[str | None, str | None]:
    match = FROM_LINE_RE.search(text)
    if not match:
        return None, None
    raw_email = match.group("email") or match.group("email2")
    return _usable_name(match.group("name")), _normalize_email(raw_email)


def _name_from_label(text: str) -> str | None:
    match = NAME_LABEL_RE.search(text)
    if not match:
        return None
    return _usable_name(match.group("name"))


def _first_customer_email(text: str) -> str | None:
    for candidate in EMAIL_RE.findall(text):
        normalized = _normalize_email(candidate)
        if normalized:
            return normalized
    return None


def _extract_inquiry(text: str) -> str | None:
    block = MESSAGE_BLOCK_RE.search(text)
    if block:
        inquiry = _clean_inquiry(block.group("msg"))
        if inquiry:
            return inquiry

    quoted = QUOTED_MESSAGE_RE.search(text)
    if quoted:
        inquiry = _clean_inquiry(quoted.group("msg"))
        if inquiry:
            return inquiry

    paragraphs: list[str] = []
    for para in re.split(r"\n\s*\n", text):
        kept: list[str] = []
        for line in para.splitlines():
            stripped = line.strip()
            if not stripped or NOISE_LINE_RE.match(stripped):
                continue
            if re.match(r"(?i)^(from|sender|customer|customer name|contact name|name|e-?mail|customer e-?mail)\s*:", stripped):
                continue
            if stripped.lower().startswith("http://") or stripped.lower().startswith("https://"):
                continue
            kept.append(stripped)
        joined = _clean(" ".join(kept))
        if joined and len(joined) >= 12 and not joined.lower().startswith("inbox-noreply@"):
            paragraphs.append(joined)
    if not paragraphs:
        return None
    return max(paragraphs, key=len)


def _clean_inquiry(raw: str) -> str | None:
    inquiry = _clean(raw)
    if not inquiry:
        return None
    if inquiry.lower().startswith("http://") or inquiry.lower().startswith("https://"):
        return None
    return inquiry


def _confidence(name: str | None, email: str | None, inquiry: str | None) -> str:
    score = sum(value is not None for value in (name, email, inquiry))
    return {3: "high", 2: "medium", 1: "low", 0: "none"}[score]


def _print_result(title: str, result: dict[str, Any]) -> None:
    print(f"\n== {title} ==")
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    mock_path = Path(__file__).resolve().parent.parent / "mock_payloads" / "shopify_inbox_email.json"
    print("Shopify Inbox email parser — sample run")

    if mock_path.is_file():
        payload = json.loads(mock_path.read_text(encoding="utf-8"))
        _print_result("mock_payloads/shopify_inbox_email.json", parse_shopify_inbox_email(payload.get("body_text", "")))
    else:
        print(f"Mock file not found at {mock_path}")

    missing_sender = (
        "You received a new message in Shopify Inbox.\n\n"
        "\"hello — just browsing, thanks\"\n\n"
        "Reply in Shopify Inbox:\n"
        "https://admin.shopify.com/store/atelier-north/inbox/90001\n"
    )
    _print_result("missing sender info", parse_shopify_inbox_email(missing_sender))
    _print_result("empty body", parse_shopify_inbox_email("   "))
