"""AgentCore HTTP adapter for the Where Are We Eating reservation agent.

AgentCore owns the HTTP lifecycle; the existing application owns agent
behavior.  This adapter deliberately accepts only a prompt and organizer ID
and returns the agent's structured text result without exposing the FastAPI
application or browser internals.
"""

from __future__ import annotations

import json
import os
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


def _add_source_path() -> None:
    """Make the repository package importable in both Docker and local dev."""
    source_file = Path(__file__).resolve()
    candidates = [Path("/app/src")]
    candidates.extend(parent / "src" for parent in source_file.parents)
    for candidate in candidates:
        if candidate.is_dir():
            sys.path.insert(0, str(candidate))
            return


_add_source_path()

from groupreservations.opentable_mcp import run  # noqa: E402


def _request_values(payload: Any) -> tuple[str | None, str]:
    """Extract the AgentCore prompt and organizer identity from JSON input."""
    if isinstance(payload, str):
        return payload, "agentcore-organizer"
    if not isinstance(payload, dict):
        return None, "agentcore-organizer"

    prompt = payload.get("prompt")
    if prompt is None and isinstance(payload.get("input"), dict):
        prompt = payload["input"].get("prompt")
    user_id = payload.get("user_id") or payload.get("organizer_id")
    return (prompt if isinstance(prompt, str) else None), str(
        user_id or "agentcore-organizer"
    )


class AgentCoreHandler(BaseHTTPRequestHandler):
    """Minimal HTTP protocol implementation expected by AgentCore Runtime."""

    server_version = "WAWEAgent/1.0"

    def _write_json(self, status: HTTPStatus, body: dict[str, Any]) -> None:
        encoded = json.dumps(body, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/") == "/ping":
            self._write_json(HTTPStatus.OK, {"status": "healthy"})
            return
        self._write_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path.rstrip("/") != "/invocations":
            self._write_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            self._write_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_json"})
            return

        prompt, user_id = _request_values(payload)
        if not prompt or not prompt.strip():
            self._write_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "prompt_required", "expected": "a non-empty prompt string"},
            )
            return

        try:
            result = run(prompt.strip(), user_id=user_id)
        except Exception as exc:  # AgentCore should receive a useful 500 response.
            self._write_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "agent_failed", "type": type(exc).__name__},
            )
            return

        self._write_json(HTTPStatus.OK, {"result": result})

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"[waweagent] {fmt % args}", flush=True)


def main() -> None:
    port = int(os.getenv("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), AgentCoreHandler)
    print(f"[waweagent] listening on 0.0.0.0:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
