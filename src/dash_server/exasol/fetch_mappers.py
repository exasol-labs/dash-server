"""pyexasol ``fetch_mapper`` callables for result sets that need Python-typed values.

Without a mapper, pyexasol returns DECIMAL(p,s), DECIMAL(p,0) beyond 18 digits, DATE,
and TIMESTAMP values as the strings Exasol sends over the websocket. CSV exports keep
that text on purpose. XLSX exports use ``typed_fetch_mapper``; hosted dashboards use
``dashboard_fetch_mapper``, which also keeps numbers a browser can't hold exactly as text.
"""

from __future__ import annotations

from collections.abc import Mapping
import decimal
from typing import Any

from pyexasol.data_types import WebSocketDataType, convert_websocket_to_python

_CONVERTED_TYPES = frozenset(
    data_type.value
    for data_type in (
        WebSocketDataType.Decimal,
        WebSocketDataType.Date,
        WebSocketDataType.Timestamp,
        WebSocketDataType.TimestampTz,
    )
)


def typed_fetch_mapper(value: Any, data_type: Mapping[str, Any]) -> Any:
    """Convert DECIMAL/DATE/TIMESTAMP values to ``int``/``Decimal``/``date``/``datetime``.

    Everything else passes through unchanged. In particular INTERVAL values stay
    strings: JSON, and so a Dash callback, has no duration type, and Exasol's own
    rendering (``+02 03:04:05.500``) reads better than a converted number. A value that doesn't parse
    (for example a session with a non-ISO ``NLS_DATE_FORMAT``) also stays the raw
    string, so an unusual session format degrades to text instead of failing the export.
    TIMESTAMP precision beyond microseconds is truncated, as Python's ``datetime`` can't
    hold it.
    """

    if value is None or data_type.get("type") not in _CONVERTED_TYPES:
        return value
    try:
        return convert_websocket_to_python(value, data_type)
    except (ValueError, TypeError, decimal.InvalidOperation):
        return value


# A browser (and Excel) stores numbers as IEEE doubles: 15 significant digits are exact.
_EXACT_SIGNIFICANT_DIGITS = 15


def dashboard_fetch_mapper(value: Any, data_type: Mapping[str, Any]) -> Any:
    """``typed_fetch_mapper`` for values bound for a hosted dashboard.

    Dash serializes callback output to JSON, and the browser parses every JSON number
    as a double. An integer or decimal with more than 15 significant digits (IDs from
    ``DECIMAL(36,0)``, say) would be silently rounded on screen, so it stays the exact
    text Exasol sent instead.
    """

    converted = typed_fetch_mapper(value, data_type)
    if isinstance(converted, bool):
        return converted
    if isinstance(converted, int) and abs(converted) >= 10**_EXACT_SIGNIFICANT_DIGITS:
        return str(value)
    if isinstance(converted, decimal.Decimal) and len(converted.normalize().as_tuple().digits) > _EXACT_SIGNIFICANT_DIGITS:
        return str(value)
    return converted


__all__ = ["dashboard_fetch_mapper", "typed_fetch_mapper"]
