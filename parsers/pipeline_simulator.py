#!/usr/bin/env python3
"""Simulate the omnichannel lead triage pipeline from ingest to agent alert.

Stages, in order:
  1. Ingest a channel event (Instagram DM, Zoko WhatsApp, Gmail, Shopify Inbox).
  2. Drop non-queries with a local stand-in for the OpenRouter intent classifier.
  3. Resolve the Shopify customer by phone or email.
  4. Build the interactive Google Chat card (ManyChat + CRM deep links).
  5. Append a row to the Google Sheets CRM log.

Identifiers in the bundled mocks are synthetic. Sheet writes and Chat posts
are printed, not sent.
"""

from __future__ import annotations

import io
import json
import os
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote

from shopify_email_parser import parse_shopify_inbox_email

ROOT = Path(__file__).resolve().parent.parent
MOCKS = ROOT / "mock_payloads"

CRM_BASE = "https://crm.ateliernorth.example/leads/create"
SHEET_ID = "1AtelierNorthCrmLogDemo"
MANYCHAT_LOOKUP = "https://app.manychat.com/contacts"

# Synthetic directory standing in for Shopify Admin customer search.
CUSTOMERS_BY_PHONE = {
    "919800011122": {
        "id": "gid://shopify/Customer/1001842",
        "name": "Rahul Mehta",
        "orders_count": 4,
    }
}
CUSTOMERS_BY_EMAIL = {
    "priya.nair@example.com": {
        "id": "gid://shopify/Customer/1001904",
        "name": "Priya Nair",
        "orders_count": 2,
    }
}

CHANNEL_LABELS = {
    "instagram_dm": "Instagram DM",
    "whatsapp_zoko": "WhatsApp",
    "gmail": "Gmail",
    "shopify_inbox": "Shopify Inbox",
}

AGENT_BY_VERTICAL = {
    "apparel": "Aanya Deshpande",
    "beauty": "Meher Kapoor",
    "home": "Vikram Shah",
    "post_purchase": "Care Desk",
    "general": "Triage Queue",
}

INTENT_RE = re.compile(
    r"(?i)\b("
    r"order|tracking|track|refund|exchange|return|cancel|available|availability|"
    r"stock|restock|size|hold|price|discount|deliver|delivery|shipping|ship|"
    r"parcel|status|buy|purchase|want|looking"
    r")\b|\?"
)
NON_QUERY_RE = re.compile(
    r"(?ix)^\s*(?:"
    r"thanks|thank\s+you|ty|ok|okay|k|hi|hey|hello|got\s+it|cool|"
    r"👍|🙏|❤+|❤️|😊"
    r")\s*[.!]?\s*$"
)
POST_PURCHASE_RE = re.compile(r"(?i)\b(order|tracking|track|refund|exchange|return|parcel|status|cancel)\b")
APPAREL_RE = re.compile(r"(?i)\b(dress|cargo|linen|shirt|apparel|size|set)\b")
BEAUTY_RE = re.compile(r"(?i)\b(serum|skincare|sandalwood|lipstick|fragrance)\b")
HOME_RE = re.compile(r"(?i)\b(candle|throw|decor|cushion)\b")


class C:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"


def _configure_stdio() -> None:
    """Turn on ANSI color in Windows terminals and write logs as UTF-8."""
    if os.environ.get("NO_COLOR"):
        for name in ("RESET", "BOLD", "DIM", "RED", "GREEN", "YELLOW", "BLUE", "MAGENTA", "CYAN"):
            setattr(C, name, "")

    for name in ("stdout", "stderr"):
        stream = getattr(sys, name)
        buffer = getattr(stream, "buffer", None)
        if buffer is None:
            continue
        try:
            setattr(
                sys,
                name,
                io.TextIOWrapper(buffer, encoding="utf-8", errors="replace", line_buffering=True),
            )
        except (AttributeError, OSError, ValueError):
            pass

    if os.name != "nt":
        return
    try:
        import ctypes

        kernel = ctypes.windll.kernel32
        handle = kernel.GetStdHandle(-11)
        mode = ctypes.c_uint()
        if kernel.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel.SetConsoleMode(handle, mode.value | 0x0004)
    except (AttributeError, OSError):
        pass


def _tag(label: str, color: str) -> str:
    return f"{color}{C.BOLD}{label:<8}{C.RESET}"


