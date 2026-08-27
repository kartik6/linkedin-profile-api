"""Read the several date shapes LinkedIn uses and render them for humans."""

from __future__ import annotations

from datetime import date as _date
from typing import Any

from app.models import Date, DateRange

_MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)


def _month_name(month: int | None) -> str | None:
    if month and 1 <= month <= 12:
        return _MONTHS[month - 1]
    return None


def render_date(d: Date | None) -> str | None:
    if d is None or d.year is None:
        return None
    name = _month_name(d.month)
    return f"{name} {d.year}" if name else str(d.year)


def parse_date(node: Any) -> Date | None:
    """Accept {"year":2021,"month":5,"day":1} or a plain year."""
    if node is None:
        return None
    if isinstance(node, int):
        return Date(year=node, text=str(node))
    if not isinstance(node, dict):
        return None
    year, month, day = node.get("year"), node.get("month"), node.get("day")
    if year is None and month is None and day is None:
        return None
    d = Date(year=year, month=month, day=day)
    d.text = render_date(d)
    return d


def months_between(start: Date | None, end: Date | None, is_current: bool) -> int | None:
    """Count whole months. An open range counts up to today."""
    if start is None or start.year is None:
        return None
    s_month = start.month or 1
    if end and end.year:
        e_year, e_month = end.year, end.month or 12
    elif is_current:
        today = _date.today()
        e_year, e_month = today.year, today.month
    else:
        return None
    total = (e_year - start.year) * 12 + (e_month - s_month) + 1
    return max(total, 0)


def parse_date_range(node: Any, *, current_label: str = "Present") -> DateRange | None:
    """Accept {"start":..,"end":..} or the legacy {"startDate":..,"endDate":..}."""
    if not isinstance(node, dict):
        return None

    start = parse_date(node.get("start") or node.get("startDate"))
    end = parse_date(node.get("end") or node.get("endDate"))

    if start is None and end is None:
        return None

    is_current = end is None
    dr = DateRange(
        start=start,
        end=end,
        is_current=is_current,
        duration_months=months_between(start, end, is_current),
    )

    left = render_date(start) or ""
    right = render_date(end) or (current_label if is_current else "")
    dr.text = f"{left} - {right}".strip(" -") or None
    return dr
