"""Shared fixtures: a real, programmable HTTP flag service on 127.0.0.1.

A real server rather than a monkeypatched ``urlopen`` because the behaviour
under test *is* HTTP behaviour: ``urllib`` surfaces ``304 Not Modified`` as a
raised ``HTTPError``, echoes request headers with its own capitalisation, and
closes bodies on its own schedule. A mock would encode our guesses about all
three. See ``docs/design.md`` §8.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from cru_flags import Client

LIVE_DOCUMENT: Mapping[str, Any] = {
    # Captured verbatim from the live service, minus flags added for the
    # cases the contract calls out (see the `document` fixture).
    "Project": "ararat",
    "Environment": "release-candidate",
    "Version": 3,
    "NotifySlack": True,
    "Flags": {
        "pilot_banner": {
            "Enabled": True,
            "Description": "Pilot: flag-gated banner proving the flag service "
            "end-to-end (ararat#198)",
            "CreatedAt": "2026-07-31T14:09:01.119Z",
            "UpdatedAt": "2026-07-31T14:09:08.777Z",
            "UpdatedBy": "Omicron7",
        },
        "checkout_v2": {"Enabled": False, "Description": "Off for now"},
        # Malformed shapes the client must read as "off".
        "no_enabled_key": {"Description": "Enabled is missing entirely"},
        "enabled_is_a_string": {"Enabled": "true"},
        "enabled_is_a_number": {"Enabled": 1},
        "enabled_is_null": {"Enabled": None},
    },
}


@dataclass(frozen=True)
class RecordedRequest:
    """One inbound request. Header names are lowercased for lookup sanity."""

    path: str
    headers: Mapping[str, str]


@dataclass
class Response:
    """A scripted response.

    ``delay`` holds the response back before a byte of it is sent. The body
    then goes out in one write unless ``chunk_size`` is set, in which case it
    is dripped out in chunks that wide with ``chunk_delay`` seconds between
    them — the shape a slow service has on the wire. ``content_length``
    advertises a length other than the body's own, which is how a response
    claims to be far larger than anything it will actually send.
    """

    status: int = 200
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)
    delay: float = 0.0
    content_length: int | None = None
    chunk_size: int = 0
    chunk_delay: float = 0.0


Responder = Callable[[RecordedRequest], Response]

_NOT_MODIFIED = 304


class FlagService:
    """A local HTTP server that serves scripted flag documents."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self.requests: list[RecordedRequest] = []
        self.statuses: list[int] = []
        self.bytes_written = 0
        self._responder: Responder = lambda _request: Response(404, b"{}")
        service = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # BaseHTTPRequestHandler's naming, not ours
                headers = {key.lower(): value for key, value in self.headers.items()}
                response = service._dispatch(RecordedRequest(self.path, headers))
                self.send_response(response.status)
                for name, value in response.headers.items():
                    self.send_header(name, value)
                body = b"" if response.status == _NOT_MODIFIED else response.body
                if response.status != _NOT_MODIFIED:
                    declared = response.content_length
                    self.send_header(
                        "Content-Length",
                        str(len(body) if declared is None else declared),
                    )
                self.end_headers()
                if body:
                    service._write_body(self.wfile, body, response)

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                """Silence the default stderr request log."""

        class Server(ThreadingHTTPServer):
            def handle_error(self, request: Any, client_address: Any) -> None:
                """Stay quiet when a client hangs up: tests do that on purpose."""

        self._server = Server(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="flag-service",
            daemon=True,
        )
        self._thread.start()

    @property
    def url(self) -> str:
        """The flag document URL, shaped like the real service's."""
        return (
            f"http://127.0.0.1:{self._server.server_port}"
            f"/flags/ararat/release-candidate"
        )

    def respond_with(self, responder: Responder) -> None:
        """Install a responder for subsequent requests."""
        with self._condition:
            self._responder = responder

    def serve_document(
        self,
        document: Mapping[str, Any],
        etag: str = '"1"',
        delay: float = 0.0,
    ) -> None:
        """Serve `document` with `etag`, answering 304 to a matching request."""
        body = json.dumps(document).encode()

        def responder(request: RecordedRequest) -> Response:
            if request.headers.get("if-none-match") == etag:
                return Response(_NOT_MODIFIED, headers={"ETag": etag}, delay=delay)
            return Response(
                200,
                body,
                headers={"ETag": etag, "Content-Type": "application/json"},
                delay=delay,
            )

        self.respond_with(responder)

    def serve_status(self, status: int, body: bytes = b"{}") -> None:
        """Serve a fixed status and body for every request."""
        self.respond_with(lambda _request: Response(status, body))

    def wait_for_polls(self, count: int, timeout: float = 5.0) -> bool:
        """Block until `count` requests have been answered."""
        deadline = time.monotonic() + timeout
        with self._condition:
            while len(self.statuses) < count:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True

    def stop(self) -> None:
        """Shut the server down."""
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(5.0)

    def _write_body(self, stream: Any, body: bytes, response: Response) -> None:
        """Write `body`, dripping it out in delayed chunks when asked to.

        Records how much actually reached the socket, which is how a test
        proves the client hung up early instead of swallowing the whole
        payload. A client that aborts mid-body breaks the pipe; that is the
        expected outcome here, not an error.
        """
        width = response.chunk_size or len(body)
        try:
            for start in range(0, len(body), width):
                chunk = body[start : start + width]
                stream.write(chunk)
                stream.flush()
                with self._condition:
                    self.bytes_written += len(chunk)
                if response.chunk_delay:
                    time.sleep(response.chunk_delay)
        except OSError:
            return

    def _dispatch(self, request: RecordedRequest) -> Response:
        with self._condition:
            responder = self._responder
            self.requests.append(request)
        response = responder(request)
        if response.delay:
            time.sleep(response.delay)
        with self._condition:
            self.statuses.append(response.status)
            self._condition.notify_all()
        return response


class ErrorRecorder:
    """An injectable ``on_error`` that records every call."""

    def __init__(self) -> None:
        self.calls: list[BaseException | None] = []
        self._condition = threading.Condition()

    def __call__(self, error: BaseException | None) -> None:
        with self._condition:
            self.calls.append(error)
            self._condition.notify_all()

    def wait_for(self, count: int, timeout: float = 5.0) -> bool:
        """Block until `count` calls have been recorded."""
        deadline = time.monotonic() + timeout
        with self._condition:
            while len(self.calls) < count:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never inherit the library's configuration from the developer's shell."""
    monkeypatch.delenv("CRU_FLAGS_URL", raising=False)
    monkeypatch.delenv("CRU_FLAGS_REFRESH_MODE", raising=False)


@pytest.fixture
def document() -> dict[str, Any]:
    """A fresh, mutable copy of the live-shaped flag document."""
    copy: dict[str, Any] = json.loads(json.dumps(LIVE_DOCUMENT))
    return copy


@pytest.fixture
def service() -> Iterator[FlagService]:
    """A running local flag service."""
    service = FlagService()
    try:
        yield service
    finally:
        service.stop()


@pytest.fixture
def make_client() -> Iterator[Callable[..., Client]]:
    """A Client factory that closes every client it created."""
    created: list[Client] = []

    def factory(**kwargs: Any) -> Client:
        client = Client(**kwargs)
        created.append(client)
        return client

    try:
        yield factory
    finally:
        for client in created:
            client.close()


@pytest.fixture
def errors() -> ErrorRecorder:
    """An ``on_error`` recorder."""
    return ErrorRecorder()
