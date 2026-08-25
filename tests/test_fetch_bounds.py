"""What bounds one tick: the deadline, the redirect limit and the size cap.

`fetch_timeout` is a wall-clock deadline for the whole tick, not a timeout on
each socket operation. Left to `urllib`, it is the latter, and two shapes of
response walk straight past it: a redirect chain, because
`HTTPRedirectHandler` re-arms `timeout` on every one of its ten permitted
hops, and a slow-drip body, because the socket timeout restarts on each
`recv()`. Both block the poller thread — or, in on-demand mode, a request
thread. The 1 MiB cap is the third bound, enforced *while* the body streams so
that an oversized document never reaches the heap whole.

These mirror cru-flags-ruby's fetcher tests (that client's PR #7). Each case
drives exactly one synchronous tick via `refresh(force=True)`, so the elapsed
time it asserts on is the fetch itself and nothing else.
"""

from __future__ import annotations

import itertools
import json
import time
import urllib.error
from collections.abc import Callable
from typing import Any

import pytest

from cru_flags import Client
from cru_flags._client import _MAX_BODY_BYTES, _MAX_REDIRECTS

from .conftest import ErrorRecorder, FlagService, RecordedRequest, Response

ClientFactory = Callable[..., Client]

#: A poll interval long enough that nothing but the tick under test runs.
IDLE = 60.0

#: Generous enough that only the size cap can end the tick, never the clock.
UNHURRIED = 10.0


def one_tick(client: Client) -> float:
    """Run exactly one fetch on this thread and return how long it blocked."""
    started = time.monotonic()
    client.refresh(force=True)
    return time.monotonic() - started


def bounded_client(
    make_client: ClientFactory,
    service: FlagService,
    errors: ErrorRecorder,
    fetch_timeout: float,
) -> Client:
    """An on-demand client, so one tick is one synchronous, timeable call."""
    client: Client = make_client(
        url=service.url,
        poll_seconds=IDLE,
        fetch_timeout=fetch_timeout,
        refresh_mode="on-demand",
        on_error=errors,
    )
    return client


# ── the deadline covers the whole tick ────────────────────────────────────


def test_a_redirect_chain_cannot_outlast_the_fetch_timeout(
    service: FlagService,
    make_client: ClientFactory,
    errors: ErrorRecorder,
) -> None:
    # Every hop redirects onward to a fresh path after a delay just under the
    # budget. Handed to `urllib`, each hop gets its own full `timeout`, so
    # this ran to the ten-redirect ceiling — measured at 5.43s against a 2.0s
    # timeout — before the deadline was shared across the chain.
    timeout = 0.5
    paths = itertools.count()
    service.respond_with(
        lambda _request: Response(
            302,
            headers={"Location": f"/hop{next(paths)}"},
            delay=timeout * 0.8,
        )
    )
    client = bounded_client(make_client, service, errors, timeout)

    elapsed = one_tick(client)

    assert elapsed < timeout * 3, f"one tick blocked for {elapsed:.2f}s"
    assert client.enabled("pilot_banner") is False
    assert len(errors.calls) == 1
    assert isinstance(errors.calls[0], OSError)


def test_a_slow_drip_body_cannot_outlast_the_fetch_timeout(
    service: FlagService,
    make_client: ClientFactory,
    errors: ErrorRecorder,
) -> None:
    # No single `recv()` ever stalls for as long as the timeout, so the socket
    # timeout never fires and the read runs to EOF: 4.8s of blocking against a
    # 1.0s timeout when measured. Only a deadline checked between chunks ends
    # it. The body is valid JSON, so nothing but the clock can fail this tick.
    timeout = 0.5
    body = b'{"Flags": {}}' + b" " * 4_000
    service.respond_with(
        lambda _request: Response(
            200,
            body,
            headers={"Content-Type": "application/json"},
            chunk_size=len(body) // 12,
            chunk_delay=timeout * 0.6,
        )
    )
    client = bounded_client(make_client, service, errors, timeout)

    elapsed = one_tick(client)

    assert elapsed < timeout * 3, f"one tick blocked for {elapsed:.2f}s"
    assert service.bytes_written < len(body), "the whole drip was read anyway"
    assert client.snapshot() == {}
    assert len(errors.calls) == 1
    assert isinstance(errors.calls[0], OSError)


# ── redirects: followed, but counted and validated ────────────────────────


def test_redirects_are_followed_to_a_limit_of_three(
    service: FlagService,
    make_client: ClientFactory,
    document: dict[str, Any],
    errors: ErrorRecorder,
) -> None:
    body = json.dumps(document).encode()

    def responder(_request: RecordedRequest) -> Response:
        hop = len(service.requests)
        if hop <= _MAX_REDIRECTS:
            return Response(302, headers={"Location": f"/hop{hop}"})
        return Response(200, body, headers={"Content-Type": "application/json"})

    service.respond_with(responder)
    client = bounded_client(make_client, service, errors, UNHURRIED)

    one_tick(client)

    assert client.enabled("pilot_banner") is True
    assert len(service.requests) == _MAX_REDIRECTS + 1
    assert errors.calls == []


