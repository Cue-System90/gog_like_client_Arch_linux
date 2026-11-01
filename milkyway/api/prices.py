"""Preise aus dem GOG-Katalog (``finalMoney``/``baseMoney``) und der Batch-Preis-API (``"1999 EUR"``): Cent-Beträge,
Rabatt in Prozent und die Anzeige je Währung und Sprache der Oberfläche („17,99 €“ bzw. „€17.99“). Ohne GTK und Netz."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

NBSP = " "
# Währungszeichen (Codes ohne eigenes Zeichen erscheinen als Code)
SYMBOLS = {"EUR": "€", "USD": "$", "GBP": "£", "PLN": "zł", "RUB": "₽", "AUD": "A$", "CAD": "C$", "CHF": "CHF",
           "NOK": "kr", "SEK": "kr", "DKK": "kr", "BRL": "R$", "CNY": "¥", "TRY": "₺", "NZD": "NZ$", "MXN": "MX$",
           "UAH": "₴", "KRW": "₩", "JPY": "¥", "CZK": "Kč", "HUF": "Ft"}
NO_DECIMALS = {"JPY", "KRW"}


@dataclass(frozen=True)
class Price:
    final: int          # Cent
    base: int           # Cent (ohne Rabatt)
    currency: str

    @property
    def discount(self) -> int:
        """Rabatt in ganzen Prozent (0 ohne Rabatt)."""
        if self.base <= 0 or self.final >= self.base:
            return 0
        return int((Decimal(self.base - self.final) * 100 / Decimal(self.base)).quantize(Decimal(1), ROUND_HALF_UP))

    @property
    def free(self) -> bool:
        return self.final <= 0

    def to_dict(self) -> dict:
        return {"final": self.final, "base": self.base, "currency": self.currency}

    @classmethod
    def from_dict(cls, d: Any) -> "Price | None":
        if not isinstance(d, dict):
            return None
        try:
            return cls(int(d["final"]), int(d["base"]), str(d.get("currency") or ""))
        except (KeyError, TypeError, ValueError):
            return None


def parse_money(value: Any) -> int | None:
    """Betrag in Währungseinheiten ("17.99", 17.99, "17,99") → Cent; None bei Unlesbarem."""
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip().replace(NBSP, "").replace(" ", "")
    if "," in text and "." not in text:
        text = text.replace(",", ".")
    try:
        cents = (Decimal(text) * 100).quantize(Decimal(1), ROUND_HALF_UP)
    except InvalidOperation:
        return None
    return int(cents) if cents >= 0 else None


def parse_cents(value: Any) -> tuple[int, str] | None:
    """Batch-Preis-API: ``"1999 EUR"`` → (1999, "EUR")."""
    if not isinstance(value, str):
        return None
    parts = value.split()
    if not parts or not parts[0].lstrip("-").isdigit():
        return None
    cents = int(parts[0])
    if cents < 0:
        return None
    return cents, (parts[1].upper() if len(parts) > 1 else "")


def catalog_price(price: Any) -> Price | None:
    """``price`` eines Katalog-Produkts → Price; None bei „Demnächst“ (kein Preis) oder unlesbaren Angaben."""
    if not isinstance(price, dict):
        return None
    fm, bm = price.get("finalMoney"), price.get("baseMoney")
    if not isinstance(fm, dict):
        return None
    final = parse_money(fm.get("amount"))
    if final is None:
        return None
    base = parse_money(bm.get("amount")) if isinstance(bm, dict) else None
    currency = str(fm.get("currency") or (bm or {}).get("currency") or "").upper()
    return Price(final, base if base is not None and base >= final else final, currency)


def format_amount(cents: int, currency: str, lang: str) -> str:
    """Betrag wie im Store der jeweiligen Sprache: Deutsch „1.299,99 €“, sonst „€1,299.99“ bzw. „CHF 12.00“."""
    cur = (currency or "").upper()
    sym = SYMBOLS.get(cur, cur)
    digits = 0 if cur in NO_DECIMALS else 2
    value = Decimal(int(cents)) / 100
    number = f"{value:,.{digits}f}"
    if lang == "de":
        number = number.replace(",", "\x00").replace(".", ",").replace("\x00", ".")
        return f"{number}{NBSP}{sym}".strip()
    if not sym:
        return number
    return f"{sym}{NBSP}{number}" if sym[-1].isalpha() else f"{sym}{number}"


def format_discount(percent: int) -> str:
    """Rabatt-Pille wie im Store: ``-40%`` ("" ohne Rabatt)."""
    return f"-{int(percent)}%" if percent and percent > 0 else ""
