"""Parser coverage for Shopify Inbox notification bodies."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from parsers.shopify_email_parser import parse_shopify_inbox_email  # noqa: E402

MOCK_EMAIL = ROOT / "mock_payloads" / "shopify_inbox_email.json"

VALID_FROM_LINE = """\
You received a new message in Shopify Inbox.

From: Priya Nair <priya.nair@example.com>

"Hi, I placed order #1842 three days ago and the tracking still says label created. Can someone check this for me before the weekend?"

Reply in Shopify Inbox:
https://admin.shopify.com/store/atelier-north/inbox/88421

— Shopify
inbox-noreply@shopify.com
"""

LABELED_FIELDS = """\
Customer name: Arjun Shah
Customer email: arjun.shah@outlook.com

Message:
Do you restock the sandalwood candle this week? I would like two if you can hold them.

View conversation:
https://admin.shopify.com/store/atelier-north/inbox/88488
"""

MISSING_SENDER = """\
You received a new message in Shopify Inbox.

"hello — just browsing, thanks"

Reply in Shopify Inbox:
https://admin.shopify.com/store/atelier-north/inbox/90001
"""

HTML_NOTIFICATION = (
    "<p>From: Siobhan O'Brien &lt;siobhan.obrien@pm.me&gt;</p>"
    "<p>Message: Can you exchange a size M dress for size S? Order 1904.</p>"
    "<p>Reply in Shopify Inbox:<br>https://admin.shopify.com/store/atelier-north/inbox/90110</p>"
)

SYSTEM_SENDER_ONLY = """\
From: Shopify Inbox <inbox-noreply@shopify.com>

"Where is my parcel? Order 2001 has not moved."
"""


class ShopifyEmailParserTests(unittest.TestCase):
    def test_valid_from_line_extracts_name_email_and_inquiry(self) -> None:
        result = parse_shopify_inbox_email(VALID_FROM_LINE)

        self.assertEqual(result["customer_name"], "Priya Nair")
        self.assertEqual(result["customer_email"], "priya.nair@example.com")
        self.assertIn("order #1842", result["inquiry_text"])
        self.assertNotIn("inbox-noreply@shopify.com", result["inquiry_text"])
        self.assertEqual(result["parse_confidence"], "high")

    def test_labeled_name_email_and_message_block(self) -> None:
        result = parse_shopify_inbox_email(LABELED_FIELDS)

        self.assertEqual(result["customer_name"], "Arjun Shah")
        self.assertEqual(result["customer_email"], "arjun.shah@outlook.com")
        self.assertIn("sandalwood candle", result["inquiry_text"])
        self.assertNotIn("https://", result["inquiry_text"])

    def test_mock_payload_file(self) -> None:
        payload = json.loads(MOCK_EMAIL.read_text(encoding="utf-8"))
        result = parse_shopify_inbox_email(payload["body_text"])

        self.assertEqual(result["customer_name"], "Priya Nair")
        self.assertEqual(result["customer_email"], "priya.nair@example.com")
        self.assertTrue(result["inquiry_text"].startswith("Hi, I placed order #1842"))

    def test_crlf_bodies(self) -> None:
        result = parse_shopify_inbox_email(VALID_FROM_LINE.replace("\n", "\r\n"))

        self.assertEqual(result["customer_email"], "priya.nair@example.com")
        self.assertIn("order #1842", result["inquiry_text"])

    def test_html_escaped_sender_with_apostrophe(self) -> None:
        result = parse_shopify_inbox_email(HTML_NOTIFICATION)

        self.assertEqual(result["customer_name"], "Siobhan O'Brien")
        self.assertEqual(result["customer_email"], "siobhan.obrien@pm.me")
        self.assertIn("exchange a size M dress", result["inquiry_text"])

    def test_missing_sender_keeps_the_inquiry(self) -> None:
        result = parse_shopify_inbox_email(MISSING_SENDER)

        self.assertIsNone(result["customer_name"])
        self.assertIsNone(result["customer_email"])
        self.assertIn("just browsing", result["inquiry_text"])
        self.assertEqual(result["parse_confidence"], "low")

    def test_system_sender_is_not_treated_as_the_customer(self) -> None:
        result = parse_shopify_inbox_email(SYSTEM_SENDER_ONLY)

        self.assertIsNone(result["customer_name"])
        self.assertIsNone(result["customer_email"])
        self.assertIn("Order 2001", result["inquiry_text"])

    def test_empty_and_blank_bodies(self) -> None:
        for body in ("", "   \n\t", None):
            with self.subTest(body=body):
                result = parse_shopify_inbox_email(body)
                self.assertIsNone(result["customer_name"])
                self.assertIsNone(result["customer_email"])
                self.assertIsNone(result["inquiry_text"])
                self.assertEqual(result["parse_confidence"], "none")

    def test_garbage_does_not_raise(self) -> None:
        result = parse_shopify_inbox_email(".... !!!")
        self.assertIsNone(result["customer_email"])
        self.assertIsNone(result["inquiry_text"])


if __name__ == "__main__":
    unittest.main()
