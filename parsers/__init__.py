"""Channel parsers for the omnichannel lead triage engine."""

from .shopify_email_parser import parse_shopify_inbox_email

__all__ = ["parse_shopify_inbox_email"]
