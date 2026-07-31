"""The behavioural contract from docs/design.md, one test per line of it."""

from __future__ import annotations

import logging
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

import pytest

from cru_flags import Client, __version__

from .conftest import ErrorRecorder, FlagService, RecordedRequest, Response

POLL = 0.02
WAIT = 5.0

ClientFactory = Callable[..., Client]


# ── inert with no configuration ───────────────────────────────────────────


def test_unset_url_is_inert(
    make_client: ClientFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    threads_before = threading.active_count()
    client = make_client()

    with caplog.at_level(logging.DEBUG, logger="cru_flags"):
        assert client.enabled("pilot_banner") is False
        assert client.ready(timeout=0.01) is False
        assert client.snapshot() == {}

    assert client._thread is None
    assert threading.active_count() == threads_before
    assert caplog.records == []


@pytest.mark.parametrize("value", ["", "   ", "\n"])
def test_blank_url_is_inert(
    value: str,
    monkeypatch: pytest.MonkeyPatch,
    make_client: ClientFactory,
    errors: ErrorRecorder,
) -> None:
    monkeypatch.setenv("CRU_FLAGS_URL", value)
    client = make_client(on_error=errors)

    assert client.enabled("pilot_banner") is False
    assert client._thread is None
    assert errors.calls == []


def test_non_http_url_is_inert_and_reports_once(
    monkeypatch: pytest.MonkeyPatch,
    make_client: ClientFactory,
    errors: ErrorRecorder,
) -> None:
    monkeypatch.setenv("CRU_FLAGS_URL", "file:///etc/passwd")
    client = make_client(on_error=errors)

    assert client.enabled("pilot_banner") is False
    assert client.ready(timeout=0.01) is False
    assert client._thread is None
    assert len(errors.calls) == 1
    assert isinstance(errors.calls[0], ValueError)


# ── lazy start, daemon thread ─────────────────────────────────────────────


def test_constructing_a_client_starts_no_thread(
    service: FlagService,
    document: dict[str, Any],
    make_client: ClientFactory,
) -> None:
    service.serve_document(document)
    threads_before = threading.active_count()

    make_client(url=service.url, poll_seconds=POLL)

    assert threading.active_count() == threads_before


def test_the_poller_is_a_lazily_started_daemon_thread(
    service: FlagService,
    document: dict[str, Any],
    make_client: ClientFactory,
) -> None:
    service.serve_document(document)
    client = make_client(url=service.url, poll_seconds=POLL)

    assert client.ready(WAIT) is True

    thread = client._thread
    assert thread is not None
    assert thread.daemon is True, "the poller must never delay interpreter exit"
    assert thread.name == "cru-flags-poller"
    assert thread.is_alive() is True


def test_environment_is_read_on_first_use_not_construction(
    monkeypatch: pytest.MonkeyPatch,
    service: FlagService,
    document: dict[str, Any],
    make_client: ClientFactory,
) -> None:
    client = make_client(poll_seconds=POLL)  # constructed while CRU_FLAGS_URL is unset
    service.serve_document(document)
    monkeypatch.setenv("CRU_FLAGS_URL", service.url)

    assert client.ready(WAIT) is True
    assert client.enabled("pilot_banner") is True


def test_explicit_url_beats_the_environment(
    monkeypatch: pytest.MonkeyPatch,
    service: FlagService,
    document: dict[str, Any],
    make_client: ClientFactory,
) -> None:
    monkeypatch.setenv("CRU_FLAGS_URL", "http://127.0.0.1:1/never-used")
    service.serve_document(document)
    client = make_client(url=service.url, poll_seconds=POLL)

    assert client.ready(WAIT) is True
    assert client.enabled("pilot_banner") is True


# ── enabled() semantics ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("pilot_banner", True),
        ("checkout_v2", False),
        ("never_created", False),
        ("no_enabled_key", False),
        ("enabled_is_a_string", False),
        ("enabled_is_a_number", False),
        ("enabled_is_null", False),
        ("", False),
    ],
)
def test_enabled_reads_the_document(
    name: str,
    expected: bool,  # noqa: FBT001 - parametrised expectation, not a flag argument
    service: FlagService,
    document: dict[str, Any],
    make_client: ClientFactory,
) -> None:
    service.serve_document(document)
    client = make_client(url=service.url, poll_seconds=POLL)

    assert client.ready(WAIT) is True
    assert client.enabled(name) is expected


