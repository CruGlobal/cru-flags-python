"""Polling feature-flag client.

Implementation of the specification in ``docs/design.md``. Nothing in this
module is public API except what ``cru_flags/__init__.py`` re-exports.
"""

from __future__ import annotations

import contextlib
import functools
import http.client
import json
import logging
import os
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _distribution_version
from types import MappingProxyType
from typing import Any, Final, Literal, TypeAlias, get_args

try:
    __version__ = _distribution_version("cru-flags")
except PackageNotFoundError:  # pragma: no cover - source tree without install
    __version__ = "0.0.0+unknown"

#: Environment variable naming the flag document to poll.
ENV_VAR: Final = "CRU_FLAGS_URL"

#: Environment variable selecting the refresh mode, for deployments that need
#: on-demand refresh without a code change.
MODE_ENV_VAR: Final = "CRU_FLAGS_REFRESH_MODE"

#: Logger used by the default ``on_error`` handler.
LOGGER_NAME: Final = "cru_flags"

OnError: TypeAlias = Callable[[BaseException | None], None]
"""Health-transition callback.

Called with the offending exception when polling starts failing, and with
``None`` when polling recovers. Never called per-poll while a failure
persists. See ``docs/design.md`` §3.7.
"""

RefreshMode: TypeAlias = Literal["background", "on-demand"]
"""How the snapshot is refreshed.

``"background"`` (the default) polls on a daemon thread. ``"on-demand"``
starts no thread and refreshes synchronously on the calling thread when the
snapshot has aged past ``poll_seconds``. See ``docs/design.md`` §5.1.
"""

_REFRESH_MODES: Final = frozenset(get_args(RefreshMode))
_DEFAULT_REFRESH_MODE: Final[RefreshMode] = "background"

_LOGGER: Final = logging.getLogger(LOGGER_NAME)
_USER_AGENT: Final = f"cru-flags-python/{__version__}"
_THREAD_NAME: Final = "cru-flags-poller"

#: Fraction of the poll interval to jitter by, in each direction.
_JITTER: Final = 0.2

#: ``urllib`` will happily open ``file://``; we will not.
_ALLOWED_SCHEMES: Final = frozenset({"http", "https"})

_HTTP_NOT_MODIFIED: Final = 304
_HTTP_NOT_FOUND: Final = 404

#: Statuses that name another URL to fetch the document from.
_REDIRECT_STATUSES: Final = frozenset({301, 302, 303, 307, 308})

#: Redirect hops followed within one tick, matching cru-flags-ruby.
_MAX_REDIRECTS: Final = 3

#: Hard cap on the flag document, matching cru-flags-ruby's MAX_BODY_BYTES.
#: The real document is a few kilobytes; a megabyte is already absurd.
_MAX_BODY_BYTES: Final = 1_048_576

#: Ceiling on each ``read1`` while the cap and the deadline are checked.
_READ_CHUNK_BYTES: Final = 65_536

_EMPTY_SNAPSHOT: Final[Mapping[str, Any]] = MappingProxyType({})

#: How long ``close()`` waits for the poller to notice the stop signal.
_CLOSE_TIMEOUT: Final = 5.0


def _freeze(value: object) -> object:
    """Return an immutable equivalent of a JSON-decoded value."""
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _freeze_document(document: dict[str, Any]) -> Mapping[str, Any]:
    """Return an immutable, deeply frozen view of a decoded flag document."""
    return MappingProxyType({key: _freeze(value) for key, value in document.items()})


