"""Currency and text helpers shared by the report and the card renderer."""

from __future__ import annotations

from decimal import Decimal


def money(amount: float, currency: str = "KES") -> str:
    """12000 -> 'KES 12,000' ; 1250.5 -> 'KES 1,250.50'."""
    value = Decimal(str(amount or 0))
    whole = int(value)
    cents = (value - whole).quantize(Decimal("0.01"))
    formatted = f"{whole:,}"
    if cents > 0:
        formatted = f"{formatted}.{int(cents * 100):02d}"
    code = (currency or "").strip().upper()
    return f"{code} {formatted}".strip()


def stock_label(quantity: int) -> str:
    if quantity <= 0:
        return "OUT OF STOCK"
    if quantity == 1:
        return "IN STOCK · 1 UNIT"
    if quantity <= 3:
        return f"IN STOCK · ONLY {quantity} LEFT"
    return f"IN STOCK · {quantity} UNITS"


def clean_phone(number: str) -> str:
    return " ".join((number or "").split())


def truncate(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"