def test_unknown_document_keys_are_ignored(
    service: FlagService,
    document: dict[str, Any],
    make_client: ClientFactory,
    errors: ErrorRecorder,
) -> None:
    document["SomeFutureKey"] = {"nested": ["additive", "change"]}
    document["Flags"]["pilot_banner"]["SomeFutureFlagKey"] = 42
    service.serve_document(document)
    client = make_client(url=service.url, poll_seconds=POLL, on_error=errors)

    assert client.ready(WAIT) is True
    assert client.enabled("pilot_banner") is True
    assert errors.calls == []


# ── ready() ───────────────────────────────────────────────────────────────


def test_ready_returns_false_while_the_first_attempt_is_outstanding(
    service: FlagService,
    document: dict[str, Any],
    make_client: ClientFactory,
) -> None:
    service.serve_document(document, delay=0.5)
    client = make_client(url=service.url, poll_seconds=POLL, fetch_timeout=WAIT)

    assert client.ready(timeout=0.05) is False
    assert client.ready(timeout=WAIT) is True
    assert client.enabled("pilot_banner") is True


def test_ready_is_true_after_a_failed_first_attempt(
    service: FlagService,
    make_client: ClientFactory,
    errors: ErrorRecorder,
) -> None:
    service.serve_status(500, b"boom")
    client = make_client(url=service.url, poll_seconds=POLL, on_error=errors)

    assert client.ready(WAIT) is True
    assert client.enabled("pilot_banner") is False


# ── fail-static ───────────────────────────────────────────────────────────


def test_everything_is_false_before_the_first_success(
    service: FlagService,
    make_client: ClientFactory,
    errors: ErrorRecorder,
) -> None:
    service.serve_status(503, b"unavailable")
    client = make_client(url=service.url, poll_seconds=POLL, on_error=errors)

    assert client.ready(WAIT) is True
    assert client.enabled("pilot_banner") is False
    assert client.snapshot() == {}
    assert errors.wait_for(1, WAIT) is True


def test_last_known_good_persists_through_failures(
    service: FlagService,
    document: dict[str, Any],
    make_client: ClientFactory,
    errors: ErrorRecorder,
) -> None:
    service.serve_document(document, etag='"3"')
    client = make_client(url=service.url, poll_seconds=POLL, on_error=errors)
    assert client.ready(WAIT) is True
    assert client.enabled("pilot_banner") is True

    service.serve_status(503, b"unavailable")
    assert errors.wait_for(1, WAIT) is True
    polls = len(service.statuses)
    assert service.wait_for_polls(polls + 5, WAIT) is True

    # No TTL: the last published document stays in force indefinitely.
    assert client.enabled("pilot_banner") is True
    assert client.snapshot()["Version"] == 3
    # ...and the failure was reported exactly once, not once per poll.
    assert len(errors.calls) == 1
    assert isinstance(errors.calls[0], urllib.error.HTTPError)


def test_recovery_is_reported_once(
    service: FlagService,
    document: dict[str, Any],
    make_client: ClientFactory,
    errors: ErrorRecorder,
) -> None:
    service.serve_status(503, b"unavailable")
    client = make_client(url=service.url, poll_seconds=POLL, on_error=errors)
    assert client.ready(WAIT) is True
    assert errors.wait_for(1, WAIT) is True

    document["Version"] = 4
    service.serve_document(document, etag='"4"')
    assert errors.wait_for(2, WAIT) is True

    polls = len(service.statuses)
    assert service.wait_for_polls(polls + 5, WAIT) is True

    assert errors.calls[1] is None, "recovery is reported with None"
    assert len(errors.calls) == 2, "no per-poll chatter once healthy"
    assert client.enabled("pilot_banner") is True


