"""Jinja filters for rendering fields.

Every field on every page goes through `display`, so nulls are handled in
one place.
"""

from __future__ import annotations

from typing import Any

from jinja2 import Environment, Undefined
from markupsafe import Markup, escape

MISSING = Markup('<span class="missing">Not provided</span>')


def display(value: Any) -> Markup:
    """None, Jinja Undefined, empty string, and empty list/dict all become 'Not provided'.

    Undefined shows up when a parent object is null, e.g. `(e.department or {}).name`.
    """
    if value is None or isinstance(value, Undefined):
        return MISSING
    if isinstance(value, str) and not value.strip():
        return MISSING
    if isinstance(value, (list, dict)) and not value:
        return MISSING
    if isinstance(value, bool):
        return Markup("Yes" if value else "No")
    if isinstance(value, str):
        # Finch enums are snake_case: full_time -> Full time
        return Markup(escape(value.replace("_", " ").capitalize())) if value.islower() and "_" in value else Markup(escape(value))
    return Markup(escape(str(value)))


def money(income: dict[str, Any] | None) -> Markup:
    """Finch sends income.amount in cents. Show dollars plus the pay unit."""
    if not income or income.get("amount") is None:
        return MISSING
    amount = income["amount"] / 100
    currency = (income.get("currency") or "USD").upper()
    unit = income.get("unit")
    unit_text = f" / {unit.replace('_', ' ')}" if unit else ""
    return Markup(escape(f"{currency} {amount:,.2f}{unit_text}"))


def address(loc: dict[str, Any] | None) -> Markup:
    if not loc:
        return MISSING
    parts = [loc.get("line1"), loc.get("line2"),
             ", ".join(p for p in (loc.get("city"), loc.get("state")) if p),
             loc.get("postal_code"), loc.get("country")]
    text = ", ".join(p for p in parts if p)
    return Markup(escape(text)) if text else MISSING


def missing_count(obj: dict[str, Any] | None, keys: list[str]) -> int:
    """Count how many of the given keys are null or empty."""
    if not obj:
        return len(keys)
    return sum(1 for k in keys if obj.get(k) in (None, "", [], {}))


def register_filters(env: Environment) -> None:
    env.filters["display"] = display
    env.filters["money"] = money
    env.filters["address"] = address
    env.filters["missing_count"] = missing_count
