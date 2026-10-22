"""pyexasol ``fetch_mapper`` callables for result sets that need Python-typed values.

Without a mapper, pyexasol returns DECIMAL(p,s), DECIMAL(p,0) beyond 18 digits, DATE,
and TIMESTAMP values as the strings Exasol sends over the websocket. That is what the
interactive dashboard path and CSV exports rely on. Typed consumers (XLSX export)
opt in to ``typed_fetch_mapper`` per connection instead.
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
    strings: pyexasol 3.0's interval parser expects a 9-digit day field and raises on
    Exasol's default ``INTERVAL DAY(2) TO SECOND`` rendering. A value that doesn't parse
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


__all__ = ["typed_fetch_mapper"]
