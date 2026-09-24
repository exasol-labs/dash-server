"""Parsing helpers for pyexasol connection strings (DSNs)."""

from __future__ import annotations

import ipaddress
import re

# One comma-separated DSN part, per pyexasol >= 2.4.1's grammar (``_parse_dsn``): host (no
# ``:`` or ``/``), optional ``N..M`` range, optional ``/FINGERPRINT`` (hex or ``nocertcheck``)
# *before* the optional ``:port``. pyexasol rejects anything else at connect time, including
# ``host:port/FINGERPRINT`` and bracketed IPv6 such as ``[::1]:8563``.
_DSN_PART_RE = re.compile(
    r"^(?P<host>[^:/]+?)"
    r"(?:(?P<range_start>\d+)\.\.(?P<range_end>\d+)(?P<host_suffix>[^:/]*?))?"
    r"(?:/(?P<fingerprint>[0-9A-Fa-f]+|nocertcheck))?"
    r"(?::(?P<port>\d+)?)?$",
    re.IGNORECASE,
)


def dsn_parse_error(dsn: str) -> str | None:
    """Return why pyexasol would reject ``dsn`` (phrased to follow "dsn "), or ``None``."""

    parts = [part for part in dsn.split(",") if part]
    if not parts:
        return "is empty."
    for part in parts:
        if _DSN_PART_RE.match(part):
            continue
        if re.search(r":\d+/", part):
            return (
                f"part {part!r}: the certificate fingerprint must come before the port "
                "(host/FINGERPRINT:port), not after it."
            )
        return (
            f"part {part!r} is not valid for pyexasol. Expected host[/FINGERPRINT][:port], "
            "comma-separated for multiple hosts; IPv6 literals are not supported."
        )
    return None


def dsn_fingerprint(dsn: str) -> str | None:
    """Return the pinned certificate fingerprint in ``dsn`` (hex, upper-case), if any.

    ``nocertcheck`` is not a pin and yields ``None``.
    """

    for part in dsn.split(","):
        match = _DSN_PART_RE.match(part)
        fingerprint = match.group("fingerprint") if match else None
        if fingerprint and fingerprint.lower() != "nocertcheck":
            return fingerprint.upper()
    return None


def dsn_is_loopback_only(dsn: str) -> bool:
    """Return whether every host in a pyexasol DSN is a loopback address.

    Loopback means ``localhost`` or an IPv4 ``127.0.0.0/8`` address. Ranges
    (``host1..3``) and anything unparseable count as non-loopback.
    """

    parts = [part for part in dsn.split(",") if part]
    if not parts:
        return False
    for part in parts:
        match = _DSN_PART_RE.match(part)
        if match is None or match.group("range_start") is not None:
            return False
        host = match.group("host")
        if host.lower() == "localhost":
            continue
        try:
            if not ipaddress.ip_address(host).is_loopback:
                return False
        except ValueError:
            return False
    return True


def default_tls_verify(dsn: str) -> bool:
    """``tls_verify`` to use when the caller did not choose one.

    Off for loopback-only DSNs, on otherwise. Loopback traffic never leaves the host, so
    certificate verification buys no protection there, while local Exasol installs
    (Exasol Personal, Nano, Community Edition) ship self-signed certificates that make
    ``tls_verify=true`` fail outright. Remote clusters keep verification by default.
    """

    return not dsn_is_loopback_only(dsn)
