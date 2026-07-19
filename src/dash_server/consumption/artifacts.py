"""Artifact storage adapters: the shared interface, the local filesystem
store used by the single-node coordinator, and an object-store reference
adapter that documents the hosted-production contract."""

from __future__ import annotations

import os
from contextlib import suppress
from pathlib import Path
import tempfile
from typing import Protocol

from dash_server.exceptions import DashServerError


class ArtifactStore(Protocol):
    """Contract every artifact backend must satisfy.

    `publish` must be atomic: a storage key becomes resolvable only after the
    complete artifact is durable, and a discarded temporary file never becomes
    visible. `resolve` returns a local filesystem path the web adapter can
    stream; remote backends materialize into a private cache first.
    """

    def temporary_path(self, job_id: str) -> Path: ...

    def publish(self, job_id: str, temporary_path: Path, filename: str) -> str: ...

    def discard(self, temporary_path: Path) -> None: ...

    def resolve(self, storage_key: str) -> Path: ...

    def delete(self, storage_key: str) -> None: ...


class LocalArtifactStore:
    def __init__(self, root: str | Path) -> None:
        self.root = (Path(root) / "consumption").resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def temporary_path(self, job_id: str) -> Path:
        job_root = self._job_root(job_id)
        job_root.mkdir(parents=True, exist_ok=True)
        fd, raw_path = tempfile.mkstemp(prefix="export-", suffix=".partial", dir=job_root)
        os.close(fd)
        return Path(raw_path)

    def publish(self, job_id: str, temporary_path: Path, filename: str) -> str:
        safe_name = Path(filename).name
        final_path = self._job_root(job_id) / safe_name
        os.replace(temporary_path, final_path)
        return str(final_path.relative_to(self.root))

    def discard(self, temporary_path: Path) -> None:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass

    def resolve(self, storage_key: str) -> Path:
        candidate = (self.root / storage_key).resolve()
        if self.root not in candidate.parents or not candidate.is_file():
            raise DashServerError(
                category="consumption_artifact_not_found",
                summary="The export artifact is unavailable.",
                details={},
                jsonrpc_code=-32004,
                http_status=404,
            )
        return candidate

    def delete(self, storage_key: str) -> None:
        try:
            candidate = (self.root / storage_key).resolve()
            if self.root in candidate.parents:
                candidate.unlink(missing_ok=True)
                with suppress(OSError):
                    candidate.parent.rmdir()
        except OSError:
            pass

    def _job_root(self, job_id: str) -> Path:
        if not job_id or any(character not in "0123456789abcdef-" for character in job_id):
            raise ValueError("Invalid consumption job id.")
        return self.root / job_id


class ObjectClient(Protocol):
    """Minimal object-store client surface (S3/GCS-shaped) the reference adapter needs."""

    def put_object(self, key: str, data: bytes) -> None: ...

    def get_object(self, key: str) -> bytes: ...

    def delete_object(self, key: str) -> None: ...

    def has_object(self, key: str) -> bool: ...


class InMemoryObjectClient:
    """Dict-backed ObjectClient for interface tests and local experiments."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put_object(self, key: str, data: bytes) -> None:
        self.objects[key] = data

    def get_object(self, key: str) -> bytes:
        return self.objects[key]

    def delete_object(self, key: str) -> None:
        self.objects.pop(key, None)

    def has_object(self, key: str) -> bool:
        return key in self.objects


class ObjectStoreArtifactStore:
    """Reference `ArtifactStore` over an object client.

    Temporary files stay local; `publish` uploads the finished bytes and only
    then removes the local staging file, so a crashed upload publishes
    nothing. `resolve` materializes the object into a private cache directory
    for the streaming download response. This adapter documents the Phase 6
    production contract; it is not wired into configuration yet.
    """

    def __init__(self, client: ObjectClient, cache_root: str | Path) -> None:
        self.client = client
        self.cache_root = (Path(cache_root) / "consumption-cache").resolve()
        self.cache_root.mkdir(parents=True, exist_ok=True)

    def temporary_path(self, job_id: str) -> Path:
        staging = self.cache_root / "staging" / _safe_job_id(job_id)
        staging.mkdir(parents=True, exist_ok=True)
        fd, raw_path = tempfile.mkstemp(prefix="export-", suffix=".partial", dir=staging)
        os.close(fd)
        return Path(raw_path)

    def publish(self, job_id: str, temporary_path: Path, filename: str) -> str:
        storage_key = f"{_safe_job_id(job_id)}/{Path(filename).name}"
        self.client.put_object(storage_key, temporary_path.read_bytes())
        temporary_path.unlink(missing_ok=True)
        return storage_key

    def discard(self, temporary_path: Path) -> None:
        with suppress(OSError):
            temporary_path.unlink(missing_ok=True)

    def resolve(self, storage_key: str) -> Path:
        if not self.client.has_object(storage_key):
            raise DashServerError(
                category="consumption_artifact_not_found",
                summary="The export artifact is unavailable.",
                details={},
                jsonrpc_code=-32004,
                http_status=404,
            )
        cached = (self.cache_root / storage_key).resolve()
        if self.cache_root not in cached.parents:
            raise DashServerError(
                category="consumption_artifact_not_found",
                summary="The export artifact is unavailable.",
                details={},
                jsonrpc_code=-32004,
                http_status=404,
            )
        cached.parent.mkdir(parents=True, exist_ok=True)
        cached.write_bytes(self.client.get_object(storage_key))
        return cached

    def delete(self, storage_key: str) -> None:
        self.client.delete_object(storage_key)
        with suppress(OSError):
            cached = (self.cache_root / storage_key).resolve()
            if self.cache_root in cached.parents:
                cached.unlink(missing_ok=True)
                cached.parent.rmdir()


def _safe_job_id(job_id: str) -> str:
    if not job_id or any(character not in "0123456789abcdef-" for character in job_id):
        raise ValueError("Invalid consumption job id.")
    return job_id


__all__ = [
    "ArtifactStore",
    "InMemoryObjectClient",
    "LocalArtifactStore",
    "ObjectClient",
    "ObjectStoreArtifactStore",
]