def test_a_fourth_redirect_fails_the_tick(
    service: FlagService,
    make_client: ClientFactory,
    errors: ErrorRecorder,
) -> None:
    paths = itertools.count()
    service.respond_with(
        lambda _request: Response(302, headers={"Location": f"/hop{next(paths)}"})
    )
    client = bounded_client(make_client, service, errors, UNHURRIED)

    one_tick(client)

    assert len(service.requests) == _MAX_REDIRECTS + 1, "the limit is not a limit"
    assert client.enabled("pilot_banner") is False
    assert len(errors.calls) == 1
    error = errors.calls[0]
    assert isinstance(error, urllib.error.URLError)
    assert "redirect" in str(error)


@pytest.mark.parametrize(
    "location",
    [
        "ftp://example.com/flags",
        "file:///etc/passwd",
        "data:application/json,%7B%7D",
        # Hostless, and `urljoin` leaves it that way because the scheme
        # differs from the document's. (A hostless `http:///flags` is *not*
        # in this list: `urljoin` resolves a same-scheme reference against
        # the base, which repairs it into an ordinary same-host URL. The
        # netloc check below is the floor under whatever `urljoin` returns,
        # not a restatement of it.)
        "https:///flags",
    ],
)
def test_a_redirect_to_an_unusable_url_is_never_followed(
    location: str,
    service: FlagService,
    make_client: ClientFactory,
    errors: ErrorRecorder,
) -> None:
    # A `Location` is attacker-reachable input. `file://` and `data:` are URLs
    # too, and an opener that has handlers for them will happily oblige.
    service.respond_with(lambda _request: Response(302, headers={"Location": location}))
    client = bounded_client(make_client, service, errors, UNHURRIED)

    one_tick(client)

    assert len(service.requests) == 1, "the redirect was followed"
    assert client.enabled("pilot_banner") is False
    assert len(errors.calls) == 1
    assert isinstance(errors.calls[0], urllib.error.URLError)


# ── the body is capped as it streams ──────────────────────────────────────


def test_an_oversized_document_fails_the_tick(
    service: FlagService,
    make_client: ClientFactory,
    errors: ErrorRecorder,
) -> None:
    # Valid JSON that merely exceeds the cap, so the size is provably what
    # failed the tick rather than a coincidental parse error.
    body = b'{"Flags": {}}' + b" " * _MAX_BODY_BYTES
    service.respond_with(
        lambda _request: Response(
            200, body, headers={"Content-Type": "application/json"}
        )
    )
    client = bounded_client(make_client, service, errors, UNHURRIED)

    one_tick(client)

    assert client.snapshot() == {}
    assert len(errors.calls) == 1
    error = errors.calls[0]
    assert isinstance(error, ValueError)
    assert str(_MAX_BODY_BYTES) in str(error)


def test_an_oversized_body_is_capped_while_it_streams(
    service: FlagService,
    make_client: ClientFactory,
    errors: ErrorRecorder,
) -> None:
    # The cap has to bite during the read, not after it: a check on an
    # already-buffered body lets a hostile or misconfigured endpoint push
    # unbounded bytes into the process before anyone objects. Visible here
    # because the harness reports how much it got onto the wire before the
    # client hung up.
    body = b"x" * (_MAX_BODY_BYTES * 4)
    service.respond_with(
        lambda _request: Response(
            200,
            body,
            headers={"Content-Type": "application/json"},
            chunk_size=65_536,
            chunk_delay=0.005,
        )
    )
    client = bounded_client(make_client, service, errors, UNHURRIED)

    one_tick(client)

    assert client.snapshot() == {}
    assert len(errors.calls) == 1
    assert isinstance(errors.calls[0], ValueError)
    assert service.bytes_written < len(body), (
        "the client buffered the whole oversized payload instead of aborting at the cap"
    )


@pytest.mark.parametrize("status", [404, 500])
def test_an_oversized_error_body_keeps_its_own_outcome(
    status: int,
    service: FlagService,
    make_client: ClientFactory,
    errors: ErrorRecorder,
) -> None:
    # Only a 200 body is the document. A huge 404 body must still mean "no
    # document published yet" and a huge 500 must still report its status —
    # the cap must not relabel outcomes the client makes real decisions on —
    # and neither body should be read into the process at all.
    body = b"x" * (_MAX_BODY_BYTES * 4)
    service.respond_with(
        lambda _request: Response(status, body, chunk_size=65_536, chunk_delay=0.005)
    )
    client = bounded_client(make_client, service, errors, UNHURRIED)

    one_tick(client)

    assert client.snapshot() == {}
    if status == 404:
        assert errors.calls == [], "404 is data, not an error"
    else:
        assert len(errors.calls) == 1
        error = errors.calls[0]
        assert isinstance(error, urllib.error.HTTPError)
        assert error.code == status
    assert service.bytes_written < len(body), "an error body was read into the process"
