"""Typed boundary for the parts of pyexasol we depend on.

pyexasol 2.2 ships a `py.typed` marker, and `ExaConnection.execute(...) -> ExaStatement`
is now well-typed. What is *not* typed yet (as of pyexasol 2.2.1) is the consumer
surface on `ExaStatement` — `fetchall`, `fetchone`, `columns`, `column_names`,
`description`, `close`. mypy sees those as untyped methods and treats the return
values as `Any`, which defeats the point of strict-checking our use of them.

We work around this by declaring `ExaStatementLike` / `ExaConnectionLike` Protocols
that describe what *we* actually use, and using those Protocols in our service
signatures. Two upsides:

1. Where we annotate `statement: ExaStatementLike` instead of `Any`, mypy
   type-checks every call into the result-set surface — `statement.fetchall()`
   returns `Iterable[Sequence[Any]]`, not `Any`.
2. If a future pyexasol release tightens its annotations, our Protocols will
   structurally match the real `ExaStatement` class without code changes; we
   only have to delete the Protocol declarations.

Keep this file focused on what we use. Add a method only when a real call site
needs it — speculative coverage is a maintenance trap.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable
from collections.abc import Mapping, Sequence


@runtime_checkable
class ExaStatementLike(Protocol):
    """Subset of `pyexasol.ExaStatement` we read from.

    `runtime_checkable` so `isinstance(obj, ExaStatementLike)` works for test doubles
    that aren't subclasses of the real `ExaStatement`. Don't lean on it for nominal
    typing — it's a smoke check, not a guarantee.
    """

    def fetchall(self) -> Sequence[Sequence[Any]]: ...
    def close(self) -> None: ...

    # `columns()` is a method (pyexasol >= 0.9) but historically it was a property.
    # `column_names()` is the modern accessor returning a flat list of strings.
    # Both are present on real `ExaStatement`; our `_extract_columns` probes each.
    def columns(self) -> Mapping[str, Any] | Sequence[str]: ...
    def column_names(self) -> Sequence[str]: ...


@runtime_checkable
class ExaConnectionLike(Protocol):
    """Subset of `pyexasol.ExaConnection` we read from.

    `execute()` returns the protocol-typed statement so consumers don't have to
    re-narrow at every call site.
    """

    def execute(
        self,
        query: str,
        query_params: Mapping[str, Any] | None = ...,
    ) -> ExaStatementLike: ...

    def close(self) -> None: ...


# Imports at the bottom so the Protocol declarations above don't pull pyexasol into
# the import graph for modules that only use the type aliases.
__all__ = ["ExaConnectionLike", "ExaStatementLike"]