@pytest.mark.parametrize("body", [b"not json at all", b"[]", b'"a string"', b"null"])
def test_a_malformed_document_keeps_the_previous_snapshot(
    body: bytes,
    service: FlagService,
    document: dict[str, Any],
    make_client: ClientFactory,
    errors: ErrorRecorder,
) -> None:
    service.serve_document(document)
    client = make_client(url=service.url, poll_seconds=POLL, on_error=errors)
    assert client.ready(WAIT) is True
    assert client.enabled("pilot_banner") is True

    service.serve_status(200, body)
    assert errors.wait_for(1, WAIT) is True

    assert client.enabled("pilot_banner") is True
    assert errors.calls[0] is not None


def test_the_default_handler_warns_once_per_transition(
    service: FlagService,
    document: dict[str, Any],
    make_client: ClientFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    service.serve_status(503, b"unavailable")

    with caplog.at_level(logging.WARNING, logger="cru_flags"):
        client = make_client(url=service.url, poll_seconds=POLL)
        assert client.ready(WAIT) is True
        polls = len(service.statuses)
        assert service.wait_for_polls(polls + 5, WAIT) is True

        def ours() -> list[logging.LogRecord]:
            return [r for r in caplog.records if r.name == "cru_flags"]

        assert len(ours()) == 1
        assert "serving last known flags" in ours()[0].getMessage()
        assert "HTTPError" in ours()[0].getMessage()

        service.serve_document(document)
        deadline = time.monotonic() + WAIT
        while len(ours()) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)

    messages = [record.getMessage() for record in ours()]
    assert len(messages) == 2, messages
    assert "recovered" in messages[1]


def test_a_broken_on_error_handler_cannot_kill_the_poller(
    service: FlagService,
    document: dict[str, Any],
    make_client: ClientFactory,
) -> None:
    def explode(_error: BaseException | None) -> None:
        message = "the caller's error handler is broken"
        raise RuntimeError(message)

    service.serve_status(503, b"unavailable")
    client = make_client(url=service.url, poll_seconds=POLL, on_error=explode)
    assert client.ready(WAIT) is True

    service.serve_document(document)
    deadline = time.monotonic() + WAIT
    while not client.enabled("pilot_banner") and time.monotonic() < deadline:
        time.sleep(0.01)
    assert client.enabled("pilot_banner") is True


# ── conditional requests ──────────────────────────────────────────────────


def test_conditional_requests_and_304_keep_the_snapshot(
    service: FlagService,
    document: dict[str, Any],
    make_client: ClientFactory,
    errors: ErrorRecorder,
) -> None:
    service.serve_document(document, etag='"3"')
    client = make_client(url=service.url, poll_seconds=POLL, on_error=errors)

    assert client.ready(WAIT) is True
    assert service.wait_for_polls(4, WAIT) is True

    first, *rest = list(service.requests)
    assert "if-none-match" not in first.headers, "nothing to revalidate yet"
    assert [request.headers.get("if-none-match") for request in rest] == ['"3"'] * len(
        rest
    )
    assert service.statuses[0] == 200
    assert set(service.statuses[1:4]) == {304}

    assert client.enabled("pilot_banner") is True
    assert client.snapshot()["Version"] == 3
    assert errors.calls == []


def test_a_new_etag_replaces_the_snapshot(
    service: FlagService,
    document: dict[str, Any],
    make_client: ClientFactory,
) -> None:
    service.serve_document(document, etag='"3"')
    client = make_client(url=service.url, poll_seconds=POLL)
    assert client.ready(WAIT) is True
    assert client.enabled("checkout_v2") is False

    document["Version"] = 4
    document["Flags"]["checkout_v2"]["Enabled"] = True
    service.serve_document(document, etag='"4"')

    deadline = time.monotonic() + WAIT
    while not client.enabled("checkout_v2") and time.monotonic() < deadline:
        time.sleep(0.01)
    assert client.enabled("checkout_v2") is True
    assert client.snapshot()["Version"] == 4


