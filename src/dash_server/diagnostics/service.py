"""Structured diagnostics, log capture, and traceback parsing."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..timestamps import seconds_since


def hashlib_sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()


class DiagnosticsService:
    """Persist per-app diagnostics data in a small filesystem-backed store."""

    _frame_re = re.compile(r'^\s*File "([^"]+)", line (\d+), in (.+)$')

    def __init__(self, root: str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def append_log(
        self,
        app_name: str,
        channel: str,
        message: str,
        *,
        level: str = "info",
        revision_number: int | None = None,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        entry = {
            "id": uuid.uuid4().hex,
            "timestamp": self._timestamp(),
            "channel": channel,
            "level": level,
            "message": message,
            "revision_number": revision_number,
            "data": data or {},
        }
        self._append_jsonl(self._app_dir(app_name) / f"{channel}.jsonl", entry)
        self._append_jsonl(self._app_dir(app_name) / "latest.jsonl", entry)
        return entry

    def emit_event(
        self,
        app_name: str,
        channel: str,
        event: str,
        message: str,
        *,
        level: str = "info",
        revision_number: int | None = None,
        **data: Any,
    ) -> None:
        """Best-effort structured event on ``channel``.

        Owns the ``data={"event": ..., **kwargs}`` convention so callers don't have to
        rebuild it (and don't have to wrap the call in a no-op-on-failure guard).
        Returns nothing — every byte that matters is on disk.
        """

        try:
            self.append_log(
                app_name,
                channel,
                message,
                level=level,
                revision_number=revision_number,
                data={"event": event, **data},
            )
        except Exception:
            # Audit/event logging is best-effort; never propagate.
            pass

    def tail_logs(self, app_name: str, *, channel: str = "latest", limit: int = 20) -> dict[str, Any]:
        entries = self._read_jsonl(self._app_dir(app_name) / f"{channel}.jsonl")
        bounded = entries[-limit:] if limit > 0 else entries
        return {"app": app_name, "channel": channel, "entries": bounded}

    def record_build_result(
        self,
        app_name: str,
        *,
        status: str,
        summary: str,
        revision_number: int | None = None,
        artifact_path: str | None = None,
        validation: dict[str, Any] | None = None,
        preflight: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = {
            "timestamp": self._timestamp(),
            "status": status,
            "summary": summary,
            "revision_number": revision_number,
            "artifact_path": artifact_path,
            "validation": validation,
            "preflight": preflight,
            "error": error,
        }
        self._write_json(self._app_dir(app_name) / "latest_build_result.json", payload)
        return payload

    def get_latest_build_result(self, app_name: str) -> dict[str, Any] | None:
        path = self._app_dir(app_name) / "latest_build_result.json"
        return self._read_json(path)

    def record_health_result(self, app_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        result = {"timestamp": self._timestamp(), **payload}
        self._write_json(self._app_dir(app_name) / "latest_health.json", result)
        return result

    def record_error(
        self,
        app_name: str,
        *,
        source: str,
        category: str,
        summary: str,
        details: dict[str, Any] | None = None,
        traceback_text: str | None = None,
        revision_number: int | None = None,
    ) -> dict[str, Any]:
        parsed_traceback = (
            self.inspect_traceback(traceback_text)["traceback"] if traceback_text else None
        )
        record = {
            "id": uuid.uuid4().hex,
            "timestamp": self._timestamp(),
            "source": source,
            "category": category,
            "summary": summary,
            "details": details or {},
            "traceback_text": traceback_text,
            "parsed_traceback": parsed_traceback,
            "revision_number": revision_number,
        }
        self._append_jsonl(self._app_dir(app_name) / "errors.jsonl", record)
        self.append_log(
            app_name,
            source,
            summary,
            level="error",
            revision_number=revision_number,
            data={"category": category, **(details or {})},
        )
        return record

    def record_callback_failure(
        self,
        app_name: str,
        *,
        summary: str,
        details: dict[str, Any] | None = None,
        traceback_text: str | None = None,
        revision_number: int | None = None,
    ) -> dict[str, Any]:
        record = self.record_error(
            app_name,
            source="runtime",
            category="dash_callback_error",
            summary=summary,
            details=details,
            traceback_text=traceback_text,
            revision_number=revision_number,
        )
        self._append_jsonl(self._app_dir(app_name) / "callback_failures.jsonl", record)
        return record

    def record_data_layer_error(
        self,
        app_name: str,
        *,
        sql_file: str,
        profile_name: str,
        error_text: str,
        revision_number: int | None = None,
        rate_limit_seconds: float = 60.0,
    ) -> dict[str, Any] | None:
        """Persist a data-layer (Exasol query) error with per-(app,sql_file,fingerprint) rate limiting.

        Stamps the active revision number on the record when provided, so probes and the
        ``dash://apps/{name}/errors`` resource can filter old-revision noise out after a
        promote/rollback. Returns the recorded entry, or None if the error was suppressed
        by rate-limit.
        """

        fingerprint = self._fingerprint(error_text)
        cache_path = self._app_dir(app_name) / "data_layer_errors_dedup.json"
        cache = self._read_json(cache_path) or {}
        key = f"{sql_file}::{fingerprint}"
        last_emitted = cache.get(key)
        now_ts = self._timestamp()
        now_dt = datetime.now(timezone.utc)
        if seconds_since(last_emitted, now_dt) < rate_limit_seconds:
            return None
        cache[key] = now_ts
        self._write_json(cache_path, cache)
        return self.record_error(
            app_name,
            source="data_layer",
            category="exasol_query_error",
            summary=f"Exasol query failed for {sql_file}.",
            details={
                "sql_file": sql_file,
                "profile": profile_name,
                "error": error_text,
                "fingerprint": fingerprint,
            },
            revision_number=revision_number,
        )

    def acknowledge_data_layer_errors(self, app_name: str) -> dict[str, Any]:
        """Mark every data-layer error recorded so far as acknowledged.

        Writes a watermark timestamp; `latest_data_layer_error_after_ack(app_name)` and
        the `data_layer` healthcheck probe both filter past it. The underlying
        ``errors.jsonl`` ledger is never modified — operators can still read the full
        history through ``dash://apps/{name}/errors?include_acknowledged=true`` once
        that parameter lands (today the resource returns the unfiltered list).
        """

        watermark_path = self._app_dir(app_name) / "data_layer_ack.json"
        now_ts = self._timestamp()
        payload = {"acknowledged_until": now_ts}
        self._write_json(watermark_path, payload)
        return payload

    def data_layer_ack_watermark(self, app_name: str) -> str | None:
        """Return the most recent ack timestamp for an app, or None if never acked."""

        record = self._read_json(self._app_dir(app_name) / "data_layer_ack.json")
        if not isinstance(record, dict):
            return None
        value = record.get("acknowledged_until")
        return value if isinstance(value, str) and value else None

    @staticmethod
    def _fingerprint(error_text: str) -> str:
        return hashlib_sha1(error_text.strip())[:16]

    def list_errors(self, app_name: str, *, limit: int = 20, source: str | None = None) -> dict[str, Any]:
        records = self._read_jsonl(self._app_dir(app_name) / "errors.jsonl")
        if source is not None:
            records = [record for record in records if record["source"] == source]
        bounded = records[-limit:] if limit > 0 else records
        return {"app": app_name, "errors": bounded}

    def list_callback_failures(self, app_name: str, *, limit: int = 20) -> dict[str, Any]:
        records = self._read_jsonl(self._app_dir(app_name) / "callback_failures.jsonl")
        bounded = records[-limit:] if limit > 0 else records
        return {"app": app_name, "callback_failures": bounded}

    def latest_error(
        self,
        app_name: str,
        *,
        source: str | None = None,
        revision_number: int | None = None,
    ) -> dict[str, Any] | None:
        records = self.list_errors(app_name, limit=0, source=source)["errors"]
        if revision_number is not None:
            records = [
                record for record in records if record.get("revision_number") == revision_number
            ]
        return records[-1] if records else None

    def inspect_traceback(self, traceback_text: str) -> dict[str, Any]:
        lines = [line.rstrip("\n") for line in traceback_text.splitlines() if line.strip()]
        frames: list[dict[str, Any]] = []
        index = 0
        while index < len(lines):
            frame_match = self._frame_re.match(lines[index])
            if frame_match:
                frame = {
                    "file": frame_match.group(1),
                    "line": int(frame_match.group(2)),
                    "function": frame_match.group(3).strip(),
                    "code": None,
                }
                if index + 1 < len(lines) and not self._frame_re.match(lines[index + 1]):
                    next_line = lines[index + 1].strip()
                    if next_line and not next_line.startswith("Traceback "):
                        frame["code"] = next_line
                frames.append(frame)
            index += 1

        exception_line = next(
            (line for line in reversed(lines) if not line.startswith("Traceback ")),
            "",
        )
        exception_type, message = self._split_exception_line(exception_line)
        category = self._classify_traceback(exception_type, message)
        return {
            "traceback": {
                "exception_type": exception_type,
                "message": message,
                "frames": frames,
                "category": category,
                "summary": exception_line or "No traceback details available.",
            }
        }

    def suggested_recovery_steps(self, category: str) -> list[str]:
        suggestions = {
            "manifest_error": [
                "Fix dash-app.json so it matches the expected manifest shape.",
                "Run app_validate again before app_build.",
            ],
            "syntax_error": [
                "Fix Python syntax errors in the draft workspace.",
                "Run app_validate before trying another build.",
            ],
            "import_error": [
                "Restore valid imports in app.py or requirements.txt.",
                "Run app_validate to confirm the import smoke check passes.",
            ],
            "dependency_conflict": [
                "Correct invalid or conflicting requirement specifiers in requirements.txt.",
                "Re-run app_validate before building again.",
            ],
            "environment_missing_dependency": [
                "Install the declared requirements for the current server environment, or enable automatic dependency installation.",
                "Re-run app_validate after dependencies are available to the import smoke check.",
            ],
            "route_misconfiguration": [
                "Update create_dash_app to mount at the provided url_base_pathname.",
                "Use routes_pathname_prefix='/' and requests_pathname_prefix=url_base_pathname.rstrip('/') + '/'.",
            ],
            "dash_layout_error": [
                "Inspect the layout factory for invalid component construction.",
                "Rebuild and probe the preview route after fixing the layout.",
            ],
            "dash_callback_error": [
                "Inspect the callback function and the referenced component ids.",
                "Re-run health checks after applying the fix.",
            ],
            "callback_validation_error": [
                "Update callback inputs, outputs, or layout ids so every referenced component exists in the mounted layout.",
                "Re-run app_validate before building or promoting the app again.",
            ],
            "exposure_routing_error": [
                "Check the live route, enable state, and route conflicts against other apps.",
                "Update exposure settings before promoting or publishing the app.",
            ],
            "permission_violation": [
                "Remove the unauthorized filesystem or module access from the app code.",
                "Re-run validation and health checks after the change.",
            ],
            "artifact_mismatch": [
                "Compare the current draft against the latest built artifact and rebuild from the draft before promoting anything live.",
                "If the traceback came from an older artifact, verify the failing file still matches the draft before patching the runtime path.",
            ],
            "runtime_crash": [
                "Inspect the latest runtime traceback and recent logs.",
                "Patch the failing code path, rebuild, and verify preview health before promotion.",
            ],
            "exasol_query_error": [
                "Inspect the failing SQL file (see `latest_data_layer_error.details.sql_file`) and the raw pyexasol error.",
                "Patch the SQL via app_patch_file, redeploy, and re-run app_run_healthcheck to confirm the sql_smoke probe passes.",
            ],
        }
        return suggestions.get(
            category,
            [
                "Inspect the latest error details and recent logs.",
                "Apply a focused patch, then validate and rebuild the app.",
            ],
        )

    def _classify_traceback(self, exception_type: str, message: str) -> str:
        combined = f"{exception_type} {message}".lower()
        if exception_type == "SyntaxError" or "syntaxerror" in combined:
            return "syntax_error"
        if exception_type in {"ImportError", "ModuleNotFoundError"} or "cannot import" in combined:
            return "import_error"
        if "version conflict" in combined or "distribution not found" in combined or "requirement" in combined:
            return "dependency_conflict"
        if "callback" in combined:
            return "dash_callback_error"
        if "layout" in combined:
            return "dash_layout_error"
        if "permission" in combined:
            return "permission_violation"
        if "address already in use" in combined:
            return "port_binding_failure"
        if "timeout" in combined:
            return "readiness_timeout"
        return "runtime_crash"

    def _split_exception_line(self, exception_line: str) -> tuple[str, str]:
        if not exception_line:
            return "UnknownError", ""
        if ": " in exception_line:
            exception_type, message = exception_line.split(": ", 1)
            return exception_type.strip(), message.strip()
        return exception_line.strip(), ""

    def _app_dir(self, app_name: str) -> Path:
        path = self.root / app_name
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _append_jsonl(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload))
            handle.write("\n")

    def _read_jsonl(self, path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        records: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                records.append(json.loads(line))
        return records

    def _write_json(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _read_json(self, path: Path) -> dict[str, Any] | None:
        if not path.exists():
            return None
        payload: Any = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return None
        return payload

    def _timestamp(self) -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
