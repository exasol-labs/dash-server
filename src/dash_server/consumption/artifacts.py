"""Local atomic artifact storage for Phase 1 exports."""

from __future__ import annotations

import os
from contextlib import suppress
from pathlib import Path
import tempfile

from dash_server.exceptions import DashServerError


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


__all__ = ["LocalArtifactStore"]