def test_request_headers_identify_the_client(
    service: FlagService,
    document: dict[str, Any],
    make_client: ClientFactory,
) -> None:
    service.serve_document(document)
    client = make_client(url=service.url, poll_seconds=POLL)
    assert client.ready(WAIT) is True

    headers = service.requests[0].headers
    assert headers["accept"] == "application/json"
    assert headers["user-agent"] == f"cru-flags-python/{__version__}"


# ── 404 is data, not an error ─────────────────────────────────────────────


def test_404_is_no_document_yet_not_an_error(
    service: FlagService,
    make_client: ClientFactory,
    errors: ErrorRecorder,
    caplog: pytest.LogCaptureFixture,
) -> None:
    service.serve_status(404, b'{"message": "ararat has no feature flags yet."}')

    with caplog.at_level(logging.DEBUG, logger="cru_flags"):
        client = make_client(url=service.url, poll_seconds=POLL, on_error=errors)
        assert client.ready(WAIT) is True
        assert service.wait_for_polls(3, WAIT) is True

    assert client.enabled("pilot_banner") is False
    assert client.snapshot() == {}
    assert errors.calls == []
    assert caplog.records == []


def test_404_after_a_success_empties_the_snapshot(
    service: FlagService,
    document: dict[str, Any],
    make_client: ClientFactory,
    errors: ErrorRecorder,
) -> None:
    service.serve_document(document, etag='"3"')
    client = make_client(url=service.url, poll_seconds=POLL, on_error=errors)
    assert client.ready(WAIT) is True
    assert client.enabled("pilot_banner") is True

    service.serve_status(404, b'{"message": "all flags deleted"}')
    deadline = time.monotonic() + WAIT
    while client.enabled("pilot_banner") and time.monotonic() < deadline:
        time.sleep(0.01)

    assert client.enabled("pilot_banner") is False
    assert client.snapshot() == {}
    assert errors.calls == []
    # The stored ETag is cleared with the document, so the next request is
    # unconditional rather than revalidating a document we no longer hold.
    assert client._etag is None


def test_400_is_an_error(
    service: FlagService,
    make_client: ClientFactory,
    errors: ErrorRecorder,
) -> None:
    # The real service answers 400 when the URL names an environment that
    # does not exist — a configuration mistake someone needs to see.
    service.serve_status(400, b'{"message": "\\"staging\\" has no feature flags."}')
    client = make_client(url=service.url, poll_seconds=POLL, on_error=errors)

    assert client.ready(WAIT) is True
    assert errors.wait_for(1, WAIT) is True
    error = errors.calls[0]
    assert isinstance(error, urllib.error.HTTPError)
    assert error.code == 400


# ── how we call urllib ────────────────────────────────────────────────────


def test_one_attempt_per_poll_with_the_configured_timeout(
    monkeypatch: pytest.MonkeyPatch,
    make_client: ClientFactory,
    errors: ErrorRecorder,
) -> None:
    calls: list[tuple[str, Any]] = []

    def fake_urlopen(request: urllib.request.Request, timeout: Any = None) -> Any:
        calls.append((request.full_url, timeout))
        message = "no route to anywhere"
        raise urllib.error.URLError(message)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    # A long poll interval means only the first tick runs before we assert.
    client = make_client(
        url="http://127.0.0.1:9/flags",
        poll_seconds=60.0,
        fetch_timeout=0.25,
        on_error=errors,
    )
    assert client.ready(WAIT) is True
    assert errors.wait_for(1, WAIT) is True

    assert calls == [("http://127.0.0.1:9/flags", 0.25)], "no retries within a tick"