def _say(label: str, color: str, message: str) -> None:
    print(f"  {_tag(label, color)} {message}")


def _load(name: str) -> dict[str, Any]:
    path = MOCKS / name
    return json.loads(path.read_text(encoding="utf-8"))


def _digits(phone: str | None) -> str:
    return re.sub(r"\D", "", phone or "")


def _clip(text: str, limit: int = 88) -> str:
    compact = re.sub(r"\s+", " ", text).strip()
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3] + "..."


def classify_intent(text: str) -> dict[str, Any]:
    """Stand-in for the OpenRouter intent call used in the Pabbly workflow."""
    raw = (text or "").strip()
    if not raw or NON_QUERY_RE.match(raw) or not INTENT_RE.search(raw):
        return {
            "is_actionable_query": False,
            "vertical": "general",
            "reason": "greeting, acknowledgement, or no purchase/support intent",
            "classifier": "regex-stand-in",
            "simulated_latency_ms": 40,
        }

    if POST_PURCHASE_RE.search(raw):
        vertical = "post_purchase"
    elif BEAUTY_RE.search(raw):
        vertical = "beauty"
    elif HOME_RE.search(raw):
        vertical = "home"
    elif APPAREL_RE.search(raw):
        vertical = "apparel"
    else:
        vertical = "general"

    return {
        "is_actionable_query": True,
        "vertical": vertical,
        "reason": f"actionable {vertical.replace('_', ' ')} intent",
        "classifier": "regex-stand-in",
        "simulated_latency_ms": 180,
    }


def lookup_shopify_customer(phone: str | None, email: str | None) -> dict[str, Any] | None:
    """Stand-in for Admin API customer search on phone, then email."""
    digits = _digits(phone)
    if digits:
        for key, record in CUSTOMERS_BY_PHONE.items():
            if digits.endswith(key[-10:]):
                return record
    if email:
        return CUSTOMERS_BY_EMAIL.get(email.strip().lower())
    return None


def normalize_instagram(payload: dict[str, Any]) -> dict[str, Any]:
    subscriber = payload["subscriber"]
    handle = subscriber.get("ig_username") or subscriber.get("id")
    return {
        "source_channel": "instagram_dm",
        "customer_name": subscriber.get("name") or handle,
        "handle_or_phone": f"@{handle}",
        "phone": subscriber.get("phone"),
        "email": subscriber.get("email"),
        "raw_text": subscriber.get("last_input_text") or "",
        "manychat_url": subscriber.get("live_chat_url"),
        "dedupe_key": f"instagram_dm:{str(handle).lower()}",
    }