def _thaw(value: object) -> object:
    """Return a plain, mutable, JSON-serializable copy of a frozen value."""
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _thaw_document(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Return a plain, deep copy of a frozen flag document."""
    return {key: _thaw(value) for key, value in snapshot.items()}


def _log_transition(error: BaseException | None) -> None:
    """Log a polling health transition; the default ``on_error`` handler."""
    if error is None:
        _LOGGER.warning("cru_flags: flag refresh recovered; snapshot is fresh again")
    else:
        _LOGGER.warning(
            "cru_flags: flag refresh failing, serving last known flags (%s: %s)",
            type(error).__name__,
            error,
        )


@functools.cache
def _opener() -> urllib.request.OpenerDirector:
    """Build the process-wide opener: http(s) only, and no redirect handler.

    ``urlopen``'s default opener follows redirects itself, and
    ``HTTPRedirectHandler`` hands each of its ten permitted hops a *fresh*
    ``timeout`` — so a redirect chain multiplies the tick's nominal bound
    instead of sharing it. Without that handler urllib surfaces a ``3xx`` as
    an ``HTTPError``, exactly as it already does a ``404``, and ``_fetch``
    follows the chain itself under one deadline and one hop limit.

    Dropping the file, ftp and data handlers along with it means a redirect
    to ``file:///etc/passwd`` has nothing to open even if the scheme check in
    ``_redirect_target`` were ever wrong.

    Built on the first fetch rather than at import: constructing
    ``HTTPSHandler`` builds an SSL context and ``ProxyHandler`` reads the
    environment, and import must do neither (``docs/design.md`` §5).
    """
    opener = urllib.request.OpenerDirector()
    for handler in (
        urllib.request.ProxyHandler(),
        urllib.request.UnknownHandler(),
        urllib.request.HTTPHandler(),
        urllib.request.HTTPSHandler(),
        urllib.request.HTTPDefaultErrorHandler(),
        urllib.request.HTTPErrorProcessor(),
    ):
        opener.add_handler(handler)
    return opener


def _time_left(deadline: float) -> float:
    """Return the seconds left before `deadline`, or raise if it has passed."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        message = "flag fetch exceeded its fetch_timeout"
        raise TimeoutError(message)
    return remaining


def _redirect_target(current: str, location: str | None) -> str:
    """Resolve and validate one hop's ``Location`` against the current URL."""
    if location is None or not location.strip():
        message = "flag fetch was redirected without a Location header"
        raise urllib.error.URLError(message)
    target = urllib.parse.urljoin(current, location.strip())
    parts = urllib.parse.urlsplit(target)
    if parts.scheme.lower() not in _ALLOWED_SCHEMES or not parts.netloc:
        # A Location header is whatever the far end says it is. `file://` and
        # `data:` are URLs too, so this is checked before the URL is opened.
        message = (
            f"flag fetch was redirected to an unusable URL "
            f"(scheme {parts.scheme!r}, host {parts.hostname!r})"
        )
        raise urllib.error.URLError(message)
    return target


def _read_capped_body(response: http.client.HTTPResponse, deadline: float) -> bytes:
    """Read the body under both the size cap and the tick's deadline.

    Both bounds have to be checked *during* the read. A cap applied to an
    already-buffered body lets a hostile or misconfigured endpoint push
    unbounded bytes into the process before anyone objects, and the socket
    timeout restarts on every ``recv()``, so a body dripped out slowly enough
    never trips it at all while still taking arbitrarily long.

    Overrunning either one fails the tick, which is what the caller wants: a
    truncated document must never be parsed.

    ``read1``, not ``read``: ``read`` keeps pulling until it has the full
    amount asked for (or the whole declared body, whichever is smaller), so a
    dripped response comes back in a single call that returns only once the
    last byte has arrived — leaving nowhere to check the deadline from.
    ``read1`` makes at most one underlying recv, which is what puts a check
    between every chunk.
    """
    chunks: list[bytes] = []
    read = 0
    while True:
        _time_left(deadline)
        chunk = response.read1(_READ_CHUNK_BYTES)
        if not chunk:
            return b"".join(chunks)
        read += len(chunk)
        if read > _MAX_BODY_BYTES:
            message = f"flag document exceeds {_MAX_BODY_BYTES} bytes"
            raise ValueError(message)
        chunks.append(chunk)


class Client:
    """A feature-flag client backed by one polled JSON document.

    Reads its URL from the ``CRU_FLAGS_URL`` environment variable unless one
    is passed explicitly. The background poller starts on the first
    :meth:`enabled` or :meth:`ready` call — never at construction — and is a
    daemon thread, so it never delays interpreter shutdown.

    With no URL configured the client is *inert*: every flag is ``False``, no
    thread is started, no socket is opened and nothing is logged.

    With ``refresh_mode="on-demand"`` there is no thread at all: the refresh
    happens on the reading thread, when the snapshot is older than
    ``poll_seconds`` — trading "never blocks" for correctness on scale-to-zero
    runtimes. See ``docs/design.md`` §5.1.
    """

    def __init__(
        self,
        url: str | None = None,
        poll_seconds: float = 30.0,
        fetch_timeout: float = 2.0,
        on_error: OnError | None = None,
        refresh_mode: RefreshMode | None = None,
    ) -> None:
        """Create a client.

        ``url`` defaults to the ``CRU_FLAGS_URL`` environment variable, read
        once on first use. ``poll_seconds`` is the refresh interval, jittered
        by ±20% in background mode and used as a minimum snapshot age in
        on-demand mode. ``fetch_timeout`` is a wall-clock deadline for the
        whole refresh — redirect hops and the body read included, DNS
        excepted (``docs/design.md`` §7) — and there are no retries within
        one. ``on_error`` is called on
        health transitions only, and defaults to a warning on the
        ``cru_flags`` logger. ``refresh_mode`` selects background polling or
        synchronous on-demand refresh; ``None`` (the default) reads
        ``CRU_FLAGS_REFRESH_MODE`` on first use and falls back to background.

        Raises ``ValueError`` for an unknown explicit ``refresh_mode`` — a
        coding mistake, not a deployment state.
        """
        if refresh_mode is not None and refresh_mode not in _REFRESH_MODES:
            message = (
                f"refresh_mode must be one of {sorted(_REFRESH_MODES)} or None, "
                f"got {refresh_mode!r}"
            )
            raise ValueError(message)
        self._configured_url = url
        self._poll_seconds = poll_seconds
        self._fetch_timeout = fetch_timeout
        self._on_error: OnError = on_error if on_error is not None else _log_transition
        self._configured_refresh_mode = refresh_mode

        # Resolved from the environment on first use, like the URL.
        self._refresh_mode: RefreshMode = _DEFAULT_REFRESH_MODE

        # Readers touch `_snapshot` and nothing else. Publication is a single
        # attribute store of an already-built immutable tree, so a reader
        # sees either the whole old document or the whole new one.
        self._snapshot: Mapping[str, Any] = _EMPTY_SNAPSHOT

        # Refresher state, mutated only under `_refresh_lock`.
        self._etag: str | None = None
        self._failing = False
        self._last_attempt: float | None = None

        self._url: str | None = None
        self._started = False
        self._thread: threading.Thread | None = None
        self._start_lock = threading.Lock()
        self._refresh_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._first_attempt = threading.Event()
        self._stop = threading.Event()

    # ── public API ────────────────────────────────────────────────────────

    def enabled(self, name: str) -> bool:
        """Return whether the named flag is on.

        In the default background mode this performs no I/O and never blocks:
        the answer comes from the last document received. In on-demand mode it
        may fetch first, when the snapshot has aged past ``poll_seconds``,
        bounded by ``fetch_timeout``.

        **Never raises.** An unknown flag, an absent document, a malformed
        document, a missing ``CRU_FLAGS_URL`` and an unreachable flag service
        all answer ``False``.
        """
        try:
            self._refresh_if_stale()
            section = self._snapshot.get("Flags")
            if not isinstance(section, Mapping):
                return False
            entry = section.get(name)
            if not isinstance(entry, Mapping):
                return False
            # `is True` rather than truthiness: a document whose Enabled is
            # "true", 1 or null is malformed, and malformed reads as off.
            return entry.get("Enabled") is True
        except Exception as error:  # noqa: BLE001 - enabled() never raises; §4
            self._log_read_failure("enabled", error)
            return False

    def ready(self, timeout: float | None = None) -> bool:
        """Block until the first fetch attempt completes, then report success.

        Returns ``True`` once an attempt has finished — whether it succeeded
        or failed, since either way the answers are now final until the next
        poll — and ``False`` if ``timeout`` elapsed first. Returns ``False``
        immediately, without blocking, when the client is inert: there is no
        attempt to wait for. ``timeout=None`` waits for the attempt to
        finish, which is bounded in practice by ``fetch_timeout``.

        In on-demand mode there is no background attempt to wait for, so this
        *performs* the first attempt (like any other read) and reports whether
        one has now completed; ``timeout`` is unused.

        Never raises.
        """
        try:
            if not self._refresh_if_stale():
                return False
            if self._refresh_mode == "on-demand":
                return self._first_attempt.is_set()
            return self._first_attempt.wait(timeout)
        except Exception as error:  # noqa: BLE001 - ready() never raises; §4
            self._log_read_failure("ready", error)
            return False

    def snapshot(self) -> dict[str, Any]:
        """Return a plain copy of the last flag document received.

        ``{}`` before the first successful fetch. The result is a deep copy,
        so mutating it cannot corrupt library state, and it is
        JSON-serializable: ``json.dumps(client.snapshot())`` reproduces what
        the service sent. Never raises.
        """
        try:
            self._refresh_if_stale()
            return _thaw_document(self._snapshot)
        except Exception as error:  # noqa: BLE001 - snapshot() never raises; §4
            self._log_read_failure("snapshot", error)
            return {}

    def refresh(self, *, force: bool = False) -> bool:
        """Refresh the snapshot on this thread; return whether it is fresh.

        Fetches only when the last attempt is older than ``poll_seconds``,
        unless ``force`` is set. Returns ``True`` when a fetch attempt has
        completed and the most recent one succeeded — so ``False`` covers an
        inert client, a service that is down, and (with ``force=False``) a
        snapshot that is still within ``poll_seconds`` of a failed attempt.

        Useful in both modes: it is the refresh in on-demand mode, and an
        out-of-band poke in background mode. Blocks for at most
        ``fetch_timeout``. Never raises.
        """
        try:
            if not self._ensure_started():
                return False
            self._refresh(force=force)
            return self._first_attempt.is_set() and not self._failing
        except Exception as error:  # noqa: BLE001 - refresh() never raises; §4
            self._log_read_failure("refresh", error)
            return False

    def close(self) -> None:
        """Stop refreshing.

        Optional and terminal: the poller is a daemon thread that never
        delays interpreter shutdown, so most callers never need this, and a
        closed client never refreshes again — in either mode, and closing
        before the first lookup leaves the client permanently inert. Useful in
        tests. The last snapshot remains readable, and reads still never raise.
        """
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(_CLOSE_TIMEOUT)

    # ── startup ───────────────────────────────────────────────────────────

    def _ensure_started(self) -> bool:
        """Start the client if needed; return whether it is active."""
        # Double-checked locking: the fast path is a single attribute read,
        # which is all every call after the first one pays.
        if not self._started:
            with self._start_lock:
                if not self._started:
                    self._start()
        return self._url is not None and not self._stop.is_set()

    def _start(self) -> None:
        """Resolve configuration and start the poller. Call under the lock."""
        if self._stop.is_set():
            # close() before the first lookup: stay inert rather than start a
            # poller that would exit before its first attempt, leaving
            # ready() waiting for an event nobody will ever set.
            self._started = True
            return
        self._refresh_mode = self._resolve_refresh_mode()
        self._url = self._resolve_url()
        # Set before the thread starts so a racing caller that observes
        # `_started` can never start a second poller.
        self._started = True
        if self._url is None:
            return  # inert: no URL means no thread and no socket
        if self._refresh_mode == "on-demand":
            return  # the reading thread does the fetching; §5.1
        self._thread = threading.Thread(
            target=self._run,
            name=_THREAD_NAME,
            daemon=True,
        )
        self._thread.start()

    def _resolve_refresh_mode(self) -> RefreshMode:
        """Resolve the refresh mode from the constructor or the environment."""
        if self._configured_refresh_mode is not None:
            return self._configured_refresh_mode
        raw = os.environ.get(MODE_ENV_VAR)
        if raw is None or not raw.strip():
            return _DEFAULT_REFRESH_MODE
        mode = raw.strip().lower()
        if mode not in _REFRESH_MODES:
            # An unreadable env var must not stop the app booting: warn and
            # keep the default, exactly as for a non-http URL.
            message = (
                f"{MODE_ENV_VAR} must be one of {sorted(_REFRESH_MODES)}; "
                f"ignoring {raw!r} and polling in the background"
            )
            self._report(ValueError(message))
            return _DEFAULT_REFRESH_MODE
        # `mode` is one of the literals, which mypy cannot see through `in`.
        return mode  # type: ignore[return-value]

    def _resolve_url(self) -> str | None:
        """Resolve the document URL from the constructor or the environment."""
        raw = self._configured_url
        if raw is None:
            raw = os.environ.get(ENV_VAR)
        if raw is None or not raw.strip():
            return None
        url = raw.strip()
        scheme = urllib.parse.urlsplit(url).scheme.lower()
        if scheme not in _ALLOWED_SCHEMES:
            message = (
                f"{ENV_VAR} must be an http(s) URL; refusing to fetch flags from "
                f"{scheme or 'a schemeless URL'!r}"
            )
            self._report(ValueError(message))
            return None
        return url

    # ── polling ───────────────────────────────────────────────────────────

    def _refresh_if_stale(self) -> bool:
        """Refresh on this thread if in on-demand mode; return if active.

        In background mode this is exactly ``_ensure_started``: the read path
        keeps its no-I/O, no-lock guarantee.
        """
        active = self._ensure_started()
        if active and self._refresh_mode == "on-demand":
            self._refresh(force=False)
        return active

    def _refresh(self, *, force: bool) -> None:
        """Fetch on the calling thread, unless the snapshot is fresh enough."""
        if not force and not self._is_stale():
            return
        before = self._last_attempt
        with self._refresh_lock:
            # Concurrent request threads pile up on this lock. A fetch that
            # completed while we waited satisfies every one of them — forced
            # callers included — so only the first of them spends a request.
            if self._last_attempt != before:
                return
            if not force and not self._is_stale():
                return
            if self._stop.is_set():
                return
            self._attempt()

    def _is_stale(self) -> bool:
        """Report whether the last fetch attempt predates the interval."""
        last = self._last_attempt
        # Anchored on the attempt, not the success, so a dead flag service is
        # asked at most once per interval per process rather than once per read.
        return last is None or time.monotonic() - last >= self._poll_seconds

    def _run(self) -> None:
        """Poll until stopped. Runs on the daemon poller thread."""
        try:
            while not self._stop.is_set():
                with self._refresh_lock:
                    self._attempt()
                if self._stop.wait(self._next_interval()):
                    return
        finally:
            # However this thread ends, nothing is outstanding any more, so
            # release anyone blocked in ready() instead of stranding them.
            self._first_attempt.set()

    def _next_interval(self) -> float:
        """Return the next sleep duration: the poll interval ±20% jitter."""
        spread = self._poll_seconds * _JITTER
        # Jitter de-phases co-deployed pods so they stop stampeding the flag
        # service in lockstep. Not a security decision, so `random` is fine.
        return self._poll_seconds + random.uniform(-spread, spread)  # noqa: S311

    def _attempt(self) -> None:
        """Make exactly one fetch attempt and record its time and health.

        Call under ``_refresh_lock``: it serialises the poller thread against
        on-demand callers, so there is never more than one in-flight fetch.
        """
        try:
            self._fetch()
        except Exception as error:  # noqa: BLE001 - every failure is "stay static"
            self._note_failure(error)
        else:
            self._note_success()
        finally:
            self._last_attempt = time.monotonic()
            self._first_attempt.set()

    def _fetch(self) -> None:
        """Fetch the document once and publish it. Raises on failure.

        One ``fetch_timeout`` deadline covers the whole tick — every redirect
        hop and the body read — rather than restarting on each socket
        operation, so the bound the client promises its callers is the bound
        they get. See ``docs/design.md`` §7.
        """
        headers = {"Accept": "application/json", "User-Agent": _USER_AGENT}
        if self._etag is not None:
            headers["If-None-Match"] = self._etag

        deadline = time.monotonic() + self._fetch_timeout
        # `_url` is set before the thread starts and never changes, and the
        # scheme was validated in `_resolve_url`.
        url = str(self._url)

        for hop in range(_MAX_REDIRECTS + 1):
            request = urllib.request.Request(  # noqa: S310
                url,
                headers=headers,
                method="GET",
            )
            try:
                with _opener().open(request, timeout=_time_left(deadline)) as response:
                    body = _read_capped_body(response, deadline)
                    etag = response.headers.get("ETag")
            except urllib.error.HTTPError as error:
                with error:  # close the error body deterministically
                    status = error.code
                    location = error.headers.get("Location")
                if status == _HTTP_NOT_MODIFIED:
                    return  # unchanged: keep both the snapshot and the ETag
                if status == _HTTP_NOT_FOUND:
                    # "No flag document published yet" — a valid answer, not a
                    # failure. See docs/design.md §2.
                    self._publish(_EMPTY_SNAPSHOT, etag=None)
                    return
                if status not in _REDIRECT_STATUSES:
                    raise
                if hop == _MAX_REDIRECTS:
                    message = f"flag fetch exceeded {_MAX_REDIRECTS} redirects"
                    raise urllib.error.URLError(message) from error
                url = _redirect_target(url, location)
            else:
                document = json.loads(body)
                if not isinstance(document, dict):
                    message = (
                        f"flag document is a JSON {type(document).__name__}, "
                        f"expected an object"
                    )
                    raise TypeError(message)
                self._publish(_freeze_document(document), etag=etag)
                return

    def _publish(self, snapshot: Mapping[str, Any], etag: str | None) -> None:
        """Swap in a new snapshot atomically."""
        with self._write_lock:
            self._snapshot = snapshot
            self._etag = etag

    # ── health reporting ──────────────────────────────────────────────────

    def _note_failure(self, error: BaseException) -> None:
        """Report a failure, but only the one that starts a failing streak."""
        if self._failing:
            return
        self._failing = True
        self._report(error)

    def _note_success(self) -> None:
        """Report a recovery, but only when a failing streak just ended."""
        if not self._failing:
            return
        self._failing = False
        self._report(None)

    def _report(self, error: BaseException | None) -> None:
        """Invoke the ``on_error`` handler; a broken handler must not spread."""
        with contextlib.suppress(Exception):
            self._on_error(error)

    def _log_read_failure(self, operation: str, error: BaseException) -> None:
        """Debug-log an impossible failure on the read path, silently."""
        # Guarded: logging can itself fail during interpreter shutdown, and
        # the read path is contractually incapable of raising.
        with contextlib.suppress(Exception):
            _LOGGER.debug("cru_flags: %s() failed", operation, exc_info=error)


#: Process-wide client built from the environment. Importing it starts
#: nothing: the environment is read, and the poller started, on first use.
flags: Final = Client()