def test_a_timeout_is_a_failure_not_a_crash(
    service: FlagService,
    make_client: ClientFactory,
    errors: ErrorRecorder,
) -> None:
    service.respond_with(lambda _request: Response(200, b"{}", delay=0.3))
    client = make_client(
        url=service.url,
        poll_seconds=1.0,
        fetch_timeout=0.05,
        on_error=errors,
    )

    assert client.ready(WAIT) is True
    assert client.enabled("pilot_banner") is False
    assert errors.wait_for(1, WAIT) is True
    assert isinstance(errors.calls[0], OSError)


# ── jitter ────────────────────────────────────────────────────────────────


def test_the_poll_interval_is_jittered_by_20_percent(
    make_client: ClientFactory,
) -> None:
    client = make_client(url="http://127.0.0.1:9/flags", poll_seconds=30.0)
    samples = [client._next_interval() for _ in range(1000)]

    assert min(samples) >= 24.0
    assert max(samples) <= 36.0
    # The spread is real, not a constant: co-deployed pods must de-phase.
    assert min(samples) < 25.0
    assert max(samples) > 35.0


# ── close() ───────────────────────────────────────────────────────────────


def test_close_stops_the_poller(
    service: FlagService,
    document: dict[str, Any],
    make_client: ClientFactory,
) -> None:
    service.serve_document(document)
    client = make_client(url=service.url, poll_seconds=POLL)
    assert client.ready(WAIT) is True

    client.close()
    polls = len(service.statuses)
    time.sleep(POLL * 10)

    assert len(service.statuses) == polls, "no polling after close()"
    thread = client._thread
    assert thread is not None
    assert thread.is_alive() is False
    # The last snapshot stays readable, and reads still never raise.
    assert client.enabled("pilot_banner") is True


def test_close_before_the_first_lookup_leaves_the_client_inert(
    service: FlagService,
    document: dict[str, Any],
    make_client: ClientFactory,
) -> None:
    service.serve_document(document)
    client = make_client(url=service.url, poll_seconds=POLL)

    client.close()

    assert client.enabled("pilot_banner") is False
    # No poller was ever started, so ready() must answer rather than block
    # forever on an event nobody will ever set.
    assert client.ready(timeout=0.05) is False
    assert client._thread is None
    assert service.requests == []


def test_close_on_an_inert_client_is_a_no_op(make_client: ClientFactory) -> None:
    client = make_client()
    client.close()
    client.close()
    assert client.enabled("pilot_banner") is False


# ── the read path never raises ────────────────────────────────────────────


def test_reads_never_raise_even_with_a_corrupt_snapshot(
    make_client: ClientFactory,
) -> None:
    client = make_client()
    assert client.enabled("pilot_banner") is False

    # Force the impossible: a snapshot that is not a mapping at all. The
    # read path must still answer instead of propagating.
    client._snapshot = "not a document"  # type: ignore[assignment]
    assert client.enabled("pilot_banner") is False
    assert client.snapshot() == {}


def test_reads_never_raise_when_the_flags_section_is_wrong(
    make_client: ClientFactory,
) -> None:
    client = make_client()
    client._publish({"Flags": ["not", "a", "mapping"]}, etag=None)
    assert client.enabled("pilot_banner") is False

    client._publish({"Flags": {"pilot_banner": "not a mapping"}}, etag=None)
    assert client.enabled("pilot_banner") is False


def test_recorded_request_shape(service: FlagService) -> None:
    # Guards the fixture itself: lowercased header keys are what the header
    # assertions above rely on.
    service.serve_status(404)
    request = urllib.request.Request(service.url, headers={"X-Test": "yes"})
    with pytest.raises(urllib.error.HTTPError) as raised:
        urllib.request.urlopen(request, timeout=WAIT)
    raised.value.close()

    recorded: RecordedRequest = service.requests[0]
    assert recorded.path.endswith("/flags/ararat/release-candidate")
    assert recorded.headers["x-test"] == "yes"