def normalize_whatsapp(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload["data"]
    customer = data["customer"]
    phone = customer.get("phoneNumber") or ""
    return {
        "source_channel": "whatsapp_zoko",
        "customer_name": customer.get("name") or phone,
        "handle_or_phone": phone,
        "phone": phone,
        "email": None,
        "raw_text": data.get("text") or "",
        "manychat_url": f"{MANYCHAT_LOOKUP}?q={_digits(phone)}",
        "dedupe_key": f"whatsapp_zoko:{_digits(phone)}",
    }


def normalize_shopify_email(payload: dict[str, Any], source_channel: str) -> dict[str, Any]:
    parsed = parse_shopify_inbox_email(payload.get("body_text") or payload.get("body") or "")
    email = parsed.get("customer_email")
    name = parsed.get("customer_name") or email or "Unknown customer"
    return {
        "source_channel": source_channel,
        "customer_name": name,
        "handle_or_phone": email or name,
        "phone": None,
        "email": email,
        "raw_text": parsed.get("inquiry_text") or "",
        "manychat_url": f"{MANYCHAT_LOOKUP}?q={email}" if email else MANYCHAT_LOOKUP,
        "dedupe_key": f"{source_channel}:{(email or name).lower()}",
    }


def build_google_chat_card(ticket: dict[str, Any], manychat_url: str, crm_url: str) -> dict[str, Any]:
    """Google Chat cards v2 payload with the two agent deep links."""
    customer = ticket["customer"]
    inquiry = ticket["inquiry"]
    shopify_id = customer["shopify_customer_id"] or "New contact — no Shopify match"
    channel_label = CHANNEL_LABELS.get(ticket["source_channel"], ticket["source_channel"])
    return {
        "fallbackText": (
            f"New lead {ticket['ticket_id']} from {channel_label}: {inquiry['raw_text']}"
        ),
        "cardsV2": [
            {
                "cardId": f"lead-{ticket['ticket_id']}",
                "card": {
                    "header": {
                        "title": f"Actionable lead · {channel_label}",
                        "subtitle": f"{customer['name']} · {customer['handle_or_phone']}",
                        "imageType": "CIRCLE",
                    },
                    "sections": [
                        {
                            "header": "Inquiry",
                            "collapsible": False,
                            "widgets": [
                                {
                                    "decoratedText": {
                                        "topLabel": "Vertical",
                                        "text": inquiry["vertical"].replace("_", " "),
                                        "startIcon": {"materialIcon": {"name": "sell"}},
                                    }
                                },
                                {
                                    "decoratedText": {
                                        "topLabel": "Shopify customer",
                                        "text": shopify_id,
                                        "startIcon": {"materialIcon": {"name": "person"}},
                                    }
                                },
                                {
                                    "decoratedText": {
                                        "topLabel": "Assigned",
                                        "text": ticket["dispatch"]["assigned_agent"] or "Unassigned",
                                        "startIcon": {"materialIcon": {"name": "support_agent"}},
                                    }
                                },
                                {"textParagraph": {"text": inquiry["raw_text"]}},
                                {
                                    "buttonList": {
                                        "buttons": [
                                            {
                                                "text": "Open ManyChat",
                                                "type": "OUTLINED",
                                                "onClick": {"openLink": {"url": manychat_url}},
                                            },
                                            {
                                                "text": "Create & Open Lead in CRM",
                                                "type": "FILLED",
                                                "onClick": {"openLink": {"url": crm_url}},
                                            },
                                        ]
                                    }
                                },
                            ],
                        }
                    ],
                },
            }
        ],
    }


def build_ticket(event: dict[str, Any], intent: dict[str, Any], shopify: dict[str, Any] | None, ticket_id: str) -> dict[str, Any]:
    return {
        "ticket_id": ticket_id,
        "source_channel": event["source_channel"],
        "customer": {
            "name": event["customer_name"],
            "handle_or_phone": event["handle_or_phone"],
            "shopify_customer_id": None if shopify is None else shopify["id"],
        },
        "inquiry": {
            "raw_text": event["raw_text"],
            "is_actionable_query": True,
            "vertical": intent["vertical"],
        },
        "dispatch": {
            "logged_to_google_sheet": False,
            "google_chat_alert_sent": False,
            "assigned_agent": AGENT_BY_VERTICAL.get(intent["vertical"], "Triage Queue"),
        },
    }


def append_sheet(sheet: list[dict[str, str]], ticket: dict[str, Any]) -> int:
    row_number = len(sheet) + 2  # row 1 is the header
    sheet.append(
        {
            "row": str(row_number),
            "ticket_id": ticket["ticket_id"],
            "source_channel": ticket["source_channel"],
            "name": ticket["customer"]["name"],
            "handle_or_phone": ticket["customer"]["handle_or_phone"],
            "shopify_customer_id": ticket["customer"]["shopify_customer_id"] or "",
            "vertical": ticket["inquiry"]["vertical"],
            "assigned_agent": ticket["dispatch"]["assigned_agent"] or "",
            "inquiry": ticket["inquiry"]["raw_text"],
        }
    )
    ticket["dispatch"]["logged_to_google_sheet"] = True
    return row_number


def process_event(
    title: str,
    event: dict[str, Any],
    sheet: list[dict[str, str]],
    seen: dict[str, str],
    sequence: list[int],
) -> None:
    print(f"\n{C.CYAN}{C.BOLD}-- {title}{C.RESET}")
    _say("INGEST", C.BLUE, f"{event['source_channel']}  {event['customer_name']}  {_clip(event['raw_text'])}")

    prior = seen.get(event["dedupe_key"])
    if prior:
        _say("DEDUP", C.YELLOW, f"suppressed — same contact already opened as {prior}")
        return

    intent = classify_intent(event["raw_text"])
    if not intent["is_actionable_query"]:
        _say(
            "FILTER",
            C.YELLOW,
            f"dropped by {intent['classifier']} ({intent['simulated_latency_ms']} ms) — {intent['reason']}",
        )
        return

    _say(
        "INTENT",
        C.GREEN,
        f"{intent['classifier']} ({intent['simulated_latency_ms']} ms) — {intent['reason']}",
    )

    shopify = lookup_shopify_customer(event.get("phone"), event.get("email"))
    if shopify:
        _say("SHOPIFY", C.GREEN, f"matched {shopify['id']} · {shopify['orders_count']} prior orders")
    else:
        _say("SHOPIFY", C.BLUE, "no phone or email match — net-new contact")

    sequence[0] += 1
    ticket_id = f"TKT-2026-{1000 + sequence[0]}"
    ticket = build_ticket(event, intent, shopify, ticket_id)
    seen[event["dedupe_key"]] = ticket_id

    row_number = append_sheet(sheet, ticket)
    sheet_url = (
        f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit#gid=0&range=A{row_number}"
    )
    _say("SHEET", C.GREEN, f"appended row {row_number} · {sheet_url}")

    crm_url = (
        f"{CRM_BASE}?ticket_id={quote(ticket_id)}"
        f"&channel={quote(ticket['source_channel'])}"
        f"&contact={quote(event['handle_or_phone'])}"
    )
    card = build_google_chat_card(ticket, event["manychat_url"], crm_url)
    ticket["dispatch"]["google_chat_alert_sent"] = True
    _say("CHAT", C.MAGENTA, "interactive card ready")
    print(f"{C.DIM}{json.dumps(card, indent=2, ensure_ascii=False)}{C.RESET}")
    _say("TICKET", C.GREEN, "unified lead ticket")
    print(f"{C.DIM}{json.dumps(ticket, indent=2, ensure_ascii=False)}{C.RESET}")


def _print_sheet(sheet: list[dict[str, str]]) -> None:
    print(f"\n{C.CYAN}{C.BOLD}-- Google Sheet CRM log (simulated){C.RESET}")
    if not sheet:
        _say("SHEET", C.YELLOW, "no rows")
        return
    header = f"{'Row':<5} {'Ticket':<14} {'Channel':<16} {'Agent':<18} Contact"
    print(f"{C.DIM}{header}{C.RESET}")
    for row in sheet:
        print(
            f"{row['row']:<5} {row['ticket_id']:<14} {row['source_channel']:<16} "
            f"{row['assigned_agent']:<18} {row['name']} ({row['handle_or_phone']})"
        )


def build_queue() -> list[tuple[str, dict[str, Any]]]:
    instagram = normalize_instagram(_load("instagram_webhook.json"))
    whatsapp = normalize_whatsapp(_load("zoko_whatsapp_webhook.json"))
    shopify_inbox = normalize_shopify_email(_load("shopify_inbox_email.json"), "shopify_inbox")
    gmail_ack = normalize_shopify_email(
        {
            "body_text": (
                "You received a new message in Shopify Inbox.\n\n"
                "From: Guest Shopper <guest.shopper@example.com>\n\n"
                "\"Thanks!\"\n"
            )
        },
        "gmail",
    )
    return [
        ("Instagram DM · ManyChat webhook", instagram),
        ("Instagram DM · duplicate delivery", instagram),
        ("WhatsApp · Zoko inbound webhook", whatsapp),
        ("Gmail · acknowledgement, not a query", gmail_ack),
        ("Shopify Inbox · notification email via Gmail", shopify_inbox),
    ]


def main() -> None:
    _configure_stdio()
    print(f"{C.BOLD}{C.CYAN}Omnichannel Support Triage - pipeline simulation{C.RESET}")
    print(f"{C.DIM}Ingest -> intent gate -> Shopify lookup -> Google Chat card -> Sheet log{C.RESET}")

    sheet: list[dict[str, str]] = []
    seen: dict[str, str] = {}
    sequence = [0]
    queue = build_queue()

    for title, event in queue:
        process_event(title, event, sheet, seen, sequence)

    _print_sheet(sheet)
    deduped = 0
    filtered = 0
    keys: set[str] = set()
    for _, event in queue:
        if event["dedupe_key"] in keys:
            deduped += 1
            continue
        keys.add(event["dedupe_key"])
        if not classify_intent(event["raw_text"])["is_actionable_query"]:
            filtered += 1
    print(
        f"\n{C.BOLD}Done.{C.RESET} ingested {len(queue)} | "
        f"dispatched {len(sheet)} | filtered {filtered} | duplicates {deduped}"
    )


if __name__ == "__main__":
    main()
