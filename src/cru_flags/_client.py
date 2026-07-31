"""Polling feature-flag client.

Implementation of the specification in ``docs/design.md``. Nothing in this
module is public API except what ``cru_flags/__init__.py`` re-exports.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import random
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _distribution_version
from types import MappingProxyType
from typing import Any, Final, TypeAlias

try:
    __version__ = _distribution_version("cru-flags")
except PackageNotFoundError:  # pragma: no cover - source tree without install
    __version__ = "0.0.0+unknown"

#: Environment variable naming the flag document to poll.
ENV_VAR: Final = "CRU_FLAGS_URL"

#: Logger used by the default ``on_error`` handler.
LOGGER_NAME: Final = "cru_flags"

OnError: TypeAlias = Callable[[BaseException | None], None]
"""Health-transition callback.

Called with the offending exception when polling starts failing, and with
``None`` when polling recovers. Never called per-poll while a failure
persists. See ``docs/design.md`` §3.6.
"""

_LOGGER: Final = logging.getLogger(LOGGER_NAME)
_USER_AGENT: Final = f"cru-flags-python/{__version__}"
_THREAD_NAME: Final = "cru-flags-poller"

#: Fraction of the poll interval to jitter by, in each direction.
_JITTER: Final = 0.2

#: ``urllib`` will happily open ``file://``; we will not.
_ALLOWED_SCHEMES: Final = frozenset({"http", "https"})

_HTTP_NOT_MODIFIED: Final = 304
_HTTP_NOT_FOUND: Final = 404

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


class Client:
    """A feature-flag client backed by one polled JSON document.

    Reads its URL from the ``CRU_FLAGS_URL`` environment variable unless one
    is passed explicitly. The background poller starts on the first
    :meth:`enabled` or :meth:`ready` call — never at construction — and is a
    daemon thread, so it never delays interpreter shutdown.

    With no URL configured the client is *inert*: every flag is ``False``, no
    thread is started, no socket is opened and nothing is logged.
    """

    def __init__(
        self,
        url: str | None = None,
        poll_seconds: float = 30.0,
        fetch_timeout: float = 2.0,
        on_error: OnError | None = None,
    ) -> None:
        """Create a client.

        ``url`` defaults to the ``CRU_FLAGS_URL`` environment variable, read
        once on first use. ``poll_seconds`` is the refresh interval, jittered
        by ±20%. ``fetch_timeout`` is the per-request socket timeout; there
        are no retries within a poll. ``on_error`` is called on health
        transitions only, and defaults to a warning on the ``cru_flags``
        logger.
        """
        self._configured_url = url
        self._poll_seconds = poll_seconds
        self._fetch_timeout = fetch_timeout
        self._on_error: OnError = on_error if on_error is not None else _log_transition

        # Readers touch `_snapshot` and nothing else. Publication is a single
        # attribute store of an already-built immutable tree, so a reader
        # sees either the whole old document or the whole new one.
        self._snapshot: Mapping[str, Any] = _EMPTY_SNAPSHOT

        # Poller-thread-local state: no reader touches these.
        self._etag: str | None = None
        self._failing = False

        self._url: str | None = None
        self._started = False
        self._thread: threading.Thread | None = None
        self._start_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._first_attempt = threading.Event()
        self._stop = threading.Event()

    # ── public API ────────────────────────────────────────────────────────

    def enabled(self, name: str) -> bool:
        """Return whether the named flag is on.

        Performs no I/O and never blocks: the answer comes from the last
        document received. **Never raises.** An unknown flag, an absent
        document, a malformed document, a missing ``CRU_FLAGS_URL`` and an
        unreachable flag service all answer ``False``.
        """
        try:
            self._ensure_started()
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

        Never raises.
        """
        try:
            if not self._ensure_started():
                return False
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
            self._ensure_started()
            return _thaw_document(self._snapshot)
        except Exception as error:  # noqa: BLE001 - snapshot() never raises; §4
            self._log_read_failure("snapshot", error)
            return {}

    def close(self) -> None:
        """Stop the background poller.

        Optional and terminal: the poller is a daemon thread that never
        delays interpreter shutdown, so most callers never need this, and a
        closed client never polls again — closing before the first lookup
        leaves the client permanently inert. Useful in tests. The last
        snapshot remains readable, and reads still never raise.
        """
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(_CLOSE_TIMEOUT)

    # ── startup ───────────────────────────────────────────────────────────

    def _ensure_started(self) -> bool:
        """Start the poller if needed; return whether this client is active."""
        # Double-checked locking: the fast path is a single attribute read,
        # which is all every call after the first one pays.
        if not self._started:
            with self._start_lock:
                if not self._started:
                    self._start()
        return self._url is not None

    def _start(self) -> None:
        """Resolve configuration and start the poller. Call under the lock."""
        if self._stop.is_set():
            # close() before the first lookup: stay inert rather than start a
            # poller that would exit before its first attempt, leaving
            # ready() waiting for an event nobody will ever set.
            self._started = True
            return
        self._url = self._resolve_url()
        # Set before the thread starts so a racing caller that observes
        # `_started` can never start a second poller.
        self._started = True
        if self._url is None:
            return  # inert: no URL means no thread and no socket
        self._thread = threading.Thread(
            target=self._run,
            name=_THREAD_NAME,
            daemon=True,
        )
        self._thread.start()

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

    def _run(self) -> None:
        """Poll until stopped. Runs on the daemon poller thread."""
        try:
            while not self._stop.is_set():
                self._poll_once()
                self._first_attempt.set()
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

    def _poll_once(self) -> None:
        """Make exactly one fetch attempt and record the resulting health."""
        try:
            self._fetch()
        except Exception as error:  # noqa: BLE001 - every failure is "stay static"
            self._note_failure(error)
        else:
            self._note_success()

    def _fetch(self) -> None:
        """Fetch the document once and publish it. Raises on failure."""
        headers = {"Accept": "application/json", "User-Agent": _USER_AGENT}
        if self._etag is not None:
            headers["If-None-Match"] = self._etag

        # `_url` is set before the thread starts and never changes, and the
        # scheme was validated in `_resolve_url`.
        request = urllib.request.Request(  # noqa: S310
            str(self._url),
            headers=headers,
            method="GET",
        )
        try:
            with urllib.request.urlopen(  # noqa: S310
                request,
                timeout=self._fetch_timeout,
            ) as response:
                body = response.read()
                etag = response.headers.get("ETag")
        except urllib.error.HTTPError as error:
            with error:  # close the error body deterministically
                status = error.code
            if status == _HTTP_NOT_MODIFIED:
                return  # unchanged: keep both the snapshot and the ETag
            if status == _HTTP_NOT_FOUND:
                # "No flag document published yet" — a valid answer, not a
                # failure. See docs/design.md §2.
                self._publish(_EMPTY_SNAPSHOT, etag=None)
                return
            raise

        document = json.loads(body)
        if not isinstance(document, dict):
            message = (
                f"flag document is a JSON {type(document).__name__}, expected an object"
            )
            raise TypeError(message)
        self._publish(_freeze_document(document), etag=etag)

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
